import copy
import asyncio
from datetime import datetime,timezone,timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import sys

import pytest

from messaging_service.backup_receiver import receive,locked,prune,AGE_HEADER
from messaging_service.backup_runner import ID,Runner,write_json,read_json,upload
from messaging_service.backup_worker import dump_credentials

NAME='20261013T000000Z-0123456789abcdef'


def packet(data=None,**overrides):
    data=data or AGE_HEADER+b'fixture-ciphertext-only\n'*20
    header={'v':1,'id':NAME,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),**overrides}
    return io.BytesIO(json.dumps(header).encode()+b'\n'+data),data,header


def test_receiver_atomic_verified_idempotent_append_only(tmp_path):
    stream,data,header=packet();result=receive(tmp_path,stream,reserve_bytes=0)
    assert result['status']=='stored' and result['sha256']==header['sha256'] and not result['duplicate']
    target=tmp_path/(NAME+'.tar.age');assert target.read_bytes()==data and target.stat().st_mode&0o777==0o600
    before=target.stat().st_mtime_ns
    result=receive(tmp_path,packet()[0],reserve_bytes=0)
    assert result['duplicate'] and target.stat().st_mtime_ns==before
    with pytest.raises(ValueError,match='id_conflict'):
        receive(tmp_path,packet(AGE_HEADER+b'other'*100)[0],reserve_bytes=0)
    assert target.read_bytes()==data and not list(tmp_path.glob('.incoming-*'))


