"""Yielding (ANYM-691 criteria 1-4 and 6): while Between or the remote tests use the host, a running inference phase is
stopped (SIGSTOP on its process group) and continued (SIGCONT) when the host is free again.

The `Yielder` is a task of its own, apart from the memory supervisor. It reads the busy file through the probe, gates
entry on the CPU the foreign users really burn, and bounds every pause by a per-job budget that protects the client's
deadline. The group stays resident while stopped: this trades RAM-minutes for CPU. Every failure means "unknown", and
unknown means: continue every group (fail open, back to Nice 20 alone). The ledger (pausestate.py) is written before
every stop, so a stopped group never outlives the service that stopped it."""

import asyncio
import contextlib
import json
import logging
import os
import pwd
import signal
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from . import pausestate
from .admission import BUSY_STALE_SECONDS
from .config import reason
from .media import MARKER_NAME

LOG = logging.getLogger("deedkit_inference.yielder")
YIELD_FILE = "yield.json"
MAX_FILE_BYTES = 64 * 1024
#: Whose CPU is "foreign": the Between VM user and the remote-test user (the method of launchd/impact_log.py).
FOREIGN_USERS = ("_betweenci", "_deedkitci")
PS_CPU = ["/bin/ps", "-axo", "uid=,pid=,lstart=,cputime="]
PS_TIMEOUT = 2.0
TICK_TIMEOUT = 8.0  # one whole tick: a ps that hangs, a ledger that blocks
MAX_BACKOFF = 30.0
LOG_EVERY_FAILURE = 30  # a SIGCONT that keeps failing is retried on every tick but logged on the first and every 30th


class YieldSettings(BaseModel):
    """`yield.json` next to config.json. Strict, but never fatal: an invalid file means the defaults."""

    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool = True
    pause_on: list[Literal["between", "deedkit_ci"]] = Field(default_factory=lambda: ["between", "deedkit_ci"])
    pause_min_foreign_cores: Annotated[float, Field(ge=0, le=64)] = 1.0
    resume_after_seconds: Annotated[float, Field(ge=0, le=3600)] = 10.0
    min_remaining_seconds: Annotated[float, Field(ge=0, le=3600)] = 20.0
    max_pause_seconds: Annotated[float, Field(ge=1, le=86400)] = 300.0
    deadline_margin_seconds: Annotated[float, Field(ge=0, le=3600)] = 10.0
    deadline_safety_factor: Annotated[float, Field(ge=1, le=100)] = 3.0


