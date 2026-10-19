"""Synthetic regressions for ranked identities and bounded archive query paths."""
from concurrent.futures import ThreadPoolExecutor
from time import monotonic

import pytest

from messaging_service.archive import Archive
from messaging_service.chat_queries import list_chats
from messaging_service.message_queries import search
from messaging_service.people_candidates import search_people
from messaging_service.store import APIError
from messaging_service.update_queries import get_updates
from test_archive import event
from test_search_performance import visited,scans


def authorized(s,key,operation,fn,*args,**kwargs):
    with s.authorized(key,operation) as (c,p):return fn(s,c,p,*args,**kwargs)


def ranked_directory(seeded):
    s,_,_=seeded;ar=Archive(s)
    ar.register('rank-tg','telegram','Synthetic Telegram names',discover=True)
    ar.register('rank-wa','whatsapp','Synthetic WhatsApp names',discover=True)
    entries=[('rank-tg','telegram','a-substring','Sonyaguseva','sonyaguseva'),
        ('rank-tg','telegram','b-substring','Ngurah Baguss','ngurah_baguss'),
        ('rank-wa','whatsapp','z-full-token','Agus Dandri','')]
    for account,platform,sender,name,username in entries:
        ar.ingest(event(account=account,platform=platform,sender=sender,provider_id=sender,
            sender_profile={'display_name':name,'username':username}))
    with s.pool.connection() as c:
        c.execute("UPDATE sender_profiles SET id=sender WHERE account IN ('rank-tg','rank-wa')")
    return s,ar,s.issue_key('rank-agent',['rank-tg','rank-wa'])


@pytest.mark.parametrize('grouped',[False,True])
def test_people_full_transliterated_token_precedes_literal_substring_and_pages(seeded,grouped):
    s,ar,key=ranked_directory(seeded)
    call=lambda **kw:authorized(s,key,'search_people',search_people,'Агус',
        transliterate=True,group_by_sender=grouped,**kw)
    page=call(limit=1)
    assert page['items'][0]['sender_id']=='z-full-token'
    assert page['next_cursor'].startswith('q1.')
    seen=[page['items'][0]['sender_id']]
    while page['next_cursor']:
        page=call(limit=1,after=page['next_cursor'])
        seen.extend(row['sender_id'] for row in page['items'])
    assert seen==['z-full-token','a-substring','b-substring']
    cursor=call(limit=1)['next_cursor']
    with pytest.raises(APIError,match='invalid_cursor'):
        authorized(s,key,'search_people',search_people,'Sony',transliterate=True,
            group_by_sender=grouped,after=cursor)
    visible=s.search(key,account='rank-tg')['items'][0]['chat']
    s.set_access('rank-agent',chats=[visible])
    assert all(row['account']=='rank-tg' for row in call()['items'])


def test_people_exact_literal_identity_precedes_fragments_without_transliteration(seeded):
    s,ar,key=ranked_directory(seeded)
    ar.ingest(event(account='rank-tg',sender='zz-exact',provider_id='18',
        sender_profile={'display_name':'Agus','username':'agus'}))
    rows=authorized(s,key,'search_people',search_people,'agus')['items']
    assert [r['sender_id'] for r in rows][:2]==['zz-exact','z-full-token']


def test_message_channel_exclusion_precedes_fuzzy_fallback_and_binds_cursor(seeded):
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type='channel' WHERE id='telegram-demo-chat'")
        c.execute("UPDATE messages SET body='needleword' WHERE id='demo-tg-1'")
        c.execute("UPDATE messages SET body='needleworb' WHERE id='demo-wa-1'")
    result=authorized(s,a,'search',search,'needleword',fuzzy=True,exclude_channels=True)
    assert [r['id'] for r in result['items']]==['demo-wa-1']
    assert result['search']['fuzzy_used'] is True
    with s.pool.connection() as c:c.execute("UPDATE chats SET chat_type='private'")
    page=authorized(s,a,'search',search,exclude_channels=True,limit=1)
    assert page['next_cursor'].startswith('q1.')
    with pytest.raises(APIError,match='invalid_cursor'):
        authorized(s,a,'search',search,exclude_channels=False,after=page['next_cursor'])
    with pytest.raises(APIError,match='invalid_filter'):
        authorized(s,a,'search',search,exclude_channels=1)


