"""Conversation metadata acceptance: real journals/helpers, synthetic UI observations only.

These cases do not execute a model or establish permission to send a chat.
"""
import asyncio
from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from deedkit_android.order_events import Notification, OrderEvents, OrderRefused
from deedkit_android.order_events_control import NativeOrderContext, OrderControl
from deedkit_android.order_supervision import Supervision


@pytest.fixture
def conversation(tmp_path):
    now, rights = [1000.0], [True]
    words = {
        'health-task': '  Help with this Halodoc consultation and its pharmacy/delivery.\nAsk me for unknown facts.  ',
        'shop-task': '  Agree dispatch with this Tokopedia seller; keep tracking and explicitly hand long monitoring to Grok.  ',
        'new-task': 'Keep the earlier obligations while helping with this new task.',
    }
    path = tmp_path / 'conversation.sqlite'
    owner = SimpleNamespace(id='owner')
    grok = SimpleNamespace(id='grok')
    def authorize(principal, target, device, order, source, epoch):
        return (rights[0] and getattr(principal, 'id', principal) in ('owner', 'grok') and target == 'owner'
                and device in ('phone', 'tablet') and source == 'source' and epoch == 1)
    def open_store():
        events = OrderEvents(sqlite3.connect(path), authorize, clock=lambda: now[0])
        control = OrderControl(events, source_id='source', auth_epoch=1, original_words=lambda ctx: words[ctx.task_id])
        return events, control
    events, control = open_store()
    counter = iter(range(1, 100))
    def context(task, *, thread='thread'):
        n = next(counter)
        return NativeOrderContext(owner, 'owner', 'phone', task, task, thread, 'turn',
                                  f'call-{n}', n, native_namespace='registered-native')
    async def act(task, operation, payload, **kwargs):
        return await control(context(task, **kwargs), operation, payload)
    yield SimpleNamespace(events=events, control=control, feed=control.feed, owner=owner, grok=grok,
        now=now, rights=rights, words=words, context=context, act=act, open_store=open_store)
    events.db.close()


# spec: ANYM-997
def test_tokopedia_handoff_includes_later_task_question_without_inventing_order_correlation(conversation):
    async def scenario():
        w = conversation
        ui = Mock(return_value={'seller': {'participant_ref': 'seller-fixture', 'evidence': 'observed-order-chat'}})
        ref = (await w.act('shop-task', 'bind', {
            'package': 'fixture.tokopedia', 'provider_order_id': 'shop-order', 'source_order_id': 'shop-chat',
            'next_check_at': 1800, 'state': ui()}))['obligation_ref']
        ctx = replace(w.context('shop-task'), native_call_id=None, native_request_id=None)
        questions = [{'title': 'The seller changed the complete basket price. Do you approve the changed basket?',
                      'options': None}]
        question = w.control.observe_native_question(ctx, item_id='native-basket-item',
            message_id='native-basket-message', questions=questions)
        proposal = (await w.act('shop-task', 'handoff', {
            'obligation_ref': ref, 'target_principal': 'grok'}))['wake_id']
        exported = w.feed.fetch(w.grok, 'owner', 'phone', proposal)
        pending = exported['payload']['snapshot']['questions']
        assert [q['wake_id'] for q in pending] == [question]
        assert pending[0]['payload']['questions'] == questions
        assert pending[0]['payload']['native_request_id'] is None
        assert pending[0]['payload']['question_ids'] == []
        assert pending[0]['scope'] == 'task_notice' and pending[0]['order_id'] is None
        assert pending[0]['task_id'] == 'shop-task' and pending[0]['original_request_id'] == 'shop-task'
        assert pending[0]['obligation_ref'] != ref
        assert exported['original_user_words'] == w.words['shop-task']
    asyncio.run(scenario())


