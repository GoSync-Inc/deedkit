"""A main push run reuses the merge queue's verification of its commit (ANYM-322); synthetic GitHub and Tiden only."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
import yaml

from scripts import ci_reuse, production, tiden_ci

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = production.REPOSITORY
SHA = "a" * 40
QUEUE_ID, PUSH_ID = 501, 900
QUEUE_BRANCH = production.QUEUE_BRANCH_PREFIX + "pr-7-" + "c" * 40
PRODUCER_URL = f"https://github.com/{REPOSITORY}/actions/runs/{QUEUE_ID}/attempts/2"
PUSH_URL = f"https://github.com/{REPOSITORY}/actions/runs/{PUSH_ID}/attempts/1"
PRODUCT = "11111111-2222-4333-8444-555555555555"
CASE = '<testsuite><testcase name="{name}" file="tests/{name}.py"/><testcase name="later" file="tests/{name}.py"><skipped/></testcase></testsuite>'
# The Go reports name real tests of the repository's Go modules (prepare reads ROOT); the streams are synthetic.
GO_IDENTITIES = json.loads((ROOT / "tests/fixtures/tiden-go-identities.json").read_text())


def go_test(check):
    """The package and the name of one real top-level test of a Go check's module."""
    module = tiden_ci.go_module_of(ROOT, check)
    return module, next(item["title"] for item in GO_IDENTITIES
                        if item["signature"].startswith(module + "::") and "/" not in item["title"])


def go_stream(check, action="pass"):
    package, test = go_test(check)
    return "".join(json.dumps(event) + "\n" for event in (
        {"Action": "run", "Package": package, "Test": test},
        {"Action": action, "Package": package, "Test": test, "Elapsed": 0.01}, {"Action": action, "Package": package}))


def queue_evidence(tmp_path, attempt="2", sha=SHA, git_branch=QUEUE_BRANCH, event="merge_group", tiden_branch="",
                   producer_url=PRODUCER_URL, shard_outcomes=None, shard_reports=None):
    """The `ci-evidence` directory a passing merge queue run uploads, built by the real assemble and prepare."""
    fragments = tmp_path / "fragments"
    verify, load = fragments / "ci-evidence-verify", fragments / "ci-evidence-load"
    for fragment in (verify, load):
        fragment.mkdir(parents=True)
        (fragment / "source-sha").write_text(sha + "\n")
        (fragment / "attempt").write_text(attempt + "\n")
    for name in tiden_ci.FRAGMENTS["verify"][1][1:]:
        (verify / name).write_text(CASE.format(name=name.split(".")[0].replace("-", "_")))
    for module, required in tiden_ci.REQUIRED_EXECUTIONS.items():  # each holds exactly its required executions
        (verify / tiden_ci.MODULE_REPORTS[module][0]).write_text("<testsuite>" + "".join(
            '<testcase name="{1}" file="{0}"/>'.format(*node.split("::")) for node in sorted(required)) + "</testsuite>")
    (verify / "metadata.json").write_text(json.dumps({
        "sha": sha, "git_branch": git_branch, "tiden_branch": tiden_branch, "checks": dict.fromkeys(tiden_ci.CHECKS, "success"),
        "module_checks": dict.fromkeys(tiden_ci.FRAGMENTS["verify"][0], "success"), "ci_url": "unused"}))
    (load / "load.xml").write_text(CASE.format(name="load"))
    (load / "load.json").write_text(json.dumps({"version": 1, "status": "passed", "sha": sha}))
    (load / "outcomes.json").write_text(json.dumps({"module_checks": {"load": "success"}}))
    for job in tiden_ci.SHARD_JOBS:  # the shard jobs of the root Python suite; a None outcome uploads nothing
        outcome = (shard_outcomes or {}).get(job, "success")
        if outcome is None:
            continue
        shard = fragments / f"ci-evidence-{job}"
        shard.mkdir()
        (shard / "source-sha").write_text(sha + "\n")
        (shard / "attempt").write_text(attempt + "\n")
        (shard / f"{job}.xml").write_text((shard_reports or {}).get(job) or CASE.format(name=job.replace("-", "_")))
        (shard / "outcomes.json").write_text(json.dumps({"checks": {"python": outcome}}))
    go = fragments / "ci-evidence-go"  # the `go` job: a `go test -json` stream per Go module
    go.mkdir()
    (go / "source-sha").write_text(sha + "\n")
    (go / "attempt").write_text(attempt + "\n")
    for check in tiden_ci.GO_MODULES:
        (go / tiden_ci.MODULE_REPORTS[check][0]).write_text(go_stream(check))
    (go / "outcomes.json").write_text(json.dumps({"module_checks": dict.fromkeys(tiden_ci.GO_MODULES, "success")}))
    evidence = tmp_path / "queue-evidence"
    producers = {job: ("success", sha) for job in tiden_ci.FRAGMENTS}
    metadata = tiden_ci.assemble(evidence, fragments, producer_url, producers)
    metadata.pop("missing")
    tiden_ci.prepare(ROOT, evidence, {**metadata, "pr_tiden_links": [], "event": event})
    return evidence


