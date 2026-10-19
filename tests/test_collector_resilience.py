"""One bad item, a slow rate gate or a database blip must not stop an account."""
import asyncio
from datetime import datetime,timezone
import os
import time
from types import SimpleNamespace as NS

import psycopg
import pytest

from messaging_service.adapters import telegram_message,whatsapp_message
from messaging_service.blobs import Blob,BlobWriter
from messaging_service.commands import CommandWorker
from messaging_service.outgoing import DispatchNotStarted,OutgoingWorker,dispatch,finish_durably
from messaging_service.spool import DurableSpool,InvalidCapture,PoisonItem
from messaging_service.telegram_collector import TelegramCollector,media_deadline
from test_collectors import message,worker  # noqa: F401  (fixture)


def strict(item):
    if item.get('bad'):raise ValueError('invalid_body')
    return item


def spool(tmp_path,**options):
    return DurableSpool(tmp_path/'spool',validator=strict,reserve_bytes=0,**options)


def names(path):
    return sorted(p.name for p in path.glob('*.json'))


def plant(queue,item):
    # Simulates an item written by an older or different validator.
    validator,queue.validator=queue.validator,lambda value:value
    try:return queue.put(item)
    finally:queue.validator=validator


def test_invalid_item_is_quarantined_and_later_items_still_drain(tmp_path):
    queue=spool(tmp_path)
    queue.put({'n':1})
    bad=queue.path/plant(queue,{'n':2,'bad':True})
    queue.put({'n':3})
    applied=[]
    assert queue.drain(applied.append)==2
    assert [item['n'] for item in applied]==[1,3] and names(queue.path)==[]
    assert queue.quarantined()==1
    held=queue.quarantine_path/bad.name
    assert held.read_bytes()==b'{"bad":true,"n":2}'
    assert (queue.quarantine_path/(bad.name+'.reason')).read_text()=='invalid_body\n'
    assert oct(queue.quarantine_path.stat().st_mode&0o777)=='0o700'




def test_reason_never_copies_exception_text(tmp_path):
    def leaky(item):raise ValueError('PRIVATE body text: '+item['text'])
    queue=DurableSpool(tmp_path/'spool',validator=lambda item:item,reserve_bytes=0)
    queue.put({'text':'secret'})
    queue.validator=leaky
    queue.drain(lambda item:None)
    reason=next(queue.quarantine_path.glob('*.reason')).read_text()
    assert reason=='invalid_capture\n'


def test_poison_from_ingest_is_quarantined_but_other_errors_keep_the_item(tmp_path):
    queue=spool(tmp_path)
    queue.put({'n':1})
    def unavailable(item):raise ValueError('collection_disabled')
    # State errors are not poison: the item must wait for the next attempt.
    with pytest.raises(ValueError,match='collection_disabled'):queue.drain(unavailable)
    assert len(names(queue.path))==1 and queue.quarantined()==0
    def poison(item):raise PoisonItem('send_receipt_state_mismatch')
    assert queue.drain(poison)==0
    assert names(queue.path)==[] and queue.quarantined()==1


def test_full_quarantine_stops_draining_instead_of_hiding_more_items(tmp_path):
    queue=spool(tmp_path)
    queue.max_quarantined=1
    for n in (1,2):plant(queue,{'n':n,'bad':True})
    with pytest.raises(RuntimeError,match='spool_corrupt'):queue.drain(lambda item:None)
    assert queue.quarantined()==1 and len(names(queue.path))==1


def test_shared_quarantine_folder_must_stay_private(tmp_path):
    queue=spool(tmp_path)
    queue.quarantine_path.mkdir(mode=0o755);os.chmod(queue.quarantine_path,0o755)
    plant(queue,{'n':1,'bad':True})
    with pytest.raises(RuntimeError,match='spool_corrupt'):queue.drain(lambda item:None)
    assert len(names(queue.path))==1


def test_discard_removes_only_strict_spool_names(tmp_path):
    queue=spool(tmp_path)
    name=queue.put({'n':1})
    for unsafe in ('../outside.json','queue.lock','x'*20+'-'+'0'*64+'.json'):
        with pytest.raises(ValueError,match='invalid_spool_name'):queue.discard(unsafe)
    queue.discard(name);queue.discard(name)
    assert names(queue.path)==[]


