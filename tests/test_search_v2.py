"""Morphology, ranked pages, scoped metadata, centred windows and durable views."""
from datetime import datetime,timezone

import pytest
from psycopg.types.json import Jsonb

from messaging_service.search_query import compile_query,cursor_binding,decode_cursor,encode_cursor
from messaging_service.store import APIError


def add(s,mid,body,*,account='telegram-demo',chat='telegram-demo-chat',sender='person',day=5,direction='incoming'):
    with s.pool.connection() as c:
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
            (mid,account,chat,mid,sender,body,datetime(2026,10,day,12,tzinfo=timezone.utc),direction))


def test_query_helper_bounds_and_no_query_interpolation():
    needle="x'); DELETE FROM messages; --"
    plan=compile_query(needle,'exact')
    assert needle not in plan.cte and needle in plan.params
    assert 'LIKE' in plan.predicate('m')
    assert 'literal_pattern' in plan.cte
    for kwargs in [{'search_language':'missing'},{'query_mode':'sql'},{'query':'x\x00'}]:
        with pytest.raises(APIError):compile_query(**kwargs)
    p={'id':'agent','scope_version':1}
    binding=cursor_binding(p,'search',{'q':'passport'})
    token=encode_cursor(binding,{'id':'msg'})
    assert decode_cursor(token,binding)=={'id':'msg'}
    with pytest.raises(APIError):decode_cursor(token,cursor_binding(p,'search',{'q':'other'}))
    with pytest.raises(APIError):decode_cursor('q1.'+'x'*5000,binding)
    import base64
    nested='q1.'+base64.urlsafe_b64encode(('['*1200+']'*1200).encode()).decode()
    with pytest.raises(APIError,match='invalid_cursor'):decode_cursor(nested,binding)


def test_native_morphology_simple_exact_and_mixed_queries(seeded):
    s,a,b=seeded
    add(s,'morph-ru','После терапии проверили копию паспорта и подпись по договору. Running quickly.')
    add(s,'morph-id','Saya membaca buku tentang pembayaran.')
    for query in ('терапия','паспорт','договор'):
        result=s.search(b,query,search_language='russian')
        assert [r['id'] for r in result['items']]==['morph-ru']
        assert result['items'][0]['match_type']=='morphology_russian'
        assert s.search(b,query,search_language='simple')['items']==[]
    assert s.search(b,'run',search_language='english')['items'][0]['id']=='morph-ru'
    assert s.search(b,'паспорт run')['items'][0]['id']=='morph-ru'
    assert s.search(b,'baca',search_language='indonesian')['items'][0]['id']=='morph-id'
    assert s.search(b,'терапия',query_mode='exact')['items']==[]
    assert s.search(b,'ТЕРАПИИ',query_mode='exact')['items'][0]['id']=='morph-ru'
    add(s,'literal','Code 100%_literal')
    add(s,'wildcard-decoy','Code 100XXliteral')
    assert [r['id'] for r in s.search(b,'100%_',query_mode='exact')['items']]==['literal']


def test_relevance_cursor_bound_to_query_scope_and_revision(seeded):
    s,a,b=seeded
    add(s,'rank-1','passport')
    add(s,'rank-2','passport passport passport')
    add(s,'rank-hidden','passport '*50,account='whatsapp-demo-2',chat='whatsapp-demo-2-chat')
    first=s.search(b,'passport',order='relevance',limit=1)
    assert first['items'][0]['id']=='rank-2'
    second=s.search(b,'passport',order='relevance',after=first['next_cursor'],limit=1)
    assert second['items'][0]['id']=='rank-1' and second['next_cursor'] is None
    with pytest.raises(APIError):s.search(b,'different',order='relevance',after=first['next_cursor'])
    s.set_access('agent-b',chats=['telegram-demo-chat'])
    with pytest.raises(APIError):s.search(b,'passport',order='relevance',after=first['next_cursor'])
    first=s.search(b,'passport',order='relevance',limit=1)
    with s.pool.connection() as c:c.execute("UPDATE messages SET revision=revision+1 WHERE id='rank-2'")
    with pytest.raises(APIError,match='cursor_changed'):
        s.search(b,'passport',order='relevance',after=first['next_cursor'])


