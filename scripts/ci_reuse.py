#!/usr/bin/env python3
"""Reuse the merge queue's verification of a main commit in its push run instead of testing it again (ANYM-322).

`decide` never fails the run: without a passed queue run of the exact commit and exactly one current evidence
artifact of its latest attempt, or on any error, the push run verifies in full. `adopt` runs only after the
choice: it downloads that pinned artifact as data, validates it and adds this run's publication record, and
any problem fails the push run. Nothing from the artifact is ever executed.

When the queue run passed by reusing a full pull request run of exactly this tree (`group`, ANYM-358), `decide`
pins that pull request run's evidence instead (the PR→queue chain), and `adopt` validates it by the same rules
and also requires the tree the pull request run tested to be exactly this commit's git tree.

`group` decides a merge queue run the same never-failing way: it reuses the verification of a passing, full pull
request run only when the group commit's tree is exactly the tree that run tested (owner decision 2026-10-18),
and otherwise the queue run verifies in full.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import added_tests  # noqa: E402  the exclusion inventory a reused run must equal (ANYM-578)
import production  # noqa: E402  the queue rules promotion applies (ANYM-321)
import tiden_ci  # noqa: E402

ARTIFACT = "ci-evidence"
WORKFLOW = ".github/workflows/ci.yml"
# The queue run usually completes seconds before main moves; wait that long, then verify in full.
QUEUE_WAIT_SECONDS, QUEUE_POLL_SECONDS = 300, 10
MAX_ARCHIVE_BYTES, MAX_ENTRIES, MAX_TOTAL_BYTES = 64 * 1024**2, 64, 128 * 1024**2
REPORT_CHECKS = (*tiden_ci.REPORTS, *tiden_ci.MODULE_REPORTS)
# Exactly what the queue run's evidence job uploads: the assembled fragments and what `prepare` adds to them.
EVIDENCE_FILES = frozenset({
    "source-sha", "metadata.json", "manifest.json", "ci-checks.json", *tiden_ci.ASSEMBLED_FILES,
    *(tiden_ci.normalized_report(check) for check in REPORT_CHECKS)})
SHA = re.compile(r"[0-9a-f]{40}")
# The merge queue names a group of main after the pull request it tests last: pr-<number>-<group base SHA>.
GROUP_REF = re.compile("refs/heads/" + re.escape(production.QUEUE_BRANCH_PREFIX) + r"pr-([1-9][0-9]*)-[0-9a-f]{40}")
# A full pull request run: a docs-only run skips `checks / verify` and never qualifies.
PULL_REQUEST_JOBS = production.PULL_REQUEST_JOBS


class Refused(RuntimeError):
    pass


class QueueSkipped(Refused):
    """The merge queue run passed with `checks / verify` skipped (production.QueueSkipped)."""


def when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def run_url(repository: str, run_id: object, attempt: object) -> str:
    return f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{attempt}"


def queue_run(sha: str, get, sleep=time.sleep, wait: int = QUEUE_WAIT_SECONDS, clock=time.monotonic) -> dict:
    """The merge queue run that verified `sha` by promotion's rules, after waiting for it to complete.

    The wait is measured on the clock, so slow API responses cannot stretch it; the decision step's own timeout,
    below the job's, also ends a hung lookup as a full verification.
    """
    deadline = clock() + wait
    try:
        while True:
            latest = production.latest_run(sha, "merge_group", get)
            if latest is None:
                raise Refused("no merge queue run tested this commit")
            if latest.get("status") == "completed" or clock() + QUEUE_POLL_SECONDS > deadline:
                break
            sleep(QUEUE_POLL_SECONDS)
        return production.queue_verification(sha, get)
    except production.QueueSkipped as exc:
        raise QueueSkipped(str(exc)) from None
    except production.Refused as exc:
        raise Refused(str(exc)) from None


def chain(sha: str, get) -> tuple[dict, dict, int, str]:
    """The queue run that reused a pull request run for `sha`, that run, its pull request and head branch."""
    try:
        return production.queue_chain(sha, get)
    except production.Refused as exc:
        raise Refused(str(exc)) from None


def decide(sha: str, repository: str, get=production.api, sleep=time.sleep, clock=time.monotonic) -> dict:
    """Pin the queue run, its latest attempt and that attempt's one evidence artifact, or, when the queue run reused
    a pull request run, that run, attempt and artifact and the pull request; Refused means verify in full."""
    if repository != production.REPOSITORY or not SHA.fullmatch(sha):
        raise Refused("only an exact commit of this repository's main reuses its queue verification")
    try:
        queue = queue_run(sha, get, sleep, clock=clock)
    except QueueSkipped as refused:
        try:
            _, run, number, _ = chain(sha, get)
        except Refused as exc:
            raise Refused(f"{refused}; {exc}") from None
        artifact = evidence_artifact(run, run["head_sha"], get, "pull request run")
        return {"run_id": str(run["id"]), "run_attempt": str(run["run_attempt"]), "artifact_id": str(artifact["id"]),
                "artifact_digest": artifact["digest"], "pull_request": str(number)}
    attempt = queue["run_attempt"]
    artifact = evidence_artifact(queue, sha, get, "queue run")
    return {"run_id": str(queue["id"]), "run_attempt": str(attempt), "artifact_id": str(artifact["id"]),
            "artifact_digest": artifact["digest"]}


def required_inventory(pin: dict, fetch=None, exclusions: dict = added_tests.EMPTY) -> None:
    """Before the choice: the pinned evidence must have excluded exactly what a main push requires, nothing (ANYM-578).
    Evidence of a run that excluded added tests is not this push's verification, so the push verifies in full
    instead of failing at adoption; adoption validates the same artifact again."""
    data = (fetch or download)(f"actions/artifacts/{pin['artifact_id']}/zip")
    if "sha256:" + hashlib.sha256(data).hexdigest() != pin["artifact_digest"]:
        raise Refused("the downloaded evidence differs from the pinned artifact")
    with tempfile.TemporaryDirectory() as scratch:
        directory = Path(scratch) / "evidence"
        extract(data, directory)
        if read_json(directory, added_tests.EVIDENCE) != exclusions:
            raise Refused("the evidence excluded other added tests than this event requires")


def evidence_artifact(run: dict, head_sha: str, get, kind: str) -> dict:
    """The one current `ci-evidence` artifact of the run's latest attempt, created by that run for `head_sha`."""
    attempt = run["run_attempt"]
    started = get(f"actions/runs/{run['id']}/attempts/{attempt}")
    if started.get("id") != run["id"] or started.get("run_attempt") != attempt or not started.get("run_started_at"):
        raise Refused(f"the {kind}'s latest attempt could not be read")
    artifacts = production.complete_listing(lambda page: get(f"actions/runs/{run['id']}/artifacts?" + urllib.parse.urlencode(
        {"name": ARTIFACT, "per_page": 100, "page": page})), "artifacts")
    # A rerun can leave an earlier attempt's evidence behind; only the one created during the pinned attempt counts.
    current = [artifact for artifact in artifacts if artifact.get("name") == ARTIFACT and artifact.get("expired") is False
               and when(artifact["created_at"]) >= when(started["run_started_at"])]
    if len(current) != 1:
        raise Refused(f"the {kind}'s latest attempt has {len(current)} current evidence artifacts, not exactly one")
    artifact = current[0]
    workflow_run = artifact.get("workflow_run") or {}
    if (workflow_run.get("id"), workflow_run.get("head_sha")) != (run["id"], head_sha) or not str(artifact.get("digest", "")).startswith("sha256:"):
        raise Refused(f"the evidence artifact does not belong to the {kind} or has no digest")
    return artifact


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def bounded(response) -> bytes:
    data = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise Refused("the evidence archive exceeds its size limit")
    return data


