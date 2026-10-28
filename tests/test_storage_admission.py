"""Disk pauses preserve capture, attempts and cross-process capacity ownership."""
import asyncio
import errno
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
import time
from types import SimpleNamespace

import pytest

from messaging_service.blobs import BlobWriter
from messaging_service.archive import Archive
from messaging_service.storage_admission import (
    MediaReservation, STREAM_WORKSPACE_BYTES, has_capacity, payload_bound, volume_key,
)
from test_archive import archive, event, media_event
from test_collectors import worker, message


def free(monkeypatch, amount):
    monkeypatch.setattr('shutil.disk_usage',lambda _:SimpleNamespace(free=amount))


def reservation_count(store):
    with store.pool.connection() as c:
        return c.execute('SELECT count(*) AS n FROM media_reservations').fetchone()['n']


def test_capacity_includes_full_reservations_and_floor():
    assert has_capacity(100,20,30,50)
    assert not has_capacity(99,20,30,50)


def test_unknown_payload_reserves_entire_bounded_quota():
    assert payload_bound(None,123)==123
    assert payload_bound(0,123)==0
    for value in (-1,124,True,'12'):
        with pytest.raises(ValueError,match='media_size_limit'):payload_bound(value,123)


def test_alias_paths_share_filesystem_key(tmp_path):
    a=tmp_path/'a';b=tmp_path/'b';a.mkdir();b.mkdir()
    assert volume_key(a)==volume_key(b)


@pytest.mark.parametrize('platform',['telegram','whatsapp'])
@pytest.mark.parametrize('capture_gib,media_gib,expected_gib',[
    (1,None,10),(20,None,20),(1,2,2),(20,10,None),
])
def test_media_config_default_preserves_larger_capture_floor(tmp_path,platform,
        capture_gib,media_gib,expected_gib):
    cfg={'enabled':True,'account':'synthetic','permit_file':str(tmp_path/'permit'),
        'disk_reserve_bytes':capture_gib*1024**3}
    permit={'account':'synthetic','prior_clients_stopped':True}
    if platform=='telegram':
        from messaging_service.telegram_collector import configuration
        cfg.update(credentials_file=str(tmp_path/'credentials'),session_file=str(tmp_path/'session.session'),
            spool_directory=str(tmp_path/'spool'))
        credentials=tmp_path/'credentials'
        credentials.write_text(json.dumps({'api_id':1,'api_hash':'a'*32,'expected_user_id':1}))
        credentials.chmod(0o600);permit['expected_user_id']=1
    else:
        from messaging_service.whatsapp_collector import configuration
        cfg.update(expected_wid='123456789@c.us',session_directory=str(tmp_path/'session'),
            events_directory=str(tmp_path/'events'),pages_directory=str(tmp_path/'pages'),
            chromium_executable='/usr/bin/chromium',node_executable='/usr/bin/node')
        permit['expected_wid']=cfg['expected_wid']
    if media_gib is not None:cfg['media_disk_reserve_bytes']=media_gib*1024**3
    path=tmp_path/'configuration';path.write_text(json.dumps(cfg));path.chmod(0o600)
    (tmp_path/'permit').write_text(json.dumps(permit));(tmp_path/'permit').chmod(0o600)
    if expected_gib is None:
        with pytest.raises(ValueError,match='invalid_media_disk_reserve_bytes'):configuration(path)
    else:
        result=configuration(path)
        parsed=result[0] if platform=='telegram' else result
        assert parsed['media_disk_reserve_bytes']==expected_gib*1024**3
        assert parsed['disk_reserve_bytes']==capture_gib*1024**3


def collector_configuration(tmp_path,platform,**extra):
    cfg={'enabled':True,'account':'synthetic','permit_file':str(tmp_path/'permit'),'disk_reserve_bytes':1024**3,**extra}
    permit={'account':'synthetic','prior_clients_stopped':True}
    if platform=='telegram':
        from messaging_service.telegram_collector import configuration
        cfg.update(credentials_file=str(tmp_path/'credentials'),session_file=str(tmp_path/'session.session'),
            spool_directory=str(tmp_path/'spool'))
        credentials=tmp_path/'credentials'
        credentials.write_text(json.dumps({'api_id':1,'api_hash':'a'*32,'expected_user_id':1}))
        credentials.chmod(0o600);permit['expected_user_id']=1
    else:
        from messaging_service.whatsapp_collector import configuration
        cfg.update(expected_wid='123456789@c.us',session_directory=str(tmp_path/'session'),
            events_directory=str(tmp_path/'events'),pages_directory=str(tmp_path/'pages'),
            chromium_executable='/usr/bin/chromium',node_executable='/usr/bin/node')
        permit['expected_wid']=cfg['expected_wid']
    path=tmp_path/'configuration';path.write_text(json.dumps(cfg));path.chmod(0o600)
    (tmp_path/'permit').write_text(json.dumps(permit));(tmp_path/'permit').chmod(0o600)
    result=configuration(path)
    return result[0] if platform=='telegram' else result


