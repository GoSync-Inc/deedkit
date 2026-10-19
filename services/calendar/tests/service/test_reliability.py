import pytest

from family_calendar.errors import CalendarError
from family_calendar.service.core import FamilyService
from family_calendar.service.store import instant
from tests.service.test_policy import SCOPES

START = "2026-10-19T08:00:00Z"
END = "2026-10-19T12:00:00Z"


def setup(household):
    household.grant("family", "adult-a", "cal-a", SCOPES, "details")
    household.write_opt_in("cal-a", True)
    return FamilyService(household), household.agent("reliability", "family", "adult-a", SCOPES, ["cal-a"])


def publish(
    store,
    ref="cal-a",
    *,
    busy=True,
    start=START,
    end="2026-10-19T09:00:00Z",
    title="Private canary",
    details=None,
):
    store.publish_window(
        ref,
        START,
        END,
        [
            {
                "resource_ref": "res-" + ref,
                "target": "provider-private-" + ref,
                "etag": '"v1"',
                "payload": {},
                "occurrences": [
                    {
                        "ref": "event-" + ref,
                        "start": start,
                        "end": end,
                        "details": details or {"title": title, "timezone": "UTC"},
                        "busy": busy,
                    }
                ],
            }
        ],
        "complete",
    )


def create(start=START, end="2026-10-19T09:00:00Z"):
    return {
        "action": "create",
        "calendar_ref": "cal-a",
        "event": {"title": "Proposed", "start": start, "end": end, "timezone": "UTC"},
    }


def test_member_slots_declare_checked_bindings_without_claiming_whole_person(household):
    service, key = setup(household)
    household.binding("family", "adult-a", "cal-a")
    publish(household)
    value = service.slots(key, ["adult-a"], START, END, 30)
    assert value["availability_scope"] == "configured_bindings"
    assert value["member_sources_complete"] == "unknown"
    assert value["checked_member_sources"] == [{"member_id": "adult-a", "calendar_refs": ["cal-a"]}]
    assert value["availability"] == "unknown"
    assert value["configured_bindings_availability"] == "verified"
    assert value["verified_slots"] == []
    assert value["candidate_slots"] == [
        {"start": "2026-10-19T09:00:00+00:00", "end": "2026-10-19T12:00:00+00:00"}
    ]


def test_plan_reports_overlaps_and_explicit_diff(household):
    service, key = setup(household)
    publish(household)
    result = service.plan(key, [create()])
    step = result["steps"][0]
    assert step["diff"]["start"]["before"] is None
    assert step["diff"]["title"]["after"] == "Proposed"
    assert result["overlap_check"]["status"] == "complete_within_visible_calendars"
    assert result["overlap_check"]["overlaps"][0]["event_ref"] == "event-cal-a"


def test_plan_half_open_intervals_and_transparent_events(household):
    service, key = setup(household)
    publish(household)
    assert (
        service.plan(key, [create("2026-10-19T09:00:00Z", "2026-10-19T10:00:00Z")])["overlap_check"][
            "overlaps"
        ]
        == []
    )
    publish(household, busy=False)
    assert service.plan(key, [create()])["overlap_check"]["overlaps"] == []


def test_plan_update_excludes_own_occurrence_and_shows_changed_fields(household):
    service, key = setup(household)
    publish(household)
    action = dict(create(), action="update", event_ref="event-cal-a")
    result = service.plan(key, [action])
    assert result["overlap_check"]["overlaps"] == []
    assert result["steps"][0]["diff"] == {"title": {"before": "Private canary", "after": "Proposed"}}


def test_plan_update_diff_omits_kept_fields_and_shows_all_day_conversion(household):
    service, key = setup(household)
    # Provider details carry no timezone; omitted fields are kept by the provider.
    publish(household, details={"title": "Dentist", "location": "Clinic", "all_day": True})
    action = {
        "action": "update",
        "calendar_ref": "cal-a",
        "event_ref": "event-cal-a",
        "event": {"start": "2026-10-19T10:00:00Z", "end": "2026-10-19T11:00:00Z", "timezone": "UTC"},
    }
    diff = service.plan(key, [action])["steps"][0]["diff"]
    assert set(diff) == {"start", "end", "all_day"}
    assert diff["all_day"] == {"before": True, "after": False}


