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
import ast
import copy
from contextlib import ExitStack, contextmanager, suppress
from datetime import datetime, timedelta, timezone
import fcntl
import fnmatch
import hashlib
import importlib.util
import itertools
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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPOSITORY = "GoSync-Inc/deedkit"
# `publish` pushed and attested this commit's images; preparation pulls nothing from a run without it (ANYM-301).
REQUIRED_JOBS = {"checks / verify", "native / synthetic", "images / build (backup)", "CI gate", "publish"}
# A main push run may skip only this job, and only when the merge queue verified the exact commit (ANYM-321).
QUEUE_VERIFIED_JOB = "checks / verify"
# With that skip the push run may also skip this job: the queue run, or the pull request run it reused, must show it
# passed on the same tree (ci-deploy-speed 11-09; TIDEN-CI.md).
NATIVE_JOB = "native / synthetic"
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
    "runtime": (".", "services/messaging/Dockerfile", "runtime", "import messaging_service.app, telegram_broker, telegram_session_owner, platform_contracts.release"),
    "indexer": (".", "services/messaging/Dockerfile", "indexer", "import messaging_service.file_index"),
    "enricher-local": (".", "services/messaging/Dockerfile", "enricher-local", "import messaging_service.enrichment"),
    "whatsapp-native": (".", "services/messaging/Dockerfile", "whatsapp-native", "import messaging_service.whatsapp_collector"),
    # ANYM-937: the session holder; its own context (whatsapp/whatsmeow only) and no Python, so the smoke is --version.
    "whatsapp-holder": ("whatsapp/whatsmeow", "whatsapp/whatsmeow/Dockerfile", "whatsapp-holder", "--version"),
    "gateway": (".", "services/gateway/Dockerfile", "", "import agent_gateway, platform_contracts.release"),
    "calendar": ("services/calendar", "services/calendar/Dockerfile", "", "import family_calendar"),
    "recordings": ("services/recordings", "services/recordings/Dockerfile", "", "import deedkit_recordings.app"),
    "calls": ("services/calls", "services/calls/Dockerfile", "", "import fastapi, pipecat"),
    "backup": (".", "services/messaging/Dockerfile", "backup",
               "import messaging_service.backup_restore_worker; import subprocess; "
               "[subprocess.run([tool, '--version'], check=True) for tool in ('age', 'pg_dump', 'pg_restore')]"),
    "http-router": (".", "services/release-router/Dockerfile", "", ""),
}
# The images the main push run publishes (scripts/publish_images.py TARGETS): preparation pulls these by verified
# digest instead of building them; `http-router` and `check-image` still build locally (ANYM-301 B1).
PUBLISHED = ("runtime", "indexer", "enricher-local", "whatsapp-native", "gateway", "calendar", "recordings", "calls", "backup")
REGISTRY = "ghcr.io/gosync-inc/deedkit-"
# The tokenless attestation verifier, built reproducibly from tools/attest-verify and installed by the owner;
# tests keep this hash equal to tools/attest-verify/attest-verify.linux-amd64.sha256.
VERIFIER = Path("/usr/local/libexec/deedkit/attest-verify")
VERIFIER_SHA256 = "2a71d64a594ce2d95d876b8164a210f14693ded6a3f48f83ed80ac2a2ff73640"
VERIFIER_RECORD = "tools/attest-verify/attest-verify.linux-amd64.sha256"
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



DEADLINE = None  # a time.monotonic() value: set only by `preflight_cleanup` and inference_worker_start.py, so every command of that pass or run is bounded by it


def run(argv, *, cwd=None, umask=-1, env=None, reason=False, timeout=1800, binary=False):
    # Never include command output in an exception: Compose can contain secrets. Only a tool that prints
    # nothing secret (the attestation verifier, git on the public source mirror) may pass its last stderr line on,
    # with `reason`.
    if DEADLINE is not None:
        if (left := DEADLINE - time.monotonic()) <= 0:
            raise Refused("the cleanup time budget is used; no further command starts")
        timeout = min(timeout, left)
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


def queue_verification(sha, get, native=False):
    """The merge queue run whose `checks / verify` and `CI gate` (and with `native`, `native / synthetic`) verified
    exactly this commit."""
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
    for required in (QUEUE_VERIFIED_JOB, "CI gate", *((NATIVE_JOB,) if native else ())):
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


def queue_chain(sha, get, queue=None, jobs=None, native=False):
    """The merge queue run that reused a full pull request run for this commit, and that pull request run.

    Returns (queue run, pull request run, pull request number, head branch). The queue run must have passed `CI gate`
    with `checks / verify` skipped after its pull request reuse job, and not by its docs-only decision; the pull
    request must have merged as exactly this commit, and the latest run of its head must have passed every
    `PULL_REQUEST_JOBS` job (with `native`, also `native / synthetic`). That run must be the one the reuse job's one passed `QUEUE_PR_REUSE_STEP` step names, at
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
    for name in (*PULL_REQUEST_JOBS, *((NATIVE_JOB,) if native else ())):
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
    reused = job_concluded(jobs, QUEUE_VERIFIED_JOB, "skipped")
    # A skipped native job counts only beside that reuse, and then the run that verified must show it passed.
    native_jobs = [job for job in jobs if job.get("name") in {NATIVE_JOB, "native"}]
    native = reused and len(native_jobs) == 1 and job_concluded(native_jobs, native_jobs[0]["name"], "skipped")
    for required in sorted(REQUIRED_JOBS):
        if required == NATIVE_JOB and native:
            continue
        if required == QUEUE_VERIFIED_JOB and reused:
            # The push run reused the queue's verification, or the pull request run the queue reused; GitHub, not
            # the push run, must show it passed.
            try:
                verified_by = queue = queue_verification(sha, get, native)
            except QueueSkipped as refused:
                try:
                    queue, verified_by, _, _ = queue_chain(sha, get, refused.queue, refused.jobs, native)
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
    if "backup" in value:
        backup_wait(value)  # ANYM-744: refused here, before any command writes
    seen = set()
    for project in value["projects"]:
        name = project["name"]
        if name in seen or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            raise Refused("invalid or duplicate Compose project")
        seen.add(name)
        if "sessions" in project:
            raise Refused("session_mark_invalid: session marks go on the services entries (ANYM-896)")
        project["services"], sessions = holder_marks(project)  # ANYM-896: `{service: target}` from here on
        if sessions:
            project["sessions"] = sessions
    if any("sessions" in project for project in value["projects"]) or {"session_window", "session_timezone"} & value.keys():
        holder_window(value)
    for project in value["projects"]:
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


def snapshot(project, *, stopped=False, labels=None, conditions=None, exited=()):
    """The project's Compose document and the identity of each selected service's container. Only rollback and a
    failed health wait (`stopped`) accept a container that exited or keeps restarting: that is when they run, and so
    do the session services named in `exited` (ANYM-896: a stopped holder is recreated at once).
    `labels`, a dict, receives each service's input label (ANYM-895), None unless the container runs steadily;
    `conditions` why it does not: stopped, restarting, paused or unhealthy (None when it runs steadily)."""
    document = json.loads(run(compose(project) + ["config", "--format", "json"]))
    running = {}
    for service in project["services"]:
        loose = stopped or service in exited
        ids = run(["docker", "ps", *(["--all", "--filter", "label=com.docker.compose.oneoff=False"] if loose else []),
                   "--filter", "label=com.docker.compose.project=" + project["name"],
                   "--filter", "label=com.docker.compose.service=" + service, "--format", "{{.ID}}"]).split()
        if len(ids) != 1 or service not in document["services"]:
            raise Refused("each selected service must already have exactly one running container")
        validate_runtime(document["services"][service])
        actual = json.loads(run(["docker", "inspect", ids[0]]))[0]
        condition = actual.get("State", {})
        if loose and not (condition.get("Running") and not condition.get("Paused")
                          or condition.get("Status") in ("restarting", "exited")):
            raise Refused("a selected container is neither running, restarting nor exited")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", actual["Image"]):
            raise Refused("running container has no immutable image identity")
        running[service] = {"container": ids[0], "image": actual["Image"], "settings": container_settings(actual)}
        if labels is not None:
            steady = condition.get("Running") and not condition.get("Restarting") and not condition.get("Paused")
            labels[service] = ((actual.get("Config") or {}).get("Labels") or {}).get(INPUT_LABEL) if steady else None
        if conditions is not None:
            health = (condition.get("Health") or {}).get("Status", "healthy") if "Health" in condition else "healthy"
            conditions[service] = ("stopped" if not condition.get("Running") else "restarting" if condition.get("Restarting")
                                   else "paused" if condition.get("Paused") else "unhealthy" if health != "healthy" else None)
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
    # A raw value parsed by Compose when a document is applied.
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [escaped(item) for item in value]
    if isinstance(value, dict):
        return {key: escaped(item) for key, item in value.items()}
    return value


def escaped_rendered(value):
    """`escaped` for a document Compose printed: `config` prints a literal `$` as `$$` (2.40.3 and 5.5.0), so it is
    decoded first and a dollar sign is never doubled."""
    if isinstance(value, str):
        return value.replace("$$", "$").replace("$", "$$")
    if isinstance(value, list):
        return [escaped_rendered(item) for item in value]
    if isinstance(value, dict):
        return {key: escaped_rendered(item) for key, item in value.items()}
    return value


# ANYM-773: release evidence keeps `env_file:` references, never a value (production.md, "Release evidence and env
# files"). Compose 2.40.3, the host's, lists `config --no-env-resolution` and ignores it, so the tool does not use it:
# `--no-interpolate` names the references on both versions, and the resolved document minus the keys of those files is
# what is written. The caller's round trip (default `config` of the written file) proves that nothing else changed.
EVIDENCE_VALUE_MINIMUM = 8
EVIDENCE_SECRET_NAME = re.compile(r"PASSWORD|PASSWD|SECRET|TOKEN|_KEY$|CREDENTIAL|DSN|DATABASE_URL|PRIVATE")
RENDER_VERSION = 2


class EvidenceRefused(Refused):
    """A refusal that names a code and at most a file path: never a value, key or content."""

    def __init__(self, code, path=""):
        super().__init__(f"{code}: {path}" if path else code)
        self.code = code


def check_env_references():
    """Before the first document of a run: this Compose names a synthetic env file without printing its value."""
    canary = os.urandom(16).hex()
    with tempfile.TemporaryDirectory(prefix="env-probe-") as scratch:
        os.chmod(scratch, 0o700)
        with os.fdopen(new_file(Path(scratch) / "probe.env"), "w") as handle:
            handle.write(f"DEEDKIT_PROBE={canary}\n")
        document = {"name": "deedkit-env-probe", "services": {"probe": {
            "image": "probe", "env_file": [{"path": str(Path(scratch) / "probe.env")}]}}}
        with os.fdopen(new_file(Path(scratch) / "compose.json"), "w") as handle:
            json.dump(document, handle)
        try:
            shown = run(["docker", "compose", "--profile", "*", "--project-name", "deedkit-env-probe", "--project-directory",
                         scratch, "-f", str(Path(scratch) / "compose.json"), "config", "--no-interpolate", "--format", "json"])
            entries = json.loads(shown)["services"]["probe"].get("env_file")
        except (Refused, ValueError, KeyError, TypeError, AttributeError):
            raise EvidenceRefused("compose_ignores_env_resolution") from None
    if canary in shown or not [path for path in reference_paths(entries) if Path(path).name == "probe.env"]:
        raise EvidenceRefused("compose_ignores_env_resolution")


def reference_entries(entries):
    entries = [entries] if isinstance(entries, (str, dict)) else entries or []
    return [{"path": entry} if isinstance(entry, str) else entry for entry in entries]


def reference_paths(entries):
    return [entry["path"] for entry in reference_entries(entries)]


def compose_stub(project, unit):
    """`unit` of a one-service document on stdin, rendered with the project's interpolation inputs and nothing on disk."""
    document = {"services": {"stub": {"image": "stub", **escaped(unit)}}}
    shown = run_input(compose({**project, "files": ["-"]}) + ["config", "--format", "json"], json.dumps(document).encode())
    return json.loads(shown)["services"]["stub"]


def absolute_reference(project, entry, raw):
    """The entry with its real absolute path. `raw` is the path as the source writes it: `--no-interpolate` alone puts the
    project directory before a path that starts with a variable. Compose interpolates it, and then resolves it, the way it does."""
    path = entry["path"]
    if "$" in path:
        document = {"services": {"stub": {"image": "stub", "labels": {"path": raw}}}}
        shown = run_input(compose({**project, "files": ["-"]}) + ["config", "--format", "json"], json.dumps(document).encode())
        path = json.loads(shown)["services"]["stub"]["labels"]["path"].replace("$$", "$")
    if not path.startswith("/"):
        path = posixpath.join(project["directory"], path)
    return {key: value for key, value in {**entry, "path": posixpath.normpath(path)}.items() if key in ("path", "required", "format")}


ENV_FILE_REFERENCE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")


