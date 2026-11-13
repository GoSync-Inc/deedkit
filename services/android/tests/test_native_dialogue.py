"""Pinned native request/reply/steer shapes on synthetic stdio, without a phone or provider."""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from agent_harness import Service, write_config
from deedkit_android.codex_session import CodexSession
from deedkit_android.config import Config, Refused


def native(tmp_path, *, policy='never', version='0.166.0', models=None, effective=None, delayed_settings=False, disconnect_before_applied=False):
    if models is None:
        models = [dict(id=name, model=name, supportedReasoningEfforts=[{'reasoningEffort': e} for e in ('minimal','low','medium','high')],serviceTiers=[{'id':'ultrafast'}]) for name in ('gpt-6.1-sol','gpt-6.1-terra')]
    record = tmp_path / 'wire.jsonl'
    binary = tmp_path / 'native'
    binary.write_text(f'''#!{sys.executable}
import json, os, sys
if sys.argv[1:] == ['--version']:
    print({version!r}); raise SystemExit
thread, turn, mode, count = 'thread-' + str(os.getpid()), None, None, 0
selected = dict(model='gpt-6.1-sol',reasoningEffort='medium',serviceTier='ultrafast')
pending_selection = None
def emit(**body): print(json.dumps(body), flush=True)
def event(method, **params): emit(method=method, params=dict(threadId=thread, **params))
def finish():
    event('item/completed', turnId=turn, item=dict(id='final', type='agentMessage', phase='final_answer', text='ordinary reply'))
    event('turn/completed', turn=dict(id=turn, status='completed', error=None))
for line in sys.stdin:
    m=json.loads(line)
    with open({str(record)!r}, 'a') as f: f.write(json.dumps(dict(pid=os.getpid(), message=m))+'\\n')
    method, p=m.get('method'), m.get('params', {{}})
    if method == 'initialize': emit(id=m['id'], result={{}})
    elif method in ('thread/start','thread/resume'):
        thread=p.get('threadId',thread); emit(id=m['id'], result=dict(thread=dict(id=thread), **selected, **({{'approvalPolicy':{policy!r}}} if {policy!r} is not None else {{}})))
        if pending_selection is not None:
            if {disconnect_before_applied!r}: raise SystemExit
            selected,pending_selection=pending_selection,None
            event('thread/settings/updated',threadSettings=dict(model=selected['model'],effort=selected['reasoningEffort'],serviceTier=selected['serviceTier'],approvalPolicy={policy!r}))
    elif method == 'model/list': emit(id=m['id'],result=dict(data={models!r},nextCursor=None))
    elif method == 'thread/settings/update':
        updated=dict(model=p['model'],reasoningEffort=p['effort'],serviceTier=None if p['serviceTier']=='default' else p['serviceTier'])
        updated.update({effective!r} or {{}})
        if {delayed_settings!r}:
            pending_selection=updated
        else:
            selected=updated
        emit(id=m['id'],result={{}})
        if not {delayed_settings!r}: event('thread/settings/updated',threadSettings=dict(model=selected['model'],effort=selected['reasoningEffort'],serviceTier=selected['serviceTier'],approvalPolicy={policy!r}))
    elif method == 'turn/start':
        count+=1; turn='turn-'+str(os.getpid())+'-'+str(count); mode=p['input'][0]['text']
        if 'Caller input (nonsecret JSON):\\n' in mode:
            mode=json.loads(mode.split('Caller input (nonsecret JSON):\\n',1)[1])['goal']
        if mode == 'LOSE_ANSWER_AFTER_WRITE': raise SystemExit
        emit(id=m['id'], result=dict(turn=dict(id=turn)))
        event('turn/started', turn=dict(id=turn,status='inProgress'))
        event('turn/completed', turn=dict(id='unrelated-old-turn',status='completed'))
        if mode == 'policy-changed': event('thread/settings/updated',threadSettings=dict(approvalPolicy='on-request'))
        if mode == 'reroute':
            event('model/rerouted',turnId=turn,fromModel='gpt-6.1-sol',toModel='gpt-6.1-terra',reason='highRiskCyberActivity')
        elif mode.startswith('  Home,'): finish()
        elif mode == 'mcp-approval' and {policy!r} == 'never':
            # Pinned0.166 mcp_tool_call.rs1655 refuses BEFORE request_user_input dispatch.
            event('item/completed',turnId=turn,item=dict(id='mcp',type='mcpToolCall',status='failed')); finish()
        elif mode.startswith('async-'):
            event('item/completed',turnId=turn,item=dict(id='async-item',type='agentMessage',phase='final_answer',
                  text='Which address?',delivery='async',questions=[dict(title='Which address?',options=['Home','Office'])]))
            if mode == 'async-closed': finish()
        elif mode in ('steer','steer-stall','steer-wrong'):
            event('item/started', turnId=turn, item=dict(id='command',type='commandExecution',command='synthetic only'))
        else:
            if mode == 'mid-question':
                event('item/started',turnId=turn,item=dict(id='observation',type='commandExecution',command='synthetic observation'))
                event('item/completed',turnId=turn,item=dict(id='observation',type='commandExecution',exitCode=0))
            rid='12' if mode == 'string-id' else 12
            kind={{'approval':'item/commandExecution/requestApproval','file-approval':'item/fileChange/requestApproval',
                  'permissions':'item/permissions/requestApproval','elicitation':'mcpServer/elicitation/request'}}.get(mode,'item/tool/requestUserInput')
            questions=[dict(id='address',header='Address',question='Which address?',options=None,isSecret=mode=='secret'),
                       dict(id='choice',header='Choice',question='Which shop?',options=None)]
            emit(id=rid,method=kind,params=dict(threadId=thread,turnId=turn,itemId='question-item',isBlocking=True,
                 autoResolutionMs=0 if mode=='expired' else None,questions=questions))
            if mode == 'cleared': event('serverRequest/resolved',requestId=rid)
    elif method == 'turn/steer':
        if mode in ('steer-stall','async-stall'): continue
        emit(id=m['id'], result=dict(turnId='unrelated' if mode=='steer-wrong' else turn))
        event('item/completed',turnId=turn,item=dict(id='command',type='commandExecution',exitCode=0))
        finish()
    elif method == 'turn/interrupt':
        emit(id=m['id'], result={{}})
        event('serverRequest/resolved',requestId='12' if mode=='string-id' else 12)
        event('turn/completed',turn=dict(id=turn,status='interrupted',error=None))
    elif method is None and 'id' in m:
        if 'error' in m: finish()
        elif mode == 'disconnect': raise SystemExit
        else:
            event('serverRequest/resolved',requestId=m['id'])
            event('item/started',turnId=turn,item=dict(id='selection',type='commandExecution',command='synthetic selection'))
            event('item/completed',turnId=turn,item=dict(id='selection',type='commandExecution',exitCode=0))
            finish()
''')
    binary.chmod(0o755)
    path = write_config(tmp_path, codex=binary, default='codex')
    body=json.loads(path.read_text())
    body['engines']['codex']['version']=version
    path.write_text(json.dumps(body))
    config = Config.model_validate(body)
    return CodexSession(tmp_path / 'conversation', config.engines['codex'], config, 'home'), record


