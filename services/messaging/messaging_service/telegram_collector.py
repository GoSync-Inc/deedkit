"""Single-owner Telegram collector. No sign-in, messages or secrets in gateway tools."""
import asyncio
from collections import deque
import copy
from contextlib import contextmanager,suppress
from datetime import datetime,timedelta,timezone
import errno
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import stat
import time
import traceback

from .adapters import telegram_message
from .archive import Archive
from .blobs import BlobWriter
from .envelope import stable_id
from .collector_state import LIVE_REJECT_CODES,CaptureSpool,CollectorState,session_lock
from .spool import InvalidCapture
from .store import Store
from .telegram_governor import LIVE_BUDGETS,ProviderRateLimited,RequestGovernor
from .operational import CONNECTION_ERRORS,MEDIA_ERRORS,collector_stop
from .storage_admission import MEDIA_RESERVE_BYTES
from .media_policy import MEDIA_POLICY_FIELDS,configure_media_policy,validate_prefetch_days

log=logging.getLogger(__name__)
# Seconds before a live read's deadline in which no archive write starts (ANYM-477).
STORE_MARGIN=0.5
# Seconds a cancelled live read waits for its write thread; the server has ended the transaction by then.
STORE_GRACE=1.0


class DeadlinePool:
    """The archive pool for a live read's writes (ANYM-477, Sol recheck F1): one absolute deadline
    (time.monotonic) bounds the pool wait, and each transaction gets the time left as its
    transaction_timeout, lock_timeout and statement_timeout. PostgreSQL then ends the transaction and
    frees the lease fence it holds at the deadline, even when this client stalls."""
    def __init__(self,pool,deadline):self.pool,self.deadline=pool,deadline

    def left(self):
        remaining=self.deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('live_store_deadline')
        return remaining

    @contextmanager
    def connection(self,timeout=None):
        with self.pool.connection(timeout=min(self.left(),timeout or self.left())) as c:
            ms=str(max(1,int(self.left()*1000)))
            c.execute("SELECT set_config('transaction_timeout',%s,true),set_config('lock_timeout',%s,true),"
                "set_config('statement_timeout',%s,true)",(ms,ms,ms))
            yield c


def bounded_state(state,deadline):
    """A CollectorState for the same account and lease whose archive writes use a DeadlinePool."""
    archive=copy.copy(state.archive);archive.store=copy.copy(archive.store)
    archive.store.pool=DeadlinePool(state.archive.store.pool,deadline)
    return CollectorState(archive,state.account,state.lease)


async def finished(fn,*args):
    """Run fn in a thread; a cancelled caller waits for it at most STORE_GRACE before it goes on."""
    task=asyncio.ensure_future(asyncio.to_thread(fn,*args))
    try: return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait({task},timeout=STORE_GRACE)
        task.add_done_callback(lambda t:t.cancelled() or t.exception()); raise


class ProviderUnavailable(Exception): pass


class ProviderPageError(Exception):
    """A non-RPC failure inside one Telethon history read; keeps only the class name, never its text."""
    def __init__(self,error_class):
        self.error_class=error_class; super().__init__('provider_page_error')


class ChatPageFailed(Exception):
    """One chat's history page failed for a provider reason that must not stop the collector (ANYM-293)."""
    def __init__(self,code,error):
        self.code=code; self.error=error; super().__init__(code)


def database_errors():
    # Transient archive faults: statement timeouts, lost connections and a
    # saturated pool. Lease loss is a RuntimeError and still stops the collector.
    import psycopg
    from psycopg_pool import PoolTimeout
    return (psycopg.OperationalError,PoolTimeout)


