"""In-place extension of a live messaging key (ANYM-422): same token, new expiry."""
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import threading
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from messaging_service import __main__ as operator
from messaging_service import store as store_module
from messaging_service.policy import AUDIT_OPERATIONS, lock_principal
from messaging_service.store import APIError, Store, digest
from test_event_delivery import call, emit, identity, subscription
from messaging_service.subscriptions import match_subscriptions


def credential(store, token):
    return store.capabilities(token)['credential_id']


def key_row(store, key):
    with store.pool.connection() as c:
        return c.execute('SELECT * FROM api_keys WHERE id=%s', (key,)).fetchone()


def expire_in(store, key, interval):
    with store.pool.connection() as c:
        c.execute('UPDATE api_keys SET expires_at=clock_timestamp()+%s::interval WHERE id=%s', (interval, key))


def extend_audit(store):
    with store.pool.connection() as c:
        return c.execute("SELECT * FROM audit_log WHERE operation='extend_key' ORDER BY id").fetchall()


# spec: ANYM-422
def test_extend_key_moves_expiry_from_database_clock_and_keeps_everything_else(seeded):
    store, other_token, _ = seeded
    token = store.issue_key('agent-a', ['telegram-demo', 'whatsapp-demo-1'], days=1)
    key, other = credential(store, token), credential(store, other_token)
    before, other_before = key_row(store, key), key_row(store, other)
    with store.pool.connection() as c:
        principal = c.execute("SELECT * FROM principals WHERE id='agent-a'").fetchone()
    result = store.extend_key(key, 183)
    after = key_row(store, key)
    assert set(result) == {'key_id', 'previous_expires_at', 'expires_at'} and result['key_id'] == key
    assert result['previous_expires_at'] == before['expires_at'].isoformat()
    assert result['expires_at'] == after['expires_at'].isoformat()
    with store.pool.connection() as c:
        left = c.execute('SELECT %s::timestamptz-clock_timestamp() AS left', (after['expires_at'],)).fetchone()['left']
        assert timedelta(days=183) - timedelta(seconds=5) < left <= timedelta(days=183)
        assert c.execute("SELECT * FROM principals WHERE id='agent-a'").fetchone() == principal
    assert {k: v for k, v in after.items() if k != 'expires_at'} == {k: v for k, v in before.items() if k != 'expires_at'}
    assert after['digest'] == digest(token) and key_row(store, other) == other_before
    assert store.capabilities(token)['credential_id'] == key
    listed = {row['id']: row for row in store.list_keys('agent-a')}
    assert listed[key]['expires_at'] == result['expires_at']


# The ceiling is 365 days since ANYM-499 (owner 2026-10-24): the name and every case keep their Tiden IDs,
# and the 184 and 365 cases, now valid, assert the extension instead.
# spec: ANYM-422
@pytest.mark.parametrize('days', [0, -1, 184, 365, True, 1.5, '7', None])
def test_extend_key_refuses_lifetime_outside_one_to_183_days(seeded, days):
    store, token, _ = seeded
    key = credential(store, token)
    before = key_row(store, key)
    if days in (184, 365) and type(days) is int:
        store.extend_key(key, days)
        with store.pool.connection() as c:
            left = c.execute('SELECT %s::timestamptz-clock_timestamp() AS left',
                             (key_row(store, key)['expires_at'],)).fetchone()['left']
        assert timedelta(days=days) - timedelta(seconds=5) < left <= timedelta(days=days)
        assert len(extend_audit(store)) == 1
        return
    with pytest.raises(ValueError, match='^invalid_key_lifetime$'):
        store.extend_key(key, days)
    assert key_row(store, key) == before and extend_audit(store) == []


# spec: ANYM-422
def test_extended_token_authenticates_after_old_instant_and_fails_from_new_instant(seeded):
    store, token, _ = seeded
    key = credential(store, token)
    expire_in(store, key, '2 seconds')
    store.extend_key(key, 1)
    time.sleep(2.5)
    assert store.capabilities(token)['credential_id'] == key
    # Reaching the new instant gives the existing 401, as for any expired key.
    with store.pool.connection() as c:
        c.execute('UPDATE api_keys SET expires_at=clock_timestamp() WHERE id=%s', (key,))
    with pytest.raises(APIError, match='^unauthorized$') as failure:
        store.capabilities(token)
    assert failure.value.status == 401


