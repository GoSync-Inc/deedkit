"""Real containers from a locally built fake image (tests/live_image), driven by the runner's own Docker calls.

They run only where AGENT_RUNNER_DOCKER_TESTS=required (the CI step): the runner's restart recovery kills every
container labelled deedkit.task on the daemon, so these tests must never run on a host with real tasks. Where the
variable is set, a missing daemon fails the run."""

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from deedkit_agent_runner import config as cfg
from deedkit_agent_runner.cli import secret_env
from deedkit_agent_runner.docker import LABEL, DockerCLI
from deedkit_agent_runner.runner import Runner

from support import Gate, document, fields, write_private

pytestmark = pytest.mark.docker
HERE = Path(__file__).resolve().parent
TOKEN = "synthetic-claude-live-login"


def docker(*args, timeout=600):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


@pytest.fixture(scope="module")
def image():
    if os.environ.get("AGENT_RUNNER_DOCKER_TESTS") != "required":
        pytest.skip("live container tests run only with AGENT_RUNNER_DOCKER_TESTS=required (CI)")
    try:
        ready = docker("info", timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ready = False
    if not ready:
        pytest.fail("AGENT_RUNNER_DOCKER_TESTS=required but no Docker daemon answers")
    built = docker("build", "--quiet", str(HERE / "live_image"))
    assert built.returncode == 0, built.stderr
    return built.stdout.strip()


@pytest.fixture
def live(tmp_path, image):
    suffix = uuid.uuid4().hex[:8]
    login = f"agentrunnertest_codex_{suffix}"
    tmpfs = {"type": "tmpfs", "device": "tmpfs", "o": "size=64m,uid=1000,gid=1000"}
    data = document(tmp_path, image=image, network="none",
                    engines={"claude": {"token_file": str(tmp_path / "claude-token")},
                             "codex": {"login_volume": login}},
                    workspace_volume={"driver": "local", "options": tmpfs},
                    out_volume={"driver": "local", "options": {**tmpfs, "o": "size=16m,uid=1000,gid=1000"}})
    write_private(tmp_path / "claude-token", TOKEN)
    config = cfg.load(write_private(tmp_path / "runner.json", data))
    runner = Runner(config, DockerCLI(config, secret_env(config)), Gate())
    runner.startup()
    yield runner
    for name, _ in runner.docker.labelled():
        docker("kill", name)
        docker("rm", name)
    docker("volume", "rm", login)
    for line in docker("volume", "ls", "--quiet", "--filter", f"label={LABEL}").stdout.split():
        docker("volume", "rm", line)


def run(runner, engine="claude", mode="ok", wait=True, **overrides):
    task, _ = runner.submit("orchestrator-a", "a-run", fields(engine, instruction=f"MODE={mode}", **overrides))
    runner.tick()
    if wait:
        assert runner.join(240)
    return runner.store.task(task["id"])


def report(task):
    return json.loads(task["summary"])


# spec: ANYM-819
def test_live_both_engines_pass_the_help_check_and_finish_in_a_removed_container(live):
    assert live.configured == {"claude": True, "codex": True}
    for engine, outcome in (("claude", "done"), ("codex", "done")):
        task = run(live, engine)
        assert (task["state"], task["exit_code"], task["outcome"]) == ("succeeded", 0, outcome), task
        assert live.docker.labelled() == []
        assert docker("volume", "inspect", f"agent-ws-{task['id']}").returncode != 0


# spec: ANYM-819
@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_live_the_container_is_non_root_without_capabilities_and_read_only(live, engine):
    found = report(run(live, engine, "probe"))
    assert found["uid"] == 1000 and int(found["cap_eff"], 16) == 0 and found["no_new_privs"] == "1"
    assert found["root_write"] == "EROFS" and found["workspace_write"] == "ok"
    assert found["docker_socket"] is False and found["prompt_has_preamble"] is True
    assert "/workspace" in found["mounts"] and "/out" in found["mounts"]
    assert not any(m.startswith(("/Users", "/home", "/root", "/var/run")) for m in found["mounts"])
    ours = {"HOME", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_CODE_OAUTH_TOKEN",
            "CODEX_HOME"}
    image_env = {"PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256", "HOSTNAME"}
    assert set(found["env"]) <= ours | image_env, set(found["env"]) - ours - image_env
    if engine == "claude":
        assert found["token"] == TOKEN and "CODEX_HOME" not in found["env"]
        assert "/codex-home" not in found["mounts"]
    else:
        assert found["token"] is None and "/codex-home" in found["mounts"]


# spec: ANYM-819
def test_live_stop_ends_the_engine_and_its_setsid_child(live):
    task = run(live, "claude", "detached", wait=False)
    deadline = time.monotonic() + 60
    while live.store.task(task["id"])["state"] != "running" and time.monotonic() < deadline:
        time.sleep(0.2)
    time.sleep(2)
    name = task["container"]
    top = docker("top", name).stdout
    assert "sleep 600" in top
    started = time.monotonic()
    live.stop(task["id"])
    assert live.join(120)
    assert time.monotonic() - started < 40
    assert live.store.task(task["id"])["state"] == "stopped"
    assert docker("inspect", name).returncode != 0  # removed, with every process of its namespace
    assert live.docker.labelled() == []


# spec: ANYM-819
def test_live_filling_memory_is_killed_by_the_container_limit(live):
    task = run(live, "claude", "memory")
    assert (task["state"], task["reason"]) == ("failed", "memory_limit"), task
    assert docker("info", timeout=30).returncode == 0  # the daemon (the VM) is unaffected


# spec: ANYM-819
def test_live_filling_the_disk_stops_at_the_workspace_and_tmp_limits(live):
    found = report(run(live, "codex", "disk"))
    assert found == {"workspace": "ENOSPC", "tmp": "ENOSPC", "root": "EROFS"}


# spec: ANYM-819
def test_live_the_pids_limit_holds(live):
    found = report(run(live, "claude", "pids"))
    assert found["threads"] < 512


# spec: ANYM-819
def test_live_a_restart_kills_a_running_labelled_container_and_marks_it_lost(live):
    task = run(live, "claude", "detached", wait=False)
    deadline = time.monotonic() + 60
    while live.store.task(task["id"])["state"] != "running" and time.monotonic() < deadline:
        time.sleep(0.2)
    fresh = Runner(live.config, live.docker, Gate())
    fresh.startup()
    assert fresh.store.task(task["id"])["state"] == "lost"
    assert fresh.docker.labelled() == []
    live.join(60)
    assert fresh.store.task(task["id"])["state"] == "lost"
