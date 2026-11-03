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
        await r.run_once(now=20000)
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
        vendor.value['structured']['overview'] = 'Changed overview'  # ANYM-724: a changed list row makes it due
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
        # ANYM-724: saved, complete records are no longer revisited hourly.
        assert not set(chosen) & {'conv-3','conv-4','conv-5'}
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


SCHEMA_3 = '''
CREATE TABLE accounts(namespace TEXT PRIMARY KEY, checkpoint TEXT, last_success REAL, error TEXT, work_turn INTEGER NOT NULL DEFAULT 0,
 throttled_until REAL, traversal_state TEXT NOT NULL DEFAULT 'not_started', passes_completed INTEGER NOT NULL DEFAULT 0,
 last_page_at REAL, started_at REAL, last_head_at REAL, head_cursor TEXT, head_backlog_since REAL, credential TEXT,
 head_floor INTEGER, text_bytes INTEGER NOT NULL DEFAULT 0);
CREATE TABLE records(
 ref TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider_id TEXT NOT NULL,
 metadata TEXT NOT NULL, parts TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 attempted REAL, due REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, fingerprint TEXT,
 UNIQUE(namespace, provider_id));
CREATE INDEX records_due ON records(namespace, due, attempted);
CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 namespace TEXT NOT NULL, ref TEXT NOT NULL, revision INTEGER NOT NULL, occurred REAL NOT NULL);
CREATE VIRTUAL TABLE texts USING fts5(ref UNINDEXED, namespace UNINDEXED, title, transcript, summary, tokenize='unicode61');
CREATE TABLE ledger(credential TEXT NOT NULL, bucket TEXT NOT NULL, window_start REAL NOT NULL, count INTEGER NOT NULL,
 updated REAL NOT NULL, PRIMARY KEY(credential, bucket, window_start));
CREATE TABLE meta(key TEXT PRIMARY KEY, value);
INSERT INTO meta(key,value) VALUES('archive_id','a'),('incarnation',1),('journal_id','b'),('text_reconciled',1);
PRAGMA application_id=1145786947;
PRAGMA user_version=3;
'''
OMI_A = {'provider': 'omi', 'id': 'omi-a', 'archive_namespace': 'a' * 32}


def stored(ident, number, filler=0):
    """One schema-3 row as the collector wrote it: JSON-line transcript, plain summary, list metadata."""
    who = 'Alice' if number % 2 else 'Bob'
    text = f'alpha{number} ' + 'filler word ' * filler
    transcript = json.dumps({'start': 0, 'end': 1, 'speaker': who, 'text': text}, separators=(',', ':'))
    summary = f'summary{number}'
    metadata = {'id': ident, 'title': f'Title {number}', 'created_at': f'2026-02-{1 + number % 28:02d}T10:{number % 60:02d}:00Z'}
    parts = {kind: {'state': 'available', 'text': value, 'digest': hashlib.sha256(value.encode()).hexdigest(),
                    'last_success': 1.0, 'attempt_state': 'available', 'attempt_error': None, 'attempted_at': 1.0}
             for kind, value in (('transcript', transcript), ('summary', summary))}
    return metadata, parts, transcript, summary


def legacy_archive(tmp_path, count, filler=0, extra=()):
    """A schema-3 archive file written without the new code, with `count` saved records and the old text index."""
    from deedkit_recordings.archive_store import namespace
    directory = tmp_path.resolve() / 'archive'
    directory.mkdir(mode=0o700)
    ns = namespace(OMI_A)
    with sqlite3.connect(directory / 'archive.sqlite') as db:
        db.executescript(SCHEMA_3)
        for number in range(count):
            metadata, parts, transcript, summary = stored(f'conv-{number:05d}', number, filler)
            ref = f'{number:032x}'
            db.execute('INSERT INTO records(ref,namespace,provider_id,metadata,parts,revision,attempted,due) VALUES(?,?,?,?,?,1,1.0,1e18)',
                       (ref, ns, metadata['id'], json.dumps(metadata, separators=(',', ':')), json.dumps(parts, separators=(',', ':'))))
            db.execute('INSERT INTO texts(ref,namespace,title,transcript,summary) VALUES(?,?,?,?,?)',
                       (ref, ns, metadata['title'], transcript, summary))
        for number, (metadata, parts) in enumerate(extra, start=count):
            db.execute('INSERT INTO records(ref,namespace,provider_id,metadata,parts,revision,attempted,due) VALUES(?,?,?,?,?,1,1.0,1e18)',
                       (f'{number:032x}', ns, f'conv-{number:05d}', metadata, parts))
            db.execute('INSERT INTO texts(ref,namespace,title,transcript,summary) VALUES(?,?,?,?,?)', (f'{number:032x}', ns, 'x', 'x', 'x'))
        db.execute("INSERT INTO events(event_id,namespace,ref,revision,occurred) VALUES('e1',?,?,1,1.0)", (ns, f'{0:032x}'))
    (directory / 'archive.sqlite').chmod(0o600)
    return directory / 'archive.sqlite'


