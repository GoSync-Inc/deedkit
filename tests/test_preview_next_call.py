"""ANYM-646: a send or operation refused for a missing or changed preview names the exact preview call
(synthetic only; nothing reaches a provider)."""
import hashlib
import json

import pytest

from messaging_service import phone_send as phone_module, username_send as username_module
from messaging_service.envelope import stable_id
from messaging_service.error_details import payload as error_payload
from messaging_service.store import APIError
from test_messenger_commands import LINK, link_check, operation_args, operator  # noqa: F401 (fixture)
from test_phone_next_call import schemas, valid
from test_phone_send import (ACCOUNT as PHONE_ACCOUNT, PHONE, dispatch as phone_dispatch,  # noqa: F401 (fixture)
                             draft as phone_draft, local as phone_local, native, preview as phone_preview)
from test_username_send import (ACCOUNT as USERNAME_ACCOUNT, USERNAME, answer as username_answer,  # noqa: F401
                                dispatch as username_dispatch, draft as username_draft,
                                preview as username_preview, telegram)

ACCOUNT = 'command-fixture'
CHAT = stable_id('chat', ACCOUNT, '100')
REPLY = stable_id('msg', ACCOUNT, '100', '1')
TEXT = 'Synthetic rich message'
ACTION = 'preview_then_send_with_digest'
SEND_HINT = ('If next_call is present, call its tool with its arguments plus your own values for caller_supplies, '
             'show the preview to the owner and, after the owner confirms, repeat this send unchanged with the '
             'returned preview_digest; if it is absent, this key or route cannot run the preview tool.')
OPERATION_HINT = SEND_HINT.replace('repeat this send', 'repeat this operation')
BARE = {'error', 'request_id', 'retryable', 'next_action'}


def refusal(call, code):
    with pytest.raises(APIError, match=code) as caught:
        call()
    assert caught.value.status == 409
    return error_payload(caught.value)


def clean(body, *secrets):
    """No digest or confirm token key, and no caller content in the body."""
    text = json.dumps(body)
    assert 'preview_digest' not in body and 'confirm_token' not in body
    for secret in secrets:
        assert secret not in text


def passes(call):
    """The refused call, repeated with the preview's digest, is no longer refused for its preview."""
    try:
        call()
    except APIError as error:
        assert error.code not in ('preview_required', 'preview_mismatch')


@pytest.fixture
def sending(operator):  # noqa: F811
    s, archive, key, state = operator
    archive.set_sending(ACCOUNT, True)
    return s, key


def archive_send(**changes):
    return {'account': ACCOUNT, 'chat_id': CHAT, 'text': TEXT, 'reply_to': REPLY, 'format': 'markdown',
            'request_id': 'next-archive-0001', **changes}


# spec: ANYM-646
def test_an_archive_send_without_a_preview_names_the_preview_call(sending):
    s, key = sending
    tools = schemas()
    body = refusal(lambda: s.send_message(key, **archive_send()), 'preview_required')
    assert body['next_call'] == {'tool': 'preview_message', 'arguments': {'account': ACCOUNT, 'chat_id': CHAT},
                                 'caller_supplies': ['text', 'reply_to', 'format']}
    assert body['next_action'] == ACTION and body['hint'] == SEND_HINT
    clean(body, TEXT)
    built = valid(tools, body['next_call'], text=TEXT, reply_to=REPLY, format='markdown')
    digest = s.preview_message(key, **built)['preview_digest']
    clean(body, digest)
    passes(lambda: s.send_message(key, **archive_send(), preview_digest=digest))


# spec: ANYM-646
def test_an_archive_send_with_a_changed_or_malformed_digest_names_the_preview_call(sending):
    s, key = sending
    tools = schemas()
    digest = s.preview_message(key, account=ACCOUNT, chat_id=CHAT, text=TEXT, reply_to=REPLY,
                               format='markdown')['preview_digest']
    for number, wrong in enumerate((digest, 'f' * 64, 'not-a-digest')):
        request_id = 'next-archive-%04d' % (number+2)
        changed = archive_send(request_id=request_id, **({'text': 'Changed synthetic message'} if wrong == digest else {}))
        body = refusal(lambda: s.send_message(key, **changed, preview_digest=wrong), 'preview_mismatch')
        assert body['next_call']['tool'] == 'preview_message' and body['next_action'] == ACTION
        assert body['hint'] == SEND_HINT
        clean(body, 'Changed synthetic message', wrong)
        built = valid(tools, body['next_call'], text=changed['text'], reply_to=REPLY, format='markdown')
        fresh = s.preview_message(key, **built)['preview_digest']
        passes(lambda: s.send_message(key, **changed, preview_digest=fresh))


