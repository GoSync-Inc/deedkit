"""ANYM-478: search with live: archive first, then one bounded wait for live Telegram searches (synthetic DB).

No test reaches a provider: a fake provider stands in for the collector's live search.
"""
import asyncio
import threading
import time

import pytest

from messaging_service import commands, live_search, send_wait
from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState
from messaging_service.commands import CommandRejected, CommandWorker
from messaging_service.envelope import stable_id
from messaging_service.store import APIError
from test_archive import event

ACCOUNT = 'live-search-fixture'
SECOND = 'live-search-fixture-2'
WHATSAPP = 'live-search-whatsapp'


def connect(s, archive, account):
    epoch = archive.acquire(account, 'synthetic-live-worker')
    state = CollectorState(archive, account, ('synthetic-live-worker', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    s.set_messenger_operations(account, ['search_messages_live'])
    return state


@pytest.fixture
def live(seeded):
    s, _, _ = seeded
    archive = Archive(s)
    for account in (ACCOUNT, SECOND):
        archive.register(account, 'telegram', 'Synthetic live search', discover=True)
        for chat in ('100', '200'):
            archive.ingest(event(account=account, chat=chat, provider_id='1', direction='incoming', body='Synthetic'))
    key = s.issue_key('live-agent', [ACCOUNT], permissions=['read', 'read-live'])
    states = {account: connect(s, archive, account) for account in (ACCOUNT, SECOND)}
    yield s, archive, key, states[ACCOUNT], states
    for account, state in states.items():
        archive.release(account, 'synthetic-live-worker', state.lease[1])


def chat(provider, account=ACCOUNT):
    return stable_id('chat', account, provider)


def queued(s):
    with s.pool.connection() as c:
        return c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n']


def queued_parameters(s):
    with s.pool.connection() as c:
        return [r['payload']['parameters'] for r in c.execute('SELECT payload FROM messenger_commands ORDER BY created_at')]


def run(s, key, arguments, workers=(), **options):
    """The live search, with fake collectors settling queued operations while it waits."""
    async def go():
        task = asyncio.create_task(live_search.search(s, key, arguments, **options))
        while not task.done():
            for worker in workers:
                await worker.once(); await asyncio.gather(*list(worker.settling))
            await asyncio.sleep(0.05)
        return task.result()
    return asyncio.run(go())


def found(archive, account=ACCOUNT, provider_chat='100'):
    """A fake provider: the live search finds a message the archive did not have and archives it."""
    async def provider(job):
        await asyncio.to_thread(archive.ingest, event(account=account, chat=provider_chat, provider_id='7',
                                                      direction='incoming', body='needle from telegram'))
        hit = {'chat': chat(provider_chat, account), 'provider_id': '7', 'text': 'needle from telegram'}
        return {'outcome': 'confirmed', 'data': {'hits': [hit], 'next_cursor': None, 'provider_total_approx': 1,
                                                 'stored_new': 1}}
    return provider


# spec: ANYM-478
def test_live_off_keeps_the_archive_answer_and_only_adds_a_hint_when_thin(live):
    s, _, key, _, _ = live
    thin = s.search(key, 'needle')
    assert thin['items'] == [] and thin['result_evidence']['empty_reason'] in live_search.THIN
    assert thin['result_evidence']['suggested_call'] == {'tool': 'messages_search', 'args': {'live': 'on'}}
    assert 'live' not in thin['result_evidence']
    full = s.search(key, 'synthetic')
    assert full['items'] and 'suggested_call' not in full['result_evidence']
    assert asyncio.run(live_search.search(s, key, {'query': 'needle'})) == s.search(key, 'needle')
    assert queued(s) == 0


# spec: ANYM-478
@pytest.mark.parametrize('arguments,fields', [
    ({'chats': [chat('100'), chat('200')]}, ['chats']),
    ({'chats': []}, ['chats']),
    ({'senders': ['synthetic-sender']}, ['senders']),
    ({'query_mode': 'web'}, ['query_mode']),
    ({'fuzzy': True}, ['fuzzy']),
    ({'query': ''}, ['query']),
])
def test_filters_live_search_cannot_apply_give_unsupported_filter_and_queue_nothing(live, arguments, fields):
    s, _, key, _, _ = live
    result = run(s, key, {'query': 'needle', **arguments}, live='on', wait_s=0)
    entry, = result['result_evidence']['live']
    assert entry['state'] == 'unsupported_filter' and entry['fields'] == fields and entry['account'] == ACCOUNT
    assert 'items' in result and queued(s) == 0


# spec: ANYM-478
def test_channels_follow_the_archive_default(live):
    s, _, key, _, _ = live
    run(s, key, {'query': 'needle'}, live='on', wait_s=0, request_id='synthetic-channels-1')
    run(s, key, {'query': 'needle', 'include_channels': True}, live='on', wait_s=0, request_id='synthetic-channels-2')
    run(s, key, {'query': 'needle', 'chat_types': ['channel', 'private'], 'exclude_channels': True},
        live='on', wait_s=0, request_id='synthetic-channels-3')
    run(s, key, {'query': 'needle', 'chats': [chat('100')]}, live='on', wait_s=0, request_id='synthetic-channels-4')
    default, channels, private, one = queued_parameters(s)
    assert 'chat_types' not in default and default['query'] == 'needle'
    assert channels['chat_types'] == ['private', 'group', 'channel']
    assert private['chat_types'] == ['private']
    assert 'chat_types' not in one
    with s.pool.connection() as c:
        assert c.execute('SELECT chat FROM messenger_commands ORDER BY created_at').fetchall()[-1]['chat'] == chat('100')


# spec: ANYM-478
def test_a_continuation_page_never_queues_a_live_search(live):
    s, _, key, _, _ = live
    first = s.search(key, 'synthetic', limit=1)
    result = run(s, key, {'query': 'synthetic', 'limit': 1, 'after': first['next_cursor']}, live='on')
    assert [e['state'] for e in result['result_evidence']['live']] == ['first_page_only']
    auto = run(s, key, {'query': 'needle', 'after': first['next_cursor']}, live='auto')
    assert [e['state'] for e in auto['result_evidence']['live']] == ['first_page_only']
    assert queued(s) == 0


# spec: ANYM-478
def test_live_on_waits_for_the_search_and_rereads_the_archive(live):
    s, archive, key, state, _ = live
    started = time.monotonic()
    result = run(s, key, {'query': 'needle'}, [CommandWorker(state, found(archive))], live='on', wait_s=10)
    assert time.monotonic() - started < 8
    assert [m['body'] for m in result['items']] == ['needle from telegram']
    entry, = result['result_evidence']['live']
    assert entry['state'] == 'done' and entry['stored_new'] == 1 and entry['operation_id'].startswith('op_')
    assert [h['provider_id'] for h in entry['hits']] == ['7']
    assert 'suggested_call' not in result['result_evidence'] and result['result_evidence']['live_wait'] == 'done'


# spec: ANYM-478
def test_the_same_request_id_queues_no_new_provider_request(live):
    s, _, key, _, _ = live
    first = run(s, key, {'query': 'needle'}, live='on', wait_s=0, request_id='synthetic-repeat-1')
    again = run(s, key, {'query': 'needle'}, live='on', wait_s=0, request_id='synthetic-repeat-1')
    assert queued(s) == 1
    assert first['result_evidence']['live'][0]['operation_id'] == again['result_evidence']['live'][0]['operation_id']
    assert again['result_evidence']['live'][0]['state'] == 'pending'


# spec: ANYM-478
def test_partial_completion_reports_done_and_pending(live):
    s, archive, _, state, _ = live
    key = s.issue_key('live-agent-2', [ACCOUNT, SECOND], permissions=['read', 'read-live'])
    result = run(s, key, {'query': 'needle'}, [CommandWorker(state, found(archive))], live='on', wait_s=1)
    entries = {e['account']: e for e in result['result_evidence']['live']}
    assert entries[ACCOUNT]['state'] == 'done'
    assert entries[SECOND]['state'] == 'pending' and entries[SECOND]['operation_id'].startswith('op_')
    assert result['result_evidence']['live_wait'] == 'timed_out'


# spec: ANYM-478
def test_rate_limited_and_provider_failure_keep_the_archive_answer(live):
    s, _, key, state, _ = live
    async def limited(job): raise CommandRejected('rate_limited', retry_after=7)
    result = run(s, key, {'query': 'synthetic'}, [CommandWorker(state, limited)], live='on', wait_s=5)
    assert result['items'] and result['result_evidence']['live'][0]['state'] == 'rate_limited'
    assert result['result_evidence']['live'][0]['retry_after'] == 7
    async def broken(job): raise RuntimeError('synthetic provider failure')
    result = run(s, key, {'query': 'synthetic'}, [CommandWorker(state, broken)], live='on', wait_s=5,
                 request_id='synthetic-broken-1')
    assert result['items'] and result['result_evidence']['live'][0]['state'] == 'unavailable'


# spec: ANYM-478
def test_saturated_live_waits_leave_send_waits_available(live):
    s, _, key, _, _ = live
    held = [live_search._WAIT_SLOTS.acquire(blocking=False) for _ in range(live_search.MAX_CONCURRENT_WAITS)]
    try:
        assert all(held)
        started = time.monotonic()
        result = run(s, key, {'query': 'needle'}, live='on', wait_s=10)
        assert time.monotonic() - started < 5
        assert result['result_evidence']['live'][0]['state'] == 'pending'
        assert result['result_evidence']['live_wait'] == 'busy'
        assert send_wait._WAIT_SLOTS.acquire(blocking=False)
        send_wait._WAIT_SLOTS.release()
    finally:
        for _ in held: live_search._WAIT_SLOTS.release()


# spec: ANYM-478
def test_a_failure_to_queue_gives_unavailable_and_the_archive_rows(live, monkeypatch):
    s, _, key, _, _ = live
    def insert_fails(*args, **kwargs): raise RuntimeError('synthetic insert failure')
    monkeypatch.setattr(commands, 'enqueue', insert_fails)
    result = run(s, key, {'query': 'synthetic'}, live='on')
    assert result['items']
    assert result['result_evidence']['live'][0] == {'account': ACCOUNT, 'stored_new': 0, 'hits': [],
                                                   'state': 'unavailable', 'reason': 'operation_queue_failed'}


# spec: ANYM-478
def test_an_unavailable_session_is_unavailable_at_once(live):
    s, _, key, state, _ = live
    state.phase('connection', 'retry_wait', 'provider_disconnected')
    started = time.monotonic()
    result = run(s, key, {'query': 'needle'}, live='on', wait_s=10)
    assert time.monotonic() - started < 5
    entry, = result['result_evidence']['live']
    assert entry['state'] == 'unavailable' and entry['reason'] == 'session_unavailable' and queued(s) == 0


# spec: ANYM-478
def test_without_read_live_or_the_account_opt_in_nothing_is_queued(live):
    s, _, _, _, _ = live
    reader = s.issue_key('live-reader', [ACCOUNT], permissions=['read'])
    entry, = run(s, reader, {'query': 'needle'}, live='on')['result_evidence']['live']
    assert entry['state'] == 'not_permitted'
    key = s.issue_key('live-agent-3', [ACCOUNT], permissions=['read', 'read-live'])
    s.set_messenger_operations(ACCOUNT, [])
    entry, = run(s, key, {'query': 'needle'}, live='on')['result_evidence']['live']
    assert entry['state'] == 'not_enabled'
    assert queued(s) == 0


# spec: ANYM-478
def test_a_key_revoked_during_the_wait_gets_the_auth_error(live):
    s, _, key, _, _ = live
    timer = threading.Timer(0.3, lambda: s.revoke(key))
    timer.start()
    try:
        with pytest.raises(APIError) as error:
            run(s, key, {'query': 'needle'}, live='on', wait_s=1)
        assert error.value.status == 401
    finally:
        timer.cancel()


# spec: ANYM-478
def test_auto_runs_live_only_when_the_archive_answer_is_thin(live):
    s, _, key, _, _ = live
    result = run(s, key, {'query': 'synthetic'}, live='auto')
    assert result['items'] and result['result_evidence']['live'] == [] and queued(s) == 0
    result = run(s, key, {'query': 'needle'}, live='auto', wait_s=0)
    assert result['result_evidence']['live'][0]['state'] == 'pending' and queued(s) == 1


# spec: ANYM-478
def test_whatsapp_chats_still_loading_suggest_a_history_request(live):
    s, archive, _, _, _ = live
    archive.register(WHATSAPP, 'whatsapp', 'Synthetic WhatsApp', discover=True)
    for provider in range(6):
        archive.ingest(event(account=WHATSAPP, platform='whatsapp', chat=str(900 + provider), provider_id='1', direction='incoming',
                             body='Synthetic'))
    key = s.issue_key('live-agent-wa', [WHATSAPP], permissions=['read', 'read-live'])
    hint = s.search(key, 'needle')['result_evidence']['suggested_call']
    assert hint['args'] == {'account': WHATSAPP, 'operation': 'request_chat_history'} and len(hint['chat_ids']) == 5
    entry, = run(s, key, {'query': 'needle'}, live='on')['result_evidence']['live']
    assert entry['state'] == 'not_supported_by_provider' and len(entry['suggested_call']['chat_ids']) == 5
    assert queued(s) == 0


# spec: ANYM-478
@pytest.mark.parametrize('live_value,wait_s,request_id,code', [
    ('sometimes', None, '', 'invalid_filter'), ('on', 21, '', 'invalid_wait_s'), ('on', True, '', 'invalid_wait_s'),
    ('on', None, 'bad id!', 'invalid_request_id')])
def test_invalid_live_options_are_refused_before_anything_runs(live, live_value, wait_s, request_id, code):
    s, _, key, _, _ = live
    with pytest.raises(APIError, match=code):
        run(s, key, {'query': 'needle'}, live=live_value, wait_s=wait_s, request_id=request_id)
    assert queued(s) == 0


# spec: ANYM-478
def test_the_live_search_receipt_counts_new_archive_rows():
    from types import SimpleNamespace
    from messaging_service.telegram_collector import TelegramCollector
    async def provider(job): return {'outcome': 'confirmed', 'data': {'hits': [], 'next_cursor': None}}
    async def store(found, **kwargs): return 3
    collector = SimpleNamespace(state=SimpleNamespace(policy=lambda: {}), provider=SimpleNamespace(execute_operation=provider),
                                store_live_hits=store)
    answer = asyncio.run(TelegramCollector.execute_operation(collector, {'operation': 'search_messages_live'}))
    assert answer['data']['stored_new'] == 3


# spec: ANYM-478
def test_a_named_chat_keeps_the_archive_account_and_chat_type_filters(live):
    s, _, _, _, _ = live
    key = s.issue_key('live-agent-both', [ACCOUNT, SECOND], permissions=['read', 'read-live'])
    with s.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type='channel' WHERE id=%s", (chat('100'),))
    named = {'query': 'needle', 'chat': chat('100')}
    for extra in ({'accounts': []}, {'account': SECOND}, {'accounts': [SECOND]},
                  {'exclude_channels': True}, {'chat_types': ['private']}):
        result = run(s, key, {**named, **extra}, live='on', wait_s=0)
        assert result['result_evidence']['live'] == [] and 'items' in result, extra
    assert queued(s) == 0
    result = run(s, key, {**named, 'chat_types': ['channel']}, live='on', wait_s=0)
    assert [e['account'] for e in result['result_evidence']['live']] == [ACCOUNT] and queued(s) == 1


# spec: ANYM-478
def test_a_failing_live_plan_or_hint_keeps_the_archive_answer(live, monkeypatch):
    s, _, key, _, _ = live
    def plan_fails(*args, **kwargs): raise RuntimeError('synthetic planning failure')
    def hint_fails(c, *args): c.execute('SELECT 1/0')
    monkeypatch.setattr(live_search, 'plan', plan_fails)
    result = run(s, key, {'query': 'synthetic'}, live='on')
    assert result['items'] and result['result_evidence']['live'] == []
    assert result['result_evidence']['live_wait'] == 'failed'
    def plan_times_out(c, *args, **kwargs): c.execute('SET LOCAL statement_timeout = 1'); c.execute('SELECT pg_sleep(1)')
    monkeypatch.setattr(live_search, 'plan', plan_times_out)
    result = run(s, key, {'query': 'synthetic'}, live='on')
    assert result['items'] and result['result_evidence']['live_wait'] == 'failed'
    def plan_forbidden(*args, **kwargs): raise APIError('forbidden', 403)
    monkeypatch.setattr(live_search, 'plan', plan_forbidden)
    with pytest.raises(APIError, match='forbidden'): run(s, key, {'query': 'synthetic'}, live='on')
    monkeypatch.setattr(live_search, 'suggest', hint_fails)
    thin = s.search(key, 'needle')
    assert thin['items'] == [] and thin['result_evidence']['empty_reason'] in live_search.THIN
    assert 'suggested_call' not in thin['result_evidence'] and queued(s) == 0
