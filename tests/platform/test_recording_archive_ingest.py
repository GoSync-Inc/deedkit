"""Durable text capture using synthetic accounts, HTTP and private temporary state."""
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'services/recordings/src'))
from deedkit_recordings.app import create_app
from deedkit_recordings.omi import Provider
from deedkit_recordings.security import Failure


def fixture(tmp_path):
    tmp_path = tmp_path.resolve()
    def private(name, value):
        p = tmp_path / name
        p.write_text(value); p.chmod(0o600)
        return str(p)
    doc = {'version': 1, 'cursor_secret_file': private('cursor.key', 'c' * 64),
           'archive': {'directory': str(tmp_path / 'archive'), 'poll_seconds': 60},
           'accounts': [{'id': 'omi-a', 'provider': 'omi', 'label': 'Synthetic',
             'token_file': private('omi.key', 'omi_synthetic'), 'archive_namespace': 'a' * 32}],
           'principals': [{'id': 'agent-a', 'key_sha256': hashlib.sha256(b'backend-a').hexdigest(),
             'expires_at': '2099-01-01T00:00:00Z', 'accounts': ['omi-a'],
             'permissions': ['recordings:read', 'recordings:content']}]}
    path = Path(private('config.json', json.dumps(doc)))
    return path, doc


def save(path, doc):
    path.write_text(json.dumps(doc)); path.chmod(0o600)


def conversation(ident='conv-a', text='Synthetic meeting about a telescope', deferred=False):
    return {'id': ident, 'transcript_included': True, 'processing_deferred': deferred,
            'structured': {'title': 'Synthetic title', 'overview': 'Decided to buy a telescope'},
            'transcript_segments': [{'text': text, 'speaker': 'Speaker 1', 'start': 0, 'end': 30}],
            'geolocation': {'secret': 'must never be archived'}, 'audio_url': 'https://invalid.test/private'}


class Vendor:
    def __init__(self):
        self.value, self.calls, self.fail = conversation(), [], False
    def __call__(self, request):
        self.calls.append(request)
        assert request.url.host == 'api.omi.me' and request.method == 'GET'
        if request.url.path.endswith('/conversations'):
            return httpx.Response(200, json=[self.value], headers={
                'X-Omi-Has-More': 'false', 'X-Omi-List-Truncated': 'false', 'X-Omi-Effective-Limit': '25'})
        return httpx.Response(503) if self.fail else httpx.Response(200, json=self.value)


def runtime(path, vendor):
    from deedkit_recordings.archive import ArchiveRuntime
    return ArchiveRuntime(path, Provider(httpx.MockTransport(vendor)), None)


# spec: ANYM-651
def test_capture_restart_replay_pending_and_failure_preserve_accepted_text(tmp_path):
    path, doc = fixture(tmp_path); vendor = Vendor()
    async def run():
        r = runtime(path, vendor)
        await r.start(background=False)
        await r.run_once(now=100)
        one = await r.db('record', doc['accounts'][0], 'conv-a')
        assert one['parts']['transcript']['state'] == 'available'
        assert one['parts']['summary']['text'] == 'Decided to buy a telescope'
        assert 'must never' not in json.dumps(one) and 'audio_url' not in json.dumps(one)
        events = await r.db('events', 0, 100)
        assert len(events) == 1 and 'telescope' not in json.dumps(events)
        await r.close()
        r = runtime(path, vendor); await r.start(background=False)
        await r.run_once(now=4000)
        two = await r.db('record', doc['accounts'][0], 'conv-a')
        assert two['ref'] == one['ref'] and two['revision'] == one['revision']
        assert len(await r.db('events', 0, 100)) == 1
        vendor.value = conversation(deferred=True)
        await r.run_once(now=8000)
        pending = await r.db('record', doc['accounts'][0], 'conv-a')
        assert pending['parts']['summary']['attempt_state'] == 'pending'
        assert pending['parts']['summary']['text'] == one['parts']['summary']['text']
        vendor.fail = True
        await r.run_once(now=12000)
        failed = await r.db('record', doc['accounts'][0], 'conv-a')
        assert failed['parts']['transcript']['attempt_error'] == 'provider_unavailable'
        assert failed['parts']['transcript']['text'] == one['parts']['transcript']['text']
        assert len(await r.db('events', 0, 100)) == 1
        status = await r.db('status', doc['accounts'][0])
        assert status['coverage'] == 'unknown' and status['retry_count'] == 1
        await r.close()
    asyncio.run(run())


