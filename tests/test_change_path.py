"""A pull request's change path follows from its diff (scripts/change_path.py); synthetic git repositories."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts import change_path

ROOT = Path(__file__).resolve().parents[1]
RULESET = json.dumps({"rules": [{"type": "required_status_checks", "parameters": {"required_status_checks": [
    {"context": "checks / verify"}, {"context": "CI gate"}]}}]})
CI = """on: [pull_request]
permissions:
  contents: read
jobs:
  checks:
    uses: ./.github/workflows/checks.yml
  gate:
    name: CI gate
    runs-on: ubuntu-latest
    steps:
      - run: echo gate
"""
CHECKS = """on: workflow_call
jobs:
  verify:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - run: pytest
"""
SETTINGS = json.dumps({"permissions": {"allow": ["mcp__claude_ai_Deedkit__messages_get_messages"],
                                       "ask": ["mcp__claude_ai_Deedkit__messages_send_message"],
                                       "deny": ["Read(**/.env)"]}}, indent=2)
OWNERS = "# Rules\n\n## 1. Always\n\n- one\n\n## 2. What the owner keeps\n\n1. Ruleset.\n\n## 3. Change paths\n\n- AUTO\n"
BASE = {
    "AGENTS.md": OWNERS, "README.md": "readme\n", "deploy/main-ruleset.json": RULESET,
    ".github/workflows/ci.yml": CI, ".github/workflows/checks.yml": CHECKS, ".claude/settings.json": SETTINGS,
    "app/store.py": "def read():\n    return 'SELECT 1'\n", "tests/test_store.py": "def test_read():\n    pass\n",
    "app/migrations/001_base.sql": "-- base\nCREATE TABLE t (id int);\n", "app/reader_role.sql": "GRANT SELECT ON t;\n",
    ".gitleaks.toml": "[allowlist]\n", "scripts/change_path.py": "RULES = {}\n",
}


def git(root, *arguments):
    environment = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": str(root), "PATH": os.environ["PATH"],
                   "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True,
                          env=environment).stdout.strip()


def write(root, files):
    for path, text in files.items():
        target = root / path
        if text is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)


def path_of(tmp_path, change):
    """Commit BASE on main, then `change` (path -> text, None deletes) on a branch, and classify the branch."""
    root = tmp_path / "repo"
    root.mkdir()
    write(root, BASE)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    git(root, "checkout", "-q", "-b", "work")
    write(root, change)
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", "change")
    return change_path.classify(root, "main", "HEAD")


# spec: ANYM-404
@pytest.mark.parametrize("change, expected", [
    ({}, "AUTO — no changes"),
    ({"README.md": "new words\n", "docs/guide.md": "guide\n", "LICENSE": "MIT\n"}, "AUTO — documentation and tests only"),
    ({"tests/test_store.py": "def test_read():\n    assert True\n", "tests/fixtures/a.sql": "DELETE FROM t;\n"},
     "AUTO — documentation and tests only"),
    ({"AGENTS.md": OWNERS.replace("- AUTO", "- AUTO, docs only")}, "AUTO — documentation and tests only"),
], ids=["empty", "docs", "tests-only", "agents-outside-owner-list"])
def test_documentation_and_tests_only_take_the_auto_path(tmp_path, change, expected):
    assert path_of(tmp_path, change) == expected


# spec: ANYM-404
@pytest.mark.parametrize("change, named", [
    ({"app/store.py": "def read():\n    return 'SELECT 2'\n"}, "app/store.py"),
    ({"tests/test_store.py": None}, "tests/test_store.py (removes or moves a test)"),
    ({"app/migrations/002_add.sql": "CREATE TABLE u (id int);\n"}, "app/migrations/002_add.sql"),
    ({"app/keys.py": "import secrets\nKEY = secrets.token_hex(8)\n"}, "app/keys.py"),
    ({"app/cache.py": "# drop the cache when it is stale\n"}, "app/cache.py"),
    ({".claude/settings.json": SETTINGS.replace('"mcp__claude_ai_Deedkit__messages_get_messages"',
                                                '"mcp__claude_ai_Deedkit__messages_get_messages", '
                                                '"mcp__claude_ai_Deedkit__calls_list_calls"')}, ".claude/settings.json"),
    ({".github/workflows/checks.yml": CHECKS.replace("pytest", "pytest -q")}, ".github/workflows/checks.yml"),
    ({".github/workflows/ci.yml": CI.replace("contents: read", "contents: none")}, ".github/workflows/ci.yml"),
], ids=["code", "deleted-test", "expand-migration", "python-secrets-module", "prose-drop", "read-only-tool",
        "inside-a-required-job", "narrowed-permissions"])
def test_any_other_file_takes_auto_with_review_and_names_it(tmp_path, change, named):
    assert path_of(tmp_path, change) == f"AUTO+review — code: {named}"


# spec: ANYM-404
@pytest.mark.parametrize("change, rule", [
    ({"deploy/main-ruleset.json": RULESET.replace("CI gate", "CI")}, "ruleset: deploy/main-ruleset.json"),
    ({".github/workflows/ci.yml": CI.replace("name: CI gate", "name: Gate")},
     "ruleset: .github/workflows (renames, removes or duplicates a required check)"),
    ({".github/workflows/checks.yml": CHECKS.replace("  verify:", "  test:")},
     "ruleset: .github/workflows (renames, removes or duplicates a required check)"),
    ({".github/workflows/other.yml": "on: push\npermissions: {}\njobs:\n  x:\n    name: CI gate\n    runs-on: u\n"},
     "ruleset: .github/workflows (renames, removes or duplicates a required check)"),
    ({".github/workflows/ci.yml": CI.replace("echo gate", "echo ${{ secrets.TOKEN }}")},
     "secrets: .github/workflows/ci.yml"),
    ({".gitleaks.toml": "[allowlist]\npaths = ['x']\n"}, "secrets: .gitleaks.toml"),
    ({".github/workflows/ci.yml": CI.replace("contents: read", "contents: write")},
     "permissions: .github/workflows/ci.yml"),
    ({".github/workflows/ci.yml": CI.replace("permissions:\n  contents: read\n", "")},
     "permissions: .github/workflows/ci.yml"),
    ({".github/workflows/new.yml": "on: push\njobs:\n  x:\n    runs-on: u\n    steps: []\n"},
     "permissions: .github/workflows/new.yml"),
    ({"app/reader_role.sql": "GRANT SELECT, UPDATE ON t;\n"}, "access: app/reader_role.sql"),
    ({".claude/settings.json": SETTINGS.replace("messages_get_messages", "messages_send_message")},
     "agent-permissions: .claude/settings.json (allow mcp__claude_ai_Deedkit__messages_send_message)"),
    ({".claude/settings.json": SETTINGS.replace('"Read(**/.env)"', "")},
     "agent-permissions: .claude/settings.json (drops deny Read(**/.env))"),
    ({"app/migrations/002_drop.sql": "ALTER TABLE t DROP COLUMN id;\n"}, "destructive-sql: app/migrations/002_drop.sql"),
    ({"app/migrations/002_rename.sql": "alter table t rename to u;\n"},
     "destructive-sql: app/migrations/002_rename.sql"),
    ({"app/migrations/001_base.sql": "CREATE TABLE t (id int);\n"},
     "destructive-sql: app/migrations/001_base.sql (changes an existing migration)"),
    ({"app/store.py": "def purge(db):\n    db.execute('DELETE FROM t')\n"}, "destructive-sql: app/store.py"),
    ({"scripts/change_path.py": "RULES = {'x': 1}\n"}, "owner-rules: scripts/change_path.py"),
    ({"AGENTS.md": OWNERS.replace("1. Ruleset.", "1. Ruleset only.")}, "owner-rules: AGENTS.md (the owner list)"),
    ({".github/workflows/ci.yml": "jobs: [unclosed\n"}, "cannot be read"),
], ids=["ruleset", "renamed-gate", "renamed-verify", "duplicated-gate", "secret-reference", "gitleaks",
        "write-permission", "dropped-permissions", "new-workflow-default-token", "role-sql", "send-tool-allowed",
        "deny-dropped", "drop-column", "rename-lowercase", "edited-migration", "delete-in-code", "own-rules",
        "owner-list", "unparsable-workflow"])
def test_what_the_owner_keeps_takes_the_owner_path_and_names_the_rule(tmp_path, change, rule):
    line = path_of(tmp_path, change)
    assert line.startswith("OWNER — ") and rule in line


# spec: ANYM-404
def test_an_owner_rule_outranks_review_and_every_match_is_reported(tmp_path):
    line = path_of(tmp_path, {"app/store.py": "X = 1\n", "deploy/main-ruleset.json": RULESET.replace("CI gate", "x"),
                              "scripts/change_path.py": "RULES = None\n"})
    assert line == "OWNER — ruleset: deploy/main-ruleset.json; owner-rules: scripts/change_path.py"


# spec: ANYM-404
def test_the_owner_list_of_the_real_agents_file_is_found():
    section = change_path.owner_list((ROOT / "AGENTS.md").read_text())
    assert section and section.startswith("## ") and "change_path.py" in section and "\n## 3." not in section


# spec: ANYM-404
def test_the_command_prints_one_line_and_a_git_failure_exits_2(tmp_path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    write(root, {"README.md": "x\n"})
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    assert change_path.main(["--repo", str(root), "--base", "main"]) == 0
    assert capsys.readouterr().out == "AUTO — no changes\n"
    assert change_path.main(["--repo", str(root), "--base", "missing"]) == 2
    assert "git failed" in capsys.readouterr().err