def archive(directory, change=None, extra=(), drop=()):
    """Zip the evidence the way upload-artifact does; `change` maps a file name to new content."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        for path in sorted(directory.iterdir()):
            if path.name not in drop:
                output.writestr(path.name, (change or {}).get(path.name, path.read_bytes()))
        for info, content in extra:
            output.writestr(info, content)
    return buffer.getvalue()


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def queue_run(run_id=QUEUE_ID, attempt=2, **change):
    return {"id": run_id, "run_attempt": attempt, "head_sha": SHA, "event": "merge_group", "head_branch": QUEUE_BRANCH,
            "status": "completed", "conclusion": "success", "head_repository": {"full_name": REPOSITORY},
            "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}", "run_started_at": "2026-10-17T08:20:00Z",
            **change}


def artifact(artifact_id=77, created="2026-10-17T08:30:00Z", data=b"", **change):
    return {"id": artifact_id, "name": "ci-evidence", "expired": False, "created_at": created, "size_in_bytes": len(data) or 1,
            "digest": digest(data), "workflow_run": {"id": QUEUE_ID, "head_sha": SHA, "repository_id": 1,
                                                     "head_repository_id": 1}, **change}


class GitHub:
    """A synthetic GitHub API for one commit: its queue runs, their jobs, attempts and artifacts."""

    def __init__(self, runs=None, artifacts=None, jobs=None, started="2026-10-17T08:20:00Z"):
        self.runs = [queue_run()] if runs is None else runs
        self.artifacts = artifacts if artifacts is not None else [artifact()]
        self.jobs = jobs or [{"name": name, "status": "completed", "conclusion": "success"}
                             for name in ("checks / verify", "CI gate")]
        self.started, self.calls, self.after = started, [], {}

    def __call__(self, path):
        self.calls.append(path)
        for prefix, action in list(self.after.items()):
            if path.startswith(prefix):
                action(self)
        if path.startswith("actions/workflows/ci.yml/runs?"):
            assert "event=merge_group" in path and f"head_sha={SHA}" in path
            return {"total_count": len(self.runs), "workflow_runs": self.runs}
        if path.startswith(f"actions/runs/{QUEUE_ID}/attempts/") and "/jobs?" in path:
            return {"total_count": len(self.jobs), "jobs": self.jobs}
        if path.startswith(f"actions/runs/{QUEUE_ID}/attempts/"):
            attempt = int(path.rsplit("/", 1)[1])
            return {"id": QUEUE_ID, "run_attempt": attempt, "run_started_at": self.started}
        if path.startswith(f"actions/runs/{QUEUE_ID}/artifacts?"):
            assert "name=ci-evidence" in path
            return {"total_count": len(self.artifacts), "artifacts": self.artifacts}
        if path.startswith("actions/artifacts/"):
            wanted = int(path.split("/")[2])
            found = next((value for value in self.artifacts if value["id"] == wanted), None)
            if found is None:
                raise production.Refused("HTTP 404")
            return found
        raise AssertionError(path)


def pin(data, artifact_id="77"):
    return {"run_id": str(QUEUE_ID), "run_attempt": "2", "artifact_id": artifact_id, "artifact_digest": digest(data)}


def adopt(tmp_path, github, data, out="published"):
    return ci_reuse.adopt(pin(data), SHA, REPOSITORY, str(PUSH_ID), "1", tmp_path / out, get=github,
                          fetch=lambda path: data)


def test_a_passed_queue_run_of_the_commit_is_pinned_by_run_attempt_and_artifact():
    github = GitHub(artifacts=[artifact(data=b"zip")])
    assert ci_reuse.decide(SHA, REPOSITORY, get=github, sleep=lambda seconds: None) == {
        "run_id": str(QUEUE_ID), "run_attempt": "2", "artifact_id": "77", "artifact_digest": digest(b"zip")}
    # Story 1's rules decide the queue run: both required jobs of its latest attempt, then a re-read.
    assert any(f"actions/runs/{QUEUE_ID}/attempts/2/jobs?" in call for call in github.calls)


@pytest.mark.parametrize("github, reason", [
    (GitHub(runs=[]), "no merge queue run"),
    (GitHub(runs=[queue_run(), queue_run(run_id=QUEUE_ID + 1, conclusion="failure")]), "not a completed success"),
    (GitHub(runs=[queue_run(head_branch="gh-readonly-queue/other/pr-7")]), "not a completed success"),
    (GitHub(jobs=[{"name": "CI gate", "status": "completed", "conclusion": "success"}]), "did not pass"),
    (GitHub(artifacts=[]), "0 current evidence artifacts"),
    (GitHub(artifacts=[artifact(expired=True)]), "0 current evidence artifacts"),
    (GitHub(artifacts=[artifact(created="2026-10-17T08:10:00Z")]), "0 current evidence artifacts"),
    (GitHub(artifacts=[artifact(), artifact(artifact_id=78, created="2026-10-17T08:31:00Z")]), "2 current"),
    (GitHub(artifacts=[artifact(digest="")]), "no digest"),
    (GitHub(artifacts=[artifact(workflow_run={"id": QUEUE_ID, "head_sha": "b" * 40})]), "does not belong"),
], ids=["absent", "newer-failed-queue-run", "other-queue", "verify-missing", "no-artifact", "expired",
        "earlier-attempt-only", "two-current", "no-digest", "other-commit-artifact"])
def test_without_one_current_evidence_artifact_of_a_passed_queue_run_the_push_run_verifies_in_full(github, reason):
    with pytest.raises(ci_reuse.Refused, match=reason):
        ci_reuse.decide(SHA, REPOSITORY, get=github, sleep=lambda seconds: None)


def test_an_earlier_attempts_evidence_beside_the_latest_attempts_is_ignored():
    github = GitHub(artifacts=[artifact(artifact_id=70, created="2026-10-17T08:05:00Z"), artifact(artifact_id=77)])
    assert ci_reuse.decide(SHA, REPOSITORY, get=github, sleep=lambda seconds: None)["artifact_id"] == "77"


class Clock:
    """A synthetic monotonic clock: sleeping, and optionally every API call, advances it."""

    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_the_decision_waits_for_a_running_queue_run_and_never_reuses_one_that_does_not_finish():
    github, clock = GitHub(runs=[queue_run(status="in_progress", conclusion=None)]), Clock()
    github.after["actions/workflows"] = lambda state: len(github.calls) > 3 and state.runs[0].update(
        status="completed", conclusion="success")
    assert ci_reuse.decide(SHA, REPOSITORY, get=github, sleep=clock.sleep, clock=clock)["run_id"] == str(QUEUE_ID)
    assert clock.sleeps and set(clock.sleeps) == {ci_reuse.QUEUE_POLL_SECONDS}
    stuck, clock = GitHub(runs=[queue_run(status="in_progress", conclusion=None)]), Clock()
    with pytest.raises(ci_reuse.Refused, match="not a completed success"):
        ci_reuse.decide(SHA, REPOSITORY, get=stuck, sleep=clock.sleep, clock=clock)
    assert clock.now <= ci_reuse.QUEUE_WAIT_SECONDS


def test_slow_api_responses_never_stretch_the_wait_for_the_queue_run():
    clock = Clock()
    stuck = GitHub(runs=[queue_run(status="in_progress", conclusion=None)])
    stuck.after[""] = lambda state: setattr(clock, "now", clock.now + 11)  # every API call takes 11 seconds
    with pytest.raises(ci_reuse.Refused, match="not a completed success"):
        ci_reuse.decide(SHA, REPOSITORY, get=stuck, sleep=clock.sleep, clock=clock)
    # The last poll starts before the deadline; the remaining reads of queue_verification follow it.
    assert clock.now <= ci_reuse.QUEUE_WAIT_SECONDS + 3 * 11
    ci, _ = workflows()
    decide = next(step for step in ci["jobs"]["reuse"]["steps"] if step.get("id") == "decide")
    # A lookup that hangs anyway ends at the step timeout, recorded as a full verification, inside the job's.
    assert decide["continue-on-error"] is True and decide["timeout-minutes"] * 60 > ci_reuse.QUEUE_WAIT_SECONDS
    assert decide["timeout-minutes"] < ci["jobs"]["reuse"]["timeout-minutes"]


def test_only_an_exact_commit_of_this_repository_is_considered():
    for sha, repository in [(SHA, "fork/deedkit"), ("A" * 40, REPOSITORY), ("a" * 39, REPOSITORY)]:
        with pytest.raises(ci_reuse.Refused, match="exact commit of this repository"):
            ci_reuse.decide(sha, repository, get=GitHub(), sleep=lambda seconds: None)


def test_a_lookup_error_before_the_choice_means_full_verification_and_a_green_decision_step(tmp_path, monkeypatch, capsys):
    output = tmp_path / "output"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_SHA", SHA)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setattr(sys, "argv", ["ci_reuse.py", "decide"])

    def unavailable(path):
        raise OSError("GitHub API unavailable")
    monkeypatch.setattr(production, "api", unavailable)
    monkeypatch.setattr(ci_reuse.decide, "__defaults__", (unavailable, lambda seconds: None, Clock()))
    assert ci_reuse.main() == 0
    assert output.read_text() == "decision=full\n"
    assert "Full verification" in capsys.readouterr().out
    output.write_text("")
    github = GitHub(artifacts=[artifact(data=b"zip")])
    monkeypatch.setattr(ci_reuse.decide, "__defaults__", (github, lambda seconds: None, Clock()))
    assert ci_reuse.main() == 0
    assert output.read_text().splitlines() == ["decision=reuse", f"run_id={QUEUE_ID}", "run_attempt=2", "artifact_id=77",
                                               "artifact_digest=" + digest(b"zip")]


def test_validated_queue_evidence_is_published_unchanged_with_this_push_runs_record(tmp_path):
    evidence = queue_evidence(tmp_path)
    data = archive(evidence)
    github = GitHub(artifacts=[artifact(data=data)])
    record = adopt(tmp_path, github, data)
    published = tmp_path / "published"
    assert {path.name for path in published.iterdir()} == ci_reuse.EVIDENCE_FILES | {"publication.json"}
    assert all((published / name).read_bytes() == (evidence / name).read_bytes() for name in ci_reuse.EVIDENCE_FILES)
    assert record == json.loads((published / "publication.json").read_text()) == {
        "schema": 1, "event": "push", "git_branch": "main", "repository": REPOSITORY, "run_id": str(PUSH_ID),
        "run_attempt": "1", "sha": SHA, "ci_url": PUSH_URL,
        "producer": {"repository": REPOSITORY, "workflow": ".github/workflows/ci.yml", "run_id": str(QUEUE_ID),
                     "run_attempt": "2", "url": PRODUCER_URL, "artifact_id": "77", "artifact_digest": digest(data),
                     "sha": SHA}}
    # The artifact and the queue run are read again after the validation.
    assert github.calls.count("actions/artifacts/77") == 2


def rewrite(evidence, name, **change):
    return json.dumps({**json.loads((evidence / name).read_text()), **change}).encode()


def producers(evidence, **load):
    """Change the load producer consistently in both metadata and manifest, so only the producer rule can refuse."""
    changed = {**json.loads((evidence / "manifest.json").read_text())["producers"]}
    changed["load"] = {**changed["load"], **load}
    return {name: rewrite(evidence, name, producers=changed) for name in ("metadata.json", "manifest.json")}


@pytest.mark.parametrize("damage, reason", [
    (lambda e: {"source-sha": b"b" * 40}, "SHA field"),
    (lambda e: {"load.json": rewrite(e, "load.json", sha="b" * 40)}, "SHA field"),
    (lambda e: {"metadata.json": rewrite(e, "metadata.json", sha="b" * 40)}, "SHA field"),
    (lambda e: {"manifest.json": rewrite(e, "manifest.json", sha="b" * 40)}, "SHA field"),
    (lambda e: {"metadata.json": rewrite(e, "metadata.json", ci_url=PRODUCER_URL.replace("/attempts/2", "/attempts/1")),
                "manifest.json": rewrite(e, "manifest.json", ci_url=PRODUCER_URL.replace("/attempts/2", "/attempts/1"))},
     "pinned queue run attempt"),
    (lambda e: {"manifest.json": rewrite(e, "manifest.json", ci_url=PUSH_URL)}, "pinned queue run attempt"),
    (lambda e: {"metadata.json": rewrite(e, "metadata.json", git_branch="main"),
                "manifest.json": rewrite(e, "manifest.json", git_branch="main", event="push")}, "merge queue run"),
    (lambda e: {"manifest.json": rewrite(e, "manifest.json", checks={**dict.fromkeys(tiden_ci.CHECKS, "success"),
                                                                     "smoke": "skipped"})},
     "pinned queue run attempt"),
    (lambda e: {"manifest.json": rewrite(e, "manifest.json", check_counts={"passed": 12, "failed": 1})}, "every check"),
    (lambda e: {"load.json": rewrite(e, "load.json", status="failed")}, "every check"),
    (lambda e: {"manifest.json": rewrite(e, "manifest.json", test_counts={"passed": 99})}, "test counts"),
    (lambda e: {"normalized-npm.xml": CASE.replace("<skipped/>", "<failure/>").format(name="npm").encode()}, "npm report"),
    (lambda e: {"normalized-calls.xml": b'<testsuite><testcase name="x" file="t.py"><skipped/></testcase></testsuite>'},
     "calls report"),
    (lambda e: {"normalized-python.xml": b'<!DOCTYPE x><testsuite/>'}, "python report is unsafe"),
    (lambda e: {"manifest.json": b"{"}, "not valid JSON"),
    (lambda e: {"ci-checks.json": json.dumps(json.loads((e / "ci-checks.json").read_text())[:-1]).encode()}, "passed CI and module checks"),
    (lambda e: producers(e, attempt="3"), "did not produce passing evidence"),
    (lambda e: producers(e, result="failure"), "did not produce passing evidence"),
    (lambda e: producers(e, sha="b" * 40), "did not produce passing evidence"),
], ids=["source-sha", "load-report-sha", "metadata-sha", "manifest-sha", "earlier-attempt", "publisher-url", "not-queue",
        "skipped-check", "failed-check-count", "failed-load", "count-mismatch", "failed-test", "only-skipped", "doctype",
        "malformed", "missing-check-row", "later-attempt-producer", "failed-producer", "other-commit-producer"])
def test_invalid_queue_evidence_fails_the_push_run_after_the_choice(tmp_path, damage, reason):
    evidence = queue_evidence(tmp_path)
    data = archive(evidence, change=damage(evidence))
    with pytest.raises(ci_reuse.Refused, match=reason):
        adopt(tmp_path, GitHub(artifacts=[artifact(data=data)]), data)


def test_a_check_skipped_in_both_metadata_and_manifest_is_refused(tmp_path):
    evidence = queue_evidence(tmp_path)
    skipped = {**dict.fromkeys(tiden_ci.CHECKS, "success"), "smoke": "skipped"}
    data = archive(evidence, change={"metadata.json": rewrite(evidence, "metadata.json", checks=skipped),
                                     "manifest.json": rewrite(evidence, "manifest.json", checks=skipped)})
    with pytest.raises(ci_reuse.Refused, match="every check"):
        adopt(tmp_path, GitHub(artifacts=[artifact(data=data)]), data)


@pytest.mark.parametrize("outcomes", [{"python-3": None}, {"python-2": "failure"}, {"python-4": "cancelled"}],
                         ids=["missing-shard", "failed-shard", "cancelled-shard"])
def test_queue_evidence_without_every_passing_python_shard_is_refused(tmp_path, outcomes):
    # The python check passes only when every shard of the root suite passed (scripts/python_shards.py).
    evidence = queue_evidence(tmp_path, shard_outcomes=outcomes)
    assert json.loads((evidence / "manifest.json").read_text())["checks"]["python"] != "success"
    data = archive(evidence)
    with pytest.raises(ci_reuse.Refused, match="not every check of the queue run passed|exactly the queue run's evidence files"):
        adopt(tmp_path, GitHub(artifacts=[artifact(data=data)]), data)


@pytest.mark.parametrize("report", [
    '<testsuite><testcase name="t" file="tests/t.py"><skipped/></testcase></testsuite>', "<testsuite/>",
    '<testsuites><testcase name="lost" file="tests/lost.py"><failure/></testcase></testsuites>',
], ids=["only-skipped-cases", "empty-report", "case-outside-a-suite"])
def test_queue_evidence_with_a_shard_report_without_executed_tests_is_refused(tmp_path, report):
    # Each shard's report is judged on its own; the other shards' passing cases never make it reusable.
    evidence = queue_evidence(tmp_path, shard_reports={"python-3": report})
    assert json.loads((evidence / "manifest.json").read_text())["checks"]["python"] != "success"
    data = archive(evidence)
    with pytest.raises(ci_reuse.Refused, match="not every check of the queue run passed"):
        adopt(tmp_path, GitHub(artifacts=[artifact(data=data)]), data)


def symlink(name):
    info = zipfile.ZipInfo(name)
    info.external_attr = 0o120777 << 16
    return info


@pytest.mark.parametrize("build, reason", [
    (lambda e: archive(e, drop=("normalized-gateway.xml",)), "exactly the queue run's evidence files"),
    (lambda e: archive(e, drop=("load.json",)), "exactly the queue run's evidence files"),
    (lambda e: archive(e, extra=[("run.sh", b"echo")]), "exactly the queue run's evidence files"),
    (lambda e: archive(e, extra=[("../manifest.json", b"{}")]), "exactly the queue run's evidence files"),
    (lambda e: archive(e, drop=("load.json",), extra=[(symlink("load.json"), b"/etc/passwd")]), "not a bounded regular file"),
    (lambda e: archive(e, change={"load.json": b" " * (tiden_ci.MAX_BYTES + 1)}), "not a bounded regular file"),
    (lambda e: b"not a zip", "not a valid zip"),
], ids=["missing-report", "missing-load-report", "extra-file", "traversal", "symlink", "oversized-file", "not-zip"])
def test_an_unsafe_or_incomplete_archive_is_never_extracted_as_evidence(tmp_path, build, reason):
    data = build(queue_evidence(tmp_path))
    with pytest.raises(ci_reuse.Refused, match=reason):
        adopt(tmp_path, GitHub(artifacts=[artifact(data=data)]), data)
    assert not (tmp_path / "published" / "publication.json").exists()
    assert not (tmp_path.parent / "manifest.json").exists()


def test_an_archive_that_expands_beyond_its_limit_is_refused(tmp_path, monkeypatch):
    evidence = queue_evidence(tmp_path)
    data = archive(evidence)
    monkeypatch.setattr(ci_reuse, "MAX_TOTAL_BYTES", 1000)
    with pytest.raises(ci_reuse.Refused, match="expands beyond"):
        adopt(tmp_path, GitHub(artifacts=[artifact(data=data)]), data)


@pytest.mark.parametrize("during, reason", [
    (lambda github: github.runs.append(queue_run(run_id=QUEUE_ID + 1, status="in_progress", conclusion=None)),
     "not a completed success"),
    (lambda github: github.runs[0].update(run_attempt=3), "newer merge queue run or rerun"),
    (lambda github: github.artifacts.clear(), "HTTP 404"),
    (lambda github: github.artifacts[0].update(expired=True), "gone, expired, replaced"),
], ids=["newer-queue-run", "rerun", "deleted-artifact", "expired-artifact"])
def test_a_newer_queue_run_rerun_or_lost_artifact_during_validation_fails_the_push_run(tmp_path, during, reason):
    evidence = queue_evidence(tmp_path)
    data = archive(evidence)
    github = GitHub(artifacts=[artifact(data=data)])
    downloaded = []

    def fetch(path):
        downloaded.append(path)
        during(github)
        return data
    with pytest.raises((ci_reuse.Refused, production.Refused), match=reason):
        ci_reuse.adopt(pin(data), SHA, REPOSITORY, str(PUSH_ID), "1", tmp_path / "published", get=github, fetch=fetch)
    assert downloaded == ["actions/artifacts/77/zip"]
    assert not (tmp_path / "published" / "publication.json").exists()


@pytest.mark.parametrize("change, reason", [
    ({"artifact_digest": digest(b"other")}, "gone, expired, replaced"),
    ({"run_id": str(QUEUE_ID + 1)}, "gone, expired, replaced"),
], ids=["other-digest", "other-run"])
def test_the_downloaded_archive_must_be_the_pinned_artifact_of_the_pinned_run(tmp_path, change, reason):
    evidence = queue_evidence(tmp_path)
    data = archive(evidence)
    with pytest.raises(ci_reuse.Refused, match=reason):
        ci_reuse.adopt({**pin(data), **change}, SHA, REPOSITORY, str(PUSH_ID), "1", tmp_path / "published",
                       get=GitHub(artifacts=[artifact(data=data)]), fetch=lambda path: data)
    pinned = GitHub(artifacts=[artifact(data=data)])
    with pytest.raises(ci_reuse.Refused, match="differs from the pinned artifact"):
        ci_reuse.adopt(pin(data), SHA, REPOSITORY, str(PUSH_ID), "1", tmp_path / "other", get=pinned,
                       fetch=lambda path: archive(evidence, drop=("load.xml",)))


def test_an_api_error_after_the_choice_fails_the_push_run(tmp_path, monkeypatch):
    evidence = queue_evidence(tmp_path)
    data = archive(evidence)
    for name, value in {"GITHUB_SHA": SHA, "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_RUN_ID": str(PUSH_ID),
                        "GITHUB_RUN_ATTEMPT": "1", "GITHUB_OUTPUT": str(tmp_path / "output"),
                        **{"QUEUE_" + key.upper(): value for key, value in pin(data).items()}}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["ci_reuse.py", "adopt", "--directory", str(tmp_path / "published")])

    def unavailable(path):
        raise OSError("GitHub API unavailable")
    monkeypatch.setattr(ci_reuse.adopt, "__defaults__", (unavailable, lambda path: data))
    with pytest.raises(OSError):
        ci_reuse.main()
    assert not (tmp_path / "output").exists()
    monkeypatch.setattr(ci_reuse.adopt, "__defaults__", (GitHub(artifacts=[artifact(data=data)]), lambda path: data))
    assert ci_reuse.main() == 0
    assert (tmp_path / "output").read_text() == f"validated_sha={SHA}\n"
    monkeypatch.delenv("QUEUE_ARTIFACT_ID")
    with pytest.raises(ci_reuse.Refused, match="did not pin"):
        ci_reuse.main()


def tiden_main(monkeypatch, directory, environment, *command):
    """Run tiden_ci as the push run's tiden or release job would, against a synthetic Tiden CLI."""
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        operation = args[1:3]
        manifest = json.loads((directory / "manifest.json").read_text())
        attempts = sum(manifest["test_counts"].values()) + len(tiden_ci.CHECKS) + len(tiden_ci.MODULE_REPORTS)
        values = {("branch", "list"): {"branches": [{"id": "main-id", "name": "main"}]},
                  ("run", "create"): {"seqNum": 5, "branchName": "main", "buildSha": SHA},
                  ("run", "get"): {"seqNum": 5, "branchName": "main", "buildSha": SHA, "status": "passed",
                                   "branchId": "main-id", "liveDocStatus": "succeeded",
                                   "stats": {"attempts": str(attempts)}},
                  ("run", "list"): {"runs": [{"id": "run-5", "buildSha": SHA, "status": "passed",
                                              "createdAt": "2026-10-17T08:30:00Z",
                                              "completedAt": "2026-10-17T08:30:05Z"}], "pagination": {}},
                  ("release", "create"): {"id": "release-id", "version": "deedkit@" + SHA,
                                          "environmentSlug": "github-actions-synthetic",
                                          "url": args[args.index("--url") + 1] if "--url" in args else "",
                                          "createdAt": "2026-10-17T08:40:00Z"},
                  ("gate", "check"): {"status": "VERDICT_STATUS_PASSED"}}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(values.get(tuple(operation), {})), stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    for name, value in {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_RUN_ID": str(PUSH_ID), "GITHUB_RUN_ATTEMPT": "1",
                        "GITHUB_SHA": SHA, "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
                        "TIDEN_PRODUCT_ID": PRODUCT, **environment}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("TIDEN_BRANCH_MAP", raising=False)
    monkeypatch.setattr(sys, "argv", ["tiden_ci.py", *command, "--directory", str(directory)])
    return tiden_ci.main(), calls


