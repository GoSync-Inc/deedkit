#!/usr/bin/env python3
"""Decide whether a pull request or merge queue run changes only documentation (ANYM-323, glob rule).

Such a run skips the test jobs; every other run, and any doubt, verifies in full. A path qualifies when it is a
Markdown file or lies under `.claude/`, and none of the deny rules matches it: the Markdown that code reads,
packages or ships (`README.md` anywhere, `SETUP.md`, `THIRD_PARTY_NOTICES.md`, `TELEMETRY.md`), license texts and
everything under the module trees. A pull request run classifies GitHub's two-parent merge of its head against the
base; a merge queue run classifies its group commit against its first parent, the group's base. The main push run
of a docs-only group finds no queue run that verified it, so it verifies in full before anything is published or
promoted (ANYM-321).

A document that a test file names is not documentation for this rule (ANYM-767). The scan reads the test files of the
classified commit and never runs them. It is computed from literal and prefix reads and is not a complete dependency
check: it sees string literals and path expressions, not a path a helper in another module builds. A path with a
non-literal part, a glob or a directory walk counts as reading every document under its longest literal directory
prefix. A comment line `# reads: <repository path>` names what such a read really reads and replaces the prefix for
the dynamic reads of the next statement (a whole `def` when it stands above one), so a read added elsewhere stays seen.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time
from typing import NamedTuple
import warnings

# Code reads these (tests/test_telemetry.py, scripts/check_release.py) or packages and images ship them; CI installs
# the Tiden CLI from the version and checksum in docs/harness/tiden.md.
DENIED_FILES = frozenset({"SETUP.md", "THIRD_PARTY_NOTICES.md", "TELEMETRY.md", "docs/harness/tiden.md"})
DENIED_NAMES = frozenset({"README.md"})
DENIED_NAME_PREFIXES = ("LICENSE", "COPYING")
# Module trees package their own documents (the calendar sdist, the npm client) and their tests may read them.
DENIED_ROOTS = frozenset({"licenses", "services", "packages", "whatsapp"})
INTENT_LINE = re.compile(r"Tiden intent branch:", re.IGNORECASE)
DOCUMENT = 0o100644
SCAN_SECONDS = 20
TEST_DIRECTORIES = frozenset({"tests", "test"})
# Binary fixtures have other suffixes; a file of these suffixes that does not decode fails the scan: a full run.
TEXT_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".sh", ".json", ".yml", ".yaml", ".toml", ".txt"})
READS_COMMENT = re.compile(r"^[ \t]*#[ \t]*reads:[ \t]*(\S.*?)[ \t]*$", re.MULTILINE)
PATH_TOKEN = re.compile(r"[\w@+.\-/]+")
QUOTED_PARTS = re.compile(r"""['"`][\w@+.\-/]+['"`](?:\s*,\s*['"`][\w@+.\-/]+['"`])*""")
UNBOUND_ROOT = re.compile(r"[A-Z_]*(?:ROOT|REPO)[A-Z_]*")
TEMPORARY = re.compile(r"te?mp", re.IGNORECASE)
WILDCARD = re.compile(r"[*?\[]")
PATH_TYPES = frozenset({"Path", "PurePath", "PosixPath", "PurePosixPath"})
SAME_PATH_METHODS = frozenset({"resolve", "absolute", "expanduser", "as_posix"})
SAME_PATH_FUNCTIONS = frozenset({"abspath", "realpath", "normpath", "str", "fspath"})
WALK_METHODS = frozenset({"glob", "rglob", "iterdir", "walk"})
WALK_FUNCTIONS = frozenset({"walk", "listdir", "scandir", "glob", "iglob"})
ROOTED, RELATIVE, FLOATING = "rooted", "relative", "floating"


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True).stdout


class Value(NamedTuple):
    """A path expression: its literal parts, what anchors them, and whether a non-literal part follows them."""
    parts: tuple[str, ...]
    anchor: str
    dynamic: bool = False


class Reads(NamedTuple):
    names: set[str]  # repository paths named exactly
    tails: set[str]  # literal path ends behind an unknown head
    dynamic: set[str]  # literal directory prefixes of dynamic reads; "" is the repository root


def split_path(text: str) -> tuple[str, ...]:
    return tuple(part for part in text.split("/") if part and part != ".")


def normal(parts: tuple[str, ...], base: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The parts below `base`, with `..` applied; leaving the repository stops at its root."""
    result = list(base)
    for part in parts:
        if part != "..": result.append(part)
        elif result: result.pop()
    return tuple(result)


