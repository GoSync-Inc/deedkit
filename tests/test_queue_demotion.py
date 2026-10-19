"""ANYM-276: the reply queue demotes broadcasts, bots, own accounts and courtesies and itemizes its score."""
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from messaging_service.adapters import telegram_message, whatsapp_message
from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.collector_state import validate_capture
from messaging_service.envelope import validate
from messaging_service.store import APIError
from test_archive import event
from test_client_contract import rpc


def structured(response):
    body=response.json()
    assert 'error' not in body and not body['result'].get('isError'),body
    return body['result']['structuredContent']

CODES={'private_chat','group_attention','question_mark','provider_contact','waiting_age','waiting_age_capped',
    'one_way_chat','provider_business_or_verified','observed_bot','own_account','closing_courtesy','owner_question'}
OWNER_SENDER='OWNER2'


@pytest.fixture()
def demote(seeded):
    s,_,_=seeded
    ar=Archive(s)
    ar.register('demote-tg','telegram','Synthetic demotion',discover=True)
    ar.register('demote-tg2','telegram','Synthetic second account',discover=True)
    key=s.issue_key('demote-agent',['demote-tg','demote-tg2'])
    def add(chat, mid, hours_ago, *, direction='incoming', body='Synthetic text', sender='42', chat_type='private',
            account='demote-tg', **metadata):
        at=(datetime.now(timezone.utc)-timedelta(hours=hours_ago)).isoformat()
        return ar.ingest(event(account=account,chat=chat,provider_id=str(mid),sent_at=at,updated_at=at,direction=direction,
            body=body,sender=sender,metadata={'chat_type':chat_type,**metadata}))['message']
    def history(chat, hours_ago=400):
        add(chat,'0',hours_ago,direction='outgoing',body='Synthetic owner message',sender=OWNER_SENDER)
    def queue(**kwargs):
        return s.list_chats(key,awaiting_reply=True,include_coverage=False,**kwargs)
    def rows(**kwargs):
        return {r['provider_chat_id']:r for r in queue(order='priority',limit=100,**kwargs)['items']}
    return s,ar,key,add,history,queue,rows


def components(row):
    return row['awaiting_reply_priority_components']


# (chat, label) -> needs owner or noise; every noise row stays below 100, every needs-owner row starts at 100.
def labelled_fixture(add, history):
    labels={}
    def person(chat, hours, body, **metadata):
        history(chat);add(chat,'1',hours,body=body,sender='S'+chat,**metadata);labels[chat]='needs'
    person('p1',2,'Can you join the call tomorrow?',sender_is_contact=True)
    person('p2',30,'Mama is asking about the weekend?',sender_is_contact=True)
    person('p3',100,'Where did we leave the contract?')
    person('p4',5,'The contract is signed')
    person('p5',9,'ок, завтра в десять созвонимся')
    person('p6',20,'Thanks, can you send the file?')
    person('p7',3,'ok?')
    def noise(chat, hours, body, *, owner_history=False, **kwargs):
        if owner_history:history(chat)
        add(chat,'1',hours,body=body,sender=kwargs.pop('sender','S'+chat),**kwargs);labels[chat]='noise'
    for n,hours in enumerate((300,213,60)):noise(f'bank{n}',hours,'Your statement is ready. Questions? Call us?')
    for n,hours in enumerate((200,40,6)):
        noise(f'shop{n}',hours,'Sale today only. Reply YES?',sender_is_business_or_verified=True,sender_is_contact=bool(n%2))
    for n in range(2):
        noise(f'bot{n}',50,'Digest posted. Any ideas?',chat_type='group',sender_is_bot=True,mentions_owner=True)
    add('o2','1',500,direction='outgoing',sender=OWNER_SENDER,account='demote-tg2',body='Synthetic owner message')
    for n in range(2):noise(f'own{n}',70,'Note to self: renew the visa?',owner_history=True,sender=OWNER_SENDER)
    for n,word in enumerate(('спасибо!','Thanks','terima kasih 🙏','👍','ок')):
        noise(f'thanks{n}',10+n,word,owner_history=True)
    return labels


