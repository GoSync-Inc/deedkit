"""A stored step's `before` stays display data on the provider variants test_plan_before_is_display_only leaves open
(ANYM-256): CalDAV single events, CalDAV and Google occurrences of a series, and Google series scopes.

Each case runs the same write twice on twin synthetic families, each in its own test schema, with fresh in-memory
providers behind the real adapters, Providers and Worker: once as planned, and once with every step's `before` replaced
by a plausible other preview (another event, time, title, text, etag, series identity and shape flags) while every
routing, write, etag, occurrence and series field stays as planned. Plans come from the real plan path, or for series
scopes from calendar_schedule. A CalDAV body is compared without the properties the codec itself marks volatile
(`codec.VOLATILE`: DTSTAMP and LAST-MODIFIED are set at write time); its intent's semantic hash, which excludes the same
properties, is compared too. No live account, network or provider session."""
import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from psycopg.types.json import Jsonb

from family_calendar.providers.caldav.adapter import CalDAVAdapter
from family_calendar.providers.caldav.codec import VOLATILE
from family_calendar.providers.caldav.transport import DAVTransport
from family_calendar.providers.google.adapter import GoogleAdapter
from family_calendar.service.core import FamilyService
from family_calendar.service.providers import Providers, normalize_google, normalize_icloud
from family_calendar.service.store import Store
from family_calendar.service.worker import Worker
from tests.fakes import FakeGoogle, MemoryDAV
from tests.service.test_policy import SCOPES
from tests.service.test_providers import ICLOUD_CAL, SERIES_URL, google_series, weekly_exceptions
from tests.service.test_schedule_intent import setup as schedule_setup
from tests.service.test_schedule_series import publish as series_publish, series_events, start as schedule_start

