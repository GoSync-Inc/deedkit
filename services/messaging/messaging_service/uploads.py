"""Bounded private staged uploads; no provider actions or artificial messages.

Only immutable completed bytes can become a command source. Files are separate
from archive CAS, so expiring an upload cannot delete a shared archive original.
Database row locks serialize chunks; a rolled-back append is truncated on retry.
"""
import base64
import binascii
import hashlib
import os
import re
import shutil
import stat

from .blobs import Blob, BlobWriter
from .policy import targets
from .storage_admission import StorageAdmission, STREAM_WORKSPACE_BYTES, restore_reserved, volume_key
from .store import APIError, serial

MAX_BYTES=32*1024**2
CHUNK_BYTES=262144
UPLOAD_BODY_BYTES=360448
RESERVE_BYTES=1024**3
REQUIRED=frozenset(('read','send','upload'))


def _permissions(p):
    if REQUIRED-set(p['permissions']):raise APIError('forbidden',403)


def _root(store, upload_id=None, *, create=False):
    root=store.files/'.uploads'
    if create:root.mkdir(mode=0o700,exist_ok=True)
    if root.is_symlink() or (root.exists() and (not root.is_dir() or root.resolve()!=root)):
        raise APIError('attachment_unavailable',409)
    if upload_id is not None:
        if not isinstance(upload_id,str) or not re.fullmatch(r'up_[a-f0-9]{32}',upload_id):
            raise APIError('not_found',404)
        root=root/upload_id
        if create:root.mkdir(mode=0o700,exist_ok=True)
        if root.is_symlink() or (root.exists() and (not root.is_dir() or root.resolve()!=root)):
            raise APIError('attachment_unavailable',409)
    return root


def _pinned(c, upload_id):
    # Status reads project expiration without rewriting the durable row. A
    # stopped collector must not retain its expired queue (or lost lease) forever.
    return c.execute("""SELECT 1 FROM messenger_commands m WHERE m.expires_at>clock_timestamp()
        AND (m.status='queued' OR (m.status='dispatching' AND EXISTS(
            SELECT 1 FROM collector_leases l WHERE l.account=m.account
                AND l.epoch=m.lease_epoch AND l.expires_at>clock_timestamp())))
        AND m.operation='send_file' AND m.payload->'parameters'->>'upload_id'=%s LIMIT 1""",
        (upload_id,)).fetchone() is not None


def _lifecycle_lock(c, upload_id):
    """Fence one upload's bytes until transaction commit, without capacity locks."""
    if not isinstance(upload_id,str) or not re.fullmatch(r'up_[a-f0-9]{32}',upload_id):
        raise APIError('not_found',404)
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('file-upload:'+upload_id,))


def cleanup(store,c):
    """Bounded opportunistic reaping, serialized with admission and chunk writes."""
    StorageAdmission.lock(c)
    candidates=c.execute("""SELECT id FROM file_uploads WHERE state<>'expired'
        AND expires_at<=clock_timestamp() ORDER BY expires_at,id LIMIT 64""").fetchall()
    for candidate in candidates:
        # Match chunk's capacity -> lifecycle -> row order. A source holds only
        # this upload's lifecycle lock; it never waits for capacity admission.
        _lifecycle_lock(c,candidate['id'])
        row=c.execute("""SELECT * FROM file_uploads WHERE id=%s AND state<>'expired'
            AND expires_at<=clock_timestamp() FOR UPDATE""",(candidate['id'],)).fetchone()
        if not row:continue
        if _pinned(c,row['id']):continue
        root=_root(store,row['id'])
        if root.exists():
            # No recursive deletion or user-controlled names. Never visit CAS.
            for name in ('.partial',row['sha256']):
                path=root/name
                if path.exists() or path.is_symlink():
                    if path.is_symlink() or not path.is_file():raise APIError('attachment_unavailable',409)
                    path.unlink()
            # A process can die during atomic publication. Reap only the
            # BlobWriter's strict temporary namespace, under this expired upload.
            with os.scandir(root) as entries:
                for index,entry in enumerate(entries):
                    if index>=64:break
                    if re.fullmatch(r'\.pending-[a-f0-9]{32}',entry.name):
                        if not entry.is_file(follow_symlinks=False):raise APIError('attachment_unavailable',409)
                        os.unlink(entry.path)
            try:root.rmdir()
            except OSError:continue  # Keep capacity reserved until bounded reaping finishes.
        c.execute("UPDATE file_uploads SET state='expired' WHERE id=%s",(row['id'],))


