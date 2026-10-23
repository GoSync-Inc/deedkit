"""Search returns forwarded copies of one message once, with the chats it was forwarded to (ANYM-273)."""
import json

import pytest

from messaging_service.envelope import stable_id
from messaging_service.store import APIError

POST={'kind':'channel','chat_id':'-1000000000777','sender_name':'Synthetic News','provider_message_id':'41',
    'sent_at':'2026-09-01T00:00:00+00:00','basis':'telegram_fwd_from'}
PERSON={'kind':'user','sender_id':'77','sender_name':'Synthetic Person','sent_at':'2026-10-01T09:00:00+00:00',
    'basis':'telegram_fwd_from'}


@pytest.fixture()
def copies(seeded):
    from messaging_service.archive import Archive
    from test_archive import event
    s,_,_=seeded
    ar=Archive(s);ar.register('forward-tg','telegram','Synthetic forwards',discover=True)
    def add(mid,body,chat,*,at='2026-10-01T12:00:00+00:00',sender='42',origin=None):
        metadata={'forwarded':True,'forward_origin':origin} if origin else {'forwarded':False}
        return ar.ingest(event(account='forward-tg',chat=chat,provider_id=mid,body=body,sender=sender,
            sent_at=at,updated_at=at,metadata=metadata))['message']
    return s,add


def chat(provider_chat):
    return stable_id('chat','forward-tg',provider_chat)


# spec: ANYM-273
def test_three_forwarded_copies_in_three_chats_are_one_row(copies):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    from test_client_contract import rpc
    from test_compact_queue_rows import structured
    s,add=copies
    ids=[add(str(n),'synthetic marquee news',provider,at=f'2026-10-01T12:0{n}:00+00:00',origin=POST)
        for n,provider in enumerate(('300','301','302'))]
    key=s.issue_key('forward-reader',['forward-tg'])
    result=s.search(key,'synthetic marquee')
    assert [row['id'] for row in result['items']]==[ids[0]]
    row=result['items'][0]
    assert row['copies_total']==3
    assert row['copies']==[{'chat':chat('301'),'message_id':ids[1],'sender':'42','sent_at':'2026-10-01T12:01:00+00:00'},
        {'chat':chat('302'),'message_id':ids[2],'sender':'42','sent_at':'2026-10-01T12:02:00+00:00'}]
    assert row['latest_copy_at']=='2026-10-01T12:02:00+00:00'
    assert result['search']['collapse_forwards'] is True
    # The agent-facing tool keeps the group in every detail level.
    with TestClient(create_app(s,['testserver'])) as client:
        for detail in ('compact','full'):
            items=structured(rpc(client,key,'search',{'query':'synthetic marquee','detail':detail,'include_coverage':False}))['items']
            assert len(items)==1 and items[0]['copies_total']==3,detail
            assert items[0]['latest_copy_at']=='2026-10-01T12:02:00+00:00',detail
            assert [c['message_id'] for c in items[0]['copies']]==ids[1:],detail


# spec: ANYM-273
def test_a_caller_reading_two_chats_counts_two_copies_and_never_sees_the_third(copies):
    s,add=copies
    ids=[add(str(n),'synthetic marquee news',provider,at=f'2026-10-01T12:0{n}:00+00:00',origin=POST)
        for n,provider in enumerate(('300','301','302'))]
    key=s.issue_key('forward-scoped',['forward-tg'],chats=[chat('300'),chat('302')])
    result=s.search(key,'synthetic marquee')
    assert [row['id'] for row in result['items']]==[ids[0]]
    assert result['items'][0]['copies_total']==2
    assert [c['message_id'] for c in result['items'][0]['copies']]==[ids[2]]
    text=json.dumps(result,default=str)
    assert chat('301') not in text and ids[1] not in text