def open_store(path):
    from deedkit_recordings.archive_store import Store
    from deedkit_recordings.security import read_document
    return Store(read_document(path)[0].archive)


def snapshot(file):
    with sqlite3.connect(file) as db:
        return (db.execute('SELECT ref,namespace,provider_id,metadata,parts,revision FROM records ORDER BY ref').fetchall(),
                db.execute('SELECT * FROM events ORDER BY sequence').fetchall())


def finish(store, limit=200):
    for _ in range(limit):
        if store.run('index_step', 1) == 'idle':
            return
    raise AssertionError('the index build did not finish')


# spec: ANYM-725
def test_schema3_archive_builds_the_new_index_across_bounded_batches_and_publishes_once(tmp_path):
    path, doc = fixture(tmp_path)
    file = legacy_archive(tmp_path, 5000, filler=450)  # about 5 KB of text per record
    before = snapshot(file)
    store = open_store(path)
    try:
        assert store.index_state()['state'] == 'building' and store.version() == 3
        assert store.search(OMI_A, '"alpha7"', 10)[0]['provider_recording_id'] == 'conv-00007'  # old index serves reads
        cursors, outcomes = [], []
        for _ in range(100):
            outcome = store.run('index_step', 1)  # one batch per call: each call is its own connection and deadline
            outcomes.append(outcome)
            cursors.append(store.index_state()['cursor'])
            if outcome == 'idle':
                break
            assert store.version() == (3 if outcome == 'built' else 4)
        assert outcomes.count('published') == 1 and outcomes[-1] == 'idle'
        assert outcomes.count('built') >= 3 and cursors == sorted(cursors), outcomes
        assert store.version() == 4 and store.index_state()['state'] == 'ready'
        assert store.index_state()['built'] == 5000 and store.index_state()['skipped'] == 0
        assert [hit['provider_recording_id'] for hit in store.search(OMI_A, '"alpha4999"', 10)] == ['conv-04999']
        assert store.search(OMI_A, None, 3, created_after='2026-02-28T00:00:00Z')[0]['created_at'].startswith('2026-02-28')
        with store.connection() as db:
            assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='texts'").fetchone()[0] == 0  # old index dropped after publication
    finally:
        store.close()
    after = snapshot(file)
    assert after == before  # refs, records, revisions, parts and the event journal are untouched; no event was written


# spec: ANYM-725
def test_reads_during_the_build_use_the_old_index_and_filters_wait_for_the_new_one(tmp_path, monkeypatch):
    monkeypatch.setattr('deedkit_recordings.archive_store.INDEX_BATCH_ROWS', 10)  # several batches for a small archive
    path, doc = fixture(tmp_path)
    legacy_archive(tmp_path, 60)
    store = open_store(path)
    try:
        store.run('index_step', 1)
        assert store.index_state()['state'] == 'building'
        for query in ('"alpha59"', '"summary59"', '"Title"'):
            assert store.search(OMI_A, query, 5)
        with pytest.raises(Failure, match='index_building'):
            store.search(OMI_A, None, 5, created_after='2026-02-01T00:00:00Z')
        with pytest.raises(Failure, match='index_building'):
            store.search(OMI_A, '"alpha1"', 5, speaker='alice')
        status = store.status(OMI_A)
        assert status['index']['state'] == 'building' and status['undated'] is None
        finish(store)
        assert store.search(OMI_A, None, 100, speaker='alice')
        assert store.status(OMI_A)['index']['state'] == 'ready' and store.status(OMI_A)['undated'] == 0
    finally:
        store.close()


