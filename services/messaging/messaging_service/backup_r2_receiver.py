"""Opt-in R2 ciphertext receiver behind an upload-only SSH forced command.

Only this module's explicit local operator commands download or rotate objects.
The producer still speaks stream_v2 and never receives object-store credentials.
No boto3 import, ambient AWS credential lookup or network call occurs on import.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import select
import signal
import stat
import struct
import sys
import time

from . import backup_receiver as local

AGE_HEADER = local.AGE_HEADER
MIB = 1024 ** 2
# A ranged GET resumes a body read that broke mid-stream (R2 resets, read
# timeouts) at most this many times per object, with bounded backoff.
STREAM_RESUMES = 5
# Codes our own SIGALRM/SIGTERM handlers raise; an SDK may wrap them.
SIGNAL_CODES = {'receiver_timeout', 'restore_deadline', 'restore_stopped'}
# The deadline/stop our signal handlers raised. botocore's retry loop can swallow it and
# return a later attempt's success, so its retry hook and every cloud call re-raise it.
CANCELLED = []
CHECKS = {'decryption', 'bundle_integrity', 'database_restore', 'table_inventory',
          'original_hashes', 'recovery_objects', 'collectors_quarantined'}


def unique_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_json_field')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError('invalid_json') from None


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


@contextmanager
def directory_handle(path, *, trusted=False):
    """Anchor every path component with openat; a rename cannot redirect reads."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('invalid_operator_path')
    if trusted:
        with local.trusted_directory(path) as fd:
            yield fd
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd); fd = child
        yield fd
    finally:
        os.close(fd)


def private_json(path, *, limit=16384):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('private_config_required')
    # Root maintenance refuses writable/unowned ancestors, even when a leaf
    # happens to be root-owned. Hold its trusted parent throughout the read.
    with directory_handle(path.parent, trusted=os.geteuid() == 0) as parent_fd:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077 or
                    info.st_uid not in {0, os.geteuid()} or info.st_size > limit):
                raise ValueError('private_config_required')
            raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('private_config_required')
    return unique_json(raw)


def credentials(config):
    value = private_json(config['credentials_file'], limit=4096)
    if (not isinstance(value, dict) or set(value) != {'access_key_id', 'secret_access_key'} or
            not isinstance(value['access_key_id'], str) or not re.fullmatch('[a-f0-9]{32}', value['access_key_id']) or
            not isinstance(value['secret_access_key'], str) or not re.fullmatch('[a-f0-9]{64}', value['secret_access_key'])):
        raise ValueError('invalid_credentials')
    return value


def load_config(path):
    value = private_json(path)
    required = {'v', 'endpoint', 'bucket', 'prefix', 'state_directory', 'credentials_file'}
    defaults = {'max_bytes': 64 * 1024 ** 3, 'quota_bytes': 128 * 1024 ** 3,
                'max_objects': 2, 'part_bytes': 64 * MIB, 'timeout_seconds': 7200,
                'cold_max_bytes': 5 * 1024 ** 3}
    # Optional pair: the root verifier's helper publication and the one verifier
    # config (the installed service's) whose publication `capabilities` accepts.
    published = {'capabilities_file', 'verifier_config_file'}
    if (not isinstance(value, dict) or not required <= set(value) or
            set(value) - required - set(defaults) - published or len(published & set(value)) == 1 or
            type(value['v']) is not int or value['v'] != 1):
        raise ValueError('invalid_config')
    value = {**defaults, **value}
    if (not isinstance(value['endpoint'], str) or
            not re.fullmatch(r'https://[a-f0-9]{32}(?:\.eu)?\.r2\.cloudflarestorage\.com', value['endpoint']) or
            not isinstance(value['bucket'], str) or not re.fullmatch('[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', value['bucket']) or
            not isinstance(value['prefix'], str) or len(value['prefix']) > 128 or
            not re.fullmatch(r'[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*', value['prefix'])):
        raise ValueError('invalid_destination')
    if (any(type(value[key]) is not int for key in defaults) or value['max_objects'] != 2 or
            not 5 * MIB <= value['part_bytes'] <= 64 * MIB or
            not 64 <= value['max_bytes'] <= min(5 * 1024 ** 4, value['part_bytes'] * 10000) or
            not value['max_bytes'] <= value['quota_bytes'] <= 10 * 1024 ** 4 or
            not 60 <= value['timeout_seconds'] <= 21600 or
            not 64 <= value['cold_max_bytes'] <= min(5 * 1024 ** 4, value['part_bytes'] * 10000)):
        raise ValueError('invalid_limits')
    for key in ('state_directory', 'credentials_file', *published):
        if key in value and (not isinstance(value[key], str) or not Path(value[key]).is_absolute() or
                             '..' in Path(value[key]).parts):
            raise ValueError('invalid_config_path')
    state = Path(value['state_directory'])
    # The upload UID owns its state; a publication it could write is no root answer.
    if any(key in value and Path(value[key]).is_relative_to(state) for key in published):
        raise ValueError('invalid_config_path')
    if any(p.is_symlink() for p in (state, *state.parents)) or not state.is_dir() or state.stat().st_mode & 0o077:
        raise ValueError('private_receiver_directory_required')
    credentials(value)
    return value


def make_client(config):
    # The official SDK is an explicit receiver-only optional installation. Supply
    # credentials to Session itself so no default chain/metadata resolution runs.
    import boto3
    from botocore.config import Config
    secret = credentials(config)
    session = boto3.session.Session(aws_access_key_id=secret['access_key_id'],
                                   aws_secret_access_key=secret['secret_access_key'], region_name='auto')
    client = session.client('s3', endpoint_url=config['endpoint'], config=Config(
        signature_version='s3v4', connect_timeout=10, read_timeout=120,
        retries={'mode': 'standard', 'total_max_attempts': 3},
        request_checksum_calculation='when_required', response_checksum_validation='when_required',
        s3={'addressing_style': 'path'}))
    client.meta.events.register_first('needs-retry.s3', cancelled)
    return client


def cancel(error):
    """Signal handlers raise through here, so a deadline/stop the SDK swallowed is still seen."""
    CANCELLED[:] = [error]
    raise error


def cancelled(**_):
    """botocore `needs-retry` hook (first) and post-call check: never retry past a deadline/stop."""
    if CANCELLED:
        raise CANCELLED[0]


def destination(config):
    return {key: config[key] for key in ('endpoint', 'bucket', 'prefix')}


def code(exc):
    response = getattr(exc, 'response', None)
    if isinstance(response, dict):
        return str(response.get('Error', {}).get('Code', ''))
    return ''


def absent(exc):
    return code(exc) in {'404', 'NoSuchKey', 'NotFound'}


