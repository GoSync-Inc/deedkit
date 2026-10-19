import asyncio
import json

import httpx
import pytest

from family_calendar.errors import CalendarError
from family_calendar.service.core import READ, WRITE, FamilyService
from family_calendar.service.worker import Worker
from tests.service.test_guardian_rules import family

LESSON = ("2026-10-20T10:00:00Z", "2026-10-20T11:00:00Z")
WINDOW = ("2026-10-20T00:00:00Z", "2026-10-21T00:00:00Z")


def lessons(s, *extra, calendar="cal-child", title="Dance class"):
    """Publishes recurring lesson occurrences: event-lesson at LESSON, then event-lesson-N on the following hours."""
    occurrences = [("event-lesson", *LESSON, {})] + list(extra)
    s.publish_window(calendar, *WINDOW, [{
        "resource_ref": "res-" + ref, "target": "provider-" + ref, "etag": '"v1"', "payload": {},
        "occurrences": [{
            "ref": ref, "start": start, "end": end, "busy": True,
            "details": {"title": title, "location": "Studio 5", "description": "Bring shoes", "recurring": True,
                        "series_ref": "series-dance", "original_start": start, **details},
        }],
    } for ref, start, end, details in occurrences], "complete")


def household(s, *, pickup_target="cal-b"):
    """adult-a drives to dance (cal-a, private); adult-b collects (cal-b, details, with the studio address)."""
    family(s)
    s.write_opt_in("cal-b", True)
    s.write_opt_in("cal-ib", True)
    for cal in ("cal-a", "cal-b", "cal-ib"):
        s.grant("family", "adult-a", cal, READ + WRITE, "details")
    s.grant("family", "adult-a", "cal-child", READ, "details")
    key = s.agent("agent", "family", "adult-a", READ + WRITE, ["cal-a", "cal-b", "cal-ib", "cal-child"])
    drop = s.guardian_rule_set("family", "child", "Dance", "dropoff", "adult-a", "cal-child", "cal-a", 20,
                               "Drive to dance", "private")["rule_id"]
    pick = s.guardian_rule_set("family", "child", "Dance", "pickup", "adult-b", "cal-child", pickup_target, 15,
                               "Collect from dance", "details", True)["rule_id"]
    for cal in ("cal-a", "cal-b"):
        s.publish_window(cal, *WINDOW, [], "complete")
    lessons(s)
    return FamilyService(s), key, drop, pick


def project(event_ref="event-lesson", activity="Dance", calendar_ref="cal-child"):
    return {"action": "project", "calendar_ref": calendar_ref, "event_ref": event_ref, "activity": activity}


def error(call):
    with pytest.raises(CalendarError) as caught:
        call()
    return caught.value


def plans(s):
    return s.one("SELECT count(*) AS n FROM plans")["n"]


class Fake:
    """Records creates; `fail` maps a calendar to a definite provider error, `lose` loses the answer once."""

    def __init__(self, fail=None, lose=()):
        self.sent, self.fail, self.lose, self.answer = [], fail or {}, set(lose), True

    async def prepare(self, cal, step):
        target = step["target_id"]
        return {"action": "create", "target": target, "desired": {"summary": step["event"]["title"]},
                "expected_etag": None, "resource_ref": "res-" + target}

    async def mutate(self, cal, intent):
        self.sent.append((cal["ref"], intent["target"]))
        if cal["ref"] in self.fail:
            raise CalendarError(self.fail[cal["ref"]])
        if cal["ref"] in self.lose:
            self.answer = False
            raise CalendarError("unknown_result")

    async def verify(self, cal, intent):
        if not self.answer or (cal["ref"], intent["target"]) not in self.sent or cal["ref"] in self.fail:
            return None
        return {"resource_ref": intent["resource_ref"], "verified": True, "etag": '"e-' + intent["target"] + '"'}


def links(s):
    return s.query("SELECT rule_id,rule_version,duty,state,etag,operation_id,ordinal FROM projection_links ORDER BY duty,state")


def run(s, fake, operation_id, reconcile=False):
    asyncio.run(Worker(s, fake).run_operation(operation_id, reconcile=reconcile))
    return s.one("SELECT state,error FROM operations WHERE id=%s", (operation_id,))


def steps(s, operation_id):
    return s.query("SELECT ordinal,state,error FROM operation_steps WHERE operation_id=%s ORDER BY ordinal", (operation_id,))


