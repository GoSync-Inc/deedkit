#!/usr/bin/env python3
"""The tests an autoloop pull request adds run apart from the existing tests (ANYM-578).

An added file can run inside the process of the existing suites and turn their failures green: a pytest module that
patches the runner, a `pytest_plugins` line, a Go `init()` in an added `_test.go` of an existing package, or an
executable fixture an existing test imports. So for every autoloop pull request (head branch `autoloop-*` in this
repository) the required suites never see a file it adds to a test directory, nor an added test file anywhere:

- `list` (ci.yml `added_list`, once per run) writes the exclusion inventory this run requires: the files and, per
  autoloop pull request, the merge base it was compared against. A pull request compares its head with its merge
  base; a merge queue group reads its pull requests from the queue, each against its own merge base with the group
  base. Other pull requests, pushes and dispatches exclude nothing. Anything unreadable fails closed.
- `strip` (every job that collects, compiles or builds tests for a required check) deletes the listed files from its
  checkouts before it starts, prints them and keeps the inventory in the job's evidence as `exclusions.json`.
- `run` (checks.yml `added`, on its own runner) runs only the added tests: pytest per Python project, `go test` per Go
  module and `node --test` per package, one process each, and records them in `added.json`. No added test passes.

Evidence is reused only with an equal inventory (scripts/ci_reuse.py), and a main push requires none (TIDEN-CI.md).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import urllib.request

REPOSITORY = "GoSync-Inc/deedkit"
AUTOLOOP_PREFIX = "autoloop-"
# The autoloop's test-directory and test-file rules (scripts/autoloop.py TEST_DIR and TEST_FILE).
TEST_DIR = re.compile(r'(^|/)tests?/')
TEST_FILE = re.compile(r'(^|/)test_[^/]*\.py$|\.test\.[^/]+$|_test\.go$')
ADDED = frozenset({"added", "renamed", "copied"})
MAX_FILES = 300  # GitHub's compare lists at most 300 files: a longer comparison cannot be listed whole
SHA = re.compile(r"[0-9a-f]{40}")
EMPTY = {"files": [], "provenance": []}
EVIDENCE = "exclusions.json"
REPORT = "added.json"
# Python projects with their own environment; any other Python test runs in the root project.
PYTHON_PROJECTS = ("services/gateway", "services/calendar", "services/calls")
NODE_TESTS = re.compile(r"\.test\.(js|mjs|cjs|ts)$")
GO_TEST = re.compile(r"^func (Test\w+)\(", re.M)
QUEUE = """query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { mergeQueue(branch: "main") {
  entries(first: 100) { nodes { headCommit { oid } baseCommit { oid } pullRequest {
    number headRefName headRefOid headRepository { nameWithOwner } } } } } } }"""


class Refused(RuntimeError):
    """The inventory cannot be established; the run fails closed."""


def api(path: str, body: dict | None = None) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "deedkit-added-tests"}
    if token := os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + token
    url = "https://api.github.com/" + (path if path == "graphql" else f"repos/{REPOSITORY}/{path}")
    data = None if body is None else json.dumps(body).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=30) as response:
        return json.loads(response.read(8 * 1024 * 1024))


def listed(files: list) -> list[str]:
    """The added (or renamed, or copied) files in a test directory, and the added test files anywhere."""
    names = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str) or not isinstance(item.get("status"), str):
            raise Refused("a compared file has no name or status")
        if item["status"] in ADDED and (TEST_DIR.search(item["filename"]) or TEST_FILE.search(item["filename"])):
            names.append(item["filename"])
    return names


def compared(base: str, head: str, get=api) -> tuple[str, list[str]]:
    """The merge base of `base` and `head` and the files `head` adds against it."""
    if not SHA.fullmatch(str(base)) or not SHA.fullmatch(str(head)):
        raise Refused("a compared commit is not a full SHA")
    try:
        result = get(f"compare/{base}...{head}")
    except Exception as exc:  # any unreadable base or head fails closed
        raise Refused(f"cannot compare {base}...{head}: {exc}") from None
    merge_base = str((result.get("merge_base_commit") or {}).get("sha", ""))
    files = result.get("files")
    if not SHA.fullmatch(merge_base) or not isinstance(files, list):
        raise Refused(f"the comparison {base}...{head} has no merge base or file list")
    if len(files) >= MAX_FILES:
        raise Refused(f"the comparison {base}...{head} lists {len(files)} files and may be truncated")
    return merge_base, listed(files)


def autoloop(branch: str, repository: str) -> bool:
    return branch.startswith(AUTOLOOP_PREFIX) and repository == REPOSITORY


def queued(group: dict, graphql) -> list[dict]:
    """The pull requests of a merge queue group, read from the queue: the entry whose commit is the group's, then
    each entry its base commit names, back to the group base."""
    head, base = str(group.get("head_sha", "")), str(group.get("base_sha", ""))
    match = re.fullmatch(r"refs/heads/gh-readonly-queue/main/pr-(\d+)-[0-9a-f]{40}", str(group.get("head_ref", "")))
    if not SHA.fullmatch(head) or not SHA.fullmatch(base) or not match:
        raise Refused("the merge group event has no head, base or queue branch")
    owner, name = REPOSITORY.split("/")
    try:
        nodes = graphql({"query": QUEUE, "variables": {"owner": owner, "name": name}})["data"]["repository"]["mergeQueue"]["entries"]["nodes"]
        entries = {node["headCommit"]["oid"]: node for node in nodes}
    except Exception as exc:
        raise Refused(f"cannot read the merge queue: {exc}") from None
    chain, commit = [], head
    while commit != base:
        entry = entries.get(commit)
        if entry is None or len(chain) >= len(entries):
            raise Refused(f"the merge queue holds no entry for group commit {commit}")
        chain.append(entry["pullRequest"])
        commit = (entry.get("baseCommit") or {}).get("oid")
    if not chain or chain[0].get("number") != int(match.group(1)):
        raise Refused("the group's queue entry is not its pull request")
    return chain


def required(event_name: str, event: dict, get=api, graphql=None) -> dict:
    """The exclusion inventory this event requires: files and, per autoloop pull request, its comparison."""
    pulls = []
    if event_name == "pull_request":
        pull = event.get("pull_request") or {}
        head, base = pull.get("head") or {}, pull.get("base") or {}
        repository = (head.get("repo") or {}).get("full_name", "")
        pulls = [(pull.get("number"), head.get("ref", ""), repository, head.get("sha"), base.get("sha"))]
    elif event_name == "merge_group":
        group = event.get("merge_group") or {}
        for pull in queued(group, graphql or (lambda body: api("graphql", body))):
            pulls.append((pull.get("number"), pull.get("headRefName", ""), (pull.get("headRepository") or {}).get("nameWithOwner", ""),
                          pull.get("headRefOid"), group["base_sha"]))
    files, provenance = set(), []
    for number, branch, repository, head, base in pulls:
        if not isinstance(number, int) or not isinstance(branch, str):
            raise Refused("a pull request has no number or head branch")
        if not autoloop(branch, repository):
            continue
        merge_base, names = compared(str(base or ""), str(head or ""), get)
        files.update(names)
        provenance.append({"pull_request": number, "head": head, "merge_base": merge_base})
    return {"files": sorted(files), "provenance": sorted(provenance, key=lambda item: item["pull_request"])}


def inventory(text: str) -> dict:
    """A well-formed inventory document; anything else is refused, so a missing list never reads as empty."""
    try:
        document = json.loads(text)
    except (TypeError, ValueError):
        raise Refused("the exclusion inventory is missing or not JSON") from None
    if not isinstance(document, dict) or set(document) != {"files", "provenance"}:
        raise Refused("the exclusion inventory is malformed")
    files, provenance = document["files"], document["provenance"]
    if not isinstance(files, list) or not isinstance(provenance, list) or not all(safe(name) for name in files) \
            or files != sorted(set(files)) \
            or any(not isinstance(item, dict) or set(item) != {"pull_request", "head", "merge_base"} for item in provenance):
        raise Refused("the exclusion inventory is malformed")
    return document


def safe(name) -> bool:
    parts = PurePosixPath(name).parts if isinstance(name, str) else ()
    return bool(parts) and not name.startswith("/") and ".." not in parts and "." not in parts and ".git" not in parts


def strip(document: dict, roots: list[Path], evidence: Path | None) -> list[str]:
    """Delete the listed files from each checkout root, print them and keep the inventory in the job's evidence."""
    for root in roots:
        for name in document["files"]:
            path = root / name
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                raise Refused(f"{name} is not a file in {root}")
    print(f"Excluded {len(document['files'])} file(s) the autoloop pull requests add (ANYM-578):")
    for name in document["files"]:
        print(f"  {name}")
    if evidence is not None:
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / EVIDENCE).write_text(json.dumps(document, sort_keys=True))
    return document["files"]


