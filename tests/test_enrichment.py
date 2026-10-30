import hashlib
import json
from pathlib import Path

import httpx
import pytest

from messaging_service.enrichment import EnrichmentWorker, verified_copy
from messaging_service.enrichment_providers import ModelError, infer, normalize_result, validate_profile
from messaging_service.envelope import fingerprint,validate
from test_archive import archive as archive,event


def profile(tmp_path,provider='openai',kind='image'):
    key=tmp_path/'model-key';key.write_text('synthetic-key');key.chmod(0o600)
    return validate_profile({'provider':provider,'model':'fixture-model','allow_cloud':True,
        'api_key_file':str(key)},kind)


@pytest.mark.parametrize('provider',['openai','anthropic','gemini','ollama'])
def test_model_protocols_only_send_file_no_tools(tmp_path,provider):
    p=profile(tmp_path,provider);image=tmp_path/'image';image.write_bytes(b'synthetic image')
    requests=[]
    def respond(request):
        requests.append(request);body=json.loads(request.content)
        assert 'tools' not in body and 'synthetic image' not in request.url.path
        content=json.dumps({'title':'Паспорт','summary':'Фотография документа','text':'SOURCE ONLY'})
        response={'openai':{'choices':[{'message':{'content':content}}]},
            'anthropic':{'content':[{'type':'text','text':content}]},
            'gemini':{'candidates':[{'content':{'parts':[{'text':content}]}}]},
            'ollama':{'message':{'content':content}}}[provider]
        return httpx.Response(200,json=response)
    value=infer(p,'image',image,'image/png',transport=httpx.MockTransport(respond))
    assert value['title']=='Паспорт' and value['metadata']['generated_by_model']
    assert len(requests)==1


def test_model_opt_in_endpoint_and_unknown_audio_provider(tmp_path):
    with pytest.raises(ValueError,match='cloud_processing_not_enabled'):
        validate_profile({'provider':'openai','model':'x'},'image')
    with pytest.raises(ValueError,match='invalid_model_endpoint'):
        validate_profile({'provider':'ollama','model':'x','base_url':'http://evil.example'},'image')
    with pytest.raises(ValueError,match='provider_does_not_support_asr'):
        profile(tmp_path,'anthropic','audio')
    with pytest.raises(ValueError,match='local_model_directory_required'):
        validate_profile({'provider':'faster_whisper','model':'small'},'audio')


def test_provider_failure_redacts_body_and_key(tmp_path):
    p=profile(tmp_path);image=tmp_path/'img';image.write_bytes(b'x')
    def respond(_):return httpx.Response(401,text='SYNTHETIC_PRIVATE_SECRET')
    with pytest.raises(ModelError,match='model_auth_failed') as error:
        infer(p,'image',image,'image/png',transport=httpx.MockTransport(respond))
    assert 'SECRET' not in str(error.value)
    with pytest.raises(ModelError):normalize_result({'text':'bad\x00'})


def test_original_integrity_before_disclosure(tmp_path):
    data=b'original';sha=hashlib.sha256(data).hexdigest();(tmp_path/sha).write_bytes(b'altered!')
    dest=tmp_path/'work';dest.mkdir()
    with pytest.raises(ModelError,match='original_integrity_error'):
        verified_copy(tmp_path,{'source_digest':sha,'bytes':len(data)},dest,100)


def add_audio(s):
    data=b'synthetic-audio';sha=hashlib.sha256(data).hexdigest();(s.files/sha).write_bytes(data)
    with s.pool.connection() as c:
        c.execute("INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES('enrich-audio','demo-tg-2',%s,'voice.wav','audio/wav',%s)",(sha,len(data)))
    return sha


def worker(s,tmp_path,maximum=20):
    cfg={'accounts':['telegram-demo'],'max_jobs_per_day':maximum,'profiles':{'audio':profile(tmp_path,kind='audio')}}
    return EnrichmentWorker(s.pool.conninfo,s.files,cfg,processor=lambda *_:normalize_result({'text':'Расшифровка договора','segments':[{'start':0,'end':1,'text':'Расшифровка договора'}]},audio=True))


