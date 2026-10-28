"""calendar_schedule guests: an explicit notification preview, Google invitations, an exact refusal elsewhere (ANYM-555)."""
import asyncio
import json

import psycopg
import pytest

from family_calendar.errors import CalendarError
from family_calendar.service.core import READ, WRITE, FamilyService
from family_calendar.service.schedule import Scheduler
from family_calendar.service.worker import Worker
from tests.service.test_guardian_rules import roles, schema_one  # noqa: F401 (fixtures)
from tests.service.test_providers import GOOGLE_CAL, ICLOUD_CAL, apply_step, caldav_world, google_world
from family_calendar.service import schedule as sched
from tests.service.test_schedule_intent import CREATE, Provider, call, plan_of, rulebook, setup

GUESTS = ["ana@example.invalid", "Bo.Lee@example.invalid"]


def invites(s):
    """The schedule tests' household, with the owner's invitation opt-in on calendar "A" (cal-a, Google)."""
    key = setup(s)
    s.invitations_opt_in("cal-a", True)
    return key


def icloud(s):
    """adult-a's agent also writes to an iCloud calendar "Home iCloud"; returns that agent's key."""
    invites(s)
    s.connection("icloud-a", "adult-a", "icloud")
    s.calendar("cal-ia", "icloud-a", "/ia/", "Home iCloud", "Asia/Makassar", "write")
    s.write_opt_in("cal-ia", True)
    s.grant("family", "adult-a", "cal-ia", READ + WRITE, "details")
    s.family_settings("family", "Asia/Makassar")
    return s.agent("agent-i", "family", "adult-a", READ + WRITE, ["cal-a", "cal-ia"])


# spec: ANYM-555
def test_notify_all_preview_names_every_recipient_and_what_each_receives(store):
    key, fake = invites(store), Provider()
    preview = rulebook.validate_result(call(store, key, {**CREATE, "guests": GUESTS, "notify": "all", "dry_run": True}, fake))
    shown = preview["preview"]
    assert shown["guests"] == GUESTS
    notes = shown["notifications"]
    assert notes["notify"] == "all" and [r["email"] for r in notes["recipients"]] == GUESTS
    for recipient in notes["recipients"]:
        assert recipient["receives"] == {
            "title": "Swimming", "start": "2026-11-02T10:00:00+08:00", "end": "2026-11-02T10:30:00+08:00",
            "timezone": "Asia/Makassar", "description": None, "location": None, "guests": GUESTS,
        }
    assert "email" in notes["summary"] and not fake.sent


# spec: ANYM-555
def test_notify_none_preview_says_nobody_is_notified(store):
    key = invites(store)
    shown = call(store, key, {**CREATE, "guests": GUESTS, "notify": "none", "dry_run": True})["preview"]
    assert shown["guests"] == GUESTS
    assert shown["notifications"]["notify"] == "none" and shown["notifications"]["recipients"] == []
    assert shown["notifications"]["summary"].startswith("Nobody is notified")
    # Without guests the preview keeps its ANYM-552 shape.
    assert "notifications" not in call(store, key, {**CREATE, "dry_run": True})["preview"]


# spec: ANYM-555
def test_schedule_plans_the_guests_and_notify_into_the_google_create_step(store):
    key, fake = invites(store), Provider()
    result = call(store, key, {**CREATE, "guests": GUESTS, "notify": "all", "request_id": "req-guests-1"}, fake)
    assert result["state"] == "succeeded"
    assert plan_of(store, result)["invite"] == {"guests": GUESTS, "notify": "all"}
    token = call(store, key, {**CREATE, "guests": GUESTS[:1], "notify": "none", "dry_run": True})["confirm_token"]
    applied = call(store, key, {"confirm_token": token}, fake)
    assert plan_of(store, applied)["invite"] == {"guests": GUESTS[:1], "notify": "none"}


# spec: ANYM-555
@pytest.mark.asyncio
@pytest.mark.parametrize("notify", ["none", "all"])
async def test_google_create_adds_the_guests_and_send_updates_follows_notify(notify):
    providers, server = google_world([])
    step = {"action": "create", "ordinal": 0, "target_id": "fcsguests" + notify,
            "event": {"title": "Swimming", "start": "2026-11-02T10:00:00+08:00", "end": "2026-11-02T10:30:00+08:00",
                      "timezone": "Asia/Makassar"},
            "invite": {"guests": GUESTS, "notify": notify}}
    intent, receipt = await apply_step(providers, GOOGLE_CAL, step)
    assert receipt["verified"] is True
    [post] = [r for r in server.requests if r.method == "POST"]
    assert post.url.params["sendUpdates"] == notify
    assert json.loads(post.content)["attendees"] == [{"email": g} for g in GUESTS]
    # Google answers with response fields and the organizer itself; the guests are still verified.
    server.events[step["target_id"]]["attendees"] = [
        {"email": "owner@example.invalid", "organizer": True, "self": True, "responseStatus": "accepted"},
        *({"email": g.lower(), "responseStatus": "needsAction"} for g in GUESTS)]
    assert (await providers.verify(GOOGLE_CAL, intent))["verified"] is True
    server.events[step["target_id"]]["attendees"] = [{"email": GUESTS[0]}, {"email": "other@example.invalid"}]
    assert await providers.verify(GOOGLE_CAL, intent) is None


