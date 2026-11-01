"""The bounded queue, the two slots (gpu, cpu), phases, deadlines and the engine children.

One asyncio loop owns all state; every method here is called on it, so no lock is needed. A job is queued
(and, when its slot is free and admission lets it, granted the slot) in one synchronous step inside `admit`, so
the queue bound and the slot are decided atomically."""

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections import deque
from dataclasses import dataclass, field

from .engines import BadOutput, PhaseContext
from .errors import ServiceError

LOG = logging.getLogger("deedkit_inference.scheduler")
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


@dataclass
class Job:
    id: str
    principal_id: str
    alias: str
    spec: object  # AliasSpec
    engine: object
    input: object  # JobInput
    estimate: float
    deadline: float | None  # seconds from the arrival of the request
    accepted: float  # monotonic time the request arrived: the deadline clock starts here
    result: object = None
    proc: asyncio.subprocess.Process | None = None
    kill_reason: str | None = None


@dataclass
class Waiter:
    job: Job
    index: int
    phase: object
    fut: asyncio.Future
    est: float
    expires: float  # monotonic time after which this wait is over, whatever the slot does
    granted: bool = False


@dataclass
class Running:
    job: Job
    index: int
    key: tuple
    started: float
    est: float


@dataclass
class Slot:
    name: str
    running: Running | None = None
    queue: deque = field(default_factory=deque)


def kill_group(pgid):
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def slot_busy(retry_after, message="the job waited past its deadline in the queue"):
    return ServiceError(503, "slot_busy", message, retry_after=retry_after)


