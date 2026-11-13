"""Local native tool preparation: synthetic pinned frames, no public capability or live client."""

import asyncio
import json
from pathlib import Path

import pytest

from deedkit_android.config import Refused
from test_native_dialogue import native, messages


def controlled(tmp_path, *, request_id=42, duplicate=False, namespace=None, operation='notify', overrides=None, resolved=False):
    session, record = native(tmp_path)
    path = Path(session.config.binary)
    source = path.read_text()
    old = "elif mode.startswith('  Home,'): finish()"
    assert old in source
    extra = f'''elif mode == 'native-control':
            event('item/started',turnId=turn,item=dict(id='real-call',type='dynamicToolCall',tool='android_order_control',namespace={namespace!r}))
            request=dict(id={request_id!r},method='item/tool/call',params=dict(threadId=thread,turnId=turn,callId='real-call',namespace={namespace!r},tool='android_order_control',arguments=dict(operation={operation!r},payload=dict(message='Owner update'))))
            request['params'].update({overrides or {}!r})
            emit(**request)
            if {duplicate!r}: emit(**request)
            if {resolved!r}:
                event('serverRequest/resolved',requestId={request_id!r}); finish()
        ''' + old
    path.write_text(source.replace(old, extra))
    return session, record


# spec: ANYM-993, ANYM-997
def test_native_registered_control_uses_real_typed_call_ids_and_no_fabricated_question(tmp_path):
    async def check():
        session, record = controlled(tmp_path)
        received = []
        async def control(context, operation, payload):
            received.append((context, operation, payload))
            return {'status': 'applied', 'effect': 'metadata_only', 'wake_id': 'durable-owner-notice'}
        await session.connect(order_control=True)
        try:
            assert await session.turn('native-control', session.config, lambda e: asyncio.sleep(0),
                                      request_id='original-task', control=control, admit=lambda: None) == 'completed'
            context, operation, payload = received[0]
            assert context['owner'] == 'synthetic-owner' and context['device_id'] == 'home'
            assert context['task_id'] == context['request_id'] == 'original-task'
            assert context['native_request_id'] == 42 and type(context['native_request_id']) is int
            assert context['native_call_id'] == 'real-call' and context['item_id'] == 'real-call'
            assert 'question_id' not in context and context['native_namespace'] is None
            assert operation == 'notify' and payload == {'message': 'Owner update'}
            assert len(received) == 1
            wire = [r['message'] for r in messages(record)]
            registration = next(m for m in wire if m.get('method') == 'thread/start')['params']['dynamicTools']
            assert registration[0]['name'] == 'android_order_control'
            response, = [m for m in wire if m.get('id') == 42 and 'result' in m]
            assert response['result']['success'] is True
            receipt, = session.receipts('original-task')
            assert receipt['status'] == 'unknown' and receipt['native_state'] in ('submitted', 'resolved')
            assert receipt['submission_intent'] is True
            assert all('Owner update' not in p.read_text() for p in [session.journal])
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993, ANYM-997
def test_resuming_existing_thread_never_hot_adds_or_replaces_registration(tmp_path):
    async def check():
        session, record = controlled(tmp_path)
        await session.connect()
        thread = session.thread_id
        await session.close()
        from deedkit_android.codex_session import CodexSession
        resumed = CodexSession(session.dir, session.config, session.host, session.device,
                               bindings=session.bindings, owner=session.owner)
        await resumed.connect(order_control=True)
        try:
            assert resumed.thread_id == thread and resumed.order_control_registered is False
            assert all('dynamicTools' not in r['message']['params'] for r in messages(record)
                       if r['message'].get('method') in ('thread/resume', 'thread/settings/update'))
            assert len([r for r in messages(record) if r['message'].get('method') == 'thread/start']) == 1
        finally:
            await resumed.close()
    asyncio.run(check())


