"""Recordings archive bundle (ANYM-714): one SQLite snapshot sealed under a `recordings` profile manifest.

The snapshot is made elsewhere (`deedkit-recordings --backup-to`, staged by ANYM-727). This module only refuses a
snapshot that must not become a bundle and records the facts the later continuity decision (ANYM-728) needs:
the SQLite schema version, size, SHA-256 and the archive identity from the snapshot's `meta`.
Whether a valid archive lost data is not decided here. No provider call and no recordings code.
"""
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from urllib.parse import quote

from .spool import fsync_dir

APPLICATION_ID=0x444B5243  # services/recordings archive_store.APPLICATION_ID
# Every schema this package supports: 1-2 shipped, 3 (ANYM-724), 4 (ANYM-725), 5 (ANYM-730 tombstones).
SUPPORTED_USER_VERSIONS=range(1,6)
SQLITE_HEADER=b'SQLite format 3\x00'
MEMBERS=('manifest.json','state/archive.sqlite')
STATE_FILE='archive.sqlite'
MANIFEST_KEYS={'profile','format','created_at','captured_at','user_version','bytes','sha256','archive'}
ARCHIVE_KEYS={'archive_id','incarnation','sequence','records'}
ARCHIVE_ID=re.compile(r'[A-Za-z0-9._:-]{1,128}')
MAX_INT=2**63-1  # SQLite's INTEGER range
# The only codes a refusal may carry (runner SAFE_ERRORS and status); they name no path and no content.
REFUSAL_CODES=('recordings_snapshot_absent','recordings_snapshot_empty','recordings_snapshot_unsafe',
               'recordings_snapshot_corrupt','recordings_snapshot_foreign','recordings_schema_unsupported',
               'recordings_identity_missing','recordings_snapshot_changed','recordings_integrity_timeout',
               'recordings_encrypt_timeout','recordings_no_space')


class RecordingsRefused(RuntimeError):
    """A snapshot that must not become a bundle; no bundle exists and the other profiles are untouched."""
    def __init__(self,code):
        if code not in REFUSAL_CODES:raise ValueError('invalid_refusal_code')
        super().__init__(code);self.code=code


def valid_recordings_member(name):
    return name in MEMBERS


def _columns(db,table):
    return {row[1] for row in db.execute(f'PRAGMA table_info({table})').fetchall()}


def _identity(db):
    """`meta` is key/value rows (ANYM-724): exactly one `archive_id` (text) and one `incarnation` (integer >= 1).

    A missing table, a duplicated key, another type or another shape is no identity: the facts must be unambiguous.
    """
    if not {'key','value'}<=_columns(db,'meta'):raise RecordingsRefused('recordings_identity_missing')
    rows=db.execute("SELECT key,value,typeof(value) FROM meta WHERE key IN ('archive_id','incarnation')").fetchall()
    found={}
    for key,value,kind in rows:
        if key in found:raise RecordingsRefused('recordings_identity_missing')
        found[key]=(value,kind)
    archive_id,incarnation=found.get('archive_id',(None,None)),found.get('incarnation',(None,None))
    if (archive_id[1]!='text' or not ARCHIVE_ID.fullmatch(archive_id[0]) or incarnation[1]!='integer' or
        not 1<=incarnation[0]<=MAX_INT):raise RecordingsRefused('recordings_identity_missing')
    return archive_id[0],incarnation[0]


def _counts(db):
    """Highest journal sequence and record count; a different shape or type is a damaged archive, not a fact."""
    if 'sequence' not in _columns(db,'events') or not _columns(db,'records'):raise RecordingsRefused('recordings_snapshot_corrupt')
    sequence,kind=db.execute('SELECT MAX(sequence),typeof(MAX(sequence)) FROM events').fetchone()
    if kind not in ('integer','null') or (kind=='integer' and not 0<=sequence<=MAX_INT):
        raise RecordingsRefused('recordings_snapshot_corrupt')
    return sequence or 0,db.execute('SELECT COUNT(*) FROM records').fetchone()[0]


