"""Readable private chat titles when the provider title is only its chat ID."""
from datetime import datetime, timedelta, timezone

import pytest

from messaging_service.archive import Archive
from messaging_service.presentation import render
from test_archive import event

LID = '48000000000001@lid'


def peer_chat(seeded):
    s,_,_=seeded
    ar=Archive(s);ar.register('title-wa','whatsapp','Synthetic titles',discover=True)
    key=s.issue_key('title-agent',['title-wa'])
    base=datetime.now(timezone.utc)-timedelta(hours=3)
    def add(n,direction,name):
        at=(base+timedelta(minutes=n)).isoformat()
        prefix='true_' if direction=='outgoing' else 'false_'
        return ar.ingest(event(account='title-wa',platform='whatsapp',chat=LID,chat_title=LID,
            provider_id=prefix+LID+'_SYNTHETIC'+str(n),sender='owner@c.us' if direction=='outgoing' else LID,
            body='Synthetic message '+str(n),sent_at=at,updated_at=at,direction=direction,
            sender_profile={'display_name':name,'username':''},metadata={'chat_type':'private'}))['message']
    add(1,'incoming','Synthetic Peer')
    add(2,'outgoing','Synthetic Owner')
    return s,key


def test_private_chat_titled_by_its_id_shows_latest_incoming_sender_name(seeded):
    s,key=peer_chat(seeded)
    row=s.list_chats(key,include_coverage=False)['items'][0]
    assert row['title']=='Synthetic Peer' and row['title_basis']=='latest_incoming_sender_name'
    assert row['provider_chat_id']==LID
    assert render('list_chats',s.list_chats(key,include_coverage=False),'compact')['items'][0]['title']=='Synthetic Peer'
    assert [r['id'] for r in s.list_chats(key,query='peer',include_coverage=False)['items']]==[row['id']]
    assert s.list_chats(key,query='owner',include_coverage=False)['items']==[]
    messages=s.get_messages(key,row['id'])['items']
    assert {m['chat_title'] for m in messages}=={'Synthetic Peer'}


def test_named_chat_keeps_provider_title(seeded):
    s,key=peer_chat(seeded)
    with s.pool.connection() as c:c.execute("UPDATE chats SET title='Synthetic provider title' WHERE account='title-wa'")
    row=s.list_chats(key,include_coverage=False)['items'][0]
    assert row['title']=='Synthetic provider title' and 'title_basis' not in row


def test_self_chat_titled_by_its_lid_is_labelled_saved_messages(seeded):
    s,_,_=seeded
    ar=Archive(s);ar.register('title-self','whatsapp','Synthetic self',discover=True)
    key=s.issue_key('title-self-agent',['title-self'])
    own='48000000000002@lid'
    at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    # The owner's own LID chat: only its messages carry the self-chat marker,
    # and an own-device copy must not rename it after the owner.
    for n,direction in ((1,'outgoing'),(2,'incoming')):
        ar.ingest(event(account='title-self',platform='whatsapp',chat=own,chat_title=own,
            provider_id=('true_' if direction=='outgoing' else 'false_')+own+'_SELF'+str(n),sender=own,
            body='Synthetic note '+str(n),sent_at=at,updated_at=at,direction=direction,
            sender_profile={'display_name':'Synthetic Owner','username':''},
            metadata={'chat_type':'private','is_self_chat':True}))
    row=s.list_chats(key,include_coverage=False)['items'][0]
    assert row['title']=='Saved messages' and row['title_basis']=='self_chat'
    assert {m['chat_title'] for m in s.get_messages(key,row['id'])['items']}=={'Saved messages'}


PHONE_TITLE = '+48 000 000 003'


def titled_chat(seeded,account,chat,*,chat_type='private',self_metadata=False,self_observation=False):
    s,_,_=seeded
    ar=Archive(s);ar.register(account,'whatsapp','Synthetic '+account,discover=True)
    key=s.issue_key(account+'-agent',[account])
    at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    metadata={'chat_type':chat_type,**({'is_self_chat':True} if self_metadata else {})}
    mid=ar.ingest(event(account=account,platform='whatsapp',chat=chat,chat_title=PHONE_TITLE,
        provider_id='true_'+chat+'_TITLED1',sender='48000000000003@lid',body='Synthetic titled note',
        sent_at=at,updated_at=at,direction='outgoing',sender_profile={'display_name':'Synthetic Owner','username':''},
        metadata=metadata))['message']
    if self_observation:
        from messaging_service.reply_state import observe
        with s.pool.connection() as c:
            observe(c,account,chat,{'observed_at':at,'basis':'whatsapp_cached_chat','is_self_chat':True})
    return s,key,mid


def chat_titles(s,key,mid):
    row=s.list_chats(key,include_coverage=False)['items'][0]
    return (row,{m['chat_title'] for m in s.get_messages(key,row['id'])['items']},
        {m['chat_title'] for m in s.search(key)['items']},s.get_context(key,mid)['message']['chat_title'])


