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
@pytest.mark.parametrize('version,accepted',[(59,True),(60,True),(61,False)],ids=['n-1','current','next'])
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
            'reserve_bytes':v.GIB,'timeout_seconds':900,'host_directory':str(tmp_path/'host')}
    # The receiver's instances file (ANYM-609): absent unless a test writes it.
    monkeypatch.setattr(v,'INSTANCES_FILE',control/'instances.json')
    cloud=Cloud();receive(receiver,cloud)
    class Engine:
        def __init__(self):self.runs=[];self.failure=None;self.resources={};self.deleted=[]
        def preflight(self,config):return str(work)
        def inspect_owned(self,kind,name,run):return self.resources.get((kind,name))
        def remove(self,kind,name):self.deleted.append((kind,name));self.resources.pop((kind,name))
        def restore(self,config,receiver,client,receipt,run,payload,secret_dir):
            self.runs.append(run)
            for kind,name in v.names(run,r2.profile(config)).items():self.resources[(kind,name)]={'Labels':{v.LABEL:run}}
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


# spec: ANYM-629
def test_a_previous_point_of_the_wrong_size_at_rotation_keeps_its_corruption_code_in_the_status(operator,monkeypatch):
    from test_backup_r2 import receive,THIRD,BID
    v,config,receiver,cloud,engine,_=operator
    v.run(config,client=cloud,engine=engine);receive(receiver,cloud,bid=THIRD);original=r2.rotate
    def rotate(*args,**kw):
        # The previous object changes size after candidate selection, right before rotation.
        key=next(k for k in cloud.objects if '/objects/' in k and BID in k);cloud.objects[key]+=b'x'
        return original(*args,**kw)
    monkeypatch.setattr(r2,'rotate',rotate)
    with pytest.raises(ValueError,match='protected_backup_corrupt'):v.run(config,client=cloud,engine=engine)
    root=Path(config['directory'])
    for name in ('status.json','last-failure.json'):assert json.loads((root/name).read_text())['error']=='protected_backup_corrupt'
    assert 'delete_object' not in cloud.calls


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
    assert registered==[('needs-retry.s3',r2.cancelled),('request-created.s3',r2.cancelled)]
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


# spec: ANYM-589
def test_server_verifier_proof_range_is_the_published_verifier_schemas():
    v=module();receipt={'wire':{'id':'20261016T091703Z-fedcba9876543210','bytes':100,'sha256':'a'*64,'snapshot_sha256':'b'*64}}
    for version,accepted in ((v.SCHEMAS[0],True),(v.SCHEMAS[-1],True),(v.SCHEMAS[0]-1,False),(v.SCHEMAS[-1]+1,False)):
        facts={'v':1,'status':'verified','schema_version':version,'manifest_sha256':'b'*64,'checks':{k:True for k in r2.CHECKS}}
        if accepted:assert v.make_proof(receipt,facts,'c'*32)['verification']['schema_version']==version
        else:
            with pytest.raises(ValueError,match='invalid_helper_result'):v.make_proof(receipt,facts,'c'*32)


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


HEAD='d'*40;NEW_IMAGE='sha256:'+'c'*64
SOURCE_LABEL='https://github.com/GoSync-Inc/deedkit'


def image_labels(revision,source=SOURCE_LABEL):
    return {'org.opencontainers.image.revision':revision,'org.opencontainers.image.source':source}


@pytest.fixture
def follower(operator,tmp_path,monkeypatch):
    # The receiver update follow (ANYM-597) with synthetic main, attestation and Docker
    # answers; the real verifier preflight, lock, config writes and capabilities reader.
    v,config,receiver,cloud,engine,write_json=operator
    from messaging_service import backup_r2_follow as f
    path=Path(config['receiver_config_file']).parent/'verifier.json';write_json(path,config)
    write_json(Path(config['receiver_config_file']),{**receiver,'verifier_config_file':str(path)})
    (tmp_path/'published').mkdir(mode=0o755);(tmp_path/'published').chmod(0o755)
    monkeypatch.setattr(r2,'CAPABILITIES_FILE',tmp_path/'published'/'capabilities.json')
    monkeypatch.setattr(f,'mount_points',lambda:set())
    images={config['helper_image']:{'Id':config['helper_image'],'Config':{'Labels':image_labels(config['helper_revision'])}},
            v.POSTGRES_IMAGE:{'RepoDigests':['postgres@'+v.POSTGRES_IMAGE.split('@')[1]]}}
    # What the registry serves for main's head; a pull stores it under the attested config digest.
    served={'Id':NEW_IMAGE,'Config':{'Labels':image_labels(HEAD)}}
    class Docker(f.Docker):
        def __init__(self):
            super().__init__(900);self.pulled=[];self.removed=[];self.in_use=set();self.daemon_down=False
            self.resources={}
        def command(self,*args):
            if self.daemon_down:raise RuntimeError('restore_docker_failed')
            if args[:2]==('image','inspect'):
                if args[2] not in images:raise RuntimeError('restore_docker_failed')
                return r2.encoded([images[args[2]]])
            if args[:2]==('image','ls'):return '\n'.join(images).encode()
            if args[:2]==('image','rm'):
                self.removed.append(args[2])
                if args[2] in self.in_use:raise RuntimeError('restore_docker_failed')
                images.pop(args[2],None);return b''
            if args[0]=='info':return str(tmp_path).encode()
            raise AssertionError(args)
        def inspect_owned(self,kind,name,run):return self.resources.get((kind,name))
        def remove(self,kind,name):self.resources.pop((kind,name))
        def pull(self,reference):self.pulled.append(reference);images[NEW_IMAGE]=served
    class Main:
        def __init__(self):self.head_value=HEAD;self.forward_value=True
        def head(self):return self.head_value
        def forward(self,current,head):return self.forward_value
    proofs={HEAD:{'reference':f.IMAGE+'@sha256:'+'e'*64,'digest':'sha256:'+'e'*64,'config_digest':NEW_IMAGE}}
    def attest(sha):
        if sha not in proofs:raise RuntimeError('helper_unattested')
        return proofs[sha]
    state=SimpleNamespace(f=f,v=v,path=path,config=config,receiver=receiver,docker=Docker(),main=Main(),
                          images=images,served=served,proofs=proofs,tested=[],results={},write_json=write_json,
                          directory=Path(config['directory']),cloud=cloud,profiles=[],calendar_failure=None)
    def self_test(engine,tested,run=None):
        state.profiles.append(r2.profile(tested))
        if r2.profile(tested)!='messaging':
            if state.calendar_failure:raise state.calendar_failure
            return [2,50],4
        state.tested.append(tested['helper_revision'])
        result=state.results.get(tested['helper_revision'],50)
        if isinstance(result,BaseException):raise result
        return [2,50],result
    state.run=lambda:f.follow(path,engine=state.docker,main=state.main,attest=attest,test=self_test)
    state.revert=lambda:f.revert(path,engine=state.docker,test=self_test)
    state.published=lambda:r2.published_helper(r2.load_config(config['receiver_config_file']))
    state.record=lambda name:json.loads((state.directory/name).read_text())
    return state


def follow_status(state):
    return state.record('follow-status.json')


# spec: ANYM-597
def test_follow_installs_an_attested_newer_helper_after_its_self_test_and_publishes_its_schemas(follower,monkeypatch,capsys):
    from test_backup_r2 import silent_stdin
    s=follower;old=(s.config['helper_image'],s.config['helper_revision'])
    result=s.run()
    assert result['status']=='switched' and result['revision']==HEAD
    installed=s.v.load_config(s.path)
    assert (installed['helper_image'],installed['helper_revision'])==(NEW_IMAGE,HEAD)
    assert {k:installed[k] for k in installed if k not in ('helper_image','helper_revision')}=={
        k:s.config[k] for k in s.config if k not in ('helper_image','helper_revision')}
    assert s.docker.pulled==[s.proofs[HEAD]['reference']] and s.tested==[old[1],HEAD]
    previous=s.record('follow-previous.json')
    assert (previous['helper_image'],previous['helper_revision'],previous['helper_schemas'])==(*old,[2,50])
    assert not (s.directory/'follow-switch.json').exists() and not (s.directory/'follow-candidate.json').exists()
    published=s.published()
    assert published['helper_revision']==HEAD and published['helper_schemas']==[2,50]
    silent_stdin(monkeypatch);assert r2.main(['--config',s.config['receiver_config_file'],'capabilities'])==0
    assert json.loads(capsys.readouterr().out)['helper_revision']==HEAD
    assert follow_status(s)['status']=='switched'
    # Main unchanged: nothing more to install or test.
    assert s.run()['status']=='current' and s.tested==[old[1],HEAD] and len(s.docker.pulled)==1


# spec: ANYM-597
@pytest.mark.parametrize('case,code',[('unattested','helper_unattested'),('other_source','unreviewed_restore_image'),
                                      ('other_revision','unreviewed_restore_image'),
                                      ('volume','unexpected_restore_image_volume'),('older','follow_not_forward'),
                                      ('wrong_digest','unreviewed_restore_image')])
def test_follow_refuses_an_unproven_or_older_helper_and_keeps_the_current_pair(follower,case,code):
    s=follower;before=s.path.read_bytes()
    if case=='unattested':s.proofs.clear()
    elif case=='other_source':s.served['Config']['Labels']=image_labels(HEAD,'https://github.com/attacker/deedkit')
    elif case=='other_revision':s.served['Config']['Labels']=image_labels('f'*40)
    elif case=='volume':s.served['Config']['Volumes']={'/data':{}}
    elif case=='older':s.main.forward_value=False
    elif case=='wrong_digest':s.served['Id']='sha256:'+'9'*64
    with pytest.raises(Exception,match=code):s.run()
    assert s.path.read_bytes()==before and s.tested==[s.config['helper_revision']]
    assert not (s.directory/'follow-previous.json').exists()
    status=follow_status(s);assert status['error']==code and status['revision']==s.config['helper_revision']
    assert status['status']==('waiting' if code=='helper_unattested' else 'refused')
    # The current, self-tested pair is still published; a refused pull leaves no image behind.
    assert s.published()['helper_revision']==s.config['helper_revision']
    assert NEW_IMAGE not in s.images and s.docker.removed==([NEW_IMAGE] if s.docker.pulled else [])
    assert not (s.directory/'follow-candidate.json').exists()


# spec: ANYM-597
@pytest.mark.parametrize('failure',['raises','other_schema'])
def test_follow_keeps_the_old_pair_when_the_new_helper_fails_its_self_test(follower,failure):
    s=follower;before=s.path.read_bytes()
    s.results[HEAD]=RuntimeError('isolated_restore_failed') if failure=='raises' else 49
    with pytest.raises(Exception,match='isolated_restore_failed' if failure=='raises' else 'helper_self_test_mismatch'):s.run()
    assert s.path.read_bytes()==before and s.tested==[s.config['helper_revision'],HEAD]
    assert not (s.directory/'follow-previous.json').exists() and s.docker.removed==[NEW_IMAGE]
    assert s.published()['helper_revision']==s.config['helper_revision'] and follow_status(s)['status']=='refused'


# spec: ANYM-597
def test_follow_refused_candidates_never_accumulate_and_a_pull_needs_its_disk_budget(follower,monkeypatch):
    s=follower;s.results[HEAD]=49
    for _ in range(3):
        with pytest.raises(ValueError,match='helper_self_test_mismatch'):s.run()
    assert len(s.docker.pulled)==3 and s.docker.removed==[NEW_IMAGE]*3 and NEW_IMAGE not in s.images
    monkeypatch.setattr(s.v.shutil,'disk_usage',lambda path:SimpleNamespace(free=s.config['reserve_bytes']+s.f.PULL_BYTES-1))
    with pytest.raises(ValueError,match='follow_disk_capacity'):s.run()
    assert len(s.docker.pulled)==3 and not (s.directory/'follow-candidate.json').exists()


# spec: ANYM-597
def test_follow_removes_the_image_of_a_candidate_interrupted_after_its_pull(follower):
    s=follower;s.results[HEAD]=KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):s.run()
    assert NEW_IMAGE in s.images and s.record('follow-candidate.json')['helper_image']==NEW_IMAGE
    s.main.head_value=s.config['helper_revision']
    assert s.run()['status']=='current'
    assert s.docker.removed==[NEW_IMAGE] and not (s.directory/'follow-candidate.json').exists()
    # Never the installed pair's image.
    assert s.config['helper_image'] in s.images


