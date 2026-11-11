"""A stale cached tool list is detectable (comparable catalog) and recoverable (current fields, once-only hints)."""
import hashlib
import json
import re

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway import diagnostics
from agent_gateway.app import create_app
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
    # A type failure is neither a required nor an unknown field: it says nothing of a stale tool list, but since
    # ANYM-984 it carries the tool's fields like every schema refusal.
    (tmp_path / "second").mkdir()
    rest, _ = post_both(calls_app(tmp_path / "second", []), "calls_dial_call",
                        {"brief_id": 7, "destination_source": "owner_named"})
    assert rest.json() == {"error": "invalid_arguments", "next_action": "check_parameters",
                           "field_errors": [{"field": "brief_id", "reason": "type", "expected": "string"}],
                           "tool_fields": diagnostics.tool_fields(CATALOG["calls_dial_call"])}


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


SHA_A, SHA_B = "a" * 40, "b" * 40
FETCH = "messages_fetch_subscription_events"
FETCH_ARGS = {"subscription_id": "subscription-1"}
OLD_MODULE = {"permissions": ["read"], "tool_manifest": {"version": "9.9.9", "names": ["search"]},
              "build": {"source_sha": SHA_A, "started_at": "2026-11-02T10:00:00.000000Z"}}
MISSING_OPERATION = {
    "error": "operation_not_in_module_build", "module": "messaging", "operation": "fetch_subscription_events",
    "module_manifest": "9.9.9", "module_build": SHA_A, "next_action": "refresh_tools_after_release",
    "hint": diagnostics.OPERATION_MISSING_HINT,
}
UNKNOWN_BODY = {"error": "not_found_or_forbidden", "next_action": "refresh_tool_list_once",
                "hint": "If the tool is still absent after a refresh, this key does not have it; do not ask again."}


def module_app(tmp_path, capabilities, tools=(FETCH, "platform_capabilities"), grants=("read",), second_key=False):
    """A messaging backend stub that answers capabilities with `capabilities` and records every other path."""
    calls = []

    def backend(request):
        if request.url.path == "/v1/capabilities":
            calls.append("capabilities")
            if isinstance(capabilities, Exception):
                raise capabilities
            return httpx.Response(200, json=capabilities)
        calls.append(request.url.path)
        return httpx.Response(200, json={"synthetic": True})

    app, path, value = configured(tmp_path, backend, module="messaging", tools=list(tools), grants=list(grants))
    if second_key:
        import copy
        other = copy.deepcopy(value["principals"][0])
        other.update(id="agent-b", key_sha256=hashlib.sha256(b"gateway-b").hexdigest(), profile="narrow")
        value["profiles"]["narrow"] = {"tools": ["platform_capabilities"], "actions": {}}
        value["principals"].append(other)
        value["revision"] += 1
        path.write_text(json.dumps(value))
    return app, calls


def fresh(tmp_path, name):
    (tmp_path / name).mkdir()
    return tmp_path / name


def call_both(client, name, values, headers=HEADERS):
    rest = client.post("/v1/" + name, json=values, headers=headers)
    mcp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": name, "arguments": values}},
                      headers={**headers, "Accept": "application/json, text/event-stream"}).json()["result"]
    strip = lambda body: {k: v for k, v in body.items() if k != "gateway_request_id"}  # noqa: E731 - differs per request
    assert mcp["isError"] and strip(mcp["structuredContent"]) == strip(rest.json())
    return rest


# spec: ANYM-662
def test_a_tool_the_module_build_lacks_names_the_module_manifest_and_build_over_both_transports(tmp_path):
    for case, capabilities, expected in (
        ("missing", OLD_MODULE, MISSING_OPERATION),
        ("absent manifest", {"permissions": ["read"], "build": OLD_MODULE["build"]},
         {**MISSING_OPERATION, "module_manifest": None}),
        ("changed build, same gap", {**OLD_MODULE, "build": {"source_sha": SHA_B, "started_at": "2026-11-02T11:00:00Z"}},
         {**MISSING_OPERATION, "module_build": SHA_B}),
        ("no build reported", {key: value for key, value in OLD_MODULE.items() if key != "build"},
         {**MISSING_OPERATION, "module_build": None}),
    ):
        app, calls = module_app(fresh(tmp_path, case.replace(" ", "_").replace(",", "")), capabilities)
        with TestClient(app) as client:
            rest = call_both(client, FETCH, FETCH_ARGS)
        assert rest.status_code == 403 and rest.json() == expected, case
        assert set(calls) == {"capabilities"}, case


