import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
from threading import Event
from types import SimpleNamespace as NS

import pytest

from messaging_service.archive import Archive
from messaging_service.blobs import BlobWriter
from messaging_service.collector_state import CaptureSpool,CollectorState,session_lock,validate_capture
from messaging_service.envelope import stable_id
from messaging_service.telegram_collector import TelegramCollector,configuration


def message(mid,chat='100',body=None,edited=None,media=False,kind='user'):
    peer={'user_id':42} if kind=='user' else {'channel_id':int(chat)}
    return NS(id=mid,chat_id=int(chat),sender_id=42,message=body or f'message {mid}',
        date=datetime(2026,10,1,12,0,min(mid,59),tzinfo=timezone.utc),edit_date=edited,out=False,
        peer_id=NS(**peer),file=NS(name='test.bin',ext='.bin',mime_type='application/octet-stream',size=4) if media else None,
        document=NS(id=200) if media else None,photo=None,media=NS(id=200) if media else None)


class FakeProvider:
    def __init__(self,messages=()): self.messages=list(messages); self.closed=False; self.reads=[]; self.connected=False
    def subscribe(self,capture,deleted): self.capture,self.deleted=capture,deleted
    async def connect(self): self.connected=True
    async def disconnect(self): self.closed=True
    async def wait_disconnected(self): await asyncio.Event().wait()
    async def dialogs(self,limit): return [('100','user','Fixture')][:limit]
    async def page(self,chat,*,limit,min_id=0,before=0,reverse=False):
        self.reads.append((chat,min_id,before,reverse))
        return sorted([m for m in self.messages if m.id>min_id and (not before or m.id<before)],
                      key=lambda m:m.id,reverse=not reverse)[:limit]
    async def chunks(self,source,descriptor):
        yield b'da'; await asyncio.sleep(.001); yield b'ta'


@pytest.mark.parametrize('kind',['document','photo'])
def test_telegram_link_preview_downloads_concrete_media(kind):
    from telethon import types,utils
    from messaging_service.adapters import telegram_message
    from messaging_service.telegram_collector import TelethonProvider
    now=datetime(2026,10,13,tzinfo=timezone.utc)
    if kind=='document':
        media=types.Document(id=200,access_hash=0,file_reference=b'reference',date=now,
            mime_type='application/octet-stream',size=4,dc_id=1,attributes=[])
    else:
        media=types.Photo(id=200,access_hash=0,file_reference=b'reference',date=now,
            sizes=[types.PhotoSize(type='x',w=2,h=2,size=4)],dc_id=1)
    item=message(10,media=True)
    item.document=media if kind=='document' else None
    item.photo=media if kind=='photo' else None
    item.media=types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=20))
    descriptor=telegram_message('tg-preview',item)['media'][0]
    closed=[]
    class Stream:
        async def __aiter__(self):yield b'data'
        async def close(self):closed.append(True)
    class Client:
        async def get_messages(self,*args,**kwargs):return item
        def iter_download(self,location,**kwargs):
            # Run the pinned library's actual location conversion. It rejects
            # MessageMediaWebPage; documents/photos preserve the original size.
            if kind=='document':
                assert utils._get_file_info(location).size==4
                assert location is media
            else:
                assert isinstance(location,types.InputPhotoFileLocation)
                assert location.id==media.id and location.thumb_size=='x'
                assert kwargs['file_size']==4 and kwargs['dc_id']==1
            return Stream()
    provider=TelethonProvider.__new__(TelethonProvider);provider.client=Client()
    async def collect():
        return b''.join([chunk async for chunk in provider.chunks(
            {'account':'tg-preview','provider_chat':'100','provider_id':'10'},descriptor)])
    assert asyncio.run(collect())==b'data'
    assert closed==[True]


@pytest.mark.parametrize('progressive',[False,True])
def test_telegram_photo_download_matches_largest_byte_size_not_list_order(progressive):
    from telethon import types,utils
    from messaging_service.adapters import telegram_message
    from messaging_service.telegram_collector import TelethonProvider
    full=(types.PhotoSizeProgressive(type='full',w=100,h=100,sizes=[2,8]) if progressive
          else types.PhotoSize(type='full',w=100,h=100,size=8))
    photo=types.Photo(id=200,access_hash=0,file_reference=b'ref',date=datetime.now(timezone.utc),
                      sizes=[full,types.PhotoSize(type='other',w=120,h=120,size=4)],dc_id=2)
    item=types.Message(id=10,peer_id=types.PeerUser(100),date=datetime.now(timezone.utc),
                       message='',media=types.MessageMediaPhoto(photo=photo))
    descriptor=telegram_message('tg-photo',item)['media'][0]
    assert descriptor['bytes']==8 and utils._get_file_info(photo).size==4
    closed=[]
    class Stream:
        async def __aiter__(self):yield b'original'
        async def close(self):closed.append(True)
    class Client:
        async def get_messages(self,*args,**kwargs):return item
        def iter_download(self,location,**kwargs):
            assert location.thumb_size=='full' and location.id==200
            assert kwargs['file_size']==8 and kwargs['dc_id']==2
            return Stream()
    p=TelethonProvider.__new__(TelethonProvider);p.client=Client()
    async def collect():
        return b''.join([c async for c in p.chunks(
            {'account':'tg-photo','provider_chat':'100','provider_id':'10'},descriptor)])
    assert asyncio.run(collect())==b'original' and closed==[True]


