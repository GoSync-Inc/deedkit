"""ANYM-450: Telegram start timing, summary_missing and the analysis filter, with a synthetic SDK and no live calls."""
import asyncio
import json
import struct
from types import SimpleNamespace

import pytest

from audio_probe import StartTiming


def pcm(value,count=480):
    return struct.pack('<'+'h'*count,*(value for _ in range(count)))  # 10 ms at 48 kHz mono


TONE,QUIET,ZERO=pcm(4000),pcm(30),pcm(0)


async def _telegram_start(monkeypatch,tmp_path,script,*,raising=False):
    """Dial with a synthetic SDK; peer_connected is at clock 100.0 s. Script steps: (seconds, 'ready'|'active'|frames)."""
    import pytgcalls
    import telegram_audio
    from pytgcalls.types import Device,Direction,Frame,StreamFrames
    from test_external_audio import _Native,_wire
    now=[100.0];callbacks=[];received=[];hooks={};done=asyncio.Event();started=asyncio.Event()
    monkeypatch.setattr(telegram_audio,'StartTiming',lambda:StartTiming(clock=lambda:now[0]))
    if raising:
        def broken(self,audio,ready):raise RuntimeError('synthetic start measurement failure')
        monkeypatch.setattr(StartTiming,'frame',broken)
    async def run(transport,brief,key,state):
        hooks['state']=state;started.set()
        await done.wait()
    calls,service,log,new_call=_wire(monkeypatch,tmp_path,run=run)
    monkeypatch.setattr(telegram_audio,'pcm_transport',lambda send,ready:hooks.update(ready=ready) or SimpleNamespace())
    class Native(_Native):
        def on_update(self):
            def register(fn):callbacks.append(fn);return fn
            return register
    monkeypatch.setattr(pytgcalls,'PyTgCalls',lambda *args,**kwargs:Native(log))
    class Input:
        async def push_audio_frame(self,frame):received.append(frame.audio)
    call=new_call('123')
    await calls.dial(call)
    await asyncio.wait_for(started.wait(),5)
    pushed_after_ready=[]
    for at,step in script:
        now[0]=100.0+at
        if step=='ready':hooks['ready'](Input())
        elif step=='active':
            service.connected(call['id']);hooks['state']['start_timing'].mark('call_active')
        else:
            if hooks['state'].get('input'):pushed_after_ready.extend(step)
            await callbacks[0](None,StreamFrames(123,Direction.INCOMING,Device.MICROPHONE,
                                                 [Frame(i,audio,Frame.Info()) for i,audio in enumerate(step)]))
    done.set();await asyncio.wait_for(calls.active[call['id']]['task'],5)
    # The model gets exactly the frames that arrived after readiness, whatever the measurement did.
    assert received==pushed_after_ready
    return service,service.get_call(call['id'])


SCENARIOS={
    # Frames before readiness are counted and still discarded; readiness and the active call come later.
    'input_before_delayed_readiness':([(0.5,[TONE]*3),(1.0,'ready'),(1.2,'active'),(1.5,[TONE])],
        {'input_ready_ms':1000.0,'call_active_ms':1200.0,'pre_ready_frames':3,
         'inbound_start':{'first_frame_ms':500.0,'first_active_ms':500.0}},None),
    'all_zero_and_quiet_frames':([(0,'ready'),(0,'active'),(0.2,[ZERO]*5),(0.4,[QUIET]*5)],
        {'input_ready_ms':0.0,'call_active_ms':0.0,'pre_ready_frames':0,
         'inbound_start':{'first_frame_ms':200.0,'first_active_ms':None}},None),
    'first_active_frame_immediately':([(0,'ready'),(0,'active'),(0,[TONE])],
        {'input_ready_ms':0.0,'call_active_ms':0.0,'pre_ready_frames':0,
         'inbound_start':{'first_frame_ms':0.0,'first_active_ms':0.0}},None),
    'first_active_frame_at_exactly_3_s':([(0,'ready'),(0,'active'),(0.1,[ZERO]),(3.0,[TONE])],
        {'input_ready_ms':0.0,'call_active_ms':0.0,'pre_ready_frames':0,
         'inbound_start':{'first_frame_ms':100.0,'first_active_ms':3000.0}},None),
    'first_active_frame_after_3_s':([(0,'ready'),(0,'active'),(0.1,[ZERO]*3),(4.25,[QUIET,TONE])],
        {'input_ready_ms':0.0,'call_active_ms':0.0,'pre_ready_frames':0,
         'inbound_start':{'first_frame_ms':100.0,'first_active_ms':4250.0}},4250.0),
    'no_input':([(0,'ready'),(0,'active')],
        {'input_ready_ms':0.0,'call_active_ms':0.0,'pre_ready_frames':0,
         'inbound_start':{'first_frame_ms':None,'first_active_ms':None}},None),
}


