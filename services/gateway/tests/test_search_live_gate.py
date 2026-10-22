"""ANYM-478: search's live part needs the search_messages_live action grant, not only the read scope."""
import hashlib
import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG
from test_policy import config

HEADERS = {"Authorization": "Bearer gateway-a"}
THIN = {"items": [], "next_cursor": None,
        "result_evidence": {"empty_reason": "history_loading",
                            "suggested_call": {"tool": "messages_search", "args": {"live": "on"}}}}


def setup(tmp_path, selected, permissions):
    value = config()
    value["deployment"] = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://backend.test"}}}
    value["profiles"]["reader"] = {"tools": ["messages_search"], "actions": {"messenger_operations": selected}}
    agent = value["principals"][0]
    agent["grants"] = {"messaging": permissions}
    agent["actions"] = {"messenger_operations": selected}
    backend_key = tmp_path / "backend.key"
    backend_key.write_text("backend-synthetic")
    backend_key.chmod(0o600)
    agent["credentials"] = {"messaging": {"key_file": str(backend_key),
                                          "key_sha256": hashlib.sha256(b"backend-synthetic").hexdigest()}}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    seen = []

    def handler(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": permissions, "messenger_operations": {
                "supported_by_account": {"synthetic": {"operations": ["search_messages_live"]}}}})
        args = json.loads(request.content)
        seen.append((request.url.path, args))
        body = json.loads(json.dumps(THIN))
        if args.get("live", "off") != "off":
            body["result_evidence"].pop("suggested_call")
            body["result_evidence"]["live"] = [{"account": "synthetic", "state": "done", "stored_new": 0, "hits": []}]
        return httpx.Response(200, json=body)

    return create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(handler)}), seen


# spec: ANYM-478
def test_the_catalog_schema_has_live_wait_and_request_id():
    properties = CATALOG["messages_search"]["inputSchema"]["properties"]
    assert properties["live"]["enum"] == ["off", "auto", "on"] and properties["live"]["default"] == "off"
    assert properties["wait_s"]["anyOf"][0]["maximum"] == 20
    assert properties["request_id"]["type"] == "string"


# spec: ANYM-478
def test_backend_permission_without_the_action_gives_not_permitted_and_the_archive_answer(tmp_path):
    app, seen = setup(tmp_path, ["rename_group"], ["read", "read-live", "groups-manage"])
    with TestClient(app) as client:
        result = client.post("/v1/messages_search", json={"query": "needle", "live": "on"}, headers=HEADERS)
    assert result.status_code == 200
    assert seen[-1][1]["live"] == "off"
    evidence = result.json()["result_evidence"]
    assert evidence["live"] == [{"state": "not_permitted", "reason": "gateway_action"}]
    assert "suggested_call" not in evidence and evidence["empty_reason"] == "history_loading"


# spec: ANYM-478
@pytest.mark.parametrize("live", ["on", "auto"])
def test_the_granted_action_passes_live_to_the_backend(tmp_path, live):
    app, seen = setup(tmp_path, ["search_messages_live"], ["read", "read-live"])
    with TestClient(app) as client:
        result = client.post("/v1/messages_search", json={"query": "needle", "live": live, "wait_s": 5,
                                                          "request_id": "synthetic-search-1"}, headers=HEADERS)
    assert result.status_code == 200
    assert seen[-1][1]["live"] == live and seen[-1][1]["request_id"] == "synthetic-search-1"
    assert result.json()["result_evidence"]["live"][0]["state"] == "done"


# spec: ANYM-478
def test_the_live_hint_is_kept_only_with_the_action(tmp_path):
    for selected, kept in ((["search_messages_live"], True), (["rename_group"], False)):
        app, _ = setup(tmp_path, selected, ["read", "read-live", "groups-manage"])
        with TestClient(app) as client:
            evidence = client.post("/v1/messages_search", json={"query": "needle"}, headers=HEADERS).json()["result_evidence"]
        assert ("suggested_call" in evidence) is kept and "live" not in evidence
