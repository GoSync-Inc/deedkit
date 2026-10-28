"""calendar_schedule: create, move or cancel one event in one call (ANYM-552)."""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import random
import sys
import threading
import time
from uuid import uuid4

import psycopg
import pytest

from family_calendar.errors import CalendarError
from family_calendar.service import schedule as sched
from family_calendar.service.api import Gateway, Schedule, failure
from family_calendar.service.core import READ, WRITE, FamilyService
from family_calendar.service.schedule import Scheduler
from family_calendar.service.store import Store
from family_calendar.service.worker import Worker
from tests.service.test_guardian_rules import family, roles, schema_one  # noqa: F401 (fixtures)
from tests.service.test_projection import Fake

ROOT = Path(__file__).resolve().parents[4]
# The calendar image ships without the shared package; it is standard library only, so tests read it from the repo.
sys.path.insert(0, str(ROOT / "packages/platform-contracts/src"))
from platform_contracts import agent as rulebook  # noqa: E402
from platform_contracts.agent_conformance import CASES, run_matrix  # noqa: E402

WINDOW = ("2026-10-20T00:00:00Z", "2026-10-21T00:00:00Z")
DENTIST = ("2026-10-20T02:00:00Z", "2026-10-20T03:00:00Z")  # 10:00-11:00 in Asia/Makassar
CREATE = {"action": "create", "title": "Swimming", "start": "2026-11-02T10:00"}


class Provider(Fake):
    """The projection tests' fake provider, also for moves and cancels: the intent keeps the step's action."""

    async def prepare(self, cal, step):
        target = step["target_id"]
        return {"action": step["action"], "target": target, "desired": {"summary": step.get("event", {}).get("title")},
                "expected_etag": step.get("expected_etag"), "resource_ref": "res-" + target}


def events(s, *rows, calendar="cal-a"):
    s.publish_window(calendar, *WINDOW, [{
        "resource_ref": "res-" + ref, "target": "provider-" + ref, "etag": '"v1"', "payload": {},
        "occurrences": [{"ref": ref, "start": start, "end": end, "busy": True, "details": {"title": title}}],
    } for ref, title, start, end in rows], "complete")


def setup(s, *, second=False):
    """adult-a's agent writes to calendar "A" (cal-a, Asia/Makassar); with `second` also to "Lessons"."""
    family(s)
    s.grant("family", "adult-a", "cal-a", READ + WRITE, "details")
    refs = ["cal-a"]
    if second:
        s.write_opt_in("cal-child", True)
        s.grant("family", "adult-a", "cal-child", READ + WRITE, "details")
        refs.append("cal-child")
        events(s, calendar="cal-child")
    events(s, ("event-dentist", "Dentist", *DENTIST))
    return s.agent("agent", "family", "adult-a", READ + WRITE, refs)


def call(s, key, arguments, fake=None, tool=sched.TOOL):
    """One synchronous calendar_schedule call; a started operation is run once by the worker, then replayed."""
    scheduler = Scheduler(FamilyService(s))
    try:
        result, operation_id, family_ = scheduler.schedule(
            key, Schedule.model_validate(arguments).model_dump(exclude_none=True), tool)
    except CalendarError as e:
        return failure(e.code, e.field_errors, e.details)
    if operation_id and fake is not None:
        asyncio.run(Worker(s, fake).run_operation(operation_id))
        deadline = time.monotonic() + 10
        while not scheduler.settled(operation_id) and time.monotonic() < deadline:
            time.sleep(0.05)
        result = scheduler.replay(operation_id, family_)
    return result


def at(value):
    return datetime.fromisoformat(value)


def plan_of(s, result):
    op = s.one("SELECT plan_id FROM operations WHERE id=%s", (result["operation"]["id"],))
    return s.one("SELECT payload FROM plans WHERE id=%s", (op["plan_id"],))["payload"]["steps"][0]


