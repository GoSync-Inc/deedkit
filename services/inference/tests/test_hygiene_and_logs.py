import json
import logging
import os
import re
import signal
import threading
import time

import pytest
from deedkit_inference.app import create_app
from deedkit_inference.engines import fake
from deedkit_inference.errors import ConfigError
from deedkit_inference.workdir import MARKER
from support import ServerProc, chat_body, fk, multipart

pytestmark = pytest.mark.anyio


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def read_pids(path):
    parts = path.read_text().split() if path.exists() else []
    return [int(p) for p in parts] if len(parts) == 2 else None


# spec: ANYM-682
async def test_work_dir_is_empty_after_every_outcome(env):
    quick = {"vision-default": {"timeout_seconds": 0.6}}
    async with env.running(aliases=quick) as s:
        pidfile = env.tmp / "pids"
        outcomes = {
            "success_chat": (await s.chat(chat_body(image=fk(label="ok")))).status == 200,
            "success_audio": (await s.transcribe()).status == 200,
            "engine_crash": (await s.chat(chat_body(image=fk(mode="crash")))).status == 502,
            "engine_garbage": (await s.chat(chat_body(image=fk(mode="garbage")))).status == 502,
            "engine_exit": (await s.chat(chat_body(image=fk(mode="exit")))).status == 502,
            "engine_timeout": (await s.chat(chat_body(image=fk(mode="hang", pidfile=str(pidfile))))).status == 504,
            "unknown_alias": (await s.chat(chat_body(model="nope", image=fk()))).status == 404,
            "invalid_body": (await s.chat(chat_body(image=fk(), temperature=1))).status == 422,
            "not_json": (await s.request("POST", "/v1/chat/completions", body=b"{",
                                         headers={"content-type": "application/json"})).status == 422,
            "malformed_multipart": (await s.request(
                "POST", "/v1/audio/transcriptions", body=b"garbage",
                headers={"content-type": "multipart/form-data; boundary=B"})).status == 422,
            "missing_file": (await s.request(
                "POST", "/v1/audio/transcriptions", body=multipart({"model": "audio-default"})[0],
                headers={"content-type": multipart({})[1]})).status == 422,
            "precheck": (await s.chat(chat_body(image=fk(megapixels=99)))).status == 422,
            "oversized": (await s.request("POST", "/v1/chat/completions", chunks=[b"x" * 100] * 3,
                                          content_length=50,
                                          headers={"content-type": "application/json"})).status == 413,
        }
        assert all(outcomes.values()), outcomes
        assert s.work_entries() == []
        pids = read_pids(pidfile)
        end = time.monotonic() + 3
        while any(alive(p) for p in pids) and time.monotonic() < end:
            time.sleep(0.02)
        assert not any(alive(p) for p in pids)
        # A client that goes away while its job runs: the job is cancelled, the child group killed.
        gone_pids = env.tmp / "gone.pids"
        reply = await s.chat(chat_body(image=fk(sleep=5, estimate=0.1, pidfile=str(gone_pids))),
                             disconnect_after_body=0.1)
        assert reply.status == 499
        assert s.work_entries() == [] and s.service.scheduler.active() == 0 and s.service.scheduler.idle()
        end = time.monotonic() + 3
        while any(alive(p) for p in read_pids(gone_pids)) and time.monotonic() < end:
            time.sleep(0.02)
        assert not any(alive(p) for p in read_pids(gone_pids))
        # A client that goes away mid-upload; a job dropped from the queue.
        body = json.dumps(chat_body(image=fk())).encode()
        reply = await s.request("POST", "/v1/chat/completions", chunks=[(0, body[:20]), (0, body[20:])],
                                content_length=len(body), disconnect_after_chunks=1,
                                headers={"content-type": "application/json"})
        assert reply.status == 499 and s.work_entries() == []
    async with env.running(max_wait_seconds=0.3) as s:
        ahead = s.spawn(s.chat(chat_body(image=fk(sleep=1.0, estimate=0.05))))
        await s.until(lambda: s.service.scheduler.active() == 1)
        dropped = await s.chat(chat_body(image=fk(estimate=0.05)))
        assert dropped.code == "slot_busy"
        assert len(s.work_entries()) == 1  # only the running job's directory
        assert (await ahead).status == 200
        assert s.work_entries() == []


