"""Recordings backend egress (ANYM-712): the host rules and the connectivity check for the receipt.

`rules --resolver IP` prints the host firewall commands for the egress bridge dk-recordings0 and the internal backend
bridge dk-recbackend0 (nothing here changes a firewall); `rules --undo` prints the mirror. The container may use DNS
to the host resolver and TCP 443 to public addresses only; the host, the tailnet, metadata and every private network are
dropped, and so is IPv6. `check` runs inside the `recordings-api` container, stdlib only:

    docker compose ... exec -T recordings-api python -I - check --open api.omi.me:443 --open-dns RESOLVER \\
        --blocked HOST_BRIDGE_IP:PORT --blocked TAILNET_IP:PORT --blocked GATEWAY_IP:PORT < scripts/recordings_egress_check.py

Blocked targets are IP:PORT pairs with a known listener (production.md lists them): an address that answers a
refusal is not blocked (a DROP never sends one); an unresolved or malformed target fails. `listen --port N` (in another
container) is a bounded synthetic listener for the positive control (`check --control IP:PORT`).

Persistence (ANYM-693 family, as root on the host, nothing here changes a firewall by itself):
`rules --resolver IP --format ufw --family v4|v6 [--source /etc/ufw/before.rules]` prints the block for ufw's rules files
(or the file with the block merged in), `rules --resolver IP --mini-ip IP --format after-init` prints the one generated
/etc/ufw/after.init (the raw-table halves of the inference and the recordings rules), and `verify` compares the repository
(`RECORDINGS_RESOLVER` and `INFERENCE_MINI_IP` in the env file), the persisted files and the kernel: `ok`, or `<pair> <code>`
per differing pair (file_vs_repo, kernel_vs_repo, file_vs_kernel), exit 1.
"""
from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import socket
import sys
import time
from pathlib import Path

BRIDGE = 'dk-recordings0'
# The internal backend bridge holds no host address (gateway mode `isolated`); its INPUT drop is the second layer.
BACKEND_BRIDGE = 'dk-recbackend0'
CHAIN = 'DEEDKIT-RECORDINGS'
# The port recordings-api listens on: the one thing a peer on the backend bridge may open a connection to.
SERVICE_PORT = 13234
# Host-local, private, tailnet (CGNAT), link-local (with metadata 169.254.169.254), multicast and reserved.
DROPPED = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12',
           '192.0.0.0/24', '192.168.0.0/16', '198.18.0.0/15', '224.0.0.0/4', '240.0.0.0/4')


def chain_rules(resolver, table):
    """The chain for one table: create, empty, the resolver, every drop, then the one public allow and a final drop."""
    t = f'-t {table} ' if table else ''
    # The backend bridge is internal, but peers on it still reach each other (the gateway listens there on 0.0.0.0): in the
    # filter table, where conntrack state exists, a connection may be OPENED only to the service port (the gateway's
    # requests); replies pass, and the recordings container cannot open one to the gateway or any other peer. The raw
    # table runs before conntrack and holds the egress half only.
    backend = [] if table else [
        f'iptables -A {CHAIN} -i {BACKEND_BRIDGE} -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN',
        f'iptables -A {CHAIN} -i {BACKEND_BRIDGE} -p tcp --dport {SERVICE_PORT} -j RETURN',
        f'iptables -A {CHAIN} -i {BACKEND_BRIDGE} -j DROP']
    return ([f'iptables {t}-N {CHAIN}', f'iptables {t}-F {CHAIN}'] + backend +
            [f'iptables {t}-A {CHAIN} -d {resolver} -p {proto} --dport 53 -j RETURN' for proto in ('udp', 'tcp')] +
            [f'iptables {t}-A {CHAIN} -d {network} -j DROP' for network in DROPPED] +
            [f'iptables {t}-A {CHAIN} -p tcp --dport 443 -j RETURN', f'iptables {t}-A {CHAIN} -j DROP'])


