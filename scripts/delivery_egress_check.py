"""Dispatcher egress isolation (ANYM-534): the host rules and the connectivity check for the receipt.

`rules --resolver IP` prints the host firewall commands for the proxy's egress bridge dk-delivery0 and the two
internal bridges (the second layer under ANYM-581; nothing here changes a firewall). `check` runs inside the
dispatcher or the `delivery-egress` proxy container, stdlib only:

    docker compose ... exec -T delivery python -I - check --open db:5432 --proxy delivery-egress:3128 \\
        --open-proxied example.com --refused-proxied blocked.example --blocked 172.18.0.5:8080 \\
        ... < scripts/delivery_egress_check.py

`--open-proxied HOST` passes when the proxy resolves HOST and opens a tunnel to its first address on 443;
`--refused-proxied HOST` passes only on the proxy's 403 refusal (a host outside the policy or with a private answer).

`check --open-dns/--blocked-dns IP` sends one UDP DNS query straight to a resolver; `listen --port N` (in another
container, such as the matcher, which has no listener of its own) is a bounded synthetic positive control.

Blocked targets are explicit IP:PORT pairs with a known listener (production.md lists them), never names: a name
the dispatcher cannot resolve sends no packet and proves nothing. An open target passes only when TCP connects. A
blocked target passes only when nothing answers (timeout, no route): a refused connection means the address
answered, which a DROP rule never does, and an unresolved or malformed target fails.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import time

BRIDGE = 'dk-delivery0'
# Internal bridges still have a host gateway address: host listeners answer on it unless dropped (measured on
# Docker 29: an internal network has no DNS, other bridges or internet, but reaches the host gateway).
DB_BRIDGE = 'dk-deliverydb0'
PROXY_BRIDGE = 'dk-deliverypx0'
INTERNAL = (DB_BRIDGE, PROXY_BRIDGE)
CHAIN = 'DEEDKIT-DELIVERY'
# Host-local, private, tailnet (CGNAT), link-local (with metadata 169.254.169.254), multicast and reserved.
DROPPED = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12',
           '192.0.0.0/24', '192.168.0.0/16', '198.18.0.0/15', '224.0.0.0/4', '240.0.0.0/4')


def rules(resolver):
    allow = [f'iptables -A {CHAIN} -d {resolver} -p {proto} --dport 53 -j RETURN' for proto in ('udp', 'tcp')]
    return ([f'iptables -N {CHAIN}', f'iptables -F {CHAIN}', *allow] +
            [f'iptables -A {CHAIN} -d {network} -j DROP' for network in DROPPED] +
            [f'iptables -A {CHAIN} -p tcp --dport 443 -j RETURN', f'iptables -A {CHAIN} -j DROP',
             f'iptables -I DOCKER-USER -i {BRIDGE} -j {CHAIN}',
             # Packets to the host itself are not forwarded, so DOCKER-USER never sees them; both bridges.
             *[f'iptables -I INPUT -i {bridge} -j DROP' for bridge in (BRIDGE, *INTERNAL)],
             # The bridges have no IPv6 (enable_ipv6: false); these drop ULA, NAT64 and everything else anyway.
             f'ip6tables -I DOCKER-USER -i {BRIDGE} -j DROP',
             *[f'ip6tables -I INPUT -i {bridge} -j DROP' for bridge in (BRIDGE, *INTERNAL)]])


def undo():
    return [f'iptables -D DOCKER-USER -i {BRIDGE} -j {CHAIN}',
            *[f'iptables -D INPUT -i {bridge} -j DROP' for bridge in (BRIDGE, *INTERNAL)],
            f'ip6tables -D DOCKER-USER -i {BRIDGE} -j DROP',
            *[f'ip6tables -D INPUT -i {bridge} -j DROP' for bridge in (BRIDGE, *INTERNAL)],
            f'iptables -F {CHAIN}', f'iptables -X {CHAIN}']


def probe(target, timeout, literal=False):
    host, _, port = target.rpartition(':')
    try:
        if literal:
            ipaddress.ip_address(host.strip('[]'))
        address = socket.getaddrinfo(host.strip('[]'), int(port), type=socket.SOCK_STREAM)[0][4]
    except ValueError:
        return 'malformed'
    except (OSError, UnicodeError):
        return 'unresolved'
    try:
        with socket.create_connection(address[:2], timeout=timeout):
            return 'connected'
    except ConnectionRefusedError:
        return 'refused'
    except TimeoutError:
        return 'timeout'
    except OSError:
        return 'unreachable'


def probe_dns(target, timeout):
    """One UDP DNS query (example.com A) straight to IP[:PORT], default 53: answered, or why not."""
    host, port = target, '53'
    if target.startswith('['):
        host, _, rest = target[1:].partition(']')
        port = rest.removeprefix(':') or '53'
    elif target.count(':') == 1:
        host, port = target.split(':')
    try:
        address = ipaddress.ip_address(host)
        port = int(port)
    except ValueError:
        return 'malformed'
    query = bytes.fromhex('d53401000001000000000000076578616d706c6503636f6d0000010001')
    with socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.settimeout(timeout)
        try:
            udp.connect((str(address), port))
            udp.send(query)
            return 'answered' if udp.recv(512)[:2] == query[:2] else 'malformed'
        except ConnectionRefusedError:
            return 'refused'
        except TimeoutError:
            return 'timeout'
        except OSError:
            return 'unreachable'


def _proxy_head(proxy, head, timeout):
    """(status, reason, body) of one proxy request; the connection closes after the head (a tunnel is not used)."""
    host, _, port = proxy.rpartition(':')
    with socket.create_connection((host, int(port)), timeout=timeout) as sock:
        sock.sendall(head.encode('ascii'))
        data = b''
        while b'\r\n\r\n' not in data and len(data) < 8192:
            part = sock.recv(4096)
            if not part:
                break
            data += part
    line, _, rest = data.partition(b'\r\n')
    parts = line.decode('ascii', 'replace').split(' ')
    if len(parts) != 3 or not parts[1].isdigit():
        return 0, 'malformed', b''
    return int(parts[1]), parts[2], rest.partition(b'\r\n\r\n')[2]


def probe_proxied(proxy, host, timeout):
    """'tunnel' (RESOLVE then CONNECT to the first answer both 200), 'refused' (a 403), or why not."""
    try:
        status, reason, body = _proxy_head(proxy, f'RESOLVE {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n', timeout)
        if status == 403:
            return 'refused'
        if status != 200:
            return f'proxy_{status}_{reason}'
        address = json.loads(body)[0]
        status, reason, _ = _proxy_head(proxy, f'CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n'
                                        f'X-DeedKit-Address: {address}\r\n\r\n', timeout)
    except (OSError, ValueError, IndexError, UnicodeError):
        return 'unreachable'
    return 'tunnel' if status == 200 else 'refused' if status == 403 else f'proxy_{status}_{reason}'


def check(targets, timeout, proxy=None):
    results = []
    for target, expect in targets:
        if expect.endswith('proxied'):
            outcome = probe_proxied(proxy, target, timeout) if proxy else 'no_proxy'
            passed = outcome == ('tunnel' if expect == 'open-proxied' else 'refused')
            results.append({'target': target, 'expect': expect, 'outcome': outcome, 'passed': passed})
            continue
        if expect.endswith('dns'):
            outcome = probe_dns(target, timeout)
            passed = outcome == 'answered' if expect == 'open-dns' else outcome in ('timeout', 'unreachable')
            results.append({'target': target, 'expect': expect, 'outcome': outcome, 'passed': passed})
            continue
        outcome = probe(target, timeout, literal=expect == 'blocked')
        passed = outcome == 'connected' if expect == 'open' else outcome in ('timeout', 'unreachable')
        results.append({'target': target, 'expect': expect, 'outcome': outcome, 'passed': passed})
    return {'passed': bool(results) and all(item['passed'] for item in results), 'results': results}


def serve(port, seconds):
    """Accept and close TCP connections on every address for a bounded time; prints the bound port first."""
    deadline = time.monotonic() + seconds
    with socket.create_server(('', port), backlog=16) as server:
        print(json.dumps({'listening': server.getsockname()[1]}), flush=True)
        while (left := deadline - time.monotonic()) > 0:
            server.settimeout(left)
            try:
                server.accept()[0].close()
            except TimeoutError:
                break
    return 0


class Target(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        namespace.targets.append((value, self.const))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    rule = commands.add_parser('rules', help='print the host firewall commands')
    rule.add_argument('--resolver', type=ipaddress.IPv4Address, help='the DNS resolver the containers use')
    rule.add_argument('--undo', action='store_true', help='print the commands that remove the rules')
    probe_parser = commands.add_parser('check', help='probe targets from inside the dispatcher container')
    probe_parser.set_defaults(targets=[])
    probe_parser.add_argument('--open', action=Target, const='open', metavar='HOST:PORT')
    probe_parser.add_argument('--blocked', action=Target, const='blocked', metavar='IP:PORT')
    probe_parser.add_argument('--open-dns', action=Target, const='open-dns', metavar='IP[:PORT]',
                              help='a UDP DNS query to this resolver must be answered')
    probe_parser.add_argument('--blocked-dns', action=Target, const='blocked-dns', metavar='IP[:PORT]',
                              help='a UDP DNS query to this resolver must get no answer')
    probe_parser.add_argument('--proxy', metavar='HOST:PORT', help='the delivery egress proxy (ANYM-581)')
    probe_parser.add_argument('--open-proxied', action=Target, const='open-proxied', metavar='HOST',
                              help='the proxy must resolve HOST and open a tunnel to it')
    probe_parser.add_argument('--refused-proxied', action=Target, const='refused-proxied', metavar='HOST',
                              help='the proxy must refuse HOST with 403')
    probe_parser.add_argument('--timeout', type=float, default=3)
    listen = commands.add_parser('listen', help='a synthetic TCP listener for a positive control (another container)')
    listen.add_argument('--port', type=int, required=True)
    listen.add_argument('--seconds', type=float, default=120)
    args = parser.parse_args(argv)
    if args.command == 'listen':
        return serve(args.port, args.seconds)
    if args.command == 'rules':
        if not args.undo and args.resolver is None:
            parser.error('rules needs --resolver')
        print('\n'.join(undo() if args.undo else rules(args.resolver)))
        return 0
    report = check(args.targets, args.timeout, args.proxy)
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
