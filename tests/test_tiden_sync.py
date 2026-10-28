"""The main-push Tiden sync against temporary Git repositories, a fake Tiden CLI and a fake `gh`; it never calls Tiden
or GitHub."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

import scripts.tiden_sync as tiden_sync
from tiden_fake import SECRET, Server, anchor, commit, git, repository, spec

SCRIPT = str(Path(__file__).resolve().parents[1] / "scripts/tiden_sync.py")
TEST_FILE = "# spec: ANYM-12\ndef test_new():\n    pass\n\n\n# spec: ANYM-12, ANYM-50\ndef test_two():\n    pass\n"


@pytest.fixture
def server(tmp_path, monkeypatch):
    return Server(tmp_path, monkeypatch)


@pytest.fixture
def root(tmp_path):
    root = repository(tmp_path / "work")
    commit(root, {"app/core.py": "x = 1\n"}, "base")
    return root


def merged(root, server, files, body, number=7, **pull):
    sha = commit(root, files, f"feat: change (#{number})")
    server.set(pulls={str(number): {"number": number, "merged": True, "merge_commit_sha": sha,
                                    "base": {"ref": "main"}, "body": body, **pull}})
    return sha


def run(root, server, sha=None, pr=None, dry=False):
    lines = []
    try:
        code = tiden_sync.run(root, sha, pr, "GoSync-Inc/deedkit", str(server.gh), str(server.cli), "product",
                              lines.append, dry)
    except tiden_sync.StepFailed as error:
        lines.append(f"Failed at step {error.step}: {error}")
        code = 1
    return code, "\n".join(lines)


def case(signature, path, title):
    return {"id": f"t-{signature}", "kind": "case", "signature": signature, "filePath": path, "title": title}


def full_server(server):
    server.set(main=[spec(12, sources=[anchor("app/core.py")])],
               branch_specs={"proposal/x": [spec(50, status="Backlog", content="Criteria.", branch=True)]},
               branches=[{"name": "main", "status": "open"}, {"name": "proposal/x", "status": "open"}],
               previews={"proposal/x": {"additions": [{"seqNum": 50}], "modifications": [], "deletions": [],
                                        "stats": {"conflicts": 0}, "blockingSessions": []}},
               tests=[case("tests.test_new::test_new", "tests/test_new.py", "test_new"),
                      case("tests.test_new::test_two[a]", "tests/test_new.py", "test_two[a]"),
                      case("tests.test_new::test_two[b]", "tests/test_new.py", "test_two[b]"),
                      case("tests.test_newer::test_new", "tests/test_newer.py", "test_new")])


# spec: ANYM-405
def test_a_merged_pull_request_merges_its_card_links_its_tests_anchors_its_files_and_delivers(root, server):
    full_server(server)
    merged(root, server, {"app/core.py": "x = 2\n", "app/new.py": "y\n", "tests/test_new.py": TEST_FILE,
                          "notes/x.md": "doc\n"},
           "Delivers: ANYM-50\nSpec: ANYM-12\nTiden branch: proposal/x\n")
    code, out = run(root, server)
    assert code == 0, out
    state = server.state()
    assert {"name": "proposal/x", "status": "merged"} in state["branches"]
    assert state["links"] == {"t-tests.test_new::test_new": ["m-0012-0000-0000-0000-000000000000"],
                              "t-tests.test_new::test_two[a]": ["m-0012-0000-0000-0000-000000000000",
                                                                "b-0050-0000-0000-0000-000000000000"],
                              "t-tests.test_new::test_two[b]": ["m-0012-0000-0000-0000-000000000000",
                                                                "b-0050-0000-0000-0000-000000000000"]}
    card = server.main(50)
    assert card["status"] == "Done"
    assert [source["repoPath"] for source in card["sources"]] == ["app/new.py"]
    assert card["sources"][0]["metadata"] == {"added_by": "tiden_sync", "commit_sha": git(root, "rev-parse", "HEAD"),
                                              "pull_request": "#7"}
    assert "merge: proposal/x merged (ANYM-50)" in out and "status: ANYM-50 Backlog -> Done" in out
    for entry in server.writes():  # the commands that reject --product-id never receive it
        if entry[:2] in (["spec", "update"], ["test", "link-spec"]):
            assert "--product-id" not in entry
    # A rerun finds everything in place and writes nothing.
    before = len(server.writes())
    code, out = run(root, server)
    assert code == 0, out
    assert len(server.writes()) == before
    assert "merge: proposal/x is merged already" in out and "status: ANYM-50 is Done already" in out


# spec: ANYM-405
def test_a_dry_run_reads_everything_and_writes_nothing(root, server):
    full_server(server)
    merged(root, server, {"app/new.py": "y\n", "tests/test_new.py": TEST_FILE},
           "Delivers: ANYM-50\nTiden branch: proposal/x\n")
    code, out = run(root, server, pr=7, dry=True)
    assert code == 0, out
    assert not server.writes()
    assert "would merge proposal/x" in out and "Dry run: nothing was written." in out


# spec: ANYM-405
@pytest.mark.parametrize("pull, step", [
    ({"merged": False}, "is not merged"),
    ({"merge_commit_sha": "0" * 40}, "merged as 0000"),
    ({"base": {"ref": "release"}}, "did not merge into main"),
])
def test_the_pull_request_must_be_merged_into_main_as_this_commit(root, server, pull, step):
    full_server(server)
    merged(root, server, {"app/new.py": "y\n"}, "Spec: ANYM-12", **pull)
    code, out = run(root, server)
    assert code == 1 and "Failed at step pr" in out and step in out
    assert not server.writes()


# spec: ANYM-405
def test_a_commit_without_a_pull_request_number_syncs_nothing_and_a_wrong_number_fails(root, server):
    commit(root, {"app/new.py": "y\n"}, "direct change")
    code, out = run(root, server)
    assert code == 0 and "names no pull request" in out and not server.state()["log"]
    merged(root, server, {"app/other.py": "z\n"}, "Spec: ANYM-12", number=8)
    code, out = run(root, server, sha="HEAD", pr=9)
    assert code == 1 and "names #8, not #9" in out


# spec: ANYM-405
@pytest.mark.parametrize("preview, reason", [
    ({"additions": [{"seqNum": 50}, {"seqNum": 51}], "modifications": []}, "ANYM-51, which the body does not name"),
    ({"additions": [], "modifications": [{"branch": {"seqNum": 77}}]}, "ANYM-77"),
    ({"additions": [], "modifications": []}, "changes no spec"),
    ({"additions": [{"seqNum": 50}], "modifications": [], "testAdditions": [{"id": "t"}]}, "testAdditions"),
    ({"additions": [{"seqNum": 50}], "modifications": [], "deletions": [{"seqNum": 12}]}, "deletions"),
    ({"additions": [{"seqNum": 50}], "modifications": [], "stats": {"conflicts": 1}}, "conflicts"),
    ({"additions": [{"seqNum": 50}], "modifications": [], "blockingSessions": [{"sessionId": "s"}]}, "blocking"),
    ({"additions": [{"title": "no number"}], "modifications": []}, "without exactly one spec number"),
    ({"additions": None, "modifications": []}, "malformed"),
])
def test_a_mixed_or_unclear_preview_is_never_merged_and_stops_the_sync(root, server, preview, reason):
    full_server(server)
    server.set(previews={"proposal/x": preview})
    merged(root, server, {"app/new.py": "y\n"}, "Delivers: ANYM-50\nTiden branch: proposal/x")
    code, out = run(root, server)
    assert code == 1 and "Failed at step merge" in out and reason in out
    assert not server.writes()


# spec: ANYM-405
def test_a_branch_that_is_not_open_fails_the_merge_step(root, server):
    full_server(server)
    server.set(branches=[{"name": "main", "status": "open"}])
    merged(root, server, {"app/new.py": "y\n"}, "Delivers: ANYM-50\nTiden branch: proposal/x")
    code, out = run(root, server)
    assert code == 1 and "proposal/x is missing, not open" in out and not server.writes()


# spec: ANYM-405
def test_existing_links_are_kept_and_never_duplicated(root, server):
    full_server(server)
    server.set(links={"t-tests.test_new::test_new": ["m-0012-0000-0000-0000-000000000000"]})
    merged(root, server, {"tests/test_new.py": "# spec: ANYM-12\ndef test_new():\n    pass\n"}, "Spec: ANYM-12")
    code, out = run(root, server)
    assert code == 0, out
    assert server.state()["links"] == {"t-tests.test_new::test_new": ["m-0012-0000-0000-0000-000000000000"]}
    assert not server.writes()


# spec: ANYM-405
def test_a_test_without_an_imported_case_or_an_unpersisted_link_fails_the_links_step_and_go_is_reported(root, server):
    full_server(server)
    server.set(fault={"test link-spec": "noop"})
    merged(root, server, {"tests/test_new.py": "# spec: ANYM-12\ndef test_new():\n    pass\n\n\n"
                                               "# spec: ANYM-12\ndef test_missing():\n    pass\n",
                          "pkg/x_test.go": "package pkg\n\n// spec: ANYM-12\nfunc TestGo(t *testing.T) {}\n",
                          "web/a.test.mjs": "// spec: ANYM-99\ntest('unknown spec', () => {});\n"}, "Spec: ANYM-12")
    code, out = run(root, server)
    assert code == 1
    assert "Failed at step links" in out
    assert "test_missing has no imported case" in out
    assert "read-back: tests.test_new::test_new is not linked to ANYM-12" in out
    assert "unknown spec names ANYM-99, not on Tiden main" in out
    assert "pkg/x_test.go TestGo lies in no Go module" in out


# spec: ANYM-421
def test_a_marked_go_test_links_its_leaf_cases_by_package_and_a_test_of_another_package_does_not(root, server):
    full_server(server)
    package = "example.com/svc/pkg"
    server.set(tests=[case(f"{package}::TestRoute/one", "a_test.go", "TestRoute/one"),
                      case(f"{package}::TestRoute/two", "a_test.go", "TestRoute/two"),
                      case(f"{package}::TestRouteMore", "a_test.go", "TestRouteMore"),
                      case("example.com/svc::TestRoute", "b_test.go", "TestRoute"),
                      case("example.com/svc/pkgx::TestRoute", "c_test.go", "TestRoute"),
                      case(f"{package}::TestFlat", "a_test.go", "TestFlat")])
    merged(root, server, {"svc/go.mod": "module example.com/svc\n\ngo 1.26\n",
                          "svc/pkg/a_test.go": "package pkg\n\n// spec: ANYM-12\nfunc TestRoute(t *testing.T) {}\n\n"
                                               "// spec: ANYM-12\nfunc TestFlat(t *testing.T) {}\n"}, "Spec: ANYM-12")
    code, out = run(root, server)
    assert code == 0, out
    assert server.state()["links"] == {f"t-{package}::TestRoute/one": ["m-0012-0000-0000-0000-000000000000"],
                                       f"t-{package}::TestRoute/two": ["m-0012-0000-0000-0000-000000000000"],
                                       f"t-{package}::TestFlat": ["m-0012-0000-0000-0000-000000000000"]}
    assert tiden_sync.go_package(root, "HEAD", "svc/go_test.go") == "example.com/svc"
    assert tiden_sync.go_package(root, "HEAD", "app/x_test.go") is None


# spec: ANYM-421
def test_new_subtests_of_an_existing_marked_go_test_are_linked_when_its_file_changes(root, server):
    full_server(server)
    package = "example.com/svc/pkg"
    old = "package pkg\n\n// spec: ANYM-12\nfunc TestRoute(t *testing.T) {}\n\nfunc TestPlain(t *testing.T) {}\n"
    commit(root, {"svc/go.mod": "module example.com/svc\n\ngo 1.26\n", "svc/pkg/a_test.go": old}, "base")
    server.set(tests=[case(f"{package}::TestRoute/one", "a_test.go", "TestRoute/one"),
                      case(f"{package}::TestRoute/two", "a_test.go", "TestRoute/two"),
                      case(f"{package}::TestPlain/one", "a_test.go", "TestPlain/one"),
                      case(f"{package}::TestGone", "a_test.go", "TestGone")])
    merged(root, server, {"svc/pkg/a_test.go": old.replace("{}\n\nfunc TestPlain", "{ /* two rows */ }\n\nfunc TestPlain")},
           "Spec: ANYM-12")
    code, out = run(root, server)
    assert code == 0, out
    assert server.state()["links"] == {f"t-{package}::TestRoute/one": ["m-0012-0000-0000-0000-000000000000"],
                                       f"t-{package}::TestRoute/two": ["m-0012-0000-0000-0000-000000000000"]}


# spec: ANYM-421
def test_a_marked_comment_copy_of_an_unmarked_go_test_links_nothing(root, server):
    full_server(server)
    package = "example.com/svc/pkg"
    old = "package pkg\n\nfunc TestRoute(t *testing.T) {}\n"
    commit(root, {"svc/go.mod": "module example.com/svc\n\ngo 1.26\n", "svc/pkg/a_test.go": old}, "base")
    server.set(tests=[case(f"{package}::TestRoute/one", "a_test.go", "TestRoute/one")])
    merged(root, server, {"svc/pkg/a_test.go": old + "\n/* example\n// spec: ANYM-12\nfunc TestRoute(t *testing.T) {}\n*/\n"},
           "Spec: ANYM-12")
    code, out = run(root, server)
    assert code == 0, out
    assert not server.state().get("links")


# spec: ANYM-421
def test_kept_go_tests_stop_being_rechecked_when_their_time_budget_is_spent(root, server, monkeypatch):
    full_server(server)
    package = "example.com/svc/pkg"
    old = "package pkg\n\n// spec: ANYM-12\nfunc TestRoute(t *testing.T) {}\n"
    commit(root, {"svc/go.mod": "module example.com/svc\n\ngo 1.26\n", "svc/pkg/a_test.go": old}, "base")
    server.set(tests=[case(f"{package}::TestRoute/one", "a_test.go", "TestRoute/one")])
    merged(root, server, {"svc/pkg/a_test.go": old + "\n"}, "Spec: ANYM-12")
    monkeypatch.setattr(tiden_sync, "KEPT_SECONDS", -1)
    code, out = run(root, server)
    assert code == 0, out
    assert "time budget is spent" in out
    assert not server.state().get("links")


# spec: ANYM-421
def test_a_marker_added_in_a_comment_elsewhere_in_the_package_links_no_unmarked_go_test(root, server):
    full_server(server)
    package = "example.com/svc/pkg"
    commit(root, {"svc/go.mod": "module example.com/svc\n\ngo 1.26\n",
                  "svc/pkg/a_test.go": "package pkg\n\n/* example\nfunc TestRoute(t *testing.T) {}\n*/\n",
                  "svc/pkg/b_test.go": "package pkg\n\nfunc TestRoute(t *testing.T) {}\n"}, "base")
    server.set(tests=[case(f"{package}::TestRoute/one", "b_test.go", "TestRoute/one")])
    merged(root, server, {"svc/pkg/a_test.go": "package pkg\n\n/* example\n// spec: ANYM-12\nfunc TestRoute(t *testing.T) {}\n*/\n"},
           "Spec: ANYM-12")
    code, out = run(root, server)
    assert code == 0, out
    assert not server.state().get("links")


# spec: ANYM-421
def test_a_marked_comment_example_that_was_already_there_links_no_unmarked_go_test(root, server):
    full_server(server)
    package = "example.com/svc/pkg"
    example = "package pkg\n\n/* %s\n// spec: ANYM-12\nfunc TestRoute(t *testing.T) {}\n*/\n"
    commit(root, {"svc/go.mod": "module example.com/svc\n\ngo 1.26\n", "svc/pkg/a_test.go": example % "Documentation example",
                  "svc/pkg/b_test.go": "package pkg\n\nfunc TestRoute(t *testing.T) {}\n"}, "base")
    server.set(tests=[case(f"{package}::TestRoute/one", "b_test.go", "TestRoute/one")])
    merged(root, server, {"svc/pkg/a_test.go": example % "Updated example"}, "Spec: ANYM-12")
    code, out = run(root, server)
    assert code == 0, out
    assert not server.state().get("links")


# spec: ANYM-421
def test_a_marker_line_inside_a_block_comment_before_a_real_declaration_links_nothing(root, server):
    full_server(server)
    package = "example.com/svc/pkg"
    text = "package pkg\n\n/* %s\n// spec: ANYM-12\n*/ func TestRoute(t *testing.T) {}\n"
    commit(root, {"svc/go.mod": "module example.com/svc\n\ngo 1.26\n", "svc/pkg/a_test.go": text % "Documentation example"},
           "base")
    server.set(tests=[case(f"{package}::TestRoute/one", "a_test.go", "TestRoute/one")])
    merged(root, server, {"svc/pkg/a_test.go": text % "Updated example"}, "Spec: ANYM-12")
    code, out = run(root, server)
    assert code == 0, out
    assert not server.state().get("links")


# spec: ANYM-421
def test_a_kept_go_test_never_links_a_case_imported_from_another_file_of_the_package(root, server):
    full_server(server)
    package = "example.com/svc/pkg"
    old = "package pkg\n\n// spec: ANYM-12\nfunc TestRoute(t *testing.T) {}\n"
    commit(root, {"svc/go.mod": "module example.com/svc\n\ngo 1.26\n", "svc/pkg/a_test.go": old}, "base")
    server.set(tests=[case(f"{package}::TestRoute/unix_only", "b_test.go", "TestRoute/unix_only")])
    merged(root, server, {"svc/pkg/a_test.go": old + "\n"}, "Spec: ANYM-12")
    code, out = run(root, server)
    assert code == 0, out
    assert not server.state().get("links")


# spec: ANYM-421
def test_go_declarations_ignore_comments_and_literals_and_keep_line_numbers():
    source = ("package p\n/* func TestA(t *testing.T) {} */\nvar s = \"func TestB(\" // func TestC(\nvar r = `\n"
              "func TestD(x`\nvar q = '\"'\nfunc TestReal(t *testing.T) {}\nvar e = \"a\\\"b\"\nfunc TestMain(m *testing.M) {}\n")
    assert tiden_sync.go_declarations(source) == [("TestReal", 6)]
    assert tiden_sync.go_scan(source)[0].count("\n") == source.count("\n")


# spec: ANYM-421
def test_kept_go_test_rechecks_stop_inside_one_test_when_the_budget_runs_out(root, server, monkeypatch):
    full_server(server)
    package = "example.com/svc/pkg"
    old = "package pkg\n\n// spec: ANYM-12\nfunc TestRoute(t *testing.T) {}\n"
    commit(root, {"svc/go.mod": "module example.com/svc\n\ngo 1.26\n", "svc/pkg/a_test.go": old}, "base")
    server.set(tests=[case(f"{package}::TestRoute/one", "a_test.go", "TestRoute/one"),
                      case(f"{package}::TestRoute/two", "a_test.go", "TestRoute/two")])
    merged(root, server, {"svc/pkg/a_test.go": old + "\n"}, "Spec: ANYM-12")
    clock, write = [0.0], tiden_sync.write

    def slow_write(*arguments):
        clock[0] += 1000
        return write(*arguments)

    monkeypatch.setattr(tiden_sync.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tiden_sync, "write", slow_write)
    code, out = run(root, server)
    assert code == 0, out
    assert "time budget is spent" in out
    assert server.state()["links"] == {f"t-{package}::TestRoute/one": ["m-0012-0000-0000-0000-000000000000"]}


# spec: ANYM-421
def test_a_go_test_resolves_to_its_nearest_module_quoted_or_commented_and_never_to_an_outer_one(root, server):
    commit(root, {"outer/go.mod": "module example.com/outer\n",
                  "outer/inner/go.mod": "module example.com/inner // the nested module\n",
                  "outer/quoted/go.mod": 'module "example.com/quoted"\n',
                  "outer/broken/go.mod": "go 1.26\n",
                  "outer/tight/go.mod": "module example.com/tight//note\n",
                  "outer/escaped/go.mod": 'module "example.com/\\x73caped"\n'}, "modules")
    assert tiden_sync.go_package(root, "HEAD", "outer/tight/x_test.go") == "example.com/tight"
    assert tiden_sync.go_package(root, "HEAD", "outer/escaped/x_test.go") == "example.com/scaped"
    assert tiden_sync.go_package(root, "HEAD", "outer/inner/x_test.go") == "example.com/inner"
    assert tiden_sync.go_package(root, "HEAD", "outer/inner/pkg/x_test.go") == "example.com/inner/pkg"
    assert tiden_sync.go_package(root, "HEAD", "outer/quoted/x_test.go") == "example.com/quoted"
    assert tiden_sync.go_package(root, "HEAD", "outer/other/x_test.go") == "example.com/outer/other"
    # A go.mod without a readable module path is still the boundary: never the outer module's package.
    assert tiden_sync.go_package(root, "HEAD", "outer/broken/x_test.go") is None
    rows = [case("example.com/inner::TestGood", "x_test.go", "TestGood"),
            case("example.com/outer/inner::TestGood", "x_test.go", "TestGood")]
    package = tiden_sync.go_package(root, "HEAD", "outer/inner/x_test.go")
    assert [row["id"] for row in tiden_sync.matches(rows, "outer/inner/x_test.go", "TestGood", package)] == [
        "t-example.com/inner::TestGood"]


# spec: ANYM-405
def test_node_cases_match_by_file_and_title_and_class_methods_by_class():
    rows = [case("node:web/a.test.mjs::adds_one", "web/a.test.mjs", "adds one"),
            case("node:web/b.test.mjs::adds_one", "web/b.test.mjs", "adds one"),
            case("tests.test_a.TestGroup::test_x", "tests/test_a.py", "test_x"),
            case("tests.test_a::test_x", "tests/test_a.py", "test_x"),
            case("tests.test_a::test_x_more", "tests/test_a.py", "test_x_more")]
    assert [row["id"] for row in tiden_sync.matches(rows, "web/a.test.mjs", "adds one")] == [
        "t-node:web/a.test.mjs::adds_one"]
    assert [row["id"] for row in tiden_sync.matches(rows, "tests/test_a.py", "TestGroup::test_x")] == [
        "t-tests.test_a.TestGroup::test_x"]
    assert [row["id"] for row in tiden_sync.matches(rows, "tests/test_a.py", "test_x")] == ["t-tests.test_a::test_x"]


# spec: ANYM-405
def test_anchoring_resends_every_source_with_provenance_and_never_overwrites_a_receipt(root, server):
    sources = [anchor("app/core.py", lineStart=3, lineEnd=9, metadata={"commit_sha": "abc"}),
               anchor("app/old.py", metadata={"reverse_pr": {"status": "modified"}}),
               anchor("app/kept.py", metadata={"historical_source_id": "original", "historical_metadata_json": "{}"}),
               {"id": "src-manual", "sourceType": "manual_input", "title": "Intent", "excerpt": "quote",
                "repository": None, "metadata": None}]
    server.set(main=[spec(12, sources=sources)])
    merged(root, server, {"app/core.py": "x = 2\n", "app/new.py": "y\n"}, "Spec: ANYM-12")
    code, out = run(root, server)
    assert code == 0, out
    sent = server.state()["sent_sources"][0]
    assert sent[0] == {"sourceType": "repo_file", "repoPath": "app/core.py", "repository": "github.com/GoSync-Inc/deedkit",
                       "lineStart": 3, "lineEnd": 9,
                       "metadata": {"commit_sha": "abc", "historical_source_id": "src-app/core.py"}}
    assert sent[1]["metadata"] == {"reverse_pr": {"status": "modified"}, "historical_source_id": "src-app/old.py",
                                   "historical_metadata_json": '{"reverse_pr":{"status":"modified"}}'}
    assert sent[2]["metadata"] == {"historical_source_id": "original", "historical_metadata_json": "{}"}
    assert sent[3] == {"sourceType": "manual_input", "title": "Intent", "excerpt": "quote",
                       "metadata": {"historical_source_id": "src-manual"}}
    assert sent[4]["repoPath"] == "app/new.py" and len(sent) == 5
    assert "anchors: 1 file(s) anchored to ANYM-12: app/new.py" in out


# spec: ANYM-504
def test_a_changed_licence_text_is_not_anchored(root, server):
    server.set(main=[spec(12, sources=[anchor("app/core.py")])])
    merged(root, server, {"services/calls/LICENSE": "licence text\n", "app/new.py": "y\n"}, "Spec: ANYM-12")
    code, out = run(root, server)
    assert code == 0, out
    assert [source["repoPath"] for source in server.state()["sent_sources"][0]] == ["app/core.py", "app/new.py"]
    assert "anchors: 1 file(s) anchored to ANYM-12: app/new.py" in out and "LICENSE" not in out
    merged(root, server, {"services/calls/LICENSE": "licence text 2\n"}, "", number=8)
    code, out = run(root, server)
    assert code == 0, out
    assert len(server.state()["sent_sources"]) == 1 and "0 code file(s)" in out


# spec: ANYM-405
def test_a_lost_source_or_no_named_spec_fails_the_anchors_step(root, server):
    server.set(main=[spec(12, sources=[anchor("app/core.py")]), spec(20, kind="Epic")], fault={"spec update": "drop"})
    merged(root, server, {"app/new.py": "y\n"}, "Spec: ANYM-12")
    code, out = run(root, server)
    assert code == 1 and "Failed at step anchors: read-back of ANYM-12: 1 sent source(s) missing or changed" in out
    server.set(fault={})
    merged(root, server, {"app/other.py": "z\n"}, "", number=8)
    code, out = run(root, server)
    assert code == 1 and "the body names no spec for app/other.py" in out
    merged(root, server, {"app/third.py": "z\n"}, "Spec: ANYM-20", number=9)
    code, out = run(root, server)
    assert code == 1 and "ANYM-20 is not a story-level spec" in out


# spec: ANYM-405
def test_an_open_live_or_measured_check_keeps_the_card_in_review_and_a_status_that_does_not_hold_fails(root, server):
    server.set(main=[spec(12, status="Active", content="- Live check [open: live] on the host."),
                     spec(13, status="Active", content="- Measured [open: measured]."),
                     spec(14, status="Review", content="Delivered; the check closed with evidence.")],
               fault={"spec update": "noop"})
    merged(root, server, {"notes/x.md": "doc\n"}, "Delivers: ANYM-12, ANYM-13, ANYM-14")
    code, out = run(root, server)
    assert code == 1
    assert "read-back: ANYM-12 is Active, not Review" in out and "read-back: ANYM-14 is Review, not Done" in out
    server.set(fault={})
    code, out = run(root, server)
    assert code == 0, out
    assert [server.main(n)["status"] for n in (12, 13, 14)] == ["Review", "Review", "Done"]
    assert "(an open live or measured check remains)" in out


# spec: ANYM-405
def test_links_anchors_and_status_each_run_when_another_fails(root, server):
    server.set(main=[spec(12, status="Active")], tests=[])
    merged(root, server, {"tests/test_new.py": "# spec: ANYM-12\ndef test_new():\n    pass\n", "app/new.py": "y\n"},
           "Delivers: ANYM-12, ANYM-99")
    code, out = run(root, server)
    assert code == 1
    assert "Failed at step resolve: ANYM-99 not on Tiden main" in out
    assert "Failed at step links" in out
    assert "anchors: 1 file(s) anchored to ANYM-12" in out
    assert "Failed at step status: ANYM-99 is not on Tiden main" in out and server.main(12)["status"] == "Done"


# spec: ANYM-405
def test_the_command_line_hides_cli_output_and_writes_the_summary(root, server, tmp_path):
    full_server(server)
    merged(root, server, {"app/new.py": "y\n"}, "Spec: ANYM-12")
    server.set(fault={"spec list": "exit"})
    summary = tmp_path / "summary.md"
    env = {"GITHUB_STEP_SUMMARY": str(summary), "TIDEN_PRODUCT_ID": "product", "TIDEN_API_TOKEN": SECRET,
           "FAKE_TIDEN_STATE": str(server.path), "PATH": "/usr/bin:/bin"}
    completed = subprocess.run([sys.executable, SCRIPT, "--root", str(root), "--cli", str(server.cli),
                                "--gh", str(server.gh)], capture_output=True, text=True, env=env)
    assert completed.returncode == 1
    assert "Failed at step listing: tiden spec list exited 3" in summary.read_text()
    assert SECRET not in completed.stdout + completed.stderr + summary.read_text()
    server.set(fault={})
    completed = subprocess.run([sys.executable, SCRIPT, "--root", str(root), "--cli", str(server.cli),
                                "--gh", str(server.gh)], capture_output=True, text=True, env=env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(server.path.read_text())["main"][0]["sources"][-1]["repoPath"] == "app/new.py"


# spec: ANYM-405
@pytest.mark.parametrize("preview, reason", [
    ({"additions": [{"seqNum": 50}], "specMoves": [{"seqNum": 12}]}, "unknown fields (specMoves)"),
    ({"additions": [{"seqNum": 50}], "componentAdditions": [{"name": "c"}]}, "componentAdditions"),
    ({"additions": [{"seqNum": 50}], "componentModifications": [{"name": "c"}]}, "componentModifications"),
    ({"additions": [{"seqNum": 50}], "testModifications": [{"id": "t"}]}, "testModifications"),
    ({"additions": [{"seqNum": 50}], "testDeletions": [{"id": "t"}]}, "testDeletions"),
    ({"additions": [{"seqNum": 50}], "stats": {"additions": 1, "conflicts": 2}}, "stats.conflicts"),
    ({"additions": [{"seqNum": 50}], "stats": {"additions": 2}}, "stats.additions"),
    ({"additions": [{"seqNum": 50}], "stats": {"conflicts": "0"}}, "malformed stats"),
    ({"additions": [{"seqNum": 50}], "intentSession": {"id": "s"}}, "intentSession"),
    ({"additions": [{"seqNum": 50}], "hasRequirementChanges": "yes"}, "hasRequirementChanges"),
    ({"modifications": [{"seqNum": 50, "branch": {"seqNum": 99}}]}, "without exactly one spec number"),
    ({"modifications": [{"seqNum": "50"}]}, "without exactly one spec number"),
])
def test_a_preview_outside_the_known_spec_only_shape_is_never_merged(root, server, preview, reason):
    full_server(server)
    server.set(previews={"proposal/x": preview})
    merged(root, server, {"app/new.py": "y\n"}, "Delivers: ANYM-50\nTiden branch: proposal/x")
    code, out = run(root, server)
    assert code == 1 and "Failed at step merge" in out and reason in out
    assert not server.writes()


# spec: ANYM-405
def test_the_cli_041_preview_keys_merge_when_empty_and_stop_the_sync_when_set(root, server):
    full_server(server)
    preview = {"additions": [{"seqNum": 50}], "modifications": [], "stats": {"conflicts": 0}, "testDuplicates": [],
               "autoMerge": None}
    server.set(previews={"proposal/x": preview})
    merged(root, server, {"app/new.py": "y\n"}, "Delivers: ANYM-50\nTiden branch: proposal/x")
    code, out = run(root, server)
    assert code == 0, out
    assert {"name": "proposal/x", "status": "merged"} in server.state()["branches"]
    for extra, reason in (({"testDuplicates": [{"id": "t"}]}, "testDuplicates"),
                          ({"autoMerge": {"state": "in_progress", "reason": "round_active"}}, "autoMerge")):
        full_server(server)
        server.set(previews={"proposal/x": preview | extra}, log=[])
        code, out = run(root, server)
        assert code == 1 and "Failed at step merge" in out and reason in out and "unknown fields" not in out
        assert not server.writes()


# spec: ANYM-405
def test_a_merge_that_leaves_the_branch_open_fails_and_stops_before_other_writes(root, server):
    full_server(server)
    server.set(fault={"branch merge": "noop"})
    merged(root, server, {"app/new.py": "y\n"}, "Delivers: ANYM-50\nTiden branch: proposal/x")
    code, out = run(root, server)
    assert code == 1 and "Failed at step merge: read-back: proposal/x is open, not merged" in out
    assert [entry[:2] for entry in server.writes()] == [["branch", "merge"]]


# spec: ANYM-405
def test_a_commented_out_copy_of_an_existing_test_links_nothing(root, server):
    full_server(server)
    server.set(tests=[case("node:web/e.test.mjs::existing", "web/e.test.mjs", "existing")])
    commit(root, {"web/e.test.mjs": "await test('existing', () => {});\n"}, "existing test")
    merged(root, server, {"web/e.test.mjs": "await test('existing', () => {});\n/*\n// spec: ANYM-12\n"
                                            "test('existing', () => {});\n*/\n"}, "Spec: ANYM-12")
    code, out = run(root, server)
    assert code == 0, out
    assert "0 added test(s)" in out and not server.writes()


# spec: ANYM-405
def test_nested_python_classes_match_their_dotted_junit_class():
    rows = [case("tests.test_a.TestOuter.TestInner::test_x", "tests/test_a.py", "test_x"),
            case("tests.test_a.TestOuter::test_x", "tests/test_a.py", "test_x")]
    assert [row["id"] for row in tiden_sync.matches(rows, "tests/test_a.py", "TestOuter::TestInner::test_x")] == [
        "t-tests.test_a.TestOuter.TestInner::test_x"]


# spec: ANYM-405
def test_a_malformed_link_read_or_an_unexpected_error_names_its_step_and_the_others_still_run(root, server,
                                                                                          monkeypatch):
    full_server(server)
    server.set(fault={"test links": "malformed"})
    merged(root, server, {"tests/test_new.py": "# spec: ANYM-12\ndef test_new():\n    pass\n", "app/new.py": "y\n"},
           "Delivers: ANYM-12")
    code, out = run(root, server)
    assert code == 1 and "Failed at step links: tests.test_new::test_new -> ANYM-12: the test links are malformed" in out
    assert "anchors: 1 file(s) anchored to ANYM-12" in out and "status: ANYM-12 Active -> Done" in out
    server.set(fault={})

    def broken(*_):
        raise TypeError("a value from the data")

    monkeypatch.setattr(tiden_sync, "matches", broken)
    merged(root, server, {"tests/test_newer.py": "# spec: ANYM-12\ndef test_new():\n    pass\n", "app/other.py": "z\n"},
           "Spec: ANYM-12", number=8)
    code, out = run(root, server)
    assert code == 1 and "Failed at step links: unexpected TypeError" in out and "a value from the data" not in out
    assert "anchors: 1 file(s) anchored to ANYM-12: app/other.py" in out


# spec: ANYM-405
def test_a_malformed_source_fails_the_anchors_step_by_name(root, server):
    server.set(main=[spec(12, status="Active", sources=[anchor("app/core.py", repository=7)])])
    merged(root, server, {"app/new.py": "y\n"}, "Delivers: ANYM-12")
    code, out = run(root, server)
    assert code == 1 and "Failed at step anchors: ANYM-12 has a malformed source repository" in out
    assert "status: ANYM-12 Active -> Done" in out


# spec: ANYM-405
def test_a_rerun_after_a_partial_failure_writes_only_what_is_left(root, server):
    full_server(server)
    server.set(fault={"spec update": "exit"})
    merged(root, server, {"app/new.py": "y\n", "tests/test_new.py": TEST_FILE},
           "Delivers: ANYM-50\nSpec: ANYM-12\nTiden branch: proposal/x\n")
    code, out = run(root, server)
    assert code == 1 and "Failed at step anchors" in out and "Failed at step status" in out
    first = server.writes()
    assert sorted({tuple(entry[:2]) for entry in first}) == [("branch", "merge"), ("spec", "update"),
                                                             ("test", "link-spec")]
    links = server.state()["links"]
    server.set(fault={})
    code, out = run(root, server)
    assert code == 0, out
    rest = server.writes()[len(first):]
    assert [entry[:2] for entry in rest] == [["spec", "update"], ["spec", "update"]]
    assert "--sources-file" in rest[0] and "--status" in rest[1]
    assert server.state()["links"] == links and server.main(50)["status"] == "Done"
    assert [source["repoPath"] for source in server.main(50)["sources"]] == ["app/new.py"]


# spec: ANYM-405
def test_two_anchor_runs_from_one_stale_listing_both_keep_their_anchors(root, server):
    server.set(main=[spec(12, sources=[anchor("app/core.py", metadata={"reverse_pr": {"pr": 3}})])])
    stale = tiden_sync.listing(str(server.cli), "product", "main")
    for path in ("app/a.py", "app/b.py"):
        tiden_sync.anchor(str(server.cli), "product", [path], 12, stale, "0" * 40, 7, lambda line: None)
    sources = server.main(12)["sources"]
    assert [source["repoPath"] for source in sources] == ["app/core.py", "app/a.py", "app/b.py"]
    assert sources[0]["metadata"]["reverse_pr"] == {"pr": 3}


# spec: ANYM-405
def test_an_existing_receipt_keeps_new_nested_metadata_and_a_changed_source_fails_the_read_back(root, server):
    kept = anchor("app/kept.py", metadata={"historical_source_id": "first", "historical_metadata_json": "{}",
                                           "reverse_pr": {"new": "unrecorded"}})
    server.set(main=[spec(12, sources=[kept])])
    merged(root, server, {"app/new.py": "y\n"}, "Spec: ANYM-12")
    code, out = run(root, server)
    assert code == 0, out
    assert server.main(12)["sources"][0]["metadata"] == kept["metadata"]
    server.set(fault={"spec update": "corrupt"})
    merged(root, server, {"app/other.py": "z\n"}, "Spec: ANYM-12", number=8)
    code, out = run(root, server)
    assert code == 1
    assert "read-back of ANYM-12: 1 sent source(s) missing or changed, 1 unexpected" in out


# spec: ANYM-405
@pytest.mark.parametrize("change", [
    {"seqNum": 50, "branch": {"seqNum": 50.0}},
    {"seqNum": 50, "branch": {"seqNum": True}},
    {"seqNum": []},
    {"seqNum": 50, "branch": {"seqNum": {"n": 50}}},
])
def test_a_spec_number_of_another_type_is_never_merged_and_fails_the_merge_step(root, server, change):
    full_server(server)
    server.set(previews={"proposal/x": {"modifications": [change]}})
    merged(root, server, {"app/new.py": "y\n"}, "Delivers: ANYM-50\nTiden branch: proposal/x")
    code, out = run(root, server)
    assert code == 1 and "Failed at step merge: the merge preview has a change without exactly one spec number" in out
    assert not server.writes()


# spec: ANYM-405
def test_an_unexpected_error_in_the_merge_step_is_named_and_stops_the_sync(root, server, monkeypatch):
    full_server(server)

    def broken(*_):
        raise TypeError("a value from the data")

    monkeypatch.setattr(tiden_sync, "preview_numbers", broken)
    merged(root, server, {"app/new.py": "y\n"}, "Delivers: ANYM-50\nTiden branch: proposal/x")
    code, out = run(root, server)
    assert code == 1 and "Failed at step merge: unexpected TypeError" in out and "a value from the data" not in out
    assert not server.writes()


# spec: ANYM-405
@pytest.mark.parametrize("change", [
    {"seqNum": 50, "branch": {"seqNum": None}},
    {"seqNum": None, "branch": {"seqNum": 50}},
])
def test_a_null_spec_number_in_any_copy_is_never_merged(root, server, change):
    full_server(server)
    server.set(previews={"proposal/x": {"modifications": [change]}})
    merged(root, server, {"app/new.py": "y\n"}, "Delivers: ANYM-50\nTiden branch: proposal/x")
    code, out = run(root, server)
    assert code == 1 and "Failed at step merge: the merge preview has a change without exactly one spec number" in out
    assert not server.writes()


# spec: ANYM-405
@pytest.mark.parametrize("state", [
    {"previews": {"proposal/x": {"additions": [{"seqNum": 50}], "modifications": [], "stats": {"conflicts": 1}}}},
    {"fault": {"branch merge": "exit"}},
    {"fault": {"spec update": "exit"}},
    {"fault": {"spec update": "drop"}},
])
def test_a_failed_sync_recovers_by_rerun_or_dispatch_and_a_change_by_hand_needs_a_maintenance_window(root, server, state):
    full_server(server)
    server.set(**state)
    merged(root, server, {"app/new.py": "y\n"}, "Delivers: ANYM-50\nTiden branch: proposal/x")
    code, out = run(root, server)
    lines = out.splitlines()
    assert code == 1 and any(line.startswith("Failed at step") for line in lines), out
    # A merge failure stops the sync and names the recovery itself; any later failure ends with one recovery line.
    assert "rerun this job or dispatch this SHA from main" in lines[-1], out
    assert all("maintenance window" in line for line in lines if "hand" in line), out


def sync_workflow():
    import yaml
    return yaml.safe_load((Path(SCRIPT).parents[1] / ".github/workflows/tiden-sync.yml").read_text())


# spec: ANYM-405
def test_the_sync_workflow_runs_after_main_ci_serialized_with_main_tools_and_a_dry_run():
    value = sync_workflow()
    triggers = value[True]  # yaml reads the `on` key as a boolean
    assert set(triggers) == {"workflow_run", "workflow_dispatch"}
    assert triggers["workflow_run"] == {"workflows": ["CI"], "types": ["completed"]}
    # A dispatch names a commit or a ready branch; the guard admits exactly one (the ready merge tests).
    assert set(triggers["workflow_dispatch"]["inputs"]) == {"sha", "branch"}
    assert value["permissions"] == {"contents": "read"}
    assert value["concurrency"] == {"group": "tiden-sync", "cancel-in-progress": False}
    (job,) = value["jobs"].values()
    assert "concurrency" not in job and "permissions" not in job and "conclusion" not in job["if"]
    steps = job["steps"]
    checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"] == {"persist-credentials": False, "ref": "main", "fetch-depth": 0}
    assert steps.index(checkout) == 1 and steps[2]["run"] == 'git merge-base --is-ancestor "$SHA" HEAD'
    holders = [step["name"] for step in steps if any("secrets." in str(v) or "github.token" in str(v)
                                                     for v in step.get("env", {}).values())]
    assert holders == [steps[-1]["name"]]
    assert all("secrets." not in step.get("run", "") and "GITHUB_PATH" not in step.get("run", "") for step in steps)

    def pinned(text):
        return next(line.strip() for line in text.splitlines() if "sha256sum --check" in line)
    install = next(step for step in steps if step.get("name") == "Install pinned Tiden CLI with integrity check")
    assert pinned(install["run"]) == pinned((Path(SCRIPT).parents[1] / ".github/workflows/ci.yml").read_text())


# spec: ANYM-405
def test_the_sync_workflow_writes_to_tiden_once_check_a_has_run_green_on_three_pull_requests():
    steps = sync_workflow()["jobs"]["sync"]["steps"]
    command = steps[-1]["run"].split()
    assert command[:2] == ["python", "scripts/tiden_sync.py"] and "--dry-run" not in command


def render_run_name(expression, **contexts):
    """The `a || b` run-name expression as GitHub evaluates it: the first non-empty operand, empty when none."""
    inner = expression.split("${{", 1)[1].rsplit("}}", 1)[0]
    for operand in (part.strip() for part in inner.split("||")):
        value = contexts.get(operand)
        if value:
            return expression.split("${{", 1)[0] + value
    return expression.split("${{", 1)[0]


# spec: ANYM-438
def test_the_sync_run_title_names_the_commit_it_processes_for_each_trigger():
    value = sync_workflow()
    assert value["run-name"] == ("Tiden sync ${{ github.event.workflow_run.head_sha || inputs.sha || "
                                 "format('ready merge {0}', inputs.branch) }}")
    # A workflow_run carries the CI run's commit; a dispatch carries its input, and the default-branch head_sha of
    # either kind of run is not what names the title.
    assert render_run_name(value["run-name"], **{"github.event.workflow_run.head_sha": "a" * 40}) == "Tiden sync " + "a" * 40
    assert render_run_name(value["run-name"], **{"inputs.sha": "b" * 40}) == "Tiden sync " + "b" * 40
    assert render_run_name(value["run-name"], **{"github.event.workflow_run.head_sha": "a" * 40,
                                                 "inputs.sha": ""}) == "Tiden sync " + "a" * 40


# spec: ANYM-438
def test_the_run_title_is_the_only_change_to_the_sync_workflow_shape():
    value = sync_workflow()
    assert set(value) == {"name", "run-name", True, "permissions", "concurrency", "jobs"}
    assert value["name"] == "Tiden sync" and value["permissions"] == {"contents": "read"}
    assert value["concurrency"] == {"group": "tiden-sync", "cancel-in-progress": False}
    assert set(value[True]) == {"workflow_run", "workflow_dispatch"} and set(value["jobs"]) == {"sync"}
    assert value["jobs"]["sync"]["name"] == "Tiden sync"


# spec: ANYM-405
@pytest.mark.parametrize(("env", "passes"), [
    ({"EVENT": "workflow_run", "UPSTREAM_EVENT": "push", "UPSTREAM_BRANCH": "main", "UPSTREAM_REPOSITORY": "o/r"}, True),
    ({"EVENT": "workflow_dispatch", "INPUT_SHA": "a" * 40}, True),
    ({"EVENT": "workflow_run", "UPSTREAM_EVENT": "pull_request", "UPSTREAM_BRANCH": "main",
      "UPSTREAM_REPOSITORY": "o/r"}, False),
    ({"EVENT": "workflow_run", "UPSTREAM_EVENT": "push", "UPSTREAM_BRANCH": "MAIN", "UPSTREAM_REPOSITORY": "o/r"}, False),
    ({"EVENT": "workflow_run", "UPSTREAM_EVENT": "push", "UPSTREAM_BRANCH": "main", "UPSTREAM_REPOSITORY": "fork/r"},
     False),
    ({"EVENT": "workflow_dispatch", "INPUT_SHA": "main"}, False),
    ({"EVENT": "workflow_dispatch", "INPUT_SHA": "A" * 40}, False),
    ({"EVENT": "workflow_dispatch", "INPUT_SHA": "a" * 40, "REF": "refs/heads/feature"}, False),
])
def test_the_sync_guard_admits_only_an_exact_main_commit(env, passes, tmp_path):
    guard = sync_workflow()["jobs"]["sync"]["steps"][0]
    assert guard["id"] == "guard"
    output = tmp_path / "output"
    base = {"REF": "refs/heads/main", "REPOSITORY": "o/r", "UPSTREAM_SHA": "b" * 40, "GITHUB_OUTPUT": str(output),
            "PATH": "/usr/bin:/bin"}
    completed = subprocess.run(["bash", "-e", "-c", guard["run"]], env={**base, **env})
    assert (completed.returncode == 0) == passes
    if passes:
        expected = "b" * 40 if env["EVENT"] == "workflow_run" else env["INPUT_SHA"]
        assert output.read_text() == f"sha={expected}\n"


READY_LINE = "Ready: 2026-10-26, Fable r1 clean; asked by the owner, 2026-10-26"
REFRESHED = "Ready: 2026-10-27, Astra r1 clean on the changed criterion; asked by the owner, 2026-10-26"


def ready_server(server, preview=None, branch=None, main=None):
    """Main holds ANYM-60, a ready Backlog card; proposal/ready adds ANYM-70 and changes the text of ANYM-60."""
    server.set(main=main or [spec(12), spec(60, status="Backlog", content=f"Criteria.\n{READY_LINE}\n")],
               branch_specs={"proposal/ready": branch or [
                   spec(70, status="Backlog", content=f"New card.\n- **{READY_LINE}**\n", branch=True),
                   spec(60, status="Backlog", content=f"Changed criteria.\n{REFRESHED}\n", branch=True)]},
               branches=[{"name": "main", "status": "open"}, {"name": "proposal/ready", "status": "open"}],
               previews={"proposal/ready": preview or {
                   "additions": [{"seqNum": 70, "parentId": None}],
                   "modifications": [{"mainVersion": {"seqNum": 60, "parentId": None},
                                      "branchVersion": {"seqNum": 60, "parentId": None}}],
                   "deletions": [], "stats": {"additions": 1, "modifications": 1, "conflicts": 0},
                   "intentSession": None, "blockingSessions": 0, "hasRequirementChanges": True}})


def ready_run(server, name="proposal/ready", dry=False):
    lines = []
    try:
        code = tiden_sync.ready_merge(str(server.cli), "product", name, lines.append, dry)
    except tiden_sync.StepFailed as error:
        lines.append(f"Failed at step {error.step}: {error}")
        code = 1
    return code, "\n".join(lines)


# spec: ANYM-405
def test_a_ready_merge_merges_a_branch_of_ready_cards_to_main_as_backlog_and_reads_each_back(server):
    ready_server(server)
    code, out = ready_run(server)
    assert code == 0, out
    assert [entry[:2] for entry in server.writes()] == [["branch", "merge"]]
    assert {"name": "proposal/ready", "status": "merged"} in server.state()["branches"]
    assert server.main(70)["status"] == "Backlog" and REFRESHED in server.main(60)["content"]
    assert "ready merge: ANYM-60 " in out and "(text changed)" in out and "ready merge: ANYM-70 " in out
    assert out.splitlines()[-1] == "Ready merge of proposal/ready done (ANYM-60, ANYM-70)."
    # A text change keeps main's record, a new spec its branch record.
    assert server.main(60)["id"].startswith("m-0060") and server.main(70)["id"].startswith("b-0070")
    # A merged branch can no longer be listed: a second dispatch merges nothing and cannot pass as a read-back.
    server.set(log=[])
    code, out = ready_run(server)
    assert code == 1 and "is merged already and cannot be read back here" in out and not server.writes()


# spec: ANYM-405
def test_a_ready_merge_dry_run_reads_and_writes_nothing(server):
    ready_server(server)
    code, out = ready_run(server, dry=True)
    assert code == 0 and "would merge proposal/ready (ANYM-60, ANYM-70)" in out and not server.writes()


NOT_READY = f"New card.\n```text\n{READY_LINE}\n```\nAnd `{READY_LINE}` inline.\n"


# spec: ANYM-405
@pytest.mark.parametrize("branch, main, reason", [
    ([spec(70, status="Backlog", content="New card, no readiness record.", branch=True)], None,
     "ANYM-70 has no `Ready:` line"),
    ([spec(70, status="Backlog", content="Accepted: 2026-10-23, owner", branch=True)], None,
     "a legacy `Accepted:` card gets `Ready:` citing it first"),
    ([spec(70, status="Backlog", content=NOT_READY, branch=True)], None, "ANYM-70 has no `Ready:` line"),
    ([spec(70, status="Active", content=READY_LINE, branch=True)], None, "ANYM-70 is Active on proposal/ready"),
    ([spec(60, status="Backlog", content=f"Changed.\n{READY_LINE}", branch=True)], None,
     "ANYM-60 keeps main's `Ready:` line"),
    ([spec(60, status="Backlog", content=REFRESHED, branch=True)],
     [spec(60, status="Active", content=READY_LINE)], "ANYM-60 is Active on main"),
])
def test_a_ready_merge_refuses_a_spec_that_is_not_a_ready_backlog_card(server, branch, main, reason):
    number = branch[0]["seqNum"]
    key = "modifications" if number == 60 else "additions"
    ready_server(server, preview={key: [{"seqNum": number}], "stats": {key: 1, "conflicts": 0}}, branch=branch,
                 main=main)
    code, out = ready_run(server)
    assert code == 1 and "Failed at step ready" in out and reason in out, out
    assert not server.writes()


# spec: ANYM-405
@pytest.mark.parametrize("extra, reason", [
    ({"deletions": [{"seqNum": 12}]}, "deletions"),
    ({"stats": {"additions": 1, "conflicts": 1}}, "stats.conflicts"),
    ({"testAdditions": [{"id": "t"}]}, "testAdditions"),
    ({"componentAdditions": [{"name": "c"}]}, "componentAdditions"),
    ({"testDuplicates": [{"id": "t"}]}, "testDuplicates"),
    ({"autoMerge": {"state": "in_progress"}}, "autoMerge"),
    ({"intentSession": {"id": "s"}}, "intentSession"),
    ({"blockingSessions": 1}, "blockingSessions"),
    ({"specMoves": [{"seqNum": 12}]}, "unknown fields (specMoves)"),
    ({"additions": []}, "changes no spec"),
])
def test_a_ready_merge_refuses_a_preview_beyond_spec_additions_and_text_changes(server, extra, reason):
    ready_server(server, preview={"additions": [{"seqNum": 70}], "stats": {"additions": 1, "conflicts": 0}} | extra,
                 branch=[spec(70, status="Backlog", content=READY_LINE, branch=True)])
    if extra == {"additions": []}:
        server.set(previews={"proposal/ready": {"additions": [], "stats": {"conflicts": 0}}})
    code, out = ready_run(server)
    assert code == 1 and "Failed at step preview" in out and reason in out, out
    assert not server.writes()


# spec: ANYM-405
@pytest.mark.parametrize("name, branches, reason", [
    ("main", None, "main is not a proposal/ or review/ branch name"),
    ("intent/2026-10-26-x", None, "is not a proposal/ or review/ branch name"),
    ("proposal/missing", None, "proposal/missing is missing, not open"),
    ("proposal/ready", [{"name": "proposal/ready", "status": "closed"}], "proposal/ready is closed, not open"),
])
def test_a_ready_merge_takes_only_an_open_proposal_or_review_branch(server, name, branches, reason):
    ready_server(server)
    if branches:
        server.set(branches=branches)
    code, out = ready_run(server, name)
    assert code == 1 and "Failed at step branch" in out and reason in out, out
    assert not server.writes()


# spec: ANYM-405
def test_a_ready_merge_that_leaves_the_branch_open_or_loses_a_ready_line_fails_its_read_back(server, monkeypatch):
    ready_server(server)
    server.set(fault={"branch merge": "noop"})
    code, out = ready_run(server)
    assert code == 1 and "Failed at step read-back: proposal/ready is open, not merged" in out
    ready_server(server)
    server.set(fault={}, log=[])
    original, reads = tiden_sync.listing, []

    def listing(cli, product, branch):
        specs = original(cli, product, branch)
        reads.append(branch)
        if len(reads) == 5:  # main read back after the merge (the branch and main are read twice before it)
            specs[70] = dict(specs[70], content="Text without its readiness line.", status="Active")
        return specs
    monkeypatch.setattr(tiden_sync, "listing", listing)
    code, out = ready_run(server)
    assert reads == ["proposal/ready", "main", "proposal/ready", "main", "main"]
    assert code == 1 and "Failed at step read-back: ANYM-70 is Active, not Backlog; ANYM-70 does not hold the " \
                         "branch's `Ready:` line" in out, out


# spec: ANYM-405
def test_the_ready_merge_command_line_takes_the_workflow_arguments_and_a_branch_alone(server, tmp_path):
    ready_server(server)
    env = {"TIDEN_PRODUCT_ID": "product", "TIDEN_API_TOKEN": SECRET, "FAKE_TIDEN_STATE": str(server.path),
           "PATH": "/usr/bin:/bin"}
    both = subprocess.run([sys.executable, SCRIPT, "--sha", "a" * 40, "--ready-branch", "proposal/ready",
                           "--cli", str(server.cli)], capture_output=True, text=True, env=env)
    assert both.returncode == 1 and "a ready merge takes a branch alone" in both.stdout and not server.writes()
    # The workflow passes both options, the one it does not use empty.
    completed = subprocess.run([sys.executable, SCRIPT, "--sha", "", "--ready-branch", "proposal/ready",
                                "--cli", str(server.cli)], capture_output=True, text=True, env=env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Ready merge of proposal/ready done" in completed.stdout and SECRET not in completed.stdout


# spec: ANYM-405
@pytest.mark.parametrize(("env", "passes", "output"), [
    ({"INPUT_BRANCH": "proposal/2026-10-26-ready"}, True, "branch=proposal/2026-10-26-ready\n"),
    ({"INPUT_BRANCH": "review/anym-60-criteria"}, True, "branch=review/anym-60-criteria\n"),
    ({"INPUT_BRANCH": "proposal/x", "INPUT_SHA": "a" * 40}, False, ""),
    ({"INPUT_BRANCH": "intent/x"}, False, ""),
    ({"INPUT_BRANCH": "proposal/X; echo"}, False, ""),
    ({"INPUT_BRANCH": "proposal/x\nsha=" + "a" * 40}, False, ""),
    ({}, False, ""),
])
def test_the_sync_guard_admits_a_dispatch_of_exactly_one_commit_or_ready_branch(env, passes, output, tmp_path):
    guard = sync_workflow()["jobs"]["sync"]["steps"][0]
    path = tmp_path / "output"
    path.write_text("")
    base = {"REF": "refs/heads/main", "EVENT": "workflow_dispatch", "REPOSITORY": "o/r", "GITHUB_OUTPUT": str(path),
            "PATH": "/usr/bin:/bin"}
    completed = subprocess.run(["bash", "-e", "-c", guard["run"]], env={**base, **env})
    assert (completed.returncode == 0) == passes
    assert path.read_text() == output


# spec: ANYM-405
def test_a_ready_merge_dispatch_runs_in_the_sync_group_titled_by_its_branch_and_passes_it_to_the_script():
    value = sync_workflow()
    inputs = value[True]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"sha", "branch"} and not inputs["sha"]["required"] and not inputs["branch"]["required"]
    assert value["concurrency"] == {"group": "tiden-sync", "cancel-in-progress": False}
    assert value["run-name"].endswith("|| format('ready merge {0}', inputs.branch) }}")
    steps = value["jobs"]["sync"]["steps"]
    assert steps[2]["if"] == "steps.guard.outputs.sha != ''"
    assert steps[-1]["env"]["BRANCH"] == "${{ steps.guard.outputs.branch }}"
    assert '--sha "$SHA" --ready-branch "$BRANCH"' in steps[-1]["run"]


# spec: ANYM-405
@pytest.mark.parametrize("content", [
    f"New card.\n\n    {READY_LINE}\n",
    f"New card.\n\t{READY_LINE}\n",
    "New card.\n**Ready:**\n",
    "New card.\n- **Ready:** **\n",
    "New card.\nReady: `2026-10-26`\n",
    f"New card.\n\n>     {READY_LINE}\n",
    f"New card.\n\n-     {READY_LINE}\n",
    f"New card.\n\n- ~~~\n  + {READY_LINE}\n  ~~~\n",
    f"New card.\n\n- ~~~\n  - {READY_LINE}\n  ~~~\n",
    f"New card.\n\n> ~~~\n> {READY_LINE}\n> ~~~\n",
    f"New card.\n\n<pre>\n{READY_LINE}\n</pre>\n",
    f"New card.\n\n<!--\n{READY_LINE}\n-->\n",
], ids=["indented", "tab", "bold-empty", "list-bold-empty", "code-span", "quote-code", "list-code", "list-fence-plus",
        "list-fence-dash", "quote-fence", "html-pre", "html-comment"])
def test_a_ready_merge_refuses_a_readiness_record_in_indented_code_or_without_text(server, content):
    ready_server(server, preview={"additions": [{"seqNum": 70, "parentId": None}],
                                  "stats": {"additions": 1, "conflicts": 0}},
                 branch=[spec(70, status="Backlog", content=content, branch=True)])
    code, out = ready_run(server)
    assert code == 1 and "Failed at step ready: ANYM-70 has no `Ready:` line" in out, out
    assert not server.writes()


# spec: ANYM-405
def test_a_ready_merge_takes_main_s_readiness_record_in_other_markup_for_the_same_record(server):
    ready_server(server, preview={"modifications": [{"seqNum": 60}], "stats": {"modifications": 1, "conflicts": 0}},
                 branch=[spec(60, status="Backlog", content=f"Changed.\n- **{READY_LINE}**", branch=True)])
    code, out = ready_run(server)
    assert code == 1 and "ANYM-60 keeps main's `Ready:` line" in out, out
    assert not server.writes()


# spec: ANYM-405
@pytest.mark.parametrize("main_content, reason", [
    (f"Criteria.\n+ {READY_LINE}\n", "keeps main's `Ready:` line"),
    (f"Criteria.\n {READY_LINE}\n", "keeps main's `Ready:` line"),
    (f"Criteria.\n\n<!-- note -->\n\n{READY_LINE}\n", "keeps main's `Ready:` line"),
    ("Criteria.\nReady: 2026-10-26, `note` Fable r1 clean; asked by the owner, 2026-10-26\n",
     "keeps main's `Ready:` line"),
    ("Criteria.\n\n<!-- Ready: 2026-10-20, Fable r1 clean -->\n", "has a readiness record on main that cannot be"),
], ids=["plus-item", "leading-space", "after-html-comment", "code-span", "unreadable"])
def test_a_ready_merge_compares_main_s_readiness_record_in_any_markup(server, main_content, reason):
    ready_server(server, preview={"modifications": [{"seqNum": 60}], "stats": {"modifications": 1, "conflicts": 0}},
                 main=[spec(12), spec(60, status="Backlog", content=main_content)],
                 branch=[spec(60, status="Backlog", content=f"Changed criteria.\n{READY_LINE}\n", branch=True)])
    code, out = ready_run(server)
    assert code == 1 and f"ANYM-60 {reason}" in out, out
    assert not server.writes()


# spec: ANYM-405
@pytest.mark.parametrize("change, preview, fields", [
    ({"type": "Epic"}, None, "type"),
    ({"componentId": "c-other"}, None, "componentId"),
    ({"priority": "High"}, None, "priority"),
    ({"sources": []}, None, "sources"),
    ({}, ({"parentId": "p-main"}, {"parentId": "p-other"}), "parentId"),
], ids=["type", "component", "priority", "sources", "parent"])
def test_a_ready_merge_refuses_a_text_change_that_also_changes_the_spec_structure(server, change, preview, fields):
    main = [spec(12), spec(60, status="Backlog", content=f"Criteria.\n{READY_LINE}\n", sources=[anchor("a.py")])]
    copy = spec(60, status="Backlog", content=f"Changed criteria.\n{REFRESHED}\n", sources=[anchor("a.py")],
                branch=True)
    versions = preview or ({}, {})
    ready_server(server, main=main, branch=[dict(copy, **change)],
                 preview={"modifications": [{"mainVersion": {"seqNum": 60, **versions[0]},
                                             "branchVersion": {"seqNum": 60, **versions[1]}}],
                          "stats": {"modifications": 1, "conflicts": 0}})
    code, out = ready_run(server)
    assert code == 1 and f"ANYM-60 changes more than its text on proposal/ready ({fields})" in out, out
    assert not server.writes()


# spec: ANYM-405
def test_a_ready_merge_refuses_a_branch_that_changes_between_its_check_and_the_merge(server, monkeypatch):
    ready_server(server)
    original, reads = tiden_sync.listing, []

    def listing(cli, product, branch):
        specs = original(cli, product, branch)
        reads.append(branch)
        if len(reads) == 3:  # the branch read again just before the merge
            specs[70] = dict(specs[70], content=f"Another criterion.\n{READY_LINE}\n")
        return specs
    monkeypatch.setattr(tiden_sync, "listing", listing)
    code, out = ready_run(server)
    assert code == 1 and "Failed at step ready: proposal/ready or main changed while it was checked" in out, out
    assert not server.writes()


# spec: ANYM-405
def test_a_ready_merge_fails_its_read_back_when_main_differs_from_the_checked_branch(server, monkeypatch):
    ready_server(server)
    original = tiden_sync.write

    def write(cli, arguments, product):
        # Someone writes to the branch after its last check: an unready card and another criterion.
        state = server.state()
        state["branch_specs"]["proposal/ready"][0]["content"] = f"Another criterion.\n{READY_LINE}\n"
        state["branch_specs"]["proposal/ready"].append(spec(80, status="Backlog", branch=True))
        server.set(branch_specs=state["branch_specs"])
        original(cli, arguments, product)
    monkeypatch.setattr(tiden_sync, "write", write)
    code, out = ready_run(server)
    assert code == 1 and "Failed at step read-back: ANYM-80 is on main, but not on the checked branch" in out, out
    assert "ANYM-70 on main differs from the checked branch copy (content)" in out
    assert "Ready merge of" not in out


# spec: ANYM-405
def test_a_ready_merge_fails_its_read_back_when_a_spec_comes_back_as_another_record(server, monkeypatch):
    ready_server(server)
    original, reads = tiden_sync.listing, []

    def listing(cli, product, branch):
        specs = original(cli, product, branch)
        reads.append(branch)
        if len(reads) == 5:
            specs[60] = dict(specs[60], id="b-0060-0000-0000-0000-000000000000")
            specs[12] = dict(specs[12], id="x-0012")
        return specs
    monkeypatch.setattr(tiden_sync, "listing", listing)
    code, out = ready_run(server)
    assert code == 1 and "ANYM-60 is record b-0060-0000-0000-0000-000000000000 on main, not m-0060-" in out, out
    assert "ANYM-12 changed on main, but not on the checked branch" in out


# spec: ANYM-405
def test_a_ready_merge_whose_merge_call_fails_after_merging_reads_it_back_in_the_same_run(server, monkeypatch):
    ready_server(server)
    original = tiden_sync.write

    def write(cli, arguments, product):
        original(cli, arguments, product)
        raise tiden_sync.Refused("tiden branch merge timed out")
    monkeypatch.setattr(tiden_sync, "write", write)
    code, out = ready_run(server)
    assert code == 0, out
    assert "the merge call failed (tiden branch merge timed out), but proposal/ready is merged" in out
    assert [entry[:2] for entry in server.writes()] == [["branch", "merge"]]
    assert out.splitlines()[-1] == "Ready merge of proposal/ready done (ANYM-60, ANYM-70)."
    # A merge call that failed with the branch still open names no success and is not retried.
    ready_server(server)
    server.set(log=[], fault={"branch merge": "exit"})
    monkeypatch.setattr(tiden_sync, "write", original)
    code, out = ready_run(server)
    assert code == 1 and "Failed at step merge" in out and "proposal/ready is open" in out, out
    assert [entry[:2] for entry in server.writes()] == [["branch", "merge"]]


# spec: ANYM-405
def test_a_ready_merge_counts_a_new_card_as_a_child_of_its_parent_on_main(server, monkeypatch):
    parent = "m-0012-0000-0000-0000-000000000000"
    ready_server(server, preview={"additions": [{"seqNum": 70, "parentId": parent}],
                                  "stats": {"additions": 1, "conflicts": 0}},
                 branch=[dict(spec(70, status="Backlog", content=READY_LINE, branch=True), parentId=parent)])
    original = tiden_sync.write

    def write(cli, arguments, product):
        original(cli, arguments, product)
        state = server.state()
        for row in state["main"]:
            row["childrenCount"] += row["seqNum"] == 12
        server.set(main=state["main"])
    monkeypatch.setattr(tiden_sync, "write", write)
    code, out = ready_run(server)
    assert code == 0 and "Ready merge of proposal/ready done (ANYM-70)." in out, out
    assert server.main(70)["parentId"] == parent


# spec: ANYM-405
def test_a_ready_merge_fails_its_read_back_when_a_spec_moved_parent_after_its_check(server, monkeypatch):
    ready_server(server)
    original = tiden_sync.write

    def write(cli, arguments, product):
        # The branch moves ANYM-60 under ANYM-12 after its last check, text and readiness record kept.
        state = server.state()
        state["branch_specs"]["proposal/ready"][1]["parentId"] = "m-0012-0000-0000-0000-000000000000"
        server.set(branch_specs=state["branch_specs"])
        original(cli, arguments, product)
        state = server.state()
        for row in state["main"]:
            row["childrenCount"] += row["seqNum"] == 12
        server.set(main=state["main"])
    monkeypatch.setattr(tiden_sync, "write", write)
    code, out = ready_run(server)
    assert code == 1 and ("ANYM-60 on main differs from the checked branch copy "
                          "(parentId: none -> m-0012-0000-0000-0000-000000000000)") in out, out
    # ANYM-12 gained a child the checked branch did not add, but a derived child count is no evidence (ANYM-598).
    assert "ANYM-12 changed on main" not in out and "Ready merge of" not in out


# spec: ANYM-405
def test_a_ready_merge_fails_its_read_back_when_unchanged_specs_swap_parents_after_its_check(server, monkeypatch):
    first, second = "m-0012-0000-0000-0000-000000000000", "m-0013-0000-0000-0000-000000000000"
    ready_server(server, main=[spec(12, children=1), spec(13, children=1),
                               spec(60, status="Backlog", content=f"Criteria.\n{READY_LINE}\n"),
                               dict(spec(80), parentId=first), dict(spec(81), parentId=second)])
    original = tiden_sync.write

    def write(cli, arguments, product):
        # After the last check the branch swaps the parents of two specs it did not change: both counts stay.
        state = server.state()
        state["branch_specs"]["proposal/ready"] += [dict(spec(80), parentId=second), dict(spec(81), parentId=first)]
        server.set(branch_specs=state["branch_specs"])
        original(cli, arguments, product)
    monkeypatch.setattr(tiden_sync, "write", write)
    code, out = ready_run(server)
    assert code == 1, out
    assert f"ANYM-80 changed on main, but not on the checked branch (parentId: {first} -> {second})" in out, out
    assert f"ANYM-81 changed on main, but not on the checked branch (parentId: {second} -> {first})" in out, out
    assert "Ready merge of" not in out


def merged_then(server, monkeypatch, change):
    """Run `change` on the fake's state right after the merge call, then save it."""
    original = tiden_sync.write

    def write(cli, arguments, product):
        original(cli, arguments, product)
        state = server.state()
        change(state)
        server.set(main=state["main"])
    monkeypatch.setattr(tiden_sync, "write", write)


