"""ACKed subscription event retention, floor, retention_gap and quarantine hold; synthetic PostgreSQL only."""
from contextlib import contextmanager
from copy import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from messaging_service import subscription_worker, subscriptions
from messaging_service.backup import create_snapshot, database_manifest, quarantine_restore, verify_bundle, verify_snapshot
from messaging_service.store import Store
from messaging_service.subscriptions import match_subscriptions, prune_subscriptions

SOURCE = Path(__file__).resolve().parents[1] / 'services/messaging/messaging_service'
PERMISSIONS = ['read', 'subscription-write', 'subscription-ack']


def set_retention(store, value):
    # The operator activation the worker reads; absent means pruning has not been enabled yet.
    with store.pool.connection() as c:
        if value is None:
            c.execute("DELETE FROM probe_metadata WHERE name='subscription_retention'")
        else:
            c.execute('''INSERT INTO probe_metadata(name,value) VALUES('subscription_retention',%s)
                ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value''', (value,))


@pytest.fixture()
def seeded(seeded):
    """The shared fixture with retention enabled, as after the operator's activation step."""
    store = seeded[0]
    store.set_subscription_retention(True)
    yield seeded
    set_retention(store, None)


def identity(store, name='retention-reader'):
    return store.issue_key(name, ['telegram-demo'], permissions=PERMISSIONS, key_permissions=PERMISSIONS)


def call(store, token, op, *args, **kwargs):
    from messaging_service import subscription_consumption
    with store.authorized(token, op) as (c, p):
        return getattr(subscription_consumption, op)(store, c, p, *args, **kwargs)


def emit(store):
    with store.pool.connection() as c:
        return store.append_event(c, 'telegram-demo', 'demo-tg-1', origin='live')


def ack(store, token, sid, limit=50):
    page = call(store, token, 'fetch_subscription_events', sid, limit=limit)
    return call(store, token, 'ack_subscription_events', sid, page['receipt'], page['ack_version'])


def age(store, sid, events, interval):
    with store.pool.connection() as c:
        c.execute('''UPDATE subscription_matches SET matched_at=clock_timestamp()-%s::interval
            WHERE subscription=%s AND event=ANY(%s)''', (interval, sid, events))


def refs(store, sid):
    with store.pool.connection() as c:
        return [r['event'] for r in c.execute(
            'SELECT event FROM subscription_matches WHERE subscription=%s ORDER BY event', (sid,)).fetchall()]


def floor(store, sid):
    with store.pool.connection() as c:
        return c.execute('SELECT retention_floor FROM subscriptions WHERE id=%s', (sid,)).fetchone()['retention_floor']


def acked_subscription(store, count=3, name='retention-reader'):
    token = identity(store, name)
    sid = store.create_subscription(token, ['telegram-demo-chat'])['subscription_id']
    events = [emit(store) for _ in range(count)]
    match_subscriptions(store)
    return token, sid, events


# spec: ANYM-446
def test_acked_references_are_pruned_at_the_boundary_day_and_free_the_match_budget(seeded, monkeypatch):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store)
    assert ack(store, token, sid)['acked_through'] == events[-1]
    age(store, sid, events[:1], '7 days 1 minute')
    age(store, sid, events[1:2], '6 days 23 hours 59 minutes')
    stats = prune_subscriptions(store)
    assert stats['pruned'] == 1 and stats['retention_failed'] == 0
    assert refs(store, sid) == events[1:] and floor(store, sid) == events[0]
    # The budget counts only retained references: a stopped subscription resumes matching.
    monkeypatch.setenv('SUBSCRIPTION_MAX_MATCHES', '2')
    later = emit(store)
    match_subscriptions(store)
    assert store.get_subscription(token, sid)['blocked_reason'] == 'match_budget'
    age(store, sid, events[1:], '8 days')
    assert prune_subscriptions(store)['pruned'] == 2
    match_subscriptions(store)
    state = store.get_subscription(token, sid)
    assert state['blocked_reason'] is None and state['matched_through'] == later
    assert refs(store, sid) == [later]
    monkeypatch.setenv('SUBSCRIPTION_ACKED_RETENTION_DAYS', '9')
    ack(store, token, sid)
    age(store, sid, [later], '8 days 23 hours')
    assert prune_subscriptions(store)['pruned'] == 0 and refs(store, sid) == [later]


