"""One whole-flow Telegram collector test: catalog, backfill, live changes and a persisted FloodWait pause."""
import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace as NS

from telethon.errors import FloodWaitError

from messaging_service.archive import Archive
from messaging_service.collector_state import CaptureSpool,CollectorState
from messaging_service.envelope import stable_id
from messaging_service.sync_status import get_sync_status
from messaging_service.telegram_collector import TelegramCollector
from messaging_service.telegram_governor import ProviderRateLimited,RequestGovernor
from test_collectors import FakeProvider,message,worker as worker  # noqa: F401  (worker is a fixture)


def dated(mid,chat,body=None,edited=None):
    item=message(mid,chat=chat,body=body or f'chat {chat} message {mid}',edited=edited)
    item.date=datetime(2026,10,mid,12,0,tzinfo=timezone.utc)
    return item


class FlowProvider(FakeProvider):
    """Several dialogs and a scripted FloodWait; every call goes through the governor like TelethonProvider.rpc."""
    def __init__(self,store,account,messages,events,flood=()):
        super().__init__(messages)
        self.store,self.account,self.events,self.flood=store,account,events,list(flood)
        self.governor=None; self.catalog_sizes=[]
    async def dialogs(self,limit):
        async def read():
            self.events.append(('dialogs',))
            return [('100','user','First'),('200','user','Second'),('300','user','Excluded')]
        return await self.governor.call(read,'telegram_discovery')
    async def page(self,chat,*,limit,min_id=0,before=0,reverse=False):
        async def read():
            self.events.append(('page',chat,min_id,before))
            self.reads.append((chat,min_id,before,reverse))
            with self.store.pool.connection() as c:
                self.catalog_sizes.append(c.execute('SELECT count(*) n FROM collector_chats WHERE account=%s',(self.account,)).fetchone()['n'])
            if self.flood and self.flood[0]==(chat,before): self.flood.pop(0); raise FloodWaitError(None,capture=120)
            rows=[m for m in self.messages if m.chat_id==int(chat) and m.id>min_id and (not before or m.id<before)]
            return sorted(rows,key=lambda m:m.id,reverse=not reverse)[:limit]
        return await self.governor.call(read,'telegram_history')