def rules(resolver):
    # Two hooks, the same chain. `raw` PREROUTING runs before conntrack and the filter FORWARD chain, where
    # Tailscale's `ts-forward` accept can precede DOCKER-USER (it installs itself at position 1 and tailscaled
    # reinstalls it on restart), so the raw hook cannot be passed over by a later reorder. DOCKER-USER stays as the
    # second layer. Own chain, own hooks: the inference and delivery rules are never edited.
    return (chain_rules(resolver, '') + chain_rules(resolver, 'raw') +
            [f'iptables -I DOCKER-USER -i {BRIDGE} -j {CHAIN}',
             f'iptables -t raw -I PREROUTING -i {BRIDGE} -j {CHAIN}',
             f'iptables -I DOCKER-USER -i {BACKEND_BRIDGE} -j {CHAIN}',
             # Packets to the host itself are not forwarded, so DOCKER-USER never sees them; both bridges.
             *[f'iptables -I INPUT -i {bridge} -j DROP' for bridge in (BRIDGE, BACKEND_BRIDGE)],
             # The bridges have no IPv6 (enable_ipv6: false); these drop ULA, NAT64 and everything else anyway.
             f'ip6tables -I DOCKER-USER -i {BRIDGE} -j DROP',
             f'ip6tables -t raw -I PREROUTING -i {BRIDGE} -j DROP',
             *[f'ip6tables -I INPUT -i {bridge} -j DROP' for bridge in (BRIDGE, BACKEND_BRIDGE)]])


def undo():
    return [f'iptables -D DOCKER-USER -i {BRIDGE} -j {CHAIN}',
            f'iptables -t raw -D PREROUTING -i {BRIDGE} -j {CHAIN}',
            f'iptables -D DOCKER-USER -i {BACKEND_BRIDGE} -j {CHAIN}',
            *[f'iptables -D INPUT -i {bridge} -j DROP' for bridge in (BRIDGE, BACKEND_BRIDGE)],
            f'ip6tables -D DOCKER-USER -i {BRIDGE} -j DROP',
            f'ip6tables -t raw -D PREROUTING -i {BRIDGE} -j DROP',
            *[f'ip6tables -D INPUT -i {bridge} -j DROP' for bridge in (BRIDGE, BACKEND_BRIDGE)],
            f'iptables -F {CHAIN}', f'iptables -X {CHAIN}',
            f'iptables -t raw -F {CHAIN}', f'iptables -t raw -X {CHAIN}']


