#!/usr/bin/env python3
"""Choose the tests an affected-only pull request run executes (owner decisions D36, D38).

A draft pull request run tests only what its change can reach; the ready run, the merge queue and the main push
verify in full, so the required checks keep meaning "the full suite passed on the merge head". The plan is a pure
function of the tested commit's tracked files and the changed paths, and every doubt means the full suite:

- a changed path under FULL_PATHS (the workflows, this selector, the root environment) runs everything;
- a module suite (SUITES) runs when a path under one of its prefixes changed;
- a root test module (tests/) runs when it changed, or when its closure holds a file that is the changed module or
  names the changed path. Its closure is the files its first-party imports reach, transitively, plus each conftest
  above it and the modules that conftest imports directly (tests/conftest.py imports the store, whose own imports
  would otherwise put every module under every test). A test or tool file (LITERAL_ROOTS; service modules' path
  words would reach every test through the store) names a path by a word of a string literal: the whole
  path, a trailing part of it, a directory path or top-level directory above a non-Python file, or a glob that
  matches it; a string naming a module counts as an import. A changed non-Python file inside a package is that
  package's module too, and a deleted module is named by every file that still imports it.

When the selected root modules would take longer on one runner than the parallel full suite does, the plan is the
full suite as well (MAX_AFFECTED_SECONDS, by the committed shard timings).
"""
from __future__ import annotations

import ast
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
# CI runs this file and the weights as they are on the base branch, copied side by side (ci.yml `plan`), so a pull
# request's own copy never chooses its own tests.
WEIGHTS = Path(__file__).resolve().with_name("python_shards.json")
# Paths whose change can alter what any test sees or how this plan is made.
FULL_PATHS = (".github/", "scripts/affected_tests.py", "pyproject.toml", "uv.lock", "tools/dev/")
# Module suites of checks.yml and the path prefixes that reach each one (its own tree and the trees it installs).
GATEWAY = ("services/gateway/", "packages/platform-contracts/")
SUITES = {
    "gateway": GATEWAY,
    "platform_contracts": ("packages/platform-contracts/",),
    "calendar": ("services/calendar/", *GATEWAY),
    "calls": ("services/calls/", *GATEWAY),
    "calls_frontend": ("services/calls/frontend/",),
    "collector": ("whatsapp/",),
    "npm": ("packages/npm/", "packages/wake-relay/"),
    "deedkit_npm": ("packages/deedkit/",),
    "load": GATEWAY,
    "go": ("whatsapp/whatsmeow/", "services/release-router/", "scripts/check_whatsmeow_fork.py"),
    "attest_verify": ("tools/attest-verify/",),
    # Build, release inventory, the synthetic MCP/HTTP drill and the installed distributions.
    "release": ("services/messaging/", "packages/npm/", *GATEWAY, "scripts/build.py", "scripts/check_release.py",
                "scripts/smoke.py", "LICENSE", "licenses/", "THIRD_PARTY_NOTICES.md"),
}
# The calls frontend is a suite of its own; the calls Python suite does not read it.
SUITE_EXCLUDES = {"calls": ("services/calls/frontend/",)}
# Above this many seconds of selected root tests, the six parallel shards finish sooner than one runner.
MAX_AFFECTED_SECONDS = 420.0
LITERAL_ROOTS = ("tests/", "scripts/", "tools/")
INTENT_LINE = re.compile(r"Tiden intent branch:", re.IGNORECASE)
TEST_FILE = re.compile(r"(test_.*|.*_test)\.py")
WORD = re.compile(r"[\w./*?-]+")
FILENAME = re.compile(r".*\w\.[A-Za-z0-9]{1,8}")
# (blob, path) -> (imports, literal words): a file's parse depends on its text and, for relative imports, its path.
PARSED: dict[tuple[str, str], tuple[frozenset[str], frozenset[str]]] = {}


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True).stdout


def changed_paths(root: Path, base: str, head: str) -> list[str]:
    """Every path the change adds, edits or deletes, renames split in two."""
    return sorted({path for path in git(root, "diff", "-z", "--name-only", "--no-renames", base, head).split("\0")
                   if path})


class Tree:
    """The tracked files of one commit: their blob ids, and their text read from Git."""

    def __init__(self, root: Path, commit: str = "HEAD"):
        self.root = root
        self.blobs: dict[str, str] = {}
        for entry in git(root, "ls-tree", "-r", "-z", "--full-tree", commit).split("\0"):
            header, _, path = entry.partition("\t")
            if path and header.split(" ")[1:2] == ["blob"]:
                self.blobs[path] = header.split(" ")[2]
        self.top = {path.split("/")[0] for path in self.blobs if "/" in path}

    def texts(self, paths: list[str]) -> dict[str, str]:
        """The text of each path, read in one `git cat-file --batch`."""
        if not paths:
            return {}
        request = "".join(self.blobs[path] + "\n" for path in paths).encode()
        data = subprocess.run(["git", "-C", str(self.root), "cat-file", "--batch"], input=request, check=True,
                              capture_output=True).stdout
        texts, offset = {}, 0
        for path in paths:
            end = data.index(b"\n", offset)
            size = int(data[offset:end].split(b" ")[2])
            texts[path] = data[end + 1:end + 1 + size].decode("utf-8", errors="replace")
            offset = end + 1 + size + 1
        return texts


