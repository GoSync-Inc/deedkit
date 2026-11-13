"""Native control and authenticated owner event routes through the real service, synthetic phone only."""

import json
import uuid

from agent_harness import Service
from test_native_order_control import controlled
from test_native_dialogue import messages

WORDS = '  Exact owner words.\nKeep the basket restriction; ask before selecting.  '


def order_host(tmp_path):
    _, record = controlled(tmp_path)
    config = tmp_path / 'config.json'
    body = json.loads(config.read_text())
    body['agent'].update(guarded=False, persistent=True,
                         order_events={'source_id': 'synthetic-configured-source', 'auth_epoch': 1})
    config.write_text(json.dumps(body))
    return config, record


# spec: ANYM-993, ANYM-997
def test_native_notify_reaches_configured_journal_and_fetch_returns_exact_original_task(tmp_path):
    config, record = order_host(tmp_path)
    with Service(config) as service:
        request = service.start(service.lease(), goal='native-control', owner_instruction=WORDS,
                                params={'COUNT': 2, 'CONFIRMED': False})
        result = service.finished(request['request_id'])
        receipt, = result['dialogue']
        assert receipt['operation_status'] == 'applied' and receipt['status'] == 'unknown'
        feed = service.client.post('/internal/v1/order-source', headers={'Authorization': 'Bearer one'},
            json={'device_id': 'home', 'source_id': 'synthetic-configured-source', 'auth_epoch': 1, 'after': 0, 'limit': 4})
        assert feed.status_code == 200, feed.text
        metadata, = feed.json()['events']
        assert metadata['order_id'] is None and metadata['request_id'] == request['request_id']
        fetched = service.post('agent', {'action': 'fetch', 'wake_id': metadata['wake_id']})
        original = fetched['original_request']
        assert original['request_id'] == request['request_id'] and original['owner_instruction'] == WORDS
        assert original['goal'] == 'native-control' and original['params'] == {'COUNT': 2, 'CONFIRMED': False}
        assert fetched['metadata']['native_request_id'] == 42
        assert fetched['metadata']['native_call_id'] == 'real-call'
        assert fetched['read_receipt']['state'] == 'not_recorded'
        assert fetched['responsibility']['responsible'] == 'codex'
        assert len(fetched['wake_digest']) == 64
        again = service.post('agent', {'action': 'fetch', 'wake_id': metadata['wake_id']})
        assert again == fetched
        assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 1


# spec: ANYM-993, ANYM-997
def test_local_read_receipt_is_explicit_idempotent_metadata_and_never_handoff_or_native_input(tmp_path):
    config, record = order_host(tmp_path)
    with Service(config) as service:
        run = service.finished(service.start(service.lease(), goal='native-control', owner_instruction=WORDS)['request_id'])
        feed = service.client.post('/internal/v1/order-source', headers={'Authorization': 'Bearer one'},
            json={'device_id': 'home', 'source_id': 'synthetic-configured-source', 'auth_epoch': 1, 'after': 0, 'limit': 4}).json()
        wake = feed['events'][0]['wake_id']
        fetched = service.post('agent', {'action': 'fetch', 'wake_id': wake})
        body = {'action': 'read_receipt', 'wake_id': wake, 'wake_digest': fetched['wake_digest']}
        first = service.post('agent', body)
        assert first == service.post('agent', body)
        after = service.post('agent', {'action': 'fetch', 'wake_id': wake})
        assert after['wake_digest'] == fetched['wake_digest'] and after['read_receipt']['state'] == 'recorded'
        assert after['responsibility']['responsible'] == 'codex'
        assert first['native_status'] == 'not_observed' and run['state'] == 'succeeded'
        assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 1


