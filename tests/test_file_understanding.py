"""Synthetic understanding fixtures preserve retained source detail and bounded coverage."""
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def seeded(seeded):
    # The archive fixture truncates account-scoped counters; this legacy shared
    # day counter has no account FK and deliberately survives normal reseeding.
    with seeded[0].pool.connection() as c:c.execute('TRUNCATE enrichment_budget')
    return seeded


def module():
    assert importlib.util.find_spec('messaging_service.file_understanding') is not None, 'understanding worker is missing'
    return importlib.import_module('messaging_service.file_understanding')


def local_profile(kind='audio', **values):
    return module().understanding_profile({'provider':'ollama','model':'synthetic-local',
        'local_only_daemon':True,**values},kind)


def test_understanding_profiles_require_local_assertion_and_explicit_cloud():
    u=module()
    with pytest.raises(ValueError,match='local_only_daemon_assertion_required'):
        u.understanding_profile({'provider':'ollama','model':'fixture'},'audio')
    with pytest.raises(ValueError,match='cloud_processing_not_enabled'):
        u.understanding_profile({'provider':'openai','model':'fixture'},'document')
    with pytest.raises(ValueError,match='understanding_text_or_vision_profile_required'):
        u.understanding_profile({'provider':'faster_whisper','model':'/synthetic/model'},'audio')
    assert local_profile()['provider']=='ollama'


def test_audio_summary_uses_existing_transcript_without_original_or_retranscription():
    u=module();requests=[]
    row={'kind':'audio','source_text':'Подробная расшифровка: счёт 871, доставка 22 октября.',
         'source_truncated':False,'source_metadata':{'segments':[{'start':0,'end':5,'text':'слова'}]}}
    def respond(request):
        requests.append(request)
        body=json.loads(request.content)
        assert '/api/chat' in request.url.path
        assert row['source_text'] in body['messages'][0]['content']
        assert body['messages'][0]['images']==[]
        return httpx.Response(200,json={'message':{'content':json.dumps({
            'title':'Доставка заказа 871','summary':'Обсуждаются счёт 871 и доставка 22 октября.',
            'text':'MODEL MUST NOT REPLACE THE TRANSCRIPT'})}})
    result=u.infer_understanding(local_profile(),row,transport=httpx.MockTransport(respond))
    assert result['text']=='' and result['title']=='Доставка заказа 871'
    assert result['coverage']['mode']=='transcript_summary'
    assert result['coverage']['complete'] is True
    assert result['coverage']['timestamps_available'] is True
    assert row['source_text'].startswith('Подробная расшифровка') and len(requests)==1


def test_document_summary_input_cap_reports_partial_coverage():
    u=module();row={'kind':'document','source_text':'0123456789 detail',
        'source_truncated':False,'source_metadata':{'complete':True}}
    def respond(request):
        content=json.loads(request.content)['messages'][0]['content']
        assert '01234' in content and '56789 detail' not in content
        return httpx.Response(200,json={'message':{'content':json.dumps({'title':'Table','summary':'A bounded table excerpt.'})}})
    result=u.infer_understanding(local_profile('document',max_text_chars=5),row,
        transport=httpx.MockTransport(respond))
    assert result['truncated'] is True
    assert result['coverage']=={'mode':'extracted_text_summary','complete':False,
        'source':'attachment_text','retained_chars':17,'input_chars':5}


def test_empty_or_whitespace_model_summary_is_not_ready():
    u=module()
    for value in ({'title':'','summary':'something'}, {'title':'Document','summary':'   '}):
        with pytest.raises(u.ModelError,match='understanding_description_empty'):
            u.description_result(value,{'mode':'image_description','complete':True})


def test_video_sampling_times_are_bounded_and_never_claim_full_coverage():
    u=module()
    assert u.sample_times(20,4)==[0.0,5.0,10.0,15.0]
    assert u.sample_times(.1,4)==[0.0,.025,.05,.075]
    for duration,count in [(float('nan'),4),(float('inf'),4),(0,4),(5,0),(5,9)]:
        with pytest.raises(u.ModelError,match='invalid_video'):
            u.sample_times(duration,count)


def test_source_artifact_hash_binds_detail_and_coverage_not_filename():
    u=module();row={'kind':'audio','source_digest':'a'*64,'source_revision':1,
        'source_text':'complete transcript','source_truncated':False,'source_metadata':{'segments':[]}}
    first=u.source_artifact_hash(row)
    assert len(first)==64 and first==u.source_artifact_hash({**row,'filename':'renamed'})
    assert first!=u.source_artifact_hash({**row,'source_text':'changed transcript'})
    assert first!=u.source_artifact_hash({**row,'source_truncated':True})


def test_understanding_migration_seeds_explicit_not_configured_without_models(seeded):
    s,_,_=seeded
    with s.pool.connection() as c:
        assert c.execute("SELECT to_regclass('attachment_understanding') name").fetchone()['name'] is not None
        rows=c.execute('SELECT state,title,summary FROM attachment_understanding').fetchall()
    assert rows and all(r=={'state':'not_configured','title':'','summary':''} for r in rows)


