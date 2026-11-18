"""A stored plan step's `before` is display data only (ANYM-256): apply, validate_plan and the worker write the same
provider request, against the same etag, to the same event, whatever `before` says.

Each case runs the same update or delete twice on twin synthetic families, each in its own test schema, with an
in-memory Google calendar behind the real Google adapter, Providers and Worker: once as planned, and once with the
stored step's `before` replaced by a plausible other preview (another time, title, location, etag, or other shape
flags) while every routing and write field stays as planned. No live account, network or provider session."""
import asyncio
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from psycopg.types.json import Jsonb

from family_calendar.providers.google.adapter import GoogleAdapter
from family_calendar.service.core import FamilyService
from family_calendar.service.providers import Providers, normalize_google
from family_calendar.service.worker import Worker
from tests.fakes import FakeGoogle
from tests.service.test_policy import SCOPES

EVENT = {"id": "swim-lesson", "etag": '"g-swim"', "summary": "Swim", "location": "Pool", "description": "Bring towel",
         "start": {"dateTime": "2026-10-20T10:00:00Z"}, "end": {"dateTime": "2026-10-20T11:00:00Z"}}
WINDOW = ("2026-10-01T00:00:00Z", "2026-12-31T00:00:00Z")
MOVE = {"title": "Swim (moved)", "start": "2026-10-20T12:00:00Z", "end": "2026-10-20T13:30:00Z", "timezone": "UTC"}
POISONS = {
    # Another preview of another event: time, title, text and an etag the provider no longer has.
    "other-event": {"start": "2026-10-21T07:00:00+00:00", "end": "2026-10-21T08:15:00+00:00", "title": "Dentist",
                    "location": "Clinic", "description": "Bring forms", "etag": '"g-stale"'},
    # The same event previewed with every shape flag a guard or a time conversion could read.
    "other-shape": {"start": "2026-10-22", "end": "2026-10-23", "all_day": True, "instant": True, "recurring": True,
                    "guests": True, "special": True, "series_ref": "series-elsewhere",
                    "original_start": "2026-10-22T07:00:00+00:00"},
}


def seed(s):
    """The conftest household, then the write grant and opt-in an apply needs."""
    s.person("adult-a", "Adult A")
    s.group("family", "Family")
    s.member("family", "adult-a", "group_admin")
    s.connection("google-a", "adult-a", "google")
    s.calendar("cal-a", "google-a", "private-a", "Private", "Asia/Makassar", "write")
    s.grant("family", "adult-a", "cal-a", SCOPES, "details")
    s.write_opt_in("cal-a", True)
    return s.agent("display-only", "family", "adult-a", SCOPES, ["cal-a"])


@pytest.fixture
def twins(store):
    """Two identical families in separate test schemas: one runs the plan as stored, the other with `before` changed."""
    from family_calendar.service.store import Store

    other = Store(store.dsn, schema="test_" + uuid4().hex)
    other.migrate()
    try:
        yield store, other
    finally:
        try:
            other.drop_test_schema()
        finally:
            other.close()


def world():
    # The fake merges a patch into nested values in place: each world gets its own copy.
    server = FakeGoogle([deepcopy(EVENT)])
    providers = Providers(None, None)
    providers.adapters["google-a"] = GoogleAdapter(
        "google-a", httpx.AsyncClient(transport=httpx.MockTransport(server)), lambda: "canary-token")
    return providers, server


def planned(s, action, routing=None):
    """A real plan of one update or delete of the published event: (key, plan_id, stored payload, providers, server)."""
    key = seed(s)
    providers, server = world()
    published = normalize_google("cal-a", server.events["swim-lesson"], "Asia/Makassar")
    published["payload"] = {}
    s.publish_window("cal-a", *WINDOW, [published], "complete")
    request = {"action": action, "calendar_ref": "cal-a", "event_ref": published["occurrences"][0]["ref"]}
    if action == "update":
        request["event"] = MOVE
    plan_id = FamilyService(s).plan(key, [request])["plan_id"]
    return key, plan_id, s.one("SELECT payload FROM plans WHERE id=%s", (plan_id,))["payload"], providers, server


