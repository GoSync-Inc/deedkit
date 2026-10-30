"""Fresh small originals remain usable when background history lacks space."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from messaging_service.blobs import BlobWriter
from messaging_service.storage_admission import STREAM_WORKSPACE_BYTES
from messaging_service.telegram_collector import configuration
from test_archive import archive as archive, event
from test_collectors import worker as worker


def original(ar, *, mid='17', age=300, size=4):
    at=(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat()
    return ar.ingest(event(provider_id=mid,sent_at=at,updated_at=at,
        media=[{'provider_id':'media-'+mid,'filename':'voice.ogg','mime':'audio/ogg','bytes':size}]))['message']


def writers(store):
    return (BlobWriter(store.files,max_bytes=128,reserve_bytes=100),
            BlobWriter(store.files,max_bytes=8,reserve_bytes=20))


def capacity(monkeypatch, amount=STREAM_WORKSPACE_BYTES+4+20):
    monkeypatch.setattr('shutil.disk_usage',lambda _:SimpleNamespace(free=amount))


def test_recent_small_original_precedes_backfill_at_background_floor(archive,monkeypatch):
    ar,s,_,_=archive
    old=original(ar,mid='1',age=3*86400)
    recent=original(ar,mid='2')
    background,live=writers(s);capacity(monkeypatch)
    job=ar.claim_media('worker',writer=background,live_writer=live)
    assert job['message']==recent and job['_live_media'] is True
    assert job['_storage_reservation'].payload_bytes==4
    with s.pool.connection() as c:
        assert c.execute('SELECT attempts,error_code FROM media_jobs WHERE message=%s',(old,)).fetchone()=={
            'attempts':0,'error_code':None}


@pytest.mark.parametrize('age,size',[(2*86400,4),(300,9),(300,None),(-600,4)])
def test_history_unknown_and_large_originals_keep_background_floor(archive,monkeypatch,age,size):
    ar,s,_,_=archive;original(ar,age=age,size=size)
    background,live=writers(s);capacity(monkeypatch)
    assert ar.claim_media('worker',writer=background,live_writer=live) is None
    with s.pool.connection() as c:
        assert c.execute('SELECT state,attempts,error_code FROM media_jobs').fetchone()=={
            'state':'queued','attempts':0,'error_code':'paused_storage'}


def test_live_originals_still_reserve_shared_capacity(archive,monkeypatch):
    ar,s,_,_=archive;original(ar);original(ar,mid='18')
    background,live=writers(s);capacity(monkeypatch)
    assert ar.claim_media('one',writer=background,live_writer=live)
    assert ar.claim_media('two',writer=background,live_writer=live) is None
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM media_reservations').fetchone()['n']==1
        assert c.execute('SELECT sum(attempts) AS n FROM media_jobs').fetchone()['n']==1


def test_recent_priority_preserves_history_bound(archive,monkeypatch):
    ar,s,_,_=archive;original(ar)
    background,live=writers(s);capacity(monkeypatch)
    assert ar.claim_media('worker',writer=background,live_writer=live,
        since=datetime.now(timezone.utc)) is None


def test_large_original_downloads_when_background_capacity_returns(archive,monkeypatch):
    ar,s,_,_=archive;mid=original(ar,size=9)
    background,live=writers(s)
    capacity(monkeypatch,STREAM_WORKSPACE_BYTES+9+100)
    job=ar.claim_media('worker',writer=background,live_writer=live)
    assert job['message']==mid and job['_live_media'] is False
    assert job['_storage_reservation'].payload_bytes==9


@pytest.mark.parametrize('change',['deleted','excluded','disabled'])
def test_live_priority_cannot_bypass_collection_policy(archive,monkeypatch,change):
    ar,s,_,_=archive;mid=original(ar)
    with s.pool.connection() as c:
        if change=='deleted':c.execute('UPDATE messages SET deleted=true WHERE id=%s',(mid,))
        elif change=='excluded':c.execute("UPDATE account_policies SET exclude_chats=ARRAY['100'] WHERE account='archive-test'")
        else:c.execute("UPDATE account_policies SET enabled=false WHERE account='archive-test'")
    background,live=writers(s);capacity(monkeypatch)
    assert ar.claim_media('worker',writer=background,live_writer=live) is None
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM media_reservations').fetchone()['n']==0


def test_worker_streams_original_without_waiting_for_analysis(worker,monkeypatch):
    w,s,tmp=worker
    at=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
    mid=w.archive.ingest(event(account=w.account,provider_id='91',sent_at=at,updated_at=at,
        media=[{'provider_id':'media-91','filename':'voice.ogg','mime':'audio/ogg','bytes':4}]))['message']
    w.writer,w.live_writer=writers(s)
    w.live_window_seconds=86400
    capacity(monkeypatch)
    assert asyncio.run(w.media_once()) is True
    with s.pool.connection() as c:
        job=c.execute('SELECT state FROM media_jobs WHERE message=%s',(mid,)).fetchone()
        assert job['state']=='ready'
    key=s.issue_key('live-original-reader',[w.account])
    attachment=s.get_context(key,mid)['attachments'][0]
    result=s.get_attachment(key,attachment['id'])
    assert result['download_status']=='ready'
    assert result['enrichment_status']!='ready'


def config_path(tmp_path,**overrides):
    cfg={'enabled':True,'account':'synthetic','credentials_file':str(tmp_path/'credentials'),
        'session_file':str(tmp_path/'session.session'),'permit_file':str(tmp_path/'permit'),
        'spool_directory':str(tmp_path/'spool'),**overrides}
    for name,value in [('configuration',cfg),('credentials',{'api_id':1,'api_hash':'a'*32,'expected_user_id':1}),
        ('permit',{'account':'synthetic','expected_user_id':1,'prior_clients_stopped':True})]:
        path=tmp_path/name;path.write_text(json.dumps(value));path.chmod(0o600)
    return tmp_path/'configuration'


def test_recent_original_policy_requires_explicit_opt_in(tmp_path):
    cfg,_=configuration(config_path(tmp_path))
    assert cfg['media_live_disk_reserve_bytes'] is None
    assert cfg['media_live_max_bytes']==32*1024**2
    assert cfg['media_live_window_seconds']==86400


def test_recent_original_configuration(tmp_path):
    cfg,_=configuration(config_path(tmp_path,media_live_disk_reserve_bytes=3*1024**3,
        media_live_max_bytes=8*1024**2,media_live_window_seconds=3600))
    assert cfg['media_live_disk_reserve_bytes']==3*1024**3
    assert cfg['media_live_max_bytes']==8*1024**2
    assert cfg['media_live_window_seconds']==3600


@pytest.mark.parametrize('overrides',[
    {'media_live_disk_reserve_bytes':0}, {'media_live_disk_reserve_bytes':True},
    {'media_live_disk_reserve_bytes':11*1024**3},
    {'media_live_disk_reserve_bytes':2*1024**3,'disk_reserve_bytes':3*1024**3},
    {'media_live_max_bytes':True}, {'media_live_max_bytes':0},
    {'media_live_max_bytes':513*1024**2}, {'media_live_window_seconds':True},
    {'media_live_window_seconds':0}, {'media_live_window_seconds':8*86400},
])
def test_recent_original_configuration_rejects_unsafe_limits(tmp_path,overrides):
    with pytest.raises(ValueError,match='invalid_media_live_'):
        configuration(config_path(tmp_path,**overrides))


def test_live_writer_cannot_bypass_archive_volume(archive,tmp_path):
    ar,s,_,_=archive;original(ar)
    background,live=writers(s)
    live=BlobWriter(tmp_path/'elsewhere',max_bytes=8,reserve_bytes=20)
    with pytest.raises(ValueError,match='invalid_live_media_policy'):
        ar.claim_media('worker',writer=background,live_writer=live)
