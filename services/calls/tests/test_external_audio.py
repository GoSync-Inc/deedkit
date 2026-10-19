import asyncio
import base64
import json
from types import SimpleNamespace

import numpy as np
import pytest

from pipecat.frames.frames import OutputAudioRawFrame,StartFrame,InputAudioRawFrame,InterruptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.workers.runner import WorkerRunner
from pipecat.serializers.twilio import TwilioFrameSerializer
from telegram_audio import pcm_transport


async def test_telegram_sdk_incoming_pcm_reaches_pipecat(monkeypatch):
    import pytgcalls
    import telegram_audio
    from pytgcalls.types import StreamFrames,Direction,Device,Frame

    callbacks=[]
    class Client:
        def __init__(self,*args):pass
        async def connect(self):pass
        async def close(self):pass
    class Native:
        def __init__(self,*args,**kwargs):
            self._app=self;self._binding=self
        def on_update(self):
            def register(fn):callbacks.append(fn);return fn
            return register
        async def start(self):pass
        async def play(self,*args,**kwargs):pass
        async def record(self,*args,**kwargs):pass
        async def get_input_call(self,*args):return SimpleNamespace(id=101)
        async def leave_call(self,*args):pass
        async def stop(self,*args):pass
    async def waiting(self,*args):await asyncio.Event().wait()
    class Input:
        async def push_audio_frame(self,frame):received.append(frame)
    monkeypatch.setattr(telegram_audio,'BrokerClient',Client)
    monkeypatch.setattr(pytgcalls,'PyTgCalls',Native)
    monkeypatch.setattr(telegram_audio,'dispose_native',lambda py:None)
    monkeypatch.setattr(telegram_audio.TelegramCalls,'_run',waiting)
    calls=telegram_audio.TelegramCalls({'socket':'/fixture'},None,None,None)
    monkeypatch.setattr(calls,'token',lambda:'fixture-token')
    received=[]
    await calls.dial({'id':'fixture','brief':{'destination':'123'}})
    calls.active['fixture']['input']=Input()
    try:
        audio=(np.sin(np.arange(960)*2*np.pi*440/48000)*4000).astype(np.int16).tobytes()
        packet=StreamFrames(123,Direction.INCOMING,Device.MICROPHONE,[Frame(1,audio,Frame.Info())])
        await callbacks[0](None,packet)
        assert len(received)==1
        assert isinstance(received[0],InputAudioRawFrame)
        assert received[0].audio==audio and received[0].sample_rate==48000
        assert calls.active['fixture']['audio_probe'].snapshot()['input_pcm_ms']==20
        await callbacks[0](None,StreamFrames(999,Direction.INCOMING,Device.MICROPHONE,packet.frames))
        await callbacks[0](None,StreamFrames(123,Direction.OUTGOING,Device.MICROPHONE,packet.frames))
        assert len(received)==1
    finally:await calls.close()


async def test_raw_telegram_audio_transport_sends_real_pcm_and_releases_worker():
    output=[];ready=asyncio.Event()
    async def send(audio):
        output.append(audio)
        if any(audio):ready.set()
    transport=pcm_transport(send,lambda v:None)
    worker=PipelineWorker(Pipeline([transport.input(),transport.output()]),idle_timeout_secs=None)
    runner=WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    @worker.event_handler('on_pipeline_started')
    async def start(worker,frame):
        tone=(np.sin(np.arange(4800)*2*np.pi*440/48000)*4000).astype(np.int16).tobytes()
        await worker.queue_frames([OutputAudioRawFrame(tone,48000,1)])
    task=asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(ready.wait(),5)
        assert any(len(chunk)==960 for chunk in output)
    finally:
        await runner.cancel();await asyncio.wait_for(task,5)
    assert task.done()


async def test_twilio_pcm_ulaw_roundtrip_and_interrupt_clear():
    serializer=TwilioFrameSerializer('MZ'+'1'*32,params=TwilioFrameSerializer.InputParams(auto_hang_up=False))
    await serializer.setup(StartFrame(audio_in_sample_rate=16000,audio_out_sample_rate=24000))
    tone=(np.sin(np.arange(800)*2*np.pi*440/8000)*4000).astype(np.int16).tobytes()
    payload=json.loads(await serializer.serialize(OutputAudioRawFrame(tone,8000,1)))
    assert payload['event']=='media' and payload['streamSid']=='MZ'+'1'*32
    assert 160<=len(base64.b64decode(payload['media']['payload']))<=800
    for _ in range(4):
        decoded=await serializer.deserialize(json.dumps({'event':'media','media':payload['media']}))
        if decoded is not None and decoded.audio:break
    assert isinstance(decoded,InputAudioRawFrame) and decoded.sample_rate==16000
    assert max(np.abs(np.frombuffer(decoded.audio,dtype=np.int16)))>1000
    assert json.loads(await serializer.serialize(InterruptionFrame()))['event']=='clear'


