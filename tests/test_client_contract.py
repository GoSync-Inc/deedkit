"""Client contract regressions with synthetic data and the isolated test DB."""

import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app


def rpc(client, key, name, arguments):
    return client.post('/mcp', headers={'Authorization': 'Bearer '+key,
        'Accept': 'application/json, text/event-stream'}, json={
        'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
        'params': {'name': name, 'arguments': arguments}})


@pytest.mark.parametrize('name,args,field', [
    ('search', {'language': 'PRIVATE_VALUE'}, 'language'),
    ('search', {'limit': '5'}, 'limit'),
    ('search', {'query_mode': 'bad'}, 'query_mode'),
    ('get_updates', {'consumer': 'invalid-kinds', 'kinds': ['message']}, 'kinds'),
    ('get_updates', {'origins': ['bad']}, 'origins'),
])
def test_invalid_mcp_arguments_are_protocol_errors_before_any_operation(seeded,name,args,field):
    s,key,_=seeded
    with TestClient(create_app(s,['testserver'])) as client:
        response=rpc(client,key,name,args)
    error=response.json()['error']
    assert error['code']==-32602
    assert field in error['data']['fields']
    assert 'PRIVATE_VALUE' not in response.text
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) n FROM consumers').fetchone()['n']==0


def test_rest_uses_same_strict_contract(seeded):
    s,key,_=seeded
    with TestClient(create_app(s,['testserver'])) as client:
        response=client.post('/v1/search',headers={'Authorization':'Bearer '+key},
            json={'language':'PRIVATE_VALUE'})
    assert response.status_code==400
    assert response.json()['error']=='invalid_parameters'
    assert response.json()['fields']==['language']
    assert 'PRIVATE_VALUE' not in response.text


def test_default_compact_wire_and_full_opt_in(seeded):
    s,key,_=seeded
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET body=repeat('long text ',2000) WHERE account='telegram-demo'")
        for n in range(3):
            c.execute("INSERT INTO chats(id,account,title,chat_type) VALUES(%s,'telegram-demo',repeat('длинное название ',20),'private')",(f'compact-chat-{n}',))
            c.execute("INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at) VALUES(%s,'telegram-demo',%s,%s,'person',repeat('длинный текст ',2000),now())",(f'compact-message-{n}',f'compact-chat-{n}',str(n)))
    with TestClient(create_app(s,['testserver'])) as client:
        compact=rpc(client,key,'search',{'limit':5,'view':'archive'})
        full=rpc(client,key,'search',{'limit':5,'detail':'full','view':'archive'})
        chats=rpc(client,key,'list_chats',{'limit':5,'view':'archive'})
    result=compact.json()['result']['structuredContent']
    assert len(compact.content)<8192
    assert len(chats.content)<4096
    assert 'sources' not in result
    assert 'evidence' not in result['items'][0]
    assert result['items'][0]['integrity']['provider_currentness_verified'] is False
    assert 'body' not in result['items'][0]
    assert len(full.json()['result']['structuredContent']['items'][0]['body'])>10000
    assert 'sync' not in chats.json()['result']['structuredContent']['items'][0]


def test_awaiting_reply_is_conservative_and_uses_activity_order(seeded):
    s,key,_=seeded
    with s.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type='private' WHERE id='telegram-demo-chat'")
        c.execute("UPDATE chats SET chat_type='group' WHERE id='whatsapp-demo-1-chat'")
    result=s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0)
    assert result['order']=='activity'
    assert result['awaiting_reply_total']==1
    assert result['items'][0]['last_message_snippet']
    assert all(r['type']=='private' for r in result['items'])


def test_non_audio_segments_explain_empty_result(seeded):
    s,key,_=seeded
    result=s.get_file_segments(key,'demo-file-1')
    assert result['segments']==[]
    assert result['empty_reason']=='unsupported_media_type'
    assert result['next_action']=='get_file_text'


def test_empty_audio_segments_distinguish_missing_configuration(seeded):
    from test_enrichment import add_audio
    s,key,_=seeded
    add_audio(s)
    result=s.get_file_segments(key,'enrich-audio')
    assert result['empty_reason']=='processing_not_configured'
    assert result['enrichment_status']=='not_configured'


