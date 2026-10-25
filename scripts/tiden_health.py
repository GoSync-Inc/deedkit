#!/usr/bin/env python3
"""One read-only report of whether Tiden still matches the code at one main commit (ANYM-360, TIDEN-CI.md).

Four metrics, each `pass`, `fail` or `unknown` with its reason: drift (the watermark covers TARGET), unlinked tests
(per suite and overall, against the Tiden main run of TARGET, read after the `Tiden sync` run of TARGET has finished,
ANYM-438), stale anchors (repo_file sources of untracked paths) and Done specs with an open check marker. With
`--sync-repository` a fifth, the sync backlog (ANYM-503): every recent first-parent main commit up to TARGET has a
successful `Tiden sync` run. Exit 0 when all pass, 1 when any fails, 2 when none fails and any is
unknown. Every input must be provably complete; anything else makes the metrics that need it unknown, never green.
It writes nothing to Tiden, and to Git only a fetch of main when `--fetch` or `--sync-backlog-only` is given (the
workflow passes `--fetch`). With `--sync-repository` it also lists that repository's workflow runs through `gh api`
(read-only). `--sync-backlog-only` (the orchestrator's watcher) measures the sync backlog alone up to the fetched main
head, without Tiden, and prints one line per commit.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

REPOSITORY = "github.com/GoSync-Inc/deedkit"
# Anchors recorded before the rename carry the former repository name, or none (as scripts/tiden_drift.py).
FORMER_REPOSITORY = "github.com/GoSync-Inc/anymsg"
ANCHOR_REPOSITORIES = {"", REPOSITORY, FORMER_REPOSITORY}
# The 2026-10-16 reconciliation kept the former repository's anchors as provenance of their recorded revision
# (`historical_source_id`) and anchored each moved file again under the messaging module
# (docs/quality/tiden-reconciliation-2026-10-16.md). Such an anchor is historical, not stale, while its spec also
# anchors that moved path and the path is tracked.
MOVED_PREFIX = "services/messaging/"
ENVIRONMENT = "github-actions-synthetic"
SHARE_PERCENT = 5  # an unlinked share of active cases at or above this fails, per suite and overall
DRIFT_WAIT, TESTS_WAIT, POLL = 15 * 60, 30 * 60, 30  # deadlines in seconds from the start of the report
# Date of the initial Done audit whose record is on ANYM-360 (22 Done specs read; 8 moved to Review with markers, by
# the tiden-hygiene worker on review/2026-10-18-tiden-hygiene-done-audit). While unset the Done metric is unknown.
DONE_AUDIT: str | None = "2026-10-18"
MARKER = re.compile(r"\[open: (?:live|measured)\]")
PHRASE = re.compile(r"live recheck pending|remains open|Not run:")
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)", re.DOTALL)
NO_SPEC_LIST = "config/tiden-unlinked-tests.json"
CATEGORIES = ("missing-spec", "helper", "trivial", "duplicate", "dead-code")
ENTRY_FIELDS = {"caseId", "externalId", "category", "reason"}
# Fixed suites, one path rule each, in this order; anything else is unassigned and fails by itself.
SUITES = ("root-python", "calendar", "calls-frontend", "calls", "gateway", "release-router-go", "whatsmeow-go",
          "whatsapp-node", "npm", "ci")
CALLS_WITHOUT_PATH = ("s:tests.test_voice", "s:tests.test_webrtc")
BARE_GO_TEST = re.compile(r"[^/]*_test\.go")
# Tiden's Go reporter records bare file names, so the router's cases are told apart by their package.
ROUTER_SIGNATURE = "s:github.com/GoSync-Inc/deedkit/services/release-router::"
SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SYNC_WORKFLOW, SYNC_PAGE_SIZE, SYNC_MAX_PAGES = "tiden-sync.yml", 100, 10
SYNC_SLACK = timedelta(days=1)  # a run that processes TARGET is created after TARGET exists; the slack covers clock skew
# The sync backlog (ANYM-503). The rollout commit, the base of the pull request that added the check: it and older
# commits are not checked (sync runs before 1a4b78d carry no `Tiden sync <sha>` title).
SYNC_BACKLOG_SINCE = "b61f412a0ab5e2c596d268f159cba4061949886d"
CI_WORKFLOW = "ci.yml"
BACKLOG_WINDOW = timedelta(days=7)
SYNC_STUCK = timedelta(minutes=45)  # an active sync run started longer ago is stuck
CI_GRACE = timedelta(minutes=30)  # after the CI push run completes, sync B has this long to succeed
CI_STUCK = timedelta(minutes=90)  # a CI attempt running longer is stuck
CI_ABSENT = timedelta(minutes=60)  # a commit pushed longer ago without a CI push run is unsynced
ACTIVE = ("queued", "in_progress", "waiting", "requested", "pending")
SYNCED, PENDING, UNSYNCED = "synced", "pending", "unsynced"
DISPATCH = "gh workflow run tiden-sync.yml --ref main -f sha={sha}"
TIMEOUT = 120
RETRY_DELAY = 2  # seconds before the one retry of a listing
RETRY_MIN = 10  # a retry needs at least this many seconds left of the call's one TIMEOUT budget
PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"


class Unknown(Exception):
    """An input cannot be proven complete or current; the metrics that need it are unknown."""

    def __init__(self, reason: str, evidence: dict | None = None):
        super().__init__(reason)
        self.evidence = evidence


def suite(case: dict) -> str:
    path, external = case.get("filePath") or "", case.get("externalId") or ""
    # The router's package signature wins over every path rule: its rows carry bare or arbitrary file names.
    if path.startswith("services/release-router/") or external.startswith(ROUTER_SIGNATURE):
        return "release-router-go"
    if path.startswith("tests/"):
        return "root-python"
    if path.startswith("services/calendar/"):
        return "calendar"
    if path.startswith("services/calls/frontend/"):
        return "calls-frontend"
    if path.startswith("services/calls/") or not path and external.startswith(CALLS_WITHOUT_PATH):
        return "calls"
    if path.startswith("services/gateway/"):
        return "gateway"
    if path.startswith("whatsapp/whatsmeow/") or BARE_GO_TEST.fullmatch(path):
        return "whatsmeow-go"
    if path.startswith("whatsapp/"):
        return "whatsapp-node"
    if path.startswith(("packages/npm/", "packages/wake-relay/")):  # one `npm` CI step runs both
        return "npm"
    if path.startswith(".github/"):
        return "ci"
    return "unassigned"


def shown(text: str, limit: int = 300) -> str:
    """Text for logs: control characters and non-ASCII escaped, so a name or spec line cannot forge output lines."""
    text = text if text.isprintable() and text.isascii() else text.encode("unicode_escape").decode("ascii")
    return text if len(text) <= limit else text[:limit] + "..."


def run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess:
    """One Git command; a timeout or a launch failure is unknown, named by its subcommand only."""
    try:
        return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Unknown(f"git {arguments[0]} timed out") from None
    except OSError:
        raise Unknown(f"git {arguments[0]} could not run") from None


def git(root: Path, *arguments: str) -> bytes:
    completed = run_git(root, *arguments)
    if completed.returncode:
        raise Unknown(f"git {arguments[0]} exited {completed.returncode}")
    return completed.stdout


def count(root: Path, span: str) -> int:
    value = git(root, "rev-list", "--count", span).strip()
    if not value.isdigit():
        raise Unknown("git rev-list returned no count")
    return int(value)


def commit(root: Path, ref: str) -> str:
    value = git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode("ascii", "replace").strip()
    if not SHA.fullmatch(value):
        raise Unknown(f"{shown(ref)} does not resolve to a commit")
    return value


def present(root: Path, sha: str) -> bool:
    return not run_git(root, "cat-file", "-e", f"{sha}^{{commit}}").returncode


def ancestor(root: Path, older: str, newer: str) -> bool:
    completed = run_git(root, "merge-base", "--is-ancestor", older, newer)
    if completed.returncode not in (0, 1):
        raise Unknown(f"git merge-base exited {completed.returncode}")
    return completed.returncode == 0


def tracked(root: Path, target: str) -> set[str]:
    """Tracked paths at TARGET, NUL-delimited and compared literally; a name that is not UTF-8 matches no anchor."""
    return {path.decode("utf-8", "surrogateescape") for path in git(root, "ls-tree", "-rz", "--name-only", target)
            .split(b"\0") if path}


class Tiden:
    """Read-only Tiden CLI listings; its stderr and failing stdout are never shown, since they may echo request data."""

    def __init__(self, cli: str, product: str, workspace: str):
        self.cli, self.product, self.workspace = cli, product, workspace

    def call(self, *arguments: str) -> dict:
        """One listing; a failed one is repeated once inside the same TIMEOUT budget, since nothing here writes."""
        start = time.monotonic()
        try:
            return self.once(*arguments)
        except Unknown:
            left = TIMEOUT - (time.monotonic() - start) - RETRY_DELAY
            if left < RETRY_MIN:
                raise
        time.sleep(RETRY_DELAY)
        return self.once(*arguments, timeout=left)

    def once(self, *arguments: str, timeout: float | None = None) -> dict:
        label = " ".join(arguments[:2])
        try:
            completed = subprocess.run([self.cli, *arguments, "--product-id", self.product, "--workspace-id",
                                        self.workspace, "--format", "json"], capture_output=True, text=True,
                                       timeout=timeout or TIMEOUT)
        except subprocess.TimeoutExpired:
            raise Unknown(f"tiden {label} timed out") from None
        except (OSError, UnicodeDecodeError):
            raise Unknown(f"tiden {label} could not run or returned undecodable output") from None
        if completed.returncode:
            raise Unknown(f"tiden {label} exited {completed.returncode}")
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError:
            raise Unknown(f"tiden {label} returned malformed JSON") from None
        if not isinstance(value, dict):
            raise Unknown(f"tiden {label} returned no object")
        return value

    def pages(self, key: str, *arguments: str, size: int = 200) -> list[dict]:
        """Every row of a paged listing: it counts only with an empty final token, a stable integer total equal to
        its rows and unique IDs."""
        label = " ".join(arguments[:2])
        cursor, seen, rows, ids, total = "", set(), [], set(), None
        while True:
            if cursor in seen or len(seen) >= 1000:
                raise Unknown(f"tiden {label} pagination does not terminate")
            seen.add(cursor)
            page = self.call(*arguments, "--page-size", str(size), *(["--page-token", cursor] if cursor else []))
            pagination, batch = page.get("pagination"), page.get(key)
            if not isinstance(pagination, dict) or not isinstance(batch, list):
                raise Unknown(f"tiden {label} returned no {key} page")
            count, cursor = pagination.get("totalCount"), pagination.get("nextPageToken")
            if type(count) is not int or count < 0 or not isinstance(cursor, str) or total not in (None, count):
                raise Unknown(f"tiden {label} pagination is incomplete")
            total = count
            for row in batch:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in ids:
                    raise Unknown(f"tiden {label} has a missing or duplicate id")
                ids.add(row["id"])
                rows.append(row)
            if not cursor:
                if len(rows) != total:
                    raise Unknown(f"tiden {label} returned {len(rows)} of {total} rows")
                return rows

    def main_branch(self) -> str:
        listing = self.call("branch", "list")
        branches, pagination = listing.get("branches"), listing.get("pagination") or {}
        if not isinstance(branches, list) or not isinstance(pagination, dict) or pagination.get("nextPageToken"):
            raise Unknown("tiden branch list is incomplete")
        ids = [branch.get("id") for branch in branches if isinstance(branch, dict) and branch.get("name") == "main"]
        if len(ids) != 1 or not isinstance(ids[0], str) or not ids[0]:
            raise Unknown("tiden branch list names no single main branch")
        return ids[0]

    def specs(self) -> list[dict]:
        listing = self.call("spec", "list", "--branch", "main", "--include-sources")
        specs, pagination = listing.get("requirements"), listing.get("pagination")
        if (not isinstance(specs, list) or not isinstance(pagination, dict) or pagination.get("nextPageToken") != ""
                or type(pagination.get("totalCount")) is not int or pagination["totalCount"] != len(specs)):
            raise Unknown("tiden spec list is incomplete")
        ids = set()
        for spec in specs:
            if not isinstance(spec, dict) or not isinstance(spec.get("id"), str) or not spec["id"] or spec["id"] in ids:
                raise Unknown("tiden spec list has a missing or duplicate spec id")
            ids.add(spec["id"])
            # The JSON API omits zero counts and empty arrays; a nonempty array still needs its exact count.
            sources, count = spec.get("sources", []), spec.get("sourceCount", 0)
            if (not isinstance(sources, list) or not all(isinstance(source, dict) for source in sources)
                    or type(count) is not int or count != len(sources)):
                raise Unknown("tiden spec list has malformed or incomplete sources")
            # A repeated or missing source ID could stand in for an omitted source, and an untyped one could hide a
            # repo_file anchor: either makes the inventory unprovable.
            source_ids = [source.get("id") for source in sources]
            if (not all(isinstance(value, str) and value for value in source_ids)
                    or len(set(source_ids)) != len(source_ids)
                    or not all(isinstance(source.get("sourceType"), str) and source["sourceType"] for source in sources)):
                raise Unknown("tiden spec list has a source with a missing or duplicate id or no type")
        return specs

    def cases(self) -> list[dict]:
        cases = []
        for row in self.pages("tests", "test", "list", "--branch", "main"):
            if row.get("kind") != "case":
                continue
            # A zero count and an empty path are omitted by the JSON API, like sourceCount.
            count = row.get("linkedRequirementCount", 0)
            if (type(count) is not int or count < 0 or not isinstance(row.get("filePath", ""), str)
                    or not isinstance(row.get("externalId", ""), str)):
                raise Unknown("tiden test list has a malformed case")
            cases.append(row)
        return cases

    def runs(self) -> list[dict]:
        return self.pages("runs", "run", "list", "--branch", "main", "--environment", ENVIRONMENT, size=100)

    def results(self, seq: int) -> list[dict]:
        return self.pages("results", "run", "results", str(seq), "--latest-only")


def moment(value: object) -> datetime | None:
    """An ISO 8601 timestamp with a zone, else None."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else None


