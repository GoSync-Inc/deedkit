import asyncio
import json
import os
import signal
import stat
import sys
import threading
import time

import pytest
from deedkit_inference import media
from deedkit_inference.models import verify_phase
from engine_support import engines, vis
from support import fk

pytestmark = pytest.mark.anyio


def pids(path):
    parts = path.read_text().split() if path.exists() else []
    return [int(p) for p in parts]


async def gone(pid, seconds=5.0):
    """The process no longer exists. Async: the event loop must keep running so the service can reap its children."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.02)
    return False


# spec: ANYM-683
async def test_timeout_kills_the_whole_process_group(env):
    pidfile = env.tmp / "hang.pids"
    async with env.running(**engines(env, timeout=5.0)) as s:
        reply = await s.transcribe(fk(mode="hang", pidfile=str(pidfile), grandchild=True),
                                   extra_fields={"language": "ru"})
        assert reply.status == 504 and reply.code == "engine_timeout"
        worker, grandchild = pids(pidfile)
        assert await gone(worker) and await gone(grandchild)
        assert s.service.engines["audio-default"].workers == {}
        assert s.work_entries() == []  # the worker's scratch directory went with it
        # The service goes on: the next job gets a fresh worker.
        assert (await s.transcribe(fk(), extra_fields={"language": "ru"})).status == 200


# spec: ANYM-683
def test_ffmpeg_helper_stays_in_the_workers_process_group(tmp_path):
    """A worker killed by its group takes its decoder with it, and the supervisor's group size includes the decoder."""
    marker, hang_pid = tmp_path / "pgid", tmp_path / "hang.pid"
    tool, hang = tmp_path / "ffmpeg", tmp_path / "hang"
    tool.write_text(f"#!{sys.executable}\nimport os, sys\nopen({str(marker)!r}, 'w').write(str(os.getpgid(0)))\n"
                    "open(sys.argv[-1], 'wb').write(b'0' * 100)\n")
    hang.write_text(f"#!{sys.executable}\nimport os, time\nopen({str(hang_pid)!r}, 'w').write(str(os.getpid()))\n"
                    "time.sleep(600)\n")
    for item in (tool, hang):
        item.chmod(item.stat().st_mode | stat.S_IXUSR)
    assert media.chunk_wav(str(tool), "in.wav", 0.0, 1.0, str(tmp_path / "w.wav"), 10.0)
    assert int(marker.read_text()) == os.getpgrp()  # the caller's group, not a new session
    # A decoder that hangs is still killed and reaped when its own timeout runs out.
    assert media.run_tool([str(hang)], 0.5, own_group=False) == (None, b"")
    with pytest.raises(ProcessLookupError):
        os.kill(int(hang_pid.read_text()), 0)


# spec: ANYM-683
async def test_evicted_worker_stays_counted_until_it_is_reaped(env):
    pidfile = env.tmp / "evict.pids"
    options = {"runtime_options": {"unload_sleep": 1.5}}
    async with env.running(**engines(env, extra_options=options)) as s:
        vision, speech = s.service.engines["vision-default"], s.service.engines["audio-default"]
        assert (await s.chat(vis(pidfile=str(pidfile)))).status == 200
        old = pids(pidfile)[0]
        # Whisper large beside the idle Qwen is over the cap: Qwen is evicted, and its unload takes 1.5 s.
        task = s.spawn(s.transcribe(fk(), extra_fields={"language": "en"}))
        await s.until(lambda: vision.retiring_workers(), timeout=5)
        assert vision.workers == {} and await_alive(old)
        await s.until(lambda: "whisper" in speech.workers, timeout=10)
        assert not await_alive(old)  # the replacement started only after the old process was gone
        assert vision.retiring_workers() == []
        assert (await task).status == 200


def await_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# spec: ANYM-683
async def test_worker_retiring_during_a_probe_is_counted_once(env):
    """The supervisor's probe awaits a thread; an eviction in that gap must not make the same process count twice
    (that sum could cross the hard cap and kill a healthy job)."""
    options = {"runtime_options": {"unload_sleep": 1.5}}
    probing, release, calls = threading.Event(), threading.Event(), []

    def rss(pgid):
        calls.append(pgid)
        if len(calls) == 1:
            probing.set()
            release.wait(10)
        return 1.0

    async with env.running(**engines(env, extra_options=options), supervise_interval_seconds=30) as s:
        vision = s.service.engines["vision-default"]
        assert (await s.chat(vis())).status == 200
        s.probe.rss_fn = rss
        task = asyncio.create_task(s.service.scheduler._measure())
        await s.until(probing.is_set)
        vision.unload("vlm")  # retires while its first probe is still out
        assert len(vision.retiring_workers()) == 1
        release.set()
        sizes = await task
        assert [size for size, _ in sizes] == [1.0] and len(calls) == 1  # once, and the retiring pass did not probe it


