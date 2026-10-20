from family_calendar.service.core import READ, WRITE
from family_calendar.service.providers import created_event_ref, normalize_google, source_ref
from tests.service.test_projection import WINDOW, error, household, links, steps
from tests.service.test_projection_undo import Google, apply

LESSON = {"title": "Swim class", "start": "2026-10-20T14:00:00+00:00", "end": "2026-10-20T15:00:00+00:00",
          "timezone": "Asia/Makassar", "location": "Pool 2"}
MOVED = dict(LESSON, start="2026-10-20T16:00:00+00:00", end="2026-10-20T17:00:00+00:00")
TARGETS = {"cal-child": "child", "cal-a": "private-a", "cal-b": "private-b"}


def create(event=LESSON, activity="Dance"):
    return {"action": "create", "calendar_ref": "cal-child", "event": event, "activity": activity}


def writable_lessons(s):
    """The household of test_projection, with the lesson calendar writable by adult-a's agent."""
    service, key, drop, pick = household(s)
    s.write_opt_in("cal-child", True)
    s.grant("family", "adult-a", "cal-child", READ + WRITE, "details")
    return service, key, drop, pick, Google(s)


def sync(s, google):
    """Publishes every calendar from the mock Google, as the worker's reads would."""
    for cal, target in TARGETS.items():
        s.publish_window(cal, *WINDOW, [normalize_google(cal, e, "Asia/Makassar")
                                        for (c, _), e in google.events.items() if c == target], "complete")


def created(service, key, google, s):
    operation_id = apply(service, key, [create()], "request-create")
    assert google.run(operation_id)["state"] == "verified"
    sync(s, google)
    return s.one("SELECT ref FROM occurrences WHERE calendar_ref='cal-child' AND details->>'title'='Swim class'")["ref"]


def link_rows(s):
    return s.query("SELECT duty,state,etag,source_event_ref,source_start,source_end,target_id FROM projection_links "
                   "ORDER BY duty DESC,created_at")


def move(event_ref):
    return {"action": "update", "calendar_ref": "cal-child", "event_ref": event_ref, "event": MOVED}


def at(event, field):
    return event[field]["dateTime"]


# spec: ANYM-435
def test_activity_create_plans_source_and_projection(store):
    s = store
    service, key, drop, pick, google = writable_lessons(s)
    plan = service.plan(key, [create()])
    lesson, dropoff, pickup = plan["steps"]
    assert (lesson["action"], lesson["calendar_ref"], "link" in lesson) == ("create", "cal-child", False)
    assert [st["ordinal"] for st in plan["steps"]] == [0, 1, 2]
    for st, rule, cal in ((dropoff, drop, "cal-a"), (pickup, pick, "cal-b")):
        assert (st["action"], st["calendar_ref"], st["link"]["rule_id"]) == ("create", cal, rule)
        assert st["link"]["source_step"] == 0 and st["link"]["source_event_ref"] is None
        assert st["source"] == {"start": LESSON["start"], "end": LESSON["end"], "title": "Swim class"}
    assert (dropoff["event"]["start"], dropoff["event"]["end"]) == ("2026-10-20T13:40:00+00:00", LESSON["start"])
    assert (pickup["event"]["start"], pickup["event"]["end"]) == ("2026-10-20T14:45:00+00:00", LESSON["end"])
    assert pickup["event"]["location"] == "Pool 2" and "location" not in dropoff["event"]
    # The pick-up ends with the lesson it serves: that is no overlap.
    assert not [o for o in plan["overlap_check"]["overlaps"] if o.get("kind") == "proposed_overlap"]
    assert plan["projections_skipped"] == []

    # A definitely failed lesson create sends no drive.
    google.reject.add("child")
    failed = apply(service, key, [create()], "request-rejected")
    assert google.run(failed)["state"] == "failed"
    assert {cal for _, cal in google.requests} == {"child"} and links(s) == []
    google.reject.clear()

    # An unknown lesson create sends no drive; reconcile verifies it first, then the drives follow it.
    google.crash.add(("POST", "child"))
    lost = apply(service, key, [create()], "request-lost")
    assert google.run(lost)["state"] == "unknown"
    assert [st["state"] for st in steps(s, lost)] == ["unknown", "prepared", "prepared"] and links(s) == []
    assert ("POST", "private-a") not in google.requests
    google.down = False
    assert google.run(lost, reconcile=True)["state"] == "verified"
    source = s.one("SELECT receipt FROM operation_steps WHERE operation_id=%s AND ordinal=0", (lost,))["receipt"]
    rows = link_rows(s)
    assert [(r["duty"], r["state"]) for r in rows] == [("pickup", "active"), ("dropoff", "active")]
    target = s.one("SELECT payload FROM plans p JOIN operations o ON o.plan_id=p.id WHERE o.id=%s",
                   (lost,))["payload"]["steps"][0]["target_id"]
    assert source["resource_ref"] == source_ref("res", "cal-child", target)
    expected = created_event_ref("google", source["resource_ref"], target)
    assert {r["source_event_ref"] for r in rows} == {expected}
    # The receipt names the event the next read publishes.
    sync(s, google)
    assert s.one("SELECT 1 FROM occurrences WHERE ref=%s AND calendar_ref='cal-child'", (expected,))

    assert error(lambda: service.plan(key, [create(activity="Chess")])).code == "nothing_to_project"
    assert error(lambda: service.plan(key, [dict(move(expected), activity="Dance")])).code == "invalid_action"
    all_day = {"title": "Camp", "all_day": True, "start": "2026-10-22"}
    assert error(lambda: service.plan(key, [create(all_day)])).code == "source_all_day"
    s.guardian_readiness = lambda: "grants_not_ready"
    assert error(lambda: service.plan(key, [create()])).code == "capability_unverified"
    # Ordinary writes never depend on links.
    assert [st["action"] for st in service.plan(key, [move(expected)])["steps"]] == ["update"]
    del s.guardian_readiness


