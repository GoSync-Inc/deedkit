"""messages_capabilities over the gateway: a summary by default over MCP and REST, named sections, full by name (ANYM-270)."""
import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from test_action_ceiling import setup
from test_capability_actions import HEADERS, capabilities
from test_transport import rpc

SUPPORTED = {"synthetic": {"provider": "whatsmeow", "operations": ["rename_group"], "available_operations": ["rename_group"]}}


def call(client, arguments):
    body = rpc(client, "tools/call", {"name": "messages_capabilities", "arguments": arguments}).json()
    result = body["result"]
    return (json.loads(result["content"][0]["text"]) if result.get("isError") else result["structuredContent"]), bool(result.get("isError"))


# spec: ANYM-270
def test_gateway_capabilities_summary_sections_and_full(tmp_path):
    app, seen = capabilities(tmp_path, SUPPORTED)
    with TestClient(app) as client:
        full = client.post("/v1/messages_capabilities", json={"detail": "full"}, headers=HEADERS).json()
        assert full["gateway_actions"] == ["rename_group"] and "gateway_tools" in full
        assert call(client, {"detail": "full"}) == (full, False)
        summary, error = call(client, {})
        assert not error and summary["detail"] == "summary" and len(json.dumps(summary)) <= 1500
        assert "messenger_operations" in summary["sections"] and "gateway_actions" not in summary
        assert summary["gateway_principal"] == full["gateway_principal"]
        assert summary["gateway_tool_count"] == len(full["gateway_tools"])
        blocks, error = call(client, {"sections": ["messenger_operations"]})
        assert not error and blocks == {k: full[k] for k in full if k.startswith("gateway_actions")}
        rest = client.post("/v1/messages_capabilities", json={"sections": ["tool_manifest"]}, headers=HEADERS).json()
        assert rest == {k: full[k] for k in ("gateway_tools", "backend_tool_manifest") if k in full}
        # Every key the gateway returns today sits in exactly one section.
        names = summary["sections"]
        assert sum(len(call(client, {"sections": [n]})[0]) for n in names) == len(call(client, {"sections": names})[0])
        bad, error = call(client, {"sections": ["unknown"]})
        assert error and "invalid_filter" in json.dumps(bad)
        response = client.post("/v1/messages_capabilities", json={"sections": ["unknown"]}, headers=HEADERS)
        assert response.status_code == 400 and response.json()["error"] == "invalid_filter"
        assert seen == []


# spec: ANYM-270
def test_gateway_rest_capabilities_default_to_summary(tmp_path):
    app, _ = capabilities(tmp_path, SUPPORTED)
    with TestClient(app) as client:
        rest = client.post("/v1/messages_capabilities", json={}, headers=HEADERS).json()
        assert rest == call(client, {})[0] and rest["detail"] == "summary" and "gateway_actions" not in rest


def recording_app(tmp_path, old_backend):
    """The gateway against a messaging backend that records each capabilities body; an old
    backend (before `detail`) rejects the argument as c8edbe6 does."""
    _, value, path, _ = setup(tmp_path, SUPPORTED)
    value["profiles"]["reader"]["tools"].append("messages_capabilities")
    value["revision"] += 1
    path.write_text(json.dumps(value))
    bodies = []

    def backend(request):
        body = json.loads(request.content)
        bodies.append(body)
        if old_backend and body:
            return httpx.Response(400, json={"error": "invalid_parameters", "fields": sorted(body)})
        return httpx.Response(200, json={
            "permissions": value["principals"][0]["grants"]["messaging"], "chat_scope": {"chat_ids": None},
            "messenger_operations": {"supported_by_account": SUPPORTED}})
    return create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(backend)}), bodies


