"""Personal reply queue: explicit group attention only, using synthetic sources."""
import pytest
from hypothesis import HealthCheck,given,settings,strategies as st
from psycopg.types.json import Jsonb

from messaging_service.archive import Archive
from messaging_service.envelope import stable_id,validate
from messaging_service.presentation import render
from test_archive import event


@pytest.fixture()
def groups(seeded):
    s,_,_=seeded
    ar=Archive(s)
    ar.register('reply-tg','telegram','Synthetic groups',discover=True)
    key=s.issue_key('reply-reader',['reply-tg'])
    def add(chat,mid,second,**changes):
        at=f'2026-10-01T12:00:{second:02d}Z'
        result=ar.ingest(event(**{'account':'reply-tg','chat':chat,'provider_id':mid,
            'sent_at':at,'updated_at':at,**changes}))
        with s.pool.connection() as c:
            c.execute("UPDATE chats SET chat_type='group' WHERE id=%s",(stable_id('chat','reply-tg',chat),))
        return result['message']
    return s,key,add


def test_group_mentions_survive_other_peoples_chatter(groups):
    s,key,add=groups
    target=add('100','1',1,body='Owner, please check',metadata={'mentions_owner':True})
    noise=add('100','2',2,body='Unrelated conversation')
    add('200','1',3,body='Just a group message')
    result=s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False)
    assert result['awaiting_reply_total']==1
    row=result['items'][0]
    assert row['awaiting_reply_message_id']==target
    assert row['last_message_id']==noise
    assert row['awaiting_reply_basis']=='owner_mention'
    assert result['awaiting_reply_scope']=='private_and_explicit_group_attention'
    compact=render('list_chats',result,'compact')['items'][0]
    assert compact['awaiting_reply_basis']=='owner_mention'
    assert compact['awaiting_reply_message_id']==target


def test_only_replies_to_owners_archived_message_enter_queue(groups):
    s,key,add=groups
    add('100','10',1,direction='outgoing')
    target=add('100','11',2,metadata={'reply_to_provider_id':'10'})
    add('100','12',3)
    add('200','10',1,direction='incoming')
    add('200','11',2,metadata={'reply_to_provider_id':'10'})
    add('300','11',2,metadata={'reply_to_provider_id':'10'})
    result=s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False)
    assert result['awaiting_reply_total']==1
    assert result['items'][0]['awaiting_reply_message_id']==target
    assert result['items'][0]['awaiting_reply_basis']=='reply_to_owner_message'


def test_owner_message_clears_group_hint_but_new_mention_reopens(groups):
    s,key,add=groups
    add('100','1',1,metadata={'mentions_owner':True})
    add('100','2',2,direction='outgoing')
    assert not s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False)['items']
    target=add('100','3',3,metadata={'mentions_owner':True})
    assert s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False)['items'][0]['awaiting_reply_message_id']==target


def test_attention_respects_chat_scope_channel_type_deletion_and_revision(groups):
    s,key,add=groups
    add('100','1',1,metadata={'mentions_owner':True})
    channel=add('200','1',2,metadata={'mentions_owner':True})
    removed=add('300','1',3,metadata={'mentions_owner':True})
    stale=add('400','1',4,metadata={'mentions_owner':True})
    with s.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type='channel' WHERE id=(SELECT chat FROM messages WHERE id=%s)",(channel,))
        c.execute('UPDATE messages SET deleted=true WHERE id=%s',(removed,))
        c.execute('UPDATE message_metadata SET revision=0 WHERE message=%s',(stale,))
    s.set_access('reply-reader',chats=[stable_id('chat','reply-tg',x) for x in ['200','300','400']])
    assert not s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False)['items']


def test_mention_metadata_requires_boolean():
    assert validate(event(metadata={'mentions_owner':True}))['metadata']['mentions_owner'] is True
    for value in ['true',1,[],{}]:
        with pytest.raises(ValueError,match='invalid_metadata'):
            validate(event(metadata={'mentions_owner':value}))


