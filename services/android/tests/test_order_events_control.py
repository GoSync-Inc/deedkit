"""Native metadata control and owner feed. Synthetic contexts/SQLite only; no native/device/network."""
import asyncio
from dataclasses import replace
import sqlite3
from types import SimpleNamespace

import pytest

from deedkit_android.order_events import OrderEvents, OrderRefused
from deedkit_android.order_events_control import NativeOrderContext, OrderControl
from deedkit_android.order_events_feed import OrderFeed


@pytest.fixture
def world(tmp_path):
    now = [1000.0]
    access = [True]
    principal = SimpleNamespace(id='owner')
    path = tmp_path / 'events.sqlite'
    def authorize(p, owner, device, order, source, epoch):
        ident = getattr(p, 'id', p)
        return access[0] and ident in ('owner', 'grok') and owner == 'owner' and device == 'phone' and source == 'usb' and epoch == 1
    def open_store():
        events = OrderEvents(sqlite3.connect(path), authorize, clock=lambda: now[0])
        control = OrderControl(events, source_id='usb', auth_epoch=1,
                               original_words=lambda ctx: 'EXACT original owner words\nwith clarification')
        return events, control, OrderFeed(control)
    events, control, feed = open_store()
    context = NativeOrderContext(principal, 'owner', 'phone', 'task-one', 'request-one', 'thread',
                                 'turn-one', 'call-one', 42, native_namespace=None)
    yield SimpleNamespace(events=events, control=control, feed=feed, context=context, now=now,
                          access=access, open_store=open_store, principal=principal)
    events.db.close()


def call(w, operation, payload, context=None):
    return asyncio.run(w.control(context or w.context, operation, payload))


def bind(w, context=None):
    return call(w, 'bind', {'next_check_at': 1060.0, 'state': {'awaiting': 'agent inspection'}}, context)


# spec: ANYM-997
def test_native_bind_preorder_has_explicit_internal_ref_and_verbatim_host_origin(world):
    receipt = bind(world)
    ref = receipt['obligation_ref']
    assert receipt['status'] == 'applied' and ref.startswith('obligation:')
    snapshot = call(world, 'update', {'obligation_ref': ref, 'changes': {}},
                    replace(world.context, native_call_id='inspect', native_request_id=43))['snapshot']
    assert snapshot['provider_order_id'] is None and snapshot['package'] is None
    assert snapshot['original_user_words'] == 'EXACT original owner words\nwith clarification'
    assert snapshot['origin']['task_id'] == 'task-one' and snapshot['origin']['request_id'] == 'request-one'
    assert snapshot['origin']['native_request_id'] == 42 and snapshot['origin']['item_id'] is None
    assert snapshot['next_check_at'] == 1060.0


# spec: ANYM-997
def test_new_task_can_discover_update_prior_obligation_without_rebinding_its_origin(world):
    first = bind(world)['obligation_ref']
    newer = replace(world.context, task_id='task-two', request_id='request-two', native_turn_id='turn-two',
                    native_call_id='call-two', native_request_id='42')
    second = bind(world, newer)['obligation_ref']
    acting = replace(newer, native_call_id='call-three', native_request_id=43)
    result = call(world, 'update', {'obligation_ref': first, 'changes': {'state': {'checked': True}}}, acting)
    assert result['origin']['task_id'] == 'task-one' and result['actor']['task_id'] == 'task-two'
    query = replace(acting, native_call_id='call-four', native_request_id=44)
    listing = call(world, 'update', {'changes': {}}, query)['items']
    assert {r['obligation_ref'] for r in listing} == {first, second}
    assert next(r for r in listing if r['obligation_ref'] == first)['origin']['request_id'] == 'request-one'


# spec: ANYM-997
def test_duplicate_native_call_returns_original_receipt_and_conflicting_payload_refuses(world):
    one = bind(world)
    assert bind(world) == one
    with pytest.raises(OrderRefused, match='control_identity_conflict'):
        call(world, 'bind', {'next_check_at': 1070.0, 'state': {}})
    assert len(call(world, 'update', {'changes': {}}, replace(world.context, native_call_id='read', native_request_id=45))['items']) == 1


