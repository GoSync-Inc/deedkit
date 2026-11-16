"""A retired source row never strands a later wake: real feed -> gateway step -> outbox; fake egress only."""
import asyncio
from dataclasses import replace
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from messaging_service import android_wake as server
from messaging_service import heartbeats
from messaging_service.event_delivery import dispatch_hints
from messaging_service.store import APIError
from test_delivery_vendor import PUBLIC, gate, identity, policy, register  # noqa: F401
from test_order_events_transport import scoped_helpers

OLD = 2 * 3600  # exported this long ago: past the source's 1 h metadata lifetime


@pytest.fixture
def chain(request, tmp_path):
    request.getfixturevalue('policy')
    store = request.getfixturevalue('gate')
    token = identity(store, 'native-owner')
    endpoint = register(store, token)
    assert dispatch_hints(store, transport=lambda *a, **kw: 200, resolver=PUBLIC)['endpoint_tests']['delivered'] == 1
    for component in heartbeats.COMPONENTS:
        assert heartbeats.record(store, component)
    with store.authorized(token) as (c, actor):
        c.execute('''INSERT INTO android_wake_bindings(principal,key_id,device_id,owner_id,source_id,auth_epoch,
            endpoint,state,routine_verified,installed_verified,valid_until) VALUES(%s,%s,'phone','native-owner',
            'native',1,%s,'active',true,true,clock_timestamp()+interval '1 hour')''',
            (actor['id'], actor['key_id'], endpoint['endpoint_id']))
    with scoped_helpers() as modules:
        skew, access = [0.0], [True]
        principal = SimpleNamespace(id='native-owner')
        def authorize(p, owner, device, order, source, epoch):
            return (access[0] and getattr(p, 'id', p) == owner == 'native-owner' and device == 'phone'
                    and source == 'native' and epoch == 1)
        def open_source():
            events = modules.events.OrderEvents(sqlite3.connect(tmp_path / 'source.sqlite'), authorize,
                                                clock=lambda: time.time() + skew[0])
            control = modules.control.OrderControl(events, source_id='native', auth_epoch=1,
                                                   original_words=lambda c: 'Original owner words.')
            return events, control, modules.feed.OrderFeed(control)
        events, control, feed = open_source()
        # Task-only native context: task and request are the same original ID.
        context = modules.control.NativeOrderContext(principal, 'native-owner', 'phone', 'task-original',
            'task-original', 'original-thread', 'original-turn', 'actual-call', 42, native_namespace='registered-native')
        state = SimpleNamespace(store=store, token=token, modules=modules, principal=principal, events=events,
                                control=control, feed=feed, context=context, skew=skew, access=access,
                                open_source=open_source, calls=[],
                                route=modules.gateway.Route('native-owner', 'phone', 'native', 1, enabled=True))
        yield state
        state.events.db.close()


async def produce(chain, *, lost_ingestion_reply=False):
    async def cursor(device): return server.call(chain.store, chain.token, 'state', {'device_id': device})
    async def source(after, limit): return chain.feed.read_source(chain.principal, 'native-owner', 'phone', after, limit)
    async def receipt(wake):
        chain.calls.append(('receipt', wake))
        return server.call(chain.store, chain.token, 'receipt', {'wake_id': wake})
    async def stage(payload):
        chain.calls.append(('stage', [e['wake_id'] for e in payload['events']]))
        result = server.call(chain.store, chain.token, 'stage', payload)
        if lost_ingestion_reply:
            raise TimeoutError('synthetic lost ingestion reply after commit')
        return result
    return await chain.modules.gateway.step(chain.route, read_cursor=cursor, read_source=source, stage=stage, receipt=receipt)


def old_question(chain, item='old-question-item', turn='original-turn'):
    """A native question exported to the immutable feed two hours ago, while no route consumed it."""
    chain.skew[0] = -OLD
    ctx = replace(chain.context, native_turn_id=turn, native_call_id=None, native_request_id=None)
    wake = chain.control.observe_native_question(ctx, item_id=item, message_id=None,
                                                 questions=[{'title': 'Which address?', 'options': None}])
    chain.feed.read_source(chain.principal, 'native-owner', 'phone', 0, 16)
    chain.skew[0] = 0.0
    return wake


def native_status(chain, n=1):
    ctx = replace(chain.context, native_call_id='status-call-%d' % n, native_request_id=100 + n)
    return asyncio.run(chain.control(ctx, 'notify', {'message': 'Native status %d' % n}))['wake_id']


def intents(chain):
    with chain.store.pool.connection() as c:
        return [str(r['wake_id']) for r in c.execute('SELECT wake_id FROM android_wake_intents ORDER BY source_seq')]


def cursor(chain):
    return server.call(chain.store, chain.token, 'state', {'device_id': 'phone'})['after']


def deliver(chain):
    attempts = []
    def http(url, raw, headers, **kwargs):
        attempts.append(json.loads(raw))
        return 200
    return server.dispatch(chain.store, transport=http, resolver=PUBLIC), attempts


def body(wake):
    return dict(module='android', type='android_wake', wake_id=wake, kind='status', next_call='android_agent')