# spec: ANYM-405
@pytest.mark.parametrize("raised", [0, 2], ids=["not-yet", "by-two"])
def test_a_ready_merge_reads_back_green_whatever_its_parents_child_count_reads_after_it(server, monkeypatch, raised):
    parent = "m-0012-0000-0000-0000-000000000000"
    ready_server(server, preview={"additions": [{"seqNum": 70, "parentId": parent}],
                                  "stats": {"additions": 1, "conflicts": 0}},
                 branch=[dict(spec(70, status="Backlog", content=READY_LINE, branch=True), parentId=parent)])

    def count(state):
        for row in state["main"]:
            row["childrenCount"] += raised * (row["seqNum"] == 12)
    merged_then(server, monkeypatch, count)
    code, out = ready_run(server)
    assert code == 0 and "ready merge: ANYM-70 b-0070-0000-0000-0000-000000000000 Backlog on main (new)." in out, out
    assert server.main(70)["parentId"] == parent


# spec: ANYM-405
@pytest.mark.parametrize("landed", ["m-0013-0000-0000-0000-000000000000", None], ids=["other-parent", "no-parent"])
def test_a_ready_merge_names_both_parents_of_a_new_card_that_landed_under_another(server, monkeypatch, landed):
    parent = "m-0012-0000-0000-0000-000000000000"
    ready_server(server, main=[spec(12), spec(13)], preview={"additions": [{"seqNum": 70, "parentId": parent}],
                                                              "stats": {"additions": 1, "conflicts": 0}},
                 branch=[dict(spec(70, status="Backlog", content=READY_LINE, branch=True), parentId=parent)])

    def move(state):
        for row in state["main"]:
            if row["seqNum"] == 70:
                row.pop("parentId", None)
                row.update({"parentId": landed} if landed else {})
    merged_then(server, monkeypatch, move)
    code, out = ready_run(server)
    assert code == 1 and ("ANYM-70 on main differs from the checked branch copy "
                          f"(parentId: {parent} -> {landed or 'none'})") in out, out
    assert "Ready merge of" not in out


