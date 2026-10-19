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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tiden_drift import REPOSITORY, Refused, excluded, shown, tiden  # noqa: E402
from tiden_pr_check import (anchor_map, blob, branch_status, changed, diff_tests, language,  # noqa: E402
                            listing, parse_body, story)

SUBJECT = re.compile(r"\(#([1-9][0-9]*)\)$")
SHA = re.compile(r"[0-9a-f]{40}")
OPEN_CHECK = re.compile(r"\[open: (?:live|measured)\]")
# Test files whose suite CI does not publish to Tiden, so a marked test there has no identity to link yet.
UNPUBLISHED: tuple[tuple[str, str], ...] = ()
GO_MODULE = re.compile(r"^module[ \t]+(\S+)[ \t]*$", re.MULTILINE)
SOURCE_FIELDS = ("sourceType", "title", "locator", "url", "repoPath", "repository", "lineStart", "lineEnd", "excerpt")
# The merge-preview shape of CLI 0.33.0 (read 2026-10-19 from 62 open branches); empty fields may be omitted. Any
# other key is a change this job cannot judge, so it refuses.
PREVIEW_CHANGES = ("additions", "modifications")
PREVIEW_EMPTY = ("deletions", "testAdditions", "testModifications", "testDeletions", "componentAdditions",
                 "componentModifications", "componentDeletions")
PREVIEW_OTHER = ("stats", "intentSession", "blockingSessions", "hasRequirementChanges")
TIMEOUT = 120
# Recovery stays inside the workflow's concurrency group; a write by hand outside it can drop sources a run adds.
RECOVER = "rerun this job or dispatch this SHA from main (TIDEN-CI.md, main sync)"
WINDOW = "only in a maintenance window, with the sync workflow disabled and no run in progress (TIDEN-CI.md)"


def write(cli: str, arguments: list[str], product: str | None) -> None:
    """One Tiden write; its output is not trusted (each step reads back instead) and never shown. `spec update`,
    `test links` and `test link-spec` take global IDs and reject `--product-id` (CLI 0.33.0), so product is None."""
    command = [cli, *arguments, *(["--product-id", product] if product else []), "--format", "json"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        raise Refused(f"tiden {' '.join(arguments[:2])} timed out or could not run") from None
    if completed.returncode:
        raise Refused(f"tiden {' '.join(arguments[:2])} exited {completed.returncode}")


def read(cli: str, arguments: list[str]) -> dict:
    """A read by global ID, without `--product-id`, validated like tiden_drift.tiden."""
    try:
        completed = subprocess.run([cli, *arguments, "--format", "json"], capture_output=True, text=True,
                                   timeout=TIMEOUT)
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


def preview_number(item: object) -> int:
    """The one spec number of a merge-preview addition or modification: its own `seqNum` and that of every nested
    object (a modification may carry the main and branch copies) must agree."""
    if not isinstance(item, dict):
        raise StepFailed("merge", "the merge preview has a change that is not an object")
    copies = [item, *(value for value in item.values() if isinstance(value, dict))]
    numbers = [copy["seqNum"] for copy in copies if "seqNum" in copy]  # a present null counts, an absent key not
    if not all(type(number) is int for number in numbers):  # before the set: 50.0 and True would equal an int
        raise StepFailed("merge", "the merge preview has a change without exactly one spec number")
    found = set(numbers)
    if len(found) != 1:
        raise StepFailed("merge", "the merge preview has a change without exactly one spec number")
    return found.pop()


def preview_numbers(preview: dict, name: str) -> set[int]:
    """The specs a merge preview changes; anything but additions and modifications of specs refuses."""
    unknown = sorted(set(preview) - {*PREVIEW_CHANGES, *PREVIEW_EMPTY, *PREVIEW_OTHER})
    if unknown:
        raise StepFailed("merge", f"the preview of {name} has unknown fields ({', '.join(map(shown, unknown))})")
    lists = {key: preview.get(key, []) for key in (*PREVIEW_CHANGES, *PREVIEW_EMPTY)}
    if not all(isinstance(value, list) for value in lists.values()):
        raise StepFailed("merge", f"the merge preview of {name} is malformed")
    other = [key for key in PREVIEW_EMPTY if lists[key]]
    stats = preview.get("stats", {})
    if not isinstance(stats, dict) or not all(type(value) is int for value in stats.values()):
        raise StepFailed("merge", f"the merge preview of {name} has malformed stats")
    other += [f"stats.{key}" for key, value in stats.items() if value and key not in PREVIEW_CHANGES]
    other += [f"stats.{key}" for key in PREVIEW_CHANGES if stats.get(key, len(lists[key])) != len(lists[key])]
    if preview.get("intentSession") is not None:
        other.append("intentSession")
    if preview.get("blockingSessions") not in (None, 0, []):
        other.append("blockingSessions")
    if not isinstance(preview.get("hasRequirementChanges", True), bool):
        other.append("hasRequirementChanges")
    if other:
        raise StepFailed("merge", f"the preview of {name} holds more than spec changes ({', '.join(other)}); "
                                  f"merge it by hand {WINDOW}, then {RECOVER}")
    return {preview_number(item) for key in PREVIEW_CHANGES for item in lists[key]}


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
        preview = tiden(cli, ["branch", "merge-preview", "--name", name], product)
    except Refused as error:
        raise StepFailed("merge", str(error)) from None
    numbers = preview_numbers(preview, name)
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
    value = tiden(cli, ["test", "list", "--all", "--view", "identity"], product)
    rows, pagination = value.get("identities"), value.get("pagination")
    if (not isinstance(rows, list) or not isinstance(pagination, dict) or pagination.get("nextPageToken") != ""
            or type(pagination.get("totalCount")) is not int or pagination["totalCount"] != len(rows)
            or not all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in rows)):
        raise Refused("the test listing is incomplete")
    return [row for row in rows if row.get("kind") == "case"]


