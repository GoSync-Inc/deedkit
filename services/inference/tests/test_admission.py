import asyncio
import json
import os
import time

import pytest
from deedkit_inference.config import Config
from deedkit_inference.engines import fake
from deedkit_inference.probe import SystemProbe, parse_busy, read_busy_file
from engine_support import engines, vis
from support import chat_body, fk

pytestmark = pytest.mark.anyio


def job(label, seconds=0.05, **more):
    return chat_body(image=fk(label=label, sleep=seconds, estimate=seconds, **more))


def audio(label, seconds=0.05, **more):
    return fk(label=label, sleep=seconds, estimate=seconds, **more)


def started():
    return [e[1] for e in fake.EVENTS if e[0] == "start"]


# spec: ANYM-683
def test_available_memory_counts_free_inactive_and_speculative_pages(monkeypatch):
    """The mini at 08:55 on 11-02: free 4.05 GiB, inactive 3.42, speculative 1.99. Without the speculative pages the
    figure read 7.47, under the 7.5 GB a cold vision start needs, so vision was refused `memory_low` at such moments
    (13% of the idle-service samples of 07:49-08:49 were below 7.5); the research guard and scripts/remote-test.sh
    both count them."""
    import subprocess
    import sys

    vm_stat = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
               "Pages free:                             265294.\n"
               "Pages active:                           297958.\n"
               "Pages inactive:                         224065.\n"
               "Pages speculative:                      130216.\n"
               "Pages wired down:                       118464.\n")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=vm_stat))
    pages = 265294 + 224065 + 130216
    assert SystemProbe._read_available() == pytest.approx(pages * 16384 / 1024**3)
    assert SystemProbe._read_available() > 7.5 + 1.0  # a cold vision start (4.5 + 3.0) is admitted with room to spare
    # An unreadable answer fails closed.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="garbage"))
    assert SystemProbe._read_available() == 0.0


# spec: ANYM-682
async def test_each_admission_rule_refuses_with_its_own_reason(env):
    big = {"vision-default": {"phases": [{"name": "vlm", "slot": "gpu", "resident_gb": 7.0}]}}
    cases = [
        ("busy_between", lambda p: setattr(p, "between", True), None),
        ("busy_deedkit_ci", lambda p: setattr(p, "ci", True), None),
        ("load_high", lambda p: setattr(p, "load", 8.5), None),
        ("memory_low", lambda p: setattr(p, "available", 4.9), None),  # 2.0 GB phase + 3 GB reserve = 5.0
        ("resident_cap", lambda p: None, big),  # a 7 GB phase never fits the 6 GB cap
    ]
    for reason, tweak, aliases in cases:
        async with env.running(aliases=aliases) as s:
            tweak(s.probe)
            reply = await s.chat(job("x"))
            assert reply.status == 503 and reply.code == "admission_closed", reason
            assert reply.headers["x-admission-reason"] == reason
            assert int(reply.headers["retry-after"]) >= 1 and reason in reply.json()["error"]["message"]
    assert started() == []
    async with env.running() as s:
        s.probe.available = 5.1
        assert (await s.chat(job("fits"))).status == 200
        s.probe.load = 8.0  # at the cap is not above it
        assert (await s.chat(job("at_cap"))).status == 200
        s.probe.between = True
        assert (await s.get("/health", key=None)).json()["admission"] == "closed"
        s.probe.between = False
        assert (await s.get("/health", key=None)).json()["admission"] == "open"
    # The load cap can be set per slot (ANYM-684 criterion 10).
    async with env.running(slot_max_load_1m={"gpu": 2.0}) as s:
        s.probe.load = 3.0
        gpu = await s.chat(job("gpu"))
        assert gpu.status == 503 and gpu.headers["x-admission-reason"] == "load_high"
        assert (await s.transcribe(audio("cpu"))).status == 200
    # A cold phase is charged its full resident_gb plus the reserve; a warm worker already loaded is charged the
    # reserve only (its memory is spent, see test_a_warm_engine_is_charged_the_reserve_only...).
    warm = {"vision-default": {"engine_options": {"persistent": True}}}
    async with env.running(aliases=warm) as s:
        assert (await s.chat(job("first"))).status == 200
        assert list(s.service.admission.loaded) == [("vision-default", "vlm")]  # idle, still in the book
        s.probe.available = 2.9  # below the 3 GB reserve alone
        again = await s.chat(job("again"))
        assert again.status == 503 and again.headers["x-admission-reason"] == "memory_low"
    # An engine whose child exited is gone from the book, and is charged as a cold start.
    async with env.running() as s:
        assert (await s.chat(job("first"))).status == 200
        assert s.service.admission.loaded == {}
        s.probe.available = 3.5
        again = await s.chat(job("again"))
        assert again.status == 503 and again.headers["x-admission-reason"] == "memory_low"