def test_queue_priority_components_sum_to_priority(demote):
    s,ar,key,add,history,queue,rows=demote
    labelled_fixture(add,history)
    result=queue(order='priority',limit=100)
    assert len(result['items'])>=20
    for row in result['items']:
        parts=components(row)
        assert set(parts)<=CODES and all(type(v) is int and v for v in parts.values()),parts
        assert sum(parts.values())==row['awaiting_reply_priority'],row['provider_chat_id']
        # detail=full reasons: exactly the non-zero keys, in one vocabulary.
        assert set(row['awaiting_reply_priority_reasons'])==set(parts) and len(row['awaiting_reply_priority_reasons'])==len(parts)
    with TestClient(create_app(s,['testserver'])) as client:
        full=rpc(client,key,'list_chats',{'awaiting_reply':True,'order':'priority','detail':'full','limit':100}).json()
        compact=rpc(client,key,'list_chats',{'awaiting_reply':True,'order':'priority','detail':'compact','limit':100}).json()
    full_items=full['result']['structuredContent']['items']
    assert len(full_items)>=20
    for row in full_items:
        assert sum(row['awaiting_reply_priority_components'].values())==row['awaiting_reply_priority']
        assert set(row['awaiting_reply_priority_reasons'])==set(row['awaiting_reply_priority_components'])
    # Owner decision 2026-10-18: compact stays as it was; components come with detail=full only, reasons stay everywhere.
    compact_items=compact['result']['structuredContent']['items']
    assert len(compact_items)>=20
    for row in compact_items:
        assert 'priority_components' not in row['awaiting_reply'] and row['awaiting_reply']['reasons']
    with TestClient(create_app(s,['testserver'])) as client:
        archive=structured(rpc(client,key,'list_chats',{'awaiting_reply':True,'order':'priority','view':'archive','limit':100}))
    assert all('awaiting_reply_priority_components' not in row and row['awaiting_reply_priority_reasons'] for row in archive['items'])


def test_queue_labelled_ranking_fixture(demote):
    s,ar,key,add,history,queue,rows=demote
    labels=labelled_fixture(add,history)
    assert len(labels)>=20
    ordered=[r['provider_chat_id'] for r in queue(order='priority',limit=100)['items']]
    assert sorted(ordered)==sorted(labels)
    needs=[labels[c]=='needs' for c in ordered]
    # Every needs-owner row ranks above every noise row.
    assert all(needs[:needs.count(True)]) and not any(needs[needs.count(True):]),ordered
    by_chat=rows()
    assert components(by_chat['bank1'])['one_way_chat']==-60
    assert components(by_chat['shop0'])['provider_business_or_verified']==-40
    assert components(by_chat['bot0'])['observed_bot']==-20 and 'one_way_chat' not in components(by_chat['bot0'])
    assert components(by_chat['own0'])['own_account']==-80
    assert components(by_chat['thanks0'])['closing_courtesy']==-30
    # A reply that starts with a courtesy word but asks or says more is not a closing.
    for chat in ('p5','p6','p7'):assert 'closing_courtesy' not in components(by_chat[chat])
    # A demoted private hint may rank below a group hint (ANYM-193).
    assert components(by_chat['p4'])['private_chat']==100


def test_one_way_chat_demoted_until_owner_writes(demote):
    s,ar,key,add,history,queue,rows=demote
    add('100','1',3,body='A mailing with a question?')
    before=components(rows()['100']);was=rows()['100']['awaiting_reply_priority']
    assert before['one_way_chat']==-60 and before['private_chat']==100
    # An empty or service outgoing message is not a meaningful owner message.
    add('100','2',2,direction='outgoing',body='',sender=OWNER_SENDER)
    add('100','3',2,direction='outgoing',body='Notice',sender=OWNER_SENDER,is_service=True)
    assert 'one_way_chat' in components(rows()['100'])
    # Once the owner writes, the chat is a conversation; new incoming text outranks the earlier mailing.
    add('100','4',2,direction='outgoing',body='Thank you, I will think about it',sender=OWNER_SENDER)
    add('100','5',1,body='Another question?')
    after=components(rows()['100'])
    assert 'one_way_chat' not in after and after['private_chat']==100
    assert rows()['100']['awaiting_reply_priority']>was+50


