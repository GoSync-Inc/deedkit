"""Selected consumption, delivery configuration and private feedback contracts."""
import asyncio
import json
import logging

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.catalog import CATALOG, arguments
from agent_gateway.http_clients import Clients
from agent_gateway.policy import Denied
from test_diagnostics import configured, HEADERS
from test_transport import rpc


TOOLS = [
    ("fetch_subscription_events", {"subscription_id": "subscription-1"}, ["read"]),
    ("ack_subscription_events", {"subscription_id": "subscription-1", "receipt": "r" * 43, "version": 1}, ["read", "subscription-ack"]),
    ("wait_for_subscription_events", {"subscription_id": "subscription-1", "timeout": 0}, ["read"]),
    ("configure_subscription_delivery", {"subscription_id": "subscription-1", "version": 1, "receiver_id": "11111111-1111-4111-8111-111111111111"}, ["read", "subscription-write"]),
    ("test_subscription", {"subscription_id": "subscription-1", "request_id": "synthetic-request"}, ["read", "subscription-write"]),
    ("submit_feedback", {"request_id": "synthetic-request", "category": "bug", "description": "Synthetic feedback"}, ["read", "feedback-write"]),
    ("list_feedback", {}, ["read"]),
]


@pytest.mark.parametrize("operation,values,permissions", TOOLS)
def test_selected_new_tools_forward_over_real_mcp_and_rest(tmp_path, operation, values, permissions):
    seen = []

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": permissions, "tool_manifest": {"names": [operation]}})
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"synthetic": True, "items": []})

    name = "messages_" + operation
    # A private policy can select exact new names; no default profile gains them.
    assert name in CATALOG
    app, _, _ = configured(tmp_path, backend, module="messaging", tools=[name], grants=permissions)
    with TestClient(app) as client:
        assert [t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]] == [name]
        rest = client.post("/v1/" + name, json=values, headers=HEADERS)
        mcp = rpc(client, "tools/call", {"name": name, "arguments": values}).json()["result"]
    assert rest.status_code == 200 and not mcp.get("isError")
    assert rest.json() == mcp["structuredContent"]
    assert seen[0] == seen[1]
    assert seen[0][0] == "/v1/" + operation
    assert all(seen[0][1][key] == value for key, value in values.items())


@pytest.mark.parametrize("operation,values,permissions", TOOLS)
def test_new_tools_fail_closed_without_backend_manifest(tmp_path, operation, values, permissions):
    def backend(request):
        assert request.url.path == "/v1/capabilities"
        return httpx.Response(200, json={"permissions": permissions})

    name = "messages_" + operation
    assert name in CATALOG
    app, _, _ = configured(tmp_path, backend, module="messaging", tools=[name], grants=permissions)
    with TestClient(app) as client:
        assert rpc(client, "tools/list").json()["result"]["tools"] == []
        assert client.post("/v1/" + name, json=values, headers=HEADERS).status_code == 403
        assert rpc(client, "tools/call", {"name": name, "arguments": values}).json()["result"]["isError"]


@pytest.mark.parametrize("operation,values", [
    ("ack_subscription_events", {"subscription_id": "s", "receipt": "r" * 43, "version": 0}),
    ("wait_for_subscription_events", {"subscription_id": "s", "timeout": 26}),
    ("wait_for_subscription_events", {"subscription_id": "s", "timeout": True}),
    ("configure_subscription_delivery", {"subscription_id": "s", "version": 1, "receiver_id": "r", "url": "https://example.test"}),
    ("submit_feedback", {"request_id": "request-1", "category": "bug", "description": "ok", "refs": {"raw_token": "secret"}}),
    ("submit_feedback", {"request_id": "request-1", "category": "bug", "description": "x" * 4001}),
])
def test_new_tool_arguments_are_bounded_and_reject_arbitrary_endpoints(operation, values):
    assert "messages_" + operation in CATALOG
    with pytest.raises(ValueError):
        arguments(CATALOG["messages_" + operation], values)


