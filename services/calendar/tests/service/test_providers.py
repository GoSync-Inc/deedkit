import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import httpx
import pytest
from family_calendar.errors import CalendarError
from tests.fakes import FakeGoogle, MemoryDAV


def test_google_conditional_patch_preserves_unrelated_fields():
    from family_calendar.providers.google.adapter import GoogleAdapter

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "event1", "etag": '"v2"'})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        adapter = GoogleAdapter("conn", client, lambda: "canary-token")
        await adapter.patch("calendar", "event1", {"summary": "Changed"}, '"v1"')
        await client.aclose()

    asyncio.run(run())
    assert requests[0].headers["if-match"] == '"v1"'
    assert requests[0].url.params["sendUpdates"] == "none"
    assert requests[0].content == b'{"summary":"Changed"}'


def test_google_create_uses_provider_native_insert_condition():
    from family_calendar.providers.google.adapter import GoogleAdapter

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "fcs012345", "etag": '"v1"'})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await GoogleAdapter("conn", client, lambda: "token").create("calendar", {"id": "fcs012345"})

    asyncio.run(run())
    assert b'"id":"fcs012345"' in requests[0].content
    assert "if-none-match" not in requests[0].headers


def test_google_definite_bad_request_is_distinct_from_unknown_write():
    from family_calendar.providers.google.adapter import GoogleAdapter

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(400, json={"error": {"message": "private"}})
            )
        ) as client:
            with pytest.raises(CalendarError, match="write_rejected"):
                await GoogleAdapter("conn", client, lambda: "token").create("calendar", {"id": "fcs012345"})

    asyncio.run(run())


