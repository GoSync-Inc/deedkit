"""search_messages_live is a read-live action: granted, supported and in scope, or never sent on."""
import pytest
from starlette.testclient import TestClient

from agent_gateway import policy
from agent_gateway.catalog import ACTIONS
from test_action_ceiling import setup

HEADERS = {"Authorization": "Bearer gateway-a"}
ARGS = {"account": "synthetic", "operation": "search_messages_live", "parameters": {"query": "synthetic words"}}
NATIVE = {"synthetic": {"provider": "telegram", "operations": ["search_messages_live"]}}


# spec: ANYM-475
def test_the_action_needs_the_read_live_scope():
    assert ACTIONS["search_messages_live"] == ["read-live"]


# spec: ANYM-475
def test_granted_action_reaches_the_backend_with_the_exact_arguments(tmp_path):
    app, _, _, seen = setup(tmp_path, NATIVE, selected=["search_messages_live"], permissions=["read", "read-live"])
    with TestClient(app) as client:
        result = client.post("/v1/messages_execute_messenger_operation",
                             json={**ARGS, "request_id": "synthetic-live-1"}, headers=HEADERS)
    assert result.status_code == 200
    assert seen[-1] == ("/v1/execute_messenger_operation", {
        **ARGS, "request_id": "synthetic-live-1", "chat_id": "", "message_id": "", "expected_revision": 0,
        "preview_digest": ""})


# spec: ANYM-475
@pytest.mark.parametrize("selected,permissions,supported", [
    (["rename_group"], ["read", "read-live", "groups-manage"], NATIVE),  # no gateway action grant
    (["search_messages_live"], ["read"], NATIVE),  # the agent lacks the read-live scope
    (["search_messages_live"], ["read", "read-live"], {"synthetic": {"operations": ["rename_group"]}}),  # not opted in
])
def test_missing_grant_scope_or_support_is_refused_before_the_backend(tmp_path, selected, permissions, supported):
    app, _, _, seen = setup(tmp_path, supported, selected=selected, permissions=permissions)
    with TestClient(app) as client:
        for tool in ("preview_messenger_operation", "execute_messenger_operation"):
            result = client.post("/v1/messages_"+tool, json={**ARGS, "request_id": "synthetic-live-2"}
                                 if tool.startswith("execute") else ARGS, headers=HEADERS)
            assert result.status_code == 403
    assert seen == []


# spec: ANYM-475
def test_a_policy_without_the_action_still_loads_on_the_new_gateway():
    policy.Policy.validate_actions({"messenger_operations": ["rename_group", "leave_group"]})


# spec: ANYM-475
def test_an_older_gateway_rejects_a_policy_naming_the_action(monkeypatch):
    older = {name: scopes for name, scopes in ACTIONS.items() if name != "search_messages_live"}
    monkeypatch.setattr(policy, "ACTIONS", older)
    with pytest.raises(ValueError):
        policy.Policy.validate_actions({"messenger_operations": ["search_messages_live"]})
