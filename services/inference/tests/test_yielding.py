"""ANYM-691 criteria 1-4 and 6: a running phase stops (SIGSTOP on its group) while Between or the remote tests are busy
and continues when they are done. The oracle is progress, not process-state letters: the fake engines append a byte to
`<path>.main` (the engine process) and `<path>.helper` (a helper in the same group) every 50 ms, and `ps -o stat=` of the
leader is polled for `T`. The rules that depend on time run on a virtual clock and manual ticks (`supervise_interval_seconds`
is 60 there, so the real loop never ticks); the end-to-end ones run on real ticks of 50 ms."""

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from contextlib import asynccontextmanager

import pytest
from deedkit_inference import canary, media, pausestate
from deedkit_inference.config import Config
from deedkit_inference.yielder import ForeignCpu, YieldSettings, load_yield_settings
from engine_support import engines
from support import KEY, ServerProc, fk

pytestmark = pytest.mark.anyio


class ScriptedCpu:
    """The foreign-CPU sampler of a test: whatever `cores` says, or `error`."""

    def __init__(self, cores=2.0):
        self.cores, self.error, self.calls = cores, None, 0

    def sample(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.cores

    def reset(self):
        pass


class Virtual:
    """The monotonic clock of the scheduler and the yielder: the real one plus an offset a test moves."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.monotonic() + self.offset


def size(path):
    return os.path.getsize(path) if os.path.exists(path) else 0


def stat_of(pid):
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def stopped(pid):
    return stat_of(pid).startswith("T")


def settings(**over):
    return YieldSettings(**{"enabled": True, "min_remaining_seconds": 0, "resume_after_seconds": 0.3, **over})


def yielding(env, cpu=None, **over):
    """Keyword arguments for `env.running(...)`: a state directory (the ledger lives there), the settings, a sampler."""
    (env.tmp / "state").mkdir(exist_ok=True)
    return {"usage_file": str(env.tmp / "state" / "usage.json"), "yield_settings": settings(**over),
            "sampler": cpu or ScriptedCpu()}


def long_timeout(seconds=30):
    return {"audio-default": {"timeout_seconds": seconds}}


@asynccontextmanager
async def manual(env, clock, aliases=None, **over):
    """A service whose yielder only ticks when the test says so, on the virtual clock."""
    cpu = ScriptedCpu()
    async with env.running(supervise_interval_seconds=60, aliases=aliases or long_timeout(),
                           **yielding(env, cpu, **over)) as s:
        s.service.scheduler.now = s.service.yielder.now = clock
        yield s, cpu


async def tick(s, clock, seconds=2.0, times=1):
    for _ in range(times):
        clock.offset += seconds
        await s.service.yielder._tick()


async def paused_child(s, clock, path, **directive):
    """A running child with progress files and a pause that has begun (two ticks, flag and CPU both true)."""
    task = await s.submit(s.transcribe(fk(tick=str(path), ticks=directive.pop("ticks", 100), **directive)))
    await s.until(lambda: size(f"{path}.main") > 2)
    pid = s.service.scheduler.children()[0].proc.pid
    s.probe.between = True
    await tick(s, clock, times=2)
    await s.until(lambda: stopped(pid))
    return task, pid


async def finish(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def running_child(s, path):
    """A running child with progress files, before any tick."""
    task = await s.submit(s.transcribe(fk(tick=str(path), ticks=100)))
    await s.until(lambda: size(f"{path}.main") > 2)
    return task, s.service.scheduler.children()[0].proc.pid


async def stops_on_the_second_tick(s, clock, pid):
    """The flag and the CPU hold now: the count of manual ticks decides, not the wall clock of a loaded host (ANYM-791).
    The signal is sent inside the second tick; `ps` may show it a little later, hence the deadline."""
    await tick(s, clock)
    assert s.service.yielder.paused == {} and not stopped(pid), "stopped on the first tick"
    await tick(s, clock)
    assert len(s.service.yielder.paused) == 1, "not stopped on the second tick"
    await s.until(lambda: stopped(pid), 5)


# --- criterion 1: what is paused, and what is not --------------------------------------------------------------
# spec: ANYM-691
async def test_running_phase_stops_while_busy_and_continues_after(env):
    path = env.tmp / "prog"
    async with env.running(aliases=long_timeout(), **yielding(env)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=60)))
        await s.until(lambda: size(f"{path}.main") > 3 and size(f"{path}.helper") > 3)
        pid = s.service.scheduler.children()[0].proc.pid
        s.probe.between = True
        await s.until(lambda: stopped(pid))
        frozen = (size(f"{path}.main"), size(f"{path}.helper"))
        await asyncio.sleep(0.6)
        assert (size(f"{path}.main"), size(f"{path}.helper")) == frozen  # neither the engine nor its helper advanced
        s.probe.between = False
        await s.until(lambda: size(f"{path}.main") > frozen[0] and size(f"{path}.helper") > frozen[1])
        reply = await task
        assert reply.status == 200 and reply.json()["text"] == "fake transcript"  # the same answer as an unpaused run
        assert size(f"{path}.main") == 60
        await s.until(lambda: not (env.tmp / "state" / pausestate.LEDGER_NAME).exists())


# spec: ANYM-691
async def test_a_worker_phase_and_its_helper_stop_together(env):
    path = env.tmp / "prog"
    async with env.running(**engines(env, timeout=30), **yielding(env)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=60), extra_fields={"language": "ru"}))
        await s.until(lambda: size(f"{path}.main") > 3 and size(f"{path}.helper") > 3)
        worker = next(iter(s.service.engines["audio-default"].workers.values()))
        s.probe.ci = True
        await s.until(lambda: stopped(worker.proc.pid) and worker.paused)
        assert worker.alive()  # a paused worker is neither stopped nor dead: reconcile never evicts it
        frozen = (size(f"{path}.main"), size(f"{path}.helper"))
        await asyncio.sleep(0.6)
        assert (size(f"{path}.main"), size(f"{path}.helper")) == frozen
        s.probe.ci = False
        reply = await task
        assert reply.status == 200 and size(f"{path}.main") == 60 and not worker.paused


# spec: ANYM-691
async def test_loading_and_preparation_are_never_paused(env):
    async with env.running(**engines(env, timeout=30, extra_options={"runtime_options": {"load_sleep": 1.5}}), **yielding(env)) as s:
        task = await s.submit(s.transcribe(fk(), extra_fields={"language": "ru"}))
        engine = s.service.engines["audio-default"]
        s.probe.between = True
        await s.until(lambda: bool(engine.workers))  # the worker exists and is loading its model
        worker = next(iter(engine.workers.values()))
        end = time.monotonic() + 1.0
        while worker.job is None and time.monotonic() < end:
            assert not stopped(worker.proc.pid) and s.service.yielder.paused == {}
            await asyncio.sleep(0.05)
        s.probe.between = False
        assert (await task).status == 200


# spec: ANYM-691
async def test_a_stop_that_lands_during_a_decode_is_undone_and_the_decoder_deadline_survives(env, monkeypatch):
    """The marker appears between the ledger write and the stop: the stop lands after it and is undone at once, and the
    group is not stopped again while the marker exists."""
    from deedkit_inference.yielder import Yielder

    original, made = Yielder._record, []

    def record_then_mark(self, candidate):
        result = original(self, candidate)
        if not made:
            made.append(candidate.marker)
            open(candidate.marker, "w").close()
        return result

    monkeypatch.setattr(Yielder, "_record", record_then_mark)
    path = env.tmp / "prog"
    async with env.running(**engines(env, timeout=30), **yielding(env, resume_after_seconds=0.2)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=50), extra_fields={"language": "ru"}))
        await s.until(lambda: size(f"{path}.main") > 3)
        worker = next(iter(s.service.engines["audio-default"].workers.values()))
        s.probe.between = True
        await s.until(lambda: bool(made))
        await asyncio.sleep(0.5)  # the marker exists the whole time: no pause is left standing, none is begun
        assert s.service.yielder.paused == {} and not stopped(worker.proc.pid)
        advanced = size(f"{path}.main")
        await asyncio.sleep(0.3)
        assert size(f"{path}.main") > advanced
        s.probe.between = False
        assert (await task).status == 200


# spec: ANYM-691
async def test_a_marker_that_exists_keeps_the_group_running_and_its_removal_allows_the_pause(env):
    path = env.tmp / "prog"
    async with env.running(**engines(env, timeout=30), **yielding(env)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=100), extra_fields={"language": "ru"}))
        await s.until(lambda: size(f"{path}.main") > 3)
        worker = next(iter(s.service.engines["audio-default"].workers.values()))
        marker = os.path.join(worker.scratch, media.MARKER_NAME)
        open(marker, "w").close()
        s.probe.between = True
        await asyncio.sleep(0.6)
        assert not stopped(worker.proc.pid) and s.service.yielder.paused == {}
        os.unlink(marker)
        await s.until(lambda: stopped(worker.proc.pid))
        s.probe.between = False
        await finish(task)


# spec: ANYM-691
async def test_flags_on_across_a_decode_never_stop_the_decoder_and_its_deadline_holds(env):
    """A 1.5 s decode helper under a 2.5 s `communicate` deadline while Between is busy the whole time: the marker keeps
    the group running for as long as the helper does; the tick phase after it is paused as usual."""
    path = env.tmp / "prog"
    async with env.running(**engines(env, timeout=30), **yielding(env)) as s:
        task = await s.submit(s.transcribe(fk(decode={"seconds": 1.5, "timeout": 2.5}, tick=str(path), ticks=20),
                                           extra_fields={"language": "ru"}))
        worker = None
        await s.until(lambda: bool(s.service.engines["audio-default"].workers))
        worker = next(iter(s.service.engines["audio-default"].workers.values()))
        marker = os.path.join(worker.scratch, media.MARKER_NAME)
        await s.until(lambda: os.path.exists(marker))
        s.probe.between = True
        while os.path.exists(marker):  # the decoder is running: the group is never stopped, nothing is paused
            assert not stopped(worker.proc.pid) and s.service.yielder.paused == {}
            await asyncio.sleep(0.05)
        await s.until(lambda: stopped(worker.proc.pid))  # the phase after the decode is paused
        s.probe.between = False
        assert (await task).status == 200  # and the decode finished inside its deadline


# spec: ANYM-691
async def test_a_stop_before_the_marker_exists_keeps_the_decoder_deadline_unspent(env):
    """The group is stopped for 1.5 s BEFORE the decode helper starts (no marker yet): the helper's 1.5 s deadline
    starts after the continue, so the 1.0 s decode finishes inside it."""
    async with env.running(**engines(env, timeout=30), **yielding(env)) as s:
        task = await s.submit(s.transcribe(fk(pre=1.0, decode={"seconds": 1.0, "timeout": 1.5}),
                                           extra_fields={"language": "ru"}))
        await s.until(lambda: bool(s.service.engines["audio-default"].workers))
        s.probe.between = True
        await s.until(lambda: bool(s.service.yielder.paused))
        await asyncio.sleep(1.5)
        s.probe.between = False
        assert (await task).status == 200


# spec: ANYM-691
async def test_a_stop_after_the_marker_exists_is_undone_before_the_decoder_deadline_runs_out(env, monkeypatch):
    """The ledger write is held until the decode helper's marker exists, so the stop lands after it, with the helper
    running under a 1.5 s deadline. Contention never clears: without the marker protocol the group would stay stopped."""
    from deedkit_inference.yielder import Yielder

    original, real, calls = Yielder._record, os.killpg, []

    def record_when_marked(self, candidate):
        end = time.monotonic() + 8
        while not os.path.exists(candidate.marker) and time.monotonic() < end:
            time.sleep(0.01)
        return original(self, candidate)

    def killpg(pgid, number):
        if number in (signal.SIGSTOP, signal.SIGCONT):
            calls.append((number, any(os.path.exists(p) for p in marker_paths())))
        return real(pgid, number)

    marker_paths = lambda: [os.path.join(w.scratch, media.MARKER_NAME) for w in workers()]  # noqa: E731
    monkeypatch.setattr(Yielder, "_record", record_when_marked)
    monkeypatch.setattr(os, "killpg", killpg)
    async with env.running(**engines(env, timeout=30), **yielding(env, resume_after_seconds=0.2)) as s:
        workers = lambda: list(s.service.engines["audio-default"].workers.values())  # noqa: E731
        task = await s.submit(s.transcribe(fk(pre=0.6, decode={"seconds": 1.0, "timeout": 1.5}),
                                           extra_fields={"language": "ru"}))
        await s.until(lambda: bool(workers()))
        s.probe.between = True  # and it stays on
        reply = await task
        assert reply.status == 200
        # the SIGSTOP really went out, while the decode helper's marker existed, and was followed by a SIGCONT
        assert calls[0] == (signal.SIGSTOP, True) and calls[1][0] == signal.SIGCONT
        assert float(reply.headers["x-paused-seconds"]) < 0.3


# spec: ANYM-691
def test_run_tool_fails_closed_when_the_marker_cannot_be_made(tmp_path):
    with pytest.raises(OSError):
        media.run_tool([sys.executable, "-I", "-c", "pass"], 5, own_group=False,
                       marker=str(tmp_path / "missing" / media.MARKER_NAME))


# spec: ANYM-691
def test_run_tool_holds_the_marker_while_the_helper_runs_and_removes_it_after(tmp_path):
    marker = tmp_path / media.MARKER_NAME
    probe = [sys.executable, "-I", "-c", f"import os; print(os.path.exists({str(marker)!r}))"]
    code, out = media.run_tool(probe, 10, own_group=False, marker=str(marker))
    assert code == 0 and out.strip() == b"True" and not marker.exists()


# spec: ANYM-691
def test_decode_deadline_still_fires_on_a_genuine_hang(tmp_path):
    marker = tmp_path / media.MARKER_NAME
    started = time.monotonic()
    assert media.run_tool(["sleep", "30"], 0.5, own_group=False, marker=str(marker)) == (None, b"")
    assert 0.4 < time.monotonic() - started < 5 and not marker.exists()  # the deadline fired and the marker is gone


# spec: ANYM-691
async def test_a_decode_that_hangs_fails_the_job_with_the_decoder_deadline(env):
    async with env.running(**engines(env, timeout=30), **yielding(env)) as s:
        reply = await s.transcribe(fk(decode={"seconds": 30, "timeout": 0.5}), extra_fields={"language": "ru"})
        assert reply.status == 502 and reply.code == "engine_failed"


# spec: ANYM-691
async def test_only_the_last_phase_of_a_job_is_paused(env):
    path = env.tmp / "prog"
    async with env.running(aliases={"audio-two-phase": {"timeout_seconds": 30}}, **yielding(env)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=40, tick_phase=0), model="audio-two-phase"))
        await s.until(lambda: size(f"{path}.main") > 3)
        s.probe.between = True  # phase 0 is running and a later phase waits behind it: it is never stopped
        before = size(f"{path}.main")
        await asyncio.sleep(0.6)
        assert size(f"{path}.main") > before and s.service.yielder.paused == {}
        s.probe.between = False
        await finish(task)


# spec: ANYM-691
async def test_a_phase_about_to_finish_is_not_paused(env):
    path = env.tmp / "prog"
    async with env.running(aliases=long_timeout(), **yielding(env, min_remaining_seconds=20)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=60, estimate=5)))
        await s.until(lambda: size(f"{path}.main") > 3)
        s.probe.between = True
        before = size(f"{path}.main")
        await asyncio.sleep(0.6)
        assert size(f"{path}.main") > before and s.service.yielder.paused == {}  # finishing is cheaper than stopping
        s.probe.between = False
        assert (await task).status == 200


# spec: ANYM-691
async def test_an_idle_warm_worker_is_not_stopped(env):
    async with env.running(**engines(env, timeout=30), **yielding(env)) as s:
        assert (await s.transcribe(fk(), extra_fields={"language": "ru"})).status == 200
        worker = next(iter(s.service.engines["audio-default"].workers.values()))
        s.probe.between = True
        await asyncio.sleep(0.6)
        assert not stopped(worker.proc.pid) and s.service.yielder.paused == {} and worker.alive()


# --- criterion 2: when ----------------------------------------------------------------------------------------
# spec: ANYM-691
async def test_stop_follows_a_fresh_busy_flag_and_foreign_cpu_within_two_ticks(env):
    clock, path = Virtual(), env.tmp / "prog"
    async with manual(env, clock) as (s, _cpu):
        task, pid = await running_child(s, path)
        s.probe.ci = True
        await stops_on_the_second_tick(s, clock, pid)
        await finish(task)


# spec: ANYM-791
async def test_the_two_tick_check_fails_when_the_stop_comes_on_the_fourth_tick(env):
    clock, path = Virtual(), env.tmp / "prog"
    async with manual(env, clock) as (s, _cpu):
        task, pid = await running_child(s, path)
        s.probe.ci = True
        s.service.yielder.streak = -2  # the streak reaches two on the fourth tick from here
        with pytest.raises(AssertionError, match="second tick"):
            await stops_on_the_second_tick(s, clock, pid)
        await tick(s, clock)
        assert s.service.yielder.paused == {}  # the third tick: still running
        await tick(s, clock)
        assert len(s.service.yielder.paused) == 1  # the fourth tick is the one the check above refused
        await s.until(lambda: stopped(pid), 5)
        await finish(task)


# spec: ANYM-691
async def test_a_busy_flag_on_a_quiet_host_does_not_pause(env):
    path = env.tmp / "prog"
    cpu = ScriptedCpu(0.2)  # a probe that just started, or a Between container that does nothing
    async with env.running(aliases=long_timeout(), **yielding(env, cpu)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=100)))
        await s.until(lambda: size(f"{path}.main") > 2)
        s.probe.between = True
        before = size(f"{path}.main")
        await asyncio.sleep(0.6)
        assert size(f"{path}.main") > before and s.service.yielder.paused == {}
        await finish(task)


# spec: ANYM-691
def test_the_service_s_own_cpu_never_opens_the_gate():
    clock = {"now": 100.0}

    def table(own, foreign):
        return (f"  501  10 Mon Nov  2 10:00:00 2026 {own}\n  501  11 Mon Nov  2 10:00:00 2026 0:01.00\n"
                f"  777  20 Mon Nov  2 10:00:00 2026 {foreign}\n")

    rows = ["0:10.00", "0:10.00"]
    sampler = ForeignCpu(uids={777}, run_ps=lambda: table(*rows), clock=lambda: clock["now"])
    assert sampler.sample() is None  # nothing to compare with yet
    clock["now"], rows[:] = 102.0, ["1:10.00", "0:10.00"]  # the service user's child burned a minute of CPU
    assert sampler.sample() == 0.0
    clock["now"], rows[:] = 104.0, ["2:10.00", "0:14.00"]  # the foreign user burned 4 s over 2 s
    assert sampler.sample() == pytest.approx(2.0)


# spec: ANYM-691
def test_a_new_foreign_process_counts_in_full_and_a_reused_pid_is_a_new_process():
    clock = {"now": 0.0}
    text = {"v": "  777  20 Mon Nov  2 10:00:00 2026 0:50.00\n"}
    sampler = ForeignCpu(uids={777}, run_ps=lambda: text["v"], clock=lambda: clock["now"])
    sampler.sample()
    clock["now"], text["v"] = 2.0, "  777  20 Mon Nov  2 10:00:05 2026 0:03.00\n"  # pid 20 again, another start time
    assert sampler.sample() == pytest.approx(1.5)


# spec: ANYM-691
def test_a_malformed_process_table_is_unknown_not_zero():
    from deedkit_inference.yielder import SampleError

    sampler = ForeignCpu(uids={777}, run_ps=lambda: "garbage line\n")
    with pytest.raises(SampleError):
        sampler.sample()


# spec: ANYM-691
async def test_pause_on_selects_which_flags_pause(env):
    path = env.tmp / "prog"
    async with env.running(aliases=long_timeout(), **yielding(env, pause_on=["deedkit_ci"])) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=100)))
        await s.until(lambda: size(f"{path}.main") > 2)
        pid = s.service.scheduler.children()[0].proc.pid
        s.probe.between = True
        await asyncio.sleep(0.5)
        assert not stopped(pid) and s.service.yielder.paused == {}
        s.probe.ci = True
        await s.until(lambda: stopped(pid))
        await finish(task)


# spec: ANYM-691
async def test_resume_waits_for_the_hysteresis(env):
    clock = Virtual()
    async with manual(env, clock, resume_after_seconds=10) as (s, cpu):
        task, pid = await paused_child(s, clock, env.tmp / "prog")
        s.probe.between = False
        await tick(s, clock, 3)  # 3 s clear: still stopped
        assert stopped(pid) and len(s.service.yielder.paused) == 1
        await tick(s, clock, 3, times=2)  # 9 s
        assert len(s.service.yielder.paused) == 1
        s.probe.between = True  # the flag comes back inside the delay: the clear time starts over
        await tick(s, clock, 3)
        s.probe.between = False
        await tick(s, clock, 6, times=1)
        assert len(s.service.yielder.paused) == 1
        await tick(s, clock, 6, times=2)  # 12 s of clear
        assert s.service.yielder.paused == {}
        await s.until(lambda: not stopped(pid))
        await finish(task)


# spec: ANYM-691
async def test_a_paused_phase_does_not_resume_and_pause_again_as_its_own_load_decays(env):
    clock = Virtual()
    async with manual(env, clock) as (s, cpu):
        task, pid = await paused_child(s, clock, env.tmp / "prog")
        cpu.cores = 0.0  # the foreign load is gone (or its decay lags): the exit does not read it
        await tick(s, clock, 2, times=10)
        assert stopped(pid) and len(s.service.yielder.paused) == 1
        await finish(task)


# spec: ANYM-691
async def test_stale_missing_or_invalid_busy_file_resumes_and_never_pauses(env):
    clock = Virtual()
    async with manual(env, clock) as (s, cpu):
        task, pid = await paused_child(s, clock, env.tmp / "prog")
        for problem, age in (("missing", 0.0), ("invalid", 0.0), (None, 120.0)):
            s.probe.problem, s.probe.busy_age = problem, age
            await tick(s, clock, 2)
            assert s.service.yielder.paused == {}
            await s.until(lambda: not stopped(pid))
            await tick(s, clock, 30, times=3)  # never pauses on an unreadable or stale file, flag or not
            assert s.service.yielder.paused == {}
            s.probe.problem, s.probe.busy_age = None, 0.0
            await tick(s, clock, 30, times=2)  # the file is fine again: the next pause comes after the delay
            await s.until(lambda: stopped(pid))
        await finish(task)


# spec: ANYM-691
async def test_alternating_invalid_and_fresh_busy_files_cannot_repause_faster_than_the_reentry_delay(env, caplog):
    caplog.set_level(logging.INFO)
    clock = Virtual()
    async with manual(env, clock, resume_after_seconds=10) as (s, cpu):
        task, pid = await paused_child(s, clock, env.tmp / "prog")
        for index in range(30):  # 30 ticks of 2 s: the file alternates invalid and fresh every tick
            s.probe.problem = "invalid" if index % 2 == 0 else None
            await tick(s, clock, 2)
        episodes = len([r for r in caplog.records if r.getMessage().startswith("yield: job=")])
        assert episodes <= 60 / 10 + 1  # at most one resume per re-entry delay
        await finish(task)


# spec: ANYM-691
async def test_flapping_flag_does_not_toggle_every_tick(env, caplog):
    caplog.set_level(logging.INFO)
    clock = Virtual()
    async with manual(env, clock) as (s, cpu):
        task = await s.submit(s.transcribe(fk(tick=str(env.tmp / "prog"), ticks=100)))
        await s.until(lambda: size(f"{env.tmp / 'prog'}.main") > 2)
        for index in range(20):  # true for one tick, false for the next: never two in a row
            s.probe.between = index % 2 == 0
            await tick(s, clock, 2)
        assert s.service.yielder.paused == {}
        await finish(task)


# --- criterion 3: a pause is bounded by the client's deadline -------------------------------------------------------
# spec: ANYM-691
async def test_pause_is_capped_by_max_pause_seconds(env, caplog):
    caplog.set_level(logging.INFO)
    clock = Virtual()
    async with manual(env, clock, max_pause_seconds=10, resume_after_seconds=1) as (s, cpu):
        task, pid = await paused_child(s, clock, env.tmp / "prog")
        await tick(s, clock, 3, times=5)  # 15 s on the flag
        assert s.service.yielder.paused == {}
        job = s.service.scheduler.children()[0]
        assert job.budget_exhausted and job.no_pause
        await s.until(lambda: not stopped(pid))
        await tick(s, clock, 3, times=5)  # the phase runs on at Nice 20 and is not paused again
        assert s.service.yielder.paused == {}
        assert any("budget_exhausted=1" in r.getMessage() for r in caplog.records)
        await finish(task)


# spec: ANYM-691
async def test_pause_budget_is_computed_at_pause_time_from_the_real_elapsed(env):
    clock = Virtual()
    async with manual(env, clock, deadline_safety_factor=2, deadline_margin_seconds=10) as (s, cpu):
        path = env.tmp / "prog"
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=100, estimate=40),
                                           headers={"X-Request-Deadline-Seconds": "200"}))
        await s.until(lambda: size(f"{path}.main") > 2)
        s.probe.between = True
        clock.offset += 20.0  # 20 s of the request are gone before the pause begins
        await tick(s, clock, 0.001, times=2)
        pause = next(iter(s.service.yielder.paused.values()))
        job = pause.job
        left = 40 - (pause.since - s.service.scheduler.slots["cpu"].running.started)
        expected = 200 - (pause.since - job.accepted) - max(10, 2 * left) - left
        assert pause.until - pause.since == pytest.approx(expected, abs=1.0) and expected < 300
        await finish(task)


# spec: ANYM-691
async def test_a_request_without_a_deadline_uses_the_cap_alone(env):
    clock = Virtual()
    async with manual(env, clock, max_pause_seconds=120) as (s, cpu):
        task, pid = await paused_child(s, clock, env.tmp / "prog")
        pause = next(iter(s.service.yielder.paused.values()))
        assert pause.until - pause.since == pytest.approx(120.0, abs=0.5)
        await finish(task)


# spec: ANYM-691
async def test_no_pause_when_the_slack_is_not_positive(env):
    path = env.tmp / "prog"
    async with env.running(aliases=long_timeout(), **yielding(env)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=100, estimate=1),
                                           headers={"X-Request-Deadline-Seconds": "3"}))  # 3 s cannot absorb K = 3
        await s.until(lambda: size(f"{path}.main") > 2)
        s.probe.between = True
        before = size(f"{path}.main")
        await asyncio.sleep(0.6)
        assert size(f"{path}.main") > before and s.service.yielder.paused == {}
        assert s.service.scheduler.children()[0].no_pause
        await finish(task)


# spec: ANYM-691
async def test_a_job_whose_real_work_is_k_times_its_estimate_still_finishes_inside_its_deadline_after_a_pause(env):
    path = env.tmp / "prog"
    async with env.running(aliases=long_timeout(), **yielding(env, deadline_margin_seconds=0.5)) as s:
        started = time.monotonic()
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=60, estimate=1),  # 3 s of work, 3 times the estimate
                                           headers={"X-Request-Deadline-Seconds": "15"}))
        await s.until(lambda: size(f"{path}.main") > 3)
        s.probe.between = True
        await s.until(lambda: bool(s.service.yielder.paused))
        await asyncio.sleep(3.0)
        s.probe.between = False
        reply = await task
        assert reply.status == 200 and time.monotonic() - started < 15
        assert float(reply.headers["x-paused-seconds"]) > 2.0


# spec: ANYM-691
async def test_a_job_that_spends_its_whole_budget_still_finishes_inside_its_deadline(env):
    """The contention never clears: the pause runs to the end of its budget. Real work is twice the estimate
    (K = 3), so the work that is left after the budget fits what the formula kept free."""
    path = env.tmp / "prog"
    async with env.running(aliases=long_timeout(), **yielding(env, deadline_margin_seconds=0.5)) as s:
        started = time.monotonic()
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=40, estimate=1),
                                           headers={"X-Request-Deadline-Seconds": "8"}))
        await s.until(lambda: size(f"{path}.main") > 3)
        s.probe.between = True  # and it stays on
        reply = await task
        assert reply.status == 200 and time.monotonic() - started < 8
        assert float(reply.headers["x-paused-seconds"]) > 3.5  # the budget (about 4.9 s) was spent


# spec: ANYM-691
async def test_a_job_whose_work_exceeds_k_times_its_estimate_is_the_documented_loss(env, caplog):
    """The counterexample the card documents: 10 times the estimate, K = 3. The pause spends all its budget, the work
    that is left runs past the deadline and the client (read timeout = deadline) has gone: the record shows it."""
    caplog.set_level(logging.INFO)
    path = env.tmp / "prog"
    async with env.running(aliases=long_timeout(), **yielding(env, deadline_margin_seconds=0.2)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=40, estimate=0.2),
                                           headers={"X-Request-Deadline-Seconds": "2"}, disconnect_after_body=2.6))
        await s.until(lambda: size(f"{path}.main") > 2)
        s.probe.between = True
        reply = await task
        assert reply.status == 499
        await s.until(lambda: any(r.getMessage().startswith("yield-job:") for r in caplog.records))
        line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("yield-job:"))
        fields = dict(part.split("=", 1) for part in line.split()[1:])
        assert float(fields["paused"]) > 0 and float(fields["total"]) > float(fields["deadline"])
        assert fields["outcome"] != "ok"


# spec: ANYM-691
async def test_yield_job_lines_are_content_free_and_account_excluded_work(env, caplog):
    caplog.set_level(logging.INFO)
    path = env.tmp / "prog"
    cpu = ScriptedCpu(0.1)  # the flag is on and the host is quiet: the job runs under a busy flag but is ineligible
    async with env.running(aliases=long_timeout(), **yielding(env, cpu)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=30, label="SECRET-LABEL-1234")))
        await s.until(lambda: size(f"{path}.main") > 2)
        s.probe.between = True
        assert (await task).status == 200
        line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("yield-job:"))
        assert re.fullmatch(r"yield-job: job=[0-9a-f]{12} alias=audio-default phase=\S+ est=[\d.]+ deadline=none "
                            r"paused=0.0 excluded=[\d.]+ total=[\d.]+ outcome=ok", line)
        assert float(dict(part.split("=", 1) for part in line.split()[1:])["excluded"]) > 0.5
        assert "SECRET" not in line and str(path) not in line


# spec: ANYM-691
async def test_response_reports_x_paused_seconds(env):
    path = env.tmp / "prog"
    async with env.running(aliases=long_timeout(), **yielding(env)) as s:
        assert (await s.transcribe(fk())).headers["x-paused-seconds"] == "0.000"
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=40)))
        await s.until(lambda: size(f"{path}.main") > 2)
        s.probe.between = True
        await s.until(lambda: bool(s.service.yielder.paused))
        await asyncio.sleep(0.5)
        s.probe.between = False
        reply = await task
        assert reply.status == 200 and float(reply.headers["x-paused-seconds"]) >= 0.4


# spec: ANYM-691
async def test_a_paused_job_is_cancelled_when_the_client_leaves(env):
    path = env.tmp / "prog"
    async with env.running(aliases=long_timeout(), **yielding(env)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=100), disconnect_after_body=1.0))
        await s.until(lambda: size(f"{path}.main") > 2)
        proc = s.service.scheduler.children()[0].proc
        s.probe.between = True
        await s.until(lambda: stopped(proc.pid))
        await task  # the client leaves while the group is stopped
        await s.until(lambda: proc.returncode is not None, 3)  # SIGKILL works on a stopped group
        await s.until(lambda: s.service.yielder.paused == {} and not (env.tmp / "state" / pausestate.LEDGER_NAME).exists())


# --- criterion 4: phase timeouts exclude stopped time -------------------------------------------------------------
# spec: ANYM-691
async def test_paused_time_does_not_count_against_the_phase_timeout(env):
    path = env.tmp / "prog"
    async with env.running(aliases={"audio-default": {"timeout_seconds": 2.5}}, **yielding(env)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=40)))  # 2 s of work under a 2.5 s phase timeout
        await s.until(lambda: size(f"{path}.main") > 10)
        s.probe.between = True
        await s.until(lambda: bool(s.service.yielder.paused))
        await asyncio.sleep(3.0)  # longer than the whole phase timeout
        s.probe.between = False
        reply = await task
        assert reply.status == 200 and size(f"{path}.main") == 40


# spec: ANYM-691
async def test_a_real_hang_still_times_out_after_a_pause(env):
    async with env.running(aliases={"audio-default": {"timeout_seconds": 1.5}}, **yielding(env)) as s:
        task = await s.submit(s.transcribe(fk(mode="hang")))
        await s.until(lambda: bool(s.service.scheduler.children()))
        s.probe.between = True
        await s.until(lambda: bool(s.service.yielder.paused))
        await asyncio.sleep(0.8)
        s.probe.between = False
        reply = await task
        assert reply.status == 504 and reply.code == "engine_timeout"


# spec: ANYM-691
async def test_a_worker_pause_longer_than_the_phase_timeout_does_not_time_it_out(env):
    path = env.tmp / "prog"
    async with env.running(**engines(env, timeout=2.5), **yielding(env)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=40), extra_fields={"language": "ru"}))
        await s.until(lambda: size(f"{path}.main") > 10)
        s.probe.between = True
        await s.until(lambda: bool(s.service.yielder.paused))
        await asyncio.sleep(3.0)  # longer than the whole phase timeout
        s.probe.between = False
        reply = await task
        assert reply.status == 200 and size(f"{path}.main") == 40


# spec: ANYM-691
async def test_a_worker_hang_still_times_out_after_a_pause(env):
    async with env.running(**engines(env, timeout=2.0), **yielding(env)) as s:
        task = await s.submit(s.transcribe(fk(mode="hang"), extra_fields={"language": "ru"}))
        await s.until(lambda: bool(s.service.engines["audio-default"].workers))
        worker = next(iter(s.service.engines["audio-default"].workers.values()))
        await s.until(lambda: worker.job is not None)
        s.probe.between = True
        await s.until(lambda: bool(s.service.yielder.paused))
        await asyncio.sleep(0.8)
        s.probe.between = False
        reply = await task
        assert reply.status == 504 and reply.code == "engine_timeout"


# spec: ANYM-691
async def test_estimated_wait_excludes_paused_time(env):
    clock = Virtual()
    async with manual(env, clock) as (s, cpu):
        task = await s.submit(s.transcribe(fk(tick=str(env.tmp / "prog"), ticks=100, estimate=50)))
        await s.until(lambda: size(f"{env.tmp / 'prog'}.main") > 2)
        clock.offset += 10.0
        before = s.service.scheduler.estimated_wait("cpu")
        s.probe.between = True
        await tick(s, clock, 0.001, times=2)
        assert s.service.yielder.paused
        clock.offset += 100.0  # a long pause
        assert s.service.scheduler.estimated_wait("cpu") == pytest.approx(before, abs=1.0)
        await finish(task)


# --- criterion 5: a stopped group never outlives its service ---------------------------------------------------------
# spec: ANYM-691
async def test_the_ledger_names_the_group_before_it_is_stopped_and_loses_it_after(env):
    clock = Virtual()
    async with manual(env, clock) as (s, cpu):
        task, pid = await paused_child(s, clock, env.tmp / "prog")
        groups = pausestate.Ledger(env.tmp / "state" / pausestate.LEDGER_NAME).groups()
        assert [g["pgid"] for g in groups] == [pid] and groups[0]["leader"] == pid
        s.probe.between = False
        await tick(s, clock, 20, times=2)
        await s.until(lambda: not (env.tmp / "state" / pausestate.LEDGER_NAME).exists())  # removed off the loop
        await finish(task)


# spec: ANYM-691
async def test_the_ledger_names_the_group_before_the_stop_signal(env, monkeypatch):
    clock, seen = Virtual(), []
    real = os.killpg

    def killpg(pgid, number):
        if number == signal.SIGSTOP:
            seen.append([g["pgid"] for g in pausestate.Ledger(env.tmp / "state" / pausestate.LEDGER_NAME).groups()])
            seen.append(pgid)
        return real(pgid, number)

    monkeypatch.setattr(os, "killpg", killpg)
    async with manual(env, clock) as (s, cpu):
        task, pid = await paused_child(s, clock, env.tmp / "prog")
        assert seen == [[pid], pid]  # the entry was on disk when the signal went out
        await finish(task)


# spec: ANYM-691
async def test_the_ledger_keeps_the_real_stop_time_so_no_window_ahead_of_it_exists(env):
    clock = Virtual()
    async with manual(env, clock) as (s, cpu):
        task, pid = await paused_child(s, clock, env.tmp / "prog")
        entry = pausestate.Ledger(env.tmp / "state" / pausestate.LEDGER_NAME).groups()[0]
        assert time.time() - 30 < entry["stopped_at"] <= time.time() + 1
        await finish(task)


# spec: ANYM-691
def test_recovery_owns_a_helper_spawned_between_the_record_and_the_stop_by_descent_with_no_later_write():
    """The ledger write precedes the freeze, so a crash right after the stop leaves only that entry. A stopped process
    that started in a later second (within LATE_SECONDS) is a member when it descends from a member; a process of a
    reused group number has no member as an ancestor and is never claimed."""
    leader, late, later = "Mon Nov 2 09:00:00 2026", "Mon Nov 2 10:00:05 2026", "Mon Nov 2 10:00:06 2026"
    stop = time.mktime(time.strptime("Mon Nov 2 10:00:04 2026", pausestate.START_FORMAT)) + 0.5
    group = {"pgid": 500, "leader": 500, "leader_start": leader, "stopped_at": stop}

    def owned(*lines, entry=group):
        table = pausestate.process_table(lambda: "".join(f"{line}\n" for line in lines))
        return sorted(row["pid"] for row in pausestate.members(entry, table))

    base = (f"500 500 T {leader} 1", f"777 500 T {late} 500", f"778 500 T {later} 777")
    assert owned(*base) == [500, 777, 778]  # a helper of the leader, and its own child
    assert owned(*base, f"779 500 T {late} 1") == [500, 777, 778]  # stopped in the group but not a descendant
    assert owned(*base, "780 500 T Mon Nov 2 10:00:30 2026 500") == [500, 777, 778]  # beyond the late window
    assert owned(f"500 500 T {leader} 1", f"777 500 S {late} 500") == [500]  # only stopped members
    # a reused number: the leader pid started later (nothing matches), or no member exists at all (a leaderless group)
    assert owned(f"500 500 S {later} 1", f"781 500 T {late} 500") == []
    assert owned(f"782 500 T {late} 1", f"783 500 T {later} 782") == []


# spec: ANYM-691
def test_process_table_reads_the_parent_pid():
    row = next(r for r in pausestate.process_table() if r["pid"] == os.getpid())
    assert row["ppid"] == os.getppid()


# --- PR 3: the two-phase freeze ----------------------------------------------------------------------------------------
LEADER_AT = "Mon Nov 2 09:00:00 2026"
HELPER_AT = "Mon Nov 2 09:00:01 2026"
RECORDED = time.mktime(time.strptime("Mon Nov 2 10:00:04 2026", pausestate.START_FORMAT)) + 0.5  # the freezing record
SEEN = [{"pid": 500, "started": LEADER_AT}, {"pid": 600, "started": HELPER_AT}]  # what the group held at the record


def ps_lines(*lines):
    return pausestate.process_table(lambda: "".join(f"{line}\n" for line in lines))


def owned_by(entry, *lines):
    return sorted(row["pid"] for row in pausestate.members(entry, ps_lines(*lines)))


def freezing(**over):
    return {"pgid": 500, "leader": 500, "leader_start": LEADER_AT, "stopped_at": RECORDED, "phase": "freezing", "seen": SEEN, **over}


# spec: ANYM-691
def test_a_freezing_entry_owns_a_late_helper_whose_parent_is_gone_however_late_the_stop_landed():
    """Sol r4 of PR 2: the record precedes the freeze, a helper starts in a later second, the service dies right after the stop,
    and the helper's parent is killed before recovery: the orphan (ppid 1) must still be recovered. Sol r1 and Astra r1 of PR 3:
    no time bound is relied on (a stall can delay the signal by any amount), the group's identity is the witness."""
    orphan, much_later = "Mon Nov 2 10:00:05 2026", "Mon Nov 2 10:05:00 2026"
    base = (f"500 500 T {LEADER_AT} 1", f"777 500 T {orphan} 1", f"778 500 T {much_later} 1")
    assert owned_by(freezing(), *base) == [500, 777, 778]  # the leader lives: the group is the recorded one
    assert owned_by(freezing(), *base, f"780 500 S {orphan} 1") == [500, 777, 778]  # a member that is not stopped is not claimed
    # the leader is gone too: a process seen at the record still lives in the group, so it is still the same group
    assert owned_by(freezing(), f"600 500 T {HELPER_AT} 1", *base[1:]) == [600, 777, 778]
    # an entry of a release before the two-phase freeze (no phase) never owned an orphan
    legacy = {k: v for k, v in freezing().items() if k not in ("phase", "seen")}
    assert owned_by(legacy, *base) == [500]


# spec: ANYM-691
def test_recovery_continues_and_kills_a_reparented_late_helper_of_a_freezing_entry(tmp_path):
    table = {500: f"500 500 T {LEADER_AT} 1", 777: "777 500 T Mon Nov 2 10:00:05 2026 1"}
    sent = []

    def send(pid, number):
        sent.append((pid, number))
        if number == signal.SIGKILL:
            table.pop(pid, None)

    path = tmp_path / pausestate.LEDGER_NAME
    pausestate.Ledger(path).record(500, 500, LEADER_AT, stopped_at=RECORDED, freezing=True, seen=SEEN)
    report = pausestate.recover(path, run_ps=lambda: "".join(f"{line}\n" for line in table.values()), send=send,
                                sleep=lambda seconds: None)
    assert (777, signal.SIGCONT) in sent and (777, signal.SIGKILL) in sent and sorted(report["killed"]) == [500, 777]
    assert report["kept"] == [] and not path.exists()  # the entry goes only when no member is left


# spec: ANYM-691
def test_a_reused_leaderless_group_is_never_claimed_not_even_inside_the_freezing_window():
    """Sol r1 and Astra r1: no time quarantine keeps a group number from being reused within seconds. The old group is gone, the
    number is reused, the new leader exited, an unrelated stopped helper remains: without a witness of the recorded group nothing
    started after the record is claimed, whenever it started."""
    inside, later = "Mon Nov 2 10:00:06 2026", "Mon Nov 2 10:00:30 2026"
    assert owned_by(freezing(), f"790 500 T {inside} 1", f"791 500 T {inside} 790") == []  # leaderless, nothing seen is alive
    assert owned_by(freezing(), f"790 500 T {later} 1") == []
    assert owned_by(freezing(), f"600 500 T {inside} 1", f"792 500 T {inside} 600") == []  # a seen pid, reused: another start time
    assert owned_by(freezing(), f"500 500 S {inside} 1", f"793 500 T {inside} 500") == []  # the leader number is a newer process
    # a process that was not seen is no witness: it is claimed only for having existed at the record, its child is not
    assert owned_by(freezing(), f"601 500 T {HELPER_AT} 1", f"794 500 T {inside} 601") == [601]
    # without a witness the entry owns only what existed at the record, like any entry
    assert owned_by(freezing(), f"795 500 T {LEADER_AT} 1", f"796 500 T {inside} 1") == [795]
    # a confirmed entry has no slack: a process of the next second after the real stop is not a member
    stopped = freezing(phase="stopped", stopped_at=RECORDED + 1.0)
    stopped.pop("seen")
    assert owned_by(stopped, f"797 500 T {LEADER_AT} 1", "798 500 T Mon Nov 2 10:00:06 2026 1") == [797]


# spec: ANYM-691
def test_recovery_continues_but_never_kills_a_late_helper_when_every_witness_of_the_group_is_gone(tmp_path):
    """Sol r2: the stop stalled, a helper was born at 10:00:10, the service crashed before the second write, and then the leader
    and every process seen at the record were killed. Nothing witnesses the group, and a stranger of a reused number looks the same:
    the stopped process is only CONTINUED (it frees its memory when it ends), never killed, and the entry goes."""
    table = {777: "777 500 T Mon Nov 2 10:00:10 2026 1"}
    sent = []

    def send(pid, number):
        sent.append((pid, number))
        if number == signal.SIGCONT and pid in table:
            table[pid] = table[pid].replace(" T ", " S ")

    path = tmp_path / pausestate.LEDGER_NAME
    pausestate.Ledger(path).record(500, 500, LEADER_AT, stopped_at=RECORDED, freezing=True, seen=SEEN)
    report = pausestate.recover(path, run_ps=lambda: "".join(f"{line}\n" for line in table.values()), send=send,
                                sleep=lambda seconds: None)
    assert sent == [(777, signal.SIGCONT)] and report["killed"] == [] and report["continued"] == [777]
    assert report["kept"] == [] and not path.exists()


# spec: ANYM-691
def test_only_a_freezing_entry_without_any_witness_continues_strays():
    late = "Mon Nov 2 10:00:10 2026"

    def strays(entry, *lines):
        return sorted(row["pid"] for row in pausestate.late_strays(entry, ps_lines(*lines)))

    assert strays(freezing(), f"777 500 T {late} 1", f"778 500 S {late} 1") == [777]  # stopped, in the group, unclaimed
    assert strays(freezing(), f"500 500 T {LEADER_AT} 1", f"777 500 T {late} 1") == []  # the leader lives: members owns it all
    assert strays(freezing(), f"500 500 S {late} 1", f"777 500 T {late} 1") == []  # a newer leader: the number is reused for certain
    assert strays(freezing(), f"600 500 T {HELPER_AT} 1", f"777 500 T {late} 1") == []  # a seen process lives: witnessed
    legacy = {k: v for k, v in freezing().items() if k not in ("phase", "seen")}
    stopped = {**freezing(phase="stopped"), "seen": []}
    assert strays(legacy, f"777 500 T {late} 1") == [] and strays(stopped, f"777 500 T {late} 1") == []  # only the freezing phase


# spec: ANYM-691
def test_a_stray_that_cannot_be_continued_keeps_the_entry_and_is_not_reported_as_continued(tmp_path):
    """Sol r3: a PermissionError on the SIGCONT of a stray must not delete the ledger entry or claim a continue."""
    table = {777: "777 500 T Mon Nov 2 10:00:10 2026 1"}
    sent = []

    def send(pid, number):
        sent.append((pid, number))
        raise PermissionError

    path = tmp_path / pausestate.LEDGER_NAME
    pausestate.Ledger(path).record(500, 500, LEADER_AT, stopped_at=RECORDED, freezing=True, seen=SEEN)
    report = pausestate.recover(path, run_ps=lambda: "".join(f"{line}\n" for line in table.values()), send=send,
                                sleep=lambda seconds: None)
    assert sent == [(777, signal.SIGCONT)] and "continued" not in report and report["killed"] == []
    assert report["kept"] == [500] and path.exists()  # the deploy sees a failed recovery and does not go on


# spec: ANYM-691
def test_a_ledger_file_cannot_carry_a_malformed_witness(tmp_path):
    ledger = pausestate.Ledger(tmp_path / pausestate.LEDGER_NAME)
    ledger.record(500, 500, LEADER_AT, stopped_at=RECORDED, freezing=True, seen=SEEN)
    entry = ledger.groups()[0]
    assert entry["phase"] == "freezing" and entry["seen"] == SEEN
    assert ledger.confirm(500, RECORDED, RECORDED + 0.3) and "seen" not in ledger.groups()[0]
    assert ledger.groups()[0]["phase"] == "stopped" and ledger.groups()[0]["stopped_at"] == RECORDED + 0.3
    huge = [{"pid": n, "started": LEADER_AT} for n in range(pausestate.MAX_SEEN + 1)]
    for bad in ({"seen": huge}, {"seen": "x"}, {"seen": [{"pid": "1", "started": LEADER_AT}]}, {"seen": [{"pid": 1}]}, {"phase": "thawing"}):
        (tmp_path / pausestate.LEDGER_NAME).write_text(json.dumps({"version": 1, "instance": "x", "groups": [{**freezing(), **bad}]}))
        with pytest.raises(pausestate.LedgerError):
            ledger.groups()


# spec: ANYM-691
async def test_the_entry_is_freezing_with_the_group_seen_when_the_signal_goes_out_and_stopped_with_the_real_time_after(env, monkeypatch):
    clock, seen = Virtual(), []
    real = os.killpg

    def killpg(pgid, number):
        if number == signal.SIGSTOP:
            seen.append(pausestate.Ledger(env.tmp / "state" / pausestate.LEDGER_NAME).groups()[0])
        result = real(pgid, number)
        if number == signal.SIGSTOP:
            seen.append(time.time())  # the stop has landed
        return result

    monkeypatch.setattr(os, "killpg", killpg)
    async with manual(env, clock) as (s, cpu):
        task, pid = await paused_child(s, clock, env.tmp / "prog")
        before, landed = seen
        assert before["phase"] == "freezing" and before["pgid"] == pid
        assert pid in {known["pid"] for known in before["seen"]}  # the leader, and its helper if it had started
        ledger = pausestate.Ledger(env.tmp / "state" / pausestate.LEDGER_NAME)
        await s.until(lambda: ledger.groups()[0].get("phase") == "stopped")  # the second write, off the loop
        entry = ledger.groups()[0]
        assert "seen" not in entry and landed - 0.001 <= entry["stopped_at"] <= time.time() + 1
        assert entry["stopped_at"] >= before["stopped_at"]
        await finish(task)


# spec: ANYM-691
async def test_a_stop_that_lands_long_after_the_record_is_still_sent_and_stays_recoverable(env, monkeypatch):
    """No time bound is relied on: the loop or the ledger thread stalls for 30 s between the write and the signal, the signal is
    sent, and until the second write the freezing entry (which owns by the group's identity) is what a crash would find."""
    clock, entries, skew = Virtual(), [], [0.0]
    real_killpg = os.killpg

    def killpg(pgid, number):
        if number == signal.SIGSTOP:
            entries.append(pausestate.Ledger(env.tmp / "state" / pausestate.LEDGER_NAME).groups()[0])
        return real_killpg(pgid, number)

    monkeypatch.setattr(os, "killpg", killpg)
    async with manual(env, clock) as (s, cpu):
        y = s.service.yielder
        y.wall = lambda: time.time() + skew[0]
        record = y.ledger.record

        def stalled(*args, **kwargs):
            record(*args, **kwargs)
            skew[0] += 30.0  # far beyond any bound the old design could have used

        y.ledger.record = stalled
        task, pid = await paused_child(s, clock, env.tmp / "prog")
        assert entries and entries[0]["phase"] == "freezing" and pid in {known["pid"] for known in entries[0]["seen"]}
        assert stopped(pid) and y.paused  # the stop was sent although the record was 30 s old
        await finish(task)


# spec: ANYM-691
async def test_a_worker_whose_call_ended_while_it_was_stopped_is_continued(env):
    """A worker can answer just before the stop lands: the scheduler then clears `worker.job` while the group stays
    stopped. The pause ends with a SIGCONT, never by being forgotten."""
    path = env.tmp / "prog"
    async with env.running(**engines(env, timeout=30), **yielding(env)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=100), extra_fields={"language": "ru"}))
        await s.until(lambda: size(f"{path}.main") > 3)
        worker = next(iter(s.service.engines["audio-default"].workers.values()))
        s.probe.between = True
        await s.until(lambda: stopped(worker.proc.pid))
        worker.job = None  # what consuming a buffered answer does
        await s.until(lambda: not stopped(worker.proc.pid) and s.service.yielder.paused == {})
        await s.until(lambda: not (env.tmp / "state" / pausestate.LEDGER_NAME).exists())
        await finish(task)