# spec: ANYM-450
@pytest.mark.parametrize('name',[*SCENARIOS,'raising_measurement'])
async def test_telegram_call_records_start_timing(monkeypatch,tmp_path,name):
    if name=='raising_measurement':
        # The measurement raises on every frame while identical audio still reaches the model.
        script=[(0,'ready'),(0,'active'),(0.1,[ZERO]),(5.0,[TONE,TONE])]
        expected={'input_ready_ms':0.0,'call_active_ms':0.0,'pre_ready_frames':0,
                  'inbound_start':None,'inbound_start_unavailable':True}
        late=None
    else:script,expected,late=SCENARIOS[name]
    service,call=await _telegram_start(monkeypatch,tmp_path,script,raising=name=='raising_measurement')
    start=call['metrics']['telegram_audio']['start']
    assert start.pop('peer_connected_at')  # wall clock of the provider connect
    assert start==expected
    assert call.get('late_inbound_ms')==late and ('late_inbound_ms' in call)==(late is not None)
    assert call['status']=='finished' and call['end_reason']=='disconnected'
    # The ANYM-426 counters and verdict are unchanged by the start timing.
    assert 'input_active_ms' in call['metrics']['telegram_audio'] and 'inbound_audio' in call
    service.close()


# spec: ANYM-450
def test_a_record_without_start_timing_reads_as_absent(tmp_path):
    from core import late_inbound_ms
    for metrics in (None,{},{'telegram_audio':{'input_pcm_ms':20}},{'telegram_audio':{'start':{}}},
                    {'telegram_audio':{'start':{'inbound_start':None,'inbound_start_unavailable':True}}}):
        assert late_inbound_ms(metrics) is None


def _external(tmp_path):
    from core import VoiceService
    from outbound import AgentExternalBrief
    from test_tasks import BRIEF
    service=VoiceService(tmp_path/'voice')
    brief=service.prepare(service.with_call_voice(AgentExternalBrief(
        **{**BRIEF,'source_message_ids':[]},channel='telegram',account='telegram',destination='123')),[])
    return service,service.start(brief['id'])


def _reports(service,call):
    return [json.loads(r[0]) for r in service.db.execute('SELECT body FROM voice_reports WHERE call_id=?',(call['id'],))]


def _unresolved(service,cid):pass


def _pending_decision(service,cid):
    service.tool(cid,'request_owner_decision',{'question':'Разрешить доплату?','options':['Да','Нет']},'t1')


def _answered_decision(service,cid):
    _pending_decision(service,cid)
    decision=service.get_call(cid)['decisions'][0]
    service.decide(cid,decision['id'],'Да')


def _rejected_closing(service,cid):
    # task_not_collected: the closing call is rejected and saves no summary.
    result=service.tool(cid,'end_conversation',{'reason':'completed','summary':'Всё узнали','evidence':'Пока'},'t1')
    assert result['error']


def _saved_summary(service,cid):
    service.tool(cid,'finish_task',{'summary':'Окно доставки не узнали'},'t1')


CASES={'unresolved':_unresolved,'pending_decision':_pending_decision,'answered_decision':_answered_decision,
       'rejected_closing':_rejected_closing,'saved_summary':_saved_summary}


# spec: ANYM-450
@pytest.mark.parametrize('case,outcome,missing',[
    ('unresolved','unresolved',True),('pending_decision','needs_owner',True),
    ('answered_decision','followup_ready',True),('rejected_closing','unresolved',True),
    ('saved_summary','incomplete',False),('never_connected','unresolved',True)])
async def test_summary_missing_is_independent_of_outcome(tmp_path,case,outcome,missing):
    from test_agent_api import api,registry
    from test_tasks import BRIEF
    path,_,_=registry(tmp_path)
    from core import VoiceService
    service=VoiceService(tmp_path/'data');surface=api(service,path)
    brief=await surface.call('alice','prepare_call',{'brief':{**BRIEF,'source_message_ids':[]}})
    call=await surface.call('alice','start_call',{'brief_id':brief['id']})
    if case!='never_connected':
        service.connected(call['id'])
        CASES[case](service,call['id'])
    before=service.get_call(call['id'])
    assert 'summary_missing' not in before  # a running call has no field
    service.stop(call['id'],'counterparty_hangup')
    status=await surface.call('alice','get_call_status',{'call_id':call['id']})
    assert status['outcome']==outcome and status['end_reason']=='counterparty_hangup'
    assert status.get('summary_missing') is (True if missing else None)
    assert ('summary_missing' in status)==missing
    rows=_reports(service,call)
    assert len(rows)==1 and rows[0]['snapshot'].get('summary_missing') is (True if missing else None)
    service.close()


