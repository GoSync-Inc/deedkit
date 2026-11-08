"""WhatsApp supervisor: fenced ownership, durable capture and bounded private IPC."""
import asyncio
import base64
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import stat
import time

from psycopg.types.json import Jsonb

from .identifiers import SEND_ID_RE
from .archive import Archive
from .blobs import BlobWriter
from .collector_state import CaptureSpool,CollectorState,session_lock,validate_capture as validate_capture
from .store import Store
from .collector_common import DbBackoff,commit_items,validate_common,validate_ranges
from .telegram_collector import TelegramCollector,ProviderUnavailable,private_json
from .error_codes import CODES
from .operational import CONNECTION_ERRORS,MEDIA_ERRORS,collector_stop
from .media_policy import MEDIA_POLICY_FIELDS,configure_media_policy

# How the adapter's error replies map to exceptions (error_codes.json, parity with the Go and JS adapters).
PROVIDER_REPLY=CODES['provider_reply']

# open_chat_by_phone: both adapter calls and the archive check (the native lookup's 10 s).
PHONE_RESOLUTION_SECONDS=10
# How long a stop waits for a child whose pipe closed to be reaped, so its exit status is known.
CHILD_EXIT_WAIT_SECONDS=2


def exit_detail(returncode):
    """The fixed CHILD_EXIT_DETAILS value of an adapter child's exit status; None while it runs."""
    if type(returncode) is not int:return None
    if returncode<0:return 'exit_killed' if returncode==-signal.SIGKILL else 'exit_signal'
    return {0:'exit_clean',1:'exit_error',2:'exit_panic'}.get(returncode,'exit_other')
# How long history waits for whatsmeow's own auto-reconnect: the connect_timeout a fresh start gets.
NOT_READY_WAIT_SECONDS=300
# ANYM-814: the repair calls are refused before any send while not ready, like a page read.
HISTORY_READS=('dialogs','page','gap_repair_status','gap_repair')


class ProviderOperationError(RuntimeError): pass
class ProviderNotReady(RuntimeError):
    """A history read answered provider_not_ready with no recorded failure; the pipe is in sync."""


