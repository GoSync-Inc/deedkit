#!/usr/bin/env python3
"""Does an inference phase get in the way of Between or the remote tests? (ANYM-684, owner 11-02). Stdlib only.

    impact_log.py log --dir DIR [--busy-file F] [--interval 15] [--hours 0]
    impact_log.py summarize FILE [FILE ...] [--interval 15]

A LaunchDaemon of the admin account runs `log` (read-only: `ps`, `vm_stat`, `sysctl`, `busy.json`; it signals and
changes nothing). Every `interval` seconds it writes one line to `DIR/impact-YYYY-MM-DD.log`:

    ts=<epoch> between=<t|f|?> ci=<t|f|?> phase=<0|1> inf_cpu=<cores> inf_rss_mb=<mb> vm_cores=<cores> ci_cores=<cores>
    load=<1-min> avail_gb=<free+inactive+speculative> swap_mb=<used>

`phase=1` means the inference user burned at least PHASE_CORES of CPU over the interval (an idle service and a warm,
idle engine worker burn almost none, so a resident model alone is not a phase). `vm_cores` is the CPU of Between's
colima VMs (the `_betweenci` Virtualization processes), `ci_cores` that of the remote-test user. When a day ends it
writes `DIR/summary-YYYY-MM-DD.json`: minutes of overlap (a phase while `between` or `ci` is true), and load, available
memory, swap and the VM cores in four situations: contended with a phase (overlap), contended without (Between alone),
a phase alone, and idle. The comparison "overlap against Between alone" is the answer to whether the phase interferes.
"""
from __future__ import annotations

import argparse
from collections import Counter
import datetime
import json
import math
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import time

BUSY_FILE = '/Users/Shared/deedkit-inference/busy.json'
SERVICE_USER = '_deedkitinf'
CI_USER = '_deedkitci'
BETWEEN_USER = '_betweenci'
VM_MARK = 'com.apple.Virtualization.VirtualMachine'
PHASE_CORES = 0.30   # CPU of the inference user, in cores, that counts as a running phase
STALE_SECONDS = 30
INTERVAL = 15
COMMAND_TIMEOUT = 10
PS = ['/bin/ps', '-axo', 'uid=,pid=,cputime=,rss=,comm=']
CLASSES = ('overlap', 'contended_alone', 'phase_alone', 'idle')


def parse_cputime(text: str) -> float:
    """macOS `ps` cputime: `S.cc`, `M:SS.cc`, `H:MM:SS.cc` or `D-HH:MM:SS`; seconds."""
    days = 0
    if '-' in text:
        head, text = text.split('-', 1)
        days = int(head)
    seconds = 0.0
    for part in text.split(':'):
        seconds = seconds * 60 + float(part)
    return days * 86400 + seconds


def read_processes(run_ps=None) -> dict[int, tuple[int, float, int, str]]:
    """pid -> (uid, cpu seconds, rss KB, command) for every process; ValueError on a line it cannot read."""
    text = run_ps() if run_ps else subprocess.run(PS, capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=True,
                                                  stdin=subprocess.DEVNULL).stdout
    table = {}
    for line in text.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 5:
            if line.strip():
                raise ValueError('unreadable ps line')
            continue
        table[int(fields[1])] = (int(fields[0]), parse_cputime(fields[2]), int(fields[3]), fields[4])
    return table


def cores(before: dict, after: dict, uid: int, seconds: float, mark: str | None = None) -> tuple[float, int, int]:
    """CPU cores used between two process tables by the processes of `uid` (optionally only those whose command ends
    with `mark`), the number of such processes now and their RSS in KB. A process that started in the interval counts
    from zero; one that ended in it is not seen (the figure errs low)."""
    used, count, rss = 0.0, 0, 0
    for pid, (owner, cpu, kb, command) in after.items():
        if owner != uid or (mark is not None and not command.endswith(mark)):
            continue
        count, rss = count + 1, rss + kb
        previous = before.get(pid)
        if previous is not None and previous[0] == owner and cpu >= previous[1]:
            used += cpu - previous[1]
        elif previous is None:
            used += cpu
    return (used / seconds if seconds > 0 else 0.0), count, rss


