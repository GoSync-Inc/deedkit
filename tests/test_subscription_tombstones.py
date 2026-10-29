"""Finished-hint and closed-definition retention, counters and tombstones (ANYM-447); synthetic PostgreSQL only."""
from contextlib import contextmanager
from copy import copy
import hashlib
from itertools import count
import json
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from messaging_service import subscriptions
from messaging_service.backup import database_manifest, quarantine_restore
from messaging_service.store import APIError, Store
from messaging_service.subscriptions import match_subscriptions, prune_subscriptions, retire_subscriptions
from test_subscription_retention import (SOURCE, ack, acked_subscription, call, emit, floor, identity, refs,
    restore_database, set_retention)
from test_subscriptions import ALL_VISIBLE, dynamic_key

FINISHED = ('delivered', 'cancelled', 'dead_letter')
KEPT = ('subscriptions', 'subscription_matches', 'subscription_outbox', 'subscription_consumption',
    'subscription_receipts')


@pytest.fixture()
def seeded(seeded):
    """The shared fixture with retention enabled, as after the operator's activation step."""
    store = seeded[0]
    store.set_subscription_retention(True)
    yield seeded
    set_retention(store, None)


POSITIONS = count(1)


def hints(store, sid, states, finished_ago=None, *, kind='events', at=None):
    """Synthetic hints of one subscription through an operator receiver, each events hint at a higher position
    than the previous one unless `at` names it; returns their IDs."""
    with store.pool.connection() as c:
        owner = c.execute('SELECT principal,key_id,version FROM subscriptions WHERE id=%s', (sid,)).fetchone()
        receiver = c.execute('''INSERT INTO subscription_receivers(id,principal,key_id,endpoint,secret_file)
            VALUES(gen_random_uuid(),%s,%s,'https://hooks.example.test/wake','/synthetic/receiver.key')
            RETURNING id''', (owner['principal'], owner['key_id'])).fetchone()['id']
        return [str(c.execute('''INSERT INTO subscription_outbox(subscription,subscription_version,receiver,
            through_position,kind,request_id,state,finished_at) VALUES(%s,%s,%s,%s,%s,%s,%s,
            CASE WHEN %s THEN clock_timestamp()-%s::interval END) RETURNING id''',
            (sid, owner['version'], receiver, next(POSITIONS) if at is None else at, kind,
             'synthetic-' + uuid4().hex if kind == 'test' else '', state, state in FINISHED,
             finished_ago)).fetchone()['id']) for state in states]


def outbox(store, sid):
    with store.pool.connection() as c:
        return sorted(str(r['id']) for r in c.execute('SELECT id FROM subscription_outbox WHERE subscription=%s',
            (sid,)).fetchall())


def column(store, sid, name):
    with store.pool.connection() as c:
        return c.execute(sql.SQL('SELECT {} AS v FROM subscriptions WHERE id=%s').format(sql.Identifier(name)),
            (sid,)).fetchone()['v']


def backdate(store, sid, name, interval):
    with store.pool.connection() as c:
        c.execute(sql.SQL('UPDATE subscriptions SET {}=clock_timestamp()-%s::interval WHERE id=%s').format(
            sql.Identifier(name)), (interval, sid))


def recent(store, value):
    with store.pool.connection() as c:
        return c.execute("SELECT %s::timestamptz>clock_timestamp()-interval '1 minute' AS r", (value,)).fetchone()['r']


def counts(store, sid):
    with store.pool.connection() as c:
        return {table: c.execute(sql.SQL('SELECT count(*) AS n FROM {} WHERE subscription=%s').format(
            sql.Identifier(table)), (sid,)).fetchone()['n'] for table in KEPT[1:]}


def close(store, token, sid):
    view = store.get_subscription(token, sid)
    return store.update_subscription(token, sid, view['version'], action='close')


def manifest(store):
    with store.pool.connection() as c:
        found = database_manifest(c)
    return {table: found[table] for table in KEPT}