def test_fallback_is_explicit_and_does_not_union_approximate_matches(seeded):
    s,a,b=seeded
    add(s,'fuzzy','The passport is here')
    assert s.search(b,'passporrt')['items']==[]
    result=s.search(b,'passporrt',fuzzy=True)
    assert result['search']['fuzzy_used'] and result['items'][0]['id']=='fuzzy'
    assert result['items'][0]['match_type']=='trigram_fallback'
    exact=s.search(b,'passport',fuzzy=True)
    assert not exact['search']['fuzzy_used']
    for query in ('pa','passport file','x'*65):
        with pytest.raises(APIError,match='invalid_fuzzy_query'):s.search(b,query,fuzzy=True)


def test_snippet_near_match_and_compact_output_keeps_coverage(seeded):
    s,a,b=seeded
    add(s,'long-body','irrelevant '*200+'копия паспорта находится здесь '+'tail '*100)
    result=s.search(b,'паспорт',compact=True,body_chars=10,include_coverage=False)
    row=result['items'][0]
    assert 'паспорт' in row['snippet'] and '[[' in row['snippet']
    assert len(row['snippet'])<=800 and 'body' not in row
    assert 'coverage' in result and 'sources' not in result
    full=s.get_messages(b,'telegram-demo-chat',after='demo-tg-2',body_chars=10)
    assert full['items'][0]['body_truncated'] and len(full['items'][0]['body'])==10


def test_decorations_same_chat_and_live_profile_source_only(seeded):
    s,a,b=seeded
    add(s,'named-source','named',sender='known')
    with s.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type='private' WHERE id='telegram-demo-chat'")
        c.execute('''INSERT INTO sender_profiles(id,account,chat,sender,display_name,username,source_message,source_sent_at)
            VALUES('profile','telegram-demo','telegram-demo-chat','known','Visible Name','visible','named-source',now())''')
        c.execute('INSERT INTO message_metadata(message,revision,metadata) VALUES(%s,1,%s)',
            ('named-source',Jsonb({'reply_to_provider_id':'12','forwarded':False,'media_kind':'photo'})))
    row=s.search(b,'named')['items'][0]
    assert (row['sender_name'],row['username'],row['chat_type'],row['direction'])==('Visible Name','visible','private','incoming')
    assert row['reply_to_provider_id']=='12' and row['forwarded'] is False and row['media_kind']=='photo'
    context=s.get_context(b,'named-source')
    assert not any(k.startswith('document') for k in context['message'])
    add(s,'same-sender','visible text',sender='known')
    with s.pool.connection() as c:c.execute("UPDATE messages SET deleted=true WHERE id='named-source'")
    assert s.search(b,'visible text')['items'][0]['sender_name'] is None


def test_centered_context_separate_counts_and_equal_timestamp_neighbors(seeded):
    s,a,b=seeded
    for n in range(12):add(s,f'around-{n:02}','nearby')
    result=s.get_context(b,'around-06',before_count=2,after_count=3)
    assert [r['id'] for r in result['context']]==[f'around-{n:02}' for n in range(4,10)]
    assert result['context_window']['has_more_before'] and result['context_window']['has_more_after']
    assert all('direction' in r for r in result['context'])
    assert len(s.get_context(b,'around-06',before_count=0,after_count=0)['context'])==1


def test_activity_order_filters_unknown_provider_state_and_heuristic(seeded):
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type='private' WHERE account='telegram-demo'")
        c.execute("UPDATE chats SET chat_type='channel' WHERE account='whatsapp-demo-1'")
    first=s.list_chats(a,order='activity',limit=1)
    assert first['items'][0]['id']=='whatsapp-demo-1-chat'
    second=s.list_chats(a,order='activity',after=first['next_cursor'],limit=1)
    assert second['items'][0]['id']=='telegram-demo-chat'
    assert second['items'][0]['provider_unread'] is None and second['items'][0]['muted'] is None
    assert [r['id'] for r in s.list_chats(a,awaiting_reply=True,awaiting_reply_days=0)['items']]==['telegram-demo-chat']
    assert s.list_chats(b,accounts=['whatsapp-demo-1'])['items']==[]
    with pytest.raises(APIError):s.list_chats(a,order='activity',query='changed',after=first['next_cursor'])


