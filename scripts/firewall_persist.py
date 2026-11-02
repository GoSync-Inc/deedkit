"""Persisted host firewall blocks (ANYM-693): the parts `inference_egress_check.py` and `delivery_egress_check.py` share.

Both scripts print their rules as commands (`rules`), as a block for ufw's /etc/ufw/before.rules and before6.rules
(`rules --format ufw`) and compare three texts with `verify`: the repository (the authority, generated from the same rule
list as the command form), the persisted files and the kernel (`iptables-save`). This module holds the parsing, the
comparison, the block merge and the boot receipt; stdlib only, run by root on the host from /usr/local/lib/deedkit.

    firewall_persist.py receipt        what deedkit-firewall-verify.service runs at each boot

Nothing here changes a firewall: `verify` and `receipt` only read.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

# v4 filter, v4 raw, v6 filter, v6 raw. The raw pair is reported as `raw`: it is loaded by one step.
TABLES = ('filter', 'raw', 'v6', 'raw6')
# ufw's own host-bound chain per family: the kernel's INPUT jumps to it, the rules files hold the drops in it.
UFW_INPUT = {'v4': 'ufw-before-input', 'v6': 'ufw6-before-input'}
ENV_FILE = '/opt/personal-os-probe/.env'
BOOT_ID = '/proc/sys/kernel/random/boot_id'
RECEIPT = '/var/lib/deedkit/firewall-verify.json'
RAW_STATE = '/var/lib/deedkit/raw-step.json'
LIMIT = 30


def die(message):
    print(message, file=sys.stderr)
    raise SystemExit(2)


def words(rule):
    """The arguments of a canonical rule, quoting honoured: text inside an option value (a `--comment`) is one argument."""
    return shlex.split(rule)


def join(tokens):
    """The inverse of `words`: an argument with whitespace or a quote keeps its quoting, so boundaries survive."""
    return ' '.join(shlex.quote(token) if re.search(r'[\s\'"\\]', token) or not token else token for token in tokens)


# Options whose value is free text chosen by whoever wrote the rule: a value such as "-i" or "! -i x" is text, not an option.
FREE_TEXT = ('--comment', '--log-prefix', '--string', '--hex-string', '--name', '--hashlimit-name', '--match-set', '--set-name',
             '--helper', '--cmd-owner', '--ctlabel')


def fields(rule):
    """The arguments of a canonical rule for analysis: the value of a free-text option can never pose as an option.

    Anything still ambiguous (an option twice, an interface after a match) is read by the callers as "may match", so a rule
    that hides an allowance behind option-shaped text is rejected, not trusted.
    """
    tokens = words(rule)
    return ['\0' + token if index and tokens[index - 1] in FREE_TEXT else token for index, token in enumerate(tokens)]


def interface_index(tokens):
    """Where the one `-i` condition is, or None when there is none or the rule is ambiguous about it (twice, or after a match)."""
    if tokens.count('-i') != 1:
        return None
    index = tokens.index('-i')
    return None if '-m' in tokens[:index] or index + 1 >= len(tokens) else index


def canon(line):
    """One rule in a form that is the same in our text and in `iptables-save`: no `-m tcp` after `-p tcp`, no /32."""
    tokens = shlex.split(line)
    proto = tokens[tokens.index('-p') + 1] if '-p' in tokens else None
    out, i = [], 0
    while i < len(tokens):
        if tokens[i] == '-m' and i + 1 < len(tokens) and tokens[i + 1] == proto:
            i += 2
        elif tokens[i] in ('-d', '-s') and i + 1 < len(tokens):
            out += [tokens[i], tokens[i + 1].removesuffix('/32')]
            i += 2
        else:
            out.append(tokens[i])
            i += 1
    if out and out[0] == '-I':
        out[0] = '-A'
    return join(out)


def empty_table():
    return {'chain': [], 'hooks': [], 'inputs': []}


def spec_from_commands(commands, chain):
    """The canonical rules per table, read back from the command form, so the two forms cannot differ."""
    tables = {name: empty_table() for name in TABLES}
    for command in commands:
        binary, *arguments = command.split()
        raw = arguments[:2] == ['-t', 'raw']
        arguments = arguments[2:] if raw else arguments
        if arguments[0] not in ('-A', '-I'):
            continue
        table = ('raw6' if raw else 'v6') if binary == 'ip6tables' else ('raw' if raw else 'filter')
        part = 'chain' if arguments[1] == chain else 'inputs' if arguments[1] == 'INPUT' else 'hooks'
        tables[table][part].append(canon(' '.join(['-A', *arguments[1:]])))
    return tables


def bridge_of(rule):
    tokens = fields(rule)
    return tokens[tokens.index('-i') + 1] if '-i' in tokens else rule


def covers(tokens, bridge):
    """Whether a rule's interface condition can match traffic from `bridge` (no -i, a wildcard, or a negation)."""
    index = interface_index(tokens)
    if index is None:
        return True
    name = tokens[index + 1]
    hit = name == bridge or (name.endswith('+') and bridge.startswith(name[:-1]))
    return not hit if index > 0 and tokens[index - 1] == '!' else hit