@pytest.mark.parametrize('stable_ids',[False,True],ids=['short-id','stable-id'])
@pytest.mark.parametrize('order',['asc','desc'])
def test_archive_channel_exclusion_cursor_keeps_exclusive_before_boundary(seeded,stable_ids,order):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    from messaging_service.envelope import stable_id
    from test_conversation_queries import add
    s,a,b=seeded
    ids=sorted(stable_id('msg','synthetic-cursor',str(n)) if stable_ids else 'cursor-'+str(n)
        for n in range(4))
    with s.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type='private' WHERE id='telegram-demo-chat'")
        for n,mid in enumerate(ids):add(c,mid,str(100+n),body='paginationfixture')
    options={'query':'paginationfixture','view':'archive','exclude_channels':True,'order':order}
    first=s.search(b,**options,limit=2)
    cursor=first['next_cursor']
    assert cursor.startswith('q1.')
    assert (len(cursor)>256) is stable_ids
    anchor=ids[1] if order=='asc' else ids[2]
    expected_before=sorted([mid for mid in ids if mid<anchor],reverse=order=='desc')
    expected_after=[mid for mid in ids if mid>anchor] if order=='asc' else expected_before
    with TestClient(create_app(s,['testserver'])) as client:
        headers={'Authorization':'Bearer '+b,'Accept':'application/json, text/event-stream'}
        for boundary,expected in [('before',expected_before),('after',expected_after)]:
            params={**options,boundary:cursor}
            assert [row['id'] for row in s.search(b,**params)['items']]==expected
            rest=client.post('/v1/search',headers=headers,json=params)
            assert rest.status_code==200,rest.text
            assert [row['id'] for row in rest.json()['items']]==expected
            rpc=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call',
                'params':{'name':'search','arguments':params}}).json()
            assert 'error' not in rpc and not rpc['result'].get('isError'),rpc
            assert [row['id'] for row in rpc['result']['structuredContent']['items']]==expected


def test_new_update_consumer_can_start_now_but_existing_receipts_survive_rejection(seeded):
    s,a,b=seeded
    page=authorized(s,a,'get_updates',get_updates,'new-now',start_from='now')
    assert page['items']==[] and page['ack_version']==0
    assert len(s.get_updates(a,'legacy-start')['items'])==3
    with pytest.raises(APIError,match='consumer_already_initialized'):
        authorized(s,a,'get_updates',get_updates,'new-now',start_from='now')
    assert s.ack_updates(a,'new-now',page['receipt'],0)['acked']
    with s.pool.connection() as c:s.append_event(c,'telegram-demo','demo-tg-1','message.edited')
    assert [e['kind'] for e in s.get_updates(a,'new-now')['items']]==['message.edited']
    readonly=s.issue_key('now-readonly',['telegram-demo'],permissions=['read'])
    with pytest.raises(APIError,match='forbidden'):
        authorized(s,readonly,'get_updates',get_updates,'no-skip',start_from='now')
    with pytest.raises(APIError,match='invalid_filter'):
        authorized(s,a,'get_updates',get_updates,'invalid',start_from='yesterday')


def test_start_now_does_not_skip_an_uncommitted_event_sequence(seeded):
    s,a,b=seeded
    with s.pool.connection() as writer:
        eid=s.append_event(writer,'telegram-demo','demo-tg-1','message.created')
        with ThreadPoolExecutor(1) as pool:
            page=pool.submit(authorized,s,a,'get_updates',get_updates,'during-write',start_from='now').result(timeout=4)
        assert page['items']==[]
    assert [e['event_sequence'] for e in s.get_updates(a,'during-write')['items']]==[eid]