def messages(record):
    return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []


async def start(session, mode, request_id='original-run'):
    queue = asyncio.Queue()
    task = asyncio.create_task(session.turn(mode, session.config, queue.put, request_id=request_id))
    while True:
        event = await asyncio.wait_for(queue.get(), 2)
        if 'id' in event or (mode.startswith('steer') and event.get('method') == 'item/started'):
            return task, event, queue


def binding(session, request):
    p = request['params']
    return dict(request_id='original-run', thread_id=p['threadId'], turn_id=p['turnId'],
                native_request_id=request['id'], item_id=p['itemId'])


# spec: ANYM-993
def test_question_event_is_owner_scoped_in_existing_http_cursor(tmp_path):
    native(tmp_path)
    path = tmp_path / 'config.json'
    config = json.loads(path.read_text())
    config['agent'].update(guarded=False, persistent=True)
    path.write_text(json.dumps(config))
    with Service(path) as service:
        run = service.start(service.lease())
        deadline = time.monotonic() + 3
        while True:
            view = service.get(run['request_id'], wait_s=1)
            questions = [event for event in view['events'] if event['kind'] == 'question']
            if questions:
                break
            assert time.monotonic() < deadline
        question, = questions
        assert question['native_request_id'] == 12 and type(question['native_request_id']) is int
        assert question['item_id'] == 'question-item' and question['questions'][0]['id'] == 'address'
        assert view['request_id'] == run['request_id'] and view['state'] == 'running'
        service.get(run['request_id'], key='two', code=404)
        assert service.phone.commands == []
        assert service.post('agent', {'action': 'stop', 'request_id': run['request_id']})['state'] == 'stopped'