def load_yield_settings(path) -> YieldSettings:
    """The settings, or the defaults when the file is absent, unreadable or invalid (a logged reason, never a value)."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return YieldSettings()
    except OSError:
        LOG.warning("yield: %s ignored: unreadable", YIELD_FILE)
        return YieldSettings()
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > MAX_FILE_BYTES:
            LOG.warning("yield: %s ignored: not_a_plain_file_or_writable_by_others", YIELD_FILE)
            return YieldSettings()
        raw = os.read(descriptor, MAX_FILE_BYTES + 1)
    except OSError:
        LOG.warning("yield: %s ignored: unreadable", YIELD_FILE)
        return YieldSettings()
    finally:
        os.close(descriptor)
    try:
        return YieldSettings.model_validate(json.loads(raw))
    except (ValueError, TypeError) as error:
        LOG.warning("yield: %s ignored: %s", YIELD_FILE, reason(error) if isinstance(error, ValidationError) else "invalid_json")
        return YieldSettings()


# --- the foreign CPU ---------------------------------------------------------------------------------------------
class SampleError(Exception):
    """The process table could not be read or is malformed: the answer is "unknown"."""


def parse_cputime(text):
    """macOS `ps` cputime: `S.cc`, `M:SS.cc`, `H:MM:SS.cc` or `D-HH:MM:SS`; seconds."""
    days = 0
    if "-" in text:
        head, text = text.split("-", 1)
        days = int(head)
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return days * 86400 + seconds


def foreign_uids(users=FOREIGN_USERS):
    found = set()
    for user in users:
        with contextlib.suppress(KeyError):
            found.add(pwd.getpwnam(user).pw_uid)
    found.discard(os.getuid())  # the service's own children are never foreign
    return found


class ForeignCpu:
    """Cores used by the processes of the foreign users over the time between two `sample()` calls, from the
    per-process cputime deltas (identified by pid and start time, so a reused pid is a new process). The service's
    own processes and the 1-minute load are never looked at. The first call after a `reset()` returns None."""

    def __init__(self, uids=None, run_ps=None, clock=time.monotonic):
        self.uids, self.run_ps, self.clock = uids, run_ps, clock
        self.before = None

    def reset(self):
        self.before = None

    def _table(self, uids):
        try:
            text = self.run_ps() if self.run_ps else subprocess.run(
                PS_CPU, capture_output=True, text=True, timeout=PS_TIMEOUT, check=True, stdin=subprocess.DEVNULL,
                env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise SampleError(f"ps failed: {type(error).__name__}") from None
        table = {}
        for line in text.splitlines():
            fields = line.split()
            if not fields:
                continue
            if len(fields) != 8:
                raise SampleError("unreadable ps line")
            try:
                uid, pid = int(fields[0]), int(fields[1])
                cpu = parse_cputime(fields[7])
            except ValueError:
                raise SampleError("unreadable ps line") from None
            if uid in uids:
                table[(pid, " ".join(fields[2:7]))] = cpu
        return table

    def sample(self):
        uids = foreign_uids() if self.uids is None else set(self.uids)
        table, now = self._table(uids), self.clock()
        before, self.before = self.before, (now, table)
        if before is None:
            return None
        then, old = before
        if now <= then:
            return None
        used = sum(max(0.0, cpu - old.get(key, 0.0)) for key, cpu in table.items())  # a new process counts in full
        return used / (now - then)


# --- the pauses ----------------------------------------------------------------------------------------------------
@dataclass
class Candidate:
    running: object
    kind: str  # "child" | "worker"
    proc: asyncio.subprocess.Process
    worker: object | None
    marker: str | None
    allowance: float = 0.0


@dataclass
class Pause:
    job: object
    kind: str
    proc: asyncio.subprocess.Process
    worker: object | None
    marker: str | None
    since: float
    until: float
    flags: set = field(default_factory=set)
    cores: list = field(default_factory=list)
    wasted: float = 0.0
    failures: int = 0  # SIGCONT attempts that failed in a row

    @property
    def pgid(self):
        return self.proc.pid


class Yielder:
    def __init__(self, scheduler, settings, probe, ledger, *, sampler=None, wall=time.time, tick_timeout=TICK_TIMEOUT):
        self.scheduler, self.cfg, self.probe, self.ledger = scheduler, settings, probe, ledger
        self.sampler = sampler or ForeignCpu()
        self.now, self.wall, self.tick_timeout = scheduler.now, wall, tick_timeout
        self.paused = {}  # pgid -> Pause
        self.on = settings.enabled
        self.streak = 0  # consecutive ticks on which the flag and the foreign CPU both held
        self.last_resume = float("-inf")
        self.clear_since = None
        self.last_tick = None
        self.task = None
        # The ledger is read-modify-write: one thread does all of it, in submission order, and the event loop never waits for it.
        self.io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yield-ledger")

    # --- life ------------------------------------------------------------------------------------------------
    def start(self):
        if self.on and self.task is None:
            self.task = asyncio.create_task(self._run())

    async def stop(self):
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.resume_all("stop")
        with contextlib.suppress(Exception):  # let the ledger cleanup finish, but never wait long for a stuck disk
            await asyncio.wait_for(asyncio.to_thread(self.io.shutdown, True), 5)

    def disable(self):
        """Yielding off for good (a drain): every stopped group continues now, and nothing is stopped again."""
        self.on = False
        self.resume_all("drain")

    async def _run(self):
        backoff = 0.0
        try:
            while True:
                await asyncio.sleep(self.scheduler.config.supervise_interval_seconds)
                try:
                    await asyncio.wait_for(self._tick(), self.tick_timeout)
                    backoff = 0.0
                except Exception as error:  # noqa: BLE001 - anything unknown: continue every group, then retry later
                    self.resume_all("error")
                    self.streak, self.clear_since, self.last_tick = 0, None, None
                    self.sampler.reset()
                    LOG.error("yield: tick failed: %s", type(error).__name__)
                    backoff = min(max(backoff * 2, 1.0), MAX_BACKOFF)
                    await asyncio.sleep(backoff)
        finally:
            self.resume_all("stop")

    # --- one tick --------------------------------------------------------------------------------------------
    def _flags(self):
        busy = self.probe.read_busy()
        if busy.problem is not None or abs(self.wall() - busy.ts) > BUSY_STALE_SECONDS:
            return None  # missing, invalid or stale: not "free", just unknown
        return sorted(flag for flag in self.cfg.pause_on if getattr(busy, flag))

    async def _tick(self):
        now = self.now()
        seconds = 0.0 if self.last_tick is None else min(now - self.last_tick, 5 * self.scheduler.config.supervise_interval_seconds)
        self.last_tick = now
        if not self.on:
            self.resume_all("off")
            return
        flags = self._flags()
        cores = None
        if flags or self.paused:
            cores = await asyncio.to_thread(self.sampler.sample)
        else:
            self.sampler.reset()
        if not self.on:
            return
        gate = bool(flags) and cores is not None and cores >= self.cfg.pause_min_foreign_cores
        self._account(seconds, flags, gate)
        self._maintain(now, seconds, flags, cores)
        self.streak = self.streak + 1 if gate else 0
        if self.streak >= 2 and now - self.last_resume >= self.cfg.resume_after_seconds:
            await self._enter(now, flags)

    def _running(self):
        return [slot.running for slot in self.scheduler.slots.values() if slot.running is not None]

    def _account(self, seconds, flags, gate):
        """Jobs that ran under a busy flag; the time they could not be paused is `excluded` (the log line says so)."""
        if not flags:
            return
        now = self.now()
        for running in self._running():
            job = running.job
            job.busy_seen = True
            if job.paused_since is not None:
                continue
            if not gate or isinstance(self._assess(running, now), str):
                job.excluded += seconds

    def _maintain(self, now, seconds, flags, cores):
        """The pauses now open: forget the ended, continue the ones that must go on."""
        if not self.paused:
            self.clear_since = None
            return
        if flags is None:
            self.resume_all("stale")  # missing, invalid, stale: fail open
            return
        self.clear_since = None if flags else (now if self.clear_since is None else self.clear_since)
        free = self.clear_since is not None and now - self.clear_since >= self.cfg.resume_after_seconds
        for pause in list(self.paused.values()):
            pause.flags.update(flags)
            if cores is not None:
                pause.cores.append(cores)
                if cores < self.cfg.pause_min_foreign_cores:
                    pause.wasted += seconds
            if pause.failures:
                self._resume(pause, "retry")
            elif self._ended(pause):
                self._resume(pause, "ended")  # a live worker whose call was answered just before the stop stays stopped
            elif pause.marker is not None and os.path.exists(pause.marker):
                self._resume(pause, "decode")  # a decode helper started after the stop: undone within one tick
            elif now >= pause.until:
                pause.job.budget_exhausted = pause.job.no_pause = True
                self._resume(pause, "budget")
            elif free:
                self._resume(pause, "clear")

    def _ended(self, pause):
        job, running = pause.job, None
        for slot in self.scheduler.slots.values():
            if slot.running is not None and slot.running.job is job:
                running = slot.running
        if running is None or pause.proc.returncode is not None:
            return True
        if pause.worker is not None:
            return pause.worker.job is not job or not pause.worker.alive()
        return job.proc is not pause.proc

    # --- who may be stopped -------------------------------------------------------------------------------------
    def _assess(self, running, now):
        """A Candidate, or the reason (a string) this phase is not stoppable now."""
        job = running.job
        if running.later:
            return "non_last"
        if job.no_pause:
            return "budget"
        if job.engine.worker:
            worker = job.engine.workers.get(running.key[1])
            if worker is None or worker.job is not job or not worker.alive():
                return "not_executing"  # still loading or being prepared: before ready() and the job message
            proc, marker = worker.proc, os.path.join(worker.scratch, MARKER_NAME)
        else:
            worker, marker, proc = None, None, job.proc
            if proc is None or proc.returncode is not None:
                return "not_executing"
        if marker is not None and os.path.exists(marker):
            return "decoding"
        left = max(0.0, running.est - (now - running.started - self.scheduler.paused_seconds(job)))
        if left < self.cfg.min_remaining_seconds:
            return "short"
        allowance = self.cfg.max_pause_seconds - job.paused
        if job.deadline is not None:
            slack = (job.deadline - (now - job.accepted)
                     - max(self.cfg.deadline_margin_seconds, self.cfg.deadline_safety_factor * left) - left)
            allowance = min(allowance, slack)
        if allowance <= 0:
            job.no_pause = True  # the deadline cannot absorb a pause (or the cap is used): never paused
            return "budget"
        return Candidate(running, "worker" if worker is not None else "child", proc, worker, marker, allowance)

    def _record(self, candidate):
        """Ledger thread: write the group BEFORE the stop. False when the leader is gone; a LedgerError fails the tick."""
        started = pausestate.start_text(candidate.proc.pid)
        if started is None:
            return False
        self.ledger.record(candidate.proc.pid, candidate.proc.pid, started, stopped_at=self.wall())
        return True

    def _snapshot(self, pgid):
        """Ledger thread, after the stop: the exact members of the frozen group (pid and start time) go into the entry.
        The recorded stop time is that of the ledger write, a little before the freeze, and recovery only owns members
        that started no later than it; a helper spawned in between is named here by identity instead, so a crash after
        this write strands nothing (a crash inside the milliseconds before it is the one window that remains)."""
        try:
            rows = [{"pid": row["pid"], "started": row["started"]} for row in pausestate.process_table()
                    if row["pgid"] == pgid and row["stat"].startswith("T")]
            if rows:
                self.ledger.set_members(pgid, rows)
        except Exception as error:  # noqa: BLE001 - the entry written before the stop still stands
            LOG.error("yield: group members not recorded: %s", type(error).__name__)

    async def _enter(self, now, flags):
        for running in self._running():
            job = running.job
            if job.paused_since is not None:
                continue
            candidate = self._assess(running, now)
            if isinstance(candidate, str):
                continue
            if not await asyncio.wrap_future(self.io.submit(self._record, candidate)):
                continue
            self._stop(candidate, flags)

    def _stop(self, candidate, flags):
        """One synchronous step: look again, stop the group, register it. Nothing awaits in between, so a
        cancellation can never leave a stopped group that `resume_all` does not know."""
        running, proc = candidate.running, candidate.proc
        job = running.job
        now = self.now()
        current = any(slot.running is running for slot in self.scheduler.slots.values())
        if (not self.on or not current or proc.returncode is not None or job.paused_since is not None
                or (candidate.worker is not None and (candidate.worker.job is not job or not candidate.worker.alive()))
                or (candidate.worker is None and job.proc is not proc)):
            self._unrecord(proc.pid)
            return
        pause = Pause(job, candidate.kind, proc, candidate.worker, candidate.marker, now,
                      now + min(candidate.allowance, self.cfg.max_pause_seconds - job.paused), set(flags or ()))
        self.paused[pause.pgid] = pause
        job.paused_since = now
        if candidate.worker is not None:
            candidate.worker.paused = True
        self.scheduler.hold_timeout(job)
        try:
            os.killpg(pause.pgid, signal.SIGSTOP)
        except (ProcessLookupError, PermissionError):
            self._resume(pause, "gone")
            return
        if pause.marker is not None and os.path.exists(pause.marker):
            self._resume(pause, "decode")  # the marker appeared before the stop landed: the decoder keeps its deadline
            return
        with contextlib.suppress(RuntimeError):
            self.io.submit(self._snapshot, pause.pgid)

    # --- continuing --------------------------------------------------------------------------------------------
    def _remove(self, pgid):
        try:
            self.ledger.remove(pgid)
            self.ledger.clear_if_empty()
        except Exception as error:  # noqa: BLE001 - a stale entry is harmless: recovery only touches stopped members
            LOG.error("yield: ledger entry not removed: %s", type(error).__name__)

    def _unrecord(self, pgid):
        """Queue the removal on the ledger thread: after a continue it is only bookkeeping, and a later record of the
        same group queues behind it."""
        with contextlib.suppress(RuntimeError):  # the executor is shut down: the process is going away
            self.io.submit(self._remove, pgid)

    def _resume(self, pause, why):
        """Continue the group, then close the books. True when closed. A SIGCONT that fails keeps the pause tracked and
        is retried on every tick for as long as the group exists: a stopped group is never forgotten."""
        LOG.debug("yield: continue reason=%s", why)
        if pause.proc.returncode is None:
            try:
                os.killpg(pause.pgid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            except OSError as error:
                pause.failures += 1
                if pause.failures % LOG_EVERY_FAILURE == 1:
                    LOG.error("yield: continue failed (%d in a row): %s", pause.failures, type(error).__name__)
                return False
        self._close(pause)
        return True

    def _close(self, pause):
        self.paused.pop(pause.pgid, None)
        job, now = pause.job, self.now()
        spent = max(0.0, now - pause.since)
        job.paused += spent
        job.paused_since = None
        self.last_resume = now
        if pause.worker is not None:
            pause.worker.paused = False
        try:
            self.scheduler.release_timeout(job)
        except Exception:  # noqa: BLE001
            LOG.error("yield: phase timeout not restored")
        self._unrecord(pause.pgid)
        mean = sum(pause.cores) / len(pause.cores) if pause.cores else 0.0
        LOG.info("yield: job=%s paused=%.1f flags=%s foreign_cores_mean=%.2f wasted=%.1f budget_exhausted=%d",
                 job.log_id, spent, ",".join(sorted(pause.flags)), mean, pause.wasted, int(job.budget_exhausted))

    def resume_all(self, why):
        for pause in list(self.paused.values()):
            try:
                self._resume(pause, why)
            except Exception as error:  # noqa: BLE001 - one group's failure never keeps another stopped
                LOG.error("yield: resume failed: %s", type(error).__name__)