# spec: ANYM-682
async def test_a_dead_idle_worker_is_charged_as_a_cold_start_not_as_warm(env):
    """Sol r1 on the live-install fix: a warm worker's discount is for a worker that is loaded. One that died while
    idle leaves the book, and the next phase needs its full memory plus the reserve."""
    pidfile = env.tmp / "dead.pids"
    key = ("vision-default", "vlm")
    async with env.running(**engines(env)) as s:
        assert (await s.chat(vis(pidfile=str(pidfile)))).status == 200
        worker = pids(pidfile)[0]
        assert s.service.admission.warm(key)
        os.kill(worker, signal.SIGKILL)  # the idle worker dies without the scheduler's doing
        assert await gone(worker)
        s.probe.available = 4.0  # enough for the 3 GB reserve alone, not for 4.5 GB more
        refused = await s.chat(vis())
        assert refused.status == 503 and refused.headers["x-admission-reason"] == "memory_low"
        assert key not in s.service.admission.loaded and s.service.admission.resident_total() == 0
        s.probe.available = 9.0
        assert (await s.chat(vis(pidfile=str(pidfile)))).status == 200  # a cold start, as usual
        assert pids(pidfile)[0] != worker


# spec: ANYM-682
async def test_a_warm_worker_lost_between_the_grant_and_the_start_is_asked_for_again_cold(env):
    pidfile = env.tmp / "lost.pids"
    key = ("vision-default", "vlm")
    async with env.running(**engines(env)) as s:
        engine, admission = s.service.engines["vision-default"], s.service.admission
        assert (await s.chat(vis(pidfile=str(pidfile)))).status == 200
        worker = pids(pidfile)[0]
        real = admission.mark_running

        scheduler = s.service.scheduler

        def grant_then_lose(*args):
            real(*args)
            # The supervisor's own path, between the grant and the job's first step: the worker is discarded and,
            # being idle, its book entry and its reservation go with it.
            scheduler._kill_victim(("worker", engine, "vlm", engine.workers["vlm"]), "memory_hard_cap")

        s.probe.available = 4.0  # the reserve alone fits: the grant is a warm one
        admission.mark_running = grant_then_lose
        reply = await s.chat(vis())
        assert reply.status == 503 and reply.code == "admission_closed" and reply.headers["x-admission-reason"] == "worker_lost"
        assert key not in admission.loaded and admission.pending == {}  # nothing is left reserved or warm
        admission.mark_running = real
        assert await gone(worker)
        # The retry is a cold start: at 4 GB it is refused for the memory it needs...
        refused = await s.chat(vis())
        assert refused.status == 503 and refused.headers["x-admission-reason"] == "memory_low"
        # ... and with enough, it holds its full reservation while it runs.
        s.probe.available = 9.0
        retry = s.spawn(s.chat(vis(pidfile=str(pidfile), sleep=0.8)))
        await s.until(lambda: admission.pending.get(key) == 4.5, timeout=5)
        assert (await retry).status == 200


# spec: ANYM-683
async def test_cancelled_preparation_is_finished_and_cleaned_before_the_job_leaves(env, monkeypatch):
    """A timeout during the full re-hash: the thread is told to stop, the job holds its slot until the thread is done,
    and a launch that was finished anyway loses its scratch directory."""
    seen = {}
    real = verify_phase

    def slow(models, store, alias, phase, cancel=None):
        seen["cancel"] = cancel.wait(10)  # blocked in verification until the scheduler cancels it
        paths = real(models, store, alias, phase)  # ... and the preparation completes anyway
        seen["done"] = True
        return paths

    monkeypatch.setattr("deedkit_inference.engines.pinned.verify_phase", slow)
    async with env.running(**engines(env, timeout=0.5)) as s:
        reply = await s.chat(vis())
        assert reply.status == 504 and reply.code == "engine_timeout"
        assert seen == {"cancel": True, "done": True}  # the reply came after the preparation thread had ended
        assert s.work_entries() == [] and s.service.engines["vision-default"].workers == {}
        await s.until(lambda: s.service.scheduler.slots["gpu"].running is None)


# spec: ANYM-683
async def test_child_over_its_memory_share_is_killed(env):
    pidfile = env.tmp / "vlm.pids"
    async with env.running(**engines(env)) as s:
        # 4.5 GB declared: 1.5 times is 6.75 GB. 6.5 GB is tolerated, 7.0 GB is not.
        task = s.spawn(s.chat(vis(mode="ok", sleep=1.0, pidfile=str(pidfile))))
        await s.until(lambda: pids(pidfile))
        worker = pids(pidfile)[0]
        s.probe.rss_fn = lambda pgid: 6.5 if pgid == worker else 0.0
        assert (await task).status == 200
        task = s.spawn(s.chat(vis(mode="hang", pidfile=str(pidfile))))  # the same warm worker takes it
        await s.until(lambda: s.service.scheduler.slots["gpu"].running is not None)
        s.probe.rss_fn = lambda pgid: 7.0 if pgid == worker else 0.0
        reply = await task
        assert reply.status == 502 and reply.code == "engine_failed"
        assert await gone(worker)
        assert s.service.engines["vision-default"].workers == {}
        assert ("vision-default", "vlm") not in s.service.admission.loaded


