"""calendar_schedule series scopes: this, following and all for recurring events, crash-safe steps (ANYM-556)."""
import asyncio
import json

import httpx
import pytest

from family_calendar.errors import CalendarError
from family_calendar.providers.google.adapter import GoogleAdapter
from family_calendar.service.core import READ, WRITE, FamilyService
from family_calendar.service.providers import Providers, normalize_google, series_ref
from family_calendar.service.schedule import Scheduler
from family_calendar.service.api import Schedule, failure
from family_calendar.service.worker import Worker
from tests.fakes import FakeGoogle
from tests.service.test_providers import GOOGLE_CAL, apply_step
from tests.service.test_schedule_intent import rulebook, setup

BERLIN = "Europe/Berlin"
UID = "lesson@example.invalid"
PLANNED = "2026-10-28T00:00:00+00:00"
# Weekly on Mondays at 09:00 Berlin from 19 Oct 2026 (CEST); DST ends on 25 Oct, so later Mondays are 08:00Z.
STARTS = {"20261019": "2026-10-19T09:00:00+02:00", "20261026": "2026-10-26T09:00:00+01:00",
          "20261102": "2026-11-02T09:00:00+01:00", "20261109": "2026-11-09T09:00:00+01:00"}
UTC_IDS = {"20261019": "20261019T070000Z", "20261026": "20261026T080000Z", "20261102": "20261102T080000Z",
           "20261109": "20261109T080000Z"}


def master(**extra):
    return {"id": "lesson", "iCalUID": UID, "etag": '"m1"', "summary": "Lesson", "location": "Pool",
            "updated": "2026-10-01T00:00:00Z", "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=10"],
            "start": {"dateTime": STARTS["20261019"], "timeZone": BERLIN},
            "end": {"dateTime": STARTS["20261019"].replace("T09", "T10"), "timeZone": BERLIN}, **extra}


def instance(day, start=None, end=None, generated=True, **extra):
    """One Monday of the series; with `start` an exception moved there (stored on its own, so listed)."""
    original = STARTS[day]
    return {"id": "lesson_" + UTC_IDS[day], "iCalUID": UID, "etag": f'"i{day}"', "summary": "Lesson",
            "recurringEventId": "lesson", "originalStartTime": {"dateTime": original, "timeZone": BERLIN},
            "start": {"dateTime": start or original, "timeZone": BERLIN},
            "end": {"dateTime": end or original.replace("T09", "T10"), "timeZone": BERLIN},
            **({"_generated": True} if generated and start is None and not extra else {}), **extra}


def series_events(*exceptions, **master_extra):
    found = {e["id"]: e for e in exceptions}
    return [master(**master_extra), *(found.get("lesson_" + UTC_IDS[d], instance(d)) for d in STARTS)]


def world(events, fail=None):
    """A Google world for calendar "A" (cal-a, connection google-a); `fail(request)` may answer instead of it."""
    server = FakeGoogle(events)

    def handle(request):
        answer = fail(request, server) if fail else None
        return answer if answer is not None else server(request)

    providers = Providers(None, None)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    providers.adapters["google-a"] = GoogleAdapter("google-a", client, lambda: "canary-token")
    return providers, server


def publish(s, server):
    """The worker's read of calendar "A": every instance (singleEvents), never the master; returns refs by day."""
    rows = [normalize_google("cal-a", e, "Asia/Makassar") for e in server.events.values() if not e.get("recurrence")]
    for row in rows:
        row["payload"] = {}
    s.publish_window("cal-a", "2026-10-01T00:00:00Z", "2026-12-31T00:00:00Z", rows, "complete")
    return {row["target"].rpartition("_")[2][:8]: row["occurrences"][0]["ref"]
            for row in rows if row["occurrences"] and row["target"].startswith("lesson_")}


def start(s, key, arguments):
    """One calendar_schedule call that may start an operation; the worker has not run it yet."""
    scheduler = Scheduler(FamilyService(s))
    try:
        result, operation_id, family = scheduler.schedule(
            key, Schedule.model_validate(arguments).model_dump(exclude_none=True))
    except CalendarError as e:
        return failure(e.code, e.field_errors, e.details), None, None
    return result, operation_id, family