# spec: ANYM-65
@pytest.mark.parametrize('platform',['telegram','whatsapp'])
def test_media_disk_reserve_is_capped_at_100_gib(tmp_path,platform):
    parsed=collector_configuration(tmp_path,platform,media_disk_reserve_bytes=100*1024**3)
    assert parsed['media_disk_reserve_bytes']==100*1024**3
    with pytest.raises(ValueError,match='invalid_media_disk_reserve_bytes'):
        collector_configuration(tmp_path,platform,media_disk_reserve_bytes=100*1024**3+1)


def test_expired_streaming_reservation_prevents_any_write(tmp_path):
    writer=BlobWriter(tmp_path/'blobs',reserve_bytes=0)
    guard=MediaReservation(8,datetime.now(timezone.utc)-timedelta(seconds=1))
    async def chunks():
        pytest.fail('Provider must not be opened for an expired local lease')
        yield b'data'
    with pytest.raises(RuntimeError,match='media_lease_lost'):
        asyncio.run(writer.write_async(chunks(),reservation=guard))
    assert list(writer.root.iterdir())==[]


def test_unknown_size_stream_cannot_exceed_reserved_bound(tmp_path):
    writer=BlobWriter(tmp_path/'blobs',max_bytes=8,reserve_bytes=0)
    guard=MediaReservation(8,datetime.now(timezone.utc)+timedelta(minutes=1))
    async def chunks():yield b'123456789'
    with pytest.raises(ValueError,match='media_size_limit'):
        asyncio.run(writer.write_async(chunks(),reservation=guard))
    assert list(writer.root.iterdir())==[]


def test_admission_pause_uses_no_attempt_and_exposes_reason(archive,monkeypatch):
    ar,s,key,_=archive;mid=ar.ingest(media_event())['message']
    writer=BlobWriter(s.files,reserve_bytes=20)
    free(monkeypatch,STREAM_WORKSPACE_BYTES+4+19)
    assert ar.claim_media('worker',writer=writer) is None
    with s.pool.connection() as c:
        job=c.execute('SELECT state,attempts,error_code FROM media_jobs WHERE message=%s',(mid,)).fetchone()
    assert job=={'state':'queued','attempts':0,'error_code':'paused_storage'}
    assert reservation_count(s)==0
    result=s.get_attachment(key,s.get_context(key,mid)['pending_attachments'][0]['id'])
    assert result['error_code']=='paused_storage'


def test_atomic_parallel_admission_prevents_overcommit(archive,monkeypatch):
    ar,s,_,_=archive
    ar.ingest(media_event());ar.ingest(media_event(provider_id='18'))
    writer=BlobWriter(s.files,reserve_bytes=0)
    free(monkeypatch,STREAM_WORKSPACE_BYTES+4)
    barrier=Barrier(2)
    def claim(owner):
        barrier.wait();return ar.claim_media(owner,writer=writer)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs=list(pool.map(claim,['one','two']))
    assert sum(job is not None for job in jobs)==1
    assert reservation_count(s)==1
    with s.pool.connection() as c:
        assert c.execute('SELECT sum(attempts) AS n FROM media_jobs').fetchone()['n']==1


def test_unknown_size_admission_cannot_assume_zero_bytes(archive,monkeypatch):
    ar,s,_,_=archive
    e=media_event();e['media'][0]['bytes']=None;ar.ingest(e)
    writer=BlobWriter(s.files,max_bytes=8,reserve_bytes=0)
    free(monkeypatch,STREAM_WORKSPACE_BYTES+7)
    assert ar.claim_media('worker',writer=writer) is None
    with s.pool.connection() as c:c.execute('UPDATE media_jobs SET next_due=now()')
    free(monkeypatch,STREAM_WORKSPACE_BYTES+8)
    job=ar.claim_media('worker',writer=writer)
    assert job['_storage_reservation'].payload_bytes==8


def test_oversize_media_fails_before_attempt_or_reservation(archive):
    ar,s,_,_=archive;ar.ingest(media_event())
    writer=BlobWriter(s.files,max_bytes=3,reserve_bytes=0)
    assert ar.claim_media('worker',writer=writer) is None
    assert reservation_count(s)==0
    with s.pool.connection() as c:
        assert c.execute('SELECT state,error_code,attempts FROM media_jobs').fetchone()=={
            'state':'failed','error_code':'size_limit','attempts':0}


