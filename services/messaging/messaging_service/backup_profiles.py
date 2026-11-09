"""The recordings (ANYM-714, ANYM-727) and calls (ANYM-778) profiles of the host backup runner; stdlib only.

backup_runner.py loads this file from its own directory and sets `runner` to itself before any call (`profiles()`
there); what both share (the journal files, Skip, the path checks, the mount table) is read from it at call time, so
one definition serves both. This module never imports backup_runner: the host runs that file as `__main__`.
"""
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess

runner=None  # the backup_runner module, set by its `profiles()`

RECORDINGS_WORKER='deedkit-recordings-backup-'
CALLS_WORKER='deedkit-calls-backup-'
CALLS_HELPER='deedkit-calls-snapshot-'
CALLS_SNAPSHOT='voice.sqlite3'
# ANYM-727. A recordings job stays this far from the next scheduled start of any other backup profile (the same quarter
# hour the admission, the yield and the unit tests use); STOP_BUDGET is `docker stop`'s 25 s plus a margin, RECOVERY_BUDGET
# the ExecStopPost run; CONTAINER_UID is the recordings image's user (Dockerfile USER, compose `user:`).
GUARD_SECONDS=900
STOP_BUDGET=35
RECOVERY_BUDGET=120
# Admission also counts the unit's own stop budgets (TimeoutStopSec of the main process and of ExecStopPost), and refuses a
# start while another profile's scheduled start is this recent (it may be due but waiting for its randomized delay).
UNIT_STOP_SECONDS=180
UNIT_START_SECONDS=4400  # TimeoutStartSec of both recordings units (a unit test keeps them equal)
CALLS_UNIT_START_SECONDS=5400  # TimeoutStartSec of both calls units (tests/test_backup_units.py keeps them equal)
DUE_GRACE_SECONDS=120
REAP_SECONDS=20
CLEANUP_CODES={'staging_cleanup_failed','staging_mount_refused','recordings_exec_unverified','calls_helper_unverified'}
CONTAINER_UID=10001
RECORDINGS_DEFAULTS={'exec_timeout_seconds':300,'hash_timeout_seconds':120,'integrity_timeout_seconds':300,
                     'encrypt_timeout_seconds':600,'min_free_bytes':1024**3,'expected_archive_bytes':1024**3}
CALLS_DEFAULTS={'hash_timeout_seconds':120,'integrity_timeout_seconds':300,'encrypt_timeout_seconds':600,
                'min_free_bytes':1024**3,'expected_archive_bytes':256*1024**2}
# The calls helper's writer bound (ANYM-778; tests/test_backup_calls.py measures it against CALLS_WRITER_BOUND_SECONDS).
CALLS_STEP_PAGES=64
CALLS_STEP_PAUSE=0.02
CALLS_BUSY_PAUSE=0.25
CALLS_BUSY_RETRIES=120
CALLS_RESTART_PAUSE=1.0
CALLS_WRITER_BOUND_SECONDS=0.25
# The helper stops itself this long before the runner would kill it, then its alarm ends it even inside one SQLite step.
CALLS_HELPER_MARGIN=15
CALLS_HELPER_ALARM_MARGIN=10
CALLS_HELPER_CODES={'no_database':'calls_snapshot_absent','busy':'calls_snapshot_busy','no_space':'calls_no_space',
                    'corrupt':'calls_snapshot_corrupt','timeout':'calls_helper_timeout'}


