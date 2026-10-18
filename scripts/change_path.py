#!/usr/bin/env python3
"""Compute a pull request's change path from its diff: AUTO, AUTO+review or OWNER (AGENTS.md sections 2 and 3).

Lists the committed changes from the merge base of `--base` (default `origin/main`) to `--head` (default `HEAD`)
with `git diff --no-renames -z`, reads each changed file's blobs at both revisions with `git cat-file` (never a
patch, a diff driver or a textconv filter) and prints one line: the path, then each matching rule with every path
that matched it. The PR carries that line as `Change path: <line>`. AUTO is documentation and tests only;
AUTO+review is any other file; OWNER is a diff that the owner keeps. A doubt (content that cannot be read or
parsed) counts as OWNER. A change to this file is itself OWNER: it holds the rule table that decides merge authority.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
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
SECRET_FILES = re.compile(r"^((.*/)?\.gitleaks[^/]*|config/gitleaks/.*|\.github/secret_scanning\.ya?ml|deploy/private/.*|"
                          r"(.*/)?\.env)$")
# Secret bindings are compared in parsed CI and Compose YAML only: Python's `secrets` module is not one.
YAML_FILE = re.compile(r".*\.ya?ml$")
SECRET_EXPRESSION = re.compile(r"\bsecrets(\.\w+|\[[^\]]*\])")
ROLE_SQL = re.compile(r"(^|/)(\w+_role|roles)\.sql$")
MIGRATION = re.compile(r"(^|/)migrations/[^/]+\.sql$")
# Matched per statement after comments are removed and whitespace is collapsed, so a statement split over lines or
# around a comment still matches. In `.sql` files any case counts; in code only upper-case SQL, so prose such as
# "drop the cache" does not.
DESTRUCTIVE_SQL = re.compile(r"\b(DROP|TRUNCATE|RENAME)\b|\bDELETE FROM\b|"
                             r"\bALTER TABLE\b.*\bALTER (COLUMN )?\S+ (SET DATA )?TYPE\b|\bSET NOT NULL\b", re.IGNORECASE)
DESTRUCTIVE_SQL_IN_CODE = re.compile(r"\b(DROP [A-Z]+|TRUNCATE( TABLE)? \S|DELETE FROM|ALTER TABLE \S+ RENAME|"
                                     r"ALTER (COLUMN )?\S+ (SET DATA )?TYPE|SET NOT NULL)\b")
SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
CODE_SUFFIXES = {".py", ".go", ".js", ".mjs", ".cjs", ".ts", ".sh"}
AGENT_SETTINGS = ".claude/settings.json"
# An agent permission may be added to `allow` without the owner only when it is read-only by name.
READ_ONLY_TOOL = re.compile(r"^mcp__\w+?__(?:(?:messages|calls|calendar)_)?(get|list|search|resolve|preview)(_\w+)?$"
                            r"|^mcp__\w+?__\w*capabilities$|^(Read|Grep|Glob|LS|WebSearch)(\(.*\))?$")
OWNER_FILES = {"scripts/change_path.py"}
OWNER_LIST = ("AGENTS.md", re.compile(r"^## .*\bowner keeps\b", re.IGNORECASE | re.MULTILINE))
DOC_SUFFIXES = {".md", ".mdx", ".rst"}
LEVELS = {"none": 0, "read": 1, "write": 2}
UNREADABLE = (ValueError, yaml.YAMLError, AttributeError, TypeError, RecursionError)


class Loader(yaml.SafeLoader):
    """A safe loader that reads Compose's `!override`/`!reset` and any other tag as the plain node under it."""


Loader.add_multi_constructor("!", lambda loader, suffix, node: (
    loader.construct_mapping(node) if isinstance(node, yaml.MappingNode) else
    loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode) else loader.construct_scalar(node)))


def load(text: str | None):
    return yaml.load(text or "", Loader=Loader)  # noqa: S506 - a SafeLoader subclass


@dataclass
class Change:
    path: str
    status: str
    before: str | None
    after: str | None


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True).stdout


def show(root: Path, revision: str, path: str) -> str | None:
    """The blob at a revision as text (undecodable bytes replaced), or None when it does not exist there."""
    result = subprocess.run(["git", "-C", str(root), "cat-file", "blob", f"{revision}:{path}"], capture_output=True)
    return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else None


def changes(root: Path, base: str, head: str) -> list[Change]:
    listing = git(root, "diff", "--no-renames", "--no-ext-diff", "--name-status", "-z", base, head)
    tokens = listing.rstrip("\0").split("\0") if listing else []
    return [Change(path, status, show(root, base, path), show(root, head, path))
            for status, path in zip(tokens[::2], tokens[1::2])]


def is_test(path: str) -> bool:
    pure = PurePosixPath(path)
    return (bool({"tests", "test", "testdata", "__tests__"} & set(pure.parts[:-1]))
            or re.match(r"(test_.*\.py|.*_test\.(py|go)|conftest\.py|.*\.test\.[cm]?[jt]s)$", pure.name) is not None)


def is_doc(path: str) -> bool:
    pure = PurePosixPath(path)
    return pure.suffix.lower() in DOC_SUFFIXES or re.match(r"(LICENSE|NOTICE|COPYING)", pure.name) is not None


def destructive_statements(text: str | None, sql: bool) -> Counter:
    """Destructive SQL statements of a file, comments removed and whitespace collapsed, counted."""
    if not text:
        return Counter()
    if sql:
        statements = (" ".join(part.split()) for part in SQL_COMMENT.sub(" ", text).split(";"))
        return Counter(statement for statement in statements if DESTRUCTIVE_SQL.search(statement))
    return Counter(match.group(0) for match in DESTRUCTIVE_SQL_IN_CODE.finditer(" ".join(text.split())))


