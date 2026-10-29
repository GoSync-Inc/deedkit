"""Native Flows contract: real FlowManager state, wire results and closing safety."""
import importlib
import importlib.util
from types import SimpleNamespace

import pytest

from core import BriefInput, VoiceService
from pipecat.flows import FlowManager, FlowsFunctionSchema
from pipecat.flows.types import ContextStrategy, ContextStrategyConfig
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor


def implementation():
    assert importlib.util.find_spec('live_flows'), 'Gemini Live Flows executor is not implemented'
    return importlib.import_module('live_flows')


class Worker(PipelineWorker):
    def __init__(self):
        super().__init__(Pipeline([FrameProcessor()]))
        self.frames = []
    async def queue_frames(self, frames):
        self.frames.extend(frames)
    async def queue_frame(self, frame):
        self.frames.append(frame)


async def manager(nodes):
    from voice import AuditedGemini
    context = LLMContext()
    worker = Worker()
    llm = AuditedGemini(api_key='fixture-not-a-key')
    flow = implementation().GeminiLiveFlowManager(worker=worker, llm=llm,
        context_aggregator=LLMContextAggregatorPair(context), nodes=nodes)
    assert isinstance(flow, FlowManager)
    await flow.prepare_tools(context)
    await flow.initialize(nodes[0])
    return flow, context, worker


async def invoke(context, name, args=None, identity='tool-1'):
    returned = []
    async def result(value, **kwargs):
        returned.append((value, kwargs))
    schema = next(s for s in context.tools.standard_tools if s.name == name)
    await schema.handler(SimpleNamespace(arguments=args or {}, tool_call_id=identity,
        result_callback=result))
    return returned[0][0]


async def test_native_transition_is_in_result_before_model_resumes_without_context_rewrites():
    next_node = {'name':'discussion', 'task_messages':[{'role':'developer','content':'Listen to {{ topic }}.'}],
                 'functions':[], 'respond_immediately':False}
    async def enter(args, flow):
        flow.state['topic'] = 'the whole story'
        return {'saved':True}, next_node
    initial = {'name':'greeting', 'task_messages':[{'role':'developer','content':'Greet once.'}],
        'functions':[FlowsFunctionSchema(name='enter',description='Start',properties={},required=[],handler=enter)],
        'respond_immediately':False}
    flow, context, worker = await manager([initial,next_node])
    result = await invoke(context,'enter')
    assert flow.current_node == 'discussion'
    assert result['flow']['node'] == 'discussion'
    assert result['flow']['instructions'] == 'Listen to the whole story.'
    assert result['flow']['allowed_tools'] == []
    assert worker.frames == [], 'A native transition must not inject messages, rerun or reconnect'


async def test_fixed_tools_are_declared_once_but_inactive_tool_cannot_execute():
    calls = []
    async def forbidden(args, flow):
        calls.append(args)
        return {'executed':True}, None
    initial = {'name':'one','task_messages':[],'functions':[],'respond_immediately':False}
    other = {'name':'two','task_messages':[],'functions':[
        FlowsFunctionSchema(name='restricted',description='Restricted',properties={},required=[],handler=forbidden)],
        'respond_immediately':False}
    flow, context, worker = await manager([initial,other])
    assert {s.name for s in context.tools.standard_tools} == {'restricted'}
    result = await invoke(context,'restricted')
    assert result['error'] == 'tool_not_allowed_in_node'
    assert not calls and flow.current_node == 'one'


async def test_reset_is_rejected_before_any_transition_side_effect():
    bad = {'name':'bad','task_messages':[], 'context_strategy':ContextStrategyConfig(strategy=ContextStrategy.RESET)}
    with pytest.raises(ValueError,match='APPEND'):
        await manager([bad])


async def test_repeated_tool_call_id_does_not_repeat_business_action_or_transition():
    calls = []
    async def note(args, flow):
        calls.append(args)
        return {'saved':True}, None
    node = {'name':'discussion','task_messages':[],'functions':[
        FlowsFunctionSchema(name='note',description='Note',properties={},required=[],handler=note)],
        'respond_immediately':False}
    flow, context, worker = await manager([node])
    first = await invoke(context,'note',identity='same')
    second = await invoke(context,'note',identity='same')
    assert first == second and len(calls) == 1