def test_wait_pool_is_bounded_and_does_not_starve_message_reads():
    async def run():
        entered = 0
        ready, release = asyncio.Event(), asyncio.Event()

        async def backend(request):
            nonlocal entered
            if request.url.path == "/v1/wait_for_subscription_events":
                entered += 1
                if entered == 2:
                    ready.set()
                await release.wait()
            return httpx.Response(200, json={"items": []})

        deployment = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://backend.test"}}}
        async with Clients(deployment, {"messaging": httpx.MockTransport(backend)}) as clients:
            tasks = [asyncio.create_task(clients.call("messaging", "wait_for_subscription_events", "synthetic", {}))
                     for _ in range(6)]
            try:
                async with asyncio.timeout(1):
                    await ready.wait()
                    while clients.wait_budgets["messaging"].pending < 6:
                        await asyncio.sleep(0)
                with pytest.raises(Denied, match="backend_busy"):
                    await clients.call("messaging", "wait_for_subscription_events", "synthetic", {})
                async with asyncio.timeout(0.2):
                    assert await clients.call("messaging", "search", "synthetic", {}) == {"items": []}
            finally:
                release.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            assert clients.wait_budgets["messaging"].pending == 0
        assert clients.closed

    asyncio.run(run())


@pytest.mark.parametrize("code,expected", [("stale_ack", "stale_ack"),
                                           ("private_internal_error", "backend_denied")])
def test_consumption_error_diagnostics_never_copy_arbitrary_backend_text(tmp_path, code, expected):
    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read", "subscription-ack"],
                                            "tool_manifest": {"names": ["ack_subscription_events"]}})
        return httpx.Response(409, json={"error": code, "request_id": "c" * 32,
                                        "next_action": "PRIVATE BACKEND INSTRUCTION", "private": "synthetic-secret"})

    name = "messages_ack_subscription_events"
    app, _, _ = configured(tmp_path, backend, module="messaging", tools=[name], grants=["read", "subscription-ack"])
    values = {"subscription_id": "s", "receipt": "r" * 43, "version": 1}
    with TestClient(app) as client:
        response = client.post("/v1/" + name, json=values, headers=HEADERS)
        mcp = rpc(client, "tools/call", {"name": name, "arguments": values}).json()["result"]
    assert response.status_code == 409 and mcp["isError"]
    assert response.json() == mcp["structuredContent"] == {
        "error": expected, "backend_status": 409, "request_id": "c" * 32,
    }


FEEDBACK = {"request_id": "synthetic-request", "category": "bug", "description": "Synthetic feedback"}
GOOD_FIELD_ERRORS = [
    {"field": "category", "reason": "enum", "allowed": ["bug", "feature", "quality", "other"]},
    {"field": "description", "reason": "blank", "expected": "text with a non-space character"},
    {"field": "refs/message_ids/0", "reason": "format"},
]


def feedback_failure(tmp_path, status, body):
    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read", "feedback-write"],
                                            "tool_manifest": {"names": ["submit_feedback"]}})
        if isinstance(body, bytes):
            return httpx.Response(status, content=body, headers={"content-type": "application/json"})
        return httpx.Response(status, json=body)

    name = "messages_submit_feedback"
    app, _, _ = configured(tmp_path, backend, module="messaging", tools=[name], grants=["read", "feedback-write"])
    with TestClient(app) as client:
        rest = client.post("/v1/" + name, json=FEEDBACK, headers=HEADERS)
        mcp = rpc(client, "tools/call", {"name": name, "arguments": FEEDBACK}).json()["result"]
    assert mcp["isError"] and rest.json() == mcp["structuredContent"]
    return rest


# spec: ANYM-295
def test_feedback_field_errors_relay_end_to_end(tmp_path):
    rest = feedback_failure(tmp_path, 422, {
        "error": "invalid_feedback", "request_id": "c" * 32, "next_action": "PRIVATE BACKEND INSTRUCTION",
        "field_errors": GOOD_FIELD_ERRORS, "private": "synthetic-secret"})
    assert rest.status_code == 422
    assert rest.json() == {"error": "invalid_feedback", "backend_status": 422, "request_id": "c" * 32,
                           "field_errors": GOOD_FIELD_ERRORS}


CANARY = "synthetic-canary-do-not-echo"


