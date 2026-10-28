"""Pipecat 1.14 Flows executor for persistent Gemini Live sessions.

This deliberately supports APPEND, fixed tools and tool-driven transitions.
It does not pretend a local context reset changes Gemini's remote history.
"""
import asyncio
import math
import time
from array import array

from pipecat.adapters.schemas.direct_function import BaseDirectFunctionWrapper
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.flows import FlowManager, FlowsFunctionSchema
from pipecat.flows.types import ContextStrategy, NO_RESPONSE
from pipecat.frames.frames import (BotStartedSpeakingFrame, BotStoppedSpeakingFrame,
                                   EndFrame, InputAudioRawFrame)
from pipecat.processors.frame_processor import FrameProcessor


class GeminiLiveFlowManager(FlowManager):
    """Reuse upstream validation, node rendering, state and action handling.

    A transition is committed BEFORE its FunctionResponse. No runtime settings,
    tool replacement, context rewriting or LLMRunFrame is sent to Gemini.
    """
    def __init__(self, *, nodes, event_sink=None, **kwargs):
        super().__init__(**kwargs)
        self.nodes = {n['name']: n for n in nodes}
        if len(self.nodes) != len(nodes):
            raise ValueError('Live nodes require unique names')
        for node in nodes:
            self._validate_live_node(node)
        self.event_sink = event_sink
        self.instructions = ''
        self.revision = 0
        self._tool_lock = asyncio.Lock()
        self._results = {}
        self.active_tool_call_id = None

    def _validate_live_node(self, node):
        strategy = node.get('context_strategy', self._context_strategy)
        if strategy.strategy != ContextStrategy.APPEND:
            raise ValueError('Gemini Live Flows supports APPEND only')
        if node.get('role_message') or node.get('role_messages'):
            raise ValueError('Configure the stable role at Live session setup')
        if node.get('respond_immediately', False):
            raise ValueError('Live nodes must use respond_immediately=False')
        for action in node.get('pre_actions', []) + node.get('post_actions', []):
            if action.get('type') in {'tts_say', 'end_conversation'}:
                raise ValueError('Native speech and closing require the guarded Live lifecycle')
        self._validate_node_config(node['name'], node)

    async def prepare_tools(self, context):
        """Declare the graph's bounded tool union once, before the session starts."""
        tools = {}
        for node in self.nodes.values():
            for tool in self._global_functions + node.get('functions', []):
                if not isinstance(tool, FlowsFunctionSchema):
                    raise ValueError('Live prototype requires explicit FlowsFunctionSchema')
                if tool.name in tools and tools[tool.name] != tool:
                    raise ValueError('A fixed Live tool must have one definition across nodes')
                tools[tool.name] = tool
        schemas = [await self._create_function_schema(tool) for tool in tools.values()]
        context.set_tools(ToolsSchema(standard_tools=schemas))

    def snapshot(self):
        return {'node': self.current_node, 'revision': self.revision,
                'instructions': self.instructions,
                'allowed_tools': sorted(self._current_functions)}

    async def _set_node(self, node_id, node_config):
        self._validate_live_node(node_config)
        if node_id not in self.nodes or node_config != self.nodes[node_id]:
            raise ValueError('Live transition must use the prepared graph node unchanged')
        await super()._set_node(node_id, {**node_config, 'respond_immediately':False})
        self.revision += 1
        if self.event_sink:
            await self.event_sink(self.snapshot())

    async def _update_llm_context(self, role_message, role_messages, task_messages,
                                  functions, strategy=None):
        # Upstream already rendered placeholders. The phase is delivered by the
        # current tool result, not by modifying a local-only LLMContext.
        self.instructions = '\n'.join(str(m.get('content', '')) for m in task_messages)

    async def _create_transition_func(self, name, handler):
        async def invoke(params):
            async with self._tool_lock:
                if params.tool_call_id in self._results:
                    result = {**self._results[params.tool_call_id], 'flow':self.snapshot()}
                elif name not in self._current_functions:
                    result = {'error':'tool_not_allowed_in_node', 'flow':self.snapshot()}
                elif len(self._results) >= 200:
                    result = {'error':'live_tool_budget_exhausted', 'flow':self.snapshot()}
                else:
                    self.active_tool_call_id = params.tool_call_id
                    started = time.monotonic()
                    try:
                        if isinstance(handler, BaseDirectFunctionWrapper):
                            value = await handler.invoke(params.arguments, self)
                        else:
                            value = await self._call_handler(handler, dict(params.arguments))
                        result, next_node = value if isinstance(value, tuple) else (value, None)
                        result = result if isinstance(result, dict) else {'result':result}
                        if next_node is not None and next_node is not NO_RESPONSE:
                            await self.set_node_from_config(next_node)
                        result = {**result, 'flow':self.snapshot(),
                                  'execution_ms':round((time.monotonic()-started)*1000, 1)}
                        if next_node is NO_RESPONSE:
                            result['scheduling'] = 'SILENT'
                    except Exception:
                        result = {'error':'tool_execution_failed', 'flow':self.snapshot(),
                                  'next_action':'No action is confirmed. Explain briefly and continue; do not invent a result.',
                                  'execution_ms':round((time.monotonic()-started)*1000,1)}
                    finally:
                        self.active_tool_call_id = None
                    # End permission is deliberately not cached here: the
                    # durable service may revoke it when the human speaks.
                    if not result.get('end_requested'):
                        self._results[params.tool_call_id] = result
                await params.result_callback(result)
        return invoke