def test_telegram_photo_does_not_replace_original_with_cached_preview():
    from telethon import types
    from messaging_service.adapters import telegram_message
    from messaging_service.telegram_collector import TelethonProvider,ProviderUnavailable
    photo=types.Photo(id=200,access_hash=0,file_reference=b'ref',date=datetime.now(timezone.utc),
        sizes=[types.PhotoCachedSize(type='s',w=1,h=1,bytes=b'cached')],dc_id=2)
    item=types.Message(id=10,peer_id=types.PeerUser(100),date=datetime.now(timezone.utc),
                       message='',media=types.MessageMediaPhoto(photo=photo))
    descriptor=telegram_message('tg-photo',item)['media'][0]
    class Client:
        async def get_messages(self,*args,**kwargs):return item
        def iter_download(self,*args,**kwargs):pytest.fail('preview must not replace an original')
    p=TelethonProvider.__new__(TelethonProvider);p.client=Client()
    async def collect():
        return [c async for c in p.chunks({'account':'tg-photo','provider_chat':'100','provider_id':'10'},descriptor)]
    with pytest.raises(ProviderUnavailable):asyncio.run(collect())


@pytest.mark.parametrize('error_name',['FileReferenceExpiredError','FilerefUpgradeNeededError'])
def test_expired_telegram_media_is_retried_without_stopping_other_jobs(worker,error_name):
    import hashlib
    from telethon import errors
    from messaging_service.telegram_collector import TelethonProvider
    w,s,key=worker
    originals=set(s.files.iterdir())
    expired=message(24,media=True)
    healthy=message(25,media=True)
    asyncio.run(w.capture(NS(message=expired)))
    asyncio.run(w.capture(NS(message=healthy)))
    w.spool.drain(w.state.apply)
    closed=[];reads=[]
    class Stream:
        def __init__(self,fail):self.fail=fail
        async def __aiter__(self):
            yield b'da'
            if self.fail:raise getattr(errors,error_name)(request=None)
            yield b'ta'
        async def close(self):closed.append(True)
    class Client:
        async def get_messages(self,chat,ids):
            reads.append(ids)
            self.fail=ids==24
            return expired if self.fail else healthy
        def iter_download(self,*args,**kwargs):return Stream(self.fail)
    provider=TelethonProvider.__new__(TelethonProvider);provider.client=Client()
    w.provider=provider
    assert asyncio.run(w.media_once())
    assert asyncio.run(w.media_once())
    with s.pool.connection() as c:
        failed=c.execute('SELECT state,error_code,attempts,next_due>now() AS delayed FROM media_jobs WHERE message=%s',
                         (stable_id('msg',w.account,'100','24'),)).fetchone()
        assert failed['state']=='queued' and failed['error_code']=='network_error'
        assert failed['attempts']==1 and failed['delayed']
        c.execute('UPDATE media_jobs SET next_due=now() WHERE message=%s',
                  (stable_id('msg',w.account,'100','24'),))
    assert asyncio.run(w.media_once())
    assert reads.count(24)==2 and reads.count(25)==1 and len(closed)==3
    assert s.get_context(key,stable_id('msg',w.account,'100','24'))['attachments']==[]
    assert len(s.get_context(key,stable_id('msg',w.account,'100','25'))['attachments'])==1
    # Partial bytes from both failed attempts were removed, never published.
    assert set(s.files.iterdir())==originals|{s.files/hashlib.sha256(b'data').hexdigest()}
    assert not list(s.files.glob('.pending-*'))


# spec: ANYM-487
def test_unclassified_telegram_media_error_fails_only_that_job(worker,caplog):
    import hashlib
    from messaging_service.telegram_collector import TelethonProvider
    class SyntheticTransferError(Exception):pass
    w,s,key=worker
    originals=set(s.files.iterdir())
    broken,healthy=message(26,media=True),message(27,media=True)
    asyncio.run(w.capture(NS(message=broken)))
    asyncio.run(w.capture(NS(message=healthy)))
    w.spool.drain(w.state.apply)
    class Stream:
        def __init__(self,fail):self.fail=fail
        async def __aiter__(self):
            yield b'da'
            # Provider text that must never reach the log or the job; also a media code.
            if self.fail:raise SyntheticTransferError('media_unavailable synthetic-secret-text')
            yield b'ta'
        async def close(self):pass
    class Client:
        async def get_messages(self,chat,ids):
            self.fail=ids==26
            return broken if self.fail else healthy
        def iter_download(self,*args,**kwargs):return Stream(self.fail)
    provider=TelethonProvider.__new__(TelethonProvider);provider.client=Client()
    w.provider=provider
    caplog.set_level('WARNING','messaging_service.telegram_collector')
    assert asyncio.run(w.media_once())
    assert asyncio.run(w.media_once())
    broken_id=stable_id('msg',w.account,'100','26')
    with s.pool.connection() as c:
        job=c.execute('SELECT state,error_code,attempts,next_due>now() AS delayed FROM media_jobs WHERE message=%s',
                      (broken_id,)).fetchone()
        assert job['state']=='queued' and job['error_code']=='network_error'
        assert job['attempts']==1 and job['delayed']
    assert len(s.get_context(key,stable_id('msg',w.account,'100','27'))['attachments'])==1
    assert 'media_transfer_failed error=SyntheticTransferError' in caplog.text
    assert 'synthetic-secret-text' not in caplog.text
    # The last attempt ends the job failed; the collector keeps running.
    with s.pool.connection() as c:
        c.execute('UPDATE media_jobs SET attempts=4,next_due=now() WHERE message=%s',(broken_id,))
    assert asyncio.run(w.media_once())
    with s.pool.connection() as c:
        job=c.execute('SELECT state,error_code,attempts FROM media_jobs WHERE message=%s',(broken_id,)).fetchone()
    assert (job['state'],job['error_code'],job['attempts'])==('failed','network_error',5)
    assert not asyncio.run(w.media_once())
    assert s.get_context(key,broken_id)['attachments']==[]
    assert set(s.files.iterdir())==originals|{s.files/hashlib.sha256(b'data').hexdigest()}
    assert not list(s.files.glob('.pending-*'))