def cloud_call(method, **kwargs):
    try:
        response = method(**kwargs)
    except Exception:
        cancelled()
        # SDK exceptions can contain signed URLs, request headers and diagnostics.
        raise RuntimeError('storage_unavailable') from None
    cancelled()
    return response


def receipt_key(config, bid):
    return config['prefix'] + '/receipts/' + bid + '.json'


def valid_id(bid):
    if not isinstance(bid, str) or not local.BACKUP_ID.fullmatch(bid):
        raise ValueError('invalid_backup_id')


def validate_receipt(config, value, bid):
    valid_id(bid)
    if (not isinstance(value, dict) or set(value) != {'v', 'destination', 'object_key', 'wire'} or
            type(value['v']) is not int or value['v'] != 1 or value['destination'] != destination(config) or
            not isinstance(value['object_key'], str) or
            not re.fullmatch(re.escape(config['prefix'] + '/objects/' + bid + '-') + r'[a-f0-9]{32}\.tar\.age', value['object_key'])):
        raise ValueError('invalid_cloud_receipt')
    wire = value['wire']
    if (not isinstance(wire, dict) or set(wire) != {'v', 'status', 'id', 'bytes', 'sha256', 'snapshot_sha256', 'duplicate', 'received_at'} or
            type(wire['v']) is not int or wire['v'] != 2 or wire['status'] != 'stored' or wire['id'] != bid or
            type(wire['bytes']) is not int or not 64 <= wire['bytes'] <= config['max_bytes'] or
            not isinstance(wire['sha256'], str) or not local.DIGEST.fullmatch(wire['sha256']) or
            not isinstance(wire['snapshot_sha256'], str) or not local.DIGEST.fullmatch(wire['snapshot_sha256']) or
            wire['duplicate'] is not False):
        raise ValueError('invalid_cloud_receipt')
    try:
        created = datetime.fromisoformat(wire['received_at'])
        if created.utcoffset() is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('invalid_cloud_receipt') from None
    return value


def get_receipt(config, client, bid):
    try:
        response = client.get_object(Bucket=config['bucket'], Key=receipt_key(config, bid))
    except Exception as exc:
        if absent(exc):
            return None
        raise RuntimeError('storage_unavailable') from None
    body = response['Body']
    try:
        raw = body.read(16385)
        if len(raw) > 16384:
            raise ValueError('invalid_cloud_receipt')
        value = unique_json(raw)
    finally:
        body.close()
    return validate_receipt(config, value, bid)


def signalled(exc):
    """Our deadline/stop error, even when urllib3/botocore wrapped it in a read error."""
    if CANCELLED:
        return CANCELLED[0]
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (TimeoutError, InterruptedError)) and exc.args[:1] and exc.args[0] in SIGNAL_CODES:
            return exc
        exc = exc.__cause__ or exc.__context__
    return None


def stream_pause(attempt):
    time.sleep(min(30, 2 ** attempt))


def resume_body(config, client, receipt, offset, etag):
    total = receipt['wire']['bytes']
    request = {'Bucket': config['bucket'], 'Key': receipt['object_key'], 'Range': 'bytes=%d-%d' % (offset, total - 1)}
    if etag:
        request['IfMatch'] = etag
    response = cloud_call(client.get_object, **request)
    if (response.get('ContentLength') != total - offset or
            response.get('ContentRange', 'bytes %d-%d/%d' % (offset, total - 1, total)) != 'bytes %d-%d/%d' % (offset, total - 1, total)):
        response['Body'].close()
        raise ValueError('stored_backup_corrupt')
    return response['Body']


def verify_object(config, client, receipt, output=None):
    """Hash actual downloaded bytes; multipart ETags are never integrity proof.

    A body read that fails mid-stream resumes at the next byte with a ranged GET
    of the same ETag (STREAM_RESUMES, bounded backoff); the full SHA256 still
    covers every delivered byte. A failed write to `output` is never retried.
    """
    wire = receipt['wire']
    response = cloud_call(client.get_object, Bucket=config['bucket'], Key=receipt['object_key'])
    body = response['Body']; etag = response.get('ETag')
    digest = hashlib.sha256(); size = 0; prefix = b''; resumes = 0
    try:
        if response.get('ContentLength') != wire['bytes']:
            raise ValueError('stored_backup_corrupt')
        while True:
            try:
                data = body.read(MIB)
            except Exception as exc:
                if (stop := signalled(exc)) is not None:
                    raise stop
                if size == wire['bytes']:
                    break
                try:
                    body.close()
                except Exception as closing:
                    if (stop := signalled(closing)) is not None:
                        raise stop
                while True:
                    resumes += 1
                    if resumes > STREAM_RESUMES:
                        # SDK errors can hold signed URLs; only their class reaches diagnostics.
                        raise RuntimeError('storage_unavailable') from None
                    stream_pause(resumes)
                    try:
                        body = resume_body(config, client, receipt, size, etag)
                        break
                    except RuntimeError as again:
                        if (stop := signalled(again)) is not None:
                            raise stop
                continue
            if not data:
                break
            size += len(data)
            if size > wire['bytes']:
                raise ValueError('stored_backup_corrupt')
            digest.update(data)
            if len(prefix) < len(AGE_HEADER):
                prefix += data[:len(AGE_HEADER) - len(prefix)]
            if output is not None:
                output.write(data)
    finally:
        body.close()
    if size != wire['bytes'] or digest.hexdigest() != wire['sha256'] or prefix != AGE_HEADER:
        raise ValueError('stored_backup_corrupt')


def state_read(directory, name):
    try:
        _, value = local.maintenance_json(name, dir_fd=directory)
    except FileNotFoundError:
        return None
    return value


def state_write(directory, name, value):
    local.write_maintenance_json(name, value, dir_fd=directory)


@contextmanager
def shared_lock(state_fd):
    """Root must never create an uploader-inaccessible lock in shared state."""
    owner = os.fstat(state_fd).st_uid
    root_in_upload_state = os.geteuid() == 0 and owner != 0
    flags = os.O_RDWR | os.O_NOFOLLOW
    if not root_in_upload_state:
        flags |= os.O_CREAT
    try:
        lock_fd = os.open('.receiver.lock', flags, 0o600, dir_fd=state_fd)
    except FileNotFoundError:
        if root_in_upload_state:
            raise ValueError('uploader_lock_initialization_required') from None
        raise
    try:
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError('invalid_receiver_lock')
        if info.st_uid != owner:
            error = 'uploader_lock_initialization_required' if root_in_upload_state else 'invalid_receiver_lock'
            raise ValueError(error)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('receiver_busy') from None
        yield
    finally:
        os.close(lock_fd)


@contextmanager
def state_lock(config):
    state = Path(config['state_directory'])
    with directory_handle(state) as fd:
        info = os.fstat(fd)
        if info.st_mode & 0o077 or (os.geteuid() != 0 and info.st_uid not in {0, os.geteuid()}):
            raise ValueError('private_receiver_directory_required')
        with shared_lock(fd):
            yield fd


