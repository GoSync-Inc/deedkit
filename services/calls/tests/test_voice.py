import importlib
from types import SimpleNamespace

import pytest

from core import BriefInput, VoiceService, TOOL_MODELS


def test_native_gemini_uses_saved_prompt_voice_and_model(tmp_path):
    voice=importlib.import_module('voice')
    store=VoiceService(tmp_path)
    brief=store.prepare(BriefInput(goal='Узнать время',counterparty='Поставщик',success_fields=['time'],voice='Kore'),[])
    model=voice.create_model(brief,'fixture-not-a-key')
    assert model._settings.model == brief['model']
    assert model._settings.voice == 'Kore'
    assert model._settings.system_instruction == brief['prompt']
    store.close()


@pytest.mark.parametrize('language,code',[('ru','ru-RU'),('en','en-US'),('id','id-ID')])
async def test_flows_transcription_language_reaches_actual_google_setup(tmp_path,monkeypatch,language,code):
    from google.genai import _common, _live_converters, types
    from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
    from voice import create_model
    sent=[]
    async def connect(self,config):
        sent.append(_common.convert_to_dict(_live_converters._LiveConnectParameters_to_mldev(
            api_client=self._client._api_client,from_object=types.LiveConnectParameters(
                model=self._settings.model,config=config).model_dump(exclude_none=True))))
    monkeypatch.setattr(GeminiLiveLLMService,'_connection_task_handler',connect)
    store=VoiceService(tmp_path)
    brief=store.prepare(BriefInput(goal='Delivery',counterparty='Browser',language=language,
        success_fields=['window'],dialog_engine='gemini_live_flows'),[])
    model=create_model(brief,'fixture-not-a-key')
    config=types.LiveConnectConfig(response_modalities=['AUDIO'],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig())
    await model._connection_task_handler(config)
    for direction in ['inputAudioTranscription','outputAudioTranscription']:
        assert sent[0]['setup'][direction].get('language_codes')==[code]
    store.close()


async def test_each_schema_handler_calls_its_own_scoped_tool_and_returns_result():
    voice=importlib.import_module('voice')
    seen=[]
    async def handler(name,params):
        seen.append((name,params.arguments))
        await params.result_callback({'tool':name})
    schemas=voice.tool_schemas(handler).standard_tools
    assert {s.name for s in schemas} == set(TOOL_MODELS)
    for schema in schemas:
        returned=[]
        async def result(value):
            returned.append(value)
        params=SimpleNamespace(arguments={'fixture':schema.name},result_callback=result)
        await schema.handler(params)
        assert returned == [{'tool':schema.name}]
    assert [name for name,args in seen] == list(TOOL_MODELS)


async def test_owner_update_sends_realtime_text_not_only_local_context():
    voice=importlib.import_module('voice')
    frames=[]
    class Worker:
        async def queue_frames(self,values):
            frames.extend(values)
    decision={'id':'decision-1','question':'Extra fee?','answer':'Нет'}
    await voice.send_owner_update(Worker(),decision)
    from pipecat.frames.frames import InputTextRawFrame
    assert len(frames)==1 and isinstance(frames[0],InputTextRawFrame)
    assert 'OWNER DECISION FROM LOCAL INTERFACE' in frames[0].text and 'Нет' in frames[0].text
    assert frames[0].append_to_context is False, 'Owner control text must not become an assistant transcript'


async def test_pipeline_error_marks_failure_without_leaking_provider_error_payload():
    voice=importlib.import_module('voice')
    state={};messages=[]
    class RTVI:
        async def send_server_message(self,value):
            messages.append(value)
    await voice.report_error(state,RTVI())
    assert state['end_reason']=='model_error'
    assert messages[0]['kind']=='error' and messages[0]['code']=='unknown'


@pytest.mark.parametrize('status,category,code',[
    (401,None,'authentication'),(402,None,'quota'),(403,None,'authorization'),
    (429,None,'rate_limit'),(None,'quota','quota'),(None,'private-fixture','unknown')])
async def test_model_failure_records_only_safe_category_and_status(tmp_path,status,category,code):
    from pipecat.frames.frames import ErrorFrame
    from voice import report_error
    secret='fixture-key-MUST-NOT-LEAK'
    class ProviderError(Exception):
        status_code=status
    store=VoiceService(tmp_path)
    brief=store.prepare(BriefInput(goal='Synthetic',counterparty='Test',success_fields=['time']),[])
    call=store.start(brief['id']);state={'service':store,'call_id':call['id']};messages=[]
    class RTVI:
        async def send_server_message(self,value):messages.append(value)
    frame=ErrorFrame(error=secret,exception=ProviderError(secret),category=SimpleNamespace(value=category))
    await report_error(state,RTVI(),frame)
    assert messages[0]['code']==code
    event=store.get_call(call['id'])['events'][-1]
    assert event['kind']=='model_error' and event['code']==code
    assert event.get('http_status')==status
    import json
    assert secret not in json.dumps([messages,event])
    store.close()


