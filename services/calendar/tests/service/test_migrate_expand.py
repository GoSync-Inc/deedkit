"""ANYM-586: autodeploy's calendar entry point. Running code survives an added column (K0), a positive classification
of a new migration and roles.sql text (K1) and the bounded `migrate-expand` (K2). Synthetic files and per-test
schemas only; real PostgreSQL through FCS_TEST_DSN."""
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from family_calendar.errors import CalendarError
from family_calendar.service import migration_rollout as rollout
from family_calendar.service import store as store_module
from family_calendar.service.store import ROLES, Store

# These tests replay ranges from schema 5 with a synthetic version 6: the repository's own files and roles.sql text
# through version 5, and a store migrated through 5 (the real migration 6 is ANYM-554's).
MIGRATIONS = tuple(m for m in store_module.MIGRATIONS if m[0] <= 5)


@pytest.fixture
def store():
    dsn = os.environ.get("FCS_TEST_DSN")
    if not dsn:
        pytest.fail("FCS_TEST_DSN is required; never fall back to a runtime database")
    s = Store(dsn, schema="test_" + uuid4().hex)
    s.migrate(through=5)
    yield s
    try:
        s.drop_test_schema()
    finally:
        s.close()


# spec: ANYM-586
def test_a_warmed_pool_keeps_reading_across_an_added_column_and_a_rolled_back_one(store):
    table = sql.Identifier(store.schema, "synthetic_k0_probe")
    with store._connect() as admin:
        admin.execute(sql.SQL("CREATE TABLE {} (id text PRIMARY KEY)").format(table))
        admin.execute(sql.SQL("INSERT INTO {} VALUES ('a')").format(table))
    select = sql.SQL("SELECT * FROM {} WHERE id=%s").format(table)
    with store.pool.connection() as c:
        for _ in range(8):
            assert c.execute(select, ("a",)).fetchone() == {"id": "a"}
        c.commit()
        # A migration that adds a column and then fails rolls back; the pool reads the old shape again.
        with store._connect() as admin:
            admin.execute(sql.SQL("ALTER TABLE {} ADD COLUMN lost text").format(table))
            admin.rollback()
        for _ in range(8):
            assert c.execute(select, ("a",)).fetchone() == {"id": "a"}
        c.commit()
        with store._connect() as admin:
            admin.execute(sql.SQL("ALTER TABLE {} ADD COLUMN kept text").format(table))
        assert c.execute(select, ("a",)).fetchone() == {"id": "a", "kept": None}


HEADER = "-- rollout: expand\n"
NEW_TABLE = HEADER + "CREATE TABLE synthetic_notes(id text PRIMARY KEY, body text NOT NULL DEFAULT '');\n"
GUARD = ("DO $$ BEGIN\n IF to_regclass('fcs.synthetic_notes') IS NOT NULL THEN\n"
         "  GRANT SELECT ON fcs.synthetic_notes TO fcs_gateway, fcs_worker;\n"
         "  GRANT INSERT ON fcs.synthetic_notes TO fcs_worker;\n END IF;\nEND $$;\n")


def files(through=5):
    return {v: Path(p).read_text() for v, p in MIGRATIONS if v <= through}


def roles_text():
    text = ROLES.read_text()
    return text[:text.index("-- Migration 6")] if "-- Migration 6" in text else text


# spec: ANYM-586
def test_v4_two_unique_indexes_on_the_existing_plans_table_are_not_additive():
    v4 = files()[4]
    assert rollout.classify(v4) == "manual"
    assert rollout.unattended_class(HEADER + v4) == "manual"
    assert rollout.plan(files(3), {**files(3), 4: HEADER + v4}, roles_text(), roles_text()) == {"pause": "not_unattended"}


# spec: ANYM-586
def test_v5_and_its_roles_guard_replay_as_additive():
    text = roles_text()
    base = text[:text.index("-- Migration 5")]
    assert rollout.unattended_class(HEADER + files()[5]) == "expand"
    created = rollout.new_tables(files()[5])
    assert rollout.role_class(base, text, created) == "additive"
    assert rollout.role_class(base, text, {}) == "manual"  # the guard names a table this range does not create


