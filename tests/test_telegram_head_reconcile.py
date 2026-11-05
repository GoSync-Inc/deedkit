"""A busy Telegram chat's head reconcile keeps up within a per-pass page cap; synthetic providers only."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from hypothesis import given,settings,strategies as st
from telethon.errors import FloodWaitError

from messaging_service.envelope import stable_id
from messaging_service.sync_status import get_sync_status
from messaging_service.telegram_collector import TelegramCollector
from test_collectors import message,worker as worker  # noqa: F401  (worker is a fixture)


def head_reads(provider):
    return [read for read in provider.reads if read[3]]


class FakeState:
    """The cursor sync_chat advances, without a database."""
    def __init__(self):self.high_water=0;self.head_scans=0
    def chat(self,chat,kind):
        return {'initialized':True,'high_water':self.high_water,'history_exhausted':True,
                'window_complete':True,'history_before':None}
    def checkpoint(self,chat,*,high_water=None,head_complete=False,**_):
        self.high_water=max(self.high_water,high_water or 0);self.head_scans+=bool(head_complete)


class FakePages:
    def __init__(self,total):self.total=total;self.head_pages=0
    async def page(self,chat,*,limit,min_id=0,before=0,reverse=False):
        self.head_pages+=bool(reverse)
        ids=range(min_id+1,self.total+1) if reverse else range(self.total,0,-1)
        return [NS(id=i) for i in list(ids)[:limit]]


# spec: ANYM-103
@settings(max_examples=200,deadline=None)
@given(total=st.integers(0,60),batch=st.integers(1,7),cap=st.integers(1,5),extra=st.integers(0,20))
def test_every_new_id_is_walked_within_cap_and_head_scan_means_a_short_page(total,batch,cap,extra):
    provider=FakePages(total)
    collector=TelegramCollector(None,'synthetic-tg',provider,NS(),writer=object(),batch_size=batch,head_pages=cap)
    collector.state=FakeState()
    async def commit(*_):return []
    collector.commit_page=commit
    previous=0;passes=0
    while True:
        scans=collector.state.head_scans;pages=provider.head_pages
        asyncio.run(collector.sync_chat('100','user','Synthetic'))
        passes+=1
        assert provider.head_pages-pages<=cap
        assert collector.state.high_water==min(total,previous+(provider.head_pages-pages)*batch)
        previous=collector.state.high_water
        capped='100' in collector._head_capped
        # A pass verifies the head exactly when it did not end at the cap on a full page.
        assert (collector.state.head_scans>scans)==(not capped)
        collector._head_capped.clear()
        if not capped:break
        assert passes<=total+1
    assert collector.state.high_water==total
    # New messages after the catch-up start from the same cursor.
    provider.total=total+extra
    asyncio.run(collector.sync_chat('100','user','Synthetic'))
    assert collector.state.high_water==min(total+extra,total+cap*batch)


# spec: ANYM-103
def test_cap_advances_by_pages_without_head_scan_and_next_pass_finishes(worker):
    w,s,_=worker
    w.batch_size=100;w.head_pages=2
    w.provider.messages=[message(1)]
    asyncio.run(w.sync_once())  # the first read initializes the chat
    with s.pool.connection() as c:
        first=c.execute("SELECT last_head_scan_at FROM collector_chats WHERE account=%s AND provider_chat='100'",(w.account,)).fetchone()['last_head_scan_at']
    w.provider.messages=[message(i) for i in range(1,252)]
    w.provider.reads.clear()
    asyncio.run(w.sync_once())
    with s.pool.connection() as c:
        row=c.execute("SELECT high_water,last_head_scan_at FROM collector_chats WHERE account=%s AND provider_chat='100'",(w.account,)).fetchone()
        stored=c.execute('SELECT count(*) n FROM messages WHERE account=%s',(w.account,)).fetchone()['n']
    assert row['high_water']==201 and row['last_head_scan_at']==first
    assert [read[1] for read in head_reads(w.provider)]==[1,101]
    assert w._head_capped=={'100'} and stored>=201
    asyncio.run(w.sync_once())
    with s.pool.connection() as c:
        row=c.execute("SELECT high_water,last_head_scan_at FROM collector_chats WHERE account=%s AND provider_chat='100'",(w.account,)).fetchone()
    assert row['high_water']==251 and row['last_head_scan_at']>first
    assert w._head_capped==set()


# spec: ANYM-103
def test_flood_wait_on_a_later_head_page_keeps_committed_high_water_and_propagates(worker):
    w,s,_=worker
    w.batch_size=100;w.head_pages=10
    w.provider.messages=[message(1)]
    asyncio.run(w.sync_once())
    w.provider.messages=[message(i) for i in range(1,352)]
    page=w.provider.page;calls=[]
    async def flooding(chat,**query):
        if query.get('reverse'):
            calls.append(query['min_id'])
            if len(calls)==2:raise FloodWaitError(None,capture=120)
        return await page(chat,**query)
    w.provider.page=flooding
    with pytest.raises(FloodWaitError):asyncio.run(w.sync_once())
    with s.pool.connection() as c:
        row=c.execute("SELECT high_water,last_error_code FROM collector_chats WHERE account=%s AND provider_chat='100'",(w.account,)).fetchone()
    assert row['high_water']==101 and row['last_error_code']=='rate_limited'
    w.provider.page=page
    asyncio.run(w.sync_once())
    assert w.state.chat('100','user')['high_water']==351


# spec: ANYM-103
def test_reconcile_loop_reports_capped_chats_and_logs_the_provider_wait(worker,capsys):
    w,s,_=worker
    w.batch_size=2;w.head_pages=1;w.interval=0
    w.provider.messages=[message(1)]
    asyncio.run(w.sync_once())
    w.provider.messages=[message(i) for i in range(1,7)]
    page=w.provider.page;floods=[FloodWaitError(None,capture=7)]
    async def flooding(chat,**query):
        if floods:raise floods.pop()
        return await page(chat,**query)
    w.provider.page=flooding
    async def sleep(_):raise asyncio.CancelledError
    async def scenario():
        import messaging_service.telegram_collector as module
        real=module.asyncio.sleep;module.asyncio.sleep=sleep
        try:await w.reconcile_loop()
        finally:module.asyncio.sleep=real
    with pytest.raises(asyncio.CancelledError):asyncio.run(scenario())
    logged=[json.loads(line) for line in capsys.readouterr().out.splitlines() if 'telegram_rate_limited' in line]
    assert logged==[{'event':'telegram_rate_limited','seconds':7}]


# spec: ANYM-103
def test_sync_status_reports_capped_count_and_chats_heard_only_live(worker):
    w,s,key=worker
    w.state.head_capped(3)
    w.state.head_capped(1)
    live=[str(1000+i) for i in range(22)]
    with s.pool.connection() as c:
        c.execute('INSERT INTO collector_chats(account,provider_chat,peer_kind) VALUES(%s,%s,%s)',(w.account,'9000','channel'))
    for i,chat in enumerate(live):
        asyncio.run(w.capture(NS(message=message(50+i,chat=chat))))
    asyncio.run(w.drain_captures([w.spool]))
    with s.pool.connection() as c:
        result=get_sync_status(c,{'accounts':[w.account],'chat_ids':None})
    source=next(row for row in result['sources'] if row['account']==w.account)
    history=source['history']
    assert history['live_only_chats']==22 and len(history['live_only_chat_ids'])==20
    expected=sorted(stable_id('chat',w.account,chat) for chat in live)[:20]
    assert history['live_only_chat_ids']==expected
    assert stable_id('chat',w.account,'9000') not in history['live_only_chat_ids']
    assert source['operations']['history']['head_capped_chats']==1


# spec: ANYM-103
def test_head_pages_per_pass_setting_has_a_default_and_bounds(tmp_path,monkeypatch):
    from messaging_service import telegram_collector as module
    from messaging_service.collector_common import validate_ranges
    assert module.HEAD_PAGES_PER_PASS==10
    assert TelegramCollector(None,'synthetic-tg',NS(),NS(),writer=object()).head_pages==10
    cfg={}
    validate_ranges(cfg,[module.HEAD_PAGES_RANGE]);assert cfg['head_pages_per_pass']==10
    for bad in (0,101,'10',True):
        with pytest.raises(ValueError,match='invalid_head_pages_per_pass'):
            validate_ranges({'head_pages_per_pass':bad},[module.HEAD_PAGES_RANGE])
