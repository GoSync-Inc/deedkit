"""Synthetic provider events only; never contact xAI or a telephony account."""
import base64
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pipecat.frames.frames import InputAudioRawFrame, InputTextRawFrame, TTSAudioRawFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.xai.realtime.llm import GrokRealtimeLLMService

from app import create_app
from core import AgentBrief, BriefInput, CallVoice, GROK_MODEL, ServiceError, TOOL_MODELS, VoiceService
from outbound import AgentExternalBrief, ExternalBrief
from test_tasks import Archive, BRIEF, HEADERS, owner_headers
from voice import create_model, tool_schemas


def grok_brief(service, **kwargs):
    return service.prepare(BriefInput(**{**BRIEF, 'dialog_engine':'grok_realtime', **kwargs}), [])


def owner_default_grok(service, grok_voice='rex', **kwargs):
    # ANYM-359: agents never choose the engine; Grok comes from the owner's call voice.
    service.set_call_voice(CallVoice(provider='grok_realtime', grok_voice=grok_voice))
    return service.prepare(service.with_call_voice(AgentBrief(**{**BRIEF, **kwargs})), [])


@pytest.mark.parametrize('language',['ru','en','id'])
def test_grok_snapshot_pins_provider_and_does_not_reuse_gemini_learning(tmp_path,language):
    service=VoiceService(tmp_path)
    service.learning.active_candidate=lambda:{'prompt_version':'gemini-only','instruction':'GEMINI CANDIDATE'}
    brief=owner_default_grok(service, grok_voice='rex',language=language)
    assert brief['dialog_engine']=='grok_realtime' and brief['model']==GROK_MODEL and brief['grok_voice']=='rex'
    assert 'GEMINI CANDIDATE' not in brief['prompt']
    model=create_model(brief, 'synthetic-xai-key')
    assert isinstance(model, GrokRealtimeLLMService)
    assert model._settings.model==GROK_MODEL
    assert model._settings.session_properties.voice=='rex'
    assert model._settings.system_instruction==brief['prompt']
    assert model._settings.session_properties.audio.input.transcription.language_hint==language
    service.close()


@pytest.mark.parametrize('channel,account,destination',[
    ('telegram','telegram','123'),('whatsapp','twilio','+12025550123')])
def test_grok_rejects_unverified_channels_before_persisting(tmp_path,channel,account,destination):
    # ANYM-359 removed the refusal: the historical name stays, both routes are now accepted.
    service=VoiceService(tmp_path)
    service.set_call_voice(CallVoice(provider='grok_realtime',grok_voice='eve'))
    brief=service.prepare(service.with_call_voice(AgentExternalBrief(**BRIEF,channel=channel,
        account=account,destination=destination)),[])
    assert brief['channel']==channel and brief['model']==GROK_MODEL and brief['grok_voice']=='eve'
    owner=service.prepare(ExternalBrief(**BRIEF,dialog_engine='grok_realtime',channel=channel,
        account=account,destination=destination),[])
    assert owner['model']==GROK_MODEL
    assert service.db.execute('SELECT count(*) FROM briefs').fetchone()[0]==2
    service.close()


def test_grok_rejects_google_custom_voice(tmp_path):
    service=VoiceService(tmp_path)
    with pytest.raises(ServiceError,match='grok_native_voice_required'):
        grok_brief(service,custom_voice_id='voice_synthetic')
    service.close()