# spec: ANYM-997
def test_async_preorder_question_retains_array_and_actual_ids_without_rpc_or_question_ids(world):
    ctx = replace(world.context, native_call_id=None, native_request_id=None)
    questions = [{'title': 'Which address?', 'options': None}, {'title': 'Confirm complete basket', 'options': [{'label': 'Yes'}]}]
    wake = world.control.observe_native_question(ctx, item_id='actual-item', message_id='actual-message',
                                                questions=questions)
    page = world.feed.read_source(world.principal, 'owner', 'phone', 0, 4)
    assert page['after'] == 0 and page['next_after'] == 1 and page['gap'] is False
    meta = page['events'][0]
    assert meta['wake_id'] == wake and meta['order_id'] is None
    assert meta['native_request_id'] is None and meta['question_ids'] == []
    assert meta['item_id'] == 'actual-item' and meta['message_id'] == 'actual-message'
    full = world.feed.fetch(world.principal, 'owner', 'phone', wake)
    assert full['payload']['questions'] == questions and full['origin']['task_id'] == 'task-one'
    assert world.events.wake(wake)['state'] == 'pending'  # reads are not delivery or approval ACKs


# spec: ANYM-997
def test_native_scope_epoch_and_protected_operation_fail_closed_without_authority_overrides(world):
    with pytest.raises(OrderRefused):
        call(world, 'request_protected_input', {'reference': 'SYNTHETIC_PROTECTED_REFERENCE'})
    with pytest.raises(OrderRefused):
        call(world, 'bind', {'next_check_at': 1060.0, 'owner': 'other'})
    with pytest.raises(OrderRefused):
        bind(world, replace(world.context, native_request_id=True))
    ref = bind(world)['obligation_ref']
    with pytest.raises(OrderRefused):
        call(world, 'update', {'obligation_ref': ref, 'changes': {}},
             replace(world.context, device_id='other', native_call_id='wrong-device', native_request_id=55))
    world.access[0] = False
    with pytest.raises(OrderRefused, match='access_changed'):
        call(world, 'update', {'obligation_ref': ref, 'changes': {}},
             replace(world.context, native_call_id='revoked', native_request_id=56))


# spec: ANYM-997
def test_feed_restart_keeps_original_journal_sequence_payload_and_expiry(world):
    ctx = replace(world.context, native_call_id='notice', native_request_id=51)
    wake = call(world, 'notify', {'message': 'Agent-chosen status before checkout'}, ctx)['wake_id']
    first = world.feed.read_source(world.principal, 'owner', 'phone', 0, 4)
    world.now[0] = 1010.0
    events, control, feed = world.open_store()
    try:
        assert feed.read_source(world.principal, 'owner', 'phone', 0, 4) == first
        assert feed.fetch(world.principal, 'owner', 'phone', wake)['payload']['message'] == 'Agent-chosen status before checkout'
        assert events.wake(wake)['state'] == 'pending'
    finally:
        events.db.close()


# spec: ANYM-997
def test_rpc_question_keeps_all_original_question_ids_and_distinguishes_numeric_from_string_rpc(world):
    ctx = replace(world.context, native_call_id=None, native_request_id=None, native_namespace='actual-namespace')
    questions = [{'id': 'q1', 'question': 'First?', 'options': None}, {'id': 'q2', 'question': 'Second?', 'options': None}]
    first = world.control.observe_native_question(ctx, item_id='item', message_id=None, questions=questions,
                                                  native_request_id=42, question_ids=['q1', 'q2'])
    second = world.control.observe_native_question(ctx, item_id='item', message_id=None, questions=questions,
                                                   native_request_id='42', question_ids=['q1', 'q2'])
    assert first != second
    page = world.feed.read_source(world.principal, 'owner', 'phone', 0, 4)
    assert [e['native_request_id'] for e in page['events']] == [42, '42']
    assert all(e['question_ids'] == ['q1', 'q2'] for e in page['events'])
    assert world.feed.fetch(world.principal, 'owner', 'phone', first)['payload']['native_namespace'] == 'actual-namespace'


# spec: ANYM-997
def test_concurrent_same_native_call_commits_one_obligation_and_receipt(world):
    from concurrent.futures import ThreadPoolExecutor
    def submit(_):
        events, control, _ = world.open_store()
        try:
            return asyncio.run(control(world.context, 'bind', {'next_check_at': 1060.0, 'state': {}}))
        finally:
            events.db.close()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(submit, range(6)))
    assert all(r == results[0] for r in results)
    assert world.events.db.execute('SELECT count(*) FROM android_order_controls').fetchone()[0] == 1
    assert world.events.db.execute('SELECT count(*) FROM android_order_obligations').fetchone()[0] == 1


