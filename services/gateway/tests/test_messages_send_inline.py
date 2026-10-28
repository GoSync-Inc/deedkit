"""messages_send inline files through the gateway (ANYM-547): the raised body cap and the upload `next`."""
import asyncio
import base64
import hashlib
import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import NORMAL_BODY_LIMIT, SEND_BODY_LIMIT, create_app
from agent_gateway.catalog import CATALOG, arguments
from test_messages_send_relay import HEADERS, SEND, setup

CAP = 262144
MCP_HEADERS = {**HEADERS, "Accept": "application/json, text/event-stream"}


def inline(size, name="synthetic.bin"):
    data = (b"synthetic-inline" * (size // 16 + 1))[:size]
    return {"name": name, "mime": "application/octet-stream", "base64": base64.b64encode(data).decode()}, data


def gateway(tmp_path, answer):
    """The gateway of test_messages_send_relay, also granting the upload tools, over a backend that `answer`s."""
    setup(tmp_path, ["send_file"])
    path = tmp_path / "policy.json"
    value = json.loads(path.read_text())
    value["profiles"]["reader"]["tools"] += ["messages_prepare_file_upload", "messages_upload_file_chunk",
                                             "messages_preview_message"]
    path.write_text(json.dumps(value))
    seen = []

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read", "send", "upload", "download"],
                                             "messenger_operations": {"supported_by_account": {"synthetic": {
                                                 "provider": "whatsmeow", "operations": ["send_file"]}}},
                                             "tool_manifest": {"names": ["messages_send", "prepare_file_upload",
                                                                         "upload_file_chunk", "preview_message"]}})
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        return answer(body)
    return create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(backend)}), seen


def mcp(name, args):
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}


def mcp_detail(response):
    text = response.json()["result"]["content"][0]["text"]
    return json.loads(text[text.index("{"):])


def over_cap(body, filename=None):
    """The backend's answer one byte over the inline cap: field_errors and the prepare call for that file."""
    item = body["files"][-1]
    data = base64.b64decode(item["base64"])
    return httpx.Response(400, json={
        "error": "invalid_arguments", "request_id": "a" * 32, "retryable": False,
        "field_errors": [{"field": f"files.{len(body['files']) - 1}.base64", "reason": "max_bytes"}],
        "next": {"tool": "messages_prepare_file_upload", "arguments": {
            "account": "synthetic", "chat_id": SEND["to"], "filename": filename or item["name"], "mime": item["mime"],
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "request_id": "REPLACE_WITH_YOUR_ID"}},
        "next_action": "check_parameters"})


# spec: ANYM-547
@pytest.mark.parametrize("size", [65536, CAP])
def test_inline_files_up_to_the_cap_pass_rest_and_mcp_unchanged(tmp_path, size):
    app, seen = gateway(tmp_path, lambda body: httpx.Response(200, json={"state": "previewed"}))
    item, _ = inline(size)
    call = {"to": SEND["to"], "text": "Synthetic caption", "files": [item], "dry_run": True}
    with TestClient(app) as client:
        assert client.post("/v1/messages_send", json=call, headers=HEADERS).status_code == 200
        assert not client.post("/mcp", json=mcp("messages_send", call), headers=MCP_HEADERS).json()["result"]["isError"]
    assert [args["files"] for _, args in seen] == [[item], [item]]


# spec: ANYM-547
def test_one_byte_over_the_cap_relays_the_upload_call_for_the_callers_own_file(tmp_path):
    item, data = inline(CAP + 1)
    call = {"to": SEND["to"], "files": [item], "request_id": "req-inline-over-1"}
    app, _ = gateway(tmp_path, over_cap)
    with TestClient(app) as client:
        rest = client.post("/v1/messages_send", json=call, headers=HEADERS)
        rpc = client.post("/mcp", json=mcp("messages_send", call), headers=MCP_HEADERS)
    for detail in (rest.json(), mcp_detail(rpc)):
        assert detail["field_errors"] == [{"field": "files.0.base64", "reason": "max_bytes"}]
        args = detail["next"]["arguments"]
        assert detail["next"]["tool"] == "messages_prepare_file_upload" and args["bytes"] == CAP + 1
        assert args["sha256"] == hashlib.sha256(data).hexdigest() and args["filename"] == "synthetic.bin"
        # Valid for that tool's schema once the caller picks its own request_id.
        arguments(CATALOG["messages_prepare_file_upload"], {**args, "request_id": "req-inline-upload-1"})
        assert item["base64"][:64] not in json.dumps(detail)
    assert rest.status_code == 400


