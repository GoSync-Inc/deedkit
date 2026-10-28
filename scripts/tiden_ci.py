"""Preserve real CI outcomes and explicitly associate evidence with a Tiden branch.

The preparation runner has no Tiden credentials. The publishing runner executes
only this tool and the pinned Tiden CLI, never tests or application code.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET

CHECKS = ("build", "release", "python", "collector", "npm", "smoke", "distributions")
REPORTS = {"python": "python.xml", "collector": "collector.xml", "npm": "npm.xml"}
MODULE_REPORTS = {
    "load": ("load.xml", "python", ""),
    "gateway": ("gateway.xml", "python", "services/gateway"),
    # The shared contract package the gateway depends on, run from its own directory in its own verify step.
    "platform_contracts": ("platform-contracts.xml", "python", "packages/platform-contracts"),
    "calendar": ("calendar.xml", "python", "services/calendar"),
    "calls": ("calls.xml", "python", "services/calls"),
    "calls_frontend": ("calls-frontend.xml", "node", "services/calls/frontend"),
    "backup_restore": ("backup-restore.xml", "python", ""),
    # Root-suite tests that cannot run in the shards: one as root under sudo, two in the enricher-test image.
    "root_session_lock": ("root-session-lock.xml", "python", ""),
    "enricher_audio": ("enricher-audio.xml", "python", ""),
    # `go test -json` streams of the `go` job, published as result rows (go_results), not JUnit.
    "whatsmeow": ("whatsmeow.json", "go", "whatsapp/whatsmeow"),
    "release_router": ("release-router.json", "go", "services/release-router"),
}
# A module that runs named root-suite nodes passes only when its report holds exactly these executions (file::name),
# each once and passed; the shards deselect them, so assembly refuses evidence whose python.xml holds one of them.
REQUIRED_EXECUTIONS = {
    "root_session_lock": frozenset({"tests/test_collectors.py::test_session_lock_rejects_directory_chowned_to_foreign_uid_as_root"}),
    "enricher_audio": frozenset({"tests/test_audio_native.py::test_optional_cpu_package_loads_without_gpu_or_model_download",
                                 "tests/test_audio_native.py::test_faster_whisper_decoder_compatibility_without_model[wav]",
                                 "tests/test_audio_native.py::test_faster_whisper_decoder_compatibility_without_model[ogg]"}),
}
GO_MODULES = tuple(name for name, (_, framework, _) in MODULE_REPORTS.items() if framework == "go")
_VERIFY_MODULES = tuple(name for name in MODULE_REPORTS if name != "load" and name not in GO_MODULES)
# The root Python suite runs in file-level shards (scripts/python_shards.py): verify runs shard 1, each
# `checks / python` matrix job one of the others, and assembly merges their reports into python.xml.
PYTHON_SHARDS = 6
SHARD_REPORTS = tuple(f"python-{index}.xml" for index in range(1, PYTHON_SHARDS + 1))
SHARD_JOBS = tuple(f"python-{index}" for index in range(2, PYTHON_SHARDS + 1))
# Each test job deletes the files autoloop pull requests add to test directories from its checkout and keeps that
# exclusion inventory as `exclusions.json` (scripts/added_tests.py, ANYM-578); every fragment's must be the same.
EXCLUSIONS, ADDED_REPORT = "exclusions.json", "added.json"
# Each parallel test job uploads one `ci-evidence-<job>` fragment: the module checks it runs and the files it
# produces (verify's seven checks travel in its metadata.json, a shard's `python` outcome in its outcomes.json),
# besides the `source-sha` and `attempt` it ran. `added`, the job that runs only the added tests on its own runner,
# reports no module check: its result is its producer result and `added.json`.
FRAGMENTS = {
    "verify": (_VERIFY_MODULES, ("metadata.json", SHARD_REPORTS[0], *(name for check, name in REPORTS.items() if check != "python"),
                                 *(MODULE_REPORTS[name][0] for name in _VERIFY_MODULES), EXCLUSIONS)),
    "load": (("load",), ("outcomes.json", MODULE_REPORTS["load"][0], "load.json", EXCLUSIONS)),
    **{job: ((), ("outcomes.json", report, EXCLUSIONS)) for job, report in zip(SHARD_JOBS, SHARD_REPORTS[1:])},
    "go": (GO_MODULES, ("outcomes.json", *(MODULE_REPORTS[name][0] for name in GO_MODULES), EXCLUSIONS)),
    "added": ((), ("outcomes.json", ADDED_REPORT, EXCLUSIONS)),
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
CLI_TIMEOUT = 120
# The CI release record's overall deadline, at least 30 seconds inside its step's 5-minute timeout (ci.yml), so the
# script, not the runner, ends a hang and leaves the receipt. Tiden calls end CI_RECORD_MARGIN earlier, so an
# unavailable Tiden is classified before the overall deadline ends anything still running.
CI_RELEASE_DEADLINE = 270
CI_RECORD_MARGIN = 10
RELEASE_RECEIPT = "tiden-release-receipt.json"
# After a published run fails, its failures grouped by message (`run triage`, no baseline), as advisory summary lines
# (TIDEN-CI.md). The call ends PUBLISH_MARGIN before the publish step's 3-minute timeout (checks.yml step `publish`),
# measured from the script's start, so a slow triage never turns into a killed publish step.
TRIAGE = "tiden-triage.json"
PUBLISH_STEP_LIMIT = 180
PUBLISH_MARGIN = 30
TRIAGE_TIMEOUT = 60
TRIAGE_MINIMUM = 10
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# The advisory list after a production release is recorded (production-release.yml): the issues Tiden first saw in
# the previous production release, as a projection only, never the raw reply (titles and messages stay in Tiden).
PREVIOUS_ISSUES = "tiden-previous-issues.json"
# Inside the step's 1-minute timeout, so the script, not the runner, ends a hang and writes its fixed reason.
ISSUES_DEADLINE = 45
ISSUE_ID = re.compile(r"[0-9A-Za-z-]{1,64}")
RELEASE_VERSION = re.compile(r"[0-9A-Za-z@._+-]{1,80}")
# A main push run that reused the merge queue's verification of its commit (scripts/ci_reuse.py) publishes that
# run's unchanged evidence with this record of itself as publisher, and these check rows under its own run URL.
PUBLICATION = "publication.json"
PUBLISHED_CHECKS = "ci-checks-published.json"
PRODUCER_FIELDS = ("repository", "workflow", "run_id", "run_attempt", "url", "artifact_id", "artifact_digest", "sha")
# The PR→queue chain: the queue run reused a pull request run of exactly this tree, and the push run publishes that
# pull request run's evidence; `tree` is the git tree hash both commits have (scripts/ci_reuse.py adopt_chain).
CHAIN_FIELDS = ("pull_request", "head_branch", "queue_run_id", "queue_run_attempt", "tree")


def checkout_tree(sha: str, root: Path | None = None) -> str:
    """The git tree hash of this checkout's commit, which must be exactly `sha`; empty when it cannot be read.

    Git runs without replacement objects and without any inherited `GIT_*` setting, so neither can redirect it to
    another repository or show another commit's tree."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"

    def git(*arguments: str) -> str:
        return subprocess.run(["git", "-C", str(root or Path(__file__).resolve().parents[1]), *arguments], check=True,
                              capture_output=True, text=True, timeout=60, env=environment).stdout.strip()
    try:
        return git("rev-parse", "--verify", "HEAD^{tree}") if git("rev-parse", "--verify", "HEAD") == sha else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def commit_tree(repository: str, sha: str) -> str:
    """The git tree hash GitHub records for commit `sha` of `repository`, read anonymously from the public API (the
    tested merge commit of a pull request is in no local history); empty when it cannot be read."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not re.fullmatch(r"[a-f0-9]{40}", sha):
        return ""
    request = urllib.request.Request(f"https://api.github.com/repos/{repository}/git/commits/{sha}",
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "deedkit-tiden-ci"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            commit = json.loads(response.read(1_000_000))
        tree = str((commit.get("tree") or {}).get("sha", "")) if commit.get("sha") == sha else ""
    except (OSError, ValueError, AttributeError):
        return ""
    return tree if re.fullmatch(r"[a-f0-9]{40}", tree) else ""


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
        counts[case_status(case)] += 1
    return ET.tostring(document, encoding="utf-8", xml_declaration=True), counts


GO_VERDICTS = {"pass": "passed", "fail": "failed", "skip": "skipped"}
GO_TEST_FUNCTION = re.compile(r"^func ((?:Test|Example|Fuzz)[A-Za-z0-9_]*)\(", re.MULTILINE)
# A go.mod `module` directive's argument: an interpreted "string" (Go escapes), a `raw` string or a bare token.
GO_MODULE_ARGUMENT = re.compile(r'"((?:[^"\\\n]|\\.)*)"|`([^`\n]*)`|(\S+)')
# `go test -json` writes test names with spaces as underscores, so a name is any run of non-space characters.
GO_SIGNATURE = re.compile(r"[A-Za-z0-9._~/-]+::(?:Test|Example|Fuzz)\S*")
BARE_GO_FILE = re.compile(r"[A-Za-z0-9_.-]+_test\.go")
ROW_KEYS, EXECUTION_KEYS = {"id", "title", "signature", "execution", "suitePath", "fields"}, {"status", "duration"}


def case_status(case: ET.Element) -> str:
    return "failed" if case.find("failure") is not None or case.find("error") is not None else (
        "skipped" if case.find("skipped") is not None else "passed")


def executions(cases: object, root: Path) -> Counter:
    """Each JUnit test case's `file::name` identity, its path placed as `normalize_junit` places it (xunit2 omits
    file: its classname's path), with its status."""
    return Counter((source_path(case.get("file") or case.get("classname", "").replace(".", "/") + ".py", root)
                    + "::" + case.get("name", ""), case_status(case)) for case in cases)


