import hashlib
import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG
from test_policy import config
from test_transport import rpc


def setup(tmp_path, supported=None, selected=None, permissions=None, items=None, backend_permissions=None):
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
                    "permissions": agent["grants"]["messaging"] if backend_permissions is None else backend_permissions,
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
                    "items": items
                    if items is not None
                    else [
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


def listed_operations(client, account="synthetic"):
    """The shaped list through REST and MCP; both transports return the same result."""
    rest = client.post(
        "/v1/messages_list_messenger_operations", json={"account": account}, headers={"Authorization": "Bearer gateway-a"}
    )
    assert rest.status_code == 200
    mcp = rpc(
        client, "tools/call", {"name": "messages_list_messenger_operations", "arguments": {"account": account}}
    ).json()["result"]
    assert not mcp.get("isError") and mcp["structuredContent"] == rest.json()
    return rest.json()


def layered(tmp_path, profile, principal, grants, backend, items=None):
    """Profile and principal actions, gateway messaging grants and backend key permissions set independently."""
    supported = {"synthetic": {"operations": ["rename_group", "archive_chat"]}}
    rows = items if items is not None else [
        {"operation": n, "supported": True, "available": True} for n in ["rename_group", "archive_chat"]
    ]
    app, value, path, _ = setup(tmp_path, supported, permissions=grants, backend_permissions=backend, items=rows)
    value["profiles"]["reader"]["actions"]["messenger_operations"] = profile
    value["principals"][0]["actions"]["messenger_operations"] = principal
    value["revision"] += 1
    path.write_text(json.dumps(value))
    return app, rows


BOTH = ["rename_group", "archive_chat"]
SCOPES = ["groups-manage", "chat-manage"]


# spec: ANYM-846
@pytest.mark.parametrize(
    ("profile", "principal", "grants", "backend", "missing"),
    [
        pytest.param(["rename_group"], BOTH, SCOPES, SCOPES, ["profile_action"], id="profile_action"),
        pytest.param(BOTH, ["rename_group"], SCOPES, SCOPES, ["principal_action"], id="principal_action"),
        pytest.param(BOTH, BOTH, ["groups-manage"], SCOPES, ["gateway_scope:chat-manage"], id="gateway_scope"),
        pytest.param(BOTH, BOTH, SCOPES, ["groups-manage"], ["backend_permission:chat-manage"],
                     id="backend_permission"),
        pytest.param(["rename_group"], BOTH, ["groups-manage"], SCOPES,
                     ["profile_action", "gateway_scope:chat-manage"], id="two_layers"),
        pytest.param(["rename_group"], ["rename_group"], ["groups-manage"], ["groups-manage"],
                     ["profile_action", "principal_action", "gateway_scope:chat-manage",
                      "backend_permission:chat-manage"], id="every_layer"),
    ],
)
def test_a_supported_operation_outside_the_action_set_is_listed_with_each_missing_grant(
    tmp_path, profile, principal, grants, backend, missing
):
    app, rows = layered(tmp_path, profile, principal, grants, backend)
    with TestClient(app) as client:
        listed = listed_operations(client)
    # items stays exactly as before: the granted rows, unchanged.
    assert listed["items"] == [rows[0]]
    assert listed["not_granted"] == [{"operation": "archive_chat", "missing": missing}]


# spec: ANYM-846
def test_a_fully_granted_principal_has_an_empty_not_granted_list(tmp_path):
    app, rows = layered(tmp_path, BOTH, BOTH, SCOPES, SCOPES)
    with TestClient(app) as client:
        listed = listed_operations(client)
    assert listed["items"] == rows
    assert listed["not_granted"] == []


# spec: ANYM-846
def test_unsupported_unknown_and_malformed_rows_never_appear_and_the_rest_still_shapes(tmp_path):
    rows = [
        # Granted but not available (opt-in, full account): stays in items as today, never in not_granted.
        {"operation": "rename_group", "supported": True, "available": False, "requires_full_account": True},
        {"operation": "archive_chat", "supported": False, "available": False},
        {"operation": "teleport", "supported": True},
        {"operation": "create_group", "supported": "true"},
        {"operation": "create_group"},
        {"supported": True},
        {"operation": ["leave_group"], "supported": True},
        {"operation": None, "supported": True},
        "leave_group",
        None,
        7,
        ["leave_group"],
        # Not granted and not opted in: reported by its missing grant only, never by the opt-in.
        {"operation": "leave_group", "supported": True, "available": False, "opt_in_required": True},
        {"operation": "leave_group", "supported": True},
    ]
    app, _, _, _ = setup(tmp_path, items=rows)
    with TestClient(app) as client:
        listed = listed_operations(client)
    assert listed["items"] == [rows[0]]
    assert listed["not_granted"] == [{"operation": "leave_group", "missing": ["profile_action"]}]


# spec: ANYM-846
def test_an_operation_listed_as_not_granted_stays_denied_in_discovery_preview_execute_and_receipts(tmp_path):
    app, value, path, seen = setup(tmp_path)
    value["profiles"]["reader"]["tools"].append("messages_manage_chat")
    value["revision"] += 1
    path.write_text(json.dumps(value))
    headers = {"Authorization": "Bearer gateway-a"}
    with TestClient(app) as client:
        listed = listed_operations(client)
        assert listed["not_granted"] == [
            {"operation": "leave_group", "missing": ["profile_action"]},
            {"operation": "remove_participant", "missing": ["profile_action", "principal_action"]},
        ]
        tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
        assert "messages_manage_chat" in tools
        for tool in tools.values():
            if "operation" in tool["inputSchema"]["properties"]:
                assert tool["inputSchema"]["properties"]["operation"]["enum"] == ["rename_group"], tool["name"]
        for operation in [row["operation"] for row in listed["not_granted"]]:
            low = {"account": "synthetic", "operation": operation, "parameters": {"title": "Synthetic"}}
            risk = {"account": "synthetic", "operation": operation, "chat_id": "chat-synthetic",
                    "parameters": {"title": "Synthetic"}}
            calls = [
                ("messages_preview_messenger_operation", low),
                ("messages_execute_messenger_operation",
                 {**low, "request_id": "synthetic-request", "preview_digest": "a" * 64}),
                ("messages_manage_chat", {**risk, "dry_run": True}),
                ("messages_manage_chat",
                 {**risk, "dry_run": False, "request_id": "synthetic-request", "preview_digest": "a" * 64}),
            ]
            for name, args in calls:
                count = len(seen)
                assert client.post("/v1/" + name, json=args, headers=headers).status_code == 403, (name, operation)
                assert rpc(client, "tools/call", {"name": name, "arguments": args}).json()["result"]["isError"]
                assert len(seen) == count, (name, operation)
        # The backend names leave_group for this receipt; the gateway still withholds it.
        receipt = {"operation_id": "synthetic-leave"}
        assert client.post("/v1/messages_get_messenger_operation", json=receipt, headers=headers).status_code == 403
        assert rpc(
            client, "tools/call", {"name": "messages_get_messenger_operation", "arguments": receipt}
        ).json()["result"]["isError"]


# spec: ANYM-846
def test_the_list_description_says_a_not_granted_operation_exists_and_names_the_grant_to_ask_for():
    description = CATALOG["messages_list_messenger_operations"]["description"]
    for code in ("not_granted", "profile_action", "principal_action", "gateway_scope:<scope>",
                 "backend_permission:<scope>", "do not report a missing feature", "ask the owner for the named grant"):
        assert code in description, code


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