def test_filtered_consumer_ack_does_not_skip_other_views_or_change_filter(seeded):
    s,a,b=seeded
    first=s.get_updates(a,'only-tg',chats=['telegram-demo-chat'],limit=1)
    assert [e['message'] for e in first['items']]==['demo-tg-1'] and first['has_more']
    with pytest.raises(APIError,match='consumer_filter_mismatch'):
        s.get_updates(a,'only-tg',chats=['whatsapp-demo-1-chat'])
    s.ack_updates(a,'only-tg',first['receipt'],first['ack_version'])
    second=s.get_updates(a,'only-tg',chats=['telegram-demo-chat'],limit=1)
    assert [e['message'] for e in second['items']]==['demo-tg-2']
    s.ack_updates(a,'only-tg',second['receipt'],second['ack_version'])
    assert s.get_updates(a,'only-tg',chats=['telegram-demo-chat'])['items']==[]
    assert len(s.get_updates(a,'all')['items'])==3
    assert s.get_updates(a,'live-only',origins=['live'])['items']==[]
    assert s.get_updates(a,'no-chats',chats=[])['items']==[]
    assert s.get_updates(b,'hidden',accounts=['whatsapp-demo-1'])['items']==[]


def test_people_transliteration_is_candidate_and_grouping_never_crosses_accounts(seeded):
    from messaging_service.archive import Archive
    from test_archive import event
    s,a,b=seeded;ar=Archive(s)
    for account in ('people-one','people-two'):
        ar.register(account,'telegram','Fixture',discover=True)
        for chat in ('100','200'):
            ar.ingest(event(account=account,chat=chat,sender_profile={'display_name':'Юлия Пример','username':''}))
    key=s.issue_key('candidate-reader',['people-one','people-two'])
    result=s.search_people(key,'Yuliya',transliterate=True,group_by_sender=True)
    assert len(result['items'])==2
    assert all(len(r['observations'])==2 for r in result['items'])
    assert all(r['match_type']=='transliteration_candidate' for r in result['items'])
    assert len(s.search_people(key,'Юля',transliterate=True)['items'])==4
    visible=result['items'][0]['observations'][0]['chat']
    s.set_access('candidate-reader',chats=[visible])
    limited=s.search_people(key,'Yuliya',transliterate=True,group_by_sender=True)
    assert len(limited['items'])==1 and len(limited['items'][0]['observations'])==1


ORIGIN={'kind':'channel','chat_id':'-1000000000777','sender_name':'Synthetic News','provider_message_id':'41',
    'post_author':'Synthetic Editor','sent_at':'2026-09-01T00:00:00+00:00','basis':'telegram_fwd_from'}


@pytest.fixture()
def forwards(seeded):
    from messaging_service.archive import Archive
    from test_archive import event
    s,_,_=seeded
    ar=Archive(s);ar.register('forward-tg','telegram','Synthetic forwards',discover=True)
    def add(mid,body,chat='300',**metadata):
        return ar.ingest(event(account='forward-tg',chat=chat,provider_id=mid,body=body,metadata=metadata))
    return s,ar,add,event


def forward_rows(s,key,message_id,chat):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    from test_client_contract import rpc
    from test_compact_queue_rows import structured
    with TestClient(create_app(s,['testserver'])) as client:
        for detail in ('compact','full'):
            for view in ('archive','conversation'):
                common={'detail':detail,'view':view,'include_coverage':False}
                result=structured(rpc(client,key,'search',{'query':'synthetic forwarded',**common}))
                yield f'search/{detail}/{view}',result['items']
                result=structured(rpc(client,key,'get_messages',{'chat_id':chat,**common}))
                yield f'get_messages/{detail}/{view}',result['items']
                result=structured(rpc(client,key,'get_context',{'message_id':message_id,**common}))
                yield f'get_context/{detail}/{view}',[result['message'],*result['context']]


def test_forwarded_message_states_origin_and_authorship_in_every_detail(forwards):
    from messaging_service.envelope import stable_id
    s,_,add,_=forwards
    forwarded=add('1','synthetic forwarded words',forwarded=True,forward_origin=ORIGIN)['message']
    plain=add('2','synthetic forwarded reply',forwarded=False)['message']
    key=s.issue_key('forward-reader',['forward-tg'])
    seen=set()
    for name,rows in forward_rows(s,key,forwarded,stable_id('chat','forward-tg','300')):
        by_id={row['id']:row for row in rows}
        assert {forwarded,plain}<=set(by_id),name
        assert by_id[forwarded]['authorship']=='forwarded_by_sender',name
        assert by_id[forwarded]['forward_origin']==ORIGIN,name
        assert 'forward_origin' not in by_id[plain] and 'authorship' not in by_id[plain],name
        seen.add(name)
    assert len(seen)==12


