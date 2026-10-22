"""ANYM-477: request_chat_history on Telegram is one governed history page; synthetic fakes only, no provider."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telethon import errors, functions, types

from messaging_service.commands import CommandRejected, CommandWorker
from messaging_service.envelope import stable_id
from messaging_service.messenger_operations import operation_manifest, validate_operation
from messaging_service.telegram_governor import LIVE_BUDGETS, ProviderRateLimited
from messaging_service.telegram_operations import execute
from test_live_search import GROUP, USER, Client, governed, job as search_job, once

ACCOUNT = 'synthetic-telegram'
POLICY = {'enabled': True, 'discover_chats': True, 'include_chats': [], 'exclude_chats': [],
          'operations_enabled': ['request_chat_history', 'search_messages_live']}
WHEN = 1_799_990_000
M1, M2 = stable_id('msg', ACCOUNT, '-22', '1'), stable_id('msg', ACCOUNT, '-22', '2')


def message(number, *, when=WHEN, peer=None):
    return types.Message(number, peer or types.PeerChat(22), datetime.fromtimestamp(when, timezone.utc), 'synthetic')


def page(messages):
    return types.messages.Messages(messages, [], [GROUP], [USER])


def job(parameters=None, *, anchor=None):
    params = validate_operation('telegram', 'request_chat_history', parameters or {})
    return {'operation': 'request_chat_history', 'platform': 'telegram', 'parameters': params, 'expires_at': 4e9,
            'account': ACCOUNT, 'chat_id': stable_id('chat', ACCOUNT, '-22'), 'provider_chat': '-22',
            'provider_message_id': anchor, '_policy': POLICY}


def run(client, value, governor=None):
    return asyncio.run(execute(client, value, governor=governor))


def request(client):
    sent = [r for r in client.requests if isinstance(r, functions.messages.GetHistoryRequest)]
    assert len(sent) == 1 and len(client.requests) == 1
    return sent[0]


# spec: ANYM-477
def test_fields_count_and_exactly_one_anchor_per_platform():
    assert validate_operation('telegram', 'request_chat_history', {}) == {'count': 50}
    assert validate_operation('telegram', 'request_chat_history', {'count': 100, 'before_message': M1})['count'] == 100
    assert validate_operation('telegram', 'request_chat_history', {'from_time': '2026-10-01T00:00:00Z'})
    for bad in ({'count': 101}, {'count': 0}, {'before_message': M1, 'around_message': M2},
                {'before_message': M1, 'from_time': '2026-10-01T00:00:00Z'},
                {'around_message': M1, 'to_time': '2026-10-01T00:00:00Z'},
                {'from_time': '2026-10-02T00:00:00Z', 'to_time': '2026-10-01T00:00:00Z'}, {'before_message': ''}):
        with pytest.raises(ValueError, match='invalid_operation_parameters'):
            validate_operation('telegram', 'request_chat_history', bad)
    telegram = {r['operation']: r for r in operation_manifest('telegram')}['request_chat_history']
    native = {r['operation']: r for r in operation_manifest('whatsapp', provider='whatsmeow')}['request_chat_history']
    assert telegram['supported'] and telegram['mutation'] is False and telegram['permission'] == 'read-live'
    assert set(telegram['parameters_schema']) == {'count', 'before_message', 'around_message', 'from_time', 'to_time'}
    assert native['supported'] and native['mutation'] is True and set(native['parameters_schema']) == {'count'}
    assert native['parameters_schema']['count']['maximum'] == 50
    with pytest.raises(ValueError, match='invalid_operation_parameters'):  # WhatsApp keeps its own fields and 1-50
        validate_operation('whatsapp', 'request_chat_history', {'before_message': M1}, provider='whatsmeow')


# spec: ANYM-477
def test_before_anchor_is_one_exclusive_page_and_reports_the_history_start():
    client = Client(page([message(n) for n in range(40, 30, -1)]))
    data = run(client, job({'count': 10, 'before_message': M1}, anchor='41'))['data']
    sent = request(client)
    assert (sent.offset_id, sent.add_offset, sent.limit, sent.offset_date) == (41, 0, 10, None)
    assert client.resolved == [-22]
    assert data == {'delivery': 'immediate', 'fetched': 10, 'provider_history_start_reached': False,
                    'basis': 'live_provider_history'}
    short = run(Client(page([message(2), message(1)])), job({'count': 10}, anchor='3'))['data']
    assert short['fetched'] == 2 and short['provider_history_start_reached'] is True


# spec: ANYM-477
def test_empty_chat_without_anchor_starts_from_the_newest_message():
    client = Client(page([]))
    data = run(client, job({'count': 5}))['data']
    assert (request(client).offset_id, request(client).add_offset) == (0, 0)
    assert data['fetched'] == 0 and data['provider_history_start_reached'] is True


# spec: ANYM-477
def test_around_anchor_takes_half_before_and_half_after():
    client = Client(page([message(n) for n in range(55, 45, -1)]))
    data = run(client, job({'count': 10, 'around_message': M1}, anchor='50'))['data']
    sent = request(client)
    assert (sent.offset_id, sent.add_offset, sent.limit) == (50, -5, 10)
    assert data['fetched'] == 10 and 'provider_history_start_reached' not in data and 'range_exhausted' not in data


# spec: ANYM-477
def test_time_range_is_inclusive_from_and_exclusive_to_and_reports_range_exhausted():
    since, until = WHEN - 100, WHEN
    client = Client(page([message(9, when=until - 1), message(8, when=since), message(7, when=since - 1)]))
    data = run(client, job({'count': 3, 'from_time': '2027-01-15T05:11:40Z', 'to_time': '2027-01-15T05:13:20Z'}))['data']
    sent = request(client)
    assert (sent.offset_id, sent.offset_date.timestamp() if sent.offset_date else None) == (0, until)
    assert data['fetched'] == 2 and data['range_exhausted'] is True and 'provider_history_start_reached' not in data
    full = run(Client(page([message(n, when=until - n) for n in (1, 2, 3)])),
               job({'count': 3, 'from_time': '2027-01-15T05:11:40Z', 'to_time': '2027-01-15T05:13:20Z'}))['data']
    assert full['fetched'] == 3 and full['range_exhausted'] is False


# spec: ANYM-477
def test_another_chats_message_in_the_page_is_never_kept():
    found = []
    value = job({'count': 3}, anchor='10'); value['_history'] = found
    data = run(Client(page([message(9), message(8, peer=types.PeerUser(11))])), value)['data']
    assert data['fetched'] == 1 and len(found) == 1


# spec: ANYM-477
@pytest.mark.parametrize('error,code', [
    (errors.ChannelPrivateError(None), 'not_found'), (errors.RPCError(None, 'SYNTHETIC_ERROR', 400), 'operation_rejected')])
def test_provider_refusal_maps_to_a_safe_code(error, code):
    with pytest.raises(CommandRejected) as refused:
        run(Client(error=error), job(anchor='3'))
    assert refused.value.args[0] == code


# spec: ANYM-477
def test_budget_is_three_then_one_per_ten_seconds_and_300_a_day_before_any_call():
    assert LIVE_BUDGETS['chat_history'] == (3, 10.0, 300)
    clock, state, governor = governed()
    client = Client(page([]))
    run(client, job(), governor)  # entity resolution and the page: two provider requests
    with pytest.raises(CommandRejected) as refused:
        run(client, job(), governor)
    assert refused.value.args[0] == 'rate_limited' and 1 <= refused.value.retry_after <= 10
    assert len(client.requests) == 1
    clock.advance(10)
    run(client, job(), governor)
    assert len(client.requests) == 2
    run(Client(page([])), search_job(), governor)  # live search keeps its own budget


# spec: ANYM-477
def test_history_flood_wait_pauses_only_agent_history():
    clock, state, governor = governed()
    with pytest.raises(CommandRejected) as flooded:
        run(Client(error=errors.FloodWaitError(None, capture=300)), job(), governor)
    assert flooded.value.retry_after == 300 and state.deferred == []
    calls = []
    async def other(name):
        async def work():calls.append(name)
        await governor.call(work, name)
    asyncio.run(other('provider_send')); asyncio.run(other('telegram_history'))
    assert calls == ['provider_send', 'telegram_history']
    run(Client(page([])), search_job(), governor)  # the search scope is not paused either
    client = Client(page([]))
    with pytest.raises(CommandRejected) as paused:
        run(client, job(), governor)
    assert paused.value.retry_after >= 290 and client.requests == []


# spec: ANYM-477
def test_send_flood_during_history_pause_sets_the_account_cooldown():
    clock, state, governor = governed()
    with pytest.raises(CommandRejected):
        run(Client(error=errors.FloodWaitError(None, capture=60)), job(), governor)
    async def send():raise errors.FloodWaitError(None, capture=600)
    with pytest.raises(ProviderRateLimited):
        asyncio.run(governor.call(send, 'provider_send'))
    assert state.deferred == [600]
    clock.advance(120)
    client = Client(page([]))
    with pytest.raises(CommandRejected) as paused:
        run(client, job(), governor)
    assert paused.value.retry_after >= 470 and client.requests == []


# spec: ANYM-477
@pytest.mark.parametrize('error,seconds', [
    (errors.FloodWaitError(None, capture=7200), 7200), (errors.FloodError(None, 'FLOOD_SYNTHETIC_UNKNOWN'), 60),
], ids=['long', 'unknown'])
def test_long_or_unknown_history_flood_sets_the_account_cooldown(error, seconds):
    clock, state, governor = governed()
    with pytest.raises(CommandRejected):
        run(Client(error=error), job(), governor)
    assert state.deferred == [seconds]


# spec: ANYM-477
def test_existing_account_cooldown_is_never_bypassed():
    clock, state, governor = governed()
    state.until = clock.now + 90
    client = Client(page([]))
    with pytest.raises(CommandRejected) as paused:
        run(client, job(), governor)
    assert paused.value.retry_after >= 90 and client.requests == []


# spec: ANYM-477
def test_worker_bounds_telegram_history_as_a_live_read_and_keeps_whatsapp_a_mutation(monkeypatch):
    import messaging_service.commands as commands
    finished, durable = [], []
    def worker(platform, mutation):
        class Worker(CommandWorker):
            def claim(self):return {'id': 'synthetic-job', 'mutation': mutation, 'operation': 'request_chat_history',
                                    'platform': platform}
            def preflight(self, job, *, verify_file=True):return job
            def finish_live(self, job, status, result, code, budget):finished.append((status, result, code))
            def finish(self, job, status, result=None, code=None):durable.append((status, result, code))
        return Worker
    monkeypatch.setattr(commands, 'LIVE_SEARCH_TIMEOUT', 0.05); monkeypatch.setattr(commands, 'LIVE_FOREGROUND_RESERVE', 0)
    async def hangs(job):await asyncio.sleep(60)
    assert once(worker('telegram', False)(SimpleNamespace(), hangs))
    assert finished[-1] == ('failed', {'data': {'code': 'provider_timeout', 'retry': True}}, 'provider_timeout')
    async def requested(job):
        return {'outcome': 'submitted', 'data': {'requested': True, 'count': 10, 'pending': True}}
    once(worker('whatsapp', True)(SimpleNamespace(), requested))
    status, result, code = durable[-1]
    assert status == 'unknown' and result['data'] == {'requested': True, 'count': 10, 'pending': True,
                                                      'delivery': 'via_phone', 'search_again_after_s': 60}
