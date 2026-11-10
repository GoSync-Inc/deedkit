"""Fresh small originals remain usable when background history lacks space."""
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from messaging_service.blobs import BlobWriter
from messaging_service.storage_admission import STREAM_WORKSPACE_BYTES
from messaging_service.telegram_collector import configuration
from test_archive import archive as archive, event
from test_collectors import idle_loop_wakes_on_own_media, worker as worker


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


PART=512*1024


def document(mid,size):
    from telethon import types
    now=datetime.now(timezone.utc)
    media=types.Document(id=5000+mid,access_hash=0,file_reference=b'reference',date=now,
        mime_type='application/octet-stream',size=size,dc_id=1,attributes=[])
    return types.Message(id=mid,peer_id=types.PeerUser(100),date=now,message='',
        media=types.MessageMediaDocument(document=media))


class Parts:
    """A fake iter_download: one part per provider request; the call after a short part makes none."""
    def __init__(self,parts,log):self.parts=list(parts);self.log=log
    def __aiter__(self):return self
    async def __anext__(self):
        self.log.append('next')
        if not self.parts:raise StopAsyncIteration
        return self.parts.pop(0)
    async def close(self):pass


class Files:
    """A fake Telegram client: messages by id and the parts each document downloads in."""
    def __init__(self,messages,parts):self.messages,self.parts,self.fetches,self.log=messages,parts,[],[]
    async def get_messages(self,chat,ids):
        self.fetches.append(ids)
        return [self.messages.get(i) for i in ids] if isinstance(ids,list) else self.messages.get(ids)
    def iter_download(self,location,**kwargs):
        assert kwargs['request_size']==PART
        return Parts(self.parts[location.id-5000],self.log)


def fake_provider(client,clock):
    from test_telegram_governor import State
    from messaging_service.telegram_collector import TelethonProvider
    from messaging_service.telegram_governor import RequestGovernor
    provider=TelethonProvider.__new__(TelethonProvider);provider.client=client
    state=State(clock);provider.governor=RequestGovernor(state,clock=clock,sleep=clock.sleep)
    return provider,state


def media_admissions(state):
    return sum(1 for entry in state.phases if entry==('receiving','telegram_media'))


# spec: ANYM-877
@pytest.mark.parametrize('parts,admissions',[([100],2),([PART,PART,100],4),([PART,PART],3)],
    ids=['one_chunk','three_chunks','exact_multiple'])
def test_a_download_takes_one_governor_admission_per_provider_request(parts,admissions):
    from test_telegram_governor import Clock
    from messaging_service.adapters import telegram_message
    from messaging_service.telegram_governor import LIVE_MEDIA
    item=document(10,sum(parts));descriptor=telegram_message('tg-live',item)['media'][0]
    client=Files({10:item},{10:[b'x'*n for n in parts]});clock=Clock()
    provider,state=fake_provider(client,clock)
    source={'account':'tg-live','provider_chat':'100','provider_id':'10','_tier':LIVE_MEDIA}
    async def collect():return b''.join([c async for c in provider.chunks(source,descriptor)])
    data=asyncio.run(collect())
    assert len(data)==sum(parts) and media_admissions(state)==admissions
    # The stream ends after the last part: no further __anext__, so no admission for it.
    assert client.log==['next']*len(parts)


# spec: ANYM-877
@pytest.mark.parametrize('declared,parts',[(1000,[100]),(100,[PART])],ids=['short','long'])
def test_a_short_or_long_stream_still_fails_the_size_check_in_the_writer(tmp_path,declared,parts):
    from test_telegram_governor import Clock
    from messaging_service.adapters import telegram_message
    item=document(10,declared);descriptor=telegram_message('tg-live',item)['media'][0]
    provider,_=fake_provider(Files({10:item},{10:[b'x'*n for n in parts]}),Clock())
    source={'account':'tg-live','provider_chat':'100','provider_id':'10'}
    writer=BlobWriter(tmp_path,reserve_bytes=0)
    with pytest.raises(ValueError,match='media_size_mismatch'):
        asyncio.run(writer.write_async(provider.chunks(source,descriptor),expected_bytes=descriptor['bytes']))


