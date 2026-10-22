"""Synthetic client contracts for explicit event consumption and private feedback."""
import json
import math

import httpx
import pytest

from anymsg import AnyMsgError, Client


METHODS = {
    'fetch_subscription_events': {'subscription_id': 'sub_synthetic', 'limit': 4},
    'ack_subscription_events': {'subscription_id': 'sub_synthetic', 'receipt': 'r'*43, 'version': 2},
    'wait_for_subscription_events': {'subscription_id': 'sub_synthetic', 'timeout': 0.25, 'limit': 4},
    'configure_subscription_delivery': {'subscription_id': 'sub_synthetic', 'version': 2,
        'receiver_id': '11111111-1111-4111-8111-111111111111'},
    'test_subscription': {'subscription_id': 'sub_synthetic', 'request_id': 'synthetic-probe'},
    'submit_feedback': {'request_id': 'synthetic-feedback', 'category': 'bug',
        'description': 'Synthetic issue', 'operation': 'get_context',
        'refs': {'message_ids': ['msg_synthetic'], 'chat_ids': [], 'subscription_ids': ['sub_synthetic']}},
    'list_feedback': {'limit': 4},
}


@pytest.mark.parametrize('operation', METHODS)
def test_python_audit_helpers_preserve_exact_wire_arguments(operation):
    seen = []
    def transport(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={'future_field': {'verified': False}})
    with Client('https://archive.example/prefix', 'synthetic-token',
                transport=httpx.MockTransport(transport)) as client:
        assert getattr(client, operation)(**METHODS[operation]) == {'future_field': {'verified': False}}
    assert seen == [('/prefix/v1/' + operation, METHODS[operation])]


def test_python_audit_defaults_and_old_server_payloads():
    seen = []
    def transport(request):
        seen.append((request.url.path.rsplit('/', 1)[-1], json.loads(request.content)))
        return httpx.Response(200, json={})
    with Client('https://archive.example', 'synthetic-token',
                transport=httpx.MockTransport(transport)) as client:
        client.fetch_subscription_events(subscription_id='sub_synthetic')
        client.wait_for_subscription_events(subscription_id='sub_synthetic')
        client.configure_subscription_delivery(subscription_id='sub_synthetic', version=1)
        client.list_feedback()
        client.submit_feedback(request_id='synthetic-feedback', category='other', description='Synthetic')
        client.create_subscription(chat_ids=['chat_synthetic'])
        client.create_subscription(chat_ids=['chat_synthetic'], wake_rules=['reply_to_confirmed_send'],
            reply_to_send='send_synthetic')
        client.send_message(account='synthetic', chat_id='chat_synthetic', text='Confirmed',
            request_id='synthetic-send', wait_s=0)
        client.send_message(account='synthetic', chat_id='chat_synthetic', text='Confirmed',
            request_id='synthetic-send', wait_s=0.25)
    assert seen[:5] == [
        ('fetch_subscription_events', {'subscription_id': 'sub_synthetic', 'limit': 50}),
        ('wait_for_subscription_events', {'subscription_id': 'sub_synthetic', 'timeout': 25, 'limit': 50}),
        ('configure_subscription_delivery', {'subscription_id': 'sub_synthetic', 'version': 1, 'receiver_id': None}),
        ('list_feedback', {'limit': 50}),
        ('submit_feedback', {'request_id': 'synthetic-feedback', 'category': 'other',
            'description': 'Synthetic', 'operation': None, 'refs': None}),
    ]
    old_selection = dict(chat_ids=['chat_synthetic'], sender_ids=None, kinds=None,
        direction='incoming', origins=None, label='', expires_at=None)
    assert seen[5] == ('create_subscription', old_selection)
    assert seen[6] == ('create_subscription', {**old_selection,
        'wake_rules': ['reply_to_confirmed_send'], 'reply_to_send': 'send_synthetic'})
    assert 'wait_s' not in seen[7][1]
    assert seen[8][1] == {**seen[7][1], 'wait_s': 0.25}


def test_python_wait_defaults_allow_server_deadline_and_respect_explicit_client_timeout():
    seen = []
    def transport(request):
        seen.append(request.extensions['timeout'])
        return httpx.Response(200, json={})
    for timeout in (None, 1):
        with Client('https://archive.example', 'synthetic-token',
                    transport=httpx.MockTransport(transport), **({} if timeout is None else {'timeout': timeout})) as client:
            client.wait_for_subscription_events(subscription_id='sub_synthetic')
    assert seen[0]['read'] == 30 and seen[1]['read'] == 1