class Harness:
    """The ANYM-544 matrix over calendar storage: "alpha" is calendar "A"; drift bumps its version (scope)."""

    def __init__(self, s):
        self.s, self.fake = s, Provider()
        self.keys = {"p1": setup(s), "p2": s.agent("agent-2", "family", "adult-a", READ + WRITE, ["cal-a"])}

    def call(self, principal, tool, arguments):
        if "target" in arguments:
            arguments = {k: v for k, v in arguments.items() if k not in ("target", "text")} | {
                "action": "create", "title": arguments["text"], "start": "2026-11-02T10:00",
                "calendar": {"alpha": "A"}[arguments["target"]]}
        return call(self.s, self.keys[principal], arguments, self.fake, sched.TOOL if tool == "t_send" else tool)

    def advance(self, seconds):
        self.s.query("UPDATE plans SET expires_at=expires_at-make_interval(secs=>%s)", (seconds,))

    def drift(self):
        self.s.write_opt_in("cal-a", True)
        return ["scope"]

    def revoke(self, principal, tool):
        self.s.grant("family", "adult-a", "cal-a", READ, "details")

    def crash_after_binding(self):
        self.fake.lose.add("cal-a")

    @property
    def effects(self):
        return len(self.fake.sent)


# spec: ANYM-552
def test_constants_match_the_rulebook():
    assert sched.REQUEST_ID.pattern == rulebook.REQUEST_ID.pattern
    assert sched.PLACEHOLDER_REQUEST_ID == rulebook.PLACEHOLDER_REQUEST_ID
    assert sched.MAX_WAIT_S == rulebook.MAX_WAIT_S and sched.MAX_CANDIDATES == rulebook.MAX_CANDIDATES
    assert sched.STATES == rulebook.STATUS_STATES["calendar.operation"]
    assert set(sched.STATES) - sched.SETTLED == {"queued", "running"}


# spec: ANYM-552
def test_conformance_matrix_runs_on_calendar_storage(store):
    # The matrix's case_expiry wants preview_expired; this card answers an expired plan with preview_stale and a
    # fresh plan (test_expired_plan_is_stale_with_a_fresh_plan).
    cases, stores = sorted(set(CASES) - {"expiry"}), []

    def make():
        stores.append(Store(store.dsn, schema="test_" + uuid4().hex))
        stores[-1].migrate()
        return Harness(stores[-1])

    try:
        assert run_matrix(make, cases) == cases
    finally:
        for s in stores:
            s.drop_test_schema()
            s.close()


# spec: ANYM-552
def test_one_call_creates_in_the_default_calendar(store):
    key, fake = setup(store), Provider()
    result = rulebook.validate_result(call(store, key, {**CREATE, "request_id": "req-create-1"}, fake))
    assert result["state"] == "succeeded" and result["detail_state"] == "verified"
    assert result["event_ref"] and result["request_id"] == "req-create-1" and result["next"] is None
    step = plan_of(store, result)
    assert step["calendar_ref"] == "cal-a"
    assert at(step["event"]["start"]) == at("2026-11-02T10:00:00+08:00")
    assert at(step["event"]["end"]) == at("2026-11-02T10:30:00+08:00")
    assert call(store, key, {**CREATE, "request_id": "req-create-1"}, fake) == result
    assert len(fake.sent) == 1


# spec: ANYM-552
def test_named_or_default_calendar_and_ambiguous_names(store):
    key, fake = setup(store, second=True), Provider()
    # Two writable calendars and no family default: the caller chooses, nothing is planned.
    choose = rulebook.validate_error(call(store, key, {**CREATE, "request_id": "req-cal-1"}, fake))
    assert choose["error"] == "selection_required" and "next" not in choose
    assert {c["id"] for c in choose["candidates"]} == {"cal-a", "cal-child"}
    assert all(c["next"]["arguments"]["dry_run"] is True for c in choose["candidates"])
    part = rulebook.validate_error(call(store, key, {**CREATE, "calendar": "Less", "request_id": "req-cal-2"}, fake))
    assert part["error"] == "selection_required" and [c["id"] for c in part["candidates"]] == ["cal-child"]
    named = call(store, key, {**CREATE, "calendar": "lessons", "request_id": "req-cal-3"}, fake)
    assert named["state"] == "succeeded" and plan_of(store, named)["calendar_ref"] == "cal-child"
    store.family_settings("family", "Europe/Berlin", "cal-child")
    default = call(store, key, {**CREATE, "request_id": "req-cal-4"}, fake)
    step = plan_of(store, default)
    assert step["calendar_ref"] == "cal-child" and at(step["event"]["start"]) == at("2026-11-02T10:00:00+01:00")
    assert len(fake.sent) == 2


