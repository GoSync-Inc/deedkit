import asyncio
import json
import time

import pytest
from deedkit_inference.config import Config
from deedkit_inference.engines import fake
from pydantic import ValidationError
from support import chat_body, fk, principal

pytestmark = pytest.mark.anyio


def slow(label, seconds=0.3, estimate=None, **more):
    return chat_body(image=fk(label=label, sleep=seconds, estimate=seconds if estimate is None else estimate, **more))


def audio(label, seconds=0.3, **more):
    return fk(label=label, sleep=seconds, estimate=seconds, **more)


def active_profile(events):
    """Per-slot and total activity after each event of the fake engine."""
    active, peak_slot, peak_total, overlap = {"gpu": 0, "cpu": 0}, {"gpu": 0, "cpu": 0}, 0, False
    for kind, _label, _index, slot in events:
        if kind == "start":
            active[slot] += 1
        elif kind == "end":
            active[slot] -= 1
        else:
            continue
        for name in active:
            peak_slot[name] = max(peak_slot[name], active[name])
        peak_total = max(peak_total, sum(active.values()))
        overlap = overlap or (active["gpu"] and active["cpu"])
    return peak_slot, peak_total, bool(overlap)


# spec: ANYM-682
async def test_full_queue_is_503_with_retry_after(env):
    async with env.running(queue_size=2) as s:
        running = s.spawn(s.chat(slow("A")))
        await s.until(lambda: s.service.scheduler.active() == 1)
        queued = [await s.submit(s.chat(slow(name))) for name in "BC"]
        await s.until(lambda: s.service.scheduler.depth() == 2)
        full = await s.chat(slow("D"))
        assert full.status == 503 and full.code == "queue_full"
        assert int(full.headers["retry-after"]) >= 1
        health = (await s.get("/health", key=None)).json()
        assert health["queue"] == {"depth": 2, "active": 1, "capacity": 2}
        for task in (running, *queued):
            assert (await task).status == 200
        assert [e[1] for e in fake.EVENTS if e[0] == "start"] == ["A", "B", "C"]  # arrival order
        assert (await s.chat(slow("E", 0.01))).status == 200  # recovers


# spec: ANYM-682
async def test_gpu_and_cpu_jobs_overlap_but_never_two_per_slot(env):
    async with env.running() as s:
        tasks = [await s.submit(s.chat(slow(f"g{i}"))) for i in range(2)]
        tasks += [await s.submit(s.transcribe(audio(f"c{i}"))) for i in range(2)]
        for task in tasks:
            assert (await task).status == 200
    peak_slot, peak_total, overlap = active_profile(fake.EVENTS)
    assert peak_slot == {"gpu": 1, "cpu": 1}
    assert peak_total == 2 and overlap
    gpu_order = [e[1] for e in fake.EVENTS if e[0] == "start" and e[3] == "gpu"]
    assert gpu_order == ["g0", "g1"]


# spec: ANYM-682
async def test_phases_take_their_own_slots_in_order(env):
    async with env.running() as s:
        tasks = [await s.submit(s.transcribe(audio(name, 0.25), model="audio-two-phase")) for name in "AB"]
        for task in tasks:
            assert (await task).status == 200
    position = {(kind, label, index): n for n, (kind, label, index, _slot) in enumerate(fake.EVENTS)}
    slots = {(label, index): slot for kind, label, index, slot in fake.EVENTS if kind == "start"}
    for label in "AB":
        assert slots[(label, 0)] == "cpu" and slots[(label, 1)] == "gpu"
        order = [position[("start", label, 0)], position[("end", label, 0)],
                 position[("start", label, 1)], position[("end", label, 1)]]
        assert order == sorted(order)
    # The pipeline: B's first phase runs while A is in its second, and each slot stays in arrival order.
    assert position[("start", "B", 0)] < position[("end", "A", 1)]
    assert position[("start", "A", 0)] < position[("start", "B", 0)]
    assert position[("start", "A", 1)] < position[("start", "B", 1)]
    peak_slot, _, overlap = active_profile(fake.EVENTS)
    assert peak_slot == {"gpu": 1, "cpu": 1} and overlap


# spec: ANYM-682
async def test_job_behind_a_long_one_is_refused_with_slot_busy_not_left_waiting(env):
    async with env.running(max_wait_seconds=1) as s:
        first = s.spawn(s.chat(slow("long", 0.6, estimate=5)))
        await s.until(lambda: s.service.scheduler.active() == 1)
        started = time.monotonic()
        behind = await s.chat(slow("behind", 0.01))
        assert time.monotonic() - started < 0.4  # at once, not after waiting
        assert behind.status == 503 and behind.code == "slot_busy"
        assert int(behind.headers["retry-after"]) >= 1
        assert s.service.scheduler.depth() == 0
        assert (await s.transcribe(audio("other", 0.01))).status == 200  # the other slot is free
        assert (await first).status == 200
        assert [e[1] for e in fake.EVENTS if e[0] == "start"] == ["long", "other"]