def published_evidence(tmp_path):
    evidence = queue_evidence(tmp_path)
    data = archive(evidence)
    adopt(tmp_path, GitHub(artifacts=[artifact(data=data)]), data)
    return evidence, tmp_path / "published"


def test_the_push_run_publishes_the_queue_evidence_to_main_under_its_own_run_url(tmp_path, monkeypatch):
    evidence, published = published_evidence(tmp_path)
    code, calls = tiden_main(monkeypatch, published, {}, "publish")
    assert code == 0
    create = next(call for call in calls if call[1:3] == ["run", "create"])
    assert create[create.index("--branch") + 1] == "main" and create[create.index("--build-sha") + 1] == SHA
    meta = [create[i + 1] for i, value in enumerate(create) if value == "--client-meta"]
    assert meta == ["git_branch=main", "ci_url=" + PUSH_URL, "tested_in=" + PRODUCER_URL]
    assert create[create.index("--description") + 1] == PUSH_URL
    assert json.loads((published / "tiden-route.json").read_text()) == {"branch": "main", "source": "explicit",
                                                                          "tested_in": PRODUCER_URL}
    report = next(call for call in calls if call[1:3] == ["run", "report"])
    names = sorted(Path(path).name for path in report[4:report.index("--product-id")])
    # The Go result rows go out re-identified for this push run (published-normalized-<check>.json).
    assert names == sorted([("published-" if check in tiden_ci.GO_MODULES else "") + tiden_ci.normalized_report(check)
                            for check in ci_reuse.REPORT_CHECKS] + ["ci-checks-published.json"])
    # Executions and outcomes are the queue run's; the check rows carry this push run's IDs.
    rows = json.loads((published / "ci-checks-published.json").read_text())
    queue_rows = json.loads((evidence / "ci-checks.json").read_text())
    assert [(row["signature"], row["execution"]) for row in rows] == [(row["signature"], row["execution"]) for row in queue_rows]
    checks = [row["signature"].split("::")[1] for row in rows]
    assert [row["id"] for row in rows] == [tiden_ci.uuid.uuid5(tiden_ci.uuid.NAMESPACE_URL, PUSH_URL + "/" + check).__str__()
                                           for check in checks]
    assert all(row["message"].endswith("tested in " + PRODUCER_URL) for row in rows)
    assert all((published / name).read_bytes() == (evidence / name).read_bytes() for name in ci_reuse.EVIDENCE_FILES)


def test_only_a_verified_passed_publication_reports_itself_published(tmp_path, monkeypatch):
    _, published = published_evidence(tmp_path)
    output = tmp_path / "github-output"
    output.touch()
    code, _ = tiden_main(monkeypatch, published, {"GITHUB_OUTPUT": str(output)}, "publish")
    assert code == 0 and output.read_text() == "verified=true\n"
    # A skipped publication (an unlinked pull request) exits 0 but published nothing, and a failed verdict exits 1.
    for status, exit_code in (("skipped", 0), ("failed", 1)):
        output.write_text("")
        monkeypatch.setattr(tiden_ci, "publish", lambda *args, status=status, **kwargs: {
            "seqNum": None, "status": status, "branchName": None, "branchId": None, "buildSha": SHA, "stats": {},
            "liveDocStatus": None})
        code, _ = tiden_main(monkeypatch, published, {"GITHUB_OUTPUT": str(output)}, "publish")
        assert code == exit_code and output.read_text() == ""


def test_the_push_run_records_the_release_under_its_own_run_url(tmp_path, monkeypatch):
    _, published = published_evidence(tmp_path)
    monkeypatch.setattr(tiden_ci, "GATE_SETTLE", tiden_ci.timedelta(0))
    code, calls = tiden_main(monkeypatch, published, {}, "release", "--environment", "github-actions-synthetic")
    assert code == 0
    create = next(call for call in calls if call[1:3] == ["release", "create"])
    assert create[create.index("--version") + 1] == "deedkit@" + SHA and create[create.index("--url") + 1] == PUSH_URL


@pytest.mark.parametrize("environment", [
    {"GITHUB_RUN_ATTEMPT": "2"}, {"GITHUB_RUN_ID": "901"}, {"GITHUB_SHA": "b" * 40}, {"GITHUB_REPOSITORY": "fork/deedkit"},
    {"GITHUB_RUN_ID": ""}, {"GITHUB_EVENT_NAME": "workflow_dispatch"}, {"GITHUB_REF": "refs/heads/feature"},
], ids=["other-attempt", "other-run", "other-commit", "other-repository", "no-environment", "not-push", "not-main"])
@pytest.mark.parametrize("command", [("publish",), ("release", "--environment", "github-actions-synthetic")],
                         ids=["publish", "release"])
def test_a_publication_record_of_another_run_attempt_or_commit_publishes_nothing(tmp_path, monkeypatch, environment, command):
    _, published = published_evidence(tmp_path)
    with pytest.raises(ValueError, match="publication record"):
        tiden_main(monkeypatch, published, environment, *command)
    assert not (published / "tiden-create.json").exists()


@pytest.mark.parametrize("command", [("publish",), ("release", "--environment", "github-actions-synthetic")],
                         ids=["publish", "release"])
def test_a_publication_record_beside_evidence_that_is_not_the_queue_runs_publishes_nothing(tmp_path, monkeypatch, command):
    _, published = published_evidence(tmp_path)
    manifest = json.loads((published / "manifest.json").read_text())
    for change in ({"event": "push", "git_branch": "main"}, {"sha": "b" * 40}, {"ci_url": PUSH_URL}):
        (published / "manifest.json").write_text(json.dumps({**manifest, **change}))
        with pytest.raises(ValueError, match="publication record"):
            tiden_main(monkeypatch, published, {}, *command)
    (published / "manifest.json").write_text(json.dumps(manifest))
    record = json.loads((published / "publication.json").read_text())
    producer = record["producer"]
    foreign = f"https://github.com/fork/deedkit/actions/runs/{QUEUE_ID}/attempts/2"
    for change in ({"event": "merge_group"}, {"git_branch": "feature"}, {"producer": {**producer, "sha": "b" * 40}},
                   {"producer": {"url": producer["url"], "sha": SHA}},
                   {"producer": {**producer, "repository": "fork/deedkit"}},
                   {"producer": {**producer, "workflow": ".github/workflows/other.yml"}},
                   {"producer": {**producer, "run_attempt": "1"}},
                   {"producer": {**producer, "run_id": str(QUEUE_ID + 1)}},
                   {"producer": {**producer, "run_id": str(PUSH_ID)}},
                   {"producer": {**producer, "artifact_digest": "md5:x"}},
                   {"producer": {**producer, "url": foreign}}):
        (published / "publication.json").write_text(json.dumps({**record, **change}))
        if change.get("producer", {}).get("url") == foreign:
            (published / "manifest.json").write_text(json.dumps({**manifest, "ci_url": foreign}))
        with pytest.raises(ValueError, match="publication record"):
            tiden_main(monkeypatch, published, {}, *command)


def test_without_a_publication_record_the_queue_evidence_still_publishes_and_records_nothing(tmp_path, monkeypatch, capsys):
    evidence = queue_evidence(tmp_path)
    code, calls = tiden_main(monkeypatch, evidence, {}, "publish")
    assert (code, calls) == (0, []) and "Tiden evidence not published" in capsys.readouterr().out
    with pytest.raises(ValueError, match="main push"):
        tiden_main(monkeypatch, evidence, {}, "release", "--environment", "github-actions-synthetic")


def workflows():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    checks = yaml.safe_load((ROOT / ".github/workflows/checks.yml").read_text())
    return ci, checks


