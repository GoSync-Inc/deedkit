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
        hint=row['awaiting_reply']
        assert row['last_message']['id']!=hint['message_id'] and hint['sent_at']
        assert hint['basis']=='owner_mention' and 'owner_attention' not in row['conversation']
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
    assert hint['priority']>=100 and 'private_chat' in hint['reasons'] and 'question_mark' in hint['reasons']
    assert isinstance(hint['waiting_hours'],int) and hint['incoming_count']==1 and hint['revision']==1


def test_compact_rows_without_a_hint_state_owner_attention_once(seeded):
    s,_,_=seeded
    ar=Archive(s)
    ar.register('attention-tg','telegram','Synthetic attention',discover=True)
    ar.register('attention-wa','whatsapp','Synthetic attention',discover=True)
    key=s.issue_key('attention-agent',['attention-tg','attention-wa'])
    now=datetime.now(timezone.utc).replace(microsecond=0)
    def add(chat, mid, at, account='attention-tg', **metadata):
        return ar.ingest(event(account=account,platform=account.split('-')[1].replace('tg','telegram').replace('wa','whatsapp'),
            chat=chat,provider_id=mid,sent_at=at.isoformat(),updated_at=at.isoformat(),
            body=f'Synthetic group message {mid}',metadata={'chat_type':'group',**metadata}))['message']
    # Expired attention (older than awaiting_reply_days) that is also the head.
    expired=add('501','1',now-timedelta(days=20),mentions_owner=True)
    # Dismissed attention that is the head.
    dismissed=add('502','1',now-timedelta(hours=1),mentions_owner=True)
    # Expired attention followed by unrelated chatter: the target differs from the head.
    older=add('503','1',now-timedelta(days=21),mentions_owner=True)
    add('503','2',now-timedelta(days=20))
    # Expired WhatsApp attention tied with other chatter: no single head is shown.
    tied=add('504@g.us','M',now-timedelta(days=20),account='attention-wa',mentions_owner=True)
    other=add('504@g.us','X',now-timedelta(days=20),account='attention-wa')
    hint=next(r for r in s.list_chats(key,awaiting_reply=True,view='conversation',include_coverage=False)['items']
        if r['awaiting_reply_message_id']==dismissed)
    s.dismiss_reply(key,hint['id'],dismissed,hint['awaiting_reply_revision'])
    with TestClient(create_app(s,['testserver'])) as client:
        items=structured(rpc(client,key,'list_chats',{}))['items']
    assert len(items)==4
    for row in items:
        assert 'awaiting_reply' not in row
        assert_each_fact_once(row)
    rows={r['last_message']['id']:r for r in items if 'last_message' in r}
    for head in (expired,dismissed):
        attention=rows[head]['conversation']['owner_attention']
        assert 'target_message_id' not in attention and 'suggested_action' not in attention and attention['last_turn']
    chatter=next(r for r in rows.values() if r['last_message']['id'] not in (expired,dismissed))
    assert chatter['conversation']['owner_attention']['target_message_id']==older
    tie=next(r for r in items if 'last_message' not in r)['conversation']
    # The target is stated once, in owner_attention; the group's only candidate is that target.
    assert tie['order_status']=='ambiguous' and tie['owner_attention']['target_message_id']==tied
    assert 'candidate_message_ids' not in tie and other not in json.dumps(tie)


def test_list_chats_awaiting_reply_25_rows_within_30000_chars(recent_queue):
    # The owner relaxed the card's 15,000 target on 2026-10-18: 25 worst-case rows
    # measured 29,349 characters after deduplication; 15,000 needs a structural change.
    s,key=recent_queue
    result=compact_queue(s,key)
    assert len(result['items'])==25 and {row['type'] for row in result['items']}=={'private','group'}
    for row in result['items']:assert_each_fact_once(row)
    assert len(json.dumps(result,ensure_ascii=False))<=30000


