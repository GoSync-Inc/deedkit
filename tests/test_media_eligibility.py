"""Sync status reports why each queued original waits, not a sticky recorded code (ANYM-284)."""
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.types.json import Jsonb

from messaging_service.collector_state import CollectorState
from messaging_service.sync_status import get_sync_status
from test_archive import archive, event


ACCOUNT='archive-test'


def queued(ar, s, *, mid='301', days=30, code=None, requested=False):
    at=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
    message=ar.ingest(event(provider_id=mid,sent_at=at,updated_at=at,
        media=[{'provider_id':'media-'+mid,'filename':'Scan '+mid+'.pdf','mime':'application/pdf','bytes':64}]))['message']
    with s.pool.connection() as c:
        c.execute('''UPDATE media_jobs SET error_code=%s,requested_until=CASE WHEN %s
            THEN clock_timestamp()+interval '1 hour' END WHERE message=%s''',(code,requested,message))
        return c.execute('SELECT id FROM media_jobs WHERE message=%s',(message,)).fetchone()['id']


def publish(ar, *, days=7, since=None, chat_since='published'):
    """A collector run's lease and window; optionally the chat row's recorded bound."""
    epoch=ar.acquire(ACCOUNT,'eligibility-owner')
    state=CollectorState(ar,ACCOUNT,('eligibility-owner',epoch))
    state.media_policy(days,since)
    if chat_since!='absent':
        with ar.store.pool.connection() as c:
            c.execute('''INSERT INTO collector_chats(account,provider_chat,peer_kind,history_since)
                VALUES(%s,'100','chat',%s) ON CONFLICT(account,provider_chat) DO UPDATE SET history_since=excluded.history_since''',
                (ACCOUNT,since if chat_since=='published' else chat_since))
    return state


def media(s):
    with s.pool.connection() as c:
        source=get_sync_status(c,{'accounts':[ACCOUNT],'chat_ids':None})['sources'][0]
    return source['operations']['media'],source['files']


# spec: ANYM-284
def test_paused_job_outside_window_is_not_reported_as_waiting_for_storage(archive):
    ar,s,_,_=archive;publish(ar)
    queued(ar,s,code='paused_storage')
    result,files=media(s)
    assert result['storage_paused_basis']=='eligible_jobs' and result['prefetch_window_days']==7
    assert result['storage_paused_jobs']==0 and result['storage_paused'] is False
    assert result['outside_prefetch_window_jobs']==1 and result['eligible_queued_jobs']==0
    assert result['state']!='paused_storage'
    # The recorded code stays as history evidence, not as a current diagnosis.
    assert result['errors']=={'paused_storage':1}
    assert [d['reason_code'] for d in result['diagnostics']]==['outside_prefetch_window']
    assert result['diagnostics'][0]['next_action']=='request_file_download'
    assert files['download']['queued']==1 and files['current_files']==1


# spec: ANYM-284
def test_requested_old_job_is_paused_for_storage_again(archive):
    ar,s,_,_=archive;publish(ar)
    queued(ar,s,code='paused_storage',requested=True)
    result,_=media(s)
    assert result['storage_paused_jobs']==1 and result['state']=='paused_storage'
    assert result['outside_prefetch_window_jobs']==0


# spec: ANYM-284
def test_in_window_storage_pause_is_still_counted(archive):
    ar,s,_,_=archive;publish(ar)
    queued(ar,s,days=1,code='paused_storage')
    queued(ar,s,mid='302',code='paused_storage')
    result,_=media(s)
    assert result['storage_paused_jobs']==1 and result['state']=='paused_storage'
    assert result['outside_prefetch_window_jobs']==1
    paused=[d for d in result['diagnostics'] if d['reason_code']=='paused_storage']
    assert [(d['job_state'],d['count']) for d in paused]==[('queued',1)]


# spec: ANYM-284
def test_attachment_outside_window_is_available_on_request_not_blocked(archive):
    ar,s,key,_=archive;publish(ar)
    fid=queued(ar,s,code='paused_storage')
    result=s.get_attachment(key,fid)
    assert result['download_phase']=='available_on_request' and result['download_phase']!='blocked'
    assert result['next_action']=='request_file_download'
    assert result['download_reason']=='outside_prefetch_window' and result['error_code']=='paused_storage'
    assert result['download_status']=='queued' and result['download_eligibility']=='outside_prefetch_window'
    from messaging_service.result_evidence import decorate
    from messaging_service.conversation import present_message
    with s.pool.connection() as c:
        row=c.execute('SELECT * FROM messages WHERE id=%s',(result['message_id'],)).fetchone()
        decorate(c,[row])
    assert row['attachments'][0]['download_reason']=='outside_prefetch_window'
    text=present_message({**row,'body':''},0)['display_text']
    assert 'доступен по запросу' in text and 'недостаточно места' not in text


