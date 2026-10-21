"""ANYM-428: at an automated menu the call agent presses keys as in-band DTMF tones."""
import asyncio
import base64
import json
from types import SimpleNamespace

import numpy as np
import pytest
from starlette.websockets import WebSocketState

from core import FLOW_TOOL_MODELS, TOOL_MODELS, BriefInput, VoiceService
from outbound import ExternalBrief
from test_tasks import BRIEF

MENU='Layanan anti-scam XL Smart. Tekan 3 untuk melanjutkan, tekan 4 untuk berhenti.'


def active_call(store,channel='phone',account='twilio',destination='+628123456789'):
    if channel=='browser':
        brief=store.prepare(BriefInput(**BRIEF),[])
    else:
        brief=store.prepare(ExternalBrief(**BRIEF,channel=channel,account=account,destination=destination),[])
    call=store.start(brief['id']);store.connected(call['id'])
    store.transcript(call['id'],'user',MENU)
    return call['id']


def press(store,cid,keys,tool_call_id,quote=MENU,**extra):
    return store.tool(cid,'press_keys',{'keys':keys,'menu_quote':quote,**extra},tool_call_id)


# spec: ANYM-428
def test_press_keys_validates_bounds_and_runs_once(tmp_path):
    store=VoiceService(tmp_path)
    cid=active_call(store)
    ok=press(store,cid,'3','press-1')
    assert ok['ok'] is True and ok['keys']=='3' and ok['delivery']=='in_band_audio'
    assert ok['delivery_confirmed'] is False and ok['evidence_found'] is True
    assert 'silent' in ok['next_action'] and 'same menu' in ok['next_action']
    # A provider retry of the same tool call returns the stored result and records nothing new.
    assert press(store,cid,'3','press-1')==ok
    pressed=[e for e in store.get_call(cid)['events'] if e['kind']=='tool' and e['name']=='press_keys']
    assert len(pressed)==1 and pressed[0]['arguments']=={'keys':'3','menu_quote':MENU} and pressed[0]['result']==ok
    # Menu choices only: at most three keys of 0-9 * #. A code or number from the facts never fits.
    for bad in ['','1234','150000','104104','12a','3 ',' 3','3\n','w3','+1']:
        assert press(store,cid,bad,'bad-'+repr(bad))=={'error':'invalid_tool_arguments'},bad
    assert press(store,cid,'1','empty-quote',quote='')=={'error':'invalid_tool_arguments'}
    assert press(store,cid,'1','long-quote',quote='x'*501)=={'error':'invalid_tool_arguments'}
    assert press(store,cid,'1','extra',approved=True)=={'error':'invalid_tool_arguments'}
    unheard=press(store,cid,'*#','unheard',quote='Press star then pound')
    assert unheard['ok'] is True and unheard['keys']=='*#' and unheard['evidence_found'] is False
    # Twenty accepted presses per call; refused ones do not count. Alternating keys stay in-band (ANYM-451).
    for i in range(18):
        assert press(store,cid,'12'[i%2],'press-'+str(i+2))['ok'] is True
    assert press(store,cid,'1','press-21')=={'error':'keypress_limit'}
    store.stop(cid)
    assert press(store,cid,'1','after-stop')=={'error':'call_not_active'}
    store.close()


# spec: ANYM-428
@pytest.mark.parametrize('channel,account,destination,allowed',[
    ('phone','twilio','+628123456789',True),('sip','twilio','sip:desk@pbx.example.com',True),
    ('browser',None,None,True),('telegram','telegram','123456789',False),('whatsapp','twilio','+628123456789',False)])
def test_press_keys_runs_only_on_phone_sip_and_rehearsal(tmp_path,channel,account,destination,allowed):
    store=VoiceService(tmp_path)
    cid=active_call(store,channel,account,destination)
    result=press(store,cid,'3','press-1')
    assert (result.get('ok') is True) if allowed else result=={'error':'keypress_unsupported_channel'}
    store.close()