def test_list_chats_25_forwarded_hints_within_39000_chars(recent_queue):
    # ANYM-274: every incoming message forwarded with a worst-case origin; queue rows keep the kind and a
    # 64-character name of quotes, the widest name character once control characters become U+FFFD
    # (test_compact_origin_names_never_serialize_as_control_escapes). The 30,000 guard above keeps its fixture
    # without forwards (decision 2026-10-18, option A). Measured 37,294 characters.
    from psycopg.types.json import Jsonb
    s,key=recent_queue
    origin={'kind':'hidden_user','sender_id':'9'*160,'sender_name':'"'*256,'chat_id':'-'*160,
        'provider_message_id':'1'*256,'post_author':'"'*256,'sent_at':'2026-09-01T00:00:00+00:00',
        'forwarding_score':2**32-1,'provider_origin_type':'META_AI','basis':'whatsapp_web_js_message'}
    with s.pool.connection() as c:
        incoming=[r['id'] for r in c.execute("SELECT id FROM messages WHERE direction='incoming'").fetchall()]
        c.execute('UPDATE message_metadata SET metadata=metadata||%s WHERE message=ANY(%s)',
            (Jsonb({'forwarded':True,'forward_origin':origin}),incoming))
    result=compact_queue(s,key)
    hints=[row['awaiting_reply'] for row in result['items'] if 'awaiting_reply' in row]
    assert len(result['items'])==25 and len(hints)==25
    assert all(h['authorship']=='forwarded_by_sender' and h['forward_origin']=={'kind':'hidden_user','sender_name':'"'*64}
        for h in hints)
    for row in result['items']:assert_each_fact_once(row)
    size=len(json.dumps(result,ensure_ascii=False))
    assert size<=39000


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


TG_OWNER,WA_OWNER='4242','48000000000001@c.us'
OWNER_NAMES={'fixed-tg':'Synthetic Owner','fixed-wa':'Synthetic Owner WA','queue-tg':f'{NAME} 42','queue-wa':f'{NAME} 42'}
OWNER_IDS={TG_OWNER,WA_OWNER,'48000000000001@lid','42'}


def build_default_list(s, now):
    """One row of each kind a default list_chats page shows (ANYM-408 criterion 1)."""
    ar=Archive(s)
    ar.register('fixed-tg','telegram','Synthetic fixed text',discover=True)
    ar.register('fixed-wa','whatsapp','Synthetic fixed text',discover=True)
    key=s.issue_key('fixed-text',['fixed-tg','fixed-wa'])
    def add(account, chat, mid, minutes, chat_type, direction='incoming', sender='61', name='Synthetic Peer',
            body='Synthetic text', **metadata):
        if direction=='outgoing' and sender=='61':sender=TG_OWNER if account=='fixed-tg' else WA_OWNER
        if direction=='outgoing' and name=='Synthetic Peer':name=OWNER_NAMES[account]
        at=(now-timedelta(minutes=minutes)).isoformat()
        ar.ingest(event(account=account,platform='telegram' if account=='fixed-tg' else 'whatsapp',chat=chat,
            chat_title='Synthetic '+chat,provider_id=mid,sender=sender,sent_at=at,updated_at=at,direction=direction,
            body=body,sender_profile={'display_name':name,'username':''},
            metadata={**({'chat_type':chat_type} if chat_type else {}),**metadata}))
    def scan(account, chat, minutes):
        with s.pool.connection() as c:
            c.execute('''INSERT INTO collector_chats(account,provider_chat,peer_kind,initialized,last_head_scan_at)
                VALUES(%s,%s,'user',true,%s)''',(account,chat,now-timedelta(minutes=minutes)))
    def self_chat(account, chat):
        from messaging_service.envelope import stable_id
        with s.pool.connection() as c:
            c.execute('''INSERT INTO chat_read_observations(chat,account,observed_at,basis,is_self_chat)
                VALUES(%s,%s,clock_timestamp(),%s,true)''',
                (stable_id('chat',account,chat),account,'telegram_dialog' if account=='fixed-tg' else 'whatsapp_cached_chat'))
    # Channels: an outgoing post signed as the channel (stale head), an incoming post (recently verified head).
    add('fixed-tg','-1001','1',60,'channel','outgoing',sender='-1001',name='Synthetic Channel')
    scan('fixed-tg','-1001',30)
    add('fixed-tg','-1002','1',59,'channel',sender='-1002',name='Synthetic Other Channel')
    scan('fixed-tg','-1002',0)
    # Self chats: Telegram and WhatsApp, known to the state; a WhatsApp one known only from message metadata.
    add('fixed-tg',TG_OWNER,'1',58,'private','outgoing',is_self_chat=True)
    self_chat('fixed-tg',TG_OWNER);scan('fixed-tg',TG_OWNER,30)
    add('fixed-wa','48000000000001@lid','true_SELF1',57,'private','outgoing',sender='48000000000001@lid')
    self_chat('fixed-wa','48000000000001@lid')
    add('fixed-wa',WA_OWNER,'true_SELF2',56,'private','outgoing',is_self_chat=True)
    # Groups: no owner attention (stale), an attention hint (recently verified), an attention tied with the owner.
    add('fixed-tg','-200','1',55,'group')
    scan('fixed-tg','-200',30)
    add('fixed-tg','-201','1',54,'group',sender='62',mentions_owner=True)
    scan('fixed-tg','-201',0)
    # An outgoing message signed as another chat whose name is unknown keeps that chat's ID.
    add('fixed-tg','-202','1',53,'group','outgoing',sender='-1003',name='')
    add('fixed-wa','300@g.us','true_OUT',53,'group','outgoing')
    add('fixed-wa','300@g.us','false_IN',53,'group',sender='63@c.us',mentions_owner=True)
    # Private: owner last (stale), a question hint (stale), a hint (recently verified), an owner/peer tie.
    add('fixed-tg','5001','1',52,'private',sender='5001')
    add('fixed-tg','5001','2',51,'private','outgoing')
    scan('fixed-tg','5001',30)
    add('fixed-tg','5002','1',50,'private','outgoing')
    add('fixed-tg','5002','2',49,'private',sender='5002',body='Synthetic question?')
    scan('fixed-tg','5002',30)
    add('fixed-tg','5003','1',48,'private','outgoing')
    add('fixed-tg','5003','2',47,'private',sender='5003')
    scan('fixed-tg','5003',0)
    add('fixed-wa','5004@c.us','true_OUT',46,'private','outgoing')
    add('fixed-wa','5004@c.us','false_IN',46,'private',sender='5004@c.us')
    # Unknown type, marked a self chat by the read observation, with the owner's head (stale).
    add('fixed-tg','7001','1',45,None,'outgoing')
    self_chat('fixed-tg','7001');scan('fixed-tg','7001',30)
    return key