# ANYM-778. The calls snapshot helper: runs as `python -I -c` in a capped container of its own from the calls image, the
# state directory read-only. SQLite holds the source's shared lock only during a step, so a writer waits for one step at
# most; a commit by the writer restarts the copy (rollback journal), and the restart waits longer, so a long call delays
# the copy instead of the copy competing with the call. Output is one fixed category, never a message of SQLite's.
CALLS_SNAPSHOT_HELPER=r"""
import errno, json, os, pathlib, signal, sqlite3, sys, time
class Stop(Exception):
    def __init__(self, code):
        self.code = code
def category(exc):
    name = getattr(exc, "sqlite_errorname", "") or ""
    for prefix, code in (("SQLITE_BUSY", "busy"), ("SQLITE_LOCKED", "busy"), ("SQLITE_FULL", "no_space"),
                         ("SQLITE_INTERRUPT", "timeout"), ("SQLITE_READONLY", "needs_recovery"),
                         ("SQLITE_CORRUPT", "corrupt"), ("SQLITE_NOTADB", "corrupt"), ("SQLITE_CANTOPEN", "cannot_open")):
        if name.startswith(prefix):
            return code
    return "sqlite_error"
def snapshot(source, staging, pages, pause, busy_pause, retries, restart_pause, limit, reserve):
    deadline = time.monotonic() + limit
    short = []
    def low():
        info = os.statvfs(staging)
        if info.f_bavail * info.f_frsize < reserve:
            short.append(True)
        return bool(short)
    if not source.is_file():
        raise Stop("no_database")
    copy, final = staging / "voice.copy.sqlite3", staging / "voice.sqlite3"
    reader = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=0)
    target = sqlite3.connect(copy)
    busy, last = 0, None
    def progress(status, remaining, total):
        nonlocal busy, last
        if time.monotonic() > deadline:
            raise Stop("timeout")
        if low():
            raise Stop("no_space")
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            busy += 1
            if busy > retries:
                raise Stop("busy")
            return
        busy = 0
        restarted = last is not None and remaining > last
        last = remaining
        if remaining:
            time.sleep(restart_pause if restarted else pause)
    reader.backup(target, pages=pages, progress=progress, sleep=busy_pause)
    reader.close()
    target.set_progress_handler(lambda: time.monotonic() > deadline or low(), 1000)
    try:
        if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise Stop("corrupt")
        target.execute("VACUUM INTO ?", (str(final),))
    except sqlite3.Error:
        if short:
            raise Stop("no_space") from None
        raise
    target.close()
    copy.unlink()
os.umask(0o077)
signal.alarm(int(sys.argv[9]))
reserve = int(sys.argv[10])
try:
    snapshot(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5]),
             int(sys.argv[6]), float(sys.argv[7]), float(sys.argv[8]), reserve)
    print(json.dumps({"status": "ok"}))
except Stop as exc:
    print(json.dumps({"error": exc.code}))
    sys.exit(3)
except sqlite3.Error as exc:
    print(json.dumps({"error": category(exc)}))
    sys.exit(3)
except OSError as exc:
    print(json.dumps({"error": "no_space" if exc.errno == errno.ENOSPC else "os_error"}))
    sys.exit(3)
except Exception:
    print(json.dumps({"error": "failed"}))
    sys.exit(3)
"""


DAILY_START=re.compile(r'\*-\*-\* ([01][0-9]|2[0-3]):([0-5][0-9])(?::([0-5][0-9]))? UTC')


def timer_starts(path,profile='recordings'):
    """Seconds after 00:00 UTC of every `OnCalendar=*-*-* HH:MM[:SS] UTC` line of a systemd timer file.

    Anything else (another calendar form, no such line, an unreadable file) is unknown, and an unknown schedule
    stops the job (fail closed): the admission must never guess when another backup starts.
    """
    unknown=profile+'_skipped_schedule_unknown'
    try:
        with open(path,'rb') as f:text=f.read(65537).decode()
    except (OSError,UnicodeDecodeError):raise runner.Skip(unknown) from None
    if len(text)>65536:raise runner.Skip(unknown)
    starts=[]
    for line in text.splitlines():
        line=line.strip()
        if not line.startswith('OnCalendar='):continue
        match=DAILY_START.fullmatch(line[len('OnCalendar='):].strip())
        if match is None:raise runner.Skip(unknown)
        starts.append(int(match[1])*3600+int(match[2])*60+int(match[3] or 0))
    if not starts:raise runner.Skip(unknown)
    return starts


def timer_delay(path,profile='recordings'):
    """`RandomizedDelaySec` of a timer file in seconds (0 when absent); a form this does not understand is unknown."""
    unknown=profile+'_skipped_schedule_unknown'
    try:
        with open(path,'rb') as f:text=f.read(65537).decode()
    except (OSError,UnicodeDecodeError):raise runner.Skip(unknown) from None
    delay=0
    for line in text.splitlines():
        line=line.strip()
        if not line.startswith('RandomizedDelaySec='):continue
        match=re.fullmatch(r'(\d{1,6})\s*(s|sec|min|m|h)?',line[len('RandomizedDelaySec='):].strip())
        if match is None:raise runner.Skip(unknown)
        delay=int(match[1])*{None:1,'s':1,'sec':1,'min':60,'m':60,'h':3600}[match[2]]
    return delay