def test_chat_tail_keeps_numeric_telegram_and_opaque_whatsapp_ties(seeded):
    from test_conversation_queries import add
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type='private'")
        for account,prefix in [('telegram-demo','tg'),('whatsapp-demo-1','wa')]:
            add(c,prefix+'-z-first','9',account=account)
            add(c,prefix+'-a-second','10',account=account,direction='outgoing')
    rows={row['account']:row for row in s.list_chats(a,include_coverage=False)['items']}
    assert rows['telegram-demo']['last_message_id']=='tg-a-second'
    assert rows['telegram-demo']['awaiting_reply'] is False
    assert rows['whatsapp-demo-1']['last_message_id']=='wa-z-first'
    assert rows['whatsapp-demo-1']['awaiting_reply_confidence']=='ambiguous_message_order'


def test_telegram_nonnumeric_and_zero_fallback_keeps_id_tie_breaker(seeded):
    from test_conversation_queries import add
    s,a,b=seeded
    with s.pool.connection() as c:
        add(c,'a-zero','0')
        add(c,'z-opaque','opaque-fixture')
    row=s.list_chats(b,include_coverage=False)['items'][0]
    assert row['last_message_id']=='z-opaque'
    state=s.get_context(b,'z-opaque',view='conversation')['conversation']
    assert state['latest_known_meaningful_message'] is None


class ChatMeasured:
    def __init__(self,c):self.connection=c;self.plans=[]
    def execute(self,sql,args=None):
        if sql.startswith('SELECT c.id') or sql.startswith('WITH chat_page'):
            self.plans.append(self.connection.execute('EXPLAIN (ANALYZE,FORMAT JSON) '+sql,args)
                .fetchone()['QUERY PLAN'][0])
        return self.connection.execute(sql,args)
    def __getattr__(self,name):return getattr(self.connection,name)


class GroupMeasured(ChatMeasured):
    def execute(self,sql,args=None):
        # Count every message query, including the independent conversation
        # head and exact incoming count, so moving work cannot hide a scan.
        if 'messages ' in sql:
            self.plans.append(self.connection.execute('EXPLAIN (ANALYZE,TIMING OFF,FORMAT JSON) '+sql,args)
                .fetchone()['QUERY PLAN'][0])
        return self.connection.execute(sql,args)


def reply_head_visits(node):
    own=(node.get('Actual Rows',0)+node.get('Rows Removed by Filter',0))*node.get('Actual Loops',1)
    return (own if node.get('CTE Name')=='reply_heads' else 0)+sum(
        reply_head_visits(child) for child in node.get('Plans',[]))


@pytest.mark.parametrize('dense,replies',[(False,False),(True,False),(True,True)],
    ids=['old-mention','dense-mentions','dense-unrelated-replies'])