@contextmanager
def operator_handles(state, marker, proof):
    # Preserve the existing trusted root ancestry/openat evidence boundary while
    # using the same ownership-safe lock as receive/download. The state leaf may
    # belong to the upload account; root controls and proofs must remain separate.
    with local.trusted_directory(state, archive=True) as state_fd, \
         local.trusted_directory(marker.parent) as control_fd, \
         local.trusted_directory(proof.parent) as proof_fd:
        identity = lambda fd: (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
        if identity(state_fd) in {identity(control_fd), identity(proof_fd)}:
            raise ValueError('separate_operator_evidence_required')
        with shared_lock(state_fd):
            yield state_fd, control_fd, proof_fd


def attempt_name(bid):
    return bid + '.attempt.json'


def validate_attempt(config, attempt, bid):
    if (not isinstance(attempt, dict) or set(attempt) != {'v', 'destination', 'header', 'key', 'phase', 'upload_id', 'receipt'} or
            type(attempt['v']) is not int or attempt['v'] != 1 or attempt['destination'] != destination(config) or
            attempt['phase'] not in {'creating', 'uploading', 'completing', 'publishing', 'committed'} or
            not isinstance(attempt['key'], str) or
            not re.fullmatch(re.escape(config['prefix'] + '/objects/' + bid + '-') + r'[a-f0-9]{32}\.tar\.age', attempt['key'])):
        raise ValueError('invalid_attempt_journal')
    header = attempt['header']
    validate_header(header, config['max_bytes'])
    if header['id'] != bid:
        raise ValueError('invalid_attempt_journal')
    if attempt['phase'] != 'creating' and (not isinstance(attempt['upload_id'], str) or not 1 <= len(attempt['upload_id']) <= 1024):
        raise ValueError('invalid_attempt_journal')
    if attempt['phase'] in {'completing', 'publishing', 'committed'}:
        validate_receipt(config, attempt['receipt'], bid)
        if attempt['receipt']['object_key'] != attempt['key'] or attempt['receipt']['wire']['snapshot_sha256'] != header['snapshot_sha256']:
            raise ValueError('invalid_attempt_journal')
    return attempt


def validate_header(header, max_bytes):
    if (not isinstance(header, dict) or set(header) != {'v', 'id', 'max_bytes', 'snapshot_sha256'} or
            type(header['v']) is not int or header['v'] != 2 or
            not isinstance(header['id'], str) or not local.BACKUP_ID.fullmatch(header['id']) or
            type(header['max_bytes']) is not int or not 64 <= header['max_bytes'] <= max_bytes or
            not isinstance(header['snapshot_sha256'], str) or not local.DIGEST.fullmatch(header['snapshot_sha256'])):
        raise ValueError('invalid_header')
    return header


def read_header(config, source):
    raw = source.readline(4097)
    if len(raw) > 4096 or not raw.endswith(b'\n'):
        raise ValueError('invalid_header')
    return validate_header(unique_json(raw), config['max_bytes'])


def inventory(config, client):
    """Bound the whole configured physical prefix, including orphan objects/MPUs."""
    objects = {}; token = None
    for _ in range(16):
        args = {'Bucket': config['bucket'], 'Prefix': config['prefix'] + '/objects/', 'MaxKeys': 1000}
        if token:
            args['ContinuationToken'] = token
        page = cloud_call(client.list_objects_v2, **args)
        for item in page.get('Contents', []):
            key, size = item.get('Key'), item.get('Size')
            if not isinstance(key, str) or type(size) is not int or size < 0 or key in objects:
                raise ValueError('invalid_storage_inventory')
            objects[key] = size
        if not page.get('IsTruncated'):
            break
        token = page.get('NextContinuationToken')
        if not isinstance(token, str) or not token:
            raise ValueError('invalid_storage_inventory')
    else:
        raise RuntimeError('receiver_quota')
    # Any unfinished remote attempt, including a create whose response was lost,
    # blocks admission. We never guess that its eventual object cannot appear.
    page = cloud_call(client.list_multipart_uploads, Bucket=config['bucket'], Prefix=config['prefix'] + '/objects/', MaxUploads=1000)
    if page.get('Uploads') or page.get('IsTruncated'):
        raise RuntimeError('unresolved_upload_required')
    return objects


def publish(config, client, attempt, state_fd):
    receipt = attempt['receipt']; bid = attempt['header']['id']
    # Save the exact body before its first conditional publication; retrying this
    # same immutable body is safe even if the earlier response was lost.
    attempt['phase'] = 'publishing'
    state_write(state_fd, attempt_name(bid), attempt)
    try:
        client.put_object(Bucket=config['bucket'], Key=receipt_key(config, bid), Body=encoded(receipt),
                          ContentType='application/json', IfNoneMatch='*')
    except Exception as exc:
        if code(exc) not in {'412', 'PreconditionFailed'}:
            raise RuntimeError('receipt_outcome_unknown') from None
        winner = get_receipt(config, client, bid)
        if winner != receipt:
            raise ValueError('backup_id_conflict')
    attempt['phase'] = 'committed'
    state_write(state_fd, attempt_name(bid), attempt)
    return receipt


def reconcile(config, client, attempt, state_fd):
    """An absent HEAD after uncertain completion cannot authorize a new attempt."""
    bid = attempt['header']['id']
    if attempt['phase'] in {'creating', 'uploading'}:
        if attempt['phase'] == 'creating':
            raise RuntimeError('creation_outcome_unknown')
        # Durable `uploading` proves Complete was never attempted. Only this
        # exact known own MPU may be aborted. Its successful abort response can
        # be lost; an authoritative NoSuchUpload on retry (also after lifecycle
        # cleanup) confirms that this pre-completion resource is gone. Generic
        # 404s, auth/transport errors and every completing-state uncertainty keep
        # their evidence and cannot authorize a fresh physical attempt.
        try:
            client.abort_multipart_upload(Bucket=config['bucket'], Key=attempt['key'], UploadId=attempt['upload_id'])
        except Exception as exc:
            response = getattr(exc, 'response', {})
            if code(exc) != 'NoSuchUpload' or response.get('ResponseMetadata', {}).get('HTTPStatusCode') != 404:
                raise RuntimeError('storage_unavailable') from None
        os.unlink(attempt_name(bid), dir_fd=state_fd); local.fsync_dir(state_fd)
        raise RuntimeError('incomplete_upload_aborted_retry')
    try:
        head = client.head_object(Bucket=config['bucket'], Key=attempt['key'])
    except Exception as exc:
        if absent(exc):
            raise RuntimeError('completion_outcome_unknown') from None
        raise RuntimeError('storage_unavailable') from None
    if head.get('ContentLength') != attempt['receipt']['wire']['bytes']:
        raise ValueError('stored_backup_corrupt')
    verify_object(config, client, attempt['receipt'])
    if attempt['phase'] != 'committed':
        return publish(config, client, attempt, state_fd)
    saved = get_receipt(config, client, bid)
    if saved != attempt['receipt']:
        raise ValueError('cloud_receipt_mismatch')
    return saved


def receive(config, source, *, client=None, respond=None):
    header = read_header(config, source); bid = header['id']
    client = client if client is not None else make_client(config)
    with state_lock(config) as state_fd:
        attempt = state_read(state_fd, attempt_name(bid))
        if attempt is not None:
            validate_attempt(config, attempt, bid)
            if attempt['header']['snapshot_sha256'] != header['snapshot_sha256']:
                raise ValueError('backup_id_conflict')
            saved = reconcile(config, client, attempt, state_fd)
            return {**saved['wire'], 'duplicate': True}
        saved = get_receipt(config, client, bid)
        if saved is not None:
            if saved['wire']['snapshot_sha256'] != header['snapshot_sha256']:
                raise ValueError('backup_id_conflict')
            verify_object(config, client, saved)
            return {**saved['wire'], 'duplicate': True}
        # Unknown attempts in another logical ID block further full candidates.
        for name in os.listdir(state_fd):
            if name.endswith('.attempt.json'):
                other_id = name[:-len('.attempt.json')]; valid_id(other_id)
                other = validate_attempt(config, state_read(state_fd, name), other_id)
                if other['phase'] != 'committed':
                    raise RuntimeError('unresolved_upload_required')
        objects = inventory(config, client)
        if len(objects) >= config['max_objects'] or sum(objects.values()) + header['max_bytes'] > config['quota_bytes']:
            raise RuntimeError('receiver_quota')
        key = config['prefix'] + '/objects/' + bid + '-' + secrets.token_hex(16) + '.tar.age'
        attempt = {'v': 1, 'destination': destination(config), 'header': header, 'key': key,
                   'phase': 'creating', 'upload_id': None, 'receipt': None}
        state_write(state_fd, attempt_name(bid), attempt)
        created = cloud_call(client.create_multipart_upload, Bucket=config['bucket'], Key=key, ContentType='application/octet-stream')
        upload_id = created.get('UploadId')
        if not isinstance(upload_id, str) or not 1 <= len(upload_id) <= 1024:
            raise ValueError('invalid_upload_id')
        attempt['upload_id'] = upload_id; attempt['phase'] = 'uploading'
        state_write(state_fd, attempt_name(bid), attempt)
        try:
            if respond:
                respond({'v': 2, 'status': 'ready', 'id': bid, 'snapshot_sha256': header['snapshot_sha256']})
            digest = hashlib.sha256(); size = 0; prefix = b''; buffer = bytearray(); parts = []
            def part(data):
                number = len(parts) + 1
                if number > 10000:
                    raise ValueError('backup_size_mismatch')
                result = cloud_call(client.upload_part, Bucket=config['bucket'], Key=key, UploadId=upload_id,
                                    PartNumber=number, Body=data)
                etag = result.get('ETag')
                if not isinstance(etag, str) or not 1 <= len(etag) <= 1024:
                    raise ValueError('invalid_part_receipt')
                parts.append({'PartNumber': number, 'ETag': etag})
                # Parts remain bounded in memory. The durable completion journal
                # needs the final object identity, not 10,000 part ETags: an
                # unknown completion is reconciled, never blindly repeated.
            while True:
                length = struct.unpack('!I', local.exact_read(source, 4))[0]
                if length == 0:
                    break
                if length > MIB:
                    raise ValueError('invalid_backup_frame')
                if size + length > header['max_bytes']:
                    raise ValueError('backup_size_mismatch')
                data = local.exact_read(source, length); size += length; digest.update(data)
                if len(prefix) < len(AGE_HEADER):
                    prefix += data[:len(AGE_HEADER) - len(prefix)]
                buffer.extend(data)
                if len(buffer) >= config['part_bytes']:
                    part(bytes(buffer[:config['part_bytes']])); del buffer[:config['part_bytes']]
            raw = source.readline(4097)
            if len(raw) > 4096 or not raw.endswith(b'\n'):
                raise ValueError('truncated_backup')
            footer = unique_json(raw)
            if (not isinstance(footer, dict) or set(footer) != {'bytes', 'sha256'} or
                    type(footer['bytes']) is not int or footer['bytes'] != size or size < 64):
                raise ValueError('backup_size_mismatch')
            if footer['sha256'] != digest.hexdigest():
                raise ValueError('backup_hash_mismatch')
            if source.read(1):
                raise ValueError('backup_size_mismatch')
            if prefix != AGE_HEADER:
                raise ValueError('encrypted_backup_required')
            if buffer:
                part(bytes(buffer))
            wire = {'v': 2, 'status': 'stored', 'id': bid, 'bytes': size, 'sha256': digest.hexdigest(),
                    'snapshot_sha256': header['snapshot_sha256'], 'duplicate': False,
                    'received_at': datetime.now(timezone.utc).isoformat()}
            attempt['receipt'] = {'v': 1, 'destination': destination(config), 'object_key': key, 'wire': wire}
            attempt['phase'] = 'completing'
            state_write(state_fd, attempt_name(bid), attempt)
        except BaseException:
            # This path is reachable only before CompleteMultipartUpload. Never
            # abort a completion whose response may have been lost.
            cloud_call(client.abort_multipart_upload, Bucket=config['bucket'], Key=key, UploadId=upload_id)
            os.unlink(attempt_name(bid), dir_fd=state_fd); local.fsync_dir(state_fd)
            raise
        try:
            client.complete_multipart_upload(Bucket=config['bucket'], Key=key, UploadId=upload_id,
                                             MultipartUpload={'Parts': parts})
        except Exception:
            raise RuntimeError('completion_outcome_unknown') from None
        # A successful multipart acknowledgement plus an actual body verification
        # precedes publishing the logical ID. No ETag equality is assumed.
        verify_object(config, client, attempt['receipt'])
        result = publish(config, client, attempt, state_fd)
        return result['wire']


def download(config, bid, output, *, client=None):
    valid_id(bid); output = Path(output)
    if not output.is_absolute() or '..' in output.parts:
        raise ValueError('private_download_required')
    client = client if client is not None else make_client(config)
    with directory_handle(output.parent, trusted=os.geteuid() == 0) as out_fd, state_lock(config):
        info = os.fstat(out_fd)
        if info.st_mode & 0o077 or info.st_uid not in {0, os.geteuid()}:
            raise ValueError('private_download_required')
        receipt = get_receipt(config, client, bid)
        if receipt is None:
            raise ValueError('backup_not_found')
        name = '.ciphertext-' + secrets.token_hex(16)
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=out_fd)
        try:
            with os.fdopen(fd, 'wb') as out:
                verify_object(config, client, receipt, out)
                out.flush(); os.fsync(out.fileno())
            local.ensure_directory_identity(output.parent, out_fd)
            os.link(name, output.name, src_dir_fd=out_fd, dst_dir_fd=out_fd, follow_symlinks=False)
            local.fsync_dir(out_fd)
        finally:
            os.unlink(name, dir_fd=out_fd)
        return {**receipt['wire'], 'status': 'downloaded'}