# spec: ANYM-433
def test_activity_plan_previews_guardian_projection(store):
    s = store
    service, key, drop, pick = household(s)
    # A sibling's drive already sits in adult-a's calendar during the drop-off window.
    s.publish_window("cal-a", *WINDOW, [{
        "resource_ref": "res-sibling", "target": "provider-sibling", "etag": '"v1"', "payload": {},
        "occurrences": [{"ref": "event-sibling", "start": "2026-10-20T09:45:00Z", "end": "2026-10-20T09:55:00Z",
                         "busy": True, "details": {"title": "Drive to swim"}}],
    }], "complete")
    plan = service.plan(key, [project()])
    dropoff, pickup = plan["steps"]
    source = {"start": "2026-10-20T10:00:00+00:00", "end": "2026-10-20T11:00:00+00:00", "title": "Dance class"}
    assert dropoff == {
        "action": "create", "calendar_ref": "cal-a", "provider": "google", "ordinal": 0,
        "event": {"title": "Drive to dance", "start": "2026-10-20T09:40:00+00:00",
                  "end": "2026-10-20T10:00:00+00:00", "timezone": "Asia/Makassar"},
        "link": {"rule_id": drop, "rule_version": 1, "duty": "dropoff", "guardian_id": "adult-a",
                 "source_event_ref": "event-lesson"},
        "source": source,
        "diff": {"title": {"before": None, "after": "Drive to dance"},
                 "start": {"before": None, "after": "2026-10-20T09:40:00+00:00"},
                 "end": {"before": None, "after": "2026-10-20T10:00:00+00:00"},
                 "timezone": {"before": None, "after": "Asia/Makassar"}},
    }
    assert pickup["calendar_ref"] == "cal-b" and pickup["ordinal"] == 1 and pickup["source"] == source
    assert pickup["event"] == {"title": "Collect from dance", "start": "2026-10-20T10:45:00+00:00",
                               "end": "2026-10-20T11:00:00+00:00", "timezone": "Asia/Makassar",
                               "location": "Studio 5"}
    assert pickup["link"]["rule_id"] == pick and pickup["link"]["duty"] == "pickup"
    assert "Bring shoes" not in json.dumps(plan) and plan["projections_skipped"] == []
    assert {"step": 0, "calendar_ref": "cal-a", "start": "2026-10-20T09:45:00+00:00",
            "end": "2026-10-20T09:55:00+00:00", "visibility": "details",
            "event_ref": "event-sibling"} in plan["overlap_check"]["overlaps"]
    assert not any(o.get("step") == 1 for o in plan["overlap_check"]["overlaps"])

    constraints = service.capabilities(key)["constraints"]
    assert (constraints["plan_action_limit"], constraints["plan_step_limit"]) == (8, 16)
    # A sibling in the same class doubles the steps: 5 lessons x 4 rules = 20 steps for 5 actions.
    s.person("sibling", "sibling")
    s.member("family", "sibling", "dependent")
    s.binding("family", "sibling", "cal-child")
    for duty, guardian, target in (("dropoff", "adult-a", "cal-a"), ("pickup", "adult-b", "cal-b")):
        s.guardian_rule_set("family", "sibling", "Dance", duty, guardian, "cal-child", target, 20, "Drive", "private")
    extra = [(f"event-lesson-{h}", f"2026-10-20T{h}:00:00Z", f"2026-10-20T{h}:30:00Z", {}) for h in (12, 13, 14, 15)]
    lessons(s, *extra)
    before = plans(s)
    assert len(service.plan(key, [project(), project("event-lesson-12"), project("event-lesson-13"),
                                  project("event-lesson-14")])["steps"]) == 16
    actions = [project()] + [project(ref) for ref, *_ in extra]
    assert error(lambda: service.plan(key, actions)).code == "too_many_steps"
    assert plans(s) == before + 1
    for bad in ({"action": "create", "calendar_ref": "cal-a", "activity": "Dance",
                 "event": {"title": "x", "start": LESSON[0], "end": LESSON[1], "timezone": "UTC"}},
                {"action": "project", "calendar_ref": "cal-child", "event_ref": "event-lesson", "activity": ""},
                {"action": "project", "calendar_ref": "cal-child", "event_ref": "event-lesson", "activity": "x" * 65},
                {"action": "project", "calendar_ref": "cal-child", "event_ref": "event-lesson"}):
        assert error(lambda: service.plan(key, [bad])).code == "invalid_action"


