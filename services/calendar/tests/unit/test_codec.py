from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import pytest
from icalendar import Calendar

from family_calendar.providers.caldav.codec import (
    create_event,
    semantic_snapshot,
    shift_series,
    move_single,
    cancel_single,
    edit_single,
    split_future,
    occurrences,
    native_future,
)
from family_calendar.errors import CalendarError


START = datetime(2026, 10, 19, 16, tzinfo=ZoneInfo("Europe/Berlin"))


def weekly(count=6):
    return create_event(
        "test@family.invalid",
        START,
        START + timedelta(hours=1),
        "Synthetic lesson",
        rrule={"FREQ": "WEEKLY", "COUNT": count},
    )


def test_roundtrip_preserves_alarms_unknown_properties_and_timezone():
    data = weekly().replace(
        b"END:VEVENT",
        b"X-FAMILY-OPAQUE;X-MODE=keep:opaque-value\r\n"
        b"BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT15M\r\n"
        b"DESCRIPTION:Synthetic alarm\r\nEND:VALARM\r\nEND:VEVENT",
    )
    changed = shift_series(data, timedelta(hours=1))
    before, after = Calendar.from_ical(data), Calendar.from_ical(changed)
    old, new = before.walk("VEVENT")[0], after.walk("VEVENT")[0]
    assert new.decoded("X-FAMILY-OPAQUE") == old.decoded("X-FAMILY-OPAQUE")
    assert new["X-FAMILY-OPAQUE"].params == old["X-FAMILY-OPAQUE"].params
    assert new.subcomponents[0].to_ical() == old.subcomponents[0].to_ical()
    assert new["DTSTART"].params["TZID"] == "Europe/Berlin"


def test_single_move_retains_original_identity_and_rest_of_series():
    rid = START + timedelta(weeks=1)
    data = move_single(weekly(), rid, rid + timedelta(hours=2), rid + timedelta(hours=3))
    values = occurrences(data, START - timedelta(days=1), START + timedelta(weeks=7))
    assert len(values) == 6
    assert values[1]["start"] == rid + timedelta(hours=2)
    assert values[1]["identity"] == rid
    assert values[2]["start"].hour == 16


def test_cancelled_instance_stays_cancelled_after_series_shift():
    rid = START + timedelta(weeks=1)
    moved = START + timedelta(weeks=2)
    data = cancel_single(weekly(), rid)
    data = move_single(data, moved, moved + timedelta(hours=2), moved + timedelta(hours=3))
    changed = shift_series(data, timedelta(hours=1))
    values = occurrences(changed, START - timedelta(days=1), START + timedelta(weeks=7))
    assert len(values) == 5
    assert all(x["start"] != rid + timedelta(hours=1) for x in values)
    assert next(x for x in values if x["start"].hour == 18)["start"] == moved + timedelta(hours=2)


@pytest.mark.parametrize("bound_index", [0, 1, 3, 5])
def test_future_split_preserves_count_and_absolute_exception(bound_index):
    data = weekly()
    rid = START + timedelta(weeks=4)
    data = move_single(data, rid, rid + timedelta(hours=2), rid + timedelta(hours=3))
    data = cancel_single(data, START + timedelta(weeks=2))
    boundary = START + timedelta(weeks=bound_index)
    past, future, mapping = split_future(data, boundary, timedelta(hours=1), "new@family.invalid")
    lower, upper = START - timedelta(days=1), START + timedelta(weeks=8)
    values = (occurrences(past, lower, upper) if past else []) + occurrences(future, lower, upper)
    assert len(values) == 5
    assert any(x["start"] == rid + timedelta(hours=2) for x in values)
    assert len({x["start"] for x in values}) == 5
    assert mapping


def test_infinite_future_split_remains_rule_not_many_events():
    data = create_event(
        "infinite@family.invalid", START, START + timedelta(hours=1), "Weekly", rrule={"FREQ": "WEEKLY"}
    )
    past, future, _ = split_future(
        data, START + timedelta(weeks=3), timedelta(hours=1), "next@family.invalid"
    )
    assert len(Calendar.from_ical(future).walk("VEVENT")) == 1
    assert "COUNT" not in Calendar.from_ical(future).walk("VEVENT")[0]["RRULE"]
    assert len(occurrences(past, START, START + timedelta(weeks=20))) == 3


def test_date_all_day_end_exclusive():
    from datetime import date

    data = create_event("date@family.invalid", date(2026, 10, 19), date(2026, 10, 20), "All day")
    snap = semantic_snapshot(data)
    assert snap["events"][0]["DTEND"]["value"] == "2026-10-20"