# spec: ANYM-361
def test_self_chat_with_a_phone_number_title_is_labelled_saved_messages(seeded):
    for account,evidence in (('title-self-md',{'self_metadata':True}),('title-self-ro',{'self_observation':True})):
        s,key,mid=titled_chat(seeded,account,'48000000000003@lid',**evidence)
        row,messages,found,context=chat_titles(s,key,mid)
        assert row['title']=='Saved messages' and row['title_basis']=='self_chat'
        assert messages==found=={'Saved messages'} and context=='Saved messages'
        # Presentation only: the stored title and the provider ID are unchanged.
        assert row['provider_chat_id']=='48000000000003@lid'
        with s.pool.connection() as c:
            assert c.execute('SELECT title FROM chats WHERE id=%s',(row['id'],)).fetchone()['title']==PHONE_TITLE
        # Title matching still uses the stored title.
        assert [r['id'] for r in s.list_chats(key,query='000 003',include_coverage=False)['items']]==[row['id']]
        # A provider reference names the chat like list_chats does.
        resolved=s.resolve_reference(key,account,'48000000000003@lid','true_48000000000003@lid_TITLED1')
        assert resolved['chat']['title']=='Saved messages' and resolved['chat']['title_basis']=='self_chat'
        assert resolved['message']['chat_title']=='Saved messages'


# spec: ANYM-361
def test_private_chat_with_a_phone_number_title_and_no_self_evidence_keeps_it(seeded):
    s,key,mid=titled_chat(seeded,'title-plain','48000000000004@lid')
    row,messages,found,context=chat_titles(s,key,mid)
    assert row['title']==PHONE_TITLE and 'title_basis' not in row
    assert messages==found=={PHONE_TITLE} and context==PHONE_TITLE
    resolved=s.resolve_reference(key,'title-plain','48000000000004@lid')['chat']
    assert resolved['title']==PHONE_TITLE and 'title_basis' not in resolved


# spec: ANYM-361
def test_self_chat_title_query_matching_is_unchanged(seeded):
    s,_,_=seeded
    ar=Archive(s);ar.register('title-self-query','whatsapp','Synthetic self query',discover=True)
    key=s.issue_key('title-self-query-agent',['title-self-query'])
    own='48000000000005@lid'
    at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    ar.ingest(event(account='title-self-query',platform='whatsapp',chat=own,chat_title=own,
        provider_id='false_'+own+'_SELF1',sender=own,body='Synthetic note',sent_at=at,updated_at=at,
        direction='incoming',sender_profile={'display_name':'Synthetic Owner','username':''},
        metadata={'chat_type':'private','is_self_chat':True}))
    chat=s.list_chats(key,include_coverage=False)['items'][0]
    assert chat['title']=='Saved messages'
    # The stored title and the raw-title sender-name fallback still match; the label is no search alias.
    for query in ('000005@lid','synthetic owner'):
        assert [r['id'] for r in s.list_chats(key,query=query,include_coverage=False)['items']]==[chat['id']]
    assert s.list_chats(key,query='saved messages',include_coverage=False)['items']==[]


# spec: ANYM-361
def test_group_chat_with_stray_self_chat_metadata_keeps_its_title(seeded):
    s,key,mid=titled_chat(seeded,'title-group','120000000000000001@g.us',chat_type='group',self_metadata=True)
    row,messages,found,context=chat_titles(s,key,mid)
    assert row['title']==PHONE_TITLE and 'title_basis' not in row
    assert messages==found=={PHONE_TITLE} and context==PHONE_TITLE


# ANYM-425: the ANYM-279 sender-name order for the title of a private WhatsApp chat titled by its raw ID.

@pytest.fixture
def wa_titles(seeded):
    s,*_=seeded;ar=Archive(s)
    ar.register('wa-titles','whatsapp','Synthetic titles',discover=True)
    key=s.issue_key('titles-reader',['wa-titles'])
    base=datetime.now(timezone.utc)-timedelta(hours=3)
    def say(mid,chat,sender,name='',pn=None,mine=False):
        at=(base+timedelta(minutes=mid)).isoformat()
        message=ar.ingest(event(account='wa-titles',platform='whatsapp',chat=chat,chat_title=chat,
            provider_id=('true_' if mine else 'false_')+chat+'_T'+str(mid),sender=sender,body='Synthetic note '+str(mid),
            sent_at=at,updated_at=at,direction='outgoing' if mine else 'incoming',
            sender_profile={'display_name':name,'username':''},
            metadata={'chat_type':'group' if chat.endswith('@g.us') else 'private'}))['message']
        if pn:
            with s.pool.connection() as c:
                c.execute("INSERT INTO whatsapp_identity_observations(account,source_message,identity_id,pn,lid,provenance,"
                    "first_observed_at,observed_at) VALUES('wa-titles',%s,%s,%s,%s,'webjs_contact_cache_v1',now(),now())",
                    (message,sender,pn,sender))
        return message
    def chat_row(provider_chat,reader=None):
        rows=s.list_chats(reader or key,include_coverage=False,limit=100)['items']
        return next(r for r in rows if r['provider_chat_id']==provider_chat)
    return s,key,say,chat_row