# spec: ANYM-422
def test_subscription_and_ack_position_survive_old_expiry_after_extension(seeded):
    store, _, _ = seeded
    token = identity(store)
    key = credential(store, token)
    sid = subscription(store, token)
    first = emit(store)
    match_subscriptions(store)
    page = call(store, token, 'fetch_subscription_events', sid)
    assert [e['event_id'] for e in page['items']] == [first]
    assert call(store, token, 'ack_subscription_events', sid, page['receipt'], page['ack_version'])['acked_through'] == first
    expire_in(store, key, '2 seconds')
    store.extend_key(key, 30)
    time.sleep(2.5)
    second = emit(store)
    match_subscriptions(store)
    following = call(store, token, 'fetch_subscription_events', sid)
    assert [e['event_id'] for e in following['items']] == [second]
    ack = call(store, token, 'ack_subscription_events', sid, following['receipt'], following['ack_version'])
    assert ack['acked_through'] == second


# spec: ANYM-422
def test_extend_key_refuses_unknown_revoked_expired_and_shortening_without_change(seeded):
    store, token, revoked_token = seeded
    with pytest.raises(ValueError, match='^unknown_key$'):
        store.extend_key(str(uuid4()), 7)
    revoked = credential(store, revoked_token)
    store.revoke(credential_id=revoked)
    long_lived = credential(store, store.issue_key('agent-c', ['telegram-demo'], days=30))
    expired = credential(store, token)
    with store.pool.connection() as c:
        c.execute("UPDATE api_keys SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (expired,))
    for key, code in ((revoked, 'key_revoked'), (expired, 'key_expired'), (long_lived, 'key_lifetime_would_shorten')):
        before = key_row(store, key)
        with pytest.raises(ValueError, match='^'+code+'$'):
            store.extend_key(key, 7)
        assert key_row(store, key) == before
    assert extend_audit(store) == []


# spec: ANYM-422
def test_extend_key_writes_one_content_free_operator_audit_row_in_the_same_transaction(seeded, monkeypatch):
    store, token, _ = seeded
    key = credential(store, token)
    assert 'extend_key' in AUDIT_OPERATIONS
    store.extend_key(key, 10)
    [row] = extend_audit(store)
    assert (row['actor'], row['principal'], str(row['key_id']), row['outcome']) == ('operator', 'agent-a', key, 'ok')
    assert token not in json.dumps(row, default=str) and digest(token) not in json.dumps(row, default=str)
    before = key_row(store, key)
    def failing_audit(c, p, operation, outcome, code=None):
        raise RuntimeError('synthetic_audit_failure')
    monkeypatch.setattr(store_module, 'audit', failing_audit)
    with pytest.raises(RuntimeError, match='synthetic_audit_failure'):
        store.extend_key(key, 20)
    assert key_row(store, key) == before and len(extend_audit(store)) == 1


# spec: ANYM-422
def test_extension_racing_revoke_on_the_principal_lock_never_revives_the_key(seeded):
    store, token, _ = seeded
    key = credential(store, token)
    outcome = []
    def extend():
        try: outcome.append(store.extend_key(key, 7))
        except ValueError as error: outcome.append(str(error))
    with store.pool.connection() as c:
        lock_principal(c, 'agent-a')
        c.execute('UPDATE api_keys SET revoked=true WHERE id=%s', (key,))
        worker = threading.Thread(target=extend)
        worker.start()
        time.sleep(0.5)
        assert worker.is_alive()
    worker.join(10)
    assert outcome == ['key_revoked'] and key_row(store, key)['revoked']


# spec: ANYM-422
def test_extension_waiting_on_the_lock_past_the_expiry_instant_is_refused(seeded):
    store, token, _ = seeded
    key = credential(store, token)
    outcome = []
    def extend():
        try: outcome.append(store.extend_key(key, 7))
        except ValueError as error: outcome.append(str(error))
    with store.pool.connection() as c:
        lock_principal(c, 'agent-a')
        c.execute("UPDATE api_keys SET expires_at=clock_timestamp()+interval '1 second' WHERE id=%s", (key,))
        worker = threading.Thread(target=extend)
        worker.start()
        time.sleep(1.5)
    worker.join(10)
    assert outcome == ['key_expired']
    with pytest.raises(APIError, match='^unauthorized$'):
        store.capabilities(token)


class RecordingStore:
    calls = []
    def __init__(self, *args): pass
    def close(self): self.calls.append(('close',))
    def extend_key(self, key, days):
        self.calls.append(('extend_key', key, days))
        return {'key_id': key, 'previous_expires_at': '2026-11-12T08:00:00+00:00',
                'expires_at': '2027-04-19T08:00:00+00:00'}


@pytest.fixture
def cli(monkeypatch, tmp_path):
    RecordingStore.calls = []
    monkeypatch.setattr(operator, 'Store', RecordingStore)
    dsn = tmp_path/'synthetic.dsn'
    dsn.write_text('postgresql://synthetic.invalid/messaging_probe_test')
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn))
    def invoke(*arguments):
        monkeypatch.setattr(sys, 'argv', ['anymsg', *arguments])
        operator.main()
    return invoke, RecordingStore.calls


