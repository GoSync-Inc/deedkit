"""CI hygiene: osv-scanner findings fail the security run, main push runs are never cancelled, every uv.lock
is checked for staleness and the DeedKit JavaScript package is tested in checks / verify."""
from datetime import datetime
from pathlib import Path
import tomllib

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_CHECKS = ("build", "release", "python", "collector", "npm", "smoke", "distributions")


def _workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def _verify_steps():
    return _workflow("checks.yml")["jobs"]["verify"]["steps"]


# spec: ANYM-141
def test_osv_scanner_fails_the_security_run_except_for_listed_advisories():
    steps = _workflow("security.yml")["jobs"]["report"]["steps"]
    osv = next(step for step in steps if step.get("id") == "osv")
    assert "continue-on-error" not in osv
    assert "osv-scanner scan source --recursive --config osv-scanner.toml" in osv["run"]
    config = tomllib.loads((ROOT / "osv-scanner.toml").read_text())
    assert set(config) <= {"IgnoredVulns"}
    ids = [entry["id"] for entry in config.get("IgnoredVulns", ())]
    assert len(ids) == len(set(ids))
    for entry in config.get("IgnoredVulns", ()):
        # An allowlisted advisory always expires and names the card that fixes it.
        assert set(entry) == {"id", "ignoreUntil", "reason"}, entry
        assert isinstance(entry["ignoreUntil"], datetime), entry
        assert "; card: " in entry["reason"], entry


# spec: ANYM-141
def test_security_runs_cancel_only_an_older_run_of_the_same_pull_request():
    concurrency = _workflow("security.yml")["concurrency"]
    assert concurrency["group"] == "security-${{ github.event.pull_request.number || github.sha }}"
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


# spec: ANYM-141
def test_verify_fails_on_a_stale_uv_lock_and_keeps_the_seven_check_steps():
    steps = _verify_steps()
    ids = [step.get("id") for step in steps]
    assert [step_id for step_id in ids if step_id in CI_CHECKS] == list(CI_CHECKS)
    locks = steps[ids.index("uv_locks")]
    assert "continue-on-error" not in locks and "if" not in locks
    assert "uv lock --check --project \"$project\"" in locks["run"]
    assert "git ls-files -- 'uv.lock' '*/uv.lock'" in locks["run"]
    assert ids.index("uv_locks") < ids.index("build")


# spec: ANYM-261
def test_verify_runs_the_deedkit_package_tests():
    steps = _verify_steps()
    step = next(step for step in steps if step.get("id") == "deedkit_npm")
    assert step["run"] == "npm test --prefix packages/deedkit"
    assert "continue-on-error" not in step
