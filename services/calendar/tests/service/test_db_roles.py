"""ANYM-601: explicit calendar grants in roles.sql and the read-only `db-roles --check`."""
import json
import os
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from family_calendar.errors import CalendarError
from family_calendar.private_files import private_dir, write_private
from family_calendar.service import store as store_module
from family_calendar.service.cli import GRANTS, check_roles, run
from family_calendar.service.store import Store
from tests.service.test_guardian_rules import roles, schema_one  # noqa: F401 (fixtures)

SERVICE = Path(store_module.__file__).parent
# Base-commit roles.sql lines (before ANYM-601) that granted whatever existed when db-roles ran.
BASE_ALL = (
    "GRANT USAGE ON SCHEMA fcs TO fcs_gateway, fcs_worker;\n"
    "GRANT SELECT ON ALL TABLES IN SCHEMA fcs TO fcs_gateway, fcs_worker;\n"
    "GRANT USAGE ON ALL SEQUENCES IN SCHEMA fcs TO fcs_gateway, fcs_worker;\n"
)
SCHEMA_ONE_TABLES = (
    "migrations people groups memberships guardians connections calendars calendar_grants bindings principals "
    "agent_calendars resources occurrences coverage sync_jobs plans operations operation_steps assignments audit"
).split()
READ = SCHEMA_ONE_TABLES + ["guardian_rules", "projection_links", "schedule_selections", "invitation_opt_ins",
                            "series_reads"]
# The privilege matrix of the latest schema, written out: (role, object, privilege); a column privilege as table(col).
MATRIX = (
    {(role, t, "SELECT") for role in ("gw", "wk") for t in READ}
    | {("gw", t, "INSERT") for t in ("plans", "operations", "operation_steps", "sync_jobs", "assignments", "audit",
                                     "schedule_selections")}
    | {("gw", "assignments", "UPDATE")}
    | {("gw", "plans(%s)" % c, "UPDATE") for c in ("token_hash", "tool", "request", "operation_id")}
    | {("wk", t, p) for t in ("resources", "occurrences", "coverage") for p in ("INSERT", "UPDATE", "DELETE")}
    | {("wk", "audit", "INSERT"), ("wk", "projection_links", "INSERT"), ("wk", "projection_links", "UPDATE")}
    | {("wk", t, "UPDATE") for t in ("operations", "operation_steps", "sync_jobs")}
    | {(role, "audit_id_seq", "USAGE") for role in ("gw", "wk")}
    | {("gw", "member_people", "SELECT")}
    | {("gw", "series_reads", "INSERT")} | {("wk", "series_reads(%s)" % c, "UPDATE") for c in ("state", "result", "error")}
)
# Schema 4, before migration 5 adds invitation_opt_ins (6 member_people, 7 series_reads).
MATRIX_4 = {e for e in MATRIX if e[1].partition("(")[0] not in ("invitation_opt_ins", "member_people", "series_reads")}


def names_of(s):
    return {"fcs_gateway": s.schema + "_gw", "fcs_worker": s.schema + "_wk"}


def apply_text(s, text):
    """roles.sql text the way the roles fixture applies it: per-test role names and schema, one transaction."""
    for old, new in names_of(s).items():
        text = text.replace(old, new)
    text = text.replace("fcs.", s.schema + ".").replace("SCHEMA fcs ", "SCHEMA " + s.schema + " ")
    with s.transaction() as c:
        for role in names_of(s).values():
            if not c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                c.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(sql.Identifier(role)))
        c.execute(text)


def matrix(s):
    """Every privilege either role holds on every table, column and sequence of the schema, by has_*_privilege."""
    short = dict(zip(names_of(s).values(), ("gw", "wk")))
    out = set()
    rows = s.query(
        "SELECT c.oid, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind IN ('r','S','v','m','p')", (s.schema,)
    )
    for role, tag in short.items():
        for rel in rows:
            if rel["relkind"] == "S":
                for p in ("USAGE", "SELECT", "UPDATE"):
                    if s.one("SELECT has_sequence_privilege(%s,%s::oid,%s) AS ok", (role, rel["oid"], p))["ok"]:
                        out.add((tag, rel["relname"], p))
                continue
            for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                if s.one("SELECT has_table_privilege(%s,%s::oid,%s) AS ok", (role, rel["oid"], p))["ok"]:
                    out.add((tag, rel["relname"], p))
                elif p in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                    for col in s.query("SELECT attname FROM pg_attribute WHERE attrelid=%s AND attnum>0 "
                                       "AND NOT attisdropped", (rel["oid"],)):
                        if s.one("SELECT has_column_privilege(%s,%s::oid,%s::text,%s) AS ok",
                                 (role, rel["oid"], col["attname"], p))["ok"]:
                            out.add((tag, "%s(%s)" % (rel["relname"], col["attname"]), p))
    return out


