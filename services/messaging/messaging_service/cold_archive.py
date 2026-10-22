"""Operator-only encrypted cold copies of CAS originals in the private R2 bucket.

The producer never holds an R2 credential: objects travel through the receiver's
`cold` SSH forced command, put/get by digest only. A copy is recorded `verified`
only after the stored ciphertext is read back, hashed and decrypted to the CAS
digest. Upload success alone never authorizes eviction (file_lifecycle.py).
Plaintext is read from the CAS and never staged; reports carry no paths.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import tempfile
import threading

from .file_lifecycle import _digest, _identity, lifecycle_lock
from .storage_admission import MEDIA_RESERVE_BYTES

MIB=1024**2
AGE_HEADER=b'age-encryption.org/v1\n'
RECIPIENT=re.compile(r'age1[02-9ac-hj-np-z]{58}')
KEY=re.compile(r'[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*/cold/([a-f0-9]{64})\.age')
UNAVAILABLE='cold_unavailable'
VERIFY_FAILED='cold_verify_failed'


class ColdUnavailable(RuntimeError):
    """Receiver, network or R2 failure: nothing about the stored object is known."""


class ColdVerifyFailed(RuntimeError):
    """The object read back does not prove the original."""


def _path(value):
    if not isinstance(value,str) or not Path(value).is_absolute() or '..' in Path(value).parts or any(c in value for c in ',\n\r\x00'):
        raise ValueError('invalid_cold_archive_path')
    return value


def load_config(path):
    """Private v1 JSON; unsupported or duplicate fields fail closed."""
    from .backup_r2_receiver import unique_json
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        info=os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_mode&0o077 or
                info.st_uid not in {0,os.geteuid()} or info.st_size>16384):raise ValueError('private_config_required')
        value=unique_json(source.read(16385))
    required={'v','transfer','recipients','identity_file','total_bytes'}
    defaults={'age':'age','archive_after_days':23,'pass_bytes':8*1024**3,'pass_files':500,
              'max_object_bytes':4*1024**3,'timeout_seconds':3600,'restore_disk_reserve_bytes':MEDIA_RESERVE_BYTES}
    if (not isinstance(value,dict) or not required<=set(value) or set(value)-required-set(defaults) or
            type(value['v']) is not int or value['v']!=1):raise ValueError('invalid_cold_archive_config')
    value={**defaults,**value}
    t=value['transfer']
    if (not isinstance(t,dict) or set(t)!={'host','user','identity_file','known_hosts_file'} or
            not isinstance(t['host'],str) or not re.fullmatch(r'[a-zA-Z0-9.:-]{1,253}',t['host']) or
            not isinstance(t['user'],str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',t['user'])):
        raise ValueError('invalid_cold_archive_transfer')
    _path(t['identity_file']);_path(t['known_hosts_file']);_path(value['identity_file'])
    recipients=value['recipients']
    # The backup recovery recipient keeps the offline backup key able to restore
    # every object; the dedicated cold recipient is the only key on the producer.
    if (not isinstance(recipients,list) or len(recipients)!=2 or len(set(recipients))!=2 or
            not all(isinstance(r,str) and RECIPIENT.fullmatch(r) for r in recipients)):
        raise ValueError('invalid_cold_archive_recipients')
    if not isinstance(value['age'],str) or not re.fullmatch(r'age|/[a-zA-Z0-9_./-]{1,255}',value['age']):
        raise ValueError('invalid_cold_archive_config')
    limits={'archive_after_days':(1,29),'pass_files':(1,100000),'timeout_seconds':(60,21600),
            'max_object_bytes':(64,5*1024**4),'pass_bytes':(1,10*1024**4),'total_bytes':(1,100*1024**4),
            # Restores never dip below the collectors' default media floor (ANYM-350).
            'restore_disk_reserve_bytes':(MEDIA_RESERVE_BYTES,100*1024**3)}
    for key,(low,high) in limits.items():
        if type(value[key]) is not int or not low<=value[key]<=high:raise ValueError('invalid_cold_archive_limits')
    return value


def fingerprint(recipient):
    return hashlib.sha256(recipient.encode()).hexdigest()


@contextmanager
def identity_descriptor(path):
    """Open and validate the private age identity; age reads this exact descriptor."""
    try:fd=os.open(_path(path),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except OSError:raise ValueError('private_cold_identity_required') from None
    try:
        info=os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_mode&0o077 or
                info.st_uid!=os.geteuid() or not 0<info.st_size<=65536):
            raise ValueError('private_cold_identity_required')
        yield fd
    finally:
        os.close(fd)


def _response(raw):
    if len(raw)>4096 or not raw.endswith(b'\n'):raise ColdUnavailable(UNAVAILABLE)
    try:item=json.loads(raw)
    except ValueError:raise ColdUnavailable(UNAVAILABLE) from None
    if not isinstance(item,dict) or item.get('v')!=1 or item.get('status')=='error':
        if isinstance(item,dict) and item.get('code')=='cold_object_missing':raise ColdVerifyFailed('cold_object_missing')
        raise ColdUnavailable(UNAVAILABLE)
    return item


def _key(item,digest):
    match=KEY.fullmatch(item.get('key') or '') if isinstance(item.get('key'),str) else None
    if not match or match.group(1)!=digest:raise ColdUnavailable(UNAVAILABLE)
    return item['key']


class SSHTransport:
    """The receiver's fixed `cold` forced command over a dedicated restricted key."""
    def __init__(self,transfer,timeout):
        self.transfer=transfer;self.timeout=timeout

    def _spawn(self):
        t=self.transfer
        args=['ssh','-F','/dev/null','-T','-o','BatchMode=yes','-o','IdentitiesOnly=yes',
            '-o','StrictHostKeyChecking=yes','-o','PasswordAuthentication=no','-o','KbdInteractiveAuthentication=no',
            '-o','ConnectTimeout=15','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
            '-o','UserKnownHostsFile='+t['known_hosts_file'],'-i',t['identity_file'],t['user']+'@'+t['host'],'cold-v1']
        env={k:v for k,v in os.environ.items() if k!='SSH_AUTH_SOCK'}
        return self.command(args,env)

    @staticmethod
    def command(args,env):
        return subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env)

    @contextmanager
    def _session(self,header):
        try:process=self._spawn()
        except OSError:raise ColdUnavailable(UNAVAILABLE) from None
        timer=threading.Timer(self.timeout,process.kill);timer.daemon=True;timer.start()
        try:
            process.stdin.write(json.dumps(header,separators=(',',':')).encode()+b'\n');process.stdin.flush()
            yield process
        except OSError:raise ColdUnavailable(UNAVAILABLE) from None
        finally:
            timer.cancel()
            for stream in (process.stdin,process.stdout):
                try:stream.close()
                except OSError:pass
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)

    def put(self,digest,chunks,before_commit):
        """Stream framed ciphertext; before_commit runs ahead of the committing footer."""
        with self._session({'v':1,'op':'put','digest':digest}) as process:
            first=_response(process.stdout.readline(4097))
            if first.get('status')=='exists':
                if type(first.get('bytes')) is not int:raise ColdUnavailable(UNAVAILABLE)
                return {'status':'exists','key':_key(first,digest),'bytes':first['bytes']}
            if first.get('status')!='ready':raise ColdUnavailable(UNAVAILABLE)
            key=_key(first,digest);sha=hashlib.sha256();size=0
            for chunk in chunks:
                for offset in range(0,len(chunk),MIB):
                    part=chunk[offset:offset+MIB]
                    process.stdin.write(struct.pack('!I',len(part))+part);size+=len(part);sha.update(part)
            before_commit(key,size,sha.hexdigest())
            process.stdin.write(struct.pack('!I',0)+json.dumps({'bytes':size,'sha256':sha.hexdigest()}).encode()+b'\n')
            process.stdin.close()
            final=_response(process.stdout.readline(4097))
            if process.wait(timeout=self.timeout):raise ColdUnavailable(UNAVAILABLE)
            if final.get('status')=='exists' and type(final.get('bytes')) is int:
                return {'status':'exists','key':_key(final,digest),'bytes':final['bytes']}
            if final.get('status')!='stored' or final.get('bytes')!=size or final.get('sha256')!=sha.hexdigest():
                raise ColdUnavailable(UNAVAILABLE)
            return {'status':'stored','key':_key(final,digest),'bytes':size,'sha256':sha.hexdigest()}

    @contextmanager
    def get(self,digest):
        """Yield (key, bytes, reader) for the stored ciphertext; the caller counts bytes."""
        with self._session({'v':1,'op':'get','digest':digest}) as process:
            process.stdin.close()
            item=_response(process.stdout.readline(4097))
            if item.get('status')!='found' or type(item.get('bytes')) is not int or item['bytes']<0:
                raise ColdUnavailable(UNAVAILABLE)
            yield _key(item,digest),item['bytes'],process.stdout
            if process.wait(timeout=self.timeout):raise ColdUnavailable(UNAVAILABLE)