# spec: ANYM-993, ANYM-997
def test_foreign_principal_cannot_fetch_or_record_receipt_for_original_owner(tmp_path):
    config, _ = order_host(tmp_path)
    with Service(config) as service:
        service.finished(service.start(service.lease(), goal='native-control')['request_id'])
        feed = service.client.post('/internal/v1/order-source', headers={'Authorization': 'Bearer one'},
            json={'device_id': 'home', 'source_id': 'synthetic-configured-source', 'auth_epoch': 1, 'after': 0, 'limit': 4}).json()
        wake = feed['events'][0]['wake_id']
        fetched = service.post('agent', {'action': 'fetch', 'wake_id': wake})
        service.post('agent', {'action': 'fetch', 'wake_id': wake}, key='two', code=404)
        service.post('agent', {'action': 'read_receipt', 'wake_id': wake,
                              'wake_digest': fetched['wake_digest']}, key='two', code=404)
        unchanged = service.post('agent', {'action': 'fetch', 'wake_id': wake})
        assert unchanged == fetched


# spec: ANYM-993, ANYM-997
def test_fetch_uses_wake_identity_and_refuses_invented_external_request_or_device(tmp_path):
    from deedkit_android.agent_run import AgentCall
    from pydantic import ValidationError
    import pytest
    wake = str(uuid.uuid4())
    assert AgentCall.model_validate({'action': 'fetch', 'wake_id': wake}).request_id is None
    for extra in ({'request_id': wake}, {'device_id': 'foreign'}, {'snapshot_digest': 'a' * 64}):
        with pytest.raises(ValidationError):
            AgentCall.model_validate({'action': 'fetch', 'wake_id': wake, **extra})


# spec: ANYM-993, ANYM-997
def test_async_preorder_question_wake_retains_array_without_fabricated_rpc_or_question_ids(tmp_path):
    config, record = order_host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        result = service.finished(service.start(lease, goal='async-closed', owner_instruction=WORDS)['request_id'])
        feed = service.client.post('/internal/v1/order-source', headers={'Authorization': 'Bearer one'},
            json={'device_id': 'home', 'source_id': 'synthetic-configured-source', 'auth_epoch': 1, 'after': 0, 'limit': 4}).json()
        metadata, = feed['events']
        assert metadata['native_request_id'] is metadata['native_call_id'] is metadata['order_id'] is None
        assert metadata['question_ids'] == [] and metadata['item_id'] == 'async-item'
        fetched = service.post('agent', {'action': 'fetch', 'wake_id': metadata['wake_id']})
        assert fetched['payload']['questions'] == [{'title': 'Which address?', 'options': ['Home', 'Office']}]
        assert fetched['original_request']['owner_instruction'] == WORDS
        reply = dict(action='reply', request_id=result['request_id'], device_id='home', session_id=lease,
                     native_thread_id=metadata['native_thread_id'], native_turn_id=metadata['native_turn_id'],
                     item_id=metadata['item_id'], message_id='owner-original-answer', text='  Home, keep exact words.  ')
        sent = service.post('agent', reply)
        assert sent['input_receipt']['status'] in ('pending', 'accepted', 'unknown')
        finished = service.finished(result['request_id'])
        assert finished['native_session']['thread_id'] == result['native_session']['thread_id']
        starts = [r for r in messages(record) if r['message'].get('method') == 'thread/start']
        assert len(starts) == 1 and len({r['pid'] for r in messages(record)}) == 1


# spec: ANYM-993, ANYM-997
def test_changed_wake_digest_and_source_epoch_refuse_metadata_mutation(tmp_path):
    config, _ = order_host(tmp_path)
    with Service(config) as service:
        service.finished(service.start(service.lease(), goal='native-control')['request_id'])
        feed_body = {'device_id': 'home', 'source_id': 'synthetic-configured-source', 'auth_epoch': 1, 'after': 0, 'limit': 4}
        feed = service.client.post('/internal/v1/order-source', headers={'Authorization': 'Bearer one'}, json=feed_body).json()
        wake = feed['events'][0]['wake_id']
        before = service.post('agent', {'action': 'fetch', 'wake_id': wake})
        service.post('agent', {'action': 'read_receipt', 'wake_id': wake, 'wake_digest': 'a' * 64}, code=409)
        assert service.post('agent', {'action': 'fetch', 'wake_id': wake}) == before
        bad = service.client.post('/internal/v1/order-source', headers={'Authorization': 'Bearer one'},
                                   json={**feed_body, 'auth_epoch': 2})
        assert bad.status_code == 409 and bad.json()['error'] == 'order_source_changed'
        assert service.post('agent', {'action': 'fetch', 'wake_id': wake}) == before


