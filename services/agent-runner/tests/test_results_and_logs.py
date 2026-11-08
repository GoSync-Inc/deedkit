import json

import pytest

from deedkit_agent_runner import engines, journal

from support import auth, fields, make_runner, run_task

STREAMS = __import__("pathlib").Path(__file__).resolve().parent / "streams"


# spec: ANYM-819
def test_the_claude_stream_gives_summary_outcome_progress_and_a_normalized_log(client, runner):
    task = run_task(runner, "claude")
    body = client.get(f"/v1/tasks/{task['id']}", headers=auth("a_read")).json()
    assert body["state"] == "succeeded" and body["outcome"] == "done" and body["exit_code"] == 0
    assert body["summary"].startswith("Fixed the typo in README.md")
    assert body["progress"]["commits"] == 1 and body["progress"]["files_changed"] == 1
    assert body["progress"]["last_text"] == "I will fix the typo in README.md."
    assert body["progress"]["last_activity_at"].endswith("Z")
    assert body["engine"] == "claude" and body["request_id"] == task["request_id"]
    assert body["continue_task_id"] is None
    assert body["publish_state"] == "refused" and body["publish_reason"] == "publication_not_configured"
    assert body["next_action"] == "tell_owner" and body["branch"] is None and body["compare_url"] is None
    page = client.get(f"/v1/tasks/{task['id']}/log", headers=auth("a_read")).json()
    kinds = [e["kind"] for e in page["entries"]]
    assert kinds == ["assistant", "tool", "tool", "tool", "result"]
    assert set(page["entries"][0]) == {"time", "kind", "text"} and page["end"] is True
    raw = client.get(f"/v1/tasks/{task['id']}/log?raw=true", headers=auth("a_read")).json()
    assert [json.loads(e["text"])["type"] for e in raw["entries"] if e["text"].startswith("{")][-1] == "result"
    assert any(e["text"] == "not json: a stray stderr line from a tool" for e in raw["entries"])


# spec: ANYM-819
def test_the_codex_stream_and_its_result_file_give_the_summary_and_outcome(client, runner):
    task = run_task(runner, "codex")
    body = client.get(f"/v1/tasks/{task['id']}", headers=auth("a_read")).json()
    assert body["outcome"] == "partial" and body["summary"].startswith("Fixed the typo.")
    assert body["progress"]["commits"] == 1 and body["progress"]["files_changed"] == 2
    page = client.get(f"/v1/tasks/{task['id']}/log", headers=auth("a_read")).json()
    assert [e["kind"] for e in page["entries"]] == ["tool", "tool", "tool", "assistant", "result"]


# spec: ANYM-819
def test_the_recorded_streams_normalize_to_the_same_shape():
    for engine in ("claude", "codex"):
        stream = engines.Stream(engine)
        entries = [e for line in (STREAMS / f"{engine}.jsonl").read_text().splitlines() for e in stream.feed(line)]
        assert {kind for kind, _ in entries} <= {"assistant", "tool", "result"}
        assert entries[-1][0] == "result"
        assert all(len(text) <= engines.TEXT_LIMIT for _, text in entries)


# spec: ANYM-819
@pytest.mark.parametrize("summary, outcome", [
    ("All good.\nRESULT: done — fixed", "done"), ("RESULT: partial — half", "partial"),
    ("x\nRESULT: blocked — needs a key", "blocked"), ("no result line", "unknown"), (None, "unknown"),
    ("RESULT: done — first\nthen\nRESULT: blocked — last wins", "blocked"),
])
def test_the_outcome_comes_from_the_result_line(summary, outcome):
    assert engines.outcome(summary) == outcome


# spec: ANYM-819
def test_summary_and_last_text_are_capped():
    stream = engines.Stream("claude")
    stream.feed(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "y" * 900}]}}))
    stream.feed(json.dumps({"type": "result", "is_error": False, "result": "z" * 9000}))
    assert len(stream.last_text) == engines.LAST_TEXT_LIMIT
    assert len(engines.summary(stream, None)) == engines.SUMMARY_LIMIT
    codex = engines.Stream("codex")
    assert len(engines.summary(codex, b"w" * 9000)) == engines.SUMMARY_LIMIT


# spec: ANYM-819
def test_log_pages_are_at_most_64_kib_with_an_opaque_cursor(tmp_path):
    runner = make_runner(tmp_path, modes={"claude": "flood"})
    task = run_task(runner)
    path = runner.state_dir / "logs" / f"{task['id']}.jsonl"
    cursor, total, pages = "", 0, 0
    while True:
        page = journal.page(path, cursor, False)
        size = sum(len(json.dumps(e)) for e in page["entries"])
        assert size <= journal.PAGE_LIMIT
        total += len(page["entries"])
        pages += 1
        if page["end"]:
            break
        assert page["next_cursor"] != cursor and not page["next_cursor"].isdigit()
        cursor = page["next_cursor"]
    assert total == len(path.read_text().splitlines()) and pages > 10
    assert journal.page(path, "!!bad", False) is None


# spec: ANYM-819
def test_a_bad_cursor_is_422_and_a_running_task_says_wait(tmp_path):
    from starlette.testclient import TestClient
    from deedkit_agent_runner import config as cfg
    from deedkit_agent_runner.app import PeerGuard, create_app
    from support import PEER
    runner = make_runner(tmp_path, modes={"claude": "sleep"})
    app = PeerGuard(create_app(runner, cfg.Keys(runner.config_path, runner.config)), [PEER])
    task, _ = runner.submit("orchestrator-a", "a-run", fields())
    runner.tick()
    with TestClient(app, client=(PEER, 1)) as client:
        import time
        deadline = time.monotonic() + 20
        while runner.store.task(task["id"])["state"] != "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        body = client.get(f"/v1/tasks/{task['id']}", headers=auth("a_read")).json()
        assert body["state"] == "running" and body["next_action"] == "wait"
        assert body["expected_finish_by"] is not None and body["started_at"] is not None
        assert client.get(f"/v1/tasks/{task['id']}/log?cursor=%%%", headers=auth("a_read")).status_code == 422
        runner.stop(task["id"])
    assert runner.join(30)
