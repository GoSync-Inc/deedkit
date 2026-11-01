import json
import threading

import pytest
from deedkit_inference.app import create_app
from deedkit_inference.cli import main
from deedkit_inference.errors import PolicyError
from deedkit_inference.policy import write_policy
from pydantic import ValidationError
from support import FAR, KEY, KEY2, PAST, chat_body, fk, policy_doc, principal

pytestmark = pytest.mark.anyio


# spec: ANYM-682
async def test_wrong_expired_and_revoked_keys_get_identical_401(env):
    gone = "dk-revoked-key"
    both = [principal(), principal("old", keys=((gone, FAR),)), principal("lapsed", keys=(("dk-expired", PAST),))]
    env.write_policy(both, 1)
    async with env.running() as s:
        assert (await s.get("/v1/models", key=gone)).status == 200
        env.write_policy([principal(), principal("lapsed", keys=(("dk-expired", PAST),))], 2)  # revoked
        replies = [
            await s.get("/v1/models", key=None),
            await s.get("/v1/models", key="nope"),
            await s.get("/v1/models", headers={"authorization": "Basic " + KEY}, key=None),
            await s.get("/v1/models", headers={"authorization": "Bearer"}, key=None),
            await s.get("/v1/models", headers={"authorization": "Bearer "}, key=None),
            await s.get("/v1/models", headers={"authorization": "Bearer a b"}, key=None),
            await s.get("/v1/models", headers={"authorization": "Bearer " + "k" * 600}, key=None),
            await s.get("/v1/models", key="dk-expired"),
            await s.get("/v1/models", key=gone),
        ]
        first = replies[0]
        for reply in replies:
            assert reply.status == 401
            assert reply.body == first.body
            assert {k: v for k, v in reply.headers.items() if k != "x-request-id"} == {
                k: v for k, v in first.headers.items() if k != "x-request-id"}
        assert (await s.get("/v1/models")).status == 200


# spec: ANYM-682
async def test_two_keys_overlap_and_the_old_one_can_be_removed(env):
    env.write_policy([principal(keys=((KEY, FAR), (KEY2, FAR)))], 1)
    async with env.running() as s:
        assert (await s.get("/v1/models", key=KEY)).status == 200
        assert (await s.get("/v1/models", key=KEY2)).status == 200
        env.write_policy([principal(keys=((KEY2, FAR),))], 2)
        assert (await s.get("/v1/models", key=KEY)).status == 401
        assert (await s.get("/v1/models", key=KEY2)).status == 200
    three = principal(keys=((KEY, FAR), (KEY2, FAR), ("dk-third", FAR)))
    with pytest.raises(ValidationError):
        write_policy(env.policy_path, policy_doc([three], 3))
    with pytest.raises(ValidationError):
        write_policy(env.policy_path, policy_doc([principal(keys=((KEY, FAR), (KEY, FAR)))], 3))


# spec: ANYM-682
async def test_scope_denied(env):
    audio_only = principal("audio-only", keys=(("dk-audio", FAR),), scopes=("audio",))
    image_only = principal("image-only", keys=(("dk-image", FAR),), scopes=("image",))
    env.write_policy([principal(), audio_only, image_only], 1)
    async with env.running() as s:
        # Route level: no scope for the route at all.
        reply = await s.chat(chat_body(image=fk()), key="dk-audio")
        assert reply.status == 403 and reply.code == "scope_denied"
        reply = await s.transcribe(key="dk-image")
        assert reply.status == 403 and reply.code == "scope_denied"
        # Alias level: the key has a chat scope, but not this alias's.
        reply = await s.chat(chat_body(model="document-default", image=fk()), key="dk-image")
        assert reply.status == 403 and reply.code == "scope_denied"
        assert (await s.chat(chat_body(image=fk()), key="dk-image")).status == 200
        assert (await s.transcribe(key="dk-audio")).status == 200
        assert [m["id"] for m in (await s.get("/v1/models", key="dk-audio")).json()["data"]] == [
            "audio-default", "audio-two-phase"]
        assert fake_events() == 2


def fake_events():
    from deedkit_inference.engines import fake

    return len([e for e in fake.EVENTS if e[0] == "start"])


# spec: ANYM-682
async def test_daily_and_concurrency_caps(env):
    env.write_policy([principal(per_day=2, concurrent=1)], 1)
    async with env.running() as s:
        slow = fk(label="slow", sleep=0.5, estimate=0.5)
        first = s.spawn(s.chat(chat_body(image=slow)))
        await s.until(lambda: s.service.quota.inflight.get("tester") == 1)
        second = await s.chat(chat_body(image=fk()))
        assert second.status == 429 and second.code == "concurrency_exceeded"
        assert int(second.headers["retry-after"]) >= 1
        assert (await first).status == 200
        # A refused or invalid request is not a job: it does not count against the day.
        assert (await s.chat(chat_body(image=fk(), temperature=1))).status == 422
        assert (await s.chat(chat_body(model="nope", image=fk()))).status == 404
        assert (await s.chat(chat_body(image=fk()))).status == 200  # the second counted job
        third = await s.chat(chat_body(image=fk()))
        assert third.status == 429 and third.code == "quota_exceeded"
        assert int(third.headers["retry-after"]) >= 1
        assert s.service.quota.inflight["tester"] == 0