def test_put_still_deduplicates_and_counts_abandoned_temporary_files(tmp_path):
    queue=spool(tmp_path,max_bytes=200)
    assert queue.put({'n':1})==queue.put({'n':1})
    (queue.path/'.pending-abandoned').write_bytes(b'x'*195)
    with pytest.raises(RuntimeError,match='spool_full'):queue.put({'n':2})


def telegram_file(**file):
    defaults={'name':'report.pdf','ext':'.pdf','mime_type':'application/pdf','size':4}
    return NS(chat_id=-100123,id=19,sender_id=42,message='caption',
        date=datetime(2026,10,1,tzinfo=timezone.utc),edit_date=None,out=False,
        file=NS(**{**defaults,**file}),document=NS(id=7),photo=None)


def test_telegram_sender_controlled_file_metadata_is_normalized():
    media=telegram_message('tg-owner',telegram_file(mime_type='Text/Plain; charset=utf-8',
        name='n'*300+'.document',size=9*1024**3))['media'][0]
    assert media['mime']=='text/plain' and media['bytes'] is None
    assert len(media['filename'])==255 and media['filename'].endswith('n.document')
    for mime in ('not a mime','text/plain\r\nX-Injected: 1',None,'a/'+'b'*200):
        assert telegram_message('tg-owner',telegram_file(mime_type=mime))['media'][0]['mime']=='application/octet-stream'
    assert telegram_message('tg-owner',telegram_file(name=None))['media'][0]['filename']=='19.pdf'


def whatsapp_event(**changes):
    return {'messageId':'false_123@c.us_SYNTH','chatId':'123@c.us','senderId':'123@c.us',
        'timestamp':1800000000,'body':'hello',**changes}


def test_whatsapp_sender_controlled_text_is_bounded_not_fatal():
    long_text='я'*70000
    e=whatsapp_message('wa',whatsapp_event(body='bad\x00text',senderName='N'*600,senderUsername='u'*200,
        chatName='C'*700,replyToId='r'*300))
    assert e['body']=='bad\ufffdtext' and e['formatting']['state']=='unknown'
    assert len(e['sender_profile']['display_name'])==512 and len(e['sender_profile']['username'])==128
    assert len(e['chat_title'])==512 and e['metadata']['reply_to_provider_id'] is None
    assert len(whatsapp_message('wa',whatsapp_event(body=long_text))['body'])==65536
    media=whatsapp_message('wa',whatsapp_event(hasMedia=True,filename='f'*400,mimetype='image/JPEG',filesize=2**40))['media'][0]
    assert media['mime']=='image/jpeg' and media['bytes'] is None and len(media['filename'])==255


def test_whatsapp_valid_reply_and_formatting_are_kept():
    e=whatsapp_message('wa',whatsapp_event(replyToId='false_123@c.us_PARENT'))
    assert e['metadata']['reply_to_provider_id']=='false_123@c.us_PARENT'


def test_unstorable_history_message_is_skipped_and_counted(worker,monkeypatch):
    w,s,key=worker
    import messaging_service.telegram_collector as collector
    original=collector.telegram_message
    def flaky(account,item,**options):
        if item.id==3:raise ValueError('invalid_body')
        return original(account,item,**options)
    monkeypatch.setattr(collector,'telegram_message',flaky)
    for _ in range(3):asyncio.run(w.sync_once())
    assert len(s.search(key)['items'])==4 and w.invalid_messages==1
    # Committed pages leave nothing for the drain to apply a second time.
    assert names(w.spool.path)==[]
    assert w.state.chat('100','user')['high_water']==5


def test_capture_status_names_quarantine_and_invalid_messages(worker):
    w,s,key=worker
    w.state.capture_progress(None,0,0,quarantined=2,invalid=3)
    w.state.capture_progress(None,0,0,quarantined=2,invalid=1)
    with s.pool.connection() as c:
        operations=c.execute('SELECT operations FROM collector_health WHERE account=%s',(w.account,)).fetchone()['operations']
    live=operations['live_capture']
    assert live['reason_code']=='capture_quarantined' and live['next_action']=='inspect_capture_quarantine'
    assert live['quarantined_captures']==2 and live['invalid_provider_messages']==4
    w.state.capture_progress(None,0,0,quarantined=0)
    with s.pool.connection() as c:
        live=c.execute('SELECT operations FROM collector_health WHERE account=%s',(w.account,)).fetchone()['operations']['live_capture']
    assert live['reason_code']=='capture_ready' and live['next_action'] is None
    with pytest.raises(ValueError,match='invalid_capture_progress'):w.state.capture_progress(None,0,0,quarantined=-1)