def call(tmp_path, mode='task'):
    store = VoiceService(tmp_path)
    brief = store.prepare(BriefInput(goal='Collect feedback',counterparty='Browser',
        success_fields=['feedback'],conversation_mode=mode,dialog_engine='gemini_live_flows'),[])
    job = store.start(brief['id']); store.connected(job['id'])
    store.transcript(job['id'],'user','Всё хорошо.')
    store.tool(job['id'],'record_answer',{'field':'feedback','value':'хорошо','evidence':'Всё хорошо.'},'answer')
    store.tool(job['id'],'finish_task',{'summary':'Feedback saved'},'finish')
    return store, job['id']


def end(store, identity, evidence='', tool_id='end'):
    return store.tool(identity,'end_conversation',{'reason':'completed',
        'summary':'Feedback saved','evidence':evidence},tool_id)


async def test_handler_failure_is_recoverable_and_does_not_expose_exception():
    async def broken(args, flow):
        raise RuntimeError('secret fixture value must not reach the model')
    node = {'name':'discussion','task_messages':[],'functions':[
        FlowsFunctionSchema(name='broken',description='Broken',properties={},required=[],handler=broken)],
        'respond_immediately':False}
    flow, context, worker = await manager([node])
    result = await invoke(context,'broken')
    assert result['error'] == 'tool_execution_failed'
    assert 'secret fixture' not in str(result)
    assert flow.current_node == 'discussion'
    assert flow.active_tool_call_id is None


async def test_cached_action_returns_current_phase_without_repeating_action():
    calls = []
    async def note(args, flow):
        calls.append(args)
        return {'saved':True}, None
    tool = FlowsFunctionSchema(name='note',description='Note',properties={},required=[],handler=note)
    nodes = [{'name':name,'task_messages':[],'functions':[tool],'respond_immediately':False}
             for name in ('discussion','review')]
    flow, context, worker = await manager(nodes)
    await invoke(context,'note',identity='same')
    await flow.set_node_from_config(nodes[1])
    result = await invoke(context,'note',identity='same')
    assert result['flow']['node'] == 'review'
    assert len(calls) == 1


async def test_guard_waits_for_playback_and_cancels_when_human_resumes(tmp_path):
    import asyncio
    store, identity = call(tmp_path)
    store.transcript(identity,'user','Спасибо, до свидания.')
    assert end(store,identity,'Спасибо, до свидания.')['end_requested']
    worker = Worker()
    llm = SimpleNamespace(_bot_is_responding=False)
    safety = SimpleNamespace(bot_speaking=True,human_speaking=False,last_bot_audio=0)
    task = asyncio.create_task(implementation().guarded_end(store,identity,worker,llm,safety,{},timeout=1))
    await asyncio.sleep(.1)
    assert worker.frames == []
    store.audio_activity(identity,True)
    assert await task is False
    assert worker.frames == []


async def test_guard_ends_only_after_permission_and_quiet_playback(tmp_path):
    import asyncio
    import time
    from pipecat.frames.frames import EndFrame
    store, identity = call(tmp_path)
    store.transcript(identity,'user','Спасибо, до свидания.')
    assert end(store,identity,'Спасибо, до свидания.')['end_requested']
    worker = Worker(); state = {}
    llm = SimpleNamespace(_bot_is_responding=False)
    safety = SimpleNamespace(bot_speaking=False,human_speaking=False,last_bot_audio=0)
    async def play_goodbye():
        await asyncio.sleep(.1)
        safety.last_bot_audio=time.monotonic()
    playback=asyncio.create_task(play_goodbye())
    assert await implementation().guarded_end(store,identity,worker,llm,safety,state,timeout=1)
    await playback
    assert len(worker.frames) == 1 and isinstance(worker.frames[0],EndFrame)
    assert state['end_reason'] == 'task_completed'


async def test_guard_does_not_hang_up_before_a_new_goodbye_has_played(tmp_path):
    store, identity = call(tmp_path)
    store.transcript(identity,'user','Спасибо, до свидания.')
    assert end(store,identity,'Спасибо, до свидания.')['end_requested']
    worker = Worker()
    llm = SimpleNamespace(_bot_is_responding=False)
    safety = SimpleNamespace(bot_speaking=False,human_speaking=False,last_bot_audio=0)
    assert not await implementation().guarded_end(store,identity,worker,llm,safety,{},timeout=.6)
    assert worker.frames == []


