"""Synthetic wire tests for safe diagnostics and effective discovery contracts."""
import hashlib
import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG
from test_policy import config
from test_transport import rpc


HEADERS = {"Authorization": "Bearer gateway-a"}


def configured(tmp_path, handler, *, module="calendar", tools=None, grants=None):
    value = config()
    value["deployment"] = {"modules": [module], "backends": {module: {"url": "http://backend.test"}}}
    if tools is not None:
        value["profiles"]["reader"]["tools"] = tools
    principal = value["principals"][0]
    principal["grants"] = {module: grants or ["agenda:read", "availability:read"]}
    key = tmp_path / "backend.key"
    key.write_text("synthetic-backend-key")
    key.chmod(0o600)
    principal["credentials"] = {module: {
        "key_file": str(key), "key_sha256": hashlib.sha256(key.read_bytes()).hexdigest(),
    }}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return create_app(path, "http://testserver", transports={module: httpx.MockTransport(handler)}), path, value


def test_query_timeout_diagnostics_match_on_rest_and_mcp_without_private_backend_text(tmp_path):
    attempts = []

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read"]})
        attempts.append(request.url.path)
        return httpx.Response(503, json={
            "error": "query_timeout", "request_id": "a" * 32,
            "next_action": "DO NOT PROPAGATE SQL OR TOKEN", "private": "synthetic-secret",
            "retryable": True,
        })

    app, _, _ = configured(tmp_path, backend, module="messaging",
                           tools=["messages_list_chats"], grants=["read"])
    with TestClient(app) as client:
        rest = client.post("/v1/messages_list_chats", json={"limit": 5}, headers=HEADERS)
        mcp = rpc(client, "tools/call", {"name": "messages_list_chats", "arguments": {"limit": 5}}).json()["result"]
    assert rest.status_code == 503 and mcp["isError"]
    assert rest.json() == mcp["structuredContent"] == {
        "error": "backend_unavailable", "backend_error": "query_timeout", "backend_status": 503,
        "request_id": "a" * 32, "phase": "operation", "retryable": True, "retry_after_seconds": 2,
        "next_action": "narrow_accounts_chat_types_or_awaiting_reply_days",
        "hint": "Narrow with accounts, chat_types or a smaller awaiting_reply_days, then retry once.",
    }
    assert attempts == ["/v1/list_chats", "/v1/list_chats"]


@pytest.mark.parametrize("body", [
    {"error": "private_stacktrace", "request_id": "synthetic-secret", "exception": "private"},
    ["private"], "private",
])
def test_unclassified_backend_failure_has_status_without_copying_body(tmp_path, body):
    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read"]})
        return httpx.Response(502, json=body)

    app, _, _ = configured(tmp_path, backend, module="messaging",
                           tools=["messages_list_chats"], grants=["read"])
    with TestClient(app) as client:
        response = client.post("/v1/messages_list_chats", json={}, headers=HEADERS)
    value = response.json()
    assert len(value.pop("gateway_request_id")) == 32
    assert value == {"error": "backend_unavailable", "backend_status": 502, "phase": "operation",
                     "retryable": True, "retry_after_seconds": 2, "next_action": "retry_after_delay"}


def test_mutation_http_failure_keeps_unknown_outcome_with_safe_correlation_and_no_retry(tmp_path):
    attempts = []

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["plan:execute"]})
        attempts.append(json.loads(request.content))
        return httpx.Response(503, json={"error": "service_unavailable", "request_id": "b" * 32})

    app, _, _ = configured(tmp_path, backend, tools=["calendar_apply"], grants=["plan:execute"])
    args = {"plan_id": "synthetic-plan", "request_id": "synthetic-request"}
    with TestClient(app) as client:
        response = client.post("/v1/calendar_apply", json=args, headers=HEADERS)
    assert response.json() == {
        "schema_version": 1, "error": "mutation_outcome_unknown", "outcome": "unknown",
        "automatic_retry": False, "backend_error": "service_unavailable", "backend_status": 503,
        "request_id": "b" * 32,
    }
    assert attempts == [args]