def download(path: str) -> bytes:
    """An artifact archive; the job token goes only to the GitHub API, never to the storage it redirects to."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "deedkit-ci-reuse"}
    if token := os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request("https://api.github.com/repos/" + production.REPOSITORY + "/" + path, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
            return bounded(response)
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location", "") if exc.code in {301, 302, 303, 307, 308} else ""
        if not location.startswith("https://"):
            raise
    with urllib.request.urlopen(urllib.request.Request(location, headers={"User-Agent": "deedkit-ci-reuse"}),
                                timeout=120) as response:
        return bounded(response)


def extract(data: bytes, directory: Path) -> None:
    """Write the archive's flat evidence files into a new or empty data directory, within size limits."""
    if directory.exists() and any(directory.iterdir()):
        raise Refused("the evidence directory must be new or empty")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
    except (zipfile.BadZipFile, ValueError):
        raise Refused("the evidence archive is not a valid zip file") from None
    names = [entry.filename for entry in entries]
    if len(entries) > MAX_ENTRIES or len(set(names)) != len(names) or set(names) != EVIDENCE_FILES:
        raise Refused("the evidence archive does not hold exactly the queue run's evidence files")
    if sum(entry.file_size for entry in entries) > MAX_TOTAL_BYTES:
        raise Refused("the evidence archive expands beyond its size limit")
    directory.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        mode = entry.external_attr >> 16
        if entry.is_dir() or stat.S_IFMT(mode) not in {0, stat.S_IFREG} or entry.file_size > tiden_ci.MAX_BYTES:
            raise Refused(f"the evidence file {entry.filename} is not a bounded regular file")
        with archive.open(entry) as source:
            content = source.read(tiden_ci.MAX_BYTES + 1)
        if len(content) != entry.file_size:
            raise Refused(f"the evidence file {entry.filename} does not match its recorded size")
        (directory / entry.filename).write_bytes(content)