def normalized_report(check: str) -> str:
    return f"normalized-{check}.json" if MODULE_REPORTS.get(check, ("", ""))[1] == "go" else f"normalized-{check}.xml"


def row_id(ci_url: str, check: str, signature: str) -> str:
    """A Go result row's ID, the idempotency key Tiden deduplicates on: new for every publishing run attempt."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ci_url}/{check}/{signature}"))


def go_module_path(text: str) -> str | None:
    """The module path a go.mod declares, read as the Go tool reads it: `//` starts a comment anywhere outside a
    string, and a quoted path has Go escapes decoded. None when no `module` line declares a usable path."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("module") or line[6:7] not in (" ", "\t"):
            continue
        found = GO_MODULE_ARGUMENT.match(line[6:].lstrip())
        if not found:
            return None
        quoted, raw, bare = found.groups()
        if quoted is not None:
            try:  # Go's interpreted-string escapes are a subset of Python's for a double-quoted literal.
                path = ast.literal_eval(f'"{quoted}"')
            except (ValueError, SyntaxError):
                return None
        else:
            path = raw if raw is not None else bare.split("//", 1)[0]
        return path if path and path[0] != "(" and not re.search(r'[\s"`\\]', path) else None
    return None


def go_module_of(root: Path, check: str) -> str:
    """The module path of a Go module check, from its go.mod under `root`."""
    module = go_module_path((root / MODULE_REPORTS[check][2] / "go.mod").read_text())
    if not module:
        raise ValueError("the Go module path is missing")
    return module


