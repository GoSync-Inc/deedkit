import json
import os
import time

import pytest

from deedkit_agent_runner import config as cfg
from deedkit_agent_runner.journal import LOG_LIMIT, MARKER
from deedkit_agent_runner.store import Refused

from support import Clock, Gate, fields, make_runner, run_task, write_config


def wait_state(runner, task_id, *states):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        task = runner.store.task(task_id)
        if task["state"] in states and (task["state"] != "running" or
                                        runner.docker.running(task["container"]) == "running"):
            return task
        time.sleep(0.02)
    raise AssertionError(runner.store.task(task_id)["state"])


def stop_everything(runner):
    for container in list(runner.docker.containers.values()):
        if container.proc and container.proc.poll() is None:
            runner.docker.kill(container.name)
    runner.join(30)


# spec: ANYM-819
def test_one_running_task_per_engine_two_in_total(tmp_path):
    runner = make_runner(tmp_path, modes={"claude": "sleep", "codex": "sleep"})
    a, _ = runner.submit("orchestrator-a", "a-run", fields("claude"))
    b, _ = runner.submit("orchestrator-a", "a-run", fields("claude"))
    c, _ = runner.submit("orchestrator-a", "a-run", fields("codex"))
    runner.tick()
    wait_state(runner, a["id"], "running")
    wait_state(runner, c["id"], "running")
    runner.tick()
    runner.tick()
    assert runner.store.task(b["id"])["state"] == "queued"
    assert runner.store.counts("claude") == {"queued": 1, "running": 1}
    assert len(runner.docker.starts) == 2
    stop_everything(runner)


# spec: ANYM-819
def test_a_third_queued_task_of_an_engine_gets_runner_busy(runner):
    for _ in range(2):
        runner.submit("orchestrator-a", "a-run", fields("claude"))
    with pytest.raises(Refused) as busy:
        runner.submit("orchestrator-a", "a-run", fields("claude"))
    assert (busy.value.status, busy.value.code) == (429, "runner_busy")
    assert busy.value.extra["retry_after_s"] >= 30
    task, created = runner.submit("orchestrator-a", "a-run", fields("codex"))  # the other engine has room
    assert created


# spec: ANYM-819
def test_the_daily_cap_counts_starts_in_a_rolling_24_hours(tmp_path):
    clock = Clock()
    runner = make_runner(tmp_path, clock=clock, daily_starts=3)
    for _ in range(3):
        run_task(runner)
        clock.advance(3600)
    with pytest.raises(Refused) as capped:
        runner.submit("orchestrator-a", "a-run", fields())
    assert (capped.value.status, capped.value.code) == (429, "daily_cap_reached")
    first = runner.store.rows("succeeded")[0]
    assert capped.value.extra["next_slot_at"] == first["created_at"] + 86400
    clock.now = first["created_at"] + 86400 + 1
    task, created = runner.submit("orchestrator-a", "a-run", fields())
    assert created


# spec: ANYM-819
def test_a_start_that_ends_login_required_does_not_count(tmp_path):
    runner = make_runner(tmp_path, modes={"claude": "login"}, daily_starts=1)
    task = run_task(runner)
    assert (task["state"], task["reason"], task["counts_toward_cap"]) == ("failed", "login_required", 0)
    runner.docker.modes["claude"] = "claude_ok"
    assert run_task(runner)["state"] == "succeeded"
    with pytest.raises(Refused, match="daily_cap_reached"):
        runner.submit("orchestrator-a", "a-run", fields())


# spec: ANYM-819
def test_a_missing_login_file_ends_login_required_and_does_not_count(tmp_path):
    runner = make_runner(tmp_path, daily_starts=1)
    (tmp_path / "claude-token").unlink()
    task = run_task(runner)
    assert (task["state"], task["reason"], task["counts_toward_cap"]) == ("failed", "login_required", 0)
    assert runner.docker.runs() == []


# spec: ANYM-819
@pytest.mark.parametrize("value", [0, 21])
def test_the_daily_cap_is_configurable_up_to_20(tmp_path, value):
    with pytest.raises(cfg.ConfigError):
        write_config(tmp_path, daily_starts=value)


