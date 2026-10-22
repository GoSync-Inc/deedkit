"""Agent-chosen gateway modules: connection paths and the in-session selection only narrow (ANYM-271)."""
import hashlib
import itertools
import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG, public
from test_policy import config
from test_uploads import chunk, upload_setup  # noqa: F401 - the shared upload fixture

MODULES = ("calendar", "calls", "messaging")
PLATFORM = ["platform_capabilities", "platform_select_modules"]
MODULE_TOOLS = {
    "messaging": ["messages_capabilities", "messages_search", "messages_list_chats"],
    "calendar": ["calendar_calendars", "calendar_slots"],
    "calls": ["calls_capabilities", "calls_list_calls"],
}
GRANTS = {"messaging": ["read"], "calendar": ["agenda:read", "availability:read"], "calls": ["calls:read"]}
ACCEPT = {"Accept": "application/json, text/event-stream"}
SELECT_ERROR = {"error": "module_not_selected", "module": "messaging",
                "next_action": "select_module_or_use_its_connection_path"}


def principal(tmp_path, name, modules):
    credentials = {}
    for module in modules:
        key = tmp_path / f"{name}-{module}.key"
        key.write_text(f"backend-{module}-{name}")
        key.chmod(0o600)
        credentials[module] = {"key_file": str(key), "key_sha256": hashlib.sha256(key.read_bytes()).hexdigest()}
    return {
        "id": name, "key_sha256": hashlib.sha256(f"gateway-{name}".encode()).hexdigest(), "profile": "reader",
        "enabled": True, "expires_at": "2099-01-01T00:00:00Z",
        "grants": {m: GRANTS[m] for m in modules}, "credentials": credentials, "actions": {},
    }


@pytest.fixture
def gateway(tmp_path):
    """Three deployed modules, two principals (`a`, `b`) and a log of every backend request."""
    value = config()
    value["deployment"] = {"modules": list(MODULES),
                           "backends": {m: {"url": f"http://{m}.test"} for m in MODULES}}
    value["profiles"]["reader"] = {"tools": PLATFORM + [t for m in MODULES for t in MODULE_TOOLS[m]], "actions": {}}
    value["principals"] = [principal(tmp_path, "a", MODULES), principal(tmp_path, "b", MODULES)]
    path = tmp_path / "policy.json"
    requests, down = [], set()

    def handler(module):
        def respond(request):
            requests.append((module, request.url.path))
            if request.url.path == "/v1/capabilities" and module in down:
                return httpx.Response(503, json={"error": "service_unavailable"})
            if request.url.path == "/v1/capabilities":
                return httpx.Response(200, json={"permissions": GRANTS[module]})
            return httpx.Response(200, json={"items": [], "coverage": "unknown"})
        return httpx.MockTransport(respond)

    def write(change=None):
        if change:
            change(value)
        value["revision"] += 1
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    value["revision"] = 0
    write()
    write.down = down  # modules whose backend reports not ready
    app = create_app(path, "http://testserver", transports={m: handler(m) for m in MODULES})
    return app, value, write, requests


def mcp(client, method, params=None, *, path="/mcp", key="a", headers=None):
    return client.post(path, json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}},
                       headers={"Authorization": f"Bearer gateway-{key}", **ACCEPT, **(headers or {})})


def listed(client, path="/mcp", key="a"):
    result = mcp(client, "tools/list", path=path, key=key).json()["result"]
    return [tool["name"] for tool in result["tools"]], result["_meta"]["deedkit.discovery"]


def call(client, name, arguments=None, *, path="/mcp", key="a"):
    return mcp(client, "tools/call", {"name": name, "arguments": arguments or {}}, path=path, key=key).json()["result"]


def calls_to(requests, module):
    return [p for m, p in requests if m == module]


# spec: ANYM-271
def test_path_selects_module_tools(gateway):
    app, value, _, requests = gateway
    with TestClient(app) as client:
        names, found = listed(client, "/mcp/modules/messaging")
    assert names == PLATFORM + MODULE_TOOLS["messaging"]
    assert found["tool_count"] == len(names)
    # An unselected module's backend is not asked for its capabilities.
    assert calls_to(requests, "calendar") == calls_to(requests, "calls") == []


