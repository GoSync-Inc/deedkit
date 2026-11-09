import json

import pytest

from deedkit_agent_runner.store import Refused

from support import Gate, auth, fields, make_runner


# spec: ANYM-819
def test_capabilities_report_engines_caps_pause_host_gate_and_host_check(client, runner):
    runner.submit("orchestrator-a", "a-run", fields("codex"))
    body = client.get("/v1/capabilities", headers=auth("a_read")).json()
    assert body["engines"]["claude"] == {"configured": True, "login_state": "ok", "max_running": 1, "max_queued": 2,
                                         "queued": 0, "running": 0}
    assert body["engines"]["codex"]["queued"] == 1
    assert body["daily_starts"] == 10 and body["starts_left"] == 9 and body["paused"] is False
    assert body["host_gate"] == {"state": "ok"} and body["host_check"] == "ok"
    runner.pause_file.touch()
    runner.gate.state, runner.gate.reason = "busy", "inference"
    body = client.get("/v1/capabilities", headers=auth("a_read")).json()
    assert body["paused"] is True and body["host_gate"] == {"state": "busy", "reason": "inference"}


# spec: ANYM-819
def test_login_state_comes_from_a_throwaway_container_with_only_that_engines_login(tmp_path):
    runner = make_runner(tmp_path)
    runner.docker.login_code = 1
    from deedkit_agent_runner.runner import capabilities
    body = capabilities(runner)
    assert body["engines"]["claude"]["login_state"] == "login_required"
    checks = [a for a in runner.docker.oneoffs if a[-1] == "status"]
    assert len(checks) == 2
    for args in checks:
        assert args[:2] == ["run", "--rm"]
        assert not any("agent-ws-" in a or "agent-out-" in a for a in args)
        assert not any("github" in a.lower() or a == "git" for a in args)
    codex = next(a for a in checks if "codex" in a)
    assert any(a.startswith("type=volume,src=codex-login,") for a in codex)
    claude = next(a for a in checks if "claude" in a)
    assert not any(a.startswith("type=volume,src=codex-login,") for a in claude)
    # Cached: a second read within ten minutes starts no container.
    capabilities(runner)
    assert len([a for a in runner.docker.oneoffs if a[-1] == "status"]) == 2


# spec: ANYM-819
def test_an_unconfigured_engine_reports_unknown_login_without_a_check(tmp_path):
    runner = make_runner(tmp_path, engines={"codex": {"login_volume": "codex-login"}})
    from deedkit_agent_runner.runner import capabilities
    body = capabilities(runner)
    assert body["engines"]["claude"] == {"configured": False, "login_state": "unknown", "max_running": 1,
                                         "max_queued": 2, "queued": 0, "running": 0}
    assert not any("claude" in a for args in runner.docker.oneoffs for a in args if args[-1] == "status")


# spec: ANYM-819
def test_a_failed_host_check_refuses_starts(tmp_path):
    runner = make_runner(tmp_path, gate=Gate(probe="failed"))
    from deedkit_agent_runner.runner import capabilities
    assert capabilities(runner)["host_check"] == "failed"
    with pytest.raises(Refused) as refused:
        runner.submit("orchestrator-a", "a-run", fields())
    assert (refused.value.status, refused.value.code) == (503, "host_check_failed")
    # A task queued before the probe failed never launches and ends host_busy after the hour.
    task, _ = runner.store.admit("orchestrator-a", fields(), runner.preamble_sha256, paused=lambda: False,
                                 refusal=lambda engine: None, daily_cap=10)
    runner.tick()
    assert json.loads(runner.store.task(task["id"])["progress"])["host_reason"] == "host_check"
    runner.clock.advance(3600)
    runner.tick()
    assert (runner.store.task(task["id"])["state"], runner.store.task(task["id"])["reason"]) == ("failed", "host_busy")
    assert runner.docker.creates == []