@pytest.mark.parametrize('assistant,human',[
    ('Хорошо, если вопросов больше нет, мы можем заканчивать. Всего доброго.','До свидания.'),
    ('Хорошо, до свидания.','До свидания.'),
    ('Thanks for your time. Goodbye.','Goodbye.'),
    ('Terima kasih. Sampai jumpa.','Sampai jumpa.'),
])
async def test_returned_farewell_ends_without_another_bot_turn(tmp_path,assistant,human):
    import time
    from pipecat.frames.frames import EndFrame
    store,identity=call(tmp_path)
    store.transcript(identity,'assistant',assistant)
    source=store.get_call(identity)['events'][-1]['id']
    store.audio_activity(identity,True);store.audio_activity(identity,False)
    store.transcript_chunk(identity,'user',human)
    result=end(store,identity,human)
    assert result.get('farewell_already_said') is True
    assert store.get_call(identity)['close_requested']['farewell_event']==source
    store.transcript(identity,'user',human)
    worker=Worker();state={}
    safety=SimpleNamespace(bot_speaking=False,human_speaking=False,last_bot_audio=time.monotonic()-1)
    assert await implementation().guarded_end(store,identity,worker,
        SimpleNamespace(_bot_is_responding=False),safety,state,timeout=.2)
    assert len(worker.frames)==1 and isinstance(worker.frames[0],EndFrame)
    assert state['end_reason']=='task_completed'
    store.close()


@pytest.mark.parametrize('assistant,interrupted,chunk,reopened',[
    ('До свидания.',True,False,False),
    ('До свидания.',False,True,False),
    ('До свидания. Вы ещё что-то хотели спросить?',False,False,False),
    ('Я не говорю до свидания.',False,False,False),
    ('Скажите «до свидания».',False,False,False),
    ('До свидания.',False,False,True),
])
def test_unfinished_or_old_assistant_farewell_does_not_skip_playback(tmp_path,assistant,interrupted,chunk,reopened):
    store,identity=call(tmp_path)
    if chunk:
        store.transcript_chunk(identity,'assistant',assistant)
    else:
        store.transcript(identity,'assistant',assistant,interrupted=interrupted)
    if reopened:
        store.transcript(identity,'user','Подожди, я ещё расскажу.')
    store.transcript(identity,'user','До свидания.')
    assert end(store,identity,'До свидания.').get('end_requested')
    assert not store.get_call(identity)['close_requested'].get('farewell_event')
    store.close()


async def test_returned_farewell_still_waits_for_playback_and_cancels_on_new_story(tmp_path):
    import asyncio
    import time
    store,identity=call(tmp_path)
    store.transcript(identity,'assistant','Всего доброго.')
    store.transcript(identity,'user','До свидания.')
    assert end(store,identity,'До свидания.').get('farewell_already_said')
    worker=Worker()
    safety=SimpleNamespace(bot_speaking=True,human_speaking=False,last_bot_audio=time.monotonic()-1)
    task=asyncio.create_task(implementation().guarded_end(store,identity,worker,
        SimpleNamespace(_bot_is_responding=False),safety,{},timeout=.3))
    await asyncio.sleep(.05)
    assert worker.frames==[]
    store.audio_activity(identity,True)
    store.transcript_chunk(identity,'user','Подожди, ещё один момент.')
    safety.bot_speaking=False
    assert await task is False
    assert worker.frames==[]
    store.close()


async def test_prepared_node_cannot_replace_fixed_tool_schema():
    nodes=[{'name':'discussion','task_messages':[],'functions':[],'respond_immediately':False}]
    flow, context, worker = await manager(nodes)
    changed={**nodes[0],'task_messages':[{'role':'developer','content':'Changed at runtime'}]}
    with pytest.raises(ValueError,match='prepared'):
        await flow.set_node_from_config(changed)
    assert flow.revision == 1


@pytest.mark.parametrize('quote',[
    'Да, теперь всё. Спасибо, до свидания.',
    'Yes, that is everything. Thank you, goodbye.',
    'Ya, sudah selesai. Terima kasih, sampai jumpa.',
])
def test_natural_farewell_phrases_are_accepted_in_all_three_languages(tmp_path,quote):
    store,identity=call(tmp_path)
    store.transcript(identity,'user',quote)
    assert end(store,identity,quote).get('end_requested')


@pytest.mark.parametrize('quote',[
    'Нет, спасибо, до свидания.',
    'No, thank you, goodbye.',
    'Tidak, terima kasih, sampai jumpa.',
])
def test_no_more_questions_with_goodbye_can_end_before_transcript_finalizes(tmp_path,quote):
    store,identity=call(tmp_path)
    store.transcript(identity,'assistant','Есть ли что-то ещё, что мы можем обсудить по этому заказу?')
    store.audio_activity(identity,True)
    store.audio_activity(identity,False)
    store.transcript_chunk(identity,'user',quote)
    assert end(store,identity,quote).get('end_requested')
    store.transcript(identity,'user',quote)
    assert store.closing_ready(identity)
    store.close()


