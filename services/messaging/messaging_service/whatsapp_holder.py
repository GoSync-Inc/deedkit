"""ANYM-937: the WhatsApp collector attached to its session holder (COLLECTORS.md, Session holders).

The holder (`anymsg-whatsmeow --serve`) owns the session store, the host lock, the session claim and the provider
connection. This collector reaches it over the two private unix sockets of the holder's socket directory and never
spawns a child, takes a host or session lock, or claims the session. The lanes, their request and reply frames and the
send gate are child mode's (whatsapp_collector.NodeProvider); only the transport and the handshake differ."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import time

from psycopg.types.json import Jsonb

from .whatsapp_collector import ProviderOperationError,WhatsAppCollector,WhatsAppState,WhatsmeowProvider

# The collector's protocol range: it supports at least the previous holder protocol (COLLECTORS.md).
COLLECTOR_PROTOCOL=1
COLLECTOR_SUPPORTED=(1,1)
# The policy a keepalive carries lives this long; the holder bounds it to the same 45 s.
POLICY_TTL_SECONDS=45
HANDSHAKE_SECONDS=10
# A protocol mismatch is retried this often (criterion 2); other refusals back off from 5 s to 60 s.
PROTOCOL_RETRY_SECONDS=60
ATTACH_RETRY_SECONDS=(5,60)
FRAME_LIMIT=1024*1024+1
LANE_QUEUE=4
# The policy version is the lease epoch in the high bits: a successor collector's versions are always newer.
POLICY_CHANGE_BITS=20
# ANYM-896 criterion 6: production.py writes it before it stops the holder for a session window.
PLANNED_STOP_FILE='.planned-stop'
DISCONNECT_REASONS=frozenset({'holder_restart','session_window','policy_expired','provider'})
PROTOCOL_REFUSALS=frozenset({'holder_protocol_unsupported','collector_protocol_unsupported'})
# Refusals that pass: the holder is starting, restarting (a new token) or still serving the predecessor.
TRANSIENT_REFUSALS=PROTOCOL_REFUSALS|{'holder_unavailable','holder_busy','holder_not_attached',
    'collector_lease_unverified','holder_token_invalid','holder_peer_refused','holder_hello_invalid'}
HOLDER_STATES=frozenset({'attached','refused','disconnected'})
SOURCE_SHA=re.compile(r'[0-9a-f]{7,40}|unknown')
COMMAND_ROW=re.compile(r'op_[0-9a-f]{32}')
# Their finish writes more than the receipt (commands.py finish_open_chat, finish_open_username, ANYM-477).
RECONCILE_SKIP=frozenset({'open_chat_by_phone','open_chat_by_username','request_chat_history'})


class HolderRefused(RuntimeError):
    """The holder refused the attachment (or cannot be reached) before any method ran."""


class OperationOutcomeUnknown(ProviderOperationError):
    """The holder has no verified outcome for the operation: the command ends unknown and is never retried."""
    def __init__(self):super().__init__('operation_result_unknown')


def negotiate(holder,ours=COLLECTOR_SUPPORTED):
    """The protocol both sides support (the higher common one), or the code of the side outside the other's range."""
    low,high=holder
    if high<ours[0]:return None,'holder_protocol_unsupported'
    if low>ours[1]:return None,'collector_protocol_unsupported'
    return min(high,ours[1]),None


def private_socket_directory(path):
    """The socket directory is a real 0700 directory of this UID; the holder made it so."""
    try:info=os.lstat(path)
    except FileNotFoundError:raise HolderRefused('holder_unavailable') from None
    if not stat.S_ISDIR(info.st_mode) or info.st_mode&0o077 or info.st_uid!=os.getuid():
        raise RuntimeError('holder_socket_directory_invalid')


def read_token(directory):
    """The holder's fresh token: a regular 0600 file of this UID, read without following a link."""
    try:fd=os.open(Path(directory)/'token',os.O_RDONLY|os.O_NOFOLLOW)
    except FileNotFoundError:raise HolderRefused('holder_unavailable') from None
    except OSError:raise RuntimeError('holder_token_invalid') from None
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_uid!=os.getuid() or info.st_size>256:
            raise RuntimeError('holder_token_invalid')
        token=os.read(fd,257).decode('ascii','replace').strip()
    finally:os.close(fd)
    if not re.fullmatch(r'[0-9a-f]{32,128}',token):raise HolderRefused('holder_unavailable')  # written right now
    return token