# spec: ANYM-487
@pytest.mark.parametrize('error_name',['AuthKeyUnregisteredError','AuthKeyDuplicatedError'])
def test_session_terminal_telegram_media_error_still_stops_collector(worker,error_name):
    from telethon import errors
    from messaging_service.telegram_collector import TelethonProvider
    w,s,_=worker
    item=message(28,media=True)
    asyncio.run(w.capture(NS(message=item))); w.spool.drain(w.state.apply)
    class Stream:
        async def __aiter__(self):
            yield b'da'
            raise getattr(errors,error_name)(request=None)
        async def close(self):pass
    class Client:
        async def get_messages(self,chat,ids):return item
        def iter_download(self,*args,**kwargs):return Stream()
    provider=TelethonProvider.__new__(TelethonProvider);provider.client=Client()
    w.provider=provider
    with pytest.raises(getattr(errors,error_name)):asyncio.run(w.media_once())
    assert not list(s.files.glob('.pending-*'))


@pytest.fixture
def worker(seeded,tmp_path):
    store,_,_=seeded; archive=Archive(store)
    archive.register('tg-collector','telegram','Synthetic collector',discover=True)
    provider=FakeProvider([message(i) for i in range(1,6)])
    spool=CaptureSpool(tmp_path/'capture',reserve_bytes=0)
    worker=TelegramCollector(archive,'tg-collector',provider,spool,batch_size=2,
                             writer=BlobWriter(store.files,reserve_bytes=0),owner='fixture-owner')
    epoch=archive.acquire(worker.account,worker.owner)
    worker.state=CollectorState(archive,worker.account,(worker.owner,epoch)); worker.policy=worker.state.policy()
    return worker,store,store.issue_key('collector-reader',[worker.account])


def test_queue_writer_does_not_wait_for_database(worker):
    w,_,_=worker
    asyncio.run(w.capture(NS(message=message(9))))
    entered=Event(); release=Event()
    def blocked(_): entered.set(); assert release.wait(3)
    with ThreadPoolExecutor(max_workers=2) as threads:
        draining=threads.submit(w.spool.drain,blocked)
        assert entered.wait(1)
        capture=threads.submit(lambda:asyncio.run(w.capture(NS(message=message(10)))))
        try: capture.result(timeout=1)
        finally: release.set()
        assert draining.result()==1
    assert len(list(w.spool.path.glob('*.json')))==1


def test_checkpoint_backfill_catchup_and_overlap_edit(worker):
    w,s,key=worker
    asyncio.run(w.sync_once())
    assert {m['body'] for m in s.search(key)['items']}=={'message 4','message 5'}
    assert all(e['historical'] for e in s.get_updates(key,'events')['items'])
    first=w.state.chat('100','user'); assert first['high_water']==5 and first['history_before']==4
    w.provider.messages.extend([message(6),message(7),message(8)])
    asyncio.run(w.sync_once())
    assert w.state.chat('100','user')['high_water']==7  # Page boundary, not latest observed callback.
    assert w.state.chat('100','user')['history_before']==2
    # Change an item within the recent overlap; it is a revision rather than a new message.
    w.provider.messages[-1]=message(8,body='edited',edited=datetime(2026,10,2,tzinfo=timezone.utc))
    asyncio.run(w.sync_once())
    assert w.state.chat('100','user')['high_water']==8
    assert w.state.chat('100','user')['history_exhausted']
    assert len(s.search(key)['items'])==8
    assert s.search(key,'edited')['items'][0]['id']==stable_id('msg',w.account,'100','8')
    w.spool.drain(w.state.apply)
    assert len(s.search(key)['items'])==8


