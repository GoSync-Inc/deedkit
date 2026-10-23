import hashlib
import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from test_policy import config
from test_transport import rpc


def setup(tmp_path, supported=None, selected=None, permissions=None):
    value = config()
    value["deployment"] = {
        "modules": ["messaging"],
        "backends": {"messaging": {"url": "http://backend.test"}},
    }
    names = [
        "list_messenger_operations",
        "preview_messenger_operation",
        "execute_messenger_operation",
        "get_messenger_operation",
    ]
    value["profiles"]["reader"] = {
        "tools": ["messages_" + n for n in names],
        "actions": {"messenger_operations": ["rename_group"]},
    }
    agent = value["principals"][0]
    agent["grants"] = {"messaging": ["groups-manage"]}
    agent["actions"] = {"messenger_operations": ["rename_group", "leave_group"]}
    if selected is not None:
        value["profiles"]["reader"]["actions"]["messenger_operations"] = selected
        agent["actions"]["messenger_operations"] = selected
    if permissions is not None:
        agent["grants"]["messaging"] = permissions
    backend_key = tmp_path / "backend.key"
    backend_key.write_text("backend-synthetic")
    backend_key.chmod(0o600)
    agent["credentials"] = {
        "messaging": {
            "key_file": str(backend_key),
            "key_sha256": hashlib.sha256(b"backend-synthetic").hexdigest(),
        }
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    seen = []

    def handler(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(
                200,
                json={
                    "permissions": agent["grants"]["messaging"],
                    "messenger_operations": {
                        "supported_by_account": supported
                        if supported is not None
                        else {
                            "synthetic": {"operations": ["rename_group", "leave_group", "remove_participant"]}
                        }
                    },
                },
            )
        args = json.loads(request.content)
        seen.append((request.url.path, args))
        if request.url.path.endswith("/list_messenger_operations"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"operation": n, "supported": True, "available": True}
                        for n in ["rename_group", "leave_group", "remove_participant"]
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "operation": args.get("operation", "leave_group"),
                "preview_digest": "unchanged",
                "status": "queued",
                "operation_id": "synthetic-id",
            },
        )

    return (
        create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(handler)}),
        value,
        path,
        seen,
    )


def test_only_exact_actions_are_discovered_and_admitted_in_both_transports(tmp_path):
    app, value, path, seen = setup(tmp_path)
    headers = {"Authorization": "Bearer gateway-a"}
    with TestClient(app) as c:
        tools = rpc(c, "tools/list").json()["result"]["tools"]
        for tool in tools:
            if "operation" in tool["inputSchema"]["properties"]:
                assert tool["inputSchema"]["properties"]["operation"]["enum"] == ["rename_group"]
        listed = c.post(
            "/v1/messages_list_messenger_operations", json={"account": "synthetic"}, headers=headers
        ).json()
        assert [x["operation"] for x in listed["items"]] == ["rename_group"]
        for operation in ["leave_group", "remove_participant", "rename_group ", "Rename_Group"]:
            args = {"account": "synthetic", "operation": operation, "parameters": {"title": "Synthetic"}}
            count = len(seen)
            assert (
                c.post("/v1/messages_preview_messenger_operation", json=args, headers=headers).status_code
                == 403
            )
            assert rpc(
                c, "tools/call", {"name": "messages_preview_messenger_operation", "arguments": args}
            ).json()["result"]["isError"]
            assert len(seen) == count
        args = {"account": "synthetic", "operation": "rename_group", "parameters": {"title": "Synthetic"}}
        result = c.post("/v1/messages_preview_messenger_operation", json=args, headers=headers)
        assert result.status_code == 200 and result.json()["preview_digest"] == "unchanged"
        execute = {**args, "request_id": "synthetic-request", "preview_digest": "unchanged"}
        assert (
            c.post("/v1/messages_execute_messenger_operation", json=execute, headers=headers).status_code
            == 200
        )
        assert seen[-1][1]["preview_digest"] == "unchanged"
        # Receipts for an action outside this ceiling are not disclosed.
        assert (
            c.post(
                "/v1/messages_get_messenger_operation",
                json={"operation_id": "foreign-action"},
                headers=headers,
            ).status_code
            == 403
        )
        value["revision"] += 1
        value["principals"][0]["actions"] = {}
        path.write_text(json.dumps(value))
        count = len(seen)
        assert (
            c.post("/v1/messages_execute_messenger_operation", json=execute, headers=headers).status_code
            == 403
        )
        assert rpc(c, "tools/list").json()["result"]["tools"] == []
        assert len(seen) == count


def test_backend_support_narrows_discovery_and_rechecks_cached_calls(tmp_path):
    supported = {"synthetic": {"provider": "whatsmeow", "operations": ["rename_group"]}}
    app, _, _, seen = setup(tmp_path, supported)
    args = {"account": "synthetic", "operation": "rename_group", "parameters": {"title": "Synthetic"}}
    with TestClient(app) as client:
        assert rpc(client, "tools/list").json()["result"]["tools"]
        supported["synthetic"]["operations"] = []
        assert [t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]] == [
            "messages_get_messenger_operation"
        ]
        for method in ("preview", "execute"):
            values = (
                args
                if method == "preview"
                else {**args, "request_id": "synthetic", "preview_digest": "a" * 64}
            )
            assert rpc(
                client, "tools/call", {"name": f"messages_{method}_messenger_operation", "arguments": values}
            ).json()["result"]["isError"]
            assert (
                client.post(
                    f"/v1/messages_{method}_messenger_operation",
                    json=values,
                    headers={"Authorization": "Bearer gateway-a"},
                ).status_code
                == 403
            )
    assert not seen