# spec: ANYM-450
def test_summary_missing_after_service_restart_and_absent_on_a_legacy_record(tmp_path):
    from core import VoiceService
    service,call=_external(tmp_path)
    service.connected(call['id'])
    service.close()
    service=VoiceService(tmp_path/'voice')
    restarted=service.get_call(call['id'])
    assert restarted['end_reason']=='service_restart' and restarted['summary_missing'] is True
    # A record saved before the deploy has no field: absent, not false.
    body=json.loads(service.db.execute('SELECT body FROM calls WHERE id=?',(call['id'],)).fetchone()[0])
    body.pop('summary_missing')
    service.db.execute('UPDATE calls SET body=? WHERE id=?',(json.dumps(body),call['id']));service.db.commit()
    service.close()
    service=VoiceService(tmp_path/'voice')
    assert 'summary_missing' not in service.get_call(call['id'])
    service.close()


# spec: ANYM-450
@pytest.mark.parametrize('broken',['mark_summary_missing','late_inbound_ms'])
def test_a_raising_derivation_keeps_the_terminal_record_and_report_count(tmp_path,monkeypatch,broken):
    import core
    normal_service,normal=_external(tmp_path/'normal')
    normal_service.connected(normal['id'])
    normal=normal_service.stop(normal['id'],'counterparty_hangup')
    def fail(*args,**kwargs):raise RuntimeError('synthetic derivation failure')
    monkeypatch.setattr(core,broken,fail)
    service,call=_external(tmp_path/'broken')
    service.connected(call['id'])
    call=service.stop(call['id'],'counterparty_hangup')
    keys=('status','end_reason','outcome','inbound_audio')
    assert {k:call[k] for k in keys}=={k:normal[k] for k in keys}
    assert call['ended_at'] and len(_reports(service,call))==1
    assert ('summary_missing' in call)==(broken!='mark_summary_missing')
    service.close();normal_service.close()


# spec: ANYM-450
async def test_analysis_gets_only_late_inbound_and_summary_missing(monkeypatch,tmp_path):
    from google import genai
    import learning
    service,call=await _telegram_start(monkeypatch,tmp_path,[(0,'ready'),(0,'active'),(4.0,[TONE])])
    rows=_reports(service,call)
    assert len(rows)==1
    snapshot=rows[0]['snapshot']
    # The first report snapshot holds the final values.
    assert snapshot['late_inbound_ms']==4000.0 and snapshot['summary_missing'] is True
    assert 'start' in snapshot['metrics']['telegram_audio']
    snapshot['events'].append({'kind':'language_hint_failed'})
    snapshot['events'].insert(0,{'kind':'transcript','role':'user','text':'Hello'})
    # Even a stray top-level copy of the raw timing never leaves.
    snapshot.update(inbound_start={'first_frame_ms':0},pre_ready_frames=2,input_ready_ms=1.0)
    before=learning.analysis_view({k:v for k,v in snapshot.items() if k not in {'late_inbound_ms','summary_missing',
        'inbound_start','pre_ready_frames','input_ready_ms'}}|{'events':[e for e in snapshot['events'] if e['kind']!='language_hint_failed']})
    sent=[]
    class Models:
        async def generate_content(self,model,contents,config):
            sent.append(contents);raise RuntimeError('stop after capturing the request')
    class Client:
        def __init__(self,**kwargs):
            async def aclose():pass
            self.aio=SimpleNamespace(models=Models(),aclose=aclose)
    monkeypatch.setattr(genai,'Client',Client)
    with pytest.raises(RuntimeError):
        await learning.GeminiReviewer().review(snapshot,'synthetic-key')
    request=json.loads(sent[0])
    text=json.dumps(request)
    for key in ('peer_connected_at','input_ready_ms','call_active_ms','pre_ready_frames','inbound_start',
                'first_frame_ms','first_active_ms','inbound_start_unavailable','language_hint_failed','library_versions'):
        assert key not in text,key
    assert 'start' not in request['metrics']['telegram_audio']
    # The analyzer gains exactly these two fields; the rest of the allowed data is unchanged.
    gained={k:request[k] for k in set(request)-set(before)}
    assert gained=={'late_inbound_ms':4000.0,'summary_missing':True}
    assert {k:request[k] for k in before}==json.loads(json.dumps(before))
    service.close()


