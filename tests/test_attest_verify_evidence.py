"""The attestation verifier's own Go tests reach Tiden one test at a time, as the other Go modules' do.

`tools/attest-verify` ran its 34 Go tests only inside one root Python test (tests/test_attest_verify.py, kept as it
is), so Tiden saw one pass or fail for the whole suite. The `go` job now writes its `go test -json` stream as module
check `deedkit-ci::attest_verify`. Synthetic streams name the module's real tests; nothing calls Tiden or a registry.
"""
import json
from pathlib import Path
import re

import pytest
import yaml

from scripts.tiden_ci import CHECKS, FRAGMENTS, GO_MODULES, MODULE_REPORTS, go_results, prepare
from scripts.tiden_health import suite

ROOT = Path(__file__).resolve().parents[1]
MODULE = "github.com/GoSync-Inc/deedkit/tools/attest-verify"
URL = "https://github.com/example/repo/actions/runs/1/attempts/1"


def declared(test):
    """The `_test.go` file of tools/attest-verify that declares this real top-level test."""
    # reads: tools/attest-verify/main_test.go
    # reads: tools/attest-verify/registry_test.go
    # reads: tools/attest-verify/verify_test.go
    (file,) =[path.name for path in (ROOT / "tools/attest-verify").glob("*_test.go")
               if re.search(rf"^func {test}\(", path.read_text(), re.M)]
    return file


def stream(*events):
    return "".join(json.dumps({"Package": MODULE, **event}) + "\n" for event in events)


def rows_of(tmp_path, text):
    path = tmp_path / "attest-verify.json"
    path.write_text(text)
    return go_results(path, ROOT, "tools/attest-verify", URL, "attest_verify")


# spec: ANYM-618
def test_the_verifier_is_a_go_module_report_of_the_go_job():
    assert MODULE_REPORTS["attest_verify"] == ("attest-verify.json", "go", "tools/attest-verify")
    assert "attest_verify" in GO_MODULES and "attest_verify" in FRAGMENTS["go"][0]
    assert "attest-verify.json" in FRAGMENTS["go"][1]


# spec: ANYM-618
def test_the_go_job_writes_the_verifier_stream_and_records_its_outcome():
    go = yaml.safe_load((ROOT / ".github/workflows/checks.yml").read_text())["jobs"]["go"]
    steps = go["steps"]
    setup = next(step for step in steps if step.get("uses", "").startswith("actions/setup-go"))
    assert "tools/attest-verify/go.sum" in setup["with"]["cache-dependency-path"].split()
    step = next(step for step in steps if step.get("id") == "attest_verify")
    assert step["working-directory"] == "tools/attest-verify" and step["if"] == "${{ !cancelled() }}"
    assert step["run"] == 'go test -race -count=1 -json ./... > "$RUNNER_TEMP/ci-evidence/attest-verify.json"'
    named = next(item for item in steps if "attest-verify.json" in item.get("run", "") and "go_failures.py" in item["run"])
    assert named["if"] == "${{ !cancelled() && steps.attest_verify.outcome == 'failure' }}"
    outcome = "${{ steps.attest_verify.outcome }}"
    reporting = next(item for item in steps if item.get("id") == "python_reporting")
    assert reporting["env"]["ATTEST_VERIFY_OUTCOME"] == outcome
    assert steps.index(step) < steps.index(reporting)
    record = next(item for item in steps if item.get("name") == "Record the Go outcomes")
    assert record["env"]["ATTEST_VERIFY_OUTCOME"] == outcome
    assert '"attest_verify": os.environ["ATTEST_VERIFY_OUTCOME"]' in record["run"]
    assert "permissions" not in go and "secrets" not in json.dumps(step)


