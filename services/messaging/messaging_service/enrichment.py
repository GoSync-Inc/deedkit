"""Optional bounded enrichment worker, with independent credentials and fenced results."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import math

from .enrichment_providers import ModelBackpressure, ModelError, infer, private_text, profile_hash, validate_profile
from . import resource_admission
from . import processing_events
from . import inference_confinement
from .inference_confinement import ConfinementLost


def configuration(path):
    raw=json.loads(private_text(path,65536))
    if not isinstance(raw,dict) or set(raw)-{'enabled','profiles','max_jobs_per_day','accounts',
            'profile_max_jobs_per_day','account_max_jobs_per_day','confinement_blocked'}:
        raise ValueError('invalid_enrichment_config')
    if raw.get('enabled') is not True:raise ValueError('enrichment_disabled')
    profiles=raw.get('profiles')
    if not isinstance(profiles,dict) or not profiles or set(profiles)-{'audio','image','document'}:
        raise ValueError('invalid_enrichment_profiles')
    maximum=raw.get('max_jobs_per_day',20)
    if type(maximum) is not int or not 1<=maximum<=10000:raise ValueError('invalid_enrichment_budget')
    accounts=raw.get('accounts')
    if not isinstance(accounts,list) or not accounts or len(accounts)>100 or any(not isinstance(x,str) or not x or len(x)>64 for x in accounts):
        raise ValueError('enrichment_accounts_required')
    for field,allowed in [('profile_max_jobs_per_day',profiles),('account_max_jobs_per_day',accounts)]:
        limits=raw.get(field,{})
        if (not isinstance(limits,dict) or set(limits)-set(allowed)
                or any(type(value) is not int or not 1<=value<=10000 for value in limits.values())):
            raise ValueError('invalid_enrichment_budget')
    if 'confinement_blocked' in raw:inference_confinement.parse_targets(raw['confinement_blocked'])
    return {**raw,'max_jobs_per_day':maximum,'profiles':{k:validate_profile(v,k) for k,v in profiles.items()}}


def verified_copy(files,row,directory,maximum):
    """No symlink following, digest checked before any disclosure to a model."""
    digest=row['source_digest']
    if len(digest)!=64 or any(x not in '0123456789abcdef' for x in digest):raise ModelError('invalid_original')
    fd=os.open(Path(files)/digest,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    out=Path(directory)/'original';sha=hashlib.sha256();total=0
    with os.fdopen(fd,'rb') as f,out.open('xb') as target:
        st=os.fstat(f.fileno())
        # `original_size_limit` means only "larger than the profile's max_input_bytes" and is final for the profile
        # (FINAL_LIMIT_CODES); a file that is not what the row recorded is an integrity problem and stays retryable.
        if not stat.S_ISREG(st.st_mode) or st.st_size!=row['bytes']:raise ModelError('original_integrity_error')
        if st.st_size>maximum:raise ModelError('original_size_limit')
        while b:=f.read(65536):
            total+=len(b)
            if total>row['bytes']:raise ModelError('original_integrity_error')  # grew while it was read
            sha.update(b);target.write(b)
    if total!=row['bytes'] or sha.hexdigest()!=digest:raise ModelError('original_integrity_error')
    return out


MAX_RESULT_BYTES=8*1024**2
# A file that can never pass these limits fails once: `claim` does not take the row again while its profile hash is
# unchanged, and the event says `retryable` false. Editing the profile (a new hash) is the way back (ANYM-702).
FINAL_LIMIT_CODES=('original_size_limit','audio_duration_limit','audio_transcode_size_limit')


def child_limits(profile):
    """Hard Linux address-space cap plus CPU/files/fds; not a filesystem sandbox."""
    import resource
    cpu=profile['timeout_seconds']*2+10
    resource.setrlimit(resource.RLIMIT_CPU,(cpu,cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE,(max(MAX_RESULT_BYTES,profile['max_input_bytes']),)*2)
    resource.setrlimit(resource.RLIMIT_NOFILE,(128,128))
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    if sys.platform.startswith('linux'):
        memory=profile['memory_limit_mb']*1024**2
        resource.setrlimit(resource.RLIMIT_AS,(memory,memory))


class WorkerStopping(Exception):
    """The worker was told to stop while this job was in flight; the job is given back."""


STOP=threading.Event()
CHILDREN=set()
REQUEUE_SECONDS=30
FINISH_RETRY_SECONDS=30
BACKPRESSURE_CAP_SECONDS=900
# After a pass that failed on a transient database error: this delay, doubled per consecutive failure up to the limit.
DB_RETRY_SECONDS=5
DB_RETRY_LIMIT_SECONDS=60
DB_TRANSIENT=(psycopg.errors.QueryCanceled,psycopg.errors.LockNotAvailable,psycopg.errors.DeadlockDetected,
    psycopg.errors.SerializationFailure,psycopg.errors.AdminShutdown,psycopg.errors.CrashShutdown,
    psycopg.errors.CannotConnectNow,psycopg.errors.IdleSessionTimeout,psycopg.errors.TooManyConnections,
    psycopg.errors.DiskFull,psycopg.errors.OutOfMemory)
# A failed or lost connection has no SQLSTATE: psycopg raises a plain OperationalError with libpq's text, and a refused
# login or a bad DSN option looks the same. Only these texts are retried; a text not listed ends the worker, as before.
DB_CONNECTION_LOST=('timeout expired','Connection refused','Connection reset','Connection timed out','No route to host',
    'Network is unreachable','could not translate host name','server closed the connection',
    'could not send data to server','could not receive data from server','SSL connection has been closed',
    'SSL SYSCALL error: EOF detected','SSL error: unexpected eof while reading',
    'the database system is','too many clients','the connection is closed','connection is lost')
# psycopg joins the errors of every address it tried, so a refusal outranks a lost connection in the same text.
DB_LOGIN_REFUSED=('password authentication failed','no password supplied','no pg_hba.conf entry','role "','database "',
    'not permitted to log in','permission denied for database','certificate')


def transient_database_error(error):
    """A timeout, a conflict, a busy or restarting server, a lost connection. Anything else stays fatal: the DSN is
    read only at start, so a retry cannot repair a refused login or a bad option, and a running container hides it."""
    if isinstance(error,DB_TRANSIENT):return True
    if error.sqlstate:return False
    message=str(error)
    if any(text in message for text in DB_LOGIN_REFUSED):return False
    return any(text in message for text in DB_CONNECTION_LOST)


def cooldown_seconds(profile,retry_after):
    """The pause for a deferred job: the service's Retry-After when it is a usable number, else the profile's
    `backpressure_seconds` (default 120); never more than BACKPRESSURE_CAP_SECONDS, never less than one."""
    if type(retry_after) in (int,float) and math.isfinite(retry_after) and retry_after>=0:seconds=math.ceil(retry_after)
    else:seconds=profile.get('backpressure_seconds',120)
    return max(1,min(seconds,BACKPRESSURE_CAP_SECONDS))


def kill_group(child):
    try:os.killpg(child.pid,signal.SIGKILL)
    except ProcessLookupError:pass


def request_stop():
    """Signal-safe: end every running child group so the blocked job can be requeued."""
    STOP.set()
    for child in list(CHILDREN):kill_group(child)


def process(files,row,profile):
    if STOP.is_set():raise WorkerStopping
    with tempfile.TemporaryDirectory(prefix='archive-enrich-') as directory:
        path=verified_copy(files,row,directory,profile['max_input_bytes'])
        payload={'profile':profile,'kind':row['kind'],'path':str(path),'mime':row['mime'],
                 'source_text':row.get('source_text','')[:profile['max_text_chars']]}
        input_truncated=len(row.get('source_text',''))>profile['max_text_chars']
        env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','SSL_CERT_FILE')}
        env.update(HOME=directory,TMPDIR=directory,HF_HUB_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
            OMP_NUM_THREADS='2',OPENBLAS_NUM_THREADS='2')
        if row.get('_resource_lease'):env['PROCESSING_RESOURCE_FD']=str(row['_resource_lease'].fd)
        # -I ignores ambient Python path; only this trusted package root is added.
        entry='import sys;sys.path.insert(0,'+repr(str(Path(__file__).resolve().parents[1]))+');from messaging_service.enrichment import main;main()'
        with tempfile.TemporaryFile() as output:
            child=subprocess.Popen([sys.executable,'-I','-c',entry,'--child'],stdin=subprocess.PIPE,
                stdout=output,stderr=subprocess.DEVNULL,env=env,cwd=directory,
                start_new_session=True,close_fds=True,
                pass_fds=(row['_resource_lease'].fd,) if row.get('_resource_lease') else ())
            CHILDREN.add(child)
            try:
                # A stop between the check above and the registration must not leave the child running.
                if STOP.is_set():kill_group(child)
                child.communicate(json.dumps(payload).encode(),timeout=profile['timeout_seconds']+10)
            except subprocess.TimeoutExpired:
                kill_group(child);child.wait()
                raise ModelError('model_timeout') from None
            finally:
                CHILDREN.discard(child)
                # Includes ffmpeg/native descendants even if the main child died.
                kill_group(child)
            if STOP.is_set() and child.returncode:raise WorkerStopping
            if child.returncode:raise ModelError('model_process_failed')
            output.seek(0);raw=output.read(MAX_RESULT_BYTES+1)
        if len(raw)>MAX_RESULT_BYTES:raise ModelError('model_response_too_large')
        try:result=json.loads(raw)
        except (ValueError,TypeError,RecursionError):raise ModelError('invalid_model_response') from None
        if not isinstance(result,dict):raise ModelError('invalid_model_response')
        if result.get('error'):
            import re
            code=result['error']
            if not isinstance(code,str) or not re.fullmatch('[a-z_]{1,64}',code):code='model_process_failed'
            if code=='model_backpressure':  # the child could not reach the mini, or the mini said "not now"
                hint=result.get('retry_after')
                raise ModelBackpressure(hint if type(hint) in (int,float) else None)
            raise ModelError(code)
        result['truncated']=bool(result.get('truncated')) or input_truncated
        return result


def database_retry(error,delay):
    """The exception's type only: a psycopg message can name the DSN's host or carry row values."""
    from .telemetry import capture
    capture(error,'enrichment_failed')
    print(json.dumps({'status':'error','error_code':'enrichment_database_retry',
        'exception_type':type(error).__name__,'retry_seconds':delay}),file=sys.stderr,flush=True)


class EnrichmentWorker:
    def __init__(self,dsn,files,config,*,processor=process,lock_directory=None,confinement=None):
        self.dsn,self.files,self.config,self.processor=dsn,Path(files),config,processor
        self.confinement=confinement
        self.requeueing=False
        self.lock_directory=lock_directory or os.environ.get('PROCESSING_LOCK_DIR','/processing-locks')
    def connection(self):
        return psycopg.connect(self.dsn,row_factory=dict_row,connect_timeout=5,
            options='-c statement_timeout=5000 -c timezone=UTC')
    def publish_profiles(self,*,enabled=True):
        import re
        with self.connection() as c:
            for kind,profile in self.config['profiles'].items():
                # Installed model paths, URLs and key locations never leave the worker.
                model=profile['model']
                if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',model):model='configured_local_model' if profile['provider']=='faster_whisper' else 'configured_model'
                c.cursor().executemany('''INSERT INTO processing_profiles(account,kind,provider,model,enabled,expires_at,pause_reason,paused_until)
                    SELECT %s,%s,%s,%s,%s,clock_timestamp()+make_interval(secs=>%s),pc.pause_reason,pc.paused_until
                    FROM (SELECT 1) singleton LEFT JOIN processing_control pc
                    ON pc.account=%s AND pc.kind=%s AND pc.profile_hash=%s
                    AND pc.pause_reason IS NOT NULL AND (pc.paused_until IS NULL OR pc.paused_until>clock_timestamp())
                    ON CONFLICT(account,kind) DO UPDATE SET provider=excluded.provider,model=excluded.model,
                    enabled=excluded.enabled,observed_at=clock_timestamp(),expires_at=excluded.expires_at,
                    pause_reason=excluded.pause_reason,paused_until=excluded.paused_until''',
                    [(account,kind,profile['provider'],model,enabled,profile['timeout_seconds']+120,
                      account,kind,profile_hash(profile))
                     for account in self.config['accounts']])
    def unpublish_profiles(self):
        # Shutdown path: one statement, so the time does not grow with accounts x profiles.
        with self.connection() as c:
            c.execute('''UPDATE processing_profiles SET enabled=false,observed_at=clock_timestamp()
                WHERE account=ANY(%s) AND kind=ANY(%s)''',(self.config['accounts'],list(self.config['profiles'])))
    def _expire_leases(self):
        # Account/row locks must all precede the publication counter. End this
        # bounded transaction before a claim examines any other account's rows.
        with self.connection() as c:
            from .file_lifecycle import lifecycle_lock
            lifecycle_lock(c)
            c.execute('SELECT pg_advisory_xact_lock(74509139)')
            expired=c.execute('''SELECT e.attachment,m.account FROM attachment_enrichment e
                JOIN attachments a ON a.id=e.attachment JOIN messages m ON m.id=a.message
                WHERE m.account=ANY(%s) AND e.state='leased' AND e.lease_until<clock_timestamp()
                AND e.attempts>=3 ORDER BY m.account,e.attachment LIMIT 64''',
                (self.config['accounts'],)).fetchall()
            for account in sorted({item['account'] for item in expired}):
                c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                          ('archive:'+account,))
            for item in expired:
                failed=c.execute('''UPDATE attachment_enrichment e SET state='failed',error_code='retry_exhausted',
                    lease_token=NULL,lease_until=NULL,next_due=clock_timestamp(),updated_at=clock_timestamp()
                    FROM attachments a,messages m WHERE e.attachment=%s AND a.id=e.attachment AND m.id=a.message
                    AND m.account=ANY(%s) AND e.state='leased' AND e.lease_until<clock_timestamp()
                    AND e.attempts>=3 RETURNING e.*,m.account''',
                    (item['attachment'],self.config['accounts'])).fetchone()
                if failed:
                    processing_events.publish(c,failed,
                        stage='transcript' if failed['kind']=='audio' else 'description', state='failed',
                        identity={'profile_hash':failed['profile_hash'],'attempts':failed['attempts'],
                            'error_code':'retry_exhausted'},basis='model_generated',
                        provider=failed['provider'],model=failed['model'],error_code='retry_exhausted')

    def claim(self):
        self._expire_leases()
        with resource_admission.transaction(self.connection()) as (c,resources):
            from .file_lifecycle import lifecycle_lock,PROCESSABLE_ORIGINAL
            from .media_policy import RECOGNITION_WITHHELD
            lifecycle_lock(c)
            # Serialize claims/budget only, never inference. Failed attempts count.
            c.execute('SELECT pg_advisory_xact_lock(74509139)')
            c.execute("""INSERT INTO enrichment_budget(day,attempts) VALUES(current_date,0)
                ON CONFLICT DO NOTHING""")
            used=c.execute('SELECT attempts FROM enrichment_budget WHERE day=current_date FOR UPDATE').fetchone()['attempts']
            if used>=self.config['max_jobs_per_day']:return None
            candidates=[]
            account_used={r['account']:r['n'] for r in c.execute('''SELECT account,sum(attempts) n
                FROM processing_budget WHERE day=current_date GROUP BY account''').fetchall()}
            for kind,profile in self.config['profiles'].items():
                used_profile=c.execute('''SELECT coalesce(sum(attempts),0) n FROM processing_budget
                    WHERE day=current_date AND kind=%s''',(kind,)).fetchone()['n']
                if used_profile>=self.config.get('profile_max_jobs_per_day',{}).get(kind,10000):continue
                ph=profile_hash(profile)
                eligible=[]
                for account in self.config['accounts']:
                    used_account=account_used.get(account,0)
                    if used_account<self.config.get('account_max_jobs_per_day',{}).get(account,10000):eligible.append(account)
                if not eligible:continue
                condition={'audio':"(a.mime LIKE 'audio/%%' OR a.mime='application/ogg')",'image':"a.mime LIKE 'image/%%'",
                    'document':"a.mime NOT LIKE 'audio/%%' AND a.mime<>'application/ogg' AND a.mime NOT LIKE 'image/%%' AND f.state='ready' AND f.source_revision=m.revision AND f.source_digest=a.digest AND length(f.text_content)>0"}[kind]
                # A note downloaded on request today is as fresh as one sent today (ANYM-455).
                fresh="m.sent_at>=clock_timestamp()-interval '1 day'"+(" OR r.updated_at>=clock_timestamp()-interval '1 day'" if kind=='audio' else '')
                residency=' LEFT JOIN attachment_residency r ON r.digest=a.digest' if kind=='audio' else ''
                row=c.execute('''SELECT e.*,a.message,a.mime,a.bytes,m.account,m.sent_at,coalesce(f.text_content,'') AS source_text,
                    coalesce(f.truncated,false) AS source_truncated,
                    '''+fresh+''' AS fresh,
                    coalesce(pc.last_started_at,'epoch'::timestamptz) AS last_started_at
                    FROM attachment_enrichment e
                    JOIN attachments a ON a.id=e.attachment JOIN messages m ON m.id=a.message
                    LEFT JOIN attachment_text f ON f.attachment=a.id'''+residency+'''
                    LEFT JOIN processing_control pc ON pc.account=m.account AND pc.kind=%s AND pc.profile_hash=%s
                    WHERE '''+PROCESSABLE_ORIGINAL+''' AND NOT '''+RECOGNITION_WITHHELD+''' AND (pc.pause_reason IS NULL OR pc.paused_until<=clock_timestamp()) AND a.active AND NOT m.deleted AND e.source_digest=a.digest AND e.source_revision=m.revision
                    AND m.account=ANY(%s) AND '''+condition+''' AND e.next_due<=clock_timestamp()
                    AND ((e.state IN ('unconfigured','queued','failed') AND (e.attempts<3 OR e.profile_hash IS DISTINCT FROM %s)
                            AND NOT (e.state='failed' AND coalesce(e.error_code,'')=ANY(%s) AND e.profile_hash IS NOT DISTINCT FROM %s))
                        OR (e.state='leased' AND e.lease_until<clock_timestamp() AND e.attempts<3)
                        OR (e.state='unsupported' AND e.profile_hash IS DISTINCT FROM %s))
                    ORDER BY fresh DESC,last_started_at,m.sent_at DESC,a.id LIMIT 1 FOR UPDATE OF e SKIP LOCKED''',
                    (kind,ph,eligible,ph,list(FINAL_LIMIT_CODES),ph,ph)).fetchone()
                if row:candidates.append((row,kind,profile,ph))
            candidates.sort(key=lambda item:(not item[0]['fresh'],item[0]['last_started_at'],-item[0]['sent_at'].timestamp()))
            for row,kind,profile,ph in candidates:
                token=uuid4();ttl=profile['timeout_seconds']+60
                # Read before the UPDATE starts the database's clock, so this end is never later than e.lease_until.
                lease_end=time.monotonic()+ttl
                resource_lease=None
                if resource_admission.local_heavy(profile):
                    resource_lease=resource_admission.acquire(c,self.lock_directory,'enrichment',row['attachment'],token,ttl)
                    if resource_lease is None:continue
                    resources.append(resource_lease)
                try:
                    c.execute('''UPDATE attachment_enrichment SET state='leased',kind=%s,profile_hash=%s,
                        provider=%s,model=%s,attempts=CASE WHEN profile_hash IS DISTINCT FROM %s THEN 1 ELSE attempts+1 END,
                        lease_token=%s,lease_until=clock_timestamp()+make_interval(secs=>%s),updated_at=clock_timestamp()
                        WHERE attachment=%s''',(kind,ph,profile['provider'],profile['model'],ph,token,ttl,row['attachment']))
                    claim_day=c.execute('SELECT current_date AS day').fetchone()['day']
                    c.execute('UPDATE enrichment_budget SET attempts=attempts+1 WHERE day=current_date')
                    c.execute('''INSERT INTO processing_budget(day,account,kind,profile_hash,attempts)
                        VALUES(current_date,%s,%s,%s,1) ON CONFLICT(day,account,kind,profile_hash)
                        DO UPDATE SET attempts=processing_budget.attempts+1''',(row['account'],kind,ph))
                    c.execute('''INSERT INTO processing_control(account,kind,profile_hash,last_started_at)
                        VALUES(%s,%s,%s,clock_timestamp()) ON CONFLICT(account,kind,profile_hash)
                        DO UPDATE SET last_started_at=excluded.last_started_at,updated_at=clock_timestamp()''',
                        (row['account'],kind,ph))
                except BaseException:
                    if resource_lease:resource_lease.close()
                    raise
                return {**row,'kind':kind,'provider':profile['provider'],'model':profile['model'],
                    'attempts':1 if row['profile_hash']!=ph else row['attempts']+1,
                    'lease_token':token,'profile_hash':ph,'_resource_lease':resource_lease,'_claim_day':claim_day,
                    '_lease_end':lease_end},profile
        return None
    def finish(self,row,result=None,error=None):
        try:return self._finish(row,result,error)
        finally:
            lease=row.get('_resource_lease')
            if lease:lease.close()

    def _finish(self,row,result=None,error=None):
        from .enrichment_providers import normalize_result
        if result is not None:
            # Child isn't a trusted SQL/value source. Revalidate sizes/types.
            clean=normalize_result({**result,'segments':result.get('metadata',{}).get('segments',[])},audio=row['kind']=='audio')
            clean['truncated']=clean['truncated'] or bool(result.get('truncated')) or bool(row.get('source_truncated'))
            clean['metadata']['source_truncated']=bool(row.get('source_truncated'))
        else:clean={'title':'','summary':'','text':'','metadata':{},'truncated':False}
        with self.connection() as c:
            from .file_lifecycle import lifecycle_lock
            lifecycle_lock(c)
            # Derived passage vectors for a near-limit transcript need a bounded
            # write budget independent of the gateway's short read timeout.
            c.execute('SET LOCAL statement_timeout=30000')
            # The archive uses this same account-before-row lock order. Keep
            # result persistence and event publication on one frozen revision.
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                      ('archive:'+row['account'],))
            r=c.execute('''UPDATE attachment_enrichment e SET state=%s,title=%s,summary=%s,text_content=%s,
                metadata=%s,truncated=%s,error_code=%s,lease_token=NULL,lease_until=NULL,
                next_due=clock_timestamp()+interval '10 minutes',updated_at=clock_timestamp(),
                indexed_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END
                FROM attachments a,messages m WHERE e.attachment=%s AND e.lease_token=%s
                AND e.lease_until>clock_timestamp() AND a.id=e.attachment AND m.id=a.message
                AND a.active AND NOT m.deleted AND a.digest=e.source_digest AND m.revision=e.source_revision
                AND e.source_digest=%s AND e.source_revision=%s RETURNING e.attachment''',
                ('ready' if result is not None else 'failed',clean['title'],clean['summary'],clean['text'],Jsonb(clean['metadata']),
                 clean['truncated'],error,result is not None,row['attachment'],row['lease_token'],row['source_digest'],row['source_revision']))
            finished=r.fetchone() is not None
            if finished:
                identity={'profile_hash':row['profile_hash'],'artifact':clean}
                if result is None:
                    identity.update(lease_token=str(row['lease_token']),error_code=error)
                processing_events.publish(c,row,
                    stage='transcript' if row['kind']=='audio' else 'description',
                    state='ready' if result is not None else 'failed',identity=identity,
                    basis='model_generated',truncated=clean['truncated'],text_chars=len(clean['text']),
                    provider=row.get('provider'),model=row.get('model'),error_code=error,
                    retryable=result is None and row['attempts']<3 and error not in (
                        'model_auth_failed','codex_private_auth_required','codex_chatgpt_login_required',
                        'codex_login_check_failed',
                        'codex_cli_unavailable','codex_upgrade_required','invalid_model_key',
                        'local_model_not_installed','audio_tools_unavailable','retry_exhausted')+FINAL_LIMIT_CODES)
            resource_admission.release(c,row.get('_resource_lease'))
            if finished and error:resource_admission.pause_profile(c,row['account'],row['kind'],row['profile_hash'],error)
            return finished
    def write_result(self,row,result):
        """Write a good result, trying a transient database error again for FINISH_RETRY_SECONDS while the lease lasts.

        A replay is harmless: the token-matched UPDATE finds nothing once the first one committed. Returns the transient
        error it gave up on (at the deadline or on a stop), else None; the job then stays under its lease with no failure
        written. Any other error propagates with the resource lease open, for the failure's own finish to close."""
        deadline=min(time.monotonic()+FINISH_RETRY_SECONDS,row['_lease_end'])
        while True:
            try:self._finish(row,result)
            except psycopg.OperationalError as error:
                if not transient_database_error(error):raise
                left=deadline-time.monotonic()
                # The clock again after the wait: a late wakeup must not start an attempt past the deadline.
                if left>0 and not STOP.is_set() and not STOP.wait(min(1,left)) and time.monotonic()<deadline:continue
                unwritten=error
            else:unwritten=None
            lease=row.get('_resource_lease')
            if lease:lease.close()
            return unwritten
    def _requeue(self,row):
        with self.connection() as c:
            c.execute('''UPDATE attachment_enrichment SET state='queued',error_code='worker_stopping',
                attempts=greatest(attempts-1,0),lease_token=NULL,lease_until=NULL,
                next_due=clock_timestamp(),updated_at=clock_timestamp()
                WHERE attachment=%s AND state='leased' AND lease_token=%s''',(row['attachment'],row['lease_token']))
            resource_admission.release(c,row.get('_resource_lease'))
    def requeue(self,row):
        """Stop path: give the leased job back without consuming its attempt; the daily budget stays spent.

        A replay is harmless: the token-matched UPDATE finds nothing once the first one committed, so a
        lock wait or an uncertain commit is retried for REQUEUE_SECONDS instead of abandoning the lease.
        """
        deadline=time.monotonic()+REQUEUE_SECONDS
        try:
            while True:
                try:return self._requeue(row)
                except psycopg.Error:
                    if time.monotonic()>=deadline:raise
                    time.sleep(1)
        finally:
            # The child group is already gone; only now drop the inherited OS lock.
            lease=row.get('_resource_lease')
            if lease:lease.close()
    def _defer(self,row,profile,retry_after):
        seconds=cooldown_seconds(profile,retry_after)
        with self.connection() as c:
            from .file_lifecycle import lifecycle_lock
            lifecycle_lock(c)
            # The lock the claim takes: a claim and a deferral of one scope never interleave.
            c.execute('SELECT pg_advisory_xact_lock(74509139)')
            gave_back=c.execute('''UPDATE attachment_enrichment SET state='queued',error_code='model_backpressure',
                attempts=greatest(attempts-1,0),lease_token=NULL,lease_until=NULL,
                next_due=clock_timestamp(),updated_at=clock_timestamp()
                WHERE attachment=%s AND state='leased' AND lease_token=%s''',(row['attachment'],row['lease_token'])).rowcount
            if gave_back:  # only the holder of the lease: a replay or a late answer after a new lease changes nothing
                day=row.get('_claim_day')
                c.execute('UPDATE enrichment_budget SET attempts=greatest(attempts-1,0) WHERE day=coalesce(%s::date,current_date)',(day,))
                c.execute('''UPDATE processing_budget SET attempts=greatest(attempts-1,0)
                    WHERE day=coalesce(%s::date,current_date) AND account=%s AND kind=%s AND profile_hash=%s''',
                    (day,row['account'],row['kind'],row['profile_hash']))
                resource_admission.defer_profile(c,row['account'],row['kind'],row['profile_hash'],seconds)
            resource_admission.release(c,row.get('_resource_lease'))
    def defer(self,row,profile,retry_after=None):
        """The mini cannot take the job now: give it back without an attempt or a budget count, and pause the scope.

        Like the stop path, a lock wait or an uncertain commit is retried for REQUEUE_SECONDS; the token-matched
        UPDATE makes a replay harmless."""
        deadline=time.monotonic()+REQUEUE_SECONDS
        try:
            while True:
                try:return self._defer(row,profile,retry_after)
                except psycopg.Error:
                    if time.monotonic()>=deadline:raise
                    time.sleep(1)
        finally:
            lease=row.get('_resource_lease')
            if lease:lease.close()
    def once(self):
        claim=self.claim()
        if not claim:return False
        row,profile=claim
        try:unwritten=self.write_result(row,self.processor(self.files,row,profile))
        except WorkerStopping:
            self.requeueing=True
            self.requeue(row)
        except ModelBackpressure as exc:self.defer(row,profile,exc.retry_after)
        except ModelError as exc:self.finish(row,error=exc.code)
        except Exception as error:
            from .telemetry import capture
            capture(error, 'enrichment_failed')
            self.finish(row,error='enrichment_failed')
        else:
            # Outside the handlers above, so it reaches the backoff in run() as no recorded failure.
            if unwritten:raise unwritten
        return True
    def run(self):
        guard=self.confinement
        if guard:
            # ANYM-688: a worker that sends files to the mini proves it cannot leave its network before it
            # publishes a profile or claims anything (raises ConfinementLost; nothing was published yet), and
            # again before every claim older than the guard's freshness and in the background.
            guard.ensure()
            guard.start_monitor(request_stop)
        delay=DB_RETRY_SECONDS
        try:
            try:
                while not STOP.is_set():
                    if guard:guard.ensure()
                    try:
                        self.publish_profiles()
                        if not self.once():STOP.wait(10)
                    except psycopg.OperationalError as error:
                        # Incident 11-05: one statement timeout in claim() ended the worker. A job whose finish failed, or
                        # a claim whose commit was lost, stays under its lease until that expires. The stop path keeps
                        # ending the worker. The next pass proves the confinement again first; a lost one wakes this wait.
                        if STOP.is_set() or self.requeueing or not transient_database_error(error):raise
                        database_retry(error,delay)
                        STOP.wait(delay);delay=min(DB_RETRY_LIMIT_SECONDS,delay*2)
                    else:delay=DB_RETRY_SECONDS
            finally:
                # Also when the stop path itself fails: never keep advertising a worker that is going away.
                if guard:guard.stop_monitor()
                self.unpublish_profiles()
        except Exception:
            # ANYM-786: exit 78 must stay fatal and readable, so a lost confinement outranks whatever failed on the way
            # out; that error's text stays out of stderr, which carries only the check names.
            if not (guard and guard.lost):raise
        if guard and guard.lost:raise ConfinementLost(guard.lost)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path);parser.add_argument('--child',action='store_true')
    args=parser.parse_args()
    if args.child:
        try:
            payload=json.loads(sys.stdin.buffer.read(2*1024**2))
            child_limits(payload['profile'])
            result=infer(payload['profile'],payload['kind'],payload['path'],payload['mime'],payload.get('source_text',''))
            encoded=json.dumps(result,ensure_ascii=False).encode()
            if len(encoded)>MAX_RESULT_BYTES:raise ModelError('model_response_too_large')
            sys.stdout.buffer.write(encoded)
        except ModelBackpressure as exc:print(json.dumps({'error':exc.code,'retry_after':exc.retry_after}))
        except ModelError as exc:print(json.dumps({'error':exc.code}))
        except Exception:print(json.dumps({'error':'enrichment_failed'}))
        return
    if not args.config:parser.error('--config required')
    from .telemetry import configure
    configure('enricher')
    cfg=configuration(args.config)
    guard=inference_confinement.Confinement(inference_confinement.mini_hosts(cfg),blocked=inference_confinement.blocked_targets(cfg)) if inference_confinement.required(cfg) else None
    worker=EnrichmentWorker(private_text(os.environ['DATABASE_URL_FILE']).strip(),os.environ['PROBE_FILES'],cfg,confinement=guard)
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:request_stop())
    try:worker.run()
    except ConfinementLost as lost:
        # Names of the failed checks only; the worker has claimed nothing (or stopped claiming) and exits non-zero.
        print(json.dumps({'error':'confinement_not_effective','checks':lost.args[0]}),file=sys.stderr)
        sys.exit(inference_confinement.EXIT_CODE)

if __name__=='__main__':main()
