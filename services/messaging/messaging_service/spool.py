"""Bounded fsync-backed inbox. Failed DB commits leave the original envelope queued."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import time

from .envelope import canonical, validate


def fsync_dir(path):
    fd=os.open(path,os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


class PoisonItem(ValueError):
    """A durably queued item that can never be applied; quarantine it instead of retrying."""


class InvalidCapture(ValueError):
    """The validator deterministically rejected an item; nothing was written.

    Its message is a content-free reason code. Capacity and I/O failures are
    never raised as this class, so callers can skip one bad item and still fail
    closed on everything else.
    """


def reason_code(exc):
    # Codes only: exception text from parsers could carry source content.
    code=str(exc)
    return code if re.fullmatch(r'[a-z][a-z0-9_]{0,63}',code) else 'invalid_capture'


class DurableSpool:
    def __init__(self,path,*,max_bytes=256*1024**2,max_entries=10000,reserve_bytes=1024**3,validator=validate):
        self.path=Path(path); self.path.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.path.is_symlink(): raise ValueError('spool_symlink')
        info=self.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode)&0o077:
            raise ValueError('private_spool_required')
        self.max_bytes,self.max_entries,self.reserve_bytes=max_bytes,max_entries,reserve_bytes
        self.validator=validator
        self.quarantine_path=self.path/'quarantine'
        self.max_quarantined=1000

    @contextmanager
    def locked(self,name='queue.lock'):
        fd=os.open(self.path/name,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX); yield
        finally: os.close(fd)

    def put(self,data):
        try:payload=canonical(self.validator(data))
        except (ValueError,TypeError,KeyError,RecursionError) as exc:raise InvalidCapture(reason_code(exc)) from None
        digest=hashlib.sha256(payload).hexdigest()
        with self.locked():
            # One directory pass: dedupe, quota and abandoned temp files.
            pending=occupied=0
            with os.scandir(self.path) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file(follow_symlinks=False):continue
                        if entry.name.endswith('.json'):
                            if entry.name.endswith('-'+digest+'.json'):return entry.name
                            pending+=1
                        # Include abandoned temp files in quota; never silently discard corrupt/unknown data.
                        occupied+=entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:continue  # Committed and removed by a concurrent drain.
            if pending>=self.max_entries or occupied+len(payload)>self.max_bytes:
                raise RuntimeError('spool_full')
            if shutil.disk_usage(self.path).free-len(payload)<self.reserve_bytes:
                raise RuntimeError('disk_reserve')
            target=self.path/f'{time.time_ns():020d}-{digest}.json'
            temporary=self.path/('.pending-'+secrets.token_hex(12))
            try:
                fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                with os.fdopen(fd,'wb') as out:
                    out.write(payload); out.flush(); os.fsync(out.fileno())
                os.replace(temporary,target); fsync_dir(self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return target.name

    def discard(self,name):
        """Remove an item whose exact content the caller already committed itself."""
        if not re.fullmatch(r'[0-9]{20}-[0-9a-f]{64}\.json',name):raise ValueError('invalid_spool_name')
        (self.path/name).unlink(missing_ok=True); fsync_dir(self.path)

    def quarantined(self):
        try:
            with os.scandir(self.quarantine_path) as entries:
                return sum(1 for entry in entries if entry.name.endswith('.json') and entry.is_file(follow_symlinks=False))
        except FileNotFoundError:return 0

    def quarantine(self,path,code):
        """Move a permanently invalid item aside so later items keep draining.

        The exact bytes and a content-free reason stay on disk for the operator;
        nothing is deleted. A full quarantine stops draining as before.
        """
        folder=self.quarantine_path
        try:folder.mkdir(mode=0o700)
        except FileExistsError:pass
        info=folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode)&0o077:
            raise RuntimeError('spool_corrupt')
        if self.quarantined()>=self.max_quarantined:raise RuntimeError('spool_corrupt')
        if not path.exists():return  # Its writer committed and removed it meanwhile.
        reason=folder/(path.name+'.reason')
        temporary=folder/('.pending-'+secrets.token_hex(12))
        try:
            fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'w') as out:
                out.write(code+'\n'); out.flush(); os.fsync(out.fileno())
            os.replace(temporary,reason)
        finally:
            temporary.unlink(missing_ok=True)
        os.replace(path,folder/path.name); fsync_dir(folder); fsync_dir(self.path)

    def drain(self,ingest,limit=100):
        count=0
        # A stalled database must not block durable capture. Serialize drainers
        # independently, holding the publication lock only for filesystem work.
        with self.locked('drain.lock'):
            for path in sorted(self.path.glob('*.json'))[:limit]:
                if path.is_symlink(): raise RuntimeError('spool_corrupt')
                try:payload=path.read_bytes()
                except FileNotFoundError:continue  # Already committed by its writer.
                if hashlib.sha256(payload).hexdigest()!=path.stem.split('-')[-1]:
                    raise RuntimeError('spool_corrupt')  # Damaged storage stays fail-closed.
                try:item=self.validator(json.loads(payload))
                except (ValueError,TypeError,KeyError,RecursionError) as exc:
                    # Deterministic validation: retrying can never succeed and
                    # would block every later capture for this account.
                    self.quarantine(path,reason_code(exc)); continue
                try:ingest(item)  # Return only after DB commit.
                except PoisonItem as exc:
                    self.quarantine(path,reason_code(exc)); continue
                # Writers never reuse a committed name; removal needs no queue lock.
                path.unlink(missing_ok=True); fsync_dir(self.path); count+=1
        return count