def valid_run(row: object) -> bool:
    return (isinstance(row, dict) and type(row.get("id")) is int and type(row.get("run_attempt")) is int
            and isinstance(row.get("display_title"), str) and isinstance(row.get("html_url"), str)
            and isinstance(row.get("status"), str) and moment(row.get("updated_at")) is not None
            and isinstance(row.get("conclusion"), str if row.get("status") == "completed" else type(None)))


class Actions:
    """Read-only listing of the `Tiden sync` workflow runs of main through `gh api` (token: GH_TOKEN). A failure is
    named by the command only; the response body, which may echo request data, is never shown."""

    def __init__(self, repository: str, gh: str = "gh", clock=time.monotonic):
        self.repository, self.gh, self.clock = repository, gh, clock

    def page(self, number: int, created: str, end: float, workflow: str = SYNC_WORKFLOW,
             filters: tuple[str, ...] = ()) -> dict:
        if not REPOSITORY_NAME.fullmatch(self.repository) or any(not part.strip(".") for part in self.repository.split("/")):
            raise Unknown("the sync lookup repository is not owner/name")
        endpoint = f"repos/{self.repository}/actions/workflows/{workflow}/runs"
        command = [self.gh, "api", "--method", "GET", endpoint, "-f", "branch=main",
                   *(part for value in filters for part in ("-f", value)), "-f", f"created=>={created}",
                   "-F", f"per_page={SYNC_PAGE_SIZE}", "-F", f"page={number}"]
        try:
            # All pages of one lookup share one TIMEOUT budget, so a slow API cannot outlast the report's deadlines.
            remaining = end - self.clock()
            if remaining <= 0:
                raise Unknown("gh api timed out")
            completed = subprocess.run(command, capture_output=True, text=True, timeout=remaining)
        except subprocess.TimeoutExpired:
            raise Unknown("gh api timed out") from None
        except (OSError, UnicodeDecodeError):
            raise Unknown("gh api could not run") from None
        if completed.returncode:
            raise Unknown(f"gh api exited {completed.returncode}")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError:
            raise Unknown("gh api returned malformed JSON") from None

    def runs(self, since: datetime, within: float | None = None) -> list[dict]:
        """Every run created at or after SINCE (a rerun keeps its `created_at`, so no run of TARGET is hidden by
        age); the pages count only when their rows equal `total_count`, which GitHub caps at 1000. The pages take
        WITHIN seconds (what the report deadline leaves) at the most."""
        return self.listing(since, self.clock() + (TIMEOUT if within is None else min(TIMEOUT, within)))

    def listing(self, since: datetime, end: float, workflow: str = SYNC_WORKFLOW,
                filters: tuple[str, ...] = ()) -> list[dict]:
        """The runs of WORKFLOW on main created at or after SINCE, every page before END (monotonic)."""
        created = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows: list[dict] = []
        for number in range(1, SYNC_MAX_PAGES + 1):
            page = self.page(number, created, end, workflow, filters)
            batch, total = page.get("workflow_runs") if isinstance(page, dict) else None, \
                page.get("total_count") if isinstance(page, dict) else None
            if not isinstance(batch, list) or type(total) is not int or total < 0:
                raise Unknown("gh api returned no run page")
            if not all(valid_run(row) for row in batch):
                raise Unknown("gh api returned a malformed or duplicate run")
            rows += batch
            if len({row["id"] for row in rows}) != len(rows):
                raise Unknown("gh api returned a malformed or duplicate run")
            if len(rows) == total:
                return rows
            if len(rows) > total or len(batch) < SYNC_PAGE_SIZE:
                break
        raise Unknown("gh api pagination is incomplete")