def test_enrichment_budget_fencing_and_deletion(seeded,tmp_path):
    s,_,_=seeded;add_audio(s);w=worker(s,tmp_path,1)
    with s.pool.connection() as c:c.execute('TRUNCATE enrichment_budget')
    row,p=w.claim();assert row['kind']=='audio'
    assert w.claim() is None
    with s.pool.connection() as c:c.execute("UPDATE messages SET revision=revision+1,deleted=true WHERE id='demo-tg-2'")
    assert w.finish(row,normalize_result({'text':'stale'})) is False
    with s.pool.connection() as c:
        assert c.execute("SELECT state FROM attachment_enrichment WHERE attachment='enrich-audio'").fetchone()['state']=='superseded'


def test_enrichment_full_text_index_and_no_cross_scope(seeded,tmp_path):
    s,key,other=seeded;add_audio(s);w=worker(s,tmp_path)
    with s.pool.connection() as c:c.execute('TRUNCATE enrichment_budget')
    assert w.once()
    with s.pool.connection() as c:
        row=c.execute("SELECT * FROM attachment_enrichment WHERE attachment='enrich-audio'").fetchone()
    assert row['state']=='ready' and row['text_content']=='Расшифровка договора'
    assert row['metadata']['segments'][0]['end']==1
    # File API coverage is tested separately; worker records source revision/digest.
    assert row['source_revision']==1 and row['source_digest']


def test_metadata_observations_do_not_change_content_revision(archive):
    ar,s,key,_=archive
    raw=event();mid=ar.ingest(raw)['message']
    enriched={**raw,'metadata':{'reply_to_provider_id':'12','forwarded':True,'media_kind':'photo'}}
    assert fingerprint(validate(raw))==fingerprint(validate(enriched))
    assert ar.ingest(enriched)['outcome']=='unchanged'
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) n FROM message_revisions WHERE message=%s',(mid,)).fetchone()['n']==1
        value=c.execute('SELECT metadata FROM message_metadata WHERE message=%s',(mid,)).fetchone()['metadata']
    assert value['reply_to_provider_id']=='12'


def test_ollama_locality_requires_daemon_assertion_and_rejects_cloud_models():
    with pytest.raises(ValueError,match='local_only_daemon_assertion_required'):
        validate_profile({'provider':'ollama','model':'local-model'},'image')
    assert validate_profile({'provider':'ollama','model':'local-model','local_only_daemon':True},'image')
    for model in ('gemma4:cloud','gpt-oss:120b-cloud'):
        with pytest.raises(ValueError,match='cloud_processing_not_enabled'):
            validate_profile({'provider':'ollama','model':model,'local_only_daemon':True},'image')
        assert validate_profile({'provider':'ollama','model':model,'allow_cloud':True},'image')


def test_segments_have_aggregate_bound_and_audio_provenance_survives_validation():
    with pytest.raises(ModelError,match='transcript_size_limit'):
        normalize_result({'text':'small','segments':[{'start':0,'end':1,'text':'x'*10000}]*101},audio=True)
    value=normalize_result({'text':'words','metadata':{'duration_seconds':2.0,'transcoded':True,
        'transcode_lossy':True,'transcode_format':'mp3','audio_truncated':False}},audio=True)
    assert value['metadata']['transcoded'] and value['metadata']['transcode_lossy']
    with pytest.raises(ModelError):normalize_result({'metadata':{'duration_seconds':float('inf')}},audio=True)


def fake_audio_tools(monkeypatch,*,duration=12,converted_duration=None,output_size=32):
    import subprocess
    import messaging_service.enrichment_providers as providers
    calls=[]
    def run(command,**kwargs):
        calls.append(command)
        if command[0]=='ffprobe':
            seconds=(converted_duration if converted_duration is not None else duration) if str(command[-1]).endswith('.mp3') else duration
            kwargs['stdout'].write(json.dumps({'streams':[{'duration':str(seconds)}],
                'format':{'duration':str(seconds)}}).encode())
        elif command[0]=='ffmpeg':Path(command[-1]).write_bytes(b'x'*output_size)
        else:raise AssertionError(command)
        return subprocess.CompletedProcess(command,0)
    monkeypatch.setattr(providers.subprocess,'run',run)
    return calls