def run(s, providers, key, arguments):
    result, operation_id, family = start(s, key, arguments)
    if operation_id:
        asyncio.run(Worker(s, providers).run_operation(operation_id))
        result = Scheduler(FamilyService(s)).replay(operation_id, family)
    return result


def writes(server):
    return [(r.method, r.url.path.rpartition("/")[2]) for r in server.requests if r.method != "GET"]


def plan_steps(s, result):
    op = s.one("SELECT plan_id FROM operations WHERE id=%s", (result["operation"]["id"],))
    return s.one("SELECT payload FROM plans WHERE id=%s", (op["plan_id"],))["payload"]["steps"]


MOVE = {"action": "move", "start": "2026-11-02T10:30:00+01:00", "series": "following"}


# spec: ANYM-556
def test_following_move_creates_the_replacement_first_then_ends_the_original(store):
    key = setup(store)
    # An exception whose original start is before the boundary, though it now starts after it, is not in range.
    moved = instance("20261026", "2026-11-03T10:00:00+01:00", "2026-11-03T11:00:00+01:00")
    providers, server = world(series_events(moved))
    kept = json.dumps(server.events[moved["id"]], sort_keys=True)
    refs = publish(store, server)
    preview = rulebook.validate_result(start(store, key, {**MOVE, "event": refs["20261102"], "dry_run": True})[0])
    shown = preview["preview"]
    assert shown["series"] == "following" and shown["steps"] == ["create_replacement", "end_original"]
    assert shown["original_start"] == "2026-11-02T16:00:00+08:00" and "created" in shown["effect"]
    assert writes(server) == []
    result = rulebook.validate_result(run(store, providers, key, {**MOVE, "event": refs["20261102"],
                                                                 "request_id": "req-series-1"}))
    assert result["state"] == "succeeded"
    replacement_id = plan_steps(store, result)[0]["target_id"]
    assert writes(server) == [("POST", "events"), ("PATCH", "lesson")]
    assert json.loads(server.requests[[r.method for r in server.requests].index("POST")].content)["id"] == (
        replacement_id)
    assert result["series_ref"] == series_ref("cal-a", "lesson")
    assert result["replacement"] == {"series_ref": series_ref("cal-a", replacement_id)}
    # The original ends one second before the boundary's original start; COUNT gives way to UNTIL.
    assert server.events["lesson"]["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261102T075959Z"]
    new = server.events[replacement_id]
    assert new["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=8"]
    assert new["start"] == {"dateTime": "2026-11-02T10:30:00+01:00", "timeZone": BERLIN}
    assert new["end"] == {"dateTime": "2026-11-02T11:30:00+01:00", "timeZone": BERLIN}
    assert (new["summary"], new["location"]) == ("Lesson", "Pool")
    assert json.dumps(server.events[moved["id"]], sort_keys=True) == kept