def test_google_does_not_retry_ambiguous_mutation():
    from family_calendar.providers.google.adapter import GoogleAdapter

    count = 0

    def handle(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("canary secret should not escape")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter = GoogleAdapter("conn", client, lambda: "canary-token")
            with pytest.raises(CalendarError, match="unknown_result"):
                await adapter.create("calendar", {"id": "stable-id"})

    asyncio.run(run())
    assert count == 1


def test_google_paginated_window_is_never_marked_complete_early():
    from family_calendar.providers.google.adapter import GoogleAdapter

    def handle(request):
        if "pageToken" not in request.url.params:
            return httpx.Response(200, json={"items": [{"id": "first"}], "nextPageToken": "page2"})
        return httpx.Response(503, json={"error": {"message": "private canary"}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter = GoogleAdapter("conn", client, lambda: "token")
            with pytest.raises(CalendarError, match="provider_unavailable"):
                await adapter.window("calendar", "2026-10-19T08:00:00Z", "2026-10-19T10:00:00Z")

    asyncio.run(run())


def test_icloud_recurring_window_preserves_moved_exception_identity():
    from family_calendar.service.providers import normalize_icloud

    raw = b"""BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:weekly\r\nDTSTART:20261019T080000Z\r\nDTEND:20261019T090000Z\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\nSUMMARY:Lesson\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:weekly\r\nRECURRENCE-ID:20261026T080000Z\r\nDTSTART:20261026T100000Z\r\nDTEND:20261026T110000Z\r\nSUMMARY:Moved\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"""
    a = datetime(2026, 10, 19, tzinfo=timezone.utc)
    b = datetime(2026, 11, 3, tzinfo=timezone.utc)
    result = normalize_icloud(
        "cal",
        {"href": "https://p01-caldav.icloud.com/test/event.ics", "etag": '"v1"', "data": raw},
        a,
        b,
        "Asia/Makassar",
    )
    assert len(result["occurrences"]) == 3
    moved = next(x for x in result["occurrences"] if x["details"]["title"] == "Moved")
    assert moved["start"].hour == 10
    assert moved["details"]["recurring"] is True


def test_floating_event_degrades_availability():
    from family_calendar.service.providers import normalize_icloud

    raw = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:x\r\nDTSTART:20261019T160000\r\nDTEND:20261019T170000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    with pytest.raises(CalendarError, match="coverage_incomplete"):
        normalize_icloud(
            "cal",
            {"href": "https://p01-caldav.icloud.com/test/x.ics", "etag": '"v1"', "data": raw},
            datetime(2026, 10, 19, tzinfo=timezone.utc),
            datetime(2026, 10, 20, tzinfo=timezone.utc),
            "Asia/Makassar",
        )


def test_google_nonrecurring_event_ref_survives_time_move():
    from family_calendar.service.providers import normalize_google

    event = {
        "id": "stable",
        "etag": '"v1"',
        "start": {"dateTime": "2026-10-19T08:00:00Z"},
        "end": {"dateTime": "2026-10-19T09:00:00Z"},
    }
    first = normalize_google("cal", event, "Asia/Makassar")["occurrences"][0]["ref"]
    event["start"]["dateTime"] = "2026-10-19T10:00:00Z"
    event["end"]["dateTime"] = "2026-10-19T11:00:00Z"
    assert normalize_google("cal", event, "Asia/Makassar")["occurrences"][0]["ref"] == first


def test_icloud_nonrecurring_event_ref_survives_time_move():
    from family_calendar.service.providers import normalize_icloud

    a = datetime(2026, 10, 19, tzinfo=timezone.utc)
    b = datetime(2026, 10, 20, tzinfo=timezone.utc)
    raw = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:stable\r\nDTSTART:20261019T080000Z\r\nDTEND:20261019T090000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    resource = {"href": "https://p01-caldav.icloud.com/test/stable.ics", "etag": '"v1"', "data": raw}
    first = normalize_icloud("cal", resource, a, b, "Asia/Makassar")["occurrences"][0]["ref"]
    resource["data"] = raw.replace(b"T080000", b"T100000").replace(b"T090000", b"T110000")
    assert normalize_icloud("cal", resource, a, b, "Asia/Makassar")["occurrences"][0]["ref"] == first


def _series(rule, dtstart="20261001T000000Z", uid="series"):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTART:{dtstart}\r\nDURATION:PT1S\r\nRRULE:{rule}\r\nSUMMARY:Synthetic\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


def _resource(data, name="series"):
    return {"href": f"https://p01-caldav.icloud.com/test/{name}.ics", "etag": '"v1"', "data": data}


OCTOBER = (datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc))


@pytest.fixture
def expansions(monkeypatch):
    from recurring_ical_events.occurrence import Occurrence

    calls = []
    original = Occurrence.as_component

    def counted(self, keep):
        calls.append(self.start)
        return original(self, keep)

    monkeypatch.setattr(Occurrence, "as_component", counted)
    return calls


@pytest.mark.parametrize(
    "rule,dtstart",
    [
        ("FREQ=SECONDLY", "20261001T000000Z"),
        ("FREQ=MINUTELY", "20250101T000000Z"),
        ("FREQ=DAILY;BYHOUR=0,1,2,3,4,5,6,7,8,9;BYMINUTE=0,10,20,30,40,50;BYSECOND=0,30", "20240101T000000Z"),
    ],
)
def test_dense_rule_is_incomplete_without_expansion(monkeypatch, rule, dtstart):
    from family_calendar.service import providers

    def refuse(*args, **kwargs):
        raise AssertionError("dense rule must not be expanded")

    monkeypatch.setattr(providers.recurring_ical_events, "of", refuse)
    with pytest.raises(CalendarError, match="coverage_incomplete"):
        providers.normalize_icloud("cal", _resource(_series(rule, dtstart)), *OCTOBER, "Asia/Makassar")


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=HOURLY;INTERVAL=5",
        "FREQ=DAILY;BYHOUR=9,17;BYMINUTE=0,30",
        "FREQ=WEEKLY;BYDAY=MO,WE,FR",
        "FREQ=WEEKLY;INTERVAL=2;BYDAY=SA;BYMONTH=6,7",
        "FREQ=MONTHLY;BYDAY=1MO,-1FR",
        "FREQ=MONTHLY;BYMONTHDAY=1,15,31",
        "FREQ=MONTHLY;INTERVAL=2;COUNT=7",
        "FREQ=YEARLY;BYMONTH=11;BYDAY=4TH",
        "FREQ=YEARLY;BYWEEKNO=1,20;BYDAY=MO,TU",
        "FREQ=YEARLY;BYYEARDAY=1,100,-1",
        "FREQ=DAILY;UNTIL=20240229",
    ],
)
def test_rule_instance_bound_is_an_upper_bound(rule):
    from dateutil.rrule import rrulestr
    from icalendar import Calendar
    from family_calendar.service.providers import rule_instances

    start = datetime(2020, 1, 1, 9, tzinfo=timezone.utc)
    event = Calendar.from_ical(_series(rule, "20200101T090000Z")).walk("VEVENT")[0]
    text = rule.replace("UNTIL=20240229", "UNTIL=20240229T235959Z")
    actual = sum(1 for _ in rrulestr(text, dtstart=start).between(start, OCTOBER[1], inc=True))
    assert actual <= rule_instances(event, OCTOBER[1], "UTC")