# spec: ANYM-691
async def test_a_failed_continue_keeps_the_group_tracked_and_is_retried(env, monkeypatch):
    clock = Virtual()
    real, failing = os.killpg, {"on": False}

    def killpg(pgid, number):
        if failing["on"] and number == signal.SIGCONT:
            raise PermissionError(1, "denied")
        return real(pgid, number)

    monkeypatch.setattr(os, "killpg", killpg)
    async with manual(env, clock) as (s, cpu):
        task, pid = await paused_child(s, clock, env.tmp / "prog")
        failing["on"] = True
        s.probe.between = False
        await tick(s, clock, 20, times=12)  # the continue is due and fails again and again: nothing is forgotten
        pause = next(iter(s.service.yielder.paused.values()))
        assert pause.failures >= 10 and stopped(pid) and not pause.worker
        assert pausestate.Ledger(env.tmp / "state" / pausestate.LEDGER_NAME).groups()
        failing["on"] = False
        await tick(s, clock, 2)  # the retry succeeds
        assert s.service.yielder.paused == {}
        await s.until(lambda: not stopped(pid))
        await finish(task)


# spec: ANYM-691
async def test_a_stuck_ledger_never_holds_back_the_continue_or_the_event_loop(env, monkeypatch):
    clock = Virtual()
    async with manual(env, clock) as (s, cpu):
        task, pid = await paused_child(s, clock, env.tmp / "prog")
        monkeypatch.setattr(pausestate.Ledger, "remove", lambda self, pgid: time.sleep(3))  # a disk that stalls
        s.probe.between = False
        started = time.monotonic()
        await tick(s, clock, 20, times=2)
        await s.until(lambda: not stopped(pid), 1.5)  # continued at once, long before the ledger write returns
        assert time.monotonic() - started < 2.5
        await finish(task)


