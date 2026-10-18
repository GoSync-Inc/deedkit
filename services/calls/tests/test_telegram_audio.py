"""Telegram Grok media wiring with a synthetic transport; no broker, Telegram or xAI connection."""
from fastapi.testclient import TestClient

from app import create_app
from audio_probe import PCMProbe
from outbound import AgentExternalBrief
from telegram_audio import MediaDiagnostics, pcm_transport
from test_tasks import Archive, BRIEF, HEADERS, owner_headers


# spec: ANYM-359
async def test_telegram_grok_uses_xai_key_and_declared_input_rate(tmp_path,monkeypatch):
    import voice
    from grok_voice import AuditedGrok
    monkeypatch.setenv('VOICE_TELEGRAM_SOCKET',str(tmp_path/'broker.sock'))
    monkeypatch.setenv('VOICE_TELEGRAM_TOKEN_FILE',str(tmp_path/'broker.token'))
    data=tmp_path/'data'
    app=create_app(data,Archive())
    with TestClient(app) as client:
        client.headers.update(owner_headers(data))
        for name in ('gemini','xai'):
            client.post('/api/credentials',json={name:f'synthetic-{name}-key'},headers=HEADERS)
        client.post('/api/call-voice',json={'provider':'grok_realtime','grok_voice':'ara'},headers=HEADERS)
        service=app.state.service
        brief=service.prepare(service.with_call_voice(AgentExternalBrief(
            **{**BRIEF,'source_message_ids':[]},channel='telegram',account='telegram',destination='123456')),[])
        call=service.start(brief['id'])
        captured={}
        def model(brief,key,recorder=None,input_rate=None):
            captured.update(brief=brief,key=key,input_rate=input_rate)
            raise RuntimeError('synthetic stop before any provider connection')
        monkeypatch.setattr(voice,'create_model',model)
        async def send(audio):pass
        state={'service':service,'call_id':call['id'],'brief':call['brief'],'supervisor':None,
               'audio_probe':PCMProbe(),'media':MediaDiagnostics()}
        await app.state.outbound.providers['telegram']._run(call['id'],pcm_transport(send,lambda value:None),state)
        assert captured['key']=='synthetic-xai-key' and captured['input_rate']==48000
        grok=AuditedGrok(captured['brief'],captured['key'],input_rate=captured['input_rate'])
        wire=[]
        async def capture(event):wire.append(event.model_dump(exclude_none=True))
        grok.send_client_event=capture
        await grok._send_session_update()
        assert wire[0]['session']['voice']=='ara'
        assert wire[0]['session']['audio']['input']['format']=={'type':'audio/pcm','rate':48000}
        assert service.get_call(call['id'])['status'] not in {'dialing','active'}