def test_duration_checked_before_model_or_network(tmp_path,monkeypatch):
    p=profile(tmp_path,kind='audio');p['max_audio_seconds']=10
    fake_audio_tools(monkeypatch,duration=11)
    audio=tmp_path/'audio';audio.write_bytes(b'synthetic')
    def forbidden(_):raise AssertionError('No request is allowed for oversized duration')
    with pytest.raises(ModelError,match='audio_duration_limit'):
        infer(p,'audio',audio,'audio/wav',transport=httpx.MockTransport(forbidden))
    local=validate_profile({'provider':'faster_whisper','model':str(tmp_path),'max_audio_seconds':10},'audio')
    with pytest.raises(ModelError,match='audio_duration_limit'):infer(local,'audio',audio,'audio/ogg')


def test_ogg_transcode_preserves_original_checks_complete_duration_and_labels_lossy(tmp_path,monkeypatch):
    p=profile(tmp_path,kind='audio');audio=tmp_path/'audio';audio.write_bytes(b'synthetic-original')
    before=audio.read_bytes();calls=fake_audio_tools(monkeypatch)
    requests=[]
    def respond(request):
        requests.append(request)
        assert b'recording.mp3' in request.content and b'audio/mpeg' in request.content
        return httpx.Response(200,json={'text':'transcript'})
    result=infer(p,'audio',audio,'audio/ogg',transport=httpx.MockTransport(respond))
    assert audio.read_bytes()==before and len(requests)==1
    assert result['metadata']['transcoded'] and result['metadata']['transcode_lossy']
    assert not result['metadata']['audio_truncated']
    command=next(c for c in calls if c[0]=='ffmpeg')
    assert '-t' not in command and '-ss' not in command and '-fs' not in command
    fake_audio_tools(monkeypatch,converted_duration=8)
    with pytest.raises(ModelError,match='audio_transcode_incomplete'):
        infer(p,'audio',audio,'audio/ogg',transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(AssertionError())))
    p['max_input_bytes']=100
    fake_audio_tools(monkeypatch,output_size=101)
    with pytest.raises(ModelError,match='audio_transcode_size_limit'):infer(p,'audio',audio,'audio/ogg')


def test_process_uses_bounded_file_output_and_cleans_timeout_group(tmp_path,monkeypatch):
    import subprocess
    import messaging_service.enrichment as worker_module
    data=b'safe-original';sha=hashlib.sha256(data).hexdigest();(tmp_path/sha).write_bytes(data)
    p=profile(tmp_path);row={'source_digest':sha,'bytes':len(data),'kind':'image','mime':'image/png'}
    killed=[];captured={}
    class FakeChild:
        pid=123456;returncode=0
        def __init__(self,args,**kwargs):
            captured.update(kwargs)
            assert '-I' in args and kwargs['start_new_session'] is True
            assert kwargs['stdout'] is not subprocess.PIPE
            self.output=kwargs['stdout']
        def communicate(self,*args,**kwargs):
            captured['payload']=json.loads(args[0]);captured['input_bytes']=len(args[0])
            self.output.write(b'{"title":"safe","text":"","summary":""}')
        def wait(self):self.returncode=-9
    monkeypatch.setattr(worker_module.subprocess,'Popen',FakeChild)
    monkeypatch.setattr(worker_module.os,'killpg',lambda pid,sig:killed.append(pid))
    assert worker_module.process(tmp_path,row,p)['title']=='safe'
    assert killed==[123456]
    assert 'PYTHONPATH' not in captured['env'] and 'DATABASE_URL_FILE' not in captured['env']
    row['source_text']='я'*1000000
    bounded=worker_module.process(tmp_path,row,p)
    assert bounded['truncated'] and len(captured['payload']['source_text'])==p['max_text_chars']
    assert captured['input_bytes']<2*1024**2
    def timeout(*args,**kwargs):raise subprocess.TimeoutExpired('fixture',1)
    monkeypatch.setattr(FakeChild,'communicate',timeout)
    with pytest.raises(ModelError,match='model_timeout'):worker_module.process(tmp_path,row,p)
    assert killed[-2:]==[123456,123456]


