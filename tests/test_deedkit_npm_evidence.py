"""ANYM-261: the DeedKit JavaScript client's own node:test run reaches Tiden as a module report under its own files.

The native Node JUnit reporter runs for real on the package and on synthetic package files; nothing here calls Tiden.
"""
from collections import Counter
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest
import yaml

from scripts.tiden_ci import CHECKS, FRAGMENTS, MODULE_REPORTS, normalize_junit, prepare

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "packages/deedkit"
TEST_CALL = re.compile(r"""^\s*test\(\s*(['"])(.+?)(?<!\\)\1""", re.M)


def node_junit(directory):
    """The native reporter's JUnit for every `test/*.test.js` of `directory`, as the CI step runs it."""
    node = shutil.which("node")
    if not node:
        pytest.fail("node is required: the DeedKit package tests run under node --test")
    files = sorted(str(p.relative_to(directory)) for p in (directory / "test").glob("*.test.js"))
    run = subprocess.run([node, "--test", "--test-reporter=junit", *files], cwd=directory, capture_output=True, text=True,
                         timeout=300)
    return run.returncode, run.stdout


def declared(directory):
    """Every `test('<title>', ...)` of the package's test files, as (repository path, title)."""
    return Counter((f"{PACKAGE}/test/{p.name}", m.group(2).replace("\\'", "'"))
                   for p in sorted((directory / "test").glob("*.test.js")) for m in TEST_CALL.finditer(p.read_text()))


def module_row(root, directory, xml, outcome):
    """The deedkit_npm CI row and normalized cases `prepare` records for this report (checkout `root`) and step outcome."""
    (directory / "deedkit-npm.xml").write_text(xml)
    meta = {"sha": "a" * 40, "git_branch": "codex/feature", "ci_url": "https://github.com/example/repo/actions/runs/1",
            "checks": dict.fromkeys(CHECKS, "success"), "module_checks": {**dict.fromkeys(MODULE_REPORTS, "success"),
                                                                         "deedkit_npm": outcome}}
    prepare(root, directory, meta)
    rows = {row["signature"]: row for row in json.loads((directory / "ci-checks.json").read_text())}
    cases = list(ET.fromstring((directory / "normalized-deedkit_npm.xml").read_bytes()).iter("testcase"))
    return rows["deedkit-ci::deedkit_npm"], cases


def status(case):
    return "failed" if case.find("failure") is not None else "skipped" if case.find("skipped") is not None else "passed"


# spec: ANYM-261
def test_the_deedkit_step_writes_the_native_node_junit_into_the_verify_evidence():
    assert MODULE_REPORTS["deedkit_npm"] == ("deedkit-npm.xml", "node", PACKAGE)
    assert "deedkit_npm" in FRAGMENTS["verify"][0] and "deedkit-npm.xml" in FRAGMENTS["verify"][1]
    steps = yaml.safe_load((ROOT / ".github/workflows/checks.yml").read_text())["jobs"]["verify"]["steps"]
    step = next(step for step in steps if step.get("id") == "deedkit_npm")
    assert step["working-directory"] == PACKAGE and step["if"] == "${{ !cancelled() }}"
    # The same test set as the package's own `npm test`, through the native reporter, with no filter.
    assert json.loads((ROOT / PACKAGE / "package.json").read_text())["scripts"]["test"] == "node --test test/*.test.js"
    assert step["run"].strip() == 'node --test --test-reporter=junit test/*.test.js > "$RUNNER_TEMP/ci-evidence/deedkit-npm.xml"'
    evidence = next(step for step in steps if step.get("name") == "Prepare honest CI evidence")
    assert json.loads(evidence["env"]["MODULE_OUTCOMES"])["deedkit_npm"] == "${{ steps.deedkit_npm.outcome }}"


# spec: ANYM-261
def test_the_real_package_run_reports_each_test_once_under_its_own_file_and_title(tmp_path):
    code, xml = node_junit(ROOT / PACKAGE)
    assert code == 0, xml[-2000:]
    report = tmp_path / "deedkit-npm.xml"
    report.write_text(xml)
    normalized, counts = normalize_junit(report, ROOT, "node", PACKAGE)
    cases = list(ET.fromstring(normalized).iter("testcase"))
    assert Counter((case.get("file"), case.get("name")) for case in cases) == declared(ROOT / PACKAGE)
    assert all(case.get("classname") == "node:" + case.get("file") for case in cases)
    # The one platform test keeps its own skip off Windows; nothing else is skipped or failed.
    skipped = {case.get("name") for case in cases if status(case) == "skipped"}
    assert skipped == ({"a Windows alternate-stream output name is refused"} if sys.platform != "win32" else set())
    assert counts == Counter(passed=len(cases) - len(skipped), skipped=len(skipped))
    row, reported = module_row(ROOT, tmp_path, xml, "success")
    assert row["execution"]["status"] == "passed"
    assert [(case.get("file"), case.get("name")) for case in reported] == [(case.get("file"), case.get("name")) for case in cases]


# spec: ANYM-261
@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_a_failing_package_test_fails_the_module_check_and_keeps_its_failure(tmp_path, outcome):
    package = tmp_path / PACKAGE
    (package / "test").mkdir(parents=True)
    (package / "test/a.test.js").write_text("import test from 'node:test';\nimport assert from 'node:assert';\n"
                                            "test('a passing case', () => {});\n"
                                            "test('a failing case', () => { assert.equal(1, 2); });\n"
                                            "test('a platform case', {skip: true}, () => {});\n")
    code, xml = node_junit(package)
    assert code != 0
    row, cases = module_row(tmp_path, tmp_path, xml, outcome)
    assert row["execution"]["status"] == "failed"
    assert {case.get("name"): status(case) for case in cases} == {"a passing case": "passed", "a failing case": "failed",
                                                                   "a platform case": "skipped"}
    assert {case.get("file") for case in cases} == {f"{PACKAGE}/test/a.test.js"}


# spec: ANYM-261
def test_a_package_run_with_only_skipped_tests_is_no_verification(tmp_path):
    package = tmp_path / PACKAGE
    (package / "test").mkdir(parents=True)
    (package / "test/a.test.js").write_text("import test from 'node:test';\ntest('a platform case', {skip: true}, () => {});\n")
    code, xml = node_junit(package)
    assert code == 0
    row, cases = module_row(tmp_path, tmp_path, xml, "success")
    assert [status(case) for case in cases] == ["skipped"]
    assert row["execution"]["status"] == "blocked" and row["message"] == "No tests executed; only skipped tests"


# spec: ANYM-261
def test_a_missing_package_report_blocks_the_module_check(tmp_path):
    meta = {"sha": "a" * 40, "git_branch": "codex/feature", "ci_url": "https://github.com/example/repo/actions/runs/1",
            "checks": dict.fromkeys(CHECKS, "success"), "module_checks": dict.fromkeys(MODULE_REPORTS, "success")}
    prepare(tmp_path, tmp_path, meta)
    rows = {row["signature"]: row for row in json.loads((tmp_path / "ci-checks.json").read_text())}
    assert rows["deedkit-ci::deedkit_npm"]["execution"]["status"] == "blocked"
    assert rows["deedkit-ci::deedkit_npm"]["message"] == "Missing or invalid test report; verification incomplete"
