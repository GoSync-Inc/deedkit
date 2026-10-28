"""Synthetic S3 state machine checks; no credentials, provider sessions or data."""
from datetime import datetime, timezone, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import struct
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from messaging_service import backup_r2_receiver as r2

BID='20261016T091703Z-fedcba9876543210'
OLD='20261013T091703Z-0123456789abcdef'
THIRD='20261016T101703Z-1123456789abcdef'
SNAPSHOT='a'*64

def sha(data):return hashlib.sha256(data).hexdigest()
def write_json(path,value):
    path.write_text(json.dumps(value,separators=(',',':'))+'\n');path.chmod(0o600)

@pytest.fixture
def config(tmp_path,monkeypatch):
    # Native Docker CI runs as root in /tmp, deliberately unsuitable for root
    # operator evidence. Ordinary receive/config tests use the upload identity.
    if os.geteuid()==0:monkeypatch.setattr(os,"geteuid",lambda:1000)
    state=tmp_path/'state';state.mkdir(mode=0o700)
    credentials=tmp_path/'credentials.json'
    write_json(credentials,{'access_key_id':'a'*32,'secret_access_key':'b'*64})
    value={'v':1,'endpoint':'https://'+'1'*32+'.r2.cloudflarestorage.com',
           'bucket':'synthetic-backups','prefix':'test-owner','state_directory':str(state),
           'credentials_file':str(credentials),'max_bytes':20*1024**2,'quota_bytes':40*1024**2,
           'max_objects':2,'part_bytes':5*1024**2,'timeout_seconds':900}
    path=tmp_path/'config.json';write_json(path,value)
    return r2.load_config(path)

class CloudError(Exception):
    def __init__(self,code):
        self.response={'Error':{'Code':str(code)},'ResponseMetadata':{'HTTPStatusCode':int(code) if str(code).isdigit() else (404 if code=='NoSuchUpload' else 500)}}
        super().__init__('cloud diagnostic must never reach wire')

class Cloud:
    """Byte-persistent S3 simulator with atomic conditions and dropped responses."""
    def __init__(self):self.objects={};self.uploads={};self.calls=[];self.fail={};self.hidden=set()
    def hit(self,name):
        self.calls.append(name)
        if self.fail.get(name):
            failure=self.fail.pop(name)
            raise failure
    def head_object(self,**kw):
        self.hit('head_object');key=kw['Key']
        if key not in self.objects or key in self.hidden:raise CloudError(404)
        return {'ContentLength':len(self.objects[key]),'ETag':'not-a-sha256'}
    def get_object(self,**kw):
        self.hit('get_object');key=kw['Key']
        if key not in self.objects:raise CloudError(404)
        return {'ContentLength':len(self.objects[key]),'Body':io.BytesIO(self.objects[key])}
    def list_objects_v2(self,**kw):
        self.hit('list_objects_v2')
        return {'IsTruncated':False,'Contents':[{'Key':k,'Size':len(v)} for k,v in self.objects.items() if k.startswith(kw['Prefix'])]}
    def list_multipart_uploads(self,**kw):
        self.hit('list_multipart_uploads')
        return {'IsTruncated':False,'Uploads':[{'Key':v['key'],'UploadId':k} for k,v in self.uploads.items() if v['key'].startswith(kw['Prefix'])]}
    def create_multipart_upload(self,**kw):
        self.hit('create_multipart_upload');uid=str(len(self.calls))
        self.uploads[uid]={'key':kw['Key'],'parts':{}}
        if self.fail.pop('created_response',False):raise TimeoutError('lost create reply')
        return {'UploadId':uid}
    def upload_part(self,**kw):
        self.hit('upload_part');self.uploads[kw['UploadId']]['parts'][kw['PartNumber']]=bytes(kw['Body'])
        return {'ETag':'part-'+str(kw['PartNumber'])}
    def complete_multipart_upload(self,**kw):
        self.hit('complete_multipart_upload');upload=self.uploads.pop(kw['UploadId'])
        part_sizes=[len(upload['parts'][p['PartNumber']]) for p in kw['MultipartUpload']['Parts']]
        assert all(size==part_sizes[0] and size>=5*1024**2 for size in part_sizes[:-1])
        assert kw['MultipartUpload']['Parts']==[{'PartNumber':n,'ETag':'part-'+str(n)} for n in sorted(upload['parts'])]
        self.objects[kw['Key']]=b''.join(upload['parts'][p['PartNumber']] for p in kw['MultipartUpload']['Parts'])
        if self.fail.pop('complete_response',False):raise TimeoutError('lost complete reply')
        return {'ETag':'multipart-not-sha256'}
    def abort_multipart_upload(self,**kw):
        self.hit('abort_multipart_upload')
        if kw['UploadId'] not in self.uploads:raise CloudError('NoSuchUpload')
        self.uploads.pop(kw['UploadId'])
        if self.fail.pop('abort_response',False):raise TimeoutError('lost abort reply')
        return {}
    def put_object(self,**kw):
        self.hit('put_object');assert kw['IfNoneMatch']=='*'
        if kw['Key'] in self.objects:raise CloudError(412)
        self.objects[kw['Key']]=bytes(kw['Body'])
        if self.fail.pop('put_response',False):raise TimeoutError('lost put reply')
        return {}
    def delete_object(self,**kw):
        self.hit('delete_object');self.objects.pop(kw['Key'],None)
        if self.fail.pop('delete_response',False):raise TimeoutError('lost delete reply')
        return {}

def wire(data=None,bid=BID,snapshot=SNAPSHOT,footer=None,tail=b''):
    data=data if data is not None else r2.AGE_HEADER+b'ciphertext fixture'*8
    header={'v':2,'id':bid,'max_bytes':max(64,len(data)),'snapshot_sha256':snapshot}
    output=json.dumps(header).encode()+b'\n'
    for start in range(0,len(data),1024**2):
        frame=data[start:start+1024**2];output+=struct.pack('!I',len(frame))+frame
    return output+b'\0'*4+json.dumps(footer if footer is not None else {'bytes':len(data),'sha256':sha(data)}).encode()+b'\n'+tail

def receive(config,cloud,**kw):return r2.receive(config,io.BytesIO(wire(**kw)),client=cloud)

@pytest.mark.parametrize('field,value',[
    ('endpoint','http://'+'1'*32+'.r2.cloudflarestorage.com'),
    ('endpoint','https://example.com'),('endpoint','https://'+'1'*32+'.r2.cloudflarestorage.com/x'),
    ('endpoint','https://user@'+'1'*32+'.r2.cloudflarestorage.com'),
    ('bucket','Bad_Bucket'),('prefix','../escape'),('prefix',''),
    ('max_objects',3),('max_bytes',True),('quota_bytes',1),('part_bytes',1024),('unknown','secret'),
])
def test_config_rejects_unsafe_or_unbounded_options(config,tmp_path,field,value):
    raw=dict(config);raw[field]=value
    path=tmp_path/'bad.json';write_json(path,raw)
    with pytest.raises(ValueError):r2.load_config(path)

@pytest.mark.parametrize('attack',['public_config','symlink_config','hardlink_config','public_credentials','unknown_credentials','duplicate_json'])
def test_config_and_credentials_require_private_regular_unique_json(config,tmp_path,attack):
    path=tmp_path/'checked.json';write_json(path,dict(config))
    if attack=='public_config':path.chmod(0o644)
    elif attack=='symlink_config':target=tmp_path/'actual.json';path.rename(target);path.symlink_to(target)
    elif attack=='hardlink_config':os.link(path,tmp_path/'other.json')
    elif attack=='public_credentials':Path(config['credentials_file']).chmod(0o644)
    elif attack=='unknown_credentials':write_json(Path(config['credentials_file']),{'access_key_id':'a','secret_access_key':'b','session_token':'c'})
    else:path.write_text('{"v":1,"v":1}');path.chmod(0o600)
    with pytest.raises((ValueError,OSError)):r2.load_config(path)

def test_config_accepts_explicit_eu_endpoint(config,tmp_path):
    raw=dict(config);raw['endpoint']='https://'+'1'*32+'.eu.r2.cloudflarestorage.com'
    path=tmp_path/'eu.json';write_json(path,raw)
    assert r2.load_config(path)['endpoint']==raw['endpoint']

def test_sdk_is_lazy_and_uses_only_explicit_credentials(config,monkeypatch):
    calls=[]
    class Session:
        def __init__(self,**kw):calls.append(('session',kw))
        def client(self,*a,**kw):calls.append(('client',a,kw));return made
    made=SimpleNamespace(meta=SimpleNamespace(events=SimpleNamespace(register_first=lambda *a:calls.append(('hook',a)))))
    monkeypatch.setitem(sys.modules,'boto3',SimpleNamespace(session=SimpleNamespace(Session=Session)))
    monkeypatch.setitem(sys.modules,'botocore.config',SimpleNamespace(Config=lambda **kw:kw))
    assert r2.make_client(config) is made and calls[2]==('hook',('needs-retry.s3',r2.cancelled))
    assert calls[0][1]=={'aws_access_key_id':'a'*32,'aws_secret_access_key':'b'*64,'region_name':'auto'}
    client=calls[1][2]
    assert client['endpoint_url']==config['endpoint']
    assert client['config']['signature_version']=='s3v4'
    assert client['config']['request_checksum_calculation']=='when_required'

