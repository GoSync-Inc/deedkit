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
    'one_way_chat','provider_business_or_verified','observed_bot','own_account','closing_courtesy'}
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
    assert set(components(by_chat['400']))=={'group_attention','question_mark','waiting_age'}
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
