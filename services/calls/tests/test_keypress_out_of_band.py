"""ANYM-451: a repeated key press on a phone call goes out-of-band; the same call reconnects to a new media session."""
import asyncio
import contextlib
import base64
import hashlib
import hmac
import json
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from starlette.datastructures import URL
from starlette.websockets import WebSocketState

from core import (HANDOVER_FAILURE, KEYPRESS_LIMIT, KEYPRESS_OUT_OF_BAND_LIMIT, REHEARSAL_NOTE, RESUME_HISTORY_CHARS,
                  ServiceError, VoiceService, inbound_audio_verdict)
from outbound import ExternalBrief, TwilioCalls
from test_keypress import MENU, active_call, press, twilio_pipeline
from test_tasks import BRIEF, Archive

SID='CA'+'2'*32
CFG={'account_sid':'AC'+'1'*32,'auth_token':'fixture-secret','public_url':'https://voice.test/voice'}


def oob(store,cid,keys,tool_call_id,generation=None):
    """An accepted out-of-band press whose reconnect arrived: the call moves to the next media session."""
    result=store.tool(cid,'press_keys',{'keys':keys,'menu_quote':MENU},tool_call_id,generation=generation)
    assert result['dtmf_path']=='out_of_band',result
    store.handover_result(cid,tool_call_id,'accepted')
    return store.activate_generation(cid)


class Http:
    def __init__(self,status=200,error=None):
        self.status,self.error,self.posts=status,error,[]

    async def post(self,url,data=None,auth=None,timeout=None):
        self.posts.append((url,data))
        if self.error:raise self.error
        return SimpleNamespace(status_code=self.status)


# spec: ANYM-451
async def test_out_of_band_trigger_sticky_and_bounded(tmp_path):
    store=VoiceService(tmp_path)
    cid=active_call(store)
    first=press(store,cid,'3','p1')
    assert first['delivery']=='in_band_audio' and first['dtmf_path']=='in_band' and 'note' not in first
    # The same keys next: one Twilio update, then the call waits for its reconnect.
    again=press(store,cid,'3','p2')
    assert again['ok'] is True and again['delivery']=='out_of_band' and again['delivery_confirmed'] is False
    assert 'stay silent' in again['next_action']
    http=Http();provider=TwilioCalls(CFG);provider.http=http
    call={'id':cid,'provider_call_id':SID}
    await provider.send_digits(call,'fixture-stream-token','3')
    stream=('<Connect><Stream url="wss://voice.test/voice/provider/twilio/media/'+cid+'"><Parameter name="attempt_token" '
            'value="fixture-stream-token"/></Stream></Connect><Hangup/>')
    assert len(http.posts)==1 and http.posts[0][0].endswith('/Calls/'+SID+'.json')
    assert http.posts[0][1]=={'Twiml':'<Response><Play digits="w3"/>'+stream+'</Response>'}
    assert provider._stream(cid,'fixture-stream-token')==stream,'the same <Connect><Stream> the call was dialed with'
    for bad in ['1234','3w','<']:
        with pytest.raises(ServiceError):await provider.send_digits(call,'fixture-stream-token',bad)
    assert len(http.posts)==1
    store.handover_result(cid,'p2','accepted');assert store.activate_generation(cid)==2
    # Once out-of-band has worked, different keys go out-of-band directly ...
    assert press(store,cid,'4','p3')['dtmf_path']=='out_of_band'
    store.handover_result(cid,'p3','accepted');store.activate_generation(cid)
    # ... and the keys just sent out-of-band are never sent that way again.
    for i in range(2):
        same=press(store,cid,'4','p4-'+str(i))
        assert same['dtmf_path']=='in_band' and same['note']=='key_already_sent_out_of_band'
        assert 'did not accept' in same['next_action']
    for i,keys in enumerate('5678'):
        assert oob(store,cid,keys,'p5-'+str(i))
    assert sum(p['dtmf_path']=='out_of_band' for p in store.get_call(cid)['keypresses'])==KEYPRESS_OUT_OF_BAND_LIMIT==6
    limited=press(store,cid,'9','p9')
    assert limited['ok'] is True and limited['dtmf_path']=='in_band' and limited['note']=='out_of_band_limit_reached'
    # A browser rehearsal never leaves the media path; it says what a phone call would do.
    store.stop(cid)
    browser=active_call(store,'browser')
    assert press(store,browser,'3','b1')['dtmf_path']=='in_band'
    rehearsal=press(store,browser,'3','b2')
    assert rehearsal['dtmf_path']=='in_band' and rehearsal['note']==REHEARSAL_NOTE and not store.get_call(browser).get('handover')
    store.close()


