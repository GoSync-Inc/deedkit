"""The tests autoloop pull requests add: their inventory, its exclusion from the required suites and the workflows
that keep them apart (ANYM-578)."""
import json
from pathlib import Path
import re

import pytest

import scripts.added_tests as added_tests
import scripts.autoloop as autoloop

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "GoSync-Inc/deedkit"
BASE, HEAD, MERGE_BASE = "b" * 40, "c" * 40, "d" * 40
GROUP_BASE, GROUP_HEAD, MIDDLE, OTHER_HEAD = "e" * 40, "f" * 40, "1" * 40, "2" * 40


def pull_event(branch="autoloop-fix-17", repository=REPOSITORY, number=17):
    return {"pull_request": {"number": number, "head": {"ref": branch, "sha": HEAD, "repo": {"full_name": repository}},
                             "base": {"sha": BASE}}}


def comparison(*files, merge_base=MERGE_BASE):
    return {"merge_base_commit": {"sha": merge_base}, "files": [{"filename": name, "status": status} for name, status in files]}


class Compare:
    """A synthetic compare endpoint: one comparison per `base...head`; an unknown one raises like an unreadable base."""

    def __init__(self, results):
        self.results, self.calls = results, []

    def __call__(self, path):
        self.calls.append(path)
        if path not in self.results:
            raise OSError(f"HTTP 404 for {path}")
        return self.results[path]


def listed(*files):
    get = Compare({f"compare/{BASE}...{HEAD}": comparison(*files)})
    return added_tests.required("pull_request", pull_event(), get=get)


# spec: ANYM-578
def test_an_added_test_file_and_every_added_file_of_a_test_directory_are_listed_against_the_merge_base():
    document = listed(("tests/test_new.py", "added"), ("tests/fixtures/data.json", "added"),
                      ("services/gateway/tests/helper_plugin.py", "added"))
    assert document == {"files": ["services/gateway/tests/helper_plugin.py", "tests/fixtures/data.json", "tests/test_new.py"],
                        "provenance": [{"pull_request": 17, "head": HEAD, "merge_base": MERGE_BASE}]}
    assert added_tests.inventory(json.dumps(document)) == document


# spec: ANYM-578
@pytest.mark.parametrize("name, status, expected", [
    ("tests/test_moved.py", "renamed", True),
    ("tests/fixtures/copy.json", "copied", True),
    ("tests/test_gone.py", "removed", False),
    ("tests/test_existing.py", "modified", False),
    ("services/calendar/tests/a/b/c/deep.txt", "added", True),
    ("whatsapp/whatsmeow/x_test.go", "added", True),
    ("whatsapp/whatsmeow/x.go", "added", False),
    ("packages/npm/src/client.test.mjs", "added", True),
    ("services/messaging/test_helpers.py", "added", True),
    ("services/messaging/helpers.py", "added", False),
    ("docs/testing.md", "added", False),
], ids=["rename", "copy", "deletion", "modified", "nested", "go-beside-package", "go-source", "node-test",
        "python-test-name", "python-source", "doc"])
def test_only_added_files_of_test_directories_and_added_test_files_anywhere_are_listed(name, status, expected):
    assert listed((name, status))["files"] == ([name] if expected else [])


# spec: ANYM-578
@pytest.mark.parametrize("event", [pull_event(branch="feature-17"), pull_event(repository="fork/deedkit"),
                                   pull_event(branch="feature-autoloop-17")], ids=["other-branch", "fork", "prefix-inside"])
def test_a_pull_request_that_is_not_an_autoloop_one_lists_nothing_and_compares_nothing(event):
    get = Compare({})
    assert added_tests.required("pull_request", event, get=get) == added_tests.EMPTY
    assert get.calls == []


# spec: ANYM-578
@pytest.mark.parametrize("event_name", ["push", "workflow_dispatch", "schedule"])
def test_other_events_exclude_nothing(event_name):
    assert added_tests.required(event_name, {}, get=Compare({})) == added_tests.EMPTY


def queue(*entries):
    return lambda body: {"data": {"repository": {"mergeQueue": {"entries": {"nodes": list(entries)}}}}}