def test_upload_validates_all_frames_then_commits_immutable_multipart_receipt(config):
    cloud=Cloud();events=[];data=r2.AGE_HEADER+b'x'*(11*1024**2)
    result=r2.receive(config,io.BytesIO(wire(data)),client=cloud,respond=events.append)
    assert events==[{'v':2,'status':'ready','id':BID,'snapshot_sha256':SNAPSHOT}]
    assert result['sha256']==sha(data) and result['bytes']==len(data) and not result['duplicate']
    assert result['snapshot_sha256']==SNAPSHOT and 'object_key' not in result
    assert list(v for k,v in cloud.objects.items() if '/objects/' in k)==[data]
    saved=json.loads(cloud.objects[config['prefix']+'/receipts/'+BID+'.json'])
    assert saved['wire']==result and saved['object_key'] in cloud.objects
    assert cloud.calls.count('upload_part')==3
    assert cloud.calls.index('complete_multipart_upload')>cloud.calls.index('upload_part')

def test_duplicate_reuses_verified_random_ciphertext_without_reading_new_capture(config):
    cloud=Cloud();first=receive(config,cloud)
    before=cloud.calls.count('create_multipart_upload')
    source=io.BytesIO(wire().split(b'\n',1)[0]+b'\n')
    second=r2.receive(config,source,client=cloud)
    assert second=={**first,'duplicate':True}
    assert cloud.calls.count('create_multipart_upload')==before
    with pytest.raises(ValueError,match='backup_id_conflict'):receive(config,cloud,snapshot='b'*64)

@pytest.mark.parametrize('attack',['bad_age','bad_hash','bad_size','tail','missing_footer','oversized_frame','truncated_frame'])
def test_invalid_stream_never_commits_and_aborts_only_own_attempt(config,attack):
    cloud=Cloud();data=r2.AGE_HEADER+b'x'*90
    raw=wire(data)
    if attack=='bad_age':raw=wire(b'x'*100)
    elif attack=='bad_hash':raw=wire(data,footer={'bytes':len(data),'sha256':'b'*64})
    elif attack=='bad_size':raw=wire(data,footer={'bytes':1,'sha256':sha(data)})
    elif attack=='tail':raw=wire(data,tail=b'x')
    elif attack=='missing_footer':raw=raw.rsplit(b'\0'*4,1)[0]+b'\0'*4
    elif attack=='oversized_frame':raw=raw.split(b'\n',1)[0]+b'\n'+struct.pack('!I',1024**2+1)
    else:raw=raw.split(b'\n',1)[0]+b'\n'+struct.pack('!I',100)+b'a'
    with pytest.raises(ValueError):r2.receive(config,io.BytesIO(raw),client=cloud)
    assert not cloud.objects and not cloud.uploads
    assert 'complete_multipart_upload' not in cloud.calls
    assert cloud.calls.count('abort_multipart_upload')==1

@pytest.mark.parametrize('lost',['complete_response','put_response'])
def test_lost_commit_responses_reconcile_same_physical_attempt(config,lost):
    cloud=Cloud();cloud.fail[lost]=True
    with pytest.raises(RuntimeError):receive(config,cloud)
    assert cloud.calls.count('create_multipart_upload')==1
    result=receive(config,cloud)
    assert result['status']=='stored' and result['duplicate']
    assert cloud.calls.count('create_multipart_upload')==1
    assert len([k for k in cloud.objects if '/objects/' in k])==1

def test_negative_head_after_unknown_complete_preserves_evidence_and_blocks(config):
    cloud=Cloud();cloud.fail['complete_response']=True
    with pytest.raises(RuntimeError):receive(config,cloud)
    cloud.hidden=set(cloud.objects)
    with pytest.raises(RuntimeError,match='completion_outcome_unknown'):receive(config,cloud)
    with pytest.raises(RuntimeError):receive(config,cloud,bid=THIRD)
    assert cloud.calls.count('create_multipart_upload')==1
    assert list(Path(config['state_directory']).glob('*.attempt.json'))

def test_storage_authorization_error_is_never_treated_as_absent(config):
    cloud=Cloud();cloud.fail['get_object']=CloudError(403)
    with pytest.raises(RuntimeError,match='storage_unavailable'):receive(config,cloud)
    assert 'create_multipart_upload' not in cloud.calls

def test_two_object_and_byte_limits_and_busy_lock(config):
    cloud=Cloud();receive(config,cloud,bid=OLD);receive(config,cloud)
    with pytest.raises(RuntimeError,match='receiver_quota'):receive(config,cloud,bid=THIRD)
    with r2.local.locked(config['state_directory']):
        with pytest.raises(RuntimeError,match='receiver_busy'):receive(config,cloud)

def test_orphan_multipart_or_unknown_create_blocks_another_full_candidate(config):
    cloud=Cloud();cloud.fail['created_response']=True
    with pytest.raises(RuntimeError):receive(config,cloud)
    with pytest.raises(RuntimeError):receive(config,cloud,bid=THIRD)
    assert cloud.calls.count('create_multipart_upload')==1

# spec: ANYM-287
def test_open_cold_multipart_upload_does_not_block_backup_admission(config):
    cloud=Cloud();cloud.uploads['cold-open']={'key':config['prefix']+'/cold/'+'c'*64+'.age','parts':{}}
    assert receive(config,cloud)['status']=='stored' and 'cold-open' in cloud.uploads
    cloud.uploads['backup-open']={'key':config['prefix']+'/objects/orphan','parts':{}}
    with pytest.raises(RuntimeError,match='unresolved_upload_required'):receive(config,cloud,bid=THIRD)

def test_duplicate_detects_corrupted_ciphertext_despite_matching_etag(config):
    cloud=Cloud();receive(config,cloud)
    key=next(k for k in cloud.objects if '/objects/' in k);cloud.objects[key]=b'corrupted'
    with pytest.raises(ValueError,match='stored_backup_corrupt'):receive(config,cloud)

def test_download_hashes_actual_ciphertext_and_atomically_preserves_existing_destination(config,tmp_path):
    cloud=Cloud();result=receive(config,cloud);output=tmp_path/'copy.age'
    r2.download(config,BID,output,client=cloud)
    assert sha(output.read_bytes())==result['sha256'] and output.stat().st_mode&0o077==0
    before=output.read_bytes()
    with pytest.raises(FileExistsError):r2.download(config,BID,output,client=cloud)
    assert output.read_bytes()==before
    key=next(k for k in cloud.objects if '/objects/' in k);cloud.objects[key]=b'corrupted'
    with pytest.raises(ValueError):r2.download(config,BID,tmp_path/'bad.age',client=cloud)
    assert not (tmp_path/'bad.age').exists()

@pytest.fixture
def operator(config,tmp_path,monkeypatch):
    # Virtualize only fixture OS ownership/ancestor trust, retaining real modes,
    # descriptors, symlinks, hard links, locks and atomic filesystem operations.
    original_fstat=os.fstat;original_stat=Path.stat
    ancestors={(original_stat(p).st_dev,original_stat(p).st_ino) for p in (tmp_path,*tmp_path.parents)}
    def root_info(info):
        values=list(info);values[4]=0
        if (info.st_dev,info.st_ino) in ancestors:values[0]&=~0o022
        return os.stat_result(values)
    monkeypatch.setattr(os,'fstat',lambda fd:root_info(original_fstat(fd)))
    monkeypatch.setattr(Path,'stat',lambda p,*a,**kw:root_info(original_stat(p,*a,**kw)))
    monkeypatch.setattr(os,'geteuid',lambda:0)
    control=tmp_path/'control';control.mkdir(mode=0o700)
    marker=control/'protected.json';proof=control/'proof.json';cloud=Cloud()
    return config,cloud,marker,proof

def proof_for(result):
    return {'v':1,'type':'independent_restore_verification','status':'verified',
            'backup':{k:result[k] for k in ('id','bytes','sha256','snapshot_sha256')},
            'verification':{'id':'1'*32,'verified_at':datetime.now(timezone.utc).isoformat(),
                            'schema_version':31,'manifest_sha256':result['snapshot_sha256'],
                            'checks':{name:True for name in r2.CHECKS}}}

def enroll(operator):
    config,cloud,marker,proof=operator
    old=receive(config,cloud,bid=OLD);write_json(proof,proof_for(old))
    result=r2.protect(config,marker,proof,client=cloud)
    return result,sha(marker.read_bytes())

def test_initial_protect_requires_absent_marker_and_independent_proof_deletes_nothing(operator):
    config,cloud,marker,proof=operator
    first,expected=enroll(operator)
    assert first['status']=='protected'
    saved=json.loads(marker.read_bytes())
    assert saved['destination']==r2.destination(config) and saved['backups'][0]['id']==OLD
    assert cloud.calls.count('delete_object')==0
    assert r2.protect(config,marker,proof,client=cloud)['duplicate']
    candidate=receive(config,cloud);write_json(proof,proof_for(candidate))
    with pytest.raises(ValueError):r2.protect(config,marker,proof,client=cloud)
    assert sha(marker.read_bytes())==expected