# spec: ANYM-446
def test_unacked_reference_is_never_pruned_at_any_age(seeded):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store)
    assert ack(store, token, sid, limit=1)['acked_through'] == events[0]
    age(store, sid, events, '400 days')
    assert prune_subscriptions(store)['pruned'] == 1
    assert refs(store, sid) == events[1:] and floor(store, sid) == events[0]
    assert prune_subscriptions(store)['pruned'] == 0 and refs(store, sid) == events[1:]
    following = call(store, token, 'fetch_subscription_events', sid)
    assert [item['event_id'] for item in following['items']] == events[1:]


# spec: ANYM-446
def test_passive_only_subscription_is_never_pruned_and_budget_stop_stays_visible(seeded, monkeypatch):
    store, _, _ = seeded
    token = identity(store)
    sid = store.create_subscription(token, ['telegram-demo-chat'])['subscription_id']
    monkeypatch.setenv('SUBSCRIPTION_MAX_MATCHES', '2')
    events = [emit(store) for _ in range(3)]
    match_subscriptions(store)
    assert [i['event_id'] for i in store.get_subscription_events(token, sid)['items']] == events[:2]
    age(store, sid, events, '400 days')
    assert prune_subscriptions(store) == {'pruned': 0, 'pruned_subscriptions': 0,
        'retention_skipped': 0, 'retention_failed': 0}
    assert refs(store, sid) == events[:2] and floor(store, sid) == 0
    state = store.get_subscription(token, sid)
    assert state['blocked_reason'] == 'match_budget' and state['gap']['requires_operator']


# spec: ANYM-446
def test_reader_below_the_floor_gets_retention_gap_and_resumes_at_the_floor(seeded):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store)
    ack(store, token, sid, limit=2)
    age(store, sid, events, '30 days')
    prune_subscriptions(store)
    assert floor(store, sid) == events[1]
    page = store.get_subscription_events(token, sid, after=0)
    assert page['items'] == [] and page['has_more'] and page['next_after'] == events[1]
    assert page['subscription']['gap'] == {'reason': 'retention_gap', 'after': 0, 'through': events[1],
        'recoverable': False, 'floor': events[1]}
    assert store.get_subscription_events(token, sid, after=events[0])['subscription']['gap']['reason'] == 'retention_gap'
    resumed = store.get_subscription_events(token, sid, after=page['next_after'])
    assert [i['event_id'] for i in resumed['items']] == events[2:] and resumed['subscription']['gap'] is None
    fetched = call(store, token, 'fetch_subscription_events', sid)
    assert fetched['acked_through'] == events[1] and [i['event_id'] for i in fetched['items']] == events[2:]


# spec: ANYM-446
def test_reader_of_the_existing_gap_fields_sees_a_non_recoverable_gap(seeded):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store, count=2)
    ack(store, token, sid)
    age(store, sid, events, '30 days')
    prune_subscriptions(store)
    page = store.get_subscription_events(token, sid, after=0)
    gap = page['subscription']['gap']
    # A journal_gap-era reader checks only these fields and must not see a complete empty page.
    assert gap['recoverable'] is False and gap['after'] == 0 and gap['through'] == events[-1]
    assert not (page['items'] == [] and not page['has_more'])


