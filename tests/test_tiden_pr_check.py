"""The Tiden PR check against temporary Git repositories and a fake Tiden CLI; it never calls Tiden."""
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

import scripts.tiden_pr_check as pr_check
import scripts.tiden_sync as tiden_sync
from tiden_fake import READS, SECRET, Server, anchor, commit, git, repository, spec

ROOT_SCRIPT = str(Path(__file__).resolve().parents[1] / "scripts/tiden_pr_check.py")


@pytest.fixture
def server(tmp_path, monkeypatch):
    return Server(tmp_path, monkeypatch)


@pytest.fixture
def repo(tmp_path):
    root = repository(tmp_path / "work")
    base = commit(root, {"app/core.py": "x = 1\n", "tests/test_old.py": "def test_old():\n    pass\n"}, "base")
    return root, base


def run(repo, server, files, body=""):
    """Commit `files` on top of the current HEAD and check that one commit as a pull request."""
    root, _ = repo
    base = git(root, "rev-parse", "HEAD")
    head = commit(root, files, "change")
    lines = []
    code = pr_check.check(root, base, head, body, str(server.cli), "product", lines.append)
    return code, "\n".join(lines)


# spec: ANYM-405
def test_marked_tests_and_a_named_spec_pass_with_reads_only(repo, server):
    server.set(main=[spec(12, sources=[anchor("app/core.py")]), spec(34)])
    code, out = run(repo, server, {
        "app/core.py": "x = 2\n", "app/new.py": "y = 1\n",
        "tests/test_new.py": "import pytest\n\n\n# spec: ANYM-12\ndef test_a():\n    pass\n\n\n"
                             "# spec: ANYM-12, ANYM-34\n@pytest.mark.parametrize('v', [1])\nasync def test_b(v):\n"
                             "    pass\n\n\nclass TestGroup:\n    # spec: ANYM-34\n    def test_c(self):\n        pass\n",
    }, "Some text\nSpec: ANYM-34\n")
    assert code == 0, out
    assert "- app/core.py: ANYM-12" in out and "- app/new.py: no anchor" in out
    assert "sync anchors 1 unanchored file(s) to ANYM-34" in out
    assert "test tests/test_new.py:5 test_a -> ANYM-12" in out
    assert "TestGroup::test_c -> ANYM-34" in out
    assert set(server.calls()) <= READS and not server.writes()


# spec: ANYM-405
def test_unanchored_code_without_a_spec_line_fails_and_anchored_code_needs_none(repo, server):
    server.set(main=[spec(12, sources=[anchor("app/core.py")])])
    code, out = run(repo, server, {"app/core.py": "x = 2\n", "notes/a.md": "text\n", "app/new.py": "y\n"})
    assert code == 1 and "1 changed code file(s) have no anchor" in out and "app/new.py" in out
    code, out = run(repo, server, {"app/core.py": "x = 3\n", "README.md": "text\n"})
    assert code == 0, out


# spec: ANYM-504
def test_a_licence_only_change_needs_no_spec_but_a_code_file_beside_it_still_does(repo, server):
    server.set(main=[spec(12, sources=[anchor("app/core.py")])])
    code, out = run(repo, server, {"services/calls/LICENSE": "licence text\n"})
    assert code == 0, out
    assert "code files 0" in out and "no anchor" not in out and "LICENSE" not in out
    code, out = run(repo, server, {"services/calls/LICENSE": "licence text 2\n",
                                   "services/calls/healthcheck.py": "ok = True\n"})
    assert code == 1 and "1 changed code file(s) have no anchor" in out
    assert "services/calls/healthcheck.py" in out and "LICENSE" not in out


# spec: ANYM-405
def test_an_added_test_without_a_marker_fails_but_existing_tests_need_none(repo, server):
    server.set(main=[spec(12)])
    code, out = run(repo, server, {"tests/test_old.py": "def test_old():\n    assert True\n\n\ndef test_new():\n"
                                                        "    pass\n"}, "Spec: ANYM-12")
    assert code == 1
    assert "added test tests/test_old.py:5 test_new: no marker" in out and "test_old:" not in out


# spec: ANYM-405
def test_a_marker_between_decorator_and_definition_or_malformed_or_with_the_wrong_comment_fails(repo, server):
    server.set(main=[spec(12)])
    code, out = run(repo, server, {
        "tests/test_a.py": "import pytest\n\n\n@pytest.mark.slow\n# spec: ANYM-12\ndef test_decorated():\n    pass\n"
                           "\n\n# Spec: anym-12\ndef test_case():\n    pass\n\n\n# spec: ANYM-12 and more\n"
                           "def test_text():\n    pass\n",
        "web/a.test.mjs": "import test from 'node:test';\n# spec: ANYM-12\ntest('hash comment', () => {});\n",
    }, "Spec: ANYM-12")
    assert code == 1
    assert "test_decorated: no marker" in out
    assert "test_case: malformed marker" in out and "test_text: malformed marker" in out
    assert "hash comment: marker must start with `//`" in out


# spec: ANYM-405
def test_node_and_go_tests_are_found_and_a_renamed_file_counts_as_new(repo, server):
    server.set(main=[spec(12)])
    root, _ = repo
    commit(root, {"pkg/old_test.go": "package pkg\n\nfunc TestOld(t *testing.T) {}\n"}, "go")
    code, out = run(repo, server, {
        "web/b.test.js": "// spec: ANYM-12\ntest('adds \\'quoted\\' title', () => {});\n  it.skip(\"nested\", () => {});\n",
        "pkg/old_test.go": None, "pkg/moved_test.go": "package pkg\n\n// spec: ANYM-12\nfunc TestOld(t *testing.T) {}\n"
                                                      "\nfunc TestHelper(t *testing.T) {}\nfunc BenchmarkX(b *testing.B) {}\n",
    }, "Spec: ANYM-12")
    assert code == 1
    assert "web/b.test.js:2 <test('adds \\'quoted\\' title', () => {});>: the test name is not a plain" in out
    assert "web/b.test.js:3 nested: no marker" in out
    assert "pkg/moved_test.go:4 TestOld -> ANYM-12" in out
    assert "TestHelper: no marker" in out and "BenchmarkX" not in out


