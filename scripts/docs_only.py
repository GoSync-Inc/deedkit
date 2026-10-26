#!/usr/bin/env python3
"""Decide whether a pull request or merge queue run changes only documentation (ANYM-323, glob rule).

Such a run skips the test jobs; every other run, and any doubt, verifies in full. A path qualifies when it is a
Markdown file or lies under `.claude/`, and none of the deny rules matches it: the only Markdown that code reads,
packages or ships (`README.md` anywhere, `SETUP.md`, `THIRD_PARTY_NOTICES.md`, `TELEMETRY.md`), license texts and
everything under the module trees. A pull request run classifies GitHub's two-parent merge of its head against the
base; a merge queue run classifies its group commit against its first parent, the group's base. The main push run
of a docs-only group finds no queue run that verified it, so it verifies in full before anything is published or
promoted (ANYM-321).
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

# Code reads these (tests/test_telemetry.py, scripts/check_release.py) or packages and images ship them; CI installs
# the Tiden CLI from the version and checksum in docs/harness/tiden.md.
DENIED_FILES = frozenset({"SETUP.md", "THIRD_PARTY_NOTICES.md", "TELEMETRY.md", "docs/harness/tiden.md"})
DENIED_NAMES = frozenset({"README.md"})
DENIED_NAME_PREFIXES = ("LICENSE", "COPYING")
# Module trees package their own documents (the calendar sdist, the npm client) and their tests may read them.
DENIED_ROOTS = frozenset({"licenses", "services", "packages", "whatsapp"})
INTENT_LINE = re.compile(r"Tiden intent branch:", re.IGNORECASE)
DOCUMENT = 0o100644


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True).stdout


def eligible(path: str) -> bool:
    """Whether a changed repository path is documentation: `*.md` or `.claude/**`, and no deny rule matches."""
    parts = PurePosixPath(path).parts
    if not parts or ".." in parts or path.startswith("/") or parts[0] in DENIED_ROOTS:
        return False
    name = parts[-1]
    if path in DENIED_FILES or name in DENIED_NAMES or name.upper().startswith(DENIED_NAME_PREFIXES):
        return False
    return path.endswith(".md") or (parts[0] == ".claude" and len(parts) > 1)


def changes(root: Path) -> list[tuple[str, str, str, str]]:
    """(status, old mode, new mode, path) of HEAD against its first parent, renames split in two."""
    raw = git(root, "diff", "-z", "--raw", "--no-renames", "--no-abbrev", "HEAD^1", "HEAD").split("\0")
    entries = []
    for header, path in zip(raw[0::2], raw[1::2]):
        old, new, _, _, status = header.lstrip(":").split(" ")
        entries.append((status, old, new, path))
    return entries


def established(root: Path, event: str, head_sha: str, base_sha: str) -> str | None:
    """Why HEAD is not the commit this event classifies, or None when it is."""
    parents = git(root, "rev-parse", "HEAD^@").split()
    if event == "pull_request":
        if len(parents) != 2 or parents[1] != head_sha:
            return "the pull request merge commit could not be established"
    elif git(root, "rev-parse", "HEAD").strip() != head_sha or not parents or parents[0] != base_sha:
        return "the merge queue group commit and its base could not be established"
    return None


def decide(root: Path, event: str, head_sha: str, body: str, base_sha: str = "") -> tuple[bool, str]:
    """(docs-only, reason). Every condition that is not established means a full verification."""
    if event not in ("pull_request", "merge_group"):
        return False, f"{event or 'this'} runs always verify in full"
    if event == "pull_request" and INTENT_LINE.search(body or ""):
        return False, "the pull request links a Tiden intent branch, which needs test evidence"
    if not head_sha or (event == "merge_group" and not base_sha):
        return False, "the event names no exact commit to classify"
    if why := established(root, event, head_sha, base_sha):
        return False, why
    entries = changes(root)
    if not entries:
        return False, "the change is empty"
    for status, old, new, path in entries:
        modes = {"A": (new,), "M": (old, new), "D": (old,)}.get(status)
        if modes is None or any(int(mode, 8) != DOCUMENT for mode in modes):
            return False, f"not a plain document addition, edit or deletion: {status} {old} {new} {path}"
        if not eligible(path):
            return False, f"not documentation by the docs-only rule: {path}"
    return True, f"only documentation changed ({len(entries)} file(s))"


def main() -> int:
    root = Path.cwd()
    event = os.environ.get("EVENT_NAME", "")
    try:
        payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        body = (payload.get("pull_request") or {}).get("body") or ""
        docs_only, reason = decide(root, event, os.environ.get("HEAD_SHA", ""), body if isinstance(body, str) else "",
                                   os.environ.get("BASE_SHA", ""))
    except Exception as exc:  # any doubt means full verification, never a failed run
        docs_only, reason = False, f"decision failed: {type(exc).__name__}: {exc}"
    sha = git(root, "rev-parse", "HEAD").strip() if docs_only else ""
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"docs_only={'true' if docs_only else 'false'}\ndocs_sha={sha}\n")
    with open(os.environ.get("GITHUB_STEP_SUMMARY", os.devnull), "a") as summary:
        summary.write(f"Docs-only run: {'yes' if docs_only else 'no'} ({reason})\n")
    print(f"docs_only={docs_only}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
