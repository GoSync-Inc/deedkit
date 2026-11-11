"""ANYM-576: operator-configured scheduled refresh and `calendar.event.starting` reminders. Provider fixtures only."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from family_calendar.errors import CalendarError
from family_calendar.service import cli, refresh
from family_calendar.service import migration_rollout as rollout
from family_calendar.service import store as store_module
from family_calendar.service.core import EVENTS, FamilyService
from family_calendar.service.providers import normalize_google, source_ref
from family_calendar.service.worker import Worker
from tests.service.test_guardian_rules import roles, schema_one  # noqa: F401 (fixtures)

NOW = datetime(2026, 11, 3, 7, 0, tzinfo=timezone.utc)


def event(id, start, end=None, **extra):
    end = end or start + timedelta(hours=1)
    return {"id": id, "status": "confirmed", "etag": '"1"', "summary": "Lesson",
            "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}, **extra}


def ref(id, cal="cal-a"):
    return source_ref("event", source_ref("res", cal, id), "master")


class Provider:
    """Google-shaped window reads of a fixed provider state; every read is recorded."""

    def __init__(self, events=(), error=None, hang=False):
        self.events, self.error, self.hang, self.reads = list(events), error, hang, []

    async def read_window(self, cal, start, end):
        self.reads.append((cal["ref"], start, end))
        if self.hang:
            await asyncio.sleep(3600)
        if self.error:
            raise CalendarError(self.error)
        return [normalize_google(cal["ref"], e, "UTC") for e in self.events]

    async def close(self):
        pass


def worker(s, provider):
    w = Worker(s, provider)
    w.refresh_spacing = 0
    return w


def configure(s, *argv):
    return cli.dispatch(cli.parser().parse_args(["refresh-calendar", *argv]), None, None, {}, s)


def starting(s):
    return s.query("SELECT event_ref,start_at,lead_minutes FROM starting_reminders ORDER BY seq")


def page(s):
    if not hasattr(s, "router_key"):
        s.router_key = s.agent("router", "family", "adult-a", [EVENTS], ["cal-a"])
    return FamilyService(s).events_page(s.router_key, 0, 100)


# spec: ANYM-576
def test_only_an_operator_configured_calendar_is_refreshed_with_the_default_window_and_cadence(household):
    s = household
    s.calendar("cal-b", "google-a", "private-b", "Other", "UTC", "read")
    assert configure(s, "--calendar", "cal-a") == {
        "calendar_ref": "cal-a", "window_days": 14, "cadence_minutes": 5, "leads": [15]}
    provider = Provider()
    w = worker(s, provider)
    assert asyncio.run(w.refresh_one(NOW)) is True
    assert provider.reads == [("cal-a", NOW, NOW + timedelta(days=14))]
    # Not due again before its cadence; cal-b has no configuration and is never read.
    assert asyncio.run(w.refresh_one(NOW + timedelta(minutes=4))) is False
    assert asyncio.run(w.refresh_one(NOW + timedelta(minutes=5))) is True
    assert [r[0] for r in provider.reads] == ["cal-a", "cal-a"]
    assert configure(s, "--calendar", "cal-a", "--off") == {"calendar_ref": "cal-a", "refresh": "off"}
    assert asyncio.run(w.refresh_one(NOW + timedelta(hours=1))) is False


# spec: ANYM-576
def test_refresh_configuration_is_validated_and_names_an_existing_calendar(household):
    s = household
    with pytest.raises(CalendarError, match="calendar_not_found"):
        configure(s, "--calendar", "nope")
    for bad in (["--window-days", "0"], ["--window-days", "61"], ["--cadence-minutes", "0"], ["--lead", "0"],
                ["--lead", "1441"], ["--lead", "5", "--lead", "5"]):
        with pytest.raises(CalendarError, match="invalid_refresh_configuration"):
            configure(s, "--calendar", "cal-a", *bad)
    assert configure(s, "--calendar", "cal-a", "--window-days", "7", "--cadence-minutes", "10", "--lead", "60",
                     "--lead", "15")["leads"] == [15, 60]
    asyncio.run(worker(s, Provider()).refresh_one(NOW))
    [listed] = json.loads(json.dumps(cli.dispatch(cli.parser().parse_args(["refresh-list"]), None, None, {}, s)))[
        "calendars"]
    assert (listed["window_days"], listed["cadence_minutes"], listed["leads"], listed["health"]) == (7, 10, [15, 60], "ok")
    assert datetime.fromisoformat(listed["last_ok_at"]) == NOW


# spec: ANYM-576
def test_one_calendar_per_pass_and_a_stated_spacing_between_refreshes(household):
    s = household
    s.calendar("cal-b", "google-a", "private-b", "Other", "UTC", "read")
    configure(s, "--calendar", "cal-a")
    configure(s, "--calendar", "cal-b")
    provider = Provider()
    w = Worker(s, provider)
    assert w.refresh_spacing == refresh.SPACING_S
    clock = iter([100.0, 100.0 + refresh.SPACING_S - 1, 100.0 + refresh.SPACING_S])
    w.clock = lambda: next(clock)
    assert asyncio.run(w.refresh_one(NOW)) is True
    assert len(provider.reads) == 1
    assert asyncio.run(w.refresh_one(NOW)) is False
    assert asyncio.run(w.refresh_one(NOW)) is True
    assert sorted(r[0] for r in provider.reads) == ["cal-a", "cal-b"]


# spec: ANYM-576
def test_the_refresh_runs_after_the_operation_and_a_hung_provider_is_bounded(household, monkeypatch):
    s = household
    configure(s, "--calendar", "cal-a")
    order = []
    w = worker(s, Provider(hang=True))

    async def run_operation(operation_id, reconcile=False):
        order.append("operation")

    real = w.refresh_one

    async def refresh_one(now=None):
        order.append("refresh")
        return await real(now)

    monkeypatch.setattr(w, "run_operation", run_operation)
    monkeypatch.setattr(w, "refresh_one", refresh_one)
    monkeypatch.setattr(refresh, "REFRESH_S", 0.2)
    monkeypatch.setattr(w.store, "one", lambda q, a=(): {"id": "op"} if "FROM operations" in q
                        else store_module.Store.one(s, q, a))
    started = asyncio.run(asyncio.wait_for(_timed(w.tick), 5))
    assert order == ["operation", "refresh"]
    assert started < 2
    row = s.one("SELECT failures,error,health FROM refresh_calendars WHERE calendar_ref='cal-a'")
    assert row == {"failures": 1, "error": "provider_timeout", "health": "ok"}


async def _timed(step):
    loop = asyncio.get_running_loop()
    begin = loop.time()
    await step()
    return loop.time() - begin


# spec: ANYM-576
def test_a_failing_refresh_is_not_an_event_and_a_tick_survives_it(household):
    s = household
    configure(s, "--calendar", "cal-a")
    lesson = event("lesson", NOW + timedelta(days=1))
    asyncio.run(worker(s, Provider([lesson])).refresh_one(NOW))
    asyncio.run(worker(s, Provider([])).refresh_one(NOW + timedelta(minutes=5)))
    head = s.one("SELECT head FROM journal_head")["head"]
    for code in ("provider_unavailable", "coverage_incomplete"):
        w = worker(s, Provider(error=code))
        assert asyncio.run(w.refresh_one(NOW + timedelta(hours=1))) is True
        s.query("UPDATE refresh_calendars SET next_at=%s", (NOW,))
    assert s.one("SELECT head FROM journal_head")["head"] == head
    assert s.one("SELECT failures,error,health FROM refresh_calendars") == {
        "failures": 2, "error": "coverage_incomplete", "health": "ok"}
    assert s.one("SELECT status FROM coverage WHERE calendar_ref='cal-a'")["status"] == "blocked"


# spec: ANYM-576
def test_a_persistent_auth_failure_raises_calendar_health_until_a_refresh_succeeds(household):
    s = household
    configure(s, "--calendar", "cal-a")
    asyncio.run(worker(s, Provider()).refresh_one(NOW))
    assert page(s)["events"] == []
    failing = worker(s, Provider(error="reauth_required"))
    asyncio.run(failing.refresh_one(NOW + timedelta(minutes=5)))
    # One refused token is not yet persistent.
    assert s.one("SELECT health FROM refresh_calendars")["health"] == "ok"
    asyncio.run(failing.refresh_one(NOW + timedelta(minutes=10)))
    row = s.one("SELECT health,health_since FROM refresh_calendars")
    assert row["health"] == "auth_failed" and row["health_since"] == NOW + timedelta(minutes=10)
    # The router reads a refusal (non-200) as the per-source module_unreachable; nothing is journaled.
    with pytest.raises(CalendarError, match="calendar_auth_failed"):
        page(s)
    asyncio.run(worker(s, Provider()).refresh_one(NOW + timedelta(minutes=15)))
    assert s.one("SELECT health,health_since,failures FROM refresh_calendars") == {
        "health": "ok", "health_since": None, "failures": 0}
    assert page(s)["events"] == []


def remind(s, now):
    return refresh.remind(s, now)


def refreshed(s, events, now, leads=("15",)):
    configure(s, "--calendar", "cal-a", *[x for lead in leads for x in ("--lead", lead)])
    s.query("UPDATE refresh_calendars SET next_at=%s", (now,))
    assert asyncio.run(worker(s, Provider(events)).refresh_one(now)) is True


# spec: ANYM-576
def test_starting_is_emitted_once_per_lead_and_survives_a_restart(household):
    s = household
    start = NOW + timedelta(hours=2)
    refreshed(s, [event("lesson", start)], start - timedelta(minutes=62), leads=("15", "60"))
    assert remind(s, start - timedelta(minutes=61)) == 0
    assert remind(s, start - timedelta(minutes=60)) == 1
    assert remind(s, start - timedelta(minutes=59)) == 0
    refreshed(s, [event("lesson", start)], start - timedelta(minutes=20), leads=("15", "60"))
    assert remind(s, start - timedelta(minutes=14)) == 1
    # A new worker and store (a restart) never emits the same (event_ref, start_at, lead) again.
    restarted = store_module.Store(s.dsn, schema=s.schema)
    try:
        assert refresh.remind(restarted, start - timedelta(minutes=10)) == 0
    finally:
        restarted.close()
    assert starting(s) == [{"event_ref": ref("lesson"), "start_at": start, "lead_minutes": 60},
                           {"event_ref": ref("lesson"), "start_at": start, "lead_minutes": 15}]
    events = [e for e in page(s)["events"] if e["type"] == "calendar.event.starting"]
    assert [e["data"]["lead_minutes"] for e in events] == [60, 15]
    first = events[0]
    assert first["data"] == {"calendar_ref": "cal-a", "event_ref": ref("lesson"), "start_at": "2026-11-03T09:00:00Z",
                             "lead_minutes": 60}
    assert (first["subject"], first["urgency"], first["expires_at"], first["scope_ref"]) == (
        ref("lesson"), "now", "2026-11-03T09:00:00Z", "calendar:cal-a")
    assert first["id"] == "calendar:%d" % s.one("SELECT min(seq) AS n FROM starting_reminders")["n"]


# spec: ANYM-576
def test_an_already_past_occurrence_an_all_day_event_and_a_stale_refresh_get_no_reminder(household):
    s = household
    now = datetime(2026, 11, 3, 23, 50, tzinfo=timezone.utc)
    past = event("past", now - timedelta(minutes=5))
    # A whole day starting at midnight UTC, 10 minutes away.
    all_day = {"id": "holiday", "status": "confirmed", "summary": "Holiday", "start": {"date": "2026-11-04"},
               "end": {"date": "2026-11-05"}}
    soon = event("soon", now + timedelta(minutes=10))
    refreshed(s, [past, all_day, soon], now - timedelta(minutes=1))
    assert remind(s, now) == 1
    assert [r["event_ref"] for r in starting(s)] == [ref("soon")]
    # The last successful refresh is more than twice the cadence (plus a minute) old: nothing is reminded from it.
    later = event("later", now + timedelta(minutes=20))
    refreshed(s, [later], now)
    assert remind(s, now + timedelta(minutes=12)) == 0
    refreshed(s, [later], now + timedelta(minutes=12))
    assert remind(s, now + timedelta(minutes=12)) == 1


# spec: ANYM-576
def test_a_moved_occurrence_withdraws_its_pending_reminder_and_is_reminded_at_the_new_start(household):
    s = household
    start = NOW + timedelta(minutes=30)
    refreshed(s, [event("lesson", start)], NOW)
    moved = start + timedelta(hours=1)
    refreshed(s, [event("lesson", moved)], NOW + timedelta(minutes=15))
    assert remind(s, start - timedelta(minutes=10)) == 0
    refreshed(s, [event("lesson", moved)], moved - timedelta(minutes=20))
    assert remind(s, moved - timedelta(minutes=15)) == 1
    assert starting(s) == [{"event_ref": ref("lesson"), "start_at": moved, "lead_minutes": 15}]


# spec: ANYM-576
def test_a_cancelled_occurrence_withdraws_its_pending_reminder(household):
    s = household
    start = NOW + timedelta(minutes=30)
    refreshed(s, [event("lesson", start), event("dentist", start)], NOW)
    refreshed(s, [event("dentist", start), {"id": "lesson", "status": "cancelled"}], NOW + timedelta(minutes=10))
    assert remind(s, start - timedelta(minutes=10)) == 1
    assert [r["event_ref"] for r in starting(s)] == [ref("dentist")]
    assert [e["type"] for e in page(s)["events"]] == ["calendar.event.cancelled", "calendar.event.starting"]


# spec: ANYM-576
def test_migration_9_is_an_unattended_expand_with_additive_grants():
    files = dict(store_module.MIGRATIONS)
    created = {}
    for version in sorted(files):
        if version < 9:
            created = rollout.new_tables(files[version].read_text(), created)
    text = files[9].read_text()
    assert rollout.unattended_class(text, set(created)) == "expand"
    roles_text = store_module.ROLES.read_text()
    base = roles_text[: roles_text.index("-- Migration 9 (scheduled refresh")]
    # roles.sql as migration 9 left it: later migrations append their own grants after it (ANYM-577: migration 10).
    through_9 = roles_text[: roles_text.index("-- Migration 10 (module access journal")]
    assert rollout.role_class(base, through_9, rollout.new_tables(text), tables=set(created)) == "additive"


# spec: ANYM-576
def test_refresh_grants_keep_the_configuration_the_operators_and_let_the_worker_remind(schema_one, roles):  # noqa: F811
    apply_roles, connect = roles
    s = schema_one
    s.migrate()
    apply_roles()
    s.person("adult-a", "Adult A")
    s.group("family", "Family")
    s.member("family", "adult-a", "group_admin")
    s.connection("google-a", "adult-a", "google")
    s.calendar("cal-a", "google-a", "private-a", "Private", "UTC", "write")
    configure(s, "--calendar", "cal-a")
    gateway, wk = connect("gateway"), connect("worker")
    # No demand channel: the gateway (messaging's side) cannot write the configuration.
    with pytest.raises(Exception):
        gateway.query("UPDATE refresh_calendars SET next_at=now()")
    with pytest.raises(Exception):
        wk.query("UPDATE refresh_calendars SET leads='{5}'")
    start = NOW + timedelta(minutes=10)
    assert asyncio.run(worker(wk, Provider([event("lesson", start)])).refresh_one(NOW)) is True
    assert refresh.remind(wk, NOW) == 1
    key = s.agent("router", "family", "adult-a", [EVENTS], ["cal-a"])
    assert [e["type"] for e in FamilyService(gateway).events_page(key, 0, 100)["events"]] == [
        "calendar.event.starting"]