def bare_collector(**attributes):
    collector=TelegramCollector.__new__(TelegramCollector)
    collector.account,collector.owner,collector.lease_ttl='tg','owner',60
    collector.state=NS(lease=('owner',1))
    collector._lease_valid_until=None
    for name,value in attributes.items():setattr(collector,name,value)
    return collector


def test_lease_renewal_retries_database_blips_within_the_lease():
    calls=[]
    def renew(*args):
        calls.append(args)
        if len(calls)==1:raise psycopg.OperationalError('PRIVATE_CONNECTION_DETAIL')
    collector=bare_collector(archive=NS(renew=renew))
    asyncio.run(collector.renew_lease())
    assert len(calls)==2 and collector._lease_valid_until>time.monotonic()+40


def test_lease_renewal_stops_when_the_local_deadline_passes():
    def hang(*args):time.sleep(.3)
    collector=bare_collector(archive=NS(renew=hang))
    collector._lease_valid_until=time.monotonic()+.05
    with pytest.raises(RuntimeError,match='^collector_lease_lost$'):asyncio.run(collector.renew_lease())
    collector._lease_valid_until=time.monotonic()-1
    with pytest.raises(RuntimeError,match='^collector_lease_lost$'):asyncio.run(collector.renew_lease())


def test_reconcile_loop_survives_database_errors(monkeypatch):
    attempts=[];sleeps=[]
    class Stop(BaseException):pass
    async def sync_once():
        attempts.append(1)
        if len(attempts)<3:raise psycopg.errors.QueryCanceled('statement timeout')
        raise Stop()
    async def sleep(delay):sleeps.append(delay)
    import messaging_service.telegram_collector as collector
    monkeypatch.setattr(collector.asyncio,'sleep',sleep)
    import messaging_service.telegram_identity as identity
    monkeypatch.setattr(identity,'repair_sent_senders',lambda *args:None)
    w=bare_collector(sync_once=sync_once,provider=NS(),interval=60,archive=None)
    with pytest.raises(Stop):asyncio.run(w.reconcile_loop())
    assert len(attempts)==3 and sleeps==[2,4]


def test_large_original_gets_a_deadline_that_matches_paced_transfer():
    assert media_deadline(None)==3600 and media_deadline(10*1024**2)==3600
    two_gib=media_deadline(2*1024**3)
    assert 2*1024**3/(256*1024)<two_gib<=6*3600 and media_deadline(64*1024**3)==6*3600


def job(**changes):
    return {'id':'synthetic-send','expires_at':time.time()+60,**changes}


def test_slow_rate_gate_does_not_consume_the_provider_timeout():
    async def provider(item):
        item['_provider_phase']('waiting')
        await asyncio.sleep(.2)  # Longer than the provider timeout below.
        item['_provider_phase']('started')
        return 'sent'
    assert asyncio.run(dispatch(job(),provider,.1))=='sent'


def test_provider_timeout_starts_with_the_request():
    async def provider(item):
        item['_provider_phase']('started')
        await asyncio.sleep(1)
    with pytest.raises(TimeoutError):asyncio.run(dispatch(job(),provider,.05))


def test_waiting_is_bounded_by_job_expiry():
    async def provider(item):
        item['_provider_phase']('waiting')
        await asyncio.Event().wait()
    started=time.monotonic()
    with pytest.raises(DispatchNotStarted):asyncio.run(dispatch(job(expires_at=time.time()-.5),provider,30))
    assert time.monotonic()-started<5


def send_outcomes(provider,**changes):
    finishes=[]
    class Worker(OutgoingWorker):
        def claim(self):return job(**changes)
        def preflight(self,item):return item
        def finish(self,item,status,provider_id=None,code=None):finishes.append((status,code))
    asyncio.run(Worker(NS(),provider)._once())
    return finishes


@pytest.mark.parametrize('phase,expected',[
    ('waiting',('failed','send_not_started')),
    ('refused',('failed','send_not_started')),
    ('started',('unknown','send_result_unknown')),
    (None,('unknown','send_result_unknown')),
])
def test_send_outcome_depends_on_whether_the_provider_saw_the_request(phase,expected):
    async def provider(item):
        if phase:item['_provider_phase'](phase)
        raise RuntimeError('PRIVATE_PROVIDER_TEXT')
    assert send_outcomes(provider)==[expected]


