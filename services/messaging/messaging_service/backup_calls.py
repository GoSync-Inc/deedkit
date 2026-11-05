"""Calls state bundle (ANYM-778): one sanitized SQLite snapshot sealed under a `calls` profile manifest.

The snapshot is made elsewhere (backup_runner's capped helper container: online backup, integrity check, `VACUUM INTO`).
This module refuses a file that must not become a bundle and records its per-table facts. No calls code, no provider call.
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

from .backup_calendar import rows_digest
from .spool import fsync_dir

SCHEMA_VERSION=1  # The bundle format; SQLite's user_version of the database is recorded separately.
STATE_FILE='voice.sqlite3'
MEMBERS=('manifest.json','state/'+STATE_FILE)
SQLITE_HEADER=b'SQLite format 3\x00'
REQUIRED_TABLES={'briefs','calls','voice_events'}  # services/calls/core.py: anything else is not the calls database
MANIFEST_KEYS={'profile','schema_version','created_at','captured_at','user_version','tables','voice_events_max_seq','bytes','sha256'}
MAX_INT=2**63-1
# The only codes a refusal may carry (runner SAFE_ERRORS and status); they name no path and no content.
REFUSAL_CODES=('calls_snapshot_absent','calls_snapshot_empty','calls_snapshot_unsafe','calls_snapshot_corrupt',
               'calls_snapshot_foreign','calls_snapshot_changed','calls_integrity_timeout','calls_encrypt_timeout',
               'calls_no_space')


class CallsRefused(RuntimeError):
    """A snapshot that must not become a bundle; no bundle exists and the other profiles are untouched."""
    def __init__(self,code):
        if code not in REFUSAL_CODES:raise ValueError('invalid_refusal_code')
        super().__init__(code);self.code=code


def valid_calls_member(name):
    return name in MEMBERS


def row_text(row):
    """One row as unambiguous ASCII text: every SQLite storage class is tagged, so no two different rows share a form."""
    def cell(value):
        if value is None:return ['n']
        if isinstance(value,int):return ['i',str(value)]
        if isinstance(value,float):return ['f',value.hex()]
        if isinstance(value,bytes):return ['b',value.hex()]
        return ['t',value]
    return json.dumps([cell(value) for value in row],ensure_ascii=True,separators=(',',':'))


def _tables(db):
    names=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")]
    tables={}
    for name in sorted(names):
        quoted='"'+name.replace('"','""')+'"'
        rows,digest=rows_digest(row_text(row) for row in db.execute('SELECT * FROM '+quoted))
        tables[name]={'rows':rows,'sha256':digest}
    return tables


def inspect_snapshot(path,deadline=None):
    """Facts of a snapshot file, or CallsRefused. Read-only and immutable. `deadline` (time.monotonic) bounds the reads."""
    path=Path(path)
    try:info=path.lstat()
    except FileNotFoundError:raise CallsRefused('calls_snapshot_absent') from None
    except OSError:raise CallsRefused('calls_snapshot_unsafe') from None
    if not stat.S_ISREG(info.st_mode):raise CallsRefused('calls_snapshot_unsafe')
    if info.st_size==0:raise CallsRefused('calls_snapshot_empty')
    try:
        with path.open('rb') as f:header=f.read(16)
        if header!=SQLITE_HEADER:raise CallsRefused('calls_snapshot_corrupt')
        db=sqlite3.connect('file:'+quote(str(path))+'?mode=ro&immutable=1',uri=True)
        # Not every stored text is valid UTF-8; it must hash, never fail or print.
        db.text_factory=lambda raw:raw.decode(errors='surrogateescape')
        if deadline is not None:db.set_progress_handler(lambda:1 if time.monotonic()>deadline else 0,1000)
        try:
            if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise CallsRefused('calls_snapshot_corrupt')
            user_version=db.execute('PRAGMA user_version').fetchone()[0]
            tables=_tables(db)
            if not REQUIRED_TABLES<=set(tables):raise CallsRefused('calls_snapshot_foreign')
            sequence,kind=db.execute('SELECT MAX(seq),typeof(MAX(seq)) FROM voice_events').fetchone()
            if kind not in ('integer','null'):raise CallsRefused('calls_snapshot_corrupt')
        finally:db.close()
    except sqlite3.Error:
        if deadline is not None and time.monotonic()>deadline:raise CallsRefused('calls_integrity_timeout') from None
        raise CallsRefused('calls_snapshot_corrupt') from None
    except OSError:raise CallsRefused('calls_snapshot_unsafe') from None
    return {'user_version':user_version,'tables':tables,'voice_events_max_seq':sequence or 0}


def _digest(path):
    """Size and SHA-256 through one descriptor (no symlink followed, regular files only)."""
    try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except FileNotFoundError:raise CallsRefused('calls_snapshot_absent') from None
    except OSError:raise CallsRefused('calls_snapshot_unsafe') from None
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):raise CallsRefused('calls_snapshot_unsafe')
    except BaseException:
        os.close(fd);raise
    with os.fdopen(fd,'rb') as source:return info.st_size,hashlib.file_digest(source,'sha256').hexdigest()


def create_calls_snapshot(staged,destination,*,captured_at,copy_state=True,expected=None,integrity_seconds=None):
    """Check `staged` and write the bundle directory at `destination`; refuse a bad file.

    `copy_state=False` (the backup worker) writes only `manifest.json`: the file stays in its private staging and is
    streamed into the encryption, so no second plaintext copy exists. `captured_at` is when the copy began, the runner's
    clock: the snapshot's content is never older. `expected` is the runner's `{bytes, sha256}` of the staged file.
    """
    destination=Path(destination);staged=Path(staged)
    if destination.exists():raise ValueError('backup_destination_exists')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=Path(tempfile.mkdtemp(prefix='.calls-backup-',dir=destination.parent))
    try:
        size,digest=_digest(staged)
        if expected is not None and (size,digest)!=(expected['bytes'],expected['sha256']):
            raise CallsRefused('calls_snapshot_changed')
        facts=inspect_snapshot(staged,None if integrity_seconds is None else time.monotonic()+integrity_seconds)
        if copy_state:
            (temporary/'state').mkdir(mode=0o700)
            shutil.copyfile(staged,temporary/'state'/STATE_FILE,follow_symlinks=False)
            fsync_dir(temporary/'state')
        manifest={'profile':'calls','schema_version':SCHEMA_VERSION,'created_at':datetime.now(timezone.utc).isoformat(),
                  'captured_at':captured_at,**facts,'bytes':size,'sha256':digest}
        with (temporary/'manifest.json').open('x') as out:
            out.write(json.dumps(manifest,indent=2)+'\n');out.flush();os.fsync(out.fileno())
        fsync_dir(temporary);os.rename(temporary,destination);fsync_dir(destination.parent)
        return {'status':'snapshot_created','profile':'calls','tables':len(facts['tables']),'bytes':size}
    finally:
        if temporary.exists():shutil.rmtree(temporary)


def _manifest_valid(manifest):
    """Every field's type and range, independent of the file: a manifest is never trusted for its shape."""
    def count(value,low=0):return type(value) is int and low<=value<=MAX_INT
    def digest(value):return isinstance(value,str) and bool(re.fullmatch('[a-f0-9]{64}',value))
    def moment(value):
        try:return isinstance(value,str) and datetime.fromisoformat(value).tzinfo is not None
        except ValueError:return False
    tables=manifest.get('tables')
    return (set(manifest)==MANIFEST_KEYS and manifest['profile']=='calls' and type(manifest['schema_version']) is int and
            manifest['schema_version']==SCHEMA_VERSION and moment(manifest['created_at']) and moment(manifest['captured_at']) and
            count(manifest['user_version']) and count(manifest['voice_events_max_seq']) and count(manifest['bytes'],1) and
            digest(manifest['sha256']) and isinstance(tables,dict) and
            all(isinstance(value,dict) and set(value)=={'rows','sha256'} and count(value['rows']) and digest(value['sha256'])
                for value in tables.values()))


