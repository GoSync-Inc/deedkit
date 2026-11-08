"""The container boundary: argv for every Docker call the runner makes, and a thin CLI wrapper.

Tests swap `DockerCLI` for a fake with the same methods; the argv builders are pure and asserted directly.
"""

import io
import os
import re
import subprocess
import tarfile

LABEL = "deedkit.task"
MEMORY = "2500m"
PIDS = 512
CPUS = 2
TMP = "/tmp:rw,nosuid,nodev,size=256m"
STOP_GRACE_S = 20
CODEX_HOME = "/codex-home"
# Every environment name a task container gets; the values are fixed except the Claude login, which the
# docker CLI reads from its own environment (never from argv, which `ps` shows to every account on the host).
ENV_ALLOWLIST = ("HOME", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_CODE_OAUTH_TOKEN",
                 "CODEX_HOME")
MOUNT_TARGETS = {"/workspace", "/out", CODEX_HOME}

CLAUDE_ARGV = ("claude", "-p", "--output-format", "stream-json", "--verbose", "--permission-mode",
               "bypassPermissions", "--no-session-persistence")
CODEX_ARGV = ("codex", "exec", "--json", "--ephemeral", "-s", "danger-full-access", "-C", "/workspace",
              "-o", "/out/result.txt", "-")
# The flags each pinned argv needs; a `--help` without one of them disables the engine (engine_not_configured).
REQUIRED_FLAGS = {
    "claude": ("--print", "--output-format", "--verbose", "--permission-mode", "--no-session-persistence"),
    "codex": ("--json", "--ephemeral", "--sandbox", "--cd", "--output-last-message"),
}
HELP_ARGV = {"claude": ("claude", "--help"), "codex": ("codex", "exec", "--help")}
LOGIN_ARGV = {"claude": ("claude", "auth", "status"), "codex": ("codex", "login", "status")}


def container_name(task_id):
    return f"agent-task-{task_id}"


def workspace_volume(task_id):
    return f"agent-ws-{task_id}"


def out_volume(task_id):
    return f"agent-out-{task_id}"


def engine_env(engine):
    env: dict[str, str | None] = {"HOME": "/tmp", "DISABLE_AUTOUPDATER": "1"}
    if engine == "claude":
        env.update({"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_OAUTH_TOKEN": None})
    else:
        env["CODEX_HOME"] = CODEX_HOME
    return env


def env_args(env):
    args = []
    for name, value in env.items():
        assert name in ENV_ALLOWLIST, name
        args += ["--env", name if value is None else f"{name}={value}"]
    return args


def hardening(config):
    return ["--user", config.task_user, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--tmpfs", TMP, "--memory", MEMORY, "--memory-swap", MEMORY,
            "--pids-limit", str(PIDS), "--cpus", str(CPUS),
            # No daemon-side copy of the output: the runner reads the attached stream into its own capped log.
            "--log-driver", "none"]


def login_mount(config, engine):
    if engine == "codex":
        return ["--mount", f"type=volume,src={config.engines.codex.login_volume},dst={CODEX_HOME}"]
    return []


def task_create_argv(config, task_id, engine):
    """`docker create` for one task: a fresh container from the pinned image, its own two volumes, the engine's
    login only, allowlisted environment, fixed limits, read-only root except /workspace, /out and /tmp."""
    return ["create", "--interactive", "--name", container_name(task_id), "--label", f"{LABEL}={task_id}",
            *hardening(config), "--network", config.network,
            "--mount", f"type=volume,src={workspace_volume(task_id)},dst=/workspace",
            "--mount", f"type=volume,src={out_volume(task_id)},dst=/out",
            *login_mount(config, engine), *env_args(engine_env(engine)), "--workdir", "/workspace",
            config.image, *(CLAUDE_ARGV if engine == "claude" else CODEX_ARGV)]


def oneoff_argv(config, engine, command, network=True, login=True):
    """A throwaway container (help check, login status, Codex login hygiene): same hardening, no task volume.
    Without `login` it gets neither the Codex login volume nor the Claude login (the help check)."""
    env = engine_env(engine)
    if not login:
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    return ["run", "--rm", *hardening(config), "--network", config.network if network else "none",
            *(login_mount(config, engine) if login else []), *env_args(env), config.image, *command]


# Removes everything in the Codex login volume except a regular auth.json, so nothing one task writes reaches
# the next one. A task's own files may carry any mode, so each entry is made writable first (never through a
# symlink operand); any failed removal, or anything left but a regular auth.json, exits non-zero.
HYGIENE_SCRIPT = (
    'set -eu; chmod u+rwx -- "$CODEX_HOME"; cd "$CODEX_HOME"; '
    'keep() { [ "$1" = auth.json ] && [ -f "$1" ] && [ ! -L "$1" ]; }; '
    'for f in .[!.]* ..?* *; do [ -e "$f" ] || [ -L "$f" ] || continue; keep "$f" && continue; '
    '[ -L "$f" ] || chmod -R u+rwx -- "$f"; rm -rf -- "$f"; done; '
    'for f in .[!.]* ..?* *; do [ -e "$f" ] || [ -L "$f" ] || continue; keep "$f" || exit 3; done')
