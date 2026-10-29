"""ANYM-643: preview_message and send_message to a Telegram @username (synthetic only; nothing reaches a provider)."""
import asyncio
import time

import pytest
from hypothesis import given, strategies as st
from telethon import errors, functions, types

from messaging_service import username_send
from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState
from messaging_service.commands import CommandWorker
from messaging_service.envelope import stable_id
from messaging_service.messenger_operations import operation_manifest, telegram_username
from messaging_service.store import APIError
from messaging_service.telegram_operations import execute
from messaging_service.telegram_governor import RequestGovernor

ACCOUNT = 'tg-username-send'
USERNAME = 'synthetic_person'
USER = '777000123'
OTHER_USER = '777000456'
TEXT = 'Synthetic first message'
CHAT = stable_id('chat', ACCOUNT, USER)


@pytest.fixture
def telegram(seeded):
    s, *_ = seeded
    archive = Archive(s)
    archive.register(ACCOUNT, 'telegram', 'Synthetic Telegram', discover=True)
    epoch = archive.acquire(ACCOUNT, 'send-test')
    state = CollectorState(archive, ACCOUNT, ('send-test', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    s.set_messenger_operations(ACCOUNT, ['open_chat_by_username'])
    archive.set_sending(ACCOUNT, True)
    key = s.issue_key('username-send-agent', [ACCOUNT], permissions=['read', 'send'])
    yield s, archive, state, key
    archive.release(ACCOUNT, 'send-test', state.lease[1])


def answer(user=USER, name='Synthetic Person'):
    return {'outcome': 'confirmed', 'data': {'found': True, 'user_id': user, 'username': USERNAME, 'name': name,
                                             'provider_lookup': True}}


def dispatch(state, jobs, result):
    async def run(job):
        jobs.append(job)
        out = result(job) if callable(result) else result
        if isinstance(out, Exception): raise out
        return out
    return asyncio.run(CommandWorker(state, run).once())


def draft(**changes):
    return {'account': ACCOUNT, 'username': USERNAME, 'text': TEXT, **changes}


def preview(s, key, state, jobs, result=None, **changes):
    arguments = draft(**changes)
    operation_id = s.username_preview_lookup(key, **arguments)
    dispatch(state, jobs, answer() if result is None else result)
    return s.username_preview_result(key, operation_id=operation_id, **arguments)


def send(s, key, state, jobs, digest, request_id='username-send-0001', result=None, **changes):
    arguments = {**draft(**changes), 'request_id': request_id, 'preview_digest': digest}
    step = s.username_send_begin(key, **arguments)
    if 'sent' in step: return step['sent']
    dispatch(state, jobs, answer() if result is None else result)
    return s.username_send_finish(key, operation_id=step['operation_id'], **arguments)


def count(s, table, where='true'):
    with s.pool.connection() as c:
        return c.execute(f'SELECT count(*) AS n FROM {table} WHERE {where}').fetchone()['n']


# spec: ANYM-643
@given(st.from_regex(r'[A-Za-z][A-Za-z0-9_]{3,31}', fullmatch=True), st.sampled_from(['', '@', 'https://t.me/']))
def test_a_username_normalizes_to_one_lowercase_name_in_every_accepted_form(name, prefix):
    normal = telegram_username(prefix + name)
    assert normal == name.lower()
    assert telegram_username(normal) == normal == telegram_username('@' + name.upper())


# spec: ANYM-643
@pytest.mark.parametrize('bad', ['abc', '1abcd', '@a' + 'b' * 32, 'name with space', 'http://t.me/abcd',
                                 'https://t.me/+abcdefgh', '@abcd?x', ''])
def test_anything_else_is_not_a_username(bad):
    assert telegram_username(bad) is None


# spec: ANYM-643
def test_username_excludes_chat_id_and_phone_and_is_telegram_only():
    assert username_send.recipient('', '', '@Synthetic_Person') == 'synthetic_person'
    assert username_send.recipient('chat_x', '', '') is None
    for recipient in (('chat_x', '', '@abcd'), ('', '+12025550161', '@abcd')):
        with pytest.raises(APIError, match='invalid_arguments'): username_send.recipient(*recipient)
    with pytest.raises(APIError, match='invalid_username'): username_send.recipient('', '', '@ab')
    assert {r['operation']: r['supported'] for r in operation_manifest('whatsapp', provider='whatsmeow')}[
        'open_chat_by_username'] is False
    assert {r['operation']: r['supported'] for r in operation_manifest('telegram')}['open_chat_by_username'] is True


# spec: ANYM-643
def test_preview_resolves_once_binds_the_user_and_writes_no_chat(telegram):
    s, archive, state, key = telegram
    jobs = []
    result = preview(s, key, state, jobs)
    assert result['username'] == '@' + USERNAME and result['user_id'] == USER and result['name'] == 'Synthetic Person'
    assert result['chat_existed'] is False and result['chat_id'] is None and result['preview_contacts_provider'] is True
    assert len(jobs) == 1 and jobs[0]['parameters'] == {'username': USERNAME, 'create': False}
    assert count(s, 'chats', f"id='{CHAT}'") == 0
    # A repeat within the validity reuses the lookup: no second provider request.
    again = s.username_preview_lookup(key, **draft())
    assert again == result['lookup_operation_id'] and count(s, 'messenger_commands') == 1


# spec: ANYM-643
def test_send_rechecks_the_user_opens_the_chat_and_queues_once(telegram):
    s, archive, state, key = telegram
    jobs = []
    digest = preview(s, key, state, jobs)['preview_digest']
    with pytest.raises(APIError, match='preview_required'):
        s.username_send_begin(key, **draft(), request_id='username-send-0000', preview_digest='')
    sent = send(s, key, state, jobs, digest)
    assert sent['chat_id'] == CHAT and sent['chat_existed'] is False and sent['user_id'] == USER
    assert jobs[-1]['parameters'] == {'username': USERNAME, 'create': True} and jobs[-1]['expected_user_id'] == USER
    with s.pool.connection() as c:
        chat = c.execute('SELECT title,chat_type,provider_chat FROM chats WHERE id=%s', (CHAT,)).fetchone()
        row = c.execute('SELECT chat,provider_chat FROM outgoing_messages').fetchone()
    assert chat == {'title': 'Synthetic Person', 'chat_type': 'private', 'provider_chat': USER}
    assert row == {'chat': CHAT, 'provider_chat': USER}
    # The same request_id replays the first result: no lookup, no second send.
    replay = send(s, key, state, jobs, digest)
    assert replay['send_id'] == sent['send_id'] and len(jobs) == 2 and count(s, 'outgoing_messages') == 1
    with pytest.raises(APIError, match='idempotency_conflict'):
        send(s, key, state, jobs, digest, text='Another synthetic text')


# spec: ANYM-643
def test_a_username_that_moved_to_another_user_sends_nothing(telegram):
    s, archive, state, key = telegram
    jobs = []
    digest = preview(s, key, state, jobs)['preview_digest']
    with pytest.raises(APIError, match='identity_changed') as caught:
        send(s, key, state, jobs, digest, result=answer(user=OTHER_USER))
    assert caught.value.next_action == 'preview_message_again'
    assert count(s, 'chats', "account='%s'" % ACCOUNT) == 0 and count(s, 'outgoing_messages') == 0


# spec: ANYM-643
def test_guards_refuse_before_any_lookup(telegram):
    s, archive, state, key = telegram
    jobs = []
    reader = s.issue_key('username-reader', [ACCOUNT], permissions=['read'])
    with pytest.raises(APIError, match='forbidden'): s.username_preview_lookup(reader, **draft())
    s.set_messenger_operations(ACCOUNT, [])
    with pytest.raises(APIError, match='operation_disabled'): s.username_preview_lookup(key, **draft())
    s.set_messenger_operations(ACCOUNT, ['open_chat_by_username'])
    with s.pool.connection() as c: c.execute('UPDATE account_policies SET discover_chats=false WHERE account=%s', (ACCOUNT,))
    with pytest.raises(APIError, match='chat_not_collected'): s.username_preview_lookup(key, **draft())
    assert count(s, 'messenger_commands') == 0 and jobs == []


# spec: ANYM-643
def test_an_excluded_user_is_refused_after_resolution_with_no_chat(telegram):
    s, archive, state, key = telegram
    jobs = []
    with s.pool.connection() as c:
        c.execute('UPDATE account_policies SET exclude_chats=%s WHERE account=%s', ([USER], ACCOUNT))
    with pytest.raises(APIError, match='chat_not_collected'): preview(s, key, state, jobs)
    assert count(s, 'chats', "account='%s'" % ACCOUNT) == 0


# spec: ANYM-643
def test_twenty_lookups_an_hour_then_rate_limited_without_a_request(telegram):
    s, archive, state, key = telegram
    jobs = []
    for index in range(20):
        name = f'synthetic_{index:02d}'
        s.username_preview_lookup(key, **draft(username=name))
        dispatch(state, jobs, lambda job: {**answer(), 'data': {**answer()['data'], 'username': job['parameters']['username']}}
                 if job.get('lookup_allowed') else pytest.fail('lookup over budget'))
    s.username_preview_lookup(key, **draft(username='synthetic_last'))
    seen = []
    def provider(job):
        seen.append(job['lookup_allowed'])
        from messaging_service.commands import CommandRejected
        raise CommandRejected('rate_limited')
    dispatch(state, jobs, provider)
    assert seen == [False]
    with s.pool.connection() as c:
        row = c.execute("SELECT status,error_code,result FROM messenger_commands WHERE payload->'parameters'->>'username'"
                        "='synthetic_last'").fetchone()
    assert row['error_code'] == 'rate_limited' and row['result']['data']['provider_lookup'] is False


class Client:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    async def __call__(self, request):
        self.calls.append(request)
        if self.error: raise self.error
        return self.response


def job(username=USERNAME, **changes):
    return {'operation': 'open_chat_by_username', 'parameters': {'username': username, 'create': False},
            'expires_at': time.time() + 60, 'lookup_allowed': True,
            '_policy': {'enabled': True, 'discover_chats': True, 'include_chats': [], 'exclude_chats': []}, **changes}


def resolved(user):
    return types.contacts.ResolvedPeer(peer=types.PeerUser(user.id), chats=[], users=[user])


# spec: ANYM-643
def test_the_collector_resolves_one_person_and_refuses_bots_channels_and_unknown_names():
    client = Client(resolved(types.User(id=int(USER), access_hash=1, first_name='Synthetic', last_name='Person')))
    out = asyncio.run(execute(client, job()))
    assert out == {'outcome': 'confirmed', 'data': {'found': True, 'user_id': USER, 'username': USERNAME,
                                                    'name': 'Synthetic Person', 'provider_lookup': True}}
    assert len(client.calls) == 1 and isinstance(client.calls[0], functions.contacts.ResolveUsernameRequest)
    cases = [(Client(resolved(types.User(id=5, access_hash=1, bot=True))), 'recipient_not_user'),
             (Client(types.contacts.ResolvedPeer(peer=types.PeerChannel(9), chats=[], users=[])), 'recipient_not_user'),
             (Client(resolved(types.User(id=5, access_hash=1, deleted=True))), 'username_not_found'),
             (Client(error=errors.UsernameNotOccupiedError(None)), 'username_not_found')]
    for candidate, code in cases:
        with pytest.raises(Exception, match=code): asyncio.run(execute(candidate, job()))
    for refused, code in ((job(lookup_allowed=False), 'rate_limited'),
                          (job(username='Bad Name'), 'invalid_parameters'),
                          (job(_policy={'enabled': True, 'discover_chats': False, 'include_chats': [],
                                        'exclude_chats': []}), 'chat_not_collected')):
        idle = Client()
        with pytest.raises(Exception, match=code): asyncio.run(execute(idle, refused))
        assert idle.calls == []


# spec: ANYM-643
def test_a_flood_wait_on_the_resolve_pauses_only_its_scope_never_sends():
    class State:
        def cooldown_remaining(self): return 0
        def status(self, *args): pass
        def defer(self, seconds): raise AssertionError('account cooldown recorded')
    governor = RequestGovernor(State(), interval=0)
    flood = errors.FloodWaitError(None, capture=60)
    with pytest.raises(Exception, match='rate_limited') as caught:
        asyncio.run(execute(Client(error=flood), job(), governor=governor))
    assert caught.value.retry_after == 60
    assert governor.scoped_remaining('resolve_username') > 0 and governor.scoped_remaining('provider_send') == 0