# spec: ANYM-691
async def test_drain_continues_paused_groups_and_finishes_clean(env):
    path = env.tmp / "prog"
    async with env.running(aliases=long_timeout(), drain_seconds=20, **yielding(env)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=30)))
        await s.until(lambda: size(f"{path}.main") > 2)
        s.probe.between = True
        await s.until(lambda: bool(s.service.yielder.paused))
        started = time.monotonic()
        assert await s.service.drain() is True  # the stopped phase is continued, finishes and the drain ends
        assert time.monotonic() - started < 10
        assert (await task).status == 200


# spec: ANYM-691
async def test_kill_all_works_on_a_stopped_group(env):
    clock = Virtual()
    async with manual(env, clock) as (s, cpu):
        task, pid = await paused_child(s, clock, env.tmp / "prog")
        proc = s.service.scheduler.children()[0].proc
        s.service.scheduler.kill_all()
        await s.until(lambda: proc.returncode is not None, 3)
        await asyncio.gather(task, return_exceptions=True)


# spec: ANYM-691
async def test_the_memory_kill_works_on_a_stopped_group(env):
    path = env.tmp / "prog"
    async with env.running(aliases=long_timeout(), **yielding(env)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=100)))
        await s.until(lambda: size(f"{path}.main") > 2)
        proc = s.service.scheduler.children()[0].proc
        s.probe.between = True
        await s.until(lambda: stopped(proc.pid))
        s.probe.rss_fn = lambda pgid: 100.0  # over the hard cap: the largest child loses its group
        await s.until(lambda: proc.returncode is not None, 3)
        assert (await task).status == 502
        await s.until(lambda: s.service.yielder.paused == {})