# spec: ANYM-682
async def test_stale_or_missing_busy_file_is_busy(env):
    async with env.running() as s:
        for problem, reason in (("missing", "busy_file_missing"), ("invalid", "busy_file_invalid")):
            s.probe.problem = problem
            reply = await s.chat(job("x"))
            assert reply.status == 503 and reply.headers["x-admission-reason"] == reason
        s.probe.problem = None
        s.probe.busy_age = 31.0
        assert (await s.chat(job("old"))).headers["x-admission-reason"] == "busy_file_stale"
        s.probe.busy_age = -60.0  # from the future
        assert (await s.chat(job("future"))).headers["x-admission-reason"] == "busy_file_stale"
        s.probe.busy_age = 5.0
        assert (await s.chat(job("fresh"))).status == 200
        assert (await s.get("/health", key=None)).json()["admission"] == "open"
        s.probe.problem = "missing"
        assert (await s.get("/health", key=None)).json()["admission"] == "closed"
    # The real reader, on real files.
    path = env.tmp / "busy.json"
    assert read_busy_file(path).problem == "missing"
    for text in ("", "{", "[]", '{"ts": "1", "between": false, "deedkit_ci": false}',
                 '{"ts": 1, "between": 0, "deedkit_ci": false}', '{"ts": 1, "between": false}',
                 '{"ts": true, "between": false, "deedkit_ci": false}',
                 '{"ts": NaN, "between": false, "deedkit_ci": false}',
                 '{"ts": Infinity, "between": false, "deedkit_ci": false}',
                 '{"ts": -Infinity, "between": false, "deedkit_ci": false}'):
        path.write_text(text)
        assert read_busy_file(path).problem == "invalid", text
    path.write_text(json.dumps({"ts": 1767225600, "between": True, "deedkit_ci": False}))
    state = SystemProbe(str(path)).read_busy()
    assert (state.problem, state.ts, state.between, state.deedkit_ci) == (None, 1767225600.0, True, False)
    assert parse_busy(b'{"ts": 5.5, "between": false, "deedkit_ci": true}').deedkit_ci is True
    link = env.tmp / "link.json"
    os.symlink(path, link)
    assert read_busy_file(link).problem == "invalid"  # a symlink is not followed
    # A field a later writer adds does not make the file unreadable.
    assert parse_busy(b'{"ts": 1, "between": false, "deedkit_ci": false, "x": 1}').problem is None


# spec: ANYM-682
async def test_a_warm_engine_is_charged_the_reserve_only_and_holds_nothing_back_from_others(env):
    """Live install 11-02: with the 4.9 GB vision worker loaded the free memory reads about 8 GB, and the next vision
    request was refused for needing 4.9 + 3 GB again. A loaded engine's memory is already spent."""
    qwen = {"vision-default": {"engine_options": {"persistent": True}, "phases": [{"name": "vlm", "slot": "gpu", "resident_gb": 4.9}]}}
    async with env.running(aliases=qwen) as s:
        assert (await s.chat(job("cold"))).status == 200  # a cold start needs 7.9 GB, and had it
        assert s.service.admission.warm(("vision-default", "vlm"))
        s.probe.available = 3.4  # the reserve only: 3.0
        assert (await s.chat(job("warm"))).status == 200
        s.probe.available = 2.9
        refused = await s.chat(job("tight"))
        assert refused.status == 503 and refused.headers["x-admission-reason"] == "memory_low"
        assert started() == ["cold", "warm"]
    fake.EVENTS.clear()
    # The same figure for a cold start is refused: 3.4 < 4.9 + 3.
    async with env.running(aliases={"vision-default": {"phases": [{"name": "vlm", "slot": "gpu", "resident_gb": 4.9}]}}) as s:
        s.probe.available = 3.4
        cold = await s.chat(job("cold"))
        assert cold.status == 503 and cold.headers["x-admission-reason"] == "memory_low"
    fake.EVENTS.clear()
    # A warm engine that is running holds back nothing from a cold start beside it: its memory is in the free figure.
    beside = {"vision-default": {"engine_options": {"persistent": True}, "phases": [{"name": "vlm", "slot": "gpu", "resident_gb": 2.0}]},
              "audio-default": {"phases": [{"name": "stt", "slot": "cpu", "resident_gb": 2.0}]}}
    async with env.running(aliases=beside) as s:
        assert (await s.chat(job("warm_up"))).status == 200
        s.probe.available = 6.0  # 5.0 needed for the cold audio phase, with the vision worker already counted in
        gpu = s.spawn(s.chat(job("vision", 1.0)))
        await s.until(lambda: s.service.scheduler.active() == 1)
        cpu = s.spawn(s.transcribe(audio("audio", 0.2)))
        await s.until(lambda: s.service.scheduler.active() == 2, timeout=3)  # together, not one after the other
        assert (await gpu).status == 200 and (await cpu).status == 200
    # Rule (b) counts a warm engine once: 2.5 loaded plus the same 2.5 again is not 5.0 against a 4.0 cap.
    fake.EVENTS.clear()
    tight = {"resident_cap_gb": 4.0, "aliases": {"vision-default": {"engine_options": {"persistent": True},
             "phases": [{"name": "vlm", "slot": "gpu", "resident_gb": 2.5}]}}}
    async with env.running(**tight) as s:
        assert (await s.chat(job("first"))).status == 200
        assert (await s.chat(job("second"))).status == 200
        assert not any(e[0] == "unload" for e in fake.EVENTS)  # nothing evicted to make room for itself
        assert s.service.admission.resident_total() == 2.5