def restore(row):
    """CONVERSATION.md "Compact queue rows", each omission rule applied in reverse."""
    row=json.loads(json.dumps(row))
    conversation,hint,kind=row.get('conversation',{}),row.get('awaiting_reply'),row.get('type')
    if kind is None:return row
    conversation.setdefault('head_freshness','stale')
    if (kind=='channel' or kind=='private' and row.get('title_basis')=='self_chat') and 'suggested_action' not in conversation:
        conversation.update(last_turn='not_applicable',suggested_action='not_applicable',basis='channel_or_self_chat')
    elif kind=='group':
        conversation.setdefault('last_turn','not_applicable')
        if hint:conversation.setdefault('owner_attention',{'last_turn':'peer'})
        elif 'suggested_action' not in conversation:
            conversation.update(suggested_action='not_applicable',basis='no_observed_owner_attention',
                owner_attention={'last_turn':'not_applicable'})
    elif kind=='private':
        if 'last_turn' not in conversation:
            head=row.get('last_message')
            conversation['last_turn']={'outgoing':'owner','incoming':'peer'}[head['direction']] if head else 'peer'
        if hint:hint.setdefault('basis','last_archived_message_incoming')
    if hint:
        if 'question_mark' in hint.get('reasons',[]):hint['has_question']=True
        conversation.setdefault('basis',hint.get('confidence') or hint['basis'])
    head=row.get('last_message')
    if head and head.get('direction')=='outgoing' and 'sender_name' not in head and 'sender_id' not in head:
        head['sender_name']=OWNER_NAMES[row['account']][:64]
    return row


