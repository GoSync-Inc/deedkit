"""The MCP text block mirrors structuredContent as one line of compact JSON."""
from datetime import datetime, timedelta, timezone
import hashlib
import json

import httpx
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from test_policy import config
from test_transport import rpc

CHATS = {"items": [{"id": "chat-1", "title": "Синтетический чат", "type": "group",
    "conversation": {"suggested_action": "review_incoming", "basis": "explicit_owner_attention"}}],
    "next_cursor": None, "detail": "compact"}
SEARCH = {"items": [{"message_id": "synthetic-1", "text": "Синтетический текст"}], "coverage": "unknown"}


def gateway(tmp_path, expires_at):
    value = config()
    value["deployment"] = {"modules": ["messaging"], "backends": {"messaging": {"url": "http://backend.test"}}}
    value["profiles"]["reader"]["tools"] = ["platform_capabilities", "messages_search", "messages_list_chats"]
    key = tmp_path / "backend.key"
    key.write_text("synthetic-backend-key")
    key.chmod(0o600)
    principal = value["principals"][0]
    principal["expires_at"] = expires_at
    principal["grants"] = {"messaging": ["read"]}
    principal["credentials"] = {"messaging": {
        "key_file": str(key), "key_sha256": hashlib.sha256(key.read_bytes()).hexdigest(),
    }}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"permissions": ["read"], "principal": "backend-a"})
        return httpx.Response(200, json=CHATS if request.url.path == "/v1/list_chats" else SEARCH)

    return create_app(path, "http://testserver", transports={"messaging": httpx.MockTransport(backend)})


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# spec: ANYM-408
def test_tool_text_block_is_compact_json(tmp_path):
    soon = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0)
    for expires_at, notice in (("2099-01-01T00:00:00Z", False), (soon.isoformat().replace("+00:00", "Z"), True)):
        with TestClient(gateway(tmp_path, expires_at)) as client:
            results = [rpc(client, "tools/call", {"name": name, "arguments": arguments}).json()["result"]
                for name, arguments in (("messages_list_chats", {}), ("messages_search", {"query": "synthetic"}),
                    ("platform_capabilities", {}), ("messages_search", {"limit": "x"}))]
        assert [r["isError"] for r in results] == [False, False, False, True]
        assert results[0]["structuredContent"] == CHATS and results[1]["structuredContent"] == SEARCH
        for result in results:
            text = result["content"][0]["text"]
            assert text == compact(result["structuredContent"]) and json.loads(text) == result["structuredContent"]
            assert "\n" not in text and ": " not in text and ", " not in text
        # The expiry notice stays a separate text block after the JSON; an error carries none.
        assert [len(r["content"]) for r in results] == ([2, 2, 2, 1] if notice else [1, 1, 1, 1])
        if notice:
            assert all(r["content"][1]["text"].startswith("DeedKit gateway key expires at") for r in results[:3])
