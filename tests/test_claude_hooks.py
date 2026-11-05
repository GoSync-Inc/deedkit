"""The PreToolUse hook refuses Bash commands that print a container's environment or a private env file (ANYM-774)."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import textwrap
import time

from hypothesis import given, settings, strategies as st
import pytest

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_FILE = ROOT / ".claude/settings.json"
MATCHER_FILE = ROOT / ".claude/hooks/block_secret_env_reads.py"
SECURITY_FILE = ROOT / "SECURITY.md"
CANARY = "synthetic_canary_7f3a"

BLOCKED = [
    "docker inspect c",
    "docker container inspect c",
    "docker --context prod inspect c",
    "docker -H tcp://127.0.0.1:2375 inspect c",
    "podman inspect c",
    "/usr/bin/docker inspect c",
    "docker ps -q | xargs docker inspect",
    "docker inspect --format '{{.Config}}' c",
    "docker inspect --format '{{.Config.Env}}' c",
    "docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' c",
    "docker inspect -f '{{.Spec}}' c",
    "docker inspect -f '{{json .}}' c",
    "docker inspect -f '{{.}}' c",
    "docker inspect -f '{{index . \"Config\"}}' c",
    "docker inspect --format=json c",
    "docker inspect --format='{{.State.Status}}' c",
    "docker inspect -f='{{.State.Status}}' c",
    "docker inspect --format json c",
    "docker inspect --format '' c",
    'docker inspect --format "$TEMPLATE" c',
    "docker inspect -f '{{.State.Status}}' -f '{{.Config.Env}}' c",
    "docker compose config",
    "docker compose -f a.yaml -p b config",
    "docker compose $FILES config",
    "docker compose config --format json",
    "docker compose config --environment",
    "docker compose config --no-env-resolution --environment",
    "docker compose config --services --environment",
    "docker-compose config",
    "docker exec c env",
    "docker exec -it c printenv",
    "docker exec -u root c set",
    "docker exec c export",
    "docker exec c cat /proc/1/environ",
    "docker exec c python3 /proc/1/environ",
    "docker exec c sh -c 'env'",
    "docker run --rm image env",
    "docker run --rm --name x image printenv",
    "docker run --entrypoint env image",
    "docker compose exec svc env",
    "docker compose exec -T svc printenv",
    "docker compose run --rm svc set",
    "docker compose -f a.yaml run --rm svc export",
    "env",
    "printenv",
    "printenv SECRET_NAME",
    "set",
    "env | grep NAME",
    "cat .env",
    "cat deploy/private/postgres.env",
    "less a.env",
    "more a.env",
    "head -n 1 a.env",
    "tail -f a.env",
    "grep NAME a.env",
    "grep -A 3 NAME a.env",
    "rg NAME a.env",
    "sed -n 1p a.env",
    "awk '{print}' a.env",
    "strings a.env",
    "xxd a.env",
    "base64 a.env",
    "cat /proc/1/environ",
    "tr '\\0' '\\n' < /proc/1/environ",
    "cat /var/lib/docker/containers/x/config.v2.json",
    "cat a.dsn",
    "cat a.token",
    "cat a.key",
    "cat a.pem",
    "cat a.agekey",
    "cat < a.env",
    "cat /dev/stdin < a.env",
    "sudo cat /dev/stdin < a.env",
    "python3 a.py --key-file /dev/stdin < /proc/1/environ",
    "python3 a.py < a.env",
    "systemctl show unit",
    "systemctl show -p Environment unit",
    "systemctl show --property=Environment unit",
    "systemctl show --property Id,Environment unit",
]

ALLOWED = [
    "docker inspect --format '{{.State.Health.Status}}' c",
    "docker inspect -f '{{.State.Status}} {{.RestartCount}}' c",
    "docker inspect -f '{{json .State}}' c",
    "docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' c",
    "docker ps",
    "docker logs c",
    "docker compose config --no-env-resolution",
    "docker compose config --services",
    "docker compose config --images",
    "docker compose config --hash '*'",
    "docker compose config --quiet",
    "docker compose config --volumes",
    "docker exec c ls /app",
    "docker exec -it c ls env",
    'git commit -m "env"',
    'git commit -m "docker inspect c and printenv"',
    "ls /usr/bin/printenv",
    "echo printenv",
    "command -v env",
    "cat <<EOF >> .env\nA=1\nEOF",
    "cat > a.env",
    "cat .env.example",
    "grep -n os.environ a.py",
    "rg '\\.env$' a.py",
    "grep -e .key a.py",
    "grep -r NAME --exclude=*.env .",
    "set -euo pipefail",
    "set -- a b",
    'run env PROBE_TEST_DSN_FILE="$RUN_DIR/test-1.dsn" uv run pytest "$@"',
    'env PROBE_TEST_DSN_FILE="$RUN_DIR/test-1.dsn" uv run pytest "$@"',
    "systemctl show -P ActiveState unit",
    "systemctl show -p ActiveState,UnitFileState unit",
    "systemctl show --property=Id unit",
    "case $1 in env) echo one;; set) echo two;; esac",
    "for name in env set; do echo $name; done",
    "gh pr create --body \"$(cat <<'EOF'\nIt's a body: docker inspect c, printenv (and cat .env)\nEOF\n)\"",
    "# printenv\nls",
    "python -m messaging_service key-info --key-file /dev/stdin < /private/a.token",
]

SUBSTITUTED = [
    "x=$(docker inspect c)",
    'echo "$(printenv)"',
    "echo `docker inspect c`",
    "cat <(docker inspect c)",
    "cat <<EOF\n$(printenv)\nEOF",
    "ssh deedkit-prod <<'EOF'\ndocker inspect c\nEOF",
    "bash -s <<'EOF'\nprintenv\nEOF",
    "bash <<< 'printenv'",
    "sudo -n cat a.env",
    "sudo -u postgres env",
    "timeout 5 docker inspect c",
    "FOO=1 docker inspect c",
    "(printenv)",
    "{ printenv; }",
    "if true; then printenv; fi",
    "ls && printenv",
    "ssh -o BatchMode=yes -p 22 deedkit-prod docker inspect c",
    "eval 'docker inspect c'",
    "find . -name x -exec docker inspect c \\;",
    "doc''ker inspect c",
]


def wrap_ssh(command):
    return f"ssh deedkit-prod {shlex.quote(command)}"


def wrap_sudo(command):
    return f"sudo sh -c {shlex.quote(command)}"


def wrap_bash(command):
    escaped = re.sub(r'(["$`\\])', r"\\\1", command)
    return f'bash -c "{escaped}"'


WRAPPERS = {"ssh": wrap_ssh, "sudo": wrap_sudo, "bash": wrap_bash}


def matcher():
    spec = importlib.util.spec_from_file_location("block_secret_env_reads", MATCHER_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def entry():
    return json.loads(SETTINGS_FILE.read_text())["hooks"]["PreToolUse"][0]


def run_hook(stdin, *, path=None, project=ROOT):
    """Runs the settings entry's exact command line, as the runtime does."""
    environment = {"PATH": path or os.environ["PATH"], "CLAUDE_PROJECT_DIR": str(project)}
    data = stdin if isinstance(stdin, bytes) else stdin.encode()
    return subprocess.run(["/bin/sh", "-c", entry()["hooks"][0]["command"]], input=data, capture_output=True,
                          env=environment, timeout=30)