def test_send_not_started_after_expiry_is_expired():
    async def provider(item):
        item['_provider_phase']('waiting')
        raise RuntimeError('provider_disconnected')
    assert send_outcomes(provider,expires_at=time.time()-1)==[('expired','send_expired')]


def test_finish_retries_transient_database_errors(monkeypatch):
    calls=[]
    def finish(*args):
        calls.append(args)
        if len(calls)<3:raise psycopg.OperationalError('PRIVATE')
        return 'recorded'
    async def instant(delay):pass
    monkeypatch.setattr('messaging_service.outgoing.asyncio.sleep',instant)
    assert asyncio.run(finish_durably(finish,'job','sent','id'))=='recorded' and len(calls)==3
    def lease_lost(*args):raise RuntimeError('collector_lease_lost')
    with pytest.raises(RuntimeError,match='collector_lease_lost'):asyncio.run(finish_durably(lease_lost))
    # A lasting outage stops the collector (not a retried loop), so the next
    # lease marks the row interrupted instead of leaving it dispatching.
    def offline(*args):raise psycopg.OperationalError('PRIVATE')
    with pytest.raises(RuntimeError,match='^dispatch_result_unrecorded$'):asyncio.run(finish_durably(offline))


@pytest.mark.parametrize('phases,mutation,expected',[
    (['waiting'],True,('failed','operation_not_started')),
    (['waiting','started'],True,('unknown','operation_result_unknown')),
    ([],True,('unknown','operation_result_unknown')),
    (['waiting'],False,('failed','operation_result_unknown')),
])
def test_command_timeout_before_mutation_is_not_unknown(phases,mutation,expected):
    finishes=[]
    class Worker(CommandWorker):
        def claim(self):return {'id':'synthetic-command','mutation':mutation,'operation':'chat_info'}
        def preflight(self,item,*,verify_file=True):return item
        def finish(self,item,status,result=None,code=None):finishes.append((status,code))
    async def provider(item):
        for phase in phases:item['_provider_phase'](phase)
        raise TimeoutError()
    asyncio.run(Worker(NS(),provider)._once())
    assert finishes==[expected]


def test_telegram_flood_wait_on_send_is_reported_as_refused():
    from telethon.errors import FloodWaitError
    from messaging_service.telegram_collector import TelethonProvider
    phases=[]
    class Client:
        async def send_message(self,*args,**kwargs):raise FloodWaitError(None,capture=30)
    provider=TelethonProvider.__new__(TelethonProvider)
    provider.client,provider.governor=Client(),None
    item={'id':'send_'+'a'*32,'provider_chat':'100','body':'Synthetic',
        'expires_at':time.time()+60,'_provider_phase':phases.append}
    with pytest.raises(FloodWaitError):asyncio.run(provider.send_text(item))
    assert phases==['waiting','started','refused']


def test_corrupt_original_is_repaired_by_a_verified_copy(tmp_path):
    writer=BlobWriter(tmp_path/'files',reserve_bytes=0)
    blob=writer.write([b'original'])
    path=writer.root/blob.digest
    path.write_bytes(b'damaged!')
    assert writer.write([b'original'])==blob
    assert writer.verify(blob)==path and path.read_bytes()==b'original'
    with pytest.raises(ValueError,match='media_hash_mismatch'):
        writer.write([b'other'],expected_sha256=blob.digest)


def test_sweep_removes_only_old_abandoned_temporary_files(tmp_path):
    writer=BlobWriter(tmp_path/'files',reserve_bytes=0)
    old=writer.root/('.pending-'+'a'*32);fresh=writer.root/('.pending-'+'b'*32)
    other=writer.root/'.pending-unexpected';upload=writer.root/'upload';upload.mkdir()
    nested=upload/('.pending-'+'c'*32);target=tmp_path/'outside'
    for path in (old,fresh,other,nested,target):path.write_bytes(b'x')
    link=writer.root/('.pending-'+'d'*32);link.symlink_to(target)
    stale=time.time()-2*86400
    for path in (old,other,nested):os.utime(path,(stale,stale))
    os.utime(link,(stale,stale),follow_symlinks=False)
    assert writer.sweep_abandoned()==1
    assert not old.exists() and fresh.exists() and other.exists() and nested.exists()
    assert link.is_symlink() and target.exists()


