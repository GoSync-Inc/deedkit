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
from test_backup_calendar import calendar_db  # noqa: F401  (a fixture)
from test_backup_r2_verifier import calendar_operator,operator  # noqa: F401  (fixtures)


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


# spec: ANYM-252
@pytest.mark.parametrize('version,checked',[(37,False),(38,True)])
def test_quarantine_checks_hint_windows_only_from_the_schema_that_creates_them(version,checked):
    from messaging_service import backup_restore_worker as worker
    queries=[]
    class Connection:
        def execute(self,query):
            queries.append(query)
            return SimpleNamespace(fetchone=lambda:{'name':worker.DATABASE} if 'current_database' in query else None)
    class Lease:
        def __enter__(self):return Connection()
        def __exit__(self,*args):pass
    worker.assert_quarantined(SimpleNamespace(pool=SimpleNamespace(connection=Lease)),version)
    assert any('subscription_hint_windows' in q for q in queries) is checked
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


# The id '36-36' is a Tiden case ID; since schema 54 the first unsupported schema is 55.
@pytest.mark.parametrize('actual,manifest',[('34',31),('31',34),pytest.param('55',55,id='36-36'),('corrupt',34),(None,34)])
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


# spec: ANYM-511
@pytest.mark.parametrize('version',[53,54],ids=['n-1','current'])
def test_restored_database_schema_accepts_the_current_and_previous_schema(version):
    from messaging_service import backup_restore_worker as worker
    class Lease:
        def __enter__(self):return SimpleNamespace(execute=lambda query:SimpleNamespace(fetchone=lambda:{'value':str(version)}))
        def __exit__(self,*args):pass
    assert worker.restored_schema_version(SimpleNamespace(pool=SimpleNamespace(connection=Lease)),version)==version


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


def fernet_encrypt(key,plaintext,iv=bytes(range(16))):
    """A real Fernet token with a fixed IV and time, so a damaged-key outcome is deterministic."""
    import base64,hmac
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
    secret=base64.urlsafe_b64decode(key);padder=padding.PKCS7(128).padder()
    encryptor=Cipher(algorithms.AES(secret[16:]),modes.CBC(iv)).encryptor()
    body=encryptor.update(padder.update(plaintext)+padder.finalize())+encryptor.finalize()
    data=b'\x80'+(1_800_000_000).to_bytes(8,'big')+iv+body
    return base64.urlsafe_b64encode(data+hmac.new(secret[:16],data,'sha256').digest())


CALENDAR_KEY=__import__('base64').urlsafe_b64encode(hashlib.sha256(b'synthetic-calendar-key').digest())
HOME_PLAINTEXT=b'{"refresh_token":"SYNTHETIC_TOKEN_NEVER_WRITTEN"}'


def calendar_worker_config(raw,data=b''):
    return {'v':1,'profile':'calendar','identity_file':'/run/restore/identity','work_directory':'/work',
            'database_host':'restore-postgres','database_password':'a'*64,'snapshot_sha256':hashlib.sha256(raw).hexdigest(),
            'max_plaintext_bytes':max(1,len(data))}


