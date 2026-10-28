"""Gemini native audio through Pipecat, with scoped tools and bounded supervision."""
import asyncio
import contextlib
import json
import time
from datetime import datetime, timezone

from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.audio.dtmf.utils import load_dtmf_audio
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import EndFrame, InputAudioRawFrame, InputTextRawFrame, LLMRunFrame, LLMTextFrame, TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame, OutputAudioRawFrame, BotStoppedSpeakingFrame, InterruptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frameworks.rtvi import RTVIProcessor
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.utils.file_resolver import FileResolver, FileResolverError, UrlReachability
from pipecat.workers.runner import WorkerRunner

from core import KEYPAD_CHANNELS, RESUME_RULES, TOOL_MODELS, call_clock
from audio_health import NoRuntimeAudioHealth, RuntimeAudioHealth
from audio_probe import PCMProbe, diagnostics
from call_errors import record_model_error


class AudioDeliveryProbe(FrameProcessor):
    """Counters only: observe completed Pipecat output writes without changing frames."""
    def __init__(self,health):
        super().__init__()
        self.health=health

    async def process_frame(self,frame,direction):
        await super().process_frame(frame,direction)
        if direction==FrameDirection.DOWNSTREAM:
            if isinstance(frame,(TTSStartedFrame,BotStoppedSpeakingFrame,InterruptionFrame)):
                self.health.stop_audio()
            elif isinstance(frame,OutputAudioRawFrame):
                self.health.audio(len(frame.audio),frame.sample_rate,frame.num_channels)
        await self.push_frame(frame,direction)


class InboundAudioProbe(FrameProcessor):
    """Counters only: observe inbound transport audio on every channel without changing frames."""
    def __init__(self,probe):
        super().__init__()
        self.probe,self.failures=probe,0

    async def process_frame(self,frame,direction):
        await super().process_frame(frame,direction)
        if direction==FrameDirection.DOWNSTREAM and isinstance(frame,InputAudioRawFrame):
            # A diagnostic failure must never drop the frame or turn into a pipeline (model) error.
            try:self.probe.input(frame.audio,rate=frame.sample_rate,channels=frame.num_channels)
            except Exception:self.failures+=1
        await self.push_frame(frame,direction)


FILES_REFUSED='Files are not accepted in calls.'


class CallRTVI(RTVIProcessor):
    """Calls take no client files (ANYM-617): RTVI send-file (Pipecat 1.13+) is refused before it becomes a frame."""
    async def _handle_message(self,message,user_id=None):
        if message.type=='send-file':
            return await self._send_error_response(message.id,FILES_REFUSED)
        await super()._handle_message(message,user_id)


class NoFileResolver(FileResolver):
    """No file URL in a call's context is fetched by this server or handed to the provider (ANYM-617)."""
    def cached_reachability(self,url):
        return UrlReachability.BLOCKED

    async def classify(self,url):
        return UrlReachability.BLOCKED

    async def fetch(self,url):
        raise FileResolverError('Files are not accepted in calls')


def call_worker(processors):
    return PipelineWorker(Pipeline(processors),
        params=PipelineParams(enable_metrics=True,enable_usage_metrics=True),idle_timeout_secs=180,
        processor_unusable_policy=ProcessorUnusablePolicy.END,rtvi_processor=CallRTVI())


DESCRIPTIONS={
    'lookup_context':'Read only selected task facts. Empty query lists all facts. Sources may be partial or outdated.',
    'record_answer':'Save or correct one requested result field with an exact human quote. Marked approval fields automatically request an owner decision. Inspect owner_decision: corrected conditions invalidate previous answers.',
    'request_owner_decision':'Save an asynchronous owner question. Reuses a pending round; after an answer starts a NEW round. Do not hold the counterparty waiting. Pending is never approval. Only the owner channel can respond.',
    'consult_supervisor':'Ask a separate contextual adviser one concrete question. Waits at most six seconds. Guidance has no approval authority; timeout or obsolete advice means clarify or escalate.',
    'finish_task':'Summarize collected information. Returns missing fields or pending decisions. In social mode this saves notes and does not imply the conversation is finished. Does not execute any external action.',
    'press_keys':'Press phone keypad keys to choose an option in an automated menu: in-band tones first; pressing the same key again lets the server '
                 'resend it through the phone provider, which briefly reconnects the call. Phone, SIP and browser rehearsal calls only. '
                 'A menu choice of at most three keys; quote the menu prompt you heard. Never key in a PIN, password, one-time code, card, account or reference number; '
                 'never choose an option that pays, subscribes, consents, confirms or cancels anything. After pressing, stay silent and wait for the menu to react.',
    'end_conversation':'After a short spoken goodbye, end as completed, needs_owner, refused or unavailable. In social mode completed requires the latest explicit human goodbye quoted in evidence; collected notes are not a reason to stop. Criticism of audio or voice is not a refusal. needs_owner preserves a pending owner question for later; do not wait on the line. For refusal/unavailability quote the human exactly. No automatic callback is scheduled.',
}


