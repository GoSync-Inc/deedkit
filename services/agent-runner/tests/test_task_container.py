import pytest

from deedkit_agent_runner import docker as dk
from deedkit_agent_runner.store import Refused

from support import IMAGE, FakeDocker, Gate, fields, make_runner, run_task, write_config

ALLOWED_MOUNTS = {"claude": {"/workspace", "/out"}, "codex": {"/workspace", "/out", dk.CODEX_HOME}}
REQUIRED = [("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges"), ("--memory", "2500m"),
            ("--memory-swap", "2500m"), ("--pids-limit", "512"), ("--cpus", "2"), ("--network", "agents"),
            ("--workdir", "/workspace"), ("--log-driver", "none")]


def check_create(args, task_id, engine):
    """The container contract: fails on any env name outside the allowlist, any mount outside the list, any
    missing limit, a root user, a host path, the Docker socket, or an engine argv other than the pinned one."""
    pairs = list(zip(args, args[1:]))
    for pair in REQUIRED:
        assert pair in pairs, pair
    assert "--read-only" in args
    assert ("--tmpfs", dk.TMP) in pairs and "size=" in dk.TMP
    user = args[args.index("--user") + 1]
    assert not user.startswith("0") and user.split(":")[0] != "0"
    assert ("--name", f"agent-task-{task_id}") in pairs and ("--label", f"deedkit.task={task_id}") in pairs
    env = [v for k, v in pairs if k == "--env"]
    names = {e.split("=", 1)[0] for e in env}
    assert names <= set(dk.ENV_ALLOWLIST), names - set(dk.ENV_ALLOWLIST)
    assert not any(e.startswith("CLAUDE_CODE_OAUTH_TOKEN=") for e in env), "the token must never be on argv"
    mounts = [v for k, v in pairs if k == "--mount"]
    targets = set()
    for mount in mounts:
        parts = dict(p.split("=", 1) for p in mount.split(","))
        assert parts["type"] == "volume", mount  # no bind mount of any host path
        targets.add(parts["dst"])
    assert targets == ALLOWED_MOUNTS[engine], targets
    assert not any(flag in args for flag in ("--volume", "-v", "--privileged", "--cap-add", "--pid", "--ipc",
                                             "--device", "--env-file", "--userns", "--security-opt=seccomp"))
    assert "docker.sock" not in " ".join(args)
    image = args.index(IMAGE)
    assert tuple(args[image + 1:]) == (dk.CLAUDE_ARGV if engine == "claude" else dk.CODEX_ARGV)
    return names, targets


# spec: ANYM-819
@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_the_fake_engine_container_gets_only_the_allowlisted_env_mounts_and_every_limit(runner, engine):
    task = run_task(runner, engine)
    args = runner.docker.creates[-1]
    names, targets = check_create(args, task["id"], engine)
    if engine == "claude":
        assert "CLAUDE_CODE_OAUTH_TOKEN" in names and "CODEX_HOME" not in names
        assert runner.docker.secret_values[-1] == {"CLAUDE_CODE_OAUTH_TOKEN": "synthetic-claude-login"}
    else:
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in names and ("--env", f"CODEX_HOME={dk.CODEX_HOME}") in \
            list(zip(args, args[1:]))
        assert f"type=volume,src=codex-login,dst={dk.CODEX_HOME}" in args
        assert runner.docker.secret_values[-1] == {}
    assert task["state"] == "succeeded"


# spec: ANYM-819
@pytest.mark.parametrize("mutation", ["extra_env", "host_mount", "socket", "no_memory", "no_pids", "root"])
def test_the_container_check_fails_on_any_deviation(runner, mutation):
    task = run_task(runner, "claude")
    args = list(runner.docker.creates[-1])
    if mutation == "extra_env":
        args[args.index("--workdir"):args.index("--workdir")] = ["--env", "GITHUB_TOKEN"]
    elif mutation == "host_mount":
        args[args.index("--workdir"):args.index("--workdir")] = ["--mount", "type=bind,src=/Users,dst=/workspace"]
    elif mutation == "socket":
        args[args.index("--workdir"):args.index("--workdir")] = ["--mount",
                                                                 "type=volume,src=/var/run/docker.sock,dst=/out"]
    elif mutation == "no_memory":
        index = args.index("--memory")
        del args[index:index + 2]
    elif mutation == "no_pids":
        index = args.index("--pids-limit")
        del args[index:index + 2]
    else:
        args[args.index("--user") + 1] = "0:0"
    with pytest.raises(AssertionError):
        check_create(args, task["id"], "claude")