class ColdArchiver:
    """Bounded archive pass: copy, read back, verify; never deletes anything."""
    def __init__(self,store,config,transport=None):
        self.store=store;self.config=config
        self.transport=transport if transport is not None else SSHTransport(config['transfer'],config['timeout_seconds'])
        self.fingerprints=sorted(fingerprint(r) for r in config['recipients'])

    def _candidates(self,limit):
        with self.store.pool.connection() as c:
            return c.execute('''SELECT r.digest,r.bytes,cc.state AS cold_state,cc.ciphertext_bytes,cc.ciphertext_sha256,cc.recipient_fingerprints
                FROM attachment_residency r LEFT JOIN attachment_cold_copies cc ON cc.digest=r.digest
                WHERE r.state='resident' AND NOT r.pinned
                  AND r.last_accessed_at<=clock_timestamp()-make_interval(days=>%s)
                  AND (cc.digest IS NULL OR cc.state<>'verified' OR cc.bytes<>r.bytes)
                ORDER BY coalesce(cc.state='failed',false),r.last_accessed_at,r.digest LIMIT %s''',(self.config['archive_after_days'],limit)).fetchall()

    def used_bytes(self,c):
        # Any copy whose ciphertext was offered for commit, or that was seen in
        # R2, may exist there even after a failed read-back; it stays budgeted.
        return c.execute("""SELECT coalesce(sum(bytes),0)::bigint AS used FROM attachment_cold_copies
            WHERE state IN ('verified','pending') OR ciphertext_bytes IS NOT NULL""").fetchone()['used']

    def _open_original(self,digest,size):
        root=os.open(self.store.files,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:fd=os.open(_digest(digest),os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW,dir_fd=root)
        except OSError:raise ValueError('original_integrity') from None
        finally:os.close(root)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size!=size:
            os.close(fd);raise ValueError('original_integrity')
        return fd,_identity(info)

    def _encrypt(self,digest,size):
        """Yield age ciphertext of the CAS original, re-hashed while it is read."""
        fd,identity=self._open_original(digest,size)
        failures=[];checked={}
        args=[self.config['age']]
        for recipient in self.config['recipients']:args+=['-r',recipient]
        with tempfile.TemporaryFile() as errors:
            try:process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=errors)
            except OSError:
                os.close(fd);raise RuntimeError('cold_encryption_failed') from None
            def feed():
                try:
                    with os.fdopen(fd,'rb') as source:
                        sha=hashlib.sha256();count=0
                        while chunk:=source.read(MIB):
                            sha.update(chunk);count+=len(chunk);process.stdin.write(chunk)
                        checked.update(sha=sha.hexdigest(),bytes=count,identity=_identity(os.fstat(source.fileno())))
                except BaseException as exc:failures.append(exc)
                finally:
                    try:process.stdin.close()
                    except OSError:pass
            thread=threading.Thread(target=feed,name='cold-archive-encrypt',daemon=True);thread.start()
            try:
                while chunk:=process.stdout.read(MIB):yield chunk
                if process.wait(timeout=60):raise RuntimeError('cold_encryption_failed')
                thread.join(timeout=60)
                if thread.is_alive() or failures:raise ValueError('original_integrity')
                # The footer that commits the object is sent only after this check.
                if checked.get('sha')!=digest or checked.get('bytes')!=size or checked.get('identity')!=identity:
                    raise ValueError('original_integrity')
            finally:
                if process.poll() is None:process.kill();process.wait(timeout=10)
                thread.join(timeout=10)

    def verify(self,digest,size,expected):
        """Read the stored object back from R2 and decrypt it with the cold identity.

        The ciphertext must match its recorded size/SHA-256 when one is known and
        decrypt to exactly the CAS digest and size. Returns the observed ciphertext.
        """
        try:
            with self.transport.get(digest) as (key,stored,reader):
                if expected.get('bytes') is not None and stored!=expected['bytes']:raise ColdVerifyFailed('cold_ciphertext_mismatch')
                with identity_descriptor(self.config['identity_file']) as identity:
                    observed=self._decrypt_check(reader,stored,identity,digest,size)
        except ColdVerifyFailed:raise
        except ValueError as error:
            if str(error)=='private_cold_identity_required':raise
            raise ColdVerifyFailed(VERIFY_FAILED) from None
        if expected.get('sha256') is not None and observed['sha256']!=expected['sha256']:
            raise ColdVerifyFailed('cold_ciphertext_mismatch')
        return {'key':key,**observed}

    def _decrypt_check(self,reader,stored,identity,digest,size,sink=None):
        failures=[];cipher={}
        with tempfile.TemporaryFile() as errors:
            process=subprocess.Popen([self.config['age'],'-d','-i','/dev/fd/'+str(identity)],stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=errors,pass_fds=(identity,))
            def feed():
                try:
                    sha=hashlib.sha256();count=0;prefix=b'';writing=True
                    while count<stored:
                        chunk=reader.read(min(MIB,stored-count))
                        if not chunk:break
                        count+=len(chunk);sha.update(chunk)
                        if len(prefix)<len(AGE_HEADER):prefix+=chunk[:len(AGE_HEADER)-len(prefix)]
                        if writing:
                            # A rejected header ends age early; keep hashing the object.
                            try:process.stdin.write(chunk)
                            except BrokenPipeError:writing=False
                    if reader.read(1):count+=1
                    cipher.update(bytes=count,sha256=sha.hexdigest(),age=prefix==AGE_HEADER)
                except BaseException as exc:failures.append(exc)
                finally:
                    try:process.stdin.close()
                    except OSError:pass
            thread=threading.Thread(target=feed,name='cold-archive-readback',daemon=True);thread.start()
            try:
                plain=hashlib.sha256();count=0
                while chunk:=process.stdout.read(MIB):
                    count+=len(chunk);plain.update(chunk)
                    if count>size:
                        # Too much plaintext already disproves the copy; never block on it.
                        process.kill();break
                    # Restores stream plaintext only into the CAS writer's private temporary file.
                    if sink is not None:sink(chunk)
                try:failed=process.wait(timeout=60)
                except subprocess.TimeoutExpired:raise ColdUnavailable(UNAVAILABLE) from None
                thread.join(timeout=60)
            finally:
                if process.poll() is None:process.kill();process.wait(timeout=10)
                thread.join(timeout=10)
        if failures or thread.is_alive():raise ColdUnavailable(UNAVAILABLE)
        if cipher.get('bytes')!=stored or not cipher.get('age'):raise ColdVerifyFailed('cold_ciphertext_mismatch')
        if failed or count!=size or plain.hexdigest()!=digest:raise ColdVerifyFailed(VERIFY_FAILED)
        return {'bytes':stored,'sha256':cipher['sha256']}

    def _record(self,digest,size,**fields):
        """Persist one copy transition under the lifecycle lock, for unchanged residency only."""
        state=fields['state']
        with self.store.pool.connection() as c:
            lifecycle_lock(c)
            # Every residency transition holds the same lock; no row lock or
            # residency UPDATE privilege is needed to read it consistently.
            resident=c.execute("SELECT bytes FROM attachment_residency WHERE digest=%s AND state='resident'",(digest,)).fetchone()
            if resident is None or resident['bytes']!=size:return False
            row=c.execute('SELECT state FROM attachment_cold_copies WHERE digest=%s FOR UPDATE',(digest,)).fetchone()
            if row is None:
                # A failure before any object was seen leaves nothing to record;
                # an existing object found in R2 is kept visible to the operator.
                if state=='failed' and not fields.get('object_key'):return True
                c.execute("INSERT INTO attachment_cold_copies(digest,bytes,state,object_key) VALUES(%s,%s,'pending',%s)",
                          (digest,size,fields['object_key']))
            if state=='pending':
                c.execute('''UPDATE attachment_cold_copies SET bytes=%s,state='pending',object_key=%s,
                    ciphertext_bytes=%s,ciphertext_sha256=%s,attempts=attempts+1,error_code=NULL,verified_at=NULL,
                    recipient_fingerprints=%s,updated_at=clock_timestamp() WHERE digest=%s''',
                    (size,fields['object_key'],fields['ciphertext_bytes'],fields['ciphertext_sha256'],self.fingerprints,digest))
            elif state=='verified':
                c.execute('''UPDATE attachment_cold_copies SET bytes=%s,state='verified',object_key=%s,ciphertext_bytes=%s,
                    ciphertext_sha256=%s,recipient_fingerprints=%s,error_code=NULL,verified_at=clock_timestamp(),
                    updated_at=clock_timestamp() WHERE digest=%s''',
                    (size,fields['object_key'],fields['ciphertext_bytes'],fields['ciphertext_sha256'],self.fingerprints,digest))
            else:
                # The recorded ciphertext expectation survives: an object this
                # attempt did commit is recognized on the next pass, never replaced.
                # An object seen in R2 keeps its size, so it stays budgeted.
                c.execute('''UPDATE attachment_cold_copies SET state='failed',error_code=%s,verified_at=NULL,
                    ciphertext_bytes=coalesce(ciphertext_bytes,%s),updated_at=clock_timestamp()
                    WHERE digest=%s AND state<>'verified' ''',(fields['error_code'],fields.get('ciphertext_bytes'),digest))
            return True

    def _archive_one(self,row):
        digest,size=row['digest'],row['bytes']
        known={'bytes':row['ciphertext_bytes'],'sha256':row['ciphertext_sha256']}
        def before_commit(key,cipher_bytes,cipher_sha):
            # The expected ciphertext is durable before the receiver may commit it.
            if not self._record(digest,size,state='pending',object_key=key,ciphertext_bytes=cipher_bytes,ciphertext_sha256=cipher_sha):
                raise ValueError('state_changed')
        stored={}
        try:
            stored=self.transport.put(digest,self._encrypt(digest,size),before_commit)
            if stored['status']=='exists':
                # Never overwritten. Decrypting with the cold key cannot show that
                # the backup recovery key also opens the object, so an existing
                # object counts only when it is the ciphertext this archiver recorded,
                # for the current recipients, before the receiver could commit it.
                if (not known['sha256'] or known['bytes']!=stored['bytes'] or
                        sorted(row['recipient_fingerprints'] or [])!=self.fingerprints):
                    raise ColdVerifyFailed('cold_object_unattested')
                expected={'bytes':stored['bytes'],'sha256':known['sha256']}
            else:expected={'bytes':stored['bytes'],'sha256':stored['sha256']}
            observed=self.verify(digest,size,expected)
        except ColdVerifyFailed as error:
            self._record(digest,size,state='failed',object_key=stored.get('key'),ciphertext_bytes=stored.get('bytes'),
                         error_code=str(error) if re.fullmatch('[a-z_]{1,64}',str(error)) else VERIFY_FAILED)
            return VERIFY_FAILED
        except ColdUnavailable:
            self._record(digest,size,state='failed',error_code=UNAVAILABLE)
            return UNAVAILABLE
        except ValueError as error:
            reason=str(error) if str(error) in ('original_integrity','state_changed','private_cold_identity_required') else 'original_integrity'
            self._record(digest,size,state='failed',error_code=reason)
            return reason
        except RuntimeError:
            self._record(digest,size,state='failed',error_code='cold_encryption_failed')
            return 'cold_encryption_failed'
        if not self._record(digest,size,state='verified',object_key=observed['key'],
                            ciphertext_bytes=observed['bytes'],ciphertext_sha256=observed['sha256']):
            return 'state_changed'
        return None

    def run_once(self,*,apply=False,limit=200):
        if type(apply) is not bool or type(limit) is not int or not 1<=limit<=10000:raise ValueError('invalid_cold_archive_limit')
        result={'dry_run':not apply,'candidates':0,'candidate_bytes':0,'archived':0,'archived_bytes':0,
                'blocked':0,'reasons':{}}
        def blocked(reason):
            result['blocked']+=1;result['reasons'][reason]=result['reasons'].get(reason,0)+1
        rows=self._candidates(limit)
        with self.store.pool.connection() as c:used=self.used_bytes(c)
        # Rows already budgeted in `used` add no new bytes.
        files=0;spent=0;stopped=False
        for row in rows:
            new=0 if row['cold_state']=='pending' or row['ciphertext_bytes'] is not None else row['bytes']
            if stopped:blocked(UNAVAILABLE);continue
            if (files>=self.config['pass_files'] or spent+row['bytes']>self.config['pass_bytes'] or
                    used+new>self.config['total_bytes'] or row['bytes']>self.config['max_object_bytes']):
                blocked('cold_budget');continue
            files+=1;spent+=row['bytes'];used+=new
            result['candidates']+=1;result['candidate_bytes']+=row['bytes']
            if not apply:continue
            reason=self._archive_one(row)
            if reason is None:
                result['archived']+=1;result['archived_bytes']+=row['bytes']
            else:
                blocked(reason)
                if reason==UNAVAILABLE:stopped=True
        with self.store.pool.connection() as c:
            result['budget']={'total_bytes':self.config['total_bytes'],'used_bytes':self.used_bytes(c)}
        return result