@pytest.fixture
def calendar_capture(calendar_db,tmp_path):  # noqa: F811  (the imported fixture)
    """A captured synthetic calendar bundle (schema 4, real Fernet tokens) and a restore target factory."""
    from test_backup_calendar import PROFILE,docker_tool,pg_tools
    from messaging_service.backup_calendar import create_calendar_snapshot
    container=pg_tools();source=calendar_db()
    vault=tmp_path/'src/vault';profile=tmp_path/'src/profile';vault.mkdir(mode=0o700,parents=True);profile.mkdir(mode=0o700)
    (vault/'key').write_bytes(CALENDAR_KEY)
    (vault/'home.enc').write_bytes(fernet_encrypt(CALENDAR_KEY,HOME_PLAINTEXT))
    (vault/'odd.enc').write_bytes(fernet_encrypt(CALENDAR_KEY,b'["not","an","object"]'))
    for name,value in PROFILE.items():(profile/name).write_bytes(value)
    import psycopg
    with psycopg.connect(source,autocommit=True) as c:
        c.execute("INSERT INTO fcs.connections VALUES ('home','google',true),('odd','google',false),('old','icloud',false)")
    snapshot=tmp_path/'snapshot'
    create_calendar_snapshot(source,snapshot,docker_tool(container,source,'pg_dump'),vault_directory=vault,profile_directory=profile)
    def restore(snapshot=snapshot,manifest_sha=None):
        from messaging_service import backup_restore_worker as worker
        from psycopg.conninfo import conninfo_to_dict
        target=calendar_db(schema=None)
        raw=(snapshot/'manifest.json').read_bytes()
        manifest_sha=manifest_sha or hashlib.sha256(raw).hexdigest()
        return worker.restore_calendar(snapshot,manifest_sha,tool=lambda name:docker_tool(container,target,name),
                                       env=None,dsn=target,database=conninfo_to_dict(target)['dbname'])
    return SimpleNamespace(snapshot=snapshot,source=source,restore=restore,dump=lambda:docker_tool(container,source,'pg_dump'))


def rewrite_manifest(snapshot,change):
    manifest=json.loads((snapshot/'manifest.json').read_text());change(manifest)
    (snapshot/'manifest.json').write_text(json.dumps(manifest))


def restate(snapshot,name,data):
    """Replace a state file and recompute its manifest hash, so the bundle still verifies."""
    path=snapshot/'state'/name
    if data is None:path.unlink()
    else:path.write_bytes(data)
    def change(manifest):
        if data is None:del manifest['state_files'][name]
        else:manifest['state_files'][name]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    rewrite_manifest(snapshot,change)


# spec: ANYM-606
def test_calendar_restore_passes_all_seven_checks_and_copies_the_vault_warnings(calendar_capture):
    result=calendar_capture.restore()
    assert result['profile']=='calendar' and result['schema_version']==4
    assert result['checks']=={name:True for name in ('decryption','bundle_integrity','database_restore','schema_version',
                                                      'table_inventory','vault_complete','vault_decrypts')}
    assert result['vault_warnings']==[{'kind':'disabled_connection_without_token','name':'old'},
                                      {'kind':'token_not_json_object','name':'odd'}]
    assert b'SYNTHETIC_TOKEN_NEVER_WRITTEN' not in json.dumps(result).encode()


def damaged_key():
    import base64
    secret=base64.urlsafe_b64decode(CALENDAR_KEY)
    return base64.urlsafe_b64encode(secret[:16]+bytes(16))


def damaged_token():
    import base64,hmac
    raw=bytearray(base64.urlsafe_b64decode(fernet_encrypt(CALENDAR_KEY,HOME_PLAINTEXT)))
    raw[-33]^=0x01  # last ciphertext byte: its block's padding no longer decrypts
    secret=base64.urlsafe_b64decode(CALENDAR_KEY);data=bytes(raw[:-32])
    return base64.urlsafe_b64encode(data+hmac.new(secret[:16],data,'sha256').digest())


# spec: ANYM-606
@pytest.mark.parametrize('check,code',[('bundle_integrity','restore_bundle_failed'),('database_restore','restore_database_failed'),
    ('schema_version','restore_schema_failed'),('table_inventory','restore_inventory_failed'),
    ('vault_complete','restore_vault_incomplete'),('vault_decrypts_key','restore_vault_decrypt_failed'),
    ('vault_decrypts_token','restore_vault_decrypt_failed')])
