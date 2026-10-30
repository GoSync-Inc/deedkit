"""Explicit original requests are bounded, scoped scheduling, never provider actions."""
from datetime import datetime, timedelta, timezone

import pytest

from messaging_service.store import APIError
from test_archive import archive as archive, event


def pending(ar, s, *, mid='201', age=90):
    at=(datetime.now(timezone.utc)-timedelta(days=age)).isoformat()
    message=ar.ingest(event(provider_id=mid,sent_at=at,updated_at=at,
        media=[{'provider_id':'media-'+mid,'filename':'Full catalogue.pdf',
                'mime':'application/pdf','bytes':1234}]))['message']
    with s.pool.connection() as c:
        return c.execute('SELECT id FROM media_jobs WHERE message=%s',(message,)).fetchone()['id']


def test_explicit_request_is_durable_coalesced_and_does_not_consume_attempt(archive):
    ar,s,key,_=archive; fid=pending(ar,s)
    result=s.request_file_download(key,fid)
    assert result['request_status']=='queued'
    assert result['attachment_id']==fid and result['filename']=='Full catalogue.pdf'
    assert result['changes_provider_state'] is False
    assert result['poll_operation']=='get_attachment'
    assert result['requested_until']
    again=s.request_file_download(key,fid)
    assert again['request_status']=='coalesced'
    assert again['requested_until']==result['requested_until']
    with s.pool.connection() as c:
        row=c.execute('SELECT attempts,requested_until FROM media_jobs WHERE id=%s',(fid,)).fetchone()
        assert row['attempts']==0


def test_passive_attachment_does_not_schedule_or_conceal_filename(archive):
    ar,s,key,_=archive; fid=pending(ar,s)
    result=s.get_attachment(key,fid)
    assert result['filename']=='Full catalogue.pdf'
    assert result['mime']=='application/pdf' and result['bytes']==1234
    assert result['message_id'] and result['source']['provider_message_id']=='201'
    assert result['next_action']=='request_file_download'
    assert 'download_url' not in result
    with s.pool.connection() as c:
        assert c.execute('SELECT requested_until FROM media_jobs WHERE id=%s',(fid,)).fetchone()['requested_until'] is None


def test_request_respects_permission_scope_and_collection_policy(archive):
    ar,s,key,_=archive; fid=pending(ar,s)
    no_download=s.issue_key('no-download',['archive-test'],permissions=['read'])
    with pytest.raises(APIError,match='forbidden'):s.request_file_download(no_download,fid)
    s.set_access('archive-reader',chats=[])
    with pytest.raises(APIError,match='not_found'):s.request_file_download(key,fid)
    s.set_access('archive-reader',chats=None)
    with s.pool.connection() as c:c.execute("UPDATE account_policies SET enabled=false WHERE account='archive-test'")
    result=s.request_file_download(key,fid)
    assert result['request_status']=='blocked' and result['reason']=='collection_disabled'
    with s.pool.connection() as c:
        assert c.execute('SELECT requested_until FROM media_jobs WHERE id=%s',(fid,)).fetchone()['requested_until'] is None


def test_request_preserves_provider_backoff_and_terminal_failures(archive):
    ar,s,key,_=archive; fid=pending(ar,s)
    with s.pool.connection() as c:
        due=c.execute("UPDATE media_jobs SET next_due=now()+interval '2 hours',error_code='rate_limited' WHERE id=%s RETURNING next_due",(fid,)).fetchone()['next_due']
    result=s.request_file_download(key,fid)
    assert result['request_status']=='deferred' and result['requested_until'] is None
    assert result['retry_after_seconds']>3600
    with s.pool.connection() as c:
        assert c.execute('SELECT next_due FROM media_jobs WHERE id=%s',(fid,)).fetchone()['next_due']==due
        assert c.execute('SELECT requested_until FROM media_jobs WHERE id=%s',(fid,)).fetchone()['requested_until'] is None
        c.execute("UPDATE media_jobs SET attempts=5,state='failed',error_code='retry_exhausted',requested_until=NULL WHERE id=%s",(fid,))
    result=s.request_file_download(key,fid)
    assert result['request_status']=='failed' and result['reason']=='retry_exhausted'
    with s.pool.connection() as c:
        assert c.execute('SELECT requested_until FROM media_jobs WHERE id=%s',(fid,)).fetchone()['requested_until'] is None


