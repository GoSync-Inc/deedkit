import asyncio
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway import files
from agent_gateway.app import create_app
from agent_gateway.files import Tickets
from agent_gateway.policy import Denied
from test_policy import config


def setup(
    tmp_path,
    *,
    ttl=120,
    source="https://archive.example.test",
    redirect=False,
    public="http://testserver",
    download_origin="https://archive.example.test",
    body=None,
    head=None,
    original=None,
):
    path = tmp_path / "policy.json"
    key = tmp_path / "backend.key"
    key.write_text("backend-messaging-a")
    key.chmod(0o600)
    value = config()
    value["deployment"] = {
        "modules": ["messaging"],
        "backends": {
            "messaging": {
                "url": "http://messaging-api:8080",
                "download_origin": download_origin,
            }
        },
    }
    value["profiles"]["reader"]["tools"] = ["messages_get_attachment", "platform_capabilities"]
    value["principals"][0]["grants"] = {"messaging": ["read", "download"]}
    value["principals"][0]["credentials"] = {
        "messaging": {"key_file": str(key), "key_sha256": hashlib.sha256(key.read_bytes()).hexdigest()}
    }
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    data = b"synthetic original\x00" * 10000 if original is None else original
    requests = []

    def handler(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read", "download"]})
        requests.append(request)
        if request.url.path == "/v1/get_attachment":
            return httpx.Response(
                200,
                json={
                    "id": "file-a",
                    "filename": "fixture.bin",
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "download_url": source + "/v1/download/" + "a" * 43,
                    "download_expires_in": ttl,
                },
            )
        assert request.url.host == "messaging-api"
        assert request.url.path == "/v1/download/" + "a" * 43
        if redirect:
            return httpx.Response(302, headers={"location": "https://foreign.invalid"})
        if request.method == "HEAD":
            return httpx.Response(head() if head else 200)
        return httpx.Response(200, stream=Slow(body(data)) if body else httpx.ByteStream(data))

    app = create_app(path, public, transports={"messaging": httpx.MockTransport(handler)})
    return app, path, data, requests


class Slow(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        async for chunk in self.chunks:
            yield chunk


class Recorded:
    """Keep wire frames, so a truncated body is visible behind TestClient."""

    def __init__(self, app):
        self.app, self.frames = app, []

    async def __call__(self, scope, receive, send):
        async def record(message):
            if scope["type"] == "http":
                self.frames.append(message)
            await send(message)

        await self.app(scope, receive, record)


def issue(client):
    return client.post(
        "/v1/messages_get_attachment",
        json={"attachment_id": "file-a"},
        headers={"Authorization": "Bearer gateway-a"},
    )


def test_ticket_streams_original_bytes_and_retains_integrity_metadata(tmp_path):
    app, path, data, requests = setup(tmp_path)
    with TestClient(app) as client:
        result = issue(client).json()
        assert urlsplit(result["download_url"]).netloc == "testserver"
        assert "a" * 43 not in result["download_url"]
        response = client.get(result["download_url"])
        assert response.content == data
        assert hashlib.sha256(response.content).hexdigest() == result["sha256"]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["content-type"] == "application/octet-stream"
    assert app.state.clients.closed
    assert len(requests) == 2


def test_ticket_revocation_is_checked_without_a_gateway_bearer_header(tmp_path):
    app, path, _, requests = setup(tmp_path)
    with TestClient(app) as client:
        url = issue(client).json()["download_url"]
        value = json.loads(path.read_text())
        value["revision"] += 1
        value["principals"][0]["enabled"] = False
        path.write_text(json.dumps(value))
        assert client.get(url).status_code == 404
    assert len(requests) == 1


# spec: ANYM-206
@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_keyless_ticket_path_refuses_a_body_before_reading_it(tmp_path, method):
    app, _, _, requests = setup(tmp_path)
    sent, reads = [], []

    async def receive():
        reads.append(method)
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    path = "/v1/files/" + "a" * 43
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
             "client": ("203.0.113.9", 1234), "server": ("testserver", 80),
             "headers": [(b"host", b"testserver"), (b"content-length", b"1024")]}
    # A slow body from anyone on the internet must not hold the request for the 10 s body deadline.
    asyncio.run(asyncio.wait_for(app(scope, receive, send), 2))
    assert reads == [] and requests == []
    assert sent[0]["status"] == 405
    assert json.loads(sent[1]["body"]) == {"error": "method_not_allowed"}


def test_expired_ticket_and_range_do_not_contact_backend(tmp_path):
    app, _, _, requests = setup(tmp_path)
    with TestClient(app) as client:
        url = issue(client).json()["download_url"]
        assert client.get(url, headers={"Range": "bytes=0-1"}).status_code == 416
        for row in app.state.tickets.entries.values():
            row["expires"] = time.time() - 1
        assert client.get(url).status_code == 404
    assert len(requests) == 1


def test_unreviewed_download_origin_and_redirect_never_followed(tmp_path):
    app, _, _, requests = setup(tmp_path, source="https://foreign.invalid")
    with TestClient(app) as client:
        assert issue(client).status_code == 503
    assert len(requests) == 1
    app, _, _, requests = setup(tmp_path, redirect=True)
    with TestClient(app) as client:
        assert client.get(issue(client).json()["download_url"]).status_code == 503
    assert len(requests) == 2


def test_explicit_proxy_prefixes_preserve_public_ticket_and_canonical_upstream(tmp_path):
    app, path, data, seen = setup(
        tmp_path,
        source="https://archive.example.test/legacy",
        public="http://testserver/deedkit",
        download_origin="https://archive.example.test/legacy",
    )
    with TestClient(app) as client:
        result = issue(client).json()
        assert result["download_url"].startswith("http://testserver/deedkit/v1/files/")
        # The trusted proxy removes only the configured public prefix.
        response = client.get(
            result["download_url"].replace("/deedkit/", "/", 1), headers={"Origin": "http://testserver"}
        )
        assert response.content == data
        assert seen[-1].url.path == "/v1/download/" + "a" * 43
        assert client.get("/healthz", headers={"Origin": "http://testserver/deedkit"}).status_code == 403


def test_ticket_ttl_bounds_admission_not_a_started_transfer(tmp_path):
    tickets = []

    async def body(data):
        yield data[:65536]
        for row in tickets[0].entries.values():
            row["expires"] = time.time() - 1  # The TTL passes while bytes are flowing.
        await asyncio.sleep(1.4)  # Several watcher ticks and one backend HEAD.
        yield data[65536:]

    app, _, data, requests = setup(tmp_path, body=body)
    tickets.append(app.state.tickets)
    with TestClient(app) as client:
        url = issue(client).json()["download_url"]
        assert client.get(url).content == data
        assert client.get(url).status_code == 404  # An expired ticket starts nothing new.
    assert [r.method for r in requests].count("GET") == 1
    assert "HEAD" in [r.method for r in requests]


def test_started_transfer_still_stops_on_policy_revocation(tmp_path):
    policies = []

    async def body(data):
        yield data[:65536]
        value = json.loads(policies[0].read_text())
        value["revision"] += 1
        value["principals"][0]["enabled"] = False
        policies[0].write_text(json.dumps(value))
        await asyncio.sleep(5)
        yield data[65536:]

    app, path, data, _ = setup(tmp_path, body=body)
    policies.append(path)
    with TestClient(app, raise_server_exceptions=False) as client:
        url = issue(client).json()["download_url"]
        started = time.monotonic()
        response = client.get(url)
        assert time.monotonic() - started < 2
        assert response.status_code == 200 and len(response.content) < len(data)


@pytest.mark.parametrize(
    "answers, complete",
    [
        ([429, 503, 502, 200], True),
        ([503], False),
        ([429], False),
        ([401], False),
        ([403], False),
        ([404], False),
    ],
)
def test_backend_revalidation_ends_only_on_denial_or_lasting_failure(
    tmp_path, monkeypatch, answers, complete
):
    monkeypatch.setattr(files, "WATCH_SECONDS", 0.02)
    monkeypatch.setattr(files, "BACKEND_RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(files, "BACKEND_GRACE_SECONDS", 0.6)
    heads = []

    def head():
        heads.append(time.monotonic())
        return answers[min(len(heads), len(answers)) - 1]

    async def body(data):
        yield data[:65536]
        await asyncio.sleep(1.5)
        yield data[65536:]

    app, _, data, _ = setup(tmp_path, body=body, head=head)
    with TestClient(app, raise_server_exceptions=False) as client:
        url = issue(client).json()["download_url"]
        started = time.monotonic()
        response = client.get(url)
        elapsed = time.monotonic() - started
    if complete:
        assert response.content == data and len(heads) > len(answers)
    elif answers[0] in (429, 503):
        # Overload is tolerated until nothing was confirmed for the grace period.
        assert len(response.content) < len(data) and 0.6 <= elapsed < 1.4 and len(heads) > 3
    else:
        assert len(response.content) < len(data) and elapsed < 0.5 and len(heads) == 1


def test_backend_check_separates_denials_from_unavailability():
    async def answer(value):
        def handler(request):
            assert request.method == "HEAD"
            if value == "error":
                raise httpx.ConnectError("synthetic")
            return httpx.Response(value)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url="http://messaging-api", transport=transport) as client:
            tickets = Tickets(None, SimpleNamespace(metadata={"messaging": client}), "http://testserver")
            try:
                return await tickets.check_backend({"path": "/v1/download/" + "a" * 43})
            except Denied as error:
                return error.status

    async def run():
        return {
            value: await answer(value) for value in [200, 429, 500, 503, "error", 302, 400, 401, 403, 404]
        }

    assert asyncio.run(run()) == {
        200: True,
        429: False,
        500: False,
        503: False,
        "error": False,
        302: 404,
        400: 404,
        401: 404,
        403: 404,
        404: 404,
    }


@pytest.mark.parametrize("position", [0, -1])
def test_digest_mismatch_truncates_the_response_instead_of_completing_it(tmp_path, position):
    async def body(data):
        # Right length, one corrupted byte; the backend stream ends normally.
        corrupted = bytearray(data)
        corrupted[position] ^= 1
        for start in range(0, len(corrupted), 65536):
            yield bytes(corrupted[start : start + 65536])

    app, _, data, _ = setup(tmp_path, body=body)
    wire = Recorded(app)
    with TestClient(wire, raise_server_exceptions=False) as client:
        url = issue(client).json()["download_url"]
        wire.frames.clear()
        response = client.get(url)
    sent = [frame for frame in wire.frames if frame["type"] == "http.response.body"]
    assert response.status_code == 200 and response.headers["content-length"] == str(len(data))
    # Everything but the held-back final chunk; the response never completes.
    assert sum(len(frame.get("body", b"")) for frame in sent) == len(data) - len(data) % 65536
    assert all(frame.get("more_body") for frame in sent)


def test_verified_file_is_completed_and_empty_file_digest_checked_before_headers(tmp_path):
    app, _, data, _ = setup(tmp_path, original=b"")
    with TestClient(app) as client:
        url = issue(client).json()["download_url"]
        response = client.get(url)
        assert response.status_code == 200 and response.content == b""
        for row in app.state.tickets.entries.values():
            row["sha256"] = "0" * 64
        response = client.get(url)
        assert response.status_code == 503 and response.json() == {"error": "backend_file_integrity_error"}
