#!/usr/bin/env python3
"""Require a Tiden source anchor for every code file changed since the drift watermark, then advance it.

Runs after every merge to main (.github/workflows/tiden-drift.yml, TIDEN-CI.md). The target is the head of main when
the run starts, so a run replaced in the concurrency queue, a delayed run and a rerun all reconcile the newest merge.
When every changed path is anchored or excluded, the watermark advances with `empty_sync`, a compare-and-set on
`--from`; manual audited advances merge a drift-sync branch instead. Anything unknown fails without an advance, and
after an advance was sent nothing is retried: a fresh run reads the server watermark again.
"""
from __future__ import annotations

import argparse
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
import subprocess
import sys

REPOSITORY = "github.com/GoSync-Inc/deedkit"
# Anchors recorded before the rename carry the former repository name, or none.
ANCHOR_REPOSITORIES = {"", REPOSITORY, "github.com/GoSync-Inc/anymsg"}
# The reviewed categories that need no anchor; `*` also matches `/`. Widening this list is a code change reviewed with
# its test, never a data edit.
EXCLUDED = (
    ("*.md", "Markdown documents and process text"),
    ("docs/*", "documentation"),
    (".claude/*", "agent rules"),
    ("tests/*", "tests trace to specs through Tiden test links, not anchors"),
    ("*/tests/*", "module tests, linked like the root tests"),
    ("*_test.go", "Go tests, linked like the other tests"),
    ("*.test.mjs", "Node tests, linked like the other tests"),
    ("*.test.js", "Node tests, linked like the other tests"),
    ("*.test.ts", "frontend tests, linked like the other tests"),
    ("*/fixtures/*", "synthetic test data"),
    ("*/testdata/*", "synthetic Go test data"),
    ("uv.lock", "lockfile"),
    ("*/uv.lock", "lockfile"),
    ("package-lock.json", "lockfile"),
    ("*/package-lock.json", "lockfile"),
    ("*/go.sum", "lockfile"),
    ("*/go.mod", "dependency pins"),
    ("*/requirements*.in", "dependency pins"),
    ("*/requirements*.txt", "dependency pins"),
)
SHA = re.compile(r"[0-9a-f]{40}")
TIMEOUT = 120


class Refused(Exception):
    """The run cannot prove the state it needs; nothing more is sent."""


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, timeout=TIMEOUT)
    if completed.returncode:
        raise Refused(f"git {arguments[0]} exited {completed.returncode}")
    return completed.stdout


def ancestor(root: Path, older: str, newer: str) -> bool:
    completed = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", older, newer],
                               capture_output=True, timeout=TIMEOUT)
    if completed.returncode not in (0, 1):
        raise Refused(f"git merge-base exited {completed.returncode}")
    return completed.returncode == 0


def commit(root: Path, ref: str) -> str:
    value = git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").strip()
    if not SHA.fullmatch(value):
        raise Refused(f"{ref} does not resolve to a commit")
    return value


def main_now(root: Path) -> str:
    """The head of main fetched again, so an advance by another writer after the checkout is visible."""
    git(root, "fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main")
    return commit(root, "refs/remotes/origin/main")


def excluded(path: str) -> bool:
    return any(fnmatchcase(path, pattern) for pattern, _ in EXCLUDED)


def changed(root: Path, base: str, target: str) -> list[str]:
    """Paths added or modified from base to target, NUL-delimited so any file name stays literal."""
    output = git(root, "diff", "-z", "--name-only", "--no-renames", "--diff-filter=d", base, target)
    return [path for path in output.split("\0") if path]


def shown(path: str) -> str:
    """A path for logs: control characters and non-ASCII escaped, so a file name cannot forge output lines."""
    return path.encode("unicode_escape").decode("ascii") if not path.isprintable() or not path.isascii() else path


