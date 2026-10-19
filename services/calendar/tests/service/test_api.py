import json
from pathlib import Path

from starlette.testclient import TestClient
from tests.service.test_policy import SCOPES


def client(household, visibility="details"):
    from family_calendar.service.api import create_app

    household.grant("family", "adult-a", "cal-a", SCOPES, "details")
    key = household.agent("agent", "family", "adult-a", SCOPES, ["cal-a"], visibility)
    return TestClient(create_app(household, port=13232), base_url="http://127.0.0.1:13232"), key


def test_rest_and_mcp_return_identical_calendar_policy(household):
    c, key = client(household, "busy_only")
    with c:
        headers = {"Authorization": "Bearer " + key}
        rest = c.post("/v1/calendars", json={}, headers=headers).json()
        r = c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "family_calendars", "arguments": {}},
            },
            headers={
                **headers,
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            },
        )
        assert r.status_code == 200
        assert r.json()["result"]["structuredContent"] == rest
        assert rest["calendars"][0]["visibility"] == "busy_only"


def test_agent_cannot_change_roles_or_create_credentials(household):
    c, key = client(household)
    with c:
        for route in ("/v1/admin", "/v1/agent-key", "/v1/connect"):
            assert c.post(route, json={}, headers={"Authorization": "Bearer " + key}).status_code == 404


def test_missing_auth_and_foreign_origin_are_rejected(household):
    c, key = client(household)
    with c:
        assert c.post("/v1/calendars", json={}).status_code == 401
        assert (
            c.post(
                "/v1/calendars",
                json={},
                headers={"Authorization": "Bearer " + key, "Origin": "https://evil.example"},
            ).status_code
            == 403
        )


def test_unknown_arguments_are_rejected_in_both_transports(household):
    c, key = client(household)
    with c:
        h = {"Authorization": "Bearer " + key}
        assert c.post("/v1/calendars", json={"extra": "secret"}, headers=h).status_code == 422
        r = c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "family_calendars", "arguments": {"extra": "secret"}},
            },
            headers={
                **h,
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            },
        )
        assert r.json()["result"]["isError"] is True


def test_notification_stream_is_disabled(household):
    c, key = client(household)
    with c:
        assert c.get("/mcp", headers={"Authorization": "Bearer " + key}).status_code == 405


def test_initialize_and_stateless_reconnect_preserve_catalog(household):
    c, key = client(household)
    with c:
        headers = {"Authorization": "Bearer " + key, "Accept": "application/json, text/event-stream"}
        for _ in range(2):
            result = c.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "synthetic", "version": "1"},
                    },
                },
            )
            assert result.status_code == 200
            assert result.json()["result"]["serverInfo"]["name"] == "family-calendar-service"
            assert "mcp-session-id" not in result.headers
            listed = c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert len(listed.json()["result"]["tools"]) == 10


def test_explicit_internal_host_does_not_trust_forwarded_identity(household):
    from family_calendar.service.api import create_app

    household.grant("family", "adult-a", "cal-a", SCOPES, "details")
    key = household.agent("agent", "family", "adult-a", SCOPES, ["cal-a"])
    with TestClient(
        create_app(household, allowed_hosts=["calendar-api:13232"]), base_url="http://calendar-api:13232"
    ) as c:
        assert c.post("/v1/calendars", json={}, headers={"Authorization": "Bearer " + key}).status_code == 200
        assert c.post("/v1/calendars", json={}, headers={"X-Agent-Id": "agent"}).status_code == 401
        assert c.get("/health", headers={"Host": "unconfigured:13232"}).status_code == 403


def test_capability_scope_discovery_matches_authenticated_principal(household):
    c, key = client(household)
    with c:
        result = c.post("/v1/capabilities", json={}, headers={"Authorization": "Bearer " + key}).json()
        assert set(result["permissions"]) == set(SCOPES)


def test_tool_annotations_separate_reads_local_plans_and_provider_writes(household):
    from family_calendar.service.api import ANNOTATIONS

    c, key = client(household)
    with c:
        listed = c.post(
            "/mcp",
            headers={"Authorization": "Bearer " + key, "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ).json()["result"]["tools"]
    hints = {tool["name"].removeprefix("family_"): tool["annotations"] for tool in listed}
    assert hints == ANNOTATIONS
    reads = {"capabilities", "calendars", "members", "agenda", "slots", "operation", "assignments"}
    assert {name for name, value in hints.items() if value["readOnlyHint"]} == reads
    assert hints["plan"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
    assert hints["apply"]["destructiveHint"] and hints["apply"]["openWorldHint"]


def test_gateway_catalog_mirrors_calendar_tool_metadata():
    from family_calendar.service.api import ANNOTATIONS, DESCRIPTIONS, MODELS

    packaged = Path(__file__).resolve().parents[3] / "gateway/src/agent_gateway/catalog.json"
    rows = {row["name"]: row for row in json.loads(packaged.read_text()) if row["module"] == "calendar"}
    assert set(rows) == {"calendar_" + name for name in MODELS}
    for name, model in MODELS.items():
        row = rows["calendar_" + name]
        assert row["operation"] == name and row["inputSchema"] == model.model_json_schema()
        assert row["description"] == DESCRIPTIONS[name]
        assert row["annotations"] == ANNOTATIONS[name]


def test_time_errors_name_the_accepted_format_in_both_transports(household):
    from family_calendar.service.core import TIME_FORMAT

    c, key = client(household)
    arguments = {"calendar_refs": ["cal-a"], "start": "2026-10-20T10:00:00", "end": "2026-10-20T11:00:00"}
    with c:
        headers = {"Authorization": "Bearer " + key}
        rest = c.post("/v1/agenda", json=arguments, headers=headers)
        mcp = c.post(
            "/mcp",
            headers={**headers, "Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "family_agenda", "arguments": arguments},
            },
        ).json()["result"]
    assert rest.status_code == 409
    assert rest.json() == mcp["structuredContent"] == {"error": "invalid_time", "hint": TIME_FORMAT}
    assert mcp["isError"] is True


# spec: ANYM-234
def test_plan_error_names_the_missing_field_in_both_transports(household):
    household.write_opt_in("cal-a", True)
    c, key = client(household)
    arguments = {"actions": [{"action": "create", "calendar_ref": "cal-a", "event": {"title": "x", "start": "2026-10-20T10:00:00Z"}}]}
    expected = {
        "error": "invalid_event",
        "field_errors": [
            {"field": "actions/0/event/end", "reason": "required"},
            {"field": "actions/0/event/timezone", "reason": "required"},
        ],
    }
    with c:
        headers = {"Authorization": "Bearer " + key}
        rest = c.post("/v1/plan", json=arguments, headers=headers)
        mcp = c.post(
            "/mcp",
            headers={**headers, "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "family_plan", "arguments": arguments}},
        ).json()["result"]
    assert rest.json() == mcp["structuredContent"] == expected and mcp["isError"] is True
    assert rest.status_code == 409