def test_filter_reset_invalidates_old_receipts_and_preserves_ack_position(seeded):
    from messaging_service.store import APIError
    s,key,_=seeded
    first=s.get_updates(key,'reset-view',chats=['telegram-demo-chat'],limit=1)
    s.ack_updates(key,'reset-view',first['receipt'],first['ack_version'])
    pending=s.get_updates(key,'reset-view',chats=['telegram-demo-chat'],limit=1)
    reset=s.get_updates(key,'reset-view',reset_filters=True,expected_ack_version=pending['ack_version'])
    assert {r['message'] for r in reset['items']}=={'demo-tg-2','demo-wa-1'}
    assert reset['ack_version']==pending['ack_version']+1
    with pytest.raises(APIError):s.ack_updates(key,'reset-view',pending['receipt'],pending['ack_version'])
    with pytest.raises(APIError):s.get_updates(key,'reset-view',reset_filters=True,expected_ack_version=0)
    assert s.get_updates(key,'reset-view')['ack_version']==reset['ack_version']


def test_filter_reset_needs_cursor_permission(seeded):
    from messaging_service.store import APIError
    s,key,_=seeded
    s.get_updates(key,'reset-read')
    s.set_access('agent-a',permissions=['read'])
    page=s.get_updates(key,'reset-read')
    with pytest.raises(APIError,match='forbidden'):
        s.get_updates(key,'reset-read',reset_filters=True,expected_ack_version=page['ack_version'])


def test_chronological_batch_does_not_skip_ingestion_events(seeded):
    s,key,_=seeded
    with s.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type='channel' WHERE id='whatsapp-demo-1-chat'")
        c.execute("UPDATE events SET snapshot=jsonb_set(snapshot,'{sent_at}','\"2025-01-01T00:00:00Z\"') WHERE message='demo-tg-2'")
        c.execute("UPDATE events SET origin='backfill' WHERE message='demo-tg-2'")
    page=s.get_updates(key,'chronological',limit=2,order='sent_at',exclude_channels=True)
    assert [r['message'] for r in page['items']]==['demo-tg-2','demo-tg-1']
    assert page['items'][0]['is_backfill']
    s.ack_updates(key,'chronological',page['receipt'],page['ack_version'])
    assert s.get_updates(key,'chronological',order='sent_at',exclude_channels=True)['items']==[]
    assert len(s.get_updates(key,'unaffected')['items'])==3


def test_spelling_variants_are_candidates_without_identity_merging(seeded):
    from messaging_service.archive import Archive
    from test_archive import event
    s,_,_=seeded;ar=Archive(s)
    ar.register('name-variants','telegram','Synthetic names',discover=True)
    for n,name in enumerate(['Юля','Юлия','Yulia','Yuliya']):
        ar.ingest(event(account='name-variants',sender=str(40+n),provider_id=str(n+1),
            sender_profile={'display_name':name}))
    key=s.issue_key('name-reader',['name-variants'])
    for query in ('Юля','Yulia','Yuliya'):
        result=s.search_people(key,query,transliterate=True,group_by_sender=True)
        assert len(result['items'])==4
        assert result['names_are_verified_identity'] is False


def test_compact_file_search_does_not_compute_full_file_inventory(seeded,monkeypatch):
    import messaging_service.sync_status as sync
    s,key,_=seeded
    def forbidden(*args,**kwargs):raise AssertionError('unexpected complete file inventory')
    monkeypatch.setattr(sync,'_files',forbidden)
    result=s.search_files(key,'no-match',include_coverage=False)
    assert result['items']==[]
    assert result['result_evidence']['file_index']['full_content_verified'] is False
    assert result['result_evidence']['next_action']


def test_compact_file_summary_keeps_the_matching_excerpt():
    from messaging_service.presentation import file_summary
    result=file_summary({'attachment_id':'synthetic-file','excerpt':'text with [[match]] nearby'})
    assert result.get('snippet')=='text with [[match]] nearby'


def test_compact_message_keeps_match_within_short_utf8_preview():
    from messaging_service.presentation import message_summary
    result=message_summary({'snippet':'я'*200+'[[совпадение]]'+'я'*200})
    assert len(result['snippet'])<=96
    assert '[[совпадение]]' in result['snippet']
