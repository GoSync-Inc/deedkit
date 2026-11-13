"""Validate Python producer reports and same-job Go inputs without executing suites or publishing."""
# spec: ANYM-421
from __future__ import annotations

from collections import Counter
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def go_identity_reports(root: Path, env=None, *, checkout_tree, control_file, module_reports, max_bytes) -> dict[str, Path] | None:
    """Use this job's completed real Go streams for the identity proof, without rerunning their suites.

    Only the explicit same-job workflow wiring enables this path. Local standalone pytest keeps running
    the real suites. Source, attempt, regular files and successful producer steps are required here;
    go_results and the identity proof still validate every event, verdict and frozen identity.
    """
    env = os.environ if env is None else env
    if not env.get("DEEDKIT_GO_REPORTS"):
        return None
    directory = Path(env["DEEDKIT_GO_REPORTS"])
    sha, attempt = env.get("DEEDKIT_GO_SOURCE_SHA", ""), env.get("GITHUB_RUN_ATTEMPT", "")
    if directory.is_symlink() or not directory.is_dir() or not re.fullmatch(r"[a-f0-9]{40}", sha) \
            or not attempt.isdigit() or int(attempt) < 1 or not checkout_tree(sha, root):
        raise ValueError("Go identity reports must name this checkout and run attempt")
    if control_file(directory, "source-sha") != sha or control_file(directory, "attempt") != attempt:
        raise ValueError("Go identity reports belong to another source or attempt")
    reports = {}
    for check in module_reports:
        if env.get(check.upper() + "_OUTCOME") != "success":
            raise ValueError(f"the real {check} suite did not succeed")
        name = module_reports[check]
        path = directory / name
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= max_bytes:
            raise ValueError(f"the real {check} report is missing or invalid")
        reports[check] = path
    return reports


def shard_suites(path: Path, root: Path, normalize_junit) -> tuple[list[ET.Element], Counter]:
    """A shard's pytest report, validated on its own as `prepare` validates a report: its test suites, which
    must hold every one of its cases, and the counts of its passed, failed and skipped cases."""
    _, counts = normalize_junit(path, root, "python")
    document = ET.fromstring(path.read_bytes())
    suites = [document] if document.tag == "testsuite" else list(document)
    if any(suite.tag != "testsuite" for suite in suites) or sum(len(suite.findall("testcase")) for suite in suites) != sum(counts.values()):
        raise ValueError("a test case lies outside the report's test suites")
    return suites, counts


def shard_outcome(state: str, report: Path, root: Path, suites: list, *, normalize_junit, reporting_module=False) -> str:
    """One shard's `python` outcome from its reported step outcome and its report, whose suites join `suites`.
    A shard passes only with a valid report of executed tests and no failed one; without such a report it
    failed, or its verification is incomplete."""
    try:
        found, counts = shard_suites(report, root, normalize_junit)
        if any((case.get("file") == "tests/test_tiden_ci.py") != reporting_module
               for suite in found for case in suite.findall("testcase")):
            raise ValueError("the Python report includes tests owned by another producer")
    except (OSError, ValueError, ET.ParseError):
        return state if state in {"failure", "cancelled"} else "skipped"
    suites.extend(found)
    if state == "success" and counts["failed"]:
        return "failure"
    return "skipped" if state == "success" and not counts["passed"] and not counts["failed"] else state


def combined_outcome(outcomes: list[str]) -> str:
    """The `python` check over its shards: passed only when every shard passed."""
    if all(outcome == "success" for outcome in outcomes):
        return "success"
    return next((state for state in ("failure", "cancelled") if state in outcomes), "skipped")