def uid_of(user: str) -> int | None:
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        return None


def busy_flags(text: str | None, now: float) -> tuple[str, str]:
    """('t'|'f'|'?', 't'|'f'|'?') for between and deedkit_ci; '?' when the file is missing, invalid or stale."""
    if text is None:
        return '?', '?'
    try:
        data = json.loads(text)
        ts, between, ci = data['ts'], data['between'], data['deedkit_ci']
    except (ValueError, KeyError, TypeError):
        return '?', '?'
    if (isinstance(ts, bool) or not isinstance(ts, (int, float)) or not isinstance(between, bool)
            or not isinstance(ci, bool) or abs(now - ts) > STALE_SECONDS):
        return '?', '?'
    return ('t' if between else 'f'), ('t' if ci else 'f')


def free_gb(text: str) -> float:
    size = int(re.search(r'page size of (\d+)', text).group(1))  # type: ignore[union-attr]  # vm_stat always prints it
    pages = sum(int(match) for match in re.findall(r'^Pages (?:free|inactive|speculative):\s+(\d+)', text, re.MULTILINE))
    return pages * size / 1024 ** 3


def swap_mb(text: str) -> float:
    found = re.search(r'used = ([\d.]+)([MG])', text)
    if not found:
        raise ValueError('swapusage')
    return float(found.group(1)) * (1024 if found.group(2) == 'G' else 1)


def sample_line(now: float, flags: tuple[str, str], figures: dict) -> str:
    phase = 1 if figures['inf_cpu'] >= PHASE_CORES else 0
    return (f'ts={int(now)} between={flags[0]} ci={flags[1]} phase={phase} inf_cpu={figures["inf_cpu"]:.2f} '
            f'inf_rss_mb={figures["inf_rss_kb"] / 1024:.0f} vm_cores={figures["vm_cores"]:.2f} '
            f'ci_cores={figures["ci_cores"]:.2f} load={figures["load"]:.2f} avail_gb={figures["avail_gb"]:.1f} '
            f'swap_mb={figures["swap_mb"]:.0f}')


def parse_line(line: str) -> dict | None:
    fields = dict(token.split('=', 1) for token in line.split() if '=' in token)
    try:
        return {'ts': int(fields['ts']), 'between': fields['between'], 'ci': fields['ci'], 'phase': fields['phase'] == '1',
                **{key: float(fields[key]) for key in ('inf_cpu', 'inf_rss_mb', 'vm_cores', 'ci_cores', 'load', 'avail_gb', 'swap_mb')}}
    except (KeyError, ValueError):
        return None


def mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def summarize(lines: list[str], interval: int = INTERVAL) -> dict:
    rows = [row for row in map(parse_line, lines) if row]
    if not rows:
        return {'samples': 0}
    groups: dict[str, list[dict]] = {name: [] for name in CLASSES}
    unknown = 0
    longest = run = episodes = 0
    by_who: Counter[str] = Counter()
    for row in rows:
        if '?' in (row['between'], row['ci']):
            unknown += 1
        contended = row['between'] == 't' or row['ci'] == 't'
        name = ('overlap' if row['phase'] else 'contended_alone') if contended else ('phase_alone' if row['phase'] else 'idle')
        groups[name].append(row)
        if name == 'overlap':
            run += 1
            if run == 1:
                episodes += 1
            longest = max(longest, run)
            by_who.update(who for who, flag in (('between', row['between']), ('ci', row['ci'])) if flag == 't')
        else:
            run = 0

    def describe(group: list[dict]) -> dict:
        return {'samples': len(group), 'minutes': round(len(group) * interval / 60, 1),
                'load_mean': mean([r['load'] for r in group]), 'load_max': max((r['load'] for r in group), default=0.0),
                'avail_gb_mean': mean([r['avail_gb'] for r in group]), 'avail_gb_min': min((r['avail_gb'] for r in group), default=0.0),
                'swap_mb_mean': mean([r['swap_mb'] for r in group]), 'vm_cores_mean': mean([r['vm_cores'] for r in group]),
                'ci_cores_mean': mean([r['ci_cores'] for r in group]), 'inf_cpu_mean': mean([r['inf_cpu'] for r in group]),
                'inf_rss_mb_max': max((r['inf_rss_mb'] for r in group), default=0.0)}

    return {'samples': len(rows), 'minutes': round(len(rows) * interval / 60, 1), 'unknown_busy_samples': unknown,
            'overlap_minutes': round(len(groups['overlap']) * interval / 60, 1), 'overlap_episodes': episodes,
            'longest_overlap_minutes': round(longest * interval / 60, 1), 'overlap_while': dict(by_who),
            **{name: describe(group) for name, group in groups.items()}}