# spec: ANYM-405
def test_unknown_epic_and_parent_ids_fail(repo, server):
    server.set(main=[spec(12), spec(20, kind="Epic"), spec(30, kind="Feature", children=2)])
    code, out = run(repo, server, {"tests/test_x.py": "# spec: ANYM-20\ndef test_x():\n    pass\n"},
                    "Delivers: ANYM-30\nSpec: ANYM-99")
    assert code == 1
    assert "ANYM-99 (named by the body) is not a spec on Tiden main" in out
    assert "ANYM-30 (named by the body) is a Feature with child specs" in out
    assert "ANYM-20 (named by tests/test_x.py:2 test_x) is a Epic" in out


# spec: ANYM-405
def test_a_card_on_the_named_open_branch_counts_and_a_closed_branch_fails(repo, server):
    server.set(main=[spec(12)], branch_specs={"proposal/x": [spec(50, status="Backlog", branch=True)]},
               previews={"proposal/x": {"additions": [{"seqNum": 50}], "modifications": []}},
               branches=[{"name": "main", "status": "open"}, {"name": "proposal/x", "status": "open"},
                         {"name": "review/y", "status": "merged"}])
    code, out = run(repo, server, {"tests/test_x.py": "# spec: ANYM-50\ndef test_x():\n    pass\n"},
                    "Delivers: ANYM-50\nTiden branch: proposal/x\n")
    assert code == 0, out
    assert "Named specs (the first is primary): ANYM-50 Spec 50" in out
    code, out = run(repo, server, {"tests/test_y.py": "# spec: ANYM-50\ndef test_y():\n    pass\n"},
                    "Delivers: ANYM-50\nTiden branch: review/y\n")
    assert code == 1 and "`Tiden branch: review/y` is merged, not open" in out
    assert "ANYM-50 (named by the body) is not a spec on Tiden main or review/y" in out


BRANCHES = [{"name": "main", "status": "open"}, {"name": "proposal/x", "status": "open"},
            {"name": "review/y", "status": "merged"}]


def previewed(server, preview, branch_numbers=(50, 51)):
    """Tiden main holds ANYM-12; proposal/x holds the given cards and its merge preview is `preview`."""
    server.set(main=[spec(12)], branches=BRANCHES, previews={"proposal/x": preview},
               branch_specs={"proposal/x": [spec(number, status="Backlog", branch=True) for number in branch_numbers]})


def run_main(repo, server, body, monkeypatch, capsys):
    """Commit a docs change and run the command line on `body`; its output and exit code are what CI shows."""
    root, _ = repo
    base = git(root, "rev-parse", "HEAD")
    commit(root, {"README.md": "text\n"}, "change")
    body_file = root.parent / "body.md"
    body_file.write_text(body)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setenv("TIDEN_PRODUCT_ID", "product")
    monkeypatch.setattr(sys, "argv", ["tiden_pr_check.py", "--root", str(root), "--base", base, "--body-file",
                                      str(body_file), "--cli", str(server.cli)])
    code = pr_check.main()
    return code, capsys.readouterr().out


# spec: ANYM-445
def test_a_named_branch_whose_preview_changes_an_unnamed_spec_or_nothing_fails_before_the_merge(repo, server):
    previewed(server, {"additions": [{"seqNum": 50}, {"seqNum": 51}], "modifications": []})
    code, out = run(repo, server, {"README.md": "text\n"}, "Delivers: ANYM-50\nTiden branch: proposal/x")
    assert code == 1 and "Passed." not in out
    assert "the preview of proposal/x changes ANYM-51, which the body does not name" in out
    previewed(server, {"additions": [{"seqNum": 52}], "modifications": [{"branch": {"seqNum": 51}}]}, (50, 51, 52))
    code, out = run(repo, server, {"README.md": "more\n"}, "Delivers: ANYM-50\nTiden branch: proposal/x")
    assert code == 1 and "changes ANYM-51, ANYM-52, which the body does not name" in out
    previewed(server, {"additions": [], "modifications": []})
    code, out = run(repo, server, {"README.md": "again\n"}, "Spec: ANYM-12\nTiden branch: proposal/x")
    assert code == 1 and "the preview of proposal/x changes no spec" in out
    assert not server.writes()


# spec: ANYM-445
def test_named_specs_the_branch_does_not_change_pass_only_when_they_are_on_main(repo, server):
    previewed(server, {"additions": [{"seqNum": 50}], "modifications": []})
    code, out = run(repo, server, {"README.md": "text\n"}, "Delivers: ANYM-50\nSpec: ANYM-12\nTiden branch: proposal/x")
    assert code == 0, out
    assert "ANYM-12: named, not changed by proposal/x" in out and "Branch preview: ANYM-50" in out
    code, out = run(repo, server, {"README.md": "more\n"}, "Delivers: ANYM-50\nSpec: ANYM-51\nTiden branch: proposal/x")
    assert code == 1
    assert out.count("ANYM-51 (named by the body) is not a spec on Tiden main or proposal/x") == 1
    code, out = run(repo, server, {"README.md": "again\n"}, "Delivers: ANYM-50\nSpec: ANYM-99\nTiden branch: proposal/x")
    assert code == 1
    assert out.count("ANYM-99 (named by the body) is not a spec on Tiden main or proposal/x") == 1
    assert not server.writes()


