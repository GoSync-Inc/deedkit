"""Calendar members linked to messaging people (ANYM-554): the owner's link, and person UUIDs in calendar_find_time."""
import os
from uuid import uuid4

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