def surfaces(s,key,message,row):
    """Every surface that names the chat of one message, as (title, title_basis or None) pairs and message titles."""
    resolved=s.resolve_reference(key,'wa-titles',row['provider_chat_id'])['chat']
    titles={row['title'],resolved['title'],s.get_context(key,message)['message']['chat_title'],
        *{m['chat_title'] for m in s.get_messages(key,row['id'])['items']},
        *{m['chat_title'] for m in s.search(key,chat=row['id'])['items']}}
    return titles,{row.get('title_basis'),resolved.get('title_basis')}


# spec: ANYM-425
def test_raw_titled_private_chat_takes_a_name_seen_in_another_readable_chat(wa_titles):
    s,key,say,chat_row=wa_titles
    own=say(1,'123@c.us','123@c.us')
    say(2,'777@g.us','123@c.us','Synthetic Bob')
    row=chat_row('123@c.us')
    assert row['title']=='Synthetic Bob' and row['title_basis']=='seen_elsewhere'
    assert surfaces(s,key,own,row)==({'Synthetic Bob'},{'seen_elsewhere'})
    assert s.resolve_reference(key,'wa-titles','123@c.us','false_123@c.us_T1')['message']['chat_title']=='Synthetic Bob'
    # The stored title stays raw and the fallback name is no search alias.
    assert [r['id'] for r in s.list_chats(key,query='123',include_coverage=False)['items']]==[row['id']]
    assert s.list_chats(key,query='Bob',include_coverage=False)['items']==[]
    with s.pool.connection() as c:
        assert c.execute('SELECT title FROM chats WHERE id=%s',(row['id'],)).fetchone()['title']=='123@c.us'


# spec: ANYM-425
def test_name_in_an_unreadable_chat_is_not_a_title_source(wa_titles):
    s,key,say,chat_row=wa_titles
    own=say(1,'123@c.us','123@c.us')
    say(2,'777@g.us','123@c.us','Synthetic Bob')
    narrow=s.issue_key('titles-narrow',['wa-titles'],chats=[chat_row('123@c.us')['id']])
    row=chat_row('123@c.us',narrow)
    assert row['title']=='+123' and row['title_basis']=='phone_number'
    assert s.get_context(narrow,own)['message']['chat_title']=='+123'


# spec: ANYM-425
def test_peer_named_through_a_visible_link_is_titled_with_the_linked_name(wa_titles):
    s,key,say,chat_row=wa_titles
    say(1,'555@c.us','555@c.us')
    say(2,'777@g.us','900@lid','Synthetic Carol',pn='555@c.us')
    row=chat_row('555@c.us')
    assert row['title']=='Synthetic Carol' and row['title_basis']=='linked_identity'


# spec: ANYM-425
def test_phone_chat_without_a_name_is_titled_by_its_number_and_a_lid_chat_is_not(wa_titles):
    s,key,say,chat_row=wa_titles
    phone=say(1,'123:7@c.us','123:7@c.us')
    lid_message=say(2,LID,LID)
    row=chat_row('123:7@c.us')
    assert row['title']=='+123' and row['title_basis']=='phone_number'
    assert surfaces(s,key,phone,row)==({'+123'},{'phone_number'})
    lid=chat_row(LID)
    assert lid['title']==LID and 'title_basis' not in lid
    assert surfaces(s,key,lid_message,lid)==({LID},{None})


# spec: ANYM-425
def test_in_chat_name_wins_but_a_raw_in_chat_name_counts_as_absent(wa_titles):
    s,key,say,chat_row=wa_titles
    say(1,'123@c.us','123@c.us','Synthetic Dave')
    say(2,'777@g.us','123@c.us','Synthetic Other')
    row=chat_row('123@c.us')
    assert row['title']=='Synthetic Dave' and row['title_basis']=='latest_incoming_sender_name'
    resolved=s.resolve_reference(key,'wa-titles','123@c.us')['chat']
    assert resolved['title']=='Synthetic Dave' and resolved['title_basis']=='latest_incoming_sender_name'
    say(3,'456@c.us','456@c.us','+456')
    say(4,'888@g.us','456@c.us','Synthetic Erin')
    erin=chat_row('456@c.us')
    assert erin['title']=='Synthetic Erin' and erin['title_basis']=='seen_elsewhere'
    say(5,'789@c.us','789@c.us','789')
    nameless=chat_row('789@c.us')
    assert nameless['title']=='+789' and nameless['title_basis']=='phone_number'