def marker_value(config, items):
    return {'v': 1, 'destination': destination(config), 'backups': items}


def validate_marker(config, value):
    if (not isinstance(value, dict) or set(value) != {'v', 'destination', 'backups'} or
            type(value['v']) is not int or value['v'] != 1 or value['destination'] != destination(config)):
        raise ValueError('protected_destination_changed')
    local.anchor_items(value['backups'])
    if len(value['backups']) != 1:
        raise ValueError('one_protected_point_required')
    return value


def validate_proof(proof, receipt):
    if (not isinstance(proof, dict) or set(proof) != {'v', 'type', 'status', 'backup', 'verification'} or
            type(proof['v']) is not int or proof['v'] != 1 or
            proof['type'] != 'independent_restore_verification' or proof['status'] != 'verified'):
        raise ValueError('independent_restore_proof_required')
    candidate = proof['backup']; verification = proof['verification']
    if not isinstance(candidate, dict) or set(candidate) != {'id', 'bytes', 'sha256', 'snapshot_sha256'}:
        raise ValueError('invalid_restore_proof')
    new = {key: candidate[key] for key in ('id', 'bytes', 'sha256')}
    local.anchor_items([new])
    if not isinstance(candidate['snapshot_sha256'], str) or not local.DIGEST.fullmatch(candidate['snapshot_sha256']):
        raise ValueError('invalid_restore_proof')
    if (not isinstance(verification, dict) or
            set(verification) != {'id', 'verified_at', 'schema_version', 'manifest_sha256', 'checks'} or
            not isinstance(verification['id'], str) or not re.fullmatch('[a-f0-9]{32}', verification['id']) or
            type(verification['schema_version']) is not int or not 1 <= verification['schema_version'] <= 100000 or
            verification['manifest_sha256'] != candidate['snapshot_sha256'] or
            not isinstance(verification['checks'], dict) or set(verification['checks']) != CHECKS or
            any(value is not True for value in verification['checks'].values())):
        raise ValueError('independent_restore_proof_required')
    if receipt is None or any(receipt['wire'].get(key) != value for key, value in candidate.items()):
        raise ValueError('candidate_receipt_mismatch')
    try:
        verified = datetime.fromisoformat(verification['verified_at'])
        received = datetime.fromisoformat(receipt['wire']['received_at'])
        if (verified.utcoffset() is None or received.utcoffset() is None or
                verified > datetime.now(timezone.utc) + timedelta(minutes=5) or verified < received):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError('invalid_restore_time') from None
    return new