# spec: ANYM-284
@pytest.mark.parametrize('publication',['absent','earlier_run','malformed'])
def test_without_current_window_status_keeps_recorded_counting(archive,publication):
    ar,s,_,_=archive
    queued(ar,s,code='paused_storage')
    if publication!='absent':
        state=publish(ar)
        with s.pool.connection() as c:
            if publication=='earlier_run':
                # A later run that could not publish must not inherit this window.
                c.execute("UPDATE collector_leases SET epoch=epoch+1 WHERE account=%s",(ACCOUNT,))
            else:
                value=c.execute('SELECT operations FROM collector_health WHERE account=%s',(ACCOUNT,)).fetchone()['operations']
                value['media_policy']['history_since']='not-a-time'
                c.execute('UPDATE collector_health SET operations=%s WHERE account=%s',(Jsonb(value),ACCOUNT))
        del state
    result,_=media(s)
    assert result['storage_paused_basis']=='unknown' and result['prefetch_window_days'] is None
    assert result['storage_paused_jobs']==1 and result['state']=='paused_storage'
    assert result['outside_prefetch_window_jobs'] is None and result['eligible_queued_jobs'] is None


# spec: ANYM-284
def test_download_counts_keep_one_state_per_occurrence(archive):
    from messaging_service.blobs import BlobWriter
    ar,s,_,_=archive;publish(ar)
    queued(ar,s,days=1,mid='303')
    job=ar.claim_media('fixture',media_prefetch_days=7)
    writer=BlobWriter(s.files,reserve_bytes=0);ar.finish_media(job,writer.write([b's'*64]),writer)
    queued(ar,s,code='paused_storage')
    result,files=media(s)
    assert files['download']['ready']==files['stored_files']==1
    assert files['download']['queued']==1 and files['current_files']==2
    assert result['counts']==files['download']


# spec: ANYM-284
def test_dormant_jobs_without_code_are_not_pending_work(archive):
    ar,s,_,_=archive;publish(ar)
    queued(ar,s)
    result,_=media(s)
    assert result['state']=='idle' and result['outside_prefetch_window_jobs']==1
    assert result['eligible_queued_jobs']==0 and result['retry_queued_jobs']==0
    queued(ar,s,mid='304',days=1)
    result,_=media(s)
    assert result['state']=='queued' and result['eligible_queued_jobs']==1


# spec: ANYM-284
def test_unbounded_window_and_excluded_chat(archive):
    ar,s,_,_=archive;publish(ar,days=None)
    queued(ar,s,code='paused_storage')
    result,_=media(s)
    assert result['storage_paused_jobs']==1 and result['prefetch_window_days'] is None
    assert result['storage_paused_basis']=='eligible_jobs'
    with s.pool.connection() as c:
        c.execute("UPDATE account_policies SET exclude_chats=ARRAY['100'] WHERE account=%s",(ACCOUNT,))
    result,_=media(s)
    assert result['storage_paused_jobs']==0 and result['collection_excluded_jobs']==1


def bounded(ar, s, **publication):
    since=datetime.now(timezone.utc)-timedelta(days=60)
    publish(ar,since=since,**publication)
    return since


# spec: ANYM-284
def test_request_before_confirmed_history_bound_is_unavailable(archive):
    ar,s,key,_=archive;bounded(ar,s)
    fid=queued(ar,s,days=90,code='paused_storage')
    result=s.request_file_download(key,fid)
    assert result['request_status']=='unavailable' and result['reason']=='outside_history_bounds'
    assert result['requested_until'] is None and result['next_action']!='request_file_download'
    with s.pool.connection() as c:
        assert c.execute('SELECT requested_until FROM media_jobs WHERE id=%s',(fid,)).fetchone()['requested_until'] is None
    attachment=s.get_attachment(key,fid)
    assert attachment['download_reason']=='outside_history_bounds'
    assert attachment['next_action']!='request_file_download' and attachment['download_phase']!='blocked'
    status,_=media(s)
    assert status['outside_history_bounds_jobs']==1 and status['outside_prefetch_window_jobs']==0
    # Inside the bound the same old job is available on request and becomes eligible.
    inside=queued(ar,s,mid='305',days=30)
    requested=s.request_file_download(key,inside)
    assert requested['request_status']=='queued' and requested['download_eligibility']=='eligible'
    assert 'eligibility_reason' not in requested and requested['download_phase']=='queued'