def store_steps(s, plan_id, payload, change):
    payload["steps"] = [change(dict(step)) for step in payload["steps"]]
    s.query("UPDATE plans SET payload=%s WHERE id=%s", (Jsonb(payload), plan_id))


def executed(s, key, plan_id, providers, server):
    """Apply and one worker run; everything the write left behind that does not carry a random local ID."""
    operation_id = FamilyService(s).apply(key, plan_id, "display-only-0001")["operation_id"]
    asyncio.run(Worker(s, providers).run_operation(operation_id))
    return {
        "requests": [(r.method, r.url.path, r.headers.get("if-match"), r.content) for r in server.requests
                     if r.method != "GET"],
        "provider": server.events,
        "operation": s.one("SELECT state,error FROM operations WHERE id=%s", (operation_id,)),
        "steps": s.query("SELECT ordinal,state,error,intent,receipt FROM operation_steps WHERE operation_id=%s "
                         "ORDER BY ordinal", (operation_id,)),
        "resources": s.query("SELECT ref,target,etag FROM resources ORDER BY ref"),
        "occurrences": s.query("SELECT ref,start_at,end_at,details FROM occurrences ORDER BY ref"),
    }


# spec: ANYM-256
@pytest.mark.parametrize("poison", sorted(POISONS))
@pytest.mark.parametrize("action", ["update", "delete"])
def test_a_changed_before_writes_exactly_what_the_plan_as_stored_writes(twins, action, poison):
    as_stored, changed = twins
    key, plan_id, payload, providers, server = planned(as_stored, action)
    [step] = payload["steps"]
    expected = executed(as_stored, key, plan_id, providers, server)
    # Positive control: the plan as stored really writes, conditionally, to the published event.
    assert expected["operation"] == {"state": "verified", "error": None}
    [(method, path, if_match, _)] = expected["requests"]
    assert (method, path.rpartition("/")[2], if_match) == (
        {"update": "PATCH", "delete": "DELETE"}[action], "swim-lesson", '"g-swim"')
    assert step["expected_etag"] == '"g-swim"' and step["target_id"] == "swim-lesson"
    if action == "update":
        written = expected["provider"]["swim-lesson"]
        assert (written["summary"], written["start"]["dateTime"], written["location"]) == (
            "Swim (moved)", "2026-10-20T12:00:00+00:00", "Pool")
    else:
        assert expected["provider"] == {}

    key, plan_id, payload, providers, server = planned(changed, action)
    assert payload["steps"] == [step]
    store_steps(changed, plan_id, payload, lambda s: dict(s, before={**s["before"], **POISONS[poison]}))
    [stored] = changed.one("SELECT payload FROM plans WHERE id=%s", (plan_id,))["payload"]["steps"]
    assert stored["before"] != step["before"] and {k: v for k, v in stored.items() if k != "before"} == {
        k: v for k, v in step.items() if k != "before"}
    assert executed(changed, key, plan_id, providers, server) == expected


# spec: ANYM-256
def test_the_comparison_sees_a_changed_write_field(store):
    """Sensitivity: the etag the `other-event` before names, put in the step's own expected_etag, is refused by the
    provider guard before any write, so a change of a field the worker does read shows up."""
    key, plan_id, payload, providers, server = planned(store, "update")
    store_steps(store, plan_id, payload, lambda s: dict(s, expected_etag=POISONS["other-event"]["etag"]))
    result = executed(store, key, plan_id, providers, server)
    assert result["requests"] == [] and result["provider"]["swim-lesson"]["summary"] == "Swim"
    assert (result["operation"]["state"], result["steps"][0]["error"]) == ("conflict", "version_conflict")