def test_each_calendar_check_fails_its_own_bundle_after_every_earlier_check_passes(calendar_capture,check,code):
    import subprocess
    from messaging_service import backup_restore_worker as worker
    from messaging_service.backup_calendar import fernet_hmac_valid,verify_calendar_bundle
    snapshot=calendar_capture.snapshot
    if check=='bundle_integrity':(snapshot/'state/profile/worker.json').write_bytes(b'{"dsn":"changed"}\n')
    elif check=='database_restore':
        dump=b'PGDMP-NOT-A-DUMP';(snapshot/'database.dump').write_bytes(dump)
        rewrite_manifest(snapshot,lambda m:m.update(dump_sha256=hashlib.sha256(dump).hexdigest()))
    elif check=='schema_version':rewrite_manifest(snapshot,lambda m:m.update(schema_version=5))
    elif check=='table_inventory':
        import psycopg
        with psycopg.connect(calendar_capture.source,autocommit=True) as c:c.execute("INSERT INTO public.extra VALUES (7,'after')")
        dump=subprocess.run([*calendar_capture.dump(),'--format=custom','--no-owner','--no-acl'],check=True,capture_output=True).stdout
        (snapshot/'database.dump').write_bytes(dump)
        rewrite_manifest(snapshot,lambda m:m.update(dump_sha256=hashlib.sha256(dump).hexdigest()))
    elif check=='vault_complete':restate(snapshot,'vault/home.enc',None)
    elif check=='vault_decrypts_key':
        assert fernet_hmac_valid(damaged_key(),(snapshot/'state/vault/home.enc').read_bytes())
        restate(snapshot,'vault/key',damaged_key())
    else:
        assert fernet_hmac_valid(CALENDAR_KEY,damaged_token())
        restate(snapshot,'vault/home.enc',damaged_token())
    if check!='bundle_integrity':verify_calendar_bundle(snapshot)
    with pytest.raises(worker.RestoreStageError,match=code):calendar_capture.restore()


# spec: ANYM-606
def test_calendar_worker_refuses_a_failed_decryption_and_an_unknown_profile(tmp_path,monkeypatch):
    from messaging_service import backup_restore_worker as worker
    data,manifest=bundle();raw=json.dumps(manifest).encode()
    decoder=tmp_path/'age-fails'
    decoder.write_text('#!'+sys.executable+'\nimport sys\nsys.stdin.buffer.read()\nsys.exit(1)\n');decoder.chmod(0o700)
    real=crypto.decrypt_snapshot_stream
    monkeypatch.setattr(worker,'decrypt_snapshot_stream',lambda source,identity,destination,max_bytes:
                        real(source,identity,tmp_path/'bundle',age=str(decoder),max_bytes=max_bytes))
    with pytest.raises(worker.RestoreStageError,match='restore_decryption_failed'):
        worker.run(calendar_worker_config(raw,data),io.BytesIO(data))
    for profile in ('messaging','notes',None):
        with pytest.raises(ValueError,match='invalid_restore_worker_config'):
            worker.run({**calendar_worker_config(raw,data),'profile':profile},io.BytesIO(data))


# spec: ANYM-606
@pytest.mark.parametrize('configured',['messaging','calendar'])
def test_worker_refuses_a_bundle_of_the_other_profile_before_any_restore(tmp_path,monkeypatch,configured):
    from messaging_service import backup_restore_worker as worker
    data,manifest=bundle()
    if configured=='messaging':manifest={**manifest,'profile':'calendar'}
    restored=tmp_path/'verified-bundle';restored.mkdir(mode=0o700)
    (restored/'database.dump').write_bytes(b'UNIT_DUMP_NOT_A_DATABASE')
    raw=json.dumps(manifest).encode();(restored/'manifest.json').write_bytes(raw)
    monkeypatch.setattr(worker,'decrypt_snapshot_stream',lambda *a,**kw:restored)
    calls=[];monkeypatch.setattr(worker.subprocess,'run',lambda *a,**kw:calls.append(a))
    config=calendar_worker_config(raw,data)
    if configured=='messaging':del config['profile']
    with pytest.raises(worker.RestoreStageError,match='verifier_profile_mismatch'):worker.run(config,io.BytesIO(data))
    assert calls==[] and not (restored/'result.json').exists()


