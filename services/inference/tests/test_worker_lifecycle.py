import asyncio
import json
import os
import time

import pytest
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
async def test_child_over_its_memory_share_is_killed(env):
    pidfile = env.tmp / "vlm.pids"
    async with env.running(**engines(env)) as s:
        # 4.9 GB declared: 1.5 times is 7.35 GB. 7.0 GB is tolerated, 8.0 GB is not.
        task = s.spawn(s.chat(vis(mode="ok", sleep=1.0, pidfile=str(pidfile))))
        await s.until(lambda: pids(pidfile))
        worker = pids(pidfile)[0]
        s.probe.rss_fn = lambda pgid: 7.0 if pgid == worker else 0.0
        assert (await task).status == 200
        task = s.spawn(s.chat(vis(mode="hang", pidfile=str(pidfile))))  # the same warm worker takes it
        await s.until(lambda: s.service.scheduler.slots["gpu"].running is not None)
        s.probe.rss_fn = lambda pgid: 8.0 if pgid == worker else 0.0
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
        # Whisper large (2.7 GB) beside the idle Qwen (4.9 GB) is 7.6 GB, over the 6.0 GB cap: the idle one goes first.
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
        # GigaAM (1.6 GB, CPU slot) is a free slot but 4.9 + 1.6 = 6.5 GB is over the 6.0 GB cap, and Qwen is running.
        second = s.spawn(s.transcribe(fk(sleep=0.2), extra_fields={"language": "ru"}))
        await s.until(lambda: scheduler.depth() == 1)
        await asyncio.sleep(0.5)
        assert scheduler.slots["cpu"].running is None and scheduler.depth() == 1  # waiting, not refused
        assert (await first).status == 200 and (await second).status == 200
