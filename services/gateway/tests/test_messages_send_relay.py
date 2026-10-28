"""messages_send through the gateway: its wait pool, its file grant and its phone grant (ANYM-546)."""
import asyncio
import hashlib
import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG
from agent_gateway.http_clients import Clients, admission, wait_tools
from agent_gateway.policy import Denied
from test_policy import config

HEADERS = {"Authorization": "Bearer gateway-a"}
DEPLOYMENT = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://messaging"}}}
SEND = {"to": "chat_" + "b" * 32, "text": "synthetic text", "request_id": "req-messages-send-1"}
FILES = [{"upload_id": "upl_" + "c" * 32}]


def setup(tmp_path, actions, operations=("open_chat_by_phone", "send_file"), permissions=("read", "send", "upload", "download")):
    value = config()
    value["deployment"] = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://backend.test"}}}
    value["profiles"]["reader"] = {"tools": ["messages_send"], "actions": {"messenger_operations": list(actions)}}
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

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": list(permissions), "messenger_operations": {
                "supported_by_account": {"synthetic": {"provider": "whatsmeow", "operations": list(operations)}}}})
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"state": "previewed"})

    return create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(backend)}), seen


# spec: ANYM-546
def test_a_waiting_or_dry_run_messages_send_takes_the_wait_pool_and_a_plain_send_stays_metadata():
    tools = wait_tools(CATALOG)
    assert admission(tools, "messaging", "messages_send", {**SEND, "wait_s": 5}) == "waits"
    assert admission(tools, "messaging", "messages_send", {**SEND, "dry_run": True}) == "waits"
    assert admission(tools, "messaging", "messages_send", {**SEND, "wait_s": 0}) == "metadata"
    assert admission(tools, "messaging", "messages_send", SEND) == "metadata"


# spec: ANYM-546
def test_a_full_wait_pool_refuses_another_waiting_messages_send_but_admits_a_plain_one():
    async def run():
        release, received = asyncio.Event(), []

        async def handler(request):
            received.append(json.loads(request.content))
            if received[-1].get("wait_s", 0) > 0:
                await release.wait()
            return httpx.Response(200, json={"state": "succeeded"})

        async with Clients(DEPLOYMENT, {"messaging": httpx.MockTransport(handler)}) as clients:
            budget = clients.wait_budgets["messaging"]
            slow = {**SEND, "wait_s": 20}
            parked = [asyncio.create_task(clients.call("messaging", "messages_send", "synthetic", slow))
                      for _ in range(budget.capacity)]
            try:
                await asyncio.sleep(0.05)
                assert budget.pending == budget.capacity
                with pytest.raises(Denied, match="backend_busy"):
                    await clients.call("messaging", "messages_send", "synthetic", slow)
                async with asyncio.timeout(1):
                    assert (await clients.call("messaging", "messages_send", "synthetic", SEND))["state"] == "succeeded"
                # The plain send went past the parked waits; no waiting call was admitted beyond the pool.
                assert received[-1] == SEND and sum(1 for r in received if r.get("wait_s")) <= budget.capacity
            finally:
                release.set()
                await asyncio.gather(*parked, return_exceptions=True)
            assert budget.pending == 0

    asyncio.run(run())


# spec: ANYM-546
@pytest.mark.parametrize("actions,operations,permissions", [
    (["open_chat_by_phone"], ("open_chat_by_phone", "send_file"), ("read", "send", "upload")),  # no send_file grant
    (["send_file"], ("open_chat_by_phone",), ("read", "send", "upload")),  # the account cannot send files
    (["send_file"], ("open_chat_by_phone", "send_file"), ("read", "send", "upload")),  # no download scope
])
def test_files_without_the_send_file_grant_are_refused_before_the_backend(tmp_path, actions, operations, permissions):
    app, seen = setup(tmp_path, actions, operations, permissions)
    with TestClient(app) as client:
        result = client.post("/v1/messages_send", json={**SEND, "files": FILES}, headers=HEADERS)
    assert result.status_code == 403 and seen == []


# spec: ANYM-546
def test_files_with_the_send_file_grant_reach_the_backend(tmp_path):
    app, seen = setup(tmp_path, ["send_file"])
    with TestClient(app) as client:
        result = client.post("/v1/messages_send", json={**SEND, "files": FILES}, headers=HEADERS)
    assert result.status_code == 200 and seen[-1][0] == "/v1/messages_send" and seen[-1][1]["files"] == FILES


# spec: ANYM-546
@pytest.mark.parametrize("actions,expected", [(["open_chat_by_phone"], True), ([], False)])
def test_allow_open_chat_is_the_gateways_grant_and_never_the_agents_word(tmp_path, actions, expected):
    app, seen = setup(tmp_path, actions)
    with TestClient(app) as client:
        for forged in (None, True, False):
            extra = {} if forged is None else {"allow_open_chat": forged}
            result = client.post("/v1/messages_send", json={**SEND, "to": "+12025550147", **extra}, headers=HEADERS)
            assert result.status_code == 200
    # Whatever the agent sends, the backend sees this gateway's grant.
    assert [args["allow_open_chat"] for _, args in seen] == [expected] * 3