def add_source(s, kind='audio', name='understand-fixture'):
    data=b'synthetic-original';digest=hashlib.sha256(data).hexdigest();(s.files/digest).write_bytes(data)
    mime={'audio':'audio/wav','document':'application/pdf','image':'image/png','video':'video/mp4'}[kind]
    with s.pool.connection() as c:
        c.execute('INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES(%s,%s,%s,%s,%s,%s)',
            (name,'demo-tg-2',digest,name,mime,len(data)))
        if kind=='audio':
            c.execute("UPDATE attachment_enrichment SET kind='audio',state='ready',text_content=%s,metadata=%s WHERE attachment=%s",
                ('Полная расшифровка встречи: решение сохранить исходный текст.',
                 json.dumps({'segments':[{'start':0,'end':5,'text':'Полная расшифровка встречи'}]}),name))
        if kind=='document':
            c.execute("UPDATE attachment_text SET state='ready',text_content=%s,extraction_metadata=%s WHERE attachment=%s",
                ('Таблица: строка 1; сумма 812; примечание: оплата.',json.dumps({'complete':True}),name))
    return digest


def make_worker(s, **values):
    u=module();kind=values.pop('kind','audio')
    config={'accounts':['telegram-demo'],'max_jobs_per_day':20,'profiles':{kind:local_profile(kind)},**values}
    return u.FileUnderstandingWorker(s.pool.conninfo,s.files,config,processor=lambda *_:{
        'title':'Обсуждение хранения','summary':'Встреча об исходном тексте.',
        'coverage':{'mode':'transcript_summary' if kind=='audio' else 'extracted_text_summary','complete':True}})


def test_summary_publication_preserves_full_transcript_and_works_without_original(seeded):
    s,_,_=seeded;digest=add_source(s);(s.files/digest).unlink()
    w=make_worker(s)
    assert w.once()
    with s.pool.connection() as c:
        description=c.execute("SELECT * FROM attachment_understanding WHERE attachment='understand-fixture'").fetchone()
        transcript=c.execute("SELECT text_content,metadata FROM attachment_enrichment WHERE attachment='understand-fixture'").fetchone()
    assert description['state']=='ready' and description['title']=='Обсуждение хранения'
    assert description['text_content']=='' and transcript['text_content'].startswith('Полная расшифровка встречи')
    assert transcript['metadata']['segments'][0]['start']==0
    assert w.claim() is None


def test_changed_source_detail_fences_leased_summary_and_old_ready_metadata(seeded):
    s,_,_=seeded;add_source(s);w=make_worker(s)
    row,_=w.claim()
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_enrichment SET text_content='Новая расшифровка' WHERE attachment=%s",(row['attachment'],))
    assert w.finish(row,{'title':'stale','summary':'stale result','coverage':{}}) is False
    assert w.once()
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_enrichment SET truncated=true WHERE attachment=%s",(row['attachment'],))
        result=c.execute('SELECT state,title,summary FROM attachment_understanding WHERE attachment=%s',(row['attachment'],)).fetchone()
    assert result=={'state':'not_configured','title':'','summary':''}


def test_understanding_respects_shared_heavy_slot_without_consuming_budget(seeded,tmp_path):
    from uuid import uuid4
    from messaging_service import resource_admission
    s,_,_=seeded;add_source(s);w=make_worker(s)
    with w.connection() as c:
        lease=resource_admission.acquire(c,w.lock_directory,'enrichment','understand-fixture',uuid4(),600)
    try:
        assert w.claim() is None
        with w.connection() as c:
            assert c.execute('SELECT coalesce(sum(attempts),0) n FROM enrichment_budget WHERE day=current_date').fetchone()['n']==0
    finally:
        with w.connection() as c:resource_admission.release(c,lease)
        lease.close()


def test_failed_summary_attempts_have_shared_daily_cap_and_three_attempt_limit(seeded):
    s,_,_=seeded;add_source(s);w=make_worker(s,max_jobs_per_day=2)
    for _ in range(2):
        row,_=w.claim();assert w.finish(row,error='model_request_failed')
        with w.connection() as c:c.execute('UPDATE attachment_understanding SET next_due=clock_timestamp()')
    assert w.claim() is None
    w.config['max_jobs_per_day']=20
    row,_=w.claim();assert row['attempts']==3;assert w.finish(row,error='model_request_failed')
    with w.connection() as c:c.execute('UPDATE attachment_understanding SET next_due=clock_timestamp()')
    assert w.claim() is None


def test_profiles_are_separate_from_asr_and_changed_profile_requeues_summary(seeded):
    s,_,_=seeded;add_source(s);w=make_worker(s)
    w.publish_profiles();assert w.once()
    with w.connection() as c:
        assert c.execute("SELECT count(*) n FROM processing_profiles WHERE kind='audio'").fetchone()['n']==0
    w.config['profiles']['audio']['model']='different-local-model';w.publish_profiles()
    with w.connection() as c:
        result=c.execute("SELECT state,title FROM attachment_understanding WHERE attachment='understand-fixture'").fetchone()
    assert result=={'state':'ready','title':'Обсуждение хранения'}
    assert w.once()


def test_video_limits_are_separate_from_text_summary_and_cannot_be_unbounded():
    module();profile=local_profile('video',max_input_bytes=128*1024**2,video_frames=4,video_frame_edge=384,video_max_seconds=300)
    assert profile['max_input_bytes']==128*1024**2 and profile['video_frames']==4
    for values in ({'video_frames':9},{'video_frame_edge':4096},{'max_input_bytes':2*1024**3},
                   {'video_max_seconds':float('inf')}):
        with pytest.raises(ValueError):local_profile('video',**values)
    with pytest.raises(ValueError):local_profile('audio',video_frames=2)


