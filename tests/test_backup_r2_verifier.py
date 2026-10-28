"""Synthetic server verifier state transitions; no live cloud/daemon/key use."""
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from messaging_service import backup_r2_receiver as r2


def module():
    from messaging_service import backup_r2_verifier
    return backup_r2_verifier


def test_server_verifier_disk_preflight_counts_plaintext_database_and_reserve(tmp_path,monkeypatch):
    v=module();calls=[]
    monkeypatch.setattr(v.shutil,'disk_usage',lambda path:SimpleNamespace(free=100))
    config={'directory':str(tmp_path),'max_plaintext_bytes':80,'postgres_headroom_bytes':20,'reserve_bytes':10}
    with pytest.raises(ValueError,match='restore_disk_capacity'):
        v.preflight_disk(config,{'wire':{'bytes':80}},tmp_path)


def test_server_verifier_proof_requires_exact_helper_facts_and_cloud_identity():
    v=module();receipt={'wire':{'id':'20261016T091703Z-fedcba9876543210','bytes':100,'sha256':'a'*64,'snapshot_sha256':'b'*64}}
    facts={'v':1,'status':'verified','schema_version':34,'manifest_sha256':'b'*64,'checks':{k:True for k in r2.CHECKS}}
    proof=v.make_proof(receipt,facts,'c'*32)
    assert proof['backup']==receipt['wire'] and proof['verification']['checks']==facts['checks']
    facts['checks']['collectors_quarantined']=False
    with pytest.raises(ValueError,match='invalid_helper_result'):v.make_proof(receipt,facts,'c'*32)


# spec: ANYM-511
@pytest.mark.parametrize('version,accepted',[(49,True),(50,True),(51,False)],ids=['n-1','current','next'])
def test_server_verifier_proof_accepts_current_schema_and_rejects_the_next(version,accepted):
    v=module();receipt={'wire':{'id':'20261016T091703Z-fedcba9876543210','bytes':100,'sha256':'a'*64,'snapshot_sha256':'b'*64}}
    facts={'v':1,'status':'verified','schema_version':version,'manifest_sha256':'b'*64,'checks':{k:True for k in r2.CHECKS}}
    if accepted:assert v.make_proof(receipt,facts,'c'*32)['verification']['schema_version']==version
    else:
        with pytest.raises(ValueError,match='invalid_helper_result'):v.make_proof(receipt,facts,'c'*32)


def test_server_verifier_cleanup_refuses_unowned_name_collision():
    v=module()
    class Engine:
        def __init__(self):self.deleted=[]
        def inspect_owned(self,kind,name,run):raise ValueError('unowned_restore_resource')
        def remove(self,kind,name):self.deleted.append((kind,name))
    engine=Engine()
    with pytest.raises(ValueError,match='unowned_restore_resource'):v.cleanup_resources(engine,'a'*32)
    assert not engine.deleted


def test_server_verifier_config_rejects_nonroot_before_client_or_docker(monkeypatch):
    v=module();monkeypatch.setattr(os,'geteuid',lambda:12345)
    with pytest.raises(ValueError,match='root_verifier_required'):v.load_config('/never/read')


def test_official_postgres_repo_digest_can_omit_configured_tag(monkeypatch):
    v=module();image='sha256:'+'a'*64;revision='b'*40
    engine=v.Docker()
    def command(*args):
        if args[0]=='info':return b'/var/lib/docker\n'
        if args[-1]==image:return r2.encoded([{'Id':image,'Config':{'Labels':{'org.opencontainers.image.revision':revision,'org.opencontainers.image.source':'https://github.com/GoSync-Inc/deedkit'}}}])
        return r2.encoded([{'RepoDigests':['postgres@sha256:'+v.POSTGRES_IMAGE.split('sha256:')[1]]}])
    monkeypatch.setattr(engine,'command',command)
    assert engine.preflight({'helper_image':image,'helper_revision':revision,'postgres_image':v.POSTGRES_IMAGE})=='/var/lib/docker'


