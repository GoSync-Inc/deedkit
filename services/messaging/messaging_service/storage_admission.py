"""Conservative media capacity leases, independent of durable message capture.

All collectors sharing an archive DB serialize admission. A filesystem device
key groups bind mounts/path aliases. Reserved bytes include the entire payload
even after some bytes have been written: this deliberately underestimates spare
capacity rather than promising an exact post-transfer free-space value.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil


STREAM_WORKSPACE_BYTES = 1024**2
MEDIA_RESERVE_BYTES = 10*1024**3
# restore_error of a cold restore that holds a capacity lease until restore_finished_at.
COLD_RESTORING = 'cold_restoring'


def volume_key(root):
    return 'fs:'+str(os.stat(Path(root),follow_symlinks=False).st_dev)


def payload_bound(expected_bytes, max_bytes):
    if expected_bytes is None:
        return max_bytes
    if type(expected_bytes) is not int or not 0<=expected_bytes<=max_bytes:
        raise ValueError('media_size_limit')
    return expected_bytes


def cold_grant(c):
    """This role may read cold-copy restore leases (ANYM-350)."""
    return c.execute("""SELECT CASE WHEN to_regclass('public.attachment_cold_copies') IS NULL THEN false
        ELSE has_table_privilege('public.attachment_cold_copies','SELECT') END AS ok""").fetchone()['ok']


def restore_reserved(c, *, excluding=None):
    """Bytes leased by cold restores still streaming (ANYM-350); 0 without the grant.

    A role without the grant keeps its old accounting; a separate query because
    PostgreSQL checks table privileges even in an unreached CASE branch.
    """
    if not cold_grant(c):
        return 0
    return c.execute('''SELECT coalesce(sum(bytes+%s),0) AS bytes FROM attachment_cold_copies
        WHERE restore_state='requested' AND restore_error=%s AND restore_finished_at>clock_timestamp()
          AND digest IS DISTINCT FROM %s''',(STREAM_WORKSPACE_BYTES,COLD_RESTORING,excluding)).fetchone()['bytes']


def has_capacity(free_bytes, reserved_bytes, requested_bytes, reserve_bytes):
    return free_bytes-reserved_bytes-requested_bytes>=reserve_bytes


@dataclass
class MediaReservation:
    """Local streaming guard for a DB reservation, renewed with the media lease."""
    payload_bytes: int
    expires_at: datetime

    def check(self, count):
        if datetime.now(timezone.utc)>=self.expires_at:
            raise RuntimeError('media_lease_lost')
        if count>self.payload_bytes:
            raise ValueError('media_size_limit')


class StorageAdmission:
    def __init__(self, writer):
        self.writer=writer
        self.volume=volume_key(writer.root)

    @staticmethod
    def lock(c):
        # Capacity admission precedes collector/job fences. Staged command
        # sources use an independent per-upload lifecycle lock, never this lock.
        c.execute("SELECT pg_advisory_xact_lock(hashtextextended('media-storage-admission',0))")

    @staticmethod
    def lock_and_reap(c):
        StorageAdmission.lock(c)
        # Supersede/delete invalidates the job lease. A crashed writer's expired
        # reservation is reclaimed on the next admission, without a scheduler.
        c.execute('''DELETE FROM media_reservations r WHERE r.expires_at<=clock_timestamp()
            OR NOT EXISTS(SELECT 1 FROM media_jobs j WHERE j.id=r.job AND j.state='leased'
                AND j.lease_epoch=r.lease_epoch AND j.lease_until>clock_timestamp())''')

    def reserve(self, c, job, expires_at):
        payload=payload_bound(job['descriptor']['bytes'],self.writer.max_bytes)
        requested=payload+STREAM_WORKSPACE_BYTES
        reserved=c.execute('''SELECT coalesce(sum(reserved_bytes),0) AS bytes
            FROM media_reservations WHERE volume_key=%s''',(self.volume,)).fetchone()['bytes']
        reserved+=c.execute("""SELECT coalesce(sum(2*bytes+%s),0) AS bytes FROM file_uploads
            WHERE volume_key=%s AND state<>'expired'""",(STREAM_WORKSPACE_BYTES,self.volume)).fetchone()['bytes']
        reserved+=restore_reserved(c)
        free=shutil.disk_usage(self.writer.root).free
        if not has_capacity(free,reserved,requested,self.writer.reserve_bytes):
            return None
        c.execute('''INSERT INTO media_reservations(job,volume_key,lease_epoch,reserved_bytes,expires_at)
            VALUES(%s,%s,%s,%s,%s)''',
            (job['id'],self.volume,job['lease_epoch']+1,requested,expires_at))
        return MediaReservation(payload,expires_at)

    @staticmethod
    def release(c, job):
        # An old worker must never release the replacement worker's capacity.
        c.execute('DELETE FROM media_reservations WHERE job=%s AND lease_epoch=%s',
            (job['id'],job['lease_epoch']))