# spec: ANYM-547
def test_an_upload_next_naming_any_other_file_is_dropped(tmp_path):
    item, _ = inline(CAP + 1)
    app, _ = gateway(tmp_path, lambda body: over_cap(body, filename="other-synthetic.pdf"))
    with TestClient(app) as client:
        detail = client.post("/v1/messages_send", json={"to": SEND["to"], "files": [item],
                                                        "request_id": "req-inline-over-2"}, headers=HEADERS).json()
    assert detail["field_errors"] == [{"field": "files.0.base64", "reason": "max_bytes"}] and "next" not in detail


# spec: ANYM-547
def test_only_messages_send_gets_the_raised_cap(tmp_path):
    app, seen = gateway(tmp_path, lambda body: httpx.Response(200, json={"state": "previewed"}))
    text = "x" * (NORMAL_BODY_LIMIT + 1)
    with TestClient(app) as client:
        other = {"account": "synthetic", "chat_id": SEND["to"], "text": text}
        assert client.post("/v1/messages_preview_message", json=other, headers=HEADERS).status_code == 413
        assert client.post("/mcp", json=mcp("messages_preview_message", other), headers=MCP_HEADERS).status_code == 413
        huge = {"to": SEND["to"], "files": [{"name": "a.bin", "mime": "a/b", "base64": "A" * SEND_BODY_LIMIT}]}
        assert client.post("/v1/messages_send", json=huge, headers=HEADERS).status_code == 413
        assert client.post("/mcp", json=mcp("messages_send", huge), headers=MCP_HEADERS).status_code == 413
        # The raised cap follows authentication: an unknown key's large body is refused before it is read.
        large = {"to": SEND["to"], "text": text, "dry_run": True}
        assert client.post("/v1/messages_send", json=large,
                           headers={"Authorization": "Bearer unknown-key"}).status_code in (401, 403)
    assert seen == []


def received(app, body, step=4096):
    """The status and how many bytes of an MCP `body` the gateway read, sent in `step`-byte pieces."""
    count = 0

    async def pieces():
        nonlocal count
        for start in range(0, len(body), step):
            count += len(body[start:start + step])
            yield body[start:start + step]

    async def post():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post("/mcp", content=pieces(), headers={**MCP_HEADERS,
                                                                            "Content-Type": "application/json"})
            return response.status_code
    return asyncio.run(post()), count


# spec: ANYM-547
def test_more_than_64_kib_is_read_only_for_the_large_tool_the_call_claims_and_the_key_may_call(tmp_path):
    app, seen = gateway(tmp_path, lambda body: httpx.Response(200, json={"state": "previewed"}))
    item, _ = inline(200000)
    send = json.dumps(mcp("messages_send", {"to": SEND["to"], "files": [item], "dry_run": True})).encode()
    listing = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                          "params": {"_meta": {"pad": "x" * 200000}}}).encode()
    batch = json.dumps([json.loads(send)]).encode()
    # Nested metadata that looks like a large call claims nothing: only the top-level method and params.name count.
    nested = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {
        "method": "tools/call", "name": "messages_send", "pad": "x" * 200000}}}).encode()
    for body in (listing, batch, nested):
        status, count = received(app, body)
        assert status == 413 and count <= NORMAL_BODY_LIMIT + 4096
    # A key that may upload chunks but not call messages_send reads no more of a messages_send call.
    policy = tmp_path / "policy.json"
    value = json.loads(policy.read_text())
    value["profiles"]["reader"]["tools"].remove("messages_send")
    policy.write_text(json.dumps(value))
    chunk_only = create_app(policy, "http://testserver", transports={"messaging": httpx.MockTransport(
        lambda request: httpx.Response(200, json={"permissions": ["read", "send", "upload"],
                                                  "tool_manifest": {"names": ["upload_file_chunk"]}}))})
    disguised = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "_meta": {"name": "messages_upload_file_chunk"}, "name": "messages_send",
        "arguments": {"to": SEND["to"], "files": [item], "dry_run": True}}}).encode()
    for body in (send, disguised):
        status, count = received(chunk_only, body)
        assert status == 413 and count <= NORMAL_BODY_LIMIT + 4096
    assert seen == []