# spec: ANYM-725
def test_kill_mid_build_resumes_from_the_cursor_and_a_failed_batch_leaves_schema3_usable(tmp_path, monkeypatch):
    monkeypatch.setattr('deedkit_recordings.archive_store.INDEX_BATCH_ROWS', 10)  # several batches for a small archive
    from deedkit_recordings import archive_store
    path, doc = fixture(tmp_path)
    file = legacy_archive(tmp_path, 40)
    before = snapshot(file)
    store = open_store(path)
    store.run('index_step', 1)
    cursor = store.index_state()['cursor']
    assert 0 < cursor
    store.close()  # the writer disappears; nothing is flushed by us
    store = open_store(path)
    try:
        assert store.index_state()['cursor'] == cursor and store.version() == 3
        original, calls = archive_store.Store.index_record, []
        def failing(self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise OSError('simulated crash inside a batch')
            return original(self, *args, **kwargs)
        monkeypatch.setattr(archive_store.Store, 'index_record', failing)
        with pytest.raises(Failure):
            store.run('index_step', 1)
        assert store.index_state()['cursor'] == cursor and store.version() == 3  # the batch rolled back whole
        assert store.search(OMI_A, '"alpha3"', 5)
        monkeypatch.undo()
        finish(store)
        assert store.version() == 4 and store.index_state()['built'] == 40
    finally:
        store.close()
    assert snapshot(file) == before


# spec: ANYM-725
def test_updates_and_title_changes_behind_and_ahead_of_the_cursor_reach_the_new_index(tmp_path, monkeypatch):
    monkeypatch.setattr('deedkit_recordings.archive_store.INDEX_BATCH_ROWS', 10)  # several batches for a small archive
    path, doc = fixture(tmp_path)
    legacy_archive(tmp_path, 40)
    store = open_store(path)
    try:
        store.run('index_step', 1)
        cursor = store.index_state()['cursor']
        assert 0 < cursor
        with store.connection() as db:
            order = [row[0] for row in db.execute('SELECT provider_id FROM records ORDER BY rowid')]
        behind, ahead = order[0], order[-1]
        for ident, word in ((behind, 'newbehind'), (ahead, 'newahead')):
            record = store.record(OMI_A, ident)
            store.observe(OMI_A, ident, {**record['metadata'], 'title': record['metadata']['title']},
                          {'transcript': ('available', json.dumps({'start': 0, 'end': 1, 'speaker': 'Carol', 'text': word}), None)}, 500)
        # a title-only change arrives through the list refresh
        store.discover(OMI_A, [{'id': order[1], 'title': 'Renamed title'}], None, 600)
        assert store.search(OMI_A, '"newbehind"', 5)  # old index, always current
        finish(store)
        for query, expected in (('"newbehind"', behind), ('"newahead"', ahead), ('"Renamed"', order[1])):
            assert [hit['provider_recording_id'] for hit in store.search(OMI_A, query, 5)] == [expected], query
        assert [hit['provider_recording_id'] for hit in store.search(OMI_A, None, 5, speaker='carol')].count(behind) == 1
        assert store.search(OMI_A, '"alpha0"', 5) == [] or store.search(OMI_A, '"alpha0"', 5)[0]['provider_recording_id'] != behind
        assert store.index_state()['built'] == 40
    finally:
        store.close()


# spec: ANYM-725
def test_a_crash_between_a_dual_write_and_the_cursor_save_is_repaired_by_the_resume(tmp_path, monkeypatch):
    monkeypatch.setattr('deedkit_recordings.archive_store.INDEX_BATCH_ROWS', 10)  # several batches for a small archive
    from deedkit_recordings import archive_store
    path, doc = fixture(tmp_path)
    legacy_archive(tmp_path, 30)
    store = open_store(path)
    try:
        store.run('index_step', 1)
        with store.connection() as db:
            first = db.execute('SELECT provider_id FROM records ORDER BY rowid LIMIT 1').fetchone()[0]
        record = store.record(OMI_A, first)
        store.observe(OMI_A, first, record['metadata'], {'summary': ('available', 'dualwrite marker', None)}, 700)
        saved = store.index_state()['cursor']
        monkeypatch.setattr(archive_store.Store, 'index_cursor_save', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('crash')))
        with pytest.raises(Failure):
            store.run('index_step', 1)
        assert store.index_state()['cursor'] == saved
        monkeypatch.undo()
        finish(store)
        assert [hit['provider_recording_id'] for hit in store.search(OMI_A, '"dualwrite"', 5)] == [first]
        with store.connection() as db:
            assert db.execute('SELECT count(*) FROM texts4 WHERE ref=?', (record['ref'],)).fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM texts4').fetchone()[0] == 30
    finally:
        store.close()


