"""Pinned context notifications through synthetic stdio, without invoking compaction or a provider."""

import asyncio
import json
from pathlib import Path

import pytest

from agent_harness import Service
from test_codex_session import calls, live_host
from test_native_dialogue import binding, messages, native
from test_order_supervision import host, rows


def frame(method, *, thread='thread', turn='turn', **params):
    return {'method': method, 'params': {'threadId': thread, 'turnId': turn, **params}}


# spec: ANYM-993
def test_native_context_receipts_keep_actual_usage_and_compaction_identity_without_invented_thresholds():
    from deedkit_android.agent_diagnostics import NativeContext
    context = NativeContext()
    context.bind('thread', 'turn')
    context.observe(frame('thread/tokenUsage/updated', tokenUsage={
        'last': {'inputTokens': 20, 'cacheWriteInputTokens': 7, 'reasoningOutputTokens': 3},
        'total': {'totalTokens': 12345}, 'modelContextWindow': 98765, 'raw': 'PRIVATE'}))
    context.observe(frame('item/started', item={'id': 'compact', 'type': 'contextCompaction', 'summary': 'PRIVATE'}))
    context.observe(frame('item/started', item={'id': 'compact', 'type': 'contextCompaction'}))
    context.observe(frame('item/completed', item={'id': 'compact', 'type': 'contextCompaction'}))
    context.observe(frame('item/started', item={'id': 'compact', 'type': 'contextCompaction'}))
    receipt = context.receipt()
    assert receipt == {'native_thread_id': 'thread', 'native_turn_id': 'turn',
        'token_usage': {'last': {'inputTokens': 20, 'cacheWriteInputTokens': 7, 'reasoningOutputTokens': 3},
                        'total': {'totalTokens': 12345}},
        'model_context_window': 98765, 'auto_compact_limit_tokens': None,
        'compactions': [{'item_id': 'compact', 'started': True, 'completed': True}]}
    for event in [frame('thread/tokenUsage/updated', thread='foreign', tokenUsage={'modelContextWindow': 1}),
                  frame('thread/tokenUsage/updated', turn='foreign', tokenUsage={'modelContextWindow': 2}),
                  frame('item/completed', turn='foreign', item={'id': 'other', 'type': 'contextCompaction'})]:
        context.observe(event)
    assert context.receipt() == receipt and 'PRIVATE' not in json.dumps(receipt)
    # Returned metadata cannot mutate the retained observation, and completion never synthesizes a start.
    receipt['token_usage']['last']['inputTokens'] = 999
    context.observe(frame('item/completed', item={'id': 'completion-only', 'type': 'contextCompaction'}))
    assert context.receipt()['token_usage']['last']['inputTokens'] == 20
    assert context.receipt()['compactions'][-1] == {'item_id': 'completion-only', 'started': False, 'completed': True}


# spec: ANYM-993
@pytest.mark.parametrize('invalid', [True, -1, 1.5, '200000'])
def test_native_context_unknown_or_invalid_window_and_token_fields_remain_unobserved(invalid):
    from deedkit_android.agent_diagnostics import NativeContext
    context = NativeContext()
    context.bind('thread', 'turn')
    assert context.receipt()['token_usage'] is None
    context.observe(frame('thread/tokenUsage/updated', tokenUsage={
        'modelContextWindow': invalid, 'last': {'inputTokens': invalid, 'outputTokens': 0}, 'total': None}))
    receipt = context.receipt()
    assert receipt['model_context_window'] is receipt['auto_compact_limit_tokens'] is None
    assert receipt['token_usage'] == {'last': {'outputTokens': 0}, 'total': {}}
    assert receipt['compactions'] == []