def negated_interface(tokens):
    """Whether the rule's one interface condition is a plain `! -i <name>` (an ambiguous rule is not: it stays a managed rule)."""
    index = interface_index(tokens)
    return index is not None and index > 0 and tokens[index - 1] == '!'


def plain_jump(rule):
    """A rule whose only condition is an interface: any other match (protocol, address) limits what it carries."""
    tokens = fields(rule)[2:]
    while tokens:
        if tokens[0] == '!' or tokens[0] in ('-i', '-j'):
            tokens = tokens[1:] if tokens[0] == '!' else tokens[2:]
        else:
            return False
    return True


def bypasses(rule, bridge, chains, top=True, seen=()):
    """Whether a rule can accept or return `bridge`'s traffic out of its chain (following jumps and gotos into user chains).

    Conditions other than the interface are ignored, so this errs towards "yes": an unclear flow is rejected, not trusted.
    A RETURN ends a user chain's walk quietly (back to the caller) and only leaves the base chain from the base chain itself
    or through a goto; a goto into a chain that does not end in an unconditional DROP also leaves it by falling off the
    chain's end (an empty chain, or one that only logs, included).
    """
    tokens = fields(rule)
    flag = '-g' if '-g' in tokens else '-j'
    if flag not in tokens or not covers(tokens, bridge):
        return False
    if tokens.count('-j') + tokens.count('-g') != 1:
        return True  # which one is the target is unclear: assume it can accept
    target = tokens[tokens.index(flag) + 1]
    if target == 'ACCEPT':
        return True
    if target == 'RETURN':
        return top
    if flag == '-g' and top and target not in seen and target not in ('DROP', 'REJECT'):
        inner = chains.get(target, [])
        if not inner or fields(inner[-1])[2:] != ['-j', 'DROP']:
            return True
    if target in chains and target not in seen:
        return any(bypasses(inner, bridge, chains, top and flag == '-g', (*seen, target)) for inner in chains[target])
    return False