@contextmanager
def database_at(database, tmp_path, through, *, restore):
    """A separate database stopped at an older schema; a *_restore_test name only when restore is set."""
    base_dsn = database.pool.conninfo
    name = 'anymsg_tomb_' + uuid4().hex + ('_restore_test' if restore else '_legacy')
    legacy = None
    with psycopg.connect(base_dsn, autocommit=True) as owner:
        owner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        legacy = Store(make_conninfo(base_dsn, dbname=name), tmp_path / 'synthetic-originals', 'http://testserver')
        with legacy.pool.connection() as c:
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
        legacy.seed()
        yield legacy
    finally:
        if legacy is not None:
            legacy.close()
        with psycopg.connect(base_dsn, autocommit=True) as owner:
            owner.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


# spec: ANYM-447
def test_finished_hints_are_deleted_after_30_days_and_the_counts_do_not_decrease(seeded):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store, name='tombstone-hint-reader')
    ack(store, token, sid)
    hints(store, sid, FINISHED, '30 days 1 minute')
    young = hints(store, sid, ['delivered'], '29 days 23 hours 59 minutes')
    queued = hints(store, sid, ['pending'])
    # A test hint keeps its request ID's replay while the definition is open.
    probe = hints(store, sid, ['delivered'], '60 days', kind='test')
    before = store.get_subscription(token, sid)['delivery']['hint_counts']
    assert before == {'cancelled': 1, 'dead_letter': 1, 'delivered': 3, 'pending': 1}
    stats = retire_subscriptions(store)
    assert stats['hints_deleted'] == 3 and stats['tombstoned'] == 0 and stats['retire_failed'] == 0
    assert outbox(store, sid) == sorted(young + queued + probe)
    view = store.get_subscription(token, sid)
    assert view['delivery']['hint_counts'] == before
    assert view['delivery']['deleted_hint_counts'] == {'cancelled': 1, 'dead_letter': 1, 'delivered': 1}
    assert recent(store, view['delivery']['hints_deleted_at'])
    # Only the finished hints go: the definition, its references and its ACK position stay.
    assert view['state'] == 'active' and view['closed_at'] is None and view['acked_through'] == events[-1]
    assert refs(store, sid) == events and floor(store, sid) == 0
    assert retire_subscriptions(store)['hints_deleted'] == 0
    assert store.get_subscription(token, sid)['delivery']['hint_counts'] == before


# spec: ANYM-447
@pytest.mark.parametrize('state', ['active', 'paused'])
def test_open_definition_keeps_its_data_however_long_it_was_idle(seeded, state):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store, name='tombstone-idle-reader')
    ack(store, token, sid, limit=1)
    call(store, token, 'fetch_subscription_events', sid)
    if state == 'paused':
        store.update_subscription(token, sid, store.get_subscription(token, sid)['version'], action='pause')
    hints(store, sid, ['pending'])
    with store.pool.connection() as c:
        c.execute('''UPDATE subscriptions SET created_at=clock_timestamp()-interval '400 days',
            updated_at=clock_timestamp()-interval '400 days',last_matched_at=clock_timestamp()-interval '400 days'
            WHERE id=%s''', (sid,))
        c.execute("UPDATE subscription_matches SET matched_at=clock_timestamp()-interval '400 days'")
    before = manifest(store)
    assert column(store, sid, 'closed_at') is None and column(store, sid, 'state') == state
    stats = retire_subscriptions(store)
    assert (stats['hints_deleted'], stats['rows_deleted'], stats['tombstoned'], stats['retire_failed']) == (0, 0, 0, 0)
    assert manifest(store) == before and refs(store, sid) == events