@pytest.mark.parametrize('attack',['missing_check','false_check','wrong_manifest','wrong_snapshot','wrong_size','future','before_delivery','public','hardlink','symlink','nonroot','writable_ancestor','duplicate_json','proof_in_state','destination_swap'])
def test_operator_rejects_untrusted_proof_or_changed_destination_without_deletion(operator,monkeypatch,attack):
    config,cloud,marker,proof=operator
    _,expected=enroll(operator);candidate=receive(config,cloud);report=proof_for(candidate)
    if attack=='missing_check':del report['verification']['checks']['database_restore']
    elif attack=='false_check':report['verification']['checks']['original_hashes']=False
    elif attack=='wrong_manifest':report['verification']['manifest_sha256']='b'*64
    elif attack=='wrong_snapshot':report['backup']['snapshot_sha256']='b'*64
    elif attack=='wrong_size':report['backup']['bytes']+=1
    elif attack=='future':report['verification']['verified_at']='2999-01-01T00:00:00+00:00'
    elif attack=='before_delivery':report['verification']['verified_at']='2000-01-01T00:00:00+00:00'
    write_json(proof,report)
    if attack=='public':proof.chmod(0o644)
    elif attack=='hardlink':os.link(proof,proof.with_name('extra.json'))
    elif attack=='symlink':target=proof.with_name('real.json');proof.rename(target);proof.symlink_to(target)
    elif attack=='nonroot':monkeypatch.setattr(os,'geteuid',lambda:123)
    elif attack=='writable_ancestor':marker.parent.chmod(0o777)
    elif attack=='duplicate_json':proof.write_text('{"v":1,"v":1}')
    elif attack=='proof_in_state':proof=Path(config['state_directory'])/'proof.json';write_json(proof,report)
    elif attack=='destination_swap':config={**config,'bucket':'another-bucket'}
    with pytest.raises((ValueError,OSError)):
        r2.rotate(config,marker,proof,expected_protected_sha256=expected,client=cloud)
    assert sha(marker.read_bytes())==expected and 'delete_object' not in cloud.calls

def test_rotate_switches_durable_marker_before_only_exact_old_object_and_receipt_deletion(operator):
    config,cloud,marker,proof=operator
    _,expected=enroll(operator);candidate=receive(config,cloud);write_json(proof,proof_for(candidate))
    original=cloud.delete_object;seen=[]
    def deletion(**kw):
        seen.append(json.loads(marker.read_bytes())['backups'][0]['id'])
        return original(**kw)
    cloud.delete_object=deletion
    result=r2.rotate(config,marker,proof,expected_protected_sha256=expected,client=cloud)
    assert result['status']=='rotated' and seen==[BID,BID]
    assert len(cloud.objects)==2 and all(OLD not in k for k in cloud.objects)
    second=r2.rotate(config,marker,proof,expected_protected_sha256=expected,client=cloud)
    assert second['duplicate']

@pytest.mark.parametrize('failure',['before_marker','after_marker','delete_response'])
def test_interrupted_rotation_retries_exact_proof_and_original_marker_digest(operator,monkeypatch,failure):
    config,cloud,marker,proof=operator
    _,expected=enroll(operator);candidate=receive(config,cloud);write_json(proof,proof_for(candidate))
    if failure=='delete_response':cloud.fail['delete_response']=True
    else:
        original=r2.local.write_maintenance_json;fired=[]
        def write(path,value,**kw):
            if Path(path).name==marker.name and not fired:
                fired.append(True)
                if failure=='after_marker':original(path,value,**kw)
                raise OSError('synthetic fsync/replace failure')
            return original(path,value,**kw)
        monkeypatch.setattr(r2.local,'write_maintenance_json',write)
    with pytest.raises((OSError,RuntimeError)):
        r2.rotate(config,marker,proof,expected_protected_sha256=expected,client=cloud)
    result=r2.rotate(config,marker,proof,expected_protected_sha256=expected,client=cloud)
    assert result['status']=='rotated' and len(cloud.objects)==2


def test_pending_rotation_cannot_switch_proof_or_digest_and_revalidates_new_anchor(operator):
    config,cloud,marker,proof=operator
    _,expected=enroll(operator);candidate=receive(config,cloud);report=proof_for(candidate);write_json(proof,report)
    cloud.fail['delete_response']=True
    with pytest.raises(RuntimeError):r2.rotate(config,marker,proof,expected_protected_sha256=expected,client=cloud)
    calls=cloud.calls.count('delete_object')
    report['verification']['id']='2'*32;write_json(proof,report)
    with pytest.raises(ValueError):r2.rotate(config,marker,proof,expected_protected_sha256=expected,client=cloud)
    assert cloud.calls.count('delete_object')==calls
    report['verification']['id']='1'*32;write_json(proof,report)
    key=next(k for k in cloud.objects if '/objects/' in k and BID in k);cloud.objects[key]=b'corrupt'
    with pytest.raises(ValueError):r2.rotate(config,marker,proof,expected_protected_sha256=expected,client=cloud)
    assert cloud.calls.count('delete_object')==calls


def ciphertext_gets(cloud):
    original=cloud.get_object;keys=[]
    def get(**kw):
        if '/objects/' in kw['Key']:keys.append(kw['Key'])
        return original(**kw)
    cloud.get_object=get
    return keys

def old_object(cloud):return next(k for k in cloud.objects if '/objects/' in k and OLD in k)

# spec: ANYM-629
def test_rotate_downloads_only_the_candidate_ciphertext_never_the_previous_object(operator):
    config,cloud,marker,proof=operator
    _,expected=enroll(operator);candidate=receive(config,cloud);write_json(proof,proof_for(candidate))
    keys=ciphertext_gets(cloud);old=old_object(cloud)
    result=r2.rotate(config,marker,proof,expected_protected_sha256=expected,client=cloud)
    assert result['status']=='rotated' and result['removed']==1
    assert len(keys)==1 and BID in keys[0] and old not in keys

# spec: ANYM-629
def test_rotate_refuses_a_previous_object_of_the_wrong_size_before_any_deletion(operator):
    config,cloud,marker,proof=operator
    _,expected=enroll(operator);candidate=receive(config,cloud);write_json(proof,proof_for(candidate))
    old=old_object(cloud);cloud.objects[old]+=b'x'
    with pytest.raises(ValueError,match='protected_backup_corrupt'):
        r2.rotate(config,marker,proof,expected_protected_sha256=expected,client=cloud)
    assert sha(marker.read_bytes())==expected and 'delete_object' not in cloud.calls and old in cloud.objects

# spec: ANYM-629
@pytest.mark.parametrize('interrupted',[False,True])
def test_corrupt_previous_object_of_the_right_size_no_longer_blocks_rotation(operator,monkeypatch,interrupted):
    config,cloud,marker,proof=operator
    _,expected=enroll(operator);candidate=receive(config,cloud);write_json(proof,proof_for(candidate))
    old=old_object(cloud);data=cloud.objects[old];cloud.objects[old]=data[:-1]+bytes([data[-1]^1])
    keys=ciphertext_gets(cloud)
    if interrupted:
        # The marker switch lands, then the run dies before any deletion.
        cloud.fail['delete_object']=TimeoutError('lost delete')
        with pytest.raises(RuntimeError,match='storage_unavailable'):
            r2.rotate(config,marker,proof,expected_protected_sha256=expected,client=cloud)
        assert json.loads(marker.read_bytes())['backups'][0]['id']==BID and old in cloud.objects
    result=r2.rotate(config,marker,proof,expected_protected_sha256=expected,client=cloud)
    assert result['status']=='rotated' and result['duplicate'] is interrupted
    assert old not in cloud.objects and len(cloud.objects)==2 and old not in keys


def test_rotation_reuses_upload_lock_and_refuses_stale_marker(operator):
    config,cloud,marker,proof=operator
    _,expected=enroll(operator);candidate=receive(config,cloud);write_json(proof,proof_for(candidate))
    with pytest.raises(ValueError,match='protected_marker_changed'):
        r2.rotate(config,marker,proof,expected_protected_sha256='0'*64,client=cloud)
    with r2.local.locked(config['state_directory']):
        with pytest.raises(RuntimeError,match='receiver_busy'):
            r2.rotate(config,marker,proof,expected_protected_sha256=expected,client=cloud)


def test_download_receipt_reports_download_completion(config,tmp_path):
    cloud=Cloud();receive(config,cloud)
    assert r2.download(config,BID,tmp_path/'cipher.age',client=cloud)['status']=='downloaded'


def test_root_config_and_credentials_reject_untrusted_parent_ancestry(config,tmp_path,monkeypatch):
    # Real ordinary-user fixture ancestors are intentionally not root controlled.
    path=tmp_path/'operator.json';write_json(path,dict(config))
    monkeypatch.setattr(os,'geteuid',lambda:0)
    with pytest.raises((ValueError,OSError)):r2.load_config(path)


def test_config_cannot_be_redirected_after_parent_open(config,tmp_path,monkeypatch):
    directory=tmp_path/'checked';directory.mkdir(mode=0o700)
    path=directory/'config.json';write_json(path,dict(config))
    other=tmp_path/'other';other.mkdir(mode=0o700)
    modified={**config,'bucket':'attacker-bucket'};write_json(other/'config.json',modified)
    original=os.open;swapped=[]
    def opening(target,flags,*a,**kw):
        # Interpose an ancestor exactly at the leaf open. A safe implementation
        # reads the already-held parent, never this replacement/symlink.
        if str(target) in ('config.json',str(path)) and not swapped:
            swapped.append(True);directory.rename(tmp_path/'saved');directory.symlink_to(other)
        return original(target,flags,*a,**kw)
    monkeypatch.setattr(os,'open',opening)
    assert r2.load_config(path)['bucket']==config['bucket']


def test_sender_original_command_cannot_select_root_maintenance(config,monkeypatch,capsys):
    cloud=Cloud();raw=wire()
    monkeypatch.setenv('SSH_ORIGINAL_COMMAND','rotate --protected-file /etc/protected --restore-proof /tmp/forged')
    monkeypatch.setattr(r2,'load_config',lambda path:config)
    monkeypatch.setattr(r2,'make_client',lambda conf:cloud)
    monkeypatch.setattr(r2.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(raw)))
    monkeypatch.setattr(r2,'rotate',lambda *a,**kw:pytest.fail('sender selected maintenance'))
    assert r2.main(['--config','/synthetic/private.json','receive'])==0
    events=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event['status'] for event in events]==['ready','stored']