# spec: ANYM-295
@pytest.mark.parametrize("malformed", [
    [{"field": f"f{i}", "reason": "type"} for i in range(21)],
    [{"field": "category", "reason": "invented"}],
    [{"field": "not an identifier", "reason": "enum"}],
    [{"field": "a" * 129, "reason": "enum"}],
    [{"field": "refs//0", "reason": "format"}],
    [{"field": "category", "reason": "enum"}, {"field": "category", "reason": "type"}],
    [{"field": "category", "reason": "enum", "private": "synthetic-secret"}],
    [{"field": "category", "reason": "enum", "expected": ["bug"]}],
    [{"field": "category", "reason": "enum", "allowed": [{"nested": "x"}]}],
    [{"field": "category", "reason": "enum", "allowed": ["x" * 65]}],
    [{"field": "category", "reason": "enum", "allowed": [str(i) for i in range(51)]}],
    [{"field": "category", "reason": "enum", "allowed": []}],
    [{"field": "category", "reason": "enum", "allowed": [10**1000]}],
    [GOOD_FIELD_ERRORS[0], "category"],
    {"field": "category", "reason": "enum"},
    "category",
    [],
    # Provenance: a well-formed entry that carries text or a field the reviewed schema does not.
    [{"field": "description", "reason": "blank", "expected": CANARY}],
    [{"field": "description", "reason": "blank", "expected": "Synthetic feedback"}],
    [{"field": "category", "reason": "enum", "allowed": [CANARY]}],
    [{"field": "category", "reason": "enum", "allowed": ["bug", 1]}],
    [{"field": "category", "reason": "enum", "allowed": [True]}],
    [{"field": "synthetic_canary", "reason": "type"}],
    [{"field": "refs/other_ids/0", "reason": "format"}],
    [{"field": "category/0", "reason": "format"}],
    [GOOD_FIELD_ERRORS[0], {"field": "description", "reason": "blank", "expected": CANARY}],
    # A JSON container where a string belongs must not raise out of the relay.
    [{"field": "category", "reason": []}],
    [{"field": "category", "reason": {}}],
    [GOOD_FIELD_ERRORS[0], {"field": "description", "reason": []}],
    [{"field": ["category"], "reason": "enum"}],
])
def test_consumption_field_errors_validated(tmp_path, malformed):
    rest = feedback_failure(tmp_path, 422, {"error": "invalid_feedback", "request_id": "c" * 32,
                                            "field_errors": malformed})
    assert rest.status_code == 422
    assert rest.json() == {"error": "invalid_feedback", "backend_status": 422, "request_id": "c" * 32}


# spec: ANYM-295
@pytest.mark.parametrize("entry", [
    '{"field":"description","reason":"blank","expected":"\\ud800"}',
    '{"field":"category","reason":"enum","allowed":["\\ud800"]}',
])
def test_consumption_field_errors_with_a_lone_surrogate_are_dropped(tmp_path, entry):
    body = ('{"error":"invalid_feedback","request_id":"%s","field_errors":[%s]}' % ("c" * 32, entry)).encode()
    rest = feedback_failure(tmp_path, 422, body)
    assert rest.status_code == 422
    assert rest.json() == {"error": "invalid_feedback", "backend_status": 422, "request_id": "c" * 32}


# spec: ANYM-295
@pytest.mark.parametrize("entry", [
    {"field": "operation", "reason": "enum", "expected": "a documented operation name"},
    {"field": "request_id", "reason": "pattern", "expected": "8–128 characters: letters, digits and _ . : -"},
    {"field": "description", "reason": "pattern", "expected": "no control characters except newline and tab"},
    {"field": "refs", "reason": "size", "expected": "at most 8 KiB"},
    {"field": "refs/message_ids", "reason": "max_items", "expected": "at most 32 items"},
])
def test_every_reviewed_feedback_hint_of_the_backend_is_relayed(tmp_path, entry):
    rest = feedback_failure(tmp_path, 422, {"error": "invalid_feedback", "field_errors": [entry]})
    assert rest.json()["field_errors"] == [entry]


# spec: ANYM-295
def test_consumption_version_limit_hint_is_relayed():
    from agent_gateway.diagnostics import reviewed_field_errors
    entries = [{"field": "version", "reason": "maximum", "expected": "at most 9007199254740991"}]
    assert reviewed_field_errors("ack_subscription_events", entries) == entries
    assert reviewed_field_errors("configure_subscription_delivery", entries) == entries


