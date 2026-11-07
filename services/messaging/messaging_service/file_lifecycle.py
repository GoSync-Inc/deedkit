"""Operator-only bounded original residency; derived content is never removed.

All state transitions serialize on one DB lock. Backup fences are durable and
have no automatic expiry. No provider connection or filesystem traversal lives
in this module. Production activation remains an explicit operator action.
"""
import hashlib
import math
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

LOCK_NAME='attachment-residency-v1'
RETENTION_DAYS=30
# ANYM-806 (owner 2026-11-06): an original whose every current occurrence is in an on-request chat (a group or
# channel no active subscription names, media_policy.ON_REQUEST_CHATS) leaves the disk 24 hours after its last read
# once its R2 copy is verified; a recognition an agent asked for holds it while it is pending, for at most 7 days.
CHAT_POLICY_HOURS=24
RECOGNITION_WAIT_DAYS=7

# Used only while holding lifecycle_lock, with the current attachment aliased a.
# A durable managed transition pauses original-dependent work without spending
# processing retries. Unknown missing/corrupt originals retain normal diagnostics.
PROCESSABLE_ORIGINAL='''NOT EXISTS(SELECT FROM attachment_residency residency
    JOIN attachment_eviction_journal eviction ON eviction.id=residency.eviction_id
        AND eviction.digest=residency.digest AND eviction.bytes=residency.bytes
    WHERE residency.digest=a.digest AND residency.bytes=a.bytes
        AND ((residency.state='evicting' AND eviction.state='prepared')
            OR (residency.state='evicted' AND eviction.state='completed')))'''


def lifecycle_lock(c):
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(LOCK_NAME,))


def _digest(value):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value):
        raise ValueError('invalid_blob')
    return value


def touch_original(c,digest,bytes=None):
    _digest(digest)
    if bytes is not None and (type(bytes) is not int or bytes<0):raise ValueError('invalid_blob')
    return c.execute('SELECT public.touch_original(%s,%s) AS changed',(digest,bytes)).fetchone()['changed']


def begin_backup_fence(c,token):
    if not isinstance(token,str) or not re.fullmatch('[a-zA-Z0-9_.:-]{1,160}',token):
        raise ValueError('invalid_backup_fence')
    # Convert controlled lifecycle errors without concealing DB/connection errors.
    import psycopg
    try:
        with c.transaction():c.execute('SELECT public.begin_backup_fence(%s)',(token,))
    except psycopg.errors.RaiseException as error:
        if error.diag.message_primary in ('backup_fence_closed','backup_eviction_pending'):
            raise ValueError(error.diag.message_primary) from error
        raise


def end_backup_fence(c,token):
    c.execute('SELECT public.end_backup_fence(%s)',(token,))


def backup_fence_active(c):
    return c.execute('SELECT EXISTS(SELECT FROM attachment_backup_fences WHERE ended_at IS NULL) AS active').fetchone()['active']


def _safe_coverage(row):
    coverage=row.get('coverage') or {}
    if row['kind']=='video':
        count=coverage.get('sampled_frames');times=coverage.get('sampled_timestamps_seconds')
        duration=coverage.get('duration_seconds')
        return (coverage.get('mode')=='sampled_frames' and coverage.get('complete') is False
            and coverage.get('audio_transcribed') is False and type(count) is int and 1<=count<=8
            and type(duration) in (int,float) and math.isfinite(duration) and duration>0
            and isinstance(times,list) and len(times)==count
            and all(type(t) in (int,float) and math.isfinite(t) and 0<=t<duration for t in times))
    mode={'audio':'transcript_summary','document':'extracted_text_summary','image':'image_description'}
    return coverage.get('mode')==mode.get(row['kind']) and coverage.get('complete') is True


class OriginalMissing(FileNotFoundError):
    def __init__(self,root_identity):
        super().__init__('original_missing');self.root_identity=root_identity


def _root_identity(info):
    return (info.st_dev,info.st_ino)