def test_dst_weekly_keeps_wall_clock_time():
    values = occurrences(weekly(), START - timedelta(days=1), START + timedelta(weeks=7))
    assert {x["start"].hour for x in values} == {16}
    assert values[0]["start"].utcoffset() != values[1]["start"].utcoffset()


def test_complex_future_rule_fails_explicitly():
    data = create_event(
        "complex@family.invalid",
        START,
        START + timedelta(hours=1),
        "Complex",
        rrule={"FREQ": "MONTHLY", "BYDAY": "MO", "BYSETPOS": -1},
    )
    with pytest.raises(CalendarError, match="unsupported_recurrence"):
        split_future(data, START, timedelta(hours=1), "new@family.invalid")


def test_native_future_keeps_recurrence_id_before_shift():
    rid = START + timedelta(weeks=1)
    data = native_future(weekly(), rid, timedelta(hours=1))
    exception = Calendar.from_ical(data).walk("VEVENT")[1]
    assert exception.decoded("RECURRENCE-ID") == rid
    assert exception["RECURRENCE-ID"].params["RANGE"] == "THISANDFUTURE"


def test_future_partition_preserves_rdate_exdate_and_until():
    until = START + timedelta(weeks=5)
    data = create_event(
        "until@family.invalid",
        START,
        START + timedelta(hours=1),
        "Until",
        rrule={"FREQ": "WEEKLY", "UNTIL": until.astimezone(ZoneInfo("UTC"))},
    )
    cal = Calendar.from_ical(data)
    event = cal.walk("VEVENT")[0]
    event.add("EXDATE", [START + timedelta(weeks=1)])
    event.add("RDATE", [START + timedelta(weeks=3, days=1)])
    data = cal.to_ical()
    past, future, _ = split_future(
        data, START + timedelta(weeks=3), timedelta(hours=1), "next@family.invalid"
    )
    lower, upper = START - timedelta(days=1), START + timedelta(weeks=8)
    vals = occurrences(past, lower, upper) + occurrences(future, lower, upper)
    assert len(vals) == 6
    assert max(x["start"] for x in vals) == until + timedelta(hours=1)


def test_nonexistent_dst_local_time_is_rejected():
    start = datetime(2026, 3, 29, 1, 30, tzinfo=ZoneInfo("Europe/Berlin"))
    data = create_event("gap@family.invalid", start, start + timedelta(minutes=20), "Gap")
    with pytest.raises(CalendarError, match="invalid_local_time"):
        shift_series(data, timedelta(hours=1))


def test_single_move_to_utc_does_not_keep_stale_tzid():
    rid = START + timedelta(weeks=1)
    changed = move_single(
        weekly(), rid, rid.astimezone(ZoneInfo("UTC")), (rid + timedelta(hours=1)).astimezone(ZoneInfo("UTC"))
    )
    event = Calendar.from_ical(changed).walk("VEVENT")[1]
    assert "TZID" not in event["DTSTART"].params
    assert event.decoded("DTSTART") == rid
    assert event["RECURRENCE-ID"].params["TZID"] == "Europe/Berlin"


def test_weekly_byday_shift_across_weekday_is_explicitly_rejected():
    data = create_event(
        "byday@family.invalid",
        START,
        START + timedelta(hours=1),
        "Weekly",
        rrule={"FREQ": "WEEKLY", "BYDAY": "MO", "COUNT": 6},
    )
    with pytest.raises(CalendarError, match="unsupported_recurrence"):
        shift_series(data, timedelta(days=1))


@pytest.mark.parametrize("name", ["weekly-exceptions.ics", "all-day.ics", "floating.ics"])
def test_saved_synthetic_fixture_preserves_full_semantics_on_roundtrip(name):
    from family_calendar.providers.caldav.codec import semantic_hash

    data = (Path(__file__).parents[1] / "fixtures" / "ics" / name).read_bytes()
    assert semantic_hash(data) == semantic_hash(Calendar.from_ical(data).to_ical())