def test_video_sampler_rejects_playlists_before_inference(tmp_path):
    u=module();playlist=tmp_path/'original'
    playlist.write_text('#EXTM3U\n#EXTINF:10,\nhttps://private.invalid/video.ts\n')
    with pytest.raises(u.ModelError,match='invalid_video'):
        u.sample_video(playlist,tmp_path,local_profile('video'))


def test_real_synthetic_video_sampling_has_explicit_partial_coverage(tmp_path):
    import subprocess
    import shutil
    if shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None:
        pytest.skip('native video decoder is verified in the worker image')
    u=module();source=tmp_path/'synthetic.mp4'
    subprocess.run(['ffmpeg','-v','error','-nostdin','-f','lavfi','-i',
        'color=c=blue:s=64x48:d=2:r=4','-an','-c:v','mpeg4',str(source)],check=True,timeout=20)
    profile=local_profile('video',video_frames=4,video_frame_edge=64,video_max_seconds=10)
    contact,coverage=u.sample_video(source,tmp_path,profile)
    assert contact.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    assert coverage=={'mode':'sampled_frames','complete':False,'sampled_frames':4,
        'sampled_timestamps_seconds':[0.0,0.5,1.0,1.5],'duration_seconds':2.0,'audio_transcribed':False}


def test_isolated_summary_child_drops_database_and_provider_environment(tmp_path,monkeypatch):
    u=module();captured={}
    monkeypatch.setenv('DATABASE_URL','synthetic-private-dsn')
    monkeypatch.setenv('TELEGRAM_SESSION','synthetic-session')
    row={'kind':'audio','source_text':'Retained detailed transcript.',
        'source_metadata':{'segments':[{'start':0,'end':1,'text':'SYNTHETIC SEGMENT CANARY'}],
            'complete':True,'private_observation':'UNNEEDED OBSERVATION'},'source_truncated':False}
    class Child:
        pid=912345;returncode=0
        def __init__(self,command,**kwargs):
            captured.update(command=command,**kwargs)
            kwargs['stdout'].write(json.dumps({'title':'Transcript','summary':'The recording discusses retention.',
                'text':'','coverage':{'mode':'transcript_summary','complete':True}}).encode())
        def communicate(self,payload,timeout):captured.update(payload=json.loads(payload),timeout=timeout)
    monkeypatch.setattr(u.subprocess,'Popen',Child)
    monkeypatch.setattr(u,'kill_group',lambda child:None)
    value=u.process(tmp_path,row,local_profile())
    assert value['title']=='Transcript'
    assert 'DATABASE_URL' not in captured['env'] and 'TELEGRAM_SESSION' not in captured['env']
    assert '-I' in captured['command'] and captured['close_fds'] is True and captured['start_new_session'] is True
    assert captured['payload']['source_text']==row['source_text']
    assert captured['payload']['source_metadata']=={'complete':True,'segments':[{}]}


def test_forged_coverage_cannot_make_partial_source_evictable(seeded):
    s,_,_=seeded;add_source(s);w=make_worker(s)
    row,_=w.claim()
    with pytest.raises(module().ModelError,match='invalid_understanding_coverage'):
        w.finish(row,{'title':'A title','summary':'A summary','coverage':{'mode':'sampled_frames','complete':True}})
    assert w.finish(row,error='invalid_understanding_coverage')


def test_configuration_can_observe_not_configured_without_enabling_a_model(tmp_path):
    u=module();path=tmp_path/'profiles.json'
    path.write_text(json.dumps({'enabled':True,'accounts':['synthetic'],'profiles':{},'max_jobs_per_day':2}))
    path.chmod(0o600)
    assert u.configuration(path)['profiles']=={}
    for raw in ({'enabled':True,'accounts':['synthetic'],'profiles':{},'max_jobs_per_day':True},
            {'enabled':True,'accounts':['synthetic'],'profiles':{},'account_max_jobs_per_day':{'foreign':2}}):
        path.write_text(json.dumps(raw))
        with pytest.raises(ValueError,match='invalid_understanding_budget'):u.configuration(path)
    path.chmod(0o644)
    with pytest.raises(ValueError,match='private_config_required'):u.configuration(path)


