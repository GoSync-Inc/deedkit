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
                          "docs/x.md": "doc\n"},
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
    ({"additions": [{"title": "no number"}], "modifications": []}, "without a spec number"),
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
    assert sent[1]["metadata"] == {"historical_source_id": "src-app/old.py",
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
    assert code == 1 and "Failed at step anchors: read-back of ANYM-12: 1 source(s) lost" in out
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
    merged(root, server, {"docs/x.md": "doc\n"}, "Delivers: ANYM-12, ANYM-13, ANYM-14")
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
