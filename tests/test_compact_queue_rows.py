"""Compact reply-queue rows state each fact once; list_chats pages 25 rows by default."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.archive import Archive
from test_archive import event
from test_client_contract import rpc

ROOT=Path(__file__).resolve().parents[1]
SNAPSHOT=ROOT/'tests/fixtures/compact-queue-full-snapshot.json'
TITLE='Синтетический чат очереди ответов с очень длинным названием для проверки обрезки 0'
NAME='Синтетический Отправитель С Очень Длинным Именем И Фамилией Для Проверки Бюджета Ответа Очереди'
TEXT=('Синтетический вопрос владельцу о встрече и документах, который заведомо длиннее ста шестидесяти символов, '
    'чтобы проверить обрезку текста в компактной строке очереди?')
# Values that follow the wall clock or ingestion time, not the projection.
VOLATILE={'waiting_hours','awaiting_reply_waiting_hours','as_of','first_observed_at','observed_at','ingested_at',
    'updated_at','created_at','last_ingest_at','provider_head_verified_at','last_head_scan_at','captured_at'}


def structured(response):
    body=response.json()
    assert 'error' not in body and not body['result'].get('isError'),body
    return body['result']['structuredContent']


def build_queue(s, anchor, *, scanned, chats=25):
    """Private, group and ambiguous WhatsApp rows with worst-case compact content."""
    ar=Archive(s)
    ar.register('queue-tg','telegram','Synthetic queue',discover=True)
    ar.register('queue-wa','whatsapp','Synthetic queue',discover=True)
    key=s.issue_key('queue-budget',['queue-tg','queue-wa'])
    def add(account, chat, mid, second, chat_type, direction='incoming', body=TEXT, sender='42', **metadata):
        at=(anchor+timedelta(seconds=second)).isoformat()
        ar.ingest(event(account=account,platform='whatsapp' if account=='queue-wa' else 'telegram',chat=chat,
            chat_title=TITLE[:-1]+chat[-2:],provider_id=mid,sender=sender,sent_at=at,updated_at=at,
            direction=direction,body=body,sender_profile={'display_name':f'{NAME} {sender}','username':f'synthetic_{sender}'},
            metadata={'chat_type':chat_type,**metadata}))
    for n in range(chats):
        chat=f'{900+n}'
        if n%5==4:
            # Ambiguous WhatsApp order: the incoming message ties the owner's reply.
            chat=f'{chat}@s.whatsapp.net'
            add('queue-wa',chat,f'OUT{n}',n*60,'private','outgoing',body=TEXT[:150])
            add('queue-wa',chat,f'IN{n}',n*60,'private',sender=f'7{n:03d}')
        elif n%5 in (1,3):
            # Group attention followed by unrelated chatter: both texts are shown.
            add('queue-tg',chat,'1',n*60,'group',sender=f'5{n:03d}',mentions_owner=True)
            add('queue-tg',chat,'2',n*60+1,'group',sender=f'6{n:03d}')
        else:
            add('queue-tg',chat,'1',n*60,'private','outgoing',body=TEXT[:150])
            add('queue-tg',chat,'2',n*60+1,'private',sender=f'4{n:03d}')
        if scanned(n):
            with s.pool.connection() as c:
                c.execute('''INSERT INTO collector_chats(account,provider_chat,peer_kind,initialized,last_head_scan_at)
                    VALUES(%s,%s,'user',true,%s) ON CONFLICT(account,provider_chat)
                    DO UPDATE SET last_head_scan_at=excluded.last_head_scan_at''',
                    ('queue-wa' if n%5==4 else 'queue-tg',chat,scanned(n)))
    return key


@pytest.fixture()
def recent_queue(seeded):
    s,_,_=seeded
    anchor=datetime.now(timezone.utc).replace(microsecond=0)-timedelta(hours=3)
    key=build_queue(s,anchor,scanned=lambda n:datetime.now(timezone.utc) if n%2 else None)
    return s,key


def compact_queue(s, key, **arguments):
    with TestClient(create_app(s,['testserver'])) as client:
        return structured(rpc(client,key,'list_chats',{'awaiting_reply':True,'order':'priority',
            'detail':'compact','limit':25,**arguments}))


def scalars(value):
    if isinstance(value,dict):
        for item in value.values():yield from scalars(item)
    elif isinstance(value,list):
        for item in value:yield from scalars(item)
    else:yield value


def assert_each_fact_once(row):
    ids=[v for v in scalars(row) if isinstance(v,str) and v.startswith('msg_')]
    assert len(ids)==len(set(ids)),row
    shown={v for key in ('awaiting_reply','last_message') for v in scalars(row.get(key,{})) if isinstance(v,str)}
    repeated={v for v in scalars(row.get('conversation',{})) if isinstance(v,str)}&shown
    assert not repeated,(repeated,row)
    hint=row.get('awaiting_reply',{})
    assert 'sent_at' not in hint or hint['sent_at']!=row.get('last_activity'),row
    conversation=row.get('conversation',{})
    attention=conversation.get('owner_attention',{})
    assert 'suggested_action' not in attention or attention['suggested_action']!=conversation['suggested_action'],row


def test_compact_queue_row_states_each_fact_once(recent_queue):
    s,key=recent_queue
    rows=compact_queue(s,key)['items']
    for row in rows:assert_each_fact_once(row)
    private=[r for r in rows if r['type']=='private' and r['account']=='queue-tg']
    groups=[r for r in rows if r['type']=='group']
    tied=[r for r in rows if r['account']=='queue-wa']
    assert private and groups and tied
    for row in private:
        # The hint is the head: its text appears once, in the hint, with its time in last_activity.
        hint=row['awaiting_reply']
        assert 'last_message' not in row and hint['display_text'].startswith(TEXT[:150]) and hint['truncated']
        assert 'sent_at' not in hint and row['last_activity']
        assert 'latest_message_id' not in row['conversation'] and 'basis' not in row['conversation']
        assert len(hint['sender_name'])==64 and hint['sender_id']
    for row in groups:
        hint,attention=row['awaiting_reply'],row['conversation']['owner_attention']
        assert row['last_message']['id']!=hint['message_id'] and hint['sent_at']
        assert hint['basis']=='owner_mention' and attention=={'last_turn':'peer'}
        if 'confidence' in hint:
            # An unverified head makes the hint uncertain; its code is stated once, in the hint.
            assert 'basis' not in row['conversation'] and row['conversation']['suggested_action']=='unclear'
        else:
            assert row['conversation']['basis']=='explicit_owner_attention'
            assert row['conversation']['suggested_action']=='review_incoming'
        assert 'latest_message_id' not in row['conversation'] and len(row['last_message']['sender_name'])==64
    for row in tied:
        conversation=row['conversation']
        assert row['awaiting_reply']['confidence']=='ambiguous_message_order' and 'basis' not in conversation
        assert conversation['order_status']=='ambiguous' and len(conversation['candidate_message_ids'])==1
    # Agent-facing hint fields keep their names and meanings.
    hint=private[0]['awaiting_reply']
    assert hint['priority']>=100 and 'private_chat' in hint['reasons'] and hint['has_question'] is True
    assert isinstance(hint['waiting_hours'],int) and hint['incoming_count']==1 and hint['revision']==1


def test_compact_rows_without_a_hint_state_owner_attention_once(seeded):
    s,_,_=seeded
    ar=Archive(s)
    ar.register('attention-tg','telegram','Synthetic attention',discover=True)
    key=s.issue_key('attention-agent',['attention-tg'])
    now=datetime.now(timezone.utc).replace(microsecond=0)
    def add(chat, mid, at, **metadata):
        return ar.ingest(event(account='attention-tg',chat=chat,provider_id=mid,sent_at=at.isoformat(),
            updated_at=at.isoformat(),body=f'Synthetic group message {mid}',metadata={'chat_type':'group',**metadata}))['message']
    # Expired attention (older than awaiting_reply_days) that is also the head.
    expired=add('501','1',now-timedelta(days=20),mentions_owner=True)
    # Dismissed attention that is the head.
    dismissed=add('502','1',now-timedelta(hours=1),mentions_owner=True)
    # Expired attention followed by unrelated chatter: the target differs from the head.
    older=add('503','1',now-timedelta(days=21),mentions_owner=True)
    add('503','2',now-timedelta(days=20))
    hint=next(r for r in s.list_chats(key,awaiting_reply=True,view='conversation',include_coverage=False)['items']
        if r['awaiting_reply_message_id']==dismissed)
    s.dismiss_reply(key,hint['id'],dismissed,hint['awaiting_reply_revision'])
    with TestClient(create_app(s,['testserver'])) as client:
        rows={r['last_message']['id']:r for r in structured(rpc(client,key,'list_chats',{}))['items']}
    for row in rows.values():
        assert 'awaiting_reply' not in row
        assert_each_fact_once(row)
    for head in (expired,dismissed):
        attention=rows[head]['conversation']['owner_attention']
        assert 'target_message_id' not in attention and 'suggested_action' not in attention and attention['last_turn']
    chatter=next(r for r in rows.values() if r['last_message']['id'] not in (expired,dismissed))
    assert chatter['conversation']['owner_attention']['target_message_id']==older


def test_list_chats_awaiting_reply_25_rows_within_30000_chars(recent_queue):
    # The owner relaxed the card's 15,000 target on 2026-10-18: 25 worst-case rows
    # measured 29,349 characters after deduplication; 15,000 needs a structural change.
    s,key=recent_queue
    result=compact_queue(s,key)
    assert len(result['items'])==25 and {row['type'] for row in result['items']}=={'private','group'}
    for row in result['items']:assert_each_fact_once(row)
    assert len(json.dumps(result,ensure_ascii=False))<=30000


def test_list_chats_defaults_to_25_rows_with_a_cursor_on_every_surface(seeded):
    s,_,_=seeded
    key=build_queue(s,datetime.now(timezone.utc).replace(microsecond=0)-timedelta(hours=3),scanned=lambda n:None,chats=30)
    headers={'Authorization':'Bearer '+key}
    with TestClient(create_app(s,['testserver'])) as client:
        mcp=structured(rpc(client,key,'list_chats',{}))
        rest=client.post('/v1/list_chats',headers=headers,json={}).json()
        queue=structured(rpc(client,key,'list_chats',{'awaiting_reply':True}))
        store=s.list_chats(key,include_coverage=False)
        for page in (mcp,rest,queue,store):
            assert len(page['items'])==25 and page['next_cursor'],(len(page['items']),page.get('next_cursor'))
        more=structured(rpc(client,key,'list_chats',{'after':mcp['next_cursor']}))
        widest=structured(rpc(client,key,'list_chats',{'limit':100}))
        too_many=rpc(client,key,'list_chats',{'limit':101}).json()
        rest_too_many=client.post('/v1/list_chats',headers=headers,json={'limit':101})
        tools={t['name']:t for t in client.post('/mcp',headers={**headers,'Accept':'application/json, text/event-stream'},
            json={'jsonrpc':'2.0','id':1,'method':'tools/list'}).json()['result']['tools']}
    assert len(more['items'])==5 and not more['next_cursor']
    assert len(widest['items'])==30 and not widest['next_cursor']
    assert too_many['result']['isError'] and 'invalid_limit' in json.dumps(too_many)
    assert rest_too_many.status_code==400 and rest_too_many.json()['error']=='invalid_limit'
    catalog=json.loads((ROOT/'services/gateway/src/agent_gateway/catalog.json').read_text())
    gateway=next(t for t in catalog if t['name']=='messages_list_chats')
    assert gateway['inputSchema']['properties']['limit']['default']==25
    assert tools['list_chats']['inputSchema']['properties']['limit']['default']==25
    assert gateway['description']==tools['list_chats']['description']


def masked(value):
    if isinstance(value,dict):return {k:'<volatile>' if k in VOLATILE else masked(v) for k,v in value.items()}
    if isinstance(value,list):return [masked(v) for v in value]
    return value


def test_full_detail_queue_rows_match_the_pre_change_snapshot(seeded):
    s,_,_=seeded
    # A fixed past anchor keeps priority capped and head freshness stale on every run.
    anchor=datetime(2026,1,15,9,0,tzinfo=timezone.utc)
    key=build_queue(s,anchor,scanned=lambda n:anchor+timedelta(hours=1) if n%2 else None)
    full=compact_queue(s,key,detail='full',awaiting_reply_days=0)
    assert len(full['items'])==25 and 'detail' not in full
    assert masked(full)==json.loads(SNAPSHOT.read_text())
