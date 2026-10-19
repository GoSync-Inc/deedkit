"""Gemini native audio through Pipecat, with scoped tools and bounded supervision."""
import asyncio
import contextlib
import json

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import EndFrame, InputAudioRawFrame, InputTextRawFrame, LLMRunFrame, LLMTextFrame, TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame, OutputAudioRawFrame, BotStoppedSpeakingFrame, InterruptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

from core import TOOL_MODELS
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


DESCRIPTIONS={
    'lookup_context':'Read only selected task facts. Empty query lists all facts. Sources may be partial or outdated.',
    'record_answer':'Save or correct one requested result field with an exact human quote. Marked approval fields automatically request an owner decision. Inspect owner_decision: corrected conditions invalidate previous answers.',
    'request_owner_decision':'Save an asynchronous owner question. Reuses a pending round; after an answer starts a NEW round. Do not hold the counterparty waiting. Pending is never approval. Only the owner channel can respond.',
    'consult_supervisor':'Ask a separate contextual adviser one concrete question. Waits at most six seconds. Guidance has no approval authority; timeout or obsolete advice means clarify or escalate.',
    'finish_task':'Summarize collected information. Returns missing fields or pending decisions. In social mode this saves notes and does not imply the conversation is finished. Does not execute any external action.',
    'end_conversation':'After a short spoken goodbye, end as completed, needs_owner, refused or unavailable. In social mode completed requires the latest explicit human goodbye quoted in evidence; collected notes are not a reason to stop. Criticism of audio or voice is not a refusal. needs_owner preserves a pending owner question for later; do not wait on the line. For refusal/unavailability quote the human exactly. No automatic callback is scheduled.',
}


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
    """Pinned Pipecat 1.12 hooks: capture provider text before sentence/turn buffering."""
    def __init__(self,*args,recorder=None,external_tts=False,live_flows=False,transcription_language=None,**kwargs):
        self.recorder=recorder
        self.external_tts=external_tts
        self.live_flows=live_flows
        self.transcription_language=transcription_language
        from audio_health import ProviderAudioHealth
        self.audio_health=ProviderAudioHealth()
        super().__init__(*args,**kwargs)

    async def _connection_task_handler(self,config):
        # Pinned Pipecat 1.12 builds empty transcription configs. Add the
        # rehearsal's language hint at every connection, including resume.
        if self.transcription_language:
            from google.genai.types import AudioTranscriptionConfig
            for field in ('input_audio_transcription','output_audio_transcription'):
                transcription=getattr(config,field) or AudioTranscriptionConfig()
                setattr(config,field,transcription.model_copy(update={
                    'language_codes':[self.transcription_language]}))
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

    async def _tool_result(self,tool_call_id,tool_name,tool_result_message):
        if not self.live_flows or not self._supports_non_blocking_tools or not self._function_is_async(tool_name):
            return await super()._tool_result(tool_call_id,tool_name,tool_result_message)
        if self._disconnecting or not self._session:
            return
        from google.genai.types import FunctionResponse
        scheduling=tool_result_message.get('scheduling','WHEN_IDLE')
        if scheduling not in {'SILENT','WHEN_IDLE'}:
            scheduling='WHEN_IDLE'
        # API scheduling is outside the tool's JSON result. A result-only hint
        # leaves the wire response at WHEN_IDLE and can trigger spoken filler.
        response=FunctionResponse(name=tool_name,id=tool_call_id,scheduling=scheduling,
            response={**tool_result_message,'scheduling':scheduling})
        try:
            await self._session.send_tool_response(function_responses=response)
        except Exception as error:
            await self._handle_send_error(error)


def input_sample_rate(transport):
    # The rate of the frames this transport delivers: its own setting, else the pipeline's (it resamples to it).
    return transport.input()._params.audio_in_sample_rate or PipelineParams().audio_in_sample_rate


def create_model(brief,key,recorder=None,input_rate=None):
    if brief.get('dialog_engine')=='grok_realtime':
        from grok_voice import AuditedGrok
        return AuditedGrok(brief,key,recorder,input_rate=input_rate or PipelineParams().audio_in_sample_rate)
    return AuditedGemini(api_key=key,recorder=recorder,external_tts=bool(brief.get('custom_voice_id')),
        live_flows=brief.get('dialog_engine')=='gemini_live_flows',settings=GeminiLiveLLMService.Settings(
        model=brief['model'],system_instruction=brief['prompt'],voice=brief['voice']),
        transcription_language={'ru':'ru-RU','en':'en-US','id':'id-ID'}[brief['language']]
            if brief.get('dialog_engine')=='gemini_live_flows' else None)


