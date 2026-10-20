import asyncio
import json

import httpx

from family_calendar.providers.google.adapter import GoogleAdapter
from family_calendar.service.core import READ, WRITE, FamilyService
from family_calendar.service.providers import Providers
from family_calendar.service.worker import Worker
from tests.service.test_guardian_rules import family, schema_one  # noqa: F401 (fixture)
from tests.service.test_projection import WINDOW, error, household, lessons, links, project, steps

SOURCE = {"start": "2026-10-20T10:00:00+00:00", "end": "2026-10-20T11:00:00+00:00"}


def unproject(event_ref="event-lesson", calendar_ref="cal-child", operation_id=None):
    if operation_id:
        return {"action": "unproject", "operation_id": operation_id}
    return {"action": "unproject", "calendar_ref": calendar_ref, "event_ref": event_ref}


class Google:
    """A mock Google Calendar: DELETE cancels as Google does, `reject` refuses creates in a calendar, `crash`
    drops the answer of one (method, calendar) write and every request after it until `down` is cleared."""

    def __init__(self, s):
        self.s, self.events, self.requests = s, {}, []
        self.reject, self.crash, self.down = set(), set(), False

    def handle(self, request):
        if self.down:
            raise httpx.ConnectError("down")
        parts = request.url.path.split("/")
        cal = parts[parts.index("calendars") + 1]
        self.requests.append((request.method, cal))
        if request.method == "POST":
            if cal in self.reject:
                return httpx.Response(400)
            body = json.loads(request.content)
            self.events[cal, body["id"]] = dict(body, etag='"1"')
            answer = httpx.Response(200, json=self.events[cal, body["id"]])
        else:
            key = (cal, parts[-1])
            event = self.events.get(key)
            if request.method == "GET":
                return httpx.Response(200, json=event) if event else httpx.Response(404)
            if not event or event.get("status") == "cancelled":
                return httpx.Response(410)
            if request.headers.get("If-Match") != event["etag"]:
                return httpx.Response(412)
            if request.method == "DELETE":
                event.update(status="cancelled")
                answer = httpx.Response(204)
            else:
                event.update({k: v for k, v in json.loads(request.content).items() if v is not None})
                answer = httpx.Response(200, json=event)
            event["etag"] = '"%d"' % (int(event["etag"].strip('"')) + 1)
        if (request.method, cal) in self.crash:
            self.crash.discard((request.method, cal))
            self.down = True
            raise httpx.ReadTimeout("lost")
        return answer

    def run(self, operation_id, reconcile=False):
        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(self.handle)) as client:
                providers = Providers(self.s, None)
                for connection in ("google-a", "google-b"):
                    providers.adapters[connection] = GoogleAdapter(connection, client, lambda: "token")
                await Worker(self.s, providers).run_operation(operation_id, reconcile=reconcile)

        asyncio.run(go())
        return self.s.one("SELECT state,error FROM operations WHERE id=%s", (operation_id,))

    def sent(self, method):
        return sum(1 for m, _ in self.requests if m == method)

    def drive(self, duty):
        """The remote drive of the newest link for this duty."""
        row = self.s.one("SELECT target_calendar_ref,target_id FROM projection_links WHERE duty=%s "
                         "ORDER BY created_at DESC LIMIT 1", (duty,))
        return self.events[{"cal-a": "private-a", "cal-b": "private-b"}[row["target_calendar_ref"]], row["target_id"]]


def apply(service, key, actions, request_id):
    plan = service.plan(key, actions)
    return service.apply(key, plan["plan_id"], request_id)["operation_id"]


def projected(service, key, google, event_ref="event-lesson", request_id="request-project"):
    operation_id = apply(service, key, [project(event_ref)], request_id)
    google.run(operation_id)
    return operation_id