# spec: ANYM-450
@pytest.mark.parametrize('location',['top','events','metrics','telegram_audio'])
async def test_raw_start_timing_leaves_no_copy_in_the_analysis_request(monkeypatch,location):
    from google import genai
    import learning
    raw={'peer_connected_at':'synthetic','input_ready_ms':1.0,'call_active_ms':2.0,'pre_ready_frames':2,
         'inbound_start':{'first_active_ms':4000},'inbound_start_unavailable':True,'media':{'library_versions':['synthetic']}}
    snapshot={'events':[{'kind':'transcript','role':'user','text':'Hello'}],'metrics':{'kept':1},
              'late_inbound_ms':4000,'summary_missing':True}
    if location=='top':snapshot.update(raw)
    elif location=='events':snapshot['events'].append({'kind':'runtime_diagnostic','note':'kept',**raw})
    elif location=='metrics':snapshot['metrics'].update(raw)
    else:snapshot['metrics']['telegram_audio']={'input_active_ms':0,'start':dict(raw),**raw}
    local=json.loads(json.dumps(snapshot))
    sent=[]
    class Models:
        async def generate_content(self,model,contents,config):
            sent.append(contents);raise RuntimeError('stop after capturing the request')
    class Client:
        def __init__(self,**kwargs):
            async def aclose():pass
            self.aio=SimpleNamespace(models=Models(),aclose=aclose)
    monkeypatch.setattr(genai,'Client',Client)
    with pytest.raises(RuntimeError):
        await learning.GeminiReviewer().review(snapshot,'synthetic-key')
    request=json.loads(sent[0])
    for key in raw:assert key not in sent[0],key
    assert request['late_inbound_ms']==4000 and request['summary_missing'] is True
    assert request['events'][0]['text']=='Hello' and request['metrics']['kept']==1
    if location=='events':assert request['events'][1]=={'kind':'runtime_diagnostic','note':'kept'}
    if location=='telegram_audio':assert request['metrics']['telegram_audio']=={'input_active_ms':0}
    # The authorized local record keeps everything.
    assert snapshot==local


# spec: ANYM-450
def test_a_failed_start_timing_constructor_or_boundary_reads_as_unavailable():
    from audio_probe import diagnostics
    from telegram_audio import start_snapshot
    def broken():raise RuntimeError('synthetic constructor failure')
    assert start_snapshot({'start_timing':diagnostics(broken)})=={'inbound_start':None,'inbound_start_unavailable':True}
    times=iter([RuntimeError('synthetic boundary failure'),105.0])
    def clock():
        value=next(times)
        if isinstance(value,Exception):raise value
        return value
    timing=StartTiming(clock=clock)
    timing.mark('peer_connected')
    timing.frame(TONE,True)
    value=start_snapshot({'start_timing':timing})
    assert value['inbound_start_unavailable'] is True and value['inbound_start'] is None


# spec: ANYM-450
async def test_the_pipeline_begin_hook_marks_call_active_after_the_durable_call_is_active(monkeypatch):
    import voice
    from telegram_audio import pcm_transport
    order=[];clock=[100.0];workers=[]
    timing=StartTiming(clock=lambda:clock[0]);timing.mark('peer_connected')
    class Service:
        def connected(self,identity):order.append('connected');clock[0]=101.5
        def runtime_event(self,identity,event):pass
    class Runner:
        def __init__(self,*args,**kwargs):pass
        async def add_workers(self,*added):workers.extend(added)
        async def run(self):
            async def ignore(value):pass
            workers[0].queue_frames=ignore;workers[0].rtvi.send_server_message=ignore
            await workers[0]._call_event_handler('on_pipeline_started',None)
    class Model(voice.FrameProcessor):
        audio_health=SimpleNamespace(snapshot=lambda:{})
    monkeypatch.setattr(voice,'create_model',lambda *args,**kwargs:Model())
    monkeypatch.setattr(voice,'WorkerRunner',Runner)
    monkeypatch.setattr(voice,'LLMContextAggregatorPair',lambda context:(voice.FrameProcessor(),voice.FrameProcessor()))
    async def send(audio):pass
    state={'service':Service(),'call_id':'synthetic','start_timing':timing}
    await voice.run_transport(pcm_transport(send,lambda value:None),{'dialog_engine':'gemini_live','model':'m','voice':'Kore'},
                              'synthetic-key',state)
    assert order==['connected'] and timing.snapshot()['call_active_ms']==1500.0