def _identity(info):
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def cold_copy_verified(c,digest,bytes):
    """Read-back proof of the encrypted R2 copy (ANYM-297); caller holds lifecycle_lock."""
    return c.execute("""SELECT EXISTS(SELECT FROM attachment_cold_copies WHERE digest=%s AND bytes=%s
        AND state='verified' AND ciphertext_sha256 IS NOT NULL AND verified_at IS NOT NULL) AS verified""",
        (_digest(digest),bytes)).fetchone()['verified']


class LifecycleWorker:
    """One bounded pass, dry-run unless apply=True; never scans directories.

    Production entry points pass require_cold_copy=True: no original is
    unlinked without a verified cold copy. The default exists for fixtures.
    """
    def __init__(self,store,*,expire_unprocessed=False,require_cold_copy=False,chat_policy_only=False):
        if type(expire_unprocessed) is not bool:raise ValueError('invalid_retention_policy')
        if type(require_cold_copy) is not bool:raise ValueError('invalid_retention_policy')
        if type(chat_policy_only) is not bool:raise ValueError('invalid_retention_policy')
        self.store=store;self.expire_unprocessed=expire_unprocessed;self.require_cold_copy=require_cold_copy
        self.chat_policy_only=chat_policy_only

    @staticmethod
    def _chat_policy(c,digest):
        """Every current occurrence of the original is in an on-request chat (ANYM-806); there is at least one."""
        from .media_policy import ON_REQUEST_CHAT
        row=c.execute('''SELECT count(*) AS occurrences,count(*) FILTER (WHERE NOT '''+ON_REQUEST_CHAT+''') AS kept
            FROM attachments a JOIN messages m ON m.id=a.message WHERE a.digest=%s AND a.active AND NOT m.deleted''',
            (digest,)).fetchone()
        return row['occurrences']>0 and row['kept']==0

    @staticmethod
    def _recognition_pending(c,digest):
        """An occurrence an agent asked to recognize (ANYM-805: its job's request key digest) still waits for it.

        Pending: enrichment unconfigured, queued, leased or failed with retries left, or text queued or leased, that
        changed in the last RECOGNITION_WAIT_DAYS; older pending work no longer holds the original (it can be asked
        for again).
        """
        return c.execute('''SELECT EXISTS(SELECT FROM attachments a JOIN messages m ON m.id=a.message
            JOIN media_jobs j ON j.id=a.id AND j.request_key_digest IS NOT NULL
            WHERE a.digest=%s AND a.active AND NOT m.deleted AND (
                EXISTS(SELECT FROM attachment_enrichment e WHERE e.attachment=a.id AND e.source_digest=a.digest
                    AND e.source_revision=m.revision AND e.updated_at>clock_timestamp()-%s*interval '1 day'
                    AND (e.state IN ('unconfigured','queued','leased') OR (e.state='failed' AND e.attempts<3)))
                OR EXISTS(SELECT FROM attachment_text f WHERE f.attachment=a.id AND f.source_digest=a.digest
                    AND f.updated_at>clock_timestamp()-%s*interval '1 day' AND f.state IN ('queued','leased')))) AS pending''',
            (digest,RECOGNITION_WAIT_DAYS,RECOGNITION_WAIT_DAYS)).fetchone()['pending']

    def _lock_sources(self,c,digest):
        return c.execute('SELECT public.lock_lifecycle_sources(%s) AS locked',(digest,)).fetchone()['locked']

    def _eligible(self,c,row):
        if backup_fence_active(c):return 'backup_active'
        if row['pinned']:return 'pinned'
        # Rechecked under the lifecycle lock before unlink; subscription create and resume take the same lock.
        chat=self._chat_policy(c,row['digest'])
        if self.chat_policy_only and not chat:return 'chat_policy_changed'
        old=c.execute("SELECT %s::timestamptz<=clock_timestamp()-%s*interval '1 hour' AS old",
            (row['last_accessed_at'],CHAT_POLICY_HOURS if chat else RETENTION_DAYS*24)).fetchone()['old']
        if not old:return 'recently_requested'
        # Rechecked under the lock by _complete, so a copy invalidated after
        # prepare cancels the journal before unlink. The chat policy always needs one.
        if (self.require_cold_copy or chat) and not cold_copy_verified(c,row['digest'],row['bytes']):return 'cold_copy_missing'
        if not self._lock_sources(c,row['digest']):return 'occurrence_limit'
        # The default policy requires usable detail for every occurrence.
        # The explicit unprocessed policy instead requires every source to be old.
        rows=c.execute('''SELECT a.id,a.bytes,a.mime,m.revision,u.*,
            m.sent_at<=clock_timestamp()-interval '30 days' AS source_old,
            t.state AS text_state,t.source_digest AS text_digest,t.source_revision AS text_revision,
            t.source_bytes AS text_bytes,t.text_content AS extracted,t.truncated AS text_truncated,
            t.error_code AS text_error,t.extraction_metadata,
            e.state AS enrichment_state,e.kind AS enrichment_kind,e.source_digest AS enrichment_digest,
            e.source_revision AS enrichment_revision,e.text_content AS transcript,
            e.truncated AS enrichment_truncated,e.error_code AS enrichment_error,e.metadata AS enrichment_metadata
            FROM attachments a JOIN messages m ON m.id=a.message
            LEFT JOIN attachment_understanding u ON u.attachment=a.id
            LEFT JOIN attachment_text t ON t.attachment=a.id
            LEFT JOIN attachment_enrichment e ON e.attachment=a.id
            WHERE a.digest=%s AND a.active AND NOT m.deleted ORDER BY a.id''',(row['digest'],)).fetchall()
        if not rows:return 'no_current_source'
        if c.execute('SELECT 1 AS present FROM attachments WHERE digest=%s AND bytes<>%s LIMIT 1',(row['digest'],row['bytes'])).fetchone():return 'identity_mismatch'
        active=c.execute('''SELECT 1 AS present FROM attachments a LEFT JOIN media_jobs j ON j.id=a.id
            WHERE a.digest=%s AND ((j.state='leased' AND j.lease_until>clock_timestamp())
              OR j.requested_until>clock_timestamp() OR EXISTS(SELECT FROM download_grants g
                WHERE g.attachment=a.id AND g.expires_at>clock_timestamp())
              OR EXISTS(SELECT FROM attachment_text f WHERE f.attachment=a.id AND f.state='leased' AND f.lease_until>clock_timestamp())
              OR EXISTS(SELECT FROM attachment_enrichment e WHERE e.attachment=a.id AND e.state='leased' AND e.lease_until>clock_timestamp())
              OR EXISTS(SELECT FROM attachment_understanding u WHERE u.attachment=a.id AND u.state='leased' AND u.lease_until>clock_timestamp())) LIMIT 1''',(row['digest'],)).fetchone()
        if active:return 'inflight'
        # The chat policy needs no detail: the message, its text and any description stay; the R2 copy restores it.
        if chat:return 'recognition_pending' if self._recognition_pending(c,row['digest']) else None
        # An explicit operator policy may accept losing unprocessed originals.
        # Every shared occurrence must still be old; all other protections apply.
        if self.expire_unprocessed:
            return None if all(item['source_old'] for item in rows) else 'recent_source'
        from .file_understanding import valid_understanding
        for item in rows:
            if (not valid_understanding(c,item) or item['bytes']!=row['bytes'] or item['state']!='ready'
                    or item['indexed_at'] is None or item['published_profile_hash'] is None
                    or item['source_digest']!=row['digest'] or item['source_revision']!=item['revision']
                    or item['truncated'] or item['error_code'] or not item['title'].strip() or not item['summary'].strip()
                    or not _safe_coverage(item)):
                return 'description_incomplete'
            if item['kind']=='document':
                if (item['text_state']!='ready' or item['text_digest']!=row['digest']
                        or item['text_revision']!=item['revision'] or item['text_bytes']!=row['bytes']
                        or not item['extracted'].strip() or item['text_truncated'] or item['text_error']
                        or (item['extraction_metadata'] or {}).get('complete') is False):return 'detail_incomplete'
            elif item['kind']=='audio':
                if (item['enrichment_state']!='ready' or item['enrichment_kind']!='audio'
                        or item['enrichment_digest']!=row['digest'] or item['enrichment_revision']!=item['revision']
                        or not item['transcript'].strip() or item['enrichment_truncated'] or item['enrichment_error']
                        or (item['enrichment_metadata'] or {}).get('source_truncated') is True
                        or (item['enrichment_metadata'] or {}).get('audio_truncated') is True
                        or (item['enrichment_metadata'] or {}).get('complete') is False):return 'detail_incomplete'
        return None

    def _verified(self,digest,size):
        root_fd=os.open(self.store.files,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        fd=None
        try:
            try:fd=os.open(_digest(digest),os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW,dir_fd=root_fd)
            except FileNotFoundError as error:raise OriginalMissing(_root_identity(os.fstat(root_fd))) from error
            before=os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size!=size:raise ValueError('original_integrity')
            with os.fdopen(fd,'rb',closefd=False) as source:
                checksum=hashlib.sha256();remaining=size
                while remaining:
                    chunk=source.read(min(1024**2,remaining))
                    if not chunk:raise ValueError('original_integrity')
                    checksum.update(chunk);remaining-=len(chunk)
                if source.read(1):raise ValueError('original_integrity')
                calculated=checksum.hexdigest()
            after=os.fstat(fd)
            if calculated!=digest or _identity(before)!=_identity(after):raise ValueError('original_integrity')
            return root_fd,fd,_identity(after)
        except BaseException:
            if fd is not None:os.close(fd)
            os.close(root_fd)
            raise

    def _cancel(self,c,row,reason):
        c.execute("UPDATE attachment_eviction_journal SET state='cancelled',completed_at=clock_timestamp(),error_code=%s WHERE id=%s AND state='prepared'",(reason,row['eviction_id']))
        c.execute("UPDATE attachment_residency SET state='resident',eviction_id=NULL,evicted_at=NULL,updated_at=clock_timestamp() WHERE digest=%s",(row['digest'],))

    def _prepare(self,digest):
        with self.store.pool.connection() as c:
            lifecycle_lock(c)
            row=c.execute("SELECT * FROM attachment_residency WHERE digest=%s AND state='resident' FOR UPDATE",(digest,)).fetchone()
            if not row:return None,'state_changed'
            reason=self._eligible(c,row)
            if reason:return None,reason
            token=uuid4()
            root_fd=os.open(self.store.files,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:root_identity=_root_identity(os.fstat(root_fd))
            finally:os.close(root_fd)
            c.execute('INSERT INTO attachment_eviction_journal(id,digest,bytes,root_device,root_inode) VALUES(%s,%s,%s,%s,%s)',(token,digest,row['bytes'],*root_identity))
            c.execute("UPDATE attachment_residency SET state='evicting',eviction_id=%s,updated_at=clock_timestamp() WHERE digest=%s",(token,digest))
            return token,None

    @staticmethod
    def _mark_evicted(c,row):
        c.execute("UPDATE attachment_eviction_journal SET state='completed',completed_at=clock_timestamp(),error_code=NULL WHERE id=%s AND state='prepared'",(row['eviction_id'],))
        c.execute("UPDATE attachment_residency SET state='evicted',evicted_at=clock_timestamp(),updated_at=clock_timestamp() WHERE digest=%s",(row['digest'],))

    def _complete(self,token):
        with self.store.pool.connection() as c:
            row=c.execute("SELECT r.*,j.root_device,j.root_inode FROM attachment_residency r JOIN attachment_eviction_journal j ON j.id=r.eviction_id WHERE j.id=%s AND j.state='prepared' AND r.state='evicting'",(token,)).fetchone()
        if not row:return False,'state_changed'
        verified=None;missing=False;failure=None
        try:verified=self._verified(row['digest'],row['bytes'])
        except OriginalMissing as error:
            if error.root_identity==(row['root_device'],row['root_inode']):missing=True
            else:failure='original_root_changed'
        except (OSError,ValueError):failure='original_integrity'
        try:
            with self.store.pool.connection() as c:
                lifecycle_lock(c)
                current=c.execute("SELECT r.*,j.root_device,j.root_inode FROM attachment_residency r JOIN attachment_eviction_journal j ON j.id=r.eviction_id WHERE j.id=%s AND j.state='prepared' AND r.state='evicting' FOR UPDATE OF r,j",(token,)).fetchone()
                if not current:return False,'state_changed'
                if missing:
                    # Reopen the expected root while serialized: a mount may
                    # have changed since the failed file open outside this lock.
                    try:
                        missing_root=os.open(self.store.files,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                    except OSError:
                        self._cancel(c,current,'original_root_changed');return False,'original_root_changed'
                    try:
                        if _root_identity(os.fstat(missing_root))!=(current['root_device'],current['root_inode']):
                            self._cancel(c,current,'original_root_changed');return False,'original_root_changed'
                        try:os.stat(current['digest'],dir_fd=missing_root,follow_symlinks=False)
                        except FileNotFoundError:pass
                        else:
                            self._cancel(c,current,'original_reappeared');return False,'original_reappeared'
                        os.fsync(missing_root)
                    finally:os.close(missing_root)
                    # Only a committed prepared intent on the same CAS root
                    # may account for absence after crash.
                    self._mark_evicted(c,current)
                    return True,None
                if failure:
                    self._cancel(c,current,failure);return False,failure
                reason=self._eligible(c,current)
                if reason:
                    self._cancel(c,current,reason);return False,reason
                root_fd,fd,identity=verified
                expected_root=(current['root_device'],current['root_inode'])
                if (_root_identity(os.fstat(root_fd))!=expected_root
                        or _root_identity(os.stat(self.store.files,follow_symlinks=False))!=expected_root):
                    self._cancel(c,current,'original_root_changed');return False,'original_root_changed'
                now=os.stat(current['digest'],dir_fd=root_fd,follow_symlinks=False)
                if _identity(now)!=identity or _identity(os.fstat(fd))!=identity:
                    self._cancel(c,current,'original_changed');return False,'original_changed'
                os.unlink(current['digest'],dir_fd=root_fd)
                os.fsync(root_fd)
                self._mark_evicted(c,current)
                return True,None
        finally:
            if verified:
                os.close(verified[1]);os.close(verified[0])

    def run_once(self,*,apply=False,limit=100,max_bytes=1024**3):
        if (type(apply) is not bool or type(limit) is not int or not 1<=limit<=1000
                or type(max_bytes) is not int or not 1<=max_bytes<=1024**4):
            raise ValueError('invalid_lifecycle_limit')
        result={'dry_run':not apply,'expire_unprocessed':self.expire_unprocessed,'cold_copy_required':self.require_cold_copy,'candidates':0,'candidate_bytes':0,'evicted':0,'evicted_bytes':0,'blocked':0,'reasons':{}}
        def blocked(reason):
            result['blocked']+=1;result['reasons'][reason]=result['reasons'].get(reason,0)+1
        with self.store.pool.connection() as c:
            if backup_fence_active(c):
                blocked('backup_active');return result
            pending=c.execute("SELECT id,digest,bytes FROM attachment_eviction_journal WHERE state='prepared' ORDER BY created_at,id LIMIT %s",(limit,)).fetchall()
            # Apply inspections advance updated_at, including blocked entries.
            # This durable order survives one-shot workers and prevents an old
            # unprocessed/corrupt/oversized original from starving later rows.
            # last_accessed_at remains exclusively the retention clock.
            # The chat policy's candidates (ANYM-806) are read with the on-request chats as a constant list;
            # _eligible rechecks each one under the lock.
            from .media_policy import ON_REQUEST_CHATS
            waiting=[r['chat'] for r in c.execute('SELECT orc.chat FROM ('+ON_REQUEST_CHATS+') orc').fetchall()]
            chat_policy='''(r.last_accessed_at<=clock_timestamp()-%(hours)s*interval '1 hour'
                AND EXISTS(SELECT FROM attachments ca JOIN messages cm ON cm.id=ca.message
                    WHERE ca.digest=r.digest AND ca.active AND NOT cm.deleted)
                AND NOT EXISTS(SELECT FROM attachments ca JOIN messages cm ON cm.id=ca.message
                    WHERE ca.digest=r.digest AND ca.active AND NOT cm.deleted AND NOT cm.chat=ANY(%(waiting)s::text[])))'''
            retention=chat_policy if self.chat_policy_only else (
                "(r.last_accessed_at<=clock_timestamp()-interval '30 days' OR "+chat_policy+')')
            rows=c.execute("SELECT r.* FROM attachment_residency r WHERE r.state='resident' AND NOT r.pinned AND "+retention
                +" ORDER BY r.updated_at,r.last_accessed_at,r.digest LIMIT %(limit)s",
                {'limit':limit,'hours':CHAT_POLICY_HOURS,'waiting':waiting}).fetchall()
        spent=0;handled=0
        for row in pending:
            if handled>=limit:break
            if spent+row['bytes']>max_bytes:blocked('byte_limit');continue
            handled+=1;spent+=row['bytes']
            if not apply:blocked('pending_replay');continue
            changed,reason=self._complete(row['id'])
            if changed:
                result['evicted']+=1;result['evicted_bytes']+=row['bytes']
            else:blocked(reason)
        for row in rows:
            if handled>=limit:break
            handled+=1
            with self.store.pool.connection() as c:
                lifecycle_lock(c)
                if backup_fence_active(c):
                    blocked('backup_active');return result
                current=c.execute("SELECT * FROM attachment_residency WHERE digest=%s AND state='resident' FOR UPDATE",(row['digest'],)).fetchone()
                if current is None:reason='state_changed'
                elif spent+current['bytes']>max_bytes:reason='byte_limit'
                else:reason=self._eligible(c,current)
                if apply and current is not None:
                    c.execute('UPDATE attachment_residency SET updated_at=clock_timestamp() WHERE digest=%s',(row['digest'],))
            if reason:blocked(reason);continue
            spent+=row['bytes']  # Failed hashes also consume the work bound.
            # Integrity preflight is read-only even during a dry run. The real
            # apply rehashes after durable prepare, outside the serving lock.
            try:
                verification=self._verified(row['digest'],row['bytes'])
                os.close(verification[1]);os.close(verification[0])
            except (OSError,ValueError):blocked('original_integrity');continue
            result['candidates']+=1;result['candidate_bytes']+=row['bytes']
            if not apply:continue
            token,reason=self._prepare(row['digest'])
            if token:
                changed,reason=self._complete(token)
                if changed:
                    result['evicted']+=1;result['evicted_bytes']+=row['bytes']
                else:blocked(reason)
            else:blocked(reason)
        return result


def pin_original(c,digest,pinned=True):
    """Operator pin is CAS-wide, so one occurrence protects all duplicates."""
    _digest(digest)
    if type(pinned) is not bool:raise ValueError('invalid_pin')
    lifecycle_lock(c)
    row=c.execute('''UPDATE attachment_residency SET pinned=%s,updated_at=clock_timestamp()
        WHERE digest=%s RETURNING state,pinned''',(pinned,digest)).fetchone()
    if row is None:raise ValueError('unknown_original')
    return dict(row)


def is_managed_evicted(c,digest,bytes=None):
    _digest(digest)
    return c.execute('''SELECT EXISTS(SELECT FROM attachment_residency r
        JOIN attachment_eviction_journal j ON j.id=r.eviction_id
        WHERE r.digest=%s AND (%s::bigint IS NULL OR r.bytes=%s)
        AND r.state='evicted' AND j.state='completed' AND j.digest=r.digest AND j.bytes=r.bytes) AS evicted''',
        (digest,bytes,bytes)).fetchone()['evicted']


def register_resident_original(c,digest,bytes):
    _digest(digest)
    return c.execute('SELECT public.register_resident_original(%s,%s) AS changed',(digest,bytes)).fetchone()['changed']


def main(argv=None):
    import argparse
    import json
    import signal
    import threading
    from .store import Store
    parser=argparse.ArgumentParser(description='Preview or apply bounded original-file retention; no provider access')
    parser.add_argument('--dsn-file',default=os.environ.get('DATABASE_URL_FILE'))
    parser.add_argument('--files',default=os.environ.get('PROBE_FILES','/data/files'))
    parser.add_argument('--apply',action='store_true',help='explicitly enable original deletion')
    parser.add_argument('--expire-unprocessed',action='store_true',
        help='allow old unprocessed originals to expire; retain existing text; a verified cold copy is still required')
    parser.add_argument('--chat-policy-only',action='store_true',
        help='one pass over originals of on-request group/channel chats only (ANYM-806); a verified cold copy is required')
    parser.add_argument('--limit',type=int,default=100)
    parser.add_argument('--max-bytes',type=int,default=1024**3)
    parser.add_argument('--interval-seconds',type=int,help='repeat until SIGTERM/SIGINT; omit for one pass')
    parser.add_argument('--digest',help='existing CAS digest for operator pin/unpin')
    pins=parser.add_mutually_exclusive_group()
    pins.add_argument('--pin',action='store_true');pins.add_argument('--unpin',action='store_true')
    args=parser.parse_args(argv)
    if not args.dsn_file:parser.error('--dsn-file or DATABASE_URL_FILE is required')
    if not 1<=args.limit<=1000 or not 1<=args.max_bytes<=1024**4:parser.error('invalid_lifecycle_limit')
    if args.interval_seconds is not None and not 60<=args.interval_seconds<=7*86400:parser.error('invalid_interval')
    if bool(args.digest)!=(args.pin or args.unpin):parser.error('--digest and --pin/--unpin are required together')
    if (args.pin or args.unpin) and (args.apply or args.interval_seconds or args.expire_unprocessed or args.chat_policy_only):
        parser.error('pin is a separate one-shot operation')
    store=Store(Path(args.dsn_file).read_text().strip(),Path(args.files),'http://localhost')
    stopped=threading.Event()
    old_handlers={}
    if args.interval_seconds:
        for signum in (signal.SIGINT,signal.SIGTERM):
            old_handlers[signum]=signal.signal(signum,lambda *_:stopped.set())
    try:
        if args.digest:
            with store.pool.connection() as c:result=pin_original(c,args.digest,args.pin)
            print(json.dumps({'operation':'pin' if args.pin else 'unpin',**result}),flush=True)
            return 0
        worker=LifecycleWorker(store,expire_unprocessed=args.expire_unprocessed,require_cold_copy=True,
            chat_policy_only=args.chat_policy_only)
        while not stopped.is_set():
            try:result=worker.run_once(apply=args.apply,limit=args.limit,max_bytes=args.max_bytes)
            except Exception:
                # Never print SQL, DSNs, file names or arbitrary exception text.
                print(json.dumps({'status':'error','error_code':'file_lifecycle_failed'}),flush=True)
                if args.interval_seconds is None:return 1
            else:print(json.dumps(result),flush=True)
            if args.interval_seconds is None:break
            stopped.wait(args.interval_seconds)
        return 0
    finally:
        store.close()
        for signum,handler in old_handlers.items():signal.signal(signum,handler)


if __name__=='__main__':
    raise SystemExit(main())
