"""Bounded original prefetch is independent of message-history coverage."""
import json
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from messaging_service.telegram_collector import configuration as telegram_configuration
from messaging_service.whatsapp_collector import configuration as whatsapp_configuration
from test_live_media import config_path, original, writers, capacity
from test_archive import archive as archive
from test_collectors import worker as worker


def whatsapp_path(tmp_path, **overrides):
    cfg={'enabled':True,'account':'synthetic','expected_wid':'123456789@c.us',
        'provider':'whatsmeow','native_executable':'/usr/local/bin/anymsg-whatsmeow',
        'permit_file':str(tmp_path/'permit'),'session_directory':str(tmp_path/'session'),
        'events_directory':str(tmp_path/'events'),'pages_directory':str(tmp_path/'pages'),**overrides}
    for name,value in [('configuration',cfg),('permit',{'account':'synthetic',
            'expected_wid':cfg['expected_wid'],'prior_clients_stopped':True})]:
        path=tmp_path/name;path.write_text(json.dumps(value));path.chmod(0o600)
    return tmp_path/'configuration'


@pytest.mark.parametrize('provider',['telegram','whatsapp'])
@pytest.mark.parametrize('days',[7,None,0,90])
def test_config_common_prefetch_policy(tmp_path,provider,days):
    options={} if days==7 else {'media_prefetch_days':days}
    if provider=='telegram':cfg,_=telegram_configuration(config_path(tmp_path,**options))
    else:cfg=whatsapp_configuration(whatsapp_path(tmp_path,**options))
    assert cfg['media_prefetch_days']==days
    assert cfg['media_live_disk_reserve_bytes'] is None
    assert cfg['media_live_max_bytes']==32*1024**2
    assert cfg['media_live_window_seconds']==86400


@pytest.mark.parametrize('provider',['telegram','whatsapp'])
@pytest.mark.parametrize('days',[-1,True,1.5,'7',36501])
def test_config_rejects_invalid_prefetch(tmp_path,provider,days):
    with pytest.raises(ValueError,match='invalid_media_prefetch_days'):
        if provider=='telegram':telegram_configuration(config_path(tmp_path,media_prefetch_days=days))
        else:whatsapp_configuration(whatsapp_path(tmp_path,media_prefetch_days=days))


def test_whatsapp_recent_original_configuration(tmp_path):
    cfg=whatsapp_configuration(whatsapp_path(tmp_path,media_live_disk_reserve_bytes=3*1024**3,
        media_live_max_bytes=8*1024**2,media_live_window_seconds=7*86400))
    assert cfg['media_live_disk_reserve_bytes']==3*1024**3
    assert cfg['media_live_max_bytes']==8*1024**2
    assert cfg['media_live_window_seconds']==7*86400


@pytest.mark.parametrize('overrides',[
    {'media_live_disk_reserve_bytes':0},{'media_live_disk_reserve_bytes':True},
    {'media_live_disk_reserve_bytes':11*1024**3},
    {'media_live_disk_reserve_bytes':2*1024**3,'disk_reserve_bytes':3*1024**3},
    {'media_live_max_bytes':True},{'media_live_max_bytes':0},
    {'media_live_max_bytes':513*1024**2},{'media_live_window_seconds':True},
    {'media_live_window_seconds':0},{'media_live_window_seconds':8*86400}])
def test_whatsapp_rejects_unsafe_recent_original_limits(tmp_path,overrides):
    with pytest.raises(ValueError,match='invalid_media_live_'):
        whatsapp_configuration(whatsapp_path(tmp_path,**overrides))


def request(s,mid,*,expired=False):
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET requested_until=clock_timestamp()+%s*interval '1 hour' WHERE message=%s",
            (-1 if expired else 1,mid))


