import asyncio
import contextlib
import json
import math
import struct
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from pipecat.frames.frames import InputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.workers.runner import WorkerRunner

from audio_probe import PCMProbe
from telegram_audio import MediaDiagnostics, dispose_native, observe_media
from voice import InboundAudioProbe


def pcm(value=0,count=960):
    return struct.pack('<'+'h'*count,*(value for _ in range(count)))


def tone(count=960,rate=48000):
    return struct.pack('<'+'h'*count,*(int(4000*math.sin(i*2*math.pi*440/rate)) for i in range(count)))


def test_probe_tells_digital_silence_from_a_quiet_microphone_and_reports_first_active_time():
    probe=PCMProbe()
    for at in (10,10.02):probe.input(pcm(0),at=at)
    probe.input(pcm(30),at=10.04)  # Quiet but not digital silence.
    probe.input(tone(),at=10.5)
    result=probe.snapshot()
    assert result['input_frames']==4
    assert result['input_zero_frames']==2
    assert result['first_active_input_ms']==pytest.approx(500)
    assert 30<result['input_max_rms']<4000
    assert result['input_active_ms']==pytest.approx(20)


def test_probe_without_input_or_speech_reports_no_first_active_time():
    probe=PCMProbe()
    assert probe.snapshot()['first_active_input_ms'] is None
    assert probe.snapshot()['input_max_rms']==0
    probe.input(pcm(0),at=1)
    assert probe.snapshot()['first_active_input_ms'] is None
    assert probe.snapshot()['input_zero_frames']==1


def test_probe_measures_input_duration_at_the_frame_sample_rate_and_bounds_rates():
    probe=PCMProbe()
    probe.input(tone(320,16000),at=0,rate=16000)  # 20 ms
    probe.input(tone(160,8000),at=1,rate=8000)  # 20 ms
    probe.input(tone(320,16000),at=2,rate=16000,channels=2)  # 10 ms per channel
    for rate in (11025,22050,24000,32000):probe.input(pcm(0,10),rate=rate)
    result=probe.snapshot()
    assert result['input_pcm_ms']>=50
    assert len(result['input_sample_rates'])==4
    assert 'audio' not in result


async def test_inbound_probe_counts_pipeline_audio_without_changing_frames():
    probe=PCMProbe();received=[];done=asyncio.Event()
    class Sink(FrameProcessor):
        async def process_frame(self,frame,direction):
            await super().process_frame(frame,direction)
            if isinstance(frame,InputAudioRawFrame):
                received.append(frame);done.set()
            await self.push_frame(frame,direction)
    audio=(np.sin(np.arange(320)*2*np.pi*440/16000)*4000).astype(np.int16).tobytes()
    worker=PipelineWorker(Pipeline([InboundAudioProbe(probe),Sink()]),idle_timeout_secs=None)
    runner=WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    @worker.event_handler('on_pipeline_started')
    async def start(worker,frame):
        await worker.queue_frames([InputAudioRawFrame(audio,16000,1)])
    task=asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(done.wait(),5)
    finally:
        await runner.cancel();await asyncio.wait_for(task,5)
    assert received[0].audio==audio and received[0].sample_rate==16000
    result=probe.snapshot()
    assert result['input_pcm_ms']==pytest.approx(20)
    assert result['input_active_ms']==pytest.approx(20)
    assert result['input_sample_rates']==[16000] and result['sample_rate']==16000


class Source:
    def __init__(self,device,state):self.device,self.state=device,state


def fake_native():
    registered={}
    class Binding:
        def on_remote_source_change(self,callback):registered['remote']=callback
        def on_connection_change(self,callback):registered['connection']=callback
    calls=[]
    async def handle(chat_id,info):calls.append(('handled',chat_id,info.state.name))
    async def confirm(*args,**kwargs):
        calls.append(('confirmed',args))
        return SimpleNamespace(library_versions=['9.0.0','13.0.0'],p2p_allowed=False)
    py=SimpleNamespace(_binding=Binding(),_handle_connection_changed=handle,_app=SimpleNamespace(confirm_call=confirm),
                       executor=SimpleNamespace(shutdown=lambda **kwargs:None),_is_running=True)
    return py,registered,calls


