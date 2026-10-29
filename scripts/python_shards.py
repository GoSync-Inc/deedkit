#!/usr/bin/env python3
"""Split the root Python/PostgreSQL suite into file-level shards that run on parallel CI runners.

`checks / verify` runs shard 1 and the `checks / python` matrix jobs run the others (checks.yml). Every test
module under tests/ belongs to exactly one shard: the plan is a pure function of the checkout and the committed
per-file timings (`python_shards.json`), computed the same way on every runner. A module without a timing gets
the default weight; a module pytest would collect that the plan does not name is a collection error, never a
module that runs everywhere or nowhere. CI runs pytest through this file (`python_shards.py pytest <args>`) as
it is in the workflow's commit, applying the shard named by DEEDKIT_TEST_SHARD (`<index>/<count>`) to the
checkout pytest tests; the tested source needs no hook of its own, so a baseline of an older revision is
sharded the same way.
"""
from __future__ import annotations

import argparse
import fnmatch
import functools
import json
import os
from pathlib import Path
import re
import statistics
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = Path(__file__).resolve().with_name("python_shards.json")
SHARD = re.compile(r"([1-9][0-9]*)/([1-9][0-9]*)")


# pytest's default `python_files`; the plugin uses the collecting run's own setting.
PATTERNS = ("test_*.py", "*_test.py")


def checkout_name(path: Path, root: Path) -> str | None:
    """`path` relative to the checkout, its directories resolved and its own name kept, so a symlinked module
    keeps the name it is collected under; None outside the checkout."""
    try:
        return (path.absolute().parent.resolve() / path.name).relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def modules(root: Path, patterns: tuple[str, ...] = PATTERNS) -> list[str]:
    """Every file under tests/ that pytest's `python_files` patterns collect, as sorted checkout paths."""
    names = (checkout_name(path, root) for path in (root / "tests").rglob("*")
             if path.is_file() and any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns))
    return sorted(name for name in names if name)


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
def _plan(root: Path, count: int, patterns: tuple[str, ...] = PATTERNS) -> dict[str, int]:
    return plan(modules(root, patterns), count, json.loads(WEIGHTS.read_text()))


def selected(value: str | None) -> tuple[int, int] | None:
    """The shard `<index>/<count>` names, or None for the whole suite; any other value is an error."""
    if value is None or value == "":
        return None
    match = SHARD.fullmatch(value)
    if not match or int(match.group(1)) > int(match.group(2)):
        raise ValueError(f"DEEDKIT_TEST_SHARD must be <index>/<count>, not {value!r}")
    return int(match.group(1)), int(match.group(2))


def ignored(path: Path, shard: tuple[int, int] | None, root: Path = ROOT, patterns: tuple[str, ...] = PATTERNS) -> bool:
    """Whether this shard leaves `path` to another one. Directories and files pytest does not collect as
    modules are never left; a collected module outside the plan is an error, as it would run in every shard."""
    if shard is None or not path.is_file() or not any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns):
        return False
    index, count = shard
    owner = _plan(root, count, patterns).get(checkout_name(path, root) or "")
    if owner is None:
        raise ValueError(f"{path} is a test module outside the shard plan of {root}/tests; it would run in every shard")
    return owner != index


# The shard of this pytest run, set when the plugin configures it.
_SHARD: tuple[int, int] | None = None


def pytest_configure(config) -> None:
    import pytest
    global _SHARD
    try:
        _SHARD = selected(os.environ.get("DEEDKIT_TEST_SHARD"))
    except ValueError as error:
        raise pytest.UsageError(str(error)) from None
    # pytest matches a pattern with a directory against the whole path; the plan matches file names only.
    if _SHARD and any("/" in pattern or os.sep in pattern for pattern in config.getini("python_files")):
        raise pytest.UsageError("the shard plan supports python_files patterns of file names only, not of paths")


def pytest_ignore_collect(collection_path: Path, config) -> bool | None:
    # None leaves the decision to pytest and the other plugins.
    import pytest
    try:
        return True if ignored(collection_path, _SHARD, config.rootpath, tuple(config.getini("python_files"))) else None
    except ValueError as error:
        raise pytest.UsageError(str(error)) from None


def junit_seconds(paths: list[Path]) -> dict[str, float]:
    """Per-file seconds of pytest xunit1 reports (CI's python.xml), summed over their test cases."""
    seconds: dict[str, float] = {}
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            file = case.get("file")
            if file:
                seconds[file] = seconds.get(file, 0.0) + float(case.get("time") or 0)
    return seconds


def median_seconds(paths: list[Path]) -> dict[str, float]:
    """Per-file median over reports of several runs (a merged python.xml each, or the shard reports of each run):
    a file counts in the reports that hold it, so one run's six shard reports give its own seconds."""
    reports = [junit_seconds([path]) for path in paths]
    names = {name for report in reports for name in report}
    return {name: statistics.median(report[name] for report in reports if name in report) for name in names}


def main() -> int:
    if sys.argv[1:2] == ["pytest"]:
        # pytest with this module as a plugin; only this file's directory joins the import path, in this process.
        import pytest
        return int(pytest.main(sys.argv[2:], plugins=[sys.modules[__name__]]))
    parser = argparse.ArgumentParser(description="Plan or reweigh the root Python suite's CI shards, or run "
                                                 "`pytest <args>` in the shard DEEDKIT_TEST_SHARD names.")
    actions = parser.add_subparsers(dest="action", required=True)
    show = actions.add_parser("plan", help="print each shard's modules and expected seconds")
    show.add_argument("--count", type=int, default=4)
    update = actions.add_parser("weights", help="rewrite python_shards.json from CI python.xml reports (per-file median)")
    update.add_argument("reports", nargs="+", type=Path)
    update.add_argument("--head-start", type=float, help="verify's measured time outside its pytest step, minus a "
                                                          "python job's (job and step times of the same runs)")
    args = parser.parse_args()
    weights = json.loads(WEIGHTS.read_text())
    if args.action == "weights":
        seconds = median_seconds(args.reports)
        weights["seconds"] = {name: round(seconds[name], 1) for name in sorted(seconds)}
        if args.head_start is not None:
            weights["head_start"] = round(args.head_start)
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
