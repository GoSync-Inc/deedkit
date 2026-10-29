"""ANYM-643: a Telegram username recipient goes only where open_chat_by_username may go (synthetic only)."""
import hashlib
import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from test_policy import config

HEADERS = {"Authorization": "Bearer gateway-a"}
TOOLS = ["messages_send_message", "messages_preview_message", "messages_send"]
DRAFT = {"account": "synthetic", "username": "@synthetic_person", "text": "Synthetic first message"}


def backend(request, seen, permissions, recipients):
    if request.url.path == "/v1/capabilities":
        return httpx.Response(200, json={"permissions": list(permissions),
            "sending": {"recipients": {name: {} for name in recipients}}, "messenger_operations": {
                "supported_by_account": {"synthetic": {"operations": ["open_chat_by_username"]}}}})
    seen.append((request.url.path.removeprefix("/v1/"), json.loads(request.content)))
    return httpx.Response(200, json={"status": "queued", "send_id": "send_" + "a" * 32, "preview_digest": "d" * 64})


def setup(tmp_path, actions=("open_chat_by_username",), permissions=("read", "send"),
          recipients=("chat_id", "phone", "username")):
    value = config()
    value["deployment"] = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://backend.test"}}}
    value["profiles"]["reader"] = {"tools": TOOLS, "actions": {"messenger_operations": list(actions)}}
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
    app = create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(
        lambda request: backend(request, seen, permissions, recipients))})
    return app, seen


# spec: ANYM-643
def test_a_username_recipient_needs_the_open_chat_by_username_grant(tmp_path):
    app, seen = setup(tmp_path, actions=("open_chat_by_phone",))
    with TestClient(app) as client:
        for tool, extra in (("preview_message", {}), ("send_message", {"request_id": "synthetic-username-1"})):
            response = client.post("/v1/messages_" + tool, json={**DRAFT, **extra}, headers=HEADERS)
            assert response.status_code == 403, (tool, response.text)
    assert seen == []


# spec: ANYM-643
def test_a_granted_username_reaches_the_backend_and_chat_id_calls_drop_the_empty_field(tmp_path):
    app, seen = setup(tmp_path)
    with TestClient(app) as client:
        assert client.post("/v1/messages_preview_message", json=DRAFT, headers=HEADERS).status_code == 200
        plain = {"account": "synthetic", "chat_id": "chat_" + "a" * 32, "text": "Synthetic", "username": ""}
        assert client.post("/v1/messages_preview_message", json=plain, headers=HEADERS).status_code == 200
    assert seen[0][1]["username"] == "@synthetic_person"
    assert "username" not in seen[1][1]


# spec: ANYM-643
@pytest.mark.parametrize("actions,recipients,expected", [
    (("open_chat_by_username",), ("chat_id", "phone", "username"), True),
    (("open_chat_by_phone",), ("chat_id", "phone", "username"), False),
    (("open_chat_by_username",), ("chat_id", "phone"), None),  # a backend at bc228151: the field is never sent
])
def test_messages_send_carries_the_gateways_own_username_grant(tmp_path, actions, recipients, expected):
    app, seen = setup(tmp_path, actions=actions, recipients=recipients)
    body = {"to": "@synthetic_person", "text": "Synthetic", "dry_run": True, "allow_open_username": True}
    with TestClient(app) as client:
        assert client.post("/v1/messages_send", json=body, headers=HEADERS).status_code == 200
    sent = seen[-1][1]
    assert sent.get("allow_open_username") is expected if expected is not None else "allow_open_username" not in sent
