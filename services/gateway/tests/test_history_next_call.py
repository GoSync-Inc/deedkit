"""ANYM-756: the history pointer nested in a chat's sync status and in a read's result evidence is named by this
gateway's own tool, or dropped where the gateway would refuse the call; the hint itself stays."""
import hashlib
import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from test_policy import config

HEADERS = {"Authorization": "Bearer gateway-a"}
CHAT_ID = "chat_" + "b" * 32
OLDEST = "msg_" + "c" * 32
STEP = {"tool": "execute_messenger_operation",
        "arguments": {"account": "synthetic", "operation": "request_chat_history", "chat_id": CHAT_ID,
                      "parameters": {"before_message": OLDEST, "count": 100}},
        "caller_supplies": ["request_id", "preview_digest"]}
HINT = {"coverage": "unknown", "reason": "traversal_incomplete"}
TOOLS = ["get_sync_status", "get_messages", "execute_messenger_operation", "preview_messenger_operation"]


def backend(request, supported, available):
    """A fake messaging backend that answers like the real one for an incompletely traversed Telegram chat."""
    if request.url.path == "/v1/capabilities":
        return httpx.Response(200, json={"permissions": ["read", "read-live"], "messenger_operations": {
            "supported_by_account": {"synthetic": {"provider": "telethon", "operations": list(supported),
                                                   "available_operations": list(available)}}}})
    operation = request.url.path.removeprefix("/v1/")
    if operation == "get_sync_status":
        return httpx.Response(200, json={"chat_id": CHAT_ID, "account": "synthetic", "history": {
            "state": "loading", "history_traversal_complete": False, "gaps": [], "history_hint": HINT,
            "next_call": STEP}})
    return httpx.Response(200, json={"items": [], "next_cursor": None, "result_evidence": {
        "basis": "archived_data", "history_hint": {**HINT, "next_call": STEP}}})


def setup(tmp_path, actions=("request_chat_history",), supported=("request_chat_history",), tools=TOOLS,
          available=("request_chat_history",)):
    value = config()
    value["deployment"] = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://backend.test"}}}
    value["profiles"]["reader"] = {"tools": ["messages_" + n for n in tools],
                                   "actions": {"messenger_operations": list(actions)}}
    agent = value["principals"][0]
    agent["grants"] = {"messaging": ["read", "read-live"]}
    agent["actions"] = {"messenger_operations": list(actions)}
    key = tmp_path / "backend.key"
    key.write_text("backend-synthetic")
    key.chmod(0o600)
    agent["credentials"] = {"messaging": {"key_file": str(key),
                                          "key_sha256": hashlib.sha256(b"backend-synthetic").hexdigest()}}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return create_app(path, "http://testserver",
                      transports={"messaging": httpx.MockTransport(lambda request: backend(request, supported,
                                                                                            available))})


def read_both(app):
    with TestClient(app) as client:
        status = client.post("/v1/messages_get_sync_status", json={"chat_id": CHAT_ID}, headers=HEADERS)
        read = client.post("/v1/messages_get_messages", json={"chat_id": CHAT_ID}, headers=HEADERS)
    assert status.status_code == 200 and read.status_code == 200, (status.text, read.text)
    return status.json()["history"], read.json()["result_evidence"]["history_hint"]


# spec: ANYM-756
def test_nested_history_next_call_is_named_by_the_gateway_tool(tmp_path):
    history, hint = read_both(setup(tmp_path))
    mapped = {**STEP, "tool": "messages_execute_messenger_operation"}
    assert history["next_call"] == mapped and history["state"] == "loading" and history["history_hint"] == HINT
    assert hint == {**HINT, "next_call": mapped}


# spec: ANYM-756
@pytest.mark.parametrize("actions,supported,tools,available", [
    (("rename_group",), ("request_chat_history",), TOOLS, ("request_chat_history",)),  # no action grant
    (("request_chat_history",), ("rename_group",), TOOLS, ("rename_group",)),  # the account does not offer it
    (("request_chat_history",), ("request_chat_history",), ["get_sync_status", "get_messages"],
     ("request_chat_history",)),  # tool hidden
    (("request_chat_history",), ("request_chat_history",), TOOLS, ()),  # supported, not enabled for the account
])
def test_nested_history_next_call_is_dropped_where_the_gateway_would_refuse_it(tmp_path, actions, supported, tools,
                                                                               available):
    history, hint = read_both(setup(tmp_path, actions, supported, tools, available))
    assert "next_call" not in history and history["state"] == "loading" and history["history_hint"] == HINT
    assert hint == HINT
