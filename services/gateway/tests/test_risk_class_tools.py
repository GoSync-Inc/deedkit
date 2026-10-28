"""Risk-class messenger operation tools route only the granted actions they carry (ANYM-549); synthetic only."""
import asyncio
import hashlib
import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG
from agent_gateway.http_clients import Clients, admission, wait_tools
from test_policy import config
from test_transport import rpc

RISK_TOOLS = ["messages_change_message", "messages_manage_chat", "messages_change_contacts", "messages_read_contacts"]
LOW_LEVEL = ["messages_list_messenger_operations", "messages_preview_messenger_operation",
             "messages_execute_messenger_operation", "messages_get_messenger_operation"]
HEADERS = {"Authorization": "Bearer gateway-a"}
REACTION = {"account": "synthetic", "operation": "send_reaction", "chat_id": "chat-synthetic",
            "message_id": "msg-synthetic", "expected_revision": 3, "parameters": {"emoji": "+"}}


def setup(tmp_path, actions, wait_bound=None):
    value = config()
    value["deployment"] = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://backend.test"}}}
    value["profiles"]["reader"] = {"tools": RISK_TOOLS + LOW_LEVEL, "actions": {"messenger_operations": actions}}
    agent = value["principals"][0]
    # The key holds every scope; only the action ceiling narrows it.
    agent["grants"] = {"messaging": ["message-react", "message-edit", "message-delete", "contacts-read"]}
    agent["actions"] = {"messenger_operations": actions}
    backend_key = tmp_path / "backend.key"
    backend_key.write_text("backend-synthetic")
    backend_key.chmod(0o600)
    agent["credentials"] = {"messaging": {"key_file": str(backend_key),
                                          "key_sha256": hashlib.sha256(b"backend-synthetic").hexdigest()}}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    seen = []
    block = {"supported_by_account": {"synthetic": {"operations": ["send_reaction", "edit_message",
                                                                   "delete_message", "list_contacts"]}}}
    if wait_bound is not None:
        block["execute_wait_s_max"] = wait_bound

    def handler(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": agent["grants"]["messaging"],
                                             "messenger_operations": block})
        args = json.loads(request.content)
        seen.append((request.url.path, args))
        return httpx.Response(200, json={"operation": args.get("operation"), "preview_digest": "d" * 64,
                                         "status": "queued", "operation_id": "op-synthetic"})

    app = create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(handler)})
    return app, seen


def call(client, name, args):
    return client.post("/v1/" + name, json=args, headers=HEADERS), rpc(
        client, "tools/call", {"name": name, "arguments": args}).json()["result"]


# spec: ANYM-549
def test_a_react_only_key_sees_one_risk_class_tool_with_only_its_operation(tmp_path):
    app, seen = setup(tmp_path, ["send_reaction"])
    with TestClient(app) as c:
        tools = {t["name"]: t for t in rpc(c, "tools/list").json()["result"]["tools"]}
    assert set(tools) & set(RISK_TOOLS) == {"messages_change_message"}
    assert tools["messages_change_message"]["inputSchema"]["properties"]["operation"]["enum"] == ["send_reaction"]
    assert seen == []


# spec: ANYM-549
def test_a_react_only_key_cannot_edit_or_delete_through_any_tool_and_no_backend_sees_it(tmp_path):
    app, seen = setup(tmp_path, ["send_reaction"])
    with TestClient(app) as c:
        for name in ("messages_change_message", "messages_execute_messenger_operation",
                     "messages_preview_messenger_operation"):
            for operation in ("edit_message", "delete_message", "list_contacts"):
                args = {**REACTION, "operation": operation, "request_id": "synthetic-forged"}
                if name != "messages_preview_messenger_operation":
                    args["preview_digest"] = "d" * 64
                rest, mcp = call(c, name, args)
                # An operation outside a risk-class tool's static enum fails its schema before admission.
                outside = name == "messages_change_message" and operation == "list_contacts"
                assert rest.status_code == (422 if outside else 403) and mcp["isError"], (name, operation)
        # A tool this key cannot see is refused too, also for an operation it holds elsewhere.
        rest, mcp = call(c, "messages_manage_chat", {**REACTION, "request_id": "synthetic-forged"})
        assert rest.status_code in (403, 404) and mcp["isError"]
    assert seen == []