def persist():
    """firewall_persist.py next to this file (not needed by `check`, which also runs from stdin)."""
    path = Path(__file__).resolve().with_name('firewall_persist.py')
    spec = importlib.util.spec_from_file_location('firewall_persist', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The recordings half of the generated /etc/ufw/after.init. It is a function the inference step calls last (see
# firewall_persist.compose_after_init) as a bounded child: its exit status (0, 11 restore, 12 and 13 the IPv4 and IPv6
# hook) is recorded in the same raw-step record, and it never stops the inference half or the hook. Heredoc lines start at column 0 on purpose.
STEP = r"""recordings_step() {
    timeout -k 2 "$LIMIT" iptables-restore --noflush -T raw <<'DEEDKIT_REC_V4' || return 11
@RAW@
DEEDKIT_REC_V4
@ENSURE@
    return 0
}
"""
# The failure marker `extract_hook` reads the recordings hook lines by.
HOOK_FAIL = r'return 1[23]'


def step_text(tables):
    """The recordings function of the generated hook, from the canonical tables."""
    raw = ['*raw', f':{CHAIN} - [0:0]', *tables['raw']['chain'], 'COMMIT']
    ensure = [f'    ensure {binary} {hook.removeprefix("-A ")} || return {code}'
              for binary, code, table in (('iptables', 12, 'raw'), ('ip6tables', 13, 'raw6'))
              for hook in tables[table]['hooks']]
    return STEP.replace('@RAW@', '\n'.join(raw)).replace('@ENSURE@', '\n'.join(ensure))


def spec(resolver, mini=None):
    """The canonical rules per table, read back from `rules()`, and the generated after.init (composed with inference)."""
    lines = [line for line in rules(resolver) if ' -N ' not in line and ' -F ' not in line]
    module = persist()
    tables = module.spec_from_commands(lines, CHAIN)
    text = None
    if mini is not None:
        text = module.compose_after_init(module.load('inference_egress_check').spec(mini)['after_init'], step_text(tables))
    return {'chain': CHAIN, 'bridges': (BRIDGE, BACKEND_BRIDGE), 'tables': tables, 'after_init': text,
            'hook_fail': HOOK_FAIL}


def ufw_form(resolver, family, source):
    """The block for before.rules (v4) or before6.rules (v6), or `source` with it merged in."""
    module, built = persist(), spec(resolver)
    block = module.ufw_block(built['tables'], 'deedkit-recordings', CHAIN, family, False)
    if source is None:
        return block
    return module.merge_block(Path(source).read_text(), block, 'deedkit-recordings', r'DEEDKIT-DELIVERY|dk-delivery', False,
                              '# deedkit-delivery end')


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
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['verify']:
        return persist().verify_cli(argv[1:], lambda value, mini: spec(ipaddress.IPv4Address(value), ipaddress.IPv4Address(mini)),
                                    'RECORDINGS_RESOLVER',
                                    'compare the repository, the ufw files and the kernel for the recordings rules',
                                    also='INFERENCE_MINI_IP', also_required=True)
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    rule = commands.add_parser('rules', help='print the host firewall commands')
    rule.add_argument('--resolver', type=ipaddress.IPv4Address, help='the DNS resolver the container uses')
    rule.add_argument('--mini-ip', type=ipaddress.IPv4Address, help='with --format after-init: the inference mini address')
    rule.add_argument('--undo', action='store_true', help='print the commands that remove the rules')
    rule.add_argument('--format', choices=('commands', 'ufw', 'after-init'), default='commands',
                      help='commands (default), the block for ufw rules files, or the after.init hook')
    rule.add_argument('--family', choices=('v4', 'v6'), help='with --format ufw: before.rules (v4) or before6.rules (v6)')
    rule.add_argument('--source', help='with --format ufw: the rules file to merge the block into (printed whole)')
    commands.add_parser('verify', add_help=False, help='compare repository, ufw files and kernel (root, read-only)')
    probe_parser = commands.add_parser('check', help='probe targets from inside the recordings-api container')
    probe_parser.set_defaults(targets=[])
    probe_parser.add_argument('--open', action=Target, const='open', metavar='HOST:PORT', help='must connect (a public 443 target)')
    probe_parser.add_argument('--blocked', action=Target, const='blocked', metavar='IP:PORT',
                              help='an IP literal with a known listener: must get no answer')
    probe_parser.add_argument('--open-dns', action=Target, const='open-dns', metavar='IP[:PORT]',
                              help='a UDP DNS query to this resolver must be answered')
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
        if not args.undo and args.resolver is None:
            parser.error('rules needs --resolver')
        if args.format != 'commands' and (args.undo or (args.format == 'ufw' and args.family is None)):
            parser.error('--format ufw needs --family and cannot be combined with --undo; --undo prints commands only')
        if args.format == 'after-init' and args.mini_ip is None:
            parser.error('--format after-init needs --mini-ip (the one hook holds the inference and the recordings halves)')
        if args.format == 'ufw':
            print(ufw_form(args.resolver, args.family, args.source), end='')
        elif args.format == 'after-init':
            print(spec(args.resolver, args.mini_ip)['after_init'], end='')
        else:
            print('\n'.join(undo() if args.undo else rules(args.resolver)))
        return 0
    report = check(args.targets, args.timeout)
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