def test_only_a_main_push_decides_and_only_the_reuse_job_reads_actions():
    ci, checks = workflows()
    job = ci["jobs"]["reuse"]
    assert job["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert job["permissions"] == {"contents": "read", "actions": "read"} and ci["permissions"] == {"contents": "read"}
    assert "actions" not in json.dumps({name: other.get("permissions") for name, other in ci["jobs"].items()
                                        if name not in ("reuse", "pr_reuse")})
    assert all("permissions" not in other for other in checks["jobs"].values())
    assert "secrets." not in json.dumps(job) and "TIDEN" not in json.dumps(job)
    steps = {step.get("id") or step.get("name") or step["uses"].split("@")[0]: step for step in job["steps"]}
    # The decision never fails the run; the validation and the publication of its evidence always can.
    assert steps["decide"]["continue-on-error"] is True and steps["decide"]["run"] == "python3 scripts/ci_reuse.py decide"
    assert "continue-on-error" not in steps["adopt"]
    assert steps["adopt"]["if"] == "steps.decide.outcome == 'success' && steps.decide.outputs.decision == 'reuse'"
    assert all("${{" not in step.get("run", "") for step in job["steps"])
    upload = steps["Publish the validated evidence as this run's evidence"]
    assert upload["if"] == "steps.adopt.outcome == 'success'" and upload["with"]["name"] == "ci-evidence"
    assert job["outputs"]["decision"] == "${{ steps.decide.outcome == 'success' && steps.decide.outputs.decision || 'full' }}"
    assert job["outputs"]["validated_sha"] == "${{ steps.adopt.outputs.validated_sha }}"
    # Package releases call checks.yml without the input, so they always verify in full.
    for kind in ("pypi", "npm"):
        release = yaml.safe_load((ROOT / f".github/workflows/publish-{kind}.yml").read_text())
        assert "reuse_queue_evidence" not in json.dumps(release)


def test_a_reusing_push_skips_only_the_test_jobs_and_still_builds_publishes_and_records():
    ci, checks = workflows()
    skip = ("${{ !cancelled() && !inputs.reuse_queue_evidence && !inputs.reuse_pr_verification "
            "&& needs.documentation.outputs.docs_only != 'true' }}")
    assert checks["jobs"]["verify"]["if"] == checks["jobs"]["load"]["if"] == skip
    assert checks["jobs"]["evidence"]["if"] == ("${{ always() && !inputs.reuse_queue_evidence && !inputs.reuse_pr_verification "
                                                "&& needs.documentation.outputs.docs_only != 'true' }}")
    tiden = checks["jobs"]["tiden"]
    assert tiden["needs"] == ["documentation", "evidence"] and tiden["if"].startswith("${{ always() && !cancelled()")
    assert "reuse_queue_evidence" not in json.dumps(tiden)
    assert "if" not in ci["jobs"]["images"] and "needs" not in ci["jobs"]["images"]
    # native skips only on the queue's pull request reuse, never on a main push (pr_reuse is skipped there).
    assert ci["jobs"]["native"]["needs"] == ["pr_reuse"]
    assert ci["jobs"]["native"]["if"] == "${{ !cancelled() && needs.pr_reuse.outputs.decision != 'reuse' }}"
    assert ci["jobs"]["publish"]["needs"] == ["gate"] and ci["jobs"]["release"]["needs"] == ["gate", "publish", "checks"]
    # The queue still gets no Tiden credential.
    assert ci["jobs"]["checks"]["secrets"]["TIDEN_API_TOKEN"] == (
        "${{ github.event_name != 'merge_group' && secrets.TIDEN_API_TOKEN || '' }}")


def test_a_reused_main_push_still_publishes_and_records_after_a_green_gate():
    ci, _ = workflows()
    jobs = ci["jobs"]
    # A main push always has a skipped ancestor of publish: pr_reuse runs only in the merge queue, and a reused
    # push skips the test jobs. Without a status function, GitHub's implicit success() skips a job when any
    # ancestor was skipped, so publish and the release record must test the results they depend on explicitly
    # (1647024..2af17b6 published no images).
    assert jobs["pr_reuse"]["if"] == "github.event_name == 'merge_group'" and "pr_reuse" in jobs["gate"]["needs"]
    main_push = "github.event_name == 'push' && github.ref == 'refs/heads/main' }}"
    assert jobs["publish"]["if"] == "${{ !cancelled() && needs.gate.result == 'success' && " + main_push
    assert jobs["release"]["if"] == ("${{ !cancelled() && needs.gate.result == 'success' "
                                     "&& needs.publish.result == 'success' && " + main_push)


GREEN = {"DECISION": "full", "REUSE_RESULT": "skipped", "REUSED_SHA": "", "VERIFIED_SHA": SHA, "EVENT_NAME": "pull_request",
         "REF": "refs/pull/7/merge", "EXPECTED_SHA": SHA, "DOCS_ONLY": "false", "DOCS_SHA": "", "PR_DECISION": "",
         "PR_REUSE_RESULT": "skipped", "PR_REUSED_SHA": ""}
REUSED = {"DECISION": "reuse", "REUSE_RESULT": "success", "REUSED_SHA": SHA, "VERIFIED_SHA": "", "EVENT_NAME": "push",
          "REF": "refs/heads/main", "EXPECTED_SHA": SHA, "DOCS_ONLY": "", "DOCS_SHA": "", "PR_DECISION": "",
          "PR_REUSE_RESULT": "skipped", "PR_REUSED_SHA": ""}


@pytest.mark.parametrize("environment, passes", [
    (GREEN, True),
    ({**GREEN, "DECISION": ""}, True),
    ({**GREEN, "VERIFIED_SHA": ""}, False),
    ({**GREEN, "VERIFIED_SHA": "b" * 40}, False),
    (REUSED, True),
    ({**REUSED, "REUSE_RESULT": "failure"}, False),
    ({**REUSED, "REUSED_SHA": ""}, False),
    ({**REUSED, "REUSED_SHA": "b" * 40}, False),
    ({**REUSED, "VERIFIED_SHA": SHA}, False),
    ({**REUSED, "EVENT_NAME": "merge_group"}, False),
    ({**REUSED, "REF": "refs/heads/feature"}, False),
], ids=["verified", "no-decision", "verify-skipped-without-reuse", "verified-other-commit", "reused", "validation-failed",
        "not-validated", "validated-other-commit", "verify-also-ran", "not-push", "not-main"])
def test_ci_gate_passes_a_skipped_verify_only_for_a_validated_reuse_of_this_commit(environment, passes):
    ci, _ = workflows()
    gate = ci["jobs"]["gate"]
    assert gate["needs"] == ["reuse", "pr_reuse", "checks", "native", "images"]
    step = gate["steps"][1]
    assert set(step["env"]) == set(GREEN)
    completed = subprocess.run(["bash", "-e", "-c", step["run"]], env={"PATH": "/usr/bin:/bin", **environment},
                               capture_output=True)
    assert (completed.returncode == 0) == passes


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_the_job_token_reaches_only_the_github_api_never_the_redirected_artifact_storage(monkeypatch):
    import email.message
    import urllib.error
    import urllib.request
    requests = []

    def redirect(location):
        headers = email.message.Message()
        headers["Location"] = location

        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                raise urllib.error.HTTPError(request.full_url, 302, "Found", headers, None)
        return lambda *handlers: Opener()

    def storage(request, timeout):
        requests.append(request)
        return Response(b"zip")
    monkeypatch.setenv("GH_TOKEN", "synthetic-job-token")
    monkeypatch.setattr(urllib.request, "urlopen", storage)
    monkeypatch.setattr(urllib.request, "build_opener", redirect("https://storage.example/blob?sig=x"))
    assert ci_reuse.download("actions/artifacts/77/zip") == b"zip"
    api, blob = requests
    assert api.full_url == f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/77/zip"
    assert api.get_header("Authorization") == "Bearer synthetic-job-token"
    assert blob.full_url == "https://storage.example/blob?sig=x"
    assert blob.get_header("Authorization") is None and "synthetic-job-token" not in json.dumps(blob.header_items())
    # A redirect that is not HTTPS is never followed, and an oversized archive is refused.
    requests.clear()
    monkeypatch.setattr(urllib.request, "build_opener", redirect("http://storage.example/blob"))
    with pytest.raises(urllib.error.HTTPError):
        ci_reuse.download("actions/artifacts/77/zip")
    assert len(requests) == 1
    monkeypatch.setattr(urllib.request, "build_opener", redirect("https://storage.example/blob"))
    monkeypatch.setattr(ci_reuse, "MAX_ARCHIVE_BYTES", 2)
    with pytest.raises(ci_reuse.Refused, match="size limit"):
        ci_reuse.download("actions/artifacts/77/zip")


# A merge queue run reuses a passing, full pull request run of exactly its group commit's tree (owner, 2026-10-18).
GROUP_SHA, BASE_SHA, HEAD_SHA, TESTED_SHA, TREE = "a" * 40, "c" * 40, "d" * 40, "e" * 40, "f" * 40
PR_NUMBER, PR_RUN_ID, PR_BRANCH = 7, 640, "claude/feature"
PR_URL = f"https://github.com/{REPOSITORY}/actions/runs/{PR_RUN_ID}/attempts/1"
GROUP_EVENT = {"merge_group": {"head_sha": GROUP_SHA, "head_ref": "refs/heads/" + QUEUE_BRANCH, "base_sha": BASE_SHA}}
PR_JOBS = ("checks / verify", "checks / load", "checks / evidence", "CI gate")


def pr_run(run_id=PR_RUN_ID, attempt=1, **change):
    return {"id": run_id, "run_attempt": attempt, "head_sha": HEAD_SHA, "event": "pull_request", "head_branch": PR_BRANCH,
            "status": "completed", "conclusion": "success", "head_repository": {"full_name": REPOSITORY},
            "created_at": "2026-10-17T08:00:00Z", **change}


class PullGitHub:
    """A synthetic GitHub API for one queue group: its pull request, that head's runs, jobs, artifacts and commits."""

    def __init__(self, data, runs=None, artifacts=None, jobs=None, pull=None, commits=None):
        self.data, self.runs, self.calls, self.after = data, [pr_run()] if runs is None else runs, [], {}
        self.artifacts = artifacts if artifacts is not None else [artifact(
            artifact_id=88, data=data, workflow_run={"id": PR_RUN_ID, "head_sha": HEAD_SHA, "repository_id": 1,
                                                     "head_repository_id": 1})]
        self.jobs = jobs if jobs is not None else [
            {"name": name, "status": "completed", "conclusion": "success"} for name in PR_JOBS]
        self.pull = {"number": PR_NUMBER, "state": "open", "base": {"ref": "main"},
                     "head": {"sha": HEAD_SHA, "ref": PR_BRANCH, "repo": {"full_name": REPOSITORY}}, **(pull or {})}
        self.commits = {TESTED_SHA: {"sha": TESTED_SHA, "tree": {"sha": TREE}, "parents": [{"sha": BASE_SHA}, {"sha": HEAD_SHA}]},
                        GROUP_SHA: {"sha": GROUP_SHA, "tree": {"sha": TREE}, "parents": [{"sha": BASE_SHA}]}, **(commits or {})}

    def __call__(self, path):
        self.calls.append(path)
        for prefix, action in list(self.after.items()):
            if path.startswith(prefix):
                action(self)
        if path == f"pulls/{PR_NUMBER}":
            return self.pull
        if path.startswith("actions/workflows/ci.yml/runs?"):
            assert "event=pull_request" in path and f"head_sha={HEAD_SHA}" in path
            return {"total_count": len(self.runs), "workflow_runs": self.runs}
        if "/jobs?" in path and any(path.startswith(f"actions/runs/{run['id']}/attempts/") for run in self.runs):
            return {"total_count": len(self.jobs), "jobs": self.jobs}
        if path.startswith(f"actions/runs/{PR_RUN_ID}/attempts/"):
            return {"id": PR_RUN_ID, "run_attempt": int(path.rsplit("/", 1)[1]), "run_started_at": "2026-10-17T08:20:00Z"}
        if path.startswith(f"actions/runs/{PR_RUN_ID}/artifacts?"):
            return {"total_count": len(self.artifacts), "artifacts": self.artifacts}
        if path.startswith("git/commits/"):
            return self.commits[path.split("/")[2]]
        raise AssertionError(path)


def pr_evidence(tmp_path, **change):
    """The `ci-evidence` a passing pull request run uploads: the tested merge commit, its head branch and run URL."""
    return queue_evidence(tmp_path, **{"attempt": "1", "sha": TESTED_SHA, "git_branch": PR_BRANCH, "event": "pull_request",
                                       "tiden_branch": "intent/2026-10-18-feature", "producer_url": PR_URL, **change})


def decide_group(github, event=GROUP_EVENT, event_name="merge_group", sha=GROUP_SHA, repository=REPOSITORY):
    return ci_reuse.decide_group(sha, repository, event_name, event, get=github, fetch=lambda path: github.data)


def test_a_queue_group_reuses_a_passed_full_pull_request_run_of_exactly_its_tree(tmp_path):
    github = PullGitHub(archive(pr_evidence(tmp_path)))
    assert decide_group(github) == {"pull_request": str(PR_NUMBER), "run_id": str(PR_RUN_ID), "run_attempt": "1",
                                    "tested_sha": TESTED_SHA, "validated_sha": GROUP_SHA, "producer_url": PR_URL}
    # The latest attempt's jobs, both commits' trees, and a re-read of the run after validation decided it.
    assert f"actions/runs/{PR_RUN_ID}/attempts/1/jobs?per_page=100&page=1" in github.calls
    assert {f"git/commits/{TESTED_SHA}", f"git/commits/{GROUP_SHA}"} <= set(github.calls)
    assert sum(call.startswith("actions/workflows/ci.yml/runs?") for call in github.calls) == 2


def damaged(tmp_path, change=None, drop=()):
    return archive(pr_evidence(tmp_path), change=change, drop=drop)


def skipped_verify():
    return [{"name": name, "status": "completed", "conclusion": "skipped" if name == "checks / verify" else "success"}
            for name in PR_JOBS]


@pytest.mark.parametrize("case, reason", [
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp))), {"event_name": "push"}), "only a merge queue run"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp))), {"repository": "fork/deedkit"}), "only a merge queue run"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp))), {"event": {"merge_group": {**GROUP_EVENT["merge_group"],
                                                                                    "head_sha": "b" * 40}}}), "not a merge queue group"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp))), {"event": {"merge_group": {
        **GROUP_EVENT["merge_group"], "head_ref": "refs/heads/gh-readonly-queue/release/pr-7-" + "c" * 40}}}), "not a merge queue group"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), pull={"state": "closed"}), {}), "not an open pull request"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), pull={"base": {"ref": "release"}}), {}), "not an open pull request"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), pull={"head": {"sha": HEAD_SHA, "ref": PR_BRANCH,
                                                                       "repo": {"full_name": "fork/deedkit"}}}), {}), "not an open pull request"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), runs=[]), {}), "not a completed success"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), runs=[pr_run(), pr_run(run_id=PR_RUN_ID + 1, conclusion="failure")]), {}),
     "not a completed success"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), runs=[pr_run(status="in_progress", conclusion=None)]), {}), "not a completed success"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), runs=[pr_run(head_branch="other/branch")]), {}), "not a completed success"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), runs=[pr_run(head_repository={"full_name": "fork/deedkit"})]), {}),
     "not a completed success"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), jobs=skipped_verify()), {}), "did not pass: checks / verify"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), jobs=[{"name": "CI gate", "status": "completed", "conclusion": "success"}]), {}),
     "did not pass"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), artifacts=[]), {}), "0 current evidence artifacts"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), artifacts=[artifact(artifact_id=88, workflow_run={
        "id": PR_RUN_ID, "head_sha": HEAD_SHA}, expired=True)]), {}), "0 current evidence artifacts"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), artifacts=[artifact(artifact_id=88, data=b"other", workflow_run={
        "id": PR_RUN_ID, "head_sha": HEAD_SHA})]), {}), "differs from the pull request run's artifact"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), artifacts=[artifact(artifact_id=88, workflow_run={
        "id": QUEUE_ID, "head_sha": HEAD_SHA})]), {}), "does not belong to the pull request run"),
    (lambda tmp: (PullGitHub(damaged(tmp, drop=("python.xml",))), {}), "exactly the queue run's evidence files"),
    (lambda tmp: (PullGitHub(damaged(tmp, change={"source-sha": b"not-a-sha\n"})), {}), "no exact tested commit"),
    (lambda tmp: (PullGitHub(archive(queue_evidence(tmp / "queue", attempt="1", sha=TESTED_SHA, producer_url=PR_URL))), {}),
     "not this pull request run's evidence"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp, git_branch="other/branch"))), {}), "not this pull request run's evidence"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp, producer_url=PRODUCER_URL))), {}), "not assembled by the pinned"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), commits={TESTED_SHA: {
        "sha": TESTED_SHA, "tree": {"sha": TREE}, "parents": [{"sha": BASE_SHA}, {"sha": "b" * 40}]}}), {}), "not GitHub's merge"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), commits={TESTED_SHA: {
        "sha": TESTED_SHA, "tree": {"sha": TREE}, "parents": [{"sha": HEAD_SHA}]}}), {}), "not GitHub's merge"),
    (lambda tmp: (PullGitHub(archive(pr_evidence(tmp)), commits={GROUP_SHA: {
        "sha": GROUP_SHA, "tree": {"sha": "0" * 40}, "parents": [{"sha": BASE_SHA}]}}), {}), "tree differs"),
], ids=["push-event", "other-repository", "other-group-commit", "other-queue", "closed", "not-into-main", "fork", "no-run",
        "newer-failed-run", "running", "other-branch-run", "fork-run", "docs-only-run", "required-job-missing", "no-artifact",
        "expired", "digest-mismatch", "other-run-artifact", "incomplete-archive", "no-tested-sha", "queue-evidence",
        "other-branch-evidence", "other-producer", "merge-of-another-head", "not-a-merge", "other-tree"])