# spec: ANYM-651
def test_discovery_checkpoint_and_queue_roll_back_together(tmp_path, monkeypatch):
    path, doc = fixture(tmp_path); vendor = Vendor()
    async def run():
        r = runtime(path, vendor); await r.start(background=False)
        account = r.document().accounts[0]
        def crash(): raise RuntimeError('synthetic crash before commit')
        with pytest.raises(RuntimeError):
            await r.db('discover', account, [{'id':'conv-a'}], 'next-history', 100, crash)
        assert (await r.db('status', account))['queued_count'] == 0
        assert await r.db('checkpoint', account) is None
        await r.db('discover', account, [{'id':'conv-a'}], 'next-history', 100, lambda: None)
        await r.close()
        r = runtime(path, vendor); await r.start(background=False)
        assert await r.db('checkpoint', account) == 'next-history'
        assert (await r.db('status', account))['queued_count'] == 1
        await r.close()
    asyncio.run(run())


# spec: ANYM-651
@pytest.mark.parametrize('change', ['remove', 'namespace', 'token'])
def test_configuration_revocation_during_fetch_discards_content(tmp_path, change):
    path, doc = fixture(tmp_path)
    vendor = Vendor()
    def handler(request):
        response = vendor(request)
        if not request.url.path.endswith('/conversations'):
            if change == 'remove': doc['accounts'] = []; doc['principals'][0]['accounts'] = []
            elif change == 'namespace': doc['accounts'][0]['archive_namespace'] = 'b' * 32
            else: Path(doc['accounts'][0]['token_file']).write_text('rotated_synthetic')
            save(path, doc)
        return response
    async def run():
        r = runtime(path, handler); await r.start(background=False)
        await r.run_once(now=100)
        assert await r.db('events', 0, 100) == []
        await r.close()
    asyncio.run(run())


# spec: ANYM-651
def test_capacity_pause_disk_full_rollback_and_recovery(tmp_path, monkeypatch):
    path, doc = fixture(tmp_path); vendor = Vendor()
    async def run():
        r = runtime(path, vendor); await r.start(background=False)
        account = r.document().accounts[0]
        await r.run_once(now=100)
        old = await r.db('record', account, 'conv-a')
        original = r.store.capacity
        def full(): raise Failure('archive_capacity')
        monkeypatch.setattr(r.store, 'capacity', full)
        vendor.value = conversation(text='changed text')
        await r.run_once(now=4000)
        assert r.error == 'archive_capacity'
        assert (await r.db('record', account, 'conv-a'))['revision'] == old['revision']
        monkeypatch.setattr(r.store, 'capacity', original)
        # A genuine SQLite FULL during a transaction rolls back both record and event.
        def disk_full(db):
            db.execute("INSERT INTO records(ref,namespace,provider_id,metadata,parts) SELECT 'new-ref',namespace,'conv-b','{}','{}' FROM records LIMIT 1")
            pages = db.execute('PRAGMA page_count').fetchone()[0]
            db.execute(f'PRAGMA max_page_count={pages}')
            db.execute('UPDATE records SET metadata=?', ('x' * 1000000,))
        with pytest.raises(Failure, match='archive_capacity'):
            await r.db('write', disk_full)
        assert await r.db('record', account, 'conv-b') is None
        await r.run_once(now=8000)
        new = await r.db('record', account, 'conv-a')
        assert new['revision'] > old['revision'] and r.error is None
        await r.close()
    asyncio.run(run())


# spec: ANYM-651
def test_private_state_lock_backup_and_restore(tmp_path):
    path, doc = fixture(tmp_path); vendor = Vendor()
    async def run():
        r = runtime(path, vendor); await r.start(background=False)
        await r.run_once(now=100)
        other = runtime(path, vendor); await other.start(background=False)
        assert other.error == 'archive_unavailable'
        await other.close()
        destination = tmp_path / 'snapshot.sqlite'
        await r.db('backup', destination)
        assert destination.stat().st_mode & 0o077 == 0
        with sqlite3.connect(destination) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 1
        archive = Path(doc['archive']['directory'])
        assert archive.stat().st_mode & 0o077 == 0
        assert all(p.stat().st_mode & 0o077 == 0 for p in archive.iterdir())
        await r.close()
        for p in archive.iterdir(): p.unlink()
        archive.rmdir(); archive.symlink_to(tmp_path, target_is_directory=True)
        bad = runtime(path, vendor); await bad.start(background=False)
        assert bad.error == 'archive_unavailable'
        await bad.close()
    asyncio.run(run())


