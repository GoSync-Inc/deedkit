"""Omi full backfill (ANYM-724): schema 3, budget ledger, 429, enumeration, ordering, progress. Synthetic Omi only."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

import httpx
from pydantic import ValidationError
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'services/recordings/src'))
from deedkit_recordings.app import create_app
from deedkit_recordings.archive import ArchiveRuntime
from deedkit_recordings.archive_store import backup_archive, namespace
from deedkit_recordings.omi import Provider
from deedkit_recordings.security import ArchiveSettings, Failure

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
        self.truncated, self.hide_overview, self.no_headers, self.on_list = 0, False, False, None
        self.truncated_header = 'echo'  # 'echo': 'true'/'false'; 'omit': only 'true' is sent, as Omi does; other: raw value
        self.gone, self.unlisted, self.after_list, self.details = set(), set(), None, []  # 404 on detail; not listed
        self.failing = set()  # 503 on detail
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
        transcript = not listing and request.url.params.get('include_transcript') == 'true'
        self.count(token, ['reads', 'transcripts'] if transcript else ['reads'])
        if not listing:
            ident = request.url.path.rsplit('/', 1)[-1]
            self.details.append((ident, request.url.params.get('include_transcript')))
            if ident in self.gone:
                return httpx.Response(404)
            if ident in self.failing:
                return httpx.Response(503)
            item = next((i for i in self.items if i['id'] == ident), None)
            return httpx.Response(200, json=self.view(item, True)) if item else httpx.Response(404)
        if self.on_list:
            self.on_list()
        params = request.url.params
        size, cursor = int(params['limit']), params.get('cursor')
        assert 1 <= size <= 200 and params.get('include_transcript') == 'false' and params.get('offset') == '0'
        start = int(cursor[1:]) if cursor else 0
        listed = [i for i in self.items if i['id'] not in self.unlisted]
        page = listed[start:start + size]
        more = start + size < len(listed)
        if self.after_list:
            self.after_list()
        truncated = self.truncated > 0
        if truncated:
            self.truncated -= 1
        headers = {'X-Omi-Has-More': str(more).lower(), 'X-Omi-List-Truncated': str(truncated).lower(),
                   'X-Omi-Effective-Limit': str(size)}
        if self.truncated_header == 'omit' and not truncated:
            del headers['X-Omi-List-Truncated']
        elif self.truncated_header not in ('echo', 'omit'):
            headers['X-Omi-List-Truncated'] = self.truncated_header
        if self.no_headers:
            return httpx.Response(200, json=[self.view(i, False) for i in page])
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


def seed_v2(doc, ns='a' * 32):
    directory = Path(doc['archive']['directory'])
    directory.mkdir(mode=0o700)
    (directory / '.lock').touch(mode=0o600)
    database = directory / 'archive.sqlite'
    database.touch(mode=0o600)
    with sqlite3.connect(database) as db:
        db.executescript(OLD_V2)
        db.execute('INSERT INTO accounts(namespace,checkpoint) VALUES(?,?)', (ns, '"old-cursor"'))
        for n in range(3):
            db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)',
                       (f'{n:032x}', ns, f'old-{n}', '{"id":"old-%d","title":"Old %d"}' % (n, n),
                        '{"transcript":{"state":"available","text":"old words %d","digest":"d%d"}}' % (n, n), 1, 50.0, 3650.0, 0))
            db.execute('INSERT INTO texts VALUES(?,?,?,?,?)', (f'{n:032x}', ns, f'Old {n}', f'old words {n}', ''))
            db.execute('INSERT INTO events(event_id,namespace,ref,revision,occurred) VALUES(?,?,?,?,?)',
                       (f'e{n}', ns, f'{n:032x}', 1, 50.0))
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
    meta = dict(sql(doc, 'SELECT key,value FROM meta'))
    archive_id, incarnation, journal_id = meta['archive_id'], meta['incarnation'], meta['journal_id']
    assert len(archive_id) == 32 and len(journal_id) == 32 and incarnation == 1 and archive_id != journal_id
    assert sql(doc, "SELECT key,typeof(value) FROM meta WHERE key NOT LIKE 'index_%' ORDER BY key") == [
        ('archive_id', 'text'), ('incarnation', 'integer'), ('journal_id', 'text')]
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
            assert copy.execute('PRAGMA user_version').fetchone() == (5,)  # ANYM-725/730: a new archive starts at schema 5
            assert copy.execute('SELECT count(*) FROM records').fetchone() == (3,)
        await r.close()
    asyncio.run(run())
    sql(doc, 'PRAGMA user_version=6')
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
            await passes(r, omi, T0 + block * 86400, 450, 192)
            await r.close()
            r = runtime(path, omi); await r.start(background=False)
        await r.close()
    asyncio.run(run())
    reads, transcripts = peaks(omi, 'tokenaaaa')
    assert reads <= 45 and transcripts <= 18
    done = sql(doc, "SELECT count(*) FROM records WHERE revision>0")[0][0]
    assert 1000 <= done <= 18 * 73  # budget-bound: 18 an hour, not pass-bound
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
        assert sql(doc, 'SELECT bucket,sum(count) FROM ledger GROUP BY bucket ORDER BY bucket') == [('reads', 13), ('transcripts', 15)]
        assert await r.reserve(fingerprint, [('transcripts', 5)], T0 + 3800) is True
        # Rows older than the sliding hour (plus margin) no longer count, though they stay as clock evidence for 2 h.
        assert sql(doc, "SELECT sum(count) FROM ledger WHERE bucket='transcripts' AND window_start>?", T0 + 3800 - 3675) == [(1,)]
        await r.close()
    asyncio.run(run())
    stored = Path(doc['archive']['directory'], 'archive.sqlite').read_bytes()
    assert b'tokenaaaa' not in stored and hashlib.sha256(b'tokenaaaa').hexdigest().encode() in stored
    assert {row[1] for row in sql(doc, 'PRAGMA table_info(ledger)')} >= {'credential', 'bucket', 'window_start', 'count'}


# spec: ANYM-724
def test_reservation_window_slides_so_an_undispatched_reservation_never_frees_a_slot_early(tmp_path):
    path, doc = build(tmp_path); omi = Omi()
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        credential = hashlib.sha256(b'tokenaaaa').hexdigest()
        share = [('transcripts', 18)]
        assert await r.reserve(credential, share, T0) is True  # reserved, then the process died before it dispatched
        grants = [await r.reserve(credential, share, T0 + 600) for _ in range(20)]
        assert sum(grants) == 17
        # A fixed window of ours would reset here, while Omi's, started by the call at T0 + 600, runs to T0 + 4200.
        assert await r.reserve(credential, share, T0 + 3600) is False
        assert await r.reserve(credential, share, T0 + 3700) is True
        assert await r.reserve(credential, share, T0 + 3700) is False
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_a_foreground_read_cannot_erase_the_evidence_of_a_clock_that_stepped_back(tmp_path):
    path, doc = build(tmp_path); omi = Omi()
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        credential = hashlib.sha256(b'tokenaaaa').hexdigest()
        assert await r.reserve(credential, [('reads', 45)], T0) is True
        assert await r.reserve(credential, [('reads', 45)], T0 + 500) is True
        await r.count('tokenaaaa', False, now=T0 + 200)
        with pytest.raises(Failure, match='ledger_uncertain'):
            await r.reserve(credential, [('reads', 45)], T0 + 200)
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_a_429_recorded_meanwhile_stops_the_next_collector_reservation_even_with_budget_left(tmp_path):
    path, doc = build(tmp_path); omi = Omi()
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        credential = hashlib.sha256(b'tokenaaaa').hexdigest()
        await r.db('throttle', credential, [], T0 + 1800)
        assert await r.reserve(credential, [('reads', 45)], T0 + 10) is False
        assert await r.reserve(credential, [('reads', 45)], T0 + 1800) is True
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
@pytest.mark.parametrize('fault', ['clock_back', 'ledger_dropped', 'ledger_garbage'])
def test_uncertain_ledger_stops_only_the_collector_and_is_reported(tmp_path, fault):
    path, doc = build(tmp_path); omi = Omi(5)
    async def run():
        app = create_app(path, transport=httpx.MockTransport(omi), archive_background=False)
        async with app.router.lifespan_context(app):
            r = app.state.archive
            await passes(r, omi, T0, 10, 2)
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
        ns = namespace(a)
        assert len(omi.of('tokenaaaa', 'detail')) == 1
        assert len(omi.of('tokenbbbb', 'detail')) == 3
        assert sql(doc, 'SELECT throttled_until FROM accounts WHERE namespace=?', ns) == [(T0 + 1800,)]
        stuck = len(omi.calls)
        for step in range(1, 6):
            omi.add(f'late-{step}', T0 + step * 300, front=True)
            await passes(r, omi, T0 + step * 300, 300, 1)
        assert len(omi.of('tokenaaaa')) == len(omi.of('tokenaaaa', 'detail')) + len(omi.of('tokenaaaa', 'list')) == 1 + 1
        assert len(omi.of('tokenbbbb', 'detail')) == 10
        status = await r.status(a, now=T0 + 1500)
        assert status['throttled_until'] == T0 + 1800 and status['error'] is None
        assert sql(doc, 'SELECT count(*) FROM records WHERE namespace=? AND (failures>0 OR parts<>?)', ns, '{}') == [(0,)]
        await passes(r, omi, T0 + 1800, 300, 5)
        assert len(omi.of('tokenaaaa', 'detail')) > 1 and stuck
        assert sql(doc, "SELECT count(*) FROM records WHERE namespace=? AND revision>0", ns)[0][0] == 10
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
def test_truncated_head_page_replayed_does_not_strand_ids_behind_the_next_cursor(tmp_path):
    path, doc = build(tmp_path); omi = Omi(450, created=T0 - 86400 * 40)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        await passes(r, omi, T0, 10, 3)
        assert (await r.status(account, now=T0 + 30))['traversal']['state'] == 'reached_end'
        for n in range(300):
            omi.add(f'new-{n:03d}', T0 + 100 + n, front=True)
        omi.truncated = 1
        await passes(r, omi, T0 + 1000, 400, 5)
        assert sql(doc, "SELECT count(*) FROM records WHERE provider_id LIKE 'new-%'") == [(300,)]
        assert (await r.status(account, now=T0 + 3000))['head_backlog'] is False
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_a_page_without_end_of_list_evidence_never_marks_the_history_finished(tmp_path):
    path, doc = build(tmp_path); omi = Omi(30)
    omi.no_headers = True
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        await passes(r, omi, T0, 10, 1)
        status = await r.status(account, now=T0)
        assert status['traversal']['state'] == 'not_started' and status['error'] == 'invalid_provider_response'
        assert sql(doc, 'SELECT count(*) FROM records') == [(30,)]
        omi.no_headers = False
        await passes(r, omi, T0 + 10, 10, 1)
        status = await r.status(account, now=T0 + 10)
        assert status['traversal']['state'] == 'reached_end' and status['error'] is None
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_a_last_page_without_the_truncation_header_ends_the_history_as_omi_sends_it(tmp_path):
    path, doc = build(tmp_path); omi = Omi(450, created=T0 - 86400 * 40)
    omi.truncated_header, omi.truncated = 'omit', 1
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        await passes(r, omi, T0, 10, 4)
        assert [c[3].get('cursor') for c in omi.of('tokenaaaa', 'list')] == [None, None, 'c200', 'c400']
        status = await r.status(account, now=T0 + 40)
        assert status['traversal']['state'] == 'reached_end' and status['error'] is None
        assert sql(doc, 'SELECT count(*) FROM records') == [(450,)]
        calls = len(omi.of('tokenaaaa', 'list'))
        await passes(r, omi, T0 + 400, 400, 3)
        assert len(omi.of('tokenaaaa', 'list')) == calls + 3  # the head only; the last page is not read again
        assert (await r.status(account, now=T0 + 1600))['error'] is None
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_an_account_smaller_than_one_page_ends_on_its_first_page_and_its_head_reads_raise_nothing(tmp_path):
    path, doc = build(tmp_path); omi = Omi(5, created=T0 - 86400 * 2)
    omi.truncated_header = 'omit'
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        await passes(r, omi, T0, 400, 4)
        status = await r.status(account, now=T0 + 1600)
        assert status['traversal']['state'] == 'reached_end' and status['error'] is None
        assert len(omi.of('tokenaaaa', 'list')) == 4 and r.error is None
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
@pytest.mark.parametrize('value', ['maybe', '', '1'])
def test_a_malformed_truncation_header_never_ends_the_history(tmp_path, value):
    path, doc = build(tmp_path); omi = Omi(30)
    omi.truncated_header = value
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        await passes(r, omi, T0, 10, 1)
        status = await r.status(accounts_of(r)[0], now=T0)
        assert status['traversal']['state'] == 'not_started' and status['error'] == 'invalid_provider_response'
        assert sql(doc, 'SELECT count(*) FROM records') == [(30,)]
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_a_finished_history_is_walked_again_daily_so_older_title_changes_are_noticed(tmp_path):
    path, doc = build(tmp_path); omi = Omi(300, created=T0 - 86400 * 40)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        await passes(r, omi, T0, 10, 3)
        omi.items[250]['structured']['title'] = 'Renamed deep zebra'
        await passes(r, omi, T0 + 3600, 400, 4)
        assert await r.db('search', account, '"zebra"', 5) == []
        calls = len(omi.of('tokenaaaa', 'list'))
        await passes(r, omi, T0 + 86500, 10, 3)
        assert [h['provider_recording_id'] for h in await r.db('search', account, '"zebra"', 5)] == ['c00250']
        assert len(omi.of('tokenaaaa', 'list')) <= calls + 4
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_clearing_a_title_clears_the_searchable_title_without_a_transcript_call(tmp_path):
    path, doc = build(tmp_path); omi = Omi(1, created=T0 - 86400 * 3)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        await passes(r, omi, T0, 10, 1)
        assert await r.db('search', account, '"Title"', 5)
        omi.items[0]['structured']['title'] = ''
        await passes(r, omi, T0 + 400, 10, 1)
        assert await r.db('search', account, '"Title"', 5) == []
        assert len(omi.of('tokenaaaa', 'detail')) == 1
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_queue_at_the_cap_blocks_the_head_only_until_a_slot_frees(tmp_path):
    path, doc = build(tmp_path, max_queued=200); omi = Omi(0)
    for n in range(200):
        omi.add(f'q{n:03d}', T0 - 86400 * 30 - n)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        old = [{'id': f'q{n:03d}', 'created_at': iso(T0 - 86400 * 30 - n)} for n in range(200)]
        await r.db('discover', account, old[:100], None, T0, lambda: None)
        await r.db('discover', account, old[100:], None, T0, lambda: None, 'head')
        assert (await r.status(account, now=T0))['queued'] == 200
        omi.add('born', T0 + 400, front=True)
        await passes(r, omi, T0 + 400, 1, 1)
        status = await r.status(account, now=T0 + 400)
        assert status['head_backlog'] is True and await r.db('record', account, 'born') is None
        await passes(r, omi, T0 + 800, 1, 1)
        assert (await r.db('record', account, 'born'))['revision'] == 1
        assert (await r.status(account, now=T0 + 800))['head_backlog'] is False
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
    path, doc = build(tmp_path, max_queued=200, omi_budget={'transcript_per_hour': 3})
    omi = Omi(400, created=T0 - 86400 * 40)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        await passes(r, omi, T0, 1, 1)
        status = await r.status(account, now=T0)
        assert status['traversal']['state'] == 'in_progress' and status['queued'] == 97
        await r.db('discover', account, [{'id': f'filler-{n}'} for n in range(3)], 'c100', T0, lambda: None)
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
        assert details[1:3] == ['old-0000', 'old-0001']
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_one_transcript_an_hour_still_serves_a_failed_record_among_a_new_backlog(tmp_path):
    path, doc = build(tmp_path, omi_budget={'transcript_per_hour': 1}); omi = Omi(6, created=T0 - 86400 * 2)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        await passes(r, omi, T0, 10, 1)
        await r.db('observe', account, 'c00005', {'id': 'c00005'}, {'summary': ('error', '', 'provider_unavailable')}, T0)
        for hour in range(1, 5):
            await passes(r, omi, T0 + hour * 3600, 300, 13)
        details = [c[3] for c in omi.of('tokenaaaa', 'detail')]
        assert 'c00005' in details and len(set(details)) >= 4
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_thirty_records_gone_at_omi_use_no_transcript_reads_while_five_new_ones_are_fetched_within_the_hour(tmp_path):
    path, doc = build(tmp_path); omi = Omi(30, created=T0 - 86400 * 40)
    gone = {item['id'] for item in omi.items}
    def vanish():  # listed once, then merged away before their first read: 404 on detail, absent from lists
        omi.gone.update(gone); omi.unlisted.update(gone); omi.after_list = None
    omi.after_list = vanish
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        await passes(r, omi, T0, 60, 6 * 60)
        status = await r.status(account, now=omi.now)
        assert status['provider_missing'] == 30 and status['failed'] == 0 and status['pending'] == 0
        assert all(sum(1 for ident, _ in omi.details if ident == g) == 2 for g in gone)  # the two 404s, nothing more
        before = len(omi.details)
        for n in range(5):
            omi.add(f'new-{n}', omi.now, front=True)
        await passes(r, omi, omi.now + 60, 60, 60)
        later = omi.details[before:]
        assert sorted(ident for ident, _ in later) == [f'new-{n}' for n in range(5)]
        assert sql(doc, "SELECT count(*) FROM records WHERE provider_id LIKE 'new-%' AND revision>0") == [(5,)]
        back = sorted(gone)[:3]  # listed again (an unlocked conversation): due now, read in full
        omi.gone.difference_update(back); omi.unlisted.difference_update(back)
        before = len(omi.details)
        await passes(r, omi, omi.now + 86400, 60, 3)
        assert sorted(omi.details[before:]) == [(ident, 'true') for ident in back]
        assert (await r.status(account, now=omi.now))['provider_missing'] == 27
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_a_missing_record_keeps_its_text_and_is_checked_weekly_without_a_transcript(tmp_path):
    path, doc = build(tmp_path); omi = Omi()
    item = omi.add('rec-1', T0 - 3600)
    item['processing_deferred'] = True  # summary pending: the record is read again in 3 hours
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        await passes(r, omi, T0, 60, 1)
        omi.gone.add('rec-1'); omi.unlisted.add('rec-1')
        await passes(r, omi, T0 + 3 * 3600 + 60, 120, 3)
        assert omi.details == [('rec-1', 'true')] * 3
        assert (await r.status(account, now=omi.now))['provider_missing'] == 1
        assert sql(doc, "SELECT json_extract(parts,'$.transcript.text') LIKE '%\"Words of rec-1\"%' FROM records") == [(1,)]
        await passes(r, omi, omi.now + 3600, 3600, 15 * 24)
        assert omi.details[3:] == [('rec-1', 'false'), ('rec-1', 'false')]  # weekly, no transcript
        omi.gone.discard('rec-1')  # found by the next weekly check, still unlisted: read in full again
        await passes(r, omi, omi.now + 3600, 3600, 7 * 24)
        assert omi.details[5:] == [('rec-1', 'false'), ('rec-1', 'true')]
        assert (await r.status(account, now=omi.now))['provider_missing'] == 0
        assert sql(doc, "SELECT json_extract(parts,'$.transcript.text') LIKE '%\"Words of rec-1\"%' FROM records") == [(1,)]
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_a_missing_record_whose_check_fails_is_still_checked_at_most_weekly(tmp_path):
    path, doc = build(tmp_path); omi = Omi()
    omi.add('rec-1', T0 - 86400 * 3)
    omi.gone.add('rec-1'); omi.unlisted.add('rec-1')
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        await r.db('discover', account, [{'id': 'rec-1'}], None, T0 - 10)
        await passes(r, omi, T0, 120, 2)
        assert (await r.status(account, now=omi.now))['provider_missing'] == 1
        omi.gone.discard('rec-1'); omi.failing.add('rec-1')  # each check now meets an outage
        await passes(r, omi, omi.now + 3600, 3600, 22 * 24)
        checks = [moment for moment, _, kind, ident in omi.calls if kind == 'detail' and ident == 'rec-1'][2:]
        assert len(checks) == 3 and all(b - a >= 7 * 86400 for a, b in zip(checks, checks[1:]))
        assert omi.details[2:] == [('rec-1', 'false')] * 3
        assert omi.peak.get(('tokenaaaa', 'transcripts'), 0) <= 2  # the two 404 reads; checks spend no transcript
        assert (await r.status(account, now=omi.now))['provider_missing'] == 1
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_a_response_too_large_record_backs_off_a_day_not_an_hour(tmp_path):
    path, doc = build(tmp_path); omi = Omi(1, created=T0 - 86400 * 3)
    omi.items[0]['text'] = 'x' * 4096
    async def run():
        r = ArchiveRuntime(path, Provider(httpx.MockTransport(omi), maximum=2048), None); await r.start(background=False)
        account = accounts_of(r)[0]
        await passes(r, omi, T0, 300, 12 * 23)
        assert len(omi.details) == 1
        assert (await r.status(account, now=omi.now))['failed'] == 1
        await passes(r, omi, T0 + 86400 + 60, 300, 2)
        assert len(omi.details) == 2
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_a_conversation_of_the_last_day_keeps_the_reserved_reads_when_a_retry_is_denied_first(tmp_path):
    path, doc = build(tmp_path); omi = Omi(0)
    omi.add('old-failed', T0 - 86400 * 30)
    omi.add('fresh-one', T0, front=True)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        await r.db('discover', account, [{'id': 'old-failed', 'created_at': iso(T0 - 86400 * 30)},
                                         {'id': 'fresh-one', 'created_at': iso(T0)}], None, T0, lambda: None)
        await r.db('observe', account, 'old-failed', {'id': 'old-failed'}, {'summary': ('error', '', 'provider_unavailable')}, T0)
        await r.db('rotate', account)
        for _ in range(39):
            await r.count('tokenaaaa', False, now=T0 + 100)
        await passes(r, omi, T0 + 200, 1, 1)
        details = [c[3] for c in omi.of('tokenaaaa', 'detail')]
        assert details == ['fresh-one']
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_two_hundred_unchanged_saved_records_cost_zero_transcript_calls_in_24_hours(tmp_path):
    path, doc = build(tmp_path, omi_budget={'transcript_per_hour': 25}); omi = Omi(200, created=T0 - 86400 * 3)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        await passes(r, omi, T0, 300, 168)
        assert sql(doc, 'SELECT count(*) FROM records WHERE revision>0') == [(200,)]
        before = len(omi.of('tokenaaaa', 'detail'))
        await passes(r, omi, T0 + 15 * 3600, 900, 96)
        assert len(omi.of('tokenaaaa', 'detail')) == before
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_summary_arriving_48_hours_after_creation_is_picked_up_within_the_daily_retry_never_hourly(tmp_path):
    path, doc = build(tmp_path); omi = Omi(0)
    item = omi.add('late', T0, overview=None)
    omi.hide_overview = True
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        found_at = None
        for hour in range(1, 100):
            moment = T0 + hour * 3600
            if hour == 48:
                item['structured']['overview'] = 'Decided to build a telescope'
            await passes(r, omi, moment, 1, 1)
            if found_at is None and (await r.db('record', account, 'late'))['parts']['summary'].get('text'):
                found_at = moment
        assert found_at is not None and T0 + 48 * 3600 < found_at <= T0 + 48 * 3600 + 24 * 3600 + 3600
        details = [c[0] for c in omi.of('tokenaaaa', 'detail')]
        assert min(b - a for a, b in zip(details, details[1:])) >= 3 * 3600
        after_day_one = [t for t in details if t >= T0 + 25 * 3600]
        assert after_day_one and all(b - a >= 24 * 3600 for a, b in zip(after_day_one, after_day_one[1:]))
        first_day = [t for t in details if t < T0 + 24 * 3600]
        assert len(first_day) <= 9 and len([t for t in details if T0 + 24 * 3600 <= t < T0 + 48 * 3600]) <= 2
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
        await passes(r, omi, T0, 600, 6)
        status = await r.status(account, now=T0 + 3000)
        assert status['traversal']['state'] == 'reached_end' and status['traversal']['discovered_total'] == 40
        assert status['with_text'] == 18 == 40 - status['queued'] and status['failed'] == 0 and status['pending'] == 0
        assert status['coverage'] == 'unknown' and status['throttled_until'] is None
        assert status['usage']['transcripts'] == {'used': 18, 'limit': 18, 'resets_at': T0 + 3675}
        assert status['usage']['reads']['limit'] == 45 and status['usage']['reads']['used'] >= 19
        assert status['db_bytes'] > 0 and status['bytes_ceiling'] == 1024 ** 3
        assert status['eta'] is None and status['capacity_warning'] is False
        assert status['bytes_ceiling'] > 0 and status['projected_db_bytes'] is not None and status['projected_db_bytes'] > 0
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
def test_a_slow_earlier_call_in_the_pass_moves_the_reservation_and_the_429_expiry_to_the_real_time(tmp_path, monkeypatch):
    path, doc = build(tmp_path, omi_budget={'transcript_per_hour': 1}); omi = Omi(2, created=T0 - 86400 * 2)
    clock = [0.0]
    monkeypatch.setattr('deedkit_recordings.archive.monotonic', lambda: clock[0])
    omi.on_list = lambda: clock.__setitem__(0, clock[0] + 120)  # the list call (like a slow PLAUD pass) takes 120 s
    omi.limited[('tokenaaaa', 'detail')] = (T0 + 10 ** 6, '1800')
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        omi.now = T0
        await r.run_once(now=T0)
        assert sql(doc, "SELECT window_start FROM ledger WHERE bucket='transcripts'") == [(T0 + 120,)]
        assert sql(doc, 'SELECT throttled_until FROM accounts') == [(T0 + 120 + 1800,)]
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_text_saved_before_an_upgrade_is_counted_once_so_the_projection_sees_it(tmp_path):
    path, doc = build(tmp_path); omi = Omi(0)
    account = {'provider': 'omi', 'id': 'omi-a', 'archive_namespace': 'a' * 32}
    ns = namespace(account)
    seed_v2(doc, ns)
    with sqlite3.connect(Path(doc['archive']['directory']) / 'archive.sqlite') as db:
        for n in range(3):
            db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)',
                       (f'b{n:031x}', ns, f'big-{n}', '{"id":"big-%d"}' % n,
                        '{"transcript":{"state":"available","text":"%s","digest":"x"}}' % ('w' * 20000), 1, 50.0, 3650.0, 0))
        for n in range(700):
            db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)',
                       (f'c{n:031x}', ns, f'queued-{n}', '{"id":"q%d","title":"%s"}' % (n, 'T' * 450), '{}', 0, None, 0.0, 0))
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        await r.run_once(now=T0)
        current = accounts_of(r)[0]
        status = await r.status(current, now=T0 + 10)
        assert status['with_text'] == 6 and status['traversal']['discovered_total'] == 706
        assert status['projected_db_bytes'] > 15_000_000
        # Transcript-only records count in full, and the counter is stable across passes and a restart.
        assert sql(doc, 'SELECT text_bytes FROM accounts WHERE namespace=?', ns) == [(3 * 20000 + 3 * len('old words 0'),)]
        await r.run_once(now=T0 + 400)
        await r.close()
        r = runtime(path, omi); await r.start(background=False)
        await r.run_once(now=T0 + 800)
        assert sql(doc, 'SELECT text_bytes FROM accounts WHERE namespace=?', ns) == [(3 * 20000 + 3 * len('old words 0'),)]
        assert (await r.status(accounts_of(r)[0], now=T0 + 900))['projected_db_bytes'] > 15_000_000
        await r.close()
    asyncio.run(run())


# spec: ANYM-724
def test_capacity_projection_never_collapses_below_what_the_accepted_text_costs(tmp_path):
    path, doc = build(tmp_path); omi = Omi(0)
    async def run():
        r = runtime(path, omi); await r.start(background=False)
        account = accounts_of(r)[0]
        items = [{'id': f'm{n:04d}', 'title': 'T' * 450, 'created_at': iso(T0 - n)} for n in range(1000)]
        await r.db('discover', account, items, None, T0, lambda: None)
        for n in range(3):
            await r.db('observe', account, f'm{n:04d}', items[n], {'transcript': ('available', 'w' * 20000, None)}, T0)
        status = await r.status(account, now=T0 + 10)
        assert status['with_text'] == 3 and status['traversal']['discovered_total'] == 1000
        assert status['projected_db_bytes'] > 1000 * 3 * 20000 * 0.9
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
            counts = dict(sql(doc, "SELECT bucket,sum(count) FROM ledger WHERE bucket<>'throttle' GROUP BY bucket"))
            assert counts == {'reads': 4, 'transcripts': 3}
            before = len(omi.of('tokenaaaa', 'detail'))
            moment = time.time()  # foreground reads are stamped with the real clock
            omi.now = moment
            await app.state.archive.run_once(now=moment)
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
