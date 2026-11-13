"""Authenticated question/reply/steer admission, synthetic native stdio and phone only."""

from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import threading
import time
import uuid

import pytest

from agent_harness import Service
from deedkit_android.codex_session import CodexSession
from test_native_dialogue import messages, native


def host(tmp_path):
    _, record = native(tmp_path)
    path = tmp_path / 'config.json'
    config = json.loads(path.read_text())
    config['agent'].update(guarded=False, persistent=True)
    config['devices']['desk'] = 'SECOND-SYNTHETIC'
    config['principals'][0]['devices'].append('desk')
    path.write_text(json.dumps(config))
    return path, record


def question(service, request_id):
    deadline = time.monotonic() + 3
    while True:
        view = service.get(request_id, wait_s=1)
        found = [event for event in view['events'] if event['kind'] == 'question']
        if found:
            return found[0]
        assert time.monotonic() < deadline, view


def answer(session_id, request_id, event):
    return dict(action='reply', device_id='home', session_id=session_id, request_id=request_id,
                native_thread_id=event['native_thread_id'], native_turn_id=event['native_turn_id'],
                native_request_id=event['native_request_id'], item_id=event['item_id'],
                answers={'address': ['Apartment two'], 'choice': ['Shop A']})


def writes(record, method=None):
    return [row['message'] for row in messages(record) if (
        row['message'].get('method') == method if method else row['message'].get('result', {}).get('answers'))]


# spec: ANYM-993
@pytest.mark.parametrize('goal', ['question', 'string-id', 'mid-question'])
def test_owner_natural_reply_and_repeat_return_original_receipt_without_resend(tmp_path, goal):
    config, record = host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        started = service.start(lease, goal=goal)
        event = question(service, started['request_id'])
        args = answer(lease, started['request_id'], event)
        result = service.post('agent', args)
        assert result['input_receipt']['status'] == 'unknown'
        assert result['input_receipt']['submission_intent'] is True
        finished = service.finished(started['request_id'])
        assert finished['state'] == 'succeeded'
        again = service.post('agent', args)
        assert again['input_receipt']['native_request_id'] == event['native_request_id']
        assert type(again['input_receipt']['native_request_id']) is type(event['native_request_id'])
        assert again['input_receipt']['status'] == 'unknown'
        assert len(writes(record)) == 1
        assert finished['dialogue'][0]['question_ids'] == ['address', 'choice']
        assert len({row['pid'] for row in messages(record)}) == 1


# spec: ANYM-993
@pytest.mark.parametrize('change', ['other_principal', 'reader', 'device', 'lease', 'thread', 'turn', 'item', 'typed_rpc', 'questions'])
def test_http_binding_mismatch_fails_before_any_native_answer_write(tmp_path, change):
    config, record = host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        started = service.start(lease, goal='question')
        event = question(service, started['request_id'])
        args = answer(lease, started['request_id'], event)
        key, code = 'one', 409
        if change == 'other_principal':
            key, code = 'two', 404
        elif change == 'reader':
            key, code = 'reader', 403
        elif change == 'device':
            args.update(device_id='desk', session_id=service.lease(device='desk'))
        elif change == 'lease':
            args['session_id'] = service.lease(device='desk')
        elif change == 'typed_rpc':
            args['native_request_id'] = str(args['native_request_id'])
        elif change == 'questions':
            args['answers'] = {'unrelated': ['two']}
        else:
            args[{'thread': 'native_thread_id', 'turn': 'native_turn_id', 'item': 'item_id'}[change]] = 'unrelated'
        service.post('agent', args, key=key, code=code)
        assert writes(record) == []
        pending = service.get(started['request_id'])['dialogue'][0]
        assert pending['native_state'] == 'waiting' and pending['submission_intent'] is False


# spec: ANYM-993
@pytest.mark.parametrize('change', ['rpc_bool', 'missing_question_ids', 'extra_rpc_method', 'start_fields', 'null_thread'])
def test_reply_schema_refuses_invalid_or_generic_rpc_shapes(tmp_path, change):
    config, record = host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        started = service.start(lease, goal='question')
        args = answer(lease, started['request_id'], question(service, started['request_id']))
        if change == 'rpc_bool':
            args['native_request_id'] = True
        elif change == 'missing_question_ids':
            del args['answers']
        elif change == 'extra_rpc_method':
            args['method'] = 'item/commandExecution/requestApproval'
        elif change == 'start_fields':
            args['goal'] = 'New unrelated goal'
        else:
            args['native_thread_id'] = None
        refusal = service.post('agent', args, code=422)
        assert refusal['error'] == 'invalid_arguments' and refusal['field_errors']
        assert writes(record) == []


# spec: ANYM-993
def test_concurrent_http_answers_write_once_and_changed_reply_conflicts(tmp_path):
    config, record = host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        started = service.start(lease, goal='question')
        args = answer(lease, started['request_id'], question(service, started['request_id']))
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: service.post('agent', args), range(2)))
        assert all(reply['input_receipt']['status'] == 'unknown' for reply in replies)
        assert len(writes(record)) == 1
        service.post('agent', {**args, 'answers': {'address': ['changed'], 'choice': ['Shop A']}}, code=409)
        assert len(writes(record)) == 1


