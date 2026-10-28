"""calendar_find_time: find a time with people by name and book it from the result (ANYM-553)."""
import asyncio
from datetime import datetime, timezone

import jsonschema

from family_calendar.errors import CalendarError
from family_calendar.service import core
from family_calendar.service.api import MODELS, Gateway, failure
from family_calendar.service.core import READ, WRITE, FamilyService
from tests.service.test_guardian_rules import family
from tests.service.test_schedule_intent import call as schedule_call

ZONE = "Asia/Makassar"
# Tue 2026-10-20 08:00-18:00 in Asia/Makassar (+08:00); the published window covers the whole UTC day.
DAY = {"from": "2026-10-20T08:00", "to": "2026-10-20T18:00"}
PUBLISHED = ("2026-10-20T00:00:00Z", "2026-10-21T00:00:00Z")


def publish(s, calendar, *busy):
    s.publish_window(calendar, *PUBLISHED, [{
        "resource_ref": f"res-{calendar}-{i}", "target": f"provider-{calendar}-{i}", "etag": '"v1"', "payload": {},
        "occurrences": [{"ref": f"occ-{calendar}-{i}", "start": a, "end": b, "busy": True, "details": {"title": "x"}}],
    } for i, (a, b) in enumerate(busy)], "complete")


def setup(s):
    """adult-a ("Egor") asks; the child is "Masha", adult-b "Anna", the helper "Anna Petrova"."""
    family(s)
    for person, label in [("adult-a", "Egor"), ("child", "Masha"), ("adult-b", "Anna"), ("helper", "Anna Petrova")]:
        s.person(person, label)
    s.write_opt_in("cal-child", True)
    for cal in ("cal-a", "cal-child"):
        s.grant("family", "adult-a", cal, READ + WRITE, "details")
    s.family_settings("family", ZONE, "cal-a")
    # Egor 10:00-11:00 local, Masha 12:00-17:30 local.
    publish(s, "cal-a", ("2026-10-20T02:00:00Z", "2026-10-20T03:00:00Z"))
    publish(s, "cal-child", ("2026-10-20T04:00:00Z", "2026-10-20T09:30:00Z"))
    return s.agent("agent", "family", "adult-a", READ + WRITE, ["cal-a", "cal-child"])


def find(s, key, **arguments):
    arguments.setdefault("refresh", False)
    try:
        return asyncio.run(Gateway(s).call(key, "find_time", arguments))
    except CalendarError as e:
        return failure(e.code, e.field_errors, e.details)


def schema(name):
    return MODELS[name].model_json_schema()


# spec: ANYM-553
def test_an_exact_unique_name_resolves_and_the_owner_is_included(store):
    key = setup(store)
    found = find(store, key, people=["masha"], window=DAY, duration=60)
    assert [m["member_id"] for m in found["people"]] == ["adult-a", "child"]
    assert [(x["start"], x["end"]) for x in found["slots"]] == [
        ("2026-10-20T08:00:00+08:00", "2026-10-20T10:00:00+08:00"),
        ("2026-10-20T11:00:00+08:00", "2026-10-20T12:00:00+08:00"),
    ]
    # Anna is an exact name although "Anna Petrova" contains it.
    anna = FamilyService(store).find_members(key, {"people": ["Anna"], "exclude_me": True})[1]
    assert [m["member_id"] for m in anna] == ["adult-b"]
    # exclude_me leaves Egor out; the helper has no binding, so no slot is claimed and none is suggested.
    alone = find(store, key, people=["anna  PETROVA", "child"], window=DAY, duration=60, exclude_me=True)
    assert [m["member_id"] for m in alone["people"]] == ["helper", "child"]
    assert alone["unbound_members"] == ["helper"] and alone["availability"] == "unknown"
    assert alone["slots"] == [] and "next" not in alone
    assert find(store, key, people=["Masha"], window=DAY, duration=60, exclude_me=True)["slots"] == [
        {"start": "2026-10-20T08:00:00+08:00", "end": "2026-10-20T12:00:00+08:00", "next": {
            "tool": "calendar_schedule",
            "arguments": {"action": "create", "start": "2026-10-20T08:00:00+08:00", "duration": 60, "dry_run": True}}},
    ]