# spec: ANYM-993
@pytest.mark.parametrize('mode', ['question', 'string-id', 'mid-question'])
def test_native_question_precedes_selection_and_exact_reply_is_sent_once(tmp_path, mode):
    async def check():
        session, record = native(tmp_path)
        await session.connect()
        task = None
        try:
            task, question, _ = await start(session, mode)
            assert question['method'] == 'item/tool/requestUserInput'
            assert question['params']['questions'][0]['options'] is None
            assert not task.done() and not session.done.is_set()  # unrelated old completion was ignored
            pending, = session.receipts('original-run')
            assert pending['status'] == 'pending' and pending['submission_intent'] is False
            assert not any(r['message'].get('result', {}).get('answers') for r in messages(record))
            kwargs = {**binding(session, question), 'answers': {'address': ['Apartment two'], 'choice': ['Shop A']}}
            first, second = await asyncio.gather(session.reply(**kwargs), session.reply(**kwargs))
            assert first['status'] == second['status'] == 'unknown'  # no native acceptance ack for answer contents
            assert first['submission_intent'] is second['submission_intent'] is True
            assert await task == 'completed'
            wire = [r['message'] for r in messages(record)]
            replies = [m for m in wire if m.get('result', {}).get('answers')]
            assert replies == [{'id': question['id'], 'result': {'answers': {
                'address': {'answers': ['Apartment two']}, 'choice': {'answers': ['Shop A']}}}}]
            assert len({r['pid'] for r in messages(record)}) == 1
            assert not session.journal.is_relative_to(session.dir)  # native workspace cannot author its own receipts
            journal = session.journal.read_text()
            assert 'Apartment two' not in journal and 'Which address?' not in journal
        finally:
            await session.close()
            if task:
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())


# spec: ANYM-993
@pytest.mark.parametrize('mode', ['approval', 'file-approval', 'permissions', 'elicitation', 'secret'])
def test_server_approvals_elicitation_and_secret_questions_are_never_answered_as_clarification(tmp_path, mode):
    async def check():
        session, record = native(tmp_path)
        await session.connect()
        events = []
        async def consume(event):
            events.append(event)
        try:
            assert await session.turn(mode, session.config, consume, request_id='original-run') == 'completed'
            replies = [r['message'] for r in messages(record) if r['message'].get('id') == 12]
            assert len(replies) == 1 and 'error' in replies[0]
            assert not any(e.get('method') == 'item/tool/requestUserInput' for e in events)
            assert session.receipts('original-run') == []
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993
def test_cancelled_question_and_restart_cannot_replay_old_answer(tmp_path):
    async def check():
        session, record = native(tmp_path)
        await session.connect()
        task, question, _ = await start(session, 'question')
        args = {**binding(session, question), 'answers': {'address': ['two'], 'choice': ['shop']}}
        await session.interrupt()
        assert await task == 'interrupted'
        with pytest.raises(Refused, match='question_expired'):
            await session.reply(**args)
        thread_id = session.thread_id
        await session.close()
        resumed = CodexSession(session.dir, session.config, session.host, session.device)
        await resumed.connect()
        try:
            assert resumed.thread_id == thread_id
            with pytest.raises(Refused, match='question_expired'):
                await resumed.reply(**args)
            assert resumed.receipts('original-run')[0]['native_state'] == 'cancelled'
            assert not any(r['message'].get('result', {}).get('answers') for r in messages(record))
            assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 1
        finally:
            await resumed.close()
    asyncio.run(check())