def test_group_attention_uses_explicit_candidates_without_rescanning_broadcasts(seeded,record_property,dense,replies,tmp_path):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        c.execute("UPDATE chats SET chat_type='group' WHERE id='telegram-demo-chat'")
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'group-load-'||n,'telegram-demo','telegram-demo-chat',n::text,'fixture',
                'Synthetic ordinary group broadcast',
                CASE WHEN n%2=0 THEN now()-interval '6 hours' ELSE now()-interval '3 hours' END+n*interval '1 second',
                CASE WHEN n%2=0 THEN 'outgoing' ELSE 'incoming' END
            FROM generate_series(1,10000) n''')
        c.execute('''INSERT INTO message_metadata(message,revision,metadata)
            SELECT 'group-load-'||n,1,'{"mentions_owner":true}'::jsonb
            FROM generate_series(1,%s,2) n''',[10000 if dense else 1])
        if replies:
            c.execute('''INSERT INTO message_metadata(message,revision,metadata)
                SELECT 'group-load-'||n,1,jsonb_build_object('reply_to_provider_id','unrelated-thread-'||n)
                FROM generate_series(2,10000,2) n''')
        # Collectors retain metadata for ordinary messages too. A sparse
        # metadata fixture would hide unscoped full-table materialization.
        c.execute('''INSERT INTO message_metadata(message,revision,metadata)
            SELECT 'group-load-'||n,1,'{"timestamp_resolution":"second"}'::jsonb
            FROM generate_series(1,10000) n ON CONFLICT(message) DO NOTHING''')
        c.execute('ANALYZE messages');c.execute('ANALYZE message_metadata')
    baseline=None
    if not dense:
        with s.authorized(b,'list_chats') as (c,p):
            measured=GroupMeasured(c)
            list_chats(s,measured,p,limit=5,view='conversation',include_coverage=False)
        baseline={relation:sum(visited(plan['Plan'],{relation}) for plan in measured.plans)
            for relation in ('messages','message_metadata')}
        record_property('target_only_rows_visited',str(baseline))
        with s.pool.connection() as c:
            c.execute("SET LOCAL statement_timeout='60s'")
            c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
                SELECT 'foreign-group-'||n,'whatsapp-demo-1','whatsapp-demo-1-chat',n::text,'other-fixture',
                    'Synthetic hidden account message',now(),
                    CASE WHEN n%2=0 THEN 'outgoing' ELSE 'incoming' END
                FROM generate_series(1,20000) n''')
            c.execute('''INSERT INTO message_metadata(message,revision,metadata)
                SELECT 'foreign-group-'||n,1,jsonb_build_object('mentions_owner',true,
                    'reply_to_provider_id','foreign-thread-'||n) FROM generate_series(1,20000) n''')
            c.execute('ANALYZE messages');c.execute('ANALYZE message_metadata')
    expected='group-load-'+('9999' if dense else '1')
    started=monotonic()
    row=s.list_chats(b,limit=5,view='conversation',include_coverage=False)['items'][0]
    record_property('store_elapsed_ms',round((monotonic()-started)*1000,2))
    assert row['awaiting_reply_message_id']==expected
    assert row['awaiting_reply_incoming_count']==(5000 if dense else 1)
    assert row['conversation']['owner_attention']['target_message']['id']==expected
    assert row['conversation']['owner_attention']['truncated'] is dense
    with s.authorized(b,'list_chats') as (c,p):
        measured=GroupMeasured(c)
        list_chats(s,measured,p,limit=5,view='conversation',include_coverage=False)
    import json
    plan_path=tmp_path/'group-plans.json'
    plan_path.write_text(json.dumps(measured.plans))
    record_property('plan_path',str(plan_path))
    visits=sum(visited(plan['Plan'],{'messages'}) for plan in measured.plans)
    metadata_visits=sum(visited(plan['Plan'],{'message_metadata'}) for plan in measured.plans)
    record_property('message_rows_visited',visits)
    record_property('metadata_rows_visited',metadata_visits)
    record_property('execution_ms',sum(plan['Execution Time'] for plan in measured.plans))
    heads=sum(reply_head_visits(plan['Plan']) for plan in measured.plans)
    record_property('reply_head_rows_visited',heads)
    # Three independent group reads may inspect the permitted 10k chat once
    # each. Hidden accounts must add neither metadata scans nor PK probes.
    assert visits<66000,[scans(plan['Plan']) for plan in measured.plans]
    assert metadata_visits<33000,[scans(plan['Plan']) for plan in measured.plans]
    if baseline is not None:
        assert visits<=baseline['messages']+100,(baseline,visits)
        assert metadata_visits<=baseline['message_metadata']+100,(baseline,metadata_visits)
    assert heads<60000,heads
    with TestClient(create_app(s,['testserver'])) as client:
        headers={'Authorization':'Bearer '+b,'Accept':'application/json, text/event-stream'}
        started=monotonic()
        response=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':'list_chats','arguments':{'limit':5}}}).json()
        record_property('mcp_elapsed_ms',round((monotonic()-started)*1000,2))
        assert 'error' not in response and not response['result'].get('isError'),response
        row=response['result']['structuredContent']['items'][0]
        # Compact states the attention target once: in the hint when it is the hint's message.
        # With a hint, owner_attention={'last_turn':'peer'} is omitted (ANYM-408 C4).
        attention=row['conversation'].get('owner_attention',{})
        assert attention.get('target_message_id',row.get('awaiting_reply',{}).get('message_id'))==expected