# spec: ANYM-295
@pytest.mark.parametrize("expected,relayed", [
    ("matching ^[a-z][a-z0-9_]*$", True),
    ("matching " + CANARY, False),
    ("matching ^[a-z]*$", False),
])
def test_consumption_field_errors_pattern_hint_must_be_the_reviewed_pattern(tmp_path, expected, relayed):
    entries = [{"field": "operation", "reason": "pattern", "expected": expected}]
    rest = feedback_failure(tmp_path, 422, {"error": "invalid_feedback", "field_errors": entries})
    assert rest.json().get("field_errors") == (entries if relayed else None)
    assert CANARY not in rest.text


# spec: ANYM-295
def test_consumption_field_errors_dropped_with_an_unlisted_error_code(tmp_path):
    rest = feedback_failure(tmp_path, 422, {"error": "private_internal_error", "field_errors": GOOD_FIELD_ERRORS})
    assert rest.json() == {"error": "backend_denied", "backend_status": 422}


SUBSCRIBER = ["messages_create_subscription", "messages_wait_for_subscription_events",
              "messages_fetch_subscription_events", "messages_ack_subscription_events", "messages_get_subscription"]
SUBSCRIBER_PERMISSIONS = ["read", "subscription-write", "subscription-ack"]


def subscriber_backend():
    """A synthetic backend with one subscription and one event, consumed by fetch and ACK."""
    state = {"acked_through": 0, "event": 7}

    def backend(request):
        operation = request.url.path.removeprefix("/v1/")
        if operation == "capabilities":
            return httpx.Response(200, json={"permissions": SUBSCRIBER_PERMISSIONS, "tool_manifest": {
                "names": [n.removeprefix("messages_") for n in SUBSCRIBER]}})
        body = json.loads(request.content)
        pending = state["event"] > state["acked_through"]
        if operation == "create_subscription":
            return httpx.Response(200, json={"subscription_id": "subscription-1", "version": 1})
        if operation == "wait_for_subscription_events":
            assert 0 <= body["timeout"] <= 25
            return httpx.Response(200, json={"wait_status": "events_available" if pending else "timeout"})
        if operation == "fetch_subscription_events":
            return httpx.Response(200, json={
                "items": [{"event_id": "event-1", "position": state["event"]}] if pending else [],
                "receipt": "r" * 43 if pending else None, "ack_version": 1, "acked_through": state["acked_through"]})
        if operation == "ack_subscription_events":
            assert body["receipt"] == "r" * 43
            state["acked_through"] = state["event"]
            return httpx.Response(200, json={"subscription_id": "subscription-1", "acked_through": state["acked_through"]})
        assert operation == "get_subscription"
        return httpx.Response(200, json={"subscription_id": "subscription-1", "acked_through": state["acked_through"]})
    return backend


# spec: ANYM-518
@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_subscriber_waits_fetches_and_acks_through_the_gateway(tmp_path, transport):
    app, _, _ = configured(tmp_path, subscriber_backend(), module="messaging", tools=SUBSCRIBER, grants=SUBSCRIBER_PERMISSIONS)
    with TestClient(app) as client:
        def tool(name, **values):
            if transport == "rest":
                response = client.post("/v1/messages_" + name, json=values, headers=HEADERS)
                assert response.status_code == 200, response.text
                return response.json()
            result = rpc(client, "tools/call", {"name": "messages_" + name, "arguments": values}).json()["result"]
            assert not result.get("isError"), result
            return result["structuredContent"]
        sid = tool("create_subscription", chat_ids=["chat-synthetic"])["subscription_id"]
        assert tool("wait_for_subscription_events", subscription_id=sid, timeout=25)["wait_status"] == "events_available"
        page = tool("fetch_subscription_events", subscription_id=sid)
        assert [item["event_id"] for item in page["items"]] == ["event-1"] and page["receipt"]
        ack = tool("ack_subscription_events", subscription_id=sid, receipt=page["receipt"], version=page["ack_version"])
        assert tool("fetch_subscription_events", subscription_id=sid)["items"] == []
        assert tool("get_subscription", subscription_id=sid)["acked_through"] == ack["acked_through"] == 7


