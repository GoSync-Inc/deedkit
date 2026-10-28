"""Every catalog tool with wait_s waits in a bounded wait pool, never the metadata pool (ANYM-544)."""
import asyncio
import json

import httpx
import pytest

from agent_gateway import http_clients
from agent_gateway.catalog import CATALOG
from agent_gateway.http_clients import WAIT_ADMISSION, Clients, admission, takes_wait, wait_tools
from agent_gateway.policy import Denied

DEPLOYMENT = {"modules": ["messaging", "calls"],
              "backends": {"messaging": {"url": "http://messaging"}, "calls": {"url": "http://calls"}}}
SLOW_SEND = {"account": "synthetic", "chat_id": "chat-synthetic", "text": "synthetic", "request_id": "req-wait-1",
             "wait_s": 20}
WAITING_ARGUMENTS = {"wait_s": 5, "live": "on", "timeout": 20}


# spec: ANYM-544
def test_every_catalog_tool_with_wait_s_has_an_admission_entry_and_a_wait_pool():
    waiting = [t for t in CATALOG.values() if takes_wait(t)]
    assert {t["name"] for t in waiting} >= {"messages_send_message", "messages_search"}
    tools = wait_tools(CATALOG)
    for tool in waiting:
        key = (tool["module"], tool["operation"])
        assert key in WAIT_ADMISSION, f"{tool['name']} takes wait_s but has no admission entry"
        assert admission(tools, *key, WAITING_ARGUMENTS) in {"waits", "searches"}, tool["name"]
    # No entry outlives its tool, and wait_s=0 stays a plain call.
    assert all(any((t["module"], t["operation"]) == key for t in CATALOG.values()) for key in WAIT_ADMISSION)
    assert admission(tools, "messaging", "send_message", {"wait_s": 0}) == "metadata"
    assert admission(tools, "messaging", "get_send_status", {}) == "metadata"


# spec: ANYM-544
def test_a_new_wait_s_tool_is_selected_from_the_catalog_and_gets_its_modules_wait_pool(monkeypatch):
    """A later story's tool (here a synthetic calls one) waits in a calls wait pool without a name list."""
    tool = {"name": "calls_synthetic_wait", "module": "calls", "operation": "synthetic_wait", "scopes": [],
            "inputSchema": {"type": "object", "properties": {"call_id": {"type": "string"},
                                                             "wait_s": {"type": "number", "minimum": 0, "maximum": 25}}}}
    monkeypatch.setitem(CATALOG, tool["name"], tool)

    async def run():
        seen = []

        def handler(request):
            seen.append(request.url.path)
            return httpx.Response(200, json={"ok": True})

        async with Clients(DEPLOYMENT, {m: httpx.MockTransport(handler) for m in DEPLOYMENT["modules"]}) as clients:
            assert admission(clients.wait_tools, "calls", "synthetic_wait", {"wait_s": 5}) == "waits"
            assert "calls" in clients.waits and "calls" in clients.wait_budgets
            assert (await clients.call("calls", "synthetic_wait", "synthetic", {"call_id": "c", "wait_s": 5}))["ok"]
        return seen

    assert asyncio.run(run()) == ["/v1/synthetic_wait"]
    assert (tool["module"], tool["operation"]) not in WAIT_ADMISSION  # so the catalog test above fails for it


# spec: ANYM-544
def test_a_saturated_wait_pool_leaves_sends_status_reads_and_stops_admitted():
    async def run():
        release, received = asyncio.Event(), []

        async def handler(request):
            body = json.loads(request.content or b"{}")
            received.append(request.url.path)
            if request.url.path == "/v1/wait_for_subscription_events" or body.get("wait_s", 0) > 0:
                await release.wait()
            return httpx.Response(200, json={"ok": True})

        transports = {m: httpx.MockTransport(handler) for m in DEPLOYMENT["modules"]}
        async with Clients(DEPLOYMENT, transports) as clients:
            budget = clients.wait_budgets["messaging"]
            waits = [asyncio.create_task(clients.call("messaging", "send_message", "synthetic", SLOW_SEND))
                     for _ in range(3)]
            waits += [asyncio.create_task(clients.call("messaging", "wait_for_subscription_events", "synthetic",
                                                       {"subscription_id": "sub-1", "timeout": 25})) for _ in range(3)]
            try:
                await asyncio.sleep(0.05)
                assert budget.pending == budget.capacity == 6
                with pytest.raises(Denied, match="backend_busy"):
                    await clients.call("messaging", "send_message", "synthetic", SLOW_SEND)
                async with asyncio.timeout(1):
                    for module, operation, args in (
                        ("messaging", "send_message", {**SLOW_SEND, "wait_s": 0}),
                        ("messaging", "get_send_status", {"send_id": "send-synthetic"}),
                        ("calls", "stop_call", {"call_id": "call-synthetic"}),
                        ("messaging", "capabilities", {}),
                    ):
                        assert (await clients.call(module, operation, "synthetic", args))["ok"] is True
                # The gateway does not poll: one backend request per running wait (two slots) plus the plain send,
                # and the waits hold no metadata slot.
                assert received.count("/v1/send_message") == 2 + 1
                assert clients.budgets["messaging"].pending == 0
            finally:
                release.set()
                await asyncio.gather(*waits, return_exceptions=True)
            assert budget.pending == 0

    asyncio.run(run())


# spec: ANYM-544
def test_a_cancelled_or_timed_out_wait_releases_its_slot():
    async def run():
        async def handler(request):
            if json.loads(request.content).get("text") == "timeout":
                raise httpx.ReadTimeout("synthetic", request=request)
            await asyncio.Event().wait()

        async with Clients(DEPLOYMENT, {m: httpx.MockTransport(handler) for m in DEPLOYMENT["modules"]}) as clients:
            budget = clients.wait_budgets["messaging"]
            parked = [asyncio.create_task(clients.call("messaging", "send_message", "synthetic", SLOW_SEND))
                      for _ in range(2)]
            await asyncio.sleep(0.02)
            assert budget.pending == 2
            parked[0].cancel()
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await parked[1]
            await asyncio.gather(*parked, return_exceptions=True)
            assert budget.pending == 0
            with pytest.raises(Denied) as failure:
                await clients.call("messaging", "send_message", "synthetic", {**SLOW_SEND, "text": "timeout"})
            assert failure.value.detail["reason"] == "gateway_timeout" and budget.pending == 0
            # Both slots are free again: two new waits are admitted at once.
            again = [asyncio.create_task(clients.call("messaging", "send_message", "synthetic", SLOW_SEND))
                     for _ in range(2)]
            await asyncio.sleep(0.02)
            assert budget.pending == 2 and budget.semaphore.locked()
            for task in again:
                task.cancel()
            await asyncio.gather(*again, return_exceptions=True)
            assert budget.pending == 0

    asyncio.run(run())


# spec: ANYM-544
def test_the_wait_pool_has_its_own_connections():
    """A parked wait holds a wait-pool connection only; the metadata pool's connections stay free for sends."""
    async def run():
        async with Clients(DEPLOYMENT, {m: httpx.MockTransport(lambda r: httpx.Response(200, json={}))
                                        for m in DEPLOYMENT["modules"]}) as clients:
            assert clients.waits["messaging"] is not clients.metadata["messaging"]
            assert "calls" in clients.waits  # ANYM-551: calls_call and calls_call_result take wait_s
            assert http_clients.WAIT_ADMISSION[("messaging", "search")][0] == "searches"

    asyncio.run(run())