# spec: ANYM-273
def test_equal_text_from_different_origins_stays_separate_rows(copies):
    s,add=copies
    first=add('1','synthetic marquee news','300',origin=POST)
    other=add('2','synthetic marquee news','301',origin={**POST,'provider_message_id':'42'})
    person=add('3','synthetic marquee news','302',origin=PERSON)
    # Two plain messages with equal text, sender and second are never grouped with each other.
    plain=[add('4','synthetic marquee news','303',sender='77'),add('5','synthetic marquee news','304',sender='77')]
    # Origins that name no message key never group.
    hidden=[add(str(n),'synthetic marquee news','305',origin={'kind':'hidden_user','sender_name':'Someone',
        'sent_at':'2026-10-01T09:00:00+00:00','basis':'telegram_fwd_from'}) for n in (6,7)]
    key=s.issue_key('forward-reader',['forward-tg'])
    rows=s.search(key,'synthetic marquee',limit=50)['items']
    assert sorted(r['id'] for r in rows)==sorted([first,other,person,*plain,*hidden])
    assert all('copies_total' not in r and 'copies' not in r for r in rows)


# spec: ANYM-273
def test_collapse_forwards_false_returns_every_copy(copies):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    from test_client_contract import rpc
    from test_compact_queue_rows import structured
    s,add=copies
    ids=[add(str(n),'synthetic marquee news',provider,at=f'2026-10-01T12:0{n}:00+00:00',origin=POST)
        for n,provider in enumerate(('300','301','302'))]
    key=s.issue_key('forward-reader',['forward-tg'])
    result=s.search(key,'synthetic marquee',collapse_forwards=False)
    assert [r['id'] for r in result['items']]==ids and result['search']['collapse_forwards'] is False
    assert all('copies_total' not in r for r in result['items'])
    # A chat listing is never collapsed: each copy matters there.
    assert [r['id'] for r in s.get_messages(key,chat('300'))['items']]==[ids[0]]
    with pytest.raises(APIError) as error:s.search(key,'synthetic marquee',collapse_forwards='no')
    assert error.value.code=='invalid_filter'
    with TestClient(create_app(s,['testserver'])) as client:
        items=structured(rpc(client,key,'search',{'query':'synthetic marquee','collapse_forwards':False,
            'include_coverage':False}))['items']
        assert [r['id'] for r in items]==ids


# spec: ANYM-273
@pytest.mark.parametrize('order',['asc','desc','relevance'])
def test_pages_never_repeat_or_skip_a_group(copies,order):
    s,add=copies
    groups={}
    # Copies of four origins interleave in time, with plain hits between them.
    for n in range(12):
        origin={**POST,'provider_message_id':str(100+n%4)}
        mid=add(f'f{n}','synthetic marquee news',('300','301','302')[n%3],at=f'2026-10-01T{10+n:02d}:00:00+00:00',origin=origin)
        groups.setdefault(origin['provider_message_id'],[]).append(mid)
    singles=[add(f'p{n}','synthetic marquee plain','303',at=f'2026-10-01T{10+n:02d}:30:00+00:00') for n in range(3)]
    key=s.issue_key('forward-reader',['forward-tg'])
    seen=[];cursor='';pages=0
    while True:
        page=s.search(key,'synthetic marquee',limit=2,order=order,after=cursor)
        seen.extend(page['items']);pages+=1
        cursor=page['next_cursor']
        if not cursor:break
        assert pages<20
    ids=[r['id'] for r in seen]
    assert len(ids)==len(set(ids))==len(groups)+len(singles)
    members={m for r in seen for m in [r['id'],*(c['message_id'] for c in r.get('copies',[]))]}
    assert members=={*singles,*(m for g in groups.values() for m in g)}
    assert sorted(r['copies_total'] for r in seen if 'copies_total' in r)==[3,3,3,3]


# spec: ANYM-273
def test_the_archived_original_represents_its_forwarded_copies(copies):
    s,add=copies
    original=add('9','synthetic marquee news','400',sender='77',at=PERSON['sent_at'])
    forwards=[add(str(n),'synthetic marquee news',provider,at=f'2026-10-01T12:0{n}:00+00:00',origin=PERSON)
        for n,provider in enumerate(('300','301'))]
    # A channel post and its forward: the post key (origin chat and message ID) joins them.
    post=add('41','synthetic marquee post','-1000000000777',sender='-1000000000777',at=POST['sent_at'])
    shared=add('50','synthetic marquee post','300',origin=POST)
    key=s.issue_key('forward-reader',['forward-tg'])
    rows={r['id']:r for r in s.search(key,'synthetic marquee',limit=50)['items']}
    assert set(rows)=={original,post}
    assert rows[original]['copies_total']==3 and [c['message_id'] for c in rows[original]['copies']]==forwards
    assert rows[post]['copies_total']==2 and [c['message_id'] for c in rows[post]['copies']]==[shared]