# spec: ANYM-877
def test_one_message_fetch_serves_a_twenty_file_burst_while_reconcile_waits():
    from test_telegram_governor import Clock,busy,stop
    from messaging_service.adapters import telegram_message
    from messaging_service.telegram_governor import BACKGROUND,LIVE_MEDIA
    ids=list(range(1,21));items={i:document(i,1000+i) for i in ids}
    client=Files(items,{i:[b'x'*(1000+i)] for i in ids});clock=Clock()
    provider,state=fake_provider(client,clock)
    async def scenario():
        log=[];reconcile=busy(provider.governor,clock,log,'reconcile',BACKGROUND)
        while not log:await asyncio.sleep(0.001)
        start=clock();ready=[]
        for i in ids:
            # Each job's source lists the chat's other queued live jobs (Archive.media_burst).
            source={'account':'tg-live','provider_chat':'100','provider_id':str(i),'_tier':LIVE_MEDIA,
                '_burst':[j for j in ids if j>i]}
            descriptor=telegram_message('tg-live',items[i])['media'][0]
            assert len(b''.join([c async for c in provider.chunks(source,descriptor)]))==1000+i
            ready.append(clock())
        await stop(reconcile)
        return start,ready,log
    start,ready,log=asyncio.run(scenario())
    assert client.fetches==[ids] and media_admissions(state)==21
    # 21 media admissions; every fifth admission is reconcile's (test_telegram_governor), so 26 intervals.
    assert ready[-1]<=start+26 and any(at>start for _,at in log)
    assert ready==sorted(ready)


# spec: ANYM-877
def test_a_fetched_message_that_no_longer_matches_is_fetched_again():
    from test_telegram_governor import Clock
    from messaging_service.adapters import telegram_message
    items={1:document(1,100),2:document(2,100)}
    client=Files(items,{1:[b'x'*100],2:[b'y'*200]});clock=Clock()
    provider,state=fake_provider(client,clock)
    async def collect(i,descriptor,burst=()):
        source={'account':'tg-live','provider_chat':'100','provider_id':str(i),'_burst':list(burst)}
        return b''.join([c async for c in provider.chunks(source,descriptor)])
    assert asyncio.run(collect(1,telegram_message('tg-live',items[1])['media'][0],[2]))==b'x'*100
    # Message 2 was edited after the burst fetch: its job now describes the new 200-byte file.
    client.messages[2]=edited=document(2,200)
    assert asyncio.run(collect(2,telegram_message('tg-live',edited)['media'][0]))==b'y'*200
    assert client.fetches==[[1,2],2]


# spec: ANYM-877
def test_a_fetched_message_serves_once_and_expires_after_ten_minutes(monkeypatch):
    from test_telegram_governor import Clock
    from messaging_service import telegram_collector
    from messaging_service.adapters import telegram_message
    items={i:document(i,100) for i in (1,2,3,4)}
    client=Files(items,{i:[b'x'*100] for i in items});provider,_=fake_provider(client,Clock())
    async def collect(i,burst=()):
        source={'account':'tg-live','provider_chat':'100','provider_id':str(i),'_burst':list(burst)}
        return b''.join([c async for c in provider.chunks(source,telegram_message('tg-live',items[i])['media'][0])])
    asyncio.run(collect(1,[2,3]))
    at,message=provider._fetched[(100,3)]
    provider._fetched[(100,3)]=(at-telegram_collector.FETCHED_SECONDS-1,message)  # older than ten minutes
    asyncio.run(collect(2));asyncio.run(collect(3))
    assert client.fetches==[[1,2,3],3] and not provider._fetched
    asyncio.run(collect(2))  # used once: a second job for it fetches again
    assert client.fetches[-1]==2
    monkeypatch.setattr(telegram_collector,'FETCHED_MAX',2)
    asyncio.run(collect(1,[2,3,4]))
    assert set(provider._fetched)=={(100,3),(100,4)}  # the oldest copy goes first