def test_in_window_minutely_series_stops_at_event_limit(expansions):
    from family_calendar.service.providers import EVENT_LIMIT, normalize_icloud

    # 44 640 in-window instances fit the rule budget; expansion itself is cut off lazily.
    with pytest.raises(CalendarError, match="coverage_incomplete"):
        normalize_icloud("cal", _resource(_series("FREQ=MINUTELY")), *OCTOBER, "Asia/Makassar")
    assert len(expansions) == EVENT_LIMIT + 1


def test_bounded_dense_rules_and_long_running_series_still_expand():
    from family_calendar.service.providers import normalize_icloud

    def count(rule, dtstart):
        resource = _resource(_series(rule, dtstart))
        return len(normalize_icloud("cal", resource, *OCTOBER, "Asia/Makassar")["occurrences"])

    assert count("FREQ=SECONDLY;COUNT=10", "20261002T000000Z") == 10
    assert count("FREQ=MINUTELY;UNTIL=20250101T010000Z", "20250101T000000Z") == 0
    assert count("FREQ=WEEKLY;BYDAY=MO,WE", "20000103T080000Z") == 8


def test_hourly_series_fits_a_week_but_not_the_maximum_window():
    from family_calendar.service.providers import normalize_icloud

    resource = _resource(_series("FREQ=HOURLY", "20260101T000000Z"))
    week = (OCTOBER[0], OCTOBER[0] + timedelta(days=7))
    assert len(normalize_icloud("cal", resource, *week, "Asia/Makassar")["occurrences"]) == 168
    with pytest.raises(CalendarError, match="coverage_incomplete"):
        normalize_icloud("cal", resource, *OCTOBER, "Asia/Makassar")


def test_calendar_window_budget_is_shared_by_resources(expansions):
    from family_calendar.service.providers import EVENT_LIMIT, Providers

    class Adapter:
        async def read_window(self, collection, start, end):
            resources = [_resource(_series("FREQ=HOURLY;COUNT=300", uid=f"s{i}"), f"s{i}") for i in range(4)]
            return {"resources": resources}

    providers = Providers(None, None)
    providers.adapters["conn"] = Adapter()
    cal = {"connection_id": "conn", "provider": "icloud", "target": "t", "ref": "cal", "timezone": "UTC"}
    with pytest.raises(CalendarError, match="coverage_incomplete"):
        asyncio.run(providers.read_window(cal, *OCTOBER))
    assert len(expansions) == EVENT_LIMIT + 1


def test_caldav_window_is_sent_in_utc_whatever_the_session_zone():
    from zoneinfo import ZoneInfo
    from family_calendar.service.providers import Providers

    sent = []

    class Adapter:
        async def read_window(self, collection, start, end):
            sent.append((start, end))
            return {"resources": []}

    providers = Providers(None, None)
    providers.adapters["conn"] = Adapter()
    cal = {"connection_id": "conn", "provider": "icloud", "target": "t", "ref": "cal", "timezone": "UTC"}
    local = ZoneInfo("Asia/Makassar")
    window = (datetime(2026, 10, 19, 8, tzinfo=local), datetime(2026, 10, 20, 8, tzinfo=local))
    assert asyncio.run(providers.read_window(cal, *window)) == []
    assert sent == [("20261019T000000Z", "20261020T000000Z")]


def _event(times):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:point\r\n"
        + times
        + "SUMMARY:Reminder\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


DAY = (datetime(2026, 10, 19, tzinfo=timezone.utc), datetime(2026, 10, 20, tzinfo=timezone.utc))


@pytest.mark.parametrize(
    "times",
    [
        "DTSTART:20261019T080000Z\r\n",
        "DTSTART:20261019T080000Z\r\nDTEND:20261019T080000Z\r\nTRANSP:OPAQUE\r\n",
        "DTSTART:20261019T080000Z\r\nDURATION:PT0S\r\n",
    ],
)
def test_icloud_zero_length_event_is_a_non_blocking_instant(times):
    from family_calendar.service.providers import normalize_icloud

    [row] = normalize_icloud("cal", _resource(_event(times)), *DAY, "Asia/Makassar")["occurrences"]
    assert row["start"] == row["end"] == datetime(2026, 10, 19, 8, tzinfo=timezone.utc)
    assert row["busy"] is False
    assert row["details"]["instant"] is True


def test_instant_belongs_to_the_half_open_window_that_contains_it():
    from family_calendar.service.providers import normalize_icloud

    def rows(times):
        return normalize_icloud("cal", _resource(_event(times)), *DAY, "UTC")["occurrences"]

    assert len(rows("DTSTART:20261019T000000Z\r\n")) == 1
    assert rows("DTSTART:20261020T000000Z\r\n") == []
    assert rows("DTSTART:20261018T235959Z\r\n") == []


