import json

from deedkit_agent_runner.journal import sha256

from support import Clock, fields, make_runner, run_task

DAY = 86400


# spec: ANYM-819
def test_the_container_goes_at_the_terminal_state_and_the_volumes_when_publication_settles(tmp_path):
    runner = make_runner(tmp_path)
    settle = {"publish_state": "pending"}
    runner.publisher = lambda task: dict(settle)
    task = run_task(runner)
    assert runner.docker.labelled() == []
    assert {f"agent-ws-{task['id']}", f"agent-out-{task['id']}"} <= set(runner.docker.volumes)
    settle.update(publish_state="published", branch=f"agent/{task['id']}",
                  compare_url="https://github.com/owner/fork/compare/main...agent")
    runner.sweep()
    row = runner.store.task(task["id"])
    assert row["publish_state"] == "published" and row["volumes_removed"] == 1
    assert not {f"agent-ws-{task['id']}", f"agent-out-{task['id']}"} & set(runner.docker.volumes)


# spec: ANYM-819
def test_texts_logs_and_patches_go_14_days_after_the_terminal_state(tmp_path):
    clock = Clock()
    runner = make_runner(tmp_path, clock=clock)
    task = run_task(runner)
    log = runner.state_dir / "logs" / f"{task['id']}.jsonl"
    patch = runner.state_dir / "patches" / task["id"]
    patch.mkdir()
    (patch / "patch.mbox").write_text("synthetic patch")
    assert log.exists()
    clock.advance(14 * DAY - 60)
    runner.sweep()
    assert runner.store.task(task["id"])["instruction"] and log.exists()
    clock.advance(120)
    runner.sweep()
    row = runner.store.task(task["id"])
    assert row["instruction"] is None and row["owner_instruction"] is None and row["summary"] is None
    assert row["progress"] == "{}" and not log.exists() and not patch.exists()
    # The dedup row stays: a replay of the same request returns the same task and starts nothing.
    replay = {k: task[k] for k in ("engine", "request_id", "max_minutes", "continue_task_id")}
    replay.update(instruction="Fix the typo in README.", owner_instruction="fix the typo")
    again, created = runner.submit("orchestrator-a", "a-run", replay)
    assert again["id"] == task["id"] and not created


# spec: ANYM-819
def test_dedup_rows_and_the_audit_go_after_365_days(tmp_path):
    clock = Clock()
    runner = make_runner(tmp_path, clock=clock)
    old = run_task(runner)
    clock.advance(200 * DAY)
    recent = run_task(runner)
    clock.advance(166 * DAY)
    runner.sweep()
    assert runner.store.get(old["id"]) is None
    assert runner.store.get(recent["id"]) is not None
    entries = [json.loads(line) for line in (runner.state_dir / "audit.jsonl").read_text().splitlines()]
    assert {e["task_id"] for e in entries} == {recent["id"]}
    assert oct((runner.state_dir / "audit.jsonl").stat().st_mode & 0o777) == "0o600"


# spec: ANYM-819
def test_the_audit_holds_hashes_never_the_texts(tmp_path):
    runner = make_runner(tmp_path)
    task = run_task(runner, instruction="Synthetic instruction text 7f3a", owner_instruction="synthetic owner 9b2c")
    text = (runner.state_dir / "audit.jsonl").read_text()
    assert "Synthetic instruction text 7f3a" not in text and "synthetic owner 9b2c" not in text
    entry = json.loads(text.splitlines()[0])
    assert set(entry) == {"time", "key_id", "principal", "task_id", "request_id", "engine", "instruction_sha256",
                          "owner_instruction_sha256", "state", "exit_code"}
    assert entry["instruction_sha256"] == sha256("Synthetic instruction text 7f3a")
    assert entry["key_id"] == "a-run" and entry["task_id"] == task["id"]


# spec: ANYM-819
def test_no_file_of_task_1_is_visible_to_task_2(tmp_path):
    runner = make_runner(tmp_path, modes={"codex": "write_secret"})
    (runner.docker.codex_login / "auth.json").write_text("{}")
    first = run_task(runner, "codex")
    assert first["state"] == "succeeded"
    runner.docker.modes["codex"] = "list_workspace"
    second = run_task(runner, "codex")
    assert second["summary"] == "seen: auth.json"
    mounts = [m for m in runner.docker.creates[-1] if m.startswith("type=volume")]
    assert not any(first["id"] in m for m in mounts)


# spec: ANYM-819
def test_state_files_are_private(tmp_path):
    runner = make_runner(tmp_path)
    task = run_task(runner)
    for path in (runner.state_dir, runner.state_dir / "logs"):
        assert path.stat().st_mode & 0o077 == 0, path
    assert (runner.state_dir / "logs" / f"{task['id']}.jsonl").stat().st_mode & 0o077 == 0


# spec: ANYM-819
def test_a_queued_task_stopped_by_another_process_is_settled_by_the_sweep(tmp_path):
    runner = make_runner(tmp_path)
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    runner.store.request_stop(task["id"])
    runner.sweep()
    assert runner.store.task(task["id"])["publish_state"] == "refused"