# spec: ANYM-691
async def test_a_sample_timeout_malformed_output_or_signal_failure_after_a_stop_continues_the_groups_and_leaves_memory_enforcement_working(env, caplog):
    from deedkit_inference.yielder import SampleError

    caplog.set_level(logging.INFO)
    path = env.tmp / "prog"
    cpu = ScriptedCpu()
    async with env.running(aliases=long_timeout(), **yielding(env, cpu)) as s:
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=100)))
        await s.until(lambda: size(f"{path}.main") > 2)
        proc = s.service.scheduler.children()[0].proc
        s.probe.between = True
        await s.until(lambda: stopped(proc.pid))
        for error in (subprocess.TimeoutExpired("ps", 2), SampleError("unreadable ps line"), OSError("no ps")):
            cpu.error = error
            await s.until(lambda: s.service.yielder.paused == {} and not stopped(proc.pid))  # unknown: continued
            cpu.error = None
            await s.until(lambda: stopped(proc.pid), 10)  # the task restarts after its backoff and may pause again
        assert any("yield: tick failed" in r.getMessage() for r in caplog.records)
        s.probe.rss_fn = lambda pgid: 100.0  # the memory supervisor never noticed any of it
        await s.until(lambda: proc.returncode is not None, 3)
        assert (await task).status == 502