def operator_paths(config, protected_file, proof_file):
    if os.geteuid() != 0:
        raise ValueError('operator_retention_required')
    protected_file = Path(protected_file); proof_file = Path(proof_file)
    state = Path(config['state_directory'])
    for path in (protected_file, proof_file):
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('invalid_operator_path')
        if path.is_relative_to(state):
            raise ValueError('separate_operator_evidence_required')
    if (proof_file == protected_file or
            proof_file in {protected_file.with_name(protected_file.name + suffix) for suffix in ('.rotation.json', '.enrollment.json')}):
        raise ValueError('separate_operator_evidence_required')
    return state, protected_file, proof_file


def read_root(directory, name, *, limit=65536, missing=False):
    try:
        return local.maintenance_json(name, root=True, limit=limit, dir_fd=directory)
    except FileNotFoundError:
        if missing:
            return None, None
        raise


def candidate_from_proof(config, client, proof):
    if not isinstance(proof, dict) or not isinstance(proof.get('backup'), dict):
        raise ValueError('independent_restore_proof_required')
    bid = proof['backup'].get('id'); valid_id(bid)
    receipt = get_receipt(config, client, bid)
    new = validate_proof(proof, receipt)
    verify_object(config, client, receipt)
    return new, receipt


def protect(config, protected_file, proof_file, *, client=None):
    """Initial root enrollment after independent restore; never deletes anything."""
    state, marker, proof_file = operator_paths(config, protected_file, proof_file)
    client = client if client is not None else make_client(config)
    with operator_handles(state, marker, proof_file) as (state_fd, control_fd, proof_fd):
        proof_raw, proof = read_root(proof_fd, proof_file.name, limit=16384)
        new, receipt = candidate_from_proof(config, client, proof)
        value = marker_value(config, [new]); proof_sha = hashlib.sha256(proof_raw).hexdigest()
        journal_name = marker.name + '.enrollment.json'
        _, journal = read_root(control_fd, journal_name, missing=True)
        _, current = read_root(control_fd, marker.name, missing=True)
        expected = {'v': 1, 'destination': destination(config), 'proof_sha256': proof_sha,
                    'marker': value, 'receipt': receipt}
        if journal is not None and journal != expected:
            raise ValueError('enrollment_evidence_changed')
        if current is not None:
            validate_marker(config, current)
            if journal != expected or current != value:
                raise ValueError('protected_marker_exists')
            local.fsync_dir(control_fd)
            return {'status': 'protected', 'id': new['id'], 'duplicate': True,
                    'verification_id': proof['verification']['id']}
        # Never replace an established marker, including a corrupt/symlinked one.
        # The shared lock serializes only configured receive/root maintenance.
        local.write_maintenance_json(journal_name, expected, dir_fd=control_fd)
        local.ensure_directory_identity(marker.parent, control_fd)
        local.ensure_directory_identity(state, state_fd)
        local.write_maintenance_json(marker.name, value, dir_fd=control_fd)
        return {'status': 'protected', 'id': new['id'], 'duplicate': False,
                'verification_id': proof['verification']['id']}


