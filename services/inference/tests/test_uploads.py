import asyncio
import json

import pytest
from deedkit_inference.engines import fake
from support import chat_body, fk, multipart

pytestmark = pytest.mark.anyio

MIB = 1024 * 1024
FILE_PREFIX = b'--B\r\nContent-Disposition: form-data; name="file"; filename="a"\r\n\r\n'


def audio_upload(size, parts=3, delay=0.0, label="up"):
    """A valid multipart body of about `size` bytes, split into `parts` chunks with `delay` before each."""
    body, content_type = multipart({"model": "audio-default"}, fk(label=label, estimate=0.01) + b"x" * size)
    step = len(body) // parts + 1
    chunks = [(delay, body[i:i + step]) for i in range(0, len(body), step)]
    return {"chunks": chunks, "content_length": len(body), "headers": {"content-type": content_type}}


async def post_audio(s, **kw):
    return await s.request("POST", "/v1/audio/transcriptions", **kw)


def lazy(n, size=65536):
    return (b"x" * size for _ in range(n))


# spec: ANYM-682
async def test_oversized_body_is_413_without_buffering(env):
    async with env.running(audio_body_max_bytes=MIB, chat_body_max_bytes=MIB) as s:
        # Declared over the cap: refused before a byte is read.
        for path, content_type in (("/v1/audio/transcriptions", "multipart/form-data; boundary=B"),
                                   ("/v1/chat/completions", "application/json")):
            reply = await s.request("POST", path, chunks=list(lazy(32)), content_length=2 * MIB,
                                    headers={"content-type": content_type})
            assert reply.status == 413 and reply.code == "payload_too_large"
            assert reply.stats.receive_calls == 0
        # Declared small, sent large: cut as soon as it passes what it declared, not after the whole stream.
        declared = 200 * 1024
        audio_chunks = [FILE_PREFIX, *lazy(80)]
        reply = await s.request("POST", "/v1/audio/transcriptions", chunks=audio_chunks, content_length=declared,
                                headers={"content-type": "multipart/form-data; boundary=B"})
        assert reply.status == 413 and reply.stats.chunks_delivered <= 5, reply.stats
        reply = await s.request("POST", "/v1/chat/completions", chunks=lazy(80), content_length=declared,
                                headers={"content-type": "application/json"})
        assert reply.status == 413 and reply.stats.chunks_delivered <= 5, reply.stats
        assert s.work_entries() == []
        assert s.service.pool_used == 0 and s.service.workdir.reserved == 0
        assert fake.SPAWNED == []


# spec: ANYM-682
async def test_precheck_rejection_is_422_before_the_engine_starts(env):
    async with env.running() as s:
        reply = await s.transcribe(fk(label="long", duration=14401, estimate=0.01))
        assert reply.status == 422 and reply.code == "audio_too_long"
        reply = await s.chat(chat_body(image=fk(label="huge", megapixels=21, estimate=0.01)))
        assert reply.status == 422 and reply.code == "image_too_large"
        assert fake.SPAWNED == [] and fake.EVENTS == []
        assert s.service.admission.loaded == {} and s.service.scheduler.active() == 0
        assert s.work_entries() == []
        # The limits themselves are allowed (14400 s, 20 MP).
        assert (await s.transcribe(fk(label="edge", duration=14400, estimate=0.01))).status == 200
        assert (await s.chat(chat_body(image=fk(label="edge", megapixels=20, estimate=0.01)))).status == 200


# spec: ANYM-682
async def test_inflight_upload_pool_is_reserved_before_the_body_is_read(env):
    async with env.running(max_inflight_upload_bytes=MIB) as s:
        first = audio_upload(600_000, delay=0.3)
        running = s.spawn(post_audio(s, **first))
        await s.until(lambda: s.service.pool_used == first["content_length"])
        second = audio_upload(600_000)
        refused = await post_audio(s, **second)
        assert refused.status == 503 and refused.code == "upload_capacity"
        assert int(refused.headers["retry-after"]) >= 1
        assert refused.stats.receive_calls == 0  # refused before one body byte was read
        # No Content-Length at all (chunked): 411. Over the per-request cap: 413.
        assert (await post_audio(s, chunks=[b"x"], content_length=None,
                                 headers={"content-type": "multipart/form-data; boundary=B"})).status == 411
        assert (await running).status == 200
        assert s.service.pool_used == 0
        assert (await post_audio(s, **second)).status == 200