@pytest.fixture
def operator(tmp_path,monkeypatch):
    # Virtualize root ownership/trust only; retain real FDs, links, modes, flock,
    # JSON hashing/fsync and conditional cloud operations. Native drill uses real UID.
    from test_backup_r2 import Cloud,write_json,receive,BID
    v=module();original_fstat=os.fstat;original_stat=Path.stat
    ancestors={(original_stat(p).st_dev,original_stat(p).st_ino) for p in (tmp_path,*tmp_path.parents)}
    def root_info(info):
        values=list(info);values[4]=0
        if (info.st_dev,info.st_ino) in ancestors:values[0]&=~0o022
        return os.stat_result(values)
    monkeypatch.setattr(os,'fstat',lambda fd:root_info(original_fstat(fd)))
    monkeypatch.setattr(Path,'stat',lambda p,*a,**kw:root_info(original_stat(p,*a,**kw)))
    monkeypatch.setattr(os,'geteuid',lambda:0)
    state=tmp_path/'state';state.mkdir(mode=0o700)
    control=tmp_path/'control';control.mkdir(mode=0o700)
    work=tmp_path/'verifier';work.mkdir(mode=0o700)
    cred=control/'credentials.json';write_json(cred,{'access_key_id':'a'*32,'secret_access_key':'b'*64})
    receiver={'v':1,'endpoint':'https://'+'1'*32+'.r2.cloudflarestorage.com','bucket':'synthetic','prefix':'test',
              'state_directory':str(state),'credentials_file':str(cred),'max_bytes':10*1024**2,'quota_bytes':20*1024**2,
              'max_objects':2,'part_bytes':5*1024**2,'timeout_seconds':900}
    receiver_path=control/'receiver.json';write_json(receiver_path,receiver)
    identity=control/'identity';identity.write_text('SYNTHETIC_NOT_A_KEY');identity.chmod(0o600)
    config={'v':1,'receiver_config_file':str(receiver_path),'directory':str(work),'identity_file':str(identity),
            'protected_file':str(control/'protected.json'),'helper_image':'sha256:'+'a'*64,'helper_revision':'b'*40,
            'postgres_image':v.POSTGRES_IMAGE,'max_plaintext_bytes':v.GIB,'postgres_headroom_bytes':2*v.GIB,
            'reserve_bytes':v.GIB,'timeout_seconds':900}
    cloud=Cloud();receive(receiver,cloud)
    class Engine:
        def __init__(self):self.runs=[];self.failure=None;self.resources={};self.deleted=[]
        def preflight(self,config):return str(work)
        def inspect_owned(self,kind,name,run):return self.resources.get((kind,name))
        def remove(self,kind,name):self.deleted.append((kind,name));self.resources.pop((kind,name))
        def restore(self,config,receiver,client,receipt,run,payload,secret_dir):
            self.runs.append(run)
            for kind,name in v.names(run).items():self.resources[(kind,name)]={'Labels':{v.LABEL:run}}
            r2.verify_object(receiver,client,receipt,io.BytesIO())
            if self.failure:raise self.failure
            return {'v':1,'status':'verified','schema_version':34,'manifest_sha256':receipt['wire']['snapshot_sha256'],
                    'checks':{k:True for k in r2.CHECKS}}
    engine=Engine()
    monkeypatch.setattr(v.shutil,'disk_usage',lambda path:SimpleNamespace(free=100*v.GIB))
    return v,config,receiver,cloud,engine,write_json


def test_server_run_protects_only_after_full_stream_restore_and_cleans_exact_resources(operator):
    v,config,receiver,cloud,engine,_=operator
    result=v.run(config,client=cloud,engine=engine)
    assert result['status']=='verified' and len(result['verification_id'])==32
    assert not engine.resources and len(engine.deleted)==4
    assert Path(config['protected_file']).exists()
    assert len(list(Path(config['directory']).glob('proof-*.json')))==1
    assert not (Path(config['directory'])/'active.json').exists()
    assert v.run(config,client=cloud,engine=engine)['status']=='idle'
    assert len(engine.runs)==1


def test_failed_restore_preserves_old_point_and_retries_exact_run(operator):
    v,config,receiver,cloud,engine,_=operator
    engine.failure=RuntimeError('restore_quarantine_failed')
    with pytest.raises(RuntimeError,match='restore_quarantine_failed'):v.run(config,client=cloud,engine=engine)
    active=json.loads((Path(config['directory'])/'active.json').read_text())
    assert not Path(config['protected_file']).exists() and not list(Path(config['directory']).glob('proof-*'))
    assert not engine.resources and not list(Path(config['directory']).glob('payload-*'))
    engine.failure=None
    result=v.run(config,client=cloud,engine=engine)
    assert result['verification_id']==active['run'] and engine.runs==[active['run']]*2


def test_failed_promotion_retries_identical_proof_without_second_restore(operator,monkeypatch):
    v,config,receiver,cloud,engine,_=operator
    original=r2.protect;calls=[]
    def lost_response(*args,**kwargs):
        calls.append(Path(args[2]).read_bytes());original(*args,**kwargs)
        raise TimeoutError('lost promotion result')
    monkeypatch.setattr(r2,'protect',lost_response)
    with pytest.raises(TimeoutError):v.run(config,client=cloud,engine=engine)
    proof=next(Path(config['directory']).glob('proof-*')).read_bytes()
    monkeypatch.setattr(r2,'protect',original)
    assert v.run(config,client=cloud,engine=engine)['status']=='verified'
    assert len(engine.runs)==1 and calls==[proof]


def test_deadline_handles_service_stop_with_cleanup_exception():
    import subprocess,sys
    code='from messaging_service.backup_r2_verifier import deadline; import signal\ntry:\n with deadline(30): signal.raise_signal(signal.SIGTERM)\nexcept InterruptedError: pass\nelse: raise RuntimeError(\"stop not observed\")'
    result=subprocess.run([sys.executable,'-c',code],capture_output=True)
    assert result.returncode==0