def assert_restores_full(compact, full):
    """Every value compact omits is restored by its rule to the matching detail=full value."""
    for row in compact:
        source=full[row['id']]
        restored,state=restore(row),source['conversation']
        conversation=restored['conversation']
        assert {k:conversation[k] for k in ('last_turn','suggested_action','basis','head_freshness')}=={
            'last_turn':state['last_turn'],'suggested_action':state['suggested_action'],
            'basis':state['basis']['code'],'head_freshness':state['head_freshness']},(row,state)
        if state.get('owner_attention'):
            assert conversation['owner_attention']['last_turn']==state['owner_attention']['last_turn'],row
        else:assert 'owner_attention' not in conversation,row
        hint=restored.get('awaiting_reply')
        assert bool(hint)==bool(source.get('awaiting_reply')),row
        if hint:
            assert hint.get('has_question',False) is source['awaiting_reply_has_question'] is True or (
                'has_question' not in hint and source['awaiting_reply_has_question'] is not True),row
            assert hint['basis']==source['awaiting_reply_basis'],row
        latest=state['latest_known_meaningful_message']
        if 'last_message' in restored:
            assert restored['last_message'].get('sender_name','')==(latest['sender_name'] or '')[:64],row
            if 'sender_name' not in row['last_message'] and latest['direction']=='outgoing':
                # Absent name and ID: the owner; absent name with an ID: that sender, name unknown.
                assert (latest['sender'] in OWNER_IDS)==('sender_id' not in row['last_message']),row
                assert row['last_message'].get('sender_id',latest['sender'])==latest['sender'],row


def assert_no_fixed_text(compact, full):
    """Compact rows carry none of the values ANYM-408 rules C1-C9 name, under their conditions."""
    for row in compact:
        source=full[row['id']]
        conversation,hint,kind=row['conversation'],row.get('awaiting_reply'),row.get('type')
        latest=source['conversation']['latest_known_meaningful_message']
        if kind is None:continue
        assert conversation.get('head_freshness')!='stale',row                                      # C9
        if kind=='channel' or kind=='private' and row.get('title_basis')=='self_chat':
            assert conversation.get('last_turn')!='not_applicable' and conversation.get('basis')!='channel_or_self_chat' \
                and conversation.get('suggested_action')!='not_applicable',row                      # C1
        if kind=='group':
            assert 'last_turn' not in conversation,row                                               # C2
            if hint:assert conversation.get('owner_attention')!={'last_turn':'peer'},row             # C4
            else:assert conversation.get('owner_attention')!={'last_turn':'not_applicable'} and \
                conversation.get('basis')!='no_observed_owner_attention',row                         # C3
        elif kind=='private':
            shown=row['last_message']['direction'] if 'last_message' in row else \
                'incoming' if hint and latest and latest['id']==hint['message_id'] else None
            assert conversation.get('last_turn')!={'outgoing':'owner','incoming':'peer'}.get(shown),row  # C5
            if hint:assert 'basis' not in hint,row                                                   # C7
        if hint:assert 'has_question' not in hint,row                                                # C6
        head=row.get('last_message')
        if head and latest['direction']=='outgoing' and latest['sender'] in OWNER_IDS:
            assert 'sender_name' not in head and 'sender_name_basis' not in head,row                 # C8


def both_details(s, key, **arguments):
    with TestClient(create_app(s,['testserver'])) as client:
        compact,full=(structured(rpc(client,key,'list_chats',{**arguments,'detail':detail})) for detail in ('compact','full'))
    assert [r['id'] for r in compact['items']]==[r['id'] for r in full['items']]
    return compact['items'],{r['id']:r for r in full['items']}


