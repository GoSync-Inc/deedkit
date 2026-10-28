"""Dot regressions: scope, partial empty results, direct IDs and classified errors."""
import asyncio
import json
from pathlib import Path
from datetime import datetime,timezone
from types import SimpleNamespace as NS

import pytest
from psycopg.errors import QueryCanceled
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.chat_evidence import backfill
from messaging_service.envelope import stable_id
from messaging_service.store import APIError
from messaging_service.result_evidence import decorate
from test_archive import archive,event
from test_collectors import worker,message
from test_whatsapp import wa


def test_exact_provider_reference_and_scoped_conflict_lifecycle(archive):
    ar,s,key,hidden=archive
    mid=ar.ingest(event())['message']
    ar.ingest(event(body='contradiction'))
    resolved=s.resolve_reference(key,'archive-test','100','17')
    assert resolved['message']['id']==mid and resolved['message']['provider_id']=='17'
    assert resolved['message']['evidence']['active_conflicts']==1
    assert resolved['sync']['conflicts']=={'active':1,'historical':0}
    for args in [('archive-test','100','17'),('archive-test','missing','17')]:
        with pytest.raises(APIError,match='not_found'):s.resolve_reference(hidden,*args)
    # Even a successful re-observation of the retained version leaves equal-time ambiguity.
    ar.ingest(event(origin='reconcile'))
    assert s.get_context(key,mid)['message']['evidence']['active_conflicts']==1
    ar.ingest(event(body='Later evidence',updated_at='2026-10-01T12:02:00Z'))
    result=s.get_context(key,mid)
    assert result['message']['evidence']['active_conflicts']==0
    assert result['message']['evidence']['historical_conflicts']==1
    assert s.capabilities(key)['sources'][0]['detail_code']!='version_conflict'


def test_telegram_provider_ids_do_not_mix_positive_negative_namespaces(archive):
    ar,s,key,_=archive
    with s.pool.connection() as c:c.execute('UPDATE account_policies SET discover_chats=true WHERE account=%s',('archive-test',))
    positive=ar.ingest(event(chat='100',provider_id='17'))['message']
    negative=ar.ingest(event(chat='-100100',provider_id='17',body='channel'))['message']
    assert s.resolve_reference(key,'archive-test','100','17')['message']['id']==positive
    assert s.resolve_reference(key,'archive-test','-100100','17')['message']['id']==negative
    with pytest.raises(APIError):s.resolve_reference(key,'archive-test','-100','17')


def test_per_chat_window_empty_reason_and_fresh_scan_without_new_messages(worker):
    w,s,key=worker
    floor=datetime(2026,7,1,tzinfo=timezone.utc)
    w.history_since=floor
    w.state.inventory([('100','user','Fixture'),('200','user','Waiting')],floor)
    cid=stable_id('chat',w.account,'100');waiting=stable_id('chat',w.account,'200')
    assert s.search(key,'missing',chat=waiting)['result_evidence']['empty_reason']=='history_not_scanned'
    with s.pool.connection() as c:
        c.execute('UPDATE accounts SET synthetic=false WHERE id=%s',(w.account,))
    w.provider.messages=[message(1)]
    asyncio.run(w.sync_once())
    first=s.get_sync_status(key,chat_id=cid)
    asyncio.run(w.sync_once())
    second=s.get_sync_status(key,chat_id=cid)
    assert second['history']['last_head_scan_at']>first['history']['last_head_scan_at']
    upper=first['history']['last_head_scan_at']
    result=s.search(key,'missing',chat=cid,from_time='2026-07-01T00:00:00Z',to_time=upper)
    assert result['result_evidence']['empty_reason']=='no_matches_in_traversed_range'
    assert result['result_evidence']['requested_range_traversed']
    # A min/max archived date is insufficient outside the configured window.
    result=s.search(key,'missing',chat=cid,from_time='2026-01-01T00:00:00Z',to_time=upper)
    assert result['result_evidence']['empty_reason']=='no_matches_in_partial_archive'
    assert not result['result_evidence']['requested_range_traversed']
    assert not second['history']['full_history_verified']
    assert s.list_chats(key)['items'][0]['sync']['history']


def test_hidden_and_absent_chats_have_identical_empty_diagnostics(seeded):
    s,a,b=seeded
    # ANYM-253: search names a hidden or absent chat with one not_found instead of an empty page.
    for chat in ('whatsapp-demo-1-chat','absent'):
        with pytest.raises(APIError,match='not_found'):s.search(b,'missing',chat=chat)
    errors=[]
    for chat in ('whatsapp-demo-1-chat','absent'):
        with pytest.raises(APIError) as error:s.search(b,'missing',chat=chat)
        errors.append((error.value.code,error.value.status,getattr(error.value,'fields',None)))
    assert errors[0]==errors[1]
    for chat in ('whatsapp-demo-1-chat','absent'):
        with pytest.raises(APIError,match='not_found'):s.get_sync_status(b,chat_id=chat)
    assert s.search(b,accounts=[],query='missing')['result_evidence']['empty_reason']=='empty_filter'


def test_media_states_match_history_and_context_before_and_after_download(worker):
    w,s,key=worker
    asyncio.run(w.capture(NS(message=message(9,media=True))));w.spool.drain(w.state.apply)
    item=s.search(key,'message 9')['items'][0]
    file=item['attachments'][0];assert file['download_status']=='queued'
    assert file['extraction_status']=='awaiting_download'
    pending=s.get_attachment(key,file['id'])
    assert pending['download_status']==file['download_status']
    context=s.get_context(key,item['id'])
    assert context['pending_attachments'][0]['id']==file['id']
    assert asyncio.run(w.media_once())
    history=s.get_messages(key,item['chat'])['items'][0]
    assert history['attachments'][0]['download_status']=='ready'
    context=s.get_context(key,item['id'])
    assert context['attachments'][0]['download_status']=='ready' and not context['pending_attachments']


