"""Effective-confinement self-check of the worker that sends files to the owner's Mac mini (ANYM-688).

The host firewall rules confine the worker's bridge to TCP 443 to the mini. Rules can be missing (a reboot, a reload,
a partial load, a start path that bypasses the operator's checks), so the worker proves the confinement itself, by
trying to leave, before it claims any job, before a claim when the last proof is older than `FRESH` seconds, and in the
background every `INTERVAL` seconds. If any attempt gets out, it exits (code `EXIT_CODE`) and claims nothing.

The attempts carry no data: TCP connects that are dropped when confined (a public IPv4 and IPv6 address on 443, a
public resolver on 53, two other ports of the mini), one DNS query with a random label to a public resolver, and one
lookup of a random name through the system resolver. Only a dropped attempt (no answer, or no route) counts as
confined; a connection, a refusal or a reply means something answered and the worker is not confined.
"""
import json
import os
import socket
import struct
import sys
import threading
import time
from urllib.parse import urlsplit

PUBLIC_TCP = (('1.1.1.1', 443), ('8.8.8.8', 443), ('2606:4700:4700::1111', 443), ('1.1.1.1', 53))
PUBLIC_DNS = ('1.1.1.1', 53)
MINI_OTHER_PORTS = (22, 80)
TIMEOUT = 2.0
FRESH = 60.0
INTERVAL = 120.0
EXIT_CODE = 78


class ConfinementLost(RuntimeError):
    """The worker could leave its network. `args[0]` lists the failed checks (names only)."""


class Network:
    """The real network layer; tests replace it with a fake."""
    def connect(self, host, port, timeout):
        with socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))

    def dns_reply(self, host, port, query, timeout):
        """True when anything that looks like an answer to `query` comes back."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(query, (host, port))
            data, _ = sock.recvfrom(512)
        return len(data) >= 12 and data[:2] == query[:2]

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

    def reaches(self, host, port):
        try:
            self.network.connect(host, port, self.timeout)
        except ConnectionRefusedError:
            return True        # a refusal is an answer; a dropped packet never gets one
        except OSError:
            return False       # timeout, no route, unreachable network
        return True

    def probe(self):
        """Every check, in parallel and bounded; the names of those that got out (empty: confined)."""
        failed, jobs = [], []

        def job(name, test):
            def run():
                try:
                    out = test()
                except Exception:
                    out = True     # a check that cannot run proves nothing: not confined
                if out:
                    failed.append(name)
            jobs.append(threading.Thread(target=run, daemon=True))

        for host, port in PUBLIC_TCP:
            job(f'tcp:{host}:{port}', lambda host=host, port=port: self.reaches(host, port))
        job('dns:public_udp', lambda: self.dns_reply_leaks())
        job('dns:system_resolver', lambda: self.system_resolver_answers())
        for name in self.hosts:
            job(f'mini_ports:{name}', lambda name=name: self.mini_other_ports_open(name))
        for thread in jobs:
            thread.start()
        deadline = time.monotonic() + self.timeout + 1.0
        for thread in jobs:
            thread.join(max(0.0, deadline - time.monotonic()))
        return sorted(failed)

    def dns_reply_leaks(self):
        try:
            return bool(self.network.dns_reply(*PUBLIC_DNS, dns_query('probe' + os.urandom(4).hex()), self.timeout))
        except OSError:
            return False

    def system_resolver_answers(self):
        try:
            return bool(self.network.resolve(f'probe-{os.urandom(6).hex()}.example.com'))
        except socket.gaierror as error:
            # NXDOMAIN, SERVFAIL and REFUSED are answers from a resolver; only silence (EAI_AGAIN) is confinement.
            return error.errno != socket.EAI_AGAIN
        except OSError:
            return False

    def mini_other_ports_open(self, name):
        addresses = self.network.resolve(name)     # the pinned name; failing to resolve it is not confined-and-working
        return any(self.reaches(address, port) for address in addresses for port in MINI_OTHER_PORTS)

    def verify(self):
        with self.lock:
            failed = self.probe()
            if failed:
                self.lost = failed
                raise ConfinementLost(failed)
            self.verified_at = self.clock()

    def ensure(self):
        """Called before a claim: raise at once once lost; probe again when the last proof is older than `fresh`."""
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