@pytest.mark.parametrize(
    "times",
    [
        "DTSTART:20261019T090000Z\r\nDTEND:20261019T080000Z\r\n",
        "DTSTART:20261019T090000Z\r\nDURATION:-PT1H\r\n",
        "DTSTART:20261019T090000Z\r\nDTEND:20261019T080000Z\r\nRRULE:FREQ=DAILY;COUNT=3\r\n",
    ],
)
def test_icloud_reversed_event_is_reported_as_provider_data(times):
    from family_calendar.service.providers import normalize_icloud

    with pytest.raises(CalendarError, match="invalid_provider_event_time"):
        normalize_icloud("cal", _resource(_event(times)), *DAY, "Asia/Makassar")


def test_google_zero_length_and_reversed_events():
    from family_calendar.service.providers import normalize_google

    event = {
        "id": "point",
        "etag": '"v1"',
        "summary": "Pay\x00ment \ud800due",
        "start": {"dateTime": "2026-10-19T08:00:00Z"},
        "end": {"dateTime": "2026-10-19T16:00:00+08:00"},
    }
    [row] = normalize_google("cal", event, "Asia/Makassar")["occurrences"]
    assert row["start"] == row["end"] and row["busy"] is False and row["details"]["instant"] is True
    assert row["details"]["title"] == "Payment ?due"
    event["end"] = {"dateTime": "2026-10-19T07:59:00Z"}
    with pytest.raises(CalendarError, match="invalid_provider_event_time"):
        normalize_google("cal", event, "Asia/Makassar")


# --- ANYM-234: one occurrence of a series and all-day events -------------------------------------

GOOGLE_CAL = {
    "ref": "cal-g",
    "connection_id": "conn-g",
    "provider": "google",
    "target": "calendar-id",
    "timezone": "Asia/Makassar",
}
ICLOUD_CAL = {
    "ref": "cal-i",
    "connection_id": "conn-i",
    "provider": "icloud",
    "target": "https://p01-caldav.icloud.com/test/cal/",
    "timezone": "Europe/Berlin",
}
SERIES_URL = ICLOUD_CAL["target"] + "series.ics"
FIXTURES = Path(__file__).parents[1] / "fixtures" / "ics"


def google_instance(day, **extra):
    start = f"2026-10-{day}T08:00:00Z"
    return {
        "id": f"series_202610{day}T080000Z",
        "etag": f'"i{day}"',
        "summary": "Lesson",
        "recurringEventId": "series",
        "originalStartTime": {"dateTime": start},
        "start": {"dateTime": start},
        "end": {"dateTime": f"2026-10-{day}T09:00:00Z"},
        **extra,
    }


def google_series(**extra):
    master = {
        "id": "series",
        "etag": '"m1"',
        "summary": "Lesson",
        "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=3"],
        "start": {"dateTime": "2026-10-19T08:00:00Z"},
        "end": {"dateTime": "2026-10-19T09:00:00Z"},
    }
    return [master, google_instance(19), google_instance(26), *extra.get("more", [])]


def occurrence_step(action, original="2026-10-19T08:00:00+00:00", target=None, etag=None, ref=None, **event):
    from family_calendar.service.providers import series_ref

    day = original[8:10]
    step = {
        "action": action,
        "ordinal": 0,
        "target_id": target or f"series_202610{day}T080000Z",
        "expected_etag": etag or f'"i{day}"',
        "occurrence": {"series_ref": ref or series_ref("cal-g", "series"), "original_start": original},
    }
    if action != "delete":
        step["event"] = {
            "title": "Moved",
            "start": "2026-10-19T10:00:00+00:00",
            "end": "2026-10-19T11:00:00+00:00",
            "timezone": "UTC",
            **event,
        }
    return step


def google_world(events=None, drop_response=False):
    from family_calendar.providers.google.adapter import GoogleAdapter
    from family_calendar.service.providers import Providers

    server = FakeGoogle(events if events is not None else google_series())
    server.drop_response = drop_response
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    providers = Providers(None, None)
    providers.adapters["conn-g"] = GoogleAdapter("conn-g", client, lambda: "canary-token")
    return providers, server