def handler_harness(monkeypatch):
    """run_transport with fake worker and keypad: returns the captured tool handler and begin hook per session."""
    import voice
    seen,tones,contexts=[],[],[]
    class Keypad:
        async def preload(self):pass
        async def press(self,keys):tones.append(keys);return 520
        def playing(self):return False
    class RTVI:
        async def send_server_message(self,message):pass
        def event_handler(self,name):return lambda fn:fn
    class Worker:
        def __init__(self,*args,**kwargs):self.rtvi,self.handlers=RTVI(),{}
        def event_handler(self,name):
            def keep(fn):self.handlers[name]=fn;return fn
            return keep
        async def queue_frames(self,frames):pass
    class Runner:
        def __init__(self,**kwargs):pass
        async def add_workers(self,worker):seen[-1]['worker']=worker
        async def run(self):pass
        async def cancel(self):pass
    class Context(voice.LLMContext):
        def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);contexts.append(self)
    real_schemas=voice.tool_schemas
    def schemas(handler):
        seen.append({'handle':handler});return real_schemas(handler)
    monkeypatch.setattr(voice,'keypad_for',lambda transport,channel:Keypad())
    monkeypatch.setattr(voice,'tool_schemas',schemas)
    monkeypatch.setattr(voice,'PipelineWorker',Worker)
    monkeypatch.setattr(voice,'WorkerRunner',Runner)
    monkeypatch.setattr(voice,'LLMContext',Context)
    async def session(store,cid,state):
        transport=twilio_pipeline()[1]
        await voice.run_transport(transport,store.get_call(cid)['brief'],'fixture-not-a-key',state)
        return seen[-1]['handle'],seen[-1]['worker'].handlers['on_pipeline_started'],contexts[-1]
    return session,tones


async def call_tool(handle,name,arguments,tool_call_id):
    results=[]
    async def result(value):results.append(value)
    await handle(name,SimpleNamespace(arguments=arguments,tool_call_id=tool_call_id,result_callback=result))
    return results[-1]


