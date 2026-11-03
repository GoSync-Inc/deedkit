"""Omi full backfill (ANYM-724): schema 3, budget ledger, 429, enumeration, ordering, progress. Synthetic Omi only."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import httpx
from pydantic import ValidationError
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'services/recordings/src'))
from deedkit_recordings.app import create_app
from deedkit_recordings.archive import ArchiveRuntime
from deedkit_recordings.archive_store import backup_archive
from deedkit_recordings.omi import Provider
from deedkit_recordings.security import ArchiveSettings

T0 = 1_800_000_000


def iso(moment):
    return datetime.fromtimestamp(moment, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def build(tmp_path, accounts=(('omi-a', 'tokenaaaa', 'a'),), **archive):
    tmp_path = tmp_path.resolve()
    def private(name, value):
        p = tmp_path / name
        p.write_text(value); p.chmod(0o600)
        return str(p)
    doc = {'version': 1, 'cursor_secret_file': private('cursor.key', 'c' * 64),
           'archive': {'directory': str(tmp_path / 'archive'), 'poll_seconds': 60, **archive},
           'accounts': [{'id': ident, 'provider': 'omi', 'label': 'Synthetic', 'token_file': private(ident + '.key', token),
                         'archive_namespace': letter * 32} for ident, token, letter in accounts],
           'principals': [{'id': 'agent-a', 'key_sha256': hashlib.sha256(b'backend-a').hexdigest(),
                           'expires_at': '2099-01-01T00:00:00Z', 'accounts': [a[0] for a in accounts],
                           'permissions': ['recordings:read', 'recordings:content']}]}
    path = Path(private('config.json', json.dumps(doc)))
    return path, doc


class Omi:
    """Synthetic Omi: newest-first conversations, list limit/cursor, fixed per-credential windows, 429 on demand."""
    def __init__(self, count=0, created=None):
        self.items, self.now, self.calls = [], float(T0), []
        self.windows, self.peak, self.limited = {}, {}, {}
        self.truncated, self.hide_overview = 0, False
        for n in range(count):
            self.add(f'c{n:05d}', (created if created is not None else T0 - 86400 * 30) - n * 60)

    def add(self, ident, created, title=None, overview='Synthetic overview', front=False):
        item = {'id': ident, 'created_at': iso(created), 'processing_deferred': False,
                'structured': {'title': title or 'Title ' + ident, 'overview': overview}, 'text': 'Words of ' + ident}
        self.items.insert(0, item) if front else self.items.append(item)
        return item

    def view(self, item, detail):
        value = {'id': item['id'], 'created_at': item['created_at'], 'processing_deferred': item['processing_deferred'],
                 'structured': dict(item['structured'])}
        if self.hide_overview and not detail:
            value['structured'].pop('overview', None)
        if detail:
            value.update(transcript_included=True, transcript_segments=[
                {'text': item['text'], 'speaker': 'Speaker 1', 'start': 0, 'end': 5}])
        return value

    def count(self, token, buckets):
        for bucket in buckets:
            start, used = self.windows.get((token, bucket), (None, 0))
            if start is None or self.now >= start + 3600:
                start, used = self.now, 0
            used += 1
            self.windows[(token, bucket)] = (start, used)
            self.peak[(token, bucket)] = max(self.peak.get((token, bucket), 0), used)

    def __call__(self, request):
        assert request.url.host == 'api.omi.me' and request.method == 'GET'
        token = request.headers['authorization'][7:]
        listing = request.url.path.endswith('/conversations')
        kind = 'list' if listing else 'detail'
        self.calls.append((self.now, token, kind, dict(request.url.params) if listing else request.url.path.rsplit('/', 1)[-1]))
        limit = self.limited.get((token, kind)) or self.limited.get((token, 'any'))
        if limit and self.now < limit[0]:
            return httpx.Response(429, headers={'Retry-After': limit[1]} if limit[1] is not None else {})
        self.count(token, ['reads'] if listing else ['reads', 'transcripts'])
        if not listing:
            ident = request.url.path.rsplit('/', 1)[-1]
            item = next((i for i in self.items if i['id'] == ident), None)
            return httpx.Response(200, json=self.view(item, True)) if item else httpx.Response(404)
        params = request.url.params
        size, cursor = int(params['limit']), params.get('cursor')
        assert 1 <= size <= 200 and params.get('include_transcript') == 'false' and params.get('offset') == '0'
        start = int(cursor[1:]) if cursor else 0
        page = self.items[start:start + size]
        more = start + size < len(self.items)
        truncated = self.truncated > 0
        if truncated:
            self.truncated -= 1
        headers = {'X-Omi-Has-More': str(more).lower(), 'X-Omi-List-Truncated': str(truncated).lower(),
                   'X-Omi-Effective-Limit': str(size)}
        if more:
            headers['X-Omi-Next-Cursor'] = f'c{start + size}'
        return httpx.Response(200, json=[self.view(i, False) for i in page], headers=headers)

    def of(self, token, kind=None):
        return [c for c in self.calls if c[1] == token and (kind is None or c[2] == kind)]


def runtime(path, omi):
    return ArchiveRuntime(path, Provider(httpx.MockTransport(omi)), None)


async def passes(r, omi, first, step, count):
    for n in range(count):
        omi.now = first + n * step
        await r.run_once(now=omi.now)


def sql(doc, statement, *args):
    with sqlite3.connect(Path(doc['archive']['directory']) / 'archive.sqlite') as db:
        return db.execute(statement, args).fetchall()


def accounts_of(r):
    return r.document().accounts


OLD_V2 = '''
CREATE TABLE accounts(namespace TEXT PRIMARY KEY, checkpoint TEXT, last_success REAL, error TEXT, work_turn INTEGER NOT NULL DEFAULT 0);
CREATE TABLE records(
 ref TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider_id TEXT NOT NULL,
 metadata TEXT NOT NULL, parts TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 attempted REAL, due REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
 UNIQUE(namespace, provider_id));
CREATE INDEX records_due ON records(namespace, due, attempted);
CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 namespace TEXT NOT NULL, ref TEXT NOT NULL, revision INTEGER NOT NULL, occurred REAL NOT NULL);
CREATE VIRTUAL TABLE texts USING fts5(ref UNINDEXED, namespace UNINDEXED, title, transcript, summary, tokenize='unicode61');
PRAGMA application_id=1145786947;
PRAGMA user_version=2;
'''


def seed_v2(doc):
    directory = Path(doc['archive']['directory'])
    directory.mkdir(mode=0o700)
    (directory / '.lock').touch(mode=0o600)
    database = directory / 'archive.sqlite'
    database.touch(mode=0o600)
    with sqlite3.connect(database) as db:
        db.executescript(OLD_V2)
        db.execute('INSERT INTO accounts(namespace,checkpoint) VALUES(?,?)', ('a' * 32, '"old-cursor"'))
        for n in range(3):
            db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)',
                       (f'{n:032x}', 'a' * 32, f'old-{n}', '{"id":"old-%d","title":"Old %d"}' % (n, n),
                        '{"transcript":{"state":"available","text":"old words %d","digest":"d%d"}}' % (n, n), 1, 50.0, 3650.0, 0))
            db.execute('INSERT INTO texts VALUES(?,?,?,?,?)', (f'{n:032x}', 'a' * 32, f'Old {n}', f'old words {n}', ''))
            db.execute('INSERT INTO events(event_id,namespace,ref,revision,occurred) VALUES(?,?,?,?,?)',
                       (f'e{n}', 'a' * 32, f'{n:032x}', 1, 50.0))
        db.commit()


OLD_COLUMNS = 'SELECT ref,namespace,provider_id,metadata,parts,revision,attempted,due,failures FROM records ORDER BY ref'


# spec: ANYM-724
def test_schema3_upgrade_is_additive_and_a_failed_upgrade_rolls_back_to_usable_v2(tmp_path, monkeypatch):
    path, doc = build(tmp_path); omi = Omi()
    seed_v2(doc)
    before = (sql(doc, OLD_COLUMNS), sql(doc, 'SELECT * FROM events ORDER BY sequence'), sql(doc, 'SELECT * FROM texts'))
    from deedkit_recordings import archive_store
    monkeypatch.setattr(archive_store, 'UPGRADE_V3', archive_store.UPGRADE_V3.replace(
        'COMMIT;', 'INSERT INTO table_that_does_not_exist VALUES(1);\nCOMMIT;'))
    async def broken():
        r = runtime(path, omi); await r.start(background=False)
        assert r.store is None and r.error == 'archive_unavailable'
    asyncio.run(broken())
    assert sql(doc, 'PRAGMA user_version') == [(2,)]
    assert sql(doc, "SELECT count(*) FROM sqlite_master WHERE name IN ('ledger','meta')") == [(0,)]
    assert sql(doc, OLD_COLUMNS) == before[0]
    monkeypatch.undo()
    async def upgraded():
        r = runtime(path, omi); await r.start(background=False)
        assert r.error is None
        await r.close()
        r = runtime(path, omi); await r.start(background=False)
        assert r.error is None
        await r.close()
    asyncio.run(upgraded())
    assert sql(doc, 'PRAGMA user_version') == [(3,)]
    assert (sql(doc, OLD_COLUMNS), sql(doc, 'SELECT * FROM events ORDER BY sequence'), sql(doc, 'SELECT * FROM texts')) == before
    archive_id, incarnation, journal_id = sql(doc, 'SELECT archive_id,incarnation,journal_id FROM meta')[0]
    assert len(archive_id) == 32 and len(journal_id) == 32 and incarnation == 1 and archive_id != journal_id
    assert sql(doc, 'SELECT count(*) FROM meta') == [(1,)]
    columns = {row[1] for row in sql(doc, 'PRAGMA table_info(accounts)')}
    assert {'throttled_until', 'traversal_state', 'passes_completed', 'last_page_at'} <= columns
    assert sql(doc, 'SELECT checkpoint FROM accounts') == [('"old-cursor"',)]


# spec: ANYM-724
def test_online_backup_accepts_schema3_and_unknown_newer_schema_is_refused(tmp_path):
    path, doc = build(tmp_path); omi = Omi(3)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        await r.run_once(now=T0)
        snapshots = tmp_path.resolve() / 'snapshots'
        snapshots.mkdir(mode=0o700)
        backup_archive(r.document().archive, str(snapshots / 'copy.sqlite'))
        with sqlite3.connect(snapshots / 'copy.sqlite') as copy:
            assert copy.execute('PRAGMA user_version').fetchone() == (3,)
            assert copy.execute('SELECT count(*) FROM records').fetchone() == (3,)
        await r.close()
    asyncio.run(run())
    sql(doc, 'PRAGMA user_version=4')
    async def refused():
        r = runtime(path, omi); await r.start(background=False)
        assert r.error == 'archive_schema_unsupported'
    asyncio.run(refused())


def peaks(omi, token):
    return omi.peak.get((token, 'reads'), 0), omi.peak.get((token, 'transcripts'), 0)


# spec: ANYM-724
def test_collector_never_exceeds_its_budget_over_72_hours_with_restarts_and_makes_progress(tmp_path):
    path, doc = build(tmp_path); omi = Omi(1500)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        for block in range(3):
            await passes(r, omi, T0 + block * 86400, 900, 96)
            await r.close()
            r = runtime(path, omi); await r.start(background=False)
        await r.close()
    asyncio.run(run())
    reads, transcripts = peaks(omi, 'tokenaaaa')
    assert reads <= 45 and transcripts <= 18
    done = sql(doc, "SELECT count(*) FROM records WHERE revision>0")[0][0]
    assert 1000 <= done <= 18 * 73
    assert sql(doc, "SELECT count(*) FROM records")[0][0] == 1500


# spec: ANYM-724
def test_collector_reservations_are_atomic_and_foreground_use_is_counted_never_refused(tmp_path):
    path, doc = build(tmp_path); omi = Omi()
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        fingerprint = hashlib.sha256(b'tokenaaaa').hexdigest()
        for _ in range(3):
            await r.count('tokenaaaa', True, now=T0)
        grants = await asyncio.gather(*[r.reserve(fingerprint, [('transcripts', 5)], T0) for _ in range(10)])
        assert sum(grants) == 2
        await asyncio.gather(*[r.count('tokenaaaa', True, now=T0 + 1) for _ in range(10)])
        assert await r.reserve(fingerprint, [('transcripts', 5)], T0 + 2) is False
        assert sql(doc, 'SELECT bucket,count FROM ledger ORDER BY bucket') == [('reads', 13), ('transcripts', 15)]
        assert await r.reserve(fingerprint, [('transcripts', 5)], T0 + 3601) is True
        assert sql(doc, "SELECT count FROM ledger WHERE bucket='transcripts'") == [(1,)]
        await r.close()
    asyncio.run(run())
    stored = Path(doc['archive']['directory'], 'archive.sqlite').read_bytes()
    assert b'tokenaaaa' not in stored and hashlib.sha256(b'tokenaaaa').hexdigest().encode() in stored
    assert {row[1] for row in sql(doc, 'PRAGMA table_info(ledger)')} >= {'credential', 'bucket', 'window_start', 'count'}


# spec: ANYM-724
@pytest.mark.parametrize('fault', ['clock_back', 'ledger_dropped', 'ledger_garbage'])
def test_uncertain_ledger_stops_only_the_collector_and_is_reported(tmp_path, fault):
    path, doc = build(tmp_path); omi = Omi(5)
    async def run():
        app = create_app(path, transport=httpx.MockTransport(omi), archive_background=False)
        async with app.router.lifespan_context(app):
            r = app.state.archive
            await r.run_once(now=T0)
            assert len(omi.of('tokenaaaa', 'detail')) == 5
            if fault == 'clock_back':
                when = T0 - 300
            else:
                when = T0 + 7200
                sql(doc, 'DROP TABLE ledger' if fault == 'ledger_dropped' else "UPDATE ledger SET count='garbage'")
            omi.add('fresh', T0, front=True)
            calls = len(omi.calls)
            omi.now = when
            await r.run_once(now=when)
            assert len(omi.calls) == calls
            account = accounts_of(r)[0]
            status = await r.status(account, now=when)
            assert status['error'] == 'ledger_uncertain' and status['record_count'] == 5
            hits = await r.db('search', account, '"Words"', 5)
            assert hits
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                         headers={'Authorization': 'Bearer backend-a'}) as client:
                read = await client.post('/v1/read_recording', json={'account': 'omi-a', 'recording_id': 'c00000'})
                assert read.status_code == 200
    asyncio.run(run())


# spec: ANYM-724
def test_two_local_accounts_with_one_credential_share_one_ledger(tmp_path):
    path, doc = build(tmp_path, accounts=(('omi-a', 'tokenshared', 'a'), ('omi-b', 'tokenshared', 'b')))
    omi = Omi(400)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        await passes(r, omi, T0, 300, 40)
        await r.close()
    asyncio.run(run())
    reads, transcripts = peaks(omi, 'tokenshared')
    assert reads <= 45 and transcripts <= 18
    assert sql(doc, 'SELECT count(DISTINCT credential) FROM ledger') == [(1,)]
    assert sql(doc, "SELECT count(*) FROM records WHERE revision>0")[0][0] > 0


# spec: ANYM-724
def test_retry_after_pauses_one_credential_without_damaging_records_and_resumes(tmp_path):
    path, doc = build(tmp_path, accounts=(('omi-a', 'tokenaaaa', 'a'), ('omi-b', 'tokenbbbb', 'b')))
    omi = Omi(5, created=T0 - 100)
    omi.limited[('tokenaaaa', 'detail')] = (T0 + 1800, '1800')
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        omi.now = T0
        await r.run_once(now=T0)
        a, b = accounts_of(r)
        assert len(omi.of('tokenaaaa', 'detail')) == 1
        assert len(omi.of('tokenbbbb', 'detail')) == 5
        assert sql(doc, 'SELECT throttled_until FROM accounts WHERE namespace=?', 'a' * 32) == [(T0 + 1800,)]
        stuck = len(omi.calls)
        for step in range(1, 6):
            omi.add(f'late-{step}', T0 + step * 300, front=True)
            await passes(r, omi, T0 + step * 300, 300, 1)
        assert len(omi.of('tokenaaaa')) == len(omi.of('tokenaaaa', 'detail')) + len(omi.of('tokenaaaa', 'list')) == 1 + 1
        assert len(omi.of('tokenbbbb', 'detail')) == 10
        status = await r.status(a, now=T0 + 1500)
        assert status['throttled_until'] == T0 + 1800 and status['error'] is None
        assert sql(doc, 'SELECT count(*) FROM records WHERE namespace=? AND (failures>0 OR parts<>?)', 'a' * 32, '{}') == [(0,)]
        await passes(r, omi, T0 + 1800, 300, 3)
        assert len(omi.of('tokenaaaa', 'detail')) > 1 and stuck
        assert sql(doc, "SELECT count(*) FROM records WHERE namespace=? AND revision>0", 'a' * 32)[0][0] == 10
        assert (await r.status(a, now=T0 + 2400))['throttled_until'] is None
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
@pytest.mark.parametrize('header,expected', [('1800', 1800), (None, 3600), ('soon', 3600), ('999999', 21600),
                                             ('-5', 3600), ('1e3', 3600)])
def test_on_demand_429_returns_provider_rate_limited_with_retry_after(tmp_path, header, expected):
    path, doc = build(tmp_path); omi = Omi(2)
    omi.limited[('tokenaaaa', 'any')] = (T0 + 10 ** 7, header)
    async def run():
        app = create_app(path, transport=httpx.MockTransport(omi), archive_background=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                         headers={'Authorization': 'Bearer backend-a'}) as client:
                for operation, body in (('list_recordings', {}), ('read_recording', {'recording_id': 'c00000'})):
                    response = await client.post('/v1/' + operation, json={'account': 'omi-a', **body})
                    assert response.status_code == 429
                    assert response.json() == {'error': 'provider_rate_limited', 'retry_after_s': expected}
            r = app.state.archive
            await r.run_once(now=T0)
            omi.now = T0
            assert len(omi.of('tokenaaaa')) == 3
    asyncio.run(run())


# spec: ANYM-724
def test_history_enumerates_1300_conversations_in_7_list_calls_without_reading_transcripts_for_it(tmp_path):
    path, doc = build(tmp_path); omi = Omi(1300)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        await passes(r, omi, T0, 10, 7)
        lists = omi.of('tokenaaaa', 'list')
        assert len(lists) == 7
        assert [c[3].get('limit') for c in lists] == ['200'] * 7
        assert lists[0][3].get('cursor') is None and lists[1][3]['cursor'] == 'c200'
        assert sql(doc, 'SELECT count(*) FROM records')[0][0] == 1300
        status = await r.status(accounts_of(r)[0], now=T0 + 70)
        assert status['traversal']['state'] == 'reached_end' and status['traversal']['discovered_total'] == 1300
        assert status['traversal']['passes_completed'] == 7
        await passes(r, omi, T0 + 80, 10, 5)
        assert len(omi.of('tokenaaaa', 'list')) == 7
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_truncated_page_is_reread_never_taken_as_the_end(tmp_path):
    path, doc = build(tmp_path); omi = Omi(30)
    omi.truncated = 2
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        await passes(r, omi, T0, 10, 1)
        status = await r.status(accounts_of(r)[0], now=T0)
        assert status['traversal']['state'] == 'not_started'
        await passes(r, omi, T0 + 10, 10, 1)
        lists = omi.of('tokenaaaa', 'list')
        assert [c[3].get('cursor') for c in lists] == [None, None]
        await passes(r, omi, T0 + 20, 10, 1)
        assert (await r.status(accounts_of(r)[0], now=T0 + 30))['traversal']['state'] == 'reached_end'
        assert sql(doc, 'SELECT count(*) FROM records') == [(30,)]
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_kill_between_id_insert_and_checkpoint_commit_resumes_with_no_duplicate_or_skipped_id(tmp_path):
    path, doc = build(tmp_path); omi = Omi(450)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        def crash(): raise RuntimeError('synthetic kill before commit')
        original = r.db
        async def crashing(method, *args):
            if method == 'discover':
                args = (*args[:4], crash, *args[5:])
            return await original(method, *args)
        r.db = crashing
        omi.now = T0
        await r.run_once(now=T0)
        assert r.error == 'archive_unavailable'
        assert sql(doc, 'SELECT count(*) FROM records') == [(0,)]
        r.db = original
        await r.close()
        r = runtime(path, omi); await r.start(background=False)
        await passes(r, omi, T0 + 10, 10, 3)
        ids = [row[0] for row in sql(doc, 'SELECT provider_id FROM records')]
        assert len(ids) == len(set(ids)) == 450 and set(ids) == {i['id'] for i in omi.items}
        assert (await r.status(account, now=T0 + 40))['traversal']['state'] == 'reached_end'
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_full_queue_pauses_only_history_and_still_discovers_and_fetches_a_conversation_created_now(tmp_path):
    path, doc = build(tmp_path, max_queued=200, omi_budget={'transcript_per_hour': 8})
    omi = Omi(400, created=T0 - 86400 * 40)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        await passes(r, omi, T0, 1, 1)
        status = await r.status(account, now=T0)
        assert status['traversal']['state'] == 'in_progress' and status['queued'] == 92
        await r.db('discover', account, [{'id': f'filler-{n}'} for n in range(8)], 'c100', T0, lambda: None)
        assert (await r.status(account, now=T0))['queued'] == 100
        omi.add('born-now', T0 + 200, front=True)
        await passes(r, omi, T0 + 300, 1, 1)
        assert not [c for c in omi.of('tokenaaaa', 'list') if c[3].get('cursor')]
        assert sql(doc, "SELECT revision FROM records WHERE provider_id='born-now'") == [(0,)]
        await passes(r, omi, T0 + 3700, 1, 1)
        assert [c[3] for c in omi.of('tokenaaaa', 'detail') if c[0] >= T0 + 3700][0] == 'born-now'
        assert sql(doc, "SELECT revision FROM records WHERE provider_id='born-now'") == [(1,)]
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_head_page_larger_than_the_free_slice_loses_no_id_across_passes(tmp_path, monkeypatch):
    monkeypatch.setattr('deedkit_recordings.archive_store.QUEUE_LIMIT', 20)
    path, doc = build(tmp_path); omi = Omi(0)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        await r.db('discover', account, [{'id': f'old-{n}'} for n in range(10)], 'cursor-old', T0, lambda: None)
        for n in range(20):
            omi.add(f'new-{n:03d}', T0 - n)
        await passes(r, omi, T0 + 1000, 3600, 1)
        assert sql(doc, "SELECT count(*) FROM records WHERE provider_id LIKE 'new-%'") == [(10,)]
        status = await r.status(account, now=T0 + 1000)
        assert status['head_backlog'] is True and status['head_slice_full'] is False
        await passes(r, omi, T0 + 1000 + 3600, 3600, 6)
        assert sql(doc, "SELECT count(*) FROM records WHERE provider_id LIKE 'new-%'") == [(20,)]
        assert (await r.status(account, now=T0 + 40000))['head_backlog'] is False
        sizes = [int(c[3]['limit']) for c in omi.of('tokenaaaa', 'list')]
        assert sizes[0] == 10 and max(sizes) <= 10
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_new_conversation_is_fetched_before_a_five_thousand_backlog_and_backlog_goes_newest_first(tmp_path):
    path, doc = build(tmp_path); omi = Omi(0)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        backlog = [{'id': f'old-{n:04d}', 'created_at': iso(T0 - 86400 * 20 - n * 60)} for n in range(5000)]
        await r.db('discover', account, backlog, 'cursor-old', T0, lambda: None)
        for item in backlog:
            omi.items.append({'id': item['id'], 'created_at': item['created_at'], 'processing_deferred': False,
                              'structured': {'title': 't', 'overview': 'o'}, 'text': 'x'})
        omi.add('born-now', T0 + 5, front=True)
        omi.now = T0 + 10
        await r.run_once(now=T0 + 10)
        details = [c[3] for c in omi.of('tokenaaaa', 'detail')]
        assert details[0] == 'born-now'
        assert details[1:4] == ['old-0000', 'old-0001', 'old-0002']
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_two_hundred_unchanged_saved_records_cost_zero_transcript_calls_in_24_hours(tmp_path):
    path, doc = build(tmp_path, omi_budget={'transcript_per_hour': 25}); omi = Omi(200, created=T0 - 86400 * 3)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        for hour in range(10):
            omi.now = T0 + hour * 3600
            await r.run_once(now=omi.now)
        assert sql(doc, 'SELECT count(*) FROM records WHERE revision>0') == [(200,)]
        before = len(omi.of('tokenaaaa', 'detail'))
        await passes(r, omi, T0 + 11 * 3600, 900, 96)
        assert len(omi.of('tokenaaaa', 'detail')) == before
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_late_summary_is_picked_up_by_the_bounded_pending_retry_and_never_hourly(tmp_path):
    path, doc = build(tmp_path); omi = Omi(0)
    item = omi.add('late', T0, overview=None)
    omi.hide_overview = True
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        await passes(r, omi, T0 + 3600, 3600, 1)
        record = await r.db('record', account, 'late')
        assert record['parts']['summary']['attempt_state'] == 'unavailable' and record['parts']['transcript']['text']
        item['structured']['overview'] = 'Decided to build a telescope'
        await passes(r, omi, T0 + 2 * 3600, 3600, 80)
        record = await r.db('record', account, 'late')
        assert record['parts']['summary']['text'] == 'Decided to build a telescope'
        details = omi.of('tokenaaaa', 'detail')
        assert len(details) <= 1 + 8 + 4
        gaps = [b[0] - a[0] for a, b in zip(details, details[1:])]
        assert min(gaps) >= 3 * 3600
        late_pick = max(c[0] for c in details)
        assert late_pick <= T0 + 48 * 3600 + 25 * 3600 or details[-1][0] <= T0 + 2 * 3600 + 80 * 3600
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_title_only_change_refreshes_the_title_without_a_transcript_call_and_overview_change_refetches(tmp_path):
    path, doc = build(tmp_path); omi = Omi(3, created=T0 - 86400 * 3)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        await passes(r, omi, T0, 10, 1)
        assert len(omi.of('tokenaaaa', 'detail')) == 3
        omi.items[0]['structured']['title'] = 'Renamed zebra'
        await passes(r, omi, T0 + 400, 10, 1)
        assert len(omi.of('tokenaaaa', 'detail')) == 3
        hits = await r.db('search', account, '"zebra"', 5)
        assert [h['provider_recording_id'] for h in hits] == ['c00000'] and hits[0]['title'] == 'Renamed zebra'
        omi.items[1]['structured']['overview'] = 'A different overview'
        await passes(r, omi, T0 + 800, 10, 1)
        assert [c[3] for c in omi.of('tokenaaaa', 'detail')][3:] == ['c00001']
        assert (await r.db('record', account, 'c00001'))['parts']['summary']['text'] == 'A different overview'
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_status_reports_progress_capacity_and_budget_without_any_content(tmp_path):
    path, doc = build(tmp_path); omi = Omi(40, created=T0 - 86400 * 3)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        status = await r.status(account, now=T0)
        assert status['traversal'] == {'state': 'not_started', 'discovered_total': 0, 'passes_completed': 0, 'last_page_at': None}
        await passes(r, omi, T0, 600, 4)
        status = await r.status(account, now=T0 + 2400)
        assert status['traversal']['state'] == 'reached_end' and status['traversal']['discovered_total'] == 40
        assert status['with_text'] == 18 == 40 - status['queued'] and status['failed'] == 0 and status['pending'] == 0
        assert status['coverage'] == 'unknown' and status['throttled_until'] is None
        assert status['usage']['transcripts'] == {'used': 18, 'limit': 18, 'resets_at': T0 + 3600}
        assert status['usage']['reads']['limit'] == 45 and status['usage']['reads']['used'] >= 19
        assert status['db_bytes'] > 0 and status['bytes_ceiling'] == 1024 ** 3
        assert status['eta'] is None and status['capacity_warning'] is False
        assert status['bytes_ceiling'] > 0 and status['projected_db_bytes'] >= status['db_bytes'] // 4
        text = json.dumps(status)
        assert 'Words of' not in text and 'Title c' not in text and 'overview' not in text.lower()
        later = await r.status(account, now=T0 + 7200)
        assert later['eta'] is not None and later['eta'] > 0 and later['usage']['transcripts']['used'] == 0
        small = r.store.settings.model_copy(update={'max_bytes': 20_000})
        r.store.settings = small
        assert (await r.status(account, now=T0 + 7200))['capacity_warning'] is True
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
@pytest.mark.parametrize('budget', [{'reads_per_hour': 11}, {'reads_per_hour': 61}, {'transcript_per_hour': 0},
                                    {'transcript_per_hour': 26}, {'unknown': 1}])
def test_budget_and_queue_settings_are_validated(budget):
    with pytest.raises(ValidationError):
        ArchiveSettings(directory='/data/archive', omi_budget=budget)
    for queued in (199, 200001):
        with pytest.raises(ValidationError):
            ArchiveSettings(directory='/data/archive', max_queued=queued)
    defaults = ArchiveSettings(directory='/data/archive')
    assert (defaults.omi_budget.transcript_per_hour, defaults.omi_budget.reads_per_hour) == (18, 45)
    assert defaults.max_queued is None
    assert ArchiveSettings(directory='/data/archive', omi_budget={'transcript_per_hour': 25, 'reads_per_hour': 60}, max_queued=200000)


# spec: ANYM-724
def test_foreground_reads_are_ledgered_never_refused_and_use_up_the_collector_share(tmp_path):
    path, doc = build(tmp_path, omi_budget={'transcript_per_hour': 1}); omi = Omi(3, created=T0 - 86400 * 3)
    async def run():
        app = create_app(path, transport=httpx.MockTransport(omi), archive_background=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                         headers={'Authorization': 'Bearer backend-a'}) as client:
                assert (await client.post('/v1/list_recordings', json={'account': 'omi-a'})).status_code == 200
                for _ in range(3):
                    assert (await client.post('/v1/read_recording', json={'account': 'omi-a', 'recording_id': 'c00000'})).status_code == 200
            counts = dict(sql(doc, 'SELECT bucket,count FROM ledger'))
            assert counts == {'reads': 4, 'transcripts': 3}
            before = len(omi.of('tokenaaaa', 'detail'))
            omi.now = T0
            await app.state.archive.run_once(now=T0)
            assert len(omi.of('tokenaaaa', 'detail')) == before
    asyncio.run(run())


# spec: ANYM-724
def test_production_example_sizes_the_archive_at_4_gib_and_page_cap_follows_the_one_third_rule(tmp_path):
    example = json.loads((Path(__file__).resolve().parents[2] / 'deploy/recordings-archive.example.json').read_text())
    settings = ArchiveSettings(**example['archive'])
    assert settings.max_bytes == 4 * 1024 ** 3 and settings.omi_budget.transcript_per_hour == 18
    path, doc = build(tmp_path, max_bytes=settings.max_bytes)
    async def run():
        r = runtime(path, Omi()); await r.start(background=False)
        with r.store.connection(readonly=False) as db:
            size = db.execute('PRAGMA page_size').fetchone()[0]
            assert db.execute('PRAGMA max_page_count').fetchone()[0] == settings.max_bytes // (3 * size)
        await r.close()
    asyncio.run(run())
