"""Operator host job; stdlib only. Docker access stays on the host, never in MCP.

Durable journal resumes a known in-flight container instead of launching another.
An upload failure retains the same encrypted object for an idempotent retry.
"""
import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime,timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time

ID=re.compile(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{16}')
SERVICES={'telegram','whatsapp1','whatsapp2'}
COLLECTOR_LABEL='org.messaging-archive.collector'
SAFE_ERRORS={'backup_capture_failed','backup_capture_timeout','backup_container_disappeared','backup_container_mismatch',
    'backup_delivery_failed','invalid_backup_receipt','local_backup_corrupt','receiver_quota','receiver_disk_reserve',
    'receiver_busy','backup_id_conflict','docker_inspection_failed','docker_operation_failed',
    'backup_fence_release_failed','backup_capture_unfenced','backup_fence_retained',
    'collector_missing_during_resume','collector_identity_changed','collector_missing_during_stop','host_lock_busy'}
# The receiver's own verifier holds its upload lock for a whole restore (about an hour). A delivery it refuses as busy
# before it stores anything (stream_v2: before the stream started) is offered again, same backup ID, every
# BUSY_RETRY_SECONDS while the bound since the first offer allows; each offer keeps its upload timeout.
BUSY_RETRY_SECONDS=60
BUSY_WAIT_SECONDS=7200
# The calendar profile (ANYM-608) waits this long for messaging's runner lock, which a deploy and messaging's own run
# hold, so a 16:00 run gives up before messaging's 18:00 one. Its worker owns no application container.
HOST_LOCK_WAIT_SECONDS=5400
HOST_LOCK_POLL_SECONDS=5
# Each wait for a worker container runs `docker container inspect` once per poll (ANYM-635).
POLL_SECONDS=1.0
CALENDAR_WORKER='deedkit-calendar-backup-'


def read_json(path,limit=65536):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as f:
        st=os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size>limit or st.st_mode&0o077:raise ValueError('private_json_required')
        return json.load(f)


def write_json(path,data):
    path=Path(path);fd,name=tempfile.mkstemp(prefix='.journal-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as out:
            out.write(json.dumps(data,separators=(',',':'))+'\n');out.flush();os.fsync(out.fileno())
        os.replace(name,path)
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:Path(name).unlink(missing_ok=True)


def path_valid(value):
    p=Path(value)
    return p.is_absolute() and '..' not in p.parts and not any(c in value for c in ',\n\r\x00') and not any(x.is_symlink() for x in [p,*p.parents])


def transfer_valid(t):
    return (isinstance(t,dict) and set(t)=={'host','user','identity_file','known_hosts_file'} and
        all(isinstance(v,str) for v in t.values()) and re.fullmatch(r'[a-zA-Z0-9.:-]{1,253}',t['host']) and
        re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',t['user']) and all(path_valid(t[k]) for k in ('identity_file','known_hosts_file')))


def calendar_config(cfg):
    """`profile: calendar`: database and calendar state only, staged (ANYM-605's worker stages), messaging's lock."""
    if set(cfg)-{'delivery_mode','busy_wait_seconds'}!={'profile','directory','project','network','image','dsn_file',
                  'recipient_file','state_mounts','transfer','timeout_seconds','keep_local','host_lock'}:
        raise ValueError('invalid_backup_config')
    if cfg.get('delivery_mode','staged_v1')!='staged_v1':raise ValueError('invalid_backup_config')
    for key in ('project','network'):
        if not isinstance(cfg[key],str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}',cfg[key]):raise ValueError('invalid_backup_config')
    if not isinstance(cfg['image'],str) or not re.fullmatch(r'[a-zA-Z0-9_./:@-]{1,256}',cfg['image']):
        raise ValueError('invalid_backup_config')
    for key in ('directory','dsn_file','recipient_file','host_lock'):
        if not isinstance(cfg[key],str) or not path_valid(cfg[key]):raise ValueError('invalid_backup_path')
    # Its own lock as host_lock would block the run on itself.
    if Path(cfg['host_lock'])==Path(cfg['directory'])/'.runner.lock':raise ValueError('invalid_backup_path')
    mounts=cfg['state_mounts']
    if (not isinstance(mounts,dict) or set(mounts)!={'vault','profile'} or
        not all(isinstance(v,str) and path_valid(v) for v in mounts.values())):raise ValueError('invalid_backup_mount')
    if not transfer_valid(cfg['transfer']):raise ValueError('invalid_backup_transfer')
    if type(cfg['timeout_seconds']) is not int or not 60<=cfg['timeout_seconds']<=7200:raise ValueError('invalid_backup_timeout')
    if type(cfg.get('busy_wait_seconds',0)) is not int or not 0<=cfg.get('busy_wait_seconds',0)<=7200:
        raise ValueError('invalid_backup_timeout')
    if type(cfg['keep_local']) is not int or not 1<=cfg['keep_local']<=24:raise ValueError('invalid_backup_retention')
    return cfg


def config(path):
    cfg=read_json(path)
    if not isinstance(cfg,dict) or cfg.get('profile','messaging') not in ('messaging','calendar'):
        raise ValueError('invalid_backup_config')
    if cfg.get('profile')=='calendar':return calendar_config(cfg)
    if set(cfg)-{'profile','delivery_mode','busy_wait_seconds','dump_timeout_seconds'}!={'directory','project','network','image','files_volume','dsn_file','recipient_file',
                  'mounts','recovery','transfer','timeout_seconds','keep_local'}:raise ValueError('invalid_backup_config')
    if cfg.get('delivery_mode','staged_v1') not in ('staged_v1','stream_v2'):raise ValueError('invalid_backup_config')
    for key in ('project','network','files_volume'):
        if not isinstance(cfg[key],str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}',cfg[key]):raise ValueError('invalid_backup_config')
    if not re.fullmatch(r'[a-zA-Z0-9_./:@-]{1,256}',cfg['image']):raise ValueError('invalid_backup_config')
    for key in ('directory','dsn_file','recipient_file'):
        if not isinstance(cfg[key],str) or not path_valid(cfg[key]):raise ValueError('invalid_backup_path')
    aliases=set()
    for mount in cfg['mounts']:
        if (set(mount)!={'name','source'} or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',mount['name']) or
            mount['name'] in aliases or not path_valid(mount['source'])):raise ValueError('invalid_backup_mount')
        aliases.add(mount['name'])
    t=cfg['transfer']
    if (set(t)!={'host','user','identity_file','known_hosts_file'} or not re.fullmatch(r'[a-zA-Z0-9.:-]{1,253}',t['host']) or
        not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',t['user']) or
        not all(path_valid(t[k]) for k in ('identity_file','known_hosts_file'))):raise ValueError('invalid_backup_transfer')
    if type(cfg['timeout_seconds']) is not int or not 60<=cfg['timeout_seconds']<=7200:raise ValueError('invalid_backup_timeout')
    if 'dump_timeout_seconds' in cfg and (type(cfg['dump_timeout_seconds']) is not int or
        not 300<=cfg['dump_timeout_seconds']<=min(7200,cfg['timeout_seconds'])):raise ValueError('invalid_backup_timeout')
    if type(cfg.get('busy_wait_seconds',0)) is not int or not 0<=cfg.get('busy_wait_seconds',0)<=7200:
        raise ValueError('invalid_backup_timeout')
    if type(cfg['keep_local']) is not int or not 1<=cfg['keep_local']<=24:raise ValueError('invalid_backup_retention')
    return cfg


def dump_timeout(cfg):
    """pg_dump's limit inside the capture: optional in runner.json, else 1800 s or the capture timeout if lower."""
    return cfg.get('dump_timeout_seconds',min(1800,cfg['timeout_seconds']))


class Docker:
    @staticmethod
    def call(*args):
        result=subprocess.run(['docker',*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90)
        if result.returncode:raise RuntimeError('docker_operation_failed')
        return result.stdout.decode().strip()
    @staticmethod
    def inspect(name):
        result=subprocess.run(['docker','container','inspect',name],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        if result.returncode:
            if b'No such container' in result.stderr or b'No such object' in result.stderr:return None
            raise RuntimeError('docker_inspection_failed')
        return json.loads(result.stdout)[0]
    def running_collectors(self,project):
        ids=self.call('ps','-q','--no-trunc','--filter','label=com.docker.compose.project='+project).split()
        result=[]
        for container_id in ids:
            info=self.inspect(container_id)
            labels=info['Config'].get('Labels',{}) if info else {}
            service=labels.get('com.docker.compose.service')
            oneoff=str(labels.get('com.docker.compose.oneoff','false')).lower()=='true'
            if info and service and not oneoff and (labels.get(COLLECTOR_LABEL)=='true' or service in SERVICES):
                result.append({'id':info['Id'],'service':service})
        return result


async def upload(path,receipt,transfer,timeout):
    args=['ssh','-F','/dev/null','-T','-o','BatchMode=yes','-o','IdentitiesOnly=yes',
        '-o','StrictHostKeyChecking=yes','-o','PasswordAuthentication=no','-o','KbdInteractiveAuthentication=no',
        '-o','ConnectTimeout=15','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
        '-o','UserKnownHostsFile='+transfer['known_hosts_file'],'-i',transfer['identity_file'],
        transfer['user']+'@'+transfer['host'],'backup-put-v1']
    env={k:v for k,v in os.environ.items() if k!='SSH_AUTH_SOCK'}
    process=await asyncio.create_subprocess_exec(*args,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,limit=4096,env=env)
    async def transfer_bytes():
        header={k:receipt[k] for k in ('v','id','bytes','sha256')}
        process.stdin.write((json.dumps(header,separators=(',',':'))+'\n').encode());await process.stdin.drain()
        with Path(path).open('rb') as src:
            while data:=src.read(1024**2):
                process.stdin.write(data);await process.stdin.drain()
        process.stdin.close();await process.stdin.wait_closed()
    async def exchange():
        sender=asyncio.create_task(transfer_bytes())
        try:
            raw=await process.stdout.readline()
            if len(raw)>4096 or not raw.endswith(b'\n'):raise RuntimeError('invalid_backup_receipt')
            response=json.loads(raw)
            if response.get('status')=='error':
                raise RuntimeError(response.get('code') if response.get('code') in SAFE_ERRORS else 'backup_delivery_failed')
            await sender
            if await process.wait()!=0 or response.get('status')!='stored':raise RuntimeError('backup_delivery_failed')
            if any(response.get(k)!=receipt[k] for k in ('v','id','bytes','sha256')):raise RuntimeError('invalid_backup_receipt')
            return response
        finally:
            if not sender.done():sender.cancel()
            await asyncio.gather(sender,return_exceptions=True)
    try:return await asyncio.wait_for(exchange(),timeout)
    finally:
        if process.returncode is None:
            process.terminate()
            try:await asyncio.wait_for(process.wait(),10)
            except asyncio.TimeoutError:process.kill();await process.wait()


async def upload_stream(path,metadata,transfer,timeout,start,completed,stopped=None):
    """Host-only SSH bridge; bounded FIFO and network buffers, never a disk spool.

    ``stopped`` reports a worker that exited or published its result; it ends
    the wait for a writer that will never open the FIFO.
    """
    args=['ssh','-F','/dev/null','-T','-o','BatchMode=yes','-o','IdentitiesOnly=yes',
        '-o','StrictHostKeyChecking=yes','-o','PasswordAuthentication=no','-o','KbdInteractiveAuthentication=no',
        '-o','ConnectTimeout=15','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
        '-o','UserKnownHostsFile='+transfer['known_hosts_file'],'-i',transfer['identity_file'],
        transfer['user']+'@'+transfer['host'],'backup-put-v2']
    env={k:v for k,v in os.environ.items() if k!='SSH_AUTH_SOCK'}
    process=await asyncio.create_subprocess_exec(*args,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,limit=4096,env=env)
    fd=None
    async def response():
        raw=await process.stdout.readline()
        if len(raw)>4096 or not raw.endswith(b'\n'):raise RuntimeError('invalid_backup_receipt')
        item=json.loads(raw)
        if not isinstance(item,dict):raise RuntimeError('invalid_backup_receipt')
        if item.get('status')=='error':
            raise RuntimeError(item.get('code') if item.get('code') in SAFE_ERRORS else 'backup_delivery_failed')
        if (item.get('v')!=2 or item.get('id')!=metadata['id'] or
            item.get('snapshot_sha256')!=metadata['snapshot_sha256']):raise RuntimeError('invalid_backup_receipt')
        return item
    async def exchange():
        nonlocal fd
        header={k:metadata[k] for k in ('v','id','max_bytes','snapshot_sha256')}
        process.stdin.write((json.dumps(header,separators=(',',':'))+'\n').encode());await process.stdin.drain()
        item=await response()
        if item.get('status')=='stored':
            if (type(item.get('bytes')) is not int or not 64<=item['bytes']<=metadata['max_bytes'] or
                not isinstance(item.get('sha256'),str) or not re.fullmatch('[a-f0-9]{64}',item['sha256'])):
                raise RuntimeError('invalid_backup_receipt')
            process.stdin.close();await process.stdin.wait_closed()
            if await process.wait()!=0:raise RuntimeError('backup_delivery_failed')
            start(False);return item
        if item.get('status')!='ready':raise RuntimeError('invalid_backup_receipt')
        fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):raise ValueError('invalid_backup_stream')
        start(True);sha=hashlib.sha256();size=0;ended=False;probe=0
        loop=asyncio.get_running_loop()
        while True:
            try:data=os.read(fd,1024**2)
            except BlockingIOError:data=None
            if data:
                size+=len(data)
                if size>metadata['max_bytes']:raise RuntimeError('backup_stream_size_limit')
                sha.update(data);process.stdin.write(struct.pack('!I',len(data))+data);await process.stdin.drain()
            elif data==b'' and (size or ended):break
            else:
                # EOF before any byte means no writer is attached yet. A worker
                # that already failed never opens the FIFO: stop waiting for the
                # whole deadline, read once more for bytes written before it
                # exited, then let the independent result decide.
                if data==b'' and stopped is not None and loop.time()>=probe:
                    probe=loop.time()+1
                    if stopped():ended=True;continue
                await asyncio.sleep(.01)
        # Source/age integrity is an independent result. EOF alone must never
        # turn failed encryption or changed originals into a published backup.
        result=await completed()
        if result.get('bytes')!=size or result.get('sha256')!=sha.hexdigest():raise RuntimeError('local_backup_corrupt')
        footer={'bytes':size,'sha256':sha.hexdigest()}
        process.stdin.write(struct.pack('!I',0)+(json.dumps(footer,separators=(',',':'))+'\n').encode())
        await process.stdin.drain();process.stdin.close();await process.stdin.wait_closed()
        receipt=await response()
        if (await process.wait()!=0 or receipt.get('status')!='stored' or
            any(receipt.get(k)!=footer[k] for k in footer)):raise RuntimeError('invalid_backup_receipt')
        return receipt
    try:return await asyncio.wait_for(exchange(),timeout)
    finally:
        if fd is not None:os.close(fd)
        if process.returncode is None:
            process.terminate()
            try:await asyncio.wait_for(process.wait(),10)
            except asyncio.TimeoutError:process.kill();await process.wait()


async def upload_file_v2(path,result,transfer,timeout):
    """The calendar's staged, already encrypted bundle over `backup-put-v2` framing: the R2 receiver speaks only v2.

    The header bounds the object at the file's exact size and names the manifest hash the verifier's proof must
    equal; a stored duplicate answers the header, otherwise the file goes in 1 MiB frames and a footer.
    """
    args=['ssh','-F','/dev/null','-T','-o','BatchMode=yes','-o','IdentitiesOnly=yes',
        '-o','StrictHostKeyChecking=yes','-o','PasswordAuthentication=no','-o','KbdInteractiveAuthentication=no',
        '-o','ConnectTimeout=15','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
        '-o','UserKnownHostsFile='+transfer['known_hosts_file'],'-i',transfer['identity_file'],
        transfer['user']+'@'+transfer['host'],'backup-put-v2']
    env={k:v for k,v in os.environ.items() if k!='SSH_AUTH_SOCK'}
    process=await asyncio.create_subprocess_exec(*args,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,limit=4096,env=env)
    async def response():
        raw=await process.stdout.readline()
        if len(raw)>4096 or not raw.endswith(b'\n'):raise RuntimeError('invalid_backup_receipt')
        item=json.loads(raw)
        if not isinstance(item,dict):raise RuntimeError('invalid_backup_receipt')
        if item.get('status')=='error':
            raise RuntimeError(item.get('code') if item.get('code') in SAFE_ERRORS else 'backup_delivery_failed')
        if item.get('v')!=2 or item.get('id')!=result['id'] or item.get('snapshot_sha256')!=result['snapshot_sha256']:
            raise RuntimeError('invalid_backup_receipt')
        return item
    def stored(item):
        if item.get('status')!='stored' or item.get('bytes')!=result['bytes'] or item.get('sha256')!=result['sha256']:
            raise RuntimeError('invalid_backup_receipt')
        return item
    async def exchange():
        header={'v':2,'id':result['id'],'max_bytes':result['bytes'],'snapshot_sha256':result['snapshot_sha256']}
        process.stdin.write((json.dumps(header,separators=(',',':'))+'\n').encode());await process.stdin.drain()
        item=await response()
        if item.get('status')=='stored':
            process.stdin.close();await process.stdin.wait_closed()
            if await process.wait()!=0:raise RuntimeError('backup_delivery_failed')
            return stored(item)
        if item.get('status')!='ready':raise RuntimeError('invalid_backup_receipt')
        sha=hashlib.sha256();size=0
        with Path(path).open('rb') as src:
            while data:=src.read(1024**2):
                size+=len(data);sha.update(data)
                process.stdin.write(struct.pack('!I',len(data))+data);await process.stdin.drain()
        footer={'bytes':size,'sha256':sha.hexdigest()}
        if size!=result['bytes'] or footer['sha256']!=result['sha256']:raise RuntimeError('local_backup_corrupt')
        process.stdin.write(struct.pack('!I',0)+(json.dumps(footer,separators=(',',':'))+'\n').encode())
        await process.stdin.drain();process.stdin.close();await process.stdin.wait_closed()
        receipt=await response()
        if await process.wait()!=0:raise RuntimeError('backup_delivery_failed')
        return stored(receipt)
    try:return await asyncio.wait_for(exchange(),timeout)
    finally:
        if process.returncode is None:
            process.terminate()
            try:await asyncio.wait_for(process.wait(),10)
            except asyncio.TimeoutError:process.kill();await process.wait()


class Runner:
    def __init__(self,cfg,*,docker=None,clock=time.time,sleep=time.sleep,send=None,stream_send=upload_stream):
        self.cfg=cfg;self.root=Path(cfg['directory']);self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_mode&0o077:raise ValueError('private_backup_directory_required')
        self.journal_path=self.root/'journal.json';self.status_path=self.root/'status.json'
        self.docker=docker or Docker();self.clock=clock;self.sleep=sleep;self.send=send;self.stream_send=stream_send;self.journal=None
        self.calendar=cfg.get('profile')=='calendar'
        # The calendar delivers to the R2 receiver, which takes only the v2 wire (ANYM-607).
        if send is None:self.send=upload_file_v2 if self.calendar else upload
        # Exists from before the calendar worker is created until it is verified stopped: a deploy refuses while it
        # exists (production.py), also after this process died or failed its recovery and released the locks.
        self.unsettled_path=Path(cfg['host_lock']+'.unsettled') if self.calendar else None
    def save(self):write_json(self.journal_path,self.journal)
    def settle(self,container):
        """Remove the unsettled marker once `container` (this profile's own worker) is absent or stopped."""
        if not self.calendar or not os.path.lexists(self.unsettled_path):return
        if not container.startswith(CALENDAR_WORKER):raise RuntimeError('backup_container_mismatch')
        info=self.docker.inspect(container)
        if info is not None and info['State']['Running']:raise RuntimeError('backup_shutdown_unverified')
        self.unsettled_path.unlink()
    def load(self):
        self.journal=read_json(self.journal_path)
        if not ID.fullmatch(self.journal['id']):raise ValueError('invalid_backup_journal')
        # A calendar job names only its own worker: never an application container to stop, start or remove.
        if self.calendar and (self.journal['stopped'] or self.journal['container']!=CALENDAR_WORKER+self.journal['id'].lower()):
            raise ValueError('invalid_backup_journal')
    def deliver(self,send,sent=lambda:False):
        """`send()`, offered again while the receiver answers busy and `sent()` says the stream has not started; a new
        offer starts only within the bound since the first. Returns the receipt and the seconds before the offer that
        was stored. Past the bound, or on any other error, the error stands."""
        first=self.clock();deadline=first+self.cfg.get('busy_wait_seconds',BUSY_WAIT_SECONDS);attempt=1
        while True:
            offered=self.clock()
            try:return send(),offered-first
            except RuntimeError as exc:
                if str(exc)!='receiver_busy' or sent():raise
                retry=self.clock()+BUSY_RETRY_SECONDS<=deadline;self.refused(attempt,retry)
                if not retry:raise
            attempt+=1;self.sleep(BUSY_RETRY_SECONDS)
            if self.clock()>deadline:  # A late wakeup never starts an offer past the bound.
                self.refused(attempt-1,False);raise RuntimeError('receiver_busy')
    def refused(self,attempt,retry):
        """One journal line per busy answer: the backup ID and the attempt, no host or path. Never fails a delivery."""
        try:print(json.dumps({'status':'waiting' if retry else 'refused','code':'receiver_busy','id':self.journal['id'],
                              'attempt':attempt,'retry_seconds':BUSY_RETRY_SECONDS if retry else 0},
                             separators=(',',':')),flush=True)
        except (OSError,ValueError):pass
    def status(self,state,code=None):
        previous=read_json(self.status_path) if self.status_path.exists() else {}
        data={**previous,'state':state,'updated_at':datetime.now(timezone.utc).isoformat()}
        data.pop('error_code',None)
        if code:data['error_code']=code
        if self.journal:data['id']=self.journal['id']
        write_json(self.status_path,data)
    def check_worker(self,info):
        if info is None:return None
        labels=info['Config'].get('Labels',{})
        if (labels.get('personal-os.backup.id')!=self.journal['id'] or
            labels.get('personal-os.backup.project')!=self.cfg['project']):raise RuntimeError('backup_container_mismatch')
        return info
    def resume_collectors(self):
        for saved in list(self.journal['stopped']):
            info=self.docker.inspect(saved['id'])
            if info is None:raise RuntimeError('collector_missing_during_resume')
            labels=info['Config'].get('Labels',{})
            if (labels.get('com.docker.compose.project')!=self.cfg['project'] or
                labels.get('com.docker.compose.service')!=saved['service']):raise RuntimeError('collector_identity_changed')
            if not info['State']['Running']:self.docker.call('start',saved['id'])
            self.journal['stopped'].remove(saved);self.save()
    def prepare(self):
        if self.journal_path.exists():return self.load()
        backup_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(8)
        work=self.root/backup_id;work.mkdir(mode=0o700)
        stopped=[] if self.calendar else self.docker.running_collectors(self.cfg['project'])
        self.journal={'id':backup_id,'phase':'stopping','stopped':stopped,'created':self.clock(),
                      'container':(CALENDAR_WORKER if self.calendar else 'pos-backup-')+backup_id.lower()}
        self.save();self.status('capturing')
    def create_calendar_worker(self,work):
        """The calendar profile's worker: its database, vault and profile files read-only, no files volume."""
        write_json(self.unsettled_path,{'profile':'calendar','id':self.journal['id'],'container':self.journal['container']})
        write_json(work/'worker.json',{'profile':'calendar','id':self.journal['id'],'dsn_file':'/run/secrets/database_url',
            'vault_directory':'/sources/vault','profile_directory':'/sources/profile',
            'recipient_file':'/run/secrets/recipient','work_directory':'/work'})
        mounts=self.cfg['state_mounts']
        self.docker.call('create','--name',self.journal['container'],'--label','personal-os.backup.id='+self.journal['id'],
            '--label','personal-os.backup.project='+self.cfg['project'],'--network',self.cfg['network'],
            '--read-only','--user','0','--cap-drop','ALL','--cap-add','DAC_OVERRIDE','--security-opt','no-new-privileges',
            '--memory','384m','--cpus','0.75','--pids-limit','64','--tmpfs','/tmp:size=32m,noexec,nosuid',
            '--mount','type=bind,src='+str(work)+',dst=/work',
            '--mount','type=bind,src='+str(work/'worker.json')+',dst=/run/secrets/backup_config,readonly',
            '--mount','type=bind,src='+self.cfg['dsn_file']+',dst=/run/secrets/database_url,readonly',
            '--mount','type=bind,src='+self.cfg['recipient_file']+',dst=/run/secrets/recipient,readonly',
            '--mount','type=bind,src='+mounts['vault']+',dst=/sources/vault,readonly',
            '--mount','type=bind,src='+mounts['profile']+',dst=/sources/profile,readonly',self.cfg['image'])
    def create_worker(self,work):
        if self.calendar:return self.create_calendar_worker(work)
        streaming=self.cfg.get('delivery_mode')=='stream_v2'
        if streaming:
            if not (work/'snapshot').exists():
                # A capture marker can predate durable manifest/blob-reference
                # commit. It must not resume collectors during a NEW capture.
                (work/'captured.json').unlink(missing_ok=True)
                self.cleanup_plaintext(work)
            # Only the absent/stopped owned worker is recreated. These are
            # transport controls, not recovery data or immutable originals.
            for name in ('stream-start.json','stream.json','result.json','ciphertext.fifo'):
                path=work/name
                if path.is_symlink():raise ValueError('unexpected_staging_symlink')
                path.unlink(missing_ok=True)
        write_json(work/'recovery.json',self.cfg['recovery'])
        write_json(work/'worker.json',{'id':self.journal['id'],'dsn_file':'/run/secrets/database_url',
            'files_directory':'/data/files','recovery_config_file':'/work/recovery.json',
            'recipient_file':'/run/secrets/recipient','work_directory':'/work','dump_timeout_seconds':dump_timeout(self.cfg),
            **({'mode':'stream_v2'} if streaming else {})})
        args=['create','--name',self.journal['container'],'--label','personal-os.backup.id='+self.journal['id'],
            '--label','personal-os.backup.project='+self.cfg['project'],'--network',self.cfg['network'],
            '--read-only','--user','0','--cap-drop','ALL','--cap-add','DAC_OVERRIDE','--security-opt','no-new-privileges',
            '--memory','384m','--cpus','0.75','--pids-limit','64','--tmpfs','/tmp:size=32m,noexec,nosuid',
            '--mount','type=bind,src='+str(work)+',dst=/work',
            '--mount','type=bind,src='+str(work/'worker.json')+',dst=/run/secrets/backup_config,readonly',
            '--mount','type=bind,src='+self.cfg['dsn_file']+',dst=/run/secrets/database_url,readonly',
            '--mount','type=bind,src='+self.cfg['recipient_file']+',dst=/run/secrets/recipient,readonly',
            '--mount','type=volume,src='+self.cfg['files_volume']+',dst=/data,readonly']
        for mount in self.cfg['mounts']:
            args+=['--mount','type=bind,src='+mount['source']+',dst=/sources/'+mount['name']+',readonly']
        self.docker.call(*args,self.cfg['image'])
    def collect(self,work):
        if self.journal['phase']=='stopping':
            for saved in self.journal['stopped']:
                info=self.docker.inspect(saved['id'])
                if info is None:raise RuntimeError('collector_missing_during_stop')
                if info['State']['Running']:self.docker.call('stop','--time','25',saved['id'])
            self.journal['phase']='capturing';self.save()
        info=self.check_worker(self.docker.inspect(self.journal['container']))
        if info is None:
            if (work/'captured.json').exists() or (work/'result.json').exists():raise RuntimeError('backup_container_disappeared')
            # Only authoritative absence permits creating a worker. A Docker
            # daemon error raises above and preserves the journal for retry.
            self.create_worker(work);info=self.check_worker(self.docker.inspect(self.journal['container']))
        if info['State']['Status']=='created':self.docker.call('start',self.journal['container'])
        while True:
            marker=work/'captured.json'
            if marker.exists():
                if read_json(marker)!={'id':self.journal['id'],'status':'capture_complete'}:raise RuntimeError('invalid_capture_marker')
                self.resume_collectors()
            info=self.check_worker(self.docker.inspect(self.journal['container']))
            if info is None:raise RuntimeError('backup_container_disappeared')
            if not info['State']['Running']:
                if info['State']['ExitCode']!=0:raise RuntimeError('backup_capture_failed')
                self.resume_collectors();break
            if self.clock()-self.journal['created']>self.cfg['timeout_seconds']:raise RuntimeError('backup_capture_timeout')
            self.sleep(POLL_SECONDS)
        self.journal['phase']='upload';self.save()
    def cleanup_plaintext(self,work):
        # Only this job's closed container work directory is eligible.
        for p in work.iterdir():
            if p.name=='snapshot' or p.name.startswith('.backup-'):
                if p.is_symlink():raise ValueError('unexpected_staging_symlink')
                shutil.rmtree(p)
    def remove_abandoned(self,work):
        # The next run takes a NEW snapshot under a new ID, so nothing in this
        # undelivered job directory is reusable: partial/complete ciphertext,
        # age temporaries and private worker inputs. Local retention counts
        # only delivered jobs, so leaving any of it would leak disk shared with
        # the database. The journal is removed only after this succeeds.
        if work.is_symlink():raise ValueError('unexpected_staging_symlink')
        # Defensive: a delivered recovery point is never an abandoned capture.
        if (work/'delivery.json').exists():raise RuntimeError('invalid_backup_journal')
        if work.exists():shutil.rmtree(work)
    def run_stream(self,work):
        if (work/'delivery.json').exists():
            # Receipt was durably committed before a crash during owned staging
            # cleanup. Complete bookkeeping without taking another live capture.
            metadata=read_json(work/'stream.json');receipt=read_json(work/'delivery.json')
            if (receipt.get('v')!=2 or receipt.get('status')!='stored' or receipt.get('id')!=self.journal['id'] or
                receipt.get('snapshot_sha256')!=metadata.get('snapshot_sha256')):raise RuntimeError('invalid_backup_receipt')
            self.resume_collectors();self.cleanup_plaintext(work)
            return self.finish(work,{**receipt,'captured_at':metadata['captured_at']},receipt)
        if self.journal['phase']=='stopping':
            for saved in self.journal['stopped']:
                info=self.docker.inspect(saved['id'])
                if info is None:raise RuntimeError('collector_missing_during_stop')
                if info['State']['Running']:self.docker.call('stop','--time','25',saved['id'])
            self.journal['phase']='capturing';self.save()
        info=self.check_worker(self.docker.inspect(self.journal['container']))
        if info is not None and not info['State']['Running'] and info['State']['Status']!='created':
            self.docker.call('rm',self.journal['container']);info=None
        if info is None:
            if not (work/'snapshot').exists():
                if self.journal['phase']=='stream_upload':raise RuntimeError('backup_capture_missing')
                # The previous marker may already have resumed some collectors
                # before a reboot lost the uncommitted capture. Record and stop
                # the currently running owned collectors before starting anew.
                saved={item['id']:item for item in self.journal['stopped']}
                saved.update({item['id']:item for item in self.docker.running_collectors(self.cfg['project'])})
                self.journal['stopped']=list(saved.values());self.save()
                for item in self.journal['stopped']:
                    current=self.docker.inspect(item['id'])
                    if current is None:raise RuntimeError('collector_missing_during_stop')
                    if current['State']['Running']:self.docker.call('stop','--time','25',item['id'])
            self.create_worker(work);info=self.check_worker(self.docker.inspect(self.journal['container']))
        if info['State']['Status']=='created':self.docker.call('start',self.journal['container'])
        started=self.clock()
        while not (work/'stream.json').exists():
            if (work/'captured.json').exists():
                if read_json(work/'captured.json')!={'id':self.journal['id'],'status':'capture_complete'}:
                    raise RuntimeError('invalid_capture_marker')
                self.resume_collectors()
            info=self.check_worker(self.docker.inspect(self.journal['container']))
            if info is None:raise RuntimeError('backup_container_disappeared')
            if not info['State']['Running']:raise RuntimeError('backup_capture_failed')
            if self.clock()-started>self.cfg['timeout_seconds']:raise RuntimeError('backup_capture_timeout')
            self.sleep(POLL_SECONDS)
        self.resume_collectors()
        metadata=read_json(work/'stream.json')
        if (set(metadata)-{'backup_fence'}!={'v','id','snapshot_sha256','max_bytes','captured_at'} or metadata['v']!=2 or
            metadata['id']!=self.journal['id'] or type(metadata['max_bytes']) is not int or metadata['max_bytes']<64 or
            not re.fullmatch('[a-f0-9]{64}',metadata['snapshot_sha256']) or
            ('backup_fence' in metadata and metadata['backup_fence']!=self.journal['id'])):
            raise RuntimeError('invalid_backup_result')
        # This durable phase retains the exact DB/session manifest for a retry.
        # No source-side ciphertext exists, and no further collector stop is needed.
        self.journal['phase']='stream_upload';self.save();self.status('uploading')
        def start(encode):write_json(work/'stream-start.json',{'action':'start' if encode else 'skip'})
        async def completed():
            # Runs inside the upload deadline: the local result.json stat stays at 0.1 s, the docker
            # inspect runs once per POLL_SECONDS.
            loop=asyncio.get_running_loop();inspected=None
            while not (work/'result.json').exists():
                if inspected is None or loop.time()-inspected>=POLL_SECONDS:
                    inspected=loop.time()
                    info=self.check_worker(self.docker.inspect(self.journal['container']))
                    if info is None or not info['State']['Running']:
                        if (work/'result.json').exists():break
                        raise RuntimeError('backup_capture_failed')
                await asyncio.sleep(.1)
            result=read_json(work/'result.json')
            if (result.get('v')!=2 or result.get('id')!=metadata['id'] or result.get('status')!='encrypted' or
                result.get('snapshot_sha256')!=metadata['snapshot_sha256']):raise RuntimeError('invalid_backup_result')
            return result
        def stopped():
            if (work/'result.json').exists():return True
            info=self.check_worker(self.docker.inspect(self.journal['container']))
            return info is None or not info['State']['Running']
        receipt,waited=self.deliver(lambda:asyncio.run(self.stream_send(work/'ciphertext.fifo',metadata,self.cfg['transfer'],
                                          self.cfg['timeout_seconds'],start,completed,stopped=stopped)),
                                    lambda:(work/'stream-start.json').exists())
        started+=waited  # The capture deadline never counts the receiver's busy answers and the waits.
        # A failed/changed source cannot publish a terminal frame. A duplicate
        # receipt instead asks the worker to exit without re-encryption.
        while True:
            info=self.check_worker(self.docker.inspect(self.journal['container']))
            if info is None:raise RuntimeError('backup_container_disappeared')
            if not info['State']['Running']:
                if info['State']['ExitCode']!=0:raise RuntimeError('backup_capture_failed')
                break
            if self.clock()-started>self.cfg['timeout_seconds']:raise RuntimeError('backup_capture_timeout')
            self.sleep(POLL_SECONDS)
        if (receipt.get('v')!=2 or receipt.get('id')!=metadata['id'] or receipt.get('status')!='stored' or
            receipt.get('snapshot_sha256')!=metadata['snapshot_sha256']):raise RuntimeError('invalid_backup_receipt')
        result={**receipt,'captured_at':metadata['captured_at']}
        # Mark delivery durably BEFORE dropping the reusable private capture.
        write_json(work/'delivery.json',receipt)
        self.cleanup_plaintext(work);self.finish(work,result,receipt)
    def release_fence(self,work):
        if self.calendar:return  # The calendar capture opens no attachment fence.
        metadata=read_json(work/('stream.json' if (work/'stream.json').exists() else 'result.json'))
        if 'backup_fence' not in metadata:return  # Pre-lifecycle deliveries remain compatible.
        if metadata['backup_fence']!=self.journal['id']:raise RuntimeError('backup_capture_unfenced')
        expected={'id':self.journal['id'],'backup_fence':self.journal['id'],'status':'fence_released'}
        self.fence_action(work,'release',[expected],'backup_fence_release_failed')
    def fence_action(self,work,kind,results,error):
        """Run the worker's ``<kind>_fence`` action for this job; return its marker, one of ``results``."""
        marker=work/{'release':'fence-released.json','abandon':'fence-abandoned.json'}[kind]
        config=work/('fence-'+kind+'.json');action=kind+'_fence'
        done=marker.exists()
        if done and read_json(marker) not in results:raise RuntimeError(error)
        # A separate, labelled operator action receives only DB credentials and
        # this job's directory. No provider sessions, originals, SSH key or socket.
        name='pos-backup-'+kind+'-'+self.journal['id'].lower()
        def inspect():
            info=self.check_worker(self.docker.inspect(name))
            if info is not None and info['Config'].get('Labels',{}).get('personal-os.backup.action')!=action:
                raise RuntimeError('backup_container_mismatch')
            return info
        info=inspect()
        if done and info is None:return read_json(marker)
        if info is not None and not info['State']['Running'] and info['State']['Status']!='created':
            self.docker.call('rm',name);info=None
            if done:return read_json(marker)
        if info is None:
            write_json(config,{'action':action,'id':self.journal['id'],
                'dsn_file':'/run/secrets/database_url','work_directory':'/work'})
            self.docker.call('create','--name',name,'--label','personal-os.backup.id='+self.journal['id'],
                '--label','personal-os.backup.project='+self.cfg['project'],
                '--label','personal-os.backup.action='+action,'--network',self.cfg['network'],
                '--read-only','--user','0','--cap-drop','ALL','--security-opt','no-new-privileges',
                '--memory','128m','--cpus','0.25','--pids-limit','32','--tmpfs','/tmp:size=8m,noexec,nosuid',
                '--mount','type=bind,src='+str(work)+',dst=/work',
                '--mount','type=bind,src='+str(config)+',dst=/run/secrets/backup_config,readonly',
                '--mount','type=bind,src='+self.cfg['dsn_file']+',dst=/run/secrets/database_url,readonly',
                self.cfg['image'],'python','-m','messaging_service.backup_worker','--config','/run/secrets/backup_config')
            info=inspect()
        if info is None:raise RuntimeError('backup_container_disappeared')
        if info['State']['Status']=='created':self.docker.call('start',name)
        started=self.clock()
        while True:
            info=inspect()
            if info is None:raise RuntimeError('backup_container_disappeared')
            if not info['State']['Running']:
                if info['State'].get('ExitCode')!=0:raise RuntimeError(error)
                break
            if self.clock()-started>60:raise RuntimeError(error)
            self.sleep(POLL_SECONDS)
        if not marker.exists() or read_json(marker) not in results:raise RuntimeError(error)
        self.docker.call('rm',name)
        return read_json(marker)
    def settle_fence_action(self,kind):
        """Stop and remove this job's leftover ``<kind>_fence`` container; its outcome is read back later."""
        release_name='pos-backup-'+kind+'-'+self.journal['id'].lower()
        release=self.check_worker(self.docker.inspect(release_name))
        if release is not None:
            if release['Config'].get('Labels',{}).get('personal-os.backup.action')!=kind+'_fence':
                raise RuntimeError('backup_container_mismatch')
            if release['State']['Running']:self.docker.call('stop','--time','25',release_name)
            release=self.check_worker(self.docker.inspect(release_name))
            if release is None or release['State']['Running']:raise RuntimeError('backup_shutdown_unverified')
            self.docker.call('rm',release_name)
    def abandon(self,work):
        """Settle a stopped capture: keep a published one, else end exactly its own fence, then remove it."""
        if work.is_symlink():raise ValueError('unexpected_staging_symlink')
        if self.journal['phase']!='abandoning':
            # Defensive: a delivered recovery point is never an abandoned capture.
            if (work/'delivery.json').exists():raise RuntimeError('invalid_backup_journal')
            if self.cfg.get('delivery_mode')=='stream_v2':
                # The published reference snapshot is reused by the retry under
                # this ID, and its fence must stay open until that delivery.
                if (work/'snapshot').exists():return
            elif (work/'result.json').exists():
                # Encryption finished before the phase update; deliver it on retry.
                # Staged plaintext alone copied its originals and is not reusable.
                self.journal['phase']='upload';self.save();return
            # Durable decision first: a lost acknowledgement is read back, never guessed.
            self.journal['phase']='abandoning';self.save()
        jid=self.journal['id']
        if self.calendar:
            # No fence to end: removing the undelivered job is the whole abandonment.
            self.remove_abandoned(work)
            status=read_json(self.status_path) if self.status_path.exists() else {}
            status.update(abandoned_id=jid);write_json(self.status_path,status)
            return self.journal_path.unlink()
        results=[{'id':jid,'backup_fence':jid,'status':s} for s in ('fence_released','fence_absent')]
        try:
            # Nothing here is reusable. Every attempt reads the outcome back from
            # the database, so a leftover helper (even one created but never
            # started) goes first, and the new one sees only its config and marker.
            self.settle_fence_action('abandon')
            self.remove_abandoned(work);work.mkdir(mode=0o700)
            result=self.fence_action(work,'abandon',results,'backup_fence_retained')
        except Exception as exc:
            self.status('error','backup_fence_retained');raise RuntimeError('backup_fence_retained') from exc
        shutil.rmtree(work)
        # The read-back settles the retained error before the journal goes.
        status=read_json(self.status_path) if self.status_path.exists() else {}
        if status.get('error_code')=='backup_fence_retained':status['error_code']='backup_job_failed'
        status.update(abandoned_id=jid,abandoned_fence=result['status']);write_json(self.status_path,status)
        self.journal_path.unlink()
    def finish(self,work,result,receipt):
        write_json(work/'delivery.json',receipt)
        self.release_fence(work)
        self.status('ok')
        status=read_json(self.status_path)
        status.update(last_success_at=datetime.now(timezone.utc).isoformat(),last_success_id=result['id'],
                      ciphertext_bytes=result['bytes'],ciphertext_sha256=result['sha256'],last_snapshot_at=result['captured_at'])
        write_json(self.status_path,status)
        info=self.check_worker(self.docker.inspect(self.journal['container']))
        if info:self.docker.call('rm',self.journal['container'])
        self.settle(self.journal['container'])
        self.journal_path.unlink();self.journal=None
        completed=sorted((p for p in self.root.iterdir() if p.is_dir() and ID.fullmatch(p.name) and (p/'delivery.json').is_file()),
                         key=lambda p:p.name,reverse=True)
        for old in completed[self.cfg['keep_local']:]:shutil.rmtree(old)
    def run(self):
        self.prepare();work=self.root/self.journal['id']
        try:
            if self.journal['phase']=='abandoning':
                # Read back the retained release before taking a new capture.
                self.abandon(work);self.prepare();work=self.root/self.journal['id']
            if self.cfg.get('delivery_mode')=='stream_v2':
                return self.run_stream(work)
            if self.journal['phase']!='upload':self.collect(work)
            result=read_json(work/'result.json')
            path=work/'bundle.tar.age'
            if result.get('id')!=self.journal['id'] or result.get('status')!='encrypted' or path.is_symlink():
                raise RuntimeError('invalid_backup_result')
            # A backup image from before ANYM-607 names no manifest hash: its bundle could never be verified.
            if self.calendar and not re.fullmatch('[a-f0-9]{64}',str(result.get('snapshot_sha256'))):
                raise RuntimeError('invalid_backup_result')
            with path.open('rb') as f:
                if path.stat().st_size!=result['bytes'] or hashlib.file_digest(f,'sha256').hexdigest()!=result['sha256']:
                    raise RuntimeError('local_backup_corrupt')
            self.cleanup_plaintext(work);self.status('uploading')
            receipt,_=self.deliver(lambda:asyncio.run(self.send(path,result,self.cfg['transfer'],self.cfg['timeout_seconds'])))
            self.finish(work,result,receipt)
        except BaseException as exc:
            self.status('error',str(exc) if str(exc) in SAFE_ERRORS else 'backup_job_failed')
            raise
    def recover_stop(self):
        """systemd ExecStopPost: settle the owned container, then resume its collectors."""
        if not self.journal_path.exists():
            if self.calendar and os.path.lexists(self.unsettled_path):self.settle(read_json(self.unsettled_path)['container'])
            return
        self.load()
        if not self.calendar:
            for kind in ('release','abandon'):self.settle_fence_action(kind)
        info=self.check_worker(self.docker.inspect(self.journal['container']))
        if info and info['State']['Running']:
            self.docker.call('stop','--time','25',self.journal['container'])
            info=self.check_worker(self.docker.inspect(self.journal['container']))
            if info is None or info['State']['Running']:raise RuntimeError('backup_shutdown_unverified')
        self.resume_collectors()
        if self.journal['phase']=='stream_upload':
            # Retain the consistent metadata capture. Encryption can safely be
            # restarted from its immutable sources, unlike consumed v1 staging.
            if info:self.docker.call('rm',self.journal['container'])
        elif self.journal['phase']!='upload':
            # A failed cold capture must be retried from a NEW snapshot, after
            # collectors have recovered; never publish its partial plaintext.
            if info:self.docker.call('rm',self.journal['container'])
            self.abandon(self.root/self.journal['id'])
        # Preserve the actionable error from the initiating process.
        if not self.status_path.exists() or read_json(self.status_path).get('state')!='error':
            self.status('error','backup_job_failed')
        self.settle(self.journal['container'])


@contextmanager
def hold_locks(cfg):
    """The runner's own lock; a calendar run then also messaging's lock (`host_lock`), the one a deploy holds.

    The host lock is waited for at most HOST_LOCK_WAIT_SECONDS (`host_lock_busy`); both are held until the run ends.
    """
    fds=[]
    try:
        fds.append(os.open(Path(cfg['directory'])/'.runner.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600))
        try:fcntl.flock(fds[0],fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('backup_job_already_running') from None
        if cfg.get('profile')=='calendar':
            fds.append(os.open(cfg['host_lock'],os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600))
            deadline=time.monotonic()+HOST_LOCK_WAIT_SECONDS
            while True:
                try:fcntl.flock(fds[1],fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    left=deadline-time.monotonic()
                    if left<=0:raise RuntimeError('host_lock_busy') from None
                    time.sleep(min(HOST_LOCK_POLL_SECONDS,left))
        yield
    finally:
        for fd in reversed(fds):os.close(fd)


def check(cfg):
    """Read-only: does an unfinished journal exist? No Docker call, no lock, nothing created."""
    profile=cfg.get('profile','messaging');journal=Path(cfg['directory'])/'journal.json'
    if not os.path.lexists(journal):
        print(json.dumps({'status':'idle','profile':profile}),flush=True);return 0
    data=read_json(journal)
    print(json.dumps({'status':'unfinished','profile':profile,'id':data.get('id'),'phase':data.get('phase'),
                      'stopped':len(data.get('stopped') or [])}),flush=True)
    return 3


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True);parser.add_argument('--recover-stop',action='store_true')
    parser.add_argument('--check',action='store_true',help='report an unfinished journal (exit 3); read-only')
    args=parser.parse_args();os.umask(0o077)
    if args.check:raise SystemExit(check(config(args.config)))
    def interrupted(*_):raise InterruptedError('backup_interrupted')
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
    runner=Runner(config(args.config))
    owns_lock=False;code='backup_job_failed'
    try:
        with hold_locks(runner.cfg):
            owns_lock=True
            try:
                if args.recover_stop:runner.recover_stop()
                else:runner.run()
            except Exception:
                try:runner.recover_stop()
                except Exception:pass  # Journal remains for ExecStopPost / next operator run.
                raise
    except Exception as exc:
        if not owns_lock and str(exc)=='host_lock_busy':
            code='host_lock_busy'
            try:runner.status('error',code)
            except Exception:pass
        print(json.dumps({'status':'error','code':code},separators=(',',':')),flush=True)
        raise SystemExit(1) from None


if __name__=='__main__':main()