# spec: ANYM-725
def test_malformed_or_oversized_stored_metadata_is_counted_and_does_not_stop_the_build(tmp_path):
    good = stored('conv-x', 1)
    extra = [('not json', json.dumps(good[1])), (json.dumps({'id': 'big', 'title': 'T' * 70000}), json.dumps(good[1])),
             (json.dumps(good[0]), 'oops'), (json.dumps([1, 2]), json.dumps(good[1]))]
    path, doc = fixture(tmp_path)
    legacy_archive(tmp_path, 10, extra=extra)
    store = open_store(path)
    try:
        finish(store)
        state = store.index_state()
        assert state['state'] == 'ready' and state['skipped'] == 4 and state['built'] == 10
        assert store.status(OMI_A)['index']['skipped'] == 4
        assert store.search(OMI_A, '"alpha3"', 5)
    finally:
        store.close()


# spec: ANYM-725
def test_publication_verifies_against_records_and_continues_building_on_a_mismatch(tmp_path):
    path, doc = fixture(tmp_path)
    legacy_archive(tmp_path, 25)
    store = open_store(path)
    try:
        while store.run('index_step', 1) == 'built':
            if store.index_state()['cursor'] >= 25:
                break
        # a writer that skipped the dual write: records moved on, the index row did not
        with store.connection(readonly=False) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE records SET revision=revision+1 WHERE provider_id=?', ('conv-00004',))
            db.execute('DELETE FROM index_rows WHERE ref=?', (f'{9:032x}',))
            db.commit()
        outcomes = []
        for _ in range(10):
            outcomes.append(store.run('index_step', 1))
            if outcomes[-1] == 'idle':
                break
        assert 'published' in outcomes and outcomes.index('built') < outcomes.index('published'), outcomes
        with store.connection() as db:
            assert db.execute('SELECT count(*) FROM index_rows i JOIN records r ON r.ref=i.ref WHERE i.revision<>r.revision').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM index_rows').fetchone()[0] == 25
    finally:
        store.close()


NESTED = '{"a":' * 12000 + '1' + '}' * 12000  # valid-looking JSON line, far deeper than the parser's recursion limit


# spec: ANYM-725
def test_deeply_nested_json_text_is_ordinary_text_in_observe_and_in_the_migration(tmp_path):
    path, doc = fixture(tmp_path)
    store = open_store(path)
    try:
        store.discover(OMI_A, [{'id': 'conv-n', 'title': 'T'}], None, 1)
        store.observe(OMI_A, 'conv-n', {'id': 'conv-n', 'title': 'T'}, {'summary': ('available', NESTED, None)}, 2)
        assert store.record(OMI_A, 'conv-n')['revision'] == 1 and len(store.events(0, 10)) == 1  # the observation committed whole
        assert store.index_state()['skipped'] == 0 and [h['provider_recording_id'] for h in store.search(OMI_A, '"a"', 5)] == ['conv-n']
    finally:
        store.close()
    other = tmp_path / 'upgrade'
    other.mkdir(mode=0o700)
    path2, _ = fixture(other)
    metadata, parts, _, _ = stored('conv-x', 1)
    parts['summary'] = {**parts['summary'], 'text': NESTED}
    legacy_archive(other, 5, extra=[(json.dumps(metadata), json.dumps(parts))])
    store = open_store(path2)
    try:
        finish(store)
        assert store.index_state()['state'] == 'ready' and store.index_state()['skipped'] == 0 and store.index_state()['built'] == 6
    finally:
        store.close()


