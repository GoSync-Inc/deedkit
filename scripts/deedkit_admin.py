#!/usr/bin/env python3
"""DeedKit admin CLI (ANYM-491): every agent key across the gateway, messaging, calendar and calls stores.

`key list` joins the gateway policy, the calls registry and each backend's own operator command (ANYM-133:
messaging `list-keys --all` / `key-info`, calendar `list-agents` / `agent-info`) into one row per gateway
principal, and reports where the stores disagree. It is read-only: no lock, no write, safe during a call.
`key label` keeps a human label in a private admin file that no service reads, so the enforced policy schema
never changes (an older gateway image rejects unknown fields). `key issue --like` (ANYM-496), `key extend`
and `key revoke` (ANYM-497), `profile tools` (the tool names of one gateway profile) and `undo` write under the
shared rules of ANYM-493. Host only, standard library only.

Secrets: key files are read here and passed to the operator commands on stdin, never in argv, environment,
output or logs; the calls registry is matched by hashing in memory. Only public IDs are printed.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import hmac
import importlib.util
import io
import json
import os
import posixpath
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

CONFIG = Path("/opt/deedkit/deploy/private/admin/config.json")
WINDOW = timedelta(days=7)  # the gateway's expiry warning window (ANYM-289, WARNING_WINDOW_DAYS)
TIMEOUT = 120  # seconds for one operator command
CLEANUP = 30  # seconds to stop this invocation's own container after a timeout or an interrupt
LIMIT = 1024 * 1024  # bytes of one private JSON or key file
BATCH = 100  # keys per lookup, the operator commands' own bound
LOOKUP = {"messaging": ["key-info", "--key-file", "/dev/stdin"], "calendar": ["agent-info", "--key-file", "/dev/stdin"]}
INVENTORY = {"messaging": ["list-keys", "--all"], "calendar": ["list-agents"]}
# Write subcommands (ANYM-493 shared rules, production.md "Admin CLI").
TOOL = Path("/usr/local/lib/deedkit/current")  # the installed host tool whose locks and checks `deploy` uses
JOURNAL = Path("/var/lib/deedkit-admin/ops")
TOKENS = Path("/root/deedkit-admin/tokens")
CALLS_LOCK = Path("/var/lib/deedkit-admin/calls-registry.lock")
LOCK_WAIT = 30  # seconds to get every lock before refusing "a deploy is running"
DAYS = (1, 30)  # ANYM-496: one N for every store, within each store's own limit (messaging 365, calendar 183)
NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,49}")  # fits every store's ID rule with the -calls-archive suffix
OPERATION = re.compile(r"[a-z0-9.-]{1,100}")
TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")  # a gateway catalog tool name; the gateway's own check knows which exist
VALIDATORS = {"policy": ["deedkit-gateway", "--check", "--policy", "{path}", "--public-url", "https://check.invalid"],
              "registry": ["python", "agent_api.py", "--check-registry", "{path}"]}
BACKENDS = ("messaging", "calendar", "calls")


class AdminError(Exception):
    """A refusal or unreadable input; the message names a file or a service, never a secret."""


class Unavailable(Exception):
    pass


def read_private(path: str | Path, limit: int = LIMIT) -> bytes:
    """A regular, owner-only file that is not a symlink, read in full and unchanged while read (the gateway's
    own reader); an oversized or growing file is refused, never truncated."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077 or before.st_size > limit:
            raise AdminError(f"unsafe private file: {path}")
        value, after = os.read(fd, limit + 1), os.fstat(fd)
        if len(value) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise AdminError(f"private file changed while read: {path}")
        return value
    finally:
        os.close(fd)


def load_json(path: str | Path, what: str) -> dict:
    try:
        value = json.loads(read_private(path))
    except (OSError, ValueError) as exc:
        raise AdminError(f"{what} unreadable: {path} ({type(exc).__name__})") from None
    if not isinstance(value, dict):
        raise AdminError(f"{what} is not a JSON object: {path}")
    return value


def instant(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def host_path(config: dict, path: str) -> str:
    """A container path (policy key_file, registry archive_config, archive token_file) on the host, by the
    longest configured mount prefix; a path under no prefix is taken as a host path."""
    pure, best = PurePosixPath(path), None
    for prefix in config.get("paths", {}):
        if pure == PurePosixPath(prefix) or PurePosixPath(prefix) in pure.parents:
            if best is None or len(prefix) > len(best):
                best = prefix
    if best is None:
        return path
    return str(Path(config["paths"][best]) / pure.relative_to(best))


def read_token(config: dict, path: str) -> str | None:
    """The token in a key file, or None when it is unreadable or not one whitespace-free line."""
    try:
        token = read_private(host_path(config, path), 1024).decode().strip()
    except (OSError, ValueError, AdminError):
        return None
    return token if token and not any(c.isspace() for c in token) else None


def runner(config: dict):
    """Run one operator command in its one-shot container; argv from the config, a secret only on stdin."""
    def run(service: str, args: list[str], data: bytes | None = None):
        prefix = config.get(service)
        if not isinstance(prefix, list) or not prefix or not all(isinstance(part, str) for part in prefix):
            raise Unavailable(f"{service}: no operator command configured")
        name = "deedkit-admin-" + uuid4().hex[:12]
        argv = [part.replace("{name}", name) for part in prefix] + args
        try:
            done = subprocess.run(argv, input=data if data is not None else b"", capture_output=True,
                                  timeout=TIMEOUT, check=False)
        except OSError as exc:
            raise Unavailable(f"{service}: {type(exc).__name__}") from None
        except BaseException as exc:  # timeout or interrupt: the killed client may leave its container running
            if any("{name}" in part for part in prefix):
                stop(name)
            if isinstance(exc, subprocess.TimeoutExpired):
                raise Unavailable(f"{service}: TimeoutExpired") from None
            raise
        # stderr is dropped: a service's error text is not ours to echo.
        if done.returncode:
            raise Unavailable(f"{service}: {args[0]} exited {done.returncode}")
        try:
            return json.loads(done.stdout)
        except ValueError:
            raise Unavailable(f"{service}: {args[0]} printed no JSON") from None
    return run


def stop(name: str, prefix: list[str] | None = None) -> None:
    """Stop only this invocation's uniquely named container, bounded; `run --rm` makes the daemon remove it."""
    try:
        subprocess.run([*(prefix or ["docker"]), "stop", "--time", "5", name], capture_output=True, timeout=CLEANUP,
                       check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def pinned(token: str, pin: object) -> bool:
    """The token matches the policy's key_sha256; a malformed pin (the gateway refuses it too) never matches."""
    if not isinstance(pin, str) or len(pin) != 64 or not all(c in "0123456789abcdef" for c in pin):
        return False
    return hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), pin)


def status(record: dict | None, now: datetime) -> str:
    if record is None:
        return "missing"
    if record.get("revoked"):
        return "revoked"
    expiry = instant(record.get("expires_at"))
    return "expired" if expiry is None or expiry <= now else "live"


def lookup(run, service: str, tokens: list[str]) -> list[dict | None]:
    """Each token's public record from the service, in order; None where it is not found."""
    result: list[dict | None] = []
    for start in range(0, len(tokens), BATCH):
        chunk = tokens[start:start + BATCH]
        reply = run(service, LOOKUP[service], ("\n".join(chunk) + "\n").encode())
        rows = reply.get("keys" if service == "messaging" else "agents") if isinstance(reply, dict) else None
        if not isinstance(rows, list) or len(rows) != len(chunk) or not all(isinstance(r, dict) for r in rows):
            raise Unavailable(f"{service}: malformed lookup reply")
        result += [row if row.get("found") else None for row in rows]
    return result


def inventory(run, service: str) -> list[dict]:
    reply = run(service, INVENTORY[service])
    rows = reply.get("agents") if service == "calendar" and isinstance(reply, dict) else reply
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise Unavailable(f"{service}: malformed inventory reply")
    return rows


def public_id(service: str, record: dict) -> str | None:
    if service == "messaging":
        return record.get("key_id") or record.get("id")
    return record.get("principal_id") or record.get("id")


def collect(config: dict, run, now: datetime | None = None) -> dict:
    """The joined view: one row per gateway principal and the findings, public fields only."""
    now = now or datetime.now(timezone.utc)
    policy = load_json(config["policy"], "gateway policy")
    principals = policy.get("principals")
    if not isinstance(principals, list) or not all(isinstance(p, dict) and isinstance(p.get("id"), str)
                                                   for p in principals):
        raise AdminError(f"gateway policy has no principal list: {config['policy']}")
    registry = load_json(config["calls_registry"], "calls registry") if config.get("calls_registry") else {}
    calls = [entry for entry in registry.get("principals", []) if isinstance(entry, dict)]
    by_hash = {entry.get("key_sha256"): entry for entry in calls}
    labels = load_labels(config)
    pending: dict[str, list[tuple[dict, str]]] = {"messaging": [], "calendar": []}
    rows, unconfigured = [], False
    for principal in principals:
        credentials = []
        items = principal.get("credentials") if isinstance(principal.get("credentials"), dict) else {}
        for backend, credential in sorted(items.items()):
            entry = {"backend": backend, "public_id": None, "expires_at": None, "status": "missing"}
            token = read_token(config, credential.get("key_file", "")) if isinstance(credential, dict) else None
            if token is None:
                entry["note"] = "key_file_unreadable"
            elif not pinned(token, credential.get("key_sha256")):
                # The gateway refuses a key file that does not match its pin, so the binding does not work.
                entry.update(status="unknown", note="credential_pin_mismatch")
            elif backend in pending:
                pending[backend].append((entry, token))
            elif backend == "calls" and not config.get("calls_registry"):
                entry.update(status="unknown", note="calls_registry_not_configured")
                unconfigured = True
            elif backend == "calls":
                match = by_hash.get(hashlib.sha256(token.encode()).hexdigest())
                if match is not None:
                    entry.update(public_id=match.get("id"), expires_at=match.get("expires_at"),
                                 status=status({"revoked": not match.get("enabled"),
                                                "expires_at": match.get("expires_at")}, now))
                    if match.get("archive_config"):
                        entry["archive"] = archive(config, match["archive_config"], pending)
            else:
                entry.update(status="unknown", note="unsupported_backend")
            credentials.append(entry)
        rows.append({"id": principal["id"], "label": labels.get(principal["id"]),
                     "profile": principal.get("profile"), "enabled": principal.get("enabled") is True,
                     "expires_at": principal.get("expires_at"), "credentials": credentials})
    unavailable, records = [], {}
    if unconfigured:
        unavailable.append({"code": "backend_unavailable", "backend": "calls", "detail": "calls: no registry configured"})
    for service, items in pending.items():
        # The lookup and the inventory fail apart: a failed inventory keeps the rows' statuses and only loses
        # this backend's unbound_key findings.
        try:
            found = lookup(run, service, [token for _, token in items]) if items else []
        except Unavailable as exc:
            unavailable.append({"code": "backend_unavailable", "backend": service, "detail": str(exc)})
            for entry, _ in items:
                entry["status"] = "unknown"
            continue
        try:
            records[service] = inventory(run, service)
        except Unavailable as exc:
            unavailable.append({"code": "backend_unavailable", "backend": service, "detail": str(exc)})
        for (entry, _), record in zip(items, found, strict=True):
            entry["status"] = status(record, now)
            if record is not None:
                entry.update(public_id=public_id(service, record), backend_principal=record.get("principal"),
                             expires_at=record.get("expires_at"))
        items.clear()
    if config.get("calls_registry"):
        records["calls"] = [{"id": entry.get("id"), "expires_at": entry.get("expires_at"),
                             "revoked": not entry.get("enabled")} for entry in calls]
    return {"principals": rows, "findings": unavailable + findings(rows, records, now)}


def archive(config: dict, path: str, pending: dict) -> dict:
    """The calls entry's own messaging credential (archive_config -> token_file), looked up with the rest."""
    entry = {"backend": "messaging", "public_id": None, "expires_at": None, "status": "missing"}
    try:
        settings = json.loads(read_private(host_path(config, path), 65536))
        token = read_token(config, settings["token_file"])
    except (OSError, ValueError, KeyError, TypeError, AdminError):
        token = None
    if token is None:
        entry["note"] = "archive_unreadable"
    else:
        pending["messaging"].append((entry, token))
    return entry


def bound(row: dict):
    for credential in row["credentials"]:
        yield credential
        if "archive" in credential:
            yield credential["archive"]


