"""A stale cached tool list is detectable (comparable catalog) and recoverable (current fields, once-only hints)."""
import json
import re

import httpx
from starlette.testclient import TestClient

from agent_gateway import diagnostics
from agent_gateway.catalog import CATALOG
from test_diagnostics import HEADERS, configured
from test_strict_arguments import DIAL, calls_app, post_both
from test_transport import rpc


def calendar_app(tmp_path, tools):
    return configured(tmp_path, lambda request: httpx.Response(200, json={
        "permissions": ["agenda:read", "availability:read"]}), tools=tools)


def listed_catalog(client):
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    description = next(t["description"] for t in tools if t["name"] == "platform_capabilities")
    found = re.search(r" Catalog: ([0-9a-f]{12})\Z", description)
    assert found, description
    return found.group(1), description


def capabilities(client):
    rest = client.post("/v1/platform_capabilities", json={}, headers=HEADERS).json()
    mcp = rpc(client, "tools/call", {"name": "platform_capabilities", "arguments": {}}).json()["result"]
    assert mcp["structuredContent"] == rest
    return rest


# spec: ANYM-452
def test_capabilities_description_and_result_carry_the_same_catalog(tmp_path):
    app, path, value = calendar_app(tmp_path, ["platform_capabilities", "calendar_calendars"])
    with TestClient(app) as client:
        cached, description = listed_catalog(client)
        result = capabilities(client)
        assert result["discovery"]["catalog"] == cached
        assert cached == result["discovery"]["tools_sha256"][:12]
        assert "reconnect the DeedKit MCP client" in description
        assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                           headers={**HEADERS, "Accept": "application/json, text/event-stream"}
                           ).json()["result"]["_meta"]["deedkit.discovery"]["catalog"] == cached
        # A synthetic catalog change: the tool list the host cached no longer matches the result.
        value["profiles"]["reader"]["tools"].append("calendar_slots")
        value["revision"] += 1
        path.write_text(json.dumps(value))
        assert capabilities(client)["discovery"]["catalog"] != cached
        assert listed_catalog(client)[0] == capabilities(client)["discovery"]["catalog"]


# spec: ANYM-452
def test_a_key_with_only_platform_capabilities_still_gets_the_catalog(tmp_path):
    app, _, _ = calendar_app(tmp_path, ["platform_capabilities"])
    with TestClient(app) as client:
        assert listed_catalog(client)[0] == capabilities(client)["discovery"]["catalog"]


# spec: ANYM-452
def test_stale_arguments_get_current_fields(tmp_path):
    values = {"brief_id": "brief-1", "owner_instruction": "Call the clinic for me", "voice_style": "warm"}
    rest, mcp = post_both(calls_app(tmp_path, []), "calls_dial_call", values)
    expected = {
        "error": "invalid_arguments", "next_action": "check_parameters",
        "field_errors": [{"field": "destination_source", "reason": "required"},
                         {"field": "voice_style", "reason": "unknown_field"}],
        "tool_fields": {
            "required": [{"name": "brief_id", "type": "string"},
                         {"name": "destination_source", "type": "string", "values": ["owner_named", "agent_found"]}],
            "optional": ["owner_instruction", "authorization_reference", "destination_evidence"],
        },
        "tool_list_stale": "likely",
        "owner_hint": "Your DeedKit tool list may be out of date; reconnect the DeedKit MCP client.",
    }
    assert rest.status_code == 422 and rest.json() == expected
    assert mcp["isError"] and mcp["structuredContent"] == expected


# spec: ANYM-452
def test_missing_required_field_alone_and_nested_failures_stay_quiet(tmp_path):
    rest, _ = post_both(calls_app(tmp_path, []), "calls_dial_call", DIAL)
    body = rest.json()
    assert "tool_fields" in body and "tool_list_stale" not in body and "owner_hint" not in body
    # A type failure is neither a required nor an unknown field: the ANYM-295 shape is kept as is.
    (tmp_path / "second").mkdir()
    rest, _ = post_both(calls_app(tmp_path / "second", []), "calls_dial_call",
                        {"brief_id": 7, "destination_source": "owner_named"})
    assert rest.json() == {"error": "invalid_arguments", "next_action": "check_parameters",
                           "field_errors": [{"field": "brief_id", "reason": "type", "expected": "string"}]}


# spec: ANYM-452
def test_tool_fields_are_bounded_and_top_level_only():
    tool = {"inputSchema": {"type": "object", "required": ["kind", "free"], "properties": {
        "kind": {"enum": list("abcdefghi")}, "short": {"enum": ["x", "y"]},
        "free": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "nested": {"type": "object", "properties": {"inner": {"type": "string"}}},
        **{f"p{i}": {"type": "string"} for i in range(100)},
    }}}
    fields = diagnostics.tool_fields(tool)
    assert fields["required"] == [{"name": "kind", "type": "any"}, {"name": "free", "type": "integer"}]
    assert "short" in fields["optional"] and "inner" not in fields["optional"]
    assert len(fields["required"]) + len(fields["optional"]) == 64


