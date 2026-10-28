"""Stream snapshot bundles through pinned age; encryption keys remain operator-only."""
import os
import hashlib
from contextlib import contextmanager
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading

from .spool import fsync_dir


@contextmanager
def regular_source(path):
    # Verify the opened descriptor, not a pathname checked before open. A
    # replaced FIFO must fail immediately rather than stall the encoder.
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError('invalid_backup_file')
        yield source


def stream_members(directory,blob_root,manifest,manifest_sha=None):
    directory=Path(directory)
    yield 'manifest.json',directory/'manifest.json',manifest_sha
    yield 'database.dump',directory/'database.dump',manifest['dump_sha256']
    for item in manifest['blobs']:
        yield 'files/'+item['digest'],Path(blob_root)/item['digest'],item['digest']
    if (directory/'recovery').exists():
        for path in sorted((directory/'recovery').iterdir()):
            yield path.relative_to(directory).as_posix(),path,path.name


def snapshot_stream_bound(directory,blob_root):
    """Conservative wire bound for normalized USTAR + pinned binary age format.

    Admission is still enforced against actual bytes on the receiver; this bound
    never authorizes unlimited output. It does not stage plaintext/ciphertext.
    Sizes only: it reads no dump or original contents (ANYM-631).
    """
    from .backup import verify_bundle
    manifest=verify_bundle(directory,blob_root=blob_root,hash_dump=False,hash_originals=False)
    size=1024
    for name,path,digest in stream_members(directory,blob_root,manifest):
        if not valid_member(name):raise ValueError('invalid_backup_file')
        size+=512+((path.stat().st_size+511)//512)*512
    size=((size+10239)//10240)*10240
    return size+size//100+1024**2


def encrypt_snapshot_stream(directory,output,recipient,*,blob_root,age='age',expected_manifest_sha256=None):
    """Encrypt directly to a caller-owned bounded sink; originals are never copied.

    The source CAS is immutable and read-only. Capture (or the retry's verify_bundle)
    hashed each original; here only its size is checked before the first byte and
    tar's reads hash it (ANYM-631). Report completion ONLY after source integrity
    and age exit succeed. The receiver must await this result before committing
    its terminal frame.
    """
    from .backup import verify_bundle
    manifest=verify_bundle(directory,blob_root=blob_root,hash_originals=False)
    with regular_source(Path(directory)/'manifest.json') as f:manifest_sha=hashlib.file_digest(f,'sha256').hexdigest()
    if expected_manifest_sha256 is not None and expected_manifest_sha256!=manifest_sha:
        raise RuntimeError('backup_source_changed')
    errors=[];process=None;thread=None
    class CheckedReader:
        def __init__(self,source,expected):self.source=source;self.expected=expected;self.sha=hashlib.sha256()
        def read(self,count=-1):
            data=self.source.read(count);self.sha.update(data);return data
        def finish(self):
            if self.source.read(1) or (self.expected and self.sha.hexdigest()!=self.expected):
                raise RuntimeError('backup_source_changed')
    try:
        with tempfile.TemporaryFile() as stderr:
            process=subprocess.Popen([age,'-r',recipient],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=stderr)
            def feed():
                try:
                    with tarfile.open(fileobj=process.stdin,mode='w|',format=tarfile.USTAR_FORMAT) as archive:
                        for name,path,digest in stream_members(directory,blob_root,manifest,manifest_sha):
                            if not valid_member(name) or path.is_symlink():raise ValueError('invalid_backup_file')
                            with regular_source(path) as source:
                                info=os.fstat(source.fileno())
                                if not stat.S_ISREG(info.st_mode):raise ValueError('invalid_backup_file')
                                member=tarfile.TarInfo(name);member.size=info.st_size;member.mode=0o600
                                checked=CheckedReader(source,digest);archive.addfile(member,checked);checked.finish()
                except BaseException as exc:errors.append(exc)
                finally:process.stdin.close()
            thread=threading.Thread(target=feed,name='backup-tar-stream',daemon=True);thread.start()
            sha=hashlib.sha256();size=0
            while chunk:=process.stdout.read(1024**2):
                output.write(chunk);sha.update(chunk);size+=len(chunk)
            output.flush();process.stdout.close()
            if process.wait(timeout=60):raise RuntimeError('backup_encryption_failed')
            thread.join(timeout=10)
            if thread.is_alive():raise RuntimeError('backup_encryption_failed')
            if errors:raise errors[0]
            return {'bytes':size,'sha256':sha.hexdigest()}
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)
        if thread is not None:thread.join(timeout=10)


def valid_member(name):
    return name in ('manifest.json','database.dump') or bool(re.fullmatch(r'(files|recovery)/[a-f0-9]{64}',name))


def snapshot_profile(directory):
    """None for messaging (its manifests carry no profile), 'calendar' for ANYM-605 bundles."""
    from .recovery_state import read_manifest
    manifest=read_manifest(directory)
    profile=manifest.get('profile') if isinstance(manifest,dict) else None
    if profile not in (None,'calendar'):raise ValueError('unsupported_backup')
    return profile


def encrypt_snapshot(directory,output,recipient,age='age',*,consume_staged_blobs=False):
    """Encrypt a verified bundle, optionally releasing exclusively owned staging.

    Only the backup worker opts in: its blob copies are disposable after tar has
    read them. A failed encoder requires a fresh snapshot, not a retry of this
    consumed directory. Manual snapshots are preserved by default.
    """
    from .backup import verify_bundle
    directory=Path(directory); output=Path(output)
    if type(consume_staged_blobs) is not bool: raise ValueError('invalid_staging_consumption')
    calendar=snapshot_profile(directory)=='calendar'
    if calendar:
        # A calendar bundle stages nothing it may consume; state/ members only under its profile.
        from .backup_calendar import valid_calendar_member,verify_calendar_bundle
        if consume_staged_blobs: raise ValueError('invalid_staging_consumption')
        manifest=verify_calendar_bundle(directory)
    else:verify_bundle(directory)
    if output.exists(): raise ValueError('encrypted_backup_exists')
    if consume_staged_blobs and any(p.stat().st_nlink!=1 for p in (directory/'files').iterdir()):
        raise ValueError('exclusive_staged_blobs_required')
    output.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.encrypt-',dir=output.parent); temporary=Path(name)
    process=None
    try:
        with os.fdopen(fd,'wb') as encrypted, tempfile.TemporaryFile() as errors:
            process=subprocess.Popen([age,'-r',recipient],stdin=subprocess.PIPE,stdout=encrypted,stderr=errors)
            try:
                with tarfile.open(fileobj=process.stdin,mode='w|') as archive:
                    if calendar:
                        paths=[directory/'manifest.json',directory/'database.dump',
                               *(directory/'state'/name for name in sorted(manifest['state_files']))]
                    else:
                        paths=[directory/'manifest.json',directory/'database.dump',*sorted((directory/'files').iterdir())]
                        if (directory/'recovery').exists():paths+=sorted((directory/'recovery').iterdir())
                    for path in paths:
                        if path.is_symlink() or not path.is_file(): raise ValueError('invalid_backup_file')
                        relative=path.relative_to(directory).as_posix()
                        if not (valid_calendar_member(relative) if calendar else valid_member(relative)):
                            raise ValueError('invalid_backup_file')
                        with regular_source(path) as source:
                            member=tarfile.TarInfo(relative)
                            member.size=os.fstat(source.fileno()).st_size
                            member.mode=0o600
                            archive.addfile(member,source)
                        if consume_staged_blobs and relative.startswith('files/'):
                            # archive.add has read all bytes; bounded pipe/tar
                            # buffers let ciphertext reuse this staged space.
                            # Provider originals are a separate read-only mount.
                            path.unlink()
                if consume_staged_blobs: fsync_dir(directory/'files')
            finally: process.stdin.close()
            if process.wait(timeout=60): raise RuntimeError('backup_encryption_failed')
            encrypted.flush(); os.fsync(encrypted.fileno())
        os.link(temporary,output); fsync_dir(output.parent)
        return output
    finally:
        if process is not None and process.poll() is None:
            process.terminate(); process.wait(timeout=10)
        temporary.unlink(missing_ok=True)


class ExtractedBundle(type(Path())):
    """The published bundle path plus {member: (sha256, bytes)} hashed while extracting it (ANYM-630).
    Paths joined to it are plain paths: only the bundle root carries the digests."""
    extracted=None
    def with_segments(self,*segments):return Path(*segments)


def decrypt_snapshot(encrypted,identity,destination,age='age',max_bytes=20*1024**3):
    return _decrypt_snapshot([age,'-d','-i',str(identity),str(encrypted)],destination,max_bytes)


def decrypt_snapshot_stream(source,identity,destination,age='age',max_bytes=20*1024**3):
    """Bounded stdin decryption, published after EOF and authentication.

    The isolated worker owns stdin. Its coordinator enforces a whole-job deadline
    and kills the owned process on stalled input. No ciphertext is staged.
    """
    return _decrypt_snapshot([age,'-d','-i',str(identity)],destination,max_bytes,source=source,cached=True)


def _decrypt_snapshot(command,destination,max_bytes,*,source=None,cached=False):
    # cached: only the isolated helper, which reads the dump back before pg_restore and every original after it
    # (backup_restore_worker.run); a standalone decryption reads the whole bundle back before publishing it.
    destination=Path(destination)
    if destination.exists(): raise ValueError('restore_destination_exists')
    destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    temporary=Path(tempfile.mkdtemp(prefix='.decrypt-',dir=destination.parent)); process=None;thread=None;failures=[]
    try:
        with tempfile.TemporaryFile() as errors:
            process=subprocess.Popen(command,stdin=subprocess.PIPE if source is not None else None,stdout=subprocess.PIPE,stderr=errors)
            if source is not None:
                def feed():
                    try:
                        while chunk:=source.read(1024**2):process.stdin.write(chunk)
                    except BaseException as exc:failures.append(exc)
                    finally:
                        try:process.stdin.close()
                        except BrokenPipeError:pass
                thread=threading.Thread(target=feed,name='backup-decrypt-input',daemon=True);thread.start()
            # Digests of the bytes this process wrote, kept in memory only: the bundle can never supply them.
            total=0; seen=set(); profile=None; extracted={}
            from .backup_calendar import valid_calendar_member
            with tarfile.open(fileobj=process.stdout,mode='r|') as archive:
                for member in archive:
                    name=member.name
                    # A calendar manifest, read from the first member, admits only calendar members.
                    if (not member.isfile() or name in seen or len(seen)>=1000000 or
                        not (valid_calendar_member(name) if profile=='calendar' else valid_member(name))):
                        raise ValueError('invalid_backup_member')
                    total+=member.size
                    if total>max_bytes or shutil.disk_usage(temporary).free-member.size<1024**3:
                        raise ValueError('restore_size_limit')
                    seen.add(name); target=temporary/name
                    for parent in reversed(Path(name).parents[:-1]):(temporary/parent).mkdir(mode=0o700,exist_ok=True)
                    member_source=archive.extractfile(member)
                    fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600); sha=hashlib.sha256(); size=0
                    with member_source,os.fdopen(fd,'wb') as out:
                        while chunk:=member_source.read(1024**2):sha.update(chunk); out.write(chunk); size+=len(chunk)
                        out.flush(); os.fsync(out.fileno())
                    extracted[name]=(sha.hexdigest(),size)
                    if name=='manifest.json' and len(seen)==1:
                        try:profile=snapshot_profile(temporary)
                        except ValueError:raise ValueError('invalid_backup_member') from None
            # Drain authenticated trailing bytes before accepting the decryption result.
            while trailing:=process.stdout.read(1024**2):
                total+=len(trailing)
                if total>max_bytes: raise ValueError('restore_size_limit')
            process.stdout.close()
            if process.wait(timeout=60): raise RuntimeError('backup_decryption_failed')
            if thread is not None:
                thread.join(timeout=10)
                if thread.is_alive():raise RuntimeError('backup_decryption_failed')
                if failures:raise failures[0]
            if not {'manifest.json','database.dump'}<=seen: raise ValueError('incomplete_backup')
            # A calendar manifest anywhere but first never admitted calendar rules: refuse it.
            if snapshot_profile(temporary)!=profile: raise ValueError('invalid_backup_member')
        from .recovery_state import read_manifest
        from .backup import verify_bundle
        if profile=='calendar':
            from .backup_calendar import verify_calendar_bundle
            verify_calendar_bundle(temporary)
            for folder in ('state/vault','state/profile','state'):
                if (temporary/folder).exists():fsync_dir(temporary/folder)
            fsync_dir(temporary); os.rename(temporary,destination); fsync_dir(destination.parent)
            return destination
        manifest=read_manifest(temporary)
        if manifest.get('format')==2:(temporary/'recovery').mkdir(mode=0o700,exist_ok=True)
        (temporary/'files').mkdir(mode=0o700,exist_ok=True)
        if cached:verify_bundle(temporary,extracted=extracted,read_dump=False)
        else:verify_bundle(temporary)
        if (temporary/'recovery').exists():fsync_dir(temporary/'recovery')
        fsync_dir(temporary/'files'); fsync_dir(temporary)
        os.rename(temporary,destination); fsync_dir(destination.parent)
        if not cached:return destination
        published=ExtractedBundle(destination); published.extracted=extracted
        return published
    finally:
        if process is not None and process.poll() is None:
            process.terminate(); process.wait(timeout=10)
        if thread is not None:thread.join(timeout=10)
        if temporary.exists(): shutil.rmtree(temporary)
