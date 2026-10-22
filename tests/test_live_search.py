"""ANYM-475: a governed Telegram live search on the existing client; synthetic fakes only, no provider."""
import asyncio
from datetime import datetime, timezone
import logging
from types import SimpleNamespace

import pytest
from telethon import errors, functions, types

from messaging_service.commands import CommandRejected, CommandWorker, LIVE_SEARCH_TIMEOUT
from messaging_service.envelope import stable_id
from messaging_service.messenger_operations import ParameterError, live_cursor_binding, validate_operation
from messaging_service.telegram_governor import LiveBudget, ProviderRateLimited, RequestGovernor
from messaging_service.telegram_operations import LIVE_CURSORS, execute

SENTINEL = 'zebra-sentinel-query-7351'
ACCOUNT = 'synthetic-telegram'
POLICY = {'enabled': True, 'discover_chats': True, 'include_chats': [], 'exclude_chats': ['-1000000000009'],
          'operations_enabled': ['search_messages_live']}


class Clock:
    def __init__(self):self.now = 1000.0; self.wall = 1_800_000_000.0
    def __call__(self):return self.now
    def time(self):return self.wall
    async def sleep(self, seconds):self.now += seconds; self.wall += seconds
    def advance(self, seconds):self.now += seconds; self.wall += seconds


class State:
    """Fake CollectorState: the account cooldown a FloodWait would persist."""
    def __init__(self, clock):self.clock, self.until, self.deferred = clock, 0.0, []
    def cooldown_remaining(self):return max(0.0, self.until - self.clock.now)
    def defer(self, seconds):self.deferred.append(seconds); self.until = max(self.until, self.clock.now + seconds)
    def status(self, *args):pass


def message(peer, number, text, *, sender=None, when=1_799_990_000):
    return types.Message(number, peer, datetime.fromtimestamp(when, timezone.utc), text,
                         from_id=types.PeerUser(sender) if sender else None)


USER = types.User(11, access_hash=5, first_name='Synthetic', last_name='Person')
GROUP = types.Chat(22, 'Synthetic group', None, 3, datetime.fromtimestamp(0, timezone.utc), 1)
CHANNEL = types.Channel(33, 'Synthetic channel', None, datetime.fromtimestamp(0, timezone.utc), access_hash=7,
                        broadcast=True)
EXCLUDED = types.Channel(9, 'Excluded group', None, datetime.fromtimestamp(0, timezone.utc), access_hash=8,
                         megagroup=True)


class Client:
    """Fake Telethon client: records requests; one instance is the one session."""
    def __init__(self, response=None, error=None):
        self.requests, self.response, self.error, self.resolved = [], response, error, []

    async def __call__(self, request):
        self.requests.append(request)
        if self.error:raise self.error
        return self.response

    async def get_input_entity(self, value):
        self.resolved.append(value)
        return types.InputPeerChat(22)


def page(messages, *, count=None, next_rate=None):
    if count is None:return types.messages.Messages(messages, [], [GROUP, CHANNEL, EXCLUDED], [USER])
    return types.messages.MessagesSlice(count, messages, [], [GROUP, CHANNEL, EXCLUDED], [USER], next_rate=next_rate)


def job(parameters=None, *, chat=None):
    params = validate_operation('telegram', 'search_messages_live', {'query': SENTINEL, **(parameters or {})})
    return {'operation': 'search_messages_live', 'parameters': params, 'expires_at': 4e9, 'account': ACCOUNT,
            'chat_id': stable_id('chat', ACCOUNT, chat) if chat else '', 'provider_chat': chat, '_policy': POLICY}


def run(client, value, governor=None):
    return asyncio.run(execute(client, value, governor=governor))


def once(worker):
    """One worker turn; a live receipt is written inside it."""
    return asyncio.run(worker._once())


def governed():
    clock = Clock(); state = State(clock)
    return clock, state, RequestGovernor(state, clock=clock, sleep=clock.sleep, wall=clock.time)


