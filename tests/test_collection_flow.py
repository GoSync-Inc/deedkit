"""One collection flow over Telegram and WhatsApp, read through MCP (ANYM-413).

In-process fake providers and the disposable test database only: backfill, live changes, a collector
restart with captures still in the spool, and the status tools, all for one reader key.
"""
import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace as NS

import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.blobs import BlobWriter
from messaging_service.collector_state import CaptureSpool,CollectorState
from messaging_service.envelope import stable_id
from messaging_service.telegram_collector import history_bound
from messaging_service.whatsapp_collector import WhatsAppState
from test_collectors import worker,message,FakeProvider
from test_telegram_window import at
from test_whatsapp import wa,event,capture,Provider


def rpc(client,key,name,**arguments):
    response=client.post('/mcp',headers={'Authorization':'Bearer '+key,
        'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':7,'method':'tools/call',
        'params':{'name':name,'arguments':arguments}})
    assert response.status_code==200 and 'error' not in response.json(),response.text
    return response.json()['result']['structuredContent']


def rows(client,key):
    """Search hits as (account, first line of text, revision); one tuple per archived message."""
    return [(i['account'],i['display_text'].split('\n')[0],i['revision']) for i in rpc(client,key,'search',limit=50)['items']]


def source(client,key,account):
    return {s['account']:s for s in rpc(client,key,'get_sync_status',detail='full',refresh=True)['sources']}[account]


def tombstone(client,key,account,chat,provider_id):
    return rpc(client,key,'get_context',message_id=stable_id('msg',account,chat,provider_id),detail='full')['message']['deleted']


def restart(archive,collector,state_class,provider,**spools):
    """Stop a collector, take a new lease and reopen its spools, as a new process would."""
    asyncio.run(collector.provider.disconnect())
    archive.release(collector.account,collector.owner,collector.state.lease[1])
    owner=collector.owner+'-restarted';epoch=archive.acquire(collector.account,owner)
    fresh=type(collector)(archive,collector.account,provider,
        CaptureSpool(collector.spool.path,reserve_bytes=0),batch_size=collector.batch_size,
        writer=BlobWriter(archive.store.files,reserve_bytes=0),owner=owner,history_since=collector.history_since,
        **{name:CaptureSpool(spool.path,reserve_bytes=0) for name,spool in spools.items()})
    fresh.state=state_class(archive,fresh.account,(owner,epoch));fresh.policy=fresh.state.policy()
    return fresh


