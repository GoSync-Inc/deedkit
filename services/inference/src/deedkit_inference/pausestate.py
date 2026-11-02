"""The ledger of process groups the service has stopped, and their recovery (ANYM-691 criterion 5). Stdlib only.

A stopped group never finishes and never reads its stdin, so if the service that stopped it dies the group stays
stopped and keeps its memory. Before every SIGSTOP the service records the group here; `recover` continues and kills
what the ledger names. Ownership is a property of the GROUP: a process belongs to a ledgered group iff its pgid is the
ledgered pgid, it is stopped (state T) and it started no later than the whole second of the ledgered stop time (every
member existed when the group was stopped, and a stopped group cannot spawn; `ps` truncates to seconds, so the cutoff is
floor(stop), never later); the process whose pid is the pgid, when it exists, must be the ledgered leader (same start
time). The ledger is read and written without following links (the engines can write its directory). A stopped helper whose leader died still matches; a pgid
number reused by a process that started after the stop, and every foreign group, never match. Nothing here looks at
"stopped, parent pid 1, same user": the deploy runs `uv sync` as that user.
"""
from __future__ import annotations

from collections.abc import Callable
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

LEDGER_NAME = "paused.json"
VERSION = 1
PS = ["/bin/ps", "-axo", "pid=,pgid=,stat=,lstart="]
START_FORMAT = "%a %b %d %H:%M:%S %Y"
SETTLE_POLLS = 20
SETTLE_SECONDS = 0.1


class LedgerError(Exception):
    """The ledger is unreadable or malformed, or the process table cannot be read."""


def ledger_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / LEDGER_NAME


