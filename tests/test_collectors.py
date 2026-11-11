import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from threading import Event
from types import SimpleNamespace as NS

import pytest

from messaging_service.archive import Archive
from messaging_service.blobs import BlobWriter
from messaging_service.media_retry import MEDIA_MAX_ATTEMPTS
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
        assert failed['state']=='queued' and failed['error_code']=='media_reference_expired'
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
        c.execute('UPDATE media_jobs SET attempts=%s,next_due=now() WHERE message=%s',(MEDIA_MAX_ATTEMPTS-1,broken_id))
    assert asyncio.run(w.media_once())
    with s.pool.connection() as c:
        job=c.execute('SELECT state,error_code,attempts FROM media_jobs WHERE message=%s',(broken_id,)).fetchone()
    assert (job['state'],job['error_code'],job['attempts'])==('failed','network_error',MEDIA_MAX_ATTEMPTS)
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


# spec: ANYM-840
def test_telegram_channel_difference_fetch_logs_chat_count_and_timing_only(capsys):
    from telethon import TelegramClient,types,utils
    from telethon.sessions import StringSession
    from telethon._updates.messagebox import State as BoxState,PossibleGap,PrematureEndReason
    from messaging_service.telegram_collector import TelethonProvider
    # A real, never-connected client: the hooks are the ones Telethon's update loop calls.
    client=TelegramClient(StringSession(),1,'synthetic',receive_updates=False)
    provider=object.__new__(TelethonProvider);provider.client=client
    provider.bind_state(NS(account='tg-gate',update_gap=lambda:None))
    class Hashes:
        self_bot=False
        def get(self,entry):return NS(id=entry,hash=99) if entry!=777 else None
        def extend(self,users,chats):pass
    hashes=Hashes();sent=datetime.now(timezone.utc)-timedelta(seconds=7770)
    def channel_message(i):
        return types.Message(id=i,peer_id=types.PeerChannel(4242),date=sent,message='synthetic private text')
    def difference(final,*ids):
        return types.updates.ChannelDifference(final=final,pts=8,new_messages=[channel_message(i) for i in ids],
            other_updates=[],chats=[],users=[])
    def new_messages(updates):return sum(isinstance(u,types.UpdateNewChannelMessage) for u in updates)
    async def scenario():
        box=client._message_box;loop=asyncio.get_running_loop()
        # The no-updates deadline expired: a timeout fetch of two pages keeps its trigger on page 2.
        box.map[4242]=BoxState(pts=5,deadline=loop.time()-1);box.next_deadline=4242;box.check_deadlines()
        assert new_messages(box.apply_channel_difference(box.get_channel_difference(hashes),difference(False,1,2),hashes)[0])==2
        assert new_messages(box.apply_channel_difference(box.get_channel_difference(hashes),difference(True,3),hashes)[0])==1
        # An expired pts gap; the first request gets no response (Telethon sleeps on OSError and asks again).
        box.map[4242].deadline=loop.time()+600;box.possible_gaps[4242]=PossibleGap(deadline=loop.time()-1,updates=[])
        box.check_deadlines();box.get_channel_difference(hashes)
        box.apply_channel_difference(box.get_channel_difference(hashes),types.updates.ChannelDifferenceEmpty(final=True,pts=8),hashes)
        # Telegram says the channel is too long: the latest messages come in `messages`; then the fetch ends early.
        box.try_begin_get_diff(4242,'received updateChannelTooLong')
        dialog=types.Dialog(peer=types.PeerChannel(4242),top_message=4,read_inbox_max_id=0,read_outbox_max_id=0,
            unread_count=0,unread_mentions_count=0,unread_reactions_count=0,unread_poll_votes_count=0,
            notify_settings=types.PeerNotifySettings(),pts=12)
        too_long=types.updates.ChannelDifferenceTooLong(dialog=dialog,messages=[channel_message(4)],chats=[],users=[],final=True)
        assert box.apply_channel_difference(box.get_channel_difference(hashes),too_long,hashes)==([],[],[])
        box.end_channel_difference(box.get_channel_difference(hashes),PrematureEndReason.TEMPORARY_SERVER_ISSUES,hashes)
        # A channel without an access hash is dropped by Telethon with no request.
        box.map[777]=BoxState(pts=1,deadline=loop.time()-1);box.next_deadline=777;box.check_deadlines()
        assert box.get_channel_difference(hashes) is None
        # A diff the hook cannot read never breaks Telethon's own apply.
        box.map[4242].deadline=loop.time()-1;box.next_deadline=4242;box.check_deadlines();request=box.get_channel_difference(hashes)
        broken=types.updates.ChannelDifference(final=True,pts=9,new_messages=[NS(id=5)],other_updates=[],chats=[],users=[])
        assert new_messages(box.apply_channel_difference(request,broken,hashes)[0])==1
        # Every finished fetch left the tracker: it stays bounded.
        assert provider.channel_fetches=={}
    # A JSON event on stderr: it reaches prod logs without any logging configuration.
    asyncio.run(scenario());provider.channel_events.join()
    out=capsys.readouterr().err
    events=[json.loads(line) for line in out.splitlines() if '"telegram_channel_difference"' in line]
    chat=stable_id('chat','tg-gate',str(utils.get_peer_id(types.PeerChannel(4242))))
    other=stable_id('chat','tg-gate',str(utils.get_peer_id(types.PeerChannel(777))))
    fields=['trigger','result','page','messages','final']
    assert [[event[f] for f in fields] for event in events]==[
        ['timeout','difference',1,2,False],['timeout','difference',2,1,True],['gap','no_response',1,0,None],
        ['gap','empty',2,0,True],['channel_too_long','too_long',1,1,True],['channel_too_long','ended_early',2,0,None],
        ['timeout','no_access_hash',0,0,None],['timeout','difference',1,1,True]]
    assert [event['chat'] for event in events]==[chat]*6+[other,chat]
    keys={'event','chat',*fields,'elapsed_ms','fetch_ms','oldest_age_s'}
    assert all(set(event)==keys for event in events)
    assert events[0]['oldest_age_s']>0 and events[4]['oldest_age_s']>0 and events[3]['oldest_age_s'] is None
    assert events[6]['elapsed_ms'] is None and all(isinstance(event['fetch_ms'],int) for event in events)
    # The raw channel ids never appear. The measured numbers are left out of that check: a count of milliseconds or the age
    # of a message can hold the digits 4242 or 777 by chance, and the age moves with the clock.
    measured={'elapsed_ms','fetch_ms','oldest_age_s'}
    logged='\n'.join(json.dumps({k:v for k,v in json.loads(line).items() if k not in measured})
        if '"telegram_channel_difference"' in line else line for line in out.splitlines())
    assert 'synthetic private text' not in out and '4242' not in logged and '777' not in logged


# spec: ANYM-840
def test_a_stalled_log_pipe_never_blocks_the_channel_difference_hooks(monkeypatch):
    import sys,threading,time
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from messaging_service.telegram_collector import TelethonProvider
    client=TelegramClient(StringSession(),1,'synthetic',receive_updates=False)
    provider=object.__new__(TelethonProvider);provider.client=client
    provider.bind_state(NS(account='tg-stall',update_gap=lambda:None))
    release=threading.Event();written=[]
    class Stalled:
        # Docker's blocking log delivery with a full pipe: every write waits.
        def write(self,text):
            release.wait(10);written.append(text);return len(text)
        def flush(self):pass
    monkeypatch.setattr(sys,'stderr',Stalled())
    calls=provider.CHANNEL_EVENTS_MAX+50
    async def scenario():
        ticks=[];loop=asyncio.get_running_loop();loop.call_soon(ticks.append,1)
        started=time.monotonic()
        for _ in range(calls):provider.channel_fetch_log(4242,['timeout',loop.time(),1,None],'empty',None,loop.time())
        elapsed=time.monotonic()-started
        await asyncio.sleep(0)
        return elapsed,ticks
    elapsed,ticks=asyncio.run(scenario())
    # The hooks returned at once and the event loop ran on; what the queue could not hold was dropped and counted.
    assert elapsed<1 and ticks==[1]
    assert provider.channel_events_dropped>=calls-provider.CHANNEL_EVENTS_MAX-1
    release.set();provider.channel_events.join()
    lines=[line for line in written if line.strip()]
    assert len(lines)==calls-provider.channel_events_dropped
    assert all(json.loads(line)['event']=='telegram_channel_difference' for line in lines)


# spec: ANYM-840
def test_gate_events_and_the_collectors_stdout_events_never_splice(monkeypatch):
    import sys,threading
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from messaging_service.telegram_collector import TelethonProvider
    client=TelegramClient(StringSession(),1,'synthetic',receive_updates=False)
    provider=object.__new__(TelethonProvider);provider.client=client
    provider.bind_state(NS(account='tg-splice',update_gap=lambda:None))
    chunks={'out':[],'err':[]};lock=threading.Lock()
    class Recorder:
        # Every write call is one chunk; a record split across calls could interleave with another thread's.
        def __init__(self,name):self.name=name
        def write(self,text):
            with lock:chunks[self.name].append(text)
            return len(text)
        def flush(self):pass
    monkeypatch.setattr(sys,'stdout',Recorder('out'));monkeypatch.setattr(sys,'stderr',Recorder('err'))
    async def scenario():
        loop=asyncio.get_running_loop()
        for i in range(300):
            provider.channel_fetch_log(4242,['timeout',loop.time(),1,None],'empty',None,loop.time())
            # The collector's existing stdout events, printed from the loop at the same time.
            print(json.dumps({'event':'telegram_discovery','dialogs':i}),flush=True)
            await asyncio.sleep(0)
    asyncio.run(scenario());provider.channel_events.join()
    # Gate events are whole lines on stderr, one write each; stdout holds only the collector's own events.
    assert len(chunks['err'])==300 and all(chunk.endswith('\n') and chunk.count('\n')==1 for chunk in chunks['err'])
    assert all(json.loads(chunk)['event']=='telegram_channel_difference' for chunk in chunks['err'])
    printed=[line for line in ''.join(chunks['out']).splitlines() if line]
    assert len(printed)==300 and all(json.loads(line)['event']=='telegram_discovery' for line in printed)


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
    w.head_pages=1  # One page per pass, so the checkpoint below stops at a page boundary.
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


def own(item):
    item.out=True;return item


def edit_hand_off(w,edited,**job):
    """A confirmed edit_message on the collector's own operation path; the fake provider leaves the read-back."""
    async def provider(work):
        work['_read_back_message']=edited
        return {'outcome':'confirmed','data':{'basis':'provider_read_back','effect_verified':True}}
    w.provider.execute_operation=provider
    return asyncio.run(w.execute_operation({'operation':'edit_message',**job}))


def drained(w,edits_first=False):
    outcomes=[]
    for spool in ([w.edit_spool,w.spool] if edits_first else [w.spool,w.edit_spool]):
        spool.drain(lambda item:outcomes.append(w.state.apply(item)['outcome']))
    return outcomes


# spec: ANYM-747
@pytest.mark.parametrize('order',['hand_off_first','live_first','hand_off_twice'])
def test_confirmed_edit_hand_off_becomes_one_next_revision(worker,order):
    w,s,key=worker
    asyncio.run(w.capture(NS(message=own(message(9,body='before edit')))))
    assert drained(w)==['created']
    edited=own(message(9,body='after edit',edited=datetime(2026,10,2,tzinfo=timezone.utc)))
    if order=='live_first':asyncio.run(w.capture(NS(message=edited)))
    answer=edit_hand_off(w,edited)
    assert answer['outcome']=='confirmed' and answer['data']['archive']=='queued'
    if order=='hand_off_first':asyncio.run(w.capture(NS(message=edited)))
    if order=='hand_off_twice':edit_hand_off(w,edited)
    outcomes=drained(w,edits_first=order!='live_first')
    assert outcomes[0]=='edited' and len(outcomes)==2 and outcomes[1] in ('duplicate','unchanged')
    with s.pool.connection() as c:
        row=c.execute('SELECT revision,body FROM messages WHERE id=%s',(stable_id('msg',w.account,'100','9'),)).fetchone()
        assert (row['revision'],row['body'])==(2,'after edit')
        assert c.execute("SELECT count(*) AS n FROM message_conflicts").fetchone()['n']==0