# spec: ANYM-435
def test_source_change_updates_linked_projection(store):
    s = store
    service, key, drop, pick, google = writable_lessons(s)
    lesson = created(service, key, google, s)
    rows = {r["duty"]: r for r in link_rows(s)}
    plan = service.plan(key, [move(lesson)])
    source, dropoff, pickup = plan["steps"]
    assert (source["action"], source["calendar_ref"], "link" in source) == ("update", "cal-child", False)
    stored = s.one("SELECT payload FROM plans WHERE id=%s", (plan["plan_id"],))["payload"]["steps"]
    for st, kept, duty, cal in ((dropoff, stored[1], "dropoff", "cal-a"), (pickup, stored[2], "pickup", "cal-b")):
        assert (st["action"], st["calendar_ref"], st["link"]["duty"]) == ("update", cal, duty)
        assert st["link"]["source_step"] == 0 and st["link"]["source_event_ref"] == lesson
        assert st["source"] == {"start": MOVED["start"], "end": MOVED["end"], "title": "Swim class"}
        assert kept["expected_etag"] == rows[duty]["etag"] and kept["target_id"] == rows[duty]["target_id"]
    assert dropoff["diff"]["start"] == {"before": "2026-10-20T13:40:00+00:00", "after": "2026-10-20T15:40:00+00:00"}
    assert dropoff["diff"]["end"]["after"] == MOVED["start"]
    assert pickup["diff"]["start"]["after"] == "2026-10-20T16:45:00+00:00" and pickup["diff"]["end"]["after"] == (
        MOVED["end"])
    assert plan["projections_skipped"] == []
    # The drives' own cached versions are no overlaps of their new spans.
    assert not [o for o in plan["overlap_check"]["overlaps"] if o.get("event_ref")]

    # A pending link (its create may be in flight) is listed, not planned.
    s.query("UPDATE projection_links SET state='pending' WHERE duty='pickup'")
    pending = service.plan(key, [move(lesson)])
    assert [st["calendar_ref"] for st in pending["steps"]] == ["cal-child", "cal-a"]
    assert pending["projections_skipped"] == [
        {"rule_id": pick, "duty": "pickup", "guardian_id": "adult-b", "reason": "projection_pending"}]
    cancel = service.plan(key, [{"action": "delete", "calendar_ref": "cal-child", "event_ref": lesson}])
    assert [(st["action"], st["calendar_ref"]) for st in cancel["steps"]] == [("delete", "cal-child"),
                                                                                ("delete", "cal-a")]
    assert cancel["steps"][1]["link"]["source_step"] == 0
    assert cancel["projections_skipped"][0]["reason"] == "projection_pending"
    s.query("UPDATE projection_links SET state='active' WHERE duty='pickup'")

    # Applied: the lesson and both drives move.
    operation_id = apply(service, key, [move(lesson)], "request-move")
    assert google.run(operation_id)["state"] == "verified"
    assert (at(google.drive("dropoff"), "start"), at(google.drive("dropoff"), "end")) == (
        "2026-10-20T15:40:00+00:00", MOVED["start"])
    assert at(google.drive("pickup"), "end") == MOVED["end"]
    # A drive the plan also writes explicitly is one action too many.
    sync(s, google)
    drive = s.one("SELECT ref FROM occurrences WHERE calendar_ref='cal-a'")["ref"]
    explicit = {"action": "delete", "calendar_ref": "cal-a", "event_ref": drive}
    assert error(lambda: service.plan(key, [move(lesson), explicit])).code == "duplicate_resource_action"


