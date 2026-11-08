import threading
import uuid

import pytest

from deedkit_agent_runner.store import Refused

from support import auth, fields


def concurrently(count, target):
    barrier, results = threading.Barrier(count), [None] * count

    def call(index):
        barrier.wait()
        try:
            results[index] = target(index)
        except Refused as refused:
            results[index] = refused

    threads = [threading.Thread(target=call, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return results


# spec: ANYM-819
def test_twenty_concurrent_identical_posts_start_one_run(runner):
    value = fields()
    results = concurrently(20, lambda i: runner.submit("orchestrator-a", "a-run", dict(value)))
    ids = {task["id"] for task, _ in results}
    assert len(ids) == 1 and sum(created for _, created in results) == 1
    for _ in range(3):
        runner.tick()
        assert runner.join(30)
    assert runner.docker.runs() == [ids.pop()]
    assert len(runner.store.rows("succeeded")) == 1


# spec: ANYM-819
def test_concurrent_conflicting_posts_give_one_task_and_409s(runner):
    request_id = str(uuid.uuid4())
    results = concurrently(10, lambda i: runner.submit(
        "orchestrator-a", "a-run", fields(request_id=request_id, instruction=f"variant {i}")))
    tasks = [r for r in results if not isinstance(r, Refused)]
    refusals = [r for r in results if isinstance(r, Refused)]
    assert len(tasks) == 1 and len(refusals) == 9
    assert {(r.status, r.code) for r in refusals} == {(409, "request_id_conflict")}


# spec: ANYM-819
def test_a_replay_while_paused_or_full_returns_the_existing_task(runner):
    first, created = runner.submit("orchestrator-a", "a-run", fields())
    assert created
    second, _ = runner.submit("orchestrator-a", "a-run", fields())
    with pytest.raises(Refused) as full:
        runner.submit("orchestrator-a", "a-run", fields())
    assert full.value.code == "runner_busy"
    runner.pause_file.touch()
    with pytest.raises(Refused) as paused:
        runner.submit("orchestrator-a", "a-run", fields())
    assert (paused.value.status, paused.value.code) == (503, "runner_paused")
    replay = {k: first[k] for k in ("engine", "instruction", "owner_instruction", "request_id", "max_minutes",
                                    "continue_task_id")}
    again, created = runner.submit("orchestrator-a", "a-run", replay)
    assert again["id"] == first["id"] and not created
    runner.pause_file.unlink()
    again, created = runner.submit("orchestrator-a", "a-run", replay)
    assert again["id"] == first["id"] and not created
    assert {t["id"] for t in runner.store.rows("queued")} == {first["id"], second["id"]}


# spec: ANYM-819
def test_the_same_request_id_is_its_own_for_each_principal(runner):
    value = fields()
    a, _ = runner.submit("orchestrator-a", "a-run", dict(value))
    b, _ = runner.submit("orchestrator-b", "b-run", dict(value))
    assert a["id"] != b["id"]


# spec: ANYM-819
@pytest.mark.parametrize("change", [
    {"instruction": ""}, {"instruction": "x" * 8001}, {"owner_instruction": "ok"},
    {"owner_instruction": "x" * 1501}, {"max_minutes": 4}, {"max_minutes": 121}, {"max_minutes": "45"},
    {"request_id": "not-a-uuid"}, {"engine": "gemini"}, {"continue_task_id": "nope"},
])
def test_fields_outside_their_ranges_are_refused(client, runner, change):
    value = fields()
    value.pop("continue_task_id")
    value.update(change)
    response = client.post("/v1/tasks", json=value, headers=auth("a_run"))
    assert response.status_code == 422 and response.json()["error"] == "invalid_request"
    assert runner.store.rows("queued") == []


# spec: ANYM-819
def test_a_body_over_64_kib_is_refused_before_parsing(client, runner):
    response = client.post("/v1/tasks", content=b"{" + b" " * (64 * 1024) + b"}", headers=auth("a_run"))
    assert response.status_code == 413 and response.json()["error"] == "request_too_large"
    assert runner.store.rows("queued") == []


# spec: ANYM-819
def test_defaults_and_the_201_then_200_replay(client):
    value = fields()
    value.pop("continue_task_id")
    value.pop("max_minutes")
    first = client.post("/v1/tasks", json=value, headers=auth("a_run"))
    assert first.status_code == 201 and first.json()["state"] == "queued"
    replay = client.post("/v1/tasks", json=value, headers=auth("a_run"))
    assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
    assert client.post("/v1/tasks", json={**value, "max_minutes": 50}, headers=auth("a_run")).status_code == 409


# spec: ANYM-819
def test_continue_task_id_needs_a_terminal_task_of_the_same_principal_with_a_branch(runner):
    base, _ = runner.submit("orchestrator-a", "a-run", fields())
    with pytest.raises(Refused, match="continue_task_invalid"):
        runner.submit("orchestrator-a", "a-run", fields(continue_task_id=base["id"]))  # still queued
    runner.store.finish(base["id"], "succeeded")
    with pytest.raises(Refused, match="continue_task_invalid"):
        runner.submit("orchestrator-a", "a-run", fields(continue_task_id=base["id"]))  # no branch
    runner.store.update(base["id"], branch=f"agent/{base['id']}")
    with pytest.raises(Refused, match="continue_task_invalid"):
        runner.submit("orchestrator-b", "b-run", fields(continue_task_id=base["id"]))  # another principal
    task, created = runner.submit("orchestrator-a", "a-run", fields(continue_task_id=base["id"]))
    assert created and task["continue_task_id"] == base["id"]