def caldav_world(data=None):
    from family_calendar.providers.caldav.adapter import CalDAVAdapter
    from family_calendar.providers.caldav.transport import DAVTransport
    from family_calendar.service.providers import Providers

    server = MemoryDAV()
    if data is not None:
        server.resources[SERIES_URL] = (data, '"v1"')
    transport = DAVTransport("a", "secret", client=httpx.AsyncClient(transport=httpx.MockTransport(server)))
    providers = Providers(None, None)
    providers.adapters["conn-i"] = CalDAVAdapter("conn-i", transport)
    return providers, server


def weekly_exceptions():
    return (FIXTURES / "weekly-exceptions.ics").read_bytes()


def caldav_step(action, original, **event):
    from family_calendar.service.providers import series_ref

    step = {
        "action": action,
        "ordinal": 0,
        "target_id": SERIES_URL,
        "expected_etag": '"v1"',
        "occurrence": {"series_ref": series_ref("cal-i", SERIES_URL), "original_start": original},
    }
    if action != "delete":
        step["event"] = {
            "title": "Moved",
            "start": "2026-11-09T14:00:00+00:00",
            "end": "2026-11-09T15:00:00+00:00",
            "timezone": "Europe/Berlin",
            **event,
        }
    return step


async def apply_step(providers, cal, step):
    intent = await providers.prepare(cal, step)
    await providers.mutate(cal, intent)
    return intent, await providers.verify(cal, intent)


def caldav_agenda(data, start="2026-10-19", end="2026-12-31"):
    from family_calendar.service.providers import normalize_icloud

    resource = {"href": SERIES_URL, "etag": '"v1"', "data": data}
    a, b = (datetime.fromisoformat(x).replace(tzinfo=timezone.utc) for x in (start, end))
    return {
        o["details"]["original_start"]: o
        for o in normalize_icloud("cal-i", resource, a, b, "Europe/Berlin")["occurrences"]
    }


# spec: ANYM-234
@pytest.mark.asyncio
async def test_google_occurrence_update_and_cancel_touch_only_the_instance():
    providers, server = google_world()
    before = json.dumps(server.events, sort_keys=True)
    others = {k: json.dumps(v, sort_keys=True) for k, v in server.events.items() if k != "series_20261019T080000Z"}
    intent, receipt = await apply_step(providers, GOOGLE_CAL, occurrence_step("update"))
    assert receipt["verified"] is True
    [patch] = [r for r in server.requests if r.method == "PATCH"]
    assert patch.url.path.endswith("/events/series_20261019T080000Z")
    assert patch.headers["if-match"] == '"i19"' and patch.url.params["sendUpdates"] == "none"
    body = json.loads(patch.content)
    assert body["summary"] == "Moved" and body["start"]["dateTime"] == "2026-10-19T10:00:00+00:00"
    assert not {"recurrence", "recurringEventId", "attendees", "id"} & set(body)
    assert server.events["series_20261019T080000Z"]["summary"] == "Moved"
    # Read-back: every other instance and the master are untouched.
    assert {k: json.dumps(v, sort_keys=True) for k, v in server.events.items() if k != "series_20261019T080000Z"} == others
    assert json.dumps(server.events, sort_keys=True) != before
    # Cancel the second instance: DELETE of that instance only, conditional, without notifications.
    cancel = occurrence_step("delete", "2026-10-26T08:00:00+00:00")
    intent, receipt = await apply_step(providers, GOOGLE_CAL, cancel)
    assert receipt["deleted"] is True and intent["action"] == "delete"
    [delete] = [r for r in server.requests if r.method == "DELETE"]
    assert delete.url.path.endswith("/events/series_20261026T080000Z")
    assert delete.headers["if-match"] == '"i26"' and delete.url.params["sendUpdates"] == "none"
    assert server.events["series_20261026T080000Z"]["status"] == "cancelled"
    assert server.events["series"]["etag"] == '"m1"' and "series_20261019T080000Z" in server.events
    assert {r.method for r in server.requests} <= {"GET", "PATCH", "DELETE"}


# spec: ANYM-234
@pytest.mark.asyncio
async def test_google_series_scope_and_guests_stay_unverified_at_write_time():
    guest = google_instance(26, attendees=[{"email": "someone@example.invalid"}])
    providers, server = google_world([google_series()[0], google_instance(19), guest])
    master = occurrence_step("update", target="series", etag='"m1"')
    guests = occurrence_step("update", "2026-10-26T08:00:00+00:00")
    # An instance reached without an occurrence identity is also refused: it is a series member.
    plain = {k: v for k, v in occurrence_step("update").items() if k != "occurrence"}
    for step in (master, guests, plain):
        with pytest.raises(CalendarError, match="capability_unverified"):
            await providers.prepare(GOOGLE_CAL, step)
    # An identity that names another occurrence than the instance is a conflict, not a write.
    other = occurrence_step("update", "2026-10-26T08:00:00+00:00", target="series_20261019T080000Z")
    with pytest.raises(CalendarError, match="version_conflict"):
        await providers.prepare(GOOGLE_CAL, other)
    assert [r.method for r in server.requests if r.method != "GET"] == []


