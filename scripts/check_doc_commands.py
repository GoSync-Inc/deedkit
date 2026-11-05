#!/usr/bin/env python3
"""Refuse a tracked document whose fenced shell block holds a command the secret-read hook blocks (ANYM-790).

A docs-only pull request skips the test jobs (ANYM-323), so tests/test_claude_hooks.py alone would find such a block
only on the main push run. The `documentation` job runs this instead, from the repository root, on the standard library
only. A hit prints `file:line` of the block's opening fence and never the command: the job log is public. Any doubt (no
matcher, no tracked document, an undecodable document, a crash) exits non-zero, so the job and `CI gate` fail.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import textwrap

MATCHER = ".claude/hooks/block_secret_env_reads.py"
FENCE = re.compile(r"^([ \t]*)```(?:sh|bash|shell|zsh|console)[ \t]*\n(.*?)^[ \t]*```[ \t]*$", re.MULTILINE | re.DOTALL)


def located_blocks(text: str) -> Iterator[tuple[int, str]]:
    """(line of the opening fence, commands) of every fenced shell block, dedented and without `$ ` prompts."""
    for match in FENCE.finditer(text):
        block = re.sub(r"^\$ ", "", textwrap.dedent(match.group(2)), flags=re.MULTILINE)
        yield text.count("\n", 0, match.start()) + 1, block


def fenced_shell_blocks(text: str) -> Iterator[str]:
    return (block for _, block in located_blocks(text))


def load_is_blocked(path: Path) -> Callable[[str], bool]:
    spec = importlib.util.spec_from_file_location("block_secret_env_reads", path)
    if spec is None or spec.loader is None: raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.is_blocked


def main() -> int:
    root = Path.cwd()
    if not (root / MATCHER).is_file():
        print(f"{MATCHER}: the hook's matcher is missing", file=sys.stderr)
        return 1
    is_blocked = load_is_blocked(root / MATCHER)
    listing = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "*.md"], check=True, capture_output=True, text=True,
                             encoding="utf-8").stdout
    names = list(filter(None, listing.split("\0")))
    if not names:
        print("no tracked document to check", file=sys.stderr)
        return 1
    status = 0
    # This file's name makes the docs-only scan read it as a test (scripts/check_*.py). It reads every document only to
    # refuse a blocked command, so a walk here must not end the skip it protects (ANYM-767): this names one document.
    # reads: docs/operations/production.md
    for name in names:
        path = root / name
        if not path.is_file(): continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            print(f"{name}: not UTF-8, so it cannot be checked", file=sys.stderr)
            status = 1
            continue
        for line, block in located_blocks(text):
            if is_blocked(block):
                print(f"{name}:{line}: this fenced command is one the secret-read hook blocks")
                status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
