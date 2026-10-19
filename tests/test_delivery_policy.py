"""Hint timing policy (ANYM-252); isolated synthetic PostgreSQL only.

Time passes by moving stored deadlines and timestamps back; production code has no test clock.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb
import pytest

from messaging_service import delivery_policy
from messaging_service.delivery_policy import flush_hints
from messaging_service.event_delivery import configure_subscription_delivery, register_receiver
from messaging_service.store import APIError
from messaging_service.subscriptions import match_subscriptions

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


def identity(store, name='policy-reader', permissions=('read', 'subscription-write', 'subscription-ack')):
    return store.issue_key(name, ['telegram-demo'], permissions=list(permissions), key_permissions=list(permissions))


def receiver(store, token, tmp_path, name='policy-reader'):
    secret = tmp_path / ('receiver-' + uuid4().hex + '.key')
    secret.write_bytes(b'synthetic receiver secret bytes 0123456789')
    secret.chmod(0o600)
    with store.authorized(token) as (_, p):
        key_id = str(p['key_id'])
    rid = str(uuid4())
    register_receiver(store, rid, name, key_id, 'https://hooks.example.test/wake', str(secret),
        resolver=lambda *a, **k: ['93.184.216.34'])
    return rid


def configure(store, token, sid, rid, policy=None, version=None):
    if version is None:
        version = store.get_subscription(token, sid)['version']
    with store.authorized(token, 'configure_subscription_delivery') as (c, p):
        return configure_subscription_delivery(store, c, p, sid, version, rid, policy)


def bound(store, token, tmp_path, policy=None, name='policy-reader', **create):
    sid = store.create_subscription(token, ['telegram-demo-chat'], **create)['subscription_id']
    configure(store, token, sid, receiver(store, token, tmp_path, name), policy)
    return sid


def emit(store, message='demo-tg-1'):
    with store.pool.connection() as c:
        return store.append_event(c, 'telegram-demo', message, origin='live')


def matched(store):
    emitted = emit(store)
    match_subscriptions(store)
    return emitted


def shift(store, sid, seconds):
    """Simulate seconds passing for one subscription's durable timers."""
    step = timedelta(seconds=seconds)
    with store.pool.connection() as c:
        c.execute('''UPDATE subscription_hint_windows SET first_match_at=first_match_at-%s,
            last_match_at=last_match_at-%s,due_at=due_at-%s,silence_due_at=silence_due_at-%s,
            silence_fired_at=silence_fired_at-%s WHERE subscription=%s''', (step,) * 5 + (sid,))
        c.execute('''UPDATE subscription_delivery SET silence_deadline=silence_deadline-%s,
            configured_at=configured_at-%s WHERE subscription=%s''', (step, step, sid))
        c.execute('''UPDATE subscription_outbox SET created_at=created_at-%s,admitted_at=admitted_at-%s,
            next_attempt_at=next_attempt_at-%s WHERE subscription=%s''', (step, step, step, sid))


def hints(store, sid, kind='events'):
    with store.pool.connection() as c:
        return c.execute('''SELECT through_position,window_id,state FROM subscription_outbox
            WHERE subscription=%s AND kind=%s ORDER BY created_at,id''', (sid, kind)).fetchall()


def held(store, token, sid):
    delivery = store.get_subscription(token, sid)['delivery']
    return delivery['held_reason'], delivery['next_hint_at']