def read_json(directory: Path, name: str):
    try:
        return json.loads((directory / name).read_text())
    except (OSError, ValueError):
        raise Refused(f"the evidence file {name} is not valid JSON") from None


def validate(directory: Path, sha: str, producer_url: str, attempt: int, pull_request_branch: str = "",
             exclusions: dict = added_tests.EMPTY) -> dict:
    """Refuse evidence that is not the queue run's complete, passing verification of exactly `sha`.

    With `pull_request_branch`, the producer is instead a pull request run of that head branch, whose `sha` is the
    merge commit it tested; every other rule is the same. The producer's exclusion inventory (ANYM-578), its
    excluded files and their comparisons, must equal `exclusions`, the one the receiving event requires: none for a
    main push, the group's for a merge queue group. Equal trees with other lists are not equal verification.
    """
    metadata, manifest, load = (read_json(directory, name) for name in ("metadata.json", "manifest.json", "load.json"))
    if not all(isinstance(value, dict) for value in (metadata, manifest, load)):
        raise Refused("the evidence metadata, manifest or load report is malformed")
    if (directory / "source-sha").read_text().strip() != sha or metadata.get("sha") != sha or manifest.get("sha") != sha \
            or load.get("sha") != sha:
        raise Refused("an evidence SHA field names another commit")
    if metadata.get("ci_url") != producer_url or any(manifest.get(key) != value for key, value in metadata.items()):
        raise Refused("the evidence was not assembled by the pinned queue run attempt")
    if pull_request_branch:
        if (manifest.get("schema"), manifest.get("event"), manifest.get("git_branch")) != (1, "pull_request", pull_request_branch) \
                or not isinstance(manifest.get("tiden_branch"), str):
            raise Refused("the evidence is not this pull request run's evidence")
    elif (manifest.get("schema"), manifest.get("event"), manifest.get("tiden_branch")) != (1, "merge_group", "") \
            or not str(manifest.get("git_branch", "")).startswith(production.QUEUE_BRANCH_PREFIX):
        raise Refused("the evidence is not a merge queue run's evidence of main")
    if manifest.get("checks") != dict.fromkeys(tiden_ci.CHECKS, "success") \
            or manifest.get("module_checks") != dict.fromkeys(tiden_ci.MODULE_REPORTS, "success") \
            or manifest.get("check_counts") != {"passed": len(tiden_ci.CHECKS) + len(tiden_ci.MODULE_REPORTS)} \
            or load.get("status") != "passed":
        raise Refused("not every check of the queue run passed")
    producers = manifest.get("producers")
    if manifest.get("fragments") != list(tiden_ci.FRAGMENTS) or not isinstance(producers, dict) \
            or set(producers) != set(tiden_ci.FRAGMENTS) or any(
                not isinstance(job, dict) or job.get("result") != "success" or job.get("sha") != sha
                or not str(job.get("attempt")).isdigit() or int(job["attempt"]) > attempt for job in producers.values()):
        raise Refused("a test job of the queue run did not produce passing evidence of this commit")
    if read_json(directory, added_tests.EVIDENCE) != exclusions:
        raise Refused("the evidence excluded other added tests than this event requires")
    added = read_json(directory, added_tests.REPORT)
    if not isinstance(added, dict) or added.get("result") not in {"passed", "none"}:
        raise Refused("the added tests of the evidence did not pass")
    reports = [tiden_ci.normalized_report(check) for check in REPORT_CHECKS]
    if sorted(manifest.get("reports") or []) != sorted([*reports, "ci-checks.json"]):
        raise Refused("the evidence manifest does not list every required report")
    totals = {}
    for check, report in zip(REPORT_CHECKS, reports):
        _, framework, _ = tiden_ci.MODULE_REPORTS.get(check, (None, check, None))
        try:
            if framework == "go":
                # The module path comes from this checkout, whose tree validation binds to the evidence's.
                counts = tiden_ci.go_row_counts(json.loads((directory / report).read_text()),
                                                tiden_ci.go_module_of(Path(__file__).resolve().parents[1], check))
            else:
                _, counts = tiden_ci.normalize_junit(directory / report, directory, framework)
        except (OSError, ValueError, SyntaxError) as exc:
            raise Refused(f"the {check} report is unsafe or malformed: {exc}") from None
        if counts["failed"] or not counts["passed"]:
            raise Refused(f"the {check} report has no passing executions or a failure")
        for status, count in counts.items():
            totals[status] = totals.get(status, 0) + count
    if manifest.get("test_counts") != totals:
        raise Refused("the evidence test counts differ from its reports")
    rows = read_json(directory, "ci-checks.json")
    expected = [(("anymsg-ci::" if check in tiden_ci.CHECKS else "deedkit-ci::") + check,
                 str(uuid.uuid5(uuid.NAMESPACE_URL, producer_url + "/" + check)))
                for check in (*tiden_ci.CHECKS, *tiden_ci.MODULE_REPORTS)]
    if not isinstance(rows, list) or [(row.get("signature"), row.get("id")) for row in rows if isinstance(row, dict)] != expected \
            or any((row.get("execution") or {}).get("status") != "passed" for row in rows):
        raise Refused(f"the evidence check rows are not the queue run's {len(expected)} passed CI and module checks")
    return manifest