def check(s):
    with s.transaction() as c:
        return check_roles(c, s.schema, names_of(s))


# spec: ANYM-601
def test_roles_sql_grants_exactly_the_written_matrix(schema_one, roles):  # noqa: F811 (fixtures)
    apply_roles, _ = roles
    s = schema_one
    for version in (1, 2, 3, 4, 5, 6, 7):  # each version's grants equal roles_expected.json at that version
        s.migrate(through=version)
        apply_roles()
        assert check(s) == (version, [])
    assert matrix(s) == MATRIX
    assert max(int(k) for k in json.loads((SERVICE / "roles_expected.json").read_text()) if k.isdigit()) == \
        store_module.MIGRATIONS[-1][0]
    # A table a migration adds gets nothing until roles.sql names it.
    s.query(sql.SQL("CREATE TABLE {}.synthetic(id int)").format(sql.Identifier(s.schema)))
    apply_roles()
    assert matrix(s) == MATRIX
    assert not any(obj == "synthetic" or obj == "principal_operations" for _, obj, _ in matrix(s))


# spec: ANYM-601
def test_roles_sql_revokes_the_old_all_tables_grant(schema_one, roles):  # noqa: F811 (fixtures)
    apply_roles, _ = roles
    s = schema_one
    s.migrate(through=4)
    apply_text(s, BASE_ALL + (SERVICE / "roles.sql").read_text().split("\n", 1)[0])
    gw, wk = names_of(s).values()
    for role in (gw, wk):
        assert s.one("SELECT has_table_privilege(%s,%s,'SELECT') AS ok",
                     (role, s.schema + ".principal_operations"))["ok"]
    seeded = matrix(s)
    # (c) one transaction: a failing statement after the file leaves the seeded privileges as they were.
    with pytest.raises(psycopg.errors.DivisionByZero):
        apply_text(s, (SERVICE / "roles.sql").read_text() + "\nSELECT 1/0;\n")
    assert matrix(s) == seeded
    # (b) the new file removes every privilege on principal_operations; a rerun changes nothing.
    apply_roles()
    assert not any(obj == "principal_operations" for _, obj, _ in matrix(s))
    assert matrix(s) == MATRIX_4
    apply_roles()
    assert matrix(s) == MATRIX_4 and check(s) == (4, [])


# spec: ANYM-601
def test_service_roles_still_read_and_write_with_the_explicit_list(schema_one, roles):  # noqa: F811 (fixtures)
    apply_roles, connect = roles
    s = schema_one
    s.migrate()
    s.person("adult-a", "Adult A")
    s.connection("google-a", "adult-a", "google")
    s.calendar("cal-a", "google-a", "private-a", "Private", "Asia/Makassar", "write")
    apply_roles()
    gateway, worker = connect("gateway"), connect("worker")
    assert gateway.query("SELECT ref FROM calendars") == [{"ref": "cal-a"}]
    gateway.query("INSERT INTO audit(action,outcome) VALUES ('test','ok')")
    worker.query("INSERT INTO coverage(calendar_ref,start_at,end_at,status) VALUES "
                 "('cal-a','2026-10-20T00:00:00Z','2026-10-21T00:00:00Z','complete')")
    worker.query("UPDATE coverage SET status='partial'")
    assert s.one("SELECT count(*) AS n FROM audit")["n"] == 1
    for store in (gateway, worker):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            store.query("SELECT 1 FROM principal_operations")


READER = "fcs_anym601_reader"


