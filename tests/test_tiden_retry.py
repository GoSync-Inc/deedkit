"""A read-only Tiden listing that fails is repeated once; a write is never repeated."""
import importlib.util
import json
from pathlib import Path
import subprocess
import time

import pytest

import scripts.tiden_drift as drift
import scripts.tiden_health as health
import scripts.tiden_sync as tiden_sync

ROOT = Path(__file__).resolve().parents[1]
snapshot_spec = importlib.util.spec_from_file_location("snapshot_retry", ROOT / "scripts/tiden_snapshot.py")
snapshot = importlib.util.module_from_spec(snapshot_spec)
snapshot_spec.loader.exec_module(snapshot)

GOOD = json.dumps({"ok": True})
FAILURES = ["exit", "garbage", "timeout"]


class Cli:
    """A fake `subprocess.run` for the Tiden CLI: the first calls follow `script` ('exit', 'garbage', 'timeout' or
    'ok'), later calls succeed; every command line is logged."""

    def __init__(self, *script):
        self.script, self.calls, self.timeouts = list(script), [], []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        self.timeouts.append(kwargs.get("timeout"))
        step = self.script.pop(0) if self.script else "ok"
        if step == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        if step == "exit":
            return subprocess.CompletedProcess(command, 3, "tfy_secret", "tfy_secret")
        return subprocess.CompletedProcess(command, 0, "not json" if step == "garbage" else GOOD, "")


def drift_read():
    return drift.tiden("tiden", ["spec", "list"], "product", retry=True)


def sync_read():
    return tiden_sync.read("tiden", ["test", "links", "t1"])


def health_read():
    return health.Tiden("tiden", "product", "workspace").call("spec", "list")


def snapshot_read():
    return snapshot.CLI("unused", "product", "workspace", "main").call("spec", "list")


READS = {"drift": (drift_read, drift.Refused), "sync": (sync_read, tiden_sync.Refused),
         "health": (health_read, health.Unknown), "snapshot": (snapshot_read, snapshot.SnapshotError)}


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    for module in (drift, tiden_sync, health, snapshot):
        monkeypatch.setattr(module, "RETRY_DELAY", 0, raising=False)


# spec: ANYM-355, ANYM-360, ANYM-202, ANYM-405
@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("reader", ["drift", "sync", "health", "snapshot"])
def test_a_read_that_fails_once_succeeds_on_the_one_retry(monkeypatch, reader, failure):
    fake = Cli(failure)
    monkeypatch.setattr(subprocess, "run", fake)
    function, _ = READS[reader]
    assert function() == {"ok": True}
    assert len(fake.calls) == 2 and fake.calls[0] == fake.calls[1]


# spec: ANYM-355, ANYM-360, ANYM-202, ANYM-405
@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("reader", ["drift", "sync", "health", "snapshot"])
def test_a_read_that_fails_twice_fails_after_exactly_two_calls(monkeypatch, reader, failure):
    fake = Cli(failure, failure)
    monkeypatch.setattr(subprocess, "run", fake)
    function, error = READS[reader]
    expected = (error, subprocess.TimeoutExpired) if reader == "snapshot" and failure == "timeout" else error
    with pytest.raises(expected) as caught:
        function()
    assert len(fake.calls) == 2
    assert "tfy_secret" not in str(caught.value)


# spec: ANYM-355
def test_a_healthy_read_makes_one_call(monkeypatch):
    fake = Cli()
    monkeypatch.setattr(subprocess, "run", fake)
    assert drift_read() == {"ok": True} and len(fake.calls) == 1


# spec: ANYM-355
@pytest.mark.parametrize("failure", FAILURES)
def test_a_write_is_never_repeated(monkeypatch, failure):
    fake = Cli(failure, "ok")
    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(drift.Refused):
        drift.tiden("tiden", ["drift", "advance", "--from", "a", "--to", "b"], "product")
    assert len(fake.calls) == 1


# spec: ANYM-405
@pytest.mark.parametrize("failure", ["exit", "timeout"])  # a write's output is not read, so garbage is no failure
def test_a_sync_write_is_never_repeated(monkeypatch, failure):
    fake = Cli(failure, "ok")
    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(tiden_sync.Refused):
        tiden_sync.write("tiden", ["branch", "merge", "--name", "x"], "product")
    assert len(fake.calls) == 1


# spec: ANYM-355
def test_the_retry_waits_the_configured_delay_once(monkeypatch):
    sleeps = []
    monkeypatch.setattr(drift.time, "sleep", sleeps.append)
    monkeypatch.setattr(drift, "RETRY_DELAY", 7)
    monkeypatch.setattr(subprocess, "run", Cli("exit", "exit"))
    with pytest.raises(drift.Refused):
        drift_read()
    assert sleeps == [7]


BUDGET = {"drift": 120, "sync": 120, "health": 120, "snapshot": 90}
MODULES = {"drift": drift, "sync": tiden_sync, "health": health, "snapshot": snapshot}


def clock(monkeypatch, *ticks):
    """time.monotonic reads `ticks` in turn, then stays on the last one."""
    remaining = list(ticks)
    monkeypatch.setattr(time, "monotonic", lambda: remaining.pop(0) if len(remaining) > 1 else remaining[0])


# spec: ANYM-355, ANYM-360, ANYM-202, ANYM-405
@pytest.mark.parametrize("reader", ["drift", "sync", "health", "snapshot"])
def test_the_retry_sleeps_between_the_two_calls_and_runs_inside_the_one_budget(monkeypatch, reader):
    events = []
    fake = Cli("exit", "ok")

    def run(command, **kwargs):
        events.append("call")
        return fake(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(time, "sleep", lambda seconds: events.append(("sleep", seconds)))
    monkeypatch.setattr(MODULES[reader], "RETRY_DELAY", 7)
    clock(monkeypatch, 0, 30)
    function, _ = READS[reader]
    assert function() == {"ok": True}
    assert events == ["call", ("sleep", 7), "call"]
    assert fake.timeouts == [BUDGET[reader], BUDGET[reader] - 30 - 7]


# spec: ANYM-355, ANYM-360, ANYM-202, ANYM-405
@pytest.mark.parametrize("reader", ["drift", "sync", "health", "snapshot"])
def test_a_first_attempt_that_used_the_budget_is_not_retried(monkeypatch, reader):
    fake = Cli("timeout", "ok")
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(time, "sleep", lambda seconds: pytest.fail("slept"))
    clock(monkeypatch, 0, BUDGET[reader] - 1)
    function, error = READS[reader]
    with pytest.raises((error, subprocess.TimeoutExpired)):
        function()
    assert len(fake.calls) == 1
