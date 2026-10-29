"""Root-only independent R2 restore/rotation coordinator, invoked by a timer.

The producer's SSH command cannot invoke this module. Cloud authority and trusted
proofs remain on the receiver; the isolated helper sees only stdin, key and its
own plaintext work. A successful upload is never considered restore evidence.
"""
import argparse
from contextlib import ExitStack,contextmanager
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
# The pinned image's postgres user and group (Debian image, uid/gid 999). A run's data directory on
# `postgres_data_directory` (ANYM-634) is 0700 and theirs; the root 0700 parent shields it on the host.
POSTGRES_UID = 999
HELPER_TMPFS = {'/tmp':'rw,noexec,nosuid,size=64m',
                PG_DATA_PATH:'ro,noexec,nosuid,nodev,size=1m,mode=0700'}
# Backup schema versions this verifier accepts from its helper (`capabilities` reports them).
SCHEMAS = range(2,61)
# Host admission (ANYM-609): every instance on the receiver shares this root-private directory.
# It holds the one heavy-restore lock, the holder's admission record and the pin step's journal.
HOST_DIRECTORY = '/var/lib/deedkit-backup-verify'
HOST_LOCK = 'host.lock'
ADMISSION = 'admission.json'
PINS_SWITCH = 'pins-switch.json'
PINS_PREVIOUS = 'pins-previous.json'
VERIFY_WAIT_SECONDS = 3600
# A new run that meets an upload's receiver lock retries it (ANYM-632): every BUSY_POLL_SECONDS
# for at most `busy_wait_seconds` (verifier config, 0..BUSY_WAIT_MAX), then ends `busy`.
BUSY_WAIT_SECONDS = 300
BUSY_WAIT_MAX = 900
BUSY_POLL_SECONDS = 15
# Every verifier config on this receiver, for the pin step: {"v": 1, "verifier_configs": [paths]}.
INSTANCES_FILE = Path('/etc/deedkit-backup-r2/instances.json')


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
    # Optional `profile` (ANYM-606): absent is messaging, exactly as before; `calendar` selects the calendar
    # helper checks and proof. It comes only from this root config and must equal its receiver config's.
    # Optional `host_directory` and `verify_wait_seconds` (ANYM-609) are read through host_settings;
    # optional `busy_wait_seconds` (ANYM-632) through busy_wait; optional `postgres_data_directory` (ANYM-634).
    if (not isinstance(value,dict) or not required<=set(value) or
            set(value)-required-set(defaults)-{'profile','host_directory','verify_wait_seconds','busy_wait_seconds',
                                               'postgres_data_directory'} or
            type(value['v']) is not int or value['v']!=1 or
            ('profile' in value and (not isinstance(value['profile'],str) or value['profile'] not in r2.PROFILES))):
        raise ValueError('invalid_verifier_config')
    value={**defaults,**value}
    if (not isinstance(value['helper_image'],str) or not re.fullmatch(r'sha256:[a-f0-9]{64}',value['helper_image']) or
            not isinstance(value['helper_revision'],str) or not re.fullmatch('[a-f0-9]{40}',value['helper_revision']) or
            value['postgres_image']!=POSTGRES_IMAGE):raise ValueError('immutable_reviewed_images_required')
    if (any(type(value[k]) is not int for k in defaults) or
            not GIB<=value['max_plaintext_bytes']<=128*GIB or
            not 2*GIB<=value['postgres_headroom_bytes']<=64*GIB or
            not GIB<=value['reserve_bytes']<=32*GIB or not 300<=value['timeout_seconds']<=21600):
        raise ValueError('invalid_verifier_limits')
    host,wait=host_settings(value)
    if type(wait) is not int or not 0<=wait<=21600:raise ValueError('invalid_verifier_limits')
    if type(busy_wait(value)) is not int or not 0<=busy_wait(value)<=BUSY_WAIT_MAX:raise ValueError('invalid_verifier_limits')
    for key in ('receiver_config_file','directory','identity_file','protected_file'):
        if not isinstance(value[key],str) or not Path(value[key]).is_absolute() or '..' in Path(value[key]).parts:
            raise ValueError('invalid_verifier_path')
    if not isinstance(host,str) or not Path(host).is_absolute() or '..' in Path(host).parts:
        raise ValueError('invalid_verifier_path')
    with local.trusted_directory(value['directory']) as directory:
        if os.fstat(directory).st_mode&0o077:raise ValueError('private_verifier_directory_required')
    receiver=r2.load_config(value['receiver_config_file'])
    if r2.profile(receiver)!=r2.profile(value):raise ValueError('verifier_profile_mismatch')
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
    host=Path(host)
    if any(host.is_relative_to(path) or path.is_relative_to(host) for path in (directory,state)):
        raise ValueError('separate_operator_evidence_required')
    if 'postgres_data_directory' in value:
        # Absent: the scratch PostgreSQL keeps a Docker volume, as before. The path enters a --mount argument.
        data=value['postgres_data_directory']
        if not isinstance(data,str) or not re.fullmatch('/[A-Za-z0-9._-][A-Za-z0-9._/-]{0,511}',data) or '..' in Path(data).parts:
            raise ValueError('invalid_verifier_path')
        data=Path(data)
        if any(data.is_relative_to(path) or path.is_relative_to(data) for path in map(lexical,(directory,state,host))):
            raise ValueError('separate_operator_evidence_required')
        with local.trusted_directory(data) as fd:
            if os.fstat(fd).st_mode&0o077:raise ValueError('private_verifier_directory_required')
    private_file(value['identity_file'])
    with local.trusted_directory(Path(value['protected_file']).parent):pass
    return value