def previous_start(now,starts):
    """The latest of the daily `starts` at or before epoch second `now` (UTC days)."""
    day=int(now//86400)*86400
    return max(t for d in (-86400,0) for t in (day+d+x for x in starts) if t<=now)


def next_start(now,starts):
    """The first of the daily `starts` after epoch second `now` (UTC days)."""
    day=int(now//86400)*86400
    return min(t for d in (0,86400) for t in (day+d+x for x in starts) if t>now)


def recordings_options(cfg):
    return {**RECORDINGS_DEFAULTS,**cfg['recordings']}


def staged_options(cfg):
    """The phase deadlines of a staged profile. The calls helper's budget stands where the recordings exec's does."""
    if cfg.get('profile')=='calls':
        # Its budget plus what surrounds it: resolving the image, creating the helper, verifying it gone.
        return {**CALLS_DEFAULTS,**cfg['calls'],'exec_timeout_seconds':cfg['calls']['capture_timeout_seconds']+30+90+REAP_SECONDS}
    return recordings_options(cfg)


def worst_case(cfg,phase='full'):
    """Seconds from the start of a recordings run to the end of its recovery, from the configured deadlines alone.

    stop + snapshot call + hash + worker container + delivery (an offer's timeout plus the busy wait) + recovery.
    A resumed `upload` job owns only the delivery and the recovery. The units' TimeoutStartSec and the admission use it.
    """
    options=staged_options(cfg)
    delivery=cfg['timeout_seconds']+cfg.get('busy_wait_seconds',runner.BUSY_WAIT_SECONDS)
    if phase=='upload':return delivery+RECOVERY_BUDGET
    return (STOP_BUDGET+options['exec_timeout_seconds']+options['hash_timeout_seconds']+
            options['capture_timeout_seconds']+delivery+RECOVERY_BUDGET)


def admission_bound(cfg):
    """Seconds from a start to the end of its recovery as the unit enforces them (any phase): the unit's own time limit
    is a floor, so a short configured budget never admits a run the unit would still let hold the lock."""
    floor=CALLS_UNIT_START_SECONDS if cfg.get('profile')=='calls' else UNIT_START_SECONDS
    return max(worst_case(cfg,'full'),floor)+2*UNIT_STOP_SECONDS


def container_path(value):
    return (isinstance(value,str) and re.fullmatch(r'/[a-zA-Z0-9_./-]{1,200}',value) is not None and
            '..' not in value.split('/') and '//' not in value)


def recordings_config(cfg):
    """`profile: recordings` (ANYM-714): one staged archive snapshot, no database and no network, messaging's lock.

    ANYM-727 adds the running container the snapshot is taken in (`docker exec`), its paths, the other profiles' timer
    files the admission reads, and a deadline per phase.
    """
    if set(cfg)-{'delivery_mode','busy_wait_seconds'}!={'profile','directory','project','image','recipient_file',
                  'transfer','timeout_seconds','keep_local','host_lock','recordings'}:raise ValueError('invalid_backup_config')
    if cfg.get('delivery_mode','staged_v1')!='staged_v1':raise ValueError('invalid_backup_config')
    if not isinstance(cfg['project'],str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}',cfg['project']):
        raise ValueError('invalid_backup_config')
    if not isinstance(cfg['image'],str) or not re.fullmatch(r'[a-zA-Z0-9_./:@-]{1,256}',cfg['image']):
        raise ValueError('invalid_backup_config')
    for key in ('directory','recipient_file','host_lock'):
        if not isinstance(cfg[key],str) or not runner.path_valid(cfg[key]):raise ValueError('invalid_backup_path')
    if Path(cfg['host_lock'])==Path(cfg['directory'])/'.runner.lock':raise ValueError('invalid_backup_path')
    own=cfg['recordings']
    required={'staging_directory','state_directory','capture_timeout_seconds','container','container_config',
              'container_staging','timers'}
    if not isinstance(own,dict) or set(own)-set(RECORDINGS_DEFAULTS)!=required:raise ValueError('invalid_backup_config')
    for key in ('staging_directory','state_directory'):
        if not isinstance(own[key],str) or not runner.path_valid(own[key]):raise ValueError('invalid_backup_path')
    # The runner empties the staging directory: it must be nothing else's (not the runner root, its lock, or state).
    staging,state=own['staging_directory'],own['state_directory']
    if (runner.nested(staging,state) or runner.nested(staging,cfg['directory']) or runner.nested(state,cfg['directory']) or
        runner.nested(staging,cfg['host_lock']) or runner.nested(state,cfg['host_lock'])):raise ValueError('invalid_backup_path')
    if not runner.transfer_valid(cfg['transfer']):raise ValueError('invalid_backup_transfer')
    # Emptying the staging directory must never reach a credential this runner needs.
    if any(runner.nested(staging,cfg[key]) for key in ('recipient_file',)) or any(
        runner.nested(staging,cfg['transfer'][key]) for key in ('identity_file','known_hosts_file')):
        raise ValueError('invalid_backup_path')
    if not isinstance(own['container'],str) or not (len(own['container'])<=128 and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*',own['container'])):
        raise ValueError('invalid_backup_config')
    if not container_path(own['container_config']) or not container_path(own['container_staging']):
        raise ValueError('invalid_backup_path')
    # Other profiles' timers: read at every start for the next scheduled time (never hard-coded); none of them may be a
    # file this job writes or deletes.
    timers=own['timers']
    if (not isinstance(timers,list) or not 1<=len(timers)<=8 or len(set(timers))!=len(timers) or
        not all(isinstance(t,str) and runner.path_valid(t) and t.endswith('.timer') for t in timers) or
        any(runner.nested(staging,t) or runner.nested(state,t) or runner.nested(cfg['directory'],t) for t in timers)):
        raise ValueError('invalid_backup_path')
    if type(cfg['timeout_seconds']) is not int or not 60<=cfg['timeout_seconds']<=7200:raise ValueError('invalid_backup_timeout')
    if type(own['capture_timeout_seconds']) is not int or not 60<=own['capture_timeout_seconds']<=7200:
        raise ValueError('invalid_backup_timeout')
    options=recordings_options(cfg)
    ranges={'exec_timeout_seconds':(30,1800),'hash_timeout_seconds':(10,1800),'integrity_timeout_seconds':(30,1800),
            'encrypt_timeout_seconds':(30,3600),'min_free_bytes':(256*1024**2,1024**4),
            'expected_archive_bytes':(1024**2,1024**4)}
    for key,(low,high) in ranges.items():
        if type(options[key]) is not int or not low<=options[key]<=high:raise ValueError('invalid_backup_timeout')
    # The worker container runs the integrity check and the encryption: its deadline covers both phases plus start-up.
    if options['integrity_timeout_seconds']+options['encrypt_timeout_seconds']+60>own['capture_timeout_seconds']:
        raise ValueError('invalid_backup_timeout')
    if type(cfg.get('busy_wait_seconds',0)) is not int or not 0<=cfg.get('busy_wait_seconds',0)<=7200:
        raise ValueError('invalid_backup_timeout')
    if type(cfg['keep_local']) is not int or not 1<=cfg['keep_local']<=24:raise ValueError('invalid_backup_retention')
    return cfg


def calls_config(cfg):
    """`profile: calls` (ANYM-778): one staged snapshot of the calls state from a capped helper, messaging's lock."""
    if set(cfg)-{'delivery_mode','busy_wait_seconds'}!={'profile','directory','project','image','recipient_file',
                  'transfer','timeout_seconds','keep_local','host_lock','calls'}:raise ValueError('invalid_backup_config')
    if cfg.get('delivery_mode','staged_v1')!='staged_v1':raise ValueError('invalid_backup_config')
    if not isinstance(cfg['project'],str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}',cfg['project']):
        raise ValueError('invalid_backup_config')
    if not isinstance(cfg['image'],str) or not re.fullmatch(r'[a-zA-Z0-9_./:@-]{1,256}',cfg['image']):
        raise ValueError('invalid_backup_config')
    for key in ('directory','recipient_file','host_lock'):
        if not isinstance(cfg[key],str) or not runner.path_valid(cfg[key]):raise ValueError('invalid_backup_path')
    if Path(cfg['host_lock'])==Path(cfg['directory'])/'.runner.lock':raise ValueError('invalid_backup_path')
    own=cfg['calls']
    if not isinstance(own,dict) or set(own)!={'staging_directory','state_directory','capture_timeout_seconds','data_mount',
                                              'calls_container','timers'}:raise ValueError('invalid_backup_config')
    for key in ('staging_directory','state_directory','data_mount'):
        if not isinstance(own[key],str) or not runner.path_valid(own[key]):raise ValueError('invalid_backup_path')
    staging,state,data=own['staging_directory'],own['state_directory'],own['data_mount']
    # The runner empties staging; the helper sees the data mount: neither may reach anything else of this job.
    if (runner.nested(staging,state) or runner.nested(staging,cfg['directory']) or runner.nested(state,cfg['directory']) or
        runner.nested(staging,cfg['host_lock']) or runner.nested(state,cfg['host_lock']) or runner.nested(data,staging) or
        runner.nested(data,state) or runner.nested(data,cfg['directory']) or runner.nested(data,cfg['host_lock'])):raise ValueError('invalid_backup_path')
    if not runner.transfer_valid(cfg['transfer']):raise ValueError('invalid_backup_transfer')
    if runner.nested(staging,cfg['recipient_file']) or any(runner.nested(staging,cfg['transfer'][key]) for key in ('identity_file','known_hosts_file')):
        raise ValueError('invalid_backup_path')
    if not isinstance(own['calls_container'],str) or not (len(own['calls_container'])<=128 and
        re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*',own['calls_container'])):raise ValueError('invalid_backup_config')
    timers=own['timers']
    if (not isinstance(timers,list) or not 1<=len(timers)<=8 or not all(isinstance(t,str) for t in timers) or
        len(set(timers))!=len(timers) or not all(runner.path_valid(t) and t.endswith('.timer') for t in timers) or
        any(runner.nested(staging,t) or runner.nested(state,t) or runner.nested(cfg['directory'],t) for t in timers)):
        raise ValueError('invalid_backup_path')
    if type(cfg['timeout_seconds']) is not int or not 60<=cfg['timeout_seconds']<=7200:raise ValueError('invalid_backup_timeout')
    if type(own['capture_timeout_seconds']) is not int or not 60<=own['capture_timeout_seconds']<=7200:
        raise ValueError('invalid_backup_timeout')
    # One budget each for the helper and the worker; the worker's covers its integrity check, encryption and start-up.
    if CALLS_DEFAULTS['integrity_timeout_seconds']+CALLS_DEFAULTS['encrypt_timeout_seconds']+60>own['capture_timeout_seconds']:
        raise ValueError('invalid_backup_timeout')
    if type(cfg.get('busy_wait_seconds',0)) is not int or not 0<=cfg.get('busy_wait_seconds',0)<=7200:
        raise ValueError('invalid_backup_timeout')
    if type(cfg['keep_local']) is not int or cfg['keep_local']!=1:raise ValueError('invalid_backup_retention')
    return cfg


class StagedProfile:
    """Runner's recordings and calls behaviour: admission, the snapshot, its cleanup, the worker (Runner's mixin)."""
    def admit(self,phase='full'):
        """Latest-start admission (ANYM-727): this job and its recovery must end 15 minutes before the next scheduled start
        of any other profile, read from those profiles' timer files now. Sets the instant after which the job yields.

        The bound is the unit's: a fresh service run (also a resumed `upload` job) is killed only at TimeoutStartSec and
        then stopped within two TimeoutStopSec, so every phase is admitted against the full worst case plus those stops.
        A start of another profile that is already due (within its randomized delay and a grace) is a veto too: its run
        may be waiting for the shared lock this run would take first."""
        now=self.clock();following=None;profile=self.cfg['profile']
        for path in self.own['timers']:
            starts=timer_starts(path,profile)
            if now-previous_start(now,starts)<DUE_GRACE_SECONDS+timer_delay(path,profile):raise runner.Skip(self.code('skipped_window'))
            following=min(next_start(now,starts),following or 1<<62)
        if now+admission_bound(self.cfg)>following-GUARD_SECONDS:raise runner.Skip(self.code('skipped_window'))
        self.yield_at=following-GUARD_SECONDS
    def guard(self):
        """A checkpoint before anything is sealed: past the yield instant the job gives way and cleans up."""
        if self.clock()>=self.yield_at:raise runner.Skip(self.code('skipped_yielded'))
    def space(self,plain,cipher):
        """Free space for `plain` more bytes of plaintext in staging and `cipher` of ciphertext in the job directory,
        counted together when both are on one filesystem, above the floor."""
        floor=staged_options(self.cfg)['min_free_bytes'];staging,root=self.staging_root,self.root
        needs=[(staging,plain+cipher)] if os.stat(staging).st_dev==os.stat(root).st_dev else [(staging,plain),(root,cipher)]
        for path,size in needs:
            if self.disk(path).free<floor+size:raise runner.Skip(self.code('skipped_disk'))
    def stage_snapshot(self,destination):
        """The archive snapshot, made by the single running writer (ANYM-727): `docker exec` of the existing CLI into
        `destination`, this job's private directory in the staging mount (never a second container on the archive volume).

        Skips (no failure, nothing sealed): container not running, archive not configured, no space, past the yield
        instant. Records the staged file's size and SHA-256 in the journal; the worker must see the same bytes.
        """
        if self.calls:return self.stage_calls(destination)
        own=self.cfg['recordings'];options=recordings_options(self.cfg);self.guard()
        info=self.staging_root.stat()
        if info.st_uid!=self.container_uid or info.st_mode&0o777!=0o700:raise RuntimeError('recordings_staging_unsafe')
        previous=runner.read_json(self.status_path).get('archive_bytes') if self.status_path.exists() else None
        estimate=max(options['expected_archive_bytes'],previous if type(previous) is int else 0)
        self.space(estimate,estimate)
        box=self.docker.inspect(own['container'])
        if box is None or not box['State']['Running']:raise runner.Skip('recordings_skipped_container_down')
        for entry in destination.iterdir():  # a resumed job: nothing of an earlier attempt stays
            shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
        os.chown(destination,self.container_uid,self.container_gid)
        target=own['container_staging'].rstrip('/')+'/'+destination.name+'/archive.sqlite'
        # The client's deadline does not stop the producer inside the container: remember what it is, and reap it.
        self.journal['exec_target']=target;self.save()
        try:
            code,error=self.docker.exec(own['container'],['deedkit-recordings','--config',own['container_config'],
                                                          '--backup-to',target],options['exec_timeout_seconds'])
        except BaseException:
            self.reap_snapshot();raise
        self.journal.pop('exec_target',None);self.save()
        if code:
            if 'archive_disabled' in error:raise runner.Skip('recordings_skipped_archive_disabled')
            if self.disk(self.staging_root).free<options['min_free_bytes']:raise RuntimeError('recordings_no_space')
            raise RuntimeError('recordings_schema_unsupported' if 'archive_schema_unsupported' in error else 'recordings_exec_failed')
        self.guard();size,digest=self.hash_staged(destination/'archive.sqlite',options['hash_timeout_seconds'])
        self.space(0,size);self.guard()
        self.journal['archive']={'bytes':size,'sha256':digest}
    def stage_calls(self,destination):
        """The calls snapshot (ANYM-778), made by a capped helper from the calls container's current image: never a
        process in that container, never a change to it. The helper is bounded by `capture_timeout_seconds`, and it is
        verified gone before anything reads or cleans staging."""
        own=self.own;options=staged_options(self.cfg);self.guard()
        info=self.staging_root.stat()
        if info.st_uid!=self.container_uid or info.st_mode&0o777!=0o700:raise RuntimeError('calls_staging_unsafe')
        image=self.docker.image_of(own['calls_container'])
        if image is None:raise RuntimeError('no_calls_container')
        previous=runner.read_json(self.status_path).get('archive_bytes') if self.status_path.exists() else None
        # The live file's size with its free pages: stat only, this root never opens it.
        try:live=os.lstat(Path(own['data_mount'])/CALLS_SNAPSHOT).st_size
        except OSError:live=0
        estimate=max(options['expected_archive_bytes'],previous if type(previous) is int else 0,live)
        # In staging at once: the copy, the vacuumed file and SQLite's temporary files, each at most the estimate.
        self.space(3*estimate,estimate)
        for entry in destination.iterdir():  # a resumed job: nothing of an earlier attempt stays
            shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
        os.chown(destination,self.container_uid,self.container_gid)
        name=CALLS_HELPER+self.journal['id'].lower()
        # The content is never older than this instant: a restarted copy only makes it newer.
        self.journal['helper']=name
        self.journal['captured_at']=datetime.fromtimestamp(self.clock(),timezone.utc).isoformat();self.save()
        try:
            self.docker.call(*self.helper_args(name,image,destination))
            code,output=self.docker.attach(name,own['capture_timeout_seconds'])
        except BaseException:
            self.reap_helper();raise
        self.reap_helper()
        try:answer=json.loads(output)
        except ValueError:answer=None
        if code or answer!={'status':'ok'}:
            category=answer.get('error') if isinstance(answer,dict) else None
            if self.disk(self.staging_root).free<options['min_free_bytes']:raise RuntimeError('calls_no_space')
            raise RuntimeError(CALLS_HELPER_CODES.get(category,'calls_helper_failed'))
        self.guard();size,digest=self.hash_staged(destination/CALLS_SNAPSHOT,options['hash_timeout_seconds'])
        self.space(0,size);self.guard()
        self.journal['archive']={'bytes':size,'sha256':digest}
    def helper_args(self,name,image,destination):
        """The helper's whole `docker create`: the limits, the two mounts and the script."""
        own=self.own;budget=own['capture_timeout_seconds']
        return ['create','--rm','--init','--name',name,'--label','personal-os.backup.id='+self.journal['id'],
            '--label','personal-os.backup.project='+self.cfg['project'],'--pull','never','--network','none','--read-only',
            '--cap-drop','ALL','--security-opt','no-new-privileges','--memory','64m','--memory-swap','64m',
            '--pids-limit','16','--cpus','0.5','--user',f'{CONTAINER_UID}:{CONTAINER_UID}',
            '--mount','type=bind,src='+own['data_mount']+',dst=/data,readonly',
            '--mount','type=bind,src='+str(destination)+',dst=/staging',
            '--env','SQLITE_TMPDIR=/staging','--entrypoint','python',image,'-I','-c',CALLS_SNAPSHOT_HELPER,
            '/data/'+CALLS_SNAPSHOT,'/staging',str(CALLS_STEP_PAGES),str(CALLS_STEP_PAUSE),str(CALLS_BUSY_PAUSE),
            str(CALLS_BUSY_RETRIES),str(CALLS_RESTART_PAUSE),str(budget-CALLS_HELPER_MARGIN),
            str(budget-CALLS_HELPER_ALARM_MARGIN),str(staged_options(self.cfg)['min_free_bytes'])]
    def reap_helper(self):
        """Remove this job's helper in any state and read back that it is gone, within REAP_SECONDS of elapsed time; only a
        container with this job's labels is touched. Unverified is `calls_helper_unverified`, and the journal keeps the name
        so the recovery looks again. Once it is gone a journal write that fails (a full disk) must not stop the cleanup that
        frees the disk: the journal then still names it, and the next recovery reads back the same absence."""
        if not (self.journal or {}).get('helper'):return
        name=CALLS_HELPER+self.journal['id'].lower();deadline=self.monotonic()+REAP_SECONDS;gone=False
        while (left:=deadline-self.monotonic())>0:
            try:
                if self.check_worker(self.docker.inspect(name,timeout=left)) is None:gone=True;break
                left=deadline-self.monotonic()
                if left>0:self.docker.call('rm','--force',name,timeout=left)
            except RuntimeError as exc:
                if str(exc)=='backup_container_mismatch':break
            except subprocess.TimeoutExpired:pass
            self.sleep(1)
        if not gone:raise RuntimeError('calls_helper_unverified')
        self.journal.pop('helper',None)
        try:self.save()
        except OSError:pass
    def reap_snapshot(self):
        """Kill and verify gone every process in the recordings container that writes this job's snapshot file.

        Matches the exact target path as one argument of a process listed by `docker top`; unverified after REAP_SECONDS
        is `recordings_exec_unverified`, and the journal keeps the target so the recovery looks again.
        """
        if self.calls:return self.reap_helper()
        target=(self.journal or {}).get('exec_target')
        if not target:return
        container=self.cfg['recordings']['container'];deadline=self.clock()+REAP_SECONDS
        while True:
            mine=[pid for pid,args in self.docker.top(container) if target in args.split()]
            if not mine:break
            if self.clock()>=deadline:raise RuntimeError('recordings_exec_unverified')
            for pid in mine:
                try:os.kill(pid,signal.SIGKILL)
                except ProcessLookupError:pass
            self.sleep(1)
        self.journal.pop('exec_target',None);self.save()
    def cleanup_unowned(self):
        """Under the runner's own lock only (the shared host lock is busy): what a crashed earlier job left in staging.

        Reaps a still-running snapshot producer, then empties staging; nothing of messaging or calendar is touched.
        A failure is reported as `staging_cleanup_failed`.
        """
        try:
            if self.journal_path.exists():
                self.load();self.reap_snapshot()
            if self.staging_root.exists():self.clean_staging()
        except Exception as exc:
            code=str(exc) if str(exc) in CLEANUP_CODES else 'staging_cleanup_failed'
            try:self.status('error',code)
            except Exception:pass
            raise RuntimeError(code) from None
    def hash_staged(self,path,seconds):
        """Size and SHA-256 of the snapshot through one descriptor (no link followed), within its own deadline."""
        started=self.clock()
        try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        except OSError:raise RuntimeError(self.code('snapshot_absent')) from None
        with os.fdopen(fd,'rb') as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):raise RuntimeError(self.code('snapshot_unsafe'))
            digest=hashlib.sha256();size=0
            while chunk:=f.read(1024**2):
                digest.update(chunk);size+=len(chunk)
                if self.clock()-started>seconds:raise RuntimeError(self.code('hash_timeout'))
        if self.clock()-started>seconds:raise RuntimeError(self.code('hash_timeout'))
        if size==0:raise RuntimeError(self.code('snapshot_empty'))
        return size,digest.hexdigest()
    def clean_staging(self):
        """Empty the staging directory (never the directory itself): plaintext of the archive must not linger.

        Refuses, before deleting anything, a mount point inside it or a directory on another device: removing
        the files of a mounted directory would delete what is mounted there.
        """
        root=self.staging_root;root.mkdir(mode=0o700,exist_ok=True)
        if root.is_symlink() or root.stat().st_mode&0o077:raise ValueError('private_backup_directory_required')
        if runner.mounts_under(root):raise ValueError('staging_mount_refused')
        device=root.stat().st_dev;folders=[]
        for entry in root.iterdir():
            if entry.is_symlink() or not entry.is_dir():continue
            for base,_,_ in os.walk(entry,followlinks=False):
                if os.lstat(base).st_dev!=device:raise ValueError('staging_mount_refused')
            folders.append(entry)
        for entry in root.iterdir():
            if entry in folders:shutil.rmtree(entry)
            else:entry.unlink()
    def create_recordings_worker(self,work):
        """The recordings profile's worker: the staged snapshot read-only, no network, no database, no secrets but age."""
        runner.write_json(self.unsettled_path,{'profile':'recordings','id':self.journal['id'],'container':self.journal['container']})
        options=recordings_options(self.cfg);archive=self.journal.get('archive')
        runner.write_json(work/'worker.json',{'profile':'recordings','id':self.journal['id'],'snapshot_directory':'/sources/snapshot',
            'recipient_file':'/run/secrets/recipient','work_directory':'/work',
            'integrity_timeout_seconds':options['integrity_timeout_seconds'],
            'encrypt_timeout_seconds':options['encrypt_timeout_seconds'],
            # What the runner hashed after the snapshot call: the worker refuses a file that differs.
            **({'expected_bytes':archive['bytes'],'expected_sha256':archive['sha256']} if archive else {})})
        self.docker.call('create','--name',self.journal['container'],'--label','personal-os.backup.id='+self.journal['id'],
            '--label','personal-os.backup.project='+self.cfg['project'],'--network','none',
            '--read-only','--user','0','--cap-drop','ALL','--cap-add','DAC_OVERRIDE','--security-opt','no-new-privileges',
            '--memory','384m','--cpus','0.75','--pids-limit','64','--tmpfs','/tmp:size=128m,noexec,nosuid',
            '--mount','type=bind,src='+str(work)+',dst=/work',
            '--mount','type=bind,src='+str(work/'worker.json')+',dst=/run/secrets/backup_config,readonly',
            '--mount','type=bind,src='+self.cfg['recipient_file']+',dst=/run/secrets/recipient,readonly',
            '--mount','type=bind,src='+str(self.staging_root/self.journal['id'])+',dst=/sources/snapshot,readonly',
            self.cfg['image'])
    def create_calls_worker(self,work):
        """The calls profile's worker: the staged snapshot read-only, no network, no database, no secrets but age."""
        runner.write_json(self.unsettled_path,{'profile':'calls','id':self.journal['id'],'container':self.journal['container']})
        options=staged_options(self.cfg);archive=self.journal['archive']
        runner.write_json(work/'worker.json',{'profile':'calls','id':self.journal['id'],'snapshot_directory':'/sources/snapshot',
            'recipient_file':'/run/secrets/recipient','work_directory':'/work','captured_at':self.journal['captured_at'],
            'integrity_timeout_seconds':options['integrity_timeout_seconds'],
            'encrypt_timeout_seconds':options['encrypt_timeout_seconds'],
            'expected_bytes':archive['bytes'],'expected_sha256':archive['sha256']})
        self.docker.call('create','--name',self.journal['container'],'--label','personal-os.backup.id='+self.journal['id'],
            '--label','personal-os.backup.project='+self.cfg['project'],'--network','none',
            '--read-only','--user','0','--cap-drop','ALL','--cap-add','DAC_OVERRIDE','--security-opt','no-new-privileges',
            '--memory','384m','--cpus','0.75','--pids-limit','64','--tmpfs','/tmp:size=128m,noexec,nosuid',
            '--mount','type=bind,src='+str(work)+',dst=/work',
            '--mount','type=bind,src='+str(work/'worker.json')+',dst=/run/secrets/backup_config,readonly',
            '--mount','type=bind,src='+self.cfg['recipient_file']+',dst=/run/secrets/recipient,readonly',
            '--mount','type=bind,src='+str(self.staging_root/self.journal['id'])+',dst=/sources/snapshot,readonly',
            self.cfg['image'])