def test_page_failure_leaves_checkpoint_and_durable_captures_for_replay(worker):
    w,s,key=worker; original=w.state.apply; calls=0
    def fail_second(item):
        nonlocal calls
        calls+=1
        if calls==2: raise RuntimeError('database_down')
        return original(item)
    w.state.apply=fail_second
    with pytest.raises(RuntimeError,match='database_down'): asyncio.run(w.sync_once())
    assert not w.state.chat('100','user')['initialized']
    # Committed history leaves its spool; failed history stays outside live capture.
    assert not list(w.spool.path.glob('*.json'))
    assert len(list(w.history_spool.path.glob('*.json')))==1
    w.state.apply=original; w.history_spool.drain(w.state.apply)
    asyncio.run(w.sync_once())
    assert len(s.search(key)['items'])==2 and w.state.chat('100','user')['high_water']==5


def test_callback_does_not_jump_reconciliation_checkpoint(worker):
    w,s,key=worker
    asyncio.run(w.capture(NS(message=message(50))))
    assert not s.search(key)['items']
    w.spool.drain(w.state.apply)
    assert w.state.chat('100','user')['high_water']==0
    asyncio.run(w.sync_once())
    assert w.state.chat('100','user')['high_water']==5


def test_chatless_delete_is_durable_and_never_deletes_channel_collision(worker):
    w,s,key=worker
    async def capture():
        await w.capture(NS(message=message(9)))
        await w.capture(NS(message=message(9,chat='-100123',kind='channel')))
        await w.deleted(NS(chat_id=None,deleted_ids=[9]))
    asyncio.run(capture()); assert len(list(w.spool.path.glob('*.json')))==3
    w.spool.drain(w.state.apply)
    remaining=s.search(key)['items']; assert len(remaining)==1
    assert remaining[0]['chat']==stable_id('chat',w.account,'-100123')
    assert s.get_context(key,stable_id('msg',w.account,'100','9'))['message']['deleted']


def test_delete_before_history_and_restart_does_not_resurrect(worker):
    w,s,key=worker
    asyncio.run(w.deleted(NS(chat_id=None,deleted_ids=[4])))
    w.spool.drain(w.state.apply)
    asyncio.run(w.sync_once())
    assert [m['body'] for m in s.search(key)['items']]==['message 5']
    mid=stable_id('msg',w.account,'100','4')
    assert s.get_context(key,mid)['message']['deleted']
    # Reopened spool replays safely and keeps the tombstone.
    reopened=CaptureSpool(w.spool.path,reserve_bytes=0)
    reopened.drain(w.state.apply)
    assert s.get_context(key,mid)['message']['deleted']


def test_missing_recent_message_is_not_inferred_deleted(worker):
    w,s,key=worker; asyncio.run(w.sync_once())
    w.provider.messages=[m for m in w.provider.messages if m.id!=4]
    asyncio.run(w.sync_once())
    assert not s.get_context(key,stable_id('msg',w.account,'100','4'))['message']['deleted']
    assert s.capabilities(key)['sources'][0]['coverage']=='partial'


def test_spool_failure_stops_collector_explicitly(worker):
    w,_,_=worker
    def full(_): raise RuntimeError('spool_full')
    w.spool.put=full
    asyncio.run(w.capture(NS(message=message(1))))
    assert w.failed.is_set() and w.failure_code=='durable_capture_failed'


def test_foreign_capture_and_lost_lease_rejected(worker):
    w,_,_=worker
    asyncio.run(w.capture(NS(message=message(1))))
    item=json.loads(next(w.spool.path.glob('*.json')).read_text())
    foreign=deepcopy(item); foreign['envelope']['account']='another'
    with pytest.raises(ValueError,match='foreign_capture'): w.state.apply(foreign)
    w.archive.release(w.account,w.owner,w.state.lease[1])
    with pytest.raises(RuntimeError,match='lease_lost'): w.spool.drain(w.state.apply)
    assert list(w.spool.path.glob('*.json'))


def test_async_media_stream_and_expired_renewal(worker):
    w,s,key=worker
    asyncio.run(w.capture(NS(message=message(20,media=True)))); w.spool.drain(w.state.apply)
    assert asyncio.run(w.media_once())
    mid=stable_id('msg',w.account,'100','20')
    attachment=s.get_context(key,mid)['attachments'][0]
    assert (s.files/attachment['digest']).read_bytes()==b'data'
    assert not asyncio.run(w.media_once())
    asyncio.run(w.capture(NS(message=message(21,media=True)))); w.spool.drain(w.state.apply)
    job=w.archive.claim_media('another-worker',account=w.account)
    w.archive.renew_media(job)
    with s.pool.connection() as c: c.execute("UPDATE media_jobs SET lease_until=now()-interval '1 second' WHERE id=%s",(job['id'],))
    with pytest.raises(RuntimeError,match='media_lease_lost'): w.archive.renew_media(job)


def test_delete_during_download_never_publishes_ready(worker):
    w,s,key=worker
    asyncio.run(w.capture(NS(message=message(22,media=True)))); w.spool.drain(w.state.apply)
    async def chunks(source,descriptor):
        yield b'da'
        await w.deleted(NS(chat_id=100,deleted_ids=[22]))
        w.spool.drain(w.state.apply)
        yield b'ta'
    w.provider.chunks=chunks
    assert asyncio.run(w.media_once())
    assert s.get_context(key,stable_id('msg',w.account,'100','22'))['attachments']==[]