def pinned_artifact(get, pin: dict, head_sha: str, kind: str):
    """A check that the pinned artifact is still the producer run's current, unexpired evidence of `head_sha`."""
    def current() -> dict:
        artifact = get(f"actions/artifacts/{pin['artifact_id']}")
        workflow_run = artifact.get("workflow_run") or {}
        if (artifact.get("name"), artifact.get("expired"), str(workflow_run.get("id")), workflow_run.get("head_sha"),
                artifact.get("digest")) != (ARTIFACT, False, pin["run_id"], head_sha, pin["artifact_digest"]) \
                or workflow_run.get("repository_id") != workflow_run.get("head_repository_id") \
                or not 0 < int(artifact.get("size_in_bytes", 0)) <= MAX_ARCHIVE_BYTES:
            raise Refused(f"the pinned evidence artifact is gone, expired, replaced or not this {kind}'s")
        return artifact
    return current


def checkout_tree(sha: str, root: Path | None = None) -> str:
    """The git tree hash of the checked-out commit, which must be exactly `sha`. Git runs without replacement objects
    and without any inherited `GIT_*` setting, so neither can redirect it or show another commit's tree."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"

    def git(*arguments: str) -> str:
        return subprocess.run(["git", "-C", str(root or Path.cwd()), *arguments], check=True, capture_output=True,
                              text=True, timeout=60, env=environment).stdout.strip()
    if git("rev-parse", "--verify", "HEAD") != sha:
        raise Refused("the checkout is not this commit")
    return git("rev-parse", "--verify", "HEAD^{tree}")


def same_tree(sha: str, tested: str, head_sha: str, get, local_tree=None) -> str:
    """The git tree hash `tested` and `sha` share; `tested` must be GitHub's merge of the pull request head."""
    commit, pushed = get(f"git/commits/{tested}"), get(f"git/commits/{sha}")
    if commit.get("sha") != tested or [parent.get("sha") for parent in commit.get("parents") or []][1:] != [head_sha]:
        raise Refused("the tested commit is not GitHub's merge of the pull request head")
    tree = str((commit.get("tree") or {}).get("sha", ""))
    if not SHA.fullmatch(tree) or pushed.get("sha") != sha or (pushed.get("tree") or {}).get("sha") != tree \
            or (local_tree or checkout_tree)(sha) != tree:
        raise Refused("the tree the pull request run tested is not exactly this commit's tree")
    return tree