def test_failure_status_distinguishes_capacity_timeout_and_quarantine(operator):
    v,config,receiver,cloud,engine,_=operator
    for error,code in [(ValueError('restore_disk_capacity'),'restore_disk_capacity'),
                       (TimeoutError('synthetic'),'restore_deadline'),
                       (RuntimeError('restore_quarantine_failed'),'restore_quarantine_failed')]:
        engine.failure=error
        with pytest.raises(type(error)):v.run(config,client=cloud,engine=engine)
        assert json.loads((Path(config['directory'])/'status.json').read_text())['error']==code


def test_two_generation_rotation_and_lost_delete_result_retry_only_exact_cloud_keys(operator):
    from test_backup_r2 import receive,THIRD
    v,config,receiver,cloud,engine,_=operator
    v.run(config,client=cloud,engine=engine)
    old_marker=Path(config['protected_file']).read_bytes()
    receive(receiver,cloud,bid=THIRD)
    cloud.fail['delete_response']=True
    with pytest.raises(RuntimeError,match='storage_unavailable'):v.run(config,client=cloud,engine=engine)
    root=Path(config['directory']);active=json.loads((root/'active.json').read_text());proof=(root/('proof-'+active['run']+'.json')).read_bytes()
    assert Path(config['protected_file']).read_bytes()!=old_marker
    assert v.run(config,client=cloud,engine=engine)['backup_id']==THIRD
    assert len(engine.runs)==2 and (root/('proof-'+active['run']+'.json')).read_bytes()==proof
    assert len([key for key in cloud.objects if '/objects/' in key])==1


@pytest.mark.parametrize('failure',['mpu','orphan','unresolved','corrupt_body','unowned_resource'])
def test_unresolved_or_corrupt_candidate_never_protects_or_removes_old_data(operator,failure):
    v,config,receiver,cloud,engine,_=operator
    before=dict(cloud.objects)
    if failure=='mpu':cloud.uploads['unknown']={'key':receiver['prefix']+'/objects/unresolved'}
    elif failure=='orphan':cloud.objects[receiver['prefix']+'/objects/orphan']=b'unknown'
    elif failure=='unresolved':
        path=next(Path(receiver['state_directory']).glob('*.attempt.json'));value=json.loads(path.read_text());value['phase']='uploading';path.write_text(json.dumps(value))
    elif failure=='corrupt_body':
        key=next(k for k in cloud.objects if '/objects/' in k);cloud.objects[key]=b'x'*len(cloud.objects[key])
    else:
        def collision(*args):raise ValueError('unowned_restore_resource')
        engine.inspect_owned=collision
    with pytest.raises((ValueError,RuntimeError)):v.run(config,client=cloud,engine=engine)
    assert not Path(config['protected_file']).exists() and not list(Path(config['directory']).glob('proof-*'))
    assert not any(call=='delete_object' for call in cloud.calls)
    assert all(key in cloud.objects for key in before)


@pytest.mark.parametrize('attack',['key_public','key_link','key_hardlink','work_public','config_duplicate','unknown_option','state_key','work_key','unpinned_image'])
def test_root_config_rejects_unsafe_authority_paths_and_bounds(operator,attack):
    v,config,receiver,cloud,engine,write_json=operator
    path=Path(config['receiver_config_file']).parent/'verifier-config.json'
    if attack=='key_public':Path(config['identity_file']).chmod(0o644)
    elif attack=='key_link':
        key=Path(config['identity_file']);key.rename(key.with_suffix('.real'));key.symlink_to(key.with_suffix('.real'))
    elif attack=='key_hardlink':os.link(config['identity_file'],str(config['identity_file'])+'-link')
    elif attack=='work_public':Path(config['directory']).chmod(0o755)
    elif attack=='unknown_option':config['sender_maintenance']=True
    elif attack=='state_key':config['identity_file']=str(Path(receiver['state_directory'])/'key')
    elif attack=='work_key':config['identity_file']=str(Path(config['directory'])/'key')
    elif attack=='unpinned_image':config['helper_image']='deedkit:latest'
    write_json(path,config)
    if attack=='config_duplicate':path.write_text(path.read_text().replace('"v":1','"v":1,"v":1'))
    with pytest.raises((ValueError,OSError)):v.load_config(path)


def test_valid_config_root_ancestry_and_key_are_accepted_without_network(operator):
    v,config,receiver,cloud,engine,write_json=operator
    path=Path(config['receiver_config_file']).parent/'verifier-config.json';write_json(path,config)
    assert v.load_config(path)==config


def test_restore_deadline_expires_then_cleans_owned_resources_with_fresh_budget(operator,monkeypatch):
    v,config,receiver,cloud,engine,_=operator
    config['timeout_seconds']=0.01
    def timeout(*args):
        import time
        engine.resources={('helper',v.names(args[4])['helper']):{'owned':True}}
        time.sleep(0.1)
    engine.restore=timeout
    with pytest.raises(TimeoutError):v.run(config,client=cloud,engine=engine)
    assert not engine.resources and not Path(config['protected_file']).exists()
    assert json.loads((Path(config['directory'])/'status.json').read_text())['error']=='restore_deadline'