# spec: ANYM-922
def test_a_telegram_live_location_is_kept_as_the_fact_and_its_moves_make_no_revision(worker):
    from telethon import types
    w,s,key=worker
    def live(lat,edited=None):
        m=message(9,edited=edited)
        m.media=types.MessageMediaGeoLive(geo=types.GeoPoint(long=37.6,lat=lat,access_hash=1,accuracy_radius=5),period=900)
        return m
    asyncio.run(w.capture(NS(message=live(55.70))))
    assert drained(w)==['created']
    for n in range(10):
        asyncio.run(w.capture(NS(message=live(55.71+n/1000,edited=datetime(2026,10,1,12,n+1,tzinfo=timezone.utc)))))
    assert all(outcome in ('duplicate','unchanged') for outcome in drained(w))
    mid=stable_id('msg',w.account,'100','9')
    with s.pool.connection() as c:
        assert c.execute('SELECT revision FROM messages WHERE id=%s',(mid,)).fetchone()['revision']==1
        assert c.execute('SELECT count(*) AS n FROM message_revisions WHERE message=%s',(mid,)).fetchone()['n']==1
        stored=c.execute('SELECT metadata FROM message_metadata WHERE message=%s',(mid,)).fetchone()['metadata']
    # Only the fact that it was shared: no point of the track is anywhere in the archive.
    assert stored['message_kind']=='location' and stored['location']=={'live':True}
    with s.pool.connection() as c:
        dump=json.dumps([r['snapshot'] for r in c.execute('SELECT snapshot FROM message_revisions WHERE message=%s',(mid,))])
        dump+=json.dumps([r['snapshot'] for r in c.execute('SELECT snapshot FROM events WHERE message=%s',(mid,))])
    assert '55.7' not in dump and '37.6' not in dump


# spec: ANYM-755
def test_a_confirmed_formatted_edit_archives_its_formatting_with_the_next_revision(worker):
    from telethon import types
    w,s,key=worker
    asyncio.run(w.capture(NS(message=own(message(9,body='before edit')))))
    assert drained(w)==['created']
    edited=own(message(9,body='Edited link',edited=datetime(2026,10,2,tzinfo=timezone.utc)))
    edited.entities=[types.MessageEntityBold(0,6),types.MessageEntityTextUrl(7,4,'https://example.invalid/x')]
    assert edit_hand_off(w,edited)['data']['archive']=='queued'
    assert drained(w,edits_first=True)[0]=='edited'
    with s.pool.connection() as c:
        row=c.execute('SELECT revision,snapshot FROM message_revisions WHERE message=%s ORDER BY revision DESC LIMIT 1',
                      (stable_id('msg',w.account,'100','9'),)).fetchone()
    formatting=row['snapshot']['formatting']
    assert (row['revision'],row['snapshot']['body'])==(2,'Edited link')
    assert formatting['state']=='observed' and formatting['basis']=='telegram_native_entities'
    assert [(e['type'],e['offset'],e['length'],e.get('url')) for e in formatting['entities']]==[
        ('bold',0,6,None),('text_link',7,4,'https://example.invalid/x')]


# spec: ANYM-747
@pytest.mark.parametrize('trouble',['spool_raises','slot_full','cancelled'])
def test_edit_hand_off_trouble_never_fails_the_collector(worker,trouble):
    from messaging_service.telegram_collector import CAPTURE_WRITES
    w,s,key=worker
    edited=own(message(9,body='after edit',edited=datetime(2026,10,2,tzinfo=timezone.utc)))
    original=w.edit_spool.put
    if trouble=='spool_raises':
        def broken(item):raise RuntimeError('spool_full')
        w.edit_spool.put=broken
    if trouble=='slot_full':w.capture_count=CAPTURE_WRITES
    job={'operation':'edit_message'}
    if trouble=='cancelled':
        entered=Event()
        def slow(item):entered.set();Event().wait(.3);return original(item)
        w.edit_spool.put=slow
        async def scenario():
            async def provider(work):
                work['_read_back_message']=edited
                return {'outcome':'confirmed','data':{}}
            w.provider.execute_operation=provider
            task=asyncio.ensure_future(w.execute_operation(job))
            while not entered.is_set():await asyncio.sleep(.01)
            task.cancel()
            answer=await task
            while w.capture_count:await asyncio.sleep(.01)  # the slot follows the write, which still ends
            return answer
        answer=asyncio.run(scenario())
        assert job['_cancel_after_settle'] is True  # the command worker settles the receipt, then stops
    else:
        answer=edit_hand_off(w,edited)
    assert answer['outcome']=='confirmed' and answer['data']['archive']=='not_queued'
    assert not w.failed.is_set() and w.failure_code is None
    assert w.capture_count==(CAPTURE_WRITES if trouble=='slot_full' else 0)
    # the worker keeps capturing
    w.capture_count=0;w.edit_spool.put=original
    asyncio.run(w.capture(NS(message=message(10))))
    assert 'created' in drained(w)


# spec: ANYM-747
def test_a_stalled_edit_hand_off_holds_its_slot_and_takes_no_shared_thread(worker,monkeypatch):
    import time
    from contextlib import contextmanager
    from messaging_service import telegram_collector
    w,s,key=worker
    monkeypatch.setattr(telegram_collector,'STORE_GRACE',0)
    release=Event();calls=[]
    real_locked=w.edit_spool.locked
    @contextmanager
    def stalled(name='queue.lock'):
        # The real put stalls inside its publication lock, as a hung fsync would.
        with real_locked(name):
            calls.append(name);assert release.wait(5);yield
    w.edit_spool.locked=stalled
    edited=own(message(9,body='after edit',edited=datetime(2026,10,2,tzinfo=timezone.utc)))
    async def scenario():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        async def provider(work):
            work['_read_back_message']=edited
            return {'outcome':'confirmed','data':{}}
        w.provider.execute_operation=provider
        answers=[]
        for _ in range(3):  # each wait is cut at its 0.2 s budget; the write is still running
            answers.append(await w.execute_operation({'operation':'edit_message','_ends_at':time.monotonic()+1.2}))
        held=w.capture_count
        # the default executor (one thread here) still serves receipts, sends and lease renewal at once,
        # and live captures go on under the live spool's own lock while the hand-off write is stalled
        await asyncio.wait_for(asyncio.to_thread(lambda:None),1)
        for number in range(20,30):await asyncio.wait_for(w.capture(NS(message=message(number))),1)
        release.set()
        while w.capture_count:await asyncio.sleep(.01)
        return answers,held
    answers,held=asyncio.run(scenario())
    assert [a['data']['archive'] for a in answers]==['not_queued']*3
    assert len(calls)==1 and held==1  # one write in flight, its slot held past the timeout; no second write
    assert w.capture_count==0 and not w.failed.is_set() and w.failure_code is None
    # the drain loop's own call, drain_captures([live],[history]), applies the edit spool with the live one
    asyncio.run(w.drain_captures([w.spool],[w.history_spool]))
    assert not list(w.spool.path.glob('*.json')) and not list(w.edit_spool.path.glob('*.json'))
    with s.pool.connection() as c:
        bodies=c.execute('SELECT body FROM messages WHERE id=ANY(%s)',([stable_id('msg',w.account,'100',str(n))
            for n in (9,*range(20,30))],)).fetchall()
    assert sorted(r['body'] for r in bodies)==sorted(['after edit',*(f'message {n}' for n in range(20,30))])


def poisoned_keyboard():
    """A real inline markup whose rows fail when read: the parser itself raises, past the type check."""
    from telethon import types
    class Poisoned(types.ReplyInlineMarkup):
        rows=property(lambda self:(_ for _ in ()).throw(RuntimeError('keyboard exploded SYNTHETIC-CALLBACK')),
                      lambda self,value:None)
    return Poisoned(rows=[])


def serialized_applies(w):
    """Every capture the collector hands the state, as the spool serializes it."""
    seen=[];original=w.state.apply
    def apply(item):
        seen.append(json.dumps(item,default=str));return original(item)
    w.state.apply=apply
    return seen


# spec: ANYM-658
def test_a_failing_keyboard_costs_neither_the_live_nor_the_history_message(worker,monkeypatch):
    from telethon import types
    monkeypatch.setenv('ANYMSG_CAPTURE_BUTTONS','1')
    w,s,key=worker
    seen=serialized_applies(w)
    keyboard=types.ReplyInlineMarkup(rows=[types.KeyboardInlineButtonRow(buttons=[types.KeyboardInlineButton(
        text='Pay',type=types.InlineButtonTypeCallback(data=b'SYNTHETIC-CALLBACK'))])])
    history=[message(i) for i in range(1,6)]
    for item,markup in zip(history,(poisoned_keyboard(),keyboard,poisoned_keyboard(),None,poisoned_keyboard())): item.reply_markup=markup
    w.provider.messages[:]=history
    live=message(9); live.reply_markup=poisoned_keyboard()
    asyncio.run(w.capture(NS(message=live)))
    asyncio.run(w.capture(NS(message=message(10))))
    w.spool.drain(w.state.apply)
    for _ in range(4):asyncio.run(w.sync_once())  # batch_size=2: the history pages come newest first
    w.history_spool.drain(w.state.apply)
    assert {m['body'] for m in s.search(key,limit=50)['items']}>={'message 9','message 10','message 1','message 2','message 3','message 4','message 5'}
    pay=s.search(key,'message 2')['items'][0]
    assert pay['buttons']['rows'][0][0]['text']=='Pay'
    assert seen and all('SYNTHETIC-CALLBACK' not in item and 'keyboard exploded' not in item for item in seen)
    assert 'SYNTHETIC-CALLBACK' not in repr(pay)


# spec: ANYM-658
def test_a_100_button_keyboard_with_2kb_links_survives_the_live_and_history_paths(worker,monkeypatch):
    from messaging_service.envelope import MAX_ENVELOPE,canonical
    from telethon import types
    monkeypatch.setenv('ANYMSG_CAPTURE_BUTTONS','1')
    w,s,key=worker
    seen=serialized_applies(w)
    link='https://example.invalid/'+'a'*2000
    def keyboarded(item):
        buttons=[types.KeyboardInlineButton(text='Link %d'%n,type=types.InlineButtonTypeUrl(url=link+str(n)))
                 for n in range(99)]
        buttons.append(types.KeyboardInlineButton(text='Pay',type=types.InlineButtonTypeCallback(data=b'SYNTHETIC-CALLBACK')))
        item.reply_markup=types.ReplyInlineMarkup(rows=[types.KeyboardInlineButtonRow(buttons=[b]) for b in buttons])
        return item
    asyncio.run(w.capture(NS(message=keyboarded(message(9)))))
    asyncio.run(w.capture(NS(message=message(10))))
    w.spool.drain(w.state.apply)
    w.provider.messages[:]=[keyboarded(message(i)) if i%2 else message(i) for i in range(1,6)]
    for _ in range(4):asyncio.run(w.sync_once())
    w.history_spool.drain(w.state.apply)
    items=s.search(key,limit=50)['items']
    assert len(items)>=7 and any(i['body']=='message 10' for i in items) and any(i['body']=='message 4' for i in items)
    kept=[i for i in items if i.get('buttons')]
    assert len(kept)==4
    for item in kept:
        flat=[b for row in item['buttons']['rows'] for b in row]
        assert len(flat)==100 and flat[0]['url'].startswith('https://example.invalid/') and len(flat[0]['url'])>2000
    assert all(len(item.encode())<=MAX_ENVELOPE for item in seen) and all('SYNTHETIC-CALLBACK' not in item for item in seen)
    assert all(len(canonical(json.loads(item)))<=MAX_ENVELOPE for item in seen)


