"""ANYM-531: a phone reference that is not found names the exact next call (synthetic only, never sends)."""
import asyncio
import json

import jsonschema
import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.commands import CommandRejected, CommandWorker
from messaging_service.envelope import stable_id
from messaging_service.error_details import payload as error_payload
from messaging_service.store import APIError
from messaging_service.whatsapp_collector import WhatsAppState
from test_media_request_surface import Store as SurfaceStore, rpc
from test_whatsapp import capture, event

ACCOUNT = 'wa-next'
OTHER = 'wa-next-other'
PHONE = '12025550147'  # NANP fictional 555-01xx range
PN = PHONE+'@c.us'
LID = '987654321012399@lid'
OPEN = {'tool': 'execute_messenger_operation', 'arguments': {'account': ACCOUNT, 'operation': 'open_chat_by_phone',
        'parameters': {'phone': '+'+PHONE, 'create': False}}, 'caller_supplies': ['request_id']}


def connect(s, archive, account):
    archive.register(account, 'whatsapp', 'Synthetic native', discover=True)
    epoch = archive.acquire(account, 'next-test')
    state = WhatsAppState(archive, account, ('next-test', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    state.provider_observation('whatsmeow')
    s.set_messenger_operations(account, ['open_chat_by_phone'])
    return state


@pytest.fixture
def native(seeded):
    s, *_ = seeded
    archive = Archive(s)
    state = connect(s, archive, ACCOUNT)
    key = s.issue_key('next-agent', [ACCOUNT], permissions=['read', 'send'])
    yield s, archive, state, key
    archive.release(ACCOUNT, 'next-test', state.lease[1])


def archive_chat(state, chat, provider_id):
    item = event(1); item['chatId'] = chat; item['senderId'] = chat; item['messageId'] = 'false_'+chat+'_'+provider_id
    state.apply(capture(item, account=state.account))


def answer(on=True, lookup=True):
    data = {'on_whatsapp': on, 'verified_name': None, 'provider_lookup': lookup}
    if on: data['provider_chat'] = LID
    return {'outcome': 'confirmed', 'data': data}


def dispatch(state, result):
    async def execute(job):
        return result
    assert asyncio.run(CommandWorker(state, execute).once())


def not_found(call):
    with pytest.raises(APIError, match='not_found') as caught:
        call()
    return error_payload(caught.value)


def both(s, key, reference, account=ACCOUNT):
    """resolve_reference and send_message; the send never reaches a provider: its target is refused first."""
    return [not_found(lambda: s.resolve_reference(key, account, reference)),
            not_found(lambda: s.send_message(key, account=account, chat_id=reference, text='Synthetic',
                                             request_id='next-send-0001'))]


def without_id(value):
    return {k: v for k, v in value.items() if k != 'request_id'}


def commands(s):
    with s.pool.connection() as c:
        return c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n']


def schemas():
    with TestClient(create_app(SurfaceStore(), ['testserver'])) as client:
        listed = rpc(client, '', method='tools/list').json()['result']['tools']
    return {tool['name']: tool for tool in listed}


def valid(tools, step, **supplied):
    """The suggestion fits the transport's exposed schema once the caller adds only what caller_supplies names."""
    assert set(supplied) == set(step['caller_supplies']) and not set(supplied) & set(step['arguments'])
    jsonschema.validate({**step['arguments'], **supplied}, tools[step['tool']]['inputSchema'])
    return {**step['arguments'], **supplied}


# spec: ANYM-531
@pytest.mark.parametrize('reference', ['+'+PHONE, PHONE, PN, PHONE+'@s.whatsapp.net'])
def test_not_found_phone_names_the_exact_open_chat_call(native, reference):
    s, archive, state, key = native
    for error in both(s, key, reference):
        assert error['hint'] == 'open_chat_by_phone' and error['next_call'] == OPEN
        assert error['next_action'] == 'open_chat_by_phone_then_follow_next_call'
        assert 'found_on_other_account' not in error
        # Only the caller's own number, normalized, is echoed.
        assert json.dumps(error).count(PHONE) == 1
    assert commands(s) == 0  # building the suggestion dispatches no lookup


# spec: ANYM-531
def test_each_step_names_the_next_call_until_the_preview(native):
    s, archive, state, key = native
    tools = schemas()
    step = both(s, key, '+'+PHONE)[0]['next_call']
    queued = s.execute_messenger_operation(key, **valid(tools, step, request_id='next-check-0001'))
    # Pending: poll by the returned operation ID, which is not the request ID.
    assert queued['operation_id'] != 'next-check-0001'
    assert queued['next_call'] == {'tool': 'get_messenger_operation',
                                   'arguments': {'operation_id': queued['operation_id']}, 'caller_supplies': []}
    dispatch(state, answer())
    checked = s.get_messenger_operation(key, **valid(tools, queued['next_call']))
    assert checked['status'] == 'succeeded' and 'chat_id' not in checked['result']['data']
    create = checked['next_call']
    assert create['tool'] == 'execute_messenger_operation' and create['caller_supplies'] == ['request_id']
    assert create['arguments'] == {**OPEN['arguments'], 'parameters': {'phone': '+'+PHONE, 'create': True}}
    assert create['owner_confirmation_required'] is True
    queued = s.execute_messenger_operation(key, **valid(tools, create, request_id='next-create-0001'))
    dispatch(state, answer())
    created = s.get_messenger_operation(key, operation_id=queued['operation_id'])
    chat_id = stable_id('chat', ACCOUNT, LID)
    assert created['next_call'] == {'tool': 'preview_message', 'arguments': {'account': ACCOUNT, 'chat_id': chat_id},
                                    'caller_supplies': ['text']}
    draft = s.preview_message(key, **valid(tools, created['next_call'], text='Synthetic first message'))
    assert draft['chat_id'] == chat_id and draft['owner_approval_verified_by_server'] is False
    # A check of a number whose chat already exists goes straight to the preview.
    queued = s.execute_messenger_operation(key, **valid(tools, step, request_id='next-check-0002'))
    dispatch(state, answer())
    again = s.get_messenger_operation(key, operation_id=queued['operation_id'])
    assert again['next_call'] == created['next_call']
    # Not on WhatsApp, or a failed lookup: nothing to suggest.
    for result in (answer(on=False), CommandRejected('identity_unresolved')):
        queued = s.execute_messenger_operation(key, **valid(tools, step, request_id='next-check-%04d' % (3+len(str(result)))))
        dispatch(state, result)
        assert 'next_call' not in s.get_messenger_operation(key, operation_id=queued['operation_id'])


# spec: ANYM-531
def test_a_readable_chat_on_another_account_is_named_without_a_next_call(native):
    s, archive, state, key = native
    other = connect(s, archive, OTHER)
    try:
        both_accounts = s.issue_key('next-both', [ACCOUNT, OTHER], permissions=['read', 'send'])
        archive_chat(other, PN, 'OTHER1')
        for error in both(s, both_accounts, '+'+PHONE):
            assert error['found_on_other_account'] == {'account': OTHER, 'chat_id': stable_id('chat', OTHER, PN)}
            assert error['next_action'] == 'use_existing_chat' and 'next_call' not in error
            assert error['hint'] == 'open_chat_by_phone'
        assert commands(s) == 0
    finally:
        archive.release(OTHER, 'next-test', other.lease[1])


# spec: ANYM-531
@pytest.mark.parametrize('hidden', ['chat_outside_key_scope', 'account_outside_key_scope'])
def test_a_hidden_match_on_another_account_changes_nothing(native, hidden):
    s, archive, state, key = native
    other = connect(s, archive, OTHER)
    try:
        if hidden == 'chat_outside_key_scope':
            archive_chat(other, '12025550100@c.us', 'VISIBLE1')
            agent = s.issue_key('next-hidden', [ACCOUNT, OTHER], permissions=['read', 'send'])
            s.set_access('next-hidden', chats=[stable_id('chat', OTHER, '12025550100@c.us')])
        else:
            agent = key
        before = [without_id(e) for e in both(s, agent, '+'+PHONE)]
        archive_chat(other, PN, 'HIDDEN1')
        assert [without_id(e) for e in both(s, agent, '+'+PHONE)] == before
        assert all('found_on_other_account' not in e and OTHER not in json.dumps(e) for e in before)
    finally:
        archive.release(OTHER, 'next-test', other.lease[1])


def spend_lookups(s, state, key):
    for n in range(10):
        s.execute_messenger_operation(key, **OPEN['arguments'], request_id='next-spend-%04d' % n)
        dispatch(state, answer(on=False))


# spec: ANYM-531
@pytest.mark.parametrize('refusal,expected', [
    ('operation_disabled', {'next_action': 'ask_operator_to_enable_open_chat_by_phone', 'account': ACCOUNT}),
    ('read_only_key', {'next_action': 'key_cannot_open_new_chats'}),
    ('chat_scoped_key', {'next_action': 'key_cannot_open_new_chats'}),
    ('lookups_spent', {'next_action': 'rate_limited'}),
    ('collection_excludes', {'next_action': 'chat_not_collected'}),
    ('not_a_number', {'next_action': 'invalid_phone'}),
])
def test_no_next_call_when_it_would_be_refused(native, refusal, expected):
    s, archive, state, key = native
    reference = '+'+PHONE
    if refusal == 'operation_disabled':
        s.set_messenger_operations(ACCOUNT, [])
    elif refusal == 'read_only_key':
        key = s.issue_key('next-reader', [ACCOUNT], permissions=['read'])
    elif refusal == 'chat_scoped_key':
        archive_chat(state, '12025550100@c.us', 'SCOPE1')
        s.set_access('next-agent', chats=[stable_id('chat', ACCOUNT, '12025550100@c.us')])
    elif refusal == 'lookups_spent':
        spend_lookups(s, state, key)
    elif refusal == 'collection_excludes':
        with s.pool.connection() as c:
            c.execute('UPDATE account_policies SET exclude_chats=%s WHERE account=%s', ([PN], ACCOUNT))
    else:
        reference = '+0123456789'
    count = commands(s)
    errors = both(s, key, reference) if refusal != 'read_only_key' else \
        [not_found(lambda: s.resolve_reference(key, ACCOUNT, reference))]
    for error in errors:
        assert error['error'] == 'not_found' and 'next_call' not in error
        assert {k: error.get(k) for k in expected} == expected and error['hint'] == 'open_chat_by_phone'
        if refusal == 'lookups_spent':
            assert 3500 <= error['resets_in_seconds'] <= 3600
    assert commands(s) == count


def sender_down(s, state, how):
    if how == 'disconnected':
        state.phase('connection', 'retry_wait', 'provider_disconnected')
    else:
        with s.pool.connection() as c:
            c.execute("UPDATE collector_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE account=%s",
                      (ACCOUNT,))


# spec: ANYM-531
@pytest.mark.parametrize('how', ['disconnected', 'lease_expired'])
def test_no_next_call_while_the_sender_is_not_ready(native, how):
    s, archive, state, key = native
    sender_down(s, state, how)
    count = commands(s)
    for error in both(s, key, '+'+PHONE):
        assert error['error'] == 'not_found' and 'next_call' not in error
        assert error['next_action'] == 'sender_not_ready' and error['hint'] == 'open_chat_by_phone'
    # The suggestion would have been refused the same way.
    with pytest.raises(APIError, match='sender_not_ready'):
        s.execute_messenger_operation(key, **OPEN['arguments'], request_id='next-ready-0001')
    assert commands(s) == count


# spec: ANYM-531
@pytest.mark.parametrize('change,expected', [
    ('operation_disabled', {'next_action': 'ask_operator_to_enable_open_chat_by_phone', 'account': ACCOUNT}),
    ('collection_excludes', {'next_action': 'chat_not_collected'}),
    ('sender_disconnected', {'next_action': 'sender_not_ready'}),
])
def test_a_receipt_offers_the_create_step_only_while_it_would_be_admitted(native, change, expected):
    s, archive, state, key = native
    queued = s.execute_messenger_operation(key, **OPEN['arguments'], request_id='next-recheck-0001')
    dispatch(state, answer())
    assert s.get_messenger_operation(key, operation_id=queued['operation_id'])['next_call']['arguments'][
        'parameters']['create'] is True
    if change == 'operation_disabled':
        s.set_messenger_operations(ACCOUNT, [])
    elif change == 'collection_excludes':
        with s.pool.connection() as c:
            c.execute('UPDATE account_policies SET exclude_chats=%s WHERE account=%s', ([PN], ACCOUNT))
    else:
        sender_down(s, state, 'disconnected')
    receipt = s.get_messenger_operation(key, operation_id=queued['operation_id'])
    # The receipt stays readable; only the refused next step is withheld, with its reason.
    assert receipt['status'] == 'succeeded' and receipt['result']['data']['on_whatsapp'] is True
    assert 'next_call' not in receipt and {k: receipt.get(k) for k in expected} == expected
    with pytest.raises(APIError):
        s.execute_messenger_operation(key, **OPEN['arguments'] | {'parameters': {'phone': '+'+PHONE, 'create': True}},
                                      request_id='next-recheck-0002')


# spec: ANYM-531
@pytest.mark.parametrize('platform', ['webjs', 'telegram'])
def test_other_adapters_get_the_supported_kind_instead_of_a_call(seeded, platform):
    s, *_ = seeded
    account = 'next-'+platform
    Archive(s).register(account, 'telegram' if platform == 'telegram' else 'whatsapp', 'Synthetic', discover=True)
    key = s.issue_key('next-other', [account], permissions=['read', 'send'])
    for error in both(s, key, '+'+PHONE, account=account):
        assert error['hint'] == 'native_whatsapp_accounts_only' and 'next_call' not in error
        assert error['next_action'] == 'use_username_or_existing_chat'


# spec: ANYM-531
def test_a_failing_suggestion_still_answers_the_plain_not_found(native, monkeypatch):
    from messaging_service import commands as module
    s, archive, state, key = native
    def broken(*args):
        raise RuntimeError('synthetic suggestion failure')
    monkeypatch.setattr(module, 'phone_next_step', broken)
    for error in both(s, key, '+'+PHONE):
        assert error['error'] == 'not_found' and error['hint'] == 'open_chat_by_phone' and 'next_call' not in error
        assert error['next_action'] == 'check_reference_and_permitted_scope'
    # The transaction stays usable: the same request still resolves an archived chat afterwards.
    archive_chat(state, PN, 'AFTER1')
    assert s.resolve_reference(key, ACCOUNT, PN)['chat']['account'] == ACCOUNT


# spec: ANYM-531
def test_tool_descriptions_say_how_a_new_number_is_reached():
    tools = schemas()
    for name in ('resolve_reference', 'send_message'):
        sentence = next(line for line in tools[name]['description'].split('. ') if 'open_chat_by_phone' in line)
        assert 'execute_messenger_operation' in sentence and 'messages_execute' not in sentence
