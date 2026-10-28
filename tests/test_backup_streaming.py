"""Bounded transport tests: fixture age headers are not a crypto assertion."""
import hashlib
import io
import json
import struct
import sys
import asyncio
import os
import threading
import time

import pytest

from messaging_service.backup_receiver import AGE_HEADER, receive
from messaging_service.backup_receiver import prune
from messaging_service.backup import create_snapshot, verify_bundle
from messaging_service.backup_crypto import encrypt_snapshot_stream, decrypt_snapshot, snapshot_stream_bound
from messaging_service.backup_runner import upload_stream
from messaging_service.backup_runner import Runner,read_json,write_json
from test_backup_operations import FakeDocker,runner_fixture

ID = '20261014T120000Z-0123456789abcdef'
SNAPSHOT = 'a' * 64


def streaming_packet(data=None, *, footer=None, **overrides):
    data = data or AGE_HEADER + b'ciphertext fixture' * 100
    header = {'v': 2, 'id': ID, 'max_bytes': len(data) + 1024,
              'snapshot_sha256': SNAPSHOT, **overrides}
    tail = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    tail.update(footer or {})
    raw = json.dumps(header).encode() + b'\n' + struct.pack('!I', len(data)) + data
    raw += struct.pack('!I', 0) + json.dumps(tail).encode() + b'\n'
    return io.BytesIO(raw), data, tail


def test_stream_receiver_commits_verified_ciphertext_and_retries_after_lost_receipt(tmp_path):
    replies = []
    stream, data, tail = streaming_packet()
    result = receive(tmp_path, stream, reserve_bytes=0, respond=replies.append)
    assert replies == [{'v': 2, 'status': 'ready', 'id': ID, 'snapshot_sha256': SNAPSHOT}]
    assert result['status'] == 'stored' and result['sha256'] == tail['sha256']
    target = tmp_path / (ID + '.tar.age')
    assert target.read_bytes() == data and not list(tmp_path.glob('.incoming-*'))
    # A retry needs only the same logical snapshot, never replaying/recreating
    # random age ciphertext or downloading an existing backup.
    header = {'v': 2, 'id': ID, 'max_bytes': len(data) + 1024, 'snapshot_sha256': SNAPSHOT}
    retry = receive(tmp_path, io.BytesIO(json.dumps(header).encode() + b'\n'), reserve_bytes=0)
    assert retry['duplicate'] and retry['sha256'] == result['sha256']
    with pytest.raises(ValueError, match='backup_id_conflict'):
        receive(tmp_path, streaming_packet(snapshot_sha256='b' * 64)[0], reserve_bytes=0)
    assert target.read_bytes() == data


@pytest.mark.parametrize('attack', ['hash', 'length', 'truncated', 'extra', 'bound', 'plaintext', 'frame'])
def test_stream_receiver_never_publishes_failed_or_incomplete_stream(tmp_path, attack):
    stream, data, _ = streaming_packet()
    if attack == 'hash': stream = streaming_packet(footer={'sha256': '0' * 64})[0]
    elif attack == 'length': stream = streaming_packet(footer={'bytes': len(data) + 1})[0]
    elif attack == 'truncated': stream = io.BytesIO(stream.getvalue()[:-20])
    elif attack == 'extra': stream = io.BytesIO(stream.getvalue() + b'X')
    elif attack == 'bound': stream = streaming_packet(max_bytes=len(data) - 1)[0]
    elif attack == 'plaintext': stream = streaming_packet(data=b'not encrypted' * 100)[0]
    else:
        header = {'v': 2, 'id': ID, 'max_bytes': 10 * 1024**2, 'snapshot_sha256': SNAPSHOT}
        stream = io.BytesIO(json.dumps(header).encode() + b'\n' + struct.pack('!I', 2 * 1024**2))
    old = tmp_path / '20261013T000000Z-0123456789abcdef.tar.age'
    old.write_bytes(AGE_HEADER + b'previous verified fixture' * 100)
    before = old.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        receive(tmp_path, stream, reserve_bytes=0)
    assert old.read_bytes() == before
    assert not (tmp_path / (ID + '.tar.age')).exists()
    assert not list(tmp_path.glob('.incoming-*'))


def test_stream_receiver_preflights_maximum_without_deleting_old_backup(tmp_path):
    old = tmp_path / '20261013T000000Z-0123456789abcdef.tar.age'
    old.write_bytes(AGE_HEADER + b'previous' * 100)
    with pytest.raises(RuntimeError, match='receiver_quota'):
        receive(tmp_path, streaming_packet()[0], quota_bytes=old.stat().st_size + 100, reserve_bytes=0)
    assert old.exists() and not list(tmp_path.glob('.incoming-*'))