# spec: ANYM-997
def test_halodoc_owner_facts_and_physician_reference_survive_consultation_to_delivery(conversation):
    async def scenario():
        w = conversation
        ui = Mock(return_value={
            'doctor': {'participant_ref': 'doctor-fixture', 'evidence': 'consultation-view'},
            'owner_facts': {'supplied': {'value': 'synthetic owner fact', 'source': 'original-owner-message'},
                            'unknown': None},
            'prescription': {'reference': 'physician-artifact-fixture', 'issuer': 'doctor-fixture',
                             'evidence': 'observed-physician-artifact'},
        })
        observed = ui()
        ctx = replace(w.context('health-task'), native_call_id=None, native_request_id=None)
        questions = [{'title': 'The doctor asks for a fact absent from your original message. Please supply it.',
                      'options': None}]
        question = w.control.observe_native_question(ctx, item_id='clinical-item', message_id='clinical-message',
                                                     questions=questions)
        before = w.feed.fetch(w.owner, 'owner', 'phone', question)
        assert before['metadata']['order_id'] is None
        assert before['payload']['questions'] == questions and before['payload']['question_ids'] == []
        assert before['payload']['native_request_id'] is None
        ref = (await w.act('health-task', 'bind', {'package': 'fixture.halodoc', 'next_check_at': 1150,
            'state': observed}))['obligation_ref']
        state = observed | {
            'pharmacy': {'participant_ref': 'pharmacy-fixture', 'evidence': 'same-order-detail'},
            'delivery': {'participant_ref': 'courier-fixture', 'evidence': 'same-order-detail'},
            'basket': {'reference': 'complete-basket-fixture', 'owner_confirmation': None},
        }
        await w.act('health-task', 'update', {'obligation_ref': ref, 'changes': {
            'provider_order_id': 'health-order', 'source_order_id': 'health-order-chat', 'state': state}})
        row = w.events.order(w.owner, 'owner', ref)
        assert row['state'] == state and row['state']['owner_facts']['unknown'] is None
        assert row['state']['basket']['owner_confirmation'] is None
        assert row['original_user_words'] == w.words['health-task']
        assert row['original_request_id'] == 'health-task' and row['next_check_at'] == 1150
        assert w.events.db.execute("SELECT count(*) FROM android_order_obligations").fetchone()[0] == 1
        # Read receipts acknowledge delivery only, not a medical answer, prescription or basket consent.
        assert w.feed.ack_read(w.owner, 'owner', 'phone', question)['native_status'] == 'not_observed'
        assert w.events.order(w.owner, 'owner', ref)['state'] == state
    asyncio.run(scenario())


# spec: ANYM-997
def test_tokopedia_explicit_grok_acceptance_keeps_seller_agreement_and_new_task_obligations(conversation):
    async def scenario():
        w = conversation
        state = Mock(return_value={
            'seller': {'participant_ref': 'seller-fixture', 'evidence': 'bound-order-chat'},
            'agreement': {'text': 'Owner-approved complete basket; ask seller about dispatch.',
                          'source': 'original-owner-message'},
            'tracking': {'reference': 'tracking-fixture', 'evidence': 'observed-order-detail'},
        })()
        ref = (await w.act('shop-task', 'bind', {'package': 'fixture.tokopedia', 'next_check_at': 1800,
            'provider_order_id': 'shop-order', 'source_order_id': 'shop-chat', 'state': state}))['obligation_ref']
        proposal = (await w.act('shop-task', 'handoff', {
            'obligation_ref': ref, 'target_principal': 'grok'}))['wake_id']
        full = w.feed.fetch(w.grok, 'owner', 'phone', proposal)
        snapshot = full['payload']['snapshot']
        assert json.loads(snapshot['state_json']) == state
        assert snapshot['next_check_at'] == 1800 and snapshot['task_id'] == 'shop-task'
        assert w.events.order(w.owner, 'owner', ref)['responsible'] == 'codex'
        with pytest.raises(OrderRefused, match='handoff_not_delivered'):
            w.feed.ack_handoff(w.grok, 'owner', 'phone', proposal,
                snapshot_digest=full['payload']['snapshot_digest'], next_check_at=1800)
        w.feed.ack_read(w.grok, 'owner', 'phone', proposal)
        newer = (await w.act('new-task', 'bind', {'next_check_at': 1300, 'state': {'pending': True}}))['obligation_ref']
        assert w.events.order(w.owner, 'owner', ref)['responsible'] == 'codex'
        ack = w.feed.ack_handoff(w.grok, 'owner', 'phone', proposal,
            snapshot_digest=full['payload']['snapshot_digest'], next_check_at=1800)
        assert ack == {'responsible': 'grok', 'acknowledged': True}
        assert w.events.order(w.grok, 'owner', ref)['state'] == state
        assert w.events.order(w.grok, 'owner', ref)['next_check_at'] == 1800
        assert w.events.order(w.owner, 'owner', newer)['responsible'] == 'codex'
        assert w.feed.ack_handoff(w.grok, 'owner', 'phone', proposal,
            snapshot_digest=full['payload']['snapshot_digest'], next_check_at=1800) == ack
    asyncio.run(scenario())