def test_session_lock_rejects_second_owner_and_releases(tmp_path):
    session=tmp_path/'telegram.session'
    with session_lock(session):
        with pytest.raises(RuntimeError,match='session_already_owned'):
            with session_lock(session): pytest.fail('second owner')
    with session_lock(session): pass


def telegram_session(tmp_path,mode):
    directory=tmp_path/'sessions'; directory.mkdir(mode=0o700); directory.chmod(mode)
    session=directory/'telegram.session'; session.write_bytes(b'FAKE_SESSION'); session.chmod(0o600)
    return session


def run_telegram(monkeypatch,session,store):
    from messaging_service import telegram_collector as tc
    monkeypatch.setattr(tc,'configuration',lambda _: ({'session_file':str(session)},{}))
    monkeypatch.setattr(tc,'Store',store)
    monkeypatch.setattr(tc,'TelethonProvider',lambda *_: pytest.fail('provider constructed'))
    dsn=session.parent.parent/'synthetic.dsn'; dsn.write_text('synthetic-not-a-connection')
    monkeypatch.setenv('DATABASE_URL_FILE',str(dsn)); monkeypatch.setenv('PROBE_FILES','/unused')
    return asyncio.run(tc.run_config('/unused/synthetic.json'))


@pytest.mark.parametrize('mode',[0o755,0o770],ids=['0755','0770'])
def test_telegram_session_in_shared_directory_fails_before_lock_or_provider(tmp_path,monkeypatch,mode):
    session=telegram_session(tmp_path,mode)
    with pytest.raises(RuntimeError,match='private_session_required'):
        run_telegram(monkeypatch,session,lambda *_: pytest.fail('database opened'))
    assert sorted(p.name for p in session.parent.iterdir())==['telegram.session']
    assert session.parent.stat().st_mode&0o777==mode  # Reported, never repaired.


def test_telegram_session_in_private_directory_proceeds_to_session_lock(tmp_path,monkeypatch):
    session=telegram_session(tmp_path,0o700)
    def store(*_):
        with pytest.raises(RuntimeError,match='session_already_owned'):
            with session_lock(session): pytest.fail('collector did not own the session')
        raise RuntimeError('synthetic_store_stop')
    with pytest.raises(RuntimeError,match='synthetic_store_stop'): run_telegram(monkeypatch,session,store)
    with session_lock(session): pass


def test_telegram_symlinked_session_file_is_rejected_without_following(tmp_path,monkeypatch):
    target=telegram_session(tmp_path,0o700)
    link=target.parent/'link.session'; link.symlink_to(target)
    with pytest.raises(RuntimeError,match='private_session_required'):
        run_telegram(monkeypatch,link,lambda *_: pytest.fail('database opened'))
    assert sorted(p.name for p in target.parent.iterdir())==['link.session','telegram.session']


@pytest.mark.skipif(hasattr(os,'geteuid') and os.geteuid()==0,reason='root-owned fixtures stay trusted; the chown test covers root')
def test_session_lock_rejects_directory_of_another_user(tmp_path,monkeypatch):
    from messaging_service import collector_state
    directory=tmp_path/'sessions'; directory.mkdir(mode=0o700)
    monkeypatch.setattr(collector_state.os,'geteuid',lambda: directory.stat().st_uid+1)
    with pytest.raises(RuntimeError,match='private_session_required'):
        with session_lock(directory/'telegram.session'): pytest.fail('foreign directory accepted')
    assert list(directory.iterdir())==[]


@pytest.mark.skipif(not hasattr(os,'geteuid') or os.geteuid()!=0,reason='chown to a foreign uid needs root')
def test_session_lock_rejects_directory_chowned_to_foreign_uid_as_root(tmp_path):
    directory=tmp_path/'sessions'; directory.mkdir(mode=0o700); os.chown(directory,12345,12345)
    with pytest.raises(RuntimeError,match='private_session_required'):
        with session_lock(directory/'telegram.session'): pytest.fail('foreign directory accepted')
    assert list(directory.iterdir())==[]


def test_session_lock_creates_missing_parent_private_and_keeps_symlink_code(tmp_path):
    session=tmp_path/'new'/'telegram.session'
    with session_lock(session): pass
    assert session.parent.stat().st_mode&0o777==0o700
    elsewhere=tmp_path/'elsewhere'; elsewhere.mkdir(mode=0o700); (tmp_path/'linked').symlink_to(elsewhere)
    with pytest.raises(ValueError,match='session_symlink'):
        with session_lock(tmp_path/'linked'/'telegram.session'): pytest.fail('symlinked parent accepted')


@pytest.mark.parametrize('mode,accepted',[(0o777,False),(0o770,False),(0o1777,True)],ids=['world','group','sticky'])
def test_session_lock_requires_ancestors_that_other_users_cannot_rename(tmp_path,mode,accepted):
    shared=tmp_path/'shared'; shared.mkdir(); shared.chmod(mode)
    directory=shared/'sessions'; directory.mkdir(mode=0o700)
    if accepted:
        with session_lock(directory/'telegram.session'): pass
    else:
        with pytest.raises(RuntimeError,match='private_session_required'):
            with session_lock(directory/'telegram.session'): pytest.fail('renamable ancestor accepted')
        assert list(directory.iterdir())==[]