@pytest.mark.parametrize("zone", ["Asia/Makassar", "Europe/Berlin"])
def test_equivalent_timezone_history_does_not_make_readback_unknown(zone):
    from datetime import date
    from dateutil.tz import gettz
    from icalendar import Timezone
    from family_calendar.providers.caldav.codec import semantic_hash

    start = START.astimezone(ZoneInfo(zone))
    desired = create_event("zone@family.invalid", start, start + timedelta(hours=1), "Synthetic")
    observed = Calendar.from_ical(desired)
    observed.subcomponents = [c for c in observed.subcomponents if c.name != "VTIMEZONE"]
    full = Timezone.from_tzinfo(
        gettz(zone), tzid=zone, first_date=date(1900, 1, 1), last_date=date(2040, 1, 1)
    )
    full.pop("COMMENT", None)
    full.add("X-LIC-LOCATION", zone)
    observed.add_component(full)
    assert semantic_hash(desired) == semantic_hash(observed.to_ical())


def test_timezone_same_id_with_changed_dst_is_not_equal():
    from family_calendar.providers.caldav.codec import semantic_hash

    desired = weekly()
    observed = Calendar.from_ical(desired)
    daylight = observed.walk("DAYLIGHT")[0]
    daylight["TZOFFSETTO"].td += timedelta(minutes=30)
    assert semantic_hash(desired) != semantic_hash(observed.to_ical())


def test_event_extension_and_alarm_loss_are_not_timezone_normalization():
    from family_calendar.providers.caldav.codec import semantic_hash

    desired = weekly().replace(b"END:VEVENT", b"X-PRIVATE:keep\r\nEND:VEVENT")
    assert semantic_hash(desired) != semantic_hash(weekly())


def test_timezone_normalization_has_an_explicit_bounded_scope():
    from family_calendar.providers.caldav.codec import semantic_snapshot

    snap = semantic_snapshot(weekly())["calendar"]
    tz = next(c for c in snap["components"] if c["type"] == "VTIMEZONE")
    assert tz["offset_scope"] == ["2020-01-01", "2035-01-01"]


def test_generated_timezone_switches_at_the_actual_utc_dst_boundary():
    from datetime import timezone

    tz = Calendar.from_ical(weekly()).walk("VTIMEZONE")[0].to_tz(lookup_tzid=False)
    for instant, offset in [
        (datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc), 1),
        (datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc), 2),
        (datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc), 2),
        (datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc), 1),
    ]:
        assert instant.astimezone(tz).utcoffset() == timedelta(hours=offset)


def test_unknown_timezone_extension_is_preserved_in_comparison():
    from family_calendar.providers.caldav.codec import semantic_hash

    desired = weekly()
    observed = Calendar.from_ical(desired)
    observed.walk("VTIMEZONE")[0].add("X-OPAQUE-ZONE", "keep")
    assert semantic_hash(desired) != semantic_hash(observed.to_ical())


def test_unknown_timezone_parameter_is_preserved_in_comparison():
    from family_calendar.providers.caldav.codec import semantic_hash

    desired = weekly()
    observed = Calendar.from_ical(desired)
    observed.walk("VTIMEZONE")[0]["TZID"].params["X-OPAQUE"] = "keep"
    assert semantic_hash(desired) != semantic_hash(observed.to_ical())


BERLIN = ZoneInfo("Europe/Berlin")


def fixture_bytes(name="weekly-exceptions.ics"):
    return (Path(__file__).parents[1] / "fixtures" / "ics" / name).read_bytes()


def components(data):
    return semantic_snapshot(data)["calendar"]["components"]


def override_of(data, rid):
    return [e for e in Calendar.from_ical(data).walk("VEVENT") if e.get("RECURRENCE-ID") and e.decoded("RECURRENCE-ID") == rid]


# spec: ANYM-234
def test_caldav_occurrence_update_and_cancel_preserve_the_rest_of_the_series():
    source = fixture_bytes()
    before = components(source)
    update_rid = datetime(2026, 11, 9, 16, tzinfo=BERLIN)
    cancel_rid = datetime(2026, 11, 23, 16, tzinfo=BERLIN)
    updated = edit_single(
        source, update_rid, update_rid + timedelta(hours=2), update_rid + timedelta(hours=3), summary="Moved", location="Room 2"
    )
    cancelled = cancel_single(source, cancel_rid)
    for data, rid in ((updated, update_rid), (cancelled, cancel_rid)):
        after = components(data)
        # Master, VTIMEZONE, VALARM, X-properties and the other exceptions are all still there, unchanged.
        assert all(c in after for c in before)
        [added] = [c for c in after if c not in before]
        props = added["properties"]
        assert props["RECURRENCE-ID"]["value"] == rid.isoformat()
        assert props["RECURRENCE-ID"]["params"] == {"TZID": "Europe/Berlin"}
        assert not {"RRULE", "RDATE", "EXDATE"} & set(props)
        assert added["components"][0]["type"] == "VALARM"  # copied from the master
        assert props["X-FAMILY-FIXTURE"]["params"] == {"X-MODE": "keep"}
    [override] = override_of(updated, update_rid)
    assert (override["SUMMARY"], override["LOCATION"]) == ("Moved", "Room 2")
    assert override.decoded("DTSTART") == update_rid + timedelta(hours=2) and "STATUS" not in override
    [override] = override_of(cancelled, cancel_rid)
    assert str(override["STATUS"]) == "CANCELLED"
    # Cancelling adds the override; it never adds or removes an EXDATE.
    assert b"EXDATE" in source and cancelled.count(b"EXDATE") == updated.count(b"EXDATE") == source.count(b"EXDATE")
    values = occurrences(cancelled, START - timedelta(days=1), START + timedelta(weeks=8))
    assert cancel_rid not in [x["identity"] for x in values] and update_rid in [x["identity"] for x in values]


