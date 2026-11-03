"""Recordings event feed for the module router: synthetic archives, HTTP over ASGI, no provider or model call."""
import asyncio
from contextlib import asynccontextmanager
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'services/recordings/src'))
from deedkit_recordings import archive_feed
from deedkit_recordings.app import create_app
from deedkit_recordings.archive_store import Store, backup_archive, namespace
from deedkit_recordings.security import Document, Failure, read_document

ROUTER = 'router-token-synthetic'
T0 = archive_feed.EPOCH_ZERO + 600_000 * 60 + 30  # a fixed clock, about 1.14 years after 2026-01-01
CANARY = 'canary-private-words-never-in-a-feed'


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def configuration(tmp_path, router=True):
    tmp_path = tmp_path.resolve()
    tmp_path.chmod(0o700)
    secret = tmp_path / 'cursor.key'
    secret.write_text('synthetic-cursor-' * 4)
    secret.chmod(0o600)
    doc = {'version': 1, 'cursor_secret_file': str(secret),
           'archive': {'directory': str(tmp_path / 'archive'), 'poll_seconds': 60},
           'accounts': [{'id': name, 'provider': 'omi', 'label': 'Synthetic', 'token_file': str(tmp_path / 'nonexistent-provider-key'),
                         'archive_namespace': letter * 32} for name, letter in (('omi-a', 'a'), ('omi-b', 'b'))],
           'principals': [{'id': 'agent-a', 'key_sha256': sha('backend-a'), 'expires_at': '2099-01-01T00:00:00Z',
                           'accounts': ['omi-a', 'omi-b'], 'permissions': ['recordings:read', 'recordings:content']},
                          {'id': 'agent-b', 'key_sha256': sha('backend-b'), 'expires_at': '2099-01-01T00:00:00Z',
                           'accounts': ['omi-a'], 'permissions': ['recordings:read', 'recordings:content']}]}
    if router:
        doc['event_router'] = {'key_sha256': [sha(ROUTER)], 'expires_at': '2099-01-01T00:00:00Z'}
    path = tmp_path / 'configuration.json'
    save(path, doc)
    return path, doc


def save(path, doc):
    path.write_text(json.dumps(doc))
    path.chmod(0o600)


def parsed(doc):
    return Document.model_validate(doc)


class Clock:
    """The test clock: serving waits for the clock to reach the incarnation's minute, so tests move it explicitly."""
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def open_store(doc, clock=T0):
    store = Store(parsed(doc).archive)
    store.feed_enabled = True
    store.clock = Clock(clock)
    return store


def accounts_of(doc):
    return {a.id: a for a in parsed(doc).accounts}


def write(store, account, ident, text='spoken words', now=100):
    store.discover(account, [{'id': ident, 'title': 'Title ' + ident}], None, now)
    store.observe(account, ident, {'id': ident, 'title': 'Title ' + ident}, {'transcript': ('available', text, None)}, now)


def view_of(doc):
    return archive_feed.access_view(parsed(doc), namespace)


def served(store, doc, after=0, limit=200):
    """Every event after `after`, paged the way the router pages."""
    view, result, cursor = view_of(doc), [], after
    store.clock.now += 120  # the incarnation's minute has passed: the feed may publish
    for _ in range(1000):
        for _ in range(5):  # a bump just made (a cap, a restore) may need one more minute of clock
            try:
                page = store.run('feed_page', view, cursor, limit)
                break
            except Failure as error:
                if error.code != 'feed_not_ready':
                    raise
                store.clock.now += 120
        else:
            raise AssertionError('the feed never became ready')
        result += page['events']
        assert page['next_after'] >= cursor
        if not page['has_more']:
            return result
        assert page['next_after'] > cursor  # a page with `has_more` never repeats its cursor
        cursor = page['next_after']
    raise AssertionError('feed did not end')


def seqs(events):
    return [int(event['id'].split(':')[1]) for event in events]


@asynccontextmanager
async def running(tmp_path, router=True, populate=0):
    path, doc = configuration(tmp_path, router)
    def forbidden(_):
        pytest.fail('The event feed attempted provider network I/O')
    app = create_app(path, transport=httpx.MockTransport(forbidden), archive_background=False)
    archive = app.state.archive
    await archive.start(background=False)
    archive.store.feed_enabled = router
    clock = Clock(T0)
    archive.store.clock = clock
    account = archive.document().accounts[0]
    for number in range(populate):
        ident = f'rec-{number:04d}'
        await archive.db('discover', account, [{'id': ident, 'title': CANARY}], None, 100 + number)
        await archive.db('observe', account, ident, {'id': ident, 'title': CANARY},
                         {'transcript': ('available', f'{CANARY} words {number}', None)}, 100 + number)
    if router:
        await archive.db('feed_sync', archive_feed.access_view(archive.document(), namespace))
        clock.now += 120  # the incarnation's minute has passed
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
            yield app, client, path, doc
    finally:
        await archive.close()


def get(client, path, token: str | None = ROUTER):
    return client.get(path, headers={'Authorization': 'Bearer ' + token} if token else {})


# spec: ANYM-726
def test_routes_do_not_exist_without_router_configuration_and_need_the_router_token(tmp_path):
    async def run():
        async with running(tmp_path / 'off', router=False) as (_, client, _, _):
            for route in ('/v1/events', '/v1/events/head'):
                assert (await get(client, route)).status_code == 404
    (tmp_path / 'off').mkdir()
    asyncio.run(run())
    async def configured():
        async with running(tmp_path / 'on') as (_, client, _, _):
            for route in ('/v1/events', '/v1/events/head'):
                assert (await get(client, route)).status_code == 200
                for token in ('backend-a', 'backend-b', 'wrong', ''):  # agent keys and garbage: the fixed auth error
                    refused = await get(client, route, token or None)
                    assert refused.status_code == 401 and refused.json() == {'error': 'unauthorized'}
            for operation in ('search', 'context', 'archive_status', 'capabilities', 'events'):  # the router token opens nothing else
                refused = await client.post('/v1/' + operation, json={'account': 'omi-a'}, headers={'Authorization': 'Bearer ' + ROUTER})
                assert refused.status_code in (401, 404), (operation, refused.text)
                assert 'events' not in refused.text or refused.json() == {'error': 'not_found_or_forbidden'}
    (tmp_path / 'on').mkdir()
    asyncio.run(configured())