@pytest.mark.parametrize('quote',[
    'Нет, не до свидания.',
    'Нет, спасибо, до свидания, подожди, я ещё расскажу.',
    'No, do not say goodbye yet.',
    'Tidak, jangan sampai jumpa dulu.',
])
def test_negated_or_unfinished_farewell_does_not_authorize_hangup(tmp_path,quote):
    store,identity=call(tmp_path)
    store.transcript(identity,'user',quote)
    assert not end(store,identity,quote).get('end_requested')
    store.close()


def test_live_end_saves_summary_atomically_if_fields_are_already_collected(tmp_path):
    store,identity=call(tmp_path)
    value=store.get_call(identity);value['summary']='';store._save_call(value)
    store.transcript(identity,'user','Спасибо, до свидания.')
    assert end(store,identity,'Спасибо, до свидания.').get('end_requested')
    assert store.get_call(identity)['summary']=='Feedback saved'


@pytest.mark.parametrize('quote,accepted',[
    ('Спасибо, до свидания.',True),
    ('Нет, спасибо.',True),
    ('Да, я ещё расскажу про доставку.',False),
])
def test_pending_owner_decision_is_separate_from_permission_to_end_the_call(tmp_path,quote,accepted):
    store=VoiceService(tmp_path)
    brief=store.prepare(BriefInput(goal='Delivery',counterparty='Browser',success_fields=['fee'],
        approval_fields=['fee'],dialog_engine='gemini_live_flows'),[])
    identity=store.start(brief['id'])['id'];store.connected(identity)
    store.transcript(identity,'user','Доплата пятьдесят тысяч.')
    store.tool(identity,'record_answer',{'field':'fee','value':'50000',
        'evidence':'Доплата пятьдесят тысяч.'},'fee')
    store.transcript(identity,'assistant','Есть ли что-то ещё, что хотелось бы обсудить?')
    store.transcript(identity,'user',quote)
    result=end(store,identity,quote)
    assert bool(result.get('end_requested')) is accepted
    value=store.get_call(identity)
    assert value['decisions'][0]['status']=='pending'
    assert 'answer' not in value['decisions'][0]
    if accepted:
        assert result['reason']=='needs_owner'
        assert result['outcome']=='needs_owner'
        assert result['callback_available'] is False
        assert store.closing_ready(identity)
    else:
        assert not store.closing_ready(identity)
    store.close()


def test_old_closing_question_cannot_authorize_yes_to_a_new_story_question(tmp_path):
    store,identity=call(tmp_path)
    store.tool(identity,'propose_closing',{'summary':'Collected'},'offer')
    store.transcript(identity,'assistant','Будем завершать разговор?')
    store.transcript(identity,'user','Подожди, я ещё расскажу про ключи.')
    store.transcript(identity,'assistant','Ты нашёл ключи в куртке?')
    store.transcript(identity,'user','Да.')
    assert end(store,identity,'Да.').get('error')=='closing_confirmation_required'


def test_native_wire_schema_requires_farewell_quote_even_with_legacy_optional_field():
    from live_flows import call_nodes
    nodes=call_nodes({'conversation_mode':'task'},None)
    schema=next(t for t in nodes[0]['functions'] if t.name=='end_conversation')
    assert 'evidence' in schema.required
    assert schema.properties['evidence']['minLength']==1
    assert 'default' not in schema.properties['evidence']


@pytest.mark.parametrize('language',['en','id'])
def test_non_russian_prompt_does_not_supply_russian_self_reference_examples(tmp_path,language):
    store=VoiceService(tmp_path)
    brief=store.prepare(BriefInput(goal='Delivery',counterparty='Browser',language=language,
        success_fields=['window'],dialog_engine='gemini_live_flows',self_reference='masculine'),[])
    assert 'я понял' not in brief['prompt']
    assert 'я уточнил' not in brief['prompt']


# spec: ANYM-375
@pytest.mark.parametrize('engine',['gemini_live','grok_realtime'])
@pytest.mark.parametrize('self_reference',['masculine','feminine','neutral'])
def test_standard_prompt_gives_russian_self_reference_only_to_russian_briefs(tmp_path,engine,self_reference):
    # Regression (Grok feedback 2026-10-19): an Indonesian call prompt said "Use masculine Russian self-reference".
    store=VoiceService(tmp_path)
    def prompt(language):
        return store.prepare(BriefInput(goal='Delivery',counterparty='Browser',language=language,
            success_fields=['window'],dialog_engine=engine,self_reference=self_reference),[])['prompt']
    for language in ('en','id'):
        assert 'Russian' not in prompt(language)
        assert not any(word in prompt(language) for word in ('я понял','я поняла','понятно'))
    form={'masculine':'Use masculine Russian self-reference: я понял, я уточнил.',
          'feminine':'Use feminine Russian self-reference: я поняла, я уточнила.',
          'neutral':'Use neutral Russian phrasing: понятно, время уточнено;'}[self_reference]
    assert f'Speak Russian. {form}' in prompt('ru')
    assert 'Speak English. Introduce' in prompt('en')