# spec: ANYM-428
def test_press_keys_is_refused_while_the_call_is_closing(tmp_path):
    store=VoiceService(tmp_path)
    cid=active_call(store)
    closing=store.tool(cid,'end_conversation',{'reason':'unavailable','summary':'Anti-scam menu; nothing pressed.',
                                               'evidence':MENU},'end-1')
    assert closing.get('end_requested') is True,closing
    assert press(store,cid,'3','press-1')=={'error':'conversation_ending'}
    store.close()


# spec: ANYM-428
def test_prompt_teaches_menu_rules_and_keypress_limits(tmp_path):
    store=VoiceService(tmp_path)
    for channel,account,destination in [('phone','twilio','+628123456789'),('sip','twilio','sip:desk@pbx.example.com'),(None,None,None)]:
        brief=store.prepare(ExternalBrief(**BRIEF,channel=channel,account=account,destination=destination) if channel else BriefInput(**BRIEF),[])
        prompt=' '.join(brief['prompt'].split())
        for rule in ['press_keys',
                     'A voice that addresses you is a person: greet at once',
                     'Only a recording that names keys or numbered options is a menu',
                     'At a menu, do not converse with it',
                     'stay silent and wait a few seconds',
                     'Never press an option that pays, subscribes, consents, confirms or cancels anything',
                     'Do not use request_owner_decision for a menu',
                     'Never key in a PIN, password, one-time code, card, account or reference number',
                     'the same menu returns after three presses',
                     'do not leave a message',
                     'A silent hold after a press can end the call after three minutes']:
            assert rule in prompt,(channel,rule)
    # Telegram and WhatsApp counterparties are people: no menu rules and no keypad in the tool list.
    for channel,account,destination in [('telegram','telegram','123456789'),('whatsapp','twilio','+628123456789')]:
        brief=store.prepare(ExternalBrief(**BRIEF,channel=channel,account=account,destination=destination),[])
        assert 'press_keys' not in brief['prompt'] and 'At a menu' not in brief['prompt']
    store.close()


# spec: ANYM-428
def test_press_keys_description_carries_hard_rules():
    import voice
    text=voice.DESCRIPTIONS['press_keys']
    for rule in ['automated menu','at most three keys','Never key in a PIN, password, one-time code, card, account or reference number',
                 'never choose an option that pays, subscribes, consents, confirms or cancels anything',
                 'Phone, SIP and browser rehearsal calls only','stay silent']:
        assert rule in text,rule
    schema=next(s for s in voice.tool_schemas(lambda *a:None).standard_tools if s.name=='press_keys')
    assert set(schema.properties)=={'keys','menu_quote'} and set(schema.required)=={'keys','menu_quote'}
    assert schema.properties['keys']['maxLength']==3
    assert 'press_keys' in TOOL_MODELS


# spec: ANYM-428
def test_flows_executor_has_no_press_keys(tmp_path):
    from live_flows import call_nodes
    assert 'press_keys' not in FLOW_TOOL_MODELS
    assert set(FLOW_TOOL_MODELS)=={'lookup_context','record_answer','request_owner_decision','consult_supervisor',
                                   'finish_task','end_conversation','propose_closing'}
    store=VoiceService(tmp_path)
    brief=store.prepare(BriefInput(**BRIEF,dialog_engine='gemini_live_flows'),[])
    assert 'press_keys' not in brief['prompt']
    for node in call_nodes(brief,None):
        assert 'press_keys' not in {f.name for f in node['functions']}
    call=store.start(brief['id']);store.connected(call['id'])
    assert press(store,call['id'],'3','press-1')=={'error':'tool_not_allowed'}
    store.close()


class Socket:
    """A connected Starlette socket that records what Pipecat sends to Twilio."""
    client_state=application_state=WebSocketState.CONNECTED

    def __init__(self):
        self.sent=[]

    async def send_text(self,data):
        self.sent.append(json.loads(data))

    async def send_bytes(self,data):
        raise AssertionError('Twilio media is JSON text')

    async def receive(self):
        await asyncio.Event().wait()

    async def close(self):
        pass

    def media(self):
        return [m for m in self.sent if m['event']=='media']

    def events(self):
        return [m['event'] for m in self.sent]