# spec: ANYM-658
def test_a_near_limit_keyboard_survives_the_live_and_history_paths_and_the_next_messages_too(worker,monkeypatch):
    from telethon import types
    monkeypatch.setenv('ANYMSG_CAPTURE_BUTTONS','1')
    w,s,key=worker
    seen=serialized_applies(w)
    def heavy(item):
        # A multibyte body and 150 long links leave the keyboard only a few KB of the 512 KB envelope.
        item.message='𝔸'*65536
        item.entities=[types.MessageEntityTextUrl(offset=2*n,length=2,url='https://example.invalid/'+'p'*1500) for n in range(150)]
        item.reply_markup=types.ReplyInlineMarkup(rows=[types.KeyboardInlineButtonRow(buttons=[types.KeyboardInlineButton(
            text='𝔸'*128,type=types.InlineButtonTypeCallback(data=b'SYNTHETIC-CALLBACK-%d'%n))]) for n in range(100)])
        return item
    live=heavy(message(9))
    asyncio.run(w.capture(NS(message=live)))
    asyncio.run(w.capture(NS(message=message(10))))
    w.spool.drain(w.state.apply)
    w.provider.messages[:]=[heavy(message(i)) if i%2 else message(i) for i in range(1,6)]
    for _ in range(4):asyncio.run(w.sync_once())
    w.history_spool.drain(w.state.apply)
    items=s.search(key,limit=50)['items']
    assert {len(i['body']) for i in items if i['body'].startswith('𝔸')}=={65536}
    assert len(items)>=7 and any(i['body']=='message 10' for i in items)
    kept=[i for i in items if i.get('buttons')]
    assert kept and all(i['buttons'].get('truncated') is True for i in kept)
    assert all('SYNTHETIC-CALLBACK' not in item for item in seen)


# spec: ANYM-796
def test_a_history_request_gives_an_old_unsupported_link_its_url_in_get_messages(worker,monkeypatch):
    from telethon import types
    link='tg://resolve?domain=synthetic_bot'
    w,s,key=worker
    def linked():
        item=message(9,body='site link')
        item.entities=[types.MessageEntityTextUrl(0,4,link)]
        return item
    monkeypatch.delenv('ANYMSG_CAPTURE_BUTTONS',raising=False)
    asyncio.run(w.capture(NS(message=linked())))
    assert drained(w)==['created']
    cid=stable_id('chat',w.account,'100')
    before=s.get_messages(key,cid)['items'][0]['formatting']
    assert before['unsupported_entities'] and before['warnings']==['native_link_unsupported']
    monkeypatch.setenv('ANYMSG_CAPTURE_BUTTONS','1')
    async def history(job):
        job['_history'].append((linked(),{},'private','Fixture'))
        return {'data':{}}
    w.provider.execute_operation=history
    asyncio.run(w.chat_history({'parameters':{'count':1,'before_message':'anchor'},'chat_id':cid,'provider_chat':'100'}))
    after=s.get_messages(key,cid)['items'][0]['formatting']
    assert after['state']=='observed' and after['unsupported_entities']==[] and after['warnings']==[]
    assert [(e['type'],e['offset'],e['length'],e['url']) for e in after['entities']]==[('text_link',0,4,link)]


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


def idle_loop_wakes_on_own_media(w, ingest):
    """Runs the collector's media loop: an empty claim is not repeated within the idle wait, and `ingest` (this
    collector storing a message with media) wakes it within 1 s (ANYM-839)."""
    from messaging_service import telegram_collector as collector
    assert collector.MEDIA_IDLE_SECONDS>=5
    claims=[]; real=w.archive.claim_media
    def counted(*args,**kwargs):
        claims.append(time.monotonic()); return real(*args,**kwargs)
    w.archive.claim_media=counted
    async def run():
        loop=asyncio.create_task(w.media_loop())
        try:
            while not claims: await asyncio.sleep(.01)
            await asyncio.sleep(4.5)
            assert len(claims)==1,[at-claims[0] for at in claims]
            started=time.monotonic()
            await ingest()
            while len(claims)<2 and time.monotonic()-started<3: await asyncio.sleep(.01)
            assert len(claims)>=2 and claims[1]-started<1,[at-started for at in claims[1:]]
        finally:
            loop.cancel(); await asyncio.gather(loop,return_exceptions=True)
    asyncio.run(run())


# spec: ANYM-839
def test_idle_telegram_media_loop_waits_after_an_empty_claim_and_wakes_on_its_own_media_ingest(worker):
    w,_,_=worker
    async def ingest():
        await w.capture(NS(message=message(30,media=True)))
        await asyncio.to_thread(w.spool.drain,w.state.apply)
    idle_loop_wakes_on_own_media(w,ingest)


# spec: ANYM-839
def test_a_contended_backfill_claim_is_retried_at_once_without_an_idle_wait(worker):
    from messaging_service.archive import MEDIA_CONTENDED
    w,_,_=worker
    w.media_backfill=True
    claims=[]
    def claim(*args,backfill=False,**kwargs):
        claims.append(backfill)
        return MEDIA_CONTENDED if backfill and claims.count(True)==1 else None
    w.archive.claim_media=claim
    # An empty main queue, then a backfill claim that lost its rechecks: not idle.
    assert asyncio.run(w.media_once()) is True
    # The next claim still has the backfill's turn; only an empty backfill starts its pause.
    assert asyncio.run(w.media_once()) is False
    assert claims==[False,True,False,True]


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
    locks=session.parent.parent/'session-locks'; locks.mkdir(mode=0o700,exist_ok=True)
    boot=session.parent.parent/'boot_id'; boot.write_text('synthetic-boot\n')
    monkeypatch.setattr(tc.session_claim,'BOOT_ID',boot)  # ANYM-914: the claim's host lock, synthetic
    monkeypatch.setattr(tc,'configuration',lambda _: ({'session_file':str(session),'account':'telegram-test',
        'session_lock_directory':str(locks)},{}))
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


WA_GROUP='120363000000000009@g.us'


def wa_capture(mid,at,*,body='',kind='chat',group=None,actor='333333@c.us',origin='live',chat=WA_GROUP,title=None):
    """The whatsmeow adapter's envelope (capture.go groupChangeEnvelope / NormalizeMessage), synthetic only."""
    import hashlib
    event={'chatId':chat,'chatName':title or chat,'messageId':mid,'messageType':kind,'timestampResolution':'second',
        'durationSeconds':None,'isService':kind!='chat','isSelfChat':False,'senderId':actor or '','senderName':'','body':body,
        'timestamp':at,'fromMe':False,'hasMedia':False,'filename':None,'mimetype':None,'filesize':None,'replyToId':None,
        'mentionsOwner':None,'replyToOwner':None,'forwarded':None,'mediaKind':None,'editedTimestampMs':None,
        'identityObservations':[],'formatting':{'version':1,'state':'wire_observed','basis':'whatsapp_native_wire',
            'wire_sha256':hashlib.sha256(body.encode()).hexdigest(),'entities':None,'unsupported_entities':[],
            'warnings':['syntax_interpretation_unknown']}}
    if group is not None:event['groupChange']={'actor':actor,'participants':[],'value':None,**group}
    return {'kind':'whatsapp_event','account':'wa-groups','event':event,'change':'message',
        'observed_at':'2026-11-09T10:00:00+00:00','origin':origin}


def group_capture(change,at,origin='live',**group):
    return wa_capture(f'group_{WA_GROUP}_{at}_{change}',at,kind='group_change',group={'change':change,**group},origin=origin)


@pytest.fixture
def wa_groups(seeded):
    store,_,_=seeded; archive=Archive(store)
    archive.register('wa-groups','whatsapp','Synthetic WhatsApp',discover=True)
    epoch=archive.acquire('wa-groups','fixture-owner')
    state=CollectorState(archive,'wa-groups',('fixture-owner',epoch))
    key=store.issue_key('wa-groups-reader',['wa-groups'],permissions=['read','subscription-write'],
        key_permissions=['read','subscription-write'])
    return state,store,key


def wa_chat(store,chat=WA_GROUP):
    with store.pool.connection() as c:
        return c.execute('SELECT title,chat_type FROM chats WHERE id=%s',(stable_id('chat','wa-groups',chat),)).fetchone()


def wa_rows(store,provider_id):
    mid=stable_id('msg','wa-groups',WA_GROUP,provider_id)
    with store.pool.connection() as c:
        return (c.execute('SELECT count(*) AS n FROM messages WHERE id=%s',(mid,)).fetchone()['n'],
            c.execute('SELECT metadata FROM message_metadata WHERE message=%s',(mid,)).fetchone(),
            [(e['kind'],e['origin']) for e in c.execute('SELECT kind,origin FROM events WHERE message=%s ORDER BY id',(mid,))])


# spec: ANYM-869
def test_each_whatsapp_group_change_is_one_service_row_with_one_live_event(wa_groups):
    state,store,key=wa_groups
    members=[f'{700000+i}@c.us' for i in range(3)]
    changes=[('rename',{'value':'Родители 5Б'}),('topic',{'value':'Собрание в пятницу'}),
        ('join',{'actor':members[0],'participants':members[:1]}),('leave',{'actor':members[0],'participants':members[:1]}),
        ('add',{'participants':members}),('remove',{'participants':members[1:2]}),
        ('promote',{'participants':members[2:]}),('demote',{'participants':members[2:]})]
    for i,(change,group) in enumerate(changes):
        capture=group_capture(change,1700000100+i,**group)
        assert state.apply(capture)['outcome']=='created'
        # A redelivered or replayed capture is a duplicate, never a second row or event.
        assert state.apply(capture)['outcome']=='duplicate'
        n,metadata,events=wa_rows(store,capture['event']['messageId'])
        assert n==1 and events==[('message.created','live')]
        stored=metadata['metadata']
        assert stored['message_kind']=='group_change' and stored['is_service'] is True
        assert stored['group_change']=={'change':change,'actor':group.get('actor','333333@c.us'),
            'participants':group.get('participants',[]),'value':group.get('value')}
    chat_id=stable_id('chat','wa-groups',WA_GROUP)
    # The conversation read hides them like other service rows unless include_system is set.
    assert store.get_messages(key,chat_id,view='conversation')['items']==[]
    shown=store.get_messages(key,chat_id,view='conversation',include_system=True)['items']
    assert len(shown)==len(changes) and {m['message_kind'] for m in shown}=={'group_change'}
    assert wa_chat(store)['title']=='Родители 5Б'


# spec: ANYM-869
def test_an_older_synced_rename_never_overwrites_the_newer_group_title(wa_groups):
    state,store,_=wa_groups
    assert state.apply(group_capture('rename',1700000200,value='Новое имя'))['outcome']=='created'
    assert wa_chat(store)['title']=='Новое имя'
    # A history stub of an earlier rename arrives later: its row is kept, the title is not rolled back.
    assert state.apply(group_capture('rename',1700000100,origin='backfill',value='Старое имя'))['outcome']=='created'
    assert wa_chat(store)['title']=='Новое имя'
    assert state.apply(group_capture('rename',1700000300,value='Ещё новее'))['outcome']=='created'
    assert wa_chat(store)['title']=='Ещё новее'
    # A scan page or a dialog list read before the rename carries the old title: neither rewinds it (Astra r1 M1).
    at=lambda unix:datetime.fromtimestamp(unix,timezone.utc)
    assert state.apply(wa_capture('false_'+WA_GROUP+'_PAGE',1700000050,body='старое сообщение',origin='reconcile',
        title='Новое имя'))['outcome']=='created'
    state.inventory([(WA_GROUP,'chat','Старое имя','group')],None,listed_at=at(1700000250))
    assert wa_chat(store)['title']=='Ещё новее'
    # A rename that was never captured (session down) still arrives: a dialog list read after the newest
    # rename row, then a page message sent after it, name the group again (orchestrator fix-diff read).
    state.inventory([(WA_GROUP,'chat','Пропущенное имя','group')],None,listed_at=at(1700000400))
    assert wa_chat(store)['title']=='Пропущенное имя'
    assert state.apply(wa_capture('false_'+WA_GROUP+'_LATER',1700000500,body='новое сообщение',origin='reconcile',
        title='Имя со страницы'))['outcome']=='created'
    assert wa_chat(store)['title']=='Имя со страницы'
    # A group with no rename or join row keeps today's behaviour: the page and the dialog list name it.
    other='120363000000000010@g.us'
    state.apply(wa_capture('false_'+other+'_A',1700000050,body='привет',chat=other,title='Из списка чатов'))
    assert wa_chat(store,other)['title']=='Из списка чатов'
    state.inventory([(other,'chat','Новое из списка','group')],None)
    assert wa_chat(store,other)['title']=='Новое из списка'