# spec: ANYM-597
def test_follow_switch_is_atomic_when_it_crashes_writing_the_verifier_config(follower,monkeypatch):
    s=follower;before=s.path.read_bytes();original=os.replace
    def crash(src,dst,**kw):
        if dst==s.path.name:raise OSError('synthetic crash')
        return original(src,dst,**kw)
    monkeypatch.setattr(os,'replace',crash)
    with pytest.raises(OSError):s.run()
    assert s.path.read_bytes()==before and s.v.load_config(s.path)['helper_revision']==s.config['helper_revision']
    assert [p.name for p in s.path.parent.iterdir() if p.name.startswith('.verifier.json')]==[]
    assert not (s.directory/'follow-previous.json').exists() and (s.directory/'follow-switch.json').exists()
    monkeypatch.setattr(os,'replace',original)
    assert s.run()['status']=='switched' and s.published()['helper_revision']==HEAD
    assert s.record('follow-previous.json')['helper_revision']==s.config['helper_revision']


# spec: ANYM-597
def test_follow_crash_after_the_switch_finishes_it_and_publishes_the_installed_pair(follower,monkeypatch):
    s=follower;original=os.replace;writes=[]
    def crash(src,dst,**kw):
        if dst=='follow-current.json':
            writes.append(dst)
            if len(writes)==2:raise OSError('synthetic crash')
        return original(src,dst,**kw)
    monkeypatch.setattr(os,'replace',crash)
    with pytest.raises(OSError):s.run()
    assert s.v.load_config(s.path)['helper_revision']==HEAD
    with pytest.raises(ValueError,match='capabilities_unavailable'):s.published()  # the old publication is stale
    monkeypatch.setattr(os,'replace',original)
    assert s.run()['status']=='current' and s.published()['helper_revision']==HEAD
    assert s.record('follow-previous.json')['helper_revision']==s.config['helper_revision']
    assert s.record('follow-current.json')['helper_revision']==HEAD and not (s.directory/'follow-switch.json').exists()


# spec: ANYM-597
def test_follow_revert_crash_before_the_config_replace_keeps_both_pairs(follower,monkeypatch):
    s=follower;s.run();original=os.replace
    def crash(src,dst,**kw):
        if dst==s.path.name:raise OSError('synthetic crash')
        return original(src,dst,**kw)
    monkeypatch.setattr(os,'replace',crash)
    with pytest.raises(OSError):s.revert()
    assert s.v.load_config(s.path)['helper_revision']==HEAD
    assert s.record('follow-previous.json')['helper_revision']==s.config['helper_revision']
    monkeypatch.setattr(os,'replace',original)
    assert s.revert()['status']=='reverted' and s.v.load_config(s.path)['helper_revision']==s.config['helper_revision']
    assert s.record('follow-previous.json')['helper_revision']==HEAD


# spec: ANYM-597
def test_follow_never_runs_during_a_verification_or_its_pending_retry(follower):
    s=follower;before=s.path.read_bytes()
    with s.v.verifier_lock(s.config['directory']):
        with pytest.raises(RuntimeError,match='verifier_busy'):s.run()
    active=s.directory/'active.json';s.write_json(active,{'v':1,'phase':'restoring'})
    with pytest.raises(RuntimeError,match='verification_pending'):s.run()
    assert s.path.read_bytes()==before and not s.tested and not s.docker.pulled
    with pytest.raises(RuntimeError,match='verification_pending'):s.revert()


# spec: ANYM-597
def test_verifier_started_before_a_switch_journals_the_pair_installed_under_its_lock(follower):
    s=follower;stale=s.v.load_config(s.path)
    s.run()  # the follow switched while the verifier waited for the lock with the old pair loaded
    seen=[]
    class Engine:
        def preflight(self,config):seen.append((config['helper_image'],config['helper_revision']));raise RuntimeError('restore_docker_failed')
        def inspect_owned(self,kind,name,run):return None
    with pytest.raises(RuntimeError,match='restore_docker_failed'):
        s.v.run(stale,client=s.cloud,engine=Engine(),path=s.path)
    journal=json.loads((s.directory/'active.json').read_text())
    assert (journal['helper_image'],journal['helper_revision'])==(NEW_IMAGE,HEAD) and seen==[(NEW_IMAGE,HEAD)]


# spec: ANYM-597
def test_follow_revert_switches_back_to_the_kept_pair_and_republishes_it(follower):
    s=follower;old=(s.config['helper_image'],s.config['helper_revision'])
    s.run()
    result=s.revert()
    installed=s.v.load_config(s.path)
    assert result['status']=='reverted' and (installed['helper_image'],installed['helper_revision'])==old
    assert s.published()['helper_revision']==old[1]
    kept=s.record('follow-previous.json')
    assert (kept['helper_image'],kept['helper_revision'])==(NEW_IMAGE,HEAD)


# spec: ANYM-597
@pytest.mark.parametrize('case,code',[('foreign_file','publication_directory_foreign'),
                                      ('foreign_publication','publication_foreign'),
                                      ('malformed_publication','publication_foreign'),
                                      ('reversed_schemas','publication_foreign'),
                                      ('mount_point','publication_mount_point'),
                                      ('private_mount_point','publication_mount_point'),
                                      ('private_directory','publication_directory_private'),
                                      ('writable_directory','untrusted_operator_ancestor')])
def test_follow_publishes_only_its_own_file_in_a_root_directory_that_is_no_mount(follower,monkeypatch,case,code):
    s=follower;s.main.head_value=s.config['helper_revision'];published=r2.CAPABILITIES_FILE.parent
    malformed=json.dumps({'v':1,'verifier_config':{'path':1},'helper_revision':'x','helper_schemas':'all'})
    if case=='foreign_file':(published/'notes.txt').write_text('operator')
    elif case=='foreign_publication':r2.CAPABILITIES_FILE.write_text('{"v":1}');r2.CAPABILITIES_FILE.chmod(0o644)
    elif case=='malformed_publication':r2.CAPABILITIES_FILE.write_text(malformed);r2.CAPABILITIES_FILE.chmod(0o644)
    elif case=='reversed_schemas':
        malformed=json.dumps({'v':1,'verifier_config':{'path':'/x','dev':1,'ino':1,'size':1,'mtime_ns':1,'ctime_ns':1},
                              'helper_revision':'a'*40,'helper_schemas':[50,2]})
        r2.CAPABILITIES_FILE.write_text(malformed);r2.CAPABILITIES_FILE.chmod(0o644)
    elif case=='mount_point':monkeypatch.setattr(s.f,'mount_points',lambda:{str(published)})
    elif case=='private_mount_point':published.chmod(0o700);monkeypatch.setattr(s.f,'mount_points',lambda:{str(published)})
    elif case=='private_directory':published.chmod(0o700)
    elif case=='writable_directory':published.chmod(0o777)
    mode=published.stat().st_mode&0o777
    with pytest.raises(ValueError,match=code):s.run()
    assert follow_status(s)['error']==code and published.stat().st_mode&0o777==mode
    if case=='foreign_file':assert (published/'notes.txt').read_text()=='operator'
    if case=='foreign_publication':assert r2.CAPABILITIES_FILE.read_text()=='{"v":1}'
    if case in ('malformed_publication','reversed_schemas'):assert r2.CAPABILITIES_FILE.read_text()==malformed


# spec: ANYM-597
def test_follow_creates_a_searchable_publication_directory_under_a_private_umask(follower):
    s=follower;s.main.head_value=s.config['helper_revision'];published=r2.CAPABILITIES_FILE.parent
    published.rmdir();previous=os.umask(0o077)
    try:assert s.run()['status']=='current'
    finally:os.umask(previous)
    assert published.stat().st_mode&0o777==0o755 and r2.CAPABILITIES_FILE.stat().st_mode&0o777==0o644
    assert s.published()['helper_revision']==s.config['helper_revision']


# spec: ANYM-597
def test_follow_reads_main_forward_only_from_a_treeless_mirror(tmp_path):
    from messaging_service import backup_r2_follow as f
    import subprocess
    source=tmp_path/'source';work=tmp_path/'work';work.mkdir(mode=0o700)
    def git(*a):
        return subprocess.run(['git','-C',str(source),'-c','user.name=t','-c','user.email=t@invalid',*a],check=True,
                              capture_output=True).stdout.decode().strip()
    subprocess.run(['git','init','--quiet','-b','main',str(source)],check=True)
    git('commit','--quiet','--allow-empty','-m','one');first=git('rev-parse','HEAD')
    git('commit','--quiet','--allow-empty','-m','two');second=git('rev-parse','HEAD')
    main=f.Main(work,source='file://'+str(source))
    assert main.head()==second
    assert main.forward(first,second) is True
    assert main.forward(second,first) is False and main.forward('0'*40,second) is False
    assert (work/'follow-main.git').is_dir() and main.head()==second


# spec: ANYM-597
@pytest.mark.parametrize('case,code',[('absent','helper_unattested'),('unreached','attestation_unreachable'),
                                      ('refused','helper_attestation_refused'),('other_reference','helper_attestation_refused'),
                                      ('writable_binary','attest_verifier_unavailable'),('other_binary','attest_verifier_unavailable')])
def test_follow_attestation_runs_the_pinned_verifier_without_credentials(tmp_path,monkeypatch,case,code):
    from messaging_service import backup_r2_follow as f
    import hashlib as h
    import subprocess
    binary=tmp_path/'attest-verify';binary.write_bytes(b'synthetic verifier');binary.chmod(0o755)
    monkeypatch.setattr(f,'ATTEST_VERIFIER',binary);monkeypatch.setattr(f,'ATTEST_SHA256',h.sha256(b'synthetic verifier').hexdigest())
    if case=='writable_binary':binary.chmod(0o777)
    if case=='other_binary':monkeypatch.setattr(f,'ATTEST_SHA256','0'*64)
    digest='sha256:'+'e'*64;calls=[]
    answers={'absent':(1,b'',b'attest-verify: refused: manifest '+HEAD.encode()+b' not found\n'),
             'unreached':(1,b'',b'attest-verify: refused: registry request failed\n'),
             'refused':(1,b'',b'attest-verify: refused: certificate identity mismatch\n'),
             'other_reference':(0,json.dumps({'sha':HEAD,'digest':digest,'config_digest':NEW_IMAGE,
                                              'reference':'ghcr.io/attacker/deedkit-backup@'+digest}).encode(),b''),
             'ok':(0,json.dumps({'sha':HEAD,'digest':digest,'config_digest':NEW_IMAGE,'reference':f.IMAGE+'@'+digest}).encode(),b'')}
    answer=[case]
    def run(args,**kw):
        calls.append((args,kw.get('env')))
        code_,out,err=answers.get(answer[0],answers['refused'])
        return subprocess.CompletedProcess(args,code_,out,err)
    monkeypatch.setattr(f.subprocess,'run',run)
    with pytest.raises(Exception,match=code):f.attested(HEAD)
    if case not in ('writable_binary','other_binary'):
        assert calls==[([str(binary),'--image',f.IMAGE,'--sha',HEAD],{'PATH':'/usr/bin:/bin'})]
        answer[0]='ok'
        assert f.attested(HEAD)=={'reference':f.IMAGE+'@'+digest,'digest':digest,'config_digest':NEW_IMAGE}


# spec: ANYM-597
def test_follow_pins_the_reviewed_attestation_verifier_build():
    from messaging_service import backup_r2_follow as f
    recorded=(Path(__file__).parents[1]/'tools/attest-verify/attest-verify.linux-amd64.sha256').read_text().split()
    assert recorded==[f.ATTEST_SHA256,'attest-verify'] and str(f.ATTEST_VERIFIER)=='/usr/local/libexec/deedkit/attest-verify'


# spec: ANYM-597
@pytest.mark.parametrize('code,exit_code',[('helper_unattested',0),('verifier_busy',0),('main_unreachable',0),
                                           ('unreviewed_restore_image',1),('follow_not_forward',1)])
def test_follow_service_exit_is_clean_only_while_it_waits(monkeypatch,capsys,code,exit_code):
    from messaging_service import backup_r2_follow as f
    def fail(path,**kw):raise RuntimeError(code)
    monkeypatch.setattr(f,'follow',fail)
    assert f.main(['--config','/etc/deedkit-backup-r2/verifier.json','follow'])==exit_code
    assert json.loads(capsys.readouterr().err)['error']==code


