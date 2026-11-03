"""ANYM-736: the send_message chat-head guard through the gateway: its read authority, defaults and refusals."""
import hashlib
import json

import httpx
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from test_policy import config

HEADERS = {"Authorization": "Bearer gateway-a"}
CHAT = "chat_" + "b" * 32
SEND = {"account": "synthetic", "chat_id": CHAT, "text": "synthetic text", "request_id": "req-send-guard-0001"}
GUARDED = {**SEND, "expected_last_message_id": "msg_" + "c" * 32}
TOOLS = ["messages_send_message", "messages_get_context", "messages_get_send_status"]
REQUEST_ID = "d" * 32


def setup(tmp_path, grants, backend_permissions=("read", "send"), tools=TOOLS, answer=None, operations=()):
    """A gateway whose principal holds `grants` over a backend credential that holds `backend_permissions`."""
    value = config()
    value["deployment"] = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://backend.test"}}}
    value["profiles"]["reader"] = {"tools": list(tools), "actions": {}}
    agent = value["principals"][0]
    agent["grants"] = {"messaging": list(grants)}
    agent["actions"] = {}
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
            return httpx.Response(200, json={"permissions": list(backend_permissions), "messenger_operations": {
                "supported_by_account": {"synthetic": {"provider": "whatsmeow", "operations": list(operations)}}}})
        arguments = json.loads(request.content)
        seen.append((request.url.path, arguments))
        if answer:
            return answer(request.url.path, arguments)
        return httpx.Response(200, json={"status": "queued", "send_id": "send_" + "e" * 32})

    return create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(backend)}), seen


def post(app, tool, body):
    return run(app, (tool, body))[0]


def run(app, *calls):
    """The responses to `calls`, one client session (an app starts once)."""
    with TestClient(app) as client:
        return [client.post("/v1/" + tool, json=body, headers=HEADERS) for tool, body in calls]


# spec: ANYM-736
def test_send_only_gateway_over_a_read_and_send_credential_is_refused_with_zero_backend_requests(tmp_path):
    app, seen = setup(tmp_path, grants=("send",))
    *refused, plain = run(app, ("messages_send_message", GUARDED),
                          ("messages_send_message", {**SEND, "guard": "outgoing_only"}),
                          ("messages_send_message", SEND))
    for result in refused:
        assert result.status_code == 403
        assert result.json() == {"error": "send_guard_not_permitted", "next_action": "check_send_status_by_request_id"}
    # The same gateway still sends without the guard: that one call is the only backend request.
    assert plain.status_code == 200 and [(path, "expected_last_message_id" in a) for path, a in seen] == [
        ("/v1/send_message", False)]


# spec: ANYM-736
def test_a_gateway_without_the_context_tool_is_refused_even_with_read_and_send(tmp_path):
    app, seen = setup(tmp_path, grants=("read", "send"), tools=["messages_send_message", "messages_get_send_status"])
    assert post(app, "messages_send_message", GUARDED).status_code == 403 and seen == []


# spec: ANYM-736
def test_a_principal_with_both_grants_is_forwarded_and_no_default_is_inserted(tmp_path):
    app, seen = setup(tmp_path, grants=("read", "send"))
    results = run(app, ("messages_send_message", GUARDED),
                  ("messages_send_message", {**GUARDED, "guard": "outgoing_only"}), ("messages_send_message", SEND))
    assert [r.status_code for r in results] == [200, 200, 200]
    forwarded = [arguments for _, arguments in seen]
    assert forwarded[0]["expected_last_message_id"] == GUARDED["expected_last_message_id"] and "guard" not in forwarded[0]
    assert forwarded[1]["guard"] == "outgoing_only"
    # Omitted, neither field reaches the backend: no schema default is inserted for them.
    assert "expected_last_message_id" not in forwarded[2] and "guard" not in forwarded[2]


