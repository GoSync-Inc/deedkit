#!/usr/bin/env python3
"""Verified, explicit production promotion. No provider credentials or migrations."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = "GoSync-Inc/deedkit"
# `publish` pushed and attested this commit's images; preparation pulls nothing from a run without it (ANYM-301).
REQUIRED_JOBS = {"checks / verify", "native / synthetic", "images / build (backup)", "CI gate", "publish"}
# A main push run may skip only this job, and only when the merge queue verified the exact commit (ANYM-321).
QUEUE_VERIFIED_JOB = "checks / verify"
QUEUE_BRANCH_PREFIX = "gh-readonly-queue/main/"
# A merge queue run that skipped `checks / verify` because this job validated a full pull request run of exactly its
# tree (ANYM-358): promotion follows that chain to the pull request run, whose jobs below must have passed.
QUEUE_PR_REUSE_JOB = "Reuse the pull request verification"
PULL_REQUEST_JOBS = ("checks / verify", "checks / load", "checks / evidence", "CI gate")
# The one passed step of that job that names the pull request run its decision reused (ci.yml): the queue's producer.
QUEUE_PR_REUSE_STEP = re.compile(r"Reused pull request run ([1-9][0-9]*) attempt ([1-9][0-9]*)")
# The merge queue names a group of main after the pull request it tests last: pr-<number>-<group base SHA>.
QUEUE_GROUP_BRANCH = re.compile(re.escape(QUEUE_BRANCH_PREFIX) + r"pr-([1-9][0-9]*)-[0-9a-f]{40}")
MAX_PAGES = 10
MIN_FREE_BYTES = 15 * 1024**3
BUILDS = {
    "runtime": (".", "services/messaging/Dockerfile", "runtime", "import messaging_service, telegram_broker, telegram_session_owner, platform_contracts.release"),
    "indexer": (".", "services/messaging/Dockerfile", "indexer", "import messaging_service.file_index"),
    "enricher-local": (".", "services/messaging/Dockerfile", "enricher-local", "import messaging_service.enrichment"),
    "whatsapp-native": (".", "services/messaging/Dockerfile", "whatsapp-native", "import messaging_service.whatsapp_collector"),
    "gateway": (".", "services/gateway/Dockerfile", "", "import agent_gateway, platform_contracts.release"),
    "calendar": ("services/calendar", "services/calendar/Dockerfile", "", "import family_calendar"),
    "calls": ("services/calls", "services/calls/Dockerfile", "", "import fastapi, pipecat"),
    "backup": (".", "services/messaging/Dockerfile", "backup",
               "import messaging_service.backup_restore_worker; import subprocess; "
               "[subprocess.run([tool, '--version'], check=True) for tool in ('age', 'pg_dump', 'pg_restore')]"),
    "http-router": (".", "services/release-router/Dockerfile", "", ""),
}
# The images the main push run publishes (scripts/publish_images.py TARGETS): preparation pulls these by verified
# digest instead of building them; `http-router` and `check-image` still build locally (ANYM-301 B1).
PUBLISHED = ("runtime", "indexer", "enricher-local", "whatsapp-native", "gateway", "calendar", "calls", "backup")
REGISTRY = "ghcr.io/gosync-inc/deedkit-"
# The tokenless attestation verifier, built reproducibly from tools/attest-verify and installed by the owner;
# tests keep this hash equal to tools/attest-verify/attest-verify.linux-amd64.sha256.
VERIFIER = Path("/usr/local/libexec/deedkit/attest-verify")
VERIFIER_SHA256 = "af405433c424fe20580cc86e2d96221670b0a75d6af9068ab06d309ec49344db"
IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# The pinned verifier's refusal lines (tools/attest-verify main.go, registry.go). Only a registry 404 for the image
# tag proves it is not published yet. "No bundle attached" is also printed when the referrers API failed, and a bundle
# that could not be fetched is nested in "no attached bundle verified ...", so both count as GHCR not read: retried,
# never a reason to take an older commit. Any other refusal is evidence that does not verify.
VERIFIER_ABSENT = re.compile(r"attest-verify: refused: manifest \S+ not found(; |$)")
VERIFIER_UNREACHED = re.compile(r"attest-verify: refused: no Sigstore bundle is attached to |registry (request|read) "
                                r"failed|: HTTP \d+")
SOURCE = "https://github.com/" + REPOSITORY + ".git"


class Refused(RuntimeError):
    pass


class QueueSkipped(Refused):
    """The merge queue run passed with `checks / verify` skipped: a pull request reuse or a docs-only decision.

    It carries the queue run and its latest attempt's jobs as read, so `queue_chain` does not read them again."""

    def __init__(self, message, queue, jobs):
        super().__init__(message)
        self.queue, self.jobs = queue, jobs


class Busy(Refused):
    """Another production operation holds the host lock."""


class RateLimited(Refused):
    """GitHub's API quota ran out: nothing is wrong with the candidate, and `deploy` retries after the reset."""


class Unreachable(Refused):
    """git could not read main from GitHub, or the verifier could not reach GHCR: nothing is known about the candidate.
    `deploy` retries it, and the third run in a row that ends this way pauses."""


class Unattested(Refused):
    """The registry has no image tag for the target at the commit: not published (yet)."""


def run(argv, *, cwd=None, umask=-1, env=None, reason=False):
    # Never include command output in an exception: Compose can contain secrets. Only a tool that prints
    # nothing secret (the attestation verifier, git on the public source mirror) may pass its last stderr line on,
    # with `reason`.
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=1800, umask=umask, env=env)
    if result.returncode:
        detail = (result.stderr.strip().splitlines() or [""])[-1][:300] if reason else ""
        raise Refused(f"{Path(argv[0]).name} failed (exit {result.returncode}){': ' + detail if detail else ''}; "
                      "production was not accepted")
    return result.stdout


