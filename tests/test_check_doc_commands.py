"""A docs-only pull request cannot add a fenced command the secret-read hook blocks (ANYM-790); synthetic git repositories."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

from hypothesis import given, strategies as st
import pytest
import yaml

from scripts import check_doc_commands, docs_only

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_doc_commands.py"
MATCHER_FILE = ROOT / ".claude/hooks/block_secret_env_reads.py"
NOTE = "synthetic-note.md"
CLEAN = "# Note\n\n```sh\ngit status\n```\n"
BLOCKED = "# Note\n\nProse.\n\n```sh\nprintenv\n```\n"


def git(root, *arguments):
    environment = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": str(root), "PATH": os.environ["PATH"],
                   "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True,
                          env=environment).stdout.strip()


def commit_all(root, message):
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)


def repository(tmp_path, *, with_matcher=True):
    """A base commit with one clean document and, normally, the hook's matcher; returns its root."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / NOTE).write_text(CLEAN)
    if with_matcher:
        (root / ".claude/hooks").mkdir(parents=True)
        shutil.copy(MATCHER_FILE, root / ".claude/hooks/block_secret_env_reads.py")
    git(root, "init", "-q", "-b", "main")
    commit_all(root, "base")
    return root


def pull_request(root, text):
    """Commit `text` to the document on a head branch and check out GitHub's two-parent merge; returns the head SHA."""
    git(root, "switch", "-q", "-c", "feature")
    (root / NOTE).write_text(text)
    commit_all(root, "head")
    head = git(root, "rev-parse", "HEAD")
    git(root, "switch", "-q", "main")
    (root / "base-moved.txt").write_text("main moved on\n")
    commit_all(root, "main moved on")
    git(root, "merge", "-q", "--no-ff", "--no-edit", "feature")
    return head


def group(root, text):
    """Commit `text` to the document as a single-parent merge queue group commit; returns (group SHA, base SHA)."""
    base = git(root, "rev-parse", "HEAD")
    (root / NOTE).write_text(text)
    commit_all(root, "group")
    return git(root, "rev-parse", "HEAD"), base


def check(root):
    return subprocess.run([sys.executable, "-I", str(SCRIPT)], cwd=root, capture_output=True, text=True, timeout=60,
                          env={"PATH": os.environ["PATH"], "HOME": str(root)})


def classify(root, event, text):
    if event == "merge_group":
        head, base = group(root, text)
        return docs_only.decide(root, event, head, "", base)
    return docs_only.decide(root, event, pull_request(root, text), "")


# spec: ANYM-790
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_a_docs_only_change_adding_a_blocked_command_exits_1_and_stays_docs_only(tmp_path, event):
    root = repository(tmp_path)
    decision = classify(root, event, BLOCKED)
    completed = check(root)
    assert decision[0] is True
    assert completed.returncode == 1
    assert f"{NOTE}:5" in completed.stdout
    # The log is public: file and line only, never the command.
    assert "printenv" not in completed.stdout + completed.stderr


# spec: ANYM-790
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_a_clean_docs_only_change_exits_0_and_stays_docs_only(tmp_path, event):
    root = repository(tmp_path)
    assert classify(root, event, CLEAN + "\nMore prose.\n")[0] is True
    completed = check(root)
    assert (completed.returncode, completed.stdout) == (0, "")


# spec: ANYM-790
def test_a_missing_matcher_exits_1(tmp_path):
    completed = check(repository(tmp_path, with_matcher=False))
    assert completed.returncode == 1
    assert "block_secret_env_reads.py" in completed.stderr


# spec: ANYM-790
def test_a_directory_with_no_tracked_document_exits_1_instead_of_passing_unchecked(tmp_path):
    root = repository(tmp_path)
    git(root, "rm", "-q", NOTE)
    assert check(root).returncode == 1


# spec: ANYM-790
def test_an_undecodable_document_exits_1_and_names_it(tmp_path):
    root = repository(tmp_path)
    (root / NOTE).write_bytes(b"\xff\xfe not text\n")
    completed = check(root)
    assert completed.returncode == 1
    assert NOTE in completed.stderr


# spec: ANYM-790
@given(st.lists(st.tuples(st.lists(st.sampled_from(["", "Prose.", "`inline`"]), max_size=3),
                          st.lists(st.sampled_from(["ls", "git status", "uv sync"]), min_size=1, max_size=3)),
                max_size=4))
def test_every_located_block_reports_its_command_and_the_line_of_its_opening_fence(sections):
    # Inverse: build a document from known blocks, and extraction returns them with the lines they were put on.
    lines, expected = [], []
    for prose, commands in sections:
        lines += prose
        expected.append((len(lines) + 1, "".join(f"{command}\n" for command in commands)))
        lines += ["```sh", *commands, "```"]
    text = "".join(f"{line}\n" for line in lines)
    assert list(check_doc_commands.located_blocks(text)) == expected
    assert list(check_doc_commands.fenced_shell_blocks(text)) == [block for _, block in expected]


# spec: ANYM-790
def test_the_documentation_job_runs_the_check_after_the_decision_and_a_failure_fails_the_gate():
    checks = yaml.safe_load((ROOT / ".github/workflows/checks.yml").read_text())
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = checks["jobs"]["documentation"]["steps"]
    index = next(position for position, step in enumerate(steps) if step.get("id") == "decide")
    step = steps[index + 1]
    assert step["run"] == "python3 -I scripts/check_doc_commands.py"
    # No condition: it runs on every run of the job, and after a failed decision too, which only continues on error.
    assert "continue-on-error" not in step and "if" not in step
    assert steps[index]["continue-on-error"] is True
    assert 'test "$CHECKS_RESULT" = success' in ci["jobs"]["gate"]["steps"][0]["run"]