# spec: ANYM-993, ANYM-997
@pytest.mark.parametrize('namespace', ['functions', 'foreign'])
def test_unregistered_namespace_never_reaches_metadata_handler(tmp_path, namespace):
    async def check():
        session, record = controlled(tmp_path, namespace=namespace)
        seen = []
        async def control(*args):
            seen.append(args)
            return {'status': 'applied', 'effect': 'metadata_only'}
        await session.connect(order_control=True)
        try:
            assert await session.turn('native-control', session.config, lambda e: asyncio.sleep(0),
                                      request_id='original', control=control) == 'completed'
            assert seen == []
            response, = [r['message'] for r in messages(record) if r['message'].get('id') == 42]
            assert response['error']['message'] == 'unregistered_native_control'
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993, ANYM-997
def test_duplicate_call_does_not_repeat_host_operation_or_response(tmp_path):
    async def check():
        session, record = controlled(tmp_path, duplicate=True)
        entered = []
        async def control(context, operation, payload):
            entered.append(context)
            await asyncio.sleep(.01)
            return {'status': 'applied', 'effect': 'metadata_only'}
        await session.connect(order_control=True)
        try:
            assert await session.turn('native-control', session.config, lambda e: asyncio.sleep(0),
                                      request_id='original', control=control, admit=lambda: None) == 'completed'
            assert len(entered) == 1
            assert len([r for r in messages(record) if r['message'].get('id') == 42 and 'result' in r['message']]) == 1
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993, ANYM-997
def test_registered_control_binding_survives_resume_without_new_registration_or_replay(tmp_path):
    async def check():
        session, record = controlled(tmp_path)
        await session.connect(order_control=True)
        thread = session.thread_id
        await session.close()
        from deedkit_android.codex_session import CodexSession
        resumed = CodexSession(session.dir, session.config, session.host, session.device,
                               bindings=session.bindings, owner=session.owner)
        await resumed.connect()
        try:
            assert resumed.order_control_registered is True and resumed.thread_id == thread
            assert all('dynamicTools' not in r['message']['params'] for r in messages(record)
                       if r['message'].get('method') == 'thread/resume')
            assert len([r for r in messages(record) if r['message'].get('method') == 'thread/start']) == 1
        finally:
            await resumed.close()
    asyncio.run(check())