def verify_calls_bundle(directory,*,state=None):
    """Structure, hash and every table of a calls bundle. `state` (the worker only): `directory` holds the manifest alone."""
    from .recovery_state import read_manifest
    directory=Path(directory);manifest=read_manifest(directory)
    if not isinstance(manifest,dict) or not _manifest_valid(manifest):raise ValueError('invalid_calls_bundle')
    entries={p.name for p in directory.iterdir()}
    if state is not None:
        if entries!={'manifest.json'}:raise ValueError('invalid_calls_bundle')
        path=Path(state)
    else:
        folder=directory/'state'
        if entries!={'manifest.json','state'} or folder.is_symlink() or not folder.is_dir() or {p.name for p in folder.iterdir()}!={STATE_FILE}:
            raise ValueError('invalid_calls_bundle')
        path=folder/STATE_FILE
    try:size,digest=_digest(path)
    except CallsRefused:raise ValueError('invalid_calls_bundle') from None
    if size!=manifest['bytes'] or digest!=manifest['sha256']:raise RuntimeError('calls_state_corrupt')
    try:facts=inspect_snapshot(path)
    except CallsRefused as exc:raise RuntimeError('calls_state_corrupt') from exc
    if any(facts[key]!=manifest[key] for key in facts):raise RuntimeError('calls_state_corrupt')
    return manifest