# Live callbacks: one deterministically unstorable item is skipped and counted;
# capacity, I/O and unexpected errors still fail the collector closed.

def live_status(w,store):
    asyncio.run(w.drain_captures([w.spool]))
    with store.pool.connection() as c:
        return c.execute('SELECT operations FROM collector_health WHERE account=%s',(w.account,)).fetchone()['operations']['live_capture']


def spooled(w):
    items=[];w.spool.drain(items.append);return items


def test_spool_rejection_is_invalid_capture_and_writes_nothing(tmp_path):
    queue=spool(tmp_path)
    with pytest.raises(InvalidCapture,match='^invalid_body$'):queue.put({'bad':True})
    with pytest.raises(InvalidCapture,match='^invalid_capture$'):queue.put({'bad':False,'x':{1,2}})
    assert names(queue.path)==[] and not list(queue.path.glob('.pending-*'))


def test_rejected_live_message_is_counted_with_its_identity_and_capture_continues(worker,monkeypatch):
    w,s,_=worker
    import messaging_service.telegram_collector as collector
    original=collector.telegram_message
    def broken(account,item,**options):
        e=original(account,item,**options)
        return {**e,'sent_at':'not-a-time'} if item.id==7 else e
    monkeypatch.setattr(collector,'telegram_message',broken)
    asyncio.run(w.capture(NS(message=message(7))))
    asyncio.run(w.capture(NS(message=message(8))))
    assert not w.failed.is_set() and w.capture_count==0
    assert [i['envelope']['provider_id'] for i in spooled(w)]==['8']
    live=live_status(w,s)
    assert sum(live['rejected_live_captures'].values())==1
    [skipped]=live['rejected_live_messages']
    assert skipped['chat']=='100' and skipped['message_id']==7 and skipped['reason_code'] in live['rejected_live_captures']
    assert set(skipped)=={'chat','message_id','reason_code','observed_at'}


def test_unsupported_live_peer_is_an_invalid_message_not_a_stop(worker):
    w,s,_=worker
    odd=message(7);odd.peer_id=NS()
    asyncio.run(w.capture(NS(message=odd)))
    assert not w.failed.is_set() and w.invalid_messages==1
    live=live_status(w,s)
    assert live['invalid_provider_messages']==1 and live['rejected_live_captures']=={'invalid_provider_message':1}
    assert live['rejected_live_messages'][0]['message_id']==7


def test_invalid_live_delete_is_counted_and_next_delete_is_spooled(worker):
    w,s,_=worker
    asyncio.run(w.deleted(NS(chat_id=None,deleted_ids=[0])))
    asyncio.run(w.deleted(NS(chat_id=None,deleted_ids=[])))
    asyncio.run(w.deleted(NS(chat_id=None,deleted_ids=[4])))
    assert not w.failed.is_set() and w.capture_count==0
    assert [i['ids'] for i in spooled(w)]==[[4]]
    assert live_status(w,s)['rejected_live_captures']=={'invalid_deleted_ids':2}


def test_large_live_delete_is_spooled_in_parts_and_drains_every_tombstone(worker):
    w,s,key=worker
    asyncio.run(w.sync_once())
    ids=list(range(2500,0,-1))
    asyncio.run(w.deleted(NS(chat_id=None,deleted_ids=ids)))
    assert not w.failed.is_set()
    items=[]
    def apply(item):items.append(item);w.state.apply(item)
    w.spool.drain(apply)
    assert sorted(len(i['ids']) for i in items)==[500,1000,1000]
    assert sorted(x for i in items for x in i['ids'])==list(range(1,2501))
    assert len({i['observed_at'] for i in items})==1
    asyncio.run(w.sync_once())
    from messaging_service.envelope import stable_id
    # The first page archived messages 4 and 5; their tombstones applied from the split records.
    assert all(s.get_context(key,stable_id('msg',w.account,'100',str(n)))['message']['deleted'] for n in (4,5))


def receipt_ready(w):
    from telethon import types
    w.provider.verified_self=types.User(id=42,is_self=True);w.provider.expected_user_id=42
    return types.UpdateReadHistoryOutbox(types.PeerUser(100),23,10,1)