def lexical(path):
    """A path without POSIX's leading `//`, which Linux resolves as `/`: for comparisons only."""
    return Path('/'+str(path).lstrip('/'))


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


def host_settings(config):
    return config.get('host_directory',HOST_DIRECTORY),config.get('verify_wait_seconds',VERIFY_WAIT_SECONDS)


def busy_wait(config):return config.get('busy_wait_seconds',BUSY_WAIT_SECONDS)


class ReceiverBusy(RuntimeError):
    """A new run's wait for the receiver lock ran out before it journaled or created anything."""


@contextmanager
def receiver_lock(state_fd,wait=None):
    """The receiver's non-blocking lock. With `wait` (a new run only) it retries every
    BUSY_POLL_SECONDS while holding no receiver or host lock, then raises ReceiverBusy;
    without it the first `receiver_busy` stays the run's failure, as before."""
    ends=time.monotonic()+(wait or 0)
    with ExitStack() as held:
        while True:
            try:held.enter_context(r2.shared_lock(state_fd));break
            except RuntimeError as error:
                if str(error)!='receiver_busy' or wait is None:raise
                left=ends-time.monotonic()
                if left<=0:raise ReceiverBusy('receiver_busy') from None
                time.sleep(min(BUSY_POLL_SECONDS,left))
        yield


def untimed(engine,stack,context):
    """Enter `context` with the restore deadline paused, so a lock wait never spends the restore's
    budget; SIGTERM still stops the wait."""
    started=time.monotonic();left=signal.getitimer(signal.ITIMER_REAL)[0]
    signal.setitimer(signal.ITIMER_REAL,0)
    try:value=stack.enter_context(context)
    finally:
        if left>0:signal.setitimer(signal.ITIMER_REAL,left)
    if isinstance(engine,Docker):engine.ends+=time.monotonic()-started
    return value


@contextmanager
def host_directory(config):
    """The shared root-private host directory, created on first use under a trusted parent."""
    path=Path(host_settings(config)[0])
    with local.trusted_directory(path.parent) as parent:
        try:os.mkdir(path.name,0o700,dir_fd=parent);local.fsync_dir(parent)
        except FileExistsError:pass
    with local.trusted_directory(path) as fd:
        if os.fstat(fd).st_mode&0o077:raise ValueError('private_verifier_directory_required')
        yield fd


@contextmanager
def host_lock(config,wait=None):
    """One heavy restore at a time on this host: waits at most `verify_wait_seconds`, then
    `verifier_host_busy`. The kernel releases it when its holder dies, even by SIGKILL."""
    wait=host_settings(config)[1] if wait is None else wait
    with host_directory(config) as fd:
        lock=os.open(HOST_LOCK,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=fd)
        try:
            info=os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o077:
                raise ValueError('invalid_verifier_lock')
            ends=time.monotonic()+wait
            while True:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    left=ends-time.monotonic()
                    if left<=0:raise RuntimeError('verifier_host_busy') from None
                    time.sleep(min(5,left))
            yield fd
        finally:os.close(lock)


def valid_resource(item,profile,run):
    if (not isinstance(item,list) or len(item)!=(4 if item[:1]==['directory'] else 3) or not isinstance(item[1],str) or
            not isinstance(item[2],str) or not re.fullmatch('[a-f0-9]{32}',item[2])):return False
    if item[0]=='directory':
        # A run's PostgreSQL data directory (ANYM-634): its exact path, named for the record's own run and
        # profile, and the device:inode of the data root it was admitted under.
        path=Path(item[1])
        return (item[2]==run and path.is_absolute() and '..' not in path.parts and len(item[1])<=1024 and
                path.name==names(run,profile)['volume'] and isinstance(item[3],str) and
                re.fullmatch('[0-9]{1,20}:[0-9]{1,20}',item[3]) is not None)
    return item[0] in ('helper','postgres','volume','network') and re.fullmatch('deedkit-verify-[a-z0-9-]{32,120}',item[1]) is not None


def valid_admission(value):
    if (not isinstance(value,dict) or set(value)!={'v','profile','run','resources','admitted_at'} or
            type(value['v']) is not int or value['v']!=1 or value['profile'] not in r2.PROFILES or
            not isinstance(value['resources'],list) or len(value['resources'])>32 or
            not isinstance(value['run'],str) or not re.fullmatch('[a-f0-9]{32}',value['run']) or
            any(not valid_resource(item,value['profile'],value['run']) for item in value['resources'])):
        raise ValueError('verifier_cleanup_unconfirmed')
    names(value['run'])
    return value