# spec: ANYM-993, ANYM-997
def test_native_call_without_observed_lifecycle_item_never_invents_item_identity(tmp_path):
    async def check():
        session, record = controlled(tmp_path)
        path = Path(session.config.binary)
        path.write_text(path.read_text().replace("event('item/started',turnId=turn,item=dict(id='real-call',type='dynamicToolCall',tool='android_order_control',namespace=None))", 'pass'))
        seen = []
        async def control(context, operation, payload):
            seen.append(context)
            return {'status': 'applied', 'effect': 'metadata_only'}
        await session.connect(order_control=True)
        try:
            await session.turn('native-control', session.config, lambda e: asyncio.sleep(0),
                               request_id='original', control=control, admit=lambda: None)
            assert seen[0]['native_call_id'] == 'real-call' and seen[0]['item_id'] is None
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993, ANYM-997
def test_protected_operation_without_real_registered_fence_refuses_before_host_callback(tmp_path):
    async def check():
        session, record = controlled(tmp_path, operation='request_protected_input')
        seen = []
        async def control(*args):
            seen.append(args)
            raise AssertionError('must not read a protected reference or open a channel')
        await session.connect(order_control=True)
        try:
            await session.turn('native-control', session.config, lambda e: asyncio.sleep(0),
                               request_id='original', control=control, admit=lambda: None)
            assert seen == []
            response, = [r['message'] for r in messages(record) if r['message'].get('id') == 42]
            assert response['error']['message'] == 'protected_input_unavailable'
            assert session.receipts('original') == []
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993, ANYM-997
@pytest.mark.parametrize('overrides', [
    {'threadId': 'foreign-thread'}, {'turnId': 'foreign-turn'}, {'tool': 'generic/native/rpc'},
    {'arguments': {'operation': 'notify', 'payload': {'message': 'hi', 'owner': 'foreign'}}},
    {'arguments': {'operation': 'notify', 'payload': {'message': 'hi'}, 'device_id': 'foreign'}},
])
def test_foreign_or_caller_supplied_authority_is_rejected_before_host_dispatch(tmp_path, overrides):
    async def check():
        session, record = controlled(tmp_path, overrides=overrides)
        seen = []
        async def control(*args):
            seen.append(args)
            return {'status': 'applied', 'effect': 'metadata_only'}
        await session.connect(order_control=True)
        try:
            await session.turn('native-control', session.config, lambda e: asyncio.sleep(0),
                               request_id='original', control=control, admit=lambda: None)
            assert seen == []
            response, = [r['message'] for r in messages(record) if r['message'].get('id') == 42]
            assert 'error' in response and session.receipts('original') == []
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993, ANYM-997
def test_current_admission_refusal_is_known_before_host_operation(tmp_path):
    async def check():
        session, record = controlled(tmp_path)
        seen, admissions = [], []
        async def control(*args):
            seen.append(args)
            return {'status': 'applied', 'effect': 'metadata_only'}
        def admit():
            admissions.append(True)
            if len(admissions) > 2:  # initial and post-selection admission precede the native control frame
                raise Refused('forbidden', 403)
        await session.connect(order_control=True)
        try:
            await session.turn('native-control', session.config, lambda e: asyncio.sleep(0),
                               request_id='original', control=control, admit=admit)
            assert seen == []
            receipt, = session.receipts('original')
            assert receipt['status'] == receipt['operation_status'] == 'refused'
            assert receipt['operation_intent'] is receipt['submission_intent'] is False
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993, ANYM-997
def test_resolved_before_serialized_dispatch_never_runs_or_acknowledges_operation(tmp_path):
    async def check():
        session, record = controlled(tmp_path, resolved=True)
        seen = []
        async def control(*args):
            seen.append(args)
            return {'status': 'applied', 'effect': 'metadata_only'}
        await session.connect(order_control=True)
        await session.interaction_lock.acquire()
        try:
            await asyncio.wait_for(session.turn('native-control', session.config, lambda e: asyncio.sleep(0),
                               request_id='original', control=control, admit=lambda: None), 2)
            assert seen == []
            receipt, = session.receipts('original')
            assert receipt['native_state'] == 'expired' and receipt['operation_status'] == 'refused'
            assert receipt['operation_intent'] is receipt['submission_intent'] is False
            assert not any(r['message'].get('id') == 42 for r in messages(record))
        finally:
            session.interaction_lock.release()
            await session.close()
    asyncio.run(check())


# spec: ANYM-993, ANYM-997
def test_cancel_after_host_dispatch_persists_unknown_across_restart_without_replay(tmp_path):
    async def check():
        session, record = controlled(tmp_path)
        entered, cancelled = asyncio.Event(), asyncio.Event()
        seen = []
        async def control(context, operation, payload):
            seen.append(context)
            assert json.loads(session.journal.read_text())  # intent exists before a host await
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        await session.connect(order_control=True)
        turn = asyncio.create_task(session.turn('native-control', session.config, lambda e: asyncio.sleep(0),
                                               request_id='original', control=control, admit=lambda: None))
        await asyncio.wait_for(entered.wait(), 2)
        await session.interrupt()
        assert cancelled.is_set() and await turn == 'interrupted'
        receipt, = session.receipts('original')
        assert receipt['status'] == receipt['operation_status'] == 'unknown'
        assert receipt['operation_intent'] is True and receipt['submission_intent'] is False
        await session.close()
        from deedkit_android.codex_session import CodexSession
        resumed = CodexSession(session.dir, session.config, session.host, session.device,
                               bindings=session.bindings, owner=session.owner)
        await resumed.connect()
        try:
            assert resumed.receipts('original') == [receipt] and len(seen) == 1
            assert not any(r['message'].get('id') == 42 for r in messages(record))
        finally:
            await resumed.close()
    asyncio.run(check())