def adopt_chain(pin: dict, sha: str, repository: str, run_id: str, run_attempt: str, directory: Path,
                get=production.api, fetch=download, local_tree=None) -> dict:
    """Validate the pinned evidence of the pull request run the merge queue reused for `sha` (the PR→queue chain)
    into `directory`, require its tested tree to be exactly this commit's, and record this push run as publisher."""
    def current_chain() -> tuple[dict, dict, str]:
        queue, run, number, head_ref = chain(sha, get)
        if (str(run["id"]), str(run["run_attempt"]), str(number)) != (pin["run_id"], pin["run_attempt"], pin["pull_request"]):
            raise Refused("the pull request run the merge queue reused is not the pinned one")
        return queue, run, head_ref

    queue, run, head_ref = current_chain()
    attempt, head_sha = int(pin["run_attempt"]), run["head_sha"]
    producer_url = run_url(repository, pin["run_id"], attempt)
    current = pinned_artifact(get, pin, head_sha, "pull request run")
    current()
    data = fetch(f"actions/artifacts/{pin['artifact_id']}/zip")
    if "sha256:" + hashlib.sha256(data).hexdigest() != pin["artifact_digest"]:
        raise Refused("the downloaded evidence differs from the pinned artifact")
    extract(data, directory)
    tested = (directory / "source-sha").read_text().strip()
    if not SHA.fullmatch(tested):
        raise Refused("the pull request run's evidence names no exact tested commit")
    validate(directory, tested, producer_url, attempt, pull_request_branch=head_ref)
    tree = same_tree(sha, tested, head_sha, get, local_tree)
    # A newer queue or pull request run, a rerun or a deleted artifact during validation leaves nothing to reuse.
    current()
    again, _, _ = current_chain()
    if (again["id"], again["run_attempt"]) != (queue["id"], queue["run_attempt"]):
        raise Refused("a newer merge queue run or rerun of this commit appeared during validation")
    record = {"schema": 1, "event": "push", "git_branch": "main", "repository": repository, "run_id": run_id,
              "run_attempt": run_attempt, "sha": sha, "ci_url": run_url(repository, run_id, run_attempt),
              "producer": {"repository": repository, "workflow": WORKFLOW, "run_id": pin["run_id"],
                           "run_attempt": str(attempt), "url": producer_url, "artifact_id": pin["artifact_id"],
                           "artifact_digest": pin["artifact_digest"], "sha": tested},
              "chain": {"pull_request": pin["pull_request"], "head_branch": head_ref, "queue_run_id": str(queue["id"]),
                        "queue_run_attempt": str(queue["run_attempt"]), "tree": tree}}
    (directory / tiden_ci.PUBLICATION).write_text(json.dumps(record, indent=2))
    return record


