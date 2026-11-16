"""Restart reconciliation of an exactly answered async task-notice question, fake native and synthetic phone only.

The stranded state: the owner's async answer was natively accepted and persisted in the dialogue journal, but the
journal settlement never ran (older build or a failed settlement). A service restart settles only that question
exchange from the persisted row, with no caller, read receipt, native dispatch, replay or new identity.
"""
import json
import sqlite3

import pytest

from agent_harness import Service
from test_answered_notice_agent_path import answer, dialogue, exported, journal, turns
from test_order_control_http import WORDS, order_host
from deedkit_android.order_events_control import OrderControl


def stranded(tmp_path, monkeypatch, goal='async-closed'):
    """One answered question whose accepted answer never reached the journal: the installed DK-49 shape."""
    config, record = order_host(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(OrderControl, 'settle_answered_question',
                      lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError('synthetic old build')))
        with Service(config) as service:
            lease = service.lease()
            request_id = service.start(lease, goal=goal, owner_instruction=WORDS)['request_id']
            if goal == 'question':
                while not any(e['kind'] == 'question' for e in service.get(request_id, wait_s=1)['events']):
                    pass
                metadata = exported(service)
                body = {k: v for k, v in answer(service, lease, metadata).items() if k not in ('text', 'message_id')}
                service.post('agent', body | {'native_request_id': 12, 'answers': {'address': ['Home'], 'choice': ['Shop']}})
            else:
                service.finished(request_id)
                metadata = exported(service)
                service.post('agent', answer(service, lease, metadata))
            service.finished(request_id)
    wake, row, settled = journal(tmp_path, metadata['wake_id'])
    assert wake['state'] == 'pending' and row['lifecycle'] == 'active' and settled == 0
    return config, record, metadata, request_id


def counts(tmp_path):
    db = sqlite3.connect(tmp_path / 'state' / 'state.sqlite3')
    try:
        return {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in (
            'android_order_wakes', 'android_order_feed', 'android_order_controls', 'android_order_obligations',
            'android_order_feed_receipts')}
    finally:
        db.close()


def settlements(service, request_id):
    return [e for e in service.get(request_id)['events'] if e['kind'] == 'order_settlement' and e['status'] == 'settled']


def settled_events(tmp_path):
    db = sqlite3.connect(tmp_path / 'state' / 'state.sqlite3')
    try:
        return [json.loads(b) for b, in db.execute("SELECT body FROM agent_events WHERE kind='order_settlement'")
                if json.loads(b)['status'] == 'settled']
    finally:
        db.close()


# spec: ANYM-997
def test_restart_settles_the_stranded_answered_notice_once_without_a_caller_or_native_dispatch(tmp_path, monkeypatch):
    config, record, metadata, request_id = stranded(tmp_path, monkeypatch)
    persisted, wire, before = dialogue(tmp_path).read_text(), record.read_text(), counts(tmp_path)
    with Service(config) as service:  # no read_receipt, fetch, reply or lease: boot alone
        settled, = settlements(service, request_id)
        assert settled['wake_id'] == metadata['wake_id'] and settled['answer'] == 'native_input_accepted'
        assert settled['goal_status'] == 'not_observed' and settled['order_status'] == 'not_observed'
        assert settled['reconciled'] == 'restart'
    wake, row, count = journal(tmp_path, metadata['wake_id'])
    assert wake['state'] == 'done' and row['lifecycle'] == 'settled' and count == 1
    assert row['task_only'] == 1 and row['provider_order_id'] is None and row['original_request_id'] == request_id
    with Service(config) as service:  # a second restart reads the same settlement back, never again
        assert len(settlements(service, request_id)) == 1
    assert journal(tmp_path, metadata['wake_id'])[2] == 1
    assert dialogue(tmp_path).read_text() == persisted and record.read_text() == wire
    assert counts(tmp_path) == before
    assert turns(record, 'turn/start') == 2 and turns(record, 'turn/steer') == 0 and turns(record, 'thread/start') == 1


def tamper(tmp_path, config, case):
    path = dialogue(tmp_path)
    rows = json.loads(path.read_text())
    key, row = next((k, r) for k, r in rows.items() if r['kind'] == 'async_question')
    body = json.loads(config.read_text())
    db = sqlite3.connect(tmp_path / 'state' / 'state.sqlite3')
    try:
        if case == 'revoked':
            owner, = [p for p in body['principals'] if p['id'] == 'one']
            owner['permissions'] = ['android:read']
        elif case == 'auth_epoch':
            body['agent']['order_events']['auth_epoch'] = 2
        elif case == 'device_removed':
            owner, = [p for p in body['principals'] if p['id'] == 'one']
            owner['devices'] = []
        elif case == 'wrong_binding':
            row['native_thread_id'] = 'thread-other'
        elif case == 'duplicate':
            rows['synthetic-second-row'] = dict(row)
        elif case == 'unknown':
            row.update(state='unknown', status='unknown')
        elif case == 'expired':
            for name in ('digest', 'message_id', 'status', 'continuation_turn_id'):
                row.pop(name, None)
            row['state'] = 'expired'
        elif case == 'real_order':
            db.execute("UPDATE android_order_obligations SET task_only=0,provider_order_id='provider-synthetic'")
        elif case == 'foreign':
            db.execute("UPDATE android_order_wakes SET principal='reader' WHERE kind='question'")
        db.commit()
    finally:
        db.close()
    config.write_text(json.dumps(body))
    path.write_text(json.dumps(rows))
    if case == 'missing':
        path.unlink()


# spec: ANYM-997
@pytest.mark.parametrize('case', ['revoked', 'auth_epoch', 'device_removed', 'wrong_binding', 'duplicate', 'missing',
                                  'unknown', 'expired', 'real_order', 'foreign'])
def test_restart_holds_an_uncertain_foreign_or_changed_answered_notice(tmp_path, monkeypatch, case):
    config, record, metadata, request_id = stranded(tmp_path, monkeypatch)
    tamper(tmp_path, config, case)
    persisted = None if case == 'missing' else dialogue(tmp_path).read_text()
    wire, before = record.read_text(), counts(tmp_path)
    with Service(config):
        pass
    assert settled_events(tmp_path) == []
    wake, row, settled = journal(tmp_path, metadata['wake_id'])
    assert wake['state'] == 'pending' and row['lifecycle'] == 'active' and settled == 0
    assert counts(tmp_path) == before and record.read_text() == wire
    assert persisted is None or dialogue(tmp_path).read_text() == persisted


# spec: ANYM-997
def test_restart_never_settles_an_rpc_question_answer(tmp_path, monkeypatch):
    config, record, metadata, request_id = stranded(tmp_path, monkeypatch, goal='question')
    assert metadata['native_request_id'] == 12
    wire = record.read_text()
    with Service(config) as service:
        assert settlements(service, request_id) == []
    wake, row, settled = journal(tmp_path, metadata['wake_id'])
    assert wake['state'] == 'pending' and row['lifecycle'] == 'active' and settled == 0
    assert record.read_text() == wire
