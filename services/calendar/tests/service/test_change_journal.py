"""ANYM-565: the calendar change journal, its baseline and GET /v1/events. Provider fixtures only."""
import asyncio
from datetime import datetime, timezone
import json

import httpx
import pytest
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient

from family_calendar.errors import CalendarError
from family_calendar.service import journal
from family_calendar.service import migration_rollout as rollout
from family_calendar.service import store as store_module
from family_calendar.service.core import EVENTS, READ, FamilyService
from family_calendar.service.providers import (
    Providers, google_cancelled, normalize_google, normalize_icloud, series_ref, source_ref,
)
from family_calendar.service.store import Store

WINDOW = ("2026-11-02T00:00:00Z", "2026-11-09T00:00:00Z")
LATER = ("2026-11-09T00:00:00Z", "2026-11-16T00:00:00Z")
# ANYM-562's envelope (messaging module_events.py ENVELOPE) plus the producer's top-level scope_ref.
ENVELOPE = {"specversion", "id", "source", "type", "subject", "time", "data", "urgency", "expires_at", "scope_ref"}
DATA = {
    "calendar.event.created": {"calendar_ref", "event_ref", "start_at", "version"},
    "calendar.event.changed": {"calendar_ref", "event_ref", "start_at", "version", "changed"},
    "calendar.event.cancelled": {"calendar_ref", "event_ref", "start_at"},
}
HOME = "https://p01-caldav.icloud.com/1/calendars/home/"
ICAL_HREF = HOME + "weekly.ics"


def event(id, start, end, **extra):
    return {"id": id, "status": "confirmed", "etag": '"1"', "summary": "Lesson",
            "start": {"dateTime": start}, "end": {"dateTime": end}, **extra}


def instance(master, original, start, end, **extra):
    return event(master + "_" + original.replace("-", "").replace(":", ""), start, end, recurringEventId=master,
                 originalStartTime={"dateTime": original}, **extra)


def read(s, events, window=WINDOW, cal="cal-a", changes=None):
    return s.publish_window(cal, *window, [normalize_google(cal, e, "UTC") for e in events], "complete",
                            changes=changes)


def rows(s):
    return s.query("SELECT * FROM change_journal ORDER BY seq")


def ref(id, identity="master", cal="cal-a"):
    return source_ref("event", source_ref("res", cal, id), identity)


LESSON = event("lesson", "2026-11-03T08:00:00Z", "2026-11-03T09:00:00Z")
DENTIST = event("dentist", "2026-11-04T10:00:00Z", "2026-11-04T11:00:00Z")


# spec: ANYM-565
def test_first_read_seeds_a_baseline_and_journals_nothing(household):
    s = household
    assert read(s, [LESSON, DENTIST]) == []
    assert rows(s) == []
    assert s.one("SELECT head FROM journal_head")["head"] == 0
    assert s.one("SELECT count(*) AS n FROM event_states")["n"] == 2
    # A second identical read changes nothing.
    assert read(s, [LESSON, DENTIST]) == []


# spec: ANYM-565
def test_created_changed_and_cancelled_are_journaled_once_with_the_stored_ref(household):
    s = household
    read(s, [LESSON, DENTIST])
    moved = dict(LESSON, start={"dateTime": "2026-11-03T10:00:00Z"}, end={"dateTime": "2026-11-03T11:00:00Z"})
    new = event("party", "2026-11-05T17:00:00Z", "2026-11-05T19:00:00Z")
    # Google answers a deleted event with status cancelled and no times (showDeleted=true).
    entries = read(s, [moved, new, {"id": "dentist", "status": "cancelled"}])
    got = {(e["type"], e["event_ref"]): e for e in entries}
    assert set(got) == {("changed", ref("lesson")), ("created", ref("party")), ("cancelled", ref("dentist"))}
    assert got["changed", ref("lesson")]["changed"] == ["start", "end"]
    assert got["changed", ref("lesson")]["version"] == 2
    # The cancellation carries the stored event_ref and start_at: the ref cannot be rebuilt otherwise.
    assert got["cancelled", ref("dentist")]["start_at"] == datetime(2026, 11, 4, 10, tzinfo=timezone.utc)
    assert all(e["origin"] == "provider" for e in entries)
    assert [r["seq"] for r in rows(s)] == [1, 2, 3]
    # The same provider state read again journals nothing more.
    assert read(s, [moved, new, {"id": "dentist", "status": "cancelled"}]) == []
    assert len(rows(s)) == 3