async def test_grok_serializes_only_scoped_tools_and_returns_tool_results(tmp_path):
    service=VoiceService(tmp_path)
    brief=grok_brief(service)
    call=service.start(brief['id']);service.connected(call['id'])
    model=create_model(brief,'synthetic-key')
    context=LLMContext()
    async def handler(name,params):
        await params.result_callback(service.tool(call['id'],name,params.arguments,params.tool_call_id))
    context.set_tools(tool_schemas(handler));model._context=context
    wire=[]
    async def send(event):wire.append(event.model_dump(exclude_none=True))
    model.send_client_event=send
    await model._send_session_update()
    session=wire[0]['session']
    assert session['instructions']==brief['prompt'] and session['voice']=='rex'
    assert {tool['name'] for tool in session['tools']}==set(TOOL_MODELS)
    assert all(tool['type']=='function' for tool in session['tools'])
    service.transcript(call['id'],'user','Delivery is 12–13.')
    tool=next(s for s in context.tools.standard_tools if s.name=='record_answer')
    returned=[]
    async def result(value):returned.append(value)
    params=SimpleNamespace(arguments={'field':'delivery_window','value':'12–13','evidence':'12–13'},
        tool_call_id='xai-function-1',result_callback=result)
    await tool.handler(params);await tool.handler(params)
    assert returned[0]==returned[1] and returned[0]['ok']
    assert service.get_call(call['id'])['answers']['delivery_window']['value']=='12–13'
    await model._send_tool_result(params.tool_call_id,json.dumps(returned[0]))
    assert wire[-1]['item']['type']=='function_call_output'
    assert wire[-1]['item']['call_id']=='xai-function-1'
    assert json.loads(wire[-1]['item']['output'])['ok']
    service.close()


async def test_grok_pcm_and_transcripts_reach_audit_without_storing_audio(tmp_path,monkeypatch):
    service=VoiceService(tmp_path)
    seen=[];frames=[];wire=[]
    model=create_model(grok_brief(service),'synthetic-key',lambda role,text:seen.append((role,text)))
    async def push(self,frame,direction=FrameDirection.DOWNSTREAM):frames.append(frame)
    async def send(event):wire.append(event.model_dump(exclude_none=True))
    monkeypatch.setattr(GrokRealtimeLLMService,'push_frame',push)
    model.send_client_event=send
    pcm=b'\x00\x10'*480
    model._ensure_audio_config(24000,24000)
    await model._send_user_audio(InputAudioRawFrame(pcm,24000,1))
    assert wire==[], 'Audio must not be sent before session.updated'
    model._api_session_ready=True
    await model._send_user_audio(InputAudioRawFrame(pcm,24000,1))
    assert base64.b64decode(wire[-1]['audio'])==pcm
    await model._handle_evt_audio_delta(SimpleNamespace(item_id='audio-1',content_index=0,delta=base64.b64encode(pcm).decode()))
    assert next(f for f in frames if isinstance(f,TTSAudioRawFrame)).audio==pcm
    assert model.audio_health.snapshot()['audio_ms']==20
    await model._handle_evt_input_audio_transcription_completed(SimpleNamespace(item_id='user-1',transcript='12–13'))
    await model._handle_evt_audio_transcript_delta(SimpleNamespace(delta='Understood.'))
    assert seen==[('user','12–13'),('assistant','Understood.')]
    assert 'audio' not in json.dumps(seen)
    service.close()


async def test_grok_owner_update_uses_system_input_without_assistant_transcript(tmp_path):
    service=VoiceService(tmp_path)
    model=create_model(grok_brief(service),'synthetic-key')
    wire=[]
    async def send(event):wire.append(event.model_dump(exclude_none=True))
    model.send_client_event=send
    await model.process_frame(InputTextRawFrame(text='OWNER DECISION FROM LOCAL INTERFACE: decline'),FrameDirection.DOWNSTREAM)
    assert len(wire)==1 and wire[0]['item']['role']=='system'
    assert wire[0]['item']['content'][0]['text'].endswith('decline')
    service.close()