def live_original(ar,mid,*,chat='100',age=60,size=4):
    at=(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat()
    return ar.ingest(event(chat=chat,provider_id=mid,sent_at=at,updated_at=at,
        metadata={'chat_type':'private' if chat=='100' else 'group'},
        media=[{'provider_id':'media-'+mid,'filename':'file.bin','mime':'application/octet-stream','bytes':size}]))['message']


def burst_writers(store):
    return (BlobWriter(store.files,max_bytes=64*1024**2,reserve_bytes=100),
            BlobWriter(store.files,max_bytes=32*1024**2,reserve_bytes=20))


# spec: ANYM-877
def test_live_claims_go_private_then_small_then_a_recent_burst_oldest_first(archive,monkeypatch):
    ar,s,_,_=archive
    with s.pool.connection() as c:
        c.execute("UPDATE account_policies SET include_chats=ARRAY['100','200'] WHERE account='archive-test'")
    order={'requested':live_original(ar,'90',chat='200',age=7200,size=3*1024**2)}
    order.update({name:live_original(ar,mid,chat=chat,age=age,size=size) for name,mid,chat,age,size in [
        ('group_small','80','200',60,4),('private_big','70','100',120,3*1024**2),
        ('burst_3','13','100',30,4),('burst_1','11','100',600,4),('burst_2','12','100',300,4),
        ('older_2','21','100',7200,4),('older_1','22','100',3600,4)]})
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET requested_until=clock_timestamp()+interval '1 hour' WHERE message=%s",
            (order['requested'],))
        group=c.execute("SELECT id FROM chats WHERE account='archive-test' AND provider_chat='200'").fetchone()['id']
    # A subscription naming the group keeps its files live instead of on request (ANYM-805).
    token=s.issue_key('media-watcher',['archive-test'],permissions=['read','subscription-write'],
        key_permissions=['read','subscription-write'])
    s.create_subscription(token,[group])
    background,live=burst_writers(s);capacity(monkeypatch,10**13)
    claimed=[]
    while (job:=ar.claim_media('worker',writer=background,live_writer=live)):claimed.append(job['message'])
    names={mid:name for name,mid in order.items()}
    assert [names[mid] for mid in claimed]==['requested','burst_1','burst_2','burst_3','older_1','older_2',
        'private_big','group_small']


# spec: ANYM-877
def test_one_message_fetch_lists_the_chats_other_queued_live_jobs_in_claim_order(archive,monkeypatch):
    ar,s,_,_=archive
    for mid,age in [('11',600),('12',300),('13',30),('21',7200),('22',3600)]:live_original(ar,mid,age=age)
    live_original(ar,'31',age=10*86400)  # outside the live window
    background,live=burst_writers(s);capacity(monkeypatch,10**13)
    job=ar.claim_media('worker',writer=background,live_writer=live)
    assert ar.media_burst(job,window_seconds=86400)==[12,13,22,21]
    assert ar.media_burst(job,window_seconds=86400,limit=2)==[12,13]