# spec: ANYM-234
@pytest.mark.asyncio
async def test_caldav_series_scope_and_guests_stay_unverified_at_write_time():
    providers, server = caldav_world(weekly_exceptions())
    rid = "2026-11-09T16:00:00+01:00"
    plain = {k: v for k, v in caldav_step("update", rid).items() if k != "occurrence"}
    with pytest.raises(CalendarError, match="capability_unverified"):
        await providers.prepare(ICLOUD_CAL, plain)
    for marker in (b"ATTENDEE:mailto:someone@example.invalid", b"ORGANIZER:mailto:someone@example.invalid"):
        data = weekly_exceptions().replace(b"RRULE:FREQ=WEEKLY;COUNT=6", b"RRULE:FREQ=WEEKLY;COUNT=6\r\n" + marker)
        guests, _ = caldav_world(data)
        with pytest.raises(CalendarError, match="capability_unverified"):
            await guests.prepare(ICLOUD_CAL, caldav_step("update", rid))
        # ... also when only a stored exception has guests.
        data = weekly_exceptions().replace(b"STATUS:CANCELLED", b"STATUS:CANCELLED\r\n" + marker)
        guests, _ = caldav_world(data)
        with pytest.raises(CalendarError, match="capability_unverified"):
            await guests.prepare(ICLOUD_CAL, caldav_step("update", rid))
    ranged = weekly_exceptions().replace(b"RECURRENCE-ID;TZID=", b"RECURRENCE-ID;RANGE=THISANDFUTURE;TZID=", 1)
    sliced, _ = caldav_world(ranged)
    with pytest.raises(CalendarError, match="capability_unverified"):
        await sliced.prepare(ICLOUD_CAL, caldav_step("update", rid))
    assert server.puts == 0


# spec: ANYM-234
@pytest.mark.asyncio
async def test_occurrence_edit_is_idempotent_by_identity_and_conditional():
    # CalDAV: an occurrence moved before keeps its recurrence identity; no second override appears.
    providers, server = caldav_world(weekly_exceptions())
    moved = "2026-11-16T16:00:00+01:00"  # fixture override: moved to 18:00
    step = caldav_step("update", moved, start="2026-11-16T19:00:00+00:00", end="2026-11-16T20:00:00+00:00")
    _, receipt = await apply_step(providers, ICLOUD_CAL, step)
    assert receipt["verified"] is True and server.puts == 1
    data = server.resources[SERIES_URL][0]
    assert data.count(b"RECURRENCE-ID") == weekly_exceptions().count(b"RECURRENCE-ID")
    assert caldav_agenda(data)[moved]["start"] == datetime(2026, 11, 16, 19, tzinfo=timezone.utc)
    # The same edit again, with the new etag, is the same override again.
    step = dict(step, expected_etag=server.resources[SERIES_URL][1])
    await apply_step(providers, ICLOUD_CAL, step)
    assert server.resources[SERIES_URL][0].count(b"RECURRENCE-ID") == data.count(b"RECURRENCE-ID")
    # A stale etag, a vanished resource and an identity outside the series write nothing.
    puts = server.puts
    with pytest.raises(CalendarError, match="version_conflict"):
        await providers.prepare(ICLOUD_CAL, caldav_step("update", moved))
    server.resources.pop(SERIES_URL)
    with pytest.raises(CalendarError, match="version_conflict"):
        await providers.prepare(ICLOUD_CAL, caldav_step("delete", moved))
    providers, server = caldav_world(weekly_exceptions())
    for original in ("2026-10-26T16:00:00+01:00", "2026-11-10T16:00:00+01:00", "2026-11-09T17:00:00+01:00"):
        with pytest.raises(CalendarError, match="invalid_occurrence_identity"):
            await providers.prepare(ICLOUD_CAL, caldav_step("delete", original))
    assert server.puts == 0 and puts == 2
    # Google: the instance ID is the identity; a second edit patches the same instance.
    providers, server = google_world()
    await apply_step(providers, GOOGLE_CAL, occurrence_step("update"))
    instance = "series_20261019T080000Z"
    etag = server.events[instance]["etag"]
    await apply_step(providers, GOOGLE_CAL, occurrence_step("update", etag=etag, title="Moved again"))
    assert sorted(server.events) == ["series", instance, "series_20261026T080000Z"]
    assert server.events[instance]["summary"] == "Moved again"
    patches = lambda: len([r for r in server.requests if r.method == "PATCH"])  # noqa: E731
    assert patches() == 2
    # A stale etag is refused before a write, and a 412 from the server writes nothing.
    with pytest.raises(CalendarError, match="version_conflict"):
        await providers.prepare(GOOGLE_CAL, occurrence_step("update", etag=etag))
    intent = await providers.prepare(GOOGLE_CAL, occurrence_step("update", etag=server.events[instance]["etag"]))
    server.events[instance]["etag"] = '"changed-by-someone-else"'
    with pytest.raises(CalendarError, match="version_conflict"):
        await providers.mutate(GOOGLE_CAL, intent)
    assert server.events[instance]["summary"] == "Moved again"
    # A cancelled instance no longer exists as an occurrence.
    await apply_step(providers, GOOGLE_CAL, occurrence_step("delete", etag='"changed-by-someone-else"'))
    with pytest.raises(CalendarError, match="version_conflict"):
        await providers.prepare(GOOGLE_CAL, occurrence_step("update", etag=server.events[instance]["etag"]))