def literal_names(text: str, directory: tuple[str, ...]) -> set[str]:
    """Repository paths a piece of text can name: from the root, and from the test file's directory when relative."""
    names = set()
    for token in PATH_TOKEN.findall(text):
        token = token.rstrip("@+.-")
        if "." not in token and "/" not in token: continue
        parts = split_path(token)
        if token.startswith("."): names.add("/".join(normal(parts, directory)))
        if ".." not in parts: names.add("/".join(parts))
    return names


class Scanner(ast.NodeVisitor):
    """Collects what one Python test file reads, in source order, so a name resolves to its latest assignment."""

    def __init__(self, path: str) -> None:
        self.file = PurePosixPath(path).parts
        self.directory = self.file[:-1]
        self.names: set[str] = set()
        self.tails: set[str] = set()
        self.scopes: list[dict[str, Value | None]] = [{}]
        self.dynamic: set[tuple[str, int]] = set()  # (literal directory prefix, line)

    def lookup(self, name: str) -> Value | None:
        for scope in reversed(self.scopes):
            if name in scope: return scope[name]
        if name == "__file__": return Value(self.file, ROOTED)
        return Value((), ROOTED) if UNBOUND_ROOT.fullmatch(name) else None

    def join(self, arguments: list[ast.expr], first: Value | None = None) -> Value | None:
        """Path join: an unknown first part stays unknown, an unknown later part makes the rest dynamic."""
        value = first
        for index, argument in enumerate(arguments):
            part = self.evaluate(argument)
            if value is None and index == 0 and first is None: value = part
            elif value is None: return None
            elif value.dynamic: return value
            elif part is None: return value._replace(dynamic=True)
            elif part.anchor == ROOTED: value = part
            else: value = Value(normal(part.parts, value.parts), value.anchor, part.dynamic)
        return value

    def evaluate(self, node: ast.expr) -> Value | None:
        if isinstance(node, ast.Constant):
            # An absolute path is outside the repository.
            return Value(split_path(node.value), RELATIVE) if isinstance(node.value, str) and node.value[:1] != "/" else None
        if isinstance(node, ast.Name): return self.lookup(node.id)
        if isinstance(node, ast.JoinedStr): return self.evaluate_fstring(node)
        if isinstance(node, ast.Attribute):
            value = self.evaluate(node.value) if node.attr == "parent" else None
            return value._replace(parts=value.parts[:-1]) if value and value.anchor == ROOTED else None
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "parents":
            value, index = self.evaluate(node.value.value), node.slice
            if value and value.anchor == ROOTED and isinstance(index, ast.Constant) and isinstance(index.value, int):
                return value._replace(parts=value.parts[:-(index.value + 1)])
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div): return self.evaluate_division(node)
        return self.evaluate_call(node) if isinstance(node, ast.Call) else None

    def evaluate_fstring(self, node: ast.JoinedStr) -> Value | None:
        """An f-string's literal directory prefix; a leading placeholder that is a known path anchors it."""
        pieces, value = list(node.values), Value((), RELATIVE, True)
        if pieces and isinstance(pieces[0], ast.FormattedValue):
            head = self.evaluate(pieces.pop(0).value)
            if head is None or head.anchor != ROOTED: return None
            value = head._replace(dynamic=True)
        first = pieces[0] if pieces else None
        text = str(first.value) if isinstance(first, ast.Constant) else ""
        if text[:1] == "/" and value.anchor == RELATIVE: return None
        return value._replace(parts=normal(split_path(text.rpartition("/")[0]), value.parts))

    def evaluate_division(self, node: ast.BinOp) -> Value | None:
        left = self.evaluate(node.left)
        if left is None or left.anchor == RELATIVE and not isinstance(node.left, (ast.Constant, ast.JoinedStr)):
            if any(isinstance(part, ast.Name) and TEMPORARY.search(part.id) for part in ast.walk(node.left)): return None
            right = self.evaluate(node.right)
            return Value(right.parts, FLOATING, right.dynamic) if right and right.anchor == RELATIVE else None
        return self.join([node.right], Value(left.parts, ROOTED if left.anchor == RELATIVE else left.anchor, left.dynamic))

    def evaluate_call(self, node: ast.Call) -> Value | None:
        function = node.func
        if isinstance(function, ast.Attribute):
            if function.attr == "join" and ast.unparse(function.value) == "os.path":
                return self.anchored(self.join(node.args))
            if function.attr == "dirname" and node.args:
                value = self.anchored(self.evaluate(node.args[0]))
                return value._replace(parts=value.parts[:-1]) if value else None
            if function.attr in SAME_PATH_FUNCTIONS and node.args: return self.evaluate(node.args[0])
            if function.attr == "getcwd" or (function.attr == "cwd" and not node.args): return Value((), ROOTED)
            if function.attr == "joinpath":
                value = self.evaluate(function.value)
                return self.join(node.args, value) if value else None
            if function.attr in SAME_PATH_METHODS: return self.evaluate(function.value)
            if function.attr in PATH_TYPES: return self.path_call(node)
            return None
        if isinstance(function, ast.Name):
            if function.id in PATH_TYPES: return self.path_call(node)
            if function.id in SAME_PATH_FUNCTIONS and node.args: return self.evaluate(node.args[0])
        return None

    def path_call(self, node: ast.Call) -> Value | None:
        return self.anchored(self.join(node.args)) if node.args else Value((), ROOTED)

    @staticmethod
    def anchored(value: Value | None) -> Value | None:
        """A relative path a path object or a file call takes starts at the working directory, the repository root."""
        return value._replace(anchor=ROOTED) if value and value.anchor == RELATIVE else value

    def add_dynamic(self, parts: tuple[str, ...], line: int) -> None:
        literal = []
        for part in parts:
            if WILDCARD.search(part): break
            literal.append(part)
        self.dynamic.add(("/".join(literal), line))

    def record(self, value: Value | None, line: int) -> None:
        if value is None: return
        if value.anchor == ROOTED:
            if value.dynamic: self.add_dynamic(value.parts, line)
            else: self.names.add("/".join(value.parts))
        elif value.anchor == FLOATING and not value.dynamic: self.tails.add("/".join(value.parts))

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str): self.names |= literal_names(node.value, self.directory)

    def visit_Expr(self, node: ast.Expr) -> None:
        # A string statement, a docstring above all, is no value any code can read.
        if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)): self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        self.record(self.evaluate(node), node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.record(self.evaluate(node), node.lineno)
        function, first = node.func, self.evaluate(node.args[0]) if node.args else None
        if isinstance(function, ast.Attribute):
            module = function.value.id if isinstance(function.value, ast.Name) else ""
            receiver = None if module in ("os", "glob") else self.evaluate(function.value)
            if receiver and receiver.anchor == ROOTED and function.attr in WALK_METHODS:
                pattern = first.parts if first and first.anchor == RELATIVE and function.attr in ("glob", "rglob") else ()
                self.add_dynamic(receiver.parts + pattern, node.lineno)
            name = function.attr if module in ("os", "glob") else ""
        else:
            name = function.id if isinstance(function, ast.Name) else ""
        if first and first.anchor != FLOATING and name in WALK_FUNCTIONS: self.add_dynamic(first.parts, node.lineno)
        elif first and first.anchor == RELATIVE and first.dynamic and name == "open": self.add_dynamic(first.parts, node.lineno)
        self.generic_visit(node)

    def bind(self, target: ast.expr, value: Value | None) -> None:
        if isinstance(target, ast.Name): self.scopes[-1][target.id] = value
        else:
            for name in ast.walk(target):
                if isinstance(name, ast.Name): self.scopes[-1][name.id] = None

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        value = self.evaluate(node.value)
        for target in node.targets: self.bind(target, value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        if node.value: self.bind(node.target, self.evaluate(node.value))

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.generic_visit(node)
        self.bind(node.target, self.evaluate(node.value))

    def visit_For(self, node: ast.For) -> None:
        self.bind(node.target, None)
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.bind(node.target, None)
        self.generic_visit(node)

    def visit_withitem(self, node: ast.withitem) -> None:
        if node.optional_vars is not None: self.bind(node.optional_vars, None)
        self.generic_visit(node)

    def scoped(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef) -> None:
        arguments = None if isinstance(node, ast.ClassDef) else node.args
        every = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, arguments.vararg, arguments.kwarg] if arguments else []
        self.scopes.append(dict.fromkeys(argument.arg for argument in every if argument))
        self.generic_visit(node)
        self.scopes.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = visit_ClassDef = scoped

    def result(self, tree: ast.Module, text: str) -> Reads:
        names, spans = set(self.names), []
        lines = [(text.count("\n", 0, match.start(1)) + 1, match.group(1)) for match in READS_COMMENT.finditer(text)]
        statements = [statement_span(node) for node in ast.walk(tree) if isinstance(node, ast.stmt)] if lines else []
        for line, path in lines:
            names.add(path)
            following = [span for span in statements if span[0] > line]
            if following: spans.append(min(following, key=lambda span: (span[0], -span[1])))
        dynamic = {prefix for prefix, line in self.dynamic if not any(first <= line <= last for first, last in spans)}
        return Reads(names, self.tails, dynamic)


def statement_span(node: ast.stmt) -> tuple[int, int]:
    """A statement's lines, its decorators included."""
    decorators = getattr(node, "decorator_list", [])
    return decorators[0].lineno if decorators else node.lineno, node.end_lineno or node.lineno


def scan_python(path: str, data: bytes) -> Reads:
    with warnings.catch_warnings():  # an invalid escape in a test's string is that test's matter
        warnings.simplefilter("ignore")
        tree = ast.parse(data, filename=path)
    scanner = Scanner(path)
    scanner.visit(tree)
    return scanner.result(tree, data.decode("utf-8"))


def scan_text(path: str, data: bytes) -> Reads:
    text, directory = data.decode("utf-8"), PurePosixPath(path).parts[:-1]
    tails = set()
    for match in QUOTED_PARTS.findall(text):
        parts = [part.strip()[1:-1] for part in match.split(",")]
        tails.add("/".join(normal(split_path("/".join(parts)))))
    return Reads(literal_names(text, directory), tails, set())


def is_test_file(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if parts[-1] == "conftest.py" or parts[-1].endswith("_test.go"): return True
    if len(parts) == 2 and parts[0] == "scripts" and parts[1].startswith("check_") and parts[1].endswith(".py"): return True
    return not TEST_DIRECTORIES.isdisjoint(parts[:-1])


def tree_blobs(root: Path, deadline: float) -> tuple[list[tuple[str, bytes]], set[str]]:
    """The test files of HEAD as (path, content), read from the commit, and every tracked path of it."""
    timeout = max(deadline - time.monotonic(), 0.001)
    listing = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "-z", "--full-tree", "HEAD"], check=True,
                             capture_output=True, timeout=timeout).stdout
    tracked, wanted = set(), []
    for entry in listing.decode("utf-8", "surrogateescape").split("\0"):
        if not entry: continue
        header, path = entry.split("\t", 1)
        mode, kind, sha = header.split(" ")
        tracked.add(path)
        suffix = PurePosixPath(path).suffix
        if kind == "blob" and mode != "120000" and is_test_file(path) and (suffix == ".py" or suffix in TEXT_SUFFIXES):
            wanted.append((path, sha))
    wanted.sort()
    timeout = max(deadline - time.monotonic(), 0.001)
    output = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"], check=True, capture_output=True, timeout=timeout,
                            input="".join(f"{sha}\n" for _, sha in wanted).encode()).stdout
    blobs, position = [], 0
    for path, _ in wanted:
        end = output.index(b"\n", position)
        size = int(output[position:end].split(b" ")[2])
        blobs.append((path, output[end + 1:end + 1 + size]))
        position = end + 2 + size
    return blobs, tracked