@pytest.mark.parametrize('question,answer,accepted',[
    ('Есть ещё вопросы?','Да.',False),
    ('Is there anything else?','Yes.',False),
    ('Apakah masih ada hal lain yang ingin ditambahkan?','Ya.',False),
    ('Есть ещё вопросы?','Нет, спасибо.',True),
    ('Is there anything else?','No, thank you.',True),
    ('Apakah masih ada hal lain yang ingin ditambahkan?','Tidak, terima kasih.',True),
    ('Are you ready to end our conversation?','Yes.',True),
    ('Boleh kita akhiri percakapan?','Ya.',True),
])
def test_closing_confirmation_respects_question_polarity(tmp_path,question,answer,accepted):
    store,identity=call(tmp_path)
    store.tool(identity,'propose_closing',{'summary':'Collected'},'offer')
    store.transcript(identity,'assistant',question)
    store.transcript(identity,'user',answer)
    result=end(store,identity,answer)
    assert bool(result.get('end_requested')) is accepted


@pytest.mark.parametrize('question,answer,accepted',[
    ('Что-то ещё хотите обсудить?','Нет, спасибо.',True),
    ('Что-то ещё хотите обсудить?','Да, я ещё расскажу.',False),
    ('Есть ли доплата?','Нет, спасибо.',False),
    ('Мы можем завершать наш разговор?','Да, можем завершать.',True),
    ('Anything else you want to discuss?','No, thanks.',True),
    ('Ada hal lain yang ingin dibahas?','Tidak, terima kasih.',True),
    ('Есть ли что-то ещё, что вы хотели бы обсудить?','Нет, у меня нету.',True),
    ('Есть ещё вопросы?','У меня больше нет вопросов, спасибо.',True),
    ('Вы хотите завершить разговор?','Хотелось бы.',True),
    ('Мы завершаем общение по этому заказу?','Да, давайте.',True),
    ('Anything else you want to discuss?',"No, I don't have any other questions.",True),
    ('Ada hal lain yang ingin dibahas?','Tidak ada lagi, terima kasih.',True),
    ('Есть ли у вас какие-то ещё вопросы?','Нет, больше вопросов нет, спасибо.',True),
    ('У вас остались ещё вопросы или мы можем завершить разговор?','Можем завершить.',True),
    ('Подскажите, есть ли ещё какие-либо вопросы?','Нет, у меня больше нет.',True),
    ('Есть ли ещё какие-либо вопросы?','Да, есть вопрос по доставке.',False),
    ('Есть ли ещё какие-либо начисления?','Нет, у меня больше нет.',False),
    ('Есть ещё вопросы?','Нет, у меня нету времени, но я ещё расскажу.',False),
    ('Есть ещё вопросы?','У меня нет вопросов по цене, но есть по времени.',False),
    ('Вы хотите завершить разговор?','Хотелось бы ещё поговорить.',False),
    ('Есть ещё вопросы?','Хотелось бы.',False),
    ('Есть ли доплата?','Нет, у меня нету.',False),
    ('Вы хотите завершить разговор?','Нет, у меня нету.',False),
    ('Хотели бы вы уточнить время доставки?','Хотелось бы.',False),
    ('Anything else?',"No, I don't have time, but listen.",False),
    ('Ada hal lain?','Tidak ada biaya lagi, tetapi saya mau cerita.',False),
])
def test_natural_closing_answer_does_not_require_an_extra_tool_ceremony(tmp_path,question,answer,accepted):
    store,identity=call(tmp_path)
    store.transcript(identity,'assistant',question)
    store.transcript(identity,'user',answer)
    assert bool(end(store,identity,answer).get('end_requested')) is accepted


def test_natural_closing_question_cannot_be_reused_after_new_human_topic(tmp_path):
    store,identity=call(tmp_path)
    store.transcript(identity,'assistant','Что-то ещё хотите обсудить?')
    store.transcript(identity,'user','Подожди, я ещё расскажу про доставку.')
    store.transcript(identity,'user','Нет, спасибо.')
    assert not end(store,identity,'Нет, спасибо.').get('end_requested')


