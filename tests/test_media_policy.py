"""Bounded original prefetch is independent of message-history coverage."""
import json
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from messaging_service.telegram_collector import configuration as telegram_configuration
from messaging_service.media_retry import MEDIA_MAX_ATTEMPTS
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


def chat_of(s,mid):
    with s.pool.connection() as c:
        return c.execute('SELECT chat FROM messages WHERE id=%s',(mid,)).fetchone()['chat']


def set_kind(s,mid,chat_type=None,peer_kind=None):
    with s.pool.connection() as c:
        row=c.execute('''UPDATE chats SET chat_type=%s FROM messages m WHERE m.id=%s AND chats.id=m.chat
            RETURNING chats.account,chats.provider_chat''',(chat_type,mid)).fetchone()
        if peer_kind:
            c.execute('''INSERT INTO collector_chats(account,provider_chat,peer_kind) VALUES(%s,%s,%s)
                ON CONFLICT(account,provider_chat) DO UPDATE SET peer_kind=excluded.peer_kind''',
                (row['account'],row['provider_chat'],peer_kind))


def on_request(s):
    from messaging_service.media_policy import ON_REQUEST_CHATS
    with s.pool.connection() as c:
        return {r['chat']:r['reason'] for r in c.execute(ON_REQUEST_CHATS).fetchall()}


def subscribe(s,chat,*,state='active',expired_hours=None,closed_hours=None,dynamic=False):
    token=s.issue_key('media-watcher',['archive-test'],permissions=['read','subscription-write'],
        key_permissions=['read','subscription-write'])
    sub=s.create_subscription(token,[chat])
    with s.pool.connection() as c:
        c.execute('''UPDATE subscriptions SET state=%s,
            expires_at=CASE WHEN %s::integer IS NULL THEN expires_at ELSE clock_timestamp()-%s*interval '1 hour' END,
            closed_at=CASE WHEN %s::integer IS NULL THEN closed_at ELSE clock_timestamp()-%s*interval '1 hour' END,
            selector=CASE WHEN %s THEN selector||'{"sources":"all"}'::jsonb ELSE selector END WHERE id=%s''',
            (state,expired_hours,expired_hours,closed_hours,closed_hours,dynamic,sub['subscription_id']))
        # A dynamic definition (all visible chats) lists no chat (schema 60).
        if dynamic:c.execute('DELETE FROM subscription_sources WHERE subscription=%s',(sub['subscription_id'],))


# spec: ANYM-805
@pytest.mark.parametrize('chat_type,peer_kind,expected',[
    ('private',None,None),('group',None,'group_chat_on_request'),('channel',None,'channel_on_request'),
    (None,'chat','group_chat_on_request'),(None,'channel','channel_on_request'),(None,'user',None),
    (None,None,None),('private','channel',None),('group','user','group_chat_on_request')])
def test_chat_kind_decides_whether_media_waits_for_a_request(archive,chat_type,peer_kind,expected):
    ar,s,_,_=archive;mid=original(ar)
    set_kind(s,mid,chat_type,peer_kind)
    # Unknown kind (no type, no collector row) counts as personal: kept and recognized.
    assert on_request(s).get(chat_of(s,mid))==expected


# spec: ANYM-805
@pytest.mark.parametrize('subscription,waits',[
    ({},False),({'expired_hours':2},False),({'state':'closed','closed_hours':2},False),
    ({'state':'expired','closed_hours':2},False),({'state':'access_changed','closed_hours':2},False),
    ({'expired_hours':25},True),({'state':'closed','closed_hours':25},True),({'state':'paused'},True),
    ({'dynamic':True},True)])
def test_a_group_named_by_an_active_subscription_keeps_automatic_media(archive,subscription,waits):
    ar,s,_,_=archive;mid=original(ar)
    set_kind(s,mid,'group')
    assert chat_of(s,mid) in on_request(s)
    subscribe(s,chat_of(s,mid),**subscription)
    assert (chat_of(s,mid) in on_request(s))==waits