def module_name(tree: Tree, path: str) -> str:
    """The dotted name a file imports as: its directories up to the first one without `__init__.py`."""
    parts = PurePosixPath(path).with_suffix("").parts
    names = [] if parts[-1] == "__init__" else [parts[-1]]
    directory = PurePosixPath(path).parent
    while directory.parts and (directory / "__init__.py").as_posix() in tree.blobs:
        names.insert(0, directory.name)
        directory = directory.parent
    return ".".join(names)


def package_of(tree: Tree, path: str) -> str | None:
    """The module a non-Python file belongs to: the nearest package directory above it, if any."""
    directory = PurePosixPath(path).parent
    while directory.parts:
        if (directory / "__init__.py").as_posix() in tree.blobs:
            return module_name(tree, (directory / "__init__.py").as_posix())
        directory = directory.parent
    return None


def prefixes(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[:index]) for index in range(1, len(parts) + 1)]


def parse(source: str, path: str, package: str) -> tuple[frozenset[str], frozenset[str]]:
    """(imported module names, words of string literals) of one Python file; an unparsable file's every word is
    both, so it still names what it may use."""
    try:
        tree = ast.parse(source, path)
    except (SyntaxError, ValueError):
        words = frozenset(WORD.findall(source))
        return words, words
    imports, words = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".") if package else []
                anchor = anchor[:len(anchor) - (node.level - 1)]
                base = ".".join([*anchor, *([base] if base else [])])
            imports.add(base)
            imports |= {f"{base}.{alias.name}" if base else alias.name for alias in node.names}
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            words |= set(WORD.findall(node.value))
    return frozenset(name for name in imports if name), frozenset(words)


class Index:
    """First-party Python modules of one tree: names, imports and path-like literal words."""

    def __init__(self, tree: Tree):
        self.tree = tree
        python = sorted(path for path in tree.blobs if path.endswith(".py"))
        self.files: dict[str, list[str]] = {}
        for path in python:
            self.files.setdefault(module_name(tree, path), []).append(path)
        missing = [path for path in python if (tree.blobs[path], path) not in PARSED]
        for path, source in tree.texts(missing).items():
            own = module_name(tree, path)
            PARSED[(tree.blobs[path], path)] = parse(source, path, own if path.endswith("__init__.py")
                                                     else own.rpartition(".")[0])
        self.imports: dict[str, set[str]] = {}
        self.literals: dict[str, set[str]] = {}
        for path in python:
            imports, words = PARSED[(tree.blobs[path], path)]
            # A string naming a module (importlib, monkeypatch targets) is a dependency on it too.
            self.imports[path] = set(imports) | {word for word in words
                                                 if word in self.files or word.split(".")[0] in self.files}
            # Only tests and repository tools read repository paths; a service module's path words (a document it
            # names, a `packages` label) would reach every test through the store.
            self.literals[path] = {token for token in (word.strip("./") for word in words)
                                   if self.path_like(token)} if path.startswith(LITERAL_ROOTS) else set()

    def path_like(self, token: str) -> bool:
        """Whether a literal word can name a repository path: a glob with a name part, a path with a slash, a file
        name with an extension or a top-level directory of the tree."""
        if not token or len(token) > 200:
            return False
        if "*" in token or "?" in token:
            return len(token.replace("*", "").replace("?", "")) >= 2 and ("." in token or "/" in token)
        return "/" in token or bool(FILENAME.fullmatch(token)) or token in self.tree.top

    def reach(self, path: str) -> set[str]:
        """Files a module reaches through first-party imports, transitively (itself included)."""
        seen, queue = set(), [path]
        while queue:
            current = queue.pop()
            if current in seen:
                continue
            seen.add(current)
            for name in self.imports.get(current, ()):
                for candidate in prefixes(name):
                    queue += [found for found in self.files.get(candidate, ()) if found not in seen]
        return seen

    def closure(self, test: str) -> set[str]:
        """The test module's reach, plus each conftest above it and the modules that conftest imports directly."""
        parts = PurePosixPath(test).parts
        files = self.reach(test)
        for depth in range(1, len(parts)):
            conftest = (PurePosixPath(*parts[:depth]) / "conftest.py").as_posix()
            if conftest in self.tree.blobs:
                files.add(conftest)
                for name in self.imports.get(conftest, ()):
                    files |= set(self.files.get(name, ()))
        return files


