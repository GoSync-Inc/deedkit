"""Effective-confinement self-check of the worker that sends files to the owner's Mac mini (ANYM-688).

The host firewall rules confine the worker's bridge to TCP 443 to the mini. Rules can be missing (a reboot, a reload,
a partial load, a start path that skips the operator's checks), so the worker tries to leave before it claims any job,
before EVERY claim, and in the background every `INTERVAL` seconds. If any attempt gets out, or any check cannot be
completed, it exits (code `EXIT_CODE`) and claims nothing.

What this is: a guard against the operational loss of the rules for the worker as shipped (every start path, a reload,
a partial load, a shadowed hook). What it is not: a defence against a compromised worker (code that skips the check) or
a proof of the whole allowlist: it samples the exits that matter (public addresses, public and configured resolvers,
the host and metadata addresses, other ports of the mini). The host rules stay the security boundary.

The attempts carry no data: TCP connects that are dropped when confined and UDP DNS queries with a random label.
Only a dropped attempt (timeout, no route) counts as confined. A connection, a refusal, a reply, an ICMP port-unreachable,
an unexpected local error (for instance EMFILE) or a check that does not finish in time all count as NOT confined.
"""
from collections.abc import Callable
import errno
import json
import os
import socket
import struct
import sys
import threading
import time
from urllib.parse import urlsplit

PUBLIC_TCP = (('1.1.1.1', 443), ('8.8.8.8', 443), ('2606:4700:4700::1111', 443), ('1.1.1.1', 53), ('169.254.169.254', 80))
PUBLIC_DNS = ('1.1.1.1', 53)
GATEWAY_PORTS = (22, 53, 80, 443)
MINI_OTHER_PORTS = (22, 80)
TIMEOUT = 2.0
FRESH = 0.0       # the proof is repeated before every claim
INTERVAL = 15.0   # and in the background while a job runs
EXIT_CODE = 78
DROPPED_V4 = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN}
DROPPED_V6 = DROPPED_V4 | {errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT}


class ConfinementLost(RuntimeError):
    """The worker could leave its network, or the proof could not be completed. `args[0]` lists the failed checks."""