# spec: ANYM-284
def test_existing_request_is_not_extended_outside_history_bound(archive):
    ar,s,key,_=archive;bounded(ar,s)
    fid=queued(ar,s,days=90,requested=True)
    result=s.request_file_download(key,fid)
    assert result['request_status']=='unavailable' and result['reason']=='outside_history_bounds'


# spec: ANYM-284
def test_stored_original_before_history_bound_stays_ready(archive):
    from messaging_service.blobs import BlobWriter
    ar,s,key,_=archive
    queued(ar,s,days=90,mid='306')
    job=ar.claim_media('fixture')
    writer=BlobWriter(s.files,reserve_bytes=0);ar.finish_media(job,writer.write([b'o'*64]),writer)
    bounded(ar,s)
    result=s.request_file_download(key,job['id'])
    assert result['request_status']=='ready' and result['reason']=='original_downloaded'
    assert s.get_attachment(key,job['id'])['download_url']


# spec: ANYM-284
@pytest.mark.parametrize('evidence',['absent','live_callback_row','stale_row','no_publication'])
def test_unknown_history_bound_records_request_without_promising_download(archive,evidence):
    ar,s,key,_=archive
    fid=queued(ar,s,days=90)
    if evidence=='absent':bounded(ar,s,chat_since='absent')
    elif evidence=='live_callback_row':bounded(ar,s,chat_since=None)
    elif evidence=='stale_row':bounded(ar,s,chat_since=datetime.now(timezone.utc)-timedelta(days=120))
    result=s.request_file_download(key,fid)
    assert result['request_status']=='queued' and result['requested_until']
    assert result['download_eligibility']=='unknown' and result['eligibility_reason']=='history_bound_unknown'
    if evidence!='no_publication':
        status,_=media(s)
        assert status['outside_history_bounds_jobs']==0


# spec: ANYM-284
def test_confirmed_unbounded_history_never_reports_outside_bounds(archive):
    ar,s,key,_=archive;publish(ar,since=None)
    fid=queued(ar,s,days=900)
    result=s.request_file_download(key,fid)
    assert result['request_status']=='queued' and result['download_eligibility']=='eligible'


# spec: ANYM-284
def test_managed_evicted_original_before_history_bound_is_unavailable(archive):
    from test_file_lifecycle import processed, lifecycle
    ar,s,key,_=archive;fid,_=processed(archive)
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET sent_at=clock_timestamp()-interval '90 days' WHERE id=(SELECT message FROM media_jobs WHERE id=%s)",(fid,))
    assert lifecycle().LifecycleWorker(s).run_once(apply=True)['evicted']==1
    bounded(ar,s)
    attachment=s.get_attachment(key,fid)
    assert attachment['download_reason']=='outside_history_bounds'
    assert attachment['next_action']!='request_file_download'
    result=s.request_file_download(key,fid)
    assert result['request_status']=='unavailable' and result['reason']=='outside_history_bounds'
    with s.pool.connection() as c:
        assert c.execute('SELECT state,requested_until FROM media_jobs WHERE id=%s',(fid,)).fetchone()=={
            'state':'ready','requested_until':None}


# spec: ANYM-284
def test_collector_run_publishes_its_effective_window(archive):
    ar,s,_,_=archive
    since=datetime(2026,7,13,8,tzinfo=timezone.utc)
    state=publish(ar,days=0,since=since,chat_since='absent')
    with s.pool.connection() as c:
        value=c.execute('SELECT operations FROM collector_health WHERE account=%s',(ACCOUNT,)).fetchone()['operations']
    assert value['media_policy']['prefetch_days']==0
    assert value['media_policy']['history_since']==since.isoformat()
    assert value['media_policy']['epoch']==state.lease[1]
    for bad in [(-1,None),(7,datetime(2026,7,13)),(7,'2026-07-13')]:
        with pytest.raises(ValueError):state.media_policy(*bad)


