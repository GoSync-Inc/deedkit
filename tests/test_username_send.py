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


def refused(call, code):
    with pytest.raises(APIError, match=code) as caught: call()
    return caught.value


def rows(s, sql, args=()):
    with s.pool.connection() as c: return c.execute(sql, args).fetchall()


def shift(s, operation_id, created, finished):
    """Move a lookup back in time by seconds (synthetic clock)."""
    with s.pool.connection() as c:
        c.execute("""UPDATE messenger_commands SET created_at=clock_timestamp()-%s*interval '1 second',
            finished_at=clock_timestamp()-%s*interval '1 second' WHERE id=%s""", (created, finished, operation_id))


# spec: ANYM-643
def test_admission_needs_read_send_full_account_a_ready_collector_and_telegram(telegram):
    s, archive, state, key = telegram
    send_only = s.issue_key('username-sender', [ACCOUNT], permissions=['send'])
    refused(lambda: s.username_preview_lookup(send_only, **draft()), 'forbidden')
    other_chat = stable_id('chat', ACCOUNT, OTHER_USER)
    with s.pool.connection() as c:
        c.execute("INSERT INTO chats(id,account,title,chat_type,provider_chat) VALUES(%s,%s,'Synthetic','private',%s)",
                  (other_chat, ACCOUNT, OTHER_USER))
    scoped = s.issue_key('username-scoped', [ACCOUNT], chats=[other_chat], permissions=['read', 'send'])
    refused(lambda: s.username_preview_lookup(scoped, **draft()), 'forbidden')
    other = s.issue_key('username-wa', ['whatsapp-demo-1'], permissions=['read', 'send'])
    wrong = refused(lambda: s.username_preview_lookup(other, **draft(account='whatsapp-demo-1')), 'unsupported_operation')
    assert wrong.hint == 'telegram_accounts_only'
    state.phase('connection', 'stopped', 'collector_shutdown')
    refused(lambda: s.username_preview_lookup(key, **draft()), 'sender_not_ready')
    state.phase('connection', 'connected', 'provider_ready')
    archive.set_sending(ACCOUNT, False)
    refused(lambda: s.username_send_begin(key, **draft(), request_id='username-off-0001', preview_digest='d' * 64),
            'sending_disabled')
    assert count(s, 'messenger_commands') == 0 and count(s, 'outgoing_messages') == 0


# spec: ANYM-643
def test_a_post_resolution_refusal_keeps_its_receipt_and_writes_no_chat(telegram):
    from messaging_service.commands import CommandRejected
    s, archive, state, key = telegram
    jobs = []
    refused(lambda: preview(s, key, state, jobs, result=CommandRejected('recipient_not_user')), 'recipient_not_user')
    assert [r['error_code'] for r in rows(s, 'SELECT error_code FROM messenger_commands')] == ['recipient_not_user']
    assert count(s, 'chats', "account='%s'" % ACCOUNT) == 0 and count(s, 'outgoing_messages') == 0


# spec: ANYM-643
def test_an_archived_private_chat_is_reused_not_duplicated(telegram):
    s, archive, state, key = telegram
    jobs = []
    with s.pool.connection() as c:
        c.execute("INSERT INTO chats(id,account,title,chat_type,provider_chat) VALUES(%s,%s,'Synthetic','private',%s)",
                  (CHAT, ACCOUNT, USER))
    previewed = preview(s, key, state, jobs)
    assert previewed['chat_existed'] is True and previewed['chat_id'] == CHAT
    sent = send(s, key, state, jobs, previewed['preview_digest'])
    assert sent['chat_id'] == CHAT and sent['chat_existed'] is True and count(s, 'chats', "account='%s'" % ACCOUNT) == 1


# spec: ANYM-643
def test_the_digest_binds_text_username_and_lookup_and_a_later_lookup_never_revives_it(telegram):
    s, archive, state, key = telegram
    jobs = []
    pending = s.username_preview_lookup(key, **draft())
    waiting = refused(lambda: s.username_preview_result(key, operation_id=pending, **draft()), 'username_lookup_pending')
    assert waiting.operation_id == pending and waiting.next_action == 'repeat_preview_message'
    dispatch(state, jobs, answer())
    digest = s.username_preview_result(key, operation_id=pending, **draft())['preview_digest']
    changed = refused(lambda: send(s, key, state, jobs, digest, text='Another synthetic text'), 'preview_mismatch')
    assert changed.next_action == 'preview_message_again'
    refused(lambda: send(s, key, state, jobs, digest, username='synthetic_other'), 'preview_expired')
    shift(s, pending, 700, 601)
    refused(lambda: send(s, key, state, jobs, digest), 'preview_expired')
    preview(s, key, state, jobs)  # A fresh lookup of the same user: the old digest names the old lookup.
    refused(lambda: send(s, key, state, jobs, digest, request_id='username-send-0002'), 'preview_mismatch')
    assert count(s, 'outgoing_messages') == 0