# spec: ANYM-682
async def test_queued_job_past_its_deadline_is_dropped_with_503_before_it_starts(env):
    async with env.running(max_wait_seconds=0.4) as s:
        # The job ahead overruns its estimate (0.1 s said, 1.2 s taken).
        ahead = s.spawn(s.chat(slow("ahead", 1.2, estimate=0.1)))
        await s.until(lambda: s.service.scheduler.active() == 1)
        started = time.monotonic()
        queued = await s.chat(slow("queued", 0.01, estimate=0.1))
        waited = time.monotonic() - started
        assert queued.status == 503 and queued.code == "slot_busy"
        assert 0.3 < waited < 1.0, waited  # left at its bound, long before the job ahead ended
        assert s.service.scheduler.depth() == 0
        assert (await ahead).status == 200
        # The client's own deadline bounds the wait too, below max_wait_seconds.
        ahead = s.spawn(s.chat(slow("ahead2", 1.2, estimate=0.05)))
        await s.until(lambda: s.service.scheduler.active() == 1)
        started = time.monotonic()
        queued = await s.chat(slow("queued2", 0.01, estimate=0.1), headers={"X-Request-Deadline-Seconds": "0.3"})
        waited = time.monotonic() - started
        assert queued.code == "slot_busy" and 0.1 < waited < 0.38, waited  # 0.3 s deadline minus 0.1 s estimate
        assert (await ahead).status == 200
    assert [e[1] for e in fake.EVENTS if e[0] == "start"] == ["ahead", "ahead2"]


# spec: ANYM-682
async def test_deadline_header_shorter_than_the_estimate_is_refused_up_front(env):
    async with env.running() as s:
        reply = await s.chat(slow("big", 0.01, estimate=5), headers={"X-Request-Deadline-Seconds": "1"})
        assert reply.status == 422 and reply.code == "deadline_too_short"
        # Wait plus own estimate over the deadline: refused at once as slot_busy.
        ahead = s.spawn(s.chat(slow("ahead", 0.6, estimate=2)))
        await s.until(lambda: s.service.scheduler.active() == 1)
        reply = await s.chat(slow("late", 0.01, estimate=1), headers={"X-Request-Deadline-Seconds": "2.5"})
        assert reply.status == 503 and reply.code == "slot_busy"
        assert (await ahead).status == 200
        for bad in ("abc", "0", "-1", "nan", "inf", "100000"):
            reply = await s.chat(slow("x", 0.01), headers={"X-Request-Deadline-Seconds": bad})
            assert reply.status == 422 and reply.code == "invalid_request", bad
        assert (await s.chat(slow("fits", 0.01, estimate=1), headers={"X-Request-Deadline-Seconds": "5"})).status == 200
    assert [e[1] for e in fake.EVENTS if e[0] == "start"] == ["ahead", "fits"]


# spec: ANYM-682
def test_fake_engine_refused_in_production_config(env, monkeypatch):
    with pytest.raises(ValidationError, match="fake_engine_in_production_config"):
        env.config(mode="production")
    real = {name: {**spec, "engine": "mlx"} for name, spec in env.config_dict()["aliases"].items()}
    with pytest.raises(ValidationError, match="test_probe_in_production_config"):
        Config.model_validate({**env.config_dict(mode="production", test_probe={"available_gb": 1}),
                               "aliases": real})
    monkeypatch.delenv("DEEDKIT_INFERENCE_TESTING")
    with pytest.raises(ValidationError, match="test_mode_needs_testing_env"):
        env.config()
    # A production config with a real engine name is a valid config (the engine itself is ANYM-683's).
    assert Config.model_validate({**env.config_dict(mode="production"), "aliases": real}).mode == "production"
    # And the service refuses an engine name it does not know.
    monkeypatch.setenv("DEEDKIT_INFERENCE_TESTING", "1")
    from deedkit_inference.app import create_app
    from deedkit_inference.errors import ConfigError

    env.write_policy()
    with pytest.raises(ConfigError, match="unknown_engine"):
        create_app(env.config(aliases={"vision-default": {"engine": "mlx"}}))