# spec: ANYM-877
def test_the_media_worker_passes_its_live_tier_and_burst_to_the_provider(worker,monkeypatch):
    from messaging_service.telegram_governor import LIVE_MEDIA
    w,s,_=worker
    sources=[]
    async def chunks(source,descriptor):
        sources.append(source);yield b'voic'
    w.provider.chunks=chunks;w.provider.fetches_bursts=True
    for mid,age in [('91',300),('92',200)]:
        at=(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat()
        w.archive.ingest(event(account=w.account,provider_id=mid,sent_at=at,updated_at=at,
            media=[{'provider_id':'media-'+mid,'filename':'voice.ogg','mime':'audio/ogg','bytes':4}]))
    w.writer,w.live_writer=writers(s);w.live_window_seconds=86400
    capacity(monkeypatch)
    assert asyncio.run(w.media_once()) is True
    assert sources[0]['_tier']==LIVE_MEDIA and sources[0]['_burst']==[92]


# spec: ANYM-877
def test_a_failed_burst_lookup_fetches_the_one_message_and_logs_under_the_collector_logger(worker,monkeypatch,caplog):
    from messaging_service.telegram_governor import LIVE_MEDIA
    w,s,_=worker
    sources,limits=[],[]
    async def chunks(source,descriptor):
        sources.append(source);yield b'voic'
    def media_burst(job,*,window_seconds,limit):
        limits.append(limit);raise RuntimeError('synthetic-burst-secret')
    w.provider.chunks=chunks;w.provider.fetches_bursts=True
    monkeypatch.setattr(w.archive,'media_burst',media_burst)
    at=(datetime.now(timezone.utc)-timedelta(seconds=300)).isoformat()
    w.archive.ingest(event(account=w.account,provider_id='93',sent_at=at,updated_at=at,
        media=[{'provider_id':'media-93','filename':'voice.ogg','mime':'audio/ogg','bytes':4}]))
    w.writer,w.live_writer=writers(s);w.live_window_seconds=86400
    capacity(monkeypatch)
    caplog.set_level('WARNING')
    assert asyncio.run(w.media_once()) is True
    assert limits==[20] and sources[0]['_tier']==LIVE_MEDIA and '_burst' not in sources[0]
    [record]=[r for r in caplog.records if r.getMessage()=='media_burst_failed error=RuntimeError']
    assert record.name=='messaging_service.telegram_collector' and 'synthetic-burst-secret' not in caplog.text


# spec: ANYM-877
@pytest.mark.parametrize('entry,logger',[('messaging_service.telegram_collector','__main__'),
    ('messaging_service.whatsapp_collector','messaging_service.telegram_collector')])
def test_the_media_path_keeps_the_collector_logger_when_a_collector_module_is_the_entry_point(entry,logger):
    # `python -m <entry>` (Compose) runs that module as __main__ with its spec set (runpy). telegram_collector's
    # own log is then getLogger('__main__'), and the media path it calls logs there too (Astra r1, W5-4a).
    script=('import importlib.util,sys\n'
            f'sys.modules["__main__"].__spec__=importlib.util.find_spec({entry!r})\n'
            'from messaging_service import collector_media\n'
            'print(collector_media.log.name)\n')
    result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=60,check=True)
    assert result.stdout.strip()==logger


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


# spec: ANYM-839
def test_idle_whatsapp_media_loop_waits_after_an_empty_claim_and_wakes_on_its_own_media_ingest(seeded,tmp_path):
    from messaging_service.archive import Archive
    from messaging_service.collector_state import CaptureSpool
    from messaging_service.whatsapp_collector import WhatsAppCollector,WhatsAppState
    s,_,_=seeded;ar=Archive(s);ar.register('wa-one','whatsapp','Synthetic WA',discover=True)
    w=WhatsAppCollector(ar,'wa-one',SimpleNamespace(),CaptureSpool(tmp_path/'pages',reserve_bytes=0),
        events_spool=CaptureSpool(tmp_path/'events',reserve_bytes=0),writer=BlobWriter(s.files,reserve_bytes=0),
        owner='wa-fixture')
    epoch=ar.acquire(w.account,w.owner);w.state=WhatsAppState(ar,w.account,(w.owner,epoch));w.policy=w.state.policy()
    at=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
    envelope=event(account='wa-one',platform='whatsapp',chat='123@c.us',chat_title='Fixture',provider_id='wake-1',
        sender='42@c.us',sent_at=at,updated_at=at,
        media=[{'provider_id':'media-wake-1','filename':'voice.ogg','mime':'audio/ogg','bytes':4}])
    async def ingest():
        await asyncio.to_thread(w.state.apply,{'kind':'message','peer_kind':'user','envelope':envelope})
    idle_loop_wakes_on_own_media(w,ingest)


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
