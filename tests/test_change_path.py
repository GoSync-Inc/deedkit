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
COMPOSE = """services:
  app:
    image: app
    security_opt: !override [no-new-privileges:true]
    secrets:
      - dsn
secrets:
  dsn:
    external: true
  sensitive:
    external: true
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
    "deploy/compose.yaml": COMPOSE, "app/schema.sql": "DROP VIEW IF EXISTS v;\nCREATE VIEW v AS SELECT 1;\n",
    "app/purge.py": 'def purge(db):\n    db.execute("DELETE FROM users WHERE expired = TRUE")\n'
                    '    db.execute("DROP TABLE scratch_cache")\n'
                    '    db.execute("DELETE FROM sessions "  # stale ones only\n'
                    '               "WHERE stale = TRUE")\n',
}
GATE_JOB = "    name: CI gate\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo gate\n"


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
    ({"app/schema.sql": "DROP  VIEW IF EXISTS v;\nCREATE VIEW v AS SELECT 2;\n"}, "app/schema.sql"),
    ({".github/workflows/ci.yml": CI.replace("      - run: echo gate", "      - run: echo first\n      - run: echo gate")},
     ".github/workflows/ci.yml"),
    ({"deploy/compose.yaml": COMPOSE.replace("image: app", "image: app:2")}, "deploy/compose.yaml"),
    ({f"app/m{n}.py": "X = 1\n" for n in range(5)}, "app/m0.py, app/m1.py, app/m2.py, app/m3.py, app/m4.py"),
    ({"app/purge.py": BASE["app/purge.py"] + "\n\nLIMIT = 10\n"}, "app/purge.py"),
    ({".github/workflows/remote.yml": "on: push\npermissions: {}\njobs:\n  lint:\n"
                                      "    uses: example/ci/.github/workflows/lint.yml@main\n"},
     ".github/workflows/remote.yml"),
], ids=["code", "deleted-test", "expand-migration", "python-secrets-module", "prose-drop", "read-only-tool",
        "inside-a-required-job", "narrowed-permissions", "existing-drop-reformatted", "step-added-before-a-step",
        "tagged-compose-edit", "every-path-listed", "unchanged-sql-in-edited-code", "unrelated-remote-workflow"])
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
    ({"app/new data.sql": "DROP TABLE t;\n"}, "destructive-sql: app/new data.sql"),
    ({'app/new"data.sql': "DROP TABLE t;\n"}, 'destructive-sql: app/new"data.sql'),
    ({"app/new\tdata.sql": "DROP TABLE t;\n"}, "destructive-sql: app/new\\tdata.sql"),
    ({".gitattributes": "*.sql -diff\n", "app/migrations/002_x.sql": "DROP TABLE t;\n"},
     "destructive-sql: app/migrations/002_x.sql"),
    ({"app/migrations/002_x.sql": "DELETE\nFROM t;\n"}, "destructive-sql: app/migrations/002_x.sql"),
    ({"app/migrations/002_x.sql": "ALTER TABLE t ALTER COLUMN id\nTYPE smallint;\n"},
     "destructive-sql: app/migrations/002_x.sql"),
    ({"app/migrations/002_x.sql": "ALTER TABLE t ALTER id TYPE smallint;\n"}, "destructive-sql: app/migrations/002_x.sql"),
    ({"app/migrations/002_x.sql": "DELETE /* archived rows */ FROM t;\n"}, "destructive-sql: app/migrations/002_x.sql"),
    ({"app/store.py": 'def reset(db):\n    db.execute("DROP SEQUENCE order_ids")\n'}, "destructive-sql: app/store.py"),
    ({".github/workflows/ci.yml": CI.replace("    uses: ./.github/workflows/checks.yml",
                                            '    uses: ./.github/workflows/checks.yml\n    "secrets": inherit')},
     "secrets: .github/workflows/ci.yml"),
    ({"deploy/compose.yaml": COMPOSE.replace("      - dsn\n", "      - dsn\n      - sensitive\n")},
     "secrets: deploy/compose.yaml"),
    ({".github/workflows/ci.yml": CI.replace("echo gate", "|\n          echo ${{ secrets\n          .TOKEN }}")},
     "secrets: .github/workflows/ci.yml"),
    ({".gitleaksignore": "abc:app/x.py:generic-api-key:1\n"}, "secrets: .gitleaksignore"),
    ({".github/workflows/checks.yml": CHECKS.replace("  verify:\n", "  verify:\n    name: checks / verify\n")},
     "ruleset: .github/workflows (renames, removes or duplicates a required check)"),
    ({".github/workflows/checks.yml": "on: workflow_call\njobs:\n  verify:\n    uses: ./.github/workflows/leaf.yml\n",
      ".github/workflows/leaf.yml": CHECKS},
     "ruleset: .github/workflows (renames, removes or duplicates a required check)"),
    ({"deploy/main-ruleset.json": "{not json"}, "ruleset: deploy/main-ruleset.json, required checks (cannot be read"),
    ({"app/new\rdata.sql": "DROP TABLE t;\n"}, "destructive-sql: app/new\\rdata.sql"),
    ({"app/purge.py": BASE["app/purge.py"].replace(" WHERE expired = TRUE", "")}, "destructive-sql: app/purge.py"),
    ({"app/purge.py": BASE["app/purge.py"].replace("scratch_cache", "users")}, "destructive-sql: app/purge.py"),
    ({"app/migrations/002_x.sql": "SELECT '-- marker'; DELETE FROM users;\n"},
     "destructive-sql: app/migrations/002_x.sql"),
    ({"app/migrations/002_x.sql": "SELECT '/*'; DELETE FROM users; SELECT '*/';\n"},
     "destructive-sql: app/migrations/002_x.sql"),
    ({".github/workflows/ci.yml": CI.replace(GATE_JOB, "    name: CI gate\n"
                                                       "    uses: example/ci/.github/workflows/gate.yml@main\n")},
     "ruleset: .github/workflows (renames, removes or duplicates a required check)"),
    ({".github/workflows/ci.yml": CI.replace("./.github/workflows/checks.yml",
                                            "example/ci/.github/workflows/checks.yml@main")},
     "ruleset: required checks (cannot be read: ValueError)"),
    ({"app/new\ndata.py": "X = 1\n"}, "doubt: app/new\\ndata.py (unusual characters in the path)"),
    ({"app/purge.py": BASE["app/purge.py"].replace('    db.execute("DROP TABLE scratch_cache")\n', "")},
     "destructive-sql: app/purge.py"),
    ({"app/cleanup.py": 'def cleanup(db):\n    db.execute("delete from users where id = %s", (1,))\n'},
     "destructive-sql: app/cleanup.py"),
    ({".github/workflows/checks.yml": CHECKS.replace("    runs-on:", "    if: false\n    runs-on:")},
     "ruleset: .github/workflows (changes the structure of a required check job)"),
    ({".github/workflows/checks.yml": CHECKS.replace("    runs-on:", "    strategy:\n      matrix:\n"
                                                                    "        version: [one, two]\n    runs-on:")},
     "ruleset: .github/workflows (changes the structure of a required check job)"),
    ({".github/workflows/checks.yml": None, ".github/workflows/renamed.yml": CHECKS,
      ".github/workflows/ci.yml": CI.replace("checks.yml", "renamed.yml")},
     "ruleset: .github/workflows (changes the structure of a required check job)"),
    ({".claude/settings.json": json.dumps({"permissions": {"allow": {"Bash": True}}})},
     "doubt: .claude/settings.json (cannot be read: ValueError)"),
    ({"AGENTS.md": OWNERS + "\n## 4. What the owner keeps for production\n\nNo approval needed.\n"},
     "owner-rules: AGENTS.md (the owner list)"),
    ({"app/purge.py": BASE["app/purge.py"].replace('"WHERE stale = TRUE"', '""')}, "destructive-sql: app/purge.py"),
    ({"app/old_users.py": 'def purge(db):\n    db.execute("""/* purge old users */\n        DELETE FROM users\n'
                          '        WHERE expired = TRUE\n    """)\n'},
     "doubt: app/old_users.py (cannot be read: ValueError)"),
    ({"app/new.sql": "/* DELETE FROM users;\n"}, "doubt: app/new.sql (cannot be read: ValueError)"),
    ({"app/new.sql": "SELECT 'unclosed;\n"}, "doubt: app/new.sql (cannot be read: ValueError)"),
    ({".claude/settings.json": json.dumps({"permissions": {"allow": {}, "deny": ["Read(**/.env)"]}})},
     "doubt: .claude/settings.json (cannot be read: ValueError)"),
], ids=["ruleset", "renamed-gate", "renamed-verify", "duplicated-gate", "secret-reference", "gitleaks",
        "write-permission", "dropped-permissions", "new-workflow-default-token", "role-sql", "send-tool-allowed",
        "deny-dropped", "drop-column", "rename-lowercase", "edited-migration", "delete-in-code", "own-rules",
        "owner-list", "unparsable-workflow", "space-in-name", "quote-in-name", "tab-in-name", "binary-diff-attribute",
        "delete-over-lines", "alter-type-over-lines", "alter-without-column", "comment-between-keywords",
        "drop-sequence-in-code", "quoted-secrets-key", "compose-secret-list-item", "secret-expression-over-lines",
        "gitleaksignore", "required-check-renamed-by-name", "required-job-became-a-caller", "malformed-ruleset",
        "carriage-return-in-name", "predicate-removed-in-code", "target-changed-in-code", "line-comment-in-literal",
        "block-comment-in-literals", "required-job-became-a-remote-call", "required-caller-became-remote",
        "newline-in-name", "destructive-sql-removed-in-code", "lower-case-delete-in-code", "required-job-skipped",
        "required-job-matrix", "required-workflow-moved", "settings-not-a-list", "second-owner-section",
        "predicate-literal-after-a-comment", "unbounded-statement-in-code", "unterminated-sql-comment",
        "unterminated-sql-quote", "settings-empty-object"])
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


# spec: ANYM-404
def test_a_changed_blob_that_cannot_be_read_takes_the_owner_path(tmp_path):
    path_of(tmp_path, {"app/new.py": "X = 1\n"})
    root = tmp_path / "repo"
    blob = git(root, "rev-parse", "HEAD:app/new.py")
    (root / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    assert change_path.classify(root, "main", "HEAD") == "OWNER — doubt: app/new.py (a blob cannot be read)"