def test_postgres_readiness_waits_for_final_tcp_listener_not_temporary_init_server(tmp_path,monkeypatch):
    v=module();engine=v.Docker();calls=[]
    monkeypatch.setattr(engine,'command',lambda *args:calls.append(args) or b'')
    engine.setup({'postgres_image':v.POSTGRES_IMAGE,'helper_image':'sha256:'+'a'*64,'identity_file':'/private/key'},'a'*32,tmp_path,tmp_path)
    ready=next(call for call in calls if 'pg_isready' in call)
    assert '-h' in ready and '127.0.0.1' in ready
    helper=next(call for call in calls if 'messaging_service.backup_restore_worker' in call)
    assert '--read-only' in helper and '--cap-drop=ALL' in helper and '--security-opt=no-new-privileges' in helper
    assert '--publish' not in helper and '-p' not in helper
    assert all('docker.sock' not in arg for arg in helper)


def test_docker_failed_helper_reads_bounded_safe_failure_fact(tmp_path,monkeypatch):
    v=module();engine=v.Docker()
    monkeypatch.setattr(engine,'setup',lambda *args:None)
    monkeypatch.setattr(engine,'inspect_owned',lambda *args:{})
    monkeypatch.setattr(v,'validate_helper_layout',lambda *args:None)
    class Process:
        stdin=io.BytesIO()
        def wait(self,timeout):return 1
        def poll(self):return 1
    monkeypatch.setattr(v.subprocess,'Popen',lambda *a,**kw:Process())
    monkeypatch.setattr(r2,'verify_object',lambda *a:None)
    monkeypatch.setattr(v,'private_file',lambda *a:r2.encoded({'v':1,'status':'failed','error':'restore_quarantine_failed'}))
    with pytest.raises(RuntimeError,match='restore_quarantine_failed'):
        engine.restore({}, {},None,{},'a'*32,tmp_path,tmp_path)


def test_reserve_monitor_rejects_growth_during_restore(tmp_path,monkeypatch):
    v=module();engine=v.Docker();engine.disk_roots=[tmp_path];engine.reserve_bytes=100
    monkeypatch.setattr(v.shutil,'disk_usage',lambda path:SimpleNamespace(free=99))
    with pytest.raises(ValueError,match='restore_disk_capacity'):engine.check_reserve()


def test_pending_retry_cannot_change_protected_marker_path(operator):
    v,config,receiver,cloud,engine,_=operator
    engine.failure=RuntimeError('restore_quarantine_failed')
    with pytest.raises(RuntimeError):v.run(config,client=cloud,engine=engine)
    config['protected_file']=str(Path(config['protected_file']).with_name('different-protected.json'))
    engine.failure=None
    with pytest.raises(ValueError,match='verifier_control_changed_during_retry'):v.run(config,client=cloud,engine=engine)
    assert len(engine.runs)==1


def test_helper_explicit_tmpfs_overrides_inherited_pg_image_volume(tmp_path,monkeypatch):
    v=module();engine=v.Docker();calls=[]
    monkeypatch.setattr(engine,'command',lambda *args:calls.append(args) or b'')
    engine.setup({'postgres_image':v.POSTGRES_IMAGE,'helper_image':'sha256:'+'a'*64,'identity_file':'/private/key'},'a'*32,tmp_path,tmp_path)
    helper=next(call for call in calls if 'messaging_service.backup_restore_worker' in call)
    tmpfs=[helper[i+1] for i,value in enumerate(helper[:-1]) if value=='--tmpfs']
    assert '/var/lib/postgresql:ro,noexec,nosuid,nodev,size=1m,mode=0700' in tmpfs


def test_unexpected_image_volume_is_rejected_before_any_resource_creation(monkeypatch):
    v=module();engine=v.Docker();image='sha256:'+'a'*64;revision='b'*40
    helper={'Id':image,'Config':{'Labels':{'org.opencontainers.image.revision':revision,
             'org.opencontainers.image.source':'https://github.com/GoSync-Inc/deedkit'},'Volumes':{'/var/lib/postgresql':{},'/unexpected':{}}}}
    monkeypatch.setattr(engine,'inspect_image',lambda value:helper if value==image else {'RepoDigests':['postgres@'+v.POSTGRES_IMAGE.split('@')[1]]})
    monkeypatch.setattr(engine,'command',lambda *args:b'/var/lib/docker\n')
    with pytest.raises(ValueError,match='unexpected_restore_image_volume'):
        engine.preflight({'helper_image':image,'helper_revision':revision,'postgres_image':v.POSTGRES_IMAGE})