# spec: ANYM-433
def test_projected_event_carries_only_opaque_link(store):
    from family_calendar.providers.google.adapter import GoogleAdapter
    from family_calendar.service.providers import Providers

    s = store
    service, key, drop, pick = household(s)
    plan = service.plan(key, [project()])
    operation_id = service.apply(key, plan["plan_id"], "request-opaque")["operation_id"]
    created, requests = {}, []

    def handle(request):
        requests.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            created[body["id"]] = dict(body, etag='"e1"')
            return httpx.Response(200, json=created[body["id"]])
        event = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=created[event]) if event in created else httpx.Response(404)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            providers = Providers(s, None)
            for connection in ("google-a", "google-b"):
                providers.adapters[connection] = GoogleAdapter(connection, client, lambda: "token")
            await Worker(s, providers).run_operation(operation_id)

    asyncio.run(go())
    posts = [r for r in requests if r.method == "POST"]
    assert [r.url.params["sendUpdates"] for r in posts] == ["none", "none"]
    bodies = {r.url.path.split("/")[-2]: json.loads(r.content) for r in posts}
    rows = {r["duty"]: r for r in s.query("SELECT id,duty,target_id,state FROM projection_links")}
    for target, duty, private in (("private-a", "dropoff", True), ("private-b", "pickup", False)):
        body = bodies[target]
        assert set(body) == {"id", "start", "end", "summary", "extendedProperties"} | (
            {"visibility"} if private else {"location"})
        assert body["id"] == rows[duty]["target_id"] and rows[duty]["state"] == "active"
        assert body.get("visibility") == ("private" if private else None)
        [(name, value)] = body["extendedProperties"]["private"].items()
        assert set(body["extendedProperties"]) == {"private"} and len(name) <= 44 and value == rows[duty]["id"]
        for leaked in ("child", "lesson", "Dance class", "Bring shoes", "provider-event", drop, pick):
            assert leaked not in json.dumps(body["extendedProperties"]) + body["id"]
        assert "attendees" not in body and "description" not in body
    assert bodies["private-b"]["location"] == "Studio 5"
    assert s.one("SELECT state FROM operations WHERE id=%s", (operation_id,))["state"] == "verified"


# spec: ANYM-433
def test_unbound_guardian_gets_no_projection(store):
    s = store
    service, key, drop, pick = household(s)
    # Another child of the class: one rule into iCloud, one whose guardian left, one into a calendar the agent lacks.
    s.person("adult-c", "adult-c")
    s.member("family", "adult-c", "adult")
    s.connection("google-c", "adult-c", "google")
    s.calendar("cal-c", "google-c", "private-c", "C", "Asia/Makassar", "write")
    s.binding("family", "adult-c", "cal-c")
    s.person("sibling", "sibling")
    s.member("family", "sibling", "dependent")
    s.binding("family", "sibling", "cal-child")
    icloud = s.guardian_rule_set("family", "sibling", "Dance", "dropoff", "adult-b", "cal-child", "cal-ib", 20,
                                 "Drive", "private")["rule_id"]
    left = s.guardian_rule_set("family", "sibling", "Dance", "pickup", "adult-c", "cal-child", "cal-c", 20,
                               "Collect", "private")["rule_id"]
    s.query("UPDATE memberships SET enabled=false WHERE person_id='adult-c'")
    s.query("UPDATE agent_calendars SET visibility='busy_only' WHERE calendar_ref='cal-b'")
    plan = service.plan(key, [project()])
    assert [st["link"]["rule_id"] for st in plan["steps"]] == [drop]
    assert sorted(plan["projections_skipped"], key=lambda x: x["rule_id"]) == sorted([
        {"rule_id": pick, "duty": "pickup", "guardian_id": "adult-b", "reason": "target_not_writable"},
        {"rule_id": icloud, "duty": "dropoff", "guardian_id": "adult-b", "reason": "provider_not_supported"},
        {"rule_id": left, "duty": "pickup", "guardian_id": "adult-c", "reason": "guardian_disabled"},
    ], key=lambda x: x["rule_id"])
    s.query("DELETE FROM agent_calendars WHERE calendar_ref='cal-b'")
    assert {x["reason"] for x in service.plan(key, [project()])["projections_skipped"]
            if x["rule_id"] == pick} == {"target_not_visible"}

    # Live and reserved claims: nothing is planned twice.
    for rule_id, duty, state, target in ((drop, "dropoff", "active", "t1"), (icloud, "dropoff", "pending", "t2")):
        s.query("INSERT INTO projection_links(id,rule_id,rule_version,duty,source_calendar_ref,source_event_ref,"
                "source_start,source_end,target_calendar_ref,target_id,state) VALUES (%s,%s,1,%s,'cal-child',"
                "'event-lesson',%s,%s,'cal-a',%s,%s)", ("link-" + target, rule_id, duty, *LESSON, target, state))
    before = plans(s)
    nothing = error(lambda: service.plan(key, [project()]))
    assert nothing.code == "nothing_to_project" and plans(s) == before
    reasons = {x["rule_id"]: x["reason"] for x in nothing.details["projections_skipped"]}
    assert (reasons[drop], reasons[icloud]) == ("already_projected", "projection_pending")
    assert nothing.details["activities"] == ["Dance"]
    swim = error(lambda: service.plan(key, [project(activity="Swim")]))
    assert (swim.code, swim.details) == ("nothing_to_project", {"projections_skipped": [], "activities": ["Dance"]})

    # A project with no step stays in a plan as a skip when another action has steps.
    create = {"action": "create", "calendar_ref": "cal-a",
              "event": {"title": "Errand", "start": "2026-10-20T13:00:00Z", "end": "2026-10-20T14:00:00Z",
                        "timezone": "UTC"}}
    mixed = service.plan(key, [project(), create])
    assert [st["action"] for st in mixed["steps"]] == ["create"] and len(mixed["projections_skipped"]) == 4
    assert error(lambda: service.plan(key, [project(), project()])).code == "duplicate_resource_action"