async def test_media_diagnostics_record_remote_mic_connection_and_protocol_without_changing_the_call():
    py,registered,calls=fake_native();media=MediaDiagnostics()
    observe_media(py,media)
    registered['remote'](7,Source(SimpleNamespace(name='MICROPHONE'),SimpleNamespace(name='IDLING')))
    registered['remote'](7,Source(SimpleNamespace(name='MICROPHONE'),SimpleNamespace(name='ACTIVE')))
    await py._handle_connection_changed(7,SimpleNamespace(state=SimpleNamespace(name='CONNECTED'),kind=SimpleNamespace(name='NORMAL')))
    protocol=await py._app.confirm_call(7,b'key',1,'protocol')
    assert calls==[('handled',7,'CONNECTED'),('confirmed',(7,b'key',1,'protocol'))]
    assert protocol.library_versions==['9.0.0','13.0.0']  # The caller still gets the library's own object.
    snapshot=media.snapshot()
    assert [(e['kind'],e.get('device'),e['state']) for e in snapshot['events']]==[
        ('remote_source','MICROPHONE','IDLING'),('remote_source','MICROPHONE','ACTIVE'),('connection',None,'CONNECTED')]
    assert snapshot['events'][2]['connection_kind']=='NORMAL'
    assert snapshot['library_versions']==['9.0.0','13.0.0'] and snapshot['p2p_allowed'] is False
    assert all(isinstance(e['at_ms'],int) for e in snapshot['events'])


def test_media_diagnostics_events_are_bounded_and_hold_only_names():
    media=MediaDiagnostics(limit=3)
    for _ in range(5):media.remote_source(Source('MICROPHONE','ACTIVE'))
    snapshot=media.snapshot()
    assert len(snapshot['events'])==3 and snapshot['events_dropped']==2
    assert set(snapshot['events'][0])=={'kind','at_ms','device','state'}


def test_media_diagnostics_survive_unexpected_library_shapes():
    media=MediaDiagnostics()
    media.remote_source(object())
    media.connection(None)
    media.protocol(object())
    assert media.snapshot()['events']==[] and media.snapshot()['library_versions'] is None


async def test_observe_media_never_blocks_a_call_when_the_library_hooks_are_missing():
    observe_media(SimpleNamespace(),MediaDiagnostics())
    observe_media(SimpleNamespace(_binding=object(),_app=object()),MediaDiagnostics())


async def test_dispose_native_clears_the_remote_source_callback():
    py,registered,_=fake_native();media=MediaDiagnostics()
    observe_media(py,media)
    dispose_native(py)
    registered['remote'](7,Source('MICROPHONE','ACTIVE'))  # Now the no-op replacement.
    assert media.snapshot()['events']==[]


def test_media_diagnostics_capacity_holds_across_threads():
    media=MediaDiagnostics(limit=50)
    def push():
        for _ in range(500):media.remote_source(Source('MICROPHONE','ACTIVE'))
    threads=[threading.Thread(target=push) for _ in range(4)]
    for thread in threads:thread.start()
    for thread in threads:thread.join()
    snapshot=media.snapshot()
    assert len(snapshot['events'])==50 and snapshot['events_dropped']==1950


def test_probe_sample_rate_follows_observed_input():
    probe=PCMProbe()
    assert probe.snapshot()['sample_rate']==48000
    probe.input(tone(320,16000),rate=16000)
    probe.input(tone(160,8000),rate=8000)
    assert probe.snapshot()['sample_rate'] is None