# spec: ANYM-643
def test_a_preview_stays_valid_until_the_expiry_it_returned(telegram):
    s, archive, state, key = telegram
    jobs = []
    previewed = preview(s, key, state, jobs)
    # The lookup was queued 620 s ago and finished 590 s ago: inside the 10 minutes after it finished.
    shift(s, previewed['lookup_operation_id'], 620, 590)
    assert send(s, key, state, jobs, previewed['preview_digest'])['chat_id'] == CHAT


# spec: ANYM-643
@pytest.mark.parametrize('revoke', ['operation', 'sending'])
def test_a_guard_revoked_after_the_chat_step_queues_no_message(telegram, revoke):
    s, archive, state, key = telegram
    jobs = []
    digest = preview(s, key, state, jobs)['preview_digest']
    arguments = {**draft(), 'request_id': 'username-revoke-1', 'preview_digest': digest}
    step = s.username_send_begin(key, **arguments)
    dispatch(state, jobs, answer())
    if revoke == 'operation': s.set_messenger_operations(ACCOUNT, [])
    else: archive.set_sending(ACCOUNT, False)
    refused(lambda: s.username_send_finish(key, operation_id=step['operation_id'], **arguments),
            'operation_disabled' if revoke == 'operation' else 'sending_disabled')
    assert count(s, 'outgoing_messages') == 0


