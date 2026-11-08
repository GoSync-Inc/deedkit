import threading
import time

from deedkit_agent_runner.cli import main
from deedkit_agent_runner.runner import stop_all
from deedkit_agent_runner.store import Refused

from support import auth, fields, make_runner


def wait_running(runner, task_id):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        task = runner.store.task(task_id)
        if task["state"] == "running" and runner.docker.running(task["container"]) == "running":
            return task
        time.sleep(0.02)
    raise AssertionError(runner.store.task(task_id)["state"])


def live(runner):
    return [c.name for c in runner.docker.containers.values() if c.proc and c.proc.poll() is None]


# spec: ANYM-819
def test_the_pause_file_refuses_new_starts_with_503(client, runner):
    runner.pause_file.touch()
    value = fields()
    value.pop("continue_task_id")
    response = client.post("/v1/tasks", json=value, headers=auth("a_run"))
    assert response.status_code == 503 and response.json()["error"] == "runner_paused"
    assert runner.store.rows("queued") == []


# spec: ANYM-819
def test_a_pause_racing_admission_admits_nothing_after_the_file_exists(runner):
    admitted, refused = [], []
    stop = threading.Event()

    def post():
        while not stop.is_set():
            try:
                task, _ = runner.submit("orchestrator-a", "a-run", fields("codex"))
                admitted.append((time.monotonic(), task["created_at"]))
            except Refused as refusal:
                refused.append((time.monotonic(), refusal.code))
                if refusal.code == "runner_busy":
                    for task in runner.store.rows("queued"):
                        runner.store.request_stop(task["id"])

    thread = threading.Thread(target=post)
    thread.start()
    time.sleep(0.2)
    runner.pause_file.touch()
    paused_at = time.monotonic()
    time.sleep(0.2)
    stop.set()
    thread.join(10)
    assert admitted and all(at < paused_at + 0.05 for at, _ in admitted)
    assert any(code == "runner_paused" for _, code in refused)


# spec: ANYM-819
def test_a_pause_between_claim_and_launch_starts_nothing(runner):
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    runner.docker.on_create = lambda name: runner.pause_file.touch()  # lands right before the re-check
    runner.tick()
    assert runner.join(30)
    row = runner.store.task(task["id"])
    assert (row["state"], row["reason"]) == ("stopped", "paused")
    assert runner.docker.starts == [] and runner.docker.labelled() == []


# spec: ANYM-819
def test_stop_all_racing_a_starting_task_leaves_no_container_running(runner):
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    runner.docker.on_create = lambda name: stop_all(runner.store, runner.docker, runner.pause_file)
    runner.tick()
    assert runner.join(30)
    assert runner.store.task(task["id"])["state"] == "stopped"
    assert runner.docker.starts == [] and live(runner) == [] and runner.docker.labelled() == []
    assert runner.pause_file.exists()


# spec: ANYM-819
def test_stop_all_after_the_recheck_still_stops_the_started_container(tmp_path):
    runner = make_runner(tmp_path, modes={"claude": "sleep"})
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    original = runner.store.commit_launch

    def recheck_then_stop_all(task_id, paused):
        ok = original(task_id, paused)
        stop_all(runner.store, runner.docker, runner.pause_file)  # between the re-check and `docker start`
        return ok

    runner.store.commit_launch = recheck_then_stop_all
    runner.tick()
    assert runner.join(30)
    assert runner.store.task(task["id"])["state"] == "stopped"
    assert live(runner) == [] and runner.docker.labelled() == []


# spec: ANYM-819
def test_stop_all_stops_every_queued_starting_and_running_task(tmp_path):
    runner = make_runner(tmp_path, modes={"claude": "sleep", "codex": "sleep"})
    running, _ = runner.submit("orchestrator-a", "a-run", fields("claude"))
    queued, _ = runner.submit("orchestrator-a", "a-run", fields("claude"))
    other, _ = runner.submit("orchestrator-a", "a-run", fields("codex"))
    runner.tick()
    wait_running(runner, running["id"])
    wait_running(runner, other["id"])
    stopped = stop_all(runner.store, runner.docker, runner.pause_file)
    assert set(stopped) == {running["id"], queued["id"], other["id"]}
    assert runner.join(30)
    for task in (running, queued, other):
        assert runner.store.task(task["id"])["state"] == "stopped"
    assert live(runner) == []
    runner.tick()
    runner.sweep()
    assert runner.docker.labelled() == []
    assert all(t["publish_state"] != "pending" for t in runner.store.rows("stopped"))


# spec: ANYM-819
def test_the_stop_all_and_resume_commands_use_the_pause_file(runner):
    from deedkit_agent_runner import cli
    original = cli.DockerCLI
    cli.DockerCLI = lambda config: runner.docker
    try:
        task, _ = runner.submit("orchestrator-a", "a-run", fields())
        assert main(["stop-all", "--config", str(runner.config_path)]) == 0
        assert runner.pause_file.exists() and runner.store.task(task["id"])["state"] == "stopped"
        assert main(["resume", "--config", str(runner.config_path)]) == 0
        assert not runner.pause_file.exists()
    finally:
        cli.DockerCLI = original


# spec: ANYM-819
def test_stop_a_queued_or_starting_task_ends_it_without_a_run(runner, client):
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    response = client.post(f"/v1/tasks/{task['id']}/stop", headers=auth("a_run"))
    assert response.status_code == 200 and response.json()["state"] == "stopped"
    other, _ = runner.submit("orchestrator-a", "a-run", fields())
    runner.docker.on_create = lambda name: runner.stop(other["id"])
    runner.tick()
    assert runner.join(30)
    assert runner.store.task(other["id"])["state"] == "stopped"
    assert runner.docker.starts == [] and runner.docker.runs() == []


# spec: ANYM-819
def test_stop_a_running_task_stops_the_container_and_a_terminal_one_is_unchanged(tmp_path):
    runner = make_runner(tmp_path, modes={"claude": "sleep"})
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    runner.tick()
    row = wait_running(runner, task["id"])
    process = runner.docker.containers[row["container"]].proc
    assert runner.stop(task["id"])["state"] == "stopped"
    assert runner.join(30)
    assert process.poll() is not None and row["container"] in runner.docker.stops
    row = runner.store.task(task["id"])
    assert (row["state"], row["reason"]) == ("stopped", "stop_requested")
    again = runner.stop(task["id"])
    assert again["state"] == "stopped" and again["finished_at"] == row["finished_at"]