CODEX_HYGIENE = ("sh", "-c", HYGIENE_SCRIPT)


NOT_FOUND = re.compile(rb"(?m)^(Error: |Error response from daemon: )?No such (container|object): \S")
TRANSPORT = re.compile(rb"(?i)cannot connect to the docker daemon|error during connect|dial unix|connection refused"
                       rb"|no such file or directory|is the docker daemon running")


class DockerError(Exception):
    pass


class DockerCLI:
    def __init__(self, config, secret_env=None):
        self.binary = config.docker.binary
        self.env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "LANG")}
        if config.docker.host:
            self.env["DOCKER_HOST"] = config.docker.host
        self.secret_env = secret_env or (lambda engine: {})

    def _run(self, args, timeout=60, env=None, stdin=None):
        try:
            done = subprocess.run([self.binary, *args], capture_output=True, timeout=timeout, env=env or self.env,
                                  input=stdin, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DockerError(type(error).__name__) from None
        return done.returncode, done.stdout, done.stderr

    def volume_create(self, name, volume, task_id):
        args = ["volume", "create", "--driver", volume.driver, "--label", f"{LABEL}={task_id}"]
        for key, value in sorted(volume.options.items()):
            args += ["--opt", f"{key}={value}"]
        code, _, err = self._run([*args, name])
        if code:
            raise DockerError(err.decode(errors="replace")[:200])

    def volume_rm(self, name):
        self._run(["volume", "rm", name])

    def create(self, args, engine):
        code, _, err = self._run(args, env={**self.env, **self.secret_env(engine)})
        if code:
            raise DockerError(err.decode(errors="replace")[:200])

    def start_attached(self, name):
        """`docker start --attach --interactive`: the prompt goes on stdin, the engine's stream comes on stdout."""
        return subprocess.Popen([self.binary, "start", "--attach", "--interactive", name], env=self.env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def running(self, name):
        """The container's status; "missing" only when the daemon says there is no such container, "unknown"
        when it could not be asked (a daemon or socket error), so absence is never assumed."""
        try:
            code, out, err = self._run(["inspect", "--type", "container", "--format", "{{.State.Status}}", name],
                                       timeout=20)
        except DockerError:
            return "unknown"
        if code == 0:
            return out.decode().strip() or "unknown"
        # Only the daemon's own not-found answer; a transport error such as a missing socket ("... connect: no
        # such file or directory") or a daemon that is down reads unknown.
        return "missing" if NOT_FOUND.search(err) and not TRANSPORT.search(err) else "unknown"

    def exit_code(self, name):
        code, out, _ = self._run(["inspect", "--format", "{{.State.ExitCode}} {{.State.OOMKilled}}", name])
        if code:
            return None, False
        value, oom = out.decode().split()
        return int(value), oom == "true"

    def stop(self, name, grace=STOP_GRACE_S):
        self._run(["stop", "--time", str(grace), name], timeout=grace + 30)
        if self.running(name) == "running":
            self._run(["kill", name], timeout=30)

    def kill(self, name):
        self._run(["kill", name], timeout=30)

    def remove(self, name):
        try:
            code, _, _ = self._run(["rm", name], timeout=60)  # named volumes stay for the export (S4)
        except DockerError:
            return False
        return code == 0

    def labelled(self):
        code, out, _ = self._run(["ps", "--all", "--filter", f"label={LABEL}", "--format",
                                  f'{{{{.Names}}}} {{{{.Label "{LABEL}"}}}}'])
        if code:
            raise DockerError("ps_failed")
        return [tuple(line.split()) for line in out.decode().splitlines() if len(line.split()) == 2]

    def copy_out(self, name, path, limit):
        """One file out of a stopped container, read as data and capped."""
        code, out, _ = self._run(["cp", f"{name}:{path}", "-"], timeout=60)
        if code:
            return None
        try:
            with tarfile.open(fileobj=io.BytesIO(out)) as archive:
                member = archive.next()
                if member is None or not member.isfile():
                    return None
                handle = archive.extractfile(member)
                return handle.read(limit) if handle else None
        except tarfile.TarError:
            return None

    def oneoff(self, args, engine, timeout=60, login=True):
        return self._run(args, timeout=timeout, env={**self.env, **(self.secret_env(engine) if login else {})})