# spec: ANYM-682
def test_startup_sweep_removes_leftovers_after_a_hard_kill(env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me")
    server = ServerProc(env)
    server.start()
    try:
        sentinel = env.tmp / "hard.pids"
        body = json.dumps(chat_body(image=fk(sleep=10, estimate=1, pidfile=str(sentinel)))).encode()
        thread = threading.Thread(
            target=lambda: server.http("POST", "/v1/chat/completions", body, {"Content-Type": "application/json"}),
            daemon=True)
        thread.start()
        end = time.monotonic() + 10
        while read_pids(sentinel) is None and time.monotonic() < end:
            time.sleep(0.05)
        leftovers = [p for p in env.work.iterdir() if p.name != MARKER]
        assert len(leftovers) == 1  # the running job's directory
        server.proc.send_signal(signal.SIGKILL)  # a hard kill: no finally runs
        server.proc.wait(timeout=10)
        children = read_pids(sentinel)
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    finally:
        server.stop()
    assert len([p for p in env.work.iterdir() if p.name != MARKER]) == 1
    (env.work / "stray-file").write_text("x")
    (env.work / "link").symlink_to(outside)
    (env.work / "dir" / "deep").mkdir(parents=True)
    (env.work / "dir" / "deep" / "f").write_text("x")
    create_app(env.config(), None)
    assert [p.name for p in env.work.iterdir()] == [MARKER]
    assert (outside / "precious.txt").read_text() == "keep me"  # a symlink is removed, never followed
    assert env.work.stat().st_mode & 0o777 == 0o700
    # A directory that is not ours is never swept.
    other = tmp_path / "somebody-elses"
    other.mkdir()
    (other / "important").write_text("data")
    with pytest.raises(ConfigError, match="work_dir_not_empty_and_not_ours"):
        create_app(env.config(work_dir=str(other)), None)
    assert (other / "important").read_text() == "data"


# spec: ANYM-682
async def test_upload_filename_cannot_escape_the_request_directory(env):
    async with env.running() as s:
        for name in ("../../evil.txt", "/etc/passwd", "..\\..\\x.bin", "a/../../b", "x" * 300, "\x00z", "con.mp3"):
            reply = await s.transcribe(fk(label="n"), filename=name.replace("\x00", ""))
            assert reply.status == 200, name
        paths = fake.SEEN_PATHS
        assert len(paths) == 7
        for path in paths:
            assert path.parent.parent == env.work and path.name == "audio.bin"
        everything = [p.name for p in env.tmp.rglob("*")]
        assert not any(n in ("evil.txt", "x.bin", "b", "passwd") for n in everything)
        assert s.work_entries() == []


# spec: ANYM-682
async def test_logs_hold_no_content_filename_or_key(env, caplog, monkeypatch):
    sentinel = "SENTINEL-7c1d"
    secret_key = f"dk-{sentinel}-key"
    from support import FAR, principal

    env.write_policy([principal(keys=((secret_key, FAR),))], 1)
    caplog.set_level(logging.DEBUG)
    async with env.running() as s:
        audio = fk(label="n") + sentinel.encode() * 20
        await s.transcribe(audio, filename=f"{sentinel}.mp3", key=secret_key, extra_fields={"language": "en"},
                           headers={"x-custom": sentinel})
        await s.transcribe(audio, model=f"{sentinel}-model", key=secret_key)
        await s.transcribe(audio, key=secret_key, extra_fields={sentinel: "1"})
        await s.chat(chat_body(image=fk(label="n"), text=f"prompt {sentinel}"), key=secret_key)
        await s.chat(chat_body(model=f"{sentinel}/m", image=fk(), text=sentinel), key=secret_key)
        await s.chat({**chat_body(image=fk()), sentinel: sentinel}, key=secret_key)
        await s.chat(chat_body(image=fk(mode="crash"), text=sentinel), key=secret_key)
        await s.get("/v1/models", key=f"{sentinel}-wrong", query=f"api_key={sentinel}")
        await s.get(f"/{sentinel}/nowhere", key=secret_key)
        await s.chat(chat_body(image=fk(), text=sentinel), key=f"{sentinel}-bad", headers={"x-custom": sentinel})

        def boom(*args, **kwargs):
            raise RuntimeError(f"{sentinel} in an exception")

        monkeypatch.setattr(s.service.scheduler, "admit", boom)
        await s.chat(chat_body(image=fk(), text=sentinel), key=secret_key)
    everything = caplog.text + "\n".join(r.getMessage() for r in caplog.records)
    assert sentinel not in everything
    access = [r.getMessage() for r in caplog.records if r.name == "deedkit_inference.access"]
    assert len(access) >= 8
    shape = re.compile(r"rid=[0-9a-f]{16} principal=\S+ alias=\S+ bytes_in=\d+ seconds=\d+\.\d{3} status=\d{3} "
                       r"refusal=\S+")
    for line in access:
        if line.startswith("unhandled "):
            assert line == "unhandled RuntimeError"
        else:
            assert shape.fullmatch(line), line
    assert any("principal=tester alias=audio-default" in line and "status=200" in line for line in access)
    assert any("status=401" in line and "refusal=unauthorized" in line for line in access)
    assert not [r for r in caplog.records if r.exc_info]
