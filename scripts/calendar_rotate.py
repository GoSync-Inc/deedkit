#!/usr/bin/env python3
"""Rotate the calendar database passwords, one role set at a time; any interruption ends in a command that says how
to finish (docs/operations/production.md, "Rotate the calendar database passwords"; ANYM-784).

Stdlib only; run as root from the release's verified source directory. It prints fingerprints (12 hex of sha256),
never a value, a DSN or a file content. Recovery is forward only: a replaced value is never enabled again.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.parse
import uuid

sys.dont_write_bytecode = True  # the release's source directory stays as it was verified

# The production layout. Everything else is read from the calendar runner's configuration.
RUNNER_CONFIG = Path("/opt/deedkit/calendar-backup/runner.json")
PRODUCTION_CONFIG = Path("/etc/deedkit/production.json")
GLOBAL_LOCK_DIR = Path("/var/lib/deedkit-production")
RUNNER_PROGRAM = Path("/opt/personal-os-probe/ops/backup_runner.py")
RUNNER_PYTHON = "/usr/bin/python3"  # the calendar backup unit's interpreter
INSTALLED_TOOL = Path("/usr/local/lib/deedkit/production.py")
# An isolated rotation never opens anything under these, and refuses a profile directory there.
PRODUCTION_ROOTS = (Path("/opt/deedkit/calendar-state"), Path("/opt/deedkit/calendar-backup"))
PRODUCTION_PROJECT = "deedkit-calendar"

SERVICES = ("calendar-db", "calendar-api", "calendar-worker")  # the containers whose identity a rotation keeps
SETS = {"owner": ("fcs_owner",), "roles": ("fcs_gateway", "fcs_worker")}
OWNER = "fcs_owner"
PHASES = ("staged", "altered", "files", "verifying", "finished")
MARKER = "rotation.unsettled"
TOKEN_ENV = "DEEDKIT_ROTATION_BACKUP_TOKEN"  # backup_runner.py: the tool's own backup in phase `verifying`
ITERATIONS = 4096  # PostgreSQL's default scram_iterations

SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGQUIT, signal.SIGTSTP)
DOCKER_TIMEOUT, ALTER_TIMEOUT, PROBE_TIMEOUT, STOP_TIMEOUT = 60, 60, 30, 120
HEALTH_WAIT, HEALTH_POLL = 120, 2
BACKUP_TIMEOUT, BACKUP_STOP = 6300, 180  # the calendar backup unit's TimeoutStartSec and TimeoutStopSec

# Runs in a throwaway container of the database's own image, on its network: never the loopback address, whose
# pg_hba line is `trust`, and `require_auth` refuses anything but scram. The value comes on stdin into a tmpfs passfile,
# so it is in no argv and no environment; only a fixed word leaves.
PROBE_SCRIPT = (
    "umask 077; IFS= read -r line; printf '%s\\n' \"$line\" > /tmp/pgpass; "
    "if LC_ALL=C PGPASSFILE=/tmp/pgpass psql -X -A -t -w -d \"$1\" -c 'SELECT 1' >/dev/null 2>/tmp/err; "
    "then echo accepted; elif grep -q 'password authentication failed' /tmp/err; then echo refused; else echo error; fi")
# deploy/compose.calendar.yaml's healthcheck: the running API's own configuration, nothing printed.
HEALTH_SCRIPT = """import json, os, sys, urllib.request
try:
    with open(os.path.join(os.environ["FCS_DATA_DIR"], "service", "gateway.json")) as file:
        config = json.load(file)
    port = int(config["port"])
    host = (config.get("allowed_hosts") or [f"127.0.0.1:{port}"])[0]
    bind = config.get("bind", "127.0.0.1")
    address = "127.0.0.1" if bind in ("", "0.0.0.0", "::") else f"[{bind}]" if ":" in bind else bind
    request = urllib.request.Request(f"http://{address}:{port}/health", headers={"Host": host})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=8) as response:
        healthy = response.status == 200
except Exception:
    healthy = False
sys.exit(0 if healthy else 1)
"""
DSN = re.compile(r"(postgres(?:ql)?://)([^:/@\s]+):([^@\s]*)(@\S+)")


class Refused(Exception):
    """A fixed code, safe to print. The rotation's state on disk says what is next."""

    status, exit_code = "refused", 1

    def __init__(self, code, **fields):
        super().__init__(code)
        self.code, self.fields = code, fields


class Held(Refused):
    """Another operation holds what this one needs: nothing was changed by this attempt, try again later."""

    status, exit_code = "busy", 75


def checkpoint(name):
    """A named boundary between two durable steps; the tests stop the process here."""


def running_as_root():
    return os.geteuid() == 0


LOG = [None]


