#!/usr/bin/env python3
"""After a merge to main, write the merged pull request's Tiden bookkeeping to Tiden main (TIDEN-CI.md, main sync).

The squash subject names the pull request as `(#n)`; its body carries the lines the PR check (scripts/tiden_pr_check.py)
validated. In order, each step reading back what it wrote:

1. merge: merge `Tiden branch:` only when its merge preview holds just the specs the body names, with no deletion,
   test or component change, conflict or blocking session;
2. links: link each test the commit adds to the specs its `spec:` marker names, add-only;
3. anchors: anchor each changed code file that no spec anchors to the primary (first named) spec, resending every
   existing source with its provenance and comparing every source on read-back;
4. status: set each `Delivers:` spec Done, or Review while its text holds an `[open: live]` or `[open: measured]`
   check.

Every step is idempotent, so a rerun finishes what an earlier run left. Any mismatch fails the run and names the step;
recovery is a rerun of the job or a dispatch of the SHA, both inside the workflow's one concurrency group. Writers must
not overlap: the anchors step reads the spec again right before it replaces the source list, but only one writer at a
time makes that safe. So a change on Tiden by hand (a conflicted branch, a source repair) waits for a maintenance
window with the workflow disabled and no run in progress.

`--ready-branch <name>` runs the ready merge instead (harness tiden.md, Ready merge; TIDEN-CI.md, Ready merge): it merges
a proposal or review branch of ready cards to Tiden main before any pull request names them. It merges only when the
merge preview holds spec additions or text changes alone and every changed spec is `Backlog` on the branch and on main
with a `Ready:` line outside code (a text change a new one, not main's); then it reads each spec back on main by its
number (a branch copy has its own record ID): `Backlog` and the same `Ready:` line. It is a dispatch of the same
workflow, so it runs inside the sync's one concurrency group and is no change by hand.
"""
from __future__ import annotations

import argparse
from collections import Counter
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tiden_drift import REPOSITORY, Refused, excluded, shown, tiden  # noqa: E402
from tiden_ci import go_module_path  # noqa: E402
from tiden_health import prose  # noqa: E402
from tiden_pr_check import (BRANCH_NAME, GO_TEST, Test, anchor_map, blob, branch_status, changed,  # noqa: E402
                            diff_tests, language, listing, marker, parse_body, preview_number, preview_numbers,
                            source_lines, story)

SUBJECT = re.compile(r"\(#([1-9][0-9]*)\)$")
SHA = re.compile(r"[0-9a-f]{40}")
OPEN_CHECK = re.compile(r"\[open: (?:live|measured)\]")
# A card's readiness record (harness tiden.md template), also as a list item or in bold. A legacy `Accepted:` alone is
# no `Ready:` line: such a card gets `Ready:` citing it first. A record starts its line (a top-level list item at most):
# indented, quoted or nested it may be code inside a container, and a raw HTML block may hide or quote it.
READY = re.compile(r"(?:[-*] )?(?:\*\*)?(Ready|Accepted):(.*)")
RAW_HTML = re.compile(r"<(?:pre|script|style|textarea|!--)", re.IGNORECASE)
# Any readiness text at all: main's record is compared without the strict reading, which could hide it.
ANY_READY = re.compile(r"\b(?:Ready|Accepted):")
# What a branch copy of a spec has of its own (record, branch, times; sources compare by their fields), and the text
# a ready merge may change: anything else differing from main is more than a text change.
COPY_FIELDS = frozenset({"id", "branchId", "branchStatus", "createdAt", "updatedAt", "sourceId", "sourceCount",
                         "sources"})
TEXT_FIELDS = frozenset({"content", "title"})
# Test files whose suite CI does not publish to Tiden, so a marked test there has no identity to link yet.
KEPT_SECONDS = 600  # the time kept tests may spend on link reads, so the later steps still run
UNPUBLISHED: tuple[tuple[str, str], ...] = ()
SOURCE_FIELDS = ("sourceType", "title", "locator", "url", "repoPath", "repository", "lineStart", "lineEnd", "excerpt")
TIMEOUT = 120
RETRY_DELAY = 2  # seconds before the one retry of a read
RETRY_MIN = 10  # a retry needs at least this many seconds left of the read's one TIMEOUT budget
# Recovery stays inside the workflow's concurrency group; a write by hand outside it can drop sources a run adds.
RECOVER = "rerun this job or dispatch this SHA from main (TIDEN-CI.md, main sync)"
WINDOW = "only in a maintenance window, with the sync workflow disabled and no run in progress (TIDEN-CI.md)"


