"""Durable progress and health scopes using isolated PostgreSQL + fake providers."""
import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace as NS

import pytest

from messaging_service.collector_state import CollectorState
from messaging_service.envelope import stable_id
from messaging_service.sync_status import _history,get_sync_status
from test_collectors import worker as worker,message


@pytest.mark.parametrize('attempt',[None,True,'100',float('inf'),float('nan')])
def test_whatsapp_summary_rejects_invalid_attempt_time_and_uncommitted_scan(attempt):
    account='synthetic-wa';chat='1@c.us'
    row={'account':account,'provider_chat':chat,
         'wa_progress':{'last_attempt_at':attempt},
         'wa_last_scan_at':datetime.now(timezone.utc)}
    result=_history('whatsapp',{stable_id('chat',account,chat)},[row])
    assert result['observed_chats']==1 and result['not_scanned_chats']==1
    assert result['last_attempt_at'] is None and result['last_scan_at'] is None
    assert result['successful_scan_chats']==result['provider_history_exhausted']==0


def test_partial_discovery_is_durable_and_does_not_start_history(worker):
    w,s,_=worker
    async def batches(limit):
        yield [('100','user','First','private'),('200','channel','Group','group')]
        raise RuntimeError('interrupted')
    w.provider.dialog_batches=batches
    with pytest.raises(RuntimeError,match='interrupted'):asyncio.run(w.sync_once())
    assert w.provider.reads==[] and w._dialog_cache is None
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) n FROM chats WHERE account=%s',(w.account,)).fetchone()['n']==2
        health=c.execute('SELECT * FROM collector_health WHERE account=%s',(w.account,)).fetchone()
        assert health['discovery_count']==2 and health['discovery_started_at']
        assert health['discovery_completed_at'] is None
        assert c.execute('SELECT chat_type FROM chats WHERE id=%s',
            (stable_id('chat',w.account,'200'),)).fetchone()['chat_type']=='group'
    # Replaying a restarted traversal is idempotent; old catalog rows survive.
    async def restarted(limit):
        yield [('100','user','First'),('200','channel','Group','group')]
        yield [('300','user','Third')]
    async def page(chat,**kwargs):
        with s.pool.connection() as c:
            assert c.execute('SELECT count(*) n FROM chats WHERE account=%s',(w.account,)).fetchone()['n']==3
        return []
    w.provider.dialog_batches=restarted;w.provider.page=page
    asyncio.run(w.sync_once())
    with s.pool.connection() as c:
        health=c.execute('SELECT * FROM collector_health WHERE account=%s',(w.account,)).fetchone()
        assert health['discovery_count']==3 and health['discovery_completed_at']>=health['discovery_started_at']
        assert not health['discovery_limited']


def test_persisted_cooldown_is_monotonic_and_survives_state_recreation(worker):
    w,s,_=worker
    w.state.defer(300)
    with s.pool.connection() as c:
        before=c.execute('SELECT cooldown_until FROM collector_health WHERE account=%s',(w.account,)).fetchone()
    w.state.defer(1)
    reopened=CollectorState(w.archive,w.account,w.state.lease)
    assert reopened.cooldown_remaining()>290
    with s.pool.connection() as c:
        assert c.execute('SELECT cooldown_until FROM collector_health WHERE account=%s',(w.account,)).fetchone()==before


def test_live_ingest_and_conflicts_do_not_hide_operational_cooldown(worker):
    w,s,_=worker
    w.state.defer(60)
    with s.pool.connection() as c:
        c.execute("UPDATE source_status SET detail_code='version_conflict' WHERE account=%s",(w.account,))
    asyncio.run(w.capture(NS(message=message(9))));w.spool.drain(w.state.apply)
    # Both a live receipt and a normal scan status must leave the active pause visible.
    w.state.status('receiving','telegram_history_partial')
    with s.pool.connection() as c:
        status=get_sync_status(c,{'accounts':[w.account],'chat_ids':None})['sources'][0]
    assert status['status']=='degraded' and status['detail_code']=='rate_limited'
    assert status['last_ingest_at']
    # Ingest keeps the stored conflict code; with no unresolved conflict it is
    # reported as unreconciled coverage rather than a live conflict alert.
    assert status['archive_detail_code']=='coverage_not_reconciled' and status['active_conflicts']==0
    assert 'archive_version_conflicts' not in status['alerts']
    with s.pool.connection() as c:
        stored=c.execute('SELECT detail_code FROM source_status WHERE account=%s',(w.account,)).fetchone()
    assert stored['detail_code']=='version_conflict'
    w.state.status('stopped','requires_reconciliation')
    assert w.state.cooldown_remaining()>0  # Graceful stop must not erase provider wait.