# spec: ANYM-445
@pytest.mark.parametrize("preview, kind, text", [
    ({"additions": [{"seqNum": 50}], "testAdditions": [{"id": "t"}]}, "verdict", "testAdditions"),
    ({"additions": [{"seqNum": 50}], "deletions": [{"seqNum": 12}]}, "verdict", "deletions"),
    ({"additions": [{"seqNum": 51}], "deletions": [{"seqNum": 12}]}, "verdict", "(deletions)"),
    ({"additions": [{"seqNum": 50}], "stats": {"conflicts": 1}}, "verdict", "conflicts"),
    ({"additions": [{"seqNum": 50}], "blockingSessions": [{"sessionId": "s"}]}, "verdict", "blocking"),
    ({"additions": [{"seqNum": 50}], "componentAdditions": [{"name": "c"}]}, "verdict", "componentAdditions"),
    ({"additions": [{"seqNum": 50}], "componentModifications": [{"name": "c"}]}, "verdict", "componentModifications"),
    ({"additions": [{"seqNum": 50}], "testModifications": [{"id": "t"}]}, "verdict", "testModifications"),
    ({"additions": [{"seqNum": 50}], "testDeletions": [{"id": "t"}]}, "verdict", "testDeletions"),
    ({"additions": [{"seqNum": 50}], "stats": {"additions": 1, "conflicts": 2}}, "verdict", "stats.conflicts"),
    ({"additions": [{"seqNum": 50}], "stats": {"additions": 2}}, "verdict", "stats.additions"),
    ({"additions": [{"seqNum": 50}], "intentSession": {"id": "s"}}, "verdict", "intentSession"),
    ({"additions": [{"seqNum": 50}], "hasRequirementChanges": "yes"}, "verdict", "hasRequirementChanges"),
    ({"additions": [{"seqNum": 50}], "specMoves": [{"seqNum": 12}]}, "refused", "the preview of proposal/x has unknown fields (specMoves)"),
    ({"additions": None, "modifications": []}, "refused", "the merge preview of proposal/x is malformed"),
    ({"additions": [{"seqNum": 50}], "stats": {"conflicts": "0"}}, "refused",
     "the merge preview of proposal/x has malformed stats"),
    ({"additions": [{"title": "no number"}], "modifications": []}, "refused",
     "the merge preview has a change without exactly one spec number"),
    ({"modifications": [{"seqNum": 50, "branch": {"seqNum": 99}}]}, "refused",
     "the merge preview has a change without exactly one spec number"),
    ({"modifications": [{"seqNum": "50"}]}, "refused", "the merge preview has a change without exactly one spec number"),
    ({"additions": ["50"]}, "refused", "the merge preview has a change that is not an object"),
])
def test_a_forbidden_preview_is_a_verdict_and_a_malformed_preview_is_not_checked(
        repo, server, monkeypatch, capsys, preview, kind, text):
    previewed(server, preview)
    code, out = run_main(repo, server, "Delivers: ANYM-50\nTiden branch: proposal/x", monkeypatch, capsys)
    assert code == 1 and "Passed." not in out
    if kind == "verdict":
        assert "the preview of proposal/x holds more than spec changes (" in out and text in out
        assert "problem(s):" in out and "Refused:" not in out and "does not name" not in out
    else:
        assert f"Refused: {text}" in out and "problem(s):" not in out
    assert not server.writes()


# The full merge-preview of a proposal branch as CLI 0.41.0 printed it on 2026-10-24: every list and stat present.
CLI_041_PREVIEW = {
    "additions": [{"seqNum": 50}], "modifications": [], "deletions": [], "testAdditions": [], "testModifications": [],
    "testDeletions": [], "componentAdditions": [], "componentModifications": [], "componentDeletions": [],
    "stats": {key: 0 for key in ("modifications", "deletions", "conflicts", "testAdditions", "testModifications",
                                 "testDeletions", "componentAdditions", "componentModifications",
                                 "componentDeletions")} | {"additions": 1},
    "intentSession": None, "blockingSessions": [], "hasRequirementChanges": True}
AUTO_MERGE = {"state": "in_progress", "reason": "no_merged_pr", "nextCheckAt": "2026-10-30T07:46:19Z"}


# spec: ANYM-445
@pytest.mark.parametrize("extra, kind, text", [
    ({}, "passed", ""),
    ({"testDuplicates": []}, "passed", ""),
    ({"autoMerge": None}, "passed", ""),
    ({"testDuplicates": [{"id": "t"}]}, "verdict", "testDuplicates"),
    ({"autoMerge": AUTO_MERGE}, "verdict", "autoMerge"),
    ({"testDuplicates": {"count": 1}}, "refused", "the merge preview of proposal/x is malformed"),
])
def test_the_cli_041_preview_keys_are_judged_not_refused_as_unknown(repo, server, monkeypatch, capsys, extra, kind,
                                                                      text):
    previewed(server, CLI_041_PREVIEW | extra, (50,))
    code, out = run_main(repo, server, "Delivers: ANYM-50\nTiden branch: proposal/x", monkeypatch, capsys)
    assert "unknown fields" not in out and not server.writes()
    if kind == "passed":
        assert code == 0 and "Branch preview: ANYM-50" in out, out
    elif kind == "verdict":
        assert code == 1 and f"the preview of proposal/x holds more than spec changes ({text})" in out
    else:
        assert code == 1 and f"Refused: {text}" in out


# spec: ANYM-445
@pytest.mark.parametrize("fault, reason", [
    ({"branch merge-preview": "exit"}, "Refused: tiden branch merge-preview exited 3"),
    ({"branch merge-preview": "garbage"}, "Refused: tiden branch merge-preview returned malformed JSON"),
    ({}, "Refused: tiden branch merge-preview returned no object"),
])
def test_a_failing_or_malformed_merge_preview_refuses_without_a_verdict_and_no_branch_reads_none(
        repo, server, monkeypatch, capsys, fault, reason):
    previewed(server, [])
    server.set(fault=fault)
    code, out = run_main(repo, server, "Delivers: ANYM-50\nTiden branch: proposal/x", monkeypatch, capsys)
    assert code == 1 and reason in out and "Passed." not in out and "problem(s):" not in out
    assert SECRET not in out and not server.writes()
    server.set(fault={"branch merge-preview": "exit"}, log=[])
    for body in ("Spec: ANYM-12", "Spec: ANYM-12\nTiden branch: review/y"):
        code, out = run_main(repo, server, body, monkeypatch, capsys)
        assert code == (1 if "review/y" in body else 0), out
    assert ("branch", "merge-preview") not in server.calls() and not server.writes()


