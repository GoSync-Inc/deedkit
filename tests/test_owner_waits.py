"""ANYM-537: list_chats(awaiting_their_reply=true, order='waiting_longest') lists the owner's unanswered private runs."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.receipts import record
from messaging_service.store import APIError
from test_archive import event
from test_client_contract import rpc

ROOT=Path(__file__).resolve().parents[1]
OWNER='OWNER1'
# The reply queue's fixtures (test_queue_demotion.labelled_fixture): closing courtesies and a question.
COURTESIES=('спасибо!','Thanks','terima kasih 🙏','👍','ок')
QUESTION='Can you send the file?'
QUEUE={'awaiting_their_reply':True,'order':'waiting_longest'}


def structured(response):
    body=response.json()
    assert 'error' not in body and not body['result'].get('isError'),body
    return body['result']['structuredContent']


@pytest.fixture()
def waits(seeded):
    s,_,_=seeded
    ar=Archive(s)
    ar.register('wait-tg','telegram','Synthetic waits',discover=True)
    ar.register('wait-wa','whatsapp','Synthetic waits',discover=True)
    key=s.issue_key('wait-agent',['wait-tg','wait-wa'])
    now=datetime.now(timezone.utc).replace(microsecond=0)
    def add(chat, mid, hours, direction='outgoing', body='Synthetic message', *, account='wait-tg', sender=None,
            seconds=0, title=None, **metadata):
        at=(now-timedelta(hours=hours)+timedelta(seconds=seconds)).isoformat()
        sender=sender or (OWNER if direction=='outgoing' else 'P'+chat.split('@')[0])
        metadata={'chat_type':'private',**metadata}
        return ar.ingest(event(account=account,platform='whatsapp' if account.startswith('wait-wa') else 'telegram',
            chat=chat,chat_title=title or 'Synthetic '+chat,provider_id=str(mid),sender=sender,sent_at=at,updated_at=at,
            direction=direction,body=body,sender_profile={'display_name':'Synthetic '+sender},metadata=metadata))['message']
    def listed(token=key, **kwargs):
        return s.list_chats(token,include_coverage=False,**{'limit':100,**QUEUE,**kwargs})
    def rows(**kwargs):
        return {r['provider_chat_id']:r for r in listed(**kwargs)['items']}
    return s,ar,key,now,add,listed,rows


def at(now, hours):
    return now-timedelta(hours=hours)


def pair(s, account, lid, pn, source):
    """A verified visible PN/LID link of one WhatsApp peer (ANYM-278), as the collector records it."""
    with s.pool.connection() as c:
        c.execute('''INSERT INTO whatsapp_identity_observations(account,source_message,identity_id,pn,lid,provenance,
            first_observed_at,observed_at) VALUES(%s,%s,%s,%s,%s,'whatsmeow_message_alt_v1',now(),now())''',
            (account,source,lid,pn,lid))


# spec: ANYM-537
def test_the_owner_run_lists_each_case_by_its_first_message(waits):
    s,ar,key,now,add,listed,rows=waits
    # A run of a question and a thanks: listed from the question.
    add('run','1',60,'incoming','Can we talk this week?')
    add('run','2',46,body='Are you coming on Friday?');add('run','3',46,body='thanks',seconds=60)
    # A nudge 2 h ago after a question 3 days old: waits 3 days.
    add('nudge','1',72,body='Did you send the contract?');add('nudge','2',2,body='ping')
    # A reply 1 s after the send.
    add('replied','1',72,body='Did you see the draft?');add('replied','2',72,'incoming','Yes',seconds=1)
    # A send 23 h old under the default 24 h.
    add('young','1',23,body='Lunch tomorrow?')
    # A reaction after the send is a service event: no answer.
    add('7001@c.us','true_7001@c.us_A',47,body='Is the room booked?',account='wait-wa')
    add('7001@c.us','R1',24,'incoming','👍',account='wait-wa',is_service=True,message_kind='system')
    # A phone/LID pair answered on the LID side.
    add('7100@c.us','true_7100@c.us_A',72,body='Are we still on?',account='wait-wa')
    pair(s,'wait-wa','9100@lid','7100@c.us',add('9100@lid','L1',48,'incoming','Yes',account='wait-wa'))
    # A pair asked on the phone side and nudged on the LID side: one row, waiting from the phone-side question.
    pair(s,'wait-wa','9200@lid','7200@c.us',add('9200@lid','L1',100,'incoming','Hello',account='wait-wa'))
    add('7200@c.us','true_7200@c.us_A',74,body='Did the parcel arrive?',account='wait-wa')
    add('9200@lid','true_9200@lid_B',3,body='?',account='wait-wa')
    page=listed()
    found={r['provider_chat_id']:r for r in page['items']}
    assert set(found)=={'run','nudge','7001@c.us','9200@lid'},sorted(found)
    assert [r['provider_chat_id'] for r in page['items']]==['9200@lid','nudge','7001@c.us','run']
    assert datetime.fromisoformat(found['run']['waiting']['since'])==at(now,46) and found['run']['waiting']['count']==2
    assert datetime.fromisoformat(found['nudge']['waiting']['since'])==at(now,72) and found['nudge']['waiting']['count']==2
    linked=found['9200@lid']
    assert datetime.fromisoformat(linked['waiting']['since'])==at(now,74) and linked['waiting']['count']==2
    assert linked['alias_chats'][0]['provider_chat_id']=='7200@c.us'
    assert page['order']=='waiting_longest' and page['awaiting_their_reply_total']==4 and page['uncertain_total']==0
    assert page['min_wait_hours']==24 and page['awaiting_reply_days']==14
    assert {a['account'] for a in page['accounts']}=={'wait-tg','wait-wa'}
    # The threshold and the window bound the first message, not the last one.
    assert set(rows(min_wait_hours=0))=={'run','nudge','7001@c.us','9200@lid','young'}
    assert set(rows(awaiting_reply_days=2))=={'run','7001@c.us'}
    assert set(rows(awaiting_reply_days=0,min_wait_hours=49))=={'nudge','9200@lid'}


# spec: ANYM-537
def test_courtesy_runs_are_never_listed_and_only_questions_counts_what_it_leaves_out(waits):
    s,ar,key,now,add,listed,rows=waits
    for n,word in enumerate(COURTESIES):add(f'thanks{n}','1',30,body=word)
    add('statement','1',30,body='The contract is signed')
    add('asked','1',30,body=QUESTION)
    add('mixed','1',30,body='Thanks');add('mixed','2',30,body='When can we meet?',seconds=5)
    assert set(rows())=={'statement','asked','mixed'}
    questions=listed(only_questions=True)
    assert {r['provider_chat_id'] for r in questions['items']}=={'asked','mixed'}
    assert questions['excluded_without_question_total']==1 and questions['awaiting_their_reply_total']==2
    assert 'excluded_without_question_total' not in listed()


# spec: ANYM-537
def test_only_questions_omitted_equals_false_on_mcp_and_rest(waits):
    s,ar,key,now,add,listed,rows=waits
    add('statement','1',30,body='The contract is signed');add('asked','1',30,body=QUESTION)
    headers={'Authorization':'Bearer '+key}
    with TestClient(create_app(s,['testserver'])) as client:
        def ids(**extra):
            mcp=structured(rpc(client,key,'list_chats',{**QUEUE,**extra}))
            rest=client.post('/v1/list_chats',headers=headers,json={**QUEUE,**extra}).json()
            assert [r['id'] for r in mcp['items']]==[r['id'] for r in rest['items']],(mcp,rest)
            return {r['title'] for r in rest['items']}
        omitted,false,true=ids(),ids(only_questions=False),ids(only_questions=True)
    assert omitted==false=={'Synthetic statement','Synthetic asked'} and true=={'Synthetic asked'}


# spec: ANYM-537
def test_groups_channels_self_chats_own_accounts_and_bots_are_left_out(waits):
    s,ar,key,now,add,listed,rows=waits
    ar.register('wait-tg2','telegram','Synthetic second account',discover=True)
    key=s.issue_key('wait-agent-2',['wait-tg','wait-tg2'])
    add('listed','1',30,body=QUESTION)
    add('group','1',30,body=QUESTION,chat_type='group')
    add('channel','1',30,body=QUESTION,chat_type='channel')
    add('self','1',30,body=QUESTION,is_self_chat=True)
    add('777000','1',30,body=QUESTION)
    # The owner's second account writes from OWNER2; its chat with the first account is not a peer.
    add('peer2','1',400,body='Synthetic owner message',account='wait-tg2',sender='OWNER2')
    add('OWNER2','1',100,'incoming','Note to self',sender='OWNER2');add('OWNER2','2',30,body=QUESTION)
    add('bot','1',100,'incoming','Digest posted',sender_is_bot=True);add('bot','2',30,body=QUESTION)
    found=s.list_chats(key,include_coverage=False,**QUEUE)
    assert [r['provider_chat_id'] for r in found['items']]==['listed'] and found['uncertain_total']==0


# spec: ANYM-537
def test_uncertain_heads_are_counted_and_an_unverified_head_is_listed_with_its_code(waits):
    s,ar,key,now,add,listed,rows=waits
    # Telegram orders a same-second reply by its higher provider ID: answered.
    add('tg-tie','10',30,body=QUESTION);add('tg-tie','11',30,'incoming','Sure')
    # Opposing WhatsApp messages in one second, and two outgoing ones with different receipts.
    add('7401@c.us','true_7401@c.us_A',30,body=QUESTION,account='wait-wa')
    add('7401@c.us','B',30,'incoming','Sure',account='wait-wa')
    add('7402@c.us','true_7402@c.us_A',30,body=QUESTION,account='wait-wa')
    add('7402@c.us','true_7402@c.us_B',30,body='And the keys?',account='wait-wa')
    with s.pool.connection() as c:
        for mid,state in (('true_7402@c.us_A','read'),('true_7402@c.us_B','server')):
            record(c,{'kind':'message_receipt','account':'wait-wa','platform':'whatsapp','chat':'7402@c.us',
                'provider_id':mid,'max_id':None,'state':state,'basis':'whatsapp_message_ack','audience':'private_peer',
                'observed_at':now.isoformat(),'provider_at':None})
    # A known run whose head was last verified an hour ago.
    add('stale','1',30,body=QUESTION)
    add('fresh','1',30,body=QUESTION)
    with s.pool.connection() as c:
        for chat,scanned in (('stale',now-timedelta(hours=1)),('fresh',datetime.now(timezone.utc))):
            c.execute('''INSERT INTO collector_chats(account,provider_chat,peer_kind,initialized,last_head_scan_at)
                VALUES('wait-tg',%s,'user',true,%s)''',(chat,scanned))
    page=listed()
    found={r['provider_chat_id']:r for r in page['items']}
    assert set(found)=={'stale','fresh'} and page['uncertain_total']==2
    assert found['stale']['waiting']['confidence']=='provider_head_stale'
    assert 'confidence' not in found['fresh']['waiting']


# spec: ANYM-537
@pytest.mark.parametrize('state,receipt,action',[('error','error','check_delivery'),('pending','pending','check_delivery'),
    ('server','server','check_delivery'),('delivered','delivered',None),('read','read',None),('played','played',None),
    ('unknown',None,None)])
def test_the_run_receipt_decides_the_suggested_action(waits,state,receipt,action):
    s,ar,key,now,add,listed,rows=waits
    chat='7500@c.us';mid='true_7500@c.us_A'
    add(chat,mid,30,body=QUESTION,account='wait-wa')
    if state!='unknown':
        with s.pool.connection() as c:
            record(c,{'kind':'message_receipt','account':'wait-wa','platform':'whatsapp','chat':chat,'provider_id':mid,
                'max_id':None,'state':state,'basis':'whatsapp_message_ack','audience':'private_peer',
                'observed_at':now.isoformat(),'provider_at':None})
    waiting=rows()[chat]['waiting']
    assert waiting.get('receipt')==receipt and waiting.get('suggested_action')==action,waiting


def message_ids(value):
    if isinstance(value,dict):
        for item in value.values():yield from message_ids(item)
    elif isinstance(value,list):
        for item in value:yield from message_ids(item)
    elif isinstance(value,str) and value.startswith('msg_'):yield value


# spec: ANYM-537
@pytest.mark.parametrize('view',['archive','conversation'])
@pytest.mark.parametrize('detail',['compact','full'])
def test_rows_carry_waiting_once_on_mcp_and_rest(waits,view,detail):
    s,ar,key,now,add,listed,rows=waits
    plain=add('plain','1',30,body=QUESTION)
    # A WhatsApp reaction is an archive head (not deleted), never a conversation head.
    asked=add('7600@c.us','true_7600@c.us_A',30,body=QUESTION,account='wait-wa')
    reaction=add('7600@c.us','R1',2,'incoming','👍',account='wait-wa',is_service=True,message_kind='system')
    headers={'Authorization':'Bearer '+key}
    arguments={**QUEUE,'view':view,'detail':detail}
    with TestClient(create_app(s,['testserver'])) as client:
        mcp=structured(rpc(client,key,'list_chats',arguments))
        rest=client.post('/v1/list_chats',headers=headers,json=arguments).json()
    for page in (mcp,rest):
        found={r['account']:r for r in page['items']}
        assert set(found)=={'wait-tg','wait-wa'},page
        for row in page['items']:
            waiting=row['waiting']
            assert waiting['count']==1 and waiting['since'] and set(waiting)<={'since','count','receipt','confidence',
                'suggested_action','muted','last_message_id','last_at'}
            if view=='conversation' and detail=='compact':
                assert row['last_message']['id']!=waiting.get('last_message_id')
                ids=list(message_ids(row))
                assert len(ids)==len(set(ids)),row
        assert 'last_message_id' not in found['wait-tg']['waiting']
        tied=found['wait-wa']
        if view=='archive':
            # The archive head is the reaction: waiting names the run's last message and its time.
            assert tied['last_message_id']==reaction and tied['waiting']['last_message_id']==asked
            assert datetime.fromisoformat(tied['waiting']['last_at'])==at(now,30)
        else:
            assert 'last_message_id' not in tied['waiting']
            head=tied['last_message']['id'] if detail=='compact' else tied['last_message_id']
            assert head==asked
    assert plain


# spec: ANYM-537
def test_a_chat_scoped_key_sees_no_message_outside_its_scope(waits):
    s,ar,key,now,add,listed,rows=waits
    mine=add('mine','1',30,body=QUESTION)
    other=add('other','1',30,body=QUESTION)
    with s.pool.connection() as c:
        chat=c.execute("SELECT chat FROM messages WHERE id=%s",(mine,)).fetchone()['chat']
    scoped=s.issue_key('wait-scoped',['wait-tg'],chats=[chat])
    with TestClient(create_app(s,['testserver'])) as client:
        for arguments in ({**QUEUE},{**QUEUE,'view':'archive','detail':'full'}):
            page=structured(rpc(client,scoped,'list_chats',arguments))
            assert [r['id'] for r in page['items']]==[chat]
            assert other not in json.dumps(page) and 'other' not in json.dumps(page)


# spec: ANYM-537
def test_mutes_apply_with_include_muted_and_muted_total(waits):
    s,ar,key,now,add,listed,rows=waits
    add('muted-chat','1',30,body=QUESTION)
    add('muted-sender','1',100,'incoming','Hi',sender='Muted');add('muted-sender','2',30,body=QUESTION)
    add('open','1',30,body=QUESTION)
    assert 'muted_total' not in listed()
    muted=next(r['id'] for r in listed()['items'] if r['provider_chat_id']=='muted-chat')
    s.mute_reply_source(key,chat_id=muted)
    s.mute_reply_source(key,account='wait-tg',sender_id='Muted')
    page=listed()
    assert [r['provider_chat_id'] for r in page['items']]==['open'] and page['muted_total']==2
    shown={r['provider_chat_id']:r['waiting'].get('muted') for r in listed(include_muted=True)['items']}
    assert shown=={'muted-chat':'chat','muted-sender':'sender','open':None}


# spec: ANYM-537
def test_pages_keep_equal_times_and_a_reply_between_pages_and_refuse_another_threshold(waits):
    s,ar,key,now,add,listed,rows=waits
    for n in range(30):add(f'equal{n:02d}','1',30,body=QUESTION)
    first=listed(limit=25)
    assert len(first['items'])==25 and first['next_cursor'] and first['awaiting_their_reply_total']==30
    rest=[r['provider_chat_id'] for r in listed(limit=25,after=first['next_cursor'])['items']]
    seen=[r['provider_chat_id'] for r in first['items']]
    assert len(rest)==5 and sorted(seen+rest)==[f'equal{n:02d}' for n in range(30)]
    # A reply arrives between the pages: the second page loses that chat and repeats none.
    add(rest[0],'2',1,'incoming','Here it is')
    after=[r['provider_chat_id'] for r in listed(limit=25,after=first['next_cursor'])['items']]
    assert after==rest[1:]
    for changed in ({'min_wait_hours':30},{'awaiting_reply_days':7},{'only_questions':True}):
        with pytest.raises(APIError) as refused:listed(limit=25,after=first['next_cursor'],**changed)
        assert refused.value.code=='invalid_cursor',changed


# spec: ANYM-537
@pytest.mark.parametrize('arguments,field',[
    ({'awaiting_their_reply':True},'order'),
    ({'awaiting_their_reply':True,'order':'activity'},'order'),
    ({'awaiting_their_reply':True,'order':'priority','awaiting_reply':True},'order'),
    ({**QUEUE,'awaiting_reply':True},'awaiting_reply'),
    ({'order':'waiting_longest'},'awaiting_their_reply'),
    ({'order':'waiting_longest','awaiting_reply':True},'awaiting_their_reply'),
    ({'min_wait_hours':5},'min_wait_hours'),
    ({**QUEUE,'only_unread':True},'only_unread'),
    ({**QUEUE,'exclude_senders':['42']},'exclude_senders')])
def test_refused_combinations_name_the_required_order(waits,arguments,field):
    s,ar,key,now,add,listed,rows=waits
    with pytest.raises(APIError) as refused:s.list_chats(key,**arguments)
    assert refused.value.code=='invalid_filter' and refused.value.fields==[field]
    assert "order='waiting_longest'" in refused.value.hint
    with TestClient(create_app(s,['testserver'])) as client:
        body=rpc(client,key,'list_chats',arguments).json()
    assert body['result']['isError'] and "waiting_longest" in body['result']['content'][0]['text']


# spec: ANYM-537
@pytest.mark.parametrize('hours',[-1,721,'24',True])
def test_min_wait_hours_is_an_integer_from_0_to_720(waits,hours):
    s,ar,key,now,add,listed,rows=waits
    with pytest.raises(APIError) as refused:listed(min_wait_hours=hours)
    assert refused.value.code=='invalid_filter'


# spec: ANYM-537
def test_twenty_five_worst_case_compact_rows_stay_under_30000_chars(waits,record_property):
    s,ar,key,now,add,listed,rows=waits
    title='Синтетический чат ожидания ответа с очень длинным названием для проверки обрезки '
    text=('Синтетический вопрос собеседнику о встрече и документах, который заведомо длиннее ста шестидесяти символов, '
        'чтобы проверить обрезку текста в компактной строке списка ожидания?')
    for n in range(25):
        chat=f'76{n:02d}@c.us'
        add(chat,f'true_{chat}_A',40,body=text,account='wait-wa',title=title+str(n))
        add(chat,f'true_{chat}_B',30,body=text,account='wait-wa',title=title+str(n))
        with s.pool.connection() as c:
            record(c,{'kind':'message_receipt','account':'wait-wa','platform':'whatsapp','chat':chat,
                'provider_id':f'true_{chat}_B','max_id':None,'state':'server','basis':'whatsapp_message_ack',
                'audience':'private_peer','observed_at':now.isoformat(),'provider_at':None})
    with TestClient(create_app(s,['testserver'])) as client:
        page=structured(rpc(client,key,'list_chats',{**QUEUE,'limit':25}))
    assert len(page['items'])==25 and all(r['waiting']['suggested_action']=='check_delivery' for r in page['items'])
    size=len(json.dumps(page,ensure_ascii=False))
    record_property('compact_chars',size)
    assert size<30000,size


# spec: ANYM-537
def test_the_gateway_catalog_declares_the_queue_without_defaults():
    catalog=json.loads((ROOT/'services/gateway/src/agent_gateway/catalog.json').read_text())
    tool=next(t for t in catalog if t['name']=='messages_list_chats')
    properties=tool['inputSchema']['properties']
    assert 'default' not in properties['awaiting_their_reply'] and 'default' not in properties['min_wait_hours']
    assert 'waiting_longest' in properties['order']['enum']
    assert 'awaiting_their_reply' in properties['only_questions']['description']
    contracts=json.loads((ROOT/'tests/platform/fixtures/messaging-contracts.json').read_text())
    snapshot=next(t for t in contracts['tools'] if t['name']=='list_chats')
    assert snapshot['description']==tool['description']
    assert {'awaiting_their_reply','min_wait_hours'}<=set(snapshot['inputSchema']['properties'])


# spec: ANYM-537
def test_a_pair_head_tie_or_unclassified_head_in_either_chat_is_uncertain(waits):
    s,ar,key,now,add,listed,rows=waits
    for n in (30,31,32):
        pair(s,'wait-wa',f'9{n}0@lid',f'7{n}0@c.us',add(f'9{n}0@lid','L1',100,'incoming','Hello',account='wait-wa'))
    # Two outgoing messages in one second, one in each chat of the pair.
    add('7300@c.us','true_7300@c.us_A',30,body=QUESTION,account='wait-wa')
    add('9300@lid','true_9300@lid_B',30,body='And the keys?',account='wait-wa')
    # The owner's message on the phone side and the peer's on the LID side in one second.
    add('7310@c.us','true_7310@c.us_A',30,body=QUESTION,account='wait-wa')
    add('9310@lid','L2',30,'incoming','Sure',account='wait-wa')
    # A newer message of unknown kind and no text on the LID side.
    add('7320@c.us','true_7320@c.us_A',30,body=QUESTION,account='wait-wa')
    add('9320@lid','L2',20,'incoming','',account='wait-wa',message_kind='unknown')
    page=listed()
    assert page['items']==[] and page['uncertain_total']==3,page


# spec: ANYM-537
@pytest.mark.parametrize('change,uncertain',[('message_revision_changed_during_read',1),('peer_wrote',0)])
def test_a_head_that_changes_during_the_read_leaves_the_row_out(waits,monkeypatch,change,uncertain):
    import messaging_service.chat_queries as chat_queries
    s,ar,key,now,add,listed,rows=waits
    add('changed','1',30,body=QUESTION);add('kept','1',29,body=QUESTION)
    real=chat_queries.conversation_state
    def raced(c,p,account,chat,**kwargs):
        state=real(c,p,account,chat,**kwargs)
        if c.execute('SELECT provider_chat FROM chats WHERE id=%s',(chat,)).fetchone()['provider_chat']=='changed':
            if change=='peer_wrote':state.update(last_turn='peer',suggested_action='review_incoming')
            else:state.update(last_turn='unknown',suggested_action='unclear',basis={**state['basis'],'code':change})
        return state
    monkeypatch.setattr(chat_queries,'conversation_state',raced)
    for view in ('archive','conversation'):
        page=listed(view=view)
        assert [r['provider_chat_id'] for r in page['items']]==['kept'],view
        assert page['awaiting_their_reply_total']==1 and page['uncertain_total']==uncertain


class Arriving:
    """Commits `arrive()` from another connection right after the first statement that reads the run lookups."""
    def __init__(self,c,arrive):self.connection=c;self.arrive=arrive
    def execute(self,sql,args=None):
        result=self.connection.execute(sql,args)
        if self.arrive and 'wh ON true' in sql:
            self.arrive();self.arrive=None
        return result
    def __getattr__(self,name):return getattr(self.connection,name)


# spec: ANYM-537
def test_totals_and_page_come_from_one_snapshot_so_a_late_change_never_goes_below_zero(waits,monkeypatch):
    import messaging_service.chat_queries as chat_queries
    s,ar,key,now,add,listed,rows=waits
    # A question backfilled while the list is read, then answered before its head state is read.
    real=chat_queries.conversation_state
    def answered(c,p,account,chat,**kwargs):
        state=real(c,p,account,chat,**kwargs)
        state.update(last_turn='peer',suggested_action='review_incoming')
        return state
    monkeypatch.setattr(chat_queries,'conversation_state',answered)
    with s.authorized(key,'list_chats') as (c,p):
        page=chat_queries.list_chats(s,Arriving(c,lambda:add('late','1',30,body=QUESTION)),p,include_coverage=False,**QUEUE)
    assert page['awaiting_their_reply_total']>=0 and page['awaiting_their_reply_total']==len(page['items'])
