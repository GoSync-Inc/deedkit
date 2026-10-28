import base64
import json
import hashlib
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway import http_clients
from agent_gateway.http_clients import Clients
from agent_gateway.policy import Denied
from test_policy import config
from test_transport import rpc


UPLOAD_NAMES = ["prepare_file_upload", "upload_file_chunk", "get_file_upload"]
HEADERS = {"Authorization": "Bearer gateway-a"}


@pytest.fixture
def upload_setup(tmp_path):
    key = tmp_path / "backend.key"
    key.write_text("backend-messaging-a")
    key.chmod(0o600)
    value = config()
    value["deployment"] = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://messaging"}}}
    value["profiles"]["reader"] = {
        "tools": ["platform_capabilities", *["messages_" + n for n in UPLOAD_NAMES]],
        "actions": {"messenger_operations": ["send_file"]},
    }
    principal = value["principals"][0]
    principal["grants"] = {"messaging": ["read", "send", "upload", "download"]}
    principal["actions"] = {"messenger_operations": ["send_file"]}
    principal["credentials"] = {
        "messaging": {"key_file": str(key), "key_sha256": hashlib.sha256(b"backend-messaging-a").hexdigest()}
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    caps = {
        "permissions": ["read", "send", "upload", "download"],
        "tool_manifest": {"names": list(UPLOAD_NAMES)},
        "messenger_operations": {
            "supported_by_account": {
                "synthetic": {"operations": ["send_file"]},
                "unsupported": {"operations": []},
            }
        },
    }
    dispatched = []

    def handler(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json=caps)
        dispatched.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200, json={"upload_id": "up_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "state": "ready"}
        )

    app = create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(handler)})
    return app, path, value, caps, dispatched


def chunk(size=262144):
    return {
        "upload_id": "up_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "offset": 0,
        "data_base64": base64.b64encode(b"a" * size).decode(),
    }


