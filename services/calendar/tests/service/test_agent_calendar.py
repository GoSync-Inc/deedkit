"""Synthetic regression cases for agent-visible calendar evidence."""

import asyncio

import pytest

from family_calendar.errors import CalendarError
from family_calendar.service.api import Gateway
from family_calendar.service.core import FamilyService
from family_calendar.service.worker import Worker
from tests.service.test_policy import SCOPES

START, END = "2026-10-19T08:00:00Z", "2026-10-19T12:00:00Z"


@pytest.fixture
def access(household):
    household.grant("family", "adult-a", "cal-a", SCOPES, "details")
    household.binding("family", "adult-a", "cal-a")
    return household.agent("agent", "family", "adult-a", SCOPES, ["cal-a"])


def additional(household, key):
    household.calendar("cal-b", "google-a", "synthetic-b", "Work", "UTC", "write")
    household.grant("family", "adult-a", "cal-b", SCOPES, "details")
    household.query(
        "INSERT INTO agent_calendars(principal_id,calendar_ref,visibility) VALUES ('agent','cal-b','details')"
    )
    household.publish_window("cal-a", START, END, [], "complete")
    household.publish_window(
        "cal-b",
        START,
        END,
        [
            {
                "resource_ref": "res-b",
                "target": "synthetic-b",
                "etag": '"v1"',
                "payload": {},
                "occurrences": [
                    {
                        "ref": "busy-b",
                        "start": "2026-10-19T09:00:00Z",
                        "end": "2026-10-19T10:00:00Z",
                        "details": {},
                        "busy": True,
                    }
                ],
            }
        ],
        "complete",
    )


def test_members_expose_only_authorized_availability_bindings(household, access):
    household.calendar("hidden", "google-a", "hidden", "Private", "UTC", "read")
    household.binding("family", "child", "hidden")
    members = {m["person_id"]: m for m in FamilyService(household).family(access)["members"]}
    assert members["adult-a"]["availability_calendar_refs"] == ["cal-a"]
    assert members["adult-b"]["availability_sources_status"] == "unbound"
    assert members["child"]["availability_sources_status"] == "inaccessible"
    assert "hidden" not in str(members)


def test_calendar_discovery_includes_availability_grant_for_mixed_scope_key(household, access):
    household.grant("family", "adult-a", "cal-a", ["availability:read"], "busy_only")
    result = FamilyService(household).calendars(access)
    assert [c["calendar_ref"] for c in result["calendars"]] == ["cal-a"]
    assert result["calendars"][0]["allowed_event_actions"] == []


def test_explicit_sources_add_to_bindings_and_block_busy_intervals(household, access):
    additional(household, access)
    result = FamilyService(household).slots(
        access, ["adult-a"], START, END, 60, additional_calendar_refs=["cal-b"]
    )
    assert result["source_selection"]["calendar_refs"] == ["cal-a", "cal-b"]
    assert result["source_selection"]["scope"] == "configured_and_explicit_sources_only"
    assert result["candidate_slots"] == [
        {"start": "2026-10-19T08:00:00+00:00", "end": "2026-10-19T09:00:00+00:00"},
        {"start": "2026-10-19T10:00:00+00:00", "end": "2026-10-19T12:00:00+00:00"},
    ]


def test_additional_sources_never_make_unbound_member_available(household, access):
    household.publish_window("cal-a", START, END, [], "complete")
    result = FamilyService(household).slots(
        access, ["adult-b"], START, END, 60, additional_calendar_refs=["cal-a"]
    )
    assert result["availability"] == "unknown" and not result["verified_slots"]
    assert result["unbound_members"] == ["adult-b"]


@pytest.mark.asyncio
async def test_refresh_validates_every_source_before_queueing(household, access):
    gateway = Gateway(household)
    with pytest.raises(CalendarError, match="not_found_or_forbidden"):
        await gateway.call(
            access, "agenda", {"calendar_refs": ["cal-a", "foreign"], "start": START, "end": END}
        )
    assert household.one("SELECT count(*) AS n FROM sync_jobs")["n"] == 0