# spec: ANYM-447
def test_every_transition_to_a_terminal_state_records_its_closure_time(seeded):
    store, _, _ = seeded
    token = identity(store, 'tombstone-closure-reader')
    sids = [store.create_subscription(token, ['telegram-demo-chat'])['subscription_id'] for _ in range(4)]
    closed, expiring, raw, kept = sids
    assert close(store, token, closed)['closed_at'] is not None
    with store.pool.connection() as c:
        c.execute("UPDATE subscriptions SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (expiring,))
        # A schema-61 runtime's own statement during the rolling release.
        c.execute("UPDATE subscriptions SET state='access_changed',version=version+1 WHERE id=%s", (raw,))
    expired = store.get_subscription(token, expiring)
    assert expired['state'] == 'expired' and recent(store, expired['closed_at'])
    assert recent(store, column(store, raw, 'closed_at'))
    assert store.get_subscription(token, kept)['closed_at'] is None
    # A later terminal change keeps the first closure time.
    backdate(store, closed, 'closed_at', '3 days')
    first = column(store, closed, 'closed_at')
    with store.pool.connection() as c:
        c.execute("UPDATE subscriptions SET state='access_changed',version=version+1 WHERE id=%s", (closed,))
    assert column(store, closed, 'closed_at') == first
    # The matcher's own transition, for a revoked key.
    with store.pool.connection() as c:
        c.execute('UPDATE api_keys SET revoked=true WHERE id=(SELECT key_id FROM subscriptions WHERE id=%s)', (kept,))
    assert match_subscriptions(store)['access_changed'] == 1
    assert column(store, kept, 'state') == 'access_changed' and recent(store, column(store, kept, 'closed_at'))


# spec: ANYM-447
def test_closed_definition_becomes_a_tombstone_with_retention_gap_and_stops_counting(seeded, monkeypatch):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store, name='tombstone-reader')
    ack(store, token, sid, limit=1)
    call(store, token, 'fetch_subscription_events', sid)
    hints(store, sid, ['delivered'], '1 day')
    version = close(store, token, sid)['version']
    assert counts(store, sid) == {'subscription_matches': 3, 'subscription_outbox': 1,
        'subscription_consumption': 1, 'subscription_receipts': 2}
    backdate(store, sid, 'closed_at', '29 days 23 hours 59 minutes')
    assert retire_subscriptions(store)['rows_deleted'] == 0 and refs(store, sid) == events
    backdate(store, sid, 'closed_at', '30 days 1 minute')
    stats = retire_subscriptions(store)
    assert stats['tombstoned'] == 1 and stats['hints_deleted'] == 1 and stats['retire_failed'] == 0
    assert counts(store, sid) == dict.fromkeys(KEPT[1:], 0)
    view = store.get_subscription(token, sid)
    assert set(view) == {'subscription_id', 'state', 'version', 'closed_at', 'data_deleted', 'deleted_at',
        'deletion_reason', 'retention_floor', 'hint_counts', 'scope_masked'}
    assert (view['state'], view['version'], view['retention_floor'], view['hint_counts']) == (
        'closed', version, events[-1], {'delivered': 1})
    assert view['data_deleted'] is True and view['deletion_reason'] == 'closed_over_30_days'
    assert recent(store, view['deleted_at']) and not recent(store, view['closed_at'])
    assert store.list_subscriptions(token)['items'] == [view]
    page = store.get_subscription_events(token, sid)
    assert page['items'] == [] and page['has_more'] is True and page['next_after'] == events[-1]
    assert page['subscription']['gap'] == {'reason': 'retention_gap', 'after': 0, 'through': events[-1],
        'recoverable': False, 'floor': events[-1]}
    with pytest.raises(APIError, match='subscription_closed'):
        call(store, token, 'fetch_subscription_events', sid)
    # A young tombstone still counts toward SUBSCRIPTION_MAX_DEFINITIONS; one older than 30 days does not.
    monkeypatch.setenv('SUBSCRIPTION_MAX_DEFINITIONS', '1')
    with pytest.raises(APIError, match='subscription_limit'):
        store.create_subscription(token, ['telegram-demo-chat'])
    backdate(store, sid, 'tombstoned_at', '29 days 23 hours 59 minutes')
    with pytest.raises(APIError, match='subscription_limit'):
        store.create_subscription(token, ['telegram-demo-chat'])
    backdate(store, sid, 'tombstoned_at', '30 days 1 minute')
    assert store.create_subscription(token, ['telegram-demo-chat'])['state'] == 'active'
    assert store.get_subscription(token, sid)['data_deleted'] is True
    assert retire_subscriptions(store)['tombstoned'] == 0