def process_table(run_ps: Callable[[], str] | None = None) -> list[dict]:
    """[{pid, pgid, stat, start (epoch seconds), started (the lstart text)}] for every process."""
    try:
        text = run_ps() if run_ps else subprocess.run(
            PS, capture_output=True, text=True, timeout=10, check=True, stdin=subprocess.DEVNULL,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise LedgerError(f"ps failed: {error}") from None
    rows = []
    for line in text.splitlines():
        fields = line.split(None, 8)
        if len(fields) < 8:
            if line.strip():
                raise LedgerError("unreadable ps line")
            continue
        started = " ".join(fields[3:8])
        try:
            start = time.mktime(time.strptime(started, START_FORMAT))
            rows.append({"pid": int(fields[0]), "pgid": int(fields[1]), "stat": fields[2], "start": start, "started": started})
        except (ValueError, OverflowError):
            raise LedgerError("unreadable ps line") from None
    return rows


def start_text(pid: int, run_ps: Callable[[], str] | None = None) -> str | None:
    """The `lstart` text of one process, or None when it does not exist."""
    for row in process_table(run_ps):
        if row["pid"] == pid:
            return row["started"]
    return None


class Ledger:
    """`paused.json`: {"version": 1, "instance": id, "groups": [{pgid, leader, leader_start, stopped_at}]}."""

    def __init__(self, path: str | Path, instance: str = "", clock: Callable[[], float] = time.time) -> None:
        self.path, self.instance, self.clock = Path(path), instance, clock

    def groups(self) -> list[dict]:
        try:  # never through a symlink: the service's engines can write this directory, recovery runs unsandboxed
            with os.fdopen(os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW), "r") as handle:
                text = handle.read()
        except FileNotFoundError:
            return []
        except OSError as error:
            raise LedgerError(f"cannot read the ledger: {error}") from None
        try:
            data = json.loads(text)
            groups = data["groups"]
            if data["version"] != VERSION or not isinstance(groups, list):
                raise ValueError("shape")
            for group in groups:
                if (type(group["pgid"]) is not int or type(group["leader"]) is not int or group["pgid"] <= 1
                        or not isinstance(group["leader_start"], str) or isinstance(group["stopped_at"], bool)
                        or not isinstance(group["stopped_at"], (int, float))):
                    raise ValueError("entry")
        except (ValueError, KeyError, TypeError):
            raise LedgerError("the ledger is malformed") from None
        return groups

    def _write(self, groups: list[dict]) -> None:
        """Atomically: a partial file is never visible, and a failed write leaves the previous ledger. The temporary file
        is created exclusively under a random name (a planted symlink or file cannot be written through), mode 0600."""
        partial: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".part", dir=self.path.parent)
            partial = Path(name)
            with os.fdopen(descriptor, "w") as out:
                json.dump({"version": VERSION, "instance": self.instance, "groups": groups}, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(partial, self.path)
        except OSError as error:
            if partial is not None:
                unlink_quietly(partial)
            raise LedgerError(f"cannot write the ledger: {error}") from None

    def record(self, pgid: int, leader: int, leader_start: str, stopped_at: float | None = None) -> None:
        """Write the group BEFORE it is stopped. A failure here means: do not stop it."""
        groups = [g for g in self.groups() if g["pgid"] != pgid]
        groups.append({"pgid": pgid, "leader": leader, "leader_start": leader_start,
                       "stopped_at": self.clock() if stopped_at is None else stopped_at})
        self._write(groups)

    def remove(self, pgid: int) -> None:
        groups = self.groups()
        left = [g for g in groups if g["pgid"] != pgid]
        if len(left) != len(groups):
            self._write(left)

    def clear_if_empty(self) -> None:
        if not self.groups():
            unlink_quietly(self.path)


def unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def members(group: dict, table: list[dict]) -> list[dict]:
    """The processes that belong to a ledgered group (see the module docstring). `lstart` has a resolution of one second
    and truncates, so a member's printed start is at most the whole second of the stop; a process printed in a later
    second is not a member, whatever its pgid. When the process that has the pgid as its pid exists and started at another
    time than the leader did, the number was reused and the whole group is gone (a kernel keeps a pid while its group is
    in use), so nothing matches."""
    leader = next((row for row in table if row["pid"] == group["pgid"]), None)
    if leader is not None and leader["started"] != group["leader_start"]:
        return []
    cutoff = math.floor(group["stopped_at"])
    return [row for row in table if row["pgid"] == group["pgid"] and row["stat"].startswith("T") and row["start"] <= cutoff]


def recover(path: str | Path, run_ps: Callable[[], str] | None = None,
            send: Callable[[int, int], None] = os.kill, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Continue and kill every member of every ledgered group; drop an entry only when no member remains.
    Returns {"groups": n, "killed": [pids], "kept": [pgids]}. LedgerError when the ledger or `ps` cannot be read."""
    ledger = Ledger(path)
    entries = ledger.groups()
    killed: list[int] = []
    kept: list[int] = []
    if not entries:
        ledger.clear_if_empty()
        return {"groups": 0, "killed": killed, "kept": kept}
    table = process_table(run_ps)
    selected: list[dict] = []
    for group in entries:
        for row in members(group, table):
            selected.append(row)
            for number in (signal.SIGCONT, signal.SIGKILL):  # the parent is gone: nobody reads its answer
                try:
                    send(row["pid"], number)
                except (ProcessLookupError, PermissionError):
                    break
            killed.append(row["pid"])
    remaining: list[dict] = entries
    for _ in range(SETTLE_POLLS):  # a killed process takes a moment to leave the table
        table = process_table(run_ps)
        alive = {(row["pid"], row["started"]) for row in table if not row["stat"].startswith("Z")}
        # a selected process that was continued but not killed is no longer stopped: it still counts until it is gone
        survivors = [row for row in selected if (row["pid"], row["started"]) in alive]
        remaining = [g for g in entries if members(g, table) or any(row["pgid"] == g["pgid"] for row in survivors)]
        if not remaining:
            break
        sleep(SETTLE_SECONDS)
    kept = [g["pgid"] for g in remaining]
    if remaining:
        ledger._write(remaining)
    else:
        unlink_quietly(ledger.path)
    return {"groups": len(entries), "killed": killed, "kept": kept}