# spec: ANYM-819
def test_the_prompt_is_the_versioned_preamble_then_the_instruction_on_stdin(runner):
    task = run_task(runner, "claude", instruction="Rename the helper in tools/x.py.")
    prompt = (runner.docker.volume_dir(f"agent-out-{task['id']}") / "prompt.txt")
    assert not prompt.exists()  # the out volume is gone once publication settles
    assert task["preamble_sha256"] == runner.preamble_sha256
    assert "Rename the helper" not in " ".join(runner.docker.creates[-1])
    text = runner.preamble
    assert text.startswith("<!-- agent-runner preamble v1")
    for phrase in ("RESULT: done|partial|blocked", "Tiden cards", "Reviews", "merging", "Never send a message",
                   "write to production", "phone numbers", "commit", "ruff check", "need no database"):
        assert phrase in text, phrase


# spec: ANYM-819
def test_the_engine_reads_preamble_and_instruction_from_stdin(tmp_path):
    runner = make_runner(tmp_path)
    runner.publisher = lambda task: {"publish_state": "pending"}  # keep the volumes to read what the engine got
    task = run_task(runner, "codex", instruction="Rename the helper in tools/x.py.")
    prompt = (runner.docker.volume_dir(f"agent-out-{task['id']}") / "prompt.txt").read_text()
    assert prompt == f"{runner.preamble}\n\nRename the helper in tools/x.py.\n"


# spec: ANYM-819
def test_a_pinned_flag_missing_from_help_disables_the_engine(tmp_path):
    path, config = write_config(tmp_path)
    docker = FakeDocker(tmp_path / "docker")
    docker.config = config
    docker.help_text["codex"] = docker.help_text["codex"].replace("--ephemeral", "")
    from deedkit_agent_runner.runner import Runner
    runner = Runner(config, docker, Gate())
    runner.startup()
    assert runner.configured == {"claude": True, "codex": False}
    with pytest.raises(Refused) as refused:
        runner.submit("orchestrator-a", "a-run", fields("codex"))
    assert (refused.value.status, refused.value.code) == (409, "engine_not_configured")
    help_calls = [a for a in docker.oneoffs if a[-1] == "--help"]
    assert all(("--network", "none") in list(zip(a, a[1:])) for a in help_calls)
    # No login of any kind in a help check: no login volume, no token name, no secret read.
    assert not any("codex-login" in x or "CLAUDE_CODE_OAUTH_TOKEN" in x for a in help_calls for x in a)
    assert all(login is False for name, login in docker.oneoff_logins if name == "--help")


# spec: ANYM-819
def test_a_missing_claude_login_neither_stops_the_service_nor_disables_an_engine(tmp_path):
    path, config = write_config(tmp_path)
    (tmp_path / "claude-token").unlink()
    docker = FakeDocker(tmp_path / "docker")
    docker.config = config
    from deedkit_agent_runner.runner import Runner, capabilities
    runner = Runner(config, docker, Gate())
    runner.startup()  # a cold start with the Claude login gone
    assert runner.configured == {"claude": True, "codex": True}
    body = capabilities(runner)
    assert body["engines"]["claude"]["login_state"] == "login_required"
    assert body["engines"]["codex"]["login_state"] == "ok"
    assert run_task(runner, "codex")["state"] == "succeeded"


# spec: ANYM-819
def test_an_engine_disabled_in_config_is_not_configured(tmp_path):
    runner = make_runner(tmp_path, engines={"claude": {"enabled": False, "token_file": str(tmp_path / "t")},
                                            "codex": {"login_volume": "codex-login"}})
    assert runner.configured == {"claude": False, "codex": True}