# spec: ANYM-993
def test_native_context_protocol_receipts_reset_per_goal_on_same_thread_without_extra_native_requests(tmp_path):
    config, record = live_host(tmp_path)
    binary = tmp_path / 'codex'
    source = binary.read_text().replace("event('item/started', turnId=current, item=dict(id='cmd'", """if turns == 1:
            event('item/started',turnId=current,item=dict(id='compact',type='contextCompaction'))
            event('item/completed',turnId=current,item=dict(id='compact',type='contextCompaction',summary='PRIVATE'))
        event('item/started', turnId=current, item=dict(id='cmd'""")
    source = source.replace('tokenUsage=dict(last=dict(inputTokens=20, cachedInputTokens=5, outputTokens=3))',
        'tokenUsage=dict(last=dict(inputTokens=20,cachedInputTokens=5,outputTokens=3,cacheWriteInputTokens=7),total=dict(totalTokens=12345),modelContextWindow=98765)')
    binary.write_text(source)
    with Service(config) as service:
        token = service.lease()
        first = service.finished(service.start(token)['request_id'])
        second = service.finished(service.start(token)['request_id'])
        assert first['state'] == second['state'] == 'succeeded'
        assert first['tokens'] == second['tokens'] == {'input': 20, 'output': 3, 'cache_read': 5, 'cache_write': 7}
        observed = first['native_context']
        assert observed['native_thread_id'] == first['native_session']['thread_id'] == second['native_session']['thread_id']
        assert observed['native_turn_id'] == first['native_session']['turn_id']
        assert observed['compactions'] == [{'item_id': 'compact', 'started': True, 'completed': True}]
        assert observed['model_context_window'] == 98765 and observed['auto_compact_limit_tokens'] is None
        assert second['native_context']['native_turn_id'] == second['native_session']['turn_id']
        assert second['native_context']['compactions'] == [] and 'PRIVATE' not in json.dumps(observed)
        wire = calls(record)
        assert len({r['pid'] for r in wire}) == 1
        assert len([r for r in wire if r['message'].get('method') == 'thread/start']) == 1
        assert len([r for r in wire if r['message'].get('method') == 'turn/start']) == 2
        assert all(r['message'].get('method') not in ('thread/compact/start','thread/rollback','thread/fork') for r in wire)


# spec: ANYM-993
def test_native_compaction_does_not_resolve_pending_question_or_replay_unknown_answer(tmp_path):
    from deedkit_android.agent_diagnostics import NativeContext
    async def check():
        session, record = native(tmp_path)
        binary = Path(session.config.binary)
        source = binary.read_text().replace("if mode == 'cleared': event('serverRequest/resolved',requestId=rid)",
            """event('item/started',turnId=turn,item=dict(id='compact',type='contextCompaction'))
            event('item/completed',turnId=turn,item=dict(id='compact',type='contextCompaction'))
            if mode == 'cleared': event('serverRequest/resolved',requestId=rid)""")
        binary.write_text(source)
        await session.connect()
        context, observed = NativeContext(), asyncio.Event()
        async def consume(event):
            context.observe(event)
            if (event.get('params',{}).get('item') or {}).get('id') == 'compact':
                if event['method'] == 'item/completed':
                    observed.set()
        task = asyncio.create_task(session.turn('disconnect', session.config, consume, request_id='original-run',
            on_started=lambda turn: context.bind(session.thread_id,turn)))
        try:
            await asyncio.wait_for(observed.wait(),3)
            receipt, = session.receipts('original-run')
            assert receipt['status'] == 'pending' and receipt['native_request_id'] == 12
            assert receipt['question_ids'] == ['address','choice'] and receipt['submission_intent'] is False
            question = next(row['message'] for row in messages(record) if row['message'].get('method') == 'turn/start')
            assert question['params']['input'][0]['text'] == 'disconnect'
            event = {'id':12,'params':{'threadId':session.thread_id,'turnId':session.turn_id,'itemId':'question-item'}}
            args = {**binding(session,event),'answers':{'address':['Home'],'choice':['Shop']}}
            answer = await session.reply(**args)
            with pytest.raises(OSError):
                await task
            repeat = await session.reply(**args)
            assert answer['status'] == repeat['status'] == 'unknown'
            assert repeat['submission_intent'] is True
            assert context.receipt()['compactions'][0]['completed'] is True
            replies = [r for r in messages(record) if r['message'].get('result',{}).get('answers')]
            assert len(replies) == 1 and len({r['pid'] for r in messages(record)}) == 1
        finally:
            await session.close()
            await asyncio.gather(task,return_exceptions=True)
    asyncio.run(check())


# spec: ANYM-993, ANYM-997
def test_native_compaction_keeps_active_original_order_and_fixed_supervision_authority(tmp_path,monkeypatch):
    config, record = host(tmp_path,monkeypatch,delay=30)
    binary = Path(json.loads(config.read_text())['engines']['codex']['binary'])
    source = binary.read_text().replace("def finish():", """def finish():
    event('item/started',turnId=turn,item=dict(id='compact',type='contextCompaction'))
    event('item/completed',turnId=turn,item=dict(id='compact',type='contextCompaction'))""")
    binary.write_text(source)
    with Service(config) as service:
        start = service.start(service.lease(),goal='native-control',owner_instruction='Keep this original order until received')
        result = service.finished(start['request_id'])
        obligation, = rows(service,'SELECT * FROM android_order_obligations')
        assert obligation['original_request_id'] == start['request_id'] and obligation['lifecycle'] == 'active'
        assert obligation['original_user_words'] == 'Keep this original order until received'
        handle = service.app.state.agents.supervision.handles[('one',start['request_id'])]
        assert handle.thread == result['native_session']['thread_id'] and handle.turn == result['native_session']['turn_id']
        assert result['native_context']['compactions'] == [{'item_id':'compact','started':True,'completed':True}]
        assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 1