# spec: ANYM-662
def test_a_tool_hidden_for_a_missing_action_keeps_todays_bare_denial(tmp_path):
    # The manifest lists the operation; the key lacks the send_file action, so this is not an "old build" claim.
    capabilities = {"permissions": ["read", "send", "upload"], "tool_manifest": {"version": "1", "names": ["prepare_file_upload"]}}
    app, calls = module_app(tmp_path, capabilities, tools=("messages_prepare_file_upload", "platform_capabilities"),
                            grants=("read", "send", "upload"))
    with TestClient(app) as client:
        rest = call_both(client, "messages_prepare_file_upload", {"account": "a", "chat_id": "c", "filename": "f.txt",
                                                                    "size": 1, "sha256": "0" * 64, "mime_type": "text/plain",
                                                                    "request_id": "request-12345678"})
    assert rest.status_code == 403 and "error" in rest.json() and rest.json().get("error") != "operation_not_in_module_build"


# spec: ANYM-662
def test_outside_the_policy_the_same_identical_body_and_no_backend_call(tmp_path):
    app, calls = module_app(tmp_path, OLD_MODULE, second_key=True)
    other = {"Authorization": "Bearer gateway-b"}
    with TestClient(app) as client:
        # Key A may call the tool and learns the gap; key B (profile without it), an unknown name and a tool of an
        # undeployed module all get the one identical body, and none of them reaches a backend.
        assert call_both(client, FETCH, FETCH_ARGS).json() == MISSING_OPERATION
        before = list(calls)
        bodies = [call_both(client, name, FETCH_ARGS, other).content for name in (FETCH, "no_such_tool", "calendar_slots")]
        assert len(set(bodies)) == 1 and json.loads(bodies[0]) == {**UNKNOWN_BODY, "next": {"tool": "platform_capabilities", "arguments": {}}}
        assert calls == before
        # No metadata of the module for key B either: no entry, no build, no query to the module.
        result = client.post("/v1/platform_capabilities", json={}, headers=other).json()
        assert result["unavailable"] == [] and result["backend_builds"] == {} and calls == before
        mine = client.post("/v1/platform_capabilities", json={}, headers=HEADERS).json()
    assert mine["unavailable"] == [{"module": "messaging", "state": "outdated", "reason": "operation_not_in_module_build",
                                    "module_manifest": "9.9.9", "module_build": SHA_A, "required_by": [FETCH]}]
    assert mine["backend_builds"] == {"messaging": OLD_MODULE["build"]}
    assert mine["gateway"]["build"].keys() == {"source_sha", "started_at"}


# spec: ANYM-985
def test_a_module_whose_build_lacks_a_tool_the_key_may_use_is_degraded_beside_the_tools_it_still_has(tmp_path):
    app, calls = module_app(tmp_path, OLD_MODULE, tools=(FETCH, "messages_search", "platform_capabilities"), second_key=True)
    other = {"Authorization": "Bearer gateway-b"}
    with TestClient(app) as client:
        mine = client.post("/v1/platform_capabilities", json={}, headers=HEADERS).json()
        before = list(calls)
        theirs = client.post("/v1/platform_capabilities", json={}, headers=other).json()
    rows = {row["id"]: row for row in mine["modules_available"]}
    assert rows["messaging"]["health"] == {"state": "degraded", "reason": "operation_not_in_module_build"}
    assert FETCH not in mine["tools"] and "messages_search" in mine["tools"]
    # Key B's profile holds no messaging tool: no row, no health word, and no query to the module for it.
    assert theirs["modules_available"] == [] and "degraded" not in json.dumps(theirs) and calls == before


# spec: ANYM-662
def test_a_backend_that_is_down_keeps_backend_unavailable_and_is_listed_as_unavailable(tmp_path):
    app, _ = module_app(tmp_path, httpx.ConnectError("synthetic"))
    with TestClient(app) as client:
        failure = call_both(client, FETCH, FETCH_ARGS)
        result = client.post("/v1/platform_capabilities", json={}, headers=HEADERS).json()
    assert failure.json()["error"] == "backend_unavailable"
    assert result["unavailable"] == [{"module": "messaging", "state": "unavailable", "reason": "backend_unavailable",
                                      "module_manifest": None, "module_build": None, "required_by": [FETCH]}]
    assert result["backend_builds"] == {}