# spec: ANYM-565
def test_details_change_is_semantic_not_etag(household):
    s = household
    read(s, [LESSON])
    assert read(s, [dict(LESSON, etag='"2"')]) == []
    [entry] = read(s, [dict(LESSON, etag='"3"', location="Pool 2")])
    assert entry["changed"] == ["details"]


# spec: ANYM-565
def test_a_change_outside_the_contract_time_range_is_stored_but_not_journaled(household):
    s = household
    old = ("1969-12-01T00:00:00Z", "1969-12-08T00:00:00Z")
    read(s, [], window=old)
    assert read(s, [event("archive", "1969-12-02T08:00:00Z", "1969-12-02T09:00:00Z")], window=old) == []
    assert s.one("SELECT count(*) AS n FROM event_states WHERE event_ref=%s", (ref("archive"),))["n"] == 1
    assert rows(s) == []


# spec: ANYM-565
def test_journal_failure_keeps_the_baseline_and_the_next_read_journals_each_change_once(household, monkeypatch):
    s = household
    read(s, [LESSON])
    moved = dict(LESSON, start={"dateTime": "2026-11-03T12:00:00Z"}, end={"dateTime": "2026-11-03T13:00:00Z"})

    def broken(c, entries):
        raise RuntimeError("journal write failed")

    with monkeypatch.context() as m:
        m.setattr(journal, "write", broken)
        assert read(s, [moved]) is None
    # The read still answers from the provider; the baseline and the journal did not move.
    assert s.one("SELECT start_at FROM occurrences WHERE ref=%s", (ref("lesson"),))["start_at"].hour == 12
    assert s.one("SELECT start_at FROM event_states WHERE event_ref=%s", (ref("lesson"),))["start_at"].hour == 8
    assert rows(s) == []
    assert s.one("SELECT outcome FROM audit WHERE action='change_journal'")["outcome"] == "failed"
    # Retry: the change is journaled once; a further read and a restarted service add nothing.
    [entry] = read(s, [moved])
    assert (entry["type"], entry["changed"]) == ("changed", ["start", "end"])
    assert read(s, [moved]) == []
    restarted = Store(s.dsn, schema=s.schema)
    try:
        assert read(restarted, [moved]) == []
    finally:
        restarted.close()
    assert len(rows(s)) == 1


# spec: ANYM-565
def test_changed_is_one_event_per_series_and_a_moved_instance_keeps_its_event_ref(household):
    s = household
    days = ("2026-11-03", "2026-11-05", "2026-11-07")
    weekly = [instance("swim", d + "T08:00:00Z", d + "T08:00:00Z", d + "T09:00:00Z") for d in days]
    read(s, weekly)
    renamed = [dict(e, summary="Swim class") for e in weekly]
    [entry] = read(s, renamed)
    assert (entry["type"], entry["changed"], entry["occurrences"]) == ("changed", ["details"], 3)
    assert entry["series_ref"] == series_ref("cal-a", "swim")
    first = ref(weekly[0]["id"], weekly[0]["originalStartTime"])
    assert entry["event_ref"] == first
    # One instance moved: changed [start, end], same event_ref (its recurringEventId and originalStartTime).
    moved = dict(renamed[1], start={"dateTime": "2026-11-05T10:00:00Z"}, end={"dateTime": "2026-11-05T11:00:00Z"})
    [entry] = read(s, [renamed[0], moved, renamed[2]])
    assert entry["event_ref"] == ref(weekly[1]["id"], weekly[1]["originalStartTime"])
    assert (entry["changed"], entry["occurrences"]) == (["start", "end"], 1)
    # A cancelled instance names its series and original start.
    [entry] = read(s, [renamed[0], moved, dict(renamed[2], status="cancelled")])
    assert (entry["type"], entry["event_ref"]) == ("cancelled", ref(weekly[2]["id"], weekly[2]["originalStartTime"]))


