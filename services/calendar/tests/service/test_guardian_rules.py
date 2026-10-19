import hashlib
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from family_calendar.errors import CalendarError
from family_calendar.service import store as store_module
from family_calendar.service.core import READ, WRITE, FamilyService
from family_calendar.service.store import Store

SERVICE = Path(store_module.__file__).parent
# Migration 1 as recorded in production; it must never change.
SCHEMA_ONE_SHA256 = "398ca8d43e3bbe9b5a90191001ea6d194aef1f732f8d5c43e1091856072a105c"
W0, W1 = "2026-10-20T00:00:00Z", "2026-10-21T00:00:00Z"


@pytest.fixture
def schema_one():
    dsn = os.environ.get("FCS_TEST_DSN")
    if not dsn:
        pytest.fail("FCS_TEST_DSN is required; never fall back to a runtime database")
    s = Store(dsn, schema="test_" + uuid4().hex)
    s.migrate(through=1)
    yield s
    try:
        s.drop_test_schema()
    finally:
        s.close()


def family(s):
    """Synthetic household: two adults, a child with a lesson calendar, one helper."""
    for person, role in [
        ("adult-a", "group_admin"),
        ("adult-b", "adult"),
        ("child", "dependent"),
        ("helper", "helper"),
    ]:
        s.person(person, person)
        s.group("family", "Family")
        s.member("family", person, role)
    s.connection("google-a", "adult-a", "google")
    s.connection("google-b", "adult-b", "google")
    s.connection("icloud-b", "adult-b", "icloud")
    s.calendar("cal-a", "google-a", "private-a", "A", "Asia/Makassar", "write")
    s.calendar("cal-child", "google-a", "child", "Lessons", "Asia/Makassar", "write")
    s.calendar("cal-b", "google-b", "private-b", "B", "Asia/Makassar", "write")
    s.calendar("cal-ib", "icloud-b", "/ib/", "B iCloud", "Asia/Makassar", "write")
    for person, cal in [("adult-a", "cal-a"), ("child", "cal-child"), ("adult-b", "cal-b"), ("adult-b", "cal-ib")]:
        s.binding("family", person, cal)
    s.write_opt_in("cal-a", True)
    return s


def rule(s, tmp_path, *argv):
    from family_calendar.service import cli

    return cli.dispatch(cli.parser().parse_args(["guardian-rule", *argv]), tmp_path, tmp_path, {}, s)


def dance(*extra, duty="dropoff", guardian="adult-a", target="cal-a", visibility="private"):
    argv = ["set", "--dependent", "child", "--activity", "Dance", "--duty", duty, "--guardian", guardian,
            "--source-calendar", "cal-child", "--target-calendar", target, "--lead-minutes", "20",
            "--title", "Drive to dance"]
    return argv + (["--visibility", visibility] if visibility else []) + list(extra)


def rows(s):
    return (
        s.query("SELECT * FROM guardian_rules ORDER BY id"),
        s.query("SELECT * FROM guardians ORDER BY adult_id,dependent_id"),
        s.query("SELECT action,ref,outcome FROM audit ORDER BY id"),
    )


def snapshot(s):
    """Every existing table's rows and columns, the version-1 schema as base code sees it."""
    tables = [r["table_name"] for r in s.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=%s ORDER BY 1", (s.schema,)
    )]
    columns = s.query(
        "SELECT table_name,column_name,data_type,is_nullable,column_default FROM information_schema.columns "
        "WHERE table_schema=%s ORDER BY 1,2",
        (s.schema,),
    )
    data = {t: s.query(sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(t))) for t in tables}
    return tables, columns, data


