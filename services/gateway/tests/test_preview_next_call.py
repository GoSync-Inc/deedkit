"""ANYM-646: a backend refusal for a missing or changed preview carries `next_call`; the gateway names it by its own
tool and drops it where it would refuse the call. A compatibility test of today's ANYM-531 mapper."""
import hashlib
import json

import httpx
import jsonschema
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from test_policy import config
from test_transport import rpc

HEADERS = {"Authorization": "Bearer gateway-a"}
TOOLS = ["send_message", "preview_message", "execute_messenger_operation", "preview_messenger_operation"]
ACTIONS = ("delete_message", "open_chat_by_phone", "open_chat_by_username")
CHAT = "chat_" + "b" * 32
MESSAGE = "msg_" + "c" * 32
HINT = ("If next_call is present, call its tool with its arguments plus your own values for caller_supplies, show the "
        "preview to the owner and, after the owner confirms, repeat this send unchanged with the returned "
        "preview_digest; if it is absent, this key or route cannot run the preview tool.")
SEND = {"tool": "preview_message", "arguments": {"account": "synthetic", "chat_id": CHAT},
        "caller_supplies": ["text", "format"]}
PHONE = {"tool": "preview_message", "arguments": {"account": "synthetic"}, "caller_supplies": ["text", "phone"]}
USERNAME = {"tool": "preview_message", "arguments": {"account": "synthetic"}, "caller_supplies": ["text", "username"]}
OPERATION = {"tool": "preview_messenger_operation", "caller_supplies": ["parameters"], "arguments": {
    "account": "synthetic", "operation": "delete_message", "chat_id": CHAT, "message_id": MESSAGE,
    "expected_revision": 1}}
CASES = [  # (gateway tool, the call's arguments, the backend's next_call, what the caller supplies)
    ("messages_send_message", {"account": "synthetic", "chat_id": CHAT, "text": "Synthetic", "format": "markdown",
                               "request_id": "synthetic-send-1"}, SEND, {"text": "Synthetic", "format": "markdown"}),
    ("messages_send_message", {"account": "synthetic", "phone": "+12025550147", "text": "Synthetic",
                               "request_id": "synthetic-send-2"}, PHONE, {"text": "Synthetic", "phone": "+12025550147"}),
    ("messages_send_message", {"account": "synthetic", "username": "synthetic_person", "text": "Synthetic",
                               "request_id": "synthetic-send-3"}, USERNAME,
     {"text": "Synthetic", "username": "synthetic_person"}),
    ("messages_execute_messenger_operation", {"account": "synthetic", "operation": "delete_message", "chat_id": CHAT,
                                              "message_id": MESSAGE, "expected_revision": 1,
                                              "request_id": "synthetic-operation-1"}, OPERATION, {"parameters": {}}),
]


def refusal(step, code):
    return {"error": code, "request_id": "c" * 32, "retryable": False, "next_action": "preview_then_send_with_digest",
            "hint": HINT, "next_call": step}


def setup(tmp_path, step, code, tools, actions=ACTIONS, permissions=("read", "send", "message-delete")):
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

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": list(permissions), "messenger_operations": {
                "supported_by_account": {"synthetic": {"provider": "telegram", "operations": list(actions)}}}})
        operation, args = request.url.path.removeprefix("/v1/"), json.loads(request.content)
        seen.append((operation, args))
        if operation in ("send_message", "execute_messenger_operation"):
            return httpx.Response(409, json=refusal(step, code))
        # A preview echoes its operation, as the real server does (the gateway checks it against the grant).
        return httpx.Response(200, json={"account": args["account"], "operation": args.get("operation"),
                                         "preview_digest": "d" * 64})

    app = create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(backend)})
    return app, seen




# spec: ANYM-646
@pytest.mark.parametrize("code", ["preview_required", "preview_mismatch"])
@pytest.mark.parametrize("tool,arguments,step,supplied", CASES)
def test_each_refusal_names_the_gateway_preview_tool_and_the_filled_call_validates(tmp_path, tool, arguments, step,
                                                                                    supplied, code):
    app, seen = setup(tmp_path, step, code, TOOLS)
    with TestClient(app) as client:
        schemas = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
        response = client.post("/v1/" + tool, json=arguments, headers=HEADERS)
        assert response.status_code == 409 and response.json()["error"] == code
        body = response.json()
        assert body["next_action"] == "preview_then_send_with_digest" and body["hint"] == HINT
        named = body["next_call"]
        assert named == {**step, "tool": "messages_" + step["tool"]}
        mcp = rpc(client, "tools/call", {"name": tool, "arguments": arguments}).json()["result"]
        assert mcp["isError"] and mcp["structuredContent"]["next_call"] == named
        filled = {**named["arguments"], **supplied}
        assert set(supplied) == set(named["caller_supplies"])
        jsonschema.validate(filled, schemas[named["tool"]]["inputSchema"])
        preview = client.post("/v1/" + named["tool"], json=filled, headers=HEADERS)
        assert preview.status_code == 200 and preview.json()["preview_digest"] == "d" * 64, preview.text
        assert seen[-1][0] == step["tool"]


# spec: ANYM-646
@pytest.mark.parametrize("tool,arguments,step,supplied", CASES)
def test_a_caller_without_the_preview_tool_gets_no_next_call(tmp_path, tool, arguments, step, supplied):
    kept = [n for n in TOOLS if n != step["tool"]]
    app, _ = setup(tmp_path, step, "preview_required", kept)
    with TestClient(app) as client:
        body = client.post("/v1/" + tool, json=arguments, headers=HEADERS).json()
        assert body["error"] == "preview_required" and "next_call" not in body