# spec: ANYM-725
def test_any_failure_deriving_one_record_is_a_counted_skip_and_never_loses_an_observation(tmp_path, monkeypatch):
    from deedkit_recordings import archive_store
    path, doc = fixture(tmp_path)
    store = open_store(path)
    try:
        store.discover(OMI_A, [{'id': 'conv-n', 'title': 'T'}], None, 1)
        monkeypatch.setattr(archive_store, 'plain_text', lambda *args: (_ for _ in ()).throw(RuntimeError('boom')))
        store.observe(OMI_A, 'conv-n', {'id': 'conv-n', 'title': 'T'}, {'summary': ('available', 'kept words', None)}, 2)
        monkeypatch.undo()
        assert store.record(OMI_A, 'conv-n')['parts']['summary']['text'] == 'kept words' and len(store.events(0, 10)) == 1
        assert store.index_state()['skipped'] == 1 and store.search(OMI_A, '"kept"', 5) == []
        store.observe(OMI_A, 'conv-n', {'id': 'conv-n', 'title': 'T'}, {'summary': ('available', 'kept words again', None)}, 3)
        assert store.index_state()['skipped'] == 0 and store.search(OMI_A, '"kept"', 5)  # the next write heals it
    finally:
        store.close()


# spec: ANYM-725
def test_publication_catches_rows_changed_by_a_writer_that_does_not_know_the_index(tmp_path):
    path, doc = fixture(tmp_path)
    file = legacy_archive(tmp_path, 25)
    store = open_store(path)
    try:
        assert store.run('index_step', 1) == 'built' and store.index_state()['cursor'] == 25  # everything is built, nothing published
        with sqlite3.connect(file) as old_writer:  # a schema-3 package resumes: it rewrites authoritative fields only
            old_writer.execute("UPDATE records SET revision=0, metadata=json_set(metadata,'$.title','newtitle') WHERE provider_id='conv-00003'")
            old_writer.execute("UPDATE records SET parts=json_set(parts,'$.transcript.text',"
                               "'{\"start\":0,\"end\":1,\"speaker\":\"Zed\",\"text\":\"zebra words\"}') WHERE provider_id='conv-00004'")
        outcomes = []
        while not outcomes or outcomes[-1] != 'idle':
            outcomes.append(store.run('index_step', 1))
        assert outcomes.index('built') < outcomes.index('published'), outcomes
        assert [h['provider_recording_id'] for h in store.search(OMI_A, '"newtitle"', 5)] == ['conv-00003']
        assert [h['provider_recording_id'] for h in store.search(OMI_A, '"zebra"', 5)] == ['conv-00004']
        assert store.search(OMI_A, '"alpha4"', 5) == []
        assert [h['provider_recording_id'] for h in store.search(OMI_A, None, 5, speaker='zed')] == ['conv-00004']
    finally:
        store.close()


# spec: ANYM-725
def test_newest_ordering_waits_for_the_published_index(tmp_path, monkeypatch):
    monkeypatch.setattr('deedkit_recordings.archive_store.INDEX_BATCH_ROWS', 10)
    path, doc = fixture(tmp_path)
    legacy_archive(tmp_path, 40)
    store = open_store(path)
    try:
        store.run('index_step', 1)
        assert store.search(OMI_A, '"alpha1"', 5, order='relevance')
        with pytest.raises(Failure, match='index_building'):
            store.search(OMI_A, '"alpha1"', 5, order='newest')  # created_at is complete only after publication
        finish(store)
        assert store.search(OMI_A, '"alpha1"', 5, order='newest')
    finally:
        store.close()


