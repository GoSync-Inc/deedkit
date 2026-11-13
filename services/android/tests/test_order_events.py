"""Synthetic order obligations; no phone, host receiver or provider."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from deedkit_android.order_events import Notification, OrderEvents, OrderRefused
from deedkit_android.order_monitor import OrderMonitor


@pytest.fixture
def world(tmp_path):
    path = tmp_path / 'orders.db'
    now = [1000.0]
    allowed = [True]

    def auth(principal, owner, device_id, order_id, source_id, auth_epoch):
        return allowed[0] and principal in ('owner', 'listener', 'grok') and auth_epoch == 1

    def open_store():
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        return OrderEvents(db, auth, clock=lambda: now[0])

    events = open_store()
    events.bind('owner', owner='owner', device_id='phone', order_id='food', package='app.food',
                source_id='listener', source_order_id='trusted-notification-key', auth_epoch=1,
                original_request_id='original-task', native_thread_id='native-thread',
                original_user_words='Please bring my order; keep these exact words.', next_check_at=1100)
    yield events, now, allowed, open_store
    events.db.close()


def notification(**changes):
    fields = dict(event_id='n1', owner='owner', device_id='phone', order_id='food',
                  package='app.food', source_id='listener', source_order_id='trusted-notification-key',
                  auth_epoch=1, observed_at=1000, observation_ref='opaque-notification-reference')
    return Notification(**(fields | changes))


def accepted(payload, **changes):
    return dict(status='accepted', original_request_id=payload['original_request_id'],
                native_thread_id=payload['native_thread_id'], native_turn_id=payload['native_turn_id'],
                question_id=payload['question_id'], native_request_id=payload.get('native_request_id'),
                item_id=payload.get('item_id')) | changes


# spec: ANYM-997
def test_duplicate_and_concurrent_notification_commits_one_wake(world):
    events, _, _, open_store = world

    def push(_):
        store = open_store()
        try:
            return store.observe('listener', notification())
        finally:
            store.db.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(push, range(12)))
    assert outcomes.count('recorded') == 1
    events.observe('listener', notification(event_id='n2'))
    wakes = events.pending()
    assert len(wakes) == 1
    assert wakes[0]['through'] == 2
    assert len(events.observations('owner', 'owner', 'food')) == 2


# spec: ANYM-997
@pytest.mark.parametrize('field,value', [('owner', 'other'), ('device_id', 'other'), ('order_id', 'other'),
                                        ('package', 'other'), ('source_order_id', 'other'),
                                        ('source_id', 'other'), ('auth_epoch', 2)])
def test_unbound_observations_cannot_wake(world, field, value):
    events, *_ = world
    with pytest.raises(OrderRefused):
        events.observe('listener', notification(**{field: value}))
    assert not events.pending()


# spec: ANYM-997
def test_revoked_and_stale_events_fail_closed(world):
    events, now, allowed, _ = world
    with pytest.raises(OrderRefused, match='stale_observation'):
        events.observe('listener', notification(observed_at=600))
    with pytest.raises(OrderRefused):
        events.observe('stranger', notification())
    events.observe('listener', notification())
    allowed[0] = False
    sent = []

    async def submit(*args, **kwargs):
        sent.append(kwargs)
    asyncio.run(OrderMonitor(events, submit=submit).dispatch_once(events.pending()[0]['wake_id']))
    assert not sent
    assert events.pending()[0]['hold_reason'] == 'access_changed'
    now[0] += 500


# spec: ANYM-997
def test_restart_before_and_after_send_and_lost_reply_never_resends(world):
    events, _, _, open_store = world
    events.observe('listener', notification())
    wake = events.pending()[0]['wake_id']
    restarted = open_store()
    restarted.recover()
    attempts = []

    async def lost_reply(owner, device_id, request_id, **envelope):
        attempts.append((owner, device_id, request_id, envelope))
        raise TimeoutError('synthetic lost reply')

    asyncio.run(OrderMonitor(restarted, submit=lost_reply).dispatch_once(wake))
    restarted.db.close()
    restarted = open_store()
    restarted.recover()
    asyncio.run(OrderMonitor(restarted, submit=lost_reply).dispatch_once(wake))
    assert len(attempts) == 1
    assert attempts[0][2] == 'original-task'
    assert restarted.wake(wake)['state'] == 'unknown'
    payload = restarted.envelope(wake)
    restarted.reconcile('owner', wake, accepted(payload))
    assert restarted.wake(wake)['state'] == 'accepted'
    asyncio.run(OrderMonitor(restarted, submit=lost_reply).dispatch_once(wake))
    assert len(attempts) == 1
    restarted.db.close()


# spec: ANYM-997
def test_crash_after_durable_claim_is_unknown_and_receipt_identity_is_checked(world):
    events, _, _, open_store = world
    events.observe('listener', notification())
    wake = events.pending()[0]['wake_id']
    assert events.claim(wake)
    restarted = open_store()
    restarted.recover()
    assert restarted.wake(wake)['state'] == 'unknown'
    with pytest.raises(OrderRefused, match='receipt_identity_mismatch'):
        restarted.reconcile('owner', wake, accepted(restarted.envelope(wake), original_request_id='new-id'))
    assert restarted.wake(wake)['state'] == 'unknown'
    restarted.db.close()


# spec: ANYM-997
def test_missing_receiver_visible_hold_and_timer_survives_restart(world):
    events, now, _, open_store = world
    now[0] = 1101
    restarted = open_store()
    restarted.stage_due()
    wake = restarted.pending()[0]['wake_id']
    asyncio.run(OrderMonitor(restarted).dispatch_once(wake))
    assert restarted.wake(wake)['hold_reason'] == 'wake_receiver_unavailable'
    assert restarted.order('owner', 'owner', 'food')['next_check_at'] == 1100
    restarted.db.close()


# spec: ANYM-997
@pytest.mark.parametrize('terminal', ['received', 'cancelled'])
def test_agent_terminal_observation_stops_timers_and_notifications(world, terminal):
    events, now, *_ = world
    events.observe('listener', notification())
    wake = events.pending()[0]['wake_id']
    events.claim(wake)
    events.complete(wake, accepted(events.envelope(wake)))
    events.checked('owner', wake, next_check_at=None, state={'confirmed': terminal}, lifecycle=terminal)
    now[0] = 5000
    events.stage_due()
    assert not events.pending()
    with pytest.raises(OrderRefused, match='order_closed'):
        events.observe('listener', notification(event_id='late', observed_at=5000))


# spec: ANYM-997
def test_no_push_fallback_and_repeat_arriving_during_native_turn_are_retained(world):
    events, now, *_ = world
    now[0] = 1101
    events.stage_due()
    wake = events.pending()[0]['wake_id']
    events.claim(wake)
    events.complete(wake, accepted(events.envelope(wake)))
    events.observe('listener', notification(event_id='during', observed_at=1101))
    assert not events.pending()
    events.checked('owner', wake, next_check_at=1200, state={'last_seen': 'in transit'})
    assert len(events.pending()) == 1
    assert events.order('owner', 'owner', 'food')['next_check_at'] == 1200


# spec: ANYM-997
def test_handoff_ack_requires_exact_snapshot_and_one_accountable_owner(world):
    events, now, *_ = world
    handoff = events.propose_handoff('owner', 'owner', 'food', target_principal='grok')
    assert events.order('owner', 'owner', 'food')['responsible'] == 'codex'
    events.claim(handoff)
    events.complete(handoff, accepted(events.envelope(handoff)))
    snapshot = events.envelope(handoff)['payload']['snapshot_digest']
    with pytest.raises(OrderRefused):
        events.ack_handoff('listener', handoff, snapshot_digest=snapshot)
    with pytest.raises(OrderRefused):
        events.ack_handoff('grok', handoff, snapshot_digest='wrong')
    events.bind('owner', owner='owner', device_id='phone', order_id='new-task', package='app.shop',
                source_id='listener', source_order_id='shop-key', auth_epoch=1,
                original_request_id='new-original', native_thread_id='native-thread',
                original_user_words='New task', next_check_at=1300)
    events.ack_handoff('grok', handoff, snapshot_digest=snapshot)
    assert events.ack_handoff('grok', handoff, snapshot_digest=snapshot)['acknowledged'] is True
    original = events.order('owner', 'owner', 'food')
    assert original['responsible'] == 'grok'
    assert original['next_check_at'] == 1100
    assert original['original_user_words'].startswith('Please bring')
    assert len(events.orders('owner', 'owner')) == 2
    now[0] = 1101
    events.stage_due()
    assert events.pending()[0]['target'] == 'grok'


# spec: ANYM-997
def test_handoff_is_stale_if_new_order_observation_arrives_before_ack(world):
    events, *_ = world
    handoff = events.propose_handoff('owner', 'owner', 'food', target_principal='grok')
    events.claim(handoff)
    events.complete(handoff, accepted(events.envelope(handoff)))
    digest = events.envelope(handoff)['payload']['snapshot_digest']
    events.observe('listener', notification())
    with pytest.raises(OrderRefused, match='handoff_snapshot_changed'):
        events.ack_handoff('grok', handoff, snapshot_digest=digest)
    assert events.order('owner', 'owner', 'food')['responsible'] == 'codex'


# spec: ANYM-997
def test_one_inflight_delivery_per_device_and_safe_boundary_busy(world):
    events, *_ = world
    events.observe('listener', notification())
    wake = events.pending()[0]['wake_id']
    calls = []

    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        async def submit(*args, **kwargs):
            calls.append(kwargs)
            started.set()
            await release.wait()
            return accepted(events.envelope(wake), status='refused', reason='device_busy')

        monitor = OrderMonitor(events, submit=submit)
        first = asyncio.create_task(monitor.dispatch_once(wake))
        await started.wait()
        await monitor.dispatch_once(wake)
        assert len(calls) == 1
        release.set()
        await first

    asyncio.run(exercise())
    assert events.wake(wake)['state'] == 'pending'
    assert events.wake(wake)['hold_reason'] == 'device_busy'


# spec: ANYM-997
def test_app_level_observation_is_explicitly_uncorrelated_and_not_discarded(world):
    events, *_ = world
    events.observe('listener', notification(order_id=None, source_order_id=None))
    row = events.observations('owner', 'owner', 'food')[0]
    assert row['correlation'] == 'unresolved_app'
    assert events.envelope(events.pending()[0]['wake_id'])['payload']['observations_untrusted'] is True


# spec: ANYM-997
def test_question_reply_keeps_native_ids_and_cannot_answer_approvals(world):
    events, *_ = world
    question = events.notify('owner', 'owner', 'food', notice_id='question-event', kind='question',
                            native_turn_id='turn-7', question_id='q1', native_request_id=42, item_id='item-9',
                            payload={'method': 'item/tool/requestUserInput', 'questions': [{'id': 'q1'}]})
    assert question == events.notify('owner', 'owner', 'food', notice_id='question-event', kind='question',
                                    native_turn_id='turn-7', question_id='q1', native_request_id=42, item_id='item-9',
                                    payload={'method': 'item/tool/requestUserInput', 'questions': [{'id': 'q1'}]})
    events.claim(question)
    events.complete(question, accepted(events.envelope(question)))
    reply = events.answer('owner', question, native_request_id=42, item_id='item-9',
                          answers={'q1': ['the blue option']})
    envelope = events.envelope(reply)
    assert envelope['original_request_id'] == 'original-task'
    assert envelope['native_turn_id'] == 'turn-7'
    assert envelope['payload']['native_request_id'] == 42
    assert envelope['payload']['answers'] == {'q1': ['the blue option']}
    with pytest.raises(OrderRefused):
        events.answer('listener', question, native_request_id=42, item_id='item-9', answers={'q1': ['yes']})
    with pytest.raises(OrderRefused):
        events.notify('owner', 'owner', 'food', notice_id='approval', kind='question', native_turn_id='turn-7',
                      question_id='q1', native_request_id=43, item_id='item-9',
                      payload={'method': 'execCommandApproval', 'questions': [{'id': 'q1'}]})


# spec: ANYM-997
def test_unknown_delivery_blocks_handoff_and_new_event_cannot_bypass_it(world):
    events, *_ = world
    events.observe('listener', notification())
    wake = events.pending()[0]['wake_id']
    events.claim(wake)
    events.unknown(wake)
    events.observe('listener', notification(event_id='n2'))
    assert not events.pending()
    with pytest.raises(OrderRefused, match='order_delivery_unsettled'):
        events.propose_handoff('owner', 'owner', 'food', target_principal='grok')


# spec: ANYM-997
def test_two_orders_and_new_task_share_one_safe_device_boundary(world):
    events, *_ = world
    events.bind('owner', owner='owner', device_id='phone', order_id='new-task', package='app.shop',
                source_id='listener', source_order_id='shop-key', auth_epoch=1,
                original_request_id='new-original', native_thread_id='native-thread',
                original_user_words='New task, keep food delivery active', next_check_at=1300)
    events.observe('listener', notification())
    events.observe('listener', notification(event_id='shop', order_id='new-task', package='app.shop',
                                            source_order_id='shop-key'))
    first, second = [wake['wake_id'] for wake in events.pending()]
    started_native = []

    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        async def submit(owner, device, request, **kwargs):
            if started_native:
                return accepted(events.envelope(second), status='refused', reason='device_busy')
            started_native.append(request)
            started.set()
            await release.wait()
            return accepted(events.envelope(first))

        monitor = OrderMonitor(events, submit=submit)
        sending = asyncio.create_task(monitor.dispatch_once(first))
        await started.wait()
        await monitor.dispatch_once(second)
        assert events.wake(second)['state'] == 'pending'
        release.set()
        await sending
        await monitor.dispatch_once(second)

    asyncio.run(exercise())
    assert started_native == ['original-task']
    assert events.wake(second)['hold_reason'] == 'device_busy'
    assert len(events.orders('owner', 'owner')) == 2
    assert events.order('owner', 'owner', 'food')['next_check_at'] == 1100
