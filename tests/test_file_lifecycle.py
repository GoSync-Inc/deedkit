"""Synthetic original residency, crash replay and bounded rehydration contracts."""
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event

import pytest

from messaging_service.media_retry import MEDIA_MAX_ATTEMPTS

from test_archive import archive as archive, event, finished_or_waiting


def lifecycle():
    try:
        return import_module('messaging_service.file_lifecycle')
    except ModuleNotFoundError:
        pytest.fail('safe original lifecycle is not implemented')


def stored(archive, *, mid='lifecycle-1', payload=b'Synthetic retained contract', mime='text/plain'):
    from messaging_service.blobs import BlobWriter
    ar,s,key,_=archive
    ar.ingest(event(provider_id=mid,media=[{'provider_id':'asset-'+mid,
        'filename':'Synthetic.txt','mime':mime,'bytes':len(payload)}]))
    job=ar.claim_media('fixture')
    writer=BlobWriter(s.files,reserve_bytes=0)
    blob=writer.write([payload]);ar.finish_media(job,blob,writer)
    return job['id'],blob


def test_new_original_starts_full_thirty_day_grace_period(archive):
    _,s,_,_=archive
    fid,blob=stored(archive)
    api=lifecycle()
    with s.pool.connection() as c:
        api.touch_original(c,blob.digest,blob.bytes)
        row=c.execute('''SELECT *,last_accessed_at>clock_timestamp()-interval '1 minute' AS recent
            FROM attachment_residency WHERE digest=%s''',(blob.digest,)).fetchone()
    assert row['state']=='resident' and row['recent'] and not row['pinned']
    assert api.LifecycleWorker(s).run_once()['evicted']==0
    assert (s.files/blob.digest).read_bytes()==b'Synthetic retained contract'


def test_backup_fence_remains_active_after_connection_closes(archive):
    _,s,_,_=archive;api=lifecycle()
    with s.pool.connection() as c:
        api.begin_backup_fence(c,'synthetic-backup-1')
    with s.pool.connection() as c:
        assert api.backup_fence_active(c)
        api.begin_backup_fence(c,'synthetic-backup-1')
        api.end_backup_fence(c,'synthetic-backup-1')
    with s.pool.connection() as c:
        assert not api.backup_fence_active(c)
        with pytest.raises(ValueError,match='backup_fence_closed'):
            api.begin_backup_fence(c,'synthetic-backup-1')


# spec: ANYM-223
def test_registering_an_original_reads_its_identity_through_an_index_under_the_residency_lock(archive):
    # Regression (production 11-09): every attachment INSERT calls register_resident_original, which checks
    # attachments by digest and bytes while it holds the global residency lock. Without an index that check read
    # the whole table (56,523 rows, 7 ms) under the lock. enable_seqscan=off stands in for production's size: the
    # planner still takes a sequential scan when no index can answer the check.
    _,s,_,_=archive
    _,blob=stored(archive)
    # The backend's pending counters may still hold earlier transactions' scans: compare before and after.
    count="SELECT seq_scan,idx_scan FROM pg_stat_xact_user_tables WHERE relid='public.attachments'::regclass"
    with s.pool.connection() as c:
        c.execute('SET LOCAL enable_seqscan=off')
        before=c.execute(count).fetchone()
        assert lifecycle().register_resident_original(c,blob.digest,blob.bytes)
        after=c.execute(count).fetchone()
        assert after['seq_scan']==before['seq_scan'] and after['idx_scan']>before['idx_scan']
        index=c.execute("SELECT pg_get_indexdef('public.attachment_digest_bytes'::regclass) AS d").fetchone()['d']
    assert index=='CREATE INDEX attachment_digest_bytes ON public.attachments USING btree (digest, bytes)'


# spec: ANYM-223
@pytest.mark.parametrize('shape',['invalid','partial','other_keys','hash','collation','opclass','descending'])
def test_migration_068_refuses_an_unusable_index_of_its_name_and_builds_it_once_dropped(database,tmp_path,shape):
    # IF NOT EXISTS keeps any relation of that name; the ledger may advance only over the index the lookup needs.
    import psycopg
    from test_extend_key import old_schema_store
    with old_schema_store(database,tmp_path,67) as store:
        with store.pool.connection() as c:
            c.execute('CREATE INDEX attachment_digest_bytes ON attachments '+{'invalid':'(digest,bytes)',
                'partial':'(digest,bytes) WHERE active','other_keys':'(digest)','hash':'USING hash (digest)','collation':'(digest COLLATE "C",bytes)',
                'opclass':'(digest text_pattern_ops,bytes)','descending':'(digest DESC,bytes)'}[shape])
            # What an interrupted CREATE INDEX CONCURRENTLY leaves behind.
            if shape=='invalid':
                c.execute("UPDATE pg_index SET indisvalid=false WHERE indexrelid='attachment_digest_bytes'::regclass")
        with pytest.raises(psycopg.errors.RaiseException,match='not a valid btree index'):store.migrate()
        with store.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='67'
            c.execute('DROP INDEX attachment_digest_bytes')
        store.migrate();store.migrate()
        with store.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='84'
            assert c.execute('''SELECT i.indisvalid,pg_get_indexdef(i.indexrelid) AS d FROM pg_index i
                WHERE i.indexrelid='attachment_digest_bytes'::regclass''').fetchone()=={'indisvalid':True,
                'd':'CREATE INDEX attachment_digest_bytes ON public.attachments USING btree (digest, bytes)'}


MESSAGE_INDEX='CREATE INDEX attachment_message ON public.attachments USING btree (message)'


def migration_072():
    from messaging_service import store as store_module
    return Path(store_module.__file__).with_name('migrations')/'072_attachment_message_index.sql'


