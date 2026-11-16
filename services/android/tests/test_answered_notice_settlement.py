"""Answered task-notice settlement: real journals, synthetic identities, the host's own receipt shape.

The receipt comes from the real CodexSession.receipt() over interaction rows shaped exactly as
reply_async/reply write them. No model, phone, provider or send runs here.
"""
import asyncio
from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace

import pytest

from deedkit_android.codex_session import CodexSession
from deedkit_android.order_events import OrderEvents, OrderRefused
from deedkit_android.order_events_control import NativeOrderContext, OrderControl

TASK, THREAD, TURN, ITEM = 'task-original', 'thread-original', 'turn-original', 'item-async-question'
QUESTIONS = [{'title': 'Which delivery address should the seller use?', 'options': None}]


@pytest.fixture
def notice(tmp_path):
    rights, path = [True], tmp_path / 'notice.sqlite'
    owner, grok = SimpleNamespace(id='owner'), SimpleNamespace(id='grok')

    def authorize(principal, target, device, order, source, epoch):
        return (rights[0] and getattr(principal, 'id', principal) in ('owner', 'grok') and target == 'owner'
                and device in ('phone', 'tablet') and source == 'source' and epoch == 1)

    def open_store(epoch=1):
        events = OrderEvents(sqlite3.connect(path), authorize, clock=lambda: 1000.0)
        return events, OrderControl(events, source_id='source', auth_epoch=epoch,
                                    original_words=lambda ctx: 'Order the groceries and ask me what you must.')

    events, control = open_store()

    def context(turn=TURN, task=TASK, thread=THREAD):
        return NativeOrderContext(owner, 'owner', 'phone', task, task, thread, turn, None, None,
                                  native_namespace='registered-native')

    def ask(turn=TURN, item=ITEM, **kwargs):
        return control.observe_native_question(context(turn, **kwargs), item_id=item, message_id=None, questions=QUESTIONS)

    def answer(status='accepted', **changes):
        # Exactly the dialogue row reply_async keeps: observed item, committed digest/message, then native receipt.
        row = {'kind': 'async_question', 'original_request_id': TASK, 'native_thread_id': THREAD,
               'native_turn_id': TURN, 'item_id': ITEM, 'question_count': 1, 'state': 'waiting'}
        if status != 'waiting':
            row.update(digest='0' * 64, message_id='owner-answer-message', state=status, status=status)
        if status == 'accepted':
            row['continuation_turn_id'] = 'turn-continuation'
        return CodexSession.receipt(row | changes)

    yield SimpleNamespace(events=events, control=control, owner=owner, grok=grok, rights=rights,
                          context=context, ask=ask, answer=answer, open_store=open_store)
    events.db.close()


def journal(events, wake_id):
    wake = dict(events.db.execute('SELECT * FROM android_order_wakes WHERE wake_id=?', (wake_id,)).fetchone())
    row = events._order('owner', wake['order_id'])
    return wake, row


