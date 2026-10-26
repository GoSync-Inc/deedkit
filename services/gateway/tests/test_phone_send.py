"""ANYM-532: preview_message and send_message to a phone go only where open_chat_by_phone may go (synthetic only)."""
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
TOOLS = ["resolve_reference", "send_message", "preview_message"]
PREVIEW = {"tool": "preview_message", "arguments": {"account": "synthetic", "phone": PHONE},
           "caller_supplies": ["text"], "caller_may_supply": ["format", "reply_to"]}


def backend(request, seen, permissions, refusal=None):
    if request.url.path == "/v1/capabilities":
        return httpx.Response(200, json={"permissions": list(permissions), "messenger_operations": {
            "supported_by_account": {"synthetic": {"provider": "whatsmeow", "operations": ["open_chat_by_phone"]}}}})
    operation, args = request.url.path.removeprefix("/v1/"), json.loads(request.content)
    seen.append((operation, args))
    if operation == "resolve_reference":
        return httpx.Response(404, json={"error": "not_found", "request_id": "c" * 32, "retryable": False,
                                         "hint": "open_chat_by_phone",
                                         "next_action": "preview_message_then_send_message_with_its_digest",
                                         "next_call": PREVIEW})
    if refusal is not None and "phone" in args:
        return httpx.Response(refusal[1], json={"error": refusal[0], "request_id": "c" * 32, "retryable": False})
    return httpx.Response(200, json={"status": "queued", "send_id": "send_" + "a" * 32, "preview_digest": "d" * 64})


def setup(tmp_path, actions=("open_chat_by_phone",), permissions=("read", "send"), refusal=None):
    value = config()
    value["deployment"] = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://backend.test"}}}
    value["profiles"]["reader"] = {"tools": ["messages_" + n for n in TOOLS],
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
                     transports={"messaging": httpx.MockTransport(lambda request: backend(request, seen, permissions, refusal))})
    return app, seen


def calls(phone=PHONE):
    draft = {"account": "synthetic", "phone": phone, "text": "Synthetic first message"}
    return [("preview_message", draft),
            ("send_message", {**draft, "request_id": "synthetic-phone-send-1", "preview_digest": "d" * 64})]


# spec: ANYM-532
@pytest.mark.parametrize("actions,permissions", [
    (("rename_group",), ("read", "send")),  # no gateway action grant for open_chat_by_phone
    (("open_chat_by_phone",), ("read",)),  # a read-only key
])
def test_a_phone_recipient_needs_the_open_chat_grant_and_send_scope(tmp_path, actions, permissions):
    app, seen = setup(tmp_path, actions, permissions)
    with TestClient(app) as client:
        for tool, arguments in calls():
            if tool == "send_message" and "send" not in permissions:
                continue  # the send tool itself already needs send
            response = client.post("/v1/messages_" + tool, json=arguments, headers=HEADERS)
            assert response.status_code == 403, (tool, response.text)
            mcp = rpc(client, "tools/call", {"name": "messages_" + tool, "arguments": arguments}).json()["result"]
            assert mcp["isError"]
        # chat_id calls keep today's permissions.
        response = client.post("/v1/messages_preview_message", json={"account": "synthetic", "chat_id": "chat_" + "b" * 32,
                               "text": "Synthetic"}, headers=HEADERS)
        assert response.status_code == 200
    assert [operation for operation, _ in seen] == ["preview_message"]  # zero phone requests reach the backend


# spec: ANYM-532
@pytest.mark.parametrize("refusal", [
    ("operation_disabled", 409),  # the account has no open_chat_by_phone opt-in
    ("chat_not_collected", 409),  # the collector or policy does not admit the chat
    ("forbidden", 403),  # a chat-scoped key: the separate operation needs the full account
])
def test_backend_guards_of_the_separate_operation_reach_the_caller_unchanged(tmp_path, refusal):
    # The gateway checks only its grants; the backend rechecks opt-in, policy and the full account.
    app, seen = setup(tmp_path, refusal=refusal)
    with TestClient(app) as client:
        for tool, arguments in calls():
            rest = client.post("/v1/messages_" + tool, json=arguments, headers=HEADERS)
            mcp = rpc(client, "tools/call", {"name": "messages_" + tool, "arguments": arguments}).json()["result"]
            assert rest.status_code == refusal[1] and mcp["isError"], (tool, rest.text)
            assert rest.json()["error"] == mcp["structuredContent"]["error"] == refusal[0]
            assert "send_id" not in rest.json() and "preview_digest" not in rest.json()
    assert [operation for operation, _ in seen] == ["preview_message"] * 2 + ["send_message"] * 2