@pytest.mark.parametrize('attack',['traversal','operation','too_large','wrong_hash','truncated','extra','plaintext','symlink'])
def test_receiver_rejects_invalid_inputs_without_publication(tmp_path,attack):
    stream,data,header=packet()
    if attack=='traversal':header['id']='../escape'
    elif attack=='operation':header['operation']='delete'
    elif attack=='too_large':header['bytes']=30*1024**3
    elif attack=='wrong_hash':header['sha256']='0'*64
    elif attack=='truncated':data=data[:-1]
    elif attack=='extra':data+=b'X'
    elif attack=='plaintext':data=b'PLAIN_TEXT_SECRET'*30;header.update(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
    else:(tmp_path/(NAME+'.tar.age')).symlink_to(tmp_path/'outside')
    stream=io.BytesIO(json.dumps(header).encode()+b'\n'+data)
    with pytest.raises((ValueError,OSError)):receive(tmp_path,stream,reserve_bytes=0)
    assert not (tmp_path/'outside').exists() and not list(tmp_path.glob('.incoming-*'))
    assert not (tmp_path/(NAME+'.tar.age')).is_file()


def test_receiver_quota_and_concurrency_do_not_delete_old_copy(tmp_path):
    stream,data,_=packet();receive(tmp_path,stream,reserve_bytes=0)
    with pytest.raises(RuntimeError,match='quota'):
        receive(tmp_path,packet(id='20261013T010000Z-0123456789abcdef')[0],quota_bytes=len(data),reserve_bytes=0)
    with pytest.raises(RuntimeError,match='quota'):
        receive(tmp_path,packet(id='20261013T010000Z-0123456789abcdef')[0],max_files=1,reserve_bytes=0)
    with locked(tmp_path):
        with pytest.raises(RuntimeError,match='busy'):receive(tmp_path,packet()[0],reserve_bytes=0)
    assert (tmp_path/(NAME+'.tar.age')).read_bytes()==data
    # A stale unpublished upload can be removed only while the global lock is held.
    (tmp_path/'.incoming-old').write_bytes(b'partial')
    receive(tmp_path,packet()[0],reserve_bytes=0);assert not (tmp_path/'.incoming-old').exists()


def test_retention_requires_verified_anchor_and_keeps_it_even_when_old(tmp_path,monkeypatch):
    now=datetime(2026,10,13,12,tzinfo=timezone.utc);data=packet()[1];files=[]
    for age in range(12):
        created=now-timedelta(days=age);name=created.strftime('%Y%m%dT%H%M%SZ')+'-'+f'{age:016x}'
        p=tmp_path/(name+'.tar.age');p.write_bytes(data);os.utime(p,(created.timestamp(),created.timestamp()));files.append(p)
    marker=tmp_path/'protected.json'
    with pytest.raises(FileNotFoundError):prune(tmp_path,marker,now=now)
    assert len(list(tmp_path.glob('*.tar.age')))==12
    write_json(marker,[{'id':files[-1].name[:-8],'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}])
    original=Path.stat
    def owned_by_root(p,*args,**kwargs):
        info=original(p,*args,**kwargs)
        if p==marker:
            values=list(info);values[4]=0;return os.stat_result(values)
        return info
    monkeypatch.setattr(Path,'stat',owned_by_root)
    result=prune(tmp_path,marker,now=now,keep_last=2,keep_daily=2,keep_weekly=1)
    assert result['removed']>=7 and files[-1].exists() and files[0].exists() and files[1].exists()
    before={p.name for p in tmp_path.glob('*.tar.age')};files[-1].write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='protected_backup_corrupt'):prune(tmp_path,marker,now=now)
    assert {p.name for p in tmp_path.glob('*.tar.age')}==before


def test_pg_dump_password_stays_out_of_argv_and_escapes_pgpass(tmp_path):
    command,password_file=dump_credentials('host=db port=5432 dbname=test user=owner password=secret:with:colon',tmp_path)
    assert 'secret' not in ' '.join(command)
    assert password_file.read_text()=='db:5432:test:owner:secret\\:with\\:colon\n'
    assert password_file.stat().st_mode&0o777==0o600
    with pytest.raises(ValueError):dump_credentials("host=db dbname=test user=owner password='line\nbreak'",tmp_path)


@pytest.mark.parametrize('mode',['ok','wrong_receipt','quota','timeout'])
def test_upload_protocol_and_timeout_terminate_owned_process(tmp_path,monkeypatch,mode):
    ssh=tmp_path/'ssh';pid=tmp_path/'child.pid'
    script='''import hashlib,json,os,sys,time
assert 'SSH_AUTH_SOCK' not in os.environ
assert sys.argv[1:4]==['-F','/dev/null','-T']
open(PID,'w').write(str(os.getpid()))
if MODE=='timeout':time.sleep(60)
header=json.loads(sys.stdin.buffer.readline());data=sys.stdin.buffer.read()
assert len(data)==header['bytes'] and hashlib.sha256(data).hexdigest()==header['sha256']
if MODE=='quota':print(json.dumps({'status':'error','code':'receiver_quota'}));sys.exit(1)
header['status']='stored'
if MODE=='wrong_receipt':header['sha256']='0'*64
print(json.dumps(header))
'''
    ssh.write_text('#!'+sys.executable+'\nPID='+repr(str(pid))+'\nMODE='+repr(mode)+'\n'+script);ssh.chmod(0o700)
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH']);monkeypatch.setenv('SSH_AUTH_SOCK','must-not-forward')
    _,data,receipt=packet();path=tmp_path/'bundle';path.write_bytes(data)
    t={'host':'fixture.invalid','user':'backup','identity_file':'unused','known_hosts_file':'unused'}
    if mode=='ok':assert asyncio.run(upload(path,receipt,t,5))['status']=='stored'
    else:
        expected=asyncio.TimeoutError if mode=='timeout' else RuntimeError
        with pytest.raises(expected):asyncio.run(upload(path,receipt,t,.3 if mode=='timeout' else 5))
    if pid.exists():
        with pytest.raises(ProcessLookupError):os.kill(int(pid.read_text()),0)


class FakeDocker:
    """Models the actual journal boundary between host, detached Docker and files."""
    def __init__(self,root):
        self.root=root;self.containers={};self.calls=[];self.capture_fails=False;self.hold_worker=False;self.transient=False
        # Runs the isolated abandon_fence action for a job directory; returns its exit code.
        self.fence_worker=None
        self.containers['tg-id']=self.collector('tg-id','telegram',True)
        self.containers['wa-id']=self.collector('wa-id','whatsapp1',False)
    @staticmethod
    def collector(cid,service,running):
        return {'Id':cid,'Config':{'Labels':{'com.docker.compose.project':'test-project','com.docker.compose.service':service}},
                'State':{'Running':running,'Status':'running' if running else 'exited','ExitCode':0}}
    def running_collectors(self,project):return [{'id':'tg-id','service':'telegram'}]
    def inspect(self,cid):
        if self.transient:
            self.transient=False;raise RuntimeError('docker_inspection_failed')
        info=self.containers.get(cid)
        if info and cid.startswith('pos-backup-') and info['State']['Running'] and not self.hold_worker:
            work=self.root/info['Config']['Labels']['personal-os.backup.id']
            if self.capture_fails:
                info['State'].update(Running=False,Status='exited',ExitCode=1)
            elif self.containers['tg-id']['State']['Running']:
                # Encryption may finish only after the collector was resumed.
                data=AGE_HEADER+b'fake encrypted fixture'*30;(work/'bundle.tar.age').write_bytes(data)
                write_json(work/'result.json',{'v':1,'id':work.name,'status':'encrypted','bytes':len(data),
                    'sha256':hashlib.sha256(data).hexdigest(),'captured_at':'2026-10-13T00:00:00+00:00'})
                info['State'].update(Running=False,Status='exited',ExitCode=0)
        return copy.deepcopy(info)
    def call(self,*args):
        self.calls.append(args)
        if args[0]=='create':
            name=args[args.index('--name')+1];labels={}
            for i,arg in enumerate(args):
                if arg=='--label':k,v=args[i+1].split('=',1);labels[k]=v
            self.containers[name]={'Id':name,'Config':{'Labels':labels},'State':{'Running':False,'Status':'created','ExitCode':0}}
            return name
        if args[0]=='start':
            info=self.containers[args[1]];info['State'].update(Running=True,Status='running')
            if info['Config']['Labels'].get('personal-os.backup.action')=='abandon_fence':
                work=self.root/info['Config']['Labels']['personal-os.backup.id']
                if self.fence_worker:code=self.fence_worker(work)
                else:
                    code=0;write_json(work/'fence-abandoned.json',{'id':work.name,'backup_fence':work.name,'status':'fence_absent'})
                info['State'].update(Running=False,Status='exited',ExitCode=code)
            elif args[1].startswith('pos-backup-'):
                work=self.root/info['Config']['Labels']['personal-os.backup.id']
                (work/'snapshot').mkdir();(work/'snapshot/private').write_bytes(b'PLAINTEXT')
                if not self.capture_fails:write_json(work/'captured.json',{'id':work.name,'status':'capture_complete'})
        elif args[0]=='stop':self.containers[args[-1]]['State'].update(Running=False,Status='exited',ExitCode=143)
        elif args[0]=='rm':self.containers.pop(args[1])
        return ''


def runner_fixture(tmp_path,send=None):
    cfg={'directory':str(tmp_path),'project':'test-project','network':'backend','image':'backup:test',
         'files_volume':'files','dsn_file':'/private/owner.dsn','recipient_file':'/private/public.recipient',
         'mounts':[],'recovery':{'accounts':[],'sources':[]},'transfer':{},'timeout_seconds':60,'keep_local':2}
    docker=FakeDocker(tmp_path)
    async def success(path,result,transfer,timeout):
        assert not (path.parent/'snapshot').exists()
        return {**result,'status':'stored','duplicate':False}
    runner=Runner(cfg,docker=docker,sleep=lambda _:None,send=send or success)
    return runner,docker


def test_runner_resumes_only_previously_running_collectors_before_encryption(tmp_path):
    runner,docker=runner_fixture(tmp_path);runner.run()
    assert ('start','tg-id') in docker.calls and ('start','wa-id') not in docker.calls
    assert ('stop','--time','25','tg-id') in docker.calls
    assert not (tmp_path/'journal.json').exists() and read_json(tmp_path/'status.json')['state']=='ok'
    assert not any(p.name=='snapshot' for p in tmp_path.rglob('*'))
    assert not any(cid.startswith('pos-backup-') for cid in docker.containers)


def test_runner_delivery_failure_retries_same_object_without_cold_capture(tmp_path):
    attempts=[]
    async def send(path,result,*_):
        attempts.append((result['id'],result['sha256']))
        if len(attempts)==1:raise RuntimeError('receiver_quota')
        return {**result,'status':'stored','duplicate':True}
    runner,docker=runner_fixture(tmp_path,send)
    with pytest.raises(RuntimeError,match='quota'):runner.run()
    runner.recover_stop()
    status=read_json(tmp_path/'status.json');assert status['error_code']=='receiver_quota' and 'last_success_at' not in status
    first=read_json(tmp_path/'journal.json');assert first['phase']=='upload' and not first['stopped']
    assert docker.containers['tg-id']['State']['Running']
    assert (tmp_path/first['id']/'bundle.tar.age').is_file() and not (tmp_path/first['id']/'snapshot').exists()
    runner.run();assert attempts[0]==attempts[1]
    assert sum(c[0]=='create' for c in docker.calls)==1


def test_runner_capture_failure_resume_and_remove_only_owned_staging(tmp_path):
    runner,docker=runner_fixture(tmp_path);docker.capture_fails=True
    with pytest.raises(RuntimeError,match='capture_failed'):runner.run()
    assert not docker.containers['tg-id']['State']['Running']
    runner.recover_stop()
    assert docker.containers['tg-id']['State']['Running'] and not docker.containers['wa-id']['State']['Running']
    assert not (tmp_path/'journal.json').exists() and not any(p.name=='snapshot' for p in tmp_path.rglob('*'))


def test_runner_interrupted_capture_removes_abandoned_ciphertext_but_never_delivered_jobs(tmp_path):
    runner,docker=runner_fixture(tmp_path);docker.hold_worker=True
    delivered=tmp_path/'20261012T000000Z-0123456789abcdef';delivered.mkdir(mode=0o700)
    (delivered/'bundle.tar.age').write_bytes(AGE_HEADER+b'delivered fixture')
    write_json(delivered/'delivery.json',{'status':'stored'})
    ticks=iter(range(0,10**6,30));runner.clock=lambda:next(ticks)
    def encrypting(_):
        # The worker is still encrypting when the whole-job deadline expires:
        # a partial age temporary and a complete but undelivered ciphertext.
        work=tmp_path/runner.journal['id']
        (work/'.encrypt-synthetic').write_bytes(b'partial ciphertext fixture')
        (work/'bundle.tar.age').write_bytes(AGE_HEADER+b'undelivered fixture')
    runner.sleep=encrypting
    with pytest.raises(RuntimeError,match='capture_timeout'):runner.run()
    job=tmp_path/runner.journal['id']
    assert (job/'bundle.tar.age').is_file() and (job/'.encrypt-synthetic').is_file()
    runner.recover_stop()
    assert not job.exists() and not (tmp_path/'journal.json').exists()
    assert docker.containers['tg-id']['State']['Running'] and not docker.containers['wa-id']['State']['Running']
    assert not any(cid.startswith('pos-backup-') for cid in docker.containers)
    assert read_json(tmp_path/'status.json')['error_code']=='backup_capture_timeout'
    assert (delivered/'bundle.tar.age').is_file() and (delivered/'delivery.json').is_file()
    # A new capture under a new ID succeeds; local retention sees only delivered jobs.
    docker.hold_worker=False;runner.sleep=lambda _:None;runner.run()
    jobs=sorted(p.name for p in tmp_path.iterdir() if p.is_dir() and ID.fullmatch(p.name))
    assert len(jobs)==2 and delivered.name in jobs and all((tmp_path/name/'delivery.json').is_file() for name in jobs)


def test_runner_recovery_never_removes_a_delivered_job_directory(tmp_path):
    runner,docker=runner_fixture(tmp_path);runner.prepare();work=tmp_path/runner.journal['id']
    (work/'bundle.tar.age').write_bytes(AGE_HEADER+b'delivered fixture')
    write_json(work/'delivery.json',{'status':'stored'})
    with pytest.raises(RuntimeError,match='invalid_backup_journal'):runner.recover_stop()
    assert (work/'bundle.tar.age').is_file() and (tmp_path/'journal.json').exists()


def test_runner_resumes_existing_worker_after_host_restart_or_inspection_failure(tmp_path):
    runner,docker=runner_fixture(tmp_path);runner.prepare();work=tmp_path/runner.journal['id']
    docker.call('stop','--time','25','tg-id');runner.journal['phase']='capturing';runner.save()
    runner.create_worker(work);docker.call('start',runner.journal['container'])
    # No PID/process guess: actual Docker identity remains present across new Runner.
    runner2=Runner(runner.cfg,docker=docker,sleep=lambda _:None,send=runner.send)
    docker.transient=True
    with pytest.raises(RuntimeError,match='inspection_failed'):runner2.run()
    assert sum(c[0]=='create' for c in docker.calls)==1
    runner2.run();assert sum(c[0]=='create' for c in docker.calls)==1
    assert read_json(tmp_path/'status.json')['state']=='ok'


def test_runner_recovers_crash_after_remote_receipt_and_container_cleanup(tmp_path):
    runner,docker=runner_fixture(tmp_path)
    # Force a host crash at the last boundary before removing the journal.
    original=runner.finish
    def crash(work,result,receipt):
        write_json(work/'delivery.json',receipt);docker.call('rm',runner.journal['container']);raise RuntimeError('host_crash')
    runner.finish=crash
    with pytest.raises(RuntimeError,match='host_crash'):runner.run()
    assert read_json(tmp_path/'journal.json')['phase']=='upload'
    runner.finish=original;runner.run()
    assert read_json(tmp_path/'status.json')['state']=='ok' and not (tmp_path/'journal.json').exists()


def test_concurrent_cli_cannot_recover_or_stop_lock_owners_job(tmp_path,monkeypatch):
    import fcntl
    from messaging_service import backup_runner
    runner,docker=runner_fixture(tmp_path);runner.prepare()
    lock=os.open(tmp_path/'.runner.lock',os.O_CREAT|os.O_RDWR,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    called=[]
    monkeypatch.setattr(backup_runner,'config',lambda _:runner.cfg)
    monkeypatch.setattr(backup_runner,'Runner',lambda _:runner)
    monkeypatch.setattr(runner,'recover_stop',lambda:called.append('unexpected recovery'))
    monkeypatch.setattr(sys,'argv',['backup_runner','--config','unused'])
    monkeypatch.setattr(backup_runner.signal,'signal',lambda *_:None)
    before=(tmp_path/'journal.json').read_bytes()
    try:
        with pytest.raises(SystemExit):backup_runner.main()
    finally:os.close(lock)
    assert called==[] and docker.calls==[]
    assert (tmp_path/'journal.json').read_bytes()==before