def call(command):
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def tool_path(directory, *names):
    """A PATH holding only these real tools, so a missing python3 is the cause under test."""
    for name in names:
        found = shutil.which(name) or (shutil.which("gtimeout") if name == "timeout" else None)
        assert found, name
        (directory / name).symlink_to(found)
    return str(directory)


# spec: ANYM-774
@pytest.mark.parametrize("command", BLOCKED)
def test_blocked_command_exits_2(command):
    assert run_hook(call(command)).returncode == 2


# spec: ANYM-774
@pytest.mark.parametrize("wrapper", sorted(WRAPPERS))
@pytest.mark.parametrize("command", BLOCKED)
def test_blocked_command_stays_blocked_when_wrapped(command, wrapper):
    assert matcher().is_blocked(WRAPPERS[wrapper](command))


# spec: ANYM-774
@pytest.mark.parametrize("command", SUBSTITUTED)
def test_blocked_inside_substitution_pipe_or_wrapper(command):
    assert run_hook(call(command)).returncode == 2


# spec: ANYM-774
@pytest.mark.parametrize("command", ALLOWED)
def test_allowed_command_exits_0(command):
    done = run_hook(call(command))
    assert (done.returncode, done.stderr) == (0, b"")


# spec: ANYM-774
@pytest.mark.parametrize("stdin", [b"", b"{", b"[]", b"\xff", b"{}", b'{"tool_input": {}}', b'{"tool_input": {"command": 1}}',
                                   b'{"tool_input": {"command": ""}}'])
def test_bad_input_fails_closed(stdin):
    assert run_hook(stdin).returncode == 2


# spec: ANYM-774
def test_a_command_of_1_mb_fails_closed():
    assert run_hook(call("x" * 1_000_000)).returncode == 2


# spec: ANYM-774
def test_a_large_ordinary_command_is_still_read():
    assert run_hook(call("echo " + "word " * 40_000)).returncode == 0


# spec: ANYM-774
def test_deep_nesting_fails_closed():
    assert matcher().decide(call("echo " + "$(" * 200 + "ls" + ")" * 200).encode())[0] == 2


# spec: ANYM-774
def test_path_without_python3_fails_closed(tmp_path):
    done = run_hook(call("ls"), path=tool_path(tmp_path, "sh", "timeout"))
    assert done.returncode == 2 and b"python3" in done.stderr


# spec: ANYM-774
def test_missing_script_file_fails_closed(tmp_path):
    assert run_hook(call("ls"), project=tmp_path).returncode == 2