def clear_admission(engine,fd):
    """Remove a previous holder's recorded resources by exact name and owner label and confirm them
    gone; a holder that died (SIGKILL) released the lock, but its containers may still run."""
    _,record=r2.read_root(fd,ADMISSION,missing=True)
    if record is None:return
    try:
        for kind,name,owner,*root in valid_admission(record)['resources']:
            if kind=='directory':remove_data_directory(name,root[0]);continue
            if engine.inspect_owned(kind,name,owner) is not None:engine.remove(kind,name)
            if engine.inspect_owned(kind,name,owner) is not None:raise RuntimeError('verifier_cleanup_unconfirmed')
    except Exception:
        if r2.CANCELLED:raise
        raise RuntimeError('verifier_cleanup_unconfirmed') from None
    os.unlink(ADMISSION,dir_fd=fd);local.fsync_dir(fd)


def admit(engine,fd,profile,run,resources):
    """Under the host lock: clear the previous holder, then record every resource before any exists."""
    clear_admission(engine,fd)
    local.write_maintenance_json(ADMISSION,{'v':1,'profile':profile,'run':run,'admitted_at':now(),
                                            'resources':[list(item) for item in resources]},dir_fd=fd)


def own_record(config,run):
    """Whether the host's admission record is still this run's, read without the lock: another holder
    replaces it only after removing what it names. An unreadable or invalid record counts as this run's:
    the locked cleanup refuses it and keeps it."""
    with host_directory(config) as fd:
        try:
            _,record=r2.read_root(fd,ADMISSION,missing=True)
            if record is None:return False
            return valid_admission(record)['run']==run
        except Exception:
            if r2.CANCELLED:raise
            return True


def release_admission(fd):
    """After the holder confirmed its own resources removed."""
    try:os.unlink(ADMISSION,dir_fd=fd)
    except FileNotFoundError:return
    local.fsync_dir(fd)


def run_resources(run,profile='messaging',data=None):
    """A run's resources by exact name and owner; a messaging retry may still own its names from before ANYM-609.
    Its data directory (ANYM-634) comes last: removed after the PostgreSQL container that uses it."""
    resources=[names(run,profile),*([legacy_names(run)] if profile=='messaging' else [])]
    return [(kind,name,run) for resource in resources for kind,name in resource.items()]+(
        [('directory',str(data),run,root_identity(data.parent))] if data is not None else [])


def root_identity(path):
    """The data root's device:inode, recorded with a run's directory: removal from a record acts only there."""
    with local.trusted_directory(path) as fd:
        info=os.fstat(fd);return str(info.st_dev)+':'+str(info.st_ino)


def pair(config):return [config['helper_image'],config['helper_revision']]


def valid_pair(value):
    return (isinstance(value,list) and len(value)==2 and isinstance(value[0],str) and re.fullmatch('sha256:[a-f0-9]{64}',value[0]) is not None and
            isinstance(value[1],str) and re.fullmatch('[a-f0-9]{40}',value[1]) is not None)


def unlink_entry(fd,name):
    try:os.unlink(name,dir_fd=fd)
    except FileNotFoundError:return
    local.fsync_dir(fd)


def write_pins(path,image,revision):
    """One atomic replace of a verifier config with only its two pins changed."""
    value=r2.unique_json(private_file(path,16384))
    local.write_maintenance_json(path,{**value,'helper_image':image,'helper_revision':revision})
    installed=load_config(path)
    if pair(installed)!=[image,revision]:raise ValueError('invalid_verifier_config')
    return installed


def move_pins(host,paths,image,revision):
    """Every instance's config to one pair, or none (ANYM-609, C2). The journal holds each config's old
    pair before the first write; each config is one atomic replace. A failure up to the journal's removal
    puts every config back; after a stop or crash the first verifier, follow or pin step to run does
    (recover_pins) before any instance uses a pin. The caller holds every instance's lock."""
    journal={'v':1,'to':[image,revision],'from':{str(p):pair(load_config(p)) for p in paths}}
    local.write_maintenance_json(PINS_SWITCH,journal,dir_fd=host)
    try:
        for p in paths:write_pins(p,image,revision)
        # The pairs before this step, for the documented undo (a pin step back to them).
        local.write_maintenance_json(PINS_PREVIOUS,{'v':1,'pins':journal['from']},dir_fd=host)
    except BaseException:
        if not r2.CANCELLED:
            try:undo_pins(host,journal)
            except Exception:pass  # the journal stays: recover_pins finishes it
        raise
    # The commit point: once the journal is gone every config holds the new pair and nothing rolls back.
    os.unlink(PINS_SWITCH,dir_fd=host);local.fsync_dir(host)


def undo_pins(host,journal):
    for p,old in journal['from'].items():
        if pair(load_config(p))!=old:write_pins(Path(p),*old)
    unlink_entry(host,PINS_SWITCH)