async def test_a_failing_provider_audio_health_write_never_skips_transport_cleanup(monkeypatch):
    import voice
    from telegram_audio import pcm_transport
    writes=[];results=[]
    class Service:
        def runtime_event(self,identity,event):
            writes.append(event['kind'])
            if event['kind']=='provider_audio_health':raise RuntimeError('synthetic event store failure')
        def get_call(self,identity):return {'metrics':{}}
        def result(self,identity,**kwargs):results.append(kwargs)
    class Runner:
        def __init__(self,*args,**kwargs):pass
        async def add_workers(self,*workers):pass
        async def run(self):pass
    class Model(voice.FrameProcessor):
        audio_health=SimpleNamespace(snapshot=lambda:{})
    monkeypatch.setattr(voice,'create_model',lambda *args,**kwargs:Model())
    monkeypatch.setattr(voice,'WorkerRunner',Runner)
    monkeypatch.setattr(voice,'LLMContextAggregatorPair',lambda context:(
        voice.FrameProcessor(),voice.FrameProcessor()))
    state={'service':Service(),'call_id':'synthetic','tts_metrics':[{'synthetic':True}]}
    async def send(audio):pass
    await voice.run_transport(pcm_transport(send,lambda value:None),{'dialog_engine':'gemini_live'},'synthetic-key',state)
    # The health write failed, yet the inbound diagnostics, the TTS metrics and the rest of cleanup still ran.
    assert writes==['provider_audio_health','inbound_audio']
    assert results==[{'metrics':{'replicated_tts':[{'synthetic':True}]}}]


# spec: ANYM-382
async def test_a_failing_replicated_tts_metrics_write_never_masks_the_runner_exception(monkeypatch):
    import voice
    class Service:
        def runtime_event(self,identity,event):pass
        def get_call(self,identity):raise KeyError('synthetic metrics read failure')
        def result(self,identity,**kwargs):pass
    class Runner:
        def __init__(self,*args,**kwargs):pass
        async def add_workers(self,*workers):pass
        async def run(self):raise RuntimeError('synthetic runner failure')
    class Model(voice.FrameProcessor):
        audio_health=SimpleNamespace(snapshot=lambda:{})
    monkeypatch.setattr(voice,'create_model',lambda *args,**kwargs:Model())
    monkeypatch.setattr(voice,'WorkerRunner',Runner)
    monkeypatch.setattr(voice,'LLMContextAggregatorPair',lambda context:(
        voice.FrameProcessor(),voice.FrameProcessor()))
    state={'service':Service(),'call_id':'synthetic','tts_metrics':[{'synthetic':True}]}
    async def send(audio):pass
    from telegram_audio import pcm_transport
    with pytest.raises(RuntimeError,match='synthetic runner failure'):
        await voice.run_transport(pcm_transport(send,lambda value:None),{'dialog_engine':'gemini_live'},'synthetic-key',state)


def _stubbed_run_transport(monkeypatch,service,runner_run,probe=None):
    import voice
    class Runner:
        def __init__(self,*args,**kwargs):pass
        async def add_workers(self,*workers):pass
        async def run(self):await runner_run()
    class Model(voice.FrameProcessor):
        audio_health=SimpleNamespace(snapshot=lambda:{})
    monkeypatch.setattr(voice,'create_model',lambda *args,**kwargs:Model())
    monkeypatch.setattr(voice,'WorkerRunner',Runner)
    monkeypatch.setattr(voice,'LLMContextAggregatorPair',lambda context:(
        voice.FrameProcessor(),voice.FrameProcessor()))
    if probe:monkeypatch.setattr(voice,'PCMProbe',probe)
    from telegram_audio import pcm_transport
    async def send(audio):pass
    state={'service':service,'call_id':'synthetic','tts_metrics':[{'synthetic':True}]}
    return voice.run_transport(pcm_transport(send,lambda value:None),{'dialog_engine':'gemini_live'},'synthetic-key',state)


# spec: ANYM-382
async def test_a_failing_replicated_tts_metrics_result_write_never_masks_the_runner_exception(monkeypatch):
    called=[]
    class Service:
        def runtime_event(self,identity,event):pass
        def get_call(self,identity):return {'metrics':{}}
        def result(self,identity,**kwargs):
            called.append('result');raise KeyError('synthetic result write failure')
    async def failing():raise RuntimeError('synthetic runner failure')
    with pytest.raises(RuntimeError,match='synthetic runner failure'):
        await _stubbed_run_transport(monkeypatch,Service(),failing)
    assert called==['result']


# spec: ANYM-382
async def test_a_failing_inbound_probe_constructor_never_stops_the_call_before_the_runner(monkeypatch):
    ran=[];events=[]
    class Service:
        def runtime_event(self,identity,event):events.append(event['kind'])
        def get_call(self,identity):return {'metrics':{}}
        def result(self,identity,**kwargs):pass
    def broken():raise RuntimeError('synthetic constructor failure')
    async def running():ran.append(True)
    await _stubbed_run_transport(monkeypatch,Service(),running,probe=broken)
    # The runner started, and the empty stand-in still produced its end-of-call diagnostics event.
    assert ran==[True] and events==['provider_audio_health','inbound_audio']
