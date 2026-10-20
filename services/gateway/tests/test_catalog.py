from copy import deepcopy

import pytest

from agent_gateway.catalog import CATALOG, load_catalog, arguments


def test_duplicate_catalog_names_and_reserved_name_rejected():
    tool = deepcopy(CATALOG["calendar_calendars"])
    with pytest.raises(ValueError, match="collision"):
        load_catalog([tool, tool])
    tool["name"] = "platform_capabilities"
    with pytest.raises(ValueError, match="collision"):
        load_catalog([tool])


def test_schema_defaults_do_not_accept_unknown_arguments():
    tool = CATALOG["messages_search"]
    with pytest.raises(ValueError):
        arguments(tool, {"query": "synthetic", "owner_override": True})
    assert "owner_override" not in arguments(tool, {"query": "synthetic"})


READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}


def test_every_tool_declares_hints_and_reads_are_never_destructive():
    for tool in CATALOG.values():
        hints = tool["annotations"]
        assert {"readOnlyHint", "destructiveHint", "openWorldHint"} <= set(hints), tool["name"]
        assert not (hints["readOnlyHint"] and hints["destructiveHint"]), tool["name"]


def test_calls_descriptions_are_distinct_and_lead_with_the_effect():
    calls = {n: t for n, t in CATALOG.items() if t["module"] == "calls"}
    assert len({t["description"] for t in calls.values()}) == len(calls)
    for name, tool in calls.items():
        reads = tool["scopes"] in ([], ["calls:read"])
        assert (tool["annotations"] == READ) is reads, name
        assert tool["description"].startswith("Read-only") is reads, name
    assert calls["calls_dial_call"]["description"].startswith("External effect: places ONE real")
    assert calls["calls_dial_call"]["annotations"] == {
        "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True,
    }
    for name in ("prepare_call", "prepare_external_call", "prepare_followup", "start_call", "stop_call"):
        hints = calls["calls_" + name]["annotations"]
        assert not hints["destructiveHint"] and not hints["openWorldHint"], name
    for name in ("list_briefs", "list_calls"):
        tool = calls["calls_" + name]
        assert tool["operation"] == name and tool["scopes"] == ["calls:read"]
        assert tool["inputSchema"] == calls["calls_get_owner_requests"]["inputSchema"]


def test_calendar_reads_and_local_plan_are_not_presented_as_provider_writes():
    for name in ("capabilities", "calendars", "members", "agenda", "slots", "operation", "assignments"):
        assert CATALOG["calendar_" + name]["annotations"] == READ
    assert CATALOG["calendar_plan"]["annotations"] == {
        "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False,
    }
    for name in ("apply", "assignment"):
        assert CATALOG["calendar_" + name]["annotations"] == {
            "readOnlyHint": False, "destructiveHint": True, "openWorldHint": True,
        }


# spec: ANYM-234
def test_calendar_plan_describes_all_day_and_occurrence_scope_without_series_or_guest_fields():
    plan = CATALOG["calendar_plan"]
    event = plan["inputSchema"]["$defs"]["EventInput"]
    assert event["required"] == ["start"] and event["properties"]["all_day"]["anyOf"][0] == {"type": "boolean"}
    assert not {"attendees", "recurrence", "rrule", "range", "scope", "guests"} & set(event["properties"])
    action = plan["inputSchema"]["$defs"]["Action"]["properties"]
    assert set(action) == {"action", "calendar_ref", "event_ref", "event", "activity", "operation_id"}
    for phrase in ("all-day", "one occurrence", "this-and-following", "capability_unverified"):
        assert phrase in plan["description"]
    assert "guest-free single-event" not in plan["description"]


# spec: ANYM-433
def test_calendar_plan_declares_project_with_a_bounded_activity_label():
    plan = CATALOG["calendar_plan"]
    action = plan["inputSchema"]["$defs"]["Action"]
    assert action["properties"]["action"]["enum"] == ["create", "update", "delete", "project", "unproject"]
    assert action["properties"]["activity"]["anyOf"][0] == {"maxLength": 64, "minLength": 1, "type": "string"}
    assert action["required"] == ["action"] and action["additionalProperties"] is False
    # Every action but unproject still requires calendar_ref.
    assert action["allOf"][0]["else"]["required"] == ["calendar_ref"]
    for phrase in ("project", "projections_skipped", "nothing_to_project", "16 steps", "too_many_steps", "private"):
        assert phrase in plan["description"]


# spec: ANYM-434
def test_calendar_plan_declares_unproject_by_lesson_or_operation():
    plan = CATALOG["calendar_plan"]
    action = plan["inputSchema"]["$defs"]["Action"]
    assert action["properties"]["operation_id"]["anyOf"][0] == {"type": "string"}
    [rule] = action["allOf"]
    assert rule["if"]["properties"]["action"] == {"const": "unproject"}
    assert rule["then"] == {"anyOf": [{"required": ["operation_id"]}, {"required": ["calendar_ref", "event_ref"]}]}
    assert rule["else"] == {"required": ["calendar_ref"], "not": {"required": ["operation_id"]}}
    for phrase in ("unproject", "operation_id", "version_conflict", "outcome_unknown", "nothing_to_unproject"):
        assert phrase in plan["description"]
