import json
import time

from deedkit_agent_runner.runner import Runner
from deedkit_agent_runner.store import TERMINAL

from support import Gate, fields, make_runner, run_task


def restart(old):
    """A new runner process over the same state directory and the same (surviving) VM containers."""
    fresh = Runner(old.config, old.docker, Gate(), clock=old.clock, publisher=old.publisher)
    fresh.startup()
    return fresh


def wait_running(runner, task_id):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        task = runner.store.task(task_id)
        if task["state"] == "running" and runner.docker.running(task["container"]) == "running":
            return task
        time.sleep(0.02)
    raise AssertionError("never running")


def audit_states(runner, task_id):
    lines = (runner.state_dir / "audit.jsonl").read_text().splitlines()
    return [json.loads(line)["state"] for line in lines if json.loads(line)["task_id"] == task_id]


# spec: ANYM-819
def test_a_task_moves_queued_starting_running_to_a_terminal_state_that_never_changes(runner):
    task = run_task(runner)
    assert task["state"] == "succeeded" and task["container"] == f"agent-task-{task['id']}"
    assert audit_states(runner, task["id"]) == ["queued", "starting", "running", "succeeded"]
    for state in TERMINAL:
        assert runner.store.finish(task["id"], state, "again") is False
    assert runner.store.task(task["id"])["state"] == "succeeded"
    assert runner.stop(task["id"])["state"] == "succeeded"


# spec: ANYM-819
def test_the_container_name_is_recorded_before_the_container_exists(runner):
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    seen = []
    runner.docker.on_create = lambda name: seen.append(runner.store.task(task["id"]))
    runner.tick()
    assert runner.join(30)
    assert seen[0]["state"] == "starting" and seen[0]["container"] == f"agent-task-{task['id']}"


# spec: ANYM-819
def test_a_crash_after_insert_leaves_the_task_queued_and_it_runs_once(runner):
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    fresh = restart(runner)
    assert fresh.store.task(task["id"])["state"] == "queued"
    fresh.tick()
    assert fresh.join(30)
    assert fresh.store.task(task["id"])["state"] == "succeeded"
    assert fresh.docker.runs() == [task["id"]]
    assert fresh.docker.labelled() == []


# spec: ANYM-819
def test_a_crash_after_starting_marks_the_task_lost_and_never_runs_it(runner):
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    assert runner.store.claim(task["id"], f"agent-task-{task['id']}")  # the process dies here
    fresh = restart(runner)
    row = fresh.store.task(task["id"])
    assert (row["state"], row["reason"]) == ("lost", "runner_restarted")
    fresh.tick()
    assert fresh.join(30)
    assert fresh.docker.runs() == [] and fresh.docker.labelled() == []


# spec: ANYM-819
def test_a_crash_after_create_before_start_kills_the_labelled_container(runner):
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    assert runner.store.claim(task["id"], f"agent-task-{task['id']}")
    from deedkit_agent_runner import docker as dk
    runner.docker.create(dk.task_create_argv(runner.config, task["id"], "claude"), "claude")
    fresh = restart(runner)
    assert fresh.store.task(task["id"])["state"] == "lost"
    assert fresh.docker.labelled() == [] and fresh.docker.runs() == []


# spec: ANYM-819
def test_a_crash_while_running_kills_the_container_marks_lost_and_runs_nothing_twice(tmp_path):
    runner = make_runner(tmp_path, modes={"claude": "sleep"})
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    queued, _ = runner.submit("orchestrator-a", "a-run", fields())
    runner.tick()
    running = wait_running(runner, task["id"])
    process = runner.docker.containers[running["container"]].proc
    fresh = restart(runner)  # the old process is gone; its containers are not
    assert process.poll() is not None
    row = fresh.store.task(task["id"])
    assert row["state"] == "lost"
    assert fresh.docker.labelled() == []
    assert fresh.store.task(queued["id"])["state"] == "queued"
    runner.join(30)  # the old monitor thread finds the stream closed; the lost state stays
    assert fresh.store.task(task["id"])["state"] == "lost"
    fresh.docker.modes["claude"] = "claude_ok"
    fresh.tick()
    assert fresh.join(30)
    assert fresh.docker.runs() == [task["id"], queued["id"]]
    assert fresh.store.task(queued["id"])["state"] == "succeeded"


# spec: ANYM-819
def test_lost_tasks_keep_their_volumes_for_the_export(tmp_path):
    runner = make_runner(tmp_path, modes={"claude": "sleep"})
    runner.publisher = lambda task: {"publish_state": "pending"}  # S4 has not exported yet
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    runner.tick()
    wait_running(runner, task["id"])
    fresh = restart(runner)
    runner.join(30)
    assert fresh.store.task(task["id"])["state"] == "lost"
    assert f"agent-ws-{task['id']}" in fresh.docker.volumes and f"agent-out-{task['id']}" in fresh.docker.volumes


# spec: ANYM-819
def test_an_engine_that_exits_nonzero_fails_and_a_create_error_fails_without_a_run(tmp_path):
    runner = make_runner(tmp_path, modes={"claude": "fail"})
    task = run_task(runner)
    assert (task["state"], task["reason"], task["exit_code"]) == ("failed", "engine_exit", 3)
    runner.docker.fail_create = "no such image"
    task = run_task(runner)
    assert (task["state"], task["reason"]) == ("failed", "container_failed")
    assert len(runner.docker.runs()) == 1