# spec: ANYM-683
async def test_cache_and_temp_paths_are_inside_the_request_directory(env):
    names = ["HOME", "TMPDIR", "TEMP", "XDG_CACHE_HOME", "HF_HOME", "HF_HUB_CACHE", "TRANSFORMERS_CACHE", "TORCH_HOME",
             "MPLCONFIGDIR", "NUMBA_CACHE_DIR"]
    async with env.running(**engines(env)) as s:
        reply = await s.transcribe(fk(echo_env=[*names, "HF_HUB_OFFLINE", "HF_HUB_DISABLE_TELEMETRY", "DO_NOT_TRACK"]),
                                   extra_fields={"language": "ru"})
        seen = json.loads(reply.json()["text"])
        work = str(env.work) + os.sep
        # A worker outlives one request, so its scratch directory (under the work directory, gone with the worker)
        # stands in for the request directory; never the service's own home or a shared place.
        for name in names:
            assert seen[name].startswith(work + "w-"), (name, seen[name])
        assert len({seen[n][len(work):].split(os.sep)[0] for n in names}) == 1
        assert seen["HF_HUB_OFFLINE"] == "1" and seen["HF_HUB_DISABLE_TELEMETRY"] == "1" and seen["DO_NOT_TRACK"] == "1"


# spec: ANYM-683
async def test_in_memory_caches_are_cleared_on_unload(env):
    marker = env.tmp / "unloaded"
    options = {"runtime_options": {"unload_marker": str(marker)}}
    async with env.running(**engines(env, extra_options=options), keep_alive_seconds=0.6) as s:
        assert (await s.transcribe(fk(), extra_fields={"language": "ru"})).status == 200
        worker = s.service.engines["audio-default"].workers["gigaam"]
        await s.until(lambda: marker.exists(), timeout=8)
        assert marker.read_text() == "cleared:0"  # the runtime emptied its cache before it left
        assert await gone(worker.proc.pid)
        await s.until(lambda: s.work_entries() == [])


# spec: ANYM-683
async def test_idle_engine_unloads_after_keep_alive(env):
    pidfile = env.tmp / "idle.pids"
    async with env.running(**engines(env), keep_alive_seconds=0.4) as s:
        engine = s.service.engines["vision-default"]
        assert (await s.chat(vis(pidfile=str(pidfile)))).status == 200
        first = pids(pidfile)[0]
        assert ("vision-default", "vlm") in s.service.admission.loaded and "vlm" in engine.workers
        await s.until(lambda: "vlm" not in engine.workers and ("vision-default", "vlm") not in s.service.admission.loaded)
        assert await gone(first)
        # The next job starts a new worker (a cold start).
        assert (await s.chat(vis(pidfile=str(pidfile)))).status == 200
        assert pids(pidfile)[0] != first
        # While a worker is warm the same process answers.
        before = pids(pidfile)[0]
        assert (await s.chat(vis(pidfile=str(pidfile)))).status == 200 and pids(pidfile)[0] == before


# spec: ANYM-683
async def test_second_large_model_unloads_the_first_idle_one(env):
    async with env.running(**engines(env)) as s:
        vision, speech = s.service.engines["vision-default"], s.service.engines["audio-default"]
        assert (await s.chat(vis())).status == 200 and "vlm" in vision.workers
        # Whisper large (2.7 GB) beside the idle Qwen (4.5 GB) is 7.2 GB, over the 6.0 GB cap: the idle one goes first.
        reply = await s.transcribe(fk(), extra_fields={"language": "en"})
        assert reply.status == 200 and reply.json()["text"] == "whisper transcript 0"
        assert "vlm" not in vision.workers and "whisper" in speech.workers
        assert ("vision-default", "vlm") not in s.service.admission.loaded
        assert s.service.admission.resident_total() <= s.service.config.resident_cap_gb


# spec: ANYM-683
async def test_pair_over_the_cap_is_serialized(env):
    async with env.running(**engines(env)) as s:
        scheduler = s.service.scheduler
        first = s.spawn(s.chat(vis(sleep=1.0)))
        await s.until(lambda: scheduler.slots["gpu"].running is not None)
        # GigaAM (1.6 GB, CPU slot) is a free slot but 4.5 + 1.6 = 6.1 GB is over the 6.0 GB cap, and Qwen is running.
        second = s.spawn(s.transcribe(fk(sleep=0.2), extra_fields={"language": "ru"}))
        await s.until(lambda: scheduler.depth() == 1)
        await asyncio.sleep(0.5)
        assert scheduler.slots["cpu"].running is None and scheduler.depth() == 1  # waiting, not refused
        assert (await first).status == 200 and (await second).status == 200

