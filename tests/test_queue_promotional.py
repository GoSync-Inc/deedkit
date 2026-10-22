"""ANYM-427: repeated one-way mailings from non-contacts leave the reply queue by default; a group owner request outranks them."""
import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.store import APIError
from test_client_contract import rpc
from test_queue_demotion import components, demote, structured  # noqa: F401  (demote is a fixture)


def mailing(add, chat, days, *, hours=2, **metadata):
    """One incoming message a day for `days` UTC days, the newest `hours` ago, no owner message."""
    for n in range(days):add(chat,str(n+1),hours+24*(days-1-n),body='Your statement is ready. Questions?',sender='M'+chat,**metadata)


def only_absent(queue):
    return queue(promotional='only',chat_types=['group'])


def promotional_fixture(add, history):
    mailing(add,'bank',3)
    mailing(add,'shop',1,sender_is_business_or_verified=True)
    mailing(add,'first',2)
    mailing(add,'regular',3,sender_is_contact=True)
    mailing(add,'shopc',1,sender_is_business_or_verified=True,sender_is_contact=True)
    history('shopt');add('shopt','1',4,body='Your order shipped?',sender='Mshopt',sender_is_business_or_verified=True)
    history('person');add('person','1',1,body='Where are you?',sender='Sperson')
    for n in range(3):add('group',str(n+1),3+24*n,chat_type='group',body='@owner see this',sender='G',mentions_owner=True)


# spec: ANYM-427
def test_repeated_one_way_mailings_leave_the_queue_by_default(demote):
    s,ar,key,add,history,queue,rows=demote
    promotional_fixture(add,history)
    kept=['first','regular','shopc','shopt','person','group']
    default=queue(order='priority',limit=100)
    assert sorted(r['provider_chat_id'] for r in default['items'])==sorted(kept)
    assert default['excluded_promotional_total']==2 and default['awaiting_reply_total']==len(kept)
    got={r['provider_chat_id']:r for r in default['items']}
    # A person's first messages and a business the owner knows stay, demoted as before.
    assert components(got['first'])['one_way_chat']==-60
    assert components(got['shopt'])['provider_business_or_verified']==-40
    assert all('awaiting_reply_likely_promotional' not in r for r in default['items'])
    # include returns them with the same scores and a marker outside the components; only lists just them.
    every=queue(order='priority',limit=100,promotional='include')
    assert sorted(r['provider_chat_id'] for r in every['items'])==sorted([*kept,'bank','shop'])
    assert 'excluded_promotional_total' not in every and 'excluded_promotional_total' not in only_absent(queue)
    marked={r['provider_chat_id']:r['awaiting_reply_promotional_basis'] for r in every['items'] if r.get('awaiting_reply_likely_promotional')}
    assert marked=={'bank':'repeated_days','shop':'business_sender'}
    bank=next(r for r in every['items'] if r['provider_chat_id']=='bank')
    assert components(bank)=={'private_chat':100,'question_mark':25,'one_way_chat':-60,'waiting_age':1}
    only=queue(order='priority',limit=100,promotional='only')
    assert sorted(r['provider_chat_id'] for r in only['items'])==['bank','shop'] and only['awaiting_reply_total']==2
    # The agent sees the marker in the hint.
    with TestClient(create_app(s,['testserver'])) as client:
        body=structured(rpc(client,key,'list_chats',{'awaiting_reply':True,'promotional':'include','limit':100}))
        plain=structured(rpc(client,key,'list_chats',{'awaiting_reply':True,'limit':100}))
    hints={r['id']:r['awaiting_reply'] for r in body['items']}
    assert sorted(h['promotional_basis'] for h in hints.values() if h.get('likely_promotional') is True)==['business_sender','repeated_days']
    assert plain['excluded_promotional_total']==2 and not any('likely_promotional' in r['awaiting_reply'] for r in plain['items'])


# spec: ANYM-427
def test_promotional_filter_cursor_scope_and_errors(demote):
    s,ar,key,add,history,queue,rows=demote
    promotional_fixture(add,history)
    mailing(add,'other',3,account='demote-tg2')
    page=queue(order='priority',limit=1)
    assert page['excluded_promotional_total']==3
    # Cursors are bound to the choice.
    with pytest.raises(APIError,match='invalid_cursor'):queue(order='priority',limit=1,after=page['next_cursor'],promotional='include')
    seen=[r['provider_chat_id'] for r in page['items']]
    while page['next_cursor']:
        page=queue(order='priority',limit=2,after=page['next_cursor']);seen+=[r['provider_chat_id'] for r in page['items']]
    assert sorted(seen)==sorted(['first','regular','shopc','shopt','person','group'])
    # Totals count only the caller's scope and filters, also when every row is left out.
    narrow=s.issue_key('promo-narrow',['demote-tg'])
    assert s.list_chats(narrow,awaiting_reply=True,include_coverage=False)['excluded_promotional_total']==2
    other=s.issue_key('promo-other',['demote-tg2'])
    empty=s.list_chats(other,awaiting_reply=True,include_coverage=False)
    assert empty['items']==[] and empty['awaiting_reply_total']==0 and empty['excluded_promotional_total']==1
    # Absent means none.
    assert 'excluded_promotional_total' not in queue(chat_types=['group'])
    # exclude_likely_broadcasts keeps its meaning; it does not combine with include or only.
    both=queue(exclude_likely_broadcasts=True)
    assert both['excluded_likely_broadcasts_total']==7 and both['excluded_promotional_total']==3
    for bad in ({'promotional':'include','exclude_likely_broadcasts':True},{'promotional':'only','exclude_likely_broadcasts':True},
            {'promotional':'none'}):
        with pytest.raises(APIError,match='invalid_filter'):queue(**bad)
    with pytest.raises(APIError,match='invalid_filter'):s.list_chats(key,promotional='only')
    # Unknown modes fail schema validation on MCP before the queue rules.
    with TestClient(create_app(s,['testserver'])) as client:
        bad=rpc(client,key,'list_chats',{'awaiting_reply':True,'promotional':'none'}).json()
    assert bad['error']['data']['field_errors']==[{'field':'promotional','reason':'enum','allowed':['exclude','include','only']}]
    # Outside the queue the default changes nothing.
    assert 'excluded_promotional_total' not in s.list_chats(key,include_coverage=False,limit=100)