# spec: ANYM-446
def test_injected_failure_mid_batch_rolls_back_floor_and_deletion_and_restart_resumes(seeded, monkeypatch, capsys):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store, count=5)
    ack(store, token, sid)
    age(store, sid, events, '30 days')
    try:
        with store.pool.connection() as c:
            # Fails after the DELETE succeeded, between the two writes of one batch.
            c.execute('''CREATE FUNCTION public.test_retention_fault() RETURNS trigger LANGUAGE plpgsql AS
                $$ BEGIN RAISE EXCEPTION 'synthetic retention fault'; END $$''')
            c.execute('''CREATE TRIGGER test_retention_fault BEFORE UPDATE OF retention_floor ON subscriptions
                FOR EACH ROW EXECUTE FUNCTION public.test_retention_fault()''')
        capsys.readouterr()
        stats = prune_subscriptions(store)
        assert stats['retention_failed'] == 1 and stats['pruned'] == 0
        assert refs(store, sid) == events and floor(store, sid) == 0
        logged = capsys.readouterr().out
        assert json.loads(logged) == {'error': 'subscription_retention_failed', 'class': 'RaiseException'}
    finally:
        with store.pool.connection() as c:
            c.execute('DROP TRIGGER IF EXISTS test_retention_fault ON subscriptions')
            c.execute('DROP FUNCTION IF EXISTS public.test_retention_fault()')
    from messaging_service import subscriptions
    monkeypatch.setattr(subscriptions, 'RETENTION_BATCH', 2)
    assert prune_subscriptions(store)['pruned'] == 2 and floor(store, sid) == events[1]
    assert prune_subscriptions(store)['pruned'] == 2 and floor(store, sid) == events[3]
    assert prune_subscriptions(store)['pruned'] == 1 and floor(store, sid) == events[4]
    assert refs(store, sid) == []


# spec: ANYM-446
def test_pruning_timeout_leaves_matching_and_hints_running_in_the_same_pass(seeded, monkeypatch, capsys, tmp_path):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store)
    ack(store, token, sid)
    age(store, sid, events, '30 days')
    fresh = emit(store)
    hints = []

    class Stop:
        def __init__(self):
            self.done = False

        def is_set(self):
            return self.done

        def set(self):
            self.done = True

        def wait(self, _):
            self.done = True

    class Kept:
        def __init__(self):
            self.pool = store.pool

        def close(self):
            pass

    def flush(target, **kwargs):
        hints.append(target)
        return {'emitted': 0, 'held': 0, 'cancelled': 0}

    dsn = tmp_path / 'synthetic.dsn'
    dsn.write_text('postgresql://synthetic.invalid/unused')
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn))
    monkeypatch.setattr(sys, 'argv', ['subscription_worker'])
    monkeypatch.setattr(subscription_worker, 'Store', lambda *a, **k: Kept())
    monkeypatch.setattr(subscription_worker, 'threading', SimpleNamespace(Event=Stop))
    monkeypatch.setattr(subscription_worker, 'signal', SimpleNamespace(SIGINT=2, SIGTERM=15, signal=lambda *a: None))
    monkeypatch.setattr(subscription_worker, 'flush_hints', flush)
    capsys.readouterr()
    with store.pool.connection() as holder:
        # A concurrent row lock makes the batch DELETE exceed its lock_timeout.
        holder.execute('SELECT event FROM subscription_matches WHERE subscription=%s FOR UPDATE', (sid,))
        subscription_worker.main()
        assert refs(store, sid) == events + [fresh]
    assert floor(store, sid) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [{'error': 'subscription_retention_failed', 'class': 'LockNotAvailable'}]
    assert store.get_subscription(token, sid)['matched_through'] == fresh and len(hints) == 1


# spec: ANYM-446
def test_concurrent_ack_and_pruning_never_delete_above_acked_through(seeded):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store, count=4)
    ack(store, token, sid, limit=1)
    age(store, sid, events, '30 days')
    page = call(store, token, 'fetch_subscription_events', sid, limit=2)
    with store.authorized(token, 'ack_subscription_events') as (c, p):
        from messaging_service.subscription_consumption import ack_subscription_events
        ack_subscription_events(store, c, p, sid, page['receipt'], page['ack_version'])
        # The uncommitted ACK holds the subscription row: pruning skips it, deleting nothing.
        assert prune_subscriptions(store)['retention_skipped'] == 1
        assert refs(store, sid) == events
    assert prune_subscriptions(store)['pruned'] == 3
    assert refs(store, sid) == events[3:] and floor(store, sid) == events[2]