# spec: ANYM-774
def test_matcher_that_sleeps_past_the_bound_exits_2_within_it(tmp_path):
    shim = tmp_path / "python3"
    shim.write_text(f"#!/bin/sh\nexec {shutil.which('sleep')} 25\n")
    shim.chmod(0o755)
    started = time.monotonic()
    done = run_hook(call("ls"), path=tool_path(tmp_path, "sh", "timeout"))
    assert done.returncode == 2 and time.monotonic() - started < 15


# spec: ANYM-774
def test_internal_error_fails_closed_without_the_command(monkeypatch):
    module = matcher()
    def explode(_):
        raise ValueError(CANARY)
    monkeypatch.setattr(module, "is_blocked", explode)
    code, message = module.decide(call(f"ls {CANARY}").encode())
    assert code == 2 and CANARY not in message


# spec: ANYM-774
def test_settings_entry_fails_closed_and_is_bound_to_bash():
    hook_entry = entry()
    assert hook_entry["matcher"] == "Bash"
    hook = hook_entry["hooks"][0]
    assert hook["command"] == 'sh "$CLAUDE_PROJECT_DIR/.claude/hooks/block-secret-env-reads.sh" || exit 2'
    assert hook["type"] == "command" and hook["onFailure"] == "block" and hook["timeout"] > 5


# spec: ANYM-774
def test_session_start_hook_is_still_first():
    first = json.loads(SETTINGS_FILE.read_text())["hooks"]["SessionStart"][0]["hooks"][0]
    assert first["command"] == 'sh "$CLAUDE_PROJECT_DIR/.claude/hooks/harness.sh"'


# spec: ANYM-774
def test_refusal_names_why_and_safe_forms_and_never_echoes_the_command():
    done = run_hook(call(f"docker inspect {CANARY}"))
    text = done.stderr.decode()
    assert done.returncode == 2 and CANARY not in text and len(text.strip().splitlines()) == 1
    for part in ("model provider", ".State", ".Name", ".Image", ".RestartCount", ".Created", "--no-env-resolution",
                 "orchestrator"):
        assert part in text


# spec: ANYM-774
def test_security_document_names_the_hook_and_its_limits():
    text = SECURITY_FILE.read_text()
    start = text.index("Agent sessions: reading container environments")
    paragraph = text[start:]
    for part in (".claude/hooks/block-secret-env-reads.sh", "worktrees", "orchestrator", "other projects", "secrets as files"):
        assert part in paragraph


def fenced_shell_blocks(text):
    pattern = re.compile(r"^([ \t]*)```(?:sh|bash|shell|zsh|console)[ \t]*\n(.*?)^[ \t]*```[ \t]*$", re.MULTILINE | re.DOTALL)
    for match in pattern.finditer(text):
        yield re.sub(r"^\$ ", "", textwrap.dedent(match.group(2)), flags=re.MULTILINE)


# spec: ANYM-774
def test_fence_extraction_finds_indented_blocks():
    assert list(fenced_shell_blocks("x\n  ```sh\n  $ ls\n  ```\n")) == ["ls\n"]


# spec: ANYM-774
def test_no_document_prescribes_a_blocked_command():
    module = matcher()
    listing = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "*.md"], check=True, capture_output=True, text=True)
    found = []
    for name in filter(None, listing.stdout.split("\0")):
        path = ROOT / name
        if not path.is_file(): continue
        for block in fenced_shell_blocks(path.read_text(encoding="utf-8")):
            if module.is_blocked(block): found.append((name, block[:200]))
    assert found == []


# spec: ANYM-774
@settings(max_examples=200, deadline=None)
@given(st.text(max_size=200))
def test_any_text_as_a_quoted_argument_of_a_plain_command_is_data(text):
    assert not matcher().is_blocked(f"git commit -m {shlex.quote(text)}")


# spec: ANYM-774
@settings(max_examples=200, deadline=None)
@given(st.sampled_from(BLOCKED), st.lists(st.sampled_from(sorted(WRAPPERS)), max_size=4))
def test_any_stack_of_wrappers_keeps_a_blocked_command_blocked(command, stack):
    for wrapper in stack:
        command = WRAPPERS[wrapper](command)
    assert matcher().is_blocked(command)


# spec: ANYM-774
@settings(max_examples=200, deadline=None)
@given(st.lists(st.text(max_size=40).filter(lambda line: "EOF" not in line and "\n" not in line and "\r" not in line), max_size=5))
def test_any_quoted_heredoc_body_of_a_plain_reader_is_data(lines):
    assert not matcher().is_blocked("cat <<'EOF'\n" + "\n".join(lines) + "\nEOF\n")


# spec: ANYM-774
@settings(max_examples=300, deadline=None)
@given(st.text(max_size=300))
def test_any_command_text_gets_a_decision_that_never_echoes_it(text):
    code, message = matcher().decide(call(text + CANARY).encode())
    assert code in (0, 2) and CANARY not in message