# spec: ANYM-552
def test_dry_run_token_applies_exactly_the_plan_once(store):
    key, fake = setup(store), Provider()
    preview = rulebook.validate_result(call(store, key, {**CREATE, "duration": 45, "dry_run": True}, fake))
    assert preview["state"] == "previewed" and not fake.sent
    assert preview["preview"]["start"] == "2026-11-02T10:00:00+08:00"
    assert preview["preview"]["end"] == "2026-11-02T10:45:00+08:00"
    assert preview["preview"]["calendar"] == {"calendar_ref": "cal-a", "label": "A"}
    assert preview["next"] == {"tool": "calendar_schedule", "arguments": {"confirm_token": preview["confirm_token"]}}
    token = preview["confirm_token"]
    stored = store.one("SELECT id,payload FROM plans WHERE token_hash=%s", (sched.token_hash(token),))
    first = call(store, key, {"confirm_token": token, "request_id": "req-token-1"}, fake)
    assert first["state"] == "succeeded"
    assert store.one("SELECT plan_id FROM operations WHERE id=%s", (first["operation"]["id"],))["plan_id"] == stored["id"]
    again = call(store, key, {"confirm_token": token, "request_id": "req-token-2"}, fake)
    assert again == first and len(fake.sent) == 1
    assert store.one("SELECT count(*) AS n FROM operations")["n"] == 1
    # A token never applies another request than the one it previewed.
    other = call(store, key, {**CREATE, "title": "Other", "confirm_token": token}, fake)
    assert other["error"] == "invalid_arguments" and other["field_errors"][0]["field"] == "confirm_token"


# spec: ANYM-552
def test_expired_plan_is_stale_with_a_fresh_plan(store):
    key, fake = setup(store), Provider()
    token = call(store, key, {**CREATE, "dry_run": True})["confirm_token"]
    store.query("UPDATE plans SET expires_at=now()-interval '1 second'")
    stale = rulebook.validate_error(call(store, key, {"confirm_token": token}, fake))
    assert stale["error"] == "preview_stale" and stale["changed"] == ["expires_at"]
    assert stale["confirm_token"] != token and stale["preview"]["start"] == "2026-11-02T10:00:00+08:00"
    assert not fake.sent
    assert call(store, key, {"confirm_token": stale["confirm_token"]}, fake)["state"] == "succeeded"
    # A token-only apply has no caller request_id to show.
    assert "request_id" not in call(store, key, {"confirm_token": stale["confirm_token"]}, fake)
    # A consumed plan replays its operation after expiry too.
    store.query("UPDATE plans SET expires_at=now()-interval '1 second' WHERE operation_id IS NOT NULL")
    assert call(store, key, {"confirm_token": stale["confirm_token"]}, fake)["state"] == "succeeded"
    assert len(fake.sent) == 1


# spec: ANYM-552
def test_changed_calendar_or_scope_is_stale(store):
    key, fake = setup(store), Provider()
    token = call(store, key, {**CREATE, "dry_run": True})["confirm_token"]
    store.family_settings("family", "Europe/Berlin")
    moved = rulebook.validate_error(call(store, key, {"confirm_token": token}, fake))
    assert moved["error"] == "preview_stale" and moved["changed"] == ["times"]
    store.write_opt_in("cal-a", True)
    scope = rulebook.validate_error(call(store, key, {"confirm_token": moved["confirm_token"]}, fake))
    assert scope["error"] == "preview_stale" and scope["changed"] == ["scope"] and not fake.sent