def extract(text, table, chain, bridges, family='v4', kernel=False):
    """The rules of one table of an `iptables-save`/ufw rules text that belong to one block.

    Besides the block's own rules it records `shadowed` (a rule before a managed hook, or before the INPUT path to a
    managed drop, that accepts or returns that bridge's traffic directly or through jumps, so the hook never sees it) and
    `late` (a managed chain declared twice or after a rule that uses it: restoring the declaration empties the chain).
    Host-bound drops count in INPUT, or in ufw's own chain of this family; in the kernel the latter only when a rule of
    INPUT that covers the bridge jumps to it.
    """
    found = {**empty_table(), 'declared': set(), 'late': [], 'shadowed': []}
    chains, managed, inside = {}, [], False
    ufw_input = UFW_INPUT[family]
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('*'):
            inside = line == f'*{table}'
        elif not inside:
            continue
        elif line.startswith(':'):
            name = line.split()[0][1:]
            if name in (chain, 'DOCKER-USER') and (name in found['declared'] or name in chains):
                found['late'].append(name)
            found['declared'].add(name)
        elif line.startswith('-A '):
            rule = canon(line)
            tokens = fields(rule)
            chains.setdefault(tokens[1], []).append(rule)
            if tokens[1] == chain:
                found['chain'].append(rule)
            elif '-i' in tokens and not negated_interface(tokens) and bridge_of(rule) in bridges:
                # `! -i <bridge>` (Docker's own `-d <container> ! -i <bridge> -j DROP` in raw PREROUTING) never matches the
                # bridge's traffic: it is neither a hook nor an allowance, and the walk over earlier rules ignores it too.
                drop = tokens[1] in ('INPUT', ufw_input) and tokens[-2:] == ['-j', 'DROP']
                if drop or tokens[1] in ('DOCKER-USER', 'PREROUTING'):
                    managed.append((tokens[1], len(chains[tokens[1]]) - 1, rule))
    for name, index, rule in managed:
        bridge = bridge_of(rule)
        path = [(name, index)]
        entry, part = rule, 'hooks'
        if name in ('INPUT', ufw_input):
            entry, part = join(['-A', 'INPUT', *words(rule)[2:]]), 'inputs'
            if name == ufw_input and kernel:
                jump = next((i for i, other in enumerate(chains.get('INPUT', []))
                             if fields(other)[-2:] == ['-j', ufw_input] and plain_jump(other) and covers(fields(other), bridge)), None)
                if jump is None:
                    continue
                path.insert(0, ('INPUT', jump))
        for where, upto in path:
            hit = next((other for other in chains[where][:upto] if bypasses(other, bridge, chains)), None)
            if hit:
                found['shadowed'].append(hit)
                break
        found[part].append(entry)
    return found


def extract_hook(text, chain):
    """The raw-table rules a generated after.init holds: the restore payload and the `ensure` hook lines."""
    found = {'raw': empty_table(), 'raw6': empty_table()}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(f'-A {chain} '):
            found['raw']['chain'].append(canon(line))
        elif match := re.match(r'ensure (ip6?tables) (PREROUTING .*?) \|\| fail ', line):
            found['raw6' if match.group(1) == 'ip6tables' else 'raw']['hooks'].append(canon(f'-A {match.group(2)}'))
    return found


def compare(expected, actual, label, address=None):
    """The first difference of one table (expected is the authority) as a code, or None."""
    def blank(table):
        return not (table['chain'] or table['hooks'] or table['inputs'])
    if blank(expected) != blank(actual):
        return f'table_differs:{label}'
    want, have = collections.Counter(expected['chain']), collections.Counter(actual['chain'])
    missing, extra = list((want - have).elements()), list((have - want).elements())
    if address is not None and any(address.search(rule) for rule in missing) and any(address.search(rule) for rule in extra):
        return 'mini_differs'
    if missing:
        return f'missing:{missing[0]}'
    if extra:
        return f'extra:{extra[0]}'
    if expected['chain'] != actual['chain']:
        return f'table_differs:{label}'
    if actual.get('shadowed'):
        return f"extra:{actual['shadowed'][0]}"
    for part in ('hooks', 'inputs'):
        want, have = collections.Counter(expected[part]), collections.Counter(actual[part])
        for rule in (want - have).elements():
            return f'hook_absent:{bridge_of(rule)}'
        for rule in (have - want).elements():
            return f'extra:{rule}'
    return None


def first_diff(expected, actual, address=None):
    for table in TABLES:
        code = compare(expected[table], actual[table], 'raw' if table == 'raw6' else table, address)
        if code:
            return code
    return None


def declaration_diff(expected, found, chain):
    """A rules file that uses DOCKER-USER or the chain without declaring it would stop ufw from loading."""
    for table in ('filter', 'v6'):
        if found[table]['late']:
            return f'table_differs:{table}'
        needs = [('DOCKER-USER', any(rule.startswith('-A DOCKER-USER ') for rule in expected[table]['hooks']))]
        needs.append((chain, table == 'filter' and bool(expected[table]['chain'])))
        for declared, needed in needs:
            if needed and declared not in found[table]['declared']:
                return f'missing::{declared}'
    return None


