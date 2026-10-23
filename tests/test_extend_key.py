"""In-place extension of a live messaging key (ANYM-422): same token, new expiry."""
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sys
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


# spec: ANYM-422
@pytest.mark.parametrize('days', [0, -1, 184, 365, True, 1.5, '7', None])
def test_extend_key_refuses_lifetime_outside_one_to_183_days(seeded, days):
    store, token, _ = seeded
    key = credential(store, token)
    before = key_row(store, key)
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
            c.execute(source.joinpath('schema.sql').read_text())
            for migration in sorted(source.joinpath('migrations').glob('*.sql')):
                version = int(migration.name[:3])
                if version > 35:
                    break
                c.execute(migration.read_text())
                if version == 17:
                    from messaging_service.chat_evidence import backfill
                    backfill(c)
                c.execute('INSERT INTO archive_migrations(version,sha256) VALUES(%s,%s)',
                    (version, hashlib.sha256(migration.read_bytes()).hexdigest()))
                c.execute("UPDATE probe_metadata SET value=%s WHERE name='schema_version'", (str(version),))
        old.seed()
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
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '43'
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
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '43'
        with pytest.raises(psycopg.errors.CheckViolation), c.transaction():
            c.execute("INSERT INTO audit_log(principal,operation,outcome) VALUES('agent-busy','not_an_operation','ok')")