async def test_a_failing_inbound_probe_never_drops_audio_or_raises_a_pipeline_error():
    class BrokenProbe:
        def input(self,*args,**kwargs):raise RuntimeError('synthetic probe failure')
    received=[];errors=[];done=asyncio.Event()
    class Sink(FrameProcessor):
        async def process_frame(self,frame,direction):
            await super().process_frame(frame,direction)
            if isinstance(frame,InputAudioRawFrame):
                received.append(frame);done.set()
            await self.push_frame(frame,direction)
    audio=(np.sin(np.arange(320)*2*np.pi*440/16000)*4000).astype(np.int16).tobytes()
    probe=InboundAudioProbe(BrokenProbe())
    worker=PipelineWorker(Pipeline([probe,Sink()]),idle_timeout_secs=None)
    runner=WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    @worker.event_handler('on_pipeline_started')
    async def start(worker,frame):
        await worker.queue_frames([InputAudioRawFrame(audio,16000,1)])
    @worker.event_handler('on_pipeline_error')
    async def error(worker,frame):errors.append(frame)
    task=asyncio.create_task(runner.run())
    try:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(done.wait(),2)
    finally:
        await runner.cancel();await asyncio.wait_for(task,5)
    # The model still gets the identical frame, and the call sees no pipeline (model) error.
    assert [frame.audio for frame in received]==[audio]
    assert errors==[] and probe.failures==1


async def test_external_analysis_never_receives_inbound_audio_diagnostics(monkeypatch):
    import json
    from google import genai
    import learning
    sent=[]
    class Models:
        async def generate_content(self,model,contents,config):
            sent.append(contents);raise RuntimeError('stop after capturing the request')
    class Client:
        def __init__(self,**kwargs):
            async def aclose():pass
            self.aio=SimpleNamespace(models=Models(),aclose=aclose)
    monkeypatch.setattr(genai,'Client',Client)
    call={'id':'synthetic','events':[{'kind':'transcript','role':'user','text':'Hello'},
                                     {'kind':'inbound_audio','audio':{'input_frames':5,'input_max_rms':10.0}}],
          'metrics':{'telegram_audio':{'measurement':'worker_pcm_energy_gap','input_pcm_ms':20,'input_frames':5,
                                       'input_zero_frames':0,'input_max_rms':10.0,'first_active_input_ms':0,
                                       'input_sample_rates':[48000],'media':{'events':[]}}}}
    with pytest.raises(RuntimeError):
        await learning.GeminiReviewer().review(call,'synthetic-key')
    request=json.loads(sent[0])
    assert [event['kind'] for event in request['events']]==['transcript']
    assert request['metrics']['telegram_audio']=={'measurement':'worker_pcm_energy_gap','input_pcm_ms':20}
    # The server copy keeps every diagnostic.
    assert call['events'][1]['kind']=='inbound_audio' and 'media' in call['metrics']['telegram_audio']


# The call-end verdict (ANYM-426): the flows below run a real VoiceService and TelegramCalls with a
# synthetic SDK; inbound PCM enters through the StreamFrames callback and the shared InboundAudioProbe.
async def _telegram_call(monkeypatch,tmp_path,audio=(),*,connect=True,emit=True):
    import pytgcalls
    from pipecat.processors.frame_processor import FrameDirection
    from pytgcalls.types import Device,Direction,Frame,StreamFrames
    from test_external_audio import _Native,_wire
    callbacks=[];done=asyncio.Event();ready=asyncio.Event()
    async def run_transport(transport,brief,key,state):
        # Stands in for voice.run_transport: the same shared probe and the same final inbound_audio event.
        probe=PCMProbe();inbound=InboundAudioProbe(probe)
        async def push(frame,direction=FrameDirection.DOWNSTREAM):pass
        inbound.push_frame=push
        class Input:
            async def push_audio_frame(self,frame):await inbound.process_frame(frame,FrameDirection.DOWNSTREAM)
        state['input']=Input();ready.set()
        try:await done.wait()
        finally:
            if emit:state['service'].runtime_event(state['call_id'],{'kind':'inbound_audio','audio':probe.snapshot()})
    calls,service,log,new_call=_wire(monkeypatch,tmp_path,run=run_transport)
    class Native(_Native):
        def on_update(self):
            def register(fn):callbacks.append(fn);return fn
            return register
    monkeypatch.setattr(pytgcalls,'PyTgCalls',lambda *args,**kwargs:Native(log))
    call=new_call('123')
    await calls.dial(call)
    task=calls.active[call['id']]['task']
    if connect:service.connected(call['id'])
    await asyncio.wait_for(ready.wait(),5)
    if audio:
        await callbacks[0](None,StreamFrames(123,Direction.INCOMING,Device.MICROPHONE,
                                             [Frame(i,chunk,Frame.Info()) for i,chunk in enumerate(audio)]))
    done.set();await asyncio.wait_for(task,5)
    return service,service.get_call(call['id'])