def revoking_app(tmp_path, mutate, tools=(FETCH, "platform_capabilities")):
    """A messaging stub that changes the key's access while the gateway reads the module's capabilities."""
    held, paths = {}, []

    def backend(request):
        if request.url.path == "/v1/capabilities":
            paths.append("capabilities")
            held["value"]["revision"] += 1
            mutate(held["value"])
            held["path"].write_text(json.dumps(held["value"]))
            return httpx.Response(200, json=OLD_MODULE)
        paths.append(request.url.path)
        return httpx.Response(200, json={"synthetic": True})

    app, path, value = configured(tmp_path, backend, module="messaging", tools=list(tools), grants=["read"])
    held.update(path=path, value=value)
    return app, paths


def drop_tool(value):
    value["profiles"]["reader"]["tools"].remove(FETCH)


def drop_tool_and_platform(value):
    value["profiles"]["reader"]["tools"][:] = []


def drop_grant(value):
    value["principals"][0]["grants"] = {"messaging": []}


def disable_key(value):
    value["principals"][0]["enabled"] = False


def over(client, transport, name=FETCH, values=FETCH_ARGS):
    """One call over REST or MCP; the status and the error body the agent sees."""
    if transport == "rest":
        response = client.post("/v1/" + name, json=values, headers=HEADERS)
        return response.status_code, response.json()
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": name, "arguments": values}},
                           headers={**HEADERS, "Accept": "application/json, text/event-stream"})
    assert response.json()["result"]["isError"]
    return response.status_code, response.json()["result"]["structuredContent"]


# spec: ANYM-662
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("mutate,status,expected", [
    (drop_tool, 403, {**UNKNOWN_BODY, "next": {"tool": "platform_capabilities", "arguments": {}}}),
    (drop_tool_and_platform, 403, UNKNOWN_BODY),
    (drop_grant, 403, {**UNKNOWN_BODY, "next": {"tool": "platform_capabilities", "arguments": {}}}),
    (disable_key, 401, {"error": "unauthorized"}),
], ids=["tool removed", "profile emptied", "grant removed", "key disabled"])
def test_a_mid_request_revocation_over_either_transport_leaves_the_exact_denial_and_no_metadata(tmp_path, transport, mutate, status, expected):
    # A fresh app and policy for every transport and mutation: the key is re-authorized after the capability read,
    # before any detail is built, and the access it lost is gone by then.
    app, paths = revoking_app(fresh(tmp_path, "case"), mutate)
    with TestClient(app) as client:
        code, body = over(client, transport)
    # A tool error over MCP is an HTTP 200 result with isError; over REST the denial's own status.
    assert body == expected and code == (status if transport == "rest" else 200)
    assert paths == ["capabilities"] and "module_manifest" not in str(body) and SHA_A not in str(body)


# spec: ANYM-662
def test_a_policy_change_in_between_is_policy_changed_not_the_detail(tmp_path):
    app, paths = revoking_app(tmp_path, lambda value: None)
    with TestClient(app) as client:
        rest = client.post("/v1/" + FETCH, json=FETCH_ARGS, headers=HEADERS)
    assert rest.status_code == 409 and rest.json().get("error") != "operation_not_in_module_build" and paths == ["capabilities"]


# spec: ANYM-662
def test_a_key_that_may_not_call_platform_capabilities_is_never_pointed_to_it(tmp_path):
    app, calls = module_app(tmp_path, OLD_MODULE, tools=(FETCH,))
    with TestClient(app) as client:
        rest = call_both(client, FETCH, FETCH_ARGS)
    assert rest.status_code == 403 and rest.json()["error"] == "operation_not_in_module_build"
    assert rest.json()["hint"] == diagnostics.OPERATION_MISSING_HINT_WITHOUT_PLATFORM
    assert "platform_capabilities" not in rest.text and "next" not in rest.json()


# spec: ANYM-662
@pytest.mark.parametrize("started_at,kept", [
    ("2026-11-02T10:00:00.000000Z", True), ("2026-11-02T10:00:00+01:00", True), ("2026-99-99T99:99:99Z", False),
    ("2026-11-02T25:00:00Z", False), ("2026-11-02T10:00:00+99:99", False), ("2026-11-02", False), (5, False),
    ("2026-11-02T10:00:00+00:99", False), ("2026-11-02T10:00:00+01:60", False), ("2026-11-02T10:00:00-23:59", True),
])
def test_build_timestamps_are_parsed_not_just_shaped(started_at, kept):
    summary = diagnostics.build_summary({"build": {"source_sha": SHA_A, "started_at": started_at}})
    assert summary == {"source_sha": SHA_A, "started_at": started_at if kept else None}


