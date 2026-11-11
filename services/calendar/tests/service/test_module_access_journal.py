"""ANYM-577: the calendar journals `module.access.changed` whenever a principal's effective access to a calendar changes.

Effective access is the full authorize(..., "agenda:read") predicate with details visibility. Triggers inside the writer's
own transaction journal it, so every write path is covered (the owner CLI, direct SQL below). Synthetic data only.
"""
import os
from datetime import timezone
from uuid import uuid4

import pytest

from family_calendar.errors import CalendarError
from family_calendar.service import journal
from family_calendar.service import migration_rollout as rollout
from family_calendar.service import store as store_module
from family_calendar.service.core import EVENTS, FamilyService
from family_calendar.service.store import Store

READ = ["agenda:read"]


START = "INSERT INTO journal_head(id) VALUES (1) ON CONFLICT DO NOTHING"


@pytest.fixture
def household(household):
    """The conftest household with the change journal started, as the worker starts it with its first write."""
    household.query(START)
    return household


def listener(s):
    """A router key bound to every calendar that exists now."""
    if not hasattr(s, "router_key"):
        # Owned by adult-b, whose membership no test disables: the router must outlive the changes it reads.
        s.router_key = s.agent("router", "family", "adult-b", [EVENTS], [r["ref"] for r in s.query("SELECT ref FROM calendars")])
    return s.router_key


def access(s, after=0):
    """The module.access.changed events a router key reads, as (principal_ref, access, reason, calendar_ref)."""
    page = FamilyService(s).events_page(listener(s), after, 200)
    return [(e["subject"], e["data"]["access"], e["data"]["reason"], e["scope_ref"])
            for e in page["events"] if e["type"] == "module.access.changed"]


def readable(s, principal="agent-a", calendars=("cal-a",), scopes=READ):
    s.grant("family", "adult-a", "cal-a", READ)
    return s.agent(principal, "family", "adult-a", scopes, list(calendars))


@pytest.fixture
def two(household):
    household.calendar("cal-b", "google-a", "private-b", "Other", "UTC", "read")
    household.grant("family", "adult-a", "cal-b", READ)
    return household


# spec: ANYM-577
def test_a_readable_binding_is_journaled_once_as_details_with_the_principals_expiry(household):
    s = household
    readable(s)
    events = FamilyService(s).events_page(listener(s), 0, 50)["events"]
    mine = [e for e in events if e["type"] == "module.access.changed"]
    assert [(e["subject"], e["scope_ref"], e["urgency"], e["expires_at"]) for e in mine] == [
        ("agent-a", "calendar:cal-a", "now", None)]
    expires = s.one("SELECT expires_at FROM principals WHERE id='agent-a'")["expires_at"]
    assert mine[0]["data"] == {"principal_ref": "agent-a", "access": "details", "reason": "granted",
        "until": journal.rfc3339(expires.astimezone(timezone.utc))}
    assert [e["id"] for e in events] == ["calendar:%d" % (i + 1) for i in range(len(events))]  # gapless, one seq space
    # An unchanged state journals nothing: a version bump, an unrelated column, the same grant again.
    s.query("UPDATE principals SET version=version+1 WHERE id='agent-a'")
    s.grant("family", "adult-a", "cal-a", READ)
    assert len(access(s)) == 1


# spec: ANYM-577
def test_a_binding_that_never_reads_with_details_is_never_journaled(two):
    s = two
    s.grant("family", "adult-a", "cal-a", ["availability:read"])
    s.agent("avail", "family", "adult-a", ["availability:read"], ["cal-a"])  # availability-only scope
    s.agent("busy", "family", "adult-a", READ, ["cal-b"], visibility="busy_only")  # agent layer busy-only
    s.grant("family", "adult-b", "cal-b", READ, "busy_only")
    s.agent("grant-busy", "family", "adult-b", READ, ["cal-b"])  # grant layer busy-only
    assert access(s) == []
    # listener() itself holds only calendar:events: no agenda:read, so it is no reader of anything.


# spec: ANYM-577
@pytest.mark.parametrize("change,reason", [
    ("UPDATE principals SET enabled=false WHERE id='agent-a'", "principal_removed"),
    ("UPDATE principals SET expires_at=now()-interval '1 minute' WHERE id='agent-a'", "expired"),
    ("UPDATE principals SET scopes='[\"availability:read\"]'::jsonb WHERE id='agent-a'", "permission_removed"),
    ("UPDATE memberships SET enabled=false WHERE person_id='adult-a'", "member_removed"),
    ("UPDATE connections SET enabled=false WHERE id='google-a'", "connection_disabled"),
    ("UPDATE calendar_grants SET visibility='busy_only' WHERE calendar_ref='cal-a'", "visibility_reduced"),
    ("UPDATE agent_calendars SET visibility='busy_only' WHERE principal_id='agent-a'", "visibility_reduced"),
    ("UPDATE calendar_grants SET scopes='[\"availability:read\"]'::jsonb WHERE calendar_ref='cal-a'",
     "permission_removed"),
    ("DELETE FROM calendar_grants WHERE calendar_ref='cal-a'", "permission_removed"),
    ("DELETE FROM agent_calendars WHERE principal_id='agent-a'", "permission_removed"),
])
def test_every_way_to_lose_the_permission_journals_none_with_its_reason_and_a_regrant_journals_details(household, change, reason):
    s = household
    readable(s)
    s.query(change)
    assert access(s)[-1] == ("agent-a", "none", reason, "calendar:cal-a") and len(access(s)) == 2
    # Restoring what the change took away journals details again (only for the changes that can be undone in place).
    undo = {"principal_removed": "UPDATE principals SET enabled=true WHERE id='agent-a'",
            "expired": "UPDATE principals SET expires_at=now()+interval '30 days' WHERE id='agent-a'",
            "member_removed": "UPDATE memberships SET enabled=true WHERE person_id='adult-a'",
            "connection_disabled": "UPDATE connections SET enabled=true WHERE id='google-a'"}.get(reason)
    if undo:
        s.query(undo)
        assert access(s)[-1][:3] == ("agent-a", "details", "granted") and len(access(s)) == 3