# spec: ANYM-234
@pytest.mark.asyncio
async def test_all_day_create_roundtrips_as_date_values():
    from family_calendar.service.providers import normalize_google, normalize_icloud
    from family_calendar.providers.caldav.codec import parse

    event = {"title": "Holiday", "all_day": True, "start": "2026-10-19", "end": "2026-10-20"}
    # Google: DATE values, no dateTime or timeZone.
    providers, server = google_world([])
    step = {"action": "create", "ordinal": 0, "target_id": "fcsallday", "event": event}
    intent, receipt = await apply_step(providers, GOOGLE_CAL, step)
    assert receipt["verified"] is True
    assert intent["desired"]["start"] == {"date": "2026-10-19"} and intent["desired"]["end"] == {"date": "2026-10-20"}
    [row] = normalize_google("cal-g", server.events["fcsallday"], "Asia/Makassar")["occurrences"]
    assert row["details"]["all_day"] is True
    assert (row["start"], row["end"]) == (
        datetime(2026, 10, 18, 16, tzinfo=timezone.utc),
        datetime(2026, 10, 19, 16, tzinfo=timezone.utc),
    )
    # CalDAV: DTSTART/DTEND;VALUE=DATE with the exclusive end, read back as one all_day occurrence.
    providers, server = caldav_world()
    step = {"action": "create", "ordinal": 0, "target_id": "fcsallday", "event": event}
    intent, receipt = await apply_step(providers, ICLOUD_CAL, step)
    assert receipt["verified"] is True
    data = next(iter(server.resources.values()))[0]
    assert b"DTSTART;VALUE=DATE:20261019" in data and b"DTEND;VALUE=DATE:20261020" in data
    assert b"TZID" not in data and len(parse(data).walk("VEVENT")) == 1
    resource = {"href": SERIES_URL, "etag": '"v1"', "data": data}
    window = (datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc))
    [row] = normalize_icloud("cal-i", resource, *window, "Asia/Makassar")["occurrences"]
    assert row["details"]["all_day"] is True
    assert (row["start"], row["end"]) == (
        datetime(2026, 10, 18, 16, tzinfo=timezone.utc),
        datetime(2026, 10, 19, 16, tzinfo=timezone.utc),
    )


