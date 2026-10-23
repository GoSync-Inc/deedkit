"""Capabilities advertise usable messenger actions; the call ceiling is unchanged."""
import json

import pytest
from starlette.testclient import TestClient

from test_action_ceiling import setup
from test_transport import rpc

HEADERS = {"Authorization": "Bearer gateway-a"}
GRANTED = ["leave_group", "remove_participant", "rename_group"]
ARGS = {"account": "synthetic", "parameters": {"title": "Synthetic"}}


def capabilities(tmp_path, supported, selected=GRANTED):
    app, value, path, seen = setup(tmp_path, supported, selected=selected)
    value["profiles"]["reader"]["tools"].append("messages_capabilities")
    value["revision"] += 1
    path.write_text(json.dumps(value))
    return app, seen


def test_only_granted_supported_and_available_actions_are_advertised(tmp_path):
    supported = {
        "synthetic": {
            "provider": "whatsmeow",
            "operations": ["leave_group", "remove_participant", "rename_group"],
            # Availability cannot add unsupported or ungranted operations.
            "available_operations": ["rename_group", "unreviewed_operation", "add_contact"],
        },
        "second": {"operations": ["leave_group", "rename_group"], "available_operations": ["leave_group"]},
        "disabled": {"operations": ["leave_group", "rename_group"], "available_operations": []},
    }
    app, seen = capabilities(tmp_path, supported)
    with TestClient(app) as client:
        caps = client.post("/v1/messages_capabilities", json={"detail": "full"}, headers=HEADERS).json()
        assert caps["gateway_actions_by_account"] == {
            "synthetic": ["rename_group"], "second": ["leave_group"], "disabled": []}
        assert caps["gateway_actions"] == ["leave_group", "rename_group"]
        assert caps["gateway_actions_excluded_by_account"] == {
            "synthetic": {"not_available_for_account": 2},
            "second": {"not_available_for_account": 1},
            "disabled": {"not_available_for_account": 2},
        }
        assert caps["gateway_actions_basis"] == "granted_supported_and_available_for_account"
        assert caps["gateway_actions_scope"] == "messenger_operations"
        assert "messenger_operations" not in caps and "remove_participant" not in json.dumps(caps)
        # Tool visibility and the operation enum keep the existing ceiling.
        tools = rpc(client, "tools/list").json()["result"]["tools"]
        for tool in tools:
            if "operation" in tool["inputSchema"]["properties"]:
                assert tool["inputSchema"]["properties"]["operation"]["enum"] == GRANTED
        assert {"messages_preview_messenger_operation", "messages_execute_messenger_operation"} <= {
            t["name"] for t in tools}
        assert seen == []


@pytest.mark.parametrize("report", [None, "rename_group", ["rename_group", None], {"rename_group": True}])
def test_missing_or_malformed_availability_advertises_nothing(tmp_path, report):
    row = {"operations": ["rename_group", "leave_group"]}
    if report is not None:
        row["available_operations"] = report
    app, _ = capabilities(tmp_path, {"synthetic": row})
    with TestClient(app) as client:
        caps = client.post("/v1/messages_capabilities", json={"detail": "full"}, headers=HEADERS).json()
    assert caps["gateway_actions"] == [] and caps["gateway_actions_by_account"] == {"synthetic": []}
    assert caps["gateway_actions_excluded_by_account"] == {"synthetic": {"availability_not_reported": 2}}


def test_missing_support_contract_reports_no_accounts(tmp_path):
    app, _ = capabilities(tmp_path, "not-a-mapping")
    with TestClient(app) as client:
        caps = client.post("/v1/messages_capabilities", json={"detail": "full"}, headers=HEADERS).json()
    assert caps["gateway_actions"] == [] and caps["gateway_actions_by_account"] == {}
    assert caps["gateway_actions_excluded_by_account"] == {}


def test_availability_report_never_loosens_the_call_ceiling(tmp_path):
    supported = {
        "synthetic": {
            "operations": ["leave_group", "rename_group"],
            "available_operations": ["leave_group", "rename_group", "remove_participant", "add_contact"],
        }
    }
    app, seen = capabilities(tmp_path, supported, selected=["rename_group"])
    with TestClient(app) as client:
        caps = client.post("/v1/messages_capabilities", json={"detail": "full"}, headers=HEADERS).json()
        assert caps["gateway_actions_by_account"] == {"synthetic": ["rename_group"]}
        assert caps["gateway_actions_excluded_by_account"] == {}
        for operation in ("leave_group", "remove_participant", "add_contact"):
            args = {**ARGS, "operation": operation}
            assert client.post(
                "/v1/messages_preview_messenger_operation", json=args, headers=HEADERS).status_code == 403
            assert rpc(client, "tools/call", {
                "name": "messages_preview_messenger_operation", "arguments": args}).json()["result"]["isError"]
        assert seen == []
        # An action that is granted and supported but not reported available keeps
        # its existing ceiling: the backend still decides account opt-in.
        supported["synthetic"]["available_operations"] = []
        caps = client.post("/v1/messages_capabilities", json={"detail": "full"}, headers=HEADERS).json()
        assert caps["gateway_actions_by_account"] == {"synthetic": []}
        response = client.post("/v1/messages_preview_messenger_operation",
                               json={**ARGS, "operation": "rename_group"}, headers=HEADERS)
        assert response.status_code == 200
        assert [path for path, _ in seen] == ["/v1/preview_messenger_operation"]
