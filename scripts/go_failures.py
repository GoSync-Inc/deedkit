#!/usr/bin/env python3
"""Name the failed tests of a `go test -json` file in the CI log (ANYM-801), so a red Go step reads without its artifact.

Prints `FAIL <package> <test>` and at most 40 output lines per failed test, `FAIL <package>` for a package that failed
without a failed test (a build failure, a data race or timeout outside a test verdict), at most 200 lines in all. The
step's own exit status stands: this script reads, prints the tests' own output and exits 0. Standard library only: CI
runs it with the runner's python3.
"""
from collections.abc import Iterable
from io import TextIOWrapper
import json
from pathlib import Path
import sys

MAX_TEST_LINES = 40
MAX_LINES = 200
FIELDS = ("Package", "Test", "ImportPath", "Output", "FailedBuild")
VERDICTS = {"pass", "fail", "skip"}


def report(stream: Iterable[bytes]) -> tuple[list[str], int]:
    """Return the report's lines, not yet capped at MAX_LINES, and the number of unreadable stream lines."""
    outputs: dict[tuple[str, str], list[str]] = {}
    builds: dict[str, list[str]] = {}
    verdicts: dict[tuple[str, str], str] = {}
    failed_builds: list[str] = []
    causes: dict[str, str] = {}
    skipped = 0
    for raw in stream:
        if not raw.strip():
            continue
        try:
            event = json.loads(raw.decode(errors="replace"))
        except (ValueError, RecursionError):
            skipped += 1
            continue
        if not isinstance(event, dict) or not isinstance(event.get("Action"), str) \
                or not all(isinstance(event.get(field, ""), str) for field in FIELDS):
            skipped += 1
            continue
        action, text = event["Action"], event.get("Output", "")
        if action.startswith("build-"):
            import_path = event.get("ImportPath", "")
            if action == "build-fail":
                failed_builds.append(import_path)
            kept = builds.setdefault(import_path, [])
        else:
            key = event.get("Package", ""), event.get("Test", "")
            kept = outputs.setdefault(key, [])
            if action in VERDICTS:
                verdicts[key] = action
                if event.get("FailedBuild"):
                    causes[key[0]] = event["FailedBuild"]
        if action in ("output", "build-output"):
            kept.extend(text.splitlines()[:MAX_TEST_LINES - len(kept)])
    explained = {package for (package, test), verdict in verdicts.items() if test and verdict == "fail"}
    lines: list[str] = []
    for key, verdict in verdicts.items():
        package, test = key
        if verdict != "fail":
            continue
        if test:
            lines += [f"FAIL {package} {test}", *outputs.get(key, [])]
            continue
        if package not in explained:
            lines += [f"FAIL {package}", *(builds.get(causes.get(package, ""), []) + outputs.get(key, []))[:MAX_TEST_LINES]]
        # A timeout or crash leaves its tests without a verdict; their names and output are the explanation.
        for (owner, name), kept in outputs.items():
            if owner == package and name and (owner, name) not in verdicts:
                lines += [f"FAIL {owner} {name}", *kept]
    claimed = set(causes.values())
    for import_path in dict.fromkeys(failed_builds):
        if import_path not in claimed:
            lines += [f"FAIL {import_path}", *builds.get(import_path, [])]
    return lines, skipped


def bounded(lines: list[str], skipped: int = 0) -> list[str]:
    """Cap the report so that it and the note on unreadable lines stay within MAX_LINES, the count last."""
    room = MAX_LINES - (1 if skipped else 0)
    return lines if len(lines) <= room else [*lines[:room - 1], f"... {len(lines) - room + 1} more"]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: go_failures.py <go test -json file>", file=sys.stderr)
        return 2
    if isinstance(sys.stdout, TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")  # output may hold a lone surrogate
    try:
        with Path(argv[1]).open("rb") as stream:
            lines, skipped = report(stream)
    except FileNotFoundError:
        print("no go test output file")
        return 0
    if skipped:
        print(f"go_failures: {skipped} unreadable lines skipped", file=sys.stderr, flush=True)
    for line in bounded(lines, skipped):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