# spec: ANYM-651
def test_disabled_failed_and_saturated_archive_leave_foreground_available(tmp_path):
    path, doc = fixture(tmp_path)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            if request.url.path.endswith('/conversations'):
                entered.set(); await release.wait()
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=conversation())
        app = create_app(path, transport=httpx.MockTransport(handler), archive_background=False)
        async with app.router.lifespan_context(app):
            r = app.state.archive
            task = asyncio.create_task(r.run_once(now=100))
            await asyncio.wait_for(entered.wait(), 2)
            assert app.state.provider.active == 1
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                    headers={'Authorization':'Bearer backend-a'}) as c:
                responses = await asyncio.gather(*(c.post('/v1/read_recording', json={
                    'account':'omi-a', 'recording_id':'conv-a'}) for _ in range(3)))
                assert all(response.status_code == 200 for response in responses)
            release.set(); await task
        # A corrupt archive is isolated during startup as well as collection.
        (Path(doc['archive']['directory']) / 'archive.sqlite').write_bytes(b'corrupt')
        app = create_app(path, transport=httpx.MockTransport(lambda _: httpx.Response(200,json=conversation())), archive_background=False)
        async with app.router.lifespan_context(app):
            assert app.state.archive.error == 'archive_unavailable'
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                    headers={'Authorization':'Bearer backend-a'}) as c:
                assert (await c.post('/v1/read_recording', json={'account':'omi-a','recording_id':'conv-a'})).status_code == 200
        del doc['archive']; del doc['accounts'][0]['archive_namespace']; save(path, doc)
        app = create_app(path, transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(AssertionError('no egress'))))
        async with app.router.lifespan_context(app):
            assert app.state.archive.store is None
    asyncio.run(run())


# spec: ANYM-651
def test_queue_bound_duplicate_ids_and_fair_new_retry_refresh_selection(tmp_path, monkeypatch):
    path, _ = fixture(tmp_path); vendor = Vendor()
    async def run():
        r = runtime(path, vendor); await r.start(background=False)
        account = r.document().accounts[0]
        items = [{'id': f'conv-{n}'} for n in range(15)]
        await r.db('discover', account, items + items, 'checkpoint-a', 100)
        assert (await r.db('status', account))['queued_count'] == 15
        for n in range(6):
            observation = ('error','','provider_unavailable') if n < 3 else ('available','accepted text',None)
            await r.db('observe', account, f'conv-{n}', {'id':f'conv-{n}'}, {'summary':observation}, 100)
        chosen = await r.db('work', account, 4000)
        assert {'conv-0','conv-1','conv-2'} <= set(chosen)
        assert len(set(chosen) & {'conv-3','conv-4','conv-5'}) == 2
        assert len(set(chosen) & {f'conv-{n}' for n in range(6,15)}) == 3
        monkeypatch.setattr('deedkit_recordings.archive_store.QUEUE_LIMIT', 9)
        with pytest.raises(Failure,match='archive_queue_full'):
            await r.db('discover', account, [{'id':'overflow'}], 'must-not-advance', 4000)
        assert await r.db('checkpoint', account) == 'checkpoint-a'
        assert await r.db('record', account, 'overflow') is None
        await r.close()
    asyncio.run(run())


# spec: ANYM-651
def test_invalid_transcript_does_not_block_summary_or_overwrite_old_text(tmp_path):
    path, doc = fixture(tmp_path); vendor = Vendor()
    async def run():
        r = runtime(path,vendor); await r.start(background=False)
        await r.run_once(now=100)
        vendor.value['transcript_segments'] = [{'text': ['invalid']}]
        vendor.value['structured']['overview'] = 'A newly available summary'
        await r.run_once(now=4000)
        row = await r.db('record', doc['accounts'][0], 'conv-a')
        assert row['parts']['transcript']['attempt_error'] == 'invalid_provider_response'
        assert 'telescope' in row['parts']['transcript']['text']
        assert row['parts']['summary']['text'] == 'A newly available summary'
        assert len(await r.db('events',0,100)) == 2
        await r.close()
    asyncio.run(run())