# spec: ANYM-475
def test_fields_are_validated_and_telegram_only():
    assert validate_operation('telegram', 'search_messages_live', {'query': 'x'}) == {'query': 'x', 'limit': 20}
    normalized = validate_operation('telegram', 'search_messages_live', {
        'query': 'x', 'from_time': '2026-10-01T10:00:00+02:00', 'to_time': '2026-10-02T00:00:00Z',
        'chat_types': ['group'], 'filter': 'voice', 'limit': 50})
    assert normalized['from_time'] == '2026-10-01T08:00:00Z' and normalized['to_time'] == '2026-10-02T00:00:00Z'
    for bad in ({}, {'query': ''}, {'query': 'x' * 257}, {'query': 'x', 'limit': 51}, {'query': 'x', 'limit': 0},
                {'query': 'x', 'chat_types': []}, {'query': 'x', 'chat_types': ['bot']},
                {'query': 'x', 'filter': 'videos'}, {'query': 'x', 'from_time': '2026-10-01'},
                {'query': 'x', 'from_time': '2026-10-02T00:00:00Z', 'to_time': '2026-10-01T00:00:00Z'},
                {'query': 'x', 'cursor': 'bad cursor!'}, {'query': 'x', 'peer': 'raw'}):
        with pytest.raises(ParameterError):
            validate_operation('telegram', 'search_messages_live', bad)
    with pytest.raises(ValueError, match='unsupported_operation'):
        validate_operation('whatsapp', 'search_messages_live', {'query': 'x'}, provider='whatsmeow')


# spec: ANYM-475
def test_global_search_request_arguments_per_field():
    client = Client(page([]))
    run(client, job({'chat_types': ['private'], 'filter': 'documents', 'from_time': '2026-10-01T00:00:00.5Z',
                     'to_time': '2026-10-02T00:00:00Z', 'limit': 5}))
    [request] = client.requests
    assert isinstance(request, functions.messages.SearchGlobalRequest)
    assert request.q == SENTINEL and request.limit == 5 and request.users_only is True
    assert not request.groups_only and not request.broadcasts_only
    assert isinstance(request.filter, types.InputMessagesFilterDocument)
    # Telegram's min_date and max_date are exclusive whole seconds.
    assert request.min_date == int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
    assert request.max_date == int(datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp())
    assert isinstance(request.offset_peer, types.InputPeerEmpty) and request.offset_rate == 0
    client = Client(page([]))
    run(client, job({'filter': 'photos'}))
    [request] = client.requests
    # Default types private and group: no single-type flag, channels dropped from hits instead.
    assert not (request.users_only or request.groups_only or request.broadcasts_only)
    assert isinstance(request.filter, types.InputMessagesFilterPhotos) and request.min_date is None
    client = Client(page([])); run(client, job({'filter': 'links'}))
    assert isinstance(client.requests[0].filter, types.InputMessagesFilterUrl)
    client = Client(page([])); run(client, job({'filter': 'voice', 'chat_types': ['channel']}))
    assert isinstance(client.requests[0].filter, types.InputMessagesFilterVoice) and client.requests[0].broadcasts_only


# spec: ANYM-475
def test_chat_search_uses_messages_search_on_the_archived_chat():
    client = Client(page([message(types.PeerChat(22), 4, 'synthetic hit')]))
    result = run(client, job({'limit': 10}, chat='-22'))
    [request] = client.requests
    assert isinstance(request, functions.messages.SearchRequest) and client.resolved == [-22]
    assert request.q == SENTINEL and request.limit == 10 and request.offset_id == 0
    assert isinstance(request.peer, types.InputPeerChat)
    assert [h['provider_id'] for h in result['data']['hits']] == ['4']


