"""Answered task-notice settlement through the real service, fake native and synthetic phone only.

Accepted native input settles only the question exchange; it is never goal, order or action success.
"""
import json
import sqlite3

import pytest

from agent_harness import Service
from test_native_dialogue import messages
from test_order_control_http import WORDS, order_host
from deedkit_android.order_events_control import OrderControl


def exported(service):
    feed = service.client.post('/internal/v1/order-source', headers={'Authorization': 'Bearer one'},
        json={'device_id': 'home', 'source_id': 'synthetic-configured-source', 'auth_epoch': 1, 'after': 0, 'limit': 4}).json()
    metadata, = feed['events']
    return metadata


def answer(service, lease, metadata, **changes):
    body = dict(action='reply', request_id=metadata['request_id'], device_id='home', session_id=lease,
                native_thread_id=metadata['native_thread_id'], native_turn_id=metadata['native_turn_id'],
                item_id=metadata['item_id'], message_id='owner-original-answer', text='  Home, exact natural words.  ')
    return body | changes


def read_receipt(service, wake_id):
    fetched = service.post('agent', {'action': 'fetch', 'wake_id': wake_id})
    return service.post('agent', {'action': 'read_receipt', 'wake_id': wake_id, 'wake_digest': fetched['wake_digest']})


def journal(tmp_path, wake_id):
    db = sqlite3.connect(tmp_path / 'state' / 'state.sqlite3')
    db.row_factory = sqlite3.Row
    try:
        wake = dict(db.execute('SELECT * FROM android_order_wakes WHERE wake_id=?', (wake_id,)).fetchone())
        row = dict(db.execute('SELECT * FROM android_order_obligations WHERE order_id=?', (wake['order_id'],)).fetchone())
        settled = db.execute('SELECT count(*) FROM android_order_answer_settlements').fetchone()[0]
        return wake, row, settled
    finally:
        db.close()


def dialogue(tmp_path):
    path, = (tmp_path / 'state' / 'agent' / 'sessions').glob('run-live-*.dialogue.json')
    return path


def turns(record, method):
    return len([r for r in messages(record) if r['message'].get('method') == method])


# spec: ANYM-997
def test_resumed_async_answer_settles_its_task_notice_after_the_native_start_receipt(tmp_path):
    config, record = order_host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        first = service.finished(service.start(lease, goal='async-closed', owner_instruction=WORDS)['request_id'])
        metadata = exported(service)
        service.post('agent', answer(service, lease, metadata))
        done = service.finished(first['request_id'])
        settlement, = [e for e in done['events'] if e['kind'] == 'order_settlement']
        assert settlement['status'] == 'settled' and settlement['goal_status'] == 'not_observed'
        assert done['dialogue'][0]['status'] == 'accepted' and done['dialogue'][0]['continuation_turn_id']
        late = read_receipt(service, metadata['wake_id'])  # Grok's later read is recorded; the exchange stays settled
        assert late['grok_state'] == 'read' and late['order_settlement']['status'] == 'settled'
        assert turns(record, 'turn/start') == 2 and turns(record, 'thread/start') == 1
    wake, row, settled = journal(tmp_path, metadata['wake_id'])
    assert wake['state'] == 'done' and row['lifecycle'] == 'settled' and settled == 1
    assert row['task_only'] == 1 and row['provider_order_id'] is None and row['original_request_id'] == first['request_id']


# spec: ANYM-997
def test_steered_async_answer_settles_in_the_same_reply(tmp_path):
    config, record = order_host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        request_id = service.start(lease, goal='async-open', owner_instruction=WORDS)['request_id']
        metadata = None
        while metadata is None:
            view = service.get(request_id, wait_s=1)
            if any(e['kind'] == 'question' for e in view['events']):
                metadata = exported(service)
        sent = service.post('agent', answer(service, lease, metadata))
        assert sent['input_receipt']['status'] == 'accepted'
        assert sent['order_settlement']['status'] == 'settled' and sent['order_settlement']['order_status'] == 'not_observed'
        service.finished(request_id)
        assert turns(record, 'turn/steer') == 1
    wake, row, _ = journal(tmp_path, metadata['wake_id'])
    assert wake['state'] == 'done' and row['lifecycle'] == 'settled'


# spec: ANYM-997
def test_failed_settlement_keeps_the_accepted_answer_and_reconciles_the_same_receipt_after_restart(tmp_path, monkeypatch):
    config, record = order_host(tmp_path)
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic journal failure')
    with monkeypatch.context() as patch:
        patch.setattr(OrderControl, 'settle_answered_question', broken)
        with Service(config) as service:
            lease = service.lease()
            first = service.finished(service.start(lease, goal='async-closed', owner_instruction=WORDS)['request_id'])
            metadata = exported(service)
            service.post('agent', answer(service, lease, metadata))
            done = service.finished(first['request_id'])
            held, = [e for e in done['events'] if e['kind'] == 'order_settlement']
            assert held == {'kind': 'order_settlement', **{k: held[k] for k in ('seq', 'at') if k in held},
                            'status': 'held', 'error': 'settlement_unavailable', 'effect': 'metadata_only'}
            assert done['dialogue'][0]['status'] == 'accepted' and done['state'] == 'succeeded'
    persisted = dialogue(tmp_path).read_text()
    assert journal(tmp_path, metadata['wake_id'])[0]['state'] == 'pending'
    with Service(config) as service:  # no live session: the durable dialogue row is the only proof
        reconciled = read_receipt(service, metadata['wake_id'])
        assert reconciled['order_settlement']['status'] == 'settled'
        assert read_receipt(service, metadata['wake_id']) == reconciled
    assert dialogue(tmp_path).read_text() == persisted
    assert turns(record, 'turn/start') == 2 and turns(record, 'turn/steer') == 0
    wake, row, settled = journal(tmp_path, metadata['wake_id'])
    assert wake['state'] == 'done' and row['lifecycle'] == 'settled' and settled == 1