async def test_a_failing_telegram_audio_probe_never_drops_or_skips_audio(monkeypatch):
    import pytgcalls
    import telegram_audio
    from pytgcalls.types import StreamFrames,Direction,Device,Frame

    callbacks=[];sent=[];received=[]
    class Client:
        def __init__(self,*args):pass
        async def connect(self):pass
        async def close(self):pass
    class Native:
        def __init__(self,*args,**kwargs):
            self._app=self;self._binding=self
        def on_update(self):
            def register(fn):callbacks.append(fn);return fn
            return register
        async def start(self):pass
        async def play(self,*args,**kwargs):pass
        async def record(self,*args,**kwargs):pass
        async def get_input_call(self,*args):return SimpleNamespace(id=101)
        async def send_frame(self,user,device,audio):sent.append(audio)
        async def leave_call(self,*args):pass
        async def stop(self,*args):pass
    class BrokenProbe:
        def input(self,*args,**kwargs):raise RuntimeError('synthetic probe failure')
        def output(self,*args,**kwargs):raise RuntimeError('synthetic probe failure')
    async def waiting(self,*args):await asyncio.Event().wait()
    class Input:
        async def push_audio_frame(self,frame):received.append(frame)
    outputs=[]
    def capture(send,set_input):
        outputs.append(send);return SimpleNamespace()
    monkeypatch.setattr(telegram_audio,'BrokerClient',Client)
    monkeypatch.setattr(pytgcalls,'PyTgCalls',Native)
    monkeypatch.setattr(telegram_audio,'dispose_native',lambda py:None)
    monkeypatch.setattr(telegram_audio,'PCMProbe',BrokenProbe)
    monkeypatch.setattr(telegram_audio,'pcm_transport',capture)
    monkeypatch.setattr(telegram_audio.TelegramCalls,'_run',waiting)
    calls=telegram_audio.TelegramCalls({'socket':'/fixture'},None,None,None)
    monkeypatch.setattr(calls,'token',lambda:'fixture-token')
    await calls.dial({'id':'fixture','brief':{'destination':'123'}})
    calls.active['fixture']['input']=Input()
    try:
        audio=(np.sin(np.arange(960)*2*np.pi*440/48000)*4000).astype(np.int16).tobytes()
        packet=StreamFrames(123,Direction.INCOMING,Device.MICROPHONE,[Frame(1,audio,Frame.Info()),Frame(2,audio,Frame.Info())])
        await callbacks[0](None,packet)
        # Both frames reach the model although every probe call raises.
        assert [frame.audio for frame in received]==[audio,audio]
        await outputs[0](audio)
        assert sent==[audio]
    finally:await calls.close()


# spec: ANYM-382
async def test_failing_diagnostics_constructors_never_break_telegram_call_setup(monkeypatch):
    import pytgcalls
    import telegram_audio

    closed=[]
    class Client:
        def __init__(self,*args):pass
        async def connect(self):pass
        async def close(self):closed.append(True)
    class Native:
        def __init__(self,*args,**kwargs):
            self._app=self;self._binding=self
        def on_update(self):return lambda fn:fn
        async def start(self):pass
        async def play(self,*args,**kwargs):pass
        async def record(self,*args,**kwargs):pass
        async def get_input_call(self,*args):return SimpleNamespace(id=101)
        async def send_frame(self,*args):pass
        async def leave_call(self,*args):pass
        async def stop(self,*args):pass
    def broken():raise RuntimeError('synthetic constructor failure')
    async def waiting(self,*args):await asyncio.Event().wait()
    monkeypatch.setattr(telegram_audio,'BrokerClient',Client)
    monkeypatch.setattr(pytgcalls,'PyTgCalls',Native)
    monkeypatch.setattr(telegram_audio,'dispose_native',lambda py:None)
    monkeypatch.setattr(telegram_audio,'PCMProbe',broken)
    monkeypatch.setattr(telegram_audio,'MediaDiagnostics',broken)
    monkeypatch.setattr(telegram_audio,'pcm_transport',lambda send,set_input:SimpleNamespace())
    monkeypatch.setattr(telegram_audio.TelegramCalls,'_run',waiting)
    calls=telegram_audio.TelegramCalls({'socket':'/fixture'},None,None,None)
    monkeypatch.setattr(calls,'token',lambda:'fixture-token')
    try:
        result=await calls.dial({'id':'fixture','brief':{'destination':'123'}})
        assert result['provider_status']=='connected'
        state=calls.active['fixture']
        # The stand-ins accept every probe call and snapshot to nothing.
        state['audio_probe'].input(b'x');state['audio_probe'].output(b'x')
        assert state['audio_probe'].snapshot()=={} and state['media'].snapshot()=={}
    finally:await calls.close()
    assert closed==[True]