def env_file_variables(project, entry, resolved=None):
    """What Compose itself reads from one env file, overridden keys included: its parser decides quoting, `export`, comments
    and `format: raw`. The service's resolved values of the variables the file refers to and does not define give it the
    context it has in the service. A file Compose still cannot read refuses: no value goes unchecked."""
    if not os.path.exists(entry["path"]):
        return {}
    text = Path(entry["path"]).read_text(errors="replace")
    defined = {line.partition("=")[0].strip().removeprefix("export ").strip() for line in text.splitlines() if "=" in line}
    available = (resolved or {}).get("environment") or {}
    context = {name: str(available[name]).replace("$$", "$") for name in set(ENV_FILE_REFERENCE.findall(text)) - defined
               if available.get(name) is not None}
    try:
        environment = compose_stub(project, {"env_file": [entry], **({"environment": context} if context else {})}).get("environment") or {}
    except (Refused, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        raise EvidenceRefused("env_value_in_evidence", entry["path"]) from None
    return {key: str(value).replace("$$", "$") for key, value in environment.items() if value is not None and key not in context}


def applied_values(project, references, resolved, literal):
    """(file path, key, decoded value) of every value of `references`, and of each key `resolved` got from them."""
    named = [(entry["path"], env_file_variables(project, entry, resolved)) for entry in references]
    return [*((path, key, value) for path, variables in named for key, value in variables.items()),
            *((next((path for path, variables in named if key in variables), references[0]["path"]), key, str(value).replace("$$", "$"))
              for key, value in (resolved.get("environment") or {}).items() if key not in literal and value is not None)]


def env_file_digest(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def document_strings(value, where=()):
    """Every decoded string value with its location; `env_file` holds the references themselves."""
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from document_strings(item, (*where, index))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key != "env_file":
                yield from document_strings(item, (*where, key))


def interpolation_files(project):
    """The files Compose interpolates this project from: `--env-file`, which replaces the project's `.env`, or that `.env`."""
    dotenv = Path(project["directory"]) / ".env"
    return list(project.get("env_files", [])) or ([str(dotenv)] if dotenv.is_file() else [])


def check_evidence(project, document, source, applied):
    """Refuses `document` when it holds a value read from an env file. The one exception is a string the source writes
    unchanged at the same location, under a key that does not look secret: that text comes from the source, not the file.
    An interpolation file holds paths, addresses and versions the document needs: only its secret-named values refuse,
    except a `*_FILE` key, which names a path."""
    found = list(document_strings(document))
    written = dict(document_strings(source))

    def occurs(value):
        return [where for where, text in found if value in text or value in text.replace("$$", "$")]

    for path, key, value in applied:
        if len(value) >= EVIDENCE_VALUE_MINIMUM:
            for where in occurs(value):
                if EVIDENCE_SECRET_NAME.search(key) or written.get(where) != dict(found)[where]:
                    raise EvidenceRefused("env_value_in_evidence", path)
    for path in interpolation_files(project):
        for key, value in env_file_variables(project, {"path": path}).items():
            if (EVIDENCE_SECRET_NAME.search(key) and not key.endswith("_FILE") and len(value) >= EVIDENCE_VALUE_MINIMUM
                    and occurs(value)):
                raise EvidenceRefused("env_value_in_evidence", path)


def evidence_source(project, *flags):
    return json.loads(run(compose(project) + ["config", "--no-interpolate", *flags, "--format", "json"]))


def evidence_document(project, resolved):
    """`resolved` as it may be written: each service names its env files and holds only the keys its source writes. Paths in
    the document stay in Compose's printed form, like every other string; the returned names are real paths."""
    source, document, entries, applied, raw = evidence_source(project), copy.deepcopy(resolved), {}, [], None
    for name, unit in document["services"].items():
        origin = source["services"].get(name, {})
        written = reference_entries(origin.get("env_file"))
        if not written:
            continue
        if raw is None and any("$" in entry["path"] for entry in written):
            raw = evidence_source(project, "--no-path-resolution")
        paths = reference_paths(raw["services"][name].get("env_file")) if raw is not None else [None] * len(written)
        references = [absolute_reference(project, entry, path) for entry, path in zip(written, paths, strict=True)]
        literal = set(origin.get("environment") or {})
        applied += applied_values(project, references, unit, literal)
        environment = {key: value for key, value in (unit.get("environment") or {}).items() if key in literal}
        if environment:
            unit["environment"] = environment
        else:
            unit.pop("environment", None)
        entries.update({entry["path"]: entry for entry in references})
        unit["env_file"] = [{**entry, "path": entry["path"].replace("$", "$$")} for entry in references]
    check_evidence(project, document, source, applied)
    return document, sorted(entries)


def check_env_files(project, item, receipt):
    """A file named by a selected service must be the one prepare read. A legacy receipt (no `render`) has inline values."""
    recorded = {entry["path"]: entry["sha256"] for entry in receipt["env_files"]}
    services = read(item["file"])["services"]
    for service in project["services"]:
        for path in reference_paths(services.get(service, {}).get("env_file")):
            path = path.replace("$$", "$")
            if path not in recorded or env_file_digest(path) != recorded[path]:
                raise Refused(f"production or prepared configuration changed since preparation: {path}")


def check_space(directory):
    if shutil.disk_usage(directory).free < MIN_FREE_BYTES:
        raise Refused(f"at least 15 GiB free is required on {directory} before obtaining production images; "
                      "`production.py images --remove` removes old release images")


CONTAINERD_ROOT = Path("/var/lib/containerd")
DOCKER_ROOT = Path("/var/lib/docker")
# A preparation writes about 100 MiB outside the image store; this floor leaves room for a migration on a database volume.
HOST_FREE_BYTES = 5 * 1024**3
# Warnings before the refusals, this far above each floor: `deploy.json`'s cleanup line, the journal and `status`.
DISK_WARNING_MARGIN = 10 * 1024**3


def check_host_space(directory):
    """The floor of a disk that receives receipts and checkouts, not image bytes."""
    if shutil.disk_usage(directory).free < HOST_FREE_BYTES:
        raise Refused(f"at least 5 GiB free is required on {directory} before a preparation; "
                      "this disk holds receipts and data volumes, `production.py images --remove` does not free it")


def containerd_image_store():
    """Whether Docker keeps images in containerd. Unknown is False: both disks then keep the full floor."""
    try:
        return ["driver-type", "io.containerd.snapshotter.v1"] in json.loads(
            run(["docker", "info", "--format", "{{json .DriverStatus}}"]))
    except Exception:
        return False


def check_image_space(docker_root, containerd=False, state=None):
    # The containerd image store keeps image content under containerd's root, often another disk than DockerRootDir.
    if containerd and CONTAINERD_ROOT.is_dir():
        check_space(CONTAINERD_ROOT)
        check_host_space(docker_root)
        return
    # Not positively the containerd store: the state directory keeps the full floor too.
    if state is not None:
        check_space(state)
    check_space(docker_root)
    if CONTAINERD_ROOT.is_dir():
        check_space(CONTAINERD_ROOT)


def disk_warnings():
    """One line per production disk close to its floor. Never raises: an unreadable or absent path warns of nothing."""
    found = []
    for path, level in ((CONTAINERD_ROOT, MIN_FREE_BYTES + DISK_WARNING_MARGIN), (DOCKER_ROOT, HOST_FREE_BYTES + DISK_WARNING_MARGIN)):
        try:
            free = shutil.disk_usage(path).free
        except OSError:
            continue
        if free < level:
            found.append(f"{path}: {round(free / 2**30, 2)} GiB free, warning below {level // 2**30} GiB")
    return found


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


# ANYM-895: a release recreates a service only when its inputs changed. Builds are not reproducible and every image
# carries its revision, so the comparison is over what the repository and host control, never over image IDs: the
# Dockerfile and .dockerignore content, the build arguments without the revision, the git objects (with their modes) of
# every path the target's stage chain copies, and the rendered service with the host files it reads. A part that cannot
# be read makes the digest None, and a service without a digest is always recreated. INPUT_VERSION changes with the rule,
# so every service is recreated once after it changes.
INPUT_VERSION = "i1:"
INPUT_LABEL = "io.deedkit.production.inputs"
REVISION_LABEL = "org.opencontainers.image.revision"
REVISION_ARGUMENTS = ("DEEDKIT_SOURCE_SHA", "VERSION_REF")
INPUT_PARTS = ("dockerfile", "recipe", "code", "migration", "compose", "env", "mounts", "secrets")
HOST_PARTS = ("env", "mounts", "secrets")
MESSAGING_PACKAGE = "services/messaging/messaging_service"


class Unreadable(Exception):
    """An input the digest cannot read or parse: the service is recreated."""


def dockerfile_lines(text):
    """The instructions of a Dockerfile: comments and parser directives dropped, continuation lines joined."""
    lines, current = [], ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if current and stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        lines.append((current + stripped).strip())
        current = ""
    if current.strip():
        lines.append(current.strip())
    return lines


def copy_sources(arguments):
    """The flags and source paths of a COPY or ADD instruction (its last argument is the destination). Options come
    first, also before the JSON form."""
    flags, rest = [], arguments.strip()
    while rest.startswith("--"):
        flag, _, rest = rest.partition(" ")
        flags.append(flag)
        rest = rest.strip()
    if rest.startswith("["):
        try:
            words = json.loads(rest)
        except ValueError as exc:
            raise Unreadable("a COPY in JSON form does not parse") from exc
    else:
        words = rest.split()
        if any(word.startswith("--") for word in words) or any(char in rest for char in "\"'"):
            raise Unreadable("a COPY with an option after a source or a quoted source")
    if len(words) < 2 or not all(isinstance(word, str) for word in words):
        raise Unreadable("a COPY without a source and a destination")
    return flags, words[:-1]


DOCKERFILE_INSTRUCTIONS = ("FROM", "ARG", "ENV", "LABEL", "WORKDIR", "USER", "RUN", "COPY", "ADD", "CMD", "ENTRYPOINT",
                           "EXPOSE", "HEALTHCHECK", "STOPSIGNAL", "VOLUME", "SHELL", "ONBUILD")


def dockerfile_stages(text):
    """Each stage of a Dockerfile in order: its name, the stage it starts FROM (None for an image), the stages its
    COPY --from names and the context paths it copies. ADD of a URL or an archive and anything not understood refuse.

    The reading is deliberately narrow (fail-safe, the service is then always recreated): only printable ASCII, single
    spaces as BuildKit reads any whitespace, no parser directive (`# syntax=`, `# escape=` change the parser), no heredoc
    or here-string anywhere, no quote in an option, and only the instructions listed below."""
    if re.search(r"[^\x20-\x7e\n]", text):
        raise Unreadable("a Dockerfile with a tab, a carriage return or a character outside printable ASCII")
    if "<<" in text:
        raise Unreadable("a Dockerfile with a heredoc or a here-string")
    for line in text.splitlines():
        if not line.strip():
            continue
        if not re.match(r"\s*#\s*[A-Za-z]+\s*=", line):
            break
        raise Unreadable("a Dockerfile with a parser directive")
    stages = []
    for line in dockerfile_lines(text):
        instruction, _, arguments = line.partition(" ")
        instruction = instruction.upper()
        if instruction not in DOCKERFILE_INSTRUCTIONS:
            raise Unreadable(f"an instruction the digest does not read: {instruction}")
        options = itertools.takewhile(lambda word: word.startswith("--"), arguments.split())  # the instruction's own
        if any(char in word for word in options for char in "\"'"):
            raise Unreadable(f"a {instruction} option with a quote")
        if instruction == "FROM":
            words = [word for word in arguments.split() if not word.startswith("--")]
            if not words or len(words) not in (1, 3) or len(words) == 3 and words[1].upper() != "AS":
                raise Unreadable("a FROM the digest cannot read")
            # Stage names are case-insensitive; an unnamed stage is known by its index only.
            name = words[2].lower() if len(words) == 3 else str(len(stages))
            known = {stage["name"] for stage in stages}
            stages.append({"name": name, "base": words[0].lower() if words[0].lower() in known else None, "from": [],
                           "copies": []})
        elif instruction in ("COPY", "ADD"):
            if not stages:
                raise Unreadable(f"{instruction} before FROM")
            flags, sources = copy_sources(arguments)
            origin = [flag.partition("=")[2].lower() for flag in flags if flag.startswith("--from=")]
            if origin:
                stages[-1]["from"].append(origin[0])
            elif instruction == "ADD" and any(re.match(r"[a-z]+://|git@", source) or re.search(r"\.(tar|gz|tgz|bz2|xz|zst)$", source)
                                              for source in sources):
                raise Unreadable("ADD of a URL or an archive")
            else:
                stages[-1]["copies"].extend(sources)
        elif instruction == "ONBUILD":
            raise Unreadable("ONBUILD")
        elif instruction == "RUN" and stages:
            for option in re.findall(r"--mount=(\S+)", arguments):
                values = dict(item.partition("=")[::2] for item in option.split(","))
                # A mount without `type` is a bind mount of the build context unless it names `from`.
                if values.get("type", "bind") == "bind" and "from" not in values:
                    raise Unreadable("RUN --mount of the build context")
                if "from" in values:  # a stage (or an image, which the Dockerfile content names) the RUN reads
                    stages[-1]["from"].append(values["from"].lower())
    if not stages:
        raise Unreadable("a Dockerfile without a stage")
    return stages


def stage_chain(stages, target):
    """The stages a target's image is built from: its stage, the stage it starts from and every COPY --from stage,
    transitively. `target` "" is the last stage. A COPY --from naming an image (not a stage) adds no context path; the
    image reference is part of the Dockerfile content, which the digest hashes whole."""
    named = {stage["name"]: stage for stage in stages} | {str(index): stage for index, stage in enumerate(stages)}
    start = stages[-1] if not target else named.get(target.lower())
    if start is None:
        raise Unreadable(f"stage {target!r} is not in the Dockerfile")
    chain, pending = [], [start]
    while pending:
        stage = pending.pop()
        if any(stage is seen for seen in chain):
            continue
        chain.append(stage)
        if stage["base"]:
            pending.append(named[stage["base"]])
        pending.extend(named[origin] for origin in stage["from"] if origin in named)
    return chain


def context_paths(context, sources):
    """The repository paths (POSIX, relative to the checkout) that `sources` of a build context select."""
    result = set()
    for source in sources:
        if source.startswith("/") or ".." in Path(source).parts or "$" in source:
            raise Unreadable(f"a COPY source the digest cannot place: {source}")
        joined = os.path.normpath(os.path.join(context, source)).replace(os.sep, "/")
        result.add("" if joined == "." else joined)
    return sorted(result)


def tree_entries(source):
    """Every file of the checked-out commit as `mode id path` (git ls-tree), so a mode change is a change too. `-z`:
    paths are NUL-separated and never quoted."""
    listing = run(["git", "-C", str(source), "ls-tree", "-r", "-z", "--full-tree", "HEAD"])
    entries = {}
    for line in listing.split("\0"):
        meta, _, path = line.partition("\t")
        mode, _, rest = meta.partition(" ")
        kind, _, object_id = rest.partition(" ")
        if path and kind in ("blob", "commit"):
            entries[path] = mode + " " + object_id
    if not entries:
        raise Unreadable("the release checkout lists no files")
    return entries


def selected_entries(entries, paths):
    """The tree entries under each copied path (a glob is matched as Docker matches it); a path that selects nothing is
    named, so a COPY of a file the commit lacks still changes the digest when the file appears."""
    chosen = {}
    for path in paths:
        pattern = any(char in path for char in "*?[")
        def under(name):
            if not path or name == path or name.startswith(path + "/"):
                return True
            # A pattern selects each matching file or directory, and a directory brings everything below it.
            parts = name.split("/")
            return pattern and any(fnmatch.fnmatchcase("/".join(parts[:end]), path) for end in range(1, len(parts) + 1))
        found = {name: value for name, value in entries.items() if under(name)}
        chosen.update(found or {path: "missing"})
    return chosen


def build_inputs(source, target, entries):
    """The repository part of a target's input digest: the Dockerfile and .dockerignore content, the recipe without
    the revision arguments and the git objects of every path its stage chain copies."""
    context, dockerfile, stage, _ = BUILDS[target]
    try:
        text = (Path(source) / dockerfile).read_text()
    except OSError as exc:
        raise Unreadable(f"{dockerfile} is not in the release checkout") from exc
    # Both ignore files and their presence: `<Dockerfile>.dockerignore` next to the Dockerfile takes precedence over
    # the context's `.dockerignore` (BuildKit).
    ignored = {}
    for ignore in (Path(source) / context / ".dockerignore", Path(source) / (dockerfile + ".dockerignore")):
        try:
            ignored[str(ignore.relative_to(source))] = hashlib.sha256(ignore.read_bytes()).hexdigest()
        except FileNotFoundError:
            ignored[str(ignore.relative_to(source))] = None
        except OSError as exc:
            raise Unreadable(f"{ignore} cannot be read") from exc
    copied = [path for item in stage_chain(dockerfile_stages(text), stage) for path in item["copies"]]
    arguments, _ = build_recipe(target, "0" * 40)
    arguments = {key: value for key, value in arguments.items() if key not in REVISION_ARGUMENTS}
    code = selected_entries(entries, context_paths(context, copied))
    # ANYM-895 criterion 4: an image that copies the messaging package names its migrations as a part of their own.
    migrations = {path: value for path, value in code.items() if path.startswith(MESSAGING_PACKAGE + "/migrations/")}
    copies_package = any(path.startswith(MESSAGING_PACKAGE + "/") for path in code)
    return {"dockerfile": digest({"dockerfile": hashlib.sha256(text.encode()).hexdigest(), "ignore": ignored}),
            "recipe": digest({"path": dockerfile, "stage": stage, "arguments": arguments}),
            "code": digest(code), "migration": digest(migrations) if copies_package else None}


# A read-only directory mount is configuration (gateway policy and keys, recordings configuration): hashed by content
# within these bounds, else the service is recreated.
INPUT_DIRECTORY_FILES = 2000
INPUT_DIRECTORY_BYTES = 64 << 20


def input_directory_digest(path):
    """The digest of a configuration directory: every entry's relative path, mode and content (a symlink by its
    target, never followed). Raises Unreadable beyond the bounds or on anything unreadable."""
    listing, total = {}, 0
    def refuse(exc):
        raise exc
    try:
        for root, directories, files in os.walk(path, onerror=refuse):
            directories.sort()
            for name in sorted(files) + directories:
                full = os.path.join(root, name)
                found = os.lstat(full)
                relative = os.path.relpath(full, path)
                if len(listing) >= INPUT_DIRECTORY_FILES:
                    raise Unreadable(f"{path} has too many entries to hash as configuration")
                if stat.S_ISLNK(found.st_mode):
                    listing[relative] = "link " + os.readlink(full)
                elif stat.S_ISDIR(found.st_mode):
                    listing[relative] = f"directory {found.st_mode:o}"
                elif stat.S_ISREG(found.st_mode):
                    total += found.st_size
                    if total > INPUT_DIRECTORY_BYTES:
                        raise Unreadable(f"{path} is too large to hash as configuration")
                    listing[relative] = f"{found.st_mode:o} " + hashlib.sha256(Path(full).read_bytes()).hexdigest()
                else:
                    raise Unreadable(f"{full} is neither a file, a directory nor a link")
    except OSError as exc:
        raise Unreadable(f"{path} cannot be read") from exc
    return "directory " + digest(listing)


def input_file_digest(path, *, configuration=False):
    """The sha256 of a regular host file the service reads. A directory counts by path only (data, sessions, spools),
    unless it is `configuration` (mounted read-only): then by content."""
    try:
        found = os.stat(path)
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        raise Unreadable(f"{path} cannot be read") from exc
    if stat.S_ISDIR(found.st_mode):
        return input_directory_digest(path) if configuration else "directory"
    if not stat.S_ISREG(found.st_mode):
        raise Unreadable(f"{path} is neither a file nor a directory")
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise Unreadable(f"{path} cannot be read") from exc


def host_inputs(document, service):
    """The host-side parts of one rendered service: digests of its env files, of its bind-mounted files and of the
    files of the top-level secrets and configs it uses. Digests only; never a value. Raises Unreadable."""
    unit = document["services"][service]
    env, mounts, secrets = {}, {}, {}
    for path in reference_paths(unit.get("env_file")):
        env[path] = input_file_digest(path.replace("$$", "$"))
    for volume in unit.get("volumes") or []:
        if isinstance(volume, dict) and volume.get("type") == "bind" and isinstance(volume.get("source"), str):
            mounts[volume["source"] + " -> " + str(volume.get("target"))] = input_file_digest(
                volume["source"], configuration=bool(volume.get("read_only")))
    for kind in ("secrets", "configs"):
        for entry in unit.get(kind) or []:
            name = entry.get("source") if isinstance(entry, dict) else entry
            top = (document.get(kind) or {}).get(name) or {}
            # The definition (which file, which variable) and the content: a switch to an equal file is a change too.
            secrets[kind + ":" + str(name)] = digest({"definition": top, "entry": entry, "content": input_file_digest(top["file"])
                                                 if isinstance(top.get("file"), str) else None})
    return {"env": digest(env), "mounts": digest(mounts), "secrets": digest(secrets)}


def service_definition(unit):
    """The rendered service without what a release sets (image, build, the revision and input labels)."""
    value = json.loads(json.dumps(unit))
    for key in ("image", "build"):
        value.pop(key, None)
    labels = value.get("labels")
    if isinstance(labels, dict):
        value["labels"] = {key: label for key, label in labels.items() if key not in (REVISION_LABEL, INPUT_LABEL)}
        if not value["labels"]:
            value.pop("labels")
    return value


def service_inputs(document, service, built):
    """`{digest, parts}` of one service: its target's build parts and its rendered definition and host files. `built` is
    the target's `build_inputs` or None when they could not be read; then the digest is None (always recreated)."""
    try:
        host = host_inputs(document, service)
    except Unreadable:
        host = None
    parts = {**(built or {}), "compose": digest(service_definition(document["services"][service])), **(host or {})}
    complete = built is not None and host is not None
    return {"digest": INPUT_VERSION + digest(parts) if complete else None,
            "parts": {name: parts.get(name) for name in INPUT_PARTS}}


def recorded_inputs(state, project_name, current=None):
    """`{service: {container, settings, digest, parts, revision}}` the current applied release recorded for a project's
    services; empty without such a release."""
    if current is None:
        current = read(state / "current.json") if (state / "current.json").exists() else {}
    if current.get("status") != "applied":
        return {}
    for item in (current.get("rollback") or {}).get("projects") or []:
        if item.get("name") == project_name:
            inputs = item.get("inputs") or {}
            return {service: {**inputs[service], **identity, "revision": (item.get("kept") or {}).get(service, current["sha"])}
                    for service, identity in (item.get("running") or {}).items() if service in inputs}
    return {}


def keep_decision(service, wanted, identity, label, recorded):
    """Why `service` is recreated (the changed parts, or `unlabeled`), or None when its running container may stay:
    the candidate's digest is known, the container carries it as its label, and the current release recorded that very
    container with the same settings and digest (a label edited by hand matches no record)."""
    if wanted["digest"] is None:
        return ["unreadable"]
    if not recorded or label is None:
        return ["unlabeled"]
    if (label != wanted["digest"] or recorded.get("digest") != label or recorded.get("container") != identity["container"]
            or recorded.get("settings") != identity["settings"] or recorded.get("image") != identity["image"]):
        changed = [name for name in INPUT_PARTS if (recorded.get("parts") or {}).get(name) != wanted["parts"].get(name)]
        return changed or ["unlabeled"]
    return None


def names_revision(image, sha, dockerfile, recipe=None):
    config = json.loads(run(["docker", "image", "inspect", image]))[0]["Config"]
    labels = config.get("Labels") or {}
    return (labels.get("org.opencontainers.image.revision") == sha
            and (recipe is None or (labels.get("io.deedkit.production.build") == recipe
                                    and labels.get("org.opencontainers.image.source") == "https://github.com/" + REPOSITORY))
            and (dockerfile != "services/messaging/Dockerfile"
                 or "DEEDKIT_SOURCE_SHA=" + sha in (config.get("Env") or [])))


def check_verifier(path=None):
    """The installed verifier, or for `self-test --verifier` a staged build, is the pinned one (ANYM-618)."""
    path = path or VERIFIER
    try:
        info = path.lstat()
        content = path.read_bytes() if stat.S_ISREG(info.st_mode) else b""
    except OSError:
        info, content = None, b""
    if (info is None or not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
            or info.st_mode & 0o022 or hashlib.sha256(content).hexdigest() != VERIFIER_SHA256):
        raise Refused(f"the attestation verifier {path} is missing, writable by others or not the pinned build")


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
        elif target == "whatsapp-holder":
            run(isolated + ["/usr/local/bin/anymsg-whatsmeow", image, smoke])
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
    check_host_space(state)
    docker_root = run(["docker", "info", "--format", "{{.DockerRootDir}}"]).strip()
    if not docker_root or not Path(docker_root).is_dir():
        raise Refused("preparation requires the local production Docker engine")
    check_image_space(docker_root, containerd_image_store(), state)
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
    check_host_space(state)
    docker_root = run(["docker", "info", "--format", "{{.DockerRootDir}}"]).strip()
    if not docker_root or not Path(docker_root).is_dir():
        raise Refused("preparation requires the local production Docker engine")
    check_image_space(docker_root, containerd_image_store(), state)
    evidence = promotable(sha)
    release = state / sha
    if release.exists():
        raise Refused("candidate directory already exists; inspect it before a new preparation")
    check_env_references()
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
    snapshots, env_files = [], {}
    for project in settings["projects"]:
        labels, conditions = {}, {}
        document, running = snapshot(project, labels=labels, conditions=conditions, exited=set(project.get("sessions") or ()))
        reference, named = evidence_document(project, document)
        env_files.update({path: env_file_digest(path) for path in named})
        snapshots.append({"project": project, "before": document, "reference": reference, "running": running,
                          "labels": labels, "conditions": conditions})
    # ANYM-914: the lock directory exists before the inputs below are hashed and before apply stops anything.
    provide_session_locks(((service, item["before"]["services"][service]) for item in snapshots
                           for service in item["project"]["services"]),
                          required=(source / SESSION_CLAIM).is_file())
    images = {}
    targets = sorted({target for project in settings["projects"] for target in project["services"].values()})
    # Every CI-published target is resolved and attested before the first pull.
    verified = {target: published(target, sha) for target in published_targets(settings)}
    for target in targets:
        images[target] = build_image(source, sha, target, verified=verified.get(target))
    # Recheck after potentially long image builds. No production command ran yet.
    evidence = promotable(sha)
    # ANYM-895: the repository part of each target's input digest. Any failure of the digest (unreadable, unparsed or
    # unexpected) recreates the services it concerns, as every release did before; it never refuses the release.
    try:
        entries = tree_entries(source)
    except Exception:
        entries = None
    built = {}
    for target in targets:
        try:
            built[target] = build_inputs(source, target, entries) if entries is not None else None
        except Exception:
            built[target] = None
    current = read(state / "current.json") if (state / "current.json").exists() else None
    projects = []
    for item in snapshots:
        project, before = item["project"], item["before"]
        try:
            recorded = recorded_inputs(state, project["name"], current or {})
        except Exception:
            recorded = {}
        inputs, reasons, kept = {}, {}, {}
        for service, target in project["services"].items():
            try:
                inputs[service] = service_inputs(before, service, built[target])
                why = keep_decision(service, inputs[service], item["running"][service], item["labels"].get(service),
                                    recorded.get(service))
            except Exception:
                inputs[service] = {"digest": None, "parts": dict.fromkeys(INPUT_PARTS)}
                why = ["unreadable"]
            if why is None:
                kept[service] = {"container": item["running"][service]["container"],
                                 "image": item["running"][service]["image"], "revision": recorded[service]["revision"]}
            else:
                reasons[service] = why
        def edited(document):
            result = json.loads(json.dumps(document))
            for service in result["services"].values():
                service.pop("build", None)
            for service, target in project["services"].items():
                unit = result["services"][service]
                labels = unit.setdefault("labels", {})
                if service in kept:  # the definition its running container was created from, never `up` here
                    unit["image"] = kept[service]["image"]
                    labels[REVISION_LABEL] = kept[service]["revision"]
                    labels[INPUT_LABEL] = inputs[service]["digest"]
                    continue
                unit["image"] = images[target]
                labels[REVISION_LABEL] = sha
                if inputs[service]["digest"] is not None:
                    labels[INPUT_LABEL] = inputs[service]["digest"]
            return result
        # The resolved candidate stays in memory: the written file names the env files and must read back as exactly it.
        candidate = edited(before)
        path = release / (project["name"] + ".json")
        write(path, escaped_rendered(edited(item["reference"])))
        # Verify that serializing/re-reading did not change secrets, mounts or ports.
        reread = json.loads(run(["docker", "compose", "--profile", "*", "-f", str(path), "config", "--format", "json"]))
        if reread != candidate:
            raise Refused("resolved Compose snapshot did not round-trip exactly")
        rollback = json.loads(json.dumps(item["reference"]))
        for unit in rollback["services"].values():
            unit.pop("build", None)
        for service, identity in item["running"].items():
            rollback["services"][service]["image"] = identity["image"]
        previous = release / (project["name"] + ".before.json")
        write(previous, escaped_rendered(rollback))
        # Bound here, so a snapshot changed before apply can never become the rollback baseline.
        projects.append({"name": project["name"], "file": str(path), "digest": digest(read(path)),
                         "before_digest": digest(before), "running": item["running"],
                         "before_file_digest": digest(read(previous)), "inputs": inputs, "kept": kept,
                         "recreated": reasons, **({"session": session} if (session := holder_prepared(
                             project, item, reasons, recorded)) else {})})
    # What current.json said at preparation: rollback of a release that failed its health wait requires it unchanged.
    receipt = {"version": 1, "render": RENDER_VERSION, "env_files": [{"path": path, "sha256": env_files[path]} for path in sorted(env_files)],
               "sha": sha, "ci": evidence, "images": images,
               "references": {target: value["reference"] for target, value in verified.items()},
               "config_digest": settings_digest(settings), "current_digest": digest(current), "projects": projects}
    write(release / "prepared.json", receipt)
    return receipt


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# The host backup runner (BACKUP.md) is no image and no deploy tool: `deploy` installs it together with the backup
# image every runner.json pins (`install_backup_runner`, owner 11-04); a manual `apply` refuses while the installed file
# is not the release's, so a merged runner fix never stays off the host unnoticed. A host without the file runs no
# backup and is not checked.
BACKUP_RUNNER = Path("/opt/personal-os-probe/ops/backup_runner.py")
BACKUP_RUNNER_SOURCE = "services/messaging/messaging_service/backup_runner.py"
# W5-9: a release whose runner keeps its recordings and calls profiles in a module beside it ships that file too; the
# host program loads it from its own directory (`backup_profiles.py`, or `.backup_profiles.py.<sha>.tmp` beside the
# staged `.backup_runner.py.<sha>.tmp` its `--check` runs as). A release without the file installs the runner alone.
BACKUP_PROFILES_SOURCE = "services/messaging/messaging_service/backup_profiles.py"
# Messaging's runner lock: messaging's run and the calendar's (its `host_lock`, ANYM-608) hold it. A manual `deploy`,
# `apply` and the standalone HTTP `promote` wait for a running backup (ANYM-744) and then hold it for their run;
# `deploy --auto` runs under the unit's `flock` wrapper on the same file (DEEDKIT_BACKUP_LOCK).
BACKUP_LOCK = Path("/opt/personal-os-probe/backups/.runner.lock")
ROTATION_MARKER = Path("/opt/deedkit/calendar-backup/rotation/rotation.unsettled")
# ANYM-744: the bound of that wait (`backup.wait_seconds` of the plan A configuration, `--backup-wait` of the standalone
# promote), its maximum (the runner's own `busy_wait_seconds` maximum, BACKUP.md), the poll interval and one
# `systemctl` probe's timeout.
BACKUP_WAIT, BACKUP_WAIT_MAX, BACKUP_POLL, BACKUP_PROBE = 2700, 7200, 15, 60
EX_TEMPFAIL = 75  # a backup is running: try again later (the autodeploy unit's SuccessExitStatus)


def backup_lock_path():
    return Path(os.environ.get("DEEDKIT_BACKUP_LOCK") or BACKUP_LOCK)


def unsettled_marker():
    """The calendar runner's `<host_lock>.unsettled` marker when it exists: its worker may still run although no
    process holds the lock (a failed recovery, a killed runner). Its `--recover-stop` or next run removes it."""
    path = Path(f"{backup_lock_path()}.unsettled")
    return path if os.path.lexists(path) else None


def unsettled_backup():
    if path := unsettled_marker():
        return f"backup_running: a calendar backup worker is not verified stopped ({path}); its runner settles it"
    return None


def rotation_running():
    """The reason a deploy waits while scripts/calendar_rotate.py has a rotation open (ANYM-784), or None. The marker
    outlives its process: a rotation killed half way holds new passwords that the running services may not have yet."""
    if os.path.lexists(ROTATION_MARKER):
        return f"rotation_running: a calendar password rotation is unsettled ({ROTATION_MARKER}); calendar_rotate.py recover or finish settles it"
    return None


class BackupRunning(Busy):
    """A backup runs (its lock, unit or unsettled marker) and the bound passed: exit 75, nothing was written."""

    code = "backup_running"

    def __init__(self, unit, waited_seconds, since=None, detail=None):
        super().__init__(detail or f"backup_running: {unit} is running")
        self.unit, self.waited_seconds, self.since = unit, waited_seconds, since

    def line(self):
        return {"status": "busy", "code": self.code, "unit": self.unit, "waited_seconds": self.waited_seconds,
                "since": self.since}


def check_backup_wait(value):
    if type(value) is not int or not 0 <= value <= BACKUP_WAIT_MAX:
        raise Refused(f"backup.wait_seconds {value!r} is not an integer from 0 to {BACKUP_WAIT_MAX} seconds")
    return value


def backup_wait(settings):
    """`backup.wait_seconds` of the plan A configuration (default BACKUP_WAIT), or a refusal naming the key, the value
    and the range. The section is outside `settings_digest`: adding it changes no prepared release."""
    section = settings.get("backup")
    if section is None:
        return BACKUP_WAIT
    if not isinstance(section, dict) or set(section) - {"wait_seconds"}:
        raise Refused("backup must be an object with only wait_seconds")
    return check_backup_wait(section["wait_seconds"]) if "wait_seconds" in section else BACKUP_WAIT


def open_backup_lock():
    """The runner lock's descriptor, or None on a host without the runner and without the lock's directory (no backup
    runs there); a missing directory named by DEEDKIT_BACKUP_LOCK or beside an installed runner refuses."""
    path = backup_lock_path()
    try:
        return os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except FileNotFoundError:
        if path.parent.exists() or os.environ.get("DEEDKIT_BACKUP_LOCK") or os.path.lexists(BACKUP_RUNNER):
            raise Refused(f"the backup runner's lock {path} cannot be opened: its directory is missing") from None
        return None


@contextmanager
def backup_lock():
    """Hold the backup runner's lock, or refuse `Busy` while a backup holds it (the single check, bound 0)."""
    fd = open_backup_lock()
    if fd is None:
        yield
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy(f"backup_running: a backup holds {backup_lock_path()}") from exc
        yield
    finally:
        os.close(fd)


def backup_unit_busy(expired=lambda: False):
    """`(unit, state)` of the first backup unit that is not positively `inactive` or `failed` (an unreadable one is
    busy), read the way the producer pin reads it, or None when every one is idle. After every probe `expired()` ends
    the scan: the bound passed before idle was confirmed."""
    for unit in BACKUP_UNITS:
        try:
            state = run(["systemctl", "show", "-P", "ActiveState", unit], timeout=BACKUP_PROBE).strip()
        except Exception:
            return unit, "unreadable"
        if state not in ("inactive", "failed"):
            return unit, state or "unreadable"
        if expired():
            return unit, "not confirmed idle within the bound"
    return None


def backup_since(unit):
    """When `unit` last started, from systemd, or None; read once, when the wait gives up."""
    try:
        return run(["systemctl", "show", "-P", "InactiveExitTimestamp", unit], timeout=10).strip() or None
    except Exception:
        return None


def backup_busy(expired=lambda: False):
    """`(name, detail, unit)` while a backup unit or the unsettled marker says a backup may run; `unit` is the unit to
    ask for a start time, or None. The marker (a file stat) is read before the units, so a bound that ends the unit scan
    still names it, and again after them: a runner can create it while the units are read."""
    if marker := unsettled_marker():
        return str(marker), unsettled_backup(), None
    if busy := backup_unit_busy(expired):
        return busy[0], f"backup_running: {busy[0]} is {busy[1]}", busy[0]
    if marker := unsettled_marker():
        return str(marker), unsettled_backup(), None
    return None


def backup_gate(settings, bound):
    """The caller's wait for a running backup with `bound` seconds (backup_wait, `--backup-wait`): the deadline is
    taken here, and a bound of 0 is the single non-waiting check."""
    return wait_backup_idle(settings, time.monotonic() + bound, single=bound == 0)


@contextmanager
def wait_backup_idle(settings, deadline, *, single=False):
    """Wait until no backup runs, then hold the runner lock for the caller's run; yields the seconds waited. Idle is
    positive: the lock is free, every BACKUP_UNITS unit reads `inactive` or `failed` and the `.unsettled` marker is
    absent, read again with the lock held (a runner can create the marker and die between the first read and the
    lock). The lock is probed first, so a held lock refuses before any `systemctl` runs, and nothing is held while
    sleeping or reading units for the first time. Polls every BACKUP_POLL seconds. The deadline (time.monotonic) is
    checked after every probe, also before admission, so it is overshot by at most one probe and a release is never
    admitted after it; `single` (the caller's bound is 0, see backup_gate) is the one non-waiting check, which no
    deadline cuts short. A deadline that passed before the call is not `single`. At the bound:
    BackupRunning, never an unlocked run. `settings` mirrors wait_for_calls; the bound comes from the caller. A host
    without a backup runner skips the wait (open_backup_lock)."""
    fd = open_backup_lock()
    if fd is None:
        yield 0
        return
    started, said, path = time.monotonic(), False, backup_lock_path()
    held = str(path), f"backup_running: a backup holds {path}", None

    def expired():
        return not single and time.monotonic() >= deadline

    def take():
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    try:
        while True:
            if not take():
                busy = held
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)  # only a probe: no candidate lock is held while units are read
                if (busy := backup_busy(expired)) is None:
                    if not take():
                        busy = held
                    elif (busy := backup_busy(expired)) is None:
                        if not expired():
                            break
                        busy = str(path), "backup_running: idle was not confirmed within the bound", None
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
            now = time.monotonic()
            if single or now >= deadline:
                raise BackupRunning(busy[0], int(now - started), backup_since(busy[2]) if busy[2] else None, busy[1])
            if not said:
                print("backup check: waiting for " + busy[1].removeprefix("backup_running: "), file=sys.stderr, flush=True)
                said = True
            time.sleep(max(0, min(BACKUP_POLL, deadline - now)))
        waited = int(time.monotonic() - started)
        if said:
            print(f"backup check: idle after {waited} s", file=sys.stderr, flush=True)
        yield waited
    finally:
        os.close(fd)


def git_blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def runner_blobs(source, sha):
    """`(installed, expected)`: the git blob of the host backup runner and of `sha`'s; None on a host without it."""
    if not os.path.lexists(BACKUP_RUNNER):
        return None
    expected = run(["git", "-C", str(source), "rev-parse", f"{sha}:{BACKUP_RUNNER_SOURCE}"]).strip()
    try:
        # The file itself, never a link to another one.
        fd = os.open(BACKUP_RUNNER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise Refused(f"the backup runner {BACKUP_RUNNER} is not a regular file")
            content = handle.read()
    except OSError as exc:
        raise Refused(f"cannot read the backup runner {BACKUP_RUNNER}: {type(exc).__name__}") from exc
    return git_blob(content), expected


def profiles_blobs(source, sha):
    """`(installed, expected)` of the runner's profiles module (BACKUP_PROFILES_SOURCE) beside the host runner, the
    installed one None while the file is absent; None when `sha` has no such module."""
    if BACKUP_PROFILES_SOURCE not in run(["git", "-C", str(source), "ls-tree", "-z", "--name-only", sha,
                                          BACKUP_PROFILES_SOURCE]).split("\0"):
        return None
    expected = run(["git", "-C", str(source), "rev-parse", f"{sha}:{BACKUP_PROFILES_SOURCE}"]).strip()
    path = BACKUP_RUNNER.with_name(Path(BACKUP_PROFILES_SOURCE).name)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, expected
    except OSError as exc:
        raise Refused(f"cannot read the backup runner's {path}: {type(exc).__name__}") from exc
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise Refused(f"the backup runner's {path} is not a regular file")
        return git_blob(handle.read()), expected


def check_backup_runner(source, sha):
    if (found := runner_blobs(source, sha)) is None:
        return
    installed, expected = found
    if installed != expected:
        raise Refused(f"the backup runner {BACKUP_RUNNER} is blob {installed[:12]}, not {sha[:12]}'s {expected[:12]}; "
                      "install it with its backup image (production.md, Backup runner)")
    if (profiles := profiles_blobs(source, sha)) and profiles[0] != profiles[1]:
        raise Refused(f"the backup runner's profiles beside {BACKUP_RUNNER} are blob {(profiles[0] or 'absent')[:12]}, "
                      f"not {sha[:12]}'s {profiles[1][:12]}; install them with its backup image (production.md, Backup runner)")


def expected_image(receipt, item, service, target):
    """The image a service runs after the release: the prepared one, or the running one for a kept service."""
    kept = (item.get("kept") or {}).get(service)
    return kept["image"] if kept else receipt["images"][target]


def restore_entry(item, document, running):
    """One project's rollback data after an apply; `kept` names each kept service with the release its digest names."""
    return {"name": item["name"], "services": sorted(item["running"]), "before_file_digest": item["before_file_digest"],
            "after_digest": digest(document), "running": running, "inputs": item.get("inputs") or {},
            "kept": {service: value["revision"] for service, value in (item.get("kept") or {}).items()}}


def boot_id():
    """The host's boot ID (criterion 8 of ANYM-895 tells a reboot from a recreate), None where it cannot be read."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


def service_outcomes(receipt):
    """`{project: {service: {"kept": {container, revision}} | {"recreated": [changed parts]} | {"deferred": {parts,
    next_window, line, container, revision}}}}` (ANYM-896: a deferred session service): digests only."""
    return {item["name"]: {**{service: {"recreated": why} for service, why in (item.get("recreated") or {}).items()},
                           **{service: {"kept": {"container": value["container"], "revision": value["revision"]}}
                              for service, value in (item.get("kept") or {}).items()},
                           **{service: {"deferred": {**value, "container": item["kept"][service]["container"],
                                                     "revision": item["kept"][service]["revision"]}}
                              for service, value in (item.get("deferred") or {}).items()}}
            for item in receipt["projects"] if "inputs" in item}


def check_inputs(project, item, document):
    """Refuse with `inputs_changed_since_prepare` when a host file a service reads hashes otherwise than at prepare."""
    for service, wanted in (item.get("inputs") or {}).items():
        if wanted["digest"] is None or service in (item.get("deferred") or {}):  # ANYM-896: the running inputs
            continue
        try:
            host = host_inputs(document, service)
        except Unreadable:
            host = None
        if host != {part: wanted["parts"][part] for part in HOST_PARTS}:
            raise Refused(f"inputs_changed_since_prepare: a host file {project['name']}/{service} reads changed "
                          "since preparation; prepare again")


def steady(actual):
    """Whether a container runs steadily: running, neither restarting nor paused, and healthy if it has a healthcheck."""
    condition = actual.get("State") or {}
    return bool(condition.get("Running") and not condition.get("Restarting") and not condition.get("Paused")
                and ("Health" not in condition or (condition["Health"] or {}).get("Status") == "healthy"))


def check_kept(release, sha, receipt, evidence, touched, project, item):
    """After a project's settle and again after the last project: every service the release kept still runs steadily
    (healthy, when it has a healthcheck); otherwise the release is `unhealthy`, like a failed replacement."""
    for service in item.get("kept") or {}:
        actual = json.loads(run(["docker", "inspect", item["running"][service]["container"]]))[0]
        if not steady(actual):
            unhealthy(release, sha, receipt, evidence, touched,
                      Refused(f"kept {project['name']}/{service} is not running steadily"))


# ANYM-914: one client per provider session. The session services' directories are the bind sources ending in `-session`;
# every opener takes its lock in the one host directory mounted at SESSION_LOCKS, which the collectors' user owns.
SESSION_LOCKS = "/run/deedkit/session-locks"
SESSION_OWNER = (10001, 10001)  # deploy/compose.collectors.yaml and compose.whatsapp.yaml `user: "10001:10001"`
SESSION_CLAIM = MESSAGING_PACKAGE + "/session_claim.py"
SESSION_GRACE_S = 20  # above the collectors' 15 s disconnect bound; the overlays set 25 s
SESSION_SCAN_DEPTH, SESSION_SCAN_ENTRIES = 4, 20000  # foreign_session_opener: levels and entries per foreign mount
CLAIM_RECORD = "session-claims.json"  # the first release carrying SESSION_CLAIM that this state recorded `applied`


def binds(unit):
    return [(volume["source"], volume["target"]) for volume in unit.get("volumes") or []
            if isinstance(volume, dict) and volume.get("type") == "bind" and str(volume.get("source", "")).startswith("/")]


class ScanBudget(Exception):
    """A foreign mount too large to search within SESSION_SCAN_ENTRIES: ownership cannot be established."""


def session_store(path, depth=2, budget=None):
    """Whether a host path holds a session store: it or an entry at most `depth` levels below is a Telegram `*.session`
    file, a WhatsApp `auth.sqlite` or a whatsapp-web.js profile directory (`session`, `session-*`). Unreadable counts.
    `budget`, a one-item list, counts the entries left; it raises ScanBudget when it runs out."""
    name = os.path.basename(path)
    if name.endswith(".session") or name == "auth.sqlite" or name != "session-locks" and re.fullmatch(r"session(-.*)?", name):
        return True
    if depth == 0 or os.path.islink(path) or not os.path.isdir(path):
        return False
    try:
        with os.scandir(path) as entries:
            found = [entry.path for entry in entries]
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    if budget is not None:
        budget[0] -= len(found)
        if budget[0] < 0:
            raise ScanBudget(path)
    return any(session_store(entry, depth - 1, budget) for entry in found)


def under(path, roots):
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def started_units(settings, receipt, documents):
    """(project, service, unit) of each service this apply stops and starts again: selected and not kept (ANYM-895)."""
    return [(project, service, documents[project["name"]]["services"][service])
            for project, item in zip(settings["projects"], receipt["projects"], strict=True)
            for service in project["services"] if service not in (item.get("kept") or {})]


def check_session_openers(settings, documents, started):
    """`foreign_session_opener` (ANYM-914 criterion 6): before a release starts a session service, no running, paused or
    restarting container mounts a configured session directory, or any mount source (bind or volume) holding a session
    store, unless it is that directory's Compose service container (not a one-off `run`, not another project, not a
    copied directory). A configured service's own mounts are DeedKit's data paths (searched two levels, as before); every
    other container's mounts are searched SESSION_SCAN_DEPTH levels outside those paths, and one too large to search
    within SESSION_SCAN_ENTRIES refuses too (fail closed) unless `session_scan_exempt` in the production settings names
    it: absolute host paths an agent read and found to hold no session store."""
    if not any(os.path.basename(source).endswith("-session") for _, _, unit in started for source, _ in binds(unit)):
        return
    owners, configured = {}, set()
    for project in settings["projects"]:
        for service, unit in documents[project["name"]]["services"].items():
            configured.add((project["name"], service))
            for source, _ in binds(unit):
                if os.path.basename(source).endswith("-session"):
                    owners.setdefault(os.path.realpath(source), set()).add((project["name"], service))
    exempt = [os.path.realpath(path) for path in settings.get("session_scan_exempt") or []
              if isinstance(path, str) and path.startswith("/")]
    containers = run(["docker", "ps", "--all", "--quiet", "--no-trunc"]).split()
    found = [actual for actual in (json.loads(run(["docker", "inspect", *containers])) if containers else [])
             if any((actual.get("State") or {}).get(key) for key in ("Running", "Paused", "Restarting"))]
    def who(actual):
        labels = (actual.get("Config") or {}).get("Labels") or {}
        return (None if labels.get("com.docker.compose.oneoff") == "True" else
                (labels.get("com.docker.compose.project"), labels.get("com.docker.compose.service")))
    # DeedKit's data paths: what the configured services' own containers mount, but a session directory.
    data = {os.path.realpath(mount.get("Source")) for actual in found if who(actual) in configured
            for mount in actual.get("Mounts") or [] if mount.get("Source")} - set(owners)
    for actual in found:
        owner, name = who(actual), str(actual.get("Name", "")).lstrip("/")
        condition = "paused" if (actual.get("State") or {}).get("Paused") else "running"
        for mount in actual.get("Mounts") or []:
            if not mount.get("Source") or mount.get("Type") not in ("bind", "volume"):
                continue
            source = os.path.realpath(mount["Source"])
            if source in owners:
                if owner in owners[source]:
                    continue
                what = "a configured session directory"
            elif owner in configured:
                if mount.get("Type") != "bind" or not session_store(source):
                    continue
                what = "a session store"
            else:
                if under(source, data) or under(source, exempt):
                    continue
                try:
                    if not session_store(source, SESSION_SCAN_DEPTH, [SESSION_SCAN_ENTRIES]):
                        continue
                    what = "a session store"
                except ScanBudget:
                    what = (f"a mount too large to search for a session store (over {SESSION_SCAN_ENTRIES} entries "
                            f"within {SESSION_SCAN_DEPTH} levels; read it, then list it in session_scan_exempt)")
            raise Refused(f"foreign_session_opener: container {name} ({str(actual.get('Id', ''))[:12]}, {condition}) "
                          f"mounts {source}, {what}, and is not that directory's Compose service container; stop or "
                          "remove it before the release (two clients on one session break it)")


def grace_seconds(value):
    """Compose's `stop_grace_period` (`25s`, `1m30s`, or a number of seconds) in seconds; None when absent or unread."""
    if isinstance(value, (int, float)):
        return float(value)
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|us|ns|h|m|s)", str(value or ""))
    if not parts or "".join(a + b for a, b in parts) != str(value):
        return None
    scale = {"h": 3600, "m": 60, "s": 1, "ms": 1e-3, "us": 1e-6, "ns": 1e-9}
    return sum(float(a) * scale[b] for a, b in parts)


