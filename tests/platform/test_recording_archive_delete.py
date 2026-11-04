"""Owner-requested deletion of archived recordings (ANYM-730): synthetic text only, no provider credentials or network."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'services/recordings/src'))
sys.path.insert(0, str(ROOT / 'services/messaging'))
from deedkit_recordings import cli
from deedkit_recordings.archive_store import Store, backup_archive, namespace
from deedkit_recordings.security import Failure, read_document
from messaging_service import backup_recordings_continuity as continuity

CANARY = {'title': 'canarytitlexq7', 'transcript': 'canarytranscriptxq7', 'summary': 'canarysummaryxq7',
          'speaker': 'Canaryspeakerxq7'}


def fixture(tmp_path):
    tmp_path = tmp_path.resolve()
    def private(name, value):
        p = tmp_path / name
        p.write_text(value); p.chmod(0o600)
        return str(p)
    doc = {'version': 1, 'cursor_secret_file': private('cursor.key', 'c' * 64),
           'archive': {'directory': str(tmp_path / 'archive'), 'poll_seconds': 60},
           'accounts': [{'id': name, 'provider': 'omi', 'label': 'Synthetic', 'token_file': private(name + '.key', 'omi_synthetic'),
                         'archive_namespace': letter * 32} for name, letter in (('omi-a', 'a'), ('omi-b', 'b'))],
           'principals': [{'id': 'agent-a', 'key_sha256': hashlib.sha256(b'backend-a').hexdigest(),
                           'expires_at': '2099-01-01T00:00:00Z', 'accounts': ['omi-a'],
                           'permissions': ['recordings:read', 'recordings:content']}]}
    path = Path(private('config.json', json.dumps(doc)))
    return path


def seed(store, account, ident, created, *, canary=False, text='ordinary synthetic words'):
    item = {'id': ident, 'created_at': created, 'finished_at': created,
            'title': CANARY['title'] if canary else 'Ordinary title ' + ident}
    store.discover(account, [item], None, 100)
    # A transcript is stored as one JSON object per utterance: the speaker label lands in the `speakers` table.
    transcript = (json.dumps({'text': CANARY['transcript'] + ' ' + 'filler words ' * 200, 'speaker': CANARY['speaker']})
                  if canary else json.dumps({'text': text, 'speaker': 'Ordinary speaker'}))
    summary = CANARY['summary'] if canary else 'Ordinary summary'
    store.observe(account, ident, item, {'transcript': ('available', transcript, None),
                                         'summary': ('available', summary, None)}, 101)
    return store.record(account, ident)['ref']


@pytest.fixture
def archive(tmp_path):
    path = fixture(tmp_path)
    doc, _ = read_document(path)
    store = Store(doc.archive)
    a, b = doc.accounts
    refs = {
        'gone': seed(store, a, 'gone', '2026-01-10T10:00:00Z', canary=True),
        'also-gone': seed(store, a, 'also-gone', '2026-01-11T10:00:00Z', canary=True),
        'kept': seed(store, a, 'kept', '2026-03-01T10:00:00Z'),
        'other': seed(store, b, 'other', '2026-01-10T10:00:00Z', canary=True),
    }
    try:
        yield path, doc, store, refs
    finally:
        store.close()


def snap(store):
    """A fresh, not yet existing snapshot path in a private directory beside the archive."""
    folder = store.directory.parent / 'snaps'
    folder.mkdir(mode=0o700, exist_ok=True)
    return str(folder / (uuid.uuid4().hex + '.sqlite'))


def raw_bytes(directory):
    return b''.join(p.read_bytes() for p in sorted(Path(directory).glob('archive.sqlite*')))


def count(store, sql, *values):
    with store.connection() as db:
        return db.execute(sql, values).fetchone()[0]


# spec: ANYM-730 criterion 1
def test_preview_selects_by_refs_or_by_account_and_range_and_changes_nothing(archive):
    path, doc, store, refs = archive
    a, b = doc.accounts
    def state():
        with store.connection() as db:
            return [list(map(tuple, db.execute(sql).fetchall())) for sql in (
                'SELECT ref,revision FROM records ORDER BY ref', 'SELECT ref FROM events ORDER BY sequence', 'SELECT * FROM meta ORDER BY key',
                'SELECT provider_hash FROM tombstones')]
    before = state()
    by_refs = store.delete_records(refs=[refs['gone'], refs['kept']])
    by_range = store.delete_records(namespaces=[namespace(a)], created_from='2026-01-01T00:00:00Z', created_to='2026-02-01T00:00:00Z')
    assert by_refs['selected'] == 2 and by_refs['applied'] is False
    assert by_range['selected'] == 2 and by_range['applied'] is False  # 'other' belongs to another account
    assert state() == before and count(store, 'SELECT count(*) FROM records') == 4
    with pytest.raises(Failure) as error:
        store.delete_records()  # no selector is never "everything"
    assert error.value.code == 'deletion_selector_required'
    with pytest.raises(Failure) as error:
        store.delete_records(refs=[refs['gone']], namespaces=[namespace(a)], created_from='2026-01-01T00:00:00Z')
    assert error.value.code == 'deletion_selector_ambiguous'


# spec: ANYM-730 criterion 1
def test_delete_removes_rows_index_speakers_and_journal_references_in_bounded_batches(archive, monkeypatch):
    path, doc, store, refs = archive
    import deedkit_recordings.archive_store as module
    monkeypatch.setattr(module, 'DELETE_BATCH', 1)
    receipt = store.delete_records(refs=[refs['gone'], refs['also-gone']], apply=True, snapshot_to=snap(store))
    assert receipt['deleted'] == 2 and receipt['batches'] == 2
    for table in ('records', 'index_rows', 'speakers', 'events'):
        column = 'ref'
        assert count(store, f'SELECT count(*) FROM {table} WHERE {column} IN (?, ?)', refs['gone'], refs['also-gone']) == 0, table
    assert count(store, 'SELECT count(*) FROM texts4 WHERE ref IN (?, ?)', refs['gone'], refs['also-gone']) == 0
    assert count(store, 'SELECT count(*) FROM records') == 2 and count(store, 'SELECT count(*) FROM events WHERE ref=?', refs['kept']) >= 1
    assert count(store, "SELECT count(*) FROM texts4 WHERE texts4 MATCH 'ordinary'") >= 1  # the kept text is still searchable


# spec: ANYM-730 criterion 2
def test_a_canary_is_absent_from_the_live_file_the_wal_and_the_snapshot_after_deletion(archive, tmp_path):
    path, doc, store, refs = archive
    assert count(store, 'SELECT count(*) FROM speakers WHERE ref=?', refs['gone']) == 1  # a real structured label, not plain text
    for needle in CANARY.values():
        assert needle.encode() in raw_bytes(store.directory), needle  # the search can fail: before, it finds every canary
    destination = tmp_path / 'snapshots' / 'after.sqlite'
    destination.parent.mkdir(mode=0o700)
    receipt = store.delete_records(refs=[refs['gone'], refs['also-gone'], refs['other']], apply=True, snapshot_to=str(destination))
    assert receipt['sanitized'] is True
    live = raw_bytes(store.directory)
    for needle in (*CANARY.values(), CANARY['speaker'].lower()):
        assert needle.encode() not in live, needle
        assert needle.encode() not in destination.read_bytes(), needle
    assert not Path(str(store.path) + '-wal').exists() or Path(str(store.path) + '-wal').stat().st_size == 0
    with sqlite3.connect(store.path) as db:
        assert db.execute('PRAGMA freelist_count').fetchone()[0] == 0
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert receipt['snapshot']['records'] == 1 and receipt['snapshot']['archive_id']


# spec: ANYM-730 criterion 3
def test_a_deleted_provider_id_is_tombstoned_and_never_archived_again_until_lifted(archive):
    path, doc, store, refs = archive
    a, _ = doc.accounts
    store.delete_records(refs=[refs['gone']], apply=True, snapshot_to=snap(store))
    expected = continuity.provider_hash(namespace(a), 'gone')  # the contract of backup_recordings_continuity.py
    assert count(store, 'SELECT count(*) FROM tombstones WHERE provider_hash=?', expected) == 1
    result = store.discover(a, [{'id': 'gone', 'title': 'again'}, {'id': 'fresh', 'title': 'new'}], None, 200)
    assert result['inserted'] == 1 and result['fenced'] == 1
    assert count(store, 'SELECT count(*) FROM records WHERE provider_id=?', 'gone') == 0
    lifted = store.lift_tombstone(namespace(a), 'gone', apply=True)
    assert lifted == {'v': 1, 'lifted': 1, 'applied': True, 'tombstones': 0}
    assert store.discover(a, [{'id': 'gone', 'title': 'again'}], None, 300)['inserted'] == 1


# spec: ANYM-730 criterion 3
def test_a_listed_deleted_recording_is_not_fetched_again_by_the_collector(tmp_path):
    import httpx
    from deedkit_recordings.archive import ArchiveRuntime
    from deedkit_recordings.omi import Provider
    path = fixture(tmp_path)
    calls = []
    def vendor(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/conversations'):
            return httpx.Response(200, json=[{'id': 'conv-a'}], headers={
                'X-Omi-Has-More': 'false', 'X-Omi-List-Truncated': 'false', 'X-Omi-Effective-Limit': '25'})
        return httpx.Response(200, json={'id': 'conv-a', 'transcript_included': True, 'processing_deferred': False,
                                         'structured': {'title': 'T', 'overview': 'O'},
                                         'transcript_segments': [{'text': 'x', 'speaker': 'Speaker 1', 'start': 0, 'end': 3}]})
    import asyncio
    async def first():
        runtime = ArchiveRuntime(path, Provider(httpx.MockTransport(vendor)), None)
        await runtime.start(background=False)
        await runtime.run_once(now=100)
        await runtime.close()
    asyncio.run(first())
    assert any(call.endswith('/conv-a') for call in calls)
    doc, _ = read_document(path)
    store = Store(doc.archive)
    ref = store.record(doc.accounts[0], 'conv-a')['ref']
    store.delete_records(refs=[ref], apply=True, snapshot_to=snap(store))
    store.close()
    calls.clear()
    async def second():
        runtime = ArchiveRuntime(path, Provider(httpx.MockTransport(vendor)), None)
        await runtime.start(background=False)
        await runtime.run_once(now=1000)
        await runtime.run_once(now=100000)
        await runtime.close()
    asyncio.run(second())
    assert not any(call.endswith('/conv-a') for call in calls), calls


# spec: ANYM-730 criterion 5
def test_schema_five_is_created_fresh_and_upgrades_a_schema_four_archive_additively(archive, tmp_path):
    path, doc, store, refs = archive
    assert count(store, 'PRAGMA user_version') == 5
    store.close()
    with sqlite3.connect(store.path) as db:  # a published schema-4 archive: no tombstones table
        db.execute('DROP TABLE tombstones')
        db.execute('PRAGMA user_version=4')
    reopened = Store(doc.archive)
    try:
        assert count(reopened, 'PRAGMA user_version') == 5 and count(reopened, 'SELECT count(*) FROM tombstones') == 0
        assert count(reopened, 'SELECT count(*) FROM records') == 4  # no record is rewritten
        snapshot = tmp_path / 'snap'
        snapshot.mkdir(mode=0o700)
        backup_archive(doc.archive, str(snapshot / 'archive.sqlite'))  # the producer's online backup accepts schema 5
    finally:
        reopened.close()
        store.lock = None


# spec: ANYM-730 criterion 4
def test_the_post_deletion_snapshot_passes_continuity_only_because_of_its_tombstones(archive, tmp_path):
    path, doc, store, refs = archive
    folder = tmp_path / 'snaps'
    folder.mkdir(mode=0o700)
    before = folder / 'before.sqlite'
    store.backup(str(before))
    baseline = continuity.archive_facts(before)
    after = folder / 'after.sqlite'
    store.delete_records(refs=[refs['gone'], refs['also-gone']], apply=True, snapshot_to=str(after))
    candidate = continuity.archive_facts(after)
    assert len(candidate['tombstones']) == 2 and candidate['archive']['incarnation'] == baseline['archive']['incarnation'] + 1
    continuity.judge(baseline, candidate)  # the deleted entries may be missing: their hashes are tombstoned in the candidate
    stripped = dict(candidate, tombstones=[])
    with pytest.raises(continuity.ContinuityRefused) as error:
        continuity.judge(baseline, stripped)
    assert error.value.args[0] == 'archive_content_lost'
    # a bundle sealed before the deletion (older incarnation) is refused against the new baseline: it is cancelled
    with pytest.raises(continuity.ContinuityRefused) as error:
        continuity.judge(candidate, baseline)
    assert error.value.args[0] == 'archive_incarnation_lower'


# spec: ANYM-730 criterion 2
def test_a_run_interrupted_before_the_rewrite_is_finished_by_repeating_the_command(archive, monkeypatch):
    path, doc, store, refs = archive
    real = Store.operator_connection
    class Crashing:
        def __init__(self, db):
            self.db = db
        def execute(self, sql, *values):
            if sql == 'VACUUM':
                raise sqlite3.OperationalError('synthetic crash before the rewrite')
            return self.db.execute(sql, *values)
    from contextlib import contextmanager
    @contextmanager
    def crashing(self):
        with real(self) as db:
            yield Crashing(db)
    monkeypatch.setattr(Store, 'operator_connection', crashing)
    with pytest.raises(Failure) as error:
        store.delete_records(refs=[refs['gone'], refs['also-gone'], refs['other']], apply=True, snapshot_to=snap(store))
    assert error.value.code == 'archive_unavailable'
    assert count(store, 'SELECT count(*) FROM records WHERE ref=?', refs['gone']) == 0  # the batches were committed
    assert count(store, 'SELECT count(*) FROM tombstones') == 3
    with pytest.raises(Failure) as fenced:  # the durable mark: no snapshot, and so no backup, before the rewrite is finished
        store.backup(snap(store))
    assert fenced.value.code == 'archive_sanitization_pending'
    monkeypatch.setattr(Store, 'operator_connection', real)
    repeated = store.delete_records(refs=[refs['gone'], refs['also-gone'], refs['other']], apply=True, snapshot_to=snap(store))
    assert repeated['selected'] == 0 and repeated['sanitized'] is True and repeated['tombstones'] == 3
    store.backup(snap(store))  # the mark is cleared
    assert CANARY['transcript'].encode() not in raw_bytes(store.directory)


# spec: ANYM-730 criterion 2
def test_a_reader_that_pins_the_wal_fails_the_command_and_keeps_the_snapshot_fenced(archive):
    path, doc, store, refs = archive
    reader = sqlite3.connect(store.path)
    reader.execute('PRAGMA journal_mode=WAL')
    reader.execute('BEGIN')
    reader.execute('SELECT count(*) FROM records').fetchone()  # a read transaction the checkpoint cannot pass
    try:
        with pytest.raises(Failure) as error:
            store.delete_records(refs=[refs['gone']], apply=True, snapshot_to=snap(store))
        assert error.value.code == 'deletion_sanitization_incomplete'
        with pytest.raises(Failure) as fenced:
            store.backup(snap(store))
        assert fenced.value.code == 'archive_sanitization_pending'
    finally:
        reader.close()
    receipt = store.delete_records(refs=[refs['gone']], apply=True, snapshot_to=snap(store))
    assert receipt['sanitized'] is True and CANARY['transcript'].encode() in raw_bytes(store.directory)  # the other canaries stay
    store.backup(snap(store))


# spec: ANYM-730 criterion 2
def test_a_deletion_without_a_snapshot_path_is_refused_and_the_receipt_describes_the_written_file(archive, tmp_path):
    path, doc, store, refs = archive
    with pytest.raises(Failure) as error:
        store.delete_records(refs=[refs['gone']], apply=True)
    assert error.value.code == 'deletion_snapshot_required' and count(store, 'SELECT count(*) FROM records') == 4
    target = snap(store)
    receipt = store.delete_records(refs=[refs['gone']], apply=True, snapshot_to=target)
    with sqlite3.connect(target) as written:
        assert written.execute('SELECT count(*) FROM records').fetchone()[0] == receipt['snapshot']['records'] == 3


# spec: ANYM-730 criterion 2
def test_a_copy_that_started_before_the_deletion_finished_is_discarded_and_schemas_1_and_2_still_back_up(archive, tmp_path, monkeypatch):
    path, doc, store, refs = archive
    # The guard passes (it is told "clean"), but a deletion committed before the copy ended: the copy carries the mark.
    with sqlite3.connect(store.path) as db:
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('sanitize_pending',1)")
    real, calls = Store.sanitize_marked, []

    def guard_then_real(db):
        calls.append(1)
        return False if len(calls) == 1 else real(db)
    monkeypatch.setattr(Store, 'sanitize_marked', staticmethod(guard_then_real))
    target = snap(store)
    with pytest.raises(Failure) as error:
        store.backup(target)
    assert error.value.code == 'archive_sanitization_pending' and not Path(target).exists() and len(calls) == 2
    monkeypatch.undo()
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM meta WHERE key='sanitize_pending'")
    store.backup(snap(store))
    # Schema 1-2 archives have no `meta` table and are still accepted by the producer's online backup.
    store.close()
    with sqlite3.connect(store.path) as db:
        db.execute('DROP TABLE meta')
        db.execute('PRAGMA user_version=2')
    old = tmp_path / 'snaps' / 'legacy.sqlite'
    backup_archive(doc.archive, str(old))
    with sqlite3.connect(old) as copy:
        assert copy.execute('PRAGMA user_version').fetchone()[0] == 2


# spec: ANYM-730 criterion 6
def test_the_receipt_carries_counts_and_identity_but_no_title_or_text(archive):
    path, doc, store, refs = archive
    receipt = store.delete_records(refs=[refs['gone'], refs['other']], apply=True, snapshot_to=snap(store))
    text = json.dumps(receipt)
    assert all(word not in text for word in CANARY.values())
    assert set(receipt) >= {'v', 'applied', 'selected', 'deleted', 'batches', 'tombstones', 'tombstones_added', 'sanitized',
                            'incarnation', 'snapshot', 'backup_replacement'}
    assert receipt['backup_replacement'] == 'backup_replacement_pending' and receipt['tombstones'] == 2


# spec: ANYM-730 criterion 1
def test_deletion_refuses_an_archive_with_an_active_feed_a_building_index_or_too_little_space(archive, monkeypatch):
    path, doc, store, refs = archive
    import deedkit_recordings.archive_store as module
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda _: shutil._ntuple_diskusage(10 ** 12, 10 ** 12 - 1, 1))
    with pytest.raises(Failure) as error:
        store.delete_records(refs=[refs['gone']], apply=True, snapshot_to=snap(store))
    assert error.value.code == 'archive_capacity' and count(store, 'SELECT count(*) FROM records') == 4
    monkeypatch.undo()
    with sqlite3.connect(store.path) as db:
        db.execute("INSERT INTO meta(key,value) VALUES('feed_epochs','[[1,1]]')")
    with pytest.raises(Failure) as error:
        store.delete_records(refs=[refs['gone']], apply=True, snapshot_to=snap(store))
    assert error.value.code == 'deletion_feed_unsupported'
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM meta WHERE key='feed_epochs'")
        db.execute("UPDATE meta SET value='building' WHERE key='index_state'")
    with pytest.raises(Failure) as error:
        store.delete_records(refs=[refs['gone']], apply=True, snapshot_to=snap(store))
    assert error.value.code == 'archive_index_building' and count(store, 'SELECT count(*) FROM records') == 4


# spec: ANYM-730 criterion 1, 6
def test_the_operator_command_previews_then_applies_only_with_yes(archive, capsys):
    path, doc, store, refs = archive
    store.close()
    store.lock = None
    cli.main(['--config', str(path), '--delete', '--ref', refs['gone']])
    preview = json.loads(capsys.readouterr().out)
    assert preview['applied'] is False and preview['selected'] == 1
    cli.main(['--config', str(path), '--delete', '--account', 'omi-a', '--created-from', '2026-01-01T00:00:00Z',
              '--created-to', '2026-02-01T00:00:00Z', '--yes', '--snapshot-to', snap(store)])
    applied = json.loads(capsys.readouterr().out)
    assert applied['applied'] is True and applied['deleted'] == 2 and applied['backup_replacement'] == 'backup_replacement_pending'
    with pytest.raises(SystemExit):
        cli.main(['--config', str(path), '--delete'])  # a selector is required
    with pytest.raises(SystemExit):
        cli.main(['--config', str(path), '--delete', '--ref', refs['kept'], '--yes'])  # --yes needs --snapshot-to
    cli.main(['--config', str(path), '--lift-tombstone', '--account', 'omi-a', '--provider-id', 'gone', '--yes'])
    assert json.loads(capsys.readouterr().out)['lifted'] == 1