def test_unknown_provider_flags_do_not_demote(demote):
    s,ar,key,add,history,queue,rows=demote
    history('100');add('100','1',2,body='Is this fine?')
    history('200');add('200','1',2,body='Is this fine?',sender_is_business_or_verified=None)
    history('300');add('300','1',2,body='Is this fine?',sender_is_business_or_verified=False,sender_is_bot=None)
    history('400');add('400','1',2,body='Is this fine?',chat_type='group',mentions_owner=True)
    by_chat=rows()
    for chat in ('100','200','300'):
        assert set(components(by_chat[chat]))=={'private_chat','question_mark','waiting_age'},components(by_chat[chat])
    assert set(components(by_chat['400']))=={'group_attention','question_mark','waiting_age','owner_question'}
    assert by_chat['100']['awaiting_reply_priority']==125+components(by_chat['100'])['waiting_age']


def test_exclude_likely_broadcasts_filter(demote):
    s,ar,key,add,history,queue,rows=demote
    history('person');add('person','1',1,body='Where are you?')
    add('mailing','1',2,body='A mailing?')
    history('shop');add('shop','1',3,body='Sale?',sender_is_business_or_verified=True)
    history('thanks');add('thanks','1',4,body='Thanks!')
    add('other','1',1,body='Hidden mailing',account='demote-tg2')
    scoped=s.issue_key('demote-scoped',['demote-tg'])
    full=queue(order='priority')
    assert 'excluded_likely_broadcasts_total' not in full and full['awaiting_reply_total']==5
    page=queue(order='priority',limit=1,exclude_likely_broadcasts=True)
    assert page['excluded_likely_broadcasts_total']==3 and page['awaiting_reply_total']==2
    rest=queue(order='priority',limit=5,exclude_likely_broadcasts=True)
    assert [r['provider_chat_id'] for r in rest['items']]==['person','thanks']
    # The cursor is bound to the filter, and the counts never include chats outside the key's scope.
    with pytest.raises(APIError,match='invalid_cursor'):queue(order='priority',after=page['next_cursor'])
    assert queue(order='priority',limit=1,after=page['next_cursor'],exclude_likely_broadcasts=True)['items'][0]['provider_chat_id']=='thanks'
    narrow=s.list_chats(scoped,awaiting_reply=True,include_coverage=False,order='priority',exclude_likely_broadcasts=True)
    wide=queue(order='priority',exclude_likely_broadcasts=True)
    assert narrow['excluded_likely_broadcasts_total']==2 and wide['excluded_likely_broadcasts_total']==3
    only=s.issue_key('demote-other',['demote-tg2'])
    hidden=s.list_chats(only,awaiting_reply=True,include_coverage=False,exclude_likely_broadcasts=True)
    assert hidden['excluded_likely_broadcasts_total']==1 and hidden['awaiting_reply_total']==0
    with pytest.raises(APIError,match='invalid_filter'):s.list_chats(key,exclude_likely_broadcasts=True)
    with TestClient(create_app(s,['testserver'])) as client:
        body=rpc(client,key,'list_chats',{'awaiting_reply':True,'exclude_likely_broadcasts':True}).json()['result']['structuredContent']
    assert body['excluded_likely_broadcasts_total']==3