class SelfTestEngine:
    """Synthetic Docker for the follow self-test: exact named resources, a fixture writer, a fake restore."""
    def __init__(self,v,f,config,hang_probe=False):
        self.v=v;self.f=f;self.config=config;self.hang_probe=hang_probe;self.resources={};self.restored=[]
    def inspect_owned(self,kind,name,run):return self.resources.get((kind,name))
    def remove(self,kind,name):self.resources.pop((kind,name),None)
    def setup(self,tested,run,payload,secret_dir):
        for kind,name in self.v.names(run).items():self.resources[(kind,name)]={}
    def command(self,*args):
        import subprocess
        if args[:2]==('container','create'):
            self.resources[('helper',args[3])]={}
            mounts=[a for a in args if a.startswith('type=bind,src=')]
            if mounts:
                fixture=Path(mounts[0].split('src=')[1].split(',')[0])
                (fixture/'ciphertext').write_bytes(r2.AGE_HEADER+b'synthetic');(fixture/'manifest-sha256').write_text('b'*64)
            return b''
        if args[:3]==('container','start','-a') and args[3].endswith('-probe'):
            if self.hang_probe:raise subprocess.TimeoutExpired(['docker'],120)
            return b'[2,50]\n'
        if args[:2]==('container','inspect'):return r2.encoded([{'State':{'ExitCode':0}}])
        return b''
    def restore(self,tested,receiver,client,receipt,run,payload,secret_dir):
        assert tested['identity_file']!=self.config['identity_file'] and Path(tested['identity_file']).is_file()
        assert tested['identity_file'].startswith(self.config['directory']+'/'+self.f.SELF_TEST)
        assert receipt['wire']['bytes']==len(client.get_object()['Body'].read())
        self.restored.append(run)
        for kind,name in self.v.names(run).items():self.resources[(kind,name)]={}
        return {'v':1,'status':'verified','schema_version':50,'manifest_sha256':'b'*64,'checks':{k:True for k in r2.CHECKS}}


@pytest.fixture
def self_test_setup(tmp_path,monkeypatch):
    from messaging_service import backup_r2_follow as f
    v=module();original=Path.lstat
    def root_owned(p):
        values=list(original(p));values[4]=0;return os.stat_result(values)
    monkeypatch.setattr(Path,'lstat',root_owned)
    directory=tmp_path/'verifier';directory.mkdir(mode=0o700)
    config={'directory':str(directory),'helper_image':NEW_IMAGE,'helper_revision':HEAD,'postgres_image':v.POSTGRES_IMAGE,
            'identity_file':str(tmp_path/'receiver-identity-never-mounted')}
    return v,f,directory,config


# spec: ANYM-597
def test_follow_self_test_uses_a_throwaway_identity_and_removes_its_workspace(self_test_setup):
    v,f,directory,config=self_test_setup
    stale='a'*32;(directory/(f.SELF_TEST+stale)/'fixture').mkdir(parents=True)
    engine=SelfTestEngine(v,f,config)
    leftover=f.self_test_containers(stale)[0][0];engine.resources[('helper',leftover)]={}
    assert f.self_test(engine,config)==([2,50],50)
    assert list(directory.iterdir())==[] and not engine.resources and len(engine.restored)==1


# spec: ANYM-597
def test_follow_self_test_removes_a_hung_schema_probe_by_its_exact_name(self_test_setup):
    v,f,directory,config=self_test_setup
    engine=SelfTestEngine(v,f,config,hang_probe=True)
    import subprocess
    with pytest.raises(subprocess.TimeoutExpired):f.self_test(engine,config)
    assert not engine.resources and list(directory.iterdir())==[] and not engine.restored


# spec: ANYM-597
def test_follow_service_stop_during_git_is_never_read_as_main_unreachable(tmp_path,monkeypatch):
    from messaging_service import backup_r2_follow as f
    def stopped(*a,**kw):r2.cancel(InterruptedError('restore_stopped'))
    monkeypatch.setattr(f.subprocess,'run',stopped)
    try:
        with pytest.raises(InterruptedError):f.Main(tmp_path).head()
        assert f.failure_code(InterruptedError('restore_stopped'))=='restore_stopped'
    finally:r2.CANCELLED.clear()


# spec: ANYM-597
def test_follow_keeps_an_unremovable_candidate_record_and_starts_no_new_candidate(follower):
    s=follower;s.results[HEAD]=KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):s.run()
    s.docker.in_use.add(NEW_IMAGE)  # a container outside the follow still uses it
    other='e'*40;s.main.head_value=other
    s.proofs[other]={'reference':s.f.IMAGE+'@sha256:'+'f'*64,'digest':'sha256:'+'f'*64,'config_digest':'sha256:'+'b'*64}
    with pytest.raises(ValueError,match='follow_candidate_unresolved'):s.run()
    assert s.record('follow-candidate.json')['helper_image']==NEW_IMAGE and len(s.docker.pulled)==1
    assert s.published()['helper_revision']==s.config['helper_revision']
    s.docker.in_use.clear();s.main.head_value=s.config['helper_revision']
    assert s.run()['status']=='current'
    assert NEW_IMAGE not in s.images and not (s.directory/'follow-candidate.json').exists()


# spec: ANYM-597
def test_follow_never_reads_a_docker_daemon_failure_as_an_absent_image(follower):
    s=follower;s.run();s.main.head_value='e'*40
    s.proofs['e'*40]={'reference':s.f.IMAGE+'@sha256:'+'f'*64,'digest':'sha256:'+'f'*64,'config_digest':'sha256:'+'b'*64}
    s.docker.daemon_down=True
    with pytest.raises(RuntimeError,match='restore_docker_failed'):s.run()
    assert not (s.directory/'follow-candidate.json').exists() and len(s.docker.pulled)==1
    assert NEW_IMAGE in s.images


# spec: ANYM-597
def test_follow_sweeps_an_interrupted_self_test_before_anything_else(follower):
    s=follower;s.run()  # the installed pair is proven: no self-test runs below
    run='a'*32;(s.directory/(s.f.SELF_TEST+run)).mkdir(mode=0o700)
    probe=s.f.self_test_containers(run)[0][0];s.docker.resources[('helper',probe)]={}
    s.proofs.clear();s.main.head_value='e'*40
    with pytest.raises(RuntimeError,match='helper_unattested'):s.run()
    assert not s.docker.resources and not (s.directory/(s.f.SELF_TEST+run)).exists() and s.tested==[s.config['helper_revision'],HEAD]
    (s.directory/(s.f.SELF_TEST+run)).mkdir(mode=0o700);s.docker.resources[('helper',probe)]={}
    s.revert()
    assert not s.docker.resources and not (s.directory/(s.f.SELF_TEST+run)).exists()


# spec: ANYM-597
def test_follow_sweeps_an_interrupted_self_test_while_a_verification_retry_waits(follower):
    s=follower;s.run()
    run='a'*32;probe=s.f.self_test_containers(run)[0][0]
    active=s.directory/'active.json';s.write_json(active,{'v':1,'phase':'restoring'})
    journal=active.read_bytes();pins=s.path.read_bytes()
    for entry in (s.run,s.revert):
        (s.directory/(s.f.SELF_TEST+run)).mkdir(mode=0o700);s.docker.resources[('helper',probe)]={}
        with pytest.raises(RuntimeError,match='verification_pending'):entry()
        assert not s.docker.resources and not (s.directory/(s.f.SELF_TEST+run)).exists()
        assert active.read_bytes()==journal and s.path.read_bytes()==pins


def calendar_facts(receipt,**changes):
    return {'v':1,'status':'verified','profile':'calendar','schema_version':5,'manifest_sha256':receipt['wire']['snapshot_sha256'],
            'checks':{k:True for k in r2.CALENDAR_CHECKS},
            'vault_warnings':[{'kind':'disabled_connection_without_token','name':'old'}],**changes}


@pytest.fixture
def calendar_operator(operator):
    # The calendar instance: its own receiver config with `profile: calendar` and a helper giving calendar facts.
    v,config,receiver,cloud,engine,write_json=operator
    receiver={**receiver,'profile':'calendar'};write_json(Path(config['receiver_config_file']),receiver)
    config={**config,'profile':'calendar'};restore=engine.restore;engine.workers=[]
    def calendar_restore(config,receiver,client,receipt,run,payload,secret_dir):
        engine.workers.append(json.loads((secret_dir/'worker.json').read_text()))
        facts=restore(config,receiver,client,receipt,run,payload,secret_dir)
        return calendar_facts(receipt) if engine.calendar else facts
    engine.restore=calendar_restore;engine.calendar=True
    return v,config,receiver,cloud,engine,write_json


# spec: ANYM-606
def test_calendar_verifier_config_needs_a_calendar_receiver_and_a_messaging_config_loads_unchanged(operator):
    v,config,receiver,cloud,engine,write_json=operator
    path=Path(config['receiver_config_file']).parent/'verifier.json';write_json(path,config)
    assert v.load_config(path)==config
    write_json(path,{**config,'profile':'calendar'})
    with pytest.raises(ValueError,match='verifier_profile_mismatch'):v.load_config(path)
    write_json(Path(config['receiver_config_file']),{**receiver,'profile':'calendar'})
    assert v.load_config(path)=={**config,'profile':'calendar'}
    for profile in ('notes',None,['calendar']):
        write_json(path,{**config,'profile':profile})
        with pytest.raises(ValueError,match='invalid_verifier_config'):v.load_config(path)
    write_json(Path(config['receiver_config_file']),{**receiver,'profile':'notes'})
    with pytest.raises(ValueError,match='invalid_config'):r2.load_config(config['receiver_config_file'])


# spec: ANYM-606
def test_calendar_run_protects_its_own_proof_and_rotates_two_points(calendar_operator):
    from test_backup_r2 import receive,THIRD
    v,config,receiver,cloud,engine,_=calendar_operator
    assert v.run(config,client=cloud,engine=engine)['status']=='verified'
    assert engine.workers[0]['profile']=='calendar'
    proof=json.loads(next(Path(config['directory']).glob('proof-*.json')).read_text())
    assert proof['verification']['profile']=='calendar' and set(proof['verification']['checks'])==r2.CALENDAR_CHECKS
    assert proof['verification']['vault_warnings']==[{'kind':'disabled_connection_without_token','name':'old'}]
    old=Path(config['protected_file']).read_bytes()
    receive(receiver,cloud,bid=THIRD)
    assert v.run(config,client=cloud,engine=engine)['backup_id']==THIRD
    assert Path(config['protected_file']).read_bytes()!=old
    objects=[key for key in cloud.objects if '/objects/' in key]
    assert len(objects)==1 and objects[0].startswith(receiver['prefix']+'/objects/'+THIRD+'-')


# spec: ANYM-606
def test_a_calendar_bundle_missing_an_enabled_token_keeps_the_old_point_protected_and_present(calendar_operator):
    from test_backup_r2 import receive,THIRD
    v,config,receiver,cloud,engine,_=calendar_operator
    v.run(config,client=cloud,engine=engine)
    marker=Path(config['protected_file']).read_bytes();objects=dict(cloud.objects)
    receive(receiver,cloud,bid=THIRD)
    engine.failure=v.HelperFailure('restore_vault_incomplete')
    with pytest.raises(RuntimeError,match='restore_vault_incomplete'):v.run(config,client=cloud,engine=engine)
    assert json.loads((Path(config['directory'])/'status.json').read_text())['error']=='restore_vault_incomplete'
    assert Path(config['protected_file']).read_bytes()==marker
    assert all(cloud.objects.get(key)==value for key,value in objects.items())


# spec: ANYM-606
@pytest.mark.parametrize('case',['messaging_facts_on_calendar','calendar_facts_on_messaging','retry_changed_profile',
                                 'receiver_changed_profile'])
def test_every_profile_mismatch_fails_and_protects_nothing(calendar_operator,case):
    v,config,receiver,cloud,engine,write_json=calendar_operator
    if case=='messaging_facts_on_calendar':engine.calendar=False
    elif case=='calendar_facts_on_messaging':
        write_json(Path(config['receiver_config_file']),{**receiver,'profile':'messaging'});config={**config,'profile':'messaging'}
    elif case=='receiver_changed_profile':write_json(Path(config['receiver_config_file']),{**receiver,'profile':'messaging'})
    else:
        # A messaging run left its journal; the same directory then runs as the calendar instance.
        write_json(Path(config['receiver_config_file']),{**receiver,'profile':'messaging'})
        engine.failure=RuntimeError('restore_quarantine_failed')
        with pytest.raises(RuntimeError):v.run({**config,'profile':'messaging'},client=cloud,engine=engine)
        assert 'profile' not in json.loads((Path(config['directory'])/'active.json').read_text())
        write_json(Path(config['receiver_config_file']),receiver);engine.failure=None
    runs=len(engine.runs)
    with pytest.raises(ValueError,match='verifier_profile_mismatch'):v.run(config,client=cloud,engine=engine)
    assert json.loads((Path(config['directory'])/'status.json').read_text())['error']=='verifier_profile_mismatch'
    assert not Path(config['protected_file']).exists()
    assert len(engine.runs)==runs+(case in ('messaging_facts_on_calendar','calendar_facts_on_messaging'))