# spec: ANYM-556
@pytest.mark.asyncio
async def test_all_move_shifts_the_master_by_wall_time_across_dst():
    """A daily series from Thu 22 Oct (CEST): its Sat 24 Oct occurrence moves to Mon 26 Oct 09:00 (CET). Elapsed
    time would be 49 hours and put the series at 10:00; wall time keeps 09:00 on every day."""
    daily = master(recurrence=["RRULE:FREQ=DAILY;COUNT=20"], start={"dateTime": "2026-10-22T09:00:00+02:00",
                   "timeZone": BERLIN}, end={"dateTime": "2026-10-22T10:00:00+02:00", "timeZone": BERLIN})
    sat = {**instance("20261019"), "id": "lesson_20261024T070000Z", "etag": '"i24"',
           "originalStartTime": {"dateTime": "2026-10-24T09:00:00+02:00", "timeZone": BERLIN}}
    providers, server = world([daily, sat])
    providers.adapters["conn-g"] = providers.adapters["google-a"]
    step = {"action": "update", "ordinal": 0, "target_id": sat["id"], "expected_etag": '"i24"',
            "occurrence": {"series_ref": series_ref("cal-g", "lesson"), "original_start": "2026-10-24T07:00:00+00:00"},
            "event": {"start": "2026-10-26T09:00:00+01:00", "end": "2026-10-26T10:00:00+01:00", "timezone": BERLIN},
            "series": {"scope": "all", "planned_at": PLANNED}}
    intent, receipt = await apply_step(providers, GOOGLE_CAL, step)
    assert receipt["verified"] is True and intent["target"] == "lesson"
    assert server.events["lesson"]["start"] == {"dateTime": "2026-10-24T09:00:00+02:00", "timeZone": BERLIN}
    assert server.events["lesson"]["end"] == {"dateTime": "2026-10-24T10:00:00+02:00", "timeZone": BERLIN}
    assert server.events["lesson"]["recurrence"] == ["RRULE:FREQ=DAILY;COUNT=20"]
    # A rule that names its weekday cannot follow a date change without a rewrite: refused before any write.
    weekly, _ = world(series_events())
    weekly.adapters["conn-g"] = weekly.adapters["google-a"]
    monday = dict(step, target_id="lesson_20261102T080000Z", expected_etag='"i20261102"',
                  occurrence=dict(step["occurrence"], original_start="2026-11-02T08:00:00+00:00"),
                  event={"start": "2026-11-03T09:00:00+01:00", "end": "2026-11-03T10:00:00+01:00", "timezone": BERLIN})
    with pytest.raises(CalendarError, match="unsupported_recurrence"):
        await weekly.prepare(GOOGLE_CAL, monday)


# spec: ANYM-556
def test_an_exception_in_the_edited_range_refuses_before_any_write(store):
    """Range membership is by original start: an exception now before the boundary, originally after it."""
    key = setup(store)
    early = instance("20261109", "2026-10-30T09:00:00+01:00", "2026-10-30T10:00:00+01:00")
    providers, server = world(series_events(early))
    refs = publish(store, server)
    result = rulebook.validate_result(run(store, providers, key, {**MOVE, "event": refs["20261102"],
                                                                 "request_id": "req-series-exc"}))
    assert (result["state"], result["error"]) == ("failed", "series_exceptions")
    assert writes(server) == []
    assert result["next"] == {"tool": "calendar_schedule", "arguments": {
        "action": "move", "event": refs["20261102"], "start": MOVE["start"], "series": "this", "dry_run": True}}
    # An all move is refused by any exception; deleting the whole series is not.
    moved = run(store, providers, key, {**MOVE, "series": "all", "event": refs["20261026"],
                                        "request_id": "req-series-all"})
    assert (moved["state"], moved["error"], writes(server)) == ("failed", "series_exceptions", [])
    gone = run(store, providers, key, {"action": "cancel", "series": "all", "event": refs["20261026"],
                                       "request_id": "req-series-del"})
    assert gone["state"] == "succeeded" and writes(server) == [("DELETE", "lesson")]
    assert "lesson" not in server.events


# spec: ANYM-556
def test_following_cancel_ends_the_series_and_keeps_earlier_exceptions(store):
    key = setup(store)
    moved = instance("20261026", "2026-11-03T10:00:00+01:00", "2026-11-03T11:00:00+01:00")
    providers, server = world(series_events(moved))
    kept = json.dumps(server.events[moved["id"]], sort_keys=True)
    refs = publish(store, server)
    result = run(store, providers, key, {"action": "cancel", "series": "following", "event": refs["20261102"],
                                         "request_id": "req-series-cut"})
    assert result["state"] == "succeeded" and writes(server) == [("PATCH", "lesson")]
    assert server.events["lesson"]["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261102T075959Z"]
    assert json.dumps(server.events[moved["id"]], sort_keys=True) == kept
    # A cancelled exception from the boundary on is already gone: it never refuses a following cancel.
    providers, server = world(series_events(instance("20261109", status="cancelled")))
    refs = publish(store, server)
    assert run(store, providers, key, {"action": "cancel", "series": "following", "event": refs["20261102"],
                                       "request_id": "req-series-cut2"})["state"] == "succeeded"


