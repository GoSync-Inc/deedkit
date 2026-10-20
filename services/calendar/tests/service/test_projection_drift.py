from family_calendar.service.core import READ, WRITE
from tests.service.test_projection import WINDOW, error, project, steps
from tests.service.test_projection_changes import MOVED, apply, created, link_rows, sync, writable_lessons


def lesson_event(google):
    return next(e for (c, _), e in google.events.items() if c == "child")


def edit(event, **fields):
    """A change made in the provider's own app: Google gives the event a new ETag."""
    event.update(fields)
    event["etag"] = '"%d"' % (int(event["etag"].strip('"')) + 100)


def moved(google):
    event = lesson_event(google)
    edit(event, start=dict(event["start"], dateTime=MOVED["start"]), end=dict(event["end"], dateTime=MOVED["end"]))


def flags(service, key, **opt_in):
    events = service.agenda(key, ["cal-a", "cal-b"], *WINDOW, **opt_in)["events"]
    return {e["projection"]["duty"]: e["projection"] for e in events if "projection" in e}


def statuses(service, key):
    return {duty: f["status"] for duty, f in flags(service, key, projection_status=True).items()}


def reasons(details):
    return {x["duty"]: x["reason"] for x in details["projections_skipped"]}


def live(s, duty):
    return s.query("SELECT * FROM projection_links WHERE duty=%s AND state<>'removed'", (duty,))


def planned(s, plan):
    return s.one("SELECT payload FROM plans WHERE id=%s", (plan["plan_id"],))["payload"]["steps"]


# spec: ANYM-436
def test_agenda_flags_projection_drift(store):
    s = store
    service, key, drop, pick, google = writable_lessons(s)
    created(service, key, google, s)
    rows = {r["duty"]: r for r in s.query("SELECT id,duty,operation_id FROM projection_links")}
    assert flags(service, key, projection_status=True) == {
        duty: {"link_id": rows[duty]["id"], "duty": duty, "status": "in_sync", "operation_id": rows[duty]["operation_id"]}
        for duty in ("dropoff", "pickup")}
    # A caller that does not opt in (a gateway past its rule-read ceiling) sees the drives without links.
    plain = service.agenda(key, ["cal-a", "cal-b"], *WINDOW)["events"]
    assert len(plain) == 2 and not [e for e in plain if "projection" in e]

    # The other guardian edits the pick-up by hand; the school moves the lesson in its own app.
    edit(google.drive("pickup"), summary="Collect later")
    moved(google)
    sync(s, google)
    assert statuses(service, key) == {"dropoff": "source_moved", "pickup": "projection_edited"}
    # Stale source coverage is unknown, never a guessed move.
    s.query("UPDATE coverage SET checked_at=now()-interval '10 minutes' WHERE calendar_ref='cal-child'")
    assert statuses(service, key) == {"dropoff": "unknown", "pickup": "projection_edited"}
    sync(s, google)
    # A reader without details on the lesson's calendar learns nothing about the lesson.
    s.grant("family", "adult-a", "cal-child", READ + WRITE, "busy_only")
    assert statuses(service, key)["dropoff"] == "unknown"
    s.grant("family", "adult-a", "cal-child", READ + WRITE, "details")
    assert statuses(service, key)["dropoff"] == "source_moved"

    # Cancelled in the provider's app: the occurrence is gone from fresh coverage.
    edit(lesson_event(google), status="cancelled")
    sync(s, google)
    assert statuses(service, key) == {"dropoff": "source_cancelled", "pickup": "projection_edited"}
    # Links are rule data: a key outside the rule-read policy gets none.
    s.query("UPDATE principals SET scopes=scopes-'assignment:read'")
    assert flags(service, key, projection_status=True) == {}