# spec: ANYM-553
def test_an_ambiguous_or_partial_name_asks_to_select_and_an_unknown_one_points_to_members(store):
    key = setup(store)
    store.person("helper", "Masha")
    twins = find(store, key, people=["Masha"], window="tomorrow")
    assert twins["error"] == "selection_required"
    assert [(c["id"], c["kind"], c["label"]) for c in twins["candidates"]] == [
        ("child", "member", "Masha"), ("helper", "member", "Masha")]
    store.person("helper", "Anna Petrova")
    partial = find(store, key, people=["Ann", "child"], window="tomorrow", duration=45, title="Swimming")
    assert partial["error"] == "selection_required"
    assert [c["id"] for c in partial["candidates"]] == ["adult-b", "helper"]
    # Each candidate's read-only next is the same request with that member: runnable as given, no title echoed.
    assert partial["candidates"][0]["next"] == {"tool": "calendar_find_time", "arguments": {
        "people": ["adult-b", "child"], "window": "tomorrow", "duration": 45, "exclude_me": False}}
    for c in partial["candidates"]:
        jsonschema.validate(c["next"]["arguments"], schema("find_time"))
    unknown = find(store, key, people=["Zoe"], window="tomorrow")
    assert unknown["error"] == "not_found" and unknown["next"] == {"tool": "calendar_members", "arguments": {}}


# spec: ANYM-553
def test_named_windows_are_read_in_the_family_timezone(store, monkeypatch):
    key = setup(store)
    # Tue 2026-10-20 09:07 local: today starts at the next quarter hour, weeks run Monday to Monday.
    monkeypatch.setattr(core, "clock", lambda: datetime(2026, 10, 20, 1, 7, tzinfo=timezone.utc))
    windows = {w: find(store, key, people=["Masha"], window=w)["window"]
               for w in ("today", "tomorrow", "this_week", "next_week")}
    assert windows == {
        "today": {"from": "2026-10-20T09:15:00+08:00", "to": "2026-10-21T00:00:00+08:00"},
        "tomorrow": {"from": "2026-10-21T00:00:00+08:00", "to": "2026-10-22T00:00:00+08:00"},
        "this_week": {"from": "2026-10-20T09:15:00+08:00", "to": "2026-10-26T00:00:00+08:00"},
        "next_week": {"from": "2026-10-26T00:00:00+08:00", "to": "2026-11-02T00:00:00+08:00"},
    }
    today = find(store, key, people=["Masha"], window="today", duration=30)
    assert today["timezone"] == ZONE and today["now"] == "2026-10-20T09:07:00+08:00"
    assert today["slots"][0]["start"] == "2026-10-20T09:15:00+08:00"


# spec: ANYM-553
def test_no_free_slot_returns_empty_slots_and_a_read_only_next_one_step_wider(store):
    key = setup(store)
    full = find(store, key, people=["Masha"], window={"from": "2026-10-20T12:00", "to": "2026-10-20T17:00"},
                duration=30, title="Swimming")
    assert full["availability"] == "verified" and full["slots"] == []
    assert full["next"] == {"tool": "calendar_find_time", "arguments": {
        "people": ["child"], "window": {"from": "2026-10-20T12:00:00+08:00", "to": "2026-10-21T17:00:00+08:00"},
        "duration": 30, "exclude_me": False, "title": "Swimming"}}
    jsonschema.validate(full["next"]["arguments"], schema("find_time"))


# spec: ANYM-553
def test_each_slot_next_is_a_calendar_schedule_preview_never_an_apply(store):
    key = setup(store)
    found = find(store, key, people=["Masha"], window=DAY, duration=60, title="Swimming")
    assert found["slots"]
    for slot in found["slots"]:
        nxt = slot["next"]
        assert nxt["tool"] == "calendar_schedule" and nxt["arguments"]["dry_run"] is True
        jsonschema.validate(nxt["arguments"], schema("schedule"))
    first = found["slots"][0]["next"]["arguments"]
    assert first == {"action": "create", "title": "Swimming", "start": "2026-10-20T08:00:00+08:00",
                     "duration": 60, "dry_run": True}
    preview = schedule_call(store, key, first)
    assert preview["state"] == "previewed" and preview["confirm_token"]
    assert store.one("SELECT count(*) AS n FROM operations")["n"] == 0


# spec: ANYM-553
def test_widening_stays_inside_the_31_day_limit_and_moves_on_at_it():
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(ZONE)
    a, b, step = core.find_window({"from": "2026-11-01T00:00", "to": "2026-11-30T00:00"}, zone, core.clock())
    assert step == 29 and core.widened(a, b, step) == (a, datetime(2026, 12, 2, tzinfo=zone))
    # A 31-day window cannot widen: the next one starts where it ends.
    full = datetime(2026, 12, 2, tzinfo=zone)
    assert core.widened(a, full, 31) == (full, datetime(2027, 1, 2, tzinfo=zone))