def ufw_block(tables, label, chain, family, declares_docker_user):
    """The lines for one rules file, from the canonical rules (hooks and host-bound drops in ufw's own chains)."""
    part = tables['filter'] if family == 'v4' else tables['v6']
    input_chain = 'ufw-before-input' if family == 'v4' else 'ufw6-before-input'
    lines = [f'# {label} begin']
    if declares_docker_user:
        lines.append(':DOCKER-USER - [0:0]')
    if family == 'v4' and part['chain']:
        lines.append(f':{chain} - [0:0]')
    lines += part['chain'] + part['hooks'] + [rule.replace('-A INPUT ', f'-A {input_chain} ', 1) for rule in part['inputs']]
    return '\n'.join([*lines, f'# {label} end']) + '\n'


def merge_block(text, block, label, anchor, declares_docker_user, anchor_end=None):
    """The rules file with the block inserted (a block of the same label is replaced), or exit 2.

    The block goes after `anchor` (the last matching line of the filter table: the delivery block, which declares
    DOCKER-USER; a second declaration after a hook would flush the hook), or after ufw's `# End required lines` when it
    declares DOCKER-USER itself. When the anchor block has an end marker (`anchor_end`), the new block goes after that line, never
    inside the anchor block (replacing the anchor block later would delete it).
    """
    lines = text.splitlines()
    begin, end = f'# {label} begin', f'# {label} end'
    if begin in lines:
        first = lines.index(begin)
        del lines[first:lines.index(end, first) + 1]
    if '*filter' not in lines or 'COMMIT' not in lines[lines.index('*filter'):]:
        die('the rules file has no *filter table closed by COMMIT')
    start = lines.index('*filter')
    stop = lines.index('COMMIT', start)
    body = range(start + 1, stop)
    if anchor:
        hits = [i for i in body if re.search(anchor, lines[i])]
        if not hits:
            die(f'no delivery block found in the filter table (nothing matches {anchor}); not inserting')
        at = hits[-1] + 1
        ends = [i for i in body if anchor_end and lines[i] == anchor_end]
        if ends:
            at = max(at, ends[-1] + 1)
    else:
        marks = [i for i in body if lines[i].strip() == '# End required lines']
        at = (marks[0] if marks else max((i for i in body if lines[i].startswith(':')), default=start)) + 1
    if not declares_docker_user and not any(line.startswith(':DOCKER-USER ') for line in lines[start:at]):
        die('DOCKER-USER is not declared before the insertion point of the filter table; not inserting')
    lines[at:at] = block.splitlines()
    return '\n'.join(lines) + '\n'


def env_value(path, key):
    try:
        for line in Path(path).read_text().splitlines():
            name, _, value = line.strip().partition('=')
            if name == key and value:
                return value.strip().strip('\'"')
    except OSError as error:
        die(f'cannot read {path}: {error.strerror}')
    die(f'{key} is not set in {path}')


def read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ''


def kernel_text(command, table):
    try:
        result = subprocess.run([*shlex.split(command), '-t', table], text=True, capture_output=True, timeout=LIMIT)
    except (OSError, subprocess.SubprocessError) as error:
        die(f'cannot read the kernel rules ({command} -t {table}): {error}')
    if result.returncode != 0:
        die(f'cannot read the kernel rules ({command} -t {table}): exit {result.returncode}')
    return result.stdout


def receipt_state(path, boot_id):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return 'receipt unknown'
    if not isinstance(data, dict) or data.get('boot_id') != boot_id:
        return 'receipt unknown'
    return 'receipt ok' if data.get('ok') is True else 'receipt not_ok'


