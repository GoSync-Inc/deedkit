import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from test_policy import config


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "policy.json"
    key = tmp_path / "calendar.key"
    key.write_text("backend-calendar-a")
    key.chmod(0o600)
    value = config()
    value["principals"][0]["credentials"]["calendar"]["key_file"] = str(key)
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    requests = []

    def handler(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["agenda:read", "availability:read"]})
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer backend-calendar-a"
        return httpx.Response(
            200, json={"coverage": "unknown", "calendars": [], "unbound_members": ["child"]}
        )

    app = create_app(path, "http://testserver", transports={"calendar": httpx.MockTransport(handler)})
    return app, path, requests


def rpc(client, method, params=None, key="gateway-a"):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}},
        headers={"Authorization": "Bearer " + key, "Accept": "application/json, text/event-stream"},
    )


def test_real_manager_startup_initialize_list_call_and_no_sse(setup):
    app, path, requests = setup
    with TestClient(app) as client:
        result = rpc(
            client,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "synthetic", "version": "1"},
            },
        )
        assert result.status_code == 200
        assert "serverInfo" in result.json()["result"]
        tools = rpc(client, "tools/list").json()["result"]["tools"]
        assert [t["name"] for t in tools] == ["platform_capabilities", "calendar_calendars", "calendar_slots"]
        params = {"name": "calendar_calendars", "arguments": {}}
        mcp = rpc(client, "tools/call", params).json()["result"]
        rest = client.post("/v1/calendar_calendars", json={}, headers={"Authorization": "Bearer gateway-a"})
        assert mcp["structuredContent"] == rest.json()
        assert mcp["structuredContent"]["coverage"] == "unknown"
        assert client.get("/mcp", headers={"Authorization": "Bearer gateway-a"}).status_code == 405
        assert client.get("/mcp").status_code == 401
    assert app.state.clients.closed


def test_hidden_calls_invalid_arguments_and_spoofed_identity_do_not_dispatch(setup):
    app, path, requests = setup
    with TestClient(app) as client:
        for name in ("calendar_apply", "messages_search", "execute", "calendar_admin"):
            assert (
                client.post(
                    "/v1/" + name,
                    json={},
                    headers={"Authorization": "Bearer gateway-a", "X-Agent-Id": "admin"},
                ).status_code
                == 403
            )
            assert rpc(client, "tools/call", {"name": name, "arguments": {}}).json()["result"]["isError"]
        assert (
            client.post(
                "/v1/calendar_calendars",
                json={"extra": "secret"},
                headers={"Authorization": "Bearer gateway-a"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/calendar_calendars", content=b"x" * 65537, headers={"Authorization": "Bearer gateway-a"}
            ).status_code
            == 413
        )
        assert (
            client.get(
                "/mcp", headers={"Authorization": "Bearer gateway-a", "Origin": "https://foreign.invalid"}
            ).status_code
            == 403
        )
    assert requests == []


def test_reload_revokes_cached_mcp_and_rest(setup):
    app, path, requests = setup
    with TestClient(app) as client:
        assert rpc(client, "tools/list").status_code == 200
        value = json.loads(path.read_text())
        value["revision"] = 2
        value["profiles"]["reader"]["tools"] = ["platform_capabilities"]
        path.write_text(json.dumps(value))
        assert rpc(client, "tools/call", {"name": "calendar_slots", "arguments": {}}).json()["result"][
            "isError"
        ]
        assert (
            client.post(
                "/v1/calendar_calendars", json={}, headers={"Authorization": "Bearer gateway-a"}
            ).status_code
            == 403
        )
    assert requests == []


# spec: ANYM-205
def test_a_policy_change_after_the_capability_read_is_policy_changed_and_never_reaches_the_backend(setup):
    _, path, _ = setup
    sent = []

    def handler(request):
        if request.url.path == "/v1/capabilities":
            # Same tools and grants under a new revision: only the recheck before the operation can refuse.
            value = json.loads(path.read_text())
            value["revision"] = 2
            path.write_text(json.dumps(value))
            return httpx.Response(200, json={"permissions": ["agenda:read", "availability:read"]})
        sent.append(request)
        return httpx.Response(200, json={"coverage": "unknown", "calendars": []})

    app = create_app(path, "http://testserver", transports={"calendar": httpx.MockTransport(handler)})
    with TestClient(app) as client:
        rest = client.post("/v1/calendar_calendars", json={}, headers={"Authorization": "Bearer gateway-a"})
    assert (rest.status_code, rest.json()) == (409, {"error": "policy_changed"})
    assert sent == []


# spec: ANYM-205
def test_platform_capabilities_labels_the_gateway_principal_and_never_claims_owner_authorization(setup):
    app, _, _ = setup
    with TestClient(app) as client:
        body = client.post("/v1/platform_capabilities", json={}, headers={"Authorization": "Bearer gateway-a"}).json()
    assert (body["service"], body["principal"], body["gateway_principal"]) == ("deedkit", "agent-a", "agent-a")
    assert body["owner_authorization_verified"] is False and body["provider_mutations"] is False
    assert body["policy_revision"] == 1 and body["modules"] == ["calendar"]


def test_transport_failure_is_explicit_and_never_retried(setup):
    app, path, requests = setup

    async def broken(request):
        requests.append(request)
        raise httpx.ReadTimeout("DO NOT RETURN SECRET TOKEN")

    app = create_app(path, "http://testserver", transports={"calendar": httpx.MockTransport(broken)})
    with TestClient(app) as client:
        r = client.post("/v1/calendar_calendars", json={}, headers={"Authorization": "Bearer gateway-a"})
        assert r.status_code == 503
        assert r.json()["error"] == "backend_unavailable"
        assert "SECRET" not in r.text
    assert len(requests) == 1
