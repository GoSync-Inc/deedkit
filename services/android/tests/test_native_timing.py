"""Host-observed native timing, using controlled clocks and a synthetic stdio process."""

import asyncio
import json
import time

import pytest

from agent_harness import Service
from test_codex_session import calls, live_host
from test_native_dialogue import native


def frame(method, *, turn='turn', **params):
    identity = {'turn': turn} if isinstance(turn, dict) else {'turnId': turn}
    return {'method': method, 'params': {'threadId': 'thread', **identity, **params}}


# spec: ANYM-993
def test_native_timing_unions_parallel_commands_without_claiming_model_or_network_time():
    from deedkit_android.agent_diagnostics import NativeTiming
    now = [0.0]
    timing = NativeTiming(clock=lambda: now[0])
    now[0] = 1
    timing.phase('preflight')
    now[0] = 2
    timing.phase('submission')
    now[0] = 3
    timing.bind('thread', 'turn')
    now[0] = 4
    timing.observe(frame('item/started', item={'id': 'a', 'type': 'commandExecution', 'command': 'PRIVATE'}))
    now[0] = 5
    timing.observe(frame('item/started', item={'id': 'b', 'type': 'commandExecution'}))
    timing.observe(frame('item/started', item={'id': 'b', 'type': 'commandExecution'}))
    now[0] = 7
    timing.observe(frame('item/completed', item={'id': 'a', 'type': 'commandExecution', 'aggregatedOutput': 'SECRET'}))
    now[0] = 8
    timing.observe(frame('item/completed', item={'id': 'b', 'type': 'commandExecution'}))
    now[0] = 9
    timing.observe(frame('item/started', item={'id': 'a', 'type': 'commandExecution'}))
    timing.observe(frame('item/completed', item={'id': 'a', 'type': 'commandExecution'}))
    now[0] = 10
    timing.observe(frame('turn/completed', turn={'id': 'turn', 'status': 'completed'}))
    result = timing.receipt()
    assert result['phase_ms'] == {'connect': 1000, 'preflight': 1000, 'submission': 1000, 'native_turn': 7000}
    assert result['command_wall_ms'] == 4000  # union, rather than overlapping 3s + 3s
    assert result['commands_started'] == result['commands_completed'] == 2
    assert result['commands_incomplete'] == 0
    assert result['stop_source'] == 'native_completed'
    assert result['native_terminal'] == 'completed' and result['elapsed_ms'] == 10000
    assert result['model_time_observed'] is result['network_time_observed'] is False
    assert 'PRIVATE' not in json.dumps(result) and 'SECRET' not in json.dumps(result)
    assert 'command' not in result and 'items' not in result
    now[0] = 50
    assert timing.receipt() == result  # terminal evidence never includes later idle time


# spec: ANYM-993
def test_native_timing_preserves_typed_blocking_request_identity_and_ignores_async_or_foreign_events():
    from deedkit_android.agent_diagnostics import NativeTiming
    now = [0.0]
    timing = NativeTiming(clock=lambda: now[0])
    timing.bind('thread', 'turn')
    now[0] = 1
    timing.observe(frame('item/tool/requestUserInput', isBlocking=True) | {'id': 12})
    now[0] = 2
    timing.observe(frame('item/tool/requestUserInput', isBlocking=False) | {'id': 'async'})
    timing.observe(frame('item/completed', item={'id': 'message', 'type': 'agentMessage', 'questions': [{'title': 'PRIVATE'}]}))
    now[0] = 4
    timing.observe(frame('serverRequest/resolved', requestId='12'))
    timing.observe(frame('serverRequest/resolved', turn='other', requestId=12))
    timing.observe(frame('item/started', turn='other', item={'id': 'foreign', 'type': 'commandExecution'}))
    timing.observe(frame('turn/completed', turn={'id': 'foreign', 'status': 'completed'}))
    now[0] = 6
    timing.observe({'method': 'serverRequest/resolved', 'params': {'threadId': 'thread', 'requestId': 12}})
    now[0] = 8
    timing.observe(frame('turn/completed', turn={'id': 'turn', 'status': 'completed'}))
    result = timing.receipt()
    assert result['blocking_question_pending_ms'] == 5000
    assert result['blocking_questions'] == 1 and result['commands_started'] == 0
    assert result['elapsed_ms'] == 8000 and 'PRIVATE' not in json.dumps(result)
    # Resolving the native wait is timing evidence only; it attests no answer acceptance.
    assert 'accepted' not in result and 'owner_wait_ms' not in result