def test_partial_reobservation_does_not_erase_known_owner_attention(groups):
    s,key,add=groups
    mid=add('100','1',1,metadata={'mentions_owner':True})
    add('100','1',1,metadata={'mentions_owner':None,'forwarded':False})
    with s.pool.connection() as c:
        assert c.execute('SELECT metadata FROM message_metadata WHERE message=%s',(mid,)).fetchone()['metadata']['mentions_owner'] is True
    assert s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False)['awaiting_reply_total']==1


def test_older_metadata_cannot_erase_a_newer_owner_observation(groups):
    s,key,add=groups
    add('100','1',1,updated_at='2026-10-01T12:01:00Z',metadata={'mentions_owner':True})
    add('100','1',1,metadata={'mentions_owner':False})
    assert s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False)['awaiting_reply_total']==1


# ANYM-915: one synthetic group message. A reply names the owner's (outgoing) message, another incoming one, the same
# provider id in a twin chat, or nothing that exists; a stanza or the full WhatsApp id; metadata may be stale.
attention_message=st.fixed_dictionaries({
    'outgoing':st.booleans(),'second':st.integers(0,6),'empty':st.sampled_from([False,False,False,True]),
    'deleted':st.sampled_from([False,False,False,False,True]),'prefix':st.sampled_from(['true_','false_']),
    'mention':st.sampled_from([None,None,True,False,'true']),'reply_to_owner':st.sampled_from([None,None,True,False]),
    'reply':st.sampled_from([None,None,'target','target','twin','missing']),'target':st.integers(0,13),
    'stanza':st.booleans(),'stale':st.sampled_from([False,False,False,True]),'metadata':st.booleans()})
attention_runs=iter(range(10**6))