# spec: ANYM-518
def test_full_wait_pool_returns_busy_not_a_bare_503(tmp_path, monkeypatch):
    original = Clients.call

    async def full(self, module, operation, credential, values):
        if operation in {"wait_for_subscription_events", "fetch_subscription_events"}:
            raise Denied("backend_busy", 503)
        return await original(self, module, operation, credential, values)
    monkeypatch.setattr(Clients, "call", full)
    app, _, _ = configured(tmp_path, subscriber_backend(), module="messaging", tools=SUBSCRIBER, grants=SUBSCRIBER_PERMISSIONS)
    values = {"subscription_id": "subscription-1", "timeout": 25}
    expected = {"wait_status": "busy", "retry_after_s": 5, "wait_pool": {"active": 2, "waiting": 4}}
    with TestClient(app) as client:
        rest = client.post("/v1/messages_wait_for_subscription_events", json=values, headers=HEADERS)
        mcp = rpc(client, "tools/call", {"name": "messages_wait_for_subscription_events", "arguments": values}).json()["result"]
        # Other calls keep the bare busy error.
        fetch = client.post("/v1/messages_fetch_subscription_events", json={"subscription_id": "subscription-1"}, headers=HEADERS)
    assert rest.status_code == 200 and rest.json() == expected
    assert not mcp.get("isError") and mcp["structuredContent"] == expected
    assert fetch.status_code == 503


ENDPOINT = "11111111-1111-4111-8111-111111111111"
SECRET_CANARY = "whsec_" + "U3ludGhldGljQ2FuYXJ5U2VjcmV0T25seUZvclRlc3Rz"
HEADER_CANARY = "synthetic-header-value-canary"
ENDPOINT_PERMISSIONS = ["read", "delivery-write"]
ENDPOINT_TOOLS = [
    ("register_delivery_endpoint", {"request_id": "synthetic-endpoint", "url": "https://hooks.example/in",
                                    "signing_secret": SECRET_CANARY, "label": "synthetic",
                                    "headers": {"X-Api-Key": HEADER_CANARY}}),
    ("list_delivery_endpoints", {}),
    ("update_delivery_endpoint", {"endpoint_id": ENDPOINT, "version": 2, "action": "rotate_secret",
                                  "new_secret": SECRET_CANARY}),
    ("update_delivery_endpoint", {"endpoint_id": ENDPOINT, "version": 2, "action": "set_headers",
                                  "headers": {"X-Api-Key": HEADER_CANARY}}),
    ("update_delivery_endpoint", {"endpoint_id": ENDPOINT, "version": 2, "action": "pause"}),
    ("delete_delivery_endpoint", {"endpoint_id": ENDPOINT, "version": 3}),
    ("test_delivery_endpoint", {"endpoint_id": ENDPOINT, "request_id": "synthetic-probe"}),
    ("create_subscription", {"chat_ids": ["chat-synthetic"], "delivery": {"endpoint_id": ENDPOINT,
                                                                           "delivery_policy": {"debounce_s": 5}}}),
    ("configure_subscription_delivery", {"subscription_id": "subscription-1", "version": 1, "endpoint_id": ENDPOINT}),
]
VIEW = {"endpoint_id": ENDPOINT, "version": 1, "state": "enabled", "header_names": ["x-api-key"],
        "headers_fingerprint": "0a1b2c3d", "secret_fingerprint": "4e5f6a7b"}


def endpoint_app(tmp_path, operation, respond):
    seen = []

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ENDPOINT_PERMISSIONS + ["subscription-write"],
                                            "tool_manifest": {"names": [operation]}})
        seen.append((request.url.path, json.loads(request.content)))
        return respond(request)

    app, _, _ = configured(tmp_path, backend, module="messaging", tools=["messages_" + operation],
                           grants=ENDPOINT_PERMISSIONS + ["subscription-write"])
    return app, seen


def both(client, name, values):
    rest = client.post("/v1/" + name, json=values, headers=HEADERS)
    mcp = rpc(client, "tools/call", {"name": name, "arguments": values}).json()["result"]
    return rest, mcp


# spec: ANYM-519
@pytest.mark.parametrize("operation,values", ENDPOINT_TOOLS)
def test_endpoint_tools_forward_exact_arguments_and_never_echo_write_only_values(tmp_path, caplog, operation, values):
    caplog.set_level(logging.DEBUG)
    app, seen = endpoint_app(tmp_path, operation, lambda request: httpx.Response(200, json=VIEW))
    name = "messages_" + operation
    with TestClient(app) as client:
        assert [t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]] == [name]
        rest, mcp = both(client, name, values)
    assert rest.status_code == 200 and not mcp.get("isError")
    assert rest.json() == mcp["structuredContent"] == VIEW
    # The gateway adds schema defaults; the backend drops the None ones (app.drop_omitted).
    assert seen[0] == seen[1] and seen[0][0] == "/v1/" + operation
    assert {k: v for k, v in seen[0][1].items() if k in values} == values
    assert all(v is None or k in ('label', 'direction') for k, v in seen[0][1].items() if k not in values)
    for text in (rest.text, json.dumps(mcp), caplog.text):
        assert SECRET_CANARY not in text and HEADER_CANARY not in text