def sync_runs(runs: list[dict], target: str) -> list[dict]:
    """The sync runs that processed TARGET: the run title names the commit, a skipped run processed nothing."""
    return [run for run in runs if run["display_title"] == f"Tiden sync {target}" and run["conclusion"] != "skipped"]


def decisive(runs: list[dict]) -> dict:
    """The latest finished run: a rerun moves `updated_at` forward while the run ID stays; ties go to the greater
    (id, attempt)."""
    return max(runs, key=lambda run: (moment(run["updated_at"]), run["id"], run["run_attempt"]))


def fingerprint(runs: list[dict]) -> list[tuple]:
    """What a later look must find unchanged: every kept run with its attempt, state and completion time."""
    return sorted((run["id"], run["run_attempt"], run["status"], run["conclusion"], run["updated_at"]) for run in runs)


def await_sync(actions: Actions, target: str, since: datetime, deadline: float, clock, sleep) -> list[dict]:
    """The `Tiden sync` runs of TARGET once none is active; none, or an active one at the deadline, is unknown."""
    while True:
        kept = sync_runs(actions.runs(since, deadline - clock()), target)
        if kept and all(run["status"] == "completed" for run in kept):
            return kept
        sleep(min(POLL, max(deadline - clock(), 0)))
        if clock() >= deadline:
            raise Unknown("no finished Tiden sync run of TARGET")