# spec: ANYM-993
def test_native_timing_keeps_incomplete_command_and_adapter_stop_distinct_from_native_terminal():
    from deedkit_android.agent_diagnostics import NativeTiming
    now = [0.0]
    timing = NativeTiming(clock=lambda: now[0])
    timing.bind('thread', 'turn')
    now[0] = 1
    timing.observe(frame('item/started', item={'id': 'running', 'type': 'commandExecution'}))
    now[0] = 2
    timing.observe(frame('turn/completed', turn={'id': 'turn', 'status': 'interrupted'}))
    timing.request_stop('deadline')  # enclosing timeout identifies the initiator after native drains
    timing.request_stop('owner_stop')
    result = timing.receipt()
    assert result['stop_source'] == 'deadline' and result['native_terminal'] == 'interrupted'
    assert result['commands_incomplete'] == 1 and result['commands_completed'] == 0
    assert result['command_wall_ms'] == 1000


# spec: ANYM-993
def test_native_timing_protocol_question_wait_ends_on_resolution_without_attesting_answer_acceptance(tmp_path):
    async def check():
        session, _ = native(tmp_path)
        await session.connect()
        question = asyncio.Event()

        async def consume(event):
            if event.get('method') == 'item/tool/requestUserInput':
                question.set()

        task = asyncio.create_task(session.turn('question', session.config, consume, request_id='original'))
        try:
            async with asyncio.timeout(3):
                await question.wait()
                await asyncio.sleep(.01)
                receipt = await session.reply(request_id='original', thread_id=session.thread_id,
                    turn_id=session.turn_id, native_request_id=12, item_id='question-item',
                    answers={'address': ['PRIVATE_ADDRESS'], 'choice': ['PRIVATE_SHOP']})
                assert await task == 'completed'
            timing = session.timing.receipt()
            assert timing['blocking_questions'] == 1 and timing['blocking_question_pending_ms'] >= 10
            assert receipt['status'] == 'unknown' and session.receipts('original')[0]['native_state'] == 'resolved'
            assert timing['stop_source'] == 'native_completed'
            assert 'PRIVATE' not in json.dumps(timing) and 'accepted' not in timing
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await session.close()

    asyncio.run(check())


# spec: ANYM-993
def test_native_timing_same_process_turns_have_independent_metadata_and_no_model_duration_guess(tmp_path):
    config, record = live_host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        first = service.finished(service.start(lease)['request_id'])
        second = service.finished(service.start(lease)['request_id'])
        for result in (first, second):
            timing = result['native_timing']
            assert timing['native_thread_id'] == result['native_session']['thread_id']
            assert timing['native_turn_id'] == result['native_session']['turn_id']
            assert timing['commands_started'] == timing['commands_completed'] == 1
            assert 0 <= timing['command_wall_ms'] <= timing['elapsed_ms']
            assert timing['stop_source'] == 'native_completed' and timing['native_terminal'] == 'completed'
            assert timing['phase_ms']['preflight'] >= 0
            assert result['timings']['model_ms'] is None
            assert 'adb' not in json.dumps(timing) and 'Remembered' not in json.dumps(timing)
        assert first['native_timing']['native_turn_id'] != second['native_timing']['native_turn_id']
        assert first['native_session']['pid'] == second['native_session']['pid']
        assert len([c for c in calls(record) if c['message']['method'] == 'thread/start']) == 1


# spec: ANYM-993
@pytest.mark.parametrize('stop_source', ['owner_stop', 'deadline', 'native_disconnected'])
def test_native_timing_real_lifecycle_reports_stop_initiator_without_replaying_input(tmp_path, monkeypatch, stop_source):
    from deedkit_android import codex_session
    if stop_source == 'deadline':
        monkeypatch.setattr(codex_session, 'MINUTE_S', .25)
    config, record = live_host(tmp_path, delay=stop_source != 'native_disconnected', crash=stop_source == 'native_disconnected')
    with Service(config) as service:
        lease = service.lease()
        request = service.start(lease, max_minutes=1)
        if stop_source == 'owner_stop':
            deadline = time.monotonic() + 5
            while not any(c['message']['method'] == 'turn/start' for c in calls(record)):
                assert time.monotonic() < deadline
                time.sleep(.01)
            service.post('agent', {'action': 'stop', 'request_id': request['request_id']})
        result = service.finished(request['request_id'])
        assert result['native_timing']['stop_source'] == stop_source
        assert result['native_timing']['native_terminal'] == (None if stop_source == 'native_disconnected' else 'interrupted')
        assert result['native_timing']['elapsed_ms'] >= 0
        service.start(lease, request_id=request['request_id'], max_minutes=1)
        assert len([c for c in calls(record) if c['message']['method'] == 'turn/start']) == 1