# spec: ANYM-446
def test_pruning_runs_as_the_real_worker_role_with_only_the_named_grants(seeded):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store, name='retention-role-reader')
    ack(store, token, sid, limit=2)
    age(store, sid, events, '30 days')
    role = 'test_subscription_retention_' + uuid4().hex
    ddl = SOURCE.joinpath('subscription_role.sql').read_text().replace('messaging_subscriptions', role)

    class RestrictedPool:
        @contextmanager
        def connection(self):
            with store.pool.connection() as connection:
                connection.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                yield connection

    worker = copy(store)
    worker.pool = RestrictedPool()
    created = False
    try:
        with store.pool.connection() as connection:
            connection.execute(ddl)
        created = True
        stats = prune_subscriptions(worker)
        assert stats['pruned'] == 2 and stats['retention_failed'] == 0
        assert refs(store, sid) == events[2:] and floor(store, sid) == events[1]
        forbidden = ('UPDATE subscriptions SET retention_hold=false', 'DELETE FROM subscriptions',
            'UPDATE subscription_consumption SET acked_through=acked_through', 'DELETE FROM subscription_consumption',
            'DELETE FROM subscription_receipts', 'DELETE FROM subscription_outbox', 'DELETE FROM events',
            "UPDATE probe_metadata SET value='enabled' WHERE name='subscription_retention'",
            "DELETE FROM probe_metadata WHERE name='subscription_retention'")
        for statement in forbidden:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with worker.pool.connection() as connection:
                    connection.execute(statement)
    finally:
        if created:
            with store.pool.connection() as connection:
                connection.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                connection.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


@contextmanager
def restore_database(database, tmp_path, *, through=None):
    """An isolated *_restore_test database, optionally stopped at an older schema."""
    base_dsn = database.pool.conninfo
    name = 'anymsg_ret_' + uuid4().hex + '_restore_test'
    restored = None
    with psycopg.connect(base_dsn, autocommit=True) as owner:
        owner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        restored = Store(make_conninfo(base_dsn, dbname=name), tmp_path / 'synthetic-originals', 'http://testserver')
        if through is None:
            restored.migrate()
        else:
            with restored.pool.connection() as c:
                c.execute(SOURCE.joinpath('schema.sql').read_text())
                for migration in sorted(SOURCE.joinpath('migrations').glob('*.sql')):
                    version = int(migration.name[:3])
                    if version > through:
                        break
                    c.execute(migration.read_text())
                    if version == 17:
                        from messaging_service.chat_evidence import backfill
                        backfill(c)
                    c.execute('INSERT INTO archive_migrations(version,sha256) VALUES(%s,%s)',
                        (version, hashlib.sha256(migration.read_bytes()).hexdigest()))
                    c.execute("UPDATE probe_metadata SET value=%s WHERE name='schema_version'", (str(version),))
        restored.seed()
        yield restored
    finally:
        if restored is not None:
            restored.close()
        with psycopg.connect(base_dsn, autocommit=True) as owner:
            owner.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


# spec: ANYM-446
def test_quarantine_hold_keeps_restored_acked_matches_until_operator_release(database, tmp_path):
    with restore_database(database, tmp_path) as restored:
        restored.set_subscription_retention(True)
        token, sid, events = acked_subscription(restored, name='retention-restore-reader')
        ack(restored, token, sid)
        age(restored, sid, events, '30 days')
        quarantine_restore(restored)
        with restored.pool.connection() as c:
            held = database_manifest(c)
            assert all(r['retention_hold'] for r in c.execute('SELECT retention_hold FROM subscriptions').fetchall())
        assert prune_subscriptions(restored)['pruned'] == 0
        with restored.pool.connection() as c:
            after = database_manifest(c)
        assert after['subscription_matches'] == held['subscription_matches']
        assert after['subscription_consumption'] == held['subscription_consumption']
        assert after['subscriptions'] == held['subscriptions']
        assert restored.release_retention_hold([str(uuid4())]) == {'released': 0}
        assert restored.release_retention_hold([sid]) == {'released': 1}
        with restored.pool.connection() as c:
            assert database_manifest(c)['subscriptions']['sha256'] != held['subscriptions']['sha256']
        # Released, the definition is still closed: terminal definitions keep their references.
        assert prune_subscriptions(restored)['pruned'] == 0
        with restored.pool.connection() as c:
            assert database_manifest(c)['subscription_matches'] == held['subscription_matches']
        # The hold alone also protects a definition that is not closed.
        with restored.pool.connection() as c:
            c.execute("UPDATE subscriptions SET state='active',retention_hold=true WHERE id=%s", (sid,))
        assert prune_subscriptions(restored)['pruned'] == 0 and refs(restored, sid) == events
        assert restored.release_retention_hold([sid]) == {'released': 1}
        assert prune_subscriptions(restored)['pruned'] == 3 and floor(restored, sid) == events[-1]