# spec: ANYM-606
def test_proof_check_sets_are_bound_to_their_profile_and_never_mix():
    from test_backup_r2 import BID
    v=module();receipt={'wire':{'id':BID,'bytes':100,'sha256':'a'*64,'snapshot_sha256':'b'*64,
                                'received_at':'2026-10-16T09:17:03+00:00'}}
    messaging_facts={'v':1,'status':'verified','schema_version':34,'manifest_sha256':'b'*64,'checks':{k:True for k in r2.CHECKS}}
    messaging=v.make_proof(receipt,messaging_facts,'c'*32);calendar=v.make_proof(receipt,calendar_facts(receipt),'c'*32,'calendar')
    assert 'profile' not in messaging['verification'] and r2.validate_proof(messaging,receipt)
    assert calendar['verification']['profile']=='calendar' and r2.validate_proof(calendar,receipt,'calendar')
    for proof,profile in ((messaging,'calendar'),(calendar,'messaging')):
        with pytest.raises(ValueError,match='verifier_profile_mismatch'):r2.validate_proof(proof,receipt,profile)
    for checks in (r2.CHECKS,r2.CHECKS|r2.CALENDAR_CHECKS,r2.CALENDAR_CHECKS-{'vault_decrypts'}):
        mixed={**calendar,'verification':{**calendar['verification'],'checks':{k:True for k in checks}}}
        with pytest.raises(ValueError,match='independent_restore_proof_required'):r2.validate_proof(mixed,receipt,'calendar')
        with pytest.raises(ValueError,match='invalid_helper_result'):
            v.make_proof(receipt,calendar_facts(receipt,checks={k:True for k in checks}),'c'*32,'calendar')
    union={**messaging,'verification':{**messaging['verification'],'checks':{k:True for k in r2.CHECKS|r2.CALENDAR_CHECKS}}}
    with pytest.raises(ValueError,match='independent_restore_proof_required'):r2.validate_proof(union,receipt)
    for warnings in ([{'kind':'x','name':'y','extra':1}],[{'kind':'Bad Kind','name':'y'}],[{'kind':'x','name':'n'*65}],
                     [{'kind':'x','name':'y'}]*33,{'kind':'x'}):
        with pytest.raises(ValueError,match='invalid_helper_result'):
            v.make_proof(receipt,calendar_facts(receipt,vault_warnings=warnings),'c'*32,'calendar')
    with pytest.raises(ValueError,match='verifier_profile_mismatch'):v.make_proof(receipt,messaging_facts,'c'*32,'calendar')
    with pytest.raises(ValueError,match='verifier_profile_mismatch'):v.make_proof(receipt,calendar_facts(receipt),'c'*32)


# spec: ANYM-606
def test_a_calendar_proof_with_the_most_warnings_the_helper_keeps_is_protected(calendar_operator):
    from messaging_service import backup_restore_worker as worker
    v,config,receiver,cloud,engine,_=calendar_operator
    warnings=worker.proof_warnings([{'kind':'unknown_entry','name':'\U0001f600'*60+'%02d'%i} for i in range(40)])
    restore=engine.restore
    def wide(*args):
        facts=restore(*args);return {**facts,'vault_warnings':warnings}
    engine.restore=wide
    assert v.run(config,client=cloud,engine=engine)['status']=='verified'
    proof=next(Path(config['directory']).glob('proof-*.json'))
    assert proof.stat().st_size<16384 and json.loads(proof.read_text())['verification']['vault_warnings']==warnings
    assert Path(config['protected_file']).exists()


# spec: ANYM-606
def test_protect_refuses_a_proof_of_the_other_profile(calendar_operator):
    v,config,receiver,cloud,engine,write_json=calendar_operator
    engine.failure=RuntimeError('stop before promotion')
    with pytest.raises(RuntimeError):v.run(config,client=cloud,engine=engine)
    active=json.loads((Path(config['directory'])/'active.json').read_text())
    messaging={'v':1,'status':'verified','schema_version':34,'manifest_sha256':active['receipt']['wire']['snapshot_sha256'],
               'checks':{k:True for k in r2.CHECKS}}
    proof=Path(config['directory'])/'proof-messaging.json'
    write_json(proof,v.make_proof(active['receipt'],messaging,active['run']))
    with pytest.raises(ValueError,match='verifier_profile_mismatch'):
        r2.protect(r2.load_config(config['receiver_config_file']),config['protected_file'],proof,client=cloud)
    assert not Path(config['protected_file']).exists()


# spec: ANYM-606
@pytest.mark.parametrize('code',['restore_bundle_failed','restore_schema_failed','restore_inventory_failed',
                                 'restore_vault_incomplete','restore_vault_decrypt_failed','verifier_profile_mismatch'])
def test_calendar_helper_failure_fact_reaches_the_status(operator,monkeypatch,code):
    v,config,receiver,cloud,engine,_=operator
    docker=v.Docker(60);helpers=[]
    monkeypatch.setattr(docker,'setup',lambda *a:None)
    monkeypatch.setattr(docker,'inspect_owned',lambda *a:{})
    monkeypatch.setattr(v,'validate_helper_layout',lambda *a:None)
    monkeypatch.setattr(r2,'verify_object',lambda receiver,client,receipt,out:out.write(b'synthetic-ciphertext'))
    fact=r2.encoded({'v':1,'status':'failed','error':code})
    def popen(*args,**kwargs):
        helpers.append(ExitedHelper(next(Path(config['directory']).glob('payload-*')),fact,False));return helpers[-1]
    monkeypatch.setattr(v.subprocess,'Popen',popen)
    engine.restore=docker.restore
    with pytest.raises(RuntimeError):v.run(config,client=cloud,engine=engine)
    assert json.loads((Path(config['directory'])/'status.json').read_text())['error']==code


# spec: ANYM-606
def test_calendar_units_run_the_same_module_with_their_own_config_and_messaging_limits():
    root=Path(__file__).resolve().parents[1]/'deploy'
    def unit(name):
        values={}
        for line in (root/name).read_text().splitlines():
            if '=' in line and not line.startswith('#'):key,value=line.split('=',1);values.setdefault(key,[]).append(value)
        return values
    service=unit('deedkit-calendar-backup-r2-verifier.service');messaging=unit('deedkit-backup-r2-verifier.service')
    assert service['ExecStart']==['/opt/deedkit-backup-r2/venv/bin/python -m messaging_service.backup_r2_verifier '
                                  '--config /etc/deedkit-calendar-backup-r2/verifier.json']
    assert {k:v for k,v in service.items() if k not in ('ExecStart','Description')}=={
        k:v for k,v in messaging.items() if k not in ('ExecStart','Description')}
    timer=unit('deedkit-calendar-backup-r2-verifier.timer');messaging_timer=unit('deedkit-backup-r2-verifier.timer')
    assert timer['OnUnitInactiveSec']==['2min'] and timer['Unit']==['deedkit-calendar-backup-r2-verifier.service']
    assert {k:v for k,v in timer.items() if k not in ('Unit','Description')}=={
        k:v for k,v in messaging_timer.items() if k not in ('Unit','Description')}
    example=r2.unique_json((root/'calendar-backup-r2-verifier.example.json').read_bytes())
    messaging_example=r2.unique_json((root/'backup-r2-verifier.example.json').read_bytes())
    assert example['profile']=='calendar' and set(example)==set(messaging_example)|{'profile'}
    paths=('receiver_config_file','directory','identity_file','protected_file')
    assert all(example[k].startswith(('/etc/deedkit-calendar-backup-r2/','/var/lib/deedkit-calendar-backup-r2/')) for k in paths)
    assert not any(Path(example[a]).is_relative_to(Path(messaging_example[b]).parent) or example[a]==messaging_example[b]
                   for a in paths for b in paths)


class HostEngine:
    """Synthetic Docker shared by two verifier instances: exact named resources, one log of creates and removes."""
    def __init__(self,v,root,host):
        self.v=v;self.root=root;self.host=host;self.resources={};self.log=[];self.during=None;self.failure=None
        self.fail_remove=set();self.admissions=[]
    def preflight(self,config):return str(self.root)
    def inspect_owned(self,kind,name,run):
        owner=self.resources.get((kind,name))
        if owner is not None and owner!=run:raise ValueError('unowned_restore_resource')
        return None if owner is None else {'Labels':{self.v.LABEL:owner}}
    def remove(self,kind,name):
        if name in self.fail_remove:raise RuntimeError('restore_docker_failed')
        self.log.append(('remove',name));self.resources.pop((kind,name))
    def restore(self,config,receiver,client,receipt,run,payload,secret_dir):
        # The holder's record names every resource before the first one exists.
        self.admissions.append(json.loads((self.host/'admission.json').read_text()))
        for kind,name in self.v.names(run,r2.profile(config)).items():
            self.resources[(kind,name)]=run;self.log.append(('create',name))
        r2.verify_object(receiver,client,receipt,io.BytesIO())
        if self.during:
            hook,self.during=self.during,None;hook()
        if self.failure:raise self.failure
        if r2.profile(config)=='calendar':return calendar_facts(receipt)
        return {'v':1,'status':'verified','schema_version':34,'manifest_sha256':receipt['wire']['snapshot_sha256'],
                'checks':{k:True for k in r2.CHECKS}}


@pytest.fixture
def instances(operator,tmp_path):
    # Two verifier instances on one receiver (ANYM-609): operator's messaging one and a calendar one with
    # its own receiver config, state, key, marker, directory and bucket prefix; one shared host directory.
    from test_backup_r2 import Cloud,receive
    v,config,receiver,cloud,_,write_json=operator
    for name in ('cal-state','cal-control','cal-verifier'):(tmp_path/name).mkdir(mode=0o700)
    control=tmp_path/'cal-control'
    credentials=control/'credentials.json';write_json(credentials,{'access_key_id':'c'*32,'secret_access_key':'d'*64})
    calendar_receiver={**receiver,'state_directory':str(tmp_path/'cal-state'),'credentials_file':str(credentials),'prefix':'calendar'}
    calendar_cloud=Cloud();receive(calendar_receiver,calendar_cloud)
    calendar_receiver={**calendar_receiver,'profile':'calendar'};write_json(control/'receiver.json',calendar_receiver)
    identity=control/'identity';identity.write_text('SYNTHETIC_NOT_A_KEY');identity.chmod(0o600)
    calendar={**config,'profile':'calendar','receiver_config_file':str(control/'receiver.json'),
              'directory':str(tmp_path/'cal-verifier'),'identity_file':str(identity),'protected_file':str(control/'protected.json')}
    messaging_path=Path(config['receiver_config_file']).parent/'verifier.json';write_json(messaging_path,config)
    calendar_path=control/'verifier.json';write_json(calendar_path,calendar)
    def listed():write_json(v.INSTANCES_FILE,{'v':1,'verifier_configs':[str(messaging_path),str(calendar_path)]})
    host=tmp_path/'host'
    return SimpleNamespace(v=v,messaging=config,calendar=calendar,cloud=cloud,calendar_cloud=calendar_cloud,
                           engine=HostEngine(v,tmp_path,host),host=host,paths=[messaging_path,calendar_path],listed=listed,
                           write_json=write_json,status=lambda c:json.loads((Path(c['directory'])/'status.json').read_text()))


def created(engine,profile):
    return [name for action,name in engine.log if action=='create' and name.startswith('deedkit-verify-'+profile+'-')]


# spec: ANYM-609
def test_two_verifier_instances_started_together_take_turns_on_the_host_lock_and_both_finish(instances,monkeypatch):
    import contextlib
    import threading
    import time
    s=instances;v=s.v;waiting=threading.Event();real_sleep=time.sleep;results={}
    # The second instance runs in a thread: no signal deadline there, and no timer left armed.
    monkeypatch.setattr(v,'deadline',lambda seconds:contextlib.nullcontext())
    monkeypatch.setattr(v.signal,'setitimer',lambda *args:None)
    def sleep(seconds):waiting.set();real_sleep(0.01)
    monkeypatch.setattr(v.time,'sleep',sleep)
    def calendar():
        try:results['calendar']=v.run(s.calendar,client=s.calendar_cloud,engine=s.engine)
        except BaseException as error:results['calendar']=error
    thread=threading.Thread(target=calendar)
    def during():
        # Messaging is mid-restore holding the host lock: the calendar instance waits and creates nothing.
        thread.start();assert waiting.wait(10)
        assert created(s.engine,'calendar')==[] and not (Path(s.calendar['directory'])/'active.json').exists()
    s.engine.during=during
    assert v.run(s.messaging,client=s.cloud,engine=s.engine)['status']=='verified'
    thread.join(30);assert not thread.is_alive()
    assert results['calendar']['status']=='verified',results
    first_calendar=s.engine.log.index(('create',created(s.engine,'calendar')[0]))
    assert all(s.engine.log.index(('remove',name))<first_calendar for name in created(s.engine,'messaging'))
    assert not s.engine.resources and not (s.host/'admission.json').exists()