def validate_rotation_journal(config, journal):
    if (not isinstance(journal, dict) or set(journal) != {'v', 'state', 'destination', 'proof_sha256',
            'expected_protected_sha256', 'previous', 'candidate', 'previous_receipts', 'candidate_receipt'} or
            type(journal['v']) is not int or journal['v'] != 1 or journal['state'] not in {'pending', 'complete'} or
            journal['destination'] != destination(config) or
            any(not isinstance(journal[key], str) or not local.DIGEST.fullmatch(journal[key])
                for key in ('proof_sha256', 'expected_protected_sha256'))):
        raise ValueError('invalid_rotation_journal')
    validate_marker(config, journal['previous']); validate_marker(config, journal['candidate'])
    previous = journal['previous']['backups']
    if not isinstance(journal['previous_receipts'], list) or len(journal['previous_receipts']) != len(previous):
        raise ValueError('invalid_rotation_journal')
    for item, receipt in zip(previous, journal['previous_receipts']):
        validate_receipt(config, receipt, item['id'])
        if any(receipt['wire'][key] != value for key, value in item.items()):
            raise ValueError('invalid_rotation_journal')
    new = journal['candidate']['backups'][0]
    validate_receipt(config, journal['candidate_receipt'], new['id'])
    if any(journal['candidate_receipt']['wire'][key] != value for key, value in new.items()):
        raise ValueError('invalid_rotation_journal')
    return journal


def rotate(config, protected_file, proof_file, *, expected_protected_sha256, client=None):
    """Fsync root evidence/marker before exact, independently authorized deletion."""
    state, marker, proof_file = operator_paths(config, protected_file, proof_file)
    if not isinstance(expected_protected_sha256, str) or not local.DIGEST.fullmatch(expected_protected_sha256):
        raise ValueError('invalid_protected_digest')
    client = client if client is not None else make_client(config)
    with operator_handles(state, marker, proof_file) as (state_fd, control_fd, proof_fd):
        proof_raw, proof = read_root(proof_fd, proof_file.name, limit=16384)
        marker_raw, current = read_root(control_fd, marker.name)
        validate_marker(config, current)
        new, candidate_receipt = candidate_from_proof(config, client, proof)
        candidate = marker_value(config, [new]); proof_sha = hashlib.sha256(proof_raw).hexdigest()
        journal_name = marker.name + '.rotation.json'
        _, journal = read_root(control_fd, journal_name, missing=True)
        if journal is not None:
            validate_rotation_journal(config, journal)
        matching = bool(journal and journal['proof_sha256'] == proof_sha and
                        journal['expected_protected_sha256'] == expected_protected_sha256 and
                        journal['candidate'] == candidate and journal['candidate_receipt'] == candidate_receipt)
        marker_matches = hashlib.sha256(marker_raw).hexdigest() == expected_protected_sha256
        resumed = not marker_matches and matching and current == candidate
        if not marker_matches and not resumed:
            raise ValueError('protected_marker_changed')
        if marker_matches:
            if any(item['id'] == new['id'] for item in current['backups']):
                raise ValueError('candidate_already_protected')
            if journal and journal['state'] == 'pending' and not matching:
                raise ValueError('anchor_rotation_incomplete')
            if matching and journal['previous'] != current:
                raise ValueError('invalid_rotation_journal')
            previous_receipts = []
            for item in current['backups']:
                receipt = get_receipt(config, client, item['id'])
                if receipt is None or any(receipt['wire'][key] != value for key, value in item.items()):
                    raise ValueError('protected_receipt_mismatch')
                verify_object(config, client, receipt)
                previous_receipts.append(receipt)
            journal = {'v': 1, 'state': 'pending', 'destination': destination(config), 'proof_sha256': proof_sha,
                       'expected_protected_sha256': expected_protected_sha256, 'previous': current,
                       'candidate': candidate, 'previous_receipts': previous_receipts, 'candidate_receipt': candidate_receipt}
            local.write_maintenance_json(journal_name, journal, dir_fd=control_fd)
            local.write_maintenance_json(marker.name, candidate, dir_fd=control_fd)
        else:
            # A prior replace may have succeeded immediately before fsync failed.
            local.fsync_dir(control_fd)
        local.ensure_directory_identity(marker.parent, control_fd)
        local.ensure_directory_identity(state, state_fd)
        # Fully inspect the exact deletion set before its first new mutation.
        # Missing old data is accepted only on a root-journal-bound resume after
        # the replacement was protected, never during initial authorization.
        observed_present = set()
        for old in journal['previous_receipts']:
            saved = get_receipt(config, client, old['wire']['id'])
            if saved is None:
                if not resumed:
                    raise ValueError('protected_receipt_mismatch')
            elif saved != old:
                raise ValueError('protected_receipt_mismatch')
            try:
                head = client.head_object(Bucket=config['bucket'], Key=old['object_key'])
            except Exception as exc:
                if not (resumed and absent(exc)):
                    raise RuntimeError('storage_unavailable') from None
            else:
                if head.get('ContentLength') != old['wire']['bytes']:
                    raise ValueError('protected_backup_corrupt')
                verify_object(config, client, old)
                observed_present.add(old['object_key'])
        removed = 0
        for old in journal['previous_receipts']:
            cloud_call(client.delete_object, Bucket=config['bucket'], Key=old['object_key'])
            cloud_call(client.delete_object, Bucket=config['bucket'], Key=receipt_key(config, old['wire']['id']))
            removed += int(old['object_key'] in observed_present)
        journal['state'] = 'complete'
        local.write_maintenance_json(journal_name, journal, dir_fd=control_fd)
        return {'status': 'rotated', 'id': new['id'], 'protected': 1, 'removed': removed,
                'duplicate': resumed, 'verification_id': proof['verification']['id']}


COLD_HEADER_KEYS = {'v', 'op', 'digest'}


def cold_key(config, digest):
    if not isinstance(digest, str) or not local.DIGEST.fullmatch(digest):
        raise ValueError('invalid_cold_request')
    return config['prefix'] + '/cold/' + digest + '.age'


@contextmanager
def cold_lock(config):
    """Cold copies serialize among themselves, never against backup uploads.

    Their lock and journals live in a private `cold/` subdirectory of upload
    state, which backup admission and the verifier never read.
    """
    with directory_handle(Path(config['state_directory'])) as state_fd:
        try:
            os.mkdir('cold', 0o700, dir_fd=state_fd)
        except FileExistsError:
            pass
        # Without this the entry, and every journal inside it, can vanish in a crash.
        # Existence is no proof: its creator may have failed or not yet reached this sync.
        local.fsync_dir(state_fd)
        cold_fd = os.open('cold', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=state_fd)
        try:
            info = os.fstat(cold_fd)
            if info.st_mode & 0o077 or info.st_uid != os.geteuid():
                raise ValueError('private_receiver_directory_required')
            fd = os.open('.cold.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=cold_fd)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077 or
                        info.st_uid != os.geteuid()):
                    raise ValueError('invalid_receiver_lock')
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError('receiver_busy') from None
                yield cold_fd
            finally:
                os.close(fd)
        finally:
            os.close(cold_fd)


def cold_attempt_name(key):
    return key.rsplit('/', 1)[1][:-len('.age')] + '.json'