# spec: ANYM-445
def test_the_check_and_the_sync_share_one_preview_parser():
    scripts = Path(pr_check.__file__).resolve().parent
    sources = [path.read_text(encoding="utf-8") for path in sorted(scripts.rglob("*.py"))]
    for name in ("preview_number", "preview_numbers"):
        assert sum(len(re.findall(rf"^def {name}\(", text, re.MULTILINE)) for text in sources) == 1
    for name in ("PREVIEW_CHANGES", "PREVIEW_EMPTY", "PREVIEW_OTHER"):
        assert sum(len(re.findall(rf"^{name}\s*=", text, re.MULTILINE)) for text in sources) == 1
        assert re.search(rf"^{name}\s*=", Path(pr_check.__file__).read_text(encoding="utf-8"), re.MULTILINE)
    # tiden_sync loads the script as the top-level module `tiden_pr_check`, the test as `scripts.tiden_pr_check`
    assert tiden_sync.preview_numbers.__module__.rsplit(".", 1)[-1] == "tiden_pr_check"
    assert not re.search(r"^\s*(?:import|from)\s+(?:scripts\.)?tiden_sync\b",
                         Path(pr_check.__file__).read_text(encoding="utf-8"), re.MULTILINE)


# spec: ANYM-405
@pytest.mark.parametrize("body, problem", [
    ("Spec: ANYM-1 ANYM-2", "is not a comma-separated list"),
    ("Delivers: #12", "is not a comma-separated list"),
    ("Spec:", "is not a comma-separated list"),
    ("Tiden branch: intent/2026-x", "is not a proposal/ or review/ branch name"),
    ("Tiden branch: proposal/a\nTiden branch: proposal/b", "more than one `Tiden branch:`"),
])
def test_malformed_body_lines_fail(body, problem):
    assert any(problem in error for error in pr_check.parse_body(body).errors)


# spec: ANYM-405
def test_body_lines_keep_order_and_delivers_comes_first():
    lines = pr_check.parse_body("x\r\n  Spec: ANYM-3, ANYM-1\nDelivers: ANYM-7,ANYM-3\nTiden branch: `proposal/a-b`\n"
                                "Spec: ANYM-3\nnot a Spec: ANYM-9 line")
    assert (lines.spec, lines.delivers, lines.branch, lines.errors) == ([3, 1], [7, 3], "proposal/a-b", [])
    assert lines.named == [7, 3, 1]


# spec: ANYM-405
@pytest.mark.parametrize("listing", [
    {"requirements": [spec(1)], "pagination": {"nextPageToken": "p2", "totalCount": 1}},
    {"requirements": [spec(1)], "pagination": {"nextPageToken": "", "totalCount": 2}},
    {"requirements": [spec(1), spec(1)], "pagination": {"nextPageToken": "", "totalCount": 2}},
    {"requirements": [{**spec(1), "sourceCount": 3}], "pagination": {"nextPageToken": "", "totalCount": 1}},
    {"requirements": [{**spec(1), "childrenCount": "0"}], "pagination": {"nextPageToken": "", "totalCount": 1}},
])
def test_an_incomplete_or_malformed_listing_refuses(repo, server, listing):
    server.set(listing=listing)
    with pytest.raises(pr_check.Refused, match="listing of main"):
        run(repo, server, {"app/core.py": "x = 5\n"})


# spec: ANYM-405
def test_a_failing_cli_refuses_without_echoing_its_output(repo, server):
    server.set(fault={"spec list": "garbage"})
    with pytest.raises(pr_check.Refused, match="malformed JSON") as error:
        run(repo, server, {"app/core.py": "x = 6\n"})
    assert SECRET not in str(error.value)


# spec: ANYM-405
@pytest.mark.parametrize("payload, token, reason", [
    ({"repository": {"full_name": "o/r"}}, "t", "not a pull request run"),
    ({"pull_request": {"head": {"repo": {"full_name": "fork/r"}}}, "repository": {"full_name": "o/r"}}, "t",
     "fork"),
    ({"pull_request": {"head": {"repo": None}}, "repository": {"full_name": "o/r"}}, "t", "fork"),
    ({"pull_request": {"head": {"repo": {"full_name": "o/r"}}}, "repository": {"full_name": "o/r"}}, "",
     "not configured"),
])
def test_forks_other_events_and_runs_without_a_token_skip(payload, token, reason, monkeypatch):
    monkeypatch.setenv("TIDEN_API_TOKEN", token)
    assert reason in pr_check.skip_reason(payload)