@pytest.mark.parametrize('transcript,evidence,accepted',[
    ('Нет, больше вопросов нет, спасибо.','Нет больше вопросов нет, спасибо.',True),
    ('Спасибо, до свидания.','спасибо до свидания',True),
    ('No, thank you.','no thank you',True),
    ('Нет, у меня есть ещё вопрос.','Нет у меня нету.',False),
    ('До свидания, подожди, я ещё расскажу.','до свидания',False),
    ('es no se daña.','До свидания.',False),
])
def test_closing_quotes_match_spoken_words_without_rewriting_the_source(tmp_path,transcript,evidence,accepted):
    store,identity=call(tmp_path)
    store.transcript(identity,'assistant','Есть ли у вас какие-то ещё вопросы?')
    store.audio_activity(identity,True);store.audio_activity(identity,False)
    store.transcript_chunk(identity,'user',transcript)
    result=end(store,identity,evidence)
    assert bool(result.get('end_requested')) is accepted
    store.transcript(identity,'user',transcript)
    assert store.closing_ready(identity) is accepted
    if accepted:
        value=store.get_call(identity)
        source=next(e for e in value['events'] if e['id']==value['close_requested']['evidence_event'])
        assert source['text']==transcript
        tool=next(e for e in value['events'] if e['kind']=='tool' and e['name']=='end_conversation')
        assert tool['arguments']['evidence']==evidence
    store.close()


def test_delayed_closing_offer_does_not_ask_again_after_a_fresh_goodbye(tmp_path):
    store,identity=call(tmp_path)
    store.transcript(identity,'user','Да, теперь всё. Спасибо, до свидания.')
    result=store.tool(identity,'propose_closing',{'summary':'Collected'},'late-offer')
    assert result.get('closing_already_confirmed')
    assert result['evidence']=='Да, теперь всё. Спасибо, до свидания.'
    assert not result.get('waiting_for_human')


def test_delayed_task_summary_preserves_latest_goodbye_without_granting_hangup(tmp_path):
    store,identity=call(tmp_path)
    store.transcript(identity,'user','Спасибо, до свидания.')
    result=store.tool(identity,'finish_task',{'summary':'Collected'},'late-summary')
    assert result.get('latest_explicit_goodbye')=='Спасибо, до свидания.'
    assert not result.get('end_requested')
    assert not store.get_call(identity).get('close_requested')
    store.transcript(identity,'user','Подожди, я ещё не закончил.')
    continued=store.tool(identity,'finish_task',{'summary':'Updated'},'new-summary')
    assert not continued.get('latest_explicit_goodbye')


def test_native_task_cannot_end_when_fields_collected_but_human_story_unfinished(tmp_path):
    store, identity = call(tmp_path)
    store.transcript(identity,'user','И ещё я хочу рассказать про свой проект.')
    assert end(store,identity)['error'] == 'closing_confirmation_required'
    assert not store.get_call(identity).get('close_requested')
    store.close()


def test_retry_with_new_tool_id_needs_new_human_speech(tmp_path):
    store, identity = call(tmp_path)
    assert end(store,identity)['await_new_human_speech']
    assert end(store,identity,tool_id='another')['error'] == 'closing_retry_requires_new_human_speech'
    store.transcript(identity,'user','Ладно, пока!')
    assert end(store,identity,'Ладно, пока!','fresh')['end_requested']
    store.close()


def test_finalizing_same_input_chunks_is_not_new_human_speech(tmp_path):
    store, identity = call(tmp_path)
    store.transcript_chunk(identity,'user','Ну, всё.')
    assert end(store,identity,'Ну, всё.')['await_new_human_speech']
    store.transcript(identity,'user','Ну, всё.')
    assert end(store,identity,'Ну, всё.','another')['error'] == 'closing_retry_requires_new_human_speech'
    store.close()


# spec: ANYM-374
def test_late_final_of_an_old_goodbye_cannot_close_a_newer_unfinished_story(tmp_path):
    store, identity = call(tmp_path)
    store.audio_activity(identity,True)
    store.transcript_chunk(identity,'user','Спасибо, до свидания.')
    store.audio_activity(identity,False)
    store.audio_activity(identity,True)
    store.transcript_chunk(identity,'user','Хотя подожди, я ещё про доставку')
    store.audio_activity(identity,False)
    # Pipecat finalizes the old goodbye only now, after the newer story began.
    store.transcript(identity,'user','Спасибо, до свидания.')
    assert end(store,identity,'Спасибо, до свидания.')['error'] == 'closing_confirmation_required'
    assert not store.closing_ready(identity)
    store.transcript(identity,'user','Хотя подожди, я ещё про доставку не сказал.')
    store.audio_activity(identity,True)
    store.transcript_chunk(identity,'user','Всё, до свидания.')
    store.audio_activity(identity,False)
    store.transcript(identity,'user','Всё, до свидания.')
    assert end(store,identity,'Всё, до свидания.','fresh')['end_requested']
    assert store.closing_ready(identity)
    store.close()


