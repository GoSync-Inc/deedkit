"""Synthetic recovery inventory and durable original-fence contracts."""
import hashlib
import json
import sys
import uuid

import pytest

from messaging_service.backup import create_snapshot, verify_bundle, verify_snapshot


@pytest.fixture
def backup_seeded(seeded):
    store,*_=seeded
    with store.pool.connection() as c:
        c.execute("UPDATE attachment_residency SET state='resident',eviction_id=NULL,evicted_at=NULL")
        c.execute('DELETE FROM attachment_eviction_journal')
        c.execute('DELETE FROM attachment_backup_fences')
    return seeded


def absent_bundle(tmp_path, *, schema=34, **changes):
    directory=tmp_path/'bundle';directory.mkdir();(directory/'files').mkdir()
    dump=b'synthetic database dump';(directory/'database.dump').write_bytes(dump)
    manifest={'format':1,'schema_version':schema,'tables':{},'blobs':[],
        'dump_sha256':hashlib.sha256(dump).hexdigest(),
        'includes_provider_sessions':False,
        'absent_blobs':[{'digest':'a'*64,'bytes':17,'eviction_id':str(uuid.uuid4())}]}
    manifest.update(changes)
    (directory/'manifest.json').write_text(json.dumps(manifest))
    return directory,manifest


def test_managed_absent_inventory_is_a_restorable_explicit_schema34_bundle(tmp_path):
    directory,manifest=absent_bundle(tmp_path)
    assert verify_bundle(directory)==manifest


@pytest.mark.parametrize('changes',[
    {'schema_version':32},
    {'absent_blobs':[{'digest':'a'*64,'bytes':17}]},
    {'absent_blobs':[{'digest':'a'*64,'bytes':17,'eviction_id':'not-an-eviction'}]},
    {'absent_blobs':[{'digest':'a'*64,'bytes':-1,'eviction_id':str(uuid.uuid4())}]},
    {'absent_blobs':[{'digest':'a'*64,'bytes':17,'eviction_id':str(uuid.uuid4())}]*2},
    {'blobs':[{'digest':'a'*64,'bytes':17}]},
])
def test_absent_inventory_never_silently_omits_invalid_or_ambiguous_originals(tmp_path,changes):
    directory,_=absent_bundle(tmp_path,**changes)
    with pytest.raises((ValueError,RuntimeError)):
        verify_bundle(directory)


def test_reference_capture_retains_durable_fence_and_schema_inventory(backup_seeded,tmp_path):
    store,*_=backup_seeded
    create_snapshot(store,tmp_path/'capture',[sys.executable,'-c','print("synthetic dump")'],
                    reference_blobs=True,backup_fence='synthetic-reference-backup')
    manifest=verify_bundle(tmp_path/'capture',blob_root=store.files)
    assert manifest['schema_version']==63
    assert manifest['backup_fence']=='synthetic-reference-backup'
    assert {'attachment_residency','attachment_eviction_journal','attachment_backup_fences',
            'attachment_understanding','file_understanding_profiles'}<=set(manifest['tables'])
    with store.pool.connection() as c:
        assert c.execute('SELECT ended_at FROM attachment_backup_fences WHERE id=%s',
                         ('synthetic-reference-backup',)).fetchone()['ended_at'] is None


def mark_evicted(store):
    with store.pool.connection() as c:
        row=c.execute('SELECT digest,bytes FROM attachments ORDER BY digest LIMIT 1').fetchone()
        eviction=str(uuid.uuid4())
        c.execute("INSERT INTO attachment_eviction_journal(id,digest,bytes,root_device,root_inode,state,completed_at) VALUES(%s,%s,%s,%s,%s,'completed',clock_timestamp())",
                  (eviction,row['digest'],row['bytes'],store.files.stat().st_dev,store.files.stat().st_ino))
        c.execute("UPDATE attachment_residency SET state='evicted',eviction_id=%s,evicted_at=clock_timestamp() WHERE digest=%s",
                  (eviction,row['digest']))
    (store.files/row['digest']).unlink()
    return {**row,'eviction_id':eviction}