# spec: ANYM-382
async def test_a_failing_final_telegram_audio_metrics_write_never_skips_stop(monkeypatch):
    import telegram_audio
    import voice

    stops=[]
    class Service:
        def get_call(self,cid):raise RuntimeError('synthetic metrics read failure')
        def stop(self,cid,reason):stops.append((cid,reason))
    async def finished(*args,**kwargs):pass
    monkeypatch.setattr(voice,'run_transport',finished)
    calls=telegram_audio.TelegramCalls({},Service(),lambda brief:'synthetic-key',None)
    await calls._run('fixture',SimpleNamespace(),{'brief':{'max_duration_secs':5}})
    assert stops==[('fixture','disconnected')]
    class BrokenSnapshot:
        def snapshot(self):raise RuntimeError('synthetic snapshot failure')
    class Reading(Service):
        def get_call(self,cid):return {'metrics':{}}
        def result(self,cid,**kwargs):pass
    stops.clear()
    calls=telegram_audio.TelegramCalls({},Reading(),lambda brief:'synthetic-key',None)
    await calls._run('fixture',SimpleNamespace(),{'brief':{'max_duration_secs':5},'audio_probe':BrokenSnapshot(),'media':BrokenSnapshot()})
    assert stops==[('fixture','disconnected')]


# spec: ANYM-382
async def test_a_failing_final_telegram_audio_result_write_or_media_snapshot_never_skips_stop(monkeypatch):
    import telegram_audio
    import voice

    stops=[];called=[]
    class Service:
        def get_call(self,cid):return {'metrics':{}}
        def result(self,cid,**kwargs):
            called.append('result');raise RuntimeError('synthetic result write failure')
        def stop(self,cid,reason):stops.append((cid,reason))
    class Probe:
        def snapshot(self):return {}
    class BrokenMedia:
        def snapshot(self):
            called.append('media');raise RuntimeError('synthetic media snapshot failure')
    async def finished(*args,**kwargs):pass
    monkeypatch.setattr(voice,'run_transport',finished)
    calls=telegram_audio.TelegramCalls({},Service(),lambda brief:'synthetic-key',None)
    await calls._run('fixture',SimpleNamespace(),{'brief':{'max_duration_secs':5},'audio_probe':Probe(),'media':Probe()})
    assert called==['result'] and stops==[('fixture','disconnected')]
    called.clear();stops.clear()
    await calls._run('fixture',SimpleNamespace(),{'brief':{'max_duration_secs':5},'audio_probe':Probe(),'media':BrokenMedia()})
    assert called==['media'] and stops==[('fixture','disconnected')]


class _Native:
    """Synthetic PyTgCalls: every cleanup step records itself and fails or blocks on request."""
    def __init__(self,log,fail=(),block=()):
        self.log,self.fail,self.block=log,set(fail),set(block)
        self._app=self._binding=self
    def on_update(self):return lambda fn:fn
    async def _step(self,name):
        self.log.append(name)
        if name in self.block:await asyncio.Event().wait()
        if name in self.fail:raise RuntimeError('synthetic '+name+' failure')
    async def start(self):pass
    async def play(self,*args,**kwargs):await self._step('play')
    async def record(self,*args,**kwargs):pass
    async def get_input_call(self,*args):return SimpleNamespace(id=101)
    async def send_frame(self,*args):pass
    async def leave_call(self,*args):await self._step('leave_call')
    async def stop(self,*args):await self._step('binding_stop')