SINGLE_URL = ICLOUD_CAL["target"] + "swim.ics"
SINGLE = (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//synthetic//EN\r\nBEGIN:VEVENT\r\nUID:swim@example.invalid\r\n"
          b"DTSTAMP:20261001T000000Z\r\nDTSTART:20261020T100000Z\r\nDTEND:20261020T110000Z\r\nSUMMARY:Swim\r\n"
          b"LOCATION:Pool\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
# Another plausible preview: another event's time, text and etag, another series identity, and every shape flag.
POISON = {"start": "2026-10-21T07:00:00+00:00", "end": "2026-10-21T08:15:00+00:00", "title": "Dentist",
          "location": "Clinic", "description": "Bring forms", "etag": '"stale"', "all_day": False, "instant": True,
          "recurring": True, "guests": True, "special": True, "series_ref": "series-elsewhere",
          "original_start": "2026-11-16T13:00:00+00:00"}


@pytest.fixture
def twins(store):
    other = Store(store.dsn, schema="test_" + uuid4().hex)
    other.migrate()
    try:
        yield store, other
    finally:
        try:
            other.drop_test_schema()
        finally:
            other.close()


def family(s, connection, provider, ref, target, zone):
    s.person("adult-a", "Adult A")
    s.group("family", "Family")
    s.member("family", "adult-a", "group_admin")
    s.connection(connection, "adult-a", provider)
    s.calendar(ref, connection, target, "Family", zone, "write")
    s.grant("family", "adult-a", ref, SCOPES, "details")
    s.write_opt_in(ref, True)
    return s.agent("variants", "family", "adult-a", SCOPES, [ref])


def caldav(resources):
    server, seen = MemoryDAV(), []
    server.resources.update(resources)

    def handle(request):
        seen.append(request)
        return server(request)
    providers = Providers(None, None)
    providers.adapters["conn-i"] = CalDAVAdapter(
        "conn-i", DAVTransport("a", "secret", client=httpx.AsyncClient(transport=httpx.MockTransport(handle))))
    return providers, server, seen


def google(events=None, connection="conn-g"):
    server = FakeGoogle(google_series() if events is None else events)
    providers = Providers(None, None)
    providers.adapters[connection] = GoogleAdapter(
        connection, httpx.AsyncClient(transport=httpx.MockTransport(server)), lambda: "canary-token")
    return providers, server, server.requests


def stable(body):
    """An ICS body without the properties the codec marks volatile; any other body as it was sent."""
    if not body.startswith(b"BEGIN:VCALENDAR"):
        return json.loads(body) if body else None
    return [line for line in body.decode().split("\r\n") if line.split(":")[0].split(";")[0] not in VOLATILE]


def wire(seen):
    return [(r.method, r.url.path, r.headers.get("if-match"), r.headers.get("if-none-match"), stable(r.content))
            for r in seen if r.method not in ("GET", "PROPFIND", "REPORT")]


def outcome(s, operation_id, seen, final):
    steps = s.query("SELECT ordinal,state,error,intent,receipt FROM operation_steps WHERE operation_id=%s "
                    "ORDER BY ordinal", (operation_id,))
    for step in steps:  # the encoded ICS carries the write time; its semantic hash and every other field are compared
        (step["intent"] or {}).pop("desired", None)
    return {"wire": wire(seen), "final": final(), "steps": steps,
            "operation": s.one("SELECT state,error FROM operations WHERE id=%s", (operation_id,)),
            "resources": s.query("SELECT ref,target,etag FROM resources ORDER BY ref")}


def poisoned(s, plan_id):
    """Every stored step that has a `before` gets the other preview; nothing else in the plan changes."""
    payload = s.one("SELECT payload FROM plans WHERE id=%s", (plan_id,))["payload"]
    kept = [{k: v for k, v in step.items() if k != "before"} for step in payload["steps"]]
    assert any("before" in step for step in payload["steps"])
    payload["steps"] = [dict(step, before={**step["before"], **POISON}) if "before" in step else step
                        for step in payload["steps"]]
    s.query("UPDATE plans SET payload=%s WHERE id=%s", (Jsonb(payload), plan_id))
    return kept


def planned_write(s, variant, action):
    """(key, plan_id, providers, seen, final) for one planned update or delete of a published event."""
    if variant == "google-occurrence":
        key = family(s, "conn-g", "google", "cal-g", "calendar-id", "Asia/Makassar")
        providers, server, seen = google()
        rows = [normalize_google("cal-g", e, "Asia/Makassar") for e in server.events.values() if e["id"] != "series"]
        s.publish_window("cal-g", "2026-10-19T00:00:00Z", "2026-11-02T00:00:00Z", rows, "complete")
        ref, window = "cal-g", ("2026-10-19T00:00:00Z", "2026-10-20T00:00:00Z")
        event = {"title": "Moved once", "start": "2026-10-19T10:00:00Z", "end": "2026-10-19T11:00:00Z",
                 "timezone": "UTC"}
        final = lambda: server.events  # noqa: E731
    else:
        key = family(s, "conn-i", "icloud", ICLOUD_CAL["ref"], ICLOUD_CAL["target"], "Europe/Berlin")
        url, data = (SINGLE_URL, SINGLE) if variant == "caldav-single" else (SERIES_URL, weekly_exceptions())
        providers, server, seen = caldav({url: (data, '"v1"')})
        a, b = datetime(2026, 10, 19, tzinfo=timezone.utc), datetime(2026, 12, 31, tzinfo=timezone.utc)
        s.publish_window(ICLOUD_CAL["ref"], a, b, [normalize_icloud(
            ICLOUD_CAL["ref"], {"href": url, "etag": '"v1"', "data": data}, a, b, "Europe/Berlin")], "complete")
        ref = ICLOUD_CAL["ref"]
        window = (("2026-10-20T00:00:00Z", "2026-10-21T00:00:00Z") if variant == "caldav-single"
                  else ("2026-11-09T00:00:00Z", "2026-11-10T00:00:00Z"))
        event = ({"title": "Swim (moved)", "start": "2026-10-20T12:00:00Z", "end": "2026-10-20T13:30:00Z",
                  "timezone": "Europe/Berlin"} if variant == "caldav-single" else
                 {"title": "Moved once", "start": "2026-11-09T14:00:00Z", "end": "2026-11-09T15:00:00Z",
                  "timezone": "Europe/Berlin"})
        final = lambda: {k: stable(v[0]) for k, v in server.resources.items()}  # noqa: E731
    service = FamilyService(s)
    [target] = [e for e in service.agenda(key, [ref], *window)["events"]
                if e["recurring"] == (variant != "caldav-single")][:1]
    request = {"action": action, "calendar_ref": ref, "event_ref": target["event_ref"]}
    if action == "update":
        request["event"] = event
    return key, service.plan(key, [request])["plan_id"], providers, seen, final


def run(s, key, plan_id, providers, seen, final):
    operation_id = FamilyService(s).apply(key, plan_id, "variants-request-1")["operation_id"]
    asyncio.run(Worker(s, providers).run_operation(operation_id))
    return outcome(s, operation_id, seen, final)


# spec: ANYM-256
@pytest.mark.parametrize("action", ["update", "delete"])
@pytest.mark.parametrize("variant", ["caldav-single", "caldav-occurrence", "google-occurrence"])
def test_a_changed_before_writes_what_the_stored_provider_variant_writes(twins, variant, action):
    as_stored, changed = twins
    key, plan_id, providers, seen, final = planned_write(as_stored, variant, action)
    steps = as_stored.one("SELECT payload FROM plans WHERE id=%s", (plan_id,))["payload"]["steps"]
    expected = run(as_stored, key, plan_id, providers, seen, final)
    # Positive control: the plan as stored really writes, once and conditionally, against the published etag.
    assert expected["operation"] == {"state": "verified", "error": None}
    [(method, path, if_match, _, body)] = expected["wire"]
    if variant == "google-occurrence":
        assert (method, path.rpartition("/")[2], if_match) == (
            {"update": "PATCH", "delete": "DELETE"}[action], steps[0]["target_id"], steps[0]["expected_etag"])
    elif variant == "caldav-single":
        assert (method, path, if_match) == ({"update": "PUT", "delete": "DELETE"}[action], urlsplit(SINGLE_URL).path,
                                            '"v1"')
        assert action == "delete" or "SUMMARY:Swim (moved)" in body
    else:  # one occurrence is a rewrite of the series resource: one more override, moved or cancelled
        assert (method, path, if_match) == ("PUT", urlsplit(SERIES_URL).path, '"v1"')
        new = "SUMMARY:Moved once" if action == "update" else "STATUS:CANCELLED"
        assert body.count(new) == stable(weekly_exceptions()).count(new) + 1
    assert ("occurrence" in steps[0]) == (variant != "caldav-single")

    key, plan_id, providers, seen, final = planned_write(changed, variant, action)
    kept = poisoned(changed, plan_id)
    assert kept == [{k: v for k, v in step.items() if k != "before"} for step in steps]
    assert run(changed, key, plan_id, providers, seen, final) == expected


# spec: ANYM-256
@pytest.mark.parametrize("arguments", [
    {"action": "move", "start": "2026-11-02T10:30:00+01:00", "series": "all"},
    {"action": "cancel", "series": "following"}], ids=["all-move", "following-cancel"])
def test_a_changed_before_writes_what_the_stored_series_scope_writes(twins, arguments):
    """calendar_schedule makes the series plan and its operation; the worker then writes it."""
    results = []
    for index, s in enumerate(twins):
        key = schedule_setup(s)
        providers, server, seen = google(series_events(), "google-a")  # the schedule family's connection
        refs = series_publish(s, server)
        result, operation_id, _ = schedule_start(s, key, {**arguments, "event": refs["20261102"],
                                                          "request_id": "req-variant-series"})
        assert operation_id, result
        plan_id = s.one("SELECT plan_id FROM operations WHERE id=%s", (operation_id,))["plan_id"]
        if index:
            poisoned(s, plan_id)
        asyncio.run(Worker(s, providers).run_operation(operation_id))
        results.append(outcome(s, operation_id, seen, lambda: server.events))
    expected, changed = results
    assert expected["operation"] == {"state": "verified", "error": None}
    assert [(m, p.rpartition("/")[2]) for m, p, *_ in expected["wire"]] == [("PATCH", "lesson")]
    assert changed == expected


# spec: ANYM-256
@pytest.mark.parametrize("field", ["expected_etag", "occurrence"])
def test_the_comparison_sees_a_changed_caldav_write_field(store, field):
    """Sensitivity: a stale expected_etag is refused before any write; another occurrence identity writes elsewhere."""
    key, plan_id, providers, seen, final = planned_write(store, "caldav-occurrence", "update")
    payload = store.one("SELECT payload FROM plans WHERE id=%s", (plan_id,))["payload"]
    [step] = payload["steps"]
    original = step["occurrence"]["original_start"]
    step.update(expected_etag=POISON["etag"]) if field == "expected_etag" else step.update(
        occurrence=dict(step["occurrence"], original_start=original.replace("2026-11-09", "2026-11-16")))
    store.query("UPDATE plans SET payload=%s WHERE id=%s", (Jsonb(payload), plan_id))
    result = run(store, key, plan_id, providers, seen, final)
    if field == "expected_etag":
        assert result["wire"] == [] and result["steps"][0]["error"] == "version_conflict"
    else:
        [(_, _, _, _, body)] = result["wire"]
        assert any(line.startswith("RECURRENCE-ID") and "20261116" in line for line in body)
        assert not any(line.startswith("RECURRENCE-ID") and "20261109" in line for line in body)
