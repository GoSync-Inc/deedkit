"""CI hygiene: osv-scanner findings fail the security run, main push runs are never cancelled, every uv.lock
is checked for staleness and the DeedKit JavaScript package is tested in checks / verify."""
from datetime import datetime
from pathlib import Path
import subprocess
import tomllib

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_CHECKS = ("build", "release", "python", "collector", "npm", "smoke", "distributions")


def _workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def _verify_steps():
    return _workflow("checks.yml")["jobs"]["verify"]["steps"]


# spec: ANYM-137
def test_osv_scanner_fails_the_security_run_except_for_listed_advisories():
    steps = _workflow("security.yml")["jobs"]["report"]["steps"]
    osv = next(step for step in steps if step.get("id") == "osv")
    assert "continue-on-error" not in osv and "if" not in osv
    assert "osv-scanner scan source --recursive --config osv-scanner.toml" in osv["run"]
    # Every later step still reports after an osv-scanner failure, and only osv-scanner can fail the run.
    for step in steps[steps.index(osv) + 1:]:
        assert step.get("if") in ("${{ !cancelled() }}", "${{ always() }}"), step
        assert step.get("continue-on-error") is True, step
    config = tomllib.loads((ROOT / "osv-scanner.toml").read_text())
    assert set(config) <= {"IgnoredVulns"}
    ids = [entry["id"] for entry in config.get("IgnoredVulns", ())]
    assert len(ids) == len(set(ids))
    for entry in config.get("IgnoredVulns", ()):
        # An allowlisted advisory always expires and names the card that fixes it.
        assert set(entry) == {"id", "ignoreUntil", "reason"}, entry
        assert isinstance(entry["ignoreUntil"], datetime), entry
        assert "; card: " in entry["reason"], entry


# spec: ANYM-500
def test_security_runs_cancel_only_an_older_run_of_the_same_pull_request():
    concurrency = _workflow("security.yml")["concurrency"]
    assert concurrency["group"] == "security-${{ github.event.pull_request.number || github.sha }}"
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def _run_uv_locks(tmp_path, listed, git_status=0, stale=()):
    """Run the uv_locks step's script as GitHub runs it (bash -e -o pipefail) with stub git and uv on PATH."""
    script = next(step for step in _verify_steps() if step.get("id") == "uv_locks")["run"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "git").write_text(f"#!/bin/sh\nprintf '%s' '{listed}'\nexit {git_status}\n")
    # The stub accepts exactly `uv lock --check --project <dir>`: any other invocation (e.g. without --check,
    # which would rewrite a stale lock instead of failing) exits 99 and fails the assertions below.
    (bin_dir / "uv").write_text('#!/bin/sh\n[ "$#" = 4 ] && [ "$1 $2 $3" = "lock --check --project" ] || exit 99\n'
                                'echo "$4" >> "$CHECKED"\ncase " $STALE " in *" $4 "*) exit 1;; esac\n')
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    checked = tmp_path / "checked"
    checked.touch()
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "CHECKED": str(checked), "STALE": " ".join(stale)}
    result = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                            env=env, capture_output=True, text=True, check=False)
    return result.returncode, checked.read_text().split()


# spec: ANYM-137
def test_verify_fails_on_a_stale_uv_lock_and_keeps_the_seven_check_steps(tmp_path):
    steps = _verify_steps()
    ids = [step.get("id") for step in steps]
    assert [step_id for step_id in ids if step_id in CI_CHECKS] == list(CI_CHECKS)
    locks = steps[ids.index("uv_locks")]
    assert "continue-on-error" not in locks and "if" not in locks
    assert ids.index("uv_locks") < ids.index("build")
    listed = "uv.lock\nservices/calls/uv.lock\ntools/dev/uv.lock\n"
    assert _run_uv_locks(tmp_path / "fresh", listed) == (0, [".", "services/calls", "tools/dev"])
    # One stale lock fails the step, and every other lock is still checked.
    assert _run_uv_locks(tmp_path / "stale", listed, stale=["services/calls"]) == (
        1, [".", "services/calls", "tools/dev"])
    # A failed or empty enumeration fails instead of checking nothing.
    assert _run_uv_locks(tmp_path / "git", listed, git_status=128)[0] != 0
    assert _run_uv_locks(tmp_path / "none", "")[0] != 0


# spec: ANYM-261
def test_verify_runs_the_deedkit_package_tests():
    steps = _verify_steps()
    step = next(step for step in steps if step.get("id") == "deedkit_npm")
    assert step["run"] == "npm test --prefix packages/deedkit"
    assert step["if"] == "${{ !cancelled() }}"
    assert "continue-on-error" not in step and "working-directory" not in step


# spec: ANYM-324
def test_every_workflow_lives_where_github_runs_it_and_calendar_runs_in_verify():
    # GitHub runs only the repository root's .github/workflows; a nested copy is dead and misleads readers.
    listed = subprocess.run(["git", "ls-files", "--", "*.github/workflows/*"], cwd=ROOT, capture_output=True,
                            text=True, check=True).stdout.split()
    assert listed and all(path.startswith(".github/workflows/") for path in listed), listed
    calendar = next(step for step in _verify_steps() if step.get("id") == "calendar")
    assert calendar["working-directory"] == "services/calendar" and "uv run pytest" in calendar["run"]


# spec: ANYM-324
def test_verify_lints_the_calendar_with_the_pinned_ruff():
    run = next(step for step in _verify_steps() if step.get("id") == "calendar")["run"].splitlines()
    # The calendar has its own [tool.ruff]; the pinned tools/dev ruff checks it after its tests and build.
    lint = "uv run --project ../../tools/dev --locked ruff check ."
    assert lint in [line.strip() for line in run]
    assert [line.strip() for line in run if line.strip() and not line.strip().startswith("#")][-1] == lint
    assert '"ruff==0.15.7"' in (ROOT / "tools/dev/pyproject.toml").read_text()