# spec: ANYM-273
def test_copies_in_one_chat_at_one_second_are_one_row(copies):
    s,add=copies
    ids=[add(str(n),'synthetic marquee news','300',sender=str(40+n),origin=POST) for n in range(3)]
    key=s.issue_key('forward-reader',['forward-tg'])
    rows=s.search(key,'synthetic marquee')['items']
    assert len(rows)==1 and rows[0]['copies_total']==3
    assert {rows[0]['id'],*(c['message_id'] for c in rows[0]['copies'])}==set(ids)


# spec: ANYM-273
def test_a_fresh_forward_of_an_old_original_keeps_its_place_in_newest_first_order(copies):
    s,add=copies
    original=add('41','synthetic marquee post','-1000000000777',sender='-1000000000777',at=POST['sent_at'])
    middle=add('2','synthetic marquee plain','301',at='2026-09-15T12:00:00+00:00')
    fresh=add('3','synthetic marquee post','300',at='2026-10-20T12:00:00+00:00',origin=POST)
    key=s.issue_key('forward-reader',['forward-tg'])
    first=s.search(key,'synthetic marquee',order='desc',limit=1)
    # The group is shown as the original but sits where its newest copy sits.
    assert [r['id'] for r in first['items']]==[original]
    assert first['items'][0]['latest_copy_at']=='2026-10-20T12:00:00+00:00'
    assert [c['message_id'] for c in first['items'][0]['copies']]==[fresh]
    second=s.search(key,'synthetic marquee',order='desc',limit=1,after=first['next_cursor'])
    assert [r['id'] for r in second['items']]==[middle] and second['next_cursor'] is None


# spec: ANYM-273
def test_two_texts_one_sender_one_second_stay_two_groups(copies):
    s,add=copies
    news=[add(str(n),'synthetic marquee news',provider,origin=PERSON) for n,provider in enumerate(('300','301'))]
    other=[add(str(n+2),'synthetic marquee weather',provider,origin=PERSON) for n,provider in enumerate(('300','301'))]
    key=s.issue_key('forward-reader',['forward-tg'])
    rows=s.search(key,'synthetic marquee',limit=50)['items']
    assert len(rows)==2 and all(r['copies_total']==2 for r in rows)
    assert sorted(sorted([r['id'],*(c['message_id'] for c in r['copies'])]) for r in rows)==sorted([sorted(news),sorted(other)])


# spec: ANYM-273
def test_an_original_outside_the_scope_leaves_the_earliest_visible_copy_shown(copies):
    s,add=copies
    original=add('41','synthetic marquee post','-1000000000777',sender='-1000000000777',at=POST['sent_at'])
    forwards=[add(str(n),'synthetic marquee post',provider,at=f'2026-10-01T12:0{n}:00+00:00',origin=POST)
        for n,provider in enumerate(('300','301'))]
    key=s.issue_key('forward-scoped',['forward-tg'],chats=[chat('300'),chat('301')])
    result=s.search(key,'synthetic marquee')
    assert [r['id'] for r in result['items']]==[forwards[0]]
    assert result['items'][0]['copies_total']==2 and [c['message_id'] for c in result['items'][0]['copies']]==forwards[1:]
    assert original not in json.dumps(result,default=str)


# spec: ANYM-273
def test_listings_without_a_query_and_context_keep_every_copy(copies):
    s,add=copies
    ids=[add(str(n),'synthetic marquee news','300',at=f'2026-10-01T12:0{n}:00+00:00',origin=POST) for n in range(3)]
    key=s.issue_key('forward-reader',['forward-tg'])
    listed=s.search(key,'',chat=chat('300'))
    assert sorted(r['id'] for r in listed['items'])==sorted(ids) and listed['search']['collapse_forwards'] is False
    context=s.get_context(key,ids[1])
    text=json.dumps(context,default=str)
    assert all(mid in text for mid in ids) and 'copies_total' not in text


