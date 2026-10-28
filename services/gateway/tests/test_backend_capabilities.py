import json
from pathlib import Path

import httpx
from starlette.testclient import TestClient

import agent_gateway
from agent_gateway.app import create_app
from test_policy import config
from test_transport import rpc


def test_backend_scope_revocation_removes_tools_and_blocks_rest_and_cached_mcp(tmp_path):
    key = tmp_path / "backend.key"
    key.write_text("backend-calendar-a")
    key.chmod(0o600)
    value = config()
    value["principals"][0]["credentials"]["calendar"]["key_file"] = str(key)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    permissions = ["agenda:read", "availability:read"]
    dispatched = []

    def backend(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(
                200, json={"permissions": permissions, "tool_manifest": {"names": ["unreviewed_admin_tool"]}}
            )
        dispatched.append(request.url.path)
        return httpx.Response(200, json={"calendars": []})

    app = create_app(path, "http://testserver", transports={"calendar": httpx.MockTransport(backend)})
    with TestClient(app) as c:
        assert {t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"]} == set(
            value["profiles"]["reader"]["tools"]
        )
        permissions.clear()
        assert [t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"]] == [
            "platform_capabilities",
            "calendar_calendars",
        ]
        arguments = {
            "member_ids": ["synthetic-child"],
            "start": "2026-10-19T08:00:00Z",
            "end": "2026-10-19T09:00:00Z",
            "duration_minutes": 30,
            "refresh": False,
        }
        assert (
            c.post(
                "/v1/calendar_slots", json=arguments, headers={"Authorization": "Bearer gateway-a"}
            ).status_code
            == 403
        )
        assert rpc(c, "tools/call", {"name": "calendar_slots", "arguments": arguments}).json()["result"][
            "isError"
        ]
    assert not dispatched


def test_unavailable_backend_does_not_prevent_platform_introspection(tmp_path):
    key = tmp_path / "backend.key"
    key.write_text("backend-calendar-a")
    key.chmod(0o600)
    value = config()
    value["principals"][0]["credentials"]["calendar"]["key_file"] = str(key)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)

    def unavailable(request):
        raise httpx.ConnectError("private backend detail")

    app = create_app(path, "http://testserver", transports={"calendar": httpx.MockTransport(unavailable)})
    with TestClient(app) as c:
        result = c.post(
            "/v1/platform_capabilities", json={}, headers={"Authorization": "Bearer gateway-a"}
        ).json()
        assert result["tools"] == ["platform_capabilities"]
        assert result["backend_states"] == {"calendar": "unavailable"}
        assert "private backend" not in json.dumps(result)


def test_calendar_constraints_survive_without_advertising_hidden_writes(tmp_path):
    key = tmp_path / "backend.key"
    key.write_text("backend-calendar-a")
    key.chmod(0o600)
    value = config()
    value["profiles"]["reader"]["tools"].append("calendar_capabilities")
    value["principals"][0]["credentials"]["calendar"]["key_file"] = str(key)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)

    def backend(request):
        return httpx.Response(
            200,
            json={
                "permissions": ["agenda:read", "availability:read", "event:create", "plan:create"],
                "features": {"event_create": True, "unreviewed_admin": True},
                "constraints": {
                    "recurrence_write": False,
                    "window_limit_days": 31,
                    "time_format": "ISO 8601 synthetic",
                    "secret_option": "hidden",
                },
                "calendars": [
                    {
                        "calendar_ref": "cal-a",
                        "write_enabled": True,
                        "allowed_event_actions": ["create", "update", "delete"],
                    }
                ],
            },
        )

    app = create_app(path, "http://testserver", transports={"calendar": httpx.MockTransport(backend)})
    with TestClient(app) as c:
        for tool in ("calendar_capabilities", "calendar_calendars"):
            result = c.post("/v1/" + tool, json={}, headers={"Authorization": "Bearer gateway-a"}).json()
            assert result["calendars"][0]["allowed_event_actions"] == []
        assert result["calendars"][0]["write_enabled"] is True
        result = rpc(c, "tools/call", {"name": "calendar_capabilities", "arguments": {}}).json()["result"][
            "structuredContent"
        ]
        assert result["constraints"] == {
            "recurrence_write": False,
            "window_limit_days": 31,
            "time_format": "ISO 8601 synthetic",
        }
        assert "calendar_plan" not in result["gateway_tools"]
        assert "features" not in result and "secret_option" not in str(result)