# spec: ANYM-555
@pytest.mark.asyncio
async def test_a_create_without_guests_still_sends_no_updates():
    providers, server = google_world([])
    step = {"action": "create", "ordinal": 0, "target_id": "fcsnoguests",
            "event": {"title": "Swimming", "start": "2026-11-02T10:00:00+08:00", "end": "2026-11-02T10:30:00+08:00",
                      "timezone": "Asia/Makassar"}}
    await apply_step(providers, GOOGLE_CAL, step)
    [post] = [r for r in server.requests if r.method == "POST"]
    assert post.url.params["sendUpdates"] == "none" and "attendees" not in json.loads(post.content)


# spec: ANYM-555
def test_icloud_guests_are_refused_naming_calendar_provider_and_the_closest_call(store):
    key, fake = icloud(store), Provider()
    for extra in ({"dry_run": True}, {"request_id": "req-icloud-1"}):
        refused = rulebook.validate_error(call(store, key, {
            **CREATE, "calendar": "Home iCloud", "guests": GUESTS, "notify": "all", **extra}, fake))
        assert refused["error"] == "capability_unsupported" and refused["capability"] == "guests"
        assert refused["calendar"] == {"calendar_ref": "cal-ia", "label": "Home iCloud"}
        assert refused["provider"] == "icloud"
        assert refused["timezone"] == "Asia/Makassar"
        closest = refused["next"]
        assert closest["tool"] == "calendar_schedule"
        assert set(closest["arguments"]) == {"selection_token", "calendar", "dry_run"}
        # The continuation never repeats the caller's words: no title, no guest address.
        assert "Swimming" not in json.dumps(closest) and "example.invalid" not in json.dumps(refused)
        preview = call(store, key, closest["arguments"], fake)
        assert preview["state"] == "previewed" and preview["preview"]["title"] == "Swimming"
        assert preview["preview"]["calendar"]["calendar_ref"] == "cal-ia" and "guests" not in preview["preview"]
    assert not fake.sent and store.one("SELECT count(*) AS n FROM operations")["n"] == 0


# spec: ANYM-555
@pytest.mark.asyncio
async def test_a_guest_step_outside_a_google_create_fails_before_any_write():
    invite = {"guests": GUESTS, "notify": "all"}
    providers, server = caldav_world()
    step = {"action": "create", "ordinal": 0, "target_id": "fcsguests", "invite": invite,
            "event": {"title": "Swimming", "start": "2026-11-02T10:00:00+08:00", "end": "2026-11-02T10:30:00+08:00",
                      "timezone": "Asia/Makassar"}}
    with pytest.raises(CalendarError) as caught:
        await providers.prepare(ICLOUD_CAL, step)
    assert caught.value.code == "capability_unsupported" and not server.resources


# spec: ANYM-555
def test_capabilities_name_guest_support_per_calendar(store):
    key = icloud(store)
    calendars = {c["calendar_ref"]: c for c in FamilyService(store).capabilities(key)["calendars"]}
    assert calendars["cal-a"]["guests"] == {"supported": True, "enabled": True, "tool": "calendar_schedule",
                                            "actions": ["create"], "notify": ["none", "all"]}
    assert calendars["cal-ia"]["guests"] == {"supported": False, "error": "capability_unsupported"}
    store.invitations_opt_in("cal-a", False)
    calendars = {c["calendar_ref"]: c for c in FamilyService(store).capabilities(key)["calendars"]}
    assert calendars["cal-a"]["guests"]["enabled"] is False
    assert calendars["cal-a"]["guests"]["error"] == "invitations_not_enabled"
    assert calendars["cal-a"]["guests"]["owner_step"] == "service allow-invitations --calendar cal-a"