# spec: ANYM-682
async def test_slow_upload_is_cut_and_cleaned(env):
    async with env.running(upload_seconds=0.3) as s:
        slow = audio_upload(1000, parts=4, delay=0.2)
        reply = await post_audio(s, **slow)
        assert reply.status == 408 and reply.code == "upload_timeout"
        assert s.work_entries() == []
        assert s.service.pool_used == 0 and s.service.workdir.reserved == 0
        assert s.service.quota.inflight.get("tester", 0) == 0
        # The same for a chat body.
        body = json.dumps(chat_body(image=fk(label="slow"))).encode()
        chunks = [(0.2, body[i:i + 40]) for i in range(0, len(body), 40)][:6]
        reply = await s.request("POST", "/v1/chat/completions", chunks=chunks, content_length=len(body),
                                headers={"content-type": "application/json"})
        assert reply.status == 408
        assert s.work_entries() == [] and s.service.pool_used == 0
        assert fake.SPAWNED == []


# spec: ANYM-682
async def test_saturated_pool_is_503_and_recovers(env):
    async with env.running(max_inflight_upload_bytes=MIB) as s:
        first = audio_upload(400_000, delay=0.4, label="one")
        second = audio_upload(400_000, parts=4, delay=0.2, label="two")
        holder = s.spawn(post_audio(s, **first))
        leaver = s.spawn(post_audio(s, disconnect_after_chunks=1, **second))
        await s.until(lambda: s.service.pool_used >= first["content_length"] + second["content_length"])
        refused = await post_audio(s, **audio_upload(400_000, label="three"))
        assert refused.status == 503 and refused.code == "upload_capacity"
        gone = await leaver  # the client went away mid-body
        assert gone.status == 499
        assert s.service.pool_used == first["content_length"]  # its reservation is back
        assert (await post_audio(s, **audio_upload(400_000, label="four"))).status == 200
        assert (await holder).status == 200
        assert s.service.pool_used == 0 and s.work_entries() == []
        assert (await post_audio(s, **audio_upload(400_000, label="five"))).status == 200


# spec: ANYM-682
async def test_work_dir_cap_refuses_new_uploads(env):
    image = fk(label="slow", sleep=0.6, estimate=0.6) + b"y" * 150_000
    body = json.dumps(chat_body(image=image)).encode()
    cap = int(len(body) * 2.5)  # a chat reserves twice its body: the body file and the decoded image
    async with env.running(work_dir_max_bytes=cap) as s:
        peak = 0

        async def watch():
            nonlocal peak
            while True:
                total = 0
                for f in env.work.rglob("*"):
                    try:
                        total += f.stat().st_size if f.is_file() else 0
                    except FileNotFoundError:
                        pass
                peak = max(peak, total)
                await asyncio.sleep(0.005)

        watcher = asyncio.create_task(watch())
        first = s.spawn(s.chat(chat_body(image=image)))
        await s.until(lambda: s.service.scheduler.active() == 1)
        assert s.service.pool_used == 0  # the upload pool is free again; the work directory is not
        refused = await s.chat(chat_body(image=image))
        assert refused.status == 503 and refused.code == "upload_capacity"
        assert (await first).status == 200
        assert s.service.workdir.reserved == 0
        quick = chat_body(image=fk(label="quick", estimate=0.01) + b"y" * 150_000)
        assert (await s.chat(quick)).status == 200
        watcher.cancel()
        assert 0 < peak <= cap, (peak, cap)
        assert s.work_entries() == []