class SpeechSafety(FrameProcessor):
    """Observe PCM for hangup safety only; Gemini still owns turn detection."""
    def __init__(self, on_activity, **kwargs):
        super().__init__(**kwargs)
        self.on_activity = on_activity
        self.human_speaking = False
        self.bot_speaking = False
        self.last_bot_audio = time.monotonic()
        self._loud_samples = self._quiet_samples = 0

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            pcm = array('h', frame.audio)
            rms = math.sqrt(sum(s*s for s in pcm)/max(1,len(pcm)))/32768
            duration = len(pcm)/max(1,frame.num_channels)/frame.sample_rate
            if rms >= .018:
                self._loud_samples += duration
                self._quiet_samples = 0
                if not self.human_speaking and self._loud_samples >= .04:
                    self.human_speaking = True
                    await self.on_activity(True)
            else:
                self._loud_samples = 0
                self._quiet_samples += duration
                if self.human_speaking and self._quiet_samples >= .35:
                    self.human_speaking = False
                    await self.on_activity(False)
        elif isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_speaking = False
            self.last_bot_audio = time.monotonic()
        await self.push_frame(frame, direction)


async def guarded_end(service, identity, worker, llm, safety, state, timeout=30):
    """Wait for native generation + transport playback and recheck human input."""
    started = time.monotonic()
    while time.monotonic()-started < timeout:
        if not service.closing_ready(identity):
            return False
        request = service.get_call(identity)['close_requested']
        # A completed farewell followed by the human's goodbye needs no
        # additional bot turn. The playback and fresh-input gates still apply.
        playback_since = safety.last_bot_audio if request.get('farewell_event') else max(safety.last_bot_audio,started)
        goodbye_played = safety.last_bot_audio > 0 if request.get('farewell_event') else safety.last_bot_audio >= started
        if (not llm._bot_is_responding and not safety.bot_speaking
                and not safety.human_speaking
                and goodbye_played
                and time.monotonic()-playback_since >= .4):
            # No await between the durable gate and queue submission.
            if service.closing_ready(identity):
                reason = service.get_call(identity)['close_requested']['reason']
                state['end_reason'] = {'completed':'task_completed','needs_owner':'awaiting_owner'}.get(
                    reason, 'counterparty_'+reason)
                await worker.queue_frames([EndFrame()])
                return True
        await asyncio.sleep(.05)
    service.revoke_closing(identity, 'playback_timeout')
    return False


def call_nodes(brief, dispatch):
    from core import FLOW_TOOL_MODELS
    from voice import DESCRIPTIONS
    nodes = {}
    tools = []
    for name, model in FLOW_TOOL_MODELS.items():
        schema = model.model_json_schema()
        if name=='end_conversation':
            schema['required']=list(dict.fromkeys([*schema.get('required',[]),'evidence']))
            schema['properties']['evidence']={**schema['properties']['evidence'],'minLength':1,
                'description':'Exact quote from the LATEST human utterance that explicitly ends the conversation. Never empty; never an earlier quote.'}
            schema['properties']['evidence'].pop('default',None)
        async def handle(args, flow, tool_name=name):
            result = await dispatch(tool_name,args,flow.active_tool_call_id)
            target = None
            if result.get('end_requested'):
                target = nodes['ending']
            elif tool_name == 'propose_closing' and result.get('waiting_for_human'):
                target = nodes['confirmation']
            elif tool_name == 'finish_task' and not result.get('error'):
                target = nodes['discussion'] if brief.get('conversation_mode') == 'social' else nodes['review']
            elif tool_name == 'record_answer' and not result.get('error'):
                target = nodes['discussion']
            # These results can be needed for the next question. Keep native
            # WHEN_IDLE scheduling; SILENT can strand a turn after saving.
            # The prompt controls whether a spoken acknowledgement is useful.
            return result,target
        # Capture the name without giving upstream's signature dispatcher a
        # third parameter: Flows handlers have exactly (args, manager).
        def bind(fn, name):
            async def handler(args, manager):
                return await fn(args,manager,name)
            return handler
        description = DESCRIPTIONS.get(name,'Offer to wrap up once; wait for NEW human speech. Never end by itself.')
        if name == 'end_conversation':
            description = 'Request permission to end with the latest exact human quote. After end_requested=true say ONE short goodbye unless farewell_already_said=true; then say nothing. A collected task or pause never permits hangup.'
        tools.append(FlowsFunctionSchema(name=name,description=description,
            properties=schema['properties'],required=schema.get('required',[]),handler=bind(handle,name),
            cancel_on_interruption=name in {'lookup_context','propose_closing','end_conversation'},timeout_secs=8))
    instructions = {
        'discussion':'Follow the human topic. Listen to the whole story; ask one relevant question. Save facts quietly. Collected notes do not end a social conversation.',
        'review':'Requested information is summarized. Address the current human question. Save quietly without repeating acknowledgements. A fresh goodbye or a clear no to your preceding anything-else question permits requesting end_conversation with that exact quote. Do not ask for another confirmation. If intent is unclear, ask once whether anything remains and listen.',
        'confirmation':'An offer to wrap up is pending. A fresh goodbye or a clear no to your preceding anything-else question permits requesting end_conversation with that exact quote. Do not ask for another confirmation. Ask once whether anything remains only if you have not already asked. Wait for NEW human speech; further discussion returns to discussion. Never repeat goodbye or end on old evidence.',
        'ending':'End permission was granted. Follow next_action: if farewell_already_said=true, stay silent because the human returned your farewell; otherwise say ONE short goodbye and nothing else. If the human starts speaking, permission is revoked; listen and continue.'}
    for name, text in instructions.items():
        nodes[name] = {'name':name,'task_messages':[{'role':'developer','content':text}],
                       'functions':tools, 'respond_immediately':False}
    return list(nodes.values())