def private_json(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as source:
        info=os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_size>65536:
            raise ValueError('private_config_permissions_or_size')
        return json.load(source)


def configuration(path):
    cfg=private_json(path)
    fields={'enabled','account','credentials_file','session_file','permit_file','spool_directory',
            'batch_size','scan_interval_seconds','max_media_bytes','disk_reserve_bytes',
            'max_dialogs','history_since','media_backfill','live_search_budget'}|MEDIA_POLICY_FIELDS
    if not isinstance(cfg,dict) or set(cfg)-fields: raise ValueError('invalid_collector_config')
    if cfg.get('enabled') is not True: raise RuntimeError('collector_disabled')
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',cfg.get('account','')): raise ValueError('invalid_account')
    for key in ['credentials_file','session_file','permit_file','spool_directory']:
        if not isinstance(cfg.get(key),str) or not Path(cfg[key]).is_absolute(): raise ValueError('invalid_private_path')
    if not cfg['session_file'].endswith('.session'): raise ValueError('invalid_session_filename')
    for key,default,lo,hi in [('batch_size',100,1,100),('scan_interval_seconds',60,10,3600),
                            ('max_media_bytes',512*1024**2,1,4*1024**3),
                            ('disk_reserve_bytes',1024**3,1024**3,100*1024**3)]:
        value=cfg.setdefault(key,default)
        if type(value) is not int or not lo<=value<=hi: raise ValueError('invalid_'+key)
    configure_media_policy(cfg)
    if type(cfg.setdefault('media_backfill',True)) is not bool: raise ValueError('invalid_media_backfill')
    maximum=cfg.setdefault('max_dialogs',None)
    if maximum is not None and (type(maximum) is not int or not 1<=maximum<=10000):
        raise ValueError('invalid_max_dialogs')
    cfg['history_since']=history_bound(cfg.get('history_since'))
    cfg['live_search_budget']=live_search_budget(cfg.get('live_search_budget'))
    auth=private_json(cfg['credentials_file']); permit=private_json(cfg['permit_file'])
    if not isinstance(auth,dict) or set(auth)!={'api_id','api_hash','expected_user_id'}:
        raise ValueError('invalid_telegram_credentials')
    if type(auth['api_id']) is not int or auth['api_id']<=0 or type(auth['expected_user_id']) is not int or auth['expected_user_id']<=0:
        raise ValueError('invalid_telegram_identity')
    if not isinstance(auth['api_hash'],str) or not re.fullmatch(r'[a-fA-F0-9]{32}',auth['api_hash']):
        raise ValueError('invalid_telegram_credentials')
    if not isinstance(permit,dict) or permit.get('account')!=cfg['account'] or permit.get('expected_user_id')!=auth['expected_user_id'] or permit.get('prior_clients_stopped') is not True:
        raise RuntimeError('session_handover_not_confirmed')
    return cfg,auth


def live_search_budget(value):
    """ANYM-475: per-account live-search limits {burst, refill_seconds, daily}; omitted keys keep the defaults."""
    burst,refill,daily=LIVE_BUDGETS['search']
    if value is None: return {'burst':burst,'refill_seconds':int(refill),'daily':daily}
    if not isinstance(value,dict) or set(value)-{'burst','refill_seconds','daily'}: raise ValueError('invalid_live_search_budget')
    result={'burst':value.get('burst',burst),'refill_seconds':value.get('refill_seconds',int(refill)),'daily':value.get('daily',daily)}
    for key,lo,hi in [('burst',1,10),('refill_seconds',1,3600),('daily',1,10000)]:
        if type(result[key]) is not int or not lo<=result[key]<=hi: raise ValueError('invalid_live_search_budget')
    return result


def history_bound(value):
    if value is None: return None
    if not isinstance(value,str): raise ValueError('invalid_history_since')
    try: result=datetime.fromisoformat(value)
    except ValueError: raise ValueError('invalid_history_since') from None
    if result.tzinfo is None: raise ValueError('invalid_history_since')
    return result.astimezone(timezone.utc)


def peer_kind(message):
    peer=message.peer_id
    if getattr(peer,'channel_id',None) is not None: return 'channel'
    if getattr(peer,'chat_id',None) is not None: return 'chat'
    if getattr(peer,'user_id',None) is not None: return 'user'
    raise ValueError('unsupported_telegram_peer')


class TelethonProvider:
    supports_receipts=True
    supports_owner_reads=True
    def __init__(self,cfg,auth,*,receive_updates=True):
        from telethon import TelegramClient
        # Do not let library diagnostics log payloads or credentials.
        from .security_logging import redact_library_logs
        redact_library_logs()
        logger=logging.getLogger('personal_os.telegram.transport')
        self.client=TelegramClient(cfg['session_file'],auth['api_id'],auth['api_hash'],
            request_retries=0,connection_retries=3,auto_reconnect=True,
            flood_sleep_threshold=0,raise_last_call_error=True,base_logger=logger,
            catch_up=False,receive_updates=receive_updates)
        self.expected_user_id=auth['expected_user_id']
        self.verified_self=None
        self.governor=None
        budget=cfg.get('live_search_budget') or live_search_budget(None)
        self.live_budgets={**LIVE_BUDGETS,'search':(budget['burst'],float(budget['refill_seconds']),budget['daily'])}

    def bind_state(self,state):
        self.governor=RequestGovernor(state,budgets=getattr(self,'live_budgets',None))
        self.read_gap_state=state
        if '_read_gap_tasks' in self.__dict__:return
        # Telethon 1.x can lose updates inside one connection: its auto-reconnect does
        # not catch up, and while it fetches any (channel) difference it drops pts
        # updates such as inbox reads, or ends the fetch early. So every difference
        # fetch Telethon starts ends the read-state continuity (ANYM-275).
        self._read_gap_tasks=set()
        sender=self.client._sender
        reconnected=sender._auto_reconnect_callback
        async def auto_reconnect():
            await self.record_read_gap()
            if reconnected:return await reconnected()
        sender._auto_reconnect_callback=auto_reconnect
        box=self.client._message_box;base=type(box);provider=self
        class WatchedBox(base):
            # MessageBox has __slots__: an empty-slot subclass keeps its layout.
            __slots__=()
            def get_difference(self):
                request=base.get_difference(self)
                if request is not None:provider.read_gap_soon()
                return request
            def get_channel_difference(self,*args):
                # Also covers the early end when the channel's access hash is missing.
                if any(isinstance(entry,int) for entry in self.getting_diff_for):provider.read_gap_soon()
                return base.get_channel_difference(self,*args)
        box.__class__=WatchedBox

    def read_gap_soon(self):
        # Called inside Telethon's update loop: never block or raise there.
        try:task=asyncio.get_running_loop().create_task(self.record_read_gap())
        except RuntimeError:return
        self._read_gap_tasks.add(task);task.add_done_callback(self._read_gap_tasks.discard)

    async def record_read_gap(self):
        try:await asyncio.to_thread(self.read_gap_state.update_gap)
        except Exception as exc:logging.getLogger(__name__).warning('read_state_gap_failed: %s',type(exc).__name__)

    async def rpc(self,operation,phase):
        governor=getattr(self,'governor',None)
        return await governor.call(operation,phase) if governor else await operation()

    def subscribe(self,capture,deleted,receipts=None,*,owner_reads=None):
        from telethon import events,types
        self.client.add_event_handler(capture,events.NewMessage())
        self.client.add_event_handler(capture,events.MessageEdited())
        self.client.add_event_handler(deleted,events.MessageDeleted())
        if receipts is not None:
            self.client.add_event_handler(receipts,events.Raw(types.UpdateReadHistoryOutbox))
        if owner_reads is not None:
            self.client.add_event_handler(owner_reads,events.Raw([types.UpdateReadHistoryInbox,types.UpdateReadChannelInbox]))

    async def connect(self):
        self.verified_self=None
        await self.rpc(self.client.connect,'provider_connect')
        # is_user_authorized() catches every RPCError, including FloodWait, and
        # caches False. get_me() catches only UnauthorizedError: required waits
        # must remain visible to the governor instead of asking for new pairing.
        me=await self.rpc(self.client.get_me,'provider_connect')
        if me is None:raise RuntimeError('telegram_pairing_required')
        if me.id!=self.expected_user_id: raise RuntimeError('telegram_identity_mismatch')
        self.verified_self=me
        await self.rpc(self.client.catch_up,'provider_connect')

    async def disconnect(self): await self.client.disconnect()

    def self_provider_id(self):
        # Set by connect() only after get_me() matched expected_user_id.
        me=self.verified_self
        return str(me.id) if me is not None and type(getattr(me,'id',None)) is int else None

    async def wait_disconnected(self): await self.client.disconnected

    async def send_text(self,job):
        from .outgoing import SendRejected,dispatch_payload
        from telethon import types
        from telethon.errors import FloodError
        report=job.get('_provider_phase')
        # The shared rate gate may hold this send behind history or media.
        if report is not None:report('waiting')
        payload=dispatch_payload(job,'telegram')
        options={'parse_mode':None,'link_preview':payload['link_preview'],
            'reply_to':int(job['reply_provider_id']) if job.get('reply_provider_id') else None,
            'clear_draft':False}
        if job.get('render_artifact') is not None:
            constructors={'bold':types.MessageEntityBold,'italic':types.MessageEntityItalic,
                'strikethrough':types.MessageEntityStrike,'underline':types.MessageEntityUnderline,
                'spoiler':types.MessageEntitySpoiler,'code':types.MessageEntityCode,
                'pre':types.MessageEntityPre,'blockquote':types.MessageEntityBlockquote,
                'text_link':types.MessageEntityTextUrl}
            entities=[]
            for entity in payload['entities']:
                arguments={'offset':entity['offset'],'length':entity['length']}
                if entity['type']=='text_link':arguments['url']=entity['url']
                elif entity['type']=='pre':arguments['language']=entity.get('language','')
                entities.append(constructors[entity['type']](**arguments))
            options['formatting_entities']=entities
        async def send():
            # Recheck after waiting for the shared rate/cooldown gate.
            if datetime.now(timezone.utc).timestamp()>=job['expires_at']:
                raise SendRejected('send_expired')
            if job.get('_preflight') is not None:await job['_preflight']()
            if report is not None:report('started')
            try:message=await self.client.send_message(int(job['provider_chat']),payload['text'],**options)
            except FloodError:
                # 420 FLOOD answers reject the request before Telegram acts on it.
                if report is not None:report('refused')
                raise
            if not message or str(message.chat_id)!=job['provider_chat']:
                raise RuntimeError('send_receipt_mismatch')
            return {'provider_message_id':str(message.id),'message':message}
        return await self.rpc(send,'provider_send')

    async def execute_operation(self,job):
        from .telegram_operations import execute
        return await execute(self.client,job,verified_self=self.verified_self,governor=self.governor)

    async def dialogs(self,limit):
        result=[]
        async for batch in self.dialog_batches(limit): result.extend(batch)
        return result

    async def dialog_batches(self,limit):
        # Telethon's pinned RequestIter keeps its next-page request unchanged when
        # _load_next_chunk raises FloodWait. Retry THIS iterator after the durable
        # pause, not a fresh traversal. Catalog batches already yielded stay saved.
        iterator=self.client.iter_dialogs(limit=limit).__aiter__()
        batch=[];page_observed_at=datetime.now(timezone.utc).isoformat()
        while True:
            try:
                buffer=getattr(iterator,'buffer',None)
                if buffer is None or getattr(iterator,'index',0)==len(buffer):
                    dialog=await self.rpc(iterator.__anext__,'telegram_discovery')
                    page_observed_at=datetime.now(timezone.utc).isoformat()
                else:
                    # Consuming the already downloaded buffer makes no RPC.
                    dialog=await iterator.__anext__()
            except StopAsyncIteration:break
            except ProviderRateLimited:
                if batch:yield batch;batch=[]
                continue
            kind='channel' if dialog.is_channel else ('user' if dialog.is_user else 'chat')
            chat_type='private' if dialog.is_user else ('group' if dialog.is_group else 'channel')
            raw=getattr(dialog,'dialog',None)
            observation={'observed_at':page_observed_at,
                'unread_count':getattr(raw,'unread_count',None),
                'read_inbox_max_id':getattr(raw,'read_inbox_max_id',None),
                'is_self_chat':bool(getattr(getattr(dialog,'entity',None),'is_self',False)),
                'basis':'telegram_dialog'}
            batch.append((str(dialog.id),kind,dialog.name or str(dialog.id),chat_type,observation))
            if len(batch)>=100:yield batch;batch=[]
        if batch:yield batch

    async def page(self,chat,*,limit,min_id=0,before=0,reverse=False):
        from telethon.errors import (AuthKeyNotFound,InvalidBufferError,InvalidChecksumError,ReadCancelledError,
            RPCError,SecurityError)
        async def read():
            try:
                return [m async for m in self.client.iter_messages(int(chat),limit=limit,min_id=min_id,
                                                                  offset_id=before,reverse=reverse)]
            # RPC errors are classified by the caller. The local session database, memory, a lost or
            # cancelled connection and the transport's authorization key or crypto are not one chat's
            # error and propagate as before (ANYM-293).
            except (RPCError,OSError,EOFError,sqlite3.Error,MemoryError,AuthKeyNotFound,InvalidBufferError,
                    InvalidChecksumError,ReadCancelledError,SecurityError):raise
            except Exception as exc:
                # Entity resolution, an unknown TL constructor and similar library errors of this
                # one read; the governor's own state calls stay outside and still propagate.
                raise ProviderPageError(type(exc).__name__) from exc
        return await self.rpc(read,'telegram_history')

    async def chunks(self,source,descriptor):
        from telethon import types,utils
        from telethon.errors import FileReferenceExpiredError,FilerefUpgradeNeededError,FloodWaitError
        try:
            message=await self.rpc(lambda:self.client.get_messages(int(source['provider_chat']),
                ids=int(source['provider_id'])),'telegram_media')
            if not message: raise ProviderUnavailable('media_unavailable')
            current=telegram_message(source['account'],message)
            if descriptor not in current['media']: raise ProviderUnavailable('media_reference_changed')
            # A link preview exposes .document/.photo too, but its outer
            # MessageMediaWebPage is not a downloadable Telethon location.
            # Download the same concrete object that the adapter described.
            media=getattr(message,'document',None) or getattr(message,'photo',None)
            if media is None: raise ProviderUnavailable('media_unavailable')
            download_options={'request_size':512*1024}
            if isinstance(media,types.Photo):
                # File(Photo).size describes the heaviest representation, but
                # iter_download(Photo) blindly chooses sizes[-1]. Ordering is
                # not a guarantee of equal byte length. Keep the descriptor and
                # download the matching full representation explicitly.
                sizes=[s for s in media.sizes if isinstance(s,(types.PhotoSize,types.PhotoSizeProgressive))
                       and (descriptor['bytes'] or 0)>0 and utils._photo_size_byte_count(s)==descriptor['bytes']]
                if not sizes: raise ProviderUnavailable('media_unavailable')
                size=sizes[-1]
                location=types.InputPhotoFileLocation(id=media.id,access_hash=media.access_hash,
                    file_reference=media.file_reference,thumb_size=size.type)
                download_options.update(file_size=descriptor['bytes'],dc_id=media.dc_id)
            else: location=media
            stream=self.client.iter_download(location,**download_options)
            try:
                iterator=stream.__aiter__()
                while True:
                    try:chunk=await self.rpc(iterator.__anext__,'telegram_media')
                    except StopAsyncIteration:break
                    yield chunk
            finally: await stream.close()
        except FloodWaitError as exc: raise ProviderRateLimited(exc.seconds) from None
        except (FileReferenceExpiredError,FilerefUpgradeNeededError):
            # Even a freshly fetched preview can contain an expired reference.
            # Retry this job with a fresh message on the next bounded attempt;
            # one unavailable original must not stop every chat's collection.
            raise OSError('media_reference_expired') from None


def media_deadline(size):
    """Whole-transfer bound that still lets a large original finish on a paced session."""
    # 512 KiB chunks at most one paced request per second, shared with history.
    expected=(size or 0)/(256*1024)
    return max(3600,min(6*3600,int(expected*1.5)+600))


# Deterministic failures of a pure parser on malformed provider input. Resource,
# I/O and unexpected errors are not among them and still fail capture closed.
PARSE_ERRORS=(ValueError,TypeError,KeyError,AttributeError,IndexError,RecursionError)
# Live capture backpressure (ANYM-291): at most CAPTURE_WRITES spool writes run at once;
# further callbacks wait in arrival order. More than CAPTURE_WAITERS waiting callbacks, or one
# wait longer than CAPTURE_WAIT_SECONDS, fails the collector closed with capture_overload.
CAPTURE_WRITES=16; CAPTURE_WAITERS=512; CAPTURE_WAIT_SECONDS=30
# History reconcile of one chat (ANYM-293): one chat may fail with a transient server error at most
# CHAT_ATTEMPTS times per pass, over all its pages; successful pages do not count. A failed page is
# retried after CHAT_RETRY_DELAYS seconds while the budget lasts.
CHAT_ATTEMPTS=3; CHAT_RETRY_DELAYS=(1,2,4)
# Out-of-window backfill: at most one original per MEDIA_BACKFILL_INTERVAL seconds, and a
# MEDIA_BACKFILL_IDLE pause once nothing is left, on top of the governor's request pacing.
MEDIA_BACKFILL_INTERVAL=15; MEDIA_BACKFILL_IDLE=600


def chat_error_evidence(error):
    """Class name and Telegram's fixed RPC code of a chat's provider error; never exception text."""
    name=error.error_class if isinstance(error,ProviderPageError) else type(error).__name__
    evidence={'error_class':name if type(name) is str and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',name) else 'unknown'}
    message=getattr(error,'message',None) if type(error).__module__.startswith('telethon.') else None
    if type(message) is str and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',message):evidence['rpc_message']=message
    return evidence


class TelegramCollector:
    state_class=CollectorState
    coverage_code='telegram_history_partial'
    connect_timeout=60
    retry_connect=False
    database_retry=2
    def __init__(self,archive,account,provider,spool,*,batch_size=100,interval=60,max_dialogs=None,
                 writer=None,owner=None,lease_ttl=60,heartbeat_interval=15,history_since=None,
                 live_writer=None,live_window_seconds=86400,media_prefetch_days=None,media_backfill=False):
        self.archive,self.account,self.provider,self.spool=archive,account,provider,spool
        self.batch_size,self.interval,self.max_dialogs=batch_size,interval,max_dialogs
        self.history_since=history_since
        self.media_prefetch_days=validate_prefetch_days(media_prefetch_days)
        self.writer=writer or BlobWriter(archive.store.files,reserve_bytes=MEDIA_RESERVE_BYTES)
        self.live_writer,self.live_window_seconds=live_writer,live_window_seconds
        self.media_backfill=media_backfill is True; self._backfill_due=0
        self.owner=owner or 'telegram-'+secrets.token_hex(12)
        self.lease_ttl,self.heartbeat_interval=lease_ttl,heartbeat_interval
        self.state=None; self.stopping=asyncio.Event(); self.failed=asyncio.Event()
        self.capture_count=0; self.failure_code=None; self.policy=None
        self.capture_waiters=deque(); self.capture_wait_seconds=CAPTURE_WAIT_SECONDS
        self._dialog_cache=None; self._next_discovery=0; self._history_spool=None
        self.invalid_messages=0; self._lease_valid_until=None
        self.failure_detail=None; self.rejected={}; self.rejected_messages=[]
        self.chat_retry_delays=CHAT_RETRY_DELAYS; self._chat_failures=0; self._logged_chat_errors=set()

    @property
    def history_spool(self):
        # Replayed history pages keep capture durability, but a pending page is
        # not live capture lag. Created lazily beside the live spool.
        if getattr(self,'_history_spool',None) is None:
            from .spool import DurableSpool
            live=self.spool
            self._history_spool=DurableSpool(live.path/'history',validator=live.validator,
                max_bytes=live.max_bytes,max_entries=live.max_entries,reserve_bytes=live.reserve_bytes)
        return self._history_spool

    def fail(self,code,detail=None):
        self.failure_code=code; self.failure_detail=detail; self.failed.set()

    def capture_failed(self,exc):
        # Capacity, I/O and anything unexpected fail closed; only the cause class is kept.
        detail=(str(exc) if isinstance(exc,RuntimeError) and str(exc) in ('spool_full','disk_reserve')
            else 'spool_io_error' if isinstance(exc,OSError) else 'capture_error')
        self.fail('durable_capture_failed',detail)

    async def capture_slot(self):
        """Take one capture write slot, waiting in arrival order; False when the collector stops instead."""
        if self.capture_count<CAPTURE_WRITES and not self.capture_waiters:
            self.capture_count+=1; return True
        if len(self.capture_waiters)>=CAPTURE_WAITERS: self.fail('capture_overload','waiters_limit'); return False
        waiter=asyncio.get_running_loop().create_future(); self.capture_waiters.append(waiter)
        try:
            async with asyncio.timeout(self.capture_wait_seconds): await waiter
        except BaseException as exc:
            # A slot granted in the same tick as the timeout or a cancel is kept or passed on, never leaked.
            granted=waiter.done() and not waiter.cancelled()
            if not granted:
                with suppress(ValueError): self.capture_waiters.remove(waiter)
                self.grant_captures()
            if not isinstance(exc,TimeoutError):
                if granted: self.release_capture()
                raise
            if not granted: self.fail('capture_overload','slot_wait_timeout'); return False
        if self.stopping.is_set() or self.failed.is_set(): self.release_capture(); return False
        return True

    def release_capture(self):
        self.capture_count-=1; self.grant_captures()

    def grant_captures(self):
        # Free slots go to the oldest waiters; a granted waiter already counts as a running write.
        while self.capture_count<CAPTURE_WRITES and self.capture_waiters:
            waiter=self.capture_waiters.popleft()
            if not waiter.done(): waiter.set_result(None); self.capture_count+=1

    def reject(self,code,chat=None,message_id=None):
        """Count one deterministically unstorable live item; a restart cannot fix it."""
        # Only fixed codes reach status, never text that merely looks like one.
        if code not in LIVE_REJECT_CODES:code='invalid_capture'
        self.rejected[code]=self.rejected.get(code,0)+1
        if (chat is not None and type(message_id) is int and 0<message_id<2**63
                and re.fullmatch(r'-?[0-9]{1,20}',chat) and self.policy and self.state.allows(self.policy,chat)):
            # Identity only, so reconciliation or an operator can re-fetch it later.
            self.rejected_messages=[*self.rejected_messages,{'chat':chat,'message_id':message_id,
                'reason_code':code,'observed_at':datetime.now(timezone.utc).isoformat()}][-20:]

    async def capture(self,event):
        # Bound pending fsync work: callbacks wait for a slot. Overload fails closed; reconciliation
        # after restart is required rather than claiming dropped callbacks succeeded.
        if self.stopping.is_set() or self.failed.is_set(): return
        if not await self.capture_slot(): return
        message=chat=None
        try:
            message=event.message; chat=str(getattr(message,'chat_id',None))
            try:
                e=telegram_message(self.account,message,verified_self=getattr(self.provider,'verified_self',None))
                kind=peer_kind(message)
            except ValueError as exc:
                if isinstance(exc,OSError):raise
                # Deterministically unstorable; reconciliation skips it the same way.
                self.invalid_messages+=1; self.reject('invalid_provider_message',chat,getattr(message,'id',None)); return
            if not self.state.allows(self.policy,e['chat']): return
            await asyncio.to_thread(self.spool.put,{'kind':'message','peer_kind':kind,'envelope':e,
                'captured_at':datetime.now(timezone.utc).isoformat()})
        except InvalidCapture as exc: self.reject(str(exc),chat,getattr(message,'id',None))
        except Exception as exc: self.capture_failed(exc)
        finally: self.release_capture()

    async def deleted(self,event):
        if self.stopping.is_set() or self.failed.is_set(): return
        if not await self.capture_slot(): return
        try:
            chat=str(event.chat_id) if event.chat_id is not None else ''
            if chat and not self.state.allows(self.policy,chat): return
            ids=list(event.deleted_ids); observed=datetime.now(timezone.utc).isoformat()
            # One update may delete more ids than a record holds; store it in parts.
            for start in range(0,max(1,len(ids)),1000):
                try:
                    await asyncio.to_thread(self.spool.put,{'kind':'telegram_delete','account':self.account,
                        'chat':chat,'ids':ids[start:start+1000],'observed_at':observed})
                except InvalidCapture as exc: self.reject(str(exc))
        except Exception as exc: self.capture_failed(exc)
        finally: self.release_capture()

    async def owner_read(self,event):
        if self.stopping.is_set() or self.failed.is_set():return
        pending=False
        try:
            from .reply_state import telegram_read_capture
            try:
                capture=telegram_read_capture(self.account,event,verified_self=getattr(self.provider,'verified_self',None),
                    expected_user_id=getattr(self.provider,'expected_user_id',None))
            except PARSE_ERRORS as exc:
                if isinstance(exc,OSError):raise
                self.reject('read_parse_failed');return
            if capture is None or not self.policy or self.policy.get('account')!=self.account:return
            if not self.state.allows(self.policy,capture['chat']):return
            if not await self.capture_slot():return
            pending=True
            await asyncio.to_thread(self.spool.put,capture)
        except InvalidCapture as exc:self.reject(str(exc))
        except Exception as exc:self.capture_failed(exc)
        finally:
            if pending:self.release_capture()

    async def receipt(self,event):
        if self.stopping.is_set() or self.failed.is_set(): return
        pending=False
        try:
            from .telegram_receipts import telegram_receipt
            try:
                capture=telegram_receipt(self.account,event,
                    verified_self=getattr(self.provider,'verified_self',None),
                    expected_user_id=getattr(self.provider,'expected_user_id',None))
            except PARSE_ERRORS as exc:
                if isinstance(exc,OSError): raise
                self.reject('receipt_parse_failed'); return
            if capture is None: return
            if not self.policy or self.policy.get('account')!=self.account: return
            if getattr(self.state,'account',self.account)!=self.account: return
            if not self.state.allows(self.policy,capture['chat']): return
            if not await self.capture_slot(): return
            pending=True
            await asyncio.to_thread(self.spool.put,capture)
        except InvalidCapture as exc: self.reject(str(exc))
        except Exception as exc: self.capture_failed(exc)
        finally:
            if pending:self.release_capture()

    async def commit_page(self,messages,chat,title,kind,origin):
        ids=[]
        for message in messages:
            if str(message.chat_id)!=chat: raise RuntimeError('provider_chat_mismatch')
            try:
                e=telegram_message(self.account,message,chat_title=title,origin=origin,
                    verified_self=getattr(self.provider,'verified_self',None))
            except ValueError:
                # One unstorable message must not stop history for the chat or account.
                self.invalid_messages+=1; ids.append(message.id); continue
            item={'kind':'message','peer_kind':kind,'envelope':e}
            try:name=await asyncio.to_thread(self.history_spool.put,item)
            except InvalidCapture:
                # The same rejection a live callback counts; capacity and I/O still raise.
                self.invalid_messages+=1; ids.append(message.id); continue
            await asyncio.to_thread(self.state.apply,item)
            # Committed directly; draining the same bytes again would only repeat the work.
            await asyncio.to_thread(self.history_spool.discard,name)
            ids.append(message.id)
        return ids

    async def chat_page(self,chat,**query):
        """One provider history page of one chat, classified per ANYM-293.

        Rate limits, session-terminal errors, unavailable peers and transport loss propagate as before.
        Transient server errors are retried here from a budget of CHAT_ATTEMPTS transient failures per
        chat and pass, shared by all pages of the chat; they and any other provider error then fail only
        this chat."""
        from telethon.errors import (AuthKeyDuplicatedError,ChannelPrivateError,ChatAdminRequiredError,FloodWaitError,
            PeerIdInvalidError,RPCError,ServerError,TimedOutError,UnauthorizedError)
        while True:
            try:return await self.provider.page(chat,**query)
            # A duplicated authorization key is session-terminal like UnauthorizedError (Telegram 406).
            except (FloodWaitError,UnauthorizedError,AuthKeyDuplicatedError,ChannelPrivateError,ChatAdminRequiredError,
                    PeerIdInvalidError):raise
            except (ServerError,TimedOutError,TimeoutError) as exc:
                self._chat_failures=getattr(self,'_chat_failures',0)+1
                if self._chat_failures>=CHAT_ATTEMPTS:raise ChatPageFailed('provider_temporarily_unavailable',exc) from exc
                await asyncio.sleep(self.chat_retry_delays[self._chat_failures-1])
            except (RPCError,ProviderPageError) as exc:raise ChatPageFailed('provider_chat_error',exc) from exc

    def chat_failed(self,chat,failure):
        """Print one content-free diagnostic per chat and process: class name and frames, never exception text."""
        if chat in self._logged_chat_errors:return
        self._logged_chat_errors.add(chat)
        error=failure.error;cause=error.__cause__ if isinstance(error,ProviderPageError) else error
        frames=[f'{os.path.basename(f.filename)}:{f.lineno}:{f.name}'
                for f in traceback.extract_tb(getattr(cause,'__traceback__',None))][-12:]
        print(json.dumps({'event':'telegram_chat_error','chat':chat,'code':failure.code,
            **chat_error_evidence(error),'frames':frames}),flush=True)

    async def sync_chat(self,chat,kind,title):
        state=await asyncio.to_thread(self.state.chat,chat,kind)
        if not state['initialized']:
            recent=await self.chat_page(chat,limit=self.batch_size)
            kept=self.in_window(recent)
            ids=await self.commit_page(kept,chat,title,kind,'backfill')
            # Keep the cursor ABOVE skipped old messages so expanding the
            # window later does not lose the rest of this boundary page.
            boundary=min(ids) if ids else (max(m.id for m in recent)+1 if recent else None)
            await asyncio.to_thread(self.state.checkpoint,chat,high_water=max((m.id for m in recent),default=0),
                history_before=boundary,exhausted=len(recent)<self.batch_size and len(kept)==len(recent),
                initialized=True,window_complete=len(kept)<len(recent),head_complete=True)
            return
        new=await self.chat_page(chat,limit=self.batch_size,min_id=state['high_water'],reverse=True)
        await self.commit_page(self.in_window(new),chat,title,kind,'reconcile')
        if new: await asyncio.to_thread(self.state.checkpoint,chat,high_water=max(m.id for m in new))
        # Recent overlap catches edits after the library's update state advanced
        # ahead of durable capture. It cannot prove absence of older edits/deletes.
        recent=await self.chat_page(chat,limit=self.batch_size)
        await self.commit_page(self.in_window(recent),chat,title,kind,'reconcile')
        if len(new)<self.batch_size:
            await asyncio.to_thread(self.state.checkpoint,chat,head_complete=True)
        if not state['history_exhausted'] and not state['window_complete'] and state['history_before']:
            older=await self.chat_page(chat,limit=self.batch_size,before=state['history_before'])
            kept=self.in_window(older)
            ids=await self.commit_page(kept,chat,title,kind,'backfill')
            await asyncio.to_thread(self.state.checkpoint,chat,history_before=min(ids) if ids else None,
                                    exhausted=len(older)<self.batch_size and len(kept)==len(older),
                                    window_complete=len(kept)<len(older))

    def in_window(self,messages):
        return [m for m in messages if self.history_since is None or m.date>=self.history_since]

    async def sync_once(self):
        from telethon.errors import ChannelPrivateError,ChatAdminRequiredError,PeerIdInvalidError,FloodWaitError
        self.policy=await asyncio.to_thread(self.state.policy)
        if self._dialog_cache is None or time.monotonic()>=self._next_discovery:
            await asyncio.to_thread(self.state.status,'receiving','telegram_discovery')
            await asyncio.to_thread(self.state.discovery_started)
            dialogs=[]
            if hasattr(self.provider,'dialog_batches'):
                async for batch in self.provider.dialog_batches(self.max_dialogs):
                    await asyncio.to_thread(self.state.inventory,batch,self.history_since,discovery=True)
                    dialogs.extend(batch)
            else:
                # Small injected/test providers may expose only a single page.
                dialogs=await self.provider.dialogs(self.max_dialogs)
                await asyncio.to_thread(self.state.inventory,dialogs,self.history_since,discovery=True)
            await asyncio.to_thread(self.state.discovery_finished,
                                    self.max_dialogs is not None and len(dialogs)>=self.max_dialogs)
            self._dialog_cache=dialogs
            # Do not refetch an entire account catalog on every history pass.
            self._next_discovery=time.monotonic()+900
            print(json.dumps({'event':'telegram_discovery','dialogs':len(dialogs),
                'limit':self.max_dialogs,'history_since':self.history_since.isoformat() if self.history_since else None}),flush=True)
        else:dialogs=self._dialog_cache
        order=await asyncio.to_thread(self.state.inventory,dialogs,self.history_since)
        by_id={d[0]:d for d in dialogs}
        seen=set(); unavailable=False
        for chat in order:
            if chat not in by_id: continue
            chat,kind,title=by_id[chat][:3]
            if not self.state.allows(self.policy,chat): continue
            await asyncio.to_thread(self.state.attempted,chat)
            self._chat_failures=0
            try:
                await self.sync_chat(chat,kind,title); seen.add(chat)
            except (ChannelPrivateError,ChatAdminRequiredError,PeerIdInvalidError):
                # A removed/private peer must not starve later allowed chats.
                # Rate limits, ownership loss and DB faults still propagate.
                unavailable=True
                await asyncio.to_thread(self.state.failed,chat,'provider_unavailable')
            except FloodWaitError:
                await asyncio.to_thread(self.state.failed,chat,'rate_limited')
                raise
            except ChatPageFailed as failure:
                # One chat's provider error marks only that chat; the pass goes on (ANYM-293).
                unavailable=True
                self.chat_failed(chat,failure)
                await asyncio.to_thread(self.state.failed,chat,failure.code,chat_error_evidence(failure.error))
        missing=set(self.policy['include_chats'])-set(self.policy['exclude_chats'])-seen
        code='dialog_discovery_limited' if self.max_dialogs is not None and len(dialogs)>=self.max_dialogs else 'telegram_history_partial'
        if missing or unavailable: code='configured_chat_unavailable'
        await asyncio.to_thread(self.state.status,'degraded' if missing or unavailable else 'receiving',code)

    async def reconcile_loop(self):
        from telethon.errors import FloodWaitError
        from .telegram_identity import repair_sent_senders
        await asyncio.to_thread(repair_sent_senders,self.archive,self.account,self.state.lease,
            getattr(self.provider,'verified_self',None))
        delay=self.database_retry
        while True:
            try: await self.sync_once()
            except (FloodWaitError,ProviderRateLimited) as exc:
                if isinstance(exc,FloodWaitError):await asyncio.to_thread(self.state.defer,max(1,exc.seconds))
                await asyncio.to_thread(self.state.status,'degraded','rate_limited')
                governor=getattr(self.provider,'governor',None)
                if governor:await governor.wait()
                else:await asyncio.sleep(max(exc.seconds,1))
                continue
            except database_errors():
                # Checkpoints are committed per page; retry instead of reconnecting the provider.
                await asyncio.sleep(delay); delay=min(60,delay*2); continue
            delay=self.database_retry
            await asyncio.sleep(self.interval)

    async def drain_loop(self):
        import psycopg
        from psycopg_pool import PoolTimeout
        while True:
            try:
                await self.drain_captures([self.spool],[self.history_spool])
                await asyncio.to_thread(self.state.apply_tombstones)
            except (psycopg.OperationalError,PoolTimeout):
                # Capture remains durable; the independent heartbeat stops the
                # provider if database ownership cannot be renewed in time.
                await asyncio.sleep(1)
            await asyncio.sleep(.25)

    async def drain_captures(self,spools,history_spools=()):
        last=None;count=0
        def apply(item):
            nonlocal last,count
            self.state.apply(item);last=item;count+=1
        for spool in spools:await asyncio.to_thread(spool.drain,apply,100)
        # History pages were applied inline when fetched. Replaying them is
        # idempotent and is neither live progress nor live backlog.
        for spool in history_spools:await asyncio.to_thread(spool.drain,self.state.apply,100)
        publish=getattr(self.state,'publish_receipts',None)
        if callable(publish):await asyncio.to_thread(publish,limit=100)
        if count or time.monotonic()>=getattr(self,'_next_capture_status',0):
            def pending(group):return min(20000,sum(sum(1 for _ in s.path.glob('*.json')) for s in group))
            extra={'history_pending':await asyncio.to_thread(pending,history_spools)} if history_spools else {}
            owned=[*spools,*history_spools,*([self.send_receipts] if getattr(self,'send_receipts',None) is not None else [])]
            quarantined=await asyncio.to_thread(lambda:sum(s.quarantined() for s in owned))
            invalid,self.invalid_messages=self.invalid_messages,0
            rejected,self.rejected=getattr(self,'rejected',{}),{}
            if rejected:extra['rejected']=rejected
            skipped,self.rejected_messages=getattr(self,'rejected_messages',[]),[]
            if skipped:extra['rejected_messages']=skipped
            try:
                await asyncio.to_thread(self.state.capture_progress,last,count,await asyncio.to_thread(pending,spools),
                    quarantined=min(20000,quarantined),invalid=invalid,**extra)
            except BaseException:
                # Skipped items have no spool record to replay; keep their counts for the retry.
                self.invalid_messages+=invalid
                for code,number in rejected.items():self.rejected[code]=self.rejected.get(code,0)+number
                self.rejected_messages=[*skipped,*self.rejected_messages][-20:]
                raise
            self._next_capture_status=time.monotonic()+15

    async def renew_lease(self):
        """Renew ownership, retrying transient faults only while the lease is still ours.

        The database enforces expiry, but a half-open connection can block a
        call far longer than the lease. Past the local deadline another owner
        may hold the account, so this client must stop rather than wait.
        """
        if self._lease_valid_until is None:
            self._lease_valid_until=time.monotonic()+self.lease_ttl-min(10,self.lease_ttl/4)
        delay=1
        while True:
            remaining=self._lease_valid_until-time.monotonic()
            if remaining<=0:raise RuntimeError('collector_lease_lost')
            started=time.monotonic()
            try:
                await asyncio.wait_for(asyncio.to_thread(self.archive.renew,self.account,self.owner,
                    self.state.lease[1],self.lease_ttl),remaining)
            except TimeoutError:raise RuntimeError('collector_lease_lost') from None
            except database_errors():
                await asyncio.sleep(min(delay,max(0,self._lease_valid_until-time.monotonic())))
                delay=min(5,delay*2); continue
            # Conservative: the server deadline starts no earlier than this call.
            self._lease_valid_until=started+self.lease_ttl-min(10,self.lease_ttl/4)
            return

    async def refresh_policy(self):
        try:self.policy=await asyncio.to_thread(self.state.policy)
        except database_errors():pass  # Keep the last committed policy; DB writes stay fenced.

    async def heartbeat(self):
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            await self.renew_lease()
            await self.refresh_policy()
            await self.publish_disk_floors()

    async def media_once(self):
        governor=getattr(self.provider,'governor',None)
        if governor:await governor.wait()
        priority=({'live_writer':self.live_writer,'live_window_seconds':self.live_window_seconds}
                  if self.live_writer is not None else {})
        job=await asyncio.to_thread(self.archive.claim_media,self.owner,account=self.account,
                                    collector_lease=self.state.lease,since=self.history_since,writer=self.writer,
                                    media_prefetch_days=self.media_prefetch_days,**priority)
        if not job and self.media_backfill and time.monotonic()>=self._backfill_due:
            # Old voice notes and small files, one at a time between idle window claims,
            # paced so live media and history keep the shared provider budget.
            job=await asyncio.to_thread(self.archive.claim_media,self.owner,account=self.account,
                collector_lease=self.state.lease,since=self.history_since,writer=self.writer,
                media_prefetch_days=self.media_prefetch_days,backfill=True,**priority)
            self._backfill_due=time.monotonic()+(MEDIA_BACKFILL_INTERVAL if job else MEDIA_BACKFILL_IDLE)
        if not job: return False
        writer=self.live_writer if job.get('_live_media') else self.writer
        async def renew():
            while True:
                await asyncio.sleep(30)
                await asyncio.to_thread(self.archive.renew_media,job,collector_lease=self.state.lease)
        async def transfer():
            source=await asyncio.to_thread(self.archive.media_source,job,collector_lease=self.state.lease)
            stream=self.provider.chunks(source,job['descriptor'])
            try:
                return await writer.write_async(stream,expected_bytes=job['descriptor']['bytes'],
                    reservation=job.get('_storage_reservation'))
            finally:
                if hasattr(stream,'aclose'): await stream.aclose()
        renewal=asyncio.create_task(renew()); download=asyncio.create_task(transfer())
        try:
            done,_=await asyncio.wait([renewal,download],return_when=asyncio.FIRST_COMPLETED,
                timeout=media_deadline(job['descriptor'].get('bytes')))
            if not done: raise TimeoutError('media_timeout')
            if renewal in done: renewal.result()
            blob=await download
            for attempt in range(3):
                # The original is already verified on disk; do not repeat the provider transfer.
                try:
                    await asyncio.to_thread(self.archive.finish_media,job,blob,writer,collector_lease=self.state.lease)
                    break
                except database_errors():
                    if attempt==2:raise
                    await asyncio.sleep(1+attempt)
        except (ProviderUnavailable,ProviderRateLimited,OSError,ValueError,RuntimeError,TimeoutError) as exc:
            if str(exc)=='media_lease_lost': return True
            if str(exc)=='collector_lease_lost': raise
            if isinstance(exc,ProviderUnavailable): code,permanent='unavailable',True
            elif isinstance(exc,ProviderRateLimited): code,permanent='rate_limited',False
            elif str(exc)=='media_size_limit': code,permanent='size_limit',True
            elif str(exc)=='disk_reserve': code,permanent='disk_reserve',False
            elif isinstance(exc,OSError) and exc.errno in (errno.ENOSPC,errno.EDQUOT):
                code,permanent='disk_reserve',False
            elif isinstance(exc,OSError) and (exc.filename is not None or exc.errno==errno.EROFS):
                # A missing directory, permission or read-only mount is local
                # storage, not the provider network.
                code,permanent='storage_unavailable',False
            elif str(exc) in ('media_size_mismatch','media_hash_mismatch','blob_corrupt'):
                code,permanent='integrity_error',False
            else: code,permanent='network_error',False
            if self.retry_connect and str(exc) in MEDIA_ERRORS:
                code=str(exc)
                permanent=code in ('media_unavailable','media_reference_changed','unsupported_media_type',
                    'media_size_limit','invalid_media_path')
            with suppress(RuntimeError):
                await asyncio.to_thread(self.archive.fail_media,job,code,permanent=permanent,
                    retry_after=exc.seconds if isinstance(exc,ProviderRateLimited) else None,collector_lease=self.state.lease)
        finally:
            renewal.cancel(); download.cancel()
            await asyncio.gather(renewal,download,return_exceptions=True)
            with suppress(RuntimeError,OSError):
                await asyncio.to_thread(self.archive.release_media_reservation,job)
        return True

    async def media_loop(self):
        delay=self.database_retry
        while True:
            try:active=await self.media_once()
            except database_errors():
                # An unfinished job's lease expires and it is claimed again later.
                await asyncio.sleep(delay); delay=min(60,delay*2); continue
            delay=self.database_retry
            if not active: await asyncio.sleep(1)

    def disk_floors(self):
        """Media and capture floors of this run per volume (ANYM-350)."""
        from .storage_admission import disk_floors
        places=[(w.root,w.reserve_bytes) for w in (self.writer,self.live_writer) if w is not None]
        places+=[(s.path,s.reserve_bytes) for s in (self.spool,getattr(self,'events_spool',None),
            getattr(self,'history_spool',None)) if s is not None and hasattr(s,'path') and hasattr(s,'reserve_bytes')]
        return disk_floors(places)

    async def publish_disk_floors(self):
        """Best effort, again each heartbeat: until it lands cold restores wait (ANYM-350)."""
        with suppress(Exception):
            await asyncio.to_thread(self.state.disk_floors,self.disk_floors())

    async def run(self):
        started=time.monotonic()
        epoch=await asyncio.to_thread(self.archive.acquire,self.account,self.owner,self.lease_ttl)
        self._lease_valid_until=started+self.lease_ttl-min(10,self.lease_ttl/4)
        self.state=self.state_class(self.archive,self.account,(self.owner,epoch))
        tasks=[];failure=None
        try:
            self.policy=await asyncio.to_thread(self.state.policy)
            await asyncio.to_thread(self.state.status,'starting',self.coverage_code)
            # Status evidence only: without it sync status keeps its unknown basis.
            with suppress(Exception):
                await asyncio.to_thread(self.state.media_policy,self.media_prefetch_days,self.history_since)
            await self.publish_disk_floors()
            sweep=getattr(self.writer,'sweep_abandoned',None)
            if sweep is not None:
                # Best effort: leftovers only waste disk, they never block capture.
                with suppress(OSError):await asyncio.to_thread(sweep)
            if hasattr(self.provider,'bind_state'):self.provider.bind_state(self.state)
            if getattr(self.provider,'supports_owner_reads',False) is True:
                self.provider.subscribe(self.capture,self.deleted,self.receipt,owner_reads=self.owner_read)
            elif getattr(self.provider,'supports_receipts',False) is True:
                self.provider.subscribe(self.capture,self.deleted,self.receipt)
            else:self.provider.subscribe(self.capture,self.deleted)
            # Renew ownership and honor shutdown during a slow network connect.
            tasks=[asyncio.create_task(self.heartbeat()),asyncio.create_task(self.stopping.wait())]
            connecting=asyncio.create_task(self.connect_provider())
            tasks.append(connecting)
            done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done: task.result()
            if self.stopping.is_set(): return
            if connecting not in done: raise RuntimeError('collector_lease_lost')
            tasks.remove(connecting)
            tasks.extend(asyncio.create_task(coro) for coro in
                [self.drain_loop(),self.reconcile_loop(),self.media_loop(),self.outgoing_loop(),
                 self.provider.wait_disconnected(),self.failed.wait()])
            done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done: task.result()
            if self.failed.is_set():
                stop=RuntimeError(self.failure_code); stop.detail=getattr(self,'failure_detail',None); raise stop
            if not self.stopping.is_set(): raise RuntimeError('provider_disconnected')
        except Exception as exc:
            failure=str(exc) if str(exc) in CONNECTION_ERRORS else 'provider_disconnected'
            detail=getattr(exc,'detail',None)
            with suppress(Exception):
                await asyncio.to_thread(self.state.connection_failed,failure,*((detail,) if detail else ()))
            raise
        finally:
            self.stopping.set()
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            with suppress(Exception): await asyncio.wait_for(self.provider.disconnect(),15)
            if failure is None:
                with suppress(Exception):
                    gate=await asyncio.to_thread(self.state.connection_gate)
                    if not gate['blocked'] and gate['remaining_seconds']<=0:
                        await asyncio.to_thread(self.state.phase,'connection','stopped','collector_shutdown')
                        await asyncio.to_thread(self.state.phase,'live_capture','stopped','collector_shutdown')
            with suppress(Exception): await asyncio.to_thread(self.state.status,'stopped','requires_reconciliation')
            with suppress(Exception): await asyncio.to_thread(self.archive.release,self.account,self.owner,epoch)

    async def outgoing_loop(self):
        from .outgoing import OutgoingWorker
        from .commands import CommandWorker
        worker=OutgoingWorker(self.state,self.send_outgoing,getattr(self.provider,'send_gate',None))
        commands=CommandWorker(self.state,self.execute_operation,getattr(self.provider,'send_gate',None))
        delay=self.database_retry
        while True:
            try:
                active=await worker.once()
                # Text sends get the first opportunity; only one foreground job is
                # dispatched on the existing account session at a time.
                active=await commands.once() or active
            except database_errors():
                # Dispatched rows keep their lease epoch; nothing is retried automatically.
                await asyncio.sleep(delay); delay=min(60,delay*2); continue
            delay=self.database_retry
            await asyncio.sleep(.25 if active else 1)

    async def execute_operation(self,job):
        job['_policy']=await asyncio.to_thread(self.state.policy)
        if job.get('operation')=='request_chat_history': return await self.chat_history(job)
        if job.get('operation')!='search_messages_live': return await self.provider.execute_operation(job)
        job['_live_hits']=found=[]
        answer=await self.provider.execute_operation(job)
        await self.store_live_hits(found,deadline=job.get('_deadline'))
        return answer

    async def chat_history(self,job):
        """ANYM-477: one agent-requested history page, archived as live search hits are; stored_new counts new rows."""
        parameters=job.get('parameters') or {}
        if not any(k in parameters for k in ('before_message','around_message','from_time','to_time')):
            # The default anchor is the oldest archived message, so each repeat reaches further back.
            job['provider_message_id']=await asyncio.to_thread(self.oldest_archived,job.get('chat_id'))
        job['_history']=found=[]
        answer=await self.provider.execute_operation(job)
        created=await self.store_live_hits(found,label='chat_history',deadline=job.get('_deadline'))
        data=answer.get('data')
        if isinstance(data,dict):
            data['stored_new']=created
            missed=sum(1 for _,hit,_,_ in found if not hit.get('stored'))
            if missed:
                # A later default request starts below the oldest archived message and would skip these,
                # so the receipt names the request that loads this page again (a rerun adds no duplicates):
                # the request's own anchor, the default anchor it used, or for a chat with no archived
                # message a time range over the messages not stored at the largest page.
                data['not_stored']=missed
                anchors=('before_message','around_message','from_time','to_time')
                if any(k in parameters for k in anchors):
                    data['retry_parameters']={k:v for k,v in parameters.items() if k in ('count',*anchors)}
                elif job.get('provider_message_id'):
                    data['retry_parameters']={'count':parameters.get('count',50),'before_message':
                        stable_id('msg',self.account,str(job.get('provider_chat')),str(job['provider_message_id']))}
                else:
                    dates=[m.date for m,hit,_,_ in found if m is not None and not hit.get('stored') and m.date]
                    if dates:
                        data['retry_parameters']={'count':100,
                            'from_time':min(dates).strftime('%Y-%m-%dT%H:%M:%SZ'),
                            'to_time':(max(dates)+timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%SZ')}
        return answer

    def oldest_archived(self,chat):
        with self.state.archive.store.pool.connection() as c:
            row=c.execute('''SELECT provider_id FROM messages WHERE account=%s AND chat=%s
                ORDER BY length(provider_id),provider_id LIMIT 1''',(self.account,chat)).fetchone()
        return row['provider_id'] if row else None

    async def store_live_hits(self,found,*,label='live_search',deadline=None):
        """ANYM-476: archive live search hits as backfill through the history page commit's archive apply.

        No spool: a hit that cannot be stored is reported in its receipt and is not replayed, so it never
        reaches the drain loop, fails the search or stops the collector. A rerun stores it again.
        Checkpoints and coverage stay with the history traversal. Returns the number of new messages.

        A live read's deadline (loop time) bounds it: no write starts in its last STORE_MARGIN seconds,
        every transaction ends at the deadline on the server (DeadlinePool), and a cancelled read waits
        at most STORE_GRACE for its thread, so no archive transaction holding the lease fence outlives
        the read's receipt or holds up the next send claim (ANYM-477 review F1)."""
        loop=asyncio.get_running_loop()
        state=self.state
        if deadline is not None:state=bounded_state(self.state,time.monotonic()+deadline-loop.time())
        def late():return deadline is not None and deadline-loop.time()<STORE_MARGIN
        stored=skipped=failed=created=0
        for message,hit,chat_type,title in found:
            if message is not None and not self.in_window([message]):
                hit.update(stored=False,reason='outside_history_window'); skipped+=1; continue
            if late():
                hit.update(stored=False,reason='deadline'); failed+=1; continue
            outcome=None
            try:
                if message is None: raise ValueError('uninitialized_message')
                chat=str(message.chat_id);kind=peer_kind(message)
                await finished(state.catalog,chat,kind,title or chat,chat_type,self.history_since)
                e=telegram_message(self.account,message,chat_title=title or '',origin='backfill',
                    verified_self=getattr(self.provider,'verified_self',None))
                if late():raise TimeoutError('live_store_deadline')
                outcome=(await finished(state.apply,{'kind':'message','peer_kind':kind,'envelope':e}))['outcome']
            except Exception as exc:
                log.warning('%s_store_failed error=%s',label,type(exc).__name__)
            if outcome in (None,'excluded'):
                hit.update(stored=False,reason='not_stored'); failed+=1; continue
            hit.update(stored=True,message_id=stable_id('msg',self.account,chat,str(message.id))); stored+=1
            created+=outcome=='created'
        if found: log.info('%s_store stored=%d outside_window=%d not_stored=%d',label,stored,skipped,failed)
        return created

    async def send_outgoing(self,job):
        receipt=await self.provider.send_text(job)
        # Send receipt is independent of archive commit. A capture failure must
        # never cause another send; reconciliation can recover the observation.
        try:
            from types import SimpleNamespace
            await self.capture(SimpleNamespace(message=receipt['message']))
        except Exception:
            self.fail('durable_capture_failed')
        return receipt

    def verified_self_id(self):
        # The owner's ID the provider has just verified; capabilities shows it (ANYM-470).
        # Advisory: a provider without it, or any failure here, records no ID.
        try:
            reader=getattr(self.provider,'self_provider_id',None)
            return reader() if callable(reader) else None
        except Exception:return None

    async def connect_provider(self):
        # A persisted provider pause is not a hung connection: honor it outside
        # the network timeout while run() still watches heartbeat and shutdown.
        while True:
            gate=await asyncio.to_thread(self.state.connection_gate)
            if gate['blocked'] or gate['remaining_seconds']>0:
                try:await asyncio.wait_for(self.stopping.wait(),5 if gate['blocked'] else min(5,gate['remaining_seconds']))
                except TimeoutError:continue
                return
            governor=getattr(self.provider,'governor',None)
            if governor:await governor.wait()
            try:
                await asyncio.to_thread(self.state.phase,'connection','connecting','collector_starting')
                await asyncio.wait_for(self.provider.connect(),self.connect_timeout)
                await asyncio.to_thread(self.state.phase,'connection','connected','provider_ready',
                    self_provider_id=self.verified_self_id())
                await asyncio.to_thread(self.state.phase,'live_capture','receiving','capture_ready')
                return
            except ProviderRateLimited:
                if not governor:raise
            except (RuntimeError,TimeoutError) as exc:
                code='provider_connect_timeout' if isinstance(exc,TimeoutError) else str(exc)
                if isinstance(exc,TimeoutError) and not self.retry_connect:raise TimeoutError(code) from None
                if code not in CONNECTION_ERRORS or code=='collector_lease_lost':raise
                await self.provider.disconnect()
                await asyncio.to_thread(self.state.connection_failed,code)


async def run_config(path):
    cfg,auth=configuration(path)
    session=Path(cfg['session_file'])
    try: info=session.lstat()  # A symlink is refused here, never followed.
    except OSError: raise RuntimeError('private_session_required') from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077: raise RuntimeError('private_session_required')
    # File ownership spans client construction, which opens the SQLite session.
    with session_lock(session):
        store=Store(Path(os.environ['DATABASE_URL_FILE']).read_text().strip(),
                    Path(os.environ['PROBE_FILES']),'http://collector.invalid')
        try:
            archive=Archive(store)
            with store.pool.connection() as c:
                row=c.execute('SELECT platform,synthetic FROM accounts WHERE id=%s',(cfg['account'],)).fetchone()
                if not row or row['platform']!='telegram' or row['synthetic']: raise RuntimeError('live_account_required')
            spool=CaptureSpool(cfg['spool_directory'],reserve_bytes=cfg['disk_reserve_bytes'])
            live_writer=(BlobWriter(store.files,max_bytes=cfg['media_live_max_bytes'],
                reserve_bytes=cfg['media_live_disk_reserve_bytes'])
                if cfg['media_live_disk_reserve_bytes'] is not None else None)
            worker=TelegramCollector(archive,cfg['account'],TelethonProvider(cfg,auth),spool,
                batch_size=cfg['batch_size'],interval=cfg['scan_interval_seconds'],max_dialogs=cfg['max_dialogs'],
                history_since=cfg['history_since'],media_prefetch_days=cfg['media_prefetch_days'],
                live_writer=live_writer,live_window_seconds=cfg['media_live_window_seconds'],
                media_backfill=cfg['media_backfill'],
                writer=BlobWriter(store.files,max_bytes=cfg['max_media_bytes'],reserve_bytes=cfg['media_disk_reserve_bytes']))
            loop=asyncio.get_running_loop()
            for sig in [signal.SIGTERM,signal.SIGINT]: loop.add_signal_handler(sig,worker.stopping.set)
            await worker.run()
        finally: store.close()


def main():
    import argparse
    from .telemetry import configure, capture
    configure('telegram')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    args=parser.parse_args()
    os.umask(0o077)
    try: asyncio.run(run_config(args.config))
    except Exception as exc:
        # Exception strings from providers can contain private requests. Emit
        # only this service's fixed error vocabulary to Tiden and the supervisor.
        code,detail,event=collector_stop(exc,'telegram')
        capture(exc, event)
        print(json.dumps({'status':'stopped','code':code,**({'detail':detail} if detail else {})}),flush=True)
        raise SystemExit(1) from None


if __name__=='__main__': main()