# spec: ANYM-446
def test_schema38_database_migrates_with_floor_zero_and_quarantine_sets_the_hold(database, tmp_path):
    with restore_database(database, tmp_path, through=38) as restored:
        token, sid, events = acked_subscription(restored, name='retention-legacy-reader')
        ack(restored, token, sid)
        with restored.pool.connection() as c:
            legacy = database_manifest(c)
        assert legacy['subscription_matches']['rows'] == 3
        with pytest.raises(RuntimeError, match='subscription_retention_needs_schema_40'):
            restored.set_subscription_retention(True)
        restored.migrate()
        with restored.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '40'
            row = c.execute('SELECT retention_floor,retention_hold FROM subscriptions WHERE id=%s', (sid,)).fetchone()
            assert row == {'retention_floor': 0, 'retention_hold': False}
            assert database_manifest(c)['subscription_matches'] == legacy['subscription_matches']
        page = restored.get_subscription_events(token, sid)
        assert [i['event_id'] for i in page['items']] == events and page['subscription']['gap'] is None
        quarantine_restore(restored)
        with restored.pool.connection() as c:
            assert c.execute('SELECT retention_hold FROM subscriptions WHERE id=%s', (sid,)).fetchone()['retention_hold']


# spec: ANYM-446
@pytest.mark.parametrize('value', ['0', '367', 'seven'])
def test_invalid_acked_retention_days_fails_worker_startup(monkeypatch, value):
    monkeypatch.setenv('SUBSCRIPTION_ACKED_RETENTION_DAYS', value)
    monkeypatch.setattr(sys, 'argv', ['subscription_worker'])
    monkeypatch.delenv('DATABASE_URL_FILE', raising=False)
    with pytest.raises(ValueError, match='invalid_subscription_setting'):
        subscription_worker.main()


# spec: ANYM-446
def test_pruning_waits_for_the_operator_activation_after_migration(seeded):
    # After migration 40 and during the release, no reader is known to report retention_gap yet.
    store, _, _ = seeded
    set_retention(store, None)
    token, sid, events = acked_subscription(store, name='retention-activation-reader')
    ack(store, token, sid)
    age(store, sid, events, '30 days')
    assert prune_subscriptions(store)['pruned'] == 0 and refs(store, sid) == events and floor(store, sid) == 0
    assert store.set_subscription_retention(False) == {'subscription_retention': 'disabled'}
    assert prune_subscriptions(store)['pruned'] == 0 and refs(store, sid) == events
    assert store.set_subscription_retention(True) == {'subscription_retention': 'enabled'}
    assert prune_subscriptions(store)['pruned'] == 3 and floor(store, sid) == events[-1]


# spec: ANYM-446
@pytest.mark.parametrize('state', ['closed', 'expired'])
def test_closed_or_expired_subscription_keeps_its_acked_references(seeded, state):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store, name='retention-terminal-reader')
    ack(store, token, sid)
    age(store, sid, events, '30 days')
    with store.pool.connection() as c:
        c.execute('UPDATE subscriptions SET state=%s WHERE id=%s', (state, sid))
    assert prune_subscriptions(store)['pruned'] == 0 and refs(store, sid) == events and floor(store, sid) == 0


# spec: ANYM-446
@pytest.mark.parametrize('state', ['active', 'paused'])
def test_subscription_past_its_deadline_keeps_its_acked_references_before_the_matcher_marks_it(seeded, state):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store, name='retention-deadline-reader')
    ack(store, token, sid)
    age(store, sid, events, '30 days')
    # The stored state lags the deadline until the matcher refreshes it, and pruning runs first.
    for deadline in ('-1 second', '30 seconds'):
        with store.pool.connection() as c:
            c.execute('UPDATE subscriptions SET state=%s,expires_at=clock_timestamp()+%s::interval WHERE id=%s',
                (state, deadline, sid))
        assert prune_subscriptions(store)['pruned'] == 0 and refs(store, sid) == events and floor(store, sid) == 0
    with store.pool.connection() as c:
        c.execute("UPDATE subscriptions SET expires_at=clock_timestamp()+interval '1 day' WHERE id=%s", (sid,))
    assert prune_subscriptions(store)['pruned'] == 3 and floor(store, sid) == events[-1]