# spec: ANYM-427
def test_group_owner_request_outranks_one_way_mailings_at_any_age(demote):
    s,ar,key,add,history,queue,rows=demote
    # The highest one-way non-contact private row: a question, the waiting cap reached.
    add('old','1',24*40,body='Your statement is ready. Questions?',sender='BANK')
    add('mid','1',240,body='Last chance?',sender='SHOP')
    add('req','1',0.2,chat_type='group',body='@owner please take a look',sender='P1',mentions_owner=True)
    add('ask','1',0.2,chat_type='group',body='@owner can you take a look?',sender='P2',mentions_owner=True)
    got={r['provider_chat_id']:r for r in queue(order='priority',limit=100,awaiting_reply_days=0,promotional='include')['items']}
    assert components(got['old'])['waiting_age_capped']==20 and got['old']['awaiting_reply_priority']==85
    assert components(got['req'])=={'group_attention':25,'owner_request':63}
    # A question gets owner_question only, and ranks at or above the same request without a question mark.
    assert 'owner_request' not in components(got['ask']) and components(got['ask'])['owner_question']==40
    assert got['ask']['awaiting_reply_priority']>=got['req']['awaiting_reply_priority']
    for chat in ('old','mid'):assert got['req']['awaiting_reply_priority']>got[chat]['awaiting_reply_priority'],chat
    order=[r['provider_chat_id'] for r in queue(order='priority',limit=100,awaiting_reply_days=0,promotional='include')['items']]
    assert order.index('req')<min(order.index('old'),order.index('mid'))


# spec: ANYM-427
def test_owner_request_is_narrow(demote):
    s,ar,key,add,history,queue,rows=demote
    def group(chat, body='@owner please send the file', sender=None, **metadata):
        add(chat,'1',5,chat_type='group',body=body,sender=sender or 'S'+chat,**{'mentions_owner':True,**metadata})
    add('o2','1',500,direction='outgoing',sender=OWNER_SENDER,account='demote-tg2',body='Synthetic owner message')
    group('plain')
    # Mention evidence is metadata; a closing courtesy that mentions the owner is not a request.
    group('thanks',body='thanks')
    group('bot',sender_is_bot=True)
    group('business',sender_is_business_or_verified=True)
    group('own',sender=OWNER_SENDER)
    # Owner decision 2026-10-22: a reply under the owner's message without a mention is not a request.
    group('reply_only',mentions_owner=False,reply_to_owner=True)
    history('priv');add('priv','1',5,body='please send the file?',sender='Spriv')
    history('privreq');add('privreq','1',5,body='please send the file',sender='Sprivreq',mentions_owner=True)
    got=rows()
    assert components(got['plain'])['owner_request']==63
    assert components(got['thanks'])['closing_courtesy']==-30
    for chat in ('thanks','bot','business','own','reply_only','priv','privreq'):
        assert 'owner_request' not in components(got[chat]),chat
    # Every private question without a demotion still outranks every group row; a private request is unchanged.
    assert got['priv']['awaiting_reply_priority']>got['plain']['awaiting_reply_priority']
    assert set(components(got['privreq']))=={'private_chat','waiting_age'}


# spec: ANYM-427
def test_muted_promotional_rows_are_neither_shown_nor_counted(demote):
    s,ar,key,add,history,queue,rows=demote
    promotional_fixture(add,history)
    bank=next(r for r in queue(promotional='only')['items'] if r['provider_chat_id']=='bank')
    s.mute_reply_source(key,chat_id=bank['id'])
    default=queue()
    assert default['excluded_promotional_total']==1 and default['muted_total']==0
    # include_muted returns muted rows; the promotional choice still applies to them.
    assert 'bank' not in [r['provider_chat_id'] for r in queue(include_muted=True)['items']]
    shown={r['provider_chat_id']:r for r in queue(include_muted=True,promotional='only')['items']}
    assert shown['bank']['awaiting_reply_muted']=='chat' and shown['bank']['awaiting_reply_likely_promotional'] is True


OWNER_SENDER='OWNER2'