def go_package(root: Path, sha: str, path: str) -> str | None:
    """The Go import path of a test file's package at `sha`, `<module>[/<package dir>]`, from the nearest go.mod above
    it; None outside any module."""
    directory = PurePosixPath(path).parent
    for parent in (directory, *directory.parents):
        found = GO_MODULE.search(blob(root, sha, str(parent / "go.mod")) or "")
        if found:
            below = directory.relative_to(parent)
            return found.group(1) + ("" if str(below) == "." else f"/{below}")
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


def link(cli: str, product: str, tests: list, main: dict[int, dict], report, dry: bool = False,
         packages: dict[str, str | None] | None = None) -> None:
    marked = [test for test in tests if test.specs]
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
        if unknown:
            missing.append(f"{shown(test.name)} names " + ", ".join(f"ANYM-{number}" for number in unknown)
                           + ", not on Tiden main")
            continue
        package = (packages or {}).get(test.path)
        if language(test.path) == "go" and not package:
            missing.append(f"{shown(test.path)} {shown(test.name)} lies in no Go module")
            continue
        cases = matches(rows, test.path, test.name, package)
        if not cases:
            missing.append(f"{shown(test.path)} {shown(test.name)} has no imported case on Tiden main")
            continue
        for case in cases:
            for number in test.specs:
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
                                   {test.path: go_package(root, sha, test.path) for test in tests
                                    if language(test.path) == "go"})),
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a merged pull request's Tiden bookkeeping to Tiden main.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--sha", help="the merged commit on main (default: HEAD, or the --pr merge commit)")
    parser.add_argument("--pr", type=int, help="the pull request number (default: `(#n)` in the commit subject)")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "GoSync-Inc/deedkit"))
    parser.add_argument("--gh", default="gh")
    parser.add_argument("--cli", default="tiden")
    parser.add_argument("--dry-run", action="store_true", help="read and report, write nothing to Tiden")
    args = parser.parse_args()
    summary = os.environ.get("GITHUB_STEP_SUMMARY")

    def report(line: str) -> None:
        print(line)
        if summary:
            with open(summary, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    try:
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