def recover_pins(host,paths=None):
    """A pin step that stopped between two config writes: every config back on its old pair.
    `paths` (the instances file's) must be the journal's; without it, the journal's own. Returns them."""
    _,journal=r2.read_root(host,PINS_SWITCH,missing=True)
    if journal is None:return None
    olds=journal.get('from') if isinstance(journal,dict) else None
    if (not isinstance(journal,dict) or set(journal)!={'v','to','from'} or type(journal['v']) is not int or
            journal['v']!=1 or not valid_pair(journal['to']) or not isinstance(olds,dict) or not olds or
            any(not Path(p).is_absolute() or '..' in Path(p).parts for p in olds) or
            (paths is not None and set(olds)!={str(p) for p in paths}) or not all(valid_pair(v) for v in olds.values())):
        raise ValueError('pins_switch_unresolved')
    if any(pair(load_config(p)) not in (old,journal['to']) for p,old in olds.items()):
        raise ValueError('pins_switch_unresolved')
    undo_pins(host,journal)
    return [Path(p) for p in olds]


def finish_pin_switch(config):
    """Before this instance uses a pin: a stopped pin step's configs back on their old pairs, when this
    instance is one of them. Its writer held every listed instance's lock, this one's too, so it is gone,
    and no listed instance passed this point since. A journal that does not list this instance may
    belong to a live writer: it is left alone. Returns this instance's config path when it recovered."""
    with host_directory(config) as host:
        try:
            _,journal=r2.read_root(host,PINS_SWITCH,missing=True)
            if journal is None:return None
            olds=journal.get('from') if isinstance(journal,dict) else None
            if not isinstance(olds,dict) or any(not isinstance(p,str) or not Path(p).is_absolute() for p in olds):
                raise ValueError('pins_switch_unresolved')
            own=next((Path(p) for p in olds if load_config(p)['directory']==config['directory']),None)
            if own is None:return None
            recover_pins(host)
        except Exception:
            if r2.CANCELLED:raise
            raise RuntimeError('verifier_pins_unfinished') from None
    return own


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


def names(run,profile='messaging'):
    """Scratch resource names carry the profile (ANYM-609), so two instances never share one."""
    if not isinstance(run,str) or not re.fullmatch('[a-f0-9]{32}',run):raise ValueError('invalid_verifier_journal')
    if profile not in r2.PROFILES:raise ValueError('invalid_verifier_config')
    base='deedkit-verify-'+profile+'-'+run
    return {'helper':base+'-helper','postgres':base+'-postgres','volume':base+'-data','network':base+'-net'}


def legacy_names(run):
    """Names of a messaging run journaled before ANYM-609: its retry still removes them."""
    base='deedkit-verify-'+run
    return {'helper':base+'-helper','postgres':base+'-postgres','volume':base+'-data','network':base+'-net'}


def data_directory(config,run):
    """The run's PostgreSQL data directory on `postgres_data_directory` (ANYM-634); None: a Docker volume."""
    root=config.get('postgres_data_directory')
    return None if root is None else Path(root)/names(run,r2.profile(config))['volume']


def make_data_directory(path):
    """Create a run's data directory, 0700 and the image's postgres user's, under the trusted root 0700
    parent. Its exact path is in admission.json before this runs."""
    path=Path(path)
    with local.trusted_directory(path.parent) as parent:
        if os.fstat(parent).st_mode&0o077:raise ValueError('private_verifier_directory_required')
        os.mkdir(path.name,0o700,dir_fd=parent)
        try:os.chown(path.name,POSTGRES_UID,POSTGRES_UID,dir_fd=parent,follow_symlinks=False)
        finally:local.fsync_dir(parent)


def remove_data_directory(path,root=None):
    """Remove exactly a run's data directory. Its parent must be root-private (from a record: the very data
    root it was admitted under, `root` device:inode) and the leaf a directory of root (created, not yet
    handed over) or of the image's postgres user, never a symlink, so a stale or wrong record cannot reach
    another directory: it is refused and kept."""
    path=Path(path)
    try:
        with local.trusted_directory(path.parent) as parent:
            info=os.fstat(parent)
            if info.st_mode&0o077 or (root is not None and str(info.st_dev)+':'+str(info.st_ino)!=root):
                raise ValueError('verifier_cleanup_unconfirmed')
            try:info=os.stat(path.name,dir_fd=parent,follow_symlinks=False)
            except FileNotFoundError:return
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0,POSTGRES_UID):raise ValueError('verifier_cleanup_unconfirmed')
            shutil.rmtree(path.name,dir_fd=parent);local.fsync_dir(parent)
    except FileNotFoundError:return  # no parent: nothing of the run is left there


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
            host.get('NetworkMode')!=names(run,r2.profile(config))['network']):
        raise ValueError('unexpected_restore_helper_layout')


