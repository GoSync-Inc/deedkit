"""One-host heavy-job admission, with durable DB and inherited OS fencing.

All local processing containers must mount the same precreated lock directory.
TTL is recovery metadata, never proof that an orphan process has stopped. Only
closing every inherited descriptor releases the OS lock; never LOCK_UN it.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import re


def local_heavy(profile):
    return profile['provider'] in ('faster_whisper', 'ollama')


def inherited_fds():
    value = os.environ.get('PROCESSING_RESOURCE_FD')
    return (int(value),) if value is not None else ()


def extraction_heavy(files, row):
    from .file_extract import extraction_profile
    if extraction_profile(row['source_mime'], row['source_filename'])=='magic':
        return True
    # Native detection precedes MIME/suffix parsing. A PDF named note.txt must
    # also acquire the slot. This header read chooses admission, not validity.
    digest=row['source_digest']
    if not re.fullmatch('[0-9a-f]{64}',digest):return True
    try:
        fd=os.open(Path(files)/digest,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):return True
            header=os.read(fd,16)
        finally:os.close(fd)
    except OSError:return True
    return header.startswith((b'%PDF-',b'\x89PNG\r\n\x1a\n',b'\xff\xd8'))


def lock_file(directory):
    """Return a held descriptor, None if busy; bad mounts fail closed."""
    directory = Path(directory)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o002:
        raise ValueError('processing_lock_directory_required')
    fd = os.open(directory/'local-heavy.lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError('private_processing_lock_required')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd
    except BaseException:
        os.close(fd)
        raise


@dataclass
class ResourceLease:
    fd: int
    token: object

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


@contextmanager
def transaction(connection):
    """Close claim descriptors if SQL or the final transaction commit fails."""
    leases=[]
    try:
        with connection as c:
            yield c,leases
    except BaseException:
        for lease in leases:lease.close()
        raise


def acquire(c, directory, worker_kind, attachment, token, ttl):
    # Same global transaction lock also serializes cross-worker budget checks.
    c.execute('SELECT pg_advisory_xact_lock(74509139)')
    fd = lock_file(directory)
    if fd is None:
        return None
    try:
        row = c.execute('''INSERT INTO processing_resource_leases
            (resource,lease_token,worker_kind,attachment,lease_until)
            VALUES('local-heavy',%s,%s,%s,clock_timestamp()+make_interval(secs=>%s))
            ON CONFLICT(resource) DO UPDATE SET lease_token=excluded.lease_token,
            worker_kind=excluded.worker_kind,attachment=excluded.attachment,
            lease_until=excluded.lease_until,updated_at=clock_timestamp()
            WHERE processing_resource_leases.lease_until<=clock_timestamp()
            RETURNING resource''', (token,worker_kind,attachment,ttl)).fetchone()
        if row is None:
            os.close(fd)
            return None
        return ResourceLease(fd, token)
    except BaseException:
        os.close(fd)
        raise


def release(c, lease):
    if lease is not None:
        c.execute('''UPDATE processing_resource_leases SET lease_until=clock_timestamp(),
            updated_at=clock_timestamp() WHERE resource='local-heavy' AND lease_token=%s''', (lease.token,))


def pause_profile(c, account, kind, profile_hash, error):
    indefinite = {'model_auth_failed', 'codex_private_auth_required',
                  'codex_chatgpt_login_required', 'codex_login_check_failed',
                  'codex_upgrade_required', 'local_model_not_installed',
                  'invalid_model_key', 'codex_cli_unavailable'}
    if error not in indefinite | {'model_rate_limited'}:
        return
    # Rate limits have a bounded cooldown; invalid auth/runtime needs an operator.
    c.execute('''INSERT INTO processing_control(account,kind,profile_hash,pause_reason,paused_until)
        VALUES(%s,%s,%s,%s,CASE WHEN %s THEN clock_timestamp()+interval '30 minutes' ELSE NULL END)
        ON CONFLICT(account,kind,profile_hash) DO UPDATE SET pause_reason=excluded.pause_reason,
        paused_until=excluded.paused_until,updated_at=clock_timestamp()''',
        (account,kind,profile_hash,error,error=='model_rate_limited'))


def defer_profile(c, account, kind, profile_hash, seconds):
    """Pause a scope for `seconds` because the model host cannot take work now (`model_backpressure`).

    A merge that never weakens a pause already there: an indefinite one (`paused_until` NULL, such as
    `model_auth_failed`) stays, and of two bounded ones the later expiry wins with its own reason, so a late
    deferral cannot replace what another job's failure just set (the plain upsert of pause_profile would)."""
    c.execute('''INSERT INTO processing_control(account,kind,profile_hash,pause_reason,paused_until)
        VALUES(%s,%s,%s,'model_backpressure',clock_timestamp()+make_interval(secs=>%s))
        ON CONFLICT(account,kind,profile_hash) DO UPDATE SET
        pause_reason=CASE WHEN processing_control.pause_reason IS NOT NULL
                AND (processing_control.paused_until IS NULL OR processing_control.paused_until>=excluded.paused_until)
            THEN processing_control.pause_reason ELSE excluded.pause_reason END,
        paused_until=CASE WHEN processing_control.pause_reason IS NOT NULL AND processing_control.paused_until IS NULL THEN NULL
            WHEN processing_control.pause_reason IS NOT NULL AND processing_control.paused_until>clock_timestamp()
            THEN greatest(processing_control.paused_until,excluded.paused_until)
            ELSE excluded.paused_until END,
        updated_at=clock_timestamp()''',(account,kind,profile_hash,seconds))