# spec: ANYM-408
def test_compact_rows_omit_values_fixed_by_the_row(seeded, recent_queue):
    s,queue_key=recent_queue
    queue,queue_full=both_details(s,queue_key,awaiting_reply=True,order='priority',limit=25)
    assert len(queue)==25
    key=build_default_list(s,datetime.now(timezone.utc).replace(microsecond=0))
    rows,full=both_details(s,key)
    assert len(rows)==14
    for compact,source in ((queue,queue_full),(rows,full)):
        assert_no_fixed_text(compact,source)
        assert_restores_full(compact,source)
    by={(r['account'],full[r['id']]['provider_chat_id']):r for r in rows}
    # Rows outside a rule's condition keep the field.
    signed=by['fixed-tg','-1001']
    assert signed['last_message']['sender_name']=='Synthetic Channel' and 'last_turn' not in signed['conversation']
    assert by['fixed-tg','-1002']['conversation']['head_freshness']=='recently_verified'
    assert by['fixed-wa','48000000000001@lid']['conversation']['head_freshness']=='unknown'
    labelled=by['fixed-wa',WA_OWNER]
    # A self-chat row whose state is not fixed keeps C1's triple, and C5 then applies to it.
    assert labelled['title_basis']=='self_chat' and 'last_turn' not in labelled['conversation']
    assert labelled['conversation']['suggested_action']=='unclear' and 'sender_name' not in labelled['last_message']
    unnamed=by['fixed-tg','-202']['last_message']
    assert 'sender_name' not in unnamed and unnamed['sender_id']=='-1003'
    assert 'sender_id' not in by['fixed-tg','5001']['last_message'] and 'sender_id' not in signed['last_message']
    tied_group=by['fixed-wa','300@g.us']['conversation']
    assert tied_group['owner_attention']=={'last_turn':'unknown'} and tied_group['suggested_action']=='unclear'
    attention=by['fixed-tg','-201']['conversation']
    assert attention['suggested_action']=='review_incoming' and attention['basis']=='explicit_owner_attention'
    assert by['fixed-wa','5004@c.us']['conversation']['last_turn']=='unknown'
    assert by['fixed-tg','5003']['conversation']['head_freshness']=='recently_verified'
    unknown=by['fixed-tg','7001']
    assert 'type' not in unknown and unknown['last_message']['sender_name']=='Synthetic Owner'
    assert {k:unknown['conversation'][k] for k in ('last_turn','suggested_action','basis','head_freshness')}=={
        'last_turn':'not_applicable','suggested_action':'not_applicable','basis':'channel_or_self_chat',
        'head_freshness':'stale'}
    # And the rules did apply where their conditions hold.
    quiet=by['fixed-tg','-200']['conversation']
    assert not {'last_turn','suggested_action','basis','owner_attention','head_freshness'}&set(quiet),quiet
    assert not {'last_turn','suggested_action','basis','head_freshness'}&set(by['fixed-tg',TG_OWNER]['conversation'])
    question=by['fixed-tg','5002']
    assert 'question_mark' in question['awaiting_reply']['reasons'] and 'last_turn' not in question['conversation']
    assert 'sender_name' not in by['fixed-tg','5001']['last_message']


LEGEND=('last_turn','suggested_action','basis','owner_attention','question_mark','last_archived_message_incoming',
    'sender_name','head_freshness','stale','confidence')


# spec: ANYM-408
def test_list_chats_description_explains_compact_omissions(seeded):
    s,key,_=seeded
    with TestClient(create_app(s,['testserver'])) as client:
        tools={t['name']:t for t in client.post('/mcp',headers={'Authorization':'Bearer '+key,
            'Accept':'application/json, text/event-stream'},
            json={'jsonrpc':'2.0','id':1,'method':'tools/list'}).json()['result']['tools']}
    description=tools['list_chats']['description']
    catalog=json.loads((ROOT/'services/gateway/src/agent_gateway/catalog.json').read_text())
    assert next(t for t in catalog if t['name']=='messages_list_chats')['description']==description
    legend=description[description.index('Compact rows omit'):].split('limit defaults')[0]
    sentences=[x.strip() for x in legend.replace('\n',' ').split('. ') if x.strip()]
    assert all(len(x)<=160 for x in sentences),[x for x in sentences if len(x)>160]
    for term in LEGEND:assert any(term in x for x in sentences),term
    # One sentence per omission: absent last_turn, the group/channel pair, the group hint, question, private hint
    # basis, the owner's head, stale freshness and conversation.basis beside a hint.
    for rule in ('last_turn','channel_or_self_chat','no_observed_owner_attention',"owner_attention",'question_mark',
            'last_archived_message_incoming','outgoing','stale','beside a hint'):
        assert sum(rule in x for x in sentences)>=1,rule


# spec: ANYM-408
def test_rest_list_chats_keeps_archive_rows_and_mirrors_mcp_in_conversation_view(seeded, recent_queue):
    """REST in its default archive view keeps every hint field; an explicit conversation view equals MCP (ANYM-23)."""
    s,key=recent_queue
    headers={'Authorization':'Bearer '+key}
    arguments={'awaiting_reply':True,'order':'priority','limit':25}
    with TestClient(create_app(s,['testserver'])) as client:
        archive=client.post('/v1/list_chats',headers=headers,json=arguments).json()
        rest=client.post('/v1/list_chats',headers=headers,json={**arguments,'view':'conversation'}).json()
        mcp=structured(rpc(client,key,'list_chats',arguments))
    assert archive['items'] and all('conversation' not in r for r in archive['items'])
    assert any(r.get('awaiting_reply_has_question') for r in archive['items'])
    assert {r['awaiting_reply_basis'] for r in archive['items'] if r['type']=='private'}=={'last_archived_message_incoming'}
    assert masked(rest)==masked(mcp)