def _wire(monkeypatch,tmp_path,fail=(),block=(),run=None,swallow=False):
    """A real VoiceService and TelegramCalls with a synthetic SDK and broker; returns (calls,service,log,new_call)."""
    import pytgcalls
    import telegram_audio
    import voice
    from core import VoiceService
    from outbound import AgentExternalBrief
    from test_tasks import BRIEF
    log=[]
    class Client:
        def __init__(self,*args):pass
        async def connect(self):pass
        async def close(self):
            log.append('broker_close')
            if 'broker_close' in block:
                # The real BrokerClient.close suppresses a cancellation while it joins its reader.
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:
                    if not swallow:raise
            if 'broker_close' in fail:raise RuntimeError('synthetic broker_close failure')
    def dispose(py):
        log.append('dispose_native')
        if 'dispose_native' in fail:raise RuntimeError('synthetic dispose_native failure')
    async def waiting(*args,**kwargs):await asyncio.Event().wait()
    monkeypatch.setattr(telegram_audio,'BrokerClient',Client)
    monkeypatch.setattr(pytgcalls,'PyTgCalls',lambda *args,**kwargs:_Native(log,fail,block))
    monkeypatch.setattr(telegram_audio,'dispose_native',dispose)
    monkeypatch.setattr(telegram_audio,'pcm_transport',lambda send,set_input:SimpleNamespace())
    monkeypatch.setattr(voice,'run_transport',run or waiting)
    service=VoiceService(tmp_path/'voice')
    calls=telegram_audio.TelegramCalls({'socket':'/fixture'},service,lambda brief:'synthetic-key',None)
    monkeypatch.setattr(calls,'token',lambda:'fixture-token')
    def new_call(destination):
        brief=service.prepare(service.with_call_voice(AgentExternalBrief(
            **{**BRIEF,'source_message_ids':[]},channel='telegram',account='telegram',destination=destination)),[])
        return service.start(brief['id'])
    return calls,service,log,new_call


async def _settle():
    for _ in range(5):await asyncio.sleep(0)


# spec: ANYM-409
@pytest.mark.parametrize('failing',[('broker_close',),('dispose_native',),('broker_close','dispose_native'),
                                    ('leave_call','binding_stop','broker_close','dispose_native')])
async def test_a_failing_release_step_never_skips_the_rest_or_the_calls_own_end_reason(monkeypatch,tmp_path,failing):
    async def finished(*args,**kwargs):pass
    calls,service,log,new_call=_wire(monkeypatch,tmp_path,fail=failing,run=finished)
    call=new_call('123456')
    await calls.dial(call)
    await asyncio.wait_for(calls.active[call['id']]['task'],5)
    assert log[-4:]==['leave_call','binding_stop','broker_close','dispose_native']
    done=service.get_call(call['id'])
    assert done['status']=='finished' and done['end_reason']=='disconnected'
    # The call is terminal right away, so a call for a different brief is admitted.
    assert new_call('654321')['status']=='dialing'
    await calls._release(call['id'])
    assert log.count('broker_close')==1
    service.close()


# spec: ANYM-409
async def test_a_failing_release_keeps_the_model_error_reason(monkeypatch,tmp_path):
    async def broken(*args,**kwargs):raise RuntimeError('synthetic model failure')
    calls,service,log,new_call=_wire(monkeypatch,tmp_path,fail=('broker_close',),run=broken)
    call=new_call('123456')
    await calls.dial(call)
    await asyncio.wait_for(calls.active[call['id']]['task'],5)
    assert service.get_call(call['id'])['end_reason']=='model_error'
    service.close()


# spec: ANYM-409
async def test_a_cancellation_during_release_is_held_until_every_step_and_service_stop_ran(monkeypatch,tmp_path):
    async def finished(*args,**kwargs):pass
    calls,service,log,new_call=_wire(monkeypatch,tmp_path,block=('leave_call',),run=finished)
    call=new_call('123456')
    await calls.dial(call)
    task=calls.active[call['id']]['task']
    await _settle()
    assert log[-1]=='leave_call'
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert log[-3:]==['binding_stop','broker_close','dispose_native']
    done=service.get_call(call['id'])
    assert done['status']=='finished' and done['end_reason']=='disconnected'
    service.close()


