"""The Tiden PR check against temporary Git repositories and a fake Tiden CLI; it never calls Tiden."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

import scripts.tiden_pr_check as pr_check
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
    assert "web/b.test.js:2 adds 'quoted' title -> ANYM-12" in out
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
def test_commented_or_quoted_test_calls_are_not_tests_and_await_and_multiline_calls_are(repo, server):
    server.set(main=[spec(12)])
    root, _ = repo
    commit(root, {"web/a.test.mjs": "import test from 'node:test';\nawait test('existing', () => {});\n"}, "node")
    code, out = run(repo, server, {"web/a.test.mjs": "import test from 'node:test';\nawait test('existing', () => {});\n"
                                                     "/*\n// spec: ANYM-12\ntest('existing', () => {});\n*/\n"
                                                     "const text = \"test('quoted', () => {})\";\n"
                                                     "// test('line comment', () => {});\n"})
    assert code == 0, out
    assert "added tests 0" in out
    code, out = run(repo, server, {"web/b.test.mjs": "// spec: ANYM-12\nawait test('awaited', () => {});\n\n"
                                                     "test(\n  'multiline',\n  () => {},\n);\n",
                                   "pkg/m_test.go": "package pkg\n\n// spec: ANYM-12\nfunc TestMulti(\n\tt *testing.T,\n) {}\n"
                                                    "\n/*\nfunc TestCommented(t *testing.T) {}\n*/\n"},
                    "Spec: ANYM-12")
    assert code == 1
    assert "web/b.test.mjs:2 awaited -> ANYM-12" in out and "web/b.test.mjs:4 multiline: no marker" in out
    assert "pkg/m_test.go:4 TestMulti -> ANYM-12" in out and "TestCommented" not in out


# spec: ANYM-405
def test_javascript_titles_are_decoded_and_a_non_literal_name_fails_unless_it_existed(repo, server):
    server.set(main=[spec(12)])
    root, _ = repo
    commit(root, {"web/c.test.mjs": "const name = 'x';\ntest(name, () => {});\n"}, "node")
    code, out = run(repo, server, {
        "web/c.test.mjs": "const name = 'x';\n\n// spec: ANYM-12\ntest(\"tab\\there \\u00e9 \\x41\", () => {});\n"
                          "test(name, () => {});\n// spec: ANYM-12\ntest(`made ${name}`, () => {});\n"}, "Spec: ANYM-12")
    assert code == 1
    assert "web/c.test.mjs:4 tab\\there \\xe9 A -> ANYM-12" in out  # shown escaped in the log
    assert pr_check.tests_in("web/c.test.mjs", "test('it\\'s \\u{1F600}\\\n done', () => {});\n") == [
        ("it's \U0001F600 done", 0, "")]
    assert "web/c.test.mjs:7 <`made ${name}`>: the test name is not a plain string literal" in out
    assert "<name>" not in out  # the moved existing call is not new


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
def test_regex_literals_neither_hide_nor_make_tests_and_an_unterminated_comment_refuses(repo, server):
    server.set(main=[spec(12)])
    code, out = run(repo, server, {"web/r.test.mjs": "import test from 'node:test';\nconst rx = /[/*]/;\n"
                                                     "test('after class', () => {});\nconst tick = /`/, ratio = a / b / c;\n"
                                                     "test('after tick', () => {});\nconst fake = /test('fake')/;\n"
                                                     "if (ok) { done(); } /x/.test(value);\n"}, "Spec: ANYM-12")
    assert code == 1
    assert "web/r.test.mjs:3 after class: no marker" in out and "web/r.test.mjs:5 after tick: no marker" in out
    assert "fake" not in out
    with pytest.raises(pr_check.Refused, match="does not parse"):
        run(repo, server, {"web/u.test.mjs": "/* open\ntest('hidden', () => {});\n"}, "Spec: ANYM-12")


# spec: ANYM-405
def test_a_literal_that_starts_a_computed_name_is_not_its_name_and_surrogate_pairs_join(repo, server):
    server.set(main=[spec(12)])
    root, _ = repo
    commit(root, {"web/p.test.mjs": "test('prefix', () => {});\n"}, "node")
    code, out = run(repo, server, {"web/p.test.mjs": "test('prefix', () => {});\ntest('prefix' + 'new', () => {});\n"
                                                     "// spec: ANYM-12\ntest('\\uD83D\\uDE00', () => {});\n"},
                    "Spec: ANYM-12")
    assert code == 1
    assert "web/p.test.mjs:2 <'prefix' + 'new'>: the test name is not a plain string literal" in out
    assert pr_check.tests_in("web/p.test.mjs", "test('\\uD83D\\uDE00', () => {});\n") == [("\U0001F600", 0, "")]
    assert "web/p.test.mjs:4 \\U0001f600 -> ANYM-12" in out