# spec: ANYM-447
def test_access_changed_tombstone_stays_masked_and_says_when_it_was_deleted(seeded):
    store, _, _ = seeded
    token, sid, _ = acked_subscription(store, name='tombstone-masked-reader')
    with store.pool.connection() as c:
        c.execute("UPDATE subscriptions SET state='access_changed',version=version+1 WHERE id=%s", (sid,))
    backdate(store, sid, 'closed_at', '31 days')
    assert retire_subscriptions(store)['tombstoned'] == 1 and refs(store, sid) == []
    view = store.get_subscription(token, sid)
    assert set(view) == {'subscription_id', 'state', 'version', 'closed_at', 'data_deleted', 'deleted_at',
        'deletion_reason', 'recreate_required', 'scope_masked'}
    assert view['state'] == 'access_changed' and view['scope_masked'] and recent(store, view['deleted_at'])
    page = store.get_subscription_events(token, sid)
    assert page['items'] == [] and page['scope_masked'] and page['subscription'] == view


# spec: ANYM-447
def test_injected_failure_mid_batch_rolls_back_and_bounded_batches_resume(seeded, monkeypatch, capsys):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store, count=5, name='tombstone-fault-reader')
    ack(store, token, sid)
    hints(store, sid, ['delivered', 'cancelled'], '40 days')
    close(store, token, sid)
    backdate(store, sid, 'closed_at', '31 days')
    before = manifest(store)
    try:
        with store.pool.connection() as c:
            # Fails after the reference DELETE succeeded, between two writes of one batch.
            c.execute('''CREATE FUNCTION public.test_tombstone_fault() RETURNS trigger LANGUAGE plpgsql AS
                $$ BEGIN RAISE EXCEPTION 'synthetic tombstone fault'; END $$''')
            c.execute('''CREATE TRIGGER test_tombstone_fault BEFORE UPDATE OF retention_floor ON subscriptions
                FOR EACH ROW EXECUTE FUNCTION public.test_tombstone_fault()''')
        capsys.readouterr()
        stats = retire_subscriptions(store)
        assert stats['retire_failed'] == 1 and stats['rows_deleted'] == 0 and stats['tombstoned'] == 0
        assert manifest(store) == before
        logged = capsys.readouterr().out
        assert json.loads(logged) == {'error': 'subscription_retention_failed', 'class': 'RaiseException'}
    finally:
        with store.pool.connection() as c:
            c.execute('DROP TRIGGER IF EXISTS test_tombstone_fault ON subscriptions')
            c.execute('DROP FUNCTION IF EXISTS public.test_tombstone_fault()')
    monkeypatch.setattr(subscriptions, 'RETENTION_BATCH', 2)
    # Hints first, counted while their references exist; then references from the bottom.
    first = retire_subscriptions(store)
    assert (first['hints_deleted'], first['rows_deleted'], first['more']) == (2, 2, True) and refs(store, sid) == events
    second = retire_subscriptions(store)
    assert (second['rows_deleted'], second['more']) == (2, True) and floor(store, sid) == events[1]
    # A reader between batches gets the gap up to the moved floor, never a hole.
    assert store.get_subscription_events(token, sid)['subscription']['gap']['through'] == events[1]
    assert retire_subscriptions(store)['rows_deleted'] == 2 and floor(store, sid) == events[3]
    # The receipt and the consumption row count toward the bound: they wait for a fresh batch.
    fourth = retire_subscriptions(store)
    assert (fourth['rows_deleted'], fourth['tombstoned'], fourth['more']) == (1, 0, True)
    last = retire_subscriptions(store)
    assert (last['rows_deleted'], last['tombstoned'], last['more']) == (2, 1, False)
    assert counts(store, sid) == dict.fromkeys(KEPT[1:], 0) and floor(store, sid) == events[-1]
    assert store.get_subscription(token, sid)['hint_counts'] == {'cancelled': 1, 'delivered': 1}


