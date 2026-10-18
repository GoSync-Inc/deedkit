"""Preserve real CI outcomes and explicitly associate evidence with a Tiden branch.

The preparation runner has no Tiden credentials. The publishing runner executes
only this tool and the pinned Tiden CLI, never tests or application code.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

CHECKS = ("build", "release", "python", "collector", "npm", "smoke", "distributions")
REPORTS = {"python": "python.xml", "collector": "collector.xml", "npm": "npm.xml"}
MODULE_REPORTS = {
    "load": ("load.xml", "python", ""),
    "gateway": ("gateway.xml", "python", "services/gateway"),
    "calendar": ("calendar.xml", "python", "services/calendar"),
    "calls": ("calls.xml", "python", "services/calls"),
    "calls_frontend": ("calls-frontend.xml", "node", "services/calls/frontend"),
    "backup_restore": ("backup-restore.xml", "python", ""),
}
_VERIFY_MODULES = tuple(name for name in MODULE_REPORTS if name != "load")
# The root Python suite runs in file-level shards (scripts/python_shards.py): verify runs shard 1, each
# `checks / python` matrix job one of the others, and assembly merges their reports into python.xml.
PYTHON_SHARDS = 4
SHARD_REPORTS = tuple(f"python-{index}.xml" for index in range(1, PYTHON_SHARDS + 1))
SHARD_JOBS = tuple(f"python-{index}" for index in range(2, PYTHON_SHARDS + 1))
# Each parallel test job uploads one `ci-evidence-<job>` fragment: the module checks it runs and the files it
# produces (verify's seven checks travel in its metadata.json, a shard's `python` outcome in its outcomes.json),
# besides the `source-sha` and `attempt` it ran.
FRAGMENTS = {
    "verify": (_VERIFY_MODULES, ("metadata.json", SHARD_REPORTS[0], *(name for check, name in REPORTS.items() if check != "python"),
                                 *(MODULE_REPORTS[name][0] for name in _VERIFY_MODULES))),
    "load": (("load",), ("outcomes.json", MODULE_REPORTS["load"][0], "load.json")),
    **{job: ((), ("outcomes.json", report)) for job, report in zip(SHARD_JOBS, SHARD_REPORTS[1:])},
}
CONTROL_FILES = {"source-sha", "attempt", "metadata.json", "outcomes.json"}
# Every file assembly writes besides `source-sha` and `metadata.json`.
ASSEMBLED_FILES = frozenset({REPORTS["python"], *(name for _, files in FRAGMENTS.values() for name in files
                                                  if name not in CONTROL_FILES and name not in SHARD_REPORTS)})
MAX_BYTES = 20 * 1024 * 1024
# Tiden branch names: lowercase letters, digits, slashes, underscores and hyphens.
TIDEN_BRANCH_NAME = re.compile(r"[a-z0-9]+(?:[/_-][a-z0-9]+)*")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# The intent loop puts exactly this line, naming the session's Tiden branch, in the pull request description.
TIDEN_INTENT_LINE = re.compile(
    rf"^[ \t]*Tiden intent branch:[ \t]*\[(intent/[a-z0-9]+(?:[/_-][a-z0-9]+)*)\]"
    rf"\(https://app\.tiden\.ai/({_UUID})/products/({_UUID})/branches/({_UUID})\)", re.MULTILINE)
MAX_INTENT_LINKS = 5
MERGE_QUEUE_SKIP = "merge queue run of a temporary group commit"
# Tiden creates an environment for any unknown slug, so only these two may receive a release record.
RELEASE_ENVIRONMENTS = ("github-actions-synthetic", "production")
RUN_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+(?:/attempts/[0-9]+)?")
RUN_ATTEMPT_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+/attempts/([0-9]+)")
RELEASED_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
# A reporting job completes its run within its 6-minute timeout.
RUN_WINDOW = timedelta(minutes=10)
# Tiden recomputes the main gate seconds after a completed run's live-doc sync.
GATE_SETTLE = timedelta(seconds=60)
MAX_RUN_PAGES = 10
# A main push run that reused the merge queue's verification of its commit (scripts/ci_reuse.py) publishes that
# run's unchanged evidence with this record of itself as publisher, and these check rows under its own run URL.
PUBLICATION = "publication.json"
PUBLISHED_CHECKS = "ci-checks-published.json"
PRODUCER_FIELDS = ("repository", "workflow", "run_id", "run_attempt", "url", "artifact_id", "artifact_digest", "sha")


def source_path(value: str, root: Path) -> str:
    path = Path(value)
    if path.is_absolute():
        path = path.relative_to(root)
    value = path.as_posix()
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"..", ".git"} for part in parts):
        raise ValueError("test source path is outside the checkout")
    return value


def normalize_junit(path: Path, root: Path, framework: str, source_prefix: str = "") -> tuple[bytes, Counter]:
    data = path.read_bytes()
    if len(data) > MAX_BYTES or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("unsafe or oversized JUnit report")
    document = ET.fromstring(data)
    if document.tag not in {"testsuites", "testsuite"}:
        raise ValueError("not a JUnit report")
    cases = list(document.iter("testcase"))
    if not cases or len(cases) > 10000:
        raise ValueError("JUnit report must contain bounded real executions")
    counts = Counter()
    for case in cases:
        if not case.get("name"):
            raise ValueError("test identity is missing")
        file = case.get("file", "")
        if not file and framework == "python":
            # pytest xunit2 omits file; xunit1 in CI preserves the actual path.
            candidate = case.get("classname", "").replace(".", "/") + ".py"
            if (root / candidate).is_file():
                file = candidate
        if not file:
            raise ValueError("test source path is missing")
        if source_prefix and not Path(file).is_absolute() and not file.startswith(source_prefix + "/"):
            file = source_prefix + "/" + file
        file = source_path(file, root)
        case.set("file", file)
        if source_prefix and framework == "python":
            case.set("classname", source_prefix.replace("/", ".") + "." + case.get("classname", ""))
        if framework != "python":
            # Node's built-in reporter uses classname='test' for every file.
            # File qualification prevents unrelated equal titles from colliding.
            case.set("classname", "node:" + file)
        status = "failed" if case.find("failure") is not None or case.find("error") is not None else (
            "skipped" if case.find("skipped") is not None else "passed")
        counts[status] += 1
    return ET.tostring(document, encoding="utf-8", xml_declaration=True), counts


def prepare(root: Path, directory: Path, metadata: dict) -> dict:
    if not re.fullmatch(r"[a-f0-9]{40}", metadata.get("sha", "")):
        raise ValueError("actual tested commit SHA is required")
    checks = dict(metadata["checks"])
    module_checks = metadata.get("module_checks")
    if module_checks is not None:
        if set(module_checks) != set(MODULE_REPORTS):
            raise ValueError("explicit outcomes for every platform module are required")
        checks.update(module_checks)
    check_names = CHECKS + (tuple(MODULE_REPORTS) if module_checks is not None else ())
    rows = []
    reports = []
    counts = Counter()
    for check in check_names:
        outcome = checks.get(check, "skipped")
        if outcome not in {"success", "failure", "skipped", "cancelled"}:
            raise ValueError("unknown CI step outcome")
        reason = ""
        if check in REPORTS or check in MODULE_REPORTS:
            try:
                filename, framework, prefix = MODULE_REPORTS.get(check, (REPORTS.get(check), check, ""))
                normalized, totals = normalize_junit(directory / filename, root, framework, prefix)
                filename = f"normalized-{check}.xml"
                (directory / filename).write_bytes(normalized)
                reports.append(filename)
                counts.update(totals)
                if totals["failed"] and outcome == "success":
                    outcome = "failure"
                if not totals["passed"] and not totals["failed"]:
                    outcome, reason = "skipped", "No tests executed; only skipped tests"
            except (OSError, ValueError, ET.ParseError):
                outcome, reason = "skipped", "Missing or invalid test report; verification incomplete"
        status = "passed" if outcome == "success" else "failed" if outcome == "failure" else "blocked"
        rows.append({
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, metadata["ci_url"] + "/" + check)),
            "title": f"CI verification: {check}", "signature": ("anymsg-ci::" if check in CHECKS else "deedkit-ci::") + check,
            "execution": {"status": status, "duration": 0},
            "suitePath": [{"title": "CI verifications"}],
            "fields": {"file_path": ".github/workflows/checks.yml"},
            "message": reason or f"GitHub Actions step outcome: {outcome}",
        })
    (directory / "ci-checks.json").write_text(json.dumps(rows, indent=2))
    manifest = {**metadata, "schema": 1, "reports": reports + ["ci-checks.json"],
                "test_counts": dict(counts), "check_counts": dict(Counter(r["execution"]["status"] for r in rows))}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def control_file(fragment: Path, name: str) -> str:
    path = fragment / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"evidence fragment file {name} is missing or not a regular file")
    return path.read_text().strip()


def fresh_fragment(fragment: Path, root: Path, job: str, result: str, attempt: str) -> bool:
    """Whether `fragment` holds this job's evidence to merge: False when it is absent, or when a job that did
    not succeed in this attempt left only an earlier attempt's fragment behind."""
    if not fragment.exists() and not fragment.is_symlink():
        return False
    if fragment.is_symlink() or not fragment.is_dir() or fragment.resolve().parent != root:
        raise ValueError(f"the {job} fragment is not a plain directory of the download")
    allowed = FRAGMENTS[job][1] + ("source-sha", "attempt")
    if any(path.name not in allowed or path.is_symlink() or not path.is_file() for path in fragment.iterdir()):
        raise ValueError(f"the {job} fragment holds a file its job does not produce")
    return result == "success" or control_file(fragment, "attempt") == attempt