def day_of(now: float) -> str:
    return datetime.date.fromtimestamp(now).isoformat()


def collect(state: dict, now: float, busy_file: str) -> str:
    """One sample line; `state` keeps the previous process table between calls."""
    table = read_processes()
    mono = time.monotonic()
    before, elapsed = state.get('table'), mono - state.get('mono', mono)
    state['table'], state['mono'] = table, mono
    if before is None or elapsed <= 0:
        raise ValueError('first sample: no interval yet')
    inf_uid, ci_uid, vm_uid = uid_of(SERVICE_USER), uid_of(CI_USER), uid_of(BETWEEN_USER)
    inf_cpu, _, inf_rss = cores(before, table, inf_uid, elapsed) if inf_uid is not None else (0.0, 0, 0)
    ci_cores = cores(before, table, ci_uid, elapsed)[0] if ci_uid is not None else 0.0
    vm_cores = cores(before, table, vm_uid, elapsed, VM_MARK)[0] if vm_uid is not None else 0.0
    try:
        text = Path(busy_file).read_text()
    except OSError:
        text = None
    run = lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=True,  # noqa: E731
                                      stdin=subprocess.DEVNULL).stdout
    figures = {'inf_cpu': inf_cpu, 'inf_rss_kb': inf_rss, 'vm_cores': vm_cores, 'ci_cores': ci_cores,
               'load': os.getloadavg()[0], 'avail_gb': free_gb(run(['/usr/bin/vm_stat'])),
               'swap_mb': swap_mb(run(['/usr/sbin/sysctl', '-n', 'vm.swapusage']))}
    if not all(math.isfinite(value) for value in figures.values()):
        raise ValueError('non-finite figure')
    return sample_line(now, busy_flags(text, now), figures)


def write_summary(directory: Path, day: str, interval: int) -> None:
    source = directory / f'impact-{day}.log'
    if source.exists():
        (directory / f'summary-{day}.json').write_text(json.dumps(summarize(source.read_text().splitlines(), interval), indent=2) + '\n')


def run_log(directory: Path, busy_file: str, interval: int, hours: float) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + hours * 3600 if hours else math.inf
    state: dict = {}
    current = day_of(time.time())
    while time.time() < deadline:
        started = time.time()
        today = day_of(started)
        if today != current:
            write_summary(directory, current, interval)
            current = today
        try:
            line = collect(state, started, busy_file)
        except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
            line = None  # a failed or first sample is skipped; the gap is visible in ts and never stops the logger
        if line:
            with open(directory / f'impact-{today}.log', 'a') as out:
                out.write(line + '\n')
        time.sleep(max(0.0, interval - (time.time() - started)))
    write_summary(directory, current, interval)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or '').split('\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    log = sub.add_parser('log')
    log.add_argument('--dir', required=True, type=Path)
    log.add_argument('--busy-file', default=BUSY_FILE)
    log.add_argument('--interval', type=int, default=INTERVAL)
    log.add_argument('--hours', type=float, default=0.0, help='0 = until stopped')
    summary = sub.add_parser('summarize')
    summary.add_argument('files', nargs='+', type=Path)
    summary.add_argument('--interval', type=int, default=INTERVAL)
    args = parser.parse_args(argv)
    if args.command == 'summarize':
        lines = [line for file in args.files for line in file.read_text().splitlines()]
        print(json.dumps(summarize(lines, args.interval), indent=2))
        return 0
    return run_log(args.dir, args.busy_file, args.interval, args.hours)


if __name__ == '__main__':
    sys.exit(main())
