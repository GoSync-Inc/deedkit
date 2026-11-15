"""Lazy native rollouts: settings evidence must not resume a never-materialized thread."""

import asyncio
from pathlib import Path

import pytest

from deedkit_android.config import Refused
from test_native_dialogue import native, messages


def lazy(tmp_path, *, change=None, delay=.02, disconnect=False, omit=False, foreign=False):
    session, record = native(tmp_path)
    binary = Path(session.config.binary)
    source = binary.read_text().replace('import json, os, sys', 'import json, os, sys, threading, time')
    source = source.replace("selected = dict(model='gpt-6.1-sol',reasoningEffort='medium',serviceTier='ultrafast')",
                            "selected = dict(model='gpt-6.1-sol',reasoningEffort='medium',serviceTier=None)")
    source = source.replace("elif method in ('thread/start','thread/resume'):", """elif method == 'thread/resume' and count == 0:
        emit(id=m['id'],error=dict(code=-32600,message='no rollout found for thread id '+thread))
    elif method in ('thread/start','thread/resume'):""")
    begin = source.index("    elif method == 'thread/settings/update':")
    end = source.index("    elif method == 'turn/start':",begin)
    source = source[:begin]+f"""    elif method == 'thread/settings/update':
        previous=dict(selected)
        selected=dict(model=p['model'],reasoningEffort=p['effort'],serviceTier=None if p['serviceTier']=='default' else p['serviceTier'])
        emit(id=m['id'],result={{}})
        if {disconnect!r}: raise SystemExit
        def applied():
            time.sleep({delay!r})
            receipt=dict(model=selected['model'],effort=selected['reasoningEffort'],serviceTier=selected['serviceTier'],
                         approvalPolicy='never',cwd=os.getcwd(),sandboxPolicy=dict(type='workspaceWrite',
                         writableRoots=[os.getcwd()],networkAccess=True,excludeTmpdirEnvVar=True,excludeSlashTmp=True))
            receipt.update({change or {}!r})
            if {foreign!r}:
                emit(method='thread/settings/updated',params=dict(threadId='foreign',threadSettings=receipt))
            else: event('thread/settings/updated',threadSettings=receipt)
        if previous != selected and not {omit!r}: threading.Thread(target=applied,daemon=True).start()
"""+source[end:]
    binary.write_text(source)
    return session, record


# spec: ANYM-993
def test_first_goal_waits_for_positive_applied_settings_without_resuming_or_materializing_rollout(tmp_path):
    async def check():
        session, record = lazy(tmp_path)
        await session.connect(order_control=True)
        thread = session.thread_id
        try:
            assert await session.turn('  Home, first',session.config,lambda e: asyncio.sleep(0),request_id='original') == 'completed'
            assert session.selection['source'] == 'thread/settings/updated'
            assert session.selection['effective'] == {'model':'gpt-6.1-sol','effort':'medium','service_tier':'ultrafast'}
            assert session.selection['configuration_status'] == 'confirmed' and session.policy_proven
            assert session.order_control_registered and session.thread_id == thread
            assert session.selection['provider_tier_observed'] is False
            assert await session.turn('  Home, second',session.config,lambda e: asyncio.sleep(0),request_id='second') == 'completed'
            wire = [r['message'] for r in messages(record)]
            assert not any(m.get('method')=='thread/resume' for m in wire)
            assert sum(m.get('method')=='thread/start' for m in wire)==1
            assert sum(m.get('method')=='thread/settings/update' for m in wire)==1  # unchanged native settings emit no event
            assert sum(m.get('method')=='turn/start' for m in wire)==2
            assert len({r['pid'] for r in messages(record)})==1
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993
@pytest.mark.parametrize('change', [
    {'model':'gpt-6.1-terra'}, {'effort':'low'}, {'serviceTier':None}, {'approvalPolicy':'on-request'},
    {'cwd':'/foreign'}, {'sandboxPolicy':{'type':'dangerFullAccess'}}, {'effort':None},
])
def test_first_goal_never_uses_unconfirmed_or_relaxed_applied_receipt(tmp_path,change):
    async def check():
        session, record = lazy(tmp_path,change=change)
        await session.connect(order_control=True)
        thread = session.thread_id
        committed = []
        try:
            with pytest.raises(Refused,match='native_selection_unconfirmed'):
                await session.turn('  Home, must not send',session.config,lambda e: asyncio.sleep(0),request_id='original',
                                   before_submit=lambda: committed.append('input-intent'))
            assert session.selection['configuration_status']=='unconfirmed'
            assert committed == []  # refusal is before the original goal intent/bytes, not inferred from command counts
            assert session.thread_id==thread and session.order_control_registered
            wire = [r['message'] for r in messages(record)]
            assert not any(m.get('method') in ('thread/resume','turn/start') for m in wire)
            assert sum(m.get('method')=='thread/settings/update' for m in wire)==1
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993
@pytest.mark.parametrize('lost', ['missing', 'foreign'])
def test_unconfirmed_first_settings_mutation_is_not_repeated_by_a_later_request(tmp_path,lost):
    async def check():
        session,record=lazy(tmp_path,omit=lost=='missing',foreign=lost=='foreign')
        await session.connect(order_control=True)
        thread=session.thread_id
        committed=[]
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(session.turn('  Home, original',session.config,lambda e: asyncio.sleep(0),
                    request_id='original',before_submit=lambda: committed.append('input-intent')),.15)
            assert session.settings_pending and session.settings_snapshot['service_tier'] is None
            with pytest.raises(Refused,match='native_selection_unconfirmed'):
                await session.turn('  Home, different',session.config.model_copy(update={'reasoning_effort':'high'}),
                                   lambda e: asyncio.sleep(0),request_id='different')
            assert session.selection['requested']['effort']=='high' and session.selection['configuration_status']=='unconfirmed'
            assert session.thread_id==thread and session.order_control_registered and not session.disconnected
            assert committed==[]
            wire=[r['message'] for r in messages(record)]
            assert sum(m.get('method')=='thread/settings/update' for m in wire)==1
            assert not any(m.get('method') in ('thread/resume','turn/start','turn/interrupt') for m in wire)
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993
def test_first_goal_settings_disconnect_keeps_original_creation_binding_and_sends_no_goal(tmp_path):
    async def check():
        session,record=lazy(tmp_path,disconnect=True)
        await session.connect(order_control=True)
        thread=session.thread_id
        try:
            with pytest.raises(OSError):
                await session.turn('  Home, original',session.config,lambda e: asyncio.sleep(0),request_id='original')
            assert session.thread_id==thread and session.selection['effective'] is None
            row=session.bindings.native_binding(session.owner,session.device)
            assert row['thread']==thread and row['boundary']
            wire=[r['message'] for r in messages(record)]
            assert not any(m.get('method') in ('thread/resume','turn/start') for m in wire)
        finally:
            await session.close()
    asyncio.run(check())