def test_prefetch_downloads_recent_original_and_leaves_history_queued(archive):
    ar,s,_,_=archive
    old=original(ar,mid='1',age=30*86400)
    recent=original(ar,mid='2',age=86400)
    assert ar.claim_media('worker',media_prefetch_days=7)['message']==recent
    assert ar.claim_media('worker',media_prefetch_days=7) is None
    with s.pool.connection() as c:
        assert c.execute('SELECT state,attempts,error_code FROM media_jobs WHERE message=%s',(old,)).fetchone()=={
            'state':'queued','attempts':0,'error_code':None}


def test_null_prefetch_retains_all_history_and_zero_requires_demand(archive):
    ar,s,_,_=archive
    old=original(ar,age=30*86400)
    assert ar.claim_media('worker',media_prefetch_days=0) is None
    assert ar.claim_media('worker',media_prefetch_days=None)['message']==old


def test_requested_history_precedes_recent_but_expired_request_does_not(archive):
    ar,s,_,_=archive
    old=original(ar,mid='1',age=30*86400);recent=original(ar,mid='2')
    expired=original(ar,mid='3',age=40*86400)
    request(s,old);request(s,expired,expired=True)
    assert ar.claim_media('worker',media_prefetch_days=7)['message']==old
    assert ar.claim_media('worker',media_prefetch_days=7)['message']==recent
    assert ar.claim_media('worker',media_prefetch_days=7) is None


@pytest.mark.parametrize('change',['deleted','excluded','disabled','attempts','cooldown','history'])
def test_demand_cannot_bypass_collection_safety(archive,change):
    ar,s,_,_=archive;mid=original(ar,age=30*86400);request(s,mid)
    with s.pool.connection() as c:
        if change=='deleted':c.execute('UPDATE messages SET deleted=true WHERE id=%s',(mid,))
        elif change=='excluded':c.execute("UPDATE account_policies SET exclude_chats=ARRAY['100'] WHERE account='archive-test'")
        elif change=='disabled':c.execute("UPDATE account_policies SET enabled=false WHERE account='archive-test'")
        elif change=='attempts':c.execute('UPDATE media_jobs SET attempts=5 WHERE message=%s',(mid,))
        elif change=='cooldown':c.execute("UPDATE media_jobs SET next_due=clock_timestamp()+interval '1 hour' WHERE message=%s",(mid,))
    since=datetime.now(timezone.utc)-timedelta(days=3) if change=='history' else None
    assert ar.claim_media('worker',media_prefetch_days=0,since=since) is None


def test_old_demand_retains_background_floor(archive,monkeypatch):
    ar,s,_,_=archive;mid=original(ar,age=30*86400);request(s,mid)
    background,live=writers(s);capacity(monkeypatch)
    assert ar.claim_media('worker',media_prefetch_days=0,writer=background,live_writer=live) is None
    with s.pool.connection() as c:
        assert c.execute('SELECT attempts,error_code FROM media_jobs WHERE message=%s',(mid,)).fetchone()=={
            'attempts':0,'error_code':'paused_storage'}


def test_worker_passes_rolling_prefetch_policy_without_changing_history(worker):
    w,s,_=worker
    w.media_prefetch_days=7
    at=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
    from test_archive import event
    mid=w.archive.ingest(event(account=w.account,provider_id='91',sent_at=at,updated_at=at,
        media=[{'provider_id':'media-91','filename':'old.ogg','mime':'audio/ogg','bytes':4}]))['message']
    assert asyncio.run(w.media_once()) is False
    # The message remains readable and the same collector can satisfy explicit demand.
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM messages WHERE id=%s',(mid,)).fetchone()['n']==1
    request(s,mid)
    assert asyncio.run(w.media_once()) is True
    with s.pool.connection() as c:
        assert c.execute('SELECT state FROM media_jobs WHERE message=%s',(mid,)).fetchone()['state']=='ready'


@pytest.mark.parametrize('days',[-1,True,1.5,'7',36501])
def test_archive_rejects_invalid_prefetch_before_claim(archive,days):
    ar,_,_,_=archive
    with pytest.raises(ValueError,match='invalid_media_prefetch_days'):
        ar.claim_media('worker',media_prefetch_days=days)