# spec: ANYM-405
def test_the_command_line_reads_the_event_and_writes_the_summary(repo, server, tmp_path):
    root, base = repo
    server.set(main=[spec(12)])
    commit(root, {"tests/test_new.py": "# spec: ANYM-12\ndef test_new():\n    pass\n"}, "change")
    event, summary = tmp_path / "event.json", tmp_path / "summary.md"
    event.write_text(json.dumps({"repository": {"full_name": "o/r"}, "pull_request": {
        "body": "Spec: ANYM-12", "base": {"sha": base}, "head": {"repo": {"full_name": "o/r"}}}}))
    env = {"GITHUB_EVENT_PATH": str(event), "GITHUB_STEP_SUMMARY": str(summary), "TIDEN_PRODUCT_ID": "product",
           "TIDEN_API_TOKEN": SECRET, "FAKE_TIDEN_STATE": str(server.path), "PATH": "/usr/bin:/bin"}
    completed = subprocess.run([sys.executable, ROOT_SCRIPT, "--root", str(root), "--cli", str(server.cli)],
                               capture_output=True, text=True, env=env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "test tests/test_new.py:2 test_new -> ANYM-12" in summary.read_text()
    server.set(fault={"spec list": "exit"})
    completed = subprocess.run([sys.executable, ROOT_SCRIPT, "--root", str(root), "--cli", str(server.cli)],
                               capture_output=True, text=True, env=env)
    assert completed.returncode == 1 and "Refused: tiden spec list exited 3" in completed.stdout
    assert SECRET not in completed.stdout + completed.stderr


# spec: ANYM-405
def test_the_no_spec_list_is_the_reviewed_empty_set():
    assert pr_check.NO_SPEC == ()


# spec: ANYM-405
def test_nested_test_classes_and_parenthesized_decorators_take_their_markers(repo, server):
    server.set(main=[spec(12)])
    code, out = run(repo, server, {
        "tests/test_n.py": "import pytest\n\n\nclass TestOuter:\n    class TestInner:\n        # spec: ANYM-12\n"
                           "        def test_deep(self):\n            pass\n\n    # spec: ANYM-12\n    @(\n"
                           "        pytest.mark.slow\n    )\n    def test_wrapped(self):\n        pass\n\n"
                           "    def helper(self):\n        pass\n\n\nclass Helper:\n    def test_ignored(self):\n"
                           "        pass\n"}, "Spec: ANYM-12")
    assert code == 0, out
    assert "test tests/test_n.py:7 TestOuter::TestInner::test_deep -> ANYM-12" in out
    assert "test tests/test_n.py:11 TestOuter::test_wrapped -> ANYM-12" in out
    assert "helper" not in out and "test_ignored" not in out


# spec: ANYM-405
def test_node_and_go_tests_are_found_by_plain_line_patterns_even_in_comments_strings_and_regexes(repo, server):
    server.set(main=[spec(12)])
    root, _ = repo
    commit(root, {"web/a.test.mjs": "await test('existing', () => {});\n"}, "node")
    code, out = run(repo, server, {
        "web/a.test.mjs": "await test('existing', () => {});\nconst rx = /[/*]/;\ntest('after regex', () => {});\n"
                          "/*\ntest('commented', () => {});\ntest('existing', () => {});\n*/\n"
                          "const text = \"it('quoted', () => {})\";\n// spec: ANYM-12\nawait test('awaited', () => {});\n"
                          "// test('line comment', () => {});\n",
        "pkg/m_test.go": "package pkg\n\n// spec: ANYM-12\nfunc TestMulti(\n\tt *testing.T,\n) {}\n\n"
                         "func TestMain(m *testing.M) {}\nfunc Testable() {}\n/*\nfunc TestCommented(t *testing.T) {}\n*/\n",
    }, "Spec: ANYM-12")
    assert code == 1
    assert "web/a.test.mjs:3 after regex: no marker" in out and "web/a.test.mjs:5 commented: no marker" in out
    assert "web/a.test.mjs:8 quoted: no marker" in out
    assert "web/a.test.mjs:10 awaited -> ANYM-12" in out and "existing" not in out
    assert "web/a.test.mjs:11 line comment: no marker" in out
    assert "pkg/m_test.go:4 TestMulti -> ANYM-12" in out and "pkg/m_test.go:11 TestCommented: no marker" in out
    assert "TestMain" not in out and "Testable" not in out


# spec: ANYM-405
def test_a_node_name_must_be_a_plain_literal_on_the_call_line_unless_that_line_existed(repo, server):
    server.set(main=[spec(12)])
    root, _ = repo
    commit(root, {"web/c.test.mjs": "const name = 'x';\ntest(name, () => {});\n"}, "node")
    code, out = run(repo, server, {"web/c.test.mjs": (
        "const name = 'x';\n// spec: ANYM-12\ntest(`plain ok`, () => {});\n// spec: ANYM-12\ntest('it\\'s', () => {});\n"
        "// spec: ANYM-12\ntest(`made ${name}`, () => {});\n// spec: ANYM-12\ntest('prefix' + 'new', () => {});\n"
        "// spec: ANYM-12\ntest(\n  'split',\n  () => {},\n);\n\n  test(name, () => {});\n"
        "// spec: ANYM-12\ntest('tab\\there', () => {});\n")}, "Spec: ANYM-12")
    assert code == 1
    assert "web/c.test.mjs:3 plain ok -> ANYM-12" in out
    for line in (5, 7, 9, 11, 18):
        assert f"web/c.test.mjs:{line} <" in out
    assert out.count("the test name is not a plain string literal") == 5
    assert "<test(name, () => {});>" not in out  # the existing call, moved and reindented, is not new


# spec: ANYM-405
def test_a_split_or_commented_call_is_never_hidden_and_an_unreadable_line_never_matches_a_title(repo, server):
    server.set(main=[spec(12)])
    root, _ = repo
    commit(root, {"web/k.test.mjs": "test(\"<test('prefix' + 'new', () => {});>\", () => {});\n"}, "node")
    code, out = run(repo, server, {
        "web/k.test.mjs": "test(\"<test('prefix' + 'new', () => {});>\", () => {});\ntest('prefix' + 'new', () => {});\n"
                          "test\n('newline', () => {});\nawait it // label\n('commented newline', () => {});\n"
                          "test /* label */ ('block comment', () => {});\n",
        "pkg/h_test.go": "package pkg\n\nfunc TestHidden /* label */(t *testing.T) {}\n",
    }, "Spec: ANYM-12")
    assert code == 1
    assert "web/k.test.mjs:2 <test('prefix' + 'new', () => {});>: the test name is not a plain" in out
    assert "web/k.test.mjs:3 <test>: the test call is split before its `(`" in out
    assert "web/k.test.mjs:5 <await it // label>: the test call is split" in out
    assert "web/k.test.mjs:7 block comment: no marker" in out and "pkg/h_test.go:3 TestHidden: no marker" in out


# spec: ANYM-405
def test_a_plain_title_is_non_empty_without_control_characters_and_may_hold_a_dollar(repo, server):
    server.set(main=[spec(12)])
    code, out = run(repo, server, {"web/d.test.mjs": "// spec: ANYM-12\ntest(`cost $5`, () => {});\n"
                                                     "// spec: ANYM-12\ntest('', () => {});\n"
                                                     "// spec: ANYM-12\ntest('tab\there', () => {});\n"},
                    "Spec: ANYM-12")
    assert code == 1
    assert "web/d.test.mjs:2 cost $5 -> ANYM-12" in out
    assert "web/d.test.mjs:4 <" in out and "web/d.test.mjs:6 <" in out
    assert out.count("the test name is not a plain string literal") == 2


# spec: ANYM-405
def test_a_go_name_after_a_line_break_and_a_call_after_an_open_comment_are_not_hidden(repo, server):
    server.set(main=[spec(12)])
    code, out = run(repo, server, {"pkg/s_test.go": "package pkg\n\nfunc\nTestSplit(t *testing.T) {}\n",
                                   "web/o.test.mjs": "test /* label\n*/ ('open comment', () => {});\n"},
                    "Spec: ANYM-12")
    assert code == 1
    assert "pkg/s_test.go:3 TestSplit: no marker" in out
    assert "web/o.test.mjs:1 <test /* label>: the test call is split" in out


# spec: ANYM-405
def test_many_block_comments_without_a_call_scan_in_linear_time():
    source = 'const description = "test ' + "/*x*/" * 40 + ' trailer";\nfunc Test' + "/*x*/" * 40 + "\n"
    completed = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'scripts'); import tiden_pr_check"
                                " as p; print(p.tests_in('p.test.mjs', sys.argv[1]), p.tests_in('p_test.go', sys.argv[1]))",
                                source], capture_output=True, text=True, timeout=20,
                               cwd=Path(ROOT_SCRIPT).parents[1])
    assert completed.stdout.strip() == "[] []", completed.stderr