def owner(name: str, markers: tuple[str, ...], root: Path) -> str:
    """The nearest directory above `name` that holds one of `markers`, or the root."""
    for parent in PurePosixPath(name).parents:
        if str(parent) != "." and any((root / parent / marker).is_file() for marker in markers):
            return str(parent)
    return "."


def plan(document: dict, root: Path) -> list[dict]:
    """One process per language and project: the added tests to run and the command that runs them."""
    groups: dict[tuple, list[str]] = {}
    for name in document["files"]:
        if not (root / name).is_file():
            continue
        if name.endswith(".py") and TEST_FILE.search(name):
            project = next((p for p in PYTHON_PROJECTS if name.startswith(p + "/")), ".")
            groups.setdefault(("python", project), []).append(name)
        elif name.endswith("_test.go"):
            groups.setdefault(("go", owner(name, ("go.mod",), root)), []).append(name)
        elif NODE_TESTS.search(name):
            groups.setdefault(("node", owner(name, ("package.json",), root)), []).append(name)
    runs = []
    for (language, project), names in sorted(groups.items()):
        relative = [str(PurePosixPath(name).relative_to(project)) if project != "." else name for name in names]
        if language == "python":
            argv = ["uv", "run", "--frozen", "--group", "dev", "pytest", "-p", "no:cacheprovider", *relative]
        elif language == "go":
            tests = sorted({test for name in names for test in GO_TEST.findall((root / name).read_text(errors="replace"))})
            packages = sorted({"./" + str(PurePosixPath(name).parent) for name in relative})
            argv = ["go", "test", "-count=1", "-run", f"^({'|'.join(tests)})$" if tests else "^$", *packages]
        else:
            argv = ["node", "--test", *relative]
        # `uv run --frozen` installs a Python project's locked environment itself; a Node package installs its own.
        prepare = [["npm", "ci", "--ignore-scripts"]] if language == "node" and (root / project / "package-lock.json").is_file() else []
        runs.append({"language": language, "project": project, "files": names, "prepare": prepare, "argv": argv})
    return runs