# spec: ANYM-997
def test_exact_accepted_async_answer_settles_the_stranded_task_notice_without_new_ids(notice):
    n = notice
    wake_id = n.ask()
    n.control.feed.read_source(n.owner, 'owner', 'phone', 0)  # exported; no read receipt was ever recorded
    before_wake, before = journal(n.events, wake_id)
    exported = n.events.db.execute('SELECT * FROM android_order_feed WHERE wake_id=?', (wake_id,)).fetchone()
    assert before_wake['state'] == 'pending' and before['lifecycle'] == 'active' and before['task_only'] == 1
    assert before['provider_order_id'] is None and before['responsible'] == 'codex' and before['revision'] == 1

    result = n.control.settle_answered_question(n.owner, 'owner', 'phone', wake_id, n.answer())

    wake, row = journal(n.events, wake_id)
    assert result == dict(wake_id=wake_id, obligation_ref=before['order_id'], delivery_state='done',
                          answer='native_input_accepted', goal_status='not_observed', order_status='not_observed',
                          lifecycle='settled', revision=3)
    assert wake == before_wake | {'state': 'done'}  # original IDs, payload, principal and turn preserved
    assert row == before | {'lifecycle': 'settled', 'revision': 3}  # same task/source/epoch/words/origin
    assert n.events.db.execute('SELECT * FROM android_order_feed WHERE wake_id=?', (wake_id,)).fetchone() == exported
    stored = n.events.db.execute('SELECT * FROM android_order_answer_settlements').fetchall()
    assert len(stored) == 1 and json.loads(stored[0]['receipt'])['message_id'] == 'owner-answer-message'
    assert n.events.db.execute('SELECT count(*) FROM android_order_wakes').fetchone()[0] == 1
    # A duplicate of the same receipt is a read; a different answer for the same question is a conflict.
    assert n.control.settle_answered_question(n.owner, 'owner', 'phone', wake_id, n.answer()) == result
    with pytest.raises(OrderRefused, match='settlement_identity_conflict'):
        n.control.settle_answered_question(n.owner, 'owner', 'phone', wake_id, n.answer(message_id='another'))
    assert journal(n.events, wake_id) == (wake, row)
    with pytest.raises(OrderRefused, match='receipt_not_current'):
        n.control.feed.ack_read(n.owner, 'owner', 'phone', wake_id)
    assert n.control.feed.read_source(n.owner, 'owner', 'phone', 1)['events'] == []
    n.events.db.close()
    events, control = n.open_store()  # restart keeps the settlement and its idempotent readback
    assert control.settle_answered_question(n.owner, 'owner', 'phone', wake_id, n.answer()) == result
    n.events = events


# spec: ANYM-997
def test_steered_answer_after_read_receipt_settles_but_other_unread_notice_keeps_record_open(notice):
    async def scenario():
        n = notice
        wake_id = n.ask()
        n.control.feed.ack_read(n.owner, 'owner', 'phone', wake_id)
        ctx = replace(n.context(), native_call_id='call-status', native_request_id=1)
        status = (await n.control(ctx, 'notify', {'message': 'Seller has not answered yet.'}))['wake_id']
        steered = {k: v for k, v in n.answer().items() if k != 'continuation_turn_id'}
        result = n.control.settle_answered_question(n.owner, 'owner', 'phone', wake_id, steered)
        assert result['delivery_state'] == 'done' and result['lifecycle'] == 'active'
        assert journal(n.events, status)[0]['state'] == 'pending'
        n.control.feed.ack_read(n.owner, 'owner', 'phone', status)  # the last outstanding delivery is now read
        assert journal(n.events, status)[1]['lifecycle'] == 'settled'
    asyncio.run(scenario())


# spec: ANYM-997
@pytest.mark.parametrize('status', ['waiting', 'unknown', 'refused', 'expired'])
def test_pending_unknown_or_refused_answer_keeps_the_question_held(notice, status):
    n = notice
    wake_id = n.ask()
    before = journal(n.events, wake_id)
    with pytest.raises(OrderRefused, match='answer_not_accepted'):
        n.control.settle_answered_question(n.owner, 'owner', 'phone', wake_id, n.answer(status))
    assert journal(n.events, wake_id) == before
    assert n.events.db.execute('SELECT count(*) FROM android_order_answer_settlements').fetchone()[0] == 0


# spec: ANYM-997
@pytest.mark.parametrize('delivery', ['sending', 'unknown', 'cancelled'])
def test_uncertain_or_closed_question_delivery_is_never_settled(notice, delivery):
    n = notice
    wake_id = n.ask()
    n.events.db.execute('UPDATE android_order_wakes SET state=? WHERE wake_id=?', (delivery, wake_id))
    n.events.db.commit()
    before = journal(n.events, wake_id)
    with pytest.raises(OrderRefused, match='settlement_delivery_unsettled'):
        n.control.settle_answered_question(n.owner, 'owner', 'phone', wake_id, n.answer())
    assert journal(n.events, wake_id) == before