def findings(rows: list[dict], records: dict, now: datetime) -> list[dict]:
    result, users = [], {}
    # Every principal counts, disabled or expired too: re-enabling or extending it would expose the finding.
    for row in rows:
        gateway = instant(row["expires_at"])
        for credential in bound(row):
            if credential["public_id"] is not None:
                users.setdefault((credential["backend"], credential["public_id"]), []).append(row["id"])
            if credential["status"] in ("missing", "revoked", "expired"):
                result.append({"code": "credential_" + credential["status"], "principal": row["id"],
                               "backend": credential["backend"], "public_id": credential["public_id"]})
            if credential.get("note") == "credential_pin_mismatch":
                result.append({"code": "credential_pin_mismatch", "principal": row["id"],
                               "backend": credential["backend"]})
            backend = instant(credential["expires_at"])
            if credential["status"] == "live" and backend is not None and gateway is not None and backend < gateway:
                result.append({"code": "backend_expires_first", "principal": row["id"],
                               "backend": credential["backend"], "public_id": credential["public_id"],
                               "expires_at": credential["expires_at"], "gateway_expires_at": row["expires_at"]})
    for (backend, key), names in sorted(users.items()):
        if len(set(names)) > 1:
            result.append({"code": "shared_credential", "backend": backend, "public_id": key,
                           "principals": sorted(set(names))})
    for backend, inventory_rows in sorted(records.items()):
        for record in inventory_rows:
            key = public_id(backend, record)
            if (backend, key) not in users:
                result.append({"code": "unbound_key", "backend": backend, "public_id": key,
                               "status": status(record, now),
                               "backend_principal": record.get("principal") or record.get("person_id")})
    return result


def select(view: dict, principal: str | None, scope: str, now: datetime) -> dict:
    """Rows by --principal and --valid/--expiring/--all. Findings: all without --principal; with it, those that
    name a shown row and every unavailable backend a shown row uses."""
    def expiries(row):
        return [instant(row["expires_at"])] + [instant(c["expires_at"]) for c in bound(row)]

    def keep(row):
        if principal is not None and row["id"] != principal:
            return False
        if scope == "valid":
            expiry = instant(row["expires_at"])
            return row["enabled"] and expiry is not None and expiry > now
        if scope == "expiring":  # in the window or already expired, as the gateway's own --check lists them
            gateway, *backends = expiries(row)
            return gateway is None or any(m is not None and m - now <= WINDOW for m in [gateway, *backends])
        return True

    rows = [row for row in view["principals"] if keep(row)]
    shown = {row["id"] for row in rows}
    used = {credential["backend"] for row in rows for credential in bound(row)}
    found = [item for item in view["findings"]
             if principal is None or item.get("principal") in shown or shown & set(item.get("principals", []))
             or (item["code"] == "backend_unavailable" and item["backend"] in used)]
    return {"principals": rows, "findings": found}


def load_labels(config: dict) -> dict:
    path = Path(config.get("labels", CONFIG.with_name("labels.json")))
    if not path.exists() and not path.is_symlink():
        return {}
    labels = load_json(path, "labels").get("labels", {})
    return {k: v for k, v in labels.items() if isinstance(k, str) and isinstance(v, str)}