# spec: ANYM-432
def test_migration_two_is_additive_and_repeatable(schema_one, tmp_path, monkeypatch):
    from family_calendar.service import cli

    s = family(schema_one)
    s.grant("family", "adult-a", "cal-a", READ, "details")
    tables, columns, data = snapshot(s)
    assert "guardian_rules" not in tables
    s.migrate()
    migrated = snapshot(s)
    after_tables, after_columns, after_data = snapshot(s)
    assert set(after_tables) - set(tables) == {"guardian_rules", "projection_links"}
    assert [c for c in after_columns if c["table_name"] in tables] == columns
    v2 = hashlib.sha256((SERVICE / "schema_v2.sql").read_bytes()).hexdigest()
    assert after_data.pop("migrations") == [
        {"version": 1, "checksum": SCHEMA_ONE_SHA256},
        {"version": 2, "checksum": v2},
    ]
    data.pop("migrations")
    assert {t: v for t, v in after_data.items() if t in data} == data
    assert hashlib.sha256((SERVICE / "schema.sql").read_bytes()).hexdigest() == SCHEMA_ONE_SHA256
    s.migrate()
    assert snapshot(s) == migrated
    status = cli.dispatch(cli.parser().parse_args(["schema-status"]), tmp_path, tmp_path, {}, s)
    assert [m["version"] for m in status["migrations"]] == [1, 2]
    edited = tmp_path / "schema_v2.sql"
    edited.write_text((SERVICE / "schema_v2.sql").read_text() + "\n-- edited\n")
    monkeypatch.setattr(store_module, "MIGRATIONS", ((1, SERVICE / "schema.sql"), (2, edited)))
    with pytest.raises(CalendarError, match="migration_checksum_mismatch"):
        s.migrate()


@pytest.fixture
def roles(schema_one):
    """Real gateway and worker roles from roles.sql, renamed per test schema; dropped afterwards."""
    s = schema_one
    names = {"fcs_gateway": s.schema + "_gw", "fcs_worker": s.schema + "_wk"}
    stores = []

    def apply():
        text = (SERVICE / "roles.sql").read_text()
        for old, new in names.items():
            text = text.replace(old, new)
        text = text.replace("fcs.", s.schema + ".").replace("SCHEMA fcs ", "SCHEMA " + s.schema + " ")
        with s.transaction() as c:
            for role in names.values():
                if not c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                    c.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(sql.Identifier(role)))
            c.execute(text)

    def connect(profile):
        role = names["fcs_" + profile]
        store = Store(make_conninfo(s.dsn, options="-c role=" + role), s.schema)
        stores.append(store)
        return store

    yield apply, connect
    for store in stores:
        store.close()
    with psycopg.connect(s.dsn, autocommit=True) as c:
        for role in names.values():
            if c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                c.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                c.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


class Provider:
    """First send loses its answer, so the path covers plan, apply, unknown and reconcile."""

    def __init__(self):
        self.sent = 0

    async def prepare(self, cal, step):
        return {"target": step["target_id"], "desired": {"title": "Test"}, "expected_etag": None, "action": "create"}

    async def mutate(self, cal, intent):
        self.sent += 1
        raise CalendarError("unknown_result")

    async def verify(self, cal, intent):
        return {"etag": '"v1"'} if self.sent else None


VOLATILE = {"assignment_id", "plan_id", "operation_id", "id", "expires_at", "created_at", "checked_at", "request_id", "target_id"}


def stable(value, hour):
    """Drops random IDs and the per-run hour so runs on different schemas compare equal."""
    if isinstance(value, dict):
        return {k: "*" if k in VOLATILE else stable(v, hour) for k, v in value.items()}
    if isinstance(value, list):
        return [stable(v, hour) for v in value]
    if isinstance(value, str):
        return value.replace(f"2026-10-20T{hour:02}:", "2026-10-20THH:").replace(f"request-{hour}", "request-H")
    return value


async def existing_paths(gateway, worker, key, hour):
    from family_calendar.service.worker import Worker

    service = FamilyService(gateway)
    members = service.family(key)
    agenda = service.agenda(key, ["cal-a"], W0, W1)
    slots = service.slots(key, ["adult-a"], W0, W1, duration_minutes=30)
    start, end = f"2026-10-20T{hour:02}:00:00Z", f"2026-10-20T{hour:02}:30:00Z"
    assignment = service.assignment(key, "adult-a", "pickup", start, end, "assigned")
    listed = service.assignments(key, start, end)
    event = {"title": "Test", "start": start, "end": end, "timezone": "Asia/Makassar"}
    plan = service.plan(key, [{"action": "create", "calendar_ref": "cal-a", "event": event}])
    op = service.apply(key, plan["plan_id"], f"request-{hour}")["operation_id"]
    provider = Provider()
    await Worker(worker, provider).run_operation(op)
    first = service.operation(key, op)["state"]
    await Worker(worker, provider).run_operation(op, reconcile=True)
    final = service.operation(key, op)
    return stable([members, agenda, slots, assignment, listed, plan, first, final, provider.sent], hour)