async def send_owner_update(worker,decision):
    # InputTextRawFrame reaches Gemini's realtime input. Updating only LLMContext
    # would leave this message local: the Live service manages remote history.
    text='OWNER DECISION FROM LOCAL INTERFACE: '+json.dumps({
        'decision_id':decision['id'],'round':decision.get('round'),'conditions':decision.get('conditions',{}),
        'question':decision['question'],'answer':decision['answer']},ensure_ascii=False)
    frame=InputTextRawFrame(text=text)
    # Gemini forwards this frame; the assistant aggregator otherwise treats it as spoken text.
    frame.append_to_context=False
    await worker.queue_frames([frame])


async def report_error(state,rtvi,frame=None):
    await rtvi.send_server_message(record_model_error(state,frame))


def webrtc_transport(connection):
    return SmallWebRTCTransport(connection,TransportParams(audio_in_enabled=True,audio_out_enabled=True))


async def run_voice(connection,brief,key,state):
    await run_transport(webrtc_transport(connection),brief,key,state,browser=True)


async def run_transport(transport,brief,key,state,browser=False):
    service,identity=state['service'],state['call_id']
    context=LLMContext()
    llm=create_model(brief,key,recorder=lambda role,text:service.transcript_chunk(identity,role,text),
                     input_rate=input_sample_rate(transport))
    live_flows=brief.get('dialog_engine')=='gemini_live_flows'
    ending_tasks=[]
    # Diagnostics are best effort: a constructor failure must never stop the call before the runner starts.
    try:runtime_health=RuntimeAudioHealth()
    except Exception:runtime_health=NoRuntimeAudioHealth()
    inbound_audio=diagnostics(PCMProbe)

    async def handle(name,params):
        if name=='consult_supervisor' and state.get('supervisor'):
            result=await state['supervisor'].consult(identity,dict(params.arguments),params.tool_call_id)
        else:
            result=service.tool(identity,name,dict(params.arguments),params.tool_call_id)
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
    worker=PipelineWorker(Pipeline(processors),
        params=PipelineParams(enable_metrics=True,enable_usage_metrics=True),idle_timeout_secs=180,
        processor_unusable_policy=ProcessorUnusablePolicy.END)
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
                result=await state['supervisor'].consult(identity,args,tool_call_id)
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
        await send_owner_update(worker,decision)

    state['owner_update']=owner_update

    @worker.event_handler('on_pipeline_error')
    async def pipeline_error(worker,frame):
        # Provider payloads may contain secrets. Never relay frame.error to the UI.
        await report_error(state,worker.rtvi,frame)

    async def begin():
        service.connected(identity)
        await worker.rtvi.send_server_message({'kind':'session','call_id':identity,'model':brief['model'],
            'voice':brief['grok_voice'] if brief.get('dialog_engine')=='grok_realtime' else brief.get('custom_voice_id') or brief['voice'],
            'voice_style':brief.get('voice_style','neutral'),
            'dialog_engine':brief.get('dialog_engine','gemini_live')})
        if live_flows:
            await flow.initialize(flow.nodes['discussion'])
        context.add_message({'role':'user','content':('Begin the browser rehearsal now. The human plays the counterparty. ' if browser else
                            'The counterparty has answered this real call. ')+'Introduce yourself and ask the first relevant question.'})
        await worker.queue_frames([LLMRunFrame()])

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
        service.transcript(identity,'user',message.content)
        await worker.rtvi.send_server_message({'kind':'transcript','role':'user','text':message.content})

    @assistant.event_handler('on_assistant_turn_stopped')
    async def assistant_message(aggregator,message):
        service.transcript(identity,'assistant',message.content,message.interrupted)
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
            service.runtime_event(identity,{'kind':'inbound_audio','audio':inbound_audio.snapshot()})
        for task in ending_tasks:
            task.cancel()
        if ending_tasks:
            await asyncio.gather(*ending_tasks,return_exceptions=True)
        if 'tts_metrics' in state:
            # Best effort too: a failure here must not mask the runner's own exception.
            with contextlib.suppress(Exception):
                value=service.get_call(identity)
                service.result(identity,metrics={**value['metrics'],'replicated_tts':state['tts_metrics']})