@pytest.fixture
def fcs_home(tmp_path):
    """A service home on the disposable database's real `fcs` schema and roles; refuses to touch existing ones."""
    dsn = os.environ.get("FCS_TEST_DSN")
    if not dsn:
        pytest.fail("FCS_TEST_DSN is required; never fall back to a runtime database")
    with psycopg.connect(dsn, autocommit=True) as c:
        taken = c.execute("SELECT (SELECT count(*) FROM pg_namespace WHERE nspname='fcs') + (SELECT count(*) FROM "
                          "pg_roles WHERE rolname IN ('fcs_gateway','fcs_worker',%s))", (READER,)).fetchone()[0]
    if taken:
        pytest.fail("the test database already holds schema fcs or a calendar service role")
    home = tmp_path / "home"
    write_private(private_dir(home / "service") / "runtime.json", json.dumps({"dsn": dsn, "port": 1}).encode())
    try:
        yield home, dsn
    finally:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("DROP SCHEMA IF EXISTS fcs CASCADE")
            for role in ("fcs_gateway", "fcs_worker", READER):
                if c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                    c.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                    c.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def snapshot(home, dsn):
    files = sorted((str(p.relative_to(home)), p.stat().st_size, p.stat().st_mtime_ns) for p in home.rglob("*"))
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        c.execute("SET search_path TO fcs, public")
        acl = c.execute(GRANTS, {"schema": "fcs", "roles": ["fcs_gateway", "fcs_worker"]}).fetchall()
    return files, sorted(acl, key=lambda r: tuple(str(v) for v in r.values()))


DRIFT = [
    ("REVOKE SELECT ON fcs.people FROM fcs_gateway", ["missing fcs_gateway SELECT fcs.people"]),
    ("GRANT SELECT ON fcs.principal_operations TO fcs_gateway",
     ["extra fcs_gateway SELECT fcs.principal_operations"]),
    ("GRANT UPDATE (payload) ON fcs.plans TO fcs_gateway", ["extra fcs_gateway UPDATE fcs.plans(payload)"]),
    ("CREATE SEQUENCE fcs.synthetic_seq; GRANT USAGE ON SEQUENCE fcs.synthetic_seq TO fcs_worker",
     ["extra fcs_worker USAGE fcs.synthetic_seq"]),
    ("REVOKE USAGE ON SCHEMA fcs FROM fcs_worker", ["missing fcs_worker USAGE fcs"]),
    ("GRANT SELECT ON fcs.people TO PUBLIC", ["extra PUBLIC SELECT fcs.people"]),
    ("GRANT SELECT ON fcs.people TO fcs_gateway WITH GRANT OPTION",
     ["extra fcs_gateway SELECT fcs.people WITH GRANT OPTION"]),
    ("CREATE TABLE fcs.synthetic(id int); GRANT SELECT ON fcs.synthetic TO fcs_worker",
     ["extra fcs_worker SELECT fcs.synthetic"]),
    (f"CREATE ROLE {READER} NOLOGIN; GRANT SELECT ON fcs.principal_operations TO {READER}; "
     f"GRANT {READER} TO fcs_gateway", [f"extra fcs_gateway MEMBER OF {READER} (set)"]),
    ("DROP OWNED BY fcs_worker; DROP ROLE fcs_worker", ["missing role fcs_worker"]),
]


# spec: ANYM-601
def test_db_roles_check_is_clean_after_db_roles_and_writes_nothing(fcs_home):
    home, dsn = fcs_home
    run(["migrate"], home)
    run(["db-roles"], home)
    before = snapshot(home, dsn)
    assert run(["db-roles", "--check"], home) == {"db_roles_check": "ok", "schema_version": 7}
    assert snapshot(home, dsn) == before


# spec: ANYM-601
@pytest.mark.parametrize("change,lines", DRIFT, ids=[str(i) for i in range(len(DRIFT))])
def test_db_roles_check_names_each_drift_and_writes_nothing(fcs_home, change, lines):
    home, dsn = fcs_home
    run(["migrate"], home)
    run(["db-roles"], home)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(change)
    before = snapshot(home, dsn)
    with pytest.raises(CalendarError) as caught:
        run(["db-roles", "--check"], home)
    assert caught.value.code == "db_roles_drift"
    assert caught.value.details == {"schema_version": 7, "drift": lines}
    assert snapshot(home, dsn) == before


# spec: ANYM-601
@pytest.mark.parametrize("missing", ["home", "service"])
def test_db_roles_check_creates_no_missing_home_or_service_directory(tmp_path, missing):
    home = tmp_path / "home"
    if missing == "service":
        home.mkdir()
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    with pytest.raises(CalendarError) as caught:
        run(["db-roles", "--check"], home)
    assert caught.value.code == "existing_runtime_configuration_required"
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


# spec: ANYM-601
def test_db_roles_check_reads_an_n_minus_one_schema_against_its_own_version(fcs_home):
    home, dsn = fcs_home
    store = Store(dsn)
    try:
        store.migrate(through=3)
    finally:
        store.close()
    run(["db-roles"], home)
    before = snapshot(home, dsn)
    assert run(["db-roles", "--check"], home) == {"db_roles_check": "ok", "schema_version": 3}
    assert snapshot(home, dsn) == before