# spec: ANYM-725
def test_batch_and_metadata_bounds_count_utf8_bytes_not_characters(tmp_path, monkeypatch):
    monkeypatch.setattr('deedkit_recordings.archive_store.INDEX_BATCH_BYTES', 2_000_000)
    path, doc = fixture(tmp_path)
    extra = []
    for number in range(4):  # 400,000 characters = 1.2 MB each: four fit in 2 MB by characters, one at a time by bytes
        metadata, parts, _, _ = stored(f'conv-{number:05d}', number)
        parts['summary'] = {**parts['summary'], 'text': '语' * 400_000}
        extra.append((json.dumps(metadata), json.dumps(parts, ensure_ascii=False)))
    wide, parts, _, _ = stored('conv-00004', 4)
    wide['title'] = '语' * 30_000  # 30,000 characters = 90,000 bytes: over the 64 KiB metadata bound
    extra.append((json.dumps(wide, ensure_ascii=False), json.dumps(parts)))
    legacy_archive(tmp_path, 0, extra=extra)
    store = open_store(path)
    try:
        assert store.run('index_step', 1) == 'built' and store.index_state()['cursor'] == 1
        finish(store)
        state = store.index_state()
        assert state['state'] == 'ready' and state['built'] == 4 and state['skipped'] == 1
    finally:
        store.close()


# spec: ANYM-725
def test_a_json_escape_that_decodes_to_an_unstorable_string_stays_ordinary_text(tmp_path):
    line = '{"type":"summary","text":"\\ud800 lone"}'
    label_line = '{"start":0,"end":1,"speaker":"\\ud800","text":"spoken"}'
    path, doc = fixture(tmp_path)
    store = open_store(path)
    try:
        store.discover(OMI_A, [{'id': 'conv-s', 'title': 'T'}], None, 1)
        store.observe(OMI_A, 'conv-s', {'id': 'conv-s', 'title': 'T'},
                      {'summary': ('available', line, None), 'transcript': ('available', label_line, None)}, 2)
        assert store.record(OMI_A, 'conv-s')['revision'] == 1 and len(store.events(0, 10)) == 1  # the observation committed whole
        assert store.index_state()['skipped'] == 0 and [h['provider_recording_id'] for h in store.search(OMI_A, '"spoken"', 5)] == ['conv-s']
    finally:
        store.close()
    other = tmp_path / 'upgrade'
    other.mkdir(mode=0o700)
    path2, _ = fixture(other)
    metadata, parts, _, _ = stored('conv-x', 1)
    parts['summary'] = {**parts['summary'], 'text': line}
    legacy_archive(other, 5, extra=[(json.dumps(metadata), json.dumps(parts))])
    store = open_store(path2)
    try:
        finish(store)
        state = store.index_state()
        assert state['state'] == 'ready' and state['built'] == 6 and state['skipped'] == 0
    finally:
        store.close()


# spec: ANYM-725
def test_a_record_over_the_batch_budget_is_skipped_and_no_batch_exceeds_it(tmp_path):
    escaped = '\x01' * 800_000  # accepted by schema 3 (under the 2 MiB text limit); JSON-escaped it stores as about 4.8 MB
    metadata, parts, _, _ = stored('conv-big', 1)
    parts['summary'] = {**parts['summary'], 'text': escaped}
    path, doc = fixture(tmp_path)
    legacy_archive(tmp_path, 3, extra=[(json.dumps(metadata), json.dumps(parts))])
    store = open_store(path)
    try:
        with store.connection() as db:
            assert db.execute('SELECT max(length(CAST(metadata AS BLOB))+length(CAST(parts AS BLOB))) FROM records').fetchone()[0] > 4 * 1024 ** 2
        fetched = []
        original = type(store).index_row
        def watching(self, db, row):
            fetched.append(len(row['metadata'].encode()) + len(row['parts'].encode()))
            return original(self, db, row)
        type(store).index_row = watching
        try:
            finish(store)
        finally:
            type(store).index_row = original
        assert fetched and max(fetched) <= 4 * 1024 ** 2, fetched  # the oversized record was never read into memory
        state = store.index_state()
        assert state['state'] == 'ready' and state['built'] == 3 and state['skipped'] == 1
        assert store.status(OMI_A)['index']['skipped'] == 1
    finally:
        store.close()