# spec: ANYM-606
@pytest.mark.parametrize('profile',[None,'calendar ',['messaging']],ids=['null','other','list'])
def test_a_manifest_naming_any_other_profile_value_is_a_mismatch_for_messaging(tmp_path,monkeypatch,profile):
    from messaging_service import backup_restore_worker as worker
    data,manifest=bundle();manifest={**manifest,'profile':profile}
    restored=tmp_path/'verified-bundle';restored.mkdir(mode=0o700)
    raw=json.dumps(manifest).encode();(restored/'manifest.json').write_bytes(raw)
    monkeypatch.setattr(worker,'decrypt_snapshot_stream',lambda *a,**kw:restored)
    config=calendar_worker_config(raw,data);del config['profile']
    with pytest.raises(worker.RestoreStageError,match='verifier_profile_mismatch'):worker.run(config,io.BytesIO(data))


# spec: ANYM-606
@pytest.mark.parametrize('damage',['dump','state'])
def test_an_age_authentic_calendar_bundle_with_a_wrong_hash_fails_bundle_integrity_not_decryption(tmp_path,monkeypatch,decoder,damage):
    from messaging_service import backup_restore_worker as worker
    from messaging_service.backup_calendar import MANIFEST_KEYS,RESTORE_MODE
    dump=b'SYNTHETIC_CALENDAR_DUMP';state=b'POSTGRES_PASSWORD=synthetic\n'
    manifest={key:None for key in MANIFEST_KEYS}
    manifest.update(profile='calendar',format=1,created_at='2026-10-28T12:00:00+00:00',captured_at='2026-10-28T12:00:00+00:00',
                    schema_version=4,tables={},type_warnings=[],vault_warnings=[],vault_staging_skipped=0,restore_mode=RESTORE_MODE,
                    dump_sha256=hashlib.sha256(dump if damage=='state' else b'other').hexdigest(),
                    state_files={'profile/postgres.env':{'bytes':len(state),'sha256':hashlib.sha256(state if damage=='dump' else b'x').hexdigest()}})
    raw=json.dumps(manifest).encode();out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w') as archive:
        for name,value in [('manifest.json',raw),('database.dump',dump),('state/profile/postgres.env',state)]:
            item=tarfile.TarInfo(name);item.size=len(value);archive.addfile(item,io.BytesIO(value))
    data=out.getvalue();real=crypto.decrypt_snapshot_stream
    monkeypatch.setattr(worker,'decrypt_snapshot_stream',lambda source,identity,destination,max_bytes:
                        real(source,identity,tmp_path/'bundle',age=decoder,max_bytes=max_bytes))
    with pytest.raises(worker.RestoreStageError,match='restore_bundle_failed'):
        worker.run(calendar_worker_config(raw,data),io.BytesIO(data))


# spec: ANYM-606
def test_warnings_with_long_escaped_names_stay_inside_the_byte_bound_and_the_proof_read_limit():
    from messaging_service import backup_restore_worker as worker
    from messaging_service import backup_r2_receiver as r2
    for count in (21,32,40):
        warnings=[{'kind':'unknown_entry','name':'\U0001f600'*60+'%02d'%i} for i in range(count)]
        bounded=worker.proof_warnings(warnings)
        assert r2.valid_warnings(bounded) and len(r2.encoded(bounded))<=r2.WARNING_BYTES
        assert bounded[-1]=={'kind':'warnings_truncated','name':str(count)}
    assert not r2.valid_warnings([{'kind':'unknown_entry','name':'\U0001f600'*60}]*21)
    small=[{'kind':'disabled_connection_without_token','name':'old'}]
    assert worker.proof_warnings(small)==small


