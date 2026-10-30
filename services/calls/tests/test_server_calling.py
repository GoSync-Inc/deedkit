import json

from fastapi.testclient import TestClient
from app import create_app
from test_tasks import owner_headers, Archive,BRIEF,HEADERS


def test_telegram_only_setup_preserves_public_url_before_twilio_credentials(tmp_path):
    with TestClient(create_app(tmp_path,Archive()), headers=owner_headers(tmp_path)) as client:
        setup={'enabled':False,'allowed':{'telegram':['123456789']},
               'twilio':{'public_url':'https://voice.test/voice'}}
        response=client.post('/api/calling',json=setup,headers=HEADERS)
        assert response.status_code==200
        status=response.json()
        assert status['twilio']['configured'] is False
        assert status['twilio']['public_url']=='https://voice.test/voice'
        assert client.get('/api/calling').json()['twilio']['public_url']=='https://voice.test/voice'
    with TestClient(create_app(tmp_path,Archive()), headers=owner_headers(tmp_path)) as client:
        assert client.get('/api/calling').json()['twilio']['public_url']=='https://voice.test/voice'


def test_private_server_credentials_routes_and_no_browser_dial_bypass(tmp_path,monkeypatch):
    monkeypatch.setenv('VOICE_PERSIST_CREDENTIALS','1');monkeypatch.setenv('VOICE_EXTERNAL_MCP','1')
    with TestClient(create_app(tmp_path,Archive()), headers=owner_headers(tmp_path)) as client:
        assert client.post('/api/calling',json={'enabled':False}).status_code==403
        assert client.post('/api/credentials',json={'gemini':'fixture-model-key'},headers=HEADERS).status_code==200
        setup={'enabled':True,'allowed':{'phone':['+628123456789']},'twilio':{
            'account_sid':'AC'+'1'*32,'auth_token':'fixture-secret','phone_from':'+12025550123',
            'public_url':'https://voice.test/voice'}}
        assert client.post('/api/calling',json=setup,headers=HEADERS).status_code==200
        status=client.get('/api/calling').json()
        assert status['twilio']['configured'] is True
        assert 'fixture-secret' not in json.dumps(status)
        assert 'fixture-model-key' not in json.dumps(client.get('/api/config').json())
        b=client.post('/api/external/briefs',json={**BRIEF,'channel':'phone','account':'twilio','destination':'+628123456789'},headers=HEADERS).json()
        assert client.post('/api/calls/start',json={'brief_id':b['id']},headers=HEADERS).json()['detail']=='use_dial_call'
        assert client.get('/api/calls').json()['items']==[]
        assert client.post('https://evil.test/provider/twilio/status/no-call',data={}).status_code==403
        assert client.get('https://voice.test/api/calls').status_code==403
    assert (tmp_path/'model-key.json').stat().st_mode&0o077==0
    assert (tmp_path/'calling.json').stat().st_mode&0o077==0
    with TestClient(create_app(tmp_path,Archive()), headers=owner_headers(tmp_path)) as client:
        assert client.get('/api/config').json()['credentials']['gemini'] is True
        assert client.get('/api/calling').json()['allowed']['phone']==['+628123456789']
        assert client.delete('/api/credentials',headers=HEADERS).status_code==200
    assert not (tmp_path/'model-key.json').exists()


def test_signed_provider_webhook_bound_to_exact_attempt(tmp_path):
    from outbound import ExternalBrief,TwilioCalls
    import hmac,hashlib,base64
    app=create_app(tmp_path,Archive())
    cfg={'account_sid':'AC'+'1'*32,'auth_token':'fixture-secret','public_url':'https://voice.test/voice'}
    provider=TwilioCalls(cfg);app.state.outbound.providers['twilio']=provider
    with TestClient(app, headers=owner_headers(tmp_path)) as client:
        b=app.state.service.prepare(ExternalBrief(**BRIEF,channel='phone',account='twilio',destination='+628123456789'),[])
        call=app.state.service.start(b['id']);sid='CA'+'2'*32
        app.state.service.provider_update(call['id'],provider_call_id=sid,dial_state='accepted')
        path='/provider/twilio/status/'+call['id'];url=cfg['public_url']+path
        payload={'AccountSid':cfg['account_sid'],'CallSid':sid,'CallStatus':'completed'}
        signature=base64.b64encode(hmac.new(b'fixture-secret',(url+''.join(k+payload[k] for k in sorted(payload))).encode(),hashlib.sha1).digest()).decode()
        assert client.post('https://voice.test'+path,data=payload).status_code==403
        response=client.post('https://voice.test'+path,data=payload,headers={'X-Twilio-Signature':signature})
        assert response.status_code==200
        assert app.state.service.get_call(call['id'])['status']=='finished'


def test_signed_websocket_carries_real_twilio_audio_and_hangs_up(monkeypatch,tmp_path):
    import base64,hmac,hashlib
    import numpy as np
    import voice
    from pipecat.frames.frames import OutputAudioRawFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineWorker
    from pipecat.workers.runner import WorkerRunner
    from outbound import ExternalBrief,TwilioCalls
    async def fixture_voice(transport,brief,key,state):
        worker=PipelineWorker(Pipeline([transport.input(),transport.output()]),idle_timeout_secs=None)
        runner=WorkerRunner(handle_sigint=False);state['runner']=runner;await runner.add_workers(worker)
        @worker.event_handler('on_pipeline_started')
        async def ready(worker,frame):
            state['service'].connected(state['call_id'])
            tone=(np.sin(np.arange(24000)*2*np.pi*440/24000)*4000).astype(np.int16).tobytes()
            await worker.queue_frames([OutputAudioRawFrame(tone,24000,1)])
        @transport.event_handler('on_client_disconnected')
        async def disconnected(transport,socket):await runner.cancel()
        await runner.run()
    monkeypatch.setattr(voice,'run_transport',fixture_voice)
    app=create_app(tmp_path,Archive());hung_up=[]
    cfg={'account_sid':'AC'+'1'*32,'auth_token':'fixture-secret','public_url':'https://voice.test/voice'}
    provider=TwilioCalls(cfg)
    async def hangup(sid):hung_up.append(sid)
    provider.hangup=hangup;app.state.outbound.providers['twilio']=provider
    with TestClient(app, headers=owner_headers(tmp_path)) as client:
        client.post('/api/credentials',json={'gemini':'fixture-model-key'},headers=HEADERS)
        b=app.state.service.prepare(ExternalBrief(**BRIEF,channel='phone',account='twilio',destination='+628123456789'),[])
        call=app.state.service.start(b['id']);sid='CA'+'2'*32
        app.state.service.provider_update(call['id'],provider_call_id=sid,dial_state='accepted')
        app.state.service.db.execute('INSERT INTO outbound_attempts VALUES (?,?)',(call['id'],'fixture-stream-token'));app.state.service.db.commit()
        path='/provider/twilio/media/'+call['id'];url=cfg['public_url']+path
        signature=base64.b64encode(hmac.new(b'fixture-secret',url.encode(),hashlib.sha1).digest()).decode()
        with client.websocket_connect('wss://voice.test'+path,headers={'X-Twilio-Signature':signature}) as socket:
            socket.send_json({'event':'connected'})
            socket.send_json({'event':'start','start':{'accountSid':cfg['account_sid'],'callSid':sid,'streamSid':'MZ'+'3'*32,
                'customParameters':{'attempt_token':'fixture-stream-token'},'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}})
            packet=socket.receive_json()
            assert packet['event']=='media' and len(base64.b64decode(packet['media']['payload']))>0
        # Closing the transport terminates exactly this attempt; no fallback call.
    assert hung_up==[sid]