# spec: ANYM-993, ANYM-997
def test_pending_question_keeps_original_task_words_beyond_normal_result_retention(tmp_path):
    config, _ = order_host(tmp_path)
    with Service(config) as service:
        result = service.finished(service.start(service.lease(), goal='async-closed', owner_instruction=WORDS)['request_id'])
    # Reopen the stopped disposable store on this thread; SQLite's thread fence stays enabled.
    from deedkit_android.store import Store
    from deedkit_android.agent_run import AgentStore
    store = Store(tmp_path / 'state')
    try:
        runs = AgentStore(store)
        assert runs.purge(1e20) == 0
        row = runs.row('one', result['request_id'])
        assert row is not None and json.loads(row['request'])['owner_instruction'] == WORDS
    finally:
        store.close()


def handoff_host(tmp_path):
    import time
    config, record = order_host(tmp_path)
    path = __import__('pathlib').Path(json.loads(config.read_text())['engines']['codex']['binary'])
    source = path.read_text()
    original = "arguments=dict(operation='notify',payload=dict(message='Owner update'))"
    assert original in source
    source = source.replace(original, f"arguments=dict(operation='bind',payload=dict(next_check_at={time.time()+600!r},state=dict(status='preorder')))")
    original = "        elif mode == 'disconnect': raise SystemExit"
    assert original in source
    source = source.replace(original, '''        elif mode == 'native-control' and m['id'] == 42:
            ref = json.loads(m['result']['contentItems'][0]['text'])['obligation_ref']
            event('item/started',turnId=turn,item=dict(id='handoff-call',type='dynamicToolCall',tool='android_order_control',namespace=None))
            emit(id=43,method='item/tool/call',params=dict(threadId=thread,turnId=turn,callId='handoff-call',namespace=None,tool='android_order_control',arguments=dict(operation='handoff',payload=dict(obligation_ref=ref,target_principal='one'))))
''' + original)
    branch = "        elif mode.startswith('  Home,'): finish()"
    assert branch in source
    source = source.replace(branch, '''        elif mode == 'owner-update':
            event('item/started',turnId=turn,item=dict(id='update-call',type='dynamicToolCall',tool='android_order_control',namespace=None))
            emit(id=55,method='item/tool/call',params=dict(threadId=thread,turnId=turn,callId='update-call',namespace=None,tool='android_order_control',arguments=dict(operation='update',payload=dict(obligation_ref=ref,changes=dict(state=dict(status='new observation'))))))
''' + branch)
    path.write_text(source)
    return config, record


def first_handoff(service):
    page = service.client.post('/internal/v1/order-source', headers={'Authorization': 'Bearer one'},
        json={'device_id': 'home', 'source_id': 'synthetic-configured-source', 'auth_epoch': 1, 'after': 0, 'limit': 4}).json()
    wake, = [m['wake_id'] for m in page['events'] if m['kind'] == 'handoff']
    return service.post('agent', {'action': 'fetch', 'wake_id': wake})


# spec: ANYM-993, ANYM-997
def test_handoff_requires_explicit_current_snapshot_and_next_check_after_read_receipt(tmp_path):
    config, _ = handoff_host(tmp_path)
    with Service(config) as service:
        service.finished(service.start(service.lease(), goal='native-control', owner_instruction=WORDS)['request_id'])
        fetched = first_handoff(service)
        wake = fetched['metadata']['wake_id']
        payload = fetched['payload']
        args = dict(action='handoff', wake_id=wake, wake_digest=fetched['wake_digest'],
                    snapshot_digest=payload['snapshot_digest'], next_check_at=payload['snapshot']['next_check_at'])
        service.post('agent', args, code=409)  # offered is not delivered/read, and never a responsibility ACK
        service.post('agent', {'action': 'read_receipt', 'wake_id': wake, 'wake_digest': fetched['wake_digest']})
        assert service.post('agent', {'action': 'fetch', 'wake_id': wake})['responsibility']['responsible'] == 'codex'
        service.post('agent', {**args, 'snapshot_digest': 'f' * 64}, code=409)
        accepted = service.post('agent', args)
        assert accepted == {'responsible': 'grok', 'acknowledged': True}
        assert service.post('agent', args) == accepted
        assert service.post('agent', {'action': 'fetch', 'wake_id': wake})['responsibility']['responsible'] == 'grok'


