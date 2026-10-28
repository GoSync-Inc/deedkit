"""calendar_schedule through the gateway: wait admission and relayed selection candidates (ANYM-552)."""
import asyncio
import json

import pytest

from agent_gateway.catalog import CATALOG
from agent_gateway.dispatch import Dispatch, caller_payload
from agent_gateway.http_clients import admission, wait_tools
from agent_gateway.policy import Denied, Policy
from test_policy import config

SCOPES = ["agenda:read", "event:read", "plan:create", "plan:execute"]
MOVE = {"action": "move", "event": {"title": "Dentist", "date": "2026-10-20"}, "start": "2026-10-21T15:00"}


def candidate(event_ref, label="Tue 20 Oct 10:00–11:00"):
    return {"id": event_ref, "kind": "event", "account": "cal-a", "label": label,
            "next": {"tool": "calendar_schedule", "arguments": {"action": "move", "event": event_ref,
                                                                "start": "2026-10-21T15:00", "dry_run": True}}}


def gateway(tmp_path, answer):
    key = tmp_path / "backend.key"
    key.write_text("backend-calendar-a")
    key.chmod(0o600)
    document = config()
    document["profiles"]["reader"]["tools"] = ["calendar_capabilities", "calendar_agenda", "calendar_schedule"]
    document["principals"][0]["grants"]["calendar"] = SCOPES
    document["principals"][0]["credentials"]["calendar"]["key_file"] = str(key)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)

    class Clients:
        deployment = Policy(path).load()["deployment"]

        async def call(self, module, operation, credential, arguments):
            if operation == "capabilities":
                return {"permissions": SCOPES, "calendars": [], "features": {"schedule": True}, "constraints": {}}
            error = Denied("backend_denied", 409)
            error.detail = answer
            raise error

    return Dispatch(Policy(path), Clients())


# spec: ANYM-552
def test_schedule_row_needs_plan_create_and_execute_and_mirrors_the_service():
    row = CATALOG["calendar_schedule"]
    assert row["module"] == "calendar" and row["operation"] == "schedule"
    assert row["scopes"] == ["plan:create", "plan:execute"] and row["annotations"]["destructiveHint"] is True


# spec: ANYM-552
def test_a_live_schedule_waits_in_the_wait_pool_and_a_dry_run_never_waits():
    tools = wait_tools(CATALOG)
    assert admission(tools, "calendar", "schedule", {"action": "cancel", "event": "e", "request_id": "r" * 8}) == "waits"
    assert admission(tools, "calendar", "schedule", {"confirm_token": "t" * 32, "wait_s": 25}) == "waits"
    assert admission(tools, "calendar", "schedule", {"confirm_token": "t" * 32, "wait_s": 0}) == "metadata"
    assert admission(tools, "calendar", "schedule", {"action": "cancel", "event": "e", "dry_run": True}) == "metadata"


# spec: ANYM-552
def test_echoed_action_and_start_are_not_payload_but_the_title_is():
    payload = caller_payload(MOVE, CATALOG["calendar_schedule"]["inputSchema"])
    assert payload == {"Dentist"}


# spec: ANYM-552
def test_selection_candidates_keep_their_preview_next_and_never_the_title(tmp_path):
    answer = {"error": "selection_required", "candidates": [
        candidate("evt-1f3a"), candidate("evt-2b4c", label="Dentist 10:00")]}
    with pytest.raises(Denied) as caught:
        asyncio.run(gateway(tmp_path, answer).call("gateway-a", "calendar_schedule", MOVE))
    relayed = caught.value.detail
    assert relayed["error"] == "selection_required" and "next" not in relayed
    # The candidate whose label echoes the caller's title is dropped; the other keeps its runnable preview.
    assert relayed["candidates"] == [candidate("evt-1f3a")]


# spec: ANYM-552
def test_enum_words_and_timestamps_stay_payload_outside_their_own_field():
    schema = {"type": "object", "properties": {
        "action": {"anyOf": [{"type": "string", "enum": ["create", "cancel"]}, {"type": "null"}]},
        "start": {"type": "string"}, "query": {"type": "string"}, "text": {"type": "string"}}}
    arguments = {"action": "cancel", "start": "2026-10-21T15:00", "query": "2026-10-21T15:00", "text": "cancel"}
    assert caller_payload(arguments, schema) == {"2026-10-21T15:00", "cancel"}


# spec: ANYM-552
def test_a_calendar_candidate_keeps_its_next_when_the_caller_named_part_of_its_label(tmp_path):
    lessons = {"id": "cal-child", "label": "Lessons", "kind": "calendar",
               "next": {"tool": "calendar_schedule",
                        "arguments": {"selection_token": "s" * 32, "calendar": "cal-child", "dry_run": True}}}
    answer = {"error": "selection_required", "candidates": [lessons]}
    create = {"action": "create", "title": "Swimming", "start": "2026-11-02T10:00", "calendar": "Less"}
    with pytest.raises(Denied) as caught:
        asyncio.run(gateway(tmp_path, answer).call("gateway-a", "calendar_schedule", create))
    assert caught.value.detail["candidates"] == [lessons]


# spec: ANYM-552
def test_the_service_fallback_label_survives_where_the_label_repeating_the_title_is_dropped(tmp_path):
    def option(ref, label):
        return {"id": ref, "label": label, "kind": "calendar",
                "next": {"tool": "calendar_schedule",
                         "arguments": {"selection_token": "s" * 32, "calendar": ref, "dry_run": True}}}

    answer = {"error": "selection_required", "candidates": [option("cal-child", "#1a"), option("cal-b", "Lessons #1")]}
    create = {"action": "create", "title": "#1", "start": "2026-11-02T10:00", "calendar": "Less"}
    with pytest.raises(Denied) as caught:
        asyncio.run(gateway(tmp_path, answer).call("gateway-a", "calendar_schedule", create))
    assert caught.value.detail["candidates"] == [option("cal-child", "#1a")]