def test_discovery_metadata_tracks_effective_catalog_for_real_mcp_and_rest(tmp_path):
    permissions = ["agenda:read", "availability:read"]

    def backend(request):
        return httpx.Response(200, json={"permissions": permissions})

    app, path, value = configured(tmp_path, backend, tools=["platform_capabilities", "calendar_calendars"])
    with TestClient(app) as client:
        def discover():
            listed = rpc(client, "tools/list").json()["result"]
            rest = client.post("/v1/platform_capabilities", json={}, headers=HEADERS).json()
            mcp = rpc(client, "tools/call", {"name": "platform_capabilities", "arguments": {}}).json()["result"]
            assert mcp["structuredContent"] == rest
            assert rest["tools"] == [tool["name"] for tool in listed["tools"]]
            discovery = rest["discovery"]
            assert listed["_meta"]["deedkit.discovery"] == discovery
            assert discovery["tool_count"] == len(rest["tools"])
            assert discovery["policy_revision"] == value["revision"]
            assert len(discovery["tools_sha256"]) == 64
            assert discovery["notifications_supported"] is False
            assert discovery["next_action"] == "refresh_connection_if_tools_list_differs"
            assert rest["principal"] == rest["gateway_principal"] == "agent-a"
            return discovery

        initial = discover()
        value["profiles"]["reader"]["tools"].append("calendar_slots")
        value["revision"] += 1
        path.write_text(json.dumps(value))
        expanded = discover()
        assert expanded["tool_count"] == initial["tool_count"] + 1
        assert expanded["tools_sha256"] != initial["tools_sha256"]
        permissions.clear()
        revoked = discover()
        assert revoked["tools_sha256"] == initial["tools_sha256"]
        assert revoked["tool_count"] == initial["tool_count"]
        args = {"member_ids": ["synthetic-child"], "start": "2026-10-19T08:00:00Z",
                "end": "2026-10-19T09:00:00Z", "duration_minutes": 30, "refresh": False}
        assert rpc(client, "tools/call", {"name": "calendar_slots", "arguments": args}).json()["result"]["isError"]
        assert client.post("/v1/calendar_slots", json=args, headers=HEADERS).status_code == 403


def test_capabilities_explain_messaging_action_scope_and_preserve_backend_identity(tmp_path):
    def backend(request):
        return httpx.Response(200, json={
            "permissions": ["calls:prepare"], "principal": "backend-agent-synthetic",
            "owner_decision_write": False, "channels": ["phone"],
            "tool_manifest": {
                "version": "1.2.3", "names": ["prepare_external_call", "unreviewed_admin"],
                "discovery_action": "refresh_connection_if_tools_list_differs",
                "rest_fallback": "DO NOT ADVERTISE PRIVATE URL",
                "private": "DO NOT FORWARD",
            },
        })

    app, _, _ = configured(tmp_path, backend, module="calls", tools=["platform_capabilities", "calls_capabilities", "calls_prepare_external_call"],
                           grants=["calls:prepare"])
    with TestClient(app) as client:
        response = client.post("/v1/calls_capabilities", json={}, headers=HEADERS).json()
        platform = client.post("/v1/platform_capabilities", json={}, headers=HEADERS).json()
    assert response["principal"] == response["backend_principal"] == "backend-agent-synthetic"
    assert response["gateway_principal"] == "agent-a"
    assert response["owner_decision_write"] is False
    assert response["gateway_actions"] == []
    assert response["gateway_actions_scope"] == "messenger_operations"
    assert response["gateway_tools"] == ["calls_capabilities", "calls_prepare_external_call"]
    assert response["backend_tool_manifest"] == platform["backend_tool_manifests"]["calls"]
    assert response["backend_tool_manifest"] == {
        "version": "1.2.3", "tool_count": 2,
        "names_sha256": hashlib.sha256(b'["prepare_external_call","unreviewed_admin"]').hexdigest(),
        "discovery_action": "refresh_connection_if_tools_list_differs",
    }
    assert "tool_manifest" not in response
    assert "DO NOT" not in json.dumps(response) + json.dumps(platform)