def scan_tests(root: Path) -> tuple[list[tuple[str, Reads]], set[str]]:
    """What each test file of HEAD reads, sorted by path, and every tracked path. Raises when it cannot say in time."""
    deadline = time.monotonic() + SCAN_SECONDS
    blobs, tracked = tree_blobs(root, deadline)
    scans = []
    for path, data in blobs:
        if time.monotonic() >= deadline: raise TimeoutError(f"over {SCAN_SECONDS} s at {path}")
        scans.append((path, scan_python(path, data) if path.endswith(".py") else scan_text(path, data)))
    if time.monotonic() >= deadline: raise TimeoutError(f"over {SCAN_SECONDS} s")
    return scans, tracked


def reader(document: str, scans: list[tuple[str, Reads]]) -> str | None:
    """Why a test keeps this document out of docs-only: a named read in any test first, then a dynamic one."""
    parts = PurePosixPath(document).parts
    ends = {"/".join(parts[index:]) for index in range(len(parts))}
    for path, reads in scans:
        if document in reads.names or not ends.isdisjoint(reads.tails): return f"{document} is read by {path}"
    for path, reads in scans:
        for prefix in sorted(reads.dynamic):
            if not prefix or document == prefix or document.startswith(f"{prefix}/"):
                return f"{document} falls under a dynamic read of {prefix or '.'} in {path}"
    return None


def scan_reason(root: Path, documents: list[str]) -> str | None:
    """Why the first of these documents a test reads keeps the change out of docs-only, or None when no test does."""
    scans, _ = scan_tests(root)
    return next(filter(None, (reader(document, scans) for document in documents)), None)


def stale_reads(root: Path) -> list[tuple[str, str]]:
    """Every (test file, path) of a `# reads:` comment in HEAD that names no tracked file."""
    deadline = time.monotonic() + SCAN_SECONDS
    blobs, tracked = tree_blobs(root, deadline)
    return [(path, match) for path, data in blobs if path.endswith(".py")
            for match in READS_COMMENT.findall(data.decode("utf-8")) if match not in tracked]


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
    try:
        if why := scan_reason(root, [path for _, _, _, path in entries]):
            return False, why
    except Exception as exc:  # any doubt means full verification (ANYM-767)
        return False, f"the scan of test files failed: {type(exc).__name__}: {exc}"
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
