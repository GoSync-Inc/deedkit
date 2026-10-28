"""Root-only independent R2 restore/rotation coordinator, invoked by a timer.

The producer's SSH command cannot invoke this module. Cloud authority and trusted
proofs remain on the receiver; the isolated helper sees only stdin, key and its
own plaintext work. A successful upload is never considered restore evidence.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
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
import subprocess
import sys
import time
import traceback

from . import backup_r2_receiver as r2
from . import backup_receiver as local

GIB = 1024**3
POSTGRES_IMAGE = 'postgres:18-bookworm@sha256:afc7e2d441324c0388fa80c3d24f733b4194a4eb7f47dd8ee2b08eb1a24a647c'
LABEL = 'org.deedkit.backup-verifier'
DATABASE = 'deedkit_verify_restore_test'
PG_DATA_PATH = '/var/lib/postgresql'
HELPER_TMPFS = {'/tmp':'rw,noexec,nosuid,size=64m',
                PG_DATA_PATH:'ro,noexec,nosuid,nodev,size=1m,mode=0700'}


def now():return datetime.now(timezone.utc).isoformat()


def private_file(path, limit=65536):
    """Validate held root evidence, including every ancestor and leaf identity."""
    path=Path(path)
    with local.trusted_directory(path.parent) as parent:
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        with os.fdopen(fd,'rb') as source:
            info=os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or
                    info.st_mode&0o077 or info.st_size>limit):raise ValueError('private_root_file_required')
            return source.read(limit+1)


def load_config(path):
    if os.geteuid()!=0:raise ValueError('root_verifier_required')
    value=r2.unique_json(private_file(path,16384))
    required={'v','receiver_config_file','directory','identity_file','protected_file','helper_image','helper_revision','postgres_image'}
    defaults={'max_plaintext_bytes':40*GIB,'postgres_headroom_bytes':8*GIB,'reserve_bytes':2*GIB,'timeout_seconds':21600}
    if (not isinstance(value,dict) or not required<=set(value) or set(value)-required-set(defaults) or
            type(value['v']) is not int or value['v']!=1):raise ValueError('invalid_verifier_config')
    value={**defaults,**value}
    if (not isinstance(value['helper_image'],str) or not re.fullmatch(r'sha256:[a-f0-9]{64}',value['helper_image']) or
            not isinstance(value['helper_revision'],str) or not re.fullmatch('[a-f0-9]{40}',value['helper_revision']) or
            value['postgres_image']!=POSTGRES_IMAGE):raise ValueError('immutable_reviewed_images_required')
    if (any(type(value[k]) is not int for k in defaults) or
            not GIB<=value['max_plaintext_bytes']<=128*GIB or
            not 2*GIB<=value['postgres_headroom_bytes']<=64*GIB or
            not GIB<=value['reserve_bytes']<=32*GIB or not 300<=value['timeout_seconds']<=21600):
        raise ValueError('invalid_verifier_limits')
    for key in ('receiver_config_file','directory','identity_file','protected_file'):
        if not isinstance(value[key],str) or not Path(value[key]).is_absolute() or '..' in Path(value[key]).parts:
            raise ValueError('invalid_verifier_path')
    with local.trusted_directory(value['directory']) as directory:
        if os.fstat(directory).st_mode&0o077:raise ValueError('private_verifier_directory_required')
    receiver=r2.load_config(value['receiver_config_file'])
    state=Path(receiver['state_directory']);directory=Path(value['directory'])
    # Config, key and marker can never be selected/replaced by the upload UID or
    # by the helper's RW payload mount. Root evidence must not share their leaf.
    for path in [directory,Path(value['identity_file']),Path(value['protected_file']),
                 Path(value['receiver_config_file']),Path(receiver['credentials_file'])]:
        if path.is_relative_to(state):raise ValueError('separate_operator_evidence_required')
    if directory.is_relative_to(state) or state.is_relative_to(directory):
        raise ValueError('separate_operator_evidence_required')
    for path in [Path(value['identity_file']),Path(value['protected_file']),Path(value['receiver_config_file']),
                 Path(receiver['credentials_file'])]:
        if path.is_relative_to(directory):raise ValueError('separate_verifier_inputs_required')
    if len({Path(value[k]) for k in ('identity_file','protected_file','receiver_config_file')})!=3:
        raise ValueError('separate_verifier_inputs_required')
    private_file(value['identity_file'])
    with local.trusted_directory(Path(value['protected_file']).parent):pass
    return value


@contextmanager
def verifier_lock(directory):
    with local.trusted_directory(directory) as fd:
        info=os.fstat(fd)
        if info.st_mode&0o077:raise ValueError('private_verifier_directory_required')
        lock=os.open('.verifier.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=fd)
        try:
            info=os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o077:
                raise ValueError('invalid_verifier_lock')
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('verifier_busy') from None
            yield fd
        finally:os.close(lock)


@contextmanager
def deadline(seconds):
    previous=signal.getsignal(signal.SIGALRM);previous_term=signal.getsignal(signal.SIGTERM)
    def expired(*_):r2.cancel(TimeoutError('restore_deadline'))
    def stopped(*_):r2.cancel(InterruptedError('restore_stopped'))
    r2.CANCELLED.clear();signal.signal(signal.SIGALRM,expired);signal.signal(signal.SIGTERM,stopped)
    signal.setitimer(signal.ITIMER_REAL,seconds)
    try:yield
    finally:
        signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,previous);signal.signal(signal.SIGTERM,previous_term)
        r2.CANCELLED.clear()


def names(run):
    if not isinstance(run,str) or not re.fullmatch('[a-f0-9]{32}',run):raise ValueError('invalid_verifier_journal')
    base='deedkit-verify-'+run
    return {'helper':base+'-helper','postgres':base+'-postgres','volume':base+'-data','network':base+'-net'}


def validate_image_volumes(image):
    # The backup target inherits this one PG18 VOLUME. Override it explicitly;
    # unknown declarations must never silently allocate writable host storage.
    volumes=image.get('Config',{}).get('Volumes')
    if volumes is None:volumes={}
    if (not isinstance(volumes,dict) or set(volumes)-{PG_DATA_PATH} or
            any(value!={} for value in volumes.values())):
        raise ValueError('unexpected_restore_image_volume')


def validate_helper_layout(config,run,payload,secret_dir,container):
    expected={'/work':(str(payload),True),'/run/restore/identity':(config['identity_file'],False),
              '/run/restore/config.json':(str(secret_dir/'worker.json'),False)}
    if not isinstance(container,dict):raise ValueError('unexpected_restore_helper_layout')
    mounts=container.get('Mounts');host=container.get('HostConfig',{})
    if (not isinstance(mounts,list) or len(mounts)!=len(expected) or
            {item.get('Destination') for item in mounts}!=set(expected) or
            any(item.get('Type')!='bind' or
                (item.get('Source'),item.get('RW'))!=expected[item['Destination']] for item in mounts) or
            host.get('Tmpfs')!=HELPER_TMPFS or host.get('ReadonlyRootfs') is not True or
            host.get('PortBindings') or host.get('Privileged') is True or
            host.get('NetworkMode')!=names(run)['network']):
        raise ValueError('unexpected_restore_helper_layout')


class Docker:
    """Small, argument-only Docker adapter. It never prints secret diagnostics."""
    def __init__(self,timeout=21600):
        self.ends=time.monotonic()+timeout;self.disk_roots=[];self.reserve_bytes=0
    def check_reserve(self):
        if any(shutil.disk_usage(path).free<self.reserve_bytes for path in self.disk_roots):
            raise ValueError('restore_disk_capacity')
    def remaining(self):
        seconds=self.ends-time.monotonic()
        if seconds<=0:raise TimeoutError('restore_deadline')
        return seconds
    def command(self,*args):
        try:
            result=subprocess.run(['docker',*args],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL,timeout=min(120,self.remaining()),check=True)
        except subprocess.CalledProcessError:raise RuntimeError('restore_docker_failed') from None
        if len(result.stdout)>8*1024**2:raise ValueError('restore_docker_response_limit')
        return result.stdout
    def inspect_image(self,image):
        value=r2.unique_json(self.command('image','inspect',image))
        if not isinstance(value,list) or len(value)!=1:raise ValueError('invalid_restore_image')
        return value[0]
    def preflight(self,config):
        helper=self.inspect_image(config['helper_image'])
        labels=helper.get('Config',{}).get('Labels',{}) or {}
        if (helper.get('Id')!=config['helper_image'] or labels.get('org.opencontainers.image.revision')!=config['helper_revision'] or
                labels.get('org.opencontainers.image.source')!='https://github.com/GoSync-Inc/deedkit'):
            raise ValueError('unreviewed_restore_image')
        validate_image_volumes(helper)
        pg=self.inspect_image(config['postgres_image'])
        expected_digest=config['postgres_image'].split('@',1)[1]
        if not any(item in {'postgres@'+expected_digest,'docker.io/library/postgres@'+expected_digest} for item in pg.get('RepoDigests',[])):
            raise ValueError('unreviewed_postgres_image')
        validate_image_volumes(pg)
        root=self.command('info','--format','{{.DockerRootDir}}').decode().strip()
        if not Path(root).is_absolute():raise ValueError('invalid_docker_data_root')
        return root
    def inspect_owned(self,kind,name,run):
        category='container' if kind in ('helper','postgres') else kind
        args=['ls'] if category=='container' else ['ls']
        if category=='container':args.append('-a')
        raw=self.command(category,*args,'--filter','name=^'+name+'$','--format','{{.Name}}' if category=='volume' else '{{.ID}}').decode().splitlines()
        if not raw:return None
        if len(raw)!=1:raise ValueError('unowned_restore_resource')
        values=r2.unique_json(self.command(category,'inspect',name))
        if not isinstance(values,list) or len(values)!=1:raise ValueError('unowned_restore_resource')
        value=values[0];labels=(value.get('Config',{}).get('Labels') if category=='container' else value.get('Labels')) or {}
        if labels.get(LABEL)!=run or labels.get(LABEL+'.owned')!='1':raise ValueError('unowned_restore_resource')
        if category=='network' and value.get('Internal') is not True:raise ValueError('unowned_restore_resource')
        return value
    def remove(self,kind,name):
        category='container' if kind in ('helper','postgres') else kind
        if category=='container':self.command('container','stop','--time=10',name)
        self.command(category,'rm',*(['--volumes'] if category=='container' else []),name)
    def setup(self,config,run,payload,secret_dir):
        resource=names(run);labels=['--label',LABEL+'='+run,'--label',LABEL+'.owned=1']
        # Persisted names+ownership precede every create. A dropped response can
        # be reconciled by exact label, never by a generic prune.
        self.command('network','create','--internal',*labels,resource['network'])
        self.command('volume','create',*labels,resource['volume'])
        common=['--network',resource['network'],'--restart=no','--log-driver=none','--cpus=1','--pids-limit=256',*labels]
        self.command('container','create','--name',resource['postgres'],*common,'--memory=1g','--memory-swap=1g',
                     '--env-file',str(secret_dir/'postgres.env'),'--network-alias','restore-postgres',
                     '--mount','type=volume,src='+resource['volume']+',dst=/var/lib/postgresql',
                     config['postgres_image'],'postgres','-c','shared_buffers=64MB','-c','work_mem=8MB',
                     '-c','maintenance_work_mem=64MB','-c','max_connections=10','-c','max_parallel_workers=0')
        self.command('container','start',resource['postgres'])
        ready=False
        for _ in range(90):
            try:
                self.command('container','exec',resource['postgres'],'pg_isready','-h','127.0.0.1','-U','restore_owner','-d',DATABASE)
                ready=True;break
            except RuntimeError:time.sleep(1)
        if not ready:raise RuntimeError('restore_postgres_unavailable')
        self.command('container','create','--name',resource['helper'],*common,'--memory=512m','--memory-swap=512m',
                     '--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--tmpfs','/tmp:'+HELPER_TMPFS['/tmp'],
                     '--tmpfs',PG_DATA_PATH+':'+HELPER_TMPFS[PG_DATA_PATH],
                     '--mount','type=bind,src='+str(payload)+',dst=/work',
                     '--mount','type=bind,src='+config['identity_file']+',dst=/run/restore/identity,readonly',
                     '--mount','type=bind,src='+str(secret_dir/'worker.json')+',dst=/run/restore/config.json,readonly',
                     '-i','--entrypoint','python',config['helper_image'],'-m','messaging_service.backup_restore_worker',
                     '--config','/run/restore/config.json')
    def restore(self,config,receiver,client,receipt,run,payload,secret_dir):
        self.setup(config,run,payload,secret_dir)
        helper=self.inspect_owned('helper',names(run)['helper'],run)
        validate_helper_layout(config,run,payload,secret_dir,helper)
        if self.inspect_owned('network',names(run)['network'],run) is None:
            raise ValueError('unexpected_restore_helper_layout')
        process=subprocess.Popen(['docker','container','start','-ai',names(run)['helper']],stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            engine=self
            class Sink:
                def write(self,data):
                    engine.check_reserve();return process.stdin.write(data)
            r2.verify_object(receiver,client,receipt,Sink())
            process.stdin.close()
            while process.poll() is None:
                self.check_reserve()
                try:process.wait(timeout=min(1,self.remaining()))
                except subprocess.TimeoutExpired:continue
            self.check_reserve()
            if process.wait(timeout=self.remaining()):
                failure='isolated_restore_failed'
                try:
                    fact=r2.unique_json(private_file(payload/'failure.json',4096))
                    safe={'restore_decryption_failed','restore_database_failed','restore_snapshot_failed','restore_quarantine_failed'}
                    if (isinstance(fact,dict) and set(fact)=={'v','status','error'} and
                            type(fact['v']) is int and fact['v']==1 and fact['status']=='failed' and fact['error'] in safe):failure=fact['error']
                except (OSError,ValueError):pass
                raise RuntimeError(failure)
        finally:
            if process.stdin and not process.stdin.closed:process.stdin.close()
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)
        # Only facts from the helper payload are read here. Root proof is written
        # to a different path after full ciphertext hashing and successful exit.
        return r2.unique_json(private_file(payload/'result.json',16384))


def cleanup_resources(engine,run):
    resource=names(run)
    for kind in ('helper','postgres','volume','network'):
        if engine.inspect_owned(kind,resource[kind],run) is not None:engine.remove(kind,resource[kind])


def preflight_disk(config,receipt,docker_root):
    wire=receipt['wire']['bytes']
    if wire>config['max_plaintext_bytes']:raise ValueError('restore_plaintext_bound')
    work=Path(config['directory']);docker_root=Path(docker_root)
    required=wire+config['postgres_headroom_bytes']+config['reserve_bytes']
    if os.stat(work).st_dev==os.stat(docker_root).st_dev:
        if shutil.disk_usage(work).free<required:raise ValueError('restore_disk_capacity')
    elif (shutil.disk_usage(work).free<wire+config['reserve_bytes'] or
          shutil.disk_usage(docker_root).free<config['postgres_headroom_bytes']+config['reserve_bytes']):
        raise ValueError('restore_disk_capacity')


def make_proof(receipt,facts,run):
    if (not isinstance(facts,dict) or set(facts)!={'v','status','schema_version','manifest_sha256','checks'} or
            type(facts['v']) is not int or facts['v']!=1 or facts['status']!='verified' or
            type(facts['schema_version']) is not int or facts['schema_version'] not in range(2,51) or
            facts['manifest_sha256']!=receipt['wire']['snapshot_sha256'] or not isinstance(facts['checks'],dict) or
            set(facts['checks'])!=r2.CHECKS or any(v is not True for v in facts['checks'].values())):
        raise ValueError('invalid_helper_result')
    return {'v':1,'type':'independent_restore_verification','status':'verified',
            'backup':{k:receipt['wire'][k] for k in ('id','bytes','sha256','snapshot_sha256')},
            'verification':{'id':run,'verified_at':now(),'schema_version':facts['schema_version'],
                            'manifest_sha256':facts['manifest_sha256'],'checks':facts['checks']}}


def read_marker(receiver,path):
    with local.trusted_directory(Path(path).parent) as directory:
        raw,value=r2.read_root(directory,Path(path).name,missing=True)
    if value is not None:r2.validate_marker(receiver,value)
    return (hashlib.sha256(raw).hexdigest() if raw is not None else None),value


def select_candidate(receiver,client,state_fd,marker):
    objects=r2.inventory(receiver,client)
    if len(objects)>2 or sum(objects.values())>receiver['quota_bytes']:raise ValueError('receiver_quota')
    # Local metadata is untrusted input from upload UID. It can only block us;
    # candidate authority comes from immutable cloud receipt plus full GET hash.
    for name in os.listdir(state_fd):
        if name=='.receiver.lock':continue
        # The cold-copy subdirectory (ANYM-297: its lock, journals and their
        # temporary files) addresses only <prefix>/cold/, which backup
        # inventory, verification and rotation never touch.
        if name=='cold' and stat.S_ISDIR(os.stat(name,dir_fd=state_fd,follow_symlinks=False).st_mode):continue
        if not name.endswith('.attempt.json'):raise ValueError('unresolved_upload_required')
        bid=name[:-len('.attempt.json')];r2.valid_id(bid)
        attempt=r2.validate_attempt(receiver,r2.state_read(state_fd,name),bid)
        if attempt['phase']!='committed':raise ValueError('unresolved_upload_required')
    protected={x['id'] for x in marker['backups']} if marker else set()
    candidates=[];seen=set()
    for key,size in objects.items():
        match=re.fullmatch(re.escape(receiver['prefix']+'/objects/')+r'(.+)-[a-f0-9]{32}\.tar\.age',key)
        if not match:raise ValueError('uncommitted_cloud_object')
        bid=match[1];r2.valid_id(bid)
        receipt=r2.get_receipt(receiver,client,bid)
        if receipt is None or receipt['object_key']!=key or receipt['wire']['bytes']!=size or bid in seen:
            raise ValueError('uncommitted_cloud_object')
        seen.add(bid)
        if bid not in protected:candidates.append(receipt)
        elif next(x for x in marker['backups'] if x['id']==bid)!={k:receipt['wire'][k] for k in ('id','bytes','sha256')}:
            raise ValueError('protected_receipt_mismatch')
    if not protected<=seen:raise ValueError('protected_backup_missing')
    if len(candidates)>1:raise ValueError('ambiguous_restore_candidate')
    return candidates[0] if candidates else None


def validate_journal(receiver,journal):
    if (not isinstance(journal,dict) or set(journal)!={'v','run','destination','receipt','prior_sha256','phase','helper_image','helper_revision','protected_file'} or
            journal['v']!=1 or type(journal['v']) is not int or journal['destination']!=r2.destination(receiver) or
            journal['phase'] not in ('restoring','proof_ready','promoted') or
            (journal['prior_sha256'] is not None and not re.fullmatch('[a-f0-9]{64}',str(journal['prior_sha256'])))):
        raise ValueError('invalid_verifier_journal')
    names(journal['run'])
    r2.validate_receipt(receiver,journal['receipt'],journal['receipt'].get('wire',{}).get('id'))
    return journal


def remove_payload(directory,run):
    # All paths are direct children of a held root-private control directory;
    # symlinks are unlinked, never traversed by shutil.rmtree's fd-safe variant.
    for name in ('payload-'+run,'secrets-'+run):
        path=Path(directory)/name
        try:info=path.lstat()
        except FileNotFoundError:continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid!=0:
            raise ValueError('invalid_restore_workspace')
        shutil.rmtree(path)
    local.fsync_dir(directory)


def write_secret(path,data):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())


def verify_run(config,receiver,client,engine,journal,fd):
    run=journal['run'];directory=Path(config['directory']);payload=directory/('payload-'+run);secret_dir=directory/('secrets-'+run)
    cleanup_resources(engine,run);remove_payload(directory,run)
    docker_root=engine.preflight(config);preflight_disk(config,journal['receipt'],docker_root)
    if isinstance(engine,Docker):engine.disk_roots=[config['directory'],docker_root];engine.reserve_bytes=config['reserve_bytes']
    private_file(config['identity_file'])
    payload.mkdir(mode=0o700);secret_dir.mkdir(mode=0o700)
    password=secrets.token_hex(32)
    write_secret(secret_dir/'postgres.env',('POSTGRES_USER=restore_owner\nPOSTGRES_DB='+DATABASE+'\nPOSTGRES_PASSWORD='+password+'\n').encode())
    worker={'v':1,'identity_file':'/run/restore/identity','work_directory':'/work','database_host':'restore-postgres',
            'database_password':password,'snapshot_sha256':journal['receipt']['wire']['snapshot_sha256'],
            'max_plaintext_bytes':min(config['max_plaintext_bytes'],journal['receipt']['wire']['bytes'])}
    write_secret(secret_dir/'worker.json',r2.encoded(worker));local.fsync_dir(secret_dir);local.fsync_dir(directory)
    facts=engine.restore(config,receiver,client,journal['receipt'],run,payload,secret_dir)
    proof=make_proof(journal['receipt'],facts,run);r2.validate_proof(proof,journal['receipt'])
    proof_name='proof-'+run+'.json'
    local.write_maintenance_json(proof_name,proof,dir_fd=fd)
    journal={**journal,'phase':'proof_ready'};local.write_maintenance_json('active.json',journal,dir_fd=fd)
    return journal


def run(config,*,client=None,engine=None):
    if os.geteuid()!=0:raise ValueError('root_verifier_required')
    receiver=r2.load_config(config['receiver_config_file'])
    client=client if client is not None else r2.make_client(receiver)
    engine=engine if engine is not None else Docker(config['timeout_seconds'])
    with verifier_lock(config['directory']) as fd:
        journal=None
        try:
            with deadline(config['timeout_seconds']):
                _,journal=r2.read_root(fd,'active.json',missing=True)
                if journal is not None:
                    validate_journal(receiver,journal)
                    if any(journal[k]!=config[k] for k in ('helper_image','helper_revision')):
                        raise ValueError('restore_image_changed_during_retry')
                    if journal['protected_file']!=config['protected_file']:raise ValueError('verifier_control_changed_during_retry')
                with local.trusted_directory(receiver['state_directory'],archive=True) as state_fd,r2.shared_lock(state_fd):
                    prior,marker=read_marker(receiver,config['protected_file'])
                    candidate=select_candidate(receiver,client,state_fd,marker)
                    if journal is None:
                        if candidate is None:
                            status={'v':1,'status':'idle','checked_at':now()};local.write_maintenance_json('status.json',status,dir_fd=fd);return status
                        journal={'v':1,'run':secrets.token_hex(16),'destination':r2.destination(receiver),'receipt':candidate,
                                 'prior_sha256':prior,'protected_file':config['protected_file'],'phase':'restoring','helper_image':config['helper_image'],'helper_revision':config['helper_revision']}
                        local.write_maintenance_json('active.json',journal,dir_fd=fd)
                    # Even after a marker switch, exact committed candidate remains
                    # available by ID. Never silently adopt another new capture.
                    actual=r2.get_receipt(receiver,client,journal['receipt']['wire']['id'])
                    if actual!=journal['receipt']:raise ValueError('restore_candidate_changed')
                    if journal['phase']=='restoring':journal=verify_run(config,receiver,client,engine,journal,fd)
                    else:
                        _,proof=r2.read_root(fd,'proof-'+journal['run']+'.json')
                        r2.validate_proof(proof,journal['receipt'])
                    cleanup_resources(engine,journal['run']);remove_payload(config['directory'],journal['run'])
                # Existing maintenance takes the same upload lock and independently
                # rechecks full ciphertext, immutable receipts and root marker CAS.
                proof_path=Path(config['directory'])/('proof-'+journal['run']+'.json')
                if journal['prior_sha256'] is None:
                    result=r2.protect(receiver,config['protected_file'],proof_path,client=client)
                else:
                    result=r2.rotate(receiver,config['protected_file'],proof_path,
                                     expected_protected_sha256=journal['prior_sha256'],client=client)
                journal={**journal,'phase':'promoted'};local.write_maintenance_json('active.json',journal,dir_fd=fd)
                status={'v':1,'status':'verified','backup_id':journal['receipt']['wire']['id'],'verification_id':journal['run'],'verified_at':now()}
                local.write_maintenance_json('last-success.json',status,dir_fd=fd)
                local.write_maintenance_json('status.json',status,dir_fd=fd)
                os.unlink('active.json',dir_fd=fd);local.fsync_dir(fd)
                return status
        except BaseException as error:
            # Generic safe codes only; SDK errors, DSNs and stderr never enter
            # durable status. Keep journal/proof and the old protected recovery point.
            cleanup_ok=True
            if isinstance(engine,Docker):engine=Docker(120)
            if journal is not None:
                try:cleanup_resources(engine,journal['run']);remove_payload(config['directory'],journal['run'])
                except Exception:cleanup_ok=False
            failure={'v':1,'status':'failed','error':failure_code(error),'failed_at':now(),
                     'cleanup_complete':cleanup_ok,'diagnostic':describe(error)}
            local.write_maintenance_json('status.json',failure,dir_fd=fd)
            # A later busy/idle/verified run overwrites status.json; keep the last failure's cause.
            try:local.write_maintenance_json('last-failure.json',failure,dir_fd=fd)
            except Exception:pass
            raise error


def failure_code(error):
    if isinstance(error,InterruptedError):return 'restore_stopped'
    if isinstance(error,(TimeoutError,subprocess.TimeoutExpired)):return 'restore_deadline'
    safe={'restore_disk_capacity','restore_plaintext_bound','restore_quarantine_failed','restore_database_failed',
          'restore_snapshot_failed','restore_decryption_failed','restore_bundle_failed','restore_manifest_mismatch',
          'receiver_busy','verifier_busy','unresolved_upload_required','ambiguous_restore_candidate',
          'restore_candidate_changed','restore_image_changed_during_retry','verifier_control_changed_during_retry','protected_marker_changed',
          'unreviewed_restore_image','unreviewed_postgres_image','unowned_restore_resource','invalid_helper_result',
          'uploader_lock_initialization_required','uncommitted_cloud_object','receiver_quota','protected_backup_missing',
          'invalid_verifier_journal','private_root_file_required','restore_docker_failed','restore_postgres_unavailable',
          'isolated_restore_failed','stored_backup_corrupt','storage_unavailable','unexpected_restore_image_volume','unexpected_restore_helper_layout'}
    return str(error) if str(error) in safe else 'independent_restore_failed'


def code_location(filename):
    parts=Path(filename).parts
    if 'site-packages' in parts:return '/'.join(parts[len(parts)-parts[::-1].index('site-packages'):])
    return '/'.join(parts[-2:]) if 'messaging_service' in parts[-2:-1] else Path(filename).name


def describe(error):
    """Exception classes and code frames only. Messages, args and locals can hold
    signed URLs, DSNs or archive data and never enter status or logs."""
    try:
        chain=[];seen=set()
        while error is not None and id(error) not in seen and len(chain)<4:
            seen.add(id(error))
            frames=[[code_location(frame.f_code.co_filename),line,frame.f_code.co_name]
                    for frame,line in traceback.walk_tb(error.__traceback__)]
            chain.append({'type':type(error).__module__+'.'+type(error).__qualname__,'frames':frames[-16:]})
            error=error.__cause__ or error.__context__
        return chain
    except Exception:return [{'type':'diagnostic_unavailable','frames':[]}]


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',required=True)
    args=parser.parse_args(argv)
    try:run(load_config(args.config))
    except Exception as error:
        print(json.dumps({'error':failure_code(error),'diagnostic':describe(error)},separators=(',',':')),file=sys.stderr);return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