def say(**fields):
    """One JSON line. A dropped ssh never stops the work: the journal's directory keeps the same lines."""
    line = (json.dumps(fields, sort_keys=True) + "\n").encode()
    with suppress(OSError):
        os.write(1, line)
    if LOG[0] is not None:
        with suppress(OSError):
            descriptor = os.open(LOG[0], os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                write_all(descriptor, line)
            finally:
                os.close(descriptor)


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def scram_verifier(password, *, salt=None, iterations=ITERATIONS):
    """PostgreSQL's stored form `SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>`, computed here so the
    plaintext never reaches the server. The generated values are ASCII, which SASLprep leaves unchanged."""
    salt = secrets.token_bytes(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(data):
        return base64.b64encode(data).decode()

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(hashlib.sha256(client_key).digest())}:{b64(server_key)}"


# Durable files -----------------------------------------------------------------------------------------------------


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_all(descriptor, data):
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view):]


def read_private(path):
    """The bytes of a regular file, never through a symbolic link."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise Refused("not_a_regular_file", file=Path(path).name)
        chunks = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def durable_write(path, data, *, mode=0o600, uid=None, gid=None, check=None):
    """Stage beside `path` (full write, fsync), read the staged file back and parse it (`check`), rename, fsync the
    directory, read the result back. A failure leaves `path` as it was."""
    path = Path(path)
    staged = path.parent / f".{path.name}.{secrets.token_hex(4)}.tmp"
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            if uid is not None and gid is not None:
                os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, mode)
            write_all(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if read_private(staged) != data:
            raise Refused("staged_read_back_failed", file=path.name)
        if check:
            check(data)
        os.rename(staged, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(staged)
        raise
    fsync_directory(path.parent)
    if read_private(path) != data:
        raise Refused("read_back_failed", file=path.name)


def sweep_staged(directories):
    """What a killed writer left beside the profile files: never a file of the running configuration."""
    for directory in directories:
        with suppress(FileNotFoundError):
            for entry in os.listdir(directory):
                if entry.startswith(".") and entry.endswith(".tmp"):
                    os.unlink(Path(directory) / entry)


# Profile files: the password is the only thing that changes -----------------------------------------------------------


def read_password(kind, data, role):
    text = data.decode()
    if kind == "env":
        found = re.findall(r"^POSTGRES_PASSWORD=(.*)$", text, re.M)
        if len(found) != 1:
            raise Refused("profile_unreadable", kind=kind)
        return found[0]
    dsn = json.loads(text).get("dsn") if kind == "json" else text.strip()
    match = DSN.fullmatch(dsn) if isinstance(dsn, str) else None
    if not match:
        raise Refused("profile_unreadable", kind=kind)
    if urllib.parse.unquote(match[2]) != role:
        raise Refused("profile_role_mismatch", kind=kind, role=role)
    return urllib.parse.unquote(match[3])


def write_password(kind, data, role, value):
    """`data` with `value` as the role's password: every other key and line stays."""
    read_password(kind, data, role)
    text = data.decode()
    if kind == "env":
        return re.sub(r"^POSTGRES_PASSWORD=.*$", lambda _: "POSTGRES_PASSWORD=" + value, text, flags=re.M).encode()
    quoted = urllib.parse.quote(value, safe="")

    def swap(dsn):
        match = DSN.fullmatch(dsn)
        if not match:
            raise Refused("profile_unreadable", kind=kind)
        return match[1] + match[2] + ":" + quoted + match[4]

    if kind == "dsn":
        return (swap(text.strip()) + text[len(text.rstrip()):]).encode()
    document = json.loads(text)
    document["dsn"] = swap(document["dsn"])
    return (json.dumps(document, ensure_ascii=False) + text[len(text.rstrip()):]).encode()


# Docker: stderr is never read, so a client message cannot carry a value into the output ---------------------------------


