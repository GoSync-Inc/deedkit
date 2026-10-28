"""Synthetic issuer affinity and release admission through the real gateway."""
from pathlib import Path
import json
import tempfile
import time

import httpx
import pytest
from starlette.testclient import TestClient

from test_files import issue, setup
from test_policy import config
from agent_gateway.app import create_app


@pytest.fixture
def tmp_path():
    with tempfile.TemporaryDirectory(prefix="dkg-", dir=str(Path("/tmp").resolve())) as path:
        yield Path(path)


def test_release_gateway_warms_and_preserves_issuer_ticket_affinity(tmp_path, monkeypatch):
    private = tmp_path / "control"
    private.mkdir(mode=0o700)
    monkeypatch.setenv("DEEDKIT_RELEASE_ID", "012345abcdef")
    monkeypatch.setenv("DEEDKIT_RELEASE_SHA", "a" * 40)
    monkeypatch.setenv("DEEDKIT_RELEASE_SOCKET", str(private / "control.sock"))
    app, _, _, _ = setup(tmp_path)
    with TestClient(app) as client:
        assert issue(client).status_code == 503
        # This fixture has no passive health route, so activation fails closed.
        status = client.portal.call(app.state.release.control, "activate")
        assert status["ready"] is False and status["phase"] == "warming"
        async def ready():
            return True
        app.state.release.readiness = ready
        client.portal.call(app.state.release.control, "activate")
        result = issue(client).json()
        assert "/_releases/012345abcdef/v1/files/" in result["download_url"]
        assert len(result["download_url"].rsplit("/", 1)[1]) == 43
        status = client.portal.call(app.state.release.control, "drain")
        assert status["pending_tickets"] == 1
        assert issue(client).status_code == 503
        local_url = result["download_url"].replace("/_releases/012345abcdef", "")
        assert client.get(local_url).status_code == 200
        for entry in app.state.tickets.entries.values():
            entry["expires"] = time.time() - 1
        assert client.portal.call(app.state.release.control, "status")["pending_tickets"] == 0


def test_release_gateway_readiness_uses_only_passive_health_with_bound_host(tmp_path, monkeypatch):
    private = tmp_path / "control"
    private.mkdir(mode=0o700)
    monkeypatch.setenv("DEEDKIT_RELEASE_ID", "012345abcdef")
    monkeypatch.setenv("DEEDKIT_RELEASE_SHA", "a" * 40)
    monkeypatch.setenv("DEEDKIT_RELEASE_SOCKET", str(private / "control.sock"))
    _, path, _, _ = setup(tmp_path)
    policy = json.loads(path.read_text())
    policy["deployment"]["backends"]["messaging"]["host_header"] = "archive.example.test"
    path.write_text(json.dumps(policy))
    requests = []
    def health(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "alive"})
    app = create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(health)})
    with TestClient(app) as client:
        status = client.portal.call(app.state.release.control, "activate")
        assert status["ready"] is True and status["phase"] == "active"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET" and request.url.path == "/healthz"
    assert request.headers["Host"] == "archive.example.test"
    assert "Authorization" not in request.headers


def test_release_messaging_dial_keeps_live_policy_and_revokes_issued_ticket(tmp_path, monkeypatch):
    private = tmp_path / "control"
    private.mkdir(mode=0o700)
    monkeypatch.setenv("DEEDKIT_RELEASE_ID", "012345abcdef")
    monkeypatch.setenv("DEEDKIT_RELEASE_SHA", "a" * 40)
    monkeypatch.setenv("DEEDKIT_RELEASE_SOCKET", str(private / "control.sock"))
    monkeypatch.setenv("DEEDKIT_RELEASE_MESSAGING_URL", "http://http-api-012345abcdef:8080")
    _, path, data, _ = setup(tmp_path)
    requests = []
    def backend(request):
        import hashlib
        requests.append(request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "alive"})
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read", "download"]})
        if request.url.path == "/v1/get_attachment":
            return httpx.Response(200, json={"id": "file-a", "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "download_expires_in": 120,
                "download_url": "https://archive.example.test/v1/download/" + "a" * 43})
        assert request.url.path == "/v1/download/" + "a" * 43
        return httpx.Response(200, stream=httpx.ByteStream(data))
    app = create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(backend)})
    source_deployment = app.state.policy.load()["deployment"]
    with TestClient(app) as client:
        assert client.portal.call(app.state.release.control, "activate")["ready"] is True
        result = issue(client)
        assert result.status_code == 200
        url = result.json()["download_url"].replace("/_releases/012345abcdef", "")
        assert client.get(url).content == data
        assert all(request.url.host == "http-api-012345abcdef" for request in requests)
        assert all(request.headers["Host"] == "messaging-api:8080" for request in requests)
        assert app.state.clients.deployment == source_deployment
        policy = json.loads(path.read_text())
        policy["revision"] += 1
        policy["principals"][0]["enabled"] = False
        path.write_text(json.dumps(policy))
        count = len(requests)
        assert client.get(url).status_code == 404
        assert issue(client).status_code == 401
        assert len(requests) == count


def test_release_messaging_dial_rejects_arbitrary_target(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEDKIT_RELEASE_ID", "012345abcdef")
    monkeypatch.setenv("DEEDKIT_RELEASE_SHA", "a" * 40)
    monkeypatch.setenv("DEEDKIT_RELEASE_SOCKET", str(tmp_path / "control.sock"))
    monkeypatch.setenv("DEEDKIT_RELEASE_MESSAGING_URL", "https://foreign.invalid")
    with pytest.raises(ValueError, match="invalid_release_messaging_url"):
        setup(tmp_path)


def test_release_messaging_dial_requires_release_mode(tmp_path, monkeypatch):
    for key in ("ID", "SHA", "SOCKET"):
        monkeypatch.delenv("DEEDKIT_RELEASE_" + key, raising=False)
    monkeypatch.setenv("DEEDKIT_RELEASE_MESSAGING_URL", "http://http-api-012345abcdef:8080")
    with pytest.raises(ValueError, match="invalid_release_messaging_url"):
        setup(tmp_path)


# spec: ANYM-394
def test_release_messaging_dial_accepts_the_generation_target_of_deployed_messaging(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEDKIT_RELEASE_ID", "012345abcdef")
    monkeypatch.setenv("DEEDKIT_RELEASE_SHA", "a" * 40)
    monkeypatch.setenv("DEEDKIT_RELEASE_SOCKET", str(tmp_path / "control.sock"))
    monkeypatch.setenv("DEEDKIT_RELEASE_MESSAGING_URL", "http://http-api-012345abcdef:8080")
    app, path, _, _ = setup(tmp_path)
    assert app.state.release.generation == "012345abcdef"
    assert app.state.clients.messaging_url == "http://http-api-012345abcdef:8080"
    # The same matching URL is refused when messaging is not a deployed module.
    path.write_text(json.dumps(config()))
    with pytest.raises(ValueError, match="invalid_release_messaging_url"):
        create_app(path, "http://testserver")
