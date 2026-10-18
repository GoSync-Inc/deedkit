#!/usr/bin/env python3
"""Compute a pull request's change path from its diff: AUTO, AUTO+review or OWNER (AGENTS.md sections 2 and 3).

Lists the committed changes from the merge base of `--base` (default `origin/main`) to `--head` (default `HEAD`)
with `git diff --no-renames -z`, reads each changed file's blobs at both revisions with `git cat-file` (never a
patch, a diff driver or a textconv filter) and prints one line: the path, then each matching rule with every path
that matched it. The PR carries that line as `Change path: <line>`. AUTO is documentation and tests only;
AUTO+review is any other file; OWNER is a diff that the owner keeps. It fails closed: a doubt (a blob or content
that cannot be read or parsed, a path with unusual characters, any error while classifying) counts as OWNER, since a
false OWNER costs one owner look. A change to this file is itself OWNER: it holds the rule table that decides merge
authority.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import os
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
    "doubt": "a change it cannot classify with certainty",
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
# around a comment still matches. In `.sql` files any case counts; in code upper-case SQL and lower-case forms that
# name an SQL object, so prose such as "drop the cache" does not. Any change to these statements, added, edited or
# removed, is OWNER.
DESTRUCTIVE_SQL = re.compile(r"\b(DROP|TRUNCATE|RENAME)\b|\bDELETE FROM\b|"
                             r"\bALTER TABLE\b.*\bALTER (COLUMN )?\S+ (SET DATA )?TYPE\b|\bSET NOT NULL\b", re.IGNORECASE)
DESTRUCTIVE_SQL_IN_CODE = re.compile(
    r"\b(DROP\s+[A-Z]+|TRUNCATE(\s+TABLE)?\s+\S|ALTER(\s+COLUMN)?\s+\S+\s+(SET\s+DATA\s+)?TYPE|(?i:delete\s+from|"
    r"drop\s+(table|schema|database|index|view|materialized\s+view|sequence|column|constraint|function|procedure|"
    r"trigger|domain|role|extension|policy|publication|subscription)|truncate\s+table|alter\s+table\s+\S+\s+rename|"
    r"alter\s+column\s+\S+\s+(set\s+data\s+)?type|set\s+not\s+null))\b")
DOLLAR_QUOTE = re.compile(r"\$([A-Za-z_]\w*)?\$")
QUOTES = "\"'`"
# A string literal joined to the previous one: `+`, a line continuation or nothing between, then a prefixed quote.
JOINED_LITERAL = re.compile(r"\s*\+?\s*(\\\s*)?[A-Za-z]{0,2}([\"'`])")
CODE_SUFFIXES = {".py", ".go", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".sh"}
AGENT_SETTINGS = ".claude/settings.json"
# An agent permission may be added to `allow` without the owner only when it is read-only by name.
READ_ONLY_TOOL = re.compile(r"^mcp__\w+?__(?:(?:messages|calls|calendar)_)?(get|list|search|resolve|preview)(_\w+)?$"
                            r"|^mcp__\w+?__\w*capabilities$|^(Read|Grep|Glob|LS|WebSearch)(\(.*\))?$")
OWNER_FILES = {"scripts/change_path.py"}
OWNER_LIST = ("AGENTS.md", re.compile(r"^## .*\bowner keeps\b", re.IGNORECASE | re.MULTILINE))
DOC_SUFFIXES = {".md", ".mdx", ".rst"}
LEVELS = {"none": 0, "read": 1, "write": 2}
# A required job's structure is everything but its steps, display name and permissions (other rules cover those).
STRUCTURE_SKIP = {"steps", "name", "permissions", "jobs"}


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
    doubt: str | None = None


def git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True).stdout


def fields(root: Path, *arguments: str) -> list[str]:
    """The NUL-delimited output of a git command, each field decoded byte for byte (no newline translation)."""
    listing = git(root, *arguments)
    return [os.fsdecode(field) for field in listing.rstrip(b"\0").split(b"\0")] if listing else []


def show(root: Path, revision: str, path: str) -> str:
    """The blob at a revision as text (undecodable bytes replaced); raises when git cannot read it."""
    return git(root, "cat-file", "blob", f"{revision}:{path}").decode("utf-8", "replace")


def changes(root: Path, base: str, head: str) -> list[Change]:
    """Every changed path with its blobs; a blob the diff status expects but git cannot read leaves a doubt."""
    tokens, found = fields(root, "diff", "--no-renames", "--no-ext-diff", "--name-status", "-z", base, head), []
    for status, path in zip(tokens[::2], tokens[1::2]):
        try:
            found.append(Change(path, status, None if status == "A" else show(root, base, path),
                                None if status == "D" else show(root, head, path)))
        except subprocess.CalledProcessError:
            found.append(Change(path, status, None, None, "a blob cannot be read"))
    return found


def is_test(path: str) -> bool:
    pure = PurePosixPath(path)
    return (bool({"tests", "test", "testdata", "__tests__"} & set(pure.parts[:-1]))
            or re.match(r"(test_.*\.py|.*_test\.(py|go)|conftest\.py|.*\.test\.[cm]?[jt]s)$", pure.name) is not None)


def is_doc(path: str) -> bool:
    pure = PurePosixPath(path)
    return pure.suffix.lower() in DOC_SUFFIXES or re.match(r"(LICENSE|NOTICE|COPYING)", pure.name) is not None


def sql_statements(text: str) -> list[str]:
    """The statements of a SQL file, whitespace collapsed: comments (nested block comments too) removed, split at
    semicolons outside string literals, quoted identifiers and dollar quotes. An unterminated one runs to the end."""
    statements, current, at, size = [], [], 0, len(text)
    while at < size:
        char = text[at]
        if text.startswith("--", at):
            end = text.find("\n", at)
            at = size if end < 0 else end
            current.append(" ")
        elif text.startswith("/*", at):
            depth, at = 1, at + 2
            while at < size and depth:
                pair = text[at:at + 2]
                depth += {"/*": 1, "*/": -1}.get(pair, 0)
                at += 2 if pair in ("/*", "*/") else 1
            current.append(" ")
        elif char in "'\"":
            escapes = char == "'" and text[at - 1:at] in ("e", "E") and not re.match(r"\w", text[at - 2:at - 1] or " ")
            end = at + 1
            while end < size:
                if escapes and text[end] == "\\" or text.startswith(char * 2, end):
                    end += 2
                elif text[end] == char:
                    break
                else:
                    end += 1
            current.append(text[at:end + 1])
            at = end + 1
        elif char == "$" and (tag := DOLLAR_QUOTE.match(text, at)):
            end = text.find(tag.group(0), tag.end())
            end = size if end < 0 else end + len(tag.group(0))
            current.append(text[at:end])
            at = end
        elif char == ";":
            statements.append("".join(current))
            current, at = [], at + 1
        else:
            current.append(char)
            at += 1
    statements.append("".join(current))
    return [" ".join(statement.split()) for statement in statements if statement.strip()]


def code_statements(text: str) -> list[str]:
    """Each upper-case destructive SQL match in code with the rest of its string literal (and of literals joined to
    it) and of that source line, whitespace collapsed, so a changed target or predicate is a new statement."""
    found = []
    for match in DESTRUCTIVE_SQL_IN_CODE.finditer(text):
        quote, end = text[:match.start()].rstrip()[-1:], match.end()
        if quote and quote in QUOTES:
            while (close := text.find(quote, end)) >= 0:
                end = close + 1
                joined = JOINED_LITERAL.match(text, end)
                if not joined:
                    break
                quote, end = joined.group(2), joined.end()
            else:
                end = len(text)
            stops = (text.find("\n", end),)
        else:
            stops = (text.find(";", end), text.find("\n", end))
        end = min([stop for stop in stops if stop >= 0] or [len(text)])
        found.append(" ".join(text[match.start():end].split()))
    return found


def destructive_statements(text: str | None, sql: bool) -> Counter:
    """Destructive SQL statements of a file, counted: whole statements in `.sql`, statements in string literals in
    code."""
    if not text:
        return Counter()
    if sql:
        return Counter(statement for statement in sql_statements(text) if DESTRUCTIVE_SQL.search(statement))
    return Counter(code_statements(text))


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


def canonical(node):
    """A parsed YAML node with string keys and JSON scalars, so it can be compared as sorted JSON."""
    if isinstance(node, dict):
        return {str(key): canonical(value) for key, value in node.items()}
    if isinstance(node, list):
        return [canonical(value) for value in node]
    return node if isinstance(node, (str, int, float, bool, type(None))) else str(node)


def structure(path: str, node: dict) -> list:
    return [path, canonical({key: value for key, value in node.items() if str(key) not in STRUCTURE_SKIP})]


def check_labels(root: Path, revision: str, required: set[str]) -> Counter:
    """Each job that reports a required check name, as (name, structure): the workflows and jobs on its way without
    their steps, display names and permissions. Only workflows with a trigger other than `workflow_call` report
    checks; a local reusable workflow call reports `<caller> / <called job>` for each leaf job, recursively. A remote
    call is a doubt when a required check starts with its `<caller> / ` prefix."""
    paths = [path for path in fields(root, "ls-tree", "-r", "-z", "--name-only", revision, "--", ".github/workflows")
             if path.endswith((".yml", ".yaml"))]
    documents = {path: load(show(root, revision, path)) or {} for path in paths}

    def leaves(path: str, prefix: str, seen: tuple, way: tuple) -> list[tuple[str, tuple]]:
        document, labels = documents[path], []
        for name, job in (document.get("jobs") or {}).items():
            if not isinstance(job, dict):
                raise ValueError(f"job {name} is not a mapping")
            label, called = f"{prefix}{job.get('name', name)}", str(job.get("uses", ""))
            here = (*way, structure(path, document), structure(str(name), job))
            if called.startswith("./.github/workflows/"):
                target = called[2:].split("@")[0]
                if target in seen or target not in documents:
                    raise ValueError(f"unresolved reusable workflow {called}")
                labels += leaves(target, f"{label} / ", (*seen, target), here)
            elif called:
                # A remote reusable workflow reports `<label> / <its job>`, which cannot be resolved here.
                if any(check.startswith(f"{label} / ") for check in required):
                    raise ValueError(f"unresolved reusable workflow {called}")
            else:
                labels.append((label, here))
        return labels

    found: Counter = Counter()
    for path, document in documents.items():
        triggers = document.get("on", document.get(True))
        names = set(triggers) if isinstance(triggers, (dict, list)) else {triggers}
        if names - {"workflow_call"}:
            found.update((label, json.dumps(way, sort_keys=True)) for label, way in leaves(path, "", (path,), ())
                         if label in required)
    return found


def required_checks(root: Path, revision: str) -> set[str]:
    if not fields(root, "ls-tree", "-z", "--name-only", revision, "--", "deploy/main-ruleset.json"):
        return set()
    text = show(root, revision, "deploy/main-ruleset.json")
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
        entries = {key: block.get(key) or [] for key in ("allow", "deny", "ask")}
        if not all(isinstance(value, list) and all(isinstance(entry, str) for entry in value)
                   for value in entries.values()):
            raise ValueError("permission entries are not lists of strings")
        return ({key: set(value) for key, value in entries.items()}, block.get("defaultMode"))
    (old, old_mode), (new, new_mode) = lists(before), lists(after)
    found = [f"allow {entry}" for entry in sorted(new["allow"] - old["allow"]) if not READ_ONLY_TOOL.match(entry)]
    found += [f"drops {key} {entry}" for key in ("deny", "ask") for entry in sorted(old[key] - new[key])]
    return found + ([f"defaultMode {new_mode}"] if new_mode != old_mode else [])


def owner_list(text: str | None) -> str | None:
    """Every "owner keeps" section of AGENTS.md, joined, or None without one."""
    if text is None:
        return None
    sections = []
    for heading in OWNER_LIST[1].finditer(text):
        rest = text[heading.end():]
        end = re.search(r"^## ", rest, re.MULTILINE)
        sections.append(text[heading.start():heading.end() + (end.start() if end else len(rest))])
    return "\n".join(sections) or None


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
        if destructive_statements(change.after, sql) != destructive_statements(change.before, sql):
            found.append(("destructive-sql", path))
    if path in OWNER_FILES:
        found.append(("owner-rules", path))
    if path == OWNER_LIST[0]:
        before, after = owner_list(change.before), owner_list(change.after)
        if before != after or before is None:
            found.append(("owner-rules", f"{path} (the owner list)"))
    return found


def classify(root: Path, base: str = "origin/main", head: str = "HEAD") -> str:
    merge_base = git(root, "merge-base", base, head).decode().strip()
    found = changes(root, merge_base, head)
    owner: dict[str, list[str]] = {rule: [] for rule in RULES}
    review: list[str] = []
    for change in found:
        if not change.path.isprintable():
            owner["doubt"].append(f"{change.path} (unusual characters in the path)")
        if change.doubt:
            owner["doubt"].append(f"{change.path} ({change.doubt})")
        else:
            try:
                for rule, detail in owner_rules(change):
                    owner[rule].append(detail)
            except Exception as error:  # noqa: BLE001 - any failure to classify is a doubt, which is OWNER
                owner["doubt"].append(f"{change.path} (cannot be read: {type(error).__name__})")
        if is_test(change.path) and change.after is None:
            review.append(f"{change.path} (removes or moves a test)")
        elif not is_test(change.path) and not is_doc(change.path):
            review.append(change.path)
    try:
        required = required_checks(root, merge_base) | required_checks(root, head)
        if required and any(change.path.startswith(".github/workflows/") for change in found):
            before, after = check_labels(root, merge_base, required), check_labels(root, head, required)
            if Counter(label for label, _ in before.elements()) != Counter(label for label, _ in after.elements()):
                owner["ruleset"].append(".github/workflows (renames, removes or duplicates a required check)")
            elif before != after:
                owner["ruleset"].append(".github/workflows (changes the structure of a required check job)")
    except Exception as error:  # noqa: BLE001 - any failure to classify is a doubt, which is OWNER
        owner["ruleset"].append(f"required checks (cannot be read: {type(error).__name__})")
    matched = {rule: paths for rule, paths in owner.items() if paths}
    if matched:
        return "OWNER — " + "; ".join(f"{rule}: {listed(paths)}" for rule, paths in matched.items())
    if review:
        return "AUTO+review — code: " + listed(review)
    return "AUTO — documentation and tests only" if found else "AUTO — no changes"


def listed(paths: list[str]) -> str:
    """Every path once, with control characters escaped, so the result stays one line."""
    return ", ".join(escaped(path) for path in dict.fromkeys(paths))


def escaped(text: str) -> str:
    return "".join(char if char.isprintable() else repr(char)[1:-1] for char in text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a pull request's change path: AUTO, AUTO+review or OWNER.")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    arguments = parser.parse_args(argv)
    try:
        print(classify(arguments.repo, arguments.base, arguments.head))
    except subprocess.CalledProcessError as error:
        command, reason = escaped(" ".join(error.cmd[3:])), escaped(os.fsdecode(error.stderr.strip()))
        print(f"change_path: git failed: {command}: {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
