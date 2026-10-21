#!/usr/bin/env python3
"""List the specs a pull request touches, for its `Spec impact:` block; read-only on Tiden (ANYM-357).

    uv run python scripts/spec_impact.py origin/main --body-file body.md

Each line is `ANYM-n: <disposition> — <title> [<mark>]`, with `(flagged)` after it when the spec's acceptance criteria
name a changed path or symbol. The match is check A's (scripts/tiden_pr_check.py) on the merge base: the source anchors
of Tiden main against the changed paths, drift's EXCLUDED left out. Deleted and renamed-away paths also count, as the
accepted both-path rule says. The mark is `named (Delivers)`, `named (Spec)` or `anchor only` from the body's lines. The
output holds IDs, one-line titles, marks and escaped paths, never spec text, since it goes into a public description.
Marks and flags are aids: the author checks every disposition. A failed read, an incomplete listing or a rejected body
line prints `Refused: <reason>` and no list, and exits 2.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import sys
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tiden_drift import Refused, excluded, shown  # noqa: E402
from tiden_pr_check import (  # noqa: E402
    anchor_map, anchor_paths, blob, changed, git, listing, parse_body, validate_listing)  # noqa: F401 (anchor_paths is a shared binding the card names)

TITLE_LIMIT = 120
FOOTER = "Flags are suggestions; the author checks every disposition (docs/operations/tiden-product-workflow.md)."
# The card template's labels (docs/operations/tiden-product-workflow.md); the criteria section ends at the next one.
LABELS = ("Title", "Problem", "Desired behavior", "Category", "Lane", "Expected paths", "Depends on", "Priority",
          "Commitment", "Boundary", "Evidence to close", "Value", "History", "How others solve it", "Research",
          "Known limitations", "Self-check", "Accepted")
CRITERIA = re.compile(r"^[ \t]*(?:#+[ \t]*)?Acceptance criteria\b.*$", re.IGNORECASE)
SECTION_END = re.compile(rf"^(?:#|[ \t]*(?:{'|'.join(LABELS)})[ \t]*:)", re.IGNORECASE)
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
# A Go method has its receiver between `func` and the name.
DECLARATION = re.compile(r"\b(?:def|class|function|fn|type|interface|struct|enum|const|let|var)\s+([A-Za-z_]\w*)"
                         r"|\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")


def criteria(content: str) -> str:
    """The acceptance-criteria section of a spec text: from its label to the next template label or Markdown heading;
    the whole text when the label is missing."""
    lines = content.splitlines()
    start = next((index for index, line in enumerate(lines) if CRITERIA.match(line)), None)
    if start is None:
        return content
    end = next((index for index in range(start + 1, len(lines)) if SECTION_END.match(lines[index])), len(lines))
    return "\n".join(lines[start:end])


def title(spec: dict | None) -> str:
    """One line without control characters, cut at TITLE_LIMIT; `(unknown)` for an ID Tiden does not list."""
    text = spec.get("title") if spec else None
    if not isinstance(text, str):
        return "(unknown)"
    text = "".join(char if not unicodedata.category(char).startswith("C") else " " for char in text)
    return " ".join(text.split())[:TITLE_LIMIT] or "(untitled)"


def declared(text: str) -> set[str]:
    return {name for pair in DECLARATION.findall(text) for name in pair if name}


def hunks(root: Path, base: str, head: str, path: str) -> list[tuple[range, range, str]]:
    """(old lines, new lines, header text) of each hunk of one file, 1-based."""
    text = git(root, "diff", "-U0", "--no-renames", base, head, "--", f":(literal){path}").decode("utf-8", errors="replace")
    found = []
    for line in text.split("\n"):
        match = HUNK.match(line)
        if match:
            old, old_count, new, new_count, context = match.groups()
            found.append((range(int(old), int(old) + int(old_count or 1)), range(int(new), int(new) + int(new_count or 1)),
                          context))
    return found


def python_spans(source: str | None) -> list[tuple[str, int, int]] | None:
    """(name, first line, last line) of every function and class, decorators included; None when it does not parse."""
    if source is None:
        return []
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    return [(node.name, min([node.lineno, *(item.lineno for item in node.decorator_list)]), node.end_lineno or node.lineno)
            for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def symbols(root: Path, base: str, head: str, path: str) -> tuple[set[str], bool]:
    """The symbols one changed file touches, and whether they come from declaration lines and hunk headers only (any
    file but Python, or a Python revision that does not parse)."""
    old, new = blob(root, base, path), blob(root, head, path)
    marks = hunks(root, base, head, path)
    before, after = python_spans(old), python_spans(new)
    if path.endswith(".py") and before is not None and after is not None:
        found = {name for name, first, last in before if any(set(range(first, last + 1)) & set(a) for a, _, _ in marks)}
        found |= {name for name, first, last in after if any(set(range(first, last + 1)) & set(b) for _, b, _ in marks)}
        found |= {name for name, _, _ in before} ^ {name for name, _, _ in after}
        return found, False
    found = set()
    for _, _, context in marks:
        found |= declared(context)
    for revision, source in ((old, "-"), (new, "+")):
        lines = (revision or "").split("\n")
        for old_range, new_range, _ in marks:
            for number in (old_range if source == "-" else new_range):
                if 0 < number <= len(lines):
                    found |= declared(lines[number - 1])
    return found, True


def flagged(content: str, paths: list[str], names: set[str]) -> bool:
    text = criteria(content)
    if any(path in text or path.rsplit("/", 1)[-1] in text for path in paths):
        return True
    return any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) for name in names)


def deleted(root: Path, base: str, head: str) -> list[str]:
    output = git(root, "diff", "-z", "--name-only", "--no-renames", "--diff-filter=D", base, head, "--")
    try:
        return [path.decode("utf-8") for path in output.split(b"\0") if path]
    except UnicodeDecodeError:
        raise Refused("a changed path is not valid UTF-8") from None


def build(args: argparse.Namespace) -> list[str]:
    root = args.root.resolve()
    lines = parse_body(args.body_file.read_text(encoding="utf-8")) if args.body_file else parse_body("")
    if lines.errors:
        raise Refused(lines.errors[0])
    if args.specs:
        try:
            main = validate_listing(json.loads(args.specs.read_text(encoding="utf-8")), "the saved listing")
        except (OSError, json.JSONDecodeError, AttributeError):
            raise Refused("the saved listing is unreadable") from None
        known = main
    else:
        product = args.product_id or os.environ.get("TIDEN_PRODUCT_ID")
        if not product:
            raise Refused("no product: pass --product-id or set TIDEN_PRODUCT_ID")
        main = listing(args.cli, product, "main")
        known = {**main, **listing(args.cli, product, lines.branch)} if lines.branch else main
    base = git(root, "merge-base", args.base, args.head).decode().strip()
    surviving, gone = changed(root, base, args.head), deleted(root, base, args.head)
    code = [path for path in surviving if not excluded(path)]
    anchors = anchor_map(main)
    matched = {number for path in [*code, *(path for path in gone if not excluded(path))]
               for number in anchors.get(path, [])}
    listed = sorted(matched | set(lines.named))
    extra = sorted(set(args.flag) - set(listed))
    if extra:
        raise Refused(f"{', '.join(f'ANYM-{number}' for number in extra)} is not listed; name it in `Spec:` or "
                      "`Delivers:` first")
    paths = [*surviving, *gone]
    names, header_only = set(), []
    for path in paths:
        found, partial = symbols(root, base, args.head, path)
        names |= found
        if partial:
            header_only.append(path)
    out = []
    for number in listed:
        mark = "named (Delivers)" if number in lines.delivers else "named (Spec)" if number in lines.spec else "anchor only"
        flag = number in args.flag or flagged(str((known.get(number) or {}).get("content") or ""), paths, names)
        out.append(f"ANYM-{number}: <disposition> — {title(known.get(number))} [{mark}]" + (" (flagged)" if flag else ""))
    unanchored = [path for path in code if path not in anchors]
    if unanchored:
        where = (f"sync B anchors them to ANYM-{lines.named[0]}" if lines.named
                 else "no spec is named, so check A will fail")
        out.append(f"Unanchored: {len(unanchored)} file(s); {where}: " + ", ".join(shown(path) for path in unanchored))
    if header_only:
        out.append("symbols: header-only: " + ", ".join(shown(path) for path in header_only))
    return [*out, FOOTER]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List the specs a change touches, for the `Spec impact:` block.")
    parser.add_argument("base", help="the base ref; the diff runs from its merge base with --head")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--body-file", type=Path, help="the pull request body, for the Spec:/Delivers: marks")
    parser.add_argument("--specs", type=Path, help="a saved `tiden spec list --include-sources` instead of Tiden main")
    parser.add_argument("--flag", action="append", default=[], type=lambda value: int(value.removeprefix("ANYM-")),
                        help="add a flag by hand (ANYM-n, repeatable)")
    parser.add_argument("--cli", default="tiden")
    parser.add_argument("--product-id")
    args = parser.parse_args(argv)
    try:
        output = build(args)
    except Refused as error:
        print(f"Refused: {shown(str(error))}")
        return 2
    except Exception as error:  # noqa: BLE001 - fail loudly without echoing data the error may hold
        print(f"Refused: unexpected {type(error).__name__}")
        return 2
    print("\n".join(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