def entry(number, branch, head_commit, base_commit, head_ref_oid, repository=REPOSITORY):
    return {"headCommit": {"oid": head_commit}, "baseCommit": {"oid": base_commit},
            "pullRequest": {"number": number, "headRefName": branch, "headRefOid": head_ref_oid,
                            "headRepository": {"nameWithOwner": repository}}}


GROUP = {"merge_group": {"head_sha": GROUP_HEAD, "base_sha": GROUP_BASE,
                         "head_ref": f"refs/heads/gh-readonly-queue/main/pr-21-{GROUP_HEAD}"}}
MIXED = queue(entry(21, "feature-21", GROUP_HEAD, MIDDLE, OTHER_HEAD), entry(20, "autoloop-fix-20", MIDDLE, GROUP_BASE, HEAD))


# spec: ANYM-578
def test_a_merge_group_lists_each_autoloop_pull_request_of_the_queue_against_its_own_merge_base():
    get = Compare({f"compare/{GROUP_BASE}...{HEAD}": comparison(("tests/test_twenty.py", "added"))})
    assert added_tests.required("merge_group", GROUP, get=get, graphql=MIXED) == {
        "files": ["tests/test_twenty.py"], "provenance": [{"pull_request": 20, "head": HEAD, "merge_base": MERGE_BASE}]}
    # The normal pull request of the group is never compared.
    assert get.calls == [f"compare/{GROUP_BASE}...{HEAD}"]


def broken_queue(body):
    raise OSError("HTTP 502")


# spec: ANYM-578
@pytest.mark.parametrize("event_name, event, get, graphql, reason", [
    ("pull_request", pull_event(), Compare({}), None, "cannot compare"),
    ("pull_request", {"pull_request": {**pull_event()["pull_request"], "base": {"sha": "main"}}}, Compare({}), None,
     "not a full SHA"),
    ("pull_request", pull_event(), Compare({f"compare/{BASE}...{HEAD}": {"files": []}}), None, "no merge base"),
    ("pull_request", pull_event(), Compare({f"compare/{BASE}...{HEAD}": comparison(
        *((f"tests/f{n}.json", "added") for n in range(300)))}), None, "may be truncated"),
    ("pull_request", pull_event(), Compare({f"compare/{BASE}...{HEAD}": {"merge_base_commit": {"sha": MERGE_BASE},
                                                                         "files": [{"filename": "tests/x.py"}]}}),
     None, "no name or status"),
    ("merge_group", GROUP, Compare({}), broken_queue, "cannot read the merge queue"),
    ("merge_group", GROUP, Compare({}), MIXED, "cannot compare"),
    ("merge_group", GROUP, Compare({}), queue(entry(20, "autoloop-fix-20", MIDDLE, GROUP_BASE, HEAD)), "no entry"),
    ("merge_group", GROUP, Compare({}), queue(entry(22, "feature-22", GROUP_HEAD, GROUP_BASE, OTHER_HEAD)),
     "not its pull request"),
    ("merge_group", {"merge_group": {**GROUP["merge_group"], "head_ref": "refs/heads/main"}}, Compare({}), MIXED,
     "no head, base or queue branch"),
], ids=["unreadable-base", "base-not-sha", "no-merge-base", "truncated", "file-without-status", "queue-unreadable",
        "group-base-unreadable", "group-commit-not-queued", "other-pull-request", "not-a-queue-branch"])
def test_an_unreadable_pull_request_base_or_queue_fails_closed(event_name, event, get, graphql, reason):
    with pytest.raises(added_tests.Refused, match=reason):
        added_tests.required(event_name, event, get=get, graphql=graphql)


# spec: ANYM-578
@pytest.mark.parametrize("text", [
    "", "not json", "[]", json.dumps({"files": []}), json.dumps({"files": [], "provenance": [], "extra": 1}),
    json.dumps({"files": ["tests/b.py", "tests/a.py"], "provenance": []}),
    json.dumps({"files": ["tests/a.py", "tests/a.py"], "provenance": []}),
    json.dumps({"files": ["../outside.py"], "provenance": []}), json.dumps({"files": ["/etc/passwd"], "provenance": []}),
    json.dumps({"files": [".git/config"], "provenance": []}), json.dumps({"files": [7], "provenance": []}),
    json.dumps({"files": [], "provenance": [{"pull_request": 1}]}),
], ids=["empty", "not-json", "list", "no-provenance", "extra-key", "unsorted", "duplicate", "parent", "absolute",
        "git", "not-a-name", "short-provenance"])