# spec: ANYM-552
def test_move_and_cancel_act_on_the_event_ref(store):
    key, fake = setup(store), Provider()
    preview = call(store, key, {"action": "move", "event": "event-dentist", "start": "2026-10-21T15:00",
                                "dry_run": True})
    assert preview["preview"]["action"] == "move" and preview["preview"]["event_ref"] == "event-dentist"
    assert preview["preview"]["start"] == "2026-10-21T15:00:00+08:00"
    assert preview["preview"]["end"] == "2026-10-21T16:00:00+08:00"
    assert preview["preview"]["before"]["start"] == "2026-10-20T10:00:00+08:00"
    moved = call(store, key, {"confirm_token": preview["confirm_token"]}, fake)
    assert moved["state"] == "succeeded" and moved["event_ref"] == "event-dentist"
    assert plan_of(store, moved)["action"] == "update"
    cancel = call(store, key, {"action": "cancel", "event": "event-dentist", "request_id": "req-cancel-1"}, fake)
    assert cancel["state"] == "succeeded" and plan_of(store, cancel)["action"] == "delete"
    missing = call(store, key, {"action": "cancel", "event": "event-gone", "request_id": "req-cancel-2"}, fake)
    assert missing["error"] == "not_found_or_forbidden" and len(fake.sent) == 2


# spec: ANYM-552
def test_title_and_date_only_select(store):
    key, fake = setup(store), Provider()
    for action, extra in (("move", {"start": "2026-10-21T15:00"}), ("cancel", {})):
        found = call(store, key, {"action": action, "event": {"title": "dentist", "date": "2026-10-20"},
                                  "request_id": "req-select-1", **extra}, fake)
        rulebook.validate_error(found, read_only={"calendar_agenda"})
        # Also when exactly one event matches: the caller confirms by event_ref.
        assert found["error"] == "selection_required" and "next" not in found
        [candidate] = found["candidates"]
        assert candidate["id"] == "event-dentist" and candidate["label"] == "Tue 20 Oct 10:00–11:00"
        assert candidate["next"] == {"tool": "calendar_schedule", "arguments": {
            "action": action, "event": "event-dentist", **extra, "dry_run": True}}
    none = call(store, key, {"action": "cancel", "event": {"title": "Ballet", "date": "2026-10-20"},
                             "request_id": "req-select-2"}, fake)
    assert none["error"] == "event_not_found" and none["next"]["tool"] == "calendar_agenda"
    assert not fake.sent and store.one("SELECT count(*) AS n FROM operations")["n"] == 0


# spec: ANYM-552
def test_live_call_needs_request_id_or_token(store):
    key, fake = setup(store), Provider()
    missing = rulebook.validate_error(call(store, key, dict(CREATE), fake))
    assert missing["error"] == "invalid_arguments" and missing["field_errors"][0]["field"] == "request_id"
    assert missing["next"]["arguments"]["request_id"] == rulebook.PLACEHOLDER_REQUEST_ID
    assert call(store, key, missing["next"]["arguments"], fake)["error"] == "invalid_arguments"
    assert not fake.sent


# spec: ANYM-552
def test_uncertain_apply_is_unknown_and_never_creates_again(store):
    key, fake = setup(store), Provider(lose={"cal-a"})
    first = rulebook.validate_result(call(store, key, {**CREATE, "request_id": "req-lost-1"}, fake))
    assert first["state"] == "unknown" and first["operation"]["module"] == "calendar"
    assert first["next"] == {"tool": "calendar_operation", "arguments": {"operation_id": first["operation"]["id"]}}
    assert first["next_action"] == "reconcile; do not create again"
    assert call(store, key, {**CREATE, "request_id": "req-lost-1"}, fake) == first
    assert call(store, key, {**CREATE, "title": "Other", "request_id": "req-lost-1"}, fake)["error"] == "idempotency_conflict"
    assert len(fake.sent) == 1