def test_exact_owned_container_cleanup_removes_anonymous_not_named_volumes(monkeypatch):
    v=module();engine=v.Docker();calls=[]
    monkeypatch.setattr(engine,'command',lambda *args:calls.append(args) or b'')
    engine.remove('helper','owned-helper')
    engine.remove('volume','owned-pg-data')
    assert ('container','rm','--volumes','owned-helper') in calls
    assert calls[-1]==('volume','rm','owned-pg-data')


def test_runtime_helper_layout_rejects_anonymous_volume_before_input(tmp_path):
    v=module();run='a'*32
    config={'identity_file':'/root/private-identity'}
    with pytest.raises(ValueError,match='unexpected_restore_helper_layout'):
        v.validate_helper_layout(config,run,tmp_path,tmp_path,{'Mounts':[{'Type':'volume','Destination':'/var/lib/postgresql','Name':'untracked-anonymous'}]})


def helper_layout(v,config,run,payload,secret_dir):
    return {'Mounts':[{'Type':'bind','Source':str(payload),'Destination':'/work','RW':True},
                     {'Type':'bind','Source':config['identity_file'],'Destination':'/run/restore/identity','RW':False},
                     {'Type':'bind','Source':str(secret_dir/'worker.json'),'Destination':'/run/restore/config.json','RW':False}],
            'HostConfig':{'Tmpfs':dict(v.HELPER_TMPFS),'ReadonlyRootfs':True,'PortBindings':{},
                          'Privileged':False,'NetworkMode':v.names(run)['network']}}


def test_runtime_helper_layout_accepts_only_explicit_binds_and_bounded_tmpfs(tmp_path):
    v=module();run='a'*32;config={'identity_file':'/root/key'}
    v.validate_helper_layout(config,run,tmp_path,tmp_path,helper_layout(v,config,run,tmp_path,tmp_path))


@pytest.mark.parametrize('attack',['unknown_mount','writable_key','wrong_source','rw_root','external_network','host_ports','unbounded_tmpfs'])
def test_runtime_layout_changes_block_helper_before_ciphertext_input(tmp_path,attack):
    v=module();run='a'*32;config={'identity_file':'/root/key'}
    value=helper_layout(v,config,run,tmp_path,tmp_path)
    if attack=='unknown_mount':value['Mounts'].append({'Type':'volume','Destination':'/unexpected','Name':'orphan'})
    elif attack=='writable_key':value['Mounts'][1]['RW']=True
    elif attack=='wrong_source':value['Mounts'][0]['Source']='/shared-production'
    elif attack=='rw_root':value['HostConfig']['ReadonlyRootfs']=False
    elif attack=='external_network':value['HostConfig']['NetworkMode']='bridge'
    elif attack=='host_ports':value['HostConfig']['PortBindings']={'5432/tcp':[{'HostPort':'5432'}]}
    elif attack=='unbounded_tmpfs':value['HostConfig']['Tmpfs'][v.PG_DATA_PATH]='rw'
    with pytest.raises(ValueError,match='unexpected_restore_helper_layout'):
        v.validate_helper_layout(config,run,tmp_path,tmp_path,value)


def test_cold_archive_state_and_objects_do_not_block_backup_verification(operator):
    v,config,receiver,cloud,engine,_=operator
    state=Path(receiver['state_directory'])
    cold_dir=state/'cold';cold_dir.mkdir(mode=0o700)
    for name in ('.cold.lock','c'*64+'.json','.'+'c'*64+'.json-'+'0'*32):  # lock, journal, journal temp
        (cold_dir/name).write_text('{"v":1}');(cold_dir/name).chmod(0o600)
    cloud.objects[receiver['prefix']+'/cold/'+'c'*64+'.age']=r2.AGE_HEADER+b'synthetic cold copy'
    assert v.run(config,client=cloud,engine=engine)['status']=='verified'
    assert receiver['prefix']+'/cold/'+'c'*64+'.age' in cloud.objects
    # Any other unknown local state still fails closed.
    (state/'unexpected.json').write_text('{}')
    fd=os.open(state,os.O_RDONLY)
    try:
        with pytest.raises(ValueError,match='unresolved_upload_required'):
            v.select_candidate(r2.load_config(config['receiver_config_file']),cloud,fd,None)
    finally:os.close(fd)


class StreamingError(Exception):
    """Stands in for botocore's ResponseStreamingError around a urllib3 reset."""


class FlakyBody:
    def __init__(self,data,fail_at,cause=None):self.data=io.BytesIO(data);self.left=fail_at;self.cause=cause
    def read(self,size):
        if self.left is not None and self.left<=0:
            try:raise self.cause or ConnectionResetError('https://signed.example/?X-Amz-Signature=SECRET')
            except BaseException as error:raise StreamingError('Connection broken: SECRET') from error
        if self.left is not None:size=min(size,self.left);self.left-=size
        return self.data.read(size)
    def close(self):pass