# spec: ANYM-993
def test_lost_answer_outcome_survives_restart_without_resend_or_false_acceptance(tmp_path):
    async def check():
        session, record = native(tmp_path)
        await session.connect()
        task, question, _ = await start(session, 'disconnect')
        args = {**binding(session, question), 'answers': {'address': ['two'], 'choice': ['shop']}}
        assert (await session.reply(**args))['status'] == 'unknown'
        with pytest.raises(OSError):
            await task
        await session.close()
        resumed = CodexSession(session.dir, session.config, session.host, session.device)
        await resumed.connect()
        try:
            assert (await resumed.reply(**args))['status'] == 'unknown'
            changed = {**args, 'answers': {'address': ['changed'], 'choice': ['shop']}}
            with pytest.raises(Refused, match='native_reply_conflict'):
                await resumed.reply(**changed)
            replies = [r for r in messages(record) if r['message'].get('result', {}).get('answers')]
            assert len(replies) == 1
            assert any(r['message'].get('method') == 'thread/resume' for r in messages(record))
        finally:
            await resumed.close()
    asyncio.run(check())


# spec: ANYM-993
def test_uncertain_steer_cancellation_prevents_retry_and_new_id_bypass(tmp_path):
    async def check():
        session, record = native(tmp_path)
        await session.connect()
        task = None
        try:
            task, _, _ = await start(session, 'steer-stall')
            args = dict(request_id='original-run', thread_id=session.thread_id, turn_id=session.turn_id,
                        message_id='original-steer-id', text='Read existing order only.')
            steering = asyncio.create_task(session.steer(**args))
            async with asyncio.timeout(2):
                while not any(r['message'].get('method') == 'turn/steer' for r in messages(record)):
                    await asyncio.sleep(.005)
            steering.cancel()
            with pytest.raises(asyncio.CancelledError):
                await steering
            assert (await session.steer(**args))['status'] == 'unknown'
            with pytest.raises(Refused, match='native_input_unresolved'):
                await session.steer(**{**args, 'message_id': 'new-bypass-id'})
            assert len([r for r in messages(record) if r['message'].get('method') == 'turn/steer']) == 1
            await session.interrupt()
            assert await task == 'interrupted'
        finally:
            await session.close()
            if task:
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())


# spec: ANYM-993
def test_mismatched_steer_receipt_never_becomes_accepted(tmp_path):
    async def check():
        session, _ = native(tmp_path)
        await session.connect()
        task = None
        try:
            task, _, _ = await start(session, 'steer-wrong')
            result = await session.steer(request_id='original-run', thread_id=session.thread_id,
                turn_id=session.turn_id, message_id='original-message', text='Read status.')
            assert result['status'] == 'unknown'
            assert await task == 'completed'  # unrelated status/progress is still consumed normally
        finally:
            await session.close()
            if task:
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())


# spec: ANYM-993
def test_closed_input_refuses_before_creating_a_reply_intent(tmp_path):
    async def check():
        session, record = native(tmp_path)
        await session.connect()
        task = None
        try:
            task, question, _ = await start(session, 'question')
            session.process.stdin.close()
            with pytest.raises(Refused, match='native_session_disconnected'):
                await session.reply(**binding(session, question), answers={'address': ['two'], 'choice': ['shop']})
            assert all('digest' not in row for row in session.interactions.values())
            assert not any(r['message'].get('result', {}).get('answers') for r in messages(record))
        finally:
            await session.close()
            if task:
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())


# spec: ANYM-993
def test_concurrent_turns_are_serial_and_other_conversation_cannot_answer(tmp_path):
    async def check():
        session, record = native(tmp_path)
        other_dir = tmp_path / 'unrelated-owner-device'
        other_dir.mkdir()
        other, _ = native(other_dir)
        await session.connect()
        await other.connect()
        first = second = None
        try:
            first, question, _ = await start(session, 'question')
            kwargs = {**binding(session, question), 'answers': {'address': ['two'], 'choice': ['shop']}}
            with pytest.raises(Refused):
                await other.reply(**kwargs)
            queue = asyncio.Queue()
            second = asyncio.create_task(session.turn('question', session.config, queue.put, request_id='second-run'))
            await asyncio.sleep(.02)
            assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 1
            await session.reply(**kwargs)
            assert await first == 'completed'
            while True:
                question2 = await asyncio.wait_for(queue.get(), 2)
                if 'id' in question2:
                    break
            assert question2['params']['turnId'] != question['params']['turnId']
            kwargs2 = {**binding(session, question2), 'request_id': 'second-run',
                       'answers': {'address': ['three'], 'choice': ['shop']}}
            await session.reply(**kwargs2)
            assert await second == 'completed'
            assert len({r['pid'] for r in messages(record)}) == 1
        finally:
            await session.close()
            await other.close()
            await asyncio.gather(*[t for t in (first, second) if t], return_exceptions=True)
    asyncio.run(check())


