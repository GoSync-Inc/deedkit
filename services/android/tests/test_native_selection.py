"""Native catalog and effective selection receipts on a synthetic pinned protocol boundary."""

import asyncio

import pytest

from deedkit_android.config import Refused
from test_native_dialogue import native, messages


def turns(record):
    return [r['message'] for r in messages(record) if r['message'].get('method') == 'turn/start']


# spec: ANYM-993
@pytest.mark.parametrize('missing', ['model', 'effort', 'tier'])
def test_unsupported_selection_refuses_before_goal_without_upgrade_or_tier_fallback(tmp_path,missing):
    async def check():
        model=dict(id='gpt-6.1-sol',model='gpt-6.1-sol',upgrade='gpt-6.1-terra',
                   supportedReasoningEfforts=[{'reasoningEffort':'medium'}],serviceTiers=[{'id':'ultrafast'}])
        if missing=='model':
            model.update(id='gpt-6.1-terra',model='gpt-6.1-terra')
        elif missing=='effort':
            model['supportedReasoningEfforts']=[{'reasoningEffort':'high'}]
        else:
            model['serviceTiers']=[{'id':'fast'}]
        session,record=native(tmp_path,models=[model])
        await session.connect()
        try:
            with pytest.raises(Refused,match='native_.*unsupported'):
                await session.turn('  Home, selection',session.config,lambda e: asyncio.sleep(0),request_id='original')
            assert turns(record)==[]
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993
@pytest.mark.parametrize('effective', [{'model':'gpt-6.1-terra'},{'reasoningEffort':'low'},{'serviceTier':None}])
def test_missing_or_changed_effective_receipt_refuses_before_goal(tmp_path,effective):
    async def check():
        session,record=native(tmp_path,effective=effective)
        await session.connect()
        try:
            with pytest.raises(Refused,match='native_selection_unconfirmed'):
                await session.turn('  Home, selection',session.config,lambda e: asyncio.sleep(0),request_id='original')
            assert turns(record)==[]
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993
def test_native_catalog_and_actual_thread_receipt_confirm_config_without_inventing_served_tier(tmp_path):
    async def check():
        session,record=native(tmp_path)
        await session.connect()
        original_thread=session.thread_id
        try:
            assert await session.turn('  Home, selection',session.config,lambda e: asyncio.sleep(0),request_id='original')=='completed'
            receipt=session.selection
            assert receipt['requested']=={'model':'gpt-6.1-sol','effort':'medium','service_tier':'ultrafast'}
            assert receipt['effective']==receipt['requested']
            assert receipt['configuration_status']=='confirmed'
            assert receipt['provider_service_tier'] is None and receipt['provider_tier_observed'] is False
            assert receipt['source']=='thread/resume' and receipt['native_thread_id']==original_thread
            seen=[r['message'] for r in messages(record)]
            resume=[m for m in seen if m.get('method')=='thread/resume'][-1]
            assert resume['params']=={'threadId':original_thread,'excludeTurns':True}
            assert turns(record)[0]['params']['serviceTier']=='ultrafast'
            assert len({r['pid'] for r in messages(record)})==1
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993
def test_unsupported_next_selection_preserves_pending_async_obligation_and_same_conversation(tmp_path):
    async def check():
        session,record=native(tmp_path)
        await session.connect()
        try:
            await session.turn('async-closed',session.config,lambda e: asyncio.sleep(0),request_id='original')
            original_thread=session.thread_id
            with pytest.raises(Refused,match='native_model_unsupported'):
                await session.turn('  Home, selection',session.config.model_copy(update={'model':'missing'}),
                                   lambda e: asyncio.sleep(0),request_id='new-request')
            assert session.receipts('original')[0]['native_state']=='waiting'
            assert len(turns(record))==1 and session.thread_id==original_thread
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993
def test_new_turn_overrides_are_receipted_on_same_thread_without_catalog_default_or_upgrade(tmp_path):
    async def check():
        session,record=native(tmp_path)
        await session.connect()
        thread=session.thread_id
        try:
            async def consume(event):
                await asyncio.sleep(0)
            await session.turn('  Home, first',session.config,consume,request_id='first')
            override=session.config.model_copy(update={'model':'gpt-6.1-terra','reasoning_effort':'high','service_tier':'default'})
            await session.turn('  Home, next',override,consume,request_id='second')
            assert session.selection['configuration_status']=='confirmed'
            assert session.selection['requested']=={'model':'gpt-6.1-terra','effort':'high','service_tier':'default'}
            assert session.selection['effective']=={'model':'gpt-6.1-terra','effort':'high','service_tier':None}
            assert all(m['params']['threadId']==thread for m in turns(record))
            assert len({r['pid'] for r in messages(record)})==1
            assert len([r for r in messages(record) if r['message'].get('method')=='thread/start'])==1
            initialize=next(r['message'] for r in messages(record) if r['message'].get('method')=='initialize')
            assert initialize['params']['capabilities']=={'experimentalApi':True}
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993
def test_public_target_and_effective_receipt_preserve_unknown_provider_tier(tmp_path):
    import json
    from agent_harness import Service
    native(tmp_path)
    config=tmp_path/'config.json'
    body=json.loads(config.read_text())
    body['agent'].update(guarded=False,persistent=True)
    config.write_text(json.dumps(body))
    with Service(config) as service:
        lease=service.lease()
        capabilities=service.post('capabilities',{})['agent']
        assert capabilities['default_model']=='gpt-6.1-sol' and capabilities['default_service_tier']=='ultrafast'
        assert capabilities['engines']['codex']['model']=='gpt-6.1-sol'
        result=service.finished(service.start(lease,goal='  Home, selection',service_tier='ultrafast')['request_id'])
        assert result['state']=='succeeded'
        receipt=result['native_selection']
        assert receipt['configuration_status']=='confirmed' and receipt['provider_tier_observed'] is False
        assert receipt['provider_service_tier'] is None
        assert any(e['kind']=='selection' for e in result['events'])
        failed=service.finished(service.start(lease,goal='  Home, unsupported',service_tier='fast')['request_id'])
        assert failed['state']=='failed' and failed['error']=='native_service_tier_unsupported'
        assert failed['native_selection']['configuration_status']=='unconfirmed'