def test_forwarded_message_stored_before_origin_capture_reports_not_captured(forwards):
    s,ar,add,event=forwards
    old=add('1','synthetic forwarded words',forwarded=True)
    key=s.issue_key('forward-reader',['forward-tg'])
    row=s.search(key,'synthetic forwarded')['items'][0]
    assert row['forward_origin']=={'kind':'not_captured'} and row['authorship']=='forwarded_by_sender'
    # Ordinary capture re-reads the unchanged message: same revision, no conflict, origin added.
    again=ar.ingest(event(account='forward-tg',chat='300',provider_id='1',body='synthetic forwarded words',
        metadata={'forwarded':True,'forward_origin':ORIGIN}))
    assert again['outcome']=='unchanged'
    row=s.search(key,'synthetic forwarded')['items'][0]
    assert row['forward_origin']==ORIGIN and row['revision']==1
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM message_conflicts WHERE message=%s',(old['message'],)).fetchone()['n']==0
        assert c.execute('SELECT count(*) AS n FROM message_revisions WHERE message=%s',(old['message'],)).fetchone()['n']==1


def test_forward_origin_chat_grants_no_access(forwards):
    from messaging_service.envelope import stable_id
    s,_,add,_=forwards
    forwarded=add('1','synthetic forwarded words',forwarded=True,forward_origin=ORIGIN)['message']
    hidden=add('41','synthetic origin post',chat='-1000000000777')['message']
    key=s.issue_key('forward-scoped',['forward-tg'],chats=[stable_id('chat','forward-tg','300')])
    row=s.get_context(key,forwarded)['message']
    assert row['forward_origin']['chat_id']=='-1000000000777' and row['forward_origin']['sender_name']=='Synthetic News'
    def refusal(call):
        with pytest.raises(APIError) as error:call()
        return error.value.code,error.value.status
    absent=refusal(lambda:s.resolve_reference(key,'forward-tg','-1000000000999'))
    assert absent==('not_found',404)
    assert refusal(lambda:s.resolve_reference(key,'forward-tg','-1000000000777'))==absent
    assert refusal(lambda:s.resolve_reference(key,'forward-tg','-1000000000777','41'))==absent
    assert refusal(lambda:s.get_context(key,hidden))==absent
    assert [r['id'] for r in s.search(key,'synthetic')['items']]==[forwarded]


def test_reply_queue_hint_states_forward_origin(forwards):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    from test_client_contract import rpc
    from test_compact_queue_rows import structured
    from datetime import timedelta
    s,ar,_,event=forwards
    at=(datetime.now(timezone.utc)-timedelta(hours=1)).replace(microsecond=0).isoformat()
    def add(chat,mid,**metadata):
        return ar.ingest(event(account='forward-tg',chat=chat,provider_id=mid,body='synthetic question?',
            sent_at=at,updated_at=at,metadata={'chat_type':'private',**metadata}))['message']
    forwarded=add('301','1',forwarded=True,forward_origin=ORIGIN)
    plain=add('302','1')
    key=s.issue_key('forward-queue',['forward-tg'])
    full={r['awaiting_reply_message_id']:r for r in s.list_chats(key,awaiting_reply=True,include_coverage=False)['items']}
    assert full[forwarded]['awaiting_reply_forward_origin']==ORIGIN
    assert full[forwarded]['awaiting_reply_authorship']=='forwarded_by_sender'
    assert 'awaiting_reply_forward_origin' not in full[plain]
    with TestClient(create_app(s,['testserver'])) as client:
        items=structured(rpc(client,key,'list_chats',{'awaiting_reply':True,'view':'conversation','include_coverage':False}))['items']
    hints={r['awaiting_reply']['message_id']:r['awaiting_reply'] for r in items}
    # Queue rows keep who wrote it (kind and name); detail=full and the message tools carry the rest.
    assert hints[forwarded]['forward_origin']=={'kind':'channel','sender_name':'Synthetic News'}
    assert hints[forwarded]['authorship']=='forwarded_by_sender'
    assert 'forward_origin' not in hints[plain] and 'authorship' not in hints[plain]