def shard_suites(path: Path, root: Path) -> tuple[list[ET.Element], Counter]:
    """A shard's pytest report, validated on its own as `prepare` validates a report: its test suites, which
    must hold every one of its cases, and the counts of its passed, failed and skipped cases."""
    _, counts = normalize_junit(path, root, "python")
    document = ET.fromstring(path.read_bytes())
    suites = [document] if document.tag == "testsuite" else list(document)
    if any(suite.tag != "testsuite" for suite in suites) or sum(len(suite.findall("testcase")) for suite in suites) != sum(counts.values()):
        raise ValueError("a test case lies outside the report's test suites")
    return suites, counts


def shard_outcome(state: str, report: Path, root: Path, suites: list) -> str:
    """One shard's `python` outcome from its reported step outcome and its report, whose suites join `suites`.
    A shard passes only with a valid report of executed tests and no failed one; without such a report it
    failed, or its verification is incomplete."""
    try:
        found, counts = shard_suites(report, root)
    except (OSError, ValueError, ET.ParseError):
        return state if state in {"failure", "cancelled"} else "skipped"
    suites.extend(found)
    if state == "success" and counts["failed"]:
        return "failure"
    return "skipped" if state == "success" and not counts["passed"] and not counts["failed"] else state


def combined_outcome(outcomes: list[str]) -> str:
    """The `python` check over its shards: passed only when every shard passed."""
    if all(outcome == "success" for outcome in outcomes):
        return "success"
    return next((state for state in ("failure", "cancelled") if state in outcomes), "skipped")