def test_plan_preview_reports_instant_event_end_as_its_start(household):
    service, key = setup(household)
    # A zero-length provider event is stored with the smallest span the projection allows.
    point = "2026-10-19T08:30:00Z"
    publish(household, start=point, end=point, busy=False, details={"title": "Reminder", "instant": True})
    update = {
        "action": "update",
        "calendar_ref": "cal-a",
        "event_ref": "event-cal-a",
        # Writes are always timed, so an update gives the instant a duration.
        "event": {"start": point, "end": "2026-10-19T09:00:00Z", "timezone": "UTC"},
    }
    result = service.plan(key, [update])
    [step] = result["steps"]
    assert step["before"]["end"] == step["before"]["start"]
    assert instant(step["before"]["end"]) == instant(point)
    assert set(step["diff"]) == {"end"}
    assert instant(step["diff"]["end"]["before"]) == instant(point)
    # The provider identity the worker writes against is unchanged.
    plan = household.one("SELECT payload FROM plans WHERE id=%s", (result["plan_id"],))
    [stored] = plan["payload"]["steps"]
    assert (stored["target_id"], stored["expected_etag"]) == ("provider-private-cal-a", '"v1"')
    # Moving only the start keeps the source end, so the end is not a change.
    earlier = dict(update, event={"start": "2026-10-19T08:00:00Z", "end": point, "timezone": "UTC"})
    [step] = service.plan(key, [earlier])["steps"]
    assert set(step["diff"]) == {"start"}
    with pytest.raises(CalendarError, match="invalid_window"):
        service.plan(key, [dict(update, event={"start": point, "end": point, "timezone": "UTC"})])
    delete = {"action": "delete", "calendar_ref": "cal-a", "event_ref": "event-cal-a"}
    [step] = service.plan(key, [delete])["steps"]
    assert step["before"]["end"] == step["before"]["start"] == step["diff"]["end"]["before"]


def test_plan_preview_keeps_stored_end_of_timed_event(household):
    service, key = setup(household)
    # Providers always mark timed events as non-instant.
    publish(household, details={"title": "Private canary", "timezone": "UTC", "instant": False})
    end = "2026-10-19T09:00:00Z"
    update = dict(create(START, end), action="update", event_ref="event-cal-a")
    [step] = service.plan(key, [update])["steps"]
    assert instant(step["before"]["end"]) == instant(end)
    assert step["diff"] == {"title": {"before": "Private canary", "after": "Proposed"}}
    delete = {"action": "delete", "calendar_ref": "cal-a", "event_ref": "event-cal-a"}
    [step] = service.plan(key, [delete])["steps"]
    assert instant(step["before"]["end"]) == instant(end)


def test_plan_rejects_recurring_guest_and_special_instant_events(household):
    service, key = setup(household)
    point = "2026-10-19T08:30:00Z"
    delete = {"action": "delete", "calendar_ref": "cal-a", "event_ref": "event-cal-a"}
    for flag in ("recurring", "guests", "special"):
        details = {"title": "Reminder", "instant": True, flag: True}
        publish(household, start=point, end=point, busy=False, details=details)
        with pytest.raises(CalendarError, match="capability_unverified"):
            service.plan(key, [delete])


def test_plan_multi_action_removes_deleted_and_reports_proposed_overlap(household):
    service, key = setup(household)
    publish(household)
    actions = [{"action": "delete", "calendar_ref": "cal-a", "event_ref": "event-cal-a"}, create(), create()]
    result = service.plan(key, actions)
    assert all("event_ref" not in row for row in result["overlap_check"]["overlaps"])
    assert result["overlap_check"]["overlaps"] == [{"step": 1, "other_step": 2, "kind": "proposed_overlap"}]


def test_plan_missing_coverage_is_explicitly_incomplete(household):
    service, key = setup(household)
    result = service.plan(key, [create()])
    assert result["overlap_check"]["status"] == "incomplete"
    assert result["overlap_check"]["coverage"][0]["status"] == "unknown"


