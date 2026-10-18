"""Compact returns each message text once; transcript returns only the transcript."""
import json

from starlette.testclient import TestClient

from messaging_service.app import create_app
from test_client_contract import rpc


def structured(response):
    body=response.json()
    assert 'error' not in body and not body['result'].get('isError'),body
    return body['result']['structuredContent']


def occurrences(value,text):
    return json.dumps(value,ensure_ascii=False).count(json.dumps(text,ensure_ascii=False)[1:-1])


def test_mcp_context_returns_the_anchor_once_at_each_detail_level(seeded):
    store,key,_=seeded
    with TestClient(create_app(store,['testserver'])) as client:
        compact=structured(rpc(client,key,'get_context',{'message_id':'demo-tg-1','before_count':1,'after_count':1}))
        transcript=structured(rpc(client,key,'get_context',{'message_id':'demo-tg-1','before_count':1,
            'after_count':1,'detail':'transcript'}))
        full=structured(rpc(client,key,'get_context',{'message_id':'demo-tg-1','before_count':1,
            'after_count':1,'detail':'full'}))
    text=compact['message']['display_text']
    assert compact['message']['id']=='demo-tg-1' and 'body' not in compact['message']
    assert 'demo-tg-1' not in [row['id'] for row in compact['context']]
    assert compact['context_window']['anchor_index']==0 and compact['context'][0]['id']=='demo-tg-2'
    assert occurrences(compact,text)==1
    assert 'transcript' not in compact['conversation_view']
    assert 'latest_known_meaningful_message' not in json.dumps(compact)
    # The transcript carries the text once, with message IDs and cursors.
    assert set(transcript)>={'transcript','message_id','context_window','conversation','content_is_untrusted'}
    assert 'message' not in transcript and 'context' not in transcript
    assert 'demo-tg-1' in transcript['transcript'] and occurrences(transcript,text)==1
    assert any(f['id']=='demo-file-1' and f['message_id']=='demo-tg-2' for f in transcript['files'])
    # Full detail is the unchanged evidence contract.
    assert full['message']['body']==text and 'demo-tg-1' in [row['id'] for row in full['context']]
    assert full['conversation_view']['transcript'] and 'latest_known_meaningful_message' in full['conversation']


def test_compact_messages_and_chats_omit_duplicate_text_and_routine_qualifiers(seeded):
    store,key,_=seeded
    with TestClient(create_app(store,['testserver'])) as client:
        page=structured(rpc(client,key,'get_messages',{'chat_id':'telegram-demo-chat'}))
        found=structured(rpc(client,key,'search',{'query':'Маяк','order':'relevance'}))
        chats=structured(rpc(client,key,'list_chats',{}))
        context=structured(rpc(client,key,'get_context',{'message_id':'demo-tg-2'}))
    for row in page['items']+found['items']:
        assert set(row)>={'id','chat','sender','sent_at','direction','kind','display_text'}
        for field in ('body','snippet','position','ordering','integrity','formatting','delivery_receipt',
                'timestamp_resolution','timestamp_precision','conversation_view_version'):
            assert field not in row, field
    assert page['conversation_view']['provider_currentness_verified'] is False
    assert [row['id'] for row in found['items']]==['demo-tg-1'] and found['items'][0]['chat_title']
    file_row=next(row for row in page['items'] if row['id']=='demo-tg-2')
    # Files keep the same id key as full detail and the published clients.
    assert file_row['attachments'][0]['id']=='demo-file-1'
    assert context['attachments'][0]['id']=='demo-file-1'
    for chat in chats['items']:
        assert not any(k.startswith('awaiting_reply_') or k.startswith('last_message_') for k in chat)
        assert 'provider_chat_id' not in chat
        if 'last_message' in chat:
            assert occurrences(chat,chat['last_message']['display_text'])==1
            assert 'latest_message_id' not in chat['conversation']


def test_transcript_needs_the_conversation_view_and_says_so(seeded):
    store,key,_=seeded
    headers={'Authorization':'Bearer '+key}
    with TestClient(create_app(store,['testserver'])) as client:
        archive=client.post('/v1/get_messages',headers=headers,json={'chat_id':'telegram-demo-chat','detail':'transcript'})
        explicit=client.post('/v1/get_messages',headers=headers,json={'chat_id':'telegram-demo-chat',
            'view':'conversation','detail':'transcript','limit':1})
        chats=client.post('/v1/list_chats',headers=headers,json={'detail':'transcript'})
    assert archive.status_code==400
    assert archive.json()['error']=='transcript_requires_conversation_view'
    assert archive.json()['expected'].startswith('view=conversation')
    assert explicit.status_code==200 and explicit.json()['detail']=='transcript'
    assert explicit.json()['transcript'].startswith('1. ') and explicit.json()['next_cursor']
    assert chats.status_code==400