# spec: ANYM-270
@pytest.mark.parametrize("old_backend", [False, True])
def test_gateway_reads_full_backend_capabilities_by_name(tmp_path, old_backend):
    app, bodies = recording_app(tmp_path, old_backend)
    with TestClient(app) as client:
        for arguments in ({}, {"detail": "full"}, {"sections": ["messenger_operations"]}):
            bodies.clear()
            result, error = call(client, arguments)
            assert not error, result
            # One request naming the full payload; an old backend gets one more with none, as before.
            assert bodies == ([{"detail": "full"}, {}] if old_backend else [{"detail": "full"}])
        assert result == {"gateway_actions": ["rename_group"], "gateway_actions_scope": "messenger_operations",
                          "gateway_actions_by_account": {"synthetic": ["rename_group"]},
                          "gateway_actions_excluded_by_account": {},
                          "gateway_actions_basis": "granted_supported_and_available_for_account"}


# spec: ANYM-470
def test_gateway_summary_keeps_the_backend_key_expiry_and_each_owner_id(tmp_path):
    _, value, path, _ = setup(tmp_path, SUPPORTED)
    value["profiles"]["reader"]["tools"].append("messages_capabilities")
    value["revision"] += 1
    path.write_text(json.dumps(value))
    rows = [{"id": "telegram-demo", "platform": "telegram", "label": "Synthetic", "synthetic": True,
             "self_provider_id": "123456789"},
            {"id": "whatsapp-demo-1", "platform": "whatsapp", "label": "Synthetic", "synthetic": True,
             "self_provider_id": None}]

    def backend(request):
        return httpx.Response(200, json={
            "permissions": value["principals"][0]["grants"]["messaging"], "expires_at": "2027-04-19T22:37:50.000000Z",
            "accounts": rows, "chat_scope": {"chat_ids": None},
            "messenger_operations": {"supported_by_account": SUPPORTED}})
    app = create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(backend)})
    with TestClient(app) as client:
        summary, error = call(client, {})
        rest = client.post("/v1/messages_capabilities", json={}, headers=HEADERS).json()
    assert not error and summary == rest and summary["detail"] == "summary"
    assert summary["expires_at"] == "2027-04-19T22:37:50.000000Z"
    assert summary["accounts"] == [{k: row[k] for k in ("id", "platform", "label", "self_provider_id")} for row in rows]


# Synthetic copy of the backend's `subscriptions.transports` rows (ANYM-518).
TRANSPORTS = [
    {"name": "pull", "tools": ["fetch_subscription_events", "ack_subscription_events"],
     "authorized": True, "configured": True, "operational": True, "reason": None},
    {"name": "long_poll", "tools": ["wait_for_subscription_events"], "max_wait_s": 25,
     "authorized": True, "configured": True, "operational": True, "reason": None},
    {"name": "webhook", "tools": ["configure_subscription_delivery"], "authorized": True,
     "configured": False, "operational": False, "reason": "no_receiver_bound", "registration": "operator_only"},
]


# spec: ANYM-518
def test_gateway_omits_a_transport_whose_tools_the_profile_lacks(tmp_path):
    from test_capability_subscription_blocks import PROFILES, SUBSCRIPTIONS, app, exposed
    subscriber = exposed(next(p for p in PROFILES if p.stem == "messaging-subscriber"))
    without_wait = [n for n in subscriber if n != "messages_wait_for_subscription_events"]
    for tools, names in ((subscriber, ["pull", "long_poll", "webhook"]), (without_wait, ["pull", "webhook"])):
        with TestClient(app(tmp_path, tools, subscriptions=SUBSCRIPTIONS | {"transports": TRANSPORTS})) as client:
            mcp, error = call(client, {"sections": ["subscriptions"]})
            rest = client.post("/v1/messages_capabilities", json={"sections": ["subscriptions"]}, headers=HEADERS).json()
        assert not error and mcp == rest
        rows = rest["subscriptions"]["transports"]
        assert [row["name"] for row in rows] == names
        # Long polling also names the gateway's wait pool.
        assert all(("wait_pool" in row) is (row["name"] == "long_poll") for row in rows)
        assert all(row["wait_pool"] == {"active": 2, "waiting": 4} for row in rows if row["name"] == "long_poll")