# spec: ANYM-869
def test_a_joined_group_lists_the_chat_with_its_title_and_members_at_once(wa_groups):
    state,store,key=wa_groups
    assert wa_chat(store) is None
    members=sorted(f'{800000+i}@c.us' for i in range(5))
    capture=group_capture('joined_group',1700000400,value='Кружок робототехники',participants=members)
    assert state.apply(capture)['outcome']=='created'
    chat=wa_chat(store)
    assert chat['title']=='Кружок робототехники' and chat['chat_type']=='group'
    _,metadata,events=wa_rows(store,capture['event']['messageId'])
    assert metadata['metadata']['group_change']['participants']==members and events==[('message.created','live')]


# spec: ANYM-869
def test_group_changes_wake_no_default_subscription_but_a_joined_group_and_other_rows_still_do(wa_groups):
    from messaging_service.subscriptions import match_subscriptions
    state,store,key=wa_groups
    state.apply(wa_capture('false_'+WA_GROUP+'_HELLO',1700000000,body='первое сообщение'))
    chat_id=stable_id('chat','wa-groups',WA_GROUP)
    sid=store.create_subscription(key,[chat_id])['subscription_id']
    members=[f'{900000+i}@c.us' for i in range(300)]
    state.apply(group_capture('joined_group',1700000500,value='Школа',participants=members))
    for i in range(50):
        state.apply(group_capture('add',1700000600+i,participants=[f'{910000+i}@c.us']))
    # Rows that are no group change keep today's behaviour: a bare notification and a text message wake as before.
    state.apply(wa_capture('false_'+WA_GROUP+'_NOTE',1700000700,kind='notification'))
    state.apply(wa_capture('false_'+WA_GROUP+'_TEXT',1700000701,body='второе сообщение'))
    match_subscriptions(store)
    items=store.get_subscription_events(key,sid,limit=100)['items']
    with store.pool.connection() as c:
        woke=[c.execute('''SELECT m.provider_id FROM events e JOIN messages m ON m.id=e.message WHERE e.id=%s''',
            (item['event_id'],)).fetchone()['provider_id'] for item in items]
    assert woke==[f'group_{WA_GROUP}_1700000500_joined_group','false_'+WA_GROUP+'_NOTE','false_'+WA_GROUP+'_TEXT']


WA_CALLER='444444@c.us'


def call_capture(call_id,at,outcome,*,chat=WA_CALLER,media='audio',ended=None,origin='live'):
    """The whatsmeow call entry (calls.go callEnvelope), synthetic only."""
    capture=wa_capture('call_'+call_id,at,kind='call',actor=WA_CALLER if chat.endswith('@g.us') else chat,chat=chat,origin=origin)
    capture['event']['call']={'call_id':call_id,'direction':'incoming','media':media,'group':chat.endswith('@g.us'),
        'started_at':at,'ended_at':ended,'outcome':outcome}
    return capture


def wa_call(store,provider_id,chat=WA_CALLER):
    mid=stable_id('msg','wa-groups',chat,provider_id)
    with store.pool.connection() as c:
        return (c.execute('SELECT count(*) AS n,min(sender) AS sender FROM messages WHERE id=%s',(mid,)).fetchone(),
            c.execute('SELECT metadata FROM message_metadata WHERE message=%s',(mid,)).fetchone(),
            [(e['kind'],e['origin']) for e in c.execute('SELECT kind,origin FROM events WHERE message=%s ORDER BY id',(mid,))])


# spec: ANYM-923
def test_a_whatsapp_call_is_one_service_row_in_the_callers_chat_with_one_live_event(wa_groups):
    state,store,key=wa_groups
    capture=call_capture('CALL1',1700001000,'missed',ended=1700001030)
    assert state.apply(capture)['outcome']=='created'
    # A redelivered or replayed capture is a duplicate, never a second row or event.
    assert state.apply(capture)['outcome']=='duplicate'
    row,metadata,events=wa_call(store,'call_CALL1')
    assert row['n']==1 and row['sender']==WA_CALLER and events==[('message.created','live')]
    stored=metadata['metadata']
    assert stored['message_kind']=='call' and stored['is_service'] is True
    assert stored['call']=={'call_id':'CALL1','direction':'incoming','media':'audio','group':False,
        'started_at':'2023-11-14T22:30:00+00:00','ended_at':'2023-11-14T22:30:30+00:00','outcome':'missed'}
    # A group call sits in the group with its caller as sender; an unknown end and media stay null.
    assert state.apply(call_capture('CALLG',1700001100,'unknown',chat=WA_GROUP,media=None))['outcome']=='created'
    row,metadata,_=wa_call(store,'call_CALLG',WA_GROUP)
    assert row['sender']==WA_CALLER and metadata['metadata']['call']['group'] is True
    assert metadata['metadata']['call']['ended_at'] is None and metadata['metadata']['call']['media'] is None
    chat_id=stable_id('chat','wa-groups',WA_CALLER)
    # The conversation read hides it like other service rows unless include_system is set.
    assert store.get_messages(key,chat_id,view='conversation')['items']==[]
    shown=store.get_messages(key,chat_id,view='conversation',include_system=True)['items']
    assert [m['message_kind'] for m in shown]==['call']


# spec: ANYM-923
def test_a_call_block_the_archive_cannot_accept_is_refused_before_storage():
    from messaging_service.adapters import whatsapp_message
    for bad in ({'outcome':'declined'},{'direction':'outgoing'},{'media':'screen'},{'group':'no'},{'started_at':'x'},
            {'call_id':''},{'extra':1}):
        capture=call_capture('CALLBAD',1700001000,'missed')
        capture['event']['call'].update(bad)
        with pytest.raises(ValueError):whatsapp_message('wa-groups',capture['event'])


# spec: ANYM-923
def test_search_counts_missed_calls_per_person_by_kind_and_outcome(wa_groups):
    from messaging_service.store import APIError
    state,store,key=wa_groups
    for call_id,at,outcome in [('M1',1700002000,'missed'),('M2',1700002100,'missed'),('A1',1700002200,'answered'),
            ('R1',1700002300,'rejected')]:
        state.apply(call_capture(call_id,at,outcome,ended=at+30))
    state.apply(call_capture('OTHER',1700002400,'missed',chat='555555@c.us'))
    state.apply(wa_capture('false_'+WA_CALLER+'_TEXT',1700002500,body='перезвони',chat=WA_CALLER,actor=WA_CALLER))
    missed=store.search(key,sender=WA_CALLER,message_kinds=['call'],call_outcome='missed')['items']
    assert sorted(m['id'] for m in missed)==sorted(stable_id('msg','wa-groups',WA_CALLER,'call_'+i) for i in ('M1','M2'))
    # call_outcome alone narrows to calls; message_kinds alone lists every kind named.
    assert len(store.search(key,call_outcome='missed')['items'])==3
    assert len(store.search(key,sender=WA_CALLER,message_kinds=['call'])['items'])==4
    assert len(store.search(key,sender=WA_CALLER,message_kinds=['call','text'])['items'])==5
    for bad in (dict(call_outcome='declined'),dict(message_kinds=['video_call']),dict(message_kinds=['text'],call_outcome='missed'),
            dict(message_kinds='call')):
        with pytest.raises(APIError) as error:store.search(key,**bad)
        assert error.value.code=='invalid_filter'


# spec: ANYM-923
def test_a_missed_call_wakes_only_subscriptions_that_list_call(wa_groups):
    from messaging_service.subscriptions import match_subscriptions
    state,store,key=wa_groups
    state.apply(wa_capture('false_'+WA_CALLER+'_HELLO',1700003000,body='привет',chat=WA_CALLER,actor=WA_CALLER))
    chat_id=stable_id('chat','wa-groups',WA_CALLER)
    default=store.create_subscription(key,[chat_id])['subscription_id']
    calls=store.create_subscription(key,[chat_id],kinds=['call'])['subscription_id']
    both=store.create_subscription(key,[chat_id],kinds=['message.created','call'])['subscription_id']
    state.apply(call_capture('MISSED',1700003100,'missed',ended=1700003130))
    state.apply(call_capture('ANSWERED',1700003200,'answered',ended=1700003300))
    state.apply(wa_capture('false_'+WA_CALLER+'_TEXT',1700003400,body='перезвони',chat=WA_CALLER,actor=WA_CALLER))
    match_subscriptions(store)
    def woke(sid):
        items=store.get_subscription_events(key,sid,limit=100)['items']
        with store.pool.connection() as c:
            return [c.execute('SELECT m.provider_id FROM events e JOIN messages m ON m.id=e.message WHERE e.id=%s',
                (item['event_id'],)).fetchone()['provider_id'] for item in items]
    text='false_'+WA_CALLER+'_TEXT'
    assert woke(default)==[text]
    assert woke(calls)==['call_MISSED']
    assert woke(both)==['call_MISSED',text]


WA_POLL='false_'+WA_GROUP+'_POLL1_444444@c.us'


def poll_capture(options=('Да','Нет','Может быть'),mid=WA_POLL,block=None):
    """The whatsmeow poll creation envelope (polls.go pollBlock), synthetic only."""
    capture=wa_capture(mid,1700000000,body='Кто в субботу?',kind='poll_creation',actor='444444@c.us')
    capture['event']['isService']=False
    capture['event']['poll']=block or {'question':'Кто в субботу?','options':[{'index':i,'text':t} for i,t in enumerate(options)],
        'selectable_count':0,'truncated':False}
    return capture


def vote_capture(voter,indices,ms,*,origin='live',from_me=False,poll=WA_POLL):
    """The whatsmeow poll_vote journal entry (polls.go pollVoteEnvelope), synthetic only."""
    return {'kind':'poll_vote','account':'wa-groups','chat':WA_GROUP,'poll_provider_id':poll,'voter':voter,'from_me':from_me,
        'option_indices':indices,'voted_at':datetime.fromtimestamp(ms/1000,timezone.utc).isoformat(),
        'observed_at':'2026-11-09T10:00:00+00:00','origin':origin}


def poll_state(store,provider_id=WA_POLL):
    mid=stable_id('msg','wa-groups',WA_GROUP,provider_id)
    with store.pool.connection() as c:
        return (sorted((r['voter'],r['option_index']) for r in c.execute('SELECT voter,option_index FROM poll_voters WHERE message=%s',(mid,))),
            {r['option_index']:r['voters'] for r in c.execute('SELECT option_index,voters FROM poll_results WHERE message=%s',(mid,))},
            c.execute("SELECT count(*) AS n FROM events WHERE message=%s AND kind='poll.updated'",(mid,)).fetchone()['n'],
            c.execute("SELECT count(*) AS n FROM messages WHERE account='wa-groups'").fetchone()['n'])