# spec: ANYM-993
def test_stop_while_catalog_preflight_waits_never_dispatches_goal_after_device_release(tmp_path,monkeypatch):
    import json
    import threading
    import time
    from agent_harness import Service
    from deedkit_android.codex_session import CodexSession
    _,record=native(tmp_path)
    config=tmp_path/'config.json'
    body=json.loads(config.read_text())
    body['agent'].update(guarded=False,persistent=True)
    config.write_text(json.dumps(body))
    entered,released=threading.Event(),threading.Event()
    original=CodexSession.select
    async def paused(self,config):
        selected=await original(self,config)
        entered.set()
        while not released.is_set():
            await asyncio.sleep(.005)
        return selected
    monkeypatch.setattr(CodexSession,'select',paused)
    with Service(config) as service:
        lease=service.lease()
        request=service.start(lease,goal='  Home, must not dispatch')['request_id']
        try:
            assert entered.wait(3)
            assert service.post('agent',{'action':'stop','request_id':request})['state']=='stopped'
        finally:
            released.set()
        deadline=time.monotonic()+3
        while service.app.state.agents.tasks:
            assert time.monotonic()<deadline
            time.sleep(.005)
        assert turns(record)==[]
        assert not service.app.state.devices.locks['home'].locked()


# spec: ANYM-993
def test_native_model_reroute_is_explicit_failure_without_another_goal_or_thread(tmp_path):
    import json
    from agent_harness import Service
    _,record=native(tmp_path)
    config=tmp_path/'config.json'
    body=json.loads(config.read_text())
    body['agent'].update(guarded=False,persistent=True)
    config.write_text(json.dumps(body))
    with Service(config) as service:
        lease=service.lease()
        result=service.finished(service.start(lease,goal='reroute')['request_id'])
        assert result['state']=='failed' and result['error']=='native_model_rerouted'
        assert result['native_selection']['configuration_status']=='rerouted'
        assert result['native_selection']['rerouted_to']=='gpt-6.1-terra'
        assert len(turns(record))==1
        assert len({r['pid'] for r in messages(record)})==1


# spec: ANYM-993
def test_async_continuation_retains_original_receipted_selection_after_owner_answer(tmp_path):
    import json
    from agent_harness import Service
    from test_agent_dialogue_http import async_answer
    _,record=native(tmp_path)
    config=tmp_path/'config.json'
    body=json.loads(config.read_text())
    body['agent'].update(guarded=False,persistent=True)
    config.write_text(json.dumps(body))
    with Service(config) as service:
        lease=service.lease()
        request=service.start(lease,goal='async-closed',model='gpt-6.1-terra',effort='high',service_tier='default')['request_id']
        answer=async_answer(service,lease,request)
        original=service.finished(request)
        service.post('agent',answer)
        continued=service.finished(request)
        assert continued['native_selection']['requested']==original['native_selection']['requested']
        assert [(m['params']['model'],m['params']['effort'],m['params']['serviceTier']) for m in turns(record)]==[
            ('gpt-6.1-terra','high','default'),('gpt-6.1-terra','high','default')]
        assert len({r['pid'] for r in messages(record)})==1