def test_invalid_or_unparsable_live_receipt_is_counted_not_a_stop(worker,monkeypatch):
    w,s,_=worker
    import messaging_service.telegram_receipts as receipts
    event=receipt_ready(w);original=receipts.telegram_receipt
    monkeypatch.setattr(receipts,'telegram_receipt',lambda *a,**k:{**original(*a,**k),'max_id':0})
    asyncio.run(w.receipt(event))
    def boom(*a,**k):raise AttributeError('synthetic-private-parser-detail')
    monkeypatch.setattr(receipts,'telegram_receipt',boom)
    asyncio.run(w.receipt(event))
    monkeypatch.setattr(receipts,'telegram_receipt',original)
    asyncio.run(w.receipt(event))
    assert not w.failed.is_set() and w.capture_count==0
    assert [i['kind'] for i in spooled(w)]==['message_receipt']
    live=live_status(w,s)
    assert live['rejected_live_captures']=={'invalid_receipt_reference':1,'receipt_parse_failed':1}
    assert 'synthetic-private' not in str(live)


def test_invalid_or_unparsable_live_owner_read_is_counted_not_a_stop(worker,monkeypatch):
    w,s,_=worker
    import messaging_service.reply_state as reply_state
    from telethon import types
    receipt_ready(w)
    event=types.UpdateReadHistoryInbox(types.PeerUser(100),23,2,10,1)
    original=reply_state.telegram_read_capture
    monkeypatch.setattr(reply_state,'telegram_read_capture',lambda *a,**k:{**original(*a,**k),'extra':1})
    asyncio.run(w.owner_read(event))
    def boom(*a,**k):raise TypeError('synthetic-private-parser-detail')
    monkeypatch.setattr(reply_state,'telegram_read_capture',boom)
    asyncio.run(w.owner_read(event))
    monkeypatch.setattr(reply_state,'telegram_read_capture',original)
    asyncio.run(w.owner_read(event))
    assert not w.failed.is_set() and w.capture_count==0
    assert [i['kind'] for i in spooled(w)]==['owner_read_state']
    assert live_status(w,s)['rejected_live_captures']=={'invalid_read_capture':1,'read_parse_failed':1}


@pytest.mark.parametrize('cause,detail',[('disk_reserve','disk_reserve'),('spool_full','spool_full'),
    ('io','spool_io_error'),('other','capture_error')])
def test_capacity_io_and_unexpected_errors_still_fail_closed_with_a_fixed_detail(worker,cause,detail):
    w,_,_=worker
    if cause=='disk_reserve':w.spool.reserve_bytes=2**62
    elif cause=='spool_full':w.spool.max_entries=0
    else:
        def failing(_):raise OSError(5,'synthetic-private-path') if cause=='io' else LookupError('synthetic-private')
        w.spool.put=failing
    asyncio.run(w.capture(NS(message=message(7))))
    assert w.failed.is_set() and w.failure_code=='durable_capture_failed' and w.failure_detail==detail
    assert names(w.spool.path)==[] and w.capture_count==0
    # The first failure wins; later callbacks write nothing.
    asyncio.run(w.deleted(NS(chat_id=None,deleted_ids=[4])))
    assert names(w.spool.path)==[]


def test_capture_stop_detail_is_kept_in_live_capture_status(worker):
    w,s,_=worker
    w.state.connection_failed('durable_capture_failed','disk_reserve')
    w.state.capture_progress(None,0,0)
    with s.pool.connection() as c:
        operations=c.execute('SELECT operations FROM collector_health WHERE account=%s',(w.account,)).fetchone()['operations']
    stop=operations['live_capture']['last_capture_stop']
    assert stop['code']=='durable_capture_failed' and stop['detail']=='disk_reserve' and stop['at']
    assert operations['connection']['reason_code']=='durable_capture_failed'
    # Unknown details and non-capture stops record no capture detail.
    w.state.connection_failed('durable_capture_failed','synthetic-private')
    w.state.connection_failed('provider_disconnected','disk_reserve')
    with s.pool.connection() as c:
        live=c.execute('SELECT operations FROM collector_health WHERE account=%s',(w.account,)).fetchone()['operations']['live_capture']
    assert live['last_capture_stop']['detail'] is None and live['last_capture_stop']['code']=='durable_capture_failed'


