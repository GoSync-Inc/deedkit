"""The gateway's own errors carry a runnable `next`; a backend error never carries payload into hint, next or logs (ANYM-544)."""
import hashlib
import json
import logging

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG, arguments
from agent_gateway.dispatch import runnable_next
from platform_contracts.agent import PLACEHOLDER_REQUEST_ID, validate_error
from test_module_selection import call, mcp
from test_policy import config

MODULES = ("calendar", "messaging")
GRANTS = {"messaging": ["read", "send"], "calendar": ["agenda:read", "availability:read"]}
TOOLS = ["platform_capabilities", "platform_select_modules", "messages_capabilities", "messages_search",
         "messages_send_message", "messages_list_feedback", "calendar_calendars"]
HEADERS = {"Authorization": "Bearer gateway-a"}
SEND = {"account": "synthetic", "chat_id": "chat-synthetic", "text": "synthetic text"}
TEXT, FILENAME, PHONE = "canary message text 7f3a", "canary-report-7f3a.pdf", "+15550107734"
CANARIES = (TEXT, FILENAME, PHONE)


def principal(tmp_path, name, expires_at="2099-01-01T00:00:00Z"):
    credentials = {}
    for module in MODULES:
        key = tmp_path / f"{name}-{module}.key"
        key.write_text(f"backend-{module}-{name}")
        key.chmod(0o600)
        credentials[module] = {"key_file": str(key), "key_sha256": hashlib.sha256(key.read_bytes()).hexdigest()}
    return {"id": name, "key_sha256": hashlib.sha256(f"gateway-{name}".encode()).hexdigest(), "profile": "agent",
            "enabled": True, "expires_at": expires_at, "grants": dict(GRANTS), "credentials": credentials,
            "actions": {}}


@pytest.fixture
def gateway(tmp_path):
    """Calendar and messaging stubs; `failures[operation]` makes the messaging backend answer (status, body)."""
    value = config()
    value["deployment"] = {"modules": list(MODULES), "backends": {m: {"url": f"http://{m}.test"} for m in MODULES}}
    value["profiles"] = {"agent": {"tools": TOOLS, "actions": {}}}
    value["principals"] = [principal(tmp_path, "a"), principal(tmp_path, "expired", "2001-01-01T00:00:00Z")]
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    requests, failures = [], {}

    def handler(module):
        def respond(request):
            operation = request.url.path.removeprefix("/v1/")
            requests.append((module, operation))
            if operation == "capabilities":
                return httpx.Response(200, json={"permissions": GRANTS[module],
                                                 "tool_manifest": {"names": ["list_feedback"]}})
            if module == "messaging" and operation in failures:
                status, body = failures[operation]
                return httpx.Response(status, json=body)
            if operation == "send_message":
                return httpx.Response(200, json={"send_id": "send-synthetic", "status": "queued"})
            return httpx.Response(200, json={"items": [], "coverage": "unknown"})
        return httpx.MockTransport(respond)

    app = create_app(path, "http://testserver", transports={m: handler(m) for m in MODULES})
    return app, requests, failures


def visible(client):
    return client.post("/v1/platform_capabilities", json={}, headers=HEADERS).json()["tools"]


def assert_runnable(client, following):
    """The caller's own catalog lists the tool and the tool's input schema accepts the arguments."""
    assert following["tool"] in visible(client)
    arguments(CATALOG[following["tool"]], following["arguments"])


# spec: ANYM-544
def test_an_unknown_tool_names_platform_capabilities_and_it_runs(gateway):
    app, _, _ = gateway
    with TestClient(app) as client:
        denied = client.post("/v1/no_such_tool", json={}, headers=HEADERS)
        following = denied.json()["next"]
        assert denied.status_code == 403 and following == {"tool": "platform_capabilities", "arguments": {}}
        validate_error(denied.json())
        assert_runnable(client, following)
        ran = client.post("/v1/" + following["tool"], json=following["arguments"], headers=HEADERS)
    assert ran.status_code == 200 and "messages_send_message" in ran.json()["tools"]


# spec: ANYM-544
def test_an_unselected_module_names_the_selection_that_widens_it_and_it_runs(gateway):
    app, requests, _ = gateway
    with TestClient(app) as client:
        call(client, "platform_select_modules", {"modules": ["calendar"]})
        denied = call(client, "messages_search", {"query": "synthetic"})
        following = denied["structuredContent"]["next"]
        assert denied["isError"] and following == {"tool": "platform_select_modules",
                                                   "arguments": {"modules": ["calendar", "messaging"]}}
        assert_runnable(client, following)
        assert not call(client, following["tool"], following["arguments"]).get("isError")
        assert not call(client, "messages_search", {"query": "synthetic"}).get("isError")
        # A module path excludes the module for good: no selection can widen it, so no `next`.
        on_path = call(client, "messages_search", {"query": "synthetic"}, path="/mcp/modules/calendar")
    assert on_path["isError"] and "next" not in on_path["structuredContent"]
    assert requests.count(("messaging", "search")) == 1