# spec: ANYM-609
def test_a_host_lock_wait_past_its_bound_ends_busy_with_nothing_created_and_the_journal_unchanged(instances,monkeypatch):
    import time
    s=instances;v=s.v;calendar={**s.calendar,'verify_wait_seconds':1};real_sleep=time.sleep;slept=[]
    monkeypatch.setattr(v.time,'sleep',lambda seconds:(slept.append(seconds),real_sleep(min(seconds,0.05))))
    active=Path(calendar['directory'])/'active.json'
    with v.host_lock(s.messaging):
        with pytest.raises(RuntimeError,match='verifier_host_busy'):v.run(calendar,client=s.calendar_cloud,engine=s.engine)
    assert slept and not s.engine.log and not active.exists() and s.status(calendar)['error']=='verifier_host_busy'
    # A pending retry stays exactly as it was.
    s.engine.failure=RuntimeError('restore_docker_failed')
    with pytest.raises(RuntimeError,match='restore_docker_failed'):v.run(calendar,client=s.calendar_cloud,engine=s.engine)
    journal=active.read_bytes();log=list(s.engine.log);s.engine.failure=None
    with v.host_lock(s.messaging):
        with pytest.raises(RuntimeError,match='verifier_host_busy'):v.run(calendar,client=s.calendar_cloud,engine=s.engine)
    assert active.read_bytes()==journal and s.engine.log==log
    assert v.run(calendar,client=s.calendar_cloud,engine=s.engine)['status']=='verified'


# spec: ANYM-609
def test_low_free_disk_after_the_host_wait_refuses_before_creating_anything(instances,monkeypatch):
    import contextlib
    s=instances;v=s.v;holder=contextlib.ExitStack();holder.enter_context(v.host_lock(s.messaging))
    def sleep(seconds):
        # The other instance finishes, leaving too little free disk behind.
        holder.close();monkeypatch.setattr(v.shutil,'disk_usage',lambda path:SimpleNamespace(free=v.GIB))
    monkeypatch.setattr(v.time,'sleep',sleep)
    with pytest.raises(ValueError,match='restore_disk_capacity'):v.run(s.calendar,client=s.calendar_cloud,engine=s.engine)
    assert not s.engine.log and not s.engine.resources and not (s.host/'admission.json').exists()
    assert s.status(s.calendar)['error']=='restore_disk_capacity'


# spec: ANYM-609
def test_scratch_resources_and_their_admission_record_are_removed_on_success_and_failure(instances):
    s=instances;v=s.v
    assert v.run(s.messaging,client=s.cloud,engine=s.engine)['status']=='verified'
    record=s.engine.admissions[-1];run=json.loads((Path(s.messaging['directory'])/'last-success.json').read_text())['verification_id']
    assert record['profile']=='messaging' and record['run']==run
    # A messaging run also names its pre-ANYM-609 names: a retry may still own them.
    assert record['resources']==[[kind,name,run] for names in (v.names(run,'messaging'),v.legacy_names(run))
                                 for kind,name in names.items()]
    assert not s.engine.resources and not (s.host/'admission.json').exists()
    s.engine.failure=RuntimeError('restore_docker_failed')
    with pytest.raises(RuntimeError):v.run(s.calendar,client=s.calendar_cloud,engine=s.engine)
    assert s.engine.admissions[-1]['profile']=='calendar' and created(s.engine,'calendar')
    assert not s.engine.resources and not (s.host/'admission.json').exists()
    assert s.status(s.calendar)['cleanup_complete'] is True


# spec: ANYM-609
def test_scratch_names_carry_the_profile_and_a_messaging_retry_from_before_still_removes_its_old_names():
    v=module();run='a'*32
    messaging,calendar=v.names(run,'messaging'),v.names(run,'calendar')
    assert all('-messaging-'+run in name for name in messaging.values()) and all('-calendar-'+run in name for name in calendar.values())
    assert not set(messaging.values())&set(calendar.values()) and v.names(run)==messaging
    removed=[]
    class Engine:
        def inspect_owned(self,kind,name,owner):return {} if name in v.legacy_names(run).values() else None
        def remove(self,kind,name):removed.append(name)
    v.cleanup_resources(Engine(),run);assert removed==list(v.legacy_names(run).values())
    removed.clear();v.cleanup_resources(Engine(),run,'calendar');assert removed==[]


def kill_holder(s,profile='messaging'):
    """A coordinator SIGKILLed mid-restore: the kernel released its lock, its record and containers remain."""
    run='f'*32
    with s.v.host_lock(s.messaging) as fd:
        s.v.admit(s.engine,fd,profile,run,s.v.run_resources(run,profile))
        for kind,name,owner in s.v.run_resources(run,profile):s.engine.resources[(kind,name)]=owner
    return run


# spec: ANYM-609
def test_a_sigkilled_holders_resources_are_removed_before_the_other_profile_starts(instances):
    s=instances;v=s.v;run=kill_holder(s)
    assert v.run(s.calendar,client=s.calendar_cloud,engine=s.engine)['status']=='verified'
    survivors=list(v.names(run,'messaging').values())
    assert [name for action,name in s.engine.log][:4]==survivors and [a for a,_ in s.engine.log][:4]==['remove']*4
    assert not s.engine.resources and not (s.host/'admission.json').exists()


# spec: ANYM-609
def test_a_failed_removal_of_a_dead_holders_resources_makes_the_other_profile_refuse(instances):
    s=instances;v=s.v;run=kill_holder(s);s.engine.fail_remove={v.names(run,'messaging')['postgres']}
    record=(s.host/'admission.json').read_bytes()
    with pytest.raises(RuntimeError,match='verifier_cleanup_unconfirmed'):
        v.run(s.calendar,client=s.calendar_cloud,engine=s.engine)
    assert created(s.engine,'calendar')==[] and not (Path(s.calendar['directory'])/'active.json').exists()
    assert (s.host/'admission.json').read_bytes()==record and s.status(s.calendar)['error']=='verifier_cleanup_unconfirmed'
    s.engine.fail_remove=set()
    assert v.run(s.calendar,client=s.calendar_cloud,engine=s.engine)['status']=='verified' and not s.engine.resources


class PinEngine(HostEngine):
    """The pin step's Docker: an image preflight that knows only the reviewed pairs."""
    def __init__(self,*args):super().__init__(*args);self.preflights=[]
    def preflight(self,config):
        self.preflights.append((r2.profile(config),config['helper_image'],config['helper_revision']));return str(self.root)


@pytest.fixture
def pins(instances,monkeypatch):
    from messaging_service import backup_r2_follow as f
    s=instances;s.listed();s.f=f;s.tested=[];s.failures={}
    s.engine=PinEngine(s.v,s.engine.root,s.host)
    def test(engine,tested,run=None):
        s.tested.append((r2.profile(tested),tested['helper_revision']))
        if r2.profile(tested) in s.failures:raise s.failures[r2.profile(tested)]
        return ([2,50],50) if r2.profile(tested)=='messaging' else ([2,50],4)
    s.update=lambda:f.update_pins(s.v.INSTANCES_FILE,NEW_IMAGE,HEAD,engine=s.engine,test=test)
    s.pairs=lambda:[(c['helper_image'],c['helper_revision']) for c in map(s.v.load_config,s.paths)]
    s.old=(s.messaging['helper_image'],s.messaging['helper_revision'])
    monkeypatch.setattr(f,'Docker',lambda timeout:s.engine);monkeypatch.setattr(f,'self_test',test)
    return s


# spec: ANYM-609
def test_the_pin_step_moves_every_instance_to_one_self_tested_pair_and_keeps_the_previous_pins(pins,capsys):
    s=pins
    assert s.v.main(['--update-pins','--helper-image',NEW_IMAGE,'--helper-revision',HEAD])==0
    assert json.loads(capsys.readouterr().out)=={'v':1,'status':'pinned','helper_revision':HEAD}
    assert s.pairs()==[(NEW_IMAGE,HEAD)]*2 and s.tested==[('messaging',HEAD),('calendar',HEAD)]
    assert {p for p,_,_ in s.engine.preflights}=={'messaging','calendar'}
    previous=json.loads((s.host/'pins-previous.json').read_text())
    assert previous=={'v':1,'pins':{str(p):list(s.old) for p in s.paths}}
    assert not (s.host/'pins-switch.json').exists() and not (s.host/'admission.json').exists()
    # Every other key of both configs is unchanged.
    for path,config in zip(s.paths,(s.messaging,s.calendar)):
        assert {**s.v.load_config(path),'helper_image':config['helper_image'],'helper_revision':config['helper_revision']}==config


# spec: ANYM-609
def test_a_pending_calendar_retry_journal_blocks_the_pin_step(pins):
    s=pins;before=[p.read_bytes() for p in s.paths]
    s.write_json(Path(s.calendar['directory'])/'active.json',{'v':1,'phase':'restoring'})
    with pytest.raises(RuntimeError,match='verification_pending'):s.update()
    assert [p.read_bytes() for p in s.paths]==before and not s.tested
    with s.v.verifier_lock(s.calendar['directory']):
        with pytest.raises(RuntimeError,match='verifier_busy'):s.update()


# spec: ANYM-609
def test_a_failing_calendar_self_test_keeps_both_configs_on_the_old_pins(pins):
    s=pins;before=[p.read_bytes() for p in s.paths];s.failures['calendar']=RuntimeError('helper_self_test_failed')
    with pytest.raises(RuntimeError,match='helper_self_test_failed'):s.update()
    assert [p.read_bytes() for p in s.paths]==before and s.tested==[('messaging',HEAD),('calendar',HEAD)]
    assert not (s.host/'pins-previous.json').exists() and not (s.host/'pins-switch.json').exists()


# spec: ANYM-609
@pytest.mark.parametrize('crash',['failure_first','failure_second','stop_second'])
def test_a_pin_step_crash_mid_write_leaves_all_old_or_all_new(pins,monkeypatch,crash):
    s=pins;original=os.replace;writes=[]
    def replace(src,dst,**kw):
        if dst=='verifier.json':
            writes.append(dst)
            if len(writes)==(1 if crash=='failure_first' else 2):
                if crash=='stop_second':r2.cancel(InterruptedError('restore_stopped'))  # the service stop's handler
                raise OSError('synthetic crash')
        return original(src,dst,**kw)
    monkeypatch.setattr(os,'replace',replace)
    with pytest.raises((OSError,InterruptedError)):s.update()
    monkeypatch.setattr(os,'replace',original)
    if crash=='stop_second':
        # Stopped between two writes: the next verifier of either instance puts every config back
        # on its old pair before it uses a pin, and runs on the old pair.
        assert (s.host/'pins-switch.json').exists()
        assert s.v.run(s.calendar,client=s.calendar_cloud,engine=s.engine)['status']=='verified'
        assert s.engine.admissions[-1]['profile']=='calendar' and s.pairs()==[s.old]*2
        assert json.loads(next(Path(s.calendar['directory']).glob('proof-*.json')).read_text())['status']=='verified'
    assert s.pairs()==[s.old]*2 and not (s.host/'pins-switch.json').exists()
    assert s.update()['status']=='pinned' and s.pairs()==[(NEW_IMAGE,HEAD)]*2


# spec: ANYM-609
def test_follow_moves_the_calendar_pins_with_messagings_after_self_testing_both_profiles(follower,instances):
    s=follower;instances.listed();calendar=instances.paths[1]
    old=(s.config['helper_image'],s.config['helper_revision'])
    assert s.run()['status']=='switched'
    assert [(c['helper_image'],c['helper_revision']) for c in map(s.v.load_config,(s.path,calendar))]==[(NEW_IMAGE,HEAD)]*2
    assert s.profiles==['messaging','messaging','calendar']
    assert s.revert()['status']=='reverted' and s.profiles[3:]==['calendar']
    assert [(c['helper_image'],c['helper_revision']) for c in map(s.v.load_config,(s.path,calendar))]==[old]*2


# spec: ANYM-609
def test_follow_keeps_both_instances_on_the_old_pins_when_the_calendar_self_test_fails(follower,instances):
    s=follower;instances.listed();before=[p.read_bytes() for p in instances.paths]
    s.calendar_failure=RuntimeError('helper_self_test_failed')
    with pytest.raises(RuntimeError,match='helper_self_test_failed'):s.run()
    assert [p.read_bytes() for p in instances.paths]==before and NEW_IMAGE in s.docker.removed
    assert follow_status(s)['error']=='helper_self_test_failed'
    write=instances.write_json;write(Path(instances.calendar['directory'])/'active.json',{'v':1})
    with pytest.raises(RuntimeError,match='verification_pending'):s.run()