def metric(state: str, reason: str, **values) -> dict:
    return {"state": state, "reason": reason, "numbers": {}, "findings": [], "warnings": [], **values}


def backlog_scope(root: Path, head: str, start: datetime) -> tuple[list[tuple[str, datetime]], int]:
    """The first-parent commits of main after SYNC_BACKLOG_SINCE up to HEAD, newest first, whose committer date is
    within BACKLOG_WINDOW of START; and how many in that range are older, so not checked."""
    if not present(root, SYNC_BACKLOG_SINCE) or not ancestor(root, SYNC_BACKLOG_SINCE, head):
        raise Unknown(f"the rollout commit {SYNC_BACKLOG_SINCE} is not on main's history")
    commits, older = [], 0
    text = git(root, "log", "--first-parent", "--format=%H %cI", f"{SYNC_BACKLOG_SINCE}..{head}").decode("ascii")
    for line in text.splitlines():
        sha, _, stamp = line.partition(" ")
        committed = moment(stamp)
        if not SHA.fullmatch(sha) or committed is None:
            raise Unknown("git log returned a malformed commit line")
        if committed > start - BACKLOG_WINDOW:
            commits.append((sha, committed))
        else:
            older += 1
    return commits, older


def started(run: dict) -> datetime:
    stamp = moment(run.get("run_started_at"))
    if stamp is None:
        raise Unknown("gh api returned a run without its start time")
    return stamp


def backlog_decision(sha: str, committed: datetime, syncs: list[dict], ci: list[dict] | None,
                     start: datetime) -> tuple[str, str, str | None]:
    """(state, reason, last sync run URL) of one main commit (ANYM-503). Each listed run carries its latest attempt;
    skipped sync runs processed nothing. CI is None when the caller has not listed it: only a synced commit or one
    with an active sync run is decided without it."""
    kept = sync_runs(syncs, sha)
    last = decisive(kept) if kept else None
    url = last["html_url"] if last else None
    if any(run["conclusion"] == "success" for run in kept):
        return SYNCED, "", url
    active = [run for run in kept if run["status"] in ACTIVE]
    if active:
        newest = max(active, key=started)
        if start - started(newest) < SYNC_STUCK:
            return PENDING, f"Tiden sync run {newest['status']}", url
        return UNSYNCED, f"stuck {newest['status']}", url
    if ci is None:
        raise Unknown("the CI push runs were not listed")
    previous = f"last sync {last['conclusion']}" if last else "no sync run"
    pushes = [run for run in ci if run.get("event") == "push" and run.get("head_sha") == sha]
    if pushes:
        run = max(pushes, key=lambda run: (moment(run.get("created_at")) or started(run), run["id"]))
        if run["status"] == "completed":
            if start - moment(run["updated_at"]) >= CI_GRACE:
                return UNSYNCED, f"{previous}; CI push run {run['conclusion']} at {run['updated_at']}", url
            return PENDING, "CI push run completed less than 30 min ago", url
        if start - started(run) < CI_STUCK:
            return PENDING, f"CI push run {run['status']}", url
        return UNSYNCED, f"{previous}; CI attempt running over 90 min", url
    if start - committed > CI_ABSENT:
        return UNSYNCED, f"{previous}; no CI push run", url
    return PENDING, "no CI push run yet", url


def backlog_metric(root: Path, actions: Actions, head: str, start: datetime) -> dict:
    """Every first-parent main commit in scope up to HEAD has a successful `Tiden sync` run (ANYM-503). Its own
    listings share one TIMEOUT budget; a failed, truncated or late listing makes the metric unknown."""
    commits, older = backlog_scope(root, head, start)
    numbers = {"since": SYNC_BACKLOG_SINCE, "head": head, "in_scope": len(commits), "not_checked": older}
    # The rollout commit, its history and the commits older than the window are named as not checked, never counted.
    skipped = f"not checked: {SYNC_BACKLOG_SINCE[:12]} and earlier, {older} later commit(s) older than 7 days"
    if not commits:
        return metric(PASS, f"no main commit in scope; {skipped}", numbers=numbers, commits=[])
    end = actions.clock() + TIMEOUT
    lower = min(committed for _, committed in commits) - SYNC_SLACK
    syncs = actions.listing(lower, end)
    ci = None
    if any(not any(run["conclusion"] == "success" or run["status"] in ACTIVE for run in sync_runs(syncs, sha))
           for sha, _ in commits):
        ci = actions.listing(lower, end, CI_WORKFLOW, ("event=push",))
    if actions.clock() >= end:  # a last page that returned after the budget still makes the metric late
        raise Unknown("gh api timed out")
    rows, findings, warnings = [], [], []
    for sha, committed in commits:
        state, reason, url = backlog_decision(sha, committed, syncs, ci, start)
        rows.append({"sha": sha, "state": state, "reason": reason, "url": url,
                     "dispatch": DISPATCH.format(sha=sha) if state == UNSYNCED else None})
        if state == UNSYNCED:
            findings.append(f"{sha}: {shown(reason)}; last run {shown(url or 'none')}; {DISPATCH.format(sha=sha)}")
        elif state == PENDING:
            warnings.append(f"{sha}: {shown(reason)}")
    numbers |= {state: sum(row["state"] == state for row in rows) for state in (SYNCED, PENDING, UNSYNCED)}
    reason = (f"{numbers[UNSYNCED]} unsynced, {numbers[PENDING]} pending, {numbers[SYNCED]} synced of {len(rows)} main "
              f"commit(s) after {SYNC_BACKLOG_SINCE[:12]}; {skipped}")
    result = metric(FAIL if findings else PASS, reason, numbers=numbers, commits=rows)
    result["findings"], result["warnings"] = findings, warnings
    return result