class ColdRestorer(ColdArchiver):
    """Restore requested evicted originals from their verified R2 copies (ANYM-350).

    The gateway queues a restore (media_requests.cold_request). The ciphertext
    must match its recorded size and SHA-256 and decrypt to the CAS digest; the
    plaintext goes only through BlobWriter. A restore is admitted against the
    collectors' media floor and reservations before any R2 read. Any later
    failure publishes nothing; a remote or copy failure hands the request to
    the provider path while its demand window is open, a local limit does not.
    Every state change is conditioned on the request generation it claimed.
    """
    def __init__(self,store,config,transport=None,*,reserve_bytes=None):
        super().__init__(store,config,transport)
        self.reserve_bytes=config['restore_disk_reserve_bytes'] if reserve_bytes is None else reserve_bytes

    def _due(self,limit):
        from .media_requests import COLD_RESTORE_STALL_SECONDS
        # Older requests belong to the gateway's stalled-restore fallback.
        with self.store.pool.connection() as c:
            return c.execute('''SELECT digest,bytes,object_key,ciphertext_bytes,ciphertext_sha256,restore_requested_at
                FROM attachment_cold_copies WHERE restore_state='requested' AND state='verified'
                  AND restore_requested_at>clock_timestamp()-make_interval(secs=>%s)
                ORDER BY restore_requested_at,digest LIMIT %s''',(COLD_RESTORE_STALL_SECONDS,limit)).fetchall()

    def _present(self,digest,size):
        from .blobs import Blob,BlobWriter
        try:
            if not (self.store.files/digest).exists():return False
            BlobWriter(self.store.files,reserve_bytes=0).verify(Blob(digest,size))
        except (RuntimeError,OSError):return False
        return True

    def _admit(self,c,digest,size,claimed):
        """Capacity check before any R2 read; lifecycle precedes the capacity lock.

        The floor is the higher of the configured one and every collector's
        published floor for this volume. An admitted restore leases its bytes in
        the same transaction, so a collector admitted while it streams counts them
        (restore_reserved); the writer stops when the lease ends.
        Returns (reason, None) or (None, (floor, lease_end)).
        """
        import shutil
        from .storage_admission import (COLD_RESTORING,STREAM_WORKSPACE_BYTES,StorageAdmission,has_capacity,
            published_floor,restore_reserved,volume_key)
        if size>self.config['max_object_bytes']:return 'cold_size_limit',None
        StorageAdmission.lock(c)
        volume=volume_key(self.store.files)
        reserved=c.execute('SELECT coalesce(sum(reserved_bytes),0) AS bytes FROM media_reservations WHERE volume_key=%s',
            (volume,)).fetchone()['bytes']
        reserved+=c.execute("""SELECT coalesce(sum(2*bytes+%s),0) AS bytes FROM file_uploads
            WHERE volume_key=%s AND state<>'expired'""",(STREAM_WORKSPACE_BYTES,volume)).fetchone()['bytes']
        reserved+=restore_reserved(c,excluding=digest)
        floor=max(self.reserve_bytes,published_floor(c,volume))
        free=shutil.disk_usage(self.store.files).free
        if not has_capacity(free,reserved,size+STREAM_WORKSPACE_BYTES,floor):return 'disk_reserve',None
        # The lease ends with the transfer timeout; a crashed worker's lease lapses on its own.
        until=c.execute('''UPDATE attachment_cold_copies SET restore_error=%s,
            restore_finished_at=clock_timestamp()+make_interval(secs=>%s) WHERE '''+claimed[0]+' RETURNING restore_finished_at',
            (COLD_RESTORING,self.config['timeout_seconds'],*claimed[1])).fetchone()['restore_finished_at']
        return None,(floor,until)

    def _register(self,c,digest,size,generation):
        """Make the original resident; clear the request only while it is still the claimed one.

        A request handed to the provider meanwhile keeps its outcome (stalled
        fallback); generation None (reconciliation) clears nothing.
        """
        from .file_lifecycle import register_resident_original
        register_resident_original(c,digest,size)
        if generation is not None:
            c.execute('''UPDATE attachment_cold_copies SET restore_state='idle',restore_error=NULL,
                restore_finished_at=clock_timestamp() WHERE digest=%s AND restore_state='requested'
                AND restore_requested_at=%s''',(digest,generation))

    def _reconcile(self):
        """A published original whose registration was interrupted becomes resident.

        Independent of request age and R2 availability: the CAS file is verified
        locally and registered under the lifecycle lock. Every candidate is seen
        on every pass (keyset pages); one unreadable file skips only itself.
        """
        from .file_lifecycle import is_managed_evicted
        reconciled=skipped=0;last=''
        while True:
            with self.store.pool.connection() as c:
                rows=c.execute('''SELECT cc.digest,cc.bytes FROM attachment_cold_copies cc
                    JOIN attachment_residency r ON r.digest=cc.digest AND r.bytes=cc.bytes
                    WHERE cc.restore_state IN ('requested','failed') AND r.state='evicted' AND cc.digest>%s
                    ORDER BY cc.digest LIMIT 1000''',(last,)).fetchall()
            if not rows:return reconciled,skipped
            last=rows[-1]['digest']
            for row in rows:
                try:
                    if not self._present(row['digest'],row['bytes']):continue
                    with self.store.pool.connection() as c:
                        lifecycle_lock(c)
                        if is_managed_evicted(c,row['digest'],row['bytes']) and self._present(row['digest'],row['bytes']):
                            self._register(c,row['digest'],row['bytes'],None);reconciled+=1
                except Exception:
                    # Retried next pass; due requests still run after it.
                    skipped+=1

    def _fetch(self,row,admission):
        from .blobs import BlobSink,BlobWriter
        from .storage_admission import MediaReservation
        digest,size=row['digest'],row['bytes'];floor,until=admission
        writer=BlobWriter(self.store.files,max_bytes=self.config['max_object_bytes'],reserve_bytes=floor)
        # Past its lease the restore no longer owns capacity: the writer stops (media_lease_lost).
        with BlobSink(writer,size,digest,reservation=MediaReservation(size,until)) as sink:
            with self.transport.get(digest) as (key,stored,reader):
                if key!=row['object_key'] or stored!=row['ciphertext_bytes']:raise ColdVerifyFailed('cold_ciphertext_mismatch')
                with identity_descriptor(self.config['identity_file']) as identity:
                    observed=self._decrypt_check(reader,stored,identity,digest,size,sink=sink.push)
            if observed['sha256']!=row['ciphertext_sha256']:raise ColdVerifyFailed('cold_ciphertext_mismatch')
            # Published only now: ciphertext and plaintext both match the record.
            return sink.finish()

    def _restore_one(self,row):
        from .file_lifecycle import is_managed_evicted
        from .media_requests import COLD_LOCAL,COLD_RESTORE_STALL_SECONDS
        digest,size,generation=row['digest'],row['bytes'],row['restore_requested_at']
        claimed='digest=%s AND restore_state=\'requested\' AND restore_requested_at=%s'
        with self.store.pool.connection() as c:
            lifecycle_lock(c)
            # Revalidate the claim: a stalled, superseded or disproved request is not ours.
            if c.execute('SELECT FROM attachment_cold_copies WHERE '+claimed+''' AND state='verified'
                    AND restore_requested_at>clock_timestamp()-make_interval(secs=>%s) FOR UPDATE''',
                    (digest,generation,COLD_RESTORE_STALL_SECONDS)).fetchone() is None:return False
            if not is_managed_evicted(c,digest,size):
                # Already resident again (provider or an earlier restore).
                c.execute('''UPDATE attachment_cold_copies SET restore_state='idle',restore_error=NULL,
                    restore_finished_at=clock_timestamp() WHERE '''+claimed,(digest,generation))
                return False
            if self._present(digest,size):
                # Published by an interrupted attempt: register it, never fetch again.
                self._register(c,digest,size,generation);return None
            reason,admission=self._admit(c,digest,size,(claimed,(digest,generation)))
        disproved=False
        if reason is None:
            try:self._fetch(row,admission)
            except ColdVerifyFailed as error:
                reason=str(error) if re.fullmatch('[a-z_]{1,64}',str(error)) else VERIFY_FAILED;disproved=True
            except ColdUnavailable:reason=UNAVAILABLE
            except ValueError as error:
                reason={'media_size_limit':'cold_size_limit','private_cold_identity_required':'private_cold_identity_required'}.get(
                    str(error),VERIFY_FAILED)
                disproved=reason==VERIFY_FAILED
            except RuntimeError as error:reason='disk_reserve' if str(error)=='disk_reserve' else 'cold_restore_failed'
            except OSError:reason='cold_restore_failed'
        with self.store.pool.connection() as c:
            lifecycle_lock(c)
            if admission is not None:
                # The writer has stopped: end this restore's capacity lease, whoever owns the request now.
                c.execute('''UPDATE attachment_cold_copies SET restore_finished_at=clock_timestamp()
                    WHERE digest=%s AND restore_finished_at>clock_timestamp()''',(digest,))
            if reason is None:
                # The verified original is in the CAS whoever owns the request now;
                # a request handed to the provider meanwhile keeps its outcome.
                self._register(c,digest,size,generation);return None
            if disproved:
                # A disproved copy no longer counts as verified; it is never used again
                # until the archiver re-proves it from a resident original.
                c.execute('''UPDATE attachment_cold_copies SET state='failed',error_code=%s,verified_at=NULL
                    WHERE digest=%s''',(reason,digest))
            owner=c.execute('''UPDATE attachment_cold_copies SET restore_state='failed',restore_error=%s,
                restore_finished_at=clock_timestamp() WHERE '''+claimed+' RETURNING restore_attachment',
                (reason,digest,generation)).fetchone()
            queued=False
            # A local limit would stop the provider download too; a superseded request is not ours to hand over.
            if owner and owner['restore_attachment'] and reason not in COLD_LOCAL:
                queued=c.execute('SELECT public.queue_managed_original(%s) AS queued',(owner['restore_attachment'],)).fetchone()['queued']
        return reason,queued

    def run_once(self,*,limit=16):
        if type(limit) is not int or not 1<=limit<=1000:raise ValueError('invalid_cold_restore_limit')
        reconciled,skipped=self._reconcile()
        result={'restored':0,'restored_bytes':0,'failed':0,'provider_fallback':0,'reasons':{},'reconciled':reconciled,
                'reconcile_skipped':skipped}
        for row in self._due(limit):
            outcome=self._restore_one(row)
            if outcome is False:continue
            if outcome is None:
                result['restored']+=1;result['restored_bytes']+=row['bytes'];continue
            reason,queued=outcome
            result['failed']+=1;result['provider_fallback']+=int(bool(queued))
            result['reasons'][reason]=result['reasons'].get(reason,0)+1
        return result