def test_plan_busy_only_overlap_never_reveals_event_identity(household):
    service, _ = setup(household)
    household.calendar("cal-b", "google-a", "private-b", "Hidden title", "UTC", "write")
    household.grant("family", "adult-a", "cal-b", ["availability:read"], "busy_only")
    key = household.agent("busy-overlap", "family", "adult-a", SCOPES, ["cal-a", "cal-b"])
    publish(household)
    publish(household, "cal-b")
    result = service.plan(key, [create()])
    hidden = [row for row in result["overlap_check"]["overlaps"] if row.get("calendar_ref") == "cal-b"]
    assert hidden and hidden[0]["visibility"] == "busy_only"
    assert "Private canary" not in str(hidden)
    assert "event-cal-b" not in str(hidden) and "provider-private-cal-b" not in str(result)


def test_calendar_identity_does_not_infer_duplicates_from_labels(household):
    service, key = setup(household)
    row = service.calendars(key)["calendars"][0]
    assert row["identity_basis"] == "provider_resource"
    assert row["primary"] is None and row["duplicate_of"] is None


def test_large_cached_calendar_keeps_plan_valid_but_overlap_check_incomplete(household):
    service, key = setup(household)
    records = [
        {
            "resource_ref": "res-" + str(i),
            "target": "provider-" + str(i),
            "etag": '"v1"',
            "payload": {},
            "occurrences": [
                {
                    "ref": "event-" + str(i),
                    "start": START,
                    "end": "2026-10-19T09:00:00Z",
                    "details": {"title": "Cached", "timezone": "UTC"},
                    "busy": True,
                }
            ],
        }
        for i in range(501)
    ]
    household.publish_window("cal-a", START, END, records, "complete")
    result = service.plan(key, [create()])
    assert result["plan_id"]
    assert result["overlap_check"]["status"] == "incomplete"
    assert result["overlap_check"]["reason"] == "event_limit"


def wall(start, end, zone):
    return {
        "action": "create",
        "calendar_ref": "cal-a",
        "event": {"title": "Wall time", "start": start, "end": end, "timezone": zone},
    }


def test_plan_reads_offset_free_event_times_in_the_event_timezone(household):
    service, key = setup(household)
    result = service.plan(key, [wall("2026-10-20T10:00:00", "2026-10-20T10:30", "Asia/Makassar")])
    event = household.one("SELECT payload FROM plans WHERE id=%s", (result["plan_id"],))["payload"]
    assert event["steps"][0]["event"]["start"] == "2026-10-20T02:00:00+00:00"
    assert result["steps"][0]["diff"]["end"]["after"] == "2026-10-20T02:30:00+00:00"
    offset = service.plan(key, [wall("2026-10-20T10:00:00+08:00", "2026-10-20T10:30", "Asia/Makassar")])
    assert offset["steps"][0]["event"]["start"] == "2026-10-20T02:00:00+00:00"
    for start, end, zone, code in [
        ("2026-03-29T02:30", "2026-03-29T04:00", "Europe/Berlin", "invalid_local_time"),
        ("2026-10-25T02:30", "2026-10-25T04:00", "Europe/Berlin", "ambiguous_local_time"),
        ("2026-10-20", "2026-10-21", "Asia/Makassar", "invalid_time"),
        ("2026-10-20T25:00", "2026-10-21T01:00", "Asia/Makassar", "invalid_time"),
        ("2026-10-20T10:00", "2026-10-20T11:00", "../Makassar", "invalid_timezone"),
    ]:
        with pytest.raises(CalendarError, match=code):
            service.plan(key, [wall(start, end, zone)])
    assert service.capabilities(key)["constraints"]["time_format"].startswith("ISO 8601")


OCCURRENCE = {
    "title": "Lesson",
    "recurring": True,
    "series_ref": "series-cal-a",
    "original_start": START.replace("Z", "+00:00"),
}


def occurrence_update(**event):
    return {
        "action": "update",
        "calendar_ref": "cal-a",
        "event_ref": "event-cal-a",
        "event": {
            "title": "Moved lesson",
            "start": "2026-10-19T10:00:00Z",
            "end": "2026-10-19T11:00:00Z",
            "timezone": "UTC",
            **event,
        },
    }


OCCURRENCE_DELETE = {"action": "delete", "calendar_ref": "cal-a", "event_ref": "event-cal-a"}