# spec: ANYM-446
def test_deadline_crossed_after_selection_is_rechecked_under_the_row_lock(seeded, monkeypatch):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store, name='retention-deadline-race-reader')
    ack(store, token, sid)
    age(store, sid, events, '30 days')
    batch = subscriptions._prune_batch

    def lapse_then_prune(c, candidate, days):
        with store.pool.connection() as other:
            other.execute("UPDATE subscriptions SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (sid,))
        return batch(c, candidate, days)

    monkeypatch.setattr(subscriptions, '_prune_batch', lapse_then_prune)
    stats = prune_subscriptions(store)
    assert stats['pruned'] == 0 and stats['retention_skipped'] == 1 and stats['retention_failed'] == 0
    assert refs(store, sid) == events and floor(store, sid) == 0


# spec: ANYM-446
def test_disable_after_candidate_selection_stops_every_later_batch(seeded, monkeypatch):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store, name='retention-disable-reader')
    ack(store, token, sid)
    age(store, sid, events, '30 days')
    batch = subscriptions._prune_batch

    def disable_then_prune(c, candidate, days):
        assert store.set_subscription_retention(False) == {'subscription_retention': 'disabled'}
        return batch(c, candidate, days)

    monkeypatch.setattr(subscriptions, '_prune_batch', disable_then_prune)
    stats = prune_subscriptions(store)
    assert stats['pruned'] == 0 and stats['retention_failed'] == 0
    assert refs(store, sid) == events and floor(store, sid) == 0


# spec: ANYM-446
def test_disable_waits_for_an_in_flight_batch_and_returns_after_its_commit(seeded):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store, name='retention-inflight-reader')
    ack(store, token, sid)
    age(store, sid, events, '30 days')
    with store.pool.connection() as c:
        candidate = c.execute('SELECT id,principal FROM subscriptions WHERE id=%s', (sid,)).fetchone()
    disabled = []
    waiter = threading.Thread(target=lambda: disabled.append(store.set_subscription_retention(False)))
    with store.pool.connection() as c:
        assert subscriptions._prune_batch(c, candidate, 7) == 3
        waiter.start()
        waiter.join(1)
        # The batch read the switch under its lock: the operator's disable cannot return before it commits.
        assert waiter.is_alive() and not disabled
    waiter.join(10)
    assert disabled == [{'subscription_retention': 'disabled'}]
    assert refs(store, sid) == [] and floor(store, sid) == events[-1]
    # After disable returned, a batch for an already-selected candidate deletes nothing.
    later = [emit(store) for _ in range(2)]
    match_subscriptions(store)
    ack(store, token, sid)
    age(store, sid, later, '30 days')
    with store.pool.connection() as c:
        assert subscriptions._prune_batch(c, candidate, 7) is None
    assert refs(store, sid) == later and floor(store, sid) == events[-1]


# spec: ANYM-446
def test_retention_failure_code_passes_the_telemetry_allowlist_without_details():
    from messaging_service.telemetry import safe_event
    try:
        raise RuntimeError('synthetic-private retention detail')
    except RuntimeError as error:
        event = safe_event(error, 'subscriptions', 'subscription_retention_failed')
    assert event is not None and 'synthetic-private' not in json.dumps(event, default=str)