class Docker:
    """Small, argument-only Docker adapter. It never prints secret diagnostics."""
    def __init__(self,timeout=21600):
        self.ends=time.monotonic()+timeout;self.disk_roots=[];self.reserve_bytes=0
        # The ranged GETs the last full stream needed (r2.verify_object); None until one completes.
        self.stream_resumes=None
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
        resource=names(run,r2.profile(config));labels=['--label',LABEL+'='+run,'--label',LABEL+'.owned=1']
        # Persisted names+ownership precede every create. A dropped response can
        # be reconciled by exact label, never by a generic prune.
        self.command('network','create','--internal',*labels,resource['network'])
        # On `postgres_data_directory` (ANYM-634) the run's directory, created by verify_run, else a volume.
        data=data_directory(config,run)
        if data is None:self.command('volume','create',*labels,resource['volume'])
        storage='type=bind,src='+str(data) if data is not None else 'type=volume,src='+resource['volume']
        common=['--network',resource['network'],'--restart=no','--log-driver=none','--cpus=1','--pids-limit=256',*labels]
        # Bulk-restore memory inside the 1 GiB limit: 256 MB buffers plus two 256 MB index builds (pg_restore --jobs=2).
        self.command('container','create','--name',resource['postgres'],*common,'--memory=1g','--memory-swap=1g',
                     '--env-file',str(secret_dir/'postgres.env'),'--network-alias','restore-postgres',
                     '--mount',storage+',dst='+PG_DATA_PATH,
                     config['postgres_image'],'postgres','-c','shared_buffers=256MB','-c','work_mem=8MB',
                     '-c','maintenance_work_mem=256MB','-c','max_connections=10','-c','max_parallel_workers=0')
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
        self.stream_resumes=None
        self.setup(config,run,payload,secret_dir);resource=names(run,r2.profile(config))
        helper=self.inspect_owned('helper',resource['helper'],run)
        validate_helper_layout(config,run,payload,secret_dir,helper)
        if self.inspect_owned('network',resource['network'],run) is None:
            raise ValueError('unexpected_restore_helper_layout')
        process=subprocess.Popen(['docker','container','start','-ai',resource['helper']],stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            engine=self
            class Sink:
                def write(self,data):
                    engine.check_reserve();return process.stdin.write(data)
            # A helper that exits mid-stream breaks the pipe: stop streaming (never
            # retried), wait for it within the deadline and report its own failure.
            try:self.stream_resumes=r2.verify_object(receiver,client,receipt,Sink());broken=False
            except BrokenPipeError:broken=True
            try:process.stdin.close()
            except BrokenPipeError:broken=True
            while process.poll() is None:
                self.check_reserve()
                try:process.wait(timeout=min(1,self.remaining()))
                except subprocess.TimeoutExpired:continue
            self.check_reserve()
            if process.wait(timeout=self.remaining()) or broken:raise helper_failure(payload)
        finally:
            if process.stdin and not process.stdin.closed:process.stdin.close()
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)
        # Only facts from the helper payload are read here. Root proof is written
        # to a different path after full ciphertext hashing and successful exit.
        return r2.unique_json(private_file(payload/'result.json',16384))


# The calendar helper's failure per V2 check (ANYM-606), and a bundle of the other profile.
CALENDAR_FAILURES={'restore_bundle_failed','restore_schema_failed','restore_inventory_failed','restore_vault_incomplete',
                   'restore_vault_decrypt_failed','verifier_profile_mismatch'}


class HelperFailure(RuntimeError):
    def __init__(self,code,detail=None):super().__init__(code);self.detail=detail


def helper_failure(payload):
    """The failed helper's own fact (backup_restore_worker.write_failure) when it is a known safe code."""
    failure='isolated_restore_failed';detail=None
    try:
        fact=r2.unique_json(private_file(payload/'failure.json',4096))
        safe={'restore_decryption_failed','restore_database_failed','restore_snapshot_failed','restore_quarantine_failed',
              *CALENDAR_FAILURES}
        if (isinstance(fact,dict) and set(fact)=={'v','status','error'} and
                type(fact['v']) is int and fact['v']==1 and fact['status']=='failed' and type(fact['error']) is str and fact['error'] in safe):failure=fact['error']
    except (OSError,ValueError):pass
    if failure=='restore_database_failed':
        try:detail=helper_detail(r2.unique_json(private_file(payload/'failure-detail.json',4096)))
        except (OSError,ValueError):pass
    return HelperFailure(failure,detail)


# The only phrases backup_restore_worker.restore_detail writes: fixed words, never a name or message text.
DETAIL_SHAPE=re.compile(r'(?:role|relation|schema|extension|type|table|column|constraint|index|policy|trigger|sequence|view|database) '
                        r'(?:does not exist|already exists)|extension is not available|COPY failed|permission denied|out of memory|'
                        r'could not extend file|could not write to file|deadlock detected|'
                        r'canceling statement due to (?:statement timeout|user request)|unclassified pg_restore error')


def helper_detail(value):
    """The helper's pg_restore detail, only in a shape DETAIL_SHAPE admits; anything else is dropped."""
    if (isinstance(value,dict) and set(value)=={'v','detail'} and type(value['v']) is int and value['v']==1 and
            isinstance(value['detail'],str) and DETAIL_SHAPE.fullmatch(value['detail'])):return value['detail']
    return None


# Docker's container states; anything else is recorded as `unknown`.
HELPER_STATES={'created','running','paused','restarting','removing','exited','dead'}


