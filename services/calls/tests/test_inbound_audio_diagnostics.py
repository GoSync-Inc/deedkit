import asyncio
import math
import struct
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
    assert result['input_sample_rates']==[16000]


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