# spec: ANYM-451
async def test_delivery_admission_is_call_scoped_and_never_repeated(tmp_path,monkeypatch):
    session,tones=handler_harness(monkeypatch)
    store=VoiceService(tmp_path)
    cid=active_call(store)
    updates,gate=[],asyncio.Event()
    async def out_of_band(keys):
        updates.append(keys);await gate.wait()
        if len(updates)>1:raise httpx.ReadError('response lost')
        return 'accepted'
    first={'service':store,'call_id':cid,'websocket':True,'generation':1,'out_of_band':out_of_band}
    handle,_,_=await session(store,cid,first)
    args={'keys':'3','menu_quote':MENU}
    assert (await call_tool(handle,'press_keys',args,'p1'))['tones_ms']==520 and tones==['3']
    # The update is in flight: a retry of the same tool call and a concurrent distinct press change nothing.
    pending=asyncio.create_task(call_tool(handle,'press_keys',args,'p2'))
    await asyncio.sleep(0.05)
    assert updates==['3']
    retry=await call_tool(handle,'press_keys',args,'p2')
    other=await call_tool(handle,'press_keys',{'keys':'5','menu_quote':MENU},'p3')
    assert other=={'error':'handover_in_progress','ok':False,'next_action':'Wait for the reconnect.'}
    gate.set();sent=await pending
    assert sent['delivery']=='out_of_band' and retry['delivery']=='out_of_band' and updates==['3'] and tones==['3']
    assert store.activate_generation(cid)==2
    second={**first,'generation':2,'resume':True}
    handle2,_,_=await session(store,cid,second)
    # After the handover the new session replays the stored result: no tones, no update.
    assert await call_tool(handle2,'press_keys',args,'p2')==sent and await call_tool(handle,'press_keys',args,'p2')==sent
    assert updates==['3'] and tones==['3']
    # The old session lost its authority over new presses.
    assert await call_tool(handle,'press_keys',{'keys':'7','menu_quote':MENU},'p4')=={'error':'stale_generation'}
    # Two concurrent distinct presses in the new session: one update, one handover_in_progress.
    both=await asyncio.gather(call_tool(handle2,'press_keys',{'keys':'7','menu_quote':MENU},'p5'),
                              call_tool(handle2,'press_keys',{'keys':'8','menu_quote':MENU},'p6'))
    assert sorted(r.get('error','sent') for r in both)==['handover_in_progress','sent']
    # Its response was lost: ambiguous, never resent, not even on a retry of the same tool call.
    assert updates==['3','7']
    await call_tool(handle2,'press_keys',{'keys':'7','menu_quote':MENU},'p5')
    assert updates==['3','7'] and store.get_call(cid)['handover']['state']=='ambiguous'
    assert store.activate_generation(cid)==3
    # The 20-press cap counts every session's presses.
    accepted=sum(e['kind']=='tool' and e['name']=='press_keys' and bool(e['result'].get('ok')) for e in store.get_call(cid)['events'])
    for i in range(KEYPRESS_LIMIT-accepted):
        assert store.tool(cid,'press_keys',args|{'keys':'7'},'cap-'+str(i),generation=3)['ok'] is True
    assert store.tool(cid,'press_keys',args|{'keys':'7'},'cap-last',generation=3)=={'error':'keypress_limit'}
    store.close()


def signature(url,params=None):
    payload=url+''.join(k+params[k] for k in sorted(params or {}))
    return base64.b64encode(hmac.new(b'fixture-secret',payload.encode(),hashlib.sha1).digest()).decode()