def test_forwarded_head_outside_the_context_window_keeps_attribution(forwards):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    from test_client_contract import rpc
    from test_compact_queue_rows import structured
    s,ar,_,event=forwards
    def add(mid,day,body,**metadata):
        at=f'2026-10-{day:02d}T12:00:00+00:00'
        return ar.ingest(event(account='forward-tg',chat='300',provider_id=mid,body=body,sent_at=at,updated_at=at,
            metadata={'chat_type':'private',**metadata}))['message']
    anchor=add('1',1,'synthetic old question?')
    for n in range(2,6):add(str(n),n,f'synthetic chatter {n}')
    head=add('9',9,'synthetic forwarded head',forwarded=True,forward_origin=ORIGIN)
    key=s.issue_key('forward-reader',['forward-tg'])
    with TestClient(create_app(s,['testserver'])) as client:
        for detail in ('compact','full'):
            result=structured(rpc(client,key,'get_context',{'message_id':anchor,'view':'conversation','detail':detail,
                'before_count':0,'after_count':1,'include_coverage':False}))
            assert head not in [r['id'] for r in result['context']]
            latest=result['conversation'].get('latest_message') or result['conversation']['latest_known_meaningful_message']
            assert latest['authorship']=='forwarded_by_sender',detail
            assert latest['forward_origin']['sender_name']=='Synthetic News',detail
        transcript=structured(rpc(client,key,'get_messages',{'chat_id':result['message']['chat'],'view':'conversation',
            'detail':'transcript','include_coverage':False}))['transcript']
    assert 'synthetic forwarded head' in transcript and 'переслано, автор: Synthetic News' in transcript
    assert transcript.count('переслано')==1


def test_transcript_says_the_author_of_a_forward_is_unknown(forwards):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    from messaging_service.envelope import stable_id
    from test_client_contract import rpc
    from test_compact_queue_rows import structured
    s,_,add,_=forwards
    add('1','synthetic forwarded words',forwarded=True,
        forward_origin={'kind':'unknown','forwarding_score':4,'basis':'whatsapp_web_js_message'})
    add('2','synthetic old forward',forwarded=True)
    key=s.issue_key('forward-reader',['forward-tg'])
    with TestClient(create_app(s,['testserver'])) as client:
        transcript=structured(rpc(client,key,'get_messages',{'chat_id':stable_id('chat','forward-tg','300'),
            'view':'conversation','detail':'transcript','include_coverage':False}))['transcript']
    assert transcript.count('переслано, автор неизвестен')==2


def test_sparser_recapture_keeps_the_origin_fields_already_stated(forwards):
    s,ar,add,event=forwards
    add('1','synthetic forwarded words',forwarded=True,forward_origin=ORIGIN)
    sparse={k:v for k,v in ORIGIN.items() if k not in ('sender_name','post_author')}
    again=ar.ingest(event(account='forward-tg',chat='300',provider_id='1',body='synthetic forwarded words',
        metadata={'forwarded':True,'forward_origin':sparse}))
    assert again['outcome']=='unchanged'
    key=s.issue_key('forward-reader',['forward-tg'])
    assert s.search(key,'synthetic forwarded')['items'][0]['forward_origin']==ORIGIN
    # A different stated origin replaces the old one instead of mixing two origins.
    other={'kind':'hidden_user','sender_name':'X','basis':'telegram_fwd_from'}
    ar.ingest(event(account='forward-tg',chat='300',provider_id='1',body='synthetic forwarded words',
        metadata={'forwarded':True,'forward_origin':other}))
    assert s.search(key,'synthetic forwarded')['items'][0]['forward_origin']==other


def test_reply_queue_hint_origin_is_read_for_the_hint_revision(forwards):
    from messaging_service.message_queries import hint_forward_origin
    s,ar,_,event=forwards
    at='2026-10-01T12:00:00+00:00'
    mid=ar.ingest(event(account='forward-tg',chat='301',provider_id='1',body='synthetic question?',sent_at=at,
        updated_at=at,metadata={'forwarded':True,'forward_origin':ORIGIN}))['message']
    # An edit lands between the queue read (revision 1) and the annotation: revision 2 is not forwarded.
    edited='2026-10-01T13:00:00+00:00'
    assert ar.ingest(event(account='forward-tg',chat='301',provider_id='1',body='synthetic question, edited?',
        sent_at=at,updated_at=edited,metadata={'forwarded':False}))['outcome']=='edited'
    with s.pool.connection() as c:
        stale,current=hint_forward_origin(c,[{'awaiting_reply_message_id':mid,'awaiting_reply_revision':1},
            {'awaiting_reply_message_id':mid,'awaiting_reply_revision':2}])
    assert stale['awaiting_reply_forward_origin']==ORIGIN and stale['awaiting_reply_authorship']=='forwarded_by_sender'
    assert 'awaiting_reply_forward_origin' not in current