# --- criterion 6: configuration, rollback and read-back ----------------------------------------------------------------
# spec: ANYM-691
def test_old_config_parser_still_loads_config_json_unchanged(env):
    assert Config.model_validate(env.config_dict()).supervise_interval_seconds == 0.05
    with pytest.raises(ValueError):  # the reason yield.json is a file of its own: config.json rejects unknown fields
        Config.model_validate(env.config_dict(pause_on=["between"]))


# spec: ANYM-691
def test_yield_json_absent_or_invalid_means_defaults_and_the_service_starts(tmp_path, caplog, env):
    caplog.set_level(logging.INFO)
    path = tmp_path / "yield.json"
    assert load_yield_settings(path) == YieldSettings()  # absent: defaults, nothing logged
    assert not caplog.records
    for text in ("not json", '{"unknown": 1}', '{"max_pause_seconds": 0}', '{"enabled": "yes"}', '{"pause_on": ["x"]}'):
        path.write_text(text)
        path.chmod(0o600)
        assert load_yield_settings(path) == YieldSettings()
    path.write_text('{"resume_after_seconds": 4}')
    path.chmod(0o666)  # writable by others: ignored like config.json
    assert load_yield_settings(path) == YieldSettings()
    path.chmod(0o600)
    assert load_yield_settings(path).resume_after_seconds == 4
    assert sum("ignored" in r.getMessage() for r in caplog.records) == 6
    (env.tmp / "yield.json").write_text("not json")  # and a service that starts beside it
    (env.tmp / "yield.json").chmod(0o600)
    server = ServerProc(env)
    server.start()
    try:
        assert server.http("GET", "/health")[0] == 200
    finally:
        server.stop()