class FlakyCloud:
    """One object; every GET's body fails after the next offset in `failures`."""
    def __init__(self,data,failures):self.data=data;self.failures=list(failures);self.calls=[]
    def get_object(self,**kw):
        self.calls.append({k:kw[k] for k in ('Range','IfMatch') if k in kw})
        start=0
        if 'Range' in kw:
            start,end=(int(x) for x in kw['Range'].removeprefix('bytes=').split('-'));assert end==len(self.data)-1
        fail=self.failures.pop(0)-start if self.failures else None
        return {'ContentLength':len(self.data)-start,'ETag':'"etag-1"','Body':FlakyBody(self.data[start:],fail)}


def stream_receipt(data):
    return {'object_key':'test/objects/x','wire':{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}}


# spec: ANYM-285
@pytest.mark.parametrize('failures',[[1],[r2.MIB],[r2.MIB+7,2*r2.MIB+1],[5,6,7,8,9]],
                         ids=['header','chunk-boundary','two-resets','max-resumes'])
def test_stream_resumes_a_transient_read_failure_from_the_next_byte(monkeypatch,failures):
    data=r2.AGE_HEADER+os.urandom(3*r2.MIB);pauses=[]
    monkeypatch.setattr(r2,'stream_pause',pauses.append)
    cloud=FlakyCloud(data,failures);out=io.BytesIO()
    r2.verify_object({'bucket':'synthetic'},cloud,stream_receipt(data),out)
    assert out.getvalue()==data and len(pauses)==len(failures)
    assert cloud.calls==[{}]+[{'Range':'bytes=%d-%d'%(offset,len(data)-1),'IfMatch':'"etag-1"'} for offset in failures]


# spec: ANYM-285
def test_stream_gives_up_after_bounded_resumes_with_a_safe_code(monkeypatch):
    data=r2.AGE_HEADER+os.urandom(r2.MIB);pauses=[]
    monkeypatch.setattr(r2,'stream_pause',pauses.append)
    cloud=FlakyCloud(data,range(10,10+r2.STREAM_RESUMES+1))
    with pytest.raises(RuntimeError,match='^storage_unavailable$'):
        r2.verify_object({'bucket':'synthetic'},cloud,stream_receipt(data),io.BytesIO())
    assert len(pauses)==r2.STREAM_RESUMES and len(cloud.calls)==r2.STREAM_RESUMES+1
    delays=[];monkeypatch.undo()
    monkeypatch.setattr(r2.time,'sleep',delays.append)
    for attempt in range(1,r2.STREAM_RESUMES+1):r2.stream_pause(attempt)
    assert delays==sorted(delays) and 0<delays[0] and delays[-1]<=30


# spec: ANYM-285
@pytest.mark.parametrize('stop',[TimeoutError('restore_deadline'),InterruptedError('restore_stopped'),
                                 TimeoutError('receiver_timeout')],ids=['deadline','service-stop','receiver-alarm'])
def test_stream_deadline_or_stop_inside_an_sdk_read_is_never_retried(monkeypatch,stop):
    data=r2.AGE_HEADER+os.urandom(r2.MIB);pauses=[]
    monkeypatch.setattr(r2,'stream_pause',pauses.append)
    cloud=FlakyCloud(data,[100])
    cloud.get_object=lambda **kw:(cloud.calls.append(kw),{'ContentLength':len(data),'ETag':'"e"','Body':FlakyBody(data,100,stop)})[1]
    with pytest.raises(type(stop)) as raised:
        r2.verify_object({'bucket':'synthetic'},cloud,stream_receipt(data),io.BytesIO())
    assert raised.value is stop and not pauses and len(cloud.calls)==1


# spec: ANYM-285
def test_stream_deadline_while_closing_the_broken_body_is_never_swallowed(monkeypatch):
    data=r2.AGE_HEADER+os.urandom(r2.MIB);pauses=[]
    monkeypatch.setattr(r2,'stream_pause',pauses.append)
    class Closing(FlakyBody):
        def close(self):raise TimeoutError('restore_deadline')
    cloud=FlakyCloud(data,[])
    cloud.get_object=lambda **kw:(cloud.calls.append(kw),{'ContentLength':len(data),'ETag':'"e"','Body':Closing(data,100)})[1]
    with pytest.raises(TimeoutError,match='restore_deadline'):
        r2.verify_object({'bucket':'synthetic'},cloud,stream_receipt(data),io.BytesIO())
    assert not pauses and len(cloud.calls)==1


# spec: ANYM-285
def test_stream_never_retries_a_failed_write_to_the_restore_helper(monkeypatch):
    data=r2.AGE_HEADER+os.urandom(r2.MIB);pauses=[]
    monkeypatch.setattr(r2,'stream_pause',pauses.append)
    class Closed:
        def write(self,data):raise BrokenPipeError('helper exited')
    cloud=FlakyCloud(data,[])
    with pytest.raises(BrokenPipeError):r2.verify_object({'bucket':'synthetic'},cloud,stream_receipt(data),Closed())
    assert not pauses and len(cloud.calls)==1


