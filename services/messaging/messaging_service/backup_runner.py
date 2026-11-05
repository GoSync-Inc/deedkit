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
import sys as sys  # Public binding used by the journal failure-isolation tests.
import tempfile
import time

ID=re.compile(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{16}')
SERVICES={'telegram','whatsapp1','whatsapp2'}
COLLECTOR_LABEL='org.messaging-archive.collector'
SAFE_ERRORS={'backup_capture_failed','backup_capture_timeout','backup_container_disappeared','backup_container_mismatch',
    'backup_delivery_failed','invalid_backup_receipt','local_backup_corrupt','receiver_quota','receiver_disk_reserve',
    'receiver_busy','backup_id_conflict','docker_inspection_failed','docker_operation_failed',
    'backup_fence_release_failed','backup_capture_unfenced','backup_fence_retained',
    'collector_missing_during_resume','collector_identity_changed','collector_missing_during_stop','host_lock_busy',
    'recordings_snapshot_absent','recordings_snapshot_empty','recordings_snapshot_unsafe','recordings_snapshot_corrupt',
    'recordings_snapshot_foreign','recordings_schema_unsupported','recordings_identity_missing',
    'staging_mount_refused','staging_cleanup_failed','recordings_snapshot_changed','recordings_integrity_timeout',
    'recordings_encrypt_timeout','recordings_no_space','recordings_exec_timeout','recordings_exec_failed',
    'recordings_hash_timeout','recordings_exec_unverified',
    'recordings_staging_unsafe',
    # A skip is not a failure (ANYM-727): nothing was sealed, no other profile was touched, a later start tries again.
    'recordings_skipped_lock_busy','recordings_skipped_window','recordings_skipped_schedule_unknown',
    'recordings_skipped_disk','recordings_skipped_container_down','recordings_skipped_archive_disabled',
    'recordings_skipped_yielded',
    # The calls profile (ANYM-778).
    'no_calls_container','calls_helper_timeout','calls_helper_failed','calls_helper_unverified','calls_snapshot_busy',
    'calls_snapshot_absent','calls_snapshot_empty','calls_snapshot_unsafe','calls_snapshot_corrupt','calls_snapshot_foreign',
    'calls_snapshot_changed','calls_integrity_timeout','calls_encrypt_timeout','calls_no_space','calls_hash_timeout',
    'calls_staging_unsafe','calls_skipped_lock_busy','calls_skipped_window','calls_skipped_schedule_unknown',
    'calls_skipped_disk','calls_skipped_yielded'}
# The receiver's own verifier holds its upload lock for a whole restore (about an hour). A delivery it refuses as busy
# before it stores anything (stream_v2: before the stream started) is offered again, same backup ID, every
# BUSY_RETRY_SECONDS while the bound since the first offer allows; each offer keeps its upload timeout.
BUSY_RETRY_SECONDS=60
BUSY_WAIT_SECONDS=7200
# The calendar profile (ANYM-608) waits this long for messaging's runner lock, which a deploy and messaging's own run
# hold, so a 16:00 run gives up before messaging's 18:00 one. Its worker owns no application container.
HOST_LOCK_WAIT_SECONDS=5400
HOST_LOCK_POLL_SECONDS=5
ROTATION_TOKEN_ENV='DEEDKIT_ROTATION_BACKUP_TOKEN'  # scripts/calendar_rotate.py's own backup run (ANYM-784)
# Each wait for a worker container runs `docker container inspect` once per poll (ANYM-635).
POLL_SECONDS=1.0
CALENDAR_WORKER='deedkit-calendar-backup-'
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
IMAGE_ID=re.compile(r'sha256:[0-9a-f]{64}')
MOUNTINFO='/proc/self/mountinfo'  # the Linux mount table; absent elsewhere (a development host), then no mount is known


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


class Skip(RuntimeError):
    """A recordings job that ends by choice (ANYM-727): fixed `code`, exit 0, `state: skipped` in status.json."""
    def __init__(self,code):super().__init__(code);self.code=code


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


def nested(a,b):
    """True when one path is the other or lies inside it."""
    a,b=Path(a),Path(b)
    return a==b or a in b.parents or b in a.parents


def mounts_under(root,mountinfo=None):
    """Mount points strictly inside `root`, read from the mount table (octal escapes undone)."""
    try:text=Path(mountinfo or MOUNTINFO).read_text()
    except FileNotFoundError:return []
    root=os.path.realpath(root).rstrip('/');found=[]
    for line in text.splitlines():
        fields=line.split(' ')
        if len(fields)<5:continue
        point=re.sub(r'\\([0-7]{3})',lambda m:chr(int(m.group(1),8)),fields[4])
        if point.startswith(root+'/'):found.append(point)
    return found


DAILY_START=re.compile(r'\*-\*-\* ([01][0-9]|2[0-3]):([0-5][0-9])(?::([0-5][0-9]))? UTC')


def timer_starts(path,profile='recordings'):
    """Seconds after 00:00 UTC of every `OnCalendar=*-*-* HH:MM[:SS] UTC` line of a systemd timer file.

    Anything else (another calendar form, no such line, an unreadable file) is unknown, and an unknown schedule
    stops the job (fail closed): the admission must never guess when another backup starts.
    """
    unknown=profile+'_skipped_schedule_unknown'
    try:
        with open(path,'rb') as f:text=f.read(65537).decode()
    except (OSError,UnicodeDecodeError):raise Skip(unknown) from None
    if len(text)>65536:raise Skip(unknown)
    starts=[]
    for line in text.splitlines():
        line=line.strip()
        if not line.startswith('OnCalendar='):continue
        match=DAILY_START.fullmatch(line[len('OnCalendar='):].strip())
        if match is None:raise Skip(unknown)
        starts.append(int(match[1])*3600+int(match[2])*60+int(match[3] or 0))
    if not starts:raise Skip(unknown)
    return starts


def timer_delay(path,profile='recordings'):
    """`RandomizedDelaySec` of a timer file in seconds (0 when absent); a form this does not understand is unknown."""
    unknown=profile+'_skipped_schedule_unknown'
    try:
        with open(path,'rb') as f:text=f.read(65537).decode()
    except (OSError,UnicodeDecodeError):raise Skip(unknown) from None
    delay=0
    for line in text.splitlines():
        line=line.strip()
        if not line.startswith('RandomizedDelaySec='):continue
        match=re.fullmatch(r'(\d{1,6})\s*(s|sec|min|m|h)?',line[len('RandomizedDelaySec='):].strip())
        if match is None:raise Skip(unknown)
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
    delivery=cfg['timeout_seconds']+cfg.get('busy_wait_seconds',BUSY_WAIT_SECONDS)
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
        if not isinstance(cfg[key],str) or not path_valid(cfg[key]):raise ValueError('invalid_backup_path')
    if Path(cfg['host_lock'])==Path(cfg['directory'])/'.runner.lock':raise ValueError('invalid_backup_path')
    own=cfg['recordings']
    required={'staging_directory','state_directory','capture_timeout_seconds','container','container_config',
              'container_staging','timers'}
    if not isinstance(own,dict) or set(own)-set(RECORDINGS_DEFAULTS)!=required:raise ValueError('invalid_backup_config')
    for key in ('staging_directory','state_directory'):
        if not isinstance(own[key],str) or not path_valid(own[key]):raise ValueError('invalid_backup_path')
    # The runner empties the staging directory: it must be nothing else's (not the runner root, its lock, or state).
    staging,state=own['staging_directory'],own['state_directory']
    if (nested(staging,state) or nested(staging,cfg['directory']) or nested(state,cfg['directory']) or
        nested(staging,cfg['host_lock']) or nested(state,cfg['host_lock'])):raise ValueError('invalid_backup_path')
    if not transfer_valid(cfg['transfer']):raise ValueError('invalid_backup_transfer')
    # Emptying the staging directory must never reach a credential this runner needs.
    if any(nested(staging,cfg[key]) for key in ('recipient_file',)) or any(
        nested(staging,cfg['transfer'][key]) for key in ('identity_file','known_hosts_file')):
        raise ValueError('invalid_backup_path')
    if not isinstance(own['container'],str) or not (len(own['container'])<=128 and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*',own['container'])):
        raise ValueError('invalid_backup_config')
    if not container_path(own['container_config']) or not container_path(own['container_staging']):
        raise ValueError('invalid_backup_path')
    # Other profiles' timers: read at every start for the next scheduled time (never hard-coded); none of them may be a
    # file this job writes or deletes.
    timers=own['timers']
    if (not isinstance(timers,list) or not 1<=len(timers)<=8 or len(set(timers))!=len(timers) or
        not all(isinstance(t,str) and path_valid(t) and t.endswith('.timer') for t in timers) or
        any(nested(staging,t) or nested(state,t) or nested(cfg['directory'],t) for t in timers)):
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
        if not isinstance(cfg[key],str) or not path_valid(cfg[key]):raise ValueError('invalid_backup_path')
    if Path(cfg['host_lock'])==Path(cfg['directory'])/'.runner.lock':raise ValueError('invalid_backup_path')
    own=cfg['calls']
    if not isinstance(own,dict) or set(own)!={'staging_directory','state_directory','capture_timeout_seconds','data_mount',
                                              'calls_container','timers'}:raise ValueError('invalid_backup_config')
    for key in ('staging_directory','state_directory','data_mount'):
        if not isinstance(own[key],str) or not path_valid(own[key]):raise ValueError('invalid_backup_path')
    staging,state,data=own['staging_directory'],own['state_directory'],own['data_mount']
    # The runner empties staging; the helper sees the data mount: neither may reach anything else of this job.
    if (nested(staging,state) or nested(staging,cfg['directory']) or nested(state,cfg['directory']) or
        nested(staging,cfg['host_lock']) or nested(state,cfg['host_lock']) or nested(data,staging) or
        nested(data,state) or nested(data,cfg['directory']) or nested(data,cfg['host_lock'])):raise ValueError('invalid_backup_path')
    if not transfer_valid(cfg['transfer']):raise ValueError('invalid_backup_transfer')
    if nested(staging,cfg['recipient_file']) or any(nested(staging,cfg['transfer'][key]) for key in ('identity_file','known_hosts_file')):
        raise ValueError('invalid_backup_path')
    if not isinstance(own['calls_container'],str) or not (len(own['calls_container'])<=128 and
        re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*',own['calls_container'])):raise ValueError('invalid_backup_config')
    timers=own['timers']
    if (not isinstance(timers,list) or not 1<=len(timers)<=8 or not all(isinstance(t,str) for t in timers) or
        len(set(timers))!=len(timers) or not all(path_valid(t) and t.endswith('.timer') for t in timers) or
        any(nested(staging,t) or nested(state,t) or nested(cfg['directory'],t) for t in timers)):
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


def config(path):
    cfg=read_json(path)
    if not isinstance(cfg,dict) or cfg.get('profile','messaging') not in ('messaging','calendar','recordings','calls'):
        raise ValueError('invalid_backup_config')
    if cfg.get('profile')=='calls':return calls_config(cfg)
    if cfg.get('profile')=='calendar':return calendar_config(cfg)
    if cfg.get('profile')=='recordings':return recordings_config(cfg)
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
    def call(*args,timeout=90):
        result=subprocess.run(['docker',*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
        if result.returncode:raise RuntimeError('docker_operation_failed')
        return result.stdout.decode().strip()
    @staticmethod
    def inspect(name,timeout=30):
        result=subprocess.run(['docker','container','inspect',name],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
        if result.returncode:
            if b'No such container' in result.stderr or b'No such object' in result.stderr:return None
            raise RuntimeError('docker_inspection_failed')
        return json.loads(result.stdout)[0]
    @staticmethod
    def top(container):
        """(host pid, command line) of every process of a running container; none when it is not running."""
        info=Docker.inspect(container)
        if info is None or not info['State']['Running']:return []
        rows=[]
        for line in Docker.call('top',container,'-eo','pid,args').splitlines()[1:]:
            pid,_,args=line.strip().partition(' ')
            if pid.isdigit():rows.append((int(pid),args.strip()))
        return rows
    @staticmethod
    def image_of(container):
        """The container's immutable image ID now; None when there is no such container."""
        result=subprocess.run(['docker','inspect','--type','container','--format','{{.Image}}',container],
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        if result.returncode:
            if b'No such' in result.stderr:return None
            raise RuntimeError('docker_inspection_failed')
        image=result.stdout.decode(errors='replace').strip()
        if not IMAGE_ID.fullmatch(image):raise RuntimeError('docker_inspection_failed')
        return image
    @staticmethod
    def attach(container,timeout):
        """`docker start --attach` with a deadline: (exit code, the first 4096 bytes of stdout). stderr is discarded."""
        try:result=subprocess.run(['docker','start','--attach',container],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=timeout)
        except subprocess.TimeoutExpired:raise RuntimeError('calls_helper_timeout') from None
        return result.returncode,result.stdout[:4096].decode(errors='replace')
    @staticmethod
    def exec(container,args,timeout):
        """`docker exec` with its own deadline: (exit code, the first 2000 bytes of stderr). No output is kept otherwise."""
        try:result=subprocess.run(['docker','exec',container,*args],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=timeout)
        except subprocess.TimeoutExpired:raise RuntimeError('recordings_exec_timeout') from None
        return result.returncode,result.stderr[:2000].decode(errors='replace')
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
        self.monotonic=time.monotonic;self.docker=docker or Docker();self.clock=clock;self.sleep=sleep;self.send=send;self.stream_send=stream_send;self.journal=None
        self.calendar=cfg.get('profile')=='calendar';self.recordings=cfg.get('profile')=='recordings'
        # Both own no application container, open no fence and take the host lock; each names only its own worker.
        self.calls=cfg.get('profile')=='calls';self.staged_profile=self.recordings or self.calls
        self.hostless=self.calendar or self.staged_profile
        self.worker_prefix=RECORDINGS_WORKER if self.recordings else CALLS_WORKER if self.calls else CALENDAR_WORKER
        # The calendar and recordings deliver to the R2 receiver, which takes only the v2 wire (ANYM-607).
        if send is None:self.send=upload_file_v2 if self.hostless else upload
        # Exists from before the worker is created until it is verified stopped: a deploy refuses while the calendar's
        # exists (production.py), also after this process died or failed its recovery and released the locks.
        self.unsettled_path=Path(cfg['host_lock']+'.unsettled') if self.calendar else None
        if self.staged_profile:
            self.own=cfg[cfg['profile']]
            state=Path(self.own['state_directory']);state.mkdir(mode=0o700,exist_ok=True)
            if state.is_symlink() or state.stat().st_mode&0o077:raise ValueError('private_backup_directory_required')
            self.unsettled_path=state/'.unsettled'
            self.staging_root=Path(self.own['staging_directory'])
            self.container_uid=self.container_gid=CONTAINER_UID;self.disk=shutil.disk_usage;self.yield_at=float('inf')
    def code(self,name):return self.cfg['profile']+'_'+name
    def save(self):write_json(self.journal_path,self.journal)
    def settle(self,container):
        """Remove the unsettled marker once `container` (this profile's own worker) is absent or stopped."""
        if not self.hostless or not os.path.lexists(self.unsettled_path):return
        if not container.startswith(self.worker_prefix):raise RuntimeError('backup_container_mismatch')
        info=self.docker.inspect(container)
        if info is not None and info['State']['Running']:raise RuntimeError('backup_shutdown_unverified')
        self.unsettled_path.unlink()
    def load(self):
        self.journal=read_json(self.journal_path)
        if not ID.fullmatch(self.journal['id']):raise ValueError('invalid_backup_journal')
        # A calendar job names only its own worker: never an application container to stop, start or remove.
        if self.hostless and (self.journal['stopped'] or self.journal['container']!=self.worker_prefix+self.journal['id'].lower()):
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
        if self.journal_path.exists():
            self.load()
            # A snapshot producer of an earlier attempt must be gone before anything is staged, cleaned or run again.
            if self.staged_profile:self.reap_snapshot()
            # A job past its capture needs no plaintext: whatever a crash left in staging goes before delivery resumes.
            if self.staged_profile and self.journal['phase']=='upload':self.clean_staging()
            return
        backup_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(8)
        work=self.root/backup_id;work.mkdir(mode=0o700)
        stopped=[] if self.hostless else self.docker.running_collectors(self.cfg['project'])
        self.journal={'id':backup_id,'phase':'stopping','stopped':stopped,'created':self.clock(),
                      'container':(self.worker_prefix if self.hostless else 'pos-backup-')+backup_id.lower()}
        self.save();self.status('capturing')
        if self.staged_profile:
            # Plaintext left by a crashed earlier job goes first; this job's directory is then staged into.
            self.clean_staging();(self.staging_root/backup_id).mkdir(mode=0o700)
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
            if now-previous_start(now,starts)<DUE_GRACE_SECONDS+timer_delay(path,profile):raise Skip(self.code('skipped_window'))
            following=min(next_start(now,starts),following or 1<<62)
        if now+admission_bound(self.cfg)>following-GUARD_SECONDS:raise Skip(self.code('skipped_window'))
        self.yield_at=following-GUARD_SECONDS
    def guard(self):
        """A checkpoint before anything is sealed: past the yield instant the job gives way and cleans up."""
        if self.clock()>=self.yield_at:raise Skip(self.code('skipped_yielded'))
    def space(self,plain,cipher):
        """Free space for `plain` more bytes of plaintext in staging and `cipher` of ciphertext in the job directory,
        counted together when both are on one filesystem, above the floor."""
        floor=staged_options(self.cfg)['min_free_bytes'];staging,root=self.staging_root,self.root
        needs=[(staging,plain+cipher)] if os.stat(staging).st_dev==os.stat(root).st_dev else [(staging,plain),(root,cipher)]
        for path,size in needs:
            if self.disk(path).free<floor+size:raise Skip(self.code('skipped_disk'))
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
        previous=read_json(self.status_path).get('archive_bytes') if self.status_path.exists() else None
        estimate=max(options['expected_archive_bytes'],previous if type(previous) is int else 0)
        self.space(estimate,estimate)
        box=self.docker.inspect(own['container'])
        if box is None or not box['State']['Running']:raise Skip('recordings_skipped_container_down')
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
            if 'archive_disabled' in error:raise Skip('recordings_skipped_archive_disabled')
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
        previous=read_json(self.status_path).get('archive_bytes') if self.status_path.exists() else None
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
        if mounts_under(root):raise ValueError('staging_mount_refused')
        device=root.stat().st_dev;folders=[]
        for entry in root.iterdir():
            if entry.is_symlink() or not entry.is_dir():continue
            for base,_,_ in os.walk(entry,followlinks=False):
                if os.lstat(base).st_dev!=device:raise ValueError('staging_mount_refused')
            folders.append(entry)
        for entry in root.iterdir():
            if entry in folders:shutil.rmtree(entry)
            else:entry.unlink()
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
    def create_recordings_worker(self,work):
        """The recordings profile's worker: the staged snapshot read-only, no network, no database, no secrets but age."""
        write_json(self.unsettled_path,{'profile':'recordings','id':self.journal['id'],'container':self.journal['container']})
        options=recordings_options(self.cfg);archive=self.journal.get('archive')
        write_json(work/'worker.json',{'profile':'recordings','id':self.journal['id'],'snapshot_directory':'/sources/snapshot',
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
        write_json(self.unsettled_path,{'profile':'calls','id':self.journal['id'],'container':self.journal['container']})
        options=staged_options(self.cfg);archive=self.journal['archive']
        write_json(work/'worker.json',{'profile':'calls','id':self.journal['id'],'snapshot_directory':'/sources/snapshot',
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
    def refusal(self,work):
        """The worker's fixed refusal code, if it wrote one (staged profiles only); never anything else from the file."""
        if not self.staged_profile:return None
        try:data=read_json(work/'refused.json')
        except (OSError,ValueError):return None
        code=data.get('code') if isinstance(data,dict) else None
        return code if isinstance(code,str) and code in SAFE_ERRORS and code.startswith(self.cfg['profile']+'_') else None
    def create_worker(self,work):
        if self.calendar:return self.create_calendar_worker(work)
        if self.recordings:return self.create_recordings_worker(work)
        if self.calls:return self.create_calls_worker(work)
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
            if self.staged_profile:self.stage_snapshot(self.staging_root/self.journal['id']);self.journal['capture_started']=self.clock()
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
                if info['State']['ExitCode']!=0:raise RuntimeError(self.refusal(work) or 'backup_capture_failed')
                self.resume_collectors();break
            if self.staged_profile:self.guard()  # another profile is about to start: give way before anything is sealed
            # The worker's own budget starts when it can start, after the staging work (which has deadlines of its own).
            began=self.journal.get('capture_started',self.journal['created']) if self.staged_profile else self.journal['created']
            if self.clock()-began>(self.own['capture_timeout_seconds'] if self.staged_profile
                                   else self.cfg['timeout_seconds']):raise RuntimeError('backup_capture_timeout')
            self.sleep(POLL_SECONDS)
        if self.staged_profile:self.clean_staging()  # the worker has exited: the bundle holds the only copy now
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
        if self.hostless:return  # The calendar and recordings captures open no attachment fence.
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
        if self.hostless:
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
        if self.staged_profile and type(result.get('snapshot',{}).get('bytes')) is int:status['archive_bytes']=result['snapshot']['bytes']
        write_json(self.status_path,status)
        info=self.check_worker(self.docker.inspect(self.journal['container']))
        if info:self.docker.call('rm',self.journal['container'])
        self.settle(self.journal['container'])
        self.journal_path.unlink();self.journal=None
        completed=sorted((p for p in self.root.iterdir() if p.is_dir() and ID.fullmatch(p.name) and (p/'delivery.json').is_file()),
                         key=lambda p:p.name,reverse=True)
        for old in completed[self.cfg['keep_local']:]:shutil.rmtree(old)
    def run(self):
        if self.staged_profile:
            resumed=read_json(self.journal_path).get('phase') if self.journal_path.exists() else None
            self.admit('upload' if resumed=='upload' else 'full')
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
            if self.hostless and not re.fullmatch('[a-f0-9]{64}',str(result.get('snapshot_sha256'))):
                raise RuntimeError('invalid_backup_result')
            with path.open('rb') as f:
                if path.stat().st_size!=result['bytes'] or hashlib.file_digest(f,'sha256').hexdigest()!=result['sha256']:
                    raise RuntimeError('local_backup_corrupt')
            self.cleanup_plaintext(work);self.status('uploading')
            receipt,_=self.deliver(lambda:asyncio.run(self.send(path,result,self.cfg['transfer'],self.cfg['timeout_seconds'])))
            self.finish(work,result,receipt)
        except BaseException as exc:
            self.status('skipped' if isinstance(exc,Skip) else 'error',str(exc) if str(exc) in SAFE_ERRORS else 'backup_job_failed')
            # A snapshot producer not verified gone still holds the file: the recovery reaps it first, then cleans.
            if self.staged_profile and not (self.journal or {}).get('exec_target') and not (self.journal or {}).get('helper'):
                try:self.clean_staging()
                except (OSError,ValueError):pass  # the next start empties it again
            raise
    def recover_stop(self):
        """systemd ExecStopPost: settle the owned container, then resume its collectors.

        A recordings job's staging is emptied after that attempt, whatever its outcome: a stop that failed is not
        masked by the cleanup, and a cleanup that failed does not undo a verified stop (status `staging_cleanup_failed`).
        """
        failure=None;reaped=True
        if self.staged_profile and self.journal_path.exists():
            # A snapshot producer still writing into staging must be gone before anything is cleaned or settled.
            try:self.load();self.reap_snapshot()
            except BaseException as exc:
                failure=exc;reaped=False
                try:self.status('error',str(exc) if str(exc) in CLEANUP_CODES else 'backup_job_failed')
                except Exception:pass
        # With a producer not verified gone nothing is settled: the journal (which names it) stays for the next recovery.
        if reaped:
            try:self.settle_job()
            except BaseException as exc:failure=failure or exc
        if self.staged_profile and reaped:
            try:self.clean_staging()
            except Exception as exc:
                try:self.status('error','staging_mount_refused' if str(exc)=='staging_mount_refused' else 'staging_cleanup_failed')
                except Exception:pass
                failure=failure or exc
        if failure is not None:raise failure
    def settle_job(self):
        if not self.journal_path.exists():
            if self.hostless and os.path.lexists(self.unsettled_path):self.settle(read_json(self.unsettled_path)['container'])
            return
        self.load()
        if not self.hostless:
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
        if not self.status_path.exists() or read_json(self.status_path).get('state') not in ('error','skipped'):
            self.status('error','backup_job_failed')
        self.settle(self.journal['container'])


@contextmanager
def hold_locks(cfg,wait=HOST_LOCK_WAIT_SECONDS,busy=None,host=True):
    """The runner's own lock; a calendar run then also messaging's lock (`host_lock`), the one a deploy holds.

    The host lock is waited for at most `wait` seconds (`host_lock_busy`); both are held until the run ends. A recordings
    run asks for no wait (ANYM-727): busy means another profile or a deploy is running, so it skips and a catch-up retries.
    `busy` (a recordings run) is called with the runner's own lock held before that give-up, so what a crashed earlier
    job left in staging goes even then; `host=False` (a recordings recovery, which touches nothing of the others) takes
    the runner's own lock only.
    """
    fds=[]
    try:
        fds.append(os.open(Path(cfg['directory'])/'.runner.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600))
        try:fcntl.flock(fds[0],fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('backup_job_already_running') from None
        if host and cfg.get('profile') in ('calendar','recordings','calls'):
            fds.append(os.open(cfg['host_lock'],os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600))
            deadline=time.monotonic()+wait
            while True:
                try:fcntl.flock(fds[1],fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    left=deadline-time.monotonic()
                    if left<=0:
                        if busy is not None:busy()
                        raise (Skip(cfg['profile']+'_skipped_lock_busy') if cfg['profile'] in ('recordings','calls') and wait==0
                               else RuntimeError('host_lock_busy')) from None
                    time.sleep(min(HOST_LOCK_POLL_SECONDS,left))
        yield
    finally:
        for fd in reversed(fds):os.close(fd)


def rotation_admission(cfg):
    """A calendar run waits for scripts/calendar_rotate.py (ANYM-784): while its marker exists, only the tool's own run
    in phase `verifying` (the token the marker holds) starts. Doubt refuses. The catch-up timer starts it again later."""
    if cfg.get('profile')!='calendar':return
    marker=Path(cfg['directory']).parent/'rotation'/'rotation.unsettled'
    if not os.path.lexists(marker):return
    try:data=read_json(marker)
    except Exception:raise RuntimeError('rotation_in_progress') from None
    token=os.environ.get(ROTATION_TOKEN_ENV)
    if isinstance(data,dict) and data.get('phase')=='verifying' and token and data.get('backup_token')==token:return
    raise RuntimeError('rotation_in_progress')


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
    owns_lock=False;code='backup_job_failed';unrecovered=None
    try:
        # A recordings run does not wait for the host lock and cleans up even when it is busy; its recovery needs only the
        # runner's own lock (it touches nothing of messaging or calendar).
        recordings=runner.staged_profile  # the calls profile follows the same rule (ANYM-778)
        if not args.recover_stop:rotation_admission(runner.cfg)  # no lock wait for a run that cannot start
        with hold_locks(runner.cfg,0 if recordings and not args.recover_stop else HOST_LOCK_WAIT_SECONDS,
                        runner.cleanup_unowned if recordings and not args.recover_stop else None,
                        not (recordings and args.recover_stop)):
            owns_lock=True
            # Again with the locks held: the tool writes its marker under them, and this run may have waited an hour.
            if not args.recover_stop:rotation_admission(runner.cfg)
            try:
                if args.recover_stop:runner.recover_stop()
                else:runner.run()
            except Exception:
                try:runner.recover_stop()
                except Exception as again:unrecovered=again  # Journal remains for ExecStopPost / next operator run.
                raise
    except Exception as exc:
        if runner.staged_profile and str(unrecovered) in CLEANUP_CODES:code=str(unrecovered)
        elif runner.staged_profile and isinstance(unrecovered,OSError):code='staging_cleanup_failed'
        elif runner.staged_profile and str(exc) in CLEANUP_CODES:code=str(exc)
        elif runner.calls and str(exc) in SAFE_ERRORS:code=str(exc)
        # A skip whose cleanup failed is a failure: plaintext may remain.
        if isinstance(exc,Skip) and not (runner.staged_profile and unrecovered is not None):
            try:runner.status('skipped',exc.code)
            except Exception:pass
            print(json.dumps({'status':'skipped','code':exc.code},separators=(',',':')),flush=True)
            raise SystemExit(0) from None
        if (not owns_lock and str(exc)=='host_lock_busy') or str(exc)=='rotation_in_progress':
            code=str(exc)
            try:runner.status('error',code)
            except Exception:pass
        print(json.dumps({'status':'error','code':code},separators=(',',':')),flush=True)
        raise SystemExit(1) from None


if __name__=='__main__':main()