def adopt(pin: dict, sha: str, repository: str, run_id: str, run_attempt: str, directory: Path,
          get=production.api, fetch=download) -> dict:
    """Validate the pinned queue evidence of `sha` into `directory` and record this push run as its publisher;
    with a pinned pull request, the evidence of the pull request run the queue reused (`adopt_chain`)."""
    if repository != production.REPOSITORY or not SHA.fullmatch(sha) or not (run_id.isdigit() and run_attempt.isdigit()):
        raise Refused("only this repository's main push run adopts queue evidence")
    if pin.get("pull_request"):
        if not pin["pull_request"].isdigit():
            raise Refused("the reuse decision pinned no exact pull request")
        return adopt_chain(pin, sha, repository, run_id, run_attempt, directory, get, fetch)
    queue_id, attempt, artifact_id = pin["run_id"], int(pin["run_attempt"]), pin["artifact_id"]
    producer_url = run_url(repository, queue_id, attempt)
    current = pinned_artifact(get, pin, sha, "queue run")
    current()
    data = fetch(f"actions/artifacts/{artifact_id}/zip")
    if "sha256:" + hashlib.sha256(data).hexdigest() != pin["artifact_digest"]:
        raise Refused("the downloaded evidence differs from the pinned artifact")
    extract(data, directory)
    validate(directory, sha, producer_url, attempt)
    # A newer queue run, a rerun or a deleted artifact during validation leaves nothing to reuse.
    current()
    queue = queue_run(sha, get, wait=0)
    if (str(queue["id"]), queue["run_attempt"]) != (queue_id, attempt):
        raise Refused("a newer merge queue run or rerun of this commit appeared during validation")
    record = {"schema": 1, "event": "push", "git_branch": "main", "repository": repository, "run_id": run_id,
              "run_attempt": run_attempt, "sha": sha, "ci_url": run_url(repository, run_id, run_attempt),
              "producer": {"repository": repository, "workflow": WORKFLOW, "run_id": queue_id, "run_attempt": str(attempt),
                           "url": producer_url, "artifact_id": artifact_id, "artifact_digest": pin["artifact_digest"],
                           "sha": sha}}
    (directory / tiden_ci.PUBLICATION).write_text(json.dumps(record, indent=2))
    return record


def decide_group(sha: str, repository: str, event_name: str, event: dict, get=production.api, fetch=download,
                 exclusions: dict | None = None) -> dict:
    """Pin the passing full pull request run that tested exactly this group commit's tree; Refused means verify in full.

    Tree identity is the whole claim: the queue builds the group commit from main and the pull request, so its tree
    usually equals the merge commit a pull request run tested only when main has not moved since and the group holds
    that pull request alone; whenever the trees are equal, the same code was tested. The pull request's workflow
    files are part of that tree, as they are of the group commit.
    """
    if exclusions is None:
        raise Refused("the group's exclusion inventory is required")
    if event_name != "merge_group" or repository != production.REPOSITORY or not SHA.fullmatch(sha):
        raise Refused("only a merge queue run of an exact commit of this repository reuses a pull request run")
    group = event.get("merge_group") or {}
    match = GROUP_REF.fullmatch(str(group.get("head_ref", "")))
    if group.get("head_sha") != sha or not match:
        raise Refused("the event is not a merge queue group of main for this commit")
    number = int(match.group(1))
    pull = get(f"pulls/{number}")
    head = pull.get("head") or {}
    head_sha, head_ref = str(head.get("sha", "")), str(head.get("ref", ""))
    if pull.get("number") != number or pull.get("state") != "open" or (pull.get("base") or {}).get("ref") != "main" \
            or (head.get("repo") or {}).get("full_name") != repository or not SHA.fullmatch(head_sha) or not head_ref:
        raise Refused(f"pull request {number} is not an open pull request of this repository into main")
    run = production.latest_run(head_sha, "pull_request", get)
    expected = (head_sha, "pull_request", head_ref, "completed", "success", repository)
    if run is None or (run.get("head_sha"), run.get("event"), run.get("head_branch"), run.get("status"), run.get("conclusion"),
                       (run.get("head_repository") or {}).get("full_name")) != expected:
        raise Refused("the latest pull request run of this head is not a completed success of this repository")
    jobs = production.attempt_jobs(run, get)
    for name in PULL_REQUEST_JOBS:
        if not production.job_concluded(jobs, name):
            raise Refused(f"pull request job did not pass: {name}")
    attempt = run["run_attempt"]
    artifact = evidence_artifact(run, head_sha, get, "pull request run")
    if not 0 < int(artifact.get("size_in_bytes", 0)) <= MAX_ARCHIVE_BYTES:
        raise Refused("the pull request run's evidence archive is empty or exceeds its size limit")
    data = fetch(f"actions/artifacts/{artifact['id']}/zip")
    if "sha256:" + hashlib.sha256(data).hexdigest() != artifact["digest"]:
        raise Refused("the downloaded evidence differs from the pull request run's artifact")
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary) / ARTIFACT
        extract(data, directory)
        tested = (directory / "source-sha").read_text().strip()
        if not SHA.fullmatch(tested):
            raise Refused("the pull request run's evidence names no exact tested commit")
        validate(directory, tested, run_url(repository, run["id"], attempt), int(attempt), pull_request_branch=head_ref,
                 exclusions=exclusions)
    commit, group_commit = get(f"git/commits/{tested}"), get(f"git/commits/{sha}")
    if commit.get("sha") != tested or [parent.get("sha") for parent in commit.get("parents") or []][1:] != [head_sha]:
        raise Refused("the tested commit is not GitHub's merge of this pull request head")
    tree = (commit.get("tree") or {}).get("sha")
    if group_commit.get("sha") != sha or not tree or (group_commit.get("tree") or {}).get("sha") != tree:
        raise Refused("the group commit's tree differs from the tree the pull request run tested")
    # A rerun of the pull request run during validation leaves nothing to reuse.
    if not production.unchanged(run, production.latest_run(head_sha, "pull_request", get)):
        raise Refused("the pull request run changed while it was being validated")
    return {"pull_request": str(number), "run_id": str(run["id"]), "run_attempt": str(attempt), "tested_sha": tested,
            "validated_sha": sha, "producer_url": run_url(repository, run["id"], attempt)}


