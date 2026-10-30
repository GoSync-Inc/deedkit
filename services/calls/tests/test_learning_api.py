import asyncio
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from google.genai import types

from app import create_app
from test_tasks import owner_headers, Archive, BRIEF, HEADERS, prepared, started
from test_regressions import proposed


def test_post_call_analysis_is_visible_and_manual_feedback_changes_input_revision(tmp_path):
    app=create_app(tmp_path,Archive())
    with TestClient(app, headers=owner_headers(tmp_path)) as client:
        call=started(client,prepared(client))
        assert client.get(f'/api/calls/{call["id"]}/analysis').json()['status']=='conversation_active'
        app.state.service.connected(call['id'])
        app.state.service.transcript(call['id'],'user','Не хочу продолжать.')
        client.post(f'/api/calls/{call["id"]}/stop',headers=HEADERS)
        report=client.get(f'/api/calls/{call["id"]}/analysis').json()
        assert report['status']=='waiting_key'
        client.post(f'/api/calls/{call["id"]}/result',headers=HEADERS,json={'review':{'task':'partial','naturalness':2,'notes':'Отказ'}})
        assert client.get(f'/api/calls/{call["id"]}/analysis').json()['id']!=report['id']
        assert client.get('/api/learning').json()['active_prompt_version']
        assert client.post(f'/api/calls/{call["id"]}/analysis/retry').status_code==403
        assert client.post(f'/api/calls/{call["id"]}/analysis/retry',headers=HEADERS).status_code==200
        assert client.get('/api/calls?offset=-1').status_code==422


async def test_native_provider_chunks_are_saved_before_the_aggregator_flushes(tmp_path):
    from core import VoiceService, BriefInput
    from voice import create_model
    from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
    store=VoiceService(tmp_path)
    brief=store.prepare(BriefInput(goal='Узнать время',counterparty='Тест',success_fields=['time']),[])
    call=store.start(brief['id']);store.connected(call['id'])
    model=create_model(brief,'fixture',recorder=lambda role,text:store.transcript_chunk(call['id'],role,text))
    message=types.LiveServerMessage(server_content=types.LiveServerContent(
        input_transcription=types.Transcription(text='Незавершённая реплика'),
        output_transcription=types.Transcription(text='Ответ')))
    with patch.object(GeminiLiveLLMService,'_handle_msg_input_transcription',new=AsyncMock()), \
         patch.object(GeminiLiveLLMService,'_handle_msg_output_transcription',new=AsyncMock()):
        await model._handle_msg_input_transcription(message)
        await model._handle_msg_output_transcription(message)
    assert [(e['role'],e['text']) for e in store.get_call(call['id'])['events']]==[
        ('user','Незавершённая реплика'),('assistant','Ответ')]
    store.close()


async def test_shutdown_flushes_final_turn_before_sealing_analysis(tmp_path):
    app=create_app(tmp_path,Archive())
    async with app.router.lifespan_context(app):
        store=app.state.service
        brief=store.prepare(__import__('core').BriefInput.model_validate({**BRIEF,'source_message_ids':[]}),[])
        call=store.start(brief['id']);store.connected(call['id'])
        class Runner:
            async def cancel(self):
                store.transcript(call['id'],'assistant','До свидания.')
        app.state.session.update(call_id=call['id'],runner=Runner())
    reopened=__import__('core').VoiceService(tmp_path)
    snapshot=reopened.learning.report(call['id'])['snapshot']
    assert snapshot['events'][-1]['text']=='До свидания.'
    assert snapshot['end_reason']=='service_shutdown'
    reopened.close()


async def test_case_review_starts_comparison_but_does_not_activate_candidate(tmp_path):
    import httpx
    app=create_app(tmp_path,Archive())
    candidate,case=await proposed(app.state.service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver', headers=owner_headers(tmp_path)) as client:
        headers={'Origin':'http://testserver','X-Voice-Service':'1'}
        reviewed=await client.post('/api/learning/cases/'+case['id'],headers=headers,json={'status':'accepted'})
        assert reviewed.status_code==200,reviewed.text
        overview=(await client.get('/api/learning')).json()
        assert overview['evaluations'][0]['status']=='queued'
        activate=await client.post('/api/learning/candidates/'+candidate['id']+'/activate',headers=headers)
        assert activate.status_code==409
        assert (await client.post('/api/learning/rollback',headers=headers)).status_code==200
    app.state.service.close()


async def test_forgetting_key_cancels_inflight_analysis_and_keeps_it_for_later(tmp_path):
    import httpx
    started_review=asyncio.Event()
    class Reviewer:
        async def review(self,snapshot,key):
            started_review.set()
            await asyncio.Future()
    app=create_app(tmp_path,Archive(),reviewer=Reviewer())
    async with app.router.lifespan_context(app):
        store=app.state.service
        from test_learning import conversation
        identity=conversation(store)
        store.transcript(identity,'user','До свидания.')
        store.stop(identity)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver', headers=owner_headers(tmp_path)) as client:
            await client.post('/api/credentials',headers=HEADERS,json={'gemini':'fixture-key'})
            await asyncio.wait_for(started_review.wait(),timeout=3)
            await client.delete('/api/credentials',headers=HEADERS)
            assert store.learning.report(identity)['status']=='queued'
            assert not (await client.get('/api/config')).json()['credentials']['gemini']