def test_courtesy_words_and_emoji_close_but_questions_do_not(demote):
    s,ar,key,add,history,queue,rows=demote
    closings=['Спасибо','спасибо большое!','Огромное спасибо вам!!','ОК','Thanks!','thank you so much','Okay, thanks',
        'terima kasih banyak','Makasih ya','🙏','👍👍','Спасибо 🙏']
    others=['Спасибо, а когда?','Thanks, send it?','ок созвонимся завтра','Thanks for the file and the long explanation yesterday','?','привет']
    for n,text in enumerate(closings+others):
        history(f'c{n}');add(f'c{n}','1',2,body=text)
    by_chat=rows()
    for n,text in enumerate(closings):assert 'closing_courtesy' in components(by_chat[f'c{n}']),text
    for n,text in enumerate(others,len(closings)):assert 'closing_courtesy' not in components(by_chat[f'c{n}']),text


def test_own_account_needs_another_connected_account(demote):
    s,ar,key,add,history,queue,rows=demote
    add('o2','1',500,direction='outgoing',sender=OWNER_SENDER,account='demote-tg2',body='Synthetic owner message')
    history('a');add('a','1',2,body='Hello from my other account',sender=OWNER_SENDER)
    history('b');add('b','1',2,body='Hello from a stranger',sender='999')
    by_chat=rows()
    assert components(by_chat['a'])['own_account']==-80 and 'own_account' not in components(by_chat['b'])
    # Outgoing history of an account outside the key's scope is not evidence.
    narrow=s.issue_key('demote-narrow',['demote-tg'])
    assert 'own_account' not in components(next(r for r in s.list_chats(narrow,awaiting_reply=True,include_coverage=False,
        order='priority')['items'] if r['provider_chat_id']=='a'))


def test_observed_bot_demotes_group_hints_and_private_bots_stay_excluded(demote):
    s,ar,key,add,history,queue,rows=demote
    add('g','1',2,body='Digest',chat_type='group',sender_is_bot=True,mentions_owner=True)
    add('h','1',2,body='Digest',chat_type='group',mentions_owner=True)
    add('p','1',2,body='Digest',sender_is_bot=True)
    by_chat=rows()
    assert components(by_chat['g'])['observed_bot']==-20 and 'observed_bot' not in components(by_chat['h'])
    assert 'p' not in by_chat


def test_telegram_verified_and_support_flags_are_captured_only_when_enabled(monkeypatch):
    from telethon import types,utils
    from test_adapters import NoRequestClient
    def captured(**flags):
        user=types.User(id=100,first_name='Synthetic',**flags)
        message=types.Message(id=19,peer_id=types.PeerUser(100),message='Sale?',from_id=types.PeerUser(100),
            date=datetime(2026,10,2,tzinfo=timezone.utc))
        message._finish_init(NoRequestClient(),{utils.get_peer_id(user):user},None)
        return telegram_message('tg-owner',message)['metadata']
    monkeypatch.delenv('ANYMSG_CAPTURE_PROVIDER_FLAGS',raising=False)
    # Off by default: a rollback server that rejects the key never meets it.
    assert 'sender_is_business_or_verified' not in captured(verified=True)
    monkeypatch.setenv('ANYMSG_CAPTURE_PROVIDER_FLAGS','1')
    assert captured(verified=True)['sender_is_business_or_verified'] is True
    assert captured(support=True)['sender_is_business_or_verified'] is True
    # An absent flag is unknown, never a statement that the sender is not verified.
    assert 'sender_is_business_or_verified' not in captured()
    assert 'sender_is_business_or_verified' not in captured(verified=False,support=False)


def test_whatsapp_business_flag_maps_to_metadata_and_unknown_stays_absent():
    base={'chatId':'1@c.us','messageId':'B1','senderId':'1@c.us','body':'Sale?','timestamp':1800000000}
    assert whatsapp_message('wa-owner',{**base,'senderIsBusiness':True})['metadata']['sender_is_business_or_verified'] is True
    assert 'sender_is_business_or_verified' not in whatsapp_message('wa-owner',base)['metadata']
    assert 'sender_is_business_or_verified' not in whatsapp_message('wa-owner',{**base,'senderIsBusiness':False})['metadata']