def test_without_a_passed_full_pull_request_run_of_the_same_tree_the_queue_run_verifies_in_full(tmp_path, case, reason):
    github, change = case(tmp_path)
    with pytest.raises(ci_reuse.Refused, match=reason):
        decide_group(github, **change)


def test_a_pull_request_rerun_during_validation_leaves_nothing_to_reuse(tmp_path):
    github = PullGitHub(archive(pr_evidence(tmp_path)))
    # A rerun starts while the trees are compared, after the evidence of the earlier attempt validated.
    github.after[f"git/commits/{GROUP_SHA}"] = lambda state: state.runs.__setitem__(
        0, pr_run(attempt=2, status="in_progress", conclusion=None))
    with pytest.raises(ci_reuse.Refused, match="changed while it was being validated"):
        decide_group(github)


def test_a_group_decision_error_at_any_point_means_full_verification_and_a_green_step(tmp_path, monkeypatch, capsys):
    output, event = tmp_path / "output", tmp_path / "event.json"
    event.write_text(json.dumps(GROUP_EVENT))
    for name, value in {"GITHUB_OUTPUT": str(output), "GITHUB_SHA": GROUP_SHA, "GITHUB_REPOSITORY": REPOSITORY,
                        "GITHUB_EVENT_NAME": "merge_group", "GITHUB_EVENT_PATH": str(event)}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["ci_reuse.py", "group"])
    data = archive(pr_evidence(tmp_path))
    for failing in ("pulls/", "actions/workflows/", f"actions/runs/{PR_RUN_ID}/artifacts", "git/commits/"):
        github = PullGitHub(data)

        def unavailable(state, failing=failing):
            raise OSError(f"GitHub API unavailable at {failing}")
        github.after[failing] = unavailable
        monkeypatch.setattr(ci_reuse.decide_group, "__defaults__", (github, lambda path: data))
        output.write_text("")
        assert ci_reuse.main() == 0
        assert output.read_text() == "decision=full\n"
        assert "Full verification" in capsys.readouterr().out
    # A download that fails, or an unreadable event, ends the same way.
    monkeypatch.setattr(ci_reuse.decide_group, "__defaults__", (PullGitHub(data), lambda path: (_ for _ in ()).throw(OSError("down"))))
    output.write_text("")
    assert ci_reuse.main() == 0 and output.read_text() == "decision=full\n"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "missing.json"))
    output.write_text("")
    assert ci_reuse.main() == 0 and output.read_text() == "decision=full\n"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setattr(ci_reuse.decide_group, "__defaults__", (PullGitHub(data), lambda path: data))
    output.write_text("")
    assert ci_reuse.main() == 0
    assert output.read_text().splitlines() == ["decision=reuse", f"pull_request={PR_NUMBER}", f"run_id={PR_RUN_ID}",
                                               "run_attempt=1", f"tested_sha={TESTED_SHA}", f"validated_sha={GROUP_SHA}",
                                               f"producer_url={PR_URL}"]


def test_only_a_merge_queue_run_decides_on_a_pull_request_run_and_its_reuse_publishes_nothing(tmp_path):
    ci, checks = workflows()
    job = ci["jobs"]["pr_reuse"]
    assert job["name"] == "Reuse the pull request verification" and job["if"] == "github.event_name == 'merge_group'"
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    assert "secrets." not in json.dumps(job) and "TIDEN" not in json.dumps(job)
    decide = next(step for step in job["steps"] if step.get("id") == "decide")
    assert decide["continue-on-error"] is True and decide["run"] == "python3 scripts/ci_reuse.py group"
    assert decide["timeout-minutes"] < job["timeout-minutes"] and all("${{" not in step.get("run", "") for step in job["steps"])
    # No evidence is uploaded: nothing of the pull request run becomes this run's ci-evidence.
    assert not any(step.get("uses", "").startswith("actions/upload-artifact") for step in job["steps"])
    assert job["outputs"] == {name: f"${{{{ steps.decide.outcome == 'success' && steps.decide.outputs.{name} || '{default}' }}}}"
                              for name, default in (("decision", "full"), ("validated_sha", ""), ("producer_url", ""),
                                                    ("tested_sha", ""))}
    assert ci["jobs"]["checks"]["needs"] == ["reuse", "pr_reuse"]
    assert ci["jobs"]["checks"]["with"]["reuse_pr_verification"] == "${{ needs.pr_reuse.outputs.decision == 'reuse' }}"
    assert ci["jobs"]["checks"]["with"]["reused_pr_run"] == "${{ needs.pr_reuse.outputs.producer_url }}"
    assert ci["jobs"]["checks"]["with"]["reused_pr_tested_sha"] == "${{ needs.pr_reuse.outputs.tested_sha }}"
    assert checks.get("on", checks.get(True))["workflow_call"]["inputs"]["reuse_pr_verification"]["default"] is False
    for kind in ("pypi", "npm"):
        release = yaml.safe_load((ROOT / f".github/workflows/publish-{kind}.yml").read_text())
        assert "reuse_pr_verification" not in json.dumps(release)
    # The tiden job of a reusing queue run writes one skipped receipt and runs no Tiden command.
    steps = {step.get("id") or step.get("name") or step["uses"].split("@")[0]: step for step in checks["jobs"]["tiden"]["steps"]}
    config = steps["config"]
    assert config["env"]["PR_REUSED"] == "${{ inputs.reuse_pr_verification }}"
    record = steps["Record that a merge queue run reusing the pull request verification publishes nothing"]
    assert record["if"] == "steps.config.outputs.pr_reused == 'true'" and "tiden_ci.py" not in record["run"]
    retain = steps["Retain Tiden association and verdict receipt"]
    assert "steps.config.outputs.pr_reused == 'true'" in retain["if"] and retain["with"]["overwrite"] is True
    assert record["env"]["TESTED_IN"] == "${{ inputs.reused_pr_run }}" and record["env"]["GROUP_SHA"] == "${{ github.sha }}"

    def receipt(directory, **env):
        directory.mkdir()
        values = {"REASON": record["env"]["REASON"], "TESTED_IN": PR_URL, "TESTED_SHA": TESTED_SHA, "GROUP_SHA": GROUP_SHA,
                  "REPOSITORY": REPOSITORY, **env}
        completed = subprocess.run(["bash", "-e", "-c", record["run"]], capture_output=True, text=True,
                                   env={"PATH": "/usr/bin:/bin", "RUNNER_TEMP": str(directory), **values})
        route = directory / "ci-evidence" / "tiden-route.json"
        return completed.returncode, json.loads(route.read_text()) if route.exists() else None
    # The receipt names the exact pull request run attempt, the merge commit it tested and this group commit.
    assert receipt(tmp_path / "named") == (0, {"branch": None, "source": "skipped", "reason": record["env"]["REASON"],
                                               "tested_in": PR_URL, "tested_sha": TESTED_SHA, "group_sha": GROUP_SHA})
    # Without them the reuse is unproven and the reporting job fails, so the gate does.
    for index, change in enumerate([{"TESTED_IN": ""}, {"TESTED_IN": PR_URL.replace(REPOSITORY, "fork/deedkit")},
                                    {"TESTED_SHA": ""}, {"GROUP_SHA": "A" * 40}]):
        assert receipt(tmp_path / f"unnamed-{index}", **change) == (1, None)
    for name, step in steps.items():
        if name not in ("config", record["name"], "Retain Tiden association and verdict receipt"):
            assert "pr_reused" not in step.get("if", ""), name

    def configure(**env):
        output = tmp_path / "config-output"
        output.write_text("")
        completed = subprocess.run(["bash", "-e", "-c", config["run"]], capture_output=True, text=True,
                                   env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(output),
                                        "GITHUB_STEP_SUMMARY": str(tmp_path / "config-summary"), **env})
        return completed.returncode, output.read_text().split()
    assert configure(EVENT_NAME="merge_group", SOURCE_BRANCH=QUEUE_BRANCH, PR_REUSED="true") == (0, ["enabled=false", "pr_reused=true"])
    assert configure(EVENT_NAME="merge_group", SOURCE_BRANCH=QUEUE_BRANCH, PR_REUSED="false") == (0, ["enabled=false", "queue=true"])
    # Only a queue run takes it: the input on any other event changes nothing.
    assert configure(EVENT_NAME="pull_request", SOURCE_BRANCH="codex/feature", PR_REUSED="true",
                     TIDEN_API_TOKEN="t", TIDEN_PRODUCT_ID="p", TIDEN_WORKSPACE_ID="w") == (0, ["enabled=true"])


PR_REUSED = {**GREEN, "VERIFIED_SHA": "", "EVENT_NAME": "merge_group", "REF": "refs/heads/" + QUEUE_BRANCH,
             "PR_DECISION": "reuse", "PR_REUSE_RESULT": "success", "PR_REUSED_SHA": SHA, "DOCS_ONLY": ""}


@pytest.mark.parametrize("environment, passes", [
    (PR_REUSED, True),
    ({**PR_REUSED, "PR_REUSE_RESULT": "failure"}, False),
    ({**PR_REUSED, "PR_REUSED_SHA": ""}, False),
    ({**PR_REUSED, "PR_REUSED_SHA": "b" * 40}, False),
    ({**PR_REUSED, "VERIFIED_SHA": SHA}, False),
    ({**PR_REUSED, "EVENT_NAME": "pull_request"}, False),
    ({**PR_REUSED, "EVENT_NAME": "push", "REF": "refs/heads/main"}, False),
    ({**PR_REUSED, "PR_DECISION": "full"}, False),
    ({**PR_REUSED, "PR_DECISION": "full", "VERIFIED_SHA": SHA}, True),
], ids=["reused", "decision-job-failed", "not-validated", "validated-other-commit", "verify-also-ran", "pull-request-event",
        "push-event", "verify-skipped-without-reuse", "full-queue-run"])
def test_ci_gate_passes_a_queue_run_with_skipped_verify_only_for_its_validated_pull_request_reuse(environment, passes):
    ci, _ = workflows()
    step = ci["jobs"]["gate"]["steps"][1]
    assert set(step["env"]) == set(PR_REUSED)
    completed = subprocess.run(["bash", "-e", "-c", step["run"]], env={"PATH": "/usr/bin:/bin", **environment},
                               capture_output=True)
    assert (completed.returncode == 0) == passes


# spec: ANYM-322
@pytest.mark.parametrize("native, decision, passes", [
    ("success", "", True), ("success", "full", True), ("success", "reuse", True), ("skipped", "reuse", True),
    ("skipped", "", False), ("skipped", "full", False), ("failure", "reuse", False), ("cancelled", "reuse", False),
], ids=["ran", "ran-full-queue", "ran-on-reuse", "skipped-on-reuse", "skipped-without-decision", "skipped-on-full-queue",
        "failed-on-reuse", "cancelled-on-reuse"])
