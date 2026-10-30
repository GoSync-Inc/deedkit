"""Local wearable retrieval: synthetic text, no provider credentials or network."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'services/recordings/src'))
from deedkit_recordings.app import create_app
from deedkit_recordings.archive_store import namespace


def configuration(tmp_path):
    tmp_path = tmp_path.resolve()
    secret = tmp_path / 'cursor.key'
    secret.write_text('synthetic-cursor-' * 4); secret.chmod(0o600)
    doc = {'version': 1, 'cursor_secret_file': str(secret),
           'archive': {'directory': str(tmp_path / 'archive'), 'poll_seconds': 60},
           'accounts': [{'id': name, 'provider': provider, 'label': 'Synthetic',
                         'token_file': str(tmp_path / 'nonexistent-provider-key'),
                         'archive_namespace': letter * 32}
                        for name, provider, letter in [('omi-a', 'omi', 'a'), ('plaud-b', 'plaud', 'b')]],
           'principals': [{'id': 'agent-a', 'key_sha256': hashlib.sha256(b'backend-a').hexdigest(),
                           'expires_at': '2099-01-01T00:00:00Z', 'accounts': ['omi-a'],
                           'permissions': ['recordings:read', 'recordings:content']}]}
    path = tmp_path / 'configuration.json'
    save(path, doc)
    return path, doc


def save(path, doc):
    path.write_text(json.dumps(doc)); path.chmod(0o600)


@asynccontextmanager
async def seeded(tmp_path):
    path, doc = configuration(tmp_path)
    def forbidden(_):
        pytest.fail('Local archive access attempted provider network I/O')
    app = create_app(path, transport=httpx.MockTransport(forbidden), archive_background=False)
    archive = app.state.archive
    await archive.start(background=False)
    refs = {}
    try:
        for account in archive.document().accounts:
            item = {'id': 'same-id', 'title': 'Синтетический телескоп' if account.id == 'omi-a' else 'Hidden title'}
            await archive.db('discover', account, [item], None, 100)
            text = ('Speaker 1 [0.0–30.0]: Обсуждаем звёзды. ' * 30 if account.id == 'omi-a'
                    else 'hidden-violet-secret')
            await archive.db('observe', account, 'same-id', item,
                             {'transcript': ('available', text, None),
                              'summary': ('available', 'Synthetic purchase decision', None)}, 101)
            refs[account.id] = (await archive.db('record', account, 'same-id'))['ref']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local',
                                    headers={'Authorization': 'Bearer backend-a'}) as client:
            yield app, client, path, doc, refs
    finally:
        await archive.close()


# spec: ANYM-652
def test_unicode_local_search_uses_fts_and_never_reads_provider_keys(tmp_path, monkeypatch):
    async def run():
        async with seeded(tmp_path) as (app, client, _, doc, refs):
            from contextlib import contextmanager
            traces = []
            original = app.state.archive.store.connection
            @contextmanager
            def traced(**kwargs):
                with original(**kwargs) as db:
                    db.set_trace_callback(traces.append)
                    yield db
            monkeypatch.setattr(app.state.archive.store, 'connection', traced)
            for query in ('телескоп', 'ЗВЁЗДЫ', 'purchase'):
                response = await client.post('/v1/search', json={'account': 'omi-a', 'query': query})
                assert response.status_code == 200, response.text
                data = response.json()
                assert data['search_mode'] == 'lexical' and data['coverage'] == 'local_observed_unknown'
                assert len(data['items']) == 1
                hit = data['items'][0]
                assert hit['recording_ref'] == refs['omi-a'] and hit['revision'] == 1
                assert hit['parts']['transcript']['digest'] and len(hit['snippet']) <= 320
                assert hit['source_data'] is True and 'hidden-violet' not in response.text
            assert any('MATCH' in sql.upper() for sql in traces)
            with original() as db:
                plan = list(db.execute('EXPLAIN QUERY PLAN SELECT ref FROM texts WHERE texts MATCH ? AND namespace=?',
                                      ('"звёзды"', namespace(doc['accounts'][0]))))
            assert any('VIRTUAL TABLE INDEX' in row[3] and 'M' in row[3] for row in plan), plan
    asyncio.run(run())


# spec: ANYM-652
def test_context_pages_bind_revision_and_preserve_failed_attempt_evidence(tmp_path):
    async def run():
        async with seeded(tmp_path) as (app, client, _, doc, refs):
            args = {'account': 'omi-a', 'recording_ref': refs['omi-a'], 'page_chars': 100}
            first = await client.post('/v1/context', json=args)
            assert first.status_code == 200, first.text
            data = first.json()
            assert len(data['text']) == 100 and data['source_data'] is True and data['next_cursor']
            assert 'Speaker 1 [0.0–30.0]' in data['text']
            account = doc['accounts'][0]
            old = await app.state.archive.db('record', account, 'same-id')
            await app.state.archive.db('observe', account, 'same-id', old['metadata'],
                                       {'transcript': ('error', '', 'provider_timeout')}, 200)
            page = await client.post('/v1/context', json={**args, 'cursor': data['next_cursor']})
            assert page.status_code == 200, page.text
            assert page.json()['offset'] == 100 and page.json()['source_digest'] == data['source_digest']
            assert page.json()['part']['attempt_error'] == 'provider_timeout'
            await app.state.archive.db('observe', account, 'same-id', old['metadata'],
                                       {'transcript': ('available', 'replacement text', None)}, 300)
            changed = await client.post('/v1/context', json={**args, 'cursor': data['next_cursor']})
            assert changed.status_code == 409 and changed.json() == {'error': 'source_changed'}
    asyncio.run(run())


# spec: ANYM-652
@pytest.mark.parametrize('change', ['grant', 'account', 'namespace', 'key'])
def test_local_continuation_rechecks_current_authority_and_namespace(tmp_path, change):
    async def run():
        async with seeded(tmp_path) as (_, client, path, doc, refs):
            args = {'account': 'omi-a', 'recording_ref': refs['omi-a'], 'page_chars': 100}
            first = await client.post('/v1/context', json=args)
            assert first.status_code == 200, first.text
            if change == 'grant': doc['principals'][0]['permissions'] = ['recordings:read']
            elif change == 'account': doc['principals'][0]['accounts'] = []
            elif change == 'namespace': doc['accounts'][0]['archive_namespace'] = 'c' * 32
            else: doc['principals'][0]['key_sha256'] = hashlib.sha256(b'replaced-key').hexdigest()
            save(path, doc)
            response = await client.post('/v1/context', json={**args, 'cursor': first.json()['next_cursor']})
            assert response.status_code in (401, 403), response.text
            assert 'Обсуждаем' not in response.text and refs['omi-a'] not in response.text
            hidden = await client.post('/v1/search', json={'account': 'plaud-b', 'query': 'hidden'})
            assert hidden.status_code in (401, 403) and 'record_count' not in hidden.text
    asyncio.run(run())


# spec: ANYM-652
def test_read_only_principal_gets_status_without_content_or_search(tmp_path):
    async def run():
        async with seeded(tmp_path) as (_, client, path, doc, refs):
            doc['principals'][0]['permissions'] = ['recordings:read']; save(path, doc)
            for operation, args in [('search', {'query': 'звёзды'}),
                                    ('context', {'recording_ref': refs['omi-a']})]:
                result = await client.post('/v1/' + operation, json={'account': 'omi-a', **args})
                assert result.status_code == 403, result.text
            response = await client.post('/v1/archive_status', json={'account': 'omi-a'})
            assert response.status_code == 200 and response.json()['record_count'] == 1
            assert 'text' not in response.text and 'звёзды' not in response.text and 'title' not in response.text
    asyncio.run(run())


# spec: ANYM-652
def test_metadata_only_change_updates_title_index_atomically_without_text_event(tmp_path):
    async def run():
        async with seeded(tmp_path) as (app, client, _, doc, _):
            account = doc['accounts'][0]
            old = await app.state.archive.db('record', account, 'same-id')
            events = await app.state.archive.db('events', 0, 100)
            await app.state.archive.db('observe', account, 'same-id', {**old['metadata'], 'title': 'Orion replacement'},
                                       {'transcript': ('error', '', 'provider_unavailable')}, 200)
            found = await client.post('/v1/search', json={'account': 'omi-a', 'query': 'Orion'})
            assert found.status_code == 200, found.text
            assert found.json()['items'][0]['revision'] == old['revision'] + 1
            assert (await client.post('/v1/search', json={'account': 'omi-a', 'query': 'телескоп'})).json()['items'] == []
            assert await app.state.archive.db('events', 0, 100) == events
    asyncio.run(run())


# spec: ANYM-652
def test_version_one_archive_index_upgrades_without_rewriting_records_and_backups(tmp_path):
    async def run():
        async with seeded(tmp_path) as (app, client, _, doc, refs):
            before = await app.state.archive.db('record', doc['accounts'][0], 'same-id')
            events = await app.state.archive.db('events', 0, 100)
            await app.state.archive.close()
            db_path = Path(doc['archive']['directory']) / 'archive.sqlite'
            with sqlite3.connect(db_path) as db:
                db.executescript("DROP TABLE texts; CREATE VIRTUAL TABLE texts USING fts5(ref UNINDEXED, namespace UNINDEXED, transcript, summary, tokenize='unicode61'); PRAGMA user_version=1;")
            await app.state.archive.start(background=False)
            found = await client.post('/v1/search', json={'account': 'omi-a', 'query': 'телескоп'})
            assert found.status_code == 200 and found.json()['items'][0]['recording_ref'] == refs['omi-a'], found.text
            assert await app.state.archive.db('record', doc['accounts'][0], 'same-id') == before
            assert await app.state.archive.db('events', 0, 100) == events
            from deedkit_recordings.archive_store import backup_archive
            target = tmp_path.resolve() / 'snapshot.sqlite'
            assert backup_archive(app.state.archive.document().archive, target)['state'] == 'saved'
            with sqlite3.connect(target) as snapshot:
                assert snapshot.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                assert snapshot.execute('PRAGMA user_version').fetchone()[0] == 2
    asyncio.run(run())


# spec: ANYM-652
def test_local_search_is_bounded_and_treats_fts_operators_as_literal_words(tmp_path):
    async def run():
        async with seeded(tmp_path) as (_, client, _, _, _):
            for query in ('*', '"', ' ', 'x' * 257, ' '.join('word' for _ in range(17))):
                invalid = await client.post('/v1/search', json={'account': 'omi-a', 'query': query})
                assert invalid.status_code == 422, invalid.text
            result = await client.post('/v1/search', json={'account': 'omi-a', 'query': 'звёзды OR hidden'})
            assert result.status_code == 200 and result.json()['items'] == [], result.text
            oversized = await client.post('/v1/search', json={'account': 'omi-a', 'query': 'звёзды', 'limit': 51})
            assert oversized.status_code == 422
    asyncio.run(run())


# spec: ANYM-652
def test_locked_archive_search_fails_bounded_and_keeps_capabilities_available(tmp_path):
    async def run():
        async with seeded(tmp_path) as (_, client, _, doc, _):
            with sqlite3.connect(Path(doc['archive']['directory']) / 'archive.sqlite') as lock:
                lock.execute('PRAGMA locking_mode=EXCLUSIVE'); lock.execute('BEGIN EXCLUSIVE')
                start = time.monotonic()
                response = await client.post('/v1/search', json={'account': 'omi-a', 'query': 'звёзды'})
                assert response.status_code == 503 and response.json() == {'error': 'archive_unavailable'}, response.text
                assert time.monotonic() - start < 1
                assert (await client.post('/v1/capabilities', json={})).status_code == 200
    asyncio.run(run())


# spec: ANYM-652
def test_local_selection_rejects_foreign_refs_and_rechecks_revocation_after_sql(tmp_path, monkeypatch):
    async def run():
        async with seeded(tmp_path) as (app, client, path, doc, refs):
            response = await client.post('/v1/context', json={'account': 'omi-a', 'recording_ref': refs['plaud-b']})
            assert response.status_code == 403 and 'hidden' not in response.text
            original = app.state.archive.db
            async def revoke_after_read(method, *args):
                result = await original(method, *args)
                if method == 'search':
                    doc['principals'][0]['permissions'] = ['recordings:read']; save(path, doc)
                return result
            monkeypatch.setattr(app.state.archive, 'db', revoke_after_read)
            response = await client.post('/v1/search', json={'account': 'omi-a', 'query': 'звёзды'})
            assert response.status_code == 403 and 'snippet' not in response.text
    asyncio.run(run())


# spec: ANYM-652
@pytest.mark.parametrize('failure', ['disabled', 'corrupt'])
def test_disabled_or_corrupt_local_archive_does_not_disable_existing_backend(tmp_path, failure):
    async def run():
        async with seeded(tmp_path) as (app, client, path, doc, _):
            if failure == 'disabled':
                del doc['archive']; save(path, doc)
            else:
                await app.state.archive.close()
                (Path(doc['archive']['directory']) / 'archive.sqlite').write_bytes(b'synthetic corrupt database')
                await app.state.archive.start(background=False)
            result = await client.post('/v1/search', json={'account': 'omi-a', 'query': 'звёзды'})
            assert result.status_code == 503, result.text
            assert result.json() == {'error': 'archive_disabled' if failure == 'disabled' else 'archive_unavailable'}
            assert (await client.post('/v1/capabilities', json={})).status_code == 200
    asyncio.run(run())


# spec: ANYM-652
def test_local_context_cursor_cannot_cross_credentials_or_change_arguments(tmp_path):
    async def run():
        async with seeded(tmp_path) as (_, client, path, doc, refs):
            args = {'account': 'omi-a', 'recording_ref': refs['omi-a'], 'page_chars': 100}
            first = await client.post('/v1/context', json=args)
            assert first.status_code == 200, first.text
            token = first.json()['next_cursor']
            doc['principals'].append({**doc['principals'][0], 'id': 'agent-b',
                                      'key_sha256': hashlib.sha256(b'backend-b').hexdigest()})
            save(path, doc)
            foreign = await client.post('/v1/context', json={**args, 'cursor': token},
                                        headers={'Authorization': 'Bearer backend-b'})
            assert foreign.status_code == 403 and 'text' not in foreign.text
            for changed in ({'page_chars': 200}, {'kind': 'summary'}):
                response = await client.post('/v1/context', json={**args, **changed, 'cursor': token})
                assert response.status_code == 400 and response.json() == {'error': 'invalid_cursor'}
    asyncio.run(run())


# spec: ANYM-652
def test_local_hit_limit_and_namespaces_bound_the_actual_sql_result(tmp_path):
    async def run():
        async with seeded(tmp_path) as (app, client, path, doc, _):
            doc['principals'][0]['accounts'].append('plaud-b'); save(path, doc)
            account = doc['accounts'][0]
            for index in range(55):
                item = {'id': f'item-{index}', 'title': 'sharedmatch'}
                await app.state.archive.db('discover', account, [item], None, 200)
                await app.state.archive.db('observe', account, item['id'], item,
                                           {'summary': ('available', 'sharedmatch', None)}, 201)
            for name, query, expected in [('omi-a', 'sharedmatch', 50), ('plaud-b', 'sharedmatch', 0),
                                           ('omi-a', 'hidden-violet-secret', 0), ('plaud-b', 'hidden-violet-secret', 1)]:
                response = await client.post('/v1/search', json={'account': name, 'query': query, 'limit': 50})
                assert response.status_code == 200, response.text
                assert len(response.json()['items']) == expected
                assert all(item['account'] == name for item in response.json()['items'])
    asyncio.run(run())