# spec: ANYM-436
def test_project_fixes_drifted_links(store):
    s = store
    service, key, drop, pick, google = writable_lessons(s)
    lesson = created(service, key, google, s)
    before = {r["duty"]: r for r in link_rows(s)}

    # Moved in the school's app: one preview moves both drives, each conditional on its link ETag.
    moved(google)
    sync(s, google)
    plan = service.plan(key, [project(lesson)])
    assert [(st["action"], st["link"]["duty"], st["reproject"]) for st in plan["steps"]] == [
        ("update", "dropoff", True), ("update", "pickup", True)]
    for st in planned(s, plan):
        kept = before[st["link"]["duty"]]
        assert (st["expected_etag"], st["target_id"]) == (kept["etag"], kept["target_id"])
    assert plan["steps"][0]["diff"]["start"] == {"before": "2026-10-20T13:40:00+00:00",
                                                 "after": "2026-10-20T15:40:00+00:00"}
    operation_id = service.apply(key, plan["plan_id"], "request-reproject")["operation_id"]
    assert google.run(operation_id)["state"] == "verified"
    assert google.drive("dropoff")["end"]["dateTime"] == MOVED["start"]
    assert google.drive("pickup")["end"]["dateTime"] == MOVED["end"]
    assert {r["source_start"].isoformat() for r in link_rows(s)} == {MOVED["start"]}
    sync(s, google)
    assert statuses(service, key) == {"dropoff": "in_sync", "pickup": "in_sync"}

    # The pick-up is deleted outside DeedKit: re-created on the same link with a new client ID.
    old = live(s, "pickup")[0]
    del google.events["private-b", old["target_id"]]
    sync(s, google)
    plan = service.plan(key, [project(lesson)])
    assert [(st["action"], st["link"]["duty"], st["reproject"]) for st in plan["steps"]] == [
        ("create", "pickup", True)]
    assert reasons(plan) == {"dropoff": "already_projected"}
    assert planned(s, plan)[0]["target_id"] != old["target_id"]
    # A definite failure gives the link its old target back; it is still flagged missing.
    google.reject.add("private-b")
    rejected = service.apply(key, plan["plan_id"], "request-repair-rejected")["operation_id"]
    assert google.run(rejected)["state"] == "failed"
    kept = live(s, "pickup")
    assert [(r["id"], r["state"], r["target_id"], r["target_resource_ref"]) for r in kept] == [
        (old["id"], "active", old["target_id"], old["target_resource_ref"])]
    google.reject.clear()
    # The attempt staled the target's coverage; the next fresh read flags the drive missing again.
    assert reasons(error(lambda: service.plan(key, [project(lesson)])).details)["pickup"] == "already_projected"
    sync(s, google)
    # An unknown result keeps the link repairing until reconcile verifies by the new client ID.
    plan = service.plan(key, [project(lesson)])
    target = planned(s, plan)[0]["target_id"]
    lost = service.apply(key, plan["plan_id"], "request-repair-lost")["operation_id"]
    google.crash.add(("POST", "private-b"))
    assert google.run(lost)["state"] == "unknown"
    row = live(s, "pickup")[0]
    assert (row["id"], row["state"], row["target_id"], row["previous_target_id"]) == (
        old["id"], "repairing", target, old["target_id"])
    assert reasons(error(lambda: service.plan(key, [project(lesson)])).details) == {
        "dropoff": "already_projected", "pickup": "outcome_unknown"}
    google.down = False
    posts = google.sent("POST")
    assert google.run(lost, reconcile=True)["state"] == "verified"
    assert google.sent("POST") == posts
    row = live(s, "pickup")[0]
    assert (row["id"], row["state"], row["target_id"], row["etag"]) == (
        old["id"], "active", target, google.events["private-b", target]["etag"])
    sync(s, google)
    assert statuses(service, key) == {"dropoff": "in_sync", "pickup": "in_sync"}

    # A drive edited by hand is listed and left alone; a cancelled lesson points to unproject.
    edit(google.drive("dropoff"), summary="By bus")
    edit(lesson_event(google), status="cancelled")
    sync(s, google)
    nothing = error(lambda: service.plan(key, [project(lesson)]))
    assert nothing.code == "nothing_to_project"
    assert reasons(nothing.details) == {"dropoff": "projection_edited", "pickup": "use_unproject"}
    undo = service.plan(key, [{"action": "unproject", "calendar_ref": "cal-child", "event_ref": lesson}])
    assert [(st["action"], st["link"]["duty"]) for st in undo["steps"]] == [("delete", "dropoff"),
                                                                          ("delete", "pickup")]


# spec: ANYM-436
def test_competing_repairs_create_one_replacement(store):
    s = store
    service, key, drop, pick, google = writable_lessons(s)
    lesson = created(service, key, google, s)
    old = live(s, "pickup")[0]
    del google.events["private-b", old["target_id"]]
    sync(s, google)
    first = apply(service, key, [project(lesson)], "request-repair-1")
    second = apply(service, key, [project(lesson)], "request-repair-2")
    posts = google.sent("POST")
    # The replacement is written, then the worker loses the answer.
    google.crash.add(("POST", "private-b"))
    assert google.run(first)["state"] == "unknown"
    google.down = False
    assert google.run(second) == {"state": "failed", "error": "projection_pending"}
    assert [(st["state"], st["error"]) for st in steps(s, second)] == [("failed", "projection_pending")]
    assert google.sent("POST") == posts + 1
    # Reconcile verifies by reading, without a second send.
    assert google.run(first, reconcile=True)["state"] == "verified"
    assert google.sent("POST") == posts + 1
    rows = live(s, "pickup")
    replacement = s.one("SELECT payload FROM plans p JOIN operations o ON o.plan_id=p.id WHERE o.id=%s",
                        (first,))["payload"]["steps"][0]["target_id"]
    assert [(r["id"], r["state"], r["target_id"]) for r in rows] == [(old["id"], "active", replacement)]
    assert [t for (c, t), e in google.events.items() if c == "private-b" and e.get("status") != "cancelled"] == [
        replacement]