# spec: ANYM-651
def test_combined_text_limit_preserves_old_version_and_records_rejection(tmp_path):
    path, doc = fixture(tmp_path); vendor = Vendor()
    vendor.value['structured']['overview'] = 's' * 1200000
    async def run():
        r = runtime(path,vendor); await r.start(background=False); await r.run_once(now=100)
        old = await r.db('record', doc['accounts'][0], 'conv-a')
        # Each wire observation fits, but a new part plus the preserved old part does not.
        vendor.value['processing_deferred'] = True
        del vendor.value['structured']['overview']
        vendor.value['transcript_segments'] = [{'text':'t' * 1200000}]
        await r.run_once(now=4000)
        row = await r.db('record',doc['accounts'][0],'conv-a')
        assert row['parts']['transcript']['attempt_error'] == 'archive_content_limit'
        assert row['parts']['transcript']['digest'] == old['parts']['transcript']['digest']
        assert row['revision'] == old['revision']
        await r.close()
    asyncio.run(run())


# spec: ANYM-651
def test_filesystem_quota_guards_and_supported_schema_are_enforced(tmp_path, monkeypatch):
    path, doc = fixture(tmp_path); vendor = Vendor()
    async def run():
        r = runtime(path,vendor); await r.start(background=False)
        monkeypatch.setattr('deedkit_recordings.archive_store.shutil.disk_usage', lambda _: type('Space',(),{'free':0})())
        await r.run_once(now=100)
        assert r.error == 'archive_capacity'
        assert (await r.db('status',doc['accounts'][0]))['queued_count'] == 0
        monkeypatch.undo()
        await r.run_once(now=200)
        assert r.error is None
        await r.close()
        with sqlite3.connect(Path(doc['archive']['directory']) / 'archive.sqlite') as db:
            db.execute('PRAGMA user_version=999')
        bad = runtime(path,vendor); await bad.start(background=False)
        assert bad.error == 'archive_schema_unsupported'
        await bad.close()
    asyncio.run(run())


# spec: ANYM-651
def test_online_backup_can_run_while_writer_holds_lock_and_never_reads_vendor_token(tmp_path):
    path, doc = fixture(tmp_path); vendor = Vendor()
    async def run():
        from deedkit_recordings.archive_store import backup_archive
        r = runtime(path,vendor); await r.start(background=False); await r.run_once(now=100)
        Path(doc['accounts'][0]['token_file']).unlink()
        snapshot = tmp_path / 'online.sqlite'
        await asyncio.to_thread(backup_archive, r.document().archive, snapshot)
        with sqlite3.connect(snapshot) as restored:
            assert restored.execute('SELECT count(*) FROM records WHERE revision=1').fetchone()[0] == 1
            assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        with pytest.raises(Failure, match='archive_unavailable'):
            await asyncio.to_thread(backup_archive, r.document().archive, snapshot)
        await r.close()
    asyncio.run(run())


# spec: ANYM-651
def test_busy_database_and_query_deadline_are_bounded(tmp_path):
    path, doc = fixture(tmp_path); vendor = Vendor()
    async def run():
        import time
        r = runtime(path,vendor); await r.start(background=False); await r.run_once(now=100)
        with sqlite3.connect(Path(doc['archive']['directory']) / 'archive.sqlite') as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            started = time.monotonic()
            with pytest.raises(Failure,match='archive_unavailable'):
                await r.db('discover', r.document().accounts[0], [{'id':'blocked'}], 'blocked', 4000)
            assert time.monotonic() - started < 2
        def expensive():
            with r.store.connection() as db:
                db.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n').fetchone()
        r.store.expensive = expensive
        started = time.monotonic()
        with pytest.raises(Failure,match='archive_unavailable'):
            await r.db('expensive')
        assert time.monotonic() - started < 4
        await r.close()
    asyncio.run(run())


# spec: ANYM-651
def test_owned_background_cancellation_releases_admission_and_writer_lock(tmp_path):
    path, _ = fixture(tmp_path)
    async def run():
        entered = asyncio.Event()
        async def handler(request):
            entered.set()
            await asyncio.Event().wait()
        r = runtime(path,handler); await r.start()
        await asyncio.wait_for(entered.wait(), 2)
        assert r.provider.active == 1
        await asyncio.wait_for(r.close(), 2)
        assert r.provider.active == 0
        replacement = runtime(path,Vendor()); await replacement.start(background=False)
        assert replacement.error is None
        await replacement.close()
    asyncio.run(run())