# spec: ANYM-556
def test_a_moved_occurrence_is_named_by_its_original_start():
    """The plan names the occurrence by Google's originalStartTime; its current start is not its identity."""
    moved = instance("20261102", "2026-11-04T11:00:00+01:00", "2026-11-04T12:00:00+01:00")
    row = normalize_google("cal-a", moved, "Asia/Makassar")["occurrences"][0]
    assert row["details"]["original_start"] == "2026-11-02T09:00:00+01:00"
    assert row["start"].isoformat() == "2026-11-04T10:00:00+00:00"


# spec: ANYM-556
def test_a_moved_selected_occurrence_previews_its_original_start_and_is_refused_as_an_exception(store):
    key = setup(store)
    moved = instance("20261102", "2026-11-04T11:00:00+01:00", "2026-11-04T12:00:00+01:00")
    providers, server = world(series_events(moved))
    refs = publish(store, server)
    shown = start(store, key, {"action": "cancel", "series": "following", "event": refs["20261102"],
                               "dry_run": True})[0]["preview"]
    assert shown["original_start"] == "2026-11-02T16:00:00+08:00"
    assert shown["before"]["start"] == "2026-11-04T18:00:00+08:00"
    result = run(store, providers, key, {"action": "cancel", "series": "following", "event": refs["20261102"],
                                         "request_id": "req-series-moved"})
    assert (result["error"], writes(server)) == ("series_exceptions", [])


class Crash(BaseException):
    """The worker process dies here: nothing after it runs, nothing is caught."""


# spec: ANYM-556
def test_a_crash_between_writes_resumes_the_same_plan_after_the_preview_ttl(store):
    key = setup(store)
    providers, server = world(series_events())
    refs = publish(store, server)
    result, operation_id, family = start(store, key, {**MOVE, "event": refs["20261102"], "request_id": "req-crash"})
    prepare, mutate, crashes = providers.prepare, providers.mutate, ["after-post", "before-truncation"]

    async def mutate_then_die(cal, intent):
        await mutate(cal, intent)
        if intent["action"] == "create" and "after-post" in crashes:
            crashes.remove("after-post")
            raise Crash

    async def die_before_truncation(cal, step):
        if step["action"] != "create" and "before-truncation" in crashes:
            crashes.remove("before-truncation")
            raise Crash
        return await prepare(cal, step)

    providers.mutate, providers.prepare = mutate_then_die, die_before_truncation
    worker = Worker(store, providers)
    for _ in range(2):
        with pytest.raises(Crash):
            asyncio.run(worker.run_operation(operation_id))
        # Restarted after the lease and the preview TTL have both run out.
        store.query("UPDATE operations SET lease_until=now()-interval '1 second' WHERE id=%s", (operation_id,))
        store.query("UPDATE plans SET expires_at=now()-interval '1 hour' WHERE operation_id=%s", (operation_id,))
    asyncio.run(worker.tick())
    result = Scheduler(FamilyService(store)).replay(operation_id, family)
    assert result["state"] == "succeeded"
    # One replacement, under the plan's one stable ID: the resend was a read-back, never a second create.
    assert writes(server) == [("POST", "events"), ("PATCH", "lesson")]
    assert server.events["lesson"]["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261102T075959Z"]
    # An apply that never started keeps its TTL.
    late, operation_id, _ = start(store, key, {**MOVE, "event": refs["20261109"], "dry_run": True})
    store.query("UPDATE plans SET expires_at=now()-interval '1 hour' WHERE token_hash IS NOT NULL "
                "AND operation_id IS NULL", ())
    assert start(store, key, {"confirm_token": late["confirm_token"]})[0]["error"] == "preview_stale"


def changed_after_create(request, server):
    if request.method == "PATCH" and request.url.path.endswith("/lesson"):
        # Someone edits the original series between the two writes.
        server.events["lesson"]["etag"] = '"m-elsewhere"'
    return None


def rejected(request, server):
    if request.method == "PATCH" and request.url.path.endswith("/lesson"):
        return httpx.Response(400)
    return None


