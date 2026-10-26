"""ANYM-532: preview_message and send_message to a WhatsApp phone (synthetic only; nothing reaches a provider)."""
import asyncio
import json
import logging
import threading
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from messaging_service import phone_send
from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.commands import CommandRejected, CommandWorker
from messaging_service.envelope import stable_id
from messaging_service.error_details import payload as error_payload
from messaging_service.store import APIError
from messaging_service.whatsapp_collector import NodeProvider, WhatsAppState
from test_open_chat_by_phone import Writer
from test_whatsapp import capture, event

ACCOUNT = 'wa-phone-send'
OTHER = 'wa-phone-send-other'
PHONE = '12025550161'  # NANP fictional 555-01xx range
PN = PHONE+'@c.us'
LID = '987654321012361@lid'
TEXT = 'Synthetic first message'
CHAT = stable_id('chat', ACCOUNT, LID)


def connect(s, archive, account):
    archive.register(account, 'whatsapp', 'Synthetic native', discover=True)
    epoch = archive.acquire(account, 'send-test')
    state = WhatsAppState(archive, account, ('send-test', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    state.provider_observation('whatsmeow')
    s.set_messenger_operations(account, ['open_chat_by_phone'])
    archive.set_sending(account, True)
    return state


@pytest.fixture
def native(seeded):
    s, *_ = seeded
    archive = Archive(s)
    state = connect(s, archive, ACCOUNT)
    key = s.issue_key('phone-send-agent', [ACCOUNT], permissions=['read', 'send'])
    yield s, archive, state, key
    archive.release(ACCOUNT, 'send-test', state.lease[1])


def answer(on=True, lookup=True, name=None, lid=LID):
    data = {'on_whatsapp': on, 'verified_name': name, 'provider_lookup': lookup}
    if on: data['provider_chat'] = lid
    return {'outcome': 'confirmed', 'data': data}


def dispatch(state, jobs, result):
    """One command to a fake adapter answering like operations.go; False when nothing was queued."""
    async def execute(job):
        jobs.append(job)
        out = result(job) if callable(result) else result
        if isinstance(out, Exception): raise out
        return out
    return asyncio.run(CommandWorker(state, execute).once())


def draft(**changes):
    return {'account': ACCOUNT, 'phone': PHONE, 'text': TEXT, **changes}


def preview(s, key, state, jobs, result=answer(name='Synthetic Bakery'), **changes):
    arguments = draft(**changes)
    operation_id = s.phone_preview_lookup(key, **arguments)
    dispatch(state, jobs, result)
    return s.phone_preview_result(key, operation_id=operation_id, **arguments)


def local(job):
    """The send's chat step: the session store still maps the number, so the adapter answers locally,
    and like operations.go's local path with no verified name."""
    assert job['expected_lid'] == LID and job['parameters'] == {'phone': PHONE, 'create': True}
    return answer(lookup=False)


def send(s, key, state, jobs, digest, request_id='phone-send-0001', result=local, **changes):
    arguments = {**draft(**changes), 'request_id': request_id, 'preview_digest': digest}
    step = s.phone_send_begin(key, **arguments)
    if 'sent' in step: return step['sent']
    if result is not None: dispatch(state, jobs, result)
    return s.phone_send_finish(key, operation_id=step['operation_id'], **arguments)


def count(s, table, where='true'):
    with s.pool.connection() as c:
        return c.execute(f'SELECT count(*) AS n FROM {table} WHERE {where}').fetchone()['n']


def lookups(s):
    return count(s, 'messenger_commands', "result->'data'->>'provider_lookup'='true'")


def refused(call, code):
    with pytest.raises(APIError, match=code) as caught:
        call()
    return error_payload(caught.value)


def rest(s, key, monkeypatch, state, jobs, result=None):
    """The app's own async path; the wait dispatches the queued lookup instead of polling."""
    async def wait(store, token, operation_id):
        if result is not None: await asyncio.to_thread(dispatch, state, jobs, result)
    monkeypatch.setattr(phone_send, '_wait', wait)
    client = TestClient(create_app(s, ['testserver']))
    auth = {'Authorization': 'Bearer '+key}
    return client, lambda tool, body: client.post('/v1/'+tool, json=body, headers=auth)


# spec: ANYM-532
@pytest.mark.parametrize('phone', ['+'+PHONE, PHONE, PN, PHONE+'@s.whatsapp.net'])
def test_a_phone_is_e164_or_a_whatsapp_jid_and_normalizes_to_digits(phone):
    assert phone_send.recipient('', phone) == PHONE
    assert phone_send.recipient('chat_'+'a'*32, '') is None


# spec: ANYM-532
def test_phone_and_chat_id_are_exclusive_and_checked_before_any_lookup(native, monkeypatch):
    s, archive, state, key = native
    jobs = []
    client, post = rest(s, key, monkeypatch, state, jobs, answer())
    with client:
        for tool in ('preview_message', 'send_message'):
            extra = {'request_id': 'phone-args-0001'} if tool == 'send_message' else {}
            for recipient in ({'chat_id': CHAT, 'phone': '+'+PHONE}, {}):
                response = post(tool, {'account': ACCOUNT, 'text': TEXT, **recipient, **extra})
                assert response.json()['error'] == 'invalid_arguments', (tool, recipient)
            for bad in ('+1202555', '+1202555016100001', '0'+PHONE, PHONE+'@lid', PHONE+'@g.us', 'synthetic'):
                assert post(tool, {'account': ACCOUNT, 'text': TEXT, 'phone': bad, **extra}).json()['error'] == 'invalid_phone'
    assert jobs == [] and count(s, 'messenger_commands') == 0


# spec: ANYM-532
@pytest.mark.parametrize('account', ['telegram-demo', 'whatsapp-demo-1'])
def test_other_adapters_get_the_supported_kind_before_any_lookup(seeded, account):
    s, *_ = seeded
    key = s.issue_key('phone-send-adapters', ['telegram-demo', 'whatsapp-demo-1'], permissions=['read', 'send'])
    for call in (lambda: s.phone_preview_lookup(key, account, PHONE, TEXT),
                 lambda: s.phone_send_begin(key, account, PHONE, TEXT, 'phone-other-0001', preview_digest='a'*64)):
        error = refused(call, 'unsupported_operation')
        assert error['hint'] == 'native_whatsapp_accounts_only'
        assert error['next_action'] == 'use_username_or_existing_chat'
    assert count(s, 'messenger_commands') == 0


# spec: ANYM-532
def test_each_missing_guard_refuses_with_zero_requests_and_writes(native):
    s, archive, state, key = native
    reader = s.issue_key('phone-send-reader', [ACCOUNT], permissions=['read'])
    refused(lambda: s.phone_preview_lookup(reader, **draft()), 'forbidden')
    archive_chat(state, PN, 'PN1')
    s.set_access('phone-send-agent', chats=[stable_id('chat', ACCOUNT, PN)])
    refused(lambda: s.phone_preview_lookup(key, **draft()), 'forbidden')
    refused(lambda: s.phone_send_begin(key, **draft(), request_id='phone-guard-0001', preview_digest='a'*64), 'forbidden')
    s.set_access('phone-send-agent', chats=None)
    s.set_messenger_operations(ACCOUNT, [])
    refused(lambda: s.phone_preview_lookup(key, **draft()), 'operation_disabled')
    s.set_messenger_operations(ACCOUNT, ['open_chat_by_phone'])
    archive.set_sending(ACCOUNT, False)
    refused(lambda: s.phone_send_begin(key, **draft(), request_id='phone-guard-0002', preview_digest='a'*64),
            'sending_disabled')
    assert count(s, 'messenger_commands') == 0 and count(s, 'chats', f"id='{CHAT}'") == 0
    # chat_id previews keep today's permissions: read only, no provider.
    assert s.preview_message(reader, account=ACCOUNT, chat_id=stable_id('chat', ACCOUNT, PN), text=TEXT)['preview_digest']


def archive_chat(state, chat, provider_id):
    item = event(1); item['chatId'] = chat; item['senderId'] = chat; item['messageId'] = 'false_'+chat+'_'+provider_id
    state.apply(capture(item, account=state.account))


# spec: ANYM-532
def test_policy_is_rechecked_before_the_chat_is_created_and_before_the_send(native):
    s, archive, state, key = native
    jobs = []
    digest = preview(s, key, state, jobs)['preview_digest']
    s.set_messenger_operations(ACCOUNT, [])
    refused(lambda: send(s, key, state, jobs, digest), 'operation_disabled')
    s.set_messenger_operations(ACCOUNT, ['open_chat_by_phone'])
    # The chat step is queued, then the opt-in goes before it runs: no chat and no send.
    arguments = {**draft(), 'request_id': 'phone-policy-0001', 'preview_digest': digest}
    step = s.phone_send_begin(key, **arguments)
    s.set_messenger_operations(ACCOUNT, [])
    dispatch(state, jobs, local)
    refused(lambda: s.phone_send_finish(key, operation_id=step['operation_id'], **arguments), 'operation_disabled')
    assert count(s, 'chats', f"id='{CHAT}'") == 0 and count(s, 'outgoing_messages') == 0
    assert len(jobs) == 1  # only the preview's lookup reached the adapter


# spec: ANYM-532
def test_a_phone_preview_does_one_lookup_and_writes_no_chat(seeded):
    s, *_ = seeded
    archive = Archive(s)
    state, other = connect(s, archive, ACCOUNT), connect(s, archive, OTHER)
    key = s.issue_key('phone-send-two', [ACCOUNT, OTHER], permissions=['read', 'send'])
    archive_chat(other, PN, 'OTHER1')
    jobs = []
    try:
        result = preview(s, key, state, jobs)
        assert jobs[0]['parameters'] == {'phone': PHONE, 'create': False}
        assert result['on_whatsapp'] is True and result['verified_name'] == 'Synthetic Bakery'
        assert result['chat_existed'] is False and result['chat_id'] is None and result['phone'] == '+'+PHONE
        assert result['preview_contacts_provider'] is True and result['owner_approval_verified_by_server'] is False
        [found] = result['chats_on_other_accounts']
        assert found['account'] == OTHER and found['chat_id'] == stable_id('chat', OTHER, PN) and found['last_message_at']
        assert count(s, 'chats', f"account='{ACCOUNT}'") == 0 and LID not in json.dumps(result, default=str)
        # A second preview inside the digest's validity reuses the lookup: same digest, no new request.
        assert preview(s, key, state, jobs)['preview_digest'] == result['preview_digest'] and len(jobs) == 1
        assert preview(s, key, state, jobs, text='Other text')['preview_digest'] != result['preview_digest']
        assert lookups(s) == 1
    finally:
        archive.release(OTHER, 'send-test', other.lease[1])
        archive.release(ACCOUNT, 'send-test', state.lease[1])


# spec: ANYM-532
def test_not_on_whatsapp_names_the_normalized_phone(native):
    s, archive, state, key = native
    error = refused(lambda: preview(s, key, state, [], answer(on=False)), 'not_on_whatsapp')
    assert error['phone'] == '+'+PHONE and error['next_action'] == 'use_username_or_existing_chat'
    assert TEXT not in json.dumps(error)


# spec: ANYM-532
def test_the_send_reuses_the_preview_lookup_and_opens_one_canonical_chat(native):
    s, archive, state, key = native
    jobs = []
    digest = preview(s, key, state, jobs)['preview_digest']
    sent = send(s, key, state, jobs, digest)
    assert sent['status'] == 'queued' and sent['chat_id'] == CHAT and sent['chat_existed'] is False
    assert sent['verified_name'] == 'Synthetic Bakery' and sent['send_id']
    assert lookups(s) == 1 and count(s, 'chats', f"id='{CHAT}'") == 1
    with s.pool.connection() as c:
        row = c.execute('SELECT to_jsonb(o) AS row FROM outgoing_messages o').fetchone()['row']
    # The outgoing row keeps the resolved chat, never another copy of the number.
    assert row['chat'] == CHAT and row['provider_chat'] == LID and PHONE not in json.dumps(row)
    # A later send to the same number finds the chat that now exists.
    digest = preview(s, key, state, jobs, text='Second synthetic message')['preview_digest']
    again = send(s, key, state, jobs, digest, request_id='phone-send-0002', text='Second synthetic message')
    assert again['chat_id'] == CHAT and again['chat_existed'] is True and count(s, 'chats', f"id='{CHAT}'") == 1


# spec: ANYM-532
def test_lookups_share_one_quota_with_the_separate_operation(native):
    s, archive, state, key = native
    jobs = []
    for n in range(10):
        s.execute_messenger_operation(key, account=ACCOUNT, operation='open_chat_by_phone',
                                      parameters={'phone': '+1202555%04d' % (200+n), 'create': False},
                                      request_id='phone-quota-%04d' % n)
        dispatch(state, jobs, answer(lid='9876543210%05d@lid' % n))
    refused(lambda: preview(s, key, state, jobs, CommandRejected('rate_limited')), 'rate_limited')
    # The preview's lookup is refused by the separate operation's spent budget.
    assert len(jobs) == 11 and jobs[-1]['lookup_allowed'] is False


# spec: ANYM-532
def test_a_changed_draft_identity_or_expired_digest_asks_for_a_new_preview(native):
    s, archive, state, key = native
    jobs = []
    digest = preview(s, key, state, jobs)['preview_digest']
    for changed in ({'text': 'Changed text'}, {'format': 'markdown'}):
        error = refused(lambda: send(s, key, state, jobs, digest, request_id='phone-change-0001', **changed),
                        'preview_mismatch')
        assert error['next_action'] == 'preview_message_again'
    error = refused(lambda: send(s, key, state, jobs, digest, request_id='phone-change-0002',
                                 result=CommandRejected('identity_changed')), 'identity_changed')
    assert error['next_action'] == 'preview_message_again'
    digest = preview(s, key, state, jobs, text='Late text')['preview_digest']
    with s.pool.connection() as c:
        c.execute("UPDATE messenger_commands SET created_at=created_at-interval '11 minutes',"
                  "finished_at=finished_at-interval '11 minutes'")
    error = refused(lambda: send(s, key, state, jobs, digest, request_id='phone-change-0003', text='Late text'),
                    'preview_expired')
    assert error['next_action'] == 'preview_message_again'
    assert count(s, 'outgoing_messages') == 0 and lookups(s) == 1


# spec: ANYM-532
@pytest.mark.parametrize('failure', ['identity_unresolved', 'rate_limited'])
def test_a_failed_chat_step_sends_nothing(native, failure):
    s, archive, state, key = native
    jobs = []
    digest = preview(s, key, state, jobs)['preview_digest']
    refused(lambda: send(s, key, state, jobs, digest, result=CommandRejected(failure)), failure)
    assert count(s, 'outgoing_messages') == 0 and count(s, 'chats', f"id='{CHAT}'") == 0


# spec: ANYM-532
def test_a_repeat_returns_the_first_result_and_any_changed_field_conflicts(native):
    s, archive, state, key = native
    jobs = []
    digest = preview(s, key, state, jobs)['preview_digest']
    first = send(s, key, state, jobs, digest)
    assert send(s, key, state, jobs, digest, result=None) == first and len(jobs) == 2
    for changed in ({'text': 'Changed text'}, {'phone': '+12025550162'}, {'format': 'markdown'},
                    {'reply_to': 'msg_'+'a'*32}, {'preview_digest': 'b'*64}):
        arguments = {**draft(), 'request_id': 'phone-send-0001', 'preview_digest': digest, **changed}
        refused(lambda: s.phone_send_begin(key, **arguments), 'idempotency_conflict')
    # The same request_id as a chat_id send is a different request.
    refused(lambda: s.send_message(key, account=ACCOUNT, chat_id=CHAT, text=TEXT, request_id='phone-send-0001',
                                   preview_digest=digest), 'idempotency_conflict')
    assert count(s, 'outgoing_messages') == 1 and len(jobs) == 2


# spec: ANYM-532
def test_recovery_after_a_stop_at_each_step_sends_once(native):
    s, archive, state, key = native
    jobs = []
    digest = preview(s, key, state, jobs)['preview_digest']
    arguments = {**draft(), 'request_id': 'phone-stop-0001', 'preview_digest': digest}
    # Stopped before the lookup ran: the repeat waits for the same chat step.
    step = s.phone_send_begin(key, **arguments)
    pending = s.phone_send_finish(key, operation_id=step['operation_id'], **arguments)
    assert pending['status'] == 'opening_chat' and pending['next_action'] == 'repeat_send_message_with_same_request_id'
    assert s.phone_send_begin(key, **arguments) == step
    # Stopped after the chat commit: the repeat queues the send under the caller's request_id.
    dispatch(state, jobs, local)
    assert count(s, 'chats', f"id='{CHAT}'") == 1 and count(s, 'outgoing_messages') == 0
    assert s.phone_send_begin(key, **arguments) == step
    sent = s.phone_send_finish(key, operation_id=step['operation_id'], **arguments)
    # Stopped after the outbox commit: the repeat returns the first result.
    assert s.phone_send_begin(key, **arguments) == {'sent': sent}
    assert s.phone_send_finish(key, operation_id=step['operation_id'], **arguments) == sent
    assert count(s, 'outgoing_messages') == 1 and len(jobs) == 2 and not dispatch(state, jobs, local)


# spec: ANYM-532
def test_concurrent_same_id_calls_open_and_send_once(native):
    s, archive, state, key = native
    jobs = []
    digest = preview(s, key, state, jobs)['preview_digest']
    arguments = {**draft(), 'request_id': 'phone-race-0001', 'preview_digest': digest}
    steps, barrier = [], threading.Barrier(4)
    def begin():
        barrier.wait(); steps.append(s.phone_send_begin(key, **arguments))
    threads = [threading.Thread(target=begin) for _ in range(4)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len(steps) == 4 and all(step == steps[0] for step in steps)
    dispatch(state, jobs, local)
    sent = []
    def finish():
        barrier.wait(); sent.append(s.phone_send_finish(key, operation_id=steps[0]['operation_id'], **arguments))
    threads = [threading.Thread(target=finish) for _ in range(4)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len(sent) == 4 and len({item['send_id'] for item in sent}) == 1
    assert count(s, 'outgoing_messages') == 1 and count(s, 'messenger_commands') == 2


# spec: ANYM-532
def test_a_lost_provider_receipt_stays_unknown_and_never_dispatches_again(native):
    s, archive, state, key = native
    jobs = []
    digest = preview(s, key, state, jobs)['preview_digest']
    first = send(s, key, state, jobs, digest)
    with s.pool.connection() as c:
        c.execute("UPDATE outgoing_messages SET status='unknown',error_code='send_result_unknown',"
                  "finished_at=clock_timestamp() WHERE id=%s", (first['send_id'],))
    repeat = send(s, key, state, jobs, digest, result=None)
    assert repeat['status'] == 'unknown' and repeat['send_id'] == first['send_id']
    assert count(s, 'outgoing_messages') == 1 and count(s, 'outgoing_messages', "status='queued'") == 0


# spec: ANYM-532
def test_guards_are_rechecked_after_the_chat_step_and_before_the_outbox(native):
    s, archive, state, key = native
    jobs = []
    digest = preview(s, key, state, jobs)['preview_digest']
    # The chat step committed, then the operation's opt-in went: the send is refused before the outbox.
    arguments = {**draft(), 'request_id': 'phone-recheck-0001', 'preview_digest': digest}
    step = s.phone_send_begin(key, **arguments)
    dispatch(state, jobs, local)
    s.set_messenger_operations(ACCOUNT, [])
    refused(lambda: s.phone_send_finish(key, operation_id=step['operation_id'], **arguments), 'operation_disabled')
    assert count(s, 'outgoing_messages') == 0
    s.set_messenger_operations(ACCOUNT, ['open_chat_by_phone'])
    with s.pool.connection() as c: c.execute('DELETE FROM chats WHERE id=%s', (CHAT,))
    # Sending turned off after the step was queued: the chat step creates no chat.
    arguments = {**arguments, 'request_id': 'phone-recheck-0002'}
    step = s.phone_send_begin(key, **arguments)
    archive.set_sending(ACCOUNT, False)
    dispatch(state, jobs, local)
    refused(lambda: s.phone_send_finish(key, operation_id=step['operation_id'], **arguments), 'sending_disabled')
    assert count(s, 'chats', f"id='{CHAT}'") == 0 and count(s, 'outgoing_messages') == 0
    archive.set_sending(ACCOUNT, True)
    # A collection policy that excludes the number refuses both tools before any lookup.
    with s.pool.connection() as c:
        c.execute('UPDATE account_policies SET exclude_chats=%s WHERE account=%s', ([PN], ACCOUNT))
    before = len(jobs)
    refused(lambda: s.phone_preview_lookup(key, **draft(text='Excluded')), 'chat_not_collected')
    refused(lambda: s.phone_send_begin(key, **draft(), request_id='phone-recheck-0003', preview_digest=digest),
            'chat_not_collected')
    assert len(jobs) == before and count(s, 'outgoing_messages') == 0


# spec: ANYM-532
def test_the_chat_step_opens_only_the_previewed_identity_titled_with_its_verified_name(native):
    s, archive, state, key = native
    jobs = []
    digest = preview(s, key, state, jobs)['preview_digest']
    other = '111222333444555@lid'
    refused(lambda: send(s, key, state, jobs, digest, request_id='phone-identity-0001',
                         result=answer(lookup=False, lid=other)), 'identity_changed')
    assert count(s, 'chats', f"account='{ACCOUNT}'") == 0 and count(s, 'outgoing_messages') == 0
    sent = send(s, key, state, jobs, digest, request_id='phone-identity-0002')
    assert sent['verified_name'] == 'Synthetic Bakery'
    with s.pool.connection() as c:
        assert c.execute('SELECT title FROM chats WHERE id=%s', (CHAT,)).fetchone()['title'] == 'Synthetic Bakery'


# spec: ANYM-532
def test_an_expired_preview_never_sends_after_a_stopped_chat_step(native):
    s, archive, state, key = native
    jobs = []
    digest = preview(s, key, state, jobs)['preview_digest']
    arguments = {**draft(), 'request_id': 'phone-late-0001', 'preview_digest': digest}
    step = s.phone_send_begin(key, **arguments)
    dispatch(state, jobs, local)
    with s.pool.connection() as c:
        c.execute("UPDATE messenger_commands SET created_at=created_at-interval '11 minutes',"
                  "finished_at=finished_at-interval '11 minutes' WHERE payload->>'phone_preview'='true'")
    error = refused(lambda: s.phone_send_finish(key, operation_id=step['operation_id'], **arguments), 'preview_expired')
    assert error['next_action'] == 'preview_message_again' and count(s, 'outgoing_messages') == 0
    # A new lookup of the same identity never revives the old digest.
    fresh = preview(s, key, state, jobs)['preview_digest']
    assert fresh != digest
    refused(lambda: send(s, key, state, jobs, digest, request_id='phone-late-0002'), 'preview_mismatch')
    assert send(s, key, state, jobs, fresh, request_id='phone-late-0003')['status'] == 'queued'
    assert count(s, 'outgoing_messages') == 1


# spec: ANYM-532
def test_a_pending_phone_send_reserves_its_request_id_for_the_principal(native):
    s, archive, state, key = native
    jobs = []
    archive_chat(state, PN, 'PN1')
    digest = preview(s, key, state, jobs)['preview_digest']
    arguments = {**draft(), 'request_id': 'phone-reserve-0001', 'preview_digest': digest}
    s.phone_send_begin(key, **arguments)
    # Before any outbox row: a chat_id send and another key of the same principal both conflict.
    refused(lambda: s.send_message(key, account=ACCOUNT, chat_id=stable_id('chat', ACCOUNT, PN), text='Other text',
                                   request_id='phone-reserve-0001'), 'idempotency_conflict')
    second = s.issue_key('phone-send-agent', [ACCOUNT], permissions=['read', 'send'])
    refused(lambda: s.phone_send_begin(second, **arguments), 'idempotency_conflict')
    assert count(s, 'outgoing_messages') == 0
    assert count(s, 'messenger_commands', "request_id LIKE 'phone-send:%%'") == 1


# spec: ANYM-532
def test_a_pending_phone_lookup_leaves_ordinary_sends_and_collection_usable(native):
    from messaging_service.outgoing import OutgoingWorker
    s, archive, state, key = native
    jobs = []
    archive_chat(state, PN, 'PN1')
    digest = preview(s, key, state, jobs)['preview_digest']
    # Another number's lookup stalls inside the adapter; a phone send's chat step stays queued behind it.
    other = draft(phone='12025550162', text='Stalled')
    stalled = s.phone_preview_lookup(key, **other)
    s.phone_send_begin(key, **draft(), request_id='phone-stall-0001', preview_digest=digest)
    queued = s.send_message(key, account=ACCOUNT, chat_id=stable_id('chat', ACCOUNT, PN), text=TEXT,
                            request_id='ordinary-send-0001')
    calls = []
    async def check():
        gate, started = asyncio.Lock(), asyncio.Event()
        async def execute(job):
            # Like the collector: the lookup's own deadline ends a stall as a retryable timeout.
            jobs.append(job); started.set()
            try: await asyncio.wait_for(asyncio.Event().wait(), 0.3)
            except TimeoutError: raise CommandRejected('provider_timeout') from None
        async def provider(job):
            calls.append(job); return {'provider_message_id': 'true_'+PN+'_SENT1'}
        lookup = asyncio.create_task(CommandWorker(state, execute, gate).once())
        await started.wait()
        sending = asyncio.create_task(OutgoingWorker(state, provider, gate).once())
        await asyncio.sleep(0.1)
        assert calls == []  # the send lane waits for the stalled lookup, it is not lost
        assert await asyncio.wait_for(lookup, 5) is True and await asyncio.wait_for(sending, 5) is True
    asyncio.run(check())
    assert jobs[-1]['parameters'] == {'phone': '12025550162', 'create': False}
    assert len(calls) == 1 and s.get_send_status(key, queued['send_id'])['status'] != 'queued'
    refused(lambda: s.phone_preview_result(key, operation_id=stalled, **other), 'provider_timeout')
    archive_chat(state, PN, 'PN2')
    assert count(s, 'messages', f"chat='{stable_id('chat', ACCOUNT, PN)}'") == 2


# spec: ANYM-532
def test_a_lost_receipt_from_the_phone_send_dispatch_is_unknown_and_never_retried(native):
    from messaging_service.outgoing import OutgoingWorker
    s, archive, state, key = native
    jobs, calls = [], []
    digest = preview(s, key, state, jobs)['preview_digest']
    first = send(s, key, state, jobs, digest, request_id='phone-lost-0001')
    async def provider(job):
        calls.append(job); raise TimeoutError('synthetic lost receipt')
    worker = OutgoingWorker(state, provider)
    asyncio.run(worker.once())
    assert s.get_send_status(key, first['send_id'])['status'] == 'unknown'
    repeat = send(s, key, state, jobs, digest, request_id='phone-lost-0001', result=None)
    asyncio.run(worker.once())
    assert repeat['status'] == 'unknown' and repeat['send_id'] == first['send_id'] and len(calls) == 1
    assert count(s, 'outgoing_messages') == 1 and len(jobs) == 2


# spec: ANYM-532
def test_a_waited_phone_send_keeps_its_chat_fields_and_old_chat_id_calls_work(native, monkeypatch):
    from messaging_service import send_wait
    s, archive, state, key = native
    jobs = []
    archive_chat(state, PN, 'PN1')
    async def waited(store, token, send_id, wait_s): return {'send_id': send_id, 'status': 'sent'}
    monkeypatch.setattr(send_wait, 'wait_for_send', waited)
    result = lambda job: local(job) if job['parameters']['create'] else answer(name='Synthetic Bakery')
    client, post = rest(s, key, monkeypatch, state, jobs, result)
    with client:
        digest = post('preview_message', draft(phone='+'+PHONE)).json()['preview_digest']
        sent = post('send_message', {**draft(phone='+'+PHONE), 'request_id': 'phone-wait-0001',
                                     'preview_digest': digest, 'wait_s': 1}).json()
        assert sent['status'] == 'sent' and sent['chat_id'] == CHAT and sent['chat_existed'] is False
        assert sent['verified_name'] == 'Synthetic Bakery'
        # A gateway or client from before ANYM-532 sends no phone field at all.
        old = {'account': ACCOUNT, 'chat_id': stable_id('chat', ACCOUNT, PN), 'text': TEXT}
        assert post('preview_message', old).json()['preview_digest']
        assert post('send_message', {**old, 'request_id': 'old-client-0001'}).json()['send_id']


def provider(tmp_path, local_lid):
    node = NodeProvider({}, tmp_path/'unused', 3)
    node.process = SimpleNamespace(returncode=None, stdout=asyncio.StreamReader())
    node.send_reader, node.send_writer = asyncio.StreamReader(), Writer()
    node.send_reader.feed_data(json.dumps({'id': 1, 'result': {'outcome': 'confirmed',
                                                               'data': {'local_lid': local_lid}}}).encode()+b'\n')
    return node


# spec: ANYM-532
@pytest.mark.parametrize('stored', [LID, '111222333444555@lid', None])
def test_the_collector_answers_the_send_step_only_from_the_previewed_identity(tmp_path, stored):
    async def check():
        node = provider(tmp_path, stored)
        node.send_reader.feed_data(json.dumps({'id': 2, 'result': answer(lookup=False)}).encode()+b'\n')
        node.send_reader.feed_data(json.dumps({'id': 3, 'result': {'provider_message_id': 'true_1@lid_X'}}).encode()+b'\n')
        asked = []
        async def archived(chat): asked.append(chat); return False
        job = {'operation': 'open_chat_by_phone', 'parameters': {'phone': '+'+PHONE, 'create': True},
               'expires_at': 1.0, 'known_chats': [], 'lookup_allowed': True, '_archived': archived, 'expected_lid': LID}
        async with node.send_lock:
            if stored != LID:
                with pytest.raises(CommandRejected, match='identity_changed'):
                    await node.execute_operation(job)
            else:
                await node.execute_operation(job)
        lines = [line['params'] for line in node.send_writer.lines]
        assert asked == [] and lines[0]['local_only'] is True
        if stored == LID:
            assert lines[1]['known_chats'] == [LID]  # a local answer, no counted lookup
            assert lines[1]['lookup_allowed'] is False  # and never a provider fallback
        else:
            assert len(lines) == 1  # no provider lookup after a changed identity
        # Isolation: the adapter stays usable for the next ordinary send.
        sent = await node._call_unlocked('send_text', {'chat': LID, 'text': 'Synthetic'}, sending=True)
        assert sent == {'provider_message_id': 'true_1@lid_X'} or stored != LID
    asyncio.run(check())


# spec: ANYM-532
def test_a_lid_that_leaves_the_adapter_between_the_two_calls_is_a_changed_identity(tmp_path):
    async def check():
        node = provider(tmp_path, LID)
        # operations.go answers rate_limited when no lookup is allowed and the LID is no longer a known chat.
        node.send_reader.feed_data(json.dumps({'id': 2, 'error': 'rate_limited'}).encode()+b'\n')
        async def archived(chat): raise AssertionError('no archive check for a send step')
        job = {'operation': 'open_chat_by_phone', 'parameters': {'phone': '+'+PHONE, 'create': True},
               'expires_at': 1.0, 'known_chats': [], 'lookup_allowed': True, '_archived': archived, 'expected_lid': LID}
        async with node.send_lock:
            with pytest.raises(CommandRejected, match='identity_changed'):
                await node.execute_operation(job)
        lines = [line['params'] for line in node.send_writer.lines]
        assert len(lines) == 2 and lines[1]['lookup_allowed'] is False and lines[1]['known_chats'] == [LID]
    asyncio.run(check())


# spec: ANYM-532
def test_the_number_is_not_logged_and_the_rest_path_works_end_to_end(native, monkeypatch, caplog):
    s, archive, state, key = native
    caplog.set_level(logging.DEBUG)
    jobs = []
    result = answer(name='Synthetic Bakery')
    client, post = rest(s, key, monkeypatch, state, jobs, lambda job: result if job['parameters']['create'] is False
                        else local(job))
    with client:
        shown = post('preview_message', draft(phone='+'+PHONE)).json()
        assert shown['on_whatsapp'] is True and shown['preview_contacts_provider'] is True
        sent = post('send_message', {**draft(phone=PN), 'request_id': 'phone-rest-0001',
                                     'preview_digest': shown['preview_digest']}).json()
        assert sent['chat_id'] == CHAT and sent['status'] == 'queued'
        # Old clients keep sending by chat_id with no phone field.
        plain = post('send_message', {'account': ACCOUNT, 'chat_id': CHAT, 'text': 'Synthetic follow-up',
                                      'request_id': 'phone-rest-0002'}).json()
        assert plain['status'] == 'queued' and plain['chat_id'] == CHAT
        reused = post('preview_message', draft(text='Another synthetic draft')).json()
    assert reused['preview_digest'] != shown['preview_digest'] and len(jobs) == 2
    assert PHONE not in caplog.text
    with s.pool.connection() as c:
        rows = c.execute('SELECT to_jsonb(a) AS row FROM audit_log a').fetchall()
    assert rows and not any(PHONE in json.dumps(r['row']) for r in rows)
