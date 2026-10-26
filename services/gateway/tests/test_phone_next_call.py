"""ANYM-531: the backend's next_call is named by this gateway's own tools, or dropped where the gateway refuses it."""
import hashlib
import json

import httpx
import jsonschema
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG
from test_policy import config
from test_transport import rpc

HEADERS = {"Authorization": "Bearer gateway-a"}
PHONE = "+12025550147"  # NANP fictional 555-01xx range
OPEN = {"account": "synthetic", "operation": "open_chat_by_phone", "parameters": {"phone": PHONE, "create": False}}
OPERATION_ID = "op_" + "a" * 32
CHAT_ID = "chat_" + "b" * 32
TOOLS = ["resolve_reference", "send_message", "preview_message", "execute_messenger_operation",
         "get_messenger_operation", "list_messenger_operations"]


def backend(request, seen):
    """A fake messaging backend that answers like the real one for each step of reaching a new number."""
    if request.url.path == "/v1/capabilities":
        return httpx.Response(200, json={"permissions": ["read", "send"], "messenger_operations": {
            "supported_by_account": {"synthetic": {"provider": "whatsmeow", "operations": ["open_chat_by_phone"]}}}})
    operation, args = request.url.path.removeprefix("/v1/"), json.loads(request.content)
    seen.append((operation, args))
    if operation in ("resolve_reference", "send_message"):
        return httpx.Response(404, json={
            "error": "not_found", "request_id": "c" * 32, "retryable": False, "hint": "open_chat_by_phone",
            "next_action": "open_chat_by_phone_then_follow_next_call",
            "next_call": {"tool": "execute_messenger_operation", "arguments": OPEN, "caller_supplies": ["request_id"]}})
    if operation == "execute_messenger_operation":
        return httpx.Response(200, json={
            "operation": "open_chat_by_phone", "operation_id": OPERATION_ID, "status": "queued",
            "request_id": args["request_id"], "next_action": "get_messenger_operation",
            "next_call": {"tool": "get_messenger_operation", "arguments": {"operation_id": OPERATION_ID},
                          "caller_supplies": []}})
    if operation == "get_messenger_operation":
        return httpx.Response(200, json={
            "operation": "open_chat_by_phone", "operation_id": OPERATION_ID, "status": "succeeded",
            "result": {"outcome": "confirmed", "data": {"on_whatsapp": True, "chat_id": CHAT_ID, "existing": False}},
            "next_call": {"tool": "preview_message", "arguments": {"account": "synthetic", "chat_id": CHAT_ID},
                          "caller_supplies": ["text"]}})
    return httpx.Response(200, json={"account": args["account"], "chat_id": args["chat_id"], "preview_digest": "d" * 64})


def setup(tmp_path, actions=("open_chat_by_phone",), permissions=("read", "send"), tools=TOOLS):
    value = config()
    value["deployment"] = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://backend.test"}}}
    value["profiles"]["reader"] = {"tools": ["messages_" + n for n in tools],
                                   "actions": {"messenger_operations": list(actions)}}
    agent = value["principals"][0]
    agent["grants"] = {"messaging": list(permissions)}
    agent["actions"] = {"messenger_operations": list(actions)}
    key = tmp_path / "backend.key"
    key.write_text("backend-synthetic")
    key.chmod(0o600)
    agent["credentials"] = {"messaging": {"key_file": str(key),
                                          "key_sha256": hashlib.sha256(b"backend-synthetic").hexdigest()}}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    seen = []
    app = create_app(path, "http://testserver",
                     transports={"messaging": httpx.MockTransport(lambda request: backend(request, seen))})
    return app, seen


def follow(client, schemas, step, **supplied):
    """Validate the suggestion against the gateway's exposed schema, then make exactly that call."""
    assert set(supplied) == set(step["caller_supplies"]) and not set(supplied) & set(step["arguments"])
    arguments = {**step["arguments"], **supplied}
    jsonschema.validate(arguments, schemas[step["tool"]]["inputSchema"])
    response = client.post("/v1/" + step["tool"], json=arguments, headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


# spec: ANYM-531
@pytest.mark.parametrize("tool", ["resolve_reference", "send_message"])
def test_each_next_call_names_the_gateway_tool_and_runs_against_the_backend(tmp_path, tool):
    app, seen = setup(tmp_path)
    arguments = {"account": "synthetic", "provider_chat_id": PHONE} if tool == "resolve_reference" else \
        {"account": "synthetic", "chat_id": PHONE, "text": "Synthetic", "request_id": "synthetic-send-1"}
    with TestClient(app) as client:
        schemas = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
        error = client.post("/v1/messages_" + tool, json=arguments, headers=HEADERS)
        assert error.status_code == 404 and error.json()["hint"] == "open_chat_by_phone"
        assert error.json()["next_action"] == "open_chat_by_phone_then_follow_next_call"
        step = error.json()["next_call"]
        assert step == {"tool": "messages_execute_messenger_operation", "arguments": OPEN, "caller_supplies": ["request_id"]}
        mcp = rpc(client, "tools/call", {"name": "messages_" + tool, "arguments": arguments}).json()["result"]
        assert mcp["isError"] and mcp["structuredContent"]["next_call"] == step
        queued = follow(client, schemas, step, request_id="synthetic-phone-1")
        assert seen[-1] == ("execute_messenger_operation", {**OPEN, "request_id": "synthetic-phone-1", "chat_id": "",
                            "message_id": "", "expected_revision": 0, "preview_digest": ""})
        assert queued["next_call"]["tool"] == "messages_get_messenger_operation"
        done = follow(client, schemas, queued["next_call"])
        assert seen[-1] == ("get_messenger_operation", {"operation_id": OPERATION_ID})
        assert done["next_call"]["tool"] == "messages_preview_message"
        follow(client, schemas, done["next_call"], text="Synthetic first message")
        assert seen[-1][0] == "preview_message" and seen[-1][1]["chat_id"] == CHAT_ID


# spec: ANYM-531
@pytest.mark.parametrize("actions,permissions,tools", [
    (("rename_group",), ("read", "send"), TOOLS),  # no gateway action grant for open_chat_by_phone
    (("open_chat_by_phone",), ("read",), TOOLS),  # the agent lacks the send scope
    (("open_chat_by_phone",), ("read", "send"), ["resolve_reference", "get_messenger_operation"]),  # tool hidden
])
def test_no_next_call_where_the_gateway_would_refuse_it(tmp_path, actions, permissions, tools):
    app, seen = setup(tmp_path, actions, permissions, tools)
    with TestClient(app) as client:
        error = client.post("/v1/messages_resolve_reference",
                            json={"account": "synthetic", "provider_chat_id": PHONE}, headers=HEADERS)
    assert error.status_code == 404 and error.json()["error"] == "not_found"
    assert "next_call" not in error.json() and error.json()["next_action"] == "key_cannot_open_new_chats"
    assert error.json()["hint"] == "open_chat_by_phone"
    assert [operation for operation, _ in seen] == ["resolve_reference"]


# spec: ANYM-531
def test_gateway_descriptions_say_how_a_new_number_is_reached():
    for name in ("messages_resolve_reference", "messages_send_message"):
        sentence = next(s for s in CATALOG[name]["description"].split(". ") if "open_chat_by_phone" in s)
        assert "messages_execute_messenger_operation" in sentence