# spec: ANYM-993, ANYM-997
def test_later_native_observation_refuses_old_handoff_without_discarding_codex_responsibility(tmp_path):
    config, _ = handoff_host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        service.finished(service.start(lease, goal='native-control', owner_instruction=WORDS)['request_id'])
        fetched = first_handoff(service)
        wake = fetched['metadata']['wake_id']
        service.post('agent', {'action': 'read_receipt', 'wake_id': wake, 'wake_digest': fetched['wake_digest']})
        service.finished(service.start(lease, goal='owner-update')['request_id'])
        result = service.post('agent', dict(action='handoff', wake_id=wake, wake_digest=fetched['wake_digest'],
            snapshot_digest=fetched['payload']['snapshot_digest'],
            next_check_at=fetched['payload']['snapshot']['next_check_at']), code=409)
        assert result['error'] == 'handoff_snapshot_changed'
        current = service.post('agent', {'action': 'fetch', 'wake_id': wake})
        assert current['responsibility']['responsible'] == 'codex' and current['wake_digest'] == fetched['wake_digest']


# spec: ANYM-993, ANYM-997
def test_native_question_source_bridge_hint_fetch_and_natural_answer_return_same_conversation(tmp_path):
    import asyncio
    from pathlib import Path
    from runpy import run_path
    # This shipped bridge is stdlib-only; Android's isolated locked test environment need not install MCP/gateway.
    bridge = run_path(str(Path(__file__).resolve().parents[3] / 'services/gateway/src/agent_gateway/android_wake_bridge.py'))
    Route, projection, step = (bridge[name] for name in ('Route', 'projection', 'step'))
    config, record = order_host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        result = service.finished(service.start(lease, goal='async-closed', owner_instruction=WORDS)['request_id'])
        staged, receipts = [], {}
        route = Route('one', 'home', 'synthetic-configured-source', 1, enabled=True)
        async def cursor(device):
            return {'after': 0}
        async def source(after, limit):
            response = service.client.post('/internal/v1/order-source', headers={'Authorization': 'Bearer one'},
                json={'device_id': 'home', 'source_id': route.source_id, 'auth_epoch': route.auth_epoch,
                      'after': after, 'limit': limit})
            assert response.status_code == 200
            return response.json()
        async def receipt(wake):
            return receipts.get(wake, {'transport_state': 'not_found'})
        async def stage(body):
            staged.append(body)
            for event in body['events']:
                item, digest = projection(event, route)
                receipts[item['wake_id']] = {'wake_id': item['wake_id'], 'identity_hash': digest,
                    'transport_state': 'pending', 'native_status': 'not_observed'}
            return {'items': list(receipts.values()), 'after': body['next_after']}
        status = asyncio.run(step(route, read_cursor=cursor, read_source=source, stage=stage, receipt=receipt))
        assert status['status'] == 'staged' and len(staged) == 1
        metadata, = staged[0]['events']
        assert WORDS not in json.dumps(staged) and 'Which address?' not in json.dumps(staged)
        # The exact released server projection is only a wake hint, never substituted task/native identity.
        hint = {'module': 'android', 'type': 'android_wake', 'wake_id': metadata['wake_id'],
                'kind': metadata['kind'], 'next_call': 'android_agent'}
        fetched = service.post('agent', {'action': 'fetch', 'wake_id': hint['wake_id']})
        assert fetched['original_request']['request_id'] == result['request_id']
        assert fetched['original_request']['owner_instruction'] == WORDS
        original = fetched['metadata']
        service.post('agent', {'action': 'reply', 'request_id': original['request_id'], 'device_id': original['device_id'],
            'session_id': lease, 'native_thread_id': original['native_thread_id'],
            'native_turn_id': original['native_turn_id'], 'item_id': original['item_id'],
            'message_id': 'original-owner-answer', 'text': '  Home, exact natural words.  '})
        completed = service.finished(original['request_id'])
        assert completed['native_session']['thread_id'] == result['native_session']['thread_id']
        assert len({r['pid'] for r in messages(record)}) == 1
        assert len([r for r in messages(record) if r['message'].get('method') == 'thread/start']) == 1
        assert completed['dialogue'][0]['status'] == 'accepted'  # turn/start input receipt, not order success