# spec: ANYM-993
@pytest.mark.parametrize('goal', ['question', 'expired'])
def test_cancelled_or_expired_http_reply_never_reaches_native(tmp_path, goal):
    config, record = host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        started = service.start(lease, goal=goal)
        args = answer(lease, started['request_id'], question(service, started['request_id']))
        if goal == 'question':
            service.post('agent', {'action': 'stop', 'request_id': started['request_id']})
        assert service.post('agent', args, code=409)['error'] == 'question_expired'
        assert writes(record) == []


# spec: ANYM-993
def test_http_receipt_readback_after_restart_does_not_start_a_native_client(tmp_path):
    config, record = host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        started = service.start(lease, goal='disconnect')
        args = answer(lease, started['request_id'], question(service, started['request_id']))
        assert service.post('agent', args)['input_receipt']['status'] == 'unknown'
        assert service.finished(started['request_id'])['state'] == 'failed'
    before = messages(record)
    with Service(config) as service:
        read = service.get(started['request_id'])
        assert read['dialogue'][0]['status'] == 'unknown'
        result = service.post('agent', args)
        assert result['input_receipt']['status'] == 'unknown'
        assert messages(record) == before


# spec: ANYM-993
def test_http_steer_uses_current_binding_and_positive_original_turn_receipt(tmp_path):
    config, record = host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        started = service.start(lease, goal='steer')
        deadline = time.monotonic() + 3
        while True:
            running = service.get(started['request_id'], wait_s=1)
            if running.get('native_session', {}).get('turn_id'):
                break
            assert time.monotonic() < deadline
        current = running['native_session']
        args = dict(action='steer', device_id='home', session_id=lease, request_id=started['request_id'],
                    native_thread_id=current['thread_id'], native_turn_id=current['turn_id'],
                    message_id=str(uuid.uuid4()), text='Read status only; retain the active order.')
        service.post('agent', {**args, 'native_turn_id': 'unrelated'}, code=409)
        assert writes(record, 'turn/steer') == []
        first = service.post('agent', args)
        assert first['input_receipt']['status'] == 'accepted'
        assert service.finished(started['request_id'])['state'] == 'succeeded'
        assert service.post('agent', args)['input_receipt']['status'] == 'accepted'
        assert len(writes(record, 'turn/steer')) == 1


# spec: ANYM-993
def test_http_rechecks_revoked_control_after_waiting_before_native_write(tmp_path, monkeypatch):
    config, record = host(tmp_path)
    entered, released = threading.Event(), threading.Event()
    original = CodexSession.reply

    async def paused(self, **kwargs):
        entered.set()
        while not released.is_set():
            await asyncio.sleep(.005)
        return await original(self, **kwargs)

    monkeypatch.setattr(CodexSession, 'reply', paused)
    with Service(config) as service:
        lease = service.lease()
        started = service.start(lease, goal='question')
        args = answer(lease, started['request_id'], question(service, started['request_id']))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(service.post, 'agent', args, code=403)
            try:
                assert entered.wait(2)
                body = json.loads(config.read_text())
                body['principals'][0]['permissions'] = ['android:read']
                config.write_text(json.dumps(body))
            finally:
                released.set()
            assert future.result(timeout=3)['error'] == 'forbidden'
        assert writes(record) == []


# spec: ANYM-993
def test_capability_pairs_dialogue_availability_with_persistent_admission(tmp_path):
    config, record = host(tmp_path)
    with Service(config) as service:
        capabilities = service.post('capabilities', {})['agent']
        assert capabilities['dialogue']['available'] is True
        assert capabilities['dialogue']['actions'] == ['reply', 'steer']
        assert set(capabilities['input_schema']['properties']['action']['enum']) == {'start', 'get', 'stop', 'reply', 'steer'}
        body = json.loads(config.read_text())
        body['agent']['persistent'] = False
        config.write_text(json.dumps(body))
        assert service.post('capabilities', {})['agent']['dialogue']['available'] is False
        assert messages(record) == []  # version/capability check, no app-server or model turn


# spec: ANYM-993
def test_start_identity_keeps_legacy_fingerprint_despite_optional_dialogue_fields():
    import hashlib
    from deedkit_android.agent_run import AgentCall
    call = AgentCall(action='start', request_id=str(uuid.uuid4()), device_id='home', session_id='lease',
                     goal='Read', owner_instruction='Read status', params={'COUNT': 2})
    body = {key: value for key, value in call.model_dump().items() if key not in {
        'action', 'request_id', 'session_id', 'after', 'wait_s', 'native_thread_id', 'native_turn_id',
        'native_request_id', 'item_id', 'answers', 'message_id', 'text'}}
    body.update(apps=[], secrets=[], outputs=[], params={'COUNT': '2'})
    assert call.identity() == hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
