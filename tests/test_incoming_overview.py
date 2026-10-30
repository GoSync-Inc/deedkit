"""Compact incoming overview: get_updates(view='by_chat') (ANYM-458)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.store import APIError
from test_archive import event
from test_read_state_freshness import ago, live as live  # noqa: F401 (fixture)

ROOT=Path(__file__).resolve().parents[1]
SINCE='2026-10-20T00:00:00Z'


def add(s,account,chat,mid,hour,origin='live',direction='incoming',body='Synthetic incoming note'):
    with s.pool.connection() as c:
        c.execute('INSERT INTO chats VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(chat,account,'Synthetic '+chat))
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',(mid,account,chat,mid,'synthetic-sender',body,
            datetime(2026,10,22,tzinfo=timezone.utc)+timedelta(hours=hour),direction))
        return s.append_event(c,account,mid,origin=origin)


def state(s):
    with s.pool.connection() as c:
        return (c.execute('SELECT principal,name,position,version,filter_spec FROM consumers ORDER BY 1,2').fetchall(),
                c.execute('SELECT count(*) AS n FROM deliveries').fetchone()['n'])


# spec: ANYM-458
def test_overview_splits_live_and_backfill(seeded):
    s,a,b=seeded
    add(s,'telegram-demo','tg-live','m1',1)
    add(s,'telegram-demo','tg-live','m2',3,body='Newest live question?')
    add(s,'telegram-demo','tg-live','m3',2,origin='backfill')
    add(s,'telegram-demo','tg-live','m4',4,direction='outgoing')
    add(s,'whatsapp-demo-1','wa-photo','m7',0,body='')
    with s.pool.connection() as c:
        c.execute("INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES('f7','m7','7777777777777777777777777777777777777777777777777777777777777777','photo.jpg','image/jpeg',1)")
    add(s,'whatsapp-demo-1','wa-history','m5',5,origin='backfill')
    add(s,'whatsapp-demo-1','wa-history','m6',6,origin='reconcile')
    page=s.get_updates(a,'overview',view='by_chat',from_time=SINCE)
    rows={row['chat']:row for row in page['chats']}
    assert [row['chat'] for row in page['chats']]==['tg-live','wa-photo','wa-history']
    assert (rows['tg-live']['live'],rows['tg-live']['backfill'])==(2,1)
    assert rows['tg-live']['newest']['message']=='m2'
    assert rows['tg-live']['newest']['text']=='Newest live question?'
    assert rows['tg-live']['owner_replied_after_newest'] is True and 'owner_replied_after_newest' not in rows['wa-photo']
    assert rows['wa-photo']['newest']['text']=='[photo.jpg]'
    assert (rows['wa-history']['live'],rows['wa-history']['backfill'],rows['wa-history']['newest'])==(0,2,None)
    assert (page['live_total'],page['backfill_total'],page['chats_total'])==(3,3,3)
    assert [row['account'] for row in page['accounts']]==['telegram-demo','whatsapp-demo-1']
    assert page['view']=='by_chat' and page['has_more'] is False


# spec: ANYM-458
def test_overview_never_acks_or_moves_cursors(seeded):
    s,a,b=seeded
    add(s,'telegram-demo','tg-live','m1',1)
    first=s.get_updates(a,'main',from_time=SINCE)
    assert s.ack_updates(a,'main',first['receipt'],first['ack_version'])['acked']
    add(s,'telegram-demo','tg-live','m2',2)
    pending=s.get_updates(a,'main',from_time=SINCE)
    before=state(s)
    page=s.get_updates(a,'main',view='by_chat')
    assert page['window']=='acknowledged_position'
    assert [(row['chat'],row['live']) for row in page['chats']]==[('tg-live',1)]
    assert 'receipt' not in page and 'ack_version' not in page
    fresh=s.get_updates(a,'never-created',view='by_chat',from_time=SINCE)
    assert fresh['window']=='from_time' and fresh['live_total']==2
    # A supplied from_time also bounds a consumer's window; neither bound is dropped.
    both=s.get_updates(a,'main',view='by_chat',from_time='2026-10-22T01:30:00Z')
    assert both['window']=='acknowledged_position_and_from_time' and both['live_total']==1
    later=s.get_updates(a,'main',view='by_chat',from_time='2026-10-22T03:00:00Z')
    assert (later['chats'],later['live_total'])==([],0)
    assert state(s)==before
    assert s.ack_updates(a,'main',pending['receipt'],pending['ack_version'])['acked']


# spec: ANYM-458
def test_overview_reports_unknown_read_state(seeded):
    s,a,b=seeded
    add(s,'telegram-demo','tg-unknown','m1',1)
    add(s,'telegram-demo','tg-observed','m2',2)
    with s.pool.connection() as c:
        c.execute('''INSERT INTO chat_read_observations(chat,account,observed_at,unread_count,basis)
            VALUES('tg-observed','telegram-demo',now(),3,'telegram_dialog')''')
    page=s.get_updates(a,'o',view='by_chat',from_time=SINCE)
    rows={row['chat']:row for row in page['chats']}
    unknown=rows['tg-unknown']
    assert unknown['read_state']=='unknown'
    assert not {'provider_unread','provider_unread_count','muted','archived'}&set(unknown)
    observed=rows['tg-observed']
    assert (observed['read_state'],observed['provider_unread'],observed['provider_unread_count'])==('observed',True,3)
    assert not {'muted','archived'}&set(observed)
    assert (page['provider_mute_archive'],page['absent_read_fields'])==('unknown','unknown')


# spec: ANYM-458
def test_overview_hides_out_of_scope_chats(seeded):
    s,a,b=seeded
    add(s,'whatsapp-demo-1','wa-hidden','m1',1,body='OUT_OF_SCOPE_SENTINEL')
    add(s,'whatsapp-demo-2','wa-unscoped','m2',2,body='OUT_OF_SCOPE_SENTINEL')
    add(s,'telegram-demo','tg-visible','m3',3)
    page=s.get_updates(b,'o',view='by_chat',from_time=SINCE)
    assert [row['chat'] for row in page['chats']]==['tg-visible'] and page['live_total']==1
    # Ingestion in a hidden account leaves the answer unchanged: no global position is exposed.
    add(s,'whatsapp-demo-1','wa-hidden','m4',4,body='OUT_OF_SCOPE_SENTINEL')
    assert s.get_updates(b,'o',view='by_chat',from_time=SINCE)==page
    named=s.get_updates(b,'o',view='by_chat',from_time=SINCE,chats=['wa-hidden','wa-unscoped'])
    assert (named['chats'],named['live_total'],named['chats_total'])==([],0,0)
    assert 'OUT_OF_SCOPE_SENTINEL' not in json.dumps(page,ensure_ascii=False)+json.dumps(named,ensure_ascii=False)
    assert [row['chat'] for row in s.get_updates(a,'o',view='by_chat',from_time=SINCE)['chats']]==['wa-hidden','tg-visible']


# spec: ANYM-458
def test_overview_of_25_chats_within_10000_chars(seeded):
    s,a,b=seeded
    for n in range(30):
        add(s,'telegram-demo' if n%2 else 'whatsapp-demo-1',f'overview-chat-{n:02d}',f'm{n}',n%20,
            body='Длинное синтетическое входящее сообщение для проверки размера сводки. '*8)
        add(s,'telegram-demo' if n%2 else 'whatsapp-demo-1',f'overview-chat-{n:02d}',f'h{n}',1,origin='backfill')
    page=s.get_updates(a,'o',view='by_chat',from_time=SINCE,limit=25)
    assert len(page['chats'])==25 and page['has_more'] is True and page['chats_total']==30
    assert len(json.dumps(page,ensure_ascii=False))<=10000
    # The MCP default (compact) must not widen the overview's 80-character text.
    with TestClient(create_app(s,['testserver'])) as client:
        headers={'Authorization':'Bearer '+a,'Accept':'application/json, text/event-stream'}
        rpc=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
            'name':'get_updates','arguments':{'consumer':'o','view':'by_chat','from_time':SINCE,'limit':25}}}).json()
    mcp=rpc['result']['structuredContent']
    assert len(mcp['chats'])==25 and len(json.dumps(mcp,ensure_ascii=False))<=10000
    assert all(len(row['newest']['text'])<=81 for row in mcp['chats'])
    zero=s.get_updates(a,'o',view='by_chat',from_time=SINCE,limit=25,body_chars=0)
    assert not any('text' in row['newest'] for row in zero['chats'])


# spec: ANYM-458
@pytest.mark.parametrize('options',[{'direction':'outgoing'},{'origins':['live']},{'kinds':['message.created']},
    {'order':'sent_at'},{'start_from':'now'},{'reset_filters':True,'expected_ack_version':0},{}])
def test_overview_rejects_event_only_options_and_unbounded_scans(seeded,options):
    s,a,b=seeded
    before=state(s)
    with pytest.raises(APIError,match='invalid_filter') as failure:
        s.get_updates(a,'o',view='by_chat',**({'from_time':SINCE} if options else {}),**options)
    if not options:assert failure.value.fields==['from_time']
    with pytest.raises(APIError,match='invalid_filter'):
        s.get_updates(a,'o',view='timeline')
    assert state(s)==before


# spec: ANYM-458
def test_overview_is_offered_over_mcp_and_the_gateway_catalog(seeded):
    s,a,b=seeded
    add(s,'telegram-demo','tg-live','m1',1)
    with TestClient(create_app(s,['testserver'])) as client:
        headers={'Authorization':'Bearer '+a,'Accept':'application/json, text/event-stream'}
        rpc=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
            'name':'get_updates','arguments':{'consumer':'o','view':'by_chat','from_time':SINCE}}}).json()
    assert 'error' not in rpc and not rpc['result'].get('isError'),rpc
    assert [row['chat'] for row in rpc['result']['structuredContent']['chats']]==['tg-live']
    catalog=json.loads((ROOT/'services/gateway/src/agent_gateway/catalog.json').read_text())
    tool=next(t for t in catalog if t['name']=='messages_get_updates')
    assert tool['inputSchema']['properties']['view']['enum']==['events','by_chat']
    # The gateway fills schema defaults (catalog.py); a pre-458 messaging release rejects unknown fields.
    assert 'default' not in tool['inputSchema']['properties']['view']
    assert 'by_chat' in tool['description']


# spec: ANYM-458
def test_overview_consumer_at_journal_start_keeps_its_window(seeded):
    s,a,b=seeded
    add(s,'telegram-demo','tg-live','m1',1)
    with s.pool.connection() as c:
        c.execute("INSERT INTO consumers(principal,name,scope_version,filter_spec,position) SELECT id,'unacked',scope_version,'{}',0 FROM principals WHERE id='agent-a'")
    page=s.get_updates(a,'unacked',view='by_chat',from_time=SINCE)
    assert page['window']=='acknowledged_position_and_from_time' and page['live_total']==1
    # Position 0 is a valid bound: an existing consumer needs no from_time.
    whole=s.get_updates(a,'unacked',view='by_chat')
    assert whole['window']=='acknowledged_position' and 'tg-live' in [row['chat'] for row in whole['chats']]


# spec: ANYM-458
def test_overview_hides_chats_outside_a_chat_scoped_key(seeded):
    s,a,b=seeded
    add(s,'telegram-demo','tg-allowed','m1',1)
    add(s,'telegram-demo','tg-hidden','m2',2,body='HIDDEN_CHAT_SENTINEL')
    key=s.issue_key('chat-scoped',['telegram-demo'],chats=['tg-allowed'])
    page=s.get_updates(key,'o',view='by_chat',from_time=SINCE)
    assert [row['chat'] for row in page['chats']]==['tg-allowed'] and page['live_total']==1
    named=s.get_updates(key,'o',view='by_chat',from_time=SINCE,chats=['tg-hidden'])
    assert (named['chats'],named['live_total'],named['chats_total'])==([],0,0)
    text=json.dumps(page,ensure_ascii=False)+json.dumps(named,ensure_ascii=False)
    assert 'HIDDEN_CHAT_SENTINEL' not in text and 'tg-hidden' not in text.replace('"chats": ["tg-hidden"]','')


# spec: ANYM-458
def test_overview_stale_read_state_shows_no_unread(seeded):
    s,a,b=seeded
    add(s,'telegram-demo','tg-stale','m1',1)
    add(s,'telegram-demo','tg-newer','m2',2)
    add(s,'telegram-demo','tg-newer','m3',30)
    with s.pool.connection() as c:
        # Observed before the newest incoming message: stale, even though m3 is outside the window.
        c.execute("""INSERT INTO chat_read_observations(chat,account,observed_at,unread_count,basis) VALUES
            ('tg-stale','telegram-demo',now()-interval '2 days',4,'telegram_dialog'),
            ('tg-newer','telegram-demo','2026-10-22T05:00:00Z',1,'telegram_dialog')""")
    rows={row['chat']:row for row in s.get_updates(a,'o',view='by_chat',from_time=SINCE,
        to_time='2026-10-22T04:00:00Z')['chats']}
    for chat in ('tg-stale','tg-newer'):
        assert rows[chat]['read_state']=='stale' and 'provider_unread' not in rows[chat]


# spec: ANYM-458
def test_overview_of_25_populated_chats_over_mcp_within_10000_chars(seeded):
    s,a,b=seeded
    with s.pool.connection() as c:
        for n in range(25):
            chat=f'chat_{n:032x}';account='telegram-demo' if n%2 else 'whatsapp-demo-1'
            c.execute('INSERT INTO chats VALUES(%s,%s,%s)',(chat,account,'Синтетический чат с очень длинным названием для проверки '+'я'*20))
            c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
                VALUES(%s,%s,%s,%s,'synthetic-sender-with-a-long-identifier-000000000000',%s,%s)""",
                (f'msg_{n:032x}',account,chat,f'p{n}','Ш'*400,datetime(2026,10,22,1,n,tzinfo=timezone.utc)))
            s.append_event(c,account,f'msg_{n:032x}',origin='live')
            c.execute("""INSERT INTO sender_profiles(id,account,chat,sender,display_name,source_message,source_sent_at)
                VALUES(%s,%s,%s,'synthetic-sender-with-a-long-identifier-000000000000',%s,%s,%s)""",
                (f'sp{n}',account,chat,'Синтетическое Имя Отправителя '+'ж'*40,f'msg_{n:032x}',datetime(2026,10,22,1,n,tzinfo=timezone.utc)))
            c.execute("INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction) VALUES(%s,%s,%s,%s,'owner','ok',%s,'outgoing')",
                (f'o{n}',account,chat,f'o{n}',datetime(2026,10,22,2,n,tzinfo=timezone.utc)))
            c.execute("INSERT INTO chat_read_observations(chat,account,observed_at,unread_count,basis) VALUES(%s,%s,now(),7,'telegram_dialog')",
                (chat,account))
    with TestClient(create_app(s,['testserver'])) as client:
        headers={'Authorization':'Bearer '+a,'Accept':'application/json, text/event-stream'}
        rpc=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
            'name':'get_updates','arguments':{'consumer':'o','view':'by_chat','from_time':SINCE,'limit':25}}}).json()
    page=rpc['result']['structuredContent']
    assert len(page['chats'])>=20 and page['chats_total']==25 and all(row['owner_replied_after_newest'] for row in page['chats'])
    assert all(row['read_state']=='stale' or row['provider_unread'] for row in page['chats'])
    assert len(json.dumps(page,ensure_ascii=False))<=10000 and page['shortened_for_size']