def test_provider_flag_is_validated_as_a_boolean_in_envelope_and_spool_event():
    base=event(metadata={'chat_type':'private','sender_is_business_or_verified':True})
    assert validate(base)['metadata']['sender_is_business_or_verified'] is True
    with pytest.raises(ValueError,match='invalid_metadata'):validate(event(metadata={'sender_is_business_or_verified':'yes'}))
    spooled={'kind':'whatsapp_event','account':'wa-owner','change':'message','origin':'live','observed_at':'2026-10-18T00:00:00Z',
        'event':{'chatId':'1@c.us','messageId':'B1','senderId':'1@c.us','body':'Sale?','timestamp':1800000000,'fromMe':False,
            'senderIsBusiness':True}}
    assert validate_capture(spooled)['envelope']['metadata']['sender_is_business_or_verified'] is True


def test_own_account_evidence_respects_the_keys_chat_scope(demote):
    from messaging_service.envelope import stable_id
    s,ar,key,add,history,queue,rows=demote
    # The matching outgoing sender exists only in chat o2 of the second account.
    add('o2','1',500,direction='outgoing',sender=OWNER_SENDER,account='demote-tg2',body='Synthetic owner message')
    history('a');add('a','1',2,body='Hello from my other account',sender=OWNER_SENDER)
    visible=[stable_id('chat','demote-tg','a')]
    scoped=s.issue_key('demote-chat-scope',['demote-tg','demote-tg2'],chats=visible)
    def component_of(token):
        item=next(r for r in s.list_chats(token,awaiting_reply=True,include_coverage=False,order='priority')['items']
            if r['provider_chat_id']=='a')
        return components(item)
    assert 'own_account' in component_of(key)
    # Hidden history neither demotes nor changes the visible row; reading its chat enables the demotion.
    assert 'own_account' not in component_of(scoped)
    readable=s.issue_key('demote-chat-scope2',['demote-tg','demote-tg2'],chats=[*visible,stable_id('chat','demote-tg2','o2')])
    assert 'own_account' in component_of(readable)


def test_scored_hint_is_the_same_with_and_without_the_awaiting_reply_filter(demote):
    s,ar,key,add,history,queue,rows=demote
    add('o2','1',500,direction='outgoing',sender=OWNER_SENDER,account='demote-tg2',body='Synthetic owner message')
    history('a');add('a','1',2,body='Hello from my other account',sender=OWNER_SENDER)
    queued=rows()['a']
    plain=next(r for r in s.list_chats(key,include_coverage=False,limit=100)['items'] if r['provider_chat_id']=='a')
    assert plain['awaiting_reply_priority']==queued['awaiting_reply_priority']
    assert components(plain)==components(queued) and 'own_account' in components(plain)
    with TestClient(create_app(s,['testserver'])) as client:
        compact=structured(rpc(client,key,'list_chats',{'limit':100}))['items']
        awaiting=structured(rpc(client,key,'list_chats',{'awaiting_reply':True,'limit':100}))['items']
    hint=lambda items:next(r['awaiting_reply'] for r in items if r['id']==queued['id'])
    assert hint(compact)['priority']==hint(awaiting)['priority'] and hint(compact)['reasons']==hint(awaiting)['reasons']


def test_excluded_total_honours_only_unread_like_the_other_totals(demote):
    from messaging_service.reply_state import observe
    s,ar,key,add,history,queue,rows=demote
    add('unread-mailing','1',3,body='Mailing?');add('read-mailing','1',3,body='Mailing?');add('unknown-mailing','1',3,body='Mailing?')
    history('person');add('person','1',3,body='Where are you?')
    def seen(chat, count):
        with s.pool.connection() as c:
            observe(c,'demote-tg',chat,{'observed_at':datetime.now(timezone.utc).isoformat(),'unread_count':count,
                'read_inbox_max_id':0,'basis':'telegram_dialog'})
    seen('unread-mailing',1);seen('read-mailing',0);seen('person',1)
    both=queue(only_unread=True,exclude_likely_broadcasts=True)
    assert [r['provider_chat_id'] for r in both['items']]==['person']
    # Only the mailing that passes the unread filter is counted as excluded; the read and unknown ones are not in the population.
    assert both['excluded_likely_broadcasts_total']==1 and both['awaiting_reply_total']==1
    assert queue(exclude_likely_broadcasts=True)['excluded_likely_broadcasts_total']==3