@pytest.mark.asyncio
async def test_refresh_coalesces_and_passive_poll_waits_for_worker(household, access, monkeypatch):
    # Keep the request deadline short while exercising the real queue and isolated DB.
    monkeypatch.setattr("family_calendar.service.api.REFRESH_WAIT_SECONDS", 0, raising=False)
    gateway = Gateway(household)
    household.publish_window("cal-a", START, END, [], "complete")
    args = {"member_ids": ["adult-a"], "start": START, "end": END, "duration_minutes": 60}
    first, second = await asyncio.gather(
        gateway.call(access, "slots", args), gateway.call(access, "slots", args)
    )
    assert first["refresh"]["status"] == second["refresh"]["status"] == "pending"
    assert first["refresh"]["jobs"] == second["refresh"]["jobs"]
    assert household.one("SELECT count(*) AS n FROM sync_jobs")["n"] == 1
    passive = await gateway.call(access, "slots", dict(args, refresh=False))
    assert passive["availability"] == "unknown" and passive["verified_slots"] == []
    assert passive["refresh"]["status"] == "pending"
    assert passive["configured_sources_complete"] is False
    assert passive["configured_bindings_availability"] == "unknown"
    assert passive["candidate_slots"] == []

    class Provider:
        async def read_window(self, cal, start, end):
            return []

    await Worker(household, Provider()).sync_one()
    final = await gateway.call(access, "slots", dict(args, refresh=False))
    assert final["configured_sources_complete"] and final["availability"] == "unknown"
    assert household.one("SELECT count(*) AS n FROM sync_jobs")["n"] == 1


@pytest.mark.asyncio
async def test_passive_poll_reads_data_after_concurrent_refresh_finishes(household, access, monkeypatch):
    household.publish_window("cal-a", START, END, [], "complete")
    household.query(
        "INSERT INTO sync_jobs(id,principal_id,calendar_ref,start_at,end_at) "
        "VALUES ('sync-a','agent','cal-a',%s,%s)",
        (START, END),
    )

    class Provider:
        async def read_window(self, cal, start, end):
            return [
                {
                    "resource_ref": "res-new",
                    "target": "synthetic-a",
                    "etag": '"v2"',
                    "payload": {},
                    "occurrences": [
                        {"ref": "busy-new", "start": START, "end": END, "details": {}, "busy": True}
                    ],
                }
            ]

    gateway = Gateway(household)
    refresh = gateway.refresh

    async def finish_then_poll(*args, **kwargs):
        await Worker(household, Provider()).sync_one()
        return await refresh(*args, **kwargs)

    monkeypatch.setattr(gateway, "refresh", finish_then_poll)
    result = await gateway.call(
        access,
        "slots",
        {"member_ids": ["adult-a"], "start": START, "end": END, "duration_minutes": 60, "refresh": False},
    )
    assert result["configured_sources_complete"] and result["availability"] == "unknown"
    assert result["candidate_slots"] == []


@pytest.mark.asyncio
async def test_invalid_slot_request_does_not_schedule_refresh(household, access):
    with pytest.raises(CalendarError, match="not_found_or_forbidden"):
        await Gateway(household).call(
            access,
            "slots",
            {"member_ids": ["adult-a", "foreign"], "start": START, "end": END, "duration_minutes": 60},
        )
    assert household.one("SELECT count(*) AS n FROM sync_jobs")["n"] == 0


@pytest.mark.asyncio
async def test_worker_failure_publishes_coverage_even_without_previous_cache(household, access):
    household.query(
        "INSERT INTO sync_jobs(id,principal_id,calendar_ref,start_at,end_at) "
        "VALUES ('sync-a','agent','cal-a',%s,%s)",
        (START, END),
    )

    class Provider:
        async def read_window(self, cal, start, end):
            raise RuntimeError("private provider detail")

    await Worker(household, Provider()).sync_one()
    result = FamilyService(household).agenda(access, ["cal-a"], START, END)
    assert result["coverage"][0]["error"] == "provider_unavailable"
    assert not result["required_sources_complete"]
    assert "private provider detail" not in str(result)