# spec: ANYM-234
def test_occurrence_edit_keeps_identity_and_rejects_foreign_identities():
    source = fixture_bytes()
    moved = datetime(2026, 11, 16, 16, tzinfo=BERLIN)  # fixture override, actually at 18:00
    once = edit_single(source, moved, moved + timedelta(hours=3), moved + timedelta(hours=4), summary="Again")
    twice = edit_single(once, moved, moved + timedelta(hours=3), moved + timedelta(hours=4), summary="Again")
    assert len(override_of(once, moved)) == len(override_of(twice, moved)) == 1
    assert len(Calendar.from_ical(once).walk("VEVENT")) == len(Calendar.from_ical(source).walk("VEVENT"))
    # A UTC identity from storage is written in the master's own zone.
    utc = datetime(2026, 11, 9, 15, tzinfo=ZoneInfo("UTC"))
    written = edit_single(source, utc, utc, utc + timedelta(hours=1), summary="UTC identity")
    [override] = override_of(written, utc)
    assert override["RECURRENCE-ID"].params["TZID"] == "Europe/Berlin"
    # EXDATE'd, never generated and cancelled-and-gone identities are not occurrences of the series.
    for rid in (datetime(2026, 10, 26, 16, tzinfo=BERLIN), datetime(2026, 11, 10, 16, tzinfo=BERLIN), datetime(2026, 11, 9, 17, tzinfo=BERLIN)):
        with pytest.raises(CalendarError, match="invalid_occurrence_identity"):
            cancel_single(source, rid)
    with pytest.raises(CalendarError, match="invalid_occurrence_identity"):
        edit_single(source, datetime(2026, 11, 9).date(), datetime(2026, 11, 9).date(), datetime(2026, 11, 10).date())


# spec: ANYM-234
def test_all_day_series_occurrence_keeps_date_values_and_exclusive_end():
    from datetime import date

    data = create_event(
        "all-day-series@family.invalid", date(2026, 10, 19), date(2026, 10, 20), "Daily", rrule={"FREQ": "DAILY", "COUNT": 4}
    )
    rid = date(2026, 10, 21)
    moved = edit_single(data, rid, date(2026, 10, 25), date(2026, 10, 26), summary="Shifted")
    [override] = override_of(moved, rid)
    assert override.to_ical().count(b"VALUE=DATE") >= 3 and override.decoded("DTEND") == date(2026, 10, 26)
    [override] = override_of(cancel_single(data, rid), rid)
    assert str(override["STATUS"]) == "CANCELLED" and override.decoded("DTEND") - override.decoded("DTSTART") == timedelta(days=1)
    # A date identity cannot address a timed series, and the reverse.
    with pytest.raises(CalendarError, match="invalid_occurrence_identity"):
        cancel_single(weekly(), date(2026, 10, 26))


# spec: ANYM-234
def test_cancel_keeps_the_times_of_an_already_moved_occurrence_and_duration_properties():
    source = fixture_bytes()
    moved = datetime(2026, 11, 16, 16, tzinfo=BERLIN)
    [override] = override_of(cancel_single(source, moved), moved)
    assert str(override["STATUS"]) == "CANCELLED" and override.decoded("DTSTART") == moved + timedelta(hours=2)
    durable = source.replace(b"DTEND;TZID=Europe/Berlin:20261019T170000", b"DURATION:PT1H")
    rid = datetime(2026, 11, 9, 16, tzinfo=BERLIN)
    [override] = override_of(cancel_single(durable, rid), rid)
    assert override.decoded("DTEND") == rid + timedelta(hours=1) and "DURATION" not in override
