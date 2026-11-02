"""Admission protects independent scopes, native children and archive fences."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from uuid import uuid4

import httpx
import pytest

from messaging_service.enrichment import EnrichmentWorker, WorkerStopping, configuration
from messaging_service.enrichment_providers import ModelBackpressure, infer, normalize_result, validate_profile
from messaging_service.file_extract import Extraction
from messaging_service.file_index import FileIndexer
from messaging_service import resource_admission as admission


def local_profile(tmp_path,kind='audio'):
    raw={'provider':'faster_whisper','model':str(tmp_path)} if kind=='audio' else {
        'provider':'ollama','model':'synthetic-local','local_only_daemon':True}
    return validate_profile(raw,kind)


def cloud_profile(tmp_path,kind='image'):
    return validate_profile({'provider':'openai','model':'synthetic-cloud','allow_cloud':True,
        'api_key_file':str(tmp_path/'unused-secret')},kind)


# A real 1x1 PNG: the mini provider decodes a picture before it sends it (ANYM-696), so an image row needs one.
PNG_1X1=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')


def add_file(s,name,message='demo-tg-1',mime='audio/ogg',data=None):
    data=data or (PNG_1X1 if mime.startswith('image/') else b'synthetic-audio')
    digest=hashlib.sha256(data).hexdigest();(s.files/digest).write_bytes(data)
    with s.pool.connection() as c:
        c.execute('INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES(%s,%s,%s,%s,%s,%s)',
            (name,message,digest,name,mime,len(data)))


@pytest.fixture
def configured(seeded,tmp_path):
    s,_,_=seeded
    with s.pool.connection() as c:c.execute('TRUNCATE enrichment_budget')
    lock=tmp_path/'locks';lock.mkdir(mode=0o700)
    resources=[]
    def make(profiles=None,accounts=None,**limits):
        w=EnrichmentWorker(s.pool.conninfo,s.files,{'accounts':accounts or ['telegram-demo'],
            'max_jobs_per_day':20,'profiles':profiles or {'audio':local_profile(tmp_path)},**limits},
            lock_directory=lock)
        old=w.claim
        def claim():
            value=old()
            if value and value[0].get('_resource_lease'):resources.append(value[0]['_resource_lease'])
            return value
        w.claim=claim
        return w
    yield s,make,lock
    for resource in resources:resource.close()


def test_configuration_scoped_limits_are_explicit_and_bounded(tmp_path):
    path=tmp_path/'config.json'
    raw={'enabled':True,'accounts':['fixture'],'max_jobs_per_day':30,
        'profile_max_jobs_per_day':{'audio':20},'account_max_jobs_per_day':{'fixture':10},
        'profiles':{'audio':{'provider':'faster_whisper','model':str(tmp_path)}}}
    path.write_text(json.dumps(raw));path.chmod(0o600)
    assert configuration(path)['profile_max_jobs_per_day']=={'audio':20}
    for field,value in [('profile_max_jobs_per_day',{'unknown':1}),
                        ('account_max_jobs_per_day',{'other':1}),
                        ('profile_max_jobs_per_day',{'audio':True}),
                        ('account_max_jobs_per_day',{'fixture':0})]:
        path.write_text(json.dumps({**raw,field:value}))
        with pytest.raises(ValueError,match='invalid_enrichment_budget'):configuration(path)


def test_lock_failclosed_missing_directory_symlinks_and_public_lock(tmp_path):
    with pytest.raises(FileNotFoundError):admission.lock_file(tmp_path/'missing')
    link=tmp_path/'link';link.symlink_to(tmp_path,target_is_directory=True)
    with pytest.raises(ValueError):admission.lock_file(link)
    file=tmp_path/'local-heavy.lock';file.write_text('');file.chmod(0o666)
    with pytest.raises(ValueError):admission.lock_file(tmp_path)
    file.unlink();file.symlink_to(tmp_path/'missing-file')
    with pytest.raises(OSError):admission.lock_file(tmp_path)


def test_inherited_child_holds_slot_after_parent_descriptor_closes(tmp_path):
    fd=admission.lock_file(tmp_path)
    child=subprocess.Popen([sys.executable,'-c',
        'import sys;print("ready",flush=True);sys.stdin.read()'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,pass_fds=(fd,))
    try:
        assert child.stdout.readline()==b'ready\n'
        os.close(fd)
        assert admission.lock_file(tmp_path) is None
        child.communicate(timeout=10)
        next_fd=admission.lock_file(tmp_path);assert next_fd is not None;os.close(next_fd)
    finally:
        if child.poll() is None:child.terminate();child.wait(timeout=10)


def test_actual_parent_death_does_not_release_inherited_child_slot(tmp_path):
    root=str(Path(__file__).resolve().parents[1]);pidfile=tmp_path/'child.pid'
    source='''import os,subprocess,sys
sys.path.insert(0,sys.argv[1])
from messaging_service.resource_admission import lock_file
fd=lock_file(sys.argv[2])
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],pass_fds=(fd,))
open(sys.argv[3],'w').write(str(p.pid))
os._exit(0)
'''
    parent=subprocess.run([sys.executable,'-c',source,root,str(tmp_path),str(pidfile)],timeout=10)
    assert parent.returncode==0
    pid=int(pidfile.read_text())
    try:assert admission.lock_file(tmp_path) is None
    finally:os.kill(pid,15)
    # Ask another bounded process to acquire after the child terminates, without
    # leaving a test-owned sleeper or polling in the parent process.
    check='''import sys,time,os
sys.path.insert(0,sys.argv[1])
from messaging_service.resource_admission import lock_file
for _ in range(100):
 fd=lock_file(sys.argv[2])
 if fd is not None:os.close(fd);sys.exit(0)
 time.sleep(.02)
sys.exit(1)
'''
    assert subprocess.run([sys.executable,'-c',check,root,str(tmp_path)],timeout=5).returncode==0


@pytest.mark.parametrize('header',[b'%PDF-1.7',b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff'])
def test_declared_text_cannot_bypass_native_admission(tmp_path,header):
    digest=hashlib.sha256(header).hexdigest();(tmp_path/digest).write_bytes(header)
    assert admission.extraction_heavy(tmp_path,{'source_digest':digest,'source_mime':'text/plain','source_filename':'note.txt'})


def test_profile_sublimit_does_not_starve_other_profile(configured,tmp_path):
    s,make,_=configured;add_file(s,'voice');add_file(s,'image',mime='image/png')
    w=make({'audio':cloud_profile(tmp_path,'audio'),'image':cloud_profile(tmp_path)},
        profile_max_jobs_per_day={'audio':1})
    row,_=w.claim();assert row['kind']=='audio';assert w.finish(row,normalize_result({'text':'voice'}))
    add_file(s,'voice-two')
    row,_=w.claim();assert row['kind']=='image';assert w.finish(row,normalize_result({'title':'image'}))
    assert w.claim() is None
    with s.pool.connection() as c:
        assert c.execute('SELECT attempts FROM enrichment_budget WHERE day=current_date').fetchone()['attempts']==2
        assert c.execute("SELECT attempts FROM attachment_enrichment WHERE attachment='voice-two'").fetchone()['attempts']==0


def test_account_sublimit_and_auth_pause_are_independent(configured,tmp_path):
    s,make,_=configured;add_file(s,'tg-image',mime='image/png');add_file(s,'wa-image','demo-wa-1',mime='image/png')
    w=make({'image':cloud_profile(tmp_path)},accounts=['telegram-demo','whatsapp-demo-1'],
        account_max_jobs_per_day={'telegram-demo':1})
    row,_=w.claim();paused=row['account'];assert w.finish(row,error='model_auth_failed')
    add_file(s,'extra','demo-tg-1' if paused=='telegram-demo' else 'demo-wa-1',mime='image/png')
    other,_=w.claim();assert other['account']!=paused
    assert w.finish(other,normalize_result({'title':'ok'}));assert w.claim() is None
    w.publish_profiles()
    with s.pool.connection() as c:
        saved=c.execute('SELECT pause_reason,paused_until FROM processing_profiles WHERE account=%s',(paused,)).fetchone()
        assert saved=={'pause_reason':'model_auth_failed','paused_until':None}
        assert c.execute('SELECT attempts FROM enrichment_budget WHERE day=current_date').fetchone()['attempts']==2


def test_cooldown_persists_and_new_profile_does_not_reset_day_limit(configured,tmp_path):
    s,make,_=configured;add_file(s,'image',mime='image/png')
    w=make({'image':cloud_profile(tmp_path)},profile_max_jobs_per_day={'image':1})
    row,_=w.claim();assert w.finish(row,error='model_rate_limited')
    with s.pool.connection() as c:
        pause=c.execute('SELECT pause_reason,paused_until>clock_timestamp() active FROM processing_control').fetchone()
        assert pause=={'pause_reason':'model_rate_limited','active':True}
    w.config['profiles']['image']['model']='changed-model'
    assert w.claim() is None  # Model change must not evade the profile's daily cap.


def mini_profile(tmp_path,kind='image',**over):
    key=tmp_path/'mini-key'
    if not key.exists():key.write_text('synthetic-key');key.chmod(0o600)
    raw={'provider':'deedkit_inference','local_inference':True,'base_url':'https://deedkit-inference.tail1234.ts.net/v1',
        'api_key_file':str(key),'model':'audio-default' if kind=='audio' else 'vision-default',**over}
    return validate_profile(raw,kind)


def refusing(retry_after=None):
    def processor(files,row,profile):raise ModelBackpressure(retry_after)
    return processor


def through_the_provider(handler):
    """The real provider code against a mock transport: what the child would report for this answer."""
    def processor(files,row,profile):
        return infer(profile,row['kind'],Path(files)/row['source_digest'],row['mime'],row.get('source_text',''),
            transport=httpx.MockTransport(handler))
    return processor


def mini_down(request):
    raise httpx.ConnectError('synthetic refusal')


def envelope(status,code):
    return lambda request:httpx.Response(status,json={'error':{'code':code,'message':'synthetic'}})


def control(c,account='telegram-demo',kind='image'):
    return c.execute('''SELECT pause_reason,paused_until,extract(epoch FROM paused_until-clock_timestamp()) AS seconds
        FROM processing_control WHERE account=%s AND kind=%s''',(account,kind)).fetchall()


def budgets(c):
    return (c.execute('SELECT coalesce(sum(attempts),0) n FROM enrichment_budget').fetchone()['n'],
        c.execute('SELECT coalesce(sum(attempts),0) n FROM processing_budget').fetchone()['n'])


# spec: ANYM-685
def test_backpressure_requeues_without_consuming_an_attempt_or_budget(configured,tmp_path):
    s,make,_=configured;add_file(s,'photo',mime='image/png')
    w=make({'image':mini_profile(tmp_path)});w.processor=refusing()
    assert w.once() is True
    with s.pool.connection() as c:
        row=c.execute("SELECT state,attempts,lease_token,lease_until FROM attachment_enrichment WHERE attachment='photo'").fetchone()
        assert row=={'state':'queued','attempts':0,'lease_token':None,'lease_until':None}
        assert budgets(c)==(0,0)
        pause,=control(c)
        assert pause['pause_reason']=='model_backpressure' and 100<pause['seconds']<=120  # the profile's default cooldown
        assert c.execute("SELECT count(*) n FROM events WHERE kind='file.processing.failed'").fetchone()['n']==0
    w.publish_profiles()  # the pause is visible through the published profile state
    with s.pool.connection() as c:
        assert c.execute("SELECT pause_reason FROM processing_profiles WHERE account='telegram-demo' AND kind='image'"
            ).fetchone()['pause_reason']=='model_backpressure'
    assert w.claim() is None and w.once() is False  # a paused scope is not claimed


# spec: ANYM-685
def test_backpressure_pauses_only_its_scope_for_the_cooldown(configured,tmp_path):
    s,make,_=configured;add_file(s,'tg-photo',mime='image/png');add_file(s,'wa-photo','demo-wa-1',mime='image/png')
    w=make({'image':mini_profile(tmp_path)},accounts=['telegram-demo','whatsapp-demo-1'])
    first,profile=w.claim();paused=first['account'];w.defer(first,profile)
    second,_=w.claim()
    assert second['account']!=paused and w.claim() is None  # the other account runs; the paused one is not claimed
    with s.pool.connection() as c:
        pauses=c.execute('SELECT account,pause_reason FROM processing_control WHERE pause_reason IS NOT NULL').fetchall()
        assert pauses==[{'account':paused,'pause_reason':'model_backpressure'}]
        c.execute("UPDATE processing_control SET paused_until=clock_timestamp()-interval '1 second' WHERE pause_reason IS NOT NULL")
    again,_=w.claim();assert again['account']==paused and again['attempts']==1  # the cooldown is over: it runs, nothing was spent


# spec: ANYM-685
def test_mini_offline_for_a_day_leaves_attempts_and_daily_budget_unchanged(configured,tmp_path):
    s,make,_=configured;add_file(s,'photo',mime='image/png')
    # A daily cap of one: a single leaked attempt would starve the day.
    w=make({'image':mini_profile(tmp_path,backpressure_seconds=900)},max_jobs_per_day=1)
    calls=[];refuse=through_the_provider(lambda request:(calls.append(1),mini_down(request)))
    w.processor=refuse
    for cycle in range(96):  # a fake clock: 96 cooldowns of 900 s are 24 hours with the mini off
        assert w.once() is True and w.once() is False, cycle  # one claim per cooldown, then the scope waits
        with s.pool.connection() as c:  # the clock advances past the cooldown
            c.execute("UPDATE processing_control SET paused_until=clock_timestamp()-interval '1 second'")
    assert len(calls)==96
    with s.pool.connection() as c:
        row=c.execute("SELECT state,attempts,error_code FROM attachment_enrichment WHERE attachment='photo'").fetchone()
        assert (row['state'],row['attempts'])==('queued',0) and row['error_code']!='retry_exhausted'
        assert budgets(c)==(0,0)
    w.processor=through_the_provider(lambda request:httpx.Response(200,json={'choices':[{'message':{'content':
        json.dumps({'title':'Фото','summary':'Описание','text':'','document_type':'photo'})}}]}))
    assert w.once() is True  # the mini is back: the job runs at once and is described
    with s.pool.connection() as c:
        assert c.execute("SELECT state FROM attachment_enrichment WHERE attachment='photo'").fetchone()['state']=='ready'
        assert budgets(c)==(1,1)


# spec: ANYM-685
@pytest.mark.parametrize('retry_after,seconds',[(5,5),(899,899),(900,900),(100000,900),(0,1),(None,45),(-5,45),
    (float('nan'),45),(float('inf'),45),(True,45),('soon',45),(12.2,13)])
def test_retry_after_is_clamped_to_the_cap(configured,tmp_path,retry_after,seconds):
    s,make,_=configured;add_file(s,'photo',mime='image/png')
    w=make({'image':mini_profile(tmp_path,backpressure_seconds=45)});w.processor=refusing(retry_after)
    assert w.once() is True
    with s.pool.connection() as c:pause,=control(c)
    assert seconds-3<=pause['seconds']<=seconds+0.5, pause


# spec: ANYM-685
@pytest.mark.parametrize('auth_first',[True,False])
def test_late_backpressure_never_overwrites_an_auth_pause_or_shortens_a_longer_one(configured,tmp_path,auth_first):
    s,make,_=configured;add_file(s,'one',mime='image/png');add_file(s,'two',mime='image/png')
    w=make({'image':mini_profile(tmp_path)})
    first,profile=w.claim();second,_=w.claim()  # two jobs of one scope in flight
    if auth_first:
        assert w.finish(first,error='model_auth_failed');w.defer(second,profile,60)
    else:
        w.defer(second,profile,60);assert w.finish(first,error='model_auth_failed')
    with s.pool.connection() as c:
        assert control(c)==[{'pause_reason':'model_auth_failed','paused_until':None,'seconds':None}]
        states={r['attachment']:r['state'] for r in c.execute('SELECT attachment,state FROM attachment_enrichment')}
        assert states[first['attachment']]=='failed' and states[second['attachment']]=='queued'
        assert budgets(c)==(1,1)  # the failed job spent its attempt; the deferred one got its own back
    assert w.claim() is None  # until the operator clears the pause


# spec: ANYM-685
def test_backpressure_pause_keeps_the_later_expiry_and_a_longer_other_pause(configured):
    s,_,_=configured;scope=('telegram-demo','image',hashlib.sha256(b'profile').hexdigest())
    def now_pause(seconds,reason='model_backpressure'):
        with s.pool.connection() as c:
            c.execute('DELETE FROM processing_control');c.execute('''INSERT INTO processing_control(account,kind,profile_hash,pause_reason,paused_until)
                VALUES(%s,%s,%s,%s,clock_timestamp()+make_interval(secs=>%s))''',(*scope,reason,seconds))
    def defer(seconds):
        with s.pool.connection() as c:admission.defer_profile(c,*scope,seconds);return control(c)[0]
    now_pause(600);got=defer(60);assert got['pause_reason']=='model_backpressure' and 595<got['seconds']<=600  # shorter: unchanged
    now_pause(30);got=defer(600);assert got['pause_reason']=='model_backpressure' and 595<got['seconds']<=600  # longer: extends
    now_pause(1800,'model_rate_limited');got=defer(120);assert got['pause_reason']=='model_rate_limited' and 1795<got['seconds']<=1800
    now_pause(60,'model_rate_limited');got=defer(600);assert got['pause_reason']=='model_backpressure' and 595<got['seconds']<=600
    now_pause(-5);got=defer(120);assert got['pause_reason']=='model_backpressure' and 115<got['seconds']<=120  # an expired pause is replaced
    with s.pool.connection() as c:c.execute('DELETE FROM processing_control')
    assert defer(90)['pause_reason']=='model_backpressure'  # no row yet: created
    with s.pool.connection() as c:c.execute("UPDATE processing_control SET pause_reason=NULL,paused_until=NULL")
    got=defer(90);assert got['pause_reason']=='model_backpressure' and 85<got['seconds']<=90  # a row that only records starts
    with s.pool.connection() as c:c.execute("UPDATE processing_control SET pause_reason='model_auth_failed',paused_until=NULL")
    got=defer(900);assert got['pause_reason']=='model_auth_failed' and got['paused_until'] is None  # indefinite: never touched


# spec: ANYM-685
def test_refund_targets_the_claims_own_day_and_never_goes_below_zero(configured,tmp_path):
    from datetime import timedelta
    s,make,_=configured;add_file(s,'photo',mime='image/png')
    w=make({'image':mini_profile(tmp_path)});row,profile=w.claim()
    with s.pool.connection() as c:  # the day rolls over between the claim and the deferral
        c.execute("UPDATE enrichment_budget SET day=day-1");c.execute("UPDATE processing_budget SET day=day-1")
        c.execute("INSERT INTO enrichment_budget(day,attempts) VALUES(current_date,5)")
        c.execute("INSERT INTO processing_budget(day,account,kind,profile_hash,attempts) VALUES(current_date,%s,'image',%s,5)",
            (row['account'],row['profile_hash']))
    row['_claim_day']=row['_claim_day']-timedelta(days=1)
    w.defer(row,profile,30)
    with s.pool.connection() as c:
        assert c.execute('SELECT day=current_date today,attempts FROM enrichment_budget ORDER BY day').fetchall()==[
            {'today':False,'attempts':0},{'today':True,'attempts':5}]
        assert c.execute('SELECT day=current_date today,attempts FROM processing_budget ORDER BY day').fetchall()==[
            {'today':False,'attempts':0},{'today':True,'attempts':5}]
    with s.pool.connection() as c:before=control(c)
    w.defer(row,profile,600)  # a replay: the lease token no longer matches, nothing is refunded twice, no pause is extended
    with s.pool.connection() as c:
        assert c.execute('SELECT min(attempts) n FROM enrichment_budget').fetchone()['n']==0
        assert c.execute('SELECT min(attempts) n FROM processing_budget').fetchone()['n']==0
        assert c.execute("SELECT attempts FROM attachment_enrichment WHERE attachment='photo'").fetchone()['attempts']==0
        assert [r['paused_until'] for r in control(c)]==[r['paused_until'] for r in before]
        c.execute("UPDATE processing_control SET paused_until=clock_timestamp()-interval '1 second'")
    again,_=w.claim();assert again['lease_token']!=row['lease_token']  # a new lease on the same job
    with s.pool.connection() as c:before=control(c)
    w.defer(row,profile,600)  # a late answer for the old lease: it pauses nothing and takes nothing from the new one
    with s.pool.connection() as c:
        assert [r['paused_until'] for r in control(c)]==[r['paused_until'] for r in before]
        assert c.execute("SELECT state FROM attachment_enrichment WHERE attachment='photo'").fetchone()['state']=='leased'


# spec: ANYM-685
def test_two_workers_deferring_one_scope_do_not_interleave_with_a_claim(configured,tmp_path):
    s,make,_=configured;add_file(s,'one',mime='image/png');add_file(s,'two',mime='image/png')
    w1,w2=make({'image':mini_profile(tmp_path)}),make({'image':mini_profile(tmp_path)})
    first,profile=w1.claim();second,_=w2.claim()
    done,errors=[],[]
    def run(worker,row,seconds):
        try:worker.defer(row,profile,seconds);done.append(seconds)
        except BaseException as error:errors.append(error)
    threads=[threading.Thread(target=run,args=(w1,first,30)),threading.Thread(target=run,args=(w2,second,600))]
    with s.pool.connection() as c:
        c.execute('SELECT pg_advisory_xact_lock(74509139)')  # a claim in flight holds the lock the claim takes
        for thread in threads:thread.start()
        time.sleep(0.7)
        assert done==[] and errors==[]  # both deferrals wait for the claim
    for thread in threads:thread.join(10)
    assert sorted(done)==[30,600] and errors==[]
    with s.pool.connection() as c:
        pause,=control(c)
        assert pause['pause_reason']=='model_backpressure' and 595<pause['seconds']<=600  # the later expiry, whatever the order
        assert budgets(c)==(0,0)
        assert c.execute("SELECT count(*) n FROM attachment_enrichment WHERE state='queued' AND attempts=0").fetchone()['n']==2


# spec: ANYM-685
def test_engine_timeout_on_one_file_consumes_attempts_and_ends_in_retry_exhausted(configured,tmp_path):
    s,make,_=configured;add_file(s,'poison',mime='image/png')
    w=make({'image':mini_profile(tmp_path)});w.processor=through_the_provider(envelope(504,'engine_timeout'))
    for attempt in (1,2):
        assert w.once() is True
        with s.pool.connection() as c:
            row=c.execute("SELECT state,error_code,attempts FROM attachment_enrichment WHERE attachment='poison'").fetchone()
            assert row=={'state':'failed','error_code':'model_http_error','attempts':attempt}
            assert control(c)==[{'pause_reason':None,'paused_until':None,'seconds':None}]  # no pause, no free retry
            c.execute("UPDATE attachment_enrichment SET next_due=clock_timestamp()")
    third,_=w.claim();assert third['attempts']==3  # the worker dies on the third attempt: its lease runs out
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_enrichment SET lease_until=clock_timestamp()-interval '1 second'")
        assert budgets(c)==(3,3)
    assert w.claim() is None
    with s.pool.connection() as c:
        row=c.execute("SELECT state,error_code,attempts FROM attachment_enrichment WHERE attachment='poison'").fetchone()
        assert row=={'state':'failed','error_code':'retry_exhausted','attempts':3}


# spec: ANYM-685
def test_three_failed_attempts_leave_the_job_unclaimable(configured,tmp_path):
    s,make,_=configured;add_file(s,'poison',mime='image/png')
    w=make({'image':mini_profile(tmp_path)});w.processor=through_the_provider(envelope(502,'engine_failed'))
    for attempt in (1,2,3):
        assert w.once() is True
        with s.pool.connection() as c:c.execute("UPDATE attachment_enrichment SET next_due=clock_timestamp()")
    assert w.once() is False
    with s.pool.connection() as c:
        row=c.execute("SELECT state,error_code,attempts FROM attachment_enrichment WHERE attachment='poison'").fetchone()
        assert row=={'state':'failed','error_code':'model_http_error','attempts':3} and budgets(c)==(3,3)


# spec: ANYM-685
@pytest.mark.parametrize('status',[401,403])
def test_bad_key_pauses_the_scope_indefinitely(configured,tmp_path,status):
    s,make,_=configured;add_file(s,'photo',mime='image/png');add_file(s,'second',mime='image/png')
    w=make({'image':mini_profile(tmp_path)});w.processor=through_the_provider(envelope(status,'unauthorized'))
    assert w.once() is True
    with s.pool.connection() as c:
        assert control(c)==[{'pause_reason':'model_auth_failed','paused_until':None,'seconds':None}]
    assert w.once() is False and w.claim() is None  # nothing is claimed until the operator clears the pause
    with s.pool.connection() as c:
        c.execute('DELETE FROM processing_control')  # the operator's clearing statement
    assert w.claim() is not None


# spec: ANYM-685
def test_other_scopes_keep_running_while_the_mini_scope_is_paused(configured,tmp_path):
    s,make,_=configured;add_file(s,'photo',mime='image/png')
    mini=make({'image':mini_profile(tmp_path)});mini.processor=refusing()
    assert mini.once() is True  # the mini scope is paused now
    add_file(s,'voice');add_file(s,'other-photo','demo-wa-1',mime='image/png')
    both=make({'image':mini_profile(tmp_path),'audio':local_profile(tmp_path)},accounts=['telegram-demo','whatsapp-demo-1'])
    row,_=both.claim()
    assert row['kind']=='audio' or row['account']=='whatsapp-demo-1'  # a profile for another kind or account is claimed as before
    assert both.finish(row,normalize_result({'text':'voice','title':'photo'}))
    nxt,_=both.claim();assert (nxt['kind'],nxt['account'])!=('image','telegram-demo')
    with s.pool.connection() as c:
        assert c.execute("SELECT state FROM attachment_enrichment WHERE attachment='photo'").fetchone()['state']=='queued'


# spec: ANYM-685
def test_stop_during_a_mini_request_requeues_the_job(configured,tmp_path):
    s,make,_=configured;add_file(s,'photo',mime='image/png')
    def stopping(files,row,profile):raise WorkerStopping
    w=make({'image':mini_profile(tmp_path)});w.processor=stopping
    assert w.once() is True
    with s.pool.connection() as c:
        row=c.execute("SELECT state,error_code,attempts,lease_token FROM attachment_enrichment WHERE attachment='photo'").fetchone()
        assert row=={'state':'queued','error_code':'worker_stopping','attempts':0,'lease_token':None}  # as today
        assert c.execute('SELECT pause_reason FROM processing_control').fetchone()['pause_reason'] is None  # no scope pause
        assert budgets(c)==(1,1)  # the spend stays counted on the stop path


# spec: ANYM-685
def test_child_reports_backpressure_with_the_retry_after_and_the_worker_raises_it(tmp_path,monkeypatch,capsys):
    import io
    import messaging_service.enrichment as module
    # The child side: a ModelBackpressure becomes {"error": "model_backpressure", "retry_after": N}.
    payload={'profile':mini_profile(tmp_path),'kind':'image','path':str(tmp_path/'x'),'mime':'image/png','source_text':''}
    class Stdin:
        buffer=io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(sys,'stdin',Stdin)
    monkeypatch.setattr(module,'child_limits',lambda profile:None)
    def refuse(*args,**kwargs):raise ModelBackpressure(33)
    monkeypatch.setattr(module,'infer',refuse)
    monkeypatch.setattr(sys,'argv',['enrichment','--child'])
    module.main()
    assert json.loads(capsys.readouterr().out)=={'error':'model_backpressure','retry_after':33}
    # The parent side: process() raises it, with the retry_after the child reported (garbage becomes None).
    data=b'safe-original';sha=hashlib.sha256(data).hexdigest();(tmp_path/sha).write_bytes(data)
    row={'source_digest':sha,'bytes':len(data),'kind':'image','mime':'image/png'}
    class FakeChild:
        pid=123456;returncode=0;answer=b''
        def __init__(self,args,**kwargs):self.output=kwargs['stdout']
        def communicate(self,*args,**kwargs):self.output.write(type(self).answer)
        def wait(self):self.returncode=-9
    monkeypatch.setattr(module.subprocess,'Popen',FakeChild)
    monkeypatch.setattr(module.os,'killpg',lambda pid,sig:None)
    for answer,wanted in [(b'{"error":"model_backpressure","retry_after":45}',45),(b'{"error":"model_backpressure"}',None),
            (b'{"error":"model_backpressure","retry_after":"x"}',None)]:
        FakeChild.answer=answer
        with pytest.raises(ModelBackpressure) as error:module.process(tmp_path,row,mini_profile(tmp_path))
        assert error.value.retry_after==wanted


def test_shared_local_slot_blocks_other_worker_without_charging_attempt(configured,tmp_path):
    s,make,lock=configured;add_file(s,'voice');add_file(s,'scan',mime='image/png')
    whisper=make();vision=make({'image':local_profile(tmp_path,'image')})
    row,_=whisper.claim()
    assert vision.claim() is None
    indexer=FileIndexer(s.pool.conninfo,s.files,lock_directory=lock)
    # Text remains processable while native work waits for the heavy slot.
    job=indexer.claim();assert job['source_filename']=='probe-note.txt'
    assert indexer.finish(job,Extraction('ready','note','stdlib-text-v1'))=='ready'
    assert indexer.claim() is None
    with s.pool.connection() as c:
        assert c.execute('SELECT attempts FROM enrichment_budget WHERE day=current_date').fetchone()['attempts']==1
        assert c.execute("SELECT attempts FROM attachment_text WHERE attachment='scan'").fetchone()['attempts']==0
    assert whisper.finish(row,normalize_result({'text':'voice'}))
    next_row,_=vision.claim();assert next_row['kind']=='image';assert vision.finish(next_row,normalize_result({'title':'scan'}))


def test_expired_db_lease_is_recoverable_but_live_os_owner_still_fences(configured):
    s,make,_=configured;add_file(s,'voice');w=make();row,_=w.claim()
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_enrichment SET lease_until=clock_timestamp()-interval '1 second'")
        c.execute("UPDATE processing_resource_leases SET lease_until=clock_timestamp()-interval '1 second'")
    assert w.claim() is None
    row['_resource_lease'].close()  # Simulates exit of all surviving native processes.
    replacement,_=w.claim();assert replacement['lease_token']!=row['lease_token']
    assert not w.finish(row,normalize_result({'text':'stale'}))
    assert w.finish(replacement,normalize_result({'text':'current'}))


def test_concurrent_workers_share_global_ceiling_and_scoped_counter(configured,tmp_path):
    s,make,_=configured;add_file(s,'image-one',mime='image/png');add_file(s,'image-two',mime='image/png')
    workers=[make({'image':cloud_profile(tmp_path)},max_jobs_per_day=1) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:values=list(pool.map(lambda w:w.claim(),workers))
    assert sum(value is not None for value in values)==1
    with s.pool.connection() as c:
        assert c.execute('SELECT sum(attempts) n FROM processing_budget').fetchone()['n']==1
        assert c.execute('SELECT attempts FROM enrichment_budget WHERE day=current_date').fetchone()['attempts']==1


def test_fresh_voice_is_selected_before_old_backlog(configured):
    s,make,_=configured;add_file(s,'old','demo-tg-1');add_file(s,'fresh','demo-tg-2')
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET sent_at=clock_timestamp()-interval '1 year' WHERE id='demo-tg-1'")
        c.execute("UPDATE messages SET sent_at=clock_timestamp() WHERE id='demo-tg-2'")
    w=make();row,_=w.claim();assert row['attachment']=='fresh';assert w.finish(row,normalize_result({'text':'new'}))


def test_worker_passes_inherited_fence_into_isolated_process(tmp_path,monkeypatch):
    import messaging_service.enrichment as module
    data=b'synthetic';digest=hashlib.sha256(data).hexdigest();(tmp_path/digest).write_bytes(data)
    fd=admission.lock_file(tmp_path);lease=admission.ResourceLease(fd,uuid4());captured={}
    class Child:
        pid=123456;returncode=0
        def __init__(self,args,**kwargs):captured.update(kwargs);self.output=kwargs['stdout']
        def communicate(self,*args,**kwargs):self.output.write(b'{"text":"ok"}')
    monkeypatch.setattr(module.subprocess,'Popen',Child)
    monkeypatch.setattr(module.os,'killpg',lambda *_:None)
    try:
        assert module.process(tmp_path,{'source_digest':digest,'bytes':len(data),'kind':'audio',
            'mime':'audio/ogg','_resource_lease':lease},local_profile(tmp_path))['text']=='ok'
        assert captured['pass_fds']==(fd,)
        assert captured['env']['PROCESSING_RESOURCE_FD']==str(fd)
    finally:lease.close()


def test_native_subprocess_receives_inherited_fence(tmp_path,monkeypatch):
    import messaging_service.file_extract as native
    fd=admission.lock_file(tmp_path);captured={}
    monkeypatch.setenv('PROCESSING_RESOURCE_FD',str(fd))
    def run(command,**kwargs):captured.update(kwargs);return subprocess.CompletedProcess(command,0)
    monkeypatch.setattr(native.subprocess,'run',run)
    try:
        native._run_native(['fixture'])
        assert captured['pass_fds']==(fd,)
    finally:os.close(fd)


def test_scoped_profile_pause_survives_worker_restart_and_resumes_explicitly(configured,tmp_path):
    s,make,_=configured;add_file(s,'image',mime='image/png');p=cloud_profile(tmp_path)
    first=make({'image':p});row,_=first.claim();assert first.finish(row,error='model_auth_failed')
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_enrichment SET next_due=clock_timestamp() WHERE attachment='image'")
    restarted=make({'image':p});assert restarted.claim() is None
    with s.pool.connection() as c:
        c.execute('UPDATE processing_control SET pause_reason=NULL,paused_until=NULL')
    current,_=restarted.claim();assert current['kind']=='image'
    assert restarted.finish(current,normalize_result({'title':'ok'}))


def test_processing_role_can_claim_without_archive_write_rights(configured,tmp_path):
    from contextlib import contextmanager
    import messaging_service.enrichment as module
    s,make,_=configured;add_file(s,'voice')
    role='test_processing_enricher_020'
    ddl=Path(module.__file__).with_name('enrichment_role.sql').read_text().replace('messaging_enricher',role)
    with s.pool.connection() as c:c.execute(ddl)
    @contextmanager
    def restricted():
        with s.pool.connection() as c:
            c.execute('SET LOCAL ROLE '+role)
            yield c
    w=make();w.connection=restricted
    try:
        w.publish_profiles();row,_=w.claim();assert w.finish(row,normalize_result({'text':'ok'}))
        with restricted() as c:
            assert not c.execute("SELECT has_table_privilege(current_user,'messages','UPDATE') allowed").fetchone()['allowed']
            assert not c.execute("SELECT has_table_privilege(current_user,'attachments','DELETE') allowed").fetchone()['allowed']
    finally:
        with s.pool.connection() as c:c.execute('DROP OWNED BY '+role);c.execute('DROP ROLE '+role)


def test_failed_claim_commit_closes_fence_without_leaking_descriptor(tmp_path):
    from contextlib import contextmanager
    @contextmanager
    def failure():
        yield object()
        raise RuntimeError('synthetic-commit-failure')
    with pytest.raises(RuntimeError,match='synthetic-commit-failure'):
        with admission.transaction(failure()) as (_,resources):
            resources.append(admission.ResourceLease(admission.lock_file(tmp_path),uuid4()))
    fd=admission.lock_file(tmp_path);assert fd is not None;os.close(fd)


def test_application_ogg_is_claimed_by_the_only_audio_profile(configured):
    s,make,_=configured;add_file(s,'application-ogg',mime='application/ogg')
    w=make()
    row,_=w.claim();assert row['attachment']=='application-ogg' and row['kind']=='audio'
    assert w.finish(row,normalize_result({'text':'synthetic transcript'},audio=True))


def test_application_ogg_never_becomes_a_document_job(configured,tmp_path):
    s,make,_=configured;add_file(s,'application-ogg',mime='application/ogg')
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_text SET state='ready',text_content='synthetic parser output' WHERE attachment='application-ogg'")
    w=make({'document':cloud_profile(tmp_path,'document')})
    assert w.claim() is None
    with s.pool.connection() as c:
        assert c.execute("SELECT attempts FROM attachment_enrichment WHERE attachment='application-ogg'").fetchone()['attempts']==0