# spec: ANYM-565
def test_a_move_out_of_the_window_is_no_event_until_another_read_finds_it(household):
    s = household
    read(s, [LESSON])
    # Re-read of the same window without the event and without a cancelled status: moved or unknown, no event.
    assert read(s, []) == []
    assert s.one("SELECT cancelled_at FROM event_states WHERE event_ref=%s", (ref("lesson"),))["cancelled_at"] is None
    # The next week's read finds the same identity at a new start: changed, never created.
    later = dict(LESSON, start={"dateTime": "2026-11-10T08:00:00Z"}, end={"dateTime": "2026-11-10T09:00:00Z"})
    [entry] = read(s, [later], window=LATER)
    assert (entry["type"], entry["event_ref"], entry["changed"]) == ("changed", ref("lesson"), ["start", "end"])


# spec: ANYM-565
def test_overlapping_and_shrinking_windows_and_failed_reads_never_cancel(household):
    s = household
    read(s, [LESSON, DENTIST])
    # A narrower window that no longer contains the dentist, then an overlapping one: no cancellation.
    assert read(s, [LESSON], window=("2026-11-02T00:00:00Z", "2026-11-04T00:00:00Z")) == []
    assert read(s, [DENTIST], window=("2026-11-04T00:00:00Z", "2026-11-12T00:00:00Z")) == []
    # A failed or partial read journals nothing and leaves the baseline.
    s.publish_window("cal-a", *WINDOW, [], "partial")
    assert rows(s) == []
    assert s.one("SELECT count(*) AS n FROM event_states WHERE cancelled_at IS NULL")["n"] == 2


def ical(*extra, rule="FREQ=WEEKLY;COUNT=3"):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", "UID:weekly", "DTSTART:20261103T080000Z",
             "DTEND:20261103T090000Z", "RRULE:" + rule, "SUMMARY:Lesson", *extra[:1], "END:VEVENT",
             *extra[1:], "END:VCALENDAR"]
    return {"href": ICAL_HREF, "etag": '"1"', "data": ("\r\n".join(line for line in lines if line) + "\r\n").encode()}


def icloud_read(s, resources, window=("2026-11-02T00:00:00Z", "2026-11-23T00:00:00Z"), changes=None):
    a, b = (datetime.fromisoformat(t.replace("Z", "+00:00")) for t in window)
    return s.publish_window("cal-i", *window, [normalize_icloud("cal-i", r, a, b, "UTC") for r in resources],
                            "complete", changes=changes)


def icloud(s):
    s.connection("icloud-a", "adult-a", "icloud")
    s.calendar("cal-i", "icloud-a", HOME, "Home", "UTC", "write")
    return s


# spec: ANYM-565
def test_caldav_instance_gone_from_a_complete_reread_is_cancelled_and_an_override_is_not(household):
    s = icloud(household)
    icloud_read(s, [ical()])
    states = {r["original_start"]: r["event_ref"] for r in s.query("SELECT * FROM event_states")}
    assert len(states) == 3
    # A rule that stops early (or a range override that moves the rest) is no evidence of a cancellation.
    assert icloud_read(s, [ical(rule="FREQ=WEEKLY;COUNT=1")]) == []
    assert icloud_read(s, [ical()]) == []
    # EXDATE drops the second week; an override moves the third out of the window: one cancellation only.
    override = "\r\n".join(["BEGIN:VEVENT", "UID:weekly", "RECURRENCE-ID:20261117T080000Z",
                            "DTSTART:20261201T080000Z", "DTEND:20261201T090000Z", "SUMMARY:Lesson", "END:VEVENT"])
    [entry] = icloud_read(s, [ical("EXDATE:20261110T080000Z", override)])
    assert (entry["type"], entry["event_ref"]) == ("cancelled", states["2026-11-10T08:00:00+00:00"])
    # A resource missing from the read is cancelled only when read back as gone (the worker's 404 probe).
    resource_ref = source_ref("res", "cal-i", ICAL_HREF)
    assert icloud_read(s, []) == []
    entries = icloud_read(s, [], changes={"cancelled": [["resource", resource_ref]]})
    # Every live occurrence of the deleted resource, the one an override moved out of the window included.
    assert sorted(e["event_ref"] for e in entries) == sorted(
        states[t] for t in ("2026-11-03T08:00:00+00:00", "2026-11-17T08:00:00+00:00"))