def test_chat_scoped_status_never_exposes_account_health_or_hidden_aggregates(worker):
    w,s,_=worker
    for mid,chat in [(1,'100'),(2,'200'),(3,'200')]:
        asyncio.run(w.capture(NS(message=message(mid,chat=chat))))
    w.spool.drain(w.state.apply);w.state.defer(60)
    with s.pool.connection() as c:
        result=get_sync_status(c,{'accounts':[w.account],
            'chat_ids':[stable_id('chat',w.account,'100')]})
    assert result['scope']=='permitted_chats' and result['coverage']=='partial'
    assert len(result['sources'])==1
    row=result['sources'][0]
    assert row['messages']==1 and row['catalog_chats']==1
    assert set(row)=={'account','platform','catalog_chats','messages','oldest_message_at','newest_message_at','history','files'}
    assert row['history']['checkpoint_chats']==1 and row['history']['unknown_chats']==1
    with s.pool.connection() as c:
        assert get_sync_status(c,{'accounts':[w.account],'chat_ids':[]})['sources']==[]


def test_catalog_cap_is_visible_and_does_not_claim_all_dialogs(worker):
    w,s,_=worker;w.max_dialogs=1
    asyncio.run(w.sync_once())
    with s.pool.connection() as c:
        row=get_sync_status(c,{'accounts':[w.account],'chat_ids':None})['sources'][0]
    assert row['discovery_limited'] is True


def test_inventory_does_not_declassify_known_group_on_three_field_replay(worker):
    w,s,_=worker
    w.state.inventory([('200','channel','Group','group')],None)
    w.state.inventory([('200','channel','Group')],None)
    with s.pool.connection() as c:
        assert c.execute('SELECT chat_type FROM chats WHERE id=%s',
            (stable_id('chat',w.account,'200'),)).fetchone()['chat_type']=='group'


def test_sync_status_distinguishes_configured_window_full_traversal_and_unknown(worker):
    w,s,_=worker
    since=datetime(2026,7,12,16,tzinfo=timezone.utc)
    w.state.inventory([(str(i),'user','Synthetic') for i in range(100,700,100)],None)
    with s.pool.connection() as c:
        c.execute('UPDATE collector_chats SET history_since=%s WHERE account=%s AND provider_chat=ANY(%s)',
                  (since,w.account,['100','200','300']))
        c.execute("UPDATE collector_chats SET initialized=true,window_complete=true WHERE account=%s AND provider_chat='100'",(w.account,))
        c.execute('UPDATE collector_chats SET initialized=true,history_exhausted=true WHERE account=%s AND provider_chat=ANY(%s)',
                  (w.account,['300','500']))
        c.execute("UPDATE collector_chats SET initialized=true WHERE account=%s AND provider_chat='400'",(w.account,))
        # Saved chat with no checkpoint/config observation must remain unknown.
        c.execute('INSERT INTO chats(id,account,title) VALUES(%s,%s,%s)',
                  (stable_id('chat',w.account,'700'),w.account,'Unobserved'))
        row=get_sync_status(c,{'accounts':[w.account],'chat_ids':None})['sources'][0]
        scoped=get_sync_status(c,{'accounts':[w.account],'chat_ids':[stable_id('chat',w.account,'100')]})['sources'][0]
    history=row['history']
    assert row['catalog_chats']==7 and history['checkpoint_chats']==6 and history['unknown_chats']==2
    assert history['configured_window']=={'chats':3,'complete':2,'in_progress':1,'since_min':since,'since_max':since}
    assert history['unbounded_history']=={'chats':2,'provider_exhausted':1,'in_progress':1}
    assert history['provider_history_exhausted']==2 and history['full_history_verified'] is False
    assert scoped['history']['configured_window']['complete']==1
    assert scoped['history']['unbounded_history']['chats']==0 and scoped['history']['provider_history_exhausted']==0


def test_whatsapp_status_does_not_treat_checkpoint_creation_as_successful_scan(worker):
    from messaging_service.whatsapp_collector import WhatsAppState
    w,s,_=worker
    account='wa-status'
    w.archive.register(account,'whatsapp','Synthetic WA',discover=True)
    epoch=w.archive.acquire(account,'wa-owner')
    state=WhatsAppState(w.archive,account,('wa-owner',epoch))
    chats=[str(i)+'@c.us' for i in range(1,6)]
    state.inventory([(chat,'user','Synthetic') for chat in chats],None)
    state.progress(chats[0],'user')
    state.begin_attempt(chats[1],'user')
    state.progress(chats[2],'user')
    state.checkpoint_wa(chats[2],{'head':[20,'m2'],'history_exhausted':False,'catchup_before':[30,'m3']})
    state.progress(chats[3],'user')
    state.checkpoint_wa(chats[3],{'history_exhausted':True,'gap':True})
    # Empty WA dialogs acquire checkpoints without any archived chat row.
    state.progress('6@c.us','user')
    state.checkpoint_wa('6@c.us',{'history_exhausted':True})
    with s.pool.connection() as c:
        history=get_sync_status(c,{'accounts':[account],'chat_ids':None})['sources'][0]['history']
        unscanned=get_sync_status(c,{'accounts':[account],
            'chat_ids':[stable_id('chat',account,chats[1])]})['sources'][0]['history']
    assert history['observed_chats']==6 and history['checkpoint_chats']==5 and history['unknown_chats']==1
    assert history['successful_scan_chats']==3 and history['not_scanned_chats']==2
    assert history['provider_history_exhausted']==2 and history['catchup_pending_chats']==1 and history['gap_chats']==1
    assert history['last_scan_at'] and history['full_history_verified'] is False
    assert unscanned['last_attempt_at'] and unscanned['last_scan_at'] is None
    assert unscanned['gap_chats']==0 and unscanned['successful_scan_chats']==0