# spec: ANYM-552
def test_gateway_waits_for_the_provider_and_states_timezone_and_now(store):
    key, fake = setup(store), Provider()
    gateway = Gateway(store)
    slow = asyncio.run(gateway.call(key, "schedule", {**CREATE, "request_id": "req-wait-1", "wait_s": 0}))
    rulebook.validate_result(slow)
    assert slow["state"] == "pending" and slow["next"]["tool"] == "calendar_operation"
    assert slow["timezone"] == "Asia/Makassar" and slow["now"].endswith("+08:00")

    def work():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not store.one(
                "SELECT 1 AS ok FROM operations WHERE request_id='req-wait-2' AND state='verified'"):
            for row in store.query("SELECT id FROM operations WHERE state='queued'"):
                asyncio.run(Worker(store, fake).run_operation(row["id"]))
            time.sleep(0.05)

    worker = threading.Thread(target=work)
    worker.start()
    done = asyncio.run(gateway.call(key, "schedule", {**CREATE, "title": "Tennis", "request_id": "req-wait-2",
                                                      "wait_s": 5}))
    worker.join(10)
    assert done["state"] == "succeeded" and done["event_ref"] and done["timezone"] == "Asia/Makassar"
    preview = asyncio.run(gateway.call(key, "schedule", {**CREATE, "dry_run": True}))
    assert preview["state"] == "previewed" and preview["timezone"] == "Asia/Makassar" and "now" in preview
    with pytest.raises(CalendarError) as caught:
        asyncio.run(gateway.call(key, "schedule", {**CREATE, "wait_s": 26, "request_id": "req-wait-3"}))
    assert caught.value.code == "invalid_request"


# spec: ANYM-552
@pytest.mark.parametrize("seed", [5, 52])
def test_random_sequences_keep_one_plan_one_operation(store, seed):
    """A seeded state machine: previews, token and keyed applies, drift and expiry in random order."""
    rng, key, fake = random.Random(seed), setup(store), Provider()
    tokens, by_token, by_id, operations = [], {}, {}, set()
    titles, ids = ["Swim", "Chess"], ["req-rand-1", "req-rand-2", "req-rand-3"]
    for _ in range(40):
        move = rng.choice(["preview", "token", "token", "keyed", "drift", "expire"])
        if move == "preview":
            tokens.append(call(store, key, {**CREATE, "title": rng.choice(titles), "dry_run": True})["confirm_token"])
        elif move == "token" and tokens:
            token, request_id = rng.choice(tokens), rng.choice([None, *ids])
            result = call(store, key, {"confirm_token": token, **({"request_id": request_id} if request_id else {})},
                          fake)
            if "operation" in result:
                op = result["operation"]["id"]
                assert by_token.setdefault(token, op) == op, "a token binds one operation"
                if request_id and op not in operations:
                    assert by_id.setdefault(request_id, op) == op, "a request_id binds one operation"
                operations.add(op)
            elif result["error"] == "idempotency_conflict":
                assert request_id in by_id and by_id[request_id] != by_token.get(token), "only a taken request_id"
            else:
                assert result["error"] == "preview_stale" and token not in by_token, result
                tokens.append(result["confirm_token"])
        elif move == "keyed":
            request_id, title = rng.choice(ids), rng.choice(titles)
            result = call(store, key, {**CREATE, "title": title, "request_id": request_id}, fake)
            if "operation" in result:
                op = result["operation"]["id"]
                assert by_id.setdefault(request_id, op) == op, "a request_id binds one operation"
                operations.add(op)
            else:
                assert result["error"] == "idempotency_conflict", result
        elif move == "drift":
            store.write_opt_in("cal-a", True)
        elif move == "expire":
            store.query("UPDATE plans SET expires_at=now()-interval '1 second' WHERE operation_id IS NULL")
    assert len(fake.sent) == len(operations) == store.one("SELECT count(*) AS n FROM operations")["n"]