# spec: ANYM-609
def test_verifier_units_allow_the_host_wait_on_top_of_the_restore_deadline_and_the_instances_example_loads(monkeypatch):
    from messaging_service import backup_r2_follow as f
    v=module();root=Path(__file__).resolve().parents[1]/'deploy'
    for name in ('deedkit-backup-r2-verifier.service','deedkit-calendar-backup-r2-verifier.service'):
        limit=[line.split('=',1)[1] for line in (root/name).read_text().splitlines() if line.startswith('TimeoutStartSec=')]
        assert limit==['26400s'] and 26400>=21600+v.VERIFY_WAIT_SECONDS+v.BUSY_WAIT_MAX+180
    example=root/'backup-r2-instances.example.json'
    monkeypatch.setattr(v,'private_file',lambda path,limit:Path(path).read_bytes())
    assert f.read_instances(example)==[Path(p) for p in r2.unique_json(example.read_bytes())['verifier_configs']]


# spec: ANYM-609
def test_a_messaging_retrys_old_named_survivor_stays_in_the_admission_record_until_it_is_gone(instances):
    s=instances;v=s.v;s.engine.failure=RuntimeError('restore_docker_failed')
    with pytest.raises(RuntimeError):v.run(s.messaging,client=s.cloud,engine=s.engine)
    run=json.loads((Path(s.messaging['directory'])/'active.json').read_text())['run']
    # A PostgreSQL the retry started before ANYM-609, under its old name, that will not go away.
    survivor=v.legacy_names(run)['postgres'];s.engine.resources[('postgres',survivor)]=run;s.engine.fail_remove={survivor}
    s.engine.failure=None
    with pytest.raises(RuntimeError,match='restore_docker_failed'):v.run(s.messaging,client=s.cloud,engine=s.engine)
    assert ['postgres',survivor,run] in json.loads((s.host/'admission.json').read_text())['resources']
    with pytest.raises(RuntimeError,match='verifier_cleanup_unconfirmed'):
        v.run(s.calendar,client=s.calendar_cloud,engine=s.engine)
    assert created(s.engine,'calendar')==[]


# spec: ANYM-609
def test_the_restore_deadline_pauses_while_the_run_waits_for_the_host_lock(instances,monkeypatch):
    import contextlib
    import time
    s=instances;v=s.v;holder=contextlib.ExitStack();holder.enter_context(v.host_lock(s.messaging));real_sleep=time.sleep
    waited=[]
    def sleep(seconds):
        # The other instance holds the lock for longer than this run's whole (real SIGALRM) deadline.
        real_sleep(0.25);waited.append(0.25)
        if sum(waited)>=1.5:holder.close()
    monkeypatch.setattr(v.time,'sleep',sleep)
    config={**s.calendar,'timeout_seconds':1,'verify_wait_seconds':10}
    assert v.run(config,client=s.calendar_cloud,engine=s.engine)['status']=='verified' and sum(waited)>=1.5


# spec: ANYM-609
def test_a_pin_step_failing_after_its_config_writes_puts_every_config_back(pins,monkeypatch):
    s=pins;original=os.replace;before=[p.read_bytes() for p in s.paths]
    def replace(src,dst,**kw):
        if dst=='pins-previous.json':raise OSError('synthetic failure')
        return original(src,dst,**kw)
    monkeypatch.setattr(os,'replace',replace)
    with pytest.raises(OSError):s.update()
    assert s.pairs()==[s.old]*2 and not (s.host/'pins-switch.json').exists()
    assert [p.read_bytes() for p in s.paths]==before


# spec: ANYM-609
def test_follow_revert_refuses_a_kept_pair_the_calendar_self_test_fails_and_changes_nothing(follower,instances):
    s=follower;instances.listed();s.run();before=[p.read_bytes() for p in instances.paths]
    kept=s.record('follow-previous.json');s.calendar_failure=RuntimeError('helper_self_test_failed')
    with pytest.raises(RuntimeError,match='helper_self_test_failed'):s.revert()
    assert [p.read_bytes() for p in instances.paths]==before and s.record('follow-previous.json')==kept
    assert s.profiles[-1]=='calendar'


# spec: ANYM-609
def test_follow_after_a_stopped_pin_switch_reconciles_against_the_restored_configs(follower,instances,monkeypatch):
    s=follower;instances.listed();old=(s.config['helper_image'],s.config['helper_revision'])
    # A kept pair from an earlier switch, whose image only a finished switch may drop.
    kept_image='sha256:'+'9'*64;s.images[kept_image]={'Id':kept_image}
    s.write_json(s.directory/'follow-previous.json',{'v':1,'helper_image':kept_image,'helper_revision':'9'*40,
                                                      'helper_schemas':[2,50],'tested_at':'2026-10-28T00:00:00+00:00'})
    original=os.replace;writes=[]
    def replace(src,dst,**kw):
        if dst=='verifier.json':
            writes.append(dst)
            if len(writes)==2:r2.cancel(InterruptedError('restore_stopped'))  # stopped between the two configs
        return original(src,dst,**kw)
    monkeypatch.setattr(os,'replace',replace)
    with pytest.raises(InterruptedError):s.run()
    monkeypatch.setattr(os,'replace',original)
    assert (s.directory/'follow-switch.json').exists() and (instances.host/'pins-switch.json').exists()
    del s.proofs[HEAD]  # the next run gets no candidate: only recovery happens
    with pytest.raises(RuntimeError,match='helper_unattested'):s.run()
    pairs=[(c['helper_image'],c['helper_revision']) for c in map(s.v.load_config,instances.paths)]
    assert pairs==[old]*2 and kept_image not in s.docker.removed
    assert s.record('follow-previous.json')['helper_image']==kept_image
    assert s.record('follow-current.json')['helper_revision']==old[1]
    assert not (s.directory/'follow-switch.json').exists() and not (instances.host/'pins-switch.json').exists()


# spec: ANYM-609
def test_follow_moves_a_newly_listed_instance_to_the_current_pair_after_its_self_test(follower,instances):
    s=follower;assert s.run()['status']=='switched'  # messaging alone follows main first
    instances.listed();calendar=instances.paths[1];before=calendar.read_bytes()
    s.calendar_failure=RuntimeError('helper_self_test_failed')
    with pytest.raises(RuntimeError,match='helper_self_test_failed'):s.run()
    assert calendar.read_bytes()==before and follow_status(s)['error']=='helper_self_test_failed'
    s.calendar_failure=None;result=s.run()
    assert result['status']=='current' and result['aligned'] is True and s.profiles[-1]=='calendar'
    assert (s.v.load_config(calendar)['helper_image'],s.v.load_config(calendar)['helper_revision'])==(NEW_IMAGE,HEAD)
    assert s.run().get('aligned') is None


# spec: ANYM-609
def test_follow_self_test_restores_a_synthetic_calendar_bundle_with_the_calendar_worker_config(self_test_setup):
    v,f,directory,config=self_test_setup;config={**config,'profile':'calendar'}
    class CalendarEngine(SelfTestEngine):
        def __init__(self,*args):super().__init__(*args);self.scripts=[];self.workers=[];self.created=[]
        def command(self,*args):
            if args[:2]==('container','create'):self.scripts.append(args[-1]);self.created.append(args[3])
            return super().command(*args)
        def setup(self,tested,run,payload,secret_dir):
            super().setup(tested,run,payload,secret_dir);self.created+=list(v.names(run,r2.profile(tested)).values())
        def restore(self,tested,receiver,client,receipt,run,payload,secret_dir):
            super().restore(tested,receiver,client,receipt,run,payload,secret_dir)
            self.workers.append(json.loads((secret_dir/'worker.json').read_text()))
            for kind,name in v.names(run,'calendar').items():self.resources[(kind,name)]={}
            return calendar_facts(receipt)
    engine=CalendarEngine(v,f,config)
    assert f.self_test(engine,config)==([2,50],5)
    assert engine.scripts[-1]==f.CALENDAR_FIXTURE_SCRIPT and engine.workers[0]['profile']=='calendar'
    assert all('-calendar-' in name for name in engine.created) and engine.created
    assert list(directory.iterdir())==[] and not engine.resources


# spec: ANYM-609
def test_a_verifier_outside_a_running_pin_step_never_undoes_it(pins,monkeypatch):
    s=pins;s.write_json(s.v.INSTANCES_FILE,{'v':1,'verifier_configs':[str(s.paths[0])]});original=os.replace;ran=[]
    def replace(src,dst,**kw):
        result=original(src,dst,**kw)
        if dst=='verifier.json' and not ran:
            # Between the writer's config writes, the calendar instance (not in its instances file) runs.
            ran.append(s.v.run(s.calendar,client=s.calendar_cloud,engine=s.engine))
        return result
    monkeypatch.setattr(os,'replace',replace)
    assert s.update()['status']=='pinned' and ran[0]['status']=='verified'
    assert s.pairs()==[(NEW_IMAGE,HEAD),s.old] and not (s.host/'pins-switch.json').exists()


# spec: ANYM-609
def test_a_pin_step_failing_after_its_commit_point_never_rolls_back(pins,monkeypatch):
    s=pins;original_unlink=os.unlink;original_replace=os.replace;committed=[];writes=[]
    def unlink(name,*args,**kw):
        original_unlink(name,*args,**kw)
        if name=='pins-switch.json':committed.append(name);raise OSError('synthetic failure after the unlink')
    def replace(src,dst,**kw):
        if committed and dst=='verifier.json':
            writes.append(dst)
            if len(writes)==2:raise OSError('synthetic crash in a rollback')
        return original_replace(src,dst,**kw)
    monkeypatch.setattr(os,'unlink',unlink);monkeypatch.setattr(os,'replace',replace)
    with pytest.raises(OSError):s.update()
    assert committed and s.pairs()==[(NEW_IMAGE,HEAD)]*2 and not (s.host/'pins-switch.json').exists()


def receiver_held(receiver):
    import contextlib
    holder=contextlib.ExitStack();holder.enter_context(r2.local.locked(receiver['state_directory']))
    return holder

def busy_main(v,config,cloud,engine,monkeypatch):
    real=v.run
    monkeypatch.setattr(v,'load_config',lambda path:config)
    monkeypatch.setattr(v,'run',lambda value,path=None:real(value,client=cloud,engine=engine,path=None))

# spec: ANYM-632
def test_a_receiver_lock_busy_for_40_seconds_is_waited_out_and_the_candidate_verified(operator,monkeypatch):
    v,config,receiver,cloud,engine,_=operator
    holder=receiver_held(receiver);slept=[]
    def sleep(seconds):
        assert not (Path(config['directory'])/'active.json').exists() and not engine.resources
        slept.append(seconds)
        if sum(slept)>=40:holder.close()
    monkeypatch.setattr(v.time,'sleep',sleep)
    assert v.run(config,client=cloud,engine=engine)['status']=='verified'
    assert slept==[15,15,15] and len(engine.runs)==1

# spec: ANYM-632
def test_a_receiver_lock_busy_past_the_wait_ends_busy_exit_0_and_keeps_the_last_failure(operator,monkeypatch,capsys):
    import time
    v,config,receiver,cloud,engine,write_json=operator
    directory=Path(config['directory']);write_json(directory/'last-failure.json',{'v':1,'status':'failed','error':'synthetic'})
    before=(directory/'last-failure.json').read_bytes();real_sleep=time.sleep;slept=[]
    monkeypatch.setattr(v.time,'sleep',lambda seconds:(slept.append(seconds),real_sleep(min(seconds,0.05))))
    config={**config,'busy_wait_seconds':1};busy_main(v,config,cloud,engine,monkeypatch)
    holder=receiver_held(receiver)
    assert v.main(['--config','/synthetic'])==0
    holder.close()
    status=json.loads((directory/'status.json').read_bytes())
    assert {k:status[k] for k in ('v','status','code')}=={'v':1,'status':'busy','code':'receiver_busy'} and set(status)=={'v','status','code','checked_at'}
    out=capsys.readouterr().out.splitlines()
    assert len(out)==1 and {k:json.loads(out[0])[k] for k in ('status','code')}=={'status':'busy','code':'receiver_busy'}
    assert (directory/'last-failure.json').read_bytes()==before and not (directory/'active.json').exists()
    assert slept and all(s<=15 for s in slept) and not engine.runs and not engine.resources