def twilio_pipeline():
    import voice
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineWorker
    from pipecat.serializers.twilio import TwilioFrameSerializer
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
    from pipecat.workers.runner import WorkerRunner
    socket=Socket()
    # The serializer and transport app.py builds for /provider/twilio/media.
    serializer=voice.KeypadTwilioSerializer('MZ'+'1'*32,params=TwilioFrameSerializer.InputParams(auto_hang_up=False))
    transport=FastAPIWebsocketTransport(websocket=socket,params=FastAPIWebsocketParams(
        audio_in_enabled=True,audio_out_enabled=True,audio_out_sample_rate=8000,audio_out_end_silence_secs=0,serializer=serializer))
    keypad=voice.keypad_for(transport,'phone')
    worker=PipelineWorker(Pipeline([transport.input(),transport.output()]),idle_timeout_secs=None)
    runner=WorkerRunner(handle_sigint=False)
    return socket,transport,keypad,worker,runner


def speech(ms):
    from pipecat.frames.frames import OutputAudioRawFrame
    samples=8*ms
    return OutputAudioRawFrame((np.sin(np.arange(samples)*2*np.pi*440/8000)*3000).astype(np.int16).tobytes(),8000,1)


async def until(condition,timeout=10):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def pcm(messages):
    """G.711 mu-law to linear PCM, decoded here so the check does not reuse the code under test."""
    u=~np.frombuffer(b''.join(base64.b64decode(m['media']['payload']) for m in messages),dtype=np.uint8)
    magnitude=((((u&0x0F).astype(np.int32)<<3)+0x84)<<((u>>4)&7))-0x84
    return np.where(u&0x80,-magnitude,magnitude)


def dtmf(audio,low,high):
    """The two strongest tones, one per DTMF band, each within 10 Hz of the key's pair."""
    spectrum=np.abs(np.fft.rfft(audio*np.hanning(len(audio))))
    freqs=np.fft.rfftfreq(len(audio),1/8000)
    found=[freqs[band][np.argmax(spectrum[band])] for band in ((freqs>600)&(freqs<1000),(freqs>1150)&(freqs<1700))]
    return abs(found[0]-low)<10 and abs(found[1]-high)<10


# spec: ANYM-428
async def test_press_keys_sends_paced_in_band_tones_that_survive_an_interruption_on_twilio():
    from pipecat.frames.frames import InterruptionFrame
    socket,transport,keypad,worker,runner=twilio_pipeline()
    started=asyncio.Event()
    @worker.event_handler('on_pipeline_started')
    async def start(worker,frame):started.set()
    await runner.add_workers(worker)
    task=asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(started.wait(),5)
        await keypad.preload()
        output=transport.output()
        await output.queue_frame(speech(400))                    # agent speech already queued: 10 chunks of 40 ms
        tones_ms=await keypad.press('3#')                         # two keys, each padded to whole chunks
        await output.queue_frame(speech(400))                    # later model audio
        assert tones_ms==1040 and keypad.playing()
        await until(lambda:len(socket.media())>=13)              # the menu talks over the first tone
        await worker.queue_frames([InterruptionFrame()])
        await until(lambda:not keypad.playing())
        await asyncio.sleep(0.3)
        media=socket.media()
        # No clear while tones play: every tone chunk is sent, then the later, interruptible speech is drained.
        assert 'clear' not in socket.events()
        assert len(media)==10+26,len(media)
        assert {len(base64.b64decode(m['media']['payload'])) for m in media}=={320}
        assert dtmf(pcm(media[10:17]),697,1477)           # "3"
        assert dtmf(pcm(media[23:30]),941,1477)           # "#"
        # After the tones an interruption clears Twilio's buffer as before.
        await output.queue_frame(speech(400))
        await until(lambda:len(socket.media())>36)
        await worker.queue_frames([InterruptionFrame()])
        await until(lambda:'clear' in socket.events())
    finally:
        await runner.cancel();await asyncio.wait_for(task,5)