def seed_attachments(store, messages, *, inactive=0, per_message=1):
    """`messages` synthetic messages with `per_message` attachments each (the first `inactive` rows inactive), about
    41,040 distinct digests as production had on 2026-11-09 (#747's read)."""
    from messaging_service.archive import Archive
    Archive(store).register('shape','telegram','Synthetic shape',include_chats=['1'])
    with store.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='600s'")
        c.execute("INSERT INTO chats(id,account,title,provider_chat) VALUES('chat_shape','shape','S','1')")
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            SELECT 'msg_shape_'||n,'shape','chat_shape','p'||n,'42','',now()-n*interval '1 minute'
            FROM generate_series(1,%s) n''',(messages,))
        c.execute('''INSERT INTO attachments(id,message,digest,filename,mime,bytes,active)
            SELECT 'file_shape_'||n||'_'||k,'msg_shape_'||n,md5('d'||(n*%s+k)%%41040)||md5('e'||(n*%s+k)%%41040),
                'synthetic.bin','application/octet-stream',100+(n*%s+k)%%41040,n>%s
            FROM generate_series(1,%s) n,generate_series(1,%s) k''',
            (per_message,per_message,per_message,inactive,messages,per_message))


# spec: ANYM-852
def test_migration_072_is_a_manual_non_critical_file_that_migrate_expand_still_runs_unattended():
    # As 068: classify refuses an index on a table the file does not create, so the header says manual; the
    # unattended release still admits it as expand.
    from messaging_service import migration_rollout as rollout
    text=migration_072().read_text()
    assert rollout.rollout_class(text)=='manual'
    assert rollout.unattended_class(text)=='expand'
    assert rollout.risk_class(text)==('non-critical',None,None)


# spec: ANYM-852
@pytest.mark.parametrize('shape',['invalid','partial','other_keys','two_keys','hash','collation','opclass',
    'descending','unique','exclusion'])
def test_migration_072_refuses_an_unusable_index_of_its_name_and_builds_it_once_dropped(database,tmp_path,shape):
    # IF NOT EXISTS keeps any relation of that name; the ledger may advance only over an ordinary index. A unique
    # or exclusion one would refuse a message's second attachment.
    import psycopg
    from test_extend_key import old_schema_store
    with old_schema_store(database,tmp_path,71) as store:
        with store.pool.connection() as c:
            if shape=='exclusion':
                c.execute('ALTER TABLE attachments ADD CONSTRAINT attachment_message EXCLUDE USING btree (message WITH =)')
            else:
                c.execute(('CREATE UNIQUE INDEX' if shape=='unique' else 'CREATE INDEX')+' attachment_message ON attachments '+{
                    'invalid':'(message)','partial':'(message) WHERE active','other_keys':'(digest)','two_keys':'(message,id)',
                    'hash':'USING hash (message)','collation':'(message COLLATE "C")','opclass':'(message text_pattern_ops)',
                    'descending':'(message DESC)','unique':'(message)'}[shape])
            # What an interrupted CREATE INDEX CONCURRENTLY leaves behind.
            if shape=='invalid':
                c.execute("UPDATE pg_index SET indisvalid=false WHERE indexrelid='attachment_message'::regclass")
        with pytest.raises(psycopg.errors.RaiseException,match='not a valid btree index on attachments\\(message\\): '
                'DROP INDEX CONCURRENTLY public.attachment_message'):store.migrate()
        with store.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='71'
            assert c.execute('SELECT max(version) AS v FROM archive_migrations').fetchone()['v']==71
            c.execute('ALTER TABLE attachments DROP CONSTRAINT attachment_message' if shape=='exclusion'
                else 'DROP INDEX attachment_message')
        store.migrate();store.migrate()
        with store.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='84'
            assert c.execute('''SELECT i.indisvalid,pg_get_indexdef(i.indexrelid) AS d FROM pg_index i
                WHERE i.indexrelid='attachment_message'::regclass''').fetchone()=={'indisvalid':True,'d':MESSAGE_INDEX}
        seed_attachments(store,1,per_message=2)
        with store.pool.connection() as c:
            assert c.execute("SELECT count(*) AS n FROM attachments WHERE message='msg_shape_1'").fetchone()['n']==2


# spec: ANYM-852
def test_migration_072_builds_on_a_production_sized_table_in_one_expand_transaction(database,tmp_path,record_property):
    # Recorded, never asserted: CI timing does not predict production's margin under rehearsal_limit_ms (068: about
    # 40 ms locally, 0.2-0.3 s left of the 1000 ms limit under production load). migrate_expand's own clock.
    import hashlib
    from messaging_service import store as store_module
    from test_extend_key import old_schema_store
    root=Path(store_module.__file__).parent
    roles={p.name:{'bytes':len(p.read_bytes()),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in sorted(root.glob('*_role.sql'))}
    with old_schema_store(database,tmp_path,71) as store:
        seed_attachments(store,56523,inactive=1220)
        with store.pool.connection() as c:
            assert c.execute('SELECT count(*) AS n FROM attachments').fetchone()['n']==56523
        timing={}
        found=store_module.migrate_expand(store.pool.conninfo,{'base':71,'target':84,'roles':roles},timing=timing)
        record_property('transaction_ms',timing['transaction_ms'])
        assert found=={'schema_version':84}
        with store.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']=='84'
            assert c.execute("SELECT pg_get_indexdef('public.attachment_message'::regclass) AS d").fetchone()['d']==MESSAGE_INDEX


@pytest.fixture(autouse=True)
def clear_lifecycle_state(database):
    with database.pool.connection() as c:
        c.execute('DELETE FROM attachment_cold_copies')
        c.execute('UPDATE attachment_residency SET eviction_id=NULL,state=\'resident\',evicted_at=NULL')
        c.execute('DELETE FROM attachment_eviction_journal')
        c.execute('DELETE FROM attachment_backup_fences')
        c.execute('DELETE FROM attachment_residency')


def processed(archive,*,mid='lifecycle-1',payload=b'Synthetic retained contract',mime='text/plain'):
    from psycopg.types.json import Jsonb
    fid,blob=stored(archive,mid=mid,payload=payload,mime=mime)
    _,s,_,_=archive
    kind='video' if mime.startswith('video/') else 'audio' if mime.startswith('audio/') else 'image' if mime.startswith('image/') else 'document'
    mode={'video':'sampled_frames','image':'image_description','audio':'transcript_summary','document':'extracted_text_summary'}[kind]
    with s.pool.connection() as c:
        if kind=='document':
            c.execute("UPDATE attachment_text SET state='ready',text_content='Full synthetic extracted text',truncated=false,error_code=NULL WHERE attachment=%s",(fid,))
        if kind=='audio':
            c.execute("UPDATE attachment_enrichment SET state='ready',kind='audio',text_content='Full synthetic speech transcript',truncated=false,error_code=NULL WHERE attachment=%s",(fid,))
        coverage={'mode':mode,'complete':kind!='video'}
        if kind=='video':coverage.update(sampled_frames=2,sampled_timestamps_seconds=[0.,2.],duration_seconds=4.,audio_transcribed=False)
        from messaging_service.file_understanding import current_understanding_source,source_artifact_hash
        source=current_understanding_source(c,fid)
        c.execute("""UPDATE attachment_understanding SET state='ready',title='Synthetic contract',summary='Agreement description',
            source_artifact_hash=%s,coverage=%s,truncated=false,profile_hash=%s,published_profile_hash=%s,
            provider='ollama',model='synthetic-model',published_provider='ollama',published_model='synthetic-model',
            indexed_at=clock_timestamp() WHERE attachment=%s""",(source_artifact_hash(source),Jsonb(coverage),'b'*64,'b'*64,fid))
        c.execute("UPDATE attachment_residency SET last_accessed_at=clock_timestamp()-interval '31 days' WHERE digest=%s",(blob.digest,))
    return fid,blob


def cold_copy(s,blob,*,state='verified',bytes=None):
    """A read-back verified R2 copy, as recorded by the cold archiver."""
    with s.pool.connection() as c:
        c.execute('''INSERT INTO attachment_cold_copies(digest,bytes,state,object_key,ciphertext_bytes,ciphertext_sha256,
            recipient_fingerprints,verified_at) VALUES(%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s='verified' THEN clock_timestamp() END)''',
            (blob.digest,blob.bytes if bytes is None else bytes,state,'synthetic/cold/'+blob.digest+'.age',blob.bytes+200,'c'*64,
             ['d'*64,'e'*64] if state=='verified' else [],state))


def test_processed_idle_original_is_only_candidate_until_apply(archive):
    _,s,_,_=archive;fid,blob=processed(archive);api=lifecycle()
    report=api.LifecycleWorker(s).run_once()
    assert report['dry_run'] is True and report['candidates']==1 and report['evicted']==0
    assert (s.files/blob.digest).exists()
    report=api.LifecycleWorker(s).run_once(apply=True)
    assert report['evicted']==1 and report['evicted_bytes']==blob.bytes
    assert not (s.files/blob.digest).exists()
    with s.pool.connection() as c:
        assert api.is_managed_evicted(c,blob.digest,blob.bytes)
        assert c.execute('SELECT text_content FROM attachment_text WHERE attachment=%s',(fid,)).fetchone()['text_content']=='Full synthetic extracted text'
        assert c.execute('SELECT title FROM attachment_understanding WHERE attachment=%s',(fid,)).fetchone()['title']=='Synthetic contract'
        assert c.execute('SELECT count(*) AS n FROM attachments WHERE id=%s',(fid,)).fetchone()['n']==1


@pytest.mark.parametrize('missing',['indexed_at','published_profile_hash','both'])
def test_ready_unpublished_description_does_not_authorize_eviction(archive,missing):
    _,s,key,_=archive;fid,blob=processed(archive);api=lifecycle()
    with s.pool.connection() as c:
        if missing in ('indexed_at','both'):
            c.execute('UPDATE attachment_understanding SET indexed_at=NULL WHERE attachment=%s',(fid,))
        if missing in ('published_profile_hash','both'):
            c.execute('UPDATE attachment_understanding SET published_profile_hash=NULL WHERE attachment=%s',(fid,))
    report=api.LifecycleWorker(s).run_once(apply=True)
    assert report['evicted']==0 and report['reasons']=={'description_incomplete':1}
    assert (s.files/blob.digest).exists()
    with s.pool.connection() as c:
        assert not api.is_managed_evicted(c,blob.digest,blob.bytes)


def test_worker_eviction_keeps_published_description_visible_to_api_and_search(archive):
    _,s,key,_=archive;fid,blob=processed(archive);api=lifecycle()
    # A successful older model publication remains useful after configuration
    # changes; deletion must not require equality with the active profile.
    with s.pool.connection() as c:
        c.execute("""INSERT INTO file_understanding_profiles(account,kind,profile_hash,provider,model,expires_at)
            VALUES('archive-test','document',%s,'ollama','new-synthetic-model',clock_timestamp()+interval '1 hour')""",('c'*64,))
    assert api.LifecycleWorker(s).run_once(apply=True)['evicted']==1
    assert not (s.files/blob.digest).exists()
    metadata=s.get_attachment(key,fid)
    assert metadata['reason']=='original_evicted'
    assert metadata['title']=='Synthetic contract' and metadata['summary']=='Agreement description'
    assert metadata['understanding']['state']=='ready'
    assert metadata['understanding']['profile_current'] is False
    hits=s.search_files(key,query='Agreement')['items']
    assert [row['attachment_id'] for row in hits]==[fid]
    assert hits[0]['summary']=='Agreement description' and 'understanding' in hits[0]['match_sources']
    assert s.get_file_text(key,fid)['text']=='Full synthetic extracted text'


def test_pin_protects_every_occurrence_of_shared_original(archive):
    _,s,_,_=archive;fid,blob=processed(archive)
    second,_=processed(archive,mid='lifecycle-2')
    api=lifecycle()
    with s.pool.connection() as c:
        api.pin_original(c,blob.digest,True)
    assert api.LifecycleWorker(s).run_once(apply=True)['evicted']==0
    assert (s.files/blob.digest).exists()
    with s.pool.connection() as c:
        api.pin_original(c,blob.digest,False)
    assert api.LifecycleWorker(s).run_once(apply=True)['evicted']==1


def test_touch_restarts_idle_window_and_never_unpins(archive):
    _,s,_,_=archive;_,blob=processed(archive);api=lifecycle()
    with s.pool.connection() as c:
        api.pin_original(c,blob.digest,True)
        api.touch_original(c,blob.digest,blob.bytes)
        assert c.execute('SELECT pinned FROM attachment_residency WHERE digest=%s',(blob.digest,)).fetchone()['pinned']
        api.pin_original(c,blob.digest,False)
    assert api.LifecycleWorker(s).run_once(apply=True)['evicted']==0


@pytest.mark.parametrize('blocked', ['unprocessed','partial_text','partial_description','stale_description',
    'active_asr','active_download','pending_request','download_grant','fence'])
def test_incomplete_or_inflight_originals_cannot_be_evicted(archive,blocked):
    _,s,key,_=archive;fid,blob=processed(archive);api=lifecycle()
    with s.pool.connection() as c:
        if blocked=='unprocessed':c.execute("UPDATE attachment_text SET state='queued',text_content='' WHERE attachment=%s",(fid,))
        if blocked=='partial_text':c.execute('UPDATE attachment_text SET truncated=true WHERE attachment=%s',(fid,))
        if blocked=='partial_description':c.execute('UPDATE attachment_understanding SET truncated=true WHERE attachment=%s',(fid,))
        if blocked=='stale_description':c.execute('UPDATE attachment_understanding SET source_revision=source_revision+1 WHERE attachment=%s',(fid,))
        if blocked=='active_asr':c.execute("UPDATE attachment_enrichment SET state='leased',lease_until=now()+interval '1 hour' WHERE attachment=%s",(fid,))
        if blocked=='active_download':c.execute("UPDATE media_jobs SET state='leased',lease_until=now()+interval '1 hour' WHERE id=%s",(fid,))
        if blocked=='pending_request':c.execute("UPDATE media_jobs SET requested_until=now()+interval '1 hour' WHERE id=%s",(fid,))
        if blocked=='fence':api.begin_backup_fence(c,'synthetic-backup-active')
    if blocked=='download_grant':
        s.get_attachment(key,fid)
        with s.pool.connection() as c:c.execute("UPDATE attachment_residency SET last_accessed_at=now()-interval '31 days' WHERE digest=%s",(blob.digest,))
    assert api.LifecycleWorker(s).run_once(apply=True)['evicted']==0
    assert (s.files/blob.digest).exists()


@pytest.mark.parametrize('unsafe',['corrupt','symlink','missing'])
def test_unverified_original_is_not_declared_managed_evicted(archive,unsafe,tmp_path):
    _,s,_,_=archive;_,blob=processed(archive);api=lifecycle();path=s.files/blob.digest
    path.unlink()
    if unsafe=='corrupt':path.write_bytes(b'x'*blob.bytes)
    if unsafe=='symlink':
        target=tmp_path/'important-original';target.write_bytes(b'Synthetic retained contract');path.symlink_to(target)
    assert api.LifecycleWorker(s).run_once(apply=True)['evicted']==0
    with s.pool.connection() as c:assert not api.is_managed_evicted(c,blob.digest,blob.bytes)
    if unsafe=='corrupt':assert path.read_bytes()==b'x'*blob.bytes
    if unsafe=='symlink':assert target.read_bytes()==b'Synthetic retained contract'


def test_eviction_limits_are_real_bounds(archive):
    _,s,_,_=archive;_,one=processed(archive,payload=b'Synthetic one')
    _,two=processed(archive,mid='lifecycle-2',payload=b'Synthetic two')
    api=lifecycle()
    report=api.LifecycleWorker(s).run_once(apply=True,limit=1,max_bytes=one.bytes)
    assert report['evicted']==1 and report['evicted_bytes']<=one.bytes
    assert sum((s.files/b.digest).exists() for b in (one,two))==1
    with pytest.raises(ValueError,match='invalid_lifecycle_limit'):api.LifecycleWorker(s).run_once(limit=0)
    with pytest.raises(ValueError,match='invalid_lifecycle_limit'):api.LifecycleWorker(s).run_once(max_bytes=True)


@pytest.mark.parametrize('blocker',['unprocessed','corrupt','oversized'])
def test_repeated_bounded_passes_visit_later_eligible_originals(archive,blocker):
    _,s,_,_=archive;api=lifecycle()
    fid,older=processed(archive,payload=b'Older synthetic original '*4)
    _,later=processed(archive,mid='lifecycle-later',payload=b'Later synthetic original')
    with s.pool.connection() as c:
        if blocker=='unprocessed':
            c.execute("UPDATE attachment_text SET state='queued',text_content='' WHERE attachment=%s",(fid,))
        c.execute("UPDATE attachment_residency SET last_accessed_at=clock_timestamp()-interval '32 days',updated_at=clock_timestamp()-interval '32 days' WHERE digest=%s",(older.digest,))
        c.execute("UPDATE attachment_residency SET updated_at=clock_timestamp()-interval '31 days' WHERE digest=%s",(later.digest,))
        original_access=c.execute('SELECT last_accessed_at FROM attachment_residency WHERE digest=%s',(older.digest,)).fetchone()['last_accessed_at']
    if blocker=='corrupt':(s.files/older.digest).write_bytes(b'x'*older.bytes)
    budget=later.bytes if blocker=='oversized' else older.bytes
    first=api.LifecycleWorker(s).run_once(apply=True,limit=1,max_bytes=budget)
    assert first['evicted']==0 and first['blocked']==1
    # Recreate the worker to prove traversal survives one-shot process restarts.
    second=api.LifecycleWorker(s).run_once(apply=True,limit=1,max_bytes=budget)
    assert second['evicted']==1
    assert (s.files/older.digest).exists() and not (s.files/later.digest).exists()
    with s.pool.connection() as c:
        row=c.execute('SELECT last_accessed_at,state FROM attachment_residency WHERE digest=%s',(older.digest,)).fetchone()
    assert row['last_accessed_at']==original_access and row['state']=='resident'


def test_preview_does_not_advance_inspection_order(archive):
    _,s,_,_=archive;_,blob=processed(archive);api=lifecycle()
    with s.pool.connection() as c:
        before=c.execute('SELECT updated_at FROM attachment_residency WHERE digest=%s',(blob.digest,)).fetchone()['updated_at']
    assert api.LifecycleWorker(s).run_once()['candidates']==1
    with s.pool.connection() as c:
        after=c.execute('SELECT updated_at FROM attachment_residency WHERE digest=%s',(blob.digest,)).fetchone()['updated_at']
    assert after==before


def test_backup_fence_stops_pass_before_source_traversal(archive,monkeypatch):
    _,s,_,_=archive;_,blob=processed(archive);api=lifecycle();worker=api.LifecycleWorker(s)
    with s.pool.connection() as c:
        api.begin_backup_fence(c,'synthetic-traversal-fence')
        before=c.execute('SELECT updated_at FROM attachment_residency WHERE digest=%s',(blob.digest,)).fetchone()['updated_at']
    def unexpected(*args):pytest.fail('active backup must stop source traversal')
    monkeypatch.setattr(worker,'_eligible',unexpected)
    report=worker.run_once(apply=True)
    assert report['evicted']==0 and report['reasons']=={'backup_active':1}
    with s.pool.connection() as c:
        after=c.execute('SELECT updated_at FROM attachment_residency WHERE digest=%s',(blob.digest,)).fetchone()['updated_at']
    assert before==after


def test_journal_replays_crash_after_unlink_without_losing_derivatives(archive,monkeypatch):
    _,s,_,_=archive;fid,blob=processed(archive);api=lifecycle();worker=api.LifecycleWorker(s)
    original=worker._mark_evicted
    def crash(*args):raise RuntimeError('synthetic_crash_after_unlink')
    monkeypatch.setattr(worker,'_mark_evicted',crash)
    with pytest.raises(RuntimeError,match='synthetic_crash'):worker.run_once(apply=True)
    assert not (s.files/blob.digest).exists()
    with s.pool.connection() as c:
        assert c.execute('SELECT state FROM attachment_residency WHERE digest=%s',(blob.digest,)).fetchone()['state']=='evicting'
        with pytest.raises(ValueError,match='backup_eviction_pending'):api.begin_backup_fence(c,'synthetic-racing-backup')
    monkeypatch.setattr(worker,'_mark_evicted',original)
    assert worker.run_once(apply=True)['evicted']==1
    with s.pool.connection() as c:
        assert api.is_managed_evicted(c,blob.digest,blob.bytes)
        assert c.execute('SELECT state FROM attachment_text WHERE attachment=%s',(fid,)).fetchone()['state']=='ready'
    assert worker.run_once(apply=True)['evicted']==0


# A pass that Compose kills after the stop grace runs no `finally`, rollback or exit handler, so
# these die by a real SIGKILL at the boundary, in a process of their own.
LIFECYCLE_KILL=r'''
import json,os,signal,sys
from pathlib import Path
from messaging_service import file_lifecycle as api
from messaging_service.store import Store
from test_file_lifecycle import die_after_sql
spec=json.loads(os.environ['KILL_SPEC'])
def die(*_,**__):os.kill(os.getpid(),signal.SIGKILL)
def then_die(function):
    def wrapped(*args,**kwargs):
        function(*args,**kwargs);die()
    return wrapped