def set_label(config: dict, principal: str, text: str) -> dict:
    if not 1 <= len(text) <= 80 or not text.isprintable():
        raise AdminError("a label is 1-80 printable characters")
    policy = load_json(config["policy"], "gateway policy")
    if principal not in {p.get("id") for p in policy.get("principals", []) if isinstance(p, dict)}:
        raise AdminError(f"no gateway principal {principal!r}")
    path = Path(config.get("labels", CONFIG.with_name("labels.json")))
    labels = load_labels(config)
    labels[principal] = text
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".labels-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as out:  # mkstemp creates it 0600
            json.dump({"version": 1, "labels": dict(sorted(labels.items()))}, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return {"principal": principal, "label": text}


# --- Write subcommands: `key issue --like` and `undo` (ANYM-496) under the shared rules (ANYM-493) ---------------


class Stopped(AdminError):
    """A failure after the journal exists: the operation stays resumable (rerun) and compensable (undo)."""


def checkpoint(step: str, phase: str) -> None:
    """Called after every write and every backend commit; a no-op that tests replace to crash at that point."""


def entry_digest(entry: dict) -> str:
    """The hash of a policy or registry entry this operation created, for the compare-and-swap of undo."""
    return hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Document:
    """A policy or registry read once: its bytes' digest (the compare-and-swap) and its parsed value."""

    def __init__(self, path: str | Path, what: str):
        self.path, self.what = Path(path), what
        try:
            self.raw = read_private(self.path)
            self.value = json.loads(self.raw)
        except (OSError, ValueError):
            raise AdminError(f"{what} unreadable: {path}") from None
        self.digest = hashlib.sha256(self.raw).hexdigest()
        rows = self.value.get("principals") if isinstance(self.value, dict) else None
        if type(self.value.get("revision") if isinstance(self.value, dict) else None) is not int or not (
                isinstance(rows, list) and all(isinstance(row, dict) for row in rows)):
            raise AdminError(f"{what} has no revision or principal list: {path}")

    def entry(self, name: str) -> dict | None:
        return next((row for row in self.value["principals"] if row.get("id") == name), None)

    def with_profile(self, name: str, profile: dict) -> dict:
        """The next revision with the profile `name` replaced; every other profile and every principal unchanged."""
        return {**self.value, "revision": self.value["revision"] + 1, "profiles": {**self.value["profiles"], name: profile}}

    def with_entry(self, name: str, entry: dict) -> dict:
        """The next revision with `entry` added, or replacing the entry `name`; every other entry unchanged."""
        rows = [row for row in self.value["principals"] if row.get("id") != name]
        index = next((i for i, row in enumerate(self.value["principals"]) if row.get("id") == name), len(rows))
        return {**self.value, "revision": self.value["revision"] + 1, "principals": rows[:index] + [entry] + rows[index:]}


class Host:
    """The installed production tool: the locks, release checks and call probe `deploy` uses (rules (a), (b))."""

    def __init__(self, tool, s, settings: dict, http: dict):
        self.tool, self.s, self.settings, self.http = tool, s, settings, http


def load_host(config: dict) -> Host:
    """production.py and seamless_release.py as installed on the host (`production.py deploy` runs them), never this
    checkout's copy: a write follows the rules the deploy that may run beside it follows."""
    if "production_config" in config:
        raise AdminError("production_config is not an admin setting: writes always use the deploy configuration "
                         "autodeploy selects (production.active_config); nothing changed")
    directory = Path(config.get("tool", TOOL))
    modules = {}
    for name in ("production", "seamless_release"):
        spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
        if spec is None or spec.loader is None:
            raise AdminError(f"the host tool is not installed in {directory}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # seamless_release imports the production module beside it
        try:
            spec.loader.exec_module(module)
        except (OSError, ImportError) as exc:
            raise AdminError(f"the host tool in {directory} does not load ({type(exc).__name__})") from None
        modules[name] = module
    tool, s = modules["production"], modules["seamless_release"]
    if not callable(getattr(tool, "unresolved", None)):
        raise AdminError(f"the host tool in {directory} has no shared release check (production.unresolved); "
                         "install the host tool of a release that has it first; nothing changed")
    try:
        # The plan A configuration autodeploy reads (DEEDKIT_DEPLOY_CONFIG, autodeploy.env), so the locks, release
        # state and rollback snapshot are the ones a running deploy uses.
        settings = tool.config(tool.active_config())
        http = s.validate_settings(tool.config(config.get("http_config", tool.HTTP_CONFIG)))
    except (tool.Refused, OSError, ValueError, KeyError, TypeError) as exc:
        raise AdminError(f"the production configuration is refused ({type(exc).__name__}); nothing changed") from None
    return Host(tool, s, settings, http)


def retrying(deadline: float, attempt, refusal: str) -> None:
    while not attempt():
        if time.monotonic() >= deadline:
            raise AdminError(refusal)
        time.sleep(0.2)


@contextmanager
def guarded(config: dict, host: Host, docker):
    """Rules (a) and (b): deploy's global, HTTP and plan A locks, then the calls-registry writer lock, in that order and
    within LOCK_WAIT seconds; then no unresolved release (deploy's own check), no deploy pause and no active call in
    deploy's calls targets or in any running container that mounts the calls registry."""
    tool, deadline = host.tool, time.monotonic() + LOCK_WAIT
    state = Path(host.settings["state_directory"])
    with ExitStack() as held:
        def deploy_locks():
            try:
                held.enter_context(tool.host_locks(host.settings, host.http, host.s))
            except tool.Busy:
                return False
            except tool.Refused as exc:
                raise AdminError(f"{exc}; nothing changed") from None
            return True
        retrying(deadline, deploy_locks, "a deploy is running: a release lock stayed held; nothing changed")
        path = Path(config.get("calls_lock", CALLS_LOCK))
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = held.enter_context(path.open("a"))

        def writer_lock():
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True
        retrying(deadline, writer_lock, "another admin write holds the calls-registry writer lock; nothing changed")
        try:
            reason = tool.unresolved(state, Path(host.http["state_directory"]), lambda: tool.main_history(state))
        except tool.Refused as exc:
            reason = str(exc)
        if reason:
            raise AdminError(f"{reason}; nothing changed")
        if tool.PAUSE.exists():
            raise AdminError(f"deploys are paused ({tool.PAUSE}); nothing changed")
        try:
            calls = tool.active_calls(host.settings)
            if config.get("calls_registry"):
                # Deploy's own per-container probe, also for a voice consumer outside the deploy configuration.
                until = time.monotonic() + tool.CALL_PROBE
                for item in consumers(running(docker), config["calls_registry"], "calls registry"):
                    calls += [call for call in tool.calls_in(item["id"], until) if call not in calls]
        except (tool.Refused, OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, tool.Refused) else type(exc).__name__
            raise AdminError(f"the call state is unreadable ({reason}); nothing changed") from None
        if calls:
            raise AdminError("a call is active (" + ", ".join(calls) + "); nothing changed")
        yield


def dockers(config: dict):
    """Docker for inspection and one-shot check containers only (rule (d)): never a service container."""
    prefix = config.get("docker", ["docker"])

    def call(args: list[str], name: str | None = None, data: bytes | None = None) -> subprocess.CompletedProcess:
        # A secret only ever on stdin (rule (g)); no stdin at all otherwise.
        try:
            return subprocess.run(prefix + args, capture_output=True, timeout=TIMEOUT, check=False, input=data,
                                  stdin=None if data is not None else subprocess.DEVNULL)
        except OSError as exc:
            raise AdminError(f"docker: {type(exc).__name__}; nothing changed") from None
        except BaseException as exc:
            if name:
                stop(name, prefix)
            if isinstance(exc, subprocess.TimeoutExpired):
                raise AdminError("docker: TimeoutExpired") from None
            raise
    return call


def running(docker) -> list[dict]:
    listed = docker(["ps", "-q", "--no-trunc"])
    ids = listed.stdout.decode().split() if not listed.returncode else None
    if ids is None:
        raise AdminError("docker ps failed; nothing changed")
    if not ids:
        return []
    found = docker(["inspect", *ids])
    try:
        value = json.loads(found.stdout) if not found.returncode else None
    except ValueError:
        value = None
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AdminError("docker inspect failed; nothing changed")
    return value


def consumers(containers: list[dict], path: str, what: str) -> list[dict]:
    """The running containers that can read `path`: every one with a mount of its directory or of any ancestor (the
    deepest such mount; "target" is the directory's path inside it). Each counts as a consumer, so a container that
    cannot answer a check or a call probe refuses rather than goes unchecked. A single-file bind of `path` pins the
    inode it started with, so no rename reaches it (ANYM-424): refused."""
    live, found = os.path.realpath(path), []
    folder = os.path.dirname(live)
    for container in containers:
        best = None
        for mount in container.get("Mounts") or []:
            if not mount.get("Source") or not mount.get("Destination"):
                continue
            source = os.path.realpath(mount["Source"])
            if source == live:
                raise AdminError(f"{str(container.get('Name', '?')).lstrip('/')} mounts the {what} as a single file, "
                                 "so a rename never reaches it (ANYM-424): mount its directory; nothing changed")
            if (folder == source or folder.startswith(source.rstrip("/") + "/")) and (
                    best is None or len(source) > len(best[0])):
                best = (source, mount)
        if best is not None:
            source, mount = best
            found.append({"id": container.get("Id"), "name": str(container.get("Name", "")).lstrip("/"),
                          "image": container.get("Image"), "user": (container.get("Config") or {}).get("User") or "",
                          "command": (container.get("Config") or {}).get("Cmd") or [],
                          "entrypoint": (container.get("Config") or {}).get("Entrypoint") or [],
                          "source": mount["Source"], "mount": mount["Destination"],
                          "target": str(PurePosixPath(mount["Destination"]) / os.path.relpath(folder, source))})
    if not found:
        raise AdminError(f"no running container mounts the {what} directory, so none can validate it; nothing changed")
    return found


def rollback_images(host: Host) -> list[str]:
    """The gateway images `production.py rollback` would restore: the current release's before-release snapshot."""
    state = Path(host.settings["state_directory"])
    try:
        sha = str(json.loads((state / "current.json").read_text()).get("sha", ""))
    except (OSError, ValueError, AttributeError):
        return []
    images = []
    for project in host.settings.get("projects", []):
        for service, target in project.get("services", {}).items():
            before = state / sha / (project["name"] + ".before.json")
            if target != "gateway" or not re.fullmatch(r"[0-9a-f]{40}", sha) or not before.exists():
                continue
            try:
                images.append(json.loads(before.read_text())["services"][service]["image"])
            except (OSError, ValueError, KeyError, TypeError):
                raise AdminError(f"the rollback snapshot {before} is unreadable; nothing changed") from None
    return images


def validate(config: dict, docker, kind: str, staging: Path, found: list[dict], extra: list[str],
             outcome: str = "not renamed") -> None:
    """Rule (c): the candidate in each running consumer's image (and the rollback images), read through that
    consumer's own directory mount, in a disposable container without network; any refusal refuses."""
    command = {**VALIDATORS, **config.get("validators", {})}[kind]
    for consumer, image in [(item, item["image"]) for item in found] + [(found[0], image) for image in extra]:
        name = "deedkit-admin-" + uuid4().hex[:12]
        argv = [part.replace("{path}", str(PurePosixPath(consumer["target"]) / staging.name)) for part in command]
        done = docker(["run", "--rm", "--name", name, "--network", "none", "--read-only", "--tmpfs", "/tmp",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pull", "never",
                       "--mount", f"type=bind,source={consumer['source']},target={consumer['mount']},readonly",
                       *(["--user", consumer["user"]] if consumer["user"] else []),
                       "--entrypoint", argv[0], image, *argv[1:]], name)
        try:  # a positive answer only: an older image without the check may exit 0 having read nothing
            answer = json.loads(done.stdout.decode().strip().splitlines()[-1]) if not done.returncode else None
        except (ValueError, IndexError, UnicodeDecodeError):
            answer = None
        if not isinstance(answer, dict) or answer.get("valid") is not True:
            raise AdminError(f"the {kind} candidate is refused by image {image} ({consumer['name']}), or the image "
                             f"has no such check; {outcome}")


@contextmanager
def staged(live: Path, data: bytes):
    """`data` under a private staging name beside `live`, owned like it and synced; removed on exit unless renamed."""
    info = os.stat(live)
    staging = live.with_name(f".{live.name}.admin-{uuid4().hex[:12]}")
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            if (info.st_uid, info.st_gid) != (os.geteuid(), os.getegid()):
                os.fchown(out.fileno(), info.st_uid, info.st_gid)
            os.fsync(out.fileno())
        yield staging
    finally:
        staging.unlink(missing_ok=True)


def preflight(config: dict, docker, document: Document, kind: str, found: list[dict], extra: list[str]) -> None:
    """Rules (c) and (h) before any write: every validator a later rename needs answers {"valid": true} on a copy of
    the live document, so an image without the check refuses before a journal, token or backend write."""
    with staged(document.path, read_private(document.path)) as staging:
        validate(config, docker, kind, staging, found, extra, "nothing changed")


def replace(config: dict, docker, document: Document, value: dict, kind: str, found: list[dict],
            extra: list[str]) -> None:
    """`value` over the live file by a rename in its mounted directory after rule (c), as a compare-and-swap on the
    bytes read under the locks: a change by anyone since refuses. The new file keeps the live file's owner."""
    live = document.path
    with staged(live, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()) as staging:
        validate(config, docker, kind, staging, found, extra)
        if hashlib.sha256(read_private(live)).hexdigest() != document.digest:
            raise AdminError(f"{live} changed during this operation; not renamed")
        os.rename(staging, live)
        sync_directory(live.parent)


def private_directory(path: Path) -> None:
    """`path` as a 0700 directory of this user (created if missing); anything wider refuses."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise AdminError(f"{path} must be private (0700) and owned by this user; nothing changed")


def write_secret(path: Path, value: str, owner: tuple[int, int]) -> None:
    """A new owner-only file, durable before any store names it: written and synced under a staging name, then
    published by link() (which never replaces an existing file), so `path` is either absent or complete."""
    staging = path.with_name(f".{path.name}.admin-{uuid4().hex[:12]}")
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(value + "\n")
            out.flush()
            if owner != (os.geteuid(), os.getegid()):
                os.fchown(out.fileno(), *owner)
            os.fsync(out.fileno())
        checkpoint(path.name, "staged")
        os.link(staging, path)
    finally:
        staging.unlink(missing_ok=True)
    sync_directory(path.parent)


def stale_staging(path: Path) -> None:
    """The staging files an interrupted write_secret left beside `path` (never published, never named by a store)."""
    for leftover in path.parent.glob(f".{path.name}.admin-*"):
        leftover.unlink(missing_ok=True)


class Journal:
    """Rule (e): `<journal>/<op-id>.json` (directory 0700, file 0600) with public IDs, the hashes of the entries the
    operation created and each step's state, saved before and after each step. Never a token or a key hash."""

    def __init__(self, config: dict, operation: str):
        if not OPERATION.fullmatch(operation):
            raise AdminError("an operation ID is 1-100 of [a-z0-9.-]")
        self.directory = Path(config.get("journal", JOURNAL))
        self.path, self.data = self.directory / f"{operation}.json", {}

    def load(self) -> dict:
        if not self.path.exists():
            raise AdminError(f"no operation journal {self.path}")
        self.data = load_json(self.path, "operation journal")
        return self.data

    def create(self, data: dict) -> None:
        private_directory(self.directory)
        if self.path.exists():
            raise AdminError(f"operation journal {self.path} exists; nothing changed")
        self.data = data
        self.save()

    def save(self) -> None:
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix=".op-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as out:  # mkstemp creates it 0600
                json.dump(self.data, out, indent=2)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        sync_directory(self.directory)

    def step(self, name: str, state: str, **fields) -> dict:
        self.data["steps"][name] = {**self.data["steps"].get(name, {}), **fields, "state": state}
        self.save()
        return self.data["steps"][name]


def ask(run, service: str, args: list[str], data: bytes | None = None):
    try:
        return run(service, args, data)
    except Unavailable as exc:
        raise AdminError(str(exc)) from None


def checked(run, service: str, args: list[str], data: bytes | None = None) -> None:
    """Rule (h): the command's no-write check mode answers `ok` only where the binary has it and its schema is
    applied; an unknown command or option, or `schema_not_ready`, refuses naming the service."""
    try:
        reply = run(service, args + ["--check"], data)
    except Unavailable as exc:
        raise AdminError(f"{exc}: the {service} image does not answer `{args[0]} --check`; update it first; "
                         "nothing changed") from None
    if not isinstance(reply, dict) or reply.get("check") != "ok":
        answer = reply.get("check") if isinstance(reply, dict) else "no result"
        raise AdminError(f"{service}: `{args[0]} --check` answered {answer}; nothing changed")


def one(run, service: str, args: list[str], data: bytes | None = None) -> dict:
    reply = ask(run, service, args, data)
    rows = reply.get("keys" if service == "messaging" else "agents") if isinstance(reply, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise AdminError(f"{service}: malformed reply to {args[0]}")
    return rows[0]


def live_until(value) -> datetime | None:
    moment = instant(value)
    return moment if moment is not None and moment > datetime.now(timezone.utc) else None


def messaging_scope(run, token: str, what: str) -> dict:
    record = one(run, "messaging", LOOKUP["messaging"], (token + "\n").encode())
    if not record.get("found") or record.get("revoked") or live_until(record.get("expires_at")) is None:
        raise AdminError(f"the source's {what} key is missing, revoked or expired; nothing changed")
    info = ask(run, "messaging", ["principal-info", "--principal", str(record.get("principal"))])
    key = next((k for k in info.get("keys", []) if k.get("key_id") == record.get("key_id")), None) \
        if isinstance(info, dict) and info.get("found") else None
    if key is None or info.get("enabled") is not True or not info.get("accounts"):
        raise AdminError(f"the source's {what} principal is unknown, disabled or has no account; nothing changed")
    # The clone's key repeats this key's stored ceiling as is (issue-key --key-permissions-of), a NULL legacy one
    # included; "key_permissions" is only shown in the plan.
    return {"accounts": info["accounts"], "chat_ids": info["chat_ids"], "permissions": info["permissions"],
            "key_of": str(record.get("key_id")), "key_permissions": key.get("permissions")}


def source_scope(config: dict, run, policy: Document, registry: Document | None, source: str) -> dict:
    """The source principal's access in every store, from each store's own read command (public fields only)."""
    entry = policy.entry(source)
    if entry is None:
        raise AdminError(f"no gateway principal {source!r}; nothing changed")
    if entry.get("enabled") is not True or live_until(entry.get("expires_at")) is None:
        raise AdminError(f"the source {source!r} is disabled or expired; nothing changed")
    credentials = entry.get("credentials") if isinstance(entry.get("credentials"), dict) else {}
    if set(credentials) - set(BACKENDS) or not all(isinstance(c, dict) for c in credentials.values()):
        raise AdminError(f"the source {source!r} has a credential this command cannot clone; nothing changed")
    tokens = {}
    for backend, credential in credentials.items():
        token = read_token(config, credential.get("key_file", ""))
        if token is None or not pinned(token, credential.get("key_sha256")):
            raise AdminError(f"the source's {backend} key file is unreadable or does not match its pin; nothing changed")
        tokens[backend] = token
    scope = {"gateway": entry}
    if "messaging" in tokens:
        scope["messaging"] = messaging_scope(run, tokens["messaging"], "messaging")
    if "calendar" in tokens:
        record = one(run, "calendar", LOOKUP["calendar"], (tokens["calendar"] + "\n").encode())
        if not record.get("found") or record.get("revoked") or live_until(record.get("expires_at")) is None:
            raise AdminError("the source's calendar key is missing, revoked or expired; nothing changed")
        info = ask(run, "calendar", ["agent-scope", "--id", str(record.get("principal_id"))])
        visibility = {row.get("visibility") for row in info.get("calendars", [])} if isinstance(info, dict) else set()
        if not isinstance(info, dict) or not info.get("found") or len(visibility) > 1:
            raise AdminError("the source's calendar agent is unknown or mixes calendar visibilities; nothing changed")
        scope["calendar"] = {"group": info["group_id"], "member": info["person_id"], "scopes": info["scopes"],
                             "calendars": [row["ref"] for row in info["calendars"]],
                             "visibility": visibility.pop() if visibility else "details"}
    if "calls" in tokens:
        if registry is None:
            raise AdminError("the source has a calls key but no calls registry is configured; nothing changed")
        digest = hashlib.sha256(tokens["calls"].encode()).hexdigest()
        calls = next((row for row in registry.value["principals"] if row.get("key_sha256") == digest), None)
        if calls is None or calls.get("enabled") is not True or live_until(calls.get("expires_at")) is None:
            raise AdminError("the source's calls entry is missing, disabled or expired; nothing changed")
        scope["calls"] = calls
        if calls.get("archive_config"):
            try:
                settings = json.loads(read_private(host_path(config, calls["archive_config"]), 65536))
                token = read_token(config, settings["token_file"])
            except (OSError, ValueError, KeyError, TypeError, AdminError):
                token = None
            if token is None:
                raise AdminError("the source's calls archive configuration is unreadable; nothing changed")
            scope["archive"] = {"settings": settings, "config": calls["archive_config"],
                                "messaging": messaging_scope(run, token, "calls archive")}
    return scope


def layout(config: dict, new: str, scope: dict) -> dict:
    """Where each new secret goes: beside the source's own file (so its consumer reads it), owned like it."""
    def beside(container: str, name: str) -> dict:
        path = PurePosixPath(container).parent / name
        source = os.stat(host_path(config, container))
        return {"container": str(path), "host": host_path(config, str(path)), "owner": [source.st_uid, source.st_gid]}
    files = {"gateway": {"container": None, "host": str(Path(config.get("tokens", TOKENS)) / f"{new}.token"),
                         "owner": [os.geteuid(), os.getegid()]}}
    for backend, credential in scope["gateway"]["credentials"].items():
        files[backend] = beside(credential["key_file"], f"{new}-{backend}.key")
    if "archive" in scope:
        files["archive-token"] = beside(scope["archive"]["settings"]["token_file"], f"{new}-calls-archive.token")
        files["archive-config"] = beside(scope["archive"]["config"], f"{new}-calls-archive.json")
    return files


def messaging_issue(principal: str, scope: dict, days: int, operation: str) -> list[str]:
    chats = ["--all-chats"] if scope["chat_ids"] is None else ["--chats", *scope["chat_ids"]]
    return ["issue-key", "--principal", principal, "--accounts", *scope["accounts"], *chats,
            "--permissions", *scope["permissions"], "--key-permissions-of", scope["key_of"],
            "--days", str(days), "--new-principal", "--token-file", "/dev/stdin", "--operation-id", operation]


def calendar_issue(principal: str, scope: dict, days: int, operation: str) -> list[str]:
    return ["agent-key", "--id", principal, "--group", scope["group"], "--member", scope["member"],
            *[part for ref in scope["calendars"] for part in ("--calendar", ref)],
            "--scopes", ",".join(scope["scopes"]), "--visibility", scope["visibility"], "--days", str(days),
            "--token-file", "/dev/stdin", "--operation-id", operation]


def backend_steps(new: str, scope: dict, days: int, operation: str) -> list[tuple[str, str, str, list[str]]]:
    """(step, service, secret, argv) in order; each credential has its own operation ID derived from the parent."""
    steps = []
    if "messaging" in scope:
        steps.append(("messaging", "messaging", "messaging",
                      messaging_issue(new, scope["messaging"], days, f"{operation}/messaging")))
    if "archive" in scope:
        steps.append(("messaging-archive", "messaging", "archive-token",
                      messaging_issue(f"{new}-calls-archive", scope["archive"]["messaging"], days,
                                      f"{operation}/messaging-archive")))
    if "calendar" in scope:
        steps.append(("calendar", "calendar", "calendar",
                      calendar_issue(new, scope["calendar"], days, f"{operation}/calendar")))
    return steps


def taken(config: dict, run, policy: Document, registry: Document | None, new: str, files: dict) -> None:
    """Refuse an ID any store holds; a disabled or revoked record is a retired ID (undo keeps it for audit)."""
    found = []
    for principal in (new, f"{new}-calls-archive"):
        if (entry := policy.entry(principal)) is not None:
            found.append((f"gateway policy ({principal})", entry.get("enabled") is not True))
        if registry is not None and (entry := registry.entry(principal)) is not None:
            found.append((f"calls registry ({principal})", entry.get("enabled") is not True))
        info = ask(run, "messaging", ["principal-info", "--principal", principal])
        if isinstance(info, dict) and info.get("found"):
            found.append((f"messaging ({principal})", info.get("enabled") is not True
                          or all(k.get("revoked") for k in info.get("keys", []))))
        if config.get("calendar"):
            info = ask(run, "calendar", ["agent-scope", "--id", principal])
            if isinstance(info, dict) and info.get("found"):
                found.append((f"calendar ({principal})", bool(info.get("revoked"))))
    # An operation reserves its ID from its first write: an undone one keeps it retired even when no store holds it.
    directory = Path(config.get("journal", JOURNAL))
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            command = load_json(path, "operation journal").get("command", {})
        except AdminError:
            raise AdminError(f"the operation journal {path} is unreadable; nothing changed") from None
        if command.get("id") == new:
            found.append((f"operation {path.stem}", True))
    if found:
        retired = all(flag for _, flag in found)
        raise AdminError(f"{new!r} exists in " + ", ".join(name for name, _ in found)
                         + (" and is retired (revoked records stay for audit): use another ID" if retired else "")
                         + "; nothing changed")
    if existing := sorted(file["host"] for file in files.values() if os.path.lexists(file["host"])):
        raise AdminError("a file this command would create exists: " + ", ".join(existing) + "; nothing changed")


def identity(scope: dict) -> dict:
    """The source's access a clone copies, without the source's own file locations (never copied to the clone), so
    moving a source key file does not change an operation's identity."""
    scope = masked(scope)
    scope["gateway"] = {**scope["gateway"], "credentials": {
        backend: {k: v for k, v in credential.items() if k != "key_file"}
        for backend, credential in scope["gateway"].get("credentials", {}).items()}}
    if "archive" in scope:
        scope["archive"] = {**scope["archive"], "config": None,
                            "settings": {**scope["archive"]["settings"], "token_file": None}}
    if "calls" in scope:
        scope["calls"] = {**scope["calls"], "archive_config": None}
    return scope


def masked(entry: dict) -> dict:
    """An entry for the dry-run plan: every hash replaced by a placeholder."""
    def mask(value):
        if isinstance(value, dict):
            return {k: ("<sha256 of the new key>" if k == "key_sha256" else mask(v)) for k, v in value.items()}
        return value
    return mask(entry)


def entries(scope: dict, new: str, files: dict, tokens: dict, expires_at: str) -> tuple[dict, dict | None]:
    """The new gateway principal and calls entry: the source's fields with the new ID, keys, files and expiry."""
    def pin(name):
        return hashlib.sha256(tokens[name].encode()).hexdigest()
    source = scope["gateway"]
    gateway = {**{k: v for k, v in source.items() if k not in ("id", "key_sha256", "expires_at", "credentials")},
               "id": new, "key_sha256": pin("gateway"), "enabled": True, "expires_at": expires_at,
               "credentials": {backend: {**credential, "key_file": files[backend]["container"],
                                         "key_sha256": pin(backend)}
                               for backend, credential in source["credentials"].items()}}
    calls = None
    if "calls" in scope:
        calls = {**{k: v for k, v in scope["calls"].items()
                    if k not in ("id", "key_sha256", "expires_at", "archive_config")},
                 "id": new, "key_sha256": pin("calls"), "enabled": True, "expires_at": expires_at}
        if "archive" in scope:
            calls["archive_config"] = files["archive-config"]["container"]
    return gateway, calls


def secrets_for(files: dict, journal: Journal | None) -> dict:
    """The new tokens: generated once, read back from their files on a rerun (never regenerated for a written file)."""
    tokens = {}
    for name, file in files.items():
        if name == "archive-config":
            continue
        if journal is not None and os.path.lexists(file["host"]):
            token = read_token({}, file["host"])
            if token is None:
                raise AdminError(f"{file['host']} is not this operation's key file; nothing changed")
            tokens[name] = token
        else:
            tokens[name] = ("pos_probe_" if name in ("messaging", "archive-token") else "dk_") + secrets.token_urlsafe(32)
    return tokens


def issue(config: dict, run, docker, host_factory, args) -> dict:
    new, source, days = args.new, args.like, args.days
    if not NAME.fullmatch(new) or new == source:
        raise AdminError("a new ID is 1-50 of [a-z0-9.-], starting with a letter or digit, and not the source's")
    if not DAYS[0] <= days <= DAYS[1]:
        raise AdminError(f"--days is {DAYS[0]}-{DAYS[1]} for every store; nothing changed")
    if args.label is not None and not (1 <= len(args.label) <= 80 and args.label.isprintable()):
        raise AdminError("a label is 1-80 printable characters; nothing changed")
    command = {"verb": "issue", "id": new, "like": source, "days": days, "label": args.label}
    operation = args.operation or f"issue-{new}-{datetime.now(timezone.utc):%Y%m%dt%H%M%S}-{secrets.token_hex(3)}"
    journal = Journal(config, operation)
    if args.operation:
        data = journal.load()
        if data.get("command") != command:
            raise AdminError(f"operation {operation} was started with other arguments; nothing changed")
        if data.get("state") in ("undoing", "undone"):
            raise AdminError(f"operation {operation} was undone; issue again with another ID; nothing changed")
        if data.get("state") == "done":
            return data["result"]
    host = None if args.dry_run else host_factory()
    with ExitStack() as held:
        if host is not None:
            held.enter_context(guarded(config, host, docker))
        policy = Document(config["policy"], "gateway policy")
        registry = Document(config["calls_registry"], "calls registry") if config.get("calls_registry") else None
        containers = running(docker)
        found = {"policy": consumers(containers, config["policy"], "gateway policy")}
        if registry is not None:
            found["registry"] = consumers(containers, config["calls_registry"], "calls registry")
        for service, read in (("messaging", ["principal-info", "--principal", new]),
                              ("messaging", ["key-info", "--operation-id", f"{operation}/messaging"]),
                              ("calendar", ["agent-scope", "--id", new]),
                              ("calendar", ["agent-info", "--operation-id", f"{operation}/calendar"])):
            if config.get(service):
                checked(run, service, read)
        scope = source_scope(config, run, policy, registry, source)
        # A rerun keeps the operation's identity: the file layout it journaled, and the source's access unchanged.
        shape = entry_digest(identity(scope))
        files = journal.data["layout"] if args.operation else layout(config, new, scope)
        if args.operation and journal.data.get("scope") != shape:
            raise AdminError(f"the source {source!r} changed since operation {operation} started; "
                             f"run `deedkit admin undo {operation}`; nothing changed")
        if not args.operation:
            taken(config, run, policy, registry, new, files)
        tokens = secrets_for(files, journal if args.operation else None)
        steps = backend_steps(new, scope, days, operation)
        for _, service, secret, argv in steps:
            checked(run, service, argv, (tokens[secret] + "\n").encode())
        bound = journal.data.get("bound") or (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        if args.dry_run:
            gateway, calls = entries(scope, new, files, tokens, bound)
            return {"dry_run": True, "principal": new, "like": source, "days": days,
                    "steps": ["key files"] + [step for step, *_ in steps]
                    + (["calls archive configuration"] if "archive" in scope else [])
                    + (["calls registry"] if calls else []) + ["gateway policy"],
                    "files": sorted(file["host"] for file in files.values()),
                    "gateway_policy": {"revision": policy.value["revision"] + 1, "add": masked(gateway)},
                    **({"calls_registry": {"revision": registry.value["revision"] + 1, "add": masked(calls)}}
                       if calls else {})}
        _, calls = entries(scope, new, files, tokens, bound)
        preflight(config, docker, policy, "policy", found["policy"], rollback_images(host))
        if calls and registry is not None:
            preflight(config, docker, registry, "registry", found["registry"], [])
        if not args.operation:
            private_directory(Path(files["gateway"]["host"]).parent)
            journal.create({"version": 1, "operation": operation, "command": command, "state": "running",
                            "bound": bound, "files": {name: file["host"] for name, file in files.items()},
                            "layout": files, "scope": shape, "steps": {}})
        try:
            return run_issue(config, run, docker, host, journal, scope, files, tokens, steps, found)
        except (AdminError, OSError) as exc:
            reason = str(exc) if isinstance(exc, AdminError) else type(exc).__name__
            journal.data.update(state="failed", error=reason)
            journal.save()
            raise Stopped(f"operation {operation} stopped: {reason}. Either rerun the same command with "
                          f"`--operation {operation}` (finished steps replay by their operation IDs), or run "
                          f"`deedkit admin undo {operation}`") from None


def run_issue(config, run, docker, host, journal: Journal, scope, files, tokens, steps, found) -> dict:
    data, new = journal.data, journal.data["command"]["id"]
    if data["steps"].get("key files", {}).get("state") == "done" and (missing := sorted(
            file["host"] for name, file in files.items() if name != "archive-config"
            and not os.path.lexists(file["host"]))):
        raise AdminError("a key file this operation wrote is gone: " + ", ".join(missing))
    journal.step("key files", "started")
    for name, file in files.items():
        stale_staging(Path(file["host"]))
        if name != "archive-config" and not os.path.lexists(file["host"]):
            write_secret(Path(file["host"]), tokens[name], tuple(file["owner"]))
            checkpoint("key files", name)
    journal.step("key files", "done")
    expiries = [data["bound"]]
    for step, service, secret, argv in steps:
        journal.step(step, "started", operation_id=argv[-1])
        reply = ask(run, service, argv, (tokens[secret] + "\n").encode())
        checkpoint(step, "committed")
        public = reply.get("key_id") or reply.get("principal_id") if isinstance(reply, dict) else None
        if not public or instant(reply.get("expires_at")) is None or reply.get("revoked"):
            raise AdminError(f"{service}: {argv[0]} returned no live record")
        journal.step(step, "done", public_id=public, expires_at=reply["expires_at"])
        expiries.append(reply["expires_at"])
    # The gateway never outlives a backend: every entry ends at the earliest backend expiry read back.
    expires_at = min(expiries, key=instant)
    gateway, calls = entries(scope, new, files, tokens, expires_at)
    if "archive" in scope:
        file = files["archive-config"]
        journal.step("calls archive configuration", "started")
        settings = {**scope["archive"]["settings"], "token_file": files["archive-token"]["container"]}
        if not os.path.lexists(file["host"]):
            write_secret(Path(file["host"]), json.dumps(settings), tuple(file["owner"]))
            checkpoint("calls archive configuration", "written")
        elif json.loads(read_private(file["host"])) != settings:
            raise AdminError(f"{file['host']} differs from what this operation wrote")
        journal.step("calls archive configuration", "done")
    for name, entry, what, extra in (("calls registry", calls, "registry", []),
                                     ("gateway policy", gateway, "policy", rollback_images(host))):
        if entry is None:
            continue
        document = Document(config["policy" if what == "policy" else "calls_registry"], name)
        journal.step(name, "started", entry=entry_digest(entry))
        current = document.entry(new)
        if current is None:
            replace(config, docker, document, document.with_entry(new, entry), what, found[what], extra)
            checkpoint(name, "renamed")
        elif entry_digest(current) != entry_digest(entry):
            raise AdminError(f"the {name} entry {new!r} differs from what this operation wrote")
        journal.step(name, "done", entry=entry_digest(entry))
    if data["command"]["label"]:
        set_label(config, new, data["command"]["label"])
    token, url = files["gateway"]["host"], host.http["http_release"]["gateway_public_url"].rstrip("/") + "/mcp"
    data["result"] = {
        "operation": data["operation"], "principal": new, "like": data["command"]["like"], "expires_at": expires_at,
        "credentials": [{"store": step, "public_id": data["steps"][step]["public_id"],
                         "expires_at": data["steps"][step]["expires_at"]} for step, *_ in steps]
        + ([{"store": "calls", "public_id": new, "expires_at": expires_at}] if calls else []),
        "token_file": token, "undo": f"deedkit admin undo {data['operation']}",
        # ANYM-498: one command on the Mac; only the Mac's ssh config names this host, so the destination is a placeholder.
        "next": [f"python3 scripts/deedkit.py key install {new} --host <ssh-destination> --url {shlex.quote(url)}"]}
    data["state"] = "done"
    journal.save()
    return data["result"]


def references(config: dict, policy: Document, registry: Document | None, new: str, data: dict,
               tokens: dict, calls_id: str | None = None) -> list[str]:
    """Rule (f): principals other than this operation's own entry that use a credential it created, by pin, key file,
    archive configuration or archive token, compared by the token itself (a copy in another file counts). A reference
    that cannot be read cannot be ruled out, so it counts too. `calls_id` is the principal's own calls entry when its
    ID differs from the gateway principal's."""
    digests = {hashlib.sha256(token.encode()).hexdigest() for token in tokens.values()}
    paths = set(data["files"].values())

    def holds(container: str) -> bool | None:
        if host_path(config, container) in paths:
            return True
        token = read_token(config, container)
        if token is None:  # unreadable, missing or malformed: its credential cannot be ruled out
            return None
        return hashlib.sha256(token.encode()).hexdigest() in digests
    users = set()
    for entry in policy.value["principals"]:
        for credential in (entry.get("credentials") or {}).values() if entry.get("id") != new else []:
            if not isinstance(credential, dict):
                continue
            used = credential.get("key_sha256") in digests or holds(str(credential.get("key_file")))
            if used is not False:
                users.add(str(entry.get("id")) + ("" if used else " (unreadable key file)"))
    for entry in registry.value["principals"] if registry is not None else []:
        if entry.get("id") == (calls_id or new):
            continue
        used = entry.get("key_sha256") in digests
        if entry.get("archive_config"):
            archive = host_path(config, entry["archive_config"])
            try:
                nested = json.loads(read_private(archive, 65536))["token_file"]
                used = used or archive in paths or holds(nested)
            except (OSError, ValueError, KeyError, TypeError, AdminError):
                used = None
        if used is not False:
            users.add(f"{entry.get('id')} (calls" + (")" if used else ", unreadable archive configuration)"))
    return sorted(users)


UNDO_FILES = {"messaging": "messaging", "messaging-archive": "archive-token", "calendar": "calendar"}


def owned(data: dict, name: str, entry: dict | None, new: str) -> str:
    """The state of this operation's policy or registry entry: "absent", "created" (exactly as the operation wrote
    it) or "disabled" (exactly as its own undo wrote it); anything else is someone else's change and refuses."""
    if entry is None:
        return "absent"
    digest = entry_digest(entry)
    if digest == data["steps"].get(name, {}).get("entry") and entry.get("enabled") is True:
        return "created"
    if digest == data["steps"].get("undo " + name, {}).get("entry"):
        return "disabled"
    raise AdminError(f"the {name} entry {new!r} changed since this operation wrote it; nothing changed")


def undo(config: dict, run, docker, host_factory, operation: str) -> dict:
    """Compensation, not restoration: disable the entries this operation created, revoke the backend records that carry
    its operation IDs, delete its key files. Later changes by others stay; a conflict refuses."""
    journal = Journal(config, operation)
    data = journal.load()
    verb = data.get("command", {}).get("verb")
    if verb == "extend":
        raise AdminError(f"operation {operation} is an extend: expiries never move earlier, so it has no undo "
                         "(to end the key, run `deedkit admin key revoke`); nothing changed")
    if verb == "revoke":
        return undo_revoke(config, run, docker, host_factory, journal)
    if verb == "profile":
        return undo_profile(config, docker, host_factory, journal)
    if verb != "issue":
        raise AdminError(f"operation {operation} is not an issue; nothing changed")
    new = data["command"]["id"]
    result = {"operation": operation, "principal": new, "undone": True,
              "note": f"{new!r} is retired: revoked records stay for audit, so a new key needs another ID"}
    if data.get("state") == "undone":
        return {**result, **data.get("undo", {})}
    host = host_factory()
    with guarded(config, host, docker):
        policy = Document(config["policy"], "gateway policy")
        registry = Document(config["calls_registry"], "calls registry") if config.get("calls_registry") else None
        containers = running(docker)
        found = {"policy": consumers(containers, config["policy"], "gateway policy")}
        if registry is not None:
            found["registry"] = consumers(containers, config["calls_registry"], "calls registry")
        records = []
        for step, service in (("messaging", "messaging"), ("messaging-archive", "messaging"),
                              ("calendar", "calendar")):
            if step in data["steps"]:
                checked(run, service, [LOOKUP[service][0], "--operation-id", f"{operation}/{step}"])
                row = one(run, service, [LOOKUP[service][0], "--operation-id", f"{operation}/{step}"])
                if row.get("found") and not row.get("revoked"):
                    revoke = (["revoke-key", "--key-id", str(row.get("key_id"))] if service == "messaging"
                              else ["revoke-agent", "--id", str(row.get("principal_id"))])
                    checked(run, service, revoke)
                    records.append((step, service, revoke))
        states = {name: owned(data, name, document.entry(new), new)
                  for name, document in (("gateway policy", policy), ("calls registry", registry)) if document is not None}
        # Rule (f) compares other principals' references with the tokens of every credential undo would still revoke
        # or disable; without such a token exclusivity cannot be proven, so undo refuses.
        tokens = {name: token for name, path in data["files"].items() if name != "archive-config"
                  and os.path.lexists(path) and (token := read_token({}, path)) is not None}
        needed = [UNDO_FILES[step] for step, _, _ in records]
        needed += ["calls"] if states.get("calls registry") == "created" else []
        if missing := sorted(data["files"][name] for name in needed if name in data["files"] and name not in tokens):
            raise AdminError("the key file " + ", ".join(missing) + " this operation wrote is unreadable or gone, so "
                             "undo cannot prove no other principal uses its credential; nothing changed")
        # A readable file is not yet this operation's credential: each token must resolve in its own store to the record
        # undo revokes, and the calls token must match the pin of the entry this operation wrote.
        replaced = [data["files"][UNDO_FILES[step]] for step, service, revoke in records if UNDO_FILES[step] in tokens
                    and public_id(service, one(run, service, LOOKUP[service],
                                               (tokens[UNDO_FILES[step]] + "\n").encode())) != revoke[-1]]
        if states.get("calls registry") == "created" and "calls" in tokens and registry is not None \
                and not pinned(tokens["calls"], (registry.entry(new) or {}).get("key_sha256")):
            replaced.append(data["files"]["calls"])
        if replaced:
            raise AdminError("the key file " + ", ".join(sorted(replaced)) + " no longer holds the credential this "
                             "operation created, so undo cannot prove no other principal uses it; nothing changed")
        if users := references(config, policy, registry, new, data, tokens):
            raise AdminError("a credential this operation created is used by " + ", ".join(users) + "; nothing changed")
        # Rules (c) and (h) before the first journal change: every validator a later rename needs answers.
        images = rollback_images(host)
        if states["gateway policy"] == "created":
            preflight(config, docker, policy, "policy", found["policy"], images)
        if states.get("calls registry") == "created":
            preflight(config, docker, registry, "registry", found["registry"], [])
        data["state"] = "undoing"
        journal.save()
        disabled, deleted = [], []
        try:
            for name, what, extra in (("gateway policy", "policy", images), ("calls registry", "registry", [])):
                if what == "registry" and registry is None:
                    continue
                # Ownership is checked on the very document the compare-and-swap replaces.
                document = Document(config["policy" if what == "policy" else "calls_registry"], name)
                entry = document.entry(new)
                if owned(data, name, entry, new) == "created":
                    after = {**entry, "enabled": False}
                    journal.step("undo " + name, "started", entry=entry_digest(after))
                    replace(config, docker, document, document.with_entry(new, after), what, found[what], extra)
                    checkpoint("undo " + name, "renamed")
                    journal.step("undo " + name, "done")
                    disabled.append(name)
            for step, service, revoke in records:
                journal.step("undo " + step, "started", public_id=revoke[-1])
                ask(run, service, revoke)
                checkpoint("undo " + step, "committed")
                journal.step("undo " + step, "done")
            for path in data["files"].values():
                if os.path.lexists(path):
                    Path(path).unlink()
                    deleted.append(path)
        except (AdminError, OSError) as exc:
            reason = str(exc) if isinstance(exc, AdminError) else type(exc).__name__
            raise Stopped(f"undo of {operation} stopped: {reason}; run `deedkit admin undo {operation}` again") from None
        data["state"] = "undone"
        data["undo"] = {"revoked": [{"store": step, "public_id": revoke[-1]} for step, _, revoke in records],
                        "disabled": disabled, "deleted_files": deleted}
        journal.save()
    return {**result, **data["undo"], "next": f"on the Mac: python3 scripts/deedkit.py key uninstall {new}"}


# --- `key extend` and `key revoke` (ANYM-497) under the same shared rules -----------------------------------------


EXTEND_DAYS = (1, 183)  # ANYM-497: within every store's extend limit (365) and the calendar's issue lifetime (183)
# Backends extend by their own database clock: the host's pre-write checks keep this margin, so a backend clock up
# to an hour off cannot refuse (expired, would shorten) after an earlier store was already extended.
SKEW = timedelta(hours=1)
STEPS = ("messaging", "messaging-archive", "calendar")  # the backend records, in their order
ORDER = ("gateway", "messaging", "messaging-archive", "calendar", "calls")
DOCUMENT_STEPS = {"gateway": "gateway policy", "calls": "calls registry"}
# One MCP initialize per bearer on stdin, from a disposable container in the gateway's own network namespace; prints
# only the HTTP statuses (0 for no answer), never a reply body.
PROBE = r"""
import json, sys, urllib.error, urllib.request
host, port = sys.argv[1], int(sys.argv[2])
body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "deedkit-admin", "version": "1"}}})
opener, statuses = urllib.request.build_opener(urllib.request.ProxyHandler({})), []
for token in sys.stdin.read().split():
    request = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body.encode(), method="POST", headers={
        "Host": host, "Authorization": "Bearer " + token, "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    try:
        with opener.open(request, timeout=10) as response:
            statuses.append(response.status)
    except urllib.error.HTTPError as exc:
        statuses.append(exc.code)
    except OSError:
        statuses.append(0)
print(json.dumps({"statuses": statuses}))
"""


def policy_read(config: dict, gateway: dict) -> str:
    """The path inside `gateway` of the policy file it enforces: its `--policy` must name the configured file through
    its directory mount, else a write or a digest proof would concern a file it does not read. The gateway's argparse
    takes the last `--policy` and any unique prefix of it (`--pol`), so anything but exactly one refuses."""
    command = [str(part) for part in [*gateway["entrypoint"], *gateway["command"]]]
    values = [part.partition("=")[2] if "=" in part else (command[i + 1] if i + 1 < len(command) else None)
              for i, part in enumerate(command)
              if len(name := part.partition("=")[0]) >= 5 and "--policy".startswith(name)]
    value = values[0] if len(values) == 1 else None
    expected = str(PurePosixPath(gateway["target"]) / Path(config["policy"]).name)
    if len(values) > 1:
        raise AdminError(f"{gateway['name']} names --policy {len(values)} times; nothing changed")
    if value is None or posixpath.normpath(value) != expected:
        raise AdminError(f"{gateway['name']} enforces {value or 'no --policy'}, not the configured policy {expected}; "
                         "nothing changed")
    return expected


def documents(config: dict, docker) -> tuple[Document, Document | None, dict]:
    policy = Document(config["policy"], "gateway policy")
    registry = Document(config["calls_registry"], "calls registry") if config.get("calls_registry") else None
    containers = running(docker)
    found = {"policy": consumers(containers, config["policy"], "gateway policy")}
    if registry is not None:
        found["registry"] = consumers(containers, config["calls_registry"], "calls registry")
    return policy, registry, found


def record_of(run, service: str, token: str) -> dict:
    """The store's own public record of a token (rule (h): its lookup answers in check mode first)."""
    checked(run, service, LOOKUP[service], (token + "\n").encode())
    record = one(run, service, LOOKUP[service], (token + "\n").encode())
    found = record if record.get("found") else None
    return {"public_id": public_id(service, record) if found else None,
            "expires_at": record.get("expires_at") if found else None,
            "status": status(found, datetime.now(timezone.utc))}


def holdings(config: dict, run, policy: Document, registry: Document | None, principal: str) -> dict:
    """The principal's credential in every store, found through its own key files: each store's public ID, expiry
    and state. The tokens and their host files stay in memory (lookups and rule (f)), never in the journal."""
    entry = policy.entry(principal)
    if entry is None:
        raise AdminError(f"no gateway principal {principal!r}; nothing changed")
    credentials = entry.get("credentials") if isinstance(entry.get("credentials"), dict) else {}
    if set(credentials) - set(BACKENDS) or not all(isinstance(c, dict) for c in credentials.values()):
        raise AdminError(f"{principal!r} has a credential this command does not know; nothing changed")
    now, tokens, files, calls = datetime.now(timezone.utc), {}, {}, None
    stores = {"gateway": {"public_id": principal, "expires_at": entry.get("expires_at"), "status": status(
        {"revoked": entry.get("enabled") is not True, "expires_at": entry.get("expires_at")}, now)}}
    for backend, credential in sorted(credentials.items()):
        token = read_token(config, credential.get("key_file", ""))
        if token is None or not pinned(token, credential.get("key_sha256")):
            raise AdminError(f"the {backend} key file of {principal!r} is unreadable or does not match its pin, so its "
                             "credential cannot be identified; nothing changed")
        tokens[backend], files[backend] = token, host_path(config, credential["key_file"])
    for backend in ("messaging", "calendar"):
        if backend in tokens:
            stores[backend] = record_of(run, backend, tokens[backend])
    if "calls" in tokens:
        if registry is None:
            raise AdminError(f"{principal!r} has a calls key but no calls registry is configured; nothing changed")
        digest = hashlib.sha256(tokens["calls"].encode()).hexdigest()
        calls = next((row for row in registry.value["principals"] if row.get("key_sha256") == digest), None)
        if calls is None:
            raise AdminError(f"the calls key of {principal!r} has no calls registry entry; nothing changed")
        stores["calls"] = {"public_id": calls.get("id"), "expires_at": calls.get("expires_at"), "status": status(
            {"revoked": calls.get("enabled") is not True, "expires_at": calls.get("expires_at")}, now)}
        if calls.get("archive_config"):
            archive = host_path(config, calls["archive_config"])
            try:
                settings = json.loads(read_private(archive, 65536))
                token = read_token(config, settings["token_file"])
            except (OSError, ValueError, KeyError, TypeError, AdminError):
                token = None
            if token is None:
                raise AdminError(f"the calls archive configuration of {principal!r} is unreadable; nothing changed")
            tokens["archive-token"] = token
            files.update({"archive-config": archive, "archive-token": host_path(config, settings["token_file"])})
            stores["messaging-archive"] = record_of(run, "messaging", token)
    return {"entry": entry, "calls": calls, "stores": stores, "tokens": tokens, "files": files}


def exclusive(config: dict, policy: Document, registry: Document | None, principal: str, held: dict) -> None:
    """Rule (f): extend and revoke act only on credentials no other principal references (directly or through a calls
    archive configuration); an unreadable reference cannot be ruled out and counts."""
    if users := references(config, policy, registry, principal, {"files": held["files"]}, held["tokens"],
                           held["calls"].get("id") if held["calls"] else None):
        raise AdminError(f"a credential of {principal!r} is also used by " + ", ".join(users)
                         + ": extend and revoke act only on a credential one principal holds; nothing changed")


def resume(config: dict, args, command: dict) -> Journal:
    """The operation's journal: a new ID, or a rerun of `--operation` with the same arguments."""
    slug = re.sub(r"[^a-z0-9.-]+", "-", command["id"].lower()).strip("-.")[:50] or "principal"
    operation = args.operation or (f"{command['verb']}-{slug}-{datetime.now(timezone.utc):%Y%m%dt%H%M%S}-"
                                   f"{secrets.token_hex(3)}")
    journal = Journal(config, operation)
    if args.operation:
        data = journal.load()
        if data.get("command") != command:
            raise AdminError(f"operation {operation} was started with other arguments; nothing changed")
        if data.get("state") in ("undoing", "undone"):
            raise AdminError(f"operation {operation} was undone; nothing changed")
    return journal


def public_ids(held: dict) -> dict:
    return {name: row["public_id"] for name, row in held["stores"].items()}


def stopped(journal: Journal, exc: Exception, undo: bool = True) -> Stopped:
    reason = str(exc) if isinstance(exc, AdminError) else type(exc).__name__
    journal.data.update(state="failed", error=reason)
    journal.save()
    operation = journal.data["operation"]
    rerun = (f"rerun the same command with `--operation {operation}` (a step already sent is read back from its "
             "store, never repeated blind)")
    return Stopped(f"operation {operation} stopped: {reason}. "
                   + (f"Either {rerun}, or run `deedkit admin undo {operation}`" if undo else rerun.capitalize()))


def swap(config: dict, docker, journal: Journal, name: str, what: str, ident: str, change, found: list[dict],
         extra: list[str], expect: str | None = None) -> bool:
    """One policy or registry entry changed by `change`, as a compare-and-swap: the entry as this step first read it
    ("before", or `expect`) or as it wrote it ("entry"); anything else is someone else's change and refuses."""
    document = Document(config["policy" if what == "policy" else "calls_registry"], name)
    current = document.entry(ident)
    if current is None:
        raise AdminError(f"the {name} entry {ident!r} is gone")
    step, digest = journal.data["steps"].get(name), entry_digest(current)
    if step is not None and digest == step["entry"]:
        if step["state"] != "done":
            journal.step(name, "done")
        return step["changed"]
    if digest != (step["before"] if step is not None else expect or digest):
        raise AdminError(f"the {name} entry {ident!r} changed since this operation read it")
    after = change(current)
    journal.step(name, "started", before=digest, entry=entry_digest(after), changed=after != current)
    if after != current:
        replace(config, docker, document, document.with_entry(ident, after), what, found, extra)
        checkpoint(name, "renamed")
    journal.step(name, "done")
    return after != current


def backend_write(step: str, row: dict, argv: list[str]) -> tuple[str, list[str]]:
    service = "calendar" if step == "calendar" else "messaging"
    head = {"extend": {"messaging": ["extend-key", "--key-id"], "calendar": ["extend-agent", "--id"]},
            "revoke": {"messaging": ["revoke-key", "--key-id"], "calendar": ["revoke-agent", "--id"]}}[argv[0]]
    return service, [*head[service], str(row["public_id"]), *argv[1:]]


def extend(config: dict, run, docker, host_factory, args) -> dict:
    principal, days = args.principal, args.days
    if not EXTEND_DAYS[0] <= days <= EXTEND_DAYS[1]:
        raise AdminError(f"--days is {EXTEND_DAYS[0]}-{EXTEND_DAYS[1]} for every store; nothing changed")
    command = {"verb": "extend", "id": principal, "days": days}
    journal = resume(config, args, command)
    if journal.data.get("state") == "done":
        return journal.data["result"]
    host = None if args.dry_run else host_factory()
    with ExitStack() as held:
        if host is not None:
            held.enter_context(guarded(config, host, docker))
        policy, registry, found = documents(config, docker)
        target = holdings(config, run, policy, registry, principal)
        exclusive(config, policy, registry, principal, target)
        if args.operation and {name: row["public_id"] for name, row in journal.data.get("stores", {}).items()} \
                != public_ids(target):
            raise AdminError(f"the credentials of {principal!r} changed since operation {journal.data['operation']} "
                             "started; nothing changed")
        for gateway in found["policy"]:
            policy_read(config, gateway)
        now = datetime.now(timezone.utc)
        bound = journal.data.get("bound") or (now + timedelta(days=days)).isoformat()
        steps = journal.data.get("steps", {})
        for name in (name for name in ORDER if name in target["stores"]):
            row, step = target["stores"][name], DOCUMENT_STEPS.get(name, name)
            # Every credential, a step done before included, is live now: a rerun never extends past a revocation.
            if row["status"] != "live":
                raise AdminError(f"the {name} credential is {row['status']}: extend refuses a revoked or expired "
                                 "credential (issue a new key instead); nothing changed")
            if steps.get(step, {}).get("state") == "done":
                continue
            # A step already sent is read back from its store on a rerun; only an unsent one is compared, with the
            # clock margin on both sides.
            if step not in steps and instant(row["expires_at"]) <= now + SKEW:
                raise AdminError(f"the {name} credential ends {row['expires_at']}, within an hour: a store whose clock "
                                 "runs ahead could refuse it as expired after another store was extended; issue a new "
                                 "key instead; nothing changed")
            if step not in steps and instant(row["expires_at"]) > instant(bound) - SKEW:
                raise AdminError(f"the {name} credential ends {row['expires_at']}, within an hour of {days} days from "
                                 "now or later, so extend would move it earlier (expiries never move earlier); "
                                 "nothing changed")
        writes = [(step, *backend_write(step, target["stores"][step], ["extend", "--days", str(days)]))
                  for step in STEPS if step in target["stores"]]
        for _, service, argv in writes:
            checked(run, service, argv)
        if args.dry_run:
            return {"dry_run": True, "principal": principal, "days": days,
                    "steps": [step for step, *_ in writes] + [DOCUMENT_STEPS[name] for name in ("calls", "gateway")
                                                               if name in target["stores"]],
                    "stores": [{"store": name, "public_id": target["stores"][name]["public_id"],
                                "previous_expires_at": target["stores"][name]["expires_at"],
                                "planned_expires_at": bound} for name in ORDER if name in target["stores"]],
                    "note": "the gateway ends at the earliest expiry the stores read back"}
        preflight(config, docker, policy, "policy", found["policy"], rollback_images(host))
        if target["calls"] is not None and registry is not None:
            preflight(config, docker, registry, "registry", found["registry"], [])
        if not args.operation:
            journal.create({"version": 1, "operation": journal.path.stem, "command": command, "state": "running",
                            "bound": bound, "steps": {},
                            "stores": {name: {"public_id": row["public_id"], "previous_expires_at": row["expires_at"]}
                                       for name, row in target["stores"].items()}})
        try:
            return run_extend(config, run, docker, host, journal, target, writes, found)
        except (AdminError, OSError) as exc:
            raise stopped(journal, exc, undo=False) from None


def run_extend(config, run, docker, host, journal: Journal, target: dict, writes, found) -> dict:
    data, principal = journal.data, journal.data["command"]["id"]
    earlier = [step for step, *_ in writes if data["steps"].get(step, {}).get("state") == "done"]
    for step, service, argv in writes:
        if data["steps"].get(step, {}).get("state") == "done":
            continue
        previous = instant(data["stores"][step]["previous_expires_at"])
        token = target["tokens"]["archive-token" if step == "messaging-archive" else step]
        # A step sent before a crash committed when the store's expiry moved since the journal's read.
        if data["steps"].get(step) is None or instant(target["stores"][step]["expires_at"]) == previous:
            journal.step(step, "started", public_id=argv[2])
            ask(run, service, argv)
            checkpoint(step, "committed")
        row = record_of(run, service, token)
        if row["status"] != "live" or row["public_id"] != argv[2] or instant(row["expires_at"]) <= previous:
            raise AdminError(f"{service}: {argv[0]} did not read back a later expiry for {argv[2]}")
        journal.step(step, "done", expires_at=row["expires_at"])
    after = {step: data["steps"][step]["expires_at"] for step, *_ in writes}
    # A step done before this run counts with its current record (extend refused it unless live).
    after.update({step: target["stores"][step]["expires_at"] for step in earlier})
    if target["calls"] is not None:
        swap(config, docker, journal, "calls registry", "registry", target["calls"]["id"],
             lambda entry: {**entry, "expires_at": data["bound"]}, found["registry"], [])
        after["calls"] = data["bound"]
    # The gateway never outlives a backend: it ends at the earliest expiry read back, whatever the delays or clocks.
    after["gateway"] = min([data["bound"], *after.values()], key=instant)
    # Compared with the entry as this run read it (a rerun may follow someone else's change), and published only over
    # that same entry.
    previous = target["stores"]["gateway"]["expires_at"]
    if previous and instant(after["gateway"]) < instant(previous):
        raise AdminError(f"a store read back an expiry before the gateway's {previous} (its clock runs behind), so "
                         "the gateway is left unchanged: expiries never move earlier")
    swap(config, docker, journal, "gateway policy", "policy", principal,
         lambda entry: {**entry, "expires_at": after["gateway"]}, found["policy"], rollback_images(host),
         expect=entry_digest(target["entry"]))
    data["result"] = {
        "operation": data["operation"], "principal": principal, "days": data["command"]["days"],
        "expires_at": after["gateway"],
        "stores": [{"store": name, "public_id": data["stores"][name]["public_id"],
                    "previous_expires_at": data["stores"][name]["previous_expires_at"], "expires_at": after[name]}
                   for name in ORDER if name in data["stores"]],
        "note": "tokens and key hashes are unchanged, so no client changes; an extend has no undo"}
    data["state"] = "done"
    journal.save()
    return data["result"]


def bearer(docker, gateway: dict, public_url: str, tokens: list[str]) -> list[int]:
    """The gateway's HTTP status for one MCP initialize per token, from a disposable container in its network
    namespace; the tokens go on stdin only."""
    command = [str(part) for part in gateway["command"]]
    value = {part: command[i + 1] for i, part in enumerate(command[:-1]) if part in ("--port", "--public-url")}
    port, origin = value.get("--port", ""), urlsplit(value.get("--public-url", public_url)).netloc
    if not port.isdigit() or not origin:
        raise AdminError(f"{gateway['name']} names no --port or public URL, so no bearer probe can reach it")
    name = "deedkit-admin-" + uuid4().hex[:12]
    done = docker(["run", "--rm", "-i", "--name", name, "--network", f"container:{gateway['id']}", "--read-only",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pull", "never",
                   *(["--user", gateway["user"]] if gateway["user"] else []),
                   "--entrypoint", "python", gateway["image"], "-c", PROBE, origin, port], name,
                  ("\n".join(tokens) + "\n").encode())
    try:
        statuses = json.loads(done.stdout.decode().strip().splitlines()[-1])["statuses"] if not done.returncode else None
    except (ValueError, IndexError, KeyError, TypeError, UnicodeDecodeError):
        statuses = None
    if not isinstance(statuses, list) or len(statuses) != len(tokens) or not all(type(s) is int for s in statuses):
        raise AdminError(f"{gateway['name']}: the bearer probe gave no answer")
    return statuses


def inside(docker, gateway: dict, path: str) -> bytes | None:
    """The file a running gateway reads at `path`, copied out by the daemon (`docker cp`, no process started)."""
    copied = docker(["cp", f"{gateway['id']}:{path}", "-"])
    if copied.returncode:
        return None
    try:
        with tarfile.open(fileobj=io.BytesIO(copied.stdout)) as archive:
            files = [member for member in archive.getmembers() if member.isfile()]
            handle = archive.extractfile(files[0]) if len(files) == 1 and files[0].size <= LIMIT else None
            return handle.read() if handle is not None else None
    except (tarfile.TarError, OSError):
        return None


def proof(config: dict, docker, host: Host, gateways: list[dict], principal: str, token: str | None,
          control: str | None) -> dict:
    """Revocation evidence: every running gateway reads the published policy (its in-container copy has the same
    digest), which disables the principal; with the principal's token, its MCP initialize answers 401 at each gateway
    right after a live control principal's initialize answered 200 there, so a 401 for another reason never counts."""
    published = read_private(config["policy"])
    value = json.loads(published)
    row = next((row for row in value.get("principals", []) if row.get("id") == principal), None)
    if row is None or row.get("enabled") is not False:
        raise AdminError(f"the published policy does not disable {principal!r}: revocation not proven")
    digest = hashlib.sha256(published).hexdigest()
    for gateway in gateways:
        copy = inside(docker, gateway, policy_read(config, gateway))
        if copy is None or hashlib.sha256(copy).hexdigest() != digest:
            raise AdminError(f"{gateway['name']} does not read the published policy (its in-container copy differs or "
                             "is unreadable): revocation not proven")
    names = [gateway["name"] for gateway in gateways]
    evidence = {"gateways": names, "policy_revision": value.get("revision")}
    if token is None or control is None:
        return {**evidence, "bearer_probe": "the bearer probe was not run (no --token-file): the evidence is each "
                                            "running gateway's in-container policy, which disables the principal"}
    for gateway in gateways:
        first, second = bearer(docker, gateway, host.http["http_release"]["gateway_public_url"], [control, token])
        if first != 200:
            raise AdminError(f"{gateway['name']}: the control principal's initialize answered {first}, so a 401 would "
                             "prove nothing: revocation not proven")
        if second != 401:
            raise AdminError(f"{gateway['name']}: the revoked bearer's initialize answered {second}, not 401: "
                             "revocation not proven")
    return {**evidence, "bearer_probe": f"rejected (401) by {len(names)} running gateway(s), each right after a "
                                        "control principal's initialize answered 200"}


def revoke(config: dict, run, docker, host_factory, args) -> dict:
    principal = args.principal
    command = {"verb": "revoke", "id": principal, "token_file": args.token_file is not None}
    journal = resume(config, args, command)
    if journal.data.get("state") == "done":
        return journal.data["result"]
    host = None if args.dry_run else host_factory()
    with ExitStack() as held:
        if host is not None:
            held.enter_context(guarded(config, host, docker))
        policy, registry, found = documents(config, docker)
        target = holdings(config, run, policy, registry, principal)
        for gateway in found["policy"]:
            policy_read(config, gateway)
        token = control = None
        if args.token_file is not None:
            token = read_token({}, str(args.token_file))
            if token is None:
                raise AdminError(f"the token file {args.token_file} is unreadable or not one token; nothing changed")
            if not pinned(token, target["entry"].get("key_sha256")):
                raise AdminError(f"the token file does not match the key_sha256 of {principal!r}; nothing changed")
            control = read_token(config, config["probe_control"]) if config.get("probe_control") else None
            if control is None or pinned(control, target["entry"].get("key_sha256")):
                raise AdminError("the bearer probe needs `probe_control` in the admin configuration: the token file of "
                                 "another live principal; nothing changed")
        exclusive(config, policy, registry, principal, target)
        if args.operation and {name: row["public_id"] for name, row in journal.data.get("stores", {}).items()} \
                != public_ids(target):
            raise AdminError(f"the credentials of {principal!r} changed since operation {journal.data['operation']} "
                             "started; nothing changed")
        writes = [(step, *backend_write(step, target["stores"][step], ["revoke"])) for step in STEPS
                  if step in target["stores"] and target["stores"][step]["status"] not in ("revoked", "missing")]
        for _, service, argv in writes:
            checked(run, service, argv)
        steps = ["gateway policy", *(step for step in STEPS if step in target["stores"])] + (
            ["calls registry"] if target["calls"] is not None else [])
        if args.dry_run:
            return {"dry_run": True, "principal": principal, "steps": steps,
                    "stores": [{"store": name, "public_id": target["stores"][name]["public_id"],
                                "status": target["stores"][name]["status"]} for name in ORDER
                               if name in target["stores"]],
                    "bearer_probe": "planned" if token else "not run (no --token-file)"}
        images = rollback_images(host)
        preflight(config, docker, policy, "policy", found["policy"], images)
        if target["calls"] is not None and registry is not None:
            preflight(config, docker, registry, "registry", found["registry"], [])
        if not args.operation:
            journal.create({"version": 1, "operation": journal.path.stem, "command": command, "state": "running",
                            "steps": {}, "stores": {name: {"public_id": row["public_id"], "status": row["status"]}
                                                    for name, row in target["stores"].items()}})
        try:
            return run_revoke(config, run, docker, host, journal, target, found, images, token, control)
        except (AdminError, OSError) as exc:
            raise stopped(journal, exc) from None


def run_revoke(config, run, docker, host, journal: Journal, target: dict, found, images, token, control) -> dict:
    data, principal = journal.data, journal.data["command"]["id"]
    # The gateway first: access ends on the next request, before any backend record changes.
    swap(config, docker, journal, "gateway policy", "policy", principal, lambda entry: {**entry, "enabled": False},
         found["policy"], images)
    for step in (step for step in STEPS if step in target["stores"]):
        row = target["stores"][step]
        if data["steps"].get(step, {}).get("state") == "done":
            continue
        if row["status"] in ("revoked", "missing"):  # read from the store: already revoked, or sent before a crash
            journal.step(step, "done", public_id=row["public_id"], found=row["status"])
            continue
        service, argv = backend_write(step, row, ["revoke"])
        journal.step(step, "started", public_id=row["public_id"])
        ask(run, service, argv)
        checkpoint(step, "committed")
        if record_of(run, service, target["tokens"]["archive-token" if step == "messaging-archive" else step]
                     )["status"] != "revoked":
            raise AdminError(f"{service}: {argv[0]} did not read back {row['public_id']} revoked")
        journal.step(step, "done")
    if target["calls"] is not None:
        swap(config, docker, journal, "calls registry", "registry", target["calls"]["id"],
             lambda entry: {**entry, "enabled": False}, found["registry"], [])
    evidence = proof(config, docker, host, found["policy"], principal, token, control)
    data["result"] = {
        "operation": data["operation"], "principal": principal,
        "revoked": [{"store": name, "public_id": data["stores"][name]["public_id"]} for name in ORDER
                    if name in data["stores"]],
        "evidence": evidence, "undo": f"deedkit admin undo {data['operation']}",
        "note": "records stay for audit; deleting them is a separate owner decision"}
    data["state"] = "done"
    journal.save()
    return data["result"]


def undo_revoke(config: dict, run, docker, host_factory, journal: Journal) -> dict:
    """Compensation of a revoke: enable again only the gateway and calls entries this operation disabled, each as a
    compare-and-swap on the entry it wrote; messaging and calendar revocations are final."""
    data = journal.data
    principal = data["command"]["id"]
    result = {"operation": data["operation"], "principal": principal, "undone": True,
              "note": "messaging and calendar revocations are final (records stay for audit): only the gateway and "
                      "calls entries are enabled again, so a working key needs `deedkit admin key issue`"}
    if data.get("state") == "undone":
        return {**result, **data.get("undo", {})}
    host = host_factory()
    with guarded(config, host, docker):
        policy, registry, found = documents(config, docker)
        images, plan = rollback_images(host), []
        for name, what, document, ident in (
                ("gateway policy", "policy", policy, principal),
                ("calls registry", "registry", registry, data["stores"].get("calls", {}).get("public_id"))):
            step = data["steps"].get(name, {})
            if document is None or not step.get("changed"):
                continue
            current = document.entry(ident)
            digest = entry_digest(current) if current is not None else None
            if digest in (step["before"], data["steps"].get("undo " + name, {}).get("entry")):
                continue  # never renamed, or already enabled again by this undo
            if digest != step["entry"]:
                raise AdminError(f"the {name} entry {ident!r} changed since this operation disabled it; nothing changed")
            preflight(config, docker, document, what, found[what], images if what == "policy" else [])
            plan.append((name, what, ident, digest))
        data["state"] = "undoing"
        journal.save()
        try:
            for name, what, ident, digest in plan:
                swap(config, docker, journal, "undo " + name, what, ident, lambda entry: {**entry, "enabled": True},
                     found[what], images if what == "policy" else [], expect=digest)
        except (AdminError, OSError) as exc:
            reason = str(exc) if isinstance(exc, AdminError) else type(exc).__name__
            raise Stopped(f"undo of {data['operation']} stopped: {reason}; run `deedkit admin undo "
                          f"{data['operation']}` again") from None
        data["state"] = "undone"
        enabled = [name for name in ("gateway policy", "calls registry")
                   if data["steps"].get("undo " + name, {}).get("changed")]
        data["undo"] = {"enabled": enabled, "final": [{"store": step, "public_id": data["stores"][step]["public_id"]}
                                                      for step in STEPS if step in data["steps"]]}
        journal.save()
    return {**result, **data["undo"]}


# --- `profile tools`: the tool names a gateway profile exposes, under the same shared rules -----------------------


def profile_of(document: Document, name: str) -> dict:
    profiles = document.value.get("profiles")
    profile = profiles.get(name) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict) or not isinstance(profile.get("tools"), list) \
            or not all(isinstance(tool, str) for tool in profile["tools"]):
        raise AdminError(f"no gateway profile {name!r} with a tool list; nothing changed")
    return profile


def retooled(profile: dict, add: list[str], remove: list[str]) -> dict:
    """The profile with `remove` taken out and the missing `add` appended; its other fields and the order kept."""
    kept = [tool for tool in profile["tools"] if tool not in remove]
    return {**profile, "tools": kept + [tool for tool in add if tool not in kept]}


def swap_profile(config: dict, docker, journal: Journal, step_name: str, name: str, change, found: list[dict],
                 extra: list[str], expect: str | None = None) -> bool:
    """`swap` for one profile: compare-and-swap on the profile as this step first read it ("before", or `expect`) or as
    it wrote it ("entry"); anything else is someone else's change and refuses."""
    document = Document(config["policy"], "gateway policy")
    current = profile_of(document, name)
    step, digest = journal.data["steps"].get(step_name), entry_digest(current)
    if step is not None and digest == step["entry"]:
        if step["state"] != "done":
            journal.step(step_name, "done")
        return step["changed"]
    if digest != (step["before"] if step is not None else expect or digest):
        raise AdminError(f"the gateway profile {name!r} changed since this operation read it")
    after = change(current)
    journal.step(step_name, "started", before=digest, entry=entry_digest(after), changed=after != current)
    if after != current:
        replace(config, docker, document, document.with_profile(name, after), "policy", found, extra)
        checkpoint(step_name, "renamed")
    journal.step(step_name, "done")
    return after != current


def profile_tools(config: dict, run, docker, host_factory, args) -> dict:
    name, add, remove = args.profile, args.add or [], args.remove or []
    if not (add or remove) or len(set(add)) != len(add) or len(set(remove)) != len(remove) or set(add) & set(remove) \
            or not all(TOOL_NAME.fullmatch(tool) for tool in add + remove):
        raise AdminError("give --add and/or --remove with distinct tool names, none in both; nothing changed")
    command = {"verb": "profile", "id": name, "add": add, "remove": remove}
    journal = resume(config, args, command)
    if journal.data.get("state") == "done":
        return journal.data["result"]
    host = None if args.dry_run else host_factory()
    with ExitStack() as held:
        if host is not None:
            held.enter_context(guarded(config, host, docker))
        policy = Document(config["policy"], "gateway policy")
        found = {"policy": consumers(running(docker), config["policy"], "gateway policy")}
        for gateway in found["policy"]:
            policy_read(config, gateway)
        current = profile_of(policy, name)
        # A rerun keeps the plan of the first run (its tools may already be listed since) and the digest of the profile
        # that plan was made from: the first write, even a resumed one, refuses a profile changed after it.
        plan = journal.data.get("plan") or {"digest": entry_digest(current), "before": list(current["tools"]),
                                            "added": [tool for tool in add if tool not in current["tools"]],
                                            "removed": [tool for tool in remove if tool in current["tools"]]}
        users = [{"principal": row.get("id"), "enabled": row.get("enabled") is True}
                 for row in policy.value["principals"] if row.get("profile") == name]
        if args.dry_run:
            return {"dry_run": True, "profile": name, "revision": policy.value["revision"],
                    "added": plan["added"], "removed": plan["removed"], "principals": users,
                    "already_listed": [tool for tool in add if tool not in plan["added"]],
                    "not_listed": [tool for tool in remove if tool not in plan["removed"]]}
        images = rollback_images(host)
        # Rules (c) and (h) before the journal: the live document in every image, then the candidate itself.
        preflight(config, docker, policy, "policy", found["policy"], images)
        candidate = retooled(current, add, remove)
        if candidate != current:
            text = (json.dumps(policy.with_profile(name, candidate), ensure_ascii=False, indent=2) + "\n").encode()
            with staged(policy.path, text) as staging:
                validate(config, docker, "policy", staging, found["policy"], images, "nothing changed")
        if not args.operation:
            journal.create({"version": 1, "operation": journal.path.stem, "command": command, "state": "running",
                            "plan": plan, "steps": {}})
        try:
            swap_profile(config, docker, journal, "gateway policy", name,
                         lambda profile: retooled(profile, add, remove), found["policy"], images,
                         expect=plan["digest"])
            data = journal.data
            data["result"] = {"operation": data["operation"], "profile": name, "added": plan["added"],
                              "removed": plan["removed"], "principals": users,
                              "revision": Document(config["policy"], "gateway policy").value["revision"],
                              "undo": f"deedkit admin undo {data['operation']}",
                              "note": "every principal of the profile gets only the tools its own grants allow; "
                                      "reconnect clients that cache the tool list"}
            data["state"] = "done"
            journal.save()
            return data["result"]
        except (AdminError, OSError) as exc:
            raise stopped(journal, exc) from None


def undo_profile(config: dict, docker, host_factory, journal: Journal) -> dict:
    """Compensation of a profile change: the tool list it started from, only while the profile is still exactly what
    this operation wrote (compare-and-swap); a later change by anyone refuses."""
    data = journal.data
    name = data["command"]["id"]
    result = {"operation": data["operation"], "profile": name, "undone": True}
    if data.get("state") == "undone":
        return {**result, **data.get("undo", {})}
    host = host_factory()
    with guarded(config, host, docker):
        policy = Document(config["policy"], "gateway policy")
        found = consumers(running(docker), config["policy"], "gateway policy")
        for gateway in found:  # the file the gateways enforce, as on the way forward
            policy_read(config, gateway)
        images, step = rollback_images(host), data["steps"].get("gateway policy", {})
        undo_step = data["steps"].get("undo gateway policy")
        current = entry_digest(profile_of(policy, name))
        # Nothing to restore when the change never renamed, or the profile already is its starting list; an undo step
        # that exists (an interrupted undo) is finished and its recorded outcome reported.
        needed = bool(step.get("changed")) and (undo_step is not None or current != step["before"])
        if needed and undo_step is None and current != step["entry"]:
            raise AdminError(f"the gateway profile {name!r} changed since this operation wrote it; nothing changed")
        if needed and (undo_step is None or current == undo_step["before"]):
            preflight(config, docker, policy, "policy", found, images)
        data["state"] = "undoing"
        journal.save()
        try:
            if needed:
                swap_profile(config, docker, journal, "undo gateway policy", name,
                             lambda profile: {**profile, "tools": list(data["plan"]["before"])}, found, images,
                             expect=current)
        except (AdminError, OSError) as exc:
            reason = str(exc) if isinstance(exc, AdminError) else type(exc).__name__
            raise Stopped(f"undo of {data['operation']} stopped: {reason}; run `deedkit admin undo "
                          f"{data['operation']}` again") from None
        restored = bool(data["steps"].get("undo gateway policy", {}).get("changed"))
        data["state"] = "undone"
        data["undo"] = {"restored": restored, "added_back": data["plan"]["removed"] if restored else [],
                        "taken_out": data["plan"]["added"] if restored else []}
        journal.save()
    return {**result, **data["undo"]}


def render(view: dict) -> str:
    lines = []
    for row in view["principals"]:
        state = "enabled" if row["enabled"] else "disabled"
        label = json.dumps(row["label"], ensure_ascii=False) if row["label"] else "-"
        lines.append(f"{row['id']}  {label}  profile={row['profile']}  {state}  expires {row['expires_at']}")
        for credential in row["credentials"]:
            lines.append(line("  ", credential))
            if "archive" in credential:
                lines.append(line("    archive ", credential["archive"]))
    lines.append(f"{len(view['principals'])} principals, {len(view['findings'])} findings")
    for item in view["findings"]:
        rest = "  ".join(f"{k}={v}" for k, v in item.items() if k != "code")
        lines.append(f"  {item['code']}  {rest}")
    return "\n".join(lines)


def line(indent: str, credential: dict) -> str:
    note = f"  ({credential['note']})" if credential.get("note") else ""
    return (f"{indent}{credential['backend']:<9} {credential['public_id'] or '-'}  {credential['status']}"
            f"  expires {credential['expires_at'] or '-'}{note}")


def delivery_endpoints(run, args, config: dict | None = None) -> dict:
    """The messaging operator commands for self-service endpoints (ANYM-519); a UUID is checked before any run.
    The list adds each owning principal's key label from the private admin file (ANYM-520)."""
    if args.verb == "list":
        listed = ask(run, "messaging", ["delivery-endpoints-list"] + (["--principal", args.principal] if args.principal else []))
        labels = load_labels(config or {})
        for item in listed.get("items", []):
            item["key_label"] = labels.get(item.get("principal"))
        return listed
    try:
        endpoint_id = str(UUID(args.endpoint_id))
    except ValueError:
        raise AdminError("delivery-endpoints: the endpoint ID is not a UUID; nothing changed") from None
    return ask(run, "messaging", [args.verb + "-delivery-endpoint", "--id", endpoint_id])


def main(argv: list[str] | None = None, run=None, docker=None, host=None) -> int:
    parser = argparse.ArgumentParser(prog="deedkit admin", description="DeedKit admin CLI (ANYM-491)")
    parser.add_argument("--config", type=Path, default=CONFIG, help=f"private admin configuration (default {CONFIG})")
    nouns = parser.add_subparsers(dest="noun", required=True)
    verbs = nouns.add_parser("key", help="agent keys").add_subparsers(dest="verb", required=True)
    listing = verbs.add_parser("list", help="every agent key, read-only")
    listing.add_argument("--principal")
    scope = listing.add_mutually_exclusive_group()
    scope.add_argument("--valid", action="store_const", const="valid", dest="scope")
    scope.add_argument("--expiring", action="store_const", const="expiring", dest="scope")
    scope.add_argument("--all", action="store_const", const="all", dest="scope")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--strict", action="store_true", help="exit 1 when there are findings")
    label = verbs.add_parser("label", help="set a principal's label in the private admin file")
    label.add_argument("principal")
    label.add_argument("text")
    issuing = verbs.add_parser("issue", help="a new principal with the same access as an existing one (ANYM-496)")
    issuing.add_argument("new")
    issuing.add_argument("--like", required=True)
    issuing.add_argument("--days", type=int, required=True, help=f"{DAYS[0]}-{DAYS[1]}, for every store")
    issuing.add_argument("--label")
    issuing.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    issuing.add_argument("--operation", help="rerun a stopped operation: finished steps replay, the rest continue")
    extending = verbs.add_parser("extend", help="extend a principal's key in every store (ANYM-497)")
    extending.add_argument("principal")
    extending.add_argument("--days", type=int, required=True, help=f"{EXTEND_DAYS[0]}-{EXTEND_DAYS[1]}, for every store")
    extending.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    extending.add_argument("--operation", help="rerun a stopped operation: finished steps are read back, the rest continue")
    revoking = verbs.add_parser("revoke", help="revoke a principal's key in every store, the gateway first (ANYM-497)")
    revoking.add_argument("principal")
    revoking.add_argument("--token-file", type=Path, help="the principal's own gateway token: prove a 401 afterwards")
    revoking.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    revoking.add_argument("--operation", help="rerun a stopped operation: finished steps are read back, the rest continue")
    profiles = nouns.add_parser("profile", help="gateway profiles").add_subparsers(dest="verb", required=True)
    tools = profiles.add_parser("tools", help="add or remove tool names in one gateway profile")
    tools.add_argument("profile")
    tools.add_argument("--add", nargs="+", metavar="TOOL", help="tool names to list (already listed: unchanged)")
    tools.add_argument("--remove", nargs="+", metavar="TOOL", help="tool names to unlist (not listed: unchanged)")
    tools.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    tools.add_argument("--operation", help="rerun a stopped operation: a finished step is read back, the rest continue")
    undoing = nouns.add_parser("undo", help="compensate an operation: disable, revoke and delete what it created")
    undoing.add_argument("operation")
    endpoints = nouns.add_parser("delivery-endpoints", help="agent webhook endpoints: the operator's disable (ANYM-519)")
    endpoint_verbs = endpoints.add_subparsers(dest="verb", required=True)
    endpoint_list = endpoint_verbs.add_parser("list", help="every self-service endpoint, read-only, no secret")
    endpoint_list.add_argument("--principal")
    for verb in ("disable", "enable"):
        endpoint_verbs.add_parser(verb, help=f"{verb} one endpoint; only an operator changes operator_disabled") \
            .add_argument("endpoint_id")
    args = parser.parse_args(argv)
    try:
        config = load_json(args.config, "admin configuration")
        if args.noun == "delivery-endpoints":
            print(json.dumps(delivery_endpoints(run or runner(config), args, config), ensure_ascii=False, indent=2))
            return 0
        if args.noun in ("undo", "profile") or args.verb in ("issue", "extend", "revoke"):
            os.umask(0o077)
            run, docker = run or runner(config), docker or dockers(config)
            factory = (lambda: host) if host is not None else (lambda: load_host(config))
            command = undo if args.noun == "undo" else {"issue": issue, "extend": extend, "revoke": revoke,
                                                        "tools": profile_tools}[args.verb]
            result = command(config, run, docker, factory, args.operation if args.noun == "undo" else args)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.verb == "label":
            print(json.dumps(set_label(config, args.principal, args.text), ensure_ascii=False))
            return 0
        now = datetime.now(timezone.utc)
        view = select(collect(config, run or runner(config), now), args.principal, args.scope or "all", now)
    except Stopped as exc:
        print(f"deedkit admin: {exc}", file=sys.stderr)
        return 1
    except (AdminError, KeyError) as exc:
        print(f"deedkit admin: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(view, ensure_ascii=False, indent=2) if args.json else render(view))
    return 1 if args.strict and view["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
