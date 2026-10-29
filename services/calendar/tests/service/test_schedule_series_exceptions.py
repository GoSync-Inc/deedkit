"""calendar_schedule series time moves that carry their exceptions (ANYM-614): a preview that lists each exception in
range and its fate, one explicit write per exception after the series write, a re-list before any write and a
read-back of every exception. Google is the synthetic SeriesGoogle below; its fate of exceptions is an assumption
the owner's live check confirms (docs/VALIDATION.md, ANYM-614)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from dateutil.rrule import rrulestr

from family_calendar.service import schedule as sched
from family_calendar.service.core import FamilyService
from family_calendar.service.schedule import Scheduler
from family_calendar.service.worker import Worker
from family_calendar.providers.google.adapter import GoogleAdapter
from family_calendar.service.providers import Providers
from tests.fakes import FakeGoogle
from tests.service.test_schedule_intent import rulebook, setup
from tests.service.test_schedule_series import BERLIN, master, plan_steps, publish, run, start, writes


class SeriesGoogle(FakeGoogle):
    """FakeGoogle that expands each recurring master into its instances (`<id>_<UTC start>`, as Google names them).
    An exception (an instance changed or cancelled on its own) whose original start the master's rule no longer
    yields is dropped, as Google Calendar's own all-events edit drops instance changes; with `keep` it stays
    (the case the read-back must catch)."""

    def __init__(self, events, keep=False):
        super().__init__(events)
        self.keep = keep
        for e in list(self.events.values()):
            if e.get("recurrence"):
                self.expand(e["id"])

    @staticmethod
    def starts(m):
        zone = ZoneInfo(m["start"]["timeZone"])
        first = datetime.fromisoformat(m["start"]["dateTime"]).astimezone(zone)
        rule = rrulestr(m["recurrence"][0][len("RRULE:"):], dtstart=first)
        return [t.astimezone(timezone.utc) for t in rule]

    def expand(self, master_id):
        m = self.events[master_id]
        length = datetime.fromisoformat(m["end"]["dateTime"]) - datetime.fromisoformat(m["start"]["dateTime"])
        starts = self.starts(m)
        zone = ZoneInfo(m["start"]["timeZone"])
        for key, e in list(self.events.items()):
            if e.get("recurringEventId") != master_id:
                continue
            original = datetime.fromisoformat(e["originalStartTime"]["dateTime"]).astimezone(timezone.utc)
            if e.get("_generated") or (original not in starts and not self.keep):
                self.events.pop(key)
        for t in starts:
            instance = f"{master_id}_{t:%Y%m%dT%H%M%SZ}"
            if instance in self.events:
                continue
            local = t.astimezone(zone)
            self.events[instance] = {
                "id": instance, "iCalUID": m["iCalUID"], "etag": self._etag(), "recurringEventId": master_id,
                "_generated": True, "status": m.get("status", "confirmed"),
                **{k: m[k] for k in ("summary", "location", "description") if k in m},
                "originalStartTime": {"dateTime": local.isoformat(), "timeZone": m["start"]["timeZone"]},
                "start": {"dateTime": local.isoformat(), "timeZone": m["start"]["timeZone"]},
                "end": {"dateTime": (local + length).astimezone(zone).isoformat(), "timeZone": m["start"]["timeZone"]},
            }

    def __call__(self, req):
        from urllib.parse import unquote

        answer = super().__call__(req)
        target = unquote(req.url.raw_path.decode().partition("/events")[2].lstrip("/").split("?")[0])
        if req.method == "POST" and answer.status_code == 200:
            target = json.loads(req.content)["id"]
        event = self.events.get(target)
        if req.method != "GET" and answer.status_code < 300 and event:
            if event.get("recurrence"):
                self.expand(target)
            else:
                # An instance written on its own is an exception from now on.
                event.pop("_generated", None)
        return answer


def week(n, hour=9, minute=0):
    """The original start of the n-th Monday of master() (19 Oct 2026 = 0) at hour:minute Berlin."""
    day = datetime(2026, 10, 19, hour, minute, tzinfo=ZoneInfo(BERLIN)) + timedelta(weeks=n)
    return day.replace(tzinfo=ZoneInfo(BERLIN))


def iid(base, n, hour=9, minute=0):
    return f"{base}_{week(n, hour, minute).astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"


def exception(n, status=None, start=None, end=None, **fields):
    """An exception of master() at week n: cancelled, moved to [start, end) or with other fields of its own."""
    original = week(n)
    return {"id": iid("lesson", n), "iCalUID": "lesson@example.invalid", "etag": f'"x{n}"', "summary": "Lesson",
            "location": "Pool", "recurringEventId": "lesson",
            "originalStartTime": {"dateTime": original.isoformat(), "timeZone": BERLIN},
            "start": {"dateTime": (start or original).isoformat(), "timeZone": BERLIN},
            "end": {"dateTime": (end or original + timedelta(hours=1)).isoformat(), "timeZone": BERLIN},
            **({"status": status} if status else {}), **fields}


def world(events, keep=False, fail=None):
    server = SeriesGoogle(events, keep)

    def handle(request):
        answer = fail(request, server) if fail else None
        return answer if answer is not None else server(request)

    providers = Providers(None, None)
    providers.adapters["google-a"] = GoogleAdapter(
        "google-a", httpx.AsyncClient(transport=httpx.MockTransport(handle)), lambda: "canary-token")
    return providers, server


def preview(s, providers, key, arguments):
    """A dry run of a series time move: with no worker running, the first call previews without the read and asks
    for it (`exceptions_read: pending`); the worker answers it; the repeat (its `next`) previews with it."""
    first = rulebook.validate_result(start(s, key, {**arguments, "dry_run": True})[0])
    if "exceptions_read" not in first["preview"]:
        # A read answered moments ago is reused.
        return first
    assert first["preview"]["exceptions_read"]["state"] == "pending" and "exceptions" not in first["preview"]
    asyncio.run(Worker(s, providers).read_series_one())
    return rulebook.validate_result(start(s, key, first["preview"]["exceptions_read"]["next"]["arguments"])[0])


ALL = {"action": "move", "start": "2026-11-02T10:30:00+01:00", "series": "all"}


# spec: ANYM-614
def test_an_all_move_lists_and_carries_a_cancelled_an_edited_and_a_moved_exception(store):
    key = setup(store)
    providers, server = world([master(), exception(1, status="cancelled"),
                               exception(3, summary="Lesson (bring cap)"),
                               exception(4, start=week(4) + timedelta(days=1, hours=2),
                                         end=week(4) + timedelta(days=1, hours=3))])
    refs = publish(store, server)
    shown = preview(store, providers, key, {**ALL, "event": refs["20261102"]})
    view = shown["preview"]
    assert view["writes"] == 4 and view["steps"] == ["move_series", *["carry_exception"] * 3]
    assert "preview_stale" in view["effect"]
    assert [(e["original_start"], e["fate"], e["series_start"]) for e in view["exceptions"]] == [
        ("2026-10-26T16:00:00+08:00", "cancelled", "2026-10-26T17:30:00+08:00"),
        ("2026-11-09T16:00:00+08:00", "moves", "2026-11-09T17:30:00+08:00"),
        ("2026-11-16T16:00:00+08:00", "kept", "2026-11-16T17:30:00+08:00")]
    assert all(e["effect"] == sched.EXCEPTION_EFFECTS[e["fate"]] for e in view["exceptions"])
    kept = view["exceptions"][2]
    assert kept["own"] == {"start": "2026-11-17T18:00:00+08:00", "end": "2026-11-17T19:00:00+08:00"}
    assert kept["follow_up"] == {"tool": "calendar_schedule", "arguments": {
        "action": "move", "event": {"date": "2026-11-17"}, "start": "2026-11-16T17:30:00+08:00", "duration": 60,
        "series": "this", "dry_run": True}}
    assert writes(server) == []
    result = rulebook.validate_result(run(store, providers, key, {"confirm_token": shown["confirm_token"]}))
    assert result["state"] == "succeeded"
    assert writes(server) == [("PATCH", "lesson"), ("DELETE", iid("lesson", 1, 10, 30)),
                              ("PATCH", iid("lesson", 3, 10, 30)), ("PATCH", iid("lesson", 4, 10, 30))]
    events = server.events
    assert events["lesson"]["start"]["dateTime"].startswith("2026-10-19T10:30:00")
    assert events[iid("lesson", 1, 10, 30)]["status"] == "cancelled"
    edited = events[iid("lesson", 3, 10, 30)]
    assert edited["summary"] == "Lesson (bring cap)" and edited["start"]["dateTime"].startswith("2026-11-09T10:30")
    moved = events[iid("lesson", 4, 10, 30)]
    assert moved["start"]["dateTime"] == (week(4) + timedelta(days=1, hours=2)).isoformat()
    assert moved["summary"] == "Lesson"
    # The plain occurrences only moved; nothing else was written.
    assert events[iid("lesson", 2, 10, 30)]["summary"] == "Lesson"


# spec: ANYM-614
def test_a_following_move_carries_only_the_exceptions_from_its_boundary_on(store):
    key = setup(store)
    before = exception(1, summary="Early edit")
    providers, server = world([master(), before, exception(4, start=week(4) + timedelta(hours=3),
                                                           end=week(4) + timedelta(hours=4)),
                               exception(5, status="cancelled")])
    kept = json.dumps(server.events[before["id"]], sort_keys=True)
    refs = publish(store, server)
    move = {"action": "move", "start": "2026-11-09T10:30:00+01:00", "series": "following", "event": refs["20261109"]}
    view = preview(store, providers, key, move)["preview"]
    assert [e["fate"] for e in view["exceptions"]] == ["kept", "cancelled"]
    assert view["steps"] == ["create_replacement", "end_original", "carry_exception", "carry_exception"]
    result = rulebook.validate_result(run(store, providers, key, {**move, "request_id": "req-carry-following"}))
    assert result["state"] == "failed" and result["error"] == "series_exceptions"
    # A keyed apply has no preview read: it keeps ANYM-556's refusal. The confirmed preview carries them.
    shown = preview(store, providers, key, move)
    result = rulebook.validate_result(run(store, providers, key, {"confirm_token": shown["confirm_token"]}))
    assert result["state"] == "succeeded"
    replacement = plan_steps(store, result)[0]["target_id"]
    assert writes(server) == [("POST", "events"), ("PATCH", "lesson"), ("PATCH", iid(replacement, 4, 10, 30)),
                              ("DELETE", iid(replacement, 5, 10, 30))]
    assert server.events[iid(replacement, 4, 10, 30)]["start"]["dateTime"] == (week(4) + timedelta(hours=3)).isoformat()
    assert server.events[iid(replacement, 5, 10, 30)]["status"] == "cancelled"
    assert json.dumps(server.events[before["id"]], sort_keys=True) == kept


# spec: ANYM-614
def test_an_exception_added_after_the_preview_refuses_as_stale_before_any_write(store):
    key = setup(store)
    providers, server = world([master(), exception(3, summary="Edited")])
    refs = publish(store, server)
    shown = preview(store, providers, key, {**ALL, "event": refs["20261102"]})
    assert len(shown["preview"]["exceptions"]) == 1
    # Someone cancels another occurrence in Google Calendar before the owner confirms.
    added = server.events[iid("lesson", 5)]
    added.update(status="cancelled", etag='"elsewhere"')
    added.pop("_generated")
    result = rulebook.validate_result(run(store, providers, key, {"confirm_token": shown["confirm_token"]}))
    assert (result["state"], result["error"], writes(server)) == ("failed", "preview_stale", [])
    assert result["next"] == {"tool": "calendar_schedule", "arguments": {**ALL, "event": refs["20261102"],
                                                                         "dry_run": True}}


# spec: ANYM-614
def test_more_than_fifty_exceptions_refuse_with_a_following_move_from_a_later_occurrence(store):
    key = setup(store)
    long = master(recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=110"])
    providers, server = world([long, *(exception(n, status="cancelled") for n in range(1, 102, 2))])
    refs = publish(store, server)
    pending = start(store, key, {**ALL, "event": refs["20261019"], "start": "2026-10-19T10:30:00+02:00",
                                 "dry_run": True})[0]["preview"]["exceptions_read"]
    asyncio.run(Worker(store, providers).read_series_one())
    refused = start(store, key, pending["next"]["arguments"])[0]
    assert refused["error"] == "series_exceptions_too_many" and (refused["count"], refused["limit"]) == (51, 50)
    # From week 2 on, 50 exceptions remain (weeks 3..101).
    assert refused["next"] == {"tool": "calendar_schedule", "arguments": {
        "action": "move", "event": {"date": "2026-11-02"}, "start": "2026-11-02T17:30:00+08:00",
        "series": "following", "dry_run": True}}
    assert writes(server) == []


class Crash(BaseException):
    """The worker process dies here: nothing after it runs, nothing is caught."""


# spec: ANYM-614
def test_a_crash_after_the_series_write_resumes_the_exception_writes_once(store):
    key = setup(store)
    providers, server = world([master(), exception(1, status="cancelled"), exception(3, summary="Edited")])
    refs = publish(store, server)
    shown = preview(store, providers, key, {**ALL, "event": refs["20261102"]})
    _, operation_id, family = start(store, key, {"confirm_token": shown["confirm_token"]})
    prepare, crashes = providers.prepare, ["before-exceptions"]

    async def die_before_exceptions(cal, step):
        if (step.get("series") or {}).get("role") == "exception" and crashes:
            crashes.pop()
            raise Crash
        return await prepare(cal, step)

    providers.prepare = die_before_exceptions
    worker = Worker(store, providers)
    with pytest.raises(Crash):
        asyncio.run(worker.run_operation(operation_id))
    assert writes(server) == [("PATCH", "lesson")]
    store.query("UPDATE operations SET lease_until=now()-interval '1 second' WHERE id=%s", (operation_id,))
    store.query("UPDATE plans SET expires_at=now()-interval '1 hour' WHERE operation_id=%s", (operation_id,))
    asyncio.run(worker.tick())
    assert Scheduler(FamilyService(store)).replay(operation_id, family)["state"] == "succeeded"
    assert writes(server) == [("PATCH", "lesson"), ("DELETE", iid("lesson", 1, 10, 30)),
                              ("PATCH", iid("lesson", 3, 10, 30))]


# spec: ANYM-614
def test_an_unknown_exception_write_is_reconciled_by_read_back_never_resent(store):
    key = setup(store)
    edited = iid("lesson", 3, 10, 30)

    def lost(request, server):
        if request.method == "PATCH" and request.url.path.endswith(edited) and not server.drop_response:
            server.drop_response = True
            try:
                server(request)
            finally:
                server.drop_response = False
            return httpx.Response(503)
        return None

    providers, server = world([master(), exception(3, summary="Edited"), exception(4, summary="Also")], fail=lost)
    refs = publish(store, server)
    shown = preview(store, providers, key, {**ALL, "event": refs["20261102"]})
    _, operation_id, family = start(store, key, {"confirm_token": shown["confirm_token"]})
    asyncio.run(Worker(store, providers).run_operation(operation_id))
    result = rulebook.validate_result(Scheduler(FamilyService(store)).replay(operation_id, family))
    assert result["state"] == "unknown" and result["next_action"] == "reconcile; do not create again"
    sent = writes(server)
    assert sent == [("PATCH", "lesson"), ("PATCH", edited)]
    asyncio.run(Worker(store, providers).run_operation(operation_id, reconcile=True))
    assert Scheduler(FamilyService(store)).replay(operation_id, family)["state"] == "succeeded"
    # The lost write was read back, not sent again; the next exception was written once.
    assert writes(server) == [*sent, ("PATCH", iid("lesson", 4, 10, 30))]
    assert server.events[edited]["summary"] == "Edited"


# spec: ANYM-614
def test_carried_exceptions_keep_their_wall_time_across_the_dst_change(store):
    """The series starts in CEST (19 Oct) and runs into CET (25 Oct on): each exception's instance in the moved
    series is named by its own UTC start, 10:30 local on both sides."""
    key = setup(store)
    providers, server = world([master(), exception(0, status="cancelled"), exception(3, summary="Edited")])
    refs = publish(store, server)
    view = preview(store, providers, key, {**ALL, "event": refs["20261102"]})
    assert [e["series_start"] for e in view["preview"]["exceptions"]] == [
        "2026-10-19T16:30:00+08:00", "2026-11-09T17:30:00+08:00"]
    result = run(store, providers, key, {"confirm_token": view["confirm_token"]})
    assert result["state"] == "succeeded"
    assert writes(server)[1:] == [("DELETE", "lesson_20261019T083000Z"), ("PATCH", "lesson_20261109T093000Z")]
    assert server.events["lesson_20261109T093000Z"]["start"]["dateTime"] == "2026-11-09T10:30:00+01:00"


# spec: ANYM-614
def test_an_exception_the_provider_kept_is_named_with_a_next_to_inspect_it(store):
    """Read-back: Google kept the moved exception at its old identity, so a write would show it twice. That
    occurrence is reported exactly, as is one edited and reverted (its fields equal the master's, so it needs no
    field of its own, but it is read back too); the series and the cancelled exception stay written."""
    key = setup(store)
    providers, server = world([master(), exception(1, status="cancelled"), exception(3),
                               exception(4, start=week(4) + timedelta(hours=3), end=week(4) + timedelta(hours=4))],
                              keep=True)
    refs = publish(store, server)
    shown = preview(store, providers, key, {**ALL, "event": refs["20261102"]})
    result = rulebook.validate_result(run(store, providers, key, {"confirm_token": shown["confirm_token"]}))
    assert (result["state"], result["detail_state"], result["error"], result["partial"]) == (
        "failed", "partial", "series_exception_mismatch", True)
    assert [(m["original_start"], m["fate"]) for m in result["mismatches"]] == [
        ("2026-11-09T16:00:00+08:00", "moves"), ("2026-11-16T16:00:00+08:00", "kept")]
    assert result["next"] == result["mismatches"][0]["inspect"] and result["next"]["tool"] == "calendar_agenda"
    assert writes(server) == [("PATCH", "lesson"), ("DELETE", iid("lesson", 1, 10, 30))]
    polled = FamilyService(store).operation(key, result["operation"]["id"])
    assert (polled["state"], polled["mismatches"]) == ("failed", result["mismatches"])


# spec: ANYM-614
def test_guests_added_to_an_instance_after_the_series_write_stop_only_that_write(store):
    """The exception's instance is read fresh before its write: guests added there since the preview make that
    write a refusal (an edit of an event with guests), reported exactly; the other exception is still written."""
    key = setup(store)
    providers, server = world([master(), exception(1, status="cancelled"), exception(3, summary="Edited")])
    refs = publish(store, server)
    shown = preview(store, providers, key, {**ALL, "event": refs["20261102"]})
    prepare = providers.prepare

    async def guests_meanwhile(cal, step):
        if (step.get("series") or {}).get("role") == "exception" and step["target_id"] == iid("lesson", 1, 10, 30):
            server.events[step["target_id"]]["attendees"] = [{"email": "guest@example.invalid"}]
        return await prepare(cal, step)

    providers.prepare = guests_meanwhile
    result = rulebook.validate_result(run(store, providers, key, {"confirm_token": shown["confirm_token"]}))
    assert (result["state"], result["error"]) == ("failed", "series_exception_mismatch")
    assert [(m["fate"], m["error"]) for m in result["mismatches"]] == [("cancelled", "capability_unverified")]
    assert writes(server) == [("PATCH", "lesson"), ("PATCH", iid("lesson", 3, 10, 30))]
    assert server.events[iid("lesson", 1, 10, 30)].get("status") != "cancelled"


# spec: ANYM-614
@pytest.mark.parametrize("scope", ["all", "following"])
def test_the_occurrence_the_move_names_goes_to_the_requested_time_even_if_moved_before(store, scope):
    """The named occurrence was moved on its own (09:00 to 12:00); the move asks for 10:30: the preview says so
    (`named`) and it lands at 10:30 with its own title, not back at 12:00."""
    key = setup(store)
    providers, server = world([master(), exception(2, start=week(2, 12), end=week(2, 13), summary="Moved lesson")])
    refs = publish(store, server)
    move = {**ALL, "series": scope, "event": refs["20261102"]}
    view = preview(store, providers, key, move)["preview"]
    assert [(e["fate"], e["effect"]) for e in view["exceptions"]] == [("named", sched.EXCEPTION_EFFECTS["named"])]
    result = run(store, providers, key, {"confirm_token": preview(store, providers, key, move)["confirm_token"]})
    assert result["state"] == "succeeded"
    base = "lesson" if scope == "all" else plan_steps(store, result)[0]["target_id"]
    landed = server.events[iid(base, 2, 10, 30)]
    assert landed["start"]["dateTime"] == week(2, 10, 30).isoformat() and landed["summary"] == "Moved lesson"


# spec: ANYM-614
def test_a_longer_series_also_lengthens_an_edited_exception_at_the_same_start(store):
    """Same start, two hours instead of one: an exception edited without a time change gets the new length too;
    the read-back checks its times, not only its title."""
    key = setup(store)
    providers, server = world([master(), exception(3, summary="Edited")])
    refs = publish(store, server)
    move = {"action": "move", "start": "2026-11-02T09:00:00+01:00", "duration": 120, "series": "all",
            "event": refs["20261102"]}
    shown = preview(store, providers, key, move)
    assert [e["fate"] for e in shown["preview"]["exceptions"]] == ["moves"]
    assert run(store, providers, key, {"confirm_token": shown["confirm_token"]})["state"] == "succeeded"
    edited = server.events[iid("lesson", 3)]
    assert edited["end"]["dateTime"] == (week(3) + timedelta(hours=2)).isoformat() and edited["summary"] == "Edited"


# spec: ANYM-614
@pytest.mark.parametrize("events, move, code", [
    ([master()], {"start": "2026-11-03T10:30:00+01:00"}, "unsupported_recurrence"),
    # The worker looks at exceptions before the rule (ANYM-556 order): a day change with one keeps that refusal.
    ([master(), exception(3, summary="Edited")], {"start": "2026-11-03T10:30:00+01:00"}, "series_exceptions"),
    ([master(), exception(3, attendees=[{"email": "guest@example.invalid"}])], {}, "capability_unverified"),
    ([master(recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=10", "EXDATE:20261116T080000Z"]),
      exception(3, summary="Edited")], {}, "series_exceptions"),
], ids=["day-change", "day-change-with-exception", "guests", "exdate"])
def test_refusals_stay_exact_when_the_preview_read_the_series(store, events, move, code):
    key = setup(store)
    providers, server = world(events)
    refs = publish(store, server)
    shown = preview(store, providers, key, {**ALL, "event": refs["20261102"], **move})
    assert "exceptions" not in shown["preview"] and "series_exceptions" in shown["preview"]["effect"]
    result = rulebook.validate_result(run(store, providers, key, {"confirm_token": shown["confirm_token"]}))
    assert (result["state"], result["error"], writes(server)) == ("failed", code, [])
    if code == "series_exceptions":
        assert result["next"]["arguments"]["series"] == "this"


# spec: ANYM-614
def test_before_schema_seven_a_series_preview_reads_nothing_and_keeps_the_old_refusal(store):
    key = setup(store)
    providers, server = world([master(), exception(3, summary="Edited")])
    refs = publish(store, server)
    store.query(f'ALTER TABLE "{store.schema}".series_reads RENAME TO series_reads_later')
    shown = rulebook.validate_result(start(store, key, {**ALL, "event": refs["20261102"], "dry_run": True})[0])
    assert shown["state"] == "previewed" and "exceptions" not in shown["preview"]
    result = run(store, providers, key, {"confirm_token": shown["confirm_token"]})
    assert (result["error"], writes(server)) == ("series_exceptions", [])


# spec: ANYM-614
def test_a_failing_series_read_never_stops_the_workers_other_jobs(store):
    key = setup(store)
    providers, server = world([master()])
    refs = publish(store, server)
    pending = start(store, key, {**ALL, "event": refs["20261102"], "dry_run": True})[0]["preview"]["exceptions_read"]

    async def broken(cal, target):
        raise RuntimeError("synthetic")

    providers.read_series = broken
    _, operation_id, _ = start(store, key, {**ALL, "event": refs["20261102"], "request_id": "req-iso-1"})
    asyncio.run(Worker(store, providers).tick())
    assert store.one("SELECT state,error FROM series_reads") == {"state": "failed", "error": "provider_unavailable"}
    assert store.one("SELECT state FROM operations WHERE id=%s", (operation_id,))["state"] == "verified"
    # The repeat within the reuse window (made "now" here: the host may be slow) answers the read's failure.
    store.query("UPDATE series_reads SET answered_at=now() WHERE state='failed'")
    assert start(store, key, pending["next"]["arguments"])[0]["error"] == "provider_unavailable"