# spec: ANYM-819
def test_a_busy_host_keeps_the_task_queued_then_fails_it_host_busy_without_a_run(tmp_path):
    clock, gate = Clock(), Gate("busy", "memory")
    runner = make_runner(tmp_path, clock=clock, gate=gate)
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    runner.tick()
    row = runner.store.task(task["id"])
    assert row["state"] == "queued"
    progress = json.loads(row["progress"])
    assert progress["waiting_for"] == "host" and progress["host_reason"] == "memory"
    clock.advance(59 * 60)
    runner.tick()
    assert runner.store.task(task["id"])["state"] == "queued"
    clock.advance(61)
    runner.tick()
    row = runner.store.task(task["id"])
    assert (row["state"], row["reason"]) == ("failed", "host_busy")
    assert runner.docker.creates == [] and runner.docker.runs() == []


# spec: ANYM-819
def test_a_host_that_turns_ok_launches_the_waiting_task(tmp_path):
    gate = Gate("busy", "tests")
    runner = make_runner(tmp_path, gate=gate)
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    runner.tick()
    gate.state = "ok"
    runner.tick()
    assert runner.join(30)
    row = runner.store.task(task["id"])
    assert row["state"] == "succeeded" and "waiting_for" not in json.loads(row["progress"])


# spec: ANYM-819
def test_the_command_gate_reads_busy_on_anything_but_a_clean_ok(tmp_path):
    from deedkit_agent_runner.hostgate import CommandGate
    script = tmp_path / "gate.sh"

    def gate(body, timeout=5.0):
        script.write_text("#!/bin/sh\n" + body + "\n")
        os.chmod(script, 0o700)
        command = cfg.Command(argv=[str(script)], timeout_s=timeout)
        return CommandGate(command, command)

    assert gate('echo \'{"state":"ok"}\'').read() == ("ok", None)
    assert gate('echo \'{"state":"busy","reason":"disk"}\'').read() == ("busy", "disk")
    assert gate('echo \'{"state":"busy","reason":"weird"}\'').read() == ("busy", "unknown")
    assert gate("echo not json").read() == ("busy", "unknown")
    assert gate('echo \'{"state":"ok"}\'; exit 1').read() == ("busy", "unknown")
    assert gate('sleep 5; echo \'{"state":"ok"}\'', timeout=0.3).read() == ("busy", "unknown")
    assert gate('echo \'{"state":"ok"}\'').self_probe() == "ok"
    assert gate("exit 0").self_probe() == "failed"


# spec: ANYM-819
def test_the_log_stops_at_5_mib_with_a_marker(tmp_path):
    runner = make_runner(tmp_path, modes={"claude": "flood"})
    task = run_task(runner)
    path = runner.state_dir / "logs" / f"{task['id']}.jsonl"
    assert path.stat().st_size <= LOG_LIMIT + 200
    last = json.loads(path.read_text().splitlines()[-1])
    assert last["text"] == MARKER and task["state"] == "succeeded"


# spec: ANYM-819
def test_max_minutes_stops_then_kills_and_ends_timed_out(tmp_path):
    clock = Clock()
    runner = make_runner(tmp_path, clock=clock, modes={"claude": "sleep"})
    task, _ = runner.submit("orchestrator-a", "a-run", fields(max_minutes=5))
    runner.tick()
    running = wait_state(runner, task["id"], "running")
    process = runner.docker.containers[running["container"]].proc
    clock.advance(4 * 60)
    runner.tick()
    assert runner.store.task(task["id"])["state"] == "running"
    clock.advance(61)
    runner.tick()
    assert runner.join(30)
    row = runner.store.task(task["id"])
    assert (row["state"], row["reason"]) == ("timed_out", "max_minutes")
    assert process.poll() is not None and running["container"] in runner.docker.stops
    assert runner.docker.labelled() == []


# spec: ANYM-819
def test_stopping_the_container_ends_a_detached_child_too(tmp_path):
    runner = make_runner(tmp_path, modes={"claude": "detached"})
    runner.publisher = lambda task: {"publish_state": "pending"}
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    runner.tick()
    wait_state(runner, task["id"], "running")
    pid_file = runner.docker.volume_dir(f"agent-out-{task['id']}") / "child.pid"
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    child = int(pid_file.read_text())
    runner.stop(task["id"])
    assert runner.join(30)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the detached child survived")
    assert runner.store.task(task["id"])["state"] == "stopped"