def clear_browser_runtime_locks(session):
    """Only call while holding this account's session_lock, before Node starts."""
    try:fd=os.open(Path(session)/'session',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    except FileNotFoundError:return
    try:
        names=('SingletonLock','SingletonSocket','SingletonCookie')
        present=[]
        for name in names:
            try:info=os.stat(name,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError:continue
            if not stat.S_ISLNK(info.st_mode):raise ValueError('invalid_browser_lock')
            present.append(name)
        for name in present:os.unlink(name,dir_fd=fd)
        if present:os.fsync(fd)
    finally:os.close(fd)


def configuration(path):
    cfg=private_json(path)
    fields={'enabled','account','expected_wid','permit_file','session_directory','events_directory',
        'pages_directory','chromium_executable','node_executable','batch_size','scan_interval_seconds',
        'max_media_bytes','disk_reserve_bytes','max_dialogs',
        'provider','native_executable','whatsapp_gap_repair'}|MEDIA_POLICY_FIELDS
    validate_common(cfg,fields)
    if not re.fullmatch(r'[0-9]{5,20}@c.us',cfg.get('expected_wid','')): raise ValueError('invalid_whatsapp_identity')
    # Legacy configs retain their existing session; new setup templates select native.
    provider=cfg.setdefault('provider','webjs')
    if provider not in ('webjs','whatsmeow'):raise ValueError('invalid_whatsapp_provider')
    executables=['chromium_executable','node_executable'] if provider=='webjs' else ['native_executable']
    for name in ['permit_file','session_directory','events_directory','pages_directory',*executables]:
        if not isinstance(cfg.get(name),str) or not Path(cfg[name]).is_absolute(): raise ValueError('invalid_private_path')
    private=[Path(cfg[name]).resolve() for name in ['session_directory','events_directory','pages_directory']]
    if any(a==b or a in b.parents or b in a.parents for i,a in enumerate(private) for b in private[i+1:]):
        raise ValueError('collector_directories_overlap')
    validate_ranges(cfg,[('batch_size',50,1,100),('scan_interval_seconds',60,10,3600),
            ('max_media_bytes',512*1024**2,1,4*1024**3),('disk_reserve_bytes',1024**3,1024**3,100*1024**3),
            ('max_dialogs',2000,1,10000)])
    configure_media_policy(cfg)
    # ANYM-814: the phone repair of unverified spans is opt-in; the native adapter reads the same key.
    if type(cfg.setdefault('whatsapp_gap_repair',False)) is not bool:raise ValueError('invalid_whatsapp_gap_repair')
    if cfg['whatsapp_gap_repair'] and provider!='whatsmeow':raise ValueError('invalid_whatsapp_gap_repair')
    permit=private_json(cfg['permit_file'])
    if not isinstance(permit,dict) or permit.get('account')!=cfg['account'] or permit.get('expected_wid')!=cfg['expected_wid'] or permit.get('prior_clients_stopped') is not True:
        raise RuntimeError('session_handover_not_confirmed')
    return cfg


def position(event):
    if type(event.get('timestamp')) is not int or not isinstance(event.get('messageId'),str):
        raise ValueError('invalid_whatsapp_position')
    return [event['timestamp'],event['messageId']]


def stalled_catchup(progress,before,head,moved):
    """ANYM-813: a native catch-up whose pointer stays put on two consecutive scans records its span.

    The span [previous head, catchup_before] is unverified, neither a hole nor covered: it is written once,
    kept when the catch-up later completes and never replaced; a flagged gap chat gets none (ANYM-700)."""
    if not before or moved or progress.get('catchup_before')!=before:
        progress.pop('catchup_stalled_scans',None);progress.pop('catchup_stalled_since',None);return
    scans=progress.get('catchup_stalled_scans');scans=scans+1 if type(scans) is int else 1
    progress['catchup_stalled_scans']=scans
    if type(progress.get('catchup_stalled_since')) not in (int,float):
        progress['catchup_stalled_since']=progress.get('last_attempt_at')
    if scans<2 or progress.get('gap') is True or 'unverified_span' in progress:return
    from datetime import datetime,timezone
    def at(value):
        with suppress(TypeError,ValueError,OverflowError,OSError):return datetime.fromtimestamp(value,timezone.utc).isoformat()
    since=progress.get('catchup_stalled_since')
    span={'from':at(head[0]) if isinstance(head,list) and head else None,
        'to':at(before[0]) if isinstance(before,list) and before else None,
        'stalled_since':at(since) if type(since) in (int,float) else None}
    if span['from'] and span['to']:progress['unverified_span']=span


def merge_gap_clear(progress,stored,gap_set):
    """ANYM-700: keep an operator's clear committed after this pass began (progress is written back whole).
    Anything unexpected leaves progress as it is: the flag stays, the page is not failed."""
    if not isinstance(stored,dict):return
    start,cleared=progress.get('last_attempt_at'),stored.get('gap_cleared_at')
    if type(start) not in (int,float) or type(cleared) not in (int,float) or cleared<=start:return
    progress['gap_cleared_at']=cleared;progress['gap_cleared_by']=stored.get('gap_cleared_by')
    if not gap_set:progress.pop('gap',None)


GAP_REPAIR_TERMINAL=frozenset({'verified','phone_has_no_older','budget_exhausted','request_uncertain','incomplete_answer'})
GAP_REPAIR_SPAN_ANSWERS=10


def gap_span_id(chat,span):
    """ANYM-814: a stable id of one recorded span; the span itself is immutable (ANYM-813)."""
    import hashlib
    return hashlib.sha256('\n'.join((chat,span['from'],span['to'])).encode()).hexdigest()[:32]


def clean_repair(repair):
    repair=dict(repair) if isinstance(repair,dict) else {}
    repair['state']=repair.get('state') if isinstance(repair.get('state'),str) else 'waiting'
    for key in ('request_ids','no_answer_ids'):
        repair[key]=[v for v in repair.get(key) or [] if isinstance(v,str)]
    repair['answered']=repair.get('answered') if type(repair.get('answered')) is int else 0
    return repair


def advance_gap_repair(span,repair,rows):
    """ANYM-814: apply the adapter's request rows of one span to its repair record (a new dict).

    Only an answered row counts: the adapter marks a row answered on the ON_DEMAND history sync whose
    PeerDataRequestSessionID is that request's id. The span closes only on such an answer that reaches
    its `from`; the cursor moves to the answer's oldest message, the span stays as recorded."""
    from datetime import datetime,timezone
    repair=clean_repair(repair)
    if repair['state'] in GAP_REPAIR_TERMINAL:return repair
    start=datetime.fromisoformat(span['from']).timestamp()
    seen=set(repair['request_ids'])|set(repair['no_answer_ids'])
    outstanding=False
    for row in rows:
        rid,state=row['request_id'],row['state']
        if state in ('reserved','sending','sent'):outstanding=True;continue
        if rid in seen:continue
        seen.add(rid)
        if state=='no_answer':repair['no_answer_ids'].append(rid);continue
        if state=='uncertain':
            repair['request_ids'].append(rid);repair['state']='request_uncertain';break
        if state!='answered':continue
        repair['request_ids'].append(rid);repair['answered']+=1
        oldest,got,count=row['oldest_ts'],row['got'],row['count']
        # A message the adapter's capture did not keep breaks continuity: never verified (Astra r1 B3).
        if row.get('skipped',0):
            repair.update(state='incomplete_answer',remaining={'from':span['from'],'to':span['to']});break
        if got>0 and oldest<=start:
            repair.update(state='verified',verified_by='phone_history');break
        if got<count:repair['state']='phone_has_no_older';break
        repair['cursor_request']=rid
        if repair['answered']>=GAP_REPAIR_SPAN_ANSWERS:
            repair.update(state='budget_exhausted',
                remaining={'from':span['from'],'to':datetime.fromtimestamp(oldest,timezone.utc).isoformat()})
            break
    if repair['state'] not in GAP_REPAIR_TERMINAL:repair['state']='outstanding' if outstanding else 'waiting'
    return repair


def valid_gap_status(status):
    """The adapter's gap_repair_status reply, or None: a malformed reply is no report and asks nothing."""
    if not isinstance(status,dict) or not isinstance(status.get('requests'),list):return None
    if type(status.get('outstanding')) is not int or type(status.get('paused_until_ms')) is not int:return None
    for row in status['requests']:
        if (not isinstance(row,dict) or not all(isinstance(row.get(k),str) for k in ('request_id','span_id','state'))
                or not all(type(row.get(k)) is int for k in ('count','got','oldest_ts'))
                or type(row.get('skipped',0)) is not int):return None
    return status


class WhatsAppState(CollectorState):
    def provider_observation(self,name):
        if name not in ('webjs','whatsmeow'):raise ValueError('invalid_whatsapp_provider')
        with self.archive.store.pool.connection() as c:
            row=self._operations(c);value=row['operations']
            value['provider']={'name':name,'contract_version':1,'observed_at':row['observed'].isoformat()}
            value.pop('provider_history',None);value.pop('provider_repair',None);value.pop('gap_repair',None)
            self._save_operations(c,value)

    def native_history_observation(self,status):
        # Only bounded machine diagnostics leave the private native spool.
        status=validate_native_history_status(status)
        with self.archive.store.pool.connection() as c:
            row=self._operations(c);value=row['operations']
            if value.get('provider',{}).get('name')!='whatsmeow':return
            value['provider_history']={**status,'checked_at':row['observed'].isoformat(),
                'scope':'latest_native_history_batch','full_history_verified':False}
            self._save_operations(c,value)

    def native_repair_observation(self,status):
        # ANYM-809: the native journal's repair count; fixed integers and one fixed code only.
        status=validate_native_repair_status(status)
        with self.archive.store.pool.connection() as c:
            row=self._operations(c);value=row['operations']
            if value.get('provider',{}).get('name')!='whatsmeow':return
            value['provider_repair']={**status,'checked_at':row['observed'].isoformat(),
                'scope':'native_capture_journal','publishes':False}
            self._save_operations(c,value)

    def progress(self,chat,kind):
        self.chat(chat,kind)
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            c.execute('INSERT INTO whatsapp_checkpoints(account,provider_chat) VALUES(%s,%s) ON CONFLICT DO NOTHING',
                (self.account,chat))
            return c.execute('SELECT progress FROM whatsapp_checkpoints WHERE account=%s AND provider_chat=%s',
                (self.account,chat)).fetchone()['progress']

    def begin_attempt(self,chat,kind):
        self.progress(chat,kind)
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            # Separate from successful scan/cursors; survives a browser crash.
            return c.execute("""UPDATE whatsapp_checkpoints SET progress=jsonb_set(progress,
                '{last_attempt_at}',to_jsonb(extract(epoch FROM clock_timestamp())))
                WHERE account=%s AND provider_chat=%s RETURNING progress""",(self.account,chat)).fetchone()['progress']

    def gap_repair_targets(self):
        """ANYM-814: chats with a recorded unverified span (a flagged gap with one included)."""
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            return c.execute("""SELECT provider_chat,progress FROM whatsapp_checkpoints
                WHERE account=%s AND progress ? 'unverified_span' ORDER BY provider_chat""",(self.account,)).fetchall()

    def save_gap_repair(self,chat,repair):
        # Only this key: the span, the gap flag and an operator's clear stay as they are (ANYM-700).
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            c.execute("""UPDATE whatsapp_checkpoints SET progress=jsonb_set(progress,'{gap_repair}',%s)
                WHERE account=%s AND provider_chat=%s""",(Jsonb(repair),self.account,chat))

    def gap_repair_observation(self,status):
        from datetime import datetime,timezone
        paused=status['paused_until_ms']
        with self.archive.store.pool.connection() as c:
            row=self._operations(c);value=row['operations']
            if value.get('provider',{}).get('name')!='whatsmeow':return
            value['gap_repair']={'enabled':status.get('enabled') is True,'outstanding':status['outstanding'],
                'paused_until':datetime.fromtimestamp(paused/1000,timezone.utc).isoformat() if paused else None,
                'reason':'phone_unreachable' if status.get('reason')=='phone_unreachable' else None,
                'checked_at':row['observed'].isoformat()}
            self._save_operations(c,value)

    def scan_order(self,dialogs):
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            rows=c.execute("SELECT provider_chat,progress FROM whatsapp_checkpoints WHERE account=%s",(self.account,)).fetchall()
        attempted={r['provider_chat']:r['progress'].get('last_attempt_at',0) for r in rows}
        return sorted(dialogs,key=lambda d:attempted.get(d[0],0))

    def checkpoint_wa(self,chat,progress,catchup_moved=False,gap_set=False):
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            # The row lock orders this write against collector_control.clear_gap.
            stored=c.execute('SELECT progress FROM whatsapp_checkpoints WHERE account=%s AND provider_chat=%s FOR UPDATE',
                (self.account,chat)).fetchone()
            if stored:merge_gap_clear(progress,stored['progress'],gap_set)
            # ANYM-454: the catch-up progress marker uses the database clock, like last_attempt_at, in this one UPDATE.
            c.execute("""UPDATE whatsapp_checkpoints SET progress=%s::jsonb||CASE WHEN %s THEN jsonb_build_object(
                'catchup_progress_at',extract(epoch FROM clock_timestamp())) ELSE '{}'::jsonb END,
                last_scan_at=clock_timestamp() WHERE account=%s AND provider_chat=%s""",
                (Jsonb(progress),catchup_moved,self.account,chat))
            c.execute('UPDATE collector_chats SET last_error_code=NULL WHERE account=%s AND provider_chat=%s',
                (self.account,chat))
            if not progress.get('gap') and not progress.get('catchup_before'):
                c.execute('UPDATE collector_chats SET last_head_scan_at=clock_timestamp() '
                    'WHERE account=%s AND provider_chat=%s',(self.account,chat))


def whatsapp_asset(name):
    """Installed wheels bundle Node sources; legacy checkout layout still works."""
    if name not in ('provider.mjs','pair.mjs'):
        raise ValueError('invalid_whatsapp_asset')
    override=os.environ.get('ANYMSG_WHATSAPP_ASSETS_DIR')
    if override:
        root=Path(override)
        if not root.is_absolute() or not stat.S_ISDIR(root.lstat().st_mode):
            raise ValueError('invalid_whatsapp_asset_directory')
        asset=root/name
        if not stat.S_ISREG(asset.lstat().st_mode):raise ValueError('invalid_whatsapp_asset')
        return asset
    package=Path(__file__).resolve().parent
    bundled=package/'whatsapp_assets'/name
    if bundled.is_file():return bundled
    legacy=package.parent/'whatsapp'/name
    if legacy.is_file():return legacy
    monorepo=package.parents[2]/'whatsapp'/name
    if monorepo.is_file():return monorepo
    raise RuntimeError('whatsapp_assets_missing')


class NodeProvider:
    # ANYM-813: only the native cache can stall a catch-up for good (it never reports exhausted).
    native_history=False
    def __init__(self,cfg,config_path,lock_fd,*,script=None):
        self.cfg,self.config_path,self.lock_fd=cfg,str(config_path),lock_fd
        self.script=script or whatsapp_asset('provider.mjs')
        self.process=None; self.lock=asyncio.Lock(); self.counter=0; self.policy=None
        self.send_lock=asyncio.Lock();self.disconnect_lock=asyncio.Lock()
        self.send_reader=None;self.send_writer=None;self.started=False
        self.not_ready_since=None # Start of the current provider_not_ready streak of history reads.
    def subscribe(self,*_): pass # Provider writes callbacks to its own disk queue, never RAM polling.
    def bind_state(self,state):self.state=state
    def command(self):
        return [self.cfg['node_executable'],str(self.script),self.config_path]
    def prepare_session(self):
        if self.cfg.get('session_directory'):clear_browser_runtime_locks(self.cfg['session_directory'])
    async def connect(self):
        if self.process and self.process.returncode is None:raise RuntimeError('provider_already_connected')
        self.prepare_session()
        if self.cfg.get('events_directory'):
            directory=Path(self.cfg['events_directory']);directory.mkdir(mode=0o700,parents=True,exist_ok=True)
            (directory/'.transport-failure').unlink(missing_ok=True)
        # Inherited, anonymous socket: no TCP listener, filesystem endpoint or
        # second browser. Media backpressure cannot consume the sending pipe.
        parent,child=socket.socketpair()
        try:
            env={**os.environ,'ANYMSG_WHATSAPP_SEND_FD':str(child.fileno())}
            self.process=await asyncio.create_subprocess_exec(*self.command(),
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
                limit=1024*1024+1,pass_fds=(self.lock_fd,child.fileno()),env=env,start_new_session=True)
            child.close()
            parent.setblocking(False)
            self.send_reader,self.send_writer=await asyncio.open_connection(sock=parent,limit=1024*1024+1)
            await self.call('start',{'policy':self.policy})
            # Both adapters answer start only after the session's own ID equals expected_wid.
            self.started=True
            if hasattr(self,'state'):
                await asyncio.to_thread(self.state.provider_observation,self.cfg.get('provider','webjs'))
        except BaseException:
            parent.close()
            await self.disconnect()
            raise
        finally:child.close()
    async def disconnect(self):
        async with self.disconnect_lock:
            await self._disconnect()
    def self_provider_id(self):
        running=self.started and self.process is not None and self.process.returncode is None
        return self.cfg.get('expected_wid') if running else None
    async def _disconnect(self):
        self.started=False
        if self.send_writer:
            self.send_writer.close()
            with suppress(OSError):await self.send_writer.wait_closed()
            self.send_writer=None;self.send_reader=None
        if not self.process:return
        if self.process.returncode is None:
            with suppress(ProcessLookupError):self.process.terminate()
            try:await asyncio.wait_for(self.process.wait(),12)
            except TimeoutError:
                with suppress(ProcessLookupError): os.killpg(self.process.pid,signal.SIGKILL)
                await self.process.wait()
        # Chromium descendants belong to this dedicated process group only.
        with suppress(ProcessLookupError): os.killpg(self.process.pid,signal.SIGTERM)
    def failure_code(self):
        try:
            value=private_json(Path(self.cfg['events_directory'])/'.transport-failure')
            code=value.get('code') if isinstance(value,dict) else None
            return code if isinstance(code,str) and code in CONNECTION_ERRORS else 'provider_disconnected'
        except (OSError,ValueError,KeyError):return 'provider_disconnected'
    def failure_recorded(self):
        directory=self.cfg.get('events_directory')
        return bool(directory) and (Path(directory)/'.transport-failure').exists()
    async def child_stop(self):
        """The stop of an ended child: its recorded code, else provider_disconnected with how it exited."""
        stop=RuntimeError(self.failure_code())
        # A child that recorded no failure still says how it ended (Tiden 1d532d67 had only the bare code).
        if str(stop)=='provider_disconnected' and self.process and not self.failure_recorded():
            # A closed pipe can precede the reaped exit; read the status before any cleanup kills the child.
            with suppress(TimeoutError):await asyncio.wait_for(self.process.wait(),CHILD_EXIT_WAIT_SECONDS)
            stop.detail=exit_detail(self.process.returncode)
        return stop
    async def wait_disconnected(self):
        await self.process.wait()
        raise await self.child_stop()
    async def request(self,method,params,*,sending=False):
        if not self.process or self.process.returncode is not None: raise await self.child_stop()
        self.counter+=1
        request_id=self.counter
        payload=json.dumps({'id':request_id,'method':method,'params':params},separators=(',',':')).encode()+b'\n'
        if len(payload)>1024*1024: raise ValueError('provider_request_too_large')
        writer=self.send_writer if sending else self.process.stdin
        if writer is None:raise RuntimeError('provider_disconnected')
        try:writer.write(payload); await writer.drain()
        except (BrokenPipeError,ConnectionResetError):raise await self.child_stop() from None
        return request_id
    async def reply(self,request,timeout=90,*,sending=False):
        reader=self.send_reader if sending else self.process.stdout
        if reader is None:raise RuntimeError('provider_disconnected')
        line=await asyncio.wait_for(reader.readline(),timeout)
        if not line: raise await self.child_stop()
        if len(line)>1024*1024: raise RuntimeError(self.failure_code())
        result=json.loads(line)
        if not isinstance(result,dict) or result.get('id')!=request: raise RuntimeError('provider_protocol_error')
        if 'error' in result:
            code=result['error']
            if isinstance(code,str) and code in PROVIDER_REPLY['command_rejected']:
                from .commands import CommandRejected
                raise CommandRejected(code)
            if code in PROVIDER_REPLY['send_rejected']:
                from .outgoing import SendRejected
                raise SendRejected(code)
            if code=='provider_not_ready':raise ProviderNotReady(code)
            if isinstance(code,str) and code in CONNECTION_ERRORS:raise RuntimeError(code)
            if code in PROVIDER_REPLY['media_unavailable']: raise ProviderUnavailable(code)
            safe=MEDIA_ERRORS|set(PROVIDER_REPLY['operation_safe'])
            raise ProviderOperationError(code if isinstance(code,str) and code in safe else 'provider_operation_failed')
        return result
    @property
    def send_gate(self): return self.send_lock

    async def call(self,method,params):
        if method in ('send_text','messenger_operation'):raise RuntimeError('send_gate_required')
        async with self.lock:
            return await self._call_unlocked(method,params)

    async def _call_unlocked(self,method,params,*,sending=False):
        try:
            request=await self.request(method,params,sending=sending)
            reply=await self.reply(request,timeout=300 if method=='start' else 90,sending=sending)
            if set(reply)!={'id','result'}: raise RuntimeError('provider_protocol_error')
            if method in HISTORY_READS:self.not_ready_since=None
            return reply['result']
        except (ProviderUnavailable,ProviderOperationError):
            # Complete error frame; pipe is still synchronized. The provider answered, so it is ready.
            if method in HISTORY_READS:self.not_ready_since=None
            raise
        except ProviderNotReady:
            # whatsmeow reconnects by itself after a short drop; a disconnect here would cut that off.
            if method in HISTORY_READS and not self.failure_recorded():
                if self.not_ready_since is None:self.not_ready_since=time.monotonic()
                raise
            await self.disconnect(); raise RuntimeError('provider_not_ready') from None
        except Exception as exc:
            from .outgoing import SendRejected
            from .commands import CommandRejected
            if isinstance(exc,(SendRejected,CommandRejected)):raise
            await self.disconnect(); raise
        except BaseException:
            # After cancellation/partial response, never reuse a desynchronized pipe.
            await self.disconnect(); raise
    async def set_policy(self,policy):
        self.policy={k:policy[k] for k in ('discover_chats','include_chats','exclude_chats')}
        self.policy.update(enabled=policy.get('enabled',True),send_enabled=policy.get('send_enabled',False))
        self.policy['operations_enabled']=policy.get('operations_enabled',[])
        # Separate control file: a long media response must not delay exclusions
        # or lease liveness. Node fails closed when this short-lived policy expires.
        def save():
            directory=Path(self.cfg['events_directory']);directory.mkdir(mode=0o700,parents=True,exist_ok=True)
            temporary=directory/('.policy-'+secrets.token_hex(8))
            try:
                fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'w') as out:
                    json.dump({'expires_at':time.time()+45,'policy':self.policy},out);out.flush();os.fsync(out.fileno())
                os.replace(temporary,directory/'.policy')
            finally:temporary.unlink(missing_ok=True)
        await asyncio.to_thread(save)
    async def dialogs(self,limit): return await self.call('dialogs',{'limit':limit})
    async def page(self,chat,*,limit,before=None): return await self.call('page',{'chat':chat,'limit':limit,'before':before})
    async def send_text(self,job):
        # OutgoingWorker holds send_gate before claiming a queued job.
        if not self.send_lock.locked():raise RuntimeError('send_gate_required')
        from .outgoing import dispatch_payload
        report=job.get('_provider_phase')
        if report is not None:report('waiting')
        payload=dispatch_payload(job,'whatsapp')
        if job.get('_preflight') is not None:await job['_preflight']()
        # Conservative: from here the adapter may already have the request.
        if report is not None:report('started')
        return await self._call_unlocked('send_text',{'chat':job['provider_chat'],'text':payload['text'],
            'link_preview':payload['link_preview'],
            **({'send_id':job['id']} if SEND_ID_RE.fullmatch(job.get('id','')) else {}),
            'reply_provider_id':job.get('reply_provider_id'),'expires_at':job['expires_at']},sending=True)
    async def execute_operation(self,job):
        if not self.send_lock.locked():raise RuntimeError('send_gate_required')
        report=job.get('_provider_phase')
        if report is not None:report('waiting')
        if job.get('_preflight') is not None:await job['_preflight']()
        # Internal callable/DB metadata never enters the subprocess protocol.
        payload={key:value for key,value in job.items() if key in (
            'operation','parameters','provider_chat','provider_message_id','expires_at',
            'source_provider_chat','source_provider_message','registered_file_path','trusted_blob_directory',
            'file_digest','file_bytes','file_mime','file_name','known_chats','lookup_allowed','expected_chat')}
        if report is not None:report('started')
        if job['operation']=='open_chat_by_phone' and job.get('_archived') is not None:
            # First ask only the session store for the number's LID, then offer the
            # adapter that exact chat if the archive holds it (local answer, no query).
            # Both calls and the archive check share one deadline well inside the
            # worker's 30 s timeout: running out between the calls is a retryable
            # provider_timeout on a synchronized pipe, never a disconnect.
            from .commands import CommandRejected
            deadline=time.monotonic()+PHONE_RESOLUTION_SECONDS
            local=await self._call_unlocked('messenger_operation',{**payload,'local_only':True,'known_chats':[]},sending=True)
            lid=local.get('data',{}).get('local_lid') if isinstance(local,dict) and isinstance(local.get('data'),dict) else None
            archived=False
            if job.get('expected_lid') is not None:
                # ANYM-532: a phone send reuses its preview's lookup. The session store must still map
                # the number to that LID, which then answers locally; any other answer asks for a new preview.
                if lid!=job['expected_lid']:raise CommandRejected('identity_changed')
                archived=True;payload['lookup_allowed']=False  # never a second, counted lookup
            elif isinstance(lid,str) and re.fullmatch(r'[0-9]{1,30}@lid',lid):
                try:archived=await asyncio.wait_for(job['_archived'](lid),max(deadline-time.monotonic(),0))
                except TimeoutError:raise CommandRejected('provider_timeout') from None
            payload['known_chats']=[lid] if archived else []
            remaining=int((deadline-time.monotonic())*1000)
            if remaining<100:raise CommandRejected('provider_timeout')
            payload['lookup_ms']=remaining
            if job.get('expected_lid') is not None:
                # With no lookup allowed, the adapter answers rate_limited when the LID left its
                # chat list between the two calls: that is a changed identity, not a quota.
                try:return await self._call_unlocked('messenger_operation',payload,sending=True)
                except CommandRejected as exc:
                    if str(exc)=='rate_limited':raise CommandRejected('identity_changed') from None
                    raise
        return await self._call_unlocked('messenger_operation',payload,sending=True)
    async def chunks(self,source,descriptor):
        complete=False
        async with self.lock:
            request=await self.request('media',{'chat':source['provider_chat'],'message':source['provider_id'],'descriptor':descriptor})
            try:
                while True:
                    result=await self.reply(request)
                    if 'chunk' in result:
                        if set(result)!={'id','chunk'} or len(result['chunk'])>87384: raise RuntimeError('provider_protocol_error')
                        chunk=base64.b64decode(result['chunk'],validate=True)
                        if len(chunk)>65536: raise RuntimeError('provider_protocol_error')
                        yield chunk
                    else:
                        if result.get('result')!={'verified':True}: raise RuntimeError('provider_protocol_error')
                        complete=True;return
            except (ProviderUnavailable,RuntimeError) as exc:
                # A complete error response is synchronized and safe for the next job.
                if str(exc) in MEDIA_ERRORS|{'provider_operation_failed','chat_excluded'}:complete=True
                raise
            finally:
                if not complete: await self.disconnect()


def validate_native_history_status(value):
    from .envelope import utc
    fields={'state','reason','attempt','max_attempts','next_attempt_ms','retry','coverage','observed_at'}
    if (not isinstance(value,dict) or set(value)!=fields
        or value['state'] not in ('downloading','retrying','failed','captured')
        or value['reason'] not in ('','history_attempts_exhausted','history_download_failed',
            'history_capture_failed','invalid_history_notification')
        or type(value['attempt']) is not int or not 1<=value['attempt']<=3
        or type(value['max_attempts']) is not int or value['max_attempts']!=3
        or type(value['next_attempt_ms']) is not int or not 0<=value['next_attempt_ms']<2**53
        or value['retry'] not in ('bounded_passive_download','explicit_only','none')
        or value['coverage']!='partial'):
        raise ValueError('invalid_native_history_status')
    return {**value,'observed_at':utc(value['observed_at'])}


NATIVE_REPAIR_STATES=('would_add_content','label_only','same','no_raw','placeholder','sender_key_only','parse_error')


def validate_native_repair_status(value):
    from .envelope import utc
    def count(x):return type(x) is int and 0<=x<2**53
    if (not isinstance(value,dict) or set(value)!={'version','done','rows_scanned','counts','last_error','observed_at'}
        or not count(value['version']) or not 1<=value['version']<=10**6 or type(value['done']) is not bool
        or not count(value['rows_scanned']) or value['last_error'] not in ('','repair_read','repair_storage')
        or not isinstance(value['counts'],dict) or set(value['counts'])!=set(NATIVE_REPAIR_STATES)
        or not all(count(x) for x in value['counts'].values())):
        raise ValueError('invalid_native_repair_status')
    try:observed=utc(value['observed_at'])
    except (ValueError,TypeError,OverflowError):raise ValueError('invalid_native_repair_status') from None
    return {**value,'counts':{k:value['counts'][k] for k in NATIVE_REPAIR_STATES},'observed_at':observed}


class WhatsmeowProvider(NodeProvider):
    """The native child shares the reviewed two-lane IPC, lease and outbox gates.

    Only process launch differs. This class never starts Node or touches Chromium
    profile locks, and an error never switches it to another provider/session.
    """
    native_history=True
    def __init__(self,cfg,config_path,lock_fd):
        super().__init__(cfg,config_path,lock_fd,script=Path(cfg['native_executable']))
    def command(self):
        return [self.cfg['native_executable'],'--config',self.config_path]
    def prepare_session(self):
        pass
    def history_status(self):
        try:return validate_native_history_status(private_json(Path(self.cfg['events_directory'])/'.history-status'))
        except FileNotFoundError:return None
    def repair_status(self):
        # ANYM-809: an optional diagnostic. A missing, unsafe, unreadable or malformed file
        # (deep nesting, an out-of-range time) is no report; it never stops the heartbeat.
        try:return validate_native_repair_status(private_json(Path(self.cfg['events_directory'])/'.repair-status'))
        except Exception:return None


def provider_for_config(cfg,path,lock_fd):
    cls=WhatsmeowProvider if cfg.get('provider')=='whatsmeow' else NodeProvider
    return cls(cfg,path,lock_fd)


def require_paired_session(cfg):
    session=Path(cfg['session_directory'])
    if cfg.get('provider')=='whatsmeow':
        auth=session/'auth.sqlite'
        try:info=auth.lstat()
        except FileNotFoundError:raise RuntimeError('whatsapp_pairing_required') from None
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:
            raise RuntimeError('private_session_required')
        # Device identity and presence of an actual linked account are checked
        # again by whatsmeow before it connects. A file alone is not a login.
    elif not (session/'session'/'Default').is_dir():
        raise RuntimeError('whatsapp_pairing_required')


class WhatsAppCollector(TelegramCollector):
    async def send_outgoing(self,job):
        # Node durable message_create + explicit capture owns this observation.
        return await self.provider.send_text(job)
    state_class=WhatsAppState
    coverage_code='whatsapp_history_partial'
    connect_timeout=300
    retry_connect=True
    gap_repair=False  # ANYM-814: also off for an instance built without __init__.
    gap_repair_reported=False
    @property
    def history_spool(self):
        # WhatsApp pages already have their own spool; live events use events_spool.
        return self.spool
    def __init__(self,*args,events_spool,gap_repair=False,**kwargs):
        kwargs.setdefault('max_dialogs',2000)
        super().__init__(*args,**kwargs); self.events_spool=events_spool
        # ANYM-814: off unless the collector config says true; off means no repair call of any kind.
        self.gap_repair=gap_repair is True
        from .spool import DurableSpool
        from .outgoing import validate_send_receipt
        self.send_receipts=DurableSpool(events_spool.path/'send-receipts',validator=validate_send_receipt,
            max_bytes=4*1024**2,max_entries=1000,reserve_bytes=events_spool.reserve_bytes)
    async def heartbeat(self):
        from .telegram_collector import database_errors
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            await self.renew_lease()
            await self.refresh_policy()
            await self.publish_disk_floors()
            await self.provider.set_policy(self.policy)
            if isinstance(self.provider,WhatsmeowProvider):
                status=await asyncio.to_thread(self.provider.history_status)
                if status is not None:
                    try:await asyncio.to_thread(self.state.native_history_observation,status)
                    except database_errors():pass  # Diagnostics only; the next heartbeat reports again.
                # ANYM-809: repair_status never raises; a lost lease below still ends the heartbeat.
                repair=await asyncio.to_thread(self.provider.repair_status)
                if repair is not None:
                    try:await asyncio.to_thread(self.state.native_repair_observation,repair)
                    except database_errors():pass
    async def commit_page_wa(self,page,chat,title,origin):
        if not isinstance(page,dict) or set(page)!={'messages','exhausted','pending'} or not isinstance(page['messages'],list) or len(page['messages'])>self.batch_size:
            raise RuntimeError('invalid_whatsapp_page')
        points=[]
        from datetime import datetime,timezone
        def check(e):
            if not isinstance(e,dict) or e.get('chatId')!=chat: raise RuntimeError('provider_chat_mismatch')
        def build(e):
            return {'kind':'whatsapp_event','account':self.account,'event':{**e,'chatName':title},
                    'change':'message','observed_at':datetime.now(timezone.utc).isoformat(),'origin':origin}
        def unstorable(e):
            with suppress(ValueError,TypeError,KeyError):points.append(position(e))
        # Deterministically unstorable entries (a spool ValueError, TypeError or KeyError): keep traversing so
        # one message cannot stall this chat's history or the account's capture.
        await commit_items(self,page['messages'],check=check,build=build,build_errors=(),
            put_errors=(ValueError,TypeError,KeyError),record=lambda e:points.append(position(e)),
            record_unstorable=unstorable)
        return sorted(points)
    async def sync_chat(self,chat,kind,title):
        progress=await asyncio.to_thread(self.state.begin_attempt,chat,kind)
        catchup_moved=gap_set=False
        head=progress.get('head')
        if not head:
            page=await self.provider.page(chat,limit=self.batch_size)
            points=await self.commit_page_wa(page,chat,title,'backfill')
            if points: progress.update(head=points[-1],history_before=points[0])
            progress['history_exhausted']=page['exhausted'] and not page['pending']
        else:
            before=progress.get('catchup_before')
            page=await self.provider.page(chat,limit=self.batch_size,before=before)
            points=await self.commit_page_wa(page,chat,title,'reconcile')
            target=progress.get('catchup_head') or (max(head,points[-1]) if points else head)
            reached=bool(points and points[0]<=head)
            if reached or (page['exhausted'] and not page['pending']):
                progress['head']=target
                if not reached and target>head: progress['gap']=gap_set=True
                progress.pop('catchup_before',None);progress.pop('catchup_head',None)
                if before:catchup_moved=True
            elif points:
                if points[0]!=before:catchup_moved=True
                progress['catchup_before']=points[0];progress['catchup_head']=target
            if getattr(self.provider,'native_history',False) is True:stalled_catchup(progress,before,head,catchup_moved)
            if not progress.get('catchup_before') and not progress.get('history_exhausted') and progress.get('history_before'):
                old=await self.provider.page(chat,limit=self.batch_size,before=progress['history_before'])
                old_points=await self.commit_page_wa(old,chat,title,'backfill')
                if old_points:progress['history_before']=min(progress['history_before'],old_points[0])
                progress['history_exhausted']=old['exhausted'] and not old['pending']
        # A failure during any page above leaves the last committed checkpoint.
        await asyncio.to_thread(self.state.checkpoint_wa,chat,progress,catchup_moved,gap_set)
        return bool(progress.get('gap'))
    async def sync_once(self):
        await asyncio.to_thread(self.state.phase,'history','scanning','history_scan')
        self.policy=await asyncio.to_thread(self.state.policy)
        await self.provider.set_policy(self.policy)
        dialogs=await self.provider.dialogs(self.max_dialogs)
        # Catalog entries exist even before a chat's first message page is saved.
        await asyncio.to_thread(self.state.inventory,dialogs,None)
        dialogs=await asyncio.to_thread(self.state.scan_order,dialogs)
        seen=set();gap=False;unavailable=False
        for dialog in dialogs:
            chat,kind,title=dialog[:3]
            if not self.state.allows(self.policy,chat): continue
            try:gap=await self.sync_chat(chat,kind,title) or gap;seen.add(chat)
            except (ProviderOperationError,ProviderUnavailable):
                unavailable=True
                await asyncio.to_thread(self.state.failed,chat,'provider_unavailable')
        missing=set(self.policy['include_chats'])-set(self.policy['exclude_chats'])-seen
        # A catch-up gap is missing history, not a live-capture failure: it stays
        # visible as history.gap_chats and the detail code, while live receiving
        # is reported as such. Unavailable chats still degrade collection.
        code='configured_chat_unavailable' if missing else ('whatsapp_chat_unavailable' if unavailable else
            ('whatsapp_catchup_gap' if gap else
             ('dialog_discovery_limited' if len(dialogs)>=self.max_dialogs else self.coverage_code)))
        await asyncio.to_thread(self.state.status,'degraded' if missing or unavailable else 'receiving',code)
        await asyncio.to_thread(self.state.phase,'history','waiting','history_scan_finished')
        if self.gap_repair:await self.repair_gaps()
        elif not self.gap_repair_reported and getattr(self.provider,'native_history',False) is True:
            # Off: say so locally, once per process, without any adapter call (Astra r1 M1).
            await asyncio.to_thread(self.state.gap_repair_observation,{'enabled':False,'outstanding':0,'paused_until_ms':0})
            self.gap_repair_reported=True
    async def repair_gaps(self):
        """ANYM-814: one step of the phone repair: read the adapter's requests, update each span, ask for at
        most one. The request returns after the send; the answer arrives through the adapter's history worker.
        A provider refusal or a malformed reply ends this step only: the scan above is already committed."""
        if getattr(self.provider,'native_history',False) is not True:return
        from .commands import CommandRejected
        # Expected adapter refusals (rate limit, anchor, caps) end this step only (Astra r1 B1).
        refused=(ProviderOperationError,ProviderUnavailable,CommandRejected)
        try:status=valid_gap_status(await self.provider.call('gap_repair_status',{}))
        except refused:return
        if status is None:return
        rows={}
        for row in status['requests']:rows.setdefault(row['span_id'],[]).append(row)
        from .sync_status import unverified_span
        candidates=[]
        for target in await asyncio.to_thread(self.state.gap_repair_targets):
            chat,stored=target['provider_chat'],target['progress']
            if not unverified_span(stored):continue
            span=stored['unverified_span'];span_id=gap_span_id(chat,span)
            old=stored.get('gap_repair')
            repair={**advance_gap_repair(span,old,rows.get(span_id,[])),'span_id':span_id}
            if repair!=old:await asyncio.to_thread(self.state.save_gap_repair,chat,repair)
            if repair['state']=='waiting' and self.state.allows(self.policy,chat):candidates.append((chat,span,repair))
        await asyncio.to_thread(self.state.gap_repair_observation,status)
        if status['outstanding'] or status['paused_until_ms']>time.time()*1000 or not candidates:return
        from datetime import datetime
        # The least recently asked span first: a span the adapter refuses rotates to the back.
        chat,span,repair=min(candidates,key=lambda c:(c[2].get('asked_at') or 0,c[0]))
        repair={**repair,'asked_at':time.time()}
        await asyncio.to_thread(self.state.save_gap_repair,chat,repair)
        params={'chat':chat,'span_id':repair['span_id'],'before_ts':int(datetime.fromisoformat(span['to']).timestamp()),
            'after_request':repair.get('cursor_request') or ''}
        try:await self.provider.call('gap_repair',params)
        except refused:return
        await asyncio.to_thread(self.state.save_gap_repair,chat,{**repair,'state':'outstanding'})
    async def reconcile_loop(self):
        from .telegram_collector import database_errors
        backoff=DbBackoff(self)
        while True:
            try:await self.sync_once()
            except ProviderNotReady:
                # The streak ends at any answer to a history read, also inside a pass.
                if time.monotonic()-self.provider.not_ready_since>=NOT_READY_WAIT_SECONDS:
                    raise RuntimeError('provider_not_ready') from None
                with suppress(*database_errors()): # Reporting must not end the wait; the next attempt reports again.
                    await asyncio.to_thread(self.state.status,'degraded','provider_temporarily_unavailable')
                    await asyncio.to_thread(self.state.phase,'history','error','provider_temporarily_unavailable')
                await asyncio.sleep(min(self.interval,60)); continue
            except (ProviderOperationError,ProviderUnavailable):
                await asyncio.to_thread(self.state.status,'degraded','provider_temporarily_unavailable')
                await asyncio.to_thread(self.state.phase,'history','error','provider_temporarily_unavailable')
            except database_errors():
                # Checkpoints are committed per chat; retry instead of reconnecting the provider.
                await backoff.wait(); continue
            backoff.reset()
            await asyncio.sleep(self.interval)
    async def drain_loop(self):
        import psycopg
        from psycopg_pool import PoolTimeout
        while True:
            try:
                from .outgoing import reconcile_send_receipt
                await asyncio.to_thread(self.send_receipts.drain,
                    lambda item:reconcile_send_receipt(self.state,item),100)
                await self.drain_captures([self.events_spool],[self.history_spool])
                await self.sweep_tombstones()
            except (psycopg.OperationalError,PoolTimeout): await asyncio.sleep(1)
            await asyncio.sleep(.25)
    async def run(self):
        # Initial policy is read only; actual ownership and enabled check in super.run.
        with self.archive.store.pool.connection() as c:
            policy=c.execute('SELECT * FROM account_policies WHERE account=%s',(self.account,)).fetchone()
        await self.provider.set_policy(policy)
        await super().run()


async def run_config(path):
    cfg=configuration(path)
    session=Path(cfg['session_directory'])
    if not session.is_dir() or session.is_symlink() or session.stat().st_mode&0o077:
        raise RuntimeError('private_session_required')
    require_paired_session(cfg)
    with session_lock(session) as lock_fd:
        if cfg.get('provider')!='whatsmeow':clear_browser_runtime_locks(session)
        store=Store(Path(os.environ['DATABASE_URL_FILE']).read_text().strip(),Path(os.environ['PROBE_FILES']),'http://collector.invalid')
        try:
            with store.pool.connection() as c:
                row=c.execute('SELECT platform,synthetic FROM accounts WHERE id=%s',(cfg['account'],)).fetchone()
                if not row or row['platform']!='whatsapp' or row['synthetic']:raise RuntimeError('live_account_required')
            live_writer=(BlobWriter(store.files,max_bytes=cfg['media_live_max_bytes'],
                reserve_bytes=cfg['media_live_disk_reserve_bytes'])
                if cfg['media_live_disk_reserve_bytes'] is not None else None)
            worker=WhatsAppCollector(Archive(store),cfg['account'],provider_for_config(cfg,path,lock_fd),
                CaptureSpool(cfg['pages_directory'],reserve_bytes=cfg['disk_reserve_bytes']),
                events_spool=CaptureSpool(cfg['events_directory'],reserve_bytes=cfg['disk_reserve_bytes']),
                batch_size=cfg['batch_size'],interval=cfg['scan_interval_seconds'],max_dialogs=cfg['max_dialogs'],
                owner='whatsapp-'+secrets.token_hex(12),
                media_prefetch_days=cfg['media_prefetch_days'],live_writer=live_writer,
                live_window_seconds=cfg['media_live_window_seconds'],
                gap_repair=cfg['whatsapp_gap_repair'],
                writer=BlobWriter(store.files,max_bytes=cfg['max_media_bytes'],reserve_bytes=cfg['media_disk_reserve_bytes']))
            loop=asyncio.get_running_loop()
            for sig in [signal.SIGTERM,signal.SIGINT]: loop.add_signal_handler(sig,worker.stopping.set)
            await worker.run()
        finally:store.close()


def main():
    import argparse
    from .telemetry import configure, capture
    configure('whatsapp')
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',type=Path,required=True)
    args=parser.parse_args();os.umask(0o077)
    try:asyncio.run(run_config(args.config))
    except Exception as exc:
        code,detail,event=collector_stop(exc,'whatsapp')
        capture(exc, event)
        print(json.dumps({'status':'stopped','code':code,**({'detail':detail} if detail else {})}),flush=True)
        raise SystemExit(1) from None


if __name__=='__main__':main()