# spec: ANYM-428
async def test_rejected_or_failing_keypress_sends_nothing_and_keeps_the_call(tmp_path,monkeypatch):
    import voice
    store=VoiceService(tmp_path)
    cid=active_call(store)
    socket,transport,keypad,worker,runner=twilio_pipeline()
    started=asyncio.Event()
    @worker.event_handler('on_pipeline_started')
    async def start(worker,frame):started.set()
    await runner.add_workers(worker)
    task=asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(started.wait(),5)
        # A rejected press never reaches the keypad.
        rejected=store.tool(cid,'press_keys',{'keys':'1234','menu_quote':MENU},'bad')
        assert await voice.deliver_keys(keypad,store,cid,rejected)==rejected
        # Tones that were never preloaded (a failed preload) and a failing queue both answer keypress_unavailable.
        accepted=press(store,cid,'3','press-1')
        assert await voice.deliver_keys(keypad,store,cid,accepted)=={'error':'keypress_unavailable'}
        await keypad.preload()
        async def broken(*args,**kwargs):raise RuntimeError('queue failed')
        monkeypatch.setattr(transport.output(),'queue_frame',broken)
        assert await voice.deliver_keys(keypad,store,cid,press(store,cid,'4','press-2'))=={'error':'keypress_unavailable'}
        monkeypatch.undo()
        assert await voice.deliver_keys(None,store,cid,press(store,cid,'4','press-3'))=={'error':'keypress_unavailable'}
        failed=[e for e in store.get_call(cid)['events'] if e['kind']=='keypress_failed']
        assert [e['keys'] for e in failed]==['3','4','4']
        assert not keypad.playing()
        await asyncio.sleep(0.2)
        assert socket.media()==[]
        # Later model audio still reaches the socket, and the call is still active.
        await transport.output().queue_frame(speech(400))
        await until(lambda:len(socket.media())==10)
        assert store.get_call(cid)['status']=='active'
    finally:
        await runner.cancel();await asyncio.wait_for(task,5)
        store.close()


# spec: ANYM-428
async def test_press_keys_handler_returns_the_result_and_presses_a_tool_call_once(tmp_path,monkeypatch):
    import voice
    store=VoiceService(tmp_path)
    cid=active_call(store)
    seen,pressed,order=[],[],[]
    class Keypad:
        async def preload(self):pass
        async def press(self,keys):
            pressed.append(keys);order.append('tones');return 520
        def playing(self):return False
    monkeypatch.setattr(voice,'keypad_for',lambda transport,channel:Keypad())
    real_schemas=voice.tool_schemas
    def schemas(handler):
        seen.append(handler);return real_schemas(handler)
    monkeypatch.setattr(voice,'tool_schemas',schemas)
    class RTVI:
        async def send_server_message(self,message):order.append('rtvi')
        def event_handler(self,name):return lambda fn:fn
    class Worker:
        rtvi=RTVI()
        def __init__(self,*args,**kwargs):pass
        def event_handler(self,name):return lambda fn:fn
        async def queue_frames(self,frames):pass
    class Runner:
        def __init__(self,**kwargs):pass
        async def add_workers(self,worker):pass
        async def run(self):pass
    monkeypatch.setattr(voice,'PipelineWorker',Worker)
    monkeypatch.setattr(voice,'WorkerRunner',Runner)
    socket,transport,*_=twilio_pipeline()
    state={'service':store,'call_id':cid,'websocket':True}
    await voice.run_transport(transport,store.get_call(cid)['brief'],'fixture-not-a-key',state)
    handle=seen[0]
    results=[]
    async def result(value):
        results.append(value);order.append('result')
    call=SimpleNamespace(arguments={'keys':'3','menu_quote':MENU},tool_call_id='press-1',result_callback=result)
    await handle('press_keys',call)
    assert pressed==['3'] and results[0]['ok'] is True and results[0]['tones_ms']==520
    assert order==['tones','result','rtvi'],'the model gets the result before the RTVI message'
    await handle('press_keys',call)   # a provider retry of the same call: same result, no second tone
    assert pressed==['3'] and results[1]['keys']=='3'
    store.close()