def single(uid, start):
    data = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", "UID:" + uid, "DTSTART:" + start,
                         "DURATION:PT1H", "SUMMARY:Visit", "END:VEVENT", "END:VCALENDAR", ""])
    return {"href": HOME + uid + ".ics", "etag": '"1"', "data": data.encode()}


class Probe:
    """CalDAV GET of a lost resource: 404 (None) for the deleted ones, the resource for the moved ones."""

    def __init__(self, deleted):
        self.deleted, self.reads = deleted, []

    async def read(self, href):
        self.reads.append(href)
        return None if href in self.deleted else {"href": href, "data": b"", "etag": '"2"'}


# spec: ANYM-565
def test_caldav_probes_rotate_so_a_deleted_resource_behind_a_moved_one_is_found(household, monkeypatch):
    from family_calendar.service import providers as providers_module

    s = icloud(household)
    # Never probed yet, the resources are read in resource_ref order: the moved one first.
    moved, deleted = sorted((single("visit-1", "20261104T080000Z"), single("visit-2", "20261105T080000Z")),
                            key=lambda r: source_ref("res", "cal-i", r["href"]))
    icloud_read(s, [moved, deleted])
    monkeypatch.setattr(providers_module, "GONE_PROBES", 1)
    providers = Providers(s, None)
    providers.adapters["icloud-a"] = probe = Probe({deleted["href"]})
    cal = {"provider": "icloud", "connection_id": "icloud-a", "target": HOME, "ref": "cal-i"}
    window = ("2026-11-02T00:00:00Z", "2026-11-23T00:00:00Z")
    uid = deleted["href"].rsplit("/", 1)[1].removesuffix(".ics")
    for expected in ([], [source_ref("event", source_ref("res", "cal-i", deleted["href"]), uid, "master")]):
        with s.transaction() as c:
            lost = journal.missing(c, "cal-i", *window, set())
        keys, probed = asyncio.run(providers.gone(cal, lost, None))
        entries = icloud_read(s, [], changes={"cancelled": keys, "probed": probed})
        assert [e["event_ref"] for e in entries] == expected
    # The moved resource was read first; the next refresh read the one behind it.
    assert probe.reads == [moved["href"], deleted["href"]]


class SlowProbe(Probe):
    async def read(self, href):
        self.reads.append(href)
        await asyncio.sleep(30)


# spec: ANYM-565
def test_a_probe_that_times_out_still_rotates_to_the_next_resource(household):
    s = icloud(household)
    first, second = sorted((single("visit-1", "20261104T080000Z"), single("visit-2", "20261105T080000Z")),
                           key=lambda r: source_ref("res", "cal-i", r["href"]))
    icloud_read(s, [first, second])
    providers = Providers(s, None)
    cal = {"provider": "icloud", "connection_id": "icloud-a", "target": HOME, "ref": "cal-i"}
    window = ("2026-11-02T00:00:00Z", "2026-11-23T00:00:00Z")

    async def refresh(adapter):
        providers.adapters["icloud-a"] = adapter
        with s.transaction() as c:
            lost = journal.missing(c, "cal-i", *window, set())
        return await providers.gone(cal, lost, asyncio.get_running_loop().time() + 0.2)

    # The first resource hangs: attempted, nothing claimed, and the refresh goes on within its deadline.
    keys, probed = asyncio.run(refresh(SlowProbe(set())))
    assert (keys, probed) == ([], [source_ref("res", "cal-i", first["href"])])
    assert icloud_read(s, [], changes={"cancelled": keys, "probed": probed}) == []
    # The next refresh starts with the other one, which is gone.
    keys, probed = asyncio.run(refresh(Probe({second["href"]})))
    assert keys[0] == ["resource", source_ref("res", "cal-i", second["href"])]
    assert len(icloud_read(s, [], changes={"cancelled": keys, "probed": probed})) == 1