# spec: ANYM-519
@pytest.mark.parametrize("operation,values", ENDPOINT_TOOLS[:7])
def test_endpoint_tools_need_delivery_write_and_fail_closed_without_manifest(tmp_path, operation, values):
    name = "messages_" + operation
    assert CATALOG[name]["scopes"] == ["read", "delivery-write"]
    for permissions, manifest in ((["read"], [operation]), (ENDPOINT_PERMISSIONS, None)):
        def backend(request):
            assert request.url.path == "/v1/capabilities"
            return httpx.Response(200, json={"permissions": permissions,
                                            **({"tool_manifest": {"names": manifest}} if manifest else {})})
        app, _, _ = configured(tmp_path, backend, module="messaging", tools=[name], grants=permissions)
        with TestClient(app) as client:
            assert rpc(client, "tools/list").json()["result"]["tools"] == []
            rest, mcp = both(client, name, values)
        assert rest.status_code == 403 and mcp["isError"]


# spec: ANYM-519
@pytest.mark.parametrize("operation,values", [
    ("register_delivery_endpoint", {"request_id": "synthetic-endpoint", "url": "https://hooks.example", "signing_secret": "plain"}),
    ("register_delivery_endpoint", {"request_id": "synthetic-endpoint", "url": "https://hooks.example",
                                    "signing_secret": "whsec_" + "A" * 31}),
    ("register_delivery_endpoint", {"request_id": "synthetic-endpoint", "url": "https://hooks.example",
                                    "signing_secret": "whsec_" + "A" * 97}),
    ("register_delivery_endpoint", {"request_id": "synthetic-endpoint", "url": "", "signing_secret": SECRET_CANARY}),
    ("register_delivery_endpoint", {"request_id": "synthetic-endpoint", "url": "https://h.example/" + "x" * 2048,
                                    "signing_secret": SECRET_CANARY}),
    ("register_delivery_endpoint", {"request_id": "synthetic-endpoint", "url": "https://hooks.example",
                                    "signing_secret": SECRET_CANARY, "headers": {f"X-{i}": "v" for i in range(5)}}),
    ("register_delivery_endpoint", {"request_id": "synthetic-endpoint", "url": "https://hooks.example",
                                    "signing_secret": SECRET_CANARY, "headers": {"X-Api-Key": 1}}),
    ("register_delivery_endpoint", {"request_id": "synthetic-endpoint", "url": "https://hooks.example",
                                    "signing_secret": SECRET_CANARY, "label": "x" * 129}),
    ("register_delivery_endpoint", {"request_id": "synthetic-endpoint", "url": "https://hooks.example",
                                    "signing_secret": SECRET_CANARY, "receiver_id": ENDPOINT}),
    ("list_delivery_endpoints", {"limit": 5}),
    ("update_delivery_endpoint", {"endpoint_id": ENDPOINT, "version": 1, "action": "close"}),
    ("update_delivery_endpoint", {"endpoint_id": ENDPOINT, "version": 1, "action": "enable"}),
    ("update_delivery_endpoint", {"endpoint_id": "endpoint-1", "version": 1, "action": "pause"}),
    ("update_delivery_endpoint", {"endpoint_id": ENDPOINT, "version": 0, "action": "pause"}),
    ("delete_delivery_endpoint", {"endpoint_id": ENDPOINT}),
    ("test_delivery_endpoint", {"endpoint_id": ENDPOINT, "request_id": "short"}),
    ("create_subscription", {"chat_ids": ["c"], "delivery": {"endpoint_id": "e"}}),
    ("create_subscription", {"chat_ids": ["c"], "delivery": {"endpoint_id": ENDPOINT, "url": "https://hooks.example"}}),
    ("configure_subscription_delivery", {"subscription_id": "s", "version": 1, "endpoint_id": "e"}),
])
def test_endpoint_arguments_are_bounded_before_any_backend(operation, values):
    with pytest.raises(ValueError):
        arguments(CATALOG["messages_" + operation], values)