# spec: ANYM-997
def test_no_push_chosen_timer_survives_restart_and_received_stops_it_without_erasing_unknown(world):
    ref = bind(world)['obligation_ref']
    nextctx = replace(world.context, native_call_id='timer', native_request_id=61)
    call(world, 'next_check', {'obligation_ref': ref, 'at': 1200.0}, nextctx)
    events, _, _ = world.open_store()
    try:
        world.now[0] = 1100.0
        events.stage_due()
        assert events.pending() == []
        world.now[0] = 1200.0
        events.stage_due()
        wake = events.pending()[0]['wake_id']
        assert events.claim(wake)
        events.recover()
        assert events.wake(wake)['state'] == 'unknown'
        closectx = replace(nextctx, native_call_id='received', native_request_id=62)
        call(world, 'update', {'obligation_ref': ref, 'changes': {'lifecycle': 'received'}}, closectx)
        world.now[0] = 1300.0
        events.stage_due()
        assert events.pending() == [] and events.wake(wake)['state'] == 'unknown'
        assert events._order('owner', ref)['next_check_at'] is None
    finally:
        events.db.close()


# spec: ANYM-997
def test_grok_read_and_handoff_ack_are_separate_and_new_task_preserves_both_responsibilities(world):
    ref = bind(world)['obligation_ref']
    handctx = replace(world.context, native_call_id='handoff', native_request_id=71)
    wake = call(world, 'handoff', {'obligation_ref': ref, 'target_principal': 'grok'}, handctx)['wake_id']
    grok = SimpleNamespace(id='grok')
    content = world.feed.fetch(grok, 'owner', 'phone', wake)
    snapshot = content['payload']['snapshot']
    digest = content['payload']['snapshot_digest']
    assert world.events._order('owner', ref)['responsible'] == 'codex'
    with pytest.raises(OrderRefused, match='handoff_not_delivered'):
        world.feed.ack_handoff(grok, 'owner', 'phone', wake, snapshot_digest=digest,
                              next_check_at=snapshot['next_check_at'])
    receipt = world.feed.ack_read(grok, 'owner', 'phone', wake)
    assert receipt['native_status'] == 'not_observed'
    assert world.events._order('owner', ref)['responsible'] == 'codex'
    newer = replace(world.context, task_id='task-two', request_id='request-two', native_turn_id='turn-two',
                    native_call_id='new-task', native_request_id=72)
    other = bind(world, newer)['obligation_ref']
    ack = world.feed.ack_handoff(grok, 'owner', 'phone', wake, snapshot_digest=digest,
                                next_check_at=snapshot['next_check_at'])
    assert ack == {'responsible': 'grok', 'acknowledged': True}
    assert world.feed.ack_handoff(grok, 'owner', 'phone', wake, snapshot_digest=digest,
                                  next_check_at=snapshot['next_check_at']) == ack
    assert world.events._order('owner', ref)['responsible'] == 'grok'
    assert world.events._order('owner', other)['responsible'] == 'codex'
    assert world.events._order('owner', ref)['next_check_at'] == snapshot['next_check_at']


# spec: ANYM-997
def test_new_owner_state_invalidates_handoff_and_unknown_native_delivery_cannot_be_grok_acked(world):
    ref = bind(world)['obligation_ref']
    handctx = replace(world.context, native_call_id='handoff', native_request_id=81)
    wake = call(world, 'handoff', {'obligation_ref': ref, 'target_principal': 'grok'}, handctx)['wake_id']
    grok = SimpleNamespace(id='grok')
    content = world.feed.fetch(grok, 'owner', 'phone', wake)
    world.feed.ack_read(grok, 'owner', 'phone', wake)
    call(world, 'update', {'obligation_ref': ref, 'changes': {'state': {'checked': 'new state'}}},
         replace(handctx, native_call_id='new-state', native_request_id=82))
    with pytest.raises(OrderRefused, match='handoff_snapshot_changed'):
        world.feed.ack_handoff(grok, 'owner', 'phone', wake, snapshot_digest=content['payload']['snapshot_digest'],
                              next_check_at=content['payload']['snapshot']['next_check_at'])
    assert world.events._order('owner', ref)['responsible'] == 'codex'
    world.now[0] = 1060.0
    world.events.stage_due()
    native = next(w for w in world.events.pending() if w['target'] == 'codex')['wake_id']
    assert world.events.claim(native)
    world.events.recover()
    with pytest.raises(OrderRefused):
        world.feed.ack_read(world.principal, 'owner', 'phone', native)
    assert world.events.wake(native)['state'] == 'unknown'


# spec: ANYM-997
def test_same_actual_native_call_cannot_bypass_its_receipt_by_changing_rpc_identity(world):
    bind(world)
    with pytest.raises(OrderRefused, match='control_identity_conflict'):
        bind(world, replace(world.context, native_request_id='42'))
    assert world.events.db.execute('SELECT count(*) FROM android_order_obligations').fetchone()[0] == 1


