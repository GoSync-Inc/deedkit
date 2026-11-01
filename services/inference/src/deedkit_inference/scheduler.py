"""The bounded queue, the two slots (gpu, cpu), phases, deadlines and the engine children.

One asyncio loop owns all state; every method here is called on it, so no lock is needed."""

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
    deadline: float | None
    accepted: float
    result: object = None
    proc: object = None
    kill_reason: str | None = None


@dataclass
class Waiter:
    job: Job
    index: int
    phase: object
    fut: asyncio.Future
    est: float
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

    def admit(self, job, quota_principal, quota):
        """The arrival checks, in order. Raises ServiceError; otherwise the job is counted and started."""
        cfg = self.config
        first = job.spec.phases[0]
        if self.draining:
            raise ServiceError(503, "draining", "the service is shutting down", retry_after=cfg.retry_after_seconds)
        slot = self.slots[first.slot]
        if (slot.running or slot.queue) and self.depth() >= cfg.queue_size:
            raise ServiceError(503, "queue_full", "the queue is full", retry_after=cfg.retry_after_seconds)
        key = (job.alias, first.name)
        anything = any(s.running for s in self.slots.values())
        verdict = self.admission.check(key, first.resident_gb, first.slot, arrival=True, anything_running=anything)
        if verdict.action == "refuse":
            raise self.closed(verdict.reason)
        wait = self.estimated_wait(first.slot)
        if job.deadline is not None and job.estimate > job.deadline:
            raise ServiceError(422, "deadline_too_short", "the job alone takes longer than the stated deadline")
        too_long = wait > cfg.max_wait_seconds
        past_deadline = job.deadline is not None and wait + job.estimate > job.deadline
        if too_long or past_deadline:
            raise ServiceError(503, "slot_busy", "the slot is busy for longer than this request can wait",
                               retry_after=min(max(wait, 1.0), cfg.max_wait_seconds))
        if not quota.accept(quota_principal):
            raise ServiceError(429, "quota_exceeded", "the daily job limit is reached", retry_after=self.until_utc_midnight())
        task = asyncio.create_task(self._run(job))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def closed(self, reason):
        return ServiceError(503, "admission_closed", f"admission is closed: {reason}",
                            retry_after=self.config.retry_after_seconds, headers={"X-Admission-Reason": reason})

    @staticmethod
    def until_utc_midnight():
        now = time.time()
        return 86400 - (now % 86400)

    # --- running a job -------------------------------------------------------------------------------------
    async def _run(self, job):
        for index, phase in enumerate(job.spec.phases):
            await self._enter(job, index, phase)
            try:
                await self._execute(job, index, phase)
            except BaseException:
                # The child group is dead (crash, timeout, kill, cancel): its memory is back, drop the book entry.
                self.admission.evict([(job.alias, phase.name)])
                raise
            finally:
                self._leave(job, index, phase)
        return job.result

    async def _enter(self, job, index, phase):
        slot = self.slots[phase.slot]
        remaining = job.estimate * sum(p.share for p in job.spec.phases[index:])
        limit = self.config.max_wait_seconds
        if job.deadline is not None:
            limit = min(limit, job.deadline - (self.now() - job.accepted) - remaining)
        waiter = Waiter(job, index, phase, asyncio.get_running_loop().create_future(), self.phase_estimate(job, phase))
        slot.queue.append(waiter)
        self._pump(slot)
        try:
            if waiter.fut.done():
                waiter.fut.result()
            else:
                await asyncio.wait_for(waiter.fut, max(limit, 0.0))
        except TimeoutError:
            if waiter.granted:
                self._release(slot, waiter)
            else:
                self._drop(slot, waiter)
            raise ServiceError(503, "slot_busy", "the job waited past its deadline in the queue",
                               retry_after=self.config.retry_after_seconds) from None
        except BaseException:
            if waiter.granted:
                self._release(slot, waiter)
            else:
                self._drop(slot, waiter)
            raise

    def _drop(self, slot, waiter):
        with contextlib.suppress(ValueError):
            slot.queue.remove(waiter)
        self._pump(slot)

    def _release(self, slot, waiter):
        if slot.running and slot.running.job is waiter.job:
            self.admission.mark_idle(slot.running.key)
            slot.running = None
        self._pump_all()

    def _leave(self, job, index, phase):
        slot = self.slots[phase.slot]
        if slot.running and slot.running.job is job:
            self.admission.mark_idle(slot.running.key)
            slot.running = None
        self._pump_all()

    def _pump_all(self):
        for slot in self.slots.values():
            self._pump(slot)

    def _pump(self, slot):
        """Start the head of the slot's queue when the slot is free and admission lets it through."""
        while slot.running is None and slot.queue:
            waiter = slot.queue[0]
            if waiter.fut.done():
                slot.queue.popleft()
                continue
            phase, job = waiter.phase, waiter.job
            key = (job.alias, phase.name)
            verdict = self.admission.check(key, phase.resident_gb, slot.name)
            if verdict.action == "evict":
                self.admission.evict(verdict.evict)
                continue
            if verdict.action == "wait":
                return
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
        try:
            stdout = await asyncio.wait_for(self._collect(proc), job.spec.timeout_seconds)
        except TimeoutError:
            raise ServiceError(504, "engine_timeout", "the engine did not answer in time") from None
        finally:
            kill_group(proc.pid)
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

    async def _collect(self, proc):
        data = bytearray()
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_OUTPUT_BYTES:
                kill_group(proc.pid)
                await proc.wait()
                return None
        await proc.wait()
        return bytes(data)

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