class WorkerPresence:
    """Session advisory lock held while the restore worker runs (ANYM-350).

    The gateway sends a request straight to the provider when no worker holds
    it; it also keeps a second worker from running beside the first.
    """
    def __init__(self,conninfo):
        self.conninfo=conninfo;self.connection=None;self.held=False

    def acquire(self):
        import psycopg
        from .media_requests import COLD_WORKER_LOCK
        try:
            if self.held:
                # A dropped session releases the lock; take it again before claiming work.
                self.connection.execute('SELECT 1');return True
            if self.connection is None:self.connection=psycopg.connect(self.conninfo,autocommit=True)
            self.held=self.connection.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))',(COLD_WORKER_LOCK,)).fetchone()[0]
        except psycopg.Error:
            self.close()
        return self.held

    def close(self):
        if self.connection is not None:
            try:self.connection.close()
            except Exception:pass
        self.connection=None;self.held=False


def status(store,config=None):
    """Operator summary; no keys, paths or object names."""
    with store.pool.connection() as c:
        rows=c.execute('''SELECT state,count(*)::bigint AS files,coalesce(sum(bytes),0)::bigint AS bytes
            FROM attachment_cold_copies GROUP BY state''').fetchall()
        failures=c.execute('''SELECT error_code,count(*)::bigint AS files FROM attachment_cold_copies
            WHERE state='failed' GROUP BY error_code''').fetchall()
        blocked=c.execute('''SELECT count(*)::bigint AS files FROM attachment_residency r
            WHERE r.state='resident' AND NOT r.pinned AND r.last_accessed_at<=clock_timestamp()-interval '30 days'
            AND NOT EXISTS(SELECT FROM attachment_cold_copies cc WHERE cc.digest=r.digest AND cc.bytes=r.bytes AND cc.state='verified')''').fetchone()
    result={'copies':{r['state']:{'files':r['files'],'bytes':r['bytes']} for r in rows},
            'failures':{r['error_code'] or 'unknown':r['files'] for r in failures},
            'due_without_verified_copy':blocked['files']}
    if config is not None:result['total_bytes']=config['total_bytes']
    return result


