"""Real PostgreSQL upload lifecycle races; no provider calls or sessions."""
import base64
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread
from types import SimpleNamespace

import psycopg
import pytest

from messaging_service import uploads
from messaging_service.archive import Archive
from messaging_service.blobs import BlobWriter
from messaging_service.commands import CommandWorker
from messaging_service.storage_admission import StorageAdmission
from test_archive import media_event
from test_file_uploads import uploader as uploader, prepare, chunk, send_args
from test_messenger_commands import operator as operator


@pytest.mark.parametrize('phase', ['claim', 'preflight'])
def test_upload_dispatch_and_background_media_admission_do_not_invert_locks(uploader, monkeypatch, phase):
    s, key, state = uploader
    monkeypatch.setattr('shutil.disk_usage', lambda _: SimpleNamespace(free=100*1024**3))
    row = prepare(s, key)
    chunk(s, key, row, b'Synthetic file')
    args = send_args(row)
    draft = s.preview_messenger_operation(key, **args)
    queued = s.execute_messenger_operation(key, **args, preview_digest=draft['preview_digest'],
        request_id='synthetic-media-upload-lock-order')
    worker = CommandWorker(state, None)
    job = worker.claim() if phase == 'preflight' else None
    state.archive.ingest(media_event(account=state.account, chat='100', provider_id='50'))
    admission_held = Event()
    command_fenced = Event()
    original_lock = StorageAdmission.lock
    original_fence = Archive.fence

    def admission(c):
        original_lock(c)
        if current_thread().name.startswith('synthetic-media-admission'):
            admission_held.set()
            assert command_fenced.wait(5), 'Command did not acquire its collector fence'

    def fence(c, account, lease):
        original_fence(c, account, lease)
        if current_thread().name.startswith('synthetic-upload-claim'):
            command_fenced.set()

    monkeypatch.setattr(StorageAdmission, 'lock', staticmethod(admission))
    monkeypatch.setattr(Archive, 'fence', staticmethod(fence))
    writer = BlobWriter(s.files, reserve_bytes=0)
    # Hold the background capacity lock, then let claim acquire the collector
    # row. The old source path requested capacity while still holding that row:
    # PostgreSQL detects a genuine cycle when admission asks for the same row.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='synthetic-media-admission') as media_pool:
        media = media_pool.submit(state.archive.claim_media, 'synthetic-media',
            account=state.account, collector_lease=state.lease, writer=writer)
        assert admission_held.wait(5), 'Media did not acquire capacity admission'
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='synthetic-upload-claim') as command_pool:
            claim = command_pool.submit(worker.preflight, job) if job else command_pool.submit(worker.claim)
            command = claim.result(timeout=10)
            media_job = media.result(timeout=10)
    assert command['id'] == queued['operation_id'] and not command.get('cancelled')
    assert media_job and media_job['account'] == state.account
    with s.pool.connection() as c:
        assert c.execute('SELECT status FROM messenger_commands WHERE id=%s',
            (command['id'],)).fetchone()['status'] == 'dispatching'
        assert c.execute('SELECT count(*) AS n FROM media_reservations').fetchone()['n'] == 1


def test_source_fences_same_upload_chunk_without_blocking_another_upload(uploader):
    s, key, _ = uploader
    content = b'Synthetic file'
    ready = prepare(s, key)
    chunk(s, key, ready, content)
    other = prepare(s, key, request_id='synthetic-independent-upload')

    def duplicate():
        with s.pool.connection() as c:
            p = s.identity(c, key)
            c.execute("SET LOCAL lock_timeout='100ms'")
            return uploads.chunk(s, c, p, ready['upload_id'], 0, base64.b64encode(content).decode())

    with s.authorized(key) as (c, p):
        uploads.source(s, c, p, ready['account'], ready['chat_id'], send_args(ready)['parameters'], verify=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            # Capacity belongs to another upload: it can complete while this
            # immutable source remains protected until its command commits.
            assert pool.submit(chunk, s, key, other, content).result(timeout=5)['status'] == 'ready'
            with pytest.raises(psycopg.errors.LockNotAvailable):
                pool.submit(duplicate).result(timeout=5)
        assert (s.files/'.uploads'/ready['upload_id']/ready['sha256']).read_bytes() == content
    assert chunk(s, key, ready, content)['status'] == 'ready'
