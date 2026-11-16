"""Source view of the immutable owner feed: retired rows never strand later eligible wakes.

Synthetic SQLite and contexts only; no native, device, network, gateway or delivery.
"""
import asyncio
from dataclasses import replace
from datetime import datetime
import json
import sqlite3
from types import SimpleNamespace

import pytest

from deedkit_android.codex_session import CodexSession
from deedkit_android.order_events import OrderEvents, OrderRefused
from deedkit_android.order_events_control import NativeOrderContext, OrderControl

LIFETIME = 3600


@pytest.fixture
def world(tmp_path):
    now, access, epoch = [1000.0], [True], [1]
    owner = SimpleNamespace(id='owner')
    path = tmp_path / 'feed.sqlite'

    def authorize(p, target, device, order, source, auth_epoch):
        return (access[0] and getattr(p, 'id', p) in ('owner', 'grok') and target == 'owner' and device == 'phone'
                and source == 'usb' and auth_epoch == epoch[0])

    def open_store():
        events = OrderEvents(sqlite3.connect(path), authorize, clock=lambda: now[0])
        return events, OrderControl(events, source_id='usb', auth_epoch=1,
                                    original_words=lambda ctx: 'Original owner words.')

    events, control = open_store()
    context = NativeOrderContext(owner, 'owner', 'phone', 'task-one', 'task-one', 'thread', 'turn-one',
                                 'call-one', 1, native_namespace='registered-native')
    state = SimpleNamespace(events=events, control=control, owner=owner, now=now, access=access, epoch=epoch,
                            context=context, open_store=open_store)
    yield state
    state.events.db.close()


def question(w, item='question-item', turn='turn-one'):
    ctx = replace(w.context, native_turn_id=turn, native_call_id=None, native_request_id=None)
    return w.control.observe_native_question(ctx, item_id=item, message_id=None,
                                             questions=[{'title': 'Which address?', 'options': None}])


def status(w, n):
    ctx = replace(w.context, native_call_id='status-%d' % n, native_request_id=100 + n)
    return asyncio.run(w.control(ctx, 'notify', {'message': 'Native status %d' % n}))['wake_id']


def read(w, after=0, limit=4):
    return w.control.feed.read_source(w.owner, 'owner', 'phone', after, limit)


def feed_rows(w):
    return [tuple(r) for r in w.events.db.execute('SELECT * FROM android_order_feed ORDER BY sequence')]


# spec: ANYM-997
def test_expired_exported_question_does_not_hide_a_later_fresh_status_and_keeps_the_immutable_row(world):
    old = question(world)
    first = read(world)
    assert [e['wake_id'] for e in first['events']] == [old]
    exported = feed_rows(world)
    world.now[0] = 1000.0 + LIFETIME + 5
    fresh = status(world, 1)
    page = read(world)
    # The cursor and journal stay the server's: only the eligible later row crosses, at its original sequence.
    assert page['journal_id'] == first['journal_id'] and page['after'] == 0 and page['gap'] is False
    assert [(e['wake_id'], e['sequence']) for e in page['events']] == [(fresh, 2)]
    assert page['next_after'] == 2
    assert feed_rows(world)[:1] == exported  # no purge, renewed expiry or new identity for the old row
    assert world.events.wake(old)['state'] == 'pending'  # a view, never a forced status


# spec: ANYM-997
def test_only_retired_rows_return_an_empty_page_at_the_unchanged_cursor(world):
    question(world)
    question(world, item='second-item', turn='turn-two')
    read(world)
    world.now[0] = 1000.0 + LIFETIME + 1
    page = read(world)
    assert page['events'] == [] and page['after'] == 0 and page['next_after'] == 0 and page['gap'] is False


# spec: ANYM-997
def test_exact_expiry_instant_is_retired_like_the_server_and_one_second_before_is_not(world):
    wake = question(world)
    read(world)
    until = json.loads(world.events.db.execute('SELECT metadata FROM android_order_feed').fetchone()[0])['expires_at']
    world.now[0] = datetime.fromisoformat(until).timestamp() - 1
    assert [e['wake_id'] for e in read(world)['events']] == [wake]
    world.now[0] = datetime.fromisoformat(until).timestamp()
    assert read(world)['events'] == []