def test_receiver_cli_never_exposes_sdk_or_credential_diagnostics(config,monkeypatch,capsys):
    monkeypatch.setattr(r2,'load_config',lambda path:config)
    monkeypatch.setattr(r2,'make_client',lambda conf:(_ for _ in ()).throw(RuntimeError('SENSITIVE PRIVATE SIGNED URL')))
    monkeypatch.setattr(r2.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(wire())))
    assert r2.main(['--config','/synthetic/private.json'])==1
    output=capsys.readouterr().out
    assert 'SENSITIVE' not in output and json.loads(output)['code']=='receiver_failed'


@pytest.mark.parametrize('attack',['duplicate_header','v1','boolean_bound','frame_exceeds_bound','boolean_footer','duplicate_footer'])
def test_strict_v2_wire_rejects_ambiguous_and_misbounded_input(config,attack):
    cloud=Cloud();raw=wire();header,rest=raw.split(b'\n',1);value=json.loads(header)
    if attack=='duplicate_header':header=b'{"v":2,'+header[1:]
    elif attack=='v1':value={'v':1,'id':BID,'bytes':100,'sha256':'a'*64};header=json.dumps(value).encode()
    elif attack=='boolean_bound':value['max_bytes']=True;header=json.dumps(value).encode()
    elif attack=='frame_exceeds_bound':value['max_bytes']=64;header=json.dumps(value).encode()
    elif attack=='boolean_footer':rest=rest.rsplit(b'\0'*4,1)[0]+b'\0'*4+b'{"bytes":true,"sha256":"'+b'a'*64+b'"}\n'
    else:rest=rest[:-2]+b',"bytes":100}\n'
    with pytest.raises(ValueError):r2.receive(config,io.BytesIO(header+b'\n'+rest),client=cloud)
    assert not cloud.objects


def test_conflicting_atomic_receipt_winner_never_overwrites_or_returns_our_candidate(config):
    cloud=Cloud();original=cloud.put_object
    def competing(**kw):
        conflict=json.loads(kw['Body']);conflict['wire']['snapshot_sha256']='b'*64
        cloud.objects[kw['Key']]=json.dumps(conflict).encode()
        return original(**kw)
    cloud.put_object=competing
    with pytest.raises(ValueError,match='backup_id_conflict'):receive(config,cloud)
    receipt=json.loads(cloud.objects[r2.receipt_key(config,BID)])
    assert receipt['wire']['snapshot_sha256']=='b'*64
    assert list(Path(config['state_directory']).glob('*.attempt.json'))
    with pytest.raises(RuntimeError):receive(config,cloud,bid=THIRD)


def test_unchanged_negative_head_cannot_clear_an_uncertain_precommit_completion(config):
    cloud=Cloud();cloud.fail['complete_multipart_upload']=TimeoutError('request may still complete')
    with pytest.raises(RuntimeError,match='completion_outcome_unknown'):receive(config,cloud)
    assert not cloud.objects and cloud.uploads
    with pytest.raises(RuntimeError,match='completion_outcome_unknown'):receive(config,cloud)
    assert cloud.calls.count('create_multipart_upload')==1 and 'abort_multipart_upload' not in cloud.calls


def test_orphan_committed_object_counts_toward_two_slots_and_quota(config):
    cloud=Cloud();prefix=config['prefix']+'/objects/'
    cloud.objects[prefix+'untracked-one']=b'x';cloud.objects[prefix+'untracked-two']=b'x'
    with pytest.raises(RuntimeError,match='receiver_quota'):receive(config,cloud)
    assert 'create_multipart_upload' not in cloud.calls
    cloud.objects.pop(prefix+'untracked-two')
    cloud.objects[prefix+'untracked-one']=b'x'*config['quota_bytes']
    with pytest.raises(RuntimeError,match='receiver_quota'):receive(config,cloud)


def test_failed_part_aborts_only_own_unpublished_mpu(config):
    cloud=Cloud();cloud.fail['upload_part']=CloudError(403)
    with pytest.raises(RuntimeError):receive(config,cloud)
    assert not cloud.uploads and not cloud.objects
    assert cloud.calls.count('abort_multipart_upload')==1 and 'complete_multipart_upload' not in cloud.calls


def test_failed_abort_preserves_attempt_until_same_id_reconciliation(config):
    cloud=Cloud();cloud.fail['abort_multipart_upload']=TimeoutError('unknown abort')
    with pytest.raises(RuntimeError):receive(config,cloud,tail=b'invalid')
    assert list(Path(config['state_directory']).glob('*.attempt.json'))
    with pytest.raises(RuntimeError,match='incomplete_upload_aborted_retry'):receive(config,cloud)
    assert not cloud.uploads
    assert receive(config,cloud)['status']=='stored'


def test_object_inventory_paginates_without_ignoring_previous_pages(config):
    cloud=Cloud();calls=[]
    def pages(**kw):
        calls.append(kw.get('ContinuationToken'))
        if len(calls)==1:return {'IsTruncated':True,'NextContinuationToken':'next','Contents':[{'Key':config['prefix']+'/objects/one','Size':100}]}
        return {'IsTruncated':False,'Contents':[{'Key':config['prefix']+'/objects/two','Size':100}]}
    cloud.list_objects_v2=pages
    with pytest.raises(RuntimeError,match='receiver_quota'):receive(config,cloud)
    assert calls==[None,'next']


def test_lost_abort_response_reconciles_authoritative_no_such_upload_before_retry(config):
    cloud=Cloud();cloud.fail['abort_response']=True
    with pytest.raises(RuntimeError,match='storage_unavailable'):
        receive(config,cloud,tail=b'invalid')
    journal=Path(config['state_directory'])/r2.attempt_name(BID)
    assert json.loads(journal.read_bytes())['phase']=='uploading'
    assert not cloud.uploads and not cloud.objects
    with pytest.raises(RuntimeError,match='incomplete_upload_aborted_retry'):
        receive(config,cloud)
    assert not journal.exists()
    assert cloud.calls.count('create_multipart_upload')==1
    assert receive(config,cloud)['status']=='stored'
    assert len([k for k in cloud.objects if '/objects/' in k])==1


@pytest.mark.parametrize('error',[CloudError(403),CloudError(404),CloudError('NoSuchBucket'),TimeoutError('lost retry')])
def test_abort_retry_preserves_precompletion_evidence_for_other_errors(config,error):
    cloud=Cloud();cloud.fail['abort_response']=True
    with pytest.raises(RuntimeError):receive(config,cloud,tail=b'invalid')
    journal=Path(config['state_directory'])/r2.attempt_name(BID);before=journal.read_bytes()
    cloud.fail['abort_multipart_upload']=error
    with pytest.raises(RuntimeError,match='storage_unavailable'):receive(config,cloud)
    assert journal.read_bytes()==before
    assert cloud.calls.count('create_multipart_upload')==1 and not cloud.objects


def test_no_such_upload_cannot_clear_uncertain_completion_evidence(config):
    cloud=Cloud();cloud.fail['complete_multipart_upload']=TimeoutError('completion may arrive later')
    with pytest.raises(RuntimeError):receive(config,cloud)
    journal=Path(config['state_directory'])/r2.attempt_name(BID);before=journal.read_bytes()
    cloud.fail['head_object']=CloudError('NoSuchUpload')
    with pytest.raises(RuntimeError,match='storage_unavailable'):receive(config,cloud)
    assert journal.read_bytes()==before
    assert cloud.calls.count('create_multipart_upload')==1 and 'abort_multipart_upload' not in cloud.calls


@pytest.fixture
def uploader_state_owner(operator,monkeypatch):
    # Keep real permissions/openat/flock operations. Virtualize only state and
    # lock UID to model the upload account separately from root maintenance.
    config,cloud,marker,proof=operator
    state=Path(config['state_directory']);state_info=state.stat()
    original=os.fstat
    def with_upload_owner(fd):
        info=original(fd)
        is_state=(info.st_dev,info.st_ino)==(state_info.st_dev,state_info.st_ino)
        try:lock_info=(state/'.receiver.lock').stat()
        except FileNotFoundError:lock_info=None
        is_lock=lock_info is not None and (info.st_dev,info.st_ino)==(lock_info.st_dev,lock_info.st_ino)
        if is_state or is_lock:
            values=list(info);values[4]=12345;return os.stat_result(values)
        return info
    monkeypatch.setattr(os,'fstat',with_upload_owner)
    return operator


@pytest.mark.parametrize('operation',['download','protect','rotate'])
def test_root_operations_do_not_create_an_unusable_root_lock_in_uploader_state(uploader_state_owner,tmp_path,operation):
    config,cloud,marker,proof=uploader_state_owner
    lock=Path(config['state_directory'])/'.receiver.lock'
    assert not lock.exists()
    with pytest.raises(ValueError,match='uploader_lock_initialization_required'):
        if operation=='download':r2.download(config,BID,tmp_path/'candidate.age',client=cloud)
        elif operation=='protect':r2.protect(config,marker,proof,client=cloud)
        else:r2.rotate(config,marker,proof,expected_protected_sha256='0'*64,client=cloud)
    assert not lock.exists() and not cloud.calls