def test_ci_gate_accepts_a_skipped_native_job_only_with_the_queue_pull_request_reuse(native, decision, passes):
    ci, _ = workflows()
    job = ci["jobs"]["native"]
    assert job["needs"] == ["pr_reuse"] and job["uses"] == "./.github/workflows/whatsmeow.yml"
    # Any doubt about the decision (a failed or skipped pr_reuse leaves no `reuse`) runs the native suite.
    assert job["if"] == "${{ !cancelled() && needs.pr_reuse.outputs.decision != 'reuse' }}"
    first, second = ci["jobs"]["gate"]["steps"][:2]
    assert first["env"]["PR_DECISION"] == second["env"]["PR_DECISION"] == "${{ needs.pr_reuse.outputs.decision }}"
    environment = {"CHECKS_RESULT": "success", "NATIVE_RESULT": native, "IMAGES_RESULT": "success", "PR_DECISION": decision,
                   "SOURCE_REF": "", "EXPECTED_SHA": SHA}
    completed = subprocess.run(["bash", "-e", "-c", first["run"]], env={"PATH": "/usr/bin:/bin", **environment},
                               capture_output=True)
    assert (completed.returncode == 0) == passes
    # The second step still refuses a `reuse` decision that is not a validated pull request reuse of this group commit.
    refused = subprocess.run(["bash", "-e", "-c", second["run"]], env={"PATH": "/usr/bin:/bin", **PR_REUSED,
                                                                        "PR_REUSE_RESULT": "failure"}, capture_output=True)
    assert refused.returncode != 0


def test_the_push_after_a_reusing_queue_run_verifies_in_full_and_promotion_takes_its_own_verify():
    # The reusing queue run's checks / verify was skipped, so the push finds no queue verification to reuse.
    skipped = [{"name": "checks / verify", "status": "completed", "conclusion": "skipped"},
               {"name": "CI gate", "status": "completed", "conclusion": "success"}]
    with pytest.raises(ci_reuse.Refused, match="did not pass: checks / verify"):
        ci_reuse.decide(SHA, REPOSITORY, get=GitHub(jobs=skipped), sleep=lambda seconds: None)
    # Promotion then accepts only the push run's own passed checks / verify, never the skipped queue job.
    with pytest.raises(production.Refused, match="did not pass: checks / verify"):
        production.queue_verification(SHA, GitHub(jobs=skipped))
    push = {"id": PUSH_ID, "run_attempt": 1, "head_sha": SHA, "head_branch": "main", "event": "push", "status": "completed",
            "conclusion": "success", "head_repository": {"full_name": REPOSITORY},
            "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{PUSH_ID}"}
    jobs = [{"name": name, "status": "completed", "conclusion": "success"} for name in production.REQUIRED_JOBS]

    def github(path):
        if path == "compare/main..." + SHA:
            return {"status": "identical", "ahead_by": 0, "merge_base_commit": {"sha": SHA}}
        if path.startswith("actions/workflows/ci.yml/runs?"):
            assert "event=push" in path  # a push run whose own verify passed never consults the queue
            return {"total_count": 1, "workflow_runs": [push]}
        if path.startswith(f"actions/runs/{PUSH_ID}/attempts/1/jobs?"):
            return {"total_count": len(jobs), "jobs": jobs}
        raise AssertionError(path)
    assert production.verify(SHA, get=github) == {
        "sha": SHA, "run_id": PUSH_ID, "run_attempt": 1, "url": push["html_url"], "verify_run_id": PUSH_ID,
        "verify_run_attempt": 1, "verify_run_url": push["html_url"]}


# The PR→queue chain: the queue run reused pull request run PR_RUN_ID, so the main push reuses that run's evidence.
def named_steps(*names, conclusion="success"):
    return [{"name": name, "status": "completed", "conclusion": conclusion} for name in names]


def chain_queue_jobs(*steps):
    """The queue run's jobs; its reuse job names the pull request run its decision reused in a step (ci.yml)."""
    steps = steps or ("Set up job", f"Reused pull request run {PR_RUN_ID} attempt 1", "Complete job")
    return [{"name": "checks / verify", "status": "completed", "conclusion": "skipped"},
            {"name": "Reuse the pull request verification", "status": "completed", "conclusion": "success",
             "steps": [step if isinstance(step, dict) else named_steps(step)[0] for step in steps]},
            {"name": "CI gate", "status": "completed", "conclusion": "success"}]


CHAIN_QUEUE_JOBS = chain_queue_jobs()


class ChainGitHub:
    """A synthetic GitHub API for a main push whose queue run reused a pull request run: GitHub for the queue run,
    PullGitHub for the merged pull request, its run, evidence artifact and commits."""

    def __init__(self, data, queue_jobs=None, pull=None, **state):
        self.queue = GitHub(jobs=queue_jobs or CHAIN_QUEUE_JOBS, artifacts=[])
        self.pr = PullGitHub(data, pull={"state": "closed", "merged": True, "merge_commit_sha": SHA, **(pull or {})}, **state)
        self.calls, self.after = [], {}

    def __call__(self, path):
        self.calls.append(path)
        for prefix, action in list(self.after.items()):
            if path.startswith(prefix):
                action(self)
        if "event=merge_group" in path or path.startswith(f"actions/runs/{QUEUE_ID}/"):
            return self.queue(path)
        if path.startswith("actions/artifacts/"):
            found = next((value for value in self.pr.artifacts if value["id"] == int(path.split("/")[2])), None)
            if found is None:
                raise production.Refused("HTTP 404")
            return found
        return self.pr(path)


def chain_pin(data, **change):
    return {"run_id": str(PR_RUN_ID), "run_attempt": "1", "artifact_id": "88", "artifact_digest": digest(data),
            "pull_request": str(PR_NUMBER), **change}


def adopt_chain(tmp_path, github, data, pinned=None, tree=TREE, out="published"):
    return ci_reuse.adopt(pinned or chain_pin(data), SHA, REPOSITORY, str(PUSH_ID), "1", tmp_path / out, get=github,
                          fetch=lambda path: data) if tree is None else ci_reuse.adopt_chain(
        pinned or chain_pin(data), SHA, REPOSITORY, str(PUSH_ID), "1", tmp_path / out, get=github,
        fetch=lambda path: data, local_tree=lambda sha: tree)


# spec: ANYM-322
def test_a_push_after_a_queue_run_that_reused_a_pull_request_run_pins_that_run_its_artifact_and_pull_request(tmp_path):
    data = archive(pr_evidence(tmp_path))
    github = ChainGitHub(data)
    assert ci_reuse.decide(SHA, REPOSITORY, get=github, sleep=lambda seconds: None) == chain_pin(data)
    assert f"pulls/{PR_NUMBER}" in github.calls
    assert f"actions/runs/{PR_RUN_ID}/attempts/1/jobs?per_page=100&page=1" in github.calls


# spec: ANYM-322
def test_the_push_publishes_the_pull_request_evidence_only_for_exactly_its_own_git_tree(tmp_path, monkeypatch):
    evidence = pr_evidence(tmp_path)
    data = archive(evidence)
    github = ChainGitHub(data)
    trees = []
    monkeypatch.setattr(ci_reuse, "checkout_tree", lambda sha: trees.append(sha) or TREE)
    record = adopt_chain(tmp_path, github, data, tree=None)
    # The local checkout of the push commit and both commits' API trees are the one tree the pull request run tested.
    assert trees == [SHA] and {f"git/commits/{TESTED_SHA}", f"git/commits/{SHA}"} <= set(github.calls)
    assert record["producer"] == {"repository": REPOSITORY, "workflow": ci_reuse.WORKFLOW, "run_id": str(PR_RUN_ID),
                                  "run_attempt": "1", "url": PR_URL, "artifact_id": "88",
                                  "artifact_digest": digest(data), "sha": TESTED_SHA}
    assert record["chain"] == {"pull_request": str(PR_NUMBER), "head_branch": PR_BRANCH, "queue_run_id": str(QUEUE_ID),
                               "queue_run_attempt": "2", "tree": TREE}
    published = tmp_path / "published"
    assert all((published / name).read_bytes() == (evidence / name).read_bytes() for name in ci_reuse.EVIDENCE_FILES)
    assert json.loads((published / tiden_ci.PUBLICATION).read_text()) == record


def queue_rerun(state):
    state.queue.runs[0] = queue_run(attempt=3)


# spec: ANYM-322
@pytest.mark.parametrize("build, reason", [
    (lambda data: (ChainGitHub(data), {"tree": "0" * 40}), "not exactly this commit's tree"),
    (lambda data: (ChainGitHub(data), {"tree": ""}), "not exactly this commit's tree"),
    (lambda data: (ChainGitHub(data, commits={SHA: {"sha": SHA, "tree": {"sha": "0" * 40}, "parents": [{"sha": BASE_SHA}]}}), {}),
     "not exactly this commit's tree"),
    (lambda data: (ChainGitHub(data, commits={TESTED_SHA: {"sha": TESTED_SHA, "tree": {"sha": TREE},
                                                           "parents": [{"sha": BASE_SHA}, {"sha": "b" * 40}]}}), {}),
     "not GitHub's merge"),
    (lambda data: (ChainGitHub(data, queue_jobs=[*CHAIN_QUEUE_JOBS, {"name": "checks / documentation", "status": "completed",
                                                                   "conclusion": "success"}]), {}), "did not pass by reusing"),
    (lambda data: (ChainGitHub(data, queue_jobs=[job for job in CHAIN_QUEUE_JOBS if job["name"] != "Reuse the pull request verification"]), {}),
     "did not pass by reusing"),
    (lambda data: (ChainGitHub(data, pull={"merged": False}), {}), "did not merge into main as this commit"),
    (lambda data: (ChainGitHub(data, pull={"merge_commit_sha": "b" * 40}), {}), "did not merge into main as this commit"),
    (lambda data: (ChainGitHub(data, jobs=skipped_verify()), {}), "pull request job did not pass: checks / verify"),
    (lambda data: (ChainGitHub(data, runs=[pr_run(), pr_run(run_id=PR_RUN_ID + 1, conclusion="failure")]), {}),
     "not a completed success"),
    (lambda data: (ChainGitHub(data), {"pinned": chain_pin(data, run_attempt="2")}), "not the pinned one"),
    (lambda data: (ChainGitHub(data), {"pinned": chain_pin(data, pull_request="8")}), "not the pinned one"),
    (lambda data: (ChainGitHub(data), {"pinned": chain_pin(b"other")}), "pinned evidence artifact is gone"),
], ids=["local-tree", "unreadable-local-tree", "push-tree", "merge-of-another-head", "docs-only-queue", "no-pull-request-reuse",
        "not-merged", "other-merge-commit", "pull-request-job-failed", "newer-pull-request-run", "other-attempt",
        "other-pull-request", "other-artifact"])
def test_the_push_refuses_pull_request_evidence_of_any_other_tree_run_or_queue_decision(tmp_path, build, reason):
    data = archive(pr_evidence(tmp_path))
    github, change = build(data)
    with pytest.raises(ci_reuse.Refused, match=reason):
        adopt_chain(tmp_path, github, data, **change)
    assert not (tmp_path / "published" / tiden_ci.PUBLICATION).exists()


# spec: ANYM-322
def test_a_queue_or_pull_request_rerun_during_chain_validation_fails_the_push_run(tmp_path):
    data = archive(pr_evidence(tmp_path))
    github = ChainGitHub(data)
    github.after[f"git/commits/{SHA}"] = queue_rerun
    with pytest.raises(ci_reuse.Refused, match="newer merge queue run or rerun"):
        adopt_chain(tmp_path, github, data)
    github = ChainGitHub(data)
    github.after[f"git/commits/{SHA}"] = lambda state: state.pr.runs.__setitem__(0, pr_run(attempt=2))
    with pytest.raises(ci_reuse.Refused, match="not the pinned one"):
        adopt_chain(tmp_path, github, data, out="again")


# spec: ANYM-322
def test_without_a_pull_request_reuse_job_the_skipped_queue_verify_still_means_full_verification(tmp_path):
    data = archive(pr_evidence(tmp_path))
    jobs = [job for job in CHAIN_QUEUE_JOBS if job["name"] != "Reuse the pull request verification"]
    with pytest.raises(ci_reuse.Refused, match="did not pass: checks / verify; the merge queue run did not pass by reusing"):
        ci_reuse.decide(SHA, REPOSITORY, get=ChainGitHub(data, queue_jobs=jobs), sleep=lambda seconds: None)


def chain_push(github, jobs=None):
    """The main push run that reused the chain: every required job passed except the skipped checks / verify."""
    push = {"id": PUSH_ID, "run_attempt": 1, "head_sha": SHA, "head_branch": "main", "event": "push", "status": "completed",
            "conclusion": "success", "head_repository": {"full_name": REPOSITORY},
            "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{PUSH_ID}"}
    jobs = jobs or [{"name": name, "status": "completed", "conclusion": "skipped" if name == "checks / verify" else "success"}
                    for name in production.REQUIRED_JOBS]
    github.queue.runs[0]["html_url"] = f"https://github.com/{REPOSITORY}/actions/runs/{QUEUE_ID}"
    for run in github.pr.runs:
        run["html_url"] = f"https://github.com/{REPOSITORY}/actions/runs/{run['id']}"

    def get(path):
        if path == "compare/main..." + SHA:
            return {"status": "identical", "ahead_by": 0, "merge_base_commit": {"sha": SHA}}
        if path.startswith("actions/workflows/ci.yml/runs?") and "event=push" in path:
            return {"total_count": 1, "workflow_runs": [push]}
        if path.startswith(f"actions/runs/{PUSH_ID}/attempts/1/jobs?"):
            return {"total_count": len(jobs), "jobs": jobs}
        return github(path)
    return get