def provide_session_locks(named, required=False):
    """ANYM-914 criterion 3: the host directory these units mount at SESSION_LOCKS exists. Created once 0700 for
    SESSION_OWNER, by prepare before it hashes the inputs (a directory that appears changes them) and checked again by
    apply before its first stop; an existing one is never changed: refused unless it is that owner's private directory
    (no symlink, no group or other access). `required` (the release carries SESSION_CLAIM): every unit with a session
    directory bind also binds SESSION_LOCKS and stops with a grace above the 15 s disconnect bound, else
    `session_locks_unmounted` before anything stops (the host's Compose files are its own; a release never edits them)."""
    named = list(named)
    units = [unit for _, unit in named]
    if required:
        for service, unit in named:
            if not any(os.path.basename(source).endswith("-session") for source, _ in binds(unit)):
                continue
            if not any(posixpath.normpath(target) == SESSION_LOCKS for _, target in binds(unit)):
                raise Refused(f"session_locks_unmounted: session service {service} "
                              f"does not bind {SESSION_LOCKS}; install the host Compose change of this release first "
                              "(production.md, Session claim guards); nothing was stopped")
            if (grace_seconds(unit.get("stop_grace_period")) or 0) < SESSION_GRACE_S:
                raise Refused(f"session_locks_unmounted: session service {service} "
                              f"has stop_grace_period {unit.get('stop_grace_period')!r}, below {SESSION_GRACE_S}s; "
                              "install the host Compose change of this release first; nothing was stopped")
    for path in sorted({source for unit in units for source, target in binds(unit)
                        if posixpath.normpath(target) == SESSION_LOCKS}):
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise Refused(f"session_locks_unsafe: {path} cannot be created ({type(exc).__name__})") from exc
        else:
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fchown(descriptor, *SESSION_OWNER)
                    os.fchmod(descriptor, 0o700)
                finally:
                    os.close(descriptor)
            except OSError as exc:
                raise Refused(f"session_locks_unsafe: {path} was created but not given to "
                              f"{SESSION_OWNER[0]}:{SESSION_OWNER[1]} 0700 ({type(exc).__name__})") from exc
        found = os.lstat(path)
        if (not stat.S_ISDIR(found.st_mode) or found.st_mode & 0o077 or found.st_uid != SESSION_OWNER[0]):
            raise Refused(f"session_locks_unsafe: {path} must be a directory (no symlink) of uid {SESSION_OWNER[0]} "
                          "with no group or other access; it is left as it is")


def claims_recorded(state):
    """The release that first ran session claims here, recorded `applied` (healthy), or None. A release applied by an
    older tool counts when the current one's source carries SESSION_CLAIM."""
    if (state / CLAIM_RECORD).exists():
        return read(state / CLAIM_RECORD)
    current = read(state / "current.json") if (state / "current.json").exists() else {}
    if current.get("status") == "applied" and RELEASE_DIRECTORY.fullmatch(str(current.get("sha"))) and (
            state / current["sha"] / "source" / SESSION_CLAIM).is_file():
        return {"sha": current["sha"]}
    return None


def check_pre_claim(state, carries, target, reason):
    """`pre_claim_rollback_refused` (ANYM-914 criterion 6): once claims ran healthy, returning to a tree without
    SESSION_CLAIM needs `--pre-claim-rollback "<reason>"`. `carries` is called only then. True: the reason let it pass."""
    if not (first := claims_recorded(state)) or carries():
        return False
    if reason:
        return True
    raise Refused(f"pre_claim_rollback_refused: {first['sha'][:12]} ran session claims healthy and {target} lacks "
                  f"{SESSION_CLAIM}; roll forward, or pass --pre-claim-rollback \"<reason>\" (only while one host runs "
                  "sessions: it falls back to the per-directory locks)")


def tree_carries(state, sha, path):
    """Whether `sha`'s tree has `path`: its release checkout, else the source mirror. Unknown is False."""
    if (state / sha / "source").is_dir():
        return (state / sha / "source" / path).is_file()
    try:
        return bool(run(["git", "-C", str(state / "deploy-source.git"), "ls-tree", "--name-only", "-z", sha,
                         path]).strip("\0"))
    except Refused:
        return False


