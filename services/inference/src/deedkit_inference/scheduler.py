"""The bounded queue, the two slots (gpu, cpu), phases, deadlines and the engine children.

One asyncio loop owns all state; every method here is called on it, so no lock is needed. A job is queued
(and, when its slot is free and admission lets it, granted the slot) in one synchronous step inside `admit`, so
the queue bound and the slot are decided atomically."""

import asyncio
import contextlib
import json
import logging
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .engines import BadOutput, PhaseContext
from .errors import ServiceError
from .workers import Worker, WorkerFailed, kill_group

LOG = logging.getLogger("deedkit_inference.scheduler")
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
#: A warm worker over this multiple of its phase's resident_gb loses its process group.
MEMORY_SHARE_FACTOR = 1.5


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
    state: dict = field(default_factory=dict)  # what finished phases left for the next ones ("done", the language)

    def remaining(self):
        return self.engine.plan(self.spec, self.input, self.state)


@dataclass
class Waiter:
    job: Job
    index: int
    phase: object
    fut: asyncio.Future
    est: float
    expires: float  # monotonic time after which this wait is over, whatever the slot does
    granted: bool = False
    later: bool = False  # the job has another phase after this one


@dataclass
class Running:
    job: Job
    index: int
    key: tuple
    started: float
    est: float
    later: bool = False
    warm: bool = False  # admitted on a loaded worker's memory (the reserve only); fixed at the grant


@dataclass
class Slot:
    name: str
    running: Running | None = None
    queue: deque = field(default_factory=deque)


def slot_busy(retry_after, message="the job waited past its deadline in the queue"):
    return ServiceError(503, "slot_busy", message, retry_after=retry_after)


