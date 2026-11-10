"""Single-owner Telegram collector. No sign-in, messages or secrets in gateway tools."""
import asyncio
from collections import deque
from collections.abc import Mapping
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager,nullcontext,suppress
from functools import cached_property
from datetime import datetime,timedelta,timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import stat
import time
import traceback
from types import SimpleNamespace

from .adapters import telegram_message
from .archive import Archive
from .blobs import BlobWriter
from .envelope import stable_id
from .collector_state import LIVE_REJECT_CODES,CaptureSpool,CollectorState,PeerKindConflict,private_session_directory,session_lock
from .spool import DurableSpool,InvalidCapture,KeepItem,PoisonItem
from .collector_common import DbBackoff,commit_items,validate_common,validate_ranges
from .store import Store
from . import session_claim
from .telegram_common import ProviderPageError,ProviderUnavailable,chat_error_evidence,database_errors
from .collector_media import (BURST_FETCH,MEDIA_BACKFILL_IDLE as MEDIA_BACKFILL_IDLE,
    MEDIA_BACKFILL_INTERVAL as MEDIA_BACKFILL_INTERVAL,MEDIA_IDLE_SECONDS as MEDIA_IDLE_SECONDS,MediaCollector,
    media_deadline as media_deadline)
from .telegram_polls import PollCollector,PollProvider
from .telegram_governor import LIVE_BUDGETS,LIVE_STATE,ProviderRateLimited,RequestGovernor
from .operational import CONNECTION_ERRORS,collector_stop
from .storage_admission import MEDIA_RESERVE_BYTES
from .media_policy import MEDIA_POLICY_FIELDS,configure_media_policy,validate_prefetch_days

log=logging.getLogger(__name__)
# Seconds before a live read's deadline in which no archive write starts (ANYM-477).
STORE_MARGIN=0.5
# Seconds a cancelled live read waits for its write thread; the server has ended the transaction by then.
STORE_GRACE=1.0
# Operations whose confirmed receipt names a group new to the account, and the seconds its catalog write may take.
NEW_CHAT_OPERATIONS=('create_group','join_chat')
CATALOG_SECONDS=5.0
# ANYM-747: seconds a confirmed edit's archive hand-off (one spool write) may take inside the command's timeout.
EDIT_ARCHIVE_SECONDS=5.0
# Its own thread: a stalled hand-off write never holds a thread of the default executor (ANYM-747, Astra r1).
EDIT_ARCHIVE_EXECUTOR=ThreadPoolExecutor(max_workers=1,thread_name_prefix='edit-archive')


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


def archive_receipt(data,policy,failure):
    """ANYM-750: the archive state of a confirmed create_group receipt; the policy's refusal wins over a write
    failure, since discovery would not collect the chat either."""
    from .telegram_operations import CREATE_GROUP_NEXT_STEPS
    if 'chat_id' in data:
        data['archive']='collected';return
    chat=data.get('provider_chat_id')
    reason=failure
    if isinstance(policy,Mapping) and isinstance(chat,str):
        if chat in (policy.get('exclude_chats') or ()):reason='chat_excluded'
        elif not policy.get('discover_chats') and chat not in (policy.get('include_chats') or ()):
            reason='discovery_disabled'
    data.update(archive='not_collected',archive_reason=reason,next_step=CREATE_GROUP_NEXT_STEPS[reason])


class ChatPageFailed(Exception):
    """One chat's history page failed for a provider reason that must not stop the collector (ANYM-293)."""
    def __init__(self,code,error):
        self.code=code; self.error=error; super().__init__(code)


class DiscoveryFailed(Exception):
    """A dialog page failed for a provider reason that degrades the discovery pass, not the collector (ANYM-749)."""
    def __init__(self,error):
        self.code='discovery_provider_error'; self.error=error; super().__init__(self.code)