store=Store(spec['dsn'],Path(spec['files']),'http://localhost')
worker=api.LifecycleWorker(store)
boundary=spec['boundary']
if boundary=='inspected':worker._verified=die
elif boundary=='prepare_uncommitted':worker.store=die_after_sql(store,"SET state='evicting'",die)
elif boundary=='prepared':worker._complete=die
elif boundary=='unlinked_unsynced':api.os.fsync=die
elif boundary=='unlinked':worker._mark_evicted=die
elif boundary=='marked_uncommitted':worker._mark_evicted=then_die(worker._mark_evicted)
elif boundary=='committed':worker._complete=then_die(worker._complete)
else:raise SystemExit('unknown boundary')
worker.run_once(apply=True)
raise SystemExit('the pass ended without reaching its boundary')
'''


def die_after_sql(store,fragment,die):
    """`store` whose connections call `die` right after a statement holding `fragment` ran, its transaction still open."""
    from contextlib import contextmanager
    from types import SimpleNamespace
    class Connection:
        def __init__(self,inner):self.inner=inner
        def __getattr__(self,name):return getattr(self.inner,name)
        def execute(self,query,*args,**kwargs):
            result=self.inner.execute(query,*args,**kwargs)
            if fragment in str(query):die()
            return result
    @contextmanager
    def connection():
        with store.pool.connection() as inner:yield Connection(inner)
    return SimpleNamespace(pool=SimpleNamespace(connection=connection,conninfo=store.pool.conninfo),files=store.files)


def killed(code,s,boundary,**fields):
    """Run `code` in a process of its own until it SIGKILLs itself at `boundary`."""
    spec={'dsn':s.pool.conninfo,'files':str(s.files),'boundary':boundary,**fields}
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,timeout=120,
        env={**os.environ,'KILL_SPEC':json.dumps(spec),'PYTHONPATH':os.pathsep.join(filter(None,(str(Path(__file__).parent),os.environ.get('PYTHONPATH'))))})
    assert result.returncode==-signal.SIGKILL,result.stderr.decode(errors='replace')[-2000:]


def eviction_state(s,digest):
    with s.pool.connection() as c:
        residency=c.execute('SELECT state FROM attachment_residency WHERE digest=%s',(digest,)).fetchone()['state']
        journal=[row['state'] for row in c.execute('SELECT state FROM attachment_eviction_journal WHERE digest=%s ORDER BY created_at,id',(digest,))]
    return residency,journal,(s.files/digest).exists()


# spec: ANYM-525
@pytest.mark.parametrize('boundary,after_kill,evicted',[
    ('inspected',('resident',[],True),1),
    ('prepare_uncommitted',('resident',[],True),1),
    ('prepared',('evicting',['prepared'],True),1),
    ('unlinked_unsynced',('evicting',['prepared'],False),1),
    ('unlinked',('evicting',['prepared'],False),1),
    ('marked_uncommitted',('evicting',['prepared'],False),1),
    ('committed',('evicted',['completed'],False),0)])
def test_a_killed_lifecycle_pass_is_finished_by_the_next_one_at_every_boundary(archive,boundary,after_kill,evicted):
    _,s,_,_=archive;fid,blob=processed(archive);api=lifecycle();worker=api.LifecycleWorker(s)
    killed(LIFECYCLE_KILL,s,boundary)
    assert eviction_state(s,blob.digest)==after_kill
    if after_kill[0]=='evicting':
        # A dry-run restart only reports the intent, and a backup still waits for it.
        before=eviction_state(s,blob.digest)
        assert worker.run_once()['reasons'].get('pending_replay')==1 and eviction_state(s,blob.digest)==before
        with s.pool.connection() as c:
            with pytest.raises(ValueError,match='backup_eviction_pending'):api.begin_backup_fence(c,'synthetic-racing-backup')
    assert worker.run_once(apply=True)['evicted']==evicted
    # One intent, one unlink: nothing was prepared or removed twice, and the derivatives stay.
    assert eviction_state(s,blob.digest)==('evicted',['completed'],False)
    with s.pool.connection() as c:
        assert api.is_managed_evicted(c,blob.digest,blob.bytes)
        assert c.execute('SELECT state FROM attachment_text WHERE attachment=%s',(fid,)).fetchone()['state']=='ready'
    assert worker.run_once(apply=True)['evicted']==0 and eviction_state(s,blob.digest)==('evicted',['completed'],False)


# spec: ANYM-525
def test_an_original_requested_again_after_a_killed_unlink_stays_resident(archive):
    _,s,_,_=archive;_,blob=processed(archive);api=lifecycle();worker=api.LifecycleWorker(s)
    killed(LIFECYCLE_KILL,s,'unlinked')
    assert not (s.files/blob.digest).exists()
    # The replay decides again under the lock: it never unlinks what became wanted meanwhile.
    (s.files/blob.digest).write_bytes(b'Synthetic retained contract')
    with s.pool.connection() as c:api.touch_original(c,blob.digest,blob.bytes)
    report=worker.run_once(apply=True)
    assert report['evicted']==0 and report['reasons']=={'recently_requested':1}
    assert eviction_state(s,blob.digest)==('resident',['cancelled'],True)
    with s.pool.connection() as c:assert not api.is_managed_evicted(c,blob.digest,blob.bytes)


def test_recent_touch_between_prepare_and_unlink_cancels_eviction(archive,monkeypatch):
    _,s,_,_=archive;_,blob=processed(archive);api=lifecycle();worker=api.LifecycleWorker(s)
    complete=worker._complete
    def touched(token):
        with s.pool.connection() as c:api.touch_original(c,blob.digest,blob.bytes)
        return complete(token)
    monkeypatch.setattr(worker,'_complete',touched)
    assert worker.run_once(apply=True)['evicted']==0
    assert (s.files/blob.digest).exists()
    with s.pool.connection() as c:
        assert c.execute('SELECT state FROM attachment_residency WHERE digest=%s',(blob.digest,)).fetchone()['state']=='resident'
        assert not api.is_managed_evicted(c,blob.digest,blob.bytes)


def test_managed_redownload_preserves_identity_detail_and_retry_history(archive):
    from messaging_service.blobs import BlobWriter
    ar,s,key,_=archive;fid,blob=processed(archive);api=lifecycle()
    with s.pool.connection() as c:c.execute('UPDATE media_jobs SET attempts=2 WHERE id=%s',(fid,))
    assert api.LifecycleWorker(s).run_once(apply=True)['evicted']==1
    result=s.request_file_download(key,fid)
    assert result['request_status']=='queued' and result['reason']=='managed_original_evicted'
    again=s.request_file_download(key,fid)
    assert again['request_status']=='coalesced' and again['requested_until']==result['requested_until']
    job=ar.claim_media('fixture');assert job['id']==fid and job['attempts']==3
    writer=BlobWriter(s.files,reserve_bytes=0);verified=writer.write([b'Synthetic retained contract'])
    ar.finish_media(job,verified,writer)
    with s.pool.connection() as c:
        assert not api.is_managed_evicted(c,blob.digest,blob.bytes)
        assert c.execute('SELECT attempts FROM media_jobs WHERE id=%s',(fid,)).fetchone()['attempts']==3
        assert c.execute('SELECT state FROM attachment_text WHERE attachment=%s',(fid,)).fetchone()['state']=='ready'
        assert c.execute('SELECT state FROM attachment_understanding WHERE attachment=%s',(fid,)).fetchone()['state']=='ready'


def test_managed_redownload_cannot_replace_retained_digest(archive):
    from messaging_service.blobs import BlobWriter
    ar,s,key,_=archive;fid,blob=processed(archive);api=lifecycle()
    api.LifecycleWorker(s).run_once(apply=True);s.request_file_download(key,fid)
    job=ar.claim_media('fixture');writer=BlobWriter(s.files,reserve_bytes=0)
    different=writer.write([b'x'*blob.bytes])
    with pytest.raises(ValueError,match='media_identity_mismatch'):ar.finish_media(job,different,writer)
    with s.pool.connection() as c:
        assert c.execute('SELECT digest FROM attachments WHERE id=%s',(fid,)).fetchone()['digest']==blob.digest
        assert api.is_managed_evicted(c,blob.digest,blob.bytes)


@pytest.mark.parametrize('mime',['audio/ogg','image/png','video/mp4'])
def test_appropriate_retained_detail_enables_processed_media_eviction(archive,mime):
    _,s,_,_=archive;_,blob=processed(archive,mime=mime)
    assert lifecycle().LifecycleWorker(s).run_once(apply=True)['evicted']==1
    assert not (s.files/blob.digest).exists()


def test_same_revision_detail_change_invalidates_old_description(archive):
    _,s,_,_=archive;fid,blob=processed(archive)
    with s.pool.connection() as c:c.execute("UPDATE attachment_text SET text_content='Replaced synthetic detail' WHERE attachment=%s",(fid,))
    assert lifecycle().LifecycleWorker(s).run_once(apply=True)['evicted']==0
    assert (s.files/blob.digest).exists()


def test_corrupt_description_artifact_hash_is_not_eligible(archive):
    _,s,_,_=archive;fid,blob=processed(archive)
    with s.pool.connection() as c:c.execute('UPDATE attachment_understanding SET source_artifact_hash=%s WHERE attachment=%s',('b'*64,fid))
    assert lifecycle().LifecycleWorker(s).run_once(apply=True)['evicted']==0


@pytest.mark.parametrize('case',['failed','exhausted','cooldown','unavailable'])
def test_managed_redownload_preserves_terminal_backoff_and_provider_absence(archive,case):
    ar,s,key,_=archive;fid,blob=processed(archive);api=lifecycle()
    api.LifecycleWorker(s).run_once(apply=True)
    with s.pool.connection() as c:
        if case=='failed':c.execute("UPDATE media_jobs SET state='failed',error_code='integrity_error',attempts=3 WHERE id=%s",(fid,))
        if case=='exhausted':c.execute('UPDATE media_jobs SET attempts=%s WHERE id=%s',(MEDIA_MAX_ATTEMPTS,fid))
        if case=='cooldown':c.execute("UPDATE media_jobs SET next_due=now()+interval '3 hours',error_code='rate_limited' WHERE id=%s",(fid,))
    result=s.request_file_download(key,fid)
    if case in ('failed','exhausted'):assert result['request_status']=='failed'
    elif case=='cooldown':assert result['request_status']=='deferred'
    else:
        assert result['request_status']=='queued'
        job=ar.claim_media('fixture');ar.fail_media(job,'unavailable',permanent=True)
        result=s.request_file_download(key,fid)
        assert result['request_status']=='failed' and result['reason']=='unavailable'
    with s.pool.connection() as c:assert api.is_managed_evicted(c,blob.digest,blob.bytes)


def test_root_replacement_during_pending_replay_fails_closed(archive,monkeypatch,tmp_path):
    _,s,_,_=archive;_,blob=processed(archive);api=lifecycle();worker=api.LifecycleWorker(s)
    def crash(*args):raise RuntimeError('synthetic_crash')
    monkeypatch.setattr(worker,'_mark_evicted',crash)
    with pytest.raises(RuntimeError):worker.run_once(apply=True)
    monkeypatch.undo()
    original_root=s.files
    moved=original_root.with_name(original_root.name+'-old');original_root.rename(moved)
    original_root.mkdir()
    try:
        assert worker.run_once(apply=True)['evicted']==0
        with s.pool.connection() as c:assert not api.is_managed_evicted(c,blob.digest,blob.bytes)
    finally:
        original_root.rmdir();moved.rename(original_root)


def test_concurrent_pin_during_hash_cancels_before_unlink(archive,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    _,s,_,_=archive;_,blob=processed(archive);api=lifecycle();worker=api.LifecycleWorker(s)
    original=worker._verified;prepared=Event();resume=Event()
    calls=0
    def paused(*args):
        nonlocal calls
        calls+=1
        value=original(*args)
        if calls==2:
            prepared.set();assert resume.wait(10)
        return value
    monkeypatch.setattr(worker,'_verified',paused)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(worker.run_once,apply=True)
        assert prepared.wait(10)
        with s.pool.connection() as c:api.pin_original(c,blob.digest,True)
        resume.set();assert future.result(timeout=10)['evicted']==0
    assert (s.files/blob.digest).exists()


def test_size_bound_skips_large_original_and_processes_small_one(archive):
    _,s,_,_=archive;_,large=processed(archive,payload=b'First synthetic large original')
    _,small=processed(archive,mid='lifecycle-small',payload=b'Small')
    report=lifecycle().LifecycleWorker(s).run_once(apply=True,max_bytes=small.bytes)
    assert report['evicted']==1 and report['evicted_bytes']==small.bytes
    assert (s.files/large.digest).exists() and not (s.files/small.digest).exists()


def test_legacy_managed_original_without_provider_job_reports_unavailable(archive):
    _,s,key,_=archive;fid,blob=processed(archive)
    with s.pool.connection() as c:c.execute('DELETE FROM media_jobs WHERE id=%s',(fid,))
    assert lifecycle().LifecycleWorker(s).run_once(apply=True)['evicted']==1
    result=s.request_file_download(key,fid)
    assert result['request_status']=='unavailable' and result['reason']=='provider_source_unavailable'
    with s.pool.connection() as c:assert c.execute('SELECT count(*) AS n FROM media_jobs WHERE id=%s',(fid,)).fetchone()['n']==0


def test_partial_audio_metadata_never_authorizes_original_eviction(archive):
    from psycopg.types.json import Jsonb
    from messaging_service.file_understanding import current_understanding_source,source_artifact_hash
    _,s,_,_=archive;fid,blob=processed(archive,mime='audio/ogg')
    with s.pool.connection() as c:
        c.execute('UPDATE attachment_enrichment SET metadata=%s WHERE attachment=%s',(Jsonb({'audio_truncated':True}),fid))
        source=current_understanding_source(c,fid)
        c.execute("""UPDATE attachment_understanding SET state='ready',title='Partial speech',summary='Partial audio only',
            source_artifact_hash=%s,coverage=%s,published_profile_hash=%s,published_provider='ollama',
            published_model='synthetic-model',indexed_at=clock_timestamp() WHERE attachment=%s""",
            (source_artifact_hash(source),Jsonb({'mode':'transcript_summary','complete':True}),'b'*64,fid))
    assert lifecycle().LifecycleWorker(s).run_once(apply=True)['evicted']==0
    assert (s.files/blob.digest).exists()


def test_cli_preview_and_apply_require_explicit_operator_arguments(archive,capsys):
    _,s,_,_=archive;_,blob=processed(archive)
    api=lifecycle()
    assert api.main(['--dsn-file',__import__('os').environ['PROBE_TEST_DSN_FILE'],'--files',str(s.files)])==0
    assert '"dry_run": true' in capsys.readouterr().out
    assert (s.files/blob.digest).exists()
    cold_copy(s,blob)
    assert api.main(['--dsn-file',__import__('os').environ['PROBE_TEST_DSN_FILE'],'--files',str(s.files),
        '--apply','--limit','1','--max-bytes',str(blob.bytes)])==0
    assert '"evicted": 1' in capsys.readouterr().out
    assert not (s.files/blob.digest).exists()


def test_operator_role_can_evict_but_cannot_modify_archive_or_derived_content(archive):
    from contextlib import contextmanager
    from copy import copy
    from pathlib import Path
    from uuid import uuid4
    import psycopg
    from psycopg import sql
    _,s,_,_=archive;_,blob=processed(archive)
    role='lifecycle_'+uuid4().hex
    path=Path('services/messaging/messaging_service/file_lifecycle_role.sql')
    assert path.exists(),'dedicated lifecycle DB role is required'
    ddl=path.read_text().replace('messaging_file_lifecycle',role)
    class RestrictedPool:
        @contextmanager
        def connection(self):
            with s.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                yield c
    restricted=copy(s);restricted.pool=RestrictedPool()
    with s.pool.connection() as c:c.execute(ddl)
    try:
        assert lifecycle().LifecycleWorker(restricted).run_once(apply=True)['evicted']==1
        for statement in ("UPDATE attachments SET active=false","UPDATE media_jobs SET id=id||'-renamed'","UPDATE attachment_text SET attachment=attachment",'DELETE FROM attachment_text',
                "UPDATE attachment_understanding SET title='Forged'",'DELETE FROM messages'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with restricted.pool.connection() as c:c.execute(statement)
    finally:
        with s.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def test_new_shared_occurrence_cannot_publish_ready_after_preflight_eviction(archive,monkeypatch):
    from messaging_service.blobs import BlobWriter
    ar,s,_,_=archive;fid,blob=processed(archive)
    ar.ingest(event(provider_id='new-during-eviction',media=[{'provider_id':'new-asset',
        'filename':'Other occurrence.txt','mime':'text/plain','bytes':blob.bytes}]))
    job=ar.claim_media('fixture');writer=BlobWriter(s.files,reserve_bytes=0)
    same=writer.write([b'Synthetic retained contract'])
    verify=writer.verify
    def verified_then_evicted(value):
        path=verify(value)
        assert lifecycle().LifecycleWorker(s).run_once(apply=True)['evicted']==1
        return path
    monkeypatch.setattr(writer,'verify',verified_then_evicted)
    with pytest.raises(RuntimeError,match='blob_corrupt'):ar.finish_media(job,same,writer)
    with s.pool.connection() as c:
        assert c.execute('SELECT state FROM media_jobs WHERE id=%s',(job['id'],)).fetchone()['state']=='leased'
        assert c.execute('SELECT count(*) AS n FROM attachments WHERE id=%s',(job['id'],)).fetchone()['n']==0


def evict_with(s, during):
    """One applied eviction pass; its final locked transaction runs `during()` after the unlink, before the mark."""
    worker=lifecycle().LifecycleWorker(s);mark=worker._mark_evicted
    def paused(c,row):during();mark(c,row)
    worker._mark_evicted=paused
    return worker.run_once(apply=True)


def leased_download(ar, s, provider_id, payload):
    """A queued original of a new message, leased and written, ready for finish_media."""
    from messaging_service.blobs import BlobWriter
    ar.ingest(event(provider_id=provider_id,media=[{'provider_id':'asset-'+provider_id,'filename':'Racing.txt',
        'mime':'text/plain','bytes':len(payload)}]))
    job=ar.claim_media('fixture');writer=BlobWriter(s.files,reserve_bytes=0)
    return job,writer.write([payload]),writer


# spec: ANYM-839
@pytest.mark.parametrize('edit',['adds_media','text_only'])
def test_an_edit_of_a_message_whose_file_is_being_evicted_waits_for_the_eviction(archive,edit):
    ar,s,_,_=archive;fid,blob=processed(archive)
    kept={'provider_id':'asset-lifecycle-1','filename':'Synthetic.txt','mime':'text/plain','bytes':blob.bytes}
    media=[kept,{'provider_id':'asset-added','filename':'Added.txt','mime':'text/plain','bytes':5}] if edit=='adds_media' else []
    envelope=event(provider_id='lifecycle-1',body='Edited during eviction',updated_at='2026-10-01T12:05:00+00:00',media=media)
    seen={}
    with ThreadPoolExecutor(max_workers=1) as threads:
        def during():
            seen['ingest']=finished_or_waiting(threads,s,lambda:ar.ingest(envelope))
            seen['waited']=not seen['ingest'].done()
        assert evict_with(s,during)['evicted']==1
        assert seen['ingest'].result(timeout=10)['outcome']=='edited'
    assert seen['waited']
    assert eviction_state(s,blob.digest)==('evicted',['completed'],False)
    with s.pool.connection() as c:
        assert c.execute('SELECT active FROM attachments WHERE id=%s',(fid,)).fetchone()['active'] is (edit=='adds_media')
        added=c.execute("SELECT state FROM media_jobs WHERE descriptor->>'provider_id'='asset-added'").fetchone()
    assert added==({'state':'queued'} if edit=='adds_media' else None)


# spec: ANYM-839
def test_finish_media_racing_an_eviction_waits_while_text_ingest_goes_on(archive):
    ar,s,_,_=archive;_,blob=processed(archive)
    ar.register('archive-other','telegram','Synthetic other',include_chats=['100'])
    job,racing,writer=leased_download(ar,s,'racing',b'Racing synthetic payload')
    seen={}
    with ThreadPoolExecutor(max_workers=2) as threads:
        def during():
            seen['finish']=finished_or_waiting(threads,s,lambda:ar.finish_media(job,racing,writer))
            seen['waited']=not seen['finish'].done()
            # Text goes on meanwhile. Here another account's: the eviction's lock_lifecycle_sources (033) also holds
            # the account lock of the original's own account until it commits.
            seen['text']=threads.submit(ar.ingest,event(account='archive-other',provider_id='text-during-eviction')).result(timeout=4)
        assert evict_with(s,during)['evicted']==1
        seen['finish'].result(timeout=10)
    assert seen['waited'] and seen['text']['outcome']=='created'
    assert eviction_state(s,blob.digest)==('evicted',['completed'],False)
    with s.pool.connection() as c:
        assert c.execute('SELECT state FROM media_jobs WHERE id=%s',(job['id'],)).fetchone()['state']=='ready'


def paused_after_account_lock(store, account, entered, release):
    """`store` whose first `archive:<account>` lock statement, once it ran, waits for `release`."""
    from contextlib import contextmanager
    class Connection:
        def __init__(self,inner):self.inner=inner
        def __getattr__(self,name):return getattr(self.inner,name)
        def execute(self,query,params=None,**kwargs):
            result=self.inner.execute(query,params,**kwargs)
            if params==('archive:'+account,) and not entered.is_set():
                entered.set();assert release.wait(15)
            return result
    class Pool:
        conninfo=store.pool.conninfo
        @contextmanager
        def connection(self):
            with store.pool.connection() as inner:yield Connection(inner)
    class Paused:
        pool=Pool()
        def __getattr__(self,name):return getattr(store,name)
    return Paused()


# spec: ANYM-839
@pytest.mark.parametrize('target',['no_attachment','attachment'])
def test_text_ingest_paused_between_the_account_and_residency_locks_does_not_deadlock_with_finish_media(archive,target):
    from messaging_service.archive import Archive
    ar,s,_,_=archive
    if target=='attachment':fid,_=stored(archive,mid='paused-target')
    else:ar.ingest(event(provider_id='paused-target'))
    job,racing,writer=leased_download(ar,s,'racing',b'Racing synthetic payload')
    entered,release=Event(),Event()
    paused=Archive(paused_after_account_lock(s,'archive-test',entered,release))
    with ThreadPoolExecutor(max_workers=2) as threads:
        ingest=threads.submit(paused.ingest,event(provider_id='paused-target',body='Edited while paused',
            updated_at='2026-10-01T12:05:00+00:00'))
        assert entered.wait(5)
        # finish_media takes the residency lock, then waits for the account lock the paused ingest holds.
        finish=finished_or_waiting(threads,s,lambda:ar.finish_media(job,racing,writer))
        assert not finish.done()
        release.set()
        # With attachment rows the ingest rolls back, releasing the account lock, and restarts lifecycle-first.
        assert ingest.result(timeout=20)['outcome']=='edited'
        finish.result(timeout=20)
    with s.pool.connection() as c:
        assert c.execute('SELECT state FROM media_jobs WHERE id=%s',(job['id'],)).fetchone()['state']=='ready'
        if target=='attachment':
            assert c.execute('SELECT active FROM attachments WHERE id=%s',(fid,)).fetchone()['active'] is False


def test_corrupt_original_hashes_still_consume_pass_byte_budget(archive,monkeypatch):
    _,s,_,_=archive;_,one=processed(archive,payload=b'First synthetic payload')
    _,two=processed(archive,mid='lifecycle-corrupt-two',payload=b'Other synthetic payload')
    for blob in (one,two):(s.files/blob.digest).write_bytes(b'x'*blob.bytes)
    api=lifecycle();worker=api.LifecycleWorker(s);hashed=[];verify=worker._verified
    def counted(digest,size):
        hashed.append(size);return verify(digest,size)
    monkeypatch.setattr(worker,'_verified',counted)
    assert worker.run_once(apply=True,max_bytes=max(one.bytes,two.bytes))['evicted']==0
    assert sum(hashed)<=max(one.bytes,two.bytes)


def test_missing_replay_rechecks_root_after_verification_before_commit(archive,monkeypatch):
    _,s,_,_=archive;_,blob=processed(archive);api=lifecycle();worker=api.LifecycleWorker(s)
    mark=worker._mark_evicted
    def crash(*args):raise RuntimeError('synthetic_crash')
    monkeypatch.setattr(worker,'_mark_evicted',crash)
    with pytest.raises(RuntimeError):worker.run_once(apply=True)
    monkeypatch.setattr(worker,'_mark_evicted',mark)
    verify=worker._verified;original_root=s.files;moved=original_root.with_name(original_root.name+'-moved')
    def root_changed_after_missing(*args):
        try:return verify(*args)
        except api.OriginalMissing:
            original_root.rename(moved);original_root.mkdir()
            raise
    monkeypatch.setattr(worker,'_verified',root_changed_after_missing)
    try:
        assert worker.run_once(apply=True)['evicted']==0
        with s.pool.connection() as c:assert not api.is_managed_evicted(c,blob.digest,blob.bytes)
    finally:
        original_root.rmdir();moved.rename(original_root)


def test_least_privilege_collector_publishes_and_gateway_requeues_managed_original(archive):
    from contextlib import contextmanager
    from copy import copy
    from pathlib import Path
    from uuid import uuid4
    from psycopg import sql
    from messaging_service.archive import Archive
    from messaging_service.blobs import BlobWriter
    ar,s,key,_=archive
    roles={kind:kind+'_'+uuid4().hex for kind in ('messaging_collector','messaging_gateway')}
    class RestrictedPool:
        def __init__(self,role):self.role=role
        @contextmanager
        def connection(self):
            with s.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(self.role)))
                yield c
    for kind,role in roles.items():
        path=Path('services/messaging/messaging_service')/(kind.removeprefix('messaging_')+'_role.sql')
        with s.pool.connection() as c:c.execute(path.read_text().replace(kind,role))
    collector=copy(s);collector.pool=RestrictedPool(roles['messaging_collector'])
    gateway=copy(s);gateway.pool=RestrictedPool(roles['messaging_gateway'])
    restricted_archive=Archive(collector)
    try:
        payload=b'Synthetic role publication'
        ar.ingest(event(provider_id='role-original',media=[{'provider_id':'role-asset',
            'filename':'Synthetic roles.txt','mime':'text/plain','bytes':len(payload)}]))
        job=restricted_archive.claim_media('fixture')
        writer=BlobWriter(s.files,reserve_bytes=0);blob=writer.write([payload])
        restricted_archive.finish_media(job,blob,writer)
        fid=job['id']
        from psycopg.types.json import Jsonb
        from messaging_service.file_understanding import current_understanding_source,source_artifact_hash
        with s.pool.connection() as c:
            c.execute("UPDATE attachment_text SET state='ready',text_content='Full role fixture detail' WHERE attachment=%s",(fid,))
            source=current_understanding_source(c,fid)
            c.execute("""UPDATE attachment_understanding SET state='ready',source_artifact_hash=%s,title='Roles',
                summary='Role fixture description',coverage=%s,profile_hash=%s,published_profile_hash=%s,
                provider='ollama',model='synthetic-model',published_provider='ollama',published_model='synthetic-model',
                indexed_at=clock_timestamp() WHERE attachment=%s""",
                (source_artifact_hash(source),Jsonb({'mode':'extracted_text_summary','complete':True}),'b'*64,'b'*64,fid))
            c.execute("UPDATE attachment_residency SET last_accessed_at=now()-interval '31 days' WHERE digest=%s",(blob.digest,))
        assert lifecycle().LifecycleWorker(s).run_once(apply=True)['evicted']==1
        assert gateway.request_file_download(key,fid)['request_status']=='queued'
        again=restricted_archive.claim_media('fixture')
        verified=writer.write([payload]);restricted_archive.finish_media(again,verified,writer)
        with s.pool.connection() as c:
            assert c.execute('SELECT state FROM attachment_understanding WHERE attachment=%s',(fid,)).fetchone()['state']=='ready'
            assert not lifecycle().is_managed_evicted(c,blob.digest,blob.bytes)
    finally:
        for role in roles.values():
            with s.pool.connection() as c:
                c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def test_replay_syncs_missing_directory_before_marking_evicted(archive,monkeypatch):
    _,s,_,_=archive;_,blob=processed(archive);api=lifecycle();worker=api.LifecycleWorker(s)
    sync=api.os.fsync;calls=[]
    def interrupted(fd):
        calls.append(fd)
        if len(calls)==1:raise OSError('synthetic directory sync interruption')
        return sync(fd)
    monkeypatch.setattr(api.os,'fsync',interrupted)
    with pytest.raises(OSError,match='synthetic'):worker.run_once(apply=True)
    assert not (s.files/blob.digest).exists()
    assert worker.run_once(apply=True)['evicted']==1
    assert len(calls)==2


def old_unprocessed(archive,*,mid='old-unprocessed',mime='application/pdf'):
    fid,blob=stored(archive,mid=mid,mime=mime)
    _,s,_,_=archive
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET sent_at=clock_timestamp()-interval '31 days' WHERE id=(SELECT message FROM attachments WHERE id=%s)",(fid,))
        c.execute("UPDATE attachment_residency SET last_accessed_at=clock_timestamp()-interval '31 days' WHERE digest=%s",(blob.digest,))
    return fid,blob


@pytest.mark.parametrize('mime',['application/pdf','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'audio/ogg','image/png','video/mp4'])
def test_explicit_policy_expires_old_unprocessed_media_without_fabricating_detail(archive,mime):
    _,s,_,_=archive;fid,blob=old_unprocessed(archive,mime=mime);api=lifecycle()
    with s.pool.connection() as c:
        before=dict(c.execute('SELECT * FROM attachments WHERE id=%s',(fid,)).fetchone())
        message=dict(c.execute('SELECT * FROM messages WHERE id=%s',(before['message'],)).fetchone())
        detail=dict(c.execute('SELECT * FROM attachment_understanding WHERE attachment=%s',(fid,)).fetchone())
    assert api.LifecycleWorker(s).run_once(apply=True)['evicted']==0
    worker=api.LifecycleWorker(s,expire_unprocessed=True)
    preview=worker.run_once()
    assert preview['expire_unprocessed'] and preview['dry_run'] and preview['candidates']==1
    assert (s.files/blob.digest).exists()
    result=worker.run_once(apply=True)
    assert result['evicted']==1 and result['evicted_bytes']==blob.bytes
    assert not (s.files/blob.digest).exists()
    with s.pool.connection() as c:
        assert api.is_managed_evicted(c,blob.digest,blob.bytes)
        assert dict(c.execute('SELECT * FROM attachments WHERE id=%s',(fid,)).fetchone())==before
        assert dict(c.execute('SELECT * FROM messages WHERE id=%s',(before['message'],)).fetchone())==message
        assert dict(c.execute('SELECT * FROM attachment_understanding WHERE attachment=%s',(fid,)).fetchone())==detail


@pytest.mark.parametrize('blocker',['recent_source','recent_access','pin','backup','media_lease','demand',
    'grant','text_lease','asr_lease','understanding_lease','shared_recent_source'])
def test_unprocessed_policy_preserves_current_use_pins_fences_and_shared_recent_sources(archive,blocker):
    _,s,key,_=archive;fid,blob=old_unprocessed(archive);api=lifecycle()
    if blocker=='shared_recent_source':
        shared,_=stored(archive,mid='shared-recent')  # event() carries a fixed 2026-10-01 sent_at: make it recent now
        with s.pool.connection() as c:
            c.execute("UPDATE messages SET sent_at=clock_timestamp() WHERE id=(SELECT message FROM attachments WHERE id=%s)",(shared,))
    if blocker=='grant':s.get_attachment(key,fid)
    with s.pool.connection() as c:
        if blocker in ('grant','shared_recent_source'):
            c.execute("UPDATE attachment_residency SET last_accessed_at=clock_timestamp()-interval '31 days' WHERE digest=%s",(blob.digest,))
        if blocker=='recent_source':c.execute('UPDATE messages SET sent_at=clock_timestamp()')
        if blocker=='recent_access':api.touch_original(c,blob.digest,blob.bytes)
        if blocker=='pin':api.pin_original(c,blob.digest,True)
        if blocker=='backup':api.begin_backup_fence(c,'unprocessed-policy-backup')
        if blocker=='media_lease':c.execute("UPDATE media_jobs SET state='leased',lease_until=clock_timestamp()+interval '1 hour' WHERE id=%s",(fid,))
        if blocker=='demand':c.execute("UPDATE media_jobs SET requested_until=clock_timestamp()+interval '1 hour' WHERE id=%s",(fid,))
        if blocker in ('text_lease','asr_lease','understanding_lease'):
            table={'text_lease':'attachment_text','asr_lease':'attachment_enrichment',
                'understanding_lease':'attachment_understanding'}[blocker]
            from psycopg import sql
            c.execute(sql.SQL("UPDATE {} SET state='leased',lease_until=clock_timestamp()+interval '1 hour' WHERE attachment=%s").format(sql.Identifier(table)),(fid,))
    result=api.LifecycleWorker(s,expire_unprocessed=True).run_once(apply=True)
    assert result['evicted']==0
    if blocker in ('pin','recent_access'):
        assert result['candidates']==0 and result['reasons']=={}
    else:
        reason=('backup_active' if blocker=='backup' else
            'recent_source' if blocker in ('recent_source','shared_recent_source') else 'inflight')
        assert result['reasons']=={reason:1}
    assert (s.files/blob.digest).exists()
    with s.pool.connection() as c:assert not api.is_managed_evicted(c,blob.digest,blob.bytes)


@pytest.mark.parametrize('unsafe',['corrupt','symlink','missing'])
def test_unprocessed_policy_never_labels_unknown_or_corrupt_absence_managed(archive,unsafe,tmp_path):
    _,s,_,_=archive;_,blob=old_unprocessed(archive);api=lifecycle();path=s.files/blob.digest
    path.unlink()
    target=tmp_path/'synthetic-unrelated';target.write_bytes(b'Keep unrelated bytes')
    if unsafe=='corrupt':path.write_bytes(b'x'*blob.bytes)
    if unsafe=='symlink':path.symlink_to(target)
    assert api.LifecycleWorker(s,expire_unprocessed=True).run_once(apply=True)['evicted']==0
    with s.pool.connection() as c:assert not api.is_managed_evicted(c,blob.digest,blob.bytes)
    assert target.read_bytes()==b'Keep unrelated bytes'


def test_unprocessed_original_can_be_requested_again_without_resetting_retries(archive):
    from messaging_service.blobs import BlobWriter
    ar,s,key,_=archive;fid,blob=old_unprocessed(archive);api=lifecycle()
    with s.pool.connection() as c:c.execute('UPDATE media_jobs SET attempts=2 WHERE id=%s',(fid,))
    assert api.LifecycleWorker(s,expire_unprocessed=True).run_once(apply=True)['evicted']==1
    assert s.request_file_download(key,fid)['request_status']=='queued'
    job=ar.claim_media('synthetic-recovery');assert job['id']==fid and job['attempts']==3
    writer=BlobWriter(s.files,reserve_bytes=0);again=writer.write([b'Synthetic retained contract'])
    ar.finish_media(job,again,writer)
    assert (s.files/blob.digest).read_bytes()==b'Synthetic retained contract'
    with s.pool.connection() as c:
        assert not api.is_managed_evicted(c,blob.digest,blob.bytes)
        assert c.execute('SELECT attempts FROM media_jobs WHERE id=%s',(fid,)).fetchone()['attempts']==3
        assert c.execute('SELECT state FROM attachment_understanding WHERE attachment=%s',(fid,)).fetchone()['state']!='ready'
    assert api.LifecycleWorker(s,expire_unprocessed=True).run_once(apply=True)['evicted']==0


def test_unprocessed_policy_still_reports_provider_unavailability(archive):
    ar,s,key,_=archive;fid,blob=old_unprocessed(archive);api=lifecycle()
    assert api.LifecycleWorker(s,expire_unprocessed=True).run_once(apply=True)['evicted']==1
    assert s.request_file_download(key,fid)['request_status']=='queued'
    ar.fail_media(ar.claim_media('synthetic-unavailable'),'unavailable',permanent=True)
    result=s.request_file_download(key,fid)
    assert result['request_status']=='failed' and result['reason']=='unavailable'
    with s.pool.connection() as c:assert api.is_managed_evicted(c,blob.digest,blob.bytes)


def test_unprocessed_policy_rejects_ambiguous_values(archive):
    _,s,_,_=archive
    for value in (1,0,'true',None):
        with pytest.raises(ValueError,match='invalid_retention_policy'):
            lifecycle().LifecycleWorker(s,expire_unprocessed=value)


def test_unprocessed_expiry_keeps_partial_text_and_produces_explicit_backup_absence(archive):
    from messaging_service.backup import blob_inventory
    _,s,key,_=archive;fid,blob=old_unprocessed(archive);api=lifecycle()
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_text SET state='ready',text_content='Synthetic partial detail',truncated=true WHERE attachment=%s",(fid,))
        partial=dict(c.execute('SELECT * FROM attachment_text WHERE attachment=%s',(fid,)).fetchone())
    assert api.LifecycleWorker(s,expire_unprocessed=True).run_once(apply=True)['evicted']==1
    with s.pool.connection() as c:
        present,absent=blob_inventory(c,34)
        journal=c.execute('SELECT eviction_id FROM attachment_residency WHERE digest=%s',(blob.digest,)).fetchone()['eviction_id']
        assert not any(item['digest']==blob.digest for item in present)
        assert {'digest':blob.digest,'bytes':blob.bytes,'eviction_id':str(journal)} in absent
        assert dict(c.execute('SELECT * FROM attachment_text WHERE attachment=%s',(fid,)).fetchone())==partial
    metadata=s.get_attachment(key,fid)
    assert metadata['reason']=='original_evicted' and metadata['next_action']=='request_file_download'
    assert metadata['understanding']['state']!='ready'


@pytest.mark.parametrize('stage',['extraction','audio','image','document'])
def test_unprocessed_expiry_defers_original_processing_without_spending_retries_until_recovery(archive,tmp_path,stage):
    from messaging_service.blobs import BlobWriter
    from messaging_service.file_index import FileIndexer
    from messaging_service.enrichment import EnrichmentWorker
    from test_enrichment import profile
    mime={'extraction':'application/pdf','audio':'audio/ogg','image':'image/png','document':'text/plain'}[stage]
    ar,s,key,_=archive;fid,blob=old_unprocessed(archive,mime=mime);api=lifecycle()
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_text SET state='unsupported' WHERE attachment<>%s",(fid,))
        if stage=='document':
            c.execute("UPDATE attachment_text SET state='ready',text_content='Retained synthetic document detail' WHERE attachment=%s",(fid,))
        c.execute('TRUNCATE enrichment_budget')
        text_before=dict(c.execute('SELECT * FROM attachment_text WHERE attachment=%s',(fid,)).fetchone())
        enrichment_before=dict(c.execute('SELECT * FROM attachment_enrichment WHERE attachment=%s',(fid,)).fetchone())
        budget_before=c.execute('SELECT coalesce(sum(attempts),0) AS used FROM processing_budget').fetchone()['used']
    assert api.LifecycleWorker(s,expire_unprocessed=True).run_once(apply=True)['evicted']==1
    if stage=='extraction':
        worker=FileIndexer(s.pool.conninfo,s.files)
        assert worker.claim() is None
    else:
        worker=EnrichmentWorker(s.pool.conninfo,s.files,{'accounts':['archive-test'],
            'max_jobs_per_day':20,'profiles':{stage:profile(tmp_path,kind=stage)}})
        assert worker.claim() is None
    with s.pool.connection() as c:
        assert dict(c.execute('SELECT * FROM attachment_text WHERE attachment=%s',(fid,)).fetchone())==text_before
        assert dict(c.execute('SELECT * FROM attachment_enrichment WHERE attachment=%s',(fid,)).fetchone())==enrichment_before
        assert c.execute('SELECT coalesce(sum(attempts),0) AS used FROM enrichment_budget').fetchone()['used']==0
        assert c.execute('SELECT coalesce(sum(attempts),0) AS used FROM processing_budget').fetchone()['used']==budget_before
    assert s.request_file_download(key,fid)['request_status']=='queued'
    media=ar.claim_media('synthetic-processing-recovery');writer=BlobWriter(s.files,reserve_bytes=0)
    ar.finish_media(media,writer.write([b'Synthetic retained contract']),writer)
    claimed=worker.claim()
    assert claimed is not None
    if stage=='extraction':
        from messaging_service.file_extract import Extraction
        assert claimed['attachment']==fid and claimed['attempts']==1
        worker.finish(claimed,Extraction('ready','Recovered synthetic full text'))
    else:
        row,_=claimed
        assert row['attachment']==fid and row['attempts']==1
        worker.finish(row,error='synthetic_fixture_complete')


def test_unknown_missing_original_still_consumes_extraction_failure_attempt(archive):
    from messaging_service.file_index import FileIndexer
    _,s,_,_=archive;fid,blob=old_unprocessed(archive)
    with s.pool.connection() as c:c.execute("UPDATE attachment_text SET state='unsupported' WHERE attachment<>%s",(fid,))
    (s.files/blob.digest).unlink()
    report=FileIndexer(s.pool.conninfo,s.files).run_once()
    assert report['claimed'] and report['error_code']=='original_unavailable'
    with s.pool.connection() as c:
        assert c.execute('SELECT attempts FROM attachment_text WHERE attachment=%s',(fid,)).fetchone()['attempts']==1
        assert not lifecycle().is_managed_evicted(c,blob.digest,blob.bytes)


@pytest.mark.parametrize('stage',['extraction','audio'])
def test_restricted_processing_roles_can_defer_managed_absence_but_cannot_change_it(archive,tmp_path,stage):
    from contextlib import contextmanager
    from pathlib import Path
    from uuid import uuid4
    import psycopg
    from psycopg import sql
    from messaging_service.file_index import FileIndexer
    from messaging_service.enrichment import EnrichmentWorker
    from test_enrichment import profile
    _,s,_,_=archive;fid,blob=old_unprocessed(archive,mime='audio/ogg' if stage=='audio' else 'application/pdf')
    with s.pool.connection() as c:c.execute("UPDATE attachment_text SET state='unsupported' WHERE attachment<>%s",(fid,))
    assert lifecycle().LifecycleWorker(s,expire_unprocessed=True).run_once(apply=True)['evicted']==1
    kind='indexer' if stage=='extraction' else 'enricher'
    template='indexer_role.sql' if stage=='extraction' else 'enrichment_role.sql'
    role='retention_processing_'+uuid4().hex
    ddl=(Path('services/messaging/messaging_service')/template).read_text().replace('messaging_'+kind,role)
    @contextmanager
    def connection():
        with s.pool.connection() as c:
            c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
            yield c
    with s.pool.connection() as c:c.execute(ddl)
    try:
        if stage=='extraction':worker=FileIndexer(s.pool.conninfo,s.files)
        else:worker=EnrichmentWorker(s.pool.conninfo,s.files,{'accounts':['archive-test'],
            'max_jobs_per_day':20,'profiles':{'audio':profile(tmp_path,kind='audio')}})
        worker.connection=connection
        assert worker.claim() is None
        for statement in ('UPDATE attachment_residency SET pinned=false','DELETE FROM attachment_eviction_journal'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with connection() as c:c.execute(statement)
    finally:
        with s.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


@pytest.mark.parametrize('copy',['absent','pending','failed','other_size','verified'])
def test_eviction_requires_verified_cold_copy(archive,copy):
    _,s,_,_=archive;_,blob=processed(archive);api=lifecycle()
    if copy=='other_size':cold_copy(s,blob,bytes=blob.bytes+1)
    elif copy!='absent':cold_copy(s,blob,state=copy)
    preview=api.LifecycleWorker(s,require_cold_copy=True).run_once()
    report=api.LifecycleWorker(s,require_cold_copy=True).run_once(apply=True)
    if copy=='verified':
        assert preview['candidates']==1 and report['evicted']==1
        assert not (s.files/blob.digest).exists()
    else:
        assert preview['reasons']==report['reasons']=={'cold_copy_missing':1} and report['evicted']==0
        assert (s.files/blob.digest).read_bytes()==b'Synthetic retained contract'
        with s.pool.connection() as c:assert not api.is_managed_evicted(c,blob.digest,blob.bytes)


@pytest.mark.parametrize('change',['deleted','invalidated'])
def test_cold_copy_lost_between_prepare_and_unlink_cancels_eviction(archive,monkeypatch,change):
    _,s,_,_=archive;_,blob=processed(archive);api=lifecycle();cold_copy(s,blob)
    worker=api.LifecycleWorker(s,require_cold_copy=True);prepare=worker._prepare
    def lost_after_prepare(digest):
        token,reason=prepare(digest)
        with s.pool.connection() as c:
            if change=='deleted':c.execute('DELETE FROM attachment_cold_copies WHERE digest=%s',(digest,))
            else:c.execute("UPDATE attachment_cold_copies SET state='failed',error_code='cold_verify_failed',verified_at=NULL WHERE digest=%s",(digest,))
        return token,reason
    monkeypatch.setattr(worker,'_prepare',lost_after_prepare)
    report=worker.run_once(apply=True)
    assert report['evicted']==0 and report['reasons']=={'cold_copy_missing':1}
    assert (s.files/blob.digest).exists()
    with s.pool.connection() as c:
        journal=c.execute('SELECT state,error_code FROM attachment_eviction_journal WHERE digest=%s',(blob.digest,)).fetchone()
        assert dict(journal)=={'state':'cancelled','error_code':'cold_copy_missing'}
        assert c.execute('SELECT state FROM attachment_residency WHERE digest=%s',(blob.digest,)).fetchone()['state']=='resident'


def chat_original(archive,*,mid='group-1',chat='100',chat_type='group',payload=b'Synthetic group photo',
        idle_hours=25,copy=True,text='Synthetic harbour invoice'):
    """A downloaded image whose chat then has the given kind, idle for idle_hours, with a verified R2 copy (ANYM-806)."""
    from messaging_service.blobs import BlobWriter
    ar,s,_,_=archive
    with s.pool.connection() as c:
        c.execute('''UPDATE account_policies SET include_chats=array_append(include_chats,%s)
            WHERE account='archive-test' AND NOT %s=ANY(include_chats)''',(chat,chat))
        # Downloaded while the chat's kind is unknown (personal); the kind is set afterwards.
        c.execute("UPDATE chats SET chat_type=NULL WHERE account='archive-test' AND provider_chat=%s",(chat,))
    ar.ingest(event(provider_id=mid,chat=chat,media=[{'provider_id':'asset-'+mid,'filename':'Photo.jpg',
        'mime':'image/jpeg','bytes':len(payload)}]))
    job=ar.claim_media('fixture');writer=BlobWriter(s.files,reserve_bytes=0)
    blob=writer.write([payload]);ar.finish_media(job,blob,writer)
    with s.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type=%s WHERE account='archive-test' AND provider_chat=%s",(chat_type,chat))
        c.execute("UPDATE attachment_text SET state='ready',text_content=%s,error_code=NULL WHERE attachment=%s",(text,job['id']))
        c.execute("UPDATE attachment_residency SET last_accessed_at=clock_timestamp()-%s*interval '1 hour' WHERE digest=%s",
            (idle_hours,blob.digest))
    if copy:cold_copy(s,blob)
    return job['id'],blob


def name_in_subscription(s,provider_chat='100'):
    token=s.issue_key('lifecycle-watcher',['archive-test'],permissions=['read','subscription-write'],
        key_permissions=['read','subscription-write'])
    with s.pool.connection() as c:
        chat=c.execute("SELECT id FROM chats WHERE account='archive-test' AND provider_chat=%s",(provider_chat,)).fetchone()['id']
    return token,s.create_subscription(token,[chat])


# spec: ANYM-806
def test_a_group_original_leaves_the_disk_a_day_after_its_last_read_and_keeps_its_text(archive):
    _,s,key,_=archive;fid,blob=chat_original(archive);api=lifecycle()
    # Neither 30 days nor a description are needed; the verified R2 copy is.
    preview=api.LifecycleWorker(s,require_cold_copy=True).run_once()
    assert preview['candidates']==1 and preview['evicted']==0 and (s.files/blob.digest).exists()
    report=api.LifecycleWorker(s,require_cold_copy=True).run_once(apply=True)
    assert report['evicted']==1 and report['evicted_bytes']==blob.bytes and not (s.files/blob.digest).exists()
    item=s.get_attachment(key,fid)
    assert item['original_residency']=='evicted' and item['download_reason']=='original_evicted'
    assert item['next_action']=='request_file_download' and item['original_retention_hours']==24
    assert s.get_file_text(key,fid)['text']=='Synthetic harbour invoice'
    assert [row['attachment_id'] for row in s.search_files(key,query='harbour')['items']]==[fid]


# spec: ANYM-806
@pytest.mark.parametrize('keeper,reason',[('read_recently',None),('personal',None),('unknown_kind',None),
    ('subscribed',None),('shared_with_personal',None),('pinned',None),('no_cold_copy','cold_copy_missing'),
    ('download_grant','inflight'),('request_window','inflight'),('legacy',None),('shared_with_legacy',None)])
def test_a_group_original_stays_while_a_rule_protects_it(archive,keeper,reason):
    _,s,key,_=archive;api=lifecycle()
    fid,blob=chat_original(archive,idle_hours=23 if keeper=='read_recently' else 25,copy=keeper!='no_cold_copy',
        chat_type={'personal':'private','unknown_kind':None}.get(keeper,'group'))
    if keeper=='subscribed':name_in_subscription(s)
    if keeper=='shared_with_personal':
        # The same bytes in a personal chat: that occurrence keeps the 30-day rule for the whole original.
        chat_original(archive,mid='personal-1',chat='101',chat_type='private',payload=b'Synthetic group photo',copy=False)
        with s.pool.connection() as c:
            c.execute("UPDATE attachment_residency SET last_accessed_at=clock_timestamp()-interval '25 hours' WHERE digest=%s",(blob.digest,))
    if keeper=='shared_with_legacy':
        # A second occurrence of the same bytes without a media job: a request could not restore it from R2.
        other,_=chat_original(archive,mid='group-legacy',payload=b'Synthetic group photo',copy=False)
        with s.pool.connection() as c:
            c.execute('DELETE FROM media_jobs WHERE id=%s',(other,))
            c.execute("UPDATE attachment_residency SET last_accessed_at=clock_timestamp()-interval '25 hours' WHERE digest=%s",(blob.digest,))
    with s.pool.connection() as c:
        if keeper=='legacy':c.execute('DELETE FROM media_jobs WHERE id=%s',(fid,))
        if keeper=='pinned':api.pin_original(c,blob.digest)
        if keeper=='download_grant':
            c.execute('''INSERT INTO download_grants SELECT %s,digest,1,%s,clock_timestamp()+interval '2 minutes'
                FROM api_keys LIMIT 1''',('g'*64,fid))
        if keeper=='request_window':
            c.execute("UPDATE media_jobs SET requested_until=clock_timestamp()+interval '1 hour' WHERE id=%s",(fid,))
    for only in (False,True):
        report=api.LifecycleWorker(s,require_cold_copy=True,chat_policy_only=only).run_once(apply=True)
        assert report['evicted']==0 and report['reasons']==({reason:1} if reason else {}),report
    assert (s.files/blob.digest).exists()


# spec: ANYM-806
@pytest.mark.parametrize('requested,attempts',[(True,1),(True,3),(False,1)])
def test_a_requested_recognition_holds_a_group_original_for_at_most_seven_days(archive,requested,attempts):
    _,s,key,_=archive;fid,blob=chat_original(archive);api=lifecycle()
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET request_key_digest=%s WHERE id=%s",('r'*64 if requested else None,fid))
        # A failure is still pending recognition, an exhausted one too: a changed profile retries it.
        c.execute("UPDATE attachment_enrichment SET state='failed',attempts=%s,error_code='provider_unavailable' WHERE attachment=%s",
            (attempts,fid))
    report=api.LifecycleWorker(s,require_cold_copy=True,chat_policy_only=True).run_once(apply=True)
    if not requested:
        # Nobody asked: the withheld recognition does not hold the original.
        assert report['evicted']==1;return
    assert report['evicted']==0 and report['reasons']=={'recognition_pending':1} and (s.files/blob.digest).exists()
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_enrichment SET updated_at=clock_timestamp()-interval '8 days' WHERE attachment=%s",(fid,))
    assert api.LifecycleWorker(s,require_cold_copy=True,chat_policy_only=True).run_once(apply=True)['evicted']==1


# spec: ANYM-806
@pytest.mark.parametrize('state',['ready','queued','failed','leased'])
def test_a_removed_group_file_shows_what_an_agent_can_do(archive,state):
    _,s,key,_=archive;fid,blob=chat_original(archive);api=lifecycle()
    finished=state=='ready'
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET request_key_digest=%s WHERE id=%s",('r'*64,fid))
        # Unfinished work older than the 7-day cap; a leased row's lease has expired.
        c.execute('''UPDATE attachment_enrichment SET state=%s,kind='image',text_content=%s,attempts=CASE WHEN %s='failed' THEN 3 ELSE attempts END,
            lease_until=CASE WHEN %s='leased' THEN clock_timestamp()-interval '1 hour' END,
            updated_at=clock_timestamp()-interval '8 days' WHERE attachment=%s''',
            (state,'A synthetic harbour photo' if finished else '',state,state,fid))
    assert api.LifecycleWorker(s,require_cold_copy=True).run_once(apply=True)['evicted']==1
    item=s.get_attachment(key,fid)
    assert item['original_residency']=='evicted' and item['next_action']=='request_file_download'
    if finished:
        assert item['enrichment_status']=='ready' and 'recognition_next_action' not in item
    else:
        assert item['enrichment_status']=='available_on_request'
        assert item['recognition_next_action']=='request_file_download' and item['recognition_reason']=='group_chat_on_request'


# spec: ANYM-806
def test_a_subscription_created_between_prepare_and_unlink_cancels_the_eviction(archive,monkeypatch):
    _,s,_,_=archive;fid,blob=chat_original(archive);api=lifecycle()
    worker=api.LifecycleWorker(s,require_cold_copy=True);prepare=worker._prepare
    def subscribed_after_prepare(digest):
        token,reason=prepare(digest);name_in_subscription(s);return token,reason
    monkeypatch.setattr(worker,'_prepare',subscribed_after_prepare)
    report=worker.run_once(apply=True)
    # Now the chat keeps its originals: the 30-day rule applies again, under the lock before unlink.
    assert report['evicted']==0 and report['reasons']=={'recently_requested':1} and (s.files/blob.digest).exists()
    with s.pool.connection() as c:
        journal=c.execute('SELECT state,error_code FROM attachment_eviction_journal WHERE digest=%s',(blob.digest,)).fetchone()
        assert dict(journal)=={'state':'cancelled','error_code':'recently_requested'}


# spec: ANYM-806
def test_a_subscription_change_during_the_recheck_waits_for_the_unlink(archive,monkeypatch):
    import threading
    _,s,_,_=archive;fid,blob=chat_original(archive);api=lifecycle()
    token,sub=name_in_subscription(s)
    paused=s.update_subscription(token,sub['subscription_id'],1,action='pause')
    # Paused: the chat is on request again, so the original is in the class.
    worker=api.LifecycleWorker(s,require_cold_copy=True);eligible=worker._eligible;closed=threading.Event()
    def close():
        s.update_subscription(token,sub['subscription_id'],paused['version'],action='close');closed.set()
    def recheck(c,row):
        reason=eligible(c,row)
        if row['state']=='evicting':
            # _complete's recheck under the lifecycle lock, before unlink: a close (which starts the chat's
            # 24-hour grace) waits for this transaction instead of committing between the recheck and the unlink.
            threading.Thread(target=close,daemon=True).start()
            assert not closed.wait(1.0)
        return reason
    monkeypatch.setattr(worker,'_eligible',recheck)
    assert worker.run_once(apply=True)['evicted']==1
    assert closed.wait(10)


# spec: ANYM-806
def test_a_retention_release_locks_its_rows_before_the_lifecycle_lock(archive):
    import threading
    _,s,_,_=archive;api=lifecycle();chat_original(archive)
    token,sub=name_in_subscription(s);sid=sub['subscription_id']
    s.update_subscription(token,sid,1,action='close')
    with s.pool.connection() as c:c.execute('UPDATE subscriptions SET retention_hold=true WHERE id=%s',(sid,))
    released=threading.Event()
    with s.pool.connection() as c:
        # A reader holds the row (as _owned does) and then needs the lifecycle lock (as _state does).
        c.execute('SELECT id FROM subscriptions WHERE id=%s FOR UPDATE',(sid,))
        worker=threading.Thread(target=lambda:(s.release_retention_hold([str(sid)]),released.set()),daemon=True)
        worker.start()
        assert not released.wait(1.0)  # the release waits for the row, holding nothing the reader needs
        c.execute("SET LOCAL lock_timeout='5s'")
        api.lifecycle_lock(c)
    assert released.wait(10)
    with s.pool.connection() as c:
        assert c.execute('SELECT retention_hold FROM subscriptions WHERE id=%s',(sid,)).fetchone()['retention_hold'] is False


# spec: ANYM-806
def test_subscription_create_takes_no_lifecycle_lock(archive,monkeypatch):
    from messaging_service import delivery_endpoints, event_delivery, file_lifecycle
    _,s,_,_=archive;chat_original(archive);taken=[];real=file_lifecycle.lifecycle_lock
    def lock(c):
        taken.append('lifecycle');real(c)
    monkeypatch.setattr(file_lifecycle,'lifecycle_lock',lock)
    monkeypatch.setattr(event_delivery,'configure_subscription_delivery',
        lambda store,c,p,subscription_id,version,*_args:{'subscription_id':subscription_id,'version':version})
    monkeypatch.setattr(delivery_endpoints,'gate_on',lambda c:True)
    token=s.issue_key('lifecycle-delivery',['archive-test'],permissions=['read','subscription-write','delivery-write'],
        key_permissions=['read','subscription-write','delivery-write'])
    with s.pool.connection() as c:
        chat=c.execute("SELECT id FROM chats WHERE account='archive-test' AND provider_chat='100'").fetchone()['id']
    # Its source rows hold the chat's key, which ingest updates under the lifecycle lock: waiting for it could deadlock.
    s.create_subscription(token,[chat])
    s.create_subscription(token,[chat],delivery={'endpoint_id':'00000000-0000-4000-8000-000000000001'})
    assert taken==[]


# spec: ANYM-806
def test_a_create_racing_an_eviction_is_restored_from_r2_on_request(archive,monkeypatch):
    import threading
    from messaging_service import cold_archive
    _,s,key,_=archive;fid,blob=chat_original(archive);api=lifecycle()
    presence=cold_archive.WorkerPresence(s.pool.conninfo);assert presence.acquire()
    try:
        worker=api.LifecycleWorker(s,require_cold_copy=True);eligible=worker._eligible;created=threading.Event()
        def recheck(c,row):
            reason=eligible(c,row)
            if row['state']=='evicting':
                # The create commits after _complete's recheck and before the unlink: it does not wait for the lock.
                threading.Thread(target=lambda:(name_in_subscription(s),created.set()),daemon=True).start()
                assert created.wait(10)
            return reason
        monkeypatch.setattr(worker,'_eligible',recheck)
        assert worker.run_once(apply=True)['evicted']==1 and not (s.files/blob.digest).exists()
        # The subscribed chat's original comes back from the verified R2 copy, never from the provider.
        result=s.request_file_download(key,fid)
        assert result['request_status']=='queued' and result['rehydration_source']=='cold_archive'
        with s.pool.connection() as c:
            assert c.execute('SELECT restore_state FROM attachment_cold_copies WHERE digest=%s',(blob.digest,)).fetchone()['restore_state']=='requested'
            assert c.execute('SELECT state FROM media_jobs WHERE id=%s',(fid,)).fetchone()['state']=='ready'
    finally:
        presence.close()


# spec: ANYM-806
def test_chat_policy_dry_run_reports_what_bounded_apply_passes_then_remove(archive,capsys):
    _,s,_,_=archive;api=lifecycle()
    blobs=[chat_original(archive,mid=f'group-{n}',payload=f'Synthetic group photo {n}'.encode())[1] for n in range(3)]
    _,personal=chat_original(archive,mid='personal-old',chat='101',chat_type='private',idle_hours=31*24,
        payload=b'Synthetic old personal photo')
    dsn=['--dsn-file',os.environ['PROBE_TEST_DSN_FILE'],'--files',str(s.files),'--chat-policy-only']
    assert api.main(dsn)==0
    report=json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert report['dry_run'] is True and report['candidates']==3
    assert report['candidate_bytes']==sum(b.bytes for b in blobs) and report['evicted']==0
    assert all((s.files/b.digest).exists() for b in blobs+[personal])
    first=api.LifecycleWorker(s,require_cold_copy=True,chat_policy_only=True).run_once(apply=True,limit=2)
    assert first['evicted']==2
    second=api.LifecycleWorker(s,require_cold_copy=True,chat_policy_only=True).run_once(apply=True,max_bytes=blobs[0].bytes-1)
    assert second['evicted']==0 and second['reasons']=={'byte_limit':1}
    assert api.main([*dsn,'--apply'])==0 and '"evicted": 1' in capsys.readouterr().out
    assert not any((s.files/b.digest).exists() for b in blobs)
    # The 30-day rule of a personal original is not part of the chat policy pass.
    assert (s.files/personal.digest).exists()


def test_every_production_entry_point_requires_a_cold_copy(archive,monkeypatch,capsys):
    from pathlib import Path
    import yaml
    _,s,_,_=archive;_,blob=processed(archive);api=lifecycle();seen=[]
    real=api.LifecycleWorker
    def recording(*args,**kwargs):
        seen.append(kwargs.get('require_cold_copy'));return real(*args,**kwargs)
    monkeypatch.setattr(api,'LifecycleWorker',recording)
    for flags in ([],['--apply'],['--apply','--expire-unprocessed']):
        assert api.main(['--dsn-file',__import__('os').environ['PROBE_TEST_DSN_FILE'],'--files',str(s.files),*flags])==0
    assert seen==[True,True,True]
    assert '"cold_copy_required": true' in capsys.readouterr().out and (s.files/blob.digest).exists()
    root=Path(__file__).resolve().parents[1]
    # Compose runs only the CLI, and no other module constructs a worker.
    command=yaml.safe_load((root/'deploy/compose.file-lifecycle.yaml').read_text())['services']['file-lifecycle']['command']
    assert command[:3]==['python','-m','messaging_service.file_lifecycle']
    package=root/'services/messaging/messaging_service'
    assert [p.name for p in package.glob('*.py') if 'LifecycleWorker(' in p.read_text()]==['file_lifecycle.py']