# spec: ANYM-921
def test_a_whatsapp_vote_is_no_chat_message_and_sets_the_voters_rows_and_counts(wa_groups):
    state,store,_=wa_groups
    assert state.apply(poll_capture())['outcome']=='created'
    _,metadata,_=wa_rows(store,WA_POLL)
    assert metadata['metadata']['message_kind']=='poll' and metadata['metadata']['poll']['options'][2]=={'index':2,'text':'Может быть'}
    # Option 7 names nothing in a three-option poll: dropped.
    assert state.apply(vote_capture('555555@c.us',[0,2,7],1700000100000))['outcome']=='poll_updated'
    assert poll_state(store)==([('555555@c.us',0),('555555@c.us',2)],{0:1,1:0,2:1},1,1)
    assert state.apply(vote_capture('555555@c.us',[0],1700000100500,poll='false_'+WA_GROUP+'_NOPOLL_1@c.us'))['outcome']=='poll_unknown'
    assert poll_state(store)[3]==1
    # A poll block the archive cannot accept never quarantines the poll message itself.
    bad=poll_capture(mid='false_'+WA_GROUP+'_POLL2_444444@c.us',block={'question':'x','options':[{'index':5,'text':'a'}],
        'selectable_count':0,'truncated':False})
    assert state.apply(bad)['outcome']=='created'
    _,metadata,_=wa_rows(store,'false_'+WA_GROUP+'_POLL2_444444@c.us')
    assert metadata['metadata']['message_kind']=='poll' and 'poll' not in metadata['metadata']
    with pytest.raises(ValueError,match='invalid_poll_vote'):
        state.apply({**vote_capture('555555@c.us',[0],1700000100000),'option_indices':[0,0]})


# spec: ANYM-921
def test_a_newer_whatsapp_vote_replaces_the_voters_choice_an_older_one_is_ignored_and_an_empty_one_retracts(wa_groups):
    state,store,_=wa_groups
    state.apply(poll_capture())
    state.apply(vote_capture('666666@c.us',[2],1700000050000))
    assert state.apply(vote_capture('555555@c.us',[0],1700000100000))['outcome']=='poll_updated'
    assert state.apply(vote_capture('555555@c.us',[1],1700000300000))['outcome']=='poll_updated'
    assert poll_state(store)[:2]==([('555555@c.us',1),('666666@c.us',2)],{0:0,1:1,2:1})
    # Delivered late: older than the stored vote of the same voter.
    assert state.apply(vote_capture('555555@c.us',[0],1700000200000))['outcome']=='stale'
    assert poll_state(store)[:3]==([('555555@c.us',1),('666666@c.us',2)],{0:0,1:1,2:1},3)
    assert state.apply(vote_capture('555555@c.us',[],1700000400000))['outcome']=='poll_updated'
    assert poll_state(store)[:3]==([('666666@c.us',2)],{0:0,1:0,2:1},4)
    # The retraction is remembered: an older vote cannot bring the voter back.
    assert state.apply(vote_capture('555555@c.us',[1],1700000350000))['outcome']=='stale'
    assert poll_state(store)[:2]==([('666666@c.us',2)],{0:0,1:0,2:1})


# spec: ANYM-921
def test_a_whatsapp_poll_and_its_votes_from_history_add_no_duplicates_when_replayed(wa_groups):
    state,store,_=wa_groups
    synced=poll_capture();synced['origin']='backfill'
    assert state.apply(synced)['outcome']=='created'
    assert state.apply(poll_capture())['outcome']!='created'
    assert state.apply(vote_capture('555555@c.us',[1],1700000100000))['outcome']=='poll_updated'
    for _ in range(2):
        assert state.apply(vote_capture('555555@c.us',[1],1700000100000,origin='backfill'))['outcome']=='stale'
        assert state.apply(synced)['outcome']!='created'
    assert poll_state(store)==([('555555@c.us',1)],{0:0,1:1,2:0},1,1)


# spec: ANYM-921
def test_message_reads_show_a_whatsapp_polls_counts_voters_and_the_owners_choice(wa_groups):
    state,store,key=wa_groups
    state.apply(poll_capture())
    named=wa_capture('false_'+WA_GROUP+'_HI_555555@c.us',1700000010,body='привет',actor='555555@c.us')
    named['event']['senderName']='Синтетик Б'
    state.apply(named)
    state.apply(vote_capture('555555@c.us',[1],1700000100000))
    state.apply(vote_capture('666666@c.us',[1],1700000200000))
    state.apply(vote_capture('123456@c.us',[0],1700000300000,from_me=True))
    chat_id=stable_id('chat','wa-groups',WA_GROUP);mid=stable_id('msg','wa-groups',WA_GROUP,WA_POLL)
    reads={'archive':store.get_messages(key,chat_id)['items'],'conversation':store.get_messages(key,chat_id,view='conversation')['items'],
        'search':store.search(key,'субботу',chat=chat_id)['items'],'context':[store.get_context(key,mid)['message']]}
    for name,items in reads.items():
        poll=next(i for i in items if i['id']==mid)['poll']
        assert [(o['text'],o['voters']) for o in poll['options']]==[('Да',1),('Нет',2),('Может быть',0)],name
        assert poll['options'][1]['voted_by']==[{'sender':'666666@c.us','name':None},{'sender':'555555@c.us','name':'Синтетик Б'}],name
        assert poll['options'][2]=={'index':2,'text':'Может быть','voters':0,'voted_by':[],'voted_by_truncated':False},name
        assert poll['total_voters']==3 and poll['chosen']==[0] and poll['results_as_of'] and poll['question']=='Кто в субботу?',name
        assert all('poll' not in i for i in items if i['id']!=mid),name
    # 60 voters of one option: 50 listed, the rest counted.
    for n in range(60):state.apply(vote_capture(f'7000{n:02d}@c.us',[2],1700000400000+n))
    option=store.get_context(key,mid)['message']['poll']['options'][2]
    assert option['voters']==60 and len(option['voted_by'])==50 and option['voted_by_truncated'] is True
    # A key without read on the chat gets nothing.
    Archive(store).register('wa-other','whatsapp','Synthetic other',discover=True)
    other=store.issue_key('wa-other-reader',['wa-other'],permissions=['read'],key_permissions=['read'])
    from messaging_service.store import APIError
    with pytest.raises(APIError):store.get_context(other,mid)
    assert store.search(other,'субботу')['items']==[]


# spec: ANYM-922
def test_message_reads_return_a_shared_whatsapp_location_and_never_its_live_track(wa_groups):
    state,store,key=wa_groups
    point={'latitude':55.755826,'longitude':37.6173,'accuracy_m':None,'title':'Парк Синтетик','address':'ул. Тестовая 1','live':False}
    shared=wa_capture('false_'+WA_GROUP+'_LOC1_444444@c.us',1700000000,body='Парк Синтетик',kind='location',actor='444444@c.us')
    shared['event'].update(location=point,isService=False)
    live=wa_capture('false_'+WA_GROUP+'_LOC2_444444@c.us',1700000010,body='иду',kind='location',actor='444444@c.us')
    live['event'].update(location={'live':True},isService=False)
    state.apply(wa_capture('false_'+WA_GROUP+'_HI_555555@c.us',1700000020,body='Парк Синтетик рядом',actor='555555@c.us'))
    subscription=store.create_subscription(key,[stable_id('chat','wa-groups',WA_GROUP)])['subscription_id']
    state.apply(shared);state.apply(live)
    chat_id=stable_id('chat','wa-groups',WA_GROUP)
    mid,lid=(stable_id('msg','wa-groups',WA_GROUP,'false_'+WA_GROUP+f'_LOC{n}_444444@c.us') for n in (1,2))
    reads={'archive':store.get_messages(key,chat_id)['items'],'conversation':store.get_messages(key,chat_id,view='conversation')['items'],
        'search':store.search(key,'Синтетик',chat=chat_id)['items'],'context':[store.get_context(key,mid)['message']]}
    for name,items in reads.items():
        assert next(i for i in items if i['id']==mid)['location']==point,name
        assert all('location' not in i for i in items if i['id'] not in (mid,lid)),name
    assert next(i for i in reads['archive'] if i['id']==lid)['location']=={'live':True}
    # Subscription references carry IDs and kinds, never the place.
    from messaging_service.subscriptions import match_subscriptions
    match_subscriptions(store)
    references=store.get_subscription_events(key,subscription,limit=100)['items']
    assert len(references)>=2 and '55.75' not in json.dumps(references,default=str,ensure_ascii=False) and 'Тестовая' not in json.dumps(references,default=str,ensure_ascii=False)
    # A deleted message keeps no place.
    gone=wa_capture('false_'+WA_GROUP+'_LOC1_444444@c.us',1700000000,kind='location',actor='444444@c.us')
    gone['event']['isService']=False;gone.update(change='delete');state.apply(gone)
    assert 'location' not in store.get_context(key,mid)['message']
    # A key without read on the chat gets nothing.
    Archive(store).register('wa-other','whatsapp','Synthetic other',discover=True)
    other=store.issue_key('wa-other-reader',['wa-other'],permissions=['read'],key_permissions=['read'])
    from messaging_service.store import APIError
    with pytest.raises(APIError):store.get_context(other,lid)
    assert store.search(other,'Синтетик')['items']==[]


WA_EVENT='false_'+WA_GROUP+'_EVT1_444444@c.us'
SYNTHETIC_EVENT={'name':'Шашлыки','description':'Берите пледы','start':'2099-06-01T10:00:00Z','end':'2099-06-01T12:00:00Z',
    'location':{'title':'Парк','address':'ул. Синтетическая 1','latitude':55.75,'longitude':37.61},
    'join_link':'https://call.whatsapp.com/video/synthetic','canceled':False,'is_call':False}


def event_capture(block=None,*,mid=WA_EVENT,at=1700000000,responses=None,edited=None):
    """The whatsmeow event envelope (events.go eventBlock, withEventResponses), synthetic only."""
    block=deepcopy(SYNTHETIC_EVENT if block is None else block)
    capture=wa_capture(mid,at,body=block['name'] if isinstance(block,dict) and isinstance(block.get('name'),str) else 'Шашлыки',kind='event',actor='444444@c.us')
    capture['event'].update(isService=False,event=block)
    if responses is not None:capture['event']['eventResponses']=responses
    if edited is not None:capture['event']['editedTimestampMs']=edited;capture['change']='edit'
    return capture


# spec: ANYM-924
def test_a_whatsapp_event_keeps_its_block_and_an_invalid_block_costs_only_the_block(caplog):
    stored=validate_capture(event_capture(responses={'going':2,'not_going':0,'maybe':1}))['envelope']
    assert stored['body']=='Шашлыки' and stored['metadata']['message_kind']=='event' and not stored['metadata']['is_service']
    assert stored['metadata']['event']==SYNTHETIC_EVENT and stored['metadata']['event_responses']=={'going':2,'not_going':0,'maybe':1}
    for bad in ({**SYNTHETIC_EVENT,'start':'tomorrow'},{**SYNTHETIC_EVENT,'end':'2099-06-01T09:00:00Z'},{**SYNTHETIC_EVENT,'name':'я'*257},
            {**SYNTHETIC_EVENT,'description':'о'*2049},{**SYNTHETIC_EVENT,'location':{'title':'Парк','latitude':91}},
            {**SYNTHETIC_EVENT,'canceled':'yes'},{**SYNTHETIC_EVENT,'extra':1},{**SYNTHETIC_EVENT,'join_link':'x\x00'},'Шашлыки'):
        caplog.clear()
        with caplog.at_level('WARNING'):
            kept=validate_capture(event_capture(bad))['envelope']
        assert 'event' not in kept['metadata'] and kept['metadata']['message_kind']=='event',bad
        assert 'invalid_event' in caplog.text and 'Шашлыки' not in caplog.text and 'Синтетическая' not in caplog.text
    # Counts the archive cannot accept are dropped too; the event stays.
    kept=validate_capture(event_capture(responses={'going':-1,'not_going':0,'maybe':0}))['envelope']
    assert 'event_responses' not in kept['metadata'] and kept['metadata']['event']==SYNTHETIC_EVENT
    # Only an event carries the block, and a deleted event keeps none.
    text=wa_capture('false_'+WA_GROUP+'_TXT_444444@c.us',1700000000,body='x',actor='444444@c.us')
    text['event'].update(event=SYNTHETIC_EVENT)
    assert 'event' not in validate_capture(text)['envelope']['metadata']
    gone=event_capture(responses={'going':1,'not_going':0,'maybe':0});gone['change']='delete'
    assert not {'event','event_responses'}&set(validate_capture(gone)['envelope']['metadata'])