def test_finish_and_failure_release_capacity_idempotently(archive):
    ar,s,_,_=archive;ar.ingest(media_event())
    writer=BlobWriter(s.files,reserve_bytes=0)
    job=ar.claim_media('worker',writer=writer);assert reservation_count(s)==1
    ar.fail_media(job,'network_error');assert reservation_count(s)==0
    ar.release_media_reservation(job);assert reservation_count(s)==0
    with s.pool.connection() as c:c.execute('UPDATE media_jobs SET next_due=now()')
    job=ar.claim_media('worker',writer=writer)
    blob=writer.write([b'data']);ar.finish_media(job,blob,writer)
    assert reservation_count(s)==0
    ar.release_media_reservation(job);assert reservation_count(s)==0


def test_crash_expiry_replaces_capacity_without_old_worker_release(archive):
    ar,s,_,_=archive;ar.ingest(media_event())
    writer=BlobWriter(s.files,reserve_bytes=0)
    old=ar.claim_media('old',writer=writer)
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET lease_until=now()-interval '1 second'")
    new=ar.claim_media('new',writer=writer)
    assert new['lease_epoch']>old['lease_epoch'] and reservation_count(s)==1
    ar.release_media_reservation(old);assert reservation_count(s)==1
    ar.release_media_reservation(new);assert reservation_count(s)==0


def test_renewal_keeps_db_and_streaming_reservations_alive(archive):
    ar,s,_,_=archive;ar.ingest(media_event())
    writer=BlobWriter(s.files,reserve_bytes=0)
    job=ar.claim_media('worker',writer=writer,ttl=10)
    ar.renew_media(job,ttl=120)
    with s.pool.connection() as c:
        reserved=c.execute('SELECT expires_at FROM media_reservations').fetchone()['expires_at']
        leased=c.execute('SELECT lease_until FROM media_jobs').fetchone()['lease_until']
    assert reserved==leased==job['_storage_reservation'].expires_at


def test_renewal_and_reaper_cannot_split_capacity_ownership(archive,monkeypatch):
    ar,s,_,_=archive
    ar.ingest(media_event());ar.ingest(media_event(provider_id='18'))
    writer=BlobWriter(s.files,reserve_bytes=0)
    free(monkeypatch,STREAM_WORKSPACE_BYTES+4)
    job=ar.claim_media('one',writer=writer)
    with s.pool.connection() as c:
        expiry=c.execute("SELECT clock_timestamp()+interval '1 second' AS at").fetchone()['at']
        c.execute('UPDATE media_jobs SET lease_until=%s WHERE id=%s',(expiry,job['id']))
        c.execute('UPDATE media_reservations SET expires_at=%s WHERE job=%s',(expiry,job['id']))
    renewing=Event();continue_renewal=Event();claiming=Event();claim_acquired_lock=Event()
    class WrappedConnection:
        def __init__(self,c,role):self.c,self.role=c,role
        def execute(self,query,*args):
            if self.role=='renew' and query.startswith('UPDATE media_reservations'):
                renewing.set();assert continue_renewal.wait(5)
            # The earlier lifecycle lock is independent of the capacity lock
            # whose renewal/reaper exclusion this test observes.
            if (self.role=='claim' and 'pg_advisory_xact_lock' in query
                    and 'media-storage-admission' in query):
                claiming.set();result=self.c.execute(query,*args);claim_acquired_lock.set();return result
            return self.c.execute(query,*args)
    class WrappedPool:
        def __init__(self,role):self.role=role
        @contextmanager
        def connection(self):
            with s.pool.connection() as c:yield WrappedConnection(c,self.role)
    renewal=Archive(SimpleNamespace(pool=WrappedPool('renew')))
    claiming_archive=Archive(SimpleNamespace(pool=WrappedPool('claim')))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(renewal.renew_media,job)
        assert renewing.wait(3)
        second=pool.submit(claiming_archive.claim_media,'two',writer=writer)
        try:
            assert claiming.wait(3)
            # Allow the old DB-visible deadline to pass while renewal is
            # uncommitted. Reaping/admission must still wait for the same lock.
            time.sleep(max(0,(expiry-datetime.now(timezone.utc)).total_seconds())+.05)
            assert not claim_acquired_lock.is_set()
        finally:continue_renewal.set()
        first.result(timeout=5)
        assert second.result(timeout=5) is None
    assert reservation_count(s)==1
    with s.pool.connection() as c:
        row=c.execute('''SELECT r.expires_at,j.lease_until FROM media_reservations r
            JOIN media_jobs j ON j.id=r.job WHERE r.job=%s''',(job['id'],)).fetchone()
    assert row['expires_at']==row['lease_until']==job['_storage_reservation'].expires_at


def test_renewal_fails_closed_if_its_reservation_disappeared(archive):
    ar,s,_,_=archive;ar.ingest(media_event())
    job=ar.claim_media('one',writer=BlobWriter(s.files,reserve_bytes=0))
    with s.pool.connection() as c:c.execute('DELETE FROM media_reservations WHERE job=%s',(job['id'],))
    with pytest.raises(RuntimeError,match='media_lease_lost'):ar.renew_media(job)