def in_module(package: str, module: str) -> bool:
    return package == module or package.startswith(module + "/") and not (
        {"", ".", ".."} & set(package[len(module) + 1:].split("/")))


def go_results(path: Path, root: Path, module_dir: str, ci_url: str, check: str) -> tuple[list[dict], Counter, bool]:
    """Tiden result rows from one `go test -json` stream, named as Tiden CLI 0.33.0's Go reporter (`tiden run exec`)
    named them, so the existing cases and their spec links keep receiving results: signature `<Package>::<Test>`
    byte for byte (Tiden lowercases JUnit identities), title the test name, the package's last path element as the
    suite and the bare name of the `_test.go` file declaring the top-level test. Only names, statuses and durations
    leave; test output does not. A test that started without a verdict failed. Also returns whether a package failed
    without a failed test to explain it (a build failure, a panic after the tests). Raises ValueError for a stream
    without any test result, a malformed event, a package outside the module or a test whose file cannot be
    named."""
    data = path.read_bytes()
    if len(data) > MAX_BYTES:
        raise ValueError("oversized go test stream")
    module = go_module_path((root / module_dir / "go.mod").read_text())
    if not module:
        raise ValueError("the Go module path is missing")
    verdicts, started, failed_packages, order = {}, set(), set(), []
    for line in data.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        # Any action is accepted (go test adds new ones, such as `attr` and `artifacts`); only run and the verdicts
        # make rows, and no other field of an event leaves.
        if not isinstance(event, dict) or not event.get("Action") \
                or not all(isinstance(event.get(key, ""), str) for key in ("Action", "Package", "Test", "ImportPath")):
            raise ValueError("not a go test -json event")
        action, package, test = event["Action"], event.get("Package", ""), event.get("Test", "")
        if action.startswith("build-"):
            if action == "build-fail":
                failed_packages.add(event.get("ImportPath") or "build")
            continue
        if not package or not in_module(package, module):
            raise ValueError("a Go package lies outside its module")
        if not test:
            if action == "fail":
                failed_packages.add(package)
            continue
        if action != "run" and action not in GO_VERDICTS:
            continue
        key = (package, test)
        if key not in verdicts and key not in started:
            order.append(key)
        if action == "run":
            started.add(key)
        elif action in GO_VERDICTS:
            elapsed = event.get("Elapsed") or 0
            if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or not 0 <= elapsed < 10**6:
                raise ValueError("a go test -json duration is malformed")
            verdicts[key] = (GO_VERDICTS[action], int(elapsed * 1000))
    # Like Tiden's Go reporter, a test with subtests is no case of its own: its subtests are. A subtest name may
    # itself contain `/`, so every observed name that another one extends at a `/` is an ancestor.
    names = set(order)
    parents = {(package, test[:index]) for package, test in order for index, char in enumerate(test)
               if char == "/" and (package, test[:index]) in names}
    order = [key for key in order if key not in parents]
    if not order:
        raise ValueError("the go test stream has no test results")
    declared: dict[str, dict[str, list[str]]] = {}
    rows, counts = [], Counter()
    for package, test in order:
        if package not in declared:
            directory = root / module_dir / package[len(module) + 1:]
            files = declared[package] = {}
            for file in sorted(directory.glob("*_test.go")):
                for name in GO_TEST_FUNCTION.findall(file.read_text()):
                    files.setdefault(name, []).append(file.name)
        files = declared[package].get(test.split("/", 1)[0], [])
        if len(files) != 1:
            raise ValueError("a Go test's source file cannot be named")
        status, duration = verdicts.get((package, test), ("failed", 0))
        signature = f"{package}::{test}"
        if not GO_SIGNATURE.fullmatch(signature):
            raise ValueError("a Go test identity is malformed")
        rows.append({"id": row_id(ci_url, check, signature), "title": test, "signature": signature,
                     "execution": {"status": status, "duration": duration},
                     "suitePath": [{"title": package.rsplit("/", 1)[-1]}], "fields": {"file_path": files[0]}})
        counts[status] += 1
    explained = {package for (package, _), row in zip(order, rows) if row["execution"]["status"] == "failed"}
    return rows, counts, bool(failed_packages - explained)