# spec: ANYM-997
def test_new_or_changed_task_question_invalidates_previously_read_handoff(conversation):
    async def scenario():
        w = conversation
        ref = (await w.act('shop-task', 'bind', {'next_check_at': 1800, 'state': {}}))['obligation_ref']
        proposal = (await w.act('shop-task', 'handoff', {
            'obligation_ref': ref, 'target_principal': 'grok'}))['wake_id']
        before = w.feed.fetch(w.grok, 'owner', 'phone', proposal)
        w.feed.ack_read(w.grok, 'owner', 'phone', proposal)
        ctx = replace(w.context('shop-task'), native_call_id=None, native_request_id=None)
        questions = [{'title': 'The seller changed the basket; confirm the complete new basket.', 'options': None}]
        question = w.control.observe_native_question(ctx, item_id='changed-basket', message_id='basket-message',
                                                     questions=questions)
        with pytest.raises(OrderRefused, match='handoff_snapshot_changed'):
            w.feed.ack_handoff(w.grok, 'owner', 'phone', proposal,
                snapshot_digest=before['payload']['snapshot_digest'], next_check_at=1800)
        assert w.events.order(w.owner, 'owner', ref)['responsible'] == 'codex'
        assert w.feed.fetch(w.owner, 'owner', 'phone', question)['payload']['questions'] == questions
        # Reusing an original native question identity with changed content is not a new authorization.
        with pytest.raises(OrderRefused, match='notice_identity_conflict'):
            w.control.observe_native_question(ctx, item_id='changed-basket', message_id='basket-message',
                questions=[{'title': 'Invented approval replacement', 'options': None}])
    asyncio.run(scenario())


# spec: ANYM-997
@pytest.mark.parametrize('delivery_state', ['sending', 'unknown'])
@pytest.mark.parametrize('notice_kind', ['question', 'status'])
def test_task_notice_uncertain_delivery_blocks_order_handoff_without_replay(conversation, delivery_state, notice_kind):
    async def scenario():
        w = conversation
        ref = (await w.act('shop-task', 'bind', {'next_check_at': 1800, 'state': {}}))['obligation_ref']
        if notice_kind == 'question':
            ctx = replace(w.context('shop-task'), native_call_id=None, native_request_id=None)
            question = w.control.observe_native_question(ctx, item_id='pending-item', message_id='pending-message',
                questions=[{'title': 'Owner clarification', 'options': None}])
        else:
            question = (await w.act('shop-task', 'notify', {'message': 'Owner status before dispatch'}))['wake_id']
        assert w.events.claim(question)
        if delivery_state == 'unknown':
            w.events.recover()
        old = w.events.wake(question)
        for _ in range(2):  # a different control call cannot bypass the original uncertain delivery
            with pytest.raises(OrderRefused, match='order_delivery_unsettled'):
                await w.act('shop-task', 'handoff', {'obligation_ref': ref, 'target_principal': 'grok'})
        assert w.events.wake(question) == old
        assert w.events.db.execute("SELECT count(*) FROM android_order_wakes WHERE kind='handoff'").fetchone()[0] == 0
        assert w.events.order(w.owner, 'owner', ref)['next_check_at'] == 1800
    asyncio.run(scenario())


# spec: ANYM-997
def test_handoff_excludes_other_task_request_thread_and_other_bound_order_questions(conversation):
    async def scenario():
        w = conversation
        ref = (await w.act('shop-task', 'bind', {'next_check_at': 1800, 'state': {}}))['obligation_ref']
        base = replace(w.context('shop-task'), native_call_id=None, native_request_id=None)
        unrelated = [
            replace(base, task_id='health-task', request_id='health-task'),
            replace(base, request_id='another-request'),
            replace(base, native_thread_id='another-thread'),
            replace(base, device_id='tablet'),
        ]
        for i, ctx in enumerate(unrelated):
            w.control.observe_native_question(ctx, item_id=f'other-item-{i}', message_id=f'other-message-{i}',
                questions=[{'title': 'Unrelated owner question', 'options': None}])
        # A separate bound obligation from this same task is not silently treated as a task notice.
        question = w.control.observe_native_question(base, item_id='other-order-item', message_id='other-order-message',
            questions=[{'title': 'Question on a different bound obligation', 'options': None}])
        other = (await w.act('shop-task', 'bind', {
            'package': 'fixture.tokopedia', 'provider_order_id': 'other-order',
            'next_check_at': 1900, 'state': {}}))['obligation_ref']
        assert other != ref
        proposal = (await w.act('shop-task', 'handoff', {
            'obligation_ref': ref, 'target_principal': 'grok'}))['wake_id']
        snapshot = w.feed.fetch(w.grok, 'owner', 'phone', proposal)['payload']['snapshot']
        assert snapshot['questions'] == [] and snapshot['unsettled_delivery'] == []
        assert w.events.wake(question)['order_id'] == other
        assert w.events.order(w.owner, 'owner', other)['responsible'] == 'codex'
    asyncio.run(scenario())