# spec: ANYM-422
def test_cli_extend_key_prints_only_the_public_id_and_both_expiries(cli, capsys):
    invoke, calls = cli
    key = '00000000-0000-4000-8000-000000000001'
    invoke('extend-key', '--key-id', key, '--days', '183')
    assert calls == [('extend_key', key, 183), ('close',)]
    assert json.loads(capsys.readouterr().out) == {'key_id': key, 'previous_expires_at': '2026-11-12T08:00:00+00:00',
                                                   'expires_at': '2027-04-19T08:00:00+00:00'}


# spec: ANYM-422
@pytest.mark.parametrize('arguments', [
    ['extend-key', '--days', '7'],
    ['extend-key', '--key-id', '00000000-0000-4000-8000-000000000001'],
    ['extend-key', '--key-file', 'synthetic.token', '--days', '7'],
])
def test_cli_extend_key_requires_explicit_key_id_and_days(cli, arguments):
    invoke, calls = cli
    with pytest.raises(SystemExit) as failure: invoke(*arguments)
    assert failure.value.code == 2 and not any(call[0] == 'extend_key' for call in calls)


@pytest.fixture
def schema35_store(database, tmp_path):
    """An independent database built from the unchanged schema-35 SQL and checksums."""
    with old_schema_store(database, tmp_path, 35) as old:
        old.seed()
        yield old


@contextmanager
def old_schema_store(database, tmp_path, last, base=None):
    name = 'anymsg_extend_' + uuid4().hex + '_test'
    dsn = database.pool.conninfo
    old = None
    with psycopg.connect(dsn, autocommit=True) as owner:
        owner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        files = tmp_path / 'originals'
        files.mkdir()
        old = Store(make_conninfo(dsn, dbname=name), files, 'http://testserver')
        source = Path(store_module.__file__).parent
        with old.pool.connection() as c:
            c.execute(base or source.joinpath('schema.sql').read_text())
            for migration in sorted(source.joinpath('migrations').glob('*.sql')):
                version = int(migration.name[:3])
                if version > last:
                    break
                c.execute(migration.read_text())
                if version == 17:
                    from messaging_service.chat_evidence import backfill
                    backfill(c)
                c.execute('INSERT INTO archive_migrations(version,sha256) VALUES(%s,%s)',
                    (version, hashlib.sha256(migration.read_bytes()).hexdigest()))
                c.execute("UPDATE probe_metadata SET value=%s WHERE name='schema_version'", (str(version),))
        yield old
    finally:
        if old is not None:
            old.close()
        with psycopg.connect(dsn, autocommit=True) as owner:
            owner.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


