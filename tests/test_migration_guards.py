"""Schema migration guards (ANYM-130): a newer database is refused, concurrent runs are serialized by a
transaction-scoped advisory lock, and the run's statement timeout is 180 s for its own transaction only."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import time

import psycopg
import pytest

from messaging_service import store as store_module
from messaging_service.store import RUNTIME_SCHEMA

MIGRATE_LOCK = 74509137


def dsn():
    return Path(os.environ['PROBE_TEST_DSN_FILE']).read_text().strip()


def schema_state(store):
    with store.pool.connection() as c:
        version = c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']
        ledger = c.execute('SELECT version,sha256 FROM archive_migrations ORDER BY version').fetchall()
    return version, ledger


# spec: ANYM-130
def test_a_database_newer_than_the_runtime_is_refused_and_nothing_changes(database):
    _, ledger = schema_state(database)
    with database.pool.connection() as c:
        c.execute("UPDATE probe_metadata SET value=%s WHERE name='schema_version'", (str(RUNTIME_SCHEMA + 1),))
    try:
        with pytest.raises(RuntimeError, match='^database_newer_than_runtime$'):
            database.migrate()
        assert schema_state(database) == (str(RUNTIME_SCHEMA + 1), ledger)
    finally:
        with database.pool.connection() as c:
            c.execute("UPDATE probe_metadata SET value=%s WHERE name='schema_version'", (str(RUNTIME_SCHEMA),))
    database.migrate()  # the restored database at the runtime's own version migrates again as a no-op
    assert schema_state(database) == (str(RUNTIME_SCHEMA), ledger)


def waiting_on_the_migrate_lock(store):
    with store.pool.connection() as c:
        return c.execute('''SELECT count(*) AS n FROM pg_locks WHERE locktype='advisory' AND objid=%s AND NOT granted''',
                         (MIGRATE_LOCK,)).fetchone()['n']


# spec: ANYM-130
def test_a_second_migrate_waits_for_the_first_ones_advisory_lock(database):
    with psycopg.connect(dsn()) as holder:
        holder.execute('SELECT pg_advisory_xact_lock(%s)', (MIGRATE_LOCK,))  # held until this transaction ends
        with ThreadPoolExecutor(1) as threads:
            running = threads.submit(database.migrate)
            deadline = time.monotonic() + 10
            while not waiting_on_the_migrate_lock(database) and time.monotonic() < deadline and not running.done():
                time.sleep(0.02)
            assert waiting_on_the_migrate_lock(database) == 1 and not running.done()
            holder.rollback()
            running.result(timeout=60)
    with psycopg.connect(dsn()) as other:  # transaction-scoped: nothing stays held after migrate returns
        assert other.execute('SELECT pg_try_advisory_lock(%s)', (MIGRATE_LOCK,)).fetchone()[0] is True
        other.execute('SELECT pg_advisory_unlock(%s)', (MIGRATE_LOCK,))


# spec: ANYM-130
def test_migrate_runs_under_a_180_second_statement_timeout_that_ends_with_its_transaction(database, monkeypatch):
    seen = []
    replay = store_module.lock_replay_tables

    def observe(c, *args, **kwargs):  # called inside migrate's transaction, after its own settings
        seen.append(c.execute('SHOW statement_timeout').fetchone()['statement_timeout'])
        return replay(c, *args, **kwargs)
    monkeypatch.setattr(store_module, 'lock_replay_tables', observe)
    database.migrate()
    assert seen == ['3min']
    with database.pool.connection() as c:
        assert c.execute('SHOW statement_timeout').fetchone()['statement_timeout'] != '3min'
