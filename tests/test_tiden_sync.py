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
    assert "pkg/x_test.go TestGo not linked: CI publishes no Go report" in out


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


def sync_workflow():
    import yaml
    return yaml.safe_load((Path(SCRIPT).parents[1] / ".github/workflows/tiden-sync.yml").read_text())


# spec: ANYM-405
def test_the_sync_workflow_runs_after_main_ci_serialized_with_main_tools_and_a_dry_run():
    value = sync_workflow()
    triggers = value[True]  # yaml reads the `on` key as a boolean
    assert set(triggers) == {"workflow_run", "workflow_dispatch"}
    assert triggers["workflow_run"] == {"workflows": ["CI"], "types": ["completed"]}
    assert triggers["workflow_dispatch"]["inputs"]["sha"]["required"] is True
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
    assert holders == [steps[-1]["name"]] and "--dry-run" in steps[-1]["run"].split()
    assert all("secrets." not in step.get("run", "") and "GITHUB_PATH" not in step.get("run", "") for step in steps)

    def pinned(text):
        return next(line.strip() for line in text.splitlines() if "sha256sum --check" in line)
    install = next(step for step in steps if step.get("name") == "Install pinned Tiden CLI with integrity check")
    assert pinned(install["run"]) == pinned((Path(SCRIPT).parents[1] / ".github/workflows/ci.yml").read_text())


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