def seed_attention_group(s,platform,messages):
    """A group and its twin (same account, same provider ids) from generated messages; direct rows, no ingest."""
    run=next(attention_runs);account='telegram-demo' if platform=='telegram' else 'whatsapp-demo-1'
    chat,twin=f'ha-{run}',f'ha-{run}-twin'
    provider={chat:f'-1009{run}',twin:f'-1008{run}'} if platform=='telegram' else {
        chat:f'1203639{run}@g.us',twin:f'1203638{run}@g.us'}
    def pid(where,n,m):
        if platform=='telegram':return str(n+1)
        return (m['prefix'] if m['outgoing'] else 'false_')+provider[where]+f'_S{n}'+('' if m['outgoing'] else '_7@lid')
    with s.pool.connection() as c:
        for where in (chat,twin):
            c.execute("INSERT INTO chats(id,account,provider_chat,title,chat_type) VALUES(%s,%s,%s,'Synthetic group','group')",
                (where,account,provider[where]))
        for n,m in enumerate(messages):
            for where in (chat,twin):
                # The twin holds the same provider ids, all outgoing: a reply must never match across chats.
                outgoing=m['outgoing'] if where==chat else True
                c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction,deleted,revision)
                    VALUES(%s,%s,%s,%s,%s,%s,timestamptz '2026-10-01T12:00:00Z'+%s*interval '1 second',%s,%s,%s)''',
                    (f'{where}-{n}',account,where,pid(where,n,{**m,'outgoing':outgoing}),'owner' if outgoing else f'peer-{n%3}',
                     '' if m['empty'] and where==chat else f'Synthetic message {n}',m['second'],
                     'outgoing' if outgoing else 'incoming',m['deleted'] and where==chat,2 if m['stale'] else 1))
            if not m['metadata']:continue
            meta={}
            if m['mention'] is not None:meta['mentions_owner']=m['mention']
            if m['reply_to_owner'] is not None:meta['reply_to_owner']=m['reply_to_owner']
            if m['reply']:
                target=m['target']%len(messages)
                if m['reply']=='missing':meta['reply_to_provider_id']='404404' if platform=='telegram' else 'NOSUCH'
                elif platform=='telegram':meta['reply_to_provider_id']=str(target+1)
                else:meta['reply_to_provider_id']=(f'S{target}' if m['stanza'] else
                    pid(twin if m['reply']=='twin' else chat,target,messages[target]))
            c.execute('INSERT INTO message_metadata(message,revision,metadata) VALUES(%s,1,%s)',(f'{chat}-{n}',Jsonb(meta)))
    return account,chat


def attention_answers(s,key,account,chat):
    from messaging_service.chat_queries import list_chats
    from messaging_service.conversation_state import conversation_state,conversation_states
    answers=[]
    with s.authorized(key,'list_chats') as (c,p):
        answers.append(conversation_state(c,p,account,chat))
        answers.append(conversation_states(c,p,[(account,chat)]))
        for view,waiting in (('conversation',False),('conversation',True),('archive',False),('archive',True)):
            answers.append(list_chats(s,c,p,'',25,order='activity',view=view,include_coverage=False,
                awaiting_reply=waiting,awaiting_reply_days=0)['items'])
    return answers


# spec: ANYM-915
@settings(max_examples=40,deadline=None,suppress_health_check=[HealthCheck.function_scoped_fixture,HealthCheck.too_slow])
@given(platform=st.sampled_from(['telegram','whatsapp']),messages=st.lists(attention_message,min_size=1,max_size=14),
    window=st.sampled_from([0,1,3,100]),owned=st.sampled_from([0,2,200]))
def test_group_attention_candidates_answer_as_the_history_walk(seeded,platform,messages,window,owned):
    from group_attention_legacy import legacy_group_thread_query
    from messaging_service import chat_queries,conversation_state
    s,owner,_=seeded
    account,chat=seed_attention_group(s,platform,messages)
    restricted=s.issue_key(f'attention-{chat}',[account],chats=[chat])
    with pytest.MonkeyPatch.context() as patched:
        # The bounds decide the route (the window, the candidates, the full walk); every route answers as today's walk.
        patched.setattr(conversation_state,'ATTENTION_WINDOW',window,raising=False)
        patched.setattr(conversation_state,'ATTENTION_OWNER_MESSAGES',owned,raising=False)
        live=[attention_answers(s,key,account,chat) for key in (owner,restricted)]
        patched.setattr(conversation_state,'group_thread_query',legacy_group_thread_query)
        patched.setattr(chat_queries,'group_thread_query',legacy_group_thread_query)
        old=[attention_answers(s,key,account,chat) for key in (owner,restricted)]
    from test_search_performance import timeless
    assert timeless(live)==timeless(old)


def test_whatsapp_stanza_reply_matches_only_same_account_and_group(seeded):
    s,_,_=seeded;ar=Archive(s)
    for account in ['reply-wa','other-wa']:
        ar.register(account,'whatsapp','Synthetic WA',discover=True)
    def add(account,chat,provider,direction,at,metadata=None):
        ar.ingest(event(account=account,platform='whatsapp',chat=chat,provider_id=provider,
            direction=direction,sent_at=at,updated_at=at,metadata=metadata or {}))
        with s.pool.connection() as c:c.execute("UPDATE chats SET chat_type='group' WHERE id=%s",(stable_id('chat',account,chat),))
    add('reply-wa','123@g.us','true_123@g.us_ABC_42@lid','outgoing','2026-10-01T12:00:01Z')
    add('reply-wa','123@g.us','false_123@g.us_DEF_43@lid','incoming','2026-10-01T12:00:02Z',{'reply_to_provider_id':'ABC'})
    add('reply-wa','999@g.us','false_999@g.us_GHI','incoming','2026-10-01T12:00:03Z',{'reply_to_provider_id':'ABC'})
    add('other-wa','123@g.us','false_123@g.us_JKL','incoming','2026-10-01T12:00:04Z',{'reply_to_provider_id':'ABC'})
    key=s.issue_key('wa-reply-reader',['reply-wa','other-wa'])
    rows=s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False)['items']
    assert len(rows)==1 and rows[0]['account']=='reply-wa'
    assert rows[0]['provider_chat_id']=='123@g.us'
