#!/usr/bin/env python3
"""The 24-hour duty-cycle record (ANYM-684 criterion 10). Stdlib only; run by hand on the mini as the admin account.

    duty_log.py log --out duty.log [--busy-file F] [--max-load 8] [--need-gb 7.9] [--interval 60] [--hours 24]
    duty_log.py summarize duty.log [--interval 60]

`log` writes one line a minute: the busy flags, the 1-minute load, free plus inactive memory, and the verdict each
admission rule of the service (ANYM-682 README, Admission) would give. `summarize` prints the fraction of the time
admission would have been open, the longest closed stretch and which rule closed it most. The thresholds are
inputs: `--max-load` is `max_load_1m`, `--need-gb` is the largest phase's `resident_gb` plus `min_free_gb`.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

BUSY_FILE = '/Users/Shared/deedkit-inference/busy.json'
STALE_SECONDS = 30
REASONS = ('busy_between', 'busy_deedkit_ci', 'busy_file_missing', 'busy_file_invalid', 'busy_file_stale', 'load_high',
           'memory_low')


def busy_reasons(text: str | None, now: float) -> list[str]:
    """What the service's busy-file rule says: missing, unparseable, wrong types, older or newer than 30 s are busy."""
    if text is None:
        return ['busy_file_missing']
    try:
        data = json.loads(text)
        ts, between, ci = data['ts'], data['between'], data['deedkit_ci']
    except (ValueError, KeyError, TypeError):
        return ['busy_file_invalid']
    if (isinstance(ts, bool) or not isinstance(ts, (int, float)) or not isinstance(between, bool)
            or not isinstance(ci, bool)):
        return ['busy_file_invalid']
    if abs(now - ts) > STALE_SECONDS:
        return ['busy_file_stale']
    return [name for name, flag in (('busy_between', between), ('busy_deedkit_ci', ci)) if flag]


def verdict(text: str | None, now: float, load: float, free_gb: float, max_load: float, need_gb: float) -> list[str]:
    reasons = busy_reasons(text, now)
    if load > max_load:
        reasons.append('load_high')
    if free_gb < need_gb:
        reasons.append('memory_low')
    return reasons


def line(now: float, reasons: list[str], load: float, free_gb: float) -> str:
    return f'ts={int(now)} open={0 if reasons else 1} load={load:.2f} free_gb={free_gb:.1f} reasons={",".join(reasons) or "-"}'


def free_gb_now() -> float:
    out = subprocess.run(['vm_stat'], capture_output=True, text=True, timeout=10).stdout
    size = int(re.search(r'page size of (\d+)', out).group(1))  # type: ignore[union-attr]  # vm_stat always prints it
    pages = sum(int(match) for match in re.findall(r'^Pages (?:free|inactive|speculative):\s+(\d+)', out, re.MULTILINE))
    return pages * size / 1024 ** 3


def summarize(lines: list[str], interval: int = 60) -> dict:
    rows = []
    for entry in lines:
        fields = dict(token.split('=', 1) for token in entry.split() if '=' in token)
        if 'open' in fields and 'reasons' in fields:
            rows.append((fields['open'] == '1', fields['reasons']))
    if not rows:
        return {'samples': 0}
    longest = run = 0
    counts: Counter[str] = Counter()
    for is_open, reasons in rows:
        run = 0 if is_open else run + 1
        longest = max(longest, run)
        if not is_open:
            counts.update(reasons.split(','))
    return {'samples': len(rows), 'open_fraction': round(sum(1 for o, _ in rows if o) / len(rows), 4),
            'longest_closed_minutes': longest * interval / 60, 'closed_by': dict(counts.most_common())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or '').split('\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    log = sub.add_parser('log')
    log.add_argument('--out', required=True)
    log.add_argument('--busy-file', default=BUSY_FILE)
    log.add_argument('--max-load', type=float, default=8.0)
    log.add_argument('--need-gb', type=float, default=7.9)
    log.add_argument('--interval', type=int, default=60)
    log.add_argument('--hours', type=float, default=24.0)
    summary = sub.add_parser('summarize')
    summary.add_argument('file')
    summary.add_argument('--interval', type=int, default=60)
    args = parser.parse_args(argv)
    if args.command == 'summarize':
        print(json.dumps(summarize(Path(args.file).read_text().splitlines(), args.interval), indent=2))
        return 0
    deadline = time.time() + args.hours * 3600
    with open(args.out, 'a', buffering=1) as out:
        while time.time() < deadline:
            now = time.time()
            try:
                text = Path(args.busy_file).read_text()
            except OSError:
                text = None
            load, free = os.getloadavg()[0], free_gb_now()
            out.write(line(now, verdict(text, now, load, free, args.max_load, args.need_gb), load, free) + '\n')
            time.sleep(max(0.0, args.interval - (time.time() - now)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