class Media:
    """A Twilio media stream socket for the app's own endpoint, in the test's event loop."""

    def __init__(self,cid,n):
        path='/provider/twilio/media/'+cid
        self.url=URL('wss://voice.test/voice'+path)
        self.headers={'x-twilio-signature':signature(CFG['public_url']+path)}
        self.inbox=[json.dumps({'event':'connected'}),json.dumps({'event':'start','start':{
            'accountSid':CFG['account_sid'],'callSid':SID,'streamSid':'MZ'+str(n)*32,
            'customParameters':{'attempt_token':'fixture-stream-token'},
            'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}})]
        self.client_state=self.application_state=WebSocketState.CONNECTED
        self.sent,self.closed_code,self.gone=[],None,asyncio.Event()

    async def accept(self):pass

    async def receive_text(self):
        if self.inbox:return self.inbox.pop(0)
        await asyncio.Event().wait()

    async def receive(self):
        await self.gone.wait()
        return {'type':'websocket.disconnect','code':1000}

    async def send_text(self,data):self.sent.append(json.loads(data))

    async def send_bytes(self,data):raise AssertionError('Twilio media is JSON text')

    async def close(self,code=1000):
        if self.closed_code is None:self.closed_code=code

    def media(self):return [m for m in self.sent if m['event']=='media']


async def until(condition,timeout=10):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


class Phone:
    """The real app (media endpoint, cleanup, owner stop, status callback); the voice session plays a tone."""

    def __init__(self,tmp_path,monkeypatch,handover_secs=3,send=None):
        import app as app_module
        import voice
        from pipecat.frames.frames import OutputAudioRawFrame
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineWorker
        from pipecat.workers.runner import WorkerRunner
        from settings import write_private
        self.sessions,self.connected,self.hung_up,self.digits,self.tasks=[],[],[],[],[]
        async def fixture_voice(transport,brief,key,state):
            self.sessions.append(state)
            worker=PipelineWorker(Pipeline([transport.input(),transport.output()]),idle_timeout_secs=None)
            runner=WorkerRunner(handle_sigint=False);state['runner']=runner;state['worker']=worker
            await runner.add_workers(worker)
            @worker.event_handler('on_pipeline_started')
            async def ready(worker,frame):
                if not state['resume']:self.connected.append(state['generation']);state['service'].connected(state['call_id'])
                await worker.queue_frames([tone()])
            @transport.event_handler('on_client_disconnected')
            async def disconnected(transport,socket):await runner.cancel()
            try:await runner.run()
            finally:
                # As run_transport records the end of each media session.
                service,cid,generation=state['service'],state['call_id'],state['generation']
                service.runtime_event(cid,{'kind':'media_session','generation':generation,'ended_by':service.media_ended_by(cid,generation)})
        def tone():
            return OutputAudioRawFrame((np.sin(np.arange(24000)*2*np.pi*440/24000)*4000).astype(np.int16).tobytes(),24000,1)
        self.tone=tone
        monkeypatch.setattr(voice,'run_transport',fixture_voice)
        monkeypatch.setattr(app_module,'HANDOVER_SECS',handover_secs)
        monkeypatch.setenv('VOICE_PERSIST_CREDENTIALS','1')
        write_private(tmp_path/'model-key.json',{'gemini':'fixture-model-key'})
        self.app=app_module.create_app(tmp_path,Archive())
        self.service=self.app.state.service
        provider=TwilioCalls(CFG)
        async def hangup(sid):self.hung_up.append(sid)
        async def send_digits(call,token,keys):
            self.digits.append((keys,token))
            if send:await send()
        provider.hangup=hangup;provider.send_digits=send_digits
        self.app.state.outbound.providers['twilio']=provider
        self.routes={(r.path,tuple(sorted(getattr(r,'methods',None) or ()))):r.endpoint for r in self.app.router.routes if hasattr(r,'endpoint')}

    def dial(self):
        b=self.service.prepare(ExternalBrief(**BRIEF,channel='phone',account='twilio',destination='+628123456789'),[])
        cid=self.service.start(b['id'])['id']
        self.service.provider_update(cid,provider_call_id=SID,dial_state='accepted')
        self.service.db.execute('INSERT INTO outbound_attempts VALUES (?,?)',(cid,'fixture-stream-token'));self.service.db.commit()
        self.service.transcript(cid,'user',MENU)
        self.sessions.clear();self.hung_up.clear();self.digits.clear()
        return cid

    async def stream(self,cid,n,joins=True):
        socket=Media(cid,n)
        task=asyncio.create_task(self.routes[('/provider/twilio/media/{identity}',())](socket,cid))
        self.tasks.append(task);socket.task=task
        if joins:await until(lambda:socket.media())
        else:await asyncio.wait_for(task,5)
        return socket

    async def close(self,socket):
        socket.gone.set()
        await asyncio.wait_for(asyncio.shield(socket.task),10)

    async def owner_stop(self,cid):
        return await self.routes[('/api/calls/{identity}/stop',('POST',))](cid)

    async def provider_status(self,cid,status):
        path='/provider/twilio/status/'+cid;params={'AccountSid':CFG['account_sid'],'CallSid':SID,'CallStatus':status}
        request=SimpleNamespace(state=SimpleNamespace(twilio_form=params),
                                headers={'x-twilio-signature':signature(CFG['public_url']+path,params)})
        return await self.routes[('/provider/twilio/status/{identity}',('POST',))](cid,request)

    async def press_out_of_band(self,state,keys,tool_call_id):
        """The session's model presses the same keys twice; the second goes through the real out-of-band path."""
        import voice
        service,cid,generation=self.service,state['call_id'],state['generation']
        for i in (0,1):
            result=service.tool(cid,'press_keys',{'keys':keys,'menu_quote':MENU},tool_call_id+str(i),generation=generation)
        assert service.claim_delivery(cid,tool_call_id+'1',generation)=='out_of_band'
        return await voice.send_out_of_band(state,service,cid,result,tool_call_id+'1')

    async def shutdown(self):
        for task in self.tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)


@contextlib.asynccontextmanager
async def phone(tmp_path,monkeypatch,**kwargs):
    harness=Phone(tmp_path,monkeypatch,**kwargs)
    async with harness.app.router.lifespan_context(harness.app):
        try:yield harness
        finally:await harness.shutdown()


def ended(service,cid,generation):
    return any(e['kind']=='media_session' and e['generation']==generation for e in service.get_call(cid)['events'])


# spec: ANYM-451
async def test_handover_generations_and_one_terminal_winner(tmp_path,monkeypatch):
    async with phone(tmp_path,monkeypatch) as p:
        service=p.service
        # Order A: the new stream connects before the old one closes.
        cid=p.dial()
        old=await p.stream(cid,3)
        assert (await p.press_out_of_band(p.sessions[0],'3','a'))['delivery']=='out_of_band'
        assert p.digits==[('3','fixture-stream-token')]
        new=await p.stream(cid,4)
        assert [s['generation'] for s in p.sessions]==[1,2] and p.sessions[1]['resume'] is True
        assert p.connected==[1],'the resumed stream never calls connected() again'
        # Old-generation callbacks released after admission have no effect; one generation is authorized.
        assert service.tool(cid,'press_keys',{'keys':'1','menu_quote':MENU},'late',generation=1)=={'error':'stale_generation'}
        assert service.tool(cid,'end_conversation',{'reason':'unavailable','summary':'x','evidence':MENU},'late-end',generation=1)=={'error':'stale_generation'}
        assert not service.authorized(cid,1,'owner_answer') and service.authorized(cid,2,'owner_answer')
        service.transcript(cid,'user','from the old stream',generation=1)
        assert 'from the old stream' not in json.dumps(service.get_call(cid)['events'])
        # A third socket during the phase is refused.
        third=await p.stream(cid,5,joins=False)
        assert third.closed_code==1008 and len(p.sessions)==2
        await p.close(old)
        assert ended(service,cid,1) and service.get_call(cid)['status']=='active' and p.hung_up==[],'old cleanup leaves the new one alone'
        # Later audio still reaches the new stream only.
        before=len(new.media());await p.sessions[1]['worker'].queue_frames([p.tone()])
        await until(lambda:len(new.media())>before)
        # Owner stop after admission: one winner, one hangup, one terminal save.
        assert (await p.owner_stop(cid))['end_reason']=='owner_stop'
        await asyncio.wait_for(asyncio.shield(new.task),10)
        call=service.get_call(cid)
        assert p.hung_up==[SID] and call['end_reason']=='owner_stop' and call['generation']==2
        assert [(e['generation'],e['ended_by']) for e in call['events'] if e['kind']=='media_session']==[(1,'handover'),(2,'end')]

        # Order B: the old stream closes first; owner stop in the gap before any reconnect.
        cid=p.dial()
        old=await p.stream(cid,6)
        await p.press_out_of_band(p.sessions[0],'4','b')
        old.gone.set()
        await until(lambda:ended(service,cid,1))
        assert service.handover_pending(cid,1) and service.get_call(cid)['status']=='active'
        call=await p.owner_stop(cid)
        assert call['status']=='finished' and call['end_reason']=='owner_stop' and p.hung_up==[SID]
        late=await p.stream(cid,7,joins=False)
        assert late.closed_code==1008 and p.hung_up==[SID] and len(p.sessions)==1

        # Order B, reconnect in the gap: the old cleanup sees it was replaced; the whole-call deadline is kept.
        cid=p.dial()
        old=await p.stream(cid,8)
        await p.press_out_of_band(p.sessions[0],'5','c')
        old.gone.set()
        await until(lambda:ended(service,cid,1))
        new=await p.stream(cid,9)
        await asyncio.wait_for(asyncio.shield(old.task),10)
        assert service.get_call(cid)['status']=='active' and p.hung_up==[] and p.connected==[1,1,1]
        await p.close(new)
        call=service.get_call(cid)
        assert p.hung_up==[SID] and call['status']=='finished' and call['end_reason']=='media_disconnected'
        assert [(e['generation'],e['ended_by']) for e in call['events'] if e['kind']=='media_session']==[(1,'handover'),(2,'end')]

        # The original deadline expiring in the gap ends the call once, as duration_limit, without a reset.
        cid=p.dial()
        old=await p.stream(cid,6)
        await p.press_out_of_band(p.sessions[0],'6','d')
        endpoint=p.routes[('/provider/twilio/media/{identity}',())]
        session=endpoint.__closure__[endpoint.__code__.co_freevars.index('session')].cell_contents
        session['deadline']=asyncio.get_running_loop().time()+0.2
        old.gone.set()
        await asyncio.wait_for(asyncio.shield(old.task),10)
        call=service.get_call(cid)
        assert call['end_reason']=='duration_limit' and p.hung_up==[SID] and not call.get('handover')


# spec: ANYM-451
async def test_handover_update_outcomes_are_isolated_and_visible(tmp_path,monkeypatch):
    outcome={'raise':None}
    async def send():
        if outcome['raise']:raise outcome['raise']
    async with phone(tmp_path,monkeypatch,handover_secs=0.5,send=send) as p:
        service=p.service
        # Rejected before acceptance: the original stream keeps working and the model is told plainly.
        outcome['raise']=ServiceError('provider_rejected')
        cid=p.dial()
        socket=await p.stream(cid,3)
        rejected=await p.press_out_of_band(p.sessions[0],'3','r')
        assert rejected=={'ok':False,'keys':'3','delivery':'out_of_band_rejected','dtmf_path':'out_of_band','delivery_confirmed':False,
                          'next_action':'Keys were not sent; follow the brief.'}
        assert not service.get_call(cid).get('handover') and service.get_call(cid)['status']=='active'
        before=len(socket.media());await p.sessions[0]['worker'].queue_frames([p.tone()])
        await until(lambda:len(socket.media())>before)
        # A non-handover socket close still ends the call as today.
        await p.close(socket)
        assert service.get_call(cid)['end_reason']=='media_disconnected' and p.hung_up==[SID]

        # Accepted, but no new stream within the handover window.
        outcome['raise']=None
        cid=p.dial()
        socket=await p.stream(cid,4)
        await p.press_out_of_band(p.sessions[0],'4','f')
        await p.close(socket)
        call=service.get_call(cid)
        assert call['end_reason']=='keypress_handover_failed' and call['handover_failure']==HANDOVER_FAILURE and p.hung_up==[SID]
        assert 'within 15 s' in HANDOVER_FAILURE and "owner's confirmation" in HANDOVER_FAILURE

        # Provider completion in the gap: the remote-hangup reason, noted, and no hangup of our own.
        cid=p.dial()
        socket=await p.stream(cid,5)
        await p.press_out_of_band(p.sessions[0],'5','c')
        socket.gone.set()
        await until(lambda:ended(service,cid,1))
        assert await p.provider_status(cid,'completed')=={'ok':True}
        await asyncio.wait({socket.task},timeout=10)   # the gap wait was cancelled by the stop
        call=service.get_call(cid)
        assert call['end_reason']=='provider_completed' and call['end_note']=='after_out_of_band_press' and p.hung_up==[]

        # Ambiguous (the response was lost): never resent; the original stream never closed, so the call goes on.
        outcome['raise']=httpx.ReadError('lost')
        cid=p.dial()
        socket=await p.stream(cid,6)
        ambiguous=await p.press_out_of_band(p.sessions[0],'6','m')
        assert ambiguous['delivery']=='out_of_band' and len(p.digits)==1
        await until(lambda:not service.get_call(cid).get('handover'))
        assert service.get_call(cid)['status']=='active' and len(p.digits)==1
        assert [e['outcome'] for e in service.get_call(cid)['events'] if e['kind']=='keypress_handover']==['ambiguous','stream_kept']
        before=len(socket.media());await p.sessions[0]['worker'].queue_frames([p.tone()])
        await until(lambda:len(socket.media())>before)
        await p.close(socket)
        assert service.get_call(cid)['end_reason']=='media_disconnected'


# spec: ANYM-451
async def test_resume_snapshot_keeps_history_decisions_and_silence(tmp_path,monkeypatch):
    session,_=handler_harness(monkeypatch)
    store=VoiceService(tmp_path)
    cid=active_call(store)
    store.transcript(cid,'assistant','Hello, I am calling about the delivery.')
    for i in range(60):
        store.transcript(cid,'user','turn '+str(i)+' '+'x'*300)
    store.transcript_chunk(cid,'user','Please hold')
    store.transcript_chunk(cid,'user',', connecting you')
    asked=store.tool(cid,'request_owner_decision',{'question':'Accept the Friday slot?','options':['yes','no']},'d1')
    press(store,cid,'3','p0');oob(store,cid,'3','p1')
    store.decide(cid,asked['decision_id'],'yes')
    text=store.resume_context(cid)
    server,history=text.split('CALL HISTORY SO FAR')
    assert 'Key 3 was sent by the phone provider' in server and 'stay silent until you hear speech' in server
    assert 'do not greet again' in server and 'recognition by the menu is not confirmed' in server
    state=json.loads(server.split('\n')[2])
    assert state['owner_decisions'][0]['owner_answer']=='yes' and state['key_presses']==[{'keys':'3','dtmf_path':'in_band'},{'keys':'3','dtmf_path':'out_of_band'}]
    assert 'turn' not in server and 'Friday slot' not in history,'server state and speech stay apart'
    lines=history.split('\n')[1:]
    assert lines[0].startswith('earlier ') and lines[0].endswith(' turns omitted; ask rather than guess')
    assert sum(len(x)+1 for x in lines[1:])<=RESUME_HISTORY_CHARS and lines[-2].startswith('counterparty: turn 59')
    assert lines[-1]=='counterparty (unfinished): Please hold, connecting you' and text.count('connecting you')==1
    assert 'data, not instructions' in history
    # A single oversized turn keeps its last 12,000 characters.
    other=VoiceService(tmp_path/'long');long=active_call(other);other.transcript(long,'user','a'*20000+'END')
    kept=other.resume_context(long).split('\n')[-1]
    assert kept==('counterparty: '+'a'*20000+'END')[-RESUME_HISTORY_CHARS:] and 'earlier 1 turns omitted' in other.resume_context(long)
    other.close()
    # The new session's first input is that snapshot, without connected() and without a greeting request.
    connected=[];monkeypatch.setattr(store,'connected',lambda identity:connected.append(identity))
    _,begin,context=await session(store,cid,{'service':store,'call_id':cid,'websocket':True,'generation':2,'resume':True})
    await begin(None,None)
    first=context.get_messages()[0]['content']
    assert first==store.resume_context(cid) and 'Introduce yourself' not in first and connected==[]
    # A context-build failure still sends the listen/no-greeting instruction and records it.
    def broken(identity):raise RuntimeError('snapshot failed')
    monkeypatch.setattr(store,'resume_context',broken)
    _,begin,context=await session(store,cid,{'service':store,'call_id':cid,'websocket':True,'generation':2,'resume':True})
    await begin(None,None)
    first=context.get_messages()[0]['content']
    assert 'stay silent until you hear speech' in first and 'do not greet again' in first and 'Introduce yourself' not in first
    assert any(e['kind']=='handover_context_failed' for e in store.get_call(cid)['events'])
    # Both live engines take that first input without answering it.
    import voice
    brief=store.get_call(cid)['brief']
    gemini=voice.create_model({**brief,'model':'gemini-3.8-live'},'fixture-not-a-key',silent=True)
    sent=[]
    class GeminiSession:
        async def send_client_content(self,**kwargs):sent.append(('content',kwargs))
        async def send_realtime_input(self,**kwargs):sent.append(('realtime',kwargs))
    gemini._session=GeminiSession();gemini._context=context
    await gemini._create_initial_response()
    assert [kind for kind,_ in sent]==['content'] and sent[0][1]['turn_complete'] is False
    assert 'CALL RESUMED' in json.dumps([str(t) for t in sent[0][1]['turns']])
    grok=voice.create_model({**brief,'dialog_engine':'grok_realtime'},'fixture-not-a-key',silent=True)
    events=[]
    async def send_client_event(event):events.append(event.type)
    async def session_update():events.append('session.update')
    grok.send_client_event=send_client_event;grok._send_session_update=session_update
    grok._context=context;grok._api_session_ready=True
    await grok._create_response()
    assert events==['conversation.item.create','session.update'],'history in, no response.create'
    store.close()


# spec: ANYM-451
async def test_out_of_band_record_status_and_cumulative_audio_verdict(tmp_path):
    from agent_api import AgentAPI, AgentRegistry
    from test_agent_api import registry
    store=VoiceService(tmp_path/'data')
    b=store.prepare(ExternalBrief(**BRIEF,channel='phone',account='twilio',destination='+12025550123'),[],owner_principal='alice')
    cid=store.start(b['id'])['id'];store.connected(cid);store.transcript(cid,'user',MENU)
    press(store,cid,'3','p1');press(store,cid,'3','p2')
    store.runtime_event(cid,{'kind':'media_session','generation':1,'started_at':'t1','ended_by':'handover'})
    store.handover_result(cid,'p2','accepted');store.activate_generation(cid)
    keypresses=[e for e in store.get_call(cid)['events'] if e['kind']=='keypress']
    assert [(e['keys'],e['dtmf_path'],e['generation']) for e in keypresses]==[('3','in_band',1),('3','out_of_band',1)]
    async def noop(identity):return store.get_call(identity)
    surface=AgentAPI(store,AgentRegistry(registry(tmp_path)[0]),noop,noop,None,None)
    status=await surface.call('alice','get_call_status',{'call_id':cid})
    assert status['last_keypress']['keys']=='3' and status['last_keypress']['dtmf_path']=='out_of_band' and 'handover_failure' not in status
    store.stop(cid,'keypress_handover_failed')
    status=await surface.call('alice','get_call_status',{'call_id':cid})
    assert status['handover_failure']==HANDOVER_FAILURE and status['end_reason']=='keypress_handover_failed'
    report=json.dumps(store.learning.report(cid),ensure_ascii=False)
    assert 'переподключался 1 раз' in report
    for event in store.get_call(cid)['events']:
        assert 'fixture-stream-token' not in json.dumps(event)
        if event['kind'] in {'keypress','keypress_handover','media_session'}:assert 'text' not in event
    # Call-level verdict across media sessions; enum meanings unchanged.
    def audio(active=0,frames=100,pcm=2000,zero=0):
        return {'input_active_ms':active,'input_frames':frames,'input_pcm_ms':pcm,'input_zero_frames':zero}
    def call(*parts):
        events=[]
        for g,a in enumerate(parts,1):
            if a is not None:events.append({'kind':'inbound_audio','audio':a,'generation':g})
            events.append({'kind':'media_session','generation':g,'ended_by':'handover' if g<len(parts) else 'end'})
        return events
    assert inbound_audio_verdict(call(audio(active=900),audio()),True)=={'heard':True,'reason':None}
    assert inbound_audio_verdict(call(audio(),audio(active=900)),True)=={'heard':True,'reason':None}
    assert inbound_audio_verdict(call(audio(pcm=6000,zero=100),audio(pcm=6000,zero=100)),True)=={'heard':False,'reason':'zero_frames'}
    assert inbound_audio_verdict(call(audio(pcm=6000),audio(pcm=6000)),True)=={'heard':False,'reason':'quiet_frames'}
    assert inbound_audio_verdict(call(audio(pcm=6000),None),True)=={'heard':None,'reason':'unknown'}
    assert inbound_audio_verdict(call(audio(frames=0,pcm=0),audio(frames=0,pcm=0)),True)=={'heard':False,'reason':'no_frames'}
    # A single-session call reads exactly as ANYM-426: its last counters.
    single=[{'kind':'inbound_audio','audio':audio(active=900)},{'kind':'inbound_audio','audio':audio(pcm=12000,zero=100)}]
    assert inbound_audio_verdict(single,True)=={'heard':False,'reason':'zero_frames'}
    assert inbound_audio_verdict(single+[{'kind':'media_session','generation':1,'ended_by':'end'}],True)=={'heard':False,'reason':'zero_frames'}
    assert inbound_audio_verdict([],True)=={'heard':None,'reason':'unknown'} and inbound_audio_verdict(single,False)=={'heard':None,'reason':None}
    store.close()