# spec: ANYM-586
def test_a_synthetic_new_table_with_its_grants_is_additive():
    base, candidate = files(), {**files(), 6: NEW_TABLE}
    result = rollout.plan(base, candidate, roles_text(), roles_text() + GUARD)
    assert result["base"] == 5 and result["target"] == 6
    assert result["roles"]["bytes"] == len(roles_text().encode())
    assert rollout.plan(base, candidate, roles_text(), roles_text() + "-- only a comment\n")["target"] == 6


# spec: ANYM-586
@pytest.mark.parametrize("appended", [
    "REVOKE SELECT ON fcs.plans FROM fcs_gateway;\n",
    "CREATE POLICY p ON fcs.synthetic_notes TO fcs_gateway USING (true);\n",
    "GRANT SELECT ON fcs.synthetic_notes TO public;\n",
    "GRANT ALL ON fcs.synthetic_notes TO fcs_gateway;\n",
    "GRANT SELECT ON fcs.synthetic_notes TO fcs_gateway WITH GRANT OPTION;\n",
    "GRANT SELECT ON fcs.synthetic_notes TO someone_else;\n",
    "GRANT SELECT ON public.synthetic_notes TO fcs_gateway;\n",
    GUARD.replace("GRANT INSERT", "REVOKE INSERT").replace(" TO fcs_worker;\n END", " FROM fcs_worker;\n END"),
    GUARD.replace("fcs.synthetic_notes')", "fcs.plans')"),
], ids=["revoke", "policy", "public", "all", "grant-option", "unknown-role", "other-schema", "revoke-in-guard",
        "guard-on-existing"])
def test_role_text_outside_the_grant_language_pauses(appended):
    assert rollout.plan(files(), {**files(), 6: NEW_TABLE}, roles_text(), roles_text() + appended) == {
        "pause": "role_not_additive"}


# spec: ANYM-586
def test_an_edited_applied_file_a_gap_a_drop_or_no_header_pauses():
    edited = {**files(), 5: files()[5] + "\n-- edited\n", 6: NEW_TABLE}
    assert rollout.plan(files(), edited, roles_text(), roles_text()) == {"pause": "edited"}
    assert rollout.plan(files(), {**files(), 7: NEW_TABLE}, roles_text(), roles_text()) == {"pause": "gap"}
    drop = HEADER + "ALTER TABLE plans DROP COLUMN tool;\n"
    assert rollout.plan(files(), {**files(), 6: drop}, roles_text(), roles_text()) == {"pause": "not_unattended"}
    bare = NEW_TABLE[len(HEADER):]
    assert rollout.plan(files(), {**files(), 6: bare}, roles_text(), roles_text()) == {"pause": "no header"}
    manual = "-- rollout: manual\n" + bare
    assert rollout.plan(files(), {**files(), 6: manual}, roles_text(), roles_text()) == {"pause": "not_unattended"}


# K2: the bounded entry point on real PostgreSQL. Synthetic roles per test schema, dropped afterwards.

@pytest.fixture
def expand(store, tmp_path):
    names = {"fcs_gateway": store.schema + "_gw", "fcs_worker": store.schema + "_wk"}
    with psycopg.connect(store.dsn, autocommit=True) as c:
        for role in names.values():
            c.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(sql.Identifier(role)))

    def local(text):
        for old, new in names.items():
            text = text.replace(old, new)
        return text.replace("fcs.", store.schema + ".")

    base_roles = local("GRANT USAGE ON SCHEMA fcs TO fcs_gateway, fcs_worker;\n")

    def setup(migration=NEW_TABLE, appended=GUARD, base_roles=base_roles):
        (tmp_path / "schema_v6.sql").write_text(migration)
        (tmp_path / "roles.sql").write_text(base_roles + local(appended))
        prefix = base_roles.encode()
        plan = {"base": 5, "target": 6, "roles": {"bytes": len(prefix), "sha256": hashlib.sha256(prefix).hexdigest()}}
        return plan, {"migrations": MIGRATIONS + ((6, tmp_path / "schema_v6.sql"),), "roles": tmp_path / "roles.sql"}

    yield setup, names
    with psycopg.connect(store.dsn, autocommit=True) as c:
        for role in names.values():
            c.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            c.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def ledger(store):
    return [r["version"] for r in store.query("SELECT version FROM migrations ORDER BY version")]