class Scheduler:
    def __init__(self, config, admission, probe, monotonic=time.monotonic):
        self.config, self.admission, self.probe, self.now = config, admission, probe, monotonic
        self.slots = {"gpu": Slot("gpu"), "cpu": Slot("cpu")}
        self.draining = False
        self.tasks = set()
        self._background = []

    # --- life ------------------------------------------------------------------------------------------
    def start(self):
        self._background = [
            asyncio.create_task(self._ticker()),
            asyncio.create_task(self._supervisor()),
        ]

    async def stop(self):
        for task in self._background:
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        self.kill_all()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def kill_all(self):
        for slot in self.slots.values():
            if slot.running and slot.running.job.proc:
                kill_group(slot.running.job.proc.pid)
        for task in list(self.tasks):
            task.cancel()

    def idle(self):
        return not self.tasks

    # --- numbers for /health and the admission of a new job -----------------------------------------------
    def depth(self):
        return sum(len(slot.queue) for slot in self.slots.values())

    def active(self):
        return sum(1 for slot in self.slots.values() if slot.running)

    def transit(self):
        """Running jobs that have a later phase: each will queue again for its next slot."""
        return sum(
            1 for s in self.slots.values() if s.running and s.running.index < len(s.running.job.spec.phases) - 1
        )

    def estimated_wait(self, slot_name):
        slot = self.slots[slot_name]
        total = sum(w.est for w in slot.queue)
        if slot.running:
            total += max(0.0, slot.running.est - (self.now() - slot.running.started))
        return total

    def children(self):
        return [s.running.job for s in self.slots.values() if s.running and s.running.job.proc]

    # --- accepting a job ----------------------------------------------------------------------------------
    def phase_estimate(self, job, phase):
        return job.estimate * phase.share

    def admit(self, job, principal, quota):
        """The arrival checks, in order, then the queue step. Raises ServiceError; otherwise the job is counted
        and its task runs."""
        cfg = self.config
        first = job.spec.phases[0]
        if self.draining:
            raise ServiceError(503, "draining", "the service is shutting down", retry_after=cfg.retry_after_seconds)
        key = (job.alias, first.name)
        anything = any(s.running for s in self.slots.values())
        verdict = self.admission.check(key, first.resident_gb, first.slot, arrival=True, anything_running=anything)
        if verdict.action == "refuse":
            raise self.closed(verdict.reason)
        wait = self.estimated_wait(first.slot)
        budget = None if job.deadline is None else job.deadline - (self.now() - job.accepted)
        if budget is not None and job.estimate > budget:
            raise ServiceError(422, "deadline_too_short",
                               "the job alone takes longer than what is left of the stated deadline")
        if wait > cfg.max_wait_seconds or (budget is not None and wait + job.estimate > budget):
            raise slot_busy(min(max(wait, 1.0), cfg.max_wait_seconds),
                            "the slot is busy for longer than this request can wait")
        if quota.daily_exhausted(principal):
            raise ServiceError(429, "quota_exceeded", "the daily job limit is reached",
                               retry_after=self.until_utc_midnight())
        # One synchronous step: queue the job, let the pump grant a free slot, then bound the queue.
        slot = self.slots[first.slot]
        waiter = self._waiter(job, 0, first)
        slot.queue.append(waiter)
        self._pump(slot)
        if waiter.fut.done():
            error = waiter.fut.exception()
            if error is not None:
                raise error
        if self._over_bound(waiter, len(job.spec.phases) > 1):
            self._abandon(slot, waiter)
            raise ServiceError(503, "queue_full", "the queue is full", retry_after=cfg.retry_after_seconds)
        quota.accept(principal)
        task = asyncio.create_task(self._run(job, waiter))
        self.tasks.add(task)
        task.add_done_callback(lambda done: self._finished(done, job))
        return task

    def _over_bound(self, waiter, later_phases):
        """The queue bound counts the jobs that wait and the running ones that will wait again for a later phase,
        so a phase transition can never push the queue past `queue_size`. A new job only counts if it waits
        itself or will wait again."""
        if waiter.granted and not later_phases:
            return False
        return self.depth() + self.transit() > self.config.queue_size

    def closed(self, reason):
        return ServiceError(503, "admission_closed", f"admission is closed: {reason}",
                            retry_after=self.config.retry_after_seconds, headers={"X-Admission-Reason": reason})

    @staticmethod
    def until_utc_midnight():
        return 86400 - (time.time() % 86400)

    def _finished(self, task, job):
        """Safety net: whatever way the task ended (even cancelled before its first step), the job holds nothing."""
        self.tasks.discard(task)
        for slot in self.slots.values():
            for waiter in [w for w in slot.queue if w.job is job]:
                slot.queue.remove(waiter)
            if slot.running and slot.running.job is job:
                self.admission.mark_idle(slot.running.key)
                slot.running = None
        self._pump_all()

    # --- running a job -------------------------------------------------------------------------------------
    async def _run(self, job, first_waiter):
        for index, phase in enumerate(job.spec.phases):
            await self._enter(job, index, phase, first_waiter if index == 0 else None)
            try:
                await self._execute(job, index, phase)
            except BaseException:
                # The child group is dead (crash, timeout, kill, cancel): its memory is back, drop the book entry.
                self.admission.drop((job.alias, phase.name))
                raise
            finally:
                self._leave(job, phase)
        return job.result

    def _waiter(self, job, index, phase):
        remaining = job.estimate * sum(p.share for p in job.spec.phases[index:])
        limit = self.config.max_wait_seconds
        if job.deadline is not None:
            limit = min(limit, job.deadline - (self.now() - job.accepted) - remaining)
        future = asyncio.get_running_loop().create_future()
        return Waiter(job, index, phase, future, self.phase_estimate(job, phase), self.now() + limit)

    async def _enter(self, job, index, phase, waiter):
        slot = self.slots[phase.slot]
        if waiter is None:
            waiter = self._waiter(job, index, phase)
            slot.queue.append(waiter)
            self._pump(slot)
        try:
            if waiter.fut.done():
                waiter.fut.result()
            else:
                await asyncio.wait_for(waiter.fut, max(waiter.expires - self.now(), 0.0))
        except TimeoutError:
            self._abandon(slot, waiter)
            raise slot_busy(self.config.retry_after_seconds) from None
        except BaseException:
            self._abandon(slot, waiter)
            raise

    def _abandon(self, slot, waiter):
        if waiter.granted:
            self._release(slot, waiter)
        else:
            self._drop(slot, waiter)

    def _drop(self, slot, waiter):
        with contextlib.suppress(ValueError):
            slot.queue.remove(waiter)
        self._pump(slot)

    def _release(self, slot, waiter):
        if slot.running and slot.running.job is waiter.job:
            self.admission.mark_idle(slot.running.key)
            slot.running = None
        self._pump_all()

    def _leave(self, job, phase):
        slot = self.slots[phase.slot]
        if slot.running and slot.running.job is job:
            self.admission.mark_idle(slot.running.key)
            slot.running = None
        self._pump_all()

    def _pump_all(self):
        for slot in self.slots.values():
            self._pump(slot)

    def _pump(self, slot):
        """Start the head of the slot's queue when the slot is free and admission lets it through. A wait whose
        time is over gets no slot."""
        while slot.running is None and slot.queue:
            waiter = slot.queue[0]
            if waiter.fut.done():
                slot.queue.popleft()
                continue
            if self.now() > waiter.expires:
                slot.queue.popleft()
                waiter.fut.set_exception(slot_busy(self.config.retry_after_seconds))
                continue
            phase, job = waiter.phase, waiter.job
            key = (job.alias, phase.name)
            verdict = self.admission.check(key, phase.resident_gb, slot.name)
            if verdict.action == "evict":
                self.admission.evict(verdict.evict)
                continue
            if verdict.action == "wait":
                return
            if self.now() > waiter.expires:  # the probes above may have taken the time that was left
                slot.queue.popleft()
                waiter.fut.set_exception(slot_busy(self.config.retry_after_seconds))
                continue
            slot.queue.popleft()
            if verdict.action == "refuse":
                waiter.fut.set_exception(self.closed(verdict.reason))
                continue
            slot.running = Running(job, waiter.index, key, self.now(), waiter.est)
            self.admission.mark_running(key, phase.resident_gb, job.engine, phase.name)
            waiter.granted = True
            waiter.fut.set_result(None)

    async def _execute(self, job, index, phase):
        ctx = PhaseContext(index, len(job.spec.phases), phase.name, phase.slot, job.input)
        try:
            argv = job.engine.argv(ctx)
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, cwd=job.input.directory, env=job.engine.env(),
                start_new_session=True,
            )
        except OSError:
            raise ServiceError(502, "engine_failed", "the engine could not start") from None
        job.proc = proc
        reader = asyncio.create_task(self._read_capped(proc))
        try:
            await asyncio.wait_for(proc.wait(), job.spec.timeout_seconds)
            # The child is gone: whatever it left behind in its group goes too, so no orphan keeps the pipe open.
            kill_group(proc.pid)
            stdout = await asyncio.wait_for(reader, 5)
        except TimeoutError:
            raise ServiceError(504, "engine_timeout", "the engine did not answer in time") from None
        finally:
            kill_group(proc.pid)
            reader.cancel()
            with contextlib.suppress(Exception):
                await asyncio.shield(proc.wait())
            job.proc = None
        if job.kill_reason or proc.returncode != 0 or stdout is None:
            raise ServiceError(502, "engine_failed", "the engine failed")
        try:
            result = job.engine.parse(ctx, stdout)
        except BadOutput:
            raise ServiceError(502, "engine_failed", "the engine returned an invalid answer") from None
        if ctx.last:
            job.result = result

    async def _read_capped(self, proc):
        """The child's whole stdout, or None when it prints more than MAX_OUTPUT_BYTES (its group is killed)."""
        data = bytearray()
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                return bytes(data)
            data += chunk
            if len(data) > MAX_OUTPUT_BYTES:
                kill_group(proc.pid)
                return None

    # --- housekeeping ------------------------------------------------------------------------------------------
    async def _ticker(self):
        while True:
            await asyncio.sleep(self.config.pump_interval_seconds)
            expired = self.admission.expired_idle()
            if expired:
                self.admission.evict(expired)
            self._pump_all()

    async def _supervisor(self):
        """When the children together exceed the hard cap, the largest one loses its process group."""
        while True:
            await asyncio.sleep(self.config.supervise_interval_seconds)
            sizes = []
            for job in self.children():
                proc = job.proc
                if proc is not None:
                    sizes.append((await asyncio.to_thread(self.probe.rss_gb, proc.pid), job))
            if sizes and sum(size for size, _ in sizes) > self.config.resident_hard_cap_gb:
                _, victim = max(sizes, key=lambda item: item[0])
                victim.kill_reason = "memory_hard_cap"
                if victim.proc is not None:
                    kill_group(victim.proc.pid)