# spec: ANYM-993, ANYM-997
def test_configuring_source_keeps_existing_unregistered_conversation_and_async_questions_work(tmp_path):
    config, record = order_host(tmp_path)
    body = json.loads(config.read_text())
    source = body['agent'].pop('order_events')
    config.write_text(json.dumps(body))
    with Service(config) as service:
        lease = service.lease()
        first = service.finished(service.start(lease, goal='  Home, initial')['request_id'])
        assert first['native_session']['order_control_registered'] is False
        body['agent']['order_events'] = source
        config.write_text(json.dumps(body))  # synthetic host configuration, never caller tool arguments
        second = service.finished(service.start(lease, goal='async-closed', owner_instruction=WORDS)['request_id'])
        assert second['native_session']['thread_id'] == first['native_session']['thread_id']
        assert second['native_session']['pid'] == first['native_session']['pid']
        assert second['native_session']['order_control_registered'] is False
        feed = service.client.post('/internal/v1/order-source', headers={'Authorization': 'Bearer one'},
            json={'device_id': 'home', **source, 'after': 0, 'limit': 4}).json()
        assert feed['events'][0]['request_id'] == second['request_id']
        registrations = [r['message'] for r in messages(record) if 'dynamicTools' in r['message'].get('params', {})]
        assert registrations == []
        assert len([r for r in messages(record) if r['message'].get('method') == 'thread/start']) == 1


# spec: ANYM-993, ANYM-997
def test_model_cannot_share_original_owner_task_with_unconfigured_other_device_principal(tmp_path):
    config, _ = handoff_host(tmp_path)
    from pathlib import Path
    binary = Path(json.loads(config.read_text())['engines']['codex']['binary'])
    binary.write_text(binary.read_text().replace("target_principal='one'", "target_principal='two'"))
    with Service(config) as service:
        result = service.finished(service.start(service.lease(), goal='native-control', owner_instruction=WORDS)['request_id'])
        assert result['dialogue'][-1]['operation_status'] == 'refused'
        page = service.client.post('/internal/v1/order-source', headers={'Authorization': 'Bearer one'},
            json={'device_id': 'home', 'source_id': 'synthetic-configured-source', 'auth_epoch': 1, 'after': 0, 'limit': 4}).json()
        assert page['events'] == []


# spec: ANYM-993, ANYM-997
def test_configured_owner_recipient_mapping_and_actual_wake_target_are_both_required(tmp_path):
    config, _ = handoff_host(tmp_path)
    from pathlib import Path
    body = json.loads(config.read_text())
    body['agent']['order_events']['handoff_principals'] = {'one': 'two'}  # synthetic trusted delegated mapping
    binary = Path(body['engines']['codex']['binary'])
    binary.write_text(binary.read_text().replace("target_principal='one'", "target_principal='two'"))
    config.write_text(json.dumps(body))
    with Service(config) as service:
        service.finished(service.start(service.lease(), goal='native-control', owner_instruction=WORDS)['request_id'])
        offered = first_handoff(service)
        wake = offered['metadata']['wake_id']
        delegated = service.post('agent', {'action': 'fetch', 'wake_id': wake}, key='two')
        assert delegated['original_request']['owner_instruction'] == WORDS
    import sqlite3
    db = sqlite3.connect(tmp_path / 'state' / 'state.sqlite3')
    with db:
        db.execute('UPDATE android_order_wakes SET principal=? WHERE wake_id=?', ('one', wake))
    db.close()
    with Service(config) as service:
        denied = service.post('agent', {'action': 'fetch', 'wake_id': wake}, key='two', code=404)
        assert denied['error'] == 'not_found' and WORDS not in json.dumps(denied)