# spec: ANYM-682
async def test_idle_engine_is_evicted_for_room_and_running_one_is_not(env):
    keep = {"persistent": True}  # warm workers: they stay loaded, and so in the book, between jobs
    tight = {"resident_cap_gb": 4.0, "aliases": {
        "vision-default": {"engine_options": keep, "phases": [{"name": "vlm", "slot": "gpu", "resident_gb": 2.5}]},
        "audio-default": {"engine_options": keep, "phases": [{"name": "stt", "slot": "cpu", "resident_gb": 2.0}]}}}
    # An idle loaded engine is evicted to make room.
    async with env.running(**tight) as s:
        assert (await s.chat(job("vision"))).status == 200
        assert list(s.service.admission.loaded) == [("vision-default", "vlm")]
        assert (await s.transcribe(audio("audio"))).status == 200
        assert ("unload", "vlm", -1, "-") in fake.EVENTS
        assert list(s.service.admission.loaded) == [("audio-default", "stt")]
    fake.EVENTS.clear()
    # A running one is not: the new job waits in the queue, and is not refused.
    async with env.running(**tight) as s:
        running = s.spawn(s.chat(job("running", 0.6)))
        await s.until(lambda: s.service.scheduler.active() == 1)
        waiting = s.spawn(s.transcribe(audio("waiting")))
        await s.until(lambda: s.service.scheduler.depth() == 1)
        assert fake.EVENTS == [("start", "running", 0, "gpu")]  # still waiting: the room is taken
        assert (await running).status == 200  # never killed for the sake of admission
        assert (await waiting).status == 200
    names = [(e[0], e[1]) for e in fake.EVENTS]
    assert names.index(("end", "running")) < names.index(("unload", "vlm")) < names.index(("start", "waiting"))


# spec: ANYM-682
async def test_admission_is_checked_when_a_job_leaves_the_queue(env):
    async with env.running() as s:
        first = s.spawn(s.chat(job("first", 0.5)))
        await s.until(lambda: s.service.scheduler.active() == 1)
        second = s.spawn(s.chat(job("second")))  # arrives while admission is open
        await s.until(lambda: s.service.scheduler.depth() == 1)
        s.probe.between = True  # Between starts while the second job waits
        refused = await second
        assert refused.status == 503 and refused.code == "admission_closed"
        assert refused.headers["x-admission-reason"] == "busy_between"
        assert (await first).status == 200  # the running job is not killed
        assert started() == ["first"]
        # And the same for a load spike.
        s.probe.between = False
        third = s.spawn(s.chat(job("third", 0.4)))
        await s.until(lambda: s.service.scheduler.active() == 1)
        fourth = s.spawn(s.chat(job("fourth")))
        await s.until(lambda: s.service.scheduler.depth() == 1)
        s.probe.load = 20.0
        assert (await fourth).headers["x-admission-reason"] == "load_high"
        assert (await third).status == 200