def test_root_shared_lock_reuses_upload_owned_private_lock_without_changing_owner(uploader_state_owner,monkeypatch):
    config,*_=uploader_state_owner
    lock=Path(config['state_directory'])/'.receiver.lock';lock.touch(mode=0o600)
    original=os.open;lock_opens=[]
    def opening(path,flags,*a,**kw):
        if str(path)=='.receiver.lock':lock_opens.append(flags)
        return original(path,flags,*a,**kw)
    monkeypatch.setattr(os,'open',opening)
    with r2.state_lock(config) as fd:
        assert os.fstat(fd).st_uid==12345
        with pytest.raises(RuntimeError,match='receiver_busy'):
            with r2.state_lock(config):pytest.fail('second lock acquisition succeeded')
    assert lock_opens and all(not flags&os.O_CREAT for flags in lock_opens)
    assert lock.stat().st_mode&0o077==0


def test_root_shared_lock_rejects_preexisting_wrong_owner_in_uploader_state(operator,monkeypatch):
    config,*_=operator
    state=Path(config['state_directory']);state_info=state.stat()
    lock=state/'.receiver.lock';lock.touch(mode=0o600)
    original=os.fstat
    def different_owner(fd):
        info=original(fd)
        if (info.st_dev,info.st_ino)==(state_info.st_dev,state_info.st_ino):
            values=list(info);values[4]=12345;return os.stat_result(values)
        return info
    monkeypatch.setattr(os,'fstat',different_owner)
    before=lock.stat()
    with pytest.raises(ValueError,match='uploader_lock_initialization_required'):
        with r2.state_lock(config):pytest.fail('wrong-owner lock was accepted')
    after=lock.stat()
    assert (after.st_uid,after.st_mode,after.st_ino)==(before.st_uid,before.st_mode,before.st_ino)


COLD='c'*64

def cold_wire(op,digest=COLD,data=None,extra=None):
    header={'v':1,'op':op,'digest':digest,**(extra or {})}
    output=json.dumps(header).encode()+b'\n'
    if data is not None:
        for start in range(0,len(data),1024**2):
            frame=data[start:start+1024**2];output+=struct.pack('!I',len(frame))+frame
        output+=b'\0'*4+json.dumps({'bytes':len(data),'sha256':sha(data)}).encode()+b'\n'
    return output

def cold(config,cloud,op,**kw):
    sink=io.BytesIO();r2.cold_serve(config,io.BytesIO(cold_wire(op,**kw)),sink,client=cloud)
    lines=sink.getvalue().split(b'\n')
    return [json.loads(line) for line in lines if line.startswith(b'{')],sink.getvalue()

def test_cold_route_is_prefix_and_verb_scoped(config):
    cloud=Cloud();data=r2.AGE_HEADER+b'synthetic cold ciphertext'*8
    key=config['prefix']+'/cold/'+COLD+'.age'
    responses,_=cold(config,cloud,'put',data=data)
    assert responses==[{'v':1,'status':'ready','key':key},
                       {'v':1,'status':'stored','key':key,'bytes':len(data),'sha256':sha(data)}]
    assert cloud.objects=={key:data}
    # An existing key is reported and kept: no second upload, no overwrite.
    other=r2.AGE_HEADER+b'a different encryption of the same original'*4
    responses,_=cold(config,cloud,'put',data=other)
    assert responses==[{'v':1,'status':'exists','key':key,'bytes':len(data)}] and cloud.objects=={key:data}
    responses,raw=cold(config,cloud,'get')
    assert responses[0]=={'v':1,'status':'found','key':key,'bytes':len(data)} and raw.endswith(data)
    with pytest.raises(ValueError,match='cold_object_missing'):cold(config,cloud,'get',digest='d'*64)
    for op,digest,extra in [('list',COLD,None),('delete',COLD,None),('overwrite',COLD,None),('put','../'+COLD[3:],None),
                            ('get','C'*64,None),('get',COLD[:63],None),('get',COLD+'/x',None),('get',None,None),
                            ('get',COLD,{'key':'test-owner/objects/x'}),('get',COLD,{'prefix':'other'})]:
        with pytest.raises(ValueError,match='invalid_cold_request'):cold(config,cloud,op,digest=digest,extra=extra)
    with pytest.raises(ValueError,match='encrypted_backup_required'):
        cold(config,cloud,'put',digest='e'*64,data=b'plaintext is never stored'*4)
    assert set(cloud.objects)=={key}
    assert not {'delete_object','list_objects_v2','list_multipart_uploads'}&set(cloud.calls)

def test_cold_route_streams_large_objects_through_bounded_multipart(config):
    cloud=Cloud();data=r2.AGE_HEADER+b'm'*(6*1024**2)
    responses,_=cold(config,cloud,'put',data=data)
    assert responses[-1]['status']=='stored' and cloud.objects[config['prefix']+'/cold/'+COLD+'.age']==data
    assert cloud.calls.count('upload_part')==2 and 'put_object' not in cloud.calls
    cloud=Cloud()
    with pytest.raises(ValueError,match='cold_size_limit'):
        cold({**config,'cold_max_bytes':1024},cloud,'put',data=data)
    assert cloud.objects=={} and cloud.uploads=={}

def test_backup_prune_ignores_cold_prefix(operator):
    config,cloud,marker,proof=operator
    archived={config['prefix']+'/cold/'+d*64+'.age':r2.AGE_HEADER+d.encode()*64 for d in 'abc'}
    cloud.objects.update(archived)
    # Cold copies never occupy a backup slot or byte quota.
    assert set(r2.inventory(config,cloud))==set()
    _,expected=enroll(operator);candidate=receive(config,cloud);write_json(proof,proof_for(candidate))
    assert r2.rotate(config,marker,proof,expected_protected_sha256=expected,client=cloud)['status']=='rotated'
    assert all(cloud.objects[key]==value for key,value in archived.items())
    assert len([k for k in cloud.objects if '/cold/' not in k])==2

def test_interrupted_cold_multipart_attempt_is_neutralized_before_a_retry(config):
    cloud=Cloud();data=r2.AGE_HEADER+b'n'*(6*1024**2);key=config['prefix']+'/cold/'+COLD+'.age'
    journal=Path(config['state_directory'])/'cold'/(COLD+'.json')
    # 1. A lost create response: the unknown upload can never complete.
    cloud.fail['created_response']=True
    with pytest.raises(RuntimeError,match='storage_unavailable'):cold(config,cloud,'put',data=data)
    assert not journal.exists() and len(cloud.uploads)==1
    orphan=next(iter(cloud.uploads))
    # 2. A process dies mid-upload: the journal keeps its upload ID.
    real=cloud.complete_multipart_upload
    def died(**kw):raise KeyboardInterrupt('synthetic process death')
    cloud.complete_multipart_upload=died
    with pytest.raises(KeyboardInterrupt):cold(config,cloud,'put',data=data)
    cloud.complete_multipart_upload=real
    saved=json.loads(journal.read_text())
    assert saved['phase']=='completing' and saved['upload_id'] in cloud.uploads
    # 3. The retry first aborts that journaled upload, so it can never land later.
    responses,_=cold(config,cloud,'put',data=data)
    assert responses[-1]['status']=='stored' and cloud.objects[key]==data
    assert saved['upload_id'] not in cloud.uploads and set(cloud.uploads)=={orphan} and not journal.exists()

def test_failed_cold_abort_keeps_the_journal_for_reconciliation(config):
    cloud=Cloud();data=r2.AGE_HEADER+b'n'*(6*1024**2)
    journal=Path(config['state_directory'])/'cold'/(COLD+'.json')
    cloud.fail['complete_multipart_upload']=CloudError(500);cloud.fail['abort_multipart_upload']=CloudError(500)
    with pytest.raises(RuntimeError,match='completion_outcome_unknown'):cold(config,cloud,'put',data=data)
    with pytest.raises(RuntimeError,match='storage_unavailable'):cold(config,cloud,'put',data=data)
    assert json.loads(journal.read_text())['phase']=='completing' and cloud.objects=={}
    # Backup admission ignores cold journals.
    assert receive(config,cloud)['status']=='stored'

# spec: ANYM-287 (receiver cold route state), ANYM-297 criterion 7 (cold copy crash safety)
def test_cold_lock_fsyncs_state_directory_before_every_cold_use(config,monkeypatch):
    state=Path(config['state_directory']);inode=state.stat().st_ino;real=os.fsync;synced=[];fail=[True]
    def fsync(fd):
        if fail and os.fstat(fd).st_ino==inode:fail.clear();raise OSError(5,'synthetic EIO')
        synced.append(os.fstat(fd).st_ino);real(fd)
    monkeypatch.setattr(os,'fsync',fsync)
    # A failed parent sync after mkdir refuses the request before its lock is taken.
    with pytest.raises(OSError):
        with r2.cold_lock(config):pytest.fail('cold lock entered without a durable cold/ entry')
    assert (state/'cold').is_dir() and not (state/'cold'/'.cold.lock').exists()
    # Existence is no proof of a completed sync: the retry, or any concurrent
    # caller, syncs the parent before its lock or any journal is used.
    for _ in range(2):
        synced.clear()
        with r2.cold_lock(config):assert synced==[inode]