class KeypadTones:
    """In-band DTMF for press_keys (ANYM-428): Pipecat's tone audio queued as uninterruptible output audio,
    chunked and paced by the transport like speech. Tone bytes count down as the transport writes them,
    so the Twilio serializer can hold back a `clear` that would cut a tone in two."""
    def __init__(self,output):
        self.output,self.tones,self.pending,self.deadline=output,None,0,0.0
        write=output.write_audio_frame
        async def counted(frame):
            if self.pending and not frame.interruptible:
                self.pending=max(0,self.pending-len(frame.audio))
            return await write(frame)
        output.write_audio_frame=counted

    async def preload(self):
        rate,chunk=self.output.sample_rate,self.output.audio_chunk_size
        tones={}
        for key in '0123456789*#':
            audio=await load_dtmf_audio(KeypadEntry(key),sample_rate=rate)
            tones[key]=audio+bytes(-len(audio)%chunk)  # whole chunks: no tone tail waits in the sender
        self.rate,self.chunk,self.tones=rate,chunk,tones

    async def press(self,keys):
        if not self.tones:raise RuntimeError('keypad_not_ready')
        audio=b''.join(self.tones[key] for key in keys)
        frame=OutputAudioRawFrame(audio=audio,sample_rate=self.rate,num_channels=1)
        frame.interruptible=False
        seconds=len(audio)/(2*self.rate)
        self.pending+=len(audio)
        # A stalled sender must not hold back interruptions for long.
        self.deadline=max(self.deadline,time.monotonic()+seconds+30)
        try:await self.output.queue_frame(frame)
        except BaseException:
            self.pending=max(0,self.pending-len(audio));raise
        return round(seconds*1000)

    def playing(self):
        # A remainder under one chunk was mixed with speech or dropped with it; it holds nothing back.
        return bool(self.tones) and self.pending>=self.chunk and time.monotonic()<self.deadline


class KeypadTwilioSerializer(TwilioFrameSerializer):
    """Twilio media: no `clear` while keypad tones are queued or in flight. Audio ahead of the tones is
    already sent then, and Pipecat still drains the interruptible speech queued after them."""
    keypad=None
    # ANYM-451: False once a newer media session of the call was admitted; this stream then sends nothing more.
    authority=None

    async def serialize(self,frame):
        if self.authority is not None and not self.authority():return None
        if isinstance(frame,InterruptionFrame) and self.keypad is not None:
            holding=False
            with contextlib.suppress(Exception):holding=self.keypad.playing()
            if holding:return None
        return await super().serialize(frame)


def keypad_for(transport,channel):
    """Keypad tones for the channels that may press keys, bound to a Twilio serializer; None elsewhere."""
    if channel not in KEYPAD_CHANNELS:
        return None
    output=transport.output()
    keypad=KeypadTones(output)
    serializer=getattr(getattr(output,'_params',None),'serializer',None)
    if isinstance(serializer,KeypadTwilioSerializer):
        serializer.keypad=keypad
    return keypad


async def deliver_keys(keypad,service,identity,result):
    """Queue the tones of an accepted press. A failure answers keypress_unavailable and never ends the call."""
    if not result.get('ok'):
        return result
    try:
        if keypad is None:raise RuntimeError('keypad_unavailable')
        return {**result,'tones_ms':await keypad.press(result['keys'])}
    except Exception:
        with contextlib.suppress(Exception):
            service.runtime_event(identity,{'kind':'keypress_failed','keys':result['keys']})
        return {'error':'keypress_unavailable'}


async def send_out_of_band(state,service,identity,result,tool_call_id):
    """One provider update for an out-of-band press (ANYM-451). Shielded: an interruption that cancels the tool call
    never leaves the update's outcome unrecorded. A failure is ambiguous, never retried."""
    async def update():
        try:outcome=await state['out_of_band'](result['keys']) if state.get('out_of_band') else 'rejected'
        except Exception:outcome='ambiguous'
        return service.handover_result(identity,tool_call_id,outcome)
    return await asyncio.shield(asyncio.ensure_future(update()))


