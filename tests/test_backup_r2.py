"""Synthetic S3 state machine checks; no credentials, provider sessions or data."""
from datetime import datetime, timezone, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import sys
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
        def client(self,*a,**kw):calls.append(('client',a,kw));return 'client'
    monkeypatch.setitem(sys.modules,'boto3',SimpleNamespace(session=SimpleNamespace(Session=Session)))
    monkeypatch.setitem(sys.modules,'botocore.config',SimpleNamespace(Config=lambda **kw:kw))
    assert r2.make_client(config)=='client'
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
def test_cold_lock_fsyncs_state_directory_when_it_creates_cold(config,monkeypatch):
    state=Path(config['state_directory']);real=os.fsync;synced=[]
    def fsync(fd):synced.append(os.fstat(fd).st_ino);real(fd)
    monkeypatch.setattr(os,'fsync',fsync)
    # The new `cold/` entry is durable before its lock or any journal is used.
    with r2.cold_lock(config):assert synced==[state.stat().st_ino]
    # An existing `cold/` (FileExistsError) needs no parent sync.
    synced.clear()
    with r2.cold_lock(config):assert synced==[]

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