def test_session_lock_keeps_the_lock_in_the_checked_directory_after_a_swap(tmp_path,monkeypatch):
    from messaging_service import collector_state
    directory=tmp_path/'sessions'; directory.mkdir(mode=0o700)
    moved=tmp_path/'moved'; public=tmp_path/'public'; public.mkdir(); public.chmod(0o777)
    real_open=os.open
    def swap_before_lock(path,*args,**kwargs):
        # Swap after every directory check, just before the lock file opens.
        if str(path).endswith('.owner.lock'):
            directory.rename(moved); directory.symlink_to(public,target_is_directory=True)
        return real_open(path,*args,**kwargs)
    monkeypatch.setattr(collector_state.os,'open',swap_before_lock)
    with session_lock(directory/'telegram.session'): pass
    assert [p.name for p in moved.iterdir()]==['telegram.session.owner.lock'] and list(public.iterdir())==[]


def test_session_path_through_a_symlinked_ancestor_is_rejected(tmp_path):
    # An indirect link: a trusted link whose target link sits in a renamable directory.
    good=tmp_path/'good'; (good/'sessions').mkdir(parents=True,mode=0o700)
    shared=tmp_path/'shared'; shared.mkdir(); shared.chmod(0o777); (shared/'redirect').symlink_to(good)
    trusted_dir=tmp_path/'trusted'; trusted_dir.mkdir(); (trusted_dir/'route').symlink_to(shared/'redirect')
    with pytest.raises(RuntimeError,match='private_session_required'):
        with session_lock(trusted_dir/'route'/'sessions'/'telegram.session'): pytest.fail('renamable link accepted')
    assert list((good/'sessions').iterdir())==[]


def test_session_lock_follows_only_links_and_directories_other_users_cannot_change(tmp_path):
    good=tmp_path/'good'; (good/'sessions').mkdir(parents=True,mode=0o700)
    (tmp_path/'direct').symlink_to(good); (tmp_path/'relative').symlink_to('good/../good')
    for name in ['direct','relative']:
        with session_lock(tmp_path/name/'sessions'/'telegram.session'): pass
    assert [p.name for p in (good/'sessions').iterdir()]==['telegram.session.owner.lock']
    # The system temporary directory: a root-owned link on macOS (/tmp -> private/tmp), sticky everywhere.
    system=Path(tempfile.mkdtemp(dir='/tmp'))
    try:
        with session_lock(system/'sessions'/'telegram.session'): pass
        assert (system/'sessions').stat().st_mode&0o777==0o700
    finally: shutil.rmtree(system)


@pytest.mark.skipif(hasattr(os,'geteuid') and os.geteuid()==0,reason='root ignores directory modes')
def test_session_under_an_inaccessible_ancestor_fails_with_the_privacy_code(tmp_path,monkeypatch,capsys):
    from messaging_service import telegram_pair as pairing
    denied=tmp_path/'denied'; (denied/'sessions').mkdir(parents=True,mode=0o700); denied.chmod(0)
    session=denied/'sessions'/'telegram.session'
    try:
        with pytest.raises(RuntimeError,match='private_session_required'):
            with session_lock(session): pytest.fail('uninspectable ancestor accepted')
        monkeypatch.setattr(pairing,'require_owner_terminal',lambda: None)
        monkeypatch.setattr(pairing,'configuration',lambda _: ({'session_file':str(session)},{}))
        monkeypatch.setattr(pairing,'Store',lambda *_: pytest.fail('database opened'))
        monkeypatch.setattr(pairing.sys,'argv',['telegram-pair','--config','/unused/synthetic.json'])
        old_mask=os.umask(0o077)
        try:
            with pytest.raises(SystemExit): pairing.main()
        finally: os.umask(old_mask)
        assert json.loads(capsys.readouterr().out)=={'status':'not_paired','code':'private_session_required'}
    finally: denied.chmod(0o700)
    assert list((denied/'sessions').iterdir())==[]


def test_session_whose_parent_is_a_file_fails_with_the_privacy_code(tmp_path,monkeypatch,capsys):
    from messaging_service import telegram_pair as pairing
    parent=tmp_path/'not-a-directory'; parent.write_bytes(b'FAKE'); parent.chmod(0o600)
    session=parent/'telegram.session'
    with pytest.raises(RuntimeError,match='private_session_required'):
        with session_lock(session): pytest.fail('file parent accepted')
    with pytest.raises(RuntimeError,match='private_session_required'):
        run_telegram(monkeypatch,session,lambda *_: pytest.fail('database opened'))
    monkeypatch.setattr(pairing,'require_owner_terminal',lambda: None)
    monkeypatch.setattr(pairing,'configuration',lambda _: ({'session_file':str(session)},{}))
    monkeypatch.setattr(pairing,'Store',lambda *_: pytest.fail('database opened'))
    monkeypatch.setattr(pairing.sys,'argv',['telegram-pair','--config','/unused/synthetic.json'])
    old_mask=os.umask(0o077)
    try:
        with pytest.raises(SystemExit): pairing.main()
    finally: os.umask(old_mask)
    assert json.loads(capsys.readouterr().out)=={'status':'not_paired','code':'private_session_required'}
    assert parent.read_bytes()==b'FAKE'


