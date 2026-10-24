"""Repeatable calendar agent-key issue (ANYM-495): caller key file, operation ID replay and conflict, lifetime, check."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import threading
import time
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from family_calendar.errors import CalendarError
from family_calendar.service import cli
from family_calendar.service import store as store_module

KEY = "fcs-agent-" + "a1" * 24


def dispatch(store, tmp_path, *argv):
    return cli.dispatch(cli.parser().parse_args(list(argv)), tmp_path, tmp_path, {}, store)


def key_file(tmp_path, value=KEY, name="agent.key", mode=0o600):
    path = tmp_path / name
    path.write_text(value + "\n")
    path.chmod(mode)
    return path


def state(store):
    return (store.query("SELECT id,key_hash,expires_at,operation_id,operation_request FROM principals ORDER BY id"),
            store.query("SELECT * FROM agent_calendars ORDER BY 1,2"))


def issue(store, tmp_path, path, operation, principal="planner", *extra):
    return dispatch(store, tmp_path, "agent-key", "--id", principal, "--member", "adult-a", "--calendar", "cal-a",
                    "--token-file", str(path), "--operation-id", operation, *extra)


@pytest.fixture
def granted(household):
    household.grant("family", "adult-a", "cal-a", ["agenda:read"], "details")
    return household


@pytest.fixture
def schema_two():
    from family_calendar.service.store import Store

    dsn = os.environ.get("FCS_TEST_DSN")
    if not dsn:
        pytest.fail("FCS_TEST_DSN is required; never fall back to a runtime database")
    s = Store(dsn, schema="test_" + uuid4().hex)
    s.migrate(through=2)
    yield s
    try:
        s.drop_test_schema()
    finally:
        s.close()


# spec: ANYM-495
def test_first_call_stores_only_the_hash_of_the_callers_key_and_returns_public_fields(granted, tmp_path):
    path = key_file(tmp_path)
    before = path.read_bytes()
    result = issue(granted, tmp_path, path, "op-1")
    assert set(result) == {"principal_id", "expires_at", "revoked", "operation_id", "replayed"}
    assert (result["principal_id"], result["revoked"], result["operation_id"], result["replayed"]) == (
        "planner", False, "op-1", False)
    principals, calendars = state(granted)
    assert [(p["id"], p["key_hash"], p["operation_id"]) for p in principals] == [
        ("planner", hashlib.sha256(KEY.encode()).hexdigest(), "op-1")]
    assert [c["calendar_ref"] for c in calendars] == ["cal-a"]
    assert path.read_bytes() == before
    assert KEY not in json.dumps(result) and hashlib.sha256(KEY.encode()).hexdigest() not in json.dumps(result)
    assert granted.agent_info([KEY])[0]["principal_id"] == "planner"


# spec: ANYM-495
def test_the_same_operation_and_request_replays_the_record_and_creates_nothing(granted, tmp_path):
    path = key_file(tmp_path)
    first = issue(granted, tmp_path, path, "op-1")
    before = state(granted)
    assert issue(granted, tmp_path, path, "op-1") == dict(first, replayed=True)
    assert state(granted) == before


# spec: ANYM-495
@pytest.mark.parametrize("change", ["days", "calendar", "principal", "key", "scopes", "member", "group", "calendars"])
def test_the_same_operation_with_another_request_or_key_is_refused_as_a_conflict(granted, tmp_path, change):
    issue(granted, tmp_path, key_file(tmp_path), "op-1")
    before = state(granted)
    path, principal, extra = key_file(tmp_path), "planner", []
    if change == "days":
        extra = ["--days", "30"]
    elif change == "calendar":
        extra = ["--visibility", "busy_only"]
    elif change == "principal":
        principal = "helper"
    elif change == "key":
        path = key_file(tmp_path, "fcs-agent-" + "b2" * 24, "other.key")
    elif change == "scopes":
        extra = ["--scopes", "agenda:read,event:read"]
    elif change == "member":
        extra = ["--member", "adult-b"]
    elif change == "group":
        extra = ["--group", "other"]
    elif change == "calendars":
        extra = ["--calendar", "cal-b"]
    with pytest.raises(CalendarError, match="^operation_conflict$"):
        issue(granted, tmp_path, path, "op-1", principal, *extra)
    assert state(granted) == before


# spec: ANYM-495
def test_a_principal_or_key_already_issued_under_another_operation_is_refused(granted, tmp_path):
    path = key_file(tmp_path)
    issue(granted, tmp_path, path, "op-1")
    before = state(granted)
    with pytest.raises(CalendarError, match="^principal_or_key_in_use$"):
        issue(granted, tmp_path, path, "op-2", "helper")
    with pytest.raises(CalendarError, match="^principal_or_key_in_use$"):
        issue(granted, tmp_path, key_file(tmp_path, "fcs-agent-" + "c3" * 24, "third.key"), "op-3")
    assert state(granted) == before


# spec: ANYM-495
def test_a_crash_after_the_commit_then_a_replay_returns_the_committed_agent(granted, tmp_path, monkeypatch):
    path = key_file(tmp_path)
    real = granted.transaction
    exits = []

    def crash_on_exit():
        manager = real()

        class Wrapped:
            def __enter__(self):
                return manager.__enter__()

            def __exit__(self, *failure):
                done = manager.__exit__(*failure)
                if failure[0] is None:
                    exits.append(True)
                    raise SystemExit("killed after commit")
                return done
        return Wrapped()

    monkeypatch.setattr(granted, "transaction", crash_on_exit)
    with pytest.raises(SystemExit):
        issue(granted, tmp_path, path, "op-1")
    monkeypatch.undo()
    assert exits == [True]
    before = state(granted)
    assert [p["operation_id"] for p in before[0]] == ["op-1"]
    result = issue(granted, tmp_path, path, "op-1")
    assert result["replayed"] is True and result["principal_id"] == "planner"
    assert state(granted) == before


# spec: ANYM-495
@pytest.mark.parametrize("days,error", [("0", True), ("1", False), ("183", False), ("184", True), ("x", True),
                                        (None, False)], ids=["0", "1", "183", "184", "text", "default"])
def test_agent_key_days_is_1_to_183_and_defaults_to_90(granted, tmp_path, days, error):
    extra = [] if days is None else ["--days", days]
    if error:
        with pytest.raises(CalendarError, match="^invalid_agent_lifetime$"):
            issue(granted, tmp_path, key_file(tmp_path), "op-1", "planner", *extra)
        assert state(granted)[0] == []
        return
    result = issue(granted, tmp_path, key_file(tmp_path), "op-1", "planner", *extra)
    expected = datetime.now(timezone.utc) + timedelta(days=90 if days is None else int(days))
    assert abs(datetime.fromisoformat(result["expires_at"]) - expected) < timedelta(minutes=5)


# spec: ANYM-495
def test_agent_key_without_the_new_options_still_writes_a_90_day_key_to_its_output(granted, tmp_path):
    output = tmp_path / "planner.key"
    result = dispatch(granted, tmp_path, "agent-key", "--id", "planner", "--member", "adult-a", "--calendar", "cal-a",
                      "--output", str(output))
    assert result == {"principal_id": "planner", "key_saved": True, "expires_days": 90}
    principal = state(granted)[0][0]
    assert principal["operation_id"] is None and principal["operation_request"] is None
    assert principal["key_hash"] == hashlib.sha256(output.read_text().strip().encode()).hexdigest()


# spec: ANYM-495
def test_agent_info_by_operation_id_reads_back_public_fields_only(granted, tmp_path):
    issued = issue(granted, tmp_path, key_file(tmp_path), "op-1")
    result = dispatch(granted, tmp_path, "agent-info", "--operation-id", "op-1")
    assert result == {"agents": [{"found": True, "principal_id": "planner", "expires_at": issued["expires_at"],
                                  "revoked": False, "operation_id": "op-1"}]}
    assert dispatch(granted, tmp_path, "agent-info", "--operation-id", "op-x") == {
        "agents": [{"found": False, "operation_id": "op-x"}]}


# spec: ANYM-495
@pytest.mark.parametrize("problem,code", [("group-readable", "unsafe_key_file"), ("two-lines", "invalid_key"),
                                          ("short", "invalid_key"), ("operation", "invalid_operation_id"),
                                          ("symlink", "unsafe_key_file"), ("fifo", "unsafe_key_file"),
                                          ("too-large", "invalid_key")])
def test_an_unsafe_key_file_or_a_bad_operation_id_is_refused_before_any_write(granted, tmp_path, problem, code):
    before = state(granted)
    path = key_file(tmp_path, mode=0o640 if problem == "group-readable" else 0o600)
    if problem == "two-lines":
        path.write_text(KEY + "\n" + KEY + "\n")
    elif problem == "short":
        path.write_text("short\n")
    elif problem == "symlink":
        link = tmp_path / "link.key"
        link.symlink_to(path)
        path = link
    elif problem == "fifo":
        path = tmp_path / "pipe.key"
        os.mkfifo(path, 0o600)
    elif problem == "too-large":
        path.write_text("x" * 1_000_000)
    with pytest.raises(CalendarError) as failure:
        issue(granted, tmp_path, path, "bad id!" if problem == "operation" else "op-1")
    assert str(failure.value) == code
    assert state(granted) == before


def check_arguments(path):
    return {
        "agent-key": ["--id", "planner", "--member", "adult-a", "--token-file", str(path), "--operation-id", "op-1"],
        "extend-agent": ["--id", "planner", "--days", "30"],
        "revoke-agent": ["--id", "planner"],
        "list-agents": [],
        "agent-info": ["--operation-id", "op-1"],
    }


# spec: ANYM-495
@pytest.mark.parametrize("command", list(check_arguments("x")))
@pytest.mark.parametrize("current", [True, False], ids=["current", "n-1"])
def test_check_validates_and_reports_the_schema_and_writes_nothing(granted, schema_two, tmp_path, command, current):
    store = granted if current else schema_two
    path = key_file(tmp_path)
    before = store.query("SELECT id,key_hash,expires_at,enabled FROM principals ORDER BY id")
    result = dispatch(store, tmp_path, command, *check_arguments(path)[command], "--check")
    assert result == ({"check": "ok", "schema_version": 3, "required": 3} if current
                      else {"check": "schema_not_ready", "schema_version": 2, "required": 3})
    assert store.query("SELECT id,key_hash,expires_at,enabled FROM principals ORDER BY id") == before


# spec: ANYM-495
def test_issue_by_operation_on_an_n_minus_1_schema_is_refused_before_any_write(schema_two, tmp_path):
    with pytest.raises(CalendarError, match="^schema_not_ready$"):
        schema_two.agent("planner", "family", "adult-a", ["agenda:read"], [], key=KEY, operation_id="op-1")
    assert schema_two.query("SELECT id FROM principals") == []


# spec: ANYM-495
def test_migration_three_is_additive_and_repeatable(schema_two):
    schema_two.person("adult-a", "Adult A")
    schema_two.group("family", "Family")
    schema_two.member("family", "adult-a", "group_admin")
    key = schema_two.agent("planner", "family", "adult-a", ["agenda:read"], [])
    before = schema_two.query("SELECT id,key_hash,expires_at,enabled FROM principals")
    schema_two.migrate()
    migrated = schema_two.query("SELECT id,key_hash,expires_at,enabled,operation_id,operation_request FROM principals")
    assert [{k: v for k, v in row.items() if not k.startswith("operation")} for row in migrated] == before
    assert all(row["operation_id"] is None and row["operation_request"] is None for row in migrated)
    schema_two.migrate()
    assert schema_two.query(
        "SELECT id,key_hash,expires_at,enabled,operation_id,operation_request FROM principals") == migrated
    v3 = hashlib.sha256(store_module.MIGRATIONS[2][1].read_bytes()).hexdigest()
    assert schema_two.query("SELECT version,checksum FROM migrations ORDER BY version")[-1] == {
        "version": 3, "checksum": v3}
    assert schema_two.agent_info([key])[0]["principal_id"] == "planner"
    with pytest.raises(psycopg.errors.CheckViolation):
        schema_two.query("UPDATE principals SET operation_id='only-half' WHERE id='planner'")


# spec: ANYM-495
@pytest.mark.parametrize("problem,code", [("member", "unknown_member"), ("calendar", "unknown_calendar"),
                                          ("principal", "principal_or_key_in_use"), ("key", "principal_or_key_in_use"),
                                          ("conflict", "operation_conflict")])
def test_agent_key_check_runs_the_issue_validation_and_writes_nothing(granted, tmp_path, problem, code):
    issue(granted, tmp_path, key_file(tmp_path), "op-1")
    before = state(granted)
    path, principal, operation, extra = key_file(tmp_path, "fcs-agent-" + "e5" * 24, "new.key"), "helper", "op-2", []
    if problem == "member":
        extra = ["--member", "nobody"]
    elif problem == "calendar":
        extra = ["--calendar", "no-such-calendar"]
    elif problem == "principal":
        principal = "planner"
    elif problem == "key":
        path = key_file(tmp_path)
    else:
        operation = "op-1"
    with pytest.raises(CalendarError) as failure:
        issue(granted, tmp_path, path, operation, principal, *extra, "--check")
    assert str(failure.value) == code
    assert state(granted) == before


# spec: ANYM-495
@pytest.mark.parametrize("problem,code", [("missing", None), ("too-large", "key_file_too_large"),
                                          ("empty", "invalid_key_count")])
def test_agent_info_check_reads_and_bounds_the_key_file(granted, tmp_path, problem, code):
    path = tmp_path / "keys.txt"
    if problem == "too-large":
        path.write_text((KEY + "\n") * 2000)
    elif problem == "empty":
        path.write_text("")
    with pytest.raises(FileNotFoundError if code is None else CalendarError) as failure:
        dispatch(granted, tmp_path, "agent-info", "--key-file", str(path), "--check")
    if code:
        assert str(failure.value) == code


# spec: ANYM-495
@pytest.mark.parametrize("collision,code", [("operation", "operation_conflict"), ("key", "principal_or_key_in_use")])
def test_a_concurrent_issue_that_loses_the_operation_or_key_is_refused_without_a_unique_violation(
        granted, collision, code):
    # Another transaction holds an uncommitted principal with the operation ID and key; it commits while the
    # issue waits on the unique index, and the issue reads the winner back instead of failing on the index.
    winner = psycopg.connect(granted.dsn, autocommit=False)
    try:
        winner.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(granted.schema)))
        winner.execute(
            "INSERT INTO principals(id,group_id,person_id,scopes,key_hash,operation_id,operation_request) "
            "VALUES ('winner','family','adult-a','[]',%s,'op-race',%s)",
            (hashlib.sha256(KEY.encode()).hexdigest(), "f" * 64))
        results = {}

        def call():
            try:
                results["second"] = granted.agent(
                    "planner", "family", "adult-a", ["agenda:read"], ["cal-a"],
                    key="fcs-agent-" + "f6" * 24 if collision == "operation" else KEY,
                    operation_id="op-race" if collision == "operation" else "op-other")
            except Exception as error:
                results["second"] = error
        second = threading.Thread(target=call)
        second.start()
        time.sleep(0.5)
        winner.commit()
        second.join(30)
    finally:
        winner.close()
    assert isinstance(results["second"], CalendarError) and str(results["second"]) == code
    assert [p["id"] for p in state(granted)[0]] == ["winner"]


# spec: ANYM-495
def test_principal_lookups_on_a_warm_connection_survive_migration_three(schema_two):
    from family_calendar.service.core import FamilyService

    schema_two.person("adult-a", "Adult A")
    schema_two.group("family", "Family")
    schema_two.member("family", "adult-a", "group_admin")
    key = schema_two.agent("planner", "family", "adult-a", ["agenda:read"], [])
    service = FamilyService(schema_two)
    for _ in range(8):
        assert service.principal(key)["id"] == "planner"
    schema_two.migrate()
    for _ in range(8):
        assert service.principal(key)["id"] == "planner"


# spec: ANYM-495
def test_a_concurrent_identical_issue_waits_on_the_winner_and_replays_it(granted):
    request = hashlib.sha256(json.dumps(
        ["planner", "family", "adult-a", ["agenda:read"], ["cal-a"], "details", 90]).encode()).hexdigest()
    winner = psycopg.connect(granted.dsn, autocommit=False)
    try:
        winner.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(granted.schema)))
        winner.execute(
            "INSERT INTO principals(id,group_id,person_id,scopes,key_hash,operation_id,operation_request) "
            "VALUES ('planner','family','adult-a','[\"agenda:read\"]',%s,'op-race',%s)",
            (hashlib.sha256(KEY.encode()).hexdigest(), request))
        results = {}

        def call():
            try:
                results["second"] = granted.agent("planner", "family", "adult-a", ["agenda:read"], ["cal-a"],
                                                  key=KEY, operation_id="op-race")
            except Exception as error:
                results["second"] = error
        second = threading.Thread(target=call)
        second.start()
        time.sleep(0.5)
        winner.commit()
        second.join(30)
    finally:
        winner.close()
    assert results["second"]["replayed"] is True and results["second"]["principal_id"] == "planner"
    assert [p["id"] for p in state(granted)[0]] == ["planner"]