def test_legacy_conflict_backfill_and_provider_catalog_mapping(archive):
    ar,s,key,_=archive
    ar.ingest(event());ar.ingest(event(body='conflict'))
    with s.pool.connection() as c:
        c.execute('DELETE FROM message_conflicts')
        c.execute('UPDATE chats SET provider_chat=NULL')
        backfill(c)
    assert s.resolve_reference(key,'archive-test','100','17')['message']['evidence']['active_conflicts']==1


def test_search_timeout_is_error_with_fallback_request_id_and_no_sensitive_details(seeded,monkeypatch):
    s,a,b=seeded
    def timeout(*args,**kwargs):raise QueryCanceled('secret SQL and private query must not escape')
    monkeypatch.setattr(s,'_search',timeout)
    with TestClient(create_app(s,['testserver'])) as client:
        response=client.post('/v1/search',json={'query':'private query'},headers={'Authorization':'Bearer '+b})
    assert response.status_code==503
    data=response.json();assert data['error']=='query_timeout' and data['retryable']
    assert data['request_id']==response.headers['x-request-id']
    assert data['results_state']=='not_returned_due_to_error' and 'get_messages' in data['fallback']
    assert 'secret SQL' not in str(data) and 'private query' not in str(data)


def test_tool_manifest_matches_actual_wire_tools(seeded):
    s,a,b=seeded
    with TestClient(create_app(s,['testserver'])) as client:
        headers={'Authorization':'Bearer '+b,'Accept':'application/json, text/event-stream'}
        result=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/list'}).json()
    assert sorted(t['name'] for t in result['result']['tools'])==s.capabilities(b)['tool_manifest']['names']
    assert 'resolve_reference' in s.capabilities(b)['available']


def test_failed_chat_scan_is_visible_and_clears_after_success(worker):
    w,s,key=worker
    w.state.inventory([('100','user','Fixture')],None)
    cid=stable_id('chat',w.account,'100')
    w.state.failed('100','provider_unavailable')
    assert s.get_sync_status(key,chat_id=cid)['history']['last_error_code']=='provider_unavailable'
    assert s.search(key,'missing',chat=cid)['result_evidence']['empty_reason']=='provider_unavailable'
    asyncio.run(w.sync_once())
    assert s.get_sync_status(key,chat_id=cid)['history']['last_error_code'] is None


def test_file_empty_search_explains_pending_originals(worker):
    w,s,key=worker
    asyncio.run(w.capture(NS(message=message(9,media=True))));w.spool.drain(w.state.apply)
    evidence=s.search_files(key,'missing')['result_evidence']
    assert evidence['empty_reason']=='file_content_not_fully_indexed'
    assert evidence['file_index']['download']['queued']==1
    assert evidence['file_index']['pending_stages_in_scope']>=1


def test_revision_changed_between_query_and_evidence_requires_reread(worker):
    w,s,key=worker
    asyncio.run(w.capture(NS(message=message(9,media=True))));w.spool.drain(w.state.apply)
    row=s.search(key,'message 9')['items'][0]
    asyncio.run(w.capture(NS(message=message(9,body='new version',edited=datetime(2026,10,2,tzinfo=timezone.utc),media=True))))
    w.spool.drain(w.state.apply)
    with s.pool.connection() as c:decorate(c,[row])
    assert row['evidence']['revision_changed_during_read']
    assert row['evidence']['archive_revision']==1 and row['evidence']['current_revision_at_evidence_read']==2
    assert row['attachments']==[] and row['evidence']['next_action']=='reread_message'


def test_whatsapp_empty_chat_is_resolvable_before_message_ingest(wa):
    w,s,key=wa
    async def empty(chat,**kwargs):return {'messages':[],'exhausted':True,'pending':False}
    w.provider.page=empty
    asyncio.run(w.sync_once())
    result=s.resolve_reference(key,w.account,'123@c.us')
    assert result['sync']['messages']['messages']==0
    assert result['sync']['history']['last_head_scan_at']
    assert result['sync']['history']['history_traversal_complete']
    with s.pool.connection() as c:
        c.execute('UPDATE accounts SET synthetic=false WHERE id=%s',(w.account,))
    cid=result['chat']['id'];upper=result['sync']['history']['last_head_scan_at']
    lower='2026-01-01T00:00:00Z'
    finite=s.get_sync_status(key,chat_id=cid,from_time=lower,to_time=upper)
    assert finite['history']['requested_range_traversed']
    for bounds in ({'to_time':upper},{'from_time':lower},{}):
        status=s.get_sync_status(key,chat_id=cid,**bounds)
        assert not status['history']['requested_range_traversed']



# spec: ANYM-53
def test_a_filter_that_names_no_visible_chat_points_to_list_chats(seeded):
    s,a,b=seeded
    # An absent and a hidden account read alike, and the next step is a real operation that needs no reference
    # the agent lacks: list_chats shows the accounts and chats the key may search.
    catalog=json.loads((Path(__file__).resolve().parents[1]/'services/gateway/src/agent_gateway/catalog.json').read_text())
    for read in (lambda account:s.search(b,accounts=[account],query='missing'),
                 lambda account:s.search_files(b,accounts=[account],query='missing'),
                 lambda account:s.search_files(b,accounts=[account],query='missing',include_coverage=False)):
        evidence=[read(account)['result_evidence'] for account in ('absent-account','whatsapp-demo-1')]
        assert evidence[0]==evidence[1]
        assert evidence[0]['empty_reason']=='not_found_or_not_permitted'
        assert evidence[0]['next_action']=='list_chats'
    assert any(tool.get('operation')=='list_chats' for tool in catalog)