# spec: ANYM-993
@pytest.mark.parametrize('change', ['request_id', 'thread_id', 'turn_id', 'native_request_id', 'item_id', 'answers'])
def test_reply_mismatches_never_reach_native(tmp_path, change):
    async def check():
        session, record = native(tmp_path)
        await session.connect()
        task = None
        try:
            task, question, _ = await start(session, 'question')
            kwargs = {**binding(session, question), 'answers': {'address': ['two'], 'choice': ['shop']}}
            kwargs[change] = {'foreign-question': ['two']} if change == 'answers' else (
                '12' if change == 'native_request_id' else 'unrelated')
            with pytest.raises(Refused):
                await session.reply(**kwargs)
            assert not any(r['message'].get('result', {}).get('answers') for r in messages(record))
            await session.interrupt()
            assert await task == 'interrupted'
        finally:
            await session.close()
            if task:
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())


# spec: ANYM-993
@pytest.mark.parametrize('mode', ['expired', 'cleared'])
def test_expired_or_cleared_question_refuses_late_reply(tmp_path, mode):
    async def check():
        session, record = native(tmp_path)
        await session.connect()
        task = None
        try:
            task, question, queue = await start(session, mode)
            if mode == 'cleared':
                while (await asyncio.wait_for(queue.get(), 2)).get('method') != 'serverRequest/resolved':
                    pass
            with pytest.raises(Refused, match='question_expired'):
                await session.reply(**binding(session, question), answers={'address': ['two'], 'choice': ['shop']})
            assert not any(r['message'].get('result', {}).get('answers') for r in messages(record))
        finally:
            await session.interrupt()
            await session.close()
            if task:
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())


# spec: ANYM-993
def test_mid_turn_steer_preserves_process_and_uses_expected_original_turn(tmp_path):
    async def check():
        session, record = native(tmp_path)
        await session.connect()
        task = None
        try:
            task, _, _ = await start(session, 'steer')
            args = dict(request_id='original-run', thread_id=session.thread_id, turn_id=session.turn_id,
                        message_id='original-steer-id', text='Read only; keep the existing order.')
            first, second = await asyncio.gather(session.steer(**args), session.steer(**args))
            assert first['status'] == second['status'] == 'accepted'
            assert await task == 'completed'
            steering, = [r['message'] for r in messages(record) if r['message'].get('method') == 'turn/steer']
            assert steering['params'] == dict(threadId=args['thread_id'], expectedTurnId=args['turn_id'],
                                              clientUserMessageId=args['message_id'], input=[{'type': 'text', 'text': args['text']}])
            assert len({r['pid'] for r in messages(record)}) == 1
        finally:
            await session.close()
            if task:
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())


# spec: ANYM-993
@pytest.mark.parametrize('policy', [None, 'on-request'])
def test_shared_question_method_requires_native_effective_never_receipt(tmp_path, policy):
    async def check():
        session, record = native(tmp_path, policy=policy)
        await session.connect()
        events = []
        try:
            with pytest.raises(Refused,match='native_selection_unconfirmed'):
                await session.turn('mcp-approval',session.config,lambda e: collect(events,e),request_id='original-run')
            assert not any(e.get('method') == 'item/tool/requestUserInput' for e in events)
            assert session.receipts('original-run') == []
            assert not any(r['message'].get('result', {}).get('answers') for r in messages(record))
        finally:
            await session.close()
    asyncio.run(check())


async def collect(events, event):
    events.append(event)


