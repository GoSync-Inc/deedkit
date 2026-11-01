"""Mini-facing enrichment worker egress (ANYM-688): the host rules and the connectivity check for the receipt.

`rules --mini-ip IP` prints the host firewall commands for the egress bridge dk-inference0 and the internal database
bridge dk-inferencedb0 (nothing here changes a firewall); `rules --undo` prints the mirror. `check` runs inside the
`enricher-mini` container, stdlib only:

    docker compose ... exec -T enricher-mini python -I - check --open db:5432 --open MINI:443 \\
        --blocked API_IP:8080 --blocked-name api:8080 --blocked GATEWAY:PORT --blocked PUBLIC_IP:443 \\
        --blocked PUBLIC_IP:53 --blocked-dns PUBLIC_IP --blocked-name example.com:443 < scripts/inference_egress_check.py

Blocked targets with an address are IP:PORT pairs with a known listener (production.md lists them): an address that
answers a refusal is not blocked (a DROP never sends one). `--blocked-name` is for names (the API alias, an external
name): it passes only when the name does not resolve; a lookup that succeeds fails, whatever a connection would do.
A failed lookup alone does not prove no query left: production.md watches the host's other interfaces for port 53. The positive control runs elsewhere: `listen` in
a sibling container, then `check --control IP:PORT` from another one that may reach it; it must connect, or "blocked"
in the worker's run could mean that nothing listens.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import time

BRIDGE = 'dk-inference0'
# The internal database bridge in gateway mode `isolated` holds no host address; its INPUT drop is the second layer.
DB_BRIDGE = 'dk-inferencedb0'
CHAIN = 'DEEDKIT-INFERENCE'
TAILNET = ipaddress.ip_network('100.64.0.0/10')
# Host-local, private, tailnet (CGNAT), link-local (with metadata 169.254.169.254), multicast and reserved. The final
# catch-all drops them too; they are listed so the rule set reads as the card's list.
DROPPED = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12',
           '192.0.0.0/24', '192.168.0.0/16', '198.18.0.0/15', '224.0.0.0/4', '240.0.0.0/4')


def mini_address(value):
    """The mini's tailnet IPv4 address; anything else (a name, IPv6, a network, outside 100.64.0.0/10) is refused."""
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f'{value!r} is not an IPv4 address') from None
    if address not in TAILNET:
        raise argparse.ArgumentTypeError(f'{value} is outside the tailnet range {TAILNET}')
    return address


def chain_rules(mini, table):
    """The chain for one table: create, empty, then the one allow first and every drop after it."""
    t = f'-t {table} ' if table else ''
    return ([f'iptables {t}-N {CHAIN}', f'iptables {t}-F {CHAIN}',
             # The one way out: TCP 443 to the mini. Everything below it is a drop.
             f'iptables {t}-A {CHAIN} -d {mini} -p tcp --dport 443 -j RETURN'] +
            [f'iptables {t}-A {CHAIN} -p {proto} --dport 53 -j DROP' for proto in ('udp', 'tcp')] +
            [f'iptables {t}-A {CHAIN} -d {network} -j DROP' for network in DROPPED] +
            [f'iptables {t}-A {CHAIN} -j DROP'])


def rules(mini):
    # Two hooks, the same chain. `raw` PREROUTING runs before conntrack and the filter FORWARD chain, where
    # Tailscale's `ts-forward` accept can precede DOCKER-USER (it installs itself at position 1 and tailscaled
    # reinstalls it on restart), so the raw hook cannot be passed over by a later reorder. DOCKER-USER stays as the
    # second layer.
    return (chain_rules(mini, '') + chain_rules(mini, 'raw') +
            [f'iptables -I DOCKER-USER -i {BRIDGE} -j {CHAIN}',
             f'iptables -t raw -I PREROUTING -i {BRIDGE} -j {CHAIN}',
             # Packets to the host itself are not forwarded, so DOCKER-USER never sees them; both bridges.
             *[f'iptables -I INPUT -i {bridge} -j DROP' for bridge in (BRIDGE, DB_BRIDGE)],
             # The bridges have no IPv6 (enable_ipv6: false); these drop ULA, NAT64 and everything else anyway.
             f'ip6tables -I DOCKER-USER -i {BRIDGE} -j DROP',
             f'ip6tables -t raw -I PREROUTING -i {BRIDGE} -j DROP',
             *[f'ip6tables -I INPUT -i {bridge} -j DROP' for bridge in (BRIDGE, DB_BRIDGE)]])