# spec: ANYM-285
def test_stream_resume_refuses_a_range_of_the_wrong_length(monkeypatch):
    data=r2.AGE_HEADER+os.urandom(r2.MIB)
    monkeypatch.setattr(r2,'stream_pause',lambda attempt:None)
    cloud=FlakyCloud(data,[50]);original=cloud.get_object
    def short(**kw):
        response=original(**kw)
        if 'Range' in kw:response['ContentLength']-=1
        return response
    cloud.get_object=short
    with pytest.raises(ValueError,match='stored_backup_corrupt'):
        r2.verify_object({'bucket':'synthetic'},cloud,stream_receipt(data),io.BytesIO())


# spec: ANYM-285
def test_first_run_survives_a_transient_cloud_reset_mid_stream(operator,monkeypatch):
    v,config,receiver,cloud,engine,_=operator
    monkeypatch.setattr(r2,'stream_pause',lambda attempt:None)
    original=cloud.get_object;broken=[]
    def get_object(**kw):
        response=original(**kw)
        if '/objects/' in kw['Key'] and not broken:
            broken.append(kw);response['Body']=FlakyBody(response['Body'].read(),64)
        elif 'Range' in kw:
            start=int(kw['Range'].removeprefix('bytes=').split('-')[0]);rest=response['Body'].read()[start:]
            response={'ContentLength':len(rest),'Body':io.BytesIO(rest)}
        return response
    cloud.get_object=get_object
    result=v.run(config,client=cloud,engine=engine)
    assert broken and result['status']=='verified' and Path(config['protected_file']).exists()


# spec: ANYM-285
def test_failure_status_records_exception_types_and_frames_but_no_message(operator):
    v,config,receiver,cloud,engine,_=operator
    try:raise StreamingError('https://signed.example/?X-Amz-Signature=SECRET')
    except StreamingError:
        try:raise RuntimeError('storage_unavailable') from None
        except RuntimeError as error:engine.failure=error
    with pytest.raises(RuntimeError):v.run(config,client=cloud,engine=engine)
    directory=Path(config['directory']);raw=(directory/'status.json').read_text()
    status=json.loads(raw)
    assert status['error']=='storage_unavailable' and 'SECRET' not in raw and 'signed.example' not in raw
    chain=status['diagnostic']
    assert [item['type'] for item in chain]==['builtins.RuntimeError','test_backup_r2_verifier.StreamingError']
    assert all(len(frame)==3 and isinstance(frame[1],int) for item in chain for frame in item['frames'])
    assert ['messaging_service/backup_r2_verifier.py','run'] in [[f[0],f[2]] for f in chain[0]['frames']]
    assert json.loads((directory/'last-failure.json').read_text())==status
    engine.failure=None
    assert v.run(config,client=cloud,engine=engine)['status']=='verified'
    assert json.loads((directory/'last-failure.json').read_text())==status


# spec: ANYM-285
def test_service_stderr_names_the_safe_code_and_exception_types_only(monkeypatch,capsys):
    v=module()
    def fail(path):
        try:raise StreamingError('SECRET diagnostic')
        except StreamingError:raise RuntimeError('storage_unavailable') from None
    monkeypatch.setattr(v,'load_config',fail)
    assert v.main(['--config','/synthetic'])==1
    err=capsys.readouterr().err;line=json.loads(err)
    assert line['error']=='storage_unavailable' and 'SECRET' not in err
    assert [item['type'] for item in line['diagnostic']]==['builtins.RuntimeError','test_backup_r2_verifier.StreamingError']


class SwallowingCloud(FlakyCloud):
    """A ranged GET whose transport catches our deadline/stop and retries to success, as botocore's retry loop does."""
    def __init__(self,data,failures,signum):super().__init__(data,failures);self.signum=signum
    def get_object(self,**kw):
        if 'Range' in kw:
            import signal
            try:signal.raise_signal(self.signum)
            except Exception:pass
        return super().get_object(**kw)


# spec: ANYM-285
@pytest.mark.parametrize('signum,code',[('SIGALRM','restore_deadline'),('SIGTERM','restore_stopped')],ids=['deadline','service-stop'])
def test_stream_deadline_or_stop_swallowed_by_an_sdk_retry_still_stops_the_run(monkeypatch,signum,code):
    import signal
    v=module();data=r2.AGE_HEADER+os.urandom(r2.MIB);pauses=[]
    monkeypatch.setattr(r2,'stream_pause',pauses.append)
    cloud=SwallowingCloud(data,[100],getattr(signal,signum));out=io.BytesIO()
    with pytest.raises((TimeoutError,InterruptedError),match=code):
        with v.deadline(60):r2.verify_object({'bucket':'synthetic'},cloud,stream_receipt(data),out)
    assert len(cloud.calls)==2 and len(pauses)==1 and len(out.getvalue())==100 and not r2.CANCELLED