# spec: ANYM-432
@pytest.mark.asyncio
async def test_schema_one_and_missing_grants_keep_existing_paths(schema_one, roles):
    apply_roles, connect = roles
    s = family(schema_one)
    s.grant("family", "adult-a", "cal-a", READ + WRITE, "details")
    key = s.agent("agent", "family", "adult-a", READ + WRITE, ["cal-a"])
    apply_roles()
    gateway, worker = connect("gateway"), connect("worker")
    service = FamilyService(gateway)

    # New code, schema 1.
    baseline = await existing_paths(gateway, worker, key, 1)
    assert (baseline[6], baseline[7]["state"], baseline[8]) == ("unknown", "verified", 1)
    assert service.capabilities(key)["features"]["guardian_projections"] == {
        "available": False, "reason": "schema_not_ready"
    }
    assert all("guardian_rules" not in m for m in service.family(key)["members"])

    # New code, schema 2 before db-roles: the runtime role cannot read the new tables, and nothing tries to.
    s.migrate()
    assert await existing_paths(gateway, worker, key, 2) == baseline
    assert service.capabilities(key)["features"]["guardian_projections"] == {
        "available": False, "reason": "grants_not_ready"
    }
    assert all("guardian_rules" not in m for m in service.family(key)["members"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        gateway.query("SELECT 1 FROM guardian_rules")

    # Base-commit code on schema 2: its only migration check (version 1 checksum) still passes,
    # and every path it runs returns what it returned on schema 1.
    assert s.one("SELECT checksum FROM migrations WHERE version=1")["checksum"] == SCHEMA_ONE_SHA256
    apply_roles()
    ready = await existing_paths(gateway, worker, key, 3)
    for m in ready[0]["members"]:
        m.pop("guardian_rules", None)
    assert ready == baseline
    assert service.capabilities(key)["features"]["guardian_projections"] == {"available": True}


@pytest.fixture
def ruled(store):
    return family(store)


# spec: ANYM-432
def test_guardian_rule_versions(ruled, tmp_path):
    s = ruled
    first = rule(s, tmp_path, *dance())
    assert first["version"] == 1 and first["rule_id"].startswith("rule-")
    assert first["consequence"] == "viewers of this calendar without edit rights see only busy"
    assert s.query("SELECT adult_id,dependent_id FROM guardians") == [{"adult_id": "adult-a", "dependent_id": "child"}]
    second = rule(s, tmp_path, *dance("--copy-location", visibility="details"))
    assert (second["rule_id"], second["version"]) == (first["rule_id"], 2)
    assert second["consequence"] == (
        "everyone who can see event details in this calendar sees the title, time and place"
    )
    icloud = rule(s, tmp_path, *dance(duty="pickup", guardian="adult-b", target="cal-ib"))
    assert icloud["consequence"] == (
        "iCloud does not enforce private events: everyone who can see this calendar sees the title and time"
    )
    before = rows(s)
    refusals = [
        ("invalid_guardian", dance(guardian="helper", target="cal-a")),
        ("invalid_guardian", dance(guardian="child")),
        ("binding_required", dance(guardian="adult-b", target="cal-a")),
        ("version_conflict", dance("--expected-version", "1")),
    ]
    for code, argv in refusals:
        with pytest.raises(CalendarError, match=code):
            rule(s, tmp_path, *argv)
    for lead in ("0", "181", "1.5", "-5", "twenty"):
        argv = dance()
        argv[argv.index("--lead-minutes") + 1] = lead
        with pytest.raises(CalendarError, match="invalid_lead_time"):
            rule(s, tmp_path, *argv)
    for field, value in (("--activity", ""), ("--activity", "x" * 65), ("--title", ""), ("--title", "x" * 129)):
        argv = dance()
        argv[argv.index(field) + 1] = value
        with pytest.raises(CalendarError, match="invalid_rule"):
            rule(s, tmp_path, *argv)
    source = dance()
    source[source.index("--source-calendar") + 1] = "cal-a"
    with pytest.raises(CalendarError, match="binding_required"):
        rule(s, tmp_path, *source)
    with pytest.raises(CalendarError, match="visibility_required") as missing:
        rule(s, tmp_path, *dance(visibility=None))
    assert missing.value.details == {
        "options": {
            "private": "viewers of this calendar without edit rights see only busy",
            "details": "everyone who can see event details in this calendar sees the title and time",
        }
    }
    s.query("UPDATE memberships SET enabled=false WHERE person_id='adult-b'")
    with pytest.raises(CalendarError, match="invalid_guardian"):
        rule(s, tmp_path, *dance(duty="pickup", guardian="adult-b", target="cal-ib"))
    assert rows(s) == before
    rule(s, tmp_path, "disable", "--id", first["rule_id"])
    revived = rule(s, tmp_path, *dance("--expected-version", "3"))
    assert revived["version"] == 4
    assert s.one("SELECT enabled FROM guardian_rules WHERE id=%s", (first["rule_id"],))["enabled"] is True


# spec: ANYM-432
def test_guardian_rule_disable_and_audit(ruled, tmp_path):
    s = ruled
    title = "CANARY TITLE"
    argv = dance()
    argv[argv.index("--title") + 1] = title
    made = rule(s, tmp_path, *argv)
    with pytest.raises(CalendarError, match="version_conflict"):
        rule(s, tmp_path, "disable", "--id", made["rule_id"], "--expected-version", "2")
    with pytest.raises(CalendarError, match="rule_not_found"):
        rule(s, tmp_path, "disable", "--id", "rule-missing")
    off = rule(s, tmp_path, "disable", "--id", made["rule_id"], "--expected-version", "1")
    assert off == {"rule_id": made["rule_id"], "version": 2, "enabled": False}
    listed = rule(s, tmp_path, "list")
    assert [(r["rule_id"], r["enabled"], r["version"]) for r in listed["guardian_rules"]] == [
        (made["rule_id"], False, 2)
    ]
    assert title not in str(listed)
    assert s.query("SELECT principal_id,action,ref,outcome FROM audit ORDER BY id") == [
        {"principal_id": None, "action": "guardian_rule_set", "ref": made["rule_id"], "outcome": "saved"},
        {"principal_id": None, "action": "guardian_rule_disable", "ref": made["rule_id"], "outcome": "disabled"},
    ]


def guardian_rules(s, key):
    members = {m["person_id"]: m for m in FamilyService(s).family(key)["members"]}
    return members, {r["duty"] + ":" + r["activity"]: r for r in members["child"].get("guardian_rules", [])}


# spec: ANYM-432
def test_members_show_guardian_rules_only_to_adult_readers(ruled, tmp_path):
    s = ruled
    drop = rule(s, tmp_path, *dance())["rule_id"]
    pick = rule(s, tmp_path, *dance(duty="pickup", guardian="adult-b", target="cal-b"))["rule_id"]
    swim = dance()
    swim[swim.index("--activity") + 1] = "Swim"
    swim_id = rule(s, tmp_path, *swim)["rule_id"]
    rule(s, tmp_path, "disable", "--id", swim_id)
    s.grant("family", "adult-a", "cal-a", READ + WRITE, "details")
    s.grant("family", "adult-a", "cal-child", READ, "details")
    key = s.agent("reader", "family", "adult-a", READ + WRITE, ["cal-a", "cal-child"])
    members, by = guardian_rules(s, key)
    assert by["dropoff:Dance"] == {
        "rule_id": drop, "activity": "Dance", "duty": "dropoff", "guardian_id": "adult-a",
        "source_calendar_ref": "cal-child", "target_calendar_ref": "cal-a", "lead_minutes": 20,
        "title": "Drive to dance", "visibility": "private", "copy_location": False, "enabled": True,
        "version": 1, "projection_ready": True, "not_ready_reason": None,
    }
    assert (by["pickup:Dance"]["rule_id"], by["pickup:Dance"]["target_calendar_ref"]) == (pick, None)
    assert by["pickup:Dance"]["not_ready_reason"] == "target_not_visible"
    assert by["dropoff:Swim"]["not_ready_reason"] == "disabled"
    assert all("guardian_rules" not in m for p, m in members.items() if p != "child")

    read_only = s.agent("read-only", "family", "adult-a", READ, ["cal-a", "cal-child"])
    assert guardian_rules(s, read_only)[1]["dropoff:Dance"]["not_ready_reason"] == "target_not_writable"
    s.write_opt_in("cal-a", False)
    assert guardian_rules(s, key)[1]["dropoff:Dance"]["not_ready_reason"] == "target_not_writable"
    s.write_opt_in("cal-a", True)
    no_source = s.agent("no-source", "family", "adult-a", READ + WRITE, ["cal-a"])
    view = guardian_rules(s, no_source)[1]["dropoff:Dance"]
    assert (view["source_calendar_ref"], view["not_ready_reason"]) == (None, "source_not_visible")
    busy = s.agent("busy-source", "family", "adult-a", READ + WRITE, ["cal-a"])
    s.query("INSERT INTO agent_calendars VALUES ('busy-source','cal-child','busy_only')")
    view = guardian_rules(s, busy)[1]["dropoff:Dance"]
    assert (view["source_calendar_ref"], view["not_ready_reason"]) == ("cal-child", "source_not_visible")

    s.grant("family", "child", "cal-child", READ, "details")
    s.grant("family", "helper", "cal-child", READ, "details")
    s.grant("family", "adult-b", "cal-b", READ, "details")
    s.person("stranger", "Stranger")
    s.group("other", "Other")
    s.member("other", "stranger", "group_admin")
    for principal, group, person, scopes in [
        ("dependent", "family", "child", READ),
        ("helper", "family", "helper", READ),
        ("no-assignment", "family", "adult-b", [x for x in READ if x != "assignment:read"]),
        ("stranger", "other", "stranger", READ + WRITE),
    ]:
        other = s.agent(principal, group, person, scopes, [])
        result = FamilyService(s).family(other)
        assert all("guardian_rules" not in m for m in result["members"]), principal
        assert drop not in str(result) and "Drive to dance" not in str(result)


# spec: ANYM-432
def test_projection_tables_role_privileges(schema_one, roles):
    apply_roles, connect = roles
    s = family(schema_one)
    apply_roles()  # db-roles on schema 1 still works: the migration-2 grants are skipped.
    s.migrate()
    apply_roles()
    rule(s, Path("."), *dance())
    rule_id = s.one("SELECT id FROM guardian_rules")["id"]
    gateway, worker = connect("gateway"), connect("worker")
    for store in (gateway, worker):
        assert len(store.query("SELECT * FROM guardian_rules")) == 1
        assert store.query("SELECT * FROM projection_links") == []
    insert_rule = (
        "INSERT INTO guardian_rules(id,group_id,dependent_id,activity,duty,guardian_id,source_calendar_ref,"
        "target_calendar_ref,lead_minutes,title,visibility) VALUES ('rule-x','family','child','Swim','dropoff',"
        "'adult-a','cal-child','cal-a',10,'t','private')"
    )
    link = (
        "INSERT INTO projection_links(id,rule_id,rule_version,duty,source_calendar_ref,source_event_ref,"
        "source_start,source_end,target_calendar_ref,target_id,state) VALUES (%s,%s,1,'dropoff','cal-child',"
        "'occ-1','2026-10-20T08:00:00Z','2026-10-20T09:00:00Z','cal-a',%s,'pending')"
    )
    for store in (gateway, worker):
        for statement in (insert_rule, "UPDATE guardian_rules SET title='x'", "DELETE FROM guardian_rules"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                store.query(statement)
    for statement, args in ((link, ("link-g", rule_id, "dkg")), ("UPDATE projection_links SET etag='x'", ())):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            gateway.query(statement, args)
    worker.query(link, ("link-1", rule_id, "dk1"))
    worker.query("UPDATE projection_links SET state='active',etag='\"v1\"' WHERE id='link-1'")
    assert worker.one("SELECT state FROM projection_links")["state"] == "active"
    # One claim per (occurrence, rule) while pending, active or repairing.
    with pytest.raises(psycopg.errors.UniqueViolation):
        worker.query(link, ("link-2", rule_id, "dk2"))