# spec: ANYM-425
def test_a_name_seen_only_on_the_own_outgoing_messages_is_never_a_title(wa_titles):
    s,key,say,chat_row=wa_titles
    say(1,'700@c.us','700@c.us')
    say(2,'777@g.us','700@c.us','Synthetic Owner',mine=True)
    row=chat_row('700@c.us')
    assert row['title']=='+700' and row['title_basis']=='phone_number'


# spec: ANYM-425
def test_unparsable_peer_keeps_its_title_and_raises_nothing(wa_titles):
    s,key,say,chat_row=wa_titles
    say(1,'123@c.us','123@c.us')
    with s.pool.connection() as c:
        c.execute("INSERT INTO chats(id,account,provider_chat,title,chat_type) VALUES('odd-1','wa-titles','not-an-id','not-an-id','private')")
    row=chat_row('not-an-id')
    assert row['title']=='not-an-id' and 'title_basis' not in row
    assert s.resolve_reference(key,'wa-titles','not-an-id')['chat']['title']=='not-an-id'


# spec: ANYM-425
def test_conversation_view_and_updates_carry_the_same_title(wa_titles):
    s,key,say,chat_row=wa_titles
    say(1,'123@c.us','123@c.us')
    say(2,'777@g.us','123@c.us','Synthetic Bob')
    rows=s.list_chats(key,view='conversation',include_coverage=False,limit=100)['items']
    row=next(r for r in rows if r['provider_chat_id']=='123@c.us')
    assert row['title']=='Synthetic Bob' and row['title_basis']=='seen_elsewhere'
    from messaging_service.presentation import render
    lean=next(r for r in render('list_chats',{'items':rows},'compact')['items'] if r['id']==row['id'])
    assert lean['title']=='Synthetic Bob' and lean['title_basis']=='seen_elsewhere'
    updates=s.get_updates(key,'titles-consumer')['items']
    assert {u['chat_title'] for u in updates if u['chat']==row['id']}=={'Synthetic Bob'}


class Counting:
    def __init__(self,c):self.connection=c;self.statements=0
    def execute(self,sql,args=None):
        self.statements+=1
        return self.connection.execute(sql,args)
    def __getattr__(self,name):return getattr(self.connection,name)


def title_pages(s,account,size,*,raw):
    """Statements of list_chats pages in both views and of a search page over `size` private chats."""
    ar=Archive(s);ar.register(account,'whatsapp','Synthetic '+account,discover=True)
    key=s.issue_key(account+'-reader',[account])
    at=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
    for n in range(size):
        peer=str(480000000000+n)+'@c.us'
        ar.ingest(event(account=account,platform='whatsapp',chat=peer,chat_title=peer if raw else 'Synthetic stored '+str(n),
            provider_id='false_'+peer+'_S1',sender=peer,body='Synthetic hello?',sent_at=at,updated_at=at,direction='incoming',
            sender_profile={'display_name':'','username':''},metadata={'chat_type':'private'}))
        ar.ingest(event(account=account,platform='whatsapp',chat='9000'+str(n)+'@g.us',chat_title='Synthetic group',
            provider_id='false_g'+str(n)+'_S1',sender=peer,body='Synthetic group note',sent_at=at,updated_at=at,
            direction='incoming',sender_profile={'display_name':'Synthetic Name '+str(n),'username':''},
            metadata={'chat_type':'group'}))
    from messaging_service.chat_queries import list_chats
    from messaging_service.message_queries import search
    counts={}
    for label,call in (
            ('archive',lambda c,p:list_chats(s,c,p,limit=size,include_coverage=False,chat_types=['private'])),
            ('conversation',lambda c,p:list_chats(s,c,p,limit=size,include_coverage=False,chat_types=['private'],
                view='conversation')),
            ('search',lambda c,p:search(s,c,p,'hello',limit=size,include_coverage=False))):
        with s.authorized(key,'search' if label=='search' else 'list_chats') as (c,p):
            counting=Counting(c);page=call(counting,p)
        assert len(page['items'])==size
        counts[label]=counting.statements
        if raw:
            assert {i.get('chat_title') or i.get('title') for i in page['items']}=={
                'Synthetic Name '+str(n) for n in range(size)}
    return counts


# spec: ANYM-425
def test_title_fallback_adds_the_same_number_of_statements_at_every_page_size(seeded):
    s,*_=seeded
    difference={}
    for size in (5,25):
        raw=title_pages(s,'wa-raw-'+str(size),size,raw=True)
        plain=title_pages(s,'wa-plain-'+str(size),size,raw=False)
        difference[size]={view:raw[view]-plain[view] for view in raw}
    assert difference[5]==difference[25]
    assert all(value>0 for value in difference[5].values())
