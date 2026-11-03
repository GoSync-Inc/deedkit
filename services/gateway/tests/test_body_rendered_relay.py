"""ANYM-737: the gateway relays body_format to the messaging read tools only when the caller gives it."""
import hashlib
import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG
from test_policy import config

HEADERS = {"Authorization": "Bearer gateway-a"}
TOOLS = ["get_messages", "search", "get_context"]
ARGUMENTS = {
    "get_messages": {"chat_id": "chat_" + "b" * 32},
    "search": {"query": "synthetic"},
    "get_context": {"message_id": "msg_" + "c" * 32},
}


def backend(request, seen, old):
    operation, args = request.url.path.removeprefix("/v1/"), json.loads(request.content)
    if request.url.path == "/v1/capabilities":
        return httpx.Response(200, json={"permissions": ["read"]})
    seen.append((operation, args))
    if old and "body_format" in args:
        # The deployed messaging has closed schemas: an unknown argument is refused, never ignored.
        return httpx.Response(400, json={
            "error": "invalid_parameters", "request_id": "c" * 32, "retryable": False,
            "field_errors": [{"field": "body_format", "reason": "unknown_field"}]})
    return httpx.Response(200, json={"items": [], "next_cursor": None})


def setup(tmp_path, old):
    value = config()
    value["deployment"] = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://backend.test"}}}
    value["profiles"]["reader"] = {"tools": ["messages_" + name for name in TOOLS]}
    agent = value["principals"][0]
    agent["grants"] = {"messaging": ["read"]}
    key = tmp_path / "backend.key"
    key.write_text("backend-synthetic")
    key.chmod(0o600)
    agent["credentials"] = {"messaging": {"key_file": str(key),
                                          "key_sha256": hashlib.sha256(b"backend-synthetic").hexdigest()}}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    seen = []
    app = create_app(path, "http://testserver",
                     transports={"messaging": httpx.MockTransport(lambda request: backend(request, seen, old))})
    return app, seen


# spec: ANYM-737
@pytest.mark.parametrize("tool", TOOLS)
def test_the_catalog_names_body_format_and_the_new_backend_receives_it_only_when_given(tmp_path, tool):
    entry = CATALOG["messages_" + tool]
    assert entry["inputSchema"]["properties"]["body_format"]["anyOf"][0]["enum"] == ["", "html"]
    assert "body_format=html" in entry["description"] and "omitted_types" in entry["description"]
    app, seen = setup(tmp_path, old=False)
    with TestClient(app) as client:
        for given in (None, "", "html"):
            arguments = dict(ARGUMENTS[tool]) if given is None else {**ARGUMENTS[tool], "body_format": given}
            response = client.post("/v1/messages_" + tool, json=arguments, headers=HEADERS)
            assert response.status_code == 200, response.text
            sent = seen[-1][1]
            assert ("body_format" in sent) == (given is not None)
            if given is not None:
                assert sent["body_format"] == given


# spec: ANYM-737
@pytest.mark.parametrize("tool", TOOLS)
def test_an_old_backend_refuses_the_argument_and_the_gateway_never_returns_plain_text(tmp_path, tool):
    app, seen = setup(tmp_path, old=True)
    with TestClient(app) as client:
        plain = client.post("/v1/messages_" + tool, json=ARGUMENTS[tool], headers=HEADERS)
        assert plain.status_code == 200, plain.text
        response = client.post("/v1/messages_" + tool, json={**ARGUMENTS[tool], "body_format": "html"}, headers=HEADERS)
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_parameters"
    assert body["field_errors"] == [{"field": "body_format", "reason": "unknown_field"}]
    assert "items" not in body