# spec: ANYM-565
def test_origin_names_the_deedkit_principal_that_wrote_the_resource(household):
    s = household
    s.grant("family", "adult-a", "cal-a", READ, "details")
    s.agent("agent-a", "family", "adult-a", READ, ["cal-a"])
    read(s, [LESSON, DENTIST])
    s.query("INSERT INTO plans(id,principal_id,payload,scope_hash,expires_at) VALUES "
            "('plan-1','agent-a','{}','x',now()+interval '1 hour')")
    # Queued before the read that saw the old state, finished (audit) after it: the write is DeedKit's.
    s.query("INSERT INTO operations(id,principal_id,plan_id,request_id,state,created_at) VALUES "
            "('op-1','agent-a','plan-1','r-1','verified',now()-interval '1 hour')")
    s.query("INSERT INTO audit(principal_id,action,ref,outcome) VALUES ('agent-a','operation','op-1','verified')")
    s.query("INSERT INTO operation_steps(operation_id,ordinal,state,intent) VALUES ('op-1',0,'verified',%s)",
            (Jsonb({"resource_ref": source_ref("res", "cal-a", "lesson"), "action": "update"}),))
    entries = read(s, [dict(LESSON, summary="Moved by agent"), dict(DENTIST, summary="Moved by hand")])
    assert {e["event_ref"]: e["origin"] for e in entries} == {
        ref("lesson"): "deedkit:agent-a", ref("dentist"): "provider"}


# spec: ANYM-565
def test_google_incremental_sync_cancels_and_its_token_is_stored(household):
    s = household
    read(s, [LESSON, DENTIST], changes={"sync": {"token": "t1"}})
    assert s.one("SELECT sync_token FROM journal_calendars")["sync_token"] == "t1"
    # A read of the next week learns from the incremental sync that the dentist (stored in this week) is cancelled:
    # journaled, and its occurrence leaves the projection, while its snapshot stays.
    changes = {"cancelled": google_cancelled("cal-a", {"id": "dentist", "status": "cancelled"}),
               "sync": {"token": "t2"}}
    [entry] = read(s, [], window=LATER, changes=changes)
    assert (entry["type"], entry["event_ref"]) == ("cancelled", ref("dentist"))
    assert s.one("SELECT sync_token FROM journal_calendars")["sync_token"] == "t2"
    assert s.one("SELECT count(*) AS n FROM occurrences WHERE ref=%s", (ref("dentist"),))["n"] == 0
    assert s.one("SELECT cancelled_at FROM event_states WHERE event_ref=%s", (ref("dentist"),))["cancelled_at"]


class SyncAdapter:
    """Google's sync: t1 -> one cancellation and t2; an expired token is a 410; a full run takes two steps."""

    def __init__(self):
        self.calls = []

    async def changes(self, calendar, token=None, start=None, page=None, deadline=None):
        self.calls.append((token, start, page))
        if token == "expired":
            return None
        if token:
            return {"items": [{"id": "dentist", "status": "cancelled"}], "done": True, "sync_token": "t2", "page": None}
        if page is None:
            return {"items": [{"id": "party", "status": "cancelled"}], "done": False, "sync_token": None, "page": "p2"}
        return {"items": [{"id": "picnic", "status": "cancelled"}], "done": True, "sync_token": "t1", "page": None}


def keys(*ids):
    return [k for id in ids for k in google_cancelled("cal-a", {"id": id, "status": "cancelled"})]


