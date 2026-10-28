"""Calendar members linked to messaging people (ANYM-554): the owner's link, and person UUIDs in calendar_find_time."""
import os
import threading
import time
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from family_calendar.errors import CalendarError
from family_calendar.service import cli
from family_calendar.service.core import READ
from family_calendar.service.store import Store
from tests.service.test_find_time_intent import DAY, find, setup

MASHA = "6f1c2a3e-4b5d-4e6f-8a7b-9c0d1e2f3a4b"
ZOE = "0d9e8f7a-6b5c-4d3e-9f2a-1b0c9d8e7f6a"


def link(s, tmp_path, *argv):
    return cli.dispatch(cli.parser().parse_args(["member-person", *argv]), tmp_path, tmp_path, {}, s)


# spec: ANYM-554
def test_one_owner_command_links_and_unlinks_a_member_and_a_person(store, tmp_path):
    setup(store)
    assert link(store, tmp_path, "--member", "child", "--person", MASHA.upper()) == {
        "group_id": "family", "member_id": "child", "person_id": MASHA, "linked": True, "previous": None}
    # Relinking names the person it replaces, so the step can be undone; unlinking is the undo of a link.
    assert link(store, tmp_path, "--member", "child", "--person", ZOE)["previous"] == MASHA
    assert link(store, tmp_path, "--member", "child", "--unlink") == {
        "group_id": "family", "member_id": "child", "person_id": None, "linked": False, "previous": ZOE}
    assert store.query("SELECT * FROM member_people") == []
    link(store, tmp_path, "--member", "child", "--person", MASHA)
    # One person is one member of a calendar; an unknown member or a malformed UUID changes nothing.
    for argv, code in [(["--member", "adult-b", "--person", MASHA], "person_already_linked"),
                       (["--member", "nobody", "--person", ZOE], "not_found"),
                       (["--member", "adult-b", "--person", "masha"], "invalid_person_id")]:
        with pytest.raises(CalendarError) as caught:
            link(store, tmp_path, *argv)
        assert caught.value.code == code
    assert [(r["member_id"], str(r["person_uuid"])) for r in store.query("SELECT * FROM member_people")] == [
        ("child", MASHA)]
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["member-person", "--member", "child", "--person", MASHA, "--unlink"])


# spec: ANYM-554
def test_a_person_id_resolves_to_the_linked_member_of_the_callers_calendar_only(store, tmp_path):
    key = setup(store)
    link(store, tmp_path, "--member", "child", "--person", MASHA)
    found = find(store, key, people=[MASHA.upper()], window=DAY, duration=60, exclude_me=True)
    assert [m["member_id"] for m in found["people"]] == ["child"]
    assert found["slots"][0]["next"]["arguments"]["title"] == "Meeting with Masha"
    # A second family with its own principal: its linked member is hidden from the first caller, and the other way.
    store.person("zoe", "Zoe")
    store.group("other", "Other")
    store.member("other", "zoe", "group_admin")
    link(store, tmp_path, "--group", "other", "--member", "zoe", "--person", ZOE)
    other = store.agent("agent-other", "other", "zoe", READ, [])
    assert [m["member_id"] for m in find(store, other, people=[ZOE], window=DAY)["people"]] == ["zoe"]
    unknown = "11111111-2222-4333-8444-555555555555"
    for caller, person in [(key, ZOE), (other, MASHA), (key, unknown)]:
        hidden = find(store, caller, people=[person], window=DAY)
        assert hidden["error"] == "not_found" and hidden["next"] == {"tool": "calendar_members", "arguments": {}}
    # A disabled member is hidden too, although the link stays.
    store.query("UPDATE memberships SET enabled=false WHERE group_id='family' AND person_id='child'")
    assert find(store, key, people=[MASHA], window=DAY)["error"] == "not_found"


# spec: ANYM-554
def test_the_calendar_keeps_only_the_person_uuid_and_reads_nothing_from_messaging(store, tmp_path):
    key = setup(store)
    link(store, tmp_path, "--member", "child", "--person", MASHA)
    columns = store.query(
        "SELECT column_name,data_type FROM information_schema.columns WHERE table_schema=%s "
        "AND table_name='member_people' ORDER BY ordinal_position", (store.schema,))
    assert [(c["column_name"], c["data_type"]) for c in columns] == [
        ("group_id", "text"), ("member_id", "text"), ("person_uuid", "uuid"),
        ("linked_at", "timestamp with time zone")]
    # The answer names the calendar member only: no messaging name, handle or channel is fetched or returned.
    found = find(store, key, people=[MASHA], window=DAY, exclude_me=True)
    assert found["people"] == [{"member_id": "child", "label": "Masha", "role": "dependent"}]