# spec: ANYM-273
def test_archive_view_and_transcript_keep_one_row_per_group(copies):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    from test_client_contract import rpc
    from test_compact_queue_rows import structured
    s,add=copies
    ids=[add(str(n),'synthetic marquee news',provider,at=f'2026-10-01T12:0{n}:00+00:00',origin=POST)
        for n,provider in enumerate(('300','301','302'))]
    key=s.issue_key('forward-reader',['forward-tg'])
    with TestClient(create_app(s,['testserver'])) as client:
        for detail in ('compact','full'):
            result=structured(rpc(client,key,'search',{'query':'synthetic marquee','view':'archive','detail':detail,
                'include_coverage':False}))
            items=result['items']
            assert [r['id'] for r in items]==[ids[0]] and items[0]['copies_total']==3,detail
            assert items[0]['latest_copy_at']=='2026-10-01T12:02:00+00:00',detail
            assert [c['message_id'] for c in items[0]['copies']]==ids[1:],detail
            assert result['search']['collapse_forwards'] is True,detail
        # REST returns the same group and reports the option.
        body=client.post('/v1/search',json={'query':'synthetic marquee'},headers={'Authorization':'Bearer '+key}).json()
        assert [r['id'] for r in body['items']]==[ids[0]] and body['items'][0]['copies_total']==3
        assert body['items'][0]['latest_copy_at']=='2026-10-01T12:02:00+00:00'
        assert [c['message_id'] for c in body['items'][0]['copies']]==ids[1:] and body['search']['collapse_forwards'] is True
        flat=client.post('/v1/search',json={'query':'synthetic marquee','collapse_forwards':False},
            headers={'Authorization':'Bearer '+key}).json()
        assert [r['id'] for r in flat['items']]==ids and flat['search']['collapse_forwards'] is False
        # The transcript names the shown row only; its copies are not listed there.
        text=json.dumps(structured(rpc(client,key,'search',{'query':'synthetic marquee','detail':'transcript',
            'include_coverage':False})),default=str)
        assert ids[1] not in text and ids[2] not in text


# spec: ANYM-273
def test_a_large_group_lists_twenty_copies_in_the_page_statement(copies):
    from messaging_service.message_queries import search
    from test_search_performance import Measured
    s,add=copies
    first=add('seed','synthetic marquee news','300',origin=POST)
    origin=json.dumps({'forwarded':True,'forward_origin':POST})
    # 3,000 matching forwards of 300 other posts (300 groups of 10) plus one 500-member group.
    with s.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            SELECT 'bulk-'||n,'forward-tg',%s,'bulk-'||n,'42','synthetic marquee news bulk '||(n%%300),
                timestamptz '2026-10-02T00:00:00Z'+n*interval '1 second' FROM generate_series(1,3000) n''',[chat('300')])
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            SELECT 'copy-'||n,'forward-tg',%s,'copy-'||n,'42','synthetic marquee news',
                timestamptz '2026-10-03T00:00:00Z'+n*interval '1 second' FROM generate_series(1,499) n''',[chat('300')])
        c.execute('''INSERT INTO message_metadata(message,revision,metadata)
            SELECT id,revision,%s::jsonb FROM messages WHERE id LIKE 'copy-%%' ''',[origin])
        c.execute('''INSERT INTO message_metadata(message,revision,metadata)
            SELECT id,revision,jsonb_build_object('forwarded',true,'forward_origin',jsonb_build_object('kind','channel',
                'chat_id','-1000000000888','provider_message_id',(split_part(id,'-',2)::int%300)::text,
                'sent_at','2026-09-01T00:00:00+00:00','basis','telegram_fwd_from'))
            FROM messages WHERE id LIKE 'bulk-%' ''')
        c.execute('ANALYZE messages');c.execute('ANALYZE message_metadata')
    key=s.issue_key('forward-reader',['forward-tg'])
    with s.authorized(key,'search') as (c,p):
        measured=Measured(c)
        result=search(s,measured,p,query='synthetic marquee news',limit=5,order='desc')
    rows=result['items']
    assert len(rows)==5 and rows[0]['id']==first and rows[0]['copies_total']==500 and len(rows[0]['copies'])==20
    assert [r['copies_total'] for r in rows[1:]]==[10]*4 and all(len(r['copies'])==9 for r in rows[1:])
    assert rows[0]['copies'][0]['message_id']=='copy-1' and rows[0]['copies'][-1]['message_id']=='copy-20'
    # Copies come from the page statement itself, and no node rescans the grouped matches per row.
    assert len(measured.plans)==1
    def loops(node):
        own=[node['Actual Loops']] if node.get('CTE Name')=='grouped' else []
        return own+sum((loops(child) for child in node.get('Plans',[])),[])
    assert loops(measured.plans[0]) and max(loops(measured.plans[0]))==1