# spec: ANYM-422
def test_migration_036_adds_extend_key_audit_and_keeps_schema35_rows(schema35_store):
    store = schema35_store
    token = store.issue_key('agent-old', ['telegram-demo'], days=1)
    key = credential(store, token)
    store.revoke(credential_id=credential(store, store.issue_key('agent-old', ['telegram-demo'])))
    with store.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '35'
        audit_before = c.execute('SELECT * FROM audit_log ORDER BY id').fetchall()
        migrations = c.execute('SELECT * FROM archive_migrations ORDER BY version').fetchall()
    before = key_row(store, key)
    # Against schema 35 the audit row is refused, so the expiry write rolls back with it.
    with pytest.raises(psycopg.errors.CheckViolation, match='audit_log_operation_check'):
        store.extend_key(key, 30)
    assert key_row(store, key) == before
    store.migrate()
    store.migrate()
    with store.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '50'
        assert c.execute('SELECT * FROM archive_migrations WHERE version<=35 ORDER BY version').fetchall() == migrations
        assert c.execute('SELECT * FROM audit_log ORDER BY id').fetchall() == audit_before
        assert {row['operation'] for row in audit_before} <= {'issue_key', 'revoke_key', 'capabilities'}
    store.extend_key(key, 30)
    assert [str(row['key_id']) for row in extend_audit(store)] == [key]
    assert store.capabilities(token)['credential_id'] == key


# spec: ANYM-422
def test_migration_036_gives_up_on_a_busy_audit_log_without_stalling_requests(schema35_store):
    store = schema35_store
    token = store.issue_key('agent-busy', ['telegram-demo'], days=1)
    holder = psycopg.connect(store.pool.conninfo)
    try:
        # An in-flight audit write holds ROW EXCLUSIVE on audit_log.
        holder.execute("INSERT INTO audit_log(principal,operation,outcome) VALUES('agent-busy','capabilities','ok')")
        outcome = []
        def migrate():
            try: store.migrate(); outcome.append('migrated')
            except psycopg.errors.LockNotAvailable: outcome.append('lock_not_available')
        worker = threading.Thread(target=migrate)
        started = time.monotonic()
        worker.start()
        time.sleep(0.5)
        # Queued behind the migration's lock request, a request still finishes in its 5 s budget.
        assert store.capabilities(token)['credential_id']
        worker.join(10)
        assert outcome == ['lock_not_available'] and time.monotonic() - started < 5
        with store.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '35'
    finally:
        holder.rollback()
        holder.close()
    store.migrate()
    with store.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '50'
        with pytest.raises(psycopg.errors.CheckViolation), c.transaction():
            c.execute("INSERT INTO audit_log(principal,operation,outcome) VALUES('agent-busy','not_an_operation','ok')")



def queued_on(table):
    def ready(watcher):
        return watcher.execute('SELECT 1 FROM pg_locks WHERE relation=%s::regclass AND NOT granted', (table,)).fetchone()
    return ready


@pytest.fixture
def replay_attempt(monkeypatch):
    """Readiness for migrate's replay locks: a failed NOWAIT attempt (it sleeps before the next), or, before
    that fix, migrate's own backend (the one holding its advisory lock) waiting on a lock."""
    attempted = threading.Event()
    def sleep(seconds):
        attempted.set()
        time.sleep(seconds)
    monkeypatch.setattr(store_module, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=sleep))
    def ready(watcher):
        return attempted.is_set() or watcher.execute("""SELECT 1 FROM pg_stat_activity a JOIN pg_locks l
            ON l.pid=a.pid AND l.locktype='advisory' AND l.objid=74509137 AND l.granted
            WHERE a.wait_event_type='Lock'""").fetchone()
    return ready


def migrate_behind_lock(store, holds, ready, request):
    """Holds each (table, mode) of `holds` in its own open transaction, runs migrate in a thread and
    `request` once `ready(watcher)` holds. Returns migrate's outcome and both durations."""
    holders = [psycopg.connect(store.pool.conninfo) for _ in holds]
    outcome = []
    def migrate():
        try: store.migrate(); outcome.append('migrated')
        except psycopg.errors.LockNotAvailable: outcome.append('lock_not_available')
    worker = threading.Thread(target=migrate)
    try:
        for holder, (table, mode) in zip(holders, holds):
            holder.execute(sql.SQL('LOCK TABLE {} IN ' + mode + ' MODE').format(sql.Identifier(table)))
        started = time.monotonic()
        worker.start()
        with psycopg.connect(store.pool.conninfo, autocommit=True) as watcher:
            while not ready(watcher):
                assert time.monotonic() - started < 10, 'migrate never reached its lock'
                time.sleep(0.01)
        queued = time.monotonic()
        with store.pool.connection() as c:
            request(c)
        request_seconds = time.monotonic() - queued
        worker.join(30)
        return outcome, request_seconds, time.monotonic() - started
    finally:
        for holder in holders:
            holder.rollback()
            holder.close()
        if worker.ident: worker.join(30)