# spec: ANYM-475
def test_hits_outside_the_policy_are_dropped_and_receipt_is_bounded():
    messages = [message(types.PeerUser(11), 1, 'private hit', sender=11),
                message(types.PeerChat(22), 2, 'g' * 400, sender=11),
                message(types.PeerChannel(33), 3, 'channel hit'),
                message(types.PeerChannel(9), 4, 'EXCLUDED-SENTINEL')]
    result = run(Client(page(messages, count=120, next_rate=77)), job({'limit': 4}))
    assert result['outcome'] == 'confirmed'
    data = result['data']
    assert 'EXCLUDED' not in repr(data) and '-1000000000009' not in repr(data)
    assert [h['provider_id'] for h in data['hits']] == ['1', '2']  # channel left out by default
    private, group = data['hits']
    assert private['chat'] == stable_id('chat', ACCOUNT, '11') and private['chat_type'] == 'private'
    assert private['sender'] == {'id': '11', 'name': 'Synthetic Person'}
    assert group['chat_type'] == 'group' and len(group['text']) == 300
    assert data['provider_total_approx'] == 120 and data['next_cursor']
    binding = live_cursor_binding(ACCOUNT, '', job({'limit': 4})['parameters'])
    # The excluded hit's message ID and chat stay in collector memory, never in the agent-visible cursor.
    assert LIVE_CURSORS[data['next_cursor']] == (binding, {'i': 4, 'r': 77, 'p': ['h', 9, 8]})
    following = Client(page([]))
    run(following, job({'limit': 4, 'cursor': data['next_cursor']}))
    request = following.requests[0]
    assert request.offset_id == 4 and request.offset_rate == 77 and isinstance(request.offset_peer, types.InputPeerChannel)
    with pytest.raises(CommandRejected, match='invalid_cursor'):  # the same cursor with other fields
        run(Client(page([])), job({'limit': 5, 'cursor': data['next_cursor']}))
    LIVE_CURSORS.clear()  # a collector restart forgets issued cursors
    with pytest.raises(CommandRejected, match='invalid_cursor'):
        run(Client(page([])), job({'limit': 4, 'cursor': data['next_cursor']}))
    big = [message(types.PeerChat(22), n, '😀' * 300) for n in range(1, 51)]
    from messaging_service.commands import safe_result
    assert safe_result(run(Client(page(big)), job({'limit': 50})))  # fits the 60 KB result bound


# spec: ANYM-475
def test_cursor_is_an_opaque_token_bound_to_account_chat_and_fields():
    import base64
    excluded_only = [message(types.PeerChannel(9), 123456, 'EXCLUDED-SENTINEL')]
    data = run(Client(page(excluded_only, count=9)), job({'limit': 1}))['data']
    assert data['hits'] == [] and data['next_cursor']
    decoded = base64.urlsafe_b64decode(data['next_cursor'] + '=' * (-len(data['next_cursor']) % 4))
    assert b'123456' not in decoded and b'123456' not in data['next_cursor'].encode()
    cursor = data['next_cursor']
    other_account = dict(job({'limit': 1, 'cursor': cursor}), account='synthetic-other')
    for rewritten in (job({'limit': 2, 'cursor': cursor}), job({'limit': 1, 'query': 'other', 'cursor': cursor}),
                      job({'limit': 1, 'cursor': cursor}, chat='-22'), other_account,
                      job({'limit': 1, 'cursor': cursor[:-1] + ('A' if cursor[-1] != 'A' else 'B')})):
        client = Client(page([]))
        with pytest.raises(CommandRejected, match='invalid_cursor'):
            run(client, rewritten)
        assert client.requests == [] and client.resolved == []
    following = Client(page([]))
    run(following, job({'limit': 1, 'cursor': cursor}))
    assert following.requests[0].offset_id == 123456


# spec: ANYM-475
def test_global_page_without_next_rate_continues_from_the_last_date():
    first = run(Client(page([message(types.PeerChat(22), 7, 'synthetic', when=1_799_990_000)], count=9)),
                job({'limit': 1}))['data']
    following = Client(page([]))
    run(following, job({'limit': 1, 'cursor': first['next_cursor']}))
    assert following.requests[0].offset_rate == 1_799_990_000 and following.requests[0].offset_id == 7