def planned_stop(directory,attached_at):
    """`(session_window, planned_end)` from a marker written after this attachment began, else holder_restart."""
    try:
        path=Path(directory)/PLANNED_STOP_FILE
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size>1024 or info.st_mtime<attached_at:return 'holder_restart',None
        value=json.loads(path.read_text())
        end=value.get('planned_end') if isinstance(value,dict) and value.get('reason')=='session_window' else None
        if not isinstance(end,str) or len(end)>40:return 'holder_restart',None
        from datetime import datetime
        return 'session_window',datetime.fromisoformat(end).isoformat()
    except (OSError,ValueError,TypeError):return 'holder_restart',None


class Lane:
    """One attached socket. A pump reads its frames into a short queue, so a closed holder is seen while idle and a
    long media answer still has backpressure."""
    def __init__(self,reader,writer):
        self.reader,self.writer=reader,writer
        self.lines=asyncio.Queue(LANE_QUEUE);self.closed=asyncio.Event();self.held=None
        self.task=asyncio.create_task(self._pump())
    async def _pump(self):
        try:
            while True:
                try:line=await self.reader.readline()
                except (ValueError,OSError):line=b''  # over the frame limit, or a reset: the lane is unusable
                if not line or not line.endswith(b'\n'):return
                await self.lines.put(line)
        finally:self.closed.set()
    def unread(self,line):self.held=line
    async def readline(self):
        if self.held is not None:
            line,self.held=self.held,None;return line
        if not self.lines.empty():return self.lines.get_nowait()
        if self.closed.is_set():return b''
        get=asyncio.ensure_future(self.lines.get());end=asyncio.ensure_future(self.closed.wait())
        try:await asyncio.wait({get,end},return_when=asyncio.FIRST_COMPLETED)
        finally:
            end.cancel()
            if not get.done():get.cancel()
        if get.done() and not get.cancelled():return get.result()
        return self.lines.get_nowait() if not self.lines.empty() else b''
    def write(self,frame):
        self.writer.write(json.dumps(frame,separators=(',',':')).encode()+b'\n')
    async def close(self):
        self.writer.close()
        with suppress(OSError,ConnectionError):await self.writer.wait_closed()
        self.task.cancel()
        with suppress(asyncio.CancelledError):await self.task