def test_sync_status_counts_only_current_allowed_file_occurrences(worker):
    w,s,_=worker
    for mid,chat in [(10,'100'),(20,'200'),(30,'100'),(40,'100'),(50,'100')]:
        asyncio.run(w.capture(NS(message=message(mid,chat=chat,media=True))))
    w.spool.drain(w.state.apply)
    with s.pool.connection() as c:
        jobs=c.execute('''SELECT j.id,j.message,m.provider_id FROM media_jobs j
            JOIN messages m ON m.id=j.message WHERE m.account=%s ORDER BY m.provider_id''',(w.account,)).fetchall()
        for job in jobs:
            mid=job['provider_id']
            c.execute('UPDATE media_jobs SET state=%s WHERE id=%s',
                      ('failed' if mid=='50' else 'ready',job['id']))
            if mid=='50':continue
            c.execute('''INSERT INTO attachments(id,message,digest,filename,mime,bytes)
                VALUES(%s,%s,%s,'synthetic.txt','text/plain',4)''',(job['id'],job['message'],'a'*64))
            c.execute("UPDATE attachment_text SET state='ready',truncated=true WHERE attachment=%s",(job['id'],))
            c.execute("UPDATE attachment_enrichment SET state='ready' WHERE attachment=%s",(job['id'],))
            if mid=='10':
                # Missing enrichment configuration remains its own state.
                c.execute("UPDATE attachment_enrichment SET state='unconfigured' WHERE attachment=%s",(job['id'],))
            if mid=='30':
                # A stale output cannot count as ready even if the state says so.
                c.execute('UPDATE attachment_text SET source_revision=source_revision+1 WHERE attachment=%s',(job['id'],))
                c.execute('UPDATE attachment_enrichment SET source_digest=%s WHERE attachment=%s',('b'*64,job['id']))
            if mid=='40':
                c.execute('UPDATE attachments SET active=false WHERE id=%s',(job['id'],))
                c.execute("UPDATE media_jobs SET state='superseded' WHERE id=%s",(job['id'],))
        result=get_sync_status(c,{'accounts':[w.account],
            'chat_ids':[stable_id('chat',w.account,'100')]})['sources'][0]['files']
        complete=get_sync_status(c,{'accounts':[w.account],'chat_ids':None})['sources'][0]['files']
    assert result['stored_files']==2 and result['stored_bytes']==8
    assert result['extraction']['ready']==1 and result['extraction']['unknown']==1
    assert result['extraction_truncated']==1
    assert result['enrichment']['ready']==0 and result['enrichment']['unconfigured']==1 and result['enrichment']['unknown']==1
    assert result['download']=={'queued':0,'leased':0,'ready':2,'failed':1,'superseded':1}
    assert complete['stored_files']==3 and complete['enrichment']['ready']==1


def test_sync_status_missing_derived_rows_are_unknown_not_configured_or_ready(worker):
    w,s,_=worker
    asyncio.run(w.capture(NS(message=message(9,media=True))));w.spool.drain(w.state.apply)
    with s.pool.connection() as c:
        job=c.execute('SELECT id,message FROM media_jobs WHERE account=%s',(w.account,)).fetchone()
        c.execute('''INSERT INTO attachments(id,message,digest,filename,mime,bytes)
            VALUES(%s,%s,%s,'unknown.txt','text/plain',0)''',(job['id'],job['message'],'a'*64))
        c.execute('DELETE FROM attachment_text WHERE attachment=%s',(job['id'],))
        c.execute('DELETE FROM attachment_enrichment WHERE attachment=%s',(job['id'],))
        result=get_sync_status(c,{'accounts':[w.account],'chat_ids':None})['sources'][0]['files']
    assert result['extraction']['unknown']==result['enrichment']['unknown']==1
    assert result['enrichment']['unconfigured']==result['enrichment']['ready']==0