# spec: ANYM-556
@pytest.mark.parametrize("fail, code", [(changed_after_create, "version_conflict"), (rejected, "write_rejected")])
def test_a_refused_truncation_is_a_partial_failure_with_both_handles(store, fail, code):
    key = setup(store)
    providers, server = world(series_events(), fail)
    refs = publish(store, server)
    result = rulebook.validate_result(run(store, providers, key, {**MOVE, "event": refs["20261102"],
                                                                 "request_id": "req-partial"}))
    steps = plan_steps(store, result)
    replacement = series_ref("cal-a", steps[0]["target_id"])
    assert (result["state"], result["detail_state"], result["error"], result["partial"]) == (
        "failed", "partial", code, True)
    assert result["handles"] == {"original": {"calendar_ref": "cal-a", "series_ref": series_ref("cal-a", "lesson")},
                                 "replacement": {"calendar_ref": "cal-a", "series_ref": replacement}}
    assert result["next"]["tool"] == "calendar_agenda" and result["next"]["arguments"]["calendar_refs"] == ["cal-a"]
    assert result["undo"] == {"tool": "calendar_schedule", "arguments": {
        "action": "cancel", "event": replacement, "series": "all", "dry_run": True}}
    sent = writes(server)
    assert sent[0] == ("POST", "events") and steps[0]["target_id"] in server.events
    # A reconcile or a repeat creates nothing, deletes nothing and overwrites nothing.
    asyncio.run(Worker(store, providers).run_operation(result["operation"]["id"], reconcile=True))
    again = run(store, providers, key, {**MOVE, "event": refs["20261102"], "request_id": "req-partial"})
    assert writes(server) == sent and again["state"] == "failed" and again["partial"] is True
    # The undo previews removing the replacement, once a read has published it.
    server.events[steps[0]["target_id"] + "_20261102T093000Z"] = {
        **instance("20261102"), "id": steps[0]["target_id"] + "_20261102T093000Z", "recurringEventId": steps[0]["target_id"],
        "start": {"dateTime": "2026-11-02T10:30:00+01:00"}, "end": {"dateTime": "2026-11-02T11:30:00+01:00"},
        "originalStartTime": {"dateTime": "2026-11-02T10:30:00+01:00", "timeZone": BERLIN}}
    publish(store, server)
    undo = rulebook.validate_result(start(store, key, result["undo"]["arguments"])[0])
    assert undo["state"] == "previewed" and undo["preview"]["series"] == "all"
    assert undo["preview"]["action"] == "cancel" and writes(server) == sent


# spec: ANYM-556
def test_an_uncertain_truncation_ends_unknown_and_offers_only_reconcile(store):
    key = setup(store)

    def timeout(request, server):
        if request.method == "PATCH":
            server.requests.append(request)
            raise httpx.ReadTimeout("no answer", request=request)
        return None

    providers, server = world(series_events(), timeout)
    refs = publish(store, server)
    result = rulebook.validate_result(run(store, providers, key, {**MOVE, "event": refs["20261102"],
                                                                 "request_id": "req-unknown"}))
    assert result["state"] == "unknown" and "partial" not in result
    assert result["next"] == {"tool": "calendar_operation", "arguments": {"operation_id": result["operation"]["id"]}}
    assert result["next_action"] == "reconcile; do not create again"
    assert writes(server) == [("POST", "events"), ("PATCH", "lesson")]


# spec: ANYM-556
def test_icloud_refuses_following_and_all_with_a_next_for_this_occurrence(store):
    key = setup(store)
    store.connection("icloud-a", "adult-a", "icloud")
    store.calendar("cal-ia", "icloud-a", "/ia/", "Home iCloud", "Asia/Makassar", "write")
    store.write_opt_in("cal-ia", True)
    store.grant("family", "adult-a", "cal-ia", READ + WRITE, "details")
    key = store.agent("agent-i", "family", "adult-a", READ + WRITE, ["cal-a", "cal-ia"])
    details = {"title": "Lesson", "recurring": True, "series_ref": series_ref("cal-ia", "/ia/lesson.ics"),
               "original_start": "2026-11-02T09:00:00+01:00"}
    store.publish_window("cal-ia", "2026-11-01T00:00:00Z", "2026-11-03T00:00:00Z", [{
        "resource_ref": "res-ia", "target": "/ia/lesson.ics", "etag": '"v1"', "payload": {},
        "occurrences": [{"ref": "event-ia", "start": "2026-11-02T08:00:00Z", "end": "2026-11-02T09:00:00Z",
                         "busy": True, "details": details}]}], "complete")
    for scope in ("following", "all"):
        refusal = start(store, key, {**MOVE, "series": scope, "event": "event-ia", "dry_run": True})[0]
        assert refusal["error"] == "capability_unsupported" and refusal["capability"] == "series"
        assert refusal["provider"] == "icloud" and refusal["scopes"] == ["this"]
        assert refusal["calendar"] == {"calendar_ref": "cal-ia", "label": "Home iCloud"}
        closest = rulebook.validate_result(start(store, key, refusal["next"]["arguments"])[0])
        assert closest["state"] == "previewed" and "series" not in closest["preview"]