# spec: ANYM-434
def test_unproject_plans_only_own_links(store):
    s = store
    service, key, drop, pick = household(s)
    lessons(s, ("event-lesson-12", "2026-10-20T12:00:00Z", "2026-10-20T13:00:00Z", {}))
    google = Google(s)
    first = projected(service, key, google)
    second = projected(service, key, google, "event-lesson-12", "request-project-12")
    assert [row["state"] for row in links(s)] == ["active"] * 4
    # An unrelated event in the same calendar as a drive is never part of an undo.
    s.publish_window("cal-a", *WINDOW, [{
        "resource_ref": "res-errand", "target": "provider-errand", "etag": '"v1"', "payload": {},
        "occurrences": [{"ref": "event-errand", "start": "2026-10-20T09:00:00Z", "end": "2026-10-20T09:30:00Z",
                         "busy": True, "details": {"title": "Errand"}}],
    }], "complete")

    plan = service.plan(key, [unproject()])
    dropoff, pickup = plan["steps"]
    assert dropoff == {
        "action": "delete", "calendar_ref": "cal-a", "provider": "google", "ordinal": 0,
        "link": {"rule_id": drop, "rule_version": 1, "duty": "dropoff", "guardian_id": "adult-a",
                 "source_event_ref": "event-lesson"},
        "source": dict(SOURCE, title="Dance class"), "diff": {},
    }
    assert (pickup["calendar_ref"], pickup["link"]["rule_id"]) == ("cal-b", pick)
    assert plan["projections_skipped"] == [] and plan["overlap_check"]["status"] == "not_applicable"
    stored = s.one("SELECT payload FROM plans WHERE id=%s", (plan["plan_id"],))["payload"]["steps"]
    etags = {row["target_id"]: row["etag"] for row in s.query("SELECT target_id,etag FROM projection_links")}
    assert all(step["expected_etag"] == etags[step["target_id"]] == '"1"' for step in stored)
    assert {step["target_id"] for step in stored} == {
        row["target_id"] for row in s.query("SELECT target_id FROM projection_links WHERE operation_id=%s", (first,))}
    by_operation = service.plan(key, [unproject(operation_id=second)])
    assert [st["link"]["source_event_ref"] for st in by_operation["steps"]] == ["event-lesson-12"] * 2

    # A vanished lesson (cancelled instances are not stored) is still unprojectable, with its stored span.
    s.publish_window("cal-child", *WINDOW, [], "complete")
    gone = service.plan(key, [unproject()])
    assert [st["source"] for st in gone["steps"]] == [dict(SOURCE, title=None)] * 2
    assert [st["link"]["source_event_ref"] for st in service.plan(key, [unproject(operation_id=first)])["steps"]] == [
        "event-lesson"] * 2

    # Source authority on the stored source calendar, and the rule-read policy.
    no_rules = s.agent("no-rules", "family", "adult-a", [x for x in READ + WRITE if x != "assignment:read"],
                       ["cal-a", "cal-b", "cal-child"])
    busy = s.agent("busy", "family", "adult-a", READ + WRITE, ["cal-a", "cal-b"])
    s.query("INSERT INTO agent_calendars VALUES ('busy','cal-child','busy_only')")
    for who in (no_rules, busy):
        for action in (unproject(), unproject(operation_id=first)):
            refused = error(lambda: service.plan(who, [action]))
            assert refused.code == "not_found_or_forbidden" and drop not in str(refused.details)
    s.guardian_readiness = lambda: "grants_not_ready"
    assert error(lambda: service.plan(key, [unproject()])).code == "capability_unverified"
    del s.guardian_readiness
    for bad in ({"action": "unproject", "calendar_ref": "cal-child", "event_ref": "event-lesson", "operation_id": first},
                {"action": "unproject", "calendar_ref": "cal-child"},
                {"action": "unproject", "calendar_ref": "cal-child", "event_ref": "event-lesson", "activity": "Dance"},
                {"action": "delete", "calendar_ref": "cal-a", "event_ref": "event-errand", "operation_id": first},
                {"action": "delete", "event_ref": "event-errand"}):
        assert error(lambda: service.plan(key, [bad])).code == "invalid_action"
    assert error(lambda: service.plan(key, [unproject(), unproject(operation_id=first)])).code == (
        "duplicate_resource_action")