# spec: ANYM-632
def test_verifier_busy_leaves_the_holders_own_status_and_exits_0(operator,monkeypatch,capsys):
    v,config,receiver,cloud,engine,write_json=operator
    directory=Path(config['directory']);busy_main(v,config,cloud,engine,monkeypatch)
    with v.verifier_lock(config['directory']):
        write_json(directory/'status.json',{'v':1,'status':'verified','backup_id':'holder','verification_id':'1'*32,'verified_at':'synthetic'})
        before=(directory/'status.json').read_bytes()
        assert v.main(['--config','/synthetic'])==0
        assert (directory/'status.json').read_bytes()==before
    out=capsys.readouterr().out.splitlines()
    assert len(out)==1 and {k:json.loads(out[0])[k] for k in ('status','code')}=={'status':'busy','code':'verifier_busy'}
    assert not (directory/'last-failure.json').exists() and not engine.runs

# spec: ANYM-632
def test_a_receiver_busy_answer_during_promotion_still_ends_failed(operator,monkeypatch):
    v,config,receiver,cloud,engine,_=operator
    original=r2.protect
    def protect(*args,**kw):
        with r2.local.locked(receiver['state_directory']):return original(*args,**kw)
    monkeypatch.setattr(r2,'protect',protect)
    monkeypatch.setattr(v.time,'sleep',lambda seconds:pytest.fail('promotion must not wait'))
    with pytest.raises(RuntimeError,match='receiver_busy'):v.run(config,client=cloud,engine=engine)
    directory=Path(config['directory'])
    for name in ('status.json','last-failure.json'):
        value=json.loads((directory/name).read_bytes());assert value['status']=='failed' and value['error']=='receiver_busy'
    assert (directory/'active.json').exists()

# spec: ANYM-632
def test_a_retry_in_progress_meeting_the_receiver_lock_keeps_the_failure_path(operator,monkeypatch):
    v,config,receiver,cloud,engine,_=operator
    engine.failure=RuntimeError('restore_quarantine_failed')
    with pytest.raises(RuntimeError):v.run(config,client=cloud,engine=engine)
    monkeypatch.setattr(v.time,'sleep',lambda seconds:pytest.fail('a retry must not wait'))
    holder=receiver_held(receiver)
    with pytest.raises(RuntimeError,match='receiver_busy'):v.run(config,client=cloud,engine=engine)
    holder.close()
    assert json.loads((Path(config['directory'])/'status.json').read_bytes())['error']=='receiver_busy'

# spec: ANYM-632
def test_the_restore_deadline_pauses_while_the_run_waits_for_the_receiver_lock(operator,monkeypatch):
    import time
    v,config,receiver,cloud,engine,_=operator
    holder=receiver_held(receiver);real_sleep=time.sleep;waited=[]
    def sleep(seconds):
        # The upload holds the lock for longer than this run's whole (real SIGALRM) deadline.
        real_sleep(0.25);waited.append(0.25)
        if sum(waited)>=1.5:holder.close()
    monkeypatch.setattr(v.time,'sleep',sleep)
    assert v.run({**config,'timeout_seconds':1},client=cloud,engine=engine)['status']=='verified' and sum(waited)>=1.5

# spec: ANYM-632
def test_an_idle_run_creates_no_docker_resource_and_writes_idle(operator):
    v,config,receiver,cloud,engine,_=operator
    assert v.run(config,client=cloud,engine=engine)['status']=='verified'
    runs=list(engine.runs);deleted=list(engine.deleted)
    assert v.run(config,client=cloud,engine=engine)['status']=='idle'
    assert engine.runs==runs and engine.deleted==deleted and not engine.resources
    status=json.loads((Path(config['directory'])/'status.json').read_bytes())
    assert status['status']=='idle' and not (Path(config['directory'])/'active.json').exists()

# spec: ANYM-632
@pytest.mark.parametrize('value,accepted',[(0,True),(300,True),(900,True),(901,False),(-1,False),(True,False),('300',False)])
def test_busy_wait_seconds_is_an_optional_bounded_integer(operator,value,accepted):
    v,config,receiver,cloud,engine,write_json=operator
    path=Path(config['receiver_config_file']).parent/'verifier.json';write_json(path,{**config,'busy_wait_seconds':value})
    if accepted:assert v.load_config(path)['busy_wait_seconds']==value
    else:
        with pytest.raises(ValueError,match='invalid_verifier_limits'):v.load_config(path)

# spec: ANYM-632
def test_both_verifier_timers_start_two_minutes_after_the_last_run():
    root=Path(__file__).resolve().parents[1]/'deploy'
    for name in ('deedkit-backup-r2-verifier.timer','deedkit-calendar-backup-r2-verifier.timer'):
        lines=(root/name).read_text().splitlines()
        assert 'OnUnitInactiveSec=2min' in lines and 'RandomizedDelaySec=30s' in lines and 'OnBootSec=10min' in lines
        assert 'AccuracySec=1s' in lines


@pytest.fixture
def datadir(instances,tmp_path,monkeypatch):
    # ANYM-634: both instances keep their scratch PostgreSQL in one local-disk root 0700; chown is recorded.
    s=instances;root=tmp_path/'pg-local';root.mkdir(mode=0o700);s.chowned=[];s.made=[]
    for config in (s.messaging,s.calendar):config['postgres_data_directory']=str(root)
    # The image's postgres user is uid 999; here the test's own uid stands in for it (removal checks the owner).
    assert s.v.POSTGRES_UID==999;monkeypatch.setattr(s.v,'POSTGRES_UID',os.getuid())
    monkeypatch.setattr(os,'chown',lambda path,uid,gid,**kw:s.chowned.append((path,uid,gid)))
    real_make=s.v.make_data_directory
    def make(path):
        # Its exact path is in the holder's record before the directory exists.
        record=json.loads((s.host/'admission.json').read_text())
        entry=['directory',str(path),record['run'],s.v.root_identity(Path(path).parent)]
        assert entry in record['resources'] and not Path(path).exists()
        s.made.append(Path(path));real_make(path);(Path(path)/'18').mkdir();(Path(path)/'18'/'PG_VERSION').write_text('18')
    monkeypatch.setattr(s.v,'make_data_directory',make)
    real_restore=s.engine.restore
    def restore(config,receiver,client,receipt,run,payload,secret_dir):
        # A bind mount needs its source: the run's directory exists, 0700, while the restore runs.
        data=s.v.data_directory(config,run);assert data is None or (data.is_dir() and data.stat().st_mode&0o777==0o700)
        return real_restore(config,receiver,client,receipt,run,payload,secret_dir)
    s.engine.restore=restore;s.root=root
    s.config=lambda profile:s.messaging if profile=='messaging' else s.calendar
    s.cloud_of=lambda profile:s.cloud if profile=='messaging' else s.calendar_cloud
    return s


# spec: ANYM-634
@pytest.mark.parametrize('outcome',['success','failure'])
@pytest.mark.parametrize('profile',['messaging','calendar'])
def test_a_runs_data_directory_is_recorded_before_it_exists_and_removed_on_success_and_failure(datadir,profile,outcome):
    s=datadir;v=s.v;config=s.config(profile)
    if outcome=='failure':
        s.engine.failure=RuntimeError('restore_docker_failed')
        with pytest.raises(RuntimeError,match='restore_docker_failed'):v.run(config,client=s.cloud_of(profile),engine=s.engine)
        run=json.loads((Path(config['directory'])/'active.json').read_text())['run']
        assert s.status(config)['cleanup_complete'] is True
    else:
        assert v.run(config,client=s.cloud_of(profile),engine=s.engine)['status']=='verified'
        run=json.loads((Path(config['directory'])/'last-success.json').read_text())['verification_id']
    assert s.made==[s.root/v.names(run,profile)['volume']] and s.chowned==[(s.made[0].name,os.getuid(),os.getuid())]
    assert s.engine.admissions[-1]['resources'][-1]==['directory',str(s.made[0]),run,v.root_identity(s.root)]
    # The bind replaces the volume: the record still names it, for a retry from before ANYM-634.
    assert ['volume',v.names(run,profile)['volume'],run] in s.engine.admissions[-1]['resources']
    assert list(s.root.iterdir())==[] and not s.engine.resources and not (s.host/'admission.json').exists()


def kill_holder_with_data(s,profile,run='f'*32,data=None):
    """A coordinator SIGKILLed mid-restore with its data directory on the local disk (ANYM-634)."""
    data=data or s.root/s.v.names(run,profile)['volume']
    with s.v.host_lock(s.messaging) as fd:
        s.v.admit(s.engine,fd,profile,run,s.v.run_resources(run,profile,data))
    for kind,name,owner in s.v.run_resources(run,profile):s.engine.resources[(kind,name)]=owner
    return data


# spec: ANYM-634
@pytest.mark.parametrize('profile',['messaging','calendar'])
def test_a_sigkilled_holders_data_directory_is_removed_by_the_next_holder(datadir,profile):
    s=datadir;v=s.v;data=kill_holder_with_data(s,profile)
    (data/'18'/'docker').mkdir(parents=True);(data/'18'/'docker'/'PG_VERSION').write_text('18')
    other='calendar' if profile=='messaging' else 'messaging'
    assert v.run(s.config(other),client=s.cloud_of(other),engine=s.engine)['status']=='verified'
    assert not data.exists() and list(s.root.iterdir())==[] and not s.engine.resources
    assert not (s.host/'admission.json').exists()


# spec: ANYM-634
@pytest.mark.parametrize('case',['unremovable','symlink','foreign_name','unrelated_parent','public_parent','other_run',
                                  'record_run','other_owner'])
def test_a_recorded_data_directory_that_cannot_be_removed_refuses_with_nothing_created(datadir,monkeypatch,tmp_path,case):
    s=datadir;v=s.v;outside=tmp_path/'outside';outside.mkdir();(outside/'keep').write_text('kept')
    if case in ('foreign_name','other_run','record_run','unrelated_parent'):
        # A record naming a path that is not its run's directory under its admitted root is never acted on.
        data=kill_holder_with_data(s,'messaging')
        record=json.loads((s.host/'admission.json').read_text());entry=record['resources'][-1]
        if case=='foreign_name':entry[1]=str(outside)
        elif case=='unrelated_parent':
            # The run's exact name under another root-private directory: not the root it was admitted under.
            outside.chmod(0o700);data=outside/data.name;data.mkdir();(data/'keep').write_text('kept');entry[1]=str(data)
        else:
            # The name of another run, also as that run's own entry inside this run's record.
            data=outside/v.names('e'*32,'messaging')['volume'];data.mkdir();(data/'keep').write_text('kept')
            entry[1]=str(data);entry[3]=v.root_identity(outside)
            if case=='record_run':entry[2]='e'*32
        s.write_json(s.host/'admission.json',record)
    elif case=='public_parent':
        # The run's exact name under a parent that is not root-private: not a data root, never removed.
        outside.chmod(0o755);data=kill_holder_with_data(s,'messaging',data=outside/v.names('f'*32,'messaging')['volume'])
        data.mkdir();(data/'keep').write_text('kept')
    elif case=='other_owner':
        # The recorded directory was replaced by one of another user.
        data=kill_holder_with_data(s,'messaging');data.mkdir();(data/'keep').write_text('kept');real_stat=os.stat
        def stat_(path,*a,**kw):
            info=real_stat(path,*a,**kw)
            if str(path)==data.name and kw.get('dir_fd') is not None:values=list(info);values[4]=1234;return os.stat_result(values)
            return info
        monkeypatch.setattr(os,'stat',stat_)
    else:
        data=kill_holder_with_data(s,'messaging')
        if case=='symlink':data.symlink_to(outside,target_is_directory=True)
        else:
            data.mkdir()
            real=v.shutil.rmtree
            def rmtree(path,*a,**kw):
                if str(path)==data.name:raise PermissionError('busy')
                return real(path,*a,**kw)
            monkeypatch.setattr(v.shutil,'rmtree',rmtree)
    record=(s.host/'admission.json').read_bytes()
    with pytest.raises(RuntimeError,match='verifier_cleanup_unconfirmed'):
        v.run(s.calendar,client=s.calendar_cloud,engine=s.engine)
    assert created(s.engine,'calendar')==[] and s.made==[] and (s.host/'admission.json').read_bytes()==record
    assert (outside/'keep').read_text()=='kept' and s.status(s.calendar)['error']=='verifier_cleanup_unconfirmed'
    if case in ('unrelated_parent','public_parent','other_owner','other_run','record_run'):assert (data/'keep').read_text()=='kept'


def separate_filesystem(monkeypatch,root):
    """The data directory's root reads as another filesystem than the working directory's."""
    real=os.stat
    def fake(path,*a,**kw):
        info=real(path,*a,**kw)
        if not kw.get('dir_fd') and not a and Path(path)==root:values=list(info);values[2]+=1;return os.stat_result(values)
        return info
    monkeypatch.setattr(os,'stat',fake)