def test_understanding_role_cannot_replace_transcripts_or_access_agent_keys(seeded):
    import psycopg
    s,_,_=seeded;add_source(s)
    role=Path(__file__).resolve().parents[1]/'deploy/file_understanding_role.sql'
    assert role.is_file(), 'a dedicated understanding role is required'
    with s.pool.connection() as c:c.execute(role.read_text())
    w=make_worker(s)
    old_connection=w.connection
    def connection():
        c=old_connection();c.execute('SET ROLE messaging_file_understanding');c.commit();return c
    w.connection=connection
    w.publish_profiles();assert w.once()
    with w.connection() as c:
        assert c.execute("SELECT state FROM attachment_understanding WHERE attachment='understand-fixture'").fetchone()['state']=='ready'
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with w.connection() as c:c.execute("UPDATE attachment_enrichment SET text_content='replaced'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with w.connection() as c:c.execute('SELECT * FROM api_keys')


def test_document_worker_preserves_complete_cells_and_page_metadata(seeded):
    s,_,_=seeded;add_source(s,'document');w=make_worker(s,kind='document')
    assert w.once()
    with w.connection() as c:
        row=c.execute("SELECT text_content,extraction_metadata FROM attachment_text WHERE attachment='understand-fixture'").fetchone()
        description=c.execute("SELECT coverage FROM attachment_understanding WHERE attachment='understand-fixture'").fetchone()
    assert 'сумма 812' in row['text_content'] and row['extraction_metadata']['complete'] is True
    assert description['coverage']['source']=='attachment_text'


def test_video_profile_does_not_disclose_installed_paths_in_publication(seeded):
    s,_,_=seeded;w=make_worker(s,kind='video');w.config['profiles']['video']['model']='/private/synthetic-model'
    w.publish_profiles()
    with w.connection() as c:
        row=c.execute('SELECT model,profile_hash FROM file_understanding_profiles').fetchone()
    assert row['model']=='configured_model' and len(row['profile_hash'])==64


def test_continuous_audio_backlog_does_not_starve_video_descriptions(seeded):
    s,_,_=seeded;add_source(s,'audio','first-audio');add_source(s,'audio','second-audio');add_source(s,'video','video')
    w=make_worker(s);w.config['profiles']['video']=local_profile('video')
    row,_=w.claim();assert row['kind']=='audio'
    assert w.finish(row,{'title':'Recording','summary':'A retained transcript.',
        'coverage':{'mode':'transcript_summary','complete':True}})
    row,_=w.claim();assert row['kind']=='video'
    assert w.finish(row,error='synthetic_stop')


def test_existing_image_description_is_reused_without_exposing_original(seeded):
    s,_,_=seeded;digest=add_source(s,'image')
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_enrichment SET kind='image',state='ready',title='Invoice',summary='A photographed invoice.' WHERE attachment='understand-fixture'")
    (s.files/digest).unlink();w=make_worker(s,kind='image')
    row,profile=w.claim()
    result=module().process(s.files,row,profile)
    assert result['title']=='Invoice' and result['coverage']=={'mode':'image_description','complete':True}
    assert w.finish(row,result)


def test_expired_lease_and_changed_source_revision_never_publish_summary(seeded):
    s,_,_=seeded;add_source(s);w=make_worker(s);row,_=w.claim()
    with w.connection() as c:c.execute("UPDATE attachment_understanding SET lease_until=clock_timestamp()-interval '1 second'")
    assert w.finish(row,{'title':'old','summary':'expired','coverage':{'mode':'transcript_summary','complete':True}}) is False
    row,_=w.claim()
    with w.connection() as c:c.execute("UPDATE messages SET revision=revision+1 WHERE id='demo-tg-2'")
    assert w.finish(row,{'title':'old','summary':'edited','coverage':{'mode':'transcript_summary','complete':True}}) is False


def test_waiting_for_asr_does_not_hide_ready_source_behind_candidate_limit(seeded):
    s,_,_=seeded;add_source(s,'audio','z-ready');w=make_worker(s)
    with w.connection() as c:
        c.cursor().executemany('''INSERT INTO attachments(id,message,digest,filename,mime,bytes)
            SELECT %s,message,digest,%s,mime,bytes FROM attachments WHERE id='z-ready' ''',
            [(f'a-waiting-{index:03}',f'waiting-{index}') for index in range(65)])
    claim=w.claim()
    assert claim is not None and claim[0]['attachment']=='z-ready'
    assert w.finish(claim[0],error='synthetic_stop')


# spec: ANYM-396
def test_document_candidate_requires_ready_current_nonblank_extracted_text_before_window(seeded):
    s,_,_=seeded;add_source(s,'document','z-ready');w=make_worker(s,kind='document')
    copies=[f'a-blank-{index:03}' for index in range(65)]+['b-queued','b-stale','b-indexed']
    with s.pool.connection() as c:
        c.cursor().executemany('''INSERT INTO attachments(id,message,digest,filename,mime,bytes)
            SELECT %s,message,digest,%s,mime,bytes FROM attachments WHERE id='z-ready' ''',[(name,name) for name in copies])
        c.execute("UPDATE attachment_text SET state='ready',text_content=%s WHERE attachment LIKE 'a-blank-%%'",('   ',))
        c.execute("UPDATE attachment_text SET text_content='Synthetic queued text.' WHERE attachment='b-queued'")
        c.execute("UPDATE attachment_text SET state='ready',text_content='Synthetic old text.',source_revision=source_revision+1 WHERE attachment='b-stale'")
        c.execute("UPDATE attachment_text SET text_content='Synthetic indexed text.',indexed_at=clock_timestamp() WHERE attachment='b-indexed'")
        assert c.execute("SELECT state FROM attachment_text WHERE attachment='b-indexed'").fetchone()['state']!='ready'
    assert [w.once() for _ in range(3)]==[True,True,False]
    with s.pool.connection() as c:
        rows=c.execute("SELECT attachment FROM attachment_understanding WHERE state='ready' ORDER BY attachment").fetchall()
    assert [r['attachment'] for r in rows]==['b-indexed','z-ready']


def test_partial_asr_metadata_prevents_claiming_complete_description():
    u=module();row={'kind':'audio','source_text':'Partial recording text.',
        'source_truncated':False,'source_metadata':{'audio_truncated':True}}
    def respond(_):
        return httpx.Response(200,json={'message':{'content':json.dumps({'title':'Recording','summary':'Partial recording.'})}})
    result=u.infer_understanding(local_profile(),row,transport=httpx.MockTransport(respond))
    assert result['truncated'] is True and result['coverage']['complete'] is False


def test_evicted_video_keeps_last_good_description_after_profile_change(seeded):
    from uuid import uuid4
    s,_,_=seeded;digest=add_source(s,'video');w=make_worker(s,kind='video')
    row,_=w.claim();old_hash=row['profile_hash']
    assert w.finish(row,{'title':'Видео о доставке','summary':'На выбранных кадрах видна доставка.',
        'coverage':{'mode':'sampled_frames','complete':False,'sampled_frames':4,
            'sampled_timestamps_seconds':[0,5,10,15],'duration_seconds':20,'audio_transcribed':False}})
    eviction=uuid4()
    with w.connection() as c:
        info=s.files.stat()
        c.execute("INSERT INTO attachment_eviction_journal(id,digest,bytes,root_device,root_inode,state,completed_at) VALUES(%s,%s,%s,%s,%s,'completed',clock_timestamp())",
            (eviction,digest,len(b'synthetic-original'),info.st_dev,info.st_ino))
        c.execute("UPDATE attachment_residency SET state='evicted',eviction_id=%s,evicted_at=clock_timestamp() WHERE digest=%s",(eviction,digest))
    (s.files/digest).unlink();w.config['profiles']['video']['model']='different-vision-model';w.publish_profiles()
    with w.connection() as c:
        saved=c.execute('SELECT title,summary,indexed_at FROM attachment_understanding WHERE attachment=%s',(row['attachment'],)).fetchone()
    assert saved['title']=='Видео о доставке' and saved['summary']=='На выбранных кадрах видна доставка.'
    assert saved['indexed_at'] is not None
    with w.connection() as c:
        assert c.execute('SELECT published_profile_hash FROM attachment_understanding WHERE attachment=%s',(row['attachment'],)).fetchone()['published_profile_hash']==old_hash
    assert w.claim() is None


def test_failed_refresh_keeps_last_good_text_and_original_model_provenance(seeded):
    s,_,_=seeded;add_source(s);w=make_worker(s);assert w.once()
    with w.connection() as c:
        old=c.execute('SELECT title,summary,profile_hash,model FROM attachment_understanding WHERE attachment=%s',('understand-fixture',)).fetchone()
    w.config['profiles']['audio']['model']='new-model';w.publish_profiles();row,_=w.claim()
    with w.connection() as c:
        during=c.execute('SELECT title,summary,indexed_at FROM attachment_understanding WHERE attachment=%s',(row['attachment'],)).fetchone()
    assert during['title']==old['title'] and during['summary']==old['summary'] and during['indexed_at'] is not None
    assert w.finish(row,error='model_request_failed')
    with w.connection() as c:
        saved=c.execute('SELECT title,summary,indexed_at,published_profile_hash,published_model FROM attachment_understanding WHERE attachment=%s',(row['attachment'],)).fetchone()
    assert saved['title']==old['title'] and saved['summary']==old['summary'] and saved['indexed_at'] is not None
    assert saved['published_profile_hash']==old['profile_hash'] and saved['published_model']==old['model']


def test_asr_state_and_profile_refresh_preserve_description_until_detail_changes(seeded):
    s,_,_=seeded;add_source(s);w=make_worker(s);assert w.once()
    with w.connection() as c:
        before=c.execute('SELECT * FROM attachment_understanding WHERE attachment=%s',('understand-fixture',)).fetchone()
        c.execute("UPDATE attachment_enrichment SET state='leased',profile_hash=%s,indexed_at=clock_timestamp() WHERE attachment='understand-fixture'",('b'*64,))
        during=c.execute('SELECT title,summary,indexed_at FROM attachment_understanding WHERE attachment=%s',('understand-fixture',)).fetchone()
        assert during['title']==before['title'] and during['summary']==before['summary'] and during['indexed_at'] is not None
        assert module().valid_understanding(c,before)
        c.execute("UPDATE attachment_enrichment SET state='failed' WHERE attachment='understand-fixture'")
        assert module().valid_understanding(c,before)
        c.execute("UPDATE attachment_enrichment SET text_content='Actually changed transcript' WHERE attachment='understand-fixture'")
        after=c.execute('SELECT indexed_at,title FROM attachment_understanding WHERE attachment=%s',('understand-fixture',)).fetchone()
    assert after=={'indexed_at':None,'title':''}


def test_configured_profile_reports_waiting_source_without_consuming_model_attempt(seeded):
    s,_,_=seeded;add_source(s);w=make_worker(s)
    with w.connection() as c:
        c.execute("UPDATE attachment_enrichment SET state='unconfigured',text_content='',metadata='{}' WHERE attachment='understand-fixture'")
    w.publish_profiles()
    with w.connection() as c:
        row=c.execute('SELECT state,error_code,attempts FROM attachment_understanding WHERE attachment=%s',('understand-fixture',)).fetchone()
    assert row=={'state':'queued','error_code':'waiting_for_transcript','attempts':0}
    assert w.claim() is None


def test_unsupported_extraction_is_distinct_from_missing_summary_model(seeded):
    s,_,_=seeded;add_source(s,'document');w=make_worker(s,kind='document')
    with w.connection() as c:
        c.execute("UPDATE attachment_text SET state='unsupported',text_content='',extraction_metadata='{}' WHERE attachment='understand-fixture'")
    w.publish_profiles()
    with w.connection() as c:
        row=c.execute('SELECT state,error_code,attempts FROM attachment_understanding WHERE attachment=%s',('understand-fixture',)).fetchone()
    assert row=={'state':'unsupported','error_code':'source_unsupported','attempts':0}
    assert w.claim() is None


# These cases exercise collector ingestion and actual managed original removal,
# rather than fabricating a missing file or only updating revision with SQL.
from test_archive import archive as archive, event


def retained_original(archive, kind):
    from messaging_service.blobs import BlobWriter
    from psycopg.types.json import Jsonb
    ar,s,_,_=archive
    payload=('Synthetic caption-independent '+kind).encode()
    mime={'audio':'audio/wav','document':'application/pdf','image':'image/png','video':'video/mp4'}[kind]
    descriptor={'provider_id':'retained-asset','filename':'Retained.bin','mime':mime,'bytes':len(payload)}
    original=event(provider_id='retained-caption',media=[descriptor])
    ar.ingest(original);job=ar.claim_media('fixture')
    writer=BlobWriter(s.files,reserve_bytes=0);blob=writer.write([payload]);ar.finish_media(job,blob,writer)
    detail=('Detailed retained source text. '*1200)+' retaineddetailcanary'
    metadata={'complete':True,'segments':[{'start':0,'end':5,'text':'retaineddetailcanary'}]}
    with s.pool.connection() as c:
        if kind=='document':
            c.execute("UPDATE attachment_text SET state='ready',text_content=%s,extraction_metadata=%s,indexed_at=clock_timestamp() WHERE attachment=%s",
                (detail,Jsonb({'complete':True,'page_spans':[{'page':1,'start':0,'end':len(detail)}]}),job['id']))
        elif kind in ('audio','image'):
            c.execute("UPDATE attachment_enrichment SET state='ready',kind=%s,text_content=%s,metadata=%s,title='Image detail',summary='Retained image detail.',indexed_at=clock_timestamp() WHERE attachment=%s",
                (kind,detail,Jsonb(metadata),job['id']))
    w=make_worker(s,kind=kind,accounts=['archive-test']);w.publish_profiles()
    row,_=w.claim()
    mode={'audio':'transcript_summary','document':'extracted_text_summary','image':'image_description','video':'sampled_frames'}[kind]
    coverage={'mode':mode,'complete':kind!='video'}
    if kind=='video':coverage.update(sampled_frames=4,sampled_timestamps_seconds=[0,5,10,15],duration_seconds=20,audio_transcribed=False)
    assert w.finish(row,{'title':'Retained summary','summary':'Describes retainedsummarycanary.', 'coverage':coverage})
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_residency SET last_accessed_at=clock_timestamp()-interval '31 days' WHERE digest=%s",(blob.digest,))
    from messaging_service.file_lifecycle import LifecycleWorker
    assert LifecycleWorker(s).run_once(apply=True)['evicted']==1
    assert not (s.files/blob.digest).exists()
    return original,job['id'],blob,detail,metadata


@pytest.mark.parametrize('kind',['audio','document','image','video'])
@pytest.mark.parametrize('edit',['caption','formatting'])
def test_caption_edit_after_managed_eviction_keeps_detail_description_and_search(archive,kind,edit):
    ar,s,key,_=archive;original,fid,blob,detail,metadata=retained_original(archive,kind)
    with s.pool.connection() as c:
        before=c.execute('SELECT * FROM attachment_understanding WHERE attachment=%s',(fid,)).fetchone()
    change={'body':'Edited caption only'}
    if edit=='formatting':
        from messaging_service.incoming_formatting import telegram
        change={'formatting':telegram(original['body'],[])}
    assert ar.ingest({**original,**change,'updated_at':'2026-10-01T12:01:00Z'})['outcome']=='edited'
    with s.pool.connection() as c:
        after=c.execute('SELECT * FROM attachment_understanding WHERE attachment=%s',(fid,)).fetchone()
        assert after['title']==before['title'] and after['summary']==before['summary']
        assert after['coverage']==before['coverage'] and after['source_artifact_hash']==before['source_artifact_hash']
        assert after['source_revision']==2 and module().valid_understanding(c,after)
        if kind=='document':
            row=c.execute('SELECT * FROM attachment_text WHERE attachment=%s',(fid,)).fetchone()
            assert row['text_content']==detail and row['source_revision']==2 and row['state']=='ready'
            assert row['extraction_metadata']['page_spans'][0]['end']==len(detail)
            assert c.execute('SELECT count(*) n FROM attachment_text_chunks WHERE attachment=%s AND source_revision=2',(fid,)).fetchone()['n']>=2
        elif kind in ('audio','image'):
            row=c.execute('SELECT * FROM attachment_enrichment WHERE attachment=%s',(fid,)).fetchone()
            assert row['text_content']==detail and row['metadata']==metadata and row['source_revision']==2 and row['state']=='ready'
            assert c.execute('SELECT count(*) n FROM attachment_enrichment_chunks WHERE attachment=%s AND source_revision=2',(fid,)).fetchone()['n']>=2
    assert [r['attachment_id'] for r in s.search_files(key,'retainedsummarycanary')['items']]==[fid]
    if kind in ('audio','document','image'):
        assert [r['attachment_id'] for r in s.search_files(key,'retaineddetailcanary')['items']]==[fid]
    if kind=='audio':assert s.get_file_segments(key,fid)['segments']==[{'index':0,**metadata['segments'][0]}]
    assert not (s.files/blob.digest).exists()


@pytest.mark.parametrize('change',['replace','descriptor','delete'])
def test_media_replacement_or_deletion_after_eviction_still_invalidates_derivatives(archive,change):
    ar,s,key,_=archive;original,fid,_,_,_=retained_original(archive,'audio')
    media=[] if change=='delete' else [{**original['media'][0],**(
        {'provider_id':'replacement-asset'} if change=='replace' else {'filename':'Different.bin'})}]
    assert ar.ingest({**original,'media':media,'deleted':change=='delete',
        'updated_at':'2026-10-01T12:01:00Z'})['outcome'] in ('edited','deleted')
    with s.pool.connection() as c:
        assert c.execute('SELECT active FROM attachments WHERE id=%s',(fid,)).fetchone()['active'] is False
        assert c.execute('SELECT text_content,metadata FROM attachment_enrichment WHERE attachment=%s',(fid,)).fetchone()=={'text_content':'','metadata':{}}
        assert c.execute('SELECT title,summary,indexed_at FROM attachment_understanding WHERE attachment=%s',(fid,)).fetchone()=={'title':'','summary':'','indexed_at':None}
    assert s.search_files(key,'retainedsummarycanary')['items']==[]


def test_caption_revision_does_not_change_content_artifact_identity():
    u=module();row={'kind':'audio','source_digest':'a'*64,'source_revision':1,
        'source_text':'retained transcript','source_metadata':{'segments':[]},'source_truncated':False}
    assert u.source_artifact_hash(row)==u.source_artifact_hash({**row,'source_revision':2})


def test_caption_edit_cancels_inflight_refresh_but_keeps_last_good_artifact(archive):
    ar,s,_,_=archive;original,fid,_,_,_=retained_original(archive,'audio')
    w=make_worker(s,accounts=['archive-test']);w.config['profiles']['audio']['model']='new-model'
    w.publish_profiles();row,_=w.claim()
    assert row['attachment']==fid
    assert ar.ingest({**original,'body':'Caption changed during refresh',
        'updated_at':'2026-10-01T12:01:00Z'})['outcome']=='edited'
    assert w.finish(row,{'title':'Stale refresh','summary':'Must not publish',
        'coverage':{'mode':'transcript_summary','complete':True}}) is False
    with s.pool.connection() as c:
        result=c.execute('SELECT * FROM attachment_understanding WHERE attachment=%s',(fid,)).fetchone()
    assert result['source_revision']==2 and result['title']=='Retained summary'
    assert result['lease_token'] is None and result['state']=='queued'


@pytest.mark.parametrize('state',['failed','unsupported','queued'])
@pytest.mark.parametrize('edit',['caption','formatting'])
def test_unchanged_original_retains_unfinished_job_history_on_revision(archive,state,edit):
    from psycopg.types.json import Jsonb
    from messaging_service.incoming_formatting import telegram
    ar,s,_,_=archive;original,fid,_,_,_=retained_original(archive,'audio')
    with s.pool.connection() as c:
        for table in ('attachment_text','attachment_enrichment','attachment_understanding'):
            c.execute('UPDATE '+table+''' SET state=%s,indexed_at=NULL,attempts=3,
                error_code='synthetic_terminal',next_due=clock_timestamp()+interval '2 days'
                WHERE attachment=%s''',(state,fid))
        c.execute('UPDATE attachment_text SET extraction_metadata=%s,display_name=%s WHERE attachment=%s',
            (Jsonb({'retained_failure_context':True}),'Retained parser name',fid))
        for table in ('attachment_enrichment','attachment_understanding'):
            c.execute('UPDATE '+table+' SET profile_hash=%s,provider=%s,model=%s WHERE attachment=%s',
                ('b'*64,'ollama','synthetic-local',fid))
        # Detail changes invalidate descriptions first; establish this waiting
        # job only after its exact retained input has been captured.
        source=module().current_understanding_source(c,fid)
        c.execute('UPDATE attachment_understanding SET state=%s,attempts=3,error_code=%s,next_due=clock_timestamp()+interval \'2 days\',source_artifact_hash=%s WHERE attachment=%s',
            (state,'synthetic_terminal',module().source_artifact_hash(source),fid))
        before={table:c.execute('SELECT * FROM '+table+' WHERE attachment=%s',(fid,)).fetchone()
            for table in ('attachment_text','attachment_enrichment','attachment_understanding')}
    change={'body':'Only a different caption'} if edit=='caption' else {'formatting':telegram(original['body'],[])}
    assert ar.ingest({**original,**change,'updated_at':'2026-10-01T12:01:00Z'})['outcome']=='edited'
    with s.pool.connection() as c:
        for table,old in before.items():
            after=c.execute('SELECT * FROM '+table+' WHERE attachment=%s',(fid,)).fetchone()
            assert after=={**old,'source_revision':2}


@pytest.mark.parametrize('attempt',[1,3])
def test_revision_cancels_summary_lease_without_refunding_attempt_or_daily_budget(archive,attempt):
    ar,s,_,_=archive;original,fid,_,_,_=retained_original(archive,'audio')
    w=make_worker(s,accounts=['archive-test']);w.config['profiles']['audio']['model']='retry-model';w.publish_profiles()
    for index in range(attempt):
        row,_=w.claim()
        if index+1<attempt:
            assert w.finish(row,error='model_request_failed')
            with w.connection() as c:c.execute('UPDATE attachment_understanding SET next_due=clock_timestamp() WHERE attachment=%s',(fid,))
    with s.pool.connection() as c:
        before=c.execute('SELECT * FROM attachment_understanding WHERE attachment=%s',(fid,)).fetchone()
        budget=c.execute('SELECT attempts FROM enrichment_budget WHERE day=current_date').fetchone()['attempts']
    assert ar.ingest({**original,'body':'Caption during model request','updated_at':'2026-10-01T12:01:00Z'})['outcome']=='edited'
    assert not w.finish(row,{'title':'Stale','summary':'A late result must be rejected.',
        'coverage':{'mode':'transcript_summary','complete':True}})
    with s.pool.connection() as c:
        after=c.execute('SELECT * FROM attachment_understanding WHERE attachment=%s',(fid,)).fetchone()
        assert c.execute('SELECT attempts FROM enrichment_budget WHERE day=current_date').fetchone()['attempts']==budget
    assert after['attempts']==attempt and after['profile_hash']==before['profile_hash']
    assert after['source_artifact_hash']==before['source_artifact_hash'] and after['next_due']==before['next_due']
    assert after['lease_token'] is None and after['lease_until'] is None
    assert after['title']=='Retained summary' and after['published_model']==before['published_model']
    if attempt==3:
        assert after['state']=='failed' and after['error_code']=='retry_exhausted'
        assert w.claim() is None
    else:
        assert after['state']=='queued'
        fresh,_=w.claim();assert fresh['attempts']==2 and fresh['source_revision']==2
        assert w.finish(fresh,error='synthetic_stop')


@pytest.mark.parametrize('limit,attempt',[(2,2),(5,3)])
def test_cancelled_extraction_obeys_its_configured_attempt_limit(archive,limit,attempt):
    from uuid import uuid4
    from messaging_service.blobs import BlobWriter
    from messaging_service.file_lifecycle import register_resident_original
    from messaging_service.file_index import FileIndexer
    from messaging_service.file_extract import Extraction
    ar,s,_,_=archive;original,fid,blob,_,_=retained_original(archive,'document')
    # Extraction requires resident bytes; managed absence deliberately pauses it.
    # Re-publish this exact synthetic original before exercising cancellation.
    recovered=BlobWriter(s.files,reserve_bytes=0).write([b'Synthetic caption-independent document'])
    assert recovered.digest==blob.digest and recovered.bytes==blob.bytes
    with s.pool.connection() as c:
        assert register_resident_original(c,recovered.digest,recovered.bytes)
    token=uuid4()
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_text SET state='unsupported' WHERE attachment<>%s",(fid,))
        c.execute("UPDATE attachment_text SET state='leased',attempts=%s,lease_token=%s,lease_until=clock_timestamp()+interval '1 minute' WHERE attachment=%s",
            (attempt,token,fid))
    assert ar.ingest({**original,'body':'Changed caption during extraction','updated_at':'2026-10-01T12:01:00Z'})['outcome']=='edited'
    indexer=FileIndexer(s.pool.conninfo,s.files,max_attempts=limit)
    fresh=indexer.claim()
    with s.pool.connection() as c:
        saved=c.execute('SELECT state,attempts,error_code,lease_token FROM attachment_text WHERE attachment=%s',(fid,)).fetchone()
    if attempt==limit:
        assert fresh is None
        assert saved=={'state':'failed','attempts':attempt,'error_code':'retry_exhausted','lease_token':None}
    else:
        assert fresh is not None and fresh['source_revision']==2 and fresh['attempts']==attempt+1
        assert indexer.finish(fresh,Extraction('ready','Completed within configured parser limit.','synthetic-v1'))=='ready'


# spec: ANYM-230
@pytest.mark.parametrize('kind',['document','audio'])
def test_blank_extracted_text_of_any_whitespace_never_starves_a_ready_document(seeded,kind):
    s,_,_=seeded;space=''.join(c for c in map(chr,range(0x110000)) if c.isspace())  # what str.strip() removes
    table={'document':'attachment_text','audio':'attachment_enrichment'}[kind]
    # 64 blank sources sort ahead of the ready one and would fill the whole claim window (LIMIT 64).
    blanks=['a-blank-%02d'%n for n in range(64)]
    for n,name in enumerate(blanks):
        add_source(s,kind,name)
        with s.pool.connection() as c:
            c.execute('UPDATE '+table+' SET text_content=%s WHERE attachment=%s',(space[n%len(space)]*3+'\n\t',name))
    add_source(s,kind,'z-ready-source');w=make_worker(s,kind=kind)
    claimed=w.claim()
    assert claimed is not None and claimed[0]['attachment']=='z-ready-source'
    # Each whitespace character alone is blank to the SQL trim too, not only the ones the window above needs.
    u=module()
    with s.pool.connection() as c:
        for ch in space:
            assert c.execute('SELECT length(btrim(%s,'+u.BLANK+')) n',(ch*2,)).fetchone()['n']==0,hex(ord(ch))
        assert c.execute('SELECT btrim(%s,'+u.BLANK+') t',(space+'x'+space,)).fetchone()['t']=='x'