def helper_state(engine,run,profile='messaging'):
    """The run's own helper container as Docker records it, read before cleanup stops it (ANYM-633): its
    status, exit code and OOM kill, not the attach client's return code. None without a helper or an answer."""
    try:
        value=engine.inspect_owned('helper',names(run,profile)['helper'],run)
        state=value.get('State') if isinstance(value,dict) else None
        if not isinstance(state,dict):return None
        return {'status':state['Status'] if state.get('Status') in HELPER_STATES else 'unknown',
                'exit_code':state['ExitCode'] if type(state.get('ExitCode')) is int else None,
                'oom_killed':state.get('OOMKilled') is True}
    except Exception:return None


def cleanup_resources(engine,run,profile='messaging',data=None):
    for resource in (names(run,profile),*([legacy_names(run)] if profile=='messaging' else [])):
        for kind in ('helper','postgres','volume','network'):
            if engine.inspect_owned(kind,resource[kind],run) is not None:engine.remove(kind,resource[kind])
    if data is not None:remove_data_directory(data)


def preflight_disk(config,receipt,docker_root):
    """The payload share on the working directory's filesystem, the database share on the data directory's
    (`postgres_data_directory`, else Docker's root); on one shared filesystem their sum."""
    wire=receipt['wire']['bytes']
    if wire>config['max_plaintext_bytes']:raise ValueError('restore_plaintext_bound')
    work=Path(config['directory']);database=Path(config.get('postgres_data_directory',docker_root))
    required=wire+config['postgres_headroom_bytes']+config['reserve_bytes']
    if os.stat(work).st_dev==os.stat(database).st_dev:
        if shutil.disk_usage(work).free<required:raise ValueError('restore_disk_capacity')
    elif (shutil.disk_usage(work).free<wire+config['reserve_bytes'] or
          shutil.disk_usage(database).free<config['postgres_headroom_bytes']+config['reserve_bytes']):
        raise ValueError('restore_disk_capacity')


def reserve_roots(config,docker_root):
    """The filesystems the reserve monitor watches during a run."""
    return [config['directory'],docker_root,*([config['postgres_data_directory']] if 'postgres_data_directory' in config else [])]