def backlog_lines(entry: dict) -> list[str]:
    """One watcher line per commit in scope: `synced|pending|unsynced <sha> [reason, last run URL, dispatch]`."""
    lines = []
    for row in entry["commits"]:
        if row["state"] == SYNCED:
            lines.append(f"{SYNCED} {row['sha']}")
        elif row["state"] == PENDING:
            lines.append(f"{PENDING} {row['sha']} {shown(row['reason'])}")
        else:
            lines.append(f"{UNSYNCED} {row['sha']} {shown(row['reason'])}; last run {shown(row['url'] or 'none')}; "
                         f"{row['dispatch']}")
    return lines


def drift_metric(root: Path, tiden: Tiden, target: str, main_ref: str, fetch: bool, deadline: float,
                 clock, sleep) -> dict:
    """The watermark W covers TARGET: W equals TARGET or descends from it on main. W behind at the deadline fails."""
    while True:
        status = tiden.call("drift", "status")
        watermark = status.get("watermarkSha")
        if status.get("repository") != REPOSITORY or not isinstance(watermark, str) or not SHA.fullmatch(watermark):
            raise Unknown("tiden drift status names no watermark commit of this repository")
        if not present(root, watermark) and fetch:
            git(root, "fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main")
        if not present(root, watermark) or not ancestor(root, watermark, main_ref):
            raise Unknown(f"the watermark {watermark} is not on main's history")
        numbers = {"watermark": watermark, "target": target}
        if ancestor(root, target, watermark):
            result = metric(PASS, f"the watermark {watermark} covers TARGET", numbers=numbers)
            break
        if not ancestor(root, watermark, target):
            raise Unknown(f"the watermark {watermark} and TARGET have diverged")
        if clock() >= deadline:
            behind = count(root, f"{watermark}..{target}")
            result = metric(FAIL, f"the watermark {watermark} is {behind} commit(s) behind TARGET at the deadline",
                            numbers=numbers | {"behind": behind})
            result["findings"].append(f"watermark {watermark} does not cover TARGET {target}; the Tiden drift run "
                                      "names the files to anchor")
            break
        sleep(POLL)
    head = commit(root, main_ref)
    if head != target and ancestor(root, target, head):
        # Merges after TARGET are shown for context only; they never decide this report.
        after = count(root, f"{target}..{head}")
        result["numbers"]["after_target"] = {"head": head, "commits": after,
                                             "covered": ancestor(root, head, result["numbers"]["watermark"])}
    return result


def anchors_metric(specs: list[dict], paths: set[str]) -> dict:
    stale, stale_paths, historical, anchors = [], set(), [], 0
    for spec in specs:
        anchored = {source.get("repoPath") for source in spec.get("sources", []) if source.get("sourceType") == "repo_file"
                    and (source.get("repository") or "") in ANCHOR_REPOSITORIES}
        for source in spec.get("sources", []):
            repository = source.get("repository")
            if repository is not None and not isinstance(repository, str):
                raise Unknown("tiden spec list has a malformed source repository")
            if source.get("sourceType") != "repo_file" or (repository or "") not in ANCHOR_REPOSITORIES:
                continue
            if not isinstance(source.get("repoPath"), str) or not source["repoPath"]:
                raise Unknown("tiden spec list has a repo_file source without a path")
            anchors += 1
            metadata = source.get("metadata")
            moved = MOVED_PREFIX + source["repoPath"]
            if source["repoPath"] not in paths and repository == FORMER_REPOSITORY and isinstance(metadata, dict) \
                    and isinstance(metadata.get("historical_source_id"), str) and metadata["historical_source_id"] \
                    and moved in anchored and moved in paths:
                historical.append(f"ANYM-{spec.get('seqNum')} source {source.get('id')}: {shown(source['repoPath'])}")
            elif source["repoPath"] not in paths:
                stale.append(f"ANYM-{spec.get('seqNum')} source {source.get('id')}: {shown(source['repoPath'])}")
                stale_paths.add(source["repoPath"])
    result = metric(FAIL if stale else PASS, f"{len(stale)} of {anchors} repo_file anchors name a path not tracked "
                    f"at TARGET; {len(historical)} historical anchors have their moved path anchored and tracked",
                    numbers={"anchors": anchors, "stale": len(stale), "stale_paths": len(stale_paths),
                             "historical": len(historical)})
    # Findings are escaped and shortened for display; the JSON keeps every stale path in full.
    result["findings"], result["paths"], result["historical"] = stale, sorted(stale_paths), historical
    return result


def prose(content: str) -> list[tuple[str, str]]:
    """(original line, line with fenced code and inline code spans blanked), so text that documents a marker is not one."""
    lines, kept, fence = content.splitlines(), [], None
    for line in lines:
        mark = FENCE.match(line)
        if fence is None:
            fence = mark.group(1) if mark else None
        elif mark and mark.group(1)[0] == fence[0] and len(mark.group(1)) >= len(fence) and not line[mark.end():].strip():
            fence = None
            kept.append("")
            continue
        kept.append(line if fence is None else "")
    blanked = CODE_SPAN.sub(lambda found: re.sub(r"[^\n]", " ", found.group(0)), "\n".join(kept))
    return list(zip(lines, blanked.split("\n")))


def done_metric(specs: list[dict]) -> dict:
    marked, phrased = [], []
    for spec in specs:
        if spec.get("status") != "Done":
            continue
        content = spec.get("content")
        if not isinstance(content, str):
            # A Done card whose text was not read cannot count as one without an open check.
            raise Unknown(f"tiden spec list has no readable text for Done spec ANYM-{spec.get('seqNum')}")
        label = f"ANYM-{spec.get('seqNum')} {shown(spec.get('title') or '', 120)}"
        text = prose(content)
        lines = [line for line, plain in text if MARKER.search(plain)]
        if lines:
            marked.extend(f"{label}: {shown(line.strip(), 200)}" for line in lines)
        elif any(PHRASE.search(plain) for _, plain in text):
            phrased.append(f"{label}: mentions an open check without a marker")
    done = sum(spec.get("status") == "Done" for spec in specs)
    numbers = {"done": done, "marked": len(marked), "unmarked_phrases": len(phrased), "audit": DONE_AUDIT}
    if DONE_AUDIT is None:
        result = metric(UNKNOWN, "the initial Done audit is not recorded yet (DONE_AUDIT)", numbers=numbers)
    else:
        result = metric(FAIL if marked else PASS, f"{len(marked)} open check marker(s) on {done} Done spec(s)",
                        numbers=numbers)
    result["findings"], result["warnings"] = marked, phrased
    return result