# spec: ANYM-691
async def test_the_yielder_ships_disabled_and_a_service_without_yield_json_never_stops_a_phase(env):
    assert YieldSettings().enabled is False  # the default: no yield.json, an invalid one, or one without `enabled`
    path = env.tmp / "prog"
    (env.tmp / "state").mkdir()
    async with env.running(aliases=long_timeout(), usage_file=str(env.tmp / "state" / "usage.json"),
                           sampler=ScriptedCpu()) as s:  # no yield_settings: the defaults, as the CLI passes them
        assert s.service.yielder.task is None
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=30)))
        await s.until(lambda: size(f"{path}.main") > 2)
        s.probe.between = True
        assert (await task).status == 200 and size(f"{path}.main") == 30  # busy the whole time, never stopped
        assert s.service.yielder.paused == {}


# spec: ANYM-691
async def test_enabled_false_restores_todays_behaviour(env):
    path = env.tmp / "prog"
    async with env.running(aliases=long_timeout(), **yielding(env, enabled=False)) as s:
        assert s.service.yielder.task is None
        task = await s.submit(s.transcribe(fk(tick=str(path), ticks=30)))
        await s.until(lambda: size(f"{path}.main") > 2)
        s.probe.between = True
        assert (await task).status == 200 and size(f"{path}.main") == 30  # busy the whole time, never stopped
        assert s.service.yielder.paused == {}


# spec: ANYM-691
def test_yield_json_enabled_true_is_the_only_way_to_turn_it_on(tmp_path):
    path = tmp_path / "yield.json"
    path.write_text('{"enabled": true}')
    path.chmod(0o600)
    assert load_yield_settings(path).enabled is True
    path.write_text('{"max_pause_seconds": 60}')  # other settings alone do not switch it on
    assert load_yield_settings(path).enabled is False


# spec: ANYM-691
async def test_health_is_unchanged(env):
    async with env.running(**yielding(env)) as s:
        reply = await s.get("/health", key=None)
        assert set(reply.json()) == {"status", "queue", "admission"}


# --- the canary ----------------------------------------------------------------------------------------------------------
# spec: ANYM-691
def test_canary_sends_its_read_timeout_as_the_request_deadline(monkeypatch):
    seen = {}

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout):
        seen.update({k.lower(): v for k, v in request.header_items()}, timeout=timeout)
        return Reply()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    canary.post("http://x/v1", KEY, b"{}", "application/json", timeout=900)
    assert seen["x-request-deadline-seconds"] == "900" and seen["timeout"] == 900
