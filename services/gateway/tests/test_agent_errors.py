"""The gateway's own errors carry a runnable `next`; a backend error never carries payload into hint, next or logs (ANYM-544)."""
import hashlib
import json
import logging

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG, arguments
from platform_contracts.agent import PLACEHOLDER_REQUEST_ID, validate_error
from test_module_selection import call, mcp
from test_policy import config

MODULES = ("calendar", "messaging")
GRANTS = {"messaging": ["read", "send"], "calendar": ["agenda:read", "availability:read"]}
TOOLS = ["platform_capabilities", "platform_select_modules", "messages_capabilities", "messages_search",
         "messages_send_message", "messages_list_feedback", "messages_get_updates", "calendar_calendars"]
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



SEARCH = {"tool": "messages_search", "arguments": {"query": "synthetic"}}
SENT = {**SEND, "request_id": "req-synthetic-1"}


def relayed(client, tool, args, path="/mcp"):
    """The REST body and the MCP structured error of one call, after asserting they agree."""
    rest = client.post("/v1/" + tool, json=args, headers=HEADERS).json() if path == "/mcp" else None
    result = call(client, tool, args, path=path)
    assert result["isError"]
    if rest is not None:
        assert result["structuredContent"] == rest
    return result["structuredContent"]


# spec: ANYM-544
def test_an_ordinary_backend_conflict_never_relays_the_callers_payload(gateway):
    app, _, failures = gateway
    sent = {**SENT, "text": f"{TEXT} {FILENAME} {PHONE}"}
    failures["send_message"] = (409, {
        "error": "conflict", "request_id": "c" * 32, "hint": f"Resend {TEXT}",
        "next": {"tool": "messages_search", "arguments": {"query": FILENAME}},
        "candidates": [{"id": "chat-1", "label": TEXT, "kind": "chat"},
                       {"id": "chat-2", "label": "Synthetic 2", "kind": "chat",
                        "next": {"tool": "messages_search", "arguments": {"query": PHONE}}},
                       {"id": "chat-3", "label": "Synthetic 3", "kind": "chat", "next": SEARCH}]})
    with TestClient(app) as client:
        body = relayed(client, "messages_send_message", sent)
    shown = json.dumps({k: body.get(k) for k in ("hint", "next", "candidates")})
    assert not any(canary in shown for canary in CANARIES)
    assert "hint" not in body and "next" not in body
    # Only the clean candidate keeps its continuation; the one labelled with the caller's text is gone.
    assert body["candidates"] == [{"id": "chat-2", "label": "Synthetic 2", "kind": "chat"},
                                  {"id": "chat-3", "label": "Synthetic 3", "kind": "chat", "next": SEARCH}]


# spec: ANYM-544
def test_a_phone_shaped_hint_or_next_argument_is_dropped_even_when_the_caller_never_sent_it(gateway):
    app, _, failures = gateway
    failures["search"] = (409, {"error": "conflict", "hint": f"Call {PHONE}",
                                "next": {"tool": "messages_search", "arguments": {"query": PHONE}}})
    with TestClient(app) as client:
        body = relayed(client, "messages_search", {"query": "lookup"})
    assert "hint" not in body and "next" not in body and PHONE not in json.dumps(body)


# spec: ANYM-544
def test_a_relayed_next_into_another_module_is_dropped_and_an_emitted_one_runs(gateway):
    app, requests, failures = gateway
    other = {"tool": "calendar_calendars", "arguments": {}}
    with TestClient(app) as client:
        for path in ("/mcp", "/mcp/modules/messaging"):
            failures["search"] = (409, {"error": "conflict", "next": other})
            assert "next" not in relayed(client, "messages_search", {"query": "lookup"}, path)
            failures["search"] = (409, {"error": "conflict", "next": SEARCH})
            following = relayed(client, "messages_search", {"query": "lookup"}, path)["next"]
            assert following == SEARCH
            failures.pop("search")
            assert not call(client, following["tool"], following["arguments"], path=path).get("isError")
    assert ("calendar", "calendars") not in requests


# spec: ANYM-544
def test_selection_required_keeps_a_mutating_next_on_candidates_only(gateway):
    app, _, failures = gateway
    mutating = {"tool": "messages_send_message", "arguments": {**SENT, "chat_id": "chat-1", "text": "other reply"}}
    failures["send_message"] = (409, {"error": "selection_required", "request_id": "c" * 32, "next": mutating,
                                      "candidates": [{"id": "chat-1", "label": "Synthetic", "kind": "chat",
                                                      "next": mutating}]})
    with TestClient(app) as client:
        body = relayed(client, "messages_send_message", SENT)
    assert "next" not in body
    assert body["candidates"] == [{"id": "chat-1", "label": "Synthetic", "kind": "chat", "next": mutating}]
    validate_error(body, read_only=frozenset(n for n in TOOLS if CATALOG[n]["annotations"].get("readOnlyHint")))


