import asyncio
import base64
import json
from types import SimpleNamespace

import numpy as np

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