# spec: ANYM-374
def test_goodbye_sentence_final_keeps_the_rest_of_its_chunk_pending(tmp_path):
    store, identity = call(tmp_path)
    store.audio_activity(identity,True)
    store.transcript_chunk(identity,'user','До свидания. Хотя нет, подожди')
    store.audio_activity(identity,False)
    # Pipecat sends one sentence per final; the rest of the chunk is still unfinalized speech.
    store.transcript(identity,'user','До свидания.')
    assert end(store,identity,'До свидания.')['error'] == 'closing_confirmation_required'
    assert not store.closing_ready(identity)
    store.close()


# spec: ANYM-374
def test_late_final_of_an_old_answer_keeps_the_newer_closing_reply(tmp_path):
    store, identity = call(tmp_path)
    store.transcript_chunk(identity,'user','Доставка с 12 до 13.')
    store.transcript(identity,'assistant','Записала. Можем заканчивать?')
    store.transcript_chunk(identity,'user','Да, можем заканчивать.')
    store.transcript(identity,'user','Доставка с 12 до 13.')
    assert end(store,identity,'Да, можем заканчивать.')['end_requested']
    # The reply's own final is the same input, not new human speech.
    store.transcript(identity,'user','Да, можем заканчивать.')
    assert store.closing_ready(identity)
    store.close()


# spec: ANYM-374
def test_new_words_after_a_granted_goodbye_revoke_it_before_their_final(tmp_path):
    store, identity = call(tmp_path)
    store.audio_activity(identity,True)
    store.transcript_chunk(identity,'user','Ну пока. Пока!')
    store.audio_activity(identity,False)
    store.transcript(identity,'user','Ну пока.')
    assert end(store,identity,'Пока!')['end_requested']
    # The transcription of newer speech can arrive after its audio already stopped.
    store.transcript_chunk(identity,'user',' Хотя подожди, я ещё про доставку')
    store.transcript(identity,'user','Пока!')
    assert not store.closing_ready(identity)
    assert end(store,identity,'Пока!')['error'] == 'closing_result_obsolete'
    store.close()


# spec: ANYM-374
def test_goodbye_granted_before_late_finals_of_two_inputs_stays_granted(tmp_path):
    store, identity = call(tmp_path)
    store.audio_activity(identity,True)
    store.transcript_chunk(identity,'user','Доставка с 12 до 13.')
    store.audio_activity(identity,False)
    store.transcript(identity,'assistant','Можем заканчивать?')
    store.audio_activity(identity,True)
    store.transcript_chunk(identity,'user','Спасибо, до свидания.')
    store.audio_activity(identity,False)
    assert end(store,identity,'Спасибо, до свидания.')['end_requested']
    store.transcript(identity,'user','Доставка с 12 до 13.')
    store.transcript(identity,'user','Спасибо, до свидания.')
    assert store.closing_ready(identity)
    assert end(store,identity,'Спасибо, до свидания.')['end_requested']
    store.close()


# spec: ANYM-374
def test_every_chunk_split_keeps_exactly_the_speech_no_final_has_transcribed(tmp_path):
    store, identity = call(tmp_path)
    old, new = 'До свидания. Ну пока.', 'Хотя подожди, ещё'
    speech = old + ' ' + new
    for first in range(1, len(speech)):
        for second in range(first, len(speech)):
            pieces = [p for p in (speech[:first], speech[first:second], speech[second:]) if p]
            events = [{'kind':'transcript_chunk','role':'user','text':p} for p in pieces]
            # Pipecat finalizes the old speech sentence by sentence, after all chunks arrived.
            for sentence in ('До свидания.', 'Ну пока.'):
                events.append({'kind':'transcript','role':'user','text':sentence})
            assert ' '.join(store._latest_turn(events,'user').split()) == new, pieces
            events.append({'kind':'transcript','role':'user','text':new})
            assert store._turns(events,'user')[2] == [], pieces
    store.close()