# How long a press's result may take to reach the model before its out-of-band update starts anyway.
RESULT_SECS=2
RESUME_FALLBACK='CALL RESUMED (server message, not speech). The phone provider reconnected this same call after a key press; '+RESUME_RULES


def resume_message(service,identity):
    try:return service.resume_context(identity)
    except Exception:
        with contextlib.suppress(Exception):service.runtime_event(identity,{'kind':'handover_context_failed'})
        return RESUME_FALLBACK


def tool_schemas(handler):
    schemas=[]
    for name,model in TOOL_MODELS.items():
        schema=model.model_json_schema()
        async def invoke(params, tool_name=name):
            await handler(tool_name,params)
        schemas.append(FunctionSchema(name=name,description=DESCRIPTIONS[name],properties=schema['properties'],
            required=schema.get('required',[]),handler=invoke))
    return ToolsSchema(standard_tools=schemas)


class AuditedGemini(GeminiLiveLLMService):
    """Pinned Pipecat 1.14 hooks: capture provider text before sentence/turn buffering."""
    def __init__(self,*args,recorder=None,external_tts=False,live_flows=False,transcription_language=None,hint_failed=None,**kwargs):
        self.recorder=recorder
        self.hint_failed=hint_failed
        self.external_tts=external_tts
        self.live_flows=live_flows
        self.transcription_language=transcription_language
        from audio_health import ProviderAudioHealth
        self.audio_health=ProviderAudioHealth()
        super().__init__(*args,**kwargs)

    async def _connection_task_handler(self,config):
        # Pinned Pipecat 1.14 builds empty transcription configs. Add the
        # brief's language hint at every connection, including resume.
        if self.transcription_language:
            try:
                from google.genai.types import AudioTranscriptionConfig
                hinted={}
                for field in ('input_audio_transcription','output_audio_transcription'):
                    transcription=getattr(config,field) or AudioTranscriptionConfig()
                    hinted[field]=transcription.model_copy(update={'language_codes':[self.transcription_language]})
                for field,value in hinted.items():setattr(config,field,value)
            except Exception:
                # A local failure connects with the existing config (ANYM-450); best effort to record it.
                with contextlib.suppress(Exception):self.hint_failed()
        return await super()._connection_task_handler(config)

    async def push_frame(self,frame,direction=FrameDirection.DOWNSTREAM):
        if direction==FrameDirection.DOWNSTREAM:
            if isinstance(frame,TTSStartedFrame):
                self.audio_health.start()
            elif isinstance(frame,TTSStoppedFrame):
                self.audio_health.stop()
            elif isinstance(frame,TTSAudioRawFrame):
                self.audio_health.chunk(len(frame.audio),frame.sample_rate,frame.num_channels)
        if self.external_tts and direction==FrameDirection.DOWNSTREAM and isinstance(
                frame,(TTSAudioRawFrame,TTSStartedFrame,TTSStoppedFrame)):
            return  # Never leak the native prebuilt voice into a replicated-voice call.
        await super().push_frame(frame,direction)

    async def _push_output_transcription_text_frames(self,text):
        if not self.external_tts:
            return await super()._push_output_transcription_text_frames(text)
        frame=LLMTextFrame(text)
        frame.includes_inter_frame_spaces=True
        await self.push_frame(frame)

    async def _handle_msg_input_transcription(self,message):
        content=message.server_content
        if self.recorder and content and content.input_transcription:
            self.recorder('user',content.input_transcription.text)
        await super()._handle_msg_input_transcription(message)

    async def _handle_msg_output_transcription(self,message):
        content=message.server_content
        if self.recorder and content and content.output_transcription:
            self.recorder('assistant',content.output_transcription.text)
        await super()._handle_msg_output_transcription(message)

    async def _tool_result(self,tool_call_id,tool_name,tool_result_message,*,is_final=True,spoken=True):
        if not self.live_flows or not self._supports_non_blocking_tools or not self._function_is_async(tool_name):
            return await super()._tool_result(tool_call_id,tool_name,tool_result_message,is_final=is_final,spoken=spoken)
        if self._disconnecting or not self._session:
            return
        from google.genai.types import FunctionResponse
        scheduling=tool_result_message.get('scheduling','WHEN_IDLE')
        if scheduling not in {'SILENT','WHEN_IDLE'}:
            scheduling='WHEN_IDLE'
        # API scheduling is outside the tool's JSON result. A result-only hint
        # leaves the wire response at WHEN_IDLE and can trigger spoken filler.
        # Scheduling stays the result's own hint, not `spoken`: Flows results keep WHEN_IDLE.
        # will_continue=False closes the call, as upstream 1.14 does for a final result.
        response=FunctionResponse(name=tool_name,id=tool_call_id,scheduling=scheduling,will_continue=not is_final,
            response={**tool_result_message,'scheduling':scheduling})
        try:
            await self._session.send_tool_response(function_responses=response)
        except Exception as error:
            await self._handle_send_error(error)