# spec: ANYM-447
def test_unfinished_hint_or_disabled_retention_keeps_the_closed_definition(seeded):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store, name='tombstone-pending-reader')
    queued = hints(store, sid, ['pending'])
    close(store, token, sid)
    backdate(store, sid, 'closed_at', '31 days')
    set_retention(store, 'disabled')
    assert retire_subscriptions(store)['rows_deleted'] == 0 and refs(store, sid) == events
    store.set_subscription_retention(True)
    stats = retire_subscriptions(store)
    # The dispatcher settles it first: references, consumption and the tombstone wait for it.
    assert stats['rows_deleted'] == 0 and stats['tombstoned'] == 0 and outbox(store, sid) == queued
    assert column(store, sid, 'tombstoned_at') is None and refs(store, sid) == events
    with store.pool.connection() as c:
        c.execute("UPDATE subscription_outbox SET state='cancelled',finished_at=clock_timestamp() WHERE subscription=%s",
            (sid,))
    assert retire_subscriptions(store)['tombstoned'] == 1


# spec: ANYM-447
def test_dynamic_counts_and_visibility_survive_hint_deletion_and_the_tombstone(seeded):
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token = dynamic_key(store, 'tombstone-dynamic-reader')
        sid = store.create_subscription(token, sources=ALL_VISIBLE)['subscription_id']
        shown, hidden, again = emit(store), emit(store), emit(store)
        match_subscriptions(store)
        assert refs(store, sid) == [shown, hidden, again]
        with store.pool.connection() as c:
            c.execute('UPDATE subscription_matches SET hidden=true WHERE subscription=%s AND event=%s', (sid, hidden))
        # A and C cover a visible event; B covers only the hidden one, so it is neither counted nor sent.
        hints(store, sid, ['delivered'], '40 days', at=shown)
        pending = hints(store, sid, ['pending'], at=hidden)
        recent_hint = hints(store, sid, ['delivered'], '1 day', at=again)

        def sent():
            with store.authorized(token) as (c, p):
                row = c.execute('SELECT * FROM subscriptions WHERE id=%s', (sid,)).fetchone()
                return subscriptions.hint_visible(c, row, p, hidden)

        before = store.get_subscription(token, sid)['delivery']['hint_counts']
        assert before == {'delivered': 2} and not sent()
        assert retire_subscriptions(store)['hints_deleted'] == 1
        assert outbox(store, sid) == sorted(pending + recent_hint) and column(store, sid, 'hints_deleted_through') == shown
        delivery = store.get_subscription(token, sid)['delivery']
        assert delivery['hint_counts'] == before and delivery['deleted_hint_counts'] == {'delivered': 1}
        # B's range still starts above A's position: deleting A widens nothing.
        assert not sent()
        with store.pool.connection() as c:
            c.execute("UPDATE subscription_outbox SET state='delivered',finished_at=clock_timestamp() WHERE id=%s",
                (pending[0],))
        close(store, token, sid)
        backdate(store, sid, 'closed_at', '31 days')
        # Hints go before their references, so C still counts.
        assert retire_subscriptions(store)['tombstoned'] == 1
        assert store.get_subscription(token, sid)['hint_counts'] == {'delivered': 2}
    finally:
        store.set_dynamic_subscriptions(False)


# spec: ANYM-447
def test_access_changed_definition_keeps_its_acked_references_until_30_days_after_closure(seeded):
    store, _, _ = seeded
    token, sid, events = acked_subscription(store, name='tombstone-acl-reader')
    ack(store, token, sid)
    with store.pool.connection() as c:
        c.execute("UPDATE subscriptions SET state='access_changed',version=version+1 WHERE id=%s", (sid,))
        c.execute("UPDATE subscription_matches SET matched_at=clock_timestamp()-interval '30 days' WHERE subscription=%s",
            (sid,))
    assert prune_subscriptions(store)['pruned'] == 0 and refs(store, sid) == events and floor(store, sid) == 0
    assert retire_subscriptions(store)['rows_deleted'] == 0