# spec: ANYM-662
def test_malformed_builds_and_diagnostic_failures_never_break_listings(tmp_path, monkeypatch):
    malformed = {**OLD_MODULE, "tool_manifest": {"version": "1", "names": ["fetch_subscription_events"]},
                 "build": {"source_sha": "zzz", "started_at": {"x": 1}}}
    app, _ = module_app(tmp_path, malformed)
    with TestClient(app) as client:
        result = capabilities(client)
        listed = [t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]]
    assert result["backend_builds"] == {"messaging": {"source_sha": None, "started_at": None}} and FETCH in listed
    string_build = {**malformed, "build": "garbage"}
    app, _ = module_app(fresh(tmp_path, "second"), string_build)
    with TestClient(app) as client:
        assert capabilities(client)["backend_builds"] == {"messaging": None}

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic")

    app, _ = module_app(fresh(tmp_path, "third"), {**OLD_MODULE, "tool_manifest": {"version": "1", "names": ["fetch_subscription_events"]}})
    monkeypatch.setattr(diagnostics, "build_summary", broken)
    with TestClient(app) as client:
        result = capabilities(client)
        assert FETCH in result["tools"] and result["unavailable"] == [] and result["backend_builds"] == {}
    # The same failure while explaining a missing operation leaves today's denial, not a server error.
    app, _ = module_app(fresh(tmp_path, "fourth"), OLD_MODULE)
    with TestClient(app) as client:
        rest = client.post("/v1/" + FETCH, json=FETCH_ARGS, headers=HEADERS)
    assert rest.status_code == 403 and rest.json().get("error") != "operation_not_in_module_build"


# spec: ANYM-662
def test_one_timed_out_backend_next_to_a_healthy_one_keeps_the_healthy_listing(tmp_path):
    import copy
    handlers = {
        "messaging": lambda request: httpx.Response(200, json={**OLD_MODULE, "tool_manifest": {"version": "1", "names": ["fetch_subscription_events"]}}),
        "calendar": lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("synthetic")),
    }
    app, path, value = configured(tmp_path, handlers["messaging"], module="messaging", tools=[FETCH, "calendar_calendars", "platform_capabilities"],
                                  grants=["read"])
    principal = value["principals"][0]
    value["deployment"] = {"modules": ["messaging", "calendar"], "backends": {m: {"url": "http://backend.test"} for m in handlers}}
    principal["grants"] = {"messaging": ["read"], "calendar": ["agenda:read", "availability:read"]}
    principal["credentials"]["calendar"] = copy.deepcopy(principal["credentials"]["messaging"])
    value["revision"] += 1
    path.write_text(json.dumps(value))
    app = create_app(path, "http://testserver", transports={m: httpx.MockTransport(h) for m, h in handlers.items()})
    with TestClient(app) as client:
        result = capabilities(client)
    assert FETCH in result["tools"] and "calendar_calendars" not in result["tools"]
    assert result["backend_states"] == {"messaging": "ready", "calendar": "unavailable"}
    assert [(e["module"], e["state"]) for e in result["unavailable"]] == [("calendar", "unavailable")]
    assert set(result["backend_builds"]) == {"messaging"}


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
    assert set(body) == {"error", "next_action", "field_errors", "tool_fields"} and body["next_action"] == "check_parameters"
    assert "tool_list_stale" not in body and "use_instead" not in body  # ANYM-984: the fields yes, a stale-list claim no
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


# spec: ANYM-452
def test_a_singular_field_of_a_plural_one_names_it_and_does_not_call_the_list_stale():
    # `chat` on search_people: the tool has `chats`; the current tool list is not stale. (ANYM-646 gave list_chats and
    # search_people `account`, so the example moved to the one singular they still lack.)
    tool = CATALOG["messages_search_people"]
    detail = {"error": "invalid_arguments", "next_action": "check_parameters",
              "field_errors": [{"field": "chat", "reason": "unknown_field"}]}
    hinted = diagnostics.stale_arguments_hint(tool, detail)
    assert hinted["use_instead"] == {"chat": "chats"} and "chats" in hinted["tool_fields"]["optional"]
    assert "tool_list_stale" not in hinted and "owner_hint" not in hinted
    # One unknown field without a near name keeps the stale-list hint and names no replacement.
    detail["field_errors"].append({"field": "voice_style", "reason": "unknown_field"})
    hinted = diagnostics.stale_arguments_hint(tool, detail)
    assert hinted["tool_list_stale"] == "likely" and "use_instead" not in hinted