def pr_check_job():
    import yaml
    return yaml.safe_load((Path(ROOT_SCRIPT).parents[1] / ".github/workflows/checks.yml").read_text())["jobs"]["tiden_pr"]


def outcome(tmp_path, lines, status):
    """Run the workflow's check step with the script replaced by one that prints `lines` and exits `status`."""
    step = next(step for step in pr_check_job()["steps"] if step.get("id") == "check")
    work = tmp_path / f"step-{len(list(tmp_path.glob('step-*')))}"
    (work / "scripts").mkdir(parents=True)
    (work / "bin").mkdir()
    (work / "bin/python").symlink_to(sys.executable)
    (work / "scripts/tiden_pr_check.py").write_text(
        f"import sys\nprint({json.dumps(chr(10).join(lines))})\nsys.exit({status})\n")
    output = work / "output"
    completed = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=work, capture_output=True, text=True, env={
        "RUNNER_TEMP": str(work), "GITHUB_OUTPUT": str(output), "PATH": f"{work / 'bin'}:/usr/bin:/bin"})
    assert completed.returncode == 0, completed.stderr
    return output.read_text().strip()


# spec: ANYM-405
def test_only_a_verdict_fails_the_pr_check_job_and_anything_else_warns(repo, server, tmp_path):
    job = pr_check_job()
    steps = {step.get("id"): step for step in job["steps"]}
    assert [step.get("id") for step in job["steps"] if "TIDEN_API_TOKEN" in str(step.get("env"))] == ["check"]
    assert [step.get("name") for step in job["steps"] if step.get("continue-on-error") is not True] == \
        ["Fail on the verdict"]
    assert steps["check"]["if"] == "steps.install.outcome == 'success'"
    assert next(step for step in job["steps"] if step.get("name") == "Fail on the verdict")["if"] == \
        "steps.check.outputs.outcome == 'failed'"
    assert "outcome != 'passed' && steps.check.outputs.outcome != 'failed'" in steps["warn"]["if"]
    assert sum(step["timeout-minutes"] for step in job["steps"]) < job["timeout-minutes"]
    receipt = next(step for step in job["steps"] if step.get("name") == "Retain the Tiden PR check receipt")
    assert receipt["if"] == steps["warn"]["if"]
    for printed in (None, "## Tiden PR check\nRefused: tiden spec list exited 3\n"):  # killed before any output, or not
        work = tmp_path / f"warn-{printed is None}"
        work.mkdir()
        if printed:
            (work / "tiden-pr-check.txt").write_text(printed)
        completed = subprocess.run(["bash", "-e", "-c", steps["warn"]["run"]], capture_output=True, text=True, env={
            "RUNNER_TEMP": str(work), "GITHUB_STEP_SUMMARY": str(work / "summary"), "PATH": "/usr/bin:/bin",
            "PREPARE": "checkout success, Python success", "INSTALL": "success", "CHECK": "failure"})
        assert completed.returncode == 0 and "::warning title=Tiden PR check not run::" in completed.stdout
        assert json.loads((work / "tiden-pr-check/tiden-pr-check.json").read_text()) == {
            "checked": False, "prepare": "checkout success, Python success", "install": "success", "check": "failure",
            "reason": "Refused: tiden spec list exited 3" if printed else None}

    root, base = repo
    commit(root, {"tests/test_new.py": "def test_new():\n    pass\n"}, "change")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"repository": {"full_name": "o/r"}, "pull_request": {
        "body": "Spec: ANYM-12", "base": {"sha": base}, "head": {"repo": {"full_name": "o/r"}}}}))
    env = {"GITHUB_EVENT_PATH": str(event), "TIDEN_PRODUCT_ID": "product", "TIDEN_API_TOKEN": SECRET,
           "FAKE_TIDEN_STATE": str(server.path), "PATH": "/usr/bin:/bin"}

    def real(cli=str(server.cli), **state):
        server.set(main=[spec(12)], **state)
        completed = subprocess.run([sys.executable, ROOT_SCRIPT, "--root", str(root), "--cli", cli],
                                   capture_output=True, text=True, env=env)
        return completed.stdout.splitlines(), completed.returncode

    assert outcome(tmp_path, *real(fault={})) == "outcome=failed"
    assert outcome(tmp_path, *real(fault={"spec list": "exit"})) == "outcome=unchecked"
    assert outcome(tmp_path, *real(fault={"spec list": "garbage"})) == "outcome=unchecked"
    assert outcome(tmp_path, *real(cli=str(tmp_path / "missing-cli"), fault={})) == "outcome=unchecked"
    commit(root, {"tests/test_new.py": "# spec: ANYM-12\ndef test_new():\n    pass\n"}, "marked")
    assert outcome(tmp_path, *real(fault={})) == "outcome=passed"
    commit(root, {"tests/test_bad.py": "def test_bad(:\n    pass\n"}, "unparseable")
    lines, status = real(fault={})
    assert lines[-1].startswith("Refused: tests/test_bad.py does not parse")
    assert outcome(tmp_path, lines, status) == "outcome=failed"
    commit(root, {"tests/test_bad.py": "def test_bad():\n    pass\n", "tests/test_nul.py": "x = 1\0\n"}, "nul")
    lines, status = real(fault={})
    assert lines[-1].startswith("Refused: tests/test_nul.py does not parse") and lines[-1].endswith("line None)")
    assert outcome(tmp_path, lines, status) == "outcome=failed"
    assert outcome(tmp_path, ["Refused: a changed path is not valid UTF-8"], 1) == "outcome=failed"
    for line in ("Refused: unexpected UnicodeDecodeError", "Refused: unexpected FileNotFoundError",
                 "Refused: the spec listing of main is incomplete", "Refused: git diff exited 128",
                 "Refused: tiden spec list timed out", "Refused: tests/x.py does not parse"):
        assert outcome(tmp_path, ["## Tiden PR check", line], 1) == "outcome=unchecked", line
    verdict = "Fix the pull request body or the markers (TIDEN-CI.md, PR check), then rerun."
    assert outcome(tmp_path, [verdict, "Refused: tiden spec list exited 3"], 1) == "outcome=unchecked"
    assert outcome(tmp_path, [verdict], 2) == "outcome=unchecked"
    assert outcome(tmp_path, [verdict], 137) == "outcome=unchecked"