def test_reference_capture_avoids_copy_and_stream_restores_ordinary_bundle(seeded,tmp_path):
    store,*_=seeded
    directory=tmp_path/'capture'
    create_snapshot(store,directory,[sys.executable,'-c','print("UNIT_DUMP")'],reference_blobs=True)
    manifest=verify_bundle(directory,blob_root=store.files)
    assert manifest['blobs'] and list((directory/'files').iterdir())==[]
    with pytest.raises(RuntimeError,match='blob_corrupt'):verify_bundle(directory)
    age=tmp_path/'age'
    age.write_text('#!'+sys.executable+'\nimport sys,shutil\n'
                   'source=open(sys.argv[-1],"rb") if "-d" in sys.argv else sys.stdin.buffer\n'
                   'shutil.copyfileobj(source,sys.stdout.buffer)\n');age.chmod(0o700)
    ciphertext=tmp_path/'test-stream'
    with ciphertext.open('wb') as out:
        result=encrypt_snapshot_stream(directory,out,'fixture',blob_root=store.files,age=str(age))
    assert result['bytes']==ciphertext.stat().st_size<=snapshot_stream_bound(directory,store.files)
    assert result['sha256']==hashlib.sha256(ciphertext.read_bytes()).hexdigest()
    decrypt_snapshot(ciphertext,'fixture',tmp_path/'restore',age=str(age))
    assert verify_bundle(tmp_path/'restore')==manifest
    assert list((directory/'files').iterdir())==[]
    assert all((store.files/item['digest']).exists() for item in manifest['blobs'])