def go_row_counts(rows: object, module: str) -> Counter:
    """The statuses of prepared Go result rows of `module`, refused unless every row has exactly the shape
    go_results writes, names a package of that module and is unique."""
    if not isinstance(rows, list) or not rows or len(rows) > 10000:
        raise ValueError("Go results must contain bounded real executions")
    counts, seen = Counter(), set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != ROW_KEYS or not isinstance(row["signature"], str) \
                or not GO_SIGNATURE.fullmatch(row["signature"]):
            raise ValueError("a Go result row is malformed")
        package, test = row["signature"].split("::", 1)
        execution, fields, suite = row["execution"], row["fields"], row["suitePath"]
        try:
            uuid.UUID(row["id"])
        except (TypeError, ValueError, AttributeError):
            raise ValueError("a Go result row is malformed") from None
        if not in_module(package, module) or row["title"] != test or row["signature"] in seen or row["id"] in seen \
                or not isinstance(execution, dict) or set(execution) != EXECUTION_KEYS \
                or execution["status"] not in GO_VERDICTS.values() or type(execution["duration"]) is not int \
                or not 0 <= execution["duration"] < 10**9 \
                or suite != [{"title": package.rsplit("/", 1)[-1]}] \
                or not isinstance(fields, dict) or set(fields) != {"file_path"} \
                or not isinstance(fields["file_path"], str) or not BARE_GO_FILE.fullmatch(fields["file_path"]):
            raise ValueError("a Go result row is malformed")
        seen.update((row["signature"], row["id"]))
        counts[execution["status"]] += 1
    return counts


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
                unexplained = False
                if framework == "go":
                    results, totals, unexplained = go_results(directory / filename, root, prefix, metadata["ci_url"], check)
                    normalized = json.dumps(results, indent=2).encode()
                else:
                    normalized, totals = normalize_junit(directory / filename, root, framework, prefix)
                filename = normalized_report(check)
                (directory / filename).write_bytes(normalized)
                reports.append(filename)
                counts.update(totals)
                if (totals["failed"] or unexplained) and outcome == "success":
                    outcome = "failure"
                if not totals["passed"] and not totals["failed"]:
                    outcome, reason = "skipped", "No tests executed; only skipped tests"
                if check in REQUIRED_EXECUTIONS and executions(ET.fromstring(normalized).iter("testcase"), root) != Counter(
                        (name, "passed") for name in REQUIRED_EXECUTIONS[check]):
                    outcome, reason = "failure", "The report does not hold exactly the required executions, each once and passed"
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


