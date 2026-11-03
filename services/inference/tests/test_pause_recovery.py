"""ANYM-691 criterion 5: a stopped engine group left by a dead service is continued and killed by `recover` and at start."""
import contextlib
import json
import os
import signal
import subprocess
import sys
import time

import pytest
from deedkit_inference import pausestate
from support import ServerProc

GROUP_CODE = "import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\ntime.sleep(600)"


def members_of(pgid):
    return [row for row in pausestate.process_table() if row["pgid"] == pgid and not row["stat"].startswith("Z")]


def wait_for(condition, seconds=15.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def stopped_group(tmp_path):
    """A real stopped process group named by a ledger in tmp_path/state, as a dead service would leave it."""
    leader = subprocess.Popen([sys.executable, "-c", GROUP_CODE], start_new_session=True)
    assert wait_for(lambda: len(members_of(leader.pid)) == 2)
    ledger = pausestate.Ledger(tmp_path / "state" / pausestate.LEDGER_NAME, instance="test")
    started = pausestate.start_text(leader.pid)
    assert started
    ledger.record(leader.pid, leader.pid, started)
    os.killpg(leader.pid, signal.SIGSTOP)
    assert wait_for(lambda: all(row["stat"].startswith("T") for row in members_of(leader.pid)))
    yield leader, ledger
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(leader.pid, signal.SIGKILL)
    leader.wait()


def run_recover(state_dir, *flags):
    return subprocess.run([sys.executable, "-m", "deedkit_inference", "recover", "--state-dir", str(state_dir), *flags],
                          capture_output=True, text=True, timeout=60)


# spec: ANYM-691
def test_recover_command_check_reads_only_and_recover_kills_the_ledgered_group(stopped_group):
    leader, ledger = stopped_group
    state_dir = ledger.path.parent
    checked = run_recover(state_dir, "--check")
    assert checked.returncode == 0 and json.loads(checked.stdout) == {"check": "ok", "groups": 1}
    assert len(members_of(leader.pid)) == 2 and ledger.path.exists()  # --check sends no signal and keeps the ledger
    done = run_recover(state_dir)
    assert done.returncode == 0 and json.loads(done.stdout)["groups"] == 1
    leader.wait(timeout=10)  # the group leader died by SIGKILL
    assert wait_for(lambda: members_of(leader.pid) == []) and not ledger.path.exists()
    assert json.loads(run_recover(state_dir).stdout) == {"groups": 0, "killed": [], "kept": []}  # nothing to do is success


# spec: ANYM-691
def test_recover_command_fails_closed_on_a_malformed_ledger(tmp_path):
    (tmp_path / pausestate.LEDGER_NAME).write_text("not json")
    for flags in (("--check",), ()):
        result = run_recover(tmp_path, *flags)
        assert result.returncode == 4 and "malformed" in result.stderr


# spec: ANYM-691
def test_service_start_recovers_a_stopped_group_of_the_previous_instance(env, stopped_group):
    leader, ledger = stopped_group
    server = ServerProc(env, usage_file=str(ledger.path.parent / "usage.json"))
    server.start()
    try:
        leader.wait(timeout=10)
        assert wait_for(lambda: members_of(leader.pid) == []) and not ledger.path.exists()
        assert "startup recovery of stopped engine groups" in (env.tmp / "server.log").read_text()
    finally:
        server.stop()


# spec: ANYM-691
def test_service_start_is_not_blocked_by_a_malformed_ledger(env, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / pausestate.LEDGER_NAME).write_text("{")
    server = ServerProc(env, usage_file=str(state / "usage.json"))
    server.start()  # raises when the server does not answer /health
    try:
        assert "startup recovery failed" in (env.tmp / "server.log").read_text()
    finally:
        server.stop()