# spec: ANYM-416
def test_telegram_collector_flow_from_catalog_to_persisted_flood_pause_across_restart(worker):
    w,s,key=worker
    account=w.account; events=[]; sleeps=[]
    w.history_since=datetime(2026,10,3,tzinfo=timezone.utc)
    with s.pool.connection() as c: c.execute('UPDATE account_policies SET exclude_chats=%s WHERE account=%s',(['300'],account))
    # Chat 100 has ids 1..8 (1 and 2 predate history_since), chat 200 has 5..7, chat 300 is excluded.
    chat_100=[dated(i,'100') for i in range(1,9)]
    chat_200=[dated(i,'200') for i in range(5,8)]
    chat_300=[dated(i,'300') for i in range(5,8)]
    w.provider=FlowProvider(s,account,chat_100+chat_200+chat_300,events,flood=[('100',5)])
    w.provider.governor=RequestGovernor(w.state,interval=0)
    w.provider.subscribe(w.capture,w.deleted)
    cursors=[]
    def snapshot(collector=w):
        row=collector.state.chat('100','user')
        cursors.append((row['high_water'],row['history_before'],row['window_complete']))
        return row
    def reader_messages(): return {m['body'] for m in s.search(key)['items']}

    async def scenario():
        # Backfill: first pass reads the newest page of each allowed chat after the whole catalog is saved.
        await w.sync_once()
        first=snapshot()
        assert (first['high_water'],first['history_before'],first['window_complete'])==(8,7,False)
        assert w.provider.catalog_sizes and set(w.provider.catalog_sizes)=={2}
        assert not any(read[0]=='300' for read in w.provider.reads)
        assert {r['title'] for r in s.list_chats(key)['items']}=={'First','Second'}
        # Live: a new, an edited and a deleted message arrive through the provider's callbacks.
        await w.provider.capture(NS(message=dated(9,'100')))
        edited=dated(8,'100',body='chat 100 edited message 8',edited=datetime(2026,10,9,13,0,tzinfo=timezone.utc))
        await w.provider.capture(NS(message=edited))
        await w.provider.deleted(NS(chat_id=100,deleted_ids=[7]))
        assert len(list(w.spool.path.glob('*.json')))==3
        assert w.spool.drain(w.state.apply)==3 and not list(w.spool.path.glob('*.json'))
        w.provider.messages=[m for m in w.provider.messages if not (m.chat_id==100 and m.id in (7,8))]+[dated(9,'100'),edited]
        # Reconcile: the head moves up, the older cursor moves down.
        await w.sync_once()
        snapshot()
        # FloodWait on the next older history page: the pause is committed before the error propagates.
        try: await w.sync_once()
        except ProviderRateLimited as exc: assert exc.seconds==120
        else: raise AssertionError('FloodWait was not raised')
        return snapshot()
    saved=asyncio.run(scenario())
    assert saved['history_before']==5 and not saved['window_complete']
    assert [c[0] for c in cursors]==sorted(c[0] for c in cursors) and [c[1] for c in cursors]==sorted((c[1] for c in cursors),reverse=True)
    assert cursors[1][:2]==(9,5)
    with s.pool.connection() as c:
        health=c.execute("SELECT status,detail_code,cooldown_until>clock_timestamp()+interval '100 seconds' AS paused FROM collector_health WHERE account=%s",(account,)).fetchone()
        status=get_sync_status(c,{'accounts':[account],'chat_ids':None})['sources'][0]
    assert health=={'status':'degraded','detail_code':'rate_limited','paused':True}
    assert status['status']=='degraded' and status['detail_code']=='rate_limited'
    # Reader: new message, revision of the edit and tombstone of the delete.
    assert {'chat 100 message 9','chat 100 edited message 8'}<=reader_messages() and 'chat 100 message 7' not in reader_messages()
    def history(mid): return [(x['kind'],x['revision'],x['origin']) for x in s.get_updates(key,'flow',100)['items'] if x['message']==stable_id('msg',account,'100',mid)]
    assert history('9')==[('message.created',1,'live')]
    assert history('8')==[('message.created',1,'backfill'),('message.edited',2,'live')]
    assert history('7')[0][0]=='message.created' and history('7')[-1][0]=='message.deleted'
    assert s.get_context(key,stable_id('msg',account,'100','7'))['message']['deleted']

    # Restart: a new collector, state and governor on the same database; the governor sleeps through the saved pause.
    w.archive.release(account,w.owner,w.state.lease[1])
    async def recorded_sleep(seconds):
        sleeps.append(seconds); events.append(('sleep',seconds))
        with s.pool.connection() as c: c.execute('UPDATE collector_health SET cooldown_until=clock_timestamp() WHERE account=%s',(account,))
    provider=FlowProvider(s,account,w.provider.messages,events)
    restarted=TelegramCollector(Archive(s),'tg-collector',provider,CaptureSpool(w.spool.path,reserve_bytes=0),batch_size=2,
        writer=w.writer,owner='restarted-owner',history_since=w.history_since)
    epoch=restarted.archive.acquire(account,restarted.owner)
    restarted.state=CollectorState(restarted.archive,account,(restarted.owner,epoch)); restarted.policy=restarted.state.policy()
    assert restarted.state.cooldown_remaining()>100
    provider.governor=RequestGovernor(restarted.state,interval=0,sleep=recorded_sleep)
    events.clear()

    async def resume():
        await restarted.sync_once()
        first=restarted.state.chat('100','user')
        # The saved cursor is used: the older page is read from before=5, not from the head of the chat.
        assert ('100',9,0,True) in provider.reads and ('100',0,5,False) in provider.reads
        assert (first['history_before'],first['window_complete'])==(3,False)
        await restarted.sync_once()
        return restarted.state.chat('100','user')
    final=asyncio.run(resume())
    assert sleeps==[30] and events[:2]==[('sleep',30),('dialogs',)]
    assert final['high_water']==9 and final['history_before']==3 and final['window_complete'] and not final['history_exhausted']
    assert not any(read[0]=='300' for read in provider.reads)
    # Newest-first: every older page starts below the previous one (7, 5, the flooded 5 again, then 3).
    older=[read[2] for read in w.provider.reads+provider.reads if read[0]=='100' and read[2]]
    assert older==[7,5,5,3] and not any(read[3] for read in w.provider.reads+provider.reads if read[2])
    assert {m for m in reader_messages() if m.startswith('chat 100')}=={f'chat 100 message {n}' for n in (3,4,5,6,9)}|{'chat 100 edited message 8'}