# spec: ANYM-434
def test_partial_projection_compensates_only_own_versions(store):
    s = store
    service, key, drop, pick = household(s)
    google = Google(s)
    google.reject.add("private-b")
    partial = projected(service, key, google)
    assert s.one("SELECT state FROM operations WHERE id=%s", (partial,))["state"] == "partial"
    plan = service.plan(key, [unproject(operation_id=partial)])
    assert [st["link"]["duty"] for st in plan["steps"]] == ["dropoff"]
    assert google.run(service.apply(key, plan["plan_id"], "request-undo-partial")["operation_id"])["state"] == "verified"
    assert google.drive("dropoff")["status"] == "cancelled" and google.sent("DELETE") == 1
    assert [(row["duty"], row["state"]) for row in links(s)] == [("dropoff", "removed"), ("pickup", "removed")]

    # The guardian edited the pick-up and deleted the drop-off: the edit stays, the deletion closes the link unsent.
    google.reject.clear()
    full = projected(service, key, google, request_id="request-project-again")
    google.drive("pickup").update(summary="Collect, Grandma comes", etag='"9"')
    dropoff = s.one("SELECT target_id FROM projection_links WHERE duty='dropoff' AND state='active'")["target_id"]
    del google.events["private-a", dropoff]
    undo = apply(service, key, [unproject(operation_id=full)], "request-undo-edited")
    assert google.run(undo) == {"state": "partial", "error": "version_conflict"}
    assert [row["state"] for row in steps(s, undo)] == ["verified", "conflict"] and google.sent("DELETE") == 1
    live = s.one("SELECT etag FROM projection_links WHERE duty='pickup' AND state='active'")
    assert live == {"etag": '"1"'} and google.drive("pickup")["summary"] == "Collect, Grandma comes"
    assert s.one("SELECT state FROM projection_links WHERE target_id=%s", (dropoff,))["state"] == "removed"


# spec: ANYM-434
def test_unknown_linked_step_is_not_compensated(store):
    s = store
    service, key, drop, pick = household(s)
    google = Google(s)
    google.crash.add(("POST", "private-b"))
    operation_id = projected(service, key, google)
    assert s.one("SELECT state FROM operations WHERE id=%s", (operation_id,))["state"] == "unknown"
    assert [(row["duty"], row["state"]) for row in links(s)] == [("dropoff", "active"), ("pickup", "pending")]
    google.down = False
    for action in (unproject(), unproject(operation_id=operation_id)):
        plan = service.plan(key, [action])
        assert [st["link"]["duty"] for st in plan["steps"]] == ["dropoff"]
        assert plan["projections_skipped"] == [
            {"rule_id": pick, "duty": "pickup", "guardian_id": "adult-b", "reason": "outcome_unknown"}]
    s.query("UPDATE projection_links SET state='repairing' WHERE duty='pickup'")
    s.query("UPDATE projection_links SET state='removed' WHERE duty='dropoff'")
    nothing = error(lambda: service.plan(key, [unproject()]))
    assert nothing.code == "nothing_to_unproject"
    assert [x["reason"] for x in nothing.details["projections_skipped"]] == ["outcome_unknown"]
    assert google.sent("DELETE") == 0


# spec: ANYM-434
def test_unproject_is_idempotent_and_scoped(store):
    s = store
    service, key, drop, pick = household(s)
    google = Google(s)
    operation_id = projected(service, key, google)
    other = s.agent("other", "family", "adult-a", READ + WRITE, ["cal-a", "cal-b", "cal-child"])
    for action in (unproject(operation_id=operation_id), unproject(operation_id="op-unknown")):
        assert error(lambda: service.plan(other, [action])).code == "not_found_or_forbidden"
    assert error(lambda: service.plan(key, [unproject(operation_id="op-unknown")])).code == "not_found_or_forbidden"

    # Two concurrent undos of the same links: one deletes, the other finds them gone and sends nothing.
    a, b = (apply(service, key, [unproject()], f"request-undo-{n}") for n in "ab")
    assert google.run(a)["state"] == "verified"
    assert google.run(b)["state"] == "verified"
    assert google.sent("DELETE") == 2 and [row["state"] for row in links(s)] == ["removed", "removed"]
    for action in (unproject(), unproject(operation_id=operation_id)):
        nothing = error(lambda: service.plan(key, [action]))
        assert (nothing.code, nothing.details) == ("nothing_to_unproject", {"projections_skipped": []})
    # Undone, the lesson can be projected again.
    assert [st["link"]["duty"] for st in service.plan(key, [project()])["steps"]] == ["dropoff", "pickup"]


# spec: ANYM-434
def test_unproject_reconcile_verifies_without_resend(store):
    s = store
    service, key, drop, pick = household(s)
    s.guardian_rule_disable(pick)
    google = Google(s)
    projected(service, key, google)
    google.crash.add(("DELETE", "private-a"))
    undo = apply(service, key, [unproject()], "request-undo-crash")
    assert google.run(undo)["state"] == "unknown"
    assert [row["state"] for row in links(s)] == ["active"] and google.sent("DELETE") == 1
    google.down = False
    assert google.run(undo, reconcile=True)["state"] == "verified"
    assert [row["state"] for row in links(s)] == ["removed"] and google.sent("DELETE") == 1
    assert google.run(undo, reconcile=True)["state"] == "verified" and google.sent("DELETE") == 1


