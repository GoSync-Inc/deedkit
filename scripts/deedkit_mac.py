#!/usr/bin/env python3
"""`deedkit key install|uninstall` (ANYM-498): put a key `deedkit admin key issue` made into Claude Code on the Mac.

`install <id> --host <ssh destination> --url <gateway MCP URL>` reads the host's gateway token over SSH into
memory, stores it as ~/.config/deedkit/<id>.token (0600 in a 0700 directory), proves through the headers helper
that the gateway accepts it as <id>, and only then adds the user-scope Claude Code server `deedkit-<id>` whose
headers helper reads that file on each connection: the token is never in argv, ~/.claude.json or any output.
`uninstall <id>` removes the entry this command wrote and the token file. Every child runs from an argv list in its
own process group under a deadline. Python 3.11+, standard library only.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, suppress
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shlex
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,49}")  # deedkit_admin.NAME, the IDs `key issue` accepts
TOKEN = re.compile(rb"dk_[A-Za-z0-9_-]{43}\n?")  # deedkit_admin issue(): "dk_" + secrets.token_urlsafe(32)
TOKENS = "/root/deedkit-admin/tokens"  # deedkit_admin.TOKENS, where `key issue` leaves the gateway token
STAGING = re.compile(r"\.(?:headers\.py|[a-z0-9][a-z0-9.-]{0,49}\.token)\.[A-Za-z0-9_-]{16}\.tmp")
HOSTNAME = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.?")
URL_PATH = re.compile(r"(?:/(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2})*)*/mcp")
MISSING = object()  # no entry of that name, unlike an entry whose value is null
HELPER = Path(__file__).resolve().with_name("deedkit_headers.py")  # installed as headers.py
FETCH_TIMEOUT, HELPER_TIMEOUT, HTTP_TIMEOUT, CLAUDE_TIMEOUT = 30, 10, 15, 30  # seconds
TOKEN_LIMIT, OUTPUT_LIMIT, HELPER_LIMIT = 1024, 4096, 64 * 1024  # bytes
RESPONSE_LIMIT, CONFIG_LIMIT = 1024 * 1024, 64 * 1024 * 1024
BAD_ID = "invalid key ID: use the ID deedkit admin key issue printed (lowercase letters, digits, dots, hyphens)"
BAD_HOST = "the ssh destination must not be empty or start with -"
BAD_URL = "--url must be https://<host>/.../mcp (http:// only for a loopback host)"


class Refused(Exception):
    """One fixed line naming the cause: never a token, a child's output or a response body."""


def uid() -> int:
    return os.getuid()


def mine(info: os.stat_result) -> bool:
    return info.st_uid == uid()


def interpreter() -> str:
    return os.path.realpath(sys.executable)


def server_name(key: str) -> str:
    return "deedkit-" + key.replace(".", "_")  # Claude Code names allow no dot; `_` is in no key ID


def private_path() -> Path:
    return Path(os.path.abspath(os.path.expanduser("~"))) / ".config" / "deedkit"


def helper_command(python: str, key: str) -> str:
    return f"{shlex.quote(python)} -I -S {shlex.quote(str(private_path() / 'headers.py'))} {shlex.quote(key)}"


def loopback(host: str) -> bool:
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def valid_url(url: str) -> bool:
    """`https://<host>[:port]/…/mcp` that http.client can send as is: a DNS name or IP literal, a plain ASCII path,
    no user info, query or fragment; `http://` only for a loopback host."""
    if not url.isascii() or not url.isprintable() or " " in url or "?" in url or "#" in url:
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not HOSTNAME.fullmatch(host):
            return False
    if port == 0 or "@" in parts.netloc or not URL_PATH.fullmatch(parts.path):
        return False
    try:  # the connection target the proof builds, checked before any fetch or write (no I/O here)
        http.client.HTTPConnection(host, port or (443 if parts.scheme == "https" else 80))
    except (ValueError, http.client.HTTPException):
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and loopback(host))