def undo():
    return [f'iptables -D DOCKER-USER -i {BRIDGE} -j {CHAIN}',
            f'iptables -t raw -D PREROUTING -i {BRIDGE} -j {CHAIN}',
            *[f'iptables -D INPUT -i {bridge} -j DROP' for bridge in (BRIDGE, DB_BRIDGE)],
            f'ip6tables -D DOCKER-USER -i {BRIDGE} -j DROP',
            f'ip6tables -t raw -D PREROUTING -i {BRIDGE} -j DROP',
            *[f'ip6tables -D INPUT -i {bridge} -j DROP' for bridge in (BRIDGE, DB_BRIDGE)],
            f'iptables -F {CHAIN}', f'iptables -X {CHAIN}',
            f'iptables -t raw -F {CHAIN}', f'iptables -t raw -X {CHAIN}']


def probe_name(target):
    """'unresolved' only when the name does not resolve; a lookup that succeeds means a resolver answered."""
    host, _, port = target.rpartition(':')
    try:
        socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
    except ValueError:
        return 'malformed'
    except (OSError, UnicodeError):
        return 'unresolved'
    return 'resolved'


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


def check(targets, timeout):
    results = []
    for target, expect in targets:
        if expect.endswith('dns'):
            outcome = probe_dns(target, timeout)
            passed = outcome == 'answered' if expect == 'open-dns' else outcome in ('timeout', 'unreachable')
        elif expect == 'blocked-name':
            # A name that resolves means a resolver answered from inside, whatever the connection then does.
            outcome = probe_name(target)
            passed = outcome == 'unresolved'
        else:
            outcome = probe(target, timeout, literal=expect == 'blocked')
            passed = outcome == 'connected' if expect in ('open', 'control') else outcome in ('timeout', 'unreachable')
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
    rule.add_argument('--mini-ip', type=mini_address, help="the Mac mini's tailnet IPv4 address (100.64.0.0/10)")
    rule.add_argument('--undo', action='store_true', help='print the commands that remove the rules')
    probe_parser = commands.add_parser('check', help='probe targets from inside the enricher-mini container')
    probe_parser.set_defaults(targets=[])
    probe_parser.add_argument('--open', action=Target, const='open', metavar='HOST:PORT',
                              help='must connect (the database, the mini on 443)')
    probe_parser.add_argument('--blocked', action=Target, const='blocked', metavar='IP:PORT',
                              help='an IP literal with a known listener: must get no answer')
    probe_parser.add_argument('--blocked-name', action=Target, const='blocked-name', metavar='NAME:PORT',
                              help='a name: must not resolve')
    probe_parser.add_argument('--blocked-dns', action=Target, const='blocked-dns', metavar='IP[:PORT]',
                              help='a UDP DNS query to this resolver must get no answer')
    probe_parser.add_argument('--control', action=Target, const='control', metavar='IP:PORT',
                              help='the positive control, run from a sibling container: must connect')
    probe_parser.add_argument('--timeout', type=float, default=3)
    listen = commands.add_parser('listen', help='a synthetic TCP listener for a positive control (another container)')
    listen.add_argument('--port', type=int, required=True)
    listen.add_argument('--seconds', type=float, default=120)
    args = parser.parse_args(argv)
    if args.command == 'listen':
        return serve(args.port, args.seconds)
    if args.command == 'rules':
        if not args.undo and args.mini_ip is None:
            parser.error('rules needs --mini-ip')
        print('\n'.join(undo() if args.undo else rules(args.mini_ip)))
        return 0
    report = check(args.targets, args.timeout)
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