# spec: ANYM-643
def test_one_request_id_is_one_chat_step_also_concurrently_and_conflicts_with_a_plain_send(telegram):
    import threading
    s, archive, state, key = telegram
    jobs = []
    digest = preview(s, key, state, jobs)['preview_digest']
    arguments = {**draft(), 'request_id': 'username-once-0001', 'preview_digest': digest}
    steps, barrier = [], threading.Barrier(2)
    def begin():
        barrier.wait(); steps.append(s.username_send_begin(key, **arguments)['operation_id'])
    threads = [threading.Thread(target=begin) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(set(steps)) == 1 and count(s, 'messenger_commands', "payload ? 'expected_user_id'") == 1
    dispatch(state, jobs, answer())
    assert s.username_send_begin(key, **arguments) == {'operation_id': steps[0]}  # A repeat after the chat step.
    refused(lambda: s.send_message(key, ACCOUNT, CHAT, TEXT, 'username-once-0001'), 'idempotency_conflict')
    sent = s.username_send_finish(key, operation_id=steps[0], **arguments)
    assert s.username_send_finish(key, operation_id=steps[0], **arguments)['send_id'] == sent['send_id']
    assert count(s, 'outgoing_messages') == 1 and len(jobs) == 2


# spec: ANYM-643
def test_an_uncertain_chat_step_is_never_dispatched_again(telegram):
    s, archive, state, key = telegram
    jobs = []
    digest = preview(s, key, state, jobs)['preview_digest']
    arguments = {**draft(), 'request_id': 'username-unsure-1', 'preview_digest': digest}
    step = s.username_send_begin(key, **arguments)
    dispatch(state, jobs, RuntimeError('synthetic lost answer'))
    refused(lambda: s.username_send_finish(key, operation_id=step['operation_id'], **arguments), 'operation_result_unknown')
    assert s.username_send_begin(key, **arguments) == step
    assert not dispatch(state, jobs, answer()) and len(jobs) == 2 and count(s, 'outgoing_messages') == 0


class Telegram:
    """A fake Telethon client: every username resolves to a synthetic person; it counts requests."""
    def __init__(self, error=None):
        self.calls, self.error = [], error

    async def __call__(self, request):
        self.calls.append(request)
        if self.error: raise self.error
        return resolved(types.User(id=int(USER), access_hash=1, first_name='Synthetic'))


def collector(state, client, governor=None):
    async def run(job): return await execute(client, job, governor=governor)
    return asyncio.run(CommandWorker(state, run).once())


# spec: ANYM-643
def test_the_quota_is_durable_across_workers_and_spends_nothing_when_refused(telegram):
    s, archive, state, key = telegram
    client = Telegram()
    for index in range(20):
        s.username_preview_lookup(key, **draft(username=f'synthetic_{index:02d}'))
        collector(state, client)  # A new worker each time, as after a restart.
    s.username_preview_lookup(key, **draft(username='synthetic_last'))
    collector(state, client)
    assert len(client.calls) == 20
    last = rows(s, "SELECT error_code,result FROM messenger_commands WHERE payload->'parameters'->>'username'=%s",
                ('synthetic_last',))[0]
    assert last['error_code'] == 'rate_limited' and last['result']['data']['provider_lookup'] is False
    # A finished lookup leaves the window an hour after it finished.
    with s.pool.connection() as c:
        c.execute("""UPDATE messenger_commands SET finished_at=clock_timestamp()-interval '61 minutes'
            WHERE id=(SELECT id FROM messenger_commands WHERE status='succeeded' ORDER BY created_at LIMIT 1)""")
    s.username_preview_lookup(key, **draft(username='synthetic_next'))
    collector(state, client)
    assert len(client.calls) == 21


# spec: ANYM-643
def test_a_paused_scope_refuses_without_asking_telegram_or_spending_a_lookup(telegram):
    s, archive, state, key = telegram
    client, governor = Telegram(), RequestGovernor(state, interval=0)
    governor.scoped['resolve_username'] = governor.clock() + 60
    s.username_preview_lookup(key, **draft())
    collector(state, client, governor)
    row = rows(s, 'SELECT error_code,result FROM messenger_commands')[0]
    assert client.calls == [] and row['error_code'] == 'rate_limited'
    data = row['result']['data']
    assert data['code'] == 'rate_limited' and data['provider_lookup'] is False and 59 <= data['retry_after'] <= 61


# spec: ANYM-643
def test_a_failed_or_flooded_lookup_leaves_ordinary_sends_dispatching(telegram, caplog):
    from messaging_service.outgoing import OutgoingWorker
    s, archive, state, key = telegram
    governor = RequestGovernor(state, interval=0)
    other = stable_id('chat', ACCOUNT, OTHER_USER)
    with s.pool.connection() as c:
        c.execute("INSERT INTO chats(id,account,title,chat_type,provider_chat) VALUES(%s,%s,'Synthetic','private',%s)",
                  (other, ACCOUNT, OTHER_USER))
    for name, error in (('synthetic_stall', TimeoutError('synthetic stall')),
                        ('synthetic_flood', errors.FloodWaitError(None, capture=60))):
        s.username_preview_lookup(key, **draft(username=name))
        collector(state, Telegram(error), governor)
    assert [r['status'] for r in rows(s, 'SELECT status FROM messenger_commands ORDER BY created_at')] == ['failed'] * 2
    assert asyncio.run(governor.account_remaining()) == 0
    sent = []
    async def provider(job):
        sent.append(job); return {'provider_message_id': '1'}
    queued = s.send_message(key, ACCOUNT, other, TEXT, 'plain-after-lookup')
    asyncio.run(OutgoingWorker(state, provider).once())
    assert [j['provider_chat'] for j in sent] == [OTHER_USER] and queued['send_id']
    assert USERNAME not in caplog.text and USER not in caplog.text


# spec: ANYM-643
def test_messages_send_opens_a_username_under_the_gateway_grant_and_keeps_the_declared_fields(telegram, caplog):
    from messaging_service.outgoing import OutgoingWorker
    from messaging_service.send_intent import send as messages_send
    s, archive, state, key = telegram
    jobs, texts = [], []
    async def adapter(job):
        jobs.append(job); return answer()
    async def provider(job):
        texts.append(job); return {'provider_message_id': 'tg-1'}
    async def with_workers(arguments):
        task = asyncio.create_task(messages_send(s, key, arguments))
        while not task.done():
            await CommandWorker(state, adapter).once()
            await OutgoingWorker(state, provider).once()
            await asyncio.sleep(0.05)
        return await task
    target = s.resolve_target(key, '@' + USERNAME)['target']
    assert target == {'account': ACCOUNT, 'chat_id': None, 'username': '@' + USERNAME, 'network': 'telegram',
                      'label': 'Telegram @' + USERNAME, 'how_resolved': 'username_opens_chat', 'opens_chat': True}
    base = {'to': '@' + USERNAME, 'text': TEXT, 'dry_run': True}
    # An older gateway sets allow_open_chat only: no username opens.
    refused(lambda: asyncio.run(with_workers({**base, 'allow_open_chat': True})), 'forbidden')
    assert jobs == []
    previewed = asyncio.run(with_workers({**base, 'allow_open_chat': True, 'allow_open_username': True}))
    recipient = previewed['preview']['recipient']
    assert recipient['username'] == '@' + USERNAME and recipient['user_id'] == USER
    assert recipient['name'] == 'Synthetic Person' and previewed['preview']['steps'] == ['username']
    token = previewed['confirm_token']
    for grants in ({'allow_open_username': False}, {'allow_open_chat': True}):
        refused(lambda: asyncio.run(with_workers({'confirm_token': token, **grants})), 'forbidden')
    result = asyncio.run(with_workers({'confirm_token': token, 'wait_s': 10, 'allow_open_username': True}))
    assert result['state'] == 'succeeded' and [t['provider_chat'] for t in texts] == [USER]
    plan = rows(s, 'SELECT plan FROM send_intents')[0]['plan']
    assert {k: plan['resolution']['target'][k] for k in ('username', 'user_id', 'name')} == {
        'username': '@' + USERNAME, 'user_id': USER, 'name': 'Synthetic Person'}
    assert TEXT not in str(plan) and USERNAME not in caplog.text