# spec: ANYM-475
def test_receipt_with_worst_case_names_fits_the_result_bound():
    from messaging_service.commands import safe_result
    from messaging_service.envelope import canonical
    title = '\U0001F600' * 200
    wide = types.Chat(22, title, None, 3, datetime.fromtimestamp(0, timezone.utc), 1)
    person = types.User(11, access_hash=5, first_name=title, last_name=title)
    response = types.messages.MessagesSlice(120, [message(types.PeerChat(22), n, '\U0001F600' * 300, sender=11)
                                                 for n in range(1, 51)], [], [wide], [person])
    result = run(Client(response), job({'limit': 50}))
    assert len(result['data']['hits']) == 50 and safe_result(result) and len(canonical(result)) <= 60_000


# spec: ANYM-475
def test_slow_preflight_counts_against_the_live_deadline(monkeypatch):
    import time
    import messaging_service.commands as commands
    finished = []
    class Worker(CommandWorker):
        def claim(self):return {'id': 'synthetic-job', 'mutation': False, 'operation': 'search_messages_live'}
        def preflight(self, job, *, verify_file=True):time.sleep(0.2); return job
        def finish_live(self, job, status, result, code, budget):finished.append((status, result, code))
    monkeypatch.setattr(commands, 'LIVE_SEARCH_TIMEOUT', 0.3); monkeypatch.setattr(commands, 'LIVE_FOREGROUND_RESERVE', 0)
    async def hangs(job):await asyncio.sleep(60)
    started = time.monotonic()
    assert once(Worker(SimpleNamespace(), hangs))
    assert time.monotonic() - started < 0.6
    assert finished[-1] == ('failed', {'data': {'code': 'provider_timeout', 'retry': True}}, 'provider_timeout')
    monkeypatch.setattr(commands, 'LIVE_SEARCH_TIMEOUT', 0.05); monkeypatch.setattr(commands, 'LIVE_FOREGROUND_RESERVE', 0)
    started = time.monotonic()
    once(Worker(SimpleNamespace(), hangs))  # a preflight past the whole deadline
    assert finished[-1][2] == 'provider_timeout' and time.monotonic() - started < 0.5


# spec: ANYM-475
def test_next_send_opportunity_follows_a_hung_search_within_the_deadline(monkeypatch):
    import time
    import messaging_service.commands as commands
    import messaging_service.outgoing as outgoing
    from messaging_service.telegram_collector import TelegramCollector
    monkeypatch.setattr(commands, 'LIVE_SEARCH_TIMEOUT', 0.3); monkeypatch.setattr(commands, 'LIVE_FOREGROUND_RESERVE', 0)
    monkeypatch.setattr(commands, 'LIVE_EXECUTION_TIMEOUT', 0.15)
    marks, finished, jobs = {}, [], [{'id': 'synthetic-job', 'mutation': False, 'operation': 'search_messages_live'}]
    class Sends:
        def __init__(self, *args):pass
        async def once(self):
            if 'claimed' in marks and 'send' not in marks:marks['send'] = time.monotonic()
            return False
    class Worker(CommandWorker):
        def claim(self):
            if jobs:marks['claimed'] = time.monotonic(); return jobs.pop()
        def preflight(self, job, *, verify_file=True):time.sleep(0.1); return job
        # A receipt stuck on a lock holds out its whole budget, as lock_timeout lets it.
        def finish_live(self, job, status, result, code, budget):time.sleep(budget); finished.append(code)
    class State:
        def cooldown_remaining(self):return 0
    monkeypatch.setattr(outgoing, 'OutgoingWorker', Sends); monkeypatch.setattr(commands, 'CommandWorker', Worker)
    collector = TelegramCollector.__new__(TelegramCollector)
    collector.state, collector.provider = State(), SimpleNamespace()
    async def hangs(job):await asyncio.sleep(60)
    collector.execute_operation = hangs
    async def scenario():
        loop = asyncio.create_task(collector.outgoing_loop())
        while 'send' not in marks:await asyncio.sleep(0.02)
        loop.cancel()
    asyncio.run(scenario())
    # Preflight 0.1 s + hung request to the 0.15 s execution deadline + a stuck receipt to 0.3 s + 0.25 s pacing.
    assert marks['send'] - marks['claimed'] < 0.65 and finished == ['provider_timeout']