def test_a_missing_or_malformed_inventory_is_refused_and_never_reads_as_empty(text):
    with pytest.raises(added_tests.Refused):
        added_tests.inventory(text)


# spec: ANYM-578
def test_strip_and_run_fail_closed_without_the_inventory(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ADDED_TESTS", raising=False)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_new.py").write_text("")
    assert added_tests.main(["strip", "--root", str(tmp_path)]) == 1
    assert added_tests.main(["run", "--root", str(tmp_path), "--evidence", str(tmp_path / "evidence")]) == 1
    assert "exclusion inventory is missing" in capsys.readouterr().err
    assert (tmp_path / "tests/test_new.py").exists() and not (tmp_path / "evidence").exists()


# spec: ANYM-578
def test_strip_deletes_the_listed_files_from_every_root_prints_them_and_keeps_the_inventory(tmp_path, capsys):
    document = {"files": ["gomod/zz_test.go", "tests/missing.py", "tests/test_new.py"],
                "provenance": [{"pull_request": 17, "head": HEAD, "merge_base": MERGE_BASE}]}
    roots = [tmp_path / "checkout", tmp_path / "ci-tools"]
    for root in roots:
        for name in ("gomod/zz_test.go", "tests/test_new.py", "tests/test_existing.py"):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text("")
    assert added_tests.strip(document, roots, tmp_path / "evidence") == document["files"]
    for root in roots:
        assert sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) == ["tests/test_existing.py"]
    assert json.loads((tmp_path / "evidence/exclusions.json").read_text()) == document
    out = capsys.readouterr().out
    assert "Excluded 3 file(s)" in out and all(f"  {name}" in out for name in document["files"])


# spec: ANYM-578
def test_strip_refuses_a_listed_name_that_is_a_directory(tmp_path):
    (tmp_path / "tests/test_dir.py").mkdir(parents=True)
    with pytest.raises(added_tests.Refused, match="not a file"):
        added_tests.strip({"files": ["tests/test_dir.py"], "provenance": []}, [tmp_path], None)


# spec: ANYM-578
def test_list_writes_the_inventory_of_an_event_that_excludes_nothing_to_the_step_output(tmp_path, monkeypatch):
    (tmp_path / "event.json").write_text(json.dumps({"ref": "refs/heads/main"}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "event.json"))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    assert added_tests.main(["list"]) == 0
    line = (tmp_path / "output").read_text().strip()
    assert line.startswith("inventory=") and added_tests.inventory(line.removeprefix("inventory=")) == added_tests.EMPTY


# spec: ANYM-578
def test_the_run_plan_is_one_process_per_language_and_project(tmp_path):
    files = {"tests/test_root.py": "", "services/gateway/tests/test_gate.py": "", "tests/fixtures/data.json": "",
             "whatsapp/whatsmeow/go.mod": "", "whatsapp/whatsmeow/x_test.go": "func TestAdded(t *testing.T) {}\n",
             "whatsapp/whatsmeow/sub/y_test.go": "func TestDeeper(t *testing.T) {}\n",
             "packages/npm/package.json": "{}", "packages/npm/package-lock.json": "{}",
             "packages/npm/test/new.test.mjs": ""}
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    names = sorted(set(files) - {"whatsapp/whatsmeow/go.mod", "packages/npm/package.json", "packages/npm/package-lock.json"})
    plan = {(item["language"], item["project"]): item for item in added_tests.plan({"files": names, "provenance": []}, tmp_path)}
    assert set(plan) == {("python", "."), ("python", "services/gateway"), ("go", "whatsapp/whatsmeow"), ("node", "packages/npm")}
    assert plan[("python", ".")]["argv"][-1] == "tests/test_root.py"
    assert plan[("python", "services/gateway")]["argv"][-1] == "tests/test_gate.py"
    assert plan[("go", "whatsapp/whatsmeow")]["argv"] == ["go", "test", "-count=1", "-run", "^(TestAdded|TestDeeper)$", "./.", "./sub"]
    assert plan[("node", "packages/npm")]["prepare"] == [["npm", "ci", "--ignore-scripts"]]
    assert plan[("node", "packages/npm")]["argv"] == ["node", "--test", "test/new.test.mjs"]