# spec: ANYM-646
def test_a_plain_send_and_a_refusal_other_than_a_preview_keep_their_body(sending):
    s, key = sending
    body = refusal(lambda: s.send_message(key, **archive_send(format='plain', reply_to=''),
                                          preview_digest='f' * 64), 'preview_mismatch')
    assert body['next_call']['caller_supplies'] == ['text']
    with pytest.raises(APIError, match='invalid_message_format') as caught:
        s.send_message(key, **archive_send(format='rtf'))
    assert set(error_payload(caught.value)) == BARE


# spec: ANYM-646
def test_a_phone_send_without_or_with_a_changed_preview_names_the_preview_call(native):  # noqa: F811
    s, archive, state, key = native
    jobs, tools = [], schemas()
    request = {**phone_draft(format='markdown'), 'request_id': 'next-phone-0001'}
    body = refusal(lambda: s.phone_send_begin(key, **request, preview_digest=''), 'preview_required')
    assert body['next_call'] == {'tool': 'preview_message', 'arguments': {'account': PHONE_ACCOUNT},
                                 'caller_supplies': ['text', 'phone', 'format']}
    assert body['next_action'] == ACTION and body['hint'] == SEND_HINT
    clean(body, TEXT, PHONE)
    built = valid(tools, body['next_call'], text=request['text'], phone=PHONE, format='markdown')
    digest = phone_preview(s, key, state, jobs, format='markdown')['preview_digest']
    assert built['account'] == PHONE_ACCOUNT
    assert 'operation_id' in s.phone_send_begin(key, **request, preview_digest=digest)
    clean(body, digest)
    changed = {**request, 'text': 'Changed synthetic text', 'request_id': 'next-phone-0002'}
    body = refusal(lambda: s.phone_send_begin(key, **changed, preview_digest=digest), 'preview_mismatch')
    assert body['next_call']['arguments'] == {'account': PHONE_ACCOUNT}
    assert body['next_action'] == 'preview_message_again' and body['hint'] == SEND_HINT
    clean(body, 'Changed synthetic text', digest, PHONE)


# spec: ANYM-646
def test_a_username_send_without_or_with_a_changed_preview_names_the_preview_call(telegram):  # noqa: F811
    s, archive, state, key = telegram
    jobs, tools = [], schemas()
    request = {**username_draft(), 'request_id': 'next-username-0001'}
    body = refusal(lambda: s.username_send_begin(key, **request, preview_digest=''), 'preview_required')
    assert body['next_call'] == {'tool': 'preview_message', 'arguments': {'account': USERNAME_ACCOUNT},
                                 'caller_supplies': ['text', 'username']}
    assert body['next_action'] == ACTION and body['hint'] == SEND_HINT
    clean(body, TEXT, USERNAME)
    valid(tools, body['next_call'], text=request['text'], username=USERNAME)
    digest = username_preview(s, key, state, jobs)['preview_digest']
    assert 'operation_id' in s.username_send_begin(key, **request, preview_digest=digest)
    changed = {**request, 'text': 'Changed synthetic text', 'request_id': 'next-username-0002'}
    body = refusal(lambda: s.username_send_begin(key, **changed, preview_digest=digest), 'preview_mismatch')
    assert body['next_action'] == 'preview_message_again' and body['hint'] == SEND_HINT
    clean(body, 'Changed synthetic text', digest, USERNAME)


def altered_digest(module, monkeypatch):
    """After the chat step the re-rendered draft no longer matches the digest the caller confirmed."""
    real = module._draft

    def draft(*args, **kwargs):
        rendered, chat_id, existed = real(*args, **kwargs)
        return {**rendered, 'preview_digest': 'f' * 64}, chat_id, existed
    monkeypatch.setattr(module, '_draft', draft)


# spec: ANYM-646
def test_a_phone_send_refused_after_its_chat_step_keeps_its_body(native, monkeypatch):  # noqa: F811
    s, archive, state, key = native
    jobs = []
    arguments = {**phone_draft(), 'request_id': 'next-finish-0001',
                 'preview_digest': phone_preview(s, key, state, jobs)['preview_digest']}
    step = s.phone_send_begin(key, **arguments)
    phone_dispatch(state, jobs, phone_local)
    altered_digest(phone_module, monkeypatch)
    body = refusal(lambda: s.phone_send_finish(key, operation_id=step['operation_id'], **arguments), 'preview_mismatch')
    assert set(body) == BARE


# spec: ANYM-646
def test_a_username_send_refused_after_its_chat_step_keeps_its_body(telegram, monkeypatch):  # noqa: F811
    s, archive, state, key = telegram
    jobs = []
    arguments = {**username_draft(), 'request_id': 'next-finish-0002',
                 'preview_digest': username_preview(s, key, state, jobs)['preview_digest']}
    step = s.username_send_begin(key, **arguments)
    username_dispatch(state, jobs, username_answer())
    altered_digest(username_module, monkeypatch)
    body = refusal(lambda: s.username_send_finish(key, operation_id=step['operation_id'], **arguments), 'preview_mismatch')
    assert set(body) == BARE