@pytest.mark.parametrize('age_seconds',[60,30*86400])
def test_sent_cold_completion_blocks_replacement_until_it_is_settled(config,age_seconds):
    cloud=Cloud();data=r2.AGE_HEADER+b'synthetic cold ciphertext'*8;key=config['prefix']+'/cold/'+COLD+'.age'
    cold_dir=Path(config['state_directory'])/'cold';cold_dir.mkdir(mode=0o700);journal=cold_dir/(COLD+'.json')
    # The completion was sent and its upload is already gone, but no object is visible yet.
    write_json(journal,{'v':1,'key':key,'phase':'completing','upload_id':'consumed-upload',
                        'at':datetime.now(timezone.utc).timestamp()-age_seconds})
    # No age of the evidence settles it: only its object or an operator can.
    with pytest.raises(RuntimeError,match='completion_outcome_unknown'):cold(config,cloud,'put',data=data)
    assert journal.exists() and cloud.objects=={}
    # Its object appearing settles it: the key then answers `exists`, never replaced.
    cloud.objects[key]=r2.AGE_HEADER+b'the earlier completion landed'*4
    responses,_=cold(config,cloud,'put',data=data)
    assert responses==[{'v':1,'status':'exists','key':key,'bytes':len(cloud.objects[key])}] and not journal.exists()


def silent_stdin(monkeypatch,data=b'',*,close=True):
    read,write=os.pipe();os.write(write,data)
    if close:os.close(write);write=None
    monkeypatch.setattr(r2.sys,'stdin',SimpleNamespace(buffer=os.fdopen(read,'rb')))
    return write

@pytest.fixture
def published(config,tmp_path,monkeypatch):
    """The root verifier's helper publication: ownership virtualized; modes, links and FDs real."""
    original=os.fstat;upload_owned=set()
    ancestors={(p.stat().st_dev,p.stat().st_ino) for p in (tmp_path,*tmp_path.parents)}
    def root_view(fd):
        info=original(fd);values=list(info)
        values[4]=1000 if (info.st_dev,info.st_ino) in upload_owned else 0
        if (info.st_dev,info.st_ino) in ancestors:values[0]&=~0o022
        return os.stat_result(values)
    monkeypatch.setattr(os,'fstat',root_view)
    control=tmp_path/'published';control.mkdir();control.chmod(0o755)
    path=control/'capabilities.json'
    # The root verifier config: private, but its identity is visible to the upload UID.
    verifier_config=tmp_path/'verifier.json';write_json(verifier_config,{'synthetic':'verifier config'})
    def good():return {'v':1,'verifier_config':r2.config_identity(verifier_config),'helper_revision':'c'*40,'helper_schemas':[2,51]}
    def publish(raw=None,*,mode=0o644,root=True):
        path.write_bytes(r2.encoded(good()) if raw is None else raw)
        path.chmod(mode)
        if not root:upload_owned.add((path.stat().st_dev,path.stat().st_ino))
    config_path=tmp_path/'config.json'
    monkeypatch.setattr(r2,'CAPABILITIES_FILE',path)
    write_json(config_path,{**json.loads(config_path.read_text()),'verifier_config_file':str(verifier_config)})
    return SimpleNamespace(publish=publish,config=str(config_path),path=path,verifier_config=verifier_config,good=good)


# spec: ANYM-589
def test_capabilities_prints_only_the_installed_verifier_and_helper_ranges(published,monkeypatch,capsys):
    from messaging_service import backup_r2_verifier as verifier
    published.publish();silent_stdin(monkeypatch)
    monkeypatch.setattr(r2,'make_client',lambda conf:pytest.fail('capabilities reached the object store'))
    monkeypatch.setattr(r2,'receive',lambda *a,**kw:pytest.fail('capabilities read an archive'))
    assert r2.main(['--config',published.config,'capabilities'])==0
    first,last=verifier.SCHEMAS[0],verifier.SCHEMAS[-1]
    assert capsys.readouterr().out==('{"v":1,"verifier_schemas":[%d,%d],"helper_schemas":[2,51],"helper_revision":"%s"}\n'
                                     %(first,last,'c'*40))


# spec: ANYM-589
def test_capabilities_refuses_an_argument_or_input(published,monkeypatch,capsys):
    published.publish()
    for extra in (['extra'],['--id',BID],['--output','/tmp/x']):
        with pytest.raises(SystemExit) as refused:r2.main(['--config',published.config,'capabilities',*extra])
        assert refused.value.code==2
    capsys.readouterr()
    silent_stdin(monkeypatch,b'{"v":2,"id":"forged"}\n')
    assert r2.main(['--config',published.config,'capabilities'])==1
    assert json.loads(capsys.readouterr().out)=={'v':1,'status':'error','code':'capabilities_takes_no_input'}
    # An open, silent stdin (ssh without -n) is no input: it answers without reading it.
    write=silent_stdin(monkeypatch,close=False)
    try:assert r2.main(['--config',published.config,'capabilities'])==0
    finally:os.close(write)
    assert set(json.loads(capsys.readouterr().out))=={'v','verifier_schemas','helper_schemas','helper_revision'}


# spec: ANYM-589
@pytest.mark.parametrize('case',['unconfigured','missing','unreadable','upload_owned','group_writable','symlink','hardlink',
                                 'malformed','duplicate_field','extra_field','reversed_range','boolean_bound','short_revision',
                                 'oversized','unbound','config_edited','config_replaced','config_removed','config_symlink',
                                 'config_relative','other_config','config_ctime_only'])
def test_capabilities_without_a_trusted_helper_publication_is_unavailable(published,monkeypatch,capsys,case):
    good=published.good();bound=good['verifier_config']
    raws={'malformed':b'{"v":1,','duplicate_field':r2.encoded(good).replace(b'{"helper',b'{"v":1,"helper',1),
          'extra_field':r2.encoded({**good,'verifier_schemas':[2,99]}),'reversed_range':r2.encoded({**good,'helper_schemas':[51,2]}),
          'boolean_bound':r2.encoded({**good,'helper_schemas':[True,51]}),'short_revision':r2.encoded({**good,'helper_revision':'c'*7}),
          'oversized':r2.encoded({**good,'pad':'x'*8192}),'unbound':r2.encoded({k:v for k,v in good.items() if k!='verifier_config'}),
          'config_relative':r2.encoded({**good,'verifier_config':{**bound,'path':'verifier.json'}})}
    if case=='other_config':
        # Bound to a staged copy, not the installed service's config: refused although it matches its own file.
        staged=published.verifier_config.with_name('verifier.next.json');write_json(staged,{'synthetic':'staged'})
        raws[case]=r2.encoded({**good,'verifier_config':r2.config_identity(staged)})
    modes={'group_writable':0o664,'unreadable':0o640}
    published.publish(raws.get(case),mode=modes.get(case,0o644),root=case!='upload_owned')
    # A publication made from an older verifier config (a pin changed since) is stale.
    if case=='config_edited':write_json(published.verifier_config,{'synthetic':'verifier config','helper_revision':'d'*40})
    elif case=='config_ctime_only':
        # Same inode and size, mtime put back (`cp -p`, `touch -r`): only ctime moved.
        before=os.stat(published.verifier_config);raw=published.verifier_config.read_bytes()
        time.sleep(0.01);published.verifier_config.write_bytes(raw.replace(b'verifier config',b'verifier CONFIG'))
        os.utime(published.verifier_config,ns=(before.st_atime_ns,before.st_mtime_ns))
        after=os.stat(published.verifier_config)
        assert (after.st_ino,after.st_size,after.st_mtime_ns)==(before.st_ino,before.st_size,before.st_mtime_ns)
        assert after.st_ctime_ns!=before.st_ctime_ns
    elif case=='config_replaced':
        new=published.verifier_config.with_name('verifier.json.new');new.write_bytes(published.verifier_config.read_bytes())
        new.chmod(0o600);new.replace(published.verifier_config)
    elif case=='config_removed':published.verifier_config.unlink()
    elif case=='config_symlink':
        real=published.verifier_config.with_name('verifier-real.json');published.verifier_config.rename(real)
        published.verifier_config.symlink_to(real)
    elif case=='unconfigured':
        write_json(Path(published.config),{k:v for k,v in json.loads(Path(published.config).read_text()).items()
                                           if k!='verifier_config_file'})
    elif case=='missing':published.path.unlink()
    elif case=='symlink':
        target=published.path.with_name('real.json');published.path.rename(target);published.path.symlink_to(target)
    elif case=='hardlink':os.link(published.path,published.path.with_name('second.json'))
    silent_stdin(monkeypatch)
    assert r2.main(['--config',published.config,'capabilities'])==1
    assert json.loads(capsys.readouterr().out)=={'v':1,'status':'error','code':'capabilities_unavailable'}


# spec: ANYM-589
def test_capabilities_file_stays_outside_upload_state_and_leaves_receive_unchanged(config,tmp_path,monkeypatch):
    path=tmp_path/'config.json';value=json.loads(path.read_text());good={'verifier_config_file':str(tmp_path/'verifier.json')}
    # The publication path is fixed: the configuration cannot name one.
    assert r2.CAPABILITIES_FILE==Path('/var/lib/deedkit-backup-r2/published/capabilities.json')
    write_json(path,{**value,**good,'capabilities_file':str(tmp_path/'elsewhere.json')})
    with pytest.raises(ValueError,match='invalid_config'):r2.load_config(path)
    for bad in ('relative/verifier.json',str(Path(config['state_directory'])/'verifier.json'),'/etc/../tmp/v.json',7,
                '/'+str(tmp_path/'verifier.json')):
        write_json(path,{**value,'verifier_config_file':bad})
        with pytest.raises(ValueError,match='invalid_config_path'):r2.load_config(path)
    # A state directory holding the fixed publication path cannot enable the mode.
    with monkeypatch.context() as scoped:
        scoped.setattr(r2,'CAPABILITIES_FILE',Path(config['state_directory'])/'published/capabilities.json')
        write_json(path,{**value,**good})
        with pytest.raises(ValueError,match='invalid_config_path'):r2.load_config(path)
    write_json(path,{**value,**good})
    loaded=r2.load_config(path);cloud=Cloud()
    assert receive(loaded,cloud)['status']=='stored'
    responses,_=cold(loaded,cloud,'put',data=r2.AGE_HEADER+b'synthetic cold ciphertext'*8)
    assert responses[-1]['status']=='stored'