# spec: ANYM-447
def test_deletion_waits_until_every_component_runs_schema_62(seeded):
    from messaging_service import __version__
    from messaging_service.store import RUNTIME_SCHEMA
    store, _, _ = seeded
    _, sid, _ = acked_subscription(store, name='tombstone-gate-reader')
    old = hints(store, sid, ['delivered'], '40 days')
    store.set_subscription_retention(False)
    with store.pool.connection() as c:
        saved = c.execute('SELECT * FROM component_heartbeats').fetchall()

    def beat(api_schema):
        with store.pool.connection() as c:
            for component, schema in (('api', api_schema), ('matcher', RUNTIME_SCHEMA), ('dispatcher', RUNTIME_SCHEMA)):
                c.execute('''INSERT INTO component_heartbeats(component,version,schema_version,beat_at)
                    VALUES(%s,%s,%s,clock_timestamp()) ON CONFLICT(component) DO UPDATE SET version=EXCLUDED.version,
                    schema_version=EXCLUDED.schema_version,beat_at=EXCLUDED.beat_at''', (component, __version__, schema))
    try:
        # A schema-61 API would serve hint counts without the deleted ones.
        beat(61)
        assert store.set_subscription_retention(True, verify_components=True) == {
            'error': 'component_schema_mismatch', 'component': 'api', 'schema_version': 61}
        assert retire_subscriptions(store)['hints_deleted'] == 0 and outbox(store, sid) == old
        beat(RUNTIME_SCHEMA)
        assert store.set_subscription_retention(True, verify_components=True) == {'subscription_retention': 'enabled'}
        assert retire_subscriptions(store)['hints_deleted'] == 1 and outbox(store, sid) == []
    finally:
        with store.pool.connection() as c:
            c.execute('DELETE FROM component_heartbeats')
            for row in saved:
                c.execute('INSERT INTO component_heartbeats(component,version,schema_version,beat_at) VALUES(%s,%s,%s,%s)',
                    (row['component'], row['version'], row['schema_version'], row['beat_at']))


# spec: ANYM-447
def test_held_restored_definition_is_untouched_by_both_paths_until_release(database, tmp_path):
    with restore_database(database, tmp_path) as restored:
        restored.set_subscription_retention(True)
        token, sid, events = acked_subscription(restored, name='tombstone-restore-reader')
        ack(restored, token, sid)
        hints(restored, sid, ['delivered'], '60 days')
        quarantine_restore(restored)
        assert column(restored, sid, 'retention_hold') and recent(restored, column(restored, sid, 'closed_at'))
        backdate(restored, sid, 'closed_at', '60 days')
        held = manifest(restored)
        assert retire_subscriptions(restored)['rows_deleted'] == 0 and prune_subscriptions(restored)['pruned'] == 0
        assert manifest(restored) == held
        # The hold alone also keeps an open definition's finished hints.
        with restored.pool.connection() as c:
            c.execute("UPDATE subscriptions SET state='active' WHERE id=%s", (sid,))
        assert column(restored, sid, 'closed_at') is None
        assert retire_subscriptions(restored)['hints_deleted'] == 0
        with restored.pool.connection() as c:
            c.execute("UPDATE subscriptions SET state='closed' WHERE id=%s", (sid,))
        backdate(restored, sid, 'closed_at', '60 days')
        assert restored.release_retention_hold([sid]) == {'released': 1}
        # After the release the 30 days count from the release.
        assert recent(restored, column(restored, sid, 'closed_at'))
        assert retire_subscriptions(restored)['rows_deleted'] == 0 and refs(restored, sid) == events
        backdate(restored, sid, 'closed_at', '30 days 1 minute')
        assert retire_subscriptions(restored)['tombstoned'] == 1 and counts(restored, sid) == dict.fromkeys(KEPT[1:], 0)