# spec: ANYM-993, ANYM-997
def test_typed_string_native_request_is_retained_in_original_response_and_receipt(tmp_path):
    async def check():
        session, record = controlled(tmp_path, request_id='42')
        seen = []
        async def control(context, operation, payload):
            seen.append(context)
            return {'status': 'applied', 'effect': 'metadata_only'}
        await session.connect(order_control=True)
        try:
            await session.turn('native-control', session.config, lambda e: asyncio.sleep(0),
                               request_id='original', control=control, admit=lambda: None)
            assert type(seen[0]['native_request_id']) is str and seen[0]['native_request_id'] == '42'
            receipt, = session.receipts('original')
            assert type(receipt['native_request_id']) is str and receipt['native_request_id'] == '42'
            responses = [r['message'] for r in messages(record) if r['message'].get('id') == '42']
            assert len(responses) == 1 and 'result' in responses[0]
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993, ANYM-997
@pytest.mark.parametrize('alias', [{'native_request_id': '42', 'call_id': 'real-call'},
                                {'native_request_id': 42, 'call_id': 'replacement-call'}])
def test_new_correlation_cannot_repeat_original_control_after_response_write(tmp_path, alias):
    async def check():
        session, record = controlled(tmp_path)
        path = Path(session.config.binary)
        source = path.read_text()
        original = "        elif mode == 'disconnect': raise SystemExit"
        assert original in source
        path.write_text(source.replace(original, f'''        elif mode == 'native-control':
            emit(id={alias['native_request_id']!r},method='item/tool/call',params=dict(threadId=thread,turnId=turn,callId={alias['call_id']!r},namespace=None,tool='android_order_control',arguments=dict(operation='notify',payload=dict(message='Owner update'))))
''' + original))
        seen = []
        async def control(context, operation, payload):
            seen.append(context)
            return {'status': 'applied', 'effect': 'metadata_only'}
        await session.connect(order_control=True)
        try:
            await session.turn('native-control', session.config, lambda e: asyncio.sleep(0),
                               request_id='original', control=control, admit=lambda: None)
            assert len(seen) == 1 and seen[0]['native_call_id'] == 'real-call'
            receipt, = session.receipts('original')
            assert type(receipt['native_request_id']) is int and receipt['native_request_id'] == 42
            assert receipt['native_call_id'] == 'real-call' and receipt['status'] == 'unknown'
            denied = [r['message'] for r in messages(record) if r['message'].get('error', {}).get('message') == 'native_control_conflict']
            assert len(denied) == 1 and denied[0]['id'] == alias['native_request_id']
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993, ANYM-997
def test_unknown_host_outcome_reports_metadata_uncertainty_without_blocking_native_progress(tmp_path):
    async def check():
        session, record = controlled(tmp_path)
        calls = []
        async def control(context, operation, payload):
            calls.append(context)
            raise RuntimeError('SYNTHETIC_INTERNAL_DETAIL_MUST_NOT_LEAK')
        await session.connect(order_control=True)
        try:
            await asyncio.wait_for(session.turn('native-control', session.config, lambda e: asyncio.sleep(0),
                request_id='original', control=control, admit=lambda: None), 2)
            receipt, = session.receipts('original')
            assert receipt['operation_status'] == receipt['status'] == 'unknown' and len(calls) == 1
            answer, = [r['message'] for r in messages(record) if r['message'].get('id') == 42]
            assert answer['result']['success'] is False
            assert 'SYNTHETIC_INTERNAL_DETAIL' not in json.dumps(answer)
            assert 'SYNTHETIC_INTERNAL_DETAIL' not in session.journal.read_text()
        finally:
            await session.close()
    asyncio.run(check())