def test_conversation_head_read_ignores_newer_messages_of_other_accounts(seeded,record_property):
    from messaging_service.conversation_state import conversation_state
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'head-own-'||n,'telegram-demo','telegram-demo-chat',(1000+n)::text,'fixture',
                'Synthetic ordinary content',now()-interval '3 hours'+n*interval '1 second',
                CASE WHEN n%2=0 THEN 'outgoing' ELSE 'incoming' END
            FROM generate_series(1,2000) n''')
        c.execute('ANALYZE messages')
    def head():
        with s.authorized(b,'list_chats') as (c,p):
            measured=GroupMeasured(c)
            state=conversation_state(measured,p,'telegram-demo','telegram-demo-chat')
        return state,sum(visited(plan['Plan'],{'messages'}) for plan in measured.plans)
    before,baseline=head()
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        # Hidden-account messages newer than the whole permitted chat.
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'head-foreign-'||n,'whatsapp-demo-1','whatsapp-demo-1-chat',n::text,'other-fixture',
                'Synthetic hidden account message',now(),
                CASE WHEN n%2=0 THEN 'outgoing' ELSE 'incoming' END
            FROM generate_series(1,20000) n''')
        c.execute('ANALYZE messages')
    after,visits=head()
    record_property('head_rows_visited',str((baseline,visits)))
    assert after['latest_known_meaningful_message']['provider_message_id']=='3000'
    assert after['latest_known_meaningful_message']==before['latest_known_meaningful_message']
    assert visits<=baseline+100,(baseline,visits)


def test_group_reply_heads_keep_telegram_order_and_whatsapp_same_second_uncertainty(seeded):
    from test_conversation_queries import add
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type='group'")
        for account,prefix in [('telegram-demo','tg'),('whatsapp-demo-1','wa')]:
            add(c,prefix+'-group-question','9',account=account)
            add(c,prefix+'-group-answer','10',account=account,direction='outgoing')
            c.execute('''UPDATE message_metadata SET metadata=metadata||'{"mentions_owner":true}'::jsonb
                WHERE message=%s''',[prefix+'-group-question'])
            c.execute('''UPDATE message_metadata SET metadata=metadata||'{"reply_to_provider_id":"9"}'::jsonb
                WHERE message=%s''',[prefix+'-group-answer'])
    rows={r['account']:r for r in s.list_chats(a,view='conversation',include_coverage=False)['items']}
    assert rows['telegram-demo']['awaiting_reply'] is False
    assert rows['telegram-demo']['conversation']['owner_attention']['target_message'] is None
    state=rows['whatsapp-demo-1']['conversation']
    assert rows['whatsapp-demo-1']['awaiting_reply'] is True
    assert state['owner_attention']['target_message']['id']=='wa-group-question'
    assert state['basis']['code']=='ambiguous_attention_reply_order'