# spec: ANYM-997
def test_cross_app_timer_fallback_and_unknown_wake_survive_restart_without_native_acceptance(conversation):
    async def scenario():
        w = conversation
        refs = []
        for task, package in [('health-task', 'fixture.halodoc'), ('shop-task', 'fixture.tokopedia')]:
            refs.append((await w.act(task, 'bind', {'package': package, 'next_check_at': 1100, 'state': {}}))['obligation_ref'])
        w.now[0] = 1101  # no push is required for the existing chosen timer
        supervisor = Supervision(SimpleNamespace(db=SimpleNamespace(db=w.events.db), order_control=lambda: w.control))
        await supervisor.tick()
        wakes = [w.events.wake(r[0]) for r in w.events.db.execute(
            "SELECT wake_id FROM android_order_wakes WHERE kind='observation'")]
        assert len(wakes) == 2 and all(x['hold_reason'] == 'lease_unavailable' for x in wakes)
        assert all(supervisor.receipt(x['wake_id']) is None for x in wakes)
        wake = wakes[0]
        assert w.events.claim(wake['wake_id'])
        w.events.db.close()
        events, control = w.open_store()
        try:
            events.recover()
            for _ in range(2):
                events.stage_due()
            assert events.wake(wake['wake_id'])['state'] == 'unknown'
            assert events.db.execute("SELECT count(*) FROM android_order_wakes WHERE kind='observation'").fetchone()[0] == 2
            assert all(events.order(w.owner, 'owner', ref)['next_check_at'] == 1100 for ref in refs)
        finally:
            events.db.close()
    asyncio.run(scenario())


# spec: ANYM-997
def test_untrusted_app_observation_never_establishes_participant_or_order_authority(conversation):
    async def scenario():
        w = conversation
        ref = (await w.act('health-task', 'bind', {'package': 'fixture.halodoc',
            'provider_order_id': 'health-order', 'source_order_id': 'health-chat',
            'next_check_at': 1100, 'state': {'doctor': {'participant_ref': 'doctor-fixture',
                                                       'evidence': 'consultation-view'}}}))['obligation_ref']
        observation = Notification('push-one', 'owner', 'phone', None, 'fixture.halodoc',
                                   'source', None, 1, 1000, 'opaque-app-update')
        assert w.events.observe(w.owner, observation) == 'recorded'
        assert w.events.observe(w.owner, observation) == 'duplicate'
        assert w.events.observations(w.owner, 'owner', ref)[0]['correlation'] == 'unresolved_app'
        assert w.events.order(w.owner, 'owner', ref)['state']['doctor']['participant_ref'] == 'doctor-fixture'
        with pytest.raises(OrderRefused, match='observation_binding_mismatch'):
            w.events.observe(w.owner, replace(observation, event_id='wrong-chat', order_id='health-order',
                                              source_order_id='stranger-claim'))
        with pytest.raises(OrderRefused, match='wake_not_owned'):
            w.feed.fetch(w.grok, 'owner', 'another-phone', w.events.pending()[0]['wake_id'])
        w.rights[0] = False
        with pytest.raises(OrderRefused, match='access_changed'):
            w.feed.read_source(w.owner, 'owner', 'phone', 0)
    asyncio.run(scenario())


# spec: ANYM-997
def test_unknown_task_question_after_proposal_changes_digest_and_prevents_acceptance(conversation):
    async def scenario():
        w = conversation
        ref = (await w.act('shop-task', 'bind', {'next_check_at': 1800, 'state': {}}))['obligation_ref']
        ctx = replace(w.context('shop-task'), native_call_id=None, native_request_id=None)
        question = w.control.observe_native_question(ctx, item_id='item', message_id='message',
            questions=[{'title': 'Owner clarification', 'options': None}])
        proposal = (await w.act('shop-task', 'handoff', {
            'obligation_ref': ref, 'target_principal': 'grok'}))['wake_id']
        before = w.feed.fetch(w.grok, 'owner', 'phone', proposal)
        w.feed.ack_read(w.grok, 'owner', 'phone', proposal)
        assert w.events.claim(question)
        w.events.recover()
        current = w.events._snapshot(w.events._order('owner', ref))
        assert current['questions'][0]['delivery_state'] == 'unknown'
        assert current['unsettled_delivery'][0]['wake_id'] == question
        assert current['unsettled_delivery'][0]['scope'] == 'task_notice'
        assert current['unsettled_delivery'][0]['order_id'] is None
        with pytest.raises(OrderRefused, match='handoff_snapshot_changed'):
            w.feed.ack_handoff(w.grok, 'owner', 'phone', proposal,
                snapshot_digest=before['payload']['snapshot_digest'], next_check_at=1800)
        assert w.events.order(w.owner, 'owner', ref)['responsible'] == 'codex'
        assert w.events.wake(question)['state'] == 'unknown'
        assert w.feed.fetch(w.grok, 'owner', 'phone', proposal) == before
    asyncio.run(scenario())
