#!/usr/bin/env python3
"""Verified, explicit production promotion. No provider credentials or migrations."""
# ruff: noqa: E402
from __future__ import annotations

import os
import sys

# `self-test` (ANYM-443, production.md "Deploy tool updates") checks a tool before `deploy` switches to it. It never
# reaches the network, starts a process, takes a lock or writes: this guard is installed before any other import.
GUARDED = __name__ == "__main__" and sys.argv[1:2] == ["self-test"]
if GUARDED:
    def refuse_side_effects(event, args, *, refused=frozenset((
            "socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo",
            "socket.gethostbyname", "socket.gethostbyaddr", "subprocess.Popen", "os.system", "os.exec",
            "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty", "os.kill", "os.killpg", "fcntl.flock",
            "fcntl.lockf", "os.mkdir", "os.rename", "os.remove", "os.rmdir", "os.chmod", "os.chown", "os.symlink",
            "os.link", "os.truncate", "os.utime", "os.mkfifo", "os.mknod", "shutil.rmtree", "shutil.copyfile",
            "shutil.move", "ctypes.dlopen")),
            writing=os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
        if event in refused or event == "open" and isinstance(args[2], int) and args[2] & writing:
            raise PermissionError(f"self-test refused {event}")
    sys.addaudithook(refuse_side_effects)

import argparse
from contextlib import ExitStack, contextmanager, suppress
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import posixpath
import re
import resource
import selectors
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
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


class CallActive(Refused):
    """A call was still active when the bounded wait before a plan A stop ran out: `deploy` tries again later."""



def run(argv, *, cwd=None, umask=-1, env=None, reason=False, timeout=1800, binary=False):
    # Never include command output in an exception: Compose can contain secrets. Only a tool that prints
    # nothing secret (the attestation verifier, git on the public source mirror) may pass its last stderr line on,
    # with `reason`.
    result = subprocess.run(argv, cwd=cwd, text=not binary, capture_output=True, timeout=timeout, umask=umask, env=env)
    if result.returncode:
        stderr = result.stderr.decode(errors="replace") if binary else result.stderr
        detail = (stderr.strip().splitlines() or [""])[-1][:300] if reason else ""
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


def check_verifier():
    try:
        info = VERIFIER.lstat()
        content = VERIFIER.read_bytes() if stat.S_ISREG(info.st_mode) else b""
    except OSError:
        info, content = None, b""
    if (info is None or not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
            or info.st_mode & 0o022 or hashlib.sha256(content).hexdigest() != VERIFIER_SHA256):
        raise Refused(f"the attestation verifier {VERIFIER} is missing, writable by others or not the pinned build")


def published(target, sha):
    """Resolve `ghcr.io/gosync-inc/deedkit-<target>:<sha>` to one digest and verify its attestation, tokenless.

    The pinned verifier (tools/attest-verify) runs with an empty environment, so no GitHub or registry
    credential can reach it; any refusal stops the target before a pull (ANYM-301 B2).
    """
    check_verifier()
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
    state = Path(settings["state_directory"])
    # Local headroom first: a full disk refuses before the verification fetches anything.
    check_space(state)
    docker_root = run(["docker", "info", "--format", "{{.DockerRootDir}}"]).strip()
    if not docker_root or not Path(docker_root).is_dir():
        raise Refused("preparation requires the local production Docker engine")
    check_image_space(docker_root)
    promotable(sha)
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
    state = Path(settings["state_directory"])
    # Local headroom first: a full disk refuses before the verification fetches anything.
    check_space(state)
    docker_root = run(["docker", "info", "--format", "{{.DockerRootDir}}"]).strip()
    if not docker_root or not Path(docker_root).is_dir():
        raise Refused("preparation requires the local production Docker engine")
    check_image_space(docker_root)
    evidence = promotable(sha)
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
               "config_digest": settings_digest(settings), "current_digest": digest(current), "projects": projects}
    write(release / "prepared.json", receipt)
    return receipt


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# The host backup runner (BACKUP.md) is no image and no deploy tool: an operator installs it together with the backup
# image its runner.json pins. Plan A refuses while the installed file is not the release's, so a merged runner fix never
# stays off the host unnoticed. A host without the file runs no backup and is not checked.
BACKUP_RUNNER = Path("/opt/personal-os-probe/ops/backup_runner.py")
BACKUP_RUNNER_SOURCE = "services/messaging/messaging_service/backup_runner.py"