# spec: ANYM-634
@pytest.mark.parametrize('low',['work','data','shared'])
def test_low_space_on_either_filesystem_refuses_before_anything_is_created(datadir,monkeypatch,low):
    s=datadir;v=s.v;config=s.messaging
    database=config['postgres_headroom_bytes']+config['reserve_bytes']
    if low!='shared':separate_filesystem(monkeypatch,s.root)
    def free(path):
        if low=='data' and Path(path)==s.root:return SimpleNamespace(free=database-1)
        if low=='work' and Path(path)==Path(config['directory']):return SimpleNamespace(free=config['reserve_bytes'])
        # One filesystem holds both shares: room for the database share alone is not enough.
        if low=='shared':return SimpleNamespace(free=database)
        return SimpleNamespace(free=100*v.GIB)
    monkeypatch.setattr(v.shutil,'disk_usage',free)
    with pytest.raises(ValueError,match='restore_disk_capacity'):v.run(config,client=s.cloud,engine=s.engine)
    assert not s.engine.log and s.made==[] and list(s.root.iterdir())==[] and not (s.host/'admission.json').exists()


# spec: ANYM-634
def test_the_reserve_monitor_watches_the_data_directorys_filesystem(tmp_path,monkeypatch):
    v=module();config={'directory':str(tmp_path/'work'),'postgres_data_directory':str(tmp_path/'pg')}
    roots=v.reserve_roots(config,'/docker');assert roots==[str(tmp_path/'work'),'/docker',str(tmp_path/'pg')]
    assert v.reserve_roots({'directory':'/work'},'/docker')==['/work','/docker']
    engine=v.Docker();engine.disk_roots=roots;engine.reserve_bytes=100
    monkeypatch.setattr(v.shutil,'disk_usage',lambda path:SimpleNamespace(free=99 if path==str(tmp_path/'pg') else 1000))
    with pytest.raises(ValueError,match='restore_disk_capacity'):engine.check_reserve()


# spec: ANYM-634
@pytest.mark.parametrize('attack',['symlink','group_writable','non_root','not_private','relative','mount_option',
                                   'in_work','holds_work','in_state','in_host','null','alias_work','alias_host'])
def test_an_unsafe_postgres_data_directory_is_refused(operator,monkeypatch,tmp_path,attack):
    v,config,receiver,cloud,engine,write_json=operator
    data=tmp_path/'pg-local';data.mkdir(mode=0o700);value=str(data)
    if attack=='symlink':
        real=tmp_path/'pg-real';real.mkdir(mode=0o700);data.rmdir();data.symlink_to(real,target_is_directory=True)
    elif attack=='group_writable':data.chmod(0o770)
    elif attack=='not_private':data.chmod(0o750)
    elif attack=='non_root':
        inode=data.stat().st_ino;patched=os.fstat
        def fstat(fd):
            info=patched(fd)
            if info.st_ino!=inode:return info
            values=list(info);values[4]=1000;return os.stat_result(values)
        monkeypatch.setattr(os,'fstat',fstat)
    elif attack=='relative':value='pg-local'
    elif attack=='mount_option':value=str(data)+',readonly'
    elif attack=='in_work':value=str(Path(config['directory'])/'pg')
    elif attack=='holds_work':value=str(tmp_path)
    elif attack=='in_state':value=str(Path(receiver['state_directory'])/'pg')
    elif attack=='in_host':value=config['host_directory']+'/pg'
    elif attack=='null':value=None
    elif attack=='alias_work':value='/'+config['directory']  # `//x` is `/x` on Linux
    elif attack=='alias_host':config={**config,'host_directory':'/'+str(data)};value=str(data/'pg')
    path=Path(config['receiver_config_file']).parent/'verifier-config.json'
    write_json(path,{**config,'postgres_data_directory':value})
    codes={'group_writable':'untrusted_operator_ancestor','non_root':'untrusted_operator_ancestor',
           'not_private':'private_verifier_directory_required','relative':'invalid_verifier_path','alias_work':'invalid_verifier_path',
           'mount_option':'invalid_verifier_path','null':'invalid_verifier_path'}
    if attack=='symlink':
        with pytest.raises(OSError):v.load_config(path)  # O_NOFOLLOW: never followed
    else:
        with pytest.raises(ValueError,match=codes.get(attack,'separate_operator_evidence_required')):v.load_config(path)
    write_json(path,{**config,'postgres_data_directory':str(tmp_path/'pg-ok')});(tmp_path/'pg-ok').mkdir(mode=0o700)
    if attack!='non_root':assert v.load_config(path)['postgres_data_directory']==str(tmp_path/'pg-ok')


# spec: ANYM-634
@pytest.mark.parametrize('profile',['messaging','calendar'])
def test_an_older_verifiers_journal_and_admission_record_are_retried_and_cleaned(datadir,profile):
    s=datadir;v=s.v;config=s.config(profile);old={k:val for k,val in config.items() if k!='postgres_data_directory'}
    # The older verifier: a Docker volume, a journal and a record without any data directory.
    s.engine.failure=RuntimeError('restore_docker_failed')
    with pytest.raises(RuntimeError):v.run(old,client=s.cloud_of(profile),engine=s.engine)
    run=json.loads((Path(config['directory'])/'active.json').read_text())['run']
    with v.host_lock(old) as fd:v.admit(s.engine,fd,profile,run,v.run_resources(run,profile))
    for kind,name,owner in v.run_resources(run,profile):s.engine.resources[(kind,name)]=owner
    assert all(item[0]!='directory' for item in json.loads((s.host/'admission.json').read_text())['resources'])
    s.engine.failure=None;log=len(s.engine.log)
    assert v.run(config,client=s.cloud_of(profile),engine=s.engine)['status']=='verified'
    assert ('remove',v.names(run,profile)['volume']) in s.engine.log[log:]
    assert s.made==[s.root/v.names(run,profile)['volume']] and list(s.root.iterdir())==[]
    assert not s.engine.resources and not (s.host/'admission.json').exists()
    assert not (Path(config['directory'])/'active.json').exists()


# spec: ANYM-634
@pytest.mark.parametrize('local_disk',[True,False],ids=['data_directory','volume'])
def test_the_scratch_postgres_gets_restore_memory_and_its_storage(tmp_path,monkeypatch,local_disk):
    v=module();engine=v.Docker();calls=[];run='a'*32
    monkeypatch.setattr(engine,'command',lambda *args:calls.append(args) or b'')
    config={'postgres_image':v.POSTGRES_IMAGE,'helper_image':'sha256:'+'a'*64,'identity_file':'/private/key',
            **({'postgres_data_directory':'/srv/pg-local'} if local_disk else {})}
    engine.setup(config,run,tmp_path,tmp_path)
    postgres=next(call for call in calls if call[:2]==('container','create') and v.POSTGRES_IMAGE in call)
    settings=postgres[postgres.index(v.POSTGRES_IMAGE)+1:]
    assert {settings[i+1] for i,value in enumerate(settings[:-1]) if value=='-c'}>={
        'shared_buffers=256MB','maintenance_work_mem=256MB','max_parallel_workers=0'}
    assert '--cpus=1' in postgres and '--memory=1g' in postgres and '--memory-swap=1g' in postgres
    mount=postgres[postgres.index('--mount')+1];volume=v.names(run)['volume']
    if local_disk:
        assert mount=='type=bind,src=/srv/pg-local/'+volume+',dst=/var/lib/postgresql'
        assert not any(call[:2]==('volume','create') for call in calls)
    else:
        assert mount=='type=volume,src='+volume+',dst=/var/lib/postgresql' and ('volume','create') in [c[:2] for c in calls]


# spec: ANYM-634
def test_follow_self_test_keeps_a_docker_volume_with_a_data_directory_configured(self_test_setup):
    v,f,directory,config=self_test_setup;seen=[]
    class Engine(SelfTestEngine):
        def setup(self,tested,run,payload,secret_dir):seen.append(tested);super().setup(tested,run,payload,secret_dir)
        def restore(self,tested,*args):seen.append(tested);return super().restore(tested,*args)
    engine=Engine(v,f,{**config,'postgres_data_directory':'/srv/pg-local'})
    assert f.self_test(engine,{**config,'postgres_data_directory':'/srv/pg-local'})==([2,50],50)
    assert len(seen)==2 and all('postgres_data_directory' not in tested for tested in seen)


class Killed(BaseException):
    """A SIGKILL: no except clause of the run handles it."""


# spec: ANYM-634
@pytest.mark.parametrize('change',['removed','moved'])
@pytest.mark.parametrize('profile',['messaging','calendar'])
def test_a_retry_after_its_proof_removes_the_recorded_data_directory_after_the_key_changed(datadir,monkeypatch,tmp_path,profile,change):
    s=datadir;v=s.v;config=s.config(profile);real=v.cleanup_resources;calls=[]
    def killed_after_proof(*args,**kw):
        calls.append(args)
        if len(calls)>=2:raise Killed()  # also the failure path's cleanup: the process is gone
        return real(*args,**kw)
    monkeypatch.setattr(v,'cleanup_resources',killed_after_proof)
    with pytest.raises(Killed):v.run(config,client=s.cloud_of(profile),engine=s.engine)
    journal=json.loads((Path(config['directory'])/'active.json').read_text())
    data=s.root/v.names(journal['run'],profile)['volume']
    assert journal['phase']=='proof_ready' and data.is_dir() and (s.host/'admission.json').exists()
    monkeypatch.setattr(v,'cleanup_resources',real)
    if change=='removed':del config['postgres_data_directory']
    else:other=tmp_path/'pg-other';other.mkdir(mode=0o700);config['postgres_data_directory']=str(other)
    assert v.run(config,client=s.cloud_of(profile),engine=s.engine)['status']=='verified'
    assert not data.exists() and list(s.root.iterdir())==[] and not (s.host/'admission.json').exists()
    assert not s.engine.resources and len(s.made)==1


# spec: ANYM-634
@pytest.mark.parametrize('phase',['proof_ready','promoted'])
def test_a_retry_after_its_proof_with_no_record_left_never_waits_for_the_host_lock(datadir,monkeypatch,phase):
    s=datadir;v=s.v;config={**s.messaging,'verify_wait_seconds':1};original=r2.protect
    def lost_response(*args,**kwargs):original(*args,**kwargs);raise TimeoutError('lost promotion result')
    monkeypatch.setattr(r2,'protect',lost_response)
    with pytest.raises(TimeoutError):v.run(config,client=s.cloud,engine=s.engine)
    monkeypatch.setattr(r2,'protect',original)
    active=Path(config['directory'])/'active.json';journal=json.loads(active.read_text())
    assert journal['phase']=='proof_ready' and not (s.host/'admission.json').exists()
    if phase=='promoted':s.write_json(active,{**journal,'phase':'promoted'})
    # The calendar instance restores meanwhile: this retry promotes without waiting for it.
    monkeypatch.setattr(v.time,'sleep',lambda seconds:pytest.fail('the retry waited for the host lock'))
    with v.host_lock(s.calendar):
        assert v.run(config,client=s.cloud,engine=s.engine)['status']=='verified'
    assert not active.exists() and list(s.root.iterdir())==[]


# spec: ANYM-634
@pytest.mark.parametrize('phase',['proof_ready','promoted'])
def test_a_retry_after_its_proof_meeting_an_invalid_record_keeps_it_and_the_directory(datadir,monkeypatch,phase):
    s=datadir;v=s.v;config=s.messaging;real=v.cleanup_resources;calls=[]
    def killed_after_proof(*args,**kw):
        calls.append(args)
        if len(calls)>=2:raise Killed()
        return real(*args,**kw)
    monkeypatch.setattr(v,'cleanup_resources',killed_after_proof)
    with pytest.raises(Killed):v.run(config,client=s.cloud,engine=s.engine)
    monkeypatch.setattr(v,'cleanup_resources',real)
    active=Path(config['directory'])/'active.json';journal=json.loads(active.read_text())
    data=s.root/v.names(journal['run'],'messaging')['volume']
    if phase=='promoted':s.write_json(active,{**journal,'phase':'promoted'})
    # The record names another run on top while its entries still name this run's resources: invalid.
    record=json.loads((s.host/'admission.json').read_text());s.write_json(s.host/'admission.json',{**record,'run':'e'*32})
    kept=(s.host/'admission.json').read_bytes();del config['postgres_data_directory']
    with pytest.raises(RuntimeError,match='verifier_cleanup_unconfirmed'):v.run(config,client=s.cloud,engine=s.engine)
    assert data.is_dir() and (s.host/'admission.json').read_bytes()==kept and active.exists()