def test_whatsapp_session_directory_with_shared_parent_is_rejected(tmp_path,monkeypatch):
    from messaging_service import whatsapp_collector as wa
    parent=tmp_path/'sessions'; parent.mkdir(mode=0o700)
    session=parent/'whatsapp'; session.mkdir(mode=0o700)
    (session/'auth.sqlite').write_bytes(b'FAKE_AUTH'); (session/'auth.sqlite').chmod(0o600)
    parent.chmod(0o755)
    monkeypatch.setattr(wa,'configuration',lambda _: {'session_directory':str(session),'provider':'whatsmeow'})
    monkeypatch.setattr(wa,'Store',lambda *_: pytest.fail('database opened'))
    dsn=tmp_path/'synthetic.dsn'; dsn.write_text('synthetic-not-a-connection')
    monkeypatch.setenv('DATABASE_URL_FILE',str(dsn)); monkeypatch.setenv('PROBE_FILES','/unused')
    with pytest.raises(RuntimeError,match='private_session_required'): asyncio.run(wa.run_config('/unused/synthetic.json'))
    assert sorted(p.name for p in parent.iterdir())==['whatsapp']


def test_telegram_pairing_reports_shared_session_directory(tmp_path,monkeypatch,capsys):
    from messaging_service import telegram_pair as pairing
    session=telegram_session(tmp_path,0o755); session.unlink()
    monkeypatch.setattr(pairing,'require_owner_terminal',lambda: None)
    monkeypatch.setattr(pairing,'configuration',lambda _: ({'session_file':str(session)},{}))
    monkeypatch.setattr(pairing,'Store',lambda *_: pytest.fail('database opened'))
    monkeypatch.setattr(pairing.sys,'argv',['telegram-pair','--config','/unused/synthetic.json'])
    old_mask=os.umask(0o077)
    try:
        with pytest.raises(SystemExit): pairing.main()
    finally: os.umask(old_mask)
    assert json.loads(capsys.readouterr().out)=={'status':'not_paired','code':'private_session_required'}
    assert list(session.parent.iterdir())==[]


def test_disabled_and_unconfirmed_configuration_never_constructs_provider(tmp_path):
    cfg=tmp_path/'config.json'; cfg.write_text('{"enabled":false}'); cfg.chmod(0o600)
    with pytest.raises(RuntimeError,match='collector_disabled'): configuration(cfg)
    cfg.chmod(0o644)
    with pytest.raises(ValueError,match='permissions'): configuration(cfg)
    for bad in [{}, {'kind':'telegram_delete','account':'a','chat':'','ids':[True],'observed_at':'2026-10-01T00:00:00Z'}]:
        with pytest.raises(ValueError): validate_capture(bad)


def test_runner_graceful_stop_disconnects_and_releases_lease(worker):
    w,s,_=worker
    w.archive.release(w.account,w.owner,w.state.lease[1])
    async def scenario():
        running=asyncio.create_task(w.run())
        for _ in range(100):
            if w.provider.connected: break
            await asyncio.sleep(.01)
        assert w.provider.connected
        w.stopping.set()
        await asyncio.wait_for(running,5)
    asyncio.run(scenario())
    assert w.provider.closed
    assert w.archive.acquire(w.account,'replacement')>w.state.lease[1]


@pytest.mark.parametrize('reason',['stop','connect_timeout','lease_lost'])
def test_runner_tears_down_hung_connect_and_releases_lease(worker,reason):
    w,s,_=worker
    w.archive.release(w.account,w.owner,w.state.lease[1])
    teardown=[]; active_loops=[]
    async def scenario():
        entered=asyncio.Event()
        async def connect():
            entered.set()
            try: await asyncio.Event().wait()
            finally: teardown.append('connect_cancelled')
        async def disconnect():
            teardown.append('disconnected'); w.provider.closed=True
        async def unexpected_loop(): active_loops.append('started')
        async def lost_lease():
            await entered.wait()
            raise RuntimeError('collector_lease_lost')
        w.provider.connect=connect; w.provider.disconnect=disconnect
        w.drain_loop=w.reconcile_loop=w.media_loop=unexpected_loop
        w.provider.wait_disconnected=unexpected_loop
        if reason=='connect_timeout': w.connect_timeout=.05
        if reason=='lease_lost': w.heartbeat=lost_lease
        running=asyncio.create_task(w.run())
        try:
            await asyncio.wait_for(entered.wait(),3)
            if reason=='stop':
                w.stopping.set()
                # Far shorter than the normal 60s/WhatsApp 300s connect timeout.
                await asyncio.wait_for(asyncio.shield(running),3)
            else:
                error=TimeoutError if reason=='connect_timeout' else RuntimeError
                code='provider_connect_timeout' if reason=='connect_timeout' else 'collector_lease_lost'
                with pytest.raises(error,match=code):
                    await asyncio.wait_for(asyncio.shield(running),3)
        finally:
            if not running.done(): running.cancel()
            await asyncio.gather(running,return_exceptions=True)
    asyncio.run(scenario())
    assert teardown==['connect_cancelled','disconnected']
    assert w.provider.closed and not active_loops and not w.provider.reads
    assert w.archive.acquire(w.account,'replacement')>w.state.lease[1]


