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


def queue_evidence(tmp_path, attempt="2", sha=SHA):
    """The `ci-evidence` directory a passing merge queue run uploads, built by the real assemble and prepare."""
    fragments = tmp_path / "fragments"
    verify, load = fragments / "ci-evidence-verify", fragments / "ci-evidence-load"
    for fragment in (verify, load):
        fragment.mkdir(parents=True)
        (fragment / "source-sha").write_text(sha + "\n")
        (fragment / "attempt").write_text(attempt + "\n")
    for name in tiden_ci.FRAGMENTS["verify"][1][1:]:
        (verify / name).write_text(CASE.format(name=name.split(".")[0].replace("-", "_")))
    (verify / "metadata.json").write_text(json.dumps({
        "sha": sha, "git_branch": QUEUE_BRANCH, "tiden_branch": "", "checks": dict.fromkeys(tiden_ci.CHECKS, "success"),
        "module_checks": dict.fromkeys(tiden_ci.FRAGMENTS["verify"][0], "success"), "ci_url": "unused"}))
    (load / "load.xml").write_text(CASE.format(name="load"))
    (load / "load.json").write_text(json.dumps({"version": 1, "status": "passed", "sha": sha}))
    (load / "outcomes.json").write_text(json.dumps({"module_checks": {"load": "success"}}))
    evidence = tmp_path / "queue-evidence"
    producers = {job: ("success", sha) for job in tiden_ci.FRAGMENTS}
    metadata = tiden_ci.assemble(evidence, fragments, PRODUCER_URL, producers)
    metadata.pop("missing")
    tiden_ci.prepare(ROOT, evidence, {**metadata, "pr_tiden_links": [], "event": "merge_group"})
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
            "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}", **change}


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
    (lambda e: {"ci-checks.json": json.dumps(json.loads((e / "ci-checks.json").read_text())[:12]).encode()}, "thirteen"),
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
    assert names == sorted([f"normalized-{check}.xml" for check in ci_reuse.REPORT_CHECKS] + ["ci-checks-published.json"])
    # Executions and outcomes are the queue run's; the thirteen check rows carry this push run's IDs.
    rows = json.loads((published / "ci-checks-published.json").read_text())
    queue_rows = json.loads((evidence / "ci-checks.json").read_text())
    assert [(row["signature"], row["execution"]) for row in rows] == [(row["signature"], row["execution"]) for row in queue_rows]
    checks = [row["signature"].split("::")[1] for row in rows]
    assert [row["id"] for row in rows] == [tiden_ci.uuid.uuid5(tiden_ci.uuid.NAMESPACE_URL, PUSH_URL + "/" + check).__str__()
                                           for check in checks]
    assert all(row["message"].endswith("tested in " + PRODUCER_URL) for row in rows)
    assert all((published / name).read_bytes() == (evidence / name).read_bytes() for name in ci_reuse.EVIDENCE_FILES)


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
    assert "actions" not in json.dumps({name: other.get("permissions") for name, other in ci["jobs"].items() if name != "reuse"})
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
    skip = "${{ !cancelled() && !inputs.reuse_queue_evidence && needs.documentation.outputs.docs_only != 'true' }}"
    assert checks["jobs"]["verify"]["if"] == checks["jobs"]["load"]["if"] == skip
    assert checks["jobs"]["evidence"]["if"] == "${{ always() && !inputs.reuse_queue_evidence && needs.documentation.outputs.docs_only != 'true' }}"
    tiden = checks["jobs"]["tiden"]
    assert tiden["needs"] == ["documentation", "evidence"] and tiden["if"].startswith("${{ always() && !cancelled()")
    assert "reuse_queue_evidence" not in json.dumps(tiden)
    for name in ("native", "images"):
        assert "if" not in ci["jobs"][name] and "needs" not in ci["jobs"][name]
    assert ci["jobs"]["publish"]["needs"] == ["gate"] and ci["jobs"]["release"]["needs"] == ["gate", "publish"]
    # The queue still gets no Tiden credential.
    assert ci["jobs"]["checks"]["secrets"]["TIDEN_API_TOKEN"] == (
        "${{ github.event_name != 'merge_group' && secrets.TIDEN_API_TOKEN || '' }}")


GREEN = {"DECISION": "full", "REUSE_RESULT": "skipped", "REUSED_SHA": "", "VERIFIED_SHA": SHA, "EVENT_NAME": "pull_request",
         "REF": "refs/pull/7/merge", "EXPECTED_SHA": SHA, "DOCS_ONLY": "false", "DOCS_SHA": ""}
REUSED = {"DECISION": "reuse", "REUSE_RESULT": "success", "REUSED_SHA": SHA, "VERIFIED_SHA": "", "EVENT_NAME": "push",
          "REF": "refs/heads/main", "EXPECTED_SHA": SHA, "DOCS_ONLY": "", "DOCS_SHA": ""}


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
    assert gate["needs"] == ["reuse", "checks", "native", "images"]
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