# spec: ANYM-565
def test_providers_use_the_sync_token_and_resync_after_410():
    providers = Providers(None, None)
    providers.adapters["google-a"] = adapter = SyncAdapter()
    cal = {"provider": "google", "connection_id": "google-a", "target": "private-a", "ref": "cal-a"}
    since = datetime(2026, 11, 2, tzinfo=timezone.utc)
    got = asyncio.run(providers.sync_changes(cal, {"token": "t1"}, since))
    assert got == {"cancelled": keys("dentist"), "sync": {"token": "t2", "page": None, "since": None}}
    # 410: a full resync from the earliest tracked event; each step claims the cancellations it read, and a run that
    # does not end resumes at its page with the same start, so no cancellation it passes is skipped.
    first = asyncio.run(providers.sync_changes(cal, {"token": "expired"}, since))
    assert first == {"cancelled": keys("party"), "sync": {"token": None, "page": "p2", "since": since}}
    later = datetime(2026, 11, 9, tzinfo=timezone.utc)
    second = asyncio.run(providers.sync_changes(cal, first["sync"], later))
    assert second == {"cancelled": keys("picnic"), "sync": {"token": "t1", "page": None, "since": None}}
    assert adapter.calls == [("t1", None, None), ("expired", None, None), (None, since.isoformat(), None),
                             (None, since.isoformat(), "p2")]
    assert asyncio.run(providers.sync_changes(dict(cal, provider="icloud"), {}, since)) == {}


# spec: ANYM-565
def test_a_resumed_sync_state_is_stored_with_the_window(household):
    s = household
    since = datetime(2026, 11, 2, tzinfo=timezone.utc)
    read(s, [LESSON], changes={"sync": {"token": None, "page": "p2", "since": since}})
    with s.transaction() as c:
        assert journal.sync_state(c, "cal-a") == {"token": None, "page": "p2", "since": since}
    read(s, [LESSON], changes={"sync": {"token": "t1", "page": None, "since": None}})
    with s.transaction() as c:
        assert journal.sync_state(c, "cal-a") == {"token": "t1", "page": None, "since": None}


class SlowSync:
    async def sync_changes(self, cal, state, since, deadline=None):
        await asyncio.sleep(30)


# spec: ANYM-565
def test_a_slow_provider_sync_keeps_the_token_and_never_holds_the_worker(household, monkeypatch):
    from family_calendar.service import worker as worker_module

    s = household
    read(s, [LESSON], changes={"sync": {"token": "t1"}})
    monkeypatch.setattr(worker_module, "SUPPLEMENT_S", 0.05)
    cal = {"provider": "google", "connection_id": "google-a", "target": "private-a", "ref": "cal-a"}
    started = asyncio.run(asyncio.wait_for(worker_module.Worker(s, SlowSync())._journal_changes(
        cal, datetime(2026, 11, 2, tzinfo=timezone.utc)), 2))
    # No changes and no token change: the window is still published, and the next refresh reads from t1 again.
    assert started == {}
    read(s, [LESSON], changes=started)
    assert s.one("SELECT sync_token FROM journal_calendars")["sync_token"] == "t1"