class Network:
    """The real network layer; tests replace it with a fake."""
    def connect(self, host, port, timeout):
        with socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))

    def dns_reply(self, host, port, query, timeout):
        """True when anything comes back for `query`; ConnectionRefusedError when the port answered with an ICMP error."""
        with socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.send(query)
            return len(sock.recv(512)) > 0

    def nameservers(self):
        with open('/etc/resolv.conf') as handle:
            return [parts[1] for parts in (line.split() for line in handle) if len(parts) > 1 and parts[0] == 'nameserver']

    def gateway(self):
        """The default gateway of the container (the host side of its bridge), or None."""
        with open('/proc/net/route') as handle:
            for line in list(handle)[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == '00000000':
                    return socket.inet_ntoa(struct.pack('<I', int(fields[2], 16)))
        return None

    def resolve(self, name):
        return sorted({item[4][0] for item in socket.getaddrinfo(name, None)})


def dns_query(label):
    name = b''.join(bytes([len(part)]) + part.encode() for part in (label, 'example', 'com')) + b'\0'
    return os.urandom(2) + struct.pack('>HHHHH', 0x0100, 1, 0, 0, 0) + name + struct.pack('>HH', 1, 1)


def required(config):
    return any(profile.get('provider') == 'deedkit_inference' for profile in config['profiles'].values())


def mini_hosts(config):
    return sorted({urlsplit(profile['base_url']).hostname for profile in config['profiles'].values()
                   if profile.get('provider') == 'deedkit_inference'})


class Confinement:
    def __init__(self, hosts, *, network=None, clock=time.monotonic, timeout=TIMEOUT, fresh=FRESH, interval=INTERVAL):
        self.hosts, self.network, self.clock = list(hosts), network or Network(), clock
        self.timeout, self.fresh, self.interval = timeout, fresh, interval
        self.verified_at, self.lost = None, None
        self.lock, self.stopping = threading.Lock(), threading.Event()

    def got_out(self, host, port):
        """True: something answered, or the attempt could not be observed. False: the packet was dropped."""
        try:
            self.network.connect(host, port, self.timeout)
        except ConnectionRefusedError:
            return True        # a refusal is an answer; a dropped packet never gets one
        except TimeoutError:
            return False
        except OSError as error:
            return error.errno not in (DROPPED_V6 if ':' in host else DROPPED_V4)   # EMFILE and the like: unobserved
        return True

    def dns_got_out(self, host):
        try:
            return bool(self.network.dns_reply(host, 53, dns_query('probe' + os.urandom(4).hex()), self.timeout))
        except ConnectionRefusedError:
            return True        # ICMP port unreachable: the address answered
        except TimeoutError:
            return False
        except OSError as error:
            return error.errno not in (DROPPED_V6 if ':' in host else DROPPED_V4)

    def checks(self) -> list[tuple[str, Callable[[], bool]]]:
        """(name, test) for every check. Reading local facts (resolv.conf, routes) failing is itself a failed check."""
        checks: list[tuple[str, Callable[[], bool]]] = []
        for host, port in PUBLIC_TCP:
            checks.append((f'tcp:{host}:{port}', lambda host=host, port=port: self.got_out(host, port)))
        checks.append((f'dns:public_udp:{PUBLIC_DNS[0]}', lambda: self.dns_got_out(PUBLIC_DNS[0])))
        try:
            servers = list(dict.fromkeys(self.network.nameservers()))
        except OSError:
            servers = None
        if servers is None:
            checks.append(('resolv_conf_unreadable', lambda: True))
        for server in servers or []:
            checks.append((f'dns:configured_udp:{server}', lambda server=server: self.dns_got_out(server)))
            checks.append((f'dns:configured_tcp:{server}', lambda server=server: self.got_out(server, 53)))
        try:
            gateway = self.network.gateway()
        except (OSError, ValueError):
            gateway = None
            checks.append(('gateway_unreadable', lambda: True))
        for port in GATEWAY_PORTS if gateway else ():
            checks.append((f'host:{gateway}:{port}', lambda port=port: self.got_out(gateway, port)))
        for name in self.hosts:
            try:
                addresses = self.network.resolve(name)     # the pinned name; failing to resolve it is not confined-and-working
            except OSError:
                checks.append((f'mini_unresolved:{name}', lambda: True))
                continue
            for address in addresses:
                for port in MINI_OTHER_PORTS:
                    checks.append((f'mini:{name}:{port}', lambda address=address, port=port: self.got_out(address, port)))
        return checks

    def probe(self):
        """Run every check in parallel. The names of the checks that got out, errored or did not finish (empty: confined)."""
        outcome = {}
        threads = []
        checks = self.checks()

        def runner(index, test):
            try:
                outcome[index] = bool(test())
            except Exception:
                outcome[index] = True      # a check that cannot run proves nothing: not confined

        for index, (_, test) in enumerate(checks):
            threads.append(threading.Thread(target=runner, args=(index, test), daemon=True))
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + self.timeout + 1.0
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        # a check that has not finished by now is NOT confined (fail closed), whatever it would have said later
        return sorted(name for index, (name, _) in enumerate(checks) if outcome.get(index, True))

    def verify(self):
        with self.lock:
            failed = self.probe()
            if failed:
                self.lost = failed
                raise ConfinementLost(failed)
            self.verified_at = self.clock()

    def ensure(self):
        """Called before every claim: raise at once once lost; probe again when the last proof is older than `fresh`."""
        if self.lost:
            raise ConfinementLost(self.lost)
        if self.verified_at is None or self.clock() - self.verified_at >= self.fresh:
            self.verify()

    def start_monitor(self, on_lost):
        def watch():
            while not self.stopping.wait(self.interval):
                try:
                    self.verify()
                except ConfinementLost:
                    on_lost()
                    return
        threading.Thread(target=watch, daemon=True, name='confinement-monitor').start()

    def stop_monitor(self):
        self.stopping.set()


def main():
    """`python -m messaging_service.inference_confinement [mini-host ...]`: one probe, one JSON line, exit 78 if not confined."""
    guard = Confinement(sys.argv[1:])
    failed = guard.probe()
    print(json.dumps({'confined': not failed, 'failed': failed}))
    sys.exit(EXIT_CODE if failed else 0)


if __name__ == '__main__':
    main()