# spec: ANYM-725
def test_new_archive_starts_at_schema4_and_runs_the_build_inside_collector_passes(tmp_path):
    path, doc = fixture(tmp_path)
    async def run():
        r = runtime(path, Vendor()); await r.start(background=False)
        assert r.store.version() == 4 and r.store.index_state()['state'] == 'ready'
        await r.run_once(now=100)
        assert (await r.db('status', doc['accounts'][0]))['index']['state'] == 'ready'
        await r.close()
    asyncio.run(run())
    other = tmp_path / 'upgrade'
    other.mkdir(mode=0o700)
    path2, doc2 = fixture(other)
    legacy_archive(other, 80)
    async def upgrade():
        vendor = Vendor()
        r = runtime(path2, vendor); await r.start(background=False)
        assert r.store.version() == 3
        for turn in range(40):
            await r.run_once(now=1000 + turn)
            if r.store.version() == 4:
                break
        assert r.store.version() == 4 and r.error is None
        await r.close()
    asyncio.run(upgrade())


# spec: ANYM-725
def test_an_index_failure_never_stops_collection(tmp_path, monkeypatch):
    path, doc = fixture(tmp_path)
    legacy_archive(tmp_path, 20)
    vendor = Vendor()
    async def run():
        from deedkit_recordings import archive_store
        r = runtime(path, vendor); await r.start(background=False)
        def broken(self, *args, **kwargs):
            raise Failure('archive_capacity')
        monkeypatch.setattr(archive_store.Store, 'index_step', broken)
        await r.run_once(now=100)
        status = await r.db('status', doc['accounts'][0])
        assert status['record_count'] >= 21 and (await r.db('record', doc['accounts'][0], 'conv-a'))['revision'] == 1
        assert r.store.version() == 3
        assert (await r.status('omi-a'))['index']['error'] == 'archive_capacity'
        await r.close()
    asyncio.run(run())


# spec: ANYM-725
def test_backup_accepts_schema_1_to_4_and_unknown_newer_schema_is_refused(tmp_path):
    from deedkit_recordings.archive_store import backup_archive
    path, doc = fixture(tmp_path)
    file = legacy_archive(tmp_path, 3)
    settings = open_store(path)
    try:
        for number, version in enumerate((3, 4)):
            if version == 4:
                finish(settings)
            target = tmp_path / f'backup{number}.sqlite'
            backup_archive(settings.settings, target)
            with sqlite3.connect(target) as copy:
                assert copy.execute('PRAGMA user_version').fetchone()[0] == version
                assert copy.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        settings.close()
    with sqlite3.connect(file) as db:
        db.execute('PRAGMA user_version=5')
    with pytest.raises(Failure, match='archive_schema_unsupported'):
        open_store(path)


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


# spec: ANYM-651
@pytest.mark.parametrize('fault',['quota','shape','namespace'])
def test_invalid_archive_configuration_never_disables_foreground_authentication(tmp_path,fault):
    path,doc = fixture(tmp_path)
    if fault == 'quota': doc['archive']['max_bytes'] = 0
    elif fault == 'shape': doc['archive'] = 'invalid archive-only settings'
    else: doc['accounts'][0]['archive_namespace'] = 'invalid namespace'
    save(path,doc)
    async def run():
        app = create_app(path,transport=httpx.MockTransport(Vendor()),archive_background=False)
        async with app.router.lifespan_context(app):
            assert app.state.archive.error
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                    headers={'Authorization':'Bearer backend-a'}) as c:
                result = await c.post('/v1/read_recording',json={'account':'omi-a','recording_id':'conv-a'})
                assert result.status_code == 200
                # Archive isolation must not weaken validation of authentication/grants.
                doc['principals'][0]['permissions'] = ['invalid-grant']; save(path,doc)
                assert (await c.post('/v1/read_recording',json={'account':'omi-a','recording_id':'conv-a'})).status_code == 503
    asyncio.run(run())