# spec: ANYM-291
def test_capture_overload_stop_keeps_which_bound_tripped(worker):
    w,s,_=worker
    class LooksSafe(str):
        # Private text whose hash and equality pretend to be a fixed detail.
        def __eq__(self,other):return other==self.target
        def __hash__(self):return hash(self.target)
    for code,target in [('capture_overload','waiters_limit'),('durable_capture_failed','disk_reserve')]:
        spoof=LooksSafe('synthetic-private-detail');spoof.target=target
        for detail in ['waiters_limit','slot_wait_timeout','disk_reserve','synthetic-private',spoof,['waiters_limit']]:
            w.state.connection_failed(code,detail)
            with s.pool.connection() as c:
                stop=c.execute('SELECT operations FROM collector_health WHERE account=%s',(w.account,)).fetchone()['operations']['live_capture']['last_capture_stop']
            fixed={'capture_overload':('waiters_limit','slot_wait_timeout'),'durable_capture_failed':('disk_reserve',)}[code]
            assert stop['code']==code
            assert stop['detail']==(detail if type(detail) is str and detail in fixed else None)
            assert 'synthetic-private' not in str(stop)


# spec: ANYM-291
def test_overload_records_which_bound_tripped(worker):
    w,_,_=worker
    w.capture_count=16;w.capture_wait_seconds=0
    asyncio.run(w.capture(NS(message=message(8))))
    assert (w.failure_code,w.failure_detail)==('capture_overload','slot_wait_timeout')


def test_rejected_counts_are_bounded_and_validated(worker):
    w,s,_=worker
    # Code-shaped exception text from a validator is not trusted as a reason.
    w.spool.validator=lambda item:(_ for _ in ()).throw(ValueError('synthetic_private_message'))
    asyncio.run(w.capture(NS(message=message(7))))
    asyncio.run(w.deleted(NS(chat_id=None,deleted_ids=[4])))
    assert not w.failed.is_set()
    live=live_status(w,s)
    assert live['rejected_live_captures']=={'invalid_capture':2}
    assert live['rejected_live_messages'][0]['reason_code']=='invalid_capture'
    assert 'synthetic_private' not in str(live)
    for bad in [{'synthetic_private_message':1},{'invalid_capture':0}]:
        with pytest.raises(ValueError,match='invalid_capture_progress'):w.state.capture_progress(None,0,0,rejected=bad)
    for bad in [{'chat':'100','message_id':1,'reason_code':'invalid_capture','observed_at':'2026-10-18T00:00:00+00:00','body':'private'},
                {'chat':'100','message_id':1,'reason_code':'synthetic_private','observed_at':'2026-10-18T00:00:00+00:00'}]:
        with pytest.raises(ValueError,match='invalid_capture_progress'):w.state.capture_progress(None,0,0,rejected_messages=[bad])


@pytest.mark.parametrize('callback',['receipt','owner_read'])
@pytest.mark.parametrize('error,detail',[(OSError(5,'synthetic-private'),'spool_io_error'),
    (MemoryError(),'capture_error'),(RuntimeError('synthetic-private'),'capture_error')])
def test_parser_resource_or_unexpected_error_still_fails_closed(worker,monkeypatch,callback,error,detail):
    w,_,_=worker
    import messaging_service.reply_state as reply_state
    import messaging_service.telegram_receipts as receipts
    event=receipt_ready(w)
    def boom(*a,**k):raise error
    monkeypatch.setattr(receipts if callback=='receipt' else reply_state,
        'telegram_receipt' if callback=='receipt' else 'telegram_read_capture',boom)
    asyncio.run(getattr(w,callback)(event))
    assert w.failed.is_set() and w.failure_code=='durable_capture_failed' and w.failure_detail==detail
    assert w.rejected=={} and names(w.spool.path)==[]


def test_an_io_error_that_is_also_a_value_error_is_never_a_rejection(worker):
    import io
    w,_,_=worker
    def unsupported(item):raise io.UnsupportedOperation('synthetic-private')
    w.spool.validator=unsupported
    asyncio.run(w.deleted(NS(chat_id=None,deleted_ids=[4])))
    assert w.failed.is_set() and w.failure_detail=='spool_io_error' and w.rejected=={}


class Raising:
    @property
    def chat_id(self):raise RuntimeError('synthetic-private')