# spec: ANYM-458
def test_overview_follows_continuous_receiving_freshness(live):
    s,ar,state,row=live
    key=s.issue_key('fresh-overview',['fresh-test'])
    first=s.get_updates(key,'o',view='by_chat',from_time=ago(hours=5).isoformat())['chats'][0]
    assert first['read_state']=='observed' and first['provider_unread'] is True
    at=ago(hours=1).isoformat()
    ar.ingest(event(account='fresh-test',chat='100',provider_id='2',sent_at=at,updated_at=at,metadata={'chat_type':'private'}))
    # A newer incoming message outside the window still makes the observation stale.
    later=s.get_updates(key,'o',view='by_chat',from_time=ago(hours=5).isoformat(),to_time=ago(hours=2).isoformat())['chats'][0]
    assert later['read_state']=='stale' and 'provider_unread' not in later and later['live']==1


# spec: ANYM-458
def test_overview_stays_within_budget_with_large_filters(seeded):
    s,a,b=seeded
    add(s,'telegram-demo','tg-live','m1',1)
    senders=['synthetic-sender']+[f'{n:03d}'+'x'*253 for n in range(40)]
    page=s.get_updates(a,'o',view='by_chat',from_time=SINCE,senders=senders)
    # Filter names only are echoed: the caller's values never inflate the page.
    assert page['filters']==['from_time','senders'] and page['live_total']==1
    assert len(json.dumps(page,ensure_ascii=False))<=10000