def cold_reconcile(config, client, key, cold_fd):
    """Settle an earlier interrupted multipart attempt for this key first.

    Before completion was sent, aborting the journaled upload is enough. A sent
    completion is settled only by its object (then `exists`) or by aborting
    the still-open upload; otherwise the key stays blocked until an operator
    resolves the journal, so an older completion can never replace a newer object.
    """
    name = cold_attempt_name(key)
    attempt = state_read(cold_fd, name)
    if attempt is None:
        return
    if (not isinstance(attempt, dict) or set(attempt) != {'v', 'key', 'phase', 'upload_id', 'at'} or
            attempt['v'] != 1 or attempt['key'] != key or attempt['phase'] not in {'creating', 'uploading', 'completing'} or
            type(attempt['at']) not in (int, float) or
            (attempt['phase'] != 'creating' and
             (not isinstance(attempt['upload_id'], str) or not 1 <= len(attempt['upload_id']) <= 1024))):
        raise ValueError('invalid_attempt_journal')
    if attempt['phase'] != 'creating':
        try:
            client.abort_multipart_upload(Bucket=config['bucket'], Key=key, UploadId=attempt['upload_id'])
        except Exception as exc:
            if code(exc) != 'NoSuchUpload':
                raise RuntimeError('storage_unavailable') from None
            # The upload is gone and a sent completion may still publish it.
            # Nothing observable proves otherwise, so the key stays blocked
            # until its object appears or an operator resolves the journal.
            if attempt['phase'] == 'completing' and cold_head(config, client, key) is None:
                raise RuntimeError('completion_outcome_unknown') from None
    os.unlink(name, dir_fd=cold_fd); local.fsync_dir(cold_fd)


def cold_head(config, client, key):
    try:
        head = client.head_object(Bucket=config['bucket'], Key=key)
    except Exception as exc:
        if absent(exc):
            return None
        raise RuntimeError('storage_unavailable') from None
    size = head.get('ContentLength')
    if type(size) is not int or size < 0:
        raise RuntimeError('storage_unavailable')
    return size


def cold_put(config, client, key, source, respond, state_fd):
    """Write once under <prefix>/cold/: an existing key is reported, never replaced."""
    cold_reconcile(config, client, key, state_fd)
    journal = cold_attempt_name(key)
    def phase(name, upload):
        state_write(state_fd, journal, {'v': 1, 'key': key, 'phase': name, 'upload_id': upload,
                                        'at': datetime.now(timezone.utc).timestamp()})
    existing = cold_head(config, client, key)
    if existing is not None:
        respond({'v': 1, 'status': 'exists', 'key': key, 'bytes': existing})
        return
    respond({'v': 1, 'status': 'ready', 'key': key})
    digest = hashlib.sha256(); size = 0; prefix = b''; buffer = bytearray(); parts = []; upload_id = None
    def part(data):
        result = cloud_call(client.upload_part, Bucket=config['bucket'], Key=key, UploadId=upload_id,
                            PartNumber=len(parts) + 1, Body=data)
        etag = result.get('ETag')
        if not isinstance(etag, str) or not 1 <= len(etag) <= 1024:
            raise ValueError('invalid_part_receipt')
        parts.append({'PartNumber': len(parts) + 1, 'ETag': etag})
    try:
        while True:
            length = struct.unpack('!I', local.exact_read(source, 4))[0]
            if length == 0:
                break
            if length > MIB:
                raise ValueError('invalid_backup_frame')
            if size + length > config['cold_max_bytes']:
                raise ValueError('cold_size_limit')
            data = local.exact_read(source, length); size += length; digest.update(data)
            if len(prefix) < len(AGE_HEADER):
                prefix += data[:len(AGE_HEADER) - len(prefix)]
            buffer.extend(data)
            if len(buffer) >= config['part_bytes']:
                if upload_id is None:
                    phase('creating', None)
                    created = cloud_call(client.create_multipart_upload, Bucket=config['bucket'], Key=key,
                                         ContentType='application/octet-stream')
                    created_id = created.get('UploadId')
                    if not isinstance(created_id, str) or not 1 <= len(created_id) <= 1024:
                        raise ValueError('invalid_upload_id')
                    phase('uploading', created_id); upload_id = created_id
                part(bytes(buffer[:config['part_bytes']])); del buffer[:config['part_bytes']]
        raw = source.readline(4097)
        if len(raw) > 4096 or not raw.endswith(b'\n'):
            raise ValueError('truncated_backup')
        footer = unique_json(raw)
        if (not isinstance(footer, dict) or set(footer) != {'bytes', 'sha256'} or
                type(footer['bytes']) is not int or footer['bytes'] != size or size < 64):
            raise ValueError('backup_size_mismatch')
        if footer['sha256'] != digest.hexdigest():
            raise ValueError('backup_hash_mismatch')
        if source.read(1):
            raise ValueError('backup_size_mismatch')
        if prefix != AGE_HEADER:
            raise ValueError('encrypted_backup_required')
        if upload_id is not None and buffer:
            part(bytes(buffer))
    except BaseException:
        # Before completion only. A failed abort keeps the journal for the next
        # attempt's reconciliation; a successful one clears it.
        if upload_id is not None:
            cloud_call(client.abort_multipart_upload, Bucket=config['bucket'], Key=key, UploadId=upload_id)
        try:
            os.unlink(journal, dir_fd=state_fd); local.fsync_dir(state_fd)
        except FileNotFoundError:
            pass
        raise
    if upload_id is None:
        try:
            client.put_object(Bucket=config['bucket'], Key=key, Body=bytes(buffer),
                              ContentType='application/octet-stream', IfNoneMatch='*')
        except Exception as exc:
            if code(exc) not in {'412', 'PreconditionFailed'}:
                raise RuntimeError('storage_unavailable') from None
            respond({'v': 1, 'status': 'exists', 'key': key, 'bytes': cold_head(config, client, key)})
            return
    else:
        phase('completing', upload_id)
        try:
            client.complete_multipart_upload(Bucket=config['bucket'], Key=key, UploadId=upload_id,
                                             MultipartUpload={'Parts': parts})
        except Exception:
            # The journal stays: the next attempt aborts this upload, then re-checks the key.
            raise RuntimeError('completion_outcome_unknown') from None
        os.unlink(journal, dir_fd=state_fd); local.fsync_dir(state_fd)
    respond({'v': 1, 'status': 'stored', 'key': key, 'bytes': size, 'sha256': digest.hexdigest()})


def cold_get(config, client, key, respond, sink):
    try:
        response = client.get_object(Bucket=config['bucket'], Key=key)
    except Exception as exc:
        if absent(exc):
            raise ValueError('cold_object_missing') from None
        raise RuntimeError('storage_unavailable') from None
    body = response['Body']; size = response.get('ContentLength'); sent = 0
    try:
        if type(size) is not int or not 0 <= size <= config['cold_max_bytes']:
            raise RuntimeError('storage_unavailable')
        respond({'v': 1, 'status': 'found', 'key': key, 'bytes': size})
        while data := body.read(MIB):
            sent += len(data)
            if sent > size:
                raise RuntimeError('storage_unavailable')
            sink.write(data)
        sink.flush()
    finally:
        body.close()
    if sent != size:
        raise RuntimeError('storage_unavailable')


