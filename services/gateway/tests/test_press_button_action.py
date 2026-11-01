"""press_button is a bot-press action for Telegram accounts: granted, supported and in scope, or never sent on."""
import pytest
from starlette.testclient import TestClient

from agent_gateway import policy
from agent_gateway.catalog import ACTIONS
from test_action_ceiling import setup

HEADERS = {"Authorization": "Bearer gateway-a"}
ARGS = {"account": "synthetic", "operation": "press_button", "chat_id": "chat_synthetic",
        "message_id": "msg_synthetic", "expected_revision": 3,
        "parameters": {"row": 0, "column": 1, "label": "Open", "fp": "0123456789abcdef0123456789abcdef"}}


def supported(provider):
    return {"synthetic": {"provider": provider, "operations": ["press_button"]}}


# spec: ANYM-676
def test_the_action_needs_the_bot_press_scope_and_is_a_change_message_route():
    assert ACTIONS["press_button"] == ["bot-press"]


# spec: ANYM-676
def test_a_granted_press_reaches_the_backend_with_the_exact_arguments(tmp_path):
    app, _, _, seen = setup(tmp_path, supported("telegram"), selected=["press_button"],
                            permissions=["read", "bot-press"])
    with TestClient(app) as client:
        result = client.post("/v1/messages_execute_messenger_operation",
                             json={**ARGS, "request_id": "synthetic-press-1", "preview_digest": "d" * 64},
                             headers=HEADERS)
    assert result.status_code == 200
    assert seen[-1] == ("/v1/execute_messenger_operation", {
        **ARGS, "request_id": "synthetic-press-1", "preview_digest": "d" * 64})


# spec: ANYM-676
@pytest.mark.parametrize("selected,permissions,support", [
    (["rename_group"], ["read", "bot-press", "groups-manage"], supported("telegram")),  # no gateway action grant
    (["press_button"], ["read", "message-react"], supported("telegram")),  # the key lacks the bot-press scope
    (["press_button"], ["read", "bot-press"], {"synthetic": {"operations": ["rename_group"]}}),  # not opted in
    (["press_button"], ["read", "bot-press"], {"synthetic": {"provider": "whatsmeow", "operations": []}}),  # no support
])
def test_missing_grant_scope_or_support_is_refused_before_the_backend(tmp_path, selected, permissions, support):
    app, _, _, seen = setup(tmp_path, support, selected=selected, permissions=permissions)
    with TestClient(app) as client:
        for tool in ("preview_messenger_operation", "execute_messenger_operation"):
            body = {**ARGS, "request_id": "synthetic-press-2"} if tool.startswith("execute") else ARGS
            assert client.post("/v1/messages_" + tool, json=body, headers=HEADERS).status_code == 403
    assert seen == []


# spec: ANYM-676
def test_the_change_message_tool_routes_press_button_and_its_example_is_a_valid_request():
    import json
    from importlib.resources import files
    tool = next(t for t in json.loads(files("agent_gateway").joinpath("catalog.json").read_text())
                if t["name"] == "messages_change_message")
    assert "press_button" in tool["routes_actions"]
    assert "press_button" in tool["inputSchema"]["properties"]["operation"]["enum"]
    example = tool["examples"]["press_button"]
    assert example["parameters"].keys() == {"row", "column", "label", "fp"} and len(example["parameters"]["fp"]) == 32


# spec: ANYM-676
def test_an_older_gateway_rejects_a_policy_naming_the_action(monkeypatch):
    older = {name: scopes for name, scopes in ACTIONS.items() if name != "press_button"}
    monkeypatch.setattr(policy, "ACTIONS", older)
    with pytest.raises(ValueError):
        policy.Policy.validate_actions({"messenger_operations": ["press_button"]})
    policy.Policy.validate_actions({"messenger_operations": ["rename_group"]})