# spec: ANYM-924
def test_message_reads_return_a_whatsapp_event_with_a_calendar_suggestion_and_no_calendar_write(wa_groups,monkeypatch):
    import socket
    import urllib.request
    requests=[]
    def refuse(*args,**kwargs):
        requests.append(args)
        raise OSError('no network in this test')
    # Capture, reads, the cancellation and the subscription match make no request to any service, the calendar included.
    monkeypatch.setattr(socket.socket,'connect',refuse);monkeypatch.setattr(socket,'create_connection',refuse)
    monkeypatch.setattr(urllib.request,'urlopen',refuse)
    from messaging_service.subscriptions import match_subscriptions
    state,store,key=wa_groups
    chat_id=stable_id('chat','wa-groups',WA_GROUP)
    state.apply(wa_capture('false_'+WA_GROUP+'_HI_555555@c.us',1699999990,body='привет',actor='555555@c.us'))
    subscription=store.create_subscription(key,[chat_id])['subscription_id']
    state.apply(event_capture(responses={'going':1,'not_going':0,'maybe':2}))
    past={**SYNTHETIC_EVENT,'name':'Прошедшее','start':'2020-01-01T10:00:00Z','end':None,'location':None,'description':None}
    state.apply(event_capture(past,mid='false_'+WA_GROUP+'_EVT2_444444@c.us',at=1700000010))
    mid,pid=(stable_id('msg','wa-groups',WA_GROUP,'false_'+WA_GROUP+f'_EVT{n}_444444@c.us') for n in (1,2))
    # The calendar service's schedule call takes title, start and duration (minutes); the rest is context to show.
    suggestion={'title':'Шашлыки','start':'2099-06-01T10:00:00Z','duration':120,'end':'2099-06-01T12:00:00Z',
        'location_text':'Парк, ул. Синтетическая 1','description':'Берите пледы','source_message':mid}
    reads={'archive':store.get_messages(key,chat_id)['items'],'conversation':store.get_messages(key,chat_id,view='conversation')['items'],
        'search':store.search(key,'Шашлыки',chat=chat_id)['items'],'context':[store.get_context(key,mid)['message']]}
    for name,items in reads.items():
        row=next(i for i in items if i['id']==mid)
        assert row['event']==SYNTHETIC_EVENT and row['calendar_suggestion']==suggestion,name
        assert row['event_responses']=={'going':1,'not_going':0,'maybe':2},name
    assert next(i for i in reads['conversation'] if i['id']==mid)['kind']=='event'
    # An event in the past keeps its block and gets no suggestion.
    old=next(i for i in reads['archive'] if i['id']==pid)
    assert old['event']==past and 'calendar_suggestion' not in old and 'event_responses' not in old
    # An event wakes subscriptions like a message; its reference names the kind, never the place or text.
    match_subscriptions(store)
    references=store.get_subscription_events(key,subscription,limit=100)['items']
    assert [r['metadata']['message_kind'] for r in references]==['event','event']
    dump=json.dumps(references,default=str,ensure_ascii=False)
    assert 'Синтетическая' not in dump and 'Берите' not in dump and 'event_responses' not in dump
    # A cancellation (an edit of the event) updates it and withdraws the suggestion.
    state.apply(event_capture({**SYNTHETIC_EVENT,'end':None,'canceled':True},edited=1700000100000,responses={'going':1,'not_going':1,'maybe':2}))
    row=store.get_context(key,mid)['message']
    assert row['event']=={**SYNTHETIC_EVENT,'end':None,'canceled':True} and 'calendar_suggestion' not in row
    assert row['event_responses']=={'going':1,'not_going':1,'maybe':2}
    assert requests==[]


# spec: ANYM-921
def test_poll_updates_wake_only_a_subscription_that_lists_them(wa_groups):
    from messaging_service.subscriptions import match_subscriptions
    state,store,key=wa_groups
    state.apply(poll_capture())
    chat_id=stable_id('chat','wa-groups',WA_GROUP)
    default=store.create_subscription(key,[chat_id])['subscription_id']
    listed=store.create_subscription(key,[chat_id],kinds=['poll.updated'])['subscription_id']
    for n in range(30):
        assert state.apply(vote_capture('555555@c.us',[n%2],1700000100000+n))['outcome']=='poll_updated'
    match_subscriptions(store)
    assert store.get_subscription_events(key,default,limit=100)['items']==[]
    items=store.get_subscription_events(key,listed,limit=100)['items']
    assert len(items)==30 and {i['kind'] for i in items}=={'poll.updated'}


TG_POLL_CHAT='-500'
# One send time per test (tg_polls sets it): two captures of one message taken a second boundary apart would carry
# two send times, a different message version, and ingest rightly calls that a conflict (merge-queue flake, #850).
TG_POLL_SENT={}


@pytest.fixture
def tg_polls(seeded,tmp_path,monkeypatch):
    monkeypatch.setenv('ANYMSG_CAPTURE_TELEGRAM_POLLS','1')
    monkeypatch.setitem(TG_POLL_SENT,'at',datetime.now(timezone.utc).replace(microsecond=0))
    store,_,_=seeded; archive=Archive(store)
    archive.register('tg-polls','telegram','Synthetic Telegram polls',discover=True)
    worker=TelegramCollector(archive,'tg-polls',FakeProvider(),CaptureSpool(tmp_path/'polls',reserve_bytes=0),
        writer=BlobWriter(store.files,reserve_bytes=0),owner='fixture-owner')
    epoch=archive.acquire('tg-polls','fixture-owner')
    worker.state=CollectorState(archive,'tg-polls',('fixture-owner',epoch));worker.policy=worker.state.policy()
    key=store.issue_key('tg-polls-reader',['tg-polls'],permissions=['read','subscription-write'],
        key_permissions=['read','subscription-write'])
    return worker,store,key


def tg_poll_results(voters,*,total=None,chosen=(),minimal=False):
    from telethon import types
    return types.PollResults(min=minimal or None,total_voters=sum(voters) if total is None else total,
        results=None if voters is None else [types.PollAnswerVoters(option=str(i).encode(),voters=v,chosen=(i in chosen) or None)
            for i,v in enumerate(voters)])


def tg_poll_message(mid=30,*,options=('Да','Нет','Может быть'),voters=(2,1,0),public=False,poll_id=777,
        question='Кто в субботу?',chosen=(),results=None,sent=None,group=500):
    """A synthetic Telegram group message carrying a MessageMediaPoll (Telethon 1.45.0 layer types)."""
    from telethon import types
    poll=types.Poll(id=poll_id,question=types.TextWithEntities(text=question,entities=[]),hash=0,
        answers=[types.PollAnswer(text=types.TextWithEntities(text=t,entities=[]),option=str(i).encode()) for i,t in enumerate(options)],
        public_voters=public or None,multiple_choice=None,quiz=None,closed=None)
    return types.Message(id=mid,peer_id=types.PeerChat(group),from_id=types.PeerUser(42),
        date=sent or TG_POLL_SENT.get('at') or datetime.now(timezone.utc).replace(microsecond=0),message='',
        media=types.MessageMediaPoll(poll=poll,results=results or tg_poll_results(voters,chosen=chosen)))


def tg_poll_capture(*args,**kwargs):
    from messaging_service.adapters import telegram_message
    return {'kind':'message','peer_kind':'chat','envelope':telegram_message('tg-polls',tg_poll_message(*args,**kwargs))}


def tg_poll_update(voters,at,*,poll_id=777,chosen=(),minimal=True,total=None):
    from telethon import types
    from messaging_service.telegram_polls import poll_update_capture
    update=types.UpdateMessagePoll(poll_id=poll_id,results=tg_poll_results(voters,chosen=chosen,minimal=minimal,total=total))
    return poll_update_capture('tg-polls',update,at.isoformat())


def tg_poll_state(store,mid=30):
    message=stable_id('msg','tg-polls',TG_POLL_CHAT,str(mid))
    with store.pool.connection() as c:
        counts={r['option_index']:r['voters'] for r in c.execute('SELECT option_index,voters FROM poll_results WHERE message=%s',(message,))}
        row=c.execute('SELECT provider_poll_id,option_keys,total_voters,chosen,votes_read_at FROM telegram_polls WHERE message=%s',(message,)).fetchone()
        events=c.execute("SELECT count(*) AS n FROM events WHERE message=%s AND kind='poll.updated'",(message,)).fetchone()['n']
        voters=sorted((r['voter'],r['option_index']) for r in c.execute('SELECT voter,option_index FROM poll_voters WHERE message=%s',(message,)))
    return counts,row,events,voters


# spec: ANYM-920
def test_a_telegram_poll_is_captured_with_its_question_options_flags_and_counts(tg_polls,caplog):
    w,store,_=tg_polls
    capture=tg_poll_capture(public=True)
    assert capture['envelope']['metadata']['poll']=={'provider_poll_id':'777','question':'Кто в субботу?',
        'options':[{'index':0,'text':'Да'},{'index':1,'text':'Нет'},{'index':2,'text':'Может быть'}],
        'multiple_choice':False,'public_voters':True,'quiz':False,'closed':False,'truncated':False}
    assert w.state.apply(capture)['outcome']=='created'
    message=stable_id('msg','tg-polls',TG_POLL_CHAT,'30')
    with store.pool.connection() as c:
        stored=c.execute('SELECT metadata FROM message_metadata WHERE message=%s',(message,)).fetchone()['metadata']
        receipt=c.execute("SELECT envelope FROM ingest_receipts WHERE account='tg-polls' AND envelope->>'provider_id'='30'").fetchone()['envelope']
    # The counts are never message metadata, a receipt or a revision: they live in the poll tables.
    assert stored['message_kind']=='poll' and stored['poll']['question']=='Кто в субботу?' and 'poll_results' not in stored
    assert 'poll_results' not in receipt['metadata']
    counts,row,events,_=tg_poll_state(store)
    assert counts=={0:2,1:1,2:0} and row['provider_poll_id']=='777' and row['option_keys']==['30','31','32']
    assert row['total_voters']==3 and events==0
    # The same caps as the live read: 12 options of 256 characters, a 512-character question, `truncated`.
    long=tg_poll_capture(31,options=[f'option {i} '+'x'*300 for i in range(14)],voters=[1]*14,question='q'*600)
    poll=long['envelope']['metadata']['poll']
    assert len(poll['options'])==12 and len(poll['options'][0]['text'])==256 and len(poll['question'])==512 and poll['truncated']
    assert w.state.apply(long)['outcome']=='created' and tg_poll_state(store,31)[0]=={i:1 for i in range(12)}
    # A poll block the archive cannot read is dropped with its reason code; the message is still stored.
    from telethon import types
    from messaging_service.adapters import telegram_message
    broken=tg_poll_message(32);broken.media=types.MessageMediaPoll(poll=types.Poll(id=778,hash=0,
        question=types.TextWithEntities(text='q',entities=[]),answers=[types.PollAnswer(
            text=types.TextWithEntities(text='a',entities=[]),option=b'x'*101)]),results=tg_poll_results([0]))
    with caplog.at_level('WARNING',logger='messaging_service.telegram_polls'):
        envelope=telegram_message('tg-polls',broken)
    assert 'poll_block_dropped: invalid_metadata' in caplog.text
    assert envelope['metadata']['message_kind']=='poll' and 'poll' not in envelope['metadata']
    assert w.state.apply({'kind':'message','peer_kind':'chat','envelope':envelope})['outcome']=='created'
    from messaging_service.envelope import validate
    with pytest.raises(ValueError,match='invalid_metadata'):
        validate({**capture['envelope'],'metadata':{**capture['envelope']['metadata'],
            'poll_results':{**capture['envelope']['metadata']['poll_results'],'voters':[1]}}})