# spec: ANYM-549
def test_dry_run_is_a_preview_without_request_id_and_live_call_is_an_execute(tmp_path):
    app, seen = setup(tmp_path, ["send_reaction"])
    with TestClient(app) as c:
        preview = c.post("/v1/messages_change_message", json={**REACTION, "dry_run": True,
                                                               "request_id": "synthetic-req-1"}, headers=HEADERS)
        execute = c.post("/v1/messages_change_message", json={**REACTION, "request_id": "synthetic-req-1",
                                                               "preview_digest": "d" * 64}, headers=HEADERS)
    assert preview.status_code == 200 and execute.status_code == 200
    assert seen[0][0] == "/v1/preview_messenger_operation"
    assert not {"request_id", "dry_run", "wait_s", "preview_digest"} & set(seen[0][1])
    assert seen[1][0] == "/v1/execute_messenger_operation"
    assert seen[1][1]["request_id"] == "synthetic-req-1" and "dry_run" not in seen[1][1] and "wait_s" not in seen[1][1]


# spec: ANYM-549
def test_a_live_call_without_request_id_is_refused_with_a_placeholder_next(tmp_path):
    app, seen = setup(tmp_path, ["send_reaction"])
    with TestClient(app) as c:
        refused = c.post("/v1/messages_change_message", json=REACTION, headers=HEADERS)
    assert refused.status_code == 422
    body = refused.json()
    assert body["field_errors"] == [{"field": "request_id", "reason": "required"}]
    assert body["next"]["tool"] == "messages_change_message"
    assert body["next"]["arguments"]["request_id"] != "" and seen == []


# spec: ANYM-549
def test_wait_s_reaches_execute_only_when_the_backend_names_its_bound(tmp_path):
    live = {**REACTION, "request_id": "synthetic-req-2", "preview_digest": "d" * 64, "wait_s": 5}
    app, seen = setup(tmp_path, ["send_reaction"])
    with TestClient(app) as c:
        for name in ("messages_change_message", "messages_execute_messenger_operation"):
            refused = c.post("/v1/" + name, json=live, headers=HEADERS)
            assert refused.status_code == 409 and refused.json()["error"] == "unsupported_by_backend"
    assert seen == []
    app, seen = setup(tmp_path, ["send_reaction"], wait_bound=25)
    with TestClient(app) as c:
        assert c.post("/v1/messages_change_message", json=live, headers=HEADERS).status_code == 200
        assert c.post("/v1/messages_preview_messenger_operation", json={**REACTION, "request_id": "synthetic-req-2"},
                      headers=HEADERS).status_code == 200
    assert seen[0] == ("/v1/execute_messenger_operation", {**{k: v for k, v in live.items()}, "wait_s": 5})
    assert seen[1][0] == "/v1/preview_messenger_operation" and "request_id" not in seen[1][1]


# spec: ANYM-549
def test_an_execute_wait_is_admitted_in_the_wait_pool_and_a_saturated_pool_refuses_it():
    tools = wait_tools(CATALOG)
    for name in RISK_TOOLS:
        operation = CATALOG[name]["operation"]
        assert admission(tools, "messaging", operation, {"wait_s": 5}) == "waits"
        assert admission(tools, "messaging", operation, {"wait_s": 5, "dry_run": True}) == "metadata"
    assert admission(tools, "messaging", "execute_messenger_operation", {"wait_s": 5}) == "waits"
    assert admission(tools, "messaging", "execute_messenger_operation", {"wait_s": 0}) == "metadata"

    async def run():
        release = asyncio.Event()

        async def handler(request):
            if json.loads(request.content or b"{}").get("wait_s", 0) > 0:
                await release.wait()
            return httpx.Response(200, json={"ok": True})

        deployment = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://messaging"}}}
        live = {**REACTION, "request_id": "synthetic-req-3", "wait_s": 20}
        async with Clients(deployment, {"messaging": httpx.MockTransport(handler)}) as clients:
            budget = clients.wait_budgets["messaging"]
            waits = [asyncio.create_task(clients.call("messaging", "execute_messenger_operation", "synthetic", live))
                     for _ in range(budget.capacity)]
            try:
                await asyncio.sleep(0.05)
                assert budget.pending == budget.capacity
                with pytest.raises(Exception, match="backend_busy"):
                    await clients.call("messaging", "execute_messenger_operation", "synthetic", live)
                async with asyncio.timeout(1):
                    plain = {**live, "wait_s": 0}
                    assert (await clients.call("messaging", "execute_messenger_operation", "synthetic", plain))["ok"]
            finally:
                release.set()
                await asyncio.gather(*waits, return_exceptions=True)
            assert budget.pending == 0

    asyncio.run(run())