@pytest.mark.parametrize('event',[NS(),NS(message=Raising())])
def test_malformed_live_event_fails_closed_and_releases_its_slot(worker,event):
    w,_,_=worker
    asyncio.run(w.capture(event))
    assert w.failed.is_set() and w.failure_code=='durable_capture_failed' and w.failure_detail=='capture_error'
    assert w.capture_count==0 and w.rejected=={}


def test_rejection_evidence_survives_a_failed_status_write(worker):
    w,s,_=worker
    asyncio.run(w.deleted(NS(chat_id=None,deleted_ids=[0])))
    odd=message(7);odd.peer_id=NS()
    asyncio.run(w.capture(NS(message=odd)))
    real=w.state.capture_progress;calls=[]
    def flaky(*args,**kwargs):
        calls.append(kwargs)
        if len(calls)==1:
            # A rejection that arrives while the first write is failing.
            w.reject('invalid_deleted_ids')
            raise psycopg.OperationalError('synthetic outage')
        return real(*args,**kwargs)
    w.state.capture_progress=flaky
    with pytest.raises(psycopg.OperationalError):asyncio.run(w.drain_captures([w.spool]))
    w._next_capture_status=0
    asyncio.run(w.drain_captures([w.spool]))
    assert calls[1]['rejected']=={'invalid_deleted_ids':2,'invalid_provider_message':1} and calls[1]['invalid']==1
    assert [m['message_id'] for m in calls[1]['rejected_messages']]==[7]
    assert w.rejected=={} and w.rejected_messages==[] and w.invalid_messages==0


def test_run_reports_the_capture_stop_detail_to_state(tmp_path):
    from messaging_service.collector_state import CaptureSpool
    calls=[]
    worker=TelegramCollector(None,'tg-fixture',None,CaptureSpool(tmp_path/'captures',reserve_bytes=0),writer=object())
    async def connect():worker.fail('durable_capture_failed','disk_reserve')
    async def idle():await asyncio.Event().wait()
    async def disconnect():pass
    worker.provider=NS(subscribe=lambda *a:None,connect=connect,disconnect=disconnect,wait_disconnected=idle)
    state=NS(policy=lambda:{'account':'tg-fixture'},status=lambda *a:None,phase=lambda *a:None,
        connection_gate=lambda:{'blocked':False,'remaining_seconds':0},connection_failed=lambda *a:calls.append(a))
    worker.state_class=lambda *a:state
    worker.archive=NS(acquire=lambda *a:1,release=lambda *a:None)
    for name in ('heartbeat','drain_loop','reconcile_loop','media_loop','outgoing_loop'):setattr(worker,name,idle)
    with pytest.raises(RuntimeError,match='^durable_capture_failed$') as stop:asyncio.run(worker.run())
    assert stop.value.detail=='disk_reserve' and calls==[('durable_capture_failed','disk_reserve')]


@pytest.mark.parametrize('detail,expected',[('disk_reserve',{'detail':'disk_reserve'}),('synthetic-private',{}),(None,{})])
def test_supervisor_stop_line_names_the_fixed_capture_detail_only(monkeypatch,capsys,detail,expected):
    import json,sys
    from messaging_service import telegram_collector,telemetry
    monkeypatch.setattr(telemetry,'configure',lambda component:None)
    monkeypatch.setattr(telemetry,'capture',lambda error,code:None)
    async def failed(config):
        stop=RuntimeError('durable_capture_failed');stop.detail=detail;raise stop
    monkeypatch.setattr(telegram_collector,'run_config',failed)
    monkeypatch.setattr(telegram_collector.os,'umask',lambda mode:None)
    monkeypatch.setattr(sys,'argv',['anymsg','--config','/synthetic/config.json'])
    with pytest.raises(SystemExit):telegram_collector.main()
    assert json.loads(capsys.readouterr().out)=={'status':'stopped','code':'durable_capture_failed',**expected}


def test_history_page_skips_an_item_the_spool_validator_rejects(worker,monkeypatch):
    w,s,key=worker
    import messaging_service.telegram_collector as collector
    original=collector.telegram_message
    def broken(account,item,**options):
        e=original(account,item,**options)
        return {**e,'sent_at':'not-a-time'} if item.id==3 else e
    monkeypatch.setattr(collector,'telegram_message',broken)
    for _ in range(3):asyncio.run(w.sync_once())
    assert len(s.search(key)['items'])==4 and w.invalid_messages==1
    assert w.state.chat('100','user')['high_water']==5