# spec: ANYM-682
async def test_children_over_the_hard_cap_lose_the_largest(env):
    pids = {"big": env.tmp / "big.pid", "small": env.tmp / "small.pid"}

    def read(path):
        parts = path.read_text().split() if path.exists() else []
        return [int(p) for p in parts] if len(parts) == 2 else None

    async with env.running() as s:
        sizes = {}
        s.probe.rss_fn = lambda pgid: sizes.get(pgid, 0.0)
        big = s.spawn(s.chat(job("big", 2.0, pidfile=str(pids["big"]))))
        small = s.spawn(s.transcribe(audio("small", 1.0, pidfile=str(pids["small"]))))
        await s.until(lambda: all(read(p) for p in pids.values()))
        big_pid, big_grandchild = read(pids["big"])
        small_pid, _ = read(pids["small"])
        sizes.update({big_pid: 6.0, small_pid: 2.0})  # 8 GB together, over the 7.5 GB hard cap
        killed = await big
        assert killed.status == 502 and killed.code == "engine_failed"
        assert (await small).status == 200  # the smaller child lives
    for pid in (big_pid, big_grandchild):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            raise AssertionError(f"process {pid} survived")


# spec: ANYM-682
async def test_concurrent_cold_starts_do_not_spend_the_same_headroom(env):
    two_gb = {"audio-default": {"phases": [{"name": "stt", "slot": "cpu", "resident_gb": 2.0}]}}
    # 6 GB free: one 2 GB start fits (5 GB needed), a second at the same time would not (6 - 2 < 5). The first
    # one's memory is held back for as long as it runs, however long its child takes to allocate.
    async with env.running(aliases=two_gb) as s:
        s.probe.available = 6.0
        gpu = s.spawn(s.chat(job("gpu", 1.5)))
        await s.until(lambda: s.service.scheduler.active() == 1)
        cpu = s.spawn(s.transcribe(audio("cpu")))
        await s.until(lambda: s.service.scheduler.depth() == 1)
        await asyncio.sleep(1.0)  # far longer than any fixed settle time would be
        assert started() == ["gpu"]  # waiting, not refused, and not started on a timer
        assert (await cpu).status == 200 and (await gpu).status == 200
    names = [(e[0], e[1]) for e in fake.EVENTS]
    assert names.index(("end", "gpu")) < names.index(("start", "cpu"))
    # A drop of the free memory is not taken as the first child's allocation: the probe cannot tell it from another
    # process's (here 2 GB go to something else), so the reservation stays until the phase ends.
    fake.EVENTS.clear()
    async with env.running(aliases=two_gb) as s:
        s.probe.available = 7.0
        gpu = s.spawn(s.chat(job("gpu", 1.0)))
        await s.until(lambda: s.service.scheduler.active() == 1)
        s.probe.available = 5.0
        cpu = s.spawn(s.transcribe(audio("cpu")))
        await s.until(lambda: s.service.scheduler.depth() == 1)
        await asyncio.sleep(0.4)
        assert started() == ["gpu"]  # 5 - 2 < 5: still waiting
        assert (await cpu).status == 200 and (await gpu).status == 200
    names = [(e[0], e[1]) for e in fake.EVENTS]
    assert names.index(("end", "gpu")) < names.index(("start", "cpu"))


# spec: ANYM-682
async def test_a_slow_probe_cannot_carry_a_job_past_its_deadline(env):
    async with env.running() as s:
        calls = []

        def slow_second_read():
            calls.append(1)
            if len(calls) == 2:  # the check at the moment the slot would be granted
                time.sleep(0.8)

        s.probe.available_hook = slow_second_read
        reply = await s.chat(job("late", 0.05), headers={"X-Request-Deadline-Seconds": "0.8"})
        assert reply.status == 503 and reply.code == "slot_busy"
        assert started() == []  # the slot was not granted after the time was over
        # All memory reads happen before the deadline check: a granted job costs exactly the two reads of its
        # arrival check and its grant check, none after the grant.
        calls.clear()
        s.probe.available_hook = lambda: calls.append(1)
        assert (await s.chat(job("fine", 0.05), headers={"X-Request-Deadline-Seconds": "5"})).status == 200
        assert len(calls) == 2, calls


FLOOR = {"admit_min_available_gb": 7.0}


# spec: ANYM-682
async def test_the_admission_floor_refuses_below_seven_gb_and_admits_at_it(env):
    # The busy rule now lets a job in while Between is light, so the memory is the guard: free plus inactive plus
    # speculative memory must be at least admit_min_available_gb (7 GB shipped) at admit. The per-phase rule alone would
    # let a 2 GB phase in at 5.0 GB.
    shipped = Config.model_validate({k: v for k, v in env.config_dict().items() if k != "admit_min_available_gb"})
    assert shipped.admit_min_available_gb == 7.0
    async with env.running(**FLOOR) as s:
        for free in (5.1, 6.9):
            s.probe.available = free
            reply = await s.chat(job("low"))
            assert reply.status == 503 and reply.headers["x-admission-reason"] == "memory_low", free
            assert (await s.get("/health", key=None)).json()["admission"] == "closed", free
        assert started() == []
        s.probe.available = 7.0  # the floor itself is admitted
        assert (await s.chat(job("at_floor"))).status == 200
        assert (await s.get("/health", key=None)).json()["admission"] == "open"
    # the undo is the config: 0 switches the floor off and the per-phase rules (here 2.0 + 3.0 GB) are all that is left
    async with env.running(admit_min_available_gb=0.0) as s:
        s.probe.available = 5.1
        assert (await s.chat(job("floor_off"))).status == 200
    with pytest.raises(Exception):
        Config.model_validate({**env.config_dict(), "admit_min_available_gb": -1})