def local_window(zone, before, after):
    """A daily quiet window around the current database time in zone."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(zone))
    return {'timezone': zone, 'windows': [{'days': ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'],
        'start': (now - timedelta(minutes=before)).strftime('%H:%M'),
        'end': (now + timedelta(minutes=after)).strftime('%H:%M')}]}


# spec: ANYM-252
def test_batch_count_and_window_flush_with_last_batched_through(seeded, tmp_path):
    store, _, _ = seeded
    token = identity(store)
    sid = bound(store, token, tmp_path, {'batch_count': 10})
    events = [matched(store) for _ in range(9)]
    flush_hints(store)
    assert hints(store, sid) == [] and held(store, token, sid)[0] == 'batch'
    events.append(matched(store))
    assert [h['through_position'] for h in hints(store, sid)] == [events[-1]]
    window = bound(store, token, tmp_path, {'batch_window_s': 600})
    last = [matched(store) for _ in range(3)][-1]
    shift(store, window, 599)
    flush_hints(store)
    assert hints(store, window) == []
    shift(store, window, 1)
    flush_hints(store)
    assert [h['through_position'] for h in hints(store, window)] == [last]
    assert held(store, token, window) == (None, None)


# spec: ANYM-252
def test_quiet_hours_hold_until_window_end_and_bypass_emits(seeded, tmp_path):
    store, _, _ = seeded
    token = identity(store)
    quiet = local_window('Europe/Moscow', 60, 60)
    sid = bound(store, token, tmp_path, {'quiet_hours': quiet})
    event = matched(store)
    reason, until = held(store, token, sid)
    assert reason == 'quiet_hours' and hints(store, sid) == []
    assert timedelta(minutes=58) < datetime.fromisoformat(until) - datetime.now(UTC) <= timedelta(minutes=61)
    flush_hints(store)
    assert hints(store, sid) == []
    # The window ends: the stored deadline and the policy both move past now.
    shift(store, sid, 3700)
    with store.pool.connection() as c:
        c.execute('UPDATE subscription_delivery SET policy=%s WHERE subscription=%s',
            (Jsonb({'quiet_hours': local_window('Europe/Moscow', 180, -120)}), sid))
    flush_hints(store)
    flush_hints(store)
    assert [h['through_position'] for h in hints(store, sid)] == [event]
    bypass = bound(store, token, tmp_path, {'quiet_hours': quiet, 'quiet_bypass': ['incoming_message']})
    event = matched(store)
    assert [h['through_position'] for h in hints(store, bypass)] == [event]


# spec: ANYM-252
@pytest.mark.parametrize('zone,window,at,expected', [
    # Moscow 23:00-08:00: 02:00 local is quiet until 08:00 local (UTC+3, no DST).
    ('Europe/Moscow', ('23:00', '08:00'), '2026-10-20T23:00:00Z', '2026-10-21T05:00:00Z'),
    ('Europe/Moscow', ('23:00', '08:00'), '2026-10-21T05:00:00Z', None),
    # Spring forward 2026-03-08: 02:30 does not exist and snaps forward to 03:30 EDT (07:30Z).
    ('America/New_York', ('02:30', '04:00'), '2026-03-08T07:31:00Z', '2026-03-08T08:00:00Z'),
    ('America/New_York', ('02:30', '04:00'), '2026-03-08T07:15:00Z', None),
    # Fall back 2026-11-01: ambiguous 01:30 uses its first (EDT) occurrence, 05:30Z.
    ('America/New_York', ('01:30', '03:00'), '2026-11-01T05:45:00Z', '2026-11-01T08:00:00Z'),
    ('America/New_York', ('01:30', '03:00'), '2026-11-01T05:15:00Z', None),
    # A night window crossing the fall-back hour ends at 07:00 EST.
    ('America/New_York', ('23:00', '07:00'), '2026-11-01T06:30:00Z', '2026-11-01T12:00:00Z'),
])
def test_quiet_hours_use_tz_rules_and_first_occurrence(seeded, zone, window, at, expected):
    store, _, _ = seeded
    policy = {'quiet_hours': {'timezone': zone, 'windows': [{'days': ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'],
        'start': window[0], 'end': window[1]}]}}
    with store.pool.connection() as c:
        result = delivery_policy.quiet_until(c, policy, datetime.fromisoformat(at))
    assert result == (None if expected is None else datetime.fromisoformat(expected))


# spec: ANYM-252
def test_quiet_window_days_and_adjacent_windows_chain(seeded):
    store, _, _ = seeded
    policy = {'quiet_hours': {'timezone': 'UTC', 'windows': [
        {'days': ['tue'], 'start': '22:00', 'end': '23:00'}, {'days': ['tue'], 'start': '23:00', 'end': '06:00'}]}}
    with store.pool.connection() as c:
        # 2026-10-20 is a Tuesday; the second window starts on Tuesday and ends on Wednesday.
        assert delivery_policy.quiet_until(c, policy, datetime(2026, 10, 20, 22, 30, tzinfo=UTC)) == \
            datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
        assert delivery_policy.quiet_until(c, policy, datetime(2026, 10, 21, 22, 30, tzinfo=UTC)) is None


def reply_subscription(store, token, tmp_path, policy):
    with store.authorized(token) as (_, p):
        owner = p
    send = 'send_' + uuid4().hex[:12]
    with store.pool.connection() as c:
        c.execute('''INSERT INTO outgoing_messages(id,principal,key_id,scope_version,key_permission_version,
            request_id,request_hash,account,chat,provider_chat,body,status,provider_message_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,'telegram-demo','telegram-demo-chat','provider-chat','Synthetic','sent',
            'native-anchor')''', (send, owner['id'], owner['key_id'], owner['scope_version'],
            owner['key_permission_version'], 'synthetic-' + send, 'a' * 64))
    return bound(store, token, tmp_path, policy, reply_to_send=send)


def reply(store):
    with store.pool.connection() as c:
        c.execute('''INSERT INTO message_metadata(message,revision,metadata) VALUES('demo-tg-1',1,%s)
            ON CONFLICT(message) DO UPDATE SET metadata=excluded.metadata''',
            (Jsonb({'reply_to_provider_id': 'native-anchor'}),))
    return matched(store)


# spec: ANYM-252
def test_silence_timer_fires_once_and_a_reply_cancels_it(seeded, tmp_path):
    store, _, _ = seeded
    token = identity(store)
    sid = reply_subscription(store, token, tmp_path, {'expect_reply_within_s': 60})
    silence = store.get_subscription(token, sid)['delivery']['silence']
    assert silence['state'] == 'pending' and silence['send_id'].startswith('send_')
    flush_hints(store)
    assert hints(store, sid, 'silence') == []
    shift(store, sid, 61)
    # Two workers and a restart across the deadline: still exactly one reference and one hint.
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: flush_hints(store), range(2)))
    flush_hints(store)
    assert len(hints(store, sid, 'silence')) == 1
    silence = store.get_subscription(token, sid)['delivery']['silence']
    assert silence['state'] == 'hinted' and silence['fired_at'] is not None
    assert store.get_subscription_events(token, sid)['items'] == []
    answered = reply_subscription(store, token, tmp_path, {'expect_reply_within_s': 60})
    reply(store)
    assert store.get_subscription(token, answered)['delivery']['silence']['state'] == 'replied'
    shift(store, answered, 61)
    flush_hints(store)
    assert hints(store, answered, 'silence') == [] and len(hints(store, answered)) == 1


# spec: ANYM-252
def test_budget_coalesces_across_subscriptions_and_loses_no_reference(seeded, tmp_path):
    store, _, _ = seeded
    token = identity(store)
    rid = receiver(store, token, tmp_path)
    sids = [store.create_subscription(token, ['telegram-demo-chat'])['subscription_id'] for _ in range(5)]
    for sid in sids:
        configure(store, token, sid, rid)
    for _ in range(20):
        matched(store)
        flush_hints(store)
    assert sum(len(hints(store, sid)) for sid in sids) == 20
    assert {held(store, token, sid)[0] for sid in sids} == {'budget'}
    assert sum(len(store.get_subscription_events(token, sid, limit=50)['items']) for sid in sids) == 100
    from messaging_service.event_delivery import test_subscription
    with pytest.raises(APIError, match='rate_limited'):
        with store.authorized(token, 'test_subscription') as (c, p):
            test_subscription(store, c, p, sids[0], 'synthetic-over-budget')
    for sid in sids:
        shift(store, sid, 3601)
    flush_hints(store)
    # Each held range becomes one hint at the next slot, through its newest match.
    assert sum(len(hints(store, sid)) for sid in sids) == 25
    assert {held(store, token, sid)[0] for sid in sids} == {None}


# spec: ANYM-252
def test_revocation_and_access_change_cancel_held_hints_and_timers(seeded, tmp_path):
    store, _, _ = seeded
    token = identity(store)
    debounced = bound(store, token, tmp_path, {'debounce_s': 30})
    timer = reply_subscription(store, token, tmp_path, {'expect_reply_within_s': 60})
    matched(store)
    store.revoke(token)
    for sid in (debounced, timer):
        shift(store, sid, 120)
    flush_hints(store)
    match_subscriptions(store)
    flush_hints(store)
    assert hints(store, debounced) == [] and hints(store, timer, 'silence') == []
    other = identity(store, 'policy-other')
    changed = bound(store, other, tmp_path, {'debounce_s': 30}, name='policy-other')
    matched(store)
    store.set_access('policy-other', accounts=['whatsapp-demo-1'])
    shift(store, changed, 120)
    flush_hints(store)
    match_subscriptions(store)
    flush_hints(store)
    assert hints(store, changed) == []


# spec: ANYM-252
def test_concurrent_workers_emit_one_hint_per_window(seeded, tmp_path):
    store, _, _ = seeded
    token = identity(store)
    sid = bound(store, token, tmp_path, {'debounce_s': 30})
    matched(store)
    shift(store, sid, 31)
    barrier = threading.Barrier(2)
    def worker(_):
        barrier.wait()
        return flush_hints(store)['emitted']
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(worker, range(2))) == 1
    flush_hints(store)
    assert len(hints(store, sid)) == 1
    window = hints(store, sid)[0]
    with store.pool.connection() as c:
        row = c.execute('SELECT * FROM subscriptions WHERE id=%s', (sid,)).fetchone()
        duplicate = c.execute('''INSERT INTO subscription_outbox(subscription,subscription_version,receiver,
            through_position,kind,window_id) SELECT subscription,subscription_version,receiver,through_position+1,
            kind,window_id FROM subscription_outbox WHERE subscription=%s ON CONFLICT DO NOTHING RETURNING id''',
            (sid,)).fetchone()
    assert duplicate is None and row['version'] == 2 and window['window_id'] == 1


# spec: ANYM-252
def test_policy_changes_are_versioned_validated_and_need_write(seeded, tmp_path):
    store, _, _ = seeded
    token = identity(store)
    sid = store.create_subscription(token, ['telegram-demo-chat'])['subscription_id']
    rid = receiver(store, token, tmp_path)
    for policy in ({'debounce_s': 601}, {'debounce_s': 30, 'batch_count': 2}, {'debounce_s': 60, 'max_wait_s': 30},
            {'unknown': 1}, {'batch_count': True}, {'expect_reply_within_s': 60},
            {'quiet_hours': {'timezone': 'Mars/Olympus', 'windows': [{'days': ['mon'], 'start': '23:00', 'end': '08:00'}]}},
            {'quiet_hours': {'timezone': 'UTC', 'windows': [{'days': ['mon'], 'start': '25:00', 'end': '08:00'}]}},
            {'quiet_bypass': ['incoming_message']}, 'not-an-object'):
        with pytest.raises(APIError, match='invalid_delivery_policy'):
            configure(store, token, sid, rid, policy)
    with pytest.raises(APIError, match='invalid_delivery_policy'):
        configure(store, token, sid, None, {'debounce_s': 30})
    result = configure(store, token, sid, rid, {'debounce_s': 30})
    assert result['version'] == 2 and result['delivery']['policy'] == {'debounce_s': 30, 'max_wait_s': 300}
    with pytest.raises(APIError, match='subscription_conflict'):
        configure(store, token, sid, rid, {'debounce_s': 10}, version=1)
    reader = identity(store, 'policy-read-only', permissions=('read',))
    with pytest.raises(APIError, match='forbidden'):
        store.configure_subscription_delivery(reader, sid, 2, rid, {'debounce_s': 10})
    assert store.capabilities(token)['subscriptions']['delivery_policy']['key_budget']['default'] == 20


# spec: ANYM-252
def test_same_receiver_reconfiguration_carries_the_held_range(seeded, tmp_path):
    store, _, _ = seeded
    token = identity(store)
    sid = store.create_subscription(token, ['telegram-demo-chat'])['subscription_id']
    rid = receiver(store, token, tmp_path)
    configure(store, token, sid, rid, {'debounce_s': 600, 'max_wait_s': 3600})
    event = matched(store)
    assert held(store, token, sid)[0] == 'debounce'
    configure(store, token, sid, rid, {'debounce_s': 0})
    flush_hints(store)
    assert [h['through_position'] for h in hints(store, sid) if h['state'] == 'pending'] == [event]
    configure(store, token, sid, rid, {'debounce_s': 600, 'max_wait_s': 3600})
    matched(store)
    assert held(store, token, sid)[0] == 'debounce'
    # A receiver change cancels the held range instead of carrying it.
    configure(store, token, sid, receiver(store, token, tmp_path), {'debounce_s': 600, 'max_wait_s': 3600})
    shift(store, sid, 3601)
    flush_hints(store)
    assert held(store, token, sid) == (None, None)
    assert [h['through_position'] for h in hints(store, sid) if h['state'] == 'pending'] == []


# spec: ANYM-252
def test_hint_step_failure_keeps_matches_and_retries_the_range(seeded, tmp_path, monkeypatch):
    from messaging_service import telemetry
    store, _, _ = seeded
    token = identity(store)
    sid = bound(store, token, tmp_path)
    captured = []
    monkeypatch.setattr(telemetry, 'capture', lambda error, code: captured.append(code))
    def broken(*args, **kwargs):
        raise RuntimeError('synthetic hint failure')
    monkeypatch.setattr(delivery_policy, 'after_match', broken)
    event = matched(store)
    assert captured == ['subscription_hint_failed'] and hints(store, sid) == []
    assert [item['event_id'] for item in store.get_subscription_events(token, sid)['items']] == [event]
    assert store.get_subscription(token, sid)['matched_through'] == event
    monkeypatch.undo()
    flush_hints(store)
    flush_hints(store)
    assert [h['through_position'] for h in hints(store, sid)] == [event]


# spec: ANYM-252
def test_matcher_role_holds_and_flushes_hints(seeded, tmp_path):
    store, _, _ = seeded
    token = identity(store)
    sid = bound(store, token, tmp_path, {'debounce_s': 30})
    role = 'test_policy_matcher_' + uuid4().hex
    ddl = (ROOT / 'services/messaging/messaging_service/subscription_role.sql').read_text()

    class RestrictedPool:
        @contextmanager
        def connection(self):
            with store.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                yield c

    matcher = copy(store)
    matcher.pool = RestrictedPool()
    try:
        with store.pool.connection() as c:
            c.execute(ddl.replace('messaging_subscriptions', role))
        event = emit(store)
        match_subscriptions(matcher)
        assert held(store, token, sid)[0] == 'debounce'
        shift(store, sid, 31)
        assert flush_hints(matcher)['emitted'] == 1
        assert [h['through_position'] for h in hints(store, sid)] == [event]
    finally:
        with store.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