def main(argv=None):
    import argparse
    import signal
    from .store import Store
    parser=argparse.ArgumentParser(description='Copy idle originals to the private R2 cold archive and verify them; never deletes')
    parser.add_argument('--config',required=True)
    parser.add_argument('--dsn-file',default=os.environ.get('DATABASE_URL_FILE'))
    parser.add_argument('--files',default=os.environ.get('PROBE_FILES','/data/files'))
    parser.add_argument('--apply',action='store_true',help='explicitly enable uploads')
    parser.add_argument('--limit',type=int,default=200)
    parser.add_argument('--interval-seconds',type=int,help='repeat until SIGTERM/SIGINT; omit for one pass')
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--restore',action='store_true',help='restore requested evicted originals from R2 (ANYM-350)')
    args=parser.parse_args(argv)
    if not args.dsn_file:parser.error('--dsn-file or DATABASE_URL_FILE is required')
    if not 1<=args.limit<=10000:parser.error('invalid_cold_archive_limit')
    # Restores answer explicit requests, so they poll often; archive passes are daily.
    low,high=(5,3600) if args.restore else (60,7*86400)
    if args.interval_seconds is not None and not low<=args.interval_seconds<=high:parser.error('invalid_interval')
    if args.status and (args.apply or args.interval_seconds or args.restore):parser.error('status is a separate one-shot operation')
    if args.restore and (args.apply or not 1<=args.limit<=1000):parser.error('restore runs only requested items; limit 1-1000')
    try:config=load_config(args.config)
    except (OSError,ValueError):
        print(json.dumps({'status':'error','error_code':'invalid_cold_archive_config'}),flush=True);return 1
    store=Store(Path(args.dsn_file).read_text().strip(),Path(args.files),'http://localhost')
    stopped=threading.Event();old={}
    presence=WorkerPresence(store.pool.conninfo) if args.restore else None
    if args.interval_seconds:
        for signum in (signal.SIGINT,signal.SIGTERM):old[signum]=signal.signal(signum,lambda *_:stopped.set())
    try:
        if args.status:
            print(json.dumps(status(store,config)),flush=True);return 0
        archiver=ColdRestorer(store,config) if args.restore else ColdArchiver(store,config)
        while not stopped.is_set():
            if presence is not None and not presence.acquire():
                # Another worker runs, or the database is unreachable: claim nothing.
                print(json.dumps({'status':'error','error_code':'cold_restore_worker_busy'}),flush=True)
                if args.interval_seconds is None:return 1
                stopped.wait(args.interval_seconds);continue
            try:result=archiver.run_once(limit=args.limit) if args.restore else archiver.run_once(apply=args.apply,limit=args.limit)
            except Exception:
                # Never print SQL, DSNs, paths, keys or arbitrary exception text.
                print(json.dumps({'status':'error','error_code':'cold_archive_failed'}),flush=True)
                if args.interval_seconds is None:return 1
            else:
                # An idle restore poll prints nothing, so the log keeps real events.
                if (not args.restore or result['restored'] or result['failed'] or result['reconciled'] or
                        args.interval_seconds is None):
                    print(json.dumps(result),flush=True)
            if args.interval_seconds is None:break
            stopped.wait(args.interval_seconds)
        return 0
    finally:
        if presence is not None:presence.close()
        store.close()
        for signum,handler in old.items():signal.signal(signum,handler)


if __name__=='__main__':
    raise SystemExit(main())