# spec: ANYM-519
def test_invalid_secret_and_missing_request_id_never_return_the_secret(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    app, seen = endpoint_app(tmp_path, "register_delivery_endpoint", lambda request: pytest.fail("reached backend"))
    name = "messages_register_delivery_endpoint"
    invalid = {"request_id": "synthetic-endpoint", "url": "https://hooks.example", "signing_secret": "whsec_!" + HEADER_CANARY}
    missing = {"url": "https://hooks.example", "signing_secret": SECRET_CANARY, "headers": {"X-Api-Key": HEADER_CANARY}}
    with TestClient(app) as client:
        for values in (invalid, missing):
            rest, mcp = both(client, name, values)
            assert rest.status_code == 422 and mcp["isError"] and rest.json()["error"] == "invalid_arguments"
            # The ANYM-544 retry hint would repeat every argument; a tool with write-only ones gets none.
            assert "next" not in rest.json()
            for text in (rest.text, json.dumps(mcp), caplog.text):
                assert SECRET_CANARY not in text and HEADER_CANARY not in text
    assert seen == []


URL_HINT = "https on port 443, a public address, a host the operator allows"
RELAY_NEXT = "for a tailnet or private host ask the operator for the wake relay (ANYM-228)"


# spec: ANYM-519
@pytest.mark.parametrize("operation,values,body,relayed", [
    ("register_delivery_endpoint", ENDPOINT_TOOLS[0][1],
     {"error": "unsafe_endpoint", "field_errors": [{"field": "url", "reason": "format", "expected": URL_HINT}],
      "next_step": RELAY_NEXT},
     {"field_errors": [{"field": "url", "reason": "format", "expected": URL_HINT}], "next_step": RELAY_NEXT}),
    ("register_delivery_endpoint", ENDPOINT_TOOLS[0][1],
     {"error": "rate_limited", "retry_after_s": 42}, {"retry_after_s": 42}),
    ("register_delivery_endpoint", ENDPOINT_TOOLS[0][1],
     {"error": "reserved_header", "header": "Content-Type"}, {"header": "Content-Type"}),
    ("update_delivery_endpoint", ENDPOINT_TOOLS[2][1],
     {"error": "rotation_in_progress", "retry_after_s": 3600}, {"retry_after_s": 3600}),
    ("update_delivery_endpoint", ENDPOINT_TOOLS[4][1],
     {"error": "operator_disabled", "state": "operator_disabled"}, {"state": "operator_disabled"}),
    ("configure_subscription_delivery", ENDPOINT_TOOLS[8][1],
     {"error": "endpoint_paused", "state": "agent_paused", "next_step": "call update_delivery_endpoint with action resume"},
     {"state": "agent_paused", "next_step": "call update_delivery_endpoint with action resume"}),
    ("list_delivery_endpoints", {}, {"error": "delivery_disabled"}, {}),
    ("register_delivery_endpoint", ENDPOINT_TOOLS[0][1], {"error": "delivery_policy_unset"}, {}),
    # Anything not a reviewed constant, counter or header name is dropped, including echoed input.
    ("register_delivery_endpoint", ENDPOINT_TOOLS[0][1],
     {"error": "invalid_header", "header": "X-Api-Key: " + HEADER_CANARY, "next_step": HEADER_CANARY,
      "state": [HEADER_CANARY], "retry_after_s": True, "detail": SECRET_CANARY,
      "field_errors": [{"field": "headers", "reason": "format", "expected": HEADER_CANARY}]}, {}),
    ("register_delivery_endpoint", ENDPOINT_TOOLS[0][1],
     {"error": "invalid_signing_secret", "retry_after_s": 10**9, "state": "secret", "next_step": {"tool": "x"},
      "secret": SECRET_CANARY}, {}),
])
def test_endpoint_errors_relay_only_reviewed_extras(tmp_path, caplog, operation, values, body, relayed):
    caplog.set_level(logging.DEBUG)
    status = 429 if body["error"] == "rate_limited" else 400 if "header" in body["error"] or "secret" in body["error"] \
        or body["error"] == "unsafe_endpoint" else 409
    app, _ = endpoint_app(tmp_path, operation, lambda request: httpx.Response(
        status, json={**body, "request_id": "c" * 32, "next_action": "PRIVATE BACKEND INSTRUCTION"}))
    with TestClient(app) as client:
        rest, mcp = both(client, "messages_" + operation, values)
    assert rest.status_code == status and mcp["isError"]
    assert rest.json() == mcp["structuredContent"] == {
        "error": body["error"], "backend_status": status, "request_id": "c" * 32, **relayed}
    for text in (rest.text, json.dumps(mcp), caplog.text):
        assert SECRET_CANARY not in text and HEADER_CANARY not in text


# spec: ANYM-520
@pytest.mark.parametrize("body,relayed", [
    ({"error": "endpoint_test_failed", "state": "auto_paused", "version": 8, "test_error": "receiver_status",
      "http_status": 500, "next_step": "fix the receiver, then call update_delivery_endpoint with action resume again"},
     {"state": "auto_paused", "version": 8, "test_error": "receiver_status", "http_status": 500,
      "next_step": "fix the receiver, then call update_delivery_endpoint with action resume again"}),
    ({"error": "endpoint_test_failed", "version": True, "test_error": "Receiver said " + SECRET_CANARY,
      "http_status": 99}, {}),
    ({"error": "endpoint_test_failed", "version": 0}, {}),
])
def test_a_refused_resume_relays_its_test_error_and_the_new_version_only(tmp_path, caplog, body, relayed):
    caplog.set_level(logging.DEBUG)
    app, _ = endpoint_app(tmp_path, "update_delivery_endpoint", lambda request: httpx.Response(
        409, json={**body, "request_id": "c" * 32}))
    with TestClient(app) as client:
        rest, mcp = both(client, "messages_update_delivery_endpoint", ENDPOINT_TOOLS[4][1])
    assert rest.json() == mcp["structuredContent"] == {
        "error": "endpoint_test_failed", "backend_status": 409, "request_id": "c" * 32, **relayed}
    for text in (rest.text, json.dumps(mcp), caplog.text):
        assert SECRET_CANARY not in text


# spec: ANYM-519
def test_register_and_test_wait_in_the_wait_pool_not_the_metadata_pool():
    from agent_gateway.http_clients import admission, wait_tools
    tools = wait_tools(CATALOG)
    for operation, values in ENDPOINT_TOOLS[:7]:
        expected = "waits" if operation in ("register_delivery_endpoint", "test_delivery_endpoint") else "metadata"
        assert admission(tools, "messaging", operation, values) == expected, operation


# spec: ANYM-519
@pytest.mark.parametrize("operation,values", [
    ("create_subscription", {"chat_ids": ["telegram-demo-chat"],
                             "delivery": {"endpoint_id": "00000000-0000-4000-8000-000000000001"}}),
    ("configure_subscription_delivery", {"subscription_id": "00000000-0000-4000-8000-000000000002", "version": 1,
                                         "endpoint_id": "00000000-0000-4000-8000-000000000001"})])
@pytest.mark.parametrize("local,upstream", [(["read", "subscription-write"], ENDPOINT_PERMISSIONS + ["subscription-write"]),
                                            (ENDPOINT_PERMISSIONS + ["subscription-write"], ["read", "subscription-write"])])
def test_binding_an_endpoint_needs_delivery_write_on_both_sides(tmp_path, operation, values, local, upstream):
    name = "messages_" + operation
    seen = []

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": upstream, "tool_manifest": {"names": [operation]}})
        seen.append(request.url.path)
        return httpx.Response(200, json={"subscription_id": values.get("subscription_id")})

    app, _, _ = configured(tmp_path, backend, module="messaging", tools=[name], grants=local)
    with TestClient(app) as client:
        rest, mcp = both(client, name, values)
        assert rest.status_code == 403 and mcp["isError"] and seen == []
        # Without an endpoint the same grants still reach the backend (operator receivers stay as before).
        plain = {k: v for k, v in values.items() if k not in ("delivery", "endpoint_id")}
        if operation == "configure_subscription_delivery":
            plain["receiver_id"] = "00000000-0000-4000-8000-000000000003"
        assert client.post("/v1/" + name, json=plain, headers=HEADERS).status_code == 200 and seen