# spec: ANYM-552
def test_runtime_gateway_role_schedules_after_db_roles(schema_one, roles):
    apply_roles, connect = roles
    schema_one.migrate()
    key = setup(schema_one, second=True)
    apply_roles()
    gateway_store, worker_store, fake = connect("gateway"), connect("worker"), Provider()
    scheduler = Scheduler(FamilyService(gateway_store))
    preview = scheduler.schedule(key, {**CREATE, "calendar": "A", "dry_run": True})[0]
    result, operation_id, family_ = scheduler.schedule(key, {"confirm_token": preview["confirm_token"]})
    asyncio.run(Worker(worker_store, fake).run_operation(operation_id))
    assert scheduler.replay(operation_id, family_)["state"] == "succeeded"
    assert scheduler.schedule(key, {**CREATE, "calendar": "A", "title": "Chess", "request_id": "req-role-1"})[1]
    with pytest.raises(CalendarError) as caught:
        scheduler.schedule(key, {**CREATE, "request_id": "req-role-2"})
    assert caught.value.code == "selection_required"
    chosen = caught.value.details["candidates"][0]["next"]["arguments"]
    assert scheduler.schedule(key, chosen)[0]["state"] == "previewed"
    # The runtime role marks and binds plans; what a plan does stays insert-only.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with gateway_store.transaction() as c:
            c.execute("UPDATE plans SET payload='{}'::jsonb WHERE id=%s", (result["operation"]["id"],))


# spec: ANYM-552
def test_distinct_concurrent_schedules_never_drain_the_pool(store, monkeypatch):
    key, n = setup(store), 8  # the store's pool max_size
    barrier, lock = threading.Barrier(n, timeout=10), Scheduler.lock

    def held(self, c, *parts):
        lock(self, c, *parts)
        barrier.wait()  # every caller holds its transaction at once, then plans and validates inside it

    monkeypatch.setattr(Scheduler, "lock", held)
    results = [None] * n

    def one(i):
        results[i] = call(store, key, {**CREATE, "title": "Swim %d" % i, "request_id": "req-pool-%d" % i})

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert all(r and r.get("state") == "pending" for r in results), results
    assert time.monotonic() - started < 10
    assert FamilyService(store).calendars(key)["calendars"]


# spec: ANYM-552
def test_calendar_candidates_preview_the_stored_request_without_the_title(store):
    key, fake = setup(store, second=True), Provider()
    choose = rulebook.validate_error(call(store, key, {**CREATE, "duration": 45, "request_id": "req-sel-1"}, fake))
    assert choose["error"] == "selection_required" and "Swimming" not in json.dumps(choose)
    nexts = {c["id"]: c["next"]["arguments"] for c in choose["candidates"]}
    assert set(nexts["cal-child"]) == {"selection_token", "calendar", "dry_run"}
    preview = call(store, key, nexts["cal-child"], fake)
    assert preview["state"] == "previewed" and preview["preview"]["title"] == "Swimming"
    assert preview["preview"]["calendar"]["calendar_ref"] == "cal-child"
    assert preview["preview"]["end"] == "2026-11-02T10:45:00+08:00"
    other = store.agent("agent-2", "family", "adult-a", READ + WRITE, ["cal-a", "cal-child"])
    assert call(store, other, nexts["cal-a"])["error"] == "invalid_selection_token"
    store.query("UPDATE schedule_selections SET expires_at=now()-interval '1 second' WHERE expires_at>now()")
    assert call(store, key, nexts["cal-a"])["error"] == "invalid_selection_token"
    assert not fake.sent


# spec: ANYM-552
def test_offset_free_start_adds_elapsed_minutes_across_dst(store):
    key = setup(store)
    store.family_settings("family", "Europe/Berlin")
    spring = call(store, key, {**CREATE, "start": "2026-03-29T01:30", "duration": 120, "dry_run": True})
    assert spring["preview"]["start"] == "2026-03-29T01:30:00+01:00"
    assert spring["preview"]["end"] == "2026-03-29T04:30:00+02:00"
    autumn = call(store, key, {"action": "move", "event": "event-dentist", "start": "2026-10-25T01:30",
                               "duration": 120, "dry_run": True})
    assert autumn["preview"]["start"] == "2026-10-25T01:30:00+02:00"
    assert autumn["preview"]["end"] == "2026-10-25T02:30:00+01:00"