def inspect_archive(path,deadline=None):
    """Facts of a snapshot file, or RecordingsRefused. Opened read-only and immutable: the file is never written.

    `deadline` (an absolute `time.monotonic()` value) bounds the integrity check: SQLite is interrupted from its
    progress handler, so a huge or hostile file ends `recordings_integrity_timeout` instead of running on.
    """
    path=Path(path)
    try:info=path.lstat()
    except FileNotFoundError:raise RecordingsRefused('recordings_snapshot_absent') from None
    except OSError:raise RecordingsRefused('recordings_snapshot_unsafe') from None
    if not stat.S_ISREG(info.st_mode):raise RecordingsRefused('recordings_snapshot_unsafe')
    if info.st_size==0:raise RecordingsRefused('recordings_snapshot_empty')
    try:
        with path.open('rb') as f:header=f.read(16)
        if header!=SQLITE_HEADER:raise RecordingsRefused('recordings_snapshot_corrupt')
        db=sqlite3.connect('file:'+quote(str(path))+'?mode=ro&immutable=1',uri=True)
        if deadline is not None:db.set_progress_handler(lambda:1 if time.monotonic()>deadline else 0,100)
        try:
            if db.execute('PRAGMA application_id').fetchone()[0]!=APPLICATION_ID:
                raise RecordingsRefused('recordings_snapshot_foreign')
            user_version=db.execute('PRAGMA user_version').fetchone()[0]
            if user_version not in SUPPORTED_USER_VERSIONS:raise RecordingsRefused('recordings_schema_unsupported')
            if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise RecordingsRefused('recordings_snapshot_corrupt')
            archive_id,incarnation=_identity(db)
            sequence,records=_counts(db)
        finally:db.close()
    except sqlite3.Error:
        if deadline is not None and time.monotonic()>deadline:raise RecordingsRefused('recordings_integrity_timeout') from None
        raise RecordingsRefused('recordings_snapshot_corrupt') from None
    except OSError:raise RecordingsRefused('recordings_snapshot_unsafe') from None
    return {'user_version':user_version,
            'archive':{'archive_id':archive_id,'incarnation':incarnation,'sequence':sequence,'records':records}}


def _copy_staged(source,target):
    """Copy through one descriptor (no symlink followed), hashing the bytes written; returns (bytes, sha256, mtime)."""
    try:fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except FileNotFoundError:raise RecordingsRefused('recordings_snapshot_absent') from None
    except OSError:raise RecordingsRefused('recordings_snapshot_unsafe') from None
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):raise RecordingsRefused('recordings_snapshot_unsafe')
    except BaseException:
        os.close(fd);raise  # a directory opens read-only but cannot be wrapped by fdopen
    with os.fdopen(fd,'rb') as src:
        sha=hashlib.sha256();size=0
        out=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(out,'wb') as dst:
            while chunk:=src.read(1024**2):sha.update(chunk);dst.write(chunk);size+=len(chunk)
            dst.flush();os.fsync(dst.fileno())
    return size,sha.hexdigest(),info.st_mtime


def _hash_staged(source):
    """sha256, size and mtime of `source` read through one descriptor (no symlink followed, regular files only)."""
    try:fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except FileNotFoundError:raise RecordingsRefused('recordings_snapshot_absent') from None
    except OSError:raise RecordingsRefused('recordings_snapshot_unsafe') from None
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):raise RecordingsRefused('recordings_snapshot_unsafe')
    except BaseException:
        os.close(fd);raise  # a directory opens read-only but cannot be wrapped by fdopen
    with os.fdopen(fd,'rb') as src:return info.st_size,hashlib.file_digest(src,'sha256').hexdigest(),info.st_mtime


