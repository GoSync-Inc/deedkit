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

Persistence (ANYM-693, as root on the host, nothing here changes a firewall by itself): `rules --mini-ip IP --format ufw
--family v4|v6 [--source /etc/ufw/before.rules]` prints the block for ufw's rules files (or the file with the block
merged in), `rules --mini-ip IP --format after-init` prints /etc/ufw/after.init (the raw-table half, which ufw's rules
files cannot hold), and `verify` compares the repository (`INFERENCE_MINI_IP` in the env file), the persisted files and the
kernel: `ok`, or `<pair> <code>` per differing pair (file_vs_repo, kernel_vs_repo, file_vs_kernel), exit 1.
"""
from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import re
import socket
import sys
import time
from pathlib import Path

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


def persist():
    """firewall_persist.py next to this file (not needed by `check`, which also runs from stdin)."""
    path = Path(__file__).resolve().with_name('firewall_persist.py')
    spec = importlib.util.spec_from_file_location('firewall_persist', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The raw-table half of the rules, run by ufw's after.init hook. Heredoc lines start at column 0 on purpose.
AFTER_INIT = r"""#!/bin/sh
# Generated by scripts/inference_egress_check.py rules --format after-init (ANYM-693); do not edit by hand.
# ufw runs this with `start` once it has loaded its rules (ufw.service starts before docker.service). It puts back the
# raw-table half of the mini worker's egress rules, which ufw's rules files cannot hold: restoring them would flush
# Docker's own raw rules. The chain is replaced in one kernel commit, so a failure leaves the previous complete chain
# and never an empty one; the jump is added only after the chain is complete; every command is bounded; a failure is
# logged and recorded, and the step still exits 0 so that ufw and Docker start. The whole hook, diagnostics and the
# record included, runs under an outer bound of 60 s (a command that ignores TERM is killed 2 s later).
STATE=${DEEDKIT_RAW_STATE:-/var/lib/deedkit/raw-step.json}
LIMIT=${DEEDKIT_STEP_TIMEOUT:-30}
if [ "$LIMIT" -ge 1 ] 2>/dev/null && [ "$LIMIT" -le 30 ] 2>/dev/null; then :; else LIMIT=30; fi
log() { echo "deedkit-inference raw step: $*" >&2; timeout -k 1 5 logger -t deedkit-inference-raw "$*" 2>/dev/null || true; }
record() {
    up=$(cut -d' ' -f1 /proc/uptime 2>/dev/null) || up=0
    boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null) || boot=unknown
    timeout -k 1 5 sh -c 'mkdir -p "$(dirname "$1")" && printf "%s\n" "{\"boot_id\": \"$2\", \"uptime\": $3, \"ok\": $4, \"reason\": \"$5\"}" > "$1.tmp" && mv "$1.tmp" "$1"' \
        record "$STATE" "$boot" "${up:-0}" "$1" "$2" 2>/dev/null
    return 0
}
fail() { log "failed: $1"; record false "$1"; exit 0; }
ensure() {
    bin=$1; shift
    timeout -k 2 "$LIMIT" "$bin" -t raw -C "$@" 2>/dev/null
    rc=$?
    [ "$rc" -eq 0 ] && return 0
    [ "$rc" -eq 1 ] || return 1
    timeout -k 2 "$LIMIT" "$bin" -t raw -I "$@"
}
step() {
    timeout -k 2 "$LIMIT" iptables-restore --noflush -T raw <<'DEEDKIT_V4' || fail restore_v4
@RAW@
DEEDKIT_V4
@ENSURE@
    record true ok
}
if [ "$1" = start ] && [ -z "$DEEDKIT_RAW_INNER" ]; then
    DEEDKIT_RAW_INNER=1 timeout -k 2 60 sh "$0" start
    rc=$?
    if [ "$rc" -ne 0 ]; then log "failed: hook rc=$rc"; record false "hook_$rc"; fi
    exit 0
fi
case "$1" in
    start) step ;;
esac
exit 0
"""


def spec(mini):
    """The canonical rules per table, read back from `rules()` (the one rule list), and the generated after.init."""
    lines = [line for line in rules(mini) if ' -N ' not in line and ' -F ' not in line]
    tables = persist().spec_from_commands(lines, CHAIN)
    raw = ['*raw', f':{CHAIN} - [0:0]', *tables['raw']['chain'], 'COMMIT']
    ensure = [f'    ensure {binary} {hook.removeprefix("-A ")} || fail hook_{family}'
              for binary, family, table in (('iptables', 'v4', 'raw'), ('ip6tables', 'v6', 'raw6'))
              for hook in tables[table]['hooks']]
    text = AFTER_INIT.replace('@RAW@', '\n'.join(raw)).replace('@ENSURE@', '\n'.join(ensure))
    return {'chain': CHAIN, 'bridges': (BRIDGE, DB_BRIDGE), 'tables': tables, 'after_init': text}


def ufw_form(mini, family, source):
    """The block for before.rules (v4) or before6.rules (v6), or `source` with it merged in."""
    module, built = persist(), spec(mini)
    block = module.ufw_block(built['tables'], 'deedkit-inference', CHAIN, family, False)
    if source is None:
        return block
    return module.merge_block(Path(source).read_text(), block, 'deedkit-inference', r'DEEDKIT-DELIVERY|dk-delivery', False,
                              '# deedkit-delivery end')


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
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['verify']:
        address = re.compile(r'^-A DEEDKIT-INFERENCE -d \S+ -p tcp --dport 443 -j RETURN$')
        return persist().verify_cli(argv[1:], lambda value: spec(mini_address(value)), 'INFERENCE_MINI_IP',
                                    'compare the repository, the ufw files and the kernel for the mini worker rules', address)
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    rule = commands.add_parser('rules', help='print the host firewall commands')
    rule.add_argument('--mini-ip', type=mini_address, help="the Mac mini's tailnet IPv4 address (100.64.0.0/10)")
    rule.add_argument('--undo', action='store_true', help='print the commands that remove the rules')
    rule.add_argument('--format', choices=('commands', 'ufw', 'after-init'), default='commands',
                      help='commands (default), the block for ufw rules files, or the after.init hook')
    rule.add_argument('--family', choices=('v4', 'v6'), help='with --format ufw: before.rules (v4) or before6.rules (v6)')
    rule.add_argument('--source', help='with --format ufw: the rules file to merge the block into (printed whole)')
    commands.add_parser('verify', add_help=False, help='compare repository, ufw files and kernel (root, read-only)')
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
        if args.format != 'commands' and (args.undo or (args.format == 'ufw' and args.family is None)):
            parser.error('--format ufw needs --family and cannot be combined with --undo; --undo prints commands only')
        if args.format == 'ufw':
            print(ufw_form(args.mini_ip, args.family, args.source), end='')
        elif args.format == 'after-init':
            print(spec(args.mini_ip)['after_init'], end='')
        else:
            print('\n'.join(undo() if args.undo else rules(args.mini_ip)))
        return 0
    report = check(args.targets, args.timeout)
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