# spec: ANYM-475
def test_budget_comes_from_collector_config_and_resets_at_utc_midnight(tmp_path):
    from messaging_service import telegram_collector
    assert telegram_collector.live_search_budget(None) == {'burst': 3, 'refill_seconds': 10, 'daily': 100}
    assert telegram_collector.live_search_budget({'daily': 20}) == {'burst': 3, 'refill_seconds': 10, 'daily': 20}
    for bad in ({'daily': 0}, {'burst': 11}, {'refill_seconds': 1.5}, {'other': 1}, [3]):
        with pytest.raises(ValueError, match='invalid_live_search_budget'):
            telegram_collector.live_search_budget(bad)
    provider = telegram_collector.TelethonProvider(
        {'session_file': str(tmp_path / 's.session'), 'live_search_budget': {'burst': 1, 'refill_seconds': 60, 'daily': 2}},
        {'api_id': 1, 'api_hash': '0' * 32, 'expected_user_id': 1}, receive_updates=False)
    clock = Clock(); provider.bind_state(State(clock))
    budget = provider.governor.budgets['search']
    assert (budget.burst, budget.refill, budget.daily) == (1, 60.0, 2)
    daily = LiveBudget(3, 10.0, 2, clock=clock, wall=clock.time)
    clock.wall = 86400 * 20000 - 30
    assert daily.take(1) == 0 and daily.take(1) == 0 and 1 <= daily.take(1) <= 31
    clock.advance(31)  # the next UTC day
    assert daily.take(1) == 0


# spec: ANYM-475
def test_budget_refuses_before_any_provider_call():
    clock, state, governor = governed()
    client = Client(page([]))
    for _ in range(3):
        run(client, job(), governor)
    with pytest.raises(CommandRejected) as refused:
        run(client, job(), governor)
    assert refused.value.args[0] == 'rate_limited' and 1 <= refused.value.retry_after <= 10
    assert len(client.requests) == 3
    clock.advance(10)
    run(client, job(), governor)
    assert len(client.requests) == 4
    with pytest.raises(CommandRejected):  # a chat search counts its entity resolution too
        run(client, job(chat='-22'), governor)
    daily = LiveBudget(3, 10.0, 100, clock=clock, wall=clock.time)
    for _ in range(100):
        clock.advance(10); assert daily.take(1) == 0
    clock.advance(10)
    assert daily.take(1) > 0


# spec: ANYM-475
def test_search_flood_wait_pauses_only_search():
    clock, state, governor = governed()
    with pytest.raises(CommandRejected) as flooded:
        run(Client(error=errors.FloodWaitError(None, capture=300)), job(), governor)
    assert flooded.value.retry_after == 300 and state.deferred == []
    calls = []
    async def other(name):
        async def work():calls.append((name, clock.now))
        await governor.call(work, name)
    start = clock.now
    asyncio.run(other('provider_send')); asyncio.run(other('telegram_history'))
    assert [c[0] for c in calls] == ['provider_send', 'telegram_history'] and calls[-1][1] - start <= 2
    clock.advance(100)
    client = Client(page([]))
    with pytest.raises(CommandRejected) as paused:
        run(client, job(), governor)
    assert paused.value.retry_after >= 198 and client.requests == []


# spec: ANYM-475
def test_send_flood_during_search_pause_sets_the_account_cooldown():
    clock, state, governor = governed()
    with pytest.raises(CommandRejected):
        run(Client(error=errors.FloodWaitError(None, capture=60)), job(), governor)
    async def send():raise errors.FloodWaitError(None, capture=600)
    with pytest.raises(ProviderRateLimited):
        asyncio.run(governor.call(send, 'provider_send'))
    assert state.deferred == [600]
    clock.advance(120)  # the search pause ended, the account pause did not
    client = Client(page([]))
    with pytest.raises(CommandRejected) as paused:
        run(client, job(), governor)
    assert paused.value.retry_after >= 470 and client.requests == []