# spec: ANYM-405
def test_a_ready_merge_names_the_status_values_of_an_unchanged_spec_that_moved(server, monkeypatch):
    ready_server(server)

    def move(state):
        for row in state["main"]:
            row["status"] = "Done" if row["seqNum"] == 12 else row["status"]
    merged_then(server, monkeypatch, move)
    code, out = ready_run(server)
    assert code == 1 and "ANYM-12 changed on main, but not on the checked branch (status: Active -> Done)" in out, out


# spec: ANYM-405
def test_a_ready_merge_names_text_and_source_changes_without_their_values(server, monkeypatch, tmp_path, capsys):
    ready_server(server)
    sentinel = "sentinel-7f3a-private"

    def rewrite(state):
        for row in state["main"]:
            if row["seqNum"] == 12:
                row.update(content=f"{sentinel} content", title=f"{sentinel} title", sources=[anchor(f"{sentinel}.py")])
                row["extra\nReady merge of proposal/ready done."] = sentinel  # a field name cannot forge a line
    merged_then(server, monkeypatch, rewrite)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("TIDEN_PRODUCT_ID", "product")
    monkeypatch.setattr(sys, "argv", ["tiden_sync.py", "--ready-branch", "proposal/ready", "--cli", str(server.cli)])
    assert tiden_sync.main() == 1
    out, written = capsys.readouterr().out, summary.read_text()
    assert ("ANYM-12 changed on main, but not on the checked branch (content, extra\\nReady merge of proposal/ready done., sources, title)") in out, out
    assert "(content, extra" in written and sentinel not in out + written
    assert not any(line.startswith("Ready merge of") for line in (out + written).splitlines())