def apply(settings, sha, *, backup_waited=0, pre_claim=None):
    promotable(sha)
    state = Path(settings["state_directory"])
    release = state / sha
    receipt = read(release / "prepared.json")
    if receipt["sha"] != sha or receipt["config_digest"] != settings_digest(settings):
        raise Refused("prepared release does not match the selected production configuration")
    if (release / "attempted.json").exists():
        raise Refused("this release was already attempted; inspect its result before any recovery")
    documents = {}
    for target, image in receipt["images"].items():
        actual = json.loads(run(["docker", "image", "inspect", image]))[0]
        if (actual["Id"] != image or actual["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != sha):
            raise Refused("prepared image is missing or has the wrong source revision")
    for project, item in zip(settings["projects"], receipt["projects"], strict=True):
        if legacy_identities(item["running"]):
            raise Refused("prepared by an older production.py with order-dependent container settings; prepare again")
        labels = {}
        document, running = snapshot(project, labels=labels, exited=set(project.get("sessions") or ()))
        if (item["name"] != project["name"] or digest(document) != item["before_digest"]
                or running != item["running"] or digest(read(item["file"])) != item["digest"]):
            raise Refused("production or prepared configuration changed since preparation")
        # ANYM-895: the host files a service reads are hashed again here, after the call waits and before each stop.
        check_inputs(project, item, document)
        documents[project["name"]] = document
        for service in item.get("kept") or {}:
            if labels.get(service) != item["inputs"][service]["digest"]:
                raise Refused(f"inputs_changed_since_prepare: kept {project['name']}/{service} is no longer running "
                              "steadily with its input label; prepare again")
        previous = release / (project["name"] + ".before.json")
        if "before_file_digest" not in item:
            raise Refused("this release was prepared without a bound rollback snapshot; prepare it again")
        if not previous.exists() or digest(read(previous)) != item["before_file_digest"]:
            raise Refused("the before-release snapshot is missing or changed since preparation")
        if "render" in receipt:
            check_env_files(project, item, receipt)
    check_backup_runner(release / "source", sha)
    # No call is active before the last remote gate, nor after it: a call that started meanwhile is waited for before
    # anything is written, within the same bound.
    deadline = time.monotonic() + CALL_WAIT
    wait_for_calls(settings, deadline)
    # Last remote gate immediately before the first mutation, under the host lock.
    evidence = promotable(sha)
    wait_for_calls(settings, deadline)
    for project, item in zip(settings["projects"], receipt["projects"], strict=True):
        check_inputs(project, item, documents[project["name"]])
    # ANYM-896: which changed session services go now and which stay until the window, decided after the call waits
    # and admitted again right before each project's stop (holder_admit).
    decisions = holder_decide(settings, state, receipt)
    # ANYM-914: no other opener holds a session this release starts, and the lock directory exists before any stop.
    started = started_units(settings, receipt, documents)
    check_session_openers(settings, documents, started)
    provide_session_locks(((service, unit) for _, service, unit in started), required=(release / "source" / SESSION_CLAIM).is_file())
    write(release / "attempted.json", {"sha": sha, "ci": evidence, "status": "applying"})
    started, touched = {}, []
    for project, item in zip(settings["projects"], receipt["projects"], strict=True):
        command = ["docker", "compose", "--profile", "*", "--project-name", project["name"], "-f", item["file"]]
        if touched and [service for service in project["services"] if service not in (item.get("kept") or {})]:
            wait_for_calls(settings, time.monotonic() + CALL_WAIT)
        holder_admit(settings, state, decisions.get(project["name"]), item)  # a window that closed meanwhile defers
        kept = item.get("kept") or {}
        services = [service for service in project["services"] if service not in kept]
        for service in kept:  # ANYM-895: never stopped nor `up`; the checks below expect this very container
            started[project["name"], service] = item["running"][service]["container"], item["running"][service]["settings"]
        if touched:  # the first project was checked right before attempted.json
            check_inputs(project, item, documents[project["name"]])
        touched.append((project, item))
        if services:
            holder_apply(project, item, release, services, command, receipt,
                         lambda exc: unhealthy(release, sha, receipt, evidence, touched, exc))
        unproven = []
        for service, target in project["services"].items():
            if service in kept:
                # A kept service skips `up --wait`: it must run steadily (healthy, when it has a healthcheck) and, without
                # one, not restart during the settle, like a replacement.
                identity = item["running"][service]["container"]
                actual = json.loads(run(["docker", "inspect", identity]))[0]
                if not steady(actual):
                    unhealthy(release, sha, receipt, evidence, touched,
                              Refused(f"kept {project['name']}/{service} is not running steadily"))
                if "Health" not in actual["State"]:
                    unproven.append((service, identity, actual))
                continue
            ids = run(command + ["ps", "-q", service]).split()
            if len(ids) != 1:
                raise Refused("replacement service is missing")
            actual = json.loads(run(["docker", "inspect", ids[0]]))[0]
            if actual["Image"] != receipt["images"][target] or not actual["State"]["Running"]:
                raise Refused("replacement did not start the prepared image")
            started[project["name"], service] = ids[0], container_settings(actual)
            if "Health" not in actual["State"]:  # no Compose healthcheck: `--wait` saw it running once, no more
                unproven.append((service, ids[0], actual))
        if failure := restart_loop(project, item, unproven):
            unhealthy(release, sha, receipt, evidence, touched, Refused(failure))
        check_kept(release, sha, receipt, evidence, touched, project, item)
    # What rollback must find unchanged before it stops anything, captured the way prepare captures the baseline.
    # It must still be what this apply started: a change meanwhile leaves the attempt unresolved.
    restore = []
    for project, item in zip(settings["projects"], receipt["projects"], strict=True):
        check_kept(release, sha, receipt, evidence, touched, project, item)
        document, running = snapshot(project)
        try:
            check_inputs(project, item, document)
        except Refused as exc:
            raise Refused("production changed while this release was applied; inspect it before any recovery") from exc
        if digest(document) != item["before_digest"] or any(
                running[service]["image"] != expected_image(receipt, item, service, target)
                or not started[project["name"], service][0].startswith(running[service]["container"])
                or started[project["name"], service][1] != running[service]["settings"]
                for service, target in project["services"].items()):
            raise Refused("production changed while this release was applied; inspect it before any recovery")
        restore.append(restore_entry(item, document, running))
    result = {"sha": sha, "ci": evidence, "images": receipt["images"], "references": receipt.get("references", {}),
              "status": "applied", "applied_at": now(), "rollback": {"version": 1, "projects": restore}}
    if outcomes := service_outcomes(receipt):
        result.update(services=outcomes, boot_id=boot_id())
    if backup_waited:  # ANYM-744: only a release that waited for a running backup says so
        result["backup_wait_seconds"] = backup_waited
    if pre_claim:  # ANYM-914: the reason a by-hand apply returned to a tree without session claims
        result["pre_claim_rollback"] = pre_claim
    write(release / "result.json", result)
    write(state / "current.json", result)
    install_backup_health(sha, release)  # the release's backup checker names it (incident 11-11); never raises
    if (release / "source" / SESSION_CLAIM).is_file() and not (state / CLAIM_RECORD).exists():
        write(state / CLAIM_RECORD, {"sha": sha, "recorded_at": now()})
    return result


# ANYM-758, Kubernetes' minReadySeconds for one Compose host: `up --wait` proves health only for a service with a Compose
# healthcheck. One without (collectors, workers, indexer) passes as soon as it runs, so a container that exits and
# restarts can be caught running. Once per project, after its replacements started, the tool waits this long and
# inspects each such replacement again. 0 turns the settle off.
APPLY_SETTLE_SECONDS = 10


def restart_loop(project, item, unproven):
    """Why a replacement without a healthcheck is not running steadily APPLY_SETTLE_SECONDS after `up`, or None.
    `unproven` holds (service, container ID, first inspect). A container this apply created must not have restarted at
    all; one `up` kept (its ID is the one before the release) must not have restarted again since the first inspect."""
    if not unproven or APPLY_SETTLE_SECONDS <= 0:
        return None
    time.sleep(APPLY_SETTLE_SECONDS)
    for service, identity, first in unproven:
        actual = json.loads(run(["docker", "inspect", identity]))[0]
        condition = actual.get("State", {})
        kept = identity.startswith(item["running"][service]["container"])
        allowed = first.get("RestartCount", 0) if kept else 0
        if (condition.get("Restarting") or condition.get("Paused") or not condition.get("Running")
                or actual.get("RestartCount", 0) > allowed):
            return (f"replacement {project['name']}/{service} is restarting or stopped {APPLY_SETTLE_SECONDS} s after "
                    f"it started (restarts: {actual.get('RestartCount', 0)})")
    return None


def unhealthy(release, sha, receipt, evidence, touched, failure):
    """After `up --wait` or the settle (ANYM-758) failed: when every replacement started so far runs the prepared image, in any state, record
    the release as `unhealthy` with rollback data for the projects it touched; otherwise the attempt stays unresolved.
    Either way current.json keeps naming the previous release, and the apply is refused."""
    restore = []
    try:
        for project, item in touched:
            document, running = snapshot(project, stopped=True)
            if digest(document) != item["before_digest"] or any(
                    running[service]["image"] != expected_image(receipt, item, service, target)
                    for service, target in project["services"].items()):
                raise Refused("a replacement does not run the prepared image")
            restore.append(restore_entry(item, document, running))
    except Exception:
        raise Refused("apply stopped before every replacement ran the prepared image; "
                      "inspect it before any recovery") from failure
    write(release / "result.json", {"sha": sha, "ci": evidence, "images": receipt["images"],
                                    "references": receipt.get("references", {}), "status": "unhealthy",
                                    "failed_at": now(), "rollback": {"version": 1, "projects": restore}})
    raise Refused(f"a replacement failed its health wait; `rollback --sha {sha}` returns the previous release") from failure


def unfinished(state):
    """Releases whose apply or rollback, or a session pass (ANYM-896: `<sha>/session-<time>`), started but never recorded
    its result."""
    return sorted([path.parent.name for marker, outcome in (("attempted.json", "result.json"),
                                                            ("rollback-attempted.json", "rollback.json"))
                   for path in state.glob("*/" + marker) if not (path.parent / outcome).exists()]
                  + [path.parent.parent.name + "/" + path.parent.name for path in state.glob("*/session-*/attempted.json")
                     if not (path.parent / "result.json").exists()])


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


# The images that carry the messaging migration inventory: plan B's serving API depends on what their workers run.
MESSAGING_TARGETS = frozenset(target for target, build in BUILDS.items() if build[1] == "services/messaging/Dockerfile")


def serving_generation(settings, http):
    """Plan B's serving release (http-current.json) for a plan A rollback, or None when there is positively no plan B:
    no HTTP configuration (`http` None) and no HTTP state. Unknown, malformed, inconsistent or unresolved HTTP state is
    refused (ANYM-642). The caller holds the locks seamless_release.py holds."""
    state = Path(settings["state_directory"])
    if http is None:
        # HTTP state in this state, the host state root, its subdirectories with a receipt or any /etc/deedkit
        # configuration's state (retention_states), or a deploy that promoted plan B from this state, is a plan B.
        def marked(path):
            # A link counts, also a dangling one: an ambiguous marker is never taken for no plan B.
            try:
                return bool(os.lstat(path))
            except (FileNotFoundError, NotADirectoryError):
                return False
        try:
            # Every subdirectory of the host state root, not only those whose receipt is readable (retention_states):
            # a sibling state holding only a dangling receipt link is a marker too.
            found = list(dict.fromkeys(str(path) for directory in [*retention_states(state), *subdirectories(HOST_STATE)]
                                       for path in (directory / "http", directory / "http-current.json",
                                                    *(release / "http-attempted.json"
                                                      for release in subdirectories(directory)))
                                       if marked(path)))
            deployed = state / "deploy.json"
            if marked(deployed) and "http" in read(deployed):
                found.append(f"{deployed} (a deploy promoted plan B)")
        except (OSError, ValueError, TypeError, KeyError, AttributeError, Refused) as exc:
            detail = type(exc).__name__ if not isinstance(exc, Refused) else str(exc).removesuffix("; no image is removed")
            raise Refused(f"whether there is a plan B cannot be read ({detail}); pass --http-config") from exc
        if found:
            raise Refused("there is no HTTP configuration, but HTTP state exists: " + ", ".join(found)
                          + "; pass --http-config")
        return None
    http_state, mirror = Path(http["state_directory"]), state / "deploy-source.git"
    current = http_state / "http-current.json"
    try:
        if not current.is_file():
            raise Refused(f"plan B's state is unknown: {current} is missing")
        serving = read(current)
        if (not isinstance(serving, dict) or serving.get("status") not in ("serving", "serving-retirement-pending")
                or not RELEASE_DIRECTORY.fullmatch(str(serving.get("sha")))
                or not re.fullmatch(r"[0-9a-f]{12}", str(serving.get("generation")))
                or not IMAGE_DIGEST.fullmatch(str(serving.get("images", {}).get("runtime")))):
            raise Refused(f"plan B's state is inconsistent: {current} names no serving generation")
        if read(http_state / serving["sha"] / "http-attempted.json").get("status") != "serving":
            raise Refused(f"plan B's state is inconsistent: {serving['sha']}'s HTTP attempt does not serve")
        # Every record of the serving API's image agrees: the receipt, the container promote started, and the release's
        # own result (the schema it recorded is compared with the image's inventory in guard_rollback).
        result = read(http_state / serving["sha"] / "http-result.json")
        runtime = serving["images"]["runtime"]
        if (serving["containers"]["api"]["image"] != runtime or result.get("generation") != serving["generation"]
                or result.get("images", {}).get("runtime") != runtime or not isinstance(serving["schema"], dict)):
            raise Refused(f"plan B's state is inconsistent: the serving generation {serving['generation']}'s records "
                          "name different runtime images")
        # The interrupted-publication check reads the local source mirror; nothing is fetched.
        if not mirror.is_dir():
            raise Refused(f"{mirror} is missing: plan B's publication cannot be checked")
        if reason := unresolved(state, http_state, lambda: mirror, serving["sha"]):
            raise Refused(reason)
    except Refused:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise Refused(f"plan B's state in {http_state} cannot be read: {type(exc).__name__}") from exc
    return serving


def inventory_order(serving, restored):
    """`equal`, `older` (the serving inventory is a strict prefix of the restored one), `newer` (the restored one is a
    strict prefix of the serving one) or `divergent`: schema.sql first, then the migrations in file order."""
    def ordered(inventory):
        return [("schema.sql", inventory["schema.sql"]), *sorted(item for item in inventory.items() if item[0] != "schema.sql")]
    old, new = ordered(serving), ordered(restored)
    if old == new:
        return "equal"
    if len(old) < len(new) and new[:len(old)] == old:
        return "older"
    return "newer" if len(new) < len(old) and old[:len(new)] == new else "divergent"


def guard_rollback(serving, plan):
    """Refuse a plan A rollback that would restore a messaging image without migrations the serving plan B generation
    runs, or with a divergent inventory (ANYM-642). Local image inspection, network disabled; there is no override."""
    if serving is None:
        return
    s, inventories = http_release_tool(), {}
    named = f"plan B serves generation {serving['generation']} of {serving['sha']}"
    way_out = ("Roll forward instead: `production.py deploy` of a newer green main SHA (a fix or a revert that keeps "
               "every applied migration; production.md, \"Roll back the current release\")")
    def inventory(image):
        if image not in inventories:
            try:
                inventories[image] = s.schema_inventory(image)
            except Exception as exc:
                detail = str(exc) if isinstance(exc, Refused) else type(exc).__name__
                raise Refused(f"the schema inventory of {image} cannot be read ({detail}) while {named}; a plan A "
                              "rollback is refused. " + way_out) from exc
        return inventories[image]
    for project, _, previous in plan:
        for service, image in sorted(previous.items()):
            if project["services"][service] not in MESSAGING_TARGETS:
                continue
            current, restored = inventory(serving["images"]["runtime"]), inventory(image)
            if current != serving["schema"]:
                raise Refused(f"plan B's state is inconsistent: the schema recorded for generation "
                              f"{serving['generation']} is not its runtime image's inventory. " + way_out)
            where = f"{project['name']}/{service}'s restored image {image}"
            order = inventory_order(current, restored)
            if order in ("equal", "older"):
                continue
            missing = ", ".join(sorted(set(current) - set(restored))) or "none"
            if order == "newer":
                detail = f"migrations {where} lacks: {missing}"
            else:
                changed = ", ".join(sorted(path for path in set(current) & set(restored) if current[path] != restored[path]))
                detail = f"a migration inventory {where} diverges from (changed: {changed or 'none'}; missing: {missing})"
            raise Refused(f"{named} with {detail}; a plan A rollback alone would leave the serving API newer than "
                          "its workers. " + way_out)


def rollback_state(settings, sha, http):
    """What a rollback of `sha` starts from: the release's receipts, whether its apply failed, plan B's serving
    generation and the rollback data; refuses anything unfinished, changed or already attempted."""
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
    # Plan B's state first, before http-current.json names the generation the guard compares (ANYM-642).
    serving = serving_generation(settings, http)
    data = result.get("rollback")
    if not isinstance(data, dict) or data.get("version") != 1 or result.get("sha") != sha:
        raise Refused("this release has no rollback data; follow the manual rollback procedure (production.md)")
    prepared = read(release / "prepared.json")
    if prepared["config_digest"] != settings_digest(settings):
        raise Refused("production configuration changed since this release")
    # current.json still names the release that ran before the failed one, unless another one was applied since.
    if failed and prepared.get("current_digest") != digest(current):
        raise Refused("another release was applied or rolled back after this one was prepared")
    return state, release, result, failed, serving, data


def rollback_plan(settings, release, data, failed):
    """The projects to restore with their before-release files and previous images (all checked, nothing changed)."""
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
        # ANYM-895: a service the release kept is never stopped or recreated by its rollback.
        kept = item.get("kept") or {}
        previous = {service: read(before)["services"][service]["image"] for service in project["services"]
                    if service not in kept}
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
    return plan


def rollback_restore(settings, release, sha, serving, plan):
    """The guard, then each project's stop, `up` on the previous images and the check that they run."""
    guard_rollback(serving, plan)
    wait_for_calls(settings, time.monotonic() + CALL_WAIT)
    write(release / "rollback-attempted.json", {"sha": sha, "status": "rolling-back", "started_at": now()})
    for index, (project, before, previous) in enumerate(plan):
        command = ["docker", "compose", "--profile", "*", "--project-name", project["name"], "-f", str(before)]
        services = [service for service in project["services"] if service in previous]
        if not services:
            continue
        if index:
            wait_for_calls(settings, time.monotonic() + CALL_WAIT)
        # Stop the release's owners before the previous ones can connect to their sessions; a holder's collector stops
        # before it and starts after it (ANYM-896).
        later = holder_collectors(project, services)
        if later:
            run(command + ["stop", *later])
        run(command + ["stop", *[service for service in services if service not in later]])
        for group in ([service for service in services if service not in later], later):
            if group:
                run(command + ["up", "-d", "--no-build", "--pull", "never", "--no-deps",
                               "--wait", "--wait-timeout", "180", *group])
        for service, image in previous.items():
            ids = run(command + ["ps", "-q", service]).split()
            if len(ids) != 1:
                raise Refused("restored service is missing")
            actual = json.loads(run(["docker", "inspect", ids[0]]))[0]
            if actual["Image"] != image or not actual["State"]["Running"]:
                raise Refused("restored service does not run its previous image")
    # ANYM-895: what the release kept was never touched; it must still run steadily on its own image.
    for project, before, previous in plan:
        command = ["docker", "compose", "--profile", "*", "--project-name", project["name"], "-f", str(before)]
        for service in (service for service in project["services"] if service not in previous):
            ids = run(command + ["ps", "-q", service]).split()
            actual = json.loads(run(["docker", "inspect", ids[0]]))[0] if len(ids) == 1 else {}
            if actual.get("Image") != read(before)["services"][service]["image"] or not steady(actual):
                raise Refused(f"{project['name']}/{service}, which the release kept, is not running steadily; inspect it")


def restored_revisions(plan):
    """The revisions of the messaging images a rollback restores (their image label), None for an unlabelled one."""
    return {((json.loads(run(["docker", "image", "inspect", image]))[0].get("Config") or {}).get("Labels") or {}).get(
                REVISION_LABEL) for project, _, previous in plan for service, image in previous.items()
            if project["services"][service] in MESSAGING_TARGETS}


def rollback(settings, sha, http=None, *, pre_claim=None):
    """Return the current release's selected services to the images and configuration that ran before it, or
    those of a release whose apply failed its health wait (`unhealthy`), which never became current.

    Owner-confirmed like apply, except as `deploy`'s one automatic rollback. It never calls promotable(): the previous
    revision is no longer current main. The caller holds the host lock, as main() does for every state-changing action,
    and with plan B (`http`, its validated configuration; None when there is none) the locks of seamless_release.py.
    """
    state, release, result, failed, serving, data = rollback_state(settings, sha, http)
    plan = rollback_plan(settings, release, data, failed)
    # ANYM-914: the restore of a release that failed its health wait (`unhealthy`) is unchanged.
    bypassed = not failed and check_pre_claim(state, lambda: all(
        revision and RELEASE_DIRECTORY.fullmatch(revision) and tree_carries(state, revision, SESSION_CLAIM)
        for revision in restored_revisions(plan)), "the release before " + sha[:12], pre_claim)
    rollback_restore(settings, release, sha, serving, plan)
    outcome = {"sha": sha, "status": "rolled-back", "rolled_back_at": now(),
               "previous_images": {project["name"]: previous for project, _, previous in plan}}
    if bypassed:
        outcome["pre_claim_rollback"] = pre_claim
    # ANYM-895: what the rollback left running because the release kept it; image retention keeps these too.
    if kept := {item["name"]: {service: running["image"] for service, running in item["running"].items()
                               if service in (item.get("kept") or {})} for item in data["projects"] if item.get("kept")}:
        outcome["kept_images"] = kept
    write(release / "rollback.json", outcome)
    if not failed:  # current.json already names the release that runs again
        write(state / "current.json", {**outcome, "images": result["images"]})
    return outcome


# ANYM-896 (production.md "Session holders"): a session service (`"session": true`, a holder) is recreated for a change
# only inside the nightly window, at most once a window, unless a precedence reason or the urgent flag says otherwise.
# A holder copies no messaging_service code and speaks only its versioned socket protocol, so a holder kept back beside
# a newer release is checked by that protocol alone; its collector is never kept back (the pair rule below).
HOLDER_TARGETS = frozenset({"whatsapp-holder", "telegram-holder"})
HOLDER_WINDOW, HOLDER_TIMEZONE = "03:00-05:00", "Asia/Makassar"  # owner 2026-10-17 (ANYM-330): «Да, ночью по Бали»
HOLDER_LEDGER, HOLDER_LEDGER_EVENTS, HOLDER_LEDGER_PASSES = "session-ledger.json", 500, 60
HOLDER_SOCKETS = "/run/deedkit/holder"  # the holder's socket directory (deploy/compose.whatsapp<n>-holder.yaml)
PLANNED_STOP = ".planned-stop"  # whatsapp_holder.py PLANNED_STOP_FILE
HOLDER_PASS_MARGIN = 30 * 60  # the night's pass starts within the window shortened by this much at both ends
HOLDER_STALE_WINDOWS = 2
HOLDER_ATTACH_S, HOLDER_ATTACH_POLL = 120, 5  # above the collector's 60 s attach backoff
PROTOCOL_LABEL = "io.deedkit.{role}.protocol.{end}"  # whatsapp/whatsmeow/Dockerfile, services/messaging/Dockerfile
FLAG_AUTHORITIES = ("incident", "owner")
HOLDER_KINDS = ("planned", "recovery", "flagged", "rollback")
PRECEDENCE = ("unreadable", "unlabeled")


def holder_marks(project):
    """Criterion 1: the project's `services` as `{service: target}` and its session services as `{holder: {"collector":
    service}}`. An object entry takes only `target`, `"session": true` and `collector`, on a holder target."""
    services, sessions = {}, {}
    for service, entry in (project.get("services") or {}).items():
        if not isinstance(entry, dict):
            services[service] = entry
            continue
        where = f"{project.get('name')}/{service}"
        if set(entry) - {"target", "session", "collector"} or entry.get("session") is not True or not isinstance(
                entry.get("target"), str):
            raise Refused(f"session_mark_invalid: {where} takes only target, \"session\": true and collector")
        if entry["target"] not in HOLDER_TARGETS:
            raise Refused(f"session_target_unsupported: {where}: only a target that copies no messaging_service code "
                          f"({', '.join(sorted(HOLDER_TARGETS))}) may be a session service")
        services[service], sessions[service] = entry["target"], {"collector": entry.get("collector")}
    collectors = [mark["collector"] for mark in sessions.values()]
    for holder, mark in sessions.items():
        if (not isinstance(mark["collector"], str) or mark["collector"] not in services or mark["collector"] in sessions
                or collectors.count(mark["collector"]) > 1):
            raise Refused(f"session_mark_invalid: {project.get('name')}/{holder} needs \"collector\": another selected "
                          "service of its own")
    return services, sessions


def holder_window(settings):
    """`(zone, start, end)` of the nightly window: local minutes after midnight, the start before the end."""
    text, name = settings.get("session_window", HOLDER_WINDOW), settings.get("session_timezone", HOLDER_TIMEZONE)
    found = re.fullmatch(r"([01][0-9]|2[0-3]):([0-5][0-9])-([01][0-9]|2[0-3]):([0-5][0-9])", text) if isinstance(
        text, str) else None
    start, end = (int(found[1]) * 60 + int(found[2]), int(found[3]) * 60 + int(found[4])) if found else (0, 0)
    if start >= end:
        raise Refused("session_window_invalid: session_window is HH:MM-HH:MM, its start before its end on one day")
    try:
        zone = ZoneInfo(name)
    except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
        if name != HOLDER_TIMEZONE:
            raise Refused("session_timezone_invalid: session_timezone is an IANA time zone name") from exc
        zone = timezone(timedelta(hours=8), "WITA")  # a host without tzdata: Makassar's offset, unchanged since 1963
    return zone, start, end


def holder_clock():
    return datetime.now(timezone.utc)


def holder_window_at(settings, moment):
    """`(begin, end)` in UTC of the window that contains `moment`, else of the next one."""
    zone, start, end = holder_window(settings)
    day = moment.astimezone(zone).date()
    for offset in (-1, 0, 1, 2):
        date = day + timedelta(days=offset)
        begin, finish = (datetime(date.year, date.month, date.day, minutes // 60, minutes % 60, tzinfo=zone).astimezone(
            timezone.utc) for minutes in (start, end))
        if moment < finish:
            return begin, finish
    raise Refused("session_window_invalid: no window follows " + moment.isoformat())


def holder_next_window(settings, moment):
    """The window a change deferred at `moment` waits for: the one ahead, or the one after the window it is in."""
    begin, end = holder_window_at(settings, moment)
    return holder_window_at(settings, end) if begin <= moment else (begin, end)


def holder_window_key(settings, begin):
    return begin.astimezone(holder_window(settings)[0]).date().isoformat()


def holder_window_text(settings, begin, end):
    zone = holder_window(settings)[0]
    local, until = begin.astimezone(zone), end.astimezone(zone)
    return f"{begin:%Y-%m-%dT%H:%M}Z-{end:%H:%M}Z UTC, {local:%Y-%m-%d %H:%M}-{until:%H:%M} {local.tzname()}"


def holder_pass_time(settings, begin, end):
    """Criterion 3: the night's pass start, derived from the window's date (03:30-04:30 for 03:00-05:00), so the
    reconnect minute varies from night to night and every run of a night agrees on it."""
    margin = min(HOLDER_PASS_MARGIN, (end - begin).total_seconds() / 4)
    span = max(1, int((end - begin).total_seconds() - 2 * margin))
    seed = int(hashlib.sha256(("session pass " + holder_window_key(settings, begin)).encode()).hexdigest(), 16)
    return begin + timedelta(seconds=margin + seed % span)


def holder_ledger(state):
    """state/session-ledger.json: the recreates of session services by kind and window, the deferred changes, and each
    window's pass outcome."""
    path = state / HOLDER_LEDGER
    value = read(path) if path.exists() else {}
    return {"version": 1, "events": list(value.get("events") or []), "pending": dict(value.get("pending") or {}),
            "passes": dict(value.get("passes") or {})}


def holder_record(state, events=(), pending=None, passes=None):
    """Append events, set or clear (None) pending changes, keeping each one's first deferral, and record passes."""
    ledger = holder_ledger(state)
    ledger["events"] = (ledger["events"] + list(events))[-HOLDER_LEDGER_EVENTS:]
    for name, entry in (pending or {}).items():
        if entry is None:
            ledger["pending"].pop(name, None)
        else:
            ledger["pending"][name] = {**entry, "since": ledger["pending"].get(name, entry).get("since", entry["since"])}
    ledger["passes"] = dict(list({**ledger["passes"], **(passes or {})}.items())[-HOLDER_LEDGER_PASSES:])
    write(state / HOLDER_LEDGER, ledger)


def holder_event(name, kind, key, sha, **fields):
    return {"service": name, "kind": kind, "window": key, "at": now(), "release": sha, **fields}


def holder_planned(ledger, name, key):
    return any(event.get("service") == name and event.get("kind") == "planned" and event.get("window") == key
               for event in ledger["events"])


def holder_counts(events):
    return {kind: sum(event["kind"] == kind for event in events) for kind in HOLDER_KINDS}


def protocol_range(image, role):
    """`(min, max)` of an image's socket protocol labels; None when they are absent or malformed."""
    try:
        labels = (json.loads(run(["docker", "image", "inspect", image]))[0].get("Config") or {}).get("Labels") or {}
    except (Refused, ValueError, LookupError, AttributeError):
        return None
    found = [labels.get(PROTOCOL_LABEL.format(role=role, end=end)) for end in ("min", "max")]
    numbers = [int(value) for value in found if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,5}", value)]
    return (numbers[0], numbers[1]) if len(numbers) == 2 and numbers[0] <= numbers[1] else None


def holder_speaks(project, running, images, holder, collector, group):
    """Criterion 7: whether `group` (what is recreated of the holder and its collector) leaves a pair that shares a
    socket protocol (a service speaks its range's maximum): a collector alone needs its candidate range to include the
    running holder's protocol, a holder alone the running collector's; a pair, each candidate the other candidate's."""
    def protocols(service, role):
        image = images[project["services"][service]] if service in group else running[service]["image"]
        return protocol_range(image, role)
    def includes(outer, inner):
        return outer is not None and inner is not None and outer[0] <= inner[1] <= outer[1]
    new_holder, new_collector = protocols(holder, "holder"), protocols(collector, "collector")
    return includes(new_holder, new_collector) and includes(new_collector, new_holder) if len(group) == 2 else (
        includes(new_collector, new_holder) if group == [collector] else includes(new_holder, new_collector))


def incompatible(project, holder, collector, group):
    what = " and ".join(group)
    return Refused(f"session_pair_incompatible: recreating {project['name']}/{what} would leave {holder} and {collector} "
                   "without a shared socket protocol; nothing was stopped")


def holder_prepared(project, item, reasons, recorded):
    """At prepare: for each changed session service and paired collector, its precedence reason (criterion 2: stopped,
    restarting, unhealthy, paused, unlabeled or unreadable) or None and the inputs its running container carries."""
    found = {}
    for holder, mark in (project.get("sessions") or {}).items():
        for service in (holder, mark["collector"]):
            if service not in reasons:
                continue
            why, record, identity = reasons[service], recorded.get(service) or {}, item["running"][service]
            precedence = item["conditions"].get(service) or (why[0] if why[0] in PRECEDENCE else None)
            if precedence is None and (item["labels"].get(service) != record.get("digest") or any(
                    record.get(key) != identity[key] for key in ("container", "image", "settings"))):
                precedence = "unlabeled"
            found[service] = {"precedence": precedence} if precedence else {"precedence": None, "recorded": {
                "digest": record["digest"], "parts": record.get("parts"), "revision": record["revision"]}}
    return found


def holder_defer(item, service, recorded, text):
    """Keep `service` as a kept service on the inputs its container carries; the receipt says what waits and until."""
    parts, identity = item["recreated"].pop(service), item["running"][service]
    item.setdefault("kept", {})[service] = {"container": identity["container"], "image": identity["image"],
                                            "revision": recorded["revision"]}
    item["inputs"][service] = {"digest": recorded["digest"], "parts": recorded["parts"]}
    item.setdefault("deferred", {})[service] = {"parts": parts, "next_window": text,
                                                "line": f"deferred ({', '.join(parts)}, next window {text})"}


def holder_decide(settings, state, receipt):
    """Criteria 2 and 7 in apply, after its call waits and before its first stop: a changed holder goes now when it
    has a precedence reason or inside the window as its first planned recreate there; otherwise it stays as a kept
    service until the next window, and a changed collector goes alone (a collector is never kept back). Refused before
    any stop: a recreate that leaves no shared protocol, and a collector that cannot speak to the holder kept for the
    window (apply that release inside the window). Changes `receipt` in memory; returns per project the ledger events,
    the pending changes and the planned holders with the window's end, for holder_admit."""
    if not any(project.get("sessions") for project in settings["projects"]):
        return {}
    moment, ledger, decisions = holder_clock(), holder_ledger(state), {}
    begin, end = holder_window_at(settings, moment)
    key = holder_window_key(settings, begin)
    for project, item in zip(settings["projects"], receipt["projects"], strict=True):
        recreated, info = item.get("recreated") or {}, item.get("session") or {}
        decision = decisions[project["name"]] = {"events": [], "pending": {}, "planned": {}, "end": end,
                                                 "release": receipt["sha"]}
        for holder, mark in (project.get("sessions") or {}).items():
            collector, name = mark["collector"], project["name"] + "/" + holder
            group = [service for service in (holder, collector) if service in recreated]
            speaks = lambda members: holder_speaks(project, item["running"], receipt["images"], holder, collector, members)
            if holder not in group:  # a collector-only recreate never recreates its holder
                if group and not speaks(group):
                    raise incompatible(project, holder, collector, group)
                continue
            precedence = (info.get(holder) or {}).get("precedence", "unlabeled")
            alone = collector not in group or speaks([collector])
            if precedence or begin <= moment < end and not holder_planned(ledger, name, key):
                if not speaks(group):
                    raise incompatible(project, holder, collector, group)
                decision["events"].append(holder_event(name, "recovery" if precedence else "planned", key, receipt["sha"],
                                                       **({"reason": precedence} if precedence else {})))
                decision["pending"][name] = None
                if not precedence:
                    decision["planned"][holder] = alone
                continue
            if not alone:
                raise Refused(f"session_pair_incompatible: the new {project['name']}/{collector} cannot speak to the running "
                              f"{holder}, which waits for the window: apply this release inside the window (production.md "
                              "\"Session holders\"); nothing was stopped")
            text = holder_window_text(settings, *holder_next_window(settings, moment))
            parts = recreated[holder]
            holder_defer(item, holder, info[holder]["recorded"], text)
            decision["pending"][name] = {"parts": parts, "release": receipt["sha"], "since": now(), "next_window": text}
    return decisions


def holder_admit(settings, state, decision, item):
    """Right before a project's first stop: a planned holder recreate whose window closed meanwhile (call waits,
    earlier projects) is deferred after all and never counted, unless its new collector, which goes now, could not
    speak to it; then the project's events and pending changes go to the ledger (a crash after the stop still counts)."""
    if not decision:
        return
    events, pending, moment = list(decision["events"]), dict(decision["pending"]), holder_clock()
    if moment >= decision["end"]:
        text = holder_window_text(settings, *holder_next_window(settings, moment))
        for holder, alone in decision["planned"].items():
            name = next(event["service"] for event in events if event["service"].endswith("/" + holder))
            if not alone:
                continue
            parts = item["recreated"][holder]
            holder_defer(item, holder, item["session"][holder]["recorded"], text)
            events = [event for event in events if event["service"] != name]
            pending[name] = {"parts": parts, "release": decision["release"], "since": now(), "next_window": text}
    if events or pending:
        holder_record(state, events=events, pending=pending)


def holder_collectors(project, services):
    """The collectors in `services` whose holders are there too: they start after their holders and stop first."""
    return [mark["collector"] for holder, mark in (project.get("sessions") or {}).items()
            if holder in services and mark["collector"] in services]


def holder_apply(project, item, release, services, command, receipt, failed):
    """apply's stop and `up` of one project's replacements. Without a session service among them: one stop and one
    `up`, as before ANYM-896. With one: a paired collector stops first and starts only after its holder passed its
    post-up check, and each recreated holder sees its collector attach (criterion 7). A failure once a collector
    stopped restores those pairs, holder first, before `failed(exc)` records the release unhealthy and raises."""
    holders = [holder for holder in (project.get("sessions") or {}) if holder in services]
    later = holder_collectors(project, services)
    first = [service for service in services if service not in later]
    pairs = [holder for holder in holders if project["sessions"][holder]["collector"] in later]
    up = command + ["up", "-d", "--no-build", "--pull", "never", "--no-deps", "--wait", "--wait-timeout", "180"]
    def fail(exc):
        if later:
            holder_restore(project, item, release, pairs, later, exc)
        failed(exc)
    def failing(exc):  # without a paired collector stopped, exactly what apply did before ANYM-896
        if not isinstance(exc, Refused) and not later:
            raise exc
        fail(exc if isinstance(exc, Refused) else Refused(f"a session step failed: {type(exc).__name__}"))
    # Stop the existing owners before replacements can connect to their sessions.
    try:
        if later:
            run(command + ["stop", *later])
        run(command + ["stop", *first])
    except Exception as exc:
        if not later:
            raise
        failing(exc)
    since = holder_since()
    try:
        run(up + first)
    except Exception as exc:
        failing(exc)
    try:
        if holders:
            holder_steady(command, project, item["running"], {holder: receipt["images"][project["services"][holder]]
                                                              for holder in holders})
        if failure := holder_attached(command, project, [holder for holder in holders if holder not in pairs], since):
            raise Refused(failure)
        if later:
            since = holder_since()
            run(up + later)
            if failure := holder_attached(command, project, pairs, since):
                raise Refused(failure)
    except Exception as exc:
        fail(exc if isinstance(exc, Refused) else Refused(f"a session check failed: {type(exc).__name__}"))


def holder_restore(project, item, release, pairs, later, failure):
    """In apply, criterion 7: the pairs of a failed project back on their previous images (the release's before-file),
    holders first, then kept, so the release's rollback leaves them as they are. A failed restore leaves the attempt
    unresolved."""
    before = ["docker", "compose", "--profile", "*", "--project-name", project["name"], "-f",
              str(release / (project["name"] + ".before.json"))]
    up = ["up", "-d", "--no-build", "--pull", "never", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "180"]
    try:
        run(before + ["stop", *later, *pairs])
        for members in (pairs, later):
            run(before + up + members)
            holder_steady(before, project, item["running"], {service: item["running"][service]["image"]
                                                             for service in members})
        for service in (*pairs, *later):
            image = item["running"][service]["image"]
            labels = (json.loads(run(["docker", "image", "inspect", image]))[0].get("Config") or {}).get("Labels") or {}
            item.setdefault("kept", {})[service] = {"container": item["running"][service]["container"], "image": image,
                                                    "revision": labels.get(REVISION_LABEL)}
    except Exception as exc:
        raise Refused(f"{failure}; restoring the previous {project['name']} pair failed too "
                      f"({exc if isinstance(exc, Refused) else type(exc).__name__}); inspect it before any recovery") from exc


def holder_steady(command, project, running, images):
    """The post-up check of recreated services (`images`: service -> expected image): each runs steadily on its image
    and, after APPLY_SETTLE_SECONDS, has not restarted. Refused with the failed check."""
    unproven = []
    for service, image in images.items():
        ids = run(command + ["ps", "-q", service]).split()
        if len(ids) != 1:
            raise Refused(f"{project['name']}/{service} is missing after its recreate")
        actual = json.loads(run(["docker", "inspect", ids[0]]))[0]
        if actual["Image"] != image or not steady(actual):
            raise Refused(f"{project['name']}/{service} is not running steadily on its image after its recreate")
        unproven.append((service, ids[0], actual))
    if failure := restart_loop(project, {"running": running}, unproven):
        raise Refused(failure)


def holder_since():
    return (holder_clock() - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def holder_log(container, since):
    """The holder's log lines since `since` (its JSON events go to stderr). Only event names are read; nothing of it is
    printed or kept."""
    try:
        result = subprocess.run(["docker", "logs", "--since", since, container], capture_output=True, text=True,
                                timeout=30)
    except (subprocess.TimeoutExpired, OSError):  # a failed read is no attach seen; the deadline decides
        return []
    return (result.stdout + result.stderr).splitlines() if result.returncode == 0 else []


def holder_attached(command, project, holders, since):
    """Why a recreated holder saw no collector attach (its `holder.attached` event since `since`) within
    HOLDER_ATTACH_S, or None."""
    def seen(container):
        for line in holder_log(container, since):
            with suppress(ValueError, RecursionError):
                if isinstance(event := json.loads(line), dict) and event.get("event") == "holder.attached":
                    return True
        return False
    deadline = time.monotonic() + HOLDER_ATTACH_S
    for holder in holders:
        ids = run(command + ["ps", "-q", holder]).split()
        while not (len(ids) == 1 and seen(ids[0])):
            if len(ids) != 1 or time.monotonic() >= deadline:
                return f"no collector attached to the recreated {project['name']}/{holder} within {HOLDER_ATTACH_S} s"
            time.sleep(HOLDER_ATTACH_POLL)
    return None


def holder_marker(document, holder, planned_end):
    """Criterion 6: `{reason: session_window, planned_end}` in the holder's socket directory (0600, the collectors'
    owner, a temporary file renamed), so its collector reports the stop as planned. None when it cannot be written:
    the collector then reports `holder_restart`, and the pass goes on."""
    sources = [volume.get("source") for volume in document["services"][holder].get("volumes") or []
               if isinstance(volume, dict) and volume.get("type") == "bind" and volume.get("target") == HOLDER_SOCKETS]
    if len(sources) != 1 or not isinstance(sources[0], str):
        return None
    directory = Path(sources[0])
    temporary = directory / f"{PLANNED_STOP}.{uuid.uuid4().hex}"
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            return None
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchown(descriptor, *SESSION_OWNER)
            os.write(descriptor, json.dumps({"reason": "session_window", "planned_end": planned_end.isoformat()}).encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, directory / PLANNED_STOP)
    except OSError:
        with suppress(OSError):
            temporary.unlink()
        return None
    return directory / PLANNED_STOP


def holder_document(base, units):
    """`base` without builds, each service of `units` on `(image, revision, input digest or None)`."""
    result = json.loads(json.dumps(base))
    for unit in result["services"].values():
        unit.pop("build", None)
    for service, (image, revision, value) in units.items():
        unit = result["services"][service]
        labels = unit.setdefault("labels", {})
        unit["image"], labels[REVISION_LABEL] = image, revision
        if value is None:
            labels.pop(INPUT_LABEL, None)
        else:
            labels[INPUT_LABEL] = value
    return result


def holder_files(directory, plan, sha, images):
    """The pass's Compose files of one project: the candidate (the recreated services on the release's images and their
    recomputed digests) and the restore file (every service as it runs, with the labels its receipt names)."""
    project, document, running, recorded = plan["project"], plan["document"], plan["running"], plan["recorded"]
    def as_running(service):
        record = recorded.get(service) or {}
        return running[service]["image"], record.get("revision", sha), record.get("digest")
    old = {service: as_running(service) for service in project["services"]}
    new = {**old, **{service: (images[project["services"][service]], sha, plan["wanted"][service]["digest"])
                     for service in plan["units"]}}
    reference, _ = evidence_document(project, document)
    files = {}
    for suffix, units in (("", new), (".before", old)):
        path = directory / f"{project['name']}{suffix}.json"
        write(path, escaped_rendered(holder_document(reference, units)))
        reread = json.loads(run(["docker", "compose", "--profile", "*", "-f", str(path), "config", "--format", "json"]))
        if reread != holder_document(document, units):
            raise Refused("a session pass Compose file did not round-trip exactly")
        files[suffix] = path
    return files


def holder_plans(settings, state, current, receipt, release, ledger, key, flagged):
    """What a pass recreates in each project with session services, from digests recomputed against the release's
    checkout and the host as it is now (a change a rollback undid is no longer pending): `units` (service -> kind),
    `groups` (holder, collector, members) and `waiting` (pending, but this window already had its planned recreate)."""
    try:
        entries = tree_entries(release / "source")
    except Exception:
        entries = None
    built, plans = {}, []
    for project in (project for project in settings["projects"] if project.get("sessions")):
        labels, conditions = {}, {}
        document, running = snapshot(project, labels=labels, conditions=conditions, exited=set(project["sessions"]))
        recorded, wanted, why = recorded_inputs(state, project["name"], current), {}, {}
        for service, target in project["services"].items():
            if target not in built:
                try:
                    built[target] = build_inputs(release / "source", target, entries) if entries is not None else None
                except Exception:
                    built[target] = None
            wanted[service] = service_inputs(document, service, built[target])
            why[service] = keep_decision(service, wanted[service], running[service], labels.get(service),
                                         recorded.get(service))
        plan = {"project": project, "document": document, "running": running, "recorded": recorded, "wanted": wanted,
                "units": {}, "groups": [], "waiting": [], "reasons": {}}
        for holder, mark in project["sessions"].items():
            collector, name = mark["collector"], project["name"] + "/" + holder
            if flagged is not None:
                if name not in flagged:
                    continue
                kind = "flagged"
            elif why[holder] is None:
                continue
            elif conditions.get(holder) or why[holder][0] in PRECEDENCE:
                kind, plan["reasons"][holder] = "recovery", conditions.get(holder) or why[holder][0]
            elif holder_planned(ledger, name, key):
                plan["waiting"].append(name)
                continue
            else:
                kind = "planned"
            members = [holder] + ([collector] if why[collector] is not None else [])
            if not holder_speaks(project, running, receipt["images"], holder, collector, members):
                raise incompatible(project, holder, collector, members)
            for service in members:
                image = receipt["images"][project["services"][service]]
                if json.loads(run(["docker", "image", "inspect", image]))[0].get("Id") != image:
                    raise Refused(f"the release image of {project['name']}/{service} is not on this host")
            plan["units"].update(dict.fromkeys(members, kind))
            plan["groups"].append((holder, collector, members))
        plans.append(plan)
    return plans


def holder_group(settings, http, plan, group, files, planned_end):
    """One holder of a pass, with its collector when both are recreated: the planned-stop marker, the collector stopped
    first, the holder up and checked, the collector up, the attach. Any failure restores the previous pair inside this
    pass, holder first, without guard_rollback for the holder (it carries no migrations) and with it for a collector
    (criterion 4). Returns `(status, failed check or None)`; a failed restore raises (the pass stays `started`)."""
    project, (holder, later) = plan["project"], (group[0], group[2][1:])
    new, old = (["docker", "compose", "--profile", "*", "--project-name", project["name"], "-f", str(files[suffix])]
                for suffix in ("", ".before"))
    up = ["up", "-d", "--no-build", "--pull", "never", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "180"]
    images = {service: plan["images"][project["services"][service]] for service in group[2]}
    marker = holder_marker(plan["document"], holder, planned_end)
    try:
        try:
            if later:
                run(new + ["stop", *later])
            run(new + ["stop", holder])
            since = holder_since()
            run(new + up + [holder])
            holder_steady(new, project, plan["running"], {holder: images[holder]})
            if later:
                since = holder_since()
                run(new + up + later)
                holder_steady(new, project, plan["running"], {service: images[service] for service in later})
            if failure := holder_attached(new, project, [holder], since):
                raise Refused(failure)
            return "recreated", None
        except Exception as exc:  # any failed step or check, a read that timed out included, restores the pair
            failed = str(exc) if isinstance(exc, Refused) else f"a session check failed: {type(exc).__name__}"
        if later:
            guard_rollback(serving_generation(settings, http),
                           [(project, files[".before"], {service: plan["running"][service]["image"] for service in later})])
        run(old + ["stop", *later, holder])
        for members in ([holder], later):
            if members:
                run(old + up + members)
                holder_steady(old, project, plan["running"], {service: plan["running"][service]["image"]
                                                              for service in members})
        return "rolled_back", failed
    finally:
        if marker is not None:
            with suppress(OSError):
                marker.unlink()


def holder_receipts(state, sha, changes):
    """After a pass, current.json and the release's result.json name each recreated or restored service's container,
    and a recreated one's inputs on this release (no longer kept), so the next release and a rollback read what runs."""
    for path in (state / "current.json", state / sha / "result.json"):
        value = read(path)
        for item in value["rollback"]["projects"]:
            for service, change in changes.get(item["name"], {}).items():
                item["running"][service] = change["running"]
                if "inputs" in change:
                    item["inputs"][service] = change["inputs"]
                    (item.get("kept") or {}).pop(service, None)
        write(path, value)


def holder_pass(settings, http, *, moment=None, flagged=None, reference=None):
    """Criteria 3-7: a session-only pass over the current applied release; no new release, every digest recomputed.
    Refused before its `started` receipt: nothing stopped. After it: a call wait bounded by the window's end (or
    CALL_WAIT for a flag), then each group; the pass's receipt is release/session-<time>/result.json."""
    state = Path(settings["state_directory"])
    current, moment = read(state / "current.json"), moment or holder_clock()
    sha, kind_of_pass = current["sha"], "flagged" if flagged is not None else "night"
    release = state / sha
    receipt, ledger = read(release / "prepared.json"), holder_ledger(state)
    begin, end = holder_window_at(settings, moment)
    key, passes = holder_window_key(settings, begin), (lambda value: {} if flagged is not None else {key: value})
    try:
        plans = holder_plans(settings, state, current, receipt, release, ledger, key, flagged)
    except Refused as exc:
        holder_record(state, passes=passes({"status": "refused", "reason": str(exc)}))
        return {"status": "refused", "reason": str(exc)}
    waiting = {name for plan in plans for name in plan["waiting"]}
    if not any(plan["units"] for plan in plans):
        holder_record(state, pending={name: None for name in ledger["pending"] if name not in waiting},
                      passes=passes({"status": "nothing pending"}))
        return {"status": "nothing pending", "waiting": sorted(waiting)}
    directory = release / ("session-" + moment.strftime("%Y%m%dT%H%M%SZ"))
    directory.mkdir(mode=0o700)
    files = []
    for plan in plans:
        plan["images"] = receipt["images"]
        files.append(holder_files(directory, plan, sha, receipt["images"]) if plan["units"] else None)
    services = {plan["project"]["name"] + "/" + service: kind for plan in plans for service, kind in plan["units"].items()}
    started = {"sha": sha, "pass": kind_of_pass, "window": key, "status": "started", "started_at": now(),
               "services": services, **({"reference": reference} if reference else {})}
    write(directory / "attempted.json", started)
    holder_record(state, passes=passes({"status": "started", "pass": directory.name}))
    try:
        wait_for_calls(settings, time.monotonic() + max(0, (CALL_WAIT if flagged is not None else
                                                            (end - holder_clock()).total_seconds())))
    except (CallActive, CallsUnknown) as exc:
        reason = str(exc) if flagged is not None else "deferred (window closed during call wait)"
        result = {**started, "status": "waiting" if flagged is not None else "deferred", "reason": reason,
                  "finished_at": now(), "counts": holder_counts([])}
        write(directory / "result.json", result)
        holder_record(state, passes=passes({"status": result["status"], "pass": directory.name}))
        return result
    planned_end = end if flagged is None else holder_clock() + timedelta(seconds=HOLDER_ATTACH_S + 180)
    events, outcomes, changes, cleared = [], {}, {}, {}
    for plan, paths in zip(plans, files):
        project = plan["project"]
        for group in plan["groups"]:
            name, members = project["name"] + "/" + group[0], group[2]
            kind = plan["units"][group[0]]
            if kind == "planned" and holder_clock() >= end:  # earlier groups or the call wait reached the window's end
                outcomes[name] = {"status": "deferred", "services": members, "reason": "window closed"}
                waiting.add(name)
                continue
            events += [holder_event(project["name"] + "/" + service, kind, key, sha, **(
                {"reason": plan["reasons"][service]} if service in plan["reasons"] else {}),
                **({"reference": reference} if reference else {})) for service in members]
            holder_record(state, events=events[-len(members):])  # counted before the first stop: a crash still counts
            status, failed = holder_group(settings, http, plan, group, paths, planned_end)
            _, running = snapshot(project)
            for service in members:
                changes.setdefault(project["name"], {})[service] = {"running": running[service], **(
                    {"inputs": plan["wanted"][service]} if status == "recreated" else {})}
            outcomes[name] = {"status": status, "services": members, **({"check": failed} if failed else {})}
            if status == "recreated":
                cleared[name] = None
            else:
                events += [holder_event(project["name"] + "/" + service, "rollback", key, sha, check=failed)
                           for service in members]
                holder_record(state, events=events[-len(members):])
    holder_receipts(state, sha, changes)
    statuses = {value["status"] for value in outcomes.values()}
    status = "rolled_back" if "rolled_back" in statuses else "applied" if "recreated" in statuses else "deferred"
    result = {**started, "status": status, "finished_at": now(), "outcomes": outcomes, "counts": holder_counts(events)}
    write(directory / "result.json", result)
    if flagged is None:
        cleared.update({name: None for name in ledger["pending"] if name not in waiting and name not in outcomes})
    holder_record(state, pending=cleared, passes=passes({"status": status, "pass": directory.name}))
    return result


def holder_nightly(settings, http, state, current, serving, outcome, *, auto, dry_run, halt):
    """Criterion 3: an `--auto` run that found nothing new to deploy runs the night's session-only pass once, from its
    per-night start until the window ends, when a deferred change is recorded."""
    if not auto or dry_run or not any(project.get("sessions") for project in settings["projects"]):
        return outcome
    moment, ledger = holder_clock(), holder_ledger(state)
    begin, end = holder_window_at(settings, moment)
    key = holder_window_key(settings, begin)
    if not ledger["pending"] or key in ledger["passes"] or not holder_pass_time(settings, begin, end) <= moment < end:
        return outcome
    if drift := configuration_drift(settings, http, current, serving):
        holder_record(state, passes={key: {"status": "skipped", "reason": drift}})
        return {**outcome, "session_pass": {"status": "skipped", "reason": drift}}
    try:
        passed = holder_pass(settings, http, moment=moment)
    except Exception as exc:
        message = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        return halt(f"the session pass of {current['sha']} stopped: {message}; inspect its receipt",
                    code="session_pass_failed")
    return {**outcome, "session_pass": passed}


def holder_flag(settings, services, reason):
    """Criterion 5: the `project/holder` names of `--session-restart-now` and its `--reason`, `incident:<runbook
    record>` or `owner:<the owner's line>`; anything else is refused with `session_flag_invalid` before any stop."""
    configured = [(project["name"] + "/" + holder, holder) for project in settings["projects"]
                  for holder in project.get("sessions") or {}]
    found = []
    for name in (part.strip() for part in (services or "").split(",")):
        matches = [full for full, holder in configured if name in (full, holder)]
        if len(matches) != 1:
            raise Refused(f"session_flag_invalid: {name[:80] or 'an empty name'} is not one configured session service")
        found.append(matches[0])
    authority, _, cited = (reason or "").partition(":")
    if authority not in FLAG_AUTHORITIES or not cited.strip() or len(reason) > 1000 or len(set(found)) != len(found):
        raise Refused("session_flag_invalid: --reason is \"incident:<runbook record>\" or \"owner:<the owner's line, "
                      "quoted>\", each service named once")
    return found, reason


def holder_flagged(settings, http, flag):
    """`deploy --session-restart-now`: a session-only pass over the named services, at once, under the deploy locks."""
    state, http_state = Path(settings["state_directory"]), Path(http["state_directory"])
    if reason := unresolved(state, http_state):
        return {"status": "refused", "reason": reason}
    current = read(state / "current.json") if (state / "current.json").exists() else {}
    if current.get("status") != "applied":
        return {"status": "refused", "reason": "the current release is not applied"}
    serving = read(http_state / "http-current.json") if (http_state / "http-current.json").exists() else {}
    if drift := configuration_drift(settings, http, current, serving):  # the restore file must be what ran before
        return {"status": "refused", "reason": drift + "; apply a release by hand first"}
    names, reference = holder_flag(settings, *flag)
    return holder_pass(settings, http, flagged=set(names), reference=reference)


def holder_pending(settings, moment=None):
    """Criterion 6: each deferred session change with its parts, release and next window (UTC and local); one still
    deferred after two windows carries `session_deferral_stale`."""
    if not any(project.get("sessions") for project in settings.get("projects") or []):
        return []
    moment, found = moment or holder_clock(), []
    for name, entry in sorted(holder_ledger(Path(settings["state_directory"]))["pending"].items()):
        line = {"service": name, "parts": entry.get("parts"), "release": entry.get("release"), "since": entry.get("since"),
                "next_window": holder_window_text(settings, *holder_window_at(settings, moment))}
        try:
            since = datetime.strptime(entry["since"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            since = None
        passed, end = 0, holder_next_window(settings, since or moment)[1]
        while since and end <= moment and passed < HOLDER_STALE_WINDOWS:
            passed, end = passed + 1, holder_window_at(settings, end)[1]
        if passed >= HOLDER_STALE_WINDOWS:
            line["code"] = "session_deferral_stale"
        found.append(line)
    return found


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
    for services in current.get("kept_images", {}).values():
        for image in services.values():
            keep(image, "kept running through the rollback of " + current["sha"] + label)
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


def configured_runner(settings):
    """The runner.json `migration.unattended.runner_config` names, or None when none is named. Read on its own: a setting
    of the migration section that keeps a release from running unattended says nothing about this file's pin."""
    section = settings.get("migration")
    found = section.get("unattended") if isinstance(section, dict) else None
    named = found.get("runner_config") if isinstance(found, dict) else None
    if named is not None and not (isinstance(named, str) and Path(named).is_absolute()):
        raise Refused("migration.unattended.runner_config is not an absolute path; no image is removed")
    return Path(named) if named is not None else None


def pinned_id(path, pinned):
    """The image ID a runner configuration's `image` names now: its own value, or what a tag or reference resolves to.
    None only when Docker says no such image: nothing on this host is left to protect. A lookup that failed otherwise says
    nothing about the image, so it refuses."""
    image = pinned.get("image") if isinstance(pinned, dict) else None
    if not isinstance(image, str):
        raise Refused(f"{path} names no backup image; no image is removed")
    if IMAGE_DIGEST.fullmatch(image):
        return image
    try:
        found = run(["docker", "image", "inspect", "--format", "{{.Id}}", image], timeout=60, reason=True).strip()
    except Refused as exc:
        if "no such image" in str(exc).lower():
            return None
        raise Refused(f"the image {path} pins could not be looked up; no image is removed") from exc
    if not IMAGE_DIGEST.fullmatch(found):
        raise Refused(f"the image {path} pins has no immutable ID; no image is removed")
    return found


def runner_configurations(settings):
    """Each backup runner configuration and its `.before-*` copies, the undo of a move, as (path, content, is a copy). A
    configuration that cannot be read refuses before any removal. Every copy counts: `cp -p` keeps an old time, so no time
    tells the last move, and an operator deletes a copy to let what it names go. A host without the runner and without a
    runner.json runs no backup and has nothing to read; each profile's runner.json is optional, as `deploy` reads them."""
    configured = configured_runner(settings)
    if configured is not None and not present(configured):
        raise Refused(f"cannot read the configured {configured}; no image is removed")
    paths = [path for path in dict.fromkeys(item for item in (configured, *RUNNER_CONFIGS) if item is not None) if present(path)]
    if present(BACKUP_RUNNER) and not paths:
        raise Refused(f"the backup runner {BACKUP_RUNNER} is installed and no runner.json names its image; no image is removed")
    for path in paths:
        yield path, load(path), False
        for entry in directory_entries(path.parent):
            if entry.name.startswith(path.name + ".before-") and present(entry):
                yield entry, load(entry), True


def runner_pins(settings, keep):
    """The image each backup runner's configuration and undo copy pin (ANYM-525). Nothing else protects them once a project
    deploys the `backup` target and no backup container exists between two runs."""
    for path, pinned, undo in runner_configurations(settings):
        image = pinned_id(path, pinned)
        if undo:  # A copy whose reference resolves to nothing has no image left: its undo is lost either way.
            keep(image, f"pinned by the undo copy {path}")
        elif image is None:
            raise Refused(f"the image {path} pins cannot be resolved; no image is removed")
        else:
            keep(image, f"pinned by {path}")


def runner_releases(settings):
    """The release directories a backup runner configuration or undo copy names: its worker exists only during a run, so
    no container mount shows them between two runs (incident 11-05: every daily backup failed after a prune)."""
    named = set()
    for path, content, _ in runner_configurations(settings):
        if not isinstance(content, dict):
            raise Refused(f"cannot read {path}; no image is removed")
        named.update(RELEASE_DIRECTORY.findall(json.dumps(content)))
    return named


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
    runner_pins(settings, keep)
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


def remove_images(settings, *, held=(), deadline=None, reasons=False):
    """Remove the retention report's candidates (production.md "Image retention"): each by its other references and then
    by image ID, never forced or pruned, so Docker still refuses an image a container uses. The caller holds the deploy
    locks of `held` and this state. Like a preparation it takes every other state's deploy lock before the image lock,
    without waiting, and holds them to the end: while another operation holds one it removes nothing, so it never takes
    the image lock from a preparation in progress. IDs and Docker's reasons are reported short, so a receipt holding this
    summary never pins an image. `deadline` (a time.monotonic() value) stops the pass between two images (ANYM-689, the
    preflight before a deploy's disk check) and `reasons` adds how many kept images each keep reason holds."""
    state = Path(settings["state_directory"])
    with ExitStack() as stack:
        mine = {Path(path).resolve() for path in (*held, state)}
        for directory in retention_states(state, removing=True):
            if directory.resolve() not in mine and not free_lock(directory, stack):
                raise Refused(f"another production operation holds the deploy lock of {directory}; no image is removed")
            mine.add(directory.resolve())
        stack.enter_context(image_lock())
        if deadline is not None and time.monotonic() >= deadline:
            # The budget went on the container pass: no discovery starts after it.
            return {"removed": [], "failed": [], "kept": 0, "interrupted": "time budget", "remaining": None}
        # The report refuses a state that appeared meanwhile instead of locking it.
        report = images_report(settings, held=mine, stack=stack)
        removed, failed, summary = [], [], {}
        for number, item in enumerate(report["candidates"]):
            image = item["image"]
            entry = {"image": image.removeprefix("sha256:")[:12], "tags": item["tags"]}
            if deadline is not None and time.monotonic() >= deadline:
                summary = {"interrupted": "time budget", "remaining": len(report["candidates"]) - number}
                break
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
        counts = {}
        if reasons:
            for item in report["keep"]:
                why = IMAGE_DIGEST.sub(lambda found: found[0][7:19], re.sub(r"[0-9a-f]{12,}", "<id>", (item["reasons"] or ["?"])[0]))
                counts[why] = counts.get(why, 0) + 1
        return {"removed": removed, "failed": failed, "kept": len(report["keep"]), **({"kept_reasons": counts} if reasons else {}), **summary}


def retain_images(settings, *, held=(), deadline=None, reasons=False):
    """`remove_images` after a successful release. It never changes the release's outcome: a failure is only reported."""
    try:
        # Only a preflight sets these; the post-release pass is called exactly as before.
        return remove_images(settings, held=held, **{key: value for key, value in (("deadline", deadline), ("reasons", reasons)) if value})
    except Exception as exc:
        return {"skipped": str(exc) if isinstance(exc, Refused) else type(exc).__name__}


PREFLIGHT_BUDGET = 300
# Release directories (production.md "Release directories"): the newest of each state stay, counted along main.
RELEASE_KEEP = 5
RELEASE_TRASH = ".pruned"
RELEASE_BUDGET = 120


def prunable_releases(history, present, protected, keep=RELEASE_KEEP):
    """Directories to remove, from `history` (newest first, main up to the current release): beyond the newest `keep` of
    `present`, never a protected one. A directory outside `history` (newer than the current release, or never on main)
    is never selected: a candidate may still need it."""
    ordered = [sha for sha in history if sha in present]
    return [sha for sha in ordered[keep:] if sha not in protected]


def release_in_use(directory, mounted, current=None, serving=None):
    """Whether a release on main before the current one still matters. Unreadable counts as in use.
    - A plan A apply that failed and was not rolled back: `rollback` still accepts it and needs its snapshots.
    - A preparation that may still apply: it is bound to what ran when it was made (older commits may be prepared), so
      it is stale only when a release was applied since: `current_digest` differs, or a receipt without it is older than
      current.json. An HTTP preparation is stale when another generation serves than the one it names as previous.
    - An HTTP attempt with an unknown outcome, or the receipt that records the retirement of a generation whose
      containers still exist (the container cleanup selects them by it)."""
    try:
        result = directory / "result.json"
        if result.exists() and read(result).get("status") != "applied" and not (directory / "rollback.json").exists():
            return True
        prepared = directory / "prepared.json"
        attempted = directory / "attempted.json"
        if current is not None and prepared.exists() and not result.exists() and not attempted.exists():
            bound = read(prepared).get("current_digest")
            if bound == digest(current) or (bound is None and prepared.stat().st_mtime >= (directory.parent / "current.json").stat().st_mtime):
                return True
        http_attempted = directory / "http-attempted.json"
        http_prepared = directory / "http-prepared.json"
        if serving is not None and http_prepared.exists() and not http_attempted.exists():
            if (read(http_prepared).get("previous") or {}).get("generation") == serving.get("generation"):
                return True
        if http_attempted.exists() and read(http_attempted).get("status") not in ("serving", "startup-failed"):
            return True
        http_result = directory / "http-result.json"
        return http_result.exists() and (read(http_result).get("previous") or {}).get("sha") in mounted
    except Exception:
        return True


def remove_path(path, deadline):
    """A child process, so the time budget also bounds the deletion. Cut short, it stays in the trash for the next pass."""
    subprocess.run(["rm", "-rf", "--", str(path)], check=True, timeout=max(deadline - time.monotonic(), 0.001),
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def remove_tree(path, deadline):
    """Remove `path`, first moved out of the `*/marker` globs by one atomic rename: a partly removed release must never
    read as an unfinished apply, which would pause every deploy. The next pass finishes an interrupted removal."""
    trash = path.parent / RELEASE_TRASH
    trash.mkdir(mode=0o700, exist_ok=True)
    if trash.is_symlink() or path.is_symlink():
        raise Refused("a release directory is a symbolic link")
    os.replace(path, trash / path.name)
    remove_path(trash / path.name, deadline)


def prune_state(directory, mirror, tip, protected, mounted, deadline, summary, *, current=None, serving=None):
    trash = directory / RELEASE_TRASH
    for leftover in (subdirectories(trash) if trash.is_dir() and not trash.is_symlink() else []):
        if time.monotonic() >= deadline:
            summary["interrupted"] = "time budget"
            return
        if leftover.name not in protected | mounted:  # a path may name a directory already moved here
            remove_path(leftover, deadline)
    history = run(["git", "-C", str(mirror), "rev-list", "--first-parent", tip]).split()
    present = {path.name for path in subdirectories(directory) if RELEASE_DIRECTORY.fullmatch(path.name)}
    if tip not in history:
        raise Refused("the current release is not on main")
    for sha in prunable_releases(history, present, protected | mounted):
        if time.monotonic() >= deadline:
            summary["interrupted"] = "time budget"
            return
        if release_in_use(directory / sha, mounted, current, serving):
            continue
        if time.monotonic() >= deadline:  # reading a receipt may have used the rest
            summary["interrupted"] = "time budget"
            return
        try:
            remove_tree(directory / sha, deadline)
            summary["removed"].append(sha[:12])
        except (OSError, Refused, subprocess.SubprocessError) as exc:
            summary["failed"].append({"release": sha[:12], "reason": type(exc).__name__})


def prune_releases(settings, http_state, deadline):
    """Before a deploy's disk check, remove the release directories no rollback or running release needs, so the never
    applied preparations stop pinning their images in the image pass that follows. The caller holds the deploy locks.
    Best effort like the rest of the pass: never raises, and with a state not settled it leaves that state alone."""
    summary = {"removed": [], "failed": []}
    deadline = min(deadline, time.monotonic() + RELEASE_BUDGET)  # the image pass after it frees the image disk
    try:
        state, http_state = Path(settings["state_directory"]), Path(http_state)
        mirror = state / "deploy-source.git"
        containers = run(["docker", "ps", "--all", "--quiet", "--no-trunc"]).split()
        mounted = {sha for value in (json.loads(run(["docker", "inspect", *containers])) if containers else [])
                   for mount in value.get("Mounts") or [] for sha in RELEASE_DIRECTORY.findall(mount.get("Source") or "")}
        try:
            named = runner_releases(settings)
        except Refused as exc:  # read before the first removal: nothing is pruned, not even the trash
            summary["error"], summary["skipped"] = "Refused", f"no release directory is removed: {exc}"
            return summary
        current = read(state / "current.json") if (state / "current.json").exists() else {}
        if current.get("status") == "applied":
            prune_state(state, mirror, current["sha"], {current["sha"], *unfinished(state), *unsettled_migrations(state), *named},
                        mounted, deadline, summary, current=current)
        serving = read(http_state / "http-current.json") if (http_state / "http-current.json").exists() else {}
        if serving.get("status") == "serving":
            prune_state(http_state, mirror, serving["sha"], {serving["sha"], (serving.get("previous") or {}).get("sha"), *named},
                        mounted, deadline, summary, serving=serving)
    except Exception as exc:
        summary["error"] = type(exc).__name__
    return summary


def preflight_cleanup(settings, http, s, http_state):
    """ANYM-689: before a deploy's disk check, remove the containers of retired HTTP generations and then the images they
    pinned, so a host just under the gate recovers itself instead of halting. Best effort: it never raises, never
    waits for a lock and never changes the deploy's outcome; every failure is a line of the summary. One budget bounds
    the whole pass: `DEADLINE` caps every command `run` starts while it is set."""
    global DEADLINE
    deadline = DEADLINE = time.monotonic() + PREFLIGHT_BUDGET
    try:
        try:
            containers = s.clean(http, True)
        except Exception as exc:
            containers = {"error": type(exc).__name__}
        releases = prune_releases(settings, http_state, deadline)
        images = retain_images(settings, held=(http_state, s.GLOBAL_LOCK), deadline=deadline, reasons=True)
    finally:
        DEADLINE = None
    return {"containers": containers, "releases": releases, "images": images}


def cleanup_line(preflight, final, retired=None):
    """The deploy journal's one line: what the cleanup removed (the preflight pass, the retirement of the predecessor in
    this run, the image pass after it) and how much room is left."""
    containers, images = (preflight or {}).get("containers", {}), (preflight or {}).get("images", {})
    later = retired if isinstance(retired, dict) else {}
    try:
        free = round(shutil.disk_usage(CONTAINERD_ROOT).free / 2**30, 2)
    except Exception:
        free = None
    generations = list(dict.fromkeys([*containers.get("generations", []), *later.get("generations", [])]))
    return {"containers_removed": len(containers.get("removed", [])) + len(later.get("removed", [])),
            "generations": generations,
            "images_removed": len(images.get("removed", [])) + len((final or {}).get("removed", [])),
            "releases_removed": len((preflight or {}).get("releases", {}).get("removed", [])),
            "disk_free_after_gib": free,
            **({"disk_warnings": warnings} if (warnings := disk_warnings()) else {})}


def attach_cleanup(state, record, outcome, preflight, retired=None):
    """A halted or errored deploy keeps the cleanup summary too (ANYM-689). Best effort and after the outcome was
    written: only the receipt of this run is rewritten, and a failed write leaves the first one (with its pause reason)."""
    try:
        outcome["preflight"], outcome["cleanup"] = preflight, cleanup_line(preflight, None, retired)
        if outcome is record:
            write(state / "deploy.json", outcome)
    except Exception:
        pass
    return outcome


# The tool's own modules (TOOL_FILES, W5-1b): each is a file beside this one that reads this module's names through `p`
# at call time, and every name it defines is imported here, so `production.<name>` stays the one name callers use and
# tests patch.
class ToolNames:
    """This module's names as attributes, each read at access time from its globals: the `p` of a tool module. A patch
    of `production.<name>` is seen at once, however this file was loaded (imported, run, or by path unregistered)."""

    def __getattr__(self, name):
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(f"production has no {name}") from None


def tool_module(name):
    """`name`.py beside the resolved production.py (scripts/ in a checkout, releases/<sha>/ on the host, also when
    deedkit admin, calendar_rotate.py or the inference worker load this file by its path), registered as `name`, with
    this module's names (ToolNames) as its `p` before its code runs: it never imports another copy of production.py."""
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None or not path.is_file():
        raise ImportError(f"the tool file {path} is missing")
    module = importlib.util.module_from_spec(spec)
    module.p = ToolNames()
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Database copies (ANYM-588, production.md "Database copies") live in release_copies.py, a file of this tool (W5-1c).
tool_module("release_copies")
from release_copies import (  # noqa: E402, F401  every name, re-exported: the patch point stays `production`
    COPIES, COPY_ROOTS, COPY_RETENTION, COPY_POLL, COPY_RESERVE, COPY_TIMEOUT, COPY_OUTPUT, COPY_STOP_READBACK,
    COPY_DATA, SCHEMA_ROUND_TRIP, COPY_CODES, COPY_NAME, COPY_ID, SQL_NAME, DOCKER_NAME, SNAPSHOT_ID, LOCALE, COUNTS,
    INSPECT_COPY, copy_clock, CopyFailed, CopyStuck, settings_digest, copy_source, filesystem, copy_reserve, new_file,
    write_record, device, trusted_parents, copies_root, copy_root, recorded_roots, record_root, copy_roots,
    copy_directories, copy_exec, copy_room, same_filesystem, check_reserve, watched, bounded, helper_output, Keeper,
    data_filesystem, file_digest, schema_lines, dumped, claim, copy_dumps, copy_scratch, sql_lines, scratch_roles,
    discard, copy_verify, snapshot_copy, copy_sql, labelled, stop_copy_containers, copy_gone, release_copy, owned_copy,
    reconcile_copies, settle_copies, expire_copies, database_copy, tend_copies)


# Continuous deployment (production.md "Continuous deployment"): one command that deploys the newest green main
# commit, or pauses for a human. A human creates or removes the pause file; `deploy` only creates it.
PAUSE = Path("/etc/deedkit/autodeploy.pause")
HALT_DOCUMENT = "docs/operations/production.md#halts"
_DOC = "docs/operations/production.md#"
_TOOLS, _DEPLOY, _HTTP = _DOC + "deploy-tool-updates", _DOC + "continuous-deployment", "seamless_release.py status --sha <sha>"
_AUTO, _ROLLBACK = _DOC + "unattended-migrations", _DOC + "roll-back-the-current-release"
# code: (who acts, the next step), production.md "Halts". `owner` only for what harness process.md section 2 keeps for
# the owner's hands or words; a halt's `reason` names the SHA and the module a step's placeholders stand for.
HALTS = {
    "attempt_unresolved": ("agent", _DEPLOY),
    "call_wait_exhausted": ("agent", _DEPLOY),
    "configuration_drift": ("agent", _DOC + "apply-and-verify"),
    "copies_unreconciled": ("agent", _DOC + "database-copies"),
    "deploy_receipt_malformed": ("agent", _DEPLOY),
    "deploy_refused": ("agent", _DEPLOY),
    "deploy_unknown_state": ("agent", "production.py status"),
    "enroll_required": ("agent", "production.py enroll --module <module> --sha <sha> --plan"),
    "host_locks_failed": ("agent", _DEPLOY),
    "http_release_unsettled": ("agent", _DEPLOY),
    "http_retirement_failed": ("agent", _HTTP),
    "main_unreadable": ("agent", _DEPLOY),
    "manifest_invalid": ("agent", _DOC + "recordings-anym-711"),
    "migration_by_hand": ("agent", _DOC + "migration-releases-and-the-backup-gate"),
    "migration_refused": ("agent", _AUTO),
    "migration_unsettled": ("agent", _AUTO),
    "migration_wait_exhausted": ("agent", _AUTO),
    "plan_a_preparation_unfinished": ("agent", _DOC + "prepare-and-inspect"),
    "plan_a_refused": ("agent", _DOC + "prepare-and-inspect"),
    "plan_a_rolled_back": ("agent", _ROLLBACK),
    "plan_a_unknown_state": ("agent", "production.py status"),
    "plan_b_preparation_unfinished": ("agent", _DEPLOY),
    "plan_b_stopped": ("agent", _HTTP),
    "registry_unreachable": ("agent", _DOC + "ci-published-images"),
    "release_not_applied": ("agent", _DOC + "apply-and-verify"),
    "release_not_on_main": ("agent", _DEPLOY),
    "rollback_failed": ("agent", _ROLLBACK),
    "session_pass_failed": ("agent", _DOC + "session-holders-anym-896"),
    "tool_blob_mismatch": ("agent", _TOOLS),
    "tool_by_hand": ("agent", _TOOLS),
    "tool_not_descended": ("agent", _TOOLS),
    "tool_release_mismatch": ("agent", _TOOLS),
    "tool_rolled_back": ("agent", _TOOLS),
    "tool_self_test_failed": ("agent", _TOOLS),
    "tool_switch_failed": ("agent", _TOOLS),
    "tool_update_stopped": ("agent", _TOOLS),
    "tool_update_unresolved": ("agent", _TOOLS),
}


def halt_route(code):
    """The keys a halt adds to the pause file; a code the table lacks still halts, for an agent to read the document."""
    actor, step = HALTS.get(code, ("agent", HALT_DOCUMENT))
    return {"code": code, "actor": actor, "next": step}


def halt_summary(text):
    """What `status` prints of a pause file's text. Only a file that names its route and a past `at` claims an actor and
    a waiting time: an older tool's pause, a hand-written one (`touch` included) or a bad `at` reads `actor: unknown`."""
    try:
        pause = json.loads(text)
    except (ValueError, RecursionError):
        pause = None
    if not isinstance(pause, dict):
        pause = {"reason": text[:1000].strip()}
    summary = {"reason": pause.get("reason"), "code": pause.get("code"), "actor": "unknown",
               "next": pause.get("next", HALT_DOCUMENT)}
    try:
        at = datetime.strptime(pause.get("at"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return summary
    waited = int((datetime.now(timezone.utc) - at).total_seconds())
    routed = isinstance(pause.get("code"), str) and isinstance(pause.get("next"), str)
    if waited >= 0 and routed and pause.get("actor") in ("agent", "owner"):
        summary.update(actor=pause["actor"], waiting=f"waiting for {waited // 3600}h{waited % 3600 // 60}m")
    return summary
CONFIG, HTTP_CONFIG = "/etc/deedkit/production.json", "/etc/deedkit/http-production.json"
# The least time between two `--auto` deploy starts: about one deploy's duration. Deploys never overlap (the host
# locks and the oneshot unit); the interval only stops a run that ended fast from starting the next at once. It was
# 20 minutes for the GitHub API quota, which promotion no longer reads (ANYM-406; ci-deploy-speed 11-09).
DEPLOY_INTERVAL = 5 * 60
# Consecutive runs whose `git ls-remote` or mirror fetch failed before the deployer pauses: a network blip heals by
# itself, a broken mirror or a refused access does not. The timer runs every minute, so an `--auto` run retries a
# failed git or GHCR read only RETRY_SPACING after the last counted failure: the pause still comes after about 10-15
# minutes of failures, never after three minutes.
FETCH_FAILURES = 3
RETRY_SPACING = 4 * 60
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
    tools = [path for path in paths if path in DEPLOY_TOOLS or path in TOOL_FILES or path.startswith(tuple(t for t in DEPLOY_TOOLS if t[-1] == "/"))]
    def current_tool(path):
        # The running tool is the candidate's when its installed file has the candidate's content; a unit this host
        # does not install (no timer) needs no install.
        if path in installed:
            if path in UNITS and not os.path.lexists(installed[path]):
                return True
            try:
                return run(["git", "-C", str(mirror), "rev-parse", f"{candidate}:{path}"]).strip() == blob(installed[path])
            except Refused:
                if path in TOOL_FILES:  # a tool file the candidate deletes waits for the tool update, which installs
                    return False        # the candidate's own list
                raise
        # The verifier is the candidate's when the installed binary is this tool's pinned build and the candidate
        # records that same build (tests keep each commit's two pins equal to its recorded file): a later tool change
        # with an unchanged verifier then installs itself instead of pausing (ANYM-618).
        recorded = f"{VERIFIER_SHA256}  attest-verify\n".encode()
        return (path.startswith("tools/attest-verify/") and VERIFIER.exists()
                and hashlib.sha256(VERIFIER.read_bytes()).hexdigest() == VERIFIER_SHA256
                and run(["git", "-C", str(mirror), "rev-parse", f"{candidate}:{VERIFIER_RECORD}"]).strip()
                == hashlib.sha1(b"blob %d\0" % len(recorded) + recorded).hexdigest())
    return sql, [path for path in tools if not current_tool(path)]


MIGRATION = re.compile(r"services/messaging/messaging_service/migrations/([0-9]{3}_[A-Za-z0-9_]+\.sql)")
CLASSIFIER = "services/messaging/messaging_service/migration_rollout.py"
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
CLASSIFY_PRELUDE = """import json, re, sys
data = json.loads(sys.stdin.read())
def refuse(event, args):
    if event.split(".")[0] in ("open", "import", "socket", "subprocess", "os", "shutil", "ctypes", "fcntl", "urllib",
                               "http", "sqlite3", "pty", "signal", "syslog", "resource", "mmap"):
        raise PermissionError(event)
sys.addaudithook(refuse)
module = {"__name__": "migration_rollout"}
exec(compile(data["source"], "migration_rollout.py", "exec"), module)
"""
# ANYM-743: with the candidate's `risk_class`, each class adds the tier, and a critical file its first critical line and
# statement kind (`manual critical: line 12 DELETE`); a candidate without it names the class alone. hashlib comes first
# for the calendar classifier, as in CALENDAR_PLAN (the audit hook refuses an import; Sol r2 F3).
CLASSIFY = "import hashlib\n" + CLASSIFY_PRELUDE + """def named(text):
    kind, risk = module["rollout_class"](text), module.get("risk_class")
    if risk is None:
        return kind
    tier, line, what = risk(text)
    return kind + (" non-critical" if tier == "non-critical" else f" critical: line {line} {what}")
print(json.dumps([named(text) for text in data["texts"]]))
"""
# The statement kinds `risk_class` names (migration_rollout.RISK_KINDS): an exact list, so a pause line carries no word
# of a migration file (Astra r1 F2).
RISK_KINDS = ("DELETE", "UPDATE", "MERGE", "TRUNCATE", "COPY", "SELECT", "CALL", "WITH", "PERFORM", "EXECUTE",
              "INSERT ON CONFLICT DO UPDATE", "DO", "SET", "CREATE EXTENSION", "CREATE TRIGGER", "CREATE RULE",
              "CREATE other", "DROP TABLE", "DROP SCHEMA", "DROP SEQUENCE", "DROP MATERIALIZED", "DROP EXTENSION",
              "DROP OWNED", "DROP DATABASE", "DROP SERVER", "DROP other", "DROP CASCADE", "DROP COLUMN",
              "ALTER COLUMN TYPE", "ALTER other", "FUNCTION CALL", "WRITING FUNCTION", "other statement", "unparsed",
              "untokenizable")
NAMED_CLASS = re.compile(r"(?:expand|manual|no header)(?: non-critical| critical: line [0-9]{1,6} (?:"
                         + "|".join(map(re.escape, RISK_KINDS)) + "))?")


def classifier_limits():
    """In the classifier child before exec: its address space and the size of a file it writes (its stdout). A limit
    the platform refuses is skipped; the output and input bounds still hold."""
    for limit, value in ((resource.RLIMIT_AS, CLASSIFY_MEMORY), (resource.RLIMIT_FSIZE, CLASSIFY_OUTPUT_LIMIT + 1)):
        with suppress(ValueError, OSError):
            resource.setrlimit(limit, (value, value))


def classifier_child(script, payload):
    """The JSON a classifier child running `script` prints for `payload`, within the CLASSIFY_* bounds; ValueError,
    OSError or a timeout otherwise."""
    with tempfile.TemporaryFile() as out:
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", script], stdout=out, stderr=subprocess.DEVNULL,
                                input=json.dumps(payload).encode(), timeout=CLASSIFY_TIMEOUT,
                                preexec_fn=classifier_limits)
        out.seek(0)
        output = out.read(CLASSIFY_OUTPUT_LIMIT + 1)
    if result.returncode or len(output) > CLASSIFY_OUTPUT_LIMIT:
        raise ValueError("the classifier failed or printed too much")
    return json.loads(output)


# Migration planning and the migration release (ANYM-444, ANYM-449, ANYM-586, production.md "Migration releases") live
# in release_migrations.py, a file of this tool (W5-1d), loaded like release_copies.py above.
tool_module("release_migrations")
from release_migrations import (  # noqa: E402, F401  every name, re-exported: the patch point stays `production`
    migration_classes, database_classes, pause_text, pause_reasons, AUTO_DATABASE, PACKAGE, ROLE_FILE, MIGRATION_NAME,
    BACKUP_SOURCE, STORE_SOURCE, BACKUP_SCHEMAS, UNPREPARED, HOST_PATH, MIGRATION_WAITS, MIGRATE_TIMEOUT,
    REHEARSAL_LIMIT_MS, EXPAND_CODES, MIGRATE_FLAGS, UNATTENDED, CATALOG, MIGRATION_DATA, LEDGER, OTHER_SESSIONS,
    MigrationWait, MigrationRefused, rehearsal_limit, unattended_settings, CALENDAR, CALENDAR_PACKAGE, CALENDAR_FILE,
    CALENDAR_ROLES, CALENDAR_CLASSIFIER, CALENDAR_STORE, CALENDAR_MIGRATION, CALENDAR_TARGET, CALENDAR_RUNTIME,
    CALENDAR_UNPREPARED, CALENDAR_CODES, CALENDAR_PLAN, CALENDAR_LEDGER, CALENDAR_DATA, BUSY, calendar_settings,
    calendar_plan, migration_plans, unattended_plan, expand_plan, live_lines, live_ledger, at_target, migrated_by_hand,
    calendar_ledger, catalog_map, digested, catalog_difference, described, destroys_data, changed_data, data_unchanged,
    calendar_data_unchanged, scratch_lines, expand_run, container_json, calendar_run, calendar_code, calendar_expand,
    calendar_identity, calendar_roles, IDENTITY_SQL, IDENTITY, CALENDAR_IDENTITY, database_identity, migration_settled,
    stable_data, rehearse, producer_reads, BACKUP_UNITS, CALENDAR_FRESHNESS, CALENDAR_FRESHNESS_MAX_AGE,
    calendar_backup_line, INFERENCE_WORKER_RECEIPT, BACKUP_HEALTH_RESULT, BACKUP_HEALTH_PROFILES, BACKUP_HEALTH_WORDS,
    backup_health_lines, inference_worker_line, negotiation_worker_line, leaf_identity, leaf_read, leaf_write,
    stage_leaf, settle_pin, follow_producer, RUNNER_CONFIGS, RUNNER_UNITS, RUNNER_PYTHON, install_backup_runner,
    BACKUP_HEALTH_PROGRAM, BACKUP_HEALTH_CONFIG, BACKUP_HEALTH_SOURCE, install_backup_health, backup_health_files,
    BACKUP_HEALTH_PROBE_S, restore_leaves,
    migration_gate, calendar_gate, migration_receipt, migration_image, NO_CHANGE, step_name, container_name,
    evidence_file, expand_answer, migration_outcome, outcome_text, Progress, resumed_migration, unsettled_outcomes,
    unsettled_migrations, migrate_rehearse, migrate_gate, migrate_expand)


def migrate(settings, http, plans, mirror, base, serving, candidate, release):
    """M2 after preparation, M3 and M4 for each database of the range (K3): the producer check, every copy and its
    rehearsal before any production write, the final gates, then one production `migrate-expand` per database,
    calendar first and then its db-roles and db-roles --check. Returns when every database is at its target with its
    rehearsal's effect; raises MigrationWait or CallActive/CallsUnknown (nothing changed), or MigrationRefused. A
    database migrated by hand gets only the producer check: no copy, rehearsal or migrate-expand."""
    messaging = [plan for plan in plans if plan["database"] == AUTO_DATABASE]
    limit = rehearsal_limit(settings)
    if messaging:
        follow_producer(settings, mirror, messaging[0], candidate, release)
    plans = [plan for plan in plans if not plan.get("by_hand")]
    messaging = [plan for plan in plans if plan["database"] == AUTO_DATABASE]
    if not plans:
        return
    images ={plan["database"]: migration_image(release, candidate, plan["database"]) for plan in plans}
    progress = Progress(release, plans)
    def refused(name, reason, phase):
        progress.update(name, status="refused", phase=phase)
        return progress.refused(name, "refused", reason, phase=phase, reason=reason)
    copies, rehearsals = migrate_rehearse(settings, plans, candidate, images, limit, progress, refused)
    evidence = migrate_gate(settings, http, plans, mirror, base, serving, candidate, release, messaging, images, copies,
                            rehearsals, refused)
    migrate_expand(settings, plans, candidate, images, copies, rehearsals, evidence, progress)


# Deploy tool updates (ANYM-443, production.md "Deploy tool updates"). Each installed tool pair has its own directory
# TOOL_ROOT/releases/<sha>/ with a tool.json; `current` points at one of them and the documented paths production.py
# and seamless_release.py point into `current/`, so Python takes both modules from one directory (sys.path[0] is the
# script's resolved directory). From a range whose only pause is that pair, `deploy` installs the candidate's pair:
# stage, self-test, `activating`, switch `current`, self-test through the documented path, `active`.
TOOL_ROOT = Path("/usr/local/lib/deedkit")
# The tool's files (W5-1a): each release reads the list from its own production.py source, never from the running
# code, so a candidate may add a module and a rollback to an older release checks that release's own list. The two
# documented paths stay the only compatibility links, whatever the list holds.
TOOL_FILES = {"scripts/production.py": "production.py", "scripts/seamless_release.py": "seamless_release.py",
              "scripts/release_enroll.py": "release_enroll.py", "scripts/release_copies.py": "release_copies.py",
              "scripts/release_migrations.py": "release_migrations.py"}
TOOL_LINKS = ("production.py", "seamless_release.py")
TOOL_FILE = re.compile(r"scripts/([a-z_][a-z0-9_]*\.py)")
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


def tool_manifest(source, where):
    """The TOOL_FILES of a production.py `source`, read without running it: the one statement that starts a line with
    `TOOL_FILES =`, a literal dict of `scripts/<module>.py` paths to their file names with both TOOL_LINKS among them.
    Only that statement is parsed, so a syntax error elsewhere is left to the self-test, which also checks that the
    module's TOOL_FILES is this list."""
    files = None
    try:
        lines = source.decode().splitlines()
        if len(starts := [index for index, line in enumerate(lines) if line.startswith("TOOL_FILES =")]) == 1:
            for end in range(starts[0] + 1, min(len(lines), starts[0] + 20) + 1):
                with suppress(SyntaxError):
                    body = ast.parse("\n".join(lines[starts[0]:end])).body
                    break
            else:
                body = []
            if (len(body) == 1 and isinstance(body[0], ast.Assign) and len(body[0].targets) == 1
                    and isinstance(body[0].targets[0], ast.Name)):
                files = ast.literal_eval(body[0].value)
    except (ValueError, TypeError, MemoryError, RecursionError):
        files = None
    if (not isinstance(files, dict)
            or not all(isinstance(path, str) and isinstance(name, str) and (match := TOOL_FILE.fullmatch(path))
                       and match[1] == name for path, name in files.items())
            or not set(TOOL_LINKS) <= set(files.values())):
        raise Refused(f"{where} names no TOOL_FILES: one literal dict of scripts/<module>.py paths to their file "
                      "names, production.py and seamless_release.py among them")
    return files


def tool_record(directory, sha=None):
    """The tool.json of `directory`, which must name the git blob ids of the files its production.py lists."""
    try:
        tool = json.loads((directory / "tool.json").read_text())
        files = tool_manifest((directory / "production.py").read_bytes(), directory / "production.py")
        blobs = {path: blob(directory / name) for path, name in files.items()}
    except (OSError, ValueError) as exc:
        raise Refused(f"cannot read the tool files of {directory}: {type(exc).__name__}") from exc
    if (not isinstance(tool, dict) or set(tool) != {"sha", "blobs", "installed_at"} or tool["blobs"] != blobs
            or not RELEASE_DIRECTORY.fullmatch(str(tool["sha"])) or sha not in (None, tool["sha"])
            or not isinstance(tool["installed_at"], str)):
        raise Refused(f"{directory / 'tool.json'} does not name this release and the blob ids of its files")
    return tool


def tool_release(sha):
    """The checked release TOOL_ROOT/releases/<sha>: exactly the files its production.py lists and tool.json, an
    optional bytecode cache, no symlink, all of them the operator's alone."""
    directory = TOOL_ROOT / "releases" / sha
    try:
        tool_path(directory, stat.S_IFDIR)
        names = set(os.listdir(directory))
        for name in names:
            tool_path(directory / name, stat.S_IFDIR if name == "__pycache__" else stat.S_IFREG)
        if "__pycache__" in names:
            for name in os.listdir(directory / "__pycache__"):
                tool_path(directory / "__pycache__" / name, stat.S_IFREG)
        if "production.py" not in names:
            raise Refused(f"{directory} holds no production.py")
        required = {*tool_manifest((directory / "production.py").read_bytes(), directory / "production.py").values(),
                    "tool.json"}
        if not required <= names or names - required - {"__pycache__"}:
            raise Refused(f"{directory} must hold exactly {', '.join(sorted(required))}")
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
        links = {"current": None, **{name: "current/" + name for name in TOOL_LINKS}}
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


def stage_tool(sha, manifest, contents, blobs):
    """releases/<sha>.staging-<random> with the files of the candidate's `manifest` (its TOOL_FILES), every file
    written to a temporary name, synced, then renamed into place."""
    staging = TOOL_ROOT / "releases" / f"{sha}.staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    record = json.dumps({"sha": sha, "blobs": blobs, "installed_at": now()}, indent=2) + "\n"
    files = {name: contents[path] for path, name in manifest.items()}
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
                    f"{old} again", code="tool_switch_failed")
    if at == old:
        s.durable_write(state / "tool-update.json", {**update, "status": "not-activated", "closed_at": now()})
        return None
    return halt(f"the tool update {old} -> {new} is unresolved: {TOOL_ROOT / 'current'} names neither; inspect it, "
                f"the previous tool is python3 {TOOL_ROOT / 'releases' / old / 'production.py'}", code="tool_update_unresolved")


def update_tool(settings, configs, s, mirror, candidate, base, tool, *, dry_run, halt):
    """Install the candidate's tool, the files its production.py lists (production.md "Deploy tool updates"). The run
    then ends `tool-updated`, and the next one deploys with the new tool."""
    state, old = Path(settings["state_directory"]), tool["sha"]
    receipt = state / "tool-update.json"
    # Forward only, from the commit whose images the verifier accepted, each file checked against its git blob id.
    if not on_main(mirror, old, candidate):
        return halt(f"the candidate {candidate} does not descend from the installed tool {old}; install the tool by "
                    "hand (production.md)", code="tool_not_descended", sha=candidate)
    contents, blobs, manifest, paths = {}, {}, None, ["scripts/production.py"]
    while paths:  # production.py first: its TOOL_FILES names the other files
        path = paths.pop(0)
        blobs[path] = run(["git", "-C", str(mirror), "rev-parse", f"{candidate}:{path}"]).strip()
        contents[path] = run(["git", "-C", str(mirror), "cat-file", "blob", f"{candidate}:{path}"], binary=True)
        if hashlib.sha1(b"blob %d\0" % len(contents[path]) + contents[path]).hexdigest() != blobs[path]:
            return halt(f"{path} of {candidate} does not match its git blob id; the tool is not updated", code="tool_blob_mismatch",
                        sha=candidate)
        if manifest is None:
            try:
                manifest = tool_manifest(contents[path], f"{path} of {candidate}")
            except Refused as exc:
                return halt(f"{exc}; the tool is not updated", code="tool_by_hand", sha=candidate)
            paths = [other for other in manifest if other != path]
    last = tool_update_receipt(state)
    if last and last["status"] == "rolled-back" and last.get("blobs") == blobs:
        return halt(f"the tool of {last['to']} was rolled back (tool-rollback); install a changed tool by hand or merge "
                    "one", code="tool_rolled_back", sha=candidate)
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
                return halt(f"{target} exists but is not the candidate's tool: {exc}; inspect it", code="tool_release_mismatch",
                            sha=candidate)
            # Its bytecode cache is not covered by the blob ids, and `-B` still reads it: the source runs instead.
            if (target / "__pycache__").exists():
                shutil.rmtree(target / "__pycache__")
            script = target / "production.py"
        else:
            staged = stage_tool(candidate, manifest, contents, blobs)
            script = staged / "production.py"
        if error := self_test_error(script, configs, candidate):
            if staged:
                shutil.rmtree(staged)
            return halt(f"the self-test of tool {candidate} failed: {error}; the installed tool {old} is unchanged",
                        code="tool_self_test_failed", sha=candidate)
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
                        f"points at {old} again", code="tool_switch_failed", sha=candidate)
        s.durable_write(receipt, {**update, "status": "active", "activated_at": now()})
    except Exception as exc:
        message = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        return halt(f"the tool update {old} -> {candidate} stopped: {message}; inspect {TOOL_ROOT} and {receipt}",
                    code="tool_update_stopped", sha=candidate)
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


def self_test(config_path, http_path, verifier=None):
    """This tool's check before `deploy` runs it (production.md "Deploy tool updates"): every module its TOOL_FILES
    lists from this directory (an imported one loaded from here, any other compiled), its tool.json, both configurations and the current receipts read with this code, the verifier (or the
    staged build `verifier` names, read and never run) and its pin. It runs under the guard at the top of this file."""
    if not GUARDED:
        raise Refused("self-test must be the first argument")
    directory = RUNNING.parent
    s = http_release_tool()
    if Path(s.__file__).resolve().parent != directory:
        raise Refused("seamless_release.py was not imported from the directory of production.py")
    tool = tool_record(directory)
    if tool_manifest(RUNNING.read_bytes(), RUNNING) != TOOL_FILES:
        raise Refused("the TOOL_FILES this module runs with is not the list its source states")
    for name in TOOL_FILES.values():
        loaded = [module for key in (name[:-3], "scripts." + name[:-3]) if (module := sys.modules.get(key))]
        if any(Path(getattr(module, "__file__", "") or "").resolve() != directory / name for module in loaded):
            raise Refused(f"{name} was not imported from the directory of production.py")
        if not loaded and name != RUNNING.name:
            compile((directory / name).read_bytes(), str(directory / name), "exec", dont_inherit=True)
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
        check_verifier(verifier and Path(verifier))
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


def running_tool(s):
    """This run's tool files by repository path: this module, the seamless_release it imported, and every other file
    its TOOL_FILES lists from this module's directory."""
    return {path: RUNNING if name == RUNNING.name else Path(s.__file__) if name == "seamless_release.py"
            else RUNNING.parent / name for path, name in TOOL_FILES.items()}


def runs_tool(tool, s):
    """Whether this run is the installed `tool`: the blob ids of its files are exactly the ones tool.json records."""
    try:
        return {path: blob(file) for path, file in running_tool(s).items()} == tool["blobs"]
    except OSError:
        return False


def stale_tool(s):
    """Why this run is not the tool `current` names on a host with a checked self-updating layout, or None. A refused
    or missing layout is left to the range check, which pauses for it when the tool changes."""
    try:
        tool = installed_tool()
    except Refused:
        return None
    if tool and not runs_tool(tool, s):
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


def deploy(settings, http, *, auto=False, dry_run=False, attested=None, configs=(CONFIG, HTTP_CONFIG), pre_claim=None,
           flag=None):
    """Deploy the newest green main commit with plan A and then plan B, or pause with a reason (production.md).
    `flag` (`--session-restart-now` services and `--reason`, ANYM-896) runs only a session-only pass over those."""
    if flag is not None:
        holder_flag(settings, *flag)  # refused before any lock or stop
    if PAUSE.exists():
        paused = {"status": "paused", "reason": PAUSE.read_text()[:1000].strip()}
        if not dry_run:
            # Copies still expire while paused (ANYM-588 C6); the pause already stops everything else.
            try:
                tend_copies(settings, locked=False)
            except Exception as exc:
                paused["copies"] = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        return paused
    def halt(reason, status="halted", *, code, **fields):
        # The pause stops every later run until a human removes it; nothing is retried.
        outcome = {"status": status, "reason": reason, "at": now(), **fields, **halt_route(code)}
        if dry_run:
            return {"status": "plan", "would_pause": reason, **fields}
        PAUSE.write_text(json.dumps(outcome) + "\n")
        PAUSE.chmod(0o600)
        return outcome
    s = http_release_tool()
    with ExitStack() as held:
        waited = 0
        try:
            held.enter_context(host_locks(settings, http, s))
            if not auto and not dry_run:  # --auto: the unit's flock wrapper holds it for this whole run.
                waited = held.enter_context(backup_gate(settings, backup_wait(settings)))
        except BackupRunning as exc:
            return {**exc.line(), "reason": str(exc)}
        except Busy as exc:
            return {"status": "busy", "reason": str(exc)}
        except Exception as exc:
            return halt("deploy could not take the host locks: " + (str(exc) if isinstance(exc, Refused)
                                                                     else type(exc).__name__), code="host_locks_failed")
        # tool-rollback or another run may have paused or switched the tool while this run waited for the locks.
        if PAUSE.exists():
            return {"status": "paused", "reason": PAUSE.read_text()[:1000].strip()}
        if unsettled := unsettled_backup():
            return {"status": "busy", "reason": unsettled}
        if rotating := rotation_running():
            return {"status": "busy", "code": "rotation_running", "reason": rotating}
        if stale := stale_tool(s):
            return {"status": "error", "reason": stale + "; nothing changed, the next run uses it"}
        if flag is not None:
            return {"status": "plan", "would_restart": holder_flag(settings, *flag)[0]} if dry_run else holder_flagged(
                settings, http, flag)
        if not dry_run:
            try:
                tend_copies(settings, locked=True)
            except Exception as exc:
                return halt(str(exc) if isinstance(exc, CopyStuck) else "database copies could not be reconciled or"
                            " expired: " + (str(exc) if isinstance(exc, Refused) else type(exc).__name__), code="copies_unreconciled")
        # A candidate is taken only when every image plan A or plan B pulls is attested, so publication that is still
        # running or failed for one target never starts a release that stops halfway.
        targets = sorted(set(published_targets(settings)) | set(published_targets(http)) | {PROOF_TARGET})
        return deploy_locked(settings, http, s, auto=auto, dry_run=dry_run, halt=halt, configs=configs,
                             attested=attested or (lambda sha: [published(target, sha) for target in targets]),
                             backup_waited=waited, pre_claim=pre_claim)


def deploy_locked(settings, http, s, *, auto, dry_run, attested, halt, configs, backup_waited=0, pre_claim=None):
    state = Path(settings["state_directory"])
    record = {}
    try:
        return deploy_steps(settings, http, s, auto=auto, dry_run=dry_run, attested=attested, halt=halt, record=record,
                            configs=configs, backup_waited=backup_waited, pre_claim=pre_claim)
    except Exception as exc:
        # Every failure the steps do not classify themselves ends here: a pause, unless nothing changed yet
        # and it was the network.
        message = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        if record:
            outcome = halt(f"deploy of {record['sha']} stopped in an unknown state: {message}; inspect it", code="deploy_unknown_state")
            record.update(outcome, finished_at=now())
            write(state / "deploy.json", record)
            return record
        if transient(exc):
            return {"status": "error", "reason": message + "; nothing changed, the next run retries"}
        return halt("deploy was refused before any change: " + message, code="deploy_refused")


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


def settle_http_generation(http, s, serving, http_state, *, dry_run, halt):
    """Finish a pending retirement before selecting another release; return its outcome when it must wait."""
    if serving.get("status") == "serving-retirement-pending" and not serving_settled(serving):
        # Retiring the managed predecessor HTTP pair is promote's last step, safe to repeat; it never cuts a request
        # short. It changes the router (the admission fence), so its failure pauses.
        if dry_run:
            return serving, {"status": "plan", "sha": serving.get("sha"),
                             "steps": ["retire the previous HTTP generation"]}
        try:
            s.retire(http, serving["sha"])
        except Exception as exc:
            message = str(exc) if isinstance(exc, Refused) else type(exc).__name__
            return serving, halt(f"retiring the HTTP generation before {serving['sha']} failed: {message}; "
                                 "inspect seamless_release.py status --sha " + serving["sha"], code="http_retirement_failed")
        serving = read(http_state / "http-current.json")
        if not serving_settled(serving):
            return serving, {"status": "waiting", "reason": "the previous HTTP generation is still draining"}
    return serving, None


def newest_attested_candidate(mirror, state, http_state, base, head, attested):
    """Choose the newest published, unattempted main commit; an unreachable registry propagates to the caller."""
    for sha in run(["git", "-C", str(mirror), "rev-list", "--first-parent", f"{base}..{head}"]).split():
        # A commit whose deployment was attempted is never retried, and older ones never follow it.
        if (state / sha / "attempted.json").exists() or (http_state / sha / "http-attempted.json").exists():
            break
        # Green CI is the attestation `publish` signs after `CI gate` (promotable); no GitHub API request.
        try:
            attested(sha)
        except Unattested:
            continue  # not published (yet): its CI still runs or failed, and an older commit may be deployed
        return sha
    return None


def retry_after(last, moment=None):
    """When an `--auto` run may read git or GHCR again after a counted failure (`fetch_failures`, `unreachable`), or
    None. A record without a readable time never holds a run back: the read is retried and counted as before."""
    times = []
    for key in ("fetch_failures", "unreachable"):
        try:
            times.append(datetime.strptime(last[key]["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
        except (KeyError, TypeError, ValueError):
            continue
    if not times:
        return None
    retry = max(times) + timedelta(seconds=RETRY_SPACING)
    return retry if retry > (moment or datetime.now(timezone.utc)) else None


def deploy_receipt(state, halt):
    """The previous deploy receipt and its counters: `(halted, None)` when it is malformed, else `(None, counters)`."""
    try:
        last = read(state / "deploy.json") if (state / "deploy.json").exists() else {}
        started, failures, misses, waits = deploy_counts(last)
        gated = last.get("migration_waits", {"count": 0})["count"]
        if type(gated) is not int or gated < 0:
            raise ValueError("migration wait count")
    except (ValueError, TypeError, AttributeError, KeyError):
        return halt(f"the previous deploy receipt {state / 'deploy.json'} is malformed; inspect it", code="deploy_receipt_malformed"), None
    return None, (last, started, failures, misses, waits, gated)


def deploy_fetch_main(state, base, serving, last, failures, dry_run, halt):
    """Read main's head: `(result, None)` ends the run (a failed read or a pause), else `(None, mirror)`; the mirror is
    None when everything already serves main."""
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
                        f"inspect the network and {state / 'deploy-source.git'}", code="main_unreadable"), None
        if not dry_run:
            write(state / "deploy.json", {**rest, "fetch_failures": {"count": failures, "at": now(), "reason": message}})
        return {"status": "fetch_failed", "reason": message, "failures": failures}, None
    if "fetch_failures" in last:
        # From the caller's receipt too: a later write of it in this run (a GHCR count, a deploy record) must never
        # restore the count this successful fetch ended.
        del last["fetch_failures"]
        if not dry_run:
            write(state / "deploy.json", last)
    return None, mirror


def deploy_candidate(state, http_state, mirror, base, head, serving, last, misses, attested, dry_run, halt):
    """The release this run deploys and the commit it starts from: `(result, None, None)` ends the run, else
    `(None, candidate, since)`."""
    if unsettled := unsettled_migrations(state):
        # An unattended migration may have changed the database before its plan A ran: that release is finished
        # first, whatever main took since (ANYM-449 M4).
        if len(unsettled) != 1 or serving["sha"] != base or unsettled[0] == base or not on_main(mirror, base, unsettled[0]):
            return halt("an unattended migration is unsettled: " + ", ".join(unsettled) + "; inspect its migration.json",
                        code="migration_unsettled"), None, None
        candidate, since = unsettled[0], base
    elif serving["sha"] != base:
        # Plan A is ahead of the HTTP pair: a deploy whose HTTP release stopped before any change, or plan A applied
        # by hand.
        candidate, since = base, serving["sha"]
    else:
        try:
            candidate = newest_attested_candidate(mirror, state, http_state, base, head, attested)
        except Unreachable as exc:
            # GHCR could not be read: no older commit is taken instead; nothing changed.
            if misses + 1 >= FETCH_FAILURES:
                return halt(f"GHCR or git could not be read {misses + 1} times in a row, last: {exc}",
                            code="registry_unreachable"), None, None
            if not dry_run:
                write(state / "deploy.json", {**{key: value for key, value in last.items() if key != "unreachable"},
                                              "unreachable": {"count": misses + 1, "at": now(), "reason": str(exc)}})
            return {"status": "error", "reason": f"{exc}; nothing changed, the next run retries"}, None, None
        if candidate is None:
            if "unreachable" in last and not dry_run:
                write(state / "deploy.json", {key: value for key, value in last.items() if key != "unreachable"})
            return {"status": "waiting", "reason": "no newer main commit has attested images yet", "sha": base}, None, None
        since = base
    return None, candidate, since


def deploy_plan(settings, s, configs, mirror, base, since, candidate, sql, pending, dry_run, halt):
    """The migration plans of the range: `(result, None)` ends the run (a tool update or a pause), else `(None, plans)`."""
    # M1 (ANYM-449, ANYM-586 K3): a messaging or calendar range migrate-expand may run without a person migrates
    # instead of pausing.
    plans = migration_plans(settings, mirror, since, candidate, sql) if sql and not pending and candidate != base else None
    if (reasons := [] if plans else pause_text(sql, pending, migration_classes(mirror, since, candidate, sql))) and not sql and set(pending) <= TOOL_FILES.keys():
        # Only the tool's files wait for an install: the self-updating layout installs it, when this run is its tool.
        try:
            tool = installed_tool()
        except Refused as exc:
            tool = reasons.append(f"the tool does not update itself: {exc}")
        if tool and not runs_tool(tool, s):
            tool = reasons.append(f"the tool does not update itself: this run is not {TOOL_ROOT / 'current'}")
        if tool:
            return update_tool(settings, configs, s, mirror, candidate, base, tool, dry_run=dry_run, halt=halt), None
    if reasons:
        if sql:
            return halt("; ".join(reasons), code="migration_by_hand", sha=candidate), None
        return halt("; ".join(reasons), code="tool_by_hand", sha=candidate), None
    return None, plans


def deploy_closers(record, state, misses, halt):
    """The two ways a started deploy ends: `finish` records a status, `retry` a change-free error."""
    def finish(status, **fields):
        record.update(status=status, finished_at=now(), **fields)
        write(state / "deploy.json", record)
        return record
    def retry(exc, message):
        # A network error changed nothing; git or GHCR failing to be read the third run in a row pauses.
        if isinstance(exc, Unreachable):
            if misses + 1 >= FETCH_FAILURES:
                return finish(**halt(f"GHCR or git could not be read {misses + 1} times in a row, last: {message}",
                                    code="registry_unreachable"))
            record["unreachable"] = {"count": misses + 1, "at": now(), "reason": message}
        return finish("error", reason=message + "; nothing changed, the next run retries")
    return finish, retry


def deploy_plan_a(settings, http, plans, mirror, base, serving, candidate, release, gated, waits, halt, finish, retry):
    """Plan A: migrations, preparation and apply, with the rollback of an unhealthy apply. None: applied and healthy."""
    migrated = False
    try:
        # M2 admission (read-only) before preparation; a run that finds migration.json resumes from it (M4).
        if plans and not (migrated := resumed_migration(settings, plans, candidate, release)):
            for plan in plans:
                # A range that also touches calendar never skips messaging (Astra r1 F2: its recovery reads both).
                migration_gate(settings, http, plan, mirror, base, serving["sha"], admission=len(plans) == 1)
        if not (release / "prepared.json").exists():
            if release.exists():
                return finish(**halt(f"an earlier preparation of {candidate} did not finish; inspect {release}",
                                     code="plan_a_preparation_unfinished"))
            prepare(settings, candidate)
        # Before the producer pin (migrate) and apply: both read the installed runner as the candidate's.
        install_backup_runner(settings, candidate, release)
        if plans and not migrated:
            migrate(settings, http, plans, mirror, base, serving["sha"], candidate, release)
            migrated = True
        apply(settings, candidate)
    except Exception as exc:
        message = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        if isinstance(exc, MigrationRefused):
            return finish(**halt(message, code="migration_refused"))
        if isinstance(exc, MigrationWait) and not (release / "attempted.json").exists():
            if gated + 1 >= MIGRATION_WAITS:
                return finish(**halt(f"the migration of {candidate} waited {gated + 1} runs in a row: {message}",
                                     code="migration_wait_exhausted"))
            return finish("waiting", reason=message + "; nothing changed, the next run retries",
                          migration_waits={"count": gated + 1, "at": now(), "reason": message})
        if migrated:
            assert plans
            message += " (" + " and ".join(f"{plan['database']} is already at schema {plan['target']}"
                                           for plan in plans) + ")"
        if isinstance(exc, (CallActive, CallsUnknown)) and not (release / "attempted.json").exists():
            # Nothing was stopped: the prepared release waits for a later run; the fourth wait in a row pauses.
            if waits + 1 >= CALL_WAITS:
                return finish(**halt(f"plan A of {candidate} waited {waits + 1} runs in a row for calls: {message}; "
                                     "inspect the calls service", code="call_wait_exhausted"))
            return finish("waiting", reason=message + "; nothing changed, the next run retries",
                          call_waits={"count": waits + 1, "at": now(), "reason": message})
        if not (release / "attempted.json").exists():
            # A network or quota error before preparation started or after it finished changed nothing.
            if transient(exc) and ((release / "prepared.json").exists() or not release.exists()):
                return retry(exc, message)
            return finish(**halt(f"plan A of {candidate} was refused before any change: {message}", code="plan_a_refused"))
        result = read(release / "result.json") if (release / "result.json").exists() else {}
        if result.get("status") != "unhealthy":
            return finish(**halt(f"plan A of {candidate} stopped in an unknown state: {message}; inspect it", code="plan_a_unknown_state"))
        # The previous release still fits: a range either leaves the SQL inventory alone (pause_reasons) or only
        # expands the schema (ANYM-449 M5), and the base runs on the target schema; a migration is never reversed.
        try:
            rollback(settings, candidate, http)
        except Exception as again:
            detail = str(again) if isinstance(again, Refused) else type(again).__name__
            return finish(**halt(f"plan A of {candidate} failed its health wait and its rollback failed: {detail}; "
                                 "production state is unknown, inspect it", code="rollback_failed"))
        if migrated:
            assert plans
            named = len(plans) > 1 or plans[0]["database"] != AUTO_DATABASE
            def schemas(text):
                return " and ".join((f"{plan['database']} " if named else "") + text.format(**plan) for plan in plans)
            return finish(**halt(f"plan A of {candidate} failed its health wait after migrating "
                                 f"{schemas('schema {base} to {target}')}; rolled back to {base} on "
                                 f"{schemas('schema {target}')}", code="plan_a_rolled_back", status="rolled-back"))
        return finish(**halt(f"plan A of {candidate} failed its health wait; rolled back to {base}",
                             code="plan_a_rolled_back", status="rolled-back"))
    return None


def deploy_plan_b(http, s, http_state, candidate, halt, finish, retry):
    """Plan B (the HTTP pair): `(result, None)` ends the run, else `(None, served)`."""
    try:
        if not (http_state / candidate / "http-prepared.json").exists():
            if (http_state / candidate).exists():
                return finish(**halt(f"an earlier HTTP preparation of {candidate} did not finish; inspect it",
                                     code="plan_b_preparation_unfinished")), None
            s.prepare(http, candidate)
        served = s.promote(http, candidate)
    except Exception as exc:
        message = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        directory = http_state / candidate
        if transient(exc) and not (directory / "http-attempted.json").exists() and (
                (directory / "http-prepared.json").exists() or not directory.exists()):
            return retry(exc, message), None
        return finish(**halt(f"plan A of {candidate} is applied, but its HTTP release stopped: {message}; "
                             "inspect seamless_release.py status --sha " + candidate, code="plan_b_stopped")), None
    return None, served


def deploy_finish(settings, s, state, http_state, finish, served, preflight=None):
    outcome = finish("deployed", http=served.get("status"))
    # Recorded as deployed first: removing old release images never changes that outcome, nor does adding its summary.
    outcome["image_removal"] = retain_images(settings, held=(http_state, s.GLOBAL_LOCK))
    outcome["preflight"] = preflight
    outcome["cleanup"] = cleanup_line(preflight, outcome["image_removal"], served.get("cleanup"))
    try:
        write(state / "deploy.json", outcome)
    except Exception:
        outcome["image_removal"]["receipt"] = "not written; deploy.json keeps the deployed receipt without this summary"
    return outcome


def deploy_steps(settings, http, s, *, auto, dry_run, attested, halt, record, configs, backup_waited=0, pre_claim=None):
    state, http_state = Path(settings["state_directory"]), Path(http["state_directory"])
    # A tool update that stopped between `activating` and `active` is settled before anything else.
    if reconciled := reconcile_tool(settings, configs, s, dry_run=dry_run, halt=halt):
        return reconciled
    halted, counts = deploy_receipt(state, halt)
    if halted is not None:
        return halted
    last, started, failures, misses, waits, gated = counts
    if auto and started and (datetime.now(timezone.utc) - started).total_seconds() < DEPLOY_INTERVAL:
        return {"status": "waiting", "reason": f"less than {DEPLOY_INTERVAL // 60} minutes since the last deploy started"}
    if auto and (retry := retry_after(last)):
        return {"status": "waiting", "reason": f"a git or GHCR read failed; the next try is at {retry:%H:%M:%SZ}"}
    if "call_waits" in last or "migration_waits" in last:
        # Waits for calls or a migration gate count only in a row: this run carries the count on only when it waits
        # again.
        last = {key: value for key, value in last.items() if key not in ("call_waits", "migration_waits")}
        if not dry_run:
            write(state / "deploy.json", last)
    if reason := unresolved(state, http_state):
        return halt(reason, code="attempt_unresolved")
    current = read(state / "current.json") if (state / "current.json").exists() else {}
    serving = read(http_state / "http-current.json") if (http_state / "http-current.json").exists() else {}
    if current.get("status") != "applied" or not re.fullmatch(r"[0-9a-f]{40}", current.get("sha", "")):
        return halt(f"the current release is {current.get('status', 'missing')}, not applied; apply one by hand first",
                    code="release_not_applied")
    base = current["sha"]
    serving, retirement = settle_http_generation(http, s, serving, http_state, dry_run=dry_run, halt=halt)
    if retirement is not None:
        return retirement
    if not serving_settled(serving) or not re.fullmatch(r"[0-9a-f]{40}", serving.get("sha", "")):
        return halt(f"the HTTP release is {serving.get('status', 'missing')}; inspect seamless_release.py status",
                    code="http_release_unsettled")
    halted, mirror = deploy_fetch_main(state, base, serving, last, failures, dry_run, halt)
    if halted is not None:
        return halted
    if mirror is None:  # ANYM-896: the night's session-only pass runs when nothing new is to be deployed
        return holder_nightly(settings, http, state, current, serving, {"status": "current", "sha": base}, auto=auto,
                              dry_run=dry_run, halt=halt)
    head = run(["git", "-C", str(mirror), "rev-parse", "refs/heads/main"]).strip()
    if not on_main(mirror, base, head) or not on_main(mirror, serving["sha"], base):
        return halt("the current release is not on main, or the serving HTTP release is not an ancestor of it",
                    code="release_not_on_main")
    if reason := unresolved(state, http_state, lambda: mirror, serving["sha"]):
        return halt(reason, code="attempt_unresolved")
    if drift := configuration_drift(settings, http, current, serving):
        # ANYM-745 M3: a drift that only an enrollment explains names the enrollment command, not "by hand".
        if enrolled := enrollment_only(mirror, head, settings, http, current, serving):
            return halt(enroll_required(enrolled[0], head), code="enroll_required")
        return halt(drift + "; apply the next release by hand", code="configuration_drift")
    installed = {**running_tool(s), **{path: SYSTEMD_DIRECTORY / Path(path).name for path in UNITS}}
    halted, candidate, since = deploy_candidate(state, http_state, mirror, base, head, serving, last, misses, attested,
                                                dry_run, halt)
    if halted is not None:
        return halted
    # ANYM-745: the gate reads the range of the release this run would deploy, not the tip of main; a catch-up of the
    # HTTP pair to the applied plan A (candidate == base) installs no new manifest.
    if candidate != base and (pending := enrollment_pending(mirror, since, candidate, settings)):
        reason, fields = enrollment_halt(pending[0], candidate)
        if isinstance(pending[0], ManifestInvalid):
            return halt(reason, code="manifest_invalid", sha=candidate, **fields)
        return halt(reason, code="enroll_required", sha=candidate, **fields)
    # ANYM-914: before a tool update or a preparation, refused as `deploy_refused` (nothing changed).
    bypassed = candidate != base and check_pre_claim(state, lambda: tree_carries(state, candidate, SESSION_CLAIM),
                                                     candidate, pre_claim)
    sql, pending = range_changes(mirror, since, candidate, installed)
    halted, plans = deploy_plan(settings, s, configs, mirror, base, since, candidate, sql, pending, dry_run, halt)
    if halted is not None:
        return halted
    if dry_run:
        migration = [f"migrate {plan['database']} schema {plan['base']} to {plan['target']} after a verified copy and "
                     "its rehearsal" + (", then db-roles and db-roles --check" if plan["database"] == CALENDAR else "")
                     for plan in plans or ()]
        # ANYM-743: every added migration with its class and tier, also when the range runs unattended.
        return {"status": "plan", "sha": candidate, "base": base,
                "steps": migration + (["plan A"] if candidate != base else []) + ["plan B"],
                **({"migrations": classes} if (classes := migration_classes(mirror, since, candidate, sql)) else {})}
    record.update(sha=candidate, base=base, started_at=now(), status="deploying")
    if backup_waited:  # ANYM-744
        record["backup_wait_seconds"] = backup_waited
    if bypassed:
        record["pre_claim_rollback"] = pre_claim
    write(state / "deploy.json", record)
    finish, retry = deploy_closers(record, state, misses, halt)
    # ANYM-689: containers of retired HTTP generations and the images they pinned go before the disk check of the
    # preparation; the summary rides in deploy.json whether the deploy ends or halts.
    preflight = preflight_cleanup(settings, http, s, http_state)
    release = state / candidate
    if candidate != base:
        halted = deploy_plan_a(settings, http, plans, mirror, base, serving, candidate, release, gated, waits, halt, finish,
                               retry)
        if halted is not None:
            return attach_cleanup(state, record, halted, preflight)
    halted, served = deploy_plan_b(http, s, http_state, candidate, halt, finish, retry)
    if halted is not None:
        return attach_cleanup(state, record, halted, preflight)
    return deploy_finish(settings, s, state, http_state, finish, served, preflight)


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


# `status` holds the deploy lock: one docker read of its service list, and all of them together, end by these seconds.
STATUS_READ, STATUS_BUDGET = 30, 60


def http_failed_attempts(path):
    """`status`: plan B attempts whose candidate failed to start, each with whether the tool stopped it (ANYM-757:
    `candidate_stopped`, absent on attempts before it). Nothing without plan B; a read failure is named, never raised."""
    if not os.path.lexists(path):
        return []
    try:
        http_state = Path(config(path)["state_directory"])
        attempts = [(item.parent.name, read(item)) for item in sorted(http_state.glob("*/http-attempted.json"))]
        return [{"sha": sha, **{key: value[key] for key in ("candidate_stopped",) if key in value}}
                for sha, value in attempts if value.get("status") == "startup-failed"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, Refused) as exc:
        return {"error": type(exc).__name__}


def project_services(settings, current):
    """`status` (ANYM-525): each selected service of each configured project with its container's state and revision
    label; `mark` names one not running, on another revision than `current` (an applied release only: after a rollback
    current.json names the release that was left) or unreadable. Selected fields only, never a container's environment;
    a read failure is named, never raised."""
    expected, found = current.get("sha") if current.get("status") == "applied" else None, []
    kept = {(item.get("name"), service): revision for item in ((current.get("rollback") or {}).get("projects") or [])
            for service, revision in (item.get("kept") or {}).items()} if expected else {}
    until = time.monotonic() + STATUS_BUDGET
    def docker_read(argv):
        if (left := until - time.monotonic()) <= 0:
            raise TimeoutError("status budget")
        return run(argv, timeout=min(STATUS_READ, left))
    for project in settings.get("projects") or []:
        for service in project["services"]:
            item = {"project": project["name"], "service": service}
            try:
                ids = docker_read(["docker", "ps", "--all", "--filter", "label=com.docker.compose.oneoff=False",
                                   "--filter", "label=com.docker.compose.project=" + project["name"],
                                   "--filter", "label=com.docker.compose.service=" + service,
                                   "--format", "{{.ID}}"]).split()
                if len(ids) > 1:
                    item.update(state=f"{len(ids)} containers", revision=None, mark="unknown")
                elif not ids:
                    item.update(state="no container", revision=None, mark="not running")
                else:
                    state, _, revision = docker_read(["docker", "inspect", "--format",
                                                      '{{.State.Status}} {{index .Config.Labels '
                                                      '"org.opencontainers.image.revision"}}', ids[0]]).strip().partition(" ")
                    item.update(state=state, revision=revision or None)
                    if state != "running":
                        item["mark"] = "not running"
                    elif expected and revision != kept.get((project["name"], service), expected):
                        item["mark"] = "differs from current.json"
            except Exception as exc:
                item.update(mark="unknown", error=type(exc).__name__)
            found.append(item)
    return found


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


# Enrollment of a new module (ANYM-745, production.md "Enrollment") lives in release_enroll.py, a file of this tool
# beside this one (TOOL_FILES, W5-1b), loaded like release_copies.py above.
tool_module("release_enroll")
from release_enroll import (  # noqa: E402, F401  every name, re-exported: the patch point stays `production`
    MODULE_DIRECTORY, ENROLL_OWNER, ENROLL_FORBIDDEN_SERVICES, VERIFY_SCRIPT, BACKUP_FILES, MODULE_NAME,
    NETWORK_NAME, BRIDGE_NAME, ENV_NAME, KEY_PATH, ENROLL_CODES, EGRESS_CLASSES, MANIFEST_KEYS, EnrollRefused,
    EnrollRefusals, manifest_keys, manifest_text, manifest_list, repo_path, host_path, check_manifest,
    check_networks, checked_manifest, repo_file, repo_blob_id, read_manifest, read_env_file, private_config_nodes,
    private_config_keys, credential_file_checks, serving_gateway_images, additions_only, candidate_policy,
    key_digest, check_policy_candidate, compose_document, enroll_plan, check_external_networks,
    check_private_config, blocked_address, check_egress, ENROLL_RECEIPT, ENROLL_HEALTH_SECONDS,
    ENROLL_HEALTH_POLL, GATEWAY_SERVICE, CONNECT_CODE, run_input, file_sha256, replace_file, replace_like,
    enroll_calls, settle_halt, settle_finish, settle_retry, Enrollment, EnrollStop, public_http, enroll_apply,
    enroll_undo, enroll_command, enroll_required, ManifestInvalid, enrollment_halt, MANIFEST_FILE,
    module_manifests, enrollment_pending, without_enrollments, enrollment_only)


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["verify", "prepare", "prepare-router", "apply", "rollback", "status",
                                           "images", "check-image", "deploy", "self-test", "tool-rollback",
                                           "copy-database", "enroll"])
    parser.add_argument("--sha")
    parser.add_argument("--target", choices=sorted(BUILDS))
    parser.add_argument("--config", help="default " + CONFIG + "; status: the autodeploy unit's (active_config)")
    parser.add_argument("--http-config", default=HTTP_CONFIG, help="deploy, rollback, status: seamless_release.py's")
    parser.add_argument("--auto", action="store_true", help="deploy: at most one deploy start per 5 minutes, and a failed read retried 4 minutes later (timer)")
    parser.add_argument("--dry-run", action="store_true", help="deploy: print the plan or pause reason, change nothing")
    parser.add_argument("--remove", action="store_true", help="images: remove the listed candidates")
    parser.add_argument("--database", help="copy-database: a name under the configuration's migration.databases")
    parser.add_argument("--module", help="enroll: the module of deploy/modules/<module>.json")
    parser.add_argument("--plan", action="store_true", help="enroll: print the enrollment plan, change nothing")
    parser.add_argument("--apply", action="store_true", help="enroll: perform the plan named by --plan-digest")
    parser.add_argument("--plan-digest", help="enroll --apply: the plan_digest of the reviewed plan")
    parser.add_argument("--undo", action="store_true", help="enroll: reverse an applied enrollment")
    parser.add_argument("--pre-claim-rollback", metavar="REASON",
                        help="deploy, apply, rollback: return to a tree without session_claim.py after claims ran (ANYM-914)")
    parser.add_argument("--session-restart-now", metavar="SERVICES",
                        help="deploy: recreate these session services at once (ANYM-896), with --reason")
    parser.add_argument("--reason", help='deploy --session-restart-now: "incident:<runbook record>" or '
                                         '"owner:<the owner\'s line, quoted>"')
    parser.add_argument("--verifier", help="self-test: check this staged verifier build against the pin instead of "
                                           "the installed one (attest-verify install, production.md)")
    return parser


def locked_action(args):
    """Run state commands while retaining each command's existing lock scope."""
    settings, http = config(args.config), None
    state = Path(settings["state_directory"])
    with ExitStack() as locks:
        if args.action == "prepare-router":
            if os.geteuid() != 0:
                raise Refused("router preparation requires the production host operator")
            canonical = Path("/var/lib/deedkit-production")
            locks.enter_context(lock(canonical))
            if state.resolve() != canonical.resolve():
                locks.enter_context(lock(state))
        elif args.action == "rollback":
            # Plan B's state is read, and the rollback runs, under the locks seamless_release.py holds (its
            # global lock also without plan B, where the host has one); only an absent default HTTP
            # configuration can mean no plan B (ANYM-642).
            s = http_release_tool()
            http = (None if args.http_config == HTTP_CONFIG and not os.path.lexists(args.http_config)
                    else s.validate_settings(config(args.http_config)))
            locks.enter_context(host_locks(settings, http, s) if http else s.locks(settings)
                                if s.GLOBAL_LOCK.is_dir() else lock(state))
        else:
            locks.enter_context(lock(state))
        if args.action == "status":
            result = read(state / "current.json") if (state / "current.json").exists() else {"status": "not promoted by this tool"}
            result["config"] = str(args.config)
            if (state / "deploy.json").exists():
                result["deploy"] = read(state / "deploy.json")
            if PAUSE.exists():
                pause = PAUSE.read_text(errors="replace")
                result["autodeploy_pause"] = pause[:1000].strip()
                result["autodeploy_halt"] = halt_summary(pause)
            if failed := http_failed_attempts(args.http_config):
                result["http_failed_attempts"] = failed
            if services := project_services(settings, result):
                result["project_services"] = services
            if warnings := disk_warnings():
                result["disk_warnings"] = warnings
            if pending := holder_pending(settings):  # ANYM-896 criterion 6
                result["session_pending"] = pending
        elif args.action == "images":
            result = remove_images(settings) if args.remove else images_report(settings)
        elif args.action == "copy-database":
            with database_copy(settings, args.database, args.sha) as copied:
                result = copied
        else:
            if not re.fullmatch(r"[0-9a-f]{40}", args.sha or ""):
                raise Refused("an exact lowercase 40-character commit SHA is required")
            if args.action == "apply":
                # ANYM-914: a by-hand apply returns to a tree without session claims like a deploy does.
                bypassed = check_pre_claim(state, lambda: (state / args.sha / "source" / SESSION_CLAIM).is_file(),
                                           args.sha, args.pre_claim_rollback)
                # ANYM-744: wait for a running backup, then hold its lock for the whole apply (the call wait included).
                with backup_gate(settings, backup_wait(settings)) as waited:
                    result = apply(settings, args.sha, backup_waited=waited,
                                   pre_claim=args.pre_claim_rollback if bypassed else None)
                    # Printed only: result.json and current.json stay the release receipts.
                    result = {**result, "image_removal": retain_images(settings)}
            else:
                result = (rollback(settings, args.sha, http, pre_claim=args.pre_claim_rollback) if args.action == "rollback" else
                          {"prepare": prepare, "prepare-router": prepare_router}[args.action](settings, args.sha))
    return result


def execute_action(args):
    if args.action == "check-image":
        if not args.target:
            raise Refused("select a target for container verification")
        source = Path(__file__).resolve().parents[1]
        sha = run(["git", "-C", str(source), "rev-parse", "HEAD"]).strip()
        return {"sha": sha, "image": build_image(source, sha, args.target, check=True)}
    if args.action == "verify":
        return verify(args.sha or "")
    if args.action == "self-test":
        return self_test(args.config, args.http_config, args.verifier)
    if args.action == "tool-rollback":
        if os.geteuid() != 0:
            raise Refused("tool-rollback requires the production host operator")
        s = http_release_tool()
        return tool_rollback(config(args.config), s.validate_settings(config(args.http_config)), s)
    if args.action == "enroll":
        if os.geteuid() != 0:
            raise Refused("enroll requires the production host operator")
        return enroll_command(args)
    if args.action == "deploy":
        if os.geteuid() != 0:
            raise Refused("deploy requires the production host operator")
        if (args.session_restart_now is None) != (args.reason is None) or args.auto and args.session_restart_now:
            raise Refused("session_flag_invalid: --session-restart-now and --reason go together, never with --auto")
        settings = config(args.config)
        flag = None if args.session_restart_now is None else (args.session_restart_now, args.reason)
        result = deploy(settings, http_release_tool().validate_settings(config(args.http_config)), auto=args.auto,
                        dry_run=args.dry_run, configs=(args.config, args.http_config), pre_claim=args.pre_claim_rollback,
                        flag=flag)
        # ANYM-896 criterion 6: the pending session changes, on stdout only (deploy.json keeps the run's receipt).
        return {**result, "session_pending": pending} if (pending := holder_pending(settings)) else result
    return locked_action(args)


def print_result(args, result, parser):
    if args.action == "self-test":
        print(json.dumps(result))
    elif args.action == "deploy":
        # One line per run for the journal; a new pause, an error or a failed fetch exits 1, so the timer unit shows failed.
        print(json.dumps(result, sort_keys=True))
        if result["status"] in ("halted", "rolled-back", "error", "fetch_failed"):
            parser.exit(1)
        if result.get("code") == BackupRunning.code:  # ANYM-744: a manual deploy met a backup past its bound
            parser.exit(EX_TEMPFAIL)
    elif args.action == "enroll":
        print(json.dumps(result, indent=2))
        if result["status"] == "refused":
            parser.exit(2)
        if result["status"] not in ("plan", "enrolled", "undone"):
            parser.exit(1)
    else:
        # Receipts contain revisions/image IDs, never resolved Compose environments.
        if args.action == "prepare":
            result = {key: result[key] for key in ("sha", "ci", "images", "references")}
        print(json.dumps(result, indent=2))
        if args.action == "status":
            # Beside the JSON, not in it: a reader that parses stdout keeps getting one document.
            print(calendar_backup_line(), file=sys.stderr)
            for line in backup_health_lines():
                print(line, file=sys.stderr)
            print(inference_worker_line(), file=sys.stderr)
            if (line := negotiation_worker_line(config(args.config))) is not None:
                print(line, file=sys.stderr)


def main(argv=None):
    parser = argument_parser()
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.verifier and args.action != "self-test":
            raise Refused("--verifier is only for self-test; every other command uses the installed verifier")
        args.config = args.config or (active_config() if args.action in ("status", "enroll") else CONFIG)
        print_result(args, execute_action(args), parser)
    except BackupRunning as exc:  # apply: nothing was written
        print(json.dumps(exc.line()))
        parser.exit(EX_TEMPFAIL)
    except Exception as exc:
        message = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        if isinstance(exc, OSError) and exc.filename:
            message += " " + str(exc.filename)
        parser.exit(1, "promotion refused: " + message + "\n")


if __name__ == "__main__":
    main()
