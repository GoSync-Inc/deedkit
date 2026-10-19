#!/usr/bin/env python3
"""One read-only report of whether Tiden still matches the code at one main commit (ANYM-360, TIDEN-CI.md).

Four metrics, each `pass`, `fail` or `unknown` with its reason: drift (the watermark covers TARGET), unlinked tests
(per suite and overall, against the Tiden main run of TARGET), stale anchors (repo_file sources of untracked paths)
and Done specs with an open check marker. Exit 0 when all pass, 1 when any fails, 2 when none fails and any is
unknown. Every input must be provably complete; anything else makes the metrics that need it unknown, never green.
It writes nothing to Tiden, and to Git only a fetch of main when `--fetch` is given (the workflow does).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
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


def metric(state: str, reason: str, **values) -> dict:
    return {"state": state, "reason": reason, "numbers": {}, "findings": [], "warnings": [], **values}


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
           sleep=time.sleep, unconfigured: str | None = None) -> dict:
    """The four metrics at TARGET; `unconfigured` names missing Tiden configuration, which makes every one unknown."""
    start = clock()
    data = {"target": None, "observedAt": datetime.now(timezone.utc).isoformat(), "evidence": {}, "metrics": {}}
    upstream = {key.lower(): os.environ[key] for key in ("UPSTREAM_RUN_URL", "UPSTREAM_CONCLUSION", "UPSTREAM_SHA")
                if os.environ.get(key)}
    if upstream:
        data["evidence"]["upstream"] = upstream
    names = ("drift", "unlinked_tests", "stale_anchors", "done_open_checks")
    try:
        target = data["target"] = commit(root, target_ref)
        if not ancestor(root, target, main_ref):
            raise Unknown("TARGET is not on main's history")
        if unconfigured:
            raise Unknown(unconfigured)
    except Unknown as error:
        data["metrics"] = {name: metric(UNKNOWN, str(error)) for name in names}
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

    def unlinked():
        listed, list_errors = no_spec_list(root, target)
        main = tiden.main_branch()
        run = evidence(tiden, main, target, start + TESTS_WAIT, clock, sleep)
        data["evidence"]["run"] = summarize(run)
        cases = tiden.cases()
        return unlinked_metric(cases, tiden.results(run["seqNum"]), run["seqNum"], listed, list_errors)

    measure("unlinked_tests", unlinked)
    data["metrics"] = {name: data["metrics"][name] for name in names}
    data["observedAt"] = datetime.now(timezone.utc).isoformat()
    return data


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
    older = data["evidence"].get("older_run")
    if older:
        lines.append(f"Not evidence for TARGET: newest completed main run {older['seqNum']} is for its own SHA "
                     f"`{older['buildSha']}` ({older['status']}, live-doc {older['liveDocStatus']}).")
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Tiden health report for one main commit (ANYM-360).")
    parser.add_argument("--product-id", default=os.environ.get("TIDEN_PRODUCT_ID"))
    parser.add_argument("--workspace-id", default=os.environ.get("TIDEN_WORKSPACE_ID"))
    parser.add_argument("--target", default="HEAD", help="the main commit to measure (default: HEAD)")
    parser.add_argument("--main-ref", default="refs/remotes/origin/main")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--cli", default="tiden")
    parser.add_argument("--fetch", action="store_true", help="fetch main when the watermark is newer than the clone")
    parser.add_argument("--output", type=Path, help="write the full report as JSON")
    parser.add_argument("--require-token", action="store_true",
                        help="treat an unset TIDEN_API_TOKEN as missing configuration (the workflow passes it)")
    args = parser.parse_args()
    # Missing configuration still writes a full report, every metric unknown, never a green run without one.
    unconfigured = None
    if not args.product_id or not args.workspace_id:
        unconfigured = "Tiden is not configured: no product or workspace ID (TIDEN-CI.md)"
    elif args.require_token and not os.environ.get("TIDEN_API_TOKEN"):
        unconfigured = "Tiden is not configured: TIDEN_API_TOKEN is not set (TIDEN-CI.md)"
    data = report(args.root.resolve(), Tiden(args.cli, args.product_id or "", args.workspace_id or ""), args.target,
                  args.main_ref, args.fetch, unconfigured=unconfigured)
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