# spec: ANYM-565
def test_google_adapter_sync_requests_no_time_bounds_with_a_token_and_reads_410(monkeypatch):
    from family_calendar.providers.google import adapter as adapter_module
    from family_calendar.providers.google.adapter import GoogleAdapter

    seen = []

    def handler(request):
        params = dict(request.url.params)
        seen.append(params)
        if params.get("syncToken") == "expired":
            return httpx.Response(410, json={})
        if "pageToken" not in params:
            return httpx.Response(200, json={"items": [{"id": "a", "status": "cancelled"}], "nextPageToken": "p2"})
        return httpx.Response(200, json={"items": [{"id": "b", "status": "confirmed"}], "nextSyncToken": "t2"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = GoogleAdapter("google-a", client, lambda: "token")
            return (await adapter.changes("private-a", token="t1"), await adapter.changes("private-a", token="expired"),
                    await adapter.changes("private-a", start="2026-11-02T00:00:00+00:00"),
                    await adapter.changes("private-a", start="2026-11-02T00:00:00+00:00", page="p2"))

    incremental, expired, initial, resumed = asyncio.run(run())
    # Only cancelled items are kept, in the incremental and in the full sync.
    done = {"items": [{"id": "a", "status": "cancelled"}], "done": True, "sync_token": "t2", "page": None}
    assert incremental == done and initial == done and expired is None
    assert resumed == {"items": [], "done": True, "sync_token": "t2", "page": None}
    assert all("timeMin" not in p and "timeMax" not in p for p in seen if "syncToken" in p)
    assert seen[-1]["timeMin"] == "2026-11-02T00:00:00+00:00" and seen[-1]["pageToken"] == "p2"
    assert "syncToken" not in seen[-1]
    # A step that reaches its page limit (or its deadline) returns its items and the page to resume from.
    monkeypatch.setattr(adapter_module, "SYNC_PAGES", 1)

    async def step():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await GoogleAdapter("google-a", client, lambda: "token").changes("private-a", token="t1")

    assert asyncio.run(step()) == {"items": [{"id": "a", "status": "cancelled"}], "done": False, "sync_token": None,
                                   "page": "p2"}
    # Only the fields that name an item and its status: no title, place or description is read.
    assert all(p["fields"] == "items(id,status,recurringEventId,originalStartTime),nextPageToken,nextSyncToken"
               and p["showDeleted"] == "true" for p in seen)


# spec: ANYM-565
def test_events_page_serves_the_cursor_contract_with_only_reference_keys(household):
    s = icloud(household)
    service = FamilyService(s)
    key = s.agent("router", "family", "adult-a", [EVENTS], ["cal-a"])
    read(s, [LESSON, DENTIST])
    icloud_read(s, [ical()])
    read(s, [dict(LESSON, location="Pool 2"), event("party", "2026-11-05T17:00:00Z", "2026-11-05T19:00:00Z"),
             {"id": "dentist", "status": "cancelled"}])
    icloud_read(s, [ical("LOCATION:Room 2")])  # journaled for cal-i, which the router is not bound to
    page = service.events_page(key, 0, 100)
    assert set(page) == {"events", "next_after", "has_more", "retention_floor", "journal_id"}
    assert (page["next_after"], page["has_more"], page["retention_floor"]) == (4, False, 0)
    assert len(page["events"]) == 3
    for e in page["events"]:
        assert set(e) == ENVELOPE and set(e["data"]) == DATA[e["type"]]
        assert e["subject"] == e["data"]["event_ref"] and e["scope_ref"] == "calendar:cal-a"
        assert (e["specversion"], e["source"], e["urgency"], e["expires_at"]) == ("1.0", "deedkit:calendar", "batch", None)
        assert e["id"].startswith("calendar:") and e["data"]["start_at"].endswith("Z")
        assert len(json.dumps(e["data"]).encode()) <= 1024
    assert service.events_head(key) == {"journal_id": page["journal_id"], "head": 4}
    # The cursor is exclusive, limit bounds the page and next_after moves past rows of other calendars.
    first = service.events_page(key, 0, 1)
    assert (len(first["events"]), first["next_after"], first["has_more"]) == (1, 1, True)
    rest = service.events_page(key, 3, 100)
    assert (rest["events"], rest["next_after"], rest["has_more"]) == ([], 4, False)
    assert service.events_page(key, 9, 100)["next_after"] == 4
    # A key with any other scope reads nothing.
    s.grant("family", "adult-a", "cal-a", READ, "details")
    agent = s.agent("agent-a", "family", "adult-a", READ, ["cal-a"])
    with pytest.raises(CalendarError) as refused:
        service.events_page(agent, 0, 100)
    assert refused.value.code == "not_found_or_forbidden"


# spec: ANYM-565
def test_events_endpoint_checks_arguments_and_scope(household):
    from family_calendar.service.api import create_app

    s = household
    key = s.agent("router", "family", "adult-a", [EVENTS], ["cal-a"])
    s.grant("family", "adult-a", "cal-a", READ, "details")
    agent = s.agent("agent-a", "family", "adult-a", READ, ["cal-a"])
    with TestClient(create_app(s, port=13232), base_url="http://127.0.0.1:13232") as c:
        auth = {"Authorization": "Bearer " + key}
        # Before the worker's first journal write there is no journal id to promise.
        assert c.get("/v1/events/head", headers=auth).json() == {"error": "journal_not_ready"}
        read(s, [LESSON])
        read(s, [dict(LESSON, location="Pool 2")])
        assert c.get("/v1/events/head", headers=auth).json()["head"] == 1
        r = c.get("/v1/events?after=0&limit=100", headers=auth)
        assert r.status_code == 200 and r.json()["events"][0]["type"] == "calendar.event.changed"
        for query in ("after=-1", "limit=0", "limit=201", "after=1&after=2", "cursor=1", "after=99999999999999999"):
            assert c.get("/v1/events?" + query, headers=auth).status_code == 422
        assert c.get("/v1/events", headers={"Authorization": "Bearer " + agent}).status_code == 403
        # The router's key reads the journal and no calendar tool.
        assert c.post("/v1/calendars", json={}, headers=auth).status_code == 403
        assert c.get("/v1/events").status_code == 401


# spec: ANYM-565
def test_a_cancelled_event_stays_readable_in_agenda_for_30_days(household):
    s = household
    s.grant("family", "adult-a", "cal-a", READ, "details")
    key = s.agent("agent-a", "family", "adult-a", READ, ["cal-a"])
    service = FamilyService(s)
    read(s, [LESSON, DENTIST])
    read(s, [LESSON, {"id": "dentist", "status": "cancelled"}])
    agenda = service.agenda(key, ["cal-a"], *WINDOW)
    [gone] = agenda["cancelled"]
    assert (gone["event_ref"], gone["status"], gone["title"]) == (ref("dentist"), "cancelled", "Lesson")
    assert datetime.fromisoformat(gone["start"]) == datetime(2026, 11, 4, 10, tzinfo=timezone.utc)
    assert [e["event_ref"] for e in agenda["events"]] == [ref("lesson")]
    # Past 30 days the next read deletes the snapshot.
    s.query("UPDATE event_states SET cancelled_at=now()-interval '31 days' WHERE event_ref=%s", (ref("dentist"),))
    read(s, [LESSON, {"id": "dentist", "status": "cancelled"}])
    assert "cancelled" not in service.agenda(key, ["cal-a"], *WINDOW)
    assert s.one("SELECT count(*) AS n FROM event_states WHERE event_ref=%s", (ref("dentist"),))["n"] == 0
    # More than 500 cancelled at one span: the response says so, and each ref stays readable by event_refs.
    s.query("INSERT INTO event_states(event_ref,calendar_ref,resource_ref,start_at,end_at,busy,details,cancelled_at) "
            "SELECT 'event-'||g,'cal-a','res-'||g,'2026-11-06T10:00:00Z','2026-11-06T11:00:00Z',true,"
            "'{\"title\": \"Visit\"}',now() FROM generate_series(1,501) g")
    crowded = service.agenda(key, ["cal-a"], *WINDOW)
    assert len(crowded["cancelled"]) == 500 and crowded["cancelled_incomplete"] is True
    [last] = service.agenda(key, ["cal-a"], *WINDOW, event_refs=["event-99"])["cancelled"]
    assert last["event_ref"] == "event-99"


# spec: ANYM-565
def test_migration_8_is_an_unattended_expand_with_additive_grants():
    files = dict(store_module.MIGRATIONS)
    created = {}
    for version in sorted(files):
        if version < 8:
            created = rollout.new_tables(files[version].read_text(), created)
    text = files[8].read_text()
    assert rollout.unattended_class(text, set(created)) == "expand"
    roles = store_module.ROLES.read_text()
    base = roles[: roles.index("-- Migration 8 (change journal")]
    assert rollout.role_class(base, roles, rollout.new_tables(text), tables=set(created)) == "additive"