def test_third_expired_lease_becomes_failed_then_new_profile_can_retry(seeded,tmp_path):
    s,_,_=seeded;add_audio(s);w=worker(s,tmp_path)
    with s.pool.connection() as c:c.execute('TRUNCATE enrichment_budget')
    job,_=w.claim()
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_enrichment SET attempts=3,lease_until=now()-interval '1 second' WHERE attachment='enrich-audio'")
    assert w.claim() is None
    with s.pool.connection() as c:
        row=c.execute("SELECT state,error_code,lease_token FROM attachment_enrichment WHERE attachment='enrich-audio'").fetchone()
    assert row=={'state':'failed','error_code':'retry_exhausted','lease_token':None}
    w.config['profiles']['audio']['model']='changed-fixture'
    assert w.claim() is not None
    assert not w.finish(job,normalize_result({'text':'obsolete'}))


def test_source_truncation_persists_after_enrichment_normalization(seeded,tmp_path):
    s,_,_=seeded;add_audio(s);w=worker(s,tmp_path)
    with s.pool.connection() as c:c.execute('TRUNCATE enrichment_budget')
    row,_=w.claim();row['source_truncated']=True
    result=normalize_result({'text':'partial','metadata':{'duration_seconds':10.0,'transcoded':True,
        'transcode_lossy':True,'transcode_format':'mp3','audio_truncated':False}},audio=True)
    assert w.finish(row,result)
    with s.pool.connection() as c:
        saved=c.execute("SELECT truncated,metadata FROM attachment_enrichment WHERE attachment='enrich-audio'").fetchone()
    assert saved['truncated'] and saved['metadata']['source_truncated']
    assert saved['metadata']['transcoded'] and saved['metadata']['duration_seconds']==10.0


def test_document_type_is_bounded_derived_metadata():
    from messaging_service.enrichment_providers import normalize_result
    first=normalize_result({'title':'Document','document_type':'contract'})
    assert first['metadata']['document_type']=='contract'
    assert normalize_result(first)['metadata']['document_type']=='contract'
    assert normalize_result({})['metadata']['document_type']=='unknown'
    with pytest.raises(ModelError):normalize_result({'document_type':'x'*81})


def test_cpu_local_asr_accepts_a_bounded_longer_deadline(tmp_path):
    local={'provider':'faster_whisper','model':str(tmp_path),'timeout_seconds':3600}
    assert validate_profile(local,'audio')['timeout_seconds']==3600
    with pytest.raises(ValueError,match='invalid_model_limit'):
        validate_profile({**local,'timeout_seconds':3601},'audio')


def test_cloud_asr_keeps_its_shorter_deadline_limit(tmp_path):
    cloud={'provider':'openai','model':'fixture-model','allow_cloud':True,
           'api_key_file':str(tmp_path/'unused-key'),'timeout_seconds':600}
    assert validate_profile(cloud,'audio')['timeout_seconds']==600
    with pytest.raises(ValueError,match='invalid_model_limit'):
        validate_profile({**cloud,'timeout_seconds':601},'audio')


def test_large_transcript_publishes_via_real_worker_and_keeps_read_timeout(seeded,tmp_path):
    s,_,_=seeded;add_audio(s);w=worker(s,tmp_path)
    with s.pool.connection() as c:c.execute('TRUNCATE enrichment_budget')
    row,_=w.claim()
    content=' '.join('voice%06d' % i for i in range(80000))+' transcript-tail'
    assert 900000<len(content)<1000000
    assert w.finish(row,normalize_result({'text':content},audio=True))
    with s.pool.connection() as c:
        assert c.execute("SHOW statement_timeout").fetchone()['statement_timeout']=='5s'
        saved=c.execute("SELECT state,length(text_content) AS n FROM attachment_enrichment WHERE attachment='enrich-audio'").fetchone()
        assert saved=={'state':'ready','n':len(content)}
        assert c.execute("SELECT max(end_offset) AS n FROM attachment_enrichment_chunks WHERE attachment='enrich-audio'").fetchone()['n']==len(content)


@pytest.fixture
def stop_state():
    import messaging_service.enrichment as module
    module.STOP.clear()
    yield module
    module.STOP.clear()