# spec: ANYM-409
async def test_a_release_failure_in_the_dial_error_path_keeps_the_original_exception(monkeypatch,tmp_path):
    calls,service,log,new_call=_wire(monkeypatch,tmp_path,fail=('play','broker_close','dispose_native'))
    with pytest.raises(RuntimeError,match='synthetic play failure'):
        await calls.dial(new_call('123456'))
    assert log[-4:]==['leave_call','binding_stop','broker_close','dispose_native'] and not calls.active
    # A cancellation of the dial itself also propagates unchanged.
    calls,service,log,new_call=_wire(monkeypatch,tmp_path/'second',fail=('broker_close',),block=('play',))
    dialing=asyncio.create_task(calls.dial(new_call('123456')))
    await _settle()
    dialing.cancel()
    with pytest.raises(asyncio.CancelledError):await dialing
    assert log[-2:]==['broker_close','dispose_native'] and not calls.active
    service.close()


# spec: ANYM-409
@pytest.mark.parametrize('how',['cancel_attempt','hangup','close'])
async def test_a_failing_release_is_swallowed_by_cancel_attempt_hangup_and_close(monkeypatch,tmp_path,how):
    calls,service,log,new_call=_wire(monkeypatch,tmp_path,fail=('broker_close','dispose_native'))
    call=new_call('123456')
    await calls.dial(call)
    task=calls.active[call['id']]['task']
    await _settle()
    if how=='cancel_attempt':await calls.cancel_attempt(call['id'])
    elif how=='hangup':await calls.hangup('101')
    else:await calls.close()
    assert task.done() and not calls.active
    assert log[-4:]==['leave_call','binding_stop','broker_close','dispose_native']
    done=service.get_call(call['id'])
    assert done['status']=='finished' and done['end_reason']=='owner_stop'
    service.close()


# spec: ANYM-409
async def test_close_releases_every_other_call_after_a_failing_release_with_a_worker_and_a_dial_task(monkeypatch,tmp_path):
    calls,service,log,new_call=_wire(monkeypatch,tmp_path,fail=('broker_close',))
    first=new_call('123456')
    await calls.dial(first)
    task=calls.active[first['id']]['task']
    # A second call is still dialing: its state exists, with the dial task and no worker.
    class Dialing:
        def __init__(self):self.closed=False
        async def close(self):self.closed=True
    dial_task=asyncio.create_task(asyncio.Event().wait())
    pending=SimpleNamespace(leave_call=lambda *a:asyncio.sleep(0),_binding=_Native(log))
    client=Dialing()
    calls.active['dialing']={'py':pending,'client':client,'user':1,'dial_task':dial_task}
    await _settle()
    await calls.close()
    assert task.done() and dial_task.cancelled() and client.closed and not calls.active
    assert service.get_call(first['id'])['end_reason']=='owner_stop'
    service.close()


# spec: ANYM-409
async def test_cancel_attempt_never_swallows_its_callers_cancellation(monkeypatch,tmp_path):
    calls,service,log,new_call=_wire(monkeypatch,tmp_path,block=('leave_call',))
    call=new_call('123456')
    await calls.dial(call)
    task=calls.active[call['id']]['task']
    await _settle()
    caller=asyncio.create_task(calls.cancel_attempt(call['id']))
    await _settle()
    assert log[-1]=='leave_call' and not caller.done()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):await caller
    await _settle()
    assert task.done() and log[-3:]==['binding_stop','broker_close','dispose_native']
    assert service.get_call(call['id'])['end_reason']=='owner_stop'
    service.close()


# spec: ANYM-409
async def test_a_failing_runtime_health_constructor_leaves_the_call_running_with_a_no_op_stand_in(monkeypatch):
    import voice
    from audio_health import NoRuntimeAudioHealth
    from test_voice import _stubbed_run_transport
    ran=[];events=[]
    class Service:
        def runtime_event(self,identity,event):events.append(event)
        def get_call(self,identity):return {'metrics':{}}
        def result(self,identity,**kwargs):pass
    def broken():raise RuntimeError('synthetic constructor failure')
    async def running():ran.append(asyncio.all_tasks())
    class Model(voice.FrameProcessor):
        audio_health=SimpleNamespace(snapshot=lambda:{'chunks':3,'audio_ms':60.0})
    monkeypatch.setattr(voice,'RuntimeAudioHealth',broken)
    before=asyncio.all_tasks()
    run=_stubbed_run_transport(monkeypatch,Service(),running)
    monkeypatch.setattr(voice,'create_model',lambda *args,**kwargs:Model())
    await run
    assert len(ran)==1
    # The health monitor task ran during the call and left nothing behind.
    assert any('voice-audio-health-' in task.get_name() for task in ran[0])
    assert asyncio.all_tasks()==before
    health=[event for event in events if event['kind']=='provider_audio_health']
    assert len(health)==1 and health[0]['runtime']=={} and health[0]['audio']=={'chunks':3,'audio_ms':60.0}
    # Output frames still pass through the delivery probe, which feeds the stand-in.
    from pipecat.frames.frames import OutputAudioRawFrame
    from pipecat.processors.frame_processor import FrameDirection
    probe=voice.AudioDeliveryProbe(NoRuntimeAudioHealth())
    pushed=[]
    async def push(frame,direction=FrameDirection.DOWNSTREAM):pushed.append(frame)
    probe.push_frame=push
    frame=OutputAudioRawFrame(audio=b'\0\0'*480,sample_rate=48000,num_channels=1)
    await probe.process_frame(frame,FrameDirection.DOWNSTREAM)
    assert pushed==[frame]