# spec: ANYM-997
def test_cached_discovery_cannot_reauthorize_old_source_after_current_epoch_changes(world):
    bind(world)
    query = replace(world.context, native_call_id='discover', native_request_id=91)
    original = call(world, 'update', {'changes': {}}, query)
    assert len(original['items']) == 1
    world.events.authorize = lambda p, owner, device, order, source, epoch: getattr(p, 'id', p) == owner == 'owner' and epoch == 2
    newer = OrderControl(world.events, source_id='usb', auth_epoch=2, original_words=lambda c: 'same actual host task')
    with pytest.raises(OrderRefused, match='access_changed'):
        asyncio.run(newer(query, 'update', {'changes': {}}))


# spec: ANYM-997
def test_native_principal_authentication_hash_is_never_serialized_into_journal_or_feed(world):
    principal = SimpleNamespace(id='owner', key_sha256='SYNTHETIC_PRIVATE_AUTH_HASH')
    ctx = replace(world.context, principal=principal, native_call_id='notice', native_request_id=92)
    wake = call(world, 'notify', {'message': 'Only nonsecret owner status'}, ctx)['wake_id']
    assert 'SYNTHETIC_PRIVATE_AUTH_HASH' not in world.feed.fetch(principal, 'owner', 'phone', wake).__str__()
    dump = '\n'.join(world.events.db.iterdump())
    assert 'SYNTHETIC_PRIVATE_AUTH_HASH' not in dump


# spec: ANYM-997
def test_legacy_unknown_journal_survives_task_metadata_upgrade_without_identity_coercion(world, tmp_path):
    path = tmp_path / 'legacy.sqlite'
    old = OrderEvents(sqlite3.connect(path), world.events.authorize, clock=lambda: 1000.0)
    old.bind('owner', owner='owner', device_id='phone', order_id='legacy-provider', package='com.example.shop',
             source_id='usb', source_order_id='legacy-source', auth_epoch=1, original_request_id='legacy-request',
             native_thread_id='thread', original_user_words='Original legacy words', next_check_at=900.0)
    old.stage_due()
    wake = old.pending()[0]['wake_id']
    assert old.claim(wake)
    old.recover()
    before = old.wake(wake)
    old.db.close()
    upgraded = OrderEvents(sqlite3.connect(path), world.events.authorize, clock=lambda: 1000.0)
    try:
        control = OrderControl(upgraded, source_id='usb', auth_epoch=1, original_words=lambda c: 'New task words')
        assert upgraded.wake(wake) == before
        query = replace(world.context, native_call_id='inspect-legacy', native_request_id=93)
        snapshot = asyncio.run(control(query, 'update', {'obligation_ref': 'legacy-provider', 'changes': {}}))['snapshot']
        assert snapshot['provider_order_id'] == 'legacy-provider'
        assert snapshot['origin']['request_id'] == 'legacy-request' and snapshot['original_user_words'] == 'Original legacy words'
        assert upgraded.wake(wake)['state'] == 'unknown'
    finally:
        upgraded.db.close()


# spec: ANYM-997
def test_async_question_promotes_without_losing_origin_and_transfers_only_explicitly(world):
    ctx = replace(world.context, native_call_id=None, native_request_id=None)
    questions = [{'title': 'Confirm the complete basket?', 'options': None}]
    question = world.control.observe_native_question(ctx, item_id='basket-item', message_id='basket-message', questions=questions)
    world.feed.ack_read(world.principal, 'owner', 'phone', question)
    binding = bind(world)
    ref = binding['obligation_ref']
    assert binding['origin']['message_id'] == 'basket-message'
    assert binding['origin']['native_request_id'] is None and binding['origin']['question_ids'] == []
    hand = call(world, 'handoff', {'obligation_ref': ref, 'target_principal': 'grok'},
                replace(world.context, native_call_id='transfer', native_request_id=94))['wake_id']
    full = world.feed.fetch(SimpleNamespace(id='grok'), 'owner', 'phone', hand)
    snapshot = full['payload']['snapshot']
    assert snapshot['questions'][0]['payload']['questions'] == questions
    assert snapshot['questions'][0]['native_status'] == 'not_observed'
    assert snapshot['origin'] == binding['origin']
    assert world.events._order('owner', ref)['responsible'] == 'codex'
    world.feed.ack_read(SimpleNamespace(id='grok'), 'owner', 'phone', hand)
    world.feed.ack_handoff(SimpleNamespace(id='grok'), 'owner', 'phone', hand,
                          snapshot_digest=full['payload']['snapshot_digest'], next_check_at=1060.0)
    assert world.events._order('owner', ref)['responsible'] == 'grok'
    assert world.events.wake(question)['state'] == 'accepted'  # delivery did not answer/approve it


