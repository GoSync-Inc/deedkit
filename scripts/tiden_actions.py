"""Read-only, complete GitHub Actions listings shared by Tiden health and drift.

A capped listing is retried in daily windows under the original deadline. Inclusive boundaries are
read twice and deduplicated; a single saturated day remains unknown, never evidence of completeness.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import subprocess
import time

REPOSITORY_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SYNC_WORKFLOW, SYNC_PAGE_SIZE, SYNC_MAX_PAGES = "tiden-sync.yml", 100, 10
SYNC_READS = 3  # reads of a changing run list, the first included
SYNC_SLACK = timedelta(days=1)  # clock skew: a run processing TARGET is created after TARGET exists
SYNC_WINDOW, LISTING_CAP = timedelta(days=1), 1000
TIMEOUT = 120


class Unknown(Exception):
    """An input cannot be proven complete or current; the metrics that need it are unknown."""

    def __init__(self, reason: str, evidence: dict | None = None):
        super().__init__(reason)
        self.evidence = evidence


class _ListingCap(Unknown):
    """A valid listing reached GitHub's cap; smaller windows may prove it complete."""


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

    def __init__(self, repository: str, gh: str = "gh", clock=time.monotonic,
                 now=lambda: datetime.now(timezone.utc)):
        self.repository, self.gh, self.clock = repository, gh, clock
        self.now = now

    def page(self, number: int, created: str, end: float, workflow: str = SYNC_WORKFLOW,
             filters: tuple[str, ...] = ()) -> dict:
        if not REPOSITORY_NAME.fullmatch(self.repository) or any(not part.strip(".") for part in self.repository.split("/")):
            raise Unknown("the sync lookup repository is not owner/name")
        endpoint = f"repos/{self.repository}/actions/workflows/{workflow}/runs"
        command = [self.gh, "api", "--method", "GET", endpoint, "-f", "branch=main",
                   *(part for value in filters for part in ("-f", value)), "-f", f"created={created}",
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
        # TARGET's existing lookup keeps its own scope and budget, separate from the backlog.
        return self._listing(since, self.clock() + (TIMEOUT if within is None else min(TIMEOUT, within)))

    def listing(self, since: datetime, end: float, workflow: str = SYNC_WORKFLOW,
                filters: tuple[str, ...] = ()) -> list[dict]:
        """Complete backlog listing; only a cap triggers smaller windows, never malformed or failed input."""
        try:
            rows = self._listing(since, end, workflow, filters)
            if len(rows) < LISTING_CAP:
                return rows
        except _ListingCap:
            pass
        return self.windows(since, end, workflow, filters)

    def windows(self, since: datetime, end: float, workflow: str = SYNC_WORKFLOW,
                filters: tuple[str, ...] = (), *, now: datetime | None = None) -> list[dict]:
        """Daily inclusive windows under END; a saturated day is unknown and boundaries are deduplicated."""
        rows: dict[int, dict] = {}
        start, now = since.astimezone(timezone.utc), self.now() if now is None else now
        while start <= now:
            stop = start + SYNC_WINDOW
            created = f"{start:%Y-%m-%dT%H:%M:%SZ}..{stop:%Y-%m-%dT%H:%M:%SZ}"
            for _ in range(SYNC_READS):
                try:
                    window = self.read(created, end, workflow, filters)
                except _ListingCap:
                    raise Unknown(f"the runs created {created} reach the listing cap of {LISTING_CAP}") from None
                if window is not None:
                    break
            else:
                raise Unknown("the run list changed on every read")
            if len(window) >= LISTING_CAP:
                raise Unknown(f"the runs created {created} reach the listing cap of {LISTING_CAP}")
            rows |= {row["id"]: row for row in window}
            start = stop
        if self.clock() >= end:
            raise Unknown("gh api timed out")
        return list(rows.values())

    def _listing(self, since: datetime, end: float, workflow: str = SYNC_WORKFLOW,
                 filters: tuple[str, ...] = ()) -> list[dict]:
        """The runs of WORKFLOW on main created at or after SINCE, every page before END (monotonic). The pages list
        the newest run first, so a run created or deleted while they are read shifts the later ones (run 40669350189):
        a row repeated from an earlier page or a changed `total_count` reads every page again, SYNC_READS reads in
        all within the same END."""
        created = ">=" + since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for _ in range(SYNC_READS):
            rows = self.read(created, end, workflow, filters)
            if rows is not None:
                return rows
        raise Unknown("the run list changed on every read")

    def read(self, created: str, end: float, workflow: str, filters: tuple[str, ...]) -> list[dict] | None:
        """One read of every page; None when the list changed between its pages."""
        rows: list[dict] = []
        first: int | None = None
        for number in range(1, SYNC_MAX_PAGES + 1):
            page = self.page(number, created, end, workflow, filters)
            batch, total = page.get("workflow_runs") if isinstance(page, dict) else None, \
                page.get("total_count") if isinstance(page, dict) else None
            if not isinstance(batch, list) or type(total) is not int or total < 0:
                raise Unknown("gh api returned no run page")
            if not all(valid_run(row) for row in batch) or len({row["id"] for row in batch}) != len(batch):
                raise Unknown("gh api returned a malformed or duplicate run")
            first = total if first is None else first
            if total != first or {row["id"] for row in rows} & {row["id"] for row in batch}:
                return None
            rows += batch
            if len(rows) == total:
                return rows
            if len(rows) > total or len(batch) < SYNC_PAGE_SIZE:
                break
        if first is not None and first >= LISTING_CAP and len(rows) == LISTING_CAP:
            raise _ListingCap("gh api pagination is incomplete")
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

