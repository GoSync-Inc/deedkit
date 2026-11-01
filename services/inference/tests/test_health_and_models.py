import pytest
from support import FAR, KEY, KEY2, chat_body, fk, principal

pytestmark = pytest.mark.anyio


# spec: ANYM-682
async def test_health_reveals_nothing_else(env):
    async with env.running() as s:
        reply = await s.get("/health", key=None)
        body = reply.json()
        assert reply.status == 200
        assert set(body) == {"status", "queue", "admission"}
        assert set(body["queue"]) == {"depth", "active", "capacity"}
        assert body == {"status": "ok", "queue": {"depth": 0, "active": 0, "capacity": 16}, "admission": "open"}
        text = reply.body.decode()
        for word in ("vision", "audio", "fake", "tester", "rel-abc123", "0.1.0", "reason", "version", "release"):
            assert word not in text, word
        # No key is needed, and a key that is wrong does not change the answer or become a 401.
        assert (await s.get("/health", key="wrong")).json() == body
        assert (await s.request("POST", "/health", key=None)).status == 405
        s.probe.between = True
        closed = (await s.get("/health", key=None)).json()
        assert closed["admission"] == "closed" and set(closed) == {"status", "queue", "admission"}
        s.probe.between = False
        s.service.draining = True
        draining = (await s.get("/health", key=None)).json()
        assert draining["status"] == "draining" and draining["admission"] == "closed"


# spec: ANYM-682
async def test_models_lists_only_scoped_aliases_and_the_callers_key_expiry(env, monkeypatch):
    env.write_policy([
        principal("audio-only", keys=(("dk-a", "2098-01-01T00:00:00Z"),), scopes=("audio",)),
        principal("image-only", keys=(("dk-i", "2097-06-01T12:30:00Z"),), scopes=("image",)),
        principal("rotating", keys=((KEY, "2098-05-05T00:00:00Z"), (KEY2, FAR)), scopes=("audio", "image", "document")),
        principal("nothing", keys=(("dk-n", FAR),), scopes=()),
    ], 1)
    async with env.running() as s:
        audio = (await s.get("/v1/models", key="dk-a")).json()
        assert [m["id"] for m in audio["data"]] == ["audio-default", "audio-two-phase"]
        assert audio["key_expires_at"] == "2098-01-01T00:00:00Z" and audio["release"] == "rel-abc123"
        assert audio["object"] == "list"
        image = (await s.get("/v1/models", key="dk-i")).json()
        assert [m["id"] for m in image["data"]] == ["vision-default"]
        assert image["key_expires_at"] == "2097-06-01T12:30:00Z"
        for entry in audio["data"] + image["data"]:
            assert set(entry) == {"id", "object", "scopes", "engine", "revision"}
            assert entry["object"] == "model" and entry["engine"] == "fake" and entry["revision"] is None
        assert image["data"][0]["scopes"] == ["image"] and audio["data"][0]["scopes"] == ["audio"]
        everything = [(await s.get("/v1/models", key=k)).json() for k in (KEY, KEY2)]
        assert [m["id"] for m in everything[0]["data"]] == [
            "audio-default", "audio-two-phase", "document-default", "vision-default"]
        # Each key reports its own expiry, not the principal's other key.
        assert [e["key_expires_at"] for e in everything] == ["2098-05-05T00:00:00Z", "2099-01-01T00:00:00Z"]
        assert (await s.get("/v1/models", key="dk-n")).json()["data"] == []
        assert (await s.get("/v1/models", key=None)).status == 401
    # The release is whatever the deployment says it runs.
    monkeypatch.setenv("DEEDKIT_INFERENCE_RELEASE", "0123abcd")
    async with env.running() as s:
        assert (await s.get("/v1/models", key=KEY)).json()["release"] == "0123abcd"
        assert (await s.chat(chat_body(image=fk()))).status == 200