def file_operation(s, key):
    content = b'Synthetic registered original'
    sha = hashlib.sha256(content).hexdigest()
    s.files.mkdir(parents=True, exist_ok=True)
    (s.files / sha).write_bytes(content)
    attachment = stable_id('att', ACCOUNT, '100', '1', 'synthetic-file')
    with s.pool.connection() as c:
        c.execute('INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES(%s,%s,%s,%s,%s,%s)',
                  (attachment, REPLY, sha, 'synthetic.txt', 'text/plain', len(content)))
    return {'account': ACCOUNT, 'operation': 'send_file', 'chat_id': stable_id('chat', ACCOUNT, '200'),
            'parameters': {'attachment_id': attachment, 'sha256': sha}}


# spec: ANYM-646
def test_an_operation_without_or_with_a_changed_preview_names_the_preview_call(operator):  # noqa: F811
    s, _, key, _ = operator
    tools = schemas()
    args = operation_args()
    body = refusal(lambda: s.execute_messenger_operation(key, **args, request_id='next-op-0001'), 'preview_required')
    assert body['next_call'] == {'tool': 'preview_messenger_operation', 'caller_supplies': [], 'arguments': {
        'account': ACCOUNT, 'operation': 'delete_message', 'chat_id': args['chat_id'],
        'message_id': args['message_id'], 'expected_revision': 1}}
    assert body['next_action'] == ACTION and body['hint'] == OPERATION_HINT
    built = valid(tools, body['next_call'])
    digest = s.preview_messenger_operation(key, **built)['preview_digest']
    assert s.execute_messenger_operation(key, **args, request_id='next-op-0001', preview_digest=digest)['status'] == 'queued'
    body = refusal(lambda: s.execute_messenger_operation(key, **args, request_id='next-op-0002', preview_digest='f' * 64),
                   'preview_mismatch')
    assert body['next_call']['arguments'] == built and body['hint'] == OPERATION_HINT
    clean(body, digest)
    body = refusal(lambda: s.execute_messenger_operation(key, **args, request_id='next-op-0003',
                                                         preview_digest='not-a-digest'), 'preview_mismatch')
    assert body['next_call']['tool'] == 'preview_messenger_operation'


# spec: ANYM-646
def test_an_operation_with_an_upload_reference_names_parameters_without_echoing_them(operator):  # noqa: F811
    s, _, key, _ = operator
    tools = schemas()
    args = file_operation(s, key)
    body = refusal(lambda: s.execute_messenger_operation(key, **args, request_id='next-file-0001'), 'preview_required')
    assert body['next_call'] == {'tool': 'preview_messenger_operation', 'caller_supplies': ['parameters'],
                                 'arguments': {'account': ACCOUNT, 'operation': 'send_file', 'chat_id': args['chat_id']}}
    clean(body, args['parameters']['attachment_id'], args['parameters']['sha256'])
    built = valid(tools, body['next_call'], parameters=args['parameters'])
    digest = s.preview_messenger_operation(key, **built)['preview_digest']
    assert s.execute_messenger_operation(key, **args, request_id='next-file-0001', preview_digest=digest)['status'] == 'queued'


# spec: ANYM-646
def test_join_chat_after_a_fresh_check_names_the_preview_and_without_one_keeps_its_body(operator):  # noqa: F811
    s, _, key, state = operator
    s.set_messenger_operations(ACCOUNT, ['check_chat_link', 'join_chat'])
    tools = schemas()
    args = {'account': ACCOUNT, 'operation': 'join_chat', 'parameters': {'link': LINK}}
    body = refusal(lambda: s.execute_messenger_operation(key, **args, request_id='next-join-0001'), 'preview_required')
    assert set(body) == BARE  # no fresh successful check: today's body, byte for byte
    link_check(s, key, state, request_id='next-check-0001')
    body = refusal(lambda: s.execute_messenger_operation(key, **args, request_id='next-join-0001'), 'preview_required')
    assert body['next_call'] == {'tool': 'preview_messenger_operation', 'caller_supplies': ['parameters'],
                                 'arguments': {'account': ACCOUNT, 'operation': 'join_chat'}}
    clean(body, LINK, 'AbCd1234')
    built = valid(tools, body['next_call'], parameters=args['parameters'])
    digest = s.preview_messenger_operation(key, **built)['preview_digest']
    link_check(s, key, state, request_id='next-check-0002', title='Synthetic renamed chat')
    body = refusal(lambda: s.execute_messenger_operation(key, **args, request_id='next-join-0001', preview_digest=digest),
                   'preview_mismatch')
    assert body['next_call']['tool'] == 'preview_messenger_operation'
    clean(body, digest, LINK)
    fresh = s.preview_messenger_operation(key, **valid(tools, body['next_call'], parameters=args['parameters']))
    assert s.execute_messenger_operation(key, **args, request_id='next-join-0001',
                                         preview_digest=fresh['preview_digest'])['status'] == 'queued'