# spec: ANYM-433
def test_projection_requires_and_keeps_source_authority(store):
    s = store
    service, key, drop, pick = household(s)
    lessons(s, ("event-day", "2026-10-20T00:00:00Z", "2026-10-21T00:00:00Z", {"all_day": True}))
    s.publish_window("cal-a", *WINDOW, [{
        "resource_ref": "res-drive", "target": "fcs-drive", "etag": '"v1"', "payload": {},
        "occurrences": [{"ref": "event-drive", "start": "2026-10-20T09:40:00Z", "end": "2026-10-20T10:00:00Z",
                         "busy": True, "details": {"title": "Drive to dance"}}],
    }], "complete")
    s.query("INSERT INTO projection_links(id,rule_id,rule_version,duty,source_calendar_ref,source_event_ref,"
            "source_start,source_end,target_calendar_ref,target_id,state) VALUES ('link-x',%s,1,'dropoff',"
            "'cal-child','event-old',%s,%s,'cal-a','fcs-drive','active')", (drop, *LESSON))
    busy = s.agent("busy", "family", "adult-a", READ + WRITE, ["cal-a", "cal-b"])
    s.query("INSERT INTO agent_calendars VALUES ('busy','cal-child','busy_only')")
    no_rules = s.agent("no-rules", "family", "adult-a", [x for x in READ + WRITE if x != "assignment:read"],
                       ["cal-a", "cal-b", "cal-child"])
    for who, action, code in (
        (busy, project(), "not_found_or_forbidden"),
        (key, project("event-unknown"), "not_found_or_forbidden"),
        (key, project("event-drive"), "not_found_or_forbidden"),
        (key, project("event-day"), "source_all_day"),
        (key, project("event-drive", calendar_ref="cal-a"), "projection_source_is_projection"),
        (no_rules, project(), "not_found_or_forbidden"),
    ):
        refused = error(lambda: service.plan(who, [action]))
        assert refused.code == code and drop not in str(refused.details) and pick not in str(refused.details)
    s.guardian_readiness = lambda: "grants_not_ready"
    assert error(lambda: service.plan(key, [project()])).code == "capability_unverified"
    del s.guardian_readiness

    def revoke():
        s.grant("family", "adult-a", "cal-child", [], "details")

    def unlist():
        s.query("DELETE FROM agent_calendars WHERE principal_id='agent' AND calendar_ref='cal-child'")

    def busy_only():
        s.query("UPDATE calendar_grants SET visibility='busy_only',version=version+1 WHERE calendar_ref='cal-child'")

    def restore():
        s.grant("family", "adult-a", "cal-child", READ, "details")
        s.query("INSERT INTO agent_calendars VALUES ('agent','cal-child','details') ON CONFLICT DO NOTHING")

    for n, change in enumerate((revoke, unlist, busy_only)):
        restore()
        early, late = (service.plan(key, [project()])["plan_id"] for _ in range(2))
        late_op = service.apply(key, late, f"request-late-{n}")["operation_id"]
        change()
        assert error(lambda: service.apply(key, early, f"request-early-{n}")).code == "scope_changed"
        fake = Fake()
        assert run(s, fake, late_op) == {"state": "failed", "error": "scope_changed"}
        assert fake.sent == [] and [row for row in links(s) if row["operation_id"] == late_op] == []