# spec: ANYM-322
def test_promotion_accepts_a_skipped_push_verify_through_the_chain_to_the_passed_pull_request_run(tmp_path):
    github = ChainGitHub(b"")
    assert production.verify(SHA, get=chain_push(github)) == {
        "sha": SHA, "run_id": PUSH_ID, "run_attempt": 1, "url": f"https://github.com/{REPOSITORY}/actions/runs/{PUSH_ID}",
        "verify_run_id": PR_RUN_ID, "verify_run_attempt": 1,
        "verify_run_url": f"https://github.com/{REPOSITORY}/actions/runs/{PR_RUN_ID}"}
    for broken, reason in ((ChainGitHub(b"", pull={"merge_commit_sha": "b" * 40}), "did not merge into main as this commit"),
                           (ChainGitHub(b"", jobs=skipped_verify()), "pull request job did not pass"),
                           (ChainGitHub(b"", queue_jobs=[*CHAIN_QUEUE_JOBS, {"name": "checks / documentation",
                                                                             "status": "completed", "conclusion": "success"}]),
                            "did not pass by reusing")):
        with pytest.raises(production.Refused, match=reason):
            production.verify(SHA, get=chain_push(broken))


def chain_published(tmp_path, monkeypatch, tree=TREE):
    data = archive(pr_evidence(tmp_path))
    adopt_chain(tmp_path, ChainGitHub(data), data)
    monkeypatch.setattr(tiden_ci, "checkout_tree", lambda sha: tree if sha == SHA else "")
    monkeypatch.setattr(tiden_ci, "commit_tree", lambda repository, sha: TREE if (repository, sha) == (REPOSITORY, TESTED_SHA) else "")
    return tmp_path / "published"


# spec: ANYM-322
def test_the_push_run_publishes_the_chained_pull_request_evidence_to_main_as_its_own_commit(tmp_path, monkeypatch):
    published = chain_published(tmp_path, monkeypatch)
    code, calls = tiden_main(monkeypatch, published, {}, "publish")
    assert code == 0
    create = next(call for call in calls if call[1:3] == ["run", "create"])
    assert create[create.index("--branch") + 1] == "main" and create[create.index("--build-sha") + 1] == SHA
    meta = [create[i + 1] for i, value in enumerate(create) if value == "--client-meta"]
    assert meta == ["git_branch=main", "ci_url=" + PUSH_URL, "tested_in=" + PR_URL]
    manifest = tiden_ci.published_manifest(published, json.loads((published / "manifest.json").read_text()))
    assert (manifest["sha"], manifest["tested_sha"], manifest["event"], manifest["tiden_branch"], manifest["pr_tiden_links"]) \
        == (SHA, TESTED_SHA, "push", "", [])
    rows = json.loads((published / "ci-checks-published.json").read_text())
    assert all(row["message"].endswith("tested in " + PR_URL) for row in rows)


# spec: ANYM-322
def test_chained_pull_request_evidence_publishes_nothing_unless_the_record_and_checkout_tree_match(tmp_path, monkeypatch):
    published = chain_published(tmp_path, monkeypatch)
    record = json.loads((published / "publication.json").read_text())
    manifest = json.loads((published / "manifest.json").read_text())
    for tree in ("0" * 40, ""):
        monkeypatch.setattr(tiden_ci, "checkout_tree", lambda sha, tree=tree: tree)
        with pytest.raises(ValueError, match="publication record"):
            tiden_main(monkeypatch, published, {}, "publish")
    monkeypatch.setattr(tiden_ci, "checkout_tree", lambda sha: TREE)
    chain = record["chain"]
    for change in ({"chain": {**chain, "tree": "0" * 40}}, {"chain": {**chain, "tree": TREE.upper()}},
                   {"chain": {key: value for key, value in chain.items() if key != "tree"}},
                   {"chain": {**chain, "head_branch": "other/branch"}}, {"chain": {**chain, "pull_request": "x"}},
                   {"chain": {**chain, "extra": "1"}}, {"chain": "yes"}, {"extra": 1},
                   {"producer": {**record["producer"], "sha": SHA}},
                   {"producer": {**record["producer"], "sha": "b" * 40}}):
        (published / "publication.json").write_text(json.dumps({**record, **change}))
        with pytest.raises(ValueError, match="publication record"):
            tiden_main(monkeypatch, published, {}, "publish")
    (published / "publication.json").write_text(json.dumps(record))
    # Evidence beside the record that is not the pull request run's: a queue run's, another branch's or another commit's.
    for change in ({"event": "merge_group", "git_branch": QUEUE_BRANCH}, {"git_branch": "other/branch"}, {"sha": "b" * 40},
                   {"event": "push", "git_branch": "main"}):
        (published / "manifest.json").write_text(json.dumps({**manifest, **change}))
        with pytest.raises(ValueError, match="publication record"):
            tiden_main(monkeypatch, published, {}, "publish")


# spec: ANYM-322
def test_the_reuse_job_passes_the_chained_pull_request_from_its_decision_to_its_validation(tmp_path, monkeypatch):
    ci, _ = workflows()
    steps = {step.get("id"): step for step in ci["jobs"]["reuse"]["steps"]}
    assert steps["adopt"]["env"]["QUEUE_PULL_REQUEST"] == "${{ steps.decide.outputs.pull_request }}"
    data = archive(pr_evidence(tmp_path))
    output = tmp_path / "output"
    for name, value in {"GITHUB_OUTPUT": str(output), "GITHUB_SHA": SHA, "GITHUB_REPOSITORY": REPOSITORY,
                        "GITHUB_RUN_ID": str(PUSH_ID), "GITHUB_RUN_ATTEMPT": "1"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(ci_reuse.decide, "__defaults__", (ChainGitHub(data), lambda seconds: None, ci_reuse.time.monotonic))
    monkeypatch.setattr(sys, "argv", ["ci_reuse.py", "decide"])
    output.write_text("")
    assert ci_reuse.main() == 0
    decided = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert decided == {"decision": "reuse", **chain_pin(data)}
    for key in ("run_id", "run_attempt", "artifact_id", "artifact_digest", "pull_request"):
        monkeypatch.setenv("QUEUE_" + key.upper(), decided[key])
    monkeypatch.setattr(ci_reuse.adopt, "__defaults__", (ChainGitHub(data), lambda path: data))
    monkeypatch.setattr(ci_reuse, "checkout_tree", lambda sha: TREE)
    monkeypatch.setattr(sys, "argv", ["ci_reuse.py", "adopt", "--directory", str(tmp_path / "published")])
    output.write_text("")
    assert ci_reuse.main() == 0 and output.read_text() == f"validated_sha={SHA}\n"
    assert json.loads((tmp_path / "published" / tiden_ci.PUBLICATION).read_text())["chain"]["tree"] == TREE
    # The same decision with another local tree fails the push run instead of publishing.
    monkeypatch.setattr(ci_reuse, "checkout_tree", lambda sha: "0" * 40)
    monkeypatch.setattr(sys, "argv", ["ci_reuse.py", "adopt", "--directory", str(tmp_path / "other")])
    with pytest.raises(ci_reuse.Refused, match="not exactly this commit's tree"):
        ci_reuse.main()


# spec: ANYM-322
def test_within_one_deploy_chained_verifications_fit_the_anonymous_quota_and_a_queue_rerun_is_verified_again(tmp_path):
    github = ChainGitHub(b"")
    get = chain_push(github)
    paths = []

    def counted(path):
        paths.append(path)
        return get(path)
    # Plans A and B verify 10 times and plan B 6 times; the first verification reads the queue run once, not twice.
    for steps, count, listing in (("plan A and B", 10, 1), ("plan B", 6, 0)):
        paths.clear()
        with production.verification_reuse():
            first = production.verify(SHA, counted)
            assert len(paths) == 11
            for _ in range(count - 1):
                assert production.verify(SHA, counted) == first
        # The anonymous quota a deploy reserved for them before promotion stopped reading the API (ANYM-406).
        assert len(paths) + listing <= {"plan A and B": 52, "plan B": 35}[steps] - 4, (steps, len(paths))
    # A later verification re-reads the queue run that reused the pull request run, and a rerun of it is read in full.
    with production.verification_reuse():
        production.verify(SHA, counted)
        github.queue.runs[0] = {**github.queue.runs[0], "run_attempt": 3}
        paths.clear()
        assert production.verify(SHA, counted)["verify_run_id"] == PR_RUN_ID
        assert f"actions/runs/{QUEUE_ID}/attempts/3/jobs?per_page=100&page=1" in paths


def git(root, *arguments):
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(root),
                               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}).stdout.strip()