# spec: ANYM-606
def test_a_real_bundle_missing_an_enabled_token_fails_vault_complete_and_keeps_the_old_point(calendar_operator,calendar_capture):  # noqa: F811
    from test_backup_r2 import receive,THIRD
    from messaging_service import backup_restore_worker as worker
    v,config,receiver,cloud,engine,_=calendar_operator
    def restore(config,receiver,client,receipt,run,payload,secret_dir):
        # The real calendar checks on the captured bundle, and the real failure fact the verifier reads.
        engine.runs.append(run)
        try:return calendar_capture.restore(manifest_sha=receipt['wire']['snapshot_sha256'])
        except worker.RestoreStageError as error:
            worker.write_failure(payload,error);raise v.helper_failure(payload) from None
    engine.restore=restore
    assert v.run(config,client=cloud,engine=engine)['status']=='verified'
    marker=Path(config['protected_file']).read_bytes();objects=dict(cloud.objects)
    receive(receiver,cloud,bid=THIRD)
    restate(calendar_capture.snapshot,'vault/home.enc',None)
    with pytest.raises(v.HelperFailure,match='restore_vault_incomplete'):v.run(config,client=cloud,engine=engine)
    assert json.loads((Path(config['directory'])/'status.json').read_text())['error']=='restore_vault_incomplete'
    assert Path(config['protected_file']).read_bytes()==marker
    assert all(cloud.objects.get(key)==value for key,value in objects.items())


def bundle_with_original(change=lambda entries,digest:None):
    content=b'SYNTHETIC_ORIGINAL_BYTES\n'*64;digest=hashlib.sha256(content).hexdigest();dump=b'UNIT_DUMP_NOT_A_DATABASE'
    manifest={'format':1,'schema_version':2,'dump_sha256':hashlib.sha256(dump).hexdigest(),'tables':{},
              'blobs':[{'digest':digest,'bytes':len(content)}]}
    raw=json.dumps(manifest).encode();entries={'manifest.json':raw,'database.dump':dump,'files/'+digest:content}
    change(entries,digest);out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w') as archive:
        for name,data in entries.items():
            item=tarfile.TarInfo(name);item.size=len(data);archive.addfile(item,io.BytesIO(data))
    return out.getvalue(),manifest,raw,digest


def offline_worker(monkeypatch,tmp_path,decoder,manifest,after=lambda restored:None):
    """worker.run with a fake database and pg_restore; extraction, bundle checks and read-backs are real."""
    import psycopg_pool
    from messaging_service import backup_restore_worker as worker
    real=crypto.decrypt_snapshot_stream;calls=[]
    def decrypt(source,identity,destination,max_bytes):
        restored=real(source,identity,tmp_path/'bundle',age=decoder,max_bytes=max_bytes);after(restored);return restored
    class Lease:
        def __enter__(self):return SimpleNamespace(execute=lambda *a:None)
        def __exit__(self,*args):pass
    class Pool:
        connection=Lease
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(worker,'decrypt_snapshot_stream',decrypt)
    monkeypatch.setattr(worker.subprocess,'run',lambda args,**kw:calls.append(args[0]))
    monkeypatch.setattr(psycopg_pool,'ConnectionPool',lambda *a,**kw:Pool())
    monkeypatch.setattr(worker,'restored_schema_version',lambda store,version:version)
    monkeypatch.setattr(worker.backup,'database_manifest',lambda c:manifest['tables'])
    monkeypatch.setattr(worker.backup,'blob_inventory',lambda c,version:(manifest['blobs'],[]))
    monkeypatch.setattr(worker.backup,'quarantine_restore',lambda store:None)
    monkeypatch.setattr(worker,'assert_quarantined',lambda store,version:None)
    monkeypatch.setattr(worker,'write_result',lambda work,result:result)
    def run(data,raw):
        config={'v':1,'identity_file':'/run/restore/identity','work_directory':'/work','database_host':'restore-postgres',
                'database_password':'a'*64,'snapshot_sha256':hashlib.sha256(raw).hexdigest(),'max_plaintext_bytes':len(data)}
        return worker.run(config,io.BytesIO(data))
    return worker,run,calls