def assemble(directory: Path, fragments: Path, ci_url: str, producers: dict, *, source_root: Path | None = None,
             commit: str | None = None) -> dict:
    """Merge the evidence fragments that the parallel test jobs uploaded into the directory `prepare` reads.

    `producers` maps each job of FRAGMENTS to its job result and the checkout SHA its job output captured. A
    fragment holds only its own files and checks and must name verify's tested SHA, which is the SHA its job
    captured. A job without a fresh fragment contributes no reports, and its checks take its job result
    (`skipped` when it succeeded), so it never reads as passed; such jobs are returned in `missing`. A stale
    verify fragment still supplies the run's routing metadata (SHA, Git and Tiden branch), never its outcomes.
    The `python` check passes only when every shard (verify's and each shard job's) passed with a fresh fragment
    and its own valid report of executed tests (`source_root`, the tested checkout, places their paths), and
    each shard job succeeded: a shard job that failed or was cancelled after its tests never reads as passed. A
    baseline of another source than `commit`, the commit receiving the checks, is the exception: every test
    job then fails by design on that difference, and its checks keep their step outcomes, as verify's always do.
    The valid shard reports merge into one python.xml, so test identities do not change.
    `ci_url` names this assembly's run attempt, which reruns with every producer; `producers` in the metadata
    keeps each job's own result, captured SHA and the attempt its merged fragment came from. `directory` must
    be new or empty, so no earlier assembly's report survives.
    """
    attempt = RUN_ATTEMPT_URL.fullmatch(ci_url or "")
    if not attempt or set(producers) != set(FRAGMENTS):
        raise ValueError("the assembling run's attempt URL and every producer's result are required")
    root = fragments.resolve()
    verify = fragments / "ci-evidence-verify"
    if not fresh_fragment(verify, root, "verify", "success", ""):
        raise ValueError("the verify fragment with the run metadata is missing")
    try:
        metadata = json.loads(control_file(verify, "metadata.json"))
        sha = metadata["sha"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("the verify fragment with the run metadata is missing") from None
    if not re.fullmatch(r"[a-f0-9]{40}", str(sha)) or set(metadata.get("checks") or {}) != set(CHECKS):
        raise ValueError("the verify fragment does not name its tested source SHA and its seven checks")
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("the evidence directory must be new or empty")
    checks, module_checks, provenance = dict(metadata["checks"]), {}, {}
    directory.mkdir(parents=True, exist_ok=True)
    present, missing, shards, suites = [], [], [], []
    source_root = source_root or Path.cwd()
    for job, (names, files) in FRAGMENTS.items():
        result, captured = producers[job]
        # A job's own captured checkout always has to agree with verify's, whatever its fragment.
        if captured and captured != sha:
            raise ValueError(f"the {job} job captured another source than verify")
        fragment = fragments / f"ci-evidence-{job}"
        provenance[job] = {"result": result, "sha": captured, "attempt": None}
        if not fresh_fragment(fragment, root, job, result, attempt.group(1)):
            missing.append(job)
            state = result if result in {"failure", "cancelled"} else "skipped"
            module_checks.update(dict.fromkeys(names, state))
            if job == "verify":
                checks = dict.fromkeys(CHECKS, state)
            if job == "verify" or job in SHARD_JOBS:
                shards.append(state)
            continue
        if control_file(fragment, "source-sha") != sha or captured != sha:
            raise ValueError(f"the {job} fragment tested another source than verify")
        provenance[job]["attempt"] = control_file(fragment, "attempt")
        if not provenance[job]["attempt"].isdigit():
            raise ValueError(f"the {job} fragment does not name its run attempt")
        try:
            outcomes = metadata if job == "verify" else json.loads(control_file(fragment, "outcomes.json"))
            reported = outcomes["checks"] if job in SHARD_JOBS else outcomes["module_checks"]
            if job in SHARD_JOBS and set(outcomes) != {"checks"}:
                reported = None
        except (ValueError, KeyError, TypeError):
            reported = None
        if not isinstance(reported, dict) or set(reported) != ({"python"} if job in SHARD_JOBS else set(names)):
            raise ValueError(f"the {job} fragment may report only its own checks")
        if job in SHARD_JOBS:
            state = shard_outcome(reported["python"], fragment / files[1], source_root, suites)
            if state == "success" and result != "success" and commit in {None, sha}:
                state = result if result in {"failure", "cancelled"} else "skipped"
            shards.append(state)
        else:
            module_checks.update(reported)
            if job == "verify":
                shards.append(shard_outcome(checks["python"], fragment / SHARD_REPORTS[0], source_root, suites))
        for path in fragment.iterdir():
            if path.name not in CONTROL_FILES and path.name not in SHARD_REPORTS:
                (directory / path.name).write_bytes(path.read_bytes())
        present.append(job)
    checks["python"] = combined_outcome(shards)
    if suites:
        merged = ET.Element("testsuites", {"name": "pytest tests"})
        merged.extend(suites)
        (directory / REPORTS["python"]).write_bytes(ET.tostring(merged, encoding="utf-8", xml_declaration=True))
    (directory / "source-sha").write_text(sha + "\n")
    metadata.update({"ci_url": ci_url, "checks": checks, "module_checks": module_checks, "fragments": present,
                     "producers": provenance})
    (directory / "metadata.json").write_text(json.dumps(metadata))
    return {**metadata, "missing": missing}


def published_manifest(directory: Path, manifest: dict) -> dict:
    """The manifest this run publishes: its own, or with a publication record the merge queue run's evidence.

    The job must run for a push to main, the record must name this job's run, attempt and commit from its
    environment, and its producer must be a ci.yml run attempt of this repository whose evidence, of that commit,
    lies beside it. Test executions, outcomes and identities stay the queue run's; the event,
    Git branch, CI URL and check row IDs become this push run's, and `tested_in` names the queue run attempt.
    """
    if not (directory / PUBLICATION).exists() and not (directory / PUBLICATION).is_symlink():
        return manifest
    repository, run_id, attempt, sha, event, ref = (os.environ.get(name, "") for name in (
        "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA", "GITHUB_EVENT_NAME", "GITHUB_REF"))
    ci_url = f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{attempt}"
    try:
        record = json.loads(control_file(directory, PUBLICATION))
        producer = record["producer"]
        producer_url = "https://github.com/{}/actions/runs/{}/attempts/{}".format(
            repository, producer["run_id"], producer["run_attempt"])
        valid = ((event, ref) == ("push", "refs/heads/main") and bool(RUN_ATTEMPT_URL.fullmatch(ci_url))
                 and re.fullmatch(r"[a-f0-9]{40}", sha) is not None
                 and {key: record.get(key) for key in ("schema", "event", "git_branch", "repository", "run_id",
                                                       "run_attempt", "sha", "ci_url")}
                 == {"schema": 1, "event": "push", "git_branch": "main", "repository": repository, "run_id": run_id,
                     "run_attempt": attempt, "sha": sha, "ci_url": ci_url}
                 and producer["sha"] == manifest.get("sha") == sha and merge_queue_run(manifest)
                 and set(producer) == set(PRODUCER_FIELDS) and producer["repository"] == repository
                 and producer["workflow"] == ".github/workflows/ci.yml"
                 and str(producer["run_id"]).isdigit() and str(producer["run_attempt"]).isdigit()
                 and str(producer["run_id"]) != run_id and str(producer["artifact_id"]).isdigit()
                 and re.fullmatch(r"sha256:[0-9a-f]{64}", producer["artifact_digest"]) is not None
                 and producer["url"] == producer_url == manifest.get("ci_url")
                 and RUN_ATTEMPT_URL.fullmatch(producer_url) is not None)
        rows = json.loads(control_file(directory, "ci-checks.json")) if valid else []
        published = [{**row, "id": str(uuid.uuid5(uuid.NAMESPACE_URL, ci_url + "/" + row["signature"].split("::", 1)[1])),
                      "message": f"{row['message']}; tested in {producer['url']}"} for row in rows]
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        valid = False
    if not valid:
        raise ValueError("the publication record does not name this main push run and its merge queue evidence")
    (directory / PUBLISHED_CHECKS).write_text(json.dumps(published, indent=2))
    reports = [name for name in manifest["reports"] if name != "ci-checks.json"] + [PUBLISHED_CHECKS]
    return {**manifest, "event": "push", "git_branch": "main", "tiden_branch": "", "pr_tiden_links": [],
            "ci_url": ci_url, "tested_in": producer["url"], "reports": reports}


def resolve_branch(manifest: dict, mapping: dict) -> str:
    branch = manifest.get("tiden_branch") or mapping.get(manifest["git_branch"])
    if not branch and manifest["git_branch"] == "main":
        branch = "main"
    if not isinstance(branch, str) or not branch:
        raise ValueError("No Tiden branch mapping; register this Git branch before publishing evidence")
    return branch


def route_branch(manifest: dict, mapping: dict) -> tuple[str, bool]:
    """Return the Tiden branch and whether Tiden itself routes the run.

    An explicit mapping always wins. An unmapped feature branch is reported under
    its own Git branch name: Tiden attributes the run to the intent branch whose
    session recorded that Git branch, otherwise to a branch of the same name.
    """
    try:
        return resolve_branch(manifest, mapping), False
    except ValueError:
        git_branch = manifest["git_branch"]
        if git_branch == "main" or not TIDEN_BRANCH_NAME.fullmatch(git_branch):
            raise ValueError("No Tiden branch mapping and the Git branch is not a valid Tiden branch name; "
                             "register this Git branch before publishing evidence") from None
        return git_branch, True


def pull_request_body(event_path: str | None) -> str:
    """The pull request description from the GitHub event file, or empty outside a pull request."""
    try:
        event = json.loads(Path(event_path).read_text()) if event_path else {}
    except (OSError, ValueError):
        return ""
    body = (event.get("pull_request") or {}).get("body") if isinstance(event, dict) else None
    return body if isinstance(body, str) else ""


def intent_links(text: str) -> list[dict]:
    """`Tiden intent branch:` lines of a pull request description, as names and IDs only."""
    links = []
    for name, workspace, product, branch in TIDEN_INTENT_LINE.findall(text or ""):
        link = {"name": name, "workspace": workspace, "product": product, "branch": branch}
        if link not in links:
            links.append(link)
    return links[:MAX_INTENT_LINKS]


def linked_intent_branch(links: list, branches: list, product: str, workspace: str) -> str | None:
    """The one open intent branch of this product that the pull request links to, if unambiguous.

    The link text must name the same branch its ID resolves to.
    """
    wanted = {(link.get("branch"), link.get("name")) for link in links
              if link.get("product") == product and link.get("workspace") == workspace}
    names = {b["name"] for b in branches if (b.get("id"), b.get("name")) in wanted and b.get("status") == "open"
             and isinstance(b.get("name"), str) and b["name"].startswith("intent/")}
    return names.pop() if len(names) == 1 else None


def merge_queue_run(manifest: dict) -> bool:
    """A GitHub merge queue run tests a temporary `gh-readonly-queue/` group commit."""
    git_branch = manifest.get("git_branch")
    return manifest.get("event") == "merge_group" or (
        isinstance(git_branch, str) and git_branch.startswith("gh-readonly-queue/"))


def routed_target(name: object, git_branch: str) -> bool:
    return isinstance(name, str) and (name == git_branch or name.startswith("intent/"))


def tiden(cli: str, arguments: list[str], product: str, directory: Path, label: str) -> tuple[int, str]:
    """Run one Tiden CLI command and keep its redacted JSON output as a receipt."""
    completed = subprocess.run([cli, *arguments, "--product-id", product, "--format", "json"],
                               capture_output=True, text=True, timeout=120)
    token = os.environ.get("TIDEN_API_TOKEN", "")
    output = completed.stdout.replace(token, "[redacted]") if token else completed.stdout
    (directory / f"tiden-{label}.json").write_text(output)
    return completed.returncode, output


def publish(directory: Path, manifest: dict, branch: str | None, product: str, cli: str = "tiden",
            routed: bool = False, workspace: str = "") -> dict:
    if manifest.get("schema") != 1 or not re.fullmatch(r"[a-f0-9]{40}", manifest.get("sha", "")):
        raise ValueError("invalid evidence manifest")

    def call(arguments: list[str], label: str, allow_verdict: bool = False) -> dict:
        returncode, output = tiden(cli, arguments, product, directory, label)
        if returncode and not allow_verdict:
            raise RuntimeError(f"Tiden {label} failed; evidence was not verified")
        value = json.loads(output)
        if returncode and value.get("status") != "failed":
            raise RuntimeError(f"Tiden {label} failed without a test verdict")
        return value

    def skip(reason: str, level: str) -> dict:
        # A merge queue group, or a pull request without its intent branch, has no Tiden home;
        # main publishes the merged code.
        (directory / "tiden-route.json").write_text(json.dumps({"branch": None, "source": "skipped",
                                                                "reason": reason}))
        print(f"::{level} title=Tiden evidence not published::{reason}; main CI publishes it after merge")
        return {"seqNum": None, "status": "skipped", "branchName": None, "branchId": None,
                "buildSha": manifest["sha"], "stats": {}, "liveDocStatus": None}

    if merge_queue_run(manifest):
        # Before any Tiden call and whatever target was chosen: a queue run never publishes.
        return skip(MERGE_QUEUE_SKIP, "notice")
    pull_request = manifest.get("event") == "pull_request"
    if routed and pull_request and not manifest.get("pr_tiden_links"):
        return skip("pull request description has no Tiden intent branch link", "notice")
    branches = call(["branch", "list"], "branches")["branches"]
    source = "routed" if routed else "explicit"
    if routed and manifest.get("pr_tiden_links"):
        # An explicit target already won in route_branch; otherwise prefer the linked intent branch.
        linked = linked_intent_branch(manifest["pr_tiden_links"], branches, product, workspace)
        if linked:
            branch, routed, source = linked, False, "pull-request-link"
    if routed and pull_request:
        return skip("the pull request's Tiden intent branch link is not usable: its text must name the "
                    "branch its ID resolves to, and that must be the one open intent branch of this "
                    "workspace and product", "warning")
    if branch is None:
        raise ValueError("No Tiden branch mapping, no usable pull request intent link, and the Git branch "
                         "is not a valid Tiden branch name; register this Git branch before publishing evidence")
    # A reusing main push names, in its retained receipt, the merge queue run attempt that ran the tests.
    tested_in = {"tested_in": manifest["tested_in"]} if manifest.get("tested_in") else {}
    (directory / "tiden-route.json").write_text(json.dumps({"branch": branch, "source": source, **tested_in}))
    expected = next((b["id"] for b in branches if b["name"] == branch), None)
    if not expected and routed:
        # Tiden skips live-doc sync for a branch name it does not know, so create it first.
        try:
            call(["branch", "create", "--name", branch,
                  "--description", "CI evidence for Git branch " + branch], "branch-create")
        except (RuntimeError, ValueError):
            pass  # A concurrent run may have created it; the listing below decides.
        branches = call(["branch", "list"], "branches-created")["branches"]
        expected = next((b["id"] for b in branches if b["name"] == branch), None)
    if not expected:
        raise ValueError("Tiden branch does not exist; refusing unassociated results")
    run = call(["run", "create", "--branch", branch, "--intent-session", "none",
                "--build-sha", manifest["sha"], "--environment", "github-actions-synthetic",
                "--title", f"GitHub CI {manifest['sha'][:12]}", "--description", manifest["ci_url"],
                "--client-meta", "git_branch=" + manifest["git_branch"],
                "--client-meta", "ci_url=" + manifest["ci_url"],
                *(["--client-meta", "tested_in=" + manifest["tested_in"]] if manifest.get("tested_in") else [])], "create")
    seq = str(run["seqNum"])
    completed = False
    try:
        named = routed_target(run["branchName"], branch) if routed else run["branchName"] == branch
        if not named or run["buildSha"] != manifest["sha"]:
            raise RuntimeError("Tiden rewrote the explicit source association")
        paths = []
        for filename in manifest["reports"]:
            if Path(filename).name != filename or (directory / filename).is_symlink():
                raise ValueError("invalid evidence file path")
            if not (directory / filename).is_file() or (directory / filename).stat().st_size > MAX_BYTES:
                raise ValueError("missing or oversized evidence")
            paths.append(str((directory / filename).resolve()))
        if not {"ci-checks.json", PUBLISHED_CHECKS} & set(manifest["reports"]):
            raise ValueError("CI step evidence is required")
        call(["run", "report", seq, *paths], "report")
        result = call(["run", "complete", seq], "complete", allow_verdict=True)
        completed = True
        for attempt in range(20):
            result = call(["run", "get", seq], "final")
            if result.get("liveDocStatus") in {"succeeded", "failed", "skipped"}:
                break
            time.sleep(3)
        if routed and result.get("liveDocStatus") == "succeeded" and result.get("branchId") != expected:
            # Tiden may attribute the run to an intent branch linked to this Git branch.
            final = call(["branch", "list"], "branches-final")["branches"]
            expected = next((b["id"] for b in final if b["id"] == result.get("branchId")
                             and routed_target(b["name"], branch)), None)
        if result.get("liveDocStatus") != "succeeded" or not expected or result.get("branchId") != expected:
            raise RuntimeError("Run recorded, but Tiden did not associate test evidence with the expected branch "
                               f"(live doc {result.get('liveDocStatus')}, branch {result.get('branchId')})")
        if result.get("status") not in {"passed", "failed"}:
            raise RuntimeError("Tiden run has no terminal test verdict")
        check_count = len(CHECKS) + (len(MODULE_REPORTS) if "module_checks" in manifest else 0)
        expected_attempts = sum(manifest["test_counts"].values()) + check_count
        if int(result["stats"]["attempts"]) != expected_attempts:
            raise RuntimeError("Tiden result count differs from the authentic CI evidence")
        expected_failure = (manifest["test_counts"].get("failed", 0)
                            or manifest["check_counts"].get("failed", 0)
                            or manifest["check_counts"].get("blocked", 0))
        if expected_failure and result["status"] != "failed":
            raise RuntimeError("Tiden verdict contradicts failed or incomplete CI evidence")
        return result
    finally:
        if not completed:
            call(["run", "abort", seq], "abort")


class StaleRelease(RuntimeError):
    """The latest completed main run is not this commit's, so a new release would capture another gate."""


class ForeignGate(RuntimeError):
    """The release exists, but the main gate it captured did not come from this commit's run."""


def when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def record_release(directory: Path, sha: str, environment: str, url: str, released_at: str, product: str,
                   cli: str = "tiden") -> dict:
    """Record release deedkit@<sha> and prove its captured gate came from this commit's passed main run.

    Tiden captures a release's gate from the current main gate when the release is first
    created; a repeated create updates its name, URL and time but keeps that first gate.
    The proof is stateless: the main run that completed last before the release's creation,
    at least GATE_SETTLE earlier, must be a passed run of this commit.
    """
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("an exact lowercase 40-character commit SHA is required")
    if environment not in RELEASE_ENVIRONMENTS:
        raise ValueError("unknown release environment; Tiden would create a duplicate")
    if not RUN_URL.fullmatch(url):
        raise ValueError("the release URL must be a GitHub Actions run")
    if not RELEASED_AT.fullmatch(released_at):
        raise ValueError("the release time must be RFC3339 UTC, YYYY-MM-DDTHH:MM:SSZ")
    if when(released_at) > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("the release time is in the future")

    def call(arguments: list[str], label: str) -> dict:
        returncode, output = tiden(cli, arguments, product, directory, label)
        if returncode:
            raise RuntimeError(f"Tiden {label} failed; the release was not verified")
        return json.loads(output)

    def latest_main_run(until: datetime, label: str) -> dict:
        """The CI main run that completed last at or before `until`, from a listing proven complete."""
        runs, token = [], ""
        for page in range(1, MAX_RUN_PAGES + 1):
            arguments = ["run", "list", "--branch", "main", "--environment", "github-actions-synthetic",
                         "--page-size", "50"]
            value = call(arguments + (["--page-token", token] if token else []), f"{label}-{page}")
            runs.extend(value["runs"])
            token = (value.get("pagination") or {}).get("nextPageToken") or ""
            done = [run for run in runs if run.get("status") in {"passed", "failed"} and run.get("completedAt")
                    and when(run["completedAt"]) <= until]
            latest = max(done, key=lambda run: when(run["completedAt"]), default={})
            # Runs are listed newest first and complete inside their reporting job, so a run
            # created before this window cannot have completed after the latest one.
            oldest = min((when(run["createdAt"]) for run in runs), default=until)
            if not token or (latest and oldest < when(latest["completedAt"]) - RUN_WINDOW):
                return latest
        raise RuntimeError("too many main runs to establish the latest completion; the release was not verified")

    ours = latest_main_run(datetime.now(timezone.utc), "release-runs")
    if ours.get("buildSha") != sha or ours.get("status") != "passed":
        raise StaleRelease("this commit's passed run is not the latest completed main run in Tiden")
    # Tiden recomputes the main gate only after the run's live-doc sync.
    wait = when(ours["completedAt"]) + GATE_SETTLE - datetime.now(timezone.utc)
    if wait > timedelta(0):
        time.sleep(wait.total_seconds())
    version = "deedkit@" + sha
    release = call(["release", "create", "--version", version, "--environment", environment,
                    "--name", "DeedKit " + sha[:12], "--url", url, "--released-at", released_at], "release")
    if (release.get("version"), release.get("environmentSlug"), release.get("url")) != (version, environment, url):
        raise RuntimeError("Tiden rewrote the release identity")
    # A repeated create keeps the original creation time, and with it the original gate.
    created = when(release["createdAt"])
    captured = latest_main_run(created, "release-runs-captured")
    own_gate = (captured.get("buildSha") == sha and captured.get("status") == "passed"
                and created - when(captured["completedAt"]) >= GATE_SETTLE)
    returncode, output = tiden(cli, ["gate", "check", "--release", release["id"]], product, directory, "release-gate")
    try:
        gate = json.loads(output)
    except json.JSONDecodeError:
        gate = {}
    if returncode and not gate:
        print("::warning title=Tiden release gate::no gate was captured for " + version)
        own_gate = None
    receipt = {"releaseId": release["id"], "version": version, "environment": environment, "url": url,
               "releasedAt": released_at, "createdAt": release["createdAt"], "mainRun": ours["id"],
               "gateRun": captured.get("id"), "gateFromThisCommit": own_gate,
               "gate": gate.get("status"), "gateComputedAt": gate.get("computedAt")}
    (directory / "tiden-release-receipt.json").write_text(json.dumps(receipt, indent=2))
    if own_gate is False:
        raise ForeignGate(f"release {version} captured the main gate after run {captured.get('id')} of "
                          f"{captured.get('buildSha')}; do not use its gate for readiness")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["assemble", "prepare", "publish", "release"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--fragments", type=Path)
    parser.add_argument("--producer", action="append", default=[], help="<job>:<job result>:<captured source SHA>")
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--environment", choices=RELEASE_ENVIRONMENTS)
    parser.add_argument("--sha")
    parser.add_argument("--url")
    parser.add_argument("--released-at")
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.command == "release":
        return release(args, directory)
    if args.command == "assemble":
        ci_url = "https://github.com/{}/actions/runs/{}/attempts/{}".format(*(
            os.environ.get(name, "") for name in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")))
        producers = {job: (result, sha) for job, result, sha in (value.split(":", 2) for value in args.producer)}
        merged = assemble(directory, args.fragments, ci_url, producers, source_root=args.source_root,
                          commit=os.environ.get("GITHUB_SHA") or None)
        print(json.dumps({"fragments": merged["fragments"], "missing": merged["missing"]}))
        # The evidence is written either way, so a run without every fresh fragment is recorded and still fails.
        return 1 if merged["missing"] else 0
    if args.command == "prepare":
        metadata = json.loads((directory / "metadata.json").read_text())
        metadata["pr_tiden_links"] = intent_links(pull_request_body(os.environ.get("GITHUB_EVENT_PATH")))
        metadata["event"] = os.environ.get("GITHUB_EVENT_NAME", "")
        result = prepare(args.source_root.resolve(), directory, metadata)
        print(json.dumps({"test_counts": result["test_counts"], "check_counts": result["check_counts"]}))
        return 0
    manifest = published_manifest(directory, json.loads((directory / "manifest.json").read_text()))
    if merge_queue_run(manifest):
        # publish() skips a queue run before any Tiden call: no credentials, map or route needed.
        result = publish(directory, manifest, None, "")
    else:
        mapping = json.loads(os.environ.get("TIDEN_BRANCH_MAP") or "{}")  # a malformed map fails loudly
        try:
            branch, routed = route_branch(manifest, mapping)
        except ValueError:
            if not manifest.get("pr_tiden_links") and manifest.get("event") != "pull_request":
                raise
            branch, routed = None, True  # publish() resolves the pull request's intent link or skips
        result = publish(directory, manifest, branch, os.environ["TIDEN_PRODUCT_ID"], routed=routed,
                         workspace=os.environ.get("TIDEN_WORKSPACE_ID", ""))
    print(json.dumps({k: result[k] for k in ("seqNum", "status", "branchName", "branchId", "buildSha", "stats", "liveDocStatus")}))
    return 0 if result["status"] in {"passed", "skipped"} else 1


def release(args: argparse.Namespace, directory: Path) -> int:
    if args.environment == "github-actions-synthetic":
        # Only this main push run's own published evidence names the commit and its build URL.
        if args.sha or args.url or args.released_at:
            raise ValueError("a CI release takes its commit, URL and time from the published run")
        manifest = published_manifest(directory, json.loads((directory / "manifest.json").read_text()))
        if (manifest.get("event"), manifest.get("git_branch"), manifest.get("tiden_branch")) != ("push", "main", ""):
            raise ValueError("only a main push run records a CI release")
        sha, url = manifest["sha"], manifest["ci_url"]
        released_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif args.environment == "production":
        if not (args.sha and args.url and args.released_at):
            raise ValueError("a production release needs the promoted commit, its CI run URL and the apply time")
        sha, url, released_at = args.sha, args.url, args.released_at
    else:
        raise ValueError("select the release environment")
    try:
        receipt = record_release(directory, sha, args.environment, url, released_at, os.environ["TIDEN_PRODUCT_ID"])
    except StaleRelease as exc:
        if args.environment == "production":
            raise
        # A newer main commit finished first; its own run records its release.
        print(f"::warning title=Tiden release not recorded::{exc}")
        return 0
    except ForeignGate as exc:
        if args.environment == "production":
            raise
        # The synthetic record is not promotion evidence; its receipt keeps the finding.
        print(f"::warning title=Tiden release gate from another run::{exc}")
        return 0
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
