#!/usr/bin/env python3
"""Split the root Python/PostgreSQL suite into file-level shards that run on parallel CI runners.

`checks / verify` runs shard 1 and the `checks / python` matrix jobs run the others (checks.yml). Every test
module under tests/ belongs to exactly one shard: the plan is a pure function of the checkout and the committed
per-file timings (`python_shards.json`), computed the same way on every runner. A module without a timing gets
the default weight, and a file outside the plan is never left out: no shard skips it, so it runs everywhere.
tests/conftest.py applies the shard named by DEEDKIT_TEST_SHARD (`<index>/<count>`) when pytest collects.
"""
from __future__ import annotations

import argparse
import functools
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = Path(__file__).resolve().with_name("python_shards.json")
SHARD = re.compile(r"([1-9][0-9]*)/([1-9][0-9]*)")


def modules(root: Path) -> list[str]:
    """Every file pytest's default `test_*.py` pattern collects under tests/, as sorted checkout paths."""
    return sorted(path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py") if path.is_file())


def plan(files: list[str], count: int, weights: dict) -> dict[str, int]:
    """Longest processing time first: the heaviest module goes to the shard with the least work so far.

    Shard 1 starts with `head_start` seconds, the rest of the verify job it shares a runner with."""
    timings, default = weights["seconds"], float(weights["default"])
    load = [float(weights["head_start"]) if count > 1 else 0.0] + [0.0] * (count - 1)
    assigned = {}
    for file in sorted(files, key=lambda name: (-float(timings.get(name, default)), name)):
        shard = load.index(min(load))
        assigned[file] = shard + 1
        load[shard] += float(timings.get(file, default))
    return assigned


@functools.cache
def _plan(root: Path, count: int) -> dict[str, int]:
    return plan(modules(root), count, json.loads(WEIGHTS.read_text()))


def selected(value: str | None) -> tuple[int, int] | None:
    """The shard `<index>/<count>` names, or None for the whole suite; any other value is an error."""
    if value is None or value == "":
        return None
    match = SHARD.fullmatch(value)
    if not match or int(match.group(1)) > int(match.group(2)):
        raise ValueError(f"DEEDKIT_TEST_SHARD must be <index>/<count>, not {value!r}")
    return int(match.group(1)), int(match.group(2))


def ignored(path: Path, shard: tuple[int, int] | None, root: Path = ROOT) -> bool:
    """Whether this shard leaves `path` to another one; directories and files outside the plan are never left."""
    if shard is None or not path.is_file():
        return False
    try:
        name = path.resolve().relative_to(root).as_posix()
    except ValueError:
        return False
    index, count = shard
    owner = _plan(root, count).get(name)
    return owner is not None and owner != index


def junit_seconds(paths: list[Path]) -> dict[str, float]:
    """Per-file seconds of pytest xunit1 reports (CI's python.xml), summed over their test cases."""
    seconds: dict[str, float] = {}
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            file = case.get("file")
            if file:
                seconds[file] = seconds.get(file, 0.0) + float(case.get("time") or 0)
    return seconds


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan or reweigh the root Python suite's CI shards.")
    actions = parser.add_subparsers(dest="action", required=True)
    show = actions.add_parser("plan", help="print each shard's modules and expected seconds")
    show.add_argument("--count", type=int, default=4)
    update = actions.add_parser("weights", help="rewrite python_shards.json from CI python.xml reports")
    update.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()
    weights = json.loads(WEIGHTS.read_text())
    if args.action == "weights":
        seconds = junit_seconds(args.reports)
        weights["seconds"] = {name: round(seconds[name], 1) for name in sorted(seconds)}
        WEIGHTS.write_text(json.dumps(weights, indent=1) + "\n")
        return 0
    assigned = plan(modules(ROOT), args.count, weights)
    for index in range(1, args.count + 1):
        files = sorted(name for name, shard in assigned.items() if shard == index)
        total = sum(float(weights["seconds"].get(name, weights["default"])) for name in files)
        print(f"shard {index}/{args.count}: {len(files)} modules, {total:.0f} s" + (
            f" after a {weights['head_start']} s head start" if index == 1 and args.count > 1 else ""))
        for name in files:
            print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
