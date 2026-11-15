"""Request-scoped compact capabilities over synthetic gateway backends."""
import json

from starlette.testclient import TestClient
from test_module_selection import call

pytest_plugins = ("test_module_selection",)


# spec: ANYM-999
def test_compact_module_overview_is_scoped_without_changing_unfiltered_discovery(gateway):
    app, _, _, _ = gateway
    with TestClient(app) as client:
        before = call(client, "platform_capabilities", path="/mcp/compact")["structuredContent"]
        result = call(client, "platform_capabilities", {"module": "messaging"}, path="/mcp/compact")["structuredContent"]
        after = call(client, "platform_capabilities", path="/mcp/compact")["structuredContent"]
    assert before == after
    assert {row["id"] for row in result["modules_available"]} == {"messaging"}
    assert result["selected_modules"] == ["messaging"]
    assert all(row["tool"].startswith("messages_") for row in result["tool_paths"])
    for field in ("backend_states", "backend_builds", "backend_tool_manifests"):
        assert set(result[field]) <= {"messaging"}
    assert all(row["module"] == "messaging" for row in result["unavailable"])
    assert result["capability"]["module"] == "gateway"
    assert result["discovery"] == before["discovery"]
    assert not app.state.dispatch.selections


# spec: ANYM-999
def test_other_module_and_noncompact_route_keep_their_contracts(gateway):
    app, _, _, _ = gateway
    with TestClient(app) as client:
        compact = call(client, "platform_capabilities", {"module": "calendar", "since": 0},
                       path="/mcp/compact")["structuredContent"]
        plain = call(client, "platform_capabilities", {"module": "calendar", "since": 0})["structuredContent"]
    assert {row["id"] for row in compact["modules_available"]} == {"calendar"}
    assert all(row["tool"].startswith("calendar_") for row in compact["tool_paths"])
    assert {row["id"] for row in plain["modules_available"]} == {"messaging", "calendar", "calls"}
    assert compact["recipes"] == plain["recipes"]
    assert compact["whats_new"] == plain["whats_new"]


# spec: ANYM-999
def test_compact_module_scope_respects_policy_and_unavailable_backend(gateway):
    app, _, write, _ = gateway
    write(lambda doc: doc["principals"][1]["credentials"].pop("messaging"))
    with TestClient(app) as client:
        restricted = call(client, "platform_capabilities", {"module": "messaging"}, key="b", path="/mcp/compact")["structuredContent"]
        write.down.add("messaging")
        unavailable = call(client, "platform_capabilities", {"module": "messaging"}, path="/mcp/compact")["structuredContent"]
    assert restricted["modules_available"] == [] and restricted["tool_paths"] == []
    assert "messages_" not in json.dumps(restricted)
    assert unavailable["modules_available"] == [] and unavailable["tool_paths"] == []
    assert [row["module"] for row in unavailable["unavailable"]] == ["messaging"]