def test_support_is_account_specific_even_when_another_account_supports_action(tmp_path):
    supported = {
        "synthetic": {"provider": "whatsmeow", "operations": []},
        "telegram": {"operations": ["rename_group", "leave_group"]},
    }
    app, _, _, seen = setup(tmp_path, supported)
    with TestClient(app) as client:
        tools = rpc(client, "tools/list").json()["result"]["tools"]
        assert tools
        for tool in tools:
            if "operation" in tool["inputSchema"]["properties"]:
                assert tool["inputSchema"]["properties"]["operation"]["enum"] == ["rename_group"]
        args = {"account": "synthetic", "operation": "rename_group", "parameters": {"title": "Synthetic"}}
        assert rpc(
            client, "tools/call", {"name": "messages_preview_messenger_operation", "arguments": args}
        ).json()["result"]["isError"]
        assert not seen
        assert (
            not rpc(
                client,
                "tools/call",
                {
                    "name": "messages_preview_messenger_operation",
                    "arguments": {**args, "account": "telegram"},
                },
            )
            .json()["result"]
            .get("isError")
        )
        listed = client.post(
            "/v1/messages_list_messenger_operations",
            json={"account": "synthetic"},
            headers={"Authorization": "Bearer gateway-a"},
        ).json()
        assert listed["items"] == []


def test_missing_support_contract_hides_actions_instead_of_guessing(tmp_path):
    app, _, _, seen = setup(tmp_path, {"synthetic": {"count": 43, "provider": "whatsmeow"}})
    with TestClient(app) as client:
        assert [t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]] == [
            "messages_get_messenger_operation"
        ]
        assert rpc(
            client,
            "tools/call",
            {
                "name": "messages_preview_messenger_operation",
                "arguments": {
                    "account": "synthetic",
                    "operation": "rename_group",
                    "parameters": {"title": "Synthetic"},
                },
            },
        ).json()["result"]["isError"]
    assert not seen


@pytest.mark.parametrize(
    "report",
    [None, [], "whatsmeow", {}, {"operations": "rename_group"}, {"operations": ["rename_group", None]}],
)
def test_malformed_support_never_enables_actions(tmp_path, report):
    app, _, _, seen = setup(tmp_path, {"synthetic": report})
    with TestClient(app) as client:
        assert [t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]] == [
            "messages_get_messenger_operation"
        ]
    assert not seen


def test_passive_receipts_survive_provider_support_change_without_new_actions(tmp_path):
    app, _, _, seen = setup(
        tmp_path, {"synthetic": {"operations": []}}, selected=["rename_group", "leave_group"]
    )
    with TestClient(app) as client:
        result = rpc(
            client,
            "tools/call",
            {
                "name": "messages_get_messenger_operation",
                "arguments": {"operation_id": "old-synthetic-operation"},
            },
        ).json()["result"]
        assert not result.get("isError")
    assert [path for path, _ in seen] == ["/v1/get_messenger_operation"]


def test_native_contact_gaps_do_not_remove_telegram_support_or_expand_grants(tmp_path):
    app, value, path, seen = setup(
        tmp_path,
        {
            "wa": {"provider": "whatsmeow", "operations": ["block_contact"], "available_operations": ["add_contact"]},
            "tg": {
                "operations": ["add_contact", "block_contact", "unreviewed_operation"],
                "available_operations": ["add_contact", "block_contact", "unreviewed_operation"],
            },
        },
        selected=["add_contact"],
        permissions=["contacts-manage"],
    )
    value["profiles"]["reader"]["tools"].append("messages_capabilities")
    value["revision"] += 1
    path.write_text(json.dumps(value))
    with TestClient(app) as client:
        tools = rpc(client, "tools/list").json()["result"]["tools"]
        for tool in tools:
            if "operation" in tool["inputSchema"]["properties"]:
                assert tool["inputSchema"]["properties"]["operation"]["enum"] == ["add_contact"]
        caps = client.post(
            "/v1/messages_capabilities", json={"detail": "full"}, headers={"Authorization": "Bearer gateway-a"}
        ).json()
        assert caps["gateway_actions"] == ["add_contact"]
        assert caps["gateway_actions_by_account"] == {"wa": [], "tg": ["add_contact"]}
        args = {
            "account": "wa",
            "operation": "add_contact",
            "parameters": {"phone": "+123456789", "first_name": "Synthetic"},
        }
        assert rpc(
            client, "tools/call", {"name": "messages_preview_messenger_operation", "arguments": args}
        ).json()["result"]["isError"]
        assert not seen
        assert (
            not rpc(
                client,
                "tools/call",
                {"name": "messages_preview_messenger_operation", "arguments": {**args, "account": "tg"}},
            )
            .json()["result"]
            .get("isError")
        )