# spec: ANYM-997
def test_settled_question_is_retired_before_expiry_without_changing_its_journal(world):
    wake = question(world)
    read(world)
    receipt = CodexSession.receipt({'kind': 'async_question', 'original_request_id': 'task-one',
        'native_thread_id': 'thread', 'native_turn_id': 'turn-one', 'item_id': 'question-item', 'question_count': 1,
        'digest': '0' * 64, 'message_id': 'owner-answer', 'state': 'accepted', 'status': 'accepted',
        'continuation_turn_id': 'turn-continuation'})
    assert world.control.settle_answered_question(world.owner, 'owner', 'phone', wake, receipt)['delivery_state'] == 'done'
    exported = feed_rows(world)
    fresh = status(world, 1)
    assert [e['wake_id'] for e in read(world)['events']] == [fresh]
    assert feed_rows(world)[:1] == exported


# spec: ANYM-997
def test_page_limit_skips_many_retired_rows_and_pages_sparse_eligible_sequences(world):
    for n in range(6):
        question(world, item='old-%d' % n, turn='old-turn-%d' % n)
    read(world, limit=16)
    world.now[0] = 1000.0 + LIFETIME + 1
    fresh = [status(world, n) for n in range(5)]
    first = read(world)
    assert [e['wake_id'] for e in first['events']] == fresh[:4]
    assert [e['sequence'] for e in first['events']] == [7, 8, 9, 10] and first['next_after'] == 10
    second = read(world, first['next_after'])
    assert [e['wake_id'] for e in second['events']] == fresh[4:] and second['next_after'] == 11
    assert read(world, 11) == dict(journal_id=first['journal_id'], after=11, next_after=11, events=[], gap=False)
    # The same cursor reads the same page again: no hidden source cursor or consumption.
    assert read(world) == first


# spec: ANYM-997
def test_restart_reads_the_same_view_of_retired_and_eligible_rows(world):
    question(world)
    read(world)
    world.now[0] = 1000.0 + LIFETIME + 1
    status(world, 1)
    before = read(world)
    world.events.db.close()
    world.events, world.control = world.open_store()
    assert read(world) == before


# spec: ANYM-997
@pytest.mark.parametrize('field,value', [('expires_at', 'not-a-time'), ('expires_at', '2026-11-16T10:00:00'),
                                         ('expires_at', None), ('source_id', 'other-source'), ('auth_epoch', 2),
                                         ('owner_id', 'other-owner'), ('device_id', 'other-phone')])
def test_malformed_or_mismatched_retired_row_fails_closed_without_raw_diagnostics(world, field, value):
    wake = question(world)
    read(world)
    world.now[0] = 1000.0 + LIFETIME + 1
    status(world, 1)
    metadata = json.loads(world.events.db.execute('SELECT metadata FROM android_order_feed WHERE wake_id=?',
                                                  (wake,)).fetchone()[0])
    world.events.db.execute('UPDATE android_order_feed SET metadata=? WHERE wake_id=?',
                            (json.dumps(metadata | {field: value}), wake))
    world.events.db.commit()
    with pytest.raises(OrderRefused) as refused:
        read(world)
    assert str(refused.value) in ('invalid_feed_metadata', 'access_changed')
    assert 'not-a-time' not in str(refused.value) and 'other' not in str(refused.value)


# spec: ANYM-997
def test_unparseable_metadata_document_fails_closed_without_raw_diagnostics(world):
    wake = question(world)
    read(world)
    world.events.db.execute("UPDATE android_order_feed SET metadata='{broken' WHERE wake_id=?", (wake,))
    world.events.db.commit()
    with pytest.raises(OrderRefused, match='^invalid_feed_metadata$'):
        read(world)


# spec: ANYM-997
def test_revoked_or_changed_epoch_never_turns_a_retired_row_into_skippable_authority(world):
    question(world)
    read(world)
    world.now[0] = 1000.0 + LIFETIME + 1
    status(world, 1)
    world.access[0] = False
    with pytest.raises(OrderRefused, match='access_changed'):
        read(world)
    world.access[0], world.epoch[0] = True, 2
    with pytest.raises(OrderRefused, match='access_changed'):
        read(world)
    with pytest.raises(OrderRefused, match='invalid_feed_cursor'):
        world.control.feed.read_source(SimpleNamespace(id='grok'), 'owner', 'phone', 0, 4)


# spec: ANYM-997
def test_unknown_delivery_is_not_retired_by_the_source_view(world):
    wake = status(world, 1)
    read(world)
    world.events.db.execute("UPDATE android_order_wakes SET state='unknown',hold_reason='delivery_unknown' WHERE wake_id=?",
                            (wake,))
    world.events.db.commit()
    assert [e['wake_id'] for e in read(world)['events']] == [wake]