# spec: ANYM-434
def test_ordinary_change_of_a_drive_keeps_its_link(store):
    s = store
    service, key, drop, pick = household(s)
    s.guardian_rule_disable(pick)
    google = Google(s)
    projected(service, key, google)
    [link] = s.query("SELECT target_id FROM projection_links")
    target = link["target_id"]

    def sync():
        drive = google.events["private-a", target]
        occurrences = [] if drive.get("status") == "cancelled" else [{
            "ref": "event-drive", "start": drive["start"]["dateTime"], "end": drive["end"]["dateTime"],
            "busy": True, "details": {"title": drive["summary"]}}]
        s.publish_window("cal-a", *WINDOW, [{"resource_ref": "res-drive", "target": target, "etag": drive["etag"],
                                             "payload": {}, "occurrences": occurrences}], "complete")

    sync()
    moved = {"title": "Drive to dance (early)", "start": "2026-10-20T09:30:00Z", "end": "2026-10-20T09:50:00Z",
             "timezone": "Asia/Makassar"}
    update = apply(service, key, [{"action": "update", "calendar_ref": "cal-a", "event_ref": "event-drive",
                                   "event": moved}], "request-move-drive")
    assert google.run(update)["state"] == "verified"
    assert [(row["state"], row["etag"]) for row in links(s)] == [("active", google.events["private-a", target]["etag"])]
    assert links(s)[0]["etag"] == '"2"'
    sync()
    assert error(lambda: service.plan(key, [unproject(), {"action": "delete", "calendar_ref": "cal-a",
                                                          "event_ref": "event-drive"}])).code == (
        "duplicate_resource_action")
    delete = apply(service, key, [{"action": "delete", "calendar_ref": "cal-a", "event_ref": "event-drive"}],
                   "request-delete-drive")
    assert google.run(delete)["state"] == "verified"
    assert [row["state"] for row in links(s)] == ["removed"]
    assert [st["link"]["duty"] for st in service.plan(key, [project()])["steps"]] == ["dropoff"]
    assert error(lambda: service.plan(key, [unproject()])).code == "nothing_to_unproject"


# spec: ANYM-434
def test_ordinary_write_does_not_depend_on_the_link_table(schema_one):  # noqa: F811 (fixture)
    s = schema_one
    family(s)
    s.grant("family", "adult-a", "cal-a", READ + WRITE, "details")
    key = s.agent("agent", "family", "adult-a", READ + WRITE, ["cal-a"])
    google = Google(s)
    google.events["private-a", "provider-errand"] = {
        "id": "provider-errand", "summary": "Errand", "etag": '"1"',
        "start": {"dateTime": "2026-10-20T09:00:00Z"}, "end": {"dateTime": "2026-10-20T09:30:00Z"}}
    s.publish_window("cal-a", *WINDOW, [{
        "resource_ref": "res-errand", "target": "provider-errand", "etag": '"1"', "payload": {},
        "occurrences": [{"ref": "event-errand", "start": "2026-10-20T09:00:00Z", "end": "2026-10-20T09:30:00Z",
                         "busy": True, "details": {"title": "Errand"}}],
    }], "complete")
    # Schema 1: no link table, so an ordinary update and delete verify exactly as before.
    for kind, extra in (("update", {"event": {"title": "Errand, later", "start": "2026-10-20T09:10:00Z",
                                              "end": "2026-10-20T09:40:00Z", "timezone": "UTC"}}), ("delete", {})):
        action = {"action": kind, "calendar_ref": "cal-a", "event_ref": "event-errand", **extra}
        assert google.run(apply(FamilyService(s), key, [action], "request-" + kind)) == {"state": "verified", "error": None}
        drive = google.events["private-a", "provider-errand"]
        s.publish_window("cal-a", *WINDOW, [{
            "resource_ref": "res-errand", "target": "provider-errand", "etag": drive["etag"], "payload": {},
            "occurrences": [{"ref": "event-errand", "start": "2026-10-20T09:10:00Z", "end": "2026-10-20T09:40:00Z",
                             "busy": True, "details": {"title": drive["summary"]}}],
        }], "complete")
    assert google.events["private-a", "provider-errand"]["status"] == "cancelled"