def input_sample_rate(transport):
    # The rate of the frames this transport delivers: its own setting, else the pipeline's (it resamples to it).
    return transport.input()._params.audio_in_sample_rate or PipelineParams().audio_in_sample_rate


TRANSCRIPTION_LANGUAGES={'ru':'ru-RU','en':'en-US','id':'id-ID'}


def create_model(brief,key,recorder=None,input_rate=None,at=None,silent=False):
    # The local time is read when the call starts; the brief's prompt and snapshot stay unchanged.
    # silent: a session resumed after a handover takes its first context without answering it (ANYM-451).
    brief={**brief,'prompt':brief['prompt']+'\n'+call_clock(brief,at or datetime.now(timezone.utc))}
    if brief.get('dialog_engine')=='grok_realtime':
        from grok_voice import AuditedGrok
        return AuditedGrok(brief,key,recorder,input_rate=input_rate or PipelineParams().audio_in_sample_rate,silent_start=silent,
            file_resolver=NoFileResolver())
    return AuditedGemini(api_key=key,recorder=recorder,external_tts=bool(brief.get('custom_voice_id')),
        inference_on_context_initialization=not silent,file_resolver=NoFileResolver(),
        live_flows=brief.get('dialog_engine')=='gemini_live_flows',settings=GeminiLiveLLMService.Settings(
        model=brief['model'],system_instruction=brief['prompt'],voice=brief['voice']),
        # Every Gemini engine gets the brief's language as a transcription hint; a legacy language gets none.
        transcription_language=TRANSCRIPTION_LANGUAGES.get(brief.get('language')))


def record_clock(service,identity,brief,llm):
    # Best effort, like the audio diagnostics: the call keeps the exact clock line its model received.
    try:
        sent=llm._settings.system_instruction
        if sent.startswith(brief['prompt']+'\n'):
            service.runtime_event(identity,{'kind':'call_clock','text':sent[len(brief['prompt'])+1:]})
    except Exception:
        pass


async def send_server_note(worker,text):
    frame=InputTextRawFrame(text=text)
    # Gemini forwards this frame; the assistant aggregator otherwise treats it as spoken text.
    frame.append_to_context=False
    await worker.queue_frames([frame])


async def send_owner_update(worker,decision):
    # InputTextRawFrame reaches Gemini's realtime input. Updating only LLMContext
    # would leave this message local: the Live service manages remote history.
    text='OWNER DECISION FROM LOCAL INTERFACE: '+json.dumps({
        'decision_id':decision['id'],'round':decision.get('round'),'conditions':decision.get('conditions',{}),
        'question':decision['question'],'answer':decision['answer']},ensure_ascii=False)
    await send_server_note(worker,text)


async def report_error(state,rtvi,frame=None):
    await rtvi.send_server_message(record_model_error(state,frame))


def webrtc_transport(connection):
    return SmallWebRTCTransport(connection,TransportParams(audio_in_enabled=True,audio_out_enabled=True))


async def run_voice(connection,brief,key,state):
    await run_transport(webrtc_transport(connection),brief,key,state,browser=True)