# spec: ANYM-618
def test_each_verifier_test_and_subtest_is_its_own_row_under_its_canonical_identity(tmp_path):
    rows, counts, unexplained = rows_of(tmp_path, stream(
        {"Action": "run", "Test": "TestIndexIsRefused"}, {"Action": "pass", "Test": "TestIndexIsRefused", "Elapsed": 0.01},
        {"Action": "run", "Test": "TestUsageErrorsExitTwoWithoutOutput"},
        {"Action": "run", "Test": "TestUsageErrorsExitTwoWithoutOutput/missing-sha"},
        {"Action": "pass", "Test": "TestUsageErrorsExitTwoWithoutOutput/missing-sha", "Elapsed": 0.01},
        {"Action": "pass", "Test": "TestUsageErrorsExitTwoWithoutOutput", "Elapsed": 0.02},
        {"Action": "run", "Test": "TestRealBundleIsAcceptedForItsCommitAndDigest"},
        {"Action": "fail", "Test": "TestRealBundleIsAcceptedForItsCommitAndDigest", "Elapsed": 0.03},
        {"Action": "fail", "Elapsed": 0.1}))
    found = {row["signature"]: (row["execution"]["status"], row["fields"]["file_path"], row["suitePath"]) for row in rows}
    assert found == {
        f"{MODULE}::TestIndexIsRefused": ("passed", declared("TestIndexIsRefused"), [{"title": "attest-verify"}]),
        f"{MODULE}::TestUsageErrorsExitTwoWithoutOutput/missing-sha":
            ("passed", declared("TestUsageErrorsExitTwoWithoutOutput"), [{"title": "attest-verify"}]),
        f"{MODULE}::TestRealBundleIsAcceptedForItsCommitAndDigest":
            ("failed", declared("TestRealBundleIsAcceptedForItsCommitAndDigest"), [{"title": "attest-verify"}]),
    }
    assert counts == {"passed": 2, "failed": 1} and not unexplained


# spec: ANYM-618
def test_a_failed_or_missing_verifier_stream_never_passes_its_module_check(tmp_path):
    meta = {"sha": "a" * 40, "git_branch": "codex/feature", "ci_url": URL,
            "checks": dict.fromkeys(CHECKS, "success"), "module_checks": dict.fromkeys(MODULE_REPORTS, "success")}
    prepare(ROOT, tmp_path, meta)
    rows = {row["signature"]: row for row in json.loads((tmp_path / "ci-checks.json").read_text())}
    assert rows["deedkit-ci::attest_verify"]["execution"]["status"] == "blocked"
    (tmp_path / "attest-verify.json").write_text(stream(
        {"Action": "run", "Test": "TestIndexIsRefused"}, {"Action": "fail", "Test": "TestIndexIsRefused", "Elapsed": 0.01},
        {"Action": "fail", "Elapsed": 0.1}))
    prepare(ROOT, tmp_path, meta)
    rows = {row["signature"]: row for row in json.loads((tmp_path / "ci-checks.json").read_text())}
    assert rows["deedkit-ci::attest_verify"]["execution"]["status"] == "failed"


# spec: ANYM-618
@pytest.mark.parametrize("case", [{"filePath": "main_test.go", "externalId": f"s:{MODULE}::TestIndexIsRefused"},
                                  {"filePath": "tools/attest-verify/main_test.go", "externalId": ""}])
def test_tiden_health_places_the_verifier_cases_in_their_own_suite(case):
    assert suite(case) == "attest-verify"


# spec: ANYM-618
def test_reuse_fixtures_name_a_declared_verifier_test_and_keep_the_frozen_go_identities():
    import subprocess
    from tests.test_ci_reuse import GO_IDENTITIES, go_test
    frozen = "tests/fixtures/tiden-go-identities.json"
    committed = subprocess.run(["git", "show", f"HEAD:{frozen}"], cwd=ROOT, capture_output=True, check=True).stdout
    assert (ROOT / frozen).read_bytes() == committed  # no identity was added for the verifier
    assert not [item for item in GO_IDENTITIES if item["signature"].startswith(MODULE + "::")]
    module, name = go_test("attest_verify")
    assert module == MODULE and name.startswith("Test") and declared(name)  # a real declaration in its own source
    for check in ("whatsmeow", "release_router"):  # the original modules keep their frozen identities
        package, test = go_test(check)
        assert any(item["signature"] == f"{package}::{test}" for item in GO_IDENTITIES)