def schema_state(store):
    with store.pool.connection() as c:
        return (c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'],
            c.execute('SELECT max(version) AS v FROM archive_migrations').fetchone()['v'],
            c.execute("SELECT to_regclass('message_conflicts') IS NOT NULL AS t").fetchone()['t'])


# spec: ANYM-422
def test_migrate_gives_up_on_a_busy_messages_table_without_stalling_writers(seeded, replay_attempt):
    store = seeded[0]
    # An open writer holds ROW EXCLUSIVE on messages; schema.sql's CREATE INDEX IF NOT EXISTS needs SHARE.
    # A new writer must not queue behind migrate's attempt.
    outcome, writer_seconds, migrate_seconds = migrate_behind_lock(store, [('messages', 'ROW EXCLUSIVE')],
        replay_attempt, lambda c: c.execute('LOCK TABLE messages IN ROW EXCLUSIVE MODE'))
    assert outcome == ['lock_not_available']
    assert writer_seconds < 5 and migrate_seconds < 10
    assert schema_state(store) == ('50', 50, True)
    store.migrate()


# spec: ANYM-422
def test_migrate_lock_timeout_rolls_back_migrations_it_already_applied(database, tmp_path):
    with old_schema_store(database, tmp_path, 16) as store:
        assert schema_state(store) == ('16', 16, False)
        # 017-021 apply, then 022's ALTER TABLE principals waits behind a reader; a request reading
        # principals queues behind that ACCESS EXCLUSIVE request.
        outcome, request_seconds, migrate_seconds = migrate_behind_lock(store,
            [('principals', 'ACCESS SHARE')], queued_on('principals'),
            lambda c: c.execute('SELECT count(*) FROM principals').fetchone())
        assert outcome == ['lock_not_available']
        assert request_seconds < 5 and migrate_seconds < 10
        # Nothing of 017-021 stays: same version, no new migration rows, no new table.
        assert schema_state(store) == ('16', 16, False)
        store.migrate()
        assert schema_state(store) == ('50', 50, True)


# spec: ANYM-422
def test_migrate_never_holds_one_replay_lock_while_waiting_for_another(seeded, replay_attempt):
    store = seeded[0]
    # messages is free, deliveries has an open writer. Taken one by one, migrate would hold SHARE on
    # messages while it waits for deliveries, and a new messages writer would wait with it (up to 2 s
    # per table, adding up past the 5 s budget over three tables).
    outcome, writer_seconds, migrate_seconds = migrate_behind_lock(store, [('deliveries', 'ROW EXCLUSIVE')],
        replay_attempt, lambda c: c.execute('LOCK TABLE messages IN ROW EXCLUSIVE MODE'))
    assert outcome == ['lock_not_available']
    assert writer_seconds < 1 and migrate_seconds < 10
    assert schema_state(store) == ('50', 50, True)
    store.migrate()
    assert schema_state(store) == ('50', 50, True)


# spec: ANYM-422
def test_migrate_replays_replay_tables_missing_from_a_partial_restore(database, tmp_path):
    base = Path(store_module.__file__).with_name('schema.sql').read_text()
    partial = base[:base.index('CREATE TABLE IF NOT EXISTS deliveries')]
    with old_schema_store(database, tmp_path, 1, base=partial) as store:
        store.migrate()
        assert schema_state(store) == ('50', 50, True)
        with store.pool.connection() as c:
            assert c.execute("""SELECT to_regclass('deliveries') IS NOT NULL
                AND to_regclass('download_grants') IS NOT NULL AS present""").fetchone()['present']