def test_request_limits_and_expiry(archive,monkeypatch):
    from messaging_service import media_requests
    ar,s,key,_=archive
    monkeypatch.setattr(media_requests,'PER_KEY_LIMIT',1)
    first=pending(ar,s); second=pending(ar,s,mid='202')
    s.request_file_download(key,first)
    with pytest.raises(APIError,match='media_request_limit'):s.request_file_download(key,second)
    with s.pool.connection() as c:c.execute("UPDATE media_jobs SET requested_until=now()-interval '1 second' WHERE id=%s",(first,))
    assert s.request_file_download(key,second)['request_status']=='queued'


def test_concurrent_request_admission_enforces_key_limit(archive,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from messaging_service import media_requests
    ar,s,key,_=archive
    monkeypatch.setattr(media_requests,'PER_KEY_LIMIT',1)
    ids=[pending(ar,s,mid=mid) for mid in ('201','202')]
    barrier=Barrier(2)
    def submit(fid):
        barrier.wait(timeout=5)
        try:return s.request_file_download(key,fid)['request_status']
        except APIError as error:return str(error)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes=list(executor.map(submit,ids))
    assert sorted(outcomes)==['media_request_limit','queued']
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM media_jobs WHERE requested_until>now()').fetchone()['n']==1


def test_gateway_role_can_schedule_but_cannot_change_source_or_retry_state(archive):
    from contextlib import contextmanager
    from copy import copy
    from pathlib import Path
    from uuid import uuid4
    import psycopg
    from psycopg import sql
    ar,s,key,_=archive; fid=pending(ar,s)
    role='media_gateway_'+uuid4().hex
    ddl=Path(__file__).resolve().parents[1].joinpath('services/messaging/messaging_service/gateway_role.sql').read_text().replace('messaging_gateway',role)
    class RestrictedPool:
        @contextmanager
        def connection(self):
            with s.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                yield c
    restricted=copy(s);restricted.pool=RestrictedPool()
    with s.pool.connection() as c:c.execute(ddl)
    try:
        assert restricted.request_file_download(key,fid)['request_status']=='queued'
        assert restricted.get_attachment(key,fid)['download_request']['state']=='requested'
        for statement in ("UPDATE media_jobs SET attempts=0", "UPDATE media_jobs SET state='ready'",
                "UPDATE media_jobs SET descriptor='{}'", 'DELETE FROM media_jobs'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with restricted.pool.connection() as c:c.execute(statement)
    finally:
        with s.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def test_missing_original_never_issues_ready_link_or_resets_integrity(archive):
    from messaging_service.blobs import BlobWriter
    ar,s,key,_=archive; fid=pending(ar,s)
    job=ar.claim_media('fixture')
    writer=BlobWriter(s.files,reserve_bytes=0)
    blob=writer.write([b'x'*1234])
    ar.finish_media(job,blob,writer=writer)
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_text SET state='ready',text_content='Synthetic extracted catalogue' WHERE attachment=%s",(fid,))
        c.execute("UPDATE attachment_enrichment SET state='ready',text_content='Synthetic description' WHERE attachment=%s",(fid,))
    (s.files/blob.digest).unlink()
    result=s.get_attachment(key,fid)
    assert result['download_status']=='unavailable'
    assert result['reason']=='original_missing' and 'download_url' not in result
    assert result['extraction_status']=='ready' and result['enrichment_status']=='ready'
    requested=s.request_file_download(key,fid)
    assert requested['request_status']=='unavailable'
    assert requested['extraction_status']=='ready' and requested['enrichment_status']=='ready'
    with s.pool.connection() as c:
        assert c.execute('SELECT state FROM media_jobs WHERE id=%s',(fid,)).fetchone()['state']=='ready'
