"""messages_capabilities over the gateway: a summary by default over MCP, named sections, full over REST (ANYM-270)."""
import json

from starlette.testclient import TestClient

from test_capability_actions import HEADERS, capabilities
from test_transport import rpc

SUPPORTED = {"synthetic": {"provider": "whatsmeow", "operations": ["rename_group"], "available_operations": ["rename_group"]}}


def call(client, arguments):
    body = rpc(client, "tools/call", {"name": "messages_capabilities", "arguments": arguments}).json()
    result = body["result"]
    return (json.loads(result["content"][0]["text"]) if result.get("isError") else result["structuredContent"]), bool(result.get("isError"))


# spec: ANYM-270
def test_gateway_capabilities_summary_sections_and_full(tmp_path):
    app, seen = capabilities(tmp_path, SUPPORTED)
    with TestClient(app) as client:
        full = client.post("/v1/messages_capabilities", json={}, headers=HEADERS).json()
        assert full["gateway_actions"] == ["rename_group"] and "gateway_tools" in full
        assert call(client, {"detail": "full"}) == (full, False)
        summary, error = call(client, {})
        assert not error and summary["detail"] == "summary" and len(json.dumps(summary)) <= 1500
        assert "messenger_operations" in summary["sections"] and "gateway_actions" not in summary
        assert summary["gateway_principal"] == full["gateway_principal"]
        assert summary["gateway_tool_count"] == len(full["gateway_tools"])
        blocks, error = call(client, {"sections": ["messenger_operations"]})
        assert not error and blocks == {k: full[k] for k in full if k.startswith("gateway_actions")}
        rest = client.post("/v1/messages_capabilities", json={"sections": ["tool_manifest"]}, headers=HEADERS).json()
        assert rest == {k: full[k] for k in ("gateway_tools", "backend_tool_manifest") if k in full}
        # Every key the gateway returns today sits in exactly one section.
        names = summary["sections"]
        assert sum(len(call(client, {"sections": [n]})[0]) for n in names) == len(call(client, {"sections": names})[0])
        bad, error = call(client, {"sections": ["unknown"]})
        assert error and "invalid_filter" in json.dumps(bad)
        response = client.post("/v1/messages_capabilities", json={"sections": ["unknown"]}, headers=HEADERS)
        assert response.status_code == 400 and response.json()["error"] == "invalid_filter"
        # The backend is still asked for its full payload with no arguments.
        assert seen == []
