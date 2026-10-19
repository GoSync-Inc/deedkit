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

from .enrichment_providers import ModelError, infer, private_text, profile_hash, validate_profile
from . import resource_admission
from . import processing_events


def configuration(path):
    raw=json.loads(private_text(path,65536))
    if not isinstance(raw,dict) or set(raw)-{'enabled','profiles','max_jobs_per_day','accounts',
            'profile_max_jobs_per_day','account_max_jobs_per_day'}:
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
    return {**raw,'max_jobs_per_day':maximum,'profiles':{k:validate_profile(v,k) for k,v in profiles.items()}}


def verified_copy(files,row,directory,maximum):
    """No symlink following, digest checked before any disclosure to a model."""
    digest=row['source_digest']
    if len(digest)!=64 or any(x not in '0123456789abcdef' for x in digest):raise ModelError('invalid_original')
    fd=os.open(Path(files)/digest,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    out=Path(directory)/'original';sha=hashlib.sha256();total=0
    with os.fdopen(fd,'rb') as f,out.open('xb') as target:
        st=os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size!=row['bytes'] or st.st_size>maximum:
            raise ModelError('original_size_limit')
        while b:=f.read(65536):
            total+=len(b)
            if total>maximum:raise ModelError('original_size_limit')
            sha.update(b);target.write(b)
    if total!=row['bytes'] or sha.hexdigest()!=digest:raise ModelError('original_integrity_error')
    return out


MAX_RESULT_BYTES=8*1024**2


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
            raise ModelError(code)
        result['truncated']=bool(result.get('truncated')) or input_truncated
        return result


class EnrichmentWorker:
    def __init__(self,dsn,files,config,*,processor=process,lock_directory=None):
        self.dsn,self.files,self.config,self.processor=dsn,Path(files),config,processor
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
                row=c.execute('''SELECT e.*,a.message,a.mime,a.bytes,m.account,m.sent_at,coalesce(f.text_content,'') AS source_text,
                    coalesce(f.truncated,false) AS source_truncated,
                    m.sent_at>=clock_timestamp()-interval '1 day' AS fresh,
                    coalesce(pc.last_started_at,'epoch'::timestamptz) AS last_started_at
                    FROM attachment_enrichment e
                    JOIN attachments a ON a.id=e.attachment JOIN messages m ON m.id=a.message
                    LEFT JOIN attachment_text f ON f.attachment=a.id
                    LEFT JOIN processing_control pc ON pc.account=m.account AND pc.kind=%s AND pc.profile_hash=%s
                    WHERE '''+PROCESSABLE_ORIGINAL+''' AND (pc.pause_reason IS NULL OR pc.paused_until<=clock_timestamp()) AND a.active AND NOT m.deleted AND e.source_digest=a.digest AND e.source_revision=m.revision
                    AND m.account=ANY(%s) AND '''+condition+''' AND e.next_due<=clock_timestamp()
                    AND ((e.state IN ('unconfigured','queued','failed') AND (e.attempts<3 OR e.profile_hash IS DISTINCT FROM %s))
                        OR (e.state='leased' AND e.lease_until<clock_timestamp() AND e.attempts<3)
                        OR (e.state='unsupported' AND e.profile_hash IS DISTINCT FROM %s))
                    ORDER BY fresh DESC,last_started_at,m.sent_at DESC,a.id LIMIT 1 FOR UPDATE OF e SKIP LOCKED''',
                    (kind,ph,eligible,ph,ph)).fetchone()
                if row:candidates.append((row,kind,profile,ph))
            candidates.sort(key=lambda item:(not item[0]['fresh'],item[0]['last_started_at'],-item[0]['sent_at'].timestamp()))
            for row,kind,profile,ph in candidates:
                token=uuid4();ttl=profile['timeout_seconds']+60
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
                    'lease_token':token,'profile_hash':ph,'_resource_lease':resource_lease},profile
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
                        'local_model_not_installed','audio_tools_unavailable','retry_exhausted'))
            resource_admission.release(c,row.get('_resource_lease'))
            if finished and error:resource_admission.pause_profile(c,row['account'],row['kind'],row['profile_hash'],error)
            return finished
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
    def once(self):
        claim=self.claim()
        if not claim:return False
        row,profile=claim
        try:self.finish(row,self.processor(self.files,row,profile))
        except WorkerStopping:self.requeue(row)
        except ModelError as exc:self.finish(row,error=exc.code)
        except Exception as error:
            from .telemetry import capture
            capture(error, 'enrichment_failed')
            self.finish(row,error='enrichment_failed')
        return True
    def run(self):
        try:
            while not STOP.is_set():
                self.publish_profiles()
                if not self.once():STOP.wait(10)
        finally:
            # Also when the stop path itself fails: never keep advertising a worker that is going away.
            self.unpublish_profiles()


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
        except ModelError as exc:print(json.dumps({'error':exc.code}))
        except Exception:print(json.dumps({'error':'enrichment_failed'}))
        return
    if not args.config:parser.error('--config required')
    from .telemetry import configure
    configure('enricher')
    cfg=configuration(args.config)
    worker=EnrichmentWorker(private_text(os.environ['DATABASE_URL_FILE']).strip(),os.environ['PROBE_FILES'],cfg)
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:request_stop())
    worker.run()

if __name__=='__main__':main()
