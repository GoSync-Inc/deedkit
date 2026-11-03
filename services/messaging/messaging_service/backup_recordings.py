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
# The only codes a refusal may carry (runner SAFE_ERRORS and status); they name no path and no content.
REFUSAL_CODES=('recordings_snapshot_absent','recordings_snapshot_empty','recordings_snapshot_unsafe',
               'recordings_snapshot_corrupt','recordings_snapshot_foreign','recordings_schema_unsupported',
               'recordings_identity_missing')


class RecordingsRefused(RuntimeError):
    """A snapshot that must not become a bundle; no bundle exists and the other profiles are untouched."""
    def __init__(self,code):
        if code not in REFUSAL_CODES:raise ValueError('invalid_refusal_code')
        super().__init__(code);self.code=code


def valid_recordings_member(name):
    return name in MEMBERS


def _identity(db):
    """`meta` is key/value rows (ANYM-724): archive_id, incarnation (>=1). Anything else is no identity."""
    try:rows=dict(db.execute("SELECT key,value FROM meta WHERE key IN ('archive_id','incarnation')").fetchall())
    except sqlite3.OperationalError:raise RecordingsRefused('recordings_identity_missing') from None
    archive_id,incarnation=rows.get('archive_id'),rows.get('incarnation')
    if isinstance(incarnation,str) and re.fullmatch(r'[0-9]{1,15}',incarnation):incarnation=int(incarnation)
    if (not isinstance(archive_id,str) or not ARCHIVE_ID.fullmatch(archive_id) or type(incarnation) is not int or
        incarnation<1):raise RecordingsRefused('recordings_identity_missing')
    return archive_id,incarnation


def inspect_archive(path):
    """Facts of a snapshot file, or RecordingsRefused. Opened read-only and immutable: the file is never written."""
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
        try:
            if db.execute('PRAGMA application_id').fetchone()[0]!=APPLICATION_ID:
                raise RecordingsRefused('recordings_snapshot_foreign')
            user_version=db.execute('PRAGMA user_version').fetchone()[0]
            if user_version not in SUPPORTED_USER_VERSIONS:raise RecordingsRefused('recordings_schema_unsupported')
            if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise RecordingsRefused('recordings_snapshot_corrupt')
            archive_id,incarnation=_identity(db)
            sequence=db.execute('SELECT COALESCE(MAX(sequence),0) FROM events').fetchone()[0]
            records=db.execute('SELECT COUNT(*) FROM records').fetchone()[0]
        finally:db.close()
    except sqlite3.Error:raise RecordingsRefused('recordings_snapshot_corrupt') from None
    except OSError:raise RecordingsRefused('recordings_snapshot_unsafe') from None
    return {'user_version':user_version,
            'archive':{'archive_id':archive_id,'incarnation':incarnation,'sequence':sequence,'records':records}}


def _copy_staged(source,target):
    """Copy through one descriptor (no symlink followed), hashing the bytes written; returns (bytes, sha256, mtime)."""
    try:fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except FileNotFoundError:raise RecordingsRefused('recordings_snapshot_absent') from None
    except OSError:raise RecordingsRefused('recordings_snapshot_unsafe') from None
    with os.fdopen(fd,'rb') as src:
        info=os.fstat(src.fileno())
        if not stat.S_ISREG(info.st_mode):raise RecordingsRefused('recordings_snapshot_unsafe')
        sha=hashlib.sha256();size=0
        out=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(out,'wb') as dst:
            while chunk:=src.read(1024**2):sha.update(chunk);dst.write(chunk);size+=len(chunk)
            dst.flush();os.fsync(dst.fileno())
    return size,sha.hexdigest(),info.st_mtime


def create_recordings_snapshot(staged,destination):
    """Seal `staged` (an SQLite snapshot file) into a bundle directory at `destination`; refuse a bad one."""
    destination=Path(destination)
    if destination.exists():raise ValueError('backup_destination_exists')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=Path(tempfile.mkdtemp(prefix='.recordings-backup-',dir=destination.parent))
    try:
        (temporary/'state').mkdir(mode=0o700)
        copy=temporary/'state'/STATE_FILE
        size,digest,mtime=_copy_staged(staged,copy)
        facts=inspect_archive(copy)  # the sealed copy is what is checked, not a file that may change underneath
        manifest={'profile':'recordings','format':1,'created_at':datetime.now(timezone.utc).isoformat(),
                  'captured_at':datetime.fromtimestamp(mtime,timezone.utc).isoformat(),
                  'user_version':facts['user_version'],'bytes':size,'sha256':digest,'archive':facts['archive']}
        with (temporary/'manifest.json').open('x') as out:
            out.write(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n');out.flush();os.fsync(out.fileno())
        fsync_dir(temporary/'state');fsync_dir(temporary);os.rename(temporary,destination);fsync_dir(destination.parent)
        return {'status':'snapshot_created','profile':'recordings','user_version':facts['user_version'],
                'records':facts['archive']['records']}
    finally:
        if temporary.exists():shutil.rmtree(temporary)  # Only this function's temporary directory.


def verify_recordings_bundle(directory):
    """Structure, hashes and identity of a recordings bundle; the protected-point decision is ANYM-728's."""
    from .recovery_state import read_manifest
    directory=Path(directory);manifest=read_manifest(directory)
    if (not isinstance(manifest,dict) or set(manifest)!=MANIFEST_KEYS or manifest['profile']!='recordings' or
        manifest['format']!=1 or type(manifest['user_version']) is not int or type(manifest['bytes']) is not int or
        not isinstance(manifest['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',manifest['sha256']) or
        not isinstance(manifest['archive'],dict) or set(manifest['archive'])!=ARCHIVE_KEYS):
        raise ValueError('invalid_recordings_bundle')
    if manifest['user_version'] not in SUPPORTED_USER_VERSIONS:raise ValueError('recordings_schema_unsupported')
    entries={p.name for p in directory.iterdir()}
    if entries!={'manifest.json','state'}:raise ValueError('invalid_recordings_bundle')
    state=directory/'state'
    if state.is_symlink() or not state.is_dir() or {p.name for p in state.iterdir()}!={STATE_FILE}:
        raise ValueError('invalid_recordings_bundle')
    path=state/STATE_FILE
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