# spec: ANYM-234
def test_calendar_write_scope_constraints_pass_and_series_writes_stay_listed_as_off(tmp_path):
    key = tmp_path / "backend.key"
    key.write_text("backend-calendar-a")
    key.chmod(0o600)
    value = config()
    value["profiles"]["reader"]["tools"].append("calendar_capabilities")
    value["principals"][0]["credentials"]["calendar"]["key_file"] = str(key)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    constraints = {
        "event_write": "guest-free single event, all-day event or one occurrence",
        "occurrence_write": True,
        "all_day_write": True,
        "provider_write": {"google": {"occurrence_write": True, "all_day_write": True}},
        "recurrence_write": False,
        "future_split": False,
        "invitations": False,
        "series_write": True,
    }

    def backend(request):
        return httpx.Response(200, json={"permissions": ["agenda:read"], "constraints": constraints, "calendars": []})

    app = create_app(path, "http://testserver", transports={"calendar": httpx.MockTransport(backend)})
    with TestClient(app) as c:
        result = rpc(c, "tools/call", {"name": "calendar_capabilities", "arguments": {}}).json()["result"][
            "structuredContent"
        ]
    # ANYM-615: series_write (calendar_schedule's series scopes) now passes; the plan flags stay off.
    assert result["constraints"] == constraints


# Series scopes of calendar_schedule as the calendar service reports them (family_calendar/service/core.py).
SERIES_WRITE = {
    "google": {"scopes": ["this", "following", "all"], "tool": "calendar_schedule"},
    "icloud": {"scopes": ["this"], "error": "capability_unsupported"},
}


def calendar_app(tmp_path, constraints):
    key = tmp_path / "backend.key"
    key.write_text("backend-calendar-a")
    key.chmod(0o600)
    value = config()
    value["profiles"]["reader"]["tools"].append("calendar_capabilities")
    value["principals"][0]["credentials"]["calendar"]["key_file"] = str(key)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)

    def backend(request):
        return httpx.Response(200, json={"permissions": ["agenda:read"], "constraints": constraints, "calendars": []})

    return create_app(path, "http://testserver", transports={"calendar": httpx.MockTransport(backend)})


def calendar_constraints(c):
    """The constraints of calendar_calendars and calendar_capabilities over REST and MCP."""
    found = []
    for tool in ("calendar_capabilities", "calendar_calendars"):
        found.append(c.post("/v1/" + tool, json={}, headers={"Authorization": "Bearer gateway-a"}).json()["constraints"])
        found.append(rpc(c, "tools/call", {"name": tool, "arguments": {}}).json()["result"]["structuredContent"]["constraints"])
    return found


# spec: ANYM-615
def test_calendar_series_write_scopes_arrive_as_the_calendar_service_sent_them(tmp_path):
    constraints = {"recurrence_write": False, "future_split": False, "series_write": SERIES_WRITE}
    with TestClient(calendar_app(tmp_path, constraints)) as c:
        found = calendar_constraints(c)
    assert found == [constraints] * 4
    assert found[0]["series_write"]["google"]["scopes"] == ["this", "following", "all"]
    assert found[0]["series_write"]["icloud"] == {"scopes": ["this"], "error": "capability_unsupported"}


# spec: ANYM-615
def test_a_calendar_service_without_series_write_gives_the_same_constraints_as_before(tmp_path):
    # The calendar service at the deployed SHA may not send series_write: nothing is added in its place.
    constraints = {"recurrence_write": False, "future_split": False, "invitations": False, "window_limit_days": 31}
    with TestClient(calendar_app(tmp_path, constraints)) as c:
        assert calendar_constraints(c) == [constraints] * 4


# spec: ANYM-615
def test_calendar_capabilities_says_the_plan_flags_describe_calendar_plan_and_series_write_calendar_schedule():
    from agent_gateway.dispatch import CALENDAR_CONSTRAINTS

    catalog = json.loads((Path(agent_gateway.__file__).parent / "catalog.json").read_text())
    description = next(tool for tool in catalog if tool["name"] == "calendar_capabilities")["description"]
    assert "recurrence_write and future_split describe calendar_plan only" in description
    assert "series_write lists calendar_schedule's series scopes per provider" in description
    assert {"recurrence_write", "future_split", "series_write"} <= CALENDAR_CONSTRAINTS