# spec: ANYM-284
@pytest.mark.parametrize('evidence',['absent','live_callback_row','stale_row'])
@pytest.mark.parametrize('age',[30,90])
def test_unknown_history_bound_holds_for_repeated_request_and_passive_reads(archive,evidence,age):
    ar,s,key,_=archive
    fid=queued(ar,s,days=age,code='paused_storage')
    bounded(ar,s,chat_since={'absent':'absent','live_callback_row':None,
        'stale_row':datetime.now(timezone.utc)-timedelta(days=120)}[evidence])
    before=s.get_attachment(key,fid)
    first=s.request_file_download(key,fid)
    again=s.request_file_download(key,fid)
    after=s.get_attachment(key,fid)
    assert first['request_status']=='queued' and again['request_status']=='coalesced'
    for result in (before,first,again,after):
        assert result['download_eligibility']=='unknown'
        assert result['eligibility_reason']=='history_bound_unknown'
        assert result['download_phase']!='available_on_request'
    assert again['next_action']!='request_file_download' and after['next_action']!='request_file_download'
    status,_=media(s)
    # Before the run's own bound the run never claims it; inside, the request makes it eligible.
    assert status['history_bound_unknown_jobs']==(1 if age==90 else 0)
    assert status['outside_history_bounds_jobs']==status['outside_prefetch_window_jobs']==0


# spec: ANYM-284
@pytest.mark.parametrize('change',['excluded','disabled'])
def test_excluded_collection_is_not_advertised_as_available_on_request(archive,change):
    from messaging_service.blobs import BlobWriter
    from messaging_service.result_evidence import decorate
    from messaging_service.conversation import present_message
    ar,s,key,_=archive;publish(ar)
    queued(ar,s,days=1,mid='307')
    job=ar.claim_media('fixture',media_prefetch_days=7)
    writer=BlobWriter(s.files,reserve_bytes=0);ar.finish_media(job,writer.write([b'p'*64]),writer)
    fid=queued(ar,s,code='paused_storage')
    with s.pool.connection() as c:
        if change=='excluded':c.execute("UPDATE account_policies SET exclude_chats=ARRAY['100'] WHERE account=%s",(ACCOUNT,))
        else:c.execute('UPDATE account_policies SET enabled=false WHERE account=%s',(ACCOUNT,))
    attachment=s.get_attachment(key,fid)
    assert attachment['download_eligibility']=='collection_excluded'
    assert attachment['download_reason']=='collection_excluded' and attachment['download_phase']=='blocked'
    assert attachment['next_action']=='check_collection_policy'
    result=s.request_file_download(key,fid)
    assert result['request_status']=='blocked' and result['reason']=='collection_disabled'
    with s.pool.connection() as c:
        row=c.execute('SELECT * FROM messages WHERE id=%s',(attachment['message_id'],)).fetchone()
        decorate(c,[row])
    assert row['attachments'][0]['next_action']=='check_collection_policy'
    assert 'доступен по запросу' not in present_message({**row,'body':''},0)['display_text']
    # A present authorized original stays readable.
    assert s.get_attachment(key,job['id'])['download_url']


# spec: ANYM-284
@pytest.mark.parametrize('change',['excluded','disabled'])
@pytest.mark.parametrize('requeued',[False,True])
def test_excluded_collection_blocks_managed_evicted_redownload_advice(archive,change,requeued):
    from test_file_lifecycle import processed, lifecycle
    ar,s,key,_=archive;fid,_=processed(archive)
    assert lifecycle().LifecycleWorker(s).run_once(apply=True)['evicted']==1
    publish(ar)
    if requeued:assert s.request_file_download(key,fid)['request_status']=='queued'
    with s.pool.connection() as c:
        if change=='excluded':c.execute("UPDATE account_policies SET exclude_chats=ARRAY['100'] WHERE account=%s",(ACCOUNT,))
        else:c.execute('UPDATE account_policies SET enabled=false WHERE account=%s',(ACCOUNT,))
    attachment=s.get_attachment(key,fid)
    assert attachment['download_eligibility']=='collection_excluded'
    assert attachment['download_reason']=='collection_excluded'
    assert attachment['next_action']=='check_collection_policy'
    result=s.request_file_download(key,fid)
    assert result['request_status']=='blocked' and result['next_action']=='check_collection_policy'
