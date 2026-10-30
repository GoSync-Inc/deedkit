"""A clipped transcript never represents a fragment as the whole message."""
from copy import deepcopy

from messaging_service.conversation import presentation
from messaging_service.presentation import render


def row(id='synthetic-message', **extra):
    return dict(id=id, account='synthetic-account', chat='synthetic-chat',
        direction='incoming', position=1, display_text='A clipped sentence', **extra)


# spec: ANYM-647
def test_transcript_marks_database_body_clipping_and_lists_affected_ids():
    rows=[row(body_truncated=True), row('synthetic-complete')]
    view=presentation(rows)
    result=render('get_messages', {'items':rows, 'conversation_view':view,
        'next_cursor':'synthetic-next'}, 'transcript')
    assert 'A clipped sentence […]' in result['transcript']
    assert result['truncated_message_ids']==['synthetic-message']
    assert any(w['code']=='message_text_truncated' for w in result['warnings'])
    assert result['next_cursor']=='synthetic-next'


# spec: ANYM-647
def test_context_transcript_includes_a_hidden_clipped_anchor_without_duplicates():
    anchor=row(body_truncated=True)
    other=row('synthetic-neighbour', display_text_truncated=True)
    result={'message':anchor, 'context':[other], 'attachments':[], 'pending_attachments':[],
        'context_window':{'anchor_hidden':True, 'older_cursor':'synthetic-older',
            'newer_cursor':'synthetic-newer'}, 'conversation_view':presentation([other])}
    before=deepcopy(result)
    output=render('get_context', result, 'transcript')
    assert set(output['truncated_message_ids'])=={'synthetic-message','synthetic-neighbour'}
    assert len(output['truncated_message_ids'])==2
    assert any(w['code']=='message_text_truncated' for w in output['warnings'])
    assert output['context_window']==before['context_window']
    assert result==before


# spec: ANYM-647
def test_complete_transcript_has_no_clipping_warning_or_recovery_claim():
    rows=[row()]
    output=render('search', {'items':rows, 'conversation_view':presentation(rows)}, 'transcript')
    assert not output.get('truncated_message_ids')
    assert not any(w['code']=='message_text_truncated' for w in output['warnings'])
    assert '[…]' not in output['transcript']


# spec: ANYM-647
def test_mcp_transcript_reports_clipping_for_context_page_and_search(seeded):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    from test_client_contract import rpc
    store,key,_=seeded
    with store.pool.connection() as c:
        c.execute("UPDATE messages SET body=repeat('synthetic long text ',100)")
    calls=[('get_context',{'message_id':'demo-tg-1','before_count':0,'after_count':0}),
        ('get_messages',{'chat_id':'telegram-demo-chat'}),('search',{'limit':5})]
    with TestClient(create_app(store,['testserver'])) as client:
        for method,args in calls:
            response=rpc(client,key,method,{**args,'body_chars':40,'detail':'transcript'})
            data=response.json()['result']
            assert not data.get('isError'),data
            output=data['structuredContent']
            assert output['truncated_message_ids']
            assert ' […]' in output['transcript']
            assert any(w['code']=='message_text_truncated' for w in output['warnings'])
            if method=='get_context':assert 'demo-tg-1' in output['truncated_message_ids']