# spec: ANYM-405
def test_a_test_merge_checkout_diffs_from_the_merged_base_tip_not_the_stale_event_base(repo, server, tmp_path):
    root, base = repo
    server.set(main=[spec(12)])
    git(root, "checkout", "-q", "-b", "feature")
    tip = commit(root, {"tests/test_new.py": "# spec: ANYM-12\ndef test_new():\n    pass\n"}, "change")
    git(root, "checkout", "-q", "main")
    commit(root, {"tests/test_later.py": "def test_later():\n    pass\n"}, "a later base commit")
    # GitHub's test merge: the base tip is the first parent, the pull request head the second.
    git(root, "merge", "-q", "--no-ff", "--no-edit", "feature")
    event = tmp_path / "event.json"
    payload = {"repository": {"full_name": "o/r"}, "pull_request": {
        "body": "Spec: ANYM-12", "base": {"sha": base}, "head": {"sha": tip, "repo": {"full_name": "o/r"}}}}
    env = {"GITHUB_EVENT_PATH": str(event), "TIDEN_PRODUCT_ID": "product", "TIDEN_API_TOKEN": SECRET,
           "FAKE_TIDEN_STATE": str(server.path), "PATH": "/usr/bin:/bin"}

    def real():
        event.write_text(json.dumps(payload))
        return subprocess.run([sys.executable, ROOT_SCRIPT, "--root", str(root), "--cli", str(server.cli)],
                              capture_output=True, text=True, env=env)

    completed = real()
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Changed files 1," in completed.stdout and "test_later" not in completed.stdout
    payload["pull_request"]["head"]["sha"] = base  # HEAD is no test merge of this pull request: the event's base
    completed = real()
    assert completed.returncode == 1 and "test tests/test_later.py:1 test_later" in completed.stdout


# spec: ANYM-405
def test_a_unicode_line_separator_in_a_test_file_does_not_shift_the_markers(repo, server):
    server.set(main=[spec(12)])
    code, out = run(repo, server, {
        "tests/test_sep.py": "NOTE = 'a b'\n\x0c\n# spec: ANYM-12\ndef test_py():\n    pass\n",
        "pkg/sep_test.go": "package pkg\n\n// a b\n// spec: ANYM-12\nfunc TestGo(t *testing.T) {}\n",
        "web/sep.test.mjs": "// a b\n// spec: ANYM-12\ntest('node', () => {});\n",
        "tests/test_crlf.py": "# spec: ANYM-12\r\ndef test_crlf():\r\n    pass\r\n",
    }, "Spec: ANYM-12")
    assert code == 0, out
    assert "test_py -> ANYM-12" in out and "TestGo -> ANYM-12" in out and "node -> ANYM-12" in out
    assert "test_crlf -> ANYM-12" in out


# spec: ANYM-405
def test_line_breaks_follow_each_language_so_no_marker_authorizes_an_unrelated_test(repo, server):
    server.set(main=[spec(12)])
    code, out = run(repo, server, {
        "tests/test_cr.py": "A = 1\rB = 2\rC = 3\ndef test_unmarked():\n    pass\n# spec: ANYM-12\n",
        "web/ls.test.mjs": "// spec: ANYM-12\nconst x = 1; test('unmarked', () => {});\n",
    }, "Spec: ANYM-12")
    assert code == 1
    assert "tests/test_cr.py:4 test_unmarked: no marker" in out
    assert "web/ls.test.mjs:3 unmarked: no marker" in out and "-> ANYM-12" not in out
    code, out = run(repo, server, {
        "tests/test_cr_ok.py": "A = 1\rB = 2\r# spec: ANYM-12\rdef test_cr_ok():\r    pass\r",
        "web/ls_ok.test.mjs": "const x = 1; // spec: ANYM-12 test('ok', () => {});\n",
    }, "Spec: ANYM-12")
    assert code == 0, out
    assert "test_cr_ok -> ANYM-12" in out and "ok -> ANYM-12" in out


GATE_FIXTURE = json.loads((Path(ROOT_SCRIPT).parents[1] / "tests/fixtures/tiden-gate-branch-041.json").read_text())
VERDICT = "Fix the pull request body or the markers (TIDEN-CI.md, PR check), then rerun."


class Clock:
    """A fake monotonic clock: the first reading is the script's start, every later one is `later` seconds on."""
    def __init__(self, later=0.0):
        self.later, self.readings = later, 0

    def __call__(self):
        self.readings += 1
        return 0.0 if self.readings == 1 else self.later


def gated(server, monkeypatch, gate=None, later=0.0, fault=None):
    """proposal/x holds ANYM-527 (Backlog) and ANYM-12 is on main; the gate replies `gate` ({exit, reply})."""
    previewed(server, {"additions": [{"seqNum": 527}], "modifications": []}, (527,))
    server.set(gate=gate, fault=fault or {})
    monkeypatch.setattr(pr_check, "clock", Clock(later))


def gate_calls(server):
    return [entry for entry in server.state()["log"] if entry[:2] == ["gate", "check"]]


PASSING = "Delivers: ANYM-527\nTiden branch: proposal/x"
FAILING = "Delivers: ANYM-527\nSpec: ANYM-99\nTiden branch: proposal/x"
PASS_REPLY = {"exit": 0, "reply": {"scope": "VERDICT_SCOPE_BRANCH", "status": "VERDICT_STATUS_PASS", "subjects": [
    {"subjectType": "component", "status": "VERDICT_STATUS_PASS", "requirements": [
        {"display": "ANYM-527", "state": "verified", "touched": "changed"},
        {"display": "ANYM-12", "state": "verified", "touched": "neighbour"}]},
    {"subjectType": "feature", "status": "VERDICT_STATUS_PASS", "requirements": [
        {"display": "ANYM-527", "state": "verified", "touched": "changed"}]}]}}