# spec: ANYM-475
@pytest.mark.parametrize('error,seconds', [
    (errors.FloodWaitError(None, capture=7200), 7200),
    (errors.FloodPremiumWaitError(None, capture=30), 30),
    (errors.FloodError(None, 'FLOOD_SYNTHETIC_UNKNOWN'), 60),
], ids=['long', 'premium', 'unknown'])
def test_long_or_other_flood_sets_the_account_cooldown(error, seconds):
    clock, state, governor = governed()
    with pytest.raises(CommandRejected):
        run(Client(error=error), job(), governor)
    assert state.deferred == [seconds]


# spec: ANYM-475
def test_existing_account_cooldown_is_never_bypassed():
    clock, state, governor = governed()
    state.until = clock.now + 90
    client = Client(page([]))
    with pytest.raises(CommandRejected) as paused:
        run(client, job(), governor)
    assert paused.value.retry_after >= 90 and client.requests == []
    async def search():return await governor.scoped_call(lambda: client(None), 'provider_command', 'search')
    with pytest.raises(ProviderRateLimited):
        asyncio.run(search())
    assert client.requests == []


# spec: ANYM-475
def test_worker_bounds_a_live_search_and_reports_retry_after(monkeypatch):
    import messaging_service.commands as commands
    assert LIVE_SEARCH_TIMEOUT == 15
    finished = []
    class Worker(CommandWorker):
        def claim(self):return {'id': 'synthetic-job', 'mutation': False, 'operation': 'search_messages_live'}
        def preflight(self, job, *, verify_file=True):return job
        def finish_live(self, job, status, result, code, budget):finished.append((status, result, code))
    monkeypatch.setattr(commands, 'LIVE_SEARCH_TIMEOUT', 0.05); monkeypatch.setattr(commands, 'LIVE_FOREGROUND_RESERVE', 0)
    async def hangs(job):await asyncio.sleep(60)
    assert once(Worker(SimpleNamespace(), hangs))
    assert finished[-1] == ('failed', {'data': {'code': 'provider_timeout', 'retry': True}}, 'provider_timeout')
    async def limited(job):raise CommandRejected('rate_limited', retry_after=42)
    once(Worker(SimpleNamespace(), limited))
    assert finished[-1] == ('failed', {'data': {'code': 'rate_limited', 'retry_after': 42}}, 'rate_limited')


# spec: ANYM-475
def test_query_text_is_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    clock, state, governor = governed()
    run(Client(page([message(types.PeerChat(22), 1, 'synthetic')])), job(), governor)
    for error in (errors.FloodWaitError(None, capture=5), errors.RPCError(None, 'SYNTHETIC_ERROR'),
                  errors.ChannelPrivateError(None)):
        with pytest.raises(CommandRejected):
            run(Client(error=error), job(), RequestGovernor(State(clock), clock=clock, sleep=clock.sleep))
    assert 'live_search method=search_global state=done returned=1' in caplog.text
    assert SENTINEL not in caplog.text


# spec: ANYM-475
def test_operation_uses_the_one_existing_client(monkeypatch, tmp_path):
    import telethon
    from messaging_service import telegram_collector
    created = []
    class FakeTelegramClient(Client):
        def __init__(self, *args, **kwargs):
            super().__init__(page([])); created.append(self)
    monkeypatch.setattr(telethon, 'TelegramClient', FakeTelegramClient)
    provider = telegram_collector.TelethonProvider({'session_file': str(tmp_path / 's.session')},
        {'api_id': 1, 'api_hash': '0' * 32, 'expected_user_id': 1}, receive_updates=False)
    clock = Clock(); provider.bind_state(State(clock))
    provider.governor.clock, provider.governor.sleep = clock, clock.sleep
    for _ in range(2):
        asyncio.run(provider.execute_operation(job()))
    assert len(created) == 1 and len(created[0].requests) == 2
