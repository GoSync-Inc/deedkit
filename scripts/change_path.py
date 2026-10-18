#!/usr/bin/env python3
"""Compute a pull request's change path from its diff: AUTO, AUTO+review or OWNER (AGENTS.md sections 2 and 3).

Reads `git diff --no-renames` from the merge base of `--base` (default `origin/main`) to `--head` (default `HEAD`),
committed changes only, and prints one line: the path, then each matching rule with the paths that matched it.
The PR carries that line as `Change path: <line>`. AUTO is documentation and tests only; AUTO+review is any other
file; OWNER is a diff that the owner keeps. A doubt (a file that cannot be read or parsed) counts as OWNER.
A change to this file is itself OWNER: it holds the rule table that decides merge authority.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

import yaml

# The OWNER rules, in the order they are reported (AGENTS.md section 2, items 1-4).
RULES = {
    "ruleset": "branch protection or required checks",
    "secrets": "a secret reference or secret-scanning configuration",
    "permissions": "widened workflow token permissions",
    "access": "database role grants",
    "agent-permissions": "agent permissions that allow a send, call or write tool",
    "destructive-sql": "destructive SQL or a changed migration",
    "owner-rules": "the owner list or the change path rules",
}
RULESET_FILES = {"deploy/main-ruleset.json", "deploy/main-protection.json"}
WORKFLOWS = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
SECRET_FILES = re.compile(r"^(\.gitleaks[^/]*\.toml|config/gitleaks/.*|deploy/private/.*|(.*/)?\.env)$")
# Only in CI and Compose YAML: Python's `secrets` module is not a secret reference.
SECRET_REFERENCE = re.compile(r"\bsecrets\s*(\.\s*\w|\[|:)")
YAML = re.compile(r"^\.github/.*|.*\.ya?ml$")
ROLE_SQL = re.compile(r"(^|/)(\w+_role|roles)\.sql$")
MIGRATION = re.compile(r"(^|/)migrations/[^/]+\.sql$")
DESTRUCTIVE_SQL = re.compile(r"\b(DROP|TRUNCATE|RENAME)\b|\bDELETE\s+FROM\b|\bALTER\s+COLUMN\b.*\b(TYPE|SET\s+NOT\s+NULL)\b",
                             re.IGNORECASE)
# In code, only upper-case SQL counts, so prose such as "drop the cache" does not.
DESTRUCTIVE_SQL_IN_CODE = re.compile(r"\b(DROP\s+(TABLE|SCHEMA|DATABASE|COLUMN|INDEX|VIEW|FUNCTION|TYPE|ROLE|OWNED)|"
                                     r"TRUNCATE\s+(TABLE\s+)?\w|DELETE\s+FROM|ALTER\s+TABLE\s+\S+\s+RENAME)\b")
CODE_SUFFIXES = {".py", ".go", ".js", ".mjs", ".cjs", ".ts", ".sh"}
AGENT_SETTINGS = ".claude/settings.json"
# An agent permission may be added to `allow` without the owner only when it is read-only by name.
READ_ONLY_TOOL = re.compile(r"^mcp__\w+?__(?:(?:messages|calls|calendar)_)?(get|list|search|resolve|preview)(_\w+)?$"
                            r"|^mcp__\w+?__\w*capabilities$|^(Read|Grep|Glob|LS|WebSearch)(\(.*\))?$")
OWNER_FILES = {"scripts/change_path.py"}
OWNER_LIST = ("AGENTS.md", re.compile(r"^## .*\bowner keeps\b", re.IGNORECASE | re.MULTILINE))
DOC_SUFFIXES = {".md", ".mdx", ".rst"}
LEVELS = {"none": 0, "read": 1, "write": 2}


@dataclass
class Change:
    path: str
    status: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True).stdout


def show(root: Path, revision: str, path: str) -> str | None:
    """The file at a revision, or None when it does not exist there."""
    result = subprocess.run(["git", "-C", str(root), "show", f"{revision}:{path}"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def changes(root: Path, base: str, head: str) -> list[Change]:
    listing = git(root, "-c", "core.quotePath=false", "diff", "--no-renames", "--name-status", "-z", base, head)
    tokens = listing.rstrip("\0").split("\0") if listing else []
    found = {path: Change(path, status) for status, path in zip(tokens[::2], tokens[1::2])}
    current, header = None, False
    diff = git(root, "-c", "core.quotePath=false", "diff", "--no-renames", "--no-color", "--no-ext-diff", "-U0", base, head)
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current, header = None, True
        elif header and line.startswith(("--- a/", "+++ b/")):
            current = found.get(line[6:], current)
        elif line.startswith("@@"):
            header = False
        elif not header and current and line[:1] == "+":
            current.added.append(line[1:])
        elif not header and current and line[:1] == "-":
            current.removed.append(line[1:])
    return list(found.values())


def is_test(path: str) -> bool:
    pure = PurePosixPath(path)
    return (bool({"tests", "test", "testdata", "__tests__"} & set(pure.parts[:-1]))
            or re.match(r"(test_.*\.py|.*_test\.(py|go)|conftest\.py|.*\.test\.[cm]?[jt]s)$", pure.name) is not None)


def is_doc(path: str) -> bool:
    pure = PurePosixPath(path)
    return pure.suffix.lower() in DOC_SUFFIXES or re.match(r"(LICENSE|NOTICE|COPYING)", pure.name) is not None


def permissions(value) -> dict[str, int]:
    """Token permissions as levels per scope, `*` for every unnamed scope; no block means the full default token."""
    if value is None or value == "write-all":
        return {"*": 2}
    if value == "read-all":
        return {"*": 1}
    if isinstance(value, dict):
        return {"*": 0, **{str(scope): LEVELS.get(str(level), 2) for scope, level in value.items()}}
    return {"*": 2}


def job_permissions(document) -> dict[str, dict[str, int]]:
    """Effective permissions per job of a workflow, or of an action file as one job."""
    if not isinstance(document, dict):
        raise ValueError("not a mapping")
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        return {}
    top = document.get("permissions")
    return {str(name): permissions(job.get("permissions", top) if isinstance(job, dict) else top)
            for name, job in jobs.items()}


def widened(before: dict[str, int] | None, after: dict[str, int]) -> bool:
    if before is None:
        return any(level == 2 for level in after.values())
    scopes = set(before) | set(after)
    return any(after.get(scope, after["*"]) > before.get(scope, before["*"]) for scope in scopes)


def check_labels(root: Path, revision: str, required: set[str]) -> Counter:
    """How many workflow jobs report each required check name (`<job>` or `<caller> / <called job>`)."""
    paths = [path for path in git(root, "ls-tree", "-r", "--name-only", revision, ".github/workflows").splitlines()
             if path.endswith((".yml", ".yaml"))]
    documents = {path: yaml.safe_load(show(root, revision, path) or "") or {} for path in paths}
    labels: Counter = Counter()
    for document in documents.values():
        for name, job in (document.get("jobs") or {}).items():
            label = str(job.get("name", name)) if isinstance(job, dict) else str(name)
            labels[label] += 1
            called = job.get("uses", "") if isinstance(job, dict) else ""
            if called.startswith("./.github/workflows/"):
                for inner, inner_job in ((documents.get(called[2:]) or {}).get("jobs") or {}).items():
                    inner_label = inner_job.get("name", inner) if isinstance(inner_job, dict) else inner
                    labels[f"{label} / {inner_label}"] += 1
    return Counter({label: count for label, count in labels.items() if label in required})


def required_checks(root: Path, revision: str) -> set[str]:
    text = show(root, revision, "deploy/main-ruleset.json")
    if text is None:
        return set()
    contexts: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("context"), str):
                contexts.add(node["context"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(json.loads(text))
    return contexts


def allow_widening(before: str | None, after: str | None) -> list[str]:
    """Added `allow` entries that are not read-only, removed `deny` or `ask` entries and a changed default mode."""
    def lists(text):
        block = (json.loads(text) if text else {}).get("permissions") or {}
        return ({key: set(block.get(key) or []) for key in ("allow", "deny", "ask")}, block.get("defaultMode"))
    (old, old_mode), (new, new_mode) = lists(before), lists(after)
    found = [f"allow {entry}" for entry in sorted(new["allow"] - old["allow"]) if not READ_ONLY_TOOL.match(entry)]
    found += [f"drops {key} {entry}" for key in ("deny", "ask") for entry in sorted(old[key] - new[key])]
    return found + ([f"defaultMode {new_mode}"] if new_mode != old_mode else [])


def owner_list(text: str | None) -> str | None:
    if text is None:
        return None
    heading = OWNER_LIST[1].search(text)
    if not heading:
        return None
    rest = text[heading.end():]
    end = re.search(r"^## ", rest, re.MULTILINE)
    return text[heading.start():heading.end() + (end.start() if end else len(rest))]


def classify(root: Path, base: str = "origin/main", head: str = "HEAD") -> str:
    merge_base = git(root, "merge-base", base, head).strip()
    found = changes(root, merge_base, head)
    owner: dict[str, list[str]] = {rule: [] for rule in RULES}
    review: list[str] = []
    for change in found:
        path = change.path
        try:
            if path in RULESET_FILES:
                owner["ruleset"].append(path)
            if SECRET_FILES.match(path):
                owner["secrets"].append(path)
            if YAML.match(path) and not is_test(path) and any(SECRET_REFERENCE.search(line) for line in change.added):
                owner["secrets"].append(path)
            if WORKFLOWS.match(path) and change.status != "D":
                before, after = show(root, merge_base, path), show(root, head, path)
                old = job_permissions(yaml.safe_load(before) or {}) if before is not None else {}
                new = job_permissions(yaml.safe_load(after or "") or {})
                if any(widened(old.get(job), levels) for job, levels in new.items()):
                    owner["permissions"].append(path)
            if ROLE_SQL.search(path):
                owner["access"].append(path)
            if path == AGENT_SETTINGS:
                owner["agent-permissions"] += [f"{path} ({entry})" for entry in
                                               allow_widening(show(root, merge_base, path), show(root, head, path))]
            if MIGRATION.search(path) and change.status != "A":
                owner["destructive-sql"].append(f"{path} (changes an existing migration)")
            if not is_test(path):
                pattern = (DESTRUCTIVE_SQL if path.endswith(".sql") else
                           DESTRUCTIVE_SQL_IN_CODE if PurePosixPath(path).suffix in CODE_SUFFIXES else None)
                if pattern and any(pattern.search(line) for line in change.added):
                    owner["destructive-sql"].append(path)
            if path in OWNER_FILES:
                owner["owner-rules"].append(path)
            if path == OWNER_LIST[0]:
                before, after = owner_list(show(root, merge_base, path)), owner_list(show(root, head, path))
                if before != after or before is None:
                    owner["owner-rules"].append(f"{path} (the owner list)")
        except (ValueError, yaml.YAMLError, AttributeError, TypeError) as error:
            owner["owner-rules"].append(f"{path} (cannot be read: {type(error).__name__})")
        if is_test(path) and change.status == "D":
            review.append(f"{path} (removes or moves a test)")
        elif not is_test(path) and not is_doc(path):
            review.append(path)
    required = required_checks(root, merge_base) | required_checks(root, head)
    if required and any(change.path.startswith(".github/workflows/") for change in found):
        try:
            if check_labels(root, merge_base, required) != check_labels(root, head, required):
                owner["ruleset"].append(".github/workflows (renames, removes or duplicates a required check)")
        except (ValueError, yaml.YAMLError, AttributeError, TypeError) as error:
            owner["ruleset"].append(f".github/workflows (cannot be read: {type(error).__name__})")
    matched = {rule: paths for rule, paths in owner.items() if paths}
    if matched:
        return "OWNER — " + "; ".join(f"{rule}: {summary(paths)}" for rule, paths in matched.items())
    if review:
        return "AUTO+review — code: " + summary(review)
    return "AUTO — documentation and tests only" if found else "AUTO — no changes"


def summary(paths: list[str], limit: int = 3) -> str:
    unique = list(dict.fromkeys(paths))
    return ", ".join(unique[:limit]) + (f" (+{len(unique) - limit} more)" if len(unique) > limit else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a pull request's change path: AUTO, AUTO+review or OWNER.")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    arguments = parser.parse_args(argv)
    try:
        print(classify(arguments.repo, arguments.base, arguments.head))
    except subprocess.CalledProcessError as error:
        print(f"change_path: git failed: {' '.join(error.cmd[3:])}: {error.stderr.strip()}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