def names_path(literal: str, path: str, python: bool) -> bool:
    """Whether a path-like literal word can refer to the changed path."""
    if "*" in literal or "?" in literal:
        name = PurePosixPath(path).name
        return fnmatch.fnmatch(path, literal) or fnmatch.fnmatch(name, literal) or fnmatch.fnmatch(path, "*/" + literal)
    if literal == path or path.endswith("/" + literal):
        return True
    # A directory path (`whatsapp/whatsmeow`) or a top-level directory (`deploy`) names every file below it; Python
    # files have their imports instead.
    return not python and path.startswith(literal + "/")


def root_tests(tree: Tree) -> list[str]:
    return sorted(path for path in tree.blobs if path.startswith("tests/")
                  and TEST_FILE.fullmatch(PurePosixPath(path).name))


def full(reason: str) -> dict:
    return {"mode": "full", "reason": reason, "suites": [], "python": [], "seconds": 0.0}


def plan(tree: Tree, changed: list[str], weights: dict | None = None) -> dict:
    """{"mode": "affected"|"full", "reason", "suites", "python", "seconds"} for the changed paths of `tree`."""
    if not changed:
        return full("the change is empty")
    for path in changed:
        if any(path == prefix or (prefix.endswith("/") and path.startswith(prefix)) for prefix in FULL_PATHS):
            return full(f"a full-suite path changed: {path}")
    suites = sorted(name for name, owned in SUITES.items()
                    if any(path.startswith(owned) and not path.startswith(SUITE_EXCLUDES.get(name, ()))
                           for path in changed))
    index = Index(tree)
    verdicts: dict[tuple[str, str], bool] = {}
    named: set[str] = set()
    for path in changed:
        python = path.endswith(".py")
        owner = module_name(tree, path) if python else package_of(tree, path)
        named |= set(index.files.get(owner, ())) if owner else set()
        if python and path not in tree.blobs and owner:
            named |= {file for file, names in index.imports.items() if owner in names}
        for file, literals in index.literals.items():
            if file not in named and any(verdicts.setdefault((literal, path), names_path(literal, path, python))
                                         for literal in literals):
                named.add(file)
    selected = [test for test in root_tests(tree) if test in changed or index.closure(test) & named]
    timings = weights if weights is not None else json.loads(WEIGHTS.read_text())
    seconds = sum(float(timings["seconds"].get(test, timings["default"])) for test in selected)
    if seconds > MAX_AFFECTED_SECONDS:
        return {**full(f"the affected root tests take {seconds:.0f} s on one runner, longer than the parallel full "
                       f"suite ({len(selected)} modules)"), "suites": suites, "python": selected, "seconds": seconds}
    return {"mode": "affected", "reason": f"{len(changed)} changed path(s): {len(suites)} module suite(s), "
            f"{len(selected)} root test module(s), {seconds:.0f} s", "suites": suites, "python": selected,
            "seconds": seconds}


def main() -> int:
    """CI: plan the pull request merge commit (HEAD) against its base (HEAD^1); any error means the full suite."""
    root = Path.cwd()
    try:
        parents = git(root, "rev-parse", "HEAD^@").split()
        if len(parents) != 2 or parents[1] != os.environ.get("HEAD_SHA", ""):
            raise ValueError("the pull request merge commit could not be established")
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        if INTENT_LINE.search(str((event.get("pull_request") or {}).get("body") or "")):
            # As for docs-only runs: a linked Tiden intent branch needs the run's full evidence.
            raise ValueError("the pull request links a Tiden intent branch, which needs test evidence")
        result = plan(Tree(root), changed_paths(root, "HEAD^1", "HEAD"))
    except Exception as exc:  # any doubt means full verification, never a failed run
        result = full(f"plan failed: {type(exc).__name__}: {exc}")
    affected = result["mode"] == "affected"
    sha = git(root, "rev-parse", "HEAD").strip() if affected else ""
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"affected={'true' if affected else 'false'}\naffected_sha={sha}\n"
                     f"affected_suites={json.dumps(result['suites'] if affected else [])}\n"
                     f"affected_python={' '.join(result['python']) if affected else ''}\n")
    with open(os.environ.get("GITHUB_STEP_SUMMARY", os.devnull), "a") as summary:
        summary.write(f"Affected-only run: {'yes' if affected else 'no'} ({result['reason']})\n")
        summary.writelines(f"- {test}\n" for test in (result["python"] if affected else []))
    print(f"affected={affected}: {result['reason']}")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["plan"] and len(sys.argv) in (3, 4):
        # Local use: `affected_tests.py plan <base> [<head>]` prints the plan of that diff of this checkout.
        head = sys.argv[3] if len(sys.argv) == 4 else "HEAD"
        print(json.dumps(plan(Tree(ROOT, head), changed_paths(ROOT, sys.argv[2], head)), indent=1))
        sys.exit(0)
    sys.exit(main())