# spec: ANYM-322
def test_the_checkout_tree_ignores_git_replacements_and_inherited_git_settings(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-q")
    (repository / "file").write_text("pushed")
    git(repository, "add", "file")
    git(repository, "commit", "-q", "-m", "pushed")
    pushed, pushed_tree = git(repository, "rev-parse", "HEAD"), git(repository, "rev-parse", "HEAD^{tree}")
    (repository / "file").write_text("other")
    git(repository, "commit", "-q", "-am", "other")
    other, other_tree = git(repository, "rev-parse", "HEAD"), git(repository, "rev-parse", "HEAD^{tree}")
    git(repository, "checkout", "-q", pushed)
    # A replacement object makes plain git show the other commit's tree for the pushed commit.
    git(repository, "replace", pushed, other)
    assert git(repository, "rev-parse", "HEAD^{tree}") == other_tree
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("GIT_DIR", str(elsewhere))
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", "refs/replace/")
    assert tiden_ci.checkout_tree(pushed, repository) == ci_reuse.checkout_tree(pushed, repository) == pushed_tree
    assert tiden_ci.checkout_tree(other, repository) == "" and tiden_ci.checkout_tree(pushed, elsewhere) == ""
    with pytest.raises(ci_reuse.Refused, match="not this commit"):
        ci_reuse.checkout_tree(other, repository)


# spec: ANYM-322
def test_chained_evidence_publishes_nothing_when_github_records_another_tree_for_the_tested_commit(tmp_path, monkeypatch):
    published = chain_published(tmp_path, monkeypatch)
    # A record and manifest that agree with each other and with the checkout, for a tested commit of another tree.
    for recorded in ("0" * 40, ""):
        monkeypatch.setattr(tiden_ci, "commit_tree", lambda repository, sha, recorded=recorded: recorded)
        with pytest.raises(ValueError, match="publication record"):
            tiden_main(monkeypatch, published, {}, "publish")
    asked = []
    monkeypatch.setattr(tiden_ci, "commit_tree", lambda repository, sha: asked.append((repository, sha)) or TREE)
    assert tiden_main(monkeypatch, published, {}, "publish")[0] == 0 and asked == [(REPOSITORY, TESTED_SHA)]


# spec: ANYM-322
def test_github_commit_tree_reads_only_an_exact_commit_anonymously_and_fails_closed(monkeypatch):
    requests = []

    def answer(body):
        def urlopen(request, timeout):
            requests.append((request.full_url, dict(request.header_items()), timeout))
            if isinstance(body, Exception):
                raise body
            return Response(json.dumps(body).encode())
        return urlopen
    monkeypatch.setattr(tiden_ci.urllib.request, "urlopen", answer({"sha": TESTED_SHA, "tree": {"sha": TREE}}))
    assert tiden_ci.commit_tree(REPOSITORY, TESTED_SHA) == TREE
    url, headers, _ = requests[0]
    assert url == f"https://api.github.com/repos/{REPOSITORY}/git/commits/{TESTED_SHA}"
    assert not any(name.lower() == "authorization" for name in headers)
    for body in ({"sha": "b" * 40, "tree": {"sha": TREE}}, {"sha": TESTED_SHA, "tree": {"sha": "short"}}, [],
                 OSError("down")):
        monkeypatch.setattr(tiden_ci.urllib.request, "urlopen", answer(body))
        assert tiden_ci.commit_tree(REPOSITORY, TESTED_SHA) == ""
    requests.clear()
    assert tiden_ci.commit_tree("fork/deedkit/../x", TESTED_SHA) == tiden_ci.commit_tree(REPOSITORY, "HEAD") == ""
    assert requests == []


# spec: ANYM-322
def test_promotion_refuses_a_pull_request_run_newer_than_the_queue_decision(tmp_path):
    # Two successful runs of the same head: the queue decided on 640; 641, created after the queue attempt started,
    # may test another merge base and tree, so promotion and the push reuse refuse it rather than name it.
    newer = pr_run(run_id=PR_RUN_ID + 1, created_at="2026-10-17T08:30:00Z")
    with pytest.raises(production.Refused, match="not the one the merge queue decision evaluated"):
        production.verify(SHA, get=chain_push(ChainGitHub(b"", runs=[pr_run(), newer])))
    with pytest.raises(ci_reuse.Refused, match="not the one the merge queue decision evaluated"):
        ci_reuse.decide(SHA, REPOSITORY, get=ChainGitHub(b"", runs=[pr_run(), newer]), sleep=lambda seconds: None)
    for broken in ({"created_at": None}, {"created_at": "2026-10-17T08:00:00"}):
        with pytest.raises(production.Refused, match="not the one the merge queue decision evaluated"):
            production.verify(SHA, get=chain_push(ChainGitHub(b"", runs=[pr_run(**broken)])))


# spec: ANYM-322
def test_promotion_follows_only_the_pull_request_run_the_queue_named_and_refuses_it_when_gone(tmp_path):
    # The queue decided on 640 and named it; once 640 is deleted, the older successful 639 of the same head (which may
    # have tested another tree) is refused by promotion and by the push decision, never substituted.
    older = [pr_run(run_id=PR_RUN_ID - 1)]
    with pytest.raises(production.Refused, match="not the one the merge queue decision evaluated"):
        production.verify(SHA, get=chain_push(ChainGitHub(b"", runs=older)))
    with pytest.raises(ci_reuse.Refused, match="not the one the merge queue decision evaluated"):
        ci_reuse.decide(SHA, REPOSITORY, get=ChainGitHub(b"", runs=older), sleep=lambda seconds: None)
    named = f"Reused pull request run {PR_RUN_ID} attempt 1"
    for steps, reason in ((("Reused pull request run 639 attempt 1",), "not the one"),
                          ((f"Reused pull request run {PR_RUN_ID} attempt 2",), "not the one"),
                          (("Set up job", "Complete job"), "does not name the one"),
                          (("Reused pull request run  attempt ",), "does not name the one"),
                          ((named_steps(named, conclusion="skipped")[0],), "does not name the one"),
                          ((f"{named} ",), "does not name the one"),
                          ((named, "Reused pull request run 639 attempt 1"), "does not name the one")):
        with pytest.raises(production.Refused, match=reason):
            production.verify(SHA, get=chain_push(ChainGitHub(b"", queue_jobs=chain_queue_jobs(*steps))))
    # A later attempt of the named run tests the same merge commit, so its result counts.
    verified = production.verify(SHA, get=chain_push(ChainGitHub(b"", runs=[pr_run(attempt=2)])))
    assert (verified["verify_run_id"], verified["verify_run_attempt"]) == (PR_RUN_ID, 2)


# spec: ANYM-322
def test_within_one_deploy_a_failed_or_pending_pull_request_rerun_is_never_taken_from_the_cache(tmp_path):
    for change, reason in (({"run_attempt": 2, "conclusion": "failure"}, "not a completed success"),
                           ({"run_attempt": 2, "status": "in_progress", "conclusion": None}, "not a completed success")):
        github = ChainGitHub(b"")
        get = chain_push(github)
        with production.verification_reuse():
            production.verify(SHA, get)
            github.pr.runs[0] = {**github.pr.runs[0], **change}  # between preparation and application
            with pytest.raises(production.Refused, match=reason):
                production.verify(SHA, get)


def go_rows(evidence, check, change):
    """The prepared Go report of `check` with `change` applied to its list of result rows."""
    return json.dumps(change(json.loads((evidence / tiden_ci.normalized_report(check)).read_text()))).encode()


# spec: ANYM-421
def test_queue_and_chained_pull_request_evidence_with_go_results_is_accepted(tmp_path):
    evidence = queue_evidence(tmp_path / "queue")
    data = archive(evidence)
    manifest = ci_reuse.validate(evidence, SHA, PRODUCER_URL, 2)
    for check in tiden_ci.GO_MODULES:
        rows = json.loads((evidence / tiden_ci.normalized_report(check)).read_text())
        assert tiden_ci.normalized_report(check) in manifest["reports"] and rows
        assert {row["execution"]["status"] for row in rows} == {"passed"} and rows[0]["signature"] == "::".join(go_test(check))
    adopt(tmp_path / "queue", GitHub(artifacts=[artifact(data=data)]), data)
    published = tmp_path / "queue" / "published"
    assert all((published / tiden_ci.normalized_report(check)).read_bytes() == (evidence / tiden_ci.normalized_report(check)).read_bytes()
               for check in tiden_ci.GO_MODULES)
    chained = pr_evidence(tmp_path / "chain")
    data = archive(chained)
    adopt_chain(tmp_path / "chain", ChainGitHub(data), data)
    assert all((tmp_path / "chain" / "published" / tiden_ci.normalized_report(check)).is_file() for check in tiden_ci.GO_MODULES)


def without_check_rows(evidence, *names):
    return json.dumps([row for row in json.loads((evidence / "ci-checks.json").read_text())
                       if row["signature"].split("::")[1] not in names]).encode()


# spec: ANYM-421
@pytest.mark.parametrize("damage, reason", [
    (lambda e: {"normalized-whatsmeow.json": go_rows(e, "whatsmeow", lambda rows: [{**rows[0], "output": "x"}])},
     "whatsmeow report is unsafe or malformed"),
    (lambda e: {"normalized-whatsmeow.json": go_rows(e, "whatsmeow", lambda rows: [{**rows[0], "signature": "TestX"}])},
     "whatsmeow report is unsafe or malformed"),
    (lambda e: {"normalized-release_router.json": b"[]"}, "release_router report is unsafe or malformed"),
    (lambda e: {"normalized-release_router.json": b"{"}, "release_router report is unsafe or malformed"),
    (lambda e: {"normalized-whatsmeow.json": go_rows(e, "whatsmeow", lambda rows: [
        {**row, "execution": {"status": "failed", "duration": 0}} for row in rows])}, "whatsmeow report has no passing"),
    (lambda e: {"normalized-whatsmeow.json": go_rows(e, "whatsmeow", lambda rows: [
        *rows, {**rows[0], "id": "00000000-0000-5000-8000-000000000002", "signature": rows[0]["signature"] + "/failed",
                "title": rows[0]["title"] + "/failed", "execution": {"status": "failed", "duration": 0}}])},
     "whatsmeow report has no passing executions or a failure"),
    (lambda e: {"normalized-release_router.json": go_rows(e, "release_router", lambda rows: [
        {**row, "execution": {"status": "skipped", "duration": 0}} for row in rows])}, "release_router report has no passing"),
    (lambda e: {"normalized-whatsmeow.json": go_rows(e, "whatsmeow", lambda rows: [*rows, {
        **rows[0], "id": "00000000-0000-5000-8000-000000000001", "signature": rows[0]["signature"] + "/extra",
        "title": rows[0]["title"] + "/extra"}])}, "test counts differ"),
    (lambda e: {"manifest.json": rewrite(e, "manifest.json", reports=[
        name for name in json.loads((e / "manifest.json").read_text())["reports"] if name != "normalized-whatsmeow.json"])},
     "does not list every required report"),
    (lambda e: {"ci-checks.json": without_check_rows(e, "whatsmeow", "release_router")}, "check rows"),
    (lambda e: {"ci-checks.json": without_check_rows(e, "release_router")}, "check rows"),
    (lambda e: {"ci-checks.json": json.dumps(json.loads((e / "ci-checks.json").read_text()) * 2).encode()}, "check rows"),
    (lambda e: {"ci-checks.json": json.dumps([{**row, "execution": {"status": "blocked", "duration": 0}}
                                              if row["signature"] == "deedkit-ci::whatsmeow" else row
                                              for row in json.loads((e / "ci-checks.json").read_text())]).encode()}, "check rows"),
    (lambda e: {"normalized-whatsmeow.json": go_rows(e, "whatsmeow", lambda rows: [{**rows[0], "id": []}])},
     "whatsmeow report is unsafe or malformed"),
    (lambda e: {"normalized-whatsmeow.json": go_rows(e, "whatsmeow", lambda rows: [{**rows[0], "suitePath": None}])},
     "whatsmeow report is unsafe or malformed"),
    (lambda e: {"normalized-whatsmeow.json": go_rows(e, "whatsmeow", lambda rows: [
        {**rows[0], "execution": {"status": "passed", "duration": -1}}])}, "whatsmeow report is unsafe or malformed"),
    (lambda e: {"normalized-whatsmeow.json": go_rows(e, "whatsmeow", lambda rows: [
        {**rows[0], "execution": {**rows[0]["execution"], "output": "x"}}])}, "whatsmeow report is unsafe or malformed"),
    (lambda e: {"normalized-whatsmeow.json": go_rows(e, "whatsmeow", lambda rows: [
        {**rows[0], "fields": {**rows[0]["fields"], "output": "x"}}])}, "whatsmeow report is unsafe or malformed"),
    (lambda e: {"normalized-whatsmeow.json": go_rows(e, "release_router", lambda rows: rows)},
     "whatsmeow report is unsafe or malformed"),
    (lambda e: {"normalized-whatsmeow.json": go_rows(e, "whatsmeow", lambda rows: [*rows, rows[0]])},
     "whatsmeow report is unsafe or malformed"),
], ids=["extra-row-field", "malformed-signature", "empty-report", "not-json", "failed-rows", "one-failed-row", "only-skipped",
        "counts-differ", "unlisted-report", "no-go-check-rows", "no-release-router-row", "duplicate-check-rows",
        "blocked-go-check-row", "row-id-not-a-uuid", "suite-path-null", "negative-duration", "nested-execution-field",
        "nested-fields-field", "other-module-rows", "duplicate-row"])
def test_queue_evidence_with_unusable_go_results_is_refused(tmp_path, damage, reason):
    evidence = queue_evidence(tmp_path)
    data = archive(evidence, change=damage(evidence))
    with pytest.raises(ci_reuse.Refused, match=reason):
        adopt(tmp_path, GitHub(artifacts=[artifact(data=data)]), data)
    assert not (tmp_path / "published" / "publication.json").exists()


# spec: ANYM-421
@pytest.mark.parametrize("report", ["normalized-whatsmeow.json", "normalized-release_router.json"])
def test_queue_evidence_without_a_go_report_is_refused(tmp_path, report):
    evidence = queue_evidence(tmp_path)
    data = archive(evidence, drop=(report,))
    with pytest.raises(ci_reuse.Refused, match="exactly the queue run's evidence files"):
        adopt(tmp_path, GitHub(artifacts=[artifact(data=data)]), data)
    (evidence / report).unlink()  # beside a complete archive check, validation itself never reads a missing report as passed
    with pytest.raises(ci_reuse.Refused, match="report is unsafe or malformed"):
        ci_reuse.validate(evidence, SHA, PRODUCER_URL, 2)


def published_go_ids(directory, url):
    """The IDs of the published Go rows, beside the IDs a run with `url` gives them."""
    found, expected = {}, {}
    for check in tiden_ci.GO_MODULES:
        rows = json.loads((directory / ("published-" + tiden_ci.normalized_report(check))).read_text())
        found[check] = [row["id"] for row in rows]
        expected[check] = [tiden_ci.row_id(url, check, row["signature"]) for row in rows]
    return found, expected


# spec: ANYM-421
def test_published_go_rows_carry_this_push_run_attempts_ids(tmp_path, monkeypatch):
    evidence, published = published_evidence(tmp_path)
    code, calls = tiden_main(monkeypatch, published, {}, "publish")
    assert code == 0
    report = next(call for call in calls if call[1:3] == ["run", "report"])
    sent = {Path(path).name for path in report[4:report.index("--product-id")]}
    assert {"published-" + tiden_ci.normalized_report(check) for check in tiden_ci.GO_MODULES} <= sent
    assert not {tiden_ci.normalized_report(check) for check in tiden_ci.GO_MODULES} & sent
    found, expected = published_go_ids(published, PUSH_URL)
    assert found == expected
    queue_ids = {row["id"] for check in tiden_ci.GO_MODULES
                 for row in json.loads((evidence / tiden_ci.normalized_report(check)).read_text())}
    assert queue_ids and not queue_ids & {value for ids in found.values() for value in ids}
    # Everything but the ID is the queue run's row, and the queue run's own report stays unchanged.
    for check in tiden_ci.GO_MODULES:
        name = tiden_ci.normalized_report(check)
        original = json.loads((evidence / name).read_text())
        assert [{**row, "id": None} for row in json.loads((published / ("published-" + name)).read_text())] == \
            [{**row, "id": None} for row in original]
        assert (published / name).read_bytes() == (evidence / name).read_bytes()
    # A rerun attempt of the push run adopts the evidence again and publishes the rows under new IDs.
    data = archive(evidence)
    ci_reuse.adopt(pin(data), SHA, REPOSITORY, str(PUSH_ID), "2", tmp_path / "rerun", get=GitHub(artifacts=[artifact(data=data)]),
                   fetch=lambda path: data)
    assert tiden_main(monkeypatch, tmp_path / "rerun", {"GITHUB_RUN_ATTEMPT": "2"}, "publish")[0] == 0
    rerun, expected = published_go_ids(tmp_path / "rerun", PUSH_URL.replace("/attempts/1", "/attempts/2"))
    assert rerun == expected and not set(rerun["whatsmeow"]) & set(found["whatsmeow"])


# spec: ANYM-421
def test_chained_pull_request_go_rows_are_published_under_the_push_runs_ids(tmp_path, monkeypatch):
    published = chain_published(tmp_path, monkeypatch)
    assert tiden_main(monkeypatch, published, {}, "publish")[0] == 0
    found, expected = published_go_ids(published, PUSH_URL)
    assert found == expected
    pull_request_ids = {row["id"] for check in tiden_ci.GO_MODULES
                        for row in json.loads((published / tiden_ci.normalized_report(check)).read_text())}
    assert pull_request_ids == {tiden_ci.row_id(PR_URL, check, row["signature"]) for check in tiden_ci.GO_MODULES
                                for row in json.loads((published / tiden_ci.normalized_report(check)).read_text())}
    assert not pull_request_ids & {value for ids in found.values() for value in ids}