def test_python_audit_valid_boundaries_and_nullable_fields():
    seen = []
    def transport(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={})
    with Client('https://archive.example', 'synthetic-token', transport=httpx.MockTransport(transport)) as client:
        for timeout in (0, 25):
            client.wait_for_subscription_events(subscription_id='sub_synthetic', timeout=timeout, limit=100)
        client.call('create_subscription', {'chat_ids':['chat_synthetic'], 'wake_rules':None, 'reply_to_send':None})
        client.create_subscription(chat_ids=['chat_synthetic'], wake_rules=['incoming_message',
            'owner_mention','reply_to_owner','reply_to_confirmed_send'], reply_to_send='send_synthetic')
        client.submit_feedback(request_id='synthetic-feedback', category='quality', description='🪪'*4000,
            refs={'message_ids':[]}, operation=None)
        client.configure_subscription_delivery(subscription_id='sub_synthetic', version=1, receiver_id=None)
    assert seen[0]['timeout'] == 0 and seen[1]['timeout'] == 25
    assert seen[2]['wake_rules'] is None and seen[-1]['receiver_id'] is None


INVALID = [
    ('fetch_subscription_events', {'subscription_id': ''}),
    ('fetch_subscription_events', {'limit': True}),
    ('fetch_subscription_events', {'limit': 101}),
    ('ack_subscription_events', {'receipt': 'r'*31}),
    ('ack_subscription_events', {'receipt': 'r'*129}),
    ('ack_subscription_events', {'receipt': False}),
    ('ack_subscription_events', {'version': True}),
    ('ack_subscription_events', {'version': 0}),
    ('ack_subscription_events', {'version': 2**53}),
    *[('wait_for_subscription_events', {'timeout': v}) for v in (-1, 25.1, True, '1', math.nan, math.inf)],
    ('wait_for_subscription_events', {'limit': 0}),
    ('configure_subscription_delivery', {'version': False}),
    *[('configure_subscription_delivery', {'receiver_id': v}) for v in ('', False,
        'https://receiver.example/hook', '11111111-1111-4111-8111-11111111111z')],
    *[('test_subscription', {'request_id': v}) for v in ('short', 'x'*129, 'private\ncontrol', True)],
    *[('submit_feedback', {'category': v}) for v in ('unknown', False, ['bug'])],
    *[('submit_feedback', {'description': v}) for v in ('', ' ', 'x'*4001, 'private\x00control', False)],
    *[('submit_feedback', {'operation': v}) for v in (False, 'private\noperation', 'x'*130)],
    *[('submit_feedback', {'refs': v}) for v in ([], {'unknown': []}, {'message_ids': 'message'},
        {'message_ids': ['x']*33}, {'chat_ids': [False]}, {'subscription_ids': ['']})],
    ('list_feedback', {'limit': 0}),
]


@pytest.mark.parametrize('operation,extra', INVALID)
def test_python_audit_validation_rejects_before_transport(operation, extra):
    def forbidden(_):
        raise AssertionError('Invalid input must not leave the SDK')
    with Client('https://archive.example', 'synthetic-token', transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ValueError):
            client.call(operation, {**METHODS[operation], **extra})
        with pytest.raises(ValueError):
            getattr(client, operation)(**{**METHODS[operation], **extra})


@pytest.mark.parametrize('operation', METHODS)
def test_python_audit_unknown_fields_rejected_before_transport(operation):
    def forbidden(_):
        raise AssertionError('Invalid input must not leave the SDK')
    with Client('https://archive.example', 'synthetic-token', transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ValueError):
            client.call(operation, {**METHODS[operation], 'task_id': 'unverified-task'})