def _target(c,p,account,chat_id):
    from .commands import _account,_chat,_policy
    _permissions(p)
    _account(c,p,account)
    chat=_chat(c,p,account,chat_id)
    policy=c.execute('SELECT send_enabled FROM account_policies WHERE account=%s',(account,)).fetchone()
    if not policy or not policy['send_enabled']:raise APIError('sending_disabled',409)
    _policy(c,account,'send_file',[chat['provider_chat']])
    targets(p,[chat_id])


def _public(row,c):
    expired=c.execute('SELECT %s<=clock_timestamp() AS expired',(row['expires_at'],)).fetchone()['expired']
    return serial({'upload_id':row['id'],'account':row['account'],'chat_id':row['chat'],
        'filename':row['filename'],'mime':row['mime'],'bytes':row['bytes'],'sha256':row['sha256'],
        'offset':row['received'],'status':'expired' if expired or row['state']=='expired' else row['state'],
        'chunk_bytes':CHUNK_BYTES,'expires_at':row['expires_at'],
        'changes_provider_state':False,'owner_approval_verified_by_server':False})


def prepare(store,c,p,account,chat_id,filename,mime,bytes,sha256,request_id):
    _target(c,p,account,chat_id)
    if isinstance(mime,str):mime=mime.lower()
    if (not isinstance(filename,str) or not 1<=len(filename)<=255 or filename in ('.','..') or
        any(ord(ch)<32 or ord(ch)==127 or 0xD800<=ord(ch)<=0xDFFF or ch in '/\\' for ch in filename) or
        not isinstance(mime,str) or not re.fullmatch(r'[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+',mime) or len(mime)>128 or
        type(bytes) is not int or not 1<=bytes<=MAX_BYTES or
        not isinstance(sha256,str) or not re.fullmatch(r'[a-f0-9]{64}',sha256) or
        not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,128}',request_id)):
        raise APIError('invalid_upload')
    cleanup(store,c)
    existing=c.execute('SELECT * FROM file_uploads WHERE key_id=%s AND request_id=%s FOR UPDATE',
        (p['key_id'],request_id)).fetchone()
    if existing:
        if (existing['account'],existing['chat'],existing['filename'],existing['mime'],existing['bytes'],existing['sha256'])!=(account,chat_id,filename,mime,bytes,sha256):
            raise APIError('idempotency_conflict',409)
        return _public(existing,c)
    root=_root(store,create=True)
    volume=volume_key(root)
    active=c.execute("""SELECT count(*) AS total,count(*) FILTER(WHERE key_id=%s) AS own,
        coalesce(sum(2*bytes+%s) FILTER(WHERE volume_key=%s),0) AS reserved
        FROM file_uploads WHERE state<>'expired'""",
        (p['key_id'],STREAM_WORKSPACE_BYTES,volume)).fetchone()
    if active['total']>=32 or active['own']>=4:raise APIError('upload_limit',409)
    media=c.execute('SELECT coalesce(sum(reserved_bytes),0) AS bytes FROM media_reservations WHERE volume_key=%s AND expires_at>clock_timestamp()',
        (volume,)).fetchone()['bytes']+restore_reserved(c)
    if shutil.disk_usage(root).free-media-active['reserved']-2*bytes-STREAM_WORKSPACE_BYTES<RESERVE_BYTES:
        raise APIError('upload_capacity',409)
    row=c.execute('''INSERT INTO file_uploads(principal,key_id,account,chat,filename,mime,bytes,sha256,volume_key,request_id)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
        (p['id'],p['key_id'],account,chat_id,filename,mime,bytes,sha256,volume,request_id)).fetchone()
    return _public(row,c)


def owned(c,p,upload_id,*,lock=False):
    _permissions(p)
    if not isinstance(upload_id,str) or not re.fullmatch(r'up_[a-f0-9]{32}',upload_id):raise APIError('not_found',404)
    row=c.execute('SELECT * FROM file_uploads WHERE id=%s AND principal=%s AND key_id=%s'+(' FOR UPDATE' if lock else ''),
        (upload_id,p['id'],p['key_id'])).fetchone()
    if not row:raise APIError('not_found',404)
    from .commands import _account,_chat
    _account(c,p,row['account']);_chat(c,p,row['account'],row['chat'])
    targets(p,[row['id'],row['chat']])
    return row


def status(store,c,p,upload_id):
    return _public(owned(c,p,upload_id),c)


def chunk(store,c,p,upload_id,offset,data_base64):
    # Serialize in the same order as admission/reaping; never grow an unknown file.
    StorageAdmission.lock(c)
    _lifecycle_lock(c,upload_id)
    row=owned(c,p,upload_id,lock=True)
    _target(c,p,row['account'],row['chat'])
    if _public(row,c)['status']=='expired':raise APIError('upload_expired',409)
    if (type(offset) is not int or offset<0 or not isinstance(data_base64,str) or
        not 1<=len(data_base64)<=4*((CHUNK_BYTES+2)//3)):
        raise APIError('invalid_upload_chunk')
    try:data=base64.b64decode(data_base64,validate=True)
    except (ValueError,binascii.Error):raise APIError('invalid_upload_chunk') from None
    if not 1<=len(data)<=CHUNK_BYTES or base64.b64encode(data).decode()!=data_base64:
        raise APIError('invalid_upload_chunk')
    if offset>row['received'] or offset+len(data)>row['bytes']:
        raise APIError('upload_offset_conflict',409)
    root=_root(store,upload_id,create=True)
    complete=root/row['sha256'];partial=root/'.partial'
    # Publication can precede a rolled-back/lost DB commit. A complete verified
    # original can recover its row without another provider effect or rewrite.
    if row['state']!='ready' and complete.exists():
        try:BlobWriter(root,max_bytes=MAX_BYTES,reserve_bytes=0).verify(Blob(row['sha256'],row['bytes']))
        except (OSError,RuntimeError,ValueError):raise APIError('attachment_unavailable',409) from None
        fd=os.open(complete,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as original:
            if not stat.S_ISREG(os.fstat(original.fileno()).st_mode):raise APIError('attachment_unavailable',409)
            original.seek(offset)
            if original.read(len(data))!=data:raise APIError('upload_offset_conflict',409)
        row=c.execute("UPDATE file_uploads SET received=bytes,state='ready' WHERE id=%s RETURNING *",(upload_id,)).fetchone()
        partial.unlink(missing_ok=True)
        return _public(row,c)
    path=complete if row['state']=='ready' else partial
    flags=os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK
    if row['state']!='ready':flags|=os.O_CREAT
    try:
        fd=os.open(path,flags,0o600)
        with os.fdopen(fd,'r+b') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size<row['received']:
                raise APIError('attachment_unavailable',409)
            if offset<row['received'] or row['state']=='ready':
                if offset+len(data)>row['received']:raise APIError('upload_offset_conflict',409)
                stream.seek(offset)
                if stream.read(len(data))!=data:raise APIError('upload_offset_conflict',409)
                return _public(row,c)
            stream.truncate(row['received']);stream.seek(row['received'])
            if shutil.disk_usage(root).free-len(data)<RESERVE_BYTES:raise APIError('upload_capacity',409)
            stream.write(data);stream.flush();os.fsync(stream.fileno())
            received=row['received']+len(data)
            if received==row['bytes']:
                stream.seek(0)
                if hashlib.file_digest(stream,'sha256').hexdigest()!=row['sha256']:
                    # Failed content is not finalized; caller can retry this last
                    # offset with corrected bytes without allocating a new upload.
                    stream.truncate(row['received'])
                    raise APIError('upload_hash_mismatch',409)
                stream.seek(0)
                writer=BlobWriter(root,max_bytes=MAX_BYTES,reserve_bytes=RESERVE_BYTES)
                writer.write(iter(lambda:stream.read(1024**2),b''),expected_bytes=received,expected_sha256=row['sha256'])
            row=c.execute('UPDATE file_uploads SET received=%s,state=%s WHERE id=%s RETURNING *',
                (received,'ready' if received==row['bytes'] else 'uploading',upload_id)).fetchone()
        if row['state']=='ready':partial.unlink(missing_ok=True)
    except OSError:raise APIError('attachment_unavailable',409) from None
    return _public(row,c)


def source(store,c,p,account,chat_id,parameters,*,verify=False,retained=False):
    # Keep the lifecycle fence until the caller commits its command. Otherwise
    # TTL reaping can unlink a verified file before that command starts pinning it.
    # Dispatch already holds the collector fence. Taking capacity admission here
    # would invert media admission's capacity -> collector order. Chunks/reaping
    # fence the same upload before its row, without acquiring collector fences.
    _lifecycle_lock(c,parameters['upload_id'])
    row=owned(c,p,parameters['upload_id'])
    if row['account']!=account or row['chat']!=chat_id:raise APIError('not_found',404)
    if row['sha256']!=parameters['sha256']:raise APIError('operation_target_changed',409)
    if row['state']!='ready' or (_public(row,c)['status']=='expired' and not (retained and _pinned(c,row['id']))):
        raise APIError('attachment_unavailable',409)
    _target(c,p,account,chat_id)
    root=_root(store,row['id'])
    try:
        path=root/row['sha256']
        if path.is_symlink() or not path.is_file() or path.stat().st_size!=row['bytes']:
            raise APIError('attachment_unavailable',409)
        if verify:BlobWriter(root,max_bytes=MAX_BYTES,reserve_bytes=0).verify(Blob(row['sha256'],row['bytes']))
    except (OSError,RuntimeError,ValueError):raise APIError('attachment_unavailable',409) from None
    return row
