"""request_chat_history is a read-live action on both platforms: granted, supported and in scope, or never sent on."""
import pytest
from starlette.testclient import TestClient

from agent_gateway import policy
from agent_gateway.catalog import ACTIONS
from test_action_ceiling import setup

HEADERS = {"Authorization": "Bearer gateway-a"}
ARGS = {"account": "synthetic", "operation": "request_chat_history", "chat_id": "chat_synthetic",
        "parameters": {"count": 20}}


def supported(provider):
    return {"synthetic": {"provider": provider, "operations": ["request_chat_history"]}}


# spec: ANYM-477
def test_the_action_needs_the_read_live_scope():
    assert ACTIONS["request_chat_history"] == ["read-live"]


# spec: ANYM-477
@pytest.mark.parametrize("provider", ["telegram", "whatsmeow"])
def test_granted_action_reaches_the_backend_with_the_exact_arguments(tmp_path, provider):
    app, _, _, seen = setup(tmp_path, supported(provider), selected=["request_chat_history"],
                            permissions=["read", "read-live"])
    with TestClient(app) as client:
        result = client.post("/v1/messages_execute_messenger_operation",
                             json={**ARGS, "request_id": "synthetic-history-1"}, headers=HEADERS)
    assert result.status_code == 200
    assert seen[-1] == ("/v1/execute_messenger_operation", {
        **ARGS, "request_id": "synthetic-history-1", "message_id": "", "expected_revision": 0, "preview_digest": ""})


# spec: ANYM-477
@pytest.mark.parametrize("selected,permissions,support", [
    (["rename_group"], ["read", "read-live", "groups-manage"], supported("telegram")),  # no gateway action grant
    (["request_chat_history"], ["read"], supported("whatsmeow")),  # the agent lacks the read-live scope
    (["request_chat_history"], ["read", "read-live"], {"synthetic": {"operations": ["rename_group"]}}),  # not opted in
])
def test_missing_grant_scope_or_support_is_refused_before_the_backend(tmp_path, selected, permissions, support):
    app, _, _, seen = setup(tmp_path, support, selected=selected, permissions=permissions)
    with TestClient(app) as client:
        for tool in ("preview_messenger_operation", "execute_messenger_operation"):
            result = client.post("/v1/messages_"+tool, json={**ARGS, "request_id": "synthetic-history-2"}
                                 if tool.startswith("execute") else ARGS, headers=HEADERS)
            assert result.status_code == 403
    assert seen == []


# spec: ANYM-477
def test_an_older_gateway_rejects_a_policy_naming_the_action(monkeypatch):
    older = {name: scopes for name, scopes in ACTIONS.items() if name != "request_chat_history"}
    monkeypatch.setattr(policy, "ACTIONS", older)
    with pytest.raises(ValueError):
        policy.Policy.validate_actions({"messenger_operations": ["request_chat_history"]})
    policy.Policy.validate_actions({"messenger_operations": ["rename_group"]})  # a policy without it still loads
