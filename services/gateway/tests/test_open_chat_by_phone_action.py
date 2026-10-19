"""open_chat_by_phone is a reviewed send-scoped action: granted, supported and in scope, or never sent on."""
import pytest
from starlette.testclient import TestClient

from agent_gateway.catalog import ACTIONS
from test_action_ceiling import setup

HEADERS = {"Authorization": "Bearer gateway-a"}
ARGS = {"account": "synthetic", "operation": "open_chat_by_phone", "parameters": {"phone": "+1 202 555 0123"}}
NATIVE = {"synthetic": {"provider": "whatsmeow", "operations": ["open_chat_by_phone"]}}


# spec: ANYM-430
def test_the_action_needs_the_send_scope():
    assert ACTIONS["open_chat_by_phone"] == ["send"]


# spec: ANYM-430
def test_granted_action_reaches_the_backend_with_the_exact_arguments(tmp_path):
    app, _, _, seen = setup(tmp_path, NATIVE, selected=["open_chat_by_phone"], permissions=["read", "send"])
    with TestClient(app) as client:
        result = client.post("/v1/messages_execute_messenger_operation",
                             json={**ARGS, "request_id": "synthetic-phone-1"}, headers=HEADERS)
    assert result.status_code == 200
    assert seen[-1] == ("/v1/execute_messenger_operation", {
        **ARGS, "request_id": "synthetic-phone-1", "chat_id": "", "message_id": "", "expected_revision": 0,
        "preview_digest": ""})


# spec: ANYM-430
@pytest.mark.parametrize("selected,permissions,supported", [
    (["rename_group"], ["read", "send", "groups-manage"], NATIVE),  # no gateway action grant
    (["open_chat_by_phone"], ["read"], NATIVE),  # the agent lacks the send scope
    (["open_chat_by_phone"], ["read", "send"], {"synthetic": {"operations": ["rename_group"]}}),  # webjs account
])
def test_missing_grant_scope_or_support_is_refused_before_the_backend(tmp_path, selected, permissions, supported):
    app, _, _, seen = setup(tmp_path, supported, selected=selected, permissions=permissions)
    with TestClient(app) as client:
        for tool in ("preview_messenger_operation", "execute_messenger_operation"):
            result = client.post("/v1/messages_"+tool, json={**ARGS, "request_id": "synthetic-phone-2"}
                                 if tool.startswith("execute") else ARGS, headers=HEADERS)
            assert result.status_code == 403
    assert seen == []