@contextmanager
def snapshot_restored(source, tmp_path):
    """A real pg_dump snapshot of `source`, verified, restored with pg_restore and verified again."""
    container = os.environ.get('ANYMSG_TEST_POSTGRES_CONTAINER')
    if not container:
        if os.environ.get('CI'):
            pytest.fail('CI must provide the isolated PostgreSQL container')
        pytest.skip('isolated PostgreSQL container unavailable locally')
    values = conninfo_to_dict(source.pool.conninfo)
    container_dsn = make_conninfo(host='127.0.0.1', port=5432, user=values['user'], dbname=values['dbname'])
    docker = ['docker', 'exec', '-i', '-e', 'PGPASSWORD=' + values['password'], container]
    capture = tmp_path / 'capture'
    create_snapshot(source, capture, [*docker, 'pg_dump', '--dbname=' + container_dsn])
    verify_bundle(capture)
    name = 'anymsg_ret_' + uuid4().hex + '_restore_test'
    restored = None
    with psycopg.connect(source.pool.conninfo, autocommit=True) as owner:
        owner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        subprocess.run([*docker, 'pg_restore', '--dbname=' + make_conninfo(container_dsn, dbname=name),
            '--exit-on-error', '--no-owner', '--no-acl'],
            input=(capture / 'database.dump').read_bytes(), check=True, capture_output=True)
        restored = Store(make_conninfo(source.pool.conninfo, dbname=name), capture / 'files', 'http://testserver')
        verify_snapshot(restored, capture)
        yield restored
    finally:
        if restored is not None:
            restored.close()
        with psycopg.connect(source.pool.conninfo, autocommit=True) as owner:
            owner.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


# spec: ANYM-446
@pytest.mark.parametrize('version', [38, 39])
def test_legacy_snapshot_quarantined_before_migration_is_never_pruned(database, tmp_path, version):
    # The restore worker's order: verify and quarantine at the snapshot's schema, migrate later.
    with restore_database(database, tmp_path / 'source', through=version) as source:
        token, sid, events = acked_subscription(source, name='retention-legacy-snapshot')
        ack(source, token, sid)
        age(source, sid, events, '30 days')
        with snapshot_restored(source, tmp_path) as restored:
            quarantine_restore(restored)
            with restored.pool.connection() as c:
                quarantined = database_manifest(c)
            restored.migrate()
            restored.set_subscription_retention(True)
            with restored.pool.connection() as c:
                row = c.execute('SELECT state,retention_floor,retention_hold FROM subscriptions WHERE id=%s',
                    (sid,)).fetchone()
                assert row == {'state': 'closed', 'retention_floor': 0, 'retention_hold': False}
                acked = c.execute('SELECT acked_through FROM subscription_consumption WHERE subscription=%s',
                    (sid,)).fetchone()['acked_through']
            assert acked == events[-1]
            assert prune_subscriptions(restored)['pruned'] == 0
            assert restored.release_retention_hold(None) == {'released': 0}
            assert prune_subscriptions(restored)['pruned'] == 0
            with restored.pool.connection() as c:
                after = database_manifest(c)
            assert after['subscription_matches'] == quarantined['subscription_matches']
            assert after['subscription_consumption'] == quarantined['subscription_consumption']


# spec: ANYM-446
def test_schema40_snapshot_carries_the_floor_and_the_hold_through_restore(database, tmp_path):
    with restore_database(database, tmp_path / 'source') as source:
        source.set_subscription_retention(True)
        token, sid, events = acked_subscription(source, count=4, name='retention-floor-snapshot')
        ack(source, token, sid, limit=2)
        age(source, sid, events, '30 days')
        assert prune_subscriptions(source)['pruned'] == 2 and floor(source, sid) == events[1]
        ack(source, token, sid)
        quarantine_restore(source)
        with snapshot_restored(source, tmp_path) as restored:
            with restored.pool.connection() as c:
                row = c.execute('SELECT retention_floor,retention_hold FROM subscriptions WHERE id=%s',
                    (sid,)).fetchone()
                assert row == {'retention_floor': events[1], 'retention_hold': True}
                assert c.execute("SELECT value FROM probe_metadata WHERE name='subscription_retention'"
                    ).fetchone()['value'] == 'enabled'
                c.execute("UPDATE subscriptions SET state='active' WHERE id=%s", (sid,))
            assert prune_subscriptions(restored)['pruned'] == 0 and refs(restored, sid) == events[2:]
            assert restored.release_retention_hold([sid]) == {'released': 1}
            assert prune_subscriptions(restored)['pruned'] == 2 and floor(restored, sid) == events[-1]