# spec: ANYM-682
def test_defaults_match_the_card(env):
    cfg = env.config()
    assert (cfg.queue_size, cfg.max_wait_seconds, cfg.resident_cap_gb, cfg.resident_hard_cap_gb) == (16, 50, 6.0, 7.5)
    default = Config.model_validate({k: v for k, v in env.config_dict().items() if k != "max_wait_seconds"})
    assert default.max_wait_seconds == 240 and default.max_load_1m == 8 and default.min_free_gb == 3.0
    assert default.max_inflight_upload_bytes == 128 * 1024**2 and default.upload_seconds == 120
    assert default.work_dir_max_bytes == 512 * 1024**2 and default.drain_seconds == 900
    assert default.audio_body_max_bytes == 32 * 1024**2 and default.chat_body_max_bytes == 44 * 1024**2
    assert default.port == 8090 and default.bind == "127.0.0.1"


# spec: ANYM-682
async def test_a_burst_never_puts_more_jobs_in_the_queue_than_its_bound(env):
    env.write_policy([principal(concurrent=16)], 1)
    async with env.running(queue_size=1) as s:
        running = s.spawn(s.chat(slow("running", 0.6)))
        await s.until(lambda: s.service.scheduler.active() == 1)
        burst = await asyncio.gather(*[s.chat(slow(f"b{i}", 0.05)) for i in range(4)])
        statuses = sorted(r.status for r in burst)
        assert statuses == [200, 503, 503, 503], statuses  # one waits, the rest are refused at once
        assert {r.code for r in burst if r.status == 503} == {"queue_full"}
        assert s.service.scheduler.depth() <= 1
        assert (await running).status == 200


# spec: ANYM-682
async def test_a_job_that_must_wait_for_resident_room_counts_against_the_queue(env):
    tight = {"resident_cap_gb": 4.0, "aliases": {
        "vision-default": {"phases": [{"name": "vlm", "slot": "gpu", "resident_gb": 2.5}]},
        "audio-default": {"phases": [{"name": "stt", "slot": "cpu", "resident_gb": 2.0}]}}}
    async with env.running(queue_size=1, **tight) as s:
        running = s.spawn(s.chat(slow("vision", 0.8)))
        await s.until(lambda: s.service.scheduler.active() == 1)
        first = await s.submit(s.transcribe(audio("first")))  # the cpu slot is idle, resident room is not
        assert s.service.scheduler.depth() == 1
        second = await s.transcribe(audio("second"))
        assert second.status == 503 and second.code == "queue_full"
        assert (await first).status == 200 and (await running).status == 200


# spec: ANYM-682
async def test_a_phase_transition_cannot_push_the_queue_past_its_bound(env):
    env.write_policy([principal(concurrent=16)], 1)
    async with env.running(queue_size=1) as s:
        depth_seen = []

        async def watch():
            while True:
                depth_seen.append(s.service.scheduler.depth())
                await asyncio.sleep(0.005)

        watcher = asyncio.create_task(watch())
        running = s.spawn(s.chat(slow("running", 0.8)))
        await s.until(lambda: s.service.scheduler.active() == 1)
        waiter = await s.submit(s.chat(slow("waiter", 0.05)))  # fills the one place of the gpu queue
        assert s.service.scheduler.depth() == 1
        # A two-phase job would start on the idle cpu slot and then join the full gpu queue: refused up front.
        two = await s.transcribe(audio("two", 0.05), model="audio-two-phase")
        assert two.status == 503 and two.code == "queue_full"
        assert (await running).status == 200 and (await waiter).status == 200
        # With room for it, the same job passes through both phases.
        assert (await s.transcribe(audio("two", 0.05), model="audio-two-phase")).status == 200
        watcher.cancel()
        assert max(depth_seen) <= 1, depth_seen


# spec: ANYM-682
async def test_the_deadline_clock_starts_when_the_request_arrives(env):
    async with env.running() as s:
        # An upload that takes 0.4 s of a 0.5 s deadline leaves less than the 0.3 s job needs.
        body = json.dumps(chat_body(image=fk(label="late", estimate=0.3))).encode()
        half = len(body) // 2
        reply = await s.request("POST", "/v1/chat/completions", chunks=[(0.2, body[:half]), (0.2, body[half:])],
                                content_length=len(body),
                                headers={"content-type": "application/json", "X-Request-Deadline-Seconds": "0.5"})
        assert reply.status == 422 and reply.code == "deadline_too_short"
        assert started() == []
        # A job whose first phase overran its budget gets no slot for the second phase.
        late = audio("late2", 0.5)
        reply = await s.transcribe(late, model="audio-two-phase", headers={"X-Request-Deadline-Seconds": "0.8"})
        assert reply.status == 503 and reply.code == "slot_busy"
        assert [(e[0], e[2]) for e in fake.EVENTS if e[0] == "start"] == [("start", 0)]


def started():
    return [e[1] for e in fake.EVENTS if e[0] == "start"]