def test_simple_yes_requires_offer_and_new_human_turn(tmp_path):
    store, identity = call(tmp_path)
    store.transcript(identity,'user','Да.')
    offer = store.tool(identity,'propose_closing',{'summary':'Feedback saved'},'offer')
    assert offer['waiting_for_human']
    assert end(store,identity,'Да.')['error'] == 'closing_confirmation_required'
    store.transcript(identity,'assistant','Можем заканчивать или хочешь ещё что-то добавить?')
    store.transcript(identity,'user','Да, заканчиваем.')
    assert end(store,identity,'Да, заканчиваем.','fresh')['end_requested']
    store.close()


def test_speech_start_revokes_pending_hangup_and_cached_success(tmp_path):
    store, identity = call(tmp_path)
    store.transcript(identity,'user','Пока!')
    assert end(store,identity,'Пока!')['end_requested']
    assert store.closing_ready(identity)
    store.audio_activity(identity,True)
    assert not store.closing_ready(identity)
    assert end(store,identity,'Пока!')['error'] == 'closing_result_obsolete'
    assert not store.get_call(identity).get('close_requested')
    store.close()


def test_speaking_human_blocks_even_explicit_goodbye(tmp_path):
    store, identity = call(tmp_path)
    store.audio_activity(identity,True)
    store.transcript_chunk(identity,'user','Пока!')
    assert end(store,identity,'Пока!')['error'] == 'human_still_speaking'
    store.close()


@pytest.mark.parametrize('requested,expected', [
    ('SILENT','SILENT'), ('WHEN_IDLE','WHEN_IDLE'),
    (None,'WHEN_IDLE'), ('INTERRUPT','WHEN_IDLE')])
async def test_background_note_scheduling_is_preserved_on_gemini_wire(requested,expected):
    import json
    from google.genai.live import AsyncSession
    from voice import AuditedGemini
    responses = []
    class Socket:
        async def send(self, message):
            responses.append(json.loads(message))
    class Session:
        _api_client = SimpleNamespace(vertexai=False)
        _ws = Socket()
        send_tool_response = AsyncSession.send_tool_response
    llm = AuditedGemini(api_key='fixture-not-a-key',live_flows=True)
    llm._session = Session()
    llm._disconnecting = False
    llm._function_is_async = lambda name: True
    result = {'ok':True}
    if requested is not None:
        result['scheduling'] = requested
    await llm._tool_result('note-1','record_answer',result)
    payload = responses[0]['tool_response']['functionResponses'][0]
    assert payload.get('scheduling') == expected
    assert payload['id'] == 'note-1'
    assert payload['response']['ok'] is True


@pytest.mark.parametrize('mode',['task','social'])
@pytest.mark.parametrize('tool',['record_answer','finish_task'])
async def test_conversation_tools_allow_the_model_to_continue_after_saving(mode,tool):
    async def dispatch(name,args,identity):
        return {'ok':True}
    flow,context,worker=await manager(implementation().call_nodes({'conversation_mode':mode},dispatch))
    result=await invoke(context,tool,{'summary':'Saved'} if tool=='finish_task' else
                        {'field':'window','value':'10–11','evidence':'с 10 до 11'})
    assert result.get('scheduling','WHEN_IDLE')=='WHEN_IDLE', \
        'Saving during a conversation must allow the next question, not wait for the human to repeat'
    assert worker.frames==[], 'Resume through the native result, without an extra LLM run'


# spec: ANYM-617
@pytest.mark.parametrize('is_final,will_continue',[(True,False),(False,True)])
async def test_flows_tool_result_takes_pipecat_1_14_keywords_and_closes_a_final_call(is_final,will_continue):
    import json
    from google.genai.live import AsyncSession
    from voice import AuditedGemini
    responses = []
    class Socket:
        async def send(self, message):
            responses.append(json.loads(message))
    class Session:
        _api_client = SimpleNamespace(vertexai=False)
        _ws = Socket()
        send_tool_response = AsyncSession.send_tool_response
    llm = AuditedGemini(api_key='fixture-not-a-key',live_flows=True)
    llm._session = Session()
    llm._disconnecting = False
    llm._function_is_async = lambda name: True
    # Pipecat 1.14 passes is_final and spoken; the result's own scheduling hint still decides.
    await llm._tool_result('note-2','record_answer',{'ok':True},is_final=is_final,spoken=False)
    payload = responses[0]['tool_response']['functionResponses'][0]
    assert payload.get('will_continue',False) is will_continue
    assert payload.get('scheduling') == 'WHEN_IDLE' and payload['id'] == 'note-2'