def make_proof(receipt,facts,run,profile='messaging'):
    if isinstance(facts,dict) and facts.get('profile','messaging')!=profile:raise ValueError('verifier_profile_mismatch')
    keys,checks,schemas={'v','status','schema_version','manifest_sha256','checks'},r2.CHECKS,SCHEMAS
    # The calendar schema is fcs.migrations' version, which the helper binds to the manifest; no messaging range.
    if profile=='calendar':keys,checks,schemas=keys|{'profile','vault_warnings'},r2.CALENDAR_CHECKS,range(1,100001)
    if (not isinstance(facts,dict) or set(facts)!=keys or
            type(facts['v']) is not int or facts['v']!=1 or facts['status']!='verified' or
            type(facts['schema_version']) is not int or facts['schema_version'] not in schemas or
            facts['manifest_sha256']!=receipt['wire']['snapshot_sha256'] or not isinstance(facts['checks'],dict) or
            set(facts['checks'])!=checks or any(v is not True for v in facts['checks'].values()) or
            (profile=='calendar' and not r2.valid_warnings(facts['vault_warnings']))):
        raise ValueError('invalid_helper_result')
    verification={'id':run,'verified_at':now(),'schema_version':facts['schema_version'],
                  'manifest_sha256':facts['manifest_sha256'],'checks':facts['checks']}
    if profile=='calendar':verification={**verification,'profile':profile,'vault_warnings':facts['vault_warnings']}
    return {'v':1,'type':'independent_restore_verification','status':'verified',
            'backup':{k:receipt['wire'][k] for k in ('id','bytes','sha256','snapshot_sha256')},'verification':verification}


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
    # A calendar journal names its profile; a messaging journal has none, as before ANYM-606.
    if (not isinstance(journal,dict) or
            set(journal)-{'profile'}!={'v','run','destination','receipt','prior_sha256','phase','helper_image','helper_revision','protected_file'} or
            journal.get('profile','messaging') not in r2.PROFILES or journal['v']!=1 or type(journal['v']) is not int or journal['destination']!=r2.destination(receiver) or
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
    data=data_directory(config,run)
    cleanup_resources(engine,run,r2.profile(config),data);remove_payload(directory,run)
    docker_root=engine.preflight(config);preflight_disk(config,journal['receipt'],docker_root)
    if isinstance(engine,Docker):engine.disk_roots=reserve_roots(config,docker_root);engine.reserve_bytes=config['reserve_bytes']
    private_file(config['identity_file'])
    if data is not None:make_data_directory(data)
    payload.mkdir(mode=0o700);secret_dir.mkdir(mode=0o700)
    password=secrets.token_hex(32)
    write_secret(secret_dir/'postgres.env',('POSTGRES_USER=restore_owner\nPOSTGRES_DB='+DATABASE+'\nPOSTGRES_PASSWORD='+password+'\n').encode())
    worker={'v':1,'identity_file':'/run/restore/identity','work_directory':'/work','database_host':'restore-postgres',
            'database_password':password,'snapshot_sha256':journal['receipt']['wire']['snapshot_sha256'],
            'max_plaintext_bytes':min(config['max_plaintext_bytes'],journal['receipt']['wire']['bytes'])}
    if r2.profile(config)=='calendar':worker['profile']='calendar'
    write_secret(secret_dir/'worker.json',r2.encoded(worker));local.fsync_dir(secret_dir);local.fsync_dir(directory)
    facts=engine.restore(config,receiver,client,journal['receipt'],run,payload,secret_dir)
    proof=make_proof(journal['receipt'],facts,run,r2.profile(config));r2.validate_proof(proof,journal['receipt'],r2.profile(config))
    proof_name='proof-'+run+'.json'
    local.write_maintenance_json(proof_name,proof,dir_fd=fd)
    journal={**journal,'phase':'proof_ready'};local.write_maintenance_json('active.json',journal,dir_fd=fd)
    return journal


def run(config,*,client=None,engine=None,path=None):
    if os.geteuid()!=0:raise ValueError('root_verifier_required')
    receiver=r2.load_config(config['receiver_config_file'])
    client=client if client is not None else r2.make_client(receiver)
    engine=engine if engine is not None else Docker(config['timeout_seconds'])
    with verifier_lock(config['directory']) as fd,ExitStack() as host:
        journal=None;admitted=None
        # A pin step stopped between two config writes (ANYM-609): every config back on its old pair first.
        try:recovered=finish_pin_switch(config);pins_error=None
        except Exception as error:recovered=None;pins_error=error
        path=path if path is not None else recovered
        if path is not None:
            # The receiver update follow (ANYM-597) rewrites the pins under this lock: a
            # config loaded before the lock must never start a journal on the old pair.
            fresh=load_config(path)
            if fresh['directory']!=config['directory']:raise ValueError('invalid_verifier_config')
            config=fresh
        profile=r2.profile(config)
        try:
            with deadline(config['timeout_seconds']):
                if r2.profile(receiver)!=profile:raise ValueError('verifier_profile_mismatch')
                if pins_error is not None:raise pins_error
                _,journal=r2.read_root(fd,'active.json',missing=True)
                if journal is not None:
                    validate_journal(receiver,journal)
                    if r2.profile(journal)!=profile:raise ValueError('verifier_profile_mismatch')
                    if any(journal[k]!=config[k] for k in ('helper_image','helper_revision')):
                        raise ValueError('restore_image_changed_during_retry')
                    if journal['protected_file']!=config['protected_file']:raise ValueError('verifier_control_changed_during_retry')
                with local.trusted_directory(receiver['state_directory'],archive=True) as state_fd,ExitStack() as held:
                    # Only a run that journaled nothing waits out an upload (ANYM-632); a retry fails as before.
                    untimed(engine,held,receiver_lock(state_fd,busy_wait(config) if journal is None else None))
                    prior,marker=read_marker(receiver,config['protected_file'])
                    candidate=select_candidate(receiver,client,state_fd,marker)
                    pending=journal
                    if journal is None:
                        if candidate is None:
                            status={'v':1,'status':'idle','checked_at':now()};local.write_maintenance_json('status.json',status,dir_fd=fd);return status
                        pending={'v':1,'run':secrets.token_hex(16),'destination':r2.destination(receiver),'receipt':candidate,
                                 'prior_sha256':prior,'protected_file':config['protected_file'],'phase':'restoring','helper_image':config['helper_image'],'helper_revision':config['helper_revision']}
                        if profile=='calendar':pending['profile']=profile
                    if pending['phase']=='restoring':
                        # One heavy restore per host (ANYM-609): wait, clear a dead holder's resources and
                        # record ours, all before a new run's journal or any scratch resource exists.
                        # The wait has its own bound (verify_wait_seconds): the restore deadline pauses while
                        # it waits, so the restore keeps its whole budget; SIGTERM still stops the wait.
                        lock=untimed(engine,host,host_lock(config))
                        # A refused admission keeps the record it could not clear for the next holder.
                        admit(engine,lock,profile,pending['run'],
                              run_resources(pending['run'],profile,data_directory(config,pending['run'])));admitted=lock
                    elif own_record(config,pending['run']):
                        # A retry after its proof whose own record a crash left: it names the run's data directory
                        # as it was then (ANYM-634), whatever the config says now. Only then does it wait.
                        clear_admission(engine,untimed(engine,host,host_lock(config)))
                    if journal is None:
                        journal=pending;local.write_maintenance_json('active.json',journal,dir_fd=fd)
                    # Even after a marker switch, exact committed candidate remains
                    # available by ID. Never silently adopt another new capture.
                    actual=r2.get_receipt(receiver,client,journal['receipt']['wire']['id'])
                    if actual!=journal['receipt']:raise ValueError('restore_candidate_changed')
                    if journal['phase']=='restoring':journal=verify_run(config,receiver,client,engine,journal,fd)
                    else:
                        _,proof=r2.read_root(fd,'proof-'+journal['run']+'.json')
                        r2.validate_proof(proof,journal['receipt'],profile)
                    cleanup_resources(engine,journal['run'],profile,data_directory(config,journal['run']))
                    remove_payload(config['directory'],journal['run'])
                    if admitted is not None:release_admission(admitted);admitted=None
                    host.close()
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
                if type(resumes:=getattr(engine,'stream_resumes',None)) is int:status['stream_resumes']=resumes
                local.write_maintenance_json('last-success.json',status,dir_fd=fd)
                local.write_maintenance_json('status.json',status,dir_fd=fd)
                os.unlink('active.json',dir_fd=fd);local.fsync_dir(fd)
                return status
        except BaseException as error:
            if isinstance(error,ReceiverBusy) and journal is None:
                # An upload still holds the receiver: not a failure. Nothing was journaled or created,
                # and last-failure.json keeps the last real failure.
                status={'v':1,'status':'busy','code':'receiver_busy','checked_at':now()}
                local.write_maintenance_json('status.json',status,dir_fd=fd);return status
            # Generic safe codes only; SDK errors, DSNs and stderr never enter
            # durable status. Keep journal/proof and the old protected recovery point.
            cleanup_ok=True;resumes=getattr(engine,'stream_resumes',None)
            # Its own short bound, so a slow answer never spends the cleanup's.
            state=None if journal is None else helper_state(Docker(10) if isinstance(engine,Docker) else engine,journal['run'],profile)
            if isinstance(engine,Docker):engine=Docker(120)
            # A refused admission created nothing and leaves its retry's resources to the next holder.
            if journal is not None and str(error) not in ('verifier_host_busy','verifier_cleanup_unconfirmed'):
                try:
                    cleanup_resources(engine,journal['run'],profile,data_directory(config,journal['run']))
                    remove_payload(config['directory'],journal['run'])
                except Exception:cleanup_ok=False
            if admitted is not None and cleanup_ok:
                try:release_admission(admitted)
                except Exception:cleanup_ok=False
            failure={'v':1,'status':'failed','error':failure_code(error),'failed_at':now(),
                     'cleanup_complete':cleanup_ok,'diagnostic':describe(error)}
            if isinstance(error,HelperFailure) and error.detail:failure['detail']=error.detail
            # Counts and Docker's fixed state words only (ANYM-633): the stream's ranged GETs and the helper's end.
            if type(resumes) is int:failure['stream_resumes']=resumes
            if state is not None:failure['helper_state']=state
            # The journal line (main) carries them too: a later failure overwrites last-failure.json.
            try:error.verifier_status=failure
            except Exception:pass
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
          'isolated_restore_failed','stored_backup_corrupt','protected_backup_corrupt','verifier_host_busy','verifier_cleanup_unconfirmed',
          'verifier_pins_unfinished','invalid_verifier_lock','verification_pending','instances_invalid','pins_switch_unresolved','storage_unavailable','unexpected_restore_image_volume','unexpected_restore_helper_layout',
          *CALENDAR_FAILURES}
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
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config')
    # The pin step (ANYM-609): one helper pair for every instance in the instances file, or none.
    parser.add_argument('--update-pins',action='store_true');parser.add_argument('--instances',default=str(INSTANCES_FILE))
    parser.add_argument('--helper-image');parser.add_argument('--helper-revision')
    args=parser.parse_args(argv)
    if args.update_pins:
        if args.config or not args.helper_image or not args.helper_revision:
            parser.error('--update-pins takes --helper-image and --helper-revision, not --config')
        from . import backup_r2_follow as follow
        try:result=follow.update_pins(args.instances,args.helper_image,args.helper_revision)
        except Exception as error:
            print(json.dumps({'error':follow.failure_code(error),'diagnostic':describe(error)},separators=(',',':')),file=sys.stderr);return 1
        # Only the outcome: config paths and the image ID stay in the root-private records.
        print(json.dumps({k:result[k] for k in ('v','status','helper_revision')},separators=(',',':')));return 0
    if not args.config:parser.error('--config is required')
    try:status=run(load_config(args.config),path=args.config)
    except Exception as error:
        if failure_code(error)=='verifier_busy':
            # Another invocation of this profile runs: it owns status.json, so this one writes nothing.
            print(json.dumps({'v':1,'status':'busy','code':'verifier_busy'},separators=(',',':')));return 0
        line={'error':failure_code(error),'diagnostic':describe(error)}
        recorded=getattr(error,'verifier_status',None)
        if isinstance(recorded,dict):line.update({k:recorded[k] for k in ('stream_resumes','helper_state') if k in recorded})
        print(json.dumps(line,separators=(',',':')),file=sys.stderr);return 1
    if status['status']=='busy':print(json.dumps(status,separators=(',',':')))
    # A verified run's resumes reach the journal too (ANYM-633): last-success.json keeps only the latest.
    elif status['status']=='verified' and 'stream_resumes' in status:
        print(json.dumps({k:status[k] for k in ('status','verification_id','stream_resumes')},separators=(',',':')))
    return 0


if __name__=='__main__':raise SystemExit(main())