def test_gateway_timeout_on_read_names_phase_retry_and_narrowing_hint(tmp_path):
    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read"]})
        raise httpx.ReadTimeout("synthetic slow archive")

    app, _, _ = configured(tmp_path, backend, module="messaging",
                           tools=["messages_list_chats"], grants=["read"])
    with TestClient(app) as client:
        value = client.post("/v1/messages_list_chats", json={"awaiting_reply": True}, headers=HEADERS).json()
    assert len(value.pop("gateway_request_id")) == 32
    assert value == {"error": "backend_unavailable", "reason": "gateway_timeout", "phase": "operation",
                     "retryable": True, "retry_after_seconds": 2,
                     "next_action": "narrow_accounts_chat_types_or_awaiting_reply_days",
                     "hint": "Narrow with accounts, chat_types or a smaller awaiting_reply_days, then retry once."}


def test_capability_failure_reports_that_a_mutation_was_not_sent(tmp_path):
    attempts = []

    def backend(request):
        attempts.append(request.url.path)
        return httpx.Response(503, json={"error": "service_busy", "request_id": "c" * 32})

    app, _, _ = configured(tmp_path, backend, tools=["calendar_apply"], grants=["plan:execute"])
    with TestClient(app) as client:
        value = client.post("/v1/calendar_apply", json={"plan_id": "synthetic-plan",
            "request_id": "synthetic-request"}, headers=HEADERS).json()
    assert value == {"error": "backend_unavailable", "backend_error": "service_busy", "backend_status": 503,
                     "request_id": "c" * 32, "phase": "capabilities", "operation_sent": False,
                     "retryable": True, "retry_after_seconds": 2, "next_action": "retry_after_delay"}
    assert attempts == ["/v1/capabilities"]


def test_calls_listings_need_read_scope_and_pass_owned_pages_through(tmp_path):
    permissions, seen = ["calls:read"], []
    page = {"items": [{"id": "brief-synthetic", "channel": "browser"}], "next_offset": None}

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": permissions})
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=page)

    tools = ["platform_capabilities", "calls_list_briefs", "calls_list_calls"]
    app, _, _ = configured(tmp_path, backend, module="calls", tools=tools, grants=["calls:read"])
    with TestClient(app) as client:
        listed = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
        assert set(listed) == set(tools)
        assert listed["calls_list_calls"]["annotations"]["readOnlyHint"] is True
        rest = client.post("/v1/calls_list_briefs", json={"limit": 5}, headers=HEADERS)
        mcp = rpc(client, "tools/call", {"name": "calls_list_calls", "arguments": {}}).json()["result"]
        assert rest.json() == mcp["structuredContent"] == page
        assert client.post("/v1/calls_list_calls", json={"limit": 101}, headers=HEADERS).status_code == 422
        permissions.clear()
        assert client.post("/v1/calls_list_calls", json={}, headers=HEADERS).status_code == 403
        assert [t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]] == [
            "platform_capabilities"
        ]
    assert seen == [
        ("/v1/list_briefs", {"limit": 5, "offset": 0}),
        ("/v1/list_calls", {"limit": 100, "offset": 0}),
    ]


# spec: ANYM-392
def test_tools_list_publishes_catalog_annotations_name_description_and_schemas_unchanged(tmp_path):
    def backend(request):
        return httpx.Response(200, json={"permissions": ["read", "cursor-write", "send"]})

    tools = ["platform_capabilities", "messages_list_chats", "messages_preview_message", "messages_ack_updates",
             "messages_send_message"]
    app, _, _ = configured(tmp_path, backend, module="messaging", tools=tools, grants=["read", "cursor-write", "send"])
    with TestClient(app) as client:
        listed = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    assert set(listed) == set(tools)
    for name in tools:
        # Built from the raw catalog entry, not public(), so a field public() dropped would fail here.
        fields = ("name", "title", "description", "inputSchema", "outputSchema", "annotations", "_meta")
        expected, tool = {k: CATALOG[name][k] for k in fields if k in CATALOG[name]}, listed[name]
        assert not CATALOG[name].get("action_group"), name
        assert tool["annotations"] == CATALOG[name]["annotations"], name
        if name == "platform_capabilities":
            # Only its description gains the catalog note; the reviewed text stays its prefix.
            assert tool.pop("description").startswith(expected.pop("description"))
        assert tool == expected, name
    assert listed["messages_send_message"]["annotations"] == {"readOnlyHint": False, "destructiveHint": True,
                                                              "idempotentHint": True, "openWorldHint": True}
