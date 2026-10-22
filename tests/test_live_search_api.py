"""ANYM-475: live search authorization, session check and cursor binding at the operations API (synthetic DB)."""
import asyncio

import pytest

from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState
from messaging_service.envelope import stable_id
from messaging_service.messenger_operations import encode_live_cursor, live_cursor_binding
from messaging_service.store import APIError
from test_archive import event

ACCOUNT = 'live-search-fixture'


@pytest.fixture
def live(seeded):
    s, _, _ = seeded
    archive = Archive(s)
    archive.register(ACCOUNT, 'telegram', 'Synthetic live search', discover=True)
    for chat in ('100', '200'):
        archive.ingest(event(account=ACCOUNT, chat=chat, provider_id='1', direction='incoming', body='Synthetic'))
    key = s.issue_key('live-agent', [ACCOUNT], permissions=['read', 'read-live'])
    epoch = archive.acquire(ACCOUNT, 'synthetic-live-worker')
    state = CollectorState(archive, ACCOUNT, ('synthetic-live-worker', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    s.set_messenger_operations(ACCOUNT, ['search_messages_live'])
    yield s, archive, key, state
    archive.release(ACCOUNT, 'synthetic-live-worker', epoch)


def chat(provider):
    return stable_id('chat', ACCOUNT, provider)


def search(s, key, request_id='synthetic-live-0001', chat_id='', **parameters):
    return s.execute_messenger_operation(key, account=ACCOUNT, operation='search_messages_live', chat_id=chat_id,
                                         parameters={'query': 'synthetic', **parameters}, request_id=request_id)


def queued(s):
    with s.pool.connection() as c:
        return c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n']


# spec: ANYM-475
def test_global_needs_full_account_and_chat_must_be_in_scope(live):
    s, _, key, _ = live
    assert search(s, key)['status'] == 'queued'
    assert search(s, key, 'synthetic-live-0002', chat_id=chat('100'))['status'] == 'queued'
    s.set_access('live-agent', chats=[chat('100')])
    with pytest.raises(APIError, match='forbidden'):
        search(s, key, 'synthetic-live-0003')
    for outside in (chat('200'), chat('999')):  # out of scope and unknown look the same
        with pytest.raises(APIError, match='forbidden'):
            search(s, key, 'synthetic-live-0004', chat_id=outside)
    assert search(s, key, 'synthetic-live-0005', chat_id=chat('100'))['status'] == 'queued'


# spec: ANYM-475
def test_unavailable_session_is_refused_at_once_and_nothing_is_queued(live):
    s, _, key, state = live
    state.phase('connection', 'retry_wait', 'provider_disconnected')
    with pytest.raises(APIError, match='session_unavailable'):
        search(s, key)
    assert queued(s) == 0


# spec: ANYM-475
def test_cursor_is_bound_to_account_chat_and_fields(live):
    s, _, key, _ = live
    fields = {'query': 'synthetic', 'limit': 20}
    good = encode_live_cursor(live_cursor_binding(ACCOUNT, '', fields), {'i': 5})
    assert search(s, key, cursor=good)['status'] == 'queued'
    for request_id, changes in (('synthetic-live-0002', {'limit': 10}), ('synthetic-live-0003', {'filter': 'voice'})):
        with pytest.raises(APIError, match='invalid_cursor'):
            search(s, key, request_id, cursor=good, **changes)
    with pytest.raises(APIError, match='invalid_cursor'):
        search(s, key, 'synthetic-live-0004', chat_id=chat('100'), cursor=good)


# spec: ANYM-475
def test_worker_round_trip_keeps_hits_and_retry_after(live):
    from messaging_service.commands import CommandRejected, CommandWorker
    s, _, key, state = live
    first = search(s, key)
    async def provider(job):
        assert job['_policy']['operations_enabled'] == ['search_messages_live'] and job['chat_id'] == ''
        return {'outcome': 'confirmed', 'data': {'hits': [], 'next_cursor': None, 'provider_total_approx': 0}}
    assert asyncio.run(CommandWorker(state, provider).once())
    assert s.get_messenger_operation(key, first['operation_id'])['result']['data']['hits'] == []
    second = search(s, key, 'synthetic-live-0002')
    async def limited(job):raise CommandRejected('rate_limited', retry_after=7)
    assert asyncio.run(CommandWorker(state, limited).once())
    receipt = s.get_messenger_operation(key, second['operation_id'])
    assert receipt['status'] == 'failed' and receipt['result']['data'] == {'code': 'rate_limited', 'retry_after': 7}