# spec: ANYM-993
def test_explicit_tier_changes_start_identity_while_absent_tier_retains_legacy_hash():
    import hashlib
    import json
    from deedkit_android.agent_run import AgentCall,DIALOGUE_FIELDS,WAKE_FIELDS
    call=AgentCall(action='start',request_id='original',device_id='home',session_id='lease',goal='Read',owner_instruction='Read only')
    body=call.model_dump(exclude={'action','request_id','session_id','after','wait_s','service_tier'}|DIALOGUE_FIELDS|WAKE_FIELDS)
    body.update(apps=[],secrets=[],outputs=[],params={})
    assert call.identity()==hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
    assert call.identity()=='06712b3700fa88e9f4e46c4e4283c1ae09aa1cfd0c72eed13376dc835cc6e7c9'
    first=call.model_copy(update={'service_tier':'ultrafast'})
    second=call.model_copy(update={'service_tier':'default'})
    assert len({call.identity(),first.identity(),second.identity()})==3


# spec: ANYM-993
def test_initial_goal_rechecks_revoked_control_after_selection_await(tmp_path,monkeypatch):
    import json
    import threading
    import time
    from agent_harness import Service
    from deedkit_android.codex_session import CodexSession
    _,record=native(tmp_path)
    config=tmp_path/'config.json'
    body=json.loads(config.read_text())
    body['agent'].update(guarded=False,persistent=True)
    config.write_text(json.dumps(body))
    entered,released=threading.Event(),threading.Event()
    original=CodexSession.select
    async def paused(self,config):
        selected=await original(self,config)
        entered.set()
        while not released.is_set():
            await asyncio.sleep(.005)
        return selected
    monkeypatch.setattr(CodexSession,'select',paused)
    with Service(config) as service:
        lease=service.lease()
        request=service.start(lease,goal='  Home, revoked')['request_id']
        try:
            assert entered.wait(3)
            active=service.app.state.agents.runs[('one',request)]
            body['principals'][0]['permissions']=['android:read']
            config.write_text(json.dumps(body))
        finally:
            released.set()
        deadline=time.monotonic()+3
        while not active.concluded.is_set():
            assert time.monotonic()<deadline
            time.sleep(.005)
        body['principals'][0]['permissions']=['android:read','android:control']
        config.write_text(json.dumps(body))
        result=service.finished(request)
        assert result['state']=='failed' and result['error']=='forbidden'
        assert turns(record)==[]


# spec: ANYM-993
@pytest.mark.parametrize('mode',['isolated','guarded'])
def test_configured_tier_is_refused_before_unsupported_engine_execution(tmp_path,mode):
    import json
    import uuid
    from agent_harness import Service
    from test_agent_bare import bare_host
    config,record=bare_host(tmp_path)
    body=json.loads(config.read_text())
    body['engines']['codex']['service_tier']='ultrafast'
    body['agent'].update(guarded=mode=='guarded',persistent=False)
    config.write_text(json.dumps(body))
    with Service(config) as service:
        lease=service.lease()
        result=service.post('agent',dict(action='start',request_id=str(uuid.uuid4()),device_id='home',session_id=lease,
                            goal='Read',owner_instruction='Read only'),code=422)
        assert result['error']=='service_tier_requires_persistent_bare'
        assert not record.exists()


# spec: ANYM-993
def test_submitted_settings_wait_for_native_applied_event_and_fresh_receipt_without_resend(tmp_path):
    async def check():
        session,record=native(tmp_path,delayed_settings=True)
        await session.connect()
        wanted=session.config.model_copy(update={'model':'gpt-6.1-terra','reasoning_effort':'high','service_tier':'default'})
        try:
            assert await session.turn('  Home, delayed selection',wanted,lambda e: asyncio.sleep(0),request_id='original')=='completed'
            assert session.selection['effective']=={'model':'gpt-6.1-terra','effort':'high','service_tier':None}
            methods=[r['message']['method'] for r in messages(record)]
            assert methods.count('thread/settings/update')==1
            assert methods.count('thread/resume')==2
            assert len(turns(record))==1
            assert methods.index('thread/settings/update')<methods.index('turn/start')
            assert len({r['pid'] for r in messages(record)})==1
        finally:
            await session.close()
    asyncio.run(check())


# spec: ANYM-993
def test_disconnect_during_native_applied_wait_fails_without_goal_or_setting_resend(tmp_path):
    async def check():
        session,record=native(tmp_path,delayed_settings=True,disconnect_before_applied=True)
        await session.connect()
        wanted=session.config.model_copy(update={'model':'gpt-6.1-terra','reasoning_effort':'high','service_tier':'default'})
        try:
            with pytest.raises(OSError,match='native_session_disconnected'):
                await asyncio.wait_for(session.turn('  Home, disconnected selection',wanted,lambda e: asyncio.sleep(0),request_id='original'),2)
            assert turns(record)==[]
            assert len([r for r in messages(record) if r['message'].get('method')=='thread/settings/update'])==1
            assert session.selection['configuration_status']=='unconfirmed'
        finally:
            await session.close()
    asyncio.run(check())