def outputs(values: dict) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.writelines(f"{key}={value}\n" for key, value in values.items())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["decide", "adopt", "group"])
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    sha, repository = os.environ.get("GITHUB_SHA", ""), os.environ.get("GITHUB_REPOSITORY", "")
    if args.command == "group":
        try:
            event = json.loads(Path(os.environ.get("GITHUB_EVENT_PATH", "")).read_text())
            # The group requires the inventory ci.yml `added_list` computed for this event (ANYM-578).
            exclusions = added_tests.inventory(os.environ.get("ADDED_TESTS", ""))
            pin = decide_group(sha, repository, os.environ.get("GITHUB_EVENT_NAME", ""), event, exclusions=exclusions)
        except Exception as exc:  # any doubt means the queue run verifies in full, never a failed run
            print(f"::notice title=Full verification::{type(exc).__name__}: {exc}")
            outputs({"decision": "full"})
            return 0
        print(f"::notice title=Reusing the pull request verification::pull request {pin['pull_request']}, "
              f"{pin['producer_url']} tested {pin['tested_sha']}, the tree of {sha}")
        outputs({"decision": "reuse", **pin})
        return 0
    if args.command == "decide":
        try:
            pin = decide(sha, repository)
            required_inventory(pin)
        except Exception as exc:  # any doubt before the choice means full verification, never a failed run
            print(f"::notice title=Full verification::{type(exc).__name__}: {exc}")
            outputs({"decision": "full"})
            return 0
        print(f"::notice title=Reusing the merge queue verification::{run_url(repository, pin['run_id'], pin['run_attempt'])}")
        outputs({"decision": "reuse", **pin})
        return 0
    pin = {key: os.environ.get("QUEUE_" + key.upper(), "")
           for key in ("run_id", "run_attempt", "artifact_id", "artifact_digest", "pull_request")}
    if not (pin["run_id"].isdigit() and pin["run_attempt"].isdigit() and pin["artifact_id"].isdigit() and args.directory):
        raise Refused("the reuse decision did not pin a queue run, attempt and artifact")
    record = adopt(pin, sha, repository, os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", ""),
                   args.directory.resolve())
    print(json.dumps(record))
    outputs({"validated_sha": sha})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