# spec: ANYM-555
def test_guests_wait_for_the_owner_invitation_opt_in_and_its_revocation_stops_the_send(store):
    key, fake = setup(store), Provider()
    for extra in ({"dry_run": True}, {"request_id": "req-optin-1"}):
        refused = rulebook.validate_error(call(store, key, {**CREATE, "guests": GUESTS, "notify": "all", **extra}, fake))
        assert refused["error"] == "invitations_not_enabled" and refused["provider"] == "google"
        assert refused["owner_step"] == "service allow-invitations --calendar cal-a"
        assert set(refused["next"]["arguments"]) == {"selection_token", "calendar", "dry_run"}
        assert "guests" not in call(store, key, refused["next"]["arguments"], fake)["preview"]
    # calendar_plan never takes guests, opted in or not.
    store.invitations_opt_in("cal-a", True)
    with pytest.raises(CalendarError) as caught:
        FamilyService(store).plan(key, [{"action": "create", "calendar_ref": "cal-a", "invite": {
            "guests": GUESTS, "notify": "all"}, "event": {"title": "Swimming", "start": "2026-11-02T10:00:00+08:00",
                                                          "end": "2026-11-02T10:30:00+08:00", "timezone": "UTC"}}])
    assert caught.value.code == "invalid_action"
    token = call(store, key, {**CREATE, "guests": GUESTS, "notify": "all", "dry_run": True})["confirm_token"]
    # A revocation between the preview and the apply: the token is stale, nothing is sent.
    store.invitations_opt_in("cal-a", False)
    stale = call(store, key, {"confirm_token": token}, fake)
    assert stale["error"] == "invitations_not_enabled" and not fake.sent
    # ... and between admission and the worker's send: the worker's revalidation refuses it.
    store.invitations_opt_in("cal-a", True)
    applied = call(store, key, {"confirm_token": call(store, key, {
        **CREATE, "guests": GUESTS, "notify": "all", "dry_run": True})["confirm_token"]}, None)
    plan = store.one("SELECT p.* FROM plans p JOIN operations o ON o.plan_id=p.id WHERE o.id=%s",
                     (applied["operation"]["id"],))
    store.invitations_opt_in("cal-a", False)
    with pytest.raises(CalendarError) as caught:
        FamilyService(store).validate_plan(plan, plan["principal_id"])
    assert caught.value.code in ("scope_changed", "invitations_not_enabled")


# spec: ANYM-555
def test_a_worker_that_predates_guests_refuses_a_plan_with_guests(store):
    key = invites(store)
    token = call(store, key, {**CREATE, "guests": GUESTS, "notify": "all", "dry_run": True})["confirm_token"]
    plan = store.one("SELECT * FROM plans WHERE token_hash=%s", (sched.token_hash(token),))
    service = FamilyService(store)
    service.validate_plan(plan, plan["principal_id"])
    # The previous worker knows no `invite`: it validates the plan as one without guests, and the scope differs.
    older = dict(plan, payload={**plan["payload"], "steps": [
        {k: v for k, v in s.items() if k != "invite"} for s in plan["payload"]["steps"]]})
    with pytest.raises(CalendarError) as caught:
        service.validate_plan(older, plan["principal_id"])
    assert caught.value.code == "scope_changed"


# spec: ANYM-555
def test_guest_arguments_are_checked_before_anything_is_planned(store):
    key = invites(store)
    cases = [
        ({"guests": GUESTS}, "notify", "required"),
        ({"notify": "all"}, "guests", "required"),
        ({"guests": ["not-an-address"], "notify": "all"}, "guests/0", "format"),
        ({"guests": [GUESTS[0], GUESTS[0].upper()], "notify": "all"}, "guests/1", "unique"),
    ]
    for extra, field, reason in cases:
        refused = rulebook.validate_error(call(store, key, {**CREATE, **extra, "dry_run": True}))
        assert refused["error"] == "invalid_arguments", extra
        assert refused["field_errors"] == [{"field": field, "reason": reason}], extra
    moved = call(store, key, {"action": "move", "event": "event-dentist", "start": "2026-10-21T15:00",
                              "guests": GUESTS, "notify": "all", "dry_run": True})
    assert moved["error"] == "invalid_arguments" and moved["field_errors"] == [{"field": "guests", "reason": "unknown_field"}]
    assert store.one("SELECT count(*) AS n FROM plans")["n"] == 0


# spec: ANYM-555
def test_runtime_roles_invite_only_after_schema_five_db_roles_and_the_opt_in(schema_one, roles):  # noqa: F811
    apply_roles, connect = roles
    schema_one.migrate(through=4)
    key = setup(schema_one)
    apply_roles()
    gateway_store, worker_store, fake = connect("gateway"), connect("worker"), Provider()
    scheduler = Scheduler(FamilyService(gateway_store))
    guests = {**CREATE, "guests": GUESTS, "notify": "all", "dry_run": True}
    # Schema 4: no opt-in table; guests are refused, a plain create still works.
    with pytest.raises(CalendarError) as caught:
        scheduler.schedule(key, guests)
    assert caught.value.code == "invitations_not_enabled"
    assert scheduler.schedule(key, {**CREATE, "dry_run": True})[0]["state"] == "previewed"
    # Schema 5 opted in, before db-roles: the runtime role cannot read the table, so guests stay refused.
    schema_one.migrate()
    schema_one.invitations_opt_in("cal-a", True)
    with pytest.raises(CalendarError) as caught:
        scheduler.schedule(key, guests)
    assert caught.value.code == "invitations_not_enabled"
    assert scheduler.schedule(key, {**CREATE, "dry_run": True})[0]["state"] == "previewed"
    apply_roles()
    preview = scheduler.schedule(key, guests)[0]
    assert preview["preview"]["notifications"]["notify"] == "all"
    result, operation_id, family_ = scheduler.schedule(key, {"confirm_token": preview["confirm_token"]})
    asyncio.run(Worker(worker_store, fake).run_operation(operation_id))
    assert scheduler.replay(operation_id, family_)["state"] == "succeeded" and len(fake.sent) == 1
    # Neither runtime role can change the owner's opt-in.
    for store in (gateway_store, worker_store):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            store.invitations_opt_in("cal-a", False)