# spec: ANYM-997
@pytest.mark.parametrize('tamper,error', [
    ('other_item', 'answer_receipt_missing'), ('unknown', 'answer_not_accepted'),
    ('duplicate', 'answer_correlation_ambiguous'), ('absent', 'answer_receipt_missing')])
def test_missing_uncertain_or_ambiguous_persisted_answer_stays_held(tmp_path, monkeypatch, tamper, error):
    config, _ = order_host(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(OrderControl, 'settle_answered_question', lambda *a, **k: (_ for _ in ()).throw(KeyError()))
        with Service(config) as service:
            lease = service.lease()
            first = service.finished(service.start(lease, goal='async-closed', owner_instruction=WORDS)['request_id'])
            metadata = exported(service)
            service.post('agent', answer(service, lease, metadata))
            service.finished(first['request_id'])
    path = dialogue(tmp_path)
    rows = json.loads(path.read_text())
    key, row = next((k, r) for k, r in rows.items() if r['kind'] == 'async_question')
    if tamper == 'other_item':
        row['item_id'] = 'other-item'
    elif tamper == 'unknown':
        row.update(state='unknown', status='unknown')
    elif tamper == 'duplicate':
        rows['synthetic-second-row'] = dict(row)
    path.write_text(json.dumps(rows))
    if tamper == 'absent':
        path.unlink()
    with Service(config) as service:
        held = read_receipt(service, metadata['wake_id'])['order_settlement']
        assert held == {'status': 'held', 'error': error, 'effect': 'metadata_only'}
    wake, row, settled = journal(tmp_path, metadata['wake_id'])
    assert wake['state'] == 'accepted' and row['lifecycle'] == 'active' and settled == 0


# spec: ANYM-997
def test_unanswered_question_read_receipt_records_read_and_holds_settlement(tmp_path):
    config, _ = order_host(tmp_path)
    with Service(config) as service:
        service.finished(service.start(service.lease(), goal='async-closed', owner_instruction=WORDS)['request_id'])
        metadata = exported(service)
        read = read_receipt(service, metadata['wake_id'])
        assert read['grok_state'] == 'read'
        assert read['order_settlement'] == {'status': 'held', 'error': 'answer_not_accepted', 'effect': 'metadata_only'}
    wake, row, settled = journal(tmp_path, metadata['wake_id'])
    assert wake['state'] == 'accepted' and row['lifecycle'] == 'active' and settled == 0


# spec: ANYM-997
def test_rpc_question_answer_and_read_never_settle(tmp_path):
    config, record = order_host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        request_id = service.start(lease, goal='question', owner_instruction=WORDS)['request_id']
        question = None
        while question is None:
            question = next((e for e in service.get(request_id, wait_s=1)['events'] if e['kind'] == 'question'), None)
        metadata = exported(service)
        assert metadata['native_request_id'] == 12
        assert 'order_settlement' not in read_receipt(service, metadata['wake_id'])
        body = {k: v for k, v in answer(service, lease, metadata).items() if k not in ('text', 'message_id')}
        sent = service.post('agent', body | {'native_request_id': 12, 'answers': {'address': ['Home'], 'choice': ['Shop']}})
        assert 'order_settlement' not in sent
        service.finished(request_id)
    wake, row, settled = journal(tmp_path, metadata['wake_id'])
    assert wake['state'] == 'accepted' and row['lifecycle'] == 'active' and settled == 0


# spec: ANYM-997
@pytest.mark.parametrize('field,value', [('native_turn_id', 'turn-other'), ('item_id', 'item-other')])
def test_refused_answer_admission_changes_neither_dialogue_nor_journal(tmp_path, field, value):
    config, record = order_host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        first = service.finished(service.start(lease, goal='async-closed', owner_instruction=WORDS)['request_id'])
        metadata = exported(service)
        before = dialogue(tmp_path).read_text()
        service.post('agent', answer(service, lease, metadata, **{field: value}), code=409)
        assert service.get(first['request_id'])['state'] == 'succeeded'
        assert dialogue(tmp_path).read_text() == before
    assert turns(record, 'turn/start') == 1
    wake, row, settled = journal(tmp_path, metadata['wake_id'])
    assert wake['state'] == 'pending' and row['lifecycle'] == 'active' and settled == 0
