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

    seen = {}

    def recheck_then_stop_all(task_id, paused):
        ok = original(task_id, paused)
        # stop-all arrives between the re-check and `docker start`: it must wait for the start (the lock)
        # and then stop the running container, never return while a started engine is left running.
        other = threading.Thread(target=stop_all, args=(runner.store, runner.docker, runner.pause_file))
        other.start()
        time.sleep(0.3)
        seen["waited"] = other.is_alive() and runner.docker.starts == []
        seen["thread"] = other
        return ok

    runner.store.commit_launch = recheck_then_stop_all
    runner.tick()
    assert runner.join(30)
    seen["thread"].join(30)
    runner.tick()
    assert runner.join(30)
    assert seen["waited"] is True
    assert runner.store.task(task["id"])["state"] == "stopped"
    assert live(runner) == [] and runner.docker.labelled() == []


# spec: ANYM-819
def test_a_container_that_has_not_stopped_keeps_its_engines_slot(tmp_path):
    runner = make_runner(tmp_path, modes={"codex": "sleep"})
    first, _ = runner.submit("orchestrator-a", "a-run", fields("codex"))
    second, _ = runner.submit("orchestrator-a", "a-run", fields("codex"))
    runner.tick()
    row = wait_running(runner, first["id"])
    runner.docker.unstoppable.add(row["container"])  # the stop does not land
    assert runner.stop(first["id"])["state"] == "stopped"
    for _ in range(3):
        runner.tick()
        time.sleep(0.1)
    assert runner.store.task(second["id"])["state"] == "queued"  # never beside a container still alive
    assert runner.store.slot_held("codex") and runner.docker.running(row["container"]) == "running"
    assert row["container"] in runner.docker.stops
    runner.docker.unstoppable.clear()  # the next reconcile attempt lands
    deadline = time.monotonic() + 20
    while runner.store.task(second["id"])["state"] == "queued" and time.monotonic() < deadline:
        runner.tick()
        time.sleep(0.1)
    assert runner.docker.running(row["container"]) == "missing"
    assert runner.store.task(second["id"])["state"] in ("starting", "running")
    runner.stop(second["id"])
    for _ in range(50):
        runner.tick()
        if not live(runner):
            break
        time.sleep(0.1)
    assert runner.join(30)


# spec: ANYM-819
def test_a_timed_out_container_also_holds_the_slot_until_it_is_gone(tmp_path):
    from support import Clock
    clock = Clock()
    runner = make_runner(tmp_path, modes={"claude": "sleep"}, clock=clock)
    first, _ = runner.submit("orchestrator-a", "a-run", fields(max_minutes=5))
    second, _ = runner.submit("orchestrator-a", "a-run", fields())
    runner.tick()
    row = wait_running(runner, first["id"])
    runner.docker.unstoppable.add(row["container"])
    clock.advance(301)
    runner.tick()
    time.sleep(0.2)
    runner.tick()
    assert runner.store.task(first["id"])["state"] == "timed_out"
    assert runner.store.task(second["id"])["state"] == "queued"
    runner.docker.unstoppable.clear()
    runner.docker.modes["claude"] = "claude_ok"
    deadline = time.monotonic() + 20
    while runner.store.task(second["id"])["state"] != "succeeded" and time.monotonic() < deadline:
        runner.tick()
        time.sleep(0.1)
    assert runner.store.task(second["id"])["state"] == "succeeded"
    assert runner.join(30)


# spec: ANYM-819
def test_a_start_not_confirmed_in_time_is_ended_before_the_lock_is_released(tmp_path, monkeypatch):
    from deedkit_agent_runner import runner as runner_module
    monkeypatch.setattr(runner_module, "START_CONFIRM_S", 0.3)
    runner = make_runner(tmp_path, modes={"claude": "sleep"})
    runner.docker.start_delay = 3.0  # the daemon would start it only after the confirmation window
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    runner.tick()
    assert runner.join(30)
    stop_all(runner.store, runner.docker, runner.pause_file)  # returns after the lock is free
    time.sleep(3.2)  # past the moment the delayed start would have landed
    row = runner.store.task(task["id"])
    assert (row["state"], row["reason"]) == ("failed", "start_unconfirmed")
    assert live(runner) == [] and runner.docker.labelled() == []


# spec: ANYM-819
def test_a_docker_outage_never_reads_as_a_gone_container(tmp_path):
    runner = make_runner(tmp_path, modes={"claude": "sleep"})
    first, _ = runner.submit("orchestrator-a", "a-run", fields())
    second, _ = runner.submit("orchestrator-a", "a-run", fields())
    runner.tick()
    row = wait_running(runner, first["id"])
    runner.docker.daemon_down = True  # inspect and rm now fail; the old container is still there
    runner.docker.containers[row["container"]].proc.kill()
    deadline = time.monotonic() + 10
    while runner.store.task(first["id"])["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    for _ in range(3):
        runner.tick()
        time.sleep(0.1)
    assert runner.store.task(first["id"])["container_gone"] == 0
    assert runner.store.slot_held("claude") and runner.store.task(second["id"])["state"] == "queued"
    runner.docker.daemon_down = False  # the daemon is back: reconciliation confirms the absence
    runner.docker.modes["claude"] = "claude_ok"
    deadline = time.monotonic() + 20
    while runner.store.task(second["id"])["state"] != "succeeded" and time.monotonic() < deadline:
        runner.tick()
        time.sleep(0.1)
    assert runner.store.task(first["id"])["container_gone"] == 1
    assert runner.store.task(second["id"])["state"] == "succeeded"
    assert runner.join(30)


# spec: ANYM-819
def test_a_stop_that_wins_the_lock_runs_no_engine_at_all(runner):
    from deedkit_agent_runner.store import launch_lock
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    with launch_lock(runner.state_dir):  # stop-all's locked part runs first
        runner.tick()
        deadline = time.monotonic() + 10
        while not runner.docker.creates and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)  # the launch is now waiting for the lock, its container created, not started
        runner.pause_file.touch()
        runner.store.request_stop(task["id"])
    assert runner.join(30)
    row = runner.store.task(task["id"])
    assert (row["state"], row["reason"]) == ("stopped", "stop_requested")
    assert runner.docker.starts == [] and runner.docker.runs() == [] and runner.docker.labelled() == []


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