# spec: ANYM-997
def test_expired_exported_question_does_not_strand_a_later_native_status_through_the_actual_outbox(chain):
    old = old_question(chain)
    raw = chain.feed.read_source(chain.principal, 'native-owner', 'phone', 0, 4)
    fresh = native_status(chain)
    assert asyncio.run(produce(chain))['status'] == 'staged'
    assert intents(chain) == [fresh] and cursor(chain) == 2
    assert deliver(chain) == ({'delivered': 1}, [body(fresh)])
    # Exactly once: no second admission, delivery or stage for either wake.
    assert deliver(chain) == ({}, [])
    assert asyncio.run(produce(chain)) == {'status': 'readback', 'items': []}
    assert intents(chain) == [fresh] and ('stage', [fresh]) in chain.calls and len(chain.calls) == 2
    assert server.call(chain.store, chain.token, 'receipt', {'wake_id': old}) == {'transport_state': 'not_found'}
    assert chain.events.wake(old)['state'] == 'pending'
    assert raw['events'] == []  # the source view already retired the expired row before any page was staged


# spec: ANYM-997
def test_raw_expired_ingestion_is_still_refused_by_the_server_without_moving_the_cursor(chain):
    old = old_question(chain)
    stored = json.loads(chain.events.db.execute('SELECT metadata FROM android_order_feed WHERE wake_id=?',
                                                (old,)).fetchone()[0])
    with pytest.raises(APIError, match='android_wake_expired'):
        server.call(chain.store, chain.token, 'stage', dict(device_id='phone', journal_id='original-journal',
            after=0, next_after=1, events=[stored | {'sequence': 1}]))
    assert intents(chain) == [] and cursor(chain) == 0


# spec: ANYM-997
def test_only_expired_and_closed_rows_cause_no_receipt_stage_or_delivery(chain):
    expired = old_question(chain)
    other = replace(chain.context, task_id='task-closed', request_id='task-closed', native_turn_id='closed-turn')
    closed = chain.control.observe_native_question(replace(other, native_call_id=None, native_request_id=None), item_id='closed-item', message_id=None,
        questions=[{'title': 'Which day?', 'options': None}])
    chain.feed.read_source(chain.principal, 'native-owner', 'phone', 0, 4)
    # The native agent closes the question's own obligation through the actual control path (pending -> cancelled).
    asyncio.run(chain.control(replace(other, native_call_id='close-call', native_request_id=77), 'update',
        {'obligation_ref': chain.events.wake(closed)['order_id'], 'changes': {'lifecycle': 'cancelled'}}))
    assert asyncio.run(produce(chain)) == {'status': 'readback', 'items': []}
    assert chain.calls == [] and intents(chain) == [] and cursor(chain) == 0
    assert deliver(chain) == ({}, [])
    assert [chain.events.wake(w)['state'] for w in (expired, closed)] == ['pending', 'cancelled']


# spec: ANYM-997
def test_many_retired_rows_beyond_the_page_limit_still_let_fresh_statuses_page_once(chain):
    for n in range(5):
        old_question(chain, item='old-%d' % n, turn='old-turn-%d' % n)
    fresh = [native_status(chain, n) for n in range(5)]
    assert asyncio.run(produce(chain))['status'] == 'staged'
    assert intents(chain) == fresh[:4] and cursor(chain) == 9
    assert asyncio.run(produce(chain))['status'] == 'staged'
    assert intents(chain) == fresh and cursor(chain) == 10
    assert [c for c in chain.calls if c[0] == 'stage'] == [('stage', fresh[:4]), ('stage', fresh[4:])]


# spec: ANYM-997
def test_duplicate_page_lost_stage_reply_restart_and_unknown_delivery_never_resend(chain):
    old_question(chain)
    fresh = native_status(chain)
    page = chain.feed.read_source(chain.principal, 'native-owner', 'phone', 0, 4)
    lost = asyncio.run(produce(chain, lost_ingestion_reply=True))
    assert lost['status'] == 'unknown' and [i['wake_id'] for i in lost['items']] == [fresh]
    # The same complete page again is an idempotent readback of the original identity.
    again = server.call(chain.store, chain.token, 'stage', dict(device_id='phone', journal_id=page['journal_id'],
        after=0, next_after=page['next_after'], events=page['events']))
    assert [i['wake_id'] for i in again['items']] == [fresh] and intents(chain) == [fresh]
    chain.events.db.close()
    chain.events, chain.control, chain.feed = chain.open_source()  # host restart, same journal
    assert asyncio.run(produce(chain)) == {'status': 'readback', 'items': []}
    attempts = []
    def accepted_lost(url, raw, headers, **kwargs):
        attempts.append(json.loads(raw))
        raise TimeoutError('synthetic accepted request with lost reply')
    server.dispatch(chain.store, transport=accepted_lost, resolver=PUBLIC)
    server.dispatch(chain.store, transport=accepted_lost, resolver=PUBLIC)
    assert attempts == [body(fresh)]
    assert server.call(chain.store, chain.token, 'receipt', {'wake_id': fresh})['transport_state'] == 'unknown'
    assert asyncio.run(produce(chain)) == {'status': 'readback', 'items': []} and intents(chain) == [fresh]


# spec: ANYM-997
def test_revoked_access_or_malformed_retired_metadata_holds_the_feed_before_any_stage(chain):
    old = old_question(chain)
    native_status(chain)
    chain.access[0] = False
    with pytest.raises(chain.modules.events.OrderRefused, match='access_changed'):
        asyncio.run(produce(chain))
    chain.access[0] = True
    chain.events.db.execute("UPDATE android_order_feed SET metadata='{broken' WHERE wake_id=?", (old,))
    chain.events.db.commit()
    with pytest.raises(chain.modules.events.OrderRefused, match='^invalid_feed_metadata$'):
        asyncio.run(produce(chain))
    assert chain.calls == [] and intents(chain) == [] and cursor(chain) == 0