# spec: ANYM-993
def test_pinned_never_denies_mcp_before_shared_method_and_does_not_classify_option_labels(tmp_path):
    async def check():
        session, record = native(tmp_path)
        await session.connect()
        try:
            events = []
            assert await asyncio.wait_for(session.turn('mcp-approval', session.config, lambda e: collect(events, e), request_id='original-run'), 2) == 'completed'
            assert session.receipts('original-run') == []
            task, event, _ = await start(session, 'question')
            event['params']['questions'][0]['options'] = [{'label': 'Accept', 'description': 'ordinary shop choice'}]
            receipt = await session.reply(**binding(session, event), answers={'address': ['Accept'], 'choice': ['Cancel']})
            assert receipt['status'] == 'unknown'
            assert await task == 'completed'
            methods = [r['message'] for r in messages(record)]
            assert all(m['params']['approvalPolicy'] == 'never' for m in methods if m.get('method') in ('thread/start','turn/start'))
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993
@pytest.mark.parametrize('mode', ['policy-changed', 'question'])
def test_changed_effective_policy_or_unproven_native_version_refuses_shared_method(tmp_path, mode):
    async def check():
        session, record = native(tmp_path, version='0.166.0' if mode == 'policy-changed' else '9.9.9')
        if mode == 'question':
            with pytest.raises(Refused,match='native_version_unsupported'):
                await session.connect()
            assert messages(record) == []
            return
        await session.connect()
        try:
            events = []
            assert await asyncio.wait_for(session.turn(mode, session.config, lambda e: collect(events, e), request_id='original-run'), 2) == 'completed'
            assert session.receipts('original-run') == []
            assert not any(r['message'].get('result', {}).get('answers') for r in messages(record))
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993
def test_resumed_thread_requires_its_own_native_effective_policy_receipt(tmp_path):
    async def check():
        session, record = native(tmp_path)
        await session.connect()
        original_thread = session.thread_id
        await session.close()
        binary = session.config.binary
        source = Path(binary).read_text()
        assert "'approvalPolicy':'never'" in source
        Path(binary).write_text(source.replace("'approvalPolicy':'never'", "'approvalPolicy':'on-request'"))
        resumed = CodexSession(session.dir, session.config, session.host, session.device)
        await resumed.connect()
        try:
            assert resumed.thread_id == original_thread
            events = []
            with pytest.raises(Refused,match='native_selection_unconfirmed'):
                await resumed.turn('question',resumed.config,lambda e: collect(events,e),request_id='original-run')
            assert resumed.receipts('original-run') == []
            request = [r['message'] for r in messages(record) if r['message'].get('method') == 'thread/resume' and 'approvalPolicy' in r['message']['params']][-1]
            assert request['params']['approvalPolicy'] == 'never'
        finally:
            await resumed.close()
    asyncio.run(check())


# spec: ANYM-993
def test_unknown_async_answer_cannot_be_bypassed_through_new_steer_identity(tmp_path):
    async def check():
        session, record = native(tmp_path)
        await session.connect()
        queue=asyncio.Queue()
        task=asyncio.create_task(session.turn('async-stall',session.config,queue.put,request_id='original-run'))
        try:
            while True:
                event=await asyncio.wait_for(queue.get(),2)
                if (event.get('params',{}).get('item') or {}).get('id') == 'async-item':
                    break
            answer=asyncio.create_task(session.reply_async(request_id='original-run',thread_id=session.thread_id,
                turn_id=session.turn_id,item_id='async-item',message_id='original-answer',text='Home',
                admit=lambda _:None,resume=None))
            async def sent():
                while not any(r['message'].get('method') == 'turn/steer' for r in messages(record)):
                    await asyncio.sleep(.005)
            await asyncio.wait_for(sent(),2)
            answer.cancel()
            await asyncio.gather(answer,return_exceptions=True)
            with pytest.raises(Refused,match='native_input_unresolved'):
                await session.steer(request_id='original-run',thread_id=session.thread_id,turn_id=session.turn_id,
                                    message_id='replacement-route',text='Home')
            assert len([r for r in messages(record) if r['message'].get('method') == 'turn/steer']) == 1
            assert session.receipts('original-run')[0]['status'] == 'unknown'
        finally:
            await session.close()
            await asyncio.gather(task,return_exceptions=True)
    asyncio.run(check())