# spec: ANYM-276
def test_group_owner_mention_question_outranks_one_way_mailing(demote):
    s,ar,key,add,history,queue,rows=demote
    # Live shape 2026-10-19: a 10-day-old one-way private mailing with a question mark (no owner message, no contact).
    add('mailing','1',240,body='Your statement is ready. Questions? Call us?',sender='BANK')
    # Two people's questions to the owner in groups: one from a provider contact, one from a stranger.
    add('g1','1',20,chat_type='group',body='@owner can you confirm?',sender='P1',mentions_owner=True,sender_is_contact=True)
    add('g2','1',3,chat_type='group',body='@owner where is the file?',sender='P2',mentions_owner=True)
    got=rows()
    assert components(got['mailing'])['one_way_chat']==-60
    for chat in ('g1','g2'):
        assert components(got[chat])['owner_question']==40
        assert got[chat]['awaiting_reply_priority']>got['mailing']['awaiting_reply_priority'],chat
    order=[r['provider_chat_id'] for r in queue(order='priority',limit=100)['items']]
    assert order.index('mailing')>max(order.index('g1'),order.index('g2'))


# spec: ANYM-276
def test_owner_question_bonus_is_narrow(demote):
    s,ar,key,add,history,queue,rows=demote
    def group(chat, body='@owner can you confirm?', sender=None, **metadata):
        add(chat,'1',5,chat_type='group',body=body,sender=sender or 'S'+chat,**{'mentions_owner':True,**metadata})
    add('o2','1',500,direction='outgoing',sender=OWNER_SENDER,account='demote-tg2',body='Synthetic owner message')
    group('plain')
    group('statement',body='@owner the report is ready')
    group('bot',sender_is_bot=True)
    group('business',sender_is_business_or_verified=True)
    group('own',sender=OWNER_SENDER)
    group('reply_only',mentions_owner=False,reply_to_owner=True)
    add('chan','1',5,chat_type='channel',body='Weekly digest, questions?',sender='S',mentions_owner=True)
    history('priv');add('priv','1',5,body='@owner can you confirm?',sender='Spriv',mentions_owner=True)
    got=rows()
    assert components(got['plain'])['owner_question']==40
    for chat in ('statement','bot','business','own','reply_only','priv'):
        assert 'owner_question' not in components(got[chat]),chat
    assert 'chan' not in got
    # Every private question without a demotion still outranks the best group hint, bonus included.
    assert got['priv']['awaiting_reply_priority']>got['plain']['awaiting_reply_priority']


# spec: ANYM-276
def test_priority_cursor_from_another_scoring_formula_is_rejected(demote, monkeypatch):
    s,ar,key,add,history,queue,rows=demote
    from messaging_service import chat_queries
    add('mailing','1',240,body='Your statement is ready. Questions?',sender='BANK')
    add('g1','1',20,chat_type='group',body='@owner can you confirm?',sender='P1',mentions_owner=True)
    add('g2','1',3,chat_type='group',body='@owner where is the file?',sender='P2',mentions_owner=True)
    page=queue(order='priority',limit=1)
    # Same formula: the cursor continues the traversal without losing a row.
    seen=[r['provider_chat_id'] for r in page['items']]
    while page['next_cursor']:
        page=queue(order='priority',limit=1,after=page['next_cursor']);seen+=[r['provider_chat_id'] for r in page['items']]
    assert sorted(seen)==['g1','g2','mailing']
    # A cursor minted before the formula changed would drop rows whose score rose past its boundary: restart instead.
    monkeypatch.setattr(chat_queries,'SCORE_FORMULA',1)
    old=queue(order='priority',limit=1)['next_cursor']
    monkeypatch.undo()
    with pytest.raises(APIError,match='invalid_cursor'):
        queue(order='priority',limit=1,after=old)