# spec: ANYM-435
def test_edited_linked_event_is_not_overwritten(store):
    s = store
    service, key, drop, pick, google = writable_lessons(s)
    lesson = created(service, key, google, s)
    before = {r["duty"]: r for r in link_rows(s)}
    operation_id = apply(service, key, [move(lesson)], "request-move")
    # The other guardian moves their pick-up by hand after the plan was made.
    edited = google.drive("pickup")
    edited.update(summary="Collect later", etag='"7"')
    snapshot = dict(edited)
    assert google.run(operation_id) == {"state": "partial", "error": "version_conflict"}
    assert [(st["state"], st["error"]) for st in steps(s, operation_id)] == [
        ("verified", None), ("verified", None), ("conflict", "version_conflict")]
    assert google.drive("pickup") == snapshot
    assert ("PATCH", "private-b") not in google.requests
    after = {r["duty"]: r for r in link_rows(s)}
    assert (after["pickup"]["state"], after["pickup"]["etag"]) == ("active", before["pickup"]["etag"])
    assert after["pickup"]["source_start"] == before["pickup"]["source_start"]


# spec: ANYM-435
def test_linked_change_updates_link_record(store):
    s = store
    service, key, drop, pick, google = writable_lessons(s)
    lesson = created(service, key, google, s)
    s.query("UPDATE guardian_rules SET lead_minutes=30,version=version+1 WHERE id=%s", (drop,))
    operation_id = apply(service, key, [move(lesson)], "request-move")
    assert google.run(operation_id)["state"] == "verified"
    rows = {r["duty"]: r for r in link_rows(s)}
    for duty in ("dropoff", "pickup"):
        assert rows[duty]["state"] == "active" and rows[duty]["etag"] == google.drive(duty)["etag"] == '"2"'
        assert (rows[duty]["source_start"].isoformat(), rows[duty]["source_end"].isoformat()) == (
            MOVED["start"], MOVED["end"])
    # The moved drive follows the rule's current version, and its link records it.
    assert at(google.drive("dropoff"), "start") == "2026-10-20T15:30:00+00:00"
    assert s.one("SELECT rule_version FROM projection_links WHERE duty='dropoff'")["rule_version"] == 2

    sync(s, google)
    cancel = apply(service, key, [{"action": "delete", "calendar_ref": "cal-child", "event_ref": lesson}],
                   "request-cancel")
    assert google.run(cancel)["state"] == "verified"
    assert [r["state"] for r in link_rows(s)] == ["removed", "removed"]
    assert google.drive("dropoff")["status"] == google.drive("pickup")["status"] == "cancelled"


# spec: ANYM-435
def test_unwritable_link_is_skipped_with_reason(store):
    s = store
    service, key, drop, pick, google = writable_lessons(s)
    lesson = created(service, key, google, s)
    s.grant("family", "adult-a", "cal-b", READ, "details")
    plan = service.plan(key, [move(lesson)])
    assert [st["calendar_ref"] for st in plan["steps"]] == ["cal-child", "cal-a"]
    assert plan["projections_skipped"] == [
        {"rule_id": pick, "duty": "pickup", "guardian_id": "adult-b", "reason": "target_not_writable"}]
    cancel = service.plan(key, [{"action": "delete", "calendar_ref": "cal-child", "event_ref": lesson}])
    assert [st["calendar_ref"] for st in cancel["steps"]] == ["cal-child", "cal-a"]
    assert cancel["projections_skipped"][0]["reason"] == "target_not_writable"