# spec: ANYM-285
def test_sdk_retry_hook_reraises_a_recorded_deadline_before_the_next_attempt(monkeypatch):
    import sys
    registered=[]
    class Events:
        def register_first(self,event,handler):registered.append((event,handler))
    client=SimpleNamespace(meta=SimpleNamespace(events=Events()))
    session=SimpleNamespace(client=lambda *a,**kw:client)
    monkeypatch.setitem(sys.modules,'boto3',SimpleNamespace(session=SimpleNamespace(Session=lambda **kw:session)))
    monkeypatch.setitem(sys.modules,'botocore',SimpleNamespace())
    monkeypatch.setitem(sys.modules,'botocore.config',SimpleNamespace(Config=lambda **kw:kw))
    monkeypatch.setattr(r2,'credentials',lambda config:{'access_key_id':'a','secret_access_key':'s'})
    monkeypatch.setattr(r2,'CANCELLED',[])
    assert r2.make_client({'endpoint':'https://synthetic.invalid'}) is client
    assert registered==[('needs-retry.s3',r2.cancelled)]
    assert r2.cancelled(caught_exception=OSError()) is None
    with pytest.raises(TimeoutError,match='restore_deadline'):r2.cancel(TimeoutError('restore_deadline'))
    with pytest.raises(TimeoutError,match='restore_deadline'):r2.cancelled(caught_exception=OSError())


# spec: ANYM-285
def test_receiver_timeout_never_leaks_into_a_later_healthy_stream(monkeypatch,capsys):
    import signal
    previous=signal.getsignal(signal.SIGALRM)
    monkeypatch.setattr(r2,'load_config',lambda path:{'timeout_seconds':60})
    monkeypatch.setattr(r2,'receive',lambda *a,**kw:signal.raise_signal(signal.SIGALRM))
    try:assert r2.main(['--config','/synthetic/private.json','receive'])==1
    finally:signal.signal(signal.SIGALRM,previous)
    assert json.loads(capsys.readouterr().out)['code']=='receiver_timeout' and not r2.CANCELLED
    data=r2.AGE_HEADER+os.urandom(r2.MIB);out=io.BytesIO()
    r2.verify_object({'bucket':'synthetic'},FlakyCloud(data,[]),stream_receipt(data),out)
    assert out.getvalue()==data


class PipeStdin:
    """The helper's stdin after the helper exited: every write breaks the pipe."""
    def __init__(self):self.closed=False;self.writes=0
    def write(self,data):self.writes+=1;raise BrokenPipeError('helper exited')
    def close(self):self.closed=True


class ExitedHelper:
    """Synthetic helper that stops reading mid-stream, optionally leaving failure.json or hanging."""
    def __init__(self,payload,fact,hangs):
        self.stdin=PipeStdin();self.hangs=hangs;self.terminated=False
        if fact is not None:
            path=payload/'failure.json';path.write_bytes(fact);path.chmod(0o600)
    def poll(self):return None if self.hangs and not self.terminated else 1
    def wait(self,timeout):
        import subprocess,time
        if self.hangs and not self.terminated:
            time.sleep(min(timeout,0.05));raise subprocess.TimeoutExpired('helper',timeout)
        return 1
    def terminate(self):self.terminated=True
    def kill(self):self.terminated=True


# spec: ANYM-603
@pytest.mark.parametrize('fact,hangs,code',[
    (r2.encoded({'v':1,'status':'failed','error':'restore_database_failed'}),False,'restore_database_failed'),
    (None,False,'isolated_restore_failed'),
    (r2.encoded({'v':1,'status':'failed','error':'storage_unavailable'}),False,'isolated_restore_failed'),
    (r2.encoded({'v':1,'status':'failed','error':[]}),False,'isolated_restore_failed'),
    (r2.encoded({'v':1,'status':'failed','error':{}}),False,'isolated_restore_failed'),
    (None,True,'restore_deadline')],ids=['named','missing','invalid','array-error','object-error','hang'])
def test_helper_exit_mid_stream_reports_its_failure_fact_not_a_generic_code(operator,monkeypatch,fact,hangs,code):
    v,config,receiver,cloud,engine,_=operator
    docker=v.Docker(0.3 if hangs else 60);helpers=[]
    monkeypatch.setattr(docker,'setup',lambda *a:None)
    monkeypatch.setattr(docker,'inspect_owned',lambda *a:{})
    monkeypatch.setattr(v,'validate_helper_layout',lambda *a:None)
    monkeypatch.setattr(r2,'verify_object',lambda receiver,client,receipt,out:out.write(b'synthetic-ciphertext'))
    def popen(*args,**kwargs):
        helpers.append(ExitedHelper(next(Path(config['directory']).glob('payload-*')),fact,hangs));return helpers[-1]
    monkeypatch.setattr(v.subprocess,'Popen',popen)
    engine.restore=docker.restore
    with pytest.raises((RuntimeError,TimeoutError)):v.run(config,client=cloud,engine=engine)
    directory=Path(config['directory'])
    for name in ('status.json','last-failure.json'):assert json.loads((directory/name).read_text())['error']==code
    assert len(helpers)==1 and helpers[0].stdin.writes==1 and helpers[0].stdin.closed
    assert helpers[0].terminated==hangs and not Path(config['protected_file']).exists()
