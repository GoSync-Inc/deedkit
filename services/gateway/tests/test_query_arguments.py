"""Query additions remain available through both selected gateway transports."""
import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.catalog import CATALOG, arguments
from test_diagnostics import configured, HEADERS
from test_transport import rpc


def test_query_additions_validate_and_keep_legacy_defaults():
    search = CATALOG["messages_search"]
    updates = CATALOG["messages_get_updates"]
    assert arguments(search, {})["exclude_channels"] is False
    assert arguments(updates, {})["start_from"] == "beginning"
    assert arguments(search, {"exclude_channels": True})["exclude_channels"] is True
    assert arguments(updates, {"start_from": "now"})["start_from"] == "now"
    for tool, values in [(search, {"exclude_channels": 1}), (updates, {"start_from": "yesterday"})]:
        with pytest.raises(ValueError):
            arguments(tool, values)


# spec: ANYM-272
def test_search_chat_types_validate_and_include_channels_defaults_false():
    search = CATALOG["messages_search"]
    assert arguments(search, {})["include_channels"] is False
    assert arguments(search, {})["chat_types"] is None
    assert arguments(search, {"chat_types": ["private", "channel"]})["chat_types"] == ["private", "channel"]
    for values in ({"chat_types": ["dm"]}, {"chat_types": "private"}, {"include_channels": 1}):
        with pytest.raises(ValueError):
            arguments(search, values)


@pytest.mark.parametrize("tool,values", [
    ("messages_search", {"query": "synthetic", "exclude_channels": True}),
    ("messages_get_updates", {"consumer": "synthetic-new", "start_from": "now"}),
])
def test_query_additions_forward_identically_over_mcp_and_rest(tmp_path, tool, values):
    seen = []

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read", "cursor-write"]})
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"items": [], "synthetic": True})

    app, _, _ = configured(tmp_path, backend, module="messaging", tools=[tool], grants=["read", "cursor-write"])
    with TestClient(app) as client:
        rest = client.post("/v1/" + tool, json=values, headers=HEADERS)
        mcp = rpc(client, "tools/call", {"name": tool, "arguments": values}).json()["result"]
    assert rest.status_code == 200 and not mcp.get("isError")
    assert rest.json() == mcp["structuredContent"]
    assert seen[0] == seen[1]
    assert all(seen[0][key] == value for key, value in values.items())


@pytest.mark.parametrize("local,upstream", [(["read"], ["read", "cursor-write"]),
                                           (["read", "cursor-write"], ["read"])])
def test_start_now_requires_both_local_and_backend_cursor_write(tmp_path, local, upstream):
    dispatched = []

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": upstream})
        dispatched.append(json.loads(request.content))
        return httpx.Response(200, json={"items": []})

    app, _, _ = configured(tmp_path, backend, module="messaging", tools=["messages_get_updates"], grants=local)
    with TestClient(app) as client:
        assert [t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]] == ["messages_get_updates"]
        now = {"consumer": "synthetic-new", "start_from": "now"}
        assert client.post("/v1/messages_get_updates", json=now, headers=HEADERS).status_code == 403
        assert rpc(client, "tools/call", {"name": "messages_get_updates", "arguments": now}).json()["result"]["isError"]
        assert dispatched == []
        assert client.post("/v1/messages_get_updates", json={}, headers=HEADERS).status_code == 200
        assert len(dispatched) == 1 and dispatched[0]["start_from"] == "beginning"


def test_create_subscription_wake_rules_remain_explicit_and_bounded():
    tool = CATALOG["messages_create_subscription"]
    args = {"chat_ids": ["chat-1"], "wake_rules": ["owner_mention", "reply_to_confirmed_send"],
            "reply_to_send": "send-1"}
    assert arguments(tool, args)["wake_rules"] == args["wake_rules"]
    with pytest.raises(ValueError):
        arguments(tool, {"chat_ids": ["chat-1"], "wake_rules": ["arbitrary_instruction"]})
    assert arguments(tool, {"chat_ids": ["chat-1"]})["wake_rules"] is None


def test_send_wait_is_bounded_and_defaults_to_existing_immediate_response():
    tool = CATALOG["messages_send_message"]
    required = {"account": "account-1", "chat_id": "chat-1", "text": "Synthetic", "request_id": "request-1"}
    assert arguments(tool, required)["wait_s"] == 0
    assert arguments(tool, {**required, "wait_s": 25})["wait_s"] == 25
    for invalid in [-1, 26, True]:
        with pytest.raises(ValueError):
            arguments(tool, {**required, "wait_s": invalid})