def run(document: dict, root: Path, evidence: Path) -> int:
    runs = plan(document, root)
    if not runs:
        print("No added test: nothing to run, passed.")
    for item in runs:
        print(f"::group::{item['language']} in {item['project']}: {' '.join(item['argv'])}", flush=True)
        item["returncode"] = 0
        for argv in (*item["prepare"], item["argv"]):
            if item["returncode"] == 0:
                item["returncode"] = subprocess.run(argv, cwd=root / item["project"], check=False).returncode
        print("::endgroup::", flush=True)
    result = "none" if not runs else "passed" if all(item["returncode"] == 0 for item in runs) else "failed"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / REPORT).write_text(json.dumps({"result": result, "runs": runs}, sort_keys=True))
    (evidence / "outcomes.json").write_text(json.dumps({"module_checks": {}}))
    print(f"Added tests: {result}")
    return 0 if result != "failed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="write this event's inventory to GITHUB_OUTPUT as `inventory`")
    for name in ("strip", "run", "languages"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, action="append", default=None)
        command.add_argument("--evidence", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "list":
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
            document = required(os.environ["GITHUB_EVENT_NAME"], event)
            print(json.dumps(document, indent=2))
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                output.write(f"inventory={json.dumps(document, sort_keys=True)}\n")
            return 0
        document, roots = inventory(os.environ.get("ADDED_TESTS", "")), arguments.root or [Path(".")]
        if arguments.command == "strip":
            strip(document, roots, arguments.evidence)
            return 0
        if arguments.command == "languages":
            languages = " ".join(sorted({item["language"] for item in plan(document, roots[0])}))
            print(f"Added-test languages: {languages or 'none'}")
            if "GITHUB_OUTPUT" in os.environ:
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    output.write(f"languages={languages}\n")
            return 0
        if arguments.evidence is None:
            parser.error("run needs --evidence")
        return run(document, roots[0], arguments.evidence)
    except Refused as exc:
        print(f"::error::added tests: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