# spec: ANYM-544
@pytest.mark.parametrize("bad", [
    {"tool": "calls_stop_call", "arguments": {"call_id": "x"}},
    {"tool": "messages_search", "arguments": {"query": "x", "phone": "synthetic"}},
    {"tool": "messages_search", "arguments": {"query": 7}},
    {"tool": "messages_search"},
    "messages_search",
])
def test_a_relayed_next_the_caller_cannot_run_as_given_is_dropped_never_repaired(gateway, bad):
    app, _, failures = gateway
    failures["search"] = (409, {"error": "conflict", "next": bad,
                                "candidates": [{"id": "chat-1", "label": "Synthetic", "kind": "chat", "next": bad}]})
    with TestClient(app) as client:
        body = relayed(client, "messages_search", {"query": "lookup"})
    assert "next" not in body and body["candidates"] == [{"id": "chat-1", "label": "Synthetic", "kind": "chat"}]


# spec: ANYM-544
def test_more_than_ten_candidates_are_cut_and_more_needs_a_read_only_next(gateway):
    app, _, failures = gateway
    many = [{"id": f"chat-{i}", "label": f"Synthetic {i}", "kind": "chat"} for i in range(12)]
    with TestClient(app) as client:
        failures["search"] = (409, {"error": "selection_required", "next": SEARCH, "candidates": many})
        listed = relayed(client, "messages_search", {"query": "lookup"})
        failures["search"] = (409, {"error": "selection_required", "candidates": many, "more": True})
        bare = relayed(client, "messages_search", {"query": "lookup"})
    assert listed["candidates"] == many[:10] and listed["more"] is True and listed["next"] == SEARCH
    assert len(bare["candidates"]) == 10 and "more" not in bare


# spec: ANYM-544
def test_a_backend_file_name_phone_label_or_short_payload_word_never_reaches_hint_next_or_candidates(gateway):
    app, _, failures = gateway
    failures["search"] = (409, {
        "error": "conflict", "hint": f"See {FILENAME}",
        "next": {"tool": "messages_search", "arguments": {"query": FILENAME}},
        "candidates": [{"id": "chat-1", "label": PHONE, "kind": "chat"},
                       {"id": "chat-2", "label": "Reply ok later", "kind": "chat"},
                       {"id": "chat-3", "label": "Booking", "kind": "chat", "last_activity": "see notes"},
                       {"id": "chat-4", "label": "Synthetic 4", "kind": "chat",
                        "last_activity": "2026-10-26T08:11:07Z"}]})
    with TestClient(app) as client:
        body = relayed(client, "messages_search", {"query": "ok"})
    assert "hint" not in body and "next" not in body
    # A short caller word counts as a whole word only ("Booking" stays); a malformed last_activity is dropped.
    assert body["candidates"] == [{"id": "chat-3", "label": "Booking", "kind": "chat"},
                                  {"id": "chat-4", "label": "Synthetic 4", "kind": "chat",
                                   "last_activity": "2026-10-26T08:11:07Z"}]


# spec: ANYM-544
@pytest.mark.parametrize("bad, good", [
    ({"tool": "messages_get_updates", "arguments": {"start_from": "now"}},
     {"tool": "messages_get_updates", "arguments": {"start_from": "beginning"}}),
    ({"tool": "messages_send_message", "arguments": {**SEND, "request_id": PLACEHOLDER_REQUEST_ID}},
     {"tool": "messages_send_message", "arguments": {**SEND, "request_id": "req-synthetic-2"}}),
])
def test_a_relayed_next_passes_the_same_argument_admission_as_the_call(gateway, bad, good):
    app, _, failures = gateway
    with TestClient(app) as client:
        failures["search"] = (409, {"error": "conflict", "next": bad})
        dropped = relayed(client, "messages_search", {"query": "lookup"})
        failures["search"] = (409, {"error": "conflict", "next": good})
        kept = relayed(client, "messages_search", {"query": "lookup"})
        refused = call(client, bad["tool"], bad["arguments"])
    assert "next" not in dropped and kept["next"] == good and refused["isError"]


# spec: ANYM-544
@pytest.mark.parametrize("name", ["canary-report.sqlite3", "canary-report.c", "canary clip.7z", "Canary.TAR"])
def test_a_backend_file_name_with_any_extension_never_reaches_hint_next_or_candidates(gateway, name):
    app, _, failures = gateway
    failures["search"] = (409, {"error": "conflict", "hint": f"See {name}",
                                "next": {"tool": "messages_search", "arguments": {"query": name}},
                                "candidates": [{"id": "chat-1", "label": name, "kind": "chat"},
                                               {"id": "chat-2", "label": "Synthetic 2", "kind": "chat"}]})
    with TestClient(app) as client:
        body = relayed(client, "messages_search", {"query": "lookup"})
    assert "hint" not in body and "next" not in body
    assert body["candidates"] == [{"id": "chat-2", "label": "Synthetic 2", "kind": "chat"}]
