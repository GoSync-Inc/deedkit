"""`deedkit key install|uninstall` (ANYM-498) on a synthetic Mac: a temp HOME and CLAUDE_CONFIG_DIR, stateful stub
`ssh` and `claude` executables alone on PATH, and a loopback gateway. No real key, config or network is touched."""
import ast
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shlex
import stat
import subprocess
import sys
import threading
import time

import pytest

from scripts import deedkit_admin as admin
from scripts import deedkit_mac as mac

STUB = r'''#!PYTHON
import json, os, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
name = Path(sys.argv[0]).name
with open(here / "calls.jsonl", "a") as log:
    log.write(json.dumps([name] + sys.argv[1:]) + "\n")
plan = here / (name + ".json")
behaviour = json.loads(plan.read_text()) if plan.exists() else {}
if behaviour.get("hang"):
    (here / (name + ".pid")).write_text(str(os.getpid()))
    time.sleep(60)
if name == "ssh":
    for stream, key in ((sys.stdout, "stdout"), (sys.stderr, "stderr")):
        if key in behaviour:
            stream.buffer.write(Path(behaviour[key]).read_bytes())
    sys.exit(behaviour.get("code", 0))
if name != "claude":
    sys.exit(3)
config = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
value = json.loads(config.read_text()) if config.exists() else {}
servers = value.setdefault("mcpServers", {})
if sys.argv[1:5] == ["mcp", "add-json", "--scope", "user"] and len(sys.argv) == 7:
    if sys.argv[5] in servers:
        sys.exit(1)
    servers[sys.argv[5]] = json.loads(sys.argv[6])
elif sys.argv[1:5] == ["mcp", "remove", "--scope", "user"] and len(sys.argv) == 6:
    if servers.pop(sys.argv[5], None) is None:
        sys.exit(1)
else:
    sys.exit(2)
config.write_text(json.dumps(value))
print("Added or removed " + sys.argv[5])
'''