# spec: ANYM-997
def test_stale_known_handoff_can_be_explicitly_withdrawn_but_unknown_cannot(world):
    ref = bind(world)['obligation_ref']
    ctx = replace(world.context, native_call_id='offer', native_request_id=101)
    old = call(world, 'handoff', {'obligation_ref': ref, 'target_principal': 'grok'}, ctx)['wake_id']
    withdrawal = call(world, 'handoff', {'obligation_ref': ref, 'withdraw_wake_id': old},
                      replace(ctx, native_call_id='withdraw', native_request_id=102))
    assert withdrawal['withdrawn_wake_id'] == old and world.events.wake(old)['state'] == 'cancelled'
    fresh = call(world, 'handoff', {'obligation_ref': ref, 'target_principal': 'grok'},
                 replace(ctx, native_call_id='reoffer', native_request_id=103))['wake_id']
    assert fresh != old and world.events.claim(fresh)
    world.events.recover()
    with pytest.raises(OrderRefused, match='handoff_not_withdrawable'):
        call(world, 'handoff', {'obligation_ref': ref, 'withdraw_wake_id': fresh},
             replace(ctx, native_call_id='bypass-withdraw', native_request_id=104))
    with pytest.raises(OrderRefused, match='order_delivery_unsettled'):
        call(world, 'handoff', {'obligation_ref': ref, 'target_principal': 'grok'},
             replace(ctx, native_call_id='bypass-reoffer', native_request_id=105))
    assert world.events.wake(fresh)['state'] == 'unknown'
    assert world.events._order('owner', ref)['responsible'] == 'codex'


# spec: ANYM-997
def test_native_next_check_settles_only_original_accepted_observation_and_rearms_after_restart(world):
    ref = bind(world)['obligation_ref']
    world.now[0] = 1060.0
    world.events.stage_due()
    wake = world.events.pending()[0]['wake_id']
    assert world.events.claim(wake)
    env = world.events.envelope(wake)
    world.events.complete(wake, {key: env[key] for key in ('original_request_id', 'native_thread_id', 'native_turn_id', 'question_id')} | {'status': 'accepted'})
    nextctx = replace(world.context, native_call_id='checked', native_request_id=111)
    result = call(world, 'next_check', {'obligation_ref': ref, 'at': 1120.0, 'checked_wake_id': wake}, nextctx)
    assert result['next_check_at'] == 1120.0 and world.events.wake(wake)['state'] == 'done'
    assert call(world, 'next_check', {'obligation_ref': ref, 'at': 1120.0, 'checked_wake_id': wake}, nextctx) == result
    restarted, control, _ = world.open_store()
    try:
        world.now[0] = 1120.0
        restarted.stage_due()
        new = restarted.pending()[0]['wake_id']
        assert new != wake and restarted.claim(new)
        restarted.recover()
        with pytest.raises(OrderRefused, match='check_not_owned'):
            asyncio.run(control(replace(nextctx, native_call_id='unknown-check', native_request_id=112), 'next_check',
                                {'obligation_ref': ref, 'at': 1180.0, 'checked_wake_id': new}))
        assert restarted.wake(new)['state'] == 'unknown'
    finally:
        restarted.db.close()


# spec: ANYM-997
def test_provider_reference_update_remains_unique_and_notification_targets_exact_obligation(world):
    from deedkit_android.order_events import Notification
    first = bind(world)['obligation_ref']
    otherctx = replace(world.context, task_id='task-two', request_id='request-two', native_call_id='second', native_request_id=121)
    second = bind(world, otherctx)['obligation_ref']
    provider = {'package': 'com.example.shop', 'provider_order_id': 'actual-provider', 'source_order_id': 'actual-source'}
    call(world, 'update', {'obligation_ref': first, 'changes': provider},
         replace(world.context, native_call_id='provider', native_request_id=122))
    with pytest.raises(OrderRefused, match='provider_already_bound'):
        call(world, 'update', {'obligation_ref': second, 'changes': provider},
             replace(otherctx, native_call_id='duplicate-provider', native_request_id=123))
    event = Notification('real-event', 'owner', 'phone', 'actual-provider', 'com.example.shop', 'usb',
                         'actual-source', 1, 1000.0, 'host-observation-reference')
    assert world.events.observe(world.principal, event) == 'recorded'
    assert {w['order_id'] for w in world.events.pending()} == {first}
    assert world.events._order('owner', second)['seen'] == 0