@pytest.mark.parametrize('change',['delete','supersede','exclude','disable'])
def test_current_source_fences_prevent_stale_claims(archive,change):
    ar,s,_,_=archive;ar.ingest(media_event())
    if change=='delete':ar.ingest(event(deleted=True,updated_at='2026-10-01T12:02:00Z'))
    elif change=='supersede':ar.ingest(event(updated_at='2026-10-01T12:02:00Z'))
    else:
        with s.pool.connection() as c:
            c.execute('UPDATE account_policies SET '+("exclude_chats=ARRAY['100']" if change=='exclude'
                else 'enabled=false')+" WHERE account='archive-test'")
    assert ar.claim_media('worker',writer=BlobWriter(s.files,reserve_bytes=0)) is None
    assert reservation_count(s)==0


def test_paused_originals_do_not_call_provider_or_stop_capture(worker,monkeypatch):
    w,s,key=worker
    asyncio.run(w.capture(SimpleNamespace(message=message(9,media=True))));w.spool.drain(w.state.apply)
    free(monkeypatch,STREAM_WORKSPACE_BYTES)
    async def must_not_fetch(*_):
        pytest.fail('Disk admission must run before provider download')
        yield b'data'
    w.provider.chunks=must_not_fetch
    assert not asyncio.run(w.media_once())
    asyncio.run(w.capture(SimpleNamespace(message=message(10))));w.spool.drain(w.state.apply)
    assert any(item['body']=='message 10' for item in s.search(key)['items'])
    with s.pool.connection() as c:
        assert c.execute('SELECT attempts,error_code FROM media_jobs WHERE account=%s',(w.account,)).fetchone()=={
            'attempts':0,'error_code':'paused_storage'}
    state=s.get_sync_status(key)['sources'][0]['operations']['media']
    assert state['state']=='paused_storage' and state['storage_paused_jobs']==1


def test_disk_pressure_during_stream_rolls_back_attempt_and_cleans_temp(worker,monkeypatch):
    w,s,_=worker
    asyncio.run(w.capture(SimpleNamespace(message=message(9,media=True))));w.spool.drain(w.state.apply)
    free(monkeypatch,STREAM_WORKSPACE_BYTES+4)
    async def chunks(*_):
        free(monkeypatch,0)
        yield b'data'
    w.provider.chunks=chunks
    assert asyncio.run(w.media_once())
    assert reservation_count(s)==0 and not list(w.writer.root.glob('.pending-*'))
    with s.pool.connection() as c:
        assert c.execute('SELECT state,attempts,error_code FROM media_jobs WHERE account=%s',(w.account,)).fetchone()=={
            'state':'queued','attempts':0,'error_code':'paused_storage'}


@pytest.mark.parametrize('error_number',[errno.ENOSPC,errno.EDQUOT])
def test_filesystem_capacity_errors_are_local_pauses(worker,error_number):
    w,s,_=worker
    asyncio.run(w.capture(SimpleNamespace(message=message(9,media=True))));w.spool.drain(w.state.apply)
    async def chunks(*_):
        raise OSError(error_number,'synthetic capacity error')
        yield b'data'
    w.provider.chunks=chunks
    assert asyncio.run(w.media_once())
    assert reservation_count(s)==0
    with s.pool.connection() as c:
        assert c.execute('SELECT state,attempts,error_code FROM media_jobs WHERE account=%s',(w.account,)).fetchone()=={
            'state':'queued','attempts':0,'error_code':'paused_storage'}


@pytest.mark.parametrize('failure,expected', [
    (PermissionError(errno.EACCES, 'synthetic permission denied', '/synthetic/originals/.pending-x'),
     ('queued', 0, 'storage_unavailable')),
    (OSError(errno.EROFS, 'synthetic read-only filesystem'), ('queued', 0, 'storage_unavailable')),
    (RuntimeError('media_storage_unavailable'), ('queued', 0, 'storage_unavailable')),
    (ConnectionResetError(errno.ECONNRESET, 'synthetic reset'), ('queued', 1, 'network_error')),
])
def test_local_storage_failures_are_not_network_errors_and_keep_attempts(worker, failure, expected):
    w,s,_=worker
    w.retry_connect=True  # Native provider codes pass through as reported.
    asyncio.run(w.capture(SimpleNamespace(message=message(9,media=True))));w.spool.drain(w.state.apply)
    async def chunks(*_):
        raise failure
        yield b'data'
    w.provider.chunks=chunks
    assert asyncio.run(w.media_once())
    with s.pool.connection() as c:
        row=c.execute('SELECT state,attempts,error_code FROM media_jobs WHERE account=%s',(w.account,)).fetchone()
    assert (row['state'],row['attempts'],row['error_code'])==expected