def tables(store):
    return {r["relname"] for r in store.query(
        "SELECT relname FROM pg_class WHERE relnamespace=%s::regnamespace AND relkind='r'", (store.schema,))}


# spec: ANYM-586
def test_migrate_expand_applies_the_file_its_ledger_row_and_the_grants_in_one_transaction(store, expand):
    setup, names = expand
    plan, paths = setup()
    assert store.migrate_expand(plan, **paths) == {"schema_version": 6}
    assert ledger(store) == [1, 2, 3, 4, 5, 6]
    privileges = store.one("SELECT has_table_privilege(%s, %s, 'SELECT') AS gw, has_table_privilege(%s, %s, 'INSERT') AS wk, "
                           "has_table_privilege(%s, %s, 'INSERT') AS gw_insert",
                           (names["fcs_gateway"], store.schema + ".synthetic_notes", names["fcs_worker"],
                            store.schema + ".synthetic_notes", names["fcs_gateway"], store.schema + ".synthetic_notes"))
    assert privileges == {"gw": True, "wk": True, "gw_insert": False}
    assert store.expand_status() == {"schema_version": 6}
    with pytest.raises(CalendarError) as again:
        store.migrate_expand(plan, **paths)
    assert again.value.code == "ledger_mismatch"


# spec: ANYM-586
def test_the_session_budgets_are_read_back(store, expand, monkeypatch):
    from family_calendar.service import store as module
    setup, _ = expand
    plan, paths = setup()
    monkeypatch.setitem(module.EXPAND_SETTINGS, "transaction_timeout", "5s")
    with pytest.raises(CalendarError) as refused:
        store.migrate_expand(plan, **paths)
    assert refused.value.code == "settings_refused"
    assert ledger(store) == [1, 2, 3, 4, 5]


# spec: ANYM-586
def test_the_session_takes_pgappname_over_the_dsns_application_name(store, expand, monkeypatch):
    setup, _ = expand
    plan, paths = setup()
    monkeypatch.setattr(store, "dsn", psycopg.conninfo.make_conninfo(store.dsn, application_name="synthetic-other"))
    monkeypatch.setenv("PGAPPNAME", "deedkit-calendar-migrate-synthetic")
    seen = []
    assert store.migrate_expand(plan, **paths, before_commit=lambda conn: seen.append(
        conn.execute("SHOW application_name").fetchone()["application_name"])) == {"schema_version": 6}
    assert seen == ["deedkit-calendar-migrate-synthetic"]