def test_async_cancellation_removes_partial_file(tmp_path):
    writer=BlobWriter(tmp_path/'files',reserve_bytes=0)
    async def scenario():
        pending=asyncio.Event()
        async def chunks():
            yield b'first'
            pending.set(); await asyncio.Event().wait()
        task=asyncio.create_task(writer.write_async(chunks()))
        await pending.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(scenario())
    assert not list(writer.root.iterdir())


def test_restricted_collector_role_ingests_but_cannot_read_keys_or_change_policy(worker):
    from contextlib import contextmanager
    import psycopg
    from messaging_service import archive as archive_module
    w,s,key=worker
    role='collector_fixture_role_20261013'
    ddl=Path(archive_module.__file__).with_name('collector_role.sql').read_text().replace('messaging_collector',role)
    with s.pool.connection() as c: c.execute(ddl)
    class RestrictedPool:
        @contextmanager
        def connection(self):
            with s.pool.connection() as c:
                c.execute('SET LOCAL ROLE '+role)
                yield c
    restricted=Archive(NS(pool=RestrictedPool(),append_event=s.append_event,files=s.files))
    state=CollectorState(restricted,w.account,w.state.lease)
    try:
        asyncio.run(w.capture(NS(message=message(23,media=True))))
        w.spool.drain(state.apply)
        job=restricted.claim_media('restricted-worker',account=w.account)
        restricted.renew_media(job)
        blob=w.writer.write([b'data']); restricted.finish_media(job,blob,w.writer)
        state.status('receiving','telegram_history_partial')
        with restricted.store.pool.connection() as c:
            c.execute('SELECT id,permissions FROM principals')
            c.execute('SELECT id,revoked,expires_at FROM api_keys')
        for query in ['SELECT * FROM api_keys','SELECT digest FROM api_keys',
                      'UPDATE account_policies SET enabled=false','CREATE TABLE forbidden_test(id int)']:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with restricted.store.pool.connection() as c: c.execute(query)
    finally:
        with s.pool.connection() as c:
            c.execute('DROP OWNED BY '+role); c.execute('DROP ROLE '+role)


def test_flood_wait_schedules_media_without_early_retry(worker):
    from messaging_service.telegram_collector import ProviderRateLimited
    w,s,key=worker
    asyncio.run(w.capture(NS(message=message(24,media=True)))); w.spool.drain(w.state.apply)
    async def limited(source,descriptor):
        raise ProviderRateLimited(3600)
        yield b''
    w.provider.chunks=limited
    assert asyncio.run(w.media_once())
    with s.pool.connection() as c:
        row=c.execute("SELECT state,error_code,next_due>now()+interval '3590 seconds' AS delayed FROM media_jobs WHERE account=%s",(w.account,)).fetchone()
    assert row['state']=='queued' and row['error_code']=='rate_limited' and row['delayed']


def test_collector_losing_ownership_cannot_publish_finished_download(worker):
    w,s,key=worker
    asyncio.run(w.capture(NS(message=message(25,media=True)))); w.spool.drain(w.state.apply)
    async def lost(source,descriptor):
        yield b'da'
        w.archive.release(w.account,w.owner,w.state.lease[1])
        yield b'ta'
    w.provider.chunks=lost
    with pytest.raises(RuntimeError,match='collector_lease_lost'): asyncio.run(w.media_once())
    assert s.get_context(key,stable_id('msg',w.account,'100','25'))['attachments']==[]


def test_pairing_without_owner_terminal_fails_before_config_or_provider(tmp_path):
    from messaging_service.telegram_pair import pair_config
    with pytest.raises(RuntimeError,match='owner_terminal_required'):
        asyncio.run(pair_config(tmp_path/'missing-config.json'))


def test_backup_covers_collector_positions_and_pending_deletions(worker,tmp_path):
    import sys
    from messaging_service.backup import create_snapshot,verify_snapshot
    w,s,_=worker
    asyncio.run(w.sync_once())
    asyncio.run(w.deleted(NS(chat_id=None,deleted_ids=[999])))
    w.spool.drain(w.state.apply)
    directory=tmp_path/'snapshot'
    create_snapshot(s,directory,[sys.executable,'-c','print("unit-dump-stub")'])
    manifest=json.loads((directory/'manifest.json').read_text())
    assert manifest['schema_version']==45
    assert manifest['tables']['collector_chats']['rows']==1
    assert manifest['tables']['provider_tombstones']['rows']==1
    assert verify_snapshot(s,directory)['status']=='verified'
    with s.pool.connection() as c: c.execute('DELETE FROM provider_tombstones WHERE account=%s',(w.account,))
    with pytest.raises(RuntimeError,match='restored_database_mismatch'): verify_snapshot(s,directory)