# spec: ANYM-544
def test_a_missing_request_id_names_a_next_the_tool_itself_refuses(gateway):
    app, requests, _ = gateway
    with TestClient(app) as client:
        denied = client.post("/v1/messages_send_message", json=SEND, headers=HEADERS)
        body = denied.json()
        assert denied.status_code == 422 and {"field": "request_id", "reason": "required"} in body["field_errors"]
        assert body["next"] == {"tool": "messages_send_message",
                                "arguments": {**SEND, "request_id": PLACEHOLDER_REQUEST_ID}}
        assert body["next_action"] == "check_parameters" and "tool_fields" in body  # existing keys stay
        assert_runnable(client, body["next"])
        verbatim = client.post("/v1/messages_send_message", json=body["next"]["arguments"], headers=HEADERS)
        mcp_verbatim = call(client, "messages_send_message", body["next"]["arguments"])
        sent = client.post("/v1/messages_send_message", json={**SEND, "request_id": "req-synthetic-1"}, headers=HEADERS)
    assert verbatim.status_code == 422 and verbatim.json()["field_errors"] == [{"field": "request_id", "reason": "format"}]
    assert "next" not in verbatim.json()
    assert mcp_verbatim["isError"] and mcp_verbatim["structuredContent"] == verbatim.json()
    assert sent.status_code == 200 and requests.count(("messaging", "send_message")) == 1


# spec: ANYM-544
def test_an_expired_key_gets_no_next(gateway):
    app, _, _ = gateway
    with TestClient(app) as client:
        rest = client.post("/v1/no_such_tool", json={}, headers={"Authorization": "Bearer gateway-expired"})
        listed = mcp(client, "tools/list", key="expired")
    assert rest.status_code == 401 and "next" not in rest.json()
    assert listed.status_code == 401 and "next" not in listed.text


def payload_body(**extra):
    return {"error": "invalid_parameters", "request_id": "c" * 32, "message": TEXT, "hint": f"{TEXT} {FILENAME} {PHONE}",
            "next": {"tool": "messages_send_message", "arguments": {**SEND, "text": TEXT, "phone": PHONE}},
            "candidates": [{"id": "chat-1", "label": "Synthetic", "kind": "chat",
                            "next": {"tool": "messages_send_message", "arguments": {"filename": FILENAME}}}],
            "field_errors": [{"field": "text", "reason": "pattern", "expected": TEXT}], **extra}


# spec: ANYM-544
@pytest.mark.parametrize("tool, operation, status, args", [
    ("messages_send_message", "send_message", 502, {**SEND, "request_id": "req-synthetic-1"}),
    ("messages_search", "search", 503, {"query": "synthetic"}),
    ("messages_list_feedback", "list_feedback", 422, {}),
])
def test_backend_error_payload_never_reaches_hint_next_or_logs(gateway, caplog, tool, operation, status, args):
    app, _, failures = gateway
    failures[operation] = (status, payload_body())
    caplog.set_level(logging.DEBUG)
    with TestClient(app) as client:
        rest = client.post("/v1/" + tool, json=args, headers=HEADERS)
        result = call(client, tool, args)
    assert rest.status_code >= 400 and result["isError"]
    for text in (rest.text, json.dumps(result), caplog.text):
        assert not any(canary in text for canary in CANARIES)


# spec: ANYM-544
def test_a_relayed_backend_next_survives_only_when_the_caller_can_run_it(gateway):
    app, _, failures = gateway
    runnable = {"tool": "messages_search", "arguments": {"query": "synthetic"}}
    failures["send_message"] = (409, {"error": "selection_required", "request_id": "c" * 32, "next": runnable,
                                      "candidates": payload_body()["candidates"]
                                      + [{"id": "chat-2", "label": "Synthetic 2", "kind": "chat", "next": runnable}]})
    with TestClient(app) as client:
        body = client.post("/v1/messages_send_message", json={**SEND, "request_id": "req-synthetic-1"},
                           headers=HEADERS).json()
    assert body["next"] == runnable and body["candidates"][1]["next"] == runnable
    assert "next" not in body["candidates"][0] and FILENAME not in json.dumps(body)
    # A tool outside the caller's list, an unknown argument or a malformed value is dropped, never repaired.
    for bad in ({"tool": "calls_stop_call", "arguments": {"call_id": "x"}},
                {"tool": "messages_search", "arguments": {"query": "x", "phone": PHONE}},
                {"tool": "messages_search"}, "messages_search"):
        assert "next" not in runnable_next({"error": "x", "next": bad}, TOOLS)