# spec: ANYM-554
def test_a_person_whose_calendar_the_caller_cannot_read_is_not_found_like_an_unlinked_one(store, tmp_path):
    key = setup(store)
    # adult-b ("Anna") is bound to cal-b and cal-ib, which adult-a's agent cannot read.
    link(store, tmp_path, "--member", "adult-b", "--person", ZOE)
    unlinked = "11111111-2222-4333-8444-555555555555"
    for people in ([ZOE], [ZOE, "Ann"], ["Ann", ZOE]):
        # Same answer as an unlinked person, before a selection could name the member.
        assert find(store, key, people=people, window=DAY) == find(
            store, key, people=[unlinked if p == ZOE else p for p in people], window=DAY)
        assert find(store, key, people=people, window=DAY)["error"] == "not_found"
    # A principal of the same family who can read both calendars gets the member.
    for cal in ("cal-b", "cal-ib"):
        store.grant("family", "adult-b", cal, READ, "details")
    anna = store.agent("agent-b", "family", "adult-b", READ, ["cal-b", "cal-ib"])
    assert [m["member_id"] for m in find(store, anna, people=[ZOE], window=DAY, exclude_me=True)["people"]] == [
        "adult-b"]


# spec: ANYM-554
def test_a_person_id_never_matches_a_name_and_an_unbound_linked_member_resolves(store, tmp_path):
    key = setup(store)
    # A member labelled with the UUID itself is still not that person without the owner's link.
    store.person("helper", MASHA)
    assert find(store, key, people=[MASHA], window=DAY)["error"] == "not_found"
    partial = find(store, key, people=[MASHA[:8]], window=DAY)
    assert partial["error"] == "selection_required" and [c["id"] for c in partial["candidates"]] == ["helper"]
    # The helper has no binding: linked, it resolves and is reported unbound, as by name.
    link(store, tmp_path, "--member", "helper", "--person", ZOE)
    found = find(store, key, people=[ZOE], window=DAY, exclude_me=True)
    assert [m["member_id"] for m in found["people"]] == ["helper"] and found["unbound_members"] == ["helper"]


# spec: ANYM-554
def test_two_concurrent_links_of_one_person_end_with_one_link_and_person_already_linked(store, tmp_path):
    setup(store)
    dsn = os.environ["FCS_TEST_DSN"]
    with psycopg.connect(dsn) as other, psycopg.connect(dsn, autocommit=True) as watch:
        other.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(store.schema)))
        other.execute("INSERT INTO member_people(group_id,member_id,person_uuid) VALUES ('family','adult-b',%s)",
                      (MASHA,))
        outcome = {}

        def second():
            try:
                outcome["result"] = link(store, tmp_path, "--member", "child", "--person", MASHA)
            except CalendarError as e:
                outcome["error"] = e.code

        thread = threading.Thread(target=second)
        thread.start()
        # The second link passed its check and waits on the uncommitted row's unique key.
        deadline = time.monotonic() + 10
        while not watch.execute("SELECT 1 FROM pg_stat_activity WHERE wait_event_type='Lock' AND query LIKE "
                                "'INSERT INTO member_people%'").fetchone():
            assert time.monotonic() < deadline and thread.is_alive()
            time.sleep(0.05)
        other.commit()
        thread.join(10)
    assert outcome == {"error": "person_already_linked"}
    assert [r["member_id"] for r in store.query("SELECT member_id FROM member_people")] == ["adult-b"]


# spec: ANYM-554
def test_migration_6_leaves_every_existing_table_as_version_5_code_reads_it():
    s = Store(os.environ["FCS_TEST_DSN"], schema="test_" + uuid4().hex)

    def shape():
        return (
            s.query("SELECT table_name,column_name,data_type,is_nullable,column_default FROM "
                    "information_schema.columns WHERE table_schema=%s AND table_name<>'member_people' "
                    "ORDER BY 1,2", (s.schema,)),
            s.query("SELECT c.conrelid::regclass::text AS t,c.conname,pg_get_constraintdef(c.oid) AS d FROM "
                    "pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname=%s AND "
                    "c.conrelid::regclass::text<>'member_people' ORDER BY 1,2", (s.schema,)),
            s.query("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname=%s AND "
                    "tablename<>'member_people' ORDER BY 1,2", (s.schema,)),
        )

    try:
        s.migrate(through=5)
        before = shape()
        s.migrate()
        assert shape() == before
        assert s.one("SELECT max(version) AS v FROM migrations")["v"] == 6
    finally:
        try:
            s.drop_test_schema()
        finally:
            s.close()


# spec: ANYM-554
def test_migration_6_and_its_grant_are_an_unattended_expand_from_schema_5():
    from family_calendar.service import migration_rollout as rollout
    from family_calendar.service.store import MIGRATIONS, ROLES

    files = {v: Path(p).read_text() for v, p in MIGRATIONS}
    roles = ROLES.read_text()
    base = roles[:roles.index("-- Migration 6")]
    plan = rollout.plan({v: t for v, t in files.items() if v <= 5}, files, base, roles)
    assert plan["base"] == 5 and plan["target"] == 6 and "pause" not in plan


# spec: ANYM-554
def test_before_migration_6_a_person_id_is_not_found_and_names_still_resolve():
    old = Store(os.environ["FCS_TEST_DSN"], schema="test_" + uuid4().hex)
    try:
        old.migrate(through=5)
        key = setup(old)
        missing = find(old, key, people=[MASHA], window=DAY)
        assert missing["error"] == "not_found" and missing["next"] == {"tool": "calendar_members", "arguments": {}}
        assert [m["member_id"] for m in find(old, key, people=["Masha"], window=DAY)["people"]] == ["adult-a", "child"]
    finally:
        try:
            old.drop_test_schema()
        finally:
            old.close()