# spec: ANYM-662
def test_capabilities_report_the_deployed_build(household, access, monkeypatch):
    service = FamilyService(household)
    monkeypatch.setenv("DEEDKIT_SOURCE_SHA", "c" * 40)
    build = service.capabilities(access)["build"]
    assert build["source_sha"] == "c" * 40 and build["started_at"].endswith("Z")
    # An invalid or absent value reads as null, like messaging's rule.
    for value in ("not-a-sha", "C" * 40, "c" * 39, ""):
        monkeypatch.setenv("DEEDKIT_SOURCE_SHA", value)
        assert service.capabilities(access)["build"]["source_sha"] is None
    monkeypatch.delenv("DEEDKIT_SOURCE_SHA")
    assert service.capabilities(access)["build"]["source_sha"] is None


def test_capabilities_describe_limits_and_calendar_specific_write_admission(household, access):
    household.write_opt_in("cal-a", True)
    service = FamilyService(household)
    result = service.capabilities(access)
    assert result["constraints"]["recurrence_write"] is False
    assert result["constraints"]["cache_max_age_seconds"] == 300
    assert result["calendars"][0]["allowed_event_actions"] == ["create", "update", "delete"]
    busy = household.agent("busy", "family", "adult-a", ["availability:read"], ["cal-a"], "busy_only")
    assert service.capabilities(busy)["calendars"][0]["allowed_event_actions"] == []


@pytest.mark.asyncio
async def test_refresh_uses_calendar_availability_grant_when_agenda_not_granted(household, access):
    household.grant("family", "adult-a", "cal-a", ["availability:read"], "busy_only")
    household.query(
        "INSERT INTO sync_jobs(id,principal_id,calendar_ref,start_at,end_at) "
        "VALUES ('sync-a','agent','cal-a',%s,%s)",
        (START, END),
    )

    class Provider:
        async def read_window(self, cal, start, end):
            return []

    await Worker(household, Provider()).sync_one()
    assert household.one("SELECT state FROM sync_jobs WHERE id='sync-a'")["state"] == "verified"


@pytest.mark.asyncio
async def test_additional_sources_are_validated_before_refresh_and_appear_in_coverage(
    household, access, monkeypatch
):
    monkeypatch.setattr("family_calendar.service.api.REFRESH_WAIT_SECONDS", 0)
    additional(household, access)
    gateway = Gateway(household)
    args = {
        "member_ids": ["adult-a"],
        "start": START,
        "end": END,
        "duration_minutes": 60,
        "additional_calendar_refs": ["cal-b"],
    }
    result = await gateway.call(access, "slots", args)
    assert {j["calendar_ref"] for j in result["refresh"]["jobs"]} == {"cal-a", "cal-b"}
    assert result["availability"] == "unknown"
    with pytest.raises(CalendarError, match="not_found_or_forbidden"):
        await gateway.call(access, "slots", dict(args, additional_calendar_refs=["foreign"]))
    assert household.one("SELECT count(*) AS n FROM sync_jobs")["n"] == 2


def test_rest_and_mcp_include_explicit_sources_with_identical_busy_slots(household, access):
    from starlette.testclient import TestClient
    from family_calendar.service.api import create_app

    additional(household, access)
    args = {
        "member_ids": ["adult-a"],
        "start": START,
        "end": END,
        "duration_minutes": 60,
        "additional_calendar_refs": ["cal-b"],
        "refresh": False,
    }
    with TestClient(create_app(household), base_url="http://127.0.0.1:13232") as client:
        headers = {"Authorization": "Bearer " + access, "Accept": "application/json, text/event-stream"}
        rest = client.post("/v1/slots", json=args, headers=headers)
        mcp = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "family_slots", "arguments": args},
            },
        )
        assert rest.status_code == 200
        assert rest.json() == mcp.json()["result"]["structuredContent"]
        assert len(rest.json()["candidate_slots"]) == 2
        assert rest.json()["availability"] == "unknown" and rest.json()["verified_slots"] == []