def private_json(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as source:
        info=os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_size>65536:
            raise ValueError('private_config_permissions_or_size')
        return json.load(source)


# Head pages of one chat per reconcile pass: a chat above one page per pass otherwise falls behind for good.
HEAD_PAGES_PER_PASS=10
HEAD_PAGES_RANGE=('head_pages_per_pass',HEAD_PAGES_PER_PASS,1,100)


def configuration(path):
    cfg=private_json(path)
    fields={'enabled','account','credentials_file','session_file','permit_file','spool_directory',
            'batch_size','scan_interval_seconds','max_media_bytes','disk_reserve_bytes',
            'max_dialogs','history_since','media_backfill','live_search_budget','head_pages_per_pass',
            'session_lock_directory'}|MEDIA_POLICY_FIELDS
    validate_common(cfg,fields)
    for key in ['credentials_file','session_file','permit_file','spool_directory']:
        if not isinstance(cfg.get(key),str) or not Path(cfg[key]).is_absolute(): raise ValueError('invalid_private_path')
    lock_directory=cfg.get('session_lock_directory')  # ANYM-914; default session_claim.LOCK_DIRECTORY
    if lock_directory is not None and (not isinstance(lock_directory,str) or not Path(lock_directory).is_absolute()):
        raise ValueError('invalid_private_path')
    if not cfg['session_file'].endswith('.session'): raise ValueError('invalid_session_filename')
    validate_ranges(cfg,[('batch_size',100,1,100),('scan_interval_seconds',60,10,3600),
                         ('max_media_bytes',512*1024**2,1,4*1024**3),
                         ('disk_reserve_bytes',1024**3,1024**3,100*1024**3),HEAD_PAGES_RANGE])
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


def dialog_kind(dialog):
    """The dialog's peer kind from its raw TL peer, which also names a ChannelForbidden.

    Telethon's Dialog.is_channel is False for a ChannelForbidden; a marked-id pattern cannot tell
    that from a basic group whose id starts with 100, so the raw peer decides."""
    peer=getattr(getattr(dialog,'dialog',None),'peer',None)
    if dialog.is_channel or getattr(peer,'channel_id',None) is not None:return 'channel'
    return 'user' if dialog.is_user else 'chat'


def peer_kind(message):
    peer=message.peer_id
    if getattr(peer,'channel_id',None) is not None: return 'channel'
    if getattr(peer,'chat_id',None) is not None: return 'chat'
    if getattr(peer,'user_id',None) is not None: return 'user'
    raise ValueError('unsupported_telegram_peer')


class TelethonProvider(PollProvider):
    supports_receipts=True
    supports_owner_reads=True
    supports_group_receipts=True
    fetches_bursts=True  # chunks() reads source['_tier'] and source['_burst'] (ANYM-877)
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
        self.governor=RequestGovernor(state,budgets=getattr(self,'live_budgets',None),
            bound=bounded_state if isinstance(state,CollectorState) else None)
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
                entry=next((entry for entry in self.getting_diff_for if isinstance(entry,int)),None)
                request=base.get_channel_difference(self,*args)
                provider.channel_fetch_request(self,entry,request)
                return request
            def check_deadlines(self):
                # Entries whose no-updates deadline expired (Telethon NO_UPDATES_TIMEOUT, 15 min) or whose
                # possible gap expired start a difference fetch here.
                before=set(self.getting_diff_for);gaps={entry:gap.deadline for entry,gap in self.possible_gaps.items()}
                deadline=base.check_deadlines(self)
                provider.channel_fetch_begun(self,before,gaps)
                return deadline
            def try_begin_get_diff(self,entry,reason):
                before=entry in self.getting_diff_for
                result=base.try_begin_get_diff(self,entry,reason)
                if not before:provider.channel_fetch_begun(self,{*self.getting_diff_for}-{entry},{},'channel_too_long')
                return result
            def apply_channel_difference(self,request,diff,*args):
                result=base.apply_channel_difference(self,request,diff,*args)
                provider.channel_fetch_page(self,request.channel.channel_id,diff)
                return result
            def end_channel_difference(self,request,*args):
                result=base.end_channel_difference(self,request,*args)
                provider.channel_fetch_page(self,request.channel.channel_id,None,'ended_early')
                return result
        box.__class__=WatchedBox

    # ANYM-840 gate: one log line per channel difference request Telethon makes, with the stored chat id, why the
    # fetch began (timeout: the channel's no-updates deadline expired; gap: a pts gap expired; channel_too_long:
    # Telegram said so; unknown: begun before the hooks), the page, the message count and timings only: never a
    # title, text or provider id. Every hook runs inside Telethon's update loop and never raises.
    CHANNEL_FETCHES_MAX=1000
    CHANNEL_EVENTS_MAX=1000

    @cached_property
    def channel_events(self):
        """The gate lines leave Telethon's loop through this bounded queue to a writer thread, so a stalled log pipe
        (Docker's blocking log delivery) stalls only that thread; a full queue drops the line and counts it. They go to
        stderr, each as one write of the whole line: the collector's own stdout events are printed from the loop
        (body and newline in two writes), so sharing stdout from a second thread could splice two records."""
        import queue,sys,threading
        events=queue.Queue(self.CHANNEL_EVENTS_MAX)
        def write():
            while True:
                line=events.get()
                try:sys.stderr.write(line+'\n');sys.stderr.flush()
                except Exception:pass
                finally:events.task_done()
        threading.Thread(target=write,name='channel-difference-events',daemon=True).start()
        return events

    def channel_event(self,event):
        import queue
        try:self.channel_events.put_nowait(json.dumps(event))
        except queue.Full:self.channel_events_dropped=getattr(self,'channel_events_dropped',0)+1

    @cached_property
    def channel_fetches(self):
        # channel id -> [trigger, began, page, page started or None]; an entry leaves when its fetch ends.
        return {}

    def channel_fetch_begun(self,box,before,gaps,trigger=None):
        try:
            now=asyncio.get_running_loop().time();fetches=self.channel_fetches
            for entry in box.getting_diff_for:
                if not isinstance(entry,int) or entry in before or entry in fetches:continue
                if len(fetches)>=self.CHANNEL_FETCHES_MAX:fetches.clear()
                fetches[entry]=[trigger or ('gap' if entry in gaps and gaps[entry]<=now else 'timeout'),now,0,None]
        except Exception:return

    def channel_fetch_request(self,box,entry,request):
        try:
            now=asyncio.get_running_loop().time();fetches=self.channel_fetches
            if request is None:
                # Telethon dropped the channel (no access hash) without a request.
                if entry is not None and entry not in box.getting_diff_for and entry in fetches:
                    self.channel_fetch_log(entry,fetches.pop(entry),'no_access_hash',None,now)
                return
            entry=request.channel.channel_id
            if entry not in fetches:
                if len(fetches)>=self.CHANNEL_FETCHES_MAX:fetches.clear()
                fetches[entry]=['unknown',now,0,None]
            fetch=fetches[entry]
            # A request that never got a response (Telethon sleeps on OSError and asks again).
            if fetch[3] is not None:self.channel_fetch_log(entry,fetch,'no_response',None,now)
            fetch[2]+=1;fetch[3]=now
        except Exception:return

    def channel_fetch_page(self,box,entry,diff,result=None):
        try:
            from telethon import types
            now=asyncio.get_running_loop().time();fetch=self.channel_fetches.get(entry)
            if fetch is None:return
            if result is None:
                result=('empty' if isinstance(diff,types.updates.ChannelDifferenceEmpty) else 'too_long'
                    if isinstance(diff,types.updates.ChannelDifferenceTooLong) else 'difference')
            self.channel_fetch_log(entry,fetch,result,diff,now);fetch[3]=None
            if entry not in box.getting_diff_for:self.channel_fetches.pop(entry,None)
        except Exception:return

    def channel_fetch_log(self,entry,fetch,result,diff,now):
        from telethon import types
        trigger,began,page,page_started=fetch
        # ChannelDifferenceTooLong carries the latest messages in `messages`, a difference in `new_messages`.
        field='messages' if isinstance(diff,types.updates.ChannelDifferenceTooLong) else 'new_messages'
        messages=list(getattr(diff,field,None) or ())
        dates=[m.date for m in messages if isinstance(getattr(m,'date',None),datetime)]
        oldest=round((datetime.now(timezone.utc)-min(dates)).total_seconds()) if dates else None
        elapsed=round((now-page_started)*1000) if page_started is not None else None
        chat=stable_id('chat',getattr(getattr(self,'read_gap_state',None),'account',None),str(-1000000000000-entry))
        # A JSON event like the collector's others (the service configures no INFO logging), written off the loop.
        self.channel_event({'event':'telegram_channel_difference','chat':chat,'trigger':trigger,'result':result,
            'page':page,'messages':len(messages),'final':getattr(diff,'final',None),'elapsed_ms':elapsed,
            'fetch_ms':round((now-began)*1000),'oldest_age_s':oldest})

    def read_gap_soon(self):
        # Called inside Telethon's update loop: never block or raise there.
        try:task=asyncio.get_running_loop().create_task(self.record_read_gap())
        except RuntimeError:return
        self._read_gap_tasks.add(task);task.add_done_callback(self._read_gap_tasks.discard)

    @cached_property
    def read_gap_lock(self):
        # Orders gap writes with the collector's continuity heartbeat (ANYM-275).
        return asyncio.Lock()

    async def record_read_gap(self):
        async with self.read_gap_lock:
            try:await asyncio.to_thread(self.read_gap_state.update_gap)
            except Exception as exc:
                # Kept until the collector writes it, before any new continuity heartbeat.
                self.read_gap_pending=True
                logging.getLogger(__name__).warning('read_state_gap_failed: %s',type(exc).__name__)

    async def rpc(self,operation,phase,*,before=None,tier=None):
        governor=getattr(self,'governor',None)
        if governor:
            return await governor.call(operation,phase,**({'before':before} if before is not None else {}),
                **({'tier':tier} if tier is not None else {}))
        if before is not None:await before()
        return await operation()

    def subscribe(self,capture,deleted,receipts=None,*,owner_reads=None,group_receipts=None):
        from telethon import events,types
        self.client.add_event_handler(capture,events.NewMessage())
        self.client.add_event_handler(capture,events.MessageEdited())
        # NewMessage builds nothing for a MessageService (Telethon 1.45.0 events/newmessage.py), so creation
        # notices, joins and title changes arrive here; ordinary messages stay with NewMessage (ANYM-847).
        self.client.add_event_handler(self.service_messages(capture),
            events.Raw([types.UpdateNewMessage,types.UpdateNewChannelMessage]))
        self.client.add_event_handler(deleted,events.MessageDeleted())
        if receipts is not None:
            self.client.add_event_handler(receipts,events.Raw(types.UpdateReadHistoryOutbox))
        if owner_reads is not None:
            self.client.add_event_handler(owner_reads,events.Raw([types.UpdateReadHistoryInbox,types.UpdateReadChannelInbox]))
        if group_receipts is not None:
            # ANYM-868: a basic group's boundary shares the private one's update type; each handler takes its own peers.
            self.client.add_event_handler(group_receipts,events.Raw([types.UpdateReadHistoryOutbox,types.UpdateReadChannelOutbox]))

    def service_messages(self,capture):
        from telethon import types
        async def service_message(update):
            message=getattr(update,'message',None)
            if not isinstance(message,types.MessageService):return
            # Run by capture inside its guard: the entities Telegram sent with this update and the offline
            # cache only, never an entity-fetch request.
            prepare=lambda:message._finish_init(self.client,getattr(update,'_entities',None) or {},None)
            await capture(SimpleNamespace(message=message,prepare=prepare),guarded=True)
        return service_message

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
        # Admission pacing/cooldown is shared, unfinished background RPCs are not.
        if report is not None:report('waiting')
        payload=dispatch_payload(job,'telegram')
        options={'parse_mode':None,'link_preview':payload['link_preview'],
            'reply_to':int(job['reply_provider_id']) if job.get('reply_provider_id') else None,
            'clear_draft':False}
        if payload.get('silent') is True:options['silent']=True  # ANYM-927: a previewed silent row, no sound.
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
        async def before():
            if datetime.now(timezone.utc).timestamp()>=job['expires_at']:
                raise SendRejected('send_expired')
            if job.get('_preflight') is not None:await job['_preflight']()
        async def send():
            if datetime.now(timezone.utc).timestamp()>=job['expires_at']:
                raise SendRejected('send_expired')
            if report is not None:report('started')
            try:message=await self.client.send_message(int(job['provider_chat']),payload['text'],**options)
            except FloodError:
                # 420 FLOOD answers reject the request before Telegram acts on it.
                if report is not None:report('refused')
                raise
            if not message or str(message.chat_id)!=job['provider_chat']:
                raise RuntimeError('send_receipt_mismatch')
            return {'provider_message_id':str(message.id),'message':message}
        return await self.rpc(send,'provider_send',before=before)

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
        # A transient server error is retried the same way from a budget of CHAT_ATTEMPTS
        # failures per pass; it and any other non-terminal RPC error then fail only this
        # pass (DiscoveryFailed, ANYM-749). Rate limits, session-terminal errors and
        # transport loss propagate as before.
        from telethon.errors import (AuthKeyDuplicatedError,FloodError,RPCError,ServerError,TimedOutError,
            UnauthorizedError)
        iterator=self.client.iter_dialogs(limit=limit).__aiter__()
        batch=[];page_observed_at=datetime.now(timezone.utc).isoformat();failures=0
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
            except (FloodError,UnauthorizedError,AuthKeyDuplicatedError):raise
            except (ServerError,TimedOutError,TimeoutError) as exc:
                # Rows already listed are yielded (and saved) before the wait or the end of the pass.
                failures+=1
                if batch:yield batch;batch=[]
                if failures>=CHAT_ATTEMPTS:raise DiscoveryFailed(exc) from exc
                await asyncio.sleep(CHAT_RETRY_DELAYS[failures-1])
                continue
            except (RPCError,ProviderPageError) as exc:
                if batch:yield batch
                raise DiscoveryFailed(exc) from exc
            kind=dialog_kind(dialog)
            chat_type='private' if dialog.is_user else ('group' if dialog.is_group else 'channel')
            raw=getattr(dialog,'dialog',None)
            # The dialog's top message id, with when this page saw it, lets history reconcile skip an unchanged
            # chat's head page (dialog_tops: chat -> (top id, observed wall time, observed monotonic time)).
            top=getattr(raw,'top_message',None)
            tops=self.__dict__.setdefault('dialog_tops',{})
            if type(top) is int and top>0:
                tops[str(dialog.id)]=(top,datetime.now(timezone.utc),time.monotonic())
            else:tops.pop(str(dialog.id),None)
            observation={'observed_at':page_observed_at,
                'unread_count':getattr(raw,'unread_count',None),
                'read_inbox_max_id':getattr(raw,'read_inbox_max_id',None),
                'is_self_chat':bool(getattr(getattr(dialog,'entity',None),'is_self',False)),
                'basis':'telegram_dialog'}
            # The account's role travels beside the read observation, from the entity this page loaded (ANYM-533).
            from .chat_roles import observation as role
            batch.append((str(dialog.id),kind,dialog.name or str(dialog.id),chat_type,observation,
                None if chat_type=='private' else role(getattr(dialog,'entity',None),chat_type)))
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

    def fetched_message(self,chat,message_id):
        """A message a burst fetch took for a later job (ANYM-877): used once, at most FETCHED_SECONDS old."""
        fetched=self.__dict__.setdefault('_fetched',{})
        now=time.monotonic()
        for key in [k for k,(at,_) in fetched.items() if now-at>FETCHED_SECONDS]:del fetched[key]
        entry=fetched.pop((chat,message_id),None)
        return entry[1] if entry else None

    def keep_fetched(self,chat,messages):
        fetched=self.__dict__.setdefault('_fetched',{})
        now=time.monotonic()
        for message in messages:
            if message is None or type(getattr(message,'id',None)) is not int:continue
            if len(fetched)>=FETCHED_MAX:del fetched[next(iter(fetched))]
            fetched[(chat,message.id)]=(now,message)

    async def media_message(self,source,tier):
        """The job's message: a burst's earlier fetch if it still matches, else one request that also
        takes the messages of the chat's other queued live jobs (source['_burst'], ANYM-877)."""
        chat,message_id=int(source['provider_chat']),int(source['provider_id'])
        burst=[i for i in dict.fromkeys(source.get('_burst') or ()) if type(i) is int and i!=message_id][:BURST_FETCH]
        if burst:
            messages=await self.rpc(lambda:self.client.get_messages(chat,ids=[message_id,*burst]),'telegram_media',
                tier=tier)
            messages=list(messages or ())
            self.keep_fetched(chat,messages[1:])
            return messages[0] if messages else None
        return await self.rpc(lambda:self.client.get_messages(chat,ids=message_id),'telegram_media',tier=tier)

    async def chunks(self,source,descriptor):
        from telethon import types,utils
        from telethon.errors import FileReferenceExpiredError,FilerefUpgradeNeededError,FloodWaitError
        tier=source.get('_tier')
        try:
            # A burst's earlier fetch serves this job once; one that no longer matches is fetched again.
            message=self.fetched_message(int(source['provider_chat']),int(source['provider_id']))
            if message is None or descriptor not in telegram_message(source['account'],message)['media']:
                message=await self.media_message(source,tier)
            if not message: raise ProviderUnavailable('media_unavailable')
            current=telegram_message(source['account'],message)
            if descriptor not in current['media']: raise ProviderUnavailable('media_reference_changed')
            # A link preview exposes .document/.photo too, but its outer
            # MessageMediaWebPage is not a downloadable Telethon location.
            # Download the same concrete object that the adapter described.
            media=getattr(message,'document',None) or getattr(message,'photo',None)
            if media is None: raise ProviderUnavailable('media_unavailable')
            download_options={'request_size':MEDIA_REQUEST_BYTES}
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
            expected=descriptor.get('bytes') if type(descriptor.get('bytes')) is int else None
            try:
                iterator=stream.__aiter__();received=0
                while True:
                    try:chunk=await self.rpc(iterator.__anext__,'telegram_media',tier=tier)
                    except StopAsyncIteration:break
                    received+=len(chunk)
                    yield chunk
                    # ANYM-877: the call after the last part makes no request (Telethon ends after a short part),
                    # so it takes no admission: the stream ends at the descriptor's size or, size unknown, after a
                    # short part. A short or long total still fails in the writer.
                    if received>=expected if expected is not None else len(chunk)<MEDIA_REQUEST_BYTES:break
            finally: await stream.close()
        except FloodWaitError as exc: raise ProviderRateLimited(exc.seconds) from None
        except (FileReferenceExpiredError,FilerefUpgradeNeededError):
            # Even a freshly fetched preview can contain an expired reference.
            # Retry this job with a fresh message on the next attempt (its own code and a
            # three-attempt budget, media_retry.MEDIA_ATTEMPT_CAPS); one unavailable original
            # must not stop every chat's collection.
            raise OSError('media_reference_expired') from None


# ANYM-877: one file part per request (Telethon's maximum). A job's message fetch also takes the messages of up to
# BURST_FETCH other queued live jobs of its chat; a later job uses its copy once, if at most FETCHED_SECONDS old.
MEDIA_REQUEST_BYTES=512*1024
FETCHED_SECONDS=600; FETCHED_MAX=200
# History reconcile reads no head page of a chat whose dialog top, seen at most this long ago, is at most its cursor.
TOP_FRESH_SECONDS=1800
# Deterministic failures of a pure parser on malformed provider input. Resource,
# I/O and unexpected errors are not among them and still fail capture closed.
PARSE_ERRORS=(ValueError,TypeError,KeyError,AttributeError,IndexError,RecursionError)
# ANYM-868: a group read boundary whose chat has no stored type is retried at most this often, and dropped once its
# observation is this old (a chat still untyped after a week is not one the owner is waiting on).
DEFERRED_RECEIPT_RETRY_S=10
DEFERRED_RECEIPT_DAYS=7
DEFERRED_RECEIPT_BATCH=500


def deferred_expired(item,now=None):
    observed=datetime.fromisoformat(item['observed_at'])
    return (now or datetime.now(timezone.utc))-observed>timedelta(days=DEFERRED_RECEIPT_DAYS)
# Live capture backpressure (ANYM-291): at most CAPTURE_WRITES spool writes run at once;
# further callbacks wait in arrival order. More than CAPTURE_WAITERS waiting callbacks, or one
# wait longer than CAPTURE_WAIT_SECONDS, fails the collector closed with capture_overload.
CAPTURE_WRITES=16; CAPTURE_WAITERS=512; CAPTURE_WAIT_SECONDS=30
# History reconcile of one chat (ANYM-293): one chat may fail with a transient server error at most
# CHAT_ATTEMPTS times per pass, over all its pages; successful pages do not count. A failed page is
# retried after CHAT_RETRY_DELAYS seconds while the budget lasts.
CHAT_ATTEMPTS=3; CHAT_RETRY_DELAYS=(1,2,4)
# Unapplied deletions (ANYM-789): a deletion event applies in line, so this sweep only repairs a crash between the
# tombstone's commit and that call, or a chat whose peer_kind arrives later. Its query scans the account's archive.
TOMBSTONE_SWEEP_SECONDS=60


class TelegramCollector(MediaCollector,PollCollector):
    state_class=CollectorState
    head_pages=HEAD_PAGES_PER_PASS
    _tops_trusted=True  # this pass may skip head pages by the catalog's tops (not after a failed discovery)
    _pass_head_capped=None
    _tombstones_due=-math.inf
    coverage_code='telegram_history_partial'
    connect_timeout=60
    retry_connect=False
    database_retry=2
    def __init__(self,archive,account,provider,spool,*,batch_size=100,interval=60,max_dialogs=None,
                 writer=None,owner=None,lease_ttl=60,heartbeat_interval=15,history_since=None,
                 live_writer=None,live_window_seconds=86400,media_prefetch_days=None,media_backfill=False,
                 head_pages=HEAD_PAGES_PER_PASS):
        self.archive,self.account,self.provider,self.spool=archive,account,provider,spool
        self.head_pages=head_pages;self._head_capped=set()
        self.batch_size,self.interval,self.max_dialogs=batch_size,interval,max_dialogs
        self.history_since=history_since
        self.media_prefetch_days=validate_prefetch_days(media_prefetch_days)
        self.writer=writer or BlobWriter(archive.store.files,reserve_bytes=MEDIA_RESERVE_BYTES)
        self.live_writer,self.live_window_seconds=live_writer,live_window_seconds
        self.media_backfill=media_backfill is True; self._backfill_due=0
        self.owner=owner or 'telegram-'+secrets.token_hex(12)
        self.lease_ttl,self.heartbeat_interval=lease_ttl,heartbeat_interval
        self.state=None; self.stopping=asyncio.Event(); self.failed=asyncio.Event()
        # ANYM-914: (pool, Claim) kept by run(); released by run_config only after a verified disconnect.
        self.session_claim=None; self.disconnect_verified=False
        self.release_claim=None  # ANYM-914: run_config's bounded release, awaited right after a verified disconnect
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

    @property
    def deferred_receipts(self):
        # ANYM-868: group read boundaries whose chat has no stored type yet (receipts.record `receipt_deferred`),
        # beside the live spool and outside the archive, so no runtime reads them before the chat is known.
        if getattr(self,'_deferred_receipts',None) is None:
            from .spool import DurableSpool
            live=self.spool
            # Capacity equals one retry pass, so every pass visits every kept file (no boundary starves another).
            self._deferred_receipts=DurableSpool(live.path/'deferred-receipts',validator=live.validator,
                max_bytes=live.max_bytes,max_entries=DEFERRED_RECEIPT_BATCH,reserve_bytes=live.reserve_bytes)
        return self._deferred_receipts

    @property
    def edit_spool(self):
        # ANYM-747: a confirmed edit's archive hand-off, beside the live spool with its own publication lock, so a
        # stalled hand-off write never holds the lock live captures take. Drained with the live spool.
        if getattr(self,'_edit_spool',None) is None:
            from .spool import DurableSpool
            live=self.spool
            self._edit_spool=DurableSpool(live.path/'edits',validator=live.validator,
                max_bytes=live.max_bytes,max_entries=live.max_entries,reserve_bytes=live.reserve_bytes)
        return self._edit_spool

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

    async def capture(self,event,*,guarded=False):
        # Bound pending fsync work: callbacks wait for a slot. Overload fails closed; reconciliation
        # after restart is required rather than claiming dropped callbacks succeeded.
        # guarded (a live service message, ANYM-847): any exception before the spool write rejects that one
        # update instead of failing the session; the spool write keeps its fail-closed rules.
        if self.stopping.is_set() or self.failed.is_set(): return
        if not await self.capture_slot(): return
        message=chat=None
        try:
            try:
                message=event.message
                if guarded: event.prepare()
                chat=str(getattr(message,'chat_id',None))
                e=telegram_message(self.account,message,verified_self=getattr(self.provider,'verified_self',None))
                kind=peer_kind(message)
            except Exception as exc:
                # Unguarded, only a ValueError from normalization (after the chat read) is unstorable; anything
                # else fails closed as before.
                if not guarded and (not isinstance(exc,ValueError) or isinstance(exc,OSError) or chat is None):raise
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

    async def receipt(self,event,*,normalize=None):
        if self.stopping.is_set() or self.failed.is_set(): return
        pending=False
        try:
            from .telegram_receipts import UnsupportedReceiptPeer,telegram_receipt
            try:
                capture=(normalize or telegram_receipt)(self.account,event,
                    verified_self=getattr(self.provider,'verified_self',None),
                    expected_user_id=getattr(self.provider,'expected_user_id',None))
            except UnsupportedReceiptPeer as exc:self.reject(exc.code);return
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

    async def group_receipt(self,event):
        """A group's outbox boundary (ANYM-868) through the private receipt's own spool, policy and capture slot."""
        from .telegram_receipts import telegram_group_receipt
        await self.receipt(event,normalize=telegram_group_receipt)

    async def commit_page(self,messages,chat,title,kind,origin):
        ids=[]
        def check(message):
            if str(message.chat_id)!=chat: raise RuntimeError('provider_chat_mismatch')
        def build(message):
            e=telegram_message(self.account,message,chat_title=title,origin=origin,
                verified_self=getattr(self.provider,'verified_self',None))
            return {'kind':'message','peer_kind':kind,'envelope':e}
        # An unstorable message (a build ValueError) must not stop history for the chat or account; a spool
        # InvalidCapture is the same rejection a live callback counts, while capacity and I/O still raise.
        await commit_items(self,messages,check=check,build=build,build_errors=ValueError,put_errors=InvalidCapture,
            record=lambda message:ids.append(message.id),record_unstorable=lambda message:ids.append(message.id))
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
        high_water=state['high_water']
        # A chat whose catalog, seen at most TOP_FRESH_SECONDS ago in a pass whose discovery did not fail, shows no
        # message above the cursor needs no head page (a head left at the page cap has a top above it); the catalog's
        # time is its head evidence. A message after that catalog is read once the next one shows it.
        top=(getattr(self.provider,'dialog_tops',None) or {}).get(chat) if self._tops_trusted else None
        unchanged=top is not None and top[0]<=high_water and 0<=time.monotonic()-top[2]<=TOP_FRESH_SECONDS
        if not unchanged:
            for _ in range(self.head_pages):
                new=await self.chat_page(chat,limit=self.batch_size,min_id=high_water,reverse=True)
                await self.commit_page(self.in_window(new),chat,title,kind,'reconcile')
                if new:
                    high_water=max(high_water,*(m.id for m in new))
                    await asyncio.to_thread(self.state.checkpoint,chat,high_water=high_water)
                if len(new)<self.batch_size:break
        # Recent overlap catches edits after the library's update state advanced
        # ahead of durable capture. It cannot prove absence of older edits/deletes.
        recent=await self.chat_page(chat,limit=self.batch_size)
        await self.commit_page(self.in_window(recent),chat,title,kind,'reconcile')
        if unchanged:await asyncio.to_thread(self.state.checkpoint,chat,head_verified_at=top[1])
        elif len(new)<self.batch_size:
            await asyncio.to_thread(self.state.checkpoint,chat,head_complete=True)
        else:self._head_capped.add(chat)
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
        discovery_failed=None;self._head_capped=set();self._tops_trusted=True
        if self._dialog_cache is None or time.monotonic()>=self._next_discovery:
            await asyncio.to_thread(self.state.status,'receiving','telegram_discovery')
            await asyncio.to_thread(self.state.discovery_started)
            dialogs=[]
            try:
                if hasattr(self.provider,'dialog_batches'):
                    async for batch in self.provider.dialog_batches(self.max_dialogs):
                        await asyncio.to_thread(self.state.inventory,batch,self.history_since,discovery=True)
                        dialogs.extend(batch)
                else:
                    # Small injected/test providers may expose only a single page.
                    dialogs=await self.provider.dialogs(self.max_dialogs)
                    await asyncio.to_thread(self.state.inventory,dialogs,self.history_since,discovery=True)
            except DiscoveryFailed as failure:
                # The pass degrades; batches already yielded stay saved, discovery is not marked finished and
                # the next loop tries again. History goes on over the previous catalog, if any (ANYM-749).
                discovery_failed=failure.code;self._tops_trusted=False  # an old catalog proves no head
                print(json.dumps({'event':'telegram_discovery_error','code':failure.code}),flush=True)
                await asyncio.to_thread(self.state.status,'degraded',failure.code)
                if self._dialog_cache is None:return
                dialogs=self._dialog_cache
            else:
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
            except (ChatPageFailed,PeerKindConflict) as failure:
                # One chat's provider error marks only that chat; the pass goes on (ANYM-293). A stored kind
                # that differs from the dialog's is the same: the row never clears by itself, and it must not end the worker.
                if isinstance(failure,PeerKindConflict):failure=ChatPageFailed('peer_kind_conflict',failure)
                unavailable=True
                self.chat_failed(chat,failure)
                await asyncio.to_thread(self.state.failed,chat,failure.code,chat_error_evidence(failure.error))
        missing=set(self.policy['include_chats'])-set(self.policy['exclude_chats'])-seen
        code='dialog_discovery_limited' if self.max_dialogs is not None and len(dialogs)>=self.max_dialogs else 'telegram_history_partial'
        if missing or unavailable: code='configured_chat_unavailable'
        # A pass over the previous catalog after a failed discovery keeps the discovery code (ANYM-749).
        if discovery_failed: code=discovery_failed
        await asyncio.to_thread(self.state.status,'degraded' if missing or unavailable or discovery_failed else 'receiving',code)
        self._pass_head_capped=len(self._head_capped)

    async def reconcile_loop(self):
        from telethon.errors import FloodWaitError
        from .telegram_identity import repair_sent_senders
        await asyncio.to_thread(repair_sent_senders,self.archive,self.account,self.state.lease,
            getattr(self.provider,'verified_self',None))
        backoff=DbBackoff(self)
        while True:
            try:
                await self.sync_once()
                # Only a pass that reached its end replaces the count; a failed write keeps it for the next try.
                if self._pass_head_capped is not None:
                    await asyncio.to_thread(self.state.head_capped,self._pass_head_capped)
                    self._pass_head_capped=None
            except (FloodWaitError,ProviderRateLimited) as exc:
                print(json.dumps({'event':'telegram_rate_limited','seconds':exc.seconds}),flush=True)
                if isinstance(exc,FloodWaitError):await asyncio.to_thread(self.state.defer,max(1,exc.seconds))
                await asyncio.to_thread(self.state.status,'degraded','rate_limited')
                governor=getattr(self.provider,'governor',None)
                if governor:await governor.wait()
                else:await asyncio.sleep(max(exc.seconds,1))
                continue
            except database_errors():
                # Checkpoints are committed per page; retry instead of reconnecting the provider.
                await backoff.wait(); continue
            backoff.reset()
            await asyncio.sleep(self.interval)

    async def drain_loop(self):
        import psycopg
        from psycopg_pool import PoolTimeout
        while True:
            try:
                await self.drain_captures([self.spool],[self.history_spool])
                await self.sweep_tombstones()
            except (psycopg.OperationalError,PoolTimeout):
                # Capture remains durable; the independent heartbeat stops the
                # provider if database ownership cannot be renewed in time.
                await asyncio.sleep(1)
            await asyncio.sleep(.25)

    async def sweep_tombstones(self):
        """Sweep on the first pass, then once a period; a full page sweeps again next pass, a failure leaves it due."""
        if time.monotonic()<self._tombstones_due:return
        await asyncio.to_thread(self.state.apply_tombstones)
        self._tombstones_due=(-math.inf if getattr(self.state,'tombstones_page_full',False)
            else time.monotonic()+TOMBSTONE_SWEEP_SECONDS)

    async def drain_captures(self,spools,history_spools=()):
        last=None;count=0
        # ANYM-747: the edit spool beside the live spool drains with it, as live progress (WhatsApp's drain passes
        # its events spool, never self.spool, so it gains nothing).
        live=getattr(self,'spool',None)
        if isinstance(live,DurableSpool) and any(spool is live for spool in spools):
            spools=[*spools,self.edit_spool]
        def settle(item):
            # A kept item whose chat has another stored kind can never apply: quarantine its bytes so the
            # other chats keep draining (counted in capture_progress), instead of ending the worker.
            try:return self.state.apply(item)
            except PeerKindConflict as exc:raise PoisonItem('peer_kind_conflict') from exc
        def apply(item):
            nonlocal last,count
            defer(item,settle(item));last=item;count+=1
            # ANYM-813: the first committed live message of a batch, kept until a status write succeeds
            # (capture_progress records it for the native WhatsApp provider only).
            if (getattr(self,'first_live',None) is None and isinstance(item,dict) and item.get('kind')=='message'
                    and isinstance(item.get('envelope'),dict) and item['envelope'].get('origin')=='live'):
                self.first_live=item
        def defer(item,result):
            # Kept durably before the drain removes the item from its own spool (ANYM-868); an expired one is dropped.
            # A full deferred spool (or the disk reserve) drops this boundary and counts it: it never stops the drain.
            if isinstance(result,dict) and result.get('outcome')=='receipt_deferred' and not deferred_expired(item):
                try:self.deferred_receipts.put(item)
                except RuntimeError as exc:
                    if str(exc) not in ('spool_full','disk_reserve'):raise
                    self.reject('receipt_deferred_full')
        gap_lock=getattr(getattr(self,'provider',None),'read_gap_lock',None) or nullcontext()
        async with gap_lock:await self.flush_read_gap()
        for spool in spools:await asyncio.to_thread(spool.drain,apply,100)
        if isinstance(live,DurableSpool) and time.monotonic()>=getattr(self,'_next_deferred_retry',0):
            self._next_deferred_retry=time.monotonic()+DEFERRED_RECEIPT_RETRY_S
            def retry(item):
                result=self.state.apply(item)
                # Still untyped: the file stays where it is (a put of the same item would dedupe onto it).
                if isinstance(result,dict) and result.get('outcome')=='receipt_deferred' and not deferred_expired(item):
                    raise KeepItem()
            await asyncio.to_thread(self.deferred_receipts.drain,retry,DEFERRED_RECEIPT_BATCH)
        # History pages were applied inline when fetched. Replaying them is
        # idempotent and is neither live progress nor live backlog.
        for spool in history_spools:await asyncio.to_thread(spool.drain,settle,100)
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
                backlog=await asyncio.to_thread(pending,spools)
                # A gap write waits for this heartbeat; one that failed meanwhile is written first.
                async with gap_lock:
                    await self.flush_read_gap()
                    first_live=getattr(self,'first_live',None)
                    if first_live is not None:extra['first_live']=first_live
                    await asyncio.to_thread(self.state.capture_progress,last,count,backlog,
                        quarantined=min(20000,quarantined),invalid=invalid,**extra)
                    self.first_live=None
            except BaseException:
                # Skipped items have no spool record to replay; keep their counts for the retry.
                self.invalid_messages+=invalid
                for code,number in rejected.items():self.rejected[code]=self.rejected.get(code,0)+number
                self.rejected_messages=[*skipped,*self.rejected_messages][-20:]
                raise
            self._next_capture_status=time.monotonic()+15

    async def flush_read_gap(self):
        # A lost gap write (ANYM-275): persist it before any heartbeat; while it fails, none.
        if not getattr(getattr(self,'provider',None),'read_gap_pending',False):return
        self.provider.read_gap_pending=False
        try:await asyncio.to_thread(self.state.update_gap)
        except BaseException:
            self.provider.read_gap_pending=True
            raise

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

    async def run(self):
        started=time.monotonic()
        epoch=await asyncio.to_thread(self.archive.acquire,self.account,self.owner,self.lease_ttl)
        self._lease_valid_until=started+self.lease_ttl-min(10,self.lease_ttl/4)
        self.state=self.state_class(self.archive,self.account,(self.owner,epoch))
        # Conflict re-evaluation (ANYM-621): its own thread and connection, fenced by this lease; never blocks capture.
        from .conflict_reevaluation import start as start_reevaluation
        reevaluation=start_reevaluation(self.archive,self.account,(self.owner,epoch))
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
                groups={'group_receipts':self.group_receipt} if getattr(self.provider,'supports_group_receipts',False) is True else {}
                self.provider.subscribe(self.capture,self.deleted,self.receipt,owner_reads=self.owner_read,**groups)
            elif getattr(self.provider,'supports_receipts',False) is True:
                self.provider.subscribe(self.capture,self.deleted,self.receipt)
            else:self.provider.subscribe(self.capture,self.deleted)
            polls=hasattr(self.provider,'subscribe_polls') and hasattr(self.provider,'poll_votes')  # ANYM-920
            if polls:self.provider.subscribe_polls(self.poll_update)
            # Renew ownership and honor shutdown during a slow network connect.
            tasks=[asyncio.create_task(self.heartbeat()),asyncio.create_task(self.stopping.wait())]
            if self.session_claim is not None:
                # Refresh is visibility only: it raises solely on a positive session_claim_lost.
                tasks.append(asyncio.create_task(session_claim.keep(*self.session_claim)))
            connecting=asyncio.create_task(self.connect_provider())
            tasks.append(connecting)
            done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done: task.result()
            if self.stopping.is_set(): return
            if connecting not in done: raise RuntimeError('collector_lease_lost')
            tasks.remove(connecting)
            tasks.extend(asyncio.create_task(coro) for coro in
                [self.drain_loop(),self.reconcile_loop(),self.media_loop(),self.outgoing_loop(),
                 self.provider.wait_disconnected(),self.failed.wait(),*([self.poll_voters_loop()] if polls else [])])
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
            if reevaluation is not None: reevaluation.stop()
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            try:
                await asyncio.wait_for(self.provider.disconnect(),15); self.disconnect_verified=True
            except Exception: pass  # an uncertain disconnect keeps the claim row (ANYM-914)
            if self.disconnect_verified and self.release_claim is not None:
                # First, before the status and lease cleanup below, so the stop grace covers it (ANYM-914).
                with suppress(Exception): await self.release_claim()
            if failure is None:
                with suppress(Exception):
                    gate=await asyncio.to_thread(self.state.connection_gate)
                    if not gate['blocked'] and gate['remaining_seconds']<=0:
                        await asyncio.to_thread(self.state.phase,'connection','stopped','collector_shutdown')
                        await asyncio.to_thread(self.state.phase,'live_capture','stopped','collector_shutdown')
            with suppress(Exception): await asyncio.to_thread(self.state.status,'stopped','requires_reconciliation')
            with suppress(Exception): await asyncio.to_thread(self.archive.release,self.account,self.owner,epoch)

    async def outgoing_loop(self):
        # Expiry is metadata housekeeping, never queued behind provider I/O or
        # its FloodWait. Supervise both tasks and cancel only our own children.
        tasks=[asyncio.create_task(self.outgoing_dispatch_loop()),
               asyncio.create_task(self.outgoing_expiry_loop())]
        try:
            done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done:task.result()
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)

    async def outgoing_expiry_loop(self):
        from psycopg import Error
        from psycopg.errors import LockNotAvailable
        from .outgoing import OutgoingWorker,EXPIRY_SECONDS,EXPIRY_GRACE
        worker=OutgoingWorker(self.state,None)
        backoff=DbBackoff(self)
        while True:
            deadline=time.monotonic()+EXPIRY_SECONDS
            pending=asyncio.create_task(asyncio.to_thread(worker.expire_queued,deadline=deadline))
            try:await asyncio.shield(pending)
            except asyncio.CancelledError:
                # The server ends the transaction and frees the fence at the
                # absolute deadline, even if the client thread remains stalled.
                await asyncio.wait({pending},timeout=max(0,deadline+EXPIRY_GRACE-time.monotonic()))
                pending.add_done_callback(lambda t:t.cancelled() or t.exception())
                raise
            except LockNotAvailable:
                # Every ingest transaction holds this account's collector_leases row (fence) and expire_queued
                # waits only 100 ms for it, so a busy archive is routine contention, not a fault. The pass
                # skips, tries again at the fixed database_retry (no doubling) and logs nothing; claim()
                # still expires a due send before it dispatches and reads report it expired by the clock.
                await asyncio.sleep(self.database_retry);continue
            except (Error,TimeoutError,*database_errors()) as exc:
                log.warning('outgoing_expiry_failed exception_type=%s',type(exc).__name__)
                await backoff.wait();continue
            backoff.reset()
            await asyncio.sleep(1)

    async def outgoing_dispatch_loop(self):
        from .outgoing import OutgoingWorker
        from .commands import CommandWorker
        worker=OutgoingWorker(self.state,self.send_outgoing,getattr(self.provider,'send_gate',None))
        commands=CommandWorker(self.state,self.execute_operation,getattr(self.provider,'send_gate',None))
        backoff=DbBackoff(self)
        while True:
            try:
                active=await worker.once()
                # Text sends get the first opportunity; only one foreground job is
                # dispatched on the existing account session at a time.
                active=await commands.once() or active
            except database_errors():
                # Dispatched rows keep their lease epoch; nothing is retried automatically.
                await backoff.wait(); continue
            backoff.reset()
            await asyncio.sleep(.25 if active else 1)

    async def execute_operation(self,job):
        # A live read's policy read and governor state calls (cooldown, status) take the lease fence too, so
        # they get its deadline on the server as well (release-042 B1); other callers keep the shared state.
        live=bounded_state(self.state,job['_deadline']) if job.get('_deadline') is not None else None
        bound=LIVE_STATE.set(live) if live is not None else None
        try:
            job['_policy']=await asyncio.to_thread((live or self.state).policy)
            if job.get('operation')=='request_chat_history': return await self.chat_history(job)
            if job.get('operation') in NEW_CHAT_OPERATIONS:
                answer=await self.provider.execute_operation(job)
                await self.catalog_new_chat(job,answer)
                return answer
            if job.get('operation')=='edit_message':
                answer=await self.provider.execute_operation(job)
                await self.queue_edit_for_archive(job,answer)
                return answer
            if job.get('operation')!='search_messages_live': return await self.provider.execute_operation(job)
            job['_live_hits']=found=[]
            answer=await self.provider.execute_operation(job)
            created=await self.store_live_hits(found,deadline=job.get('_deadline'))
            # ANYM-478: search with live reports how many hits were new to the archive.
            if isinstance(answer,dict) and isinstance(answer.get('data'),dict):answer['data']['stored_new']=created
            return answer
        finally:
            if bound is not None:LIVE_STATE.reset(bound)

    async def catalog_new_chat(self,job,answer):
        """A group this account just created or joined enters the archive catalog now, not at the next dialog
        discovery (15 minutes or more later; a new basic group's only message is a service message, which live
        capture skips), and its receipt names the archive chat_id. The provider change has already happened:
        a failure or an exhausted time budget here leaves the chat to that discovery and never changes the
        operation's outcome. A confirmed create_group receipt always says which (ANYM-750): `archive` is
        `collected` with `chat_id`, or `not_collected` with an `archive_reason` from a closed vocabulary and its
        fixed `next_step`; the collection policy's reason comes first, never exception text."""
        from psycopg.errors import LockNotAvailable,QueryCanceled,TransactionTimeout
        from psycopg_pool import PoolTimeout
        from telethon import types,utils
        data=answer.get('data') if isinstance(answer,dict) and answer.get('outcome')=='confirmed' else None
        if not isinstance(data,dict):return
        failure,policy='catalog_failed',job.get('_policy')
        try:
            chat=data.get('provider_chat_id')
            if not isinstance(chat,str) or job['operation']=='join_chat' and data.get('archive')!='collected':return
            _,peer=utils.resolve_id(int(chat))
            if peer not in (types.PeerChat,types.PeerChannel):return
            expected=job.get('expected_chat') if isinstance(job.get('expected_chat'),dict) else {}
            title=(job.get('parameters') or {}).get('title') if job['operation']=='create_group' else expected.get('title')
            chat_type='group' if job['operation']=='create_group' or expected.get('kind')=='group' else 'channel'
            # Inside the command's own timeout, so a slow archive never turns a confirmed change into unknown.
            budget=min(CATALOG_SECONDS,job.get('_ends_at',math.inf)-time.monotonic()-STORE_GRACE-1)
            if budget<=0:
                failure='catalog_timeout';return
            state=bounded_state(self.state,time.monotonic()+budget)
            if await asyncio.wait_for(finished(state.catalog,chat,'chat' if peer is types.PeerChat else 'channel',
                    title if isinstance(title,str) and title else chat,chat_type,self.history_since),budget):
                data['chat_id']=stable_id('chat',self.account,chat)
            # A refusal is named by the policy the write was judged by, not the one read before the provider call.
            policy=getattr(state,'evaluated_policy',policy)
        except asyncio.CancelledError:
            # A shutdown during the catalog write: the worker settles the confirmed receipt first, then stops.
            job['_cancel_after_settle']=True
            log.warning('new_chat_catalog_failed operation=%s error=CancelledError',job.get('operation'))
        except (TimeoutError,QueryCanceled,LockNotAvailable,TransactionTimeout,PoolTimeout) as exc:
            failure='catalog_timeout'
            log.warning('new_chat_catalog_failed operation=%s error=%s',job.get('operation'),type(exc).__name__)
        except Exception as exc:
            log.warning('new_chat_catalog_failed operation=%s error=%s',job.get('operation'),type(exc).__name__)
        finally:
            if job.get('operation')=='create_group':
                with suppress(Exception):archive_receipt(data,policy,failure)

    async def queue_edit_for_archive(self,job,answer):
        """ANYM-747: a confirmed edit reaches the archive through the edit spool, which the drain loop applies with
        the live spool, so the drain makes it the next revision. Never through capture(): this is best-effort and
        total, it never fails the collector, and the operation stays confirmed whatever happens here (the live
        MessageEdited event and reconcile still apply). A capture slot is taken only when one is free at once (waiting
        for one could fail the collector) and is held until the spool write itself ends, also past a timeout. The
        write runs on its own one-thread executor, at most one at a time, under the edit spool's own lock: a stalled
        disk never takes the threads or the lock that receipts, sends, lease renewal and live captures use."""
        data=answer.get('data') if isinstance(answer,dict) and answer.get('outcome')=='confirmed' else None
        message=job.pop('_read_back_message',None)
        if not isinstance(data,dict) or message is None:return
        data['archive']='not_queued'
        try:
            budget=min(EDIT_ARCHIVE_SECONDS,job.get('_ends_at',math.inf)-time.monotonic()-STORE_GRACE-1)
            if budget<=0 or self.stopping.is_set() or self.failed.is_set():return
            pending=getattr(self,'_edit_archive_write',None)
            if pending is not None and not pending.done():return
            if self.capture_count>=CAPTURE_WRITES or self.capture_waiters:return
            e=telegram_message(self.account,message,verified_self=getattr(self.provider,'verified_self',None))
            policy=job.get('_policy') or self.policy
            if not policy or not self.state.allows(policy,e['chat']):return
            item={'kind':'message','peer_kind':peer_kind(message),'envelope':e,
                'captured_at':datetime.now(timezone.utc).isoformat()}
            self.capture_count+=1
            try:
                # The property is read on that thread too: creating the directory is filesystem work.
                write=asyncio.get_running_loop().run_in_executor(EDIT_ARCHIVE_EXECUTOR,lambda:self.edit_spool.put(item))
            except BaseException:
                self.release_capture();raise
            self._edit_archive_write=write
            # The slot follows the write, not this call: a timed-out or cancelled wait leaves it held until the end.
            write.add_done_callback(lambda done:(self.release_capture(),done.cancelled() or done.exception()))
            await asyncio.wait_for(asyncio.shield(write),budget)
            data['archive']='queued'
        except asyncio.CancelledError:
            # A shutdown during the spool write: the worker settles the confirmed receipt first, then stops.
            job['_cancel_after_settle']=True
            log.warning('edit_archive_queue_failed error=CancelledError')
        except Exception as exc:
            log.warning('edit_archive_queue_failed error=%s',type(exc).__name__)

    async def chat_history(self,job):
        """ANYM-477: one agent-requested history page, archived as live search hits are; stored_new counts new rows."""
        parameters=job.get('parameters') or {}
        default=None
        if not any(k in parameters for k in ('before_message','around_message','from_time','to_time')):
            # The default anchor is the oldest archived message, so each repeat reaches further back. It is
            # kept here too: each provider request's preflight replaces the job's payload fields (release-042 M1).
            default=job['provider_message_id']=await asyncio.to_thread(self.oldest_archived,job.get('chat_id'))
        job['_history']=found=[]
        answer=await self.provider.execute_operation(job)
        # Owner 2026-10-23: an explicit request for one named chat also stores its page below history_since;
        # background collection and live search hits keep the boundary.
        created=await self.store_live_hits(found,label='chat_history',deadline=job.get('_deadline'),window=False)
        data=answer.get('data')
        if isinstance(data,dict):
            data['stored_new']=created
            below=sum(1 for m,_,_,_ in found if m is not None and m.date and not self.in_window([m]))
            if below:data['below_history_since']=below
            # not_stored with retry_parameters covers only storage errors and the deadline (F-477a).
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
                elif default:
                    data['retry_parameters']={'count':parameters.get('count',50),'before_message':
                        stable_id('msg',self.account,str(job.get('provider_chat')),str(default))}
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

    async def store_live_hits(self,found,*,label='live_search',deadline=None,window=True):
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
            if window and message is not None and not self.in_window([message]):
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
    os.close(private_session_directory(session))  # a shared directory is refused before any lock or account check
    # File ownership spans client construction, which opens the SQLite session. ANYM-914: the canonical host lock
    # first, then the session flock, then the claim, and only then the TelegramClient (it opens the session store).
    with session_claim.host_lock(cfg.get('account'),cfg.get('session_lock_directory')) as host,session_lock(session):
        def open_store():
            return Store(Path(os.environ['DATABASE_URL_FILE']).read_text().strip(),
                         Path(os.environ['PROBE_FILES']),'http://collector.invalid')
        def check(store):
            with store.pool.connection() as c:
                row=c.execute('SELECT platform,synthetic FROM accounts WHERE id=%s',(cfg['account'],)).fetchone()
                if not row or row['platform']!='telegram' or row['synthetic']: raise RuntimeError('live_account_required')
        store=await session_claim.open_waiting(open_store,check,cfg['account'])
        try:
            archive=Archive(store)
            claim=await session_claim.acquire_waiting(store.pool,host,'collector')
            spool=CaptureSpool(cfg['spool_directory'],reserve_bytes=cfg['disk_reserve_bytes'])
            live_writer=(BlobWriter(store.files,max_bytes=cfg['media_live_max_bytes'],
                reserve_bytes=cfg['media_live_disk_reserve_bytes'])
                if cfg['media_live_disk_reserve_bytes'] is not None else None)
            worker=TelegramCollector(archive,cfg['account'],TelethonProvider(cfg,auth),spool,
                batch_size=cfg['batch_size'],interval=cfg['scan_interval_seconds'],max_dialogs=cfg['max_dialogs'],head_pages=cfg['head_pages_per_pass'],
                history_since=cfg['history_since'],media_prefetch_days=cfg['media_prefetch_days'],
                live_writer=live_writer,live_window_seconds=cfg['media_live_window_seconds'],
                media_backfill=cfg['media_backfill'],
                writer=BlobWriter(store.files,max_bytes=cfg['max_media_bytes'],reserve_bytes=cfg['media_disk_reserve_bytes']))
            worker.session_claim=(store.pool,claim)
            # Only a verified disconnect releases (run's finally); a failed or timed-out one keeps the row for this kernel.
            worker.release_claim=lambda: session_claim.release_bounded(store.pool,claim)
            loop=asyncio.get_running_loop()
            for sig in [signal.SIGTERM,signal.SIGINT]: loop.add_signal_handler(sig,worker.stopping.set)
            await worker.run()
        finally: store.close()


def main():
    from .store import name_sessions
    name_sessions('collector-telegram')  # ANYM-842: this service's sessions carry its name
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