BLOCKED_REPLY = {"exit": 3, "reply": {"scope": "VERDICT_SCOPE_BRANCH", "status": "VERDICT_STATUS_BLOCKED",
                                      "nextAction": "fix " + SECRET, "subjects": [{"requirements": [
                                          {"display": "ANYM-527", "state": "failing", "touched": "changed",
                                           "nextAction": "rerun " + SECRET}]}]}}


# spec: ANYM-541
@pytest.mark.parametrize("body, code, closing", [(PASSING, 0, "Passed."), (FAILING, 1, VERDICT)],
                         ids=["check-passes", "check-fails"])
@pytest.mark.parametrize("gate, later, fault, rows", [
    (PASS_REPLY, 0.0, None, ["Tiden branch gate: PASS (advisory, never changes this check)",
                             "- ANYM-12 Active verified (neighbour)", "- ANYM-527 Backlog verified"]),
    ({"exit": GATE_FIXTURE["exit"], "reply": GATE_FIXTURE["reply"]}, 0.0, None,
     ["Tiden branch gate: NOT VERIFIED (advisory, never changes this check)",
      "- ANYM-527 Backlog no_test (planned)"]),
    (BLOCKED_REPLY, 0.0, None, ["Tiden branch gate: BLOCKED (advisory, never changes this check)",
                                "- ANYM-527 Backlog failing"]),
    (None, 0.0, {"gate check": "garbage"}, ["Tiden branch gate: not checked (not JSON)"]),
    (None, 0.0, {"gate check": "exit"}, ["Tiden branch gate: not checked (not JSON)"]),
    ({"exit": 1, "reply": PASS_REPLY["reply"]}, 0.0, None, ["Tiden branch gate: not checked (exit 1)"]),
    ({"exit": 0, "reply": {"scope": "VERDICT_SCOPE_MAIN", "status": "VERDICT_STATUS_PASS", "subjects": []}}, 0.0, None,
     ["Tiden branch gate: not checked (unexpected shape)"]),
    ({"exit": 3, "reply": {"scope": "VERDICT_SCOPE_BRANCH", "status": "VERDICT_STATUS_BLOCKED", "subjects": [
        {"requirements": [{"display": "ANYM-527", "state": "failing\n" + SECRET}]}]}}, 0.0, None,
     ["Tiden branch gate: not checked (unexpected shape)"]),
    (PASS_REPLY, 411.0, None, ["Tiden branch gate: not checked (no time)"]),
], ids=["passed", "no-test-exit-3", "blocked", "garbage", "exit-3-not-json", "exit-1", "main-scope",
        "bad-state", "no-time"])
def test_the_branch_gate_prints_advisory_rows_before_the_unchanged_last_line(
        repo, server, monkeypatch, body, code, closing, gate, later, fault, rows):
    gated(server, monkeypatch, gate, later, fault)
    got, out = run(repo, server, {"README.md": "text\n"}, body)
    lines = out.splitlines()
    assert got == code and lines[-1] == closing, out
    assert lines[-1 - len(rows):-1] == rows, out
    assert SECRET not in out and "nextAction" not in out and "fix " not in out
    assert gate_calls(server) == ([] if later else [
        ["gate", "check", "--branch", "proposal/x", "--product-id", "product", "--format", "json"]])
    assert not server.writes()


# spec: ANYM-541
@pytest.mark.parametrize("body, code, closing", [(PASSING, 0, "Passed."), (FAILING, 1, VERDICT)],
                         ids=["check-passes", "check-fails"])
def test_a_branch_gate_past_its_timeout_is_not_checked_and_keeps_the_outcome(
        repo, server, monkeypatch, body, code, closing):
    gated(server, monkeypatch, PASS_REPLY, fault={"gate check": "sleep"})
    monkeypatch.setattr(pr_check, "GATE_TIMEOUT", 1)
    got, out = run(repo, server, {"README.md": "text\n"}, body)
    assert got == code and out.splitlines()[-2:] == ["Tiden branch gate: not checked (timeout)", closing], out


# spec: ANYM-541
@pytest.mark.parametrize("elapsed, budget", [(0, 60), (300, 60), (380, 40), (405, 15), (410, 10), (410.5, None),
                                             (480, None)])
def test_the_branch_gate_timeout_is_bounded_by_the_time_left_in_the_step(elapsed, budget):
    assert pr_check.gate_budget(elapsed) == budget
    step = next(step for step in pr_check_job()["steps"] if step.get("id") == "check")
    assert pr_check.STEP_LIMIT == step["timeout-minutes"] * 60


# spec: ANYM-541
@pytest.mark.parametrize("later", [400.0, 411.0, 600.0])
def test_a_failing_check_near_its_step_deadline_is_still_a_verdict_of_the_workflow(
        repo, server, monkeypatch, capsys, tmp_path, later):
    gated(server, monkeypatch, {"exit": GATE_FIXTURE["exit"], "reply": GATE_FIXTURE["reply"]}, later)
    code, out = run_main(repo, server, FAILING, monkeypatch, capsys)
    assert code == 1 and out.splitlines()[-1] == VERDICT
    assert outcome(tmp_path, out.splitlines(), code) == "outcome=failed"
    assert len(gate_calls(server)) == (1 if later == 400.0 else 0)


# spec: ANYM-541
def test_no_tiden_branch_line_makes_no_gate_call(repo, server, monkeypatch):
    gated(server, monkeypatch, PASS_REPLY)
    code, out = run(repo, server, {"README.md": "text\n"}, "Spec: ANYM-12")
    assert code == 0 and out.splitlines()[-1] == "Passed." and "Tiden branch gate" not in out
    code, out = run(repo, server, {"README.md": "more\n"}, "Spec: ANYM-12\nTiden branch: feature/x")
    assert code == 1 and "Tiden branch gate" not in out
    assert gate_calls(server) == []