# spec: ANYM-532
def test_phone_calls_reach_the_backend_and_chat_id_calls_stay_compatible(tmp_path):
    app, seen = setup(tmp_path)
    with TestClient(app) as client:
        for tool, arguments in calls():
            assert client.post("/v1/messages_" + tool, json=arguments, headers=HEADERS).status_code == 200
        client.post("/v1/messages_send_message", json={"account": "synthetic", "chat_id": "chat_" + "b" * 32,
                    "text": "Synthetic", "request_id": "synthetic-chat-send-1"}, headers=HEADERS)
    (preview, phone_preview), (send, phone_send), (_, chat_send) = seen
    assert preview == "preview_message" and phone_preview["phone"] == PHONE and "chat_id" not in phone_preview
    assert send == "send_message" and phone_send["phone"] == PHONE and "chat_id" not in phone_send
    # A backend from before ANYM-532 rejects unknown fields: a chat_id call never carries an empty phone.
    assert "phone" not in chat_send and chat_send["chat_id"] == "chat_" + "b" * 32


# spec: ANYM-532
@pytest.mark.parametrize("actions,permissions,named", [
    (("open_chat_by_phone",), ("read", "send"), True),
    (("rename_group",), ("read", "send"), False),
    (("open_chat_by_phone",), ("read",), False),
])
def test_a_not_found_phone_suggests_the_phone_preview_only_where_the_gateway_admits_it(tmp_path, actions, permissions,
                                                                                         named):
    app, seen = setup(tmp_path, actions, permissions)
    with TestClient(app) as client:
        schemas = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
        error = client.post("/v1/messages_resolve_reference", json={"account": "synthetic", "provider_chat_id": PHONE},
                            headers=HEADERS).json()
        if not named:
            assert "next_call" not in error and error["next_action"] == "key_cannot_open_new_chats"
            return
        step = error["next_call"]
        assert step == {**PREVIEW, "tool": "messages_preview_message"}
        arguments = {**step["arguments"], "text": "Synthetic first message"}
        jsonschema.validate(arguments, schemas[step["tool"]]["inputSchema"])
        assert client.post("/v1/messages_preview_message", json=arguments, headers=HEADERS).status_code == 200
    assert seen[-1] == ("preview_message", {**arguments, "reply_to": "", "format": "plain", "link_preview": False,
                                            "allow_lossy": False})


# spec: ANYM-532
def test_gateway_schemas_take_chat_id_or_phone_and_describe_the_phone():
    for name in ("messages_preview_message", "messages_send_message"):
        schema = CATALOG[name]["inputSchema"]
        assert "chat_id" not in schema["required"] and "phone" not in schema["required"]
        assert schema["properties"]["phone"] == {"default": "", "title": "Phone", "type": "string"}
        assert "phone" in CATALOG[name]["description"]
    assert CATALOG["messages_preview_message"]["annotations"]["openWorldHint"] is True


# The backend tool fields at the deployed release before ANYM-532 (54d0d5f), which reject unknown fields.
OLD_FIELDS = {"preview_message": {"account", "chat_id", "text", "reply_to", "format", "link_preview", "allow_lossy"},
              "send_message": {"account", "chat_id", "text", "request_id", "reply_to", "format", "preview_digest",
                               "link_preview", "allow_lossy", "wait_s"}}


# spec: ANYM-532
def test_chat_id_calls_through_this_gateway_fit_the_backend_before_phone(tmp_path):
    app, seen = setup(tmp_path)
    chat = {"account": "synthetic", "chat_id": "chat_" + "b" * 32, "text": "Synthetic"}
    with TestClient(app) as client:
        for tool, extra in (("preview_message", {}), ("send_message", {"request_id": "synthetic-chat-send-2"})):
            assert client.post("/v1/messages_" + tool, json={**chat, **extra}, headers=HEADERS).status_code == 200
            assert rpc(client, "tools/call", {"name": "messages_" + tool, "arguments": {**chat, **extra}}).json()["result"]
    assert len(seen) == 4
    for operation, args in seen:
        assert set(args) <= OLD_FIELDS[operation] and args["chat_id"] == chat["chat_id"], (operation, args)
