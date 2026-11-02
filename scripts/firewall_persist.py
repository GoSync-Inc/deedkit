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
import time
from pathlib import Path

# v4 filter, v4 raw, v6 filter, v6 raw. The raw pair is reported as `raw`: it is loaded by one step.
TABLES = ('filter', 'raw', 'v6', 'raw6')
INPUT_CHAINS = ('INPUT', 'ufw-before-input', 'ufw6-before-input')
ENV_FILE = '/opt/personal-os-probe/.env'
BOOT_ID = '/proc/sys/kernel/random/boot_id'
RECEIPT = '/var/lib/deedkit/firewall-verify.json'
RAW_STATE = '/var/lib/deedkit/raw-step.json'
LIMIT = 30


def die(message):
    print(message, file=sys.stderr)
    raise SystemExit(2)


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
    if len(out) > 1 and out[1] in INPUT_CHAINS:
        out[1] = 'INPUT'
    return ' '.join(out)


def empty_table():
    return {'chain': [], 'hooks': [], 'inputs': []}


def spec_from_commands(commands, chain):
    """The canonical rules per table, read back from the command form, so the two forms cannot differ."""
    tables = {name: empty_table() for name in TABLES}
    for command in commands:
        binary, *words = command.split()
        raw = words[:2] == ['-t', 'raw']
        words = words[2:] if raw else words
        if words[0] not in ('-A', '-I'):
            continue
        table = ('raw6' if raw else 'v6') if binary == 'ip6tables' else ('raw' if raw else 'filter')
        part = 'chain' if words[1] == chain else 'inputs' if words[1] == 'INPUT' else 'hooks'
        tables[table][part].append(canon(' '.join(['-A', *words[1:]])))
    return tables


def bridge_of(rule):
    tokens = rule.split()
    return tokens[tokens.index('-i') + 1] if '-i' in tokens else rule


def extract(text, table, chain, bridges):
    """The rules of one table of an `iptables-save`/ufw rules text that belong to one block."""
    found = {**empty_table(), 'declared': set()}
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('*'):
            inside = line == f'*{table}'
        elif not inside:
            continue
        elif line.startswith(':'):
            found['declared'].add(line.split()[0][1:])
        elif line.startswith('-A '):
            rule = canon(line)
            tokens = rule.split()
            if tokens[1] == chain:
                found['chain'].append(rule)
            elif '-i' in tokens and bridge_of(rule) in bridges:
                if tokens[1] == 'INPUT':
                    found['inputs'].append(rule)
                elif tokens[1] in ('DOCKER-USER', 'PREROUTING'):
                    found['hooks'].append(rule)
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


def merge_block(text, block, label, anchor, declares_docker_user):
    """The rules file with the block inserted (a block of the same label is replaced), or exit 2.

    The block goes after `anchor` (the last matching line of the filter table: the delivery block, which declares
    DOCKER-USER; a second declaration after a hook would flush the hook), or after ufw's `# End required lines` when it
    declares DOCKER-USER itself.
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
    found = {'filter': extract(v4, 'filter', chain, bridges), 'v6': extract(v6, 'filter', chain, bridges)}
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
        kernel = {'filter': extract(kernel_text(args.iptables_save, 'filter'), 'filter', chain, bridges),
                  'v6': extract(kernel_text(args.ip6tables_save, 'filter'), 'filter', chain, bridges),
                  'raw': extract(kernel_text(args.iptables_save, 'raw'), 'raw', chain, bridges) if wants_raw else empty_table(),
                  'raw6': extract(kernel_text(args.ip6tables_save, 'raw'), 'raw', chain, bridges) if wants_raw else empty_table()}
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


def first_container_start(docker):
    """The earliest container start of this boot on the monotonic clock, from the wall-clock StartedAt values."""
    ids = command_output(docker, 'ps', '-aq').split()
    if not ids:
        return None
    now_wall, now_mono, starts = time.time(), time.monotonic(), []
    for stamp in command_output(docker, 'inspect', '-f', '{{.State.StartedAt}}', *ids).split():
        try:
            when = datetime.datetime.fromisoformat(re.sub(r'(\.\d{6})\d*', r'\1', stamp.replace('Z', '+00:00')))
        except ValueError:
            continue
        mono = now_mono - (now_wall - when.timestamp())
        if when.year > 1970 and mono >= 0:
            starts.append(mono)
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