def no_spec_list(root: Path, target: str) -> tuple[dict[str, dict], list[str]]:
    """The reviewed no-spec list at TARGET: valid entries by case ID, and an error for each defect."""
    completed = run_git(root, "show", f"{target}:{NO_SPEC_LIST}")
    if completed.returncode:
        return {}, [f"{NO_SPEC_LIST} is missing at TARGET"]
    return parse_no_spec_list(completed.stdout)


def parse_no_spec_list(text: bytes) -> tuple[dict[str, dict], list[str]]:
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}, [f"{NO_SPEC_LIST} is not valid JSON"]
    if not isinstance(document, dict) or document.get("format") != 1 or not isinstance(document.get("cases"), list):
        return {}, [f"{NO_SPEC_LIST} needs format 1 and a cases list"]
    entries, errors = {}, []
    for index, entry in enumerate(document["cases"]):
        if (not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS
                or not all(isinstance(entry[field], str) and entry[field].strip() for field in ENTRY_FIELDS)):
            errors.append(f"{NO_SPEC_LIST} entry {index} needs exactly caseId, externalId, category and reason")
        elif entry["category"] not in CATEGORIES:
            errors.append(f"{NO_SPEC_LIST} entry {index} has the unknown category {shown(entry['category'])}")
        elif entry["caseId"] in entries:
            errors.append(f"{NO_SPEC_LIST} entry {index} repeats case {shown(entry['caseId'])}")
        else:
            entries[entry["caseId"]] = entry
    return entries, errors


def evidence(tiden: Tiden, main: str, target: str, deadline: float, clock, sleep) -> dict:
    """The newest completed Tiden main run of TARGET in CI's environment, once its live-doc sync succeeded."""
    while True:
        runs = [run for run in tiden.runs() if run.get("branchId") == main and run.get("environmentSlug") == ENVIRONMENT]
        if any(type(run.get("seqNum")) is not int for run in runs):
            raise Unknown("tiden run list has a run without a sequence number")
        done = [run for run in runs if run.get("buildSha") == target and run.get("status") in ("passed", "failed")]
        if done:
            run = max(done, key=lambda run: run["seqNum"])
            if run.get("liveDocStatus") == "succeeded":
                return run
            if run.get("liveDocStatus") in ("failed", "skipped"):
                raise Unknown(f"Tiden run {run['seqNum']} of TARGET has live-doc sync {run['liveDocStatus']}",
                              summarize(run))
        if clock() >= deadline:
            older = [run for run in runs if run.get("status") in ("passed", "failed")]
            raise Unknown("no completed Tiden main run of TARGET with a succeeded live-doc sync before the deadline",
                          summarize(max(older, key=lambda run: run["seqNum"])) if older else None)
        sleep(POLL)


def summarize(run: dict) -> dict:
    return {key: run.get(key) for key in ("seqNum", "buildSha", "environmentSlug", "status", "liveDocStatus",
                                          "completedAt", "description")}


def unlinked_metric(cases: list[dict], results: list[dict], seq: int, listed: dict[str, dict],
                    list_errors: list[str]) -> dict:
    by_id = {case["id"]: case for case in cases}
    active = set()
    for result in results:
        if result.get("testId") not in by_id:
            raise Unknown(f"a result of Tiden run {seq} names a case missing from the main inventory")
        active.add(result["testId"])
    table: dict[str, dict] = {name: {"cases": 0, "active": 0, "stale": 0, "unlinked": 0}
                              for name in (*SUITES, "unassigned")}
    findings, warnings, stale = list(list_errors), [], []
    for case in cases:
        row, linked = table[suite(case)], case.get("linkedRequirementCount", 0) > 0
        row["cases"] += 1
        label = f"{case['id']} {shown(case.get('filePath') or '-')} {shown(case.get('title') or '')}"
        if case["id"] not in active:
            row["stale"] += 1
            stale.append(label)
            continue
        row["active"] += 1
        if suite(case) == "unassigned":
            findings.append(f"unassigned path: {label}")
        if not linked:
            row["unlinked"] += 1
            entry = listed.get(case["id"])
            if entry is None:
                findings.append(f"unlinked and not in the no-spec list: {label}")
            elif entry["externalId"] != (case.get("externalId") or ""):
                findings.append(f"no-spec list entry {case['id']} names a different externalId")
    for case_id in listed.keys() - by_id.keys():
        warnings.append(f"no-spec list entry {shown(case_id)} is not a case on Tiden main")
    total: dict = {key: sum(row[key] for row in table.values()) for key in ("cases", "active", "stale", "unlinked")}
    for name, row in (*table.items(), ("overall", total)):
        row["share"] = round(100 * row["unlinked"] / row["active"], 2) if row["active"] else None
        if row["active"] and row["unlinked"] * 100 >= SHARE_PERCENT * row["active"]:
            findings.append(f"{name}: {row['unlinked']} of {row['active']} active cases unlinked ({row['share']}%), "
                            f"at or above {SHARE_PERCENT}%")
        elif name in SUITES and row["cases"] and not row["active"]:
            findings.append(f"{name}: not reporting ({row['cases']} case rows, none active in run {seq})")
    result = metric(FAIL if findings else PASS, f"{total['unlinked']} of {total['active']} active cases unlinked "
                    f"in Tiden run {seq}; {len(findings)} finding(s)",
                    numbers={"suites": table, "overall": total, "listed": len(listed)})
    result["findings"], result["warnings"], result["stale"] = findings, warnings, stale
    return result


