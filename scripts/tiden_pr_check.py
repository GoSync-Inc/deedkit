#!/usr/bin/env python3
"""Check a pull request's Tiden bookkeeping before it merges; read-only on Tiden (TIDEN-CI.md, PR check).

The pull request body names the specs the change implements or delivers, one kind per line:

    Spec: ANYM-12, ANYM-34
    Delivers: ANYM-56
    Tiden branch: proposal/2026-10-19-example

and every test the diff adds carries `# spec: ANYM-n` (Python) or `// spec: ANYM-n` (JS, TS, Go) on the line
directly above its definition, above its decorators. The check fails when a changed code file has no source anchor on
Tiden main and the body names no spec, when an added test has no marker and is not in NO_SPEC, when a named ID is not
an existing story-level spec on Tiden main or the named branch, or when the named branch is not an open proposal or
review branch, or when that open branch's merge preview changes a spec the body does not name, changes no spec or
holds more than spec changes (one preview parser, shared with the main sync). A failed read or an unknown or
malformed preview is a refusal, not a verdict, except that a forbidden class is judged before the spec numbers. It prints the anchor matches for the job summary. The main-push sync
(scripts/tiden_sync.py) later anchors, links and merges from the same lines and markers.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tiden_drift import ANCHOR_REPOSITORIES, Refused, excluded, shown, tiden  # noqa: E402

SPEC_ID = re.compile(r"ANYM-([1-9][0-9]*)")
ID_LIST = re.compile(r"ANYM-[1-9][0-9]*(?:[ \t]*,[ \t]*ANYM-[1-9][0-9]*)*")
PR_LINE = re.compile(r"^[ \t]*(Spec|Delivers|Tiden branch):[ \t]*(.*?)[ \t]*$", re.MULTILINE)
BRANCH_NAME = re.compile(r"(?:proposal|review)/[a-z0-9]+(?:[/_-][a-z0-9]+)*")
MARKER = re.compile(r"^[ \t]*(#|//)[ \t]*spec:[ \t]*(.*?)[ \t]*$")
# A comment that tries to be a marker but is malformed (wrong case, missing colon) is reported, not ignored.
MARKER_LIKE = re.compile(r"^[ \t]*(#|//)[ \t]*spec\b", re.IGNORECASE)
# Node and Go tests are found by plain patterns, line by line, with no parsing: a line that looks like a test call or
# definition counts, even inside a comment or a string, so a test is never hidden (fail closed). A Node name must be a
# plain string literal, the whole first argument on the call's line, so it equals the title CI reports: non-empty, with
# no escape, `${` or control character (the report rewrites those). A block comment may sit before the `(`; a `test`
# or `it` alone on its line, its `(` on a later one or after a comment left open, is a call this check cannot read,
# so it fails (NODE_SPLIT). COMMENT is one block comment, written so no text matches it two ways (no backtracking).
COMMENT = r"/\*(?:[^*]|\*+[^*/])*\*+/"
NODE_TEST = re.compile(rf"(?<![\w.$])(?:test|it)(?:\.(?:only|skip|todo))?\s*(?:{COMMENT}\s*)*\(")
NODE_SPLIT = re.compile(rf"^\s*(?:await\s+)?(?:test|it)(?:\.(?:only|skip|todo))?\s*(?:{COMMENT}\s*)*"
                        r"(?://.*|/\*(?:[^*]|\*+[^*/])*\**)?$")
NODE_NAME = re.compile(r"\s*(?:'([^'\\\x00-\x1f\x7f]+)'|\"([^\"\\\x00-\x1f\x7f]+)\""
                       r"|`((?:[^`\\$\x00-\x1f\x7f]|\$(?!\{))+)`)\s*,")
GO_TEST = re.compile(rf"^[ \t]*func\s+(Test(?![a-z])\w*)\s*(?:{COMMENT}\s*)*\(", re.MULTILINE)
# Story-level means a leaf that holds criteria: not an Epic and without child specs.
CONTAINER_TYPES = {"Epic"}
# The merge-preview shape of CLI 0.33.0 (read 2026-10-19 from 62 open branches); empty fields may be omitted. CLI 0.41.0
# adds `testDuplicates` (test marks, omitted when empty) and the server `autoMerge` (an intent branch Tiden merges by
# itself; read 2026-10-24 from 51 open branches). Any other key is a change this job cannot judge, so it refuses. The
# main sync (tiden_sync.py) uses the same parser.
PREVIEW_CHANGES = ("additions", "modifications")
PREVIEW_EMPTY = ("deletions", "testAdditions", "testModifications", "testDeletions", "testDuplicates",
                 "componentAdditions", "componentModifications", "componentDeletions")
PREVIEW_OTHER = ("stats", "intentSession", "blockingSessions", "hasRequirementChanges", "autoMerge")
# Tests that verify no spec, as `<path>::<test>` patterns with the reason; widening it is a reviewed code change.
NO_SPEC: tuple[tuple[str, str], ...] = ()
TIMEOUT = 120


@dataclass
class Lines:
    """The Tiden lines of a pull request body; IDs keep their order, the first named is the primary spec."""
    spec: list[int] = field(default_factory=list)
    delivers: list[int] = field(default_factory=list)
    branch: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def named(self) -> list[int]:
        return list(dict.fromkeys(self.delivers + self.spec))


@dataclass(frozen=True)
class Test:
    path: str
    name: str
    line: int
    specs: tuple[int, ...]
    problem: str = ""  # why the marker is missing or malformed; empty when specs holds a valid marker


def ids(text: str) -> list[int] | None:
    if not ID_LIST.fullmatch(text):
        return None
    return list(dict.fromkeys(int(match) for match in SPEC_ID.findall(text)))


def parse_body(body: str) -> Lines:
    lines = Lines()
    for key, value in PR_LINE.findall(body.replace("\r\n", "\n")):
        if key == "Tiden branch":
            value = value.strip("`")
            if lines.branch is not None and lines.branch != value:
                lines.errors.append("the body names more than one `Tiden branch:`")
            elif not BRANCH_NAME.fullmatch(value):
                lines.errors.append(f"`Tiden branch: {shown(value)}` is not a proposal/ or review/ branch name")
            else:
                lines.branch = value
            continue
        found = ids(value)
        if found is None:
            lines.errors.append(f"`{key}: {shown(value)}` is not a comma-separated list of ANYM-n IDs")
            continue
        target = lines.delivers if key == "Delivers" else lines.spec
        target.extend(number for number in found if number not in target)
    return lines


def language(path: str) -> str | None:
    name = PurePosixPath(path).name
    if fnmatchcase(name, "test_*.py") or fnmatchcase(name, "*_test.py"):
        return "python"
    if any(fnmatchcase(name, pattern) for pattern in ("*.test.mjs", "*.test.js", "*.test.ts")):
        return "node"
    if fnmatchcase(name, "*_test.go"):
        return "go"
    return None


LINE_BREAKS = {"python": re.compile(r"\r\n|\r|\n"), "node": re.compile(r"\r\n|[\r\n\u2028\u2029]")}


def source_lines(source: str, kind: str) -> list[str]:
    """The lines of a test file as its language numbers them, so a marker lookup and a test's line agree: Python
    (`ast`) breaks on CR, LF and CRLF, JavaScript also on U+2028 and U+2029, Go (matches are counted by `\\n`) only
    on LF. `splitlines()` also splits on form feed, U+0085 and others, which shifts every later marker."""
    if kind in LINE_BREAKS:
        return LINE_BREAKS[kind].split(source)
    return [line.removesuffix("\r") for line in source.split("\n")]


def marker(lines: list[str], index: int, comment: str) -> tuple[tuple[int, ...], str]:
    """The marker on the line above 0-based line `index`, as (spec numbers, problem)."""
    above = lines[index - 1] if index > 0 else ""
    match = MARKER.match(above)
    if not match:
        return (), "malformed marker" if MARKER_LIKE.match(above) else "no marker"
    if match.group(1) != comment:
        return (), f"marker must start with `{comment}`"
    found = ids(match.group(2))
    if not found:
        return (), "malformed marker"
    return tuple(found), ""


def python_tests(source: str) -> list[tuple[str, int, str]]:
    """(name, 0-based line of the marker's target, problem) of each module-level test and each test method of a
    `Test*` class, nested classes included; the target is the `@` line of the first decorator, else the `def`."""
    tree, lines = ast.parse(source), source_lines(source, "python")
    found = []

    def start(node) -> int:
        first = node.lineno - 1
        for decorator in node.decorator_list:
            line = decorator.lineno - 1  # a parenthesized decorator reports its expression line, not the `@`
            while line > 0 and not lines[line].lstrip().startswith("@"):
                line -= 1
            first = min(first, line)
        return first

    def walk(body, prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                found.append((prefix + node.name, start(node), ""))
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                walk(node.body, prefix + node.name + "::")

    walk(tree.body, "")
    return found


def tests_in(path: str, source: str) -> list[tuple[str, int, str]]:
    """(name, 0-based line of the marker's target, problem) of each test in one file. A Node call whose name is not a
    plain literal is keyed by its stripped line, so an unchanged or moved one is not new."""
    kind = language(path)
    if kind == "python":
        return python_tests(source)
    if kind == "go":
        return [(match.group(1), source.count("\n", 0, match.start()), "") for match in GO_TEST.finditer(source)
                if match.group(1) != "TestMain"]
    found = []
    for line, text in enumerate(source_lines(source, "node")):
        if NODE_SPLIT.match(text):
            found.append((f"<{text.strip()}>", line, "the test call is split before its `(`; write `test(` on one "
                                                     "line"))
        for match in NODE_TEST.finditer(text):
            name = NODE_NAME.match(text, match.end())
            if name:
                found.append((next(group for group in name.groups() if group is not None), line, ""))
            else:
                found.append((f"<{text.strip()}>", line, "the test name is not a plain string literal on the call's "
                                                         "line (no escape, `${`, expression or line break)"))
    return found


def added_tests(path: str, before: str | None, after: str) -> list[Test]:
    """Tests in `after` whose name the same file did not have in `before`; existing tests need no marker."""
    try:
        # A literal title and an unreadable call's line are different kinds of key, so they never collide.
        old = {(bool(problem), name) for name, _, problem in tests_in(path, before)} if before is not None else set()
    except SyntaxError:
        old = set()
    try:
        new = tests_in(path, after)
    except SyntaxError as error:
        raise Refused(f"{shown(path)} does not parse ({error.msg}, line {error.lineno})") from None
    comment = "#" if language(path) == "python" else "//"
    lines = source_lines(after, language(path) or "")
    result, seen = [], set()
    for name, index, problem in new:
        if (bool(problem), name) in old or (bool(problem), name) in seen:
            continue
        seen.add((bool(problem), name))
        specs, missing = marker(lines, index, comment)
        result.append(Test(path, name, index + 1, () if problem else specs, problem or missing))
    return result


def no_spec(test: Test) -> bool:
    return any(fnmatchcase(f"{test.path}::{test.name}", pattern) for pattern, _ in NO_SPEC)


def git(root: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        raise Refused(f"git {arguments[0]} could not run") from None
    if completed.returncode:
        raise Refused(f"git {arguments[0]} exited {completed.returncode}")
    return completed.stdout


def changed(root: Path, base: str, head: str) -> list[str]:
    """Paths added or modified from base to head, NUL-delimited and exact, as the drift check reads them."""
    output = git(root, "diff", "-z", "--name-only", "--no-renames", "--diff-filter=d", base, head, "--")
    try:
        return [path.decode("utf-8") for path in output.split(b"\0") if path]
    except UnicodeDecodeError:
        raise Refused("a changed path is not valid UTF-8") from None


def blob(root: Path, commit: str, path: str) -> str | None:
    try:
        completed = subprocess.run(["git", "-C", str(root), "cat-file", "blob", f"{commit}:{path}"],
                                   capture_output=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        raise Refused(f"git cat-file could not run for {shown(path)}") from None
    if completed.returncode:
        return None
    return completed.stdout.decode("utf-8", errors="replace")


def diff_tests(root: Path, base: str, head: str, paths: list[str]) -> list[Test]:
    found = []
    for path in paths:
        if language(path) is None:
            continue
        after = blob(root, head, path)
        if after is None:
            raise Refused(f"{shown(path)} is missing at {head}")
        found.extend(added_tests(path, blob(root, base, path), after))
    return found


def listing(cli: str, product: str, branch: str) -> dict[int, dict]:
    """Specs of one Tiden branch by number, validated as complete; each keeps its sources."""
    return validate_listing(tiden(cli, ["spec", "list", "--branch", branch, "--include-sources"], product, retry=True),
                            branch)


def validate_listing(value: dict, branch: str) -> dict[int, dict]:
    """The completeness rules of `listing`, for a listing already read (a saved file included)."""
    specs, pagination = value.get("requirements"), value.get("pagination")
    if (not isinstance(specs, list) or not isinstance(pagination, dict) or pagination.get("nextPageToken") != ""
            or type(pagination.get("totalCount")) is not int or pagination["totalCount"] != len(specs)):
        raise Refused(f"the spec listing of {branch} is incomplete")
    result, uuids = {}, set()
    for spec in specs:
        if (not isinstance(spec, dict) or not isinstance(spec.get("id"), str) or not spec["id"]
                or spec["id"] in uuids or type(spec.get("seqNum")) is not int or spec["seqNum"] in result):
            raise Refused(f"the spec listing of {branch} has a missing or duplicate spec id or number")
        sources, count = spec.get("sources", []), spec.get("sourceCount", 0)
        if (not isinstance(sources, list) or not all(isinstance(source, dict) for source in sources)
                or type(count) is not int or count != len(sources)):
            raise Refused(f"the spec listing of {branch} has malformed or incomplete sources")
        if type(spec.get("childrenCount", 0)) is not int or not isinstance(spec.get("type"), str):
            raise Refused(f"the spec listing of {branch} has a malformed spec type or child count")
        uuids.add(spec["id"])
        result[spec["seqNum"]] = spec
    return result


def anchor_paths(spec: dict) -> set[str]:
    paths = set()
    for source in spec.get("sources", []):
        repository = source.get("repository")
        if repository is not None and not isinstance(repository, str):
            raise Refused(f"ANYM-{spec['seqNum']} has a malformed source repository")
        if source.get("sourceType") == "repo_file" and (repository or "") in ANCHOR_REPOSITORIES:
            if not isinstance(source.get("repoPath"), str) or not source["repoPath"]:
                raise Refused(f"ANYM-{spec['seqNum']} has a repo_file source without a path")
            paths.add(source["repoPath"])
    return paths


def anchor_map(specs: dict[int, dict]) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for number in sorted(specs):
        for path in anchor_paths(specs[number]):
            result.setdefault(path, []).append(number)
    return result


def story(spec: dict) -> bool:
    return spec["type"] not in CONTAINER_TYPES and spec.get("childrenCount", 0) == 0


def branch_status(cli: str, product: str, name: str) -> str | None:
    value = tiden(cli, ["branch", "list"], product, retry=True)
    branches = value.get("branches")
    if not isinstance(branches, list) or not all(isinstance(branch, dict) for branch in branches):
        raise Refused("the branch listing is malformed")
    statuses = [branch.get("status") for branch in branches if branch.get("name") == name]
    if len(statuses) > 1:
        raise Refused(f"the branch listing names {name} more than once")
    return statuses[0] if statuses else None


def preview_number(item: object) -> int:
    """The one spec number of a merge-preview addition or modification: its own `seqNum` and that of every nested
    object (a modification may carry the main and branch copies) must agree."""
    if not isinstance(item, dict):
        raise Refused("the merge preview has a change that is not an object")
    copies = [item, *(value for value in item.values() if isinstance(value, dict))]
    numbers = [copy["seqNum"] for copy in copies if "seqNum" in copy]  # a present null counts, an absent key not
    if not all(type(number) is int for number in numbers):  # before the set: 50.0 and True would equal an int
        raise Refused("the merge preview has a change without exactly one spec number")
    found = set(numbers)
    if len(found) != 1:
        raise Refused("the merge preview has a change without exactly one spec number")
    return found.pop()


def preview_numbers(preview: dict, name: str) -> tuple[set[int], list[str]]:
    """The specs a merge preview changes, and the forbidden change classes it holds (anything but additions and
    modifications of specs). A forbidden preview returns no numbers; an unknown or malformed shape refuses."""
    unknown = sorted(set(preview) - {*PREVIEW_CHANGES, *PREVIEW_EMPTY, *PREVIEW_OTHER})
    if unknown:
        raise Refused(f"the preview of {name} has unknown fields ({', '.join(map(shown, unknown))})")
    lists = {key: preview.get(key, []) for key in (*PREVIEW_CHANGES, *PREVIEW_EMPTY)}
    if not all(isinstance(value, list) for value in lists.values()):
        raise Refused(f"the merge preview of {name} is malformed")
    other = [key for key in PREVIEW_EMPTY if lists[key]]
    stats = preview.get("stats", {})
    if not isinstance(stats, dict) or not all(type(value) is int for value in stats.values()):
        raise Refused(f"the merge preview of {name} has malformed stats")
    other += [f"stats.{key}" for key, value in stats.items() if value and key not in PREVIEW_CHANGES]
    other += [f"stats.{key}" for key in PREVIEW_CHANGES if stats.get(key, len(lists[key])) != len(lists[key])]
    other += [key for key in ("intentSession", "autoMerge") if preview.get(key) is not None]
    if preview.get("blockingSessions") not in (None, 0, []):
        other.append("blockingSessions")
    if not isinstance(preview.get("hasRequirementChanges", True), bool):
        other.append("hasRequirementChanges")
    if other:
        return set(), other
    return {preview_number(item) for key in PREVIEW_CHANGES for item in lists[key]}, []


def preview_problems(cli: str, product: str, lines: Lines, main: dict[int, dict], known: dict[int, dict],
                     failures: list[str]) -> set[int]:
    """Compare the named open branch's merge preview with the body: what sync B's merge step would refuse after the
    squash fails here, before it. Returns the specs the preview changes (empty when it is forbidden or changes none);
    a failed read or an unknown or malformed shape raises Refused, which the job reports as not checked."""
    branch = lines.branch
    numbers, forbidden = preview_numbers(tiden(cli, ["branch", "merge-preview", "--name", branch], product, retry=True),
                                         branch)
    if forbidden:
        failures.append(f"the preview of {branch} holds more than spec changes ({', '.join(forbidden)})")
        return numbers
    stray = sorted(numbers - set(lines.named))
    if stray:
        failures.append(f"the preview of {branch} changes " + ", ".join(f"ANYM-{number}" for number in stray)
                        + ", which the body does not name")
    elif not numbers:
        failures.append(f"the preview of {branch} changes no spec")
    for number in lines.named:
        if number in known and number not in numbers and number not in main:  # unknown IDs fail once, in check()
            failures.append(f"ANYM-{number} (named by the body) is not a spec on Tiden main or {branch}")
    return numbers


def check(root: Path, base: str, head: str, body: str, cli: str, product: str, report) -> int:
    failures: list[str] = []
    lines = parse_body(body)
    failures.extend(lines.errors)
    # The merge base, so a pull request checkout (a merge commit) and a branch head both diff only the change.
    base = git(root, "merge-base", base, head).decode().strip()
    paths = changed(root, base, head)
    code = [path for path in paths if not excluded(path)]
    tests = diff_tests(root, base, head, paths)
    main = listing(cli, product, "main")
    anchors = anchor_map(main)
    known = main
    previewed: set[int] | None = None
    if lines.branch:
        status = branch_status(cli, product, lines.branch)
        if status != "open":
            failures.append(f"`Tiden branch: {lines.branch}` is {'missing' if status is None else status}, not open")
        else:
            known = {**main, **listing(cli, product, lines.branch)}
            previewed = preview_problems(cli, product, lines, main, known, failures)

    report("## Tiden PR check")
    report(f"Changed files {len(paths)}, code files {len(code)}, added tests {len(tests)}.")
    if lines.named:
        report("Named specs (the first is primary): " + ", ".join(
            f"ANYM-{number}" + (f" {known[number]['title']}" if number in known else " (unknown)")
            for number in lines.named))
    if lines.branch:
        report(f"Tiden branch: {lines.branch}")
    if previewed:
        report("Branch preview: " + ", ".join(f"ANYM-{number}" for number in sorted(previewed)))
        for number in lines.named:
            if number not in previewed and number in main:
                report(f"- ANYM-{number}: named, not changed by {lines.branch}")
    unanchored = [path for path in code if path not in anchors]
    for path in code:
        found = anchors.get(path)
        report(f"- {shown(path)}: " + (", ".join(f"ANYM-{number}" for number in found) if found else "no anchor"))
    if unanchored and lines.named:
        report(f"After the merge the sync anchors {len(unanchored)} unanchored file(s) to ANYM-{lines.named[0]}.")
    if unanchored and not lines.named:
        failures.append(f"{len(unanchored)} changed code file(s) have no anchor on Tiden main and the body names no "
                        "`Spec:` or `Delivers:`: " + ", ".join(shown(path) for path in unanchored))

    cited: dict[int, list[str]] = {}
    for number in lines.named:
        cited.setdefault(number, []).append("the body")
    for test in tests:
        where = f"{shown(test.path)}:{test.line} {shown(test.name)}"
        if test.problem:
            if not no_spec(test):
                failures.append(f"added test {where}: {test.problem}")
            continue
        report(f"- test {where} -> " + ", ".join(f"ANYM-{number}" for number in test.specs))
        for number in test.specs:
            cited.setdefault(number, []).append(where)
    for number, places in sorted(cited.items()):
        spec = known.get(number)
        if spec is None:
            failures.append(f"ANYM-{number} (named by {places[0]}) is not a spec on Tiden main"
                            + (f" or {lines.branch}" if lines.branch else ""))
        elif not story(spec):
            failures.append(f"ANYM-{number} (named by {places[0]}) is a {spec['type']} with child specs or an Epic; "
                            "name the story that holds the criterion")

    if failures:
        report(f"{len(failures)} problem(s):")
        report("\n".join(f"- {failure}" for failure in failures))
        report("Fix the pull request body or the markers (TIDEN-CI.md, PR check), then rerun.")
        return 1
    report("Passed.")
    return 0


def event(path: str | None) -> dict:
    if not path:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise Refused("the event file is unreadable") from None
    return value if isinstance(value, dict) else {}


def skip_reason(payload: dict) -> str | None:
    pull = payload.get("pull_request")
    if not isinstance(pull, dict):
        return "not a pull request run"
    head = ((pull.get("head") or {}).get("repo") or {}).get("full_name")
    if not head or head != (payload.get("repository") or {}).get("full_name"):
        return "a fork pull request receives no Tiden token"
    if not os.environ.get("TIDEN_API_TOKEN"):
        return "Tiden reporting is not configured (no TIDEN_API_TOKEN)"
    return None


def merged_base(root: Path, head: str, pull: dict) -> str | None:
    """The base tip of GitHub's test merge when `head` is that merge of the pull request (first parent the base tip,
    second the pull request head). The event's base.sha is the base when the pull request was last pushed; once
    the base moves on, a diff from it would also hold every later base commit."""
    tip = (pull.get("head") or {}).get("sha")
    parents = git(root, "rev-list", "--parents", "-n", "1", head, "--").decode().split()
    return parents[1] if tip and len(parents) == 3 and parents[2] == tip else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a pull request's Tiden lines, markers and anchors.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base", help="the base commit (default: a test merge's base tip, else the event's base.sha)")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--body-file", type=Path, help="read the body from a file instead of the event")
    parser.add_argument("--cli", default="tiden")
    args = parser.parse_args()
    summary = os.environ.get("GITHUB_STEP_SUMMARY")

    def report(line: str) -> None:
        print(line)
        if summary:
            with open(summary, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    try:
        payload = event(os.environ.get("GITHUB_EVENT_PATH"))
        if args.body_file is None:
            reason = skip_reason(payload)
            if reason:
                report(f"Tiden PR check skipped: {reason}.")
                return 0
        pull = payload.get("pull_request") or {}
        body = args.body_file.read_text(encoding="utf-8") if args.body_file else (pull.get("body") or "")
        base = args.base or merged_base(args.root.resolve(), args.head, pull) or (pull.get("base") or {}).get("sha")
        if not base:
            raise Refused("no base commit: pass --base")
        return check(args.root.resolve(), base, args.head, body, args.cli, os.environ["TIDEN_PRODUCT_ID"], report)
    except Refused as error:
        report(f"Refused: {error}")
        return 1
    except Exception as error:  # noqa: BLE001 - fail loudly without echoing data the error may hold
        report(f"Refused: unexpected {type(error).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