class Scheduler:
    def __init__(self, config, admission, probe, monotonic=time.monotonic):
        self.config, self.admission, self.probe, self.now = config, admission, probe, monotonic
        self.slots = {"gpu": Slot("gpu"), "cpu": Slot("cpu")}
        self.draining = False
        self.tasks = set()
        self.engines = set()  # worker engines that have been given a job: their workers are watched and killed
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
        for engine in self.engines:
            engine.stop_workers()
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
        return sum(1 for s in self.slots.values() if s.running and s.running.later)

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
        remaining = job.remaining()
        first = remaining[0]
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
        waiter = self._waiter(job, 0, first, remaining)
        slot.queue.append(waiter)
        self._pump(slot)
        if waiter.fut.done():
            error = waiter.fut.exception()
            if error is not None:
                raise error
        if self._over_bound(waiter, len(remaining) > 1):
            self._abandon(slot, waiter)
            raise ServiceError(503, "queue_full", "the queue is full", retry_after=cfg.retry_after_seconds)
        quota.accept(principal)
        if job.engine.worker:
            self.engines.add(job.engine)
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
        """The phases the engine plans, one after the other; the plan is asked again after each, because what a
        phase found (the language of a voice note) can change which phase comes next."""
        done = job.state.setdefault("done", [])
        while remaining := job.remaining():
            index, phase = len(done), remaining[0]
            await self._enter(job, index, phase, remaining, first_waiter if index == 0 else None)
            try:
                await self._execute(job, index, phase, len(remaining))
            except BaseException:
                # The child group is dead (crash, timeout, kill, cancel): its memory is back, drop the book entry.
                self.admission.drop((job.alias, phase.name))
                raise
            finally:
                self._leave(job, phase)
            done.append(phase.name)
        return job.result

    def _waiter(self, job, index, phase, remaining):
        left = job.estimate * sum(p.share for p in remaining)
        limit = self.config.max_wait_seconds
        if job.deadline is not None:
            limit = min(limit, job.deadline - (self.now() - job.accepted) - left)
        future = asyncio.get_running_loop().create_future()
        return Waiter(job, index, phase, future, self.phase_estimate(job, phase), self.now() + limit,
                      later=len(remaining) > 1)

    async def _enter(self, job, index, phase, remaining, waiter):
        slot = self.slots[phase.slot]
        if waiter is None:
            waiter = self._waiter(job, index, phase, remaining)
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
            if verdict.action == "go" and any(engine.retiring_workers() for engine in self.engines):
                return  # an evicted or killed worker is not reaped yet: its memory is still in use
            if self.now() > waiter.expires:  # the probes above may have taken the time that was left
                slot.queue.popleft()
                waiter.fut.set_exception(slot_busy(self.config.retry_after_seconds))
                continue
            slot.queue.popleft()
            if verdict.action == "refuse":
                waiter.fut.set_exception(self.closed(verdict.reason))
                continue
            slot.running = Running(job, waiter.index, key, self.now(), waiter.est, waiter.later,
                                   warm=self.admission.warm(key))
            self.admission.mark_running(key, phase.resident_gb, job.engine, phase.name)
            waiter.granted = True
            waiter.fut.set_result(None)

    async def _execute(self, job, index, phase, left):
        ctx = PhaseContext(index, index + left, phase.name, phase.slot, job.input, job.state)
        stdout = await (self._call_worker(job, ctx, phase) if job.engine.worker else self._run_child(job, ctx))
        try:
            result = job.engine.parse(ctx, stdout)
        except BadOutput:
            raise ServiceError(502, "engine_failed", "the engine returned an invalid answer") from None
        if ctx.last:
            job.result = result

    async def _run_child(self, job, ctx):
        """One child for this phase: it runs, answers on stdout and its group is killed."""
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
        return stdout

    def _discard_worker(self, engine, phase_name, worker):
        if worker is not None:
            if engine.workers.get(phase_name) is worker:
                del engine.workers[phase_name]
            worker.kill()
            engine.retire(worker)

    @staticmethod
    async def _prepare(engine, phase_name):
        """engine.prepare in a thread (it hashes the model files in full). A timeout or a cancel stops the hashing at
        its next chunk and the job keeps its slot until the thread is done, so no hashing outlives its admission and
        a launch that finished anyway has its scratch directory removed."""
        cancel = threading.Event()
        task = asyncio.ensure_future(asyncio.to_thread(engine.prepare, phase_name, cancel))
        try:
            return await asyncio.shield(task)
        except BaseException:
            cancel.set()
            while not task.done():
                try:
                    await asyncio.wait({task})
                except asyncio.CancelledError:
                    pass
            if task.exception() is None:
                shutil.rmtree(task.result().scratch, ignore_errors=True)
            raise

    async def _call_worker(self, job, ctx, phase):
        """Send the phase's job to the warm worker of `<alias>/<phase>`, starting it first when there is none (model
        files verified in full, then loaded). The whole call is bounded by the alias's timeout; a worker that fails,
        times out or is cancelled mid-job dies with its group, so no half-answered job is ever reused."""
        engine, worker = job.engine, job.engine.workers.get(phase.name)
        try:
            async with asyncio.timeout(job.spec.timeout_seconds):
                if worker is None or not worker.alive():
                    self._discard_worker(engine, phase.name, worker)
                    running = self.slots[phase.slot].running
                    if running is not None and running.job is job and running.warm:
                        # Admitted on the memory of a warm worker that is gone now (however it was lost: the book
                        # entry may be gone with it): a replacement is a cold start and goes through admission as
                        # one, so the client asks again.
                        raise self.closed("worker_lost")
                    launch = await self._prepare(engine, phase.name)
                    try:
                        worker = await Worker.spawn(launch.argv, launch.env, launch.scratch, (job.alias, phase.name),
                                                    phase.resident_gb)
                    except OSError:
                        shutil.rmtree(launch.scratch, ignore_errors=True)
                        raise ServiceError(502, "engine_failed", "the engine could not start") from None
                    engine.workers[phase.name] = worker
                    await worker.ready()
                worker.job = job
                line = await worker.call({"op": "job", "job": engine.job_message(ctx)})
            answer = json.loads(line)
            if not isinstance(answer, dict) or answer.get("ok") is not True:
                raise WorkerFailed("job_failed")
            return json.dumps(answer.get("result")).encode()
        except TimeoutError:
            self._discard_worker(engine, phase.name, worker)
            raise ServiceError(504, "engine_timeout", "the engine did not answer in time") from None
        except (WorkerFailed, ValueError):
            self._discard_worker(engine, phase.name, worker)
            raise ServiceError(502, "engine_failed", "the engine failed") from None
        except BaseException:
            self._discard_worker(engine, phase.name, worker)
            raise
        finally:
            if worker is not None:
                worker.job = None

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
        """Every warm worker over MEMORY_SHARE_FACTOR times its phase's resident_gb loses its process group; when the
        children together (running one-shot children and all workers, idle ones too) still exceed the hard cap, the
        largest loses its group."""
        while True:
            await asyncio.sleep(self.config.supervise_interval_seconds)
            sizes = await self._measure()
            if sizes and sum(size for size, _ in sizes) > self.config.resident_hard_cap_gb:
                _, victim = max(sizes, key=lambda item: item[0])
                self._kill_victim(victim, "memory_hard_cap")

    async def _measure(self):
        """(rss, victim) for every child that holds memory; victim = ("job", job) | ("worker", engine, phase name,
        worker) | ("retiring", worker). A probe awaits a thread, so a worker can retire between two reads: each
        process group is counted once, and a worker that was killed (its memory is already coming back) not at all."""
        sizes, seen = [], set()
        for job in self.children():
            if job.proc is not None:
                seen.add(job.proc.pid)
                sizes.append((await asyncio.to_thread(self.probe.rss_gb, job.proc.pid), ("job", job)))
        for engine in list(self.engines):
            for name, worker in list(engine.workers.items()):
                if worker.alive():
                    seen.add(worker.proc.pid)
                    size = await asyncio.to_thread(self.probe.rss_gb, worker.proc.pid)
                    victim = ("worker", engine, name, worker)
                    if size > MEMORY_SHARE_FACTOR * worker.resident_gb:
                        self._kill_victim(victim, "memory_share")
                    else:
                        sizes.append((size, victim))
        for engine in list(self.engines):
            for worker in engine.retiring_workers():
                if worker.killed or worker.proc.pid in seen:
                    continue
                seen.add(worker.proc.pid)
                size = await asyncio.to_thread(self.probe.rss_gb, worker.proc.pid)
                sizes.append((size, ("retiring", worker)))
        return sizes

    def _kill_victim(self, victim, reason):
        if victim[0] == "retiring":
            kill_group(victim[1].proc.pid)
            return
        if victim[0] == "job":
            job = victim[1]
            job.kill_reason = reason
            if job.proc is not None:
                kill_group(job.proc.pid)
            return
        _, engine, name, worker = victim
        idle = worker.job is None
        if worker.job is not None:
            worker.job.kill_reason = reason
        self._discard_worker(engine, name, worker)
        if idle:
            self.admission.drop(worker.key)  # gone from the resident book as well