# spec: ANYM-552
def test_all_day_move_keeps_its_days_across_dst(store):
    key = setup(store)
    store.calendar("cal-a", "google-a", "private-a", "A", "Europe/Berlin", "write")
    # Saturday 28 and Sunday 29 March in Berlin: 47 hours between the two local midnights.
    store.publish_window("cal-a", "2026-03-27T00:00:00Z", "2026-03-31T00:00:00Z", [{
        "resource_ref": "res-trip", "target": "provider-trip", "etag": '"v1"', "payload": {},
        "occurrences": [{"ref": "event-trip", "start": "2026-03-27T23:00:00Z", "end": "2026-03-29T22:00:00Z",
                         "busy": True, "details": {"title": "Trip", "all_day": True}}],
    }], "complete")
    moved = call(store, key, {"action": "move", "event": "event-trip", "start": "2026-04-10", "dry_run": True})
    assert moved["preview"]["start"] == "2026-04-10" and moved["preview"]["end"] == "2026-04-12"


# spec: ANYM-552
def test_a_read_only_twin_name_needs_a_choice(store):
    setup(store)
    store.calendar("cal-twin", "google-b", "twin", "A", "Asia/Makassar", "read")
    store.grant("family", "adult-a", "cal-twin", READ, "details")
    key = store.agent("agent-twin", "family", "adult-a", READ + WRITE, ["cal-a", "cal-twin"])
    twin = rulebook.validate_error(call(store, key, {**CREATE, "calendar": "A", "request_id": "req-twin-1"}))
    assert twin["error"] == "selection_required" and [c["id"] for c in twin["candidates"]] == ["cal-a"]
    assert call(store, key, {**CREATE, "calendar": "cal-twin", "dry_run": True})["error"] == "not_found_or_forbidden"


# spec: ANYM-552
def test_renamed_or_revoked_calendar_is_stale(store):
    key, fake = setup(store, second=True), Provider()
    token = call(store, key, {**CREATE, "calendar": "Lessons", "dry_run": True})["confirm_token"]
    store.calendar("cal-child", "google-a", "child", "Lessons 2", "Asia/Makassar", "write")
    renamed = rulebook.validate_error(call(store, key, {"confirm_token": token}, fake))
    assert renamed["error"] == "preview_stale" and renamed["changed"] == ["calendar"]
    assert renamed["preview"]["calendar"] == {"calendar_ref": "cal-child", "label": "Lessons 2"}
    store.grant("family", "adult-a", "cal-child", READ, "details")
    # A revoked calendar is no staleness: the token is refused (ANYM-544 matrix case_revocation).
    revoked = rulebook.validate_error(call(store, key, {"confirm_token": renamed["confirm_token"]}, fake))
    assert revoked["error"] == "not_found_or_forbidden" and not fake.sent


# spec: ANYM-552
def test_errors_state_timezone_and_now(store):
    key = setup(store, second=True)
    gateway = Gateway(store)
    with pytest.raises(CalendarError) as caught:
        asyncio.run(gateway.call(key, "schedule", {**CREATE, "request_id": "req-ctx-1"}))
    relayed = failure(caught.value.code, caught.value.field_errors, caught.value.details)
    assert relayed["error"] == "selection_required" and relayed["timezone"] == "Asia/Makassar"
    assert relayed["now"].endswith("+08:00")
    token = asyncio.run(gateway.call(key, "schedule", {**CREATE, "calendar": "A", "dry_run": True}))["confirm_token"]
    store.query("UPDATE plans SET expires_at=now()-interval '1 second' WHERE operation_id IS NULL")
    with pytest.raises(CalendarError) as caught:
        asyncio.run(gateway.call(key, "schedule", {"confirm_token": token}))
    stale = failure(caught.value.code, caught.value.field_errors, caught.value.details)
    assert stale["error"] == "preview_stale" and stale["timezone"] == "Asia/Makassar" and "now" in stale