# spec: ANYM-651
@pytest.mark.parametrize('listing_error',[False,True])
def test_discovery_failure_and_queue_saturation_do_not_block_durable_work(tmp_path,monkeypatch,listing_error):
    path,doc = fixture(tmp_path)
    monkeypatch.setattr('deedkit_recordings.archive_store.QUEUE_LIMIT',3)
    def vendor(request):
        if request.url.path.endswith('/conversations'):
            return httpx.Response(503) if listing_error else httpx.Response(200,json=[conversation(f'new-{i}') for i in range(3)])
        return httpx.Response(200,json=conversation(request.url.path.rsplit('/',1)[-1]))
    async def run():
        r = runtime(path,vendor); await r.start(background=False)
        account = r.document().accounts[0]
        await r.db('discover',account,[{'id':'old-0'},{'id':'old-1'}],None,100)
        await r.run_once(now=100)
        assert (await r.db('status',account))['queued_count'] == 0
        assert len(await r.db('events',0,100)) == 2
        assert await r.db('checkpoint',account) is None
        if not listing_error:
            await r.run_once(now=200)
            assert (await r.db('status',account))['queued_count'] == 0
            assert len(await r.db('events',0,100)) == 5
            await r.run_once(now=300)
            assert len(await r.db('events',0,100)) == 5
        await r.close()
    asyncio.run(run())


# spec: ANYM-651
def test_work_classes_make_actual_progress_across_interrupted_passes(tmp_path,monkeypatch):
    path,_ = fixture(tmp_path)
    original_timeout = asyncio.timeout
    monkeypatch.setattr('deedkit_recordings.archive.asyncio.timeout',
                        lambda seconds: original_timeout(0.075 if seconds == 60 else seconds))
    async def vendor(request):
        if request.url.path.endswith('/conversations'):
            return httpx.Response(200,json=[])
        await asyncio.sleep(0.04)
        return httpx.Response(200,json=conversation(request.url.path.rsplit('/',1)[-1]))
    async def run():
        r = runtime(path,vendor); await r.start(background=False)
        account = r.document().accounts[0]
        items = [{'id':f'new-{n}'} for n in range(30)] + [{'id':'retry'},{'id':'refresh'}]
        await r.db('discover',account,items,None,1)
        await r.db('observe',account,'retry',{'id':'retry'},{'summary':('error','','provider_unavailable')},1)
        await r.db('observe',account,'refresh',{'id':'refresh'},{'summary':('pending','',None)},1)
        for now in (40000,40010,40020): await r.run_once(now=now)
        assert (await r.db('record',account,'retry'))['attempted'] > 1
        assert (await r.db('record',account,'refresh'))['attempted'] > 1
        await r.close()
    asyncio.run(run())


# spec: ANYM-651
def test_capacity_status_is_readable_without_a_successful_state_write(tmp_path,monkeypatch):
    path,_ = fixture(tmp_path)
    async def run():
        app = create_app(path,transport=httpx.MockTransport(Vendor()),archive_background=False)
        async with app.router.lifespan_context(app):
            r = app.state.archive; await r.run_once(now=100)
            original = r.store.capacity
            def full(): raise Failure('archive_capacity')
            monkeypatch.setattr(r.store,'capacity',full)
            await r.run_once(now=4000)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                    headers={'Authorization':'Bearer backend-a'}) as c:
                paused = await c.post('/v1/archive_status',json={'account':'omi-a'})
                assert paused.status_code == 200 and paused.json()['error'] == 'archive_capacity'
                assert paused.json()['record_count'] == 1
                denied = await c.post('/v1/archive_status',json={'account':'other-account'})
                assert denied.status_code == 403
                monkeypatch.setattr(r.store,'capacity',original)
                await r.run_once(now=8000)
                resumed = (await c.post('/v1/archive_status',json={'account':'omi-a'})).json()
                assert resumed['error'] is None
    asyncio.run(run())


# spec: ANYM-651
def test_backup_rejects_descendants_of_live_archive(tmp_path):
    path,_ = fixture(tmp_path)
    async def run():
        r = runtime(path,Vendor()); await r.start(background=False)
        child = r.store.directory / 'snapshots'; child.mkdir(mode=0o700)
        with pytest.raises(Failure,match='archive_unavailable'):
            await r.db('backup',child/'copy.sqlite')
        assert not (child/'copy.sqlite').exists()
        await r.close()
    asyncio.run(run())