# spec: ANYM-586
def test_a_held_migration_lock_returns_busy_at_once_and_changes_nothing(store, expand):
    setup, _ = expand
    plan, paths = setup()
    with psycopg.connect(store.dsn, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(61729128)")
        started = time.monotonic()
        with pytest.raises(CalendarError) as busy:
            store.migrate_expand(plan, **paths)
        assert busy.value.code == "busy" and time.monotonic() - started < 1.5
    assert ledger(store) == [1, 2, 3, 4, 5] and "synthetic_notes" not in tables(store)


# spec: ANYM-586
def test_a_held_table_lock_fails_lock_timeout_while_a_running_writer_finishes(store, expand):
    setup, _ = expand
    table = sql.Identifier(store.schema, "synthetic_live")
    with store._connect() as admin:
        admin.execute(sql.SQL("CREATE TABLE {} (id int)").format(table))
    plan, paths = setup(migration=HEADER + "ALTER TABLE synthetic_live ADD COLUMN note text;\n", appended="")
    outcome, writer = {}, {}
    with psycopg.connect(store.dsn) as reader:
        reader.execute(sql.SQL("SELECT * FROM {}").format(table))  # ACCESS SHARE until this transaction ends

        def migrate():
            try:
                store.migrate_expand(plan, **paths)
            except CalendarError as exc:
                outcome["code"] = exc.code

        thread = threading.Thread(target=migrate)
        thread.start()
        with psycopg.connect(store.dsn, autocommit=True) as watch:
            while not watch.execute("SELECT 1 FROM pg_locks WHERE NOT granted AND relation=%s::regclass",
                                    (store.schema + ".synthetic_live",)).fetchone():
                time.sleep(0.05)

        def write():
            started = time.monotonic()
            with psycopg.connect(store.dsn, autocommit=True, options="-c statement_timeout=5000") as w:
                w.execute(sql.SQL("INSERT INTO {} VALUES (1)").format(table))
            writer["elapsed"] = time.monotonic() - started

        writing = threading.Thread(target=write)
        writing.start()
        thread.join(10)
        writing.join(10)
    assert outcome == {"code": "lock_timeout"}
    assert writer["elapsed"] < 5
    assert ledger(store) == [1, 2, 3, 4, 5]
    assert "note" not in {r["attname"] for r in store.query(
        "SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0", (store.schema + ".synthetic_live",))}


# spec: ANYM-586
def test_a_slow_transaction_ends_at_the_budget_with_nothing_applied(store, expand):
    setup, _ = expand
    plan, paths = setup()
    started = time.monotonic()
    with pytest.raises(CalendarError) as slow:
        store.migrate_expand(plan, **paths, before_commit=lambda c: c.execute("SELECT pg_sleep(5)"))
    assert slow.value.code == "transaction_timeout" and time.monotonic() - started < 6
    assert ledger(store) == [1, 2, 3, 4, 5] and "synthetic_notes" not in tables(store)


# spec: ANYM-586
def test_a_failing_grant_leaves_nothing(store, expand):
    setup, names = expand
    # A role the base text names but the database does not have: the GRANT fails after the table was created.
    base = "GRANT USAGE ON SCHEMA fcs TO fcs_gateway, fcs_worker, missing_role;\n".replace("fcs_gateway", names["fcs_gateway"]).replace(
        "fcs_worker", names["fcs_worker"]).replace("missing_role", store.schema + "_missing").replace("fcs ", store.schema + " ")
    plan, paths = setup(appended="GRANT SELECT ON fcs.synthetic_notes TO " + store.schema + "_missing;\n", base_roles=base)
    with pytest.raises(CalendarError) as failed:
        store.migrate_expand(plan, **paths)
    assert failed.value.code == "failed"
    assert ledger(store) == [1, 2, 3, 4, 5] and "synthetic_notes" not in tables(store)


# spec: ANYM-586
@pytest.mark.parametrize("migration,appended,code", [
    (HEADER + "ALTER TABLE plans DROP COLUMN tool;\n", "", "not_unattended"),
    (HEADER + "CREATE INDEX synthetic_idx ON plans(id);\n", "", "not_unattended"),
    (NEW_TABLE, "REVOKE SELECT ON fcs.plans FROM fcs_gateway;\n", "role_not_additive"),
    (NEW_TABLE + "COMMENT ON SCHEMA public IS 'changed';\n", "", "not_unattended"),
    (HEADER + "CREATE TABLE synthetic_notes(id text REFERENCES outsider(id));\n", "", "not_unattended"),
    (NEW_TABLE, "GRANT UPDATE ON fcs.audit_id_seq TO fcs_gateway;\n", "role_not_additive"),
    (HEADER + "COMMENT ON COLUMN plans.fcs.tool IS 'changed';\n", "", "not_unattended"),
], ids=["drop", "index-on-existing", "revoke", "comment-outside", "reference-outside", "grant-on-sequence",
        "comment-displaced-qualifier"])
def test_migrate_expand_reclassifies_before_running(store, expand, migration, appended, code):
    setup, _ = expand
    plan, paths = setup(migration=migration, appended=appended)
    with pytest.raises(CalendarError) as refused:
        store.migrate_expand(plan, **paths)
    assert refused.value.code == code
    assert ledger(store) == [1, 2, 3, 4, 5]


# spec: ANYM-586
def test_the_cli_prints_one_json_line(store, tmp_path, capsys):
    from family_calendar.cli import main
    from family_calendar.private_files import private_dir, write_private
    root = private_dir(tmp_path / "home" / "service")
    write_private(root / "runtime.json", json.dumps({"dsn": os.environ["FCS_TEST_DSN"], "port": 1}).encode())
    # The runtime profile reaches the `fcs` schema, which this disposable database does not hold: an empty ledger.
    assert main(["--home", str(tmp_path / "home"), "service", "migrate-expand", "--status"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and json.loads(out) == {"schema_version": 0}
    (tmp_path / "plan.json").write_text("not json")
    assert main(["--home", str(tmp_path / "home"), "service", "migrate-expand", "--plan", str(tmp_path / "plan.json")]) == 1
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and json.loads(out) == {"error": "plan_invalid"}
    assert main(["--home", str(tmp_path / "home"), "service", "migrate-expand"]) == 2
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and json.loads(out) == {"error": "usage"}


# spec: ANYM-586
@pytest.mark.parametrize("migration,appended,reason", [
    (NEW_TABLE + "COMMENT ON SCHEMA public IS 'changed';\n", "", "not_unattended"),
    (NEW_TABLE + "COMMENT ON ROLE fcs_gateway IS 'changed';\n", "", "not_unattended"),
    (HEADER + "CREATE TABLE synthetic_notes(id text REFERENCES outsider(id));\n", "", "not_unattended"),
    (HEADER + "CREATE TABLE IF NOT EXISTS plans(id text);\nCREATE UNIQUE INDEX probe_idx ON plans(id);\n", "",
     "not_unattended"),
    (NEW_TABLE, "GRANT UPDATE ON fcs.audit_id_seq TO fcs_gateway;\n", "role_not_additive"),
    (HEADER + 'CREATE TABLE fcs."MixedCase"(id int);\n',
     "DO $$ BEGIN IF to_regclass('fcs.MixedCase') IS NOT NULL THEN GRANT SELECT ON fcs.\"MixedCase\" TO fcs_gateway;"
     " END IF; END $$;\n", "role_not_additive"),
    (NEW_TABLE, GUARD.replace(" END IF;\nEND $$", ' "end" "if";\n"end" $$'), "role_not_additive"),
    (HEADER + "COMMENT ON COLUMN plans.fcs.tool IS 'changed';\n", "", "not_unattended"),
    (HEADER + "COMMENT ON COLUMN fcs.plans.fcs.tool IS 'changed';\n", "", "not_unattended"),
    (HEADER + "COMMENT ON TABLE fcs.fcs.plans IS 'changed';\n", "", "not_unattended"),
], ids=["comment-on-schema", "comment-on-role", "reference-outside", "existing-name", "grant-on-sequence",
        "mixed-case-guard", "quoted-guard-close", "comment-displaced-qualifier", "comment-two-qualifiers",
        "comment-doubled-schema"])
def test_plan_pauses_on_text_outside_the_schema_or_the_template(migration, appended, reason):
    assert rollout.plan(files(), {**files(), 6: migration}, roles_text(), roles_text() + appended) == {"pause": reason}


# spec: ANYM-586
def test_plan_accepts_a_comment_and_a_reference_on_the_schema_s_tables():
    migration = NEW_TABLE + ("CREATE TABLE synthetic_links(id text PRIMARY KEY, note text REFERENCES synthetic_notes(id),"
                             " plan_id text REFERENCES plans(id));\nCOMMENT ON TABLE plans IS 'synthetic';\n"
                             "COMMENT ON COLUMN fcs.synthetic_notes.body IS 'synthetic';\n")
    assert rollout.plan(files(), {**files(), 6: migration}, roles_text(), roles_text())["target"] == 6


# spec: ANYM-586
def test_the_base_inventory_plan_reads_from_the_files_is_the_live_schema_5_tables(store):
    inventory = {}
    for version, text in sorted(files().items()):
        inventory = rollout.new_tables(text, inventory)
    assert set(inventory) | {"migrations"} == tables(store)
    grant = "GRANT SELECT ON fcs.migrations, fcs.invitation_opt_ins TO fcs_gateway;\n"
    assert rollout.plan(files(), {**files(), 6: NEW_TABLE}, roles_text(), roles_text() + grant)["target"] == 6


# spec: ANYM-586
def test_a_self_referencing_new_table_is_additive():
    inline = HEADER + "CREATE TABLE synthetic_tree(id int PRIMARY KEY, parent_id int REFERENCES synthetic_tree(id));\n"
    table = HEADER + ("CREATE TABLE synthetic_tree(id int PRIMARY KEY, parent_id int,"
                      " FOREIGN KEY (parent_id) REFERENCES fcs.synthetic_tree(id));\n")
    for migration in (inline, table):
        assert rollout.plan(files(), {**files(), 6: migration}, roles_text(), roles_text())["target"] == 6