def cold_serve(config, source, sink, *, client=None):
    """Producer route for cold originals: put or get one digest, nothing else.

    There is no list, delete or overwrite verb, and backup inventory, quota and
    rotation only ever address <prefix>/objects/ and <prefix>/receipts/.
    """
    def respond(value):
        sink.write(encoded(value)); sink.flush()
    raw = source.readline(4097)
    if len(raw) > 4096 or not raw.endswith(b'\n'):
        raise ValueError('invalid_cold_request')
    header = unique_json(raw)
    if (not isinstance(header, dict) or set(header) != COLD_HEADER_KEYS or type(header['v']) is not int or
            header['v'] != 1 or header['op'] not in {'put', 'get'}):
        raise ValueError('invalid_cold_request')
    key = cold_key(config, header['digest'])
    client = client if client is not None else make_client(config)
    if header['op'] == 'get':
        cold_get(config, client, key, respond, sink)
        return
    with cold_lock(config) as state_fd:
        cold_put(config, client, key, source, respond, state_fd)


def config_identity(path):
    """What any rewrite, replacement or removal of the root verifier config changes.

    Visible to the upload UID without reading the private file (search permission only).
    """
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('capabilities_unavailable')
    return {'path': str(path), 'dev': info.st_dev, 'ino': info.st_ino, 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns}


def published_helper(config):
    """Read the root verifier's publication: root-owned, world-readable, not writable by others, one link.

    It names the verifier config it was made from; once that config changed, it is stale.
    """
    if 'capabilities_file' not in config:
        raise ValueError('capabilities_unavailable')
    path = Path(config['capabilities_file'])
    try:
        with local.trusted_directory(path.parent) as parent_fd:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
            with os.fdopen(fd, 'rb') as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or
                        info.st_mode & 0o022 or not info.st_mode & 0o004 or info.st_size > 4096):
                    raise ValueError('capabilities_unavailable')
                value = unique_json(source.read(4097))
        bound = value.get('verifier_config') if isinstance(value, dict) else None
        # Only the installed service's config counts: a staged copy's publication is refused.
        if (not isinstance(bound, dict) or bound.get('path') != str(Path(config['verifier_config_file'])) or
                config_identity(bound['path']) != bound):
            raise ValueError('capabilities_unavailable')
    except (OSError, ValueError):
        raise ValueError('capabilities_unavailable') from None
    schemas = value.get('helper_schemas')
    if (set(value) != {'v', 'verifier_config', 'helper_revision', 'helper_schemas'} or
            type(value['v']) is not int or value['v'] != 1 or
            not isinstance(value['helper_revision'], str) or not re.fullmatch('[a-f0-9]{40}', value['helper_revision']) or
            not isinstance(schemas, list) or len(schemas) != 2 or any(type(item) is not int for item in schemas) or
            not 2 <= schemas[0] <= schemas[1]):
        raise ValueError('capabilities_unavailable')
    return value


def capabilities(config, source):
    """Report the schema ranges this receiver's verifier and pinned helper read.

    Reached only through its own forced-command key. It takes no input and reads
    no archive, receipt or object store: only installed code and one root file.
    """
    # A pending byte is refused; an open, silent stdin is left unread.
    if select.select([source], [], [], 0)[0] and os.read(source.fileno(), 1):
        raise ValueError('capabilities_takes_no_input')
    from . import backup_r2_verifier as verifier
    helper = published_helper(config)
    return {'v': 1, 'verifier_schemas': [verifier.SCHEMAS[0], verifier.SCHEMAS[-1]],
            'helper_schemas': helper['helper_schemas'], 'helper_revision': helper['helper_revision']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('receive')
    commands.add_parser('cold')
    commands.add_parser('capabilities')
    fetch = commands.add_parser('download')
    fetch.add_argument('--id', required=True)
    fetch.add_argument('--output', type=Path, required=True)
    for name in ('protect', 'rotate'):
        command = commands.add_parser(name)
        command.add_argument('--protected-file', type=Path, required=True)
        command.add_argument('--restore-proof', type=Path, required=True)
        if name == 'rotate':
            command.add_argument('--expected-protected-sha256', required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    def timed_out(*_):
        cancel(TimeoutError('receiver_timeout'))
    CANCELLED.clear()
    signal.signal(signal.SIGALRM, timed_out)
    try:
        config = load_config(args.config)
        signal.alarm(config['timeout_seconds'])
        # SSH_ORIGINAL_COMMAND is deliberately neither read nor executed.
        # authorized_keys fixes this argv to `... --config ... receive`.
        if args.command == 'cold':
            # A separate forced-command key fixes this argv; it writes its own frames.
            cold_serve(config, sys.stdin.buffer, sys.stdout.buffer)
            return 0
        if args.command == 'capabilities':
            # Its own forced-command key fixes this argv; argparse refuses any argument.
            result = capabilities(config, sys.stdin.buffer)
        elif args.command == 'download':
            result = download(config, args.id, args.output)
        elif args.command == 'protect':
            result = protect(config, args.protected_file, args.restore_proof)
        elif args.command == 'rotate':
            result = rotate(config, args.protected_file, args.restore_proof,
                            expected_protected_sha256=args.expected_protected_sha256)
        else:
            result = receive(config, sys.stdin.buffer,
                             respond=lambda value: print(json.dumps(value, separators=(',', ':')), flush=True))
        print(json.dumps(result, separators=(',', ':')), flush=True)
        return 0
    except Exception as exc:
        safe = {'invalid_header', 'receiver_busy', 'receiver_quota', 'backup_id_conflict', 'truncated_backup',
                'backup_size_mismatch', 'backup_hash_mismatch', 'encrypted_backup_required', 'invalid_backup_frame',
                'storage_unavailable', 'receiver_timeout', 'completion_outcome_unknown', 'receipt_outcome_unknown',
                'creation_outcome_unknown', 'unresolved_upload_required', 'incomplete_upload_aborted_retry',
                'uploader_lock_initialization_required', 'invalid_cold_request', 'cold_size_limit',
                'cold_object_missing', 'capabilities_unavailable', 'capabilities_takes_no_input'}
        error = str(exc) if str(exc) in safe else 'receiver_failed'
        print(json.dumps({'v': 1, 'status': 'error', 'code': error}), flush=True)
        return 1
    finally:
        signal.alarm(0)
        CANCELLED.clear()


if __name__ == '__main__':
    raise SystemExit(main())