# spec: ANYM-234
def test_capabilities_report_occurrence_and_all_day_scope_without_series_or_invitations(household):
    service, key = setup(household)
    value = service.capabilities(key)
    constraints, features = value["constraints"], value["features"]
    assert "one occurrence" in constraints["event_write"] and "all-day" in constraints["event_write"]
    for scope in (constraints, features):
        assert scope["occurrence_write"] is True and scope["all_day_write"] is True
        assert (scope["recurrence_write"], scope["future_split"], scope["invitations"]) == (
            False,
            False,
            False,
        )
    assert features["recurrence_read"] is True
    assert constraints["provider_write"] == {
        "google": {"occurrence_write": True, "all_day_write": True},
        "icloud": {"occurrence_write": True, "all_day_write": True},
    }


# spec: ANYM-234
def test_plan_targets_one_occurrence_and_rejects_series_scope(household):
    service, key = setup(household)
    publish(household, details=dict(OCCURRENCE))
    [step] = service.plan(key, [occurrence_update()])["steps"]
    # The preview names the series and the occurrence's original start, never provider IDs.
    assert step["occurrence"] == {"series_ref": "series-cal-a", "original_start": "2026-10-19T08:00:00+00:00"}
    assert step["diff"]["title"] == {"before": "Lesson", "after": "Moved lesson"}
    assert set(step["diff"]) == {"title", "start", "end"}
    assert "target_id" not in step and "provider-private-cal-a" not in str(step)
    [cancel] = service.plan(key, [OCCURRENCE_DELETE])["steps"]
    assert cancel["action"] == "delete" and cancel["occurrence"] == step["occurrence"]
    stored = household.one("SELECT payload FROM plans WHERE id=%s", (service.plan(key, [OCCURRENCE_DELETE])["plan_id"],))
    assert stored["payload"]["steps"][0]["occurrence"] == step["occurrence"]
    # A series-wide target has no occurrence identity: a master, or a row stored before identities existed.
    for details in (
        {"title": "Lesson", "recurring": True, "series_ref": "series-cal-a"},
        {"title": "Lesson", "recurring": True, "original_start": OCCURRENCE["original_start"]},
        {"title": "Lesson", "recurring": True},
    ):
        publish(household, details=details)
        for action in (occurrence_update(), OCCURRENCE_DELETE):
            with pytest.raises(CalendarError, match="capability_unverified"):
                service.plan(key, [action])


# spec: ANYM-234
def test_occurrence_with_guests_stays_unverified(household):
    service, key = setup(household)
    for flag in ("guests", "special"):
        publish(household, details=dict(OCCURRENCE, **{flag: True}))
        for action in (occurrence_update(), OCCURRENCE_DELETE):
            with pytest.raises(CalendarError, match="capability_unverified"):
                service.plan(key, [action])
    # No write path asks the provider to notify anybody.
    assert service.plan(key, [create()])["notifications"].startswith("none")


# spec: ANYM-234
def test_occurrence_cannot_change_between_timed_and_all_day(household):
    service, key = setup(household)
    publish(household, details=dict(OCCURRENCE))
    with pytest.raises(CalendarError, match="capability_unverified"):
        service.plan(key, [dict(occurrence_update(), event=all_day())])
    publish(household, details=dict(OCCURRENCE, all_day=True))
    with pytest.raises(CalendarError, match="capability_unverified"):
        service.plan(key, [occurrence_update()])


def all_day(start="2026-10-19", end="2026-10-20", **extra):
    event = {"all_day": True, "start": start, **extra}
    if end:
        event["end"] = end
    return event


def all_day_action(kind="create", **event):
    action = {"action": kind, "calendar_ref": "cal-a", "event": all_day(**event)}
    if kind == "update":
        action["event_ref"] = "event-cal-a"
    return action


# Asia/Makassar is UTC+8 without DST: local midnight 2026-10-19 is 2026-10-18T16:00Z.
MIDNIGHT = "2026-10-18T16:00:00Z"
NEXT_MIDNIGHT = "2026-10-19T16:00:00Z"