def verify_cli(argv, spec_for, key, description, address=None):
    """`verify`: repository vs persisted files vs kernel. Prints `ok` or `<pair> <code>` lines; exit 1 on any."""
    parser = argparse.ArgumentParser(prog='verify', description=description)
    parser.add_argument('--env-file', default=ENV_FILE)
    parser.add_argument('--rules-v4', default='/etc/ufw/before.rules')
    parser.add_argument('--rules-v6', default='/etc/ufw/before6.rules')
    parser.add_argument('--after-init', default='/etc/ufw/after.init')
    parser.add_argument('--iptables-save', default='iptables-save')
    parser.add_argument('--ip6tables-save', default='ip6tables-save')
    parser.add_argument('--persisted-only', action='store_true', help='compare the files with the repository, read no kernel')
    parser.add_argument('--receipt-check', action='store_true', help='also read the boot receipt (unknown is a failure)')
    parser.add_argument('--receipt', default=RECEIPT)
    parser.add_argument('--boot-id', default=None)
    args = parser.parse_args(argv)
    try:
        spec = spec_for(env_value(args.env_file, key))
    except (ValueError, argparse.ArgumentTypeError) as error:
        die(f'{key}: {error}')
    expected, chain, bridges = spec['tables'], spec['chain'], spec['bridges']
    v4, v6 = read(args.rules_v4), read(args.rules_v6)
    found = {'filter': extract(v4, 'filter', chain, bridges, 'v4'), 'v6': extract(v6, 'filter', chain, bridges, 'v6')}
    hook = read(args.after_init)
    # ufw runs the hook only when it is executable; a hook that is not byte for byte the generated one differs even when
    # its rules are the same (it could stop early).
    runs = spec['after_init'] is not None and os.access(args.after_init, os.X_OK)
    hooked = extract_hook(hook, chain) if runs else {'raw': empty_table(), 'raw6': empty_table()}
    persisted = {'filter': found['filter'], 'v6': found['v6'], **hooked}
    lines = []
    code = declaration_diff(expected, found, chain) or first_diff(expected, persisted, address)
    if not code and spec['after_init'] is not None and hook != spec['after_init']:
        code = 'table_differs:raw'
    if code:
        lines.append(f'file_vs_repo {code}')
    if not args.persisted_only:
        wants_raw = spec['after_init'] is not None
        def read_kernel(command, table, family):
            return extract(kernel_text(command, table), table, chain, bridges, family, kernel=True)
        kernel = {'filter': read_kernel(args.iptables_save, 'filter', 'v4'), 'v6': read_kernel(args.ip6tables_save, 'filter', 'v6'),
                  'raw': read_kernel(args.iptables_save, 'raw', 'v4') if wants_raw else empty_table(),
                  'raw6': read_kernel(args.ip6tables_save, 'raw', 'v6') if wants_raw else empty_table()}
        for pair, left, right in (('kernel_vs_repo', expected, kernel), ('file_vs_kernel', persisted, kernel)):
            code = first_diff(left, right, address)
            if code:
                lines.append(f'{pair} {code}')
    failed = bool(lines)
    if not lines:
        lines.append('ok')
    if args.receipt_check:
        state = receipt_state(args.receipt, args.boot_id or read(BOOT_ID).strip())
        lines.append(state)
        failed = failed or state != 'receipt ok'
    print('\n'.join(lines))
    return 1 if failed else 0


def rules_before_first_container(ufw, raw, raw_ok, container):
    """yes: the ufw load and the raw step both completed before the first container started (one monotonic clock)."""
    if ufw is None or raw is None or container is None or not raw_ok:
        return 'unknown'
    return 'yes' if max(ufw, raw) < container else 'no'


def command_output(command, *arguments):
    try:
        result = subprocess.run([*shlex.split(command), *arguments], text=True, capture_output=True, timeout=LIMIT)
    except (OSError, subprocess.SubprocessError):
        return ''
    return result.stdout if result.returncode == 0 else ''


def unit_time(systemctl, unit, name):
    value = command_output(systemctl, 'show', unit, '-p', name, '--value').strip()
    return int(value) / 1_000_000 if value.isdigit() and int(value) > 0 else None


def process_start(pid):
    """Seconds since boot at which a process started (/proc/PID/stat field 22), or None."""
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()
        return int(fields[19]) / os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, IndexError):
        return None