def report(root: Path, tiden: Tiden, target_ref: str, main_ref: str, fetch: bool, clock=time.monotonic,
           sleep=time.sleep, unconfigured: str | None = None, actions: Actions | None = None,
           backlog: Actions | None = None, now=lambda: datetime.now(timezone.utc)) -> dict:
    """The four metrics at TARGET; `unconfigured` names missing Tiden configuration, which makes every one unknown.
    With `actions` the unlinked-test count is read only after the sync run of TARGET succeeded (ANYM-438). With
    `backlog`, its own lookup, the sync backlog up to TARGET is the fifth metric (ANYM-503); it needs no Tiden
    configuration and is measured last, whatever the others found."""
    start, began = clock(), now()
    data = {"target": None, "observedAt": datetime.now(timezone.utc).isoformat(), "evidence": {},
            "sync": {"checked": actions is not None, "target": None, "run_id": None, "run_attempt": None, "url": None,
                     "conclusion": None, "completed_at": None, "inventory_read_at": None}, "metrics": {}}
    upstream = {key.lower(): os.environ[key] for key in ("UPSTREAM_RUN_URL", "UPSTREAM_CONCLUSION", "UPSTREAM_SHA")
                if os.environ.get(key)}
    if upstream:
        data["evidence"]["upstream"] = upstream
    names = ("drift", "unlinked_tests", "stale_anchors", "done_open_checks")
    try:
        target = data["target"] = commit(root, target_ref)
        if not ancestor(root, target, main_ref):
            raise Unknown("TARGET is not on main's history")
    except Unknown as error:
        data["metrics"] = {name: metric(UNKNOWN, str(error)) for name in names}
        if backlog is not None:
            data["metrics"]["sync_backlog"] = metric(UNKNOWN, str(error))
        return data

    def measure(name, compute):
        try:
            data["metrics"][name] = compute()
        except Unknown as error:
            data["metrics"][name] = metric(UNKNOWN, str(error))
            if error.evidence:
                data["evidence"]["older_run"] = error.evidence
        except Exception as error:  # noqa: BLE001 - any other defect leaves this metric unknown; the report is still written
            data["metrics"][name] = metric(UNKNOWN, f"unexpected {type(error).__name__} while measuring")

    def finish() -> dict:
        data["metrics"] = {name: data["metrics"][name] for name in names}
        if backlog is not None:
            measure("sync_backlog", lambda: backlog_metric(root, backlog, target, began))
        data["observedAt"] = datetime.now(timezone.utc).isoformat()
        return data

    if unconfigured:
        data["metrics"] = {name: metric(UNKNOWN, unconfigured) for name in names}
        return finish()
    measure("drift", lambda: drift_metric(root, tiden, target, main_ref, fetch, start + DRIFT_WAIT, clock, sleep))
    try:
        specs: list[dict] | Unknown = tiden.specs()
    except Unknown as error:
        specs = error  # both spec metrics are unknown for the same reason
    except Exception as error:  # noqa: BLE001 - as in measure
        specs = Unknown(f"unexpected {type(error).__name__} while reading the spec list")

    def with_specs(compute):
        if isinstance(specs, Unknown):
            raise specs
        return compute(specs)

    measure("stale_anchors", lambda: with_specs(lambda specs: anchors_metric(specs, tracked(root, target))))
    measure("done_open_checks", lambda: with_specs(done_metric))

    def settled_cases() -> list[dict]:
        """The case inventory, kept only when the sync runs of TARGET were finished before it and unchanged after it."""
        committed = moment(git(root, "show", "-s", "--format=%cI", target).decode("ascii", "replace").strip())
        if committed is None:
            raise Unknown("git show returned no commit date of TARGET")
        since = committed - SYNC_SLACK
        while True:
            kept = await_sync(actions, target, since, start + TESTS_WAIT, clock, sleep)
            sync = decisive(kept)
            data["sync"] |= {"target": target, "run_id": sync["id"], "run_attempt": sync["run_attempt"],
                             "url": sync["html_url"], "conclusion": sync["conclusion"],
                             "completed_at": sync["updated_at"], "inventory_read_at": None}
            if sync["conclusion"] != "success":
                raise Unknown(f"Tiden sync run {shown(sync['html_url'])} of TARGET concluded "
                              f"{shown(sync['conclusion'])}; the links of TARGET may be missing")
            data["sync"]["inventory_read_at"] = datetime.now(timezone.utc).isoformat()
            cases = tiden.cases()
            if fingerprint(sync_runs(actions.runs(since, start + TESTS_WAIT - clock()), target)) == fingerprint(kept):
                return cases
            # That inventory is discarded, and so is the sync record that vouched for it.
            data["sync"] |= {"target": None, "run_id": None, "run_attempt": None, "url": None, "conclusion": None,
                             "completed_at": None, "inventory_read_at": None}
            if clock() >= start + TESTS_WAIT:
                raise Unknown("a Tiden sync run of TARGET changed while the case inventory was read")
            sleep(min(POLL, start + TESTS_WAIT - clock()))

    def unlinked():
        listed, list_errors = no_spec_list(root, target)
        main = tiden.main_branch()
        run = evidence(tiden, main, target, start + TESTS_WAIT, clock, sleep)
        data["evidence"]["run"] = summarize(run)
        cases = settled_cases() if actions is not None else tiden.cases()
        return unlinked_metric(cases, tiden.results(run["seqNum"]), run["seqNum"], listed, list_errors)

    measure("unlinked_tests", unlinked)
    return finish()


def outcome(data: dict) -> int:
    states = {entry["state"] for entry in data["metrics"].values()}
    return 1 if FAIL in states else 2 if UNKNOWN in states else 0