PART=5*1024**2

class Parallel(Cloud):
    """Cloud whose parts block until released: counts parts in flight and the thread of each call."""
    def __init__(self,*,hold=True,fail_part=None):
        super().__init__();self.lock=threading.Lock();self.inflight=0;self.peak=0;self.threads=set()
        self.release={};self.hold=hold;self.fail_part=fail_part;self.completed=None;self.done=[];self.at_stop=None
    def gate(self,number):
        with self.lock:return self.release.setdefault(number,threading.Event())
    def upload_part(self,**kw):
        number=kw['PartNumber'];self.threads.add(threading.current_thread().name)
        with self.lock:self.inflight+=1;self.peak=max(self.peak,self.inflight)
        try:
            if number==self.fail_part:
                eventually(lambda:self.inflight>=3);raise CloudError(500)
            if self.hold:assert self.gate(number).wait(10),'part never released'
            result=super().upload_part(**kw);self.done.append(number);return result
        finally:
            with self.lock:self.inflight-=1
            self.calls.append('upload_end')
    def complete_multipart_upload(self,**kw):
        self.completed=list(kw['MultipartUpload']['Parts'])
        return super().complete_multipart_upload(**kw)
    def free(self,numbers=range(1,64)):
        for number in numbers:self.gate(number).set()

class Counted(io.BytesIO):
    """The producer's stream: how far the receiver has read; at `pause_at` it waits for `until`, then stops or ends."""
    def __init__(self,raw,*,pause_at=None,until=None,stop=False):
        super().__init__(raw);self.pause_at=pause_at;self.until=until;self.stop=stop
    def read(self,*a):
        if self.pause_at is not None and self.tell()>=self.pause_at:
            eventually(self.until)
            if self.stop:r2.cancel(TimeoutError('receiver_timeout'))
            return b''
        return super().read(*a)
    def readline(self,*a):return self.read(*a) if self.pause_at is not None and self.tell()>=self.pause_at else super().readline(*a)

def workers_alive():return [t.name for t in threading.enumerate() if t.name.startswith('r2-part')]

def eventually(check,seconds=10):
    deadline=time.monotonic()+seconds
    while not check():
        assert time.monotonic()<deadline,'condition never held'
        time.sleep(0.01)

def parallel(config,n=3):return {**config,'upload_concurrency':n,'max_bytes':64*1024**2,'quota_bytes':128*1024**2}

@pytest.fixture
def free_on_stop(monkeypatch):
    """Release held parts only once the reader has begun stopping, so the join meets running uploads."""
    def arm(cloud):
        original=r2.Parts.stop
        def stop(self):
            if cloud.at_stop is None:cloud.at_stop=cloud.inflight
            cloud.free();return original(self)
        monkeypatch.setattr(r2.Parts,'stop',stop)
    return arm

@pytest.fixture
def alarm():
    """The receiver's own deadline handler (main) on SIGALRM; restored and cleared afterwards."""
    def timed_out(*_):r2.cancel(TimeoutError('receiver_timeout'))
    previous=signal.signal(signal.SIGALRM,timed_out)
    yield lambda:os.kill(os.getpid(),signal.SIGALRM)
    signal.signal(signal.SIGALRM,previous);r2.CANCELLED.clear()

def joined_before_abort(cloud):
    ends=[i for i,c in enumerate(cloud.calls) if c=='upload_end']
    return bool(ends) and cloud.calls.index('abort_multipart_upload')>max(ends) and not workers_alive()

def journal(config):return list(Path(config['state_directory']).glob('*.attempt.json'))

# spec: ANYM-628
@pytest.mark.parametrize('value',[0,5,'3',True,1.0,-1])
def test_upload_concurrency_config_accepts_only_whole_numbers_one_to_four(config,tmp_path,value):
    path=tmp_path/'bad.json';write_json(path,{**config,'upload_concurrency':value})
    with pytest.raises(ValueError,match='invalid_limits'):r2.load_config(path)

# spec: ANYM-628
def test_upload_concurrency_defaults_to_serial_and_accepts_production_value(config,tmp_path):
    assert config['upload_concurrency']==1
    path=tmp_path/'three.json';write_json(path,{**config,'upload_concurrency':3})
    assert r2.load_config(path)['upload_concurrency']==3

# spec: ANYM-628
def test_without_the_key_every_part_uploads_inline_on_the_reader_thread(config):
    cloud=Parallel(hold=False);data=r2.AGE_HEADER+b'x'*(11*1024**2)
    assert receive(config,cloud,data=data)['status']=='stored'
    assert cloud.peak==1 and cloud.threads=={threading.current_thread().name}
    assert cloud.completed==[{'PartNumber':n,'ETag':'part-'+str(n)} for n in (1,2,3)]

# spec: ANYM-628
def test_sdk_refuses_every_attempt_after_a_deadline_including_retries_after_backoff(config,monkeypatch):
    hooks=[]
    made=SimpleNamespace(meta=SimpleNamespace(events=SimpleNamespace(register_first=lambda *a:hooks.append(a))))
    monkeypatch.setitem(sys.modules,'boto3',SimpleNamespace(session=SimpleNamespace(Session=lambda **kw:SimpleNamespace(client=lambda *a,**k:made))))
    monkeypatch.setitem(sys.modules,'botocore.config',SimpleNamespace(Config=lambda **kw:kw))
    r2.make_client(config)
    assert ('request-created.s3',r2.cancelled) in hooks and ('needs-retry.s3',r2.cancelled) in hooks

# spec: ANYM-628
def test_reader_keeps_receiving_while_three_parts_upload_within_the_memory_bound(config):
    config=parallel(config);cloud=Parallel();data=r2.AGE_HEADER+b'p'*(9*PART)
    source=Counted(wire(data));outcome={}
    def run():
        try:outcome['result']=r2.receive(config,source,client=cloud)
        except BaseException as exc:outcome['error']=exc
    reader=threading.Thread(target=run);reader.start()
    try:
        eventually(lambda:cloud.inflight==3 and source.tell()>=5*PART)
        time.sleep(0.3)
        # Three parts upload, one waits, the reader's own part: (3 + 2) parts plus at most one frame.
        assert cloud.inflight==3 and cloud.peak==3
        assert 4*PART<source.tell()<=5*PART+2*1024**2
        assert len(workers_alive())==3
    finally:
        cloud.free(range(10,0,-1));reader.join(20)
    assert not reader.is_alive() and 'error' not in outcome
    assert outcome['result']['status']=='stored' and cloud.peak==3
    assert list(v for k,v in cloud.objects.items() if '/objects/' in k)==[data]

class Lean(Parallel):
    """Keeps only each part's digest, so traced memory is the receiver's own."""
    def upload_part(self,**kw):
        number=kw['PartNumber'];self.uploads[kw['UploadId']]['parts'][number]=None
        with self.lock:self.inflight+=1;self.peak=max(self.peak,self.inflight)
        try:
            assert self.gate(number).wait(10);self.digests[number]=sha(kw['Body']);self.sizes[number]=len(kw['Body'])
            return {'ETag':'part-'+str(number)}
        finally:
            with self.lock:self.inflight-=1
    def complete_multipart_upload(self,**kw):
        self.completed=list(kw['MultipartUpload']['Parts']);self.uploads.pop(kw['UploadId']);self.objects[kw['Key']]=b''
        return {}

# spec: ANYM-628
def test_part_memory_stays_within_concurrency_plus_two_parts_and_one_frame(config,monkeypatch):
    import tracemalloc
    config=parallel(config);data=r2.AGE_HEADER+b'm'*(8*PART+12345);raw=wire(data)
    cloud=Lean();cloud.digests={};cloud.sizes={}
    monkeypatch.setattr(r2,'verify_object',lambda *a,**k:None)
    source=Counted(raw);outcome={}
    def run():
        try:outcome['result']=r2.receive(config,source,client=cloud)
        except BaseException as exc:outcome['error']=exc
    tracemalloc.start()
    try:
        reader=threading.Thread(target=run);reader.start()
        try:
            # The worst moment: three uploading, one waiting, the reader's fifth part full.
            eventually(lambda:cloud.inflight==3 and source.tell()>=5*PART);time.sleep(0.3)
        finally:
            cloud.free();reader.join(20)
        peak=tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert 'error' not in outcome and outcome['result']['status']=='stored'
    assert peak<=5*PART+4*1024**2,peak/1024**2
    assert b''.join(data[(n-1)*PART:n*PART] for n in range(1,10))==data and len(cloud.sizes)==9
    assert all(cloud.digests[n]==sha(data[(n-1)*PART:n*PART]) for n in range(1,10))