# spec: ANYM-273
def test_live_search_passes_collapse_forwards_to_the_archive_read():
    import asyncio
    from messaging_service import live_search
    calls=[]
    class Archive:
        def search(self,token,**arguments):
            calls.append(arguments);return {'items':[],'result_evidence':{}}
    asyncio.run(live_search.search(Archive(),'synthetic-token',{'query':'synthetic marquee','collapse_forwards':False}))
    assert calls==[{'query':'synthetic marquee','collapse_forwards':False}]


# spec: ANYM-273
def test_malformed_or_keyless_origins_stay_ordinary_hits(copies):
    s,add=copies
    bad=[{'kind':'user','sender_id':'77','sent_at':'2026-10'},{'kind':'user','sent_at':PERSON['sent_at']},
        {'kind':'channel','chat_id':'-1000000000777','provider_message_id':''},{'kind':'unknown'},'text']
    ids=[add(str(n),'synthetic marquee news',str(300+n)) for n in range(len(bad))]
    # Shapes an older writer or a damaged row could hold, set below the ingest validation.
    with s.pool.connection() as c:
        for mid,origin in zip(ids,bad):
            c.execute("UPDATE message_metadata SET metadata=jsonb_build_object('forwarded',true,'forward_origin',%s::jsonb) WHERE message=%s",
                [json.dumps(origin),mid])
    key=s.issue_key('forward-reader',['forward-tg'])
    result=s.search(key,'synthetic marquee',limit=50)
    assert sorted(r['id'] for r in result['items'])==sorted(ids)
    assert all('copies_total' not in r for r in result['items']) and result['search']['collapse_forwards'] is True


# spec: ANYM-273
@pytest.mark.parametrize('order',['asc','desc'])
def test_a_cursor_never_continues_in_the_other_view(copies,order):
    s,add=copies
    for n in range(4):
        add(f'f{n}','synthetic marquee news',('300','301')[n%2],at=f'2026-10-01T1{n}:00:00+00:00',
            origin={**POST,'provider_message_id':str(100+n%2)})
    key=s.issue_key('forward-reader',['forward-tg'])
    collapsed=s.search(key,'synthetic marquee',order=order,limit=1)
    flat=s.search(key,'synthetic marquee',order=order,limit=1,collapse_forwards=False)
    assert collapsed['next_cursor'] and flat['next_cursor'] and flat['next_cursor'].startswith('q1.')
    for cursor,collapse in ((collapsed['next_cursor'],False),(flat['next_cursor'],True)):
        with pytest.raises(APIError) as error:
            s.search(key,'synthetic marquee',order=order,limit=1,after=cursor,collapse_forwards=collapse)
        assert error.value.code=='invalid_cursor'
    # The flat view pages through every copy exactly once.
    seen=[*flat['items']];cursor=flat['next_cursor']
    while cursor:
        page=s.search(key,'synthetic marquee',order=order,limit=1,after=cursor,collapse_forwards=False)
        seen.extend(page['items']);cursor=page['next_cursor']
    assert len({r['id'] for r in seen})==len(seen)==4


# spec: ANYM-273
@pytest.mark.parametrize('kind',['hidden_user','status','unknown'])
def test_excluded_origin_kinds_never_group_even_with_ids(copies,kind):
    s,add=copies
    ids=[add(str(n),'synthetic marquee news',str(300+n),origin={**POST,'kind':kind}) for n in range(2)]
    key=s.issue_key('forward-reader',['forward-tg'])
    rows=s.search(key,'synthetic marquee')['items']
    assert sorted(r['id'] for r in rows)==sorted(ids) and all('copies_total' not in r for r in rows)
