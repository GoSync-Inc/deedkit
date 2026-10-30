"""Private call evidence and the improvement loop, with no real provider calls."""
import json

import pytest

from core import BriefInput, VoiceService, ServiceError


def conversation(store, language='ru'):
    brief=store.prepare(BriefInput(goal='Уточнить интервал доставки',counterparty='Учебный поставщик',
                                  language=language,success_fields=['time']),[])
    call=store.start(brief['id'])
    store.connected(call['id'])
    return call['id']


def test_received_transcript_is_not_cut_off_and_terminal_call_is_queued_once(tmp_path):
    store=VoiceService(tmp_path)
    identity=conversation(store)
    long='Реплика. '*1500
    store.transcript(identity,'user',long)
    assert store.get_call(identity)['events'][0]['text']==long
    for i in range(1001):
        store.transcript_chunk(identity,'assistant',str(i))
    store.stop(identity)
    report=store.learning.report(identity)
    assert report['status']=='waiting_key'
    assert report['snapshot']['events'][-1]['text']=='1000'
    assert report['coverage']['audio_recorded'] is False
    assert report['coverage']['recognition_accuracy']=='unverified'
    store.stop(identity)
    assert store.learning.report(identity)['id']==report['id']
    store.close()
    reopened=VoiceService(tmp_path)
    assert reopened.learning.report(identity)['id']==report['id']
    assert reopened.get_call(identity)['events'][0]['text']==long
    reopened.close()


def test_restart_and_owner_feedback_create_versioned_analysis_inputs(tmp_path):
    store=VoiceService(tmp_path)
    identity=conversation(store)
    store.transcript(identity,'user','Не хочу продолжать этот разговор.')
    store.close()
    store=VoiceService(tmp_path)
    initial=store.learning.report(identity)
    assert initial['snapshot']['end_reason']=='service_restart'
    store.result(identity,review={'task':'fail','naturalness':2,'notes':'Продолжал убеждать после отказа'})
    updated=store.learning.report(identity)
    assert updated['id']!=initial['id']
    assert updated['snapshot']['review']['notes'].startswith('Продолжал')
    assert store.learning.history(identity)[-1]['id']==initial['id']
    store.close()


async def test_semantic_analysis_creates_evidence_linked_case_and_prompt_candidate(tmp_path):
    from learning import LearningWorker
    store=VoiceService(tmp_path)
    identity=conversation(store)
    store.transcript(identity,'user','Не звоните мне больше.')
    store.transcript(identity,'assistant','А может всё-таки обсудим доставку?')
    events=store.get_call(identity)['events']
    store.stop(identity)
    class Reviewer:
        async def review(self, snapshot, key):
            assert key=='fixture-key'
            return {'summary':'Продолжил после явного отказа.', 'findings':[{
                'category':'refusal','severity':'high','title':'Не принял отказ',
                'explanation':'Продолжил убеждать собеседника.',
                'evidence_event_ids':[e['id'] for e in events],
                'expected_behavior':'После явного отказа кратко попрощаться и закончить разговор.'}],
                'candidate_instruction':'Respect an explicit request to end the conversation. Close briefly without persuasion.'}
    worker=LearningWorker(store,lambda:'fixture-key',Reviewer())
    assert await worker.run_once()
    report=store.learning.report(identity)
    assert report['status']=='complete' and report['semantic']['model']=='gemini-3.8-flash'
    assert report['findings'][-1]['evidence_event_ids']==[e['id'] for e in events]
    cases=store.learning.cases()
    case=next(c for c in cases if c.get('source_call_id')==identity)
    assert case['status']=='draft' and case['language']=='ru'
    assert case['turns']==['Не звоните мне больше.']
    assert case['source_report_id']==report['id']
    candidate=store.learning.candidates()[0]
    assert candidate['status']=='proposed'
    assert store.prepare(BriefInput(goal='Узнать время',counterparty='Тест',success_fields=['time']),[])['prompt_version']==report['snapshot']['brief']['prompt_version']
    with pytest.raises(ServiceError):
        store.learning.activate(candidate['id'])
    assert not await worker.run_once()
    store.close()