def secret_bindings(text: str | None) -> Counter:
    """Every secret binding of a YAML document: each child of a `secrets` key and each `secrets.X` expression, with
    the mapping keys above it (list positions left out, so moving an unchanged step is not a new binding)."""
    found: Counter = Counter()
    if not text:
        return found

    def walk(node, where: tuple):
        if isinstance(node, dict):
            for key, value in node.items():
                name = str(key).strip().strip("'\"")
                if name == "secrets":
                    children = value if isinstance(value, list) else list(value) if isinstance(value, dict) else [value]
                    found.update(json.dumps([*where, "secrets", str(child)]) for child in children)
                walk(value, (*where, name))
        elif isinstance(node, list):
            for value in node:
                walk(value, where)
        elif isinstance(node, str):
            found.update(json.dumps([*where, match.group(0)]) for match in SECRET_EXPRESSION.finditer("".join(node.split())))
    for document in yaml.load_all(text, Loader=Loader):  # noqa: S506 - a SafeLoader subclass
        walk(document, ())
    return found


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
    """Effective permissions per job of a workflow."""
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
    """How many jobs report each required check name. Only workflows with a trigger other than `workflow_call`
    report checks; a local reusable workflow call reports `<caller> / <called job>` for each leaf job, recursively."""
    paths = [path for path in git(root, "ls-tree", "-r", "--name-only", revision, ".github/workflows").splitlines()
             if path.endswith((".yml", ".yaml"))]
    documents = {path: load(show(root, revision, path)) or {} for path in paths}

    def leaves(document: dict, prefix: str, seen: tuple) -> list[str]:
        labels = []
        for name, job in (document.get("jobs") or {}).items():
            job = job if isinstance(job, dict) else {}
            label = f"{prefix}{job.get('name', name)}"
            called = str(job.get("uses", ""))
            if called.startswith("./.github/workflows/"):
                target = called[2:].split("@")[0]
                if target in seen or target not in documents:
                    raise ValueError(f"unresolved reusable workflow {called}")
                labels += leaves(documents[target], f"{label} / ", (*seen, target))
            else:
                labels.append(label)
        return labels

    labels: Counter = Counter()
    for path, document in documents.items():
        triggers = document.get("on", document.get(True))
        names = set(triggers) if isinstance(triggers, (dict, list)) else {triggers}
        if names - {"workflow_call"}:
            labels.update(leaves(document, "", (path,)))
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


def owner_rules(change: Change) -> list[tuple[str, str]]:
    """The OWNER rules one changed file matches, as (rule, path with detail)."""
    path, found = change.path, []
    if path in RULESET_FILES:
        found.append(("ruleset", path))
    if SECRET_FILES.match(path):
        found.append(("secrets", path))
    if YAML_FILE.match(path) and not is_test(path) and secret_bindings(change.after) - secret_bindings(change.before):
        found.append(("secrets", path))
    if WORKFLOWS.match(path) and change.after is not None:
        old = job_permissions(load(change.before) or {}) if change.before is not None else {}
        new = job_permissions(load(change.after) or {})
        if any(widened(old.get(job), levels) for job, levels in new.items()):
            found.append(("permissions", path))
    if ROLE_SQL.search(path):
        found.append(("access", path))
    if path == AGENT_SETTINGS:
        found += [("agent-permissions", f"{path} ({entry})") for entry in allow_widening(change.before, change.after)]
    if MIGRATION.search(path) and change.before is not None:
        found.append(("destructive-sql", f"{path} (changes an existing migration)"))
    suffix = PurePosixPath(path).suffix.lower()
    if not is_test(path) and (suffix == ".sql" or suffix in CODE_SUFFIXES):
        sql = suffix == ".sql"
        if destructive_statements(change.after, sql) - destructive_statements(change.before, sql):
            found.append(("destructive-sql", path))
    if path in OWNER_FILES:
        found.append(("owner-rules", path))
    if path == OWNER_LIST[0]:
        before, after = owner_list(change.before), owner_list(change.after)
        if before != after or before is None:
            found.append(("owner-rules", f"{path} (the owner list)"))
    return found


def classify(root: Path, base: str = "origin/main", head: str = "HEAD") -> str:
    merge_base = git(root, "merge-base", base, head).strip()
    found = changes(root, merge_base, head)
    owner: dict[str, list[str]] = {rule: [] for rule in RULES}
    review: list[str] = []
    for change in found:
        try:
            for rule, detail in owner_rules(change):
                owner[rule].append(detail)
        except UNREADABLE as error:
            owner["owner-rules"].append(f"{change.path} (cannot be read: {type(error).__name__})")
        if is_test(change.path) and change.after is None:
            review.append(f"{change.path} (removes or moves a test)")
        elif not is_test(change.path) and not is_doc(change.path):
            review.append(change.path)
    try:
        required = required_checks(root, merge_base) | required_checks(root, head)
        if required and any(change.path.startswith(".github/workflows/") for change in found):
            if check_labels(root, merge_base, required) != check_labels(root, head, required):
                owner["ruleset"].append(".github/workflows (renames, removes or duplicates a required check)")
    except UNREADABLE as error:
        owner["ruleset"].append(f"required checks (cannot be read: {type(error).__name__})")
    matched = {rule: paths for rule, paths in owner.items() if paths}
    if matched:
        return "OWNER — " + "; ".join(f"{rule}: {listed(paths)}" for rule, paths in matched.items())
    if review:
        return "AUTO+review — code: " + listed(review)
    return "AUTO — documentation and tests only" if found else "AUTO — no changes"


def listed(paths: list[str]) -> str:
    """Every path once, with control characters escaped, so the result stays one line."""
    return ", ".join("".join(c if c.isprintable() else repr(c)[1:-1] for c in path) for path in dict.fromkeys(paths))


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