# spec: ANYM-577
def test_extending_a_principal_journals_the_new_until_and_other_calendars_are_judged_separately(two):
    s = two
    s.grant("family", "adult-a", "cal-a", READ)
    s.agent("agent-a", "family", "adult-a", READ, ["cal-a", "cal-b"])
    assert sorted(e[3] for e in access(s)) == ["calendar:cal-a", "calendar:cal-b"]
    s.extend_agent("agent-a", "200")
    assert len(access(s)) == 4  # one details event per calendar, with the extended expiry as `until`
    s.query("UPDATE calendar_grants SET visibility='busy_only' WHERE calendar_ref='cal-b'")
    assert access(s)[-1] == ("agent-a", "none", "visibility_reduced", "calendar:cal-b") and len(access(s)) == 5


# spec: ANYM-577
def test_a_reader_sees_the_access_events_of_its_bound_calendars_only(two):
    s = two
    listener(s)  # bound to cal-a and cal-b, before the third calendar exists
    s.calendar("cal-c", "google-a", "private-c", "Third", "UTC", "read")
    s.grant("family", "adult-a", "cal-c", READ)
    s.agent("agent-c", "family", "adult-a", READ, ["cal-c"])
    assert access(s) == []  # the router is bound to cal-a and cal-b only
    first = s.agent("other-router", "family", "adult-b", [EVENTS], ["cal-c"])
    page = FamilyService(s).events_page(first, 0, 50)
    assert [e["scope_ref"] for e in page["events"] if e["type"] == "module.access.changed"] == ["calendar:cal-c"]
    assert page["next_after"] >= int(page["events"][-1]["id"].split(":")[1]) and not page["has_more"]


@pytest.fixture
def schema_nine():
    dsn = os.environ.get("FCS_TEST_DSN")
    if not dsn:
        pytest.fail("FCS_TEST_DSN is required; never fall back to a runtime database")
    s = Store(dsn, schema="test_" + uuid4().hex)
    s.migrate(through=9)
    yield s
    try:
        s.drop_test_schema()
    finally:
        s.close()


def existing(s):
    """A schema-9 service with one reader and one router bound to one readable calendar."""
    s.person("adult-a", "Adult A")
    s.group("family", "Family")
    s.member("family", "adult-a", "group_admin")
    s.connection("google-a", "adult-a", "google")
    s.calendar("cal-a", "google-a", "private-a", "Private", "UTC", "write")
    s.grant("family", "adult-a", "cal-a", READ)
    s.agent("agent-a", "family", "adult-a", READ, ["cal-a"])
    s.agent("router", "family", "adult-a", [EVENTS], ["cal-a"])


def baseline(s):
    rows = s.query("SELECT principal_id,calendar_ref,access,reason FROM module_access_events ORDER BY seq")
    return [(r["principal_id"], r["calendar_ref"], r["access"], r["reason"]) for r in rows]


# spec: ANYM-577
def test_migration_10_journals_the_state_of_every_existing_binding_once_and_repeats_to_nothing(schema_nine):
    s = schema_nine
    existing(s)
    s.query(START)  # a journal the worker already started
    s.migrate()
    assert baseline(s) == [("agent-a", "cal-a", "details", "granted")]
    assert s.one("SELECT head FROM journal_head")["head"] == 1
    s.migrate()
    assert len(baseline(s)) == 1


# spec: ANYM-577
def test_a_journal_started_after_migration_10_journals_the_baseline_and_a_never_started_one_stays_not_ready(schema_nine):
    s = schema_nine
    existing(s)
    s.migrate()
    assert baseline(s) == [] and s.one("SELECT count(*) AS n FROM journal_head")["n"] == 0
    with pytest.raises(CalendarError, match="journal_not_ready"):  # a binding change never starts the journal
        s.query("UPDATE principals SET version=version+1 WHERE id='agent-a'")
        FamilyService(s).events_head(s.agent("head-reader", "family", "adult-a", [EVENTS], ["cal-a"]))
    s.query(START)  # the worker's first write creates the row: the baseline follows in the same transaction
    assert baseline(s) == [("agent-a", "cal-a", "details", "granted")]
    assert s.one("SELECT head FROM journal_head")["head"] == 1


# spec: ANYM-577
def test_migration_10_is_manual_with_additive_grants():
    files = dict(store_module.MIGRATIONS)
    created = {}
    for version in sorted(files):
        if version < 10:
            created = rollout.new_tables(files[version].read_text(), created)
    text = files[10].read_text()
    assert text.startswith("-- rollout: manual")
    roles_text = store_module.ROLES.read_text()
    base = roles_text[: roles_text.index("-- Migration 10 (module access journal")]
    assert rollout.role_class(base, roles_text, rollout.new_tables(text), tables=set(created)) == "additive"