# spec: ANYM-736
def test_after_read_access_is_removed_the_guarded_retry_is_refused_and_the_status_call_recovers(tmp_path):
    first, seen = setup(tmp_path, grants=("read", "send"))
    sent = post(first, "messages_send_message", GUARDED)
    assert sent.status_code == 200 and len(seen) == 1

    def saved(path, arguments):
        return httpx.Response(200, json={"status": "queued", "send_id": arguments["send_id"], "request_id_state": "saved"})
    later_dir = tmp_path / "after"
    later_dir.mkdir()
    after, later = setup(later_dir, grants=("send",), answer=saved)
    retried, status = run(after, ("messages_send_message", GUARDED),
                          ("messages_get_send_status", {"send_id": sent.json()["send_id"]}))
    assert retried.status_code == 403 and retried.json()["next_action"] == "check_send_status_by_request_id"
    assert status.status_code == 200 and status.json()["send_id"] == sent.json()["send_id"]
    assert later == [("/v1/get_send_status", {"send_id": sent.json()["send_id"]})]


def closed_schema(path, arguments):
    """The deployed messaging before this change: a closed schema without the guard fields."""
    unknown = sorted(set(arguments) - {"account", "chat_id", "text", "request_id", "reply_to", "format",
                                       "preview_digest", "link_preview", "allow_lossy", "wait_s", "phone", "username"})
    if unknown:
        return httpx.Response(400, json={"error": "invalid_parameters", "request_id": REQUEST_ID, "retryable": False,
            "field_errors": [{"field": name, "reason": "unknown_field"} for name in unknown],
            "next_action": "check_parameters"})
    return httpx.Response(200, json={"status": "queued", "send_id": "send_" + "e" * 32})


# spec: ANYM-736
def test_against_a_deployed_closed_schema_backend_a_plain_send_works_and_a_guarded_send_fails_closed(tmp_path):
    app, seen = setup(tmp_path, grants=("read", "send"), answer=closed_schema)
    plain, refused = run(app, ("messages_send_message", SEND),
                         ("messages_send_message", {**GUARDED, "guard": "outgoing_only"}))
    assert plain.status_code == 200
    assert "guard" not in seen[0][1] and "expected_last_message_id" not in seen[0][1]
    assert refused.status_code == 400
    body = refused.json()
    assert body["error"] == "invalid_parameters"
    assert body["field_errors"] == [{"field": "expected_last_message_id", "reason": "unknown_field"},
                                    {"field": "guard", "reason": "unknown_field"}]


# spec: ANYM-736
def test_a_changed_chat_refusal_is_relayed_with_the_gateways_own_next_call(tmp_path):
    def changed(path, arguments):
        return httpx.Response(409, json={
            "error": "chat_changed_since_seen", "request_id": REQUEST_ID, "retryable": False, "newer_count": 2,
            "newer_count_capped": False, "newer_outgoing_count": 1, "first_newer_message_id": "msg_" + "1" * 32,
            "newest_message_id": "msg_" + "2" * 32, "next_action": "read_newer_messages_then_decide",
            "next_call": {"tool": "get_context", "arguments": {"message_id": "msg_" + "1" * 32, "before_count": 0,
                                                                  "after_count": 20}, "caller_supplies": []}})
    app, _ = setup(tmp_path, grants=("read", "send"), answer=changed)
    result = post(app, "messages_send_message", GUARDED)
    body = result.json()
    assert result.status_code == 409 and body["error"] == "chat_changed_since_seen"
    assert (body["newer_count"], body["newer_outgoing_count"], body["newest_message_id"]) == (2, 1, "msg_" + "2" * 32)
    assert body["next_call"]["tool"] == "messages_get_context"
    assert body["next_call"]["arguments"]["after_count"] == 20


# spec: ANYM-736
def test_a_guard_read_failure_is_a_rejection_never_an_unknown_outcome(tmp_path):
    def unavailable(path, arguments):
        return httpx.Response(503, json={"error": "chat_guard_unavailable", "request_id": REQUEST_ID, "retryable": True})
    app, seen = setup(tmp_path, grants=("read", "send"), answer=unavailable)
    result = post(app, "messages_send_message", GUARDED)
    body = result.json()
    assert result.status_code == 503 and body["error"] == "chat_guard_unavailable" and len(seen) == 1
    assert body["outcome"] == "rejected" and body["next_action"] == "retry_after_delay"
    # Any other failure of a send keeps the unknown outcome.
    other, _ = setup(tmp_path, grants=("read", "send"), answer=lambda p, a: httpx.Response(503, json={"error": "service_busy"}))
    assert post(other, "messages_send_message", GUARDED).json()["outcome"] == "unknown"