def test_upload_discovery_requires_manifest_scopes_and_send_file_support(upload_setup):
    app, path, value, caps, dispatched = upload_setup
    with TestClient(app) as c:
        listed = [t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"]]
        assert listed == value["profiles"]["reader"]["tools"]
        caps["tool_manifest"]["names"].remove("upload_file_chunk")
        listed = [t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"]]
        assert "messages_upload_file_chunk" not in listed
        assert c.post("/v1/messages_upload_file_chunk", json=chunk(), headers=HEADERS).status_code == 403
        caps["tool_manifest"]["names"].append("upload_file_chunk")
        caps["permissions"].remove("upload")
        assert [t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"]] == [
            "platform_capabilities"
        ]
        caps["permissions"].append("upload")
        for row in caps["messenger_operations"]["supported_by_account"].values():
            row["operations"] = []
        assert [t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"]] == [
            "platform_capabilities"
        ]
    assert not dispatched


def test_max_chunk_works_through_rest_and_mcp_without_relaxing_other_bodies(upload_setup):
    app, path, value, caps, dispatched = upload_setup
    arguments = chunk()
    with TestClient(app) as c:
        assert c.post("/v1/messages_upload_file_chunk", json=arguments, headers=HEADERS).status_code == 200
        result = rpc(c, "tools/call", {"name": "messages_upload_file_chunk", "arguments": arguments})
        assert result.status_code == 200 and not result.json()["result"].get("isError")
        assert (
            c.post(
                "/v1/messages_get_file_upload", json={"upload_id": "x" * 65536}, headers=HEADERS
            ).status_code
            == 413
        )
        assert (
            rpc(
                c, "tools/call", {"name": "messages_get_file_upload", "arguments": {"upload_id": "x" * 65536}}
            ).status_code
            == 413
        )
        assert rpc(c, "initialize", {"padding": "x" * 65536}).status_code == 413
        assert (
            c.post("/v1/messages_upload_file_chunk", content=b"x" * 360449, headers=HEADERS).status_code
            == 413
        )
        assert rpc(
            c, "tools/call", {"name": "messages_upload_file_chunk", "arguments": chunk(262145)}
        ).json()["result"]["isError"]
    assert len(dispatched) == 2


def test_upload_revocation_and_wrong_account_never_dispatch(upload_setup):
    app, path, value, caps, dispatched = upload_setup
    args = {
        "account": "unsupported",
        "chat_id": "chat-synthetic",
        "filename": "photo.jpg",
        "mime": "image/jpeg",
        "bytes": 10,
        "sha256": "a" * 64,
        "request_id": "synthetic-request",
    }
    with TestClient(app) as c:
        assert c.post("/v1/messages_prepare_file_upload", json=args, headers=HEADERS).status_code == 403
        value["revision"] += 1
        value["principals"][0]["grants"]["messaging"].remove("upload")
        path.write_text(json.dumps(value))
        assert c.post("/v1/messages_upload_file_chunk", json=chunk(), headers=HEADERS).status_code in (
            403,
            413,
        )
        assert rpc(c, "tools/call", {"name": "messages_upload_file_chunk", "arguments": chunk(100)}).json()[
            "result"
        ]["isError"]
    assert not dispatched


@pytest.mark.parametrize("missing", ["download", "action"])
def test_upload_requires_existing_local_send_file_ceiling_before_body_read(upload_setup, missing):
    app, path, value, caps, dispatched = upload_setup
    value["revision"] += 1
    if missing == "download":
        value["principals"][0]["grants"]["messaging"].remove("download")
    else:
        value["profiles"]["reader"]["actions"] = {}
    path.write_text(json.dumps(value))
    with TestClient(app) as c:
        assert [t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"]] == [
            "platform_capabilities"
        ]
        assert c.post("/v1/messages_upload_file_chunk", json=chunk(), headers=HEADERS).status_code == 403
    assert not dispatched


@pytest.mark.parametrize("manifest", [None, {}, {"names": ["upload_file_chunk", None]}])
def test_missing_or_malformed_upload_manifest_fails_closed(upload_setup, manifest):
    app, path, value, caps, dispatched = upload_setup
    caps["tool_manifest"] = manifest
    with TestClient(app) as c:
        assert [t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"]] == [
            "platform_capabilities"
        ]
        assert c.post("/v1/messages_upload_file_chunk", json=chunk(), headers=HEADERS).status_code == 403
    assert not dispatched


@pytest.mark.parametrize(
    "changes",
    [
        {"data_base64": "!invalid!"},
        {"data_base64": ""},
        {"offset": -1},
        {"path": "/private/file"},
        {"url": "https://foreign.invalid/file"},
    ],
)
def test_invalid_chunks_and_source_locations_never_dispatch(upload_setup, changes):
    app, path, value, caps, dispatched = upload_setup
    with TestClient(app) as c:
        assert (
            c.post(
                "/v1/messages_upload_file_chunk", json={**chunk(100), **changes}, headers=HEADERS
            ).status_code
            == 422
        )
    assert not dispatched


def test_body_limits_apply_without_content_length_and_duplicate_mcp_keys(upload_setup):
    app, path, value, caps, dispatched = upload_setup
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "messages_upload_file_chunk", "arguments": chunk()},
        }
    ).encode()
    with TestClient(app) as c:
        result = c.post(
            "/mcp",
            content=iter([body[:10000], body[10000:]]),
            headers={
                **HEADERS,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert result.status_code == 200
        duplicate = body[:-1] + b',"method":"initialize"}'
        assert c.post("/mcp", content=duplicate, headers=HEADERS).status_code == 413
        assert (
            c.post(
                "/v1/messages_get_file_upload", content=iter([b"x" * 65536, b"x"]), headers=HEADERS
            ).status_code
            == 413
        )
        assert (
            c.post(
                "/v1/messages_upload_file_chunk", content=iter([b"x" * 360448, b"x"]), headers=HEADERS
            ).status_code
            == 413
        )
    assert len(dispatched) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"filename": "../secret"},
        {"filename": "x\n.jpg"},
        {"mime": "image/jpeg; charset=utf-8"},
        {"bytes": 0},
        {"bytes": 33554433},
        {"sha256": "not-a-hash"},
        {"path": "/private/source"},
        {"url": "https://foreign.invalid/source"},
    ],
)
def test_upload_prepare_is_bounded_metadata_without_source_paths(upload_setup, changes):
    app, path, value, caps, dispatched = upload_setup
    arguments = {
        "account": "synthetic",
        "chat_id": "chat-synthetic",
        "filename": "photo.jpg",
        "mime": "image/jpeg",
        "bytes": 10,
        "sha256": "a" * 64,
        "request_id": "synthetic-request",
    }
    with TestClient(app) as c:
        assert (
            c.post(
                "/v1/messages_prepare_file_upload", json={**arguments, **changes}, headers=HEADERS
            ).status_code
            == 422
        )
    assert not dispatched