def test_large_chat_card_uses_bounded_direction_tail_lookups(seeded,record_property):
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'card-load-'||n,'telegram-demo','telegram-demo-chat',n::text,'fixture',
                'Synthetic ordinary content',now()+n*interval '1 second',
                CASE WHEN n%2=0 THEN 'incoming' ELSE 'outgoing' END
            FROM generate_series(1,20000) n''')
        c.execute('ANALYZE messages')
    with s.authorized(b,'list_chats') as (c,p):
        measured=ChatMeasured(c)
        rows=list_chats(s,measured,p,limit=5,include_coverage=False)['items']
    assert rows[0]['last_message_id']=='card-load-20000'
    visits=sum(visited(plan['Plan'],{'messages'}) for plan in measured.plans)
    record_property('message_rows_visited',visits)
    record_property('execution_ms',sum(plan['Execution Time'] for plan in measured.plans))
    if visits>=100:
        import json
        print(json.dumps([scans(plan['Plan']) for plan in measured.plans],indent=2))
    assert visits<100,[scans(plan['Plan']) for plan in measured.plans]


@pytest.mark.parametrize('order',['id','activity'])
def test_catalog_limits_chat_page_before_attention_lookups(seeded,record_property,order):
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute('''INSERT INTO chats(id,account,provider_chat,title,chat_type)
            SELECT 'catalog-'||lpad(n::text,4,'0'),'telegram-demo',n::text,'Synthetic catalog','private'
            FROM generate_series(1,500) n''')
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'catalog-message-'||n,'telegram-demo','catalog-'||lpad(n::text,4,'0'),
                n::text,'fixture','Synthetic catalog message',now(),'outgoing'
            FROM generate_series(1,500) n''')
        c.execute('ANALYZE messages');c.execute('ANALYZE chats')
    with s.authorized(b,'list_chats') as (c,p):
        measured=ChatMeasured(c)
        rows=list_chats(s,measured,p,limit=5,order=order,include_coverage=False)['items']
    visits=sum(visited(plan['Plan'],{'messages'}) for plan in measured.plans)
    record_property('message_rows_visited',visits)
    expected=range(1,6) if order=='id' else range(500,495,-1)
    assert [row['id'] for row in rows]==['catalog-'+str(n).zfill(4) for n in expected]
    assert visits<(100 if order=='id' else 600),[scans(plan['Plan']) for plan in measured.plans]


@pytest.mark.parametrize('filters',[{}, {'order':'activity'},
    {'awaiting_reply':True,'exclude_channels':True,'order':'priority'}])
def test_large_chat_audit_calls_work_through_store_rest_and_mcp(seeded,record_property,filters):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        c.execute("UPDATE chats SET chat_type='private' WHERE id='telegram-demo-chat'")
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            SELECT 'audit-call-'||n,'telegram-demo','telegram-demo-chat',n::text,'fixture',
                'Synthetic ordinary question?',now()+n*interval '1 second',
                CASE WHEN n%2=0 THEN 'incoming' ELSE 'outgoing' END
            FROM generate_series(1,20000) n''')
        c.execute('ANALYZE messages')
    # Match the audit's ordinary call shape, including default coverage. Store
    # and REST keep their archive default; MCP independently adds its normal
    # conversation view and head evidence.
    options={'limit':5,**filters}
    started=monotonic()
    direct=s.list_chats(b,**options)
    record_property('store_elapsed_ms',round((monotonic()-started)*1000,2))
    assert direct['items'][0]['last_message_id']=='audit-call-20000'
    with TestClient(create_app(s,['testserver'])) as client:
        headers={'Authorization':'Bearer '+b,'Accept':'application/json, text/event-stream'}
        started=monotonic();rest=client.post('/v1/list_chats',headers=headers,json=options)
        record_property('rest_elapsed_ms',round((monotonic()-started)*1000,2))
        assert rest.status_code==200 and rest.json()['items'][0]['last_message_id']=='audit-call-20000'
        started=monotonic()
        rpc=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':'list_chats','arguments':options}}).json()
        record_property('mcp_elapsed_ms',round((monotonic()-started)*1000,2))
        assert 'error' not in rpc and not rpc['result'].get('isError'),rpc
        row=rpc['result']['structuredContent']['items'][0]
        # A compact queue row shows the head once: in the hint when the hint is the head.
        head=row['last_message']['id'] if 'last_message' in row else row['awaiting_reply']['message_id']
        assert head=='audit-call-20000' and 'latest_message_id' not in row['conversation']