# spec: ANYM-682
async def test_the_admission_floor_fails_closed_when_memory_cannot_be_read(env):
    # SystemProbe returns 0.0 for a vm_stat that failed (test_available_memory_counts_...): zero never opens the floor,
    # not even with an engine loaded whose memory would be added to it
    warm = {"vision-default": {"engine_options": {"persistent": True}, "phases": [{"name": "vlm", "slot": "gpu", "resident_gb": 8.0}]}}
    async with env.running(aliases=warm, **FLOOR, resident_cap_gb=9.0, resident_hard_cap_gb=9.0) as s:
        assert (await s.chat(job("first"))).status == 200
        assert s.service.admission.resident_total() == 8.0
        s.probe.available = 0.0
        reply = await s.chat(job("unreadable"))
        assert reply.status == 503 and reply.headers["x-admission-reason"] == "memory_low"
        assert (await s.get("/health", key=None)).json()["admission"] == "closed"


# spec: ANYM-682
async def test_the_floor_credits_what_the_engines_measure_not_what_is_configured(env):
    # A warm worker's memory is in use and out of the free figure (the live service showed 6.7 GB median with the vision
    # worker loaded). The floor reads what Between would see without us: free memory plus what the supervisor measured
    # of our workers. The configured resident_gb has headroom in it (4.5 charged for a worker that holds 3.84), and a
    # reservation that nothing measured has allocated nothing: neither is credited.
    async with env.running(**engines(env), **FLOOR) as s:  # the shipped vision alias: a warm worker, 4.5 GB charged
        admission, key = s.service.admission, ("vision-default", "vlm")
        assert (await s.chat(vis())).status == 200
        assert list(admission.loaded) == [key] and admission.resident_total() == 4.5  # idle, still in the book
        await s.until(lambda: key in admission.measured)  # the supervisor ticked: the fake probe measures 0.0
        s.probe.available = 6.9  # the configured 4.5 would credit it up to 11.4: nothing measured, nothing credited
        reply = await s.chat(vis())
        assert reply.status == 503 and reply.headers["x-admission-reason"] == "memory_low"
        s.probe.rss_fn = lambda pgid: 3.84  # the worker really holds 3.84 GB
        await s.until(lambda: admission.credit() == 3.84)
        s.probe.available = 3.2  # 3.2 + 3.84 = 7.04: the floor and the per-phase reserve (3.0) both pass
        assert (await s.chat(vis())).status == 200
        s.probe.available = 3.1  # 3.1 + 3.84 = 6.94 (4.5 configured would have said 7.6): only the floor refuses
        reply = await s.chat(vis())
        assert reply.status == 503 and reply.headers["x-admission-reason"] == "memory_low"
        s.probe.rss_fn = lambda pgid: 6.0  # above the configured figure is not credited above it (and within the supervisor's share limit)
        await s.until(lambda: admission.measured.get(key) == 6.0)
        assert admission.credit() == 4.5
        admission.drop(key)  # a dropped engine leaves the book and the credit with it
        assert admission.credit() == 0.0 and key not in admission.measured


# spec: ANYM-682
async def test_the_floor_is_checked_again_when_the_phase_starts_and_costs_no_extra_read(env):
    async with env.running(**FLOOR) as s:
        admission, key = s.service.admission, ("vision-default", "vlm")
        s.probe.available = 6.5
        assert admission.check(key, 2.0, "gpu").reason == "memory_low"  # the check at the moment the slot would be granted
        s.probe.available = 7.5
        assert admission.check(key, 2.0, "gpu").action == "go"
        # the arrival check and the grant check read the memory once each, floor or not (the deadline contract)
        calls = []
        s.probe.available = 16.0
        s.probe.available_hook = lambda: calls.append(1)
        assert (await s.chat(job("fine", 0.05), headers={"X-Request-Deadline-Seconds": "5"})).status == 200
        assert len(calls) == 2, calls