# spec: ANYM-556
def test_series_takes_a_recurring_move_or_cancel_only(store):
    key = setup(store)
    single = start(store, key, {"action": "cancel", "series": "all", "event": "event-dentist", "dry_run": True})[0]
    assert single["error"] == "invalid_arguments"
    assert single["field_errors"] == [{"field": "series", "reason": "not_recurring"}]
    create = start(store, key, {"action": "create", "title": "Swim", "start": "2026-11-02T10:00", "series": "all",
                                "dry_run": True})[0]
    assert create["field_errors"] == [{"field": "series", "reason": "unknown_field"}]
    # `this` of a single event is the ordinary edit.
    assert start(store, key, {"action": "cancel", "series": "this", "event": "event-dentist",
                              "dry_run": True})[0]["state"] == "previewed"
    google = FamilyService(store).capabilities(key)["constraints"]["series_write"]["google"]
    assert google["scopes"] == ["this", "following", "all"]


# spec: ANYM-556
def test_a_worker_that_predates_series_refuses_a_series_plan(store):
    """The plan's scope hash names series; a worker that hashes without it sees scope_changed, never a write to
    the one occurrence."""
    key = setup(store)
    providers, server = world(series_events())
    refs = publish(store, server)
    preview = start(store, key, {**MOVE, "event": refs["20261102"], "dry_run": True})[0]
    service = FamilyService(store)
    plan = store.one("SELECT * FROM plans WHERE token_hash IS NOT NULL ORDER BY created_at DESC LIMIT 1")
    p = service.principal(key)
    calendars = [service.authorize(key, s["calendar_ref"], "plan:create")[1] for s in plan["payload"]["steps"]]
    assert service._scope_hash(p, calendars, False, True) == plan["scope_hash"]
    assert service._scope_hash(p, calendars, False) != plan["scope_hash"]
    assert preview["state"] == "previewed"


# spec: ANYM-556
@pytest.mark.asyncio
async def test_all_day_following_cancel_ends_the_series_the_day_before():
    day = master(recurrence=["RRULE:FREQ=WEEKLY;COUNT=4"], start={"date": "2026-10-19"}, end={"date": "2026-10-20"})
    nov = {**instance("20261102"), "id": "lesson_20261102", "originalStartTime": {"date": "2026-11-02"},
           "start": {"date": "2026-11-02"}, "end": {"date": "2026-11-03"}}
    providers, server = world([day, nov])
    providers.adapters["conn-g"] = providers.adapters["google-a"]
    step = {"action": "delete", "ordinal": 0, "target_id": nov["id"], "expected_etag": nov["etag"],
            "occurrence": {"series_ref": series_ref("cal-g", "lesson"), "original_start": "2026-11-02"},
            "series": {"scope": "following", "planned_at": PLANNED}}
    intent, receipt = await apply_step(providers, GOOGLE_CAL, step)
    assert receipt["verified"] is True
    assert server.events["lesson"]["recurrence"] == ["RRULE:FREQ=WEEKLY;UNTIL=20261101"]
    # From the first occurrence on, following is the whole series: the master goes.
    first, server = world([day, {**nov, "id": "lesson_20261019", "etag": '"f"',
                                 "originalStartTime": {"date": "2026-10-19"}}])
    first.adapters["conn-g"] = first.adapters["google-a"]
    whole = dict(step, target_id="lesson_20261019", expected_etag='"f"',
                 occurrence=dict(step["occurrence"], original_start="2026-10-19"))
    await apply_step(first, GOOGLE_CAL, whole)
    assert "lesson" not in server.events