def test_upload_pool_is_bounded_and_does_not_block_reads_or_file_download_pool():
    async def run():
        release = asyncio.Event()
        started = asyncio.Event()
        count = 0

        async def backend(request):
            nonlocal count
            if request.url.path.endswith("upload_file_chunk"):
                count += 1
                if count == 2:
                    started.set()
                await release.wait()
            return httpx.Response(200, json={"state": "ready"})

        deployment = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://messaging"}}}
        async with Clients(deployment, {"messaging": httpx.MockTransport(backend)}) as clients:
            tasks = [
                asyncio.create_task(clients.call("messaging", "upload_file_chunk", "synthetic", {}))
                for _ in range(6)
            ]
            try:
                await asyncio.wait_for(started.wait(), 0.2)
                with pytest.raises(Denied, match="backend_busy"):
                    await clients.call("messaging", "upload_file_chunk", "synthetic", {})
                async with asyncio.timeout(0.2), clients.bulk_budgets["messaging"].slot():
                    assert await clients.call("messaging", "search", "synthetic", {}) == {"state": "ready"}
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            assert clients.upload_budgets["messaging"].pending == 0
        assert clients.closed
        assert not clients.upload_rate.observations

    asyncio.run(run())


def test_upload_rate_is_credential_scoped_and_never_retries():
    async def run():
        seen = []

        async def backend(request):
            seen.append(request.url.path)
            return httpx.Response(200, json={"state": "ready"})

        deployment = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://messaging"}}}
        async with Clients(deployment, {"messaging": httpx.MockTransport(backend)}) as clients:
            for _ in range(8):
                await clients.call("messaging", "upload_file_chunk", "synthetic", {})
            with pytest.raises(Denied, match="upload_rate_limited"):
                await clients.call("messaging", "upload_file_chunk", "synthetic", {})
            assert len(seen) == 8
            await clients.call("messaging", "upload_file_chunk", "other-synthetic", {})
            await clients.call("messaging", "search", "synthetic", {})
            assert len(seen) == 10

    asyncio.run(run())


def test_upload_timeout_is_bounded_and_not_retried(monkeypatch):
    async def run():
        attempts = []
        timeout = asyncio.timeout
        monkeypatch.setattr(asyncio, "timeout", lambda delay: timeout(0.02 if delay == 10 else delay))

        async def backend(request):
            attempts.append(request.url.path)
            await asyncio.sleep(60)

        deployment = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://messaging"}}}
        async with Clients(deployment, {"messaging": httpx.MockTransport(backend)}) as clients:
            with pytest.raises(Denied, match="backend_unavailable"):
                await clients.call("messaging", "upload_file_chunk", "synthetic", {})
            assert attempts == ["/v1/upload_file_chunk"]
            assert clients.upload_budgets["messaging"].pending == 0

    asyncio.run(run())


# spec: ANYM-393
def test_upload_rate_state_tracks_at_most_1024_credentials(monkeypatch):
    now = [100.0]
    # Freeze only the upload-rate clock, so all 1024 credentials stay active in the last second.
    monkeypatch.setattr(http_clients, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def run():
        seen = []

        async def backend(request):
            seen.append(request.url.path)
            return httpx.Response(200, json={"state": "ready"})

        deployment = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://messaging"}}}
        async with Clients(deployment, {"messaging": httpx.MockTransport(backend)}) as clients:
            for index in range(1024):
                clients.upload_rate.admit(f"synthetic-{index}")
            assert len(clients.upload_rate.observations) == 1024
            with pytest.raises(Denied, match="backend_busy") as refused:
                await clients.call("messaging", "upload_file_chunk", "synthetic-new", {})
            assert refused.value.status == 503 and seen == []
            assert len(clients.upload_rate.observations) == 1024
            await clients.call("messaging", "upload_file_chunk", "synthetic-0", {})
            now[0] += 1
            await clients.call("messaging", "upload_file_chunk", "synthetic-new", {})
            assert len(clients.upload_rate.observations) == 1
            assert seen == ["/v1/upload_file_chunk"] * 2

    asyncio.run(run())