# spec: ANYM-271
def test_plain_mcp_unchanged_without_new_tool(gateway):
    app, value, write, _ = gateway
    write(lambda v: v["profiles"]["reader"]["tools"].remove("platform_select_modules"))
    tools = value["profiles"]["reader"]["tools"]
    with TestClient(app) as client:
        names, found = listed(client)
        capabilities = call(client, "platform_capabilities")["structuredContent"]
    assert names == tools
    # The fingerprint is the one taken before module selection existed: the reviewed schemas of the same list.
    expected = hashlib.sha256(json.dumps([public(CATALOG[n], []) for n in sorted(tools)], ensure_ascii=False,
                                         sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert found["tools_sha256"] == capabilities["discovery"]["tools_sha256"] == expected
    assert capabilities["tools"] == tools


# spec: ANYM-271
def test_select_tool_additive(gateway):
    app, value, write, _ = gateway
    write(lambda v: v["profiles"]["reader"]["tools"].remove("platform_select_modules"))
    with TestClient(app) as client:
        before, before_found = listed(client)
        write(lambda v: v["profiles"]["reader"]["tools"].insert(1, "platform_select_modules"))
        after, after_found = listed(client)
        again, again_found = listed(client)
    assert [n for n in after if n not in before] == ["platform_select_modules"]
    assert [n for n in after if n != "platform_select_modules"] == before
    assert before_found["tools_sha256"] != after_found["tools_sha256"] == again_found["tools_sha256"]


def module_paths():
    yield "/mcp", set(MODULES)
    for size in (1, 2, 3):
        for chosen in itertools.combinations(MODULES, size):
            yield "/mcp/modules/" + ",".join(chosen), set(chosen)


# spec: ANYM-271
def test_selection_only_narrows(gateway):
    """Every profile × grant × credential × readiness × path combination: a path lists its modules' share of the
    plain list, and each combination starts from the complete principal."""
    app, value, write, _ = gateway
    complete = json.loads(json.dumps(value["principals"][0]))
    with TestClient(app) as client:
        for dropped_tools, no_grant, no_credential, not_ready in itertools.product(
                (None, "messaging", "calls"), (None, "calendar", "messaging"), (None, "calls", "calendar"),
                (None, "messaging", "calendar")):
            def change(v):
                v["profiles"]["reader"]["tools"] = PLATFORM + [
                    t for m in MODULES if m != dropped_tools for t in MODULE_TOOLS[m]]
                v["principals"][0] = principal_of(complete, no_grant, no_credential)
            write(change)
            write.down.clear()
            write.down.update({not_ready} - {None})
            agent = value["principals"][0]
            assert set(agent["credentials"]) == set(MODULES) - {no_credential}
            assert set(agent["grants"]) == set(MODULES) - {no_grant}
            plain, _ = listed(client)
            excluded = {dropped_tools, no_credential, not_ready} - {None}
            # A missing grant hides that module's scoped tools; scope-free ones (its capabilities) stay.
            assert not [n for n in plain if CATALOG[n]["module"] in excluded
                        or (CATALOG[n]["module"] == no_grant and CATALOG[n]["scopes"])]
            for path, chosen in module_paths():
                names, found = listed(client, path)
                assert names == [n for n in plain if CATALOG[n]["module"] in chosen | {"gateway"}], (path, excluded, no_grant)
                assert found["tool_count"] == len(names)
                # Calling through the path never reaches a profile tool its listing hides.
                for name in sorted({t for m in MODULES for t in MODULE_TOOLS[m]} - set(names)):
                    assert call(client, name, path=path)["isError"], (path, name)


def principal_of(complete, no_grant, no_credential):
    agent = json.loads(json.dumps(complete))
    agent["grants"] = {m: GRANTS[m] for m in MODULES if m != no_grant}
    agent["credentials"] = {m: c for m, c in agent["credentials"].items() if m != no_credential}
    return agent


# spec: ANYM-271
@pytest.mark.parametrize("path", [
    "/mcp/modules/unknown", "/mcp/modules/messaging,messaging", "/mcp/modules/", "/mcp/modules",
    "/mcp/modules/Messaging", "/mcp/modules/messaging,", "/mcp/modules/messaging/extra", "/mcp/modules/gateway",
])
def test_invalid_module_path(gateway, path):
    app, _, _, requests = gateway
    with TestClient(app) as client:
        response = mcp(client, "tools/list", path=path)
        unauthenticated = client.post(path, json={})
    assert (response.status_code, response.json()) == (404, {"error": "unknown_module"})
    assert unauthenticated.status_code == 401
    assert requests == []


# spec: ANYM-271
def test_in_session_selection_scope(gateway):
    app, _, _, _ = gateway
    with TestClient(app) as client:
        before, before_found = listed(client)
        other_before, _ = listed(client, key="b")
        path_before, _ = listed(client, "/mcp/modules/messaging,calendar")
        result = call(client, "platform_select_modules", {"modules": ["calendar"]})
        names, found = listed(client)
        capabilities = call(client, "platform_capabilities")["structuredContent"]
        other, _ = listed(client, key="b")
        on_path, _ = listed(client, "/mcp/modules/messaging,calendar")
        invalid = call(client, "platform_select_modules", {"modules": ["gateway"]})
        empty = call(client, "platform_select_modules", {"modules": []})
    assert not result.get("isError")
    assert result["structuredContent"] == {
        "selection_path": "/mcp", "effective_modules": ["calendar"], "tools_sha256": found["tools_sha256"],
        "takes_effect": "next_tools_list", "next_action": "refresh_connection_if_tools_list_differs"}
    assert names == PLATFORM + MODULE_TOOLS["calendar"]
    assert found["tools_sha256"] != before_found["tools_sha256"]
    assert capabilities["tools"] == names and capabilities["discovery"] == found
    assert (capabilities["selection_path"], capabilities["selected_modules"]) == ("/mcp", ["calendar"])
    assert other == other_before == before
    assert on_path == path_before
    assert invalid["isError"] and empty["isError"]
    assert invalid["structuredContent"]["error"] == empty["structuredContent"]["error"] == "invalid_arguments"


# spec: ANYM-271
def test_selection_narrows_a_module_path_and_never_widens(gateway):
    app, _, _, _ = gateway
    with TestClient(app) as client:
        result = call(client, "platform_select_modules", {"modules": ["calendar", "messaging"]},
                      path="/mcp/modules/messaging")
        names, _ = listed(client, "/mcp/modules/messaging")
        plain, _ = listed(client)
    assert result["structuredContent"]["effective_modules"] == ["messaging"]
    assert result["structuredContent"]["selection_path"] == "/mcp/modules/messaging"
    assert names == PLATFORM + MODULE_TOOLS["messaging"]
    assert len(plain) == len(PLATFORM) + sum(map(len, MODULE_TOOLS.values()))


# spec: ANYM-271
def test_selection_reset_on_revision(gateway):
    app, _, write, _ = gateway
    with TestClient(app) as client:
        before, _ = listed(client)
        call(client, "platform_select_modules", {"modules": ["calls"]})
        assert listed(client)[0] == PLATFORM + MODULE_TOOLS["calls"]
        write()
        after, _ = listed(client)
    assert after == before
    assert app.state.dispatch.selections == {}


# spec: ANYM-271
def test_selection_cleared_by_another_principals_request_after_revision(gateway):
    app, _, write, _ = gateway
    with TestClient(app) as client:
        call(client, "platform_select_modules", {"modules": ["calls"]})
        call(client, "platform_select_modules", {"modules": ["calls"]}, path="/mcp/modules/calls,messaging")
        assert len(app.state.dispatch.selections) == 2
        write(lambda v: v["principals"].pop(0))
        listed(client, key="b")
        assert app.state.dispatch.selections == {}
        write()
        call(client, "platform_select_modules", {"modules": ["calls"]}, key="b")
        write()
        assert client.post("/v1/messages_search", json={"query": "x"},
                           headers={"Authorization": "Bearer gateway-b"}).status_code == 200
        assert app.state.dispatch.selections == {}
        call(client, "platform_select_modules", {"modules": ["calls"]}, key="b")
        write()
        assert mcp(client, "ping", key="b").status_code == 200
        assert app.state.dispatch.selections == {}


# spec: ANYM-271
def test_select_tool_is_a_local_write_not_a_provider_mutation(gateway):
    app, value, write, _ = gateway
    hints = CATALOG["platform_select_modules"]["annotations"]
    assert hints == {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                     "openWorldHint": False}
    write(lambda v: v["profiles"]["reader"].update(tools=PLATFORM + MODULE_TOOLS["calendar"]))
    with TestClient(app) as client:
        assert call(client, "platform_capabilities")["structuredContent"]["provider_mutations"] is False


# spec: ANYM-271
def test_unselected_mcp_call_denied_without_backend(gateway):
    app, _, _, requests = gateway
    arguments = {"query": "synthetic"}
    with TestClient(app) as client:
        on_path = call(client, "messages_search", arguments, path="/mcp/modules/calendar")
        call(client, "platform_select_modules", {"modules": ["calendar"]})
        selected = call(client, "messages_search", arguments)
        other = call(client, "messages_search", arguments, key="b")
    assert on_path["isError"] and selected["isError"]
    assert on_path["structuredContent"] == selected["structuredContent"] == SELECT_ERROR
    assert not other.get("isError")
    # Only principal b's call reached the messaging backend.
    assert calls_to(requests, "messaging").count("/v1/search") == 1


# spec: ANYM-271
def test_rest_ignores_selection(gateway):
    app, _, _, requests = gateway
    headers = {"Authorization": "Bearer gateway-a"}
    with TestClient(app) as client:
        selected = client.post("/v1/platform_select_modules", json={"modules": ["calendar"]}, headers=headers)
        rest = client.post("/v1/messages_search", json={"query": "synthetic"}, headers=headers)
        mcp_call = call(client, "messages_search", {"query": "synthetic"})
        capabilities = client.post("/v1/platform_capabilities", json={}, headers=headers).json()
    assert selected.status_code == 200 and selected.json()["effective_modules"] == ["calendar"]
    assert rest.status_code == 200 and "/v1/search" in calls_to(requests, "messaging")
    assert mcp_call["structuredContent"] == SELECT_ERROR
    assert "messages_search" in capabilities["tools"]
    assert capabilities["selected_modules"] == ["calendar"]


# spec: ANYM-271
def test_module_path_upload_chunk_limit(upload_setup):  # noqa: F811
    app, _, _, _, dispatched = upload_setup
    headers = {"Authorization": "Bearer gateway-a", **ACCEPT}
    with TestClient(app) as client:
        def post(path, name, arguments):
            return client.post(path, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                            "params": {"name": name, "arguments": arguments}})
        allowed = post("/mcp/modules/messaging", "messages_upload_file_chunk", chunk())
        other = post("/mcp/modules/messaging", "messages_get_file_upload", {"upload_id": "x" * 65536})
        unselected = post("/mcp/modules/calendar", "messages_upload_file_chunk", chunk())
    assert allowed.status_code == 200 and not allowed.json()["result"].get("isError")
    assert other.status_code == 413
    assert unselected.status_code == 200 and unselected.json()["result"]["structuredContent"]["error"] == (
        "module_not_selected")
    assert len(dispatched) == 1


# spec: ANYM-271
def test_module_path_body_limit(gateway):
    app, _, _, requests = gateway
    with TestClient(app) as client:
        response = mcp(client, "initialize", {"padding": "x" * 65536}, path="/mcp/modules/messaging")
    assert (response.status_code, response.json()) == (413, {"error": "body_too_large"})
    assert requests == []


# spec: ANYM-271
def test_module_path_get_405(gateway):
    app, _, _, _ = gateway
    with TestClient(app) as client:
        response = client.get("/mcp/modules/messaging", headers={"Authorization": "Bearer gateway-a"})
        anonymous = client.get("/mcp/modules/messaging")
    assert response.status_code == 405 and response.headers["Allow"] == "POST"
    assert response.json() == {"error": "server_notifications_not_supported"}
    assert anonymous.status_code == 401


# spec: ANYM-271
def test_module_path_origin(gateway):
    app, _, _, requests = gateway
    with TestClient(app) as client:
        response = mcp(client, "tools/list", path="/mcp/modules/messaging", headers={"Origin": "http://evil.test"})
    assert (response.status_code, response.json()) == (403, {"error": "invalid_origin"})
    assert requests == []


# spec: ANYM-271
def test_module_path_revoked_key(gateway):
    app, _, write, requests = gateway
    with TestClient(app) as client:
        assert mcp(client, "tools/list", path="/mcp/modules/messaging").status_code == 200
        write(lambda v: v["principals"][0].update(enabled=False))
        listing = mcp(client, "tools/list", path="/mcp/modules/messaging")
        called = mcp(client, "tools/call", {"name": "messages_search", "arguments": {"query": "x"}},
                     path="/mcp/modules/messaging")
    assert listing.status_code == called.status_code == 401
    assert "/v1/search" not in calls_to(requests, "messaging")


# spec: ANYM-271
def test_modules_available_summary(gateway):
    app, _, write, _ = gateway
    # calls: no credential, so no usable tool; calendar: one of its two tools.
    write(lambda v: (v["principals"][0]["credentials"].pop("calls"),
                     v["principals"][0]["grants"].update(calendar=["agenda:read"])))
    with TestClient(app) as client:
        plain = call(client, "platform_capabilities")["structuredContent"]
        on_path = call(client, "platform_capabilities", path="/mcp/modules/messaging,calls")["structuredContent"]
    rows = {row["id"]: row for row in plain["modules_available"]}
    assert list(rows) == ["calendar", "messaging"]
    assert (rows["calendar"]["tool_count"], rows["messaging"]["tool_count"]) == (1, 3)
    assert all(row["selected"] for row in rows.values())
    assert rows["messaging"]["path"] == "/mcp/modules/messaging"
    assert all(isinstance(row["purpose"], str) and 0 < len(row["purpose"]) <= 120 for row in rows.values())
    assert (plain["selection_path"], plain["selected_modules"]) == ("/mcp", ["calendar", "messaging"])
    assert on_path["selection_path"] == "/mcp/modules/calls,messaging"
    assert on_path["selected_modules"] == ["messaging"]
    assert {row["id"]: row["selected"] for row in on_path["modules_available"]} == {
        "calendar": False, "messaging": True}
    assert on_path["tools"] == PLATFORM + MODULE_TOOLS["messaging"]