def check_backup_runner(source, sha):
    if not os.path.lexists(BACKUP_RUNNER):
        return
    expected = run(["git", "-C", str(source), "rev-parse", f"{sha}:{BACKUP_RUNNER_SOURCE}"]).strip()
    try:
        # The file itself, never a link to another one.
        fd = os.open(BACKUP_RUNNER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise Refused(f"the backup runner {BACKUP_RUNNER} is not a regular file")
            content = handle.read()
        installed = hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()
    except OSError as exc:
        raise Refused(f"cannot read the backup runner {BACKUP_RUNNER}: {type(exc).__name__}") from exc
    if installed != expected:
        raise Refused(f"the backup runner {BACKUP_RUNNER} is blob {installed[:12]}, not {sha[:12]}'s {expected[:12]}; "
                      "install it with its backup image (production.md, Backup runner)")


def apply(settings, sha):
    promotable(sha)
    state = Path(settings["state_directory"])
    release = state / sha
    receipt = read(release / "prepared.json")
    if receipt["sha"] != sha or receipt["config_digest"] != settings_digest(settings):
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
    check_backup_runner(release / "source", sha)
    # No call is active before the last remote gate, nor after it: a call that started meanwhile is waited for before
    # anything is written, within the same bound.
    deadline = time.monotonic() + CALL_WAIT
    wait_for_calls(settings, deadline)
    # Last remote gate immediately before the first mutation, under the host lock.
    evidence = promotable(sha)
    wait_for_calls(settings, deadline)
    write(release / "attempted.json", {"sha": sha, "ci": evidence, "status": "applying"})
    started, touched = {}, []
    for project, item in zip(settings["projects"], receipt["projects"], strict=True):
        command = ["docker", "compose", "--profile", "*", "--project-name", project["name"], "-f", item["file"]]
        services = list(project["services"])
        if touched:
            wait_for_calls(settings, time.monotonic() + CALL_WAIT)
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


# A plan A stop never cuts a call (production.md "Apply and verify"): restarting the calls service ends every unfinished
# call as `interrupted`, and the Telegram collector carries Telegram call audio. ACTIVE of services/calls/core.py.
CALL_STATUSES = ("waiting_for_browser", "dialing", "ringing", "connecting", "active")
# A call lasts at most 600 s (core.py max_duration_secs), Twilio 40 s more; a browser call can wait for its human
# longer, so the bound is a budget: when it runs out, nothing is stopped.
CALL_WAIT, CALL_POLL, CALL_WAITS = 15 * 60, 10, 4
# One read stops after CALL_READ seconds; the probe process dies after CALL_LIFETIME whatever SQLite does, which
# releases its shared lock (the service's own writes wait for it). One poll's Docker commands share CALL_PROBE
# seconds; confirming that a probe the tool gave up on is gone gets CALL_KILL seconds per command. The host reads at
# most CALL_OUTPUT bytes of a probe's output.
CALL_READ, CALL_LIFETIME, CALL_PROBE, CALL_KILL, CALL_OUTPUT = 0.25, 1, 20, 10, 65536
CALL_LABEL = "io.deedkit.calls-probe"
CALL_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
# Run in a container of its own from the calls container's image: the calls state directory mounted read-only, no
# network, its own memory and process limits, as the calls user. This host's root never opens a file there, the live
# calls container shares none of its limits, and nothing can be written there (no journal or WAL file). `--init`
# keeps python off PID 1, which the kernel would shield from SIGALRM's default action.
CALLS_PROBE = r"""
import json, os, pathlib, signal, sqlite3, sys, time
def probe(limit, statuses):
    directory = os.environ.get("VOICE_DATA_DIR")
    if not directory:
        return {"error": "no_data_directory"}
    database = pathlib.Path(directory, "voice.sqlite3")
    if not database.is_file():
        return {"error": "no_database"}
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=limit)
        try:
            deadline = time.monotonic() + limit
            connection.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
            rows = connection.execute(
                "SELECT id, json_extract(body, '$.status') FROM calls WHERE json_extract(body, '$.status') IN ("
                + ", ".join("?" * len(statuses)) + ") ORDER BY id LIMIT 20", statuses).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        name = getattr(exc, "sqlite_errorname", "")
        return {"error": next((category for prefix, category in (
            ("SQLITE_BUSY", "busy"), ("SQLITE_LOCKED", "busy"), ("SQLITE_INTERRUPT", "interrupted"),
            ("SQLITE_READONLY", "read_only_recovery_needed"), ("SQLITE_CANTOPEN", "cannot_open"))
            if name.startswith(prefix)), "no_calls_table" if "no such table" in str(exc) else "sqlite_error")}
    return {"calls": [[str(identity), str(status)] for identity, status in rows]}
# The process ends here at the latest, even inside one SQLite step: SIGALRM's default action terminates it.
signal.alarm(int(sys.argv[2]))
print(json.dumps(probe(float(sys.argv[1]), sys.argv[3:])))
"""
CALL_ERRORS = {"no_data_directory", "no_database", "busy", "interrupted", "read_only_recovery_needed", "cannot_open",
               "no_calls_table", "sqlite_error"}


class CallsUnknown(Refused):
    """Whether a call is active could not be read (a fixed category, never raw output)."""


class ProbeStuck(Refused):
    """A calls probe the tool gave up on could not be confirmed gone: no further probe or stop, inspect it."""


# Tokens of probes whose `docker create` or `docker start` did not end cleanly: Docker can still act on a request
# after the tool gave up, so each later probe first confirms that none of them can run.
UNCONFIRMED = set()


def docker_probe(argv, until):
    """A Docker command of one poll, never past the poll's own budget `until` (time.monotonic)."""
    remaining = until - time.monotonic()
    if remaining <= 0:
        raise CallsUnknown("probe_timeout")
    try:
        return run(argv, timeout=remaining)
    except subprocess.TimeoutExpired:
        raise CallsUnknown("probe_timeout") from None
    except Refused:
        raise CallsUnknown("probe_failed") from None


def probe_output(argv, until):
    """The stdout of a probe's `docker start --attach`, never past `until` and never more than CALL_OUTPUT bytes held
    (more is malformed); stderr is discarded. A failed probe is probe_failed."""
    remaining = until - time.monotonic()
    if remaining <= 0:
        raise CallsUnknown("probe_timeout")
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    stdout, output = process.stdout, b""
    assert stdout is not None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(stdout, selectors.EVENT_READ)
            while True:
                remaining = until - time.monotonic()
                if remaining <= 0:
                    raise CallsUnknown("probe_timeout")
                if selector.select(remaining):
                    if not (chunk := os.read(stdout.fileno(), CALL_OUTPUT + 1)):
                        break
                    output += chunk
                    if len(output) > CALL_OUTPUT:
                        raise CallsUnknown("malformed_probe_output")
        if process.wait(timeout=max(until - time.monotonic(), 0.001)):
            raise CallsUnknown("probe_failed")
    except subprocess.TimeoutExpired:
        raise CallsUnknown("probe_timeout") from None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        stdout.close()
    try:
        return output.decode()
    except UnicodeDecodeError:
        raise CallsUnknown("malformed_probe_output") from None


def stop_probes():
    """Remove every probe container of UNCONFIRMED in any state, found by its own label value (never by name: a
    failed `docker create --name` may have met another container), and confirm none is left that could still run. A
    removed container never starts, so a `docker start` the tool gave up on cannot run it later; one whose creation
    the tool gave up on and that Docker creates later is never started. Otherwise ProbeStuck."""
    def owned():
        rows = [line.split("\t") for line in run([
            "docker", "ps", "--all", "--no-trunc", "--filter", "label=" + CALL_LABEL, "--format",
            '{{.ID}}\t{{.State}}\t{{.Label "' + CALL_LABEL + '"}}'], timeout=CALL_KILL).splitlines() if line]
        if not all(len(row) == 3 and CONTAINER_ID.fullmatch(row[0]) for row in rows):
            raise Refused("unexpected probe listing")  # never taken for "none left"
        # A container Docker is removing, or failed to remove, never starts again.
        return [identity for identity, status, token in rows if token in UNCONFIRMED and status not in (
            "removing", "dead")]
    try:
        for container in owned():
            with suppress(Refused):  # It may have ended and been removed meanwhile.
                run(["docker", "rm", "--force", container], timeout=CALL_KILL)
        left = owned()
    except (Refused, subprocess.TimeoutExpired):
        left = None
    if left != []:
        raise ProbeStuck("a calls probe container could not be confirmed stopped; "
                         f"inspect `docker ps --all --filter label={CALL_LABEL}`")


def calls_in(container, until):
    """`<id> (<status>)` of every active call the calls container's database lists, read by CALLS_PROBE in a
    container of its own."""
    if UNCONFIRMED:
        stop_probes()
    actual = json.loads(docker_probe(["docker", "inspect", container], until))[0]
    env = dict(item.partition("=")[::2] for item in actual["Config"].get("Env") or [])
    directory = env.get("VOICE_DATA_DIR", "/data")
    mounts = [mount for mount in actual.get("Mounts") or [] if mount.get("Destination") == directory]
    if len(mounts) != 1 or mounts[0].get("Type") not in ("bind", "volume") or not IMAGE_DIGEST.fullmatch(
            actual.get("Image", "")):
        raise CallsUnknown("no_data_mount")
    mount = mounts[0]
    source = ("type=bind,source=" + mount["Source"] if mount["Type"] == "bind" else "type=volume,source=" + mount["Name"])
    if "," in source.partition("source=")[2]:
        raise CallsUnknown("no_data_mount")
    token = uuid.uuid4().hex
    # Created first, so the container that may run has a known ID before it starts; a creation or start that did not
    # end cleanly leaves the token to stop_probes, here and before every later probe.
    UNCONFIRMED.add(token)
    try:
        container = docker_probe([
            "docker", "create", "--rm", "--init", "--name", "deedkit-calls-probe-" + token[:12], "--label",
            f"{CALL_LABEL}={token}", "--pull", "never", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "64m", "--memory-swap", "64m",
            "--pids-limit", "16", "--cpus", "0.5", "--user", actual["Config"].get("User") or "10001:10001",
            "--mount", source + ",target=/data,readonly", "--env", "VOICE_DATA_DIR=/data", "--entrypoint", "python",
            actual["Image"], "-I", "-c", CALLS_PROBE, str(CALL_READ), str(CALL_LIFETIME), *CALL_STATUSES], until).strip()
        if not CONTAINER_ID.fullmatch(container):
            raise CallsUnknown("probe_failed")
        output = probe_output(["docker", "start", "--attach", container], until)
    except CallsUnknown:
        # The tool gave up or failed; its probe container must not outlive it (its alarm ends it anyway).
        stop_probes()
        raise
    # It ran to its end, and `--rm` removes it.
    UNCONFIRMED.discard(token)
    try:
        answer = json.loads(output)
    except ValueError:
        raise CallsUnknown("malformed_probe_output") from None
    # Exactly the shape CALLS_PROBE prints: nothing else is passed on or taken for "no call".
    if not isinstance(answer, dict) or len(answer) != 1:
        raise CallsUnknown("malformed_probe_output")
    if "error" in answer:
        raise CallsUnknown(answer["error"] if isinstance(answer["error"], str) and answer["error"] in CALL_ERRORS
                           else "malformed_probe_output")
    calls = answer.get("calls")
    if not isinstance(calls, list) or len(calls) > 20 or not all(
            isinstance(call, list) and len(call) == 2 and isinstance(call[0], str) and CALL_ID.fullmatch(call[0])
            and call[1] in CALL_STATUSES for call in calls):
        raise CallsUnknown("malformed_probe_output")
    return [f"{identity} ({status})" for identity, status in calls]


def active_calls(settings):
    """The active calls of every running container of a configured `calls` service, found by its Compose labels,
    in one poll of at most CALL_PROBE seconds. No running calls container has no active call; a configuration
    without a `calls` target checks nothing."""
    found, until = [], time.monotonic() + CALL_PROBE
    for project in settings["projects"]:
        for service in (service for service, target in project["services"].items() if target == "calls"):
            for container in docker_probe(["docker", "ps", "-q", "--filter",
                                           "label=com.docker.compose.project=" + project["name"], "--filter",
                                           "label=com.docker.compose.service=" + service], until).split():
                found += calls_in(container, until)
    return found


def wait_for_calls(settings, deadline):
    """Wait until no call is active; no poll starts at or after `deadline` (time.monotonic), and one poll takes at
    most CALL_PROBE plus confirming probes the tool gave up on gone. At the bound: CallActive with the calls, or
    CallsUnknown with the category when the last read failed, `wait_expired` when the bound passed before this wait
    could poll; it never proceeds on doubt. ProbeStuck ends it at once."""
    said, calls, error = False, None, "wait_expired"
    while time.monotonic() < deadline:
        try:
            calls, error = active_calls(settings), None
        except CallsUnknown as exc:
            calls, error = None, str(exc)
        if calls == []:
            if not said:
                print("calls check: no active call", file=sys.stderr, flush=True)
            return
        if not said:
            print("calls check: waiting for " + ("the calls database (" + error + ")" if error is not None else
                                                 "a call to end: " + ", ".join(calls or [])), file=sys.stderr, flush=True)
            said = True
        time.sleep(max(0, min(CALL_POLL, deadline - time.monotonic())))
    if error is not None:
        raise CallsUnknown("whether a call is active could not be read: " + error)
    raise CallActive("a call is active: " + ", ".join(calls or []))


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
    if prepared["config_digest"] != settings_digest(settings):
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
    wait_for_calls(settings, time.monotonic() + CALL_WAIT)
    write(release / "rollback-attempted.json", {"sha": sha, "status": "rolling-back", "started_at": now()})
    for index, (project, before, previous) in enumerate(plan):
        command = ["docker", "compose", "--profile", "*", "--project-name", project["name"], "-f", str(before)]
        services = list(project["services"])
        if index:
            wait_for_calls(settings, time.monotonic() + CALL_WAIT)
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


# Unlike Path.glob and Path.exists, which take an unreadable directory for an empty or absent one, these refuse: a
# protecting receipt they cannot see would otherwise leave its images unprotected.
def present(path):
    """The stat of `path`, or None when it does not exist."""
    try:
        return os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise Refused(f"cannot read {path}; no image is removed") from exc


def directory_entries(directory):
    """The entries of `directory`, none when it does not exist."""
    try:
        return sorted(Path(directory).iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        raise Refused(f"cannot list {directory}; no image is removed") from exc


def subdirectories(directory):
    return [path for path in directory_entries(directory) if (found := present(path)) and stat.S_ISDIR(found.st_mode)]


def json_files(directory):
    return [path for path in directory_entries(directory) if path.suffix == ".json"
            and (found := present(path)) and stat.S_ISREG(found.st_mode)]


def retention_states(state, *, removing=False):
    """The production states on this host. For a removal, a configured state directory that does not exist yet refuses:
    a first preparation there would take its deploy lock only after the removal took the image lock."""
    configured = [Path(value["state_directory"]) for value in map(load, json_files(CONFIG_DIRECTORY))
                  if isinstance(value, dict) and isinstance(value.get("state_directory"), str)]
    found = [path for path in subdirectories(HOST_STATE)
             if present(path / "current.json") or present(path / "http-current.json")]
    states, seen = [], set()
    for directory in [state, HOST_STATE, *found, *configured]:
        exists = (found := present(directory)) and stat.S_ISDIR(found.st_mode)
        if removing and not exists and directory in configured:
            raise Refused(f"the configured state directory {directory} does not exist; no image is removed")
        if exists and directory.resolve() not in seen:
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


def rollback_snapshots(directory, current):
    """The services of each tool-written `<project>.before.json` of the current applied release, one per project its
    rollback record names (a receipt without that record: per project of its prepared.json). Each must be there with
    its `services`, or the rollback target is unknown. Other files there, such as an operator's copy of another
    configuration, are not rollback targets."""
    release = directory / current["sha"]
    entries = {path.name: path for path in directory_entries(release)}
    projects = (current.get("rollback") or {}).get("projects")
    if not isinstance(projects, list) and "prepared.json" in entries:
        prepared = load(entries["prepared.json"])
        projects = prepared.get("projects") if isinstance(prepared, dict) else None
    names = [project.get("name") if isinstance(project, dict) else None
             for project in (projects if isinstance(projects, list) else [])]
    snapshots = [load(entries[f"{name}.before.json"]) if isinstance(name, str) and f"{name}.before.json" in entries
                 else None for name in names]
    # Every service entry, too: one that is not a mapping would leave its image unprotected (Astra r2 F6).
    if not snapshots or not all(isinstance(snapshot, dict) and isinstance(snapshot.get("services"), dict) and all(
            isinstance(unit, dict) for unit in snapshot["services"].values()) for snapshot in snapshots):
        raise Refused(f"the rollback snapshot of {release} is missing or malformed; no image is removed")
    return [snapshot["services"] for snapshot in snapshots if snapshot]


def state_images(directory, keep, revisions, *, label, idle):
    """The images one state still needs: its current release, the images a rollback restored, the current release's
    `<project>.before.json` (the rollback target), every preparation neither applied nor attempted, the HTTP serving and retained
    generations, and images an operator protected. Unless the state is `idle` (its deploy lock was free), the images
    tagged with the revision of any release directory without a result: a preparation may be pulling them now."""
    current = load(directory / "current.json") if present(directory / "current.json") else {}
    for image in current.get("images", {}).values():
        keep(image, ("rolled-back release " if current.get("status") == "rolled-back" else "current release ")
             + current["sha"] + label)
    for services in current.get("previous_images", {}).values():
        for image in services.values():
            keep(image, "restored by the rollback of " + current["sha"] + label)
    if current.get("status") == "applied":
        for services in rollback_snapshots(directory, current):
            for unit in services.values():
                keep(unit.get("image"), "rollback target of the current release" + label)
    if present(directory / "http-current.json"):
        # Read one level deep: each HTTP receipt nests its predecessor, and those before it are retired.
        serving = load(directory / "http-current.json")
        previous = serving.get("previous")
        serving = {**serving, "previous": {key: value for key, value in previous.items() if key != "previous"}
                   if isinstance(previous, dict) else previous}
        for image in IMAGE_DIGEST.findall(json.dumps(serving)):
            keep(image, f"HTTP release state {directory / 'http-current.json'}")
    for release in subdirectories(directory):
        attempted = present(release / "http-attempted.json")
        if present(release / "prepared.json") and not present(release / "result.json") and not attempted:
            for image in load(release / "prepared.json").get("images", {}).values():
                keep(image, f"prepared release {release.name}{label} is not applied")
        if present(release / "http-prepared.json") and not attempted:
            for image in load(release / "http-prepared.json").get("images", {}).values():
                keep(image, f"HTTP release {release.name}{label} is prepared, not attempted")
        if (not idle and RELEASE_DIRECTORY.fullmatch(release.name) and not present(release / "result.json")
                and not attempted):
            revisions.setdefault(release.name, f"release {release.name}{label} may be preparing now")
    for path in json_files(directory):
        if path.name not in ("current.json", "http-current.json", "deploy.json"):
            # Operator plans next to the receipts, such as a storage cleanup's protected images.
            plan = load(path)
            for image in plan.get("protected_images", []) if isinstance(plan, dict) else []:
                keep(image, f"protected by {path}")


def pinned_images(keep, states):
    """Image IDs named in a top-level JSON file of /etc/deedkit (the configurations and the jobs that pin an image, such
    as file-lifecycle.json) or of an operator directory under the host state root that is not a state."""
    states = {path.resolve() for path in states}
    directories = [CONFIG_DIRECTORY, *(path for path in subdirectories(HOST_STATE)
                                       if path.resolve() not in states and not RELEASE_DIRECTORY.fullmatch(path.name))]
    for directory in directories:
        for path in json_files(directory):
            for image in IMAGE_DIGEST.findall(json.dumps(load(path))):
                keep(image, f"named in {path}")


def release_name(name):
    """The CI-published target of a `deedkit-production-<target>:<tag>` name, or None."""
    repository = name.rpartition(":")[0]
    return next((target for target in PUBLISHED if repository == "deedkit-production-" + target), None)


def images_report(settings, *, held=(), stack=None):
    """Which `deedkit-production-<target>` images the retention rules keep, and why; `remove_images` removes the rest.
    `held` are the state directories whose deploy lock the caller holds; the others' locks are probed. A removal passes
    `stack` with every state's lock held already; a state outside them refuses."""
    state = Path(settings["state_directory"])
    reasons = {}
    def keep(image, why):
        if isinstance(image, str) and IMAGE_DIGEST.fullmatch(image):
            reasons.setdefault(image, []).append(why)
    states, revisions = retention_states(state), {}
    held = {Path(path).resolve() for path in (*held, state)}
    with ExitStack() as probes:
        for directory in states:
            if stack is not None and directory.resolve() not in held:
                # Never a deploy lock under the image lock: a preparation takes them in the other order.
                raise Refused(f"the production state {directory} appeared during the removal; no image is removed")
            idle = directory.resolve() in held or free_lock(directory, probes)
            state_images(directory, keep, revisions, label="" if directory == state else f" of {directory}", idle=idle)
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
    locks of `held` and this state. Like a preparation it takes every other state's deploy lock before the image lock,
    without waiting, and holds them to the end: while another operation holds one it removes nothing, so it never takes
    the image lock from a preparation in progress. IDs and Docker's reasons are reported short, so a receipt holding this
    summary never pins an image."""
    state = Path(settings["state_directory"])
    with ExitStack() as stack:
        mine = {Path(path).resolve() for path in (*held, state)}
        for directory in retention_states(state, removing=True):
            if directory.resolve() not in mine and not free_lock(directory, stack):
                raise Refused(f"another production operation holds the deploy lock of {directory}; no image is removed")
            mine.add(directory.resolve())
        stack.enter_context(image_lock())
        # The report refuses a state that appeared meanwhile instead of locking it.
        report = images_report(settings, held=mine, stack=stack)
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


# Database copies (ANYM-588, production.md "Database copies"): an exactly checked copy of a live database on this host,
# restored in a capped scratch PostgreSQL. No helper's output is kept or printed (stderr is discarded): a failure is one
# fixed code. `<id>/owner.json` names every resource before it starts; only `released.json` finishes them.
COPIES = "migration-copies"
COPY_RETENTION = 48 * 3600
COPY_POLL = 2
COPY_RESERVE = 10 << 30
COPY_TIMEOUT = 1800
COPY_STOP_READBACK = 30
COPY_USER = "deedkit_copy"
COPY_DATA = "/deedkit-copy"
COPY_CODES = ("dump_failed", "restore_failed", "count_mismatch", "schema_mismatch", "disk_reserve", "deadline")
COPY_NAME = re.compile(r"[a-z][a-z0-9]{0,11}")
COPY_ID = re.compile(r"[0-9a-f]{12}-[a-z][a-z0-9]{0,11}-([0-9]{8}T[0-9]{6}Z)")
SQL_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
DOCKER_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")
SNAPSHOT_ID = re.compile(r"[0-9A-F]+-[0-9A-F]+-[0-9]+")
LOCALE = re.compile(r"[A-Za-z0-9_.@-]{1,64}")
# Every table outside the system schemas, one JSON line each: the same text counts the live snapshot and the restore.
COUNTS = r"""SELECT format('SELECT json_build_object(''table'', %L, ''rows'', count(*)) FROM %s', t, t)
  FROM (SELECT format('%I.%I', n.nspname, c.relname) AS t FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname !~ '^pg_(toast|temp_|toast_temp_)') AS tables ORDER BY t \gexec"""
INSPECT_COPY = ('{{json .Image}}\n{{json .Mounts}}\n'
                '{{range .Config.Env}}{{if eq (index (split . "=") 0) "PGDATA"}}{{json .}}{{end}}{{end}}')
copy_clock = time.monotonic


class CopyFailed(Refused):
    """A database copy ended with one of COPY_CODES; nothing a helper printed is in it."""

    def __init__(self, copy_id, code):
        super().__init__(f"database copy {copy_id} failed: {code}")
        self.code = code


class CopyStuck(Refused):
    """A resource of a database copy could not be confirmed gone: `deploy` pauses."""

    def __init__(self, copy_id):
        super().__init__(f"a database copy could not be stopped: {copy_id}")


def settings_digest(settings):
    """The plan A configuration digest receipts record. The `migration` section is not in it (ANYM-588 C7): adding it
    changes no prepared release."""
    return digest({key: value for key, value in settings.items() if key != "migration"})


def copy_source(settings, name):
    """`migration.databases.<name>` and the copy deadline, or a refusal: without the section nothing is copied."""
    section = settings.get("migration")
    if not isinstance(section, dict) or not isinstance(section.get("databases"), dict):
        raise Refused("the production configuration has no migration section; no database is copied")
    source = section["databases"].get(name) if COPY_NAME.fullmatch(name or "") else None
    if (not isinstance(source, dict) or set(source) != {"container", "owner", "database"}
            or not DOCKER_NAME.fullmatch(str(source["container"]))
            or not all(SQL_NAME.fullmatch(str(source[key])) for key in ("owner", "database"))):
        raise Refused("migration.databases must name the database's container, owner and database")
    timeout = section.get("copy_timeout_s", COPY_TIMEOUT)
    if type(timeout) is not int or not 60 <= timeout <= 86400:
        raise Refused("migration.copy_timeout_s must be 60 to 86400 seconds")
    return dict(source), timeout


def filesystem(path):
    """(free bytes, size bytes) of the filesystem holding `path`."""
    found = os.statvfs(path)
    return found.f_bavail * found.f_frsize, found.f_blocks * found.f_frsize


def copy_reserve(size):
    return max(COPY_RESERVE, size // 10)


def new_file(path):
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def write_record(path, value):
    """`value` as JSON, fsynced with its directory; an earlier record of the name is replaced in one rename."""
    temporary = path.with_name(path.name + ".tmp")
    with suppress(FileNotFoundError):
        temporary.unlink()
    with os.fdopen(new_file(temporary), "w") as handle:
        handle.write(json.dumps(value, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    fsync_directory(path.parent)


def copies_root(state, *, create=False):
    """`<state>/migration-copies`, None when there is none."""
    root = Path(state) / COPIES
    if create:
        with suppress(FileExistsError):
            root.mkdir(mode=0o700)
    try:
        found = os.lstat(root)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.geteuid() or found.st_mode & 0o077:
        raise Refused(f"{root} must be a private operator-owned directory")
    return root


def copy_directories(root):
    """The copy directories under `root` (a planted symlink or file is no copy), oldest first."""
    found = []
    for path in sorted(root.iterdir()) if root else ():
        if COPY_ID.fullmatch(path.name) and stat.S_ISDIR(os.lstat(path).st_mode):
            found.append(path)
    return found


def copy_exec(copy, step, container, *argv):
    """A helper session of the copy: its application name starts with the copy's prefix (C4)."""
    return ["docker", "exec", "-i", "-e", f"PGAPPNAME={copy['application_name']}-{step}", container, *argv]


def check_reserve(copy, paths):
    for path in paths:
        free, size = filesystem(path)
        if free < copy_reserve(size):
            raise CopyFailed(copy["id"], "disk_reserve")


def watched(copy, argv, *, timeout, paths, failure, stdin=None, stdout=None):
    """Run one helper to its end: free space on `paths` is read when it starts and every COPY_POLL seconds; a breach of
    the reserve or the deadline kills it. Its stderr is discarded; a non-zero exit is `failure`."""
    deadline, checked = copy_clock() + timeout, None
    process = subprocess.Popen(argv, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                               stdout=stdout if stdout is not None else subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while True:
            moment = copy_clock()
            if moment >= deadline:
                raise CopyFailed(copy["id"], "deadline")
            if checked is None or moment - checked >= COPY_POLL:
                checked = moment
                check_reserve(copy, paths)
            with suppress(subprocess.TimeoutExpired):
                if process.wait(timeout=0.2):
                    raise CopyFailed(copy["id"], failure)
                return
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def helper_output(copy, argv, *, timeout, failure, text=""):
    """The stdout of a short helper reading `text`; it holds only names, counts and versions, never a row."""
    try:
        result = subprocess.run(argv, input=text.encode(), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CopyFailed(copy["id"], "deadline") from exc
    if result.returncode:
        raise CopyFailed(copy["id"], failure)
    return result.stdout.decode()


def counts(copy, lines, failure):
    try:
        rows = [json.loads(line) for line in lines if line.strip()]
        return {row["table"]: row["rows"] for row in rows}
    except (ValueError, KeyError, TypeError) as exc:
        raise CopyFailed(copy["id"], failure) from exc


class Keeper:
    """The psql session that holds the copy's exported snapshot until the dumps have read it (C1)."""

    MARK = "deedkit-copy-read"

    def __init__(self, copy, argv, deadline):
        self.copy, self.deadline = copy, deadline
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)
        assert self.process.stdin and self.process.stdout
        self.stdin, self.stdout, self.pending = self.process.stdin, self.process.stdout, b""

    def query(self, text):
        """The output lines of `text`; an error ends psql (ON_ERROR_STOP), and that is `dump_failed`."""
        try:
            self.stdin.write(f"{text}\n\\echo {self.MARK}\n".encode())
            self.stdin.flush()
        except OSError as exc:
            raise CopyFailed(self.copy["id"], "dump_failed") from exc
        lines = []
        with selectors.DefaultSelector() as selector:
            selector.register(self.stdout, selectors.EVENT_READ)
            while True:
                while b"\n" in self.pending:
                    line, self.pending = self.pending.split(b"\n", 1)
                    if line.decode() == self.MARK:
                        return lines
                    lines.append(line.decode())
                remaining = self.deadline - copy_clock()
                if remaining <= 0:
                    raise CopyFailed(self.copy["id"], "deadline")
                if selector.select(min(remaining, 1.0)):
                    chunk = os.read(self.stdout.fileno(), 65536)
                    if not chunk:
                        raise CopyFailed(self.copy["id"], "dump_failed")
                    self.pending += chunk

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        with suppress(OSError):
            self.stdin.close()
        self.stdout.close()


def data_filesystem(inspected):
    """The host path of the mount holding the database container's PostgreSQL data directory."""
    env = dict(item.split("=", 1) for item in inspected.get("Config", {}).get("Env") or [] if "=" in item)
    data = posixpath.normpath(env.get("PGDATA", "/var/lib/postgresql/data"))
    mounts = [mount for mount in inspected.get("Mounts") or []
              if data == mount["Destination"] or data.startswith(mount["Destination"].rstrip("/") + "/")]
    if not mounts:
        raise Refused("the database container's data directory is not a mount; its free space is unknown")
    return max(mounts, key=lambda mount: len(mount["Destination"]))["Source"]


def file_digest(path):
    hashed, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            hashed.update(chunk)
            size += len(chunk)
    return {"sha256": hashed.hexdigest(), "bytes": size}


def schema_lines(path):
    """A schema-only dump without its random `\\restrict` key lines (probe 2)."""
    return [line for line in Path(path).read_text().splitlines()
            if not line.startswith(("\\restrict ", "\\unrestrict "))]


def dumped(copy, argv, path, **watch):
    descriptor = new_file(path)
    try:
        watched(copy, argv, stdout=descriptor, **watch)
    finally:
        os.close(descriptor)


def restored(copy, argv, path, **watch):
    with open(path, "rb") as handle:
        watched(copy, argv, stdin=handle, **watch)


def claim(copy, kind, name):
    """Name a container or network a caller (ANYM-449's rehearsal) creates, before it creates it; `release_copy`
    removes it. A container should carry the label `deedkit-copy=<id>`."""
    if kind not in ("container", "network") or not DOCKER_NAME.fullmatch(name):
        raise Refused("a copy resource is a named container or network")
    copy["resources"].append({"kind": kind, "name": name})
    write_record(Path(copy["directory"]) / "owner.json", copy)


def snapshot_copy(copy, timeout, keep):
    """C1-C3: the snapshot, its dumps and their verified restore; writes `copy.json`. With `keep` the scratch container
    stays up for the caller."""
    source, directory = copy["source"], Path(copy["directory"])
    container, owner, database = source["container"], source["owner"], source["database"]
    # Only the image, the mounts and PGDATA: the container's other variables hold its password.
    image, mounts, data = (run(["docker", "inspect", "--type", "container", "--format", INSPECT_COPY, container],
                               timeout=60).splitlines() + ["", "", ""])[:3]
    inspected = {"Image": json.loads(image), "Mounts": json.loads(mounts),
                 "Config": {"Env": [json.loads(data)] if data else []}}
    paths = [str(directory), data_filesystem(inspected)]
    watch = {"timeout": timeout, "paths": paths}
    keeper = Keeper(copy, copy_exec(copy, "keeper", container, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                                    "-U", owner, "-d", database), copy_clock() + timeout)
    try:
        keeper.query(f"SET idle_in_transaction_session_timeout = '{timeout}s'; SET statement_timeout = '{timeout}s';"
                     " BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;")
        found = json.loads(keeper.query(
            "SELECT json_build_object('snapshot', pg_export_snapshot(), 'at', now(), 'version',"
            " current_setting('server_version'), 'bytes', pg_database_size(d.oid), 'owner', pg_get_userbyid(d.datdba),"
            " 'encoding', pg_encoding_to_char(d.encoding), 'collate', d.datcollate, 'ctype', d.datctype)"
            " FROM pg_database d WHERE d.datname = current_database();")[0])
        if (not SNAPSHOT_ID.fullmatch(found["snapshot"]) or not SQL_NAME.fullmatch(found["owner"])
                or not all(LOCALE.fullmatch(found[key]) for key in ("encoding", "collate", "ctype"))):
            raise CopyFailed(copy["id"], "dump_failed")
        for path in paths:
            free, size = filesystem(path)
            if free < 2 * found["bytes"] + copy_reserve(size):
                raise CopyFailed(copy["id"], "disk_reserve")
        reference = counts(copy, keeper.query(COUNTS), "dump_failed")
        snapshot = "--snapshot=" + found["snapshot"]
        dumped(copy, copy_exec(copy, "schema", container, "pg_dump", "--schema-only", snapshot, "-U", owner,
                               "-d", database), directory / "reference.schema", failure="dump_failed", **watch)
        dumped(copy, copy_exec(copy, "dump", container, "pg_dump", "-Fc", snapshot, "-U", owner, "-d", database),
               directory / f"{database}.dump", failure="dump_failed", **watch)
        dumped(copy, copy_exec(copy, "roles", container, "pg_dumpall", "--roles-only", "--no-role-passwords",
                               "-U", owner, "-l", database), directory / "roles.sql", failure="dump_failed", **watch)
        keeper.query("COMMIT;")
    finally:
        keeper.close()
    files = {name: file_digest(directory / name) for name in (f"{database}.dump", "roles.sql", "reference.schema")}
    image = inspected["Image"]
    scratch, scratch_container = directory / "scratch", copy["container"]
    scratch.mkdir(mode=0o700)
    # A root operator lets the image's entrypoint own the data directory; anyone else runs PostgreSQL as itself, so
    # the cleanup can remove what it wrote.
    user = ["--user", f"{os.geteuid()}:{os.getegid()}"] if os.geteuid() else []
    run(["docker", "run", "-d", "--rm", "--name", scratch_container, "--label", "deedkit-copy=" + copy["id"],
         "--network", "none", "--log-driver", "none", "--memory", "1g", "--cpus", "1", "--pids-limit", "256",
         "--shm-size", "256m", *user, "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_USER=" + COPY_USER,
         "-e", "PGDATA=" + COPY_DATA, "-v", f"{scratch}:{COPY_DATA}", image], timeout=120)
    scratch_sql = ("-h", "127.0.0.1", "-U", COPY_USER)
    deadline = copy_clock() + timeout
    # The entrypoint's initialisation server listens on no TCP address: 127.0.0.1 answers only the final server.
    while subprocess.run(copy_exec(copy, "ready", scratch_container, "pg_isready", "-q", *scratch_sql, "-d",
                                   "postgres"), stdin=subprocess.DEVNULL, capture_output=True, timeout=60).returncode:
        if copy_clock() >= deadline:
            raise CopyFailed(copy["id"], "deadline")
        check_reserve(copy, paths)
        time.sleep(0.5)
    psql = ("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", *scratch_sql)
    restored(copy, copy_exec(copy, "roles", scratch_container, *psql, "-d", "postgres"), directory / "roles.sql",
             failure="restore_failed", **watch)
    helper_output(copy, copy_exec(copy, "create", scratch_container, *psql, "-d", "postgres"), timeout=timeout,
                  failure="restore_failed",
                  text=f"CREATE DATABASE \"{database}\" OWNER \"{found['owner']}\" TEMPLATE template0 ENCODING"
                       f" '{found['encoding']}' LC_COLLATE '{found['collate']}' LC_CTYPE '{found['ctype']}';")
    restored(copy, copy_exec(copy, "restore", scratch_container, "pg_restore", "--exit-on-error",
                             "--single-transaction", *scratch_sql, "-d", database), directory / f"{database}.dump",
             failure="restore_failed", **watch)
    copied = counts(copy, helper_output(copy, copy_exec(copy, "count", scratch_container, *psql, "-A", "-t",
                                                        "-d", database), timeout=timeout, failure="restore_failed",
                                        text=COUNTS).splitlines(), "restore_failed")
    if copied != reference:
        raise CopyFailed(copy["id"], "count_mismatch")
    dumped(copy, copy_exec(copy, "verify", scratch_container, "pg_dump", "--schema-only", *scratch_sql,
                           "-d", database), directory / "restored.schema", failure="restore_failed", **watch)
    if schema_lines(directory / "restored.schema") != schema_lines(directory / "reference.schema"):
        raise CopyFailed(copy["id"], "schema_mismatch")
    (directory / "restored.schema").unlink()
    record = {"id": copy["id"], "verified": True, "database": database, "counts": reference, "files": files,
              "snapshot_at": found["at"], "server_version": found["version"], "database_bytes": found["bytes"],
              "image": image, "scratch_container": scratch_container if keep else None, "verified_at": now()}
    write_record(directory / "copy.json", record)
    if not keep:
        stop_copy_containers(copy)
    return record


def copy_sql(source, step, prefix, text):
    """A count or termination statement about the copy's own sessions on the live database."""
    return run(["docker", "exec", "-i", "-e", f"PGAPPNAME={prefix}-{step}", source["container"], "psql", "-X", "-q",
                "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", source["owner"], "-d", source["database"], "-c",
                text.format(f"starts_with(application_name, '{prefix}-') AND pid <> pg_backend_pid()")],
               timeout=60).strip()


def stop_copy_containers(copy):
    for name in [copy["container"], *(item["name"] for item in copy["resources"] if item["kind"] == "container")]:
        # An error or a lost answer is decided by the read back, never by this call.
        with suppress(Exception):
            run(["docker", "stop", "-t", "10", name], timeout=60)


def copy_gone(copy):
    """True when no session, container or network of the copy remains (read back, never assumed)."""
    try:
        if copy_sql(copy["source"], "check", copy["application_name"],
                    "SELECT count(*) FROM pg_stat_activity WHERE {}") != "0":
            return False
        if run(["docker", "ps", "-a", "-q", "--filter", "label=deedkit-copy=" + copy["id"]], timeout=60).strip():
            return False
        for item in [{"kind": "container", "name": copy["container"]}, *copy["resources"]]:
            listing = ["docker", "ps", "-a", "-q"] if item["kind"] == "container" else ["docker", "network", "ls", "-q"]
            if run([*listing, "--filter", f"name=^/?{re.escape(item['name'])}$"], timeout=60).strip():
                return False
    except Exception:
        return False
    return True


def release_copy(copy):
    """C4: stop every session and resource `owner.json` names, confirm each gone, remove the scratch data and write
    `released.json`. The dump files stay for the retention (C6)."""
    directory = Path(copy["directory"])
    with suppress(Exception):
        copy_sql(copy["source"], "cancel", copy["application_name"],
                 "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE {}")
    stop_copy_containers(copy)
    for item in reversed(copy["resources"]):
        if item["kind"] == "network":
            with suppress(Exception):
                run(["docker", "network", "rm", item["name"]], timeout=60)
    deadline = copy_clock() + COPY_STOP_READBACK
    while not copy_gone(copy):
        if copy_clock() >= deadline:
            raise CopyStuck(copy["id"])
        time.sleep(1)
    if os.path.lexists(directory / "scratch"):
        shutil.rmtree(directory / "scratch")
    write_record(directory / "released.json", {"id": copy["id"], "released_at": now()})


def owned_copy(directory):
    """The copy `owner.json` in `directory` names, its fields checked before any of them reaches a command."""
    copy = read(directory / "owner.json")
    source = copy.get("source") or {}
    if (copy.get("id") != directory.name or copy.get("container") != "deedkit-copy-" + directory.name
            or copy.get("application_name") != copy["container"]
            or not DOCKER_NAME.fullmatch(str(source.get("container")))
            or not all(SQL_NAME.fullmatch(str(source.get(key))) for key in ("owner", "database"))
            or not all(item.get("kind") in ("container", "network") and DOCKER_NAME.fullmatch(str(item.get("name")))
                       for item in copy.get("resources", []))):
        raise CopyStuck(directory.name)
    return {**copy, "directory": str(directory)}


def reconcile_copies(state):
    """C4: release every copy a crashed or killed run left without `released.json`; the ids released."""
    released = []
    for directory in copy_directories(copies_root(state)):
        if (directory / "owner.json").exists() and not (directory / "released.json").exists():
            release_copy(owned_copy(directory))
            released.append(directory.name)
    return released


def expire_copies(state, *, at=None):
    """C6: delete every copy older than COPY_RETENTION, whatever its state. Only the `owner.json` of a copy whose
    resources are not confirmed released stays, so a later run can still stop them."""
    at, expired = at or datetime.now(timezone.utc), []
    for directory in copy_directories(copies_root(state)):
        stamp = COPY_ID.fullmatch(directory.name)
        made = datetime.strptime(stamp[1] if stamp else "", "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if (at - made).total_seconds() <= COPY_RETENTION:
            continue
        if (directory / "released.json").exists() or not (directory / "owner.json").exists():
            shutil.rmtree(directory)
        else:
            for entry in directory.iterdir():
                if entry.name != "owner.json":
                    shutil.rmtree(entry) if stat.S_ISDIR(os.lstat(entry).st_mode) else entry.unlink()
        expired.append(directory.name)
    return expired


@contextmanager
def database_copy(settings, name, sha, *, keep=False):
    """ANYM-588: a verified copy of `migration.databases.<name>` taken for release `sha`; its `copy.json` record. With
    `keep` the scratch container serves the block. Every call ends with its resources released, pass or fail."""
    source, timeout = copy_source(settings, name)
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        raise Refused("an exact lowercase 40-character commit SHA is required")
    state = Path(settings["state_directory"])
    root = copies_root(state, create=True)
    assert root
    reconcile_copies(state)
    expire_copies(state)
    copy_id = f"{sha[:12]}-{name}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    directory = root / copy_id
    directory.mkdir(mode=0o700)
    copy = {"id": copy_id, "directory": str(directory), "container": "deedkit-copy-" + copy_id,
            "application_name": "deedkit-copy-" + copy_id, "source": source, "resources": [], "created_at": now()}
    write_record(directory / "owner.json", copy)
    try:
        yield snapshot_copy(copy, timeout, keep)
    finally:
        release_copy(copy)


def tend_copies(settings, *, locked):
    """`deploy`'s copy housekeeping (C4, C6). Unlocked (a paused run) it takes the plan A lock only when it is free."""
    state = Path(settings["state_directory"])
    if copies_root(state) is None:
        return
    with ExitStack() as held:
        if not locked:
            try:
                held.enter_context(lock(state))
            except Busy:
                return
        reconcile_copies(state)
        expire_copies(state)


# Continuous deployment (production.md "Continuous deployment"): one command that deploys the newest green main
# commit, or pauses for a human. A human creates or removes the pause file; `deploy` only creates it.
PAUSE = Path("/etc/deedkit/autodeploy.pause")
CONFIG, HTTP_CONFIG = "/etc/deedkit/production.json", "/etc/deedkit/http-production.json"
DEPLOY_INTERVAL = 20 * 60
# Consecutive runs whose `git ls-remote` or mirror fetch failed before the deployer pauses (about 15 minutes on the
# timer): a network blip heals by itself, a broken mirror or a refused access does not.
FETCH_FAILURES = 3
# Changes a human applies or installs by hand, so the deployer pauses for them; but `deploy` installs the pair of
# TOOL_FILES itself on a host with the self-updating layout ("Deploy tool updates" below).
SQL_INVENTORY = re.compile(r"\.sql$|^services/.+/migrations/")
UNITS = ("deploy/deedkit-autodeploy.service", "deploy/deedkit-autodeploy.timer")
DEPLOY_TOOLS = ("scripts/production.py", "scripts/seamless_release.py", "tools/attest-verify/", "services/release-router/",
                *UNITS)
SYSTEMD_DIRECTORY = Path("/etc/systemd/system")
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


def range_changes(mirror, base, candidate, installed):
    """The SQL inventory files `base..candidate` changes, and its deploy tools that are not installed yet."""
    paths = [path for path in run(["git", "-C", str(mirror), "diff", "--name-only", "--no-renames", "-z", base,
                                   candidate]).split("\0") if path]
    sql = [path for path in paths if SQL_INVENTORY.search(path)]
    tools = [path for path in paths if path in DEPLOY_TOOLS or path.startswith(tuple(t for t in DEPLOY_TOOLS if t[-1] == "/"))]
    def current_tool(path):
        # The running tool is the candidate's when its installed file has the candidate's content; a unit this host
        # does not install (no timer) needs no install.
        if path in installed:
            if path in UNITS and not os.path.lexists(installed[path]):
                return True
            return run(["git", "-C", str(mirror), "rev-parse", f"{candidate}:{path}"]).strip() == blob(installed[path])
        # The verifier is the candidate's when it matches the pin of the installed (the candidate's) production.py.
        return (path.startswith("tools/attest-verify/") and current_tool("scripts/production.py") and VERIFIER.exists()
                and hashlib.sha256(VERIFIER.read_bytes()).hexdigest() == VERIFIER_SHA256)
    return sql, [path for path in tools if not current_tool(path)]


MIGRATION = re.compile(r"services/messaging/messaging_service/migrations/([0-9]{3}_[A-Za-z0-9_]+\.sql)")
CLASSIFIER = "services/messaging/messaging_service/migration_rollout.py"
CLASSES = ("expand", "manual", "no header")
CLASSIFY_TIMEOUT = 30
# Bounds of the classifier child: the bytes it gets (migration texts and its source), the stdout read back and its
# address space. Beyond any of them every added migration is `manual`.
CLASSIFY_INPUT_LIMIT = 4 << 20
CLASSIFY_OUTPUT_LIMIT = 64 << 10
CLASSIFY_MEMORY = 512 << 20
# The candidate's classifier gets the migration texts on stdin and prints one class per text. The candidate is an
# attested main commit (or the current release), the same trust the tool self-update gives its production.py; the
# audit hook is no sandbox, it only stops a faulty classifier from opening files, importing, starting processes or
# reaching the network.
CLASSIFY = """import json, re, sys
data = json.loads(sys.stdin.read())
def refuse(event, args):
    if event.split(".")[0] in ("open", "import", "socket", "subprocess", "os", "shutil", "ctypes", "fcntl", "urllib",
                               "http", "sqlite3", "pty", "signal", "syslog", "resource", "mmap"):
        raise PermissionError(event)
sys.addaudithook(refuse)
module = {"__name__": "migration_rollout"}
exec(compile(data["source"], "migration_rollout.py", "exec"), module)
print(json.dumps([module["rollout_class"](text) for text in data["texts"]]))
"""


def classifier_limits():
    """In the classifier child before exec: its address space and the size of a file it writes (its stdout). A limit
    the platform refuses is skipped; the output and input bounds still hold."""
    for limit, value in ((resource.RLIMIT_AS, CLASSIFY_MEMORY), (resource.RLIMIT_FSIZE, CLASSIFY_OUTPUT_LIMIT + 1)):
        with suppress(ValueError, OSError):
            resource.setrlimit(limit, (value, value))


def migration_classes(mirror, base, candidate, sql):
    """`<file> <class>` for each migration `base..candidate` adds (ANYM-444): the class the candidate's classifier
    gives the candidate's bytes, `expand`, `manual` or `no header`. It only names; whatever fails here is `manual`."""
    if not any(MIGRATION.fullmatch(path) for path in sql):
        return []
    try:
        added = [path for path in run(["git", "-C", str(mirror), "diff", "--name-only", "--no-renames", "--diff-filter=A",
                                       "-z", base, candidate]).split("\0") if MIGRATION.fullmatch(path)]
    except Refused:
        return []
    objects = [f"{candidate}:{path}" for path in added + [CLASSIFIER]]
    try:
        # Sizes first: nothing over the input limit is read, and then no process starts.
        if sum(int(run(["git", "-C", str(mirror), "cat-file", "-s", obj])) for obj in objects) > CLASSIFY_INPUT_LIMIT:
            raise ValueError("over the classifier input limit")
        blobs = [run(["git", "-C", str(mirror), "cat-file", "blob", obj], binary=True) for obj in objects]
        if sum(map(len, blobs)) > CLASSIFY_INPUT_LIMIT:
            raise ValueError("over the classifier input limit")
        *texts, source = [data.decode() for data in blobs]
        with tempfile.TemporaryFile() as out:
            result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", CLASSIFY], stdout=out,
                                    stderr=subprocess.DEVNULL, input=json.dumps({"source": source, "texts": texts}).encode(),
                                    timeout=CLASSIFY_TIMEOUT, preexec_fn=classifier_limits)
            out.seek(0)
            output = out.read(CLASSIFY_OUTPUT_LIMIT + 1)
        if result.returncode or len(output) > CLASSIFY_OUTPUT_LIMIT:
            raise ValueError("the classifier failed or printed too much")
        classes = json.loads(output)
    except Exception:  # Refused, OSError, a timeout, a limit, RecursionError of deep JSON: all name `manual`
        classes = None
    if not (isinstance(classes, list) and len(classes) == len(added) and all(c in CLASSES for c in classes)):
        classes = ["manual"] * len(added)
    return [f"{MIGRATION.fullmatch(path)[1]} {kind}" for path, kind in zip(added, classes)]


def pause_text(sql, pending, classes=()):
    reasons = []
    if sql:
        reasons.append("the SQL inventory changed (" + ", ".join(sql[:5]) + "); migrate by hand (production.md)"
                       + ("; added migrations: " + ", ".join(classes) if classes else ""))
    if pending:
        reasons.append("deploy tools changed (" + ", ".join(pending[:5]) + "); install them by hand (production.md)")
    return reasons


def pause_reasons(mirror, base, candidate, installed):
    """Why `base..candidate` needs a human: SQL inventory changes, each added migration's class, and deploy tools
    that are not installed yet."""
    sql, pending = range_changes(mirror, base, candidate, installed)
    return pause_text(sql, pending, migration_classes(mirror, base, candidate, sql))


# Deploy tool updates (ANYM-443, production.md "Deploy tool updates"). Each installed tool pair has its own directory
# TOOL_ROOT/releases/<sha>/ with a tool.json; `current` points at one of them and the documented paths production.py
# and seamless_release.py point into `current/`, so Python takes both modules from one directory (sys.path[0] is the
# script's resolved directory). From a range whose only pause is that pair, `deploy` installs the candidate's pair:
# stage, self-test, `activating`, switch `current`, self-test through the documented path, `active`.
TOOL_ROOT = Path("/usr/local/lib/deedkit")
TOOL_FILES = {"scripts/production.py": "production.py", "scripts/seamless_release.py": "seamless_release.py"}
TOOL_PYTHON = "/usr/bin/python3"
SELF_TEST_TIMEOUT = 60
# The running module, resolved at import: after a switch the documented path already names the next tool.
RUNNING = Path(__file__).resolve()
TOOL_STATUSES = ("activating", "active", "reverted", "not-activated", "rolled-back")


def tool_path(path, kind):
    """`path` itself, no symlink followed: of `kind`, operator-owned and not writable by group or others."""
    info = path.lstat()
    if stat.S_IFMT(info.st_mode) != kind or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise Refused(f"{path} must be a {'directory' if kind == stat.S_IFDIR else 'file'} only the operator owns "
                      "and writes")


def tool_record(directory, sha=None):
    """The tool.json of `directory`, which must name the git blob ids of the two files beside it."""
    try:
        tool = json.loads((directory / "tool.json").read_text())
        blobs = {path: blob(directory / name) for path, name in TOOL_FILES.items()}
    except (OSError, ValueError) as exc:
        raise Refused(f"cannot read the tool files of {directory}: {type(exc).__name__}") from exc
    if (not isinstance(tool, dict) or set(tool) != {"sha", "blobs", "installed_at"} or tool["blobs"] != blobs
            or not RELEASE_DIRECTORY.fullmatch(str(tool["sha"])) or sha not in (None, tool["sha"])
            or not isinstance(tool["installed_at"], str)):
        raise Refused(f"{directory / 'tool.json'} does not name this release and the blob ids of its files")
    return tool


def tool_release(sha):
    """The checked release TOOL_ROOT/releases/<sha>: its two files and tool.json, an optional bytecode cache, no
    symlink, all of them the operator's alone."""
    directory = TOOL_ROOT / "releases" / sha
    try:
        tool_path(directory, stat.S_IFDIR)
        names = set(os.listdir(directory))
        required = {*TOOL_FILES.values(), "tool.json"}
        if not required <= names or names - required - {"__pycache__"}:
            raise Refused(f"{directory} must hold exactly production.py, seamless_release.py and tool.json")
        for name in names:
            tool_path(directory / name, stat.S_IFDIR if name == "__pycache__" else stat.S_IFREG)
        if "__pycache__" in names:
            for name in os.listdir(directory / "__pycache__"):
                tool_path(directory / "__pycache__" / name, stat.S_IFREG)
    except OSError as exc:
        raise Refused(f"cannot check {directory}: {type(exc).__name__}") from exc
    return tool_record(directory, sha)


def current_tool_sha():
    """The release `current` points at, or None when it is no `releases/<sha>` symlink."""
    try:
        match = re.fullmatch(r"releases/([0-9a-f]{40})", os.readlink(TOOL_ROOT / "current"))
    except OSError:
        return None
    return match and match[1]


def installed_tool():
    """The tool `current` points at, its whole layout checked, or None on a host without the self-updating layout (no
    `current`): that host keeps the pause for every tool change."""
    current = TOOL_ROOT / "current"
    try:
        if not os.path.lexists(current):
            return None
        releases = TOOL_ROOT / "releases"
        tool_path(TOOL_ROOT, stat.S_IFDIR)
        tool_path(releases, stat.S_IFDIR)
        links = {"current": None, **{name: "current/" + name for name in TOOL_FILES.values()}}
        for name, target in links.items():
            info = (TOOL_ROOT / name).lstat()
            if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid() or target not in (
                    None, os.readlink(TOOL_ROOT / name)):
                raise Refused(f"{TOOL_ROOT / name} must be the operator's symlink to {target or 'releases/<sha>'}")
        extra = [entry.path for directory in (TOOL_ROOT, releases) for entry in os.scandir(directory)
                 if entry.is_symlink() and not (directory == TOOL_ROOT and entry.name in links)]
        if extra:
            raise Refused(f"unexpected symlinks in the tool layout: {', '.join(sorted(extra)[:5])}")
    except OSError as exc:
        raise Refused(f"cannot check the tool layout in {TOOL_ROOT}: {type(exc).__name__}") from exc
    if (sha := current_tool_sha()) is None:
        raise Refused(f"{current} must point at releases/<40-character sha>")
    return tool_release(sha)


def fsync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def point_current(sha):
    """Switch `current` to releases/<sha> with one rename(2)."""
    link = TOOL_ROOT / f".current-{uuid.uuid4().hex}"
    os.symlink(f"releases/{sha}", link)
    try:
        os.replace(link, TOOL_ROOT / "current")
    except BaseException:
        link.unlink(missing_ok=True)
        raise
    fsync_directory(TOOL_ROOT)


def stage_tool(sha, files, blobs):
    """releases/<sha>.staging-<random>, every file written to a temporary name, synced, then renamed into place."""
    staging = TOOL_ROOT / "releases" / f"{sha}.staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    record = json.dumps({"sha": sha, "blobs": blobs, "installed_at": now()}, indent=2) + "\n"
    for name, content in {**files, "tool.json": record.encode()}.items():
        temporary = staging / (name + ".tmp")
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        temporary.replace(staging / name)
    staging.chmod(0o755)
    fsync_directory(staging)
    return staging


def self_test_error(script, configs, sha):
    """None when `script self-test` exits 0 and prints exactly its one ok line for `sha`, else its error line: the
    refusal or the exception, the last line it wrote to stderr. The host's Python, no bytecode written, no environment
    but PATH, not isolated (`-I` would leave the script's directory off sys.path)."""
    try:
        result = subprocess.run([TOOL_PYTHON, "-B", "-E", "-s", str(script), "self-test", "--config", str(configs[0]),
                                 "--http-config", str(configs[1])], capture_output=True, text=True, cwd="/",
                                timeout=SELF_TEST_TIMEOUT, env={"PATH": "/usr/bin:/bin"})
    except subprocess.TimeoutExpired:
        return f"the self-test did not finish within {SELF_TEST_TIMEOUT} seconds"
    except OSError as exc:
        return f"the self-test could not start: {type(exc).__name__}"
    with suppress(ValueError):
        if (result.returncode == 0 and not result.stderr and result.stdout.count("\n") == 1
                and json.loads(result.stdout) == {"self_test": "ok", "sha": sha}):
            return None
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    return (lines[-1] if lines else f"exit {result.returncode} without the ok line for {sha}")[:300]


def accepted_tools(update, current):
    """The accepted tools from the last tool-update.json, `current` first, then the ones it replaced. A release is
    accepted by its `active` record, never by its install time: a reverted or abandoned candidate is none."""
    history = [*([update["to"]] if update and update["status"] == "active" else []), *(update or {}).get("accepted", [])]
    return history if history[:1] == [current] else [current]


def retain_tools(keep):
    """After an update: the releases in `keep` (the new tool, the one it replaced and the one before that) stay, other
    checked releases are removed."""
    releases, removed = TOOL_ROOT / "releases", []
    for name in sorted(os.listdir(releases)):
        if RELEASE_DIRECTORY.fullmatch(name) and name not in keep:
            with suppress(Refused):  # a directory that is no checked release is not this tool's to remove
                removed.append(tool_release(name)["sha"])
    for name in removed:
        shutil.rmtree(releases / name)
    return removed


def tool_update_receipt(state):
    """tool-update.json of the plan A state, or None; Refused when it is malformed."""
    path = state / "tool-update.json"
    if not path.exists():
        return None
    try:
        update = read(path)
        if (update.get("status") not in TOOL_STATUSES or not RELEASE_DIRECTORY.fullmatch(str(update.get("from")))
                or not RELEASE_DIRECTORY.fullmatch(str(update.get("to")))
                or not isinstance(accepted := update.get("accepted", []), list)
                or not all(RELEASE_DIRECTORY.fullmatch(str(sha)) for sha in accepted)):
            raise ValueError("tool update receipt")
    except (OSError, ValueError, AttributeError) as exc:
        raise Refused(f"the tool update receipt {path} is malformed; inspect it") from exc
    return update


def reconcile_tool(settings, configs, s, *, dry_run, halt):
    """An update that stopped between `activating` and `active`: accepted only by the self-test through the documented
    path, closed when `current` still names the previous tool, otherwise a pause. None when nothing is left open."""
    state = Path(settings["state_directory"])
    if (update := tool_update_receipt(state)) is None or update["status"] != "activating":
        return None
    old, new = update["from"], update["to"]
    if dry_run:
        return {"status": "plan", "steps": [f"reconcile the tool update {old} -> {new}"]}
    at = current_tool_sha()
    if at == new:
        if (error := self_test_error(TOOL_ROOT / "production.py", configs, new)) is None:
            s.durable_write(state / "tool-update.json", {**update, "status": "active", "activated_at": now()})
            return None
        point_current(old)
        s.durable_write(state / "tool-update.json", {**update, "status": "reverted", "reason": error})
        return halt(f"the self-test of tool {new} after its switch failed: {error}; {TOOL_ROOT / 'current'} points at "
                    f"{old} again")
    if at == old:
        s.durable_write(state / "tool-update.json", {**update, "status": "not-activated", "closed_at": now()})
        return None
    return halt(f"the tool update {old} -> {new} is unresolved: {TOOL_ROOT / 'current'} names neither; inspect it, "
                f"the previous tool is python3 {TOOL_ROOT / 'releases' / old / 'production.py'}")


def update_tool(settings, configs, s, mirror, candidate, base, tool, *, dry_run, halt):
    """Install the candidate's tool pair (production.md "Deploy tool updates"). The run then ends `tool-updated`, and
    the next one deploys with the new tool."""
    state, old = Path(settings["state_directory"]), tool["sha"]
    receipt = state / "tool-update.json"
    # Forward only, from the commit whose images the verifier accepted, each file checked against its git blob id.
    if not on_main(mirror, old, candidate):
        return halt(f"the candidate {candidate} does not descend from the installed tool {old}; install the tool by "
                    "hand (production.md)", sha=candidate)
    files, blobs = {}, {}
    for path, name in TOOL_FILES.items():
        blobs[path] = run(["git", "-C", str(mirror), "rev-parse", f"{candidate}:{path}"]).strip()
        files[name] = run(["git", "-C", str(mirror), "cat-file", "blob", f"{candidate}:{path}"], binary=True)
        if hashlib.sha1(b"blob %d\0" % len(files[name]) + files[name]).hexdigest() != blobs[path]:
            return halt(f"{path} of {candidate} does not match its git blob id; the tool is not updated", sha=candidate)
    last = tool_update_receipt(state)
    if last and last["status"] == "rolled-back" and last.get("blobs") == blobs:
        return halt(f"the tool of {last['to']} was rolled back (tool-rollback); install a changed tool by hand or merge "
                    "one", sha=candidate)
    if dry_run:
        return {"status": "plan", "sha": candidate, "base": base, "steps": [f"would update tool {candidate}"]}
    target, staged, history = TOOL_ROOT / "releases" / candidate, None, accepted_tools(last, old)
    try:
        for leftover in (TOOL_ROOT / "releases").glob("*.staging-*"):
            shutil.rmtree(leftover)  # an interrupted update's; rmtree never follows a symlink
        if os.path.lexists(target):
            # From an interrupted update: reused only when it is exactly the candidate's pair.
            try:
                if tool_release(candidate)["blobs"] != blobs:
                    raise Refused("its blob ids are not the candidate's")
            except Refused as exc:
                return halt(f"{target} exists but is not the candidate's tool: {exc}; inspect it", sha=candidate)
            # Its bytecode cache is not covered by the blob ids, and `-B` still reads it: the source runs instead.
            if (target / "__pycache__").exists():
                shutil.rmtree(target / "__pycache__")
            script = target / "production.py"
        else:
            staged = stage_tool(candidate, files, blobs)
            script = staged / "production.py"
        if error := self_test_error(script, configs, candidate):
            if staged:
                shutil.rmtree(staged)
            return halt(f"the self-test of tool {candidate} failed: {error}; the installed tool {old} is unchanged",
                        sha=candidate)
        if staged:
            staged.rename(target)
            fsync_directory(TOOL_ROOT / "releases")
        update = {"from": old, "to": candidate, "at": now(), "status": "activating", "accepted": history[:2]}
        s.durable_write(receipt, update)
        point_current(candidate)
        if error := self_test_error(TOOL_ROOT / "production.py", configs, candidate):
            point_current(old)
            s.durable_write(receipt, {**update, "status": "reverted", "reason": error})
            return halt(f"the self-test of tool {candidate} after its switch failed: {error}; {TOOL_ROOT / 'current'} "
                        f"points at {old} again", sha=candidate)
        s.durable_write(receipt, {**update, "status": "active", "activated_at": now()})
    except Exception as exc:
        message = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        return halt(f"the tool update {old} -> {candidate} stopped: {message}; inspect {TOOL_ROOT} and {receipt}",
                    sha=candidate)
    try:
        removed = retain_tools([candidate, *history[:2]])
    except Exception as exc:
        removed = "not finished: " + (str(exc) if isinstance(exc, Refused) else type(exc).__name__)
    outcome = {"status": "tool-updated", "sha": candidate, "base": base, "finished_at": now(),
               "tool_update": {"from": old, "to": candidate, "removed": removed}}
    write(state / "deploy.json", outcome)
    return outcome


def tool_rollback(settings, http, s):
    """Point `current` back at the tool the last update replaced, and pause autodeploy (production.md)."""
    state = Path(settings["state_directory"])
    with host_locks(settings, http, s):
        tool, update = installed_tool(), tool_update_receipt(state)
        if tool is None or not update or update["status"] != "active" or update["to"] != tool["sha"]:
            raise Refused("no active tool update to roll back; switch current by hand (production.md)")
        tool_release(update["from"])
        reason = (f"tool rolled back from {update['to']} to {update['from']}; remove this file to resume, a changed "
                  "tool installs itself again")
        PAUSE.write_text(json.dumps({"status": "tool-rolled-back", "reason": reason, "at": now()}) + "\n")
        PAUSE.chmod(0o600)
        point_current(update["from"])
        s.durable_write(state / "tool-update.json",
                        {**update, "status": "rolled-back", "rolled_back_at": now(), "blobs": tool["blobs"]})
        return {"status": "tool-rolled-back", "from": update["to"], "to": update["from"]}


def self_test(config_path, http_path):
    """This tool's check before `deploy` runs it (production.md "Deploy tool updates"): both modules from this
    directory, its tool.json, both configurations and the current receipts read with this code, the verifier and its
    pin. It runs under the guard at the top of this file."""
    if not GUARDED:
        raise Refused("self-test must be the first argument")
    directory = RUNNING.parent
    s = http_release_tool()
    if Path(s.__file__).resolve().parent != directory:
        raise Refused("seamless_release.py was not imported from the directory of production.py")
    tool = tool_record(directory)
    settings = config(config_path)
    http = s.validate_settings(config(http_path))
    state, http_state = Path(settings["state_directory"]), Path(http["state_directory"])
    for path in (state / "current.json", http_state / "http-current.json", state / "deploy.json"):
        try:
            receipt = read(path) if path.name != "deploy.json" or path.exists() else {}
            if path.name == "deploy.json":
                deploy_counts(receipt)
            elif (receipt.get("status") not in ("applied", "serving", "serving-retirement-pending")
                  or not RELEASE_DIRECTORY.fullmatch(str(receipt.get("sha")))):
                raise ValueError("receipt")
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            raise Refused(f"the receipt {path} is missing or malformed") from exc
    tool_update_receipt(state)
    try:
        check_verifier()
    except Refused as exc:
        raise Refused(f"{exc}; install the verifier first") from exc
    return {"self_test": "ok", "sha": tool["sha"]}


def configuration_drift(settings, http, current, serving):
    """Why the configuration differs from the one the current releases were applied with, or None."""
    state = Path(settings["state_directory"])
    if read(state / current["sha"] / "prepared.json").get("config_digest") != settings_digest(settings):
        return "the production configuration changed since the current release was prepared"
    after = {item["name"]: item["after_digest"] for item in current.get("rollback", {}).get("projects", [])}
    for project in settings["projects"]:
        if after.get(project["name"]) != digest(json.loads(run(compose(project) + ["config", "--format", "json"]))):
            return f"the Compose configuration of {project['name']} changed since the current release was applied"
    if serving.get("config_digest") != digest(http):
        return "the HTTP release configuration changed since the serving HTTP release was prepared"
    return None


def stale_tool(s):
    """Why this run is not the tool `current` names on a host with a checked self-updating layout, or None. A refused
    or missing layout is left to the range check, which pauses for it when the tool changes."""
    try:
        tool = installed_tool()
    except Refused:
        return None
    if tool and [blob(RUNNING), blob(s.__file__)] != [tool["blobs"][path] for path in TOOL_FILES]:
        return f"this run is not the tool {TOOL_ROOT / 'current'} names ({tool['sha']})"
    return None


@contextmanager
def host_locks(settings, http, s):
    """The global, HTTP and plan A locks `deploy` holds for its whole run."""
    state = Path(settings["state_directory"])
    with ExitStack() as held:
        held.enter_context(s.locks(http))
        if state.resolve() not in (s.GLOBAL_LOCK.resolve(), Path(http["state_directory"]).resolve()):
            held.enter_context(lock(state))
        yield


def deploy(settings, http, *, auto=False, dry_run=False, attested=None, configs=(CONFIG, HTTP_CONFIG)):
    """Deploy the newest green main commit with plan A and then plan B, or pause with a reason (production.md)."""
    if PAUSE.exists():
        paused = {"status": "paused", "reason": PAUSE.read_text()[:1000].strip()}
        if not dry_run:
            # Copies still expire while paused (ANYM-588 C6); the pause already stops everything else.
            try:
                tend_copies(settings, locked=False)
            except Exception as exc:
                paused["copies"] = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        return paused
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
            held.enter_context(host_locks(settings, http, s))
        except Busy as exc:
            return {"status": "busy", "reason": str(exc)}
        except Exception as exc:
            return halt("deploy could not take the host locks: " + (str(exc) if isinstance(exc, Refused)
                                                                     else type(exc).__name__))
        # tool-rollback or another run may have paused or switched the tool while this run waited for the locks.
        if PAUSE.exists():
            return {"status": "paused", "reason": PAUSE.read_text()[:1000].strip()}
        if stale := stale_tool(s):
            return {"status": "error", "reason": stale + "; nothing changed, the next run uses it"}
        if not dry_run:
            try:
                tend_copies(settings, locked=True)
            except Exception as exc:
                return halt(str(exc) if isinstance(exc, CopyStuck) else "database copies could not be reconciled or"
                            " expired: " + (str(exc) if isinstance(exc, Refused) else type(exc).__name__))
        # A candidate is taken only when every image plan A or plan B pulls is attested, so publication that is still
        # running or failed for one target never starts a release that stops halfway.
        targets = sorted(set(published_targets(settings)) | set(published_targets(http)) | {PROOF_TARGET})
        return deploy_locked(settings, http, s, auto=auto, dry_run=dry_run, halt=halt, configs=configs,
                             attested=attested or (lambda sha: [published(target, sha) for target in targets]))


def deploy_locked(settings, http, s, *, auto, dry_run, attested, halt, configs):
    state = Path(settings["state_directory"])
    record = {}
    try:
        return deploy_steps(settings, http, s, auto=auto, dry_run=dry_run, attested=attested, halt=halt, record=record,
                            configs=configs)
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


def deploy_counts(last):
    """The start and the failure counts of a deploy.json; ValueError, TypeError, AttributeError or KeyError when it is
    malformed."""
    started = last.get("started_at") and datetime.strptime(last["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    failures = last.get("fetch_failures", {"count": 0})["count"]
    misses = last.get("unreachable", {"count": 0})["count"]
    waits = last.get("call_waits", {"count": 0})["count"]
    if any(type(count) is not int or count < 0 for count in (failures, misses, waits)):
        raise ValueError("fetch failure count")
    return started, failures, misses, waits


def unresolved(state, http_state, history=None, serving=None):
    """Why no release-changing write may start, or None: the checks `deploy` runs before it changes anything, shared
    with `deedkit admin` (ANYM-493). `history` returns the main mirror and `serving` is the serving HTTP release's
    SHA (http-current.json's when omitted); without `history` the interrupted-publication check is skipped."""
    if blocked := unfinished(state):
        return "an apply or rollback is unfinished: " + ", ".join(blocked) + "; inspect it before any recovery"
    attempts = {path.parent.name: read(path).get("status") for path in http_state.glob("*/http-attempted.json")}
    # An HTTP attempt that neither serves nor failed before admission has an unknown router or retirement outcome.
    if pending := sorted(name for name, status in attempts.items() if status not in ("serving", "startup-failed")):
        return ("an HTTP release attempt is unresolved: " + ", ".join(pending)
                + "; inspect seamless_release.py status --sha <sha>")
    attempts = sorted(name for name, status in attempts.items() if status == "serving")
    if history is None or not attempts:
        return None
    if serving is None:
        current = http_state / "http-current.json"
        serving = read(current).get("sha", "") if current.exists() else ""
    # promote marks an attempt `serving` before it publishes http-current.json: a serving attempt the serving release
    # does not contain is an interrupted publication, and the router may already admit that generation.
    mirror, sha = history(), re.compile(r"[0-9a-f]{40}")
    if unpublished := [name for name in attempts
                       if not (sha.fullmatch(name) and sha.fullmatch(serving) and on_main(mirror, name, serving))]:
        return ("an HTTP release attempt serves, but http-current.json does not name it: " + ", ".join(unpublished)
                + "; inspect seamless_release.py status --sha <sha>")
    return None


def deploy_steps(settings, http, s, *, auto, dry_run, attested, halt, record, configs):
    state, http_state = Path(settings["state_directory"]), Path(http["state_directory"])
    # A tool update that stopped between `activating` and `active` is settled before anything else.
    if reconciled := reconcile_tool(settings, configs, s, dry_run=dry_run, halt=halt):
        return reconciled
    try:
        last = read(state / "deploy.json") if (state / "deploy.json").exists() else {}
        started, failures, misses, waits = deploy_counts(last)
    except (ValueError, TypeError, AttributeError, KeyError):
        return halt(f"the previous deploy receipt {state / 'deploy.json'} is malformed; inspect it")
    if auto and started and (datetime.now(timezone.utc) - started).total_seconds() < DEPLOY_INTERVAL:
        return {"status": "waiting", "reason": "less than 20 minutes since the last deploy started"}
    if "call_waits" in last:
        # Waits for calls count only in a row: this run carries the count on only when it waits again.
        last = {key: value for key, value in last.items() if key != "call_waits"}
        if not dry_run:
            write(state / "deploy.json", last)
    if reason := unresolved(state, http_state):
        return halt(reason)
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
    if reason := unresolved(state, http_state, lambda: mirror, serving["sha"]):
        return halt(reason)
    if drift := configuration_drift(settings, http, current, serving):
        return halt(drift + "; apply the next release by hand")
    installed = {"scripts/production.py": RUNNING, "scripts/seamless_release.py": s.__file__,
                 **{path: SYSTEMD_DIRECTORY / Path(path).name for path in UNITS}}
    if serving["sha"] != base:
        # Plan A is ahead of the HTTP pair: a deploy whose HTTP release stopped before any change, or plan A applied
        # by hand.
        candidate, since = base, serving["sha"]
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
        since = base
    sql, pending = range_changes(mirror, since, candidate, installed)
    if (reasons := pause_text(sql, pending, migration_classes(mirror, since, candidate, sql))) and not sql and set(pending) <= TOOL_FILES.keys():
        # Only the tool pair waits for an install: the self-updating layout installs it, when this run is its tool.
        try:
            tool = installed_tool()
        except Refused as exc:
            tool = reasons.append(f"the tool does not update itself: {exc}")
        if tool and [blob(RUNNING), blob(s.__file__)] != [tool["blobs"][path] for path in TOOL_FILES]:
            tool = reasons.append(f"the tool does not update itself: this run is not {TOOL_ROOT / 'current'}")
        if tool:
            return update_tool(settings, configs, s, mirror, candidate, base, tool, dry_run=dry_run, halt=halt)
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
            if isinstance(exc, (CallActive, CallsUnknown)) and not (release / "attempted.json").exists():
                # Nothing was stopped: the prepared release waits for a later run; the fourth wait in a row pauses.
                if waits + 1 >= CALL_WAITS:
                    return finish(**halt(f"plan A of {candidate} waited {waits + 1} runs in a row for calls: {message}; "
                                         "inspect the calls service"))
                return finish("waiting", reason=message + "; nothing changed, the next run retries",
                              call_waits={"count": waits + 1, "at": now(), "reason": message})
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


def active_config():
    """The plan A configuration the autodeploy unit reads: DEEDKIT_DEPLOY_CONFIG from the environment, else from the
    optional /etc/deedkit/autodeploy.env (deploy/deedkit-autodeploy.service), else CONFIG."""
    if value := os.environ.get("DEEDKIT_DEPLOY_CONFIG"):
        return value
    try:
        text = (CONFIG_DIRECTORY / "autodeploy.env").read_bytes().decode("ascii", "replace")
    except FileNotFoundError:
        return CONFIG
    if re.search(r"[^\x20-\x7e\t\n]", text):  # no CR: systemd also ends a line (and a comment) there
        raise Refused(f"{CONFIG_DIRECTORY / 'autodeploy.env'} has a character status cannot read as systemd does; pass --config")
    found = CONFIG
    for line in text.split("\n"):
        line = line.strip(" \t")
        if not line or line[0] in "#;":
            continue
        # Only plain KEY=value lines (one optional pair of quotes): what systemd's EnvironmentFile= reads the same way.
        # Escapes, continuations and multi-line quotes are refused, never guessed.
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(?:'([^'\\]*)'|\"([^\"\\]*)\"|([^'\"\\]*))", line)
        if not match:
            raise Refused(f"{CONFIG_DIRECTORY / 'autodeploy.env'} has a line status cannot read as systemd does; pass --config")
        if match[1] == "DEEDKIT_DEPLOY_CONFIG":
            found = match[2] if match[2] is not None else match[3] if match[3] is not None else match[4].strip(" \t")
    if not found:
        raise Refused(f"{CONFIG_DIRECTORY / 'autodeploy.env'} sets an empty DEEDKIT_DEPLOY_CONFIG; pass --config")
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["verify", "prepare", "prepare-router", "apply", "rollback", "status",
                                           "images", "check-image", "deploy", "self-test", "tool-rollback",
                                           "copy-database"])
    parser.add_argument("--sha")
    parser.add_argument("--target", choices=sorted(BUILDS))
    parser.add_argument("--config", help="default " + CONFIG + "; status: the autodeploy unit's (active_config)")
    parser.add_argument("--http-config", default=HTTP_CONFIG, help="deploy: seamless_release.py's")
    parser.add_argument("--auto", action="store_true", help="deploy: at most one deploy start per 20 minutes (timer)")
    parser.add_argument("--dry-run", action="store_true", help="deploy: print the plan or pause reason, change nothing")
    parser.add_argument("--remove", action="store_true", help="images: remove the listed candidates")
    parser.add_argument("--database", help="copy-database: a name under the configuration's migration.databases")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        args.config = args.config or (active_config() if args.action == "status" else CONFIG)
        if args.action == "check-image":
            if not args.target:
                raise Refused("select a target for container verification")
            source = Path(__file__).resolve().parents[1]
            sha = run(["git", "-C", str(source), "rev-parse", "HEAD"]).strip()
            result = {"sha": sha, "image": build_image(source, sha, args.target, check=True)}
        elif args.action == "verify":
            result = verify(args.sha or "")
        elif args.action == "self-test":
            print(json.dumps(self_test(args.config, args.http_config)))
            return
        elif args.action == "tool-rollback":
            if os.geteuid() != 0:
                raise Refused("tool-rollback requires the production host operator")
            s = http_release_tool()
            result = tool_rollback(config(args.config), s.validate_settings(config(args.http_config)), s)
        elif args.action == "deploy":
            if os.geteuid() != 0:
                raise Refused("deploy requires the production host operator")
            result = deploy(config(args.config), http_release_tool().validate_settings(config(args.http_config)),
                            auto=args.auto, dry_run=args.dry_run, configs=(args.config, args.http_config))
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
                    result["config"] = str(args.config)
                    if (state / "deploy.json").exists():
                        result["deploy"] = read(state / "deploy.json")
                    if PAUSE.exists():
                        result["autodeploy_pause"] = PAUSE.read_text()[:1000].strip()
                elif args.action == "images":
                    result = remove_images(settings) if args.remove else images_report(settings)
                elif args.action == "copy-database":
                    with database_copy(settings, args.database, args.sha) as copied:
                        result = copied
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
        if isinstance(exc, OSError) and exc.filename:
            message += " " + str(exc.filename)
        parser.exit(1, "promotion refused: " + message + "\n")


if __name__ == "__main__":
    main()