def frames(value,count):
    return [pcm(value)]*count  # 20 ms per frame at 48 kHz.


def external_call(tmp_path):
    from core import VoiceService
    from outbound import AgentExternalBrief
    from test_tasks import BRIEF
    service=VoiceService(tmp_path/'voice')
    brief=service.prepare(service.with_call_voice(AgentExternalBrief(
        **{**BRIEF,'source_message_ids':[]},channel='telegram',account='telegram',destination='123')),[])
    return service,service.start(brief['id'])


def reports(service,call):
    return [json.loads(r[0]) for r in service.db.execute('SELECT body FROM voice_reports WHERE call_id=?',(call['id'],))]


# spec: ANYM-426
async def test_telegram_call_with_only_all_zero_frames_ends_with_zero_frames(monkeypatch,tmp_path):
    _,call=await _telegram_call(monkeypatch,tmp_path,frames(0,501))
    assert call['status']=='finished' and call['end_reason']=='disconnected'
    assert call['inbound_audio']=={'heard':False,'reason':'zero_frames'}


# spec: ANYM-426
async def test_telegram_call_with_quiet_nonzero_frames_ends_with_quiet_frames(monkeypatch,tmp_path):
    _,call=await _telegram_call(monkeypatch,tmp_path/'quiet',frames(30,501))
    assert call['inbound_audio']=={'heard':False,'reason':'quiet_frames'}
    # A mix that includes all-zero frames is quiet too, not digital silence.
    _,call=await _telegram_call(monkeypatch,tmp_path/'mix',frames(0,250)+frames(30,251))
    assert call['inbound_audio']=={'heard':False,'reason':'quiet_frames'}


# spec: ANYM-426
async def test_telegram_call_with_one_active_frame_is_heard_even_under_ten_seconds(monkeypatch,tmp_path):
    _,call=await _telegram_call(monkeypatch,tmp_path/'long',frames(0,500)+[tone()])
    assert call['inbound_audio']=={'heard':True,'reason':None}
    _,call=await _telegram_call(monkeypatch,tmp_path/'short',[tone()])
    assert call['inbound_audio']=={'heard':True,'reason':None}


# spec: ANYM-426
async def test_connected_telegram_call_without_any_frame_ends_with_no_frames(monkeypatch,tmp_path):
    _,call=await _telegram_call(monkeypatch,tmp_path)
    assert call['inbound_audio']=={'heard':False,'reason':'no_frames'}


# spec: ANYM-426
async def test_connected_telegram_call_under_ten_seconds_without_an_active_frame_has_no_verdict(monkeypatch,tmp_path):
    _,call=await _telegram_call(monkeypatch,tmp_path,frames(0,499))
    assert call['inbound_audio']=={'heard':None,'reason':None}


# spec: ANYM-426
@pytest.mark.parametrize('emit',[True,False],ids=['with_event','without_event'])
async def test_never_connected_telegram_call_has_no_verdict(monkeypatch,tmp_path,emit):
    _,call=await _telegram_call(monkeypatch,tmp_path,frames(0,501),connect=False,emit=emit)
    assert call['status']=='finished'
    assert any(e['kind']=='inbound_audio' for e in call['events']) is emit
    assert call['inbound_audio']=={'heard':None,'reason':None}


# spec: ANYM-426
@pytest.mark.parametrize('audio',[None,'text',{'input_frames':5},
                                  {'input_frames':True,'input_active_ms':0,'input_pcm_ms':20000,'input_zero_frames':0}],
                         ids=['missing','not_a_dict','partial','bool'])
def test_connected_call_with_a_missing_or_malformed_inbound_event_has_an_unknown_verdict(tmp_path,audio):
    service,call=external_call(tmp_path)
    service.connected(call['id'])
    if audio is not None:service.runtime_event(call['id'],{'kind':'inbound_audio','audio':audio})
    assert service.stop(call['id'],'disconnected')['inbound_audio']=={'heard':None,'reason':'unknown'}
    service.close()