def render(data: dict, code: int) -> list[str]:
    lines = [f"## Tiden health: {('PASS', 'FAIL', 'UNKNOWN')[code]}", "",
             f"TARGET `{data['target']}` (main when the run started); observed {data['observedAt']}."]
    upstream, run = data["evidence"].get("upstream"), data["evidence"].get("run")
    if upstream:
        lines.append(f"Upstream CI: {upstream.get('upstream_conclusion', '?')} for "
                     f"`{upstream.get('upstream_sha', '?')}`, {upstream.get('upstream_run_url', '')}")
    if run:
        lines.append(f"Test evidence: Tiden run {run['seqNum']} on main, {run['environmentSlug']}, build "
                     f"`{run['buildSha']}`, {run['status']}, live-doc {run['liveDocStatus']}, completed "
                     f"{run['completedAt']}.")
    sync = data["sync"]
    if sync["run_id"] is not None:
        lines.append(f"Tiden sync of TARGET: run {sync['run_id']} attempt {sync['run_attempt']}, "
                     f"{shown(sync['conclusion'])}, completed {sync['completed_at']}, {shown(sync['url'])}; inventory "
                     f"read {sync['inventory_read_at'] or 'not started'}.")
    elif sync["checked"]:
        lines.append("Tiden sync of TARGET: no finished run read, so the unlinked-test count is unknown.")
    else:
        lines.append("Tiden sync of TARGET: not checked (no --sync-repository).")
    older = data["evidence"].get("older_run")
    if older:
        lines.append(f"Not evidence for TARGET: newest completed main run {older['seqNum']} is for its own SHA "
                     f"`{older['buildSha']}` ({older['status']}, live-doc {older['liveDocStatus']}).")
    if "sync_backlog" not in data["metrics"]:
        lines.append("Sync backlog of earlier main commits: not checked (no --sync-repository).")
    lines += ["", "| Metric | State | Reason |", "| --- | --- | --- |"]
    lines += [f"| {name} | {entry['state']} | {entry['reason']} |" for name, entry in data["metrics"].items()]
    drift = data["metrics"]["drift"]["numbers"].get("after_target")
    if drift:
        lines += ["", f"After TARGET (context only, never decides the result): {drift['commits']} commit(s) up to "
                  f"`{drift['head']}`, covered by the watermark: {str(drift['covered']).lower()}."]
    unlinked = data["metrics"]["unlinked_tests"]["numbers"]
    if "suites" in unlinked:
        lines += ["", "| Suite | Cases | Active | Stale | Unlinked | Share % |", "| --- | --- | --- | --- | --- | --- |"]
        for name, row in (*unlinked["suites"].items(), ("overall", unlinked["overall"])):
            share = "-" if row["share"] is None else row["share"]
            lines.append(f"| {name} | {row['cases']} | {row['active']} | {row['stale']} | {row['unlinked']} | {share} |")
    for name, entry in data["metrics"].items():
        if entry["findings"]:
            lines += ["", f"### {name}: {len(entry['findings'])} finding(s)", *(f"- {item}" for item in entry["findings"])]
        if entry["warnings"]:
            lines += ["", f"### {name}: {len(entry['warnings'])} warning(s)", *(f"- {item}" for item in entry["warnings"])]
        if entry.get("stale"):
            lines += ["", f"{name}: {len(entry['stale'])} stale case row(s) without a result in the evidence run are "
                      "counted, listed in tiden-health.json and never deleted."]
        if entry.get("historical"):
            lines += ["", f"{name}: {len(entry['historical'])} historical anchor(s) of the former repository, whose "
                      "moved path is anchored and tracked, are provenance: listed in tiden-health.json, never stale."]
    return lines


def backlog_only(root: Path, main_ref: str, repository: str | None, gh: str,
                 now=lambda: datetime.now(timezone.utc)) -> int:
    """Watcher mode: exit 0 when no commit in scope is unsynced, 1 when any is, 2 when the metric is unknown (the
    reason on stderr)."""
    began = now()
    try:
        if not repository:
            raise Unknown("--sync-backlog-only needs --sync-repository")
        git(root, "fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main")
        entry = backlog_metric(root, Actions(repository, gh), commit(root, main_ref), began)
    except Unknown as error:
        print(f"unknown: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - any other defect is unknown, never a pass
        print(f"unknown: unexpected {type(error).__name__} while measuring", file=sys.stderr)
        return 2
    print("\n".join(backlog_lines(entry)))
    return 1 if entry["state"] == FAIL else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Tiden health report for one main commit (ANYM-360).")
    parser.add_argument("--product-id", default=os.environ.get("TIDEN_PRODUCT_ID"))
    parser.add_argument("--workspace-id", default=os.environ.get("TIDEN_WORKSPACE_ID"))
    parser.add_argument("--target", default="HEAD", help="the main commit to measure (default: HEAD)")
    parser.add_argument("--main-ref", default="refs/remotes/origin/main")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--cli", default="tiden")
    parser.add_argument("--fetch", action="store_true", help="fetch main when the watermark is newer than the clone")
    parser.add_argument("--sync-repository", help="owner/name whose `Tiden sync` runs must finish before the "
                        "unlinked tests are read (the workflow passes it; GH_TOKEN needs actions: read)")
    parser.add_argument("--gh", default="gh")
    parser.add_argument("--output", type=Path, help="write the full report as JSON")
    parser.add_argument("--require-token", action="store_true",
                        help="treat an unset TIDEN_API_TOKEN as missing configuration (the workflow passes it)")
    parser.add_argument("--sync-backlog-only", action="store_true",
                        help="fetch main and list each main commit in scope as synced, pending or unsynced; needs "
                        "--sync-repository, no Tiden token (the orchestrator's watcher, ANYM-503)")
    args = parser.parse_args()
    if args.sync_backlog_only:
        return backlog_only(args.root.resolve(), args.main_ref, args.sync_repository, args.gh)
    # Missing configuration still writes a full report, every metric unknown, never a green run without one.
    unconfigured = None
    if not args.product_id or not args.workspace_id:
        unconfigured = "Tiden is not configured: no product or workspace ID (TIDEN-CI.md)"
    elif args.require_token and not os.environ.get("TIDEN_API_TOKEN"):
        unconfigured = "Tiden is not configured: TIDEN_API_TOKEN is not set (TIDEN-CI.md)"
    data = report(args.root.resolve(), Tiden(args.cli, args.product_id or "", args.workspace_id or ""), args.target,
                  args.main_ref, args.fetch, unconfigured=unconfigured,
                  actions=None if args.sync_repository is None else Actions(args.sync_repository, args.gh),
                  backlog=None if args.sync_repository is None else Actions(args.sync_repository, args.gh))
    code = outcome(data)
    lines = render(data, code)
    print("\n".join(lines))
    for entry in data["metrics"].values():
        for item in entry.get("stale", []):
            print(f"stale case: {item}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
    if args.output:
        args.output.write_text(json.dumps(data | {"exit": code}, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8", errors="backslashreplace")
    return code


if __name__ == "__main__":
    sys.exit(main())