def test_chunk_archive_appends_without_rewriting_the_call_body(tmp_path):
    store=VoiceService(tmp_path)
    identity=conversation(store)
    statements=[]
    store.db.set_trace_callback(statements.append)
    for text in ['Начало',' середина',' конец']:
        store.transcript_chunk(identity,'user',text)
    assert not any(s.startswith('UPDATE calls') for s in statements)
    store.db.set_trace_callback(None)
    store.close()
    store=VoiceService(tmp_path)
    assert ''.join(e['text'] for e in store.get_call(identity)['events'] if e['kind']=='transcript_chunk')=='Начало середина конец'
    store.close()


async def test_unknown_evidence_cannot_become_a_case_or_candidate(tmp_path):
    from learning import LearningWorker
    store=VoiceService(tmp_path)
    identity=conversation(store)
    store.transcript(identity,'user','Нет, доставка не утром, а вечером.')
    store.stop(identity)
    class Reviewer:
        async def review(self,snapshot,key):
            return {'summary':'Недоказанный вывод','findings':[{
                'category':'refusal','severity':'high','title':'Отказ','explanation':'Ошибка',
                'evidence_event_ids':['invented-event'], 'expected_behavior':'Повесить трубку'}],
                'candidate_instruction':'Hang up whenever the user says no.'}
    worker=LearningWorker(store,lambda:'fixture-key',Reviewer())
    await worker.run_once()
    report=store.learning.report(identity)
    assert report['semantic']['rejected_findings']==1
    assert not [c for c in store.learning.cases() if c.get('source_call_id')==identity]
    assert store.learning.candidates()==[]
    store.close()


async def test_failed_reviewer_keeps_archive_and_does_not_retry_forever_or_leak_key(tmp_path):
    from learning import LearningWorker
    store=VoiceService(tmp_path)
    identity=conversation(store)
    store.transcript(identity,'user','Добрый день.')
    store.stop(identity)
    class Reviewer:
        async def review(self,snapshot,key):
            raise RuntimeError('provider payload SECRET-KEY')
    worker=LearningWorker(store,lambda:'SECRET-KEY',Reviewer())
    await worker.run_once()
    report=store.learning.report(identity)
    assert report['status']=='failed'
    assert 'SECRET-KEY' not in json.dumps(report)
    assert not await worker.run_once()
    store.learning.retry(identity)
    assert store.learning.report(identity)['status']=='queued'
    store.close()


def test_native_end_tool_requires_explicit_transcript_and_keeps_refusal_separate_from_success(tmp_path):
    store=VoiceService(tmp_path)
    identity=conversation(store)
    missing=store.tool(identity,'end_conversation',{'reason':'refused','summary':'Отказ', 'evidence':'Не звоните мне'},'bad')
    assert missing.get('error')=='closing_evidence_missing'
    store.transcript(identity,'user','Не звоните мне больше.')
    result=store.tool(identity,'end_conversation',{'reason':'refused','summary':'Собеседник отказался',
                     'evidence':'Не звоните мне больше.'},'close')
    assert result['end_requested'] is True and result['outcome']=='refused'
    assert store.get_call(identity)['status']=='active'  # media runtime closes after the tool callback
    store.stop(identity,'counterparty_refused')
    assert store.learning.report(identity)['snapshot']['outcome']=='refused'
    store.close()


def test_closing_can_cite_received_chunks_when_the_final_turn_is_not_flushed(tmp_path):
    store=VoiceService(tmp_path)
    identity=conversation(store)
    store.transcript_chunk(identity,'user','Не звоните')
    store.transcript_chunk(identity,'user',' мне больше.')
    result=store.tool(identity,'end_conversation',{'reason':'refused','summary':'Отказ',
                       'evidence':'Не звоните мне больше.'},'close')
    assert result.get('end_requested') is True
    store.stop(identity,'counterparty_refused')
    assert store.learning.report(identity)['snapshot']['close_requested']['evidence_event']
    store.close()


def test_controller_analysis_has_separate_provenance_and_cannot_overwrite_a_gemini_review(tmp_path):
    store=VoiceService(tmp_path)
    identity=conversation(store)
    store.transcript(identity,'user','До свидания.')
    store.stop(identity)
    initial=store.learning.report(identity)
    saved=store.learning.submit_agent_review(identity,{'summary':'Разбор контроллера','findings':[],'candidate_instruction':''})
    assert saved['id']!=initial['id']
    assert saved['semantic']['model']=='control_mcp_agent'
    assert store.learning.history(identity)[-1]['id']==initial['id']
    assert store.learning.pending()==[]
    store.close()