def tiden(cli: str, arguments: list[str], product: str) -> dict:
    """One Tiden CLI command; its stderr and a failing stdout are never shown, since they may echo request data."""
    label = " ".join(arguments[:2])
    try:
        completed = subprocess.run([cli, *arguments, "--product-id", product, "--format", "json"],
                                   capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Refused(f"tiden {label} timed out") from None
    if completed.returncode:
        raise Refused(f"tiden {label} exited {completed.returncode}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise Refused(f"tiden {label} returned malformed JSON") from None
    if not isinstance(value, dict):
        raise Refused(f"tiden {label} returned no object")
    return value


def watermark(cli: str, product: str) -> str:
    status = tiden(cli, ["drift", "status"], product)
    value = status.get("watermarkSha")
    if status.get("repository") != REPOSITORY or not isinstance(value, str) or not SHA.fullmatch(value):
        raise Refused("tiden drift status names no watermark commit of this repository")
    return value


def anchors(cli: str, product: str) -> set[str]:
    listing = tiden(cli, ["spec", "list", "--branch", "main", "--include-sources"], product)
    specs, pagination = listing.get("requirements"), listing.get("pagination")
    if (not isinstance(specs, list) or not isinstance(pagination, dict) or pagination.get("nextPageToken") != ""
            or type(pagination.get("totalCount")) is not int or pagination["totalCount"] != len(specs)):
        raise Refused("the spec listing is incomplete")
    paths, ids = set(), set()
    for spec in specs:
        if not isinstance(spec, dict) or not isinstance(spec.get("id"), str) or spec["id"] in ids:
            raise Refused("the spec listing has a missing or duplicate spec id")
        ids.add(spec["id"])
        # The JSON API omits zero counts and empty arrays; a nonempty array still needs its exact count.
        sources = spec.get("sources", [])
        if (not isinstance(sources, list) or not all(isinstance(source, dict) for source in sources)
                or spec.get("sourceCount", 0) != len(sources)):
            raise Refused("the spec listing has malformed or incomplete sources")
        paths.update(source["repoPath"] for source in sources
                     if source.get("sourceType") == "repo_file" and isinstance(source.get("repoPath"), str)
                     and (source.get("repository") or "") in ANCHOR_REPOSITORIES)
    return paths


def present(root: Path, sha: str) -> bool:
    return not subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"],
                              capture_output=True, timeout=TIMEOUT).returncode


def check(root: Path, cli: str, product: str, report) -> int:
    target = commit(root, "HEAD")  # the workflow checks out main as it is when the run starts
    base = watermark(cli, product)
    report(f"Target: main at {target}; watermark {base}.")
    if base == target:
        report("The watermark is at main already; nothing to advance.")
        return 0
    if not present(root, base):
        main_now(root)  # another writer may have advanced it to a merge newer than this checkout
        if not present(root, base):
            raise Refused("the watermark commit is not on main; reconcile by hand, the job never resets it")
    if not ancestor(root, base, target):
        if ancestor(root, target, base) and ancestor(root, base, main_now(root)):
            report("Another writer advanced the watermark past this target on main; nothing to advance.")
            return 0
        raise Refused("the watermark and main have diverged; reconcile by hand, the job never resets it")
    paths = [path for path in changed(root, base, target) if not excluded(path)]
    known = anchors(cli, product)
    missing = [path for path in paths if path not in known]
    if missing:
        report(f"{len(missing)} changed code file(s) since {base} have no Tiden source anchor:")
        report("\n".join(f"- {shown(path)}" for path in missing))
        report("Anchor each on the spec it implements on Tiden main, then rerun this run. The watermark stays.")
        return 1
    report(f"All {len(paths)} changed code file(s) are anchored; advancing {base} -> {target} (empty_sync).")
    try:
        answer = tiden(cli, ["drift", "advance", "--from", base, "--to", target, "--reason", "empty_sync",
                             "--repo", REPOSITORY], product)
        after = watermark(cli, product)
    except Refused as error:
        # The server may have taken the advance; a fresh run reads it again instead of retrying here.
        raise Refused(f"advance result unknown ({error}); rerun to read the watermark again") from None
    if answer.get("advanced") is not True:
        # CLI 0.33.0 answers a stale --from with exit 0 and "advanced": false (verified 2026-10-18).
        raise Refused(f"the server did not advance from {base} (now {after}); rerun to read the watermark again")
    if after != target and not ancestor(root, target, after):
        raise Refused(f"after the advance the watermark is {after}, not {target}")
    report(f"Watermark advanced to {target}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Require anchors for changed code, then advance the drift watermark.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--cli", default="tiden")
    args = parser.parse_args()
    summary = os.environ.get("GITHUB_STEP_SUMMARY")

    def report(line: str) -> None:
        print(line)
        if summary:
            with open(summary, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    try:
        return check(args.root.resolve(), args.cli, os.environ["TIDEN_PRODUCT_ID"], report)
    except Refused as error:
        report(f"Refused: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