def flip_on_disk(path):
    """Same size, different bytes: only a read of the file can tell."""
    data=bytearray(path.read_bytes());data[0]^=0x01;path.write_bytes(bytes(data))


# spec: ANYM-630
@pytest.mark.parametrize('damage,error',[('original','blob_corrupt'),('dump','dump_corrupt'),('missing','blob_corrupt'),
                                         ('extra','backup_blob_set_mismatch')])
def test_extraction_refuses_a_tampered_missing_or_extra_member_of_the_stream(tmp_path,decoder,damage,error):
    def change(entries,digest):
        if damage=='original':entries['files/'+digest]=b'X'+entries['files/'+digest][1:]
        elif damage=='dump':entries['database.dump']=b'X'+entries['database.dump'][1:]
        elif damage=='missing':del entries['files/'+digest]
        else:entries['files/'+'0'*64]=b'SYNTHETIC_EXTRA'
    data,*_=bundle_with_original(change)
    with pytest.raises((RuntimeError,ValueError),match=error):
        crypto.decrypt_snapshot_stream(io.BytesIO(data),'key',tmp_path/'restore',age=decoder,max_bytes=len(data))
    assert not (tmp_path/'restore').exists() and not list(tmp_path.glob('.decrypt-*'))


# spec: ANYM-630
def test_a_dump_changed_on_disk_after_extraction_is_dump_corrupt_before_pg_restore(tmp_path,monkeypatch,decoder):
    from messaging_service import backup
    data,manifest,raw,_=bundle_with_original();seen=[]
    def after(restored):flip_on_disk(restored/'database.dump');seen.append(restored)
    worker,run,calls=offline_worker(monkeypatch,tmp_path,decoder,manifest,after)
    with pytest.raises(worker.RestoreStageError,match='restore_snapshot_failed'):run(data,raw)
    assert calls==[]
    with pytest.raises(RuntimeError,match='dump_corrupt'):backup.verify_bundle(seen[0],extracted=seen[0].extracted)


# spec: ANYM-630
def test_an_original_changed_on_disk_after_extraction_fails_the_original_hashes_read_back(tmp_path,monkeypatch,decoder):
    data,manifest,raw,digest=bundle_with_original()
    worker,run,calls=offline_worker(monkeypatch,tmp_path,decoder,manifest,lambda restored:flip_on_disk(restored/'files'/digest))
    causes=[];real_stage=worker.stage
    def stage(code,action):
        try:return action()
        except Exception as error:causes.append(error)
        return real_stage(code,action)
    monkeypatch.setattr(worker,'stage',stage)
    with pytest.raises(worker.RestoreStageError,match='restore_snapshot_failed'):run(data,raw)
    # The size-preserving change passes the pre-restore check; the read-back after pg_restore refuses it.
    assert calls==['psql','pg_restore'] and [str(error) for error in causes]==['blob_corrupt']


# spec: ANYM-630
def test_each_original_is_read_once_and_the_dump_twice_after_extraction(tmp_path,monkeypatch,decoder):
    import os
    data,manifest,raw,digest=bundle_with_original()
    worker,run,calls=offline_worker(monkeypatch,tmp_path,decoder,manifest)
    opened=[];real_os_open=os.open;real_io_open=io.open
    def os_open(path,flags,*args,**kw):
        if not flags&(os.O_WRONLY|os.O_RDWR):opened.append(Path(path).name)
        return real_os_open(path,flags,*args,**kw)
    def io_open(file,mode='r',*args,**kw):
        if isinstance(file,(str,os.PathLike)) and not set(mode)&set('wax+'):opened.append(Path(file).name)
        return real_io_open(file,mode,*args,**kw)
    monkeypatch.setattr(os,'open',os_open);monkeypatch.setattr(io,'open',io_open)
    result=run(data,raw)
    assert result['status']=='verified' and all(result['checks'].values()) and calls==['psql','pg_restore']
    assert opened.count(digest)==1 and opened.count('database.dump')==2