# spec: ANYM-578
def test_the_listed_test_directories_and_file_names_are_the_autoloop_envelopes():
    assert (added_tests.TEST_DIR.pattern, added_tests.TEST_FILE.pattern) == (autoloop.TEST_DIR.pattern, autoloop.TEST_FILE.pattern)
    assert added_tests.MAX_FILES == autoloop.MAX_FILES


# spec: ANYM-578
@pytest.mark.parametrize("name", ["tests/__init__.pyc", "services/gateway/tests/fixtures/helper.cpython-313.pyc",
                                  "tests/__pycache__/checker.cpython-313.pyc", "whatsapp/__pycache__/notes.txt",
                                  "tests/fixtures/native.so", "tests/fixtures/native.pyd", "tests/fixtures/native.dylib",
                                  "packages/npm/vendor/lib.so"],
                         ids=["pyc", "nested-pyc", "pycache-pyc", "pycache-any", "so", "pyd", "dylib", "so-outside-tests"])
def test_an_added_executable_fixture_anywhere_is_an_autoloop_protected_path_hit(name):
    hits = autoloop.diff_hits([{"filename": name, "status": "added", "patch": "@@ -0,0 +1 @@\n+x"}])
    assert ("protected path", name) in hits


def workflow(name):
    import yaml
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


TEST_STEP = re.compile(r"\bpytest\b|gateway_read_load\.py|\bgo (test|vet|build)\b|node --test|\bnpm (test|ci|run)\b|scripts/smoke\.py|docker build")


# spec: ANYM-578
@pytest.mark.parametrize("name", ["checks.yml", "whatsmeow.yml"])
def test_every_job_that_runs_or_builds_tests_strips_the_added_files_before_its_first_test_step(name):
    jobs = workflow(name)["jobs"]
    tested = 0
    for job_name, job in jobs.items():
        steps = job.get("steps") or []
        runs = [str(step.get("run", "")) for step in steps]
        first = next((i for i, text in enumerate(runs) if TEST_STEP.search(text)), None)
        if first is None or job_name == "added":
            continue
        tested += 1
        strips = [i for i, text in enumerate(runs) if "added_tests.py strip --root . " in text]
        assert strips and strips[0] < first, job_name
        step = steps[strips[0]]
        assert step["env"]["ADDED_TESTS"] == "${{ inputs.added_tests }}", job_name
        assert "--evidence" in runs[strips[0]], job_name
    assert tested >= (6 if name == "checks.yml" else 1)


# spec: ANYM-578
def test_the_added_test_job_runs_only_the_added_tests_and_is_part_of_evidence_and_ci_gate():
    checks, ci = workflow("checks.yml")["jobs"], workflow("ci.yml")["jobs"]
    added = checks["added"]
    runs = "\n".join(str(step.get("run", "")) for step in added["steps"])
    assert "added_tests.py run --root ." in runs and "added_tests.py strip --root ci-tools" in runs
    assert "added_tests.py strip --root . " not in runs
    assert "added" in checks["evidence"]["needs"]
    require = next(step for step in checks["evidence"]["steps"] if step.get("name") == "Require every test job to have succeeded")
    assert require["env"]["ADDED_RESULT"] == "${{ needs.added.result }}" and 'test "$ADDED_RESULT" = success' in require["run"]
    for job in ("pr_reuse", "checks", "native", "gate"):
        needs = ci[job]["needs"]
        assert "added_list" in ([needs] if isinstance(needs, str) else needs), job
    gate = "\n".join(str(step.get("run", "")) for step in ci["gate"]["steps"])
    assert 'test "$ADDED_LIST_RESULT" = success' in gate
    assert ci["checks"]["with"]["added_tests"] == ci["native"]["with"]["added_tests"] == "${{ needs.added_list.outputs.inventory }}"
    assert ci["added_list"]["permissions"] == {"contents": "read", "pull-requests": "read"}
