"""Durable provider captures and reconciliation checkpoints, independent of clients."""
from contextlib import contextmanager
from datetime import datetime,timezone
import fcntl
import os
from pathlib import Path
import re
import stat
from psycopg.errors import CheckViolation
from psycopg.types.json import Jsonb

from .operational import CAPTURE_STOP_DETAILS,OVERLOAD_STOP_DETAILS,CONNECTION_ERRORS,TERMINAL,PHASE_STATES,PHASE_REASONS
from .operational import identity_safe,self_provider_id as verified_self_id

from .envelope import canonical,utc,validate,stable_id,MAX_ENVELOPE
from .spool import DurableSpool


GAP_REASONS=('app_state_sync_error','provider_disconnected','provider_reconnected','read_state_write_failed')


def validate_capture(value):
    if not isinstance(value,dict) or len(canonical(value))>MAX_ENVELOPE: raise ValueError('invalid_capture')
    if value.get('kind')=='owner_read_state':
        from .reply_state import validate_observation
        if (set(value)!={'kind','account','chat','observed_at','observation'}
            or not isinstance(value['account'],str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',value['account'])
            or not isinstance(value['chat'],str)):
            raise ValueError('invalid_read_capture')
        from .reply_state import WHATSAPP_READ_BASES
        observation=validate_observation(value['observation'])
        # Telegram inbox reads and the passive WhatsApp read sources (ANYM-275), never a cache guess.
        whatsapp=observation['basis'] in WHATSAPP_READ_BASES
        if (observation['basis']!='telegram_dialog' and not whatsapp
                or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,140}@(c.us|g.us|lid)' if whatsapp else r'-?[0-9]{1,20}',value['chat'])
                or utc(value['observed_at'])!=observation['observed_at']):
            raise ValueError('invalid_read_capture')
        return {**value,'observed_at':observation['observed_at'],'observation':observation}
    if value.get('kind')=='read_state_gap':
        # A provider update gap (whatsmeow app-state sync error): read events may be missing.
        if (set(value)!={'kind','account','observed_at','reason'} or value['reason'] not in GAP_REASONS
                or not isinstance(value['account'],str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',value['account'])):
            raise ValueError('invalid_capture')
        return {**value,'observed_at':utc(value['observed_at'])}
    if value.get('kind')=='message_receipt':
        from .receipts import validate_capture as validate_receipt
        return validate_receipt(value)
    if value.get('kind')=='message':
        if not {'kind','peer_kind','envelope'}<=set(value) or set(value)-{'kind','peer_kind','envelope','captured_at','identity_observations'} or value.get('peer_kind') not in ('user','chat','channel'):
            raise ValueError('invalid_capture')
        envelope=validate(value['envelope'])
        from .whatsapp_identity import observations
        identity=observations(value['identity_observations'],envelope) if 'identity_observations' in value else None
        return {**value,'envelope':envelope,**({'captured_at':utc(value['captured_at'])} if 'captured_at' in value else {}),
            **({'identity_observations':identity} if identity is not None else {})}
    if value.get('kind')=='whatsapp_event':
        from .adapters import whatsapp_message
        if set(value)!={'kind','account','event','change','observed_at','origin'}: raise ValueError('invalid_capture')
        event=value['event']
        if not isinstance(event,dict) or set(event)-{'chatId','chatName','messageId','senderId','body',
                'timestamp','timestampResolution','durationSeconds','fromMe','hasMedia','filename','mimetype','filesize','editedTimestampMs',
                'senderName','senderUsername','replyToId','forwarded','forwardOrigin','mediaKind','mentionsOwner','replyToOwner','identityObservations','formatting','messageType','isService','isSelfChat','senderIsContact','senderIsBusiness','contactCard'}:
            raise ValueError('invalid_whatsapp_event')
        envelope=whatsapp_message(value['account'],event,change=value['change'],
            observed_at=utc(value['observed_at']),origin=value['origin'])
        kind='chat' if envelope['chat'].endswith('@g.us') else 'user'
        from .whatsapp_identity import observations
        identity=observations(event['identityObservations'],envelope) if 'identityObservations' in event else None
        return {'kind':'message','peer_kind':kind,'envelope':envelope,'captured_at':utc(value['observed_at']),
            **({'identity_observations':identity} if identity is not None else {})}
    if value.get('kind') in ('telegram_delete','whatsapp_delete'):
        if set(value)!={'kind','account','chat','ids','observed_at'}: raise ValueError('invalid_capture')
        if not isinstance(value['account'],str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',value['account']):
            raise ValueError('invalid_capture_account')
        whatsapp=value['kind']=='whatsapp_delete'
        pattern=r'[a-zA-Z0-9_.:-]{1,140}@(c.us|g.us|lid)' if whatsapp else r'-?[0-9]{1,20}|'
        if not isinstance(value['chat'],str) or not re.fullmatch(pattern,value['chat']):
            raise ValueError('invalid_capture_chat')
        ids=value['ids']
        valid_id=lambda i: (isinstance(i,str) and 0<len(i)<=256 and '\x00' not in i) if whatsapp else (type(i) is int and 0<i<2**63)
        if not isinstance(ids,list) or not 1<=len(ids)<=1000 or any(not valid_id(i) for i in ids):
            raise ValueError('invalid_deleted_ids')
        return {**value,'ids':sorted(set(ids)),'observed_at':utc(value['observed_at'])}
    raise ValueError('invalid_capture_kind')


MAX_REJECTED_MESSAGES=20
# Per-chat history scan errors (ANYM-293): an unavailable peer, a rate limit, a transient server
# error after its retries, or any other provider error of that chat. The last two are stored as
# provider_unavailable, which every deployed reader knows; history.chat_errors keeps the exact one.
CHAT_SCAN_ERRORS=('provider_unavailable','rate_limited','provider_temporarily_unavailable','provider_chat_error')
MAX_CHAT_ERRORS=20


def validate_chat_error(evidence):
    """Only a class name and a fixed Telegram RPC code; never exception text."""
    if (not isinstance(evidence,dict) or set(evidence)-{'error_class','rpc_message'}
            or type(evidence.get('error_class')) is not str
            or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',evidence['error_class'])
            or 'rpc_message' in evidence and (type(evidence['rpc_message']) is not str
                or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',evidence['rpc_message']))):
        raise ValueError('invalid_chat_error')
    return {k:evidence[k] for k in ('error_class','rpc_message') if k in evidence}
# Reason codes a skipped live capture may report: the capture validators' own
# codes plus the collector's parser codes. Anything else is `invalid_capture`.
LIVE_REJECT_CODES=frozenset({'invalid_capture','invalid_capture_kind','invalid_capture_account','invalid_capture_chat',
    'invalid_deleted','invalid_deleted_ids','invalid_envelope','envelope_too_large','unknown_envelope_fields',
    'invalid_account','invalid_chat_type','invalid_peer_kind','invalid_timestamp','invalid_metadata','invalid_media',
    'invalid_media_size','invalid_mime','duplicate_media_id','invalid_sender_profile','invalid_identity_observation',
    'invalid_identity_observations','identity_source_mismatch','invalid_read_capture','invalid_read_observation',
    'invalid_receipt','invalid_receipt_account','invalid_receipt_chat','invalid_receipt_platform',
    'invalid_receipt_reference','unsupported_receipt_provider_time','invalid_whatsapp_event',
    'invalid_provider_message','receipt_parse_failed','read_parse_failed'})
# Stop codes of live capture itself; the fixed causes of a fail-closed write are CAPTURE_STOP_DETAILS.
CAPTURE_STOPS={'durable_capture_failed','capture_overload'}


def validate_rejected_message(value):
    """Identity of a skipped live message, never its content, for a later re-fetch."""
    if (not isinstance(value,dict) or set(value)!={'chat','message_id','reason_code','observed_at'}
            or not isinstance(value['chat'],str) or not re.fullmatch(r'-?[0-9]{1,20}',value['chat'])
            or type(value['message_id']) is not int or not 0<value['message_id']<2**63
            or value['reason_code'] not in LIVE_REJECT_CODES):
        raise ValueError('invalid_capture_progress')
    return {**value,'observed_at':utc(value['observed_at'])}


class CaptureSpool(DurableSpool):
    def __init__(self,path,**kwargs): super().__init__(path,validator=validate_capture,**kwargs)


def trusted(info):
    return info.st_uid in {0,os.geteuid()}


def trusted_real_path(path):
    """Resolve path, refusing any component another non-root user could change.

    As OpenSSH StrictModes: each directory used, including those that resolve a
    symlink (like macOS /tmp), is owned by root or this uid and not group/other
    writable unless sticky; each symlink is owned by root or this uid. Returns the
    real path and the missing tail."""
    real=Path('/'); pending=list(Path(os.getcwd(),path).parts[1:]); links=0
    info=os.lstat(real)
    if not trusted(info) or info.st_mode&0o022: raise RuntimeError('private_session_required')
    while pending:
        name=pending.pop(0)
        if name=='..': real=real.parent; continue  # real holds no symlink, so this is the kernel's parent.
        candidate=real/name
        try: info=os.lstat(candidate)
        except FileNotFoundError: return real,[name,*pending]
        if not trusted(info): raise RuntimeError('private_session_required')
        if stat.S_ISLNK(info.st_mode):
            links+=1
            if links>32: raise RuntimeError('private_session_required')
            target=Path(os.readlink(candidate))
            if target.is_absolute(): real=Path('/')
            pending=[*target.parts[1 if target.is_absolute() else 0:],*pending]; continue
        # A sticky directory protects the entries it does not own.
        if not stat.S_ISDIR(info.st_mode) or info.st_mode&0o022 and not info.st_mode&stat.S_ISVTX:
            raise RuntimeError('private_session_required')
        real=candidate
    return real,[]


def private_session_directory(session):
    """Open the directory that holds a session, its SQLite side files and its lock.

    A file's own 0600 mode does not stop another user from replacing it, so the
    directory must be private, and no other non-root user may change the path to
    it, so the provider's later access by name reaches the checked directory.
    Never repaired."""
    session=Path(session)
    try:
        if session.is_symlink() or session.parent.is_symlink(): raise ValueError('session_symlink')
        real,missing=trusted_real_path(session.parent)
        if '..' in missing: raise RuntimeError('private_session_required')
        if missing:
            real.joinpath(*missing).mkdir(mode=0o700,parents=True,exist_ok=True)
            # Another user may create a missing name first in a sticky directory.
            real,missing=trusted_real_path(session.parent)
            if missing: raise RuntimeError('private_session_required')
        fd=os.open(real,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    except OSError: raise RuntimeError('private_session_required') from None
    info=os.fstat(fd)
    if info.st_mode&0o077 or not trusted(info):
        os.close(fd); raise RuntimeError('private_session_required')
    return fd


@contextmanager
def session_lock(session):
    """Hold for the whole provider lifetime, including authorization. Never unlink lock."""
    session=Path(session)
    directory=private_session_directory(session)
    try: fd=os.open(session.name+'.owner.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=directory)
    finally: os.close(directory)
    try:
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('session_already_owned') from None
        yield fd
    finally: os.close(fd)


class CollectorState:
    def __init__(self,archive,account,lease): self.archive,self.account,self.lease=archive,account,lease

    def _operations(self,c):
        self.archive.fence(c,self.account,self.lease)
        c.execute('INSERT INTO collector_health(account) VALUES(%s) ON CONFLICT DO NOTHING',(self.account,))
        return c.execute('SELECT '+identity_safe('operations')+' AS operations,clock_timestamp() AS observed '
            'FROM collector_health WHERE account=%s FOR UPDATE',(self.account,)).fetchone()

    def _save_operations(self,c,value):
        if 'identity' in value:
            # The optional verified ID never costs a phase write its 16 KiB budget
            # (018): over the limit the row is saved without it (ANYM-470).
            try:
                with c.transaction():
                    c.execute('UPDATE collector_health SET operations=%s,updated_at=clock_timestamp() WHERE account=%s',
                        (Jsonb(value),self.account))
                return
            except CheckViolation:value.pop('identity')
        c.execute('UPDATE collector_health SET operations=%s,updated_at=clock_timestamp() WHERE account=%s',
            (Jsonb(value),self.account))

    # Receiving period (ANYM-275): continuity evidence that keeps an older read observation true.
    # It starts when the provider connection is ready under this lease epoch and ends on any
    # disconnect or stop; a takeover or an expired lease is visible through collector_leases.
    def _start_period(self,c):
        c.execute('''UPDATE collector_health SET receiving_epoch=%s,receiving_since=clock_timestamp(),
            receiving_heartbeat_at=clock_timestamp(),receiving_backlog_since=NULL,receiving_gap_at=NULL
            WHERE account=%s''',(self.lease[1],self.account))

    def _end_period(self,c):
        c.execute('''UPDATE collector_health SET receiving_since=NULL,receiving_heartbeat_at=NULL,
            receiving_backlog_since=NULL,receiving_gap_at=NULL WHERE account=%s''',(self.account,))

    def update_gap(self,observed_at=None):
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            c.execute('''UPDATE collector_health SET receiving_gap_at=clock_timestamp()
                WHERE account=%s AND receiving_epoch=%s AND receiving_since IS NOT NULL''',(self.account,self.lease[1]))

    @staticmethod
    def _phase(value,name,state,code,observed,action=None,**extra):
        previous=value.get(name,{})
        changed=(previous.get('state'),previous.get('reason_code'),previous.get('next_action'))!=(state,code,action)
        value[name]={**previous,'state':state,'reason_code':code,'next_action':action,
            'changed_at':observed.isoformat() if changed else previous.get('changed_at'),
            'observed_at':observed.isoformat(),**extra}

    def phase(self,name,state,code,*,self_provider_id=None):
        if state not in PHASE_STATES.get(name,set()) or code not in PHASE_REASONS:
            raise ValueError('invalid_operational_state')
        with self.archive.store.pool.connection() as c:
            row=self._operations(c);value=row['operations']
            connected=name=='connection' and state=='connected'
            extra={'retry_at':None,'retry_attempts':0} if connected else {}
            self._phase(value,name,state,code,row['observed'],**extra)
            if connected:self._start_period(c)
            elif name in ('connection','live_capture') and state!='receiving':self._end_period(c)
            if connected and self_provider_id is not None:
                # Only the ID the provider just verified against the configured one; a
                # malformed value is dropped, never a reason to fail the connect (ANYM-470).
                platform=c.execute('SELECT platform FROM accounts WHERE id=%s',(self.account,)).fetchone()
                verified=verified_self_id(platform and platform['platform'],self_provider_id)
                if verified:value['identity']={'self_provider_id':verified,'verified_at':row['observed'].isoformat()}
            self._save_operations(c,value)

    def media_policy(self,prefetch_days,history_since):
        """Publish the window and history bound this run claims originals with (ANYM-284)."""
        from .media_policy import validate_prefetch_days
        validate_prefetch_days(prefetch_days)
        if history_since is not None and (not isinstance(history_since,datetime) or history_since.tzinfo is None):
            raise ValueError('invalid_history_since')
        with self.archive.store.pool.connection() as c:
            row=self._operations(c);value=row['operations']
            value['media_policy']={'prefetch_days':prefetch_days,
                'history_since':history_since.isoformat() if history_since else None,
                'epoch':self.lease[1],'published_at':row['observed'].isoformat()}
            self._save_operations(c,value)

    def disk_floors(self,floors):
        """Publish this run's disk floors per volume; cold restores keep above them (ANYM-350)."""
        if (type(floors) is not dict or not 1<=len(floors)<=8 or
                any(type(k) is not str or not re.fullmatch(r'fs:[0-9]{1,20}',k) or
                    type(v) is not int or not 0<=v<=100*1024**3 for k,v in floors.items())):
            raise ValueError('invalid_disk_floors')
        with self.archive.store.pool.connection() as c:
            row=self._operations(c);value=row['operations']
            value['disk_floors']={'volumes':floors,'epoch':self.lease[1],'published_at':row['observed'].isoformat()}
            self._save_operations(c,value)

    def connection_failed(self,code,detail=None):
        if code not in CONNECTION_ERRORS:code='provider_disconnected'
        # Exactly a built-in str of the fixed vocabulary; a str subclass could fake equality.
        if not (type(detail) is str and (code=='durable_capture_failed' and detail in CAPTURE_STOP_DETAILS
                or code=='capture_overload' and detail in OVERLOAD_STOP_DETAILS)):detail=None
        with self.archive.store.pool.connection() as c:
            row=self._operations(c);value=row['operations'];now=row['observed']
            attempts=min(1024,value.get('connection',{}).get('retry_attempts',0)+1)
            delay=min(300,5*2**min(attempts-1,6))
            from datetime import timedelta
            self._phase(value,'connection','blocked' if code in TERMINAL else 'retry_wait',code,now,
                TERMINAL.get(code,'wait_for_retry'),retry_attempts=attempts,
                retry_at=None if code in TERMINAL else (now+timedelta(seconds=delay)).isoformat())
            # A capture stop keeps its content-free cause after the next run resets the phase.
            stop={'last_capture_stop':{'code':code,'detail':detail,'at':now.isoformat()}} if code in CAPTURE_STOPS else {}
            self._phase(value,'live_capture','paused',code,now,TERMINAL.get(code,'wait_for_retry'),**stop)
            self._end_period(c)
            self._save_operations(c,value)

    def connection_gate(self):
        with self.archive.store.pool.connection() as c:
            row=self._operations(c);phase=row['operations'].get('connection',{})
            deadline=phase.get('retry_at')
            remaining=max(0,(datetime.fromisoformat(deadline)-row['observed']).total_seconds()) if deadline else 0
            return {'blocked':phase.get('state')=='blocked','remaining_seconds':remaining}

    def capture_progress(self,last,count,pending,*,quarantined=None,invalid=0,history_pending=None,
                         rejected=None,rejected_messages=()):
        if type(count) is not int or count<0 or type(pending) is not int or not 0<=pending<=20000:
            raise ValueError('invalid_capture_progress')
        if (quarantined is not None and (type(quarantined) is not int or not 0<=quarantined<=20000)
                or type(invalid) is not int or invalid<0
                or history_pending is not None and (type(history_pending) is not int or not 0<=history_pending<=20000)):
            raise ValueError('invalid_capture_progress')
        rejected=rejected or {}
        if not isinstance(rejected,dict) or any(k not in LIVE_REJECT_CODES or type(n) is not int or n<1
                for k,n in rejected.items()):
            raise ValueError('invalid_capture_progress')
        rejected_messages=[validate_rejected_message(m) for m in rejected_messages]
        capture=validate_capture(last) if last is not None else None
        with self.archive.store.pool.connection() as c:
            row=self._operations(c);value=row['operations'];now=row['observed']
            previous=value.get('live_capture',{});extra={'pending_events':pending,
                'committed_captures':previous.get('committed_captures',0)+count}
            if quarantined is not None:extra['quarantined_captures']=quarantined
            if invalid or 'invalid_provider_messages' in previous:
                extra['invalid_provider_messages']=previous.get('invalid_provider_messages',0)+invalid
            if rejected:
                counts=dict(previous.get('rejected_live_captures',{}))
                for code,number in rejected.items():counts[code]=counts.get(code,0)+number
                extra['rejected_live_captures']=counts
            if rejected_messages:
                extra['rejected_live_messages']=[*previous.get('rejected_live_messages',[]),*rejected_messages][-MAX_REJECTED_MESSAGES:]
            if capture is not None:
                envelope=capture.get('envelope')
                captured=capture.get('captured_at') if envelope else capture['observed_at']
                observed=datetime.fromisoformat(utc(captured)) if captured else None
                sent=datetime.fromisoformat(utc(envelope['sent_at'])) if envelope else None
                extra['last_commit']={'captured_at':observed.isoformat() if observed else None,'committed_by':now.isoformat(),
                    'capture_to_commit_seconds':round(max(0,(now-observed).total_seconds()),3) if observed else None,
                    'provider_age_at_capture_seconds':round((observed-sent).total_seconds(),3) if sent and observed else None,
                    'origin':envelope['origin'] if envelope else
                        'receipt' if capture['kind']=='message_receipt' else 'owner_read_state' if capture['kind']=='owner_read_state' else 'deletion',
                    'mcp_available_after_commit':True,'measurement':'upper_bound_after_archive_commit' if observed else 'capture_time_not_recorded'}
            held=extra.get('quarantined_captures',previous.get('quarantined_captures',0))
            # Quarantined items no longer block capture, but they need an operator decision.
            self._phase(value,'live_capture','backlogged' if pending else 'receiving',
                'capture_draining' if pending else 'capture_quarantined' if held else 'capture_ready',now,
                'inspect_capture_quarantine' if held and not pending else None,**extra)
            if history_pending is not None:
                value['history']={**value.get('history',{}),'pending_events':history_pending}
            # Drained live captures are the heartbeat; an undrained spool may hold a delayed read event,
            # and a newly quarantined or invalid capture may have been one: both end continuity.
            lost=bool(invalid) or held>previous.get('quarantined_captures',0)
            c.execute('''UPDATE collector_health SET receiving_heartbeat_at=clock_timestamp(),
                receiving_backlog_since=CASE WHEN %s THEN coalesce(receiving_backlog_since,clock_timestamp()) END,
                receiving_gap_at=CASE WHEN %s THEN clock_timestamp() ELSE receiving_gap_at END
                WHERE account=%s AND receiving_epoch=%s AND receiving_since IS NOT NULL''',(pending>0,lost,self.account,self.lease[1]))
            self._save_operations(c,value)

    def policy(self):
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            row=c.execute('SELECT * FROM account_policies WHERE account=%s AND enabled',(self.account,)).fetchone()
            if not row: raise RuntimeError('collection_disabled')
            return row

    @staticmethod
    def allows(policy,chat):
        return chat not in policy['exclude_chats'] and (policy['discover_chats'] or chat in policy['include_chats'])

    def chat(self,provider_chat,peer_kind):
        if peer_kind not in ('user','chat','channel'): raise ValueError('invalid_peer_kind')
        if not self.allows(self.policy(),provider_chat): raise ValueError('chat_excluded')
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            c.execute('''INSERT INTO collector_chats(account,provider_chat,peer_kind) VALUES(%s,%s,%s)
                ON CONFLICT DO NOTHING''',(self.account,provider_chat,peer_kind))
            row=c.execute('SELECT * FROM collector_chats WHERE account=%s AND provider_chat=%s',
                          (self.account,provider_chat)).fetchone()
            if row['peer_kind']!=peer_kind: raise RuntimeError('peer_kind_conflict')
            return row

    def inventory(self,dialogs,history_since,*,discovery=False):
        """Persist the catalog before any history fetch. Fence bounded batches."""
        for offset in range(0,len(dialogs),100):
            with self.archive.store.pool.connection() as c:
                self.archive.fence(c,self.account,self.lease)
                policy=c.execute('SELECT * FROM account_policies WHERE account=%s AND enabled',
                                 (self.account,)).fetchone()
                if not policy: raise RuntimeError('collection_disabled')
                from .chat_roles import CHAT_ROLE_SCHEMA,valid
                from .store import schema_version
                roles=discovery and schema_version(c)>=CHAT_ROLE_SCHEMA
                stored=0
                for dialog in dialogs[offset:offset+100]:
                    chat,kind,title=dialog[:3]
                    if not self.allows(policy,chat): continue
                    if kind not in ('user','chat','channel'): raise ValueError('invalid_peer_kind')
                    chat_type=dialog[3] if len(dialog)>=4 else {'user':'private','chat':'group','channel':'unknown'}[kind]
                    if chat_type not in ('private','group','channel','unknown'):raise ValueError('invalid_chat_type')
                    c.execute('''INSERT INTO collector_chats(account,provider_chat,peer_kind,history_since)
                        VALUES(%s,%s,%s,%s) ON CONFLICT(account,provider_chat) DO UPDATE SET
                        window_complete=CASE WHEN collector_chats.history_since IS DISTINCT FROM
                            excluded.history_since THEN false ELSE collector_chats.window_complete END,
                        history_since=excluded.history_since''',(self.account,chat,kind,history_since))
                    c.execute('''INSERT INTO chats(id,account,title,chat_type,provider_chat) VALUES(%s,%s,%s,%s,%s)
                        ON CONFLICT(id) DO UPDATE SET
                        title=CASE WHEN excluded.title<>%s AND excluded.title<>'' THEN excluded.title ELSE chats.title END,
                        chat_type=CASE WHEN excluded.chat_type='unknown' THEN chats.chat_type ELSE excluded.chat_type END,
                        provider_chat=excluded.provider_chat''',
                        (stable_id('chat',self.account,chat),self.account,title or chat,
                         chat_type,chat,chat))
                    if len(dialog)>=5 and dialog[4] is not None:
                        from .reply_state import observe
                        observe(c,self.account,chat,dialog[4])
                    if discovery and roles and len(dialog)>=6 and dialog[5] is not None:
                        # Only a discovery pass observes the role; a cached replay keeps role_observed_at (ANYM-533).
                        role=valid(dialog[5])
                        c.execute('''UPDATE chats SET my_role=%s,can_post=%s,username=%s,role_observed_at=clock_timestamp()
                            WHERE id=%s''',(role['my_role'],role['can_post'],role['username'],stable_id('chat',self.account,chat)))
                    stored+=1
                if discovery:
                    c.execute('''UPDATE collector_health SET discovery_count=discovery_count+%s,
                        updated_at=clock_timestamp() WHERE account=%s''',(stored,self.account))
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            return [r['provider_chat'] for r in c.execute('''SELECT provider_chat FROM collector_chats
                WHERE account=%s ORDER BY last_attempt_at NULLS FIRST,provider_chat''',(self.account,))]

    def catalog(self,provider_chat,peer_kind,title,chat_type,history_since):
        """ANYM-476: a chat first seen in a live search hit enters the catalog as a dialog would; a known one is left as is."""
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            if c.execute('SELECT 1 FROM collector_chats WHERE account=%s AND provider_chat=%s',
                         (self.account,provider_chat)).fetchone(): return
        self.inventory([(provider_chat,peer_kind,title,chat_type or 'unknown')],history_since)

    def attempted(self,chat):
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            c.execute('UPDATE collector_chats SET last_attempt_at=clock_timestamp() WHERE account=%s AND provider_chat=%s',
                      (self.account,chat))

    def failed(self,chat,code,evidence=None):
        if code not in CHAT_SCAN_ERRORS:raise ValueError('invalid_scan_error')
        if evidence is not None:evidence=validate_chat_error(evidence)
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            c.execute('UPDATE collector_chats SET last_error_code=%s,last_error_at=clock_timestamp() '
                'WHERE account=%s AND provider_chat=%s',
                (code if code in ('provider_unavailable','rate_limited') else 'provider_unavailable',self.account,chat))
            if evidence is not None:
                # The class of a chat's provider error outlives the container's logs (ANYM-293).
                row=self._operations(c);value=row['operations'];history=dict(value.get('history') or {})
                entry={'chat':chat,'reason_code':code,**evidence,'at':row['observed'].isoformat()}
                history['chat_errors']=[*[e for e in history.get('chat_errors',[]) if e.get('chat')!=chat],
                    entry][-MAX_CHAT_ERRORS:]
                value['history']=history;self._save_operations(c,value)

    def checkpoint(self,chat,*,high_water=None,history_before=None,exhausted=False,initialized=False,
                   window_complete=False,head_complete=False):
        # Caller advances only after all captures in a provider page committed.
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            c.execute('''UPDATE collector_chats SET high_water=greatest(high_water,coalesce(%s,0)),
                history_before=CASE WHEN %s::bigint IS NULL THEN history_before
                    ELSE least(coalesce(history_before,%s),%s) END,
                history_exhausted=history_exhausted OR %s,initialized=initialized OR %s,
                window_complete=window_complete OR %s,
                last_head_scan_at=CASE WHEN %s THEN clock_timestamp() ELSE last_head_scan_at END,
                last_error_code=NULL,
                last_scan_at=clock_timestamp() WHERE account=%s AND provider_chat=%s''',
                (high_water,history_before,history_before,history_before,exhausted,initialized,
                 window_complete,head_complete,self.account,chat))

    def status(self,status,code):
        if status not in ('starting','receiving','degraded','stopped'): raise ValueError('invalid_status')
        if not re.fullmatch(r'[a-z_]{1,64}',code): raise ValueError('invalid_status_code')
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            if status=='stopped':self._end_period(c)
            c.execute('''UPDATE source_status SET status=%s,coverage='partial',
                detail_code=CASE WHEN detail_code='version_conflict' THEN detail_code ELSE %s END WHERE account=%s''',
                (status,code,self.account))
            c.execute('''INSERT INTO collector_health(account,status,detail_code) VALUES(%s,%s,%s)
                ON CONFLICT(account) DO UPDATE SET
                status=CASE WHEN collector_health.cooldown_until>clock_timestamp() AND excluded.status<>'stopped'
                    THEN 'degraded' ELSE excluded.status END,
                detail_code=CASE WHEN collector_health.cooldown_until>clock_timestamp() AND excluded.status<>'stopped'
                    THEN 'rate_limited' ELSE excluded.detail_code END,updated_at=clock_timestamp()''',
                (self.account,status,code))

    def cooldown_remaining(self):
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            row=c.execute('''SELECT greatest(0,extract(epoch FROM cooldown_until-clock_timestamp())) AS seconds
                FROM collector_health WHERE account=%s''',(self.account,)).fetchone()
            return float(row['seconds']) if row else 0

    def defer(self,seconds):
        if type(seconds) is not int or not 1<=seconds<2**31:raise ValueError('invalid_retry_delay')
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            c.execute('''INSERT INTO collector_health(account,status,detail_code,cooldown_until)
                VALUES(%s,'degraded','rate_limited',clock_timestamp()+%s*interval '1 second')
                ON CONFLICT(account) DO UPDATE SET status='degraded',detail_code='rate_limited',
                cooldown_until=greatest(collector_health.cooldown_until,excluded.cooldown_until),
                updated_at=clock_timestamp()''',(self.account,seconds))

    def discovery_started(self):
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            c.execute('''INSERT INTO collector_health(account,discovery_started_at)
                VALUES(%s,clock_timestamp()) ON CONFLICT(account) DO UPDATE SET
                discovery_started_at=clock_timestamp(),discovery_count=0,updated_at=clock_timestamp()''',
                (self.account,))

    def discovery_finished(self,limited=False):
        with self.archive.store.pool.connection() as c:
            self.archive.fence(c,self.account,self.lease)
            c.execute('''UPDATE collector_health SET discovery_completed_at=clock_timestamp(),
                discovery_limited=%s,updated_at=clock_timestamp() WHERE account=%s''',(limited,self.account))
            from .chat_roles import CHAT_ROLE_SCHEMA
            from .store import schema_version
            if schema_version(c)>=CHAT_ROLE_SCHEMA:
                # A chat this pass did not list has no current role (ANYM-533); its last observation time stays.
                c.execute('''UPDATE chats SET my_role='unknown',can_post=NULL WHERE account=%s
                    AND role_observed_at<(SELECT discovery_started_at FROM collector_health WHERE account=%s)
                    AND my_role IS DISTINCT FROM 'unknown' ''',(self.account,self.account))

    def apply(self,capture):
        capture=validate_capture(capture)
        if capture['kind']=='message':
            e=capture['envelope']
            if e['account']!=self.account: raise ValueError('foreign_capture')
            if not self.allows(self.policy(),e['chat']): return {'outcome':'excluded'}
            self.chat(e['chat'],capture['peer_kind'])
            result=self.archive.ingest(e,lease=self.lease)
            if result['outcome'] not in ('excluded','conflict','stale','tombstoned','deleted'):
                from .whatsapp_identity import record
                record(self.archive,e,capture.get('identity_observations',[]),self.lease)
            return result
        if capture['account']!=self.account: raise ValueError('foreign_capture')
        if capture['kind']=='read_state_gap':
            self.update_gap(capture['observed_at'])
            return {'outcome':'read_state_gap_recorded'}
        chat=capture['chat']
        if chat and not self.allows(self.policy(),chat): return {'outcome':'excluded'}
        if capture['kind']=='owner_read_state':
            from .reply_state import observe
            with self.archive.store.pool.connection() as c:
                c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:'+self.account,))
                self.archive.fence(c,self.account,self.lease)
                policy=c.execute('SELECT p.*,a.platform FROM account_policies p JOIN accounts a ON a.id=p.account '
                    'WHERE p.account=%s AND p.enabled',(self.account,)).fetchone()
                if not policy or policy['platform']!=capture['observation']['basis'].split('_',1)[0]:
                    raise RuntimeError('collection_disabled')
                if not self.allows(policy,chat):return {'outcome':'excluded'}
                cid=stable_id('chat',self.account,chat)
                c.execute('INSERT INTO chats(id,account,title,provider_chat) VALUES(%s,%s,%s,%s) '
                    'ON CONFLICT(id) DO NOTHING',(cid,self.account,chat,chat))
                observe(c,self.account,chat,capture['observation'])
            return {'outcome':'owner_read_observed'}
        if capture['kind']=='message_receipt':
            from .receipts import record
            with self.archive.store.pool.connection() as c:
                c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:'+self.account,))
                self.archive.fence(c,self.account,self.lease)
                policy=c.execute('''SELECT p.*,a.platform FROM account_policies p JOIN accounts a ON a.id=p.account
                    WHERE p.account=%s AND p.enabled''',(self.account,)).fetchone()
                if not policy or policy['platform']!=capture['platform']:raise RuntimeError('collection_disabled')
                if not self.allows(policy,chat):return {'outcome':'excluded'}
                result=record(c,capture)
            # Drain loops publish once after captures commit. A burst of captures
            # cannot multiply the per-loop publication budget; jobs survive an
            # interrupted publication even after the disk captures are removed.
            return result
        with self.archive.store.pool.connection() as c:
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:'+self.account,))
            self.archive.fence(c,self.account,self.lease)
            for mid in capture['ids']:
                c.execute('''INSERT INTO provider_tombstones(account,provider_chat,provider_id,observed_at)
                    VALUES(%s,%s,%s,%s) ON CONFLICT(account,provider_chat,provider_id)
                    DO UPDATE SET observed_at=greatest(provider_tombstones.observed_at,excluded.observed_at)''',
                    (self.account,chat,str(mid),capture['observed_at']))
        # The tombstone commit is independent: replay or later backfill still
        # applies it if the process dies before publishing these revisions.
        self.apply_tombstones()
        return {'outcome':'deletions_recorded'}

    def publish_receipts(self,limit=100):
        from .receipts import publish
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('invalid_receipt_limit')
        with self.archive.store.pool.connection() as c:
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:'+self.account,))
            self.archive.fence(c,self.account,self.lease)
            policy=c.execute('SELECT * FROM account_policies WHERE account=%s AND enabled',(self.account,)).fetchone()
            if not policy:raise RuntimeError('collection_disabled')
            return publish(c,self.account,limit=limit)

    def apply_tombstones(self,limit=100):
        with self.archive.store.pool.connection() as c:
            rows=c.execute('''SELECT r.snapshot,max(t.observed_at) AS deleted_at FROM messages m
                JOIN message_revisions r ON r.message=m.id AND r.revision=m.revision
                JOIN collector_chats cc ON cc.account=m.account AND cc.provider_chat=r.snapshot->>'provider_chat'
                JOIN provider_tombstones t ON t.account=m.account AND t.provider_id=m.provider_id
                    AND (t.provider_chat=cc.provider_chat OR (t.provider_chat='' AND cc.peer_kind IN ('user','chat')))
                WHERE m.account=%s AND NOT m.deleted GROUP BY m.id,r.snapshot LIMIT %s''',
                (self.account,limit)).fetchall()
        applied=0
        for row in rows:
            e=dict(row['snapshot']); e['chat']=e.pop('provider_chat')
            e.pop('message',None); e.pop('revision',None)
            e.update(deleted=True,body='',media=[],origin='reconcile',
                     updated_at=max(row['deleted_at'],datetime.fromisoformat(e['updated_at'])).isoformat())
            if 'formatting' in e:
                from .incoming_formatting import unknown
                e['formatting']=unknown()
            result=self.archive.ingest(e,lease=self.lease)
            applied+=result['outcome']=='deleted'
        return applied