def local_worker(s,tmp_path,processor=None):
    raw={'provider':'faster_whisper','model':str(tmp_path),'timeout_seconds':1800}
    cfg={'accounts':['telegram-demo'],'max_jobs_per_day':20,'profiles':{'audio':validate_profile(raw,'audio')}}
    return EnrichmentWorker(s.pool.conninfo,s.files,cfg,**({'processor':processor} if processor else {}))


# spec: ANYM-310
def test_stop_during_long_local_child_requeues_job_and_frees_the_slot(seeded,tmp_path,monkeypatch,stop_state):
    import os,subprocess,sys,threading,time
    module=stop_state
    s,_,_=seeded;add_audio(s);w=local_worker(s,tmp_path)
    with s.pool.connection() as c:c.execute('TRUNCATE enrichment_budget')
    real=subprocess.Popen;children=[]
    def long_child(args,**kwargs):
        child=real([sys.executable,'-c','import time;time.sleep(600)'],**kwargs);children.append(child);return child
    monkeypatch.setattr(module.subprocess,'Popen',long_child)
    def stop_when_running():
        while not children:time.sleep(0.05)
        time.sleep(0.2);module.request_stop()
    threading.Thread(target=stop_when_running,daemon=True).start()
    started=time.monotonic();w.run();elapsed=time.monotonic()-started
    assert elapsed<30 and children[0].poll() is not None
    with pytest.raises(ProcessLookupError):os.killpg(children[0].pid,0)
    with s.pool.connection() as c:
        row=c.execute("SELECT state,error_code,attempts,lease_token,lease_until FROM attachment_enrichment WHERE attachment='enrich-audio'").fetchone()
        assert row=={'state':'queued','error_code':'worker_stopping','attempts':0,'lease_token':None,'lease_until':None}
        assert c.execute("SELECT lease_until<=clock_timestamp() AS free FROM processing_resource_leases WHERE resource='local-heavy'").fetchone()['free']
        assert c.execute("SELECT enabled FROM processing_profiles WHERE account='telegram-demo' AND kind='audio'").fetchone()['enabled'] is False
        # The attempt is returned, the spend stays counted.
        assert c.execute('SELECT attempts FROM enrichment_budget WHERE day=current_date').fetchone()['attempts']==1
        assert c.execute('SELECT sum(attempts) n FROM processing_budget WHERE day=current_date').fetchone()['n']==1
        assert c.execute("SELECT count(*) n FROM events WHERE kind='file.processing.failed'").fetchone()['n']==0
    module.STOP.clear()
    other=local_worker(s,tmp_path).claim()
    assert other is not None and other[0]['attachment']=='enrich-audio'
    other[0]['_resource_lease'].close()


# spec: ANYM-310
def test_interrupted_third_attempt_stays_claimable_and_a_late_write_is_fenced(seeded,tmp_path,stop_state):
    module=stop_state;seen=[]
    def stopping(files,row,profile):
        seen.append(row);raise module.WorkerStopping()
    s,_,_=seeded;add_audio(s);w=local_worker(s,tmp_path,stopping)
    with s.pool.connection() as c:c.execute('TRUNCATE enrichment_budget')
    job,_=w.claim();w.finish(job,error='model_timeout')
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_enrichment SET attempts=2,next_due=clock_timestamp() WHERE attachment='enrich-audio'")
    assert w.once() and seen[0]['attempts']==3
    with s.pool.connection() as c:
        row=c.execute("SELECT state,error_code,attempts FROM attachment_enrichment WHERE attachment='enrich-audio'").fetchone()
        assert row=={'state':'queued','error_code':'worker_stopping','attempts':2}
        assert c.execute('SELECT attempts FROM enrichment_budget WHERE day=current_date').fetchone()['attempts']==2
        assert c.execute('SELECT sum(attempts) n FROM processing_budget WHERE day=current_date').fetchone()['n']==2
    assert not w.finish(seen[0],normalize_result({'text':'late'},audio=True))
    with s.pool.connection() as c:
        assert c.execute("SELECT state FROM attachment_enrichment WHERE attachment='enrich-audio'").fetchone()['state']=='queued'
    again,_=w.claim();assert again['attempts']==3 and again['lease_token']!=seen[0]['lease_token']
    again['_resource_lease'].close()