class Quiet(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # a client that stops reading a large answer is the case under test


class Gateway:
    """A loopback gateway: `reply` is (status, headers, body) or None to hang; every request is recorded."""

    def __init__(self, principal):
        self.requests, self.release = [], threading.Event()
        self.reply = (200, [("Content-Type", "application/json")],
                      json.dumps({"service": "deedkit", "principal": principal, "tools": []}).encode())
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append({"method": self.command, "path": self.path, "body": body,
                                       "authorization": self.headers.get("Authorization")})
                if outer.reply is None:
                    outer.release.wait(10)
                    return
                if callable(outer.reply):
                    outer.reply(self, outer.release)
                    return
                status, headers, payload = outer.reply
                self.send_response(status)
                for header in headers + [("Content-Length", str(len(payload)))]:
                    self.send_header(*header)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = Quiet(("127.0.0.1", 0), Handler)
        self.server.daemon_threads, self.server.block_on_close = True, False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/deedkit/mcp"

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


class Mac:
    def __init__(self, tmp_path, monkeypatch, capsys, key="clone"):
        self.root, self.capsys, self.key = tmp_path, capsys, key
        self.home, self.bin, self.claude_dir, self.host = (tmp_path / n for n in ("home", "bin", "claude", "host"))
        for path in (self.home, self.bin, self.claude_dir, self.host):
            path.mkdir()
        for name in ("ssh", "claude"):
            self.stub(name)
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("PATH", str(self.bin))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self.claude_dir))
        self.token = "dk_" + secrets.token_urlsafe(32)
        self.host_token(self.token + "\n")
        self.gateway = Gateway(key)
        self.outputs, self.allowed = [], set()

    def stub(self, name, behaviour=None):
        path = self.bin / name
        path.write_text(STUB.replace("PYTHON", sys.executable))
        path.chmod(0o755)
        if behaviour is not None:
            (self.bin / (name + ".json")).write_text(json.dumps(behaviour))

    def host_token(self, content, **behaviour):
        """What the host's `cat` prints: the files live in the host directory, which the leak check skips."""
        (self.host / "stdout").write_bytes(content.encode() if isinstance(content, str) else content)
        (self.host / "stderr").write_text(behaviour.pop("stderr", ""))
        self.stub("ssh", {"stdout": str(self.host / "stdout"), "stderr": str(self.host / "stderr"), **behaviour})

    @property
    def directory(self):
        return self.home / ".config/deedkit"

    @property
    def token_file(self):
        return self.directory / f"{self.key}.token"

    @property
    def config_file(self):
        return self.claude_dir / ".claude.json"

    def config(self):
        return json.loads(self.config_file.read_text()) if self.config_file.exists() else {}

    def run(self, *argv):
        code = mac.main(list(argv))
        out, err = self.capsys.readouterr()
        self.outputs += [out, err]
        return code, out, err

    def install(self, *extra, key=None, url=None):
        return self.run("install", key or self.key, "--host", "deedkit-host", "--url", url or self.gateway.url, *extra)

    def calls(self, name=None):
        log = self.bin / "calls.jsonl"
        rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [row for row in rows if name is None or row[0] == name]

    def helper(self):
        return mac.helper_command(os.path.realpath(sys.executable), self.key)

    def entry(self, url=None):
        return {"type": "http", "url": url or self.gateway.url, "headersHelper": self.helper()}

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.lstat().st_mode, p.read_bytes() if p.is_file() and not p.is_symlink()
                                                else None)
                for p in sorted(self.root.rglob("*")) if "calls.jsonl" not in p.name and not p.name.endswith(".pid")}

    def lock_is_free(self):
        lock = os.open(self.directory / ".lock", os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
        finally:
            os.close(lock)

    def assert_no_leak(self):
        """Criterion 8: neither the token nor any 8-character piece of it in an argv, an output stream or a file
        outside the token file (and the host directory, which stands for the host's own copy)."""
        pieces = {self.token[i:i + 8] for i in range(len(self.token) - 7)}
        texts = [("output", text) for text in self.outputs]
        texts += [(f"argv {row}", " ".join(row)) for row in self.calls()]
        for path in self.root.rglob("*"):
            if path.is_file() and not path.is_symlink() and self.host not in path.parents \
                    and path != self.token_file and path not in self.allowed:
                texts.append((str(path), path.read_bytes().decode("latin-1")))
        for where, text in texts:
            assert not any(piece in text for piece in pieces), f"token piece in {where}"


@pytest.fixture
def site(tmp_path, monkeypatch, capsys):
    site = Mac(tmp_path, monkeypatch, capsys)
    yield site
    site.gateway.close()
    site.assert_no_leak()


def dead(pid_file, wait=5.0):
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


# --- criterion 1: command and inputs --------------------------------------------------------------------------------


# spec: ANYM-498
def test_the_id_pattern_is_the_admin_cli_one_and_names_map_injectively():
    assert mac.NAME.pattern == admin.NAME.pattern
    assert mac.server_name("a.b") == "deedkit-a_b" and mac.server_name("a-b") == "deedkit-a-b"


# spec: ANYM-498
@pytest.mark.parametrize("argv, line", [
    (["install", "Clone", "--host", "h", "--url", "https://g.test/mcp"], mac.BAD_ID),
    (["install", "a_b", "--host", "h", "--url", "https://g.test/mcp"], mac.BAD_ID),
    (["install", "clone", "--host=-oProxyCommand=touch pwned", "--url", "https://g.test/mcp"], mac.BAD_HOST),
    (["install", "clone", "--host", "h", "--url", "http://g.test/deedkit/mcp"], mac.BAD_URL),
    (["install", "clone", "--host", "h", "--url", "https://g.test/deedkit/v1"], mac.BAD_URL),
    (["install", "clone", "--host", "h", "--url", "https://user@g.test/mcp"], mac.BAD_URL),
    (["install", "clone", "--host", "h", "--url", "https://g.test/mcp?x=1"], mac.BAD_URL),
    (["install", "clone", "--host", "h", "--url", "ftp://g.test/mcp"], mac.BAD_URL),
    # Astra r1 F2: URLs http.client cannot send are refused before the fetch, not after the writes.
    (["install", "clone", "--host", "h", "--url", "https://bad host/mcp"], mac.BAD_URL),
    (["install", "clone", "--host", "h", "--url", "https://g_x.test/mcp"], mac.BAD_URL),
    (["install", "clone", "--host", "h", "--url", "https://g.test/dé/mcp"], mac.BAD_URL),
    (["install", "clone", "--host", "h", "--url", "https://g.test/a b/mcp"], mac.BAD_URL),
    (["install", "clone", "--host", "h", "--url", "https://g.test:99999/mcp"], mac.BAD_URL),
    (["install", "clone", "--host", "h", "--url", "https://g.test/x\n/mcp"], mac.BAD_URL),
    (["uninstall", "a_b"], mac.BAD_ID),
])
def test_a_bad_input_is_refused_before_any_call_or_write(site, argv, line):
    before = site.snapshot()
    assert site.run(*argv) == (1, "", line + "\n")
    assert site.calls() == [] and site.snapshot() == before and not (site.home / ".config").exists()
    assert site.gateway.requests == []


# spec: ANYM-498
@pytest.mark.parametrize("missing", ["ssh", "claude"])
def test_a_missing_executable_is_refused_before_any_call_or_write(site, missing):
    (site.bin / missing).rename(site.root / f"moved-{missing}")
    assert site.install() == (1, "", f"{missing} is not on PATH; nothing changed\n")
    assert site.calls() == [] and not (site.home / ".config").exists() and site.gateway.requests == []


# spec: ANYM-498
def test_a_dotted_id_gets_an_underscore_name_and_its_own_token_path(site):
    site.key = "clone.work"
    site.gateway.close()
    site.gateway = Gateway("clone.work")
    code, out, err = site.install()
    assert code == 0, err
    assert site.calls("ssh")[0][-1] == "/root/deedkit-admin/tokens/clone.work.token"
    assert site.calls("claude")[0][5] == "deedkit-clone_work" and out.startswith("deedkit-clone_work: user scope")
    assert site.token_file.name == "clone.work.token" and site.token_file.exists()


# --- criterion 2: fetch ---------------------------------------------------------------------------------------------


# spec: ANYM-498
@pytest.mark.parametrize("behaviour, line", [
    ({"code": 255}, "ssh to deedkit-host failed; check: ssh deedkit-host true"),
    ({"code": 1}, "deedkit-host has no readable token for clone; issue it with deedkit admin key issue or check the ssh user"),
    ({"stdout": "x" * 2048}, "the host did not return a gateway token for clone"),
    ({"stdout": "TWO_LINES"}, "the host did not return a gateway token for clone"),
    ({"stdout": "dk_short\n"}, "the host did not return a gateway token for clone"),
    ({"stdout": ""}, "the host did not return a gateway token for clone"),
    ({"hang": True}, "ssh to deedkit-host timed out"),
])
def test_a_failed_fetch_writes_nothing_and_prints_one_fixed_line(site, monkeypatch, behaviour, line):
    monkeypatch.setattr(mac, "FETCH_TIMEOUT", 5 if behaviour.get("hang") else 60)
    stdout = behaviour.pop("stdout", site.token + "\n")
    stdout = f"{site.token}\n{site.token}\n" if stdout == "TWO_LINES" else stdout
    site.host_token(stdout, stderr=f"\x1b[2J{site.token} hostile stderr\n", **behaviour)
    started = time.monotonic()
    assert site.install() == (1, "", line + "\n")
    assert time.monotonic() - started < 30
    assert site.calls("ssh") == [["ssh", "-T", "-o", "BatchMode=yes", "--", "deedkit-host", "cat", "--",
                                  "/root/deedkit-admin/tokens/clone.token"]]
    assert site.calls("claude") == [] and not (site.home / ".config").exists() and site.gateway.requests == []
    if behaviour.get("hang"):
        assert dead(site.bin / "ssh.pid")


# --- criteria 3-6: a fresh install, its files, the proof and the registration ---------------------------------------


# spec: ANYM-498
def test_install_stores_a_private_token_proves_it_and_adds_one_user_scope_server(site):
    code, out, err = site.install()
    assert code == 0, err
    assert out == (f"deedkit-clone: user scope, token file {site.token_file}, verified as clone; "
                   "restart Claude Code or run /mcp to connect\n") and err == ""
    assert site.calls() == [
        ["ssh", "-T", "-o", "BatchMode=yes", "--", "deedkit-host", "cat", "--", "/root/deedkit-admin/tokens/clone.token"],
        ["claude", "mcp", "add-json", "--scope", "user", "deedkit-clone", json.dumps(site.entry())]]
    assert site.config()["mcpServers"] == {"deedkit-clone": site.entry()}
    assert stat.S_IMODE(site.directory.stat().st_mode) == 0o700
    for path in (site.token_file, site.directory / "headers.py"):
        assert stat.S_IMODE(path.lstat().st_mode) == 0o600 and stat.S_ISREG(path.lstat().st_mode)
    assert site.token_file.read_text() == site.token + "\n"
    assert (site.directory / "headers.py").read_bytes() == Path(mac.HELPER).read_bytes()
    assert sorted(p.name for p in site.directory.iterdir()) == [".lock", "clone.token", "headers.py"]
    assert site.gateway.requests == [{"method": "POST", "path": "/deedkit/v1/platform_capabilities", "body": b"{}",
                                      "authorization": f"Bearer {site.token}"}]
    assert site.lock_is_free()


# spec: ANYM-498
def test_a_rerun_keeps_the_files_and_adds_nothing(site):
    assert site.install()[0] == 0
    before = site.snapshot()
    code, out, err = site.install()
    assert code == 0, err
    assert out.startswith("deedkit-clone: user scope") and len(site.calls("claude")) == 1
    assert site.snapshot() == before


# spec: ANYM-498
def test_a_token_without_a_trailing_newline_is_accepted(site):
    site.host_token(site.token)
    assert site.install()[0] == 0 and site.token_file.read_text() == site.token + "\n"


def private_directory(site):
    site.directory.mkdir(parents=True, mode=0o700)
    return site.directory


def unsafe_cases():
    def directory_symlink(site):
        target = site.home / "elsewhere"
        target.mkdir(mode=0o700)
        (site.home / ".config").mkdir()
        site.directory.symlink_to(target)

    def token_symlink(site):
        private_directory(site)
        target = site.home / "elsewhere.token"
        target.write_text(site.token + "\n")
        target.chmod(0o600)
        site.allowed.add(target)
        site.token_file.symlink_to(target)

    def token_fifo(site):
        os.mkfifo(private_directory(site) / "clone.token", 0o600)

    def directory_mode(site):
        private_directory(site).chmod(0o755)

    def token_mode(site):
        private_directory(site)
        site.token_file.write_text(site.token + "\n")
        site.token_file.chmod(0o644)

    def other_token(site):
        private_directory(site)
        site.token_file.write_text("dk_" + secrets.token_urlsafe(32) + "\n")
        site.token_file.chmod(0o600)

    def foreign_directory(site, monkeypatch):
        private_directory(site)
        monkeypatch.setattr(mac, "uid", lambda: os.getuid() + 1)

    def foreign_token(site, monkeypatch):
        private_directory(site)
        site.token_file.write_text(site.token + "\n")
        site.token_file.chmod(0o600)
        monkeypatch.setattr(mac, "mine", lambda st: st.st_uid == os.getuid() and not stat.S_ISREG(st.st_mode))

    def helper_symlink(site):
        target = site.home / "elsewhere.py"
        target.write_bytes(Path(mac.HELPER).read_bytes())
        target.chmod(0o600)
        (private_directory(site) / "headers.py").symlink_to(target)

    def helper_fifo(site):
        os.mkfifo(private_directory(site) / "headers.py", 0o600)

    def helper_mode(site):
        (private_directory(site) / "headers.py").write_bytes(Path(mac.HELPER).read_bytes())
        (site.directory / "headers.py").chmod(0o644)

    directory = "{d} is not a private directory of yours (mode 0700); nothing changed"
    file = "{d}/clone.token is not a private file of yours (mode 0600); nothing changed"
    helper = "{d}/headers.py is not a private file of yours (mode 0600); nothing changed"
    other = "{d}/clone.token holds another token; nothing changed: run key uninstall clone first"
    return [pytest.param(directory_symlink, directory, id="directory-symlink"),
            pytest.param(token_symlink, file, id="token-symlink"), pytest.param(token_fifo, file, id="token-fifo"),
            pytest.param(directory_mode, directory, id="directory-mode"), pytest.param(token_mode, file, id="token-mode"),
            pytest.param(other_token, other, id="other-token"),
            pytest.param(foreign_directory, directory, id="foreign-directory"),
            pytest.param(foreign_token, file, id="foreign-token"),
            # Astra r1 F3: an unsafe helper refuses before the token file is written.
            pytest.param(helper_symlink, helper, id="helper-symlink"), pytest.param(helper_fifo, helper, id="helper-fifo"),
            pytest.param(helper_mode, helper, id="helper-mode")]


# spec: ANYM-498
@pytest.mark.parametrize("prepare, line", unsafe_cases())
def test_an_unsafe_directory_or_token_file_is_refused_and_nothing_changes(site, monkeypatch, prepare, line):
    if prepare.__code__.co_argcount == 2:
        prepare(site, monkeypatch)
    else:
        prepare(site)
    before = {k: v for k, v in site.snapshot().items() if not k.endswith(".lock")}
    assert site.install() == (1, "", line.format(d=site.directory) + "\n")
    assert {k: v for k, v in site.snapshot().items() if not k.endswith(".lock")} == before
    assert site.calls("claude") == [] and site.gateway.requests == [] and site.config() == {}


# spec: ANYM-498
def test_a_staging_file_left_by_a_killed_run_is_removed_by_the_next_run(site, monkeypatch):
    class Killed(BaseException):
        pass

    def kill(fd):
        raise Killed

    with monkeypatch.context() as patch:
        patch.setattr(mac.os, "fsync", kill)
        with pytest.raises(Killed):
            site.install()
    site.capsys.readouterr()
    left = [p.name for p in site.directory.iterdir() if p.name.endswith(".tmp")]
    assert len(left) == 1 and mac.STAGING.fullmatch(left[0]) and not site.token_file.exists()
    site.allowed.add(site.directory / left[0])
    unrelated = site.directory / ".notes.tmp"
    unrelated.write_text("mine")
    assert site.install()[0] == 0
    assert sorted(p.name for p in site.directory.iterdir()) == [".lock", ".notes.tmp", "clone.token", "headers.py"]


# spec: ANYM-498
@pytest.mark.parametrize("action", [["install"], ["uninstall"]])
def test_a_concurrent_run_is_refused(site, action):
    private_directory(site)
    lock = os.open(site.directory / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = site.install() if action == ["install"] else site.run("uninstall", "clone")
        assert result == (1, "", "another key install or uninstall is running\n")
    finally:
        os.close(lock)
    assert site.calls("claude") == [] and not site.token_file.exists()


# --- criterion 4: the headers helper --------------------------------------------------------------------------------


# spec: ANYM-498
def test_the_helper_is_pinned_to_the_interpreter_and_quoted_for_any_home(site, monkeypatch):
    home = site.root / "my home 'q' \"d\" $(touch pwned) `touch pwned2`"
    home.mkdir()
    site.home = home
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(site.root)
    assert site.install()[0] == 0
    helper = site.config()["mcpServers"]["deedkit-clone"]["headersHelper"]
    words = shlex.split(helper)
    assert words == [os.path.realpath(sys.executable), "-I", "-S", str(home / ".config/deedkit/headers.py"), "clone"]
    assert helper.startswith(shlex.quote(os.path.realpath(sys.executable)) + " ")
    fake = site.root / "fakebin"
    fake.mkdir()
    (fake / "python3").write_text(STUB.replace("PYTHON", sys.executable).replace('here / "calls.jsonl"',
                                                                                  f'Path({str(site.bin)!r}) / "calls.jsonl"'))
    (fake / "python3").chmod(0o755)
    printed = subprocess.run(["/bin/sh", "-c", helper], env={"PATH": f"{fake}:/usr/bin:/bin", "HOME": "/nonexistent"},
                             capture_output=True, check=True, cwd=site.root).stdout
    assert json.loads(printed) == {"Authorization": f"Bearer {site.token}"}
    assert site.calls("python3") == [] and not list(site.root.rglob("pwned*"))


# spec: ANYM-498
def test_the_helper_source_parses_as_python_3_9():
    ast.parse(Path(mac.HELPER).read_text(), feature_version=(3, 9))


def helper_cases():
    def missing(path, token):
        pass

    def wide(path, token):
        path.write_text(token + "\n")
        path.chmod(0o644)

    def directory(path, token):
        path.mkdir(mode=0o700)

    def symlink(path, token):
        target = path.with_name("target")
        target.write_text(token + "\n")
        target.chmod(0o600)
        path.symlink_to(target)

    def oversized(path, token):
        path.write_text(token + "\n" + " " * 1024)
        path.chmod(0o600)

    def malformed(path, token):
        path.write_text(token + "x\n")
        path.chmod(0o600)

    return [pytest.param(f, id=f.__name__) for f in (missing, wide, directory, symlink, oversized, malformed)]


# spec: ANYM-498
@pytest.mark.parametrize("prepare", helper_cases())
def test_the_helper_prints_nothing_for_a_missing_replaced_or_bad_token_file(tmp_path, prepare):
    directory = tmp_path / "deedkit"
    directory.mkdir(mode=0o700)
    helper = directory / "headers.py"
    helper.write_bytes(Path(mac.HELPER).read_bytes())
    token = "dk_" + secrets.token_urlsafe(32)
    prepare(directory / "clone.token", token)
    result = subprocess.run([sys.executable, "-I", "-S", str(helper), "clone"], capture_output=True, timeout=30)
    assert result.returncode != 0 and result.stdout == b"" and token.encode() not in result.stderr


# spec: ANYM-498
def test_the_helper_refuses_an_id_that_is_not_a_key_id(tmp_path):
    result = subprocess.run([sys.executable, "-I", "-S", str(mac.HELPER), "../clone"], capture_output=True, timeout=30)
    assert result.returncode != 0 and result.stdout == b""


# --- criterion 5: proof before registration -------------------------------------------------------------------------


def body(value):
    return (200, [("Content-Type", "application/json")], json.dumps(value).encode())


# spec: ANYM-498
@pytest.mark.parametrize("reply, line", [
    (body({"service": "deedkit", "principal": "other"}), "the token belongs to another key"),
    (body({"principal": "clone"}), "the gateway at {url} is not a DeedKit gateway"),
    (body({"service": "other", "principal": "clone"}), "the gateway at {url} is not a DeedKit gateway"),
    (body({"service": "deedkit"}), "the gateway answer names no principal"),
    (body(["deedkit", "clone"]), "the gateway answer is not a JSON object"),
    ((200, [("Content-Type", "text/html")], b"<html>login</html>"), "the gateway answer is not a JSON object"),
    ((200, [("Content-Type", "application/json")], b'{"service": "deedkit",'), "the gateway answer is not a JSON object"),
    ((200, [], b"[" * (2 * 1024 * 1024)), "the gateway answer exceeds 1 MiB"),
    ((401, [], b'{"error": "unauthorized"}'),
     "token rejected by the gateway: check the URL, then deedkit admin key list on the host (expired or revoked key)"),
    ((503, [], b"unavailable"), "the gateway answered HTTP 503"),
    ((503, [], b"TOKEN"), "the gateway answered HTTP 503"),
    (None, "the gateway at {url} did not answer within 1 s"),
])
def test_a_failed_proof_adds_no_entry_and_keeps_the_token_file(site, monkeypatch, reply, line):
    monkeypatch.setattr(mac, "HTTP_TIMEOUT", 1)
    if reply is not None and reply[2] == b"TOKEN":
        reply = (503, [], f"echo {site.token}".encode())
    site.gateway.reply = reply
    assert site.install() == (1, "", line.format(url=site.gateway.url) + "\n")
    assert site.calls("claude") == [] and site.config() == {}
    assert site.token_file.read_text() == site.token + "\n" and site.lock_is_free()
    assert [(r["method"], r["path"], r["body"], r["authorization"]) for r in site.gateway.requests] == [
        ("POST", "/deedkit/v1/platform_capabilities", b"{}", f"Bearer {site.token}")]


# spec: ANYM-498
def test_a_trickled_answer_or_a_stalled_lookup_ends_at_the_deadline(site, monkeypatch):
    # Astra r1 F1: one hard deadline across DNS, connect, headers and body, not a per-read timeout.
    def trickle(handler, release):
        for byte in b"HTTP/1.0 401 Unauthorized\r\nX-Slow: " + b"x" * 200:
            if release.wait(0.2):
                return
            handler.wfile.write(bytes([byte]))
            handler.wfile.flush()

    monkeypatch.setattr(mac, "HTTP_TIMEOUT", 1)
    site.gateway.reply = trickle
    started = time.monotonic()
    assert site.install() == (1, "", f"the gateway at {site.gateway.url} did not answer within 1 s\n")
    assert time.monotonic() - started < 30 and site.calls("claude") == [] and site.lock_is_free()
    lookup, stalled = mac.socket.getaddrinfo, threading.Event()
    monkeypatch.setattr(mac.socket, "getaddrinfo", lambda *a, **k: stalled.wait(10) and lookup(*a, **k))
    url = site.gateway.url.replace("127.0.0.1", "localhost")
    try:
        started = time.monotonic()
        assert site.install(url=url) == (1, "", f"the gateway at {url} did not answer within 1 s\n")
        assert time.monotonic() - started < 30 and site.calls("claude") == [] and site.lock_is_free()
    finally:
        stalled.set()


# spec: ANYM-498
def test_a_redirect_is_not_followed(site):
    second = Gateway("clone")
    try:
        site.gateway.reply = (302, [("Location", second.url.replace("/mcp", "/v1/platform_capabilities"))], b"")
        assert site.install() == (1, "", "the gateway answered HTTP 302 (a redirect, not followed): check --url\n")
        assert second.requests == [] and len(site.gateway.requests) == 1 and site.calls("claude") == []
    finally:
        second.close()


# spec: ANYM-498
def test_a_failed_proof_then_a_rerun_adds_the_entry_once(site):
    site.gateway.reply = (401, [], b"")
    assert site.install()[0] == 1
    site.gateway.reply = body({"service": "deedkit", "principal": "clone"})
    code, out, err = site.install()
    assert code == 0, err
    assert len(site.calls("claude")) == 1 and site.config()["mcpServers"] == {"deedkit-clone": site.entry()}


# spec: ANYM-498
def test_a_hanging_helper_is_killed_and_the_next_run_takes_the_lock(site, monkeypatch):
    site.stub("slowpython", {"hang": True})
    with monkeypatch.context() as patch:
        patch.setattr(mac, "HELPER_TIMEOUT", 5)
        patch.setattr(mac, "interpreter", lambda: str(site.bin / "slowpython"))
        started = time.monotonic()
        assert site.install() == (1, "", "the headers helper timed out; nothing was added to Claude Code\n")
        assert time.monotonic() - started < 30
    assert dead(site.bin / "slowpython.pid") and site.calls("claude") == [] and site.lock_is_free()
    assert site.install()[0] == 0 and len(site.calls("claude")) == 1


# --- criterion 6: registration --------------------------------------------------------------------------------------


# spec: ANYM-498
def test_a_null_entry_with_the_name_is_refused_before_any_write(site):
    # Astra r1 F4: `"deedkit-clone": null` is another configuration, not an absent entry.
    site.config_file.write_text(json.dumps({"mcpServers": {"deedkit-clone": None}}))
    assert site.install() == (1, "", "deedkit-clone exists with another configuration; run key uninstall clone first\n")
    assert site.calls("claude") == [] and not site.token_file.exists() and site.gateway.requests == []


# spec: ANYM-498
def test_another_entry_with_the_name_is_refused_before_any_write(site):
    site.config_file.write_text(json.dumps({"mcpServers": {"deedkit-clone": site.entry(url="https://old.test/mcp")}}))
    before = site.config_file.read_bytes()
    assert site.install() == (1, "", "deedkit-clone exists with another configuration; run key uninstall clone first\n")
    assert site.config_file.read_bytes() == before and site.calls("claude") == [] and not site.token_file.exists()
    assert site.gateway.requests == []


# spec: ANYM-498
def test_a_local_scope_entry_of_the_same_name_is_reported(site):
    site.config_file.write_text(json.dumps({"projects": {"/work/a": {"mcpServers": {"deedkit-clone": {}}},
                                                         "/work/b": {"mcpServers": {"other": {}}}}}))
    code, out, err = site.install()
    assert code == 0, err
    assert out.splitlines()[1] == "shadowed in: /work/a; remove with claude mcp remove --scope local deedkit-clone there"


def config_cases():
    def fifo(site, monkeypatch):
        os.mkfifo(site.config_file, 0o600)

    def oversized(site, monkeypatch):
        site.config_file.write_text(json.dumps({"padding": "x" * 200}))
        monkeypatch.setattr(mac, "CONFIG_LIMIT", 100)

    def malformed(site, monkeypatch):
        site.config_file.write_text('{"mcpServers": ')

    def not_an_object(site, monkeypatch):
        site.config_file.write_text('["mcpServers"]')

    def unreadable(site, monkeypatch):
        if os.geteuid() == 0:
            pytest.skip("root reads a mode 000 file")
        site.config_file.write_text("{}")
        site.config_file.chmod(0)

    return [pytest.param(f, id=f.__name__) for f in (fifo, oversized, malformed, not_an_object, unreadable)]


# spec: ANYM-498
@pytest.mark.parametrize("prepare", config_cases())
@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_an_unreadable_claude_config_is_refused_promptly(site, monkeypatch, prepare, action):
    prepare(site, monkeypatch)
    before = site.config_file.lstat()
    started = time.monotonic()
    result = site.install() if action == "install" else site.run("uninstall", "clone")
    assert result == (1, "", f"cannot read Claude Code config {site.config_file}; nothing changed\n")
    assert time.monotonic() - started < 30
    after = site.config_file.lstat()
    assert (after.st_mode, after.st_size, after.st_mtime_ns) == (before.st_mode, before.st_size, before.st_mtime_ns)
    assert site.calls("claude") == [] and not site.token_file.exists() and site.lock_is_free()
    if stat.S_ISREG(after.st_mode):
        site.config_file.chmod(0o600)


# spec: ANYM-498
@pytest.mark.parametrize("action", ["add-json", "remove"])
def test_a_hanging_claude_is_killed_and_the_next_run_takes_the_lock(site, monkeypatch, action):
    if action == "remove":
        assert site.install()[0] == 0
    site.stub("claude", {"hang": True})
    with monkeypatch.context() as patch:
        patch.setattr(mac, "CLAUDE_TIMEOUT", 5)
        started = time.monotonic()
        result = site.install() if action == "add-json" else site.run("uninstall", "clone")
        assert result == (1, "", f"claude mcp {action} timed out; run the command again\n")
        assert time.monotonic() - started < 30
    assert dead(site.bin / "claude.pid") and site.lock_is_free()
    site.stub("claude", {})
    result = site.install() if action == "add-json" else site.run("uninstall", "clone")
    assert result[0] == 0, result


# --- criterion 7: uninstall -----------------------------------------------------------------------------------------


# spec: ANYM-498
def test_uninstall_removes_the_entry_and_the_token_and_a_second_run_is_done(site):
    assert site.install()[0] == 0
    line = (f"removed deedkit-clone and {site.token_file}; the key stays valid on the host: "
            "revoke it with deedkit admin key revoke clone\n")
    assert site.run("uninstall", "clone") == (0, line, "")
    assert site.calls("claude")[-1] == ["claude", "mcp", "remove", "--scope", "user", "deedkit-clone"]
    assert site.config()["mcpServers"] == {} and not site.token_file.exists()
    assert (site.directory / "headers.py").exists()
    assert site.run("uninstall", "clone") == (0, line, "") and len(site.calls("claude")) == 2


# spec: ANYM-498
def test_a_url_with_an_at_sign_in_its_path_or_a_terminal_dot_installs_and_uninstalls(site):
    # Sol r2: only user info is refused; `@` is a valid path character and `host.` a valid DNS name.
    assert mac.valid_url("https://gateway.example.com/team@work/mcp") and mac.valid_url("https://gateway.example.com./mcp")
    assert not mac.valid_url("https://user@gateway.example.com/mcp")
    url = site.gateway.url.replace("/deedkit/mcp", "/team@work/mcp")
    code, out, err = site.install(url=url)
    assert code == 0, err
    assert site.gateway.requests[-1]["path"] == "/team@work/v1/platform_capabilities"
    assert site.run("uninstall", "clone")[0] == 0 and site.config()["mcpServers"] == {}
    site.config_file.write_text(json.dumps({"mcpServers": {"deedkit-clone": site.entry(url="https://gateway.example.com./mcp")}}))
    assert site.run("uninstall", "clone")[0] == 0 and site.config()["mcpServers"] == {}


# spec: ANYM-498
def test_uninstall_accepts_an_entry_pinned_to_another_interpreter(site):
    assert site.install()[0] == 0
    value = site.config()
    value["mcpServers"]["deedkit-clone"]["headersHelper"] = mac.helper_command("/old/python3.12", "clone")
    site.config_file.write_text(json.dumps(value))
    assert site.run("uninstall", "clone")[0] == 0 and site.config()["mcpServers"] == {}


# spec: ANYM-498
@pytest.mark.parametrize("foreign", [
    {"type": "http", "url": "https://g.test/mcp",
     "headersHelper": 'printf \'{"Authorization":"Bearer %s"}\' "$(cat ~/.config/deedkit/clone.token)"'},
    {"type": "stdio", "command": "deedkit"},
    None,  # Astra r1 F4: a null entry is an entry
])
def test_uninstall_refuses_an_entry_install_did_not_write(site, foreign):
    assert site.install()[0] == 0
    value = site.config()
    value["mcpServers"]["deedkit-clone"] = foreign
    site.config_file.write_text(json.dumps(value))
    before = site.snapshot()
    assert site.run("uninstall", "clone") == (
        1, "", "deedkit-clone was not added by key install; remove it with claude mcp remove --scope user deedkit-clone\n")
    assert site.snapshot() == before and len(site.calls("claude")) == 1
