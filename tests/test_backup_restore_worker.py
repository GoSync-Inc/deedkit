"""Synthetic streaming restore boundaries; never connects a provider or live DB."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace

import pytest

from messaging_service import backup_crypto as crypto


def bundle():
    dump=b'UNIT_DUMP_NOT_A_DATABASE'
    manifest={'format':1,'schema_version':2,'dump_sha256':hashlib.sha256(dump).hexdigest(),'tables':{},'blobs':[]}
    out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w') as archive:
        for name,data in [('manifest.json',json.dumps(manifest).encode()),('database.dump',dump)]:
            item=tarfile.TarInfo(name);item.size=len(data);archive.addfile(item,io.BytesIO(data))
    return out.getvalue(),manifest


@pytest.fixture
def decoder(tmp_path):
    path=tmp_path/'age-synthetic'
    path.write_text('#!'+sys.executable+'\nimport sys,shutil\nshutil.copyfileobj(sys.stdin.buffer,sys.stdout.buffer)\n')
    path.chmod(0o700)
    return str(path)


def test_stream_decryption_extracts_verified_bundle_without_ciphertext_file(tmp_path,decoder):
    data,manifest=bundle()
    result=crypto.decrypt_snapshot_stream(io.BytesIO(data),'synthetic-key',tmp_path/'restore',age=decoder,max_bytes=len(data))
    assert json.loads((result/'manifest.json').read_text())==manifest
    assert sorted(p.name for p in tmp_path.iterdir() if p.name!='processing-locks')==['age-synthetic','restore']


def test_stream_decryption_rejects_late_decoder_auth_failure(tmp_path):
    data,_=bundle();decoder=tmp_path/'age-fails'
    decoder.write_text('#!'+sys.executable+'\nimport sys,shutil\nshutil.copyfileobj(sys.stdin.buffer,sys.stdout.buffer)\nsys.exit(1)\n');decoder.chmod(0o700)
    with pytest.raises(RuntimeError,match='backup_decryption_failed'):
        crypto.decrypt_snapshot_stream(io.BytesIO(data),'key',tmp_path/'restore',age=str(decoder))
    assert not (tmp_path/'restore').exists() and not list(tmp_path.glob('.decrypt-*'))


def test_stream_decryption_propagates_source_failure_before_publication(tmp_path,decoder):
    data,_=bundle()
    class Broken(io.BytesIO):
        def read(self,size=-1):
            chunk=super().read(size)
            if not chunk:raise OSError('synthetic upstream disconnect')
            return chunk
    with pytest.raises(OSError):
        crypto.decrypt_snapshot_stream(Broken(data),'key',tmp_path/'restore',age=decoder)
    assert not (tmp_path/'restore').exists()


def test_worker_has_no_provider_or_store_initialization():
    from messaging_service import backup_restore_worker as worker
    assert callable(worker.run)
    assert worker.DATABASE.endswith('_restore_test')


def test_quarantine_assertion_uses_actual_text_array_policy_type():
    from messaging_service import backup_restore_worker as worker
    queries=[]
    class Connection:
        def execute(self,query):
            queries.append(query)
            if "operations_enabled" in query:
                assert "'{}'::text[]" in query
            return SimpleNamespace(fetchone=lambda:{'name':worker.DATABASE} if 'current_database' in query else None)
    class Lease:
        def __enter__(self):return Connection()
        def __exit__(self,*args):pass
    worker.assert_quarantined(SimpleNamespace(pool=SimpleNamespace(connection=Lease)),34)
    assert any('subscription_receipts' in q for q in queries)
    assert any('attachment_backup_fences' in q for q in queries)


@pytest.mark.parametrize('unsafe_table',['account_policies','api_keys','collector_leases','source_status','outgoing_messages',
 'messenger_commands','subscriptions','file_uploads','subscription_receivers','subscription_outbox','subscription_receipts','attachment_backup_fences'])
def test_each_unsafe_restored_authority_prevents_quarantine_acceptance(unsafe_table):
    from messaging_service import backup_restore_worker as worker
    class Connection:
        def execute(self,query):
            row={'name':worker.DATABASE} if 'current_database' in query else ({'unsafe':True} if 'FROM '+unsafe_table+' WHERE' in query else None)
            return SimpleNamespace(fetchone=lambda:row)
    class Lease:
        def __enter__(self):return Connection()
        def __exit__(self,*args):pass
    with pytest.raises(RuntimeError,match='restore_quarantine_failed'):
        worker.assert_quarantined(SimpleNamespace(pool=SimpleNamespace(connection=Lease)),34)


@pytest.mark.parametrize('code',['restore_decryption_failed','restore_database_failed','restore_snapshot_failed','restore_quarantine_failed'])
def test_worker_stage_failure_exposes_only_fixed_safe_code(code):
    from messaging_service import backup_restore_worker as worker
    def secret_failure():raise OSError('SYNTHETIC_PASSWORD_DO_NOT_PERSIST')
    with pytest.raises(worker.RestoreStageError) as caught:worker.stage(code,secret_failure)
    assert str(caught.value)==code


def test_large_stream_keeps_original_ciphertext_source_distinct_from_tar_members(tmp_path,decoder):
    import os,signal,subprocess
    content=(b'SYNTHETIC_LARGE_ORIGINAL\n'*(6*1024**2//25+1))[:6*1024**2]
    digest=hashlib.sha256(content).hexdigest();dump=b'UNIT_DUMP_NOT_A_DATABASE'
    manifest={'format':1,'schema_version':2,'dump_sha256':hashlib.sha256(dump).hexdigest(),
              'tables':{},'blobs':[{'digest':digest,'bytes':len(content)}]}
    source=tmp_path/'synthetic.tar'
    with tarfile.open(source,'w') as archive:
        for name,data in [('manifest.json',json.dumps(manifest).encode()),('database.dump',dump),('files/'+digest,content)]:
            member=tarfile.TarInfo(name);member.size=len(data);archive.addfile(member,io.BytesIO(data))
    script='from messaging_service.backup_crypto import decrypt_snapshot_stream; import sys\nwith open(sys.argv[1],"rb") as source: decrypt_snapshot_stream(source,"synthetic",sys.argv[2],age=sys.argv[3])'
    process=subprocess.Popen([sys.executable,'-c',script,str(source),str(tmp_path/'large-restore'),decoder],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    try:
        try:stdout,stderr=process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);stdout,stderr=process.communicate(timeout=5)
            pytest.fail('large ciphertext stream stalled after tar member extraction')
        assert process.returncode==0,stderr.decode(errors='replace')
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)
    assert hashlib.sha256((tmp_path/'large-restore/files'/digest).read_bytes()).hexdigest()==digest


# The id '36-36' is a Tiden case ID; since schema 36 the first unsupported schema is 37.
@pytest.mark.parametrize('actual,manifest',[('34',31),('31',34),pytest.param('37',37,id='36-36'),('corrupt',34),(None,34)])
def test_restored_database_schema_must_match_manifest_before_quarantine_or_proof(actual,manifest):
    from messaging_service import backup_restore_worker as worker
    class Lease:
        def __enter__(self):return SimpleNamespace(execute=lambda query:SimpleNamespace(fetchone=lambda:({'value':actual} if actual is not None else None)))
        def __exit__(self,*args):pass
    store=SimpleNamespace(pool=SimpleNamespace(connection=Lease))
    with pytest.raises(ValueError,match='restored_schema_mismatch'):worker.restored_schema_version(store,manifest)


def test_restored_database_schema_reports_actual_verified_version():
    from messaging_service import backup_restore_worker as worker
    class Lease:
        def __enter__(self):return SimpleNamespace(execute=lambda query:SimpleNamespace(fetchone=lambda:{'value':'34'}))
        def __exit__(self,*args):pass
    assert worker.restored_schema_version(SimpleNamespace(pool=SimpleNamespace(connection=Lease)),34)==34


def test_worker_mismatched_dump_schema_never_enters_inventory_quarantine_or_result(tmp_path,monkeypatch):
    import psycopg_pool
    from messaging_service import backup_restore_worker as worker
    data,manifest=bundle();manifest['schema_version']=31
    restored=tmp_path/'verified-bundle';restored.mkdir(mode=0o700);(restored/'files').mkdir(mode=0o700)
    (restored/'database.dump').write_bytes(b'UNIT_DUMP_NOT_A_DATABASE')
    raw=json.dumps(manifest).encode();(restored/'manifest.json').write_bytes(raw)
    monkeypatch.setattr(worker,'decrypt_snapshot_stream',lambda *a,**kw:restored)
    monkeypatch.setattr(worker.subprocess,'run',lambda *a,**kw:None)
    class Lease:
        def __enter__(self):return SimpleNamespace(execute=lambda query:SimpleNamespace(fetchone=lambda:{'value':'34'}))
        def __exit__(self,*args):pass
    class Pool:
        connection=Lease
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(psycopg_pool,'ConnectionPool',lambda *a,**kw:Pool())
    later=[]
    monkeypatch.setattr(worker.backup,'verify_snapshot',lambda *a:later.append('inventory'))
    monkeypatch.setattr(worker.backup,'quarantine_restore',lambda *a:later.append('quarantine'))
    config={'v':1,'identity_file':'/run/restore/identity','work_directory':'/work','database_host':'restore-postgres',
            'database_password':'a'*64,'snapshot_sha256':hashlib.sha256(raw).hexdigest(),'max_plaintext_bytes':len(data)}
    with pytest.raises(worker.RestoreStageError,match='restore_snapshot_failed'):worker.run(config,io.BytesIO(data))
    assert later==[] and not (restored/'result.json').exists()