# Changed assertions (name kept as the Tiden identity): adds the worker_exited and
# late_writer modes and passes the stopped probe, which every mode now receives.
@pytest.mark.parametrize('mode',['ok','encoder_failed','timeout','duplicate','quota','worker_exited','late_writer'])
def test_host_streams_fifo_without_ciphertext_staging_and_requires_encoder_success(tmp_path,monkeypatch,mode):
    ssh=tmp_path/'ssh';pid=tmp_path/'ssh.pid'
    script='''import hashlib,json,os,struct,sys,time
open(PID,'w').write(str(os.getpid()))
assert 'SSH_AUTH_SOCK' not in os.environ
header=json.loads(sys.stdin.buffer.readline())
if MODE=='timeout':time.sleep(60)
if MODE=='quota':print(json.dumps({'status':'error','code':'receiver_quota'}),flush=True);sys.exit(1)
response={'v':2,'id':header['id'],'snapshot_sha256':header['snapshot_sha256']}
if MODE=='duplicate':
    print(json.dumps({**response,'status':'stored','bytes':len(DATA),'sha256':hashlib.sha256(DATA).hexdigest(),'duplicate':True}),flush=True)
    sys.exit(0)
print(json.dumps({**response,'status':'ready'}),flush=True)
data=b''
while True:
    raw=sys.stdin.buffer.read(4)
    if len(raw)!=4:sys.exit(1)
    length=struct.unpack('!I',raw)[0]
    if not length:break
    data+=sys.stdin.buffer.read(length)
footer=json.loads(sys.stdin.buffer.readline())
assert footer['bytes']==len(data) and footer['sha256']==hashlib.sha256(data).hexdigest()
assert sys.stdin.buffer.read()==b''
print(json.dumps({**response,**footer,'status':'stored'}),flush=True)
'''
    # A late writer must fit the pipe buffer: it writes during a probe, before reads.
    data=AGE_HEADER+b'fixture payload'*(100 if mode=='late_writer' else 10000)
    ssh.write_text('#!'+sys.executable+'\nPID='+repr(str(pid))+'\nMODE='+repr(mode)+'\nDATA='+repr(data)+'\n'+script)
    ssh.chmod(0o700);monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    monkeypatch.setenv('SSH_AUTH_SOCK','never-forward')
    fifo=tmp_path/'ciphertext.fifo';os.mkfifo(fifo,0o600)
    threads=[];actions=[];probes=[]
    def start(encode):
        actions.append(encode)
        if encode and mode not in ('worker_exited','late_writer'):
            def writer():
                with fifo.open('wb',buffering=0) as out:out.write(data)
            thread=threading.Thread(target=writer);thread.start();threads.append(thread)
    def stopped():
        probes.append(mode)
        if mode=='late_writer' and len(probes)==1:
            # The worker wrote everything and exited between the host's first
            # EOF read and this probe; its bytes must still be delivered.
            with fifo.open('wb',buffering=0) as out:out.write(data)
        return mode in ('worker_exited','late_writer')
    async def completed():
        if mode=='encoder_failed':raise RuntimeError('backup_encryption_failed')
        if mode=='worker_exited':raise RuntimeError('backup_capture_failed')
        return {'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    metadata={'v':2,'id':ID,'max_bytes':len(data)+1024,'snapshot_sha256':SNAPSHOT}
    transfer={'host':'fixture.invalid','user':'backup','identity_file':'unused','known_hosts_file':'unused'}
    if mode in ('ok','duplicate','late_writer'):
        receipt=asyncio.run(upload_stream(fifo,metadata,transfer,5,start,completed,stopped))
        assert receipt['status']=='stored' and actions==[mode!='duplicate']
        if mode=='late_writer':assert receipt['bytes']==len(data)
    else:
        # A worker that failed before opening the FIFO ends the wait promptly,
        # not after the whole (up to two-hour) stream deadline.
        began=time.monotonic();deadline={'timeout':.2,'worker_exited':30}.get(mode,5)
        with pytest.raises(asyncio.TimeoutError if mode=='timeout' else RuntimeError):
            asyncio.run(upload_stream(fifo,metadata,transfer,deadline,start,completed,stopped))
        if mode=='worker_exited':assert probes and time.monotonic()-began<10
    for thread in threads:thread.join(timeout=2);assert not thread.is_alive()
    assert not (tmp_path/'bundle.tar.age').exists()
    if pid.exists():
        with pytest.raises(ProcessLookupError):os.kill(int(pid.read_text()),0)


class StreamingDocker(FakeDocker):
    def call(self,*args):
        if args[0]=='start' and args[1].startswith('pos-backup-'):
            info=self.containers[args[1]];info['State'].update(Running=True,Status='running')
            work=self.root/info['Config']['Labels']['personal-os.backup.id']
            if not (work/'snapshot').exists():
                (work/'snapshot').mkdir();(work/'snapshot/private').write_bytes(b'PRIVATE_CAPTURE_FIXTURE')
            write_json(work/'captured.json',{'id':work.name,'status':'capture_complete'})
            write_json(work/'stream.json',{'v':2,'id':work.name,'snapshot_sha256':SNAPSHOT,'max_bytes':4096,
                                        'captured_at':'2026-10-14T00:00:00+00:00'})
            self.calls.append(args);return ''
        return super().call(*args)
    def inspect(self,cid):
        if not cid.startswith('pos-backup-'):return super().inspect(cid)
        import copy
        info=self.containers.get(cid)
        if info and info['State']['Running']:
            work=self.root/info['Config']['Labels']['personal-os.backup.id']
            if (work/'stream-start.json').exists():
                if read_json(work/'stream-start.json')['action']=='start':
                    data=AGE_HEADER+b'fixture'*100
                    write_json(work/'result.json',{'v':2,'id':work.name,'snapshot_sha256':SNAPSHOT,'status':'encrypted',
                                                 'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
                info['State'].update(Running=False,Status='exited',ExitCode=0)
        return copy.deepcopy(info)


def streaming_runner(tmp_path,send):
    baseline,_=runner_fixture(tmp_path)
    cfg={**baseline.cfg,'delivery_mode':'stream_v2'}
    docker=StreamingDocker(tmp_path)
    return Runner(cfg,docker=docker,sleep=lambda _:None,stream_send=send),docker


def test_stream_runner_retries_consistent_capture_without_stopping_collectors_or_recopying(tmp_path):
    attempts=[]
    async def send(path,metadata,transfer,timeout,start,completed,stopped=None):
        attempts.append(metadata.copy())
        if len(attempts)==1:raise RuntimeError('receiver_quota')
        start(True);result=await completed()
        return {**result,'status':'stored','duplicate':False}
    runner,docker=streaming_runner(tmp_path,send)
    with pytest.raises(RuntimeError,match='receiver_quota'):runner.run()
    journal=read_json(tmp_path/'journal.json')
    assert journal['phase']=='stream_upload' and not journal['stopped']
    assert docker.containers['tg-id']['State']['Running']
    runner.recover_stop()
    assert (tmp_path/journal['id']/'snapshot/private').read_bytes()==b'PRIVATE_CAPTURE_FIXTURE'
    assert not (tmp_path/journal['id']/'bundle.tar.age').exists()
    runner.run()
    assert attempts[0]==attempts[1]
    assert sum(call==('stop','--time','25','tg-id') for call in docker.calls)==1
    assert not (tmp_path/'journal.json').exists()
    assert read_json(tmp_path/'status.json')['state']=='ok'
    assert not any(path.name=='snapshot' for path in tmp_path.rglob('*'))


@pytest.mark.parametrize('outcome',['result','exited'])
def test_stream_runner_reports_finished_or_failed_worker_to_the_stream_wait(tmp_path,outcome):
    observed=[]
    async def send(path,metadata,transfer,timeout,start,completed,stopped=None):
        observed.append(stopped())  # Worker is running and waiting for admission.
        if outcome=='exited':
            docker.containers[runner.journal['container']]['State'].update(Running=False,Status='exited',ExitCode=1)
        start(True)
        observed.append(stopped())
        result=await completed()
        return {**result,'status':'stored','duplicate':False}
    runner,docker=streaming_runner(tmp_path,send)
    if outcome=='result':
        runner.run()
        assert read_json(tmp_path/'status.json')['state']=='ok'
    else:
        with pytest.raises(RuntimeError,match='backup_capture_failed'):runner.run()
    assert observed==[False,True]


def test_stream_runner_lost_receipt_uses_duplicate_without_reencryption(tmp_path):
    async def send(path,metadata,transfer,timeout,start,completed,stopped=None):
        start(False)
        return {**metadata,'status':'stored','bytes':64,'sha256':'c'*64,'duplicate':True}
    runner,docker=streaming_runner(tmp_path,send);runner.run()
    assert read_json(tmp_path/'status.json')['state']=='ok'
    assert not any(path.name=='result.json' for path in tmp_path.rglob('*'))


def test_stream_runner_recovers_crash_after_receipt_before_owned_capture_cleanup(tmp_path):
    calls=[]
    async def send(path,metadata,transfer,timeout,start,completed,stopped=None):
        calls.append(metadata['id']);start(False)
        return {**metadata,'status':'stored','bytes':64,'sha256':'c'*64,'duplicate':True}
    runner,docker=streaming_runner(tmp_path,send)
    original=runner.finish
    def crash(*_):raise RuntimeError('host_crash')
    runner.finish=crash
    with pytest.raises(RuntimeError,match='host_crash'):runner.run()
    assert len(calls)==1
    runner.finish=original;runner.run()
    assert len(calls)==1 and read_json(tmp_path/'status.json')['state']=='ok'


@pytest.mark.parametrize('already_resumed',[False,True])
def test_reboot_before_capture_commit_clears_stale_marker_and_stops_fresh_capture(tmp_path,already_resumed):
    async def send(path,metadata,transfer,timeout,start,completed,stopped=None):
        start(True);result=await completed()
        return {**result,'status':'stored'}
    runner,docker=streaming_runner(tmp_path,send);runner.prepare()
    work=tmp_path/runner.journal['id']
    runner.journal['phase']='capturing'
    if already_resumed:runner.journal['stopped']=[]
    else:docker.containers['tg-id']['State'].update(Running=False,Status='exited')
    runner.save();write_json(work/'captured.json',{'id':work.name,'status':'capture_complete'})
    pending=work/'.backup-interrupted';pending.mkdir();(pending/'database.dump').write_bytes(b'UNCOMMITTED')
    original=docker.call;seen=[]
    def checked_call(*args):
        if args[0]=='start' and args[1].startswith('pos-backup-'):
            seen.append(True)
            assert not (work/'captured.json').exists()
            assert not pending.exists()
            assert not docker.containers['tg-id']['State']['Running']
        return original(*args)
    docker.call=checked_call;runner.run()
    assert seen and docker.containers['tg-id']['State']['Running']
    assert read_json(tmp_path/'status.json')['state']=='ok'


def reference_fixture(tmp_path):
    directory=tmp_path/'capture';directory.mkdir();(directory/'files').mkdir()
    source=tmp_path/'cas';source.mkdir();data=b'SYNTHETIC ORIGINAL'*100
    digest=hashlib.sha256(data).hexdigest();(source/digest).write_bytes(data)
    dump=b'UNIT_NOT_A_DATABASE';(directory/'database.dump').write_bytes(dump)
    manifest={'format':1,'schema_version':21,'tables':{},'blobs':[{'digest':digest,'bytes':len(data)}],
              'dump_sha256':hashlib.sha256(dump).hexdigest()}
    (directory/'manifest.json').write_text(json.dumps(manifest))
    age=tmp_path/'age-stub'
    age.write_text('#!'+sys.executable+'\nimport sys,shutil\nshutil.copyfileobj(sys.stdin.buffer,sys.stdout.buffer)\n')
    age.chmod(0o700)
    return directory,source,manifest,str(age)


def test_stream_checks_original_again_during_tar_reads_after_initial_verification(tmp_path,monkeypatch):
    directory,source,manifest,age=reference_fixture(tmp_path)
    import messaging_service.backup as module
    original=module.verify_bundle
    def verified_then_corrupted(*args,**kwargs):
        result=original(*args,**kwargs)
        path=source/manifest['blobs'][0]['digest'];path.write_bytes(b'CHANGED AFTER VERIFY')
        return result
    monkeypatch.setattr(module,'verify_bundle',verified_then_corrupted)
    with pytest.raises(RuntimeError,match='backup_source_changed'):
        encrypt_snapshot_stream(directory,io.BytesIO(),'fixture',blob_root=source,age=age)


def test_stream_checks_capture_binding_before_encoding(tmp_path):
    directory,source,manifest,age=reference_fixture(tmp_path)
    with pytest.raises(RuntimeError,match='backup_source_changed'):
        encrypt_snapshot_stream(directory,io.BytesIO(),'fixture',blob_root=source,age=age,
                                expected_manifest_sha256='0'*64)


def test_receiver_crash_after_binding_before_publish_can_retry_without_replacing_old_copy(tmp_path,monkeypatch):
    original=os.link;failed=[]
    def fail_publish(source,target,*args,**kwargs):
        if str(target).endswith(ID+'.tar.age') and not failed:
            failed.append(True);raise OSError('publication interrupted')
        return original(source,target,*args,**kwargs)
    monkeypatch.setattr(os,'link',fail_publish)
    with pytest.raises(OSError,match='interrupted'):receive(tmp_path,streaming_packet()[0],reserve_bytes=0)
    assert not (tmp_path/(ID+'.tar.age')).exists()
    assert (tmp_path/(ID+'.receipt.json')).is_file()
    assert not list(tmp_path.glob('.incoming-*'))
    assert receive(tmp_path,streaming_packet()[0],reserve_bytes=0)['status']=='stored'


def test_bounded_retention_counts_verified_anchor_in_maximum_three(tmp_path,monkeypatch):
    files=[]
    for index in range(6):
        name=f'20261014T12000{index}Z-0123456789abcdef';path=tmp_path/(name+'.tar.age')
        path.write_bytes(AGE_HEADER+b'fixture'*20);os.utime(path,(index,index));files.append(path)
    anchor=files[0];marker=tmp_path/'protected.json'
    write_json(marker,[{'id':anchor.name[:-8],'bytes':anchor.stat().st_size,
                        'sha256':hashlib.sha256(anchor.read_bytes()).hexdigest()}])
    original=type(marker).stat
    def root_owned(path,*args,**kwargs):
        info=original(path,*args,**kwargs)
        if path==marker:
            values=list(info);values[4]=0;return os.stat_result(values)
        return info
    monkeypatch.setattr(type(marker),'stat',root_owned)
    result=prune(tmp_path,marker,max_copies=3)
    assert result['kept']==3 and result['removed']==3
    assert {path.name for path in tmp_path.glob('*.tar.age')}=={anchor.name,files[-1].name,files[-2].name}


def dump_timeout_config(tmp_path,**values):
    from test_coverage_backup import _runner_config
    from messaging_service import backup_runner as runner_module
    cfg={**_runner_config(tmp_path),**values}
    path=tmp_path/'runner.json';write_json(path,cfg)
    return runner_module.config(path)


# spec: ANYM-627
@pytest.mark.parametrize('capture,expected',[(60,60),(1799,1799),(1800,1800),(7200,1800)])
def test_dump_timeout_absent_from_todays_runner_json_defaults_to_1800_or_the_capture_timeout(tmp_path,capture,expected):
    from messaging_service.backup_runner import dump_timeout
    cfg=dump_timeout_config(tmp_path,timeout_seconds=capture)
    assert 'dump_timeout_seconds' not in cfg and dump_timeout(cfg)==expected


# spec: ANYM-627
@pytest.mark.parametrize('value',[299,7201,'1800',True,1801,None,1800.0])
def test_dump_timeout_outside_300_7200_or_above_the_capture_timeout_is_refused(tmp_path,value):
    with pytest.raises(ValueError,match='invalid_backup_timeout'):
        dump_timeout_config(tmp_path,timeout_seconds=1800,dump_timeout_seconds=value)


# spec: ANYM-627
def test_configured_dump_timeout_reaches_worker_json_and_the_undo_restores_the_default(tmp_path):
    from messaging_service.backup_runner import dump_timeout
    cfg=dump_timeout_config(tmp_path,timeout_seconds=7200,dump_timeout_seconds=300)
    assert dump_timeout(cfg)==300
    assert dump_timeout(dump_timeout_config(tmp_path,timeout_seconds=7200,dump_timeout_seconds=7200))==7200
    baseline,_=runner_fixture(tmp_path)
    runner=Runner({**baseline.cfg,'timeout_seconds':3600,'dump_timeout_seconds':2400},docker=FakeDocker(tmp_path),sleep=lambda _:None)
    runner.prepare();runner.create_worker(tmp_path/runner.journal['id'])
    assert read_json(tmp_path/runner.journal['id']/'worker.json')['dump_timeout_seconds']==2400
    # Undo, first step: the key removed from runner.json again is valid and gives the default.
    cfg=dump_timeout_config(tmp_path,timeout_seconds=3600)
    assert 'dump_timeout_seconds' not in cfg and dump_timeout(cfg)==1800


# spec: ANYM-627
@pytest.mark.parametrize('form',['older_runner','newer_runner','refused_bool','refused_text','refused_low','refused_high'])
def test_worker_accepts_worker_json_with_or_without_the_dump_timeout(tmp_path,monkeypatch,form):
    from messaging_service import backup_worker as worker
    work=tmp_path/'work';work.mkdir(mode=0o700)
    dsn=tmp_path/'dsn';dsn.write_text('host=db port=5432 user=owner password=synthetic dbname=synthetic')
    recovery=tmp_path/'recovery.json';worker.write_json(recovery,{'accounts':[],'sources':[]})
    cfg={'id':ID,'dsn_file':str(dsn),'files_directory':str(tmp_path/'files'),'recovery_config_file':str(recovery),
         'recipient_file':str(tmp_path/'recipient'),'work_directory':str(work),'mode':'stream_v2'}
    value={'older_runner':None,'newer_runner':2400,'refused_bool':True,'refused_text':'2400',
           'refused_low':59,'refused_high':7201}[form]
    if form!='older_runner':cfg['dump_timeout_seconds']=value
    class SyntheticStore:
        def __init__(self,*_):pass
        def close(self):pass
    calls=[]
    class Captured(Exception):pass
    def capture(*args,**kwargs):calls.append(kwargs);raise Captured
    monkeypatch.setattr(worker,'Store',SyntheticStore);monkeypatch.setattr(worker,'create_snapshot',capture)
    if form.startswith('refused'):
        with pytest.raises(ValueError,match='invalid_backup_worker_config'):worker.run(cfg)
        assert calls==[]
        return
    with pytest.raises(Captured):worker.run(cfg)
    assert calls[0].get('dump_timeout',600)==(600 if value is None else value)


# spec: ANYM-627
def test_snapshot_dumps_with_zstd_1_and_passes_the_dump_timeout_to_pg_dump(seeded,tmp_path,monkeypatch):
    import subprocess
    from messaging_service import backup
    store,*_=seeded
    timeouts=[];run=subprocess.run
    def recorded(*args,**kwargs):timeouts.append(kwargs.get('timeout'));return run(*args,**kwargs)
    monkeypatch.setattr(backup.subprocess,'run',recorded)
    argv=[sys.executable,'-c','import json,sys;print(json.dumps(sys.argv[1:]))']
    create_snapshot(store,tmp_path/'configured',argv,dump_timeout=2400)
    create_snapshot(store,tmp_path/'default',argv)
    assert timeouts==[2400,600]
    arguments=json.loads((tmp_path/'configured/database.dump').read_text())
    assert arguments[:4]==['--format=custom','--compress=zstd:1','--no-owner','--no-acl']
    assert arguments[4].startswith('--snapshot=') and len(arguments)==5


# spec: ANYM-627
def test_dump_past_its_timeout_fails_the_capture_and_releases_the_fence(seeded,tmp_path):
    import subprocess
    store,*_=seeded
    slow=[sys.executable,'-c','import time;time.sleep(30)']
    with pytest.raises(subprocess.TimeoutExpired):
        create_snapshot(store,tmp_path/'capture',slow,backup_fence=ID,dump_timeout=1)
    assert not (tmp_path/'capture').exists()
    with store.pool.connection() as c:
        assert c.execute('SELECT ended_at FROM attachment_backup_fences WHERE id=%s',(ID,)).fetchone()['ended_at'] is not None


def counting_opens(monkeypatch,root):
    """Counts opens of each file directly under root, through Path.open (io.open) and os.open."""
    import collections
    opened=collections.Counter();real_io=io.open;real_os=os.open;root=os.fspath(root)
    def record(path):
        if isinstance(path,(str,os.PathLike)) and os.path.dirname(os.fspath(path))==root:
            opened[os.path.basename(os.fspath(path))]+=1
    def io_open(file,*args,**kwargs):record(file);return real_io(file,*args,**kwargs)
    def os_open(path,*args,**kwargs):record(path);return real_os(path,*args,**kwargs)
    monkeypatch.setattr(io,'open',io_open);monkeypatch.setattr(os,'open',os_open)
    return opened


def copying_age(directory):
    directory.mkdir(exist_ok=True);age=directory/'age'
    age.write_text('#!'+sys.executable+'\nimport sys,shutil\nshutil.copyfileobj(sys.stdin.buffer,sys.stdout.buffer)\n')
    age.chmod(0o700)
    return str(age)


# spec: ANYM-631
def test_streamed_run_opens_each_original_twice_at_capture_and_in_the_stream(seeded,tmp_path,monkeypatch):
    store,*_=seeded
    opened=counting_opens(monkeypatch,store.files)
    directory=tmp_path/'capture'
    create_snapshot(store,directory,[sys.executable,'-c','print("UNIT_DUMP")'],reference_blobs=True)
    digests=[item['digest'] for item in json.loads((directory/'manifest.json').read_text())['blobs']]
    assert digests and dict(opened)=={digest:1 for digest in digests}
    snapshot_stream_bound(directory,store.files)
    assert dict(opened)=={digest:1 for digest in digests}
    output=io.BytesIO()
    encrypt_snapshot_stream(directory,output,'fixture',blob_root=store.files,age=copying_age(tmp_path/'bin'))
    assert output.getvalue() and dict(opened)=={digest:2 for digest in digests}


# spec: ANYM-631
def test_stream_bound_from_sizes_equals_the_former_bound_and_refuses_a_changed_size(tmp_path,monkeypatch):
    directory,source,manifest,_=reference_fixture(tmp_path)
    # The former bound, written out: USTAR members padded to 512, the archive to 10240, then age's overhead.
    members=[directory/'manifest.json',directory/'database.dump',*(source/item['digest'] for item in manifest['blobs'])]
    size=1024+sum(512+((path.stat().st_size+511)//512)*512 for path in members)
    size=((size+10239)//10240)*10240
    opened=counting_opens(monkeypatch,source)
    assert snapshot_stream_bound(directory,source)==size+size//100+1024**2
    assert not opened
    original=source/manifest['blobs'][0]['digest'];data=original.read_bytes()
    original.write_bytes(data+b'X')
    with pytest.raises(RuntimeError,match='backup_source_changed'):snapshot_stream_bound(directory,source)
    original.write_bytes(data)
    (directory/'manifest.json').write_text(json.dumps({**manifest,'blobs':[{'digest':'NOT-A-DIGEST','bytes':1}]}))
    with pytest.raises(ValueError,match='invalid_backup_manifest'):snapshot_stream_bound(directory,source)
    (directory/'manifest.json').write_text(json.dumps({**manifest,'format':3}))
    with pytest.raises(ValueError,match='unsupported_backup'):snapshot_stream_bound(directory,source)


# spec: ANYM-631
def test_original_changed_after_capture_fails_the_stream_and_the_receiver_sees_no_footer(tmp_path,monkeypatch):
    directory,source,manifest,age=reference_fixture(tmp_path)
    original=source/manifest['blobs'][0]['digest'];original.write_bytes(b'Y'*original.stat().st_size)
    seen=tmp_path/'receiver.log';ssh=tmp_path/'ssh'
    ssh.write_text('#!'+sys.executable+'\nimport json,struct,sys\nlog=open('+repr(str(seen))+',"w")\n'+'''
header=json.loads(sys.stdin.buffer.readline())
print(json.dumps({'v':2,'id':header['id'],'snapshot_sha256':header['snapshot_sha256'],'status':'ready'}),flush=True)
while True:
    raw=sys.stdin.buffer.read(4)
    if len(raw)!=4:log.write('aborted\\n');log.flush();sys.exit(1)
    length=struct.unpack('!I',raw)[0]
    if not length:log.write('footer\\n');log.flush();sys.exit(0)
    sys.stdin.buffer.read(length);log.write('frame\\n');log.flush()
''')
    ssh.chmod(0o700);monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    fifo=tmp_path/'ciphertext.fifo';os.mkfifo(fifo,0o600);outcome={}
    def encode():
        try:
            with fifo.open('wb',buffering=0) as out:
                outcome['result']=encrypt_snapshot_stream(directory,out,'fixture',blob_root=source,age=age)
        except BaseException as exc:outcome['error']=exc
    thread=threading.Thread(target=encode)
    def start(encoding):
        if encoding:thread.start()
    async def completed():
        await asyncio.to_thread(thread.join,30)
        if 'error' in outcome:raise outcome['error']
        return outcome['result']
    # The size check passes: only the in-stream hash can see this change.
    metadata={'v':2,'id':ID,'max_bytes':snapshot_stream_bound(directory,source),'snapshot_sha256':SNAPSHOT}
    transfer={'host':'fixture.invalid','user':'backup','identity_file':'unused','known_hosts_file':'unused'}
    with pytest.raises(RuntimeError,match='backup_source_changed'):
        asyncio.run(upload_stream(fifo,metadata,transfer,30,start,completed))
    assert not thread.is_alive() and 'frame' in seen.read_text() and 'footer' not in seen.read_text()


# spec: ANYM-631
@pytest.mark.parametrize('changed',['dump','manifest'])
def test_changed_dump_or_manifest_fails_the_stream_before_its_first_byte(tmp_path,monkeypatch,changed):
    directory,source,manifest,age=reference_fixture(tmp_path)
    expected=hashlib.sha256((directory/'manifest.json').read_bytes()).hexdigest()
    if changed=='dump':(directory/'database.dump').write_bytes(b'UNIT_NOT_A_DATABASX')
    else:(directory/'manifest.json').write_text(json.dumps({**manifest,'tables':{'changed':1}}))
    import messaging_service.backup_crypto as crypto
    started=[];popen=crypto.subprocess.Popen
    def recorded(*args,**kwargs):started.append(args);return popen(*args,**kwargs)
    monkeypatch.setattr(crypto.subprocess,'Popen',recorded)
    output=io.BytesIO()
    with pytest.raises(RuntimeError,match='dump_corrupt' if changed=='dump' else 'backup_source_changed'):
        encrypt_snapshot_stream(directory,output,'fixture',blob_root=source,age=age,expected_manifest_sha256=expected)
    assert output.getvalue()==b'' and started==[]


# spec: ANYM-631
@pytest.mark.parametrize('kept',['intact','original_changed','dump_changed'])
def test_retry_of_a_kept_capture_runs_the_capture_checks_and_opens_each_original_twice(tmp_path,monkeypatch,kept):
    from messaging_service import backup_worker as worker
    directory,source,manifest,_=reference_fixture(tmp_path)
    work=tmp_path/'work';work.mkdir(mode=0o700);directory.rename(work/'snapshot')
    (work/'snapshot/manifest.json').write_text(json.dumps({**manifest,'captured_at':'2026-10-14T00:00:00+00:00'}))
    original=source/manifest['blobs'][0]['digest']
    if kept=='original_changed':original.write_bytes(b'Y'*original.stat().st_size)
    if kept=='dump_changed':(work/'snapshot/database.dump').write_bytes(b'UNIT_NOT_A_DATABASX')
    copying_age(tmp_path/'bin');monkeypatch.setenv('PATH',str(tmp_path/'bin')+os.pathsep+os.environ['PATH'])
    dsn=tmp_path/'dsn';dsn.write_text('host=db port=5432 user=owner password=synthetic dbname=synthetic')
    recovery=tmp_path/'recovery.json';worker.write_json(recovery,{'accounts':[],'sources':[]})
    recipient=tmp_path/'recipient';recipient.write_text('fixture')
    cfg={'id':ID,'dsn_file':str(dsn),'files_directory':str(source),'recovery_config_file':str(recovery),
         'recipient_file':str(recipient),'work_directory':str(work),'mode':'stream_v2'}
    class SyntheticStore:
        def __init__(self,dsn,files,url):self.files=files
        def close(self):pass
    fences=[]
    monkeypatch.setattr(worker,'Store',SyntheticStore)
    monkeypatch.setattr(worker,'validate_reference_fence',lambda store,manifest,token:fences.append(token))
    monkeypatch.setattr(worker,'create_snapshot',lambda *args,**kwargs:pytest.fail('a kept capture is reused'))
    opened=counting_opens(monkeypatch,source);outcome={}
    def run():
        try:outcome['result']=worker.run(cfg)
        except BaseException as exc:outcome['error']=exc
    thread=threading.Thread(target=run);thread.start()
    deadline=time.monotonic()+30
    while thread.is_alive() and not (work/'stream.json').exists() and time.monotonic()<deadline:time.sleep(.01)
    if kept!='intact':
        thread.join(30)
        assert isinstance(outcome['error'],RuntimeError)
        assert str(outcome['error'])==('blob_corrupt' if kept=='original_changed' else 'dump_corrupt')
        assert not (work/'stream.json').exists() and fences==[ID]
        return
    worker.write_json(work/'stream-start.json',{'action':'start'})
    with (work/'ciphertext.fifo').open('rb') as fifo:ciphertext=fifo.read()
    thread.join(30)
    assert outcome['result']['status']=='encrypted' and outcome['result']['bytes']==len(ciphertext)
    assert fences==[ID] and dict(opened)=={original.name:2}