def test_capabilities_state_the_forwarded_authorship_rule(forwards):
    s,_,_,_=forwards
    rule=s.capabilities(s.issue_key('forward-reader',['forward-tg']))['search_options']['forwarded_messages']
    assert rule['authorship']=='forwarded_by_sender' and rule['origin_field']=='forward_origin'
    assert rule['origin_grants_chat_access'] is False and 'not_captured' in rule['origin_kinds']
    assert rule['rule']=='sender_is_the_forwarder_never_quote_forwarded_words_as_written_by_sender'


def test_archive_compact_hint_bounds_the_origin(forwards):
    from datetime import timedelta
    from messaging_service.presentation import render
    s,ar,_,event=forwards
    at=(datetime.now(timezone.utc)-timedelta(hours=1)).replace(microsecond=0).isoformat()
    long={**ORIGIN,'sender_name':'N'*256,'post_author':'P'*256}
    ar.ingest(event(account='forward-tg',chat='301',provider_id='1',body='synthetic question?',sent_at=at,updated_at=at,
        metadata={'chat_type':'private','forwarded':True,'forward_origin':long}))
    key=s.issue_key('forward-queue',['forward-tg'])
    row=render('list_chats',s.list_chats(key,awaiting_reply=True,include_coverage=False),'compact')['items'][0]
    assert row['awaiting_reply_forward_origin']=={'kind':'channel','sender_name':'N'*64}
    assert row['awaiting_reply_authorship']=='forwarded_by_sender'


def test_a_conflicting_origin_id_replaces_the_stored_origin(forwards):
    s,ar,add,event=forwards
    add('1','synthetic forwarded words',forwarded=True,
        forward_origin={'kind':'user','sender_id':'111','sender_name':'Synthetic Alice','basis':'telegram_fwd_from'})
    other={'kind':'user','sender_id':'222','basis':'telegram_fwd_from'}
    assert ar.ingest(event(account='forward-tg',chat='300',provider_id='1',body='synthetic forwarded words',
        metadata={'forwarded':True,'forward_origin':other}))['outcome']=='unchanged'
    key=s.issue_key('forward-reader',['forward-tg'])
    assert s.search(key,'synthetic forwarded')['items'][0]['forward_origin']==other


def test_forwarded_group_attention_target_keeps_attribution(forwards):
    from datetime import timedelta
    s,ar,_,event=forwards
    at=(datetime.now(timezone.utc)-timedelta(hours=1)).replace(microsecond=0).isoformat()
    mid=ar.ingest(event(account='forward-tg',chat='-500',provider_id='1',body='synthetic forwarded mention',sent_at=at,
        updated_at=at,metadata={'chat_type':'group','mentions_owner':True,'forwarded':True,'forward_origin':ORIGIN}))['message']
    key=s.issue_key('forward-reader',['forward-tg'])
    target=s.get_context(key,mid,view='conversation')['conversation']['owner_attention']['target_message']
    assert target['id']==mid and target['authorship']=='forwarded_by_sender' and target['forward_origin']==ORIGIN


def test_compact_origin_names_never_serialize_as_control_escapes():
    import json
    from messaging_service.presentation import forward_fields
    row={'forwarded':True,'forward_origin':{**ORIGIN,'sender_name':'\x01'*256,'post_author':'\x1f'*256}}
    for brief in (False,True):
        origin=forward_fields(row,brief=brief)['forward_origin']
        assert origin['sender_name']=='�'*64
        # A quote or backslash (two serialized characters) is the widest name character left.
        assert len(json.dumps(origin['sender_name'],ensure_ascii=False))==66
    assert forward_fields(row)['forward_origin']['post_author']=='�'*64


def test_a_null_origin_re_read_keeps_the_stored_origin(forwards):
    s,ar,add,event=forwards
    add('1','synthetic forwarded words',forwarded=True,forward_origin=ORIGIN)
    assert ar.ingest(event(account='forward-tg',chat='300',provider_id='1',body='synthetic forwarded words',
        metadata={'forwarded':True,'forward_origin':None}))['outcome']=='unchanged'
    key=s.issue_key('forward-reader',['forward-tg'])
    assert s.search(key,'synthetic forwarded')['items'][0]['forward_origin']==ORIGIN