# spec: ANYM-310
def test_stop_requested_before_the_child_starts_spawns_nothing(tmp_path,monkeypatch,stop_state):
    module=stop_state
    data=b'safe-original';sha=hashlib.sha256(data).hexdigest();(tmp_path/sha).write_bytes(data)
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**k:pytest.fail('child spawned while stopping'))
    module.STOP.set()
    with pytest.raises(module.WorkerStopping):
        module.process(tmp_path,{'source_digest':sha,'bytes':len(data),'kind':'image','mime':'image/png'},profile(tmp_path))


# spec: ANYM-310
def test_shutdown_unpublish_is_one_statement_whatever_the_account_count(seeded,tmp_path,monkeypatch):
    s,_,_=seeded
    w=local_worker(s,tmp_path);w.config['accounts']=['telegram-demo','whatsapp-demo-1']
    w.publish_profiles()
    statements=[]
    real=w.connection
    class Recording:
        def __init__(self,c):self.c=c
        def __enter__(self):self.c.__enter__();return self
        def __exit__(self,*exc):return self.c.__exit__(*exc)
        def execute(self,*a,**k):statements.append(a[0]);return self.c.execute(*a,**k)
        def cursor(self,*a,**k):
            cursor=self.c.cursor(*a,**k);original=cursor.executemany
            def many(query,params,**kw):
                statements.extend([query]*len(list(params)));return original(query,params,**kw)
            cursor.executemany=many;return cursor
    monkeypatch.setattr(w,'connection',lambda:Recording(real()))
    w.unpublish_profiles()
    assert len(statements)==1
    with s.pool.connection() as c:
        rows=c.execute("SELECT enabled FROM processing_profiles WHERE kind='audio'").fetchall()
        assert len(rows)==2 and not any(r['enabled'] for r in rows)


def stopping_worker(s,tmp_path,module):
    def stopping(files,row,profile):raise module.WorkerStopping()
    w=local_worker(s,tmp_path,stopping)
    with s.pool.connection() as c:c.execute('TRUNCATE enrichment_budget')
    return w


# spec: ANYM-310
def test_requeue_retries_a_cancelled_statement_and_still_releases_the_slot(seeded,tmp_path,monkeypatch,stop_state):
    import psycopg
    module=stop_state
    s,_,_=seeded;add_audio(s);w=stopping_worker(s,tmp_path,module)
    real=w._requeue;calls=[]
    def flaky(row):
        calls.append(1)
        if len(calls)==1:raise psycopg.errors.QueryCanceled('synthetic lock wait')
        return real(row)
    monkeypatch.setattr(w,'_requeue',flaky);monkeypatch.setattr(module.time,'sleep',lambda *_:None)
    assert w.once() and len(calls)==2
    with s.pool.connection() as c:
        assert c.execute("SELECT state,attempts FROM attachment_enrichment WHERE attachment='enrich-audio'").fetchone()=={'state':'queued','attempts':0}
        assert c.execute("SELECT lease_until<=clock_timestamp() AS free FROM processing_resource_leases WHERE resource='local-heavy'").fetchone()['free']


# spec: ANYM-310
def test_profile_is_unpublished_even_when_the_requeue_keeps_failing(seeded,tmp_path,monkeypatch,stop_state):
    import psycopg
    module=stop_state
    s,_,_=seeded;add_audio(s);w=stopping_worker(s,tmp_path,module)
    def broken(row):raise psycopg.errors.QueryCanceled('synthetic lock wait')
    monkeypatch.setattr(w,'_requeue',broken);monkeypatch.setattr(module,'REQUEUE_SECONDS',0)
    with pytest.raises(psycopg.errors.QueryCanceled):w.run()
    with s.pool.connection() as c:
        assert c.execute("SELECT enabled FROM processing_profiles WHERE account='telegram-demo' AND kind='audio'").fetchone()['enabled'] is False
        # The failed requeue left the DB lease; keep it from blocking later tests.
        c.execute("UPDATE processing_resource_leases SET lease_until=clock_timestamp() WHERE resource='local-heavy'")