@pytest.mark.parametrize('reference_kind', ['message_ids', 'chat_ids', 'subscription_ids'])
@pytest.mark.parametrize('control', ['\x00', '\n', '\x1f', '\x7f'])
def test_python_feedback_control_references_rejected_before_transport(reference_kind, control):
    seen = []
    def transport(request):
        seen.append(request)
        return httpx.Response(200, json={})
    parameters = {**METHODS['submit_feedback'], 'refs': {reference_kind: ['private' + control + 'control']}}
    with Client('https://archive.example', 'synthetic-token', transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(ValueError):
            client.submit_feedback(**parameters)
        with pytest.raises(ValueError):
            client.call('submit_feedback', parameters)
    assert seen == []


@pytest.mark.parametrize('extra', [
    {'wake_rules': []}, {'wake_rules': ['unknown']}, {'wake_rules': [False]},
    {'wake_rules': ['incoming_message']*5}, {'reply_to_send': ''}, {'reply_to_send': 'x'*129},
    {'reply_to_send': True}, {'wake_rules': ['reply_to_confirmed_send']},
])
def test_python_wake_rules_reject_malformed_selection_before_transport(extra):
    with Client('https://archive.example', 'synthetic-token', transport=httpx.MockTransport(
            lambda _: pytest.fail('Invalid selection reached transport'))) as client:
        with pytest.raises(ValueError):
            client.create_subscription(chat_ids=['chat_synthetic'], **extra)


@pytest.mark.parametrize('wait_s', [-1, 25.1, False, True, '1', math.nan, math.inf])
def test_python_send_wait_rejects_invalid_bound_before_transport(wait_s):
    with Client('https://archive.example', 'synthetic-token', transport=httpx.MockTransport(
            lambda _: pytest.fail('Invalid wait reached transport'))) as client:
        with pytest.raises(ValueError):
            client.send_message(account='synthetic', chat_id='chat_synthetic', text='Confirmed',
                request_id='synthetic-send', wait_s=wait_s)
        with pytest.raises(ValueError):
            client.call('send_message', {'wait_s': wait_s})


ERRORS = ('subscription_wait_timeout', 'subscription_receipt_limit', 'invalid_feedback',
    'feedback_limit', 'invalid_receiver', 'delivery_not_configured', 'consumer_already_initialized',
    'send_wait_busy', 'send_status_timeout', 'send_access_changed', 'send_expired', 'send_result_unknown')


@pytest.mark.parametrize('code', ERRORS)
def test_python_audit_errors_are_safe_and_never_retried(code):
    seen = []
    def transport(request):
        seen.append(request.url.path)
        return httpx.Response(409, json={'error': code, 'detail': 'synthetic-private-source', 'request_id': 'b'*32})
    with Client('https://archive.example', 'synthetic-token', transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(AnyMsgError) as failure:
            client.list_feedback()
    assert failure.value.code == code and failure.value.request_id == 'b'*32
    assert 'synthetic-private-source' not in str(failure.value)
    assert seen == ['/v1/list_feedback']


@pytest.mark.parametrize('operation', ['ack_subscription_events', 'configure_subscription_delivery',
    'test_subscription', 'submit_feedback'])
def test_python_audit_uncertain_mutations_never_retry(operation):
    seen = []
    def transport(request):
        seen.append(request.url.path)
        raise httpx.ReadTimeout('synthetic-private-source')
    with Client('https://archive.example', 'synthetic-token', transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(AnyMsgError, match='request_failed'):
            getattr(client, operation)(**METHODS[operation])
    assert seen == ['/v1/' + operation]


# spec: ANYM-343
def test_python_list_feedback_forwards_status_filter_and_rejects_unknown_status():
    seen = []
    def transport(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'items': [{'id': 'synthetic', 'status': 'verify_requested',
            'fixed_in': 'deedkit@' + 'b' * 40, 'spec_ref': 'ANYM-274', 'status_note': None,
            'duplicate_of': None, 'status_changed_at': '2026-10-21T00:00:00Z'}], 'has_more': False})
    with Client('https://archive.example', 'synthetic-token', transport=httpx.MockTransport(transport)) as client:
        assert client.list_feedback(limit=5, status='verify_requested')['items'][0]['status'] == 'verify_requested'
        for status in ('planned', '', 7, ['fixed']):
            with pytest.raises(ValueError, match='invalid_parameters'):
                client.list_feedback(status=status)
    assert seen == [{'limit': 5, 'status': 'verify_requested'}]


# spec: ANYM-342
def test_python_feedback_operation_accepts_the_longest_tool_name():
    seen = []
    def transport(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={})
    with Client('https://archive.example', 'synthetic-token', transport=httpx.MockTransport(transport)) as client:
        client.submit_feedback(**{**METHODS['submit_feedback'], 'operation': 'calendar_' + 'a' * 120})
    assert [body['operation'] for body in seen] == ['calendar_' + 'a' * 120]