def first_container_start(docker, proc_start=process_start):
    """The earliest start of a running container's main process this boot, on the boot clock, or None (= unknown).

    The container start times Docker prints are wall-clock and a clock step would move them, so the start of the
    process itself is used. Unknown (None) when any container cannot be read, and when a container that is not running
    ever ran (it may have run before the rules, and without a reliable boot-wide clock mapping its start cannot be placed
    on this boot's clock). It is the earliest *observable* start: a container that restarted shows its latest one.
    """
    ids = command_output(docker, 'ps', '-aq').split()
    if not ids:
        return None
    lines = command_output(docker, 'inspect', '-f', '{{.State.Running}} {{.State.Pid}} {{.State.StartedAt}}', *ids).splitlines()
    if len(lines) != len(ids):
        return None
    starts = []
    for line in lines:
        running, _, rest = line.partition(' ')
        pid, _, stamp = rest.partition(' ')
        if running == 'true' and pid.isdigit():
            mono = proc_start(int(pid))
            if mono is None:
                return None
            starts.append(mono)
            continue
        try:
            when = datetime.datetime.fromisoformat(re.sub(r'(\.\d{6})\d*', r'\1', stamp.replace('Z', '+00:00')))
        except ValueError:
            return None
        if when.year > 1970:
            return None
    return min(starts) if starts else None


def receipt_cli(argv):
    parser = argparse.ArgumentParser(prog='receipt', description='run both verify commands and write the boot receipt')
    parser.add_argument('--out', default=RECEIPT)
    parser.add_argument('--boot-id', default=None)
    parser.add_argument('--raw-state', default=RAW_STATE)
    parser.add_argument('--systemctl', default='systemctl')
    parser.add_argument('--docker', default='docker')
    parser.add_argument('--verify', action='append', default=[], metavar='NAME=COMMAND',
                        help='replace the verify command of inference or delivery (tests)')
    args = parser.parse_args(argv)
    boot = args.boot_id or read(BOOT_ID).strip()
    custom = dict(item.partition('=')[::2] for item in args.verify)
    here = Path(__file__).resolve().parent
    results = {}
    for name in ('inference', 'delivery'):
        command = shlex.split(custom[name]) if name in custom else [sys.executable, str(here / f'{name}_egress_check.py'), 'verify']
        try:
            done = subprocess.run(command, text=True, capture_output=True, timeout=LIMIT)
            first = (done.stdout.strip().splitlines() or [f'error:exit {done.returncode}'])[0]
            results[name] = first if done.returncode == 0 or first != 'ok' else f'error:exit {done.returncode}'
        except (OSError, subprocess.SubprocessError) as error:
            results[name] = f'error:{type(error).__name__}'
    try:
        state = json.loads(read(args.raw_state))
    except ValueError:
        state = {}
    mine = isinstance(state, dict) and state.get('boot_id') == boot
    raw_time = state.get('uptime') if mine and isinstance(state.get('uptime'), (int, float)) else None
    raw_ok = bool(mine and state.get('ok') is True)
    ufw = unit_time(args.systemctl, 'ufw.service', 'ExecMainExitTimestampMonotonic')
    container = first_container_start(args.docker)
    receipt = {'boot_id': boot, 'inference': results['inference'], 'delivery': results['delivery'],
               'ok': all(value == 'ok' for value in results.values()),
               'ufw_rules_loaded_monotonic': ufw, 'raw_step_monotonic': raw_time, 'raw_step_ok': raw_ok,
               'docker_start_monotonic': unit_time(args.systemctl, 'docker.service', 'ExecMainStartTimestampMonotonic'),
               'first_container_monotonic': container,
               'rules_before_first_container': rules_before_first_container(ufw, raw_time, raw_ok, container)}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(out.name + '.tmp')
    temporary.write_text(json.dumps(receipt) + '\n')
    temporary.chmod(0o644)
    os.replace(temporary, out)
    print(json.dumps(receipt), flush=True)
    return 0 if receipt['ok'] else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['receipt']:
        return receipt_cli(argv[1:])
    print('usage: firewall_persist.py receipt [--out FILE]', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