def run(argv: list[str], timeout: float, limit: int | None = None) -> tuple[int | str, bytes]:
    """(exit code, stdout) of one child, or ("timeout" | "oversized", stdout): no shell, stdin /dev/null, its own
    process group killed and reaped on the deadline, an oversized stdout or an interrupt. stderr is read (bounded)
    and dropped; stdout beyond `limit` ends the child, without `limit` it is drained and truncated."""
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
    stdout, stderr = child.stdout, child.stderr
    assert stdout is not None and stderr is not None
    deadline, out, err, status = time.monotonic() + timeout, bytearray(), bytearray(), None
    try:
        with selectors.DefaultSelector() as streams:
            streams.register(stdout, selectors.EVENT_READ, out)
            streams.register(stderr, selectors.EVENT_READ, err)
            while streams.get_map() and status is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    status = "timeout"
                    break
                for key, _ in streams.select(remaining):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        streams.unregister(key.fileobj)
                        continue
                    key.data.extend(chunk)
                    if key.data is out and limit is not None and len(out) > limit:
                        status = "oversized"
                    del out[OUTPUT_LIMIT if limit is None else limit + 1:], err[OUTPUT_LIMIT:]
        if status is None:
            try:
                status = child.wait(max(deadline - time.monotonic(), 0))
            except subprocess.TimeoutExpired:
                status = "timeout"
        return status, bytes(out)
    finally:
        if child.returncode is None:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        stdout.close()
        stderr.close()