# spec: ANYM-628
def test_parts_released_out_of_order_complete_sorted_like_the_serial_run(config):
    data=r2.AGE_HEADER+b'q'*(6*PART+123)
    serial=Parallel(hold=False);assert r2.receive(parallel(config,1),io.BytesIO(wire(data,bid=THIRD)),client=serial)['status']=='stored'
    # Four upload while the fifth waits; each release lets a later part in, so all seven finish out of order.
    cloud=Parallel();order=[4,2,3,7,5,6,1]
    def release():
        for number in order:
            eventually(lambda:number in cloud.release);cloud.gate(number).set()
            eventually(lambda:number in cloud.done)
    releaser=threading.Thread(target=release);releaser.start()
    try:result=r2.receive(parallel(config,4),io.BytesIO(wire(data)),client=cloud)
    finally:cloud.free();releaser.join(20)
    assert cloud.done==order and serial.done==sorted(order)
    assert result['status']=='stored' and cloud.completed==serial.completed
    assert [p['PartNumber'] for p in cloud.completed]==list(range(1,8))
    assert cloud.calls.index('complete_multipart_upload')>max(i for i,c in enumerate(cloud.calls) if c=='upload_end')
    assert list(v for k,v in cloud.objects.items() if '/objects/' in k)==[data] and not workers_alive()

# spec: ANYM-628
def test_failed_part_two_of_five_joins_every_upload_then_aborts_with_safe_code(config,free_on_stop):
    cloud=Parallel(fail_part=2);free_on_stop(cloud)
    with pytest.raises(RuntimeError,match='^storage_unavailable$'):
        r2.receive(parallel(config),io.BytesIO(wire(r2.AGE_HEADER+b'f'*(5*PART-100))),client=cloud)
    assert 'complete_multipart_upload' not in cloud.calls and cloud.calls.count('abort_multipart_upload')==1
    assert joined_before_abort(cloud) and cloud.at_stop>=1 and cloud.peak==3
    assert not cloud.uploads and not cloud.objects and not journal(config)

# spec: ANYM-628
def test_failed_abort_with_uploads_in_flight_keeps_journal_for_same_id_reconcile(config,free_on_stop):
    config=parallel(config);data=r2.AGE_HEADER+b'g'*(5*PART-100)
    cloud=Parallel(fail_part=2);cloud.fail['abort_multipart_upload']=TimeoutError('unknown abort');free_on_stop(cloud)
    with pytest.raises(RuntimeError,match='storage_unavailable'):r2.receive(config,io.BytesIO(wire(data)),client=cloud)
    assert joined_before_abort(cloud) and cloud.at_stop>=1 and 'complete_multipart_upload' not in cloud.calls
    assert journal(config)
    cloud.fail_part=None
    with pytest.raises(RuntimeError,match='incomplete_upload_aborted_retry'):r2.receive(config,io.BytesIO(wire(data)),client=cloud)
    assert not cloud.uploads and r2.receive(config,io.BytesIO(wire(data)),client=cloud)['status']=='stored'

# spec: ANYM-628
def test_stream_truncated_while_parts_upload_aborts_after_joining(config,free_on_stop):
    cloud=Parallel();free_on_stop(cloud);raw=wire(r2.AGE_HEADER+b't'*(5*PART))
    source=Counted(raw,pause_at=len(raw)//5*4,until=lambda:cloud.inflight==3)
    with pytest.raises(ValueError,match='truncated_backup'):r2.receive(parallel(config),source,client=cloud)
    assert cloud.at_stop==3 and 'complete_multipart_upload' not in cloud.calls
    assert joined_before_abort(cloud) and not cloud.uploads and not journal(config)

# spec: ANYM-628
def test_stop_during_uploads_ends_with_the_deadline_code_and_no_thread_alive(config,free_on_stop):
    cloud=Parallel();free_on_stop(cloud);raw=wire(r2.AGE_HEADER+b's'*(6*PART))
    source=Counted(raw,pause_at=4*PART,until=lambda:cloud.inflight==3,stop=True)
    try:
        with pytest.raises(TimeoutError,match='receiver_timeout'):r2.receive(parallel(config),source,client=cloud)
        assert cloud.at_stop==3 and 'complete_multipart_upload' not in cloud.calls
        assert not workers_alive() and cloud.inflight==0
        # The stop also refuses the abort's answer, as before: the journal waits for the same-ID reconcile.
        assert journal(config)
    finally:
        r2.CANCELLED.clear()

# spec: ANYM-628
def test_deadline_during_the_join_keeps_joining_and_leaves_the_journal_for_reconcile(config,alarm,monkeypatch):
    config=parallel(config);data=r2.AGE_HEADER+b'd'*(5*PART);raw=wire(data);cloud=Parallel()
    source=Counted(raw,pause_at=len(raw)//5*4,until=lambda:cloud.inflight==3)
    original=r2.Parts.stop
    def stop(self):
        # The deadline lands while the reader joins three held uploads; they end only after it ran.
        def later():eventually(lambda:bool(r2.CANCELLED));time.sleep(0.2);cloud.free()
        helper=threading.Thread(target=later);helper.start();alarm()
        try:return original(self)
        finally:helper.join(20)
    monkeypatch.setattr(r2.Parts,'stop',stop)
    with pytest.raises(TimeoutError,match='receiver_timeout'):r2.receive(config,source,client=cloud)
    # The receiver lock is released only here: every worker had already ended.
    assert not workers_alive() and cloud.inflight==0 and cloud.peak==3
    assert 'abort_multipart_upload' not in cloud.calls and 'complete_multipart_upload' not in cloud.calls
    assert journal(config)
    r2.CANCELLED.clear()
    with pytest.raises(RuntimeError,match='incomplete_upload_aborted_retry'):r2.receive(config,io.BytesIO(wire(data)),client=cloud)
    assert not cloud.uploads

# spec: ANYM-628
@pytest.mark.parametrize('moment',['thread_start','slot_wait'])
def test_deadline_during_bookkeeping_is_held_until_a_safe_point(config,alarm,free_on_stop,monkeypatch,moment):
    config=parallel(config);cloud=Parallel();free_on_stop(cloud);raw=wire(r2.AGE_HEADER+b'b'*(9*PART))
    if moment=='thread_start':
        class Started(threading.Thread):
            def start(self):super().start();alarm()
        monkeypatch.setattr(r2,'threading',SimpleNamespace(Thread=Started,Condition=threading.Condition))
    else:
        source=Counted(raw)
        def fire():eventually(lambda:cloud.inflight==3 and source.tell()>=5*PART);time.sleep(0.2);alarm()
        helper=threading.Thread(target=fire);helper.start()
    with pytest.raises(TimeoutError,match='receiver_timeout'):
        r2.receive(config,io.BytesIO(raw) if moment=='thread_start' else source,client=cloud)
    if moment=='slot_wait':helper.join(20)
    assert not workers_alive() and cloud.inflight==0 and 'complete_multipart_upload' not in cloud.calls
    assert journal(config) and cloud.at_stop is not None

# spec: ANYM-628
def test_a_part_request_outliving_the_stop_limit_ends_the_process_without_releasing_the_lock(config,monkeypatch):
    class Ended(BaseException):pass
    seen={}
    def ended():seen['alive']=workers_alive();raise Ended()
    monkeypatch.setattr(r2,'STOP_SECONDS',0.3);monkeypatch.setattr(r2,'stop_timed_out',ended)
    cloud=Parallel();raw=wire(r2.AGE_HEADER+b'h'*(5*PART))
    source=Counted(raw,pause_at=len(raw)//5*4,until=lambda:cloud.inflight==3)
    try:
        with pytest.raises(Ended):r2.receive(parallel(config),source,client=cloud)
        assert len(seen['alive'])==3 and 'abort_multipart_upload' not in cloud.calls and journal(config)
    finally:
        cloud.free();eventually(lambda:not workers_alive())

# spec: ANYM-628
def test_deadline_at_the_completing_write_aborts_like_serial_and_never_strands_the_journal(config,alarm,monkeypatch):
    config=parallel(config);data=r2.AGE_HEADER+b'c'*(4*PART);cloud=Parallel(hold=False);written=r2.state_write
    def state_write(directory,name,value):
        if value.get('phase')=='completing':alarm()
        return written(directory,name,value)
    monkeypatch.setattr(r2,'state_write',state_write)
    with pytest.raises(TimeoutError,match='receiver_timeout'):r2.receive(config,io.BytesIO(wire(data)),client=cloud)
    assert 'complete_multipart_upload' not in cloud.calls and not workers_alive()
    # The deadline refuses the abort's answer as in serial mode; the journal says `uploading`, so it reconciles.
    assert [json.loads(p.read_text())['phase'] for p in journal(config)]==['uploading']
    r2.CANCELLED.clear();monkeypatch.setattr(r2,'state_write',written)
    with pytest.raises(RuntimeError,match='incomplete_upload_aborted_retry'):r2.receive(config,io.BytesIO(wire(data)),client=cloud)
    assert r2.receive(config,io.BytesIO(wire(data)),client=cloud)['status']=='stored'

# spec: ANYM-628
def test_a_stalled_abort_after_the_join_still_gets_the_deadline(config,alarm,free_on_stop):
    config=parallel(config);cloud=Parallel(fail_part=2);free_on_stop(cloud);entered=threading.Event();held=threading.Event()
    def abort(**kw):
        entered.set();assert held.wait(10),'abort never released'
        return Cloud.abort_multipart_upload(cloud,**kw)
    cloud.abort_multipart_upload=abort
    def fire():entered.wait(10);alarm()
    helper=threading.Thread(target=fire);helper.start();started=time.monotonic()
    try:
        with pytest.raises(TimeoutError,match='receiver_timeout'):
            r2.receive(config,io.BytesIO(wire(r2.AGE_HEADER+b'a'*(5*PART-100))),client=cloud)
        assert time.monotonic()-started<5 and not held.is_set() and not workers_alive() and journal(config)
    finally:
        held.set();helper.join(20)