# spec: ANYM-920
def test_telegram_poll_updates_replace_the_counts_and_an_older_observation_never_wins(tg_polls):
    w,store,_=tg_polls
    # One message date for the capture and its replay: a replay dated a second later is other content (a conflict).
    sent=datetime.now(timezone.utc).replace(microsecond=0)
    w.state.apply(tg_poll_capture(sent=sent))
    base=datetime.now(timezone.utc)
    from datetime import timedelta
    newer,older=tg_poll_update((3,1,0),base+timedelta(minutes=2)),tg_poll_update((0,0,5),base+timedelta(minutes=1))
    assert w.state.apply(newer)['outcome']=='poll_updated'
    assert w.state.apply(older)['outcome']=='stale'
    counts,row,events,_=tg_poll_state(store)
    assert counts=={0:3,1:1,2:0} and row['total_voters']==4 and events==1
    # The owner's own choice comes from a full (non-min) result; a min one keeps it.
    assert w.state.apply(tg_poll_update((3,2,0),base+timedelta(minutes=3),chosen=(1,),minimal=False))['outcome']=='poll_updated'
    assert w.state.apply(tg_poll_update((3,2,1),base+timedelta(minutes=4)))['outcome']=='poll_updated'
    assert tg_poll_state(store)[1]['chosen']==[1]
    # A replay of the capture (an older observation of its counts) changes nothing.
    assert w.state.apply(tg_poll_capture(sent=sent))['outcome'] in ('duplicate','unchanged') and tg_poll_state(store)[0]=={0:3,1:2,2:1}
    unknown=tg_poll_update((1,),base+timedelta(minutes=5),poll_id=999)
    assert w.state.apply(unknown)['outcome']=='poll_update_unknown' and w.state.poll_counts['poll_update_unknown']==1
    assert tg_poll_state(store)[2]==3
    with pytest.raises(ValueError,match='invalid_poll_update'):validate_capture({**newer,'poll_provider_id':'-1'})


class PollVotesProvider(FakeProvider):
    def __init__(self,pages=None,limited=()):
        super().__init__();self.vote_reads=[];self.pages=pages;self.limited=set(limited);self.complete=True
    async def poll_votes(self,chat,message_id,*,limit):
        from messaging_service.telegram_governor import ProviderRateLimited
        self.vote_reads.append((chat,message_id,limit))
        if message_id in self.limited:raise ProviderRateLimited(30)
        return (self.pages if self.pages is not None else [(42,'Синтетик А',[b'1']),(43,'Синтетик Б',[b'0',b'2'])]),self.complete


# spec: ANYM-920
def test_a_public_polls_voters_are_read_once_per_ten_minutes_and_an_anonymous_poll_never(tg_polls):
    w,store,_=tg_polls
    w.provider=PollVotesProvider()
    w.state.apply(tg_poll_capture(public=True))
    w.state.apply(tg_poll_capture(40,poll_id=888))  # anonymous
    base=datetime.now(timezone.utc)
    from datetime import timedelta
    for n in range(5):
        # Five pushed updates within one minute, a pass after each.
        w.state.apply(tg_poll_update((2+n,1,0),base+timedelta(seconds=10*n+1)))
        w.state.apply(tg_poll_update((n,0),base+timedelta(seconds=10*n+1),poll_id=888))
        asyncio.run(w.poll_voters_pass())
        w.spool.drain(w.state.apply,100)
    assert w.provider.vote_reads==[(TG_POLL_CHAT,30,200)]
    _,row,_,voters=tg_poll_state(store)
    assert voters==[('42',1),('43',0),('43',2)] and row['votes_read_at'] is not None
    assert tg_poll_state(store,40)[3]==[]
    # The throttle is the archive's too: a new collector after a restart does not read again within 10 minutes.
    fresh=TelegramCollector(w.archive,'tg-polls',w.provider,w.spool,writer=w.writer,owner='fixture-owner')
    fresh.state,fresh.policy=w.state,w.policy
    asyncio.run(fresh.poll_voters_pass())
    assert len(w.provider.vote_reads)==1


# spec: ANYM-920
def test_a_floodwait_stops_the_voter_pass_and_a_failed_pass_never_ends_the_collector(tg_polls):
    w,store,_=tg_polls
    w.provider=PollVotesProvider(limited={51})
    w.state.apply(tg_poll_capture(50,public=True,poll_id=901))
    w.state.apply(tg_poll_capture(51,public=True,poll_id=902,voters=(0,0,0)))
    asyncio.run(w.poll_voters_pass())
    # The newest counts go first; the FloodWait on it ends the pass, so the other poll waits for the next one.
    assert [read[1] for read in w.provider.vote_reads]==[51] and list(w.spool.path.glob('*.json'))==[]
    passes=[]
    async def failing(limit=None):
        passes.append(1)
        if len(passes)==2:w.stopping.set()
        raise RuntimeError('synthetic')
    w.poll_voters_pass=failing;w.poll_votes_interval=0
    asyncio.run(asyncio.wait_for(w.poll_voters_loop(),5))
    assert len(passes)==2 and not w.failed.is_set()


# spec: ANYM-920
def test_a_hung_poll_votes_page_is_cancelled_inside_the_governed_call_and_ends_only_the_pass(tg_polls,monkeypatch):
    # Astra r1 B1: a page that never completes must release the shared non-send lane, not hold it.
    from messaging_service import telegram_polls
    from messaging_service.telegram_collector import TelethonProvider
    monkeypatch.setattr(telegram_polls,'POLL_VOTES_TIMEOUT',0.05)
    lane=[]
    class Client:
        async def get_input_entity(self,peer):return peer
        async def __call__(self,request):await asyncio.Event().wait()
    provider=TelethonProvider.__new__(TelethonProvider);provider.client=Client()
    async def rpc(operation,phase,*,before=None,tier=None):
        lane.append('held')
        try:return await operation()
        finally:lane.append('released')
    provider.rpc=rpc
    with pytest.raises(TimeoutError):
        asyncio.run(asyncio.wait_for(provider.poll_votes(TG_POLL_CHAT,30,limit=200),2))
    assert lane==['held','released']
    w,store,_=tg_polls
    class Hung(PollVotesProvider):
        async def poll_votes(self,chat,message_id,*,limit):
            self.vote_reads.append(message_id);raise TimeoutError
    w.provider=Hung()
    w.state.apply(tg_poll_capture(60,public=True,poll_id=960))
    w.state.apply(tg_poll_capture(61,public=True,poll_id=961))
    asyncio.run(w.poll_voters_pass())
    assert w.provider.vote_reads==[61] and not w.failed.is_set() and list(w.spool.path.glob('*.json'))==[]


# spec: ANYM-920
def test_a_database_outage_while_applying_captured_poll_counts_keeps_the_capture_for_its_replay(tg_polls,monkeypatch):
    # Astra r1 B2: the poll's mapping and counts must not be acknowledged as stored when their transaction failed.
    import psycopg
    from messaging_service import polls
    w,store,_=tg_polls
    real=polls._store_counts
    def outage(*args):raise psycopg.OperationalError('synthetic outage')
    monkeypatch.setattr(polls,'_store_counts',outage)
    capture=tg_poll_capture()
    with pytest.raises(psycopg.OperationalError):w.state.apply(capture)
    assert tg_poll_state(store)[1] is None
    monkeypatch.setattr(polls,'_store_counts',real)
    # The drain keeps the item and replays it: a duplicate receipt, and the counts arrive.
    assert w.state.apply(capture)['outcome']=='duplicate'
    counts,row,_,_=tg_poll_state(store)
    assert counts=={0:2,1:1,2:0} and row['provider_poll_id']=='777'


# spec: ANYM-920
def test_a_failing_poll_report_line_never_ends_the_voter_loop(tg_polls,monkeypatch):
    # Astra r1 B3: the loop's own stdout line is best effort.
    from messaging_service import telegram_polls
    w,_,_=tg_polls
    passes=[]
    async def counting(limit=None):
        passes.append(1);w.state.poll_counts={'poll_update_unknown':1}
        if len(passes)==3:w.stopping.set()
    def broken(*args,**kwargs):raise BrokenPipeError()
    monkeypatch.setattr(telegram_polls,'print',broken,raising=False)
    w.poll_voters_pass=counting;w.poll_votes_interval=0
    asyncio.run(asyncio.wait_for(w.poll_voters_loop(),5))
    assert len(passes)==3 and not w.failed.is_set()


# spec: ANYM-920
def test_telegram_poll_capture_stays_off_until_its_flag_so_a_rollback_target_never_quarantines_a_message(tg_polls,monkeypatch):
    # Astra r1 B4: with the kill switch ANYMSG_CAPTURE_TELEGRAM_POLLS=0 the envelope is the one a schema-83 collector accepts.
    from telethon import types
    from messaging_service.envelope import validate
    w,store,_=tg_polls
    monkeypatch.setenv('ANYMSG_CAPTURE_TELEGRAM_POLLS','0')
    capture=tg_poll_capture(public=True)
    metadata=capture['envelope']['metadata']
    assert metadata['message_kind']=='poll' and 'poll' not in metadata and 'poll_results' not in metadata
    assert validate(capture['envelope'])==capture['envelope']
    asyncio.run(w.poll_update(types.UpdateMessagePoll(poll_id=777,results=tg_poll_results((1,0,0)))))
    w.provider=PollVotesProvider()
    asyncio.run(w.poll_voters_pass())
    assert list(w.spool.path.glob('*.json'))==[] and w.provider.vote_reads==[]
    monkeypatch.setenv('ANYMSG_CAPTURE_TELEGRAM_POLLS','1')
    asyncio.run(w.poll_update(types.UpdateMessagePoll(poll_id=777,results=tg_poll_results((1,0,0)))))
    assert len(list(w.spool.path.glob('*.json')))==1


# spec: ANYM-920
def test_telegram_poll_capture_is_on_by_default_once_schema_84_is_the_rollback_target(tg_polls,monkeypatch):
    # Schema 84 is deployed and is the rollback base: an unset flag captures the poll block and pushed counts.
    from telethon import types
    from messaging_service.envelope import validate
    w,_,_=tg_polls
    monkeypatch.delenv('ANYMSG_CAPTURE_TELEGRAM_POLLS')
    capture=tg_poll_capture(public=True)
    metadata=capture['envelope']['metadata']
    assert metadata['message_kind']=='poll' and metadata['poll']['question'] and 'poll_results' in metadata
    assert validate(capture['envelope'])==capture['envelope']
    asyncio.run(w.poll_update(types.UpdateMessagePoll(poll_id=777,results=tg_poll_results((1,0,0)))))
    assert len(list(w.spool.path.glob('*.json')))==1


# spec: ANYM-920
def test_voter_reads_follow_the_policy_and_one_read_serves_every_forwarded_copy(tg_polls):
    # Astra r1 M1 (an excluded chat is never read) and M3 (the throttle is the poll ID's, not one message's).
    from messaging_service.adapters import telegram_message
    w,store,_=tg_polls
    w.provider=PollVotesProvider()
    w.state.apply(tg_poll_capture(public=True))
    forwarded=tg_poll_message(70,public=True,group=600)
    w.state.apply({'kind':'message','peer_kind':'chat','envelope':telegram_message('tg-polls',forwarded)})
    asyncio.run(w.poll_voters_pass());w.spool.drain(w.state.apply,100)
    assert len(w.provider.vote_reads)==1
    copy=stable_id('msg','tg-polls','-600','70')
    with store.pool.connection() as c:
        assert sorted(r['voter'] for r in c.execute('SELECT DISTINCT voter FROM poll_voters WHERE message=%s',(copy,)))==['42','43']
        assert c.execute('SELECT count(*) AS n FROM telegram_polls WHERE votes_read_at IS NOT NULL').fetchone()['n']==2
        c.execute("UPDATE account_policies SET exclude_chats=ARRAY['-500','-600'] WHERE account='tg-polls'")
        c.execute("UPDATE telegram_polls SET votes_read_at=now()-interval '11 minutes',results_at=now()")
    w.policy=w.state.policy();w._poll_reads.clear()
    asyncio.run(w.poll_voters_pass())
    assert len(w.provider.vote_reads)==1 and list(w.spool.path.glob('*.json'))==[]


