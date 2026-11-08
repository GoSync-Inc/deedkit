"""Fakes: a Docker whose containers are local fake-engine processes, a host gate, a clock, a config writer.

No test here starts a real engine or contacts a vendor; tests/test_docker_live.py runs real containers from a
locally built fake image."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from deedkit_agent_runner import config as cfg
from deedkit_agent_runner.docker import DockerError
from deedkit_agent_runner.runner import Runner

HERE = Path(__file__).resolve().parent
FAKE_ENGINE = HERE / "fake_engine.py"
IMAGE = "registry.example/agent-task@sha256:" + "a" * 64
PEER = "100.100.1.2"
KEYS = {"a_run": "key-a-run-0001", "a_read": "key-a-read-0001", "b_run": "key-b-run-0001",
        "gone": "key-revoked-0001"}
CLAUDE_HELP = ("Usage: claude [options]\n  -p, --print  print\n  --output-format <format>\n  --verbose\n"
               "  --permission-mode <mode>\n  --no-session-persistence\n")
CODEX_HELP = ("Usage: codex exec [OPTIONS] [PROMPT]\n  --json\n  --ephemeral\n  -s, --sandbox <MODE>\n"
              "  -C, --cd <DIR>\n  -o, --output-last-message <FILE>\n")


def auth(name):
    return {"authorization": f"Bearer {KEYS[name]}"}


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def document(tmp, **overrides):
    value = {
        "version": 1,
        "listen": {"tailnet_host": "100.101.102.103", "port": 8448, "tls_cert": str(tmp / "cert.pem"),
                   "tls_key": str(tmp / "key.pem")},
        "production_peer": PEER,
        "state_dir": str(tmp / "state"),
        "image": IMAGE,
        "network": "agents",
        "engines": {"claude": {"token_file": str(tmp / "claude-token")}, "codex": {"login_volume": "codex-login"}},
        "workspace_volume": {"driver": "local", "options": {"type": "tmpfs", "device": "tmpfs", "o": "size=3g"}},
        "out_volume": {"driver": "local", "options": {"type": "tmpfs", "device": "tmpfs", "o": "size=16m"}},
        "host_gate": {"argv": ["true"]},
        "host_check": {"argv": ["true"]},
        "keys": [
            {"key_id": "a-run", "sha256": sha(KEYS["a_run"]), "principal": "orchestrator-a", "operations": ["read", "run"]},
            {"key_id": "a-read", "sha256": sha(KEYS["a_read"]), "principal": "orchestrator-a", "operations": ["read"]},
            {"key_id": "b-run", "sha256": sha(KEYS["b_run"]), "principal": "orchestrator-b", "operations": ["read", "run"]},
            {"key_id": "gone", "sha256": sha(KEYS["gone"]), "principal": "orchestrator-a", "operations": ["read", "run"],
             "revoked": True},
        ],
    }
    for key, item in overrides.items():
        value[key] = item
    return value


def write_private(path, data):
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    os.chmod(path, 0o600)
    return path


def write_config(tmp, **overrides):
    write_private(tmp / "claude-token", "synthetic-claude-login")
    path = write_private(tmp / "runner.json", document(tmp, **overrides))
    return path, cfg.load(path)


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Gate:
    def __init__(self, state="ok", reason=None, probe="ok"):
        self.state, self.reason, self.probe = state, reason, probe
        self.reads = 0

    def read(self):
        self.reads += 1
        return (self.state, self.reason) if self.state != "ok" else ("ok", None)

    def self_probe(self):
        return self.probe


class Container:
    def __init__(self, name, args, env, mounts):
        self.name, self.args, self.env, self.mounts = name, args, env, mounts
        self.proc, self.task_id = None, name.removeprefix("agent-task-")


class FakeDocker:
    """Containers are fake-engine processes; volumes are directories. Records every call."""

    def __init__(self, root, modes=None, help_text=None, login_code=0):
        self.root = Path(root)
        self.modes = modes or {}
        self.help_text = help_text or {"claude": CLAUDE_HELP, "codex": CODEX_HELP}
        self.login_code = login_code
        self.containers, self.volumes = {}, {}
        self.creates, self.oneoffs, self.starts, self.stops = [], [], [], []
        self.secret_values = []
        self.lock = threading.Lock()
        self.fail_create, self.config = None, None
        self.on_create = None
        self.codex_login = self.root / "codex-login"
        self.codex_login.mkdir(parents=True, exist_ok=True)

    def volume_dir(self, name):
        return self.root / "volumes" / name

    def volume_create(self, name, volume, task_id):
        assert volume.limit() is not None
        self.volume_dir(name).mkdir(parents=True, exist_ok=True)
        self.volumes[name] = volume

    def volume_rm(self, name):
        shutil.rmtree(self.volume_dir(name), ignore_errors=True)
        self.volumes.pop(name, None)

    def create(self, args, engine):
        if self.fail_create:
            raise DockerError(self.fail_create)
        from deedkit_agent_runner.runner import claude_token
        secrets = {}
        if engine == "claude":
            secrets = claude_token(self.config)
        self.secret_values.append(secrets)
        name = args[args.index("--name") + 1]
        mounts = [a for i, a in enumerate(args) if i and args[i - 1] == "--mount"]
        env = [a for i, a in enumerate(args) if i and args[i - 1] == "--env"]
        with self.lock:
            self.containers[name] = Container(name, args, env, mounts)
            self.creates.append(args)
        if self.on_create:
            self.on_create(name)

    def start_attached(self, name):
        container = self.containers[name]
        engine = "claude" if "claude" in container.args[-8:] else "codex"
        mode = self.modes.get(container.task_id) or self.modes.get(engine) or f"{engine}_ok"
        task_id = container.task_id
        env = {"PATH": os.environ.get("PATH", ""), "FAKE_MODE": mode,
               "FAKE_WORKSPACE": str(self.volume_dir(f"agent-ws-{task_id}")),
               "FAKE_OUT": str(self.volume_dir(f"agent-out-{task_id}")),
               "FAKE_STREAMS": str(HERE / "streams"), "FAKE_CODEX_HOME": str(self.codex_login),
               "FAKE_RUNS": str(self.root / "runs.log")}
        container.proc = subprocess.Popen([sys.executable, str(FAKE_ENGINE)], env=env, stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        self.starts.append(name)
        return container.proc

    def running(self, name):
        container = self.containers.get(name)
        if container is None:
            return "missing"
        if container.proc is None:
            return "created"
        return "running" if container.proc.poll() is None else "exited"

    def exit_code(self, name):
        container = self.containers.get(name)
        if container is None or container.proc is None:
            return None, False
        code = container.proc.wait()
        return (128 - code if code < 0 else code), False

    def _signal(self, name, sig):
        container = self.containers.get(name)
        if container and container.proc and container.proc.poll() is None:
            try:
                os.killpg(container.proc.pid, sig)  # the container's whole process tree, as the cgroup kill does
            except ProcessLookupError:
                pass
            container.proc.wait(10)

    def stop(self, name, grace=20):
        self.stops.append(name)
        self._signal(name, 15)

    def kill(self, name):
        self._signal(name, 9)

    def remove(self, name):
        with self.lock:
            self.containers.pop(name, None)

    def labelled(self):
        return [(name, c.task_id) for name, c in self.containers.items()]

    def copy_out(self, name, path, limit):
        container = self.containers.get(name)
        if container is None:
            return None
        target = self.volume_dir(f"agent-out-{container.task_id}") / Path(path).name
        return target.read_bytes()[:limit] if target.exists() else None

    def oneoff(self, args, engine, timeout=60):
        self.oneoffs.append(args)
        if args[-1] == "--help":
            return 0, self.help_text[engine].encode(), b""
        if args[-1] == "status":
            return (0, b"Logged in", b"") if self.login_code == 0 else (1, b"Not logged in. Please run /login", b"")
        if args[-3:-1] == ["sh", "-c"]:
            for entry in self.codex_login.iterdir():
                if entry.name == "auth.json" and entry.is_file() and not entry.is_symlink():
                    continue
                shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
            return 0, b"", b""
        return 0, b"", b""

    def runs(self):
        path = self.root / "runs.log"
        return path.read_text().split() if path.exists() else []


def make_runner(tmp, modes=None, gate=None, clock=None, **overrides):
    path, config = write_config(tmp, **overrides)
    docker = FakeDocker(tmp / "docker", modes)
    docker.config = config
    runner = Runner(config, docker, gate or Gate(), clock=clock or Clock())
    runner.startup()
    runner.config_path = path
    return runner


def fields(engine="claude", **overrides):
    value = {"engine": engine, "instruction": "Fix the typo in README.", "owner_instruction": "fix the typo",
             "request_id": str(uuid.uuid4()), "max_minutes": 45, "continue_task_id": None}
    value.update(overrides)
    return value


def run_task(runner, engine="claude", principal="orchestrator-a", **overrides):
    task, _ = runner.submit(principal, "a-run", fields(engine, **overrides))
    runner.tick()
    assert runner.join(30)
    return runner.store.task(task["id"])