# spec: ANYM-997
def test_rpc_question_and_rpc_receipt_stay_held(notice):
    n = notice
    ctx = replace(n.context(), native_request_id=7)
    rpc = n.control.observe_native_question(ctx, item_id='item-rpc', message_id=None, native_request_id=7,
        questions=[{'id': 'q1', 'question': 'Which address?'}], question_ids=['q1'])
    submitted = CodexSession.receipt({'kind': 'question', 'original_request_id': TASK, 'native_thread_id': THREAD,
        'native_turn_id': TURN, 'native_request_id': 7, 'item_id': 'item-rpc', 'question_ids': ['q1'],
        'digest': '0' * 64, 'state': 'accepted', 'status': 'accepted'})
    before = journal(n.events, rpc)
    for wake_id, receipt in ((rpc, submitted), (rpc, n.answer(item_id='item-rpc')), (n.ask(), submitted)):
        with pytest.raises(OrderRefused, match='settlement_not_async'):
            n.control.settle_answered_question(n.owner, 'owner', 'phone', wake_id, receipt)
    assert journal(n.events, rpc) == before


# spec: ANYM-997
def test_question_of_a_real_bound_order_stays_held(notice):
    async def scenario():
        n = notice
        wake_id = n.ask()
        ctx = replace(n.context(), native_call_id='call-bind', native_request_id=2)
        await n.control(ctx, 'bind', {'package': 'fixture.shop', 'next_check_at': 2000})
        before = journal(n.events, wake_id)
        assert before[1]['task_only'] == 0
        with pytest.raises(OrderRefused, match='settlement_not_task_notice'):
            n.control.settle_answered_question(n.owner, 'owner', 'phone', wake_id, n.answer())
        assert journal(n.events, wake_id) == before
    asyncio.run(scenario())


# spec: ANYM-997
@pytest.mark.parametrize('field,value', [
    ('original_request_id', 'task-other'), ('native_thread_id', 'thread-other'),
    ('native_turn_id', 'turn-continuation'), ('item_id', 'item-other')])
def test_stale_or_foreign_answer_identity_is_refused(notice, field, value):
    n = notice
    wake_id = n.ask()
    before = journal(n.events, wake_id)
    with pytest.raises(OrderRefused, match='settlement_identity_mismatch'):
        n.control.settle_answered_question(n.owner, 'owner', 'phone', wake_id, n.answer(**{field: value}))
    assert journal(n.events, wake_id) == before


# spec: ANYM-997
def test_foreign_principal_device_revoked_rights_and_changed_epoch_are_refused(notice):
    n = notice
    wake_id = n.ask()
    before = journal(n.events, wake_id)
    attempts = [(n.control, n.grok, 'owner', 'phone', 'settlement_not_owned'),
                (n.control, n.owner, 'owner', 'tablet', 'settlement_not_owned')]
    events, changed = n.open_store(epoch=2)
    attempts.append((changed, n.owner, 'owner', 'phone', 'access_changed|settlement_source_changed'))
    for control, principal, owner, device, error in attempts:
        with pytest.raises(OrderRefused, match=error):
            control.settle_answered_question(principal, owner, device, wake_id, n.answer())
    events.db.close()
    n.rights[0] = False
    with pytest.raises(OrderRefused, match='access_changed'):
        n.control.settle_answered_question(n.owner, 'owner', 'phone', wake_id, n.answer())
    assert journal(n.events, wake_id) == before


# spec: ANYM-997
def test_same_task_question_or_bind_after_settlement_reopens_the_original_record(notice):
    async def scenario():
        n = notice
        first = n.ask()
        n.control.settle_answered_question(n.owner, 'owner', 'phone', first, n.answer())
        ref = journal(n.events, first)[1]['order_id']
        later = n.ask(turn='turn-continuation', item='item-later')
        wake, row = journal(n.events, later)
        assert row['order_id'] == ref and row['lifecycle'] == 'active' and wake['state'] == 'pending'
        assert journal(n.events, first)[0]['state'] == 'done'
        ctx = replace(n.context('turn-continuation'), native_call_id='call-bind', native_request_id=3)
        assert (await n.control(ctx, 'bind', {'next_check_at': 2000}))['obligation_ref'] == ref
        assert journal(n.events, later)[1]['task_only'] == 0
    asyncio.run(scenario())