# spec: ANYM-447
def test_migration_backfills_the_closure_time_so_nothing_goes_before_30_days(database, tmp_path):
    with database_at(database, tmp_path, 61, restore=False) as legacy:
        token = identity(legacy, 'tombstone-backfill-reader')
        sids = {state: legacy.create_subscription(token, ['telegram-demo-chat'])['subscription_id']
            for state in ('active', 'closed', 'expired', 'access_changed')}
        with legacy.pool.connection() as c:
            for state, sid in sids.items():
                c.execute('''UPDATE subscriptions SET state=%s,updated_at=clock_timestamp()-interval '400 days'
                    WHERE id=%s''', (state, sid))
            started = c.execute('SELECT clock_timestamp() AS t').fetchone()['t']
        legacy.migrate()
        legacy.set_subscription_retention(True)
        with legacy.pool.connection() as c:
            finished = c.execute('SELECT clock_timestamp() AS t').fetchone()['t']
            rows = {str(r['id']): r for r in c.execute(
                'SELECT id,closed_at,retention_hold FROM subscriptions').fetchall()}
        assert rows[sids['active']]['closed_at'] is None
        assert all(started <= rows[sids[state]]['closed_at'] <= finished for state in ('closed', 'expired', 'access_changed'))
        assert not any(row['retention_hold'] for row in rows.values())
        stats = retire_subscriptions(legacy)
        assert stats['rows_deleted'] == 0 and stats['tombstoned'] == 0


# spec: ANYM-447
def test_schema38_restore_quarantined_before_migration_is_backfilled_and_held(database, tmp_path):
    # The restore worker's order: quarantine at the snapshot's schema, migrate later.
    with database_at(database, tmp_path, 38, restore=True) as restored:
        token, sid, events = acked_subscription(restored, name='tombstone-legacy-restore')
        ack(restored, token, sid)
        quarantine_restore(restored)
        restored.migrate()
        restored.set_subscription_retention(True)
        assert column(restored, sid, 'state') == 'closed' and column(restored, sid, 'retention_hold')
        assert recent(restored, column(restored, sid, 'closed_at'))
        backdate(restored, sid, 'closed_at', '31 days')
        assert retire_subscriptions(restored)['rows_deleted'] == 0 and refs(restored, sid) == events
        assert restored.release_retention_hold(None) == {'released': 1}
        assert retire_subscriptions(restored)['rows_deleted'] == 0 and refs(restored, sid) == events


# spec: ANYM-447
def test_retirement_runs_as_the_real_worker_role_with_only_the_named_grants(seeded):
    store, _, _ = seeded
    token, active, _ = acked_subscription(store, name='tombstone-role-reader')
    hints(store, active, ['delivered'], '40 days')
    _, closed, _ = acked_subscription(store, name='tombstone-role-closed')
    with store.pool.connection() as c:
        c.execute("UPDATE subscriptions SET state='closed',version=version+1 WHERE id=%s", (closed,))
        c.execute('INSERT INTO subscription_consumption(subscription,acked_through) VALUES(%s,0)', (closed,))
    hints(store, closed, ['dead_letter'], '1 day')
    backdate(store, closed, 'closed_at', '31 days')
    revoked_token, revoked, _ = acked_subscription(store, name='tombstone-role-revoked')
    with store.pool.connection() as c:
        c.execute('UPDATE api_keys SET revoked=true WHERE id=(SELECT key_id FROM subscriptions WHERE id=%s)', (revoked,))
    role = 'test_subscription_tombstones_' + uuid4().hex
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
        stats = retire_subscriptions(worker)
        assert stats['hints_deleted'] == 2 and stats['tombstoned'] == 1 and stats['retire_failed'] == 0
        assert outbox(store, active) == [] and counts(store, closed) == dict.fromkeys(KEPT[1:], 0)
        # The matcher's transition sets closed_at through the trigger, with no grant on the column.
        assert match_subscriptions(worker)['access_changed'] == 1
        assert recent(store, column(store, revoked, 'closed_at'))
        forbidden = ('UPDATE subscriptions SET closed_at=NULL', 'UPDATE subscriptions SET retention_hold=false',
            'DELETE FROM subscriptions', 'DELETE FROM subscription_sources', 'DELETE FROM subscription_delivery',
            'DELETE FROM subscription_hint_windows', 'SELECT receipt_hash FROM subscription_receipts',
            'UPDATE subscription_outbox SET state=state',
            'UPDATE subscription_consumption SET acked_through=acked_through', 'DELETE FROM events',
            "UPDATE probe_metadata SET value='enabled' WHERE name='subscription_retention'")
        for statement in forbidden:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with worker.pool.connection() as connection:
                    connection.execute(statement)
    finally:
        if created:
            with store.pool.connection() as connection:
                connection.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                connection.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