# spec: ANYM-726
def test_router_token_is_distinct_from_agent_keys_and_has_its_own_expiry(tmp_path):
    path, doc = configuration(tmp_path)
    doc['event_router']['key_sha256'] = [sha('backend-a')]
    save(path, doc)
    with pytest.raises(Failure, match='configuration_unavailable'):
        read_document(path)
    doc['event_router'] = {'key_sha256': [sha(ROUTER)], 'expires_at': '2000-01-01T00:00:00Z'}
    save(path, doc)
    async def run():
        app = create_app(path, transport=httpx.MockTransport(lambda _: pytest.fail('network')), archive_background=False)
        await app.state.archive.start(background=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
            assert (await get(client, '/v1/events')).status_code == 401  # expired
            doc['event_router'] = {'key_sha256': [sha('old'), sha(ROUTER)], 'expires_at': '2099-01-01T00:00:00Z'}
            save(path, doc)
            ready = await get(client, '/v1/events')  # a rotation keeps two digests: authenticated, and the first minute is not ready
            assert ready.status_code == 503 and ready.json() == {'error': 'feed_not_ready'}
        await app.state.archive.close()
    asyncio.run(run())


# spec: ANYM-726
def test_envelope_has_exact_keys_bounds_and_never_content(tmp_path):
    async def run():
        async with running(tmp_path, populate=3) as (app, client, _, doc):
            body = (await get(client, '/v1/events')).json()
            assert set(body) == {'events', 'next_after', 'has_more', 'retention_floor', 'journal_id'}
            assert body['retention_floor'] == 0 and body['has_more'] is False and len(body['events']) == 3
            ns = namespace(parsed(doc).accounts[0])
            for event in body['events']:
                assert set(event) == {'specversion', 'id', 'source', 'type', 'subject', 'time', 'data', 'urgency', 'expires_at', 'scope_ref'}
                assert event['specversion'] == '1.0' and event['source'] == 'deedkit:recordings'
                assert event['type'] == 'recording.text.archived' and event['urgency'] == 'batch' and event['expires_at'] is None
                assert event['id'] == 'recordings:' + str(int(event['id'].split(':')[1])) and int(event['id'].split(':')[1]) < 2 ** 53 - 1
                assert event['subject'] == event['data']['recording_ref'] and len(event['subject']) == 32
                assert event['scope_ref'] == f'recordings:omi-a.{ns[:16]}'
                assert set(event['data']) == {'recording_ref', 'account_ref', 'revision'} and event['data']['account_ref'] == 'omi-a'
                assert len(json.dumps(event['data'])) <= 1024 and event['time'].endswith('Z')
            assert body['journal_id'] == (await get(client, '/v1/events/head')).json()['journal_id']
            assert CANARY not in json.dumps(body) and 'Title' not in json.dumps(body)
            assert seqs(body['events']) == sorted(seqs(body['events'])) and body['next_after'] == seqs(body['events'])[-1]
    asyncio.run(run())


# spec: ANYM-726
def test_text_events_of_a_removed_account_or_replaced_namespace_are_not_served_and_scope_changes(tmp_path):
    path, doc = configuration(tmp_path)
    store = open_store(doc)
    try:
        write(store, accounts_of(doc)['omi-a'], 'rec-1')
        write(store, accounts_of(doc)['omi-b'], 'rec-2')
        before = served(store, doc)
        assert {e['data']['account_ref'] for e in before} == {'omi-a', 'omi-b'}
        removed = copy.deepcopy(doc)
        removed['accounts'] = [a for a in doc['accounts'] if a['id'] != 'omi-b']
        for principal in removed['principals']:
            principal['accounts'] = [a for a in principal['accounts'] if a != 'omi-b']
        assert {e['data']['account_ref'] for e in served(store, removed) if e['type'] == 'recording.text.archived'} == {'omi-a'}
        replaced = copy.deepcopy(doc)
        replaced['accounts'][0]['archive_namespace'] = 'c' * 32
        write(store, parsed(replaced).accounts[0], 'rec-3')  # the same provider id under the new identity
        text = [e for e in served(store, replaced) if e['type'] == 'recording.text.archived']
        assert [e['data']['account_ref'] for e in text] == ['omi-b', 'omi-a']
        assert text[1]['scope_ref'] == f"recordings:omi-a.{namespace(parsed(replaced).accounts[0])[:16]}" != before[0]['scope_ref']
    finally:
        store.close()


# spec: ANYM-726
def test_one_event_per_usable_text_revision_and_a_backfill_is_ordered_and_gapless(tmp_path):
    path, doc = configuration(tmp_path)
    store = open_store(doc)
    try:
        account = accounts_of(doc)['omi-a']
        store.discover(account, [{'id': 'rec-x', 'title': 'T'}], None, 1)
        for _ in range(2):  # a replay of the same text
            store.observe(account, 'rec-x', {'id': 'rec-x', 'title': 'T'}, {'transcript': ('available', 'same words', None)}, 2)
        store.observe(account, 'rec-x', {'id': 'rec-x', 'title': 'T2'}, {'transcript': ('pending', '', None)}, 3)  # nothing usable
        store.observe(account, 'rec-x', {'id': 'rec-x', 'title': 'T2'}, {'transcript': ('error', '', 'provider_timeout')}, 4)
        assert [e['data']['revision'] for e in served(store, doc)] == [1]
        store.observe(account, 'rec-x', {'id': 'rec-x', 'title': 'T2'}, {'transcript': ('available', 'edited words', None)}, 5)
        assert [e['data']['revision'] for e in served(store, doc)] == [1, 3]
        for number in range(300):
            write(store, account, f'bulk-{number:03d}', now=10 + number)
        events = served(store, doc, limit=100)
        assert len(events) == 302
        ordered = seqs(events)
        assert ordered == sorted(set(ordered)) and all(b - a == 1 for a, b in zip(ordered, ordered[1:]))  # gapless inside one epoch
        # paging by next_after reproduces the same list at any page size, never repeating or skipping
        assert seqs(served(store, doc, limit=7)) == ordered
        assert seqs(served(store, doc, after=ordered[149], limit=50)) == ordered[150:]
    finally:
        store.close()


# spec: ANYM-726
def test_head_is_the_highest_assigned_seq_and_a_fresh_journal_starts_at_its_base(tmp_path):
    async def run():
        async with running(tmp_path) as (app, client, _, doc):
            first = (await get(client, '/v1/events/head')).json()
            assert set(first) == {'journal_id', 'head'} and first['head'] % archive_feed.BASE == 0 and first['head'] > 0
            account = app.state.archive.document().accounts[0]
            await app.state.archive.db('discover', account, [{'id': 'r', 'title': 't'}], None, 1)
            await app.state.archive.db('observe', account, 'r', {'id': 'r', 'title': 't'}, {'transcript': ('available', 'words', None)}, 2)
            head = (await get(client, '/v1/events/head')).json()
            events = (await get(client, f"/v1/events?after={first['head']}&limit=10")).json()['events']
            assert head['head'] == first['head'] + 1 == seqs(events)[0] and head['journal_id'] == first['journal_id']
            assert (await get(client, f"/v1/events?after={head['head']}")).json()['events'] == []
    asyncio.run(run())


# spec: ANYM-726
@pytest.mark.parametrize('query', ['after=-1', 'after=x', 'limit=0', 'limit=201', 'limit=', 'extra=1', 'after=1&after=2',
                                   'after=99999999999999999', f'after={2 ** 53}', 'limit=' + '9' * 5000, 'after=' + '9' * 5000])
def test_malformed_feed_queries_are_refused_with_a_fixed_code(tmp_path, query):
    async def run():
        async with running(tmp_path) as (_, client, _, _):
            refused = await get(client, '/v1/events?' + query)
            assert refused.status_code == 422 and refused.json() == {'error': 'invalid_parameters'}
        async with running(tmp_path / 'h') as (_, client, _, _):
            assert (await get(client, '/v1/events/head?after=1')).status_code == 422
    (tmp_path / 'h').mkdir()
    asyncio.run(run())


# spec: ANYM-726
def test_a_same_incarnation_restore_never_reuses_or_skips_an_identity(tmp_path):
    path, doc = configuration(tmp_path)
    account = accounts_of(doc)['omi-a']
    store = open_store(doc)
    try:
        for number in range(100):
            write(store, account, f'rec-{number:04d}', now=number)
        snapshot = tmp_path / 'snapshots'
        snapshot.mkdir(mode=0o700)
        backup_archive(parsed(doc).archive, snapshot / 'head100.sqlite')
        for number in range(100, 200):
            write(store, account, f'rec-{number:04d}', now=number)
        published = served(store, doc)
        router_cursor = seqs(published)[-1]  # the router consumed 200 events
        assert len(published) == 200
    finally:
        store.close()
    archive = Path(doc['archive']['directory'])
    for stale in archive.glob('archive.sqlite*'):
        stale.unlink()
    shutil.copy(snapshot / 'head100.sqlite', archive / 'archive.sqlite')
    (archive / 'archive.sqlite').chmod(0o600)  # restored: head 100, the marker still says 200
    store = open_store(doc)
    try:
        for number in range(100, 201):  # collection continues to 201 before the router polls again
            write(store, account, f'new-{number:04d}', now=number)
        fresh = served(store, doc, after=router_cursor)
        assert len(fresh) == 101  # none skipped
        assert min(seqs(fresh)) > router_cursor  # every new identity is greater than anything published before
        assert not {e['id'] for e in fresh} & {e['id'] for e in published[100:]}  # none reused
        assert seqs(served(store, doc)[:100]) == seqs(published[:100])  # what was served before keeps its identity
    finally:
        store.close()


# spec: ANYM-726
def test_a_lost_marker_or_a_new_volume_bumps_past_everything_published(tmp_path):
    path, doc = configuration(tmp_path)
    account = accounts_of(doc)['omi-a']
    archive = Path(doc['archive']['directory'])
    store = open_store(doc)
    try:
        for number in range(5):
            write(store, account, f'rec-{number}', now=number)
        old = seqs(served(store, doc))
    finally:
        store.close()
    (archive / archive_feed.MARKER).unlink()  # a new volume with the database restored onto it
    store = open_store(doc, clock=T0 + 3600)
    try:
        write(store, account, 'rec-after', now=9)
        events = served(store, doc)
        assert seqs(events)[:5] == old and seqs(events)[5] > old[-1] + 1 and len(events) == 6
        marker = archive_feed.read_marker(archive)
        assert marker['incarnation'] * archive_feed.BASE < seqs(events)[5] < (marker['incarnation'] + 1) * archive_feed.BASE
    finally:
        store.close()
    (archive / archive_feed.MARKER).write_text('not json')
    (archive / archive_feed.MARKER).chmod(0o600)  # a damaged marker is a lost marker
    store = open_store(doc, clock=T0 + 7200)
    try:
        write(store, account, 'rec-again', now=10)
        assert seqs(served(store, doc))[-1] > seqs(events)[-1] and len(served(store, doc)) == 7
    finally:
        store.close()


# spec: ANYM-726
def test_a_clock_regression_cannot_reuse_an_incarnation(tmp_path):
    path, doc = configuration(tmp_path)
    account = accounts_of(doc)['omi-a']
    archive = Path(doc['archive']['directory'])
    store = open_store(doc, clock=T0)
    try:
        write(store, account, 'rec-1')
        first = seqs(served(store, doc))
    finally:
        store.close()
    (archive / archive_feed.MARKER).unlink()
    store = open_store(doc, clock=T0 - 100 * 24 * 3600)  # the host clock went back by 100 days
    try:
        write(store, account, 'rec-2')
        store.clock.now += 120
        with pytest.raises(Failure, match='feed_not_ready'):  # fail closed: no publication until the clock reaches the incarnation
            store.run('feed_page', view_of(doc), 0, 10)
        with pytest.raises(Failure, match='feed_not_ready'):
            store.run('feed_head', view_of(doc))
        store.clock.now = T0 + 3 * 60
        second = seqs(served(store, doc))
        assert second[:1] == first and second[1] > first[0] and second[1] // archive_feed.BASE > first[0] // archive_feed.BASE
    finally:
        store.close()


# spec: ANYM-726
def test_the_local_offset_cap_forces_a_bump_and_identities_stay_stable(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_feed, 'LOCAL_CAP', 5)
    path, doc = configuration(tmp_path)
    account = accounts_of(doc)['omi-a']
    store = open_store(doc)
    try:
        seen = []
        for number in range(13):
            write(store, account, f'rec-{number}', now=number)
            current = seqs(served(store, doc))
            assert current[:len(seen)] == seen  # an event keeps the identity it was served with
            seen = current
        assert len(seen) == 13 and seen == sorted(set(seen))
        assert len({s // archive_feed.BASE for s in seen}) >= 3  # the cap forced bumps
        assert all(s % archive_feed.BASE < 5 for s in seen)
    finally:
        store.close()


# spec: ANYM-726
def test_the_feed_refuses_to_start_when_the_base_would_reach_2_pow_53(tmp_path):
    path, doc = configuration(tmp_path)
    limit = (archive_feed.MAX_SEQ - 1) // archive_feed.BASE  # the largest incarnation whose base stays below 2^53-1
    assert limit * archive_feed.BASE < archive_feed.MAX_SEQ <= (limit + 1) * archive_feed.BASE
    def clock_for(incarnation):  # the first start takes minutes(now) + 1
        return archive_feed.EPOCH_ZERO + (incarnation - 1) * 60 + 30
    (tmp_path / 'ok').mkdir()
    path_ok, doc_ok = configuration(tmp_path / 'ok')
    store = open_store(doc_ok, clock=clock_for(limit))
    try:
        store.run('feed_sync', view_of(doc_ok))
        write(store, accounts_of(doc_ok)['omi-a'], 'rec-1')
        assert seqs(served(store, doc_ok))[0] // archive_feed.BASE == limit and seqs(served(store, doc_ok))[0] < archive_feed.MAX_SEQ
    finally:
        store.close()
    (tmp_path / 'bad').mkdir()
    path_bad, doc_bad = configuration(tmp_path / 'bad')
    store = open_store(doc_bad, clock=clock_for(limit + 1))
    try:
        with pytest.raises(Failure, match='sequence_space_exhausted'):
            store.run('feed_sync', view_of(doc_bad))
        with pytest.raises(Failure, match='sequence_space_exhausted'):
            store.run('feed_head', view_of(doc_bad))
    finally:
        store.close()


# spec: ANYM-726
def test_access_changes_publish_one_event_per_reason_and_are_idempotent(tmp_path):
    path, doc = configuration(tmp_path)
    store = open_store(doc)
    try:
        account = accounts_of(doc)['omi-a']
        write(store, account, 'rec-1')
        view = view_of(doc)
        store.run('feed_sync', view)
        assert [e['type'] for e in served(store, doc)] == ['recording.text.archived']  # the first view is a baseline
        old_a, old_b = (namespace(a) for a in parsed(doc).accounts)
        def access(current):
            return [e for e in served(store, current) if e['type'] == 'recording.access.changed']
        # 1. the content permission is removed from agent-a: one event per account it held
        step = copy.deepcopy(doc)
        step['principals'][0]['permissions'] = ['recordings:read']
        store.run('feed_sync', view_of(step))
        store.run('feed_sync', view_of(step))  # a repeated reload changes nothing
        events = access(step)
        assert sorted((e['data']['account_ref'], e['data']['principal_ref'], e['data']['reason']) for e in events) == [
            ('omi-a', 'agent-a', 'permission_removed'), ('omi-b', 'agent-a', 'permission_removed')]
        for event in events:
            assert set(event) == {'specversion', 'id', 'source', 'type', 'subject', 'time', 'data', 'urgency', 'expires_at', 'scope_ref'}
            assert set(event['data']) == {'account_ref', 'principal_ref', 'reason'} and event['urgency'] == 'now'
            assert event['subject'] == event['data']['account_ref'] and event['expires_at'] is None
        assert {e['scope_ref'] for e in events} == {f'recordings:omi-a.{old_a[:16]}', f'recordings:omi-b.{old_b[:16]}'}
        # 2. an account leaves agent-b's ceiling
        gone = copy.deepcopy(step)
        gone['principals'][1]['accounts'] = []
        store.run('feed_sync', view_of(gone))
        assert [e['data'] for e in access(gone)][2:] == [{'account_ref': 'omi-a', 'principal_ref': 'agent-b', 'reason': 'account_removed'}]
        # 3. the namespace of omi-a is replaced: every principal that held it (agent-a still lists it), with the OLD scope
        moved = copy.deepcopy(gone)
        moved['accounts'][0]['archive_namespace'] = 'd' * 32
        store.run('feed_sync', view_of(moved))
        replaced = [e for e in access(moved) if e['data']['reason'] == 'namespace_replaced']
        assert [e['data']['principal_ref'] for e in replaced] == ['agent-a'] and replaced[0]['scope_ref'] == f'recordings:omi-a.{old_a[:16]}'
        # the old-scope events are still served although the old namespace has no account any more
        assert len(access(moved)) == 4
        # 4. a key replacement and a new expiry change no entitlement
        keys = copy.deepcopy(moved)
        keys['principals'][0]['key_sha256'] = sha('rotated')
        keys['principals'][1]['expires_at'] = '2030-01-01T00:00:00Z'
        store.run('feed_sync', view_of(keys))
        assert len(access(keys)) == 4
        # 5. the principal itself disappears
        none = copy.deepcopy(keys)
        none['principals'] = none['principals'][:1]
        store.run('feed_sync', view_of(none))
        assert len(access(none)) == 4  # agent-b had no account left in its ceiling: nothing it could still see
        removed = copy.deepcopy(none)
        removed['principals'] = []
        store.run('feed_sync', view_of(removed))
        assert [e['data']['reason'] for e in access(removed)][4:] == ['permission_removed'] * 2  # agent-a still held read on omi-a and omi-b
        text = ' '.join(json.dumps(e) for e in served(store, removed))
        assert 'rec-1' not in text and 'Title' not in text and 'spoken' not in text  # no content, ids or labels
    finally:
        store.close()


# spec: ANYM-726
def test_a_busy_or_broken_archive_gives_a_fixed_code_and_the_collector_goes_on(tmp_path, monkeypatch):
    async def run():
        async with running(tmp_path / 'busy', populate=1) as (app, client, _, _):
            original = app.state.archive.db
            async def slow(method, *args):
                await asyncio.sleep(0.2)
                return await original(method, *args)
            monkeypatch.setattr(app.state.archive, 'db', slow)
            answers = await asyncio.gather(*[get(client, '/v1/events') for _ in range(6)])
            codes = sorted(a.json().get('error', 'ok') for a in answers)
            assert 'archive_busy' in codes and codes.count('ok') == 4
            monkeypatch.undo()
        async with running(tmp_path / 'broken', populate=1) as (app, client, path, doc):
            await app.state.archive.close()
            (Path(doc['archive']['directory']) / 'archive.sqlite').write_bytes(b'synthetic corrupt database')
            await app.state.archive.start(background=False)
            for route in ('/v1/events', '/v1/events/head'):
                broken = await get(client, route)
                assert broken.status_code == 503 and broken.json() == {'error': 'archive_unavailable'}
            assert (await client.post('/v1/capabilities', json={}, headers={'Authorization': 'Bearer backend-a'})).status_code == 200
        async with running(tmp_path / 'disabled') as (app, client, path, doc):
            del doc['archive']
            save(path, doc)
            assert (await get(client, '/v1/events')).json() == {'error': 'archive_disabled'}
    for name in ('busy', 'broken', 'disabled', 'collecting'):
        (tmp_path / name).mkdir()
    asyncio.run(run())
    path, doc = configuration(tmp_path / 'collecting', router=True)
    async def collecting():
        from deedkit_recordings.archive import ArchiveRuntime
        from deedkit_recordings.omi import Provider
        runtime = ArchiveRuntime(path, Provider(httpx.MockTransport(lambda _: httpx.Response(200, json=[]))), None)
        await runtime.start(background=False)
        monkeypatch.setattr(Store, 'feed_sync', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('marker volume full')))
        await runtime.run_once(now=100)
        assert runtime.feed_error == 'archive_unavailable'  # recorded, and the pass still ran (the index build and passes are not skipped)
        assert runtime.store.feed_enabled is True
        await runtime.close()
    asyncio.run(collecting())


# spec: ANYM-726
def test_a_marker_failure_never_stops_text_from_being_archived_and_only_the_feed_waits(tmp_path, monkeypatch):
    path, doc = configuration(tmp_path)
    account = accounts_of(doc)['omi-a']
    store = open_store(doc)
    try:
        write(store, account, 'rec-1')
        calls = []
        real = archive_feed.write_marker
        monkeypatch.setattr(archive_feed, 'write_marker', lambda *args: calls.append(args) or (_ for _ in ()).throw(OSError('disk full')))
        for number in range(2, 6):  # the whole text path, many records, with every marker write failing
            write(store, account, f'rec-{number}', now=number)
        assert calls == [] and store.record(account, 'rec-5')['revision'] == 1  # observe() never touches the marker file
        assert len(store.events(0, 100)) == 5
        store.clock.now += 120
        with pytest.raises(Failure, match='archive_unavailable'):
            store.run('feed_page', view_of(doc), 0, 10)  # only the feed is unavailable, and nothing was served
        monkeypatch.setattr(archive_feed, 'write_marker', real)
        assert len(served(store, doc)) == 5  # then it catches up, identities intact
    finally:
        store.close()


# spec: ANYM-726
def test_publication_waits_for_the_clock_so_two_restores_inside_a_minute_cannot_collide(tmp_path):
    path, doc = configuration(tmp_path)
    account = accounts_of(doc)['omi-a']
    archive = Path(doc['archive']['directory'])
    snapshot = tmp_path.resolve() / 'snapshots'
    snapshot.mkdir(mode=0o700)
    store = open_store(doc)
    try:
        write(store, account, 'rec-1')
        backup_archive(parsed(doc).archive, snapshot / 'older.sqlite')  # the snapshot every restore below starts from
    finally:
        store.close()
    published = []
    for attempt in range(3):  # restore the older snapshot with a lost marker, again and again, the clock barely moving
        for stale in archive.glob('archive.sqlite*'):
            stale.unlink()
        shutil.copy(snapshot / 'older.sqlite', archive / 'archive.sqlite')
        (archive / 'archive.sqlite').chmod(0o600)
        (archive / archive_feed.MARKER).unlink(missing_ok=True)
        store = open_store(doc, clock=T0 + attempt * 5)  # seconds apart: the same clock minute
        try:
            write(store, account, f'new-{attempt}', now=10 + attempt)
            with pytest.raises(Failure, match='feed_not_ready'):  # nothing is published inside the minute of the bump
                store.run('feed_page', view_of(doc), 0, 10)
            with pytest.raises(Failure, match='feed_not_ready'):
                store.run('feed_head', view_of(doc))
            if attempt < 2:
                continue  # a restore before anything was published may take any incarnation
            events = served(store, doc)  # the clock moved on: now it publishes
            published = [e['id'] for e in events]
        finally:
            store.close()
    assert len(published) == 2 and len(set(published)) == 2
    # a further restore after that publication takes a larger incarnation than anything published
    for stale in archive.glob('archive.sqlite*'):
        stale.unlink()
    shutil.copy(snapshot / 'older.sqlite', archive / 'archive.sqlite')
    (archive / 'archive.sqlite').chmod(0o600)
    (archive / archive_feed.MARKER).unlink()
    store = open_store(doc, clock=T0 + 120)
    try:
        write(store, account, 'last', now=99)
        assert min(seqs(served(store, doc))[1:]) > max(int(i.split(':')[1]) for i in published)
    finally:
        store.close()


# spec: ANYM-726
def test_the_whole_sequence_stays_below_2_pow_53_and_a_backlog_is_partitioned_into_capped_epochs(tmp_path, monkeypatch):
    limit = (archive_feed.MAX_SEQ - 1) // archive_feed.BASE
    assert archive_feed.capacity(limit) == archive_feed.MAX_SEQ - limit * archive_feed.BASE < archive_feed.BASE - 1
    assert archive_feed.capacity(limit) * 1 + limit * archive_feed.BASE == archive_feed.MAX_SEQ  # the last usable seq
    assert archive_feed.capacity(limit + 1) < 1 and archive_feed.capacity(1) == archive_feed.LOCAL_CAP - 1
    monkeypatch.setattr(archive_feed, 'LOCAL_CAP', 5)
    path, doc = configuration(tmp_path)
    account = accounts_of(doc)['omi-a']
    store = open_store(doc)
    store.feed_enabled = False  # a dark archive with a backlog of 12 usable text events
    try:
        for number in range(12):
            write(store, account, f'rec-{number:02d}', now=number)
        store.feed_enabled = True
        events = served(store, doc)
        assert len(events) == 12
        offsets = [s % archive_feed.BASE for s in seqs(events)]
        assert max(offsets) <= 4 and seqs(events) == sorted(set(seqs(events)))  # offsets 1..4 in each of three epochs
        assert len({s // archive_feed.BASE for s in seqs(events)}) == 3
    finally:
        store.close()


# spec: ANYM-726
def test_retraction_events_keep_the_exact_principal_and_account_identifiers(tmp_path):
    path, doc = configuration(tmp_path)
    doc['accounts'].append({'id': '_hidden', 'provider': 'omi', 'label': 'Synthetic', 'token_file': doc['accounts'][0]['token_file'],
                            'archive_namespace': 'e' * 32})
    doc['principals'] += [{'id': 'agent:reader', 'key_sha256': sha('k1'), 'expires_at': '2099-01-01T00:00:00Z',
                           'accounts': ['omi-a', '_hidden'], 'permissions': ['recordings:read', 'recordings:content']},
                          {'id': 'agent reader', 'key_sha256': sha('k2'), 'expires_at': '2099-01-01T00:00:00Z',
                           'accounts': ['omi-a'], 'permissions': ['recordings:read', 'recordings:content']}]
    store = open_store(doc)
    try:
        store.run('feed_sync', view_of(doc))
        gone = copy.deepcopy(doc)
        gone['principals'] = [p for p in doc['principals'] if p['id'] in ('agent-a', 'agent-b')]
        store.run('feed_sync', view_of(gone))
        events = [e for e in served(store, gone) if e['type'] == 'recording.access.changed']
        data = {(e['data']['principal_ref'], e['data']['account_ref']) for e in events}
        assert ('agent:reader', 'omi-a') in data  # a valid reference is kept exactly, ':' included
        assert (archive_feed.safe_ref('agent reader'), 'omi-a') in data and archive_feed.safe_ref('agent reader').startswith('h-')
        hidden = next(e for e in events if e['data']['principal_ref'] == 'agent:reader' and e['data']['account_ref'] != 'omi-a')
        assert hidden['data']['account_ref'] == archive_feed.safe_ref('_hidden') and hidden['data']['account_ref'].startswith('h-')
        assert hidden['scope_ref'] == f"recordings:_hidden.{namespace(accounts_of(doc)['_hidden'])[:16]}"  # the scope keeps the exact account id
        assert archive_feed.safe_ref('omi-a') == 'omi-a' and archive_feed.safe_ref('A' * 128) == 'A' * 128
        assert archive_feed.safe_ref('A' * 129).startswith('h-')
    finally:
        store.close()


# spec: ANYM-726
def test_a_large_entitlement_removal_is_published_in_one_pass(tmp_path):
    path, doc = configuration(tmp_path)
    doc['principals'] = [{'id': f'agent-{n:03d}', 'key_sha256': sha(f'key-{n}'), 'expires_at': '2099-01-01T00:00:00Z',
                          'accounts': ['omi-a', 'omi-b'], 'permissions': ['recordings:read', 'recordings:content']} for n in range(256)]
    store = open_store(doc)
    try:
        store.run('feed_sync', view_of(doc))
        emptied = copy.deepcopy(doc)
        emptied['principals'] = []
        store.run('feed_sync', view_of(emptied))  # 512 retractions in one bounded write; marker I/O happens once
        events = [e for e in served(store, emptied) if e['type'] == 'recording.access.changed']
        assert len(events) == 512 and {e['data']['reason'] for e in events} == {'permission_removed'}
        store.run('feed_sync', view_of(emptied))
        assert len([e for e in served(store, emptied) if e['type'] == 'recording.access.changed']) == 512
    finally:
        store.close()


# spec: ANYM-726
def test_a_consumer_following_the_contract_detects_a_journal_reset_and_never_reads_it_as_new(tmp_path):
    def consume(cursor, known_journal, page):
        """The router's reference rules (SUBSCRIPTIONS.md): another journal_id, or a next_after behind the cursor, is a source reset."""
        if page['journal_id'] != known_journal or page['next_after'] < cursor:
            return 'source_reset', cursor
        return 'ok', page['next_after']
    async def run():
        async with running(tmp_path / 'one', populate=3) as (_, client, _, _):
            first = (await get(client, '/v1/events')).json()
            assert consume(0, first['journal_id'], first) == ('ok', first['next_after'])
            cursor, journal = first['next_after'], first['journal_id']
            same = (await get(client, f'/v1/events?after={cursor}')).json()
            assert same['events'] == [] and consume(cursor, journal, same) == ('ok', cursor)  # an idle journal never moves the cursor back
            ahead = (await get(client, f'/v1/events?after={cursor + archive_feed.BASE * 50}')).json()
            assert ahead['events'] == [] and ahead['next_after'] >= cursor + archive_feed.BASE * 50  # a cursor ahead of the journal is not rewound
        async with running(tmp_path / 'two', populate=3) as (_, client, _, _):  # a different archive: a new journal_id
            other = (await get(client, '/v1/events')).json()
            assert other['journal_id'] != journal and consume(cursor, journal, other)[0] == 'source_reset'
            behind = dict(other, journal_id=journal, next_after=cursor - 1)
            assert consume(cursor, journal, behind)[0] == 'source_reset'
    (tmp_path / 'one').mkdir()
    (tmp_path / 'two').mkdir()
    asyncio.run(run())


def journal_of(store, doc):
    store.clock.now += 120
    for _ in range(5):
        try:
            return store.run('feed_page', view_of(doc), 0, 1)['journal_id']
        except Failure as error:
            if error.code != 'feed_not_ready':
                raise
            store.clock.now += 120
    raise AssertionError('never ready')


def replace_database(doc, source, keep_marker=None):
    archive = Path(doc['archive']['directory'])
    for stale in archive.glob('archive.sqlite*'):
        stale.unlink()
    shutil.copy(source, archive / 'archive.sqlite')
    (archive / 'archive.sqlite').chmod(0o600)
    marker = archive / archive_feed.MARKER
    if keep_marker is None:
        marker.unlink(missing_ok=True)
    else:
        marker.write_text(keep_marker)
        marker.chmod(0o600)
    return archive


# spec: ANYM-726
def test_a_lost_marker_rotates_the_journal_id_and_so_does_a_marker_ahead_of_the_database(tmp_path):
    """Two restores of the same older snapshot, each with a lost marker and a clock behind the other's incarnation, may
    reuse an id; the router must see another journal_id (a source reset), never a continuation."""
    path, doc = configuration(tmp_path)
    account = accounts_of(doc)['omi-a']
    snapshots = tmp_path.resolve() / 'snapshots'
    snapshots.mkdir(mode=0o700)
    store = open_store(doc)
    try:
        write(store, account, 'rec-1')
        original = journal_of(store, doc)
        backup_archive(parsed(doc).archive, snapshots / 'older.sqlite')
        write(store, account, 'rec-2')
        assert journal_of(store, doc) == original  # ordinary growth and bumps keep the journal
    finally:
        store.close()
    archive = Path(doc['archive']['directory'])
    saved = (archive / archive_feed.MARKER).read_text()
    seen = {original}
    for attempt in range(2):  # two timelines from one snapshot, the clock at the same minute, no marker
        replace_database(doc, snapshots / 'older.sqlite')
        store = open_store(doc, clock=T0)
        try:
            write(store, account, f'timeline-{attempt}', now=20)
            seen.add(journal_of(store, doc))
        finally:
            store.close()
    assert len(seen) == 3  # every lost-marker restore is a source reset for the router
    # a restore whose marker survives (the marker is ahead of the older database) is no safer: two such restores pick the same
    # next incarnation, so it rotates the journal as well
    replace_database(doc, snapshots / 'older.sqlite', keep_marker=saved)
    store = open_store(doc, clock=T0 + 3600)
    try:
        write(store, account, 'after-restore', now=30)
        assert journal_of(store, doc) not in seen
    finally:
        store.close()


# spec: ANYM-726
def test_a_marker_older_than_the_database_is_a_restore_after_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_feed, 'LOCAL_CAP', 5)
    path, doc = configuration(tmp_path)
    account = accounts_of(doc)['omi-a']
    snapshots = tmp_path.resolve() / 'snapshots'
    snapshots.mkdir(mode=0o700)
    store = open_store(doc)
    try:
        for number in range(3):
            write(store, account, f'rec-{number}', now=number)
        served(store, doc)  # marker: first incarnation, high water 3
        archive = Path(doc['archive']['directory'])
        old_marker = (archive / archive_feed.MARKER).read_text()
        write(store, account, 'rec-3', now=3)  # the fourth row fills the epoch (capacity 4)
        write(store, account, 'rec-4', now=4)  # the fifth: a cap bump INSIDE the text write, the marker not yet written
        assert json.loads((archive / archive_feed.MARKER).read_text()) == json.loads(old_marker)
        backup_archive(parsed(doc).archive, snapshots / 'between.sqlite')  # a volume snapshot taken in that window
        for number in range(5, 8):
            write(store, account, f'rec-{number}', now=number)
        published = seqs(served(store, doc))
    finally:
        store.close()
    replace_database(doc, snapshots / 'between.sqlite', keep_marker=old_marker)  # database newer than its marker
    store = open_store(doc, clock=T0 + 3600)
    try:
        write(store, account, 'fresh', now=50)
        later = seqs(served(store, doc))
        assert later[-1] > max(published) and len(set(later)) == len(later)
        assert not set(later[5:]) & set(published[5:]) and later[-1] // archive_feed.BASE > max(published) // archive_feed.BASE
    finally:
        store.close()


# spec: ANYM-726
def test_two_restores_of_one_snapshot_never_share_a_journal_id_whatever_the_marker_says(tmp_path, monkeypatch):
    """Sol r3: an older marker after a restart, a marker ahead in rows, and a rotation rolled back after its marker was renamed
    each left the journal unchanged while two clock-behind timelines picked one incarnation."""
    monkeypatch.setattr(archive_feed, 'LOCAL_CAP', 5)
    path, doc = configuration(tmp_path)
    account = accounts_of(doc)['omi-a']
    archive = Path(doc['archive']['directory'])
    snapshots = tmp_path.resolve() / 'snapshots'
    snapshots.mkdir(mode=0o700)
    store = open_store(doc)
    try:
        for number in range(3):
            write(store, account, f'rec-{number}', now=number)
        original = journal_of(store, doc)
        older_marker = (archive / archive_feed.MARKER).read_text()
        write(store, account, 'rec-3', now=3)
        write(store, account, 'rec-4', now=4)  # a cap bump inside the text write, its marker not yet written
        backup_archive(parsed(doc).archive, snapshots / 'between.sqlite')
        for number in range(5, 8):
            write(store, account, f'rec-{number}', now=number)
        journal_of(store, doc)
        newest_marker = (archive / archive_feed.MARKER).read_text()
    finally:
        store.close()
    seen = {original}
    for stale in (older_marker, newest_marker):  # a marker older than the database; a marker ahead of it
        for attempt in range(2):
            replace_database(doc, snapshots / 'between.sqlite', keep_marker=stale)
            store = open_store(doc, clock=T0)
            try:
                write(store, account, f'timeline-{attempt}', now=40)
                journal = journal_of(store, doc)
                assert journal not in seen
                seen.add(journal)
            finally:
                store.close()
    real = archive_feed.write_marker  # a lost marker, and the marker write dies after the rename and before the commit
    def torn(*args):
        real(*args)
        raise OSError('directory fsync failed')
    for attempt in range(2):
        replace_database(doc, snapshots / 'between.sqlite')
        store = open_store(doc, clock=T0)
        try:
            store.clock.now += 120
            monkeypatch.setattr(archive_feed, 'write_marker', torn)
            with pytest.raises(Failure, match='archive_unavailable'):
                store.run('feed_page', view_of(doc), 0, 10)
            monkeypatch.setattr(archive_feed, 'write_marker', real)
            journal = journal_of(store, doc)
            assert journal not in seen
            seen.add(journal)
        finally:
            store.close()


# spec: ANYM-726
def test_a_rolled_back_bump_does_not_suppress_the_next_one(tmp_path):
    path, doc = configuration(tmp_path)
    account = accounts_of(doc)['omi-a']
    snapshots = tmp_path.resolve() / 'snapshots'
    snapshots.mkdir(mode=0o700)
    store = open_store(doc)
    try:
        for number in range(3):
            write(store, account, f'rec-{number}', now=number)
        backup_archive(parsed(doc).archive, snapshots / 'older.sqlite')
        for number in range(3, 6):
            write(store, account, f'rec-{number}', now=number)
        published = seqs(served(store, doc))
    finally:
        store.close()
    saved = (Path(doc['archive']['directory']) / archive_feed.MARKER).read_text()
    replace_database(doc, snapshots / 'older.sqlite', keep_marker=saved)  # marker ahead of the database
    store = open_store(doc, clock=T0 + 3600)
    try:
        store.discover(account, [{'id': 'next', 'title': 'T'}], None, 60)
        def refuse():
            raise Failure('archive_authorization_changed')
        with pytest.raises(Failure, match='archive_authorization_changed'):
            store.observe(account, 'next', {'id': 'next', 'title': 'T'}, {'transcript': ('available', 'words', None)}, 61, guard=refuse)
        assert store.feed_handled == {}  # the rolled-back bump left no memory behind
        store.observe(account, 'next', {'id': 'next', 'title': 'T'}, {'transcript': ('available', 'words', None)}, 62)
        later = seqs(served(store, doc))
        assert later[-1] > max(published) and later[-1] not in published
    finally:
        store.close()


# spec: ANYM-726
def test_a_page_serves_only_rows_the_marker_covers(tmp_path, monkeypatch):
    path, doc = configuration(tmp_path)
    account = accounts_of(doc)['omi-a']
    store = open_store(doc)
    try:
        for number in range(3):
            write(store, account, f'rec-{number}', now=number)
        original = Store.feed_sync
        def racing(self, view, now=None):
            original(self, view, now)
            if not racing.done:  # a collector commits between the sync and the page read
                racing.done = True
                write(self, account, 'racer', now=9)
        racing.done = False
        monkeypatch.setattr(Store, 'feed_sync', racing)
        store.clock.now += 120
        first = store.run('feed_page', view_of(doc), 0, 100)
        assert len(first['events']) == 3 and first['has_more'] is False  # the raced row is not covered yet
        monkeypatch.undo()
        second = store.run('feed_page', view_of(doc), first['next_after'], 100)
        assert len(second['events']) == 1
        marker = archive_feed.read_marker(Path(doc['archive']['directory']))
        assert marker['high_water'] == 4  # the marker covered the row before it was served
        assert store.run('feed_head', view_of(doc))['head'] == seqs(second['events'])[-1]
    finally:
        store.close()


# spec: ANYM-726
def test_access_events_respect_the_epoch_capacity_one_by_one(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_feed, 'LOCAL_CAP', 5)
    path, doc = configuration(tmp_path)
    doc['principals'] = [{'id': f'agent-{n}', 'key_sha256': sha(f'key-{n}'), 'expires_at': '2099-01-01T00:00:00Z',
                          'accounts': ['omi-a', 'omi-b'], 'permissions': ['recordings:read', 'recordings:content']} for n in range(4)]
    store = open_store(doc)
    try:
        store.run('feed_sync', view_of(doc))
        emptied = copy.deepcopy(doc)
        emptied['principals'] = []
        store.run('feed_sync', view_of(emptied))  # eight retractions in one sync
        events = served(store, emptied)
        assert len(events) == 8
        assert max(s % archive_feed.BASE for s in seqs(events)) <= 4 and seqs(events) == sorted(set(seqs(events)))
        assert len({s // archive_feed.BASE for s in seqs(events)}) == 2
    finally:
        store.close()


# spec: ANYM-726
def test_the_feed_meets_the_router_reference_consumer_rules(tmp_path):
    async def run():
        async with running(tmp_path, populate=250) as (app, client, path, doc):
            head = (await get(client, '/v1/events/head')).json()
            cursor, journal, total, previous = 0, head['journal_id'], 0, 0
            for _ in range(10):
                response = await get(client, f'/v1/events?after={cursor}&limit=100')
                assert response.status_code == 200 and len(response.content) <= 256 * 1024
                assert not response.is_redirect
                page = response.json()
                assert page['journal_id'] == journal and page['next_after'] >= cursor and len(page['events']) <= 100
                assert all(previous < s for s in seqs(page['events'])) if page['events'] else True
                previous = seqs(page['events'])[-1] if page['events'] else previous
                total += len(page['events'])
                assert page['retention_floor'] <= cursor or cursor == 0
                if not page['has_more']:
                    break
                assert page['next_after'] > cursor
                cursor = page['next_after']
            assert total == 250 and cursor + 0 <= head['head'] + archive_feed.BASE
    asyncio.run(run())