# spec: ANYM-234
@pytest.mark.asyncio
async def test_all_day_update_is_written_as_dates_and_conversion_clears_the_other_form():
    # Google all-day stays all-day.
    day = {"id": "day", "etag": '"d1"', "summary": "Holiday", "start": {"date": "2026-10-19"}, "end": {"date": "2026-10-20"}}
    timed = {"id": "timed", "etag": '"t1"', "summary": "Dentist", "start": {"dateTime": "2026-10-19T08:00:00Z", "timeZone": "UTC"}, "end": {"dateTime": "2026-10-19T09:00:00Z", "timeZone": "UTC"}}
    providers, server = google_world([day, timed])
    new = {"all_day": True, "start": "2026-10-21", "end": "2026-10-22", "title": "Holiday"}
    _, receipt = await apply_step(providers, GOOGLE_CAL, {"action": "update", "ordinal": 0, "target_id": "day", "expected_etag": '"d1"', "event": new})
    assert receipt["verified"] and server.events["day"]["start"] == {"date": "2026-10-21"}
    # timed -> all-day: the dateTime form is removed, not left beside the date.
    _, receipt = await apply_step(providers, GOOGLE_CAL, {"action": "update", "ordinal": 0, "target_id": "timed", "expected_etag": '"t1"', "event": new})
    assert receipt["verified"] and server.events["timed"]["start"] == {"date": "2026-10-21"}
    # all-day -> timed.
    etag = server.events["day"]["etag"]
    back = {"start": "2026-10-21T10:00:00+00:00", "end": "2026-10-21T11:00:00+00:00", "timezone": "UTC"}
    _, receipt = await apply_step(providers, GOOGLE_CAL, {"action": "update", "ordinal": 0, "target_id": "day", "expected_etag": etag, "event": back})
    assert receipt["verified"] and "date" not in server.events["day"]["start"]
    # CalDAV: DATE stays DATE with an exclusive end; conversion rewrites both properties.
    ics = (FIXTURES / "all-day.ics").read_bytes()
    providers, server = caldav_world(ics)
    step = {"action": "update", "ordinal": 0, "target_id": SERIES_URL, "expected_etag": '"v1"', "event": dict(new, title="Renamed")}
    _, receipt = await apply_step(providers, ICLOUD_CAL, step)
    data = server.resources[SERIES_URL][0]
    assert receipt["verified"] and b"DTSTART;VALUE=DATE:20261021" in data and b"DTEND;VALUE=DATE:20261022" in data
    step = dict(step, expected_etag=server.resources[SERIES_URL][1], event=back)
    _, receipt = await apply_step(providers, ICLOUD_CAL, step)
    data = server.resources[SERIES_URL][0]
    assert receipt["verified"] and b"VALUE=DATE" not in data and b"DTSTART:20261021T100000Z" in data


# spec: ANYM-234
@pytest.mark.asyncio
async def test_caldav_occurrence_cancelled_elsewhere_is_a_conflict_not_a_resurrection():
    providers, server = caldav_world(weekly_exceptions())
    gone = "2026-11-02T16:00:00+01:00"  # a fixture override with STATUS:CANCELLED, the etag is current
    for action in ("update", "delete"):
        with pytest.raises(CalendarError, match="version_conflict"):
            await providers.prepare(ICLOUD_CAL, caldav_step(action, gone))
    cancelled_series = weekly_exceptions().replace(b"BEGIN:VEVENT\r\n", b"BEGIN:VEVENT\r\nSTATUS:CANCELLED\r\n", 1)
    providers, server = caldav_world(cancelled_series)
    for action in ("update", "delete"):
        with pytest.raises(CalendarError, match="version_conflict"):
            await providers.prepare(ICLOUD_CAL, caldav_step(action, "2026-11-09T16:00:00+01:00"))
    assert server.puts == 0


# spec: ANYM-234
@pytest.mark.asyncio
async def test_caldav_occurrence_of_a_series_of_instants_can_be_cancelled():
    instants = weekly_exceptions().replace(b"DTEND;TZID=Europe/Berlin:20261019T170000\r\n", b"")
    providers, server = caldav_world(instants)
    rid = "2026-11-09T16:00:00+01:00"
    assert caldav_agenda(instants)[rid]["details"]["instant"] is True
    intent, receipt = await apply_step(providers, ICLOUD_CAL, caldav_step("delete", rid))
    assert intent["action"] == "update" and receipt["verified"] is True and server.puts == 1
    assert rid not in caldav_agenda(server.resources[SERIES_URL][0])


# spec: ANYM-234
@pytest.mark.asyncio
async def test_google_instance_identity_that_cannot_be_compared_is_a_definite_error_before_any_write():
    # A timestamp outside the range of datetime arithmetic is not an identity to publish ...
    from family_calendar.service.providers import normalize_google

    extreme = google_instance(19)
    extreme["originalStartTime"] = {"dateTime": "0001-01-01T00:00:00+14:00"}
    for identity in (extreme["originalStartTime"], {"dateTime": 5}, {"dateTime": ["x"]}, {"date": "not-a-date"}):
        [row] = normalize_google("cal-g", dict(extreme, originalStartTime=identity), "UTC")["occurrences"]
        assert "original_start" not in row["details"] and "series_ref" not in row["details"]
    # ... and one that reaches the guard through a changed provider event is a CalendarError, not an exception.
    providers, server = google_world([google_series()[0], extreme, google_instance(26)])
    step = occurrence_step("update", "2026-10-19T08:00:00+00:00")
    with pytest.raises(CalendarError):
        await providers.prepare(GOOGLE_CAL, step)
    assert [r.method for r in server.requests if r.method != "GET"] == []