def read_bounded(fd: int, limit: int) -> bytes | None:
    data = bytearray()
    while len(data) <= limit:
        chunk = os.read(fd, min(1024 * 1024, limit + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data += chunk
    return None


# --- the private directory ------------------------------------------------------------------------------------------


@contextmanager
def private_directory():
    """The descriptor of ~/.config/deedkit (created 0700) under the exclusive `.lock`; every later access is relative
    to it with O_NOFOLLOW. Stale staging files are removed first: under the lock every one is a dead run's."""
    path = private_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(FileExistsError):
        os.mkdir(path, 0o700)
    unsafe = f"{path} is not a private directory of yours (mode 0700); nothing changed"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise Refused(unsafe) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or not mine(info) or info.st_mode & 0o077:
            raise Refused(unsafe)
        lock = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused("another key install or uninstall is running") from None
            for name in os.listdir(fd):
                if STAGING.fullmatch(name):
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and mine(info):
                        os.unlink(name, dir_fd=fd)
            yield fd
        finally:
            os.close(lock)
    finally:
        os.close(fd)


def existing(fd: int, name: str, limit: int = TOKEN_LIMIT) -> bytes | None:
    """The content of `name` when it is a regular 0600 file of the user of at most `limit` bytes, None when absent;
    anything else (a symlink, FIFO, directory, another owner or mode) is refused."""
    unsafe = f"{private_path() / name} is not a private file of yours (mode 0600); nothing changed"
    try:
        file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise Refused(unsafe) from None
    try:
        info = os.fstat(file)
        if not stat.S_ISREG(info.st_mode) or not mine(info) or stat.S_IMODE(info.st_mode) != 0o600 \
                or info.st_size > limit:
            raise Refused(unsafe)
        data = read_bounded(file, limit)
        if data is None:
            raise Refused(unsafe)
        return data
    finally:
        os.close(file)


def publish(fd: int, name: str, data: bytes, replace: bool = False) -> None:
    """`data` as `name` (0600): a synced staging file published by a hard link that never replaces a file, or (the
    shipped helper over its older copy) by a rename; the staging name is gone either way."""
    staging = f".{name}.{secrets.token_urlsafe(12)}.tmp"
    file = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
    try:
        os.fchmod(file, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(file, view):]
        os.fsync(file)
    finally:
        os.close(file)
    try:
        if replace:
            os.rename(staging, name, src_dir_fd=fd, dst_dir_fd=fd)
        else:
            os.link(staging, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(staging, dir_fd=fd)
    os.fsync(fd)


# --- Claude Code's user config (read only) --------------------------------------------------------------------------


def claude_config() -> tuple[Path, dict]:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    path = Path(base) / ".claude.json" if base else Path(os.path.expanduser("~")) / ".claude.json"
    refused = Refused(f"cannot read Claude Code config {path}; nothing changed")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return path, {}
    except OSError:
        raise refused from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > CONFIG_LIMIT:
            raise refused
        data = read_bounded(fd, CONFIG_LIMIT)
        value = json.loads(data) if data is not None else None
    except (OSError, ValueError, RecursionError):
        raise refused from None
    finally:
        os.close(fd)
    if not isinstance(value, dict) or not isinstance(value.get("mcpServers", {}), dict):
        raise refused
    return path, value


def lookup(name: str) -> tuple[object, list[str]]:
    """The user-scope entry `name` (MISSING when absent; null is an entry) and the folders whose local scope has the
    same name (those win in that folder)."""
    _, value = claude_config()
    projects = value.get("projects")
    shadowed = sorted(folder for folder, project in projects.items()
                      if isinstance(project, dict) and isinstance(project.get("mcpServers"), dict)
                      and name in project["mcpServers"]) if isinstance(projects, dict) else []
    return value.get("mcpServers", {}).get(name, MISSING), shadowed


def ours(entry: object, key: str) -> bool:
    """The shape install writes for `key`, with any pinned interpreter (an upgrade may have removed it)."""
    if not isinstance(entry, dict) or set(entry) != {"type", "url", "headersHelper"} or entry["type"] != "http" \
            or not isinstance(entry["url"], str) or not valid_url(entry["url"]) \
            or not isinstance(entry["headersHelper"], str):
        return False
    try:
        python = shlex.split(entry["headersHelper"])[0]
    except (ValueError, IndexError):
        return False
    return os.path.isabs(python) and entry["headersHelper"] == helper_command(python, key)


def claude(executable: str, action: str, *argv: str) -> None:
    status, _ = run([executable, "mcp", action, "--scope", "user", *argv], CLAUDE_TIMEOUT)
    if status == "timeout":
        raise Refused(f"claude mcp {action} timed out; run the command again")
    if status != 0:
        raise Refused(f"claude mcp {action} failed (exit {status}); run the command again")


# --- install --------------------------------------------------------------------------------------------------------


def fetch(ssh: str, destination: str, key: str) -> bytes:
    status, out = run([ssh, "-T", "-o", "BatchMode=yes", "--", destination, "cat", "--", f"{TOKENS}/{key}.token"],
                      FETCH_TIMEOUT, TOKEN_LIMIT)
    if status == "timeout":
        raise Refused(f"ssh to {destination} timed out")
    if status == 255:
        raise Refused(f"ssh to {destination} failed; check: ssh {destination} true")
    if status not in (0, "oversized"):
        raise Refused(f"{destination} has no readable token for {key}; "
                      "issue it with deedkit admin key issue or check the ssh user")
    if status == "oversized" or not TOKEN.fullmatch(out):
        raise Refused(f"the host did not return a gateway token for {key}")
    return out.rstrip(b"\n")


def exchange(url: str, authorization: str) -> tuple[int, bytes]:
    """(status, body up to 1 MiB + 1 byte for a 200) of the one proof request: no proxies, no redirects, the default
    TLS context. One hard deadline covers DNS, connect, TLS, headers and body: the request runs in a daemon thread;
    on the deadline its socket is shut down and the run refuses, so the lock is released (a lookup still stalled in
    DNS ends with the process)."""
    parts = urlsplit(url)
    # An explicit port: without one http.client reads the last group of an IPv6 literal as a port (Sol r3).
    host, port, done = parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80), {}
    try:
        if parts.scheme == "https":
            connection = http.client.HTTPSConnection(host, port, timeout=HTTP_TIMEOUT,
                                                     context=ssl.create_default_context())
        else:
            connection = http.client.HTTPConnection(host, port, timeout=HTTP_TIMEOUT)
    except (ValueError, http.client.HTTPException) as exc:
        raise Refused(f"cannot reach the gateway at {url}: {type(exc).__name__}") from None

    def work() -> None:
        try:
            connection.request("POST", parts.path[:-len("/mcp")] + "/v1/platform_capabilities", body=b"{}",
                               headers={"Authorization": authorization, "Content-Type": "application/json",
                                        "Accept": "application/json"})
            response = connection.getresponse()
            done["result"] = response.status, response.read(RESPONSE_LIMIT + 1) if response.status == 200 else b""
        except BaseException as exc:  # handed to the caller, which names only its class
            done["error"] = exc

    worker = threading.Thread(target=work, name="deedkit-proof", daemon=True)
    worker.start()
    worker.join(HTTP_TIMEOUT)
    if worker.is_alive():
        with suppress(OSError, AttributeError):
            connection.sock.shutdown(socket.SHUT_RDWR)
        raise Refused(f"the gateway at {url} did not answer within {HTTP_TIMEOUT} s")
    connection.close()
    error = done.get("error")
    if isinstance(error, TimeoutError):
        raise Refused(f"the gateway at {url} did not answer within {HTTP_TIMEOUT} s")
    if isinstance(error, (OSError, ValueError, http.client.HTTPException)):
        raise Refused(f"cannot reach the gateway at {url}: {type(error).__name__}")
    if error is not None:
        raise error
    return done["result"]


def prove(helper: str, url: str, key: str, token: bytes) -> None:
    """Run the stored helper as Claude Code does, then one read of the gateway with the header it printed."""
    status, out = run(["/bin/sh", "-c", helper], HELPER_TIMEOUT, TOKEN_LIMIT)
    if status == "timeout":
        raise Refused("the headers helper timed out; nothing was added to Claude Code")
    if status != 0:
        raise Refused(f"the headers helper failed (exit {status}); nothing was added to Claude Code")
    try:
        header = json.loads(out)
    except ValueError:
        header = None
    if header != {"Authorization": "Bearer " + token.decode()}:
        raise Refused("the headers helper printed another header; nothing was added to Claude Code")
    status, body = exchange(url, "Bearer " + token.decode())
    if status == 401:
        raise Refused("token rejected by the gateway: check the URL, then deedkit admin key list on the host "
                      "(expired or revoked key)")
    if 300 <= status < 400:
        raise Refused(f"the gateway answered HTTP {status} (a redirect, not followed): check --url")
    if status != 200:
        raise Refused(f"the gateway answered HTTP {status}")
    if len(body) > RESPONSE_LIMIT:
        raise Refused("the gateway answer exceeds 1 MiB")
    try:
        value = json.loads(body)
    except (ValueError, RecursionError):
        value = None
    if not isinstance(value, dict):
        raise Refused("the gateway answer is not a JSON object")
    if value.get("service") != "deedkit":
        raise Refused(f"the gateway at {url} is not a DeedKit gateway")
    if not isinstance(value.get("principal"), str):
        raise Refused("the gateway answer names no principal")
    if value["principal"] != key:
        raise Refused("the token belongs to another key")


def install(key: str, destination: str, url: str, tools: dict[str, str]) -> list[str]:
    token = fetch(tools["ssh"], destination, key)
    name, helper, path = server_name(key), helper_command(interpreter(), key), private_path() / f"{key}.token"
    wanted = {"type": "http", "url": url, "headersHelper": helper}
    conflict = Refused(f"{name} exists with another configuration; run key uninstall {key} first")
    with private_directory() as fd:
        current, _ = lookup(name)
        if current is not MISSING and current != wanted:
            raise conflict
        # Every check before the first write: a refusal leaves no new file behind.
        stored, source, shipped = existing(fd, path.name), existing(fd, "headers.py", HELPER_LIMIT), HELPER.read_bytes()
        if stored is not None and stored.rstrip(b"\n") != token:
            raise Refused(f"{path} holds another token; nothing changed: run key uninstall {key} first")
        if stored is None:
            publish(fd, path.name, token + b"\n")
        if source != shipped:
            publish(fd, "headers.py", shipped, replace=source is not None)
        prove(helper, url, key, token)
        current, shadowed = lookup(name)
        if current is MISSING:
            claude(tools["claude"], "add-json", name, json.dumps(wanted))
        elif current != wanted:
            raise conflict
    lines = [f"{name}: user scope, token file {path}, verified as {key}; restart Claude Code or run /mcp to connect"]
    if shadowed:
        lines.append(f"shadowed in: {', '.join(shadowed)}; remove with claude mcp remove --scope local {name} there")
    return lines


def uninstall(key: str, tools: dict[str, str]) -> list[str]:
    name, path = server_name(key), private_path() / f"{key}.token"
    with private_directory() as fd:
        current, _ = lookup(name)
        if current is not MISSING and not ours(current, key):
            raise Refused(f"{name} was not added by key install; remove it with claude mcp remove --scope user {name}")
        stored = existing(fd, path.name)  # an unsafe file refuses before anything changes
        if current is not MISSING:
            claude(tools["claude"], "remove", name)
        if stored is not None:
            os.unlink(path.name, dir_fd=fd)
            os.fsync(fd)
    return [f"removed {name} and {path}; the key stays valid on the host: revoke it with deedkit admin key revoke {key}"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deedkit key",
                                     description="put a key deedkit admin key issue made into Claude Code on this Mac")
    actions = parser.add_subparsers(dest="action", required=True)
    add = actions.add_parser("install", help="fetch, prove and add a key to Claude Code (user scope)")
    add.add_argument("id")
    add.add_argument("--host", required=True, help="the ssh destination of the DeedKit host")
    add.add_argument("--url", required=True, help="the gateway MCP URL `deedkit admin key issue` printed")
    actions.add_parser("uninstall", help="remove the Claude Code entry and the token file").add_argument("id")
    args = parser.parse_args(argv)
    try:
        if not NAME.fullmatch(args.id):
            raise Refused(BAD_ID)
        if args.action == "install":
            if not args.host or args.host.startswith("-"):
                raise Refused(BAD_HOST)
            if not valid_url(args.url):
                raise Refused(BAD_URL)
        tools = {}
        for tool in ("ssh", "claude") if args.action == "install" else ("claude",):
            found = shutil.which(tool)
            if found is None:
                raise Refused(f"{tool} is not on PATH; nothing changed")
            tools[tool] = found
        lines = install(args.id, args.host, args.url, tools) if args.action == "install" else uninstall(args.id, tools)
    except Refused as exc:
        print(exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"key {args.action} stopped: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