# spec: ANYM-433
def test_projection_reservation_blocks_a_second_send(store):
    s = store
    service, key, drop, pick = household(s)
    s.guardian_rule_disable(pick)
    first, second = (service.plan(key, [project()])["plan_id"] for _ in range(2))
    a = service.apply(key, first, "request-first")["operation_id"]
    b = service.apply(key, second, "request-second")["operation_id"]
    fake = Fake(lose={"cal-a"})
    # A is stopped after the provider write and before its verified transaction.
    assert run(s, fake, a)["state"] == "unknown"
    assert [row["state"] for row in links(s)] == ["pending"]
    assert run(s, fake, b) == {"state": "failed", "error": "already_projected"}
    assert steps(s, b) == [{"ordinal": 0, "state": "failed", "error": "already_projected"}]
    assert len(fake.sent) == 1
    fake.answer = True
    assert run(s, fake, a, reconcile=True)["state"] == "verified"
    [link] = links(s)
    assert (link["state"], link["operation_id"], link["etag"]) == ("active", a, '"e-' + fake.sent[0][1] + '"')
    assert len(fake.sent) == 1


# spec: ANYM-433
def test_reconcile_links_under_the_frozen_rule_version(store):
    s = store
    service, key, drop, pick = household(s)
    s.guardian_rule_disable(pick)
    plan = service.plan(key, [project()])
    operation_id = service.apply(key, plan["plan_id"], "request-crash")["operation_id"]
    fake = Fake(lose={"cal-a"})
    assert run(s, fake, operation_id)["state"] == "unknown"
    s.guardian_rule_disable(drop)
    fake.answer = True
    assert run(s, fake, operation_id, reconcile=True)["state"] == "verified"
    expected = [{"rule_id": drop, "rule_version": 1, "duty": "dropoff", "state": "active",
                 "etag": '"e-' + fake.sent[0][1] + '"', "operation_id": operation_id, "ordinal": 0}]
    assert links(s) == expected and len(fake.sent) == 1
    assert run(s, fake, operation_id, reconcile=True)["state"] == "verified"
    assert links(s) == expected and len(fake.sent) == 1

    # Before a fresh send, a disabled or re-versioned rule stops the step with nothing written.
    s.query("DELETE FROM projection_links")
    s.guardian_rule_set("family", "child", "Dance", "dropoff", "adult-a", "cal-child", "cal-a", 20,
                        "Drive to dance", "private")
    for n, change in enumerate((
        lambda: s.guardian_rule_disable(drop),
        lambda: s.guardian_rule_set("family", "child", "Dance", "dropoff", "adult-a", "cal-child", "cal-a", 25,
                                    "Drive to dance", "private"),
    )):
        plan = service.plan(key, [project()])
        operation_id = service.apply(key, plan["plan_id"], f"request-changed-{n}")["operation_id"]
        change()
        fake = Fake()
        assert run(s, fake, operation_id) == {"state": "failed", "error": "rule_changed"}
        assert fake.sent == [] and links(s) == []
        s.guardian_rule_set("family", "child", "Dance", "dropoff", "adult-a", "cal-child", "cal-a", 20,
                            "Drive to dance", "private")


# spec: ANYM-433
def test_partial_projection_replans_only_the_missing_duty(store):
    s = store
    service, key, drop, pick = household(s)
    plan = service.plan(key, [project()])
    operation_id = service.apply(key, plan["plan_id"], "request-partial")["operation_id"]
    fake = Fake(fail={"cal-b": "write_rejected"})
    assert run(s, fake, operation_id) == {"state": "partial", "error": "write_rejected"}
    assert [row["state"] for row in steps(s, operation_id)] == ["verified", "failed"]
    assert [(row["duty"], row["state"]) for row in links(s)] == [("dropoff", "active"), ("pickup", "removed")]
    again = service.plan(key, [project()])
    assert [st["link"]["duty"] for st in again["steps"]] == ["pickup"]
    assert again["projections_skipped"] == [
        {"rule_id": drop, "duty": "dropoff", "guardian_id": "adult-a", "reason": "already_projected"}]