def api(path):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "deedkit-production"}
    if token := os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request("https://api.github.com/repos/" + REPOSITORY + "/" + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read(4 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429) and exc.headers.get("x-ratelimit-remaining") == "0":
            reset = str(exc.headers.get("x-ratelimit-reset", ""))
            when = datetime.fromtimestamp(int(reset), timezone.utc).strftime("%H:%M:%SZ") if reset.isdigit() else "unknown"
            raise RateLimited(f"GitHub rate limit exhausted, resets at {when}") from None
        raise
    if len(payload) > 4 * 1024 * 1024:
        raise Refused("GitHub response exceeded the safety limit")
    return json.loads(payload)


def complete_listing(fetch, key):
    """Every item of a paginated GitHub listing, or Refused when the pages cannot prove they are complete.

    A page that reports `total_count` must agree with every other page, and the distinct items collected
    must reach exactly that count. A page without a count is complete only when it is shorter than a page.
    """
    items, total = {}, None
    for page in range(1, MAX_PAGES + 1):
        listing = fetch(page)
        chunk = listing[key]
        count = listing.get("total_count")
        if count is not None:
            if not isinstance(count, int) or isinstance(count, bool) or count < 0 or total not in (None, count):
                raise Refused("the CI listing reported an invalid or changing total")
            total = count
        for item in chunk:
            items[item.get("id", len(items))] = item
        if total is not None:
            if len(items) > total:
                raise Refused("the CI listing returned more items than its total")
            if len(items) == total:
                return list(items.values())
            if not chunk:
                break
        elif len(chunk) < 100:
            return list(items.values())
    raise Refused("the CI listing could not be read completely")


def latest_run(sha, event, get):
    """The newest ci.yml run of `event` for `sha`, from a complete listing, or None."""
    query = {"event": event, "head_sha": sha, "per_page": 100}
    if event == "push":
        query["branch"] = "main"
    runs = complete_listing(lambda page: get("actions/workflows/ci.yml/runs?" + urllib.parse.urlencode(
        {**query, "page": page})), "workflow_runs")
    # A later failed/pending rerun must supersede a previous successful run.
    return max(runs, key=lambda value: value["id"]) if runs else None


def attempt_jobs(run, get):
    return complete_listing(lambda page: get(
        f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100&page={page}"), "jobs")


def job_concluded(jobs, name, conclusion="success"):
    matches = [job for job in jobs if job["name"] == name]
    return len(matches) == 1 and matches[0].get("status") == "completed" and matches[0].get("conclusion") == conclusion


def unchanged(run, again):
    """Whether a re-read shows the same run, attempt and result, so no rerun started between the reads."""
    return again is not None and all(again.get(key) == run.get(key)
                                     for key in ("id", "run_attempt", "status", "conclusion"))


def queue_verification(sha, get):
    """The merge queue run whose `checks / verify` and `CI gate` verified exactly this commit."""
    queue = latest_run(sha, "merge_group", get)
    if queue is None:
        raise Refused("the main push run skipped checks / verify and no merge queue run verified this commit")
    if (queue.get("head_sha") != sha or queue.get("event") != "merge_group"
            or not str(queue.get("head_branch", "")).startswith(QUEUE_BRANCH_PREFIX)
            or queue.get("status") != "completed" or queue.get("conclusion") != "success"
            or queue.get("head_repository", {}).get("full_name") != REPOSITORY):
        raise Refused("latest merge queue run is not a completed success of this repository's main queue for this commit")
    jobs = attempt_jobs(queue, get)
    if job_concluded(jobs, QUEUE_VERIFIED_JOB, "skipped") and job_concluded(jobs, "CI gate"):
        raise QueueSkipped(f"merge queue job did not pass: {QUEUE_VERIFIED_JOB}", queue, jobs)
    for required in (QUEUE_VERIFIED_JOB, "CI gate"):
        if not job_concluded(jobs, required):
            raise Refused(f"merge queue job did not pass: {required}")
    if not unchanged(queue, latest_run(sha, "merge_group", get)):
        raise Refused("the merge queue run changed while it was being verified")
    return queue


def timestamp(value):
    """An ISO 8601 GitHub timestamp ("...Z") as an aware datetime."""
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("a GitHub timestamp names its time zone")
    return moment


def queue_chain(sha, get, queue=None, jobs=None):
    """The merge queue run that reused a full pull request run for this commit, and that pull request run.

    Returns (queue run, pull request run, pull request number, head branch). The queue run must have passed `CI gate`
    with `checks / verify` skipped after its pull request reuse job, and not by its docs-only decision; the pull
    request must have merged as exactly this commit, and the latest run of its head must have passed every
    `PULL_REQUEST_JOBS` job. That run must be the one the reuse job's one passed `QUEUE_PR_REUSE_STEP` step names, at
    that or a later attempt (a rerun attempt tests the same merge commit), created before the queue run's latest attempt
    started: a deleted or replaced producer is refused, never substituted. The exact tree equality is the queue gate's
    (ANYM-358), checked again by the push run. `queue` and `jobs`, when given, are the latest queue run and its attempt's jobs as just read.
    """
    if queue is None:
        queue = latest_run(sha, "merge_group", get)
    branch = str((queue or {}).get("head_branch", ""))
    match = QUEUE_GROUP_BRANCH.fullmatch(branch)
    if queue is None or not match or queue.get("head_sha") != sha or queue.get("event") != "merge_group" \
            or queue.get("status") != "completed" or queue.get("conclusion") != "success" \
            or queue.get("head_repository", {}).get("full_name") != REPOSITORY:
        raise Refused("latest merge queue run is not a completed success of one pull request's group for this commit")
    jobs = attempt_jobs(queue, get) if jobs is None else jobs
    if not (job_concluded(jobs, "CI gate") and job_concluded(jobs, QUEUE_VERIFIED_JOB, "skipped")
            and job_concluded(jobs, QUEUE_PR_REUSE_JOB)) or job_concluded(jobs, "checks / documentation"):
        raise Refused("the merge queue run did not pass by reusing a pull request verification")
    reuse = next(job for job in jobs if job["name"] == QUEUE_PR_REUSE_JOB)
    named = [QUEUE_PR_REUSE_STEP.fullmatch(str(step.get("name", ""))) for step in reuse.get("steps") or []
             if step.get("status") == "completed" and step.get("conclusion") == "success"]
    named = [found for found in named if found]
    if len(named) != 1:
        raise Refused("the merge queue run's pull request reuse job does not name the one pull request run it reused")
    decided_id, decided_attempt = map(int, named[0].groups())
    number = int(match.group(1))
    pull = get(f"pulls/{number}")
    head = pull.get("head") or {}
    head_sha, head_ref = str(head.get("sha", "")), str(head.get("ref", ""))
    if pull.get("number") != number or pull.get("merged") is not True or pull.get("merge_commit_sha") != sha \
            or (pull.get("base") or {}).get("ref") != "main" or (head.get("repo") or {}).get("full_name") != REPOSITORY \
            or not re.fullmatch(r"[0-9a-f]{40}", head_sha) or not head_ref:
        raise Refused(f"pull request {number} did not merge into main as this commit from this repository")
    run = latest_run(head_sha, "pull_request", get)
    expected = (head_sha, "pull_request", head_ref, "completed", "success", REPOSITORY)
    if run is None or (run.get("head_sha"), run.get("event"), run.get("head_branch"), run.get("status"),
                       run.get("conclusion"), (run.get("head_repository") or {}).get("full_name")) != expected:
        raise Refused("the latest pull request run of the merged head is not a completed success of this repository")
    try:
        decided = run["id"] == decided_id and int(run["run_attempt"]) >= decided_attempt \
            and timestamp(run["created_at"]) <= timestamp(queue["run_started_at"])
    except (KeyError, TypeError, ValueError):
        decided = False
    if not decided:
        raise Refused("the latest pull request run of the merged head is not the one the merge queue decision evaluated")
    pull_jobs = attempt_jobs(run, get)
    for name in PULL_REQUEST_JOBS:
        if not job_concluded(pull_jobs, name):
            raise Refused(f"pull request job did not pass: {name}")
    if not (unchanged(queue, latest_run(sha, "merge_group", get)) and unchanged(run, latest_run(head_sha, "pull_request", get))):
        raise Refused("the merge queue or pull request run changed while it was being verified")
    return queue, run, number, head_ref


def verify(sha, get=api):
    """The main push run of `sha` and its jobs, read through the GitHub REST API: `production.py verify` and the release
    record (production-release.yml, with the runner's token). Promotion on the host proves CI with `promotable`."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise Refused("an exact lowercase 40-character commit SHA is required")
    # Any commit main contains is promotable, not only its head, so merging never waits for a release.
    # `main...<sha>` lists what the candidate has beyond main: nothing, and so a tiny response, when main contains it.
    comparison = get("compare/main..." + sha)
    if (comparison.get("status") not in ("identical", "behind") or comparison.get("ahead_by") != 0
            or comparison.get("merge_base_commit", {}).get("sha") != sha):
        raise Refused("candidate is not a commit on main")
    known = _verified.get(sha) if _verified is not None else None
    if known and known.get("pull") is not None:
        # Within one verification_reuse block, a verification through the PR→queue chain re-reads the queue run, the pull request
        # run and then the push run, each once and none of their jobs: a rerun or newer run of any of them, or a push
        # run that changed at any time since it was verified, means a full verification. Reading the push run only
        # last bounds the requests to the anonymous quota.
        if (unchanged(known["queue"], latest_run(sha, "merge_group", get))
                and unchanged(known["pull"], latest_run(known["pull"]["head_sha"], "pull_request", get))
                and unchanged(known["push"], latest_run(sha, "push", get))):
            return known["result"]
        known = None
    latest = latest_run(sha, "push", get)
    if latest is None:
        raise Refused("main has no CI push run for this exact commit")
    if (latest.get("head_sha") != sha or latest.get("head_branch") != "main"
            or latest.get("event") != "push" or latest.get("status") != "completed"
            or latest.get("conclusion") != "success"
            or latest.get("head_repository", {}).get("full_name") != REPOSITORY):
        raise Refused("latest main CI run is not a completed success for this repository and commit")
    if known and unchanged(known["push"], latest) and (known["queue"] is None or (
            unchanged(known["queue"], latest_run(sha, "merge_group", get))
            and unchanged(latest, latest_run(sha, "push", get)))):
        # Within one verification_reuse block: the same completed attempts have the same jobs, so the runs are read again
        # but not their jobs (the host's anonymous GitHub quota is 60 requests an hour). After the queue read
        # the push run is read once more, as the full path does, so a rerun between the reads is not missed.
        return known["result"]
    jobs = attempt_jobs(latest, get)
    verified_by, queue = latest, None
    for required in sorted(REQUIRED_JOBS):
        if required == QUEUE_VERIFIED_JOB and job_concluded(jobs, required, "skipped"):
            # The push run reused the queue's verification, or the pull request run the queue reused; GitHub, not
            # the push run, must show it passed.
            try:
                verified_by = queue = queue_verification(sha, get)
            except QueueSkipped as refused:
                try:
                    queue, verified_by, _, _ = queue_chain(sha, get, refused.queue, refused.jobs)
                except Refused as exc:
                    raise Refused(f"{refused}; {exc}") from None
        elif not job_concluded(jobs, required):
            raise Refused(f"required job did not pass: {required}")
    if not unchanged(latest, latest_run(sha, "push", get)):
        raise Refused("the main CI push run changed while it was being verified")
    result = {"sha": sha, "run_id": latest["id"], "run_attempt": latest["run_attempt"], "url": latest["html_url"],
              "verify_run_id": verified_by["id"], "verify_run_attempt": verified_by["run_attempt"],
              "verify_run_url": verified_by["html_url"]}
    if _verified is not None:
        _verified[sha] = {"push": latest, "queue": None if verified_by is latest else queue,
                          "pull": verified_by if queue is not None and verified_by is not queue else None, "result": result}
    return result


# Full verifications of this process, set only inside `verification_reuse`.
_verified = None


@contextmanager
def verification_reuse():
    global _verified
    _verified = {}
    try:
        yield
    finally:
        _verified = None


# The image whose attestation proves a commit's CI to promotion. ci.yml's `publish` job, the only one that can sign as
# ci.yml@refs/heads/main, runs only on a main push after `CI gate` passed: the commit's own verification, or a
# validated reuse of the merge queue's or the pull request run's (scripts/ci_reuse.py applies queue_chain there).
PROOF_TARGET = "runtime"


def promotable(sha):
    """Whether promotion may take `sha`, without the GitHub REST API (ANYM-406): main contains it, by `git ls-remote`
    and main's history fetched treeless, and the verifier accepts its `PROOF_TARGET` attestation. An attestation never
    changes, so a later failed rerun of the commit's run does not withdraw it."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise Refused("an exact lowercase 40-character commit SHA is required")
    with tempfile.TemporaryDirectory(prefix="deedkit-main-") as directory:
        repository = Path(directory) / "main.git"
        # git sees only this repository: no inherited GIT_* variable (GIT_DIR, GIT_DEFAULT_HASH ...), no global or
        # system configuration (URL rewrites, credential helpers), replacement objects, templates or prompts.
        environment = {**{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
                       "HOME": directory, "XDG_CONFIG_HOME": directory, "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0"}
        # A local failure is no network blip: it pauses a deploy (Refused).
        run(["git", "init", "--quiet", "--bare", "--template=", str(repository)], env=environment)
        try:
            # From inside the empty repository: the caller's working directory repository and its config never apply.
            head = remote_main(environment, cwd=repository)
            # Commits only (no trees, no blobs): exactly the history of the head ls-remote named.
            run(["git", "-C", str(repository), "fetch", "--quiet", "--filter=tree:0", SOURCE, head],
                env=environment, reason=True)
        except Refused as exc:
            raise Unreachable(f"main could not be read: {exc}") from None
        if not on_main(repository, sha, head, environment):
            raise Refused("candidate is not a commit on main")
    proof = published(PROOF_TARGET, sha)
    return {"sha": sha, "main": head, "attestation": {"target": PROOF_TARGET, **proof}}


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def config(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o022:
        raise Refused("production config must be owned by the operator and not writable by others")
    value = read(path)
    if value.get("version") != 1 or not value.get("projects"):
        raise Refused("invalid production configuration")
    state = Path(value["state_directory"])
    if not state.is_absolute() or state.is_symlink():
        raise Refused("state directory must be an absolute non-symlink path")
    seen = set()
    for project in value["projects"]:
        name = project["name"]
        if name in seen or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            raise Refused("invalid or duplicate Compose project")
        seen.add(name)
        if not project.get("services") or set(project["services"].values()) - BUILDS.keys():
            raise Refused("select explicit services with known build targets")
        if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", service)
               or service in {"db", "calendar-db", "database", "postgres", "operator"}
               for service in project["services"]):
            raise Refused("only named application services may be promoted")
        for filename in [project["directory"], *project["files"], *project.get("env_files", [])]:
            if not Path(filename).is_absolute() or not Path(filename).exists():
                raise Refused("Compose paths must be absolute and already exist")
    return value


@contextmanager
def lock(state):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.stat().st_uid != os.geteuid() or state.stat().st_mode & 0o077:
        raise Refused("production state directory must be private and operator-owned")
    with (state / "deploy.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy("another production operation holds the deployment lock") from exc
        yield


def compose(project):
    # Config omits inactive profiles by default, even for currently running
    # services. Loading profiles does not start them; apply selects exact names.
    result = ["docker", "compose", "--profile", "*", "--project-name", project["name"], "--project-directory", project["directory"]]
    for filename in project["files"]:
        result += ["-f", filename]
    for filename in project.get("env_files", []):
        result += ["--env-file", filename]
    return result


def snapshot(project, *, stopped=False):
    """The project's Compose document and the identity of each selected service's container. Only rollback and a
    failed health wait (`stopped`) accept a container that exited or keeps restarting: that is when they run."""
    document = json.loads(run(compose(project) + ["config", "--format", "json"]))
    running = {}
    for service in project["services"]:
        ids = run(["docker", "ps", *(["--all", "--filter", "label=com.docker.compose.oneoff=False"] if stopped else []),
                   "--filter", "label=com.docker.compose.project=" + project["name"],
                   "--filter", "label=com.docker.compose.service=" + service, "--format", "{{.ID}}"]).split()
        if len(ids) != 1 or service not in document["services"]:
            raise Refused("each selected service must already have exactly one running container")
        validate_runtime(document["services"][service])
        actual = json.loads(run(["docker", "inspect", ids[0]]))[0]
        condition = actual.get("State", {})
        if stopped and not (condition.get("Running") and not condition.get("Paused")
                            or condition.get("Status") in ("restarting", "exited")):
            raise Refused("a selected container is neither running, restarting nor exited")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", actual["Image"]):
            raise Refused("running container has no immutable image identity")
        running[service] = {"container": ids[0], "image": actual["Image"], "settings": container_settings(actual)}
    return document, running


# Version of the container settings digest. Unversioned digests came from a tool that hashed Mounts, Links and
# Ulimits in the order docker happened to return them; they cannot be compared and are refused explicitly.
SETTINGS_VERSION = "v2:"


def canonical_list(value):
    return sorted(value, key=lambda item: json.dumps(item, sort_keys=True)) if isinstance(value, list) else value


def container_settings(inspected):
    """A digest of a container's configuration, including what `docker update` changes in place (resources,
    restart policy), so drift without a new container or image is visible. Receipts keep the digest only.
    Docker returns Mounts, HostConfig.Links and inherited HostConfig.Ulimits in no stable order: they are sets."""
    value = {key: inspected.get(key) for key in ("Config", "HostConfig", "Mounts")}
    value["Mounts"] = canonical_list(value["Mounts"])
    if isinstance(value["HostConfig"], dict):
        value["HostConfig"] = {**value["HostConfig"], **{key: canonical_list(value["HostConfig"][key])
                                                          for key in ("Links", "Ulimits") if key in value["HostConfig"]}}
    return SETTINGS_VERSION + digest(value)


def legacy_identities(running):
    return any(not str(identity.get("settings", "")).startswith(SETTINGS_VERSION) for identity in running.values())


# The runtime's data/config mount roots, never executable roots.
MOUNT_ROOTS = ("/data", "/sessions", "/spool", "/run/secrets", "/run/deedkit",
               "/models", "/processing-locks", "/broker", "/voice-broker")
# A mount root named inside a larger argument, e.g. inline code or `--require=/data/x.js`.
MOUNT_REFERENCE = re.compile(r"(?<![\w./-])(?:" + "|".join(map(re.escape, MOUNT_ROOTS)) + r")(?![\w.-])")
CODE_SUFFIXES = (".py", ".pyc", ".pth", ".so", ".js", ".mjs", ".cjs", ".sh")
# Options whose value is a configuration/data file: the only mounted paths a command may name.
DATA_OPTIONS = {"--config", "--policy", "--home", "--dsn-file"}
SHELLS = {"sh", "bash", "dash", "ash", "zsh", "ksh", "busybox"}
NODE_CODE_OPTIONS = {"-e", "--eval", "-p", "--print", "-r", "--require", "--import",
                     "--loader", "--experimental-loader", "-i", "--interactive"}
# Diagnostics/encoding only. Every other PYTHON* setting can change imports or code
# (PYTHONPATH, PYTHONHOME, PYTHONUSERBASE, PYTHONSTARTUP, PYTHONSAFEPATH, ...).
PYTHON_VARIABLES = {"PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "PYTHONFAULTHANDLER",
                    "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONNOUSERSITE", "PYTHONHASHSEED", "PYTHONWARNINGS"}
LOADER_VARIABLES = {"NODE_OPTIONS", "NODE_PATH", "NODE_REPL_EXTERNAL_MODULE", "GCONV_PATH",
                    "BASH_ENV", "ENV", "PERL5LIB", "PERL5OPT", "RUBYLIB", "RUBYOPT"}
HOST_NAMESPACES = ("network_mode", "pid", "ipc", "uts", "userns_mode", "cgroup")
UNCONFINED = re.compile(r"(seccomp|apparmor|systempaths)[=:]unconfined|label[=:]disable")
CODE_REFUSED = "host-mounted code or an unknown mount root bypasses verified images; package extensions in main first"
IMPORT_REFUSED = "runtime import path overrides bypass verified images"
INLINE_REFUSED = "inline shell or interpreter code bypasses verified images"
PRIVILEGE_REFUSED = "privileged settings or host namespaces bypass the reviewed container sandbox"


def mounted(value):
    # Relative paths are also resolved from "/": an image WORKDIR may be the root.
    path = posixpath.normpath(posixpath.join("/", str(value)))
    return any(path == root or path.startswith(root + "/") for root in MOUNT_ROOTS)


def unsafe_variable(name, value):
    if name.startswith("PYTHON"):
        return name not in PYTHON_VARIABLES
    if name.startswith("LD_") or name in LOADER_VARIABLES:
        return True
    if name == "PATH":
        return any(not entry.startswith("/") or mounted(entry) for entry in str(value or "").split(":"))
    # A user site directory under HOME is imported and runs its .pth files.
    return name == "HOME" and mounted(value or "/")


def python_code(arguments):
    values = iter(arguments)
    for item in values:
        if item == "-":
            return "inline"
        if item.startswith("--"):
            if item == "--check-hash-based-pycs":
                next(values, None)
            continue
        if not item.startswith("-"):
            return None  # A script: validate_argv checks it as a path.
        for index, flag in enumerate(item[1:], 1):
            if flag == "c":
                return "inline"
            if flag in "mWX":
                value = item[index + 1:] or next(values, "")
                # With WORKDIR /, `-m broker.x` imports a host-mounted directory.
                if flag == "m":
                    return "module" if mounted(value.partition(".")[0] or "/") else None
                break
    return None


def interpreter_code(argv):
    # Every position may start a program: wrappers such as nice or timeout pass it on.
    for index, item in enumerate(argv):
        program, arguments = posixpath.basename(item), argv[index + 1:]
        short = [value for value in arguments if value.startswith("-") and not value.startswith("--")]
        if ((program in SHELLS and any("c" in value for value in short))
                or (re.fullmatch(r"perl[0-9.]*", program) and any("e" in value.lower() for value in short))
                or (program in ("node", "nodejs")
                    and any(value.partition("=")[0] in NODE_CODE_OPTIONS for value in arguments))
                or (program == "env" and (any("S" in value for value in short)
                    or any(value.partition("=")[0] == "--split-string" for value in arguments)))):
            return "inline"
        if re.fullmatch(r"python[0-9.]*", program) and (result := python_code(arguments)):
            return result
    return None


def validate_argv(argv, *, healthcheck=False):
    argv = shlex.split(argv) if isinstance(argv, str) else [str(item) for item in argv or []]
    if healthcheck:
        if not argv or argv[0] == "NONE":
            return
        argv = ["sh", "-c", *argv[1:]] if argv[0] == "CMD-SHELL" else argv[1:] if argv[0] == "CMD" else argv
    previous = ""
    for item in argv:
        name, separator, value = item.partition("=")
        if separator and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and unsafe_variable(name, value):
            raise Refused(IMPORT_REFUSED)
        if (previous not in DATA_OPTIONS and not (separator and name in DATA_OPTIONS)
                and (mounted(item) or MOUNT_REFERENCE.search(item))):
            raise Refused(CODE_REFUSED)
        previous = item
    code = interpreter_code(argv)
    if code == "module":
        raise Refused(CODE_REFUSED)
    # A health probe needs inline Python; it still cannot name a mount (checked above).
    if code == "inline" and not (healthcheck and re.fullmatch(r"python[0-9.]*", posixpath.basename(argv[0]))):
        raise Refused(INLINE_REFUSED)


def validate_runtime(unit):
    # Legacy host-mounted extensions would bypass the verified image entirely.
    mounts = [(mount["target"], mount.get("source", "")) for mount in unit.get("volumes") or []]
    # Compose secrets/configs are file mounts too; a config defaults to /<name>.
    for kind, default in (("secrets", "/run/secrets/"), ("configs", "/")):
        for item in unit.get(kind) or []:
            item = {"source": item} if isinstance(item, str) else item
            target = item.get("target") or item["source"]
            mounts.append((target if target.startswith("/") else default + target, item["source"]))
    for target, source in mounts:
        target = posixpath.normpath(target)
        if (not target.startswith("/") or not mounted(target)
                or target.endswith(CODE_SUFFIXES) or str(source).endswith(CODE_SUFFIXES)):
            raise Refused(CODE_REFUSED)
    environment = unit.get("environment") or {}
    if isinstance(environment, list):
        environment = dict(item.partition("=")[::2] for item in environment)
    if any(unsafe_variable(name, value) for name, value in environment.items()):
        raise Refused(IMPORT_REFUSED)
    if unit.get("working_dir") and mounted(unit["working_dir"]):
        raise Refused(CODE_REFUSED)
    def split(value):
        return shlex.split(value) if isinstance(value, str) else [str(item) for item in value or []]
    # Without an entrypoint override the command may itself start a program.
    validate_argv(split(unit.get("entrypoint")) + split(unit.get("command")))
    validate_argv((unit.get("healthcheck") or {}).get("test"), healthcheck=True)
    if unit.get("post_start") or unit.get("pre_stop"):
        raise Refused("lifecycle hooks run commands outside the verified image; package them in main first")
    if (any(unit.get(key) for key in ("privileged", "cap_add", "devices", "device_cgroup_rules", "volumes_from", "gpus"))
            or any(UNCONFINED.search(str(option)) for option in unit.get("security_opt") or [])
            or any(str(unit.get(key) or "") == "host" for key in HOST_NAMESPACES)
            or str(unit.get("pid") or "").startswith(("container:", "service:"))):
        raise Refused(PRIVILEGE_REFUSED)


def escaped(value):
    # Resolved Compose values are parsed a second time when applying the snapshot.
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [escaped(item) for item in value]
    if isinstance(value, dict):
        return {key: escaped(item) for key, item in value.items()}
    return value


def check_space(directory):
    if shutil.disk_usage(directory).free < MIN_FREE_BYTES:
        raise Refused(f"at least 15 GiB free is required on {directory} before obtaining production images; "
                      "`production.py images --remove` removes old release images")


CONTAINERD_ROOT = Path("/var/lib/containerd")


def check_image_space(docker_root):
    check_space(docker_root)
    # The containerd image store keeps image content under containerd's root, often another disk than DockerRootDir.
    if CONTAINERD_ROOT.is_dir():
        check_space(CONTAINERD_ROOT)


IMAGE_LOCK = Path("/var/lib/deedkit-production/image-build.lock")


@contextmanager
def image_lock():
    """One tag lookup or build at a time on the production host, whichever tool or state directory asks."""
    if not IMAGE_LOCK.parent.is_dir():
        yield  # CI runners and development machines have no production host state.
        return
    with IMAGE_LOCK.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refused("another image build holds the production image lock") from exc
        yield


def build_recipe(target, sha):
    """The build arguments of a target and a digest that binds an image to them, its Dockerfile and stage."""
    _, dockerfile, stage, _ = BUILDS[target]
    arguments = {"DEEDKIT_SOURCE_SHA": sha}
    if stage:
        arguments["ANYMSG_INSTALL_TELEMETRY"] = "1"
    if target == "http-router":
        arguments["VERSION_REF"] = sha
    return arguments, digest({"dockerfile": dockerfile, "stage": stage, "arguments": arguments})


def names_revision(image, sha, dockerfile, recipe=None):
    config = json.loads(run(["docker", "image", "inspect", image]))[0]["Config"]
    labels = config.get("Labels") or {}
    return (labels.get("org.opencontainers.image.revision") == sha
            and (recipe is None or (labels.get("io.deedkit.production.build") == recipe
                                    and labels.get("org.opencontainers.image.source") == "https://github.com/" + REPOSITORY))
            and (dockerfile != "services/messaging/Dockerfile"
                 or "DEEDKIT_SOURCE_SHA=" + sha in (config.get("Env") or [])))


def published(target, sha):
    """Resolve `ghcr.io/gosync-inc/deedkit-<target>:<sha>` to one digest and verify its attestation, tokenless.

    The pinned verifier (tools/attest-verify) runs with an empty environment, so no GitHub or registry
    credential can reach it; any refusal stops the target before a pull (ANYM-301 B2).
    """
    try:
        info = VERIFIER.lstat()
        content = VERIFIER.read_bytes() if stat.S_ISREG(info.st_mode) else b""
    except OSError:
        info, content = None, b""
    if (info is None or not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
            or info.st_mode & 0o022 or hashlib.sha256(content).hexdigest() != VERIFIER_SHA256):
        raise Refused(f"the attestation verifier {VERIFIER} is missing, writable by others or not the pinned build")
    try:
        output = run([str(VERIFIER), "--image", REGISTRY + target, "--sha", sha], env={"PATH": "/usr/bin:/bin"},
                     reason=True)
    except OSError as exc:  # for example a pinned file without the execute bit
        raise Refused(f"the attestation verifier {VERIFIER} could not be run") from exc
    except Refused as exc:
        if VERIFIER_ABSENT.search(str(exc)):
            raise Unattested(str(exc)) from None
        if VERIFIER_UNREACHED.search(str(exc)):
            raise Unreachable(str(exc)) from None
        raise
    try:
        value = json.loads(output)
    except ValueError:
        value = None
    if (not isinstance(value, dict) or value.get("sha") != sha
            or not all(isinstance(value.get(key), str) and IMAGE_DIGEST.fullmatch(value[key])
                       for key in ("digest", "config_digest"))
            or value.get("reference") != f"{REGISTRY}{target}@{value['digest']}"):
        raise Refused("the attestation verifier returned no verified reference for this target and revision")
    return {key: value[key] for key in ("reference", "digest", "config_digest")}


def build_image(source, sha, target, *, check=False, verified=None):
    """The image of `target` at `sha`: pulled by verified digest when CI publishes it, otherwise built."""
    context, dockerfile, stage, smoke = BUILDS[target]
    # A reused tag must carry this exact recipe, not only the revision.
    arguments, recipe = build_recipe(target, sha)
    # A container check builds the working tree; it never shares or reuses production tags.
    tag = f"deedkit-{'check' if check else 'production'}-{target}:{sha}"
    pull = not check and target in PUBLISHED
    reference, matches = "", set()
    if pull:
        verified = verified or published(target, sha)
        # The image store reports the config digest (classic store) or the manifest digest (containerd store).
        reference, matches = verified["reference"], {verified["digest"], verified["config_digest"]}
    with image_lock():
        # Builds are not reproducible, and retagging can drop the image running containers use
        # (containerd image store), so an existing production tag is reused, never rebuilt or retagged.
        existing = [] if check else run(["docker", "image", "ls", "--quiet", "--no-trunc", tag]).split()
        if existing:
            image = existing[0]
            if len(set(existing)) != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
                raise Refused("existing image tag is ambiguous; inspect it before preparing this revision")
            if not names_revision(image, sha, dockerfile, recipe):
                raise Refused("existing image tag does not name the candidate source revision and build; "
                              "remove it only after checking that no container uses it")
            # Matching labels are not enough: a host-built image of the same revision has other content.
            if pull and image not in matches:
                raise Refused("existing image tag is not the verified CI image of this revision; "
                              "remove it only after checking that no container uses it")
        elif pull:
            run(["docker", "pull", "--quiet", reference])
            image = run(["docker", "image", "inspect", "--format", "{{.Id}}", reference]).strip()
            if image not in matches:
                raise Refused("pulled image is not the verified digest")
            if not names_revision(image, sha, dockerfile, recipe):
                raise Refused("pulled image does not name the candidate source revision and build")
            run(["docker", "tag", reference, tag])
            if run(["docker", "image", "ls", "--quiet", "--no-trunc", tag]).split() != [image]:
                raise Refused("the production tag does not name the pulled image")
        else:
            command = ["docker", "build", "--label", "org.opencontainers.image.revision=" + sha,
                       "--label", "org.opencontainers.image.source=https://github.com/" + REPOSITORY,
                       "--label", "io.deedkit.production.build=" + recipe]
            for argument, value in arguments.items():
                command += ["--build-arg", f"{argument}={value}"]
            command += ["--tag", tag, "--file", str(source / dockerfile)] + (["--target", stage] if stage else [])
            run(command + [str(source / context)])
            image = run(["docker", "image", "inspect", "--format", "{{.Id}}", tag]).strip()
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
                raise Refused("build did not produce an immutable image ID")
            # Only a fresh build is checked: earlier images keep their rollback path without this variable.
            if dockerfile == "services/messaging/Dockerfile" and not names_revision(image, sha, dockerfile):
                raise Refused("built image does not name the candidate source revision")
    name = f"deedkit-smoke-{target}-{uuid.uuid4().hex[:12]}"
    try:
        isolated = ["docker", "run", "--name", name, "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--entrypoint"]
        if target == "http-router":
            run(isolated + ["/deedkit-release-router", image, "--version"])
        else:
            run(isolated + ["python", image, "-c", smoke])
    except Exception:
        # This name was created by this invocation; never stop a shared runtime.
        subprocess.run(["docker", "stop", "--time", "10", name], capture_output=True, timeout=20)
        raise
    return image


def prepare_router(settings, sha):
    """Build enrollment infrastructure from fetched main, without touching serving units."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise Refused("an exact lowercase 40-character commit SHA is required")
    promotable(sha)
    state = Path(settings["state_directory"])
    check_space(state)
    docker_root = run(["docker", "info", "--format", "{{.DockerRootDir}}"]).strip()
    if not docker_root or not Path(docker_root).is_dir():
        raise Refused("preparation requires the local production Docker engine")
    check_image_space(docker_root)
    release = state / ("router-" + sha)
    if release.exists():
        raise Refused("router directory already exists; inspect it before a new preparation")
    release.mkdir(mode=0o700)
    source = release / "source"
    run(["git", "init", "--quiet", str(source)])
    run(["git", "-C", str(source), "remote", "add", "origin", "https://github.com/" + REPOSITORY + ".git"])
    # The verified commit itself, which main contains (promotable), whether or not main moved on since.
    run(["git", "-C", str(source), "fetch", "--depth=1", "origin", sha])
    if run(["git", "-C", str(source), "rev-parse", "FETCH_HEAD"]).strip() != sha:
        raise Refused("fetched source is not the verified candidate commit")
    run(["git", "-C", str(source), "checkout", "--quiet", "--detach", sha], umask=0o022)
    image = build_image(source, sha, "http-router")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise Refused("router build did not produce an immutable image ID")
    evidence = promotable(sha)
    receipt = {"version": 1, "target": "http-router", "sha": sha, "image": image, "ci": evidence}
    write(release / "router-build.json", receipt)
    return receipt


def published_targets(settings):
    """The CI-published targets the projects of `settings` select."""
    return sorted({target for project in settings["projects"] for target in project["services"].values()} & set(PUBLISHED))


def prepare(settings, sha):
    evidence = promotable(sha)
    state = Path(settings["state_directory"])
    check_space(state)
    docker_root = run(["docker", "info", "--format", "{{.DockerRootDir}}"]).strip()
    if not docker_root or not Path(docker_root).is_dir():
        raise Refused("preparation requires the local production Docker engine")
    check_image_space(docker_root)
    release = state / sha
    if release.exists():
        raise Refused("candidate directory already exists; inspect it before a new preparation")
    release.mkdir(mode=0o700)
    source = release / "source"
    run(["git", "init", "--quiet", str(source)])
    run(["git", "-C", str(source), "remote", "add", "origin", "https://github.com/" + REPOSITORY + ".git"])
    # The verified commit itself, which main contains (promotable), whether or not main moved on since.
    run(["git", "-C", str(source), "fetch", "--depth=1", "origin", sha])
    if run(["git", "-C", str(source), "rev-parse", "FETCH_HEAD"]).strip() != sha:
        raise Refused("fetched source is not the verified candidate commit")
    # Docker COPY preserves checkout modes. Public code must be readable by
    # image UID 10001; private release parents and snapshots retain 0700/0600.
    run(["git", "-C", str(source), "checkout", "--quiet", "--detach", sha], umask=0o022)
    snapshots = []
    for project in settings["projects"]:
        document, running = snapshot(project)
        snapshots.append({"project": project, "before": document, "running": running})
    images = {}
    targets = sorted({target for project in settings["projects"] for target in project["services"].values()})
    # Every CI-published target is resolved and attested before the first pull.
    verified = {target: published(target, sha) for target in published_targets(settings)}
    for target in targets:
        images[target] = build_image(source, sha, target, verified=verified.get(target))
    # Recheck after potentially long image builds. No production command ran yet.
    evidence = promotable(sha)
    projects = []
    for item in snapshots:
        project, before = item["project"], item["before"]
        candidate = json.loads(json.dumps(before))
        for service in candidate["services"].values():
            service.pop("build", None)
        for service, target in project["services"].items():
            unit = candidate["services"][service]
            unit["image"] = images[target]
            unit.setdefault("labels", {})["org.opencontainers.image.revision"] = sha
        path = release / (project["name"] + ".json")
        write(path, escaped(candidate))
        # Verify that serializing/re-reading did not change secrets, mounts or ports.
        reread = json.loads(run(["docker", "compose", "--profile", "*", "-f", str(path), "config", "--format", "json"]))
        if reread != candidate:
            raise Refused("resolved Compose snapshot did not round-trip exactly")
        rollback = json.loads(json.dumps(before))
        for unit in rollback["services"].values():
            unit.pop("build", None)
        for service, identity in item["running"].items():
            rollback["services"][service]["image"] = identity["image"]
        previous = release / (project["name"] + ".before.json")
        write(previous, escaped(rollback))
        # Bound here, so a snapshot changed before apply can never become the rollback baseline.
        projects.append({"name": project["name"], "file": str(path), "digest": digest(read(path)),
                         "before_digest": digest(before), "running": item["running"],
                         "before_file_digest": digest(read(previous))})
    # What current.json said at preparation: rollback of a release that failed its health wait requires it unchanged.
    current = read(state / "current.json") if (state / "current.json").exists() else None
    receipt = {"version": 1, "sha": sha, "ci": evidence, "images": images,
               "references": {target: value["reference"] for target, value in verified.items()},
               "config_digest": digest(settings), "current_digest": digest(current), "projects": projects}
    write(release / "prepared.json", receipt)
    return receipt


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply(settings, sha):
    promotable(sha)
    state = Path(settings["state_directory"])
    release = state / sha
    receipt = read(release / "prepared.json")
    if receipt["sha"] != sha or receipt["config_digest"] != digest(settings):
        raise Refused("prepared release does not match the selected production configuration")
    if (release / "attempted.json").exists():
        raise Refused("this release was already attempted; inspect its result before any recovery")
    for target, image in receipt["images"].items():
        actual = json.loads(run(["docker", "image", "inspect", image]))[0]
        if (actual["Id"] != image or actual["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != sha):
            raise Refused("prepared image is missing or has the wrong source revision")
    for project, item in zip(settings["projects"], receipt["projects"], strict=True):
        if legacy_identities(item["running"]):
            raise Refused("prepared by an older production.py with order-dependent container settings; prepare again")
        document, running = snapshot(project)
        if (item["name"] != project["name"] or digest(document) != item["before_digest"]
                or running != item["running"] or digest(read(item["file"])) != item["digest"]):
            raise Refused("production or prepared configuration changed since preparation")
        previous = release / (project["name"] + ".before.json")
        if "before_file_digest" not in item:
            raise Refused("this release was prepared without a bound rollback snapshot; prepare it again")
        if not previous.exists() or digest(read(previous)) != item["before_file_digest"]:
            raise Refused("the before-release snapshot is missing or changed since preparation")
    # Last remote gate immediately before the first mutation, under the host lock.
    evidence = promotable(sha)
    write(release / "attempted.json", {"sha": sha, "ci": evidence, "status": "applying"})
    started, touched = {}, []
    for project, item in zip(settings["projects"], receipt["projects"], strict=True):
        command = ["docker", "compose", "--profile", "*", "--project-name", project["name"], "-f", item["file"]]
        services = list(project["services"])
        # Stop the existing owners before replacements can connect to their sessions.
        run(command + ["stop", *services])
        touched.append((project, item))
        try:
            run(command + ["up", "-d", "--no-build", "--pull", "never", "--no-deps",
                           "--wait", "--wait-timeout", "180", *services])
        except Refused as exc:
            unhealthy(release, sha, receipt, evidence, touched, exc)
        for service, target in project["services"].items():
            ids = run(command + ["ps", "-q", service]).split()
            if len(ids) != 1:
                raise Refused("replacement service is missing")
            actual = json.loads(run(["docker", "inspect", ids[0]]))[0]
            if actual["Image"] != receipt["images"][target] or not actual["State"]["Running"]:
                raise Refused("replacement did not start the prepared image")
            started[project["name"], service] = ids[0], container_settings(actual)
    # What rollback must find unchanged before it stops anything, captured the way prepare captures the baseline.
    # It must still be what this apply started: a change meanwhile leaves the attempt unresolved.
    restore = []
    for project, item in zip(settings["projects"], receipt["projects"], strict=True):
        document, running = snapshot(project)
        if digest(document) != item["before_digest"] or any(
                running[service]["image"] != receipt["images"][target]
                or not started[project["name"], service][0].startswith(running[service]["container"])
                or started[project["name"], service][1] != running[service]["settings"]
                for service, target in project["services"].items()):
            raise Refused("production changed while this release was applied; inspect it before any recovery")
        restore.append({"name": project["name"], "services": sorted(project["services"]),
                        "before_file_digest": item["before_file_digest"],
                        "after_digest": digest(document), "running": running})
    result = {"sha": sha, "ci": evidence, "images": receipt["images"], "references": receipt.get("references", {}),
              "status": "applied", "applied_at": now(), "rollback": {"version": 1, "projects": restore}}
    write(release / "result.json", result)
    write(state / "current.json", result)
    return result


def unhealthy(release, sha, receipt, evidence, touched, failure):
    """After `up --wait` failed: when every replacement started so far runs the prepared image, in any state, record
    the release as `unhealthy` with rollback data for the projects it touched; otherwise the attempt stays unresolved.
    Either way current.json keeps naming the previous release, and the apply is refused."""
    restore = []
    try:
        for project, item in touched:
            document, running = snapshot(project, stopped=True)
            if digest(document) != item["before_digest"] or any(running[service]["image"] != receipt["images"][target]
                                                               for service, target in project["services"].items()):
                raise Refused("a replacement does not run the prepared image")
            restore.append({"name": project["name"], "services": sorted(project["services"]),
                            "before_file_digest": item["before_file_digest"],
                            "after_digest": digest(document), "running": running})
    except Exception:
        raise Refused("apply stopped before every replacement ran the prepared image; "
                      "inspect it before any recovery") from failure
    write(release / "result.json", {"sha": sha, "ci": evidence, "images": receipt["images"],
                                    "references": receipt.get("references", {}), "status": "unhealthy",
                                    "failed_at": now(), "rollback": {"version": 1, "projects": restore}})
    raise Refused(f"a replacement failed its health wait; `rollback --sha {sha}` returns the previous release") from failure


def unfinished(state):
    """Releases whose apply or rollback started but never recorded its result."""
    return sorted(path.parent.name for marker, outcome in (("attempted.json", "result.json"),
                                                           ("rollback-attempted.json", "rollback.json"))
                  for path in state.glob("*/" + marker) if not (path.parent / outcome).exists())


def rollback(settings, sha):
    """Return the current release's selected services to the images and configuration that ran before it, or
    those of a release whose apply failed its health wait (`unhealthy`), which never became current.

    Owner-confirmed like apply, except as `deploy`'s one automatic rollback. It never calls promotable(): the previous
    revision is no longer current main. The caller holds the host lock, as main() does for every state-changing action.
    """
    state = Path(settings["state_directory"])
    release = state / sha
    current = read(state / "current.json") if (state / "current.json").exists() else None
    result = read(release / "result.json") if (release / "result.json").exists() else {}
    failed, named = result.get("status") == "unhealthy", current or {}
    if not result or not failed and (named.get("sha") != sha or named.get("status") != "applied"):
        raise Refused("only the current applied release can be rolled back")
    if (release / "rollback-attempted.json").exists():
        raise Refused("this release was already rolled back or attempted; inspect it before any recovery")
    if blocked := unfinished(state):
        raise Refused("an apply or rollback is unfinished: " + ", ".join(blocked) + "; inspect it first")
    data = result.get("rollback")
    if not isinstance(data, dict) or data.get("version") != 1 or result.get("sha") != sha:
        raise Refused("this release has no rollback data; follow the manual rollback procedure (production.md)")
    prepared = read(release / "prepared.json")
    if prepared["config_digest"] != digest(settings):
        raise Refused("production configuration changed since this release")
    # current.json still names the release that ran before the failed one, unless another one was applied since.
    if failed and prepared.get("current_digest") != digest(current):
        raise Refused("another release was applied or rolled back after this one was prepared")
    names = [project["name"] for project in settings["projects"]]
    touched = [item.get("name") for item in data["projects"]]
    # An applied release replaced every project; a failed health wait, those up to the one that failed.
    if not touched or touched != (names[:len(touched)] if failed else names):
        raise Refused("rollback data does not match the selected production configuration")
    plan = []
    for project, item in zip(settings["projects"], data["projects"]):
        before = release / (project["name"] + ".before.json")
        if item["services"] != sorted(project["services"]):
            raise Refused("rollback data does not match the selected production configuration")
        if not before.exists() or digest(read(before)) != item["before_file_digest"]:
            raise Refused("the before-release snapshot is missing or was modified")
        if legacy_identities(item["running"]):
            raise Refused("rollback data was written by an older production.py with order-dependent container settings; "
                          "use the manual procedure")
        # A crashed replacement exits or keeps restarting: that is when rollback is needed.
        document, running = snapshot(project, stopped=True)
        if digest(document) != item["after_digest"] or running != item["running"]:
            raise Refused("production changed since this release was applied")
        previous = {service: read(before)["services"][service]["image"] for service in project["services"]}
        for image in sorted(set(previous.values())):
            # It never pulls: the exact previous image must still be on this host.
            try:
                present = bool(IMAGE_DIGEST.fullmatch(image)) and json.loads(
                    run(["docker", "image", "inspect", image]))[0]["Id"] == image
            except Refused:
                present = False
            if not present:
                raise Refused("a previous image is no longer present on this host")
        plan.append((project, before, previous))
    write(release / "rollback-attempted.json", {"sha": sha, "status": "rolling-back", "started_at": now()})
    for project, before, previous in plan:
        command = ["docker", "compose", "--profile", "*", "--project-name", project["name"], "-f", str(before)]
        services = list(project["services"])
        # Stop the release's owners before the previous ones can connect to their sessions.
        run(command + ["stop", *services])
        run(command + ["up", "-d", "--no-build", "--pull", "never", "--no-deps",
                       "--wait", "--wait-timeout", "180", *services])
        for service, image in previous.items():
            ids = run(command + ["ps", "-q", service]).split()
            if len(ids) != 1:
                raise Refused("restored service is missing")
            actual = json.loads(run(["docker", "inspect", ids[0]]))[0]
            if actual["Image"] != image or not actual["State"]["Running"]:
                raise Refused("restored service does not run its previous image")
    outcome = {"sha": sha, "status": "rolled-back", "rolled_back_at": now(),
               "previous_images": {project["name"]: previous for project, _, previous in plan}}
    write(release / "rollback.json", outcome)
    if not failed:  # current.json already names the release that runs again
        write(state / "current.json", {**outcome, "images": result["images"]})
    return outcome


# Image retention (production.md "Image retention"). Besides this configuration's state, other states on the host keep
# their own current and rollback images: the host state root and its subdirectories with a plan A (`current.json`) or
# HTTP (`http-current.json`) receipt, and the state directory of every /etc/deedkit config.
HOST_STATE = Path("/var/lib/deedkit-production")
CONFIG_DIRECTORY = Path("/etc/deedkit")
RELEASE_DIRECTORY = re.compile(r"[0-9a-f]{40}")


def load(path):
    """A receipt that protects images. Unreadable, nothing may be removed."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise Refused(f"cannot read {path}; no image is removed") from exc


def retention_states(state):
    configured = [Path(value["state_directory"]) for value in map(load, sorted(CONFIG_DIRECTORY.glob("*.json")))
                  if isinstance(value, dict) and isinstance(value.get("state_directory"), str)]
    found = [path for path in sorted(HOST_STATE.glob("*")) if path.is_dir()
             and ((path / "current.json").exists() or (path / "http-current.json").exists())]
    states, seen = [], set()
    for directory in [state, HOST_STATE, *found, *configured]:
        if directory.is_dir() and directory.resolve() not in seen:
            seen.add(directory.resolve())
            states.append(directory)
    return states


def free_lock(directory, stack):
    """Take a state's deploy lock without waiting, held until `stack` closes; False when another operation holds it."""
    try:
        handle = stack.enter_context((directory / "deploy.lock").open("a"))
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:  # BlockingIOError included
        return False
    return True


def state_images(directory, keep, revisions, *, label, idle):
    """The images one state still needs: its current release, the images a rollback restored, the current release's
    `*.before.json` (the rollback target), every preparation neither applied nor attempted, the HTTP serving and retained
    generations, and images an operator protected. Unless the state is `idle` (its deploy lock was free), the images
    tagged with the revision of any release directory without a result: a preparation may be pulling them now."""
    current = load(directory / "current.json") if (directory / "current.json").exists() else {}
    for image in current.get("images", {}).values():
        keep(image, ("rolled-back release " if current.get("status") == "rolled-back" else "current release ")
             + current["sha"] + label)
    for services in current.get("previous_images", {}).values():
        for image in services.values():
            keep(image, "restored by the rollback of " + current["sha"] + label)
    if current.get("sha"):
        for before in (directory / current["sha"]).glob("*.before.json"):
            for unit in load(before)["services"].values():
                keep(unit.get("image"), "rollback target of the current release" + label)
    if (directory / "http-current.json").exists():
        # Read one level deep: each HTTP receipt nests its predecessor, and those before it are retired.
        serving = load(directory / "http-current.json")
        previous = serving.get("previous")
        serving = {**serving, "previous": {key: value for key, value in previous.items() if key != "previous"}
                   if isinstance(previous, dict) else previous}
        for image in IMAGE_DIGEST.findall(json.dumps(serving)):
            keep(image, f"HTTP release state {directory / 'http-current.json'}")
    for path in sorted(directory.glob("*/prepared.json")):
        if not (path.parent / "result.json").exists() and not (path.parent / "http-attempted.json").exists():
            for image in load(path).get("images", {}).values():
                keep(image, f"prepared release {path.parent.name}{label} is not applied")
    for path in sorted(directory.glob("*/http-prepared.json")):
        if not (path.parent / "http-attempted.json").exists():
            for image in load(path).get("images", {}).values():
                keep(image, f"HTTP release {path.parent.name}{label} is prepared, not attempted")
    for path in sorted(directory.glob("*.json")):
        if path.name not in ("current.json", "http-current.json", "deploy.json"):
            # Operator plans next to the receipts, such as a storage cleanup's protected images.
            plan = load(path)
            for image in plan.get("protected_images", []) if isinstance(plan, dict) else []:
                keep(image, f"protected by {path}")
    if not idle:
        for release in directory.glob("*"):
            if (RELEASE_DIRECTORY.fullmatch(release.name) and release.is_dir() and not (release / "result.json").exists()
                    and not (release / "http-attempted.json").exists()):
                revisions.setdefault(release.name, f"release {release.name}{label} may be preparing now")


def pinned_images(keep, states):
    """Image IDs named in a top-level JSON file of /etc/deedkit (the configurations and the jobs that pin an image, such
    as file-lifecycle.json) or of an operator directory under the host state root that is not a state."""
    states = {path.resolve() for path in states}
    directories = [CONFIG_DIRECTORY, *(path for path in sorted(HOST_STATE.glob("*")) if path.is_dir()
                                       and path.resolve() not in states and not RELEASE_DIRECTORY.fullmatch(path.name))]
    for directory in directories:
        for path in sorted(directory.glob("*.json")):
            for image in IMAGE_DIGEST.findall(json.dumps(load(path))):
                keep(image, f"named in {path}")


def release_name(name):
    """The CI-published target of a `deedkit-production-<target>:<tag>` name, or None."""
    repository = name.rpartition(":")[0]
    return next((target for target in PUBLISHED if repository == "deedkit-production-" + target), None)


def images_report(settings, *, held=(), stack=None):
    """Which `deedkit-production-<target>` images the retention rules keep, and why; `remove_images` removes the rest.
    `held` are the state directories whose deploy lock the caller holds; the others' locks are probed, and held on
    `stack` when given, so no preparation starts there before the removal ends."""
    state = Path(settings["state_directory"])
    reasons = {}
    def keep(image, why):
        if isinstance(image, str) and IMAGE_DIGEST.fullmatch(image):
            reasons.setdefault(image, []).append(why)
    states, revisions = retention_states(state), {}
    held = {Path(path).resolve() for path in (*held, state)}
    with ExitStack() as probes:
        for directory in states:
            state_images(directory, keep, revisions, label="" if directory == state else f" of {directory}",
                         idle=directory.resolve() in held or free_lock(directory, stack or probes))
    pinned_images(keep, states)
    containers = run(["docker", "ps", "--all", "--quiet", "--no-trunc"]).split()
    for value in json.loads(run(["docker", "inspect", *containers])) if containers else []:
        keep(value["Image"], "used by container " + value["Id"][:12])
    deployed = {target for project in settings["projects"] for target in project["services"].values()}
    tags, other = {}, {}
    for line in run(["docker", "image", "ls", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"]).splitlines():
        name, _, image = line.rpartition(" ")
        if release_name(name):
            tags.setdefault(image, []).append(name)
        elif not name.startswith(REGISTRY) and name != "<none>:<none>":
            other.setdefault(image, []).append(name)
    report = {"keep": [], "candidates": []}
    for image, tagged in sorted(tags.items()):
        targets = {release_name(name) for name in tagged} - {None}
        for name in tagged:
            if name.rpartition(":")[2] in revisions:
                keep(image, revisions[name.rpartition(":")[2]])
        if targets - deployed:
            # The backup runner, for one, pins its image in its own configuration.
            keep(image, "target " + ", ".join(sorted(targets - deployed)) + " is not deployed by this configuration")
        if image in other:
            keep(image, "also named " + ", ".join(sorted(other[image])))
        if image in reasons:
            report["keep"].append({"image": image, "tags": sorted(tagged), "reasons": reasons[image]})
        else:
            report["candidates"].append({"image": image, "tags": sorted(tagged),
                                         "reason": "not the current release, its rollback target, prepared, "
                                                   "named by a host receipt or used by a container"})
    return report


def owned_reference(name, target):
    """A release tag of `target`, or its GHCR name or digest reference: the only names removal may take away."""
    repository = name.partition("@")[0] if "@" in name else name.rpartition(":")[0]
    return repository in ("deedkit-production-" + target, REGISTRY + target)


def remove_images(settings, *, held=()):
    """Remove the retention report's candidates (production.md "Image retention"): each by its other references and then
    by image ID, never forced or pruned, so Docker still refuses an image a container uses. The caller holds the deploy
    locks of `held` and this state; the image lock keeps pulls out and the other states' free locks stay held until it
    ends. IDs and Docker's reasons are reported short, so a receipt holding this summary never pins an image."""
    with image_lock(), ExitStack() as stack:
        report = images_report(settings, held=held, stack=stack)
        removed, failed, summary = [], [], {}
        for number, item in enumerate(report["candidates"]):
            image = item["image"]
            entry = {"image": image.removeprefix("sha256:")[:12], "tags": item["tags"]}
            try:
                (target,) = {release_name(name) for name in item["tags"]}
                actual = json.loads(run(["docker", "image", "inspect", image]))[0]
                names = actual.get("RepoTags") or []
                if (actual["Id"] != image or not set(item["tags"]) <= set(names)
                        or not all(owned_reference(name, target) for name in names)):
                    raise Refused("its references changed since the report")
                # Docker removes an image by ID only while a single reference names it. A release tag stays to the end:
                # interrupted, the image is still a candidate of the next report.
                last = item["tags"][0]
                for name in [name for name in names if name != last]:
                    run(["docker", "image", "rm", "--no-prune", name], reason=True)
                run(["docker", "image", "rm", "--no-prune", image], reason=True)
                removed.append(entry)
            except (Refused, ValueError) as exc:
                # Docker refused this image (a container started from it, say): it stays, the next one is tried.
                reason = (str(exc).removesuffix("; production was not accepted") if isinstance(exc, Refused)
                          else "its tags name several targets")
                failed.append({**entry, "reason": IMAGE_DIGEST.sub(lambda found: found[0][7:19], reason)})
            except Exception as exc:
                # Anything else stops the pass; what it already removed is still reported. Its release tag stays until
                # the image goes, so the next pass finds an interrupted image again.
                summary = {"interrupted": f"{type(exc).__name__} at {entry['image']}",
                           "remaining": len(report["candidates"]) - number}
                break
        return {"removed": removed, "failed": failed, "kept": len(report["keep"]), **summary}


def retain_images(settings, *, held=()):
    """`remove_images` after a successful release. It never changes the release's outcome: a failure is only reported."""
    try:
        return remove_images(settings, held=held)
    except Exception as exc:
        return {"skipped": str(exc) if isinstance(exc, Refused) else type(exc).__name__}


# Continuous deployment (production.md "Continuous deployment"): one command that deploys the newest green main
# commit, or pauses for a human. A human creates or removes the pause file; `deploy` only creates it.
PAUSE = Path("/etc/deedkit/autodeploy.pause")
DEPLOY_INTERVAL = 20 * 60
# Consecutive runs whose `git ls-remote` or mirror fetch failed before the deployer pauses (about 15 minutes on the
# timer): a network blip heals by itself, a broken mirror or a refused access does not.
FETCH_FAILURES = 3
# Changes a human applies or installs by hand, so the deployer pauses for them.
SQL_INVENTORY = re.compile(r"\.sql$|^services/.+/migrations/")
DEPLOY_TOOLS = ("scripts/production.py", "scripts/seamless_release.py", "tools/attest-verify/", "services/release-router/")
def remote_main(env=None, cwd=None):
    """main's head from `git ls-remote`, which the API quota does not meter."""
    line = run(["git", "ls-remote", SOURCE, "refs/heads/main"], cwd=cwd, env=env, reason=True).split()
    if len(line) != 2 or line[1] != "refs/heads/main" or not re.fullmatch(r"[0-9a-f]{40}", line[0]):
        raise Refused("git ls-remote returned no main commit")
    return line[0]


def main_history(state):
    """A blobless mirror of main in the state directory: commits and trees, enough to classify a range."""
    mirror = state / "deploy-source.git"
    if mirror.exists():
        # Only the mirror itself, a bare repository, is fetched into; anything else there is refused (a pause).
        if run(["git", "-C", str(mirror), "rev-parse", "--is-bare-repository", "--absolute-git-dir"],
               reason=True).splitlines() != ["true", str(mirror.resolve())]:
            raise Refused(f"{mirror} is not the bare source mirror; remove or repair it")
        run(["git", "-C", str(mirror), "fetch", "--quiet", "--filter=blob:none", "origin",
             "+refs/heads/main:refs/heads/main"], reason=True)
    else:
        run(["git", "clone", "--quiet", "--bare", "--filter=blob:none", "--single-branch", "--branch", "main",
             "https://github.com/" + REPOSITORY + ".git", str(mirror)], reason=True)
    return mirror


def on_main(mirror, ancestor, sha, env=None):
    try:
        run(["git", "-C", str(mirror), "merge-base", "--is-ancestor", ancestor, sha], env=env)
        return True
    except Refused:
        return False


def blob(path):
    content = Path(path).read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def pause_reasons(mirror, base, candidate, installed):
    """Why `base..candidate` needs a human: SQL inventory changes, and deploy tools that are not installed yet."""
    paths = [path for path in run(["git", "-C", str(mirror), "diff", "--name-only", "--no-renames", "-z", base,
                                   candidate]).split("\0") if path]
    reasons = []
    if sql := [path for path in paths if SQL_INVENTORY.search(path)]:
        reasons.append("the SQL inventory changed (" + ", ".join(sql[:5]) + "); migrate by hand (production.md)")
    tools = [path for path in paths if path in DEPLOY_TOOLS or path.startswith(tuple(t for t in DEPLOY_TOOLS if t[-1] == "/"))]
    def current_tool(path):
        # The running tool is the candidate's when its installed file has the candidate's content.
        if path in installed:
            return run(["git", "-C", str(mirror), "rev-parse", f"{candidate}:{path}"]).strip() == blob(installed[path])
        # The verifier is the candidate's when it matches the pin of the installed (the candidate's) production.py.
        return (path.startswith("tools/attest-verify/") and current_tool("scripts/production.py") and VERIFIER.exists()
                and hashlib.sha256(VERIFIER.read_bytes()).hexdigest() == VERIFIER_SHA256)
    if pending := [path for path in tools if not current_tool(path)]:
        reasons.append("deploy tools changed (" + ", ".join(pending[:5]) + "); install them by hand (production.md)")
    return reasons


def configuration_drift(settings, http, current, serving):
    """Why the configuration differs from the one the current releases were applied with, or None."""
    state = Path(settings["state_directory"])
    if read(state / current["sha"] / "prepared.json").get("config_digest") != digest(settings):
        return "the production configuration changed since the current release was prepared"
    after = {item["name"]: item["after_digest"] for item in current.get("rollback", {}).get("projects", [])}
    for project in settings["projects"]:
        if after.get(project["name"]) != digest(json.loads(run(compose(project) + ["config", "--format", "json"]))):
            return f"the Compose configuration of {project['name']} changed since the current release was applied"
    if serving.get("config_digest") != digest(http):
        return "the HTTP release configuration changed since the serving HTTP release was prepared"
    return None


def deploy(settings, http, *, auto=False, dry_run=False, attested=None):
    """Deploy the newest green main commit with plan A and then plan B, or pause with a reason (production.md)."""
    if PAUSE.exists():
        return {"status": "paused", "reason": PAUSE.read_text()[:1000].strip()}
    state = Path(settings["state_directory"])
    def halt(reason, status="halted", **fields):
        # The pause stops every later run until a human removes it; nothing is retried.
        outcome = {"status": status, "reason": reason, "at": now(), **fields}
        if dry_run:
            return {"status": "plan", "would_pause": reason, **fields}
        PAUSE.write_text(json.dumps(outcome) + "\n")
        PAUSE.chmod(0o600)
        return outcome
    s = http_release_tool()
    with ExitStack() as held:
        try:
            held.enter_context(s.locks(http))
            if state.resolve() not in (s.GLOBAL_LOCK.resolve(), Path(http["state_directory"]).resolve()):
                held.enter_context(lock(state))
        except Busy as exc:
            return {"status": "busy", "reason": str(exc)}
        except Exception as exc:
            return halt("deploy could not take the host locks: " + (str(exc) if isinstance(exc, Refused)
                                                                     else type(exc).__name__))
        # A candidate is taken only when every image plan A or plan B pulls is attested, so publication that is still
        # running or failed for one target never starts a release that stops halfway.
        targets = sorted(set(published_targets(settings)) | set(published_targets(http)) | {PROOF_TARGET})
        return deploy_locked(settings, http, s, auto=auto, dry_run=dry_run, halt=halt, attested=attested or (
            lambda sha: [published(target, sha) for target in targets]))


def deploy_locked(settings, http, s, *, auto, dry_run, attested, halt):
    state = Path(settings["state_directory"])
    record = {}
    try:
        return deploy_steps(settings, http, s, auto=auto, dry_run=dry_run, attested=attested, halt=halt, record=record)
    except Exception as exc:
        # Every failure the steps do not classify themselves ends here: a pause, unless nothing changed yet
        # and it was the network.
        message = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        if record:
            outcome = halt(f"deploy of {record['sha']} stopped in an unknown state: {message}; inspect it")
            record.update(outcome, finished_at=now())
            write(state / "deploy.json", record)
            return record
        if transient(exc):
            return {"status": "error", "reason": message + "; nothing changed, the next run retries"}
        return halt("deploy was refused before any change: " + message)


def serving_settled(receipt):
    """The serving HTTP release has no predecessor left to retire: retired, or legacy instances that stay retained."""
    return receipt.get("status") == "serving" or (receipt.get("status") == "serving-retirement-pending"
                                                 and receipt.get("previous") is None)


def deploy_steps(settings, http, s, *, auto, dry_run, attested, halt, record):
    state, http_state = Path(settings["state_directory"]), Path(http["state_directory"])
    try:
        last = read(state / "deploy.json") if (state / "deploy.json").exists() else {}
        started = last.get("started_at") and datetime.strptime(last["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        failures = last.get("fetch_failures", {"count": 0})["count"]
        misses = last.get("unreachable", {"count": 0})["count"]
        if type(failures) is not int or failures < 0 or type(misses) is not int or misses < 0:
            raise ValueError("fetch failure count")
    except (ValueError, TypeError, AttributeError, KeyError):
        return halt(f"the previous deploy receipt {state / 'deploy.json'} is malformed; inspect it")
    if auto and started and (datetime.now(timezone.utc) - started).total_seconds() < DEPLOY_INTERVAL:
        return {"status": "waiting", "reason": "less than 20 minutes since the last deploy started"}
    if blocked := unfinished(state):
        return halt("an apply or rollback is unfinished: " + ", ".join(blocked) + "; inspect it before any recovery")
    # An HTTP attempt that neither serves nor failed before admission has an unknown router or retirement outcome.
    if unresolved := sorted(path.parent.name for path in http_state.glob("*/http-attempted.json")
                            if read(path).get("status") not in ("serving", "startup-failed")):
        return halt("an HTTP release attempt is unresolved: " + ", ".join(unresolved)
                    + "; inspect seamless_release.py status --sha <sha>")
    current = read(state / "current.json") if (state / "current.json").exists() else {}
    serving = read(http_state / "http-current.json") if (http_state / "http-current.json").exists() else {}
    if current.get("status") != "applied" or not re.fullmatch(r"[0-9a-f]{40}", current.get("sha", "")):
        return halt(f"the current release is {current.get('status', 'missing')}, not applied; apply one by hand first")
    base = current["sha"]
    if serving.get("status") == "serving-retirement-pending" and not serving_settled(serving):
        # Retiring the managed predecessor HTTP pair is promote's last step, safe to repeat; it never cuts a request
        # short. It changes the router (the admission fence), so its failure pauses.
        if dry_run:
            return {"status": "plan", "sha": serving.get("sha"), "steps": ["retire the previous HTTP generation"]}
        try:
            s.retire(http, serving["sha"])
        except Exception as exc:
            message = str(exc) if isinstance(exc, Refused) else type(exc).__name__
            return halt(f"retiring the HTTP generation before {serving['sha']} failed: {message}; "
                        "inspect seamless_release.py status --sha " + serving["sha"])
        serving = read(http_state / "http-current.json")
        if not serving_settled(serving):
            return {"status": "waiting", "reason": "the previous HTTP generation is still draining"}
    if not serving_settled(serving) or not re.fullmatch(r"[0-9a-f]{40}", serving.get("sha", "")):
        return halt(f"the HTTP release is {serving.get('status', 'missing')}; inspect seamless_release.py status")
    try:
        # Every failure here precedes any change: this run does nothing, and the third in a row pauses.
        head = remote_main()
        mirror = None if head == base == serving["sha"] else main_history(state)
    except Exception as exc:
        message, failures = (str(exc) if isinstance(exc, Refused) else type(exc).__name__), failures + 1
        rest = {key: value for key, value in last.items() if key != "fetch_failures"}
        if failures >= FETCH_FAILURES:
            if not dry_run:
                write(state / "deploy.json", rest)
            return halt(f"git could not read main {failures} times in a row, last: {message}; "
                        f"inspect the network and {state / 'deploy-source.git'}")
        if not dry_run:
            write(state / "deploy.json", {**rest, "fetch_failures": {"count": failures, "at": now(), "reason": message}})
        return {"status": "fetch_failed", "reason": message, "failures": failures}
    if "fetch_failures" in last and not dry_run:
        write(state / "deploy.json", {key: value for key, value in last.items() if key != "fetch_failures"})
    if mirror is None:
        return {"status": "current", "sha": base}
    head = run(["git", "-C", str(mirror), "rev-parse", "refs/heads/main"]).strip()
    if not on_main(mirror, base, head) or not on_main(mirror, serving["sha"], base):
        return halt("the current release is not on main, or the serving HTTP release is not an ancestor of it")
    # promote marks an attempt `serving` before it publishes http-current.json: a serving attempt the serving release
    # does not contain is an interrupted publication, and the router may already admit that generation.
    if unpublished := sorted(path.parent.name for path in http_state.glob("*/http-attempted.json")
                             if read(path).get("status") == "serving" and not (
                                 re.fullmatch(r"[0-9a-f]{40}", path.parent.name)
                                 and on_main(mirror, path.parent.name, serving["sha"]))):
        return halt("an HTTP release attempt serves, but http-current.json does not name it: " + ", ".join(unpublished)
                    + "; inspect seamless_release.py status --sha <sha>")
    if drift := configuration_drift(settings, http, current, serving):
        return halt(drift + "; apply the next release by hand")
    installed = {"scripts/production.py": __file__, "scripts/seamless_release.py": s.__file__}
    if serving["sha"] != base:
        # Plan A is ahead of the HTTP pair: a deploy whose HTTP release stopped before any change, or plan A applied
        # by hand.
        candidate = base
        reasons = pause_reasons(mirror, serving["sha"], base, installed)
    else:
        candidate = None
        for sha in run(["git", "-C", str(mirror), "rev-list", "--first-parent", f"{base}..{head}"]).split():
            # A commit whose deployment was attempted is never retried, and older ones never follow it.
            if (state / sha / "attempted.json").exists() or (http_state / sha / "http-attempted.json").exists():
                break
            # Green CI is the attestation `publish` signs after `CI gate` (promotable); no GitHub API request.
            try:
                attested(sha)
            except Unattested:
                continue  # not published (yet): its CI still runs or failed, and an older commit may be deployed
            except Unreachable as exc:
                # GHCR could not be read: no older commit is taken instead; nothing changed.
                if misses + 1 >= FETCH_FAILURES:
                    return halt(f"GHCR or git could not be read {misses + 1} times in a row, last: {exc}")
                if not dry_run:
                    write(state / "deploy.json", {**{key: value for key, value in last.items() if key != "unreachable"},
                                                  "unreachable": {"count": misses + 1, "at": now(), "reason": str(exc)}})
                return {"status": "error", "reason": f"{exc}; nothing changed, the next run retries"}
            candidate = sha
            break
        if candidate is None:
            if "unreachable" in last and not dry_run:
                write(state / "deploy.json", {key: value for key, value in last.items() if key != "unreachable"})
            return {"status": "waiting", "reason": "no newer main commit has attested images yet", "sha": base}
        reasons = pause_reasons(mirror, base, candidate, installed)
    if reasons:
        return halt("; ".join(reasons), sha=candidate)
    if dry_run:
        return {"status": "plan", "sha": candidate, "base": base,
                "steps": (["plan A"] if candidate != base else []) + ["plan B"]}
    record.update(sha=candidate, base=base, started_at=now(), status="deploying")
    write(state / "deploy.json", record)
    def finish(status, **fields):
        record.update(status=status, finished_at=now(), **fields)
        write(state / "deploy.json", record)
        return record
    def retry(exc, message):
        # A network error changed nothing; git or GHCR failing to be read the third run in a row pauses.
        if isinstance(exc, Unreachable):
            if misses + 1 >= FETCH_FAILURES:
                return finish(**halt(f"GHCR or git could not be read {misses + 1} times in a row, last: {message}"))
            record["unreachable"] = {"count": misses + 1, "at": now(), "reason": message}
        return finish("error", reason=message + "; nothing changed, the next run retries")
    release = state / candidate
    if candidate != base:
        try:
            if not (release / "prepared.json").exists():
                if release.exists():
                    return finish(**halt(f"an earlier preparation of {candidate} did not finish; inspect {release}"))
                prepare(settings, candidate)
            apply(settings, candidate)
        except Exception as exc:
            message = str(exc) if isinstance(exc, Refused) else type(exc).__name__
            if not (release / "attempted.json").exists():
                # A network or quota error before preparation started or after it finished changed nothing.
                if transient(exc) and ((release / "prepared.json").exists() or not release.exists()):
                    return retry(exc, message)
                return finish(**halt(f"plan A of {candidate} was refused before any change: {message}"))
            result = read(release / "result.json") if (release / "result.json").exists() else {}
            if result.get("status") != "unhealthy":
                return finish(**halt(f"plan A of {candidate} stopped in an unknown state: {message}; inspect it"))
            # Automatic deploys never change the SQL inventory (pause_reasons), so the previous release still fits.
            try:
                rollback(settings, candidate)
            except Exception as again:
                detail = str(again) if isinstance(again, Refused) else type(again).__name__
                return finish(**halt(f"plan A of {candidate} failed its health wait and its rollback failed: {detail}; "
                                     "production state is unknown, inspect it"))
            return finish(**halt(f"plan A of {candidate} failed its health wait; rolled back to {base}",
                                 status="rolled-back"))
    try:
        if not (http_state / candidate / "http-prepared.json").exists():
            if (http_state / candidate).exists():
                return finish(**halt(f"an earlier HTTP preparation of {candidate} did not finish; inspect it"))
            s.prepare(http, candidate)
        served = s.promote(http, candidate)
    except Exception as exc:
        message = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        directory = http_state / candidate
        if transient(exc) and not (directory / "http-attempted.json").exists() and (
                (directory / "http-prepared.json").exists() or not directory.exists()):
            return retry(exc, message)
        return finish(**halt(f"plan A of {candidate} is applied, but its HTTP release stopped: {message}; "
                             "inspect seamless_release.py status --sha " + candidate))
    outcome = finish("deployed", http=served.get("status"))
    # Recorded as deployed first: removing old release images never changes that outcome, nor does adding its summary.
    outcome["image_removal"] = retain_images(settings, held=(http_state, s.GLOBAL_LOCK))
    try:
        write(state / "deploy.json", outcome)
    except Exception:
        outcome["image_removal"]["receipt"] = "not written; deploy.json keeps the deployed receipt without this summary"
    return outcome


def transient(exc):
    """A network error, git failing to read main, a GitHub server error or an exhausted API quota says nothing about
    the candidate: the next run retries it. Anything else (a refusal, a malformed receipt, a program error) pauses."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    return isinstance(exc, (RateLimited, Unreachable, urllib.error.URLError, TimeoutError, ConnectionError))


def http_release_tool():
    """seamless_release.py next to this file, sharing this module (it imports `production`)."""
    if __name__ == "__main__":
        sys.modules.setdefault("production", sys.modules[__name__])
    try:
        from scripts import seamless_release
    except ImportError:
        import seamless_release
    return seamless_release


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["verify", "prepare", "prepare-router", "apply", "rollback", "status",
                                           "images", "check-image", "deploy"])
    parser.add_argument("--sha")
    parser.add_argument("--target", choices=sorted(BUILDS))
    parser.add_argument("--config", default="/etc/deedkit/production.json")
    parser.add_argument("--http-config", default="/etc/deedkit/http-production.json", help="deploy: seamless_release.py's")
    parser.add_argument("--auto", action="store_true", help="deploy: at most one deploy start per 20 minutes (timer)")
    parser.add_argument("--dry-run", action="store_true", help="deploy: print the plan or pause reason, change nothing")
    parser.add_argument("--remove", action="store_true", help="images: remove the listed candidates")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.action == "check-image":
            if not args.target:
                raise Refused("select a target for container verification")
            source = Path(__file__).resolve().parents[1]
            sha = run(["git", "-C", str(source), "rev-parse", "HEAD"]).strip()
            result = {"sha": sha, "image": build_image(source, sha, args.target, check=True)}
        elif args.action == "verify":
            result = verify(args.sha or "")
        elif args.action == "deploy":
            if os.geteuid() != 0:
                raise Refused("deploy requires the production host operator")
            result = deploy(config(args.config), http_release_tool().validate_settings(config(args.http_config)),
                            auto=args.auto, dry_run=args.dry_run)
            # One line per run for the journal; a new pause, an error or a failed fetch exits 1, so the timer unit shows failed.
            print(json.dumps(result, sort_keys=True))
            if result["status"] in ("halted", "rolled-back", "error", "fetch_failed"):
                parser.exit(1)
            return
        else:
            settings = config(args.config)
            state = Path(settings["state_directory"])
            with ExitStack() as locks:
                if args.action == "prepare-router":
                    if os.geteuid() != 0:
                        raise Refused("router preparation requires the production host operator")
                    canonical = Path("/var/lib/deedkit-production")
                    locks.enter_context(lock(canonical))
                    if state.resolve() != canonical.resolve():
                        locks.enter_context(lock(state))
                else:
                    locks.enter_context(lock(state))
                if args.action == "status":
                    result = read(state / "current.json") if (state / "current.json").exists() else {"status": "not promoted by this tool"}
                    if (state / "deploy.json").exists():
                        result["deploy"] = read(state / "deploy.json")
                    if PAUSE.exists():
                        result["autodeploy_pause"] = PAUSE.read_text()[:1000].strip()
                elif args.action == "images":
                    result = remove_images(settings) if args.remove else images_report(settings)
                else:
                    if not re.fullmatch(r"[0-9a-f]{40}", args.sha or ""):
                        raise Refused("an exact lowercase 40-character commit SHA is required")
                    result = {"prepare": prepare, "prepare-router": prepare_router, "apply": apply,
                              "rollback": rollback}[args.action](settings, args.sha)
                    if args.action == "apply":
                        # Printed only: result.json and current.json stay the release receipts.
                        result = {**result, "image_removal": retain_images(settings)}
        # Receipts contain revisions/image IDs, never resolved Compose environments.
        if args.action == "prepare":
            result = {key: result[key] for key in ("sha", "ci", "images", "references")}
        print(json.dumps(result, indent=2))
    except Exception as exc:
        message = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        parser.exit(1, "promotion refused: " + message + "\n")


if __name__ == "__main__":
    main()