# spec: ANYM-409
async def test_a_cancellation_that_the_broker_close_swallows_still_ends_the_run_cancelled(monkeypatch,tmp_path):
    async def finished(*args,**kwargs):pass
    calls,service,log,new_call=_wire(monkeypatch,tmp_path,block=('broker_close',),swallow=True,run=finished)
    call=new_call('123456')
    await calls.dial(call)
    task=calls.active[call['id']]['task']
    await _settle()
    assert log[-1]=='broker_close'
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert log[-1]=='dispose_native'
    done=service.get_call(call['id'])
    assert done['status']=='finished' and done['end_reason']=='disconnected'
    service.close()


# spec: ANYM-409
async def test_cancel_attempt_holds_its_callers_cancellation_that_the_broker_close_swallows(monkeypatch,tmp_path):
    async def consuming(*args,**kwargs):
        # Like the pinned WorkerRunner.run(): the cancellation ends the run without propagating.
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:pass
    calls,service,log,new_call=_wire(monkeypatch,tmp_path,block=('broker_close',),swallow=True,run=consuming)
    call=new_call('123456')
    await calls.dial(call)
    await _settle()
    caller=asyncio.create_task(calls.cancel_attempt(call['id']))
    await _settle()
    assert log[-1]=='broker_close' and not caller.done()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):await caller
    assert log[-1]=='dispose_native'
    assert service.get_call(call['id'])['end_reason']=='owner_stop'
    service.close()


# spec: ANYM-409
async def test_a_cancellation_during_release_never_replaces_the_original_dial_failure(monkeypatch,tmp_path):
    calls,service,log,new_call=_wire(monkeypatch,tmp_path,fail=('play',),block=('leave_call',))
    dialing=asyncio.create_task(calls.dial(new_call('123456')))
    await _settle()
    assert log[-1]=='leave_call'
    dialing.cancel()
    with pytest.raises(RuntimeError,match='synthetic play failure'):await dialing
    assert log[-2:]==['broker_close','dispose_native'] and not calls.active
    # Two distinct cancellations: the first, from setup, is the one that propagates.
    calls,service,log,new_call=_wire(monkeypatch,tmp_path/'second',block=('play','leave_call'))
    dialing=asyncio.create_task(calls.dial(new_call('123456')))
    await _settle()
    assert log[-1]=='play'
    dialing.cancel('original setup cancellation')
    await _settle()
    assert log[-1]=='leave_call'
    dialing.cancel('second cancellation in release')
    with pytest.raises(asyncio.CancelledError) as raised:await dialing
    assert raised.value.args==('original setup cancellation',)
    assert log[-2:]==['broker_close','dispose_native'] and not calls.active
    service.close()


# spec: ANYM-409
async def test_cancel_attempt_inside_an_already_handled_cancellation_is_not_aborted(monkeypatch,tmp_path):
    calls,service,log,new_call=_wire(monkeypatch,tmp_path)
    call=new_call('123456')
    await calls.dial(call)
    await _settle()
    async def enclosing():
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:
            # No uncancel(): the task's cancel count stays positive while its cleanup runs.
            await calls.cancel_attempt(call['id'])
            return 'cleaned'
    handler=asyncio.create_task(enclosing())
    await _settle()
    handler.cancel()
    assert await handler=='cleaned'
    assert not calls.active and service.get_call(call['id'])['end_reason']=='owner_stop'
    service.close()
