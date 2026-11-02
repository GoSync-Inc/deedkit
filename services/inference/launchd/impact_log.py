#!/usr/bin/env python3
"""Does an inference phase get in the way of Between or the remote tests? (ANYM-684, owner 11-02). Stdlib only.

    impact_log.py log --dir DIR [--busy-file F] [--interval 15] [--hours 0]
    impact_log.py summarize FILE [FILE ...] [--interval 15]

A LaunchDaemon of the admin account runs `log` (read-only: `ps`, `vm_stat`, `sysctl`, `busy.json`; it signals and
changes nothing). Every `interval` seconds it writes one line to `DIR/impact-YYYY-MM-DD.log`:

    ts=<epoch> between=<t|f|?> ci=<t|f|?> phase=<0|1> inf_cpu=<cores> inf_rss_mb=<mb> vm_cores=<cores> ci_cores=<cores>
    stopped_mb=<mb of the service user's stopped processes> load=<1-min> avail_gb=<free+inactive+speculative> swap_mb=<used>

`phase=1` means the inference user burned at least PHASE_CORES of CPU over the interval (an idle service and a warm,
idle engine worker burn almost none, so a resident model alone is not a phase). `vm_cores` is the CPU of Between's
colima VMs (the `_betweenci` Virtualization processes), `ci_cores` that of the remote-test user. A process is
identified by its pid and its start time, so a pid that is reused inside an interval counts from zero. When a day
ends it writes `DIR/summary-YYYY-MM-DD.json` (atomically; a day that has a log and no summary is retried until it
has one, also after a restart): minutes of overlap (a phase while `between` or `ci` is true), and load, available
memory, swap and the VM cores in five situations: contended with a phase running (overlap), contended with a phase
stopped (paused: it holds its RAM and burns no CPU), contended without any (Between alone), a phase alone, and idle. A sample whose busy flags are unknown (`?`) belongs to none of the four. The comparison
"overlap against Between alone" is the answer to whether the phase interferes.
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
GAP_FACTOR = 1.5     # two samples further apart than this many intervals mean at least one sample is missing (jitter is below 1)
PS = ['/bin/ps', '-axo', 'uid=,pid=,stat=,lstart=,cputime=,rss=,comm=']
CLASSES = ('overlap', 'paused', 'contended_alone', 'phase_alone', 'idle')
COUNTERS = ('free', 'inactive', 'speculative')
ADMIT_FLOOR_GB = 7.0  # the service's admit_min_available_gb (ANYM-684): the summary estimates admission from the busy flags and this
RETRY_EVERY = 40     # loop passes between two retries of a summary that could not be written


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


def read_processes(run_ps=None) -> dict[int, tuple[int, float, int, str, str, str]]:
    """pid -> (uid, cpu seconds, rss KB, command, start time text, state) for every process; ValueError on a line it
    cannot read. The start time (`lstart`, five words) is what tells a reused pid from the process it replaced."""
    text = run_ps() if run_ps else subprocess.run(PS, capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=True,
                                                  stdin=subprocess.DEVNULL).stdout
    table = {}
    for line in text.splitlines():
        fields = line.split(None, 10)
        if len(fields) < 11:
            if line.strip():
                raise ValueError('unreadable ps line')
            continue
        table[int(fields[1])] = (int(fields[0]), parse_cputime(fields[8]), int(fields[9]), fields[10], ' '.join(fields[3:8]), fields[2])
    return table


def cores(before: dict, after: dict, uid: int, seconds: float, mark: str | None = None) -> tuple[float, int, int]:
    """CPU cores used between two process tables by the processes of `uid` (optionally only those whose command ends
    with `mark`), the number of such processes now and their RSS in KB. A process that started in the interval, or whose
    pid was reused by another process, counts from zero; one that ended in it is not seen (the figure errs low)."""
    used, count, rss = 0.0, 0, 0
    for pid, (owner, cpu, kb, command, started, _state) in after.items():
        if owner != uid or (mark is not None and not command.endswith(mark)):
            continue
        count, rss = count + 1, rss + kb
        previous = before.get(pid)
        if previous is not None and previous[0] == owner and previous[4] == started and cpu >= previous[1]:
            used += cpu - previous[1]
        else:
            used += cpu
    return (used / seconds if seconds > 0 else 0.0), count, rss


def stopped_kb(table: dict, uid: int) -> int:
    """RSS in KB of the processes of `uid` that are stopped (state T): memory held by a paused phase."""
    return sum(kb for owner, _cpu, kb, _command, _started, state in table.values() if owner == uid and state.startswith('T'))


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
        if (isinstance(ts, bool) or not isinstance(ts, (int, float)) or not isinstance(between, bool)
                or not isinstance(ci, bool) or not math.isfinite(ts) or not abs(now - ts) <= STALE_SECONDS):
            return '?', '?'
    except (ValueError, KeyError, TypeError, OverflowError):
        return '?', '?'
    return ('t' if between else 'f'), ('t' if ci else 'f')


def free_gb(text: str) -> float:
    """Free + inactive + speculative memory in GB; ValueError unless the page size and all three counters are there."""
    size = re.search(r'page size of (\d+)', text)
    pages = [re.search(rf'^Pages {name}:\s+(\d+)', text, re.MULTILINE) for name in COUNTERS]
    if size is None or not all(pages):
        raise ValueError('vm_stat')
    return sum(int(match.group(1)) for match in pages if match) * int(size.group(1)) / 1024 ** 3


def swap_mb(text: str) -> float:
    found = re.search(r'used = ([\d.]+)([MG])', text)
    if not found:
        raise ValueError('swapusage')
    return float(found.group(1)) * (1024 if found.group(2) == 'G' else 1)


def sample_line(now: float, flags: tuple[str, str], figures: dict) -> str:
    phase = 1 if figures['inf_cpu'] >= PHASE_CORES else 0
    return (f'ts={int(now)} between={flags[0]} ci={flags[1]} phase={phase} inf_cpu={figures["inf_cpu"]:.2f} '
            f'inf_rss_mb={figures["inf_rss_kb"] / 1024:.0f} vm_cores={figures["vm_cores"]:.2f} '
            f'ci_cores={figures["ci_cores"]:.2f} stopped_mb={figures["stopped_kb"] / 1024:.0f} load={figures["load"]:.2f} avail_gb={figures["avail_gb"]:.1f} '
            f'swap_mb={figures["swap_mb"]:.0f}')


def parse_line(line: str) -> dict | None:
    fields = dict(token.split('=', 1) for token in line.split() if '=' in token)
    try:
        return {'ts': int(fields['ts']), 'between': fields['between'], 'ci': fields['ci'], 'phase': fields['phase'] == '1',
                'stopped_mb': float(fields.get('stopped_mb', 0.0)),
                **{key: float(fields[key]) for key in ('inf_cpu', 'inf_rss_mb', 'vm_cores', 'ci_cores', 'load', 'avail_gb', 'swap_mb')}}
    except (KeyError, ValueError):
        return None


def percentile(values: list[float], q: float) -> float:
    """Nearest rank: the smallest value with at least q of the values at or below it."""
    ordered = sorted(values)
    return ordered[min(len(ordered), max(1, math.ceil(q * len(ordered)))) - 1] if ordered else 0.0


def mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def classify(row: dict) -> str | None:
    """The situation of one sample; None when the busy flags are unknown and neither says contended."""
    if row['between'] == 't' or row['ci'] == 't':
        if row['phase']:
            return 'overlap'
        return 'paused' if row['stopped_mb'] > 0 else 'contended_alone'  # a stopped phase still holds its RAM
    if '?' in (row['between'], row['ci']):
        return None
    return 'phase_alone' if row['phase'] else 'idle'


def summarize(lines: list[str], interval: int = INTERVAL) -> dict:
    rows = sorted((row for row in map(parse_line, lines) if row), key=lambda row: row['ts'])
    if not rows:
        return {'samples': 0}
    groups: dict[str, list[dict]] = {name: [] for name in CLASSES}
    unknown = gaps = 0
    gap_seconds = 0
    longest = run = episodes = 0
    by_who: Counter[str] = Counter()
    previous = None
    for row in rows:
        broke = previous is not None and row['ts'] - previous > interval * GAP_FACTOR
        if broke:
            gaps += 1
            gap_seconds += row['ts'] - previous - interval
        previous = row['ts']
        name = classify(row)
        if name is None:
            unknown += 1
        else:
            groups[name].append(row)
        if name == 'overlap':
            run = 1 if broke or run == 0 else run + 1
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
                'vm_cores_p90': percentile([r['vm_cores'] for r in group], 0.9),
                'ci_cores_mean': mean([r['ci_cores'] for r in group]), 'inf_cpu_mean': mean([r['inf_cpu'] for r in group]),
                'inf_rss_mb_max': max((r['inf_rss_mb'] for r in group), default=0.0),
                'stopped_mb_max': max((r['stopped_mb'] for r in group), default=0.0),
                'stopped_samples': sum(1 for r in group if r['stopped_mb'] > 0)}

    # Admission as the service would have decided from what the log holds: neither busy flag true (unknown is not open) and the free
    # memory at the floor. The per-phase memory rule and the load cap are not in the log, so this is an upper bound of the share.
    open_rows = [r for r in rows if r['between'] == 'f' and r['ci'] == 'f']
    admitted = [r for r in open_rows if r['avail_gb'] >= ADMIT_FLOOR_GB]
    result = {'samples': len(rows), 'minutes': round(len(rows) * interval / 60, 1), 'unknown_busy_samples': unknown,
              'gaps': gaps, 'gap_minutes': round(gap_seconds / 60, 1),
              'busy_open_percent': round(100 * len(open_rows) / len(rows), 1), 'admitted_percent': round(100 * len(admitted) / len(rows), 1),
              'overlap_minutes': round(len(groups['overlap']) * interval / 60, 1), 'overlap_episodes': episodes,
              'longest_overlap_minutes': round(longest * interval / 60, 1), 'overlap_while': dict(by_who),
              'avail_gb_min': min(r['avail_gb'] for r in rows),
              'swap_mb_first': rows[0]['swap_mb'], 'swap_mb_last': rows[-1]['swap_mb'], 'swap_mb_max': max(r['swap_mb'] for r in rows),
              **{name: describe(group) for name, group in groups.items()}}
    overlap = groups['overlap']
    result['headline'] = (f"admitted {result['admitted_percent']:.1f} % | overlap {result['overlap_minutes']:.1f} min | avail_gb min "
                          + (f"{result['overlap']['avail_gb_min']:.1f} in overlap" if overlap else "n/a (no overlap)")
                          + f", {result['avail_gb_min']:.1f} all day | Between VM p90 "
                          + (f"{result['overlap']['vm_cores_p90']:.2f} cores in overlap" if overlap else "n/a (no overlap)"))
    return result


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
    stopped = stopped_kb(table, inf_uid) if inf_uid is not None else 0
    figures = {'inf_cpu': inf_cpu, 'inf_rss_kb': inf_rss, 'stopped_kb': stopped, 'vm_cores': vm_cores, 'ci_cores': ci_cores,
               'load': os.getloadavg()[0], 'avail_gb': free_gb(run(['/usr/bin/vm_stat'])),
               'swap_mb': swap_mb(run(['/usr/sbin/sysctl', '-n', 'vm.swapusage']))}
    if not all(math.isfinite(value) for value in figures.values()):
        raise ValueError('non-finite figure')
    return sample_line(now, busy_flags(text, now), figures)


def write_summary(directory: Path, day: str, interval: int) -> None:
    """summary-DAY.json from impact-DAY.log, atomically: a failure leaves no half-written summary."""
    source = directory / f'impact-{day}.log'
    if not source.exists():
        return
    target = directory / f'summary-{day}.json'
    partial = directory / f'.summary-{day}.json.part'
    partial.write_text(json.dumps(summarize(source.read_text().splitlines(), interval), indent=2) + '\n')
    os.replace(partial, target)


def pending_days(directory: Path, today: str) -> list[str]:
    """Days before `today` that have a log and no summary yet."""
    days = [path.name[len('impact-'):-len('.log')] for path in directory.glob('impact-????-??-??.log')]
    return sorted(day for day in days if day < today and not (directory / f'summary-{day}.json').exists())


def catch_up(directory: Path, today: str, interval: int) -> bool:
    """Write every missing summary of a finished day; True when none is left. A failure is kept for the next try."""
    clean = True
    try:
        days = pending_days(directory, today)
    except OSError:
        return False
    for day in days:
        try:
            write_summary(directory, day, interval)  # one bad day never blocks the newer ones
        except (OSError, ValueError):
            clean = False
    return clean


def append_line(directory: Path, day: str, line: str) -> None:
    with open(directory / f'impact-{day}.log', 'a') as out:
        out.write(line + '\n')


def run_log(directory: Path, busy_file: str, interval: int, hours: float, *, clock=time.time, sleep=time.sleep) -> int:
    """The loop. Nothing it does after the directory exists can end it: a failed sample, a full disk or a failed summary
    is skipped and tried again (the gap shows in `ts`; a missing summary is retried until it exists)."""
    directory.mkdir(parents=True, exist_ok=True)
    deadline = clock() + hours * 3600 if hours else math.inf
    state: dict = {}
    current, passes, settled = day_of(clock()), 0, False
    while clock() < deadline:
        started = clock()
        today = day_of(started)
        passes += 1
        if today != current or passes % RETRY_EVERY == 1 or not settled:
            settled = catch_up(directory, today, interval)
            current = today
        try:
            line = collect(state, started, busy_file)
            append_line(directory, today, line)
        except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
            pass  # a failed or first sample, or a failed append: skipped, the gap is visible in ts
        sleep(max(0.0, interval - (clock() - started)))
    catch_up(directory, day_of(clock() + 86400), interval)
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
        summary = summarize(lines, args.interval)
        print(json.dumps(summary, indent=2))
        if 'headline' in summary:
            print(summary['headline'])
        return 0
    return run_log(args.dir, args.busy_file, args.interval, args.hours)


if __name__ == '__main__':
    sys.exit(main())