def write(cli: str, arguments: list[str], product: str | None) -> None:
    """One Tiden write; its output is not trusted (each step reads back instead) and never shown. `spec update`,
    `test links` and `test link-spec` take global IDs; CLI 0.33.0 rejected `--product-id` on them (0.41.0 accepts it
    as a check), so product is None."""
    command = [cli, *arguments, *(["--product-id", product] if product else []), "--format", "json"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        raise Refused(f"tiden {' '.join(arguments[:2])} timed out or could not run") from None
    if completed.returncode:
        raise Refused(f"tiden {' '.join(arguments[:2])} exited {completed.returncode}")


def read(cli: str, arguments: list[str]) -> dict:
    """A read by global ID, without `--product-id`, validated like tiden_drift.tiden; a failed read is repeated once inside the same TIMEOUT budget."""
    start = time.monotonic()
    try:
        return read_once(cli, arguments)
    except Refused:
        left = TIMEOUT - (time.monotonic() - start) - RETRY_DELAY
        if left < RETRY_MIN:
            raise
    time.sleep(RETRY_DELAY)
    return read_once(cli, arguments, left)


def read_once(cli: str, arguments: list[str], timeout: float | None = None) -> dict:
    try:
        completed = subprocess.run([cli, *arguments, "--format", "json"], capture_output=True, text=True,
                                   timeout=timeout or TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        raise Refused(f"tiden {' '.join(arguments[:2])} timed out or could not run") from None
    if completed.returncode:
        raise Refused(f"tiden {' '.join(arguments[:2])} exited {completed.returncode}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise Refused(f"tiden {' '.join(arguments[:2])} returned malformed JSON") from None
    if not isinstance(value, dict):
        raise Refused(f"tiden {' '.join(arguments[:2])} returned no object")
    return value


class StepFailed(Exception):
    def __init__(self, step: str, message: str):
        super().__init__(message)
        self.step = step


def git(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True,
                                   timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        raise Refused(f"git {arguments[0]} could not run") from None
    if completed.returncode:
        raise Refused(f"git {arguments[0]} exited {completed.returncode}")
    return completed.stdout


def pull_request(gh: str, repository: str, number: int) -> dict:
    """The pull request from the GitHub API; its stderr is never shown."""
    try:
        completed = subprocess.run([gh, "api", f"repos/{repository}/pulls/{number}"], capture_output=True, text=True,
                                   timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        raise StepFailed("pr", f"reading pull request #{number} timed out or could not run") from None
    if completed.returncode:
        raise StepFailed("pr", f"reading pull request #{number} exited {completed.returncode}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise StepFailed("pr", f"pull request #{number} is malformed JSON") from None
    if not isinstance(value, dict):
        raise StepFailed("pr", f"pull request #{number} is not an object")
    return value


def merge(cli: str, product: str, name: str, named: list[int], report, dry: bool = False) -> bool:
    """Merge the branch; true only for a dry run that would have merged it."""
    try:
        status = branch_status(cli, product, name)
    except Refused as error:
        raise StepFailed("merge", str(error)) from None
    if status == "merged":
        report(f"merge: {name} is merged already.")
        return False
    if status != "open":
        raise StepFailed("merge", f"{name} is {'missing' if status is None else status}, not open")
    try:
        preview = tiden(cli, ["branch", "merge-preview", "--name", name], product, retry=True)
    except Refused as error:
        raise StepFailed("merge", str(error)) from None
    try:
        numbers, forbidden = preview_numbers(preview, name)
    except Refused as error:
        raise StepFailed("merge", str(error)) from None
    if forbidden:
        raise StepFailed("merge", f"the preview of {name} holds more than spec changes ({', '.join(forbidden)}); "
                                  f"merge it by hand {WINDOW}, then {RECOVER}")
    stray = sorted(numbers - set(named))
    if stray or not numbers:
        raise StepFailed("merge", f"the preview of {name} changes " + (
            ", ".join(f"ANYM-{number}" for number in stray) + ", which the body does not name" if stray
            else "no spec"))
    if dry:
        report(f"merge: would merge {name} ({', '.join(f'ANYM-{number}' for number in sorted(numbers))}).")
        return True
    try:
        write(cli, ["branch", "merge", "--name", name], product)
    except Refused as error:
        raise StepFailed("merge", f"{error}; {RECOVER}: it reads the branch back first") from None
    try:
        status = branch_status(cli, product, name)
    except Refused as error:
        raise StepFailed("merge", f"read-back: {error}") from None
    if status != "merged":
        raise StepFailed("merge", f"read-back: {name} is {status}, not merged")
    report(f"merge: {name} merged ({', '.join(f'ANYM-{number}' for number in sorted(numbers))}).")
    return False


def identities(cli: str, product: str) -> list[dict]:
    value = tiden(cli, ["test", "list", "--all", "--view", "identity"], product, retry=True)
    rows, pagination = value.get("identities"), value.get("pagination")
    if (not isinstance(rows, list) or not isinstance(pagination, dict) or pagination.get("nextPageToken") != ""
            or type(pagination.get("totalCount")) is not int or pagination["totalCount"] != len(rows)
            or not all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in rows)):
        raise Refused("the test listing is incomplete")
    return [row for row in rows if row.get("kind") == "case"]


def go_package(root: Path, sha: str, path: str) -> str | None:
    """The Go import path of a test file's package at `sha`, `<module>[/<package dir>]`, from the nearest go.mod above
    it; None outside any module or when that go.mod declares no module path (never a module further up)."""
    directory = PurePosixPath(path).parent
    for parent in (directory, *directory.parents):
        text = blob(root, sha, str(parent / "go.mod"))
        if text is not None:
            module, below = go_module_path(text), directory.relative_to(parent)
            return module and module + ("" if str(below) == "." else f"/{below}")
    return None


def matches(rows: list[dict], path: str, name: str, package: str | None = None) -> list[dict]:
    """The Tiden cases CI imported for one test: a Python identity is `<module>[.<Class>]::<name>[<params>]`, a Node
    one is its file path and title, a Go one `<package>::<Test>` or, for each subtest, `<package>::<Test>/<sub>`."""
    if language(path) == "go":
        if not package:
            return []
        prefix = f"{package}::{name}"
        return [row for row in rows if row.get("signature") == prefix
                or str(row.get("signature", "")).startswith(prefix + "/")]
    if language(path) == "python":
        cls, _, leaf = name.rpartition("::")  # nested classes are `Outer::Inner`, dotted in the JUnit class name
        prefix = (str(PurePosixPath(path).with_suffix("")).replace("/", ".") + (f".{cls.replace('::', '.')}" if cls
                                                                                 else "") + "::" + leaf)
        return [row for row in rows if row.get("signature") == prefix
                or str(row.get("signature", "")).startswith(prefix + "[")]
    return [row for row in rows if row.get("filePath") == path and row.get("title") == name]


def linked(cli: str, test: str) -> set[str]:
    value = read(cli, ["test", "links", test])
    links = value.get("links", [])
    if not isinstance(links, list) or not all(isinstance(link, dict) and isinstance(link.get("requirementId"), str)
                                              for link in links):
        raise Refused("the test links are malformed")
    return {link["requirementId"] for link in links}


def go_scan(source: str) -> tuple[str, set[int]]:
    """The Go source with every comment and string, rune or raw-string literal blanked (newlines kept), so a
    declaration found in it is executable code and not text that only looks like one; and the 0-based lines that
    begin with a real `//` comment, so a marker inside a block comment or a literal is not one."""
    out, comments, i, n, line = [], set(), 0, len(source), 0
    while i < n:
        two, ch = source[i:i + 2], source[i]
        if two == "//":
            end = source.find("\n", i)
            end = n if end < 0 else end
            if not source[source.rfind("\n", 0, i) + 1:i].strip():
                comments.add(line)
        elif two == "/*":
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
        elif ch in "\"'`":
            end = i + 1
            while end < n and source[end] != ch and (ch == "`" or source[end] != "\n"):
                end += 2 if source[end] == "\\" and ch != "`" else 1
            end = min(end + 1, n)
        else:
            out.append(ch)
            line += ch == "\n"
            i += 1
            continue
        out.append("".join(c if c == "\n" else " " for c in source[i:end]))
        line += source.count("\n", i, end)
        i = end
    return "".join(out), comments


def go_declarations(source: str) -> list[tuple[str, int]]:
    """(name, 0-based line) of each executable `func TestX` of one Go file."""
    code = go_scan(source)[0]
    return [(match.group(1), code.count("\n", 0, match.start())) for match in GO_TEST.finditer(code)
            if match.group(1) != "TestMain"]


def go_markers(source: str) -> dict[str, tuple[int, ...]]:
    """Name -> spec numbers of each executable Go test declared once whose marker is a real `//` comment line."""
    lines, comments = source_lines(source, "go"), go_scan(source)[1]
    found = go_declarations(source)
    result = {}
    for name, index in found:
        specs, problem = marker(lines, index, "//")
        if not problem and index - 1 in comments and [other for other, _ in found].count(name) == 1:
            result[name] = specs
    return result


def kept_go_tests(root: Path, base: str, head: str, paths: list[str]) -> list[Test]:
    """Marked Go tests that a changed file had before and still has, with the very same marker: a table-driven one
    gains subtests when a row is added, and each new subtest is a case of its own that only a link makes covered.
    Only executable declarations with a real `//` marker line count, so a marker in a comment or string never claims
    a test."""
    found = []
    for path in paths:
        before, after = (blob(root, ref, path) if language(path) == "go" else None for ref in (base, head))
        if before is None or after is None:
            continue
        was, lines = go_markers(before), go_declarations(after)
        for name, specs in go_markers(after).items():
            if was.get(name) == specs:
                found.append(Test(path, name, next(index for other, index in lines if other == name) + 1, specs))
    return found


def link(cli: str, product: str, tests: list, main: dict[int, dict], report, dry: bool = False,
         packages: dict[str, str | None] | None = None, kept: list | None = None) -> None:
    """Links the cases of each marked added test; `kept` are marked tests that existed before, linked when they have
    cases and their spec is on main, and otherwise left alone."""
    marked = [test for test in tests if test.specs]
    marked += [test for test in kept or [] if test.specs]
    old, deadline = {id(test) for test in kept or []}, time.monotonic() + KEPT_SECONDS
    if not marked:
        report("links: no marked test added.")
        return
    try:
        rows = identities(cli, product)
    except Refused as error:
        raise StepFailed("links", str(error)) from None
    missing = []
    for test in marked:
        reason = next((why for pattern, why in UNPUBLISHED if fnmatchcase(PurePosixPath(test.path).name, pattern)), None)
        if reason:
            report(f"links: {shown(test.path)} {shown(test.name)} not linked: {reason}.")
            continue
        unknown = [number for number in test.specs if number not in main]
        if unknown and id(test) in old:
            continue
        if unknown:
            missing.append(f"{shown(test.name)} names " + ", ".join(f"ANYM-{number}" for number in unknown)
                           + ", not on Tiden main")
            continue
        package = (packages or {}).get(test.path)
        if language(test.path) == "go" and not package:
            if id(test) in old:
                continue
            missing.append(f"{shown(test.path)} {shown(test.name)} lies in no Go module")
            continue
        cases = matches(rows, test.path, test.name, package)
        if id(test) in old:  # a test of the same name in a build-excluded or other file of the package is not this one
            cases = [case for case in cases if case.get("filePath") == PurePosixPath(test.path).name]
        if not cases and id(test) in old:
            continue
        if not cases:
            missing.append(f"{shown(test.path)} {shown(test.name)} has no imported case on Tiden main")
            continue
        stopped = False
        for case in cases:
            for number in test.specs:
                if id(test) in old and time.monotonic() > deadline:
                    report(f"links: {shown(test.path)} {shown(test.name)} not rechecked further: the kept-test time "
                           "budget is spent.")
                    stopped = True
                    break
                spec = main[number]["id"]
                try:
                    if spec in linked(cli, case["id"]):
                        continue
                    if dry:
                        report(f"links: would link {shown(case.get('signature', case['id']))} -> ANYM-{number}.")
                        continue
                    write(cli, ["test", "link-spec", case["id"], "--spec-id", spec], None)
                    if spec not in linked(cli, case["id"]):
                        missing.append(f"read-back: {shown(case.get('signature', case['id']))} is not linked to "
                                       f"ANYM-{number}")
                        continue
                except Refused as error:
                    missing.append(f"{shown(case.get('signature', case['id']))} -> ANYM-{number}: {error}")
                    continue
                report(f"links: {shown(case.get('signature', case['id']))} -> ANYM-{number}.")
            if stopped:
                break
    if missing:
        raise StepFailed("links", "; ".join(missing))


# The source update regenerates record IDs, so each resent source keeps its original ID, and its complete original
# metadata when that nests objects, in provenance receipts (docs/operations/tiden-product-workflow.md, Reconciliation
# and maintenance). The metadata itself is resent whole: a direct update keeps nested values (checked 2026-10-19).
def resent(source: dict) -> dict:
    """An existing source as the update must resend it; an existing receipt is never overwritten."""
    value = {key: source[key] for key in SOURCE_FIELDS if source.get(key) is not None}
    original: dict = source["metadata"] if isinstance(source.get("metadata"), dict) else {}
    metadata = dict(original)
    if source.get("id") and "historical_source_id" not in metadata:
        metadata["historical_source_id"] = source["id"]
    if any(isinstance(item, (dict, list)) for item in original.values()) and "historical_metadata_json" not in metadata:
        metadata["historical_metadata_json"] = json.dumps(original, separators=(",", ":"), sort_keys=True)
    if metadata:
        value["metadata"] = metadata
    return value


def normalized(source: dict) -> str:
    """A source as comparable text: every field, an empty string and an absent field alike, and the whole metadata;
    record IDs and timestamps are left out because the update regenerates them."""
    fields = {key: (None if source.get(key) in (None, "") else source[key]) for key in SOURCE_FIELDS}
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    return json.dumps([fields, metadata], sort_keys=True)


def anchor(cli: str, product: str, paths: list[str], primary: int | None, main: dict[int, dict], sha: str, pr: int,
           report, dry: bool = False) -> None:
    anchors = anchor_map(main)
    missing = [path for path in paths if path not in anchors]
    if not missing:
        report(f"anchors: all {len(paths)} changed code file(s) are anchored.")
        return
    if primary is None:
        raise StepFailed("anchors", "the body names no spec for " + ", ".join(shown(path) for path in missing))
    if not dry:
        main = listing(cli, product, "main")  # fresh, right before the whole source list is replaced
        anchors = anchor_map(main)
        missing = [path for path in paths if path not in anchors]
        if not missing:
            report(f"anchors: all {len(paths)} changed code file(s) are anchored.")
            return
    spec = main.get(primary)
    if spec is None or not story(spec):
        raise StepFailed("anchors", f"the primary spec ANYM-{primary} is not a story-level spec on Tiden main")
    before = spec.get("sources", [])
    sources = [resent(source) for source in before] + [
        {"sourceType": "repo_file", "repoPath": path, "repository": REPOSITORY, "title": "",
         "metadata": {"added_by": "tiden_sync", "commit_sha": sha, "pull_request": f"#{pr}"}} for path in missing]
    if dry:
        report(f"anchors: would anchor {len(missing)} file(s) to ANYM-{primary}: "
               + ", ".join(shown(path) for path in missing))
        return
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as stream:
        json.dump(sources, stream)
    try:
        write(cli, ["spec", "update", "--id", spec["id"], "--sources-file", stream.name], None)
        after = listing(cli, product, "main").get(primary)
    except Refused as error:
        raise StepFailed("anchors", f"{error}; {RECOVER}: it reads ANYM-{primary} again first") from None
    finally:
        os.unlink(stream.name)
    if after is None:
        raise StepFailed("anchors", f"read-back: ANYM-{primary} is gone from Tiden main")
    expected, actual = Counter(map(normalized, sources)), Counter(map(normalized, after.get("sources", [])))
    if expected != actual:
        raise StepFailed("anchors", f"read-back of ANYM-{primary}: {sum((expected - actual).values())} sent source(s) "
                                    f"missing or changed, {sum((actual - expected).values())} unexpected; "
                                    f"repair the sources of ANYM-{primary} by hand {WINDOW}")
    report(f"anchors: {len(missing)} file(s) anchored to ANYM-{primary}: " + ", ".join(shown(p) for p in missing))


def settle_status(cli: str, product: str, delivers: list[int], main: dict[int, dict], report,
                  dry: bool = False) -> None:
    problems = []
    for number in delivers:
        spec = main.get(number)
        if spec is None:
            problems.append(f"ANYM-{number} is not on Tiden main")
            continue
        target = "Review" if OPEN_CHECK.search(spec.get("content") or "") else "Done"
        if spec.get("status") == target:
            report(f"status: ANYM-{number} is {target} already.")
            continue
        if dry:
            report(f"status: would set ANYM-{number} {spec.get('status')} -> {target}.")
            continue
        try:
            write(cli, ["spec", "update", "--id", spec["id"], "--status", target], None)
            now = listing(cli, product, "main").get(number) or {}
        except Refused as error:
            problems.append(f"ANYM-{number}: {error}")
            continue
        if now.get("status") != target:
            problems.append(f"read-back: ANYM-{number} is {now.get('status')}, not {target}")
            continue
        report(f"status: ANYM-{number} {spec.get('status')} -> {target}"
               + (" (an open live or measured check remains)" if target == "Review" else "") + ".")
    if problems:
        raise StepFailed("status", "; ".join(problems))


def sync(root: Path, sha: str, number: int, body: str, cli: str, product: str, report, dry: bool = False) -> int:
    lines = parse_body(body)
    if lines.errors:
        raise StepFailed("parse", "; ".join(lines.errors))
    parent = git(root, "rev-parse", "--verify", f"{sha}^1").strip()
    paths = changed(root, parent, sha)
    code = [path for path in paths if not excluded(path)]
    tests = diff_tests(root, parent, sha, paths)
    kept = kept_go_tests(root, parent, sha, paths)
    report(f"Pull request #{number}, commit {sha}: {len(code)} code file(s), {len(tests)} added test(s); named "
           + (", ".join(f"ANYM-{n}" for n in lines.named) or "no spec")
           + (f"; branch {lines.branch}" if lines.branch else "") + ".")
    try:
        pending = bool(lines.branch) and merge(cli, product, lines.branch or "", lines.named, report, dry)
    except (StepFailed, Refused):
        raise
    except Exception as error:  # noqa: BLE001 - named like the other steps, and the later steps still stop
        raise StepFailed("merge", f"unexpected {type(error).__name__}") from None
    failures = []
    try:
        # A dry run before the merge reads the branch view, which is main with the branch's specs on top.
        main = listing(cli, product, lines.branch if pending and lines.branch else "main")
    except Refused as error:
        raise StepFailed("listing", str(error)) from None
    unknown = [n for n in lines.named if n not in main]
    if unknown:
        failures.append(StepFailed("resolve", ", ".join(f"ANYM-{n}" for n in unknown) + " not on Tiden main"))
    steps = (("links", lambda: link(cli, product, tests, main, report, dry,
                                   {test.path: go_package(root, sha, test.path) for test in (*tests, *kept)
                                    if language(test.path) == "go"}, kept)),
             ("anchors", lambda: anchor(cli, product, code, lines.named[0] if lines.named else None, main, sha, number,
                                        report, dry)),
             ("status", lambda: settle_status(cli, product, lines.delivers, main, report, dry)))
    for name, step in steps:
        try:
            step()
        except StepFailed as error:
            failures.append(error)
        except Refused as error:
            failures.append(StepFailed(name, str(error)))
        except Exception as error:  # noqa: BLE001 - one step's defect must not silence the others
            failures.append(StepFailed(name, f"unexpected {type(error).__name__}"))
    if failures:
        for failure in failures:
            report(f"Failed at step {failure.step}: {failure}")
        report(f"Recovery: {RECOVER}; a change by hand on Tiden main needs a maintenance window (TIDEN-CI.md).")
        return 1
    report("Dry run: nothing was written." if dry else "Synced.")
    return 0


def run(root: Path, sha: str | None, pr: int | None, repository: str, gh: str, cli: str, product: str, report,
        dry: bool = False) -> int:
    if sha is None and pr is None:
        sha = "HEAD"
    if sha is not None:
        sha = git(root, "rev-parse", "--verify", "--end-of-options", f"{sha}^{{commit}}").strip()
        subject = git(root, "log", "-1", "--format=%s", sha).strip()
        match = SUBJECT.search(subject)
        if not match:
            report(f"Commit {sha} names no pull request `(#n)` in its subject; nothing to sync.")
            return 0
        if pr is not None and pr != int(match.group(1)):
            raise StepFailed("pr", f"commit {sha} names #{match.group(1)}, not #{pr}")
        pr = int(match.group(1))
    if pr is None:
        raise StepFailed("pr", "no pull request number")
    value = pull_request(gh, repository, pr)
    merged = value.get("merge_commit_sha")
    if value.get("merged") is not True or not isinstance(merged, str) or not SHA.fullmatch(merged):
        raise StepFailed("pr", f"pull request #{pr} is not merged")
    if (value.get("base") or {}).get("ref") != "main":
        raise StepFailed("pr", f"pull request #{pr} did not merge into main")
    if sha is None:
        sha = git(root, "rev-parse", "--verify", "--end-of-options", f"{merged}^{{commit}}").strip()
        match = SUBJECT.search(git(root, "log", "-1", "--format=%s", sha).strip())
        if not match or int(match.group(1)) != pr:
            raise StepFailed("pr", f"commit {sha} does not name #{pr} in its subject")
    if merged != sha:
        raise StepFailed("pr", f"pull request #{pr} merged as {merged}, not {sha}")
    return sync(root, sha, pr, value.get("body") or "", cli, product, report, dry)


def ready_line(content: str) -> tuple[str | None, bool]:
    """The first `Ready:` record of a spec outside code (fenced, indented, spans), without its bold or list mark, and
    whether a legacy `Accepted:` record is there. A record without text after the colon is none, and a spec with a raw
    HTML code, script or comment block has none."""
    found, legacy = None, False
    if RAW_HTML.search(content):
        return None, False
    for _, text in prose(content):
        match = READY.match(text)
        value = " ".join(match.group(2).replace("*", " ").split()) if match else ""
        if not match or not re.search(r"\w", value):
            continue
        if match.group(1) == "Accepted":
            legacy = True
        elif found is None:
            found = f"Ready: {value}"
    return found, legacy


def flat(text: str) -> str:
    """Text without bold marks, with one space after a record's colon and between words."""
    return " ".join(ANY_READY.sub(lambda match: match.group(0) + " ", text.replace("*", " ")).split())


def stale_ready(main_content: str, line: str) -> str | None:
    """Why a branch's `Ready:` line is no refreshed record of a spec on main: main's text has it in any markup, or main
    has readiness text that the strict reading cannot compare."""
    if flat(line) in flat(main_content) or ready_line(main_content)[0] == line:
        return "keeps main's `Ready:` line; a reviewed change refreshes it"
    if ANY_READY.search(main_content) and ready_line(main_content) == (None, False):
        return "has a readiness record on main that cannot be compared; fix main's record by hand first"
    return None


def structure(spec: dict) -> dict:
    """A spec without its text and a copy's own record fields, with its sources by their fields: what a ready merge
    leaves as main has it. An absent field and a null alike."""
    value = {key: item for key, item in spec.items() if key not in COPY_FIELDS | TEXT_FIELDS and item is not None}
    value["sources"] = sorted(normalized(source) for source in spec.get("sources") or [])
    return value


def card(spec: dict) -> dict:
    """A spec as a ready merge checks it, text included, without a copy's own record fields."""
    return structure(spec) | {key: spec.get(key) for key in TEXT_FIELDS}


def differing(before: dict, after: dict, skip: frozenset = frozenset()) -> list[str]:
    return sorted(key for key in set(before) | set(after) if key not in skip and before.get(key) != after.get(key))


def ready_check(cli: str, product: str, name: str) -> tuple[set[int], dict[int, dict], dict[int, dict]]:
    """Read and check a branch for a ready merge: the numbers it changes and both listings, or a refusal naming its
    cause."""
    try:
        preview = tiden(cli, ["branch", "merge-preview", "--name", name], product, retry=True)
        numbers, forbidden = preview_numbers(preview, name)
        if forbidden:
            raise StepFailed("preview", f"the preview of {name} holds more than spec additions or text changes "
                                        f"({', '.join(forbidden)})")
        if not numbers:
            raise StepFailed("preview", f"the preview of {name} changes no spec")
        versions = {preview_number(item): item for item in preview.get("modifications", [])}
        branch, main = listing(cli, product, name), listing(cli, product, "main")
    except Refused as error:
        raise StepFailed("preview", str(error)) from None
    problems = []
    for number in sorted(numbers):
        spec = branch.get(number)
        if spec is None:
            problems.append(f"ANYM-{number} is not on {name}")
            continue
        line, legacy = ready_line(spec.get("content") or "")
        if spec.get("status") != "Backlog":
            problems.append(f"ANYM-{number} is {spec.get('status')} on {name}, not Backlog")
        if number in main and main[number].get("status") != "Backlog":
            problems.append(f"ANYM-{number} is {main[number].get('status')} on main: a change to work in progress "
                            "goes on the review branch of the pull request that builds it")
        if number in versions and number not in main:
            problems.append(f"ANYM-{number} is a text change of a spec that is not on main")
        if number in main:
            # The listings and the preview's two versions must both keep main's structure.
            item = versions.get(number, {})
            fields = set(differing(structure(main[number]), structure(spec)))
            if isinstance(item.get("mainVersion"), dict) and isinstance(item.get("branchVersion"), dict):
                fields |= set(differing(structure(item["mainVersion"]), structure(item["branchVersion"])))
            if fields:
                problems.append(f"ANYM-{number} changes more than its text on {name} ({', '.join(sorted(fields))})")
        if line is None:
            problems.append(f"ANYM-{number} has no `Ready:` line"
                            + (" (a legacy `Accepted:` card gets `Ready:` citing it first)" if legacy else ""))
        elif number in main and (stale := stale_ready(main[number].get("content") or "", line)):
            problems.append(f"ANYM-{number} {stale}")
    if problems:
        raise StepFailed("ready", "; ".join(problems))
    return numbers, branch, main


def records(specs: dict[int, dict]) -> dict[int, tuple[str, dict]]:
    return {number: (spec["id"], card(spec)) for number, spec in specs.items()}


def ready_merge(cli: str, product: str, name: str, report, dry: bool = False) -> int:
    """Merge a branch of ready cards to Tiden main and read each spec back; any refusal names its cause."""
    if not BRANCH_NAME.fullmatch(name):
        raise StepFailed("branch", f"{shown(name)} is not a proposal/ or review/ branch name")
    try:
        status = branch_status(cli, product, name)
    except Refused as error:
        raise StepFailed("branch", str(error)) from None
    if status == "merged":
        # A merged branch can no longer be listed, so nothing here can show what the merge put on main.
        raise StepFailed("branch", f"{name} is merged already and cannot be read back here: the receipt of the run "
                                   "that merged it lists every spec; if that run failed, check those specs on main")
    if status != "open":
        raise StepFailed("branch", f"{name} is {'missing' if status is None else status}, not open")
    numbers, branch, main = ready_check(cli, product, name)
    named = ", ".join(f"ANYM-{number}" for number in sorted(numbers))
    if dry:
        report(f"ready merge: would merge {name} ({named}).")
        report("Dry run: nothing was written.")
        return 0
    # Tiden merges a branch by name, with no revision to hold it to: read it all again just before the merge, and
    # after it check main against what was checked, so a write in between fails the run instead of passing it.
    again = ready_check(cli, product, name)
    if again[0] != numbers or records(again[1]) != records(branch) or records(again[2]) != records(main):
        raise StepFailed("ready", f"{name} or main changed while it was checked; nothing was merged: dispatch the "
                                  "ready merge again when no one writes to the branch")
    try:
        write(cli, ["branch", "merge", "--name", name], product)
    except Refused as error:
        try:
            status = branch_status(cli, product, name)
        except Refused:
            status = None
        if status != "merged":
            raise StepFailed("merge", f"{error}; {name} is {status or 'unread'}: dispatch the ready merge of {name} "
                                      "again once the branch list shows it open") from None
        report(f"ready merge: the merge call failed ({error}), but {name} is merged; reading it back.")
    try:
        status = branch_status(cli, product, name)
        now = listing(cli, product, "main")
    except Refused as error:
        raise StepFailed("read-back", str(error)) from None
    if status != "merged":
        raise StepFailed("read-back", f"{name} is {status}, not merged")
    lines = {number: ready_line(branch[number].get("content") or "")[0] for number in numbers}
    problems = [f"ANYM-{number} is on main, but not on the checked branch" for number in sorted(set(now) - set(main)
                                                                                                 - numbers)]
    for number in sorted(numbers):
        spec = now.get(number)
        if spec is None:
            problems.append(f"ANYM-{number} is not on main")
            continue
        if spec.get("status") != "Backlog":
            problems.append(f"ANYM-{number} is {spec.get('status')}, not Backlog")
        if ready_line(spec.get("content") or "")[0] != lines[number]:
            problems.append(f"ANYM-{number} does not hold the branch's `Ready:` line")
        # A text change keeps main's record; a new spec keeps its branch record (no record is created on a merge).
        expected = (main.get(number) or branch[number])["id"]
        if spec["id"] != expected:
            problems.append(f"ANYM-{number} is record {spec['id']} on main, not {expected}")
        fields = differing(card(branch[number]), card(spec))
        if fields:
            problems.append(f"ANYM-{number} on main differs from the checked branch copy ({', '.join(fields)})")
    # The listing names a spec's parent (absent when it has none): a parent counts each new spec under it, and
    # anything else of a spec the branch did not change stays, its parent included.
    gained = Counter(branch[number].get("parentId") for number in numbers if number not in main)
    for number in sorted(set(main) - numbers):
        before, after = main[number], now.get(number)
        expected = card(before) | {"childrenCount": before.get("childrenCount", 0) + gained[before["id"]]}
        if after is None or after["id"] != before["id"] or differing(
                expected, card(after) | {"childrenCount": after.get("childrenCount", 0)}):
            problems.append(f"ANYM-{number} changed on main, but not on the checked branch")
    if problems:
        raise StepFailed("read-back", "; ".join(problems))
    for number in sorted(numbers):
        report(f"ready merge: ANYM-{number} {now[number]['id']} Backlog on main"
               + (" (text changed)." if number in main else " (new)."))
    report(f"Ready merge of {name} done ({named}).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a merged pull request's Tiden bookkeeping to Tiden main.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--sha", help="the merged commit on main (default: HEAD, or the --pr merge commit)")
    parser.add_argument("--pr", type=int, help="the pull request number (default: `(#n)` in the commit subject)")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "GoSync-Inc/deedkit"))
    parser.add_argument("--gh", default="gh")
    parser.add_argument("--cli", default="tiden")
    parser.add_argument("--ready-branch", help="merge this proposal or review branch of ready cards to Tiden main "
                                               "instead (empty: the main sync)")
    parser.add_argument("--dry-run", action="store_true", help="read and report, write nothing to Tiden")
    args = parser.parse_args()
    summary = os.environ.get("GITHUB_STEP_SUMMARY")

    def report(line: str) -> None:
        print(line)
        if summary:
            with open(summary, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    try:
        if args.ready_branch:
            if args.sha or args.pr is not None:
                raise StepFailed("branch", "a ready merge takes a branch alone, no commit or pull request")
            return ready_merge(args.cli, os.environ["TIDEN_PRODUCT_ID"], args.ready_branch, report, args.dry_run)
        return run(args.root.resolve(), args.sha, args.pr, args.repository, args.gh, args.cli,
                   os.environ["TIDEN_PRODUCT_ID"], report, args.dry_run)
    except StepFailed as error:
        report(f"Failed at step {error.step}: {error}")
        return 1
    except Refused as error:
        report(f"Refused: {error}")
        return 1
    except Exception as error:  # noqa: BLE001 - fail loudly without echoing data the error may hold
        report(f"Refused: unexpected {type(error).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