# spec: ANYM-426
async def test_the_first_analysis_report_carries_the_verdict_and_a_normal_call_has_exactly_one(monkeypatch,tmp_path):
    service,call=await _telegram_call(monkeypatch,tmp_path,frames(0,501))
    rows=reports(service,call)
    assert len(rows)==1
    assert rows[0]['snapshot']['inbound_audio']=={'heard':False,'reason':'zero_frames'}


# spec: ANYM-426
async def test_a_raising_verdict_is_unknown_and_changes_nothing_else_about_the_call(monkeypatch,tmp_path):
    import core
    _,normal=await _telegram_call(monkeypatch,tmp_path/'normal',frames(0,501))
    def broken(*args,**kwargs):raise RuntimeError('synthetic verdict failure')
    monkeypatch.setattr(core,'inbound_audio_verdict',broken)
    service,call=await _telegram_call(monkeypatch,tmp_path/'broken',frames(0,501))
    assert call['inbound_audio']=={'heard':None,'reason':'unknown'}
    assert {k:call[k] for k in ('status','end_reason','outcome')}=={k:normal[k] for k in ('status','end_reason','outcome')}
    assert call['ended_at'] and len(reports(service,call))==1


# spec: ANYM-426
def test_a_call_interrupted_by_service_restart_has_no_verdict_field(tmp_path):
    from core import VoiceService
    service,call=external_call(tmp_path)
    service.connected(call['id'])
    service.runtime_event(call['id'],{'kind':'inbound_audio','audio':PCMProbe().snapshot()})
    service.close()
    service=VoiceService(tmp_path/'voice')
    call=service.get_call(call['id'])
    assert call['end_reason']=='service_restart' and 'inbound_audio' not in call
    service.close()


# spec: ANYM-426
async def test_get_call_status_returns_the_verdict_without_raw_counters(tmp_path):
    from core import VoiceService
    from test_agent_api import api,registry
    from test_tasks import BRIEF
    path,_,_=registry(tmp_path)
    service=VoiceService(tmp_path/'data');surface=api(service,path)
    brief=await surface.call('alice','prepare_call',{'brief':{**BRIEF,'source_message_ids':[]}})
    call=await surface.call('alice','start_call',{'brief_id':brief['id']})
    service.connected(call['id'])
    # A running call, like one saved before the deploy, has no field at all (absent, not null).
    assert 'inbound_audio' not in await surface.call('alice','get_call_status',{'call_id':call['id']})
    probe=PCMProbe();probe.input(tone())  # Browser channel: the shared probe's counters.
    service.runtime_event(call['id'],{'kind':'inbound_audio','audio':probe.snapshot()})
    await surface.call('alice','stop_call',{'call_id':call['id']})
    status=await surface.call('alice','get_call_status',{'call_id':call['id']})
    assert status['inbound_audio']=={'heard':True,'reason':None}
    text=json.dumps(status)
    assert not any(key in text for key in ('input_frames','input_active_ms','input_pcm_ms','input_max_rms','metrics','events'))
    # The full record still holds the raw counters.
    result=await surface.call('alice','get_call_result',{'call_id':call['id']})
    assert [e['audio']['input_frames'] for e in result['events'] if e['kind']=='inbound_audio']==[1]
    service.close()


# spec: ANYM-426
async def test_the_analysis_model_receives_only_heard_and_reason_from_the_verdict(monkeypatch,tmp_path):
    from google import genai
    import learning
    service,call=await _telegram_call(monkeypatch,tmp_path,frames(0,501))
    snapshot=reports(service,call)[0]['snapshot']
    snapshot['events'].insert(0,{'kind':'transcript','role':'user','text':'Hello'})
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
    assert request['inbound_audio']=={'heard':False,'reason':'zero_frames'}
    assert 'inbound_audio' not in [e['kind'] for e in request['events']]
    assert not set(request['metrics']['telegram_audio'])&learning.SERVER_ONLY_AUDIO