def assemble(directory: Path, fragments: Path, ci_url: str, producers: dict, *, source_root: Path | None = None) -> dict:
    """Merge the evidence fragments that the parallel test jobs uploaded into the directory `prepare` reads.

    `producers` maps each job of FRAGMENTS to its job result and the checkout SHA its job output captured. A
    fragment holds only its own files and checks and must name verify's tested SHA, which is the SHA its job
    captured. A job without a fresh fragment contributes no reports, and its checks take its job result
    (`skipped` when it succeeded), so it never reads as passed; such jobs are returned in `missing`. A stale
    verify fragment still supplies the run's routing metadata (SHA, Git and Tiden branch), never its outcomes.
    The `python` check passes only when every shard (verify's and each shard job's) passed with a fresh fragment
    and its own valid report of executed tests (`source_root`, the tested checkout, places their paths), and
    each shard job succeeded: a shard job that failed or was cancelled after its tests never reads as passed. A
    shard job's result is the one it recorded before its receiving-commit check, which a baseline of another
    source fails by design (checks.yml). The valid shard reports merge into one python.xml, so test identities
    do not change. Every fresh fragment holds the same exclusion inventory (`EXCLUSIONS`), which the merged
    evidence keeps, so no job tested another set of files than the others (ANYM-578).
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
    present, missing, shards, suites, inventories = [], [], [], [], {}
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
        inventories[job] = control_file(fragment, EXCLUSIONS)
        if len(set(inventories.values())) > 1:
            raise ValueError(f"the {job} fragment excluded other added tests than another job")
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
            if state == "success" and result != "success":
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
    required = set().union(*REQUIRED_EXECUTIONS.values())
    if any(name in required for name, _ in executions((case for suite in suites for case in suite.iter("testcase")), source_root)):
        raise ValueError("a shard report holds a test that only its own module check may run")
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

    A record with a `chain` instead names the pull request run the queue reused: its evidence is of the merge commit
    that run tested, of the record's head branch, and is accepted only when the git tree hash GitHub records for that
    tested commit, the record's tree and the tree of this checkout's commit, which must be the push commit, are one
    tree. The published manifest then names the push commit, and `tested_sha` the tested merge commit.
    """
    if not (directory / PUBLICATION).exists() and not (directory / PUBLICATION).is_symlink():
        return manifest
    repository, run_id, attempt, sha, event, ref = (os.environ.get(name, "") for name in (
        "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA", "GITHUB_EVENT_NAME", "GITHUB_REF"))
    ci_url = f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{attempt}"
    go_reports: dict[str, list] = {}
    try:
        record = json.loads(control_file(directory, PUBLICATION))
        producer = record["producer"]
        producer_url = "https://github.com/{}/actions/runs/{}/attempts/{}".format(
            repository, producer["run_id"], producer["run_attempt"])
        chain = record.get("chain")
        if chain is None:
            tested = sha
            source = producer["sha"] == manifest.get("sha") == sha and merge_queue_run(manifest)
        else:
            tested = producer["sha"]
            source = (isinstance(chain, dict) and set(chain) == set(CHAIN_FIELDS) and set(record) == {
                          "schema", "event", "git_branch", "repository", "run_id", "run_attempt", "sha", "ci_url",
                          "producer", "chain"}
                      and all(isinstance(chain[key], str) for key in CHAIN_FIELDS)
                      and chain["pull_request"].isdigit() and chain["queue_run_id"].isdigit()
                      and chain["queue_run_attempt"].isdigit() and chain["head_branch"] != ""
                      and isinstance(tested, str) and re.fullmatch(r"[a-f0-9]{40}", tested) is not None
                      and tested == manifest.get("sha") != sha and manifest.get("event") == "pull_request"
                      and manifest.get("git_branch") == chain["head_branch"] and not merge_queue_run(manifest)
                      and re.fullmatch(r"[a-f0-9]{40}", chain["tree"]) is not None
                      and checkout_tree(sha) == chain["tree"] == commit_tree(repository, tested))
        valid = ((event, ref) == ("push", "refs/heads/main") and bool(RUN_ATTEMPT_URL.fullmatch(ci_url))
                 and re.fullmatch(r"[a-f0-9]{40}", sha) is not None
                 and {key: record.get(key) for key in ("schema", "event", "git_branch", "repository", "run_id",
                                                       "run_attempt", "sha", "ci_url")}
                 == {"schema": 1, "event": "push", "git_branch": "main", "repository": repository, "run_id": run_id,
                     "run_attempt": attempt, "sha": sha, "ci_url": ci_url}
                 and source
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
        # Go result rows carry IDs too; Tiden deduplicates on them, so this run publishes them under its own.
        for check in GO_MODULES:
            if normalized_report(check) in manifest["reports"] and valid:
                results = json.loads(control_file(directory, normalized_report(check)))
                go_row_counts(results, go_module_of(Path(__file__).resolve().parents[1], check))
                go_reports[normalized_report(check)] = [{**row, "id": row_id(ci_url, check, row["signature"])}
                                                        for row in results]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        valid = False
    if not valid:
        raise ValueError("the publication record does not name this main push run and its merge queue evidence, "
                         "or the pull request evidence of exactly this commit's tree")
    (directory / PUBLISHED_CHECKS).write_text(json.dumps(published, indent=2))
    for name, results in go_reports.items():
        (directory / ("published-" + name)).write_text(json.dumps(results, indent=2))
    reports = [("published-" + name if name in go_reports else name) for name in manifest["reports"]
               if name != "ci-checks.json"] + [PUBLISHED_CHECKS]
    chained = {"sha": sha, "tested_sha": tested} if tested != sha else {}
    return {**manifest, "event": "push", "git_branch": "main", "tiden_branch": "", "pr_tiden_links": [],
            "ci_url": ci_url, "tested_in": producer["url"], "reports": reports, **chained}


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


def tiden(cli: str, arguments: list[str], product: str, directory: Path, label: str,
          timeout: float = CLI_TIMEOUT) -> tuple[int, str]:
    """Run one Tiden CLI command and keep its redacted JSON output as a receipt."""
    completed = subprocess.run([cli, *arguments, "--product-id", product, "--format", "json"],
                               capture_output=True, text=True, timeout=timeout)
    token = os.environ.get("TIDEN_API_TOKEN", "")
    output = completed.stdout.replace(token, "[redacted]") if token else completed.stdout
    (directory / f"tiden-{label}.json").write_text(output)
    return completed.returncode, output


def evidence_failures(manifest: dict) -> str:
    """The failed tests and the failed or blocked checks prepared evidence records; empty when all passed."""
    tests, checks = manifest["test_counts"], manifest["check_counts"]
    failures = [f"{tests['failed']} failed test(s)"] if tests.get("failed") else []
    return ", ".join(failures + [f"{checks[status]} {status} check(s)" for status in ("failed", "blocked") if checks.get(status)])


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
        if evidence_failures(manifest) and result["status"] != "failed":
            raise RuntimeError("Tiden verdict contradicts failed or incomplete CI evidence")
        return result
    finally:
        if not completed:
            call(["run", "abort", seq], "abort")


class TriageUnavailable(RuntimeError):
    """A triage read failed; the message is one fixed reason, never CLI output."""


def triage_budget(started: float, now: float) -> float:
    return min(TRIAGE_TIMEOUT, PUBLISH_STEP_LIMIT - PUBLISH_MARGIN - (now - started))


def plain(text: str) -> str:
    return CONTROL_CHARACTERS.sub("", text)[:200]


def triage(directory: Path, seq: int, product: str, started: float, cli: str = "tiden",
           clock=time.monotonic) -> list[str]:
    """Group a failed published run's failures by message: summary lines, and only `{count, exemplar}` per group in
    the receipt. Any failure is one fixed line; it never changes the publication result."""
    try:
        timeout = triage_budget(started, clock())
        if timeout < TRIAGE_MINIMUM:
            raise TriageUnavailable("no time")
        try:
            completed = subprocess.run([cli, "run", "triage", str(seq), "--limit", "10", "--cluster-exemplars", "3",
                                        "--failure-output-lines", "0", "--product-id", product, "--format", "json"],
                                       capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TriageUnavailable("timeout") from None
        except OSError:
            raise TriageUnavailable("CLI not started") from None
        if completed.returncode:
            raise TriageUnavailable(f"exit {completed.returncode}")
        try:
            reply = json.loads(completed.stdout)
        except ValueError:
            raise TriageUnavailable("malformed") from None
        clusters = reply.get("clusters") if isinstance(reply, dict) else None
        if not isinstance(clusters, list) or not all(
                isinstance(cluster, dict) and type(cluster.get("count")) is int and cluster["count"] > 0
                and isinstance(cluster.get("message"), str) and isinstance(cluster.get("failures"), list)
                and cluster["failures"] and isinstance(cluster["failures"][0], dict)
                and isinstance(cluster["failures"][0].get("title"), str) for cluster in clusters):
            raise TriageUnavailable("malformed")
        groups = [{"count": cluster["count"], "exemplar": plain(cluster["failures"][0]["title"])}
                  for cluster in clusters[:10]]
        lines = [f"Tiden triage: {group['count']} × {group['exemplar']}: {plain(cluster['message'])}"
                 for group, cluster in zip(groups, clusters)] or ["Tiden triage: no failure groups"]
        record = {"groups": groups}
    except TriageUnavailable as exc:
        record, lines = {"state": "not available", "reason": str(exc)}, [f"Tiden triage: not available ({exc})"]
    except Exception:
        reason = "unexpected error"
        record, lines = {"state": "not available", "reason": reason}, [f"Tiden triage: not available ({reason})"]
    try:
        (directory / TRIAGE).write_text(json.dumps({"triage": record}, indent=2))
    except OSError:
        lines = [*lines, "Tiden triage: receipt not written"]
    return lines


class StaleRelease(RuntimeError):
    """The latest completed main run is not this commit's, so a new release would capture another gate."""


class ForeignGate(RuntimeError):
    """The release exists, but the main gate it captured did not come from this commit's run."""


class TidenUnavailable(RuntimeError):
    """A Tiden call exited non-zero, printed unparseable output or passed its timeout; `state` is the receipt's."""
    state = "not_attempted"


class RecordDeadline(RuntimeError):
    """The CI release record passed its overall deadline outside a Tiden call, for example in manifest validation."""


def when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def record_release(directory: Path, sha: str, environment: str, url: str, released_at: str, product: str,
                   cli: str = "tiden", deadline: float | None = None) -> dict:
    """Record release deedkit@<sha> and prove its captured gate came from this commit's passed main run.

    Tiden captures a release's gate from the current main gate when the release is first
    created; a repeated create updates its name, URL and time but keeps that first gate.
    The proof is stateless: the main run that completed last before the release's creation,
    at least GATE_SETTLE earlier, must be a passed run of this commit.

    With a `deadline` (time.monotonic()), every call and the settle wait end by it. When Tiden is unavailable the
    receipt records `not_attempted` before the create call is sent and `not_verified` after it; a create whose
    outcome is uncertain is never retried.
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

    version = "deedkit@" + sha
    progress = {"state": "not_attempted", "version": version, "environment": environment, "url": url,
                "releasedAt": released_at}

    def save() -> None:
        # Written at every state change, so even a killed or crashed record leaves an honest receipt.
        (directory / RELEASE_RECEIPT).write_text(json.dumps(progress, indent=2))

    save()

    def unavailable(message: str) -> TidenUnavailable:
        """The receipt keeps what is known; the exception carries its state."""
        save()
        exc = TidenUnavailable(message)
        exc.state = progress["state"]
        return exc

    def left(label: str) -> float:
        if deadline is None:
            return CLI_TIMEOUT
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise unavailable(f"Tiden {label} was not sent: the record deadline passed")
        return min(CLI_TIMEOUT, remaining)

    def run(arguments: list[str], label: str, create: bool = False) -> tuple[int, str]:
        timeout = left(label)
        if create:
            progress["state"] = "not_verified"  # Once sent, the release may exist whatever comes back.
            save()
        try:
            return tiden(cli, arguments, product, directory, label, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise unavailable(f"Tiden {label} passed its {timeout:.0f}-second timeout") from None

    def call(arguments: list[str], label: str, create: bool = False) -> dict:
        returncode, output = run(arguments, label, create)
        if returncode:
            raise unavailable(f"Tiden {label} failed; the release was not verified")
        try:
            value = json.loads(output)
        except json.JSONDecodeError:
            value = None
        if not isinstance(value, dict):
            raise unavailable(f"Tiden {label} printed unparseable output; the release was not verified")
        return value

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
    if deadline is not None:
        wait = min(wait, timedelta(seconds=max(0.0, deadline - time.monotonic())))
    if wait > timedelta(0):
        time.sleep(wait.total_seconds())
    release = call(["release", "create", "--version", version, "--environment", environment,
                    "--name", "DeedKit " + sha[:12], "--url", url, "--released-at", released_at],
                   "release", create=True)
    if (release.get("version"), release.get("environmentSlug"), release.get("url")) != (version, environment, url):
        raise RuntimeError("Tiden rewrote the release identity")
    progress.update(releaseId=release["id"], createdAt=release["createdAt"])
    save()
    # A repeated create keeps the original creation time, and with it the original gate.
    created = when(release["createdAt"])
    captured = latest_main_run(created, "release-runs-captured")
    own_gate = (captured.get("buildSha") == sha and captured.get("status") == "passed"
                and created - when(captured["completedAt"]) >= GATE_SETTLE)
    returncode, output = run(["gate", "check", "--release", release["id"]], "release-gate")
    try:
        answer = json.loads(output) if output.strip() else None
    except json.JSONDecodeError:
        answer = output
    gate = answer if isinstance(answer, dict) else {}
    if returncode and not output.strip():
        # Tiden exits non-zero and prints nothing when the release captured no gate.
        print("::warning title=Tiden release gate::no gate was captured for " + version)
        own_gate = None
    elif not isinstance(gate.get("status"), str):
        # A verdict, passed or not, always names its status; anything else is not an answer.
        raise unavailable("Tiden release-gate printed no gate verdict; the release was not verified")
    receipt = {"state": "recorded", "releaseId": release["id"], "version": version, "environment": environment,
               "url": url, "releasedAt": released_at, "createdAt": release["createdAt"], "mainRun": ours["id"],
               "gateRun": captured.get("id"), "gateFromThisCommit": own_gate,
               "gate": gate.get("status"), "gateComputedAt": gate.get("computedAt")}
    (directory / RELEASE_RECEIPT).write_text(json.dumps(receipt, indent=2))
    if own_gate is False:
        raise ForeignGate(f"release {version} captured the main gate after run {captured.get('id')} of "
                          f"{captured.get('buildSha')}; do not use its gate for readiness")
    return receipt


class IssuesUnavailable(RuntimeError):
    """A release-issues read failed; the message is one fixed reason, never CLI output."""


def aware(value: object) -> datetime | None:
    """A timezone-aware timestamp, or None for a missing one; any other value is malformed."""
    if value is None:
        return None
    try:
        stamp = when(value) if isinstance(value, str) else None
    except ValueError:
        stamp = None
    if stamp is None or stamp.tzinfo is None:
        raise IssuesUnavailable("malformed")
    return stamp


def previous_release(releases: list, release_id: str, released_at: str) -> tuple[dict | None, bool]:
    """The production release recorded last before `released_at`, other than `release_id`, and whether that choice
    is ambiguous: two releases at the same time, one at this release's own time, or one without `releasedAt` (a
    repeated create keeps `createdAt`, so nothing bounds a missing release time)."""
    target, dated, ambiguous = when(released_at), [], False
    for release in releases:
        if release["id"] == release_id:
            continue
        stamp = aware(release.get("releasedAt"))
        if stamp is None:
            ambiguous = True
        else:
            dated.append((stamp, release))
    earlier = [(stamp, release) for stamp, release in dated if stamp < target]
    latest = max((stamp for stamp, _ in earlier), default=None)
    chosen = [release for stamp, release in earlier if stamp == latest]
    ambiguous = ambiguous or len(chosen) > 1 or any(stamp == target for stamp, _ in dated)
    return (chosen[0] if chosen else None), ambiguous


def release_issues(directory: Path, released_at: str, product: str, cli: str = "tiden",
                   deadline: float | None = None) -> dict:
    """Project the issues first seen in the previous production release and write only that projection."""
    deadline = time.monotonic() + ISSUES_DEADLINE if deadline is None else deadline

    def read(arguments: list[str]) -> object:
        left = deadline - time.monotonic()
        if left <= 0:
            raise IssuesUnavailable("timeout")
        try:
            completed = subprocess.run([cli, *arguments, "--product-id", product, "--format", "json"],
                                       capture_output=True, text=True, timeout=left)
        except subprocess.TimeoutExpired:
            raise IssuesUnavailable("timeout") from None
        if completed.returncode:
            raise IssuesUnavailable(f"exit {completed.returncode}")
        try:
            return json.loads(completed.stdout)
        except ValueError:
            raise IssuesUnavailable("malformed") from None

    try:
        try:
            receipt = json.loads((directory / RELEASE_RECEIPT).read_text())
        except (OSError, ValueError):
            receipt = None
        release_id = receipt.get("releaseId") if isinstance(receipt, dict) else None
        if not isinstance(receipt, dict) or receipt.get("state") != "recorded" or not isinstance(release_id, str) \
                or not release_id or not RELEASED_AT.fullmatch(released_at):
            raise IssuesUnavailable("no recorded release")
        listing = read(["release", "list", "--environment", "production"])
        releases = listing.get("releases") if isinstance(listing, dict) else None
        pagination = listing.get("pagination") if isinstance(listing, dict) else None
        if not isinstance(pagination, dict):
            raise IssuesUnavailable("malformed")
        if pagination.get("nextPageToken"):
            # The CLI (0.41.0, 0.42.0) has no page token for `release list`; a partial list cannot prove the predecessor.
            raise IssuesUnavailable("more pages")
        if (not isinstance(releases, list)
                or not all(isinstance(release, dict) and isinstance(release.get("id"), str) and release["id"]
                           and isinstance(release.get("version"), str)
                           and RELEASE_VERSION.fullmatch(release["version"]) for release in releases)):
            raise IssuesUnavailable("malformed")
        predecessor, ambiguous = previous_release(releases, release_id, released_at)
        if ambiguous or predecessor is None:
            result = {"state": "ambiguous predecessor" if ambiguous else "no predecessor", "predecessor": None,
                      "newIssues": [], "seenCount": None}
        else:
            reply = read(["release", "issues", predecessor["id"]])
            issues = reply.get("newIssues") if isinstance(reply, dict) else None
            seen = reply.get("seenCount") if isinstance(reply, dict) else None
            if (not isinstance(issues, list) or not all(isinstance(issue, dict) and isinstance(issue.get("id"), str)
                                                        and ISSUE_ID.fullmatch(issue["id"]) for issue in issues)
                    or not (type(seen) is int or isinstance(seen, str) and seen.isascii() and seen.isdigit() and len(seen) <= 12)):
                raise IssuesUnavailable("malformed")
            result = {"state": "listed", "predecessor": {"id": predecessor["id"], "version": predecessor["version"]},
                      "newIssues": [issue["id"] for issue in issues], "seenCount": int(seen)}
    except IssuesUnavailable as exc:
        result = {"state": "unavailable", "reason": str(exc)}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Unexpected input or a failed CLI start keeps the fixed-reason contract; the exception text stays out.
        result = {"state": "unavailable", "reason": "malformed"}
    (directory / PREVIOUS_ISSUES).write_text(json.dumps(result, indent=2))
    return result


def issues_line(result: dict) -> str:
    if result["state"] == "listed":
        version, issues = result["predecessor"]["version"], result["newIssues"]
        shown = version[:20] if version.startswith("deedkit@") else version
        return f"first seen in {shown}: {len(issues)} issues" + (": " + ", ".join(issues) if issues else "")
    if result["state"] == "unavailable":
        return f"Tiden previous-release issues: unavailable ({result['reason']})"
    return f"Tiden previous-release issues: none listed ({result['state']})"


def main() -> int:
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["assemble", "prepare", "verdict", "publish", "queue-receipt", "release",
                                            "release-issues"])
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
    if args.command == "release-issues":
        if args.environment != "production":
            raise ValueError("only a production release lists the previous release's issues")
        result = release_issues(directory, args.released_at or "", os.environ.get("TIDEN_PRODUCT_ID", ""))
        line = issues_line(result)
        print(line)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
                summary.write(line + "\n")
        # A failed read fails only this continue-on-error step, so the run shows it; the record is unchanged.
        return 1 if result["state"] == "unavailable" else 0
    if args.command == "queue-receipt":
        # A merge queue run publishes nothing; its receipt needs no evidence download (ANYM-500).
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "tiden-route.json").write_text(json.dumps({"branch": None, "source": "skipped",
                                                                "reason": MERGE_QUEUE_SKIP}))
        print(f"::notice title=Tiden evidence not published::{MERGE_QUEUE_SKIP}; main CI publishes it after merge")
        return 0
    if args.command == "assemble":
        ci_url = "https://github.com/{}/actions/runs/{}/attempts/{}".format(*(
            os.environ.get(name, "") for name in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")))
        producers = {job: (result, sha) for job, result, sha in (value.split(":", 2) for value in args.producer)}
        merged = assemble(directory, args.fragments, ci_url, producers, source_root=args.source_root)
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
    if args.command == "verdict":
        # The evidence job's own verdict, so the gate never depends on the Tiden round trip.
        failures = evidence_failures(json.loads((directory / "manifest.json").read_text()))
        print(f"The evidence records {failures}; see ci-checks.json." if failures else "Every test and check passed.")
        return 1 if failures else 0
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
    if result["status"] == "passed" and os.environ.get("GITHUB_OUTPUT"):
        # Only a verified passed run counts as published; a skip published nothing (ci.yml release record).
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write("verified=true\n")
    if result["status"] == "failed" and result.get("seqNum") is not None:
        # Advisory, after the result and receipts are saved: the step's result stays the publication's.
        lines = triage(directory, result["seqNum"], os.environ["TIDEN_PRODUCT_ID"], started)
        try:
            if os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
                    summary.write("".join(line + "\n" for line in lines))
        except OSError:
            print("Tiden triage: summary not written")
    return 0 if result["status"] in {"passed", "skipped"} else 1


def release(args: argparse.Namespace, directory: Path) -> int:
    if args.environment != "github-actions-synthetic":
        return record(args, directory, None)

    def expire(signum: int, frame: object) -> None:
        # Fatal, like the hang it ends; a receipt already written is current.
        if not (directory / RELEASE_RECEIPT).exists():
            (directory / RELEASE_RECEIPT).write_text(json.dumps({"state": "not_attempted"}, indent=2))
        raise RecordDeadline(f"the CI release record passed its {CI_RELEASE_DEADLINE}-second deadline")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, CI_RELEASE_DEADLINE)
    try:
        return record(args, directory, time.monotonic() + CI_RELEASE_DEADLINE - CI_RECORD_MARGIN)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def record(args: argparse.Namespace, directory: Path, deadline: float | None) -> int:
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
    product = os.environ.get("TIDEN_PRODUCT_ID", "")
    if not product:
        raise ValueError("TIDEN_PRODUCT_ID is required to record a release")
    try:
        receipt = record_release(directory, sha, args.environment, url, released_at, product,
                                 deadline=deadline)
    except TidenUnavailable as exc:
        if args.environment == "production":
            raise
        # Temporary (ANYM-411): until Tiden health is green, an unavailable Tiden only warns; the receipt keeps
        # whether a create call was sent.
        print(f"::warning title=Tiden release not recorded::{exc} (receipt state {exc.state}); until Tiden health "
              "is green a Tiden error in the CI release record only warns (ANYM-411, TIDEN-CI.md)")
        return 0
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