# spec: ANYM-452
def test_unknown_tool_denial_is_identical_and_hints_once(tmp_path):
    app = calls_app(tmp_path, [])
    # nonexistent name; a catalog tool outside the key's profile; a tool of an undeployed module
    names = ["no_such_tool", "calls_list_calls", "calendar_slots"]
    assert all(name in CATALOG for name in names[1:])
    bodies = []
    with TestClient(app) as client:
        for name in names:
            rest = client.post("/v1/" + name, json={}, headers=HEADERS)
            mcp = rpc(client, "tools/call", {"name": name, "arguments": {}}).json()["result"]
            assert rest.status_code == 403 and mcp["isError"]
            assert mcp["structuredContent"] == rest.json()
            bodies.append(rest.content)
        denied = client.post("/v1/no_such_tool", json={}, headers={"Authorization": "Bearer wrong"})
    assert len(set(bodies)) == 1
    assert json.loads(bodies[0]) == {
        "error": "not_found_or_forbidden", "next_action": "refresh_tool_list_once",
        "hint": "If the tool is still absent after a refresh, this key does not have it; do not ask again.",
    }
    assert denied.status_code == 401 and "next_action" not in denied.json()


# spec: ANYM-452
def test_initialize_instructions_name_the_catalog_check(tmp_path):
    app, _, _ = calendar_app(tmp_path, ["platform_capabilities"])
    with TestClient(app) as client:
        instructions = rpc(client, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"},
        }).json()["result"]["instructions"]
    assert "platform_capabilities" in instructions and "catalog" in instructions.lower()
    assert "reconnect" in instructions


# spec: ANYM-452
def test_hint_failure_keeps_the_original_error(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("synthetic")

    app = calls_app(tmp_path, [])
    monkeypatch.setattr(diagnostics, "tool_fields", broken)
    rest, mcp = post_both(app, "calls_dial_call", {"brief_id": "brief-1", "voice_style": "warm"})
    expected = {"error": "invalid_arguments", "next_action": "check_parameters",
                "field_errors": [{"field": "destination_source", "reason": "required"},
                                 {"field": "voice_style", "reason": "unknown_field"}]}
    assert rest.status_code == 422 and rest.json() == expected and mcp["structuredContent"] == expected

    monkeypatch.setattr(diagnostics, "catalog_note", broken)
    (tmp_path / "second").mkdir()
    plain, _, _ = calendar_app(tmp_path / "second", ["platform_capabilities"])
    with TestClient(plain) as client:
        tools = rpc(client, "tools/list").json()["result"]["tools"]
        assert tools[0]["description"] == CATALOG["platform_capabilities"]["description"]
        assert capabilities(client)["tools"] == ["platform_capabilities"]


def prepare_app(tmp_path):
    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["calls:prepare"],
                                            "tool_manifest": {"names": ["prepare_call"]}})
        return httpx.Response(200, json={"synthetic": True})

    return configured(tmp_path, backend, module="calls", tools=["calls_prepare_call"], grants=["calls:prepare"])[0]


def assert_nested_failure_keeps_the_original_shape(tmp_path, brief):
    rest, mcp = post_both(prepare_app(tmp_path), "calls_prepare_call", {"brief": brief})
    body = rest.json()
    assert rest.status_code == 422 and mcp["isError"] and mcp["structuredContent"] == body
    assert set(body) == {"error", "next_action", "field_errors"} and body["next_action"] == "check_parameters"
    return body


# spec: ANYM-452
def test_nested_required_fields_get_no_stale_hint(tmp_path):
    body = assert_nested_failure_keeps_the_original_shape(tmp_path, {})
    assert {"field": "brief/goal", "reason": "required"} in body["field_errors"]


# spec: ANYM-452
def test_nested_unknown_fields_get_no_stale_hint(tmp_path):
    brief = {"goal": "Ask about opening hours", "counterparty": "Clinic", "success_fields": ["hours"]}
    body = assert_nested_failure_keeps_the_original_shape(tmp_path, {**brief, "voice_style": "warm"})
    assert body["field_errors"] == [{"field": "brief/voice_style", "reason": "unknown_field"}]


# spec: ANYM-452
def test_nested_unsafe_key_redacted_to_its_parent_gets_no_stale_hint(tmp_path):
    brief = {"goal": "Ask about opening hours", "counterparty": "Clinic", "success_fields": ["hours"]}
    body = assert_nested_failure_keeps_the_original_shape(tmp_path, {**brief, "unsafe key": 1})
    assert body["field_errors"] == [{"field": "brief", "reason": "unknown_field"}]