# spec: ANYM-682
def test_policy_file_modes_and_symlink_refused(env, tmp_path):
    env.write_policy()
    assert create_app(env.config(), None) is not None
    for mode in (0o644, 0o640, 0o604, 0o660):
        env.policy_path.chmod(mode)
        with pytest.raises(PolicyError):
            create_app(env.config(), None)
    env.policy_path.chmod(0o600)
    real = tmp_path / "real.json"
    real.write_bytes(env.policy_path.read_bytes())
    real.chmod(0o600)
    env.policy_path.unlink()
    env.policy_path.symlink_to(real)
    with pytest.raises(PolicyError):
        create_app(env.config(), None)
    env.policy_path.unlink()
    env.policy_path.write_text(" " * (64 * 1024 + 1))
    env.policy_path.chmod(0o600)
    with pytest.raises(PolicyError):
        create_app(env.config(), None)
    env.policy_path.unlink()
    env.policy_path.mkdir()
    with pytest.raises(PolicyError):
        create_app(env.config(), None)
    env.policy_path.rmdir()
    with pytest.raises(PolicyError):
        create_app(env.config(), None)  # missing
    # Invalid content is refused as well, and so is a duplicate key.
    for text in ("{", '{"version": 1, "revision": 1, "principals": [], "extra": 1}',
                 '{"version": 1, "revision": 1, "revision": 2, "principals": []}'):
        env.policy_path.write_text(text)
        env.policy_path.chmod(0o600)
        with pytest.raises(PolicyError):
            create_app(env.config(), None)
        env.policy_path.unlink()
    # Through the command: exit 2 and one reason line, nothing started.
    env.policy_path.write_text("{}")
    env.policy_path.chmod(0o644)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(env.config_dict()))
    config_path.chmod(0o600)
    with pytest.raises(SystemExit) as raised:
        main(["serve", "--config", str(config_path)])
    assert raised.value.code == 2


# spec: ANYM-682
async def test_policy_rollback_refused(env):
    only_new, only_old = "dk-only-in-5", "dk-only-in-4"
    env.write_policy([principal(keys=((only_new, FAR),))], 5)
    async with env.running() as s:
        assert (await s.get("/v1/models", key=only_new)).status == 200
        # A smaller revision written behind the writer's back: refused, the old policy stays.
        env.policy_path.write_text(json.dumps(policy_doc([principal(keys=((only_old, FAR),))], 4)))
        env.policy_path.chmod(0o600)
        assert (await s.get("/v1/models", key=only_old)).status == 401
        assert (await s.get("/v1/models", key=only_new)).status == 200
        # The same revision with different content: refused as well.
        env.policy_path.write_text(json.dumps(policy_doc([principal(keys=((only_old, FAR),))], 5)))
        env.policy_path.chmod(0o600)
        assert (await s.get("/v1/models", key=only_old)).status == 401
        assert (await s.get("/v1/models", key=only_new)).status == 200
        # A greater revision is taken.
        write_policy(env.policy_path, policy_doc([principal(keys=((only_old, FAR),))], 6))
        assert (await s.get("/v1/models", key=only_old)).status == 200
        assert (await s.get("/v1/models", key=only_new)).status == 401
    # The writer itself refuses a rollback and an equal revision, and leaves a private file.
    for revision in (6, 3):
        with pytest.raises(PolicyError):
            write_policy(env.policy_path, policy_doc([principal()], revision))
    assert env.policy_path.stat().st_mode & 0o777 == 0o600
    assert env.policy_path.with_name("policy.json.lock").exists()
    # One writer at a time: two writers race for the same revision, exactly one wins.
    results = []

    def writer(pid):
        try:
            write_policy(env.policy_path, policy_doc([principal(pid)], 7))
            results.append("ok")
        except PolicyError:
            results.append("refused")

    threads = [threading.Thread(target=writer, args=(f"w{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["ok", "refused", "refused", "refused"]


# spec: ANYM-682
async def test_key_in_query_string_is_not_accepted(env):
    async with env.running() as s:
        for name in ("api_key", "key", "access_token", "token", "authorization"):
            reply = await s.get("/v1/models", key=None, query=f"{name}={KEY}")
            assert reply.status == 401, name
        body = json.dumps(chat_body(image=fk())).encode()
        reply = await s.request("POST", "/v1/chat/completions", body=body, key=None, query=f"api_key={KEY}",
                                headers={"content-type": "application/json"})
        assert reply.status == 401 and reply.stats.receive_calls == 0
        # The header still works when a query string is present, and the query value is not the credential.
        assert (await s.get("/v1/models", key=KEY, query="api_key=junk")).status == 200


# spec: ANYM-682
async def test_unauthenticated_request_body_is_never_read(env):
    audio_only = principal("audio-only", keys=(("dk-audio", FAR),), scopes=("audio",), per_day=1)
    env.write_policy([principal(), audio_only], 1)
    async with env.running() as s:
        chunks = [b"x" * 65536 for _ in range(20)]
        total = 65536 * 20
        for key, path in ((None, "/v1/chat/completions"), ("wrong", "/v1/chat/completions"),
                          ("dk-expired", "/v1/audio/transcriptions"), ("dk-audio", "/v1/chat/completions")):
            reply = await s.request("POST", path, chunks=chunks, content_length=total, key=key,
                                    headers={"content-type": "application/json"})
            assert reply.status in (401, 403), (key, reply.status)
            assert reply.stats.receive_calls == 0 and reply.stats.bytes_delivered == 0
            assert reply.headers.get("connection") == "close"
        assert s.work_entries() == []
        # The daily cap is refused before the body as well.
        assert (await s.transcribe(key="dk-audio")).status == 200
        reply = await s.request("POST", "/v1/audio/transcriptions", chunks=chunks, content_length=total,
                                key="dk-audio", headers={"content-type": "multipart/form-data; boundary=x"})
        assert reply.status == 429 and reply.stats.receive_calls == 0