# spec: ANYM-234
def test_all_day_update_and_conversion_preview(household):
    service, key = setup(household)
    publish(household, start=MIDNIGHT, end=NEXT_MIDNIGHT, details={"title": "Holiday", "all_day": True})
    # Staying all-day: the same dates are no change and no conversion is shown.
    [step] = service.plan(key, [all_day_action("update", title="Holiday!")])["steps"]
    assert step["diff"] == {"title": {"before": "Holiday", "after": "Holiday!"}}
    assert step["event"]["all_day"] is True
    # A single day without an end is start + 1 day; moving it shows both dates.
    [step] = service.plan(key, [all_day_action("update", start="2026-10-21", end=None)])["steps"]
    assert step["event"]["end"] == "2026-10-22"
    assert step["diff"] == {
        "start": {"before": "2026-10-19", "after": "2026-10-21"},
        "end": {"before": "2026-10-20", "after": "2026-10-22"},
    }
    # Explicit conversions in both directions.
    timed = {"start": "2026-10-19T10:00:00Z", "end": "2026-10-19T11:00:00Z", "timezone": "UTC"}
    [step] = service.plan(key, [dict(all_day_action("update"), event=timed)])["steps"]
    assert step["diff"]["all_day"] == {"before": True, "after": False}
    publish(household, details={"title": "Dentist"})
    [step] = service.plan(key, [all_day_action("update")])["steps"]
    assert step["diff"]["all_day"] == {"before": False, "after": True}
    # Create keeps the date values and a multi-day exclusive end.
    [step] = service.plan(key, [all_day_action(title="Trip", end="2026-10-22")])["steps"]
    assert (step["event"]["start"], step["event"]["end"]) == ("2026-10-19", "2026-10-22")


# spec: ANYM-234
@pytest.mark.parametrize(
    "event,code",
    [
        (all_day(end="2026-10-19"), "invalid_window"),
        (all_day(end="2026-10-18"), "invalid_window"),
        (all_day(end="2026-12-19"), "invalid_window"),
        (all_day(timezone="UTC"), "invalid_event"),
        (all_day(start="2026-10-19T10:00:00Z", end="2026-10-20T10:00:00Z"), "invalid_event"),
        (all_day(start="2026-10-19", end="2026-10-20T00:00:00"), "invalid_event"),
        (all_day(start="2026-13-40"), "invalid_event"),
        (all_day(all_day="yes"), "invalid_event"),
        ({"all_day": True, "end": "2026-10-20"}, "invalid_event"),
        ({"start": "2026-10-19", "end": "2026-10-20", "timezone": "UTC"}, "invalid_time"),
    ],
)
def test_invalid_all_day_ranges_and_mixed_fields_are_rejected(household, event, code):
    service, key = setup(household)
    with pytest.raises(CalendarError, match=code):
        service.plan(key, [{"action": "create", "calendar_ref": "cal-a", "event": dict(event, title="x")}])


# spec: ANYM-234
def test_all_day_event_blocks_the_whole_local_day_in_the_overlap_check(household):
    service, key = setup(household)
    publish(household)  # 08:00-09:00Z is 16:00-17:00 on 2026-10-19 in Asia/Makassar
    overlaps = service.plan(key, [all_day_action(title="Holiday")])["overlap_check"]["overlaps"]
    assert [o["event_ref"] for o in overlaps] == ["event-cal-a"]
    overlaps = service.plan(key, [all_day_action(title="Later", start="2026-10-20", end=None)])["overlap_check"]
    assert overlaps["overlaps"] == []


# spec: ANYM-234
@pytest.mark.parametrize("missing", ["start", "end", "timezone"])
def test_timed_event_without_a_required_field_is_rejected_naming_the_field(household, missing):
    service, key = setup(household)
    event = {"title": "x", "start": START, "end": END, "timezone": "UTC"}
    del event[missing]
    with pytest.raises(CalendarError, match="invalid_event") as caught:
        service.plan(key, [create(), {"action": "create", "calendar_ref": "cal-a", "event": event}])
    assert caught.value.field_errors == [{"field": f"actions/1/event/{missing}", "reason": "required"}]
    # Nothing was stored for apply and no provider is reachable from planning.
    assert household.one("SELECT count(*) AS n FROM plans")["n"] == 0


# spec: ANYM-234
def test_all_day_event_with_a_timezone_is_rejected_naming_the_field(household):
    service, key = setup(household)
    with pytest.raises(CalendarError, match="invalid_event") as caught:
        service.plan(key, [all_day_action(timezone="UTC")])
    assert caught.value.field_errors == [{"field": "actions/0/event/timezone", "reason": "not_allowed_for_all_day"}]
