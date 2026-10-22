"""Synthetic cold-archive contracts: real age, real wire, file-backed fake R2.

The receiver side is the real `backup_r2_receiver.cold_serve` in a child
process, reached through the producer's own SSH transport code. No credentials,
network, provider sessions or real data are used.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_archive import archive  # noqa: F401  (fixture)
from test_file_lifecycle import processed, stored

STUB=r'''
import hashlib,io,json,sys
from pathlib import Path
from messaging_service import backup_r2_receiver as r2
class Code(Exception):
    def __init__(self,code):self.response={'Error':{'Code':code},'ResponseMetadata':{'HTTPStatusCode':int(code) if code.isdigit() else 404}}
class FileCloud:
    """Byte-persistent fake R2 in a directory; failure switches are files."""
    def __init__(self,root):self.root=Path(root)
    def path(self,key):return self.root/('obj-'+hashlib.sha256(key.encode()).hexdigest())
    def log(self,name):
        with open(self.root/'calls.log','a') as f:f.write(name+'\n')
    def check(self):
        if (self.root/'unavailable').exists():raise RuntimeError('signed diagnostic must never reach wire')
    def head_object(self,Bucket,Key):
        self.log('head_object');self.check();p=self.path(Key)
        if not p.exists():raise Code('404')
        return {'ContentLength':p.stat().st_size}
    def get_object(self,Bucket,Key):
        self.log('get_object');self.check();p=self.path(Key)
        tamper=(self.root/'tamper').read_text() if (self.root/'tamper').exists() else ''
        if not p.exists() or tamper=='missing':raise Code('NoSuchKey')
        data=p.read_bytes()
        if tamper=='different':data=data[:-1]+bytes([data[-1]^1])
        if tamper=='truncated':return {'ContentLength':len(data),'Body':io.BytesIO(data[:len(data)//2])}
        return {'ContentLength':len(data),'Body':io.BytesIO(data)}
    def put_object(self,Bucket,Key,Body,ContentType,IfNoneMatch):
        self.log('put_object');self.check();p=self.path(Key)
        assert IfNoneMatch=='*'
        if p.exists():raise Code('412')
        p.write_bytes(Body)
    def create_multipart_upload(self,Bucket,Key,ContentType):
        self.log('create_multipart_upload');self.check();(self.root/'mpu').write_bytes(b'');return {'UploadId':'u1'}
    def upload_part(self,Bucket,Key,UploadId,PartNumber,Body):
        self.log('upload_part')
        with open(self.root/'mpu','ab') as f:f.write(Body)
        return {'ETag':'part-'+str(PartNumber)}
    def complete_multipart_upload(self,Bucket,Key,UploadId,MultipartUpload):
        self.log('complete_multipart_upload');(self.root/'mpu').rename(self.path(Key));return {}
    def abort_multipart_upload(self,Bucket,Key,UploadId):
        self.log('abort_multipart_upload');(self.root/'mpu').unlink(missing_ok=True);return {}
config=json.loads(sys.argv[1])
try:r2.cold_serve(config,sys.stdin.buffer,sys.stdout.buffer,client=FileCloud(sys.argv[2]))
except Exception as exc:
    safe={'storage_unavailable','invalid_cold_request','cold_object_missing','receiver_busy','cold_size_limit'}
    sys.stdout.buffer.write(r2.encoded({'v':1,'status':'error','code':str(exc) if str(exc) in safe else 'receiver_failed'}))
    sys.exit(1)
'''


@pytest.fixture(scope='session')
def keys(tmp_path_factory):
    root=tmp_path_factory.mktemp('cold-keys');result={}
    for name in ('backup','cold','unrelated'):
        path=root/name
        subprocess.run(['age-keygen','-o',str(path)],check=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        path.chmod(0o600)
        result[name]=(path,subprocess.check_output(['age-keygen','-y',str(path)],stderr=subprocess.DEVNULL).decode().strip())
    return result


@pytest.fixture(autouse=True)
def clear_cold_copies(database):
    with database.pool.connection() as c:c.execute('DELETE FROM attachment_cold_copies')
    yield
    with database.pool.connection() as c:c.execute('DELETE FROM attachment_cold_copies')


def cold():
    from messaging_service import cold_archive
    return cold_archive


class StubTransport:
    """The production SSH transport with ssh replaced by the stub receiver."""
    def __new__(cls,receiver,cloud):
        api=cold()
        class Transport(api.SSHTransport):
            def _spawn(self):
                return subprocess.Popen([sys.executable,'-c',STUB,json.dumps(receiver),str(cloud)],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        return Transport({'host':'receiver.invalid','user':'cold','identity_file':'/synthetic/key',
                          'known_hosts_file':'/synthetic/known_hosts'},60)


@pytest.fixture
def env(archive,keys,tmp_path):
    _,s,_,_=archive
    state=tmp_path/'receiver-state';state.mkdir(mode=0o700)
    cloud=tmp_path/'cloud';cloud.mkdir()
    receiver={'prefix':'synthetic-owner/v1','bucket':'synthetic-backups','state_directory':str(state),
              'part_bytes':5*1024**2,'cold_max_bytes':64*1024**2}
    identity=tmp_path/'cold-identity';shutil.copyfile(keys['cold'][0],identity);identity.chmod(0o600)
    config={'v':1,'transfer':{'host':'receiver.invalid','user':'cold','identity_file':'/synthetic/key',
                              'known_hosts_file':'/synthetic/known_hosts'},
            'recipients':[keys['backup'][1],keys['cold'][1]],'identity_file':str(identity),
            'total_bytes':1024**3,'pass_files':100,'pass_bytes':1024**3,'max_object_bytes':32*1024**2}
    path=tmp_path/'cold-archive.json';path.write_text(json.dumps(config));path.chmod(0o600)
    config=cold().load_config(str(path))
    def archiver(**changes):
        return cold().ColdArchiver(s,{**config,**changes},StubTransport(receiver,cloud))
    return s,cloud,archiver,identity


def idle(archive,*,mid='cold-1',payload=b'Synthetic cold original',days=24):
    if len(payload)>1024**2:
        from messaging_service.blobs import BlobWriter
        from test_archive import event
        ar,s,_,_=archive
        ar.ingest(event(provider_id=mid,media=[{'provider_id':'asset-'+mid,'filename':'Large.bin',
            'mime':'application/octet-stream','bytes':len(payload)}]))
        job=ar.claim_media('fixture');writer=BlobWriter(s.files,max_bytes=len(payload),reserve_bytes=0)
        blob=writer.write(payload[i:i+1024**2] for i in range(0,len(payload),1024**2));ar.finish_media(job,blob,writer)
    else:_,blob=stored(archive,mid=mid,payload=payload)
    _,s,_,_=archive
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_residency SET last_accessed_at=clock_timestamp()-make_interval(days=>%s) WHERE digest=%s",(days,blob.digest))
    return blob


def objects(cloud):
    return sorted(p for p in cloud.iterdir() if p.name.startswith('obj-'))


def calls(cloud):
    log=cloud/'calls.log'
    return log.read_text().split() if log.exists() else []


def copy(s,digest):
    with s.pool.connection() as c:
        row=c.execute('SELECT * FROM attachment_cold_copies WHERE digest=%s',(digest,)).fetchone()
    return dict(row) if row else None


def evict(s):
    from messaging_service.file_lifecycle import LifecycleWorker
    return LifecycleWorker(s,require_cold_copy=True).run_once(apply=True)


def decrypt(identity,data):
    return subprocess.run(['age','-d','-i',str(identity)],input=data,capture_output=True)


def test_verified_copy_is_read_back_and_then_authorizes_eviction(env,archive):
    s,cloud,archiver,_=env;_,blob=processed(archive)
    report=archiver().run_once(apply=True)
    assert report['archived']==1 and report['archived_bytes']==blob.bytes and report['reasons']=={}
    row=copy(s,blob.digest)
    assert row['state']=='verified' and row['object_key']=='synthetic-owner/v1/cold/'+blob.digest+'.age'
    [stored_object]=objects(cloud)
    assert row['ciphertext_bytes']==stored_object.stat().st_size
    assert row['ciphertext_sha256']==hashlib.sha256(stored_object.read_bytes()).hexdigest()
    assert calls(cloud).index('get_object')>calls(cloud).index('put_object')
    assert evict(s)['evicted']==1 and not (s.files/blob.digest).exists()


@pytest.mark.parametrize('tamper',['different','truncated','missing'])
def test_upload_without_matching_readback_is_not_verified(env,archive,tamper):
    s,cloud,archiver,_=env;_,blob=processed(archive)
    (cloud/'tamper').write_text(tamper)
    report=archiver().run_once(apply=True)
    assert report['archived']==0 and report['reasons']=={'cold_verify_failed':1}
    row=copy(s,blob.digest)
    assert row['state']=='failed' and row['verified_at'] is None
    assert row['error_code'] in {'cold_verify_failed','cold_ciphertext_mismatch','cold_object_missing'}
    assert len(objects(cloud))==1  # The upload itself succeeded.
    assert evict(s)['reasons']=={'cold_copy_missing':1}
    assert (s.files/blob.digest).read_bytes()==b'Synthetic retained contract'


def test_budget_exhaustion_pauses_eviction_not_data(env,archive):
    s,cloud,archiver,_=env
    one=idle(archive,mid='cold-1',payload=b'Synthetic first original')
    two=idle(archive,mid='cold-2',payload=b'Synthetic second original!')
    three=idle(archive,mid='cold-3',payload=b'Synthetic third original!!!')
    fresh=idle(archive,mid='cold-4',payload=b'Recently requested original',days=1)
    budget=one.bytes+two.bytes
    preview=archiver(total_bytes=budget).run_once()
    assert preview['dry_run'] and preview['candidates']==2 and preview['reasons']=={'cold_budget':1}
    assert objects(cloud)==[] and calls(cloud)==[] and all(copy(s,b.digest) is None for b in (one,two,three))
    report=archiver(total_bytes=budget).run_once(apply=True)
    assert report['archived']==2 and report['reasons']=={'cold_budget':1}
    assert report['budget']=={'total_bytes':budget,'used_bytes':budget}
    assert copy(s,three.digest) is None and copy(s,fresh.digest) is None and len(objects(cloud))==2
    # Per-pass file and byte limits stop the same way, before any upload.
    for changes in ({'pass_bytes':1},{'max_object_bytes':1},{'pass_files':0}):
        assert archiver(**changes).run_once(apply=True)['reasons']=={'cold_budget':1}
    (cloud/'unavailable').write_text('down')
    report=archiver().run_once(apply=True)
    assert report['archived']==0 and report['reasons']=={'cold_unavailable':1}
    assert copy(s,three.digest) is None
    for blob in (one,two,three,fresh):
        assert (s.files/blob.digest).exists()
        with s.pool.connection() as c:
            assert c.execute('SELECT state FROM attachment_residency WHERE digest=%s',(blob.digest,)).fetchone()['state']=='resident'


def test_objects_are_age_encrypted_to_both_recipients(env,archive,keys,capsys):
    s,cloud,archiver,identity=env;payload=b'Synthetic private cold original'
    blob=idle(archive,payload=payload)
    report=archiver().run_once(apply=True)
    assert report['archived']==1
    [stored_object]=objects(cloud);data=stored_object.read_bytes()
    assert data.startswith(b'age-encryption.org/v1\n') and payload not in data
    for name in ('backup','cold'):
        result=decrypt(keys[name][0],data)
        assert result.returncode==0 and result.stdout==payload
    assert decrypt(keys['unrelated'][0],data).returncode!=0
    fingerprints=sorted(hashlib.sha256(keys[n][1].encode()).hexdigest() for n in ('backup','cold'))
    assert sorted(copy(s,blob.digest)['recipient_fingerprints'])==fingerprints
    text=json.dumps(report)+json.dumps(cold().status(s))
    for secret in (str(identity),str(keys['backup'][0]),payload.decode(),'AGE-SECRET-KEY',str(s.files)):
        assert secret not in text


@pytest.mark.parametrize('attack',['public','hardlink','symlink','missing'])
def test_cold_identity_must_be_a_private_single_link_file(env,archive,attack,tmp_path):
    s,cloud,archiver,identity=env;blob=idle(archive)
    if attack=='public':identity.chmod(0o644)
    elif attack=='hardlink':os.link(identity,tmp_path/'identity-copy')
    elif attack=='symlink':target=tmp_path/'identity-target';identity.rename(target);identity.symlink_to(target)
    else:identity.unlink()
    report=archiver().run_once(apply=True)
    assert report['archived']==0 and set(report['reasons'])<={'private_cold_identity_required','cold_verify_failed'}
    assert copy(s,blob.digest)['state']=='failed'
    assert str(identity) not in json.dumps(report)


def test_config_requires_two_distinct_recipients_and_bounded_limits(env,tmp_path,keys):
    base=json.loads((tmp_path/'cold-archive.json').read_text())
    for change in ({'recipients':[keys['cold'][1]]},{'recipients':[keys['cold'][1]]*2},
                   {'recipients':[keys['cold'][1],'ssh-ed25519 AAAA']},{'archive_after_days':30},
                   {'total_bytes':0},{'unknown':True},{'identity_file':'relative/identity'}):
        path=tmp_path/'bad.json';path.write_text(json.dumps({**base,**change}));path.chmod(0o600)
        with pytest.raises(ValueError):cold().load_config(str(path))
    path=tmp_path/'public.json';path.write_text(json.dumps(base));path.chmod(0o644)
    with pytest.raises(ValueError):cold().load_config(str(path))
    example=Path(__file__).resolve().parents[1]/'deploy/cold-archive.example.json'
    copied=tmp_path/'example.json';copied.write_bytes(example.read_bytes());copied.chmod(0o600)
    assert cold().load_config(str(copied))['archive_after_days']<30


class Crash(BaseException):
    pass


def test_crash_points_and_shared_digest(env,archive,monkeypatch):
    s,cloud,archiver,_=env
    shared=idle(archive,mid='cold-shared-1',payload=b'Synthetic shared original')
    assert idle(archive,mid='cold-shared-2',payload=b'Synthetic shared original')==shared
    # 1. Crash after the upload, before read-back: pending, never verified.
    worker=archiver()
    def crash(*args,**kwargs):raise Crash()
    monkeypatch.setattr(worker,'verify',crash)
    with pytest.raises(Crash):worker.run_once(apply=True)
    row=copy(s,shared.digest)
    assert row['state']=='pending' and row['ciphertext_sha256'] and row['verified_at'] is None
    assert calls(cloud).count('put_object')==1 and len(objects(cloud))==1
    # 2. The retry reads the existing object back instead of uploading again.
    assert archiver().run_once(apply=True)['archived']==1
    assert calls(cloud).count('put_object')==1 and len(objects(cloud))==1
    assert copy(s,shared.digest)['state']=='verified'
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM attachment_cold_copies').fetchone()['n']==1
    # 3. Crash before the committing footer: nothing is stored, the retry uploads.
    second=idle(archive,mid='cold-2',payload=b'Synthetic second original')
    worker=archiver();record=worker._record
    def crash_before_commit(digest,size,**fields):
        if fields['state']=='pending':raise Crash()
        return record(digest,size,**fields)
    monkeypatch.setattr(worker,'_record',crash_before_commit)
    with pytest.raises(Crash):worker.run_once(apply=True)
    assert len(objects(cloud))==1 and copy(s,second.digest) is None
    # 4. Crash after read-back, before recording verification: no eviction.
    worker=archiver();record=worker._record
    def crash_before_verified(digest,size,**fields):
        if fields['state']=='verified':raise Crash()
        return record(digest,size,**fields)
    monkeypatch.setattr(worker,'_record',crash_before_verified)
    with pytest.raises(Crash):worker.run_once(apply=True)
    assert copy(s,second.digest)['state']=='pending' and len(objects(cloud))==2
    puts=calls(cloud).count('put_object')
    assert archiver().run_once(apply=True)['archived']==1
    assert calls(cloud).count('put_object')==puts and copy(s,second.digest)['state']=='verified'


def test_large_original_streams_through_bounded_multipart(env,archive):
    s,cloud,archiver,_=env;payload=os.urandom(6*1024**2)
    blob=idle(archive,mid='cold-large',payload=payload)
    assert archiver().run_once(apply=True)['archived']==1
    assert 'complete_multipart_upload' in calls(cloud) and copy(s,blob.digest)['state']=='verified'


def test_archiver_role_records_copies_but_cannot_evict(env,archive):
    from contextlib import contextmanager
    from copy import copy as clone
    from uuid import uuid4
    import psycopg
    from psycopg import sql
    s,cloud,archiver,_=env;blob=idle(archive)
    role='cold_'+uuid4().hex
    ddl=(Path(__file__).resolve().parents[1]/'services/messaging/messaging_service/cold_archive_role.sql').read_text()
    class RestrictedPool:
        @contextmanager
        def connection(self):
            with s.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                yield c
    restricted=clone(s);restricted.pool=RestrictedPool()
    with s.pool.connection() as c:c.execute(ddl.replace('messaging_cold_archive',role))
    try:
        worker=archiver();worker.store=restricted
        assert worker.run_once(apply=True)['archived']==1
        for statement in ("UPDATE attachment_residency SET state='evicted' WHERE digest='"+blob.digest+"'",
                          'DELETE FROM attachment_cold_copies','DELETE FROM attachment_eviction_journal',
                          "UPDATE attachments SET active=false WHERE digest='"+blob.digest+"'"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with restricted.pool.connection() as c:c.execute(statement)
    finally:
        with s.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def test_compose_runs_cold_archive_in_dry_run_without_r2_credentials():
    import yaml
    from scripts.production import validate_argv
    root=Path(__file__).resolve().parents[1]
    service=yaml.safe_load((root/'deploy/compose.file-lifecycle.yaml').read_text())['services']['cold-archive']
    validate_argv(service['command'])
    assert service['command'][:3]==['python','-m','messaging_service.cold_archive'] and '--apply' not in service['command']
    assert service['image'].startswith('anymsg-backup:')
    data=[v for v in service['volumes'] if v['target']=='/data']
    assert data==[{'type':'volume','source':'files','target':'/data','read_only':True}]
    assert all(v['read_only'] for v in service['volumes'])
    assert {v['target'] for v in service['volumes']}=={'/data','/run/secrets/database_url','/run/secrets/cold_archive',
                                                        '/run/secrets/cold_identity','/run/secrets/cold_ssh'}
    assert not any('credential' in v['source'] or 'r2' in v['source'] for v in service['volumes'])
    document=yaml.safe_load((root/'deploy/compose.file-lifecycle.yaml').read_text())
    assert service['networks']==['backend','cold_archive_egress'] and 'cold_archive_egress' in document['networks']
    backup=(root/'services/messaging/Dockerfile').read_text().split('AS backup',1)[1].split('\nCOPY',1)[0]
    # ssh refuses to start for a UID without a passwd entry; compose runs as 10001.
    assert 'openssh-client' in backup and 'useradd -u 10001' in backup and service['user']=='10001:10001'


def place(cloud,digest,data):
    key='synthetic-owner/v1/cold/'+digest+'.age'
    (cloud/('obj-'+hashlib.sha256(key.encode()).hexdigest())).write_bytes(data)


def encrypt(payload,*recipients):
    args=['age']
    for recipient in recipients:args+=['-r',recipient]
    return subprocess.run(args,input=payload,capture_output=True,check=True).stdout


@pytest.mark.parametrize('history',['no_catalog_row','other_recipients'])
def test_existing_object_without_recorded_provenance_is_never_verified(env,archive,keys,history):
    s,cloud,archiver,_=env;payload=b'Synthetic original stored by someone else'
    blob=idle(archive,payload=payload)
    # Decrypts with the cold key, but the backup recovery key cannot open it.
    data=encrypt(payload,keys['cold'][1]);place(cloud,blob.digest,data)
    if history=='other_recipients':
        # The recorded ciphertext matches, but it was sent for other recipients.
        with s.pool.connection() as c:
            c.execute('''INSERT INTO attachment_cold_copies(digest,bytes,state,object_key,ciphertext_bytes,ciphertext_sha256,recipient_fingerprints)
                VALUES(%s,%s,'failed',%s,%s,%s,%s)''',(blob.digest,blob.bytes,'synthetic-owner/v1/cold/'+blob.digest+'.age',
                len(data),hashlib.sha256(data).hexdigest(),['0'*64,'1'*64]))
    report=archiver().run_once(apply=True)
    assert report['archived']==0 and report['reasons']=={'cold_verify_failed':1}
    row=copy(s,blob.digest)
    assert row['state']=='failed' and row['error_code']=='cold_object_unattested' and row['verified_at'] is None
    assert 'put_object' not in calls(cloud)
    # The object exists in R2, so it keeps its share of the total budget.
    with s.pool.connection() as c:assert archiver().used_bytes(c)==blob.bytes


def test_new_originals_are_archived_before_failed_retries(env,archive):
    s,cloud,archiver,_=env
    stuck=idle(archive,mid='cold-stuck',payload=b'Synthetic original that keeps failing',days=28)
    with s.pool.connection() as c:
        c.execute('''INSERT INTO attachment_cold_copies(digest,bytes,state,object_key,error_code)
            VALUES(%s,%s,'failed',%s,'original_integrity')''',(stuck.digest,stuck.bytes,'synthetic-owner/v1/cold/'+stuck.digest+'.age'))
    fresh=idle(archive,mid='cold-new',payload=b'Synthetic original never attempted')
    report=archiver().run_once(apply=True,limit=1)
    assert report['archived']==1 and copy(s,fresh.digest)['state']=='verified'
    assert copy(s,stuck.digest)['state']=='failed'


def test_failed_read_back_keeps_the_stored_object_in_budget(env,archive):
    s,cloud,archiver,_=env
    one=idle(archive,mid='cold-1',payload=b'Synthetic first original')
    (cloud/'tamper').write_text('different')
    assert archiver().run_once(apply=True)['reasons']=={'cold_verify_failed':1}
    (cloud/'tamper').unlink()
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_residency SET last_accessed_at=clock_timestamp() WHERE digest=%s",(one.digest,))
    two=idle(archive,mid='cold-2',payload=b'Synthetic second original')
    report=archiver(total_bytes=one.bytes+two.bytes-1).run_once(apply=True)
    # The failed object may still exist in R2, so it keeps its share of the budget.
    assert report['budget']['used_bytes']==one.bytes and report['reasons']=={'cold_budget':1}
    assert copy(s,two.digest) is None and len(objects(cloud))==1


def test_oversized_plaintext_fails_verification_without_stalling(env,archive,keys):
    s,cloud,archiver,_=env;blob=idle(archive,payload=b'Synthetic small original')
    place(cloud,blob.digest,encrypt(os.urandom(3*1024**2),keys['backup'][1],keys['cold'][1]))
    with pytest.raises(cold().ColdVerifyFailed):archiver().verify(blob.digest,blob.bytes,{})


def evicted_copy(env,archive,*,mid='lifecycle-1',payload=b'Synthetic retained contract'):
    """An original with a read-back verified cold copy, then evicted (schema 35)."""
    s,cloud,archiver,_=env;fid,blob=processed(archive,mid=mid,payload=payload)
    assert archiver().run_once(apply=True)['archived']==1 and evict(s)['evicted']==1
    assert not (s.files/blob.digest).exists()
    return fid,blob


def restorer(env,**changes):
    worker=env[2](**changes)
    return cold().ColdRestorer(worker.store,worker.config,worker.transport,reserve_bytes=changes.get('reserve_bytes',0))


def job_state(s,fid):
    with s.pool.connection() as c:return c.execute('SELECT state FROM media_jobs WHERE id=%s',(fid,)).fetchone()['state']


def resident(s,digest):
    with s.pool.connection() as c:
        return c.execute('SELECT state FROM attachment_residency WHERE digest=%s',(digest,)).fetchone()['state']=='resident'


# spec: ANYM-350
def test_rehydrate_prefers_cold_archive(env,archive):
    ar,_,key,_=archive;s,cloud,_,_=env;fid,blob=evicted_copy(env,archive)
    first=s.request_file_download(key,fid)
    assert first['request_status']=='queued' and first['rehydration_source']=='cold_archive'
    assert first['reason']=='managed_original_evicted' and 'cold_failure' not in first
    again=s.request_file_download(key,fid)
    assert again['request_status']=='coalesced' and again['rehydration_source']=='cold_archive'
    # No collector job: the provider is never asked while R2 can answer.
    assert job_state(s,fid)=='ready' and ar.claim_media('fixture') is None
    report=restorer(env).run_once()
    assert report=={'restored':1,'restored_bytes':blob.bytes,'failed':0,'provider_fallback':0,'reasons':{}}
    data=(s.files/blob.digest).read_bytes()
    assert hashlib.sha256(data).hexdigest()==blob.digest and resident(s,blob.digest)
    assert copy(s,blob.digest)['restore_state']=='idle' and copy(s,blob.digest)['state']=='verified'
    assert job_state(s,fid)=='ready' and ar.claim_media('fixture') is None
    assert s.request_file_download(key,fid)['request_status']=='ready'
    assert not [p for p in s.files.iterdir() if p.name.startswith('.pending-')]


# spec: ANYM-350
@pytest.mark.parametrize('failure',['missing','different','truncated','unavailable'])
def test_rehydrate_falls_back_to_provider_on_cold_failure(env,archive,failure):
    ar,_,key,_=archive;s,cloud,_,_=env;fid,blob=evicted_copy(env,archive)
    assert s.request_file_download(key,fid)['rehydration_source']=='cold_archive'
    (cloud/failure).write_text('') if failure=='unavailable' else (cloud/'tamper').write_text(failure)
    report=restorer(env).run_once()
    assert report['restored']==0 and report['failed']==1 and report['provider_fallback']==1
    assert not (s.files/blob.digest).exists() and not resident(s,blob.digest)
    assert not [p for p in s.files.iterdir() if p.name.startswith('.pending-')]
    row=copy(s,blob.digest);code=row['restore_error']
    assert row['restore_state']=='failed'
    if failure=='unavailable':
        # Nothing is known against the stored copy: it stays verified.
        assert code=='cold_unavailable' and row['state']=='verified'
    else:
        assert code in {'cold_object_missing','cold_ciphertext_mismatch','cold_verify_failed'}
        assert row['state']=='failed' and row['verified_at'] is None
    # The provider path ran inside the request's demand window.
    assert job_state(s,fid)=='queued'
    after=s.request_file_download(key,fid)
    assert after['rehydration_source']=='provider' and after['cold_failure']==code
    assert after['request_status']=='coalesced'
    job=ar.claim_media('fixture');assert job['id']==fid


# spec: ANYM-350
def test_rehydrate_without_cold_copy_uses_provider(archive):
    from messaging_service.file_lifecycle import LifecycleWorker
    ar,s,key,_=archive;fid,blob=processed(archive)
    # Evicted before schema 35: no copy row, the provider path is unchanged.
    assert LifecycleWorker(s).run_once(apply=True)['evicted']==1
    result=s.request_file_download(key,fid)
    assert result['request_status']=='queued' and result['reason']=='managed_original_evicted'
    assert result['rehydration_source']=='provider' and 'cold_failure' not in result
    assert s.request_file_download(key,fid)['request_status']=='coalesced'
    assert ar.claim_media('fixture')['id']==fid


# spec: ANYM-350
def test_stalled_cold_restore_falls_back_to_provider(env,archive):
    ar,_,key,_=archive;s,cloud,_,_=env;fid,blob=evicted_copy(env,archive)
    assert s.request_file_download(key,fid)['rehydration_source']=='cold_archive'
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_cold_copies SET restore_requested_at=clock_timestamp()-interval '15 minutes 1 second' WHERE digest=%s",(blob.digest,))
    # The worker no longer picks a stalled restore.
    assert restorer(env).run_once()['restored']==0 and calls(cloud).count('get_object')==1
    result=s.request_file_download(key,fid)
    assert result['request_status']=='queued' and result['rehydration_source']=='provider'
    assert result['cold_failure']=='cold_restore_stalled'
    assert ar.claim_media('fixture')['id']==fid
    assert copy(s,blob.digest)['restore_state']=='failed' and copy(s,blob.digest)['state']=='verified'


# spec: ANYM-350
def test_outstanding_cold_restores_are_globally_bounded(env,archive,monkeypatch):
    from messaging_service import media_requests
    from messaging_service.store import APIError
    _,_,key,_=archive;s,_,_,_=env
    one,_=evicted_copy(env,archive,mid='bound-1',payload=b'Synthetic bounded first')
    two,_=evicted_copy(env,archive,mid='bound-2',payload=b'Synthetic bounded second')
    monkeypatch.setattr(media_requests,'GLOBAL_LIMIT',1)
    assert s.request_file_download(key,one)['rehydration_source']=='cold_archive'
    with pytest.raises(APIError,match='media_request_limit'):s.request_file_download(key,two)
    assert copy(s,_digest_of(s,two))['restore_state']=='idle'


def _digest_of(s,fid):
    with s.pool.connection() as c:return c.execute('SELECT digest FROM attachments WHERE id=%s',(fid,)).fetchone()['digest']


# spec: ANYM-350
@pytest.mark.parametrize('limit',['disk_reserve','cold_size_limit'])
def test_restore_honors_disk_reserve_and_object_size_bound(env,archive,limit):
    import shutil as disk
    ar,_,key,_=archive;s,_,_,_=env;fid,blob=evicted_copy(env,archive)
    s.request_file_download(key,fid)
    changes={'reserve_bytes':disk.disk_usage(s.files).free+1} if limit=='disk_reserve' else {'max_object_bytes':blob.bytes-1}
    report=restorer(env,**changes).run_once()
    assert report['reasons']=={limit:1} and report['provider_fallback']==1
    assert not (s.files/blob.digest).exists()
    assert not [p for p in s.files.iterdir() if p.name.startswith('.pending-')]
    # A local limit says nothing against the copy.
    assert copy(s,blob.digest)['state']=='verified' and copy(s,blob.digest)['restore_error']==limit


# spec: ANYM-350
def test_least_privilege_gateway_queues_and_cold_role_restores(env,archive):
    from contextlib import contextmanager
    from copy import copy as clone
    from uuid import uuid4
    from psycopg import sql
    _,_,key,_=archive;s,_,_,_=env;fid,blob=evicted_copy(env,archive)
    roles={kind:kind+'_'+uuid4().hex for kind in ('messaging_gateway','messaging_cold_archive')}
    class RestrictedPool:
        def __init__(self,role):self.role=role
        @contextmanager
        def connection(self):
            with s.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(self.role)))
                yield c
    root=Path(__file__).resolve().parents[1]/'services/messaging/messaging_service'
    for kind,role in roles.items():
        with s.pool.connection() as c:
            c.execute((root/(kind.removeprefix('messaging_')+'_role.sql')).read_text().replace(kind,role))
    gateway=clone(s);gateway.pool=RestrictedPool(roles['messaging_gateway'])
    worker=restorer(env);worker.store=clone(s);worker.store.pool=RestrictedPool(roles['messaging_cold_archive'])
    try:
        with s.pool.connection() as c:
            c.execute(sql.SQL('REVOKE SELECT,UPDATE ON attachment_cold_copies FROM {}').format(sql.Identifier(roles['messaging_gateway'])))
        # A gateway role from before the restore grant keeps the provider path.
        old=gateway.request_file_download(key,fid)
        assert old['request_status']=='queued' and old['rehydration_source']=='provider'
        with s.pool.connection() as c:
            c.execute("UPDATE media_jobs SET state='ready',requested_until=NULL WHERE id=%s",(fid,))
            c.execute((root/'gateway_role.sql').read_text().replace('messaging_gateway',roles['messaging_gateway']))
        assert gateway.request_file_download(key,fid)['rehydration_source']=='cold_archive'
        assert worker.run_once()['restored']==1 and resident(s,blob.digest)
    finally:
        for role in roles.values():
            with s.pool.connection() as c:
                c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


# spec: ANYM-350
def test_restore_drill_rehydrates_from_cold(env,archive,tmp_path):
    """Restore a backup point into an isolated files directory, then rehydrate from cold/.

    The disposable test database stands in for the restored database (the dump
    is a stub; pg_restore itself is the BACKUP.md drill); the CAS is a fresh
    directory filled only from the bundle.
    """
    from copy import copy as clone
    from messaging_service.backup import create_snapshot,verify_bundle,verify_snapshot
    from messaging_service.blobs import Blob,BlobWriter
    _,_,key,_=archive;s,cloud,_,_=env;fid,blob=evicted_copy(env,archive)
    create_snapshot(s,tmp_path/'point',[sys.executable,'-c','print("synthetic dump")'])
    manifest=verify_bundle(tmp_path/'point')
    [absent]=[item for item in manifest['absent_blobs'] if item['digest']==blob.digest]
    assert absent['cold_object_key']=='synthetic-owner/v1/cold/'+blob.digest+'.age'
    isolated=tmp_path/'restored-files';writer=BlobWriter(isolated,reserve_bytes=0)
    for item in manifest['blobs']:
        with (tmp_path/'point/files'/item['digest']).open('rb') as source:
            writer.write(iter(lambda:source.read(1024**2),b''),expected_bytes=item['bytes'],expected_sha256=item['digest'])
    target=clone(s);target.files=isolated
    assert verify_snapshot(target,tmp_path/'point')['status']=='verified'
    assert s.request_file_download(key,fid)['rehydration_source']=='cold_archive'
    worker=restorer(env);worker.store=target
    assert worker.run_once()['restored']==1
    writer.verify(Blob(blob.digest,blob.bytes))
    assert hashlib.sha256((isolated/blob.digest).read_bytes()).hexdigest()==absent['digest']
    assert not (s.files/blob.digest).exists()


# spec: ANYM-350
def test_compose_runs_cold_restore_with_the_cold_key_and_writable_cas():
    import yaml
    from scripts.production import validate_argv
    root=Path(__file__).resolve().parents[1]
    services=yaml.safe_load((root/'deploy/compose.file-lifecycle.yaml').read_text())['services']
    service,archive_service=services['cold-restore'],services['cold-archive']
    validate_argv(service['command'])
    assert service['command'][:4]==['python','-m','messaging_service.cold_archive','--restore']
    assert service['profiles']==archive_service['profiles'] and service['image']==archive_service['image']
    assert 'files:/data' in service['volumes']
    secrets=[v for v in service['volumes'] if isinstance(v,dict)]
    assert secrets==[v for v in archive_service['volumes'] if v['target']!='/data'] and all(v['read_only'] for v in secrets)
    assert service['user']=='10001:10001' and service['read_only'] and service['cap_drop']==['ALL']
    from messaging_service.cold_archive import main
    with pytest.raises(SystemExit):main(['--restore','--apply','--config','/nonexistent','--dsn-file','/nonexistent'])