def test_snapshot_preserves_managed_absence_with_exact_restored_catalog(backup_seeded,tmp_path):
    store,*_=backup_seeded;expected=mark_evicted(store)
    create_snapshot(store,tmp_path/'capture',[sys.executable,'-c','print("synthetic dump")'])
    manifest=verify_bundle(tmp_path/'capture')
    assert manifest['blobs']==[] and manifest['absent_blobs']==[expected]
    assert verify_snapshot(store,tmp_path/'capture')['status']=='verified'
    manifest['absent_blobs']=[]
    (tmp_path/'capture/manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError,match='restored_blob_inventory_mismatch'):
        verify_snapshot(store,tmp_path/'capture')


def test_unexpected_missing_original_abandons_only_owned_capture_fence(backup_seeded,tmp_path):
    store,*_=backup_seeded
    with store.pool.connection() as c:
        row=c.execute('SELECT digest FROM attachments LIMIT 1').fetchone()
    (store.files/row['digest']).unlink()
    with pytest.raises(RuntimeError,match='blob_corrupt'):
        create_snapshot(store,tmp_path/'capture',[sys.executable,'-c','print("synthetic dump")'],
                        backup_fence='synthetic-failed-backup')
    with store.pool.connection() as c:
        assert c.execute('SELECT ended_at IS NOT NULL AS closed FROM attachment_backup_fences WHERE id=%s',
                         ('synthetic-failed-backup',)).fetchone()['closed']
    assert not (tmp_path/'capture').exists()


def test_reference_retry_rejects_unfenced_pre_lifecycle_capture(backup_seeded):
    from messaging_service import backup
    store,*_=backup_seeded
    validate=getattr(backup,'validate_reference_fence',None)
    assert validate is not None,'reference retry needs a durable-fence admission gate'
    with pytest.raises(ValueError,match='backup_capture_unfenced'):
        validate(store,{'schema_version':32},'synthetic-legacy-backup')


@pytest.mark.parametrize('mode',['staged','stream'])
def test_release_requires_exact_confirmed_receiver_receipt(backup_seeded,tmp_path,mode):
    from messaging_service import backup_worker
    from messaging_service.backup_runner import write_json
    store,*_=backup_seeded;backup_id='20261016T000000Z-0123456789abcdef'
    with store.pool.connection() as c:c.execute('SELECT public.begin_backup_fence(%s)',(backup_id,))
    work=tmp_path/'work';work.mkdir(mode=0o700)
    digest='b'*64
    result={'v':1,'id':backup_id,'status':'encrypted','bytes':100,'sha256':digest,'backup_fence':backup_id}
    if mode=='staged':write_json(work/'result.json',result)
    else:write_json(work/'stream.json',{'v':2,'id':backup_id,'snapshot_sha256':'c'*64,
        'max_bytes':1024,'captured_at':'2026-10-16T00:00:00+00:00','backup_fence':backup_id})
    release=getattr(backup_worker,'release_delivered_fence',None)
    assert release is not None,'encrypted completion must not release the original fence'
    with pytest.raises(FileNotFoundError):release(store,work,backup_id)
    with store.pool.connection() as c:
        assert c.execute('SELECT ended_at FROM attachment_backup_fences WHERE id=%s',(backup_id,)).fetchone()['ended_at'] is None
    receipt={**result,'v':1 if mode=='staged' else 2,'status':'stored','snapshot_sha256':'c'*64,'duplicate':True}
    write_json(work/'delivery.json',{**receipt,'id':'20261016T000000Z-1111111111111111'})
    with pytest.raises(ValueError,match='invalid_backup_receipt'):release(store,work,backup_id)
    write_json(work/'delivery.json',receipt)
    assert release(store,work,backup_id)['status']=='fence_released'
    assert release(store,work,backup_id)['status']=='fence_released'
    with store.pool.connection() as c:
        assert c.execute('SELECT ended_at FROM attachment_backup_fences WHERE id=%s',(backup_id,)).fetchone()['ended_at'] is not None


def test_worker_reuses_exact_reference_fence_without_releasing_on_duplicate_encryption_skip(backup_seeded,tmp_path):
    from messaging_service.backup_worker import run
    from messaging_service.backup_runner import write_json
    store,*_=backup_seeded;backup_id='20261016T000000Z-fedcba9876543210'
    work=tmp_path/'work';work.mkdir(mode=0o700)
    create_snapshot(store,work/'snapshot',[sys.executable,'-c','print("synthetic dump")'],
                    reference_blobs=True,backup_fence=backup_id)
    write_json(work/'stream-start.json',{'action':'skip'})
    write_json(work/'recovery.json',{})
    config={'id':backup_id,'dsn_file':__import__('os').environ['PROBE_TEST_DSN_FILE'],
            'files_directory':str(store.files),'recovery_config_file':str(work/'recovery.json'),
            'recipient_file':str(work/'unused-recipient'),'work_directory':str(work),'mode':'stream_v2'}
    assert run(config)['status']=='already_delivered'
    metadata=json.loads((work/'stream.json').read_text())
    assert metadata['backup_fence']==backup_id
    with store.pool.connection() as c:
        assert c.execute('SELECT ended_at FROM attachment_backup_fences WHERE id=%s',(backup_id,)).fetchone()['ended_at'] is None


@pytest.mark.parametrize('delivered',[False,True])
def test_host_releases_reference_fence_only_after_durable_matching_delivery(tmp_path,delivered):
    from messaging_service.backup_runner import Runner,read_json,write_json
    from test_backup_streaming import StreamingDocker,streaming_runner
    from test_backup_operations import FakeDocker
    class FencedDocker(StreamingDocker):
        def call(self,*args):
            if args[0]=='start' and args[1].startswith('pos-backup-release-'):
                info=self.containers[args[1]];work=self.root/info['Config']['Labels']['personal-os.backup.id']
                assert read_json(work/'delivery.json')['status']=='stored'
                assert read_json(work/'fence-release.json')['action']=='release_fence'
                write_json(work/'fence-released.json',{'id':work.name,'backup_fence':work.name,'status':'fence_released'})
                info['State'].update(Running=False,Status='exited',ExitCode=0)
                self.calls.append(args);return ''
            result=super().call(*args)
            if args[0]=='start' and args[1].startswith('pos-backup-'):
                info=self.containers[args[1]];work=self.root/info['Config']['Labels']['personal-os.backup.id']
                write_json(work/'stream.json',{**read_json(work/'stream.json'),'backup_fence':work.name})
            return result
        def inspect(self,cid):
            if cid.startswith('pos-backup-release-'):return FakeDocker.inspect(self,cid)
            return super().inspect(cid)
    async def send(path,metadata,transfer,timeout,start,completed,stopped=None):
        if not delivered:raise RuntimeError('receiver_quota')
        start(True);result=await completed()
        return {**result,'status':'stored','duplicate':False}
    baseline,_=streaming_runner(tmp_path,send);docker=FencedDocker(tmp_path)
    runner=Runner(baseline.cfg,docker=docker,sleep=lambda _:None,stream_send=send)
    if delivered:runner.run()
    else:
        with pytest.raises(RuntimeError,match='receiver_quota'):runner.run()
    releases=[call for call in docker.calls if call[0]=='create' and 'pos-backup-release-' in ' '.join(call)]
    assert bool(releases)==delivered
    if delivered:
        release=releases[0]
        assert not any('/sources/' in arg or ',dst=/data,' in arg or arg.endswith(',dst=/data') or '/recipient' in arg or 'identity' in arg for arg in release)
        assert 'no-new-privileges' in release
        assert not any(name.startswith('pos-backup-release-') for name in docker.containers)


def test_committed_release_marker_does_not_leave_its_owned_container_after_crash(tmp_path):
    from messaging_service.backup_runner import write_json
    from test_backup_operations import runner_fixture
    runner,docker=runner_fixture(tmp_path);runner.prepare()
    work=tmp_path/runner.journal['id'];name='pos-backup-release-'+work.name.lower()
    write_json(work/'stream.json',{'backup_fence':work.name})
    write_json(work/'fence-released.json',{'id':work.name,'backup_fence':work.name,'status':'fence_released'})
    docker.containers[name]={'Id':name,'Config':{'Labels':{'personal-os.backup.id':work.name,
        'personal-os.backup.project':'test-project','personal-os.backup.action':'release_fence'}},
        'State':{'Running':False,'Status':'exited','ExitCode':0}}
    runner.release_fence(work)
    assert name not in docker.containers


def test_reference_retry_keeps_captured_absence_when_source_is_later_rehydrated(backup_seeded,tmp_path):
    store,*_=backup_seeded
    with store.pool.connection() as c:row=c.execute('SELECT digest FROM attachments LIMIT 1').fetchone()
    original=(store.files/row['digest']).read_bytes();expected=mark_evicted(store)
    create_snapshot(store,tmp_path/'capture',[sys.executable,'-c','print("synthetic dump")'],reference_blobs=True)
    (store.files/row['digest']).write_bytes(original)
    with store.pool.connection() as c:
        c.execute("UPDATE attachment_residency SET state='resident',eviction_id=NULL,evicted_at=NULL WHERE digest=%s",(row['digest'],))
    assert verify_bundle(tmp_path/'capture',blob_root=store.files)['absent_blobs']==[expected]


def test_concurrent_schema_upgrade_cannot_create_unfenced_lifecycle_capture(backup_seeded,tmp_path):
    from contextlib import contextmanager
    from types import SimpleNamespace
    store,*_=backup_seeded
    with store.pool.connection() as c:c.execute("UPDATE probe_metadata SET value='32' WHERE name='schema_version'")
    class UpgradePool:
        calls=0
        @contextmanager
        def connection(self):
            with store.pool.connection() as c:yield c
            self.calls+=1
            if self.calls==1:
                with store.pool.connection() as c:
                    c.execute("UPDATE probe_metadata SET value='63' WHERE name='schema_version'")
    with pytest.raises(RuntimeError,match='backup_capture_unfenced'):
        create_snapshot(SimpleNamespace(pool=UpgradePool(),files=store.files),tmp_path/'capture',
                        [sys.executable,'-c','print("synthetic dump")'])


def test_quarantine_releases_restored_backup_fences_after_verification(backup_seeded,tmp_path):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from messaging_service.backup import quarantine_restore
    from messaging_service.store import Store
    source,*_=backup_seeded;name='lifecycle_'+uuid.uuid4().hex+'_restore_test';restored=None
    with psycopg.connect(source.pool.conninfo,autocommit=True) as c:
        c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        restored=Store(make_conninfo(source.pool.conninfo,dbname=name),tmp_path/'originals','http://testserver')
        restored.migrate();restored.seed()
        with restored.pool.connection() as c:c.execute("SELECT public.begin_backup_fence('synthetic-restored-fence')")
        quarantine_restore(restored)
        with restored.pool.connection() as c:
            assert c.execute("SELECT ended_at IS NOT NULL AS ended FROM attachment_backup_fences WHERE id='synthetic-restored-fence'").fetchone()['ended']
    finally:
        if restored:restored.close()
        with psycopg.connect(source.pool.conninfo,autocommit=True) as c:
            c.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


def test_uncertain_fence_commit_keeps_originals_protected_for_reconciliation(backup_seeded,tmp_path):
    from contextlib import contextmanager
    from types import SimpleNamespace
    store,*_=backup_seeded;token='synthetic-uncertain-commit'
    class AmbiguousPool:
        calls=0
        @contextmanager
        def connection(self):
            with store.pool.connection() as c:yield c
            self.calls+=1
            if self.calls==1:raise RuntimeError('uncertain_commit')
    with pytest.raises(RuntimeError,match='uncertain_commit'):
        create_snapshot(SimpleNamespace(pool=AmbiguousPool(),files=store.files),tmp_path/'capture',
                        [sys.executable,'-c','print("synthetic dump")'],backup_fence=token)
    with store.pool.connection() as c:
        assert c.execute('SELECT ended_at IS NULL AS active FROM attachment_backup_fences WHERE id=%s',(token,)).fetchone()['active']


def test_schema35_absence_names_the_verified_cold_copy(backup_seeded,tmp_path):
    store,*_=backup_seeded
    with store.pool.connection() as c:c.execute('DELETE FROM attachment_cold_copies')
    expected=mark_evicted(store);key='synthetic-owner/cold/'+expected['digest']+'.age'
    with store.pool.connection() as c:
        c.execute('''INSERT INTO attachment_cold_copies(digest,bytes,state,object_key,ciphertext_bytes,ciphertext_sha256,
            recipient_fingerprints,verified_at) VALUES(%s,%s,'verified',%s,%s,%s,%s,clock_timestamp())''',
            (expected['digest'],expected['bytes'],key,expected['bytes']+200,'c'*64,['d'*64,'e'*64]))
    try:
        create_snapshot(store,tmp_path/'capture',[sys.executable,'-c','print("synthetic dump")'])
        manifest=verify_bundle(tmp_path/'capture')
        assert manifest['schema_version']==63 and manifest['tables']['attachment_cold_copies']['rows']==1
        assert manifest['absent_blobs']==[{**expected,'cold_object_key':key}]
        assert verify_snapshot(store,tmp_path/'capture')['status']=='verified'
        # The restore check still requires the managed-absent original to be absent.
        (store.files/expected['digest']).write_bytes(b'x')
        with pytest.raises(RuntimeError,match='backup_absence_mismatch|restored_blob_inventory_mismatch'):
            verify_snapshot(store,tmp_path/'capture')
        (store.files/expected['digest']).unlink()
    finally:
        with store.pool.connection() as c:c.execute('DELETE FROM attachment_cold_copies')


@pytest.mark.parametrize('cold_key',[None,7,'synthetic-owner/objects/x.age','synthetic-owner/cold/'+'b'*64+'.age','../cold/'+'a'*64+'.age'])
def test_schema35_absent_entry_rejects_an_invalid_cold_key(tmp_path,cold_key):
    entry={'digest':'a'*64,'bytes':17,'eviction_id':str(uuid.uuid4()),'cold_object_key':cold_key}
    directory,_=absent_bundle(tmp_path,schema=35,absent_blobs=[entry])
    with pytest.raises(ValueError,match='invalid_backup_manifest'):verify_bundle(directory)
    for schema,key in ((35,'synthetic-owner/cold/'+'a'*64+'.age'),(34,None)):
        path=tmp_path/str(schema);path.mkdir()
        valid={k:v for k,v in entry.items() if k!='cold_object_key'}|({'cold_object_key':key} if key else {})
        assert verify_bundle(absent_bundle(path,schema=schema,absent_blobs=[valid])[0])['absent_blobs']==[valid]


def test_invalid_capture_fence_is_rejected_without_cleanup_masking_the_error(backup_seeded,tmp_path):
    store,*_=backup_seeded
    with pytest.raises(ValueError,match='invalid_backup_fence'):
        create_snapshot(store,tmp_path/'capture',[sys.executable,'-c','print("synthetic dump")'],backup_fence='../invalid')


OTHER_FENCE='synthetic-other-backup'


def fence_ended(store,token):
    with store.pool.connection() as c:
        row=c.execute('SELECT ended_at FROM attachment_backup_fences WHERE id=%s',(token,)).fetchone()
    return None if row is None else row['ended_at'] is not None


def abandon_runner(store,tmp_path,mode='staged',*,begin=True):
    """A capture stopped during pg_dump: its fence is open, only a private temporary exists."""
    import os
    import shutil
    from messaging_service import backup_worker
    from messaging_service.backup_runner import read_json
    from test_backup_operations import runner_fixture
    runner,docker=runner_fixture(tmp_path)
    if mode=='stream':runner.cfg={**runner.cfg,'delivery_mode':'stream_v2'}
    runner.prepare();work=tmp_path/runner.journal['id']
    docker.call('stop','--time','25','tg-id');runner.journal['phase']='capturing';runner.save()
    runner.create_worker(work);docker.call('start',runner.journal['container'])
    for name in ('snapshot','captured.json'):  # The fake worker publishes at start; this one is still in pg_dump.
        path=work/name;path.unlink() if path.is_file() else shutil.rmtree(path)
    (work/'.backup-pg').mkdir();(work/'.backup-pg/database.dump').write_bytes(b'PARTIAL_SYNTHETIC_DUMP')
    docker.hold_worker=True
    with store.pool.connection() as c:
        if begin:c.execute('SELECT public.begin_backup_fence(%s)',(work.name,))
        c.execute('SELECT public.begin_backup_fence(%s)',(OTHER_FENCE,))
    def isolated(work):
        # The runner's own container config, with only its two secret paths mapped.
        assert sorted(p.name for p in work.iterdir())==['fence-abandon.json']
        config=read_json(work/'fence-abandon.json')
        assert config=={'action':'abandon_fence','id':work.name,'dsn_file':'/run/secrets/database_url','work_directory':'/work'}
        try:backup_worker.run({**config,'dsn_file':os.environ['PROBE_TEST_DSN_FILE'],'work_directory':str(work)})
        except Exception:return 1
        return 0
    docker.fence_worker=isolated
    return runner,docker,work


# spec: ANYM-305
@pytest.mark.parametrize('mode',['staged','stream'])
def test_recover_stop_ends_the_fence_of_a_capture_stopped_during_pg_dump(backup_seeded,tmp_path,mode):
    from messaging_service.backup_runner import read_json
    from messaging_service.file_lifecycle import LifecycleWorker
    store,*_=backup_seeded;runner,docker,work=abandon_runner(store,tmp_path,mode)
    with store.pool.connection() as c:c.execute('SELECT public.end_backup_fence(%s)',(OTHER_FENCE,))
    assert LifecycleWorker(store).run_once()['reasons'].get('backup_active')==1
    runner.recover_stop()
    assert fence_ended(store,work.name) is True
    assert 'backup_active' not in LifecycleWorker(store).run_once()['reasons']
    assert not work.exists() and not (tmp_path/'journal.json').exists()
    assert docker.containers['tg-id']['State']['Running']
    status=read_json(tmp_path/'status.json')
    assert status['abandoned_id']==work.name and status['abandoned_fence']=='fence_released'
    release=[call for call in docker.calls if call[0]=='create' and 'pos-backup-abandon-'+work.name.lower() in call][0]
    assert not any('/sources/' in arg or ',dst=/data,' in arg or '/recipient' in arg or 'identity' in arg for arg in release)
    assert 'no-new-privileges' in release and 'personal-os.backup.action=abandon_fence' in release
    assert not any(name.startswith('pos-backup-') for name in docker.containers)


# spec: ANYM-305
def test_recover_stop_never_ends_a_fence_while_the_capture_may_still_run(backup_seeded,tmp_path):
    store,*_=backup_seeded;runner,docker,work=abandon_runner(store,tmp_path)
    original=docker.call
    def stuck(*args):
        if args[0]=='stop' and args[-1]==runner.journal['container']:docker.calls.append(args);return ''
        return original(*args)
    docker.call=stuck
    with pytest.raises(RuntimeError,match='backup_shutdown_unverified'):runner.recover_stop()
    assert fence_ended(store,work.name) is False and (tmp_path/'journal.json').exists() and work.is_dir()
    assert not any(call[0]=='create' and 'pos-backup-abandon-' in ' '.join(call) for call in docker.calls)


# spec: ANYM-305
@pytest.mark.parametrize('mode',['staged','stream'])
def test_recover_stop_keeps_a_capture_published_before_the_phase_update(backup_seeded,tmp_path,mode):
    from messaging_service.backup_runner import Runner,read_json
    from test_backup_operations import runner_fixture
    from test_backup_streaming import streaming_runner
    store,*_=backup_seeded
    async def stream_send(path,metadata,transfer,timeout,start,completed,stopped=None):
        start(True);result=await completed()
        return {**result,'status':'stored','duplicate':False}
    runner,docker=streaming_runner(tmp_path,stream_send) if mode=='stream' else runner_fixture(tmp_path)
    runner.prepare();work=tmp_path/runner.journal['id']
    with store.pool.connection() as c:c.execute('SELECT public.begin_backup_fence(%s)',(work.name,))
    published='upload' if mode=='staged' else 'stream_upload'
    original=runner.save
    def terminated():
        # SIGTERM lands after the worker published, before the runner records it.
        if runner.journal['phase']==published:raise InterruptedError('backup_interrupted')
        original()
    runner.save=terminated
    with pytest.raises(InterruptedError):runner.run()
    assert read_json(tmp_path/'journal.json')['phase']=='capturing'
    artifact=work/('result.json' if mode=='staged' else 'snapshot/private')
    before=artifact.read_bytes()
    runner2=Runner(runner.cfg,docker=docker,sleep=lambda _:None,send=runner.send,stream_send=runner.stream_send)
    runner2.recover_stop()
    assert fence_ended(store,work.name) is False
    assert read_json(tmp_path/'journal.json')['id']==work.name and artifact.read_bytes()==before
    assert not any(call[0]=='create' and 'pos-backup-abandon-' in ' '.join(call) for call in docker.calls)
    assert docker.containers['tg-id']['State']['Running']
    # The retry delivers the same capture instead of a new cold snapshot.
    creates=sum(call[0]=='create' for call in docker.calls)
    runner2.run()
    assert read_json(tmp_path/'status.json')['state']=='ok' and (work/'delivery.json').is_file()
    assert sum(call[0]=='create' for call in docker.calls)==creates+(mode=='stream')


# spec: ANYM-305
def test_stream_upload_keeps_its_fence_for_retry_after_recover_stop(backup_seeded,tmp_path):
    from messaging_service.backup_runner import read_json
    from test_backup_streaming import streaming_runner
    store,*_=backup_seeded
    async def send(path,metadata,transfer,timeout,start,completed,stopped=None):raise RuntimeError('receiver_quota')
    runner,docker=streaming_runner(tmp_path,send);runner.prepare();work=tmp_path/runner.journal['id']
    with store.pool.connection() as c:c.execute('SELECT public.begin_backup_fence(%s)',(work.name,))
    with pytest.raises(RuntimeError,match='receiver_quota'):runner.run()
    runner.recover_stop()
    assert fence_ended(store,work.name) is False and read_json(tmp_path/'journal.json')['phase']=='stream_upload'
    assert (work/'snapshot').is_dir()


# spec: ANYM-305
@pytest.mark.parametrize('fenced',[True,False])
def test_abandonment_is_idempotent_by_job_id_and_ends_no_other_fence(backup_seeded,tmp_path,fenced):
    from messaging_service.backup_runner import read_json
    store,*_=backup_seeded;runner,docker,work=abandon_runner(store,tmp_path,begin=fenced)
    isolated=docker.fence_worker
    def restart(work):
        # The release commits, then the host dies before it reads the marker or cleans up.
        assert isolated(work)==0;raise InterruptedError('backup_interrupted')
    docker.fence_worker=restart
    with pytest.raises(RuntimeError,match='backup_fence_retained'):runner.recover_stop()
    assert read_json(tmp_path/'journal.json')['phase']=='abandoning'
    expected='fence_released' if fenced else 'fence_absent'
    assert read_json(work/'fence-abandoned.json')['status']==expected
    docker.fence_worker=isolated;runner.recover_stop()
    status=read_json(tmp_path/'status.json')
    assert status['abandoned_id']==work.name and status['abandoned_fence']==expected
    runner.recover_stop()
    assert read_json(tmp_path/'status.json')==status
    assert fence_ended(store,work.name) is (True if fenced else None)
    assert fence_ended(store,OTHER_FENCE) is False
    assert not work.exists() and not (tmp_path/'journal.json').exists()
    assert not any(name.startswith('pos-backup-') for name in docker.containers)


# spec: ANYM-305
@pytest.mark.parametrize('failure',['release_failed','acknowledgement_lost'])
def test_unconfirmed_release_retains_the_journal_until_a_read_back(backup_seeded,tmp_path,failure):
    from messaging_service.backup_runner import read_json
    store,*_=backup_seeded;runner,docker,work=abandon_runner(store,tmp_path)
    isolated=docker.fence_worker
    def failing(work):
        if failure=='release_failed':return 1
        assert isolated(work)==0;(work/'fence-abandoned.json').unlink();return 1
    docker.fence_worker=failing
    with pytest.raises(RuntimeError,match='backup_fence_retained'):runner.recover_stop()
    status=read_json(tmp_path/'status.json')
    assert status['state']=='error' and status['error_code']=='backup_fence_retained'
    assert read_json(tmp_path/'journal.json')['phase']=='abandoning' and work.is_dir()
    assert fence_ended(store,work.name) is (failure=='acknowledgement_lost')
    assert docker.containers['tg-id']['State']['Running']
    docker.fence_worker=isolated
    if failure=='release_failed':runner.recover_stop()
    else:
        # The next scheduled run reads the outcome back before a new capture.
        docker.hold_worker=False;runner.run()
    status=read_json(tmp_path/'status.json')
    assert status['error_code' if failure=='release_failed' else 'state']==('backup_job_failed' if failure=='release_failed' else 'ok')
    assert status['abandoned_id']==work.name and status['abandoned_fence']=='fence_released'
    assert fence_ended(store,work.name) is True and fence_ended(store,OTHER_FENCE) is False
    assert not work.exists()


# spec: ANYM-305
def test_abandon_action_needs_only_the_dsn_and_job_id(backup_seeded,tmp_path):
    import os
    from messaging_service.backup_worker import run
    store,*_=backup_seeded;backup_id='20261019T000000Z-0123456789abcdef'
    work=tmp_path/'work';work.mkdir(mode=0o700)
    config={'action':'abandon_fence','id':backup_id,'dsn_file':os.environ['PROBE_TEST_DSN_FILE'],'work_directory':str(work)}
    with store.pool.connection() as c:
        c.execute('SELECT public.begin_backup_fence(%s)',(backup_id,))
        c.execute('SELECT public.begin_backup_fence(%s)',(OTHER_FENCE,))
    for extra in ({'files_directory':str(store.files)},{'mode':'stream_v2'}):
        with pytest.raises(ValueError,match='invalid_backup_worker_config'):run({**config,**extra})
    with pytest.raises(ValueError,match='invalid_backup_id'):run({**config,'id':OTHER_FENCE})
    assert fence_ended(store,backup_id) is False
    assert run(config)['status']=='fence_released' and run(config)['status']=='fence_released'
    assert fence_ended(store,backup_id) is True and fence_ended(store,OTHER_FENCE) is False
    assert sorted(p.name for p in work.iterdir())==['fence-abandoned.json']


# spec: ANYM-305
@pytest.mark.parametrize('helper',['created','running'])
def test_restart_settles_a_leftover_abandon_helper_before_reading_back(backup_seeded,tmp_path,helper):
    from messaging_service.backup_runner import read_json
    store,*_=backup_seeded;runner,docker,work=abandon_runner(store,tmp_path)
    name='pos-backup-abandon-'+work.name.lower();isolated=docker.fence_worker;original=docker.call
    def host_dies(*args):
        # The host dies after creating the helper, or while the helper still runs.
        if helper=='created' and args[:2]==('start',name):raise InterruptedError('backup_interrupted')
        return original(*args)
    def still_running(work):raise InterruptedError('backup_interrupted')
    docker.call=host_dies
    if helper=='running':docker.fence_worker=still_running
    with pytest.raises(RuntimeError,match='backup_fence_retained'):runner.recover_stop()
    assert docker.containers[name]['State']['Running'] is (helper=='running')
    docker.call=original;docker.fence_worker=isolated;docker.hold_worker=False
    runner.run()  # The next scheduled run reads back, then takes a new capture.
    status=read_json(tmp_path/'status.json')
    assert status['state']=='ok' and status['abandoned_id']==work.name and status['abandoned_fence']=='fence_released'
    assert fence_ended(store,work.name) is True and fence_ended(store,OTHER_FENCE) is False
    assert ('rm',name) in docker.calls and not work.exists()


# spec: ANYM-305
def test_read_back_clears_the_retained_error_before_the_journal_goes(backup_seeded,tmp_path,monkeypatch):
    from pathlib import Path
    from messaging_service.backup_runner import read_json
    store,*_=backup_seeded;runner,docker,work=abandon_runner(store,tmp_path)
    isolated=docker.fence_worker;docker.fence_worker=lambda work:1
    with pytest.raises(RuntimeError,match='backup_fence_retained'):runner.recover_stop()
    docker.fence_worker=isolated;unlink=Path.unlink
    def crash(path,*args,**kwargs):
        if path==tmp_path/'journal.json':raise InterruptedError('backup_interrupted')
        return unlink(path,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',crash)
    with pytest.raises(InterruptedError):runner.recover_stop()
    status=read_json(tmp_path/'status.json')
    assert status['error_code']=='backup_job_failed' and status['abandoned_fence']=='fence_released'
    monkeypatch.setattr(Path,'unlink',unlink);runner.recover_stop()
    assert read_json(tmp_path/'status.json')['error_code']=='backup_job_failed' and not (tmp_path/'journal.json').exists()
    assert fence_ended(store,work.name) is True