async def run_transport(transport,brief,key,state,browser=False):
    service,identity=state['service'],state['call_id']
    # A phone call's media session number (ANYM-451): only the newest one may act on the call.
    generation,resume=state.get('generation'),bool(state.get('resume'))
    context=LLMContext()
    llm=create_model(brief,key,recorder=lambda role,text:service.transcript_chunk(identity,role,text,generation=generation),
                     input_rate=input_sample_rate(transport),**({'silent':True} if resume else {}))
    with contextlib.suppress(Exception):
        llm.hint_failed=lambda:service.runtime_event(identity,{'kind':'language_hint_failed'})
        # Grok builds its hint in the constructor; Gemini reports through hint_failed on connection.
        if getattr(llm,'hint_unavailable',False):llm.hint_failed()
    record_clock(service,identity,brief,llm)
    live_flows=brief.get('dialog_engine')=='gemini_live_flows'
    ending_tasks=[]
    # Diagnostics are best effort: a constructor failure must never stop the call before the runner starts.
    try:runtime_health=RuntimeAudioHealth()
    except Exception:runtime_health=NoRuntimeAudioHealth()
    inbound_audio=diagnostics(PCMProbe)
    keypad=None
    if not live_flows:
        # Best effort too: without a keypad a press answers keypress_unavailable; the call goes on.
        with contextlib.suppress(Exception):keypad=keypad_for(transport,brief.get('channel','browser'))

    async def handle(name,params):
        if name=='consult_supervisor' and state.get('supervisor'):
            if not service.authorized(identity,generation,name):
                result={'error':'stale_generation'}
            else:
                result=await state['supervisor'].consult(identity,dict(params.arguments),params.tool_call_id,generation=generation)
        else:
            result=service.tool(identity,name,dict(params.arguments),params.tool_call_id,generation=generation)
        if name=='press_keys':
            # Delivery once per call and tool call, whichever session asks (ANYM-451). The model gets its result
            # before anything else is awaited, so an interruption from the still-talking menu cannot cancel the
            # call without a result.
            path=service.claim_delivery(identity,params.tool_call_id,generation) if result.get('ok') else None
            if path=='out_of_band':
                # The stay-silent result goes first: a successful update may retire this session before Twilio
                # answers. Only a definite rejection keeps the session, and then it is told plainly. Result and
                # update run as one task: an interruption or a failed callback never strands the claimed press.
                async def notify_then_update(stay_silent):
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(params.result_callback(stay_silent),RESULT_SECS)
                    return await send_out_of_band(state,service,identity,stay_silent,params.tool_call_id)
                result=await asyncio.shield(asyncio.ensure_future(notify_then_update(result)))
                if result.get('delivery')=='out_of_band_rejected' and service.authorized(identity,generation,name):
                    await send_server_note(worker,'KEY NOT SENT (server message, not speech): '+json.dumps(result,ensure_ascii=False))
                await worker.rtvi.send_server_message({'kind':'tool','name':name,'result':result})
                return
            elif path:
                result=service.press_delivered(identity,params.tool_call_id,await deliver_keys(keypad,service,identity,result))
            await params.result_callback(result)
            await worker.rtvi.send_server_message({'kind':'tool','name':name,'result':result})
            return
        await worker.rtvi.send_server_message({'kind':'tool','name':name,'result':result})
        await params.result_callback(result)
        if result.get('end_requested'):
            state['end_reason']={'completed':'task_completed','needs_owner':'awaiting_owner'}.get(
                result['reason'],'counterparty_'+result['reason'])
            await worker.queue_frames([EndFrame()])

    if not live_flows:
        context.set_tools(tool_schemas(handle))
    aggregators=LLMContextAggregatorPair(context)
    user,assistant=aggregators
    processors=[transport.input(),InboundAudioProbe(inbound_audio)]
    if live_flows:
        from live_flows import SpeechSafety
        async def activity(speaking):
            revoked=service.audio_activity(identity,speaking)
            if revoked:
                await flow.set_node_from_config(flow.nodes['discussion'])
                await worker.rtvi.send_server_message({'kind':'flow','flow':flow.snapshot(),'closing_revoked':True})
        safety=SpeechSafety(activity)
        processors.append(safety)
    processors.extend([user,llm])
    if brief.get('custom_voice_id'):
        from custom_tts import ReplicatedVoiceTTS
        state['tts_metrics']=[]
        processors.append(ReplicatedVoiceTTS(api_key=key,voice_id=brief['custom_voice_id'],
            style=brief.get('voice_style','neutral'),metric_callback=state['tts_metrics'].append))
    processors.extend([transport.output(),AudioDeliveryProbe(runtime_health),assistant])
    worker=call_worker(processors)
    runner=WorkerRunner(handle_sigint=False)
    state['runner']=runner
    await runner.add_workers(worker)
    if live_flows:
        from live_flows import GeminiLiveFlowManager, call_nodes, guarded_end
        async def phase(snapshot):
            service.runtime_event(identity,{'kind':'flow','flow':snapshot})
            await worker.rtvi.send_server_message({'kind':'flow','flow':snapshot})
        async def dispatch(name,args,tool_call_id):
            if name=='consult_supervisor' and state.get('supervisor'):
                result=await state['supervisor'].consult(identity,args,tool_call_id,generation=generation)
            else:
                result=service.tool(identity,name,args,tool_call_id)
            await worker.rtvi.send_server_message({'kind':'tool','name':name,'result':result})
            if result.get('end_requested'):
                ending_tasks.append(asyncio.create_task(guarded_end(service,identity,worker,llm,safety,state)))
            return dict(result)
        nodes=call_nodes(brief,dispatch)
        flow=GeminiLiveFlowManager(worker=worker,llm=llm,context_aggregator=aggregators,
            nodes=nodes,event_sink=phase,transport=transport)
        await flow.prepare_tools(context)
        state['flow']=flow

    async def owner_update(decision):
        if service.authorized(identity,generation,'owner_answer'):
            await send_owner_update(worker,decision)

    state['owner_update']=owner_update

    @worker.event_handler('on_pipeline_error')
    async def pipeline_error(worker,frame):
        # Provider payloads may contain secrets. Never relay frame.error to the UI.
        await report_error(state,worker.rtvi,frame)

    async def begin():
        if not resume:service.connected(identity)
        # Telegram start timing (ANYM-450): the durable call is active from here; best effort.
        if 'start_timing' in state:
            with contextlib.suppress(Exception):state['start_timing'].mark('call_active')
        await worker.rtvi.send_server_message({'kind':'session','call_id':identity,'model':brief['model'],
            'voice':brief['grok_voice'] if brief.get('dialog_engine')=='grok_realtime' else brief.get('custom_voice_id') or brief['voice'],
            'voice_style':brief.get('voice_style','neutral'),
            'dialog_engine':brief.get('dialog_engine','gemini_live')})
        if live_flows:
            await flow.initialize(flow.nodes['discussion'])
        context.add_message({'role':'user','content':resume_message(service,identity) if resume else
                            ('Begin the browser rehearsal now. The human plays the counterparty. ' if browser else
                            'The counterparty has answered this real call. ')+'Introduce yourself and ask the first relevant question.'})
        await worker.queue_frames([LLMRunFrame()])
        if keypad:
            # Loaded once per call, so a press awaits no file I/O before its result.
            with contextlib.suppress(Exception):await keypad.preload()

    if browser:
        @worker.rtvi.event_handler('on_client_ready')
        async def ready(rtvi): await begin()
    else:
        @worker.event_handler('on_pipeline_started')
        async def pipeline_started(worker,frame): await begin()

    if browser or state.get('websocket'):
        @transport.event_handler('on_client_disconnected')
        async def disconnected(transport,client): await runner.cancel()

    @user.event_handler('on_user_turn_message_added')
    async def user_message(aggregator,message):
        service.transcript(identity,'user',message.content,generation=generation)
        await worker.rtvi.send_server_message({'kind':'transcript','role':'user','text':message.content})

    @assistant.event_handler('on_assistant_turn_stopped')
    async def assistant_message(aggregator,message):
        service.transcript(identity,'assistant',message.content,message.interrupted,generation=generation)
        await worker.rtvi.send_server_message({'kind':'transcript','role':'assistant','text':message.content,'interrupted':message.interrupted})

    health_task=asyncio.create_task(runtime_health.monitor(),name='voice-audio-health-'+identity)
    try:
        await runner.run()
    finally:
        health_task.cancel()
        await asyncio.gather(health_task,return_exceptions=True)
        # Diagnostics are best effort: a failure here must not skip cleanup or change the end reason.
        with contextlib.suppress(Exception):
            service.runtime_event(identity,{'kind':'provider_audio_health','audio':llm.audio_health.snapshot(),
                                            'runtime':runtime_health.snapshot()})
        with contextlib.suppress(Exception):
            service.runtime_event(identity,{'kind':'inbound_audio','audio':inbound_audio.snapshot(),
                                            **({'generation':generation} if generation is not None else {})})
        if generation is not None:
            with contextlib.suppress(Exception):
                service.runtime_event(identity,{'kind':'media_session','generation':generation,'started_at':state.get('started_at'),
                                                'ended_by':service.media_ended_by(identity,generation)})
        for task in ending_tasks:
            task.cancel()
        if ending_tasks:
            await asyncio.gather(*ending_tasks,return_exceptions=True)
        if 'tts_metrics' in state:
            # Best effort too: a failure here must not mask the runner's own exception.
            with contextlib.suppress(Exception):
                value=service.get_call(identity)
                service.result(identity,metrics={**value['metrics'],'replicated_tts':state['tts_metrics']})