def test_xai_key_is_separate_private_and_removed_on_forget(tmp_path,monkeypatch):
    monkeypatch.setenv('VOICE_PERSIST_CREDENTIALS','1')
    with TestClient(create_app(tmp_path,Archive())) as client:
        client.headers.update(owner_headers(tmp_path))
        for body in ({},{'xai':'   '},{'xai':'secret'*200}):
            response=client.post('/api/credentials',json=body,headers=HEADERS)
            assert response.status_code==422 and 'secretsecret' not in response.text
        response=client.post('/api/credentials',json={'xai':'synthetic-xai-key'},headers=HEADERS)
        assert response.json()['credentials']=={'gemini':False,'xai':True}
        config=client.get('/api/config').json()
        assert config['credentials']=={'gemini':False,'xai':True}
        assert 'synthetic-xai-key' not in json.dumps(config)
        assert (tmp_path/'model-key.json').stat().st_mode&0o077==0
    with TestClient(create_app(tmp_path,Archive())) as client:
        client.headers.update(owner_headers(tmp_path))
        assert client.get('/api/config').json()['credentials']['xai']
        client.post('/api/credentials',json={'gemini':'synthetic-gemini-key'},headers=HEADERS)
        assert client.get('/api/config').json()['credentials']=={'gemini':True,'xai':True}
        assert client.delete('/api/credentials',headers=HEADERS).json()['credentials']=={'gemini':False,'xai':False}
    assert not (tmp_path/'model-key.json').exists()


def test_grok_followup_preserves_model_and_owner_decision(tmp_path):
    service=VoiceService(tmp_path)
    brief=owner_default_grok(service,approval_fields=['delivery_window'])
    service.set_call_voice(CallVoice(provider='gemini_live'))
    call=service.start(brief['id']);cid=call['id'];service.connected(cid)
    service.transcript(cid,'user','12–13')
    decision=service.tool(cid,'record_answer',{'field':'delivery_window','value':'12–13','evidence':'12–13'},'xai-1')['owner_decision']
    service.stop(cid,'awaiting_owner');service.decide(cid,decision['decision_id'],'No')
    child=service.prepare_followup(cid,decision['decision_id'])
    assert child['dialog_engine']=='grok_realtime' and child['model']==GROK_MODEL
    assert child['grok_voice']==brief['grok_voice'] and child['followup']['owner_answer']=='No'
    service.close()


# spec: ANYM-359
async def test_grok_declares_input_format_matching_transport_frames(tmp_path):
    from pipecat.frames.frames import StartFrame
    from pipecat.pipeline.worker import PipelineParams
    from pipecat.serializers.twilio import TwilioFrameSerializer
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
    from telegram_audio import pcm_transport
    from voice import input_sample_rate, webrtc_transport

    service=VoiceService(tmp_path)
    brief=owner_default_grok(service)
    async def send(audio):pass
    serializer=TwilioFrameSerializer('MZ'+'1'*32,params=TwilioFrameSerializer.InputParams(auto_hang_up=False))
    twilio=FastAPIWebsocketTransport(websocket=SimpleNamespace(),params=FastAPIWebsocketParams(
        audio_in_enabled=True,audio_out_enabled=True,audio_out_sample_rate=8000,audio_out_end_silence_secs=0,serializer=serializer))
    pipeline=PipelineParams()
    await serializer.setup(StartFrame(audio_in_sample_rate=pipeline.audio_in_sample_rate,audio_out_sample_rate=8000))
    mulaw=base64.b64encode(bytes(range(256))*4).decode()
    twilio_frame=None
    for _ in range(8):
        twilio_frame=await serializer.deserialize(json.dumps({'event':'media','media':{'payload':mulaw}}))
        if twilio_frame is not None and twilio_frame.audio:break
    rates={'browser':(webrtc_transport(SmallWebRTCConnection([])),pipeline.audio_in_sample_rate),
           'twilio':(twilio,twilio_frame.sample_rate),
           # Telegram pushes InputAudioRawFrame(sample_rate=48000) itself (telegram_audio.dial).
           'telegram':(pcm_transport(send,lambda value:None),48000)}
    for name,(transport,frames) in rates.items():
        rate=input_sample_rate(transport)
        assert rate==frames, name
        model=create_model(brief,'synthetic-xai-key',input_rate=rate)
        wire=[]
        async def capture(event):wire.append(event.model_dump(exclude_none=True))
        model.send_client_event=capture
        await model._send_session_update()
        audio_in=wire[0]['session']['audio']['input']
        assert audio_in['format']=={'type':'audio/pcm','rate':frames}, name
        assert audio_in['transcription']['language_hint']=='ru'
    assert [rates[n][1] for n in rates]==[16000,16000,48000]
    service.close()
