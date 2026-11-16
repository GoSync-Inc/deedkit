"""Negative proof: pinned Codex 0.166 offers no protected-window barrier, so the real path refuses.

Synthetic stdio frames model official rust-v0.166.0 semantics (docs/android-protected-input.md):
dynamic tools and exec_command both take the parallel read lock, so a held protected call does
not stop a sibling command, and turn cancellation drops the pending dynamic call. No phone,
provider, model, reference or channel is used.
"""

import asyncio
import json
from pathlib import Path

import pytest

from deedkit_android.codex_session import CodexSession
from test_native_dialogue import native, messages

PROTECTED_RPC = '7'  # typed string RPC ID; must never become int 7
ORDINARY_RPC = 8


def window(tmp_path, scenario):
    """Inject one model response: a protected call beside an ordinary call and observer commands."""
    session, record = native(tmp_path)
    path = Path(session.config.binary)
    source = path.read_text()
    old = "elif mode.startswith('  Home,'): finish()"
    respond = "    elif method is None and 'id' in m:\n        if 'error' in m: finish()"
    assert old in source and respond in source
    extra = f'''elif mode == 'protected-window':
            answered = set()
            event('item/started',turnId=turn,item=dict(id='observer',type='commandExecution',command='synthetic observer'))
            if {scenario!r} in ('quiet', 'detached'):
                # A finished item proves nothing about a detached descendant or a device-side process.
                event('item/completed',turnId=turn,item=dict(id='observer',type='commandExecution',exitCode=0))
            for ident, call, operation in (({PROTECTED_RPC!r}, 'protected-call', 'request_protected_input'),
                                           ({ORDINARY_RPC!r}, 'ordinary-call', 'notify')):
                event('item/started',turnId=turn,item=dict(id=call,type='dynamicToolCall',tool='android_order_control',namespace=None))
                emit(id=ident,method='item/tool/call',params=dict(threadId=thread,turnId=turn,callId=call,namespace=None,
                     tool='android_order_control',arguments=dict(operation=operation,payload=dict(message='Owner update'))))
        ''' + old
    reply = f'''    elif method is None and 'id' in m and mode == 'protected-window':
        answered.add(m['id'])
        if answered >= {{{PROTECTED_RPC!r}, {ORDINARY_RPC!r}}} and {scenario!r} != 'cancelled':
            event('item/completed',turnId=turn,item=dict(id='observer',type='commandExecution',exitCode=0))
            finish()
''' + respond
    path.write_text(source.replace(old, extra).replace(respond, reply))
    return session, record


def wire(record, ident):
    return [r['message'] for r in messages(record)
            if 'method' not in r['message'] and type(r['message'].get('id')) is type(ident) and r['message']['id'] == ident]


# spec: ANYM-998
@pytest.mark.parametrize('scenario', ['parallel', 'quiet', 'detached'])
def test_held_native_call_is_not_a_fence_so_protected_input_refuses_with_original_ids(tmp_path, scenario):
    """No candidate signal (live sibling command, zero pending commands, completed item) admits it."""
    async def check():
        session, record = window(tmp_path, scenario)
        seen, live = [], []

        async def control(context, operation, payload):
            seen.append((context['native_request_id'], context['native_call_id'], operation))
            live.append(dict(session.pending_commands))  # what executes beside a held dynamic call
            return {'status': 'applied', 'effect': 'metadata_only'}
        await session.connect(order_control=True)
        try:
            status = await asyncio.wait_for(session.turn('protected-window', session.config, lambda e: asyncio.sleep(0),
                                            request_id='original-task', control=control, admit=lambda: None), 5)
            assert status == 'completed'
            # The ordinary sibling still runs exactly once: 0.166 parallel dispatch continued meanwhile.
            assert seen == [(ORDINARY_RPC, 'ordinary-call', 'notify')]
            if scenario == 'parallel':
                assert list(live[0].values()) == ['original-task']
            else:
                assert live == [{}]  # zero tracked commands is still not isolation
            refusal, = wire(record, PROTECTED_RPC)
            assert refusal['error']['message'] == 'protected_input_unavailable'
            assert 'result' not in refusal
            receipt, = session.receipts('original-task')
            assert receipt['native_call_id'] == 'ordinary-call' and receipt['native_request_id'] == ORDINARY_RPC
            assert 'request_protected_input' not in session.journal.read_text()
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-998
def test_cancellation_and_restart_never_resume_or_reanswer_the_refused_protected_call(tmp_path):
    """Interrupt drops 0.166 pending dynamic calls; the host keeps one refusal and no resumable row."""
    async def check():
        session, record = window(tmp_path, 'cancelled')
        dispatched = asyncio.Event()

        async def control(context, operation, payload):
            dispatched.set()
            return {'status': 'applied', 'effect': 'metadata_only'}
        await session.connect(order_control=True)
        turn = asyncio.create_task(session.turn('protected-window', session.config, lambda e: asyncio.sleep(0),
                                                request_id='original-task', control=control, admit=lambda: None))
        await asyncio.wait_for(dispatched.wait(), 5)
        for _ in range(200):
            if wire(record, PROTECTED_RPC) and wire(record, ORDINARY_RPC):
                break
            await asyncio.sleep(.01)
        await session.interrupt()
        assert await asyncio.wait_for(turn, 5) == 'interrupted'
        before = session.receipts('original-task')
        assert [r['native_call_id'] for r in before] == ['ordinary-call']
        await session.close()
        resumed = CodexSession(session.dir, session.config, session.host, session.device,
                               bindings=session.bindings, owner=session.owner)
        await resumed.connect()
        try:
            assert resumed.receipts('original-task') == before
            refusal, = wire(record, PROTECTED_RPC)  # exactly once, original string type, never a result
            assert refusal['error']['message'] == 'protected_input_unavailable'
            assert len(wire(record, ORDINARY_RPC)) == 1
            assert 'request_protected_input' not in json.dumps(json.loads(resumed.journal.read_text()))
        finally:
            await resumed.close()
    asyncio.run(check())