# spec: ANYM-413
def test_both_providers_backfill_live_restart_and_status_through_mcp(worker,wa):
    tg,s,_=worker;w,_,_=wa;archive=tg.archive
    key=s.issue_key('flow-reader',[tg.account,w.account])
    tg.history_since=history_bound('2026-07-13T00:00:00Z')
    tg.provider.messages=[at(1,'2026-06-01'),at(2,'2026-07-12'),at(3,'2026-07-13'),at(4,'2026-07-20'),at(5,'2026-08-01')]
    with TestClient(create_app(s,['testserver'])) as client:
        # Backfill: Telegram stops at its window, WhatsApp pages through to the provider's first message.
        for _ in range(8):
            asyncio.run(tg.sync_once());asyncio.run(w.sync_once())
            if tg.state.chat('100','user')['window_complete'] and w.state.progress('123@c.us','user').get('history_exhausted'):break
        assert sorted((a,t) for a,t,_ in rows(client,key))==sorted(
            [(tg.account,f'message {n}') for n in (3,4,5)]+[(w.account,f'item {n}') for n in range(1,6)])
        chat=tg.state.chat('100','user');progress=w.state.progress('123@c.us','user')
        assert chat['window_complete'] and not chat['history_exhausted']
        assert progress['history_exhausted'] and not progress.get('catchup_before') and not progress.get('gap')
        history=source(client,key,tg.account)['history']
        assert history['configured_window']['complete']==1 and history['configured_window']['in_progress']==0
        assert history['provider_history_exhausted']==0 and history['full_history_verified'] is False
        history=source(client,key,w.account)['history']
        assert history['successful_scan_chats']==1 and history['provider_history_exhausted']==1
        assert history['gap_chats']==0 and history['full_history_verified'] is False

        # Live: a new, an edited and a deleted message per provider pass through the spools.
        edited=at(5,'2026-08-01');edited.message='edited 5';edited.edit_date=datetime(2026,10,2,tzinfo=timezone.utc)
        asyncio.run(tg.capture(NS(message=message(6))));asyncio.run(tg.capture(NS(message=edited)))
        asyncio.run(tg.deleted(NS(chat_id=100,deleted_ids=[4])))
        w.events_spool.put(capture(event(6)));w.events_spool.put(capture(event(5,body='corrected',edited=1791840000000),change='edit'))
        w.events_spool.put({'kind':'whatsapp_delete','account':w.account,'chat':'123@c.us','ids':[event(4)['messageId']],
            'observed_at':'2026-10-13T00:00:00+00:00'})
        assert len(list(tg.spool.path.glob('*.json')))==3 and len(list(w.events_spool.path.glob('*.json')))==3
        tg.spool.drain(tg.state.apply);w.events_spool.drain(w.state.apply)
        found=rows(client,key)
        assert len(found)==len({(a,t) for a,t,_ in found})==len(
            {(tg.account,'message 3'),(tg.account,'edited 5'),(tg.account,'message 6'),
             (w.account,'item 1'),(w.account,'item 2'),(w.account,'item 3'),(w.account,'corrected'),(w.account,'item 6')})
        assert {(a,t) for a,t,_ in found}=={(tg.account,'message 3'),(tg.account,'edited 5'),(tg.account,'message 6'),
            (w.account,'item 1'),(w.account,'item 2'),(w.account,'item 3'),(w.account,'corrected'),(w.account,'item 6')}
        assert {(a,t):r for a,t,r in found}[(tg.account,'edited 5')]==2
        assert {(a,t):r for a,t,r in found}[(w.account,'corrected')]==2
        assert tombstone(client,key,tg.account,'100','4') and tombstone(client,key,w.account,'123@c.us',event(4)['messageId'])
        assert not tombstone(client,key,tg.account,'100','3')

        # Restart: one more capture and a redelivered one wait in each spool when the collectors stop.
        before=(tg.state.chat('100','user')['high_water'],w.state.progress('123@c.us','user')['head'])
        asyncio.run(tg.capture(NS(message=message(7))));asyncio.run(tg.capture(NS(message=message(6))))
        w.events_spool.put(capture(event(7)));w.events_spool.put(capture(event(6)))
        tg2=restart(archive,tg,CollectorState,FakeProvider(tg.provider.messages))
        w2=restart(archive,w,WhatsAppState,Provider(),events_spool=w.events_spool)
        w2.provider.messages=list(w.provider.messages)
        for old in (tg,w):  # The old instances lost their lease and cannot consume what they left behind.
            with pytest.raises(RuntimeError,match='lease_lost'):(old.spool if old is tg else old.events_spool).drain(old.state.apply)
        assert len(list(tg2.spool.path.glob('*.json')))==2 and len(list(w2.events_spool.path.glob('*.json')))==2
        # The providers now carry the live changes: an edited 5, 6 and 7, and no deleted 4 (the tombstones must survive without them).
        tg2.provider.messages=[m for m in tg2.provider.messages if m.id not in (4,5)]+[edited,message(6),message(7)]
        w2.provider.messages=[e for e in w2.provider.messages if e['messageId']!=event(4)['messageId'] and e['messageId']!=event(5)['messageId']]+[
            event(5,body='corrected',edited=1791840000000),event(6),event(7)]
        tg2.spool.drain(tg2.state.apply);w2.events_spool.drain(w2.state.apply)
        assert not list(tg2.spool.path.glob('*.json')) and not list(w2.events_spool.path.glob('*.json'))
        # Only the drain has run: the spooled captures are archived, and no cursor moved.
        drained=rows(client,key)
        assert len(drained)==len({(a,t) for a,t,_ in drained})==10
        assert {(a,t) for a,t,_ in drained}>={(tg.account,'message 7'),(w.account,'item 7')}
        assert (tg2.state.chat('100','user')['high_water'],w2.state.progress('123@c.us','user')['head'])==before
        tg2.provider.reads.clear()
        for _ in range(3):asyncio.run(tg2.sync_once());asyncio.run(w2.sync_once())
        found=rows(client,key)
        assert len(found)==len({(a,t) for a,t,_ in found})==10
        assert {(a,t) for a,t,_ in found}>={(tg.account,'message 7'),(w.account,'item 7')}
        assert {(a,t):r for a,t,r in found}[(tg.account,'edited 5')]==2
        assert tombstone(client,key,tg.account,'100','4') and tombstone(client,key,w.account,'123@c.us',event(4)['messageId'])
        after=(tg2.state.chat('100','user')['high_water'],w2.state.progress('123@c.us','user')['head'])
        assert after[0]>=before[0] and after[1]>=before[1]
        assert tg2.provider.reads and all(read[1]>=before[0] for read in tg2.provider.reads if read[3])
        assert tg2.state.chat('100','user')['window_complete'] and w2.state.progress('123@c.us','user')['history_exhausted']

        # Health: both sources show the three phases; a connection failure degrades only its own source.
        for c in (tg2,w2):
            c.state.phase('connection','connected','provider_ready');c.state.capture_progress(None,0,0)
            c.state.status('receiving','provider_ready')
        for account in (tg.account,w.account):
            phases=source(client,key,account)['operations']
            assert phases['connection']['state']=='connected' and phases['live_capture']['state']=='receiving'
            assert phases['history']['state']=='partial' and phases['connection']['collector_running'] is True
        tg2.state.connection_failed('provider_disconnected')
        tg2.state.status('degraded','provider_temporarily_unavailable')
        broken=source(client,key,tg.account)
        assert broken['status']=='degraded' and broken['operations']['connection']['state']=='retry_wait'
        assert broken['operations']['connection']['reason_code']=='provider_disconnected'
        assert broken['operations']['live_capture']['state']=='paused' and broken['liveness']['connected'] is False
        healthy=source(client,key,w.account)
        assert healthy['status']=='receiving' and healthy['operations']['connection']['state']=='connected'
        tg2.state.phase('connection','connected','provider_ready');tg2.state.capture_progress(None,0,0)
        tg2.state.status('receiving','provider_ready')
        fixed=source(client,key,tg.account)
        assert fixed['status']=='receiving' and fixed['operations']['connection']['state']=='connected'
        assert fixed['operations']['connection']['retry_attempts']==0
        assert fixed['operations']['live_capture']['state']=='receiving' and fixed['liveness']['connected'] is True