# spec: ANYM-805
@pytest.mark.parametrize('kind',['group','channel'])
@pytest.mark.parametrize('days',[7,None])
def test_group_and_channel_media_is_downloaded_only_on_request(archive,kind,days):
    ar,s,_,_=archive;mid=original(ar,age=3600)
    set_kind(s,mid,kind)
    assert ar.claim_media('worker',media_prefetch_days=days) is None
    with s.pool.connection() as c:
        assert c.execute('SELECT state,attempts,error_code FROM media_jobs WHERE message=%s',(mid,)).fetchone()=={
            'state':'queued','attempts':0,'error_code':None}
    request(s,mid)
    assert ar.claim_media('worker',media_prefetch_days=days)['message']==mid


# spec: ANYM-805
def test_personal_and_subscribed_group_media_keep_the_prefetch_window(archive):
    ar,s,_,_=archive
    personal=original(ar,mid='1',age=3600)
    set_kind(s,personal,'private')
    assert ar.claim_media('worker',media_prefetch_days=7)['message']==personal
    group=original(ar,mid='2',age=3600)
    set_kind(s,group,'group')
    assert ar.claim_media('worker',media_prefetch_days=7) is None
    subscribe(s,chat_of(s,group))
    assert ar.claim_media('worker',media_prefetch_days=7)['message']==group


# spec: ANYM-805
def test_a_request_during_a_running_download_records_the_recognition_request(archive):
    ar,s,key,_=archive;mid=original(ar,age=3600)
    set_kind(s,mid,'group')
    subscribe(s,chat_of(s,mid))
    job=ar.claim_media('worker',media_prefetch_days=7);assert job['message']==mid
    with s.pool.connection() as c:
        # Paused while the file downloads: the chat is on request again.
        c.execute("UPDATE subscriptions SET state='paused'")
    assert s.request_file_download(key,job['id'])['request_status']=='processing'
    with s.pool.connection() as c:
        recorded=c.execute('SELECT request_key_digest FROM media_jobs WHERE id=%s',(job['id'],)).fetchone()['request_key_digest']
    assert recorded is not None
    # A repeated request changes nothing.
    assert s.request_file_download(key,job['id'])['request_status']=='processing'
    with s.pool.connection() as c:
        assert c.execute('SELECT request_key_digest FROM media_jobs WHERE id=%s',(job['id'],)).fetchone()['request_key_digest']==recorded


# spec: ANYM-805
def test_backfill_and_live_tier_skip_on_request_chats(archive):
    ar,s,_,_=archive
    background,live=writers(s)
    voice=original(ar,mid='1',age=30*86400)
    set_kind(s,voice,'group')
    assert ar._backfill_candidates('archive-test',None,7,{'voice_mimes':['audio/ogg'],'voice_max':32*1024**2,'small_max':1024**2})==[]
    fresh=original(ar,mid='2',age=60)
    assert ar.claim_media('worker',media_prefetch_days=7,backfill=True,writer=background,live_writer=live) is None
    request(s,fresh)
    assert ar.claim_media('worker',media_prefetch_days=7,backfill=True,writer=background,live_writer=live)['message']==fresh


@pytest.mark.parametrize('change',['deleted','excluded','disabled','attempts','cooldown','history'])
def test_demand_cannot_bypass_collection_safety(archive,change):
    ar,s,_,_=archive;mid=original(ar,age=30*86400);request(s,mid)
    with s.pool.connection() as c:
        if change=='deleted':c.execute('UPDATE messages SET deleted=true WHERE id=%s',(mid,))
        elif change=='excluded':c.execute("UPDATE account_policies SET exclude_chats=ARRAY['100'] WHERE account='archive-test'")
        elif change=='disabled':c.execute("UPDATE account_policies SET enabled=false WHERE account='archive-test'")
        elif change=='attempts':c.execute('UPDATE media_jobs SET attempts=%s WHERE message=%s',(MEDIA_MAX_ATTEMPTS,mid))
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