class Attachment:
    """The attached holder in the place of NodeProvider's child: the control lane is its stdin/stdout, the send
    lane its send socket; it "exits" when either lane closes."""
    def __init__(self,control,send,hello,attached):
        self.control,self.send,self.hello,self.attached=control,send,hello,attached
        self.stdin,self.stdout=control.writer,control
        self.methods=frozenset(hello['methods']);self.attached_at=time.time()
    @property
    def returncode(self):return 0 if self.control.closed.is_set() or self.send.closed.is_set() else None
    async def wait(self):
        waits=[asyncio.ensure_future(lane.closed.wait()) for lane in (self.control,self.send)]
        try:await asyncio.wait(waits,return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:w.cancel()
        return 0
    def abort(self):
        for lane in (self.control,self.send):lane.writer.transport.abort()
    async def close(self):
        await asyncio.gather(self.control.close(),self.send.close(),return_exceptions=True)


def valid_hello(frame,account,lane):
    hello=frame.get('hello') if isinstance(frame,dict) else None
    if not isinstance(hello,dict):return None
    supported,methods=hello.get('supported'),hello.get('methods')
    if (type(hello.get('protocol')) is not int or not isinstance(supported,list) or len(supported)!=2
            or not all(type(v) is int and v>=1 for v in supported) or supported[1]<supported[0]
            or not isinstance(methods,list) or len(methods)>64 or not all(isinstance(m,str) and len(m)<=64 for m in methods)
            or hello.get('lane')!=lane or not isinstance(hello.get('holder_source_sha'),str)):return None
    if hello.get('account')!=account:raise RuntimeError('holder_account_mismatch')
    return hello


async def open_lane(directory,name,lane,token,lease,account):
    """One lane's handshake: the holder's hello, then this collector's hello with the token and the write lease."""
    try:reader,writer=await asyncio.open_unix_connection(str(Path(directory)/name),limit=FRAME_LIMIT)
    except (FileNotFoundError,ConnectionRefusedError):raise HolderRefused('holder_unavailable') from None
    try:
        async with asyncio.timeout(HANDSHAKE_SECONDS):
            frame=json.loads(await reader.readline() or b'null')
            if isinstance(frame,dict) and isinstance(frame.get('error'),str):
                raise HolderRefused(frame['error'] if frame['error'] in TRANSIENT_REFUSALS else 'holder_peer_refused')
            hello=valid_hello(frame,account,lane)
            if hello is None:raise HolderRefused('holder_hello_invalid')
            _,code=negotiate(hello['supported'])
            if code:raise HolderRefused(code)  # nothing sent: no token, no lease, no method
            writer.write(json.dumps({'hello':{'protocol':COLLECTOR_PROTOCOL,'supported':list(COLLECTOR_SUPPORTED)},
                'token':token,'lane':lane,'lease':{'owner':lease[0],'epoch':lease[1]}},separators=(',',':')).encode()+b'\n')
            await writer.drain()
            answer=json.loads(await reader.readline() or b'null')
        if isinstance(answer,dict) and isinstance(answer.get('error'),str):
            if answer['error']=='collector_lease_invalid':raise RuntimeError('collector_lease_lost')
            raise HolderRefused(answer['error'] if answer['error'] in TRANSIENT_REFUSALS else 'holder_peer_refused')
        attached=answer.get('attached') if isinstance(answer,dict) else None
        if not isinstance(attached,dict) or attached.get('lane')!=lane or attached.get('account')!=account:
            raise HolderRefused('holder_hello_invalid')
        return Lane(reader,writer),hello,attached
    except (TimeoutError,ValueError,OSError):
        writer.close();raise HolderRefused('holder_unavailable') from None
    except BaseException:
        writer.close();raise


class HolderState(WhatsAppState):
    def holder_observation(self,**fields):
        """Source status of the attachment: generation, protocol and the last refusal or disconnect; no message data."""
        if fields.get('state') not in HOLDER_STATES:raise ValueError('invalid_holder_observation')
        with self.archive.store.pool.connection() as c:
            row=self._operations(c);value=row['operations']
            holder=dict(value.get('holder') or {})
            if fields['state']=='attached':holder.pop('code',None);holder.pop('reason',None);holder.pop('planned_end',None)
            holder.update({k:v for k,v in fields.items() if v is not None},observed_at=row['observed'].isoformat())
            value['holder']=holder
            self._save_operations(c,value)


class HolderProvider(WhatsmeowProvider):
    """Child mode's lanes over the holder's sockets. A keepalive every heartbeat (15 s) renews the lease on the holder
    and carries the policy with `{version, expires_at}`; one more goes out right before a planned stop."""
    def __init__(self,cfg,config_path):
        super().__init__(cfg,config_path,None)
        self.socket_directory=Path(cfg['holder_socket_directory'])
        self.policy_changes=0;self.operation_key=None;self.refusal=None;self.reason=None
        self.sleep=asyncio.sleep
    def command(self):raise RuntimeError('holder_attached')  # never a child while a holder is configured
    def lease(self):
        state=getattr(self,'state',None)
        if state is None:raise RuntimeError('collector_lease_lost')
        return state.lease
    def policy_version(self):
        return (self.lease()[1]<<POLICY_CHANGE_BITS)|self.policy_changes
    async def observe(self,**fields):
        if hasattr(self,'state'):
            from .telegram_collector import database_errors
            with suppress(*database_errors()):await asyncio.to_thread(self.state.holder_observation,**fields)
    async def attach(self):
        private_socket_directory(self.socket_directory)
        token=read_token(self.socket_directory);lease=self.lease();account=self.cfg['account']
        control,hello,attached=await open_lane(self.socket_directory,'control.sock','control',token,lease,account)
        try:send,_,_=await open_lane(self.socket_directory,'send.sock','send',token,lease,account)
        except BaseException:
            await control.close();raise
        if not {'start','send_text','messenger_operation'}<=set(hello['methods']):
            await asyncio.gather(control.close(),send.close());raise HolderRefused('holder_protocol_unsupported')
        return Attachment(control,send,hello,attached)
    async def connect(self):
        if self.process is not None and self.process.returncode is None:raise RuntimeError('provider_already_connected')
        delay=ATTACH_RETRY_SECONDS[0]
        while True:
            try:self.process=await self.attach();break
            except HolderRefused as exc:
                code=str(exc)
                if code!=self.refusal:  # reported once per refusal, then retried (criterion 2)
                    self.refusal=code;await self.observe(state='refused',code=code)
                await self.sleep(PROTOCOL_RETRY_SECONDS if code in PROTOCOL_REFUSALS else delay)
                delay=min(ATTACH_RETRY_SECONDS[1],delay*2)
        self.refusal=None;self.reason=None
        try:
            self.send_reader,self.send_writer=self.process.send,self.process.send.writer
            await self.keepalive()  # the holder connects only under a fresh policy
            result=await self.call('start',{})
            generation=result.get('generation') if isinstance(result,dict) else None
            if type(generation) is not int or generation<1:raise RuntimeError('provider_protocol_error')
            self.started=True
            hello=self.process.hello;sha=hello['holder_source_sha']
            if hasattr(self,'state'):await asyncio.to_thread(self.state.provider_observation,'whatsmeow')
            await self.observe(state='attached',generation=generation,protocol=negotiate(hello['supported'])[0],
                holder_source_sha=sha if SOURCE_SHA.fullmatch(sha) else 'unknown')
        except BaseException:
            await self.disconnect();raise
    def self_provider_id(self):
        running=self.started and self.process is not None and self.process.returncode is None
        return self.cfg.get('expected_wid') if running else None
    async def keepalive(self):
        att=self.process
        if att is None or att.returncode is not None or self.policy is None:return
        try:
            att.control.write({'keepalive':{'lease':{'owner':self.lease()[0],'epoch':self.lease()[1]},
                'policy':{**self.policy,'version':self.policy_version(),'expires_at':time.time()+POLICY_TTL_SECONDS}}})
            await att.control.writer.drain()
        except (OSError,ConnectionError):att.abort()  # the lanes end; wait_disconnected reports why
    async def set_policy(self,policy):
        selected={k:list(policy[k]) if k!='discover_chats' else policy[k] for k in ('discover_chats','include_chats','exclude_chats')}
        selected.update(enabled=policy.get('enabled',True),send_enabled=policy.get('send_enabled',False),
            operations_enabled=list(policy.get('operations_enabled') or []))
        if selected!=self.policy:
            self.policy=selected;self.policy_changes=min(self.policy_changes+1,(1<<POLICY_CHANGE_BITS)-1)
        await self.keepalive()
    async def _disconnect(self):
        self.started=False
        att=self.process
        if att is None:return
        if att.returncode is None:
            # Right before a planned stop: a fresh policy keeps the connection over a restart inside 45 s.
            with suppress(Exception):await asyncio.wait_for(self.keepalive(),2)
        await att.close()
        self.send_writer=None;self.send_reader=None
    def failure_recorded(self):return False  # the holder owns .transport-failure; its replies carry the code
    async def child_stop(self):
        reason,end=self.reason,None
        if reason is None:
            reason,end=planned_stop(self.socket_directory,self.process.attached_at if self.process else time.time())
        self.reason=reason
        await self.observe(state='disconnected',code='provider_disconnected',reason=reason,planned_end=end)
        stop=RuntimeError('provider_disconnected');stop.detail=reason
        return stop
    async def request(self,method,params,*,sending=False):
        att=self.process
        if att is not None and att.returncode is None and method not in att.methods:
            from .commands import CommandRejected
            raise CommandRejected('operation_unsupported')  # a method the hello did not list is never called
        return await super().request(method,params,sending=sending)
    async def reply(self,request,timeout=90,*,sending=False):
        reader=self.send_reader if sending else (self.process.stdout if self.process else None)
        if reader is None:raise RuntimeError('provider_disconnected')
        line=await asyncio.wait_for(reader.readline(),timeout)
        code=None
        with suppress(ValueError,AttributeError):
            frame=json.loads(line) if b'"error"' in line[:4096] else None
            code=frame.get('error') if isinstance(frame,dict) and frame.get('id')==request else None
        if code=='operation_outcome_unknown':raise OperationOutcomeUnknown()
        if code=='collector_lease_invalid':raise RuntimeError('collector_lease_lost')
        if code in ('policy_expired','provider_disconnected'):
            self.reason='policy_expired' if code=='policy_expired' else 'provider'
            raise await self.child_stop()
        reader.unread(line)
        return await super().reply(request,timeout,sending=sending)
    async def execute_operation(self,job):
        # The journal key (criterion 5): the globally unique command row ID, one step per provider call of the job.
        if not COMMAND_ROW.fullmatch(str(job.get('id',''))):
            from .commands import CommandRejected
            raise CommandRejected('operation_invalid_parameters')
        self.operation_key=[job['id'],0]
        try:return await super().execute_operation(job)
        finally:self.operation_key=None
    async def _call_unlocked(self,method,params,*,sending=False):
        if method=='messenger_operation':
            if self.operation_key is None:raise RuntimeError('send_gate_required')
            params={**params,'command_row_id':self.operation_key[0],'step':self.operation_key[1]}
            self.operation_key[1]+=1
        return await super()._call_unlocked(method,params,sending=sending)


def journal_outcome(directory,command_id):
    """The holder's verified, confirmed outcome of a one-step operation, or None (pending, refused or absent)."""
    from .commands import safe_result
    from .telegram_collector import private_json
    if not COMMAND_ROW.fullmatch(command_id):return None
    journal=Path(directory)/'operation-outcomes'
    try:records=sorted(p.name for p in journal.glob(command_id+'.*.json'))
    except OSError:return None
    if records!=[command_id+'.0.json']:return None
    try:record=private_json(journal/records[0])
    except (OSError,ValueError):return None
    if (not isinstance(record,dict) or record.get('command_row_id')!=command_id or record.get('pending') is not False
            or record.get('error') or not isinstance(record.get('result'),dict)):return None
    try:result=safe_result(record['result'])
    except ValueError:return None
    return result if result['outcome']=='confirmed' else None


def reconcile_operation_outcomes(state,directory):
    """After a collector restart: a predecessor's dispatching command whose verified outcome the holder journaled
    gets that outcome; everything else is left to the claim's sweep, which ends it unknown (never retried)."""
    settled=0
    with state.archive.store.pool.connection() as c:
        state.archive.fence(c,state.account,state.lease)
        rows=c.execute("""SELECT id,operation,lease_epoch FROM messenger_commands WHERE account=%s AND status='dispatching'
            AND lease_epoch IS DISTINCT FROM %s FOR UPDATE SKIP LOCKED""",(state.account,state.lease[1])).fetchall()
        for row in rows:
            if row['operation'] in RECONCILE_SKIP:continue
            outcome=journal_outcome(directory,row['id'])
            if outcome is None:continue
            settled+=c.execute("""UPDATE messenger_commands SET status='succeeded',result=%s,error_code=NULL,
                finished_at=clock_timestamp() WHERE id=%s AND status='dispatching' AND lease_epoch=%s""",
                (Jsonb(outcome),row['id'],row['lease_epoch'])).rowcount
    return settled


class HolderCollector(WhatsAppCollector):
    state_class=HolderState
    async def outgoing_loop(self):
        from .telegram_collector import database_errors
        events=self.provider.cfg['events_directory']
        # Best effort: on a database fault the claim's sweep still ends those commands unknown, never retried.
        with suppress(*database_errors()):await asyncio.to_thread(reconcile_operation_outcomes,self.state,events)
        await super().outgoing_loop()


async def run_attached(cfg,path,build):
    """run_config in holder mode: no session directory, lock, claim or child; the holder owns all of them."""
    from .store import Store
    from . import session_claim
    def open_store():
        return Store(Path(os.environ['DATABASE_URL_FILE']).read_text().strip(),Path(os.environ['PROBE_FILES']),'http://collector.invalid')
    def check(store):
        with store.pool.connection() as c:
            row=c.execute('SELECT platform,synthetic FROM accounts WHERE id=%s',(cfg['account'],)).fetchone()
            if not row or row['platform']!='whatsapp' or row['synthetic']:raise RuntimeError('live_account_required')
    store=await session_claim.open_waiting(open_store,check,cfg['account'])
    try:
        worker=build(cfg,store,HolderProvider(cfg,path),HolderCollector,'whatsapp-'+secrets.token_hex(12))
        loop=asyncio.get_running_loop()
        for sig in [signal.SIGTERM,signal.SIGINT]:loop.add_signal_handler(sig,worker.stopping.set)
        await worker.run()
    finally:store.close()