def docker(*arguments, stdin=None, timeout=DOCKER_TIMEOUT, name=None):
    """(exit status, stdout), or (None, "") when the deadline passed: the client and the named container are killed."""
    process = subprocess.Popen(["docker", *arguments], stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        output, _ = process.communicate(stdin.encode() if stdin is not None else None, timeout=timeout)
        return process.returncode, output.decode(errors="replace")
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        if name:
            with suppress(Exception):
                subprocess.run(["docker", "kill", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=DOCKER_TIMEOUT, check=False)
        return None, ""


def inspect(container):
    code, output = docker("inspect", container)
    if code != 0:
        raise Refused("container_unreadable")
    return json.loads(output)[0]


def service_containers(project, service):
    code, output = docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + project,
                          "--filter", "label=com.docker.compose.service=" + service)
    if code != 0:
        raise Refused("docker_unreadable")
    return output.split()


def production_tool():
    """scripts/production.py beside this file: the call gate and the calendar run are its own."""
    if "production" not in sys.modules:
        spec = importlib.util.spec_from_file_location("production", Path(__file__).with_name("production.py"))
        if spec is None or spec.loader is None:
            raise Refused("production_tool_missing")
        module = importlib.util.module_from_spec(spec)
        sys.modules["production"] = module
        spec.loader.exec_module(module)
    return sys.modules["production"]


class Target:
    """What one rotation works on: the database container, the profile files, the journal's directory."""

    production = False
    retired = None
    project = None

    def __init__(self, journal_dir, profile_dir):
        self.journal_dir, self.profile_dir = Path(journal_dir), Path(profile_dir)
        self.marker, self.journal_path = self.journal_dir / MARKER, self.journal_dir / "journal.json"
        self.staged_root = self.journal_dir / "staged"
        self.container, self.network, self.address, self.image = None, None, None, None
        self.database = "family_calendar"

    def attach(self, container):
        """The database container's identity, network and address; every probe goes through them."""
        info = inspect(container)
        if not info.get("State", {}).get("Running"):
            raise Refused("database_not_running")
        self.container, self.image = info["Id"], info["Image"]
        networks = info.get("NetworkSettings", {}).get("Networks") or {}
        internal = [name for name in networks if self.internal(name)]
        if not internal or not networks[internal[0]].get("IPAddress"):
            raise Refused("database_network_unusable")
        self.network, self.address = internal[0], networks[internal[0]]["IPAddress"]
        return info

    @staticmethod
    def internal(network):
        code, output = docker("network", "inspect", network)
        return code == 0 and json.loads(output)[0].get("Internal") is True

    def files(self, which):
        directory = self.profile_dir
        if which == "owner":
            found = [(directory / "postgres.env", OWNER, "env"), (directory / "runtime.json", OWNER, "json")]
            dsn = self.owner_dsn()
            return found + ([(dsn, OWNER, "dsn")] if dsn else [])
        return [(directory / "gateway.json", "fcs_gateway", "json"), (directory / "worker.json", "fcs_worker", "json")]

    def owner_dsn(self):
        dsn = self.profile_dir / "owner.dsn"
        return dsn if os.path.lexists(dsn) else None

    def sql(self, statements, timeout=DOCKER_TIMEOUT):
        """Over the container's local socket as the owner: the path that needs no password, so operators keep one."""
        return docker("exec", "-i", self.container, "psql", "-X", "-q", "-A", "-t", "-w", "-v", "ON_ERROR_STOP=1",
                      "-U", OWNER, "-d", self.database, "-f", "-", stdin=statements, timeout=timeout)

    def probe(self, role, value):
        """`accepted`, `refused` or `error`: a fresh scram login from a container of its own."""
        token = uuid.uuid4().hex[:12]
        name = "deedkit-calendar-rotate-probe-" + token
        conninfo = (f"host={self.address} port=5432 user={role} dbname={self.database} sslmode=disable "
                    "require_auth=scram-sha-256 connect_timeout=5")
        escaped = value.replace("\\", "\\\\").replace(":", "\\:")
        code, output = docker(
            "run", "--rm", "-i", "--name", name, "--label", "io.deedkit.calendar-rotate-probe=" + token,
            "--pull", "never", "--network", self.network, "--log-driver", "none", "--read-only", "--user", "postgres",
            "--tmpfs", "/tmp:rw,size=1m,mode=1777", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "64m", "--pids-limit", "16", "--entrypoint", "sh", self.image, "-c", PROBE_SCRIPT, "sh", conninfo,
            stdin=f"*:*:*:*:{escaped}\n", timeout=PROBE_TIMEOUT, name=name)
        answer = output.strip()
        return answer if code == 0 and answer in ("accepted", "refused") else "error"

    # Admission: a production rotation overrides these.

    def gate_calls(self):
        """No active call to wait for."""

    def preflight(self):
        """Nothing else stands in the way of a new rotation; asked with the locks held."""

    def identities(self):
        return {}

    def restart(self):
        """Nothing runs on these files."""

    def service_checks(self, journal):
        return {}

    def run_backup(self, journal, token):
        return None

    @contextmanager
    def instant(self):
        yield


class Production(Target):
    production = True

    def __init__(self):
        try:
            cfg = json.loads(read_private(RUNNER_CONFIG))
            self.cfg = cfg
            directory = Path(cfg["directory"])
            super().__init__(directory.parent / "rotation", cfg["state_mounts"]["profile"])
            self.dsn_file = Path(cfg["dsn_file"])
            self.retired = directory.parent / "retired-passwords"
            self.project = cfg["project"]
            self.runner_lock, self.host_lock = directory / ".runner.lock", Path(cfg["host_lock"])
            self.status_path = directory / "status.json"
            self.runner_journal = directory / "journal.json"
        except (OSError, ValueError, KeyError, TypeError):
            raise Refused("runner_config_unreadable") from None

    def owner_dsn(self):
        return self.dsn_file

    def connect(self):
        found = [container for container in service_containers(self.project, "calendar-db")
                 if inspect(container).get("State", {}).get("Running")]
        if len(found) != 1:
            raise Refused("database_container_not_unique")
        return self.attach(found[0])

    def container_of(self, service):
        found = service_containers(self.project, service)
        if len(found) != 1:
            raise Refused("service_container_not_unique", service=service)
        return inspect(found[0])

    def identities(self):
        return {service: self.container_of(service)["Id"] for service in SERVICES}

    def gate_calls(self):
        """production.py's own read of the call database, read-only; doubt counts as a call."""
        try:
            tool = production_tool()
            calls = tool.active_calls(json.loads(read_private(PRODUCTION_CONFIG)))
        except Exception:
            raise Held("call_state_unknown") from None
        if calls:
            raise Held("active_call", count=len(calls))

    def admission_installed(self):
        """A runner or a deploy tool from before this card would not read the marker."""
        for path, word in ((RUNNER_PROGRAM, b"rotation_in_progress"), (INSTALLED_TOOL, b"rotation_running")):
            try:
                if word not in read_private(path):
                    return False
            except OSError:
                return False
        return True

    def deploy_locks(self):
        state = json.loads(read_private(PRODUCTION_CONFIG))["state_directory"]
        return [Path(directory) / "deploy.lock" for directory in dict.fromkeys((str(GLOBAL_LOCK_DIR), state))]

    def preflight(self):
        if not self.admission_installed():
            raise Refused("admission_not_installed")
        if os.path.lexists(f"{self.host_lock}.unsettled") or os.path.lexists(self.runner_journal):
            raise Held("backup_unsettled")

    @contextmanager
    def instant(self):
        """The runner's lock, messaging's lock and the deploy locks, each without waiting: held while this writes."""
        descriptors = []
        try:
            for path, code in ((self.runner_lock, "backup_running"), (self.host_lock, "backup_running"),
                               *((path, "deploy_running") for path in self.deploy_locks())):
                descriptors.append(os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600))
                try:
                    fcntl.flock(descriptors[-1], fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise Held(code) from None
            yield
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def restart(self):
        """Stop and start of the existing containers: no recreate, no build. A bind mount follows the renamed file."""
        self.gate_calls()
        containers = [self.container_of(service)["Id"] for service in ("calendar-worker", "calendar-api")]
        for container in containers:
            if docker("stop", container, timeout=STOP_TIMEOUT)[0] != 0:
                raise Refused("restart_failed")
        for container in reversed(containers):
            if docker("start", container, timeout=STOP_TIMEOUT)[0] != 0:
                raise Refused("restart_failed")

    def healthy(self, api):
        """/health answers 200 from inside the running API, read with its own configuration."""
        deadline = time.monotonic() + HEALTH_WAIT
        while True:
            if docker("exec", api, "python", "-I", "-c", HEALTH_SCRIPT)[0] == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(HEALTH_POLL)

    def service_checks(self, journal):
        checks = {}
        try:
            containers = {service: self.container_of(service) for service in SERVICES}
            current = {service: info["Id"] for service, info in containers.items()}
            checks["identities"] = current == journal["identities"]
            checks["running"] = all(info.get("State", {}).get("Running") for info in containers.values())
            api = containers["calendar-api"]
            checks["health"] = self.healthy(api["Id"])
            tool = production_tool()
            code, answer = tool.calendar_run(f"deedkit-calendar-rotate-check-{journal['id'][-8:]}", api["Image"], self.network,
                                             str(self.profile_dir / "runtime.json"), ["db-roles", "--check"])
            checks["db_roles"] = code == 0 and isinstance(answer, dict) and answer.get("db_roles_check") == "ok"
        except Refused as exc:
            checks[exc.code] = False
        return checks

    def backup_id(self):
        try:
            return json.loads(read_private(self.status_path)).get("id")
        except (OSError, ValueError):
            return None

    def run_backup(self, journal, token):
        """One real calendar backup through the new owner.dsn. The tool's locks are released: the runner takes them."""
        before = self.backup_id()
        process = subprocess.Popen([RUNNER_PYTHON, "-I", str(RUNNER_PROGRAM), "--config", str(RUNNER_CONFIG)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env={**os.environ, TOKEN_ENV: token}, start_new_session=True)
        try:
            code = process.wait(BACKUP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.terminate()  # the runner's handler recovers its worker
            try:
                process.wait(BACKUP_STOP)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise Refused("backup_timeout") from None
        status = None
        with suppress(OSError, ValueError):
            status = json.loads(read_private(self.status_path))
        if code != 0 or not isinstance(status, dict) or status.get("state") != "ok" or not status.get("id") or (
                status.get("id") == before):
            raise Refused("backup_failed")
        return status["id"]


class Isolated(Target):
    """A restore's scratch database: no production path, no service, no production admission."""

    def __init__(self, container, profile_dir, retired):
        # Asked before anything is created: the journal's directory lies beside the profile.
        real = Path(os.path.realpath(profile_dir))
        if not real.is_dir():
            raise Refused("isolated_profile_missing")
        for root in PRODUCTION_ROOTS:
            root = Path(os.path.realpath(root))
            if real == root or root in real.parents or real in root.parents:
                raise Refused("isolated_profile_is_production")
        super().__init__(str(profile_dir).rstrip("/") + ".rotation", profile_dir)
        self.retired = Path(retired) if retired else None
        self.requested = container

    def connect(self):
        info = inspect(self.requested)
        project = info.get("Config", {}).get("Labels", {}).get("com.docker.compose.project")
        if project == PRODUCTION_PROJECT:
            raise Refused("isolated_target_is_production")
        if project and any(service_containers(project, service) for service in ("calendar-api", "calendar-worker")):
            raise Refused("isolated_project_has_services")
        networks = info.get("NetworkSettings", {}).get("Networks") or {}
        if not networks or not all(self.internal(name) for name in networks):
            raise Refused("isolated_network_not_internal")
        return self.attach(self.requested)


# Marker and journal -------------------------------------------------------------------------------------------------


def write_state(path, value):
    durable_write(path, (json.dumps(value, sort_keys=True) + "\n").encode())


def read_state(path, code):
    if not os.path.lexists(path):
        return None
    try:
        value = json.loads(read_private(path))
    except (OSError, ValueError):
        raise Refused(code) from None
    if not isinstance(value, dict):
        raise Refused(code)
    return value


class Rotation:
    """One process's work on a target: its run id owns the marker while it writes."""

    def __init__(self, target):
        self.target, self.run_id, self.journal = target, uuid.uuid4().hex, {}

    def marker(self):
        return read_state(self.target.marker, "marker_unreadable")

    def write_marker(self, **fields):
        write_state(self.target.marker, {"owner": self.run_id, **fields})

    def check_owner(self):
        """Another process took the marker: this one writes nothing more."""
        found = self.marker()
        if not found or found.get("owner") != self.run_id:
            raise Refused("marker_owner_changed")

    def set_phase(self, phase, **fields):
        """The journal first: a marker never says more than the journal, so the runner cannot be admitted early."""
        self.check_owner()
        self.journal.update(phase=phase, **fields)
        write_state(self.target.journal_path, self.journal)
        marker = {"id": self.journal["id"], "phase": phase}
        if phase == "verifying":
            marker["backup_token"] = secrets.token_hex(16)
        self.write_marker(**marker)
        return marker

    def save(self):
        self.check_owner()
        write_state(self.target.journal_path, self.journal)

    def staged_dir(self):
        return self.target.staged_root / self.journal["id"]

    def new_values(self):
        """The staged values, or None when one is lost or is not the value the journal names."""
        try:
            values = json.loads(read_private(self.staged_dir() / "new.json"))
            if isinstance(values, dict) and all(
                    isinstance(values.get(role), str) and fingerprint(values[role]) == fp
                    for role, fp in self.journal["new"].items()):
                return values
        except (OSError, ValueError):
            pass
        return None

    def old_values(self):
        """role -> the replaced value from the copies on disk; a role whose copy is lost is absent."""
        values = {}
        for entry in self.journal["files"]:
            copy = self.staged_dir() / "old" / Path(entry["path"]).name
            with suppress(OSError, Refused, ValueError):
                values.setdefault(entry["role"], read_password(entry["kind"], read_private(copy), entry["role"]))
        return values

    def stage_values(self, values):
        durable_write(self.staged_dir() / "new.json", (json.dumps(values) + "\n").encode())
        self.journal["new"] = {role: fingerprint(value) for role, value in values.items()}

    def send_alter(self, values):
        """One transaction per set, with the verifier computed here. The exit status proves nothing: see settle."""
        statements = ["BEGIN;", "SET LOCAL log_statement = 'none';", "SET LOCAL log_min_error_statement = 'panic';"]
        for role, value in values.items():
            statements.append(f"ALTER ROLE \"{role}\" PASSWORD '{scram_verifier(value)}';")
        statements.append("COMMIT;")
        self.target.sql("\n".join(statements) + "\n", timeout=ALTER_TIMEOUT)

    def settle(self, values, *, resend):
        """Which password the database accepts decides, on fresh connections. `resend` (recover) sends the staged value
        again, the only value that can move forward; the replaced one is never sent."""
        answers = {role: self.target.probe(role, value) for role, value in values.items()}
        if all(answer == "accepted" for answer in answers.values()):
            return
        if not resend:
            raise Refused("alter_uncertain" if "error" in answers.values() else "alter_not_applied")
        self.send_alter(values)
        if any(self.target.probe(role, value) != "accepted" for role, value in values.items()):
            raise Refused("database_unsettled")

    def retire(self):
        """Durable before any file changes; entries are never removed. A repeat adds nothing."""
        path = self.target.retired
        old = self.journal["old"].get(OWNER)
        if path is None or old is None:
            return
        descriptor = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            present = b""
            while chunk := os.read(descriptor, 65536):
                present += chunk
            if old.encode() not in present.split():
                write_all(descriptor, old.encode() + b"\n")
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(path.parent)

    def rewrite_files(self, values):
        """Each file from what it holds now (lossless), same owner and mode. Already right: not written again."""
        for entry in self.journal["files"]:
            path, role, kind = Path(entry["path"]), entry["role"], entry["kind"]
            checkpoint("file:" + path.name)
            current = read_private(path)
            if read_password(kind, current, role) == values[role]:
                continue

            def parses(data, kind=kind, role=role, value=values[role]):
                if read_password(kind, data, role) != value:
                    raise Refused("staged_parse_failed", file=path.name)

            info = os.stat(path)
            durable_write(path, write_password(kind, current, role, values[role]), mode=stat.S_IMODE(info.st_mode),
                          uid=info.st_uid, gid=info.st_gid, check=parses)

    def forward(self, values, *, resend):
        """From a possibly sent ALTER to phase `verifying`. Every step repeats safely."""
        self.settle(values, resend=resend)
        checkpoint("settled")
        self.retire()
        self.set_phase("altered")
        checkpoint("altered")
        self.rewrite_files(values)
        self.set_phase("files")
        checkpoint("files")
        self.target.restart()
        checkpoint("restarted")
        self.set_phase("verifying")
        checkpoint("verifying")


def ignore_signals():
    """The critical section ends in `verifying` or at a refusal, every command in it has a deadline."""
    for number in SIGNALS:
        signal.signal(number, signal.SIG_IGN)


@contextmanager
def tool_lock(target):
    """One process at a time on a journal (the marker outlives a process, this lock does not)."""
    target.journal_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = target.journal_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise Refused("journal_directory_not_private")
    fsync_directory(target.journal_dir.parent)
    LOG[0] = target.journal_dir / "rotation.log"
    descriptor = os.open(target.journal_dir / ".tool.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Held("rotation_tool_running") from None
        yield
    finally:
        os.close(descriptor)


def calls_after(target):
    """The read after a step: forward only, so a call that began during it is reported, not undone."""
    try:
        target.gate_calls()
    except Held as exc:
        say(event="call_gate_after", code=exc.code)


def current_values(target, which):
    """role -> the value its files agree on, plus the file list; files that disagree refuse."""
    entries, values = [], {}
    for path, role, kind in target.files(which):
        try:
            value = read_password(kind, read_private(path), role)
        except OSError:
            raise Refused("profile_file_missing", file=Path(path).name) from None
        if values.setdefault(role, value) != value:
            raise Refused("profile_disagrees", role=role)
        entries.append({"path": str(path), "role": role, "kind": kind})
    return values, entries


def check_retired_list(target):
    """A list that cannot take the prefix refuses now: after the ALTER it could only stop the recovery too."""
    path = target.retired
    if path is None:
        return
    try:
        if not path.parent.is_dir():
            raise Refused("retired_list_unusable")
        if os.path.lexists(path) and not stat.S_ISREG(path.lstat().st_mode):
            raise Refused("retired_list_unusable")
    except OSError:
        raise Refused("retired_list_unusable") from None


def apply(target, which):
    ignore_signals()
    with tool_lock(target):
        rotation = Rotation(target)
        target.connect()
        target.gate_calls()
        check_retired_list(target)
        with target.instant():
            if os.path.lexists(target.marker):
                raise Held("rotation_unsettled", next="recover")
            target.preflight()
            old, entries = current_values(target, which)
            for role, value in old.items():
                if target.probe(role, value) != "accepted":
                    raise Refused("current_value_not_accepted", role=role, fingerprint=fingerprint(value))
            identities = target.identities()
            rotation_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(4)
            rotation.write_marker(id=rotation_id, phase="staged")  # the first byte of `staged`
            checkpoint("marker")
            rotation.journal = {"id": rotation_id, "set": which, "mode": "production" if target.production else "isolated",
                                "phase": "staged", "alter_sent": False, "files": entries, "identities": identities,
                                "old": {role: fingerprint(value) for role, value in old.items()}, "new": {},
                                "verified": False, "backup_id": None, "container": target.container}
            directory = rotation.staged_dir()
            (directory / "old").mkdir(mode=0o700, parents=True, exist_ok=True)
            for entry in entries:
                durable_write(directory / "old" / Path(entry["path"]).name, read_private(entry["path"]))
            fsync_directory(directory)
            fsync_directory(target.staged_root)
            values = {role: secrets.token_urlsafe(40) for role in old}
            rotation.stage_values(values)
            write_state(target.journal_path, rotation.journal)
            checkpoint("staged")
            rotation.check_owner()
            rotation.journal["alter_sent"] = True
            rotation.save()
            checkpoint("before-alter")
            rotation.send_alter(values)
            checkpoint("after-alter")
            rotation.forward(values, resend=False)
        calls_after(target)
        say(status="verifying", id=rotation_id, set=which, old=rotation.journal["old"], new=rotation.journal["new"],
            next="verify")


def abort(rotation, target):
    """Before any ALTER was sent: nothing changed, the new value was never used."""
    with suppress(FileNotFoundError):
        shutil.rmtree(target.staged_root)
    if rotation.journal:
        rotation.journal["phase"] = "aborted"
        write_state(target.journal_path, rotation.journal)
    os.unlink(target.marker)
    fsync_directory(target.journal_dir)
    say(status="aborted", code="nothing_changed")


def finish_cleanup(rotation, target):
    """Old copies and the staged value go, the marker last: `finished` stays readable with fingerprints only."""
    with suppress(FileNotFoundError):
        shutil.rmtree(target.staged_root)
    fsync_directory(target.journal_dir)
    checkpoint("finish-copies")
    with suppress(FileNotFoundError):
        os.unlink(target.marker)
    fsync_directory(target.journal_dir)


def load_journal(target):
    return read_state(target.journal_path, "journal_unreadable")


def recover(target):
    ignore_signals()
    with tool_lock(target):
        rotation = Rotation(target)
        journal = load_journal(target)
        marker = rotation.marker()
        if marker is None:
            say(status="nothing_to_recover", phase=journal.get("phase") if journal else None)
            return
        if journal is not None and journal.get("id") != marker.get("id"):
            journal = None  # killed before this rotation's first journal: the file is an earlier rotation's
        unsent = journal is None or journal.get("phase") in ("aborted", "finished") or (
            journal["phase"] == "staged" and not journal.get("alter_sent"))
        if not unsent:
            target.connect()
        target.gate_calls()
        with target.instant():
            if journal is None or journal["phase"] == "aborted":
                rotation.write_marker(phase="staged")
                rotation.journal = {}
                abort(rotation, target)
                return
            assert journal is not None
            rotation.journal = journal
            if journal["phase"] == "finished":
                rotation.write_marker(id=journal["id"], phase="finished")
                finish_cleanup(rotation, target)
                say(status="finished", id=journal["id"])
                return
            rotation.write_marker(id=journal["id"], phase=journal["phase"])
            if journal["phase"] == "staged" and not journal.get("alter_sent"):
                abort(rotation, target)
                return
            directories = {str(Path(entry["path"]).parent) for entry in journal["files"]}
            sweep_staged(directories)
            values = rotation.new_values()
            if values is None:
                values = {role: secrets.token_urlsafe(40) for role in SETS[journal["set"]]}
                rotation.staged_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
                rotation.stage_values(values)
                rotation.save()
                say(event="staged_value_lost", new=journal["new"])
            identities = target.identities()
            if identities != journal.get("identities"):  # a hand release moved them: named, then the new ones count
                say(event="identities_changed", services=sorted(identities))
            journal.update(verified=False, backup_id=None, identities=identities)
            rotation.save()
            rotation.forward(values, resend=True)
        calls_after(target)
        say(status="verifying", id=journal["id"], set=journal["set"], old=journal["old"], new=journal["new"], next="verify")


def active_journal(target):
    journal = load_journal(target)
    if not os.path.lexists(target.marker) or journal is None:
        raise Refused("no_rotation")
    if journal["phase"] != "verifying":
        raise Refused("not_verifying", phase=journal["phase"], next="recover")
    return journal


def verify(target, backup):
    with tool_lock(target):
        rotation = Rotation(target)
        journal = rotation.journal = active_journal(target)
        target.connect()
        values, old = rotation.new_values(), rotation.old_values()
        if values is None:
            raise Refused("staged_value_lost", next="recover")
        checks = {}
        for role, value in values.items():
            checks["new_accepted:" + role] = target.probe(role, value) == "accepted"
            if role in old:
                checks["old_refused:" + role] = target.probe(role, old[role]) == "refused"
            else:
                say(event="old_copy_lost", role=role)
        for entry in journal["files"]:
            with suppress(OSError, Refused, ValueError):
                checks["file:" + Path(entry["path"]).name] = False
                current = read_password(entry["kind"], read_private(entry["path"]), entry["role"])
                checks["file:" + Path(entry["path"]).name] = current == values[entry["role"]]
        checks.update(target.service_checks(journal))
        failed = sorted(name for name, passed in checks.items() if not passed)
        journal["verified"] = not failed
        write_state(target.journal_path, journal)
        if failed:
            raise Refused("verify_failed", failed=failed, next="recover")
        say(event="verified", new=journal["new"], checks=sorted(checks))
        if target.production and backup:
            token = rotation.marker().get("backup_token")
            journal["backup_id"] = target.run_backup(journal, token)
            write_state(target.journal_path, journal)
        say(status="verified", id=journal["id"], backup_id=journal["backup_id"], next="finish")


def finish(target):
    ignore_signals()
    with tool_lock(target):
        rotation = Rotation(target)
        journal = rotation.journal = active_journal(target)
        if not journal.get("verified"):
            raise Refused("not_verified", next="verify")
        if target.production and not journal.get("backup_id"):
            raise Refused("backup_missing", next="verify")
        target.gate_calls()
        with target.instant():
            found = rotation.marker()
            rotation.write_marker(**{key: value for key, value in found.items() if key != "owner"})
            rotation.check_owner()
            journal["phase"] = "finished"
            rotation.save()
            checkpoint("finish-journal")
            finish_cleanup(rotation, target)
            checkpoint("finished")
        calls_after(target)
        say(status="finished", id=journal["id"], set=journal["set"], old=journal["old"], new=journal["new"],
            backup_id=journal["backup_id"])


def plan(target):
    """Read-only: what a rotation would act on and whether anything stands in the way. Nothing is created."""
    issues, facts = [], {"mode": "production" if target.production else "isolated"}
    if os.path.lexists(target.marker):
        found = read_state(target.marker, "marker_unreadable")
        issues.append("rotation_unsettled")
        facts["phase"] = found.get("phase")
    try:
        target.connect()
        facts["database"] = target.container[:12]
        target.gate_calls()
        if target.production and not target.admission_installed():
            issues.append("admission_not_installed")
        if target.production and (os.path.lexists(f"{target.host_lock}.unsettled") or os.path.lexists(target.runner_journal)):
            issues.append("backup_unsettled")
    except Refused as exc:
        issues.append(exc.code)
    for which in SETS:
        try:
            values, _ = current_values(target, which)
        except Refused as exc:
            issues.append(exc.code)
            continue
        for role, value in values.items():
            answer = target.probe(role, value) if target.container else "error"
            facts[role] = {"fingerprint": fingerprint(value), "database": answer}
            if answer != "accepted":
                issues.append("current_value_not_accepted:" + role)
    say(status="plan", ready=not issues, issues=issues, **facts)
    return 0 if not issues else 1


def undo_note(target):
    journal = load_journal(target) if os.path.lexists(target.journal_path) else None
    say(status="undo_note", phase=journal.get("phase") if journal else None,
        note="undo is another rotation: `apply` the same set again; an exposed value is never restored")
    return 0


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--mode", choices=("production", "isolated"), default="production")
    shared.add_argument("--container")
    shared.add_argument("--profile-dir")
    shared.add_argument("--retired-list")
    commands.add_parser("plan", parents=[shared])
    commands.add_parser("apply", parents=[shared]).add_argument("set", choices=tuple(SETS))
    commands.add_parser("verify", parents=[shared]).add_argument("--no-backup", action="store_true")
    commands.add_parser("finish", parents=[shared])
    commands.add_parser("recover", parents=[shared])
    commands.add_parser("undo-note", parents=[shared])
    return parser


def build_target(args):
    if args.mode == "isolated":
        if not args.container or not args.profile_dir:
            raise Refused("isolated_needs_container_and_profile_dir")
        return Isolated(args.container, args.profile_dir, args.retired_list)
    if args.container or args.profile_dir or args.retired_list:
        raise Refused("production_mode_takes_no_target")
    return Production()


def main(argv=None):
    args = arguments().parse_args(argv)
    os.umask(0o077)
    try:
        if not running_as_root():
            raise Refused("root_required")
        target = build_target(args)
        if args.command == "plan":
            return plan(target)
        if args.command == "undo-note":
            return undo_note(target)
        if args.command == "apply":
            apply(target, args.set)
        elif args.command == "verify":
            verify(target, not args.no_backup)
        elif args.command == "finish":
            finish(target)
        else:
            recover(target)
        return 0
    except Refused as exc:
        say(status=exc.status, code=exc.code, **exc.fields)
        return exc.exit_code
    except OSError as exc:
        # Only the error number: a message could name more than the code is sure of.
        say(status="refused", code="os_error", errno=errno.errorcode.get(exc.errno, "unknown"))
        return 1
    except Exception as exc:
        say(status="refused", code="failed", error=type(exc).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