# spec: ANYM-920
def test_a_late_message_snapshot_never_undoes_a_newer_pushed_count_and_a_bare_one_keeps_the_owners_choice(tg_polls):
    # Astra r1 M2 (a snapshot inside the margin is stale), M5 (results without options keep `chosen`), m1.
    from datetime import timedelta
    from telethon import types
    w,store,key=tg_polls
    w.state.apply(tg_poll_capture(chosen=(1,)))
    pushed=datetime.now(timezone.utc)+timedelta(seconds=1)
    assert w.state.apply(tg_poll_update((5,5,5),pushed))['outcome']=='poll_updated'
    late=tg_poll_capture(voters=(0,0,1))
    assert w.state.apply(late)['outcome'] in ('duplicate','unchanged') and tg_poll_state(store)[0]=={0:5,1:5,2:5}
    from messaging_service import polls
    bare=tg_poll_capture(results=types.PollResults(total_voters=16))
    bare['envelope']['metadata']['poll_results']['observed_at']=(pushed+timedelta(minutes=5)).isoformat()
    w.state.apply(bare)
    counts,row,_,_=tg_poll_state(store)
    assert counts=={0:5,1:5,2:5} and row['total_voters']==16 and row['chosen']==[1]
    mid=stable_id('msg','tg-polls',TG_POLL_CHAT,'30')
    poll=store.get_context(key,mid)['message']['poll']
    assert datetime.fromisoformat(poll['results_as_of'])==pushed+timedelta(minutes=5) and polls.SNAPSHOT_MARGIN==timedelta(minutes=2)


# spec: ANYM-920
def test_an_unread_or_capped_public_voter_list_marks_options_with_unseen_voters_truncated(tg_polls):
    # Astra r1 M4: 200 fetched voters of option 0 and unseen voters of option 1 are not a complete list.
    w,store,key=tg_polls
    w.provider=PollVotesProvider(pages=[(2000+n,f'Синтетик {n}',[b'0']) for n in range(200)])
    w.provider.complete=False
    w.state.apply(tg_poll_capture(public=True,voters=(200,7,0)))
    mid=stable_id('msg','tg-polls',TG_POLL_CHAT,'30')
    before=store.get_context(key,mid)['message']['poll']['options']
    assert [o['voted_by_truncated'] for o in before]==[True,True,False]
    asyncio.run(w.poll_voters_pass());w.spool.drain(w.state.apply,100)
    options=store.get_context(key,mid)['message']['poll']['options']
    assert [o['voted_by_truncated'] for o in options]==[True,True,False] and options[1]['voted_by']==[]
    assert len(options[0]['voted_by'])==50


# spec: ANYM-920
def test_message_reads_show_a_telegram_polls_counts_total_choice_and_public_voters(tg_polls):
    w,store,key=tg_polls
    w.provider=PollVotesProvider()
    w.state.apply(tg_poll_capture(public=True,chosen=(1,)))
    w.state.apply(tg_poll_capture(41,poll_id=889,voters=(4,1),options=('Да','Нет')))  # anonymous
    asyncio.run(w.poll_voters_pass());w.spool.drain(w.state.apply,100)
    chat_id=stable_id('chat','tg-polls',TG_POLL_CHAT)
    public,anonymous=(stable_id('msg','tg-polls',TG_POLL_CHAT,str(m)) for m in (30,41))
    reads={'archive':store.get_messages(key,chat_id)['items'],'conversation':store.get_messages(key,chat_id,view='conversation')['items'],
        'search':store.search(key,'',chat=chat_id)['items'],'context':[store.get_context(key,public)['message'],store.get_context(key,anonymous)['message']]}
    for name,items in reads.items():
        poll=next(i for i in items if i['id']==public)['poll']
        assert [(o['text'],o['voters']) for o in poll['options']]==[('Да',2),('Нет',1),('Может быть',0)],name
        assert poll['options'][1]['voted_by']==[{'sender':'42','name':'Синтетик А'}],name
        assert poll['options'][0]['voted_by_truncated'] is False and poll['public_voters'] is True,name
        assert poll['total_voters']==3 and poll['chosen']==[1] and poll['results_as_of'] and 'provider_poll_id' not in poll,name
        hidden=next(i for i in items if i['id']==anonymous)['poll']
        assert [o['voters'] for o in hidden['options']]==[4,1] and hidden['total_voters']==5,name
        assert all(o['voted_by']==[] for o in hidden['options']) and hidden['public_voters'] is False,name
    # 60 voters of one option: 50 listed, the rest counted.
    w.provider.pages=[(1000+n,f'Синтетик {n}',[b'2']) for n in range(60)]
    with w.archive.store.pool.connection() as c:
        c.execute("UPDATE telegram_polls SET votes_read_at=now()-interval '11 minutes',results_at=now() WHERE message=%s",(public,))
    w._poll_reads.clear()  # the in-process throttle of the same 10 minutes
    asyncio.run(w.poll_voters_pass());w.spool.drain(w.state.apply,100)
    option=store.get_context(key,public)['message']['poll']['options'][2]
    assert len(option['voted_by'])==50 and option['voted_by_truncated'] is True and option['voted_by'][0]['name']
    # A key without read on the chat gets nothing.
    Archive(store).register('tg-other','telegram','Synthetic other',discover=True)
    other=store.issue_key('tg-other-reader',['tg-other'],permissions=['read'],key_permissions=['read'])
    from messaging_service.store import APIError
    with pytest.raises(APIError):store.get_context(other,public)
    assert store.search(other,'')['items']==[]


# spec: ANYM-920
def test_telegram_poll_count_changes_wake_only_a_subscription_that_lists_them(tg_polls):
    from datetime import timedelta
    from messaging_service.subscriptions import match_subscriptions
    w,store,key=tg_polls
    w.state.apply(tg_poll_capture())
    chat_id=stable_id('chat','tg-polls',TG_POLL_CHAT)
    match_subscriptions(store)
    default=store.create_subscription(key,[chat_id])['subscription_id']
    listed=store.create_subscription(key,[chat_id],kinds=['poll.updated'])['subscription_id']
    base=datetime.now(timezone.utc)
    for n in range(30):
        assert w.state.apply(tg_poll_update((n+3,1,0),base+timedelta(seconds=n+1)))['outcome']=='poll_updated'
    match_subscriptions(store)
    assert store.get_subscription_events(key,default,limit=100)['items']==[]
    items=store.get_subscription_events(key,listed,limit=100)['items']
    assert len(items)==30 and {i['kind'] for i in items}=={'poll.updated'}


# spec: ANYM-920
def test_the_telethon_provider_subscribes_poll_updates_and_pages_a_polls_votes_through_the_governor():
    from telethon import events,types
    from telethon.tl.functions.messages import GetPollVotesRequest
    from messaging_service.telegram_collector import TelethonProvider
    pages=[types.messages.VotesList(count=3,next_offset='next',chats=[],
                users=[types.User(id=42,first_name='Синтетик',last_name='А'),types.User(id=43,first_name='Синтетик')],
                votes=[types.MessagePeerVote(peer=types.PeerUser(42),option=b'1',date=None),
                       types.MessagePeerVoteMultiple(peer=types.PeerUser(43),options=[b'0',b'2'],date=None)]),
           types.messages.VotesList(count=3,next_offset=None,chats=[],users=[],
                votes=[types.MessagePeerVoteInputOption(peer=types.PeerUser(44),date=None)])]
    requests,phases,handlers=[],[],[]
    class Client:
        def add_event_handler(self,handler,event):handlers.append((handler,event))
        async def get_input_entity(self,peer):return types.InputPeerChat(500)
        async def __call__(self,request):
            requests.append(request);return pages[len(requests)-1]
    provider=TelethonProvider.__new__(TelethonProvider);provider.client=Client()
    async def rpc(operation,phase,*,before=None,tier=None):
        phases.append((phase,tier));return await operation()
    provider.rpc=rpc
    provider.subscribe_polls(print)
    assert isinstance(handlers[0][1],events.Raw) and handlers[0][1].types is types.UpdateMessagePoll
    votes=asyncio.run(provider.poll_votes(TG_POLL_CHAT,30,limit=200))
    assert votes==([(42,'Синтетик А',[b'1']),(43,'Синтетик',[b'0',b'2'])],True)
    assert all(isinstance(r,GetPollVotesRequest) and r.limit<=50 and r.id==30 for r in requests)
    assert [r.offset for r in requests]==[None,'next'] and phases==[('telegram_poll_votes',2)]*2


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


# Upper bound of an event wait in the runner tests below: generous for a loaded host, never a timing.
BOUND=30


def test_runner_graceful_stop_disconnects_and_releases_lease(worker):
    w,s,_=worker
    w.archive.release(w.account,w.owner,w.state.lease[1])
    async def scenario():
        # Wait on the connect itself, not a wall-clock poll: a loaded host runs the DB steps before it slowly.
        connected=asyncio.Event(); connect=w.provider.connect
        async def connecting(): await connect(); connected.set()
        w.provider.connect=connecting
        running=asyncio.create_task(w.run())
        await asyncio.wait_for(connected.wait(),BOUND)
        assert w.provider.connected
        w.stopping.set()
        await asyncio.wait_for(running,BOUND)
    asyncio.run(scenario())
    assert w.provider.closed
    assert w.archive.acquire(w.account,'replacement')>w.state.lease[1]


# spec: ANYM-914
@pytest.mark.parametrize('disconnect',['verified','failed'])
def test_runner_stop_releases_the_claim_first_and_only_after_a_verified_disconnect(worker,disconnect):
    w,s,_=worker
    w.archive.release(w.account,w.owner,w.state.lease[1])
    order=[]
    async def release_claim(): order.append('claim')
    w.release_claim=release_claim
    lease=w.archive.release
    def recorded(*args): order.append('lease'); return lease(*args)
    w.archive.release=recorded  # the last cleanup step of run's finally, after the status rows
    if disconnect=='failed':
        async def broken(): raise OSError('synthetic half-open connection')
        w.provider.disconnect=broken
    async def scenario():
        connected=asyncio.Event(); connect=w.provider.connect
        async def connecting(): await connect(); connected.set()
        w.provider.connect=connecting
        running=asyncio.create_task(w.run())
        await asyncio.wait_for(connected.wait(),BOUND)
        order.clear(); w.stopping.set()
        await asyncio.wait_for(running,BOUND)
    asyncio.run(scenario())
    # Released before the status and lease cleanup (the stop grace covers it); an unverified disconnect keeps the row.
    assert order==(['claim','lease'] if disconnect=='verified' else ['lease']), order


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
        # A stop or a lost lease ends the hung connect by itself: the connect timeout is far past the bound.
        w.connect_timeout=.05 if reason=='connect_timeout' else 100*BOUND
        if reason=='lease_lost': w.heartbeat=lost_lease
        running=asyncio.create_task(w.run())
        try:
            await asyncio.wait_for(entered.wait(),BOUND)
            if reason=='stop':
                w.stopping.set()
                await asyncio.wait_for(asyncio.shield(running),BOUND)
            else:
                error=TimeoutError if reason=='connect_timeout' else RuntimeError
                code='provider_connect_timeout' if reason=='connect_timeout' else 'collector_lease_lost'
                with pytest.raises(error,match=code):
                    await asyncio.wait_for(asyncio.shield(running),BOUND)
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
    assert manifest['schema_version'] == 93
    assert manifest['tables']['collector_chats']['rows']==1
    assert manifest['tables']['provider_tombstones']['rows']==1
    assert verify_snapshot(s,directory)['status']=='verified'
    with s.pool.connection() as c: c.execute('DELETE FROM provider_tombstones WHERE account=%s',(w.account,))
    with pytest.raises(RuntimeError,match='restored_database_mismatch'): verify_snapshot(s,directory)