# spec: ANYM-819
def test_the_codex_login_volume_keeps_only_auth_json_before_each_task(runner):
    login = runner.docker.codex_login
    (login / "auth.json").write_text("{}")
    (login / "sessions").mkdir()
    (login / "sessions" / "a.jsonl").write_text("x")
    (login / "config.toml").write_text("model = 'x'")
    (login / ".hidden").write_text("x")
    run_task(runner, "codex")
    assert sorted(p.name for p in login.iterdir()) == ["auth.json"]
    hygiene = [a for a in runner.docker.oneoffs if a[-3:-1] == ["sh", "-c"]]
    assert hygiene and ("--network", "none") in list(zip(hygiene[-1], hygiene[-1][1:]))


# spec: ANYM-819
def test_the_real_hygiene_script_removes_unreadable_dirs_and_symlinks_and_keeps_only_auth_json(runner, tmp_path):
    login = runner.docker.codex_login
    (login / "auth.json").write_text("{}")
    locked = login / "a-locked"
    (locked / "inner").mkdir(parents=True)
    (locked / "inner" / "secret.txt").write_text("synthetic")
    (locked / "inner").chmod(0)
    locked.chmod(0)  # a task leaves a directory with mode 000, followed in glob order by a removable file
    (login / "b-file").write_text("x")
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive")
    (login / "link").symlink_to(outside)
    (login / "dirlink").symlink_to(tmp_path)
    (login / "-rf").write_text("odd name")
    (login / "with space").write_text("x")
    (login / "..dots").write_text("x")
    assert runner.codex_hygiene() is True
    assert sorted(p.name for p in login.iterdir()) == ["auth.json"]
    assert outside.read_text() == "must survive" and tmp_path.exists()


# spec: ANYM-819
def test_a_symlinked_or_directory_auth_json_is_removed_too(runner, tmp_path):
    login = runner.docker.codex_login
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (login / "auth.json").symlink_to(target)
    assert runner.codex_hygiene() is True
    assert list(login.iterdir()) == [] and target.exists()
    (login / "auth.json").mkdir()
    assert runner.codex_hygiene() is True and list(login.iterdir()) == []


# spec: ANYM-819
def test_a_failed_hygiene_stops_the_codex_task_before_it_starts(runner):
    runner.docker.hygiene_code = 3
    task = run_task(runner, "codex")
    assert (task["state"], task["reason"]) == ("failed", "container_failed")
    assert runner.docker.creates == [] and runner.docker.runs() == []


# spec: ANYM-819
def test_nothing_a_codex_task_writes_to_the_login_volume_outlives_it(tmp_path):
    runner = make_runner(tmp_path, modes={"codex": "write_secret"})
    (runner.docker.codex_login / "auth.json").write_text("{}")
    task = run_task(runner, "codex")
    assert task["state"] == "succeeded"
    assert sorted(p.name for p in runner.docker.codex_login.iterdir()) == ["auth.json"]  # no later task needed
    (runner.docker.codex_login / "late.txt").write_text("x")  # e.g. a file a crashed cleanup left
    runner.sweep()
    assert sorted(p.name for p in runner.docker.codex_login.iterdir()) == ["auth.json"]


# spec: ANYM-819
def test_image_must_be_pinned(tmp_path):
    from deedkit_agent_runner import config as cfg
    with pytest.raises(cfg.ConfigError, match="image_must_be_pinned_by_digest"):
        write_config(tmp_path, image="deedkit-agent-task:latest")
    with pytest.raises(cfg.ConfigError, match="workspace_volume_needs_a_size_limit"):
        write_config(tmp_path, workspace_volume={"driver": "local", "options": {}})
    with pytest.raises(cfg.ConfigError, match="workspace_volume_needs_a_size_limit"):
        write_config(tmp_path, workspace_volume={"driver": "local", "options": {"size": "4g"}})
    with pytest.raises(cfg.ConfigError, match="out_volume_needs_a_size_limit"):
        write_config(tmp_path, out_volume={"driver": "local", "options": {"o": "size=32m"}})