def _write_manifest(directory,size,digest,mtime,facts):
    manifest={'profile':'recordings','format':1,'created_at':datetime.now(timezone.utc).isoformat(),
              'captured_at':datetime.fromtimestamp(mtime,timezone.utc).isoformat(),
              'user_version':facts['user_version'],'bytes':size,'sha256':digest,'archive':facts['archive']}
    with (directory/'manifest.json').open('x') as out:
        out.write(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n');out.flush();os.fsync(out.fileno())


def create_recordings_snapshot(staged,destination,*,copy_state=True,expected=None,integrity_seconds=None):
    """Check `staged` (an SQLite snapshot file) and write the bundle directory at `destination`; refuse a bad one.

    `copy_state=False` (the backup worker) writes only `manifest.json`: the archive is read in place from its
    private staging directory and streamed into the encryption (`encrypt_snapshot(..., recordings_state=staged)`),
    so no plaintext copy of it exists anywhere else. `copy_state=True` also copies it to `state/archive.sqlite`
    (a complete bundle, as a restore produces) and checks the copy.

    `expected` is the runner's `{bytes, sha256}` of the staged file (ANYM-727): a file that differs from what the
    runner hashed after the snapshot call ends `recordings_snapshot_changed`. `integrity_seconds` bounds the check.
    """
    destination=Path(destination);staged=Path(staged)
    if destination.exists():raise ValueError('backup_destination_exists')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=Path(tempfile.mkdtemp(prefix='.recordings-backup-',dir=destination.parent))
    try:
        if copy_state:
            (temporary/'state').mkdir(mode=0o700)
            copy=temporary/'state'/STATE_FILE
            size,digest,mtime=_copy_staged(staged,copy)
            target=copy  # the sealed copy is what is checked, not a file that may change underneath
        else:
            size,digest,mtime=_hash_staged(staged)
            target=staged
        if expected is not None and (size,digest)!=(expected['bytes'],expected['sha256']):
            raise RecordingsRefused('recordings_snapshot_changed')
        facts=inspect_archive(target,None if integrity_seconds is None else time.monotonic()+integrity_seconds)
        _write_manifest(temporary,size,digest,mtime,facts)
        if copy_state:fsync_dir(temporary/'state')
        fsync_dir(temporary);os.rename(temporary,destination);fsync_dir(destination.parent)
        return {'status':'snapshot_created','profile':'recordings','user_version':facts['user_version'],
                'records':facts['archive']['records'],'bytes':size}
    finally:
        if temporary.exists():shutil.rmtree(temporary)  # Only this function's temporary directory.


def _manifest_valid(manifest):
    """Every field's type and range, independent of the archive: a manifest is never trusted for its shape."""
    def count(value,low=0):return type(value) is int and low<=value<=MAX_INT
    def moment(value):
        try:return isinstance(value,str) and datetime.fromisoformat(value) is not None
        except ValueError:return False
    archive=manifest.get('archive')
    return (set(manifest)==MANIFEST_KEYS and manifest['profile']=='recordings' and type(manifest['format']) is int and manifest['format']==1 and
            moment(manifest['created_at']) and moment(manifest['captured_at']) and count(manifest['user_version']) and
            count(manifest['bytes'],1) and isinstance(manifest['sha256'],str) and
            bool(re.fullmatch('[a-f0-9]{64}',manifest['sha256'])) and isinstance(archive,dict) and
            set(archive)==ARCHIVE_KEYS and isinstance(archive['archive_id'],str) and
            bool(ARCHIVE_ID.fullmatch(archive['archive_id'])) and count(archive['incarnation'],1) and
            count(archive['sequence']) and count(archive['records']))


def verify_recordings_bundle(directory,*,state=None):
    """Structure, hashes and identity of a recordings bundle; the protected-point decision is ANYM-728's.

    `state` (the backup worker only): the archive is read in place and `directory` holds `manifest.json` alone.
    """
    from .recovery_state import read_manifest
    directory=Path(directory);manifest=read_manifest(directory)
    if not isinstance(manifest,dict) or not _manifest_valid(manifest):raise ValueError('invalid_recordings_bundle')
    if manifest['user_version'] not in SUPPORTED_USER_VERSIONS:raise ValueError('recordings_schema_unsupported')
    entries={p.name for p in directory.iterdir()}
    if state is not None:
        if entries!={'manifest.json'}:raise ValueError('invalid_recordings_bundle')
        path=Path(state)
    else:
        if entries!={'manifest.json','state'}:raise ValueError('invalid_recordings_bundle')
        folder=directory/'state'
        if folder.is_symlink() or not folder.is_dir() or {p.name for p in folder.iterdir()}!={STATE_FILE}:
            raise ValueError('invalid_recordings_bundle')
        path=folder/STATE_FILE
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):raise ValueError('invalid_recordings_bundle')
            size=os.fstat(source.fileno()).st_size
            digest=hashlib.file_digest(source,'sha256').hexdigest()
    except OSError:raise ValueError('invalid_recordings_bundle') from None
    if size!=manifest['bytes'] or digest!=manifest['sha256']:raise RuntimeError('archive_corrupt')
    try:facts=inspect_archive(path)
    except RecordingsRefused as exc:raise RuntimeError('archive_corrupt') from exc
    if facts['user_version']!=manifest['user_version'] or facts['archive']!=manifest['archive']:
        raise RuntimeError('archive_corrupt')
    return manifest
