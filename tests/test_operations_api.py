"""Strict messenger-operation boundaries and SDK transports; synthetic only."""
import json

import httpx
import pytest
from starlette.testclient import TestClient

from anymsg.client import AnyMsgError, Client
from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.messenger_operations import ParameterError, parameter_format, validate_operation
from messaging_service.store import APIError
from test_archive import event


METHODS = ('list_messenger_operations', 'preview_messenger_operation',
           'execute_messenger_operation', 'get_messenger_operation')


class RecordingStore:
    public_url = 'http://testserver'
    def __init__(self): self.calls = []
    def authenticate(self, token): return 'synthetic-principal'
    def audit_boundary(self, code): pass
    def _record(self, method, token, params):
        self.calls.append((method, params))
        return {'operation_id': 'op_synthetic', 'status': 'queued'}
    def list_messenger_operations(self, token, **params):
        return self._record(METHODS[0], token, params)
    def preview_messenger_operation(self, token, **params):
        return self._record(METHODS[1], token, params)
    def execute_messenger_operation(self, token, **params):
        return self._record(METHODS[2], token, params)
    def get_messenger_operation(self, token, **params):
        return self._record(METHODS[3], token, params)


def arguments(method):
    if method == METHODS[0]: return {'account': 'synthetic'}
    if method == METHODS[3]: return {'operation_id': 'op_synthetic'}
    value = dict(account='synthetic', operation='react', chat_id='chat_synthetic',
        message_id='msg_synthetic', expected_revision=3, parameters={'emoji': '👍'})
    if method == METHODS[2]: value.update(request_id='synthetic-action', preview_digest='a'*64)
    return value


def rpc(client, method, params):
    return client.post('/mcp', headers={'Authorization': 'Bearer synthetic-token',
        'Accept': 'application/json, text/event-stream'}, json={'jsonrpc': '2.0', 'id': 1,
        'method': 'tools/call', 'params': {'name': method, 'arguments': params}})


@pytest.mark.parametrize('method', METHODS)
def test_operation_rest_and_mcp_match_exact_payload(method):
    store = RecordingStore()
    params = arguments(method)
    with TestClient(create_app(store, ['testserver'])) as client:
        rest = client.post('/v1/'+method, headers={'Authorization': 'Bearer synthetic-token'}, json=params)
        mcp = rpc(client, method, params)
    assert rest.status_code == 200
    assert not mcp.json().get('error') and not mcp.json()['result'].get('isError')
    assert store.calls == [(method, params), (method, params)]


# spec: ANYM-306
@pytest.mark.parametrize('body', [b'\xff', b'\xff\xfe\x00', b'{"query": "\xc3\x28"}', b'{"query": ', b'[]', b'{"token": "x"}',
    b'{"limit": '+b'1'*4400+b'}'])
def test_rest_body_that_cannot_be_decoded_is_invalid_parameters_not_retryable(body):
    store = RecordingStore()
    with TestClient(create_app(store, ['testserver'])) as client:
        rest = client.post('/v1/search', headers={'Authorization': 'Bearer synthetic-token'}, content=body)
    assert rest.status_code == 400
    assert rest.json()['error'] == 'invalid_parameters' and rest.json()['retryable'] is False
    assert 'retry_after_seconds' not in rest.json()
    assert store.calls == []


# spec: ANYM-306
def test_rest_body_nested_beyond_the_parser_recursion_limit_is_invalid_parameters():
    store = RecordingStore()
    with TestClient(create_app(store, ['testserver'])) as client:
        rest = client.post('/v1/upload_file_chunk', headers={'Authorization': 'Bearer synthetic-token'}, content=b'['*12000+b']'*12000)
    assert rest.status_code == 400 and rest.json()['error'] == 'invalid_parameters' and rest.json()['retryable'] is False
    assert store.calls == []


# spec: ANYM-306
def test_decoding_error_raised_by_the_backend_after_parsing_keeps_its_retryable_code():
    class Failing(RecordingStore):
        def search(self, token, **params):
            self.calls.append(('search', params))
            raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'synthetic backend failure')
    store = Failing()
    with TestClient(create_app(store, ['testserver']), raise_server_exceptions=False) as client:
        rest = client.post('/v1/search', headers={'Authorization': 'Bearer synthetic-token'}, json={})
    assert rest.status_code == 503 and rest.json()['error'] == 'temporarily_unavailable' and rest.json()['retryable'] is True
    assert len(store.calls) == 1


@pytest.mark.parametrize('method', METHODS)
def test_operation_arguments_reject_unknown_before_backend(method):
    store = RecordingStore()
    with TestClient(create_app(store, ['testserver'])) as client:
        params = {**arguments(method), 'approved': True}
        rest = client.post('/v1/'+method, headers={'Authorization': 'Bearer synthetic-token'}, json=params)
        mcp = rpc(client, method, params)
    assert rest.status_code == 400 and rest.json()['fields'] == ['approved']
    assert mcp.json()['error']['code'] == -32602
    assert store.calls == []


@pytest.mark.parametrize('method, field', [
    (METHODS[0], 'account'), (METHODS[1], 'account'), (METHODS[1], 'operation'),
    (METHODS[2], 'account'), (METHODS[2], 'operation'), (METHODS[2], 'request_id'),
    (METHODS[3], 'operation_id'),
])
def test_operation_required_fields_rejected_before_backend(method, field):
    store = RecordingStore()
    params = arguments(method)
    del params[field]
    with TestClient(create_app(store, ['testserver'])) as client:
        rest = client.post('/v1/'+method, headers={'Authorization': 'Bearer synthetic-token'}, json=params)
        mcp = rpc(client, method, params)
    assert rest.status_code == 400 and rest.json()['fields'] == [field]
    assert mcp.json()['error']['data']['fields'] == [field]
    assert store.calls == []


@pytest.mark.parametrize('extra', [
    {'account': 1}, {'operation': None}, {'chat_id': False}, {'message_id': 4},
    {'expected_revision': True}, {'expected_revision': '3'}, {'expected_revision': 1.5},
    {'parameters': None}, {'parameters': []}, {'request_id': 123}, {'preview_digest': None},
])
def test_operation_strict_types_before_backend(extra):
    store = RecordingStore()
    params = {**arguments(METHODS[2]), **extra}
    with TestClient(create_app(store, ['testserver'])) as client:
        rest = client.post('/v1/'+METHODS[2], headers={'Authorization': 'Bearer synthetic-token'}, json=params)
        mcp = rpc(client, METHODS[2], params)
    assert rest.status_code == 400 and rest.json()['error'] == 'invalid_parameters'
    assert mcp.json()['error']['code'] == -32602
    assert store.calls == []


def test_operation_default_payload_and_required_action_id():
    store = RecordingStore()
    with TestClient(create_app(store, ['testserver'])) as client:
        minimal = {'account': 'synthetic', 'operation': 'chat_info'}
        assert rpc(client, METHODS[1], minimal).json()['result']['isError'] is False
        denied = rpc(client, METHODS[2], minimal).json()
    assert denied['error']['data']['fields'] == ['request_id']
    assert store.calls == [(METHODS[1], {**minimal, 'chat_id': '', 'message_id': '',
        'expected_revision': 0, 'parameters': {}})]


def test_operation_manifest_has_30_actual_tools_and_truthful_annotations():
    store = RecordingStore()
    with TestClient(create_app(store, ['testserver'])) as client:
        response = client.post('/mcp', headers={'Authorization': 'Bearer synthetic-token',
            'Accept': 'application/json, text/event-stream'}, json={'jsonrpc': '2.0', 'id': 1,
            'method': 'tools/list', 'params': {}})
        initialize = client.post('/mcp', headers={'Authorization': 'Bearer synthetic-token',
            'Accept': 'application/json, text/event-stream'}, json={'jsonrpc': '2.0', 'id': 2,
            'method': 'initialize', 'params': {'protocolVersion': '2025-11-25',
            'capabilities': {}, 'clientInfo': {'name': 'synthetic', 'version': '1'}}})
    tools = {row['name']: row for row in response.json()['result']['tools']}
    assert len(tools) == 52 and set(METHODS) <= set(tools)
    for method in METHODS:
        assert tools[method]['inputSchema']['additionalProperties'] is False
        annotations = tools[method]['annotations']
        execution = method == METHODS[2]
        assert annotations['readOnlyHint'] is not execution
        assert annotations['openWorldHint'] is execution
        assert annotations['destructiveHint'] is execution
    assert tools[METHODS[2]]['annotations']['idempotentHint'] is True
    instructions = initialize.json()['result']['instructions']
    assert 'owner confirmation' in instructions and 'groups' in instructions
    assert 'contacts' in instructions and 'profile' in instructions
    assert 'not human approval' in instructions and 'submitted' in instructions


def test_python_operation_methods_preserve_exact_intent_without_retries():
    captured = []
    def transport(request):
        captured.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={'status': 'submitted', 'operation_id': 'op_synthetic'})
    with Client('https://archive.example/base', 'synthetic-token',
                transport=httpx.MockTransport(transport)) as client:
        for method in METHODS:
            assert getattr(client, method)(**arguments(method))['status'] == 'submitted'
    assert captured == [('/base/v1/'+method, arguments(method)) for method in METHODS]


@pytest.mark.parametrize('method', METHODS)
def test_python_generic_operation_call_forbids_unknown_outer_parameters(method):
    captured = []
    with Client('https://archive.example', 'synthetic-token',
                transport=httpx.MockTransport(lambda request: captured.append(request))) as client:
        with pytest.raises(ValueError, match='invalid_parameters'):
            client.call(method, {**arguments(method), 'approved': True})
    assert captured == []


@pytest.mark.parametrize('extra', [
    {'account': 1}, {'operation': None}, {'chat_id': False}, {'message_id': 4},
    {'expected_revision': True}, {'expected_revision': '3'}, {'expected_revision': -1},
    {'parameters': None}, {'parameters': []}, {'parameters': {1: 'not a string key'}},
    {'request_id': 123}, {'preview_digest': None},
])
def test_python_operations_validate_before_http(extra):
    captured = []
    with Client('https://archive.example', 'synthetic-token',
                transport=httpx.MockTransport(lambda request: captured.append(request))) as client:
        with pytest.raises(ValueError, match='invalid_parameters'):
            client.execute_messenger_operation(**{**arguments(METHODS[2]), **extra})
    assert captured == []


@pytest.mark.parametrize('code', ['unsupported_operation', 'invalid_operation_parameters',
    'operation_disabled', 'sender_not_ready', 'preview_required', 'preview_mismatch',
    'operation_target_changed', 'operation_access_changed', 'operation_result_unknown'])
def test_python_operation_errors_are_safe_and_never_retried(code):
    captured = []
    def transport(request):
        captured.append(request.url.path)
        return httpx.Response(400, json={'error': code, 'detail': 'synthetic-private-content'})
    with Client('https://archive.example', 'synthetic-token',
                transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(AnyMsgError) as failure:
            client.execute_messenger_operation(**arguments(METHODS[2]))
    assert failure.value.code == code and 'synthetic-private-content' not in str(failure.value)
    assert len(captured) == 1


CANARY = 'CANARY_7f3e'


@pytest.fixture
def operations(seeded):
    s, _, _ = seeded
    archive = Archive(s)
    archive.register('fields-telegram', 'telegram', 'Synthetic field errors', discover=True)
    archive.ingest(event(account='fields-telegram', chat='100', provider_id='1',
                         direction='outgoing', body='Synthetic outgoing text'))
    key = s.issue_key('fields-agent', ['fields-telegram'],
                      permissions=['read', 'download', 'send', 'message-edit', 'groups-manage'])
    s.set_messenger_operations('fields-telegram', ['create_group', 'rename_group', 'edit_message'])
    with s.pool.connection() as c:
        chat = c.execute("SELECT id FROM chats WHERE account='fields-telegram'").fetchone()['id']
        message = c.execute("SELECT id,revision FROM messages WHERE account='fields-telegram'").fetchone()
    return s, key, chat, message


def rejected(client, key, params):
    headers = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json, text/event-stream'}
    rest = client.post('/v1/preview_messenger_operation', headers=headers, json=params)
    mcp = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'preview_messenger_operation', 'arguments': params}}).json()['result']
    assert rest.status_code == 400 and mcp['isError']
    body = json.loads(mcp['content'][0]['text'].split(': ', 1)[1])
    assert body['field_errors'] == rest.json()['field_errors']
    assert CANARY not in rest.text and CANARY not in mcp['content'][0]['text']
    return rest.json()


def test_operation_field_error_contact_format(operations):
    s, key, _, _ = operations
    with TestClient(create_app(s, ['testserver'])) as client:
        body = rejected(client, key, {'account': 'fields-telegram', 'operation': 'create_group',
            'parameters': {'title': 'Synthetic group', 'user_ids': ['person_abc' + CANARY]}})
    assert body['error'] == 'invalid_operation_parameters' and body['next_action'] == 'check_parameters'
    assert body['field_errors'] == [{'field': 'user_ids/0', 'reason': 'format',
                                     'expected': parameter_format('telegram', 'contact_id')}]
    assert 'decimal' in body['field_errors'][0]['expected'] and 'person_*' in body['field_errors'][0]['expected']


def test_create_group_preview_from_published_schema(operations):
    s, key, _, _ = operations
    rows = {r['operation']: r for r in s.list_messenger_operations(key, account='fields-telegram')['items']}
    assert rows['create_group']['available'] is True
    schema = rows['create_group']['parameters_schema']
    assert set(schema) == {'title', 'user_ids'} and all(f['required'] for f in schema.values())
    # An agent fills each required field from its published type and format only.
    assert schema['title']['type'] == 'string' and schema['user_ids']['type'] == 'contact_ids'
    assert 'decimal string' in schema['user_ids']['format']
    parameters = {'title': 'Synthetic family group', 'user_ids': ['424242', '515151']}
    preview = s.preview_messenger_operation(key, account='fields-telegram', operation='create_group',
                                            parameters=parameters)
    assert preview['parameters'] == parameters and len(preview['preview_digest']) == 64
    assert preview['owner_approval_verified_by_server'] is False
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n'] == 0


def errors(platform, operation, parameters):
    with pytest.raises(ParameterError, match='^invalid_operation_parameters$') as failure:
        validate_operation(platform, operation, parameters)
    return failure.value.field_errors


def test_operation_field_errors_cover_validators(operations):
    assert errors('telegram', 'create_group', {'user_ids': ['1']}) == [
        {'field': 'title', 'reason': 'required'}]
    assert errors('telegram', 'create_group', {'title': 'x' * 129, 'user_ids': ['1'], 'foo': CANARY}) == [
        {'field': 'title', 'reason': 'max_length', 'expected': 'at most 128 UTF-16 code units; most emoji count as 2'},
        {'field': 'foo', 'reason': 'unknown_field', 'expected': 'only title, user_ids'}]
    assert errors('telegram', 'create_group', {'title': ' ', 'user_ids': ['1', '1']}) == [
        {'field': 'title', 'reason': 'blank', 'expected': 'non-blank text'},
        {'field': 'user_ids', 'reason': 'unique', 'expected': 'no duplicate items'}]
    # A non-identifier key is never returned; the error names the parameters object.
    assert errors('telegram', 'rename_group', {'title': 'ok', CANARY + ' x': 1})[0]['field'] == 'parameters'
    sha = 'a' * 64
    assert errors('telegram', 'send_file', {'sha256': sha}) == [{'field': 'attachment_id', 'reason': 'required',
        'expected': 'exactly one of attachment_id or upload_id'}]
    assert errors('telegram', 'send_file', {'sha256': sha, 'attachment_id': 'att_' + 'b' * 32,
                                            'upload_id': 'up_' + 'c' * 32}) == [
        {'field': 'upload_id', 'reason': 'unique', 'expected': 'exactly one of attachment_id or upload_id'}]
    assert errors('whatsapp', 'send_file', {'sha256': sha, 'attachment_id': 'att_' + 'b' * 32, 'mode': 'video_note'}) == [
        {'field': 'mode', 'reason': 'enum', 'allowed': ['document', 'photo', 'video', 'audio', 'voice', 'sticker']}]
    assert errors('telegram', 'update_profile', {})[0] == {'field': 'parameters', 'reason': 'required',
        'expected': 'at least one of first_name, last_name, bio'}
    assert errors('telegram', 'list_participants', {'limit': 0, 'offset': True}) == [
        {'field': 'limit', 'reason': 'minimum', 'expected': 'at least 1'},
        {'field': 'offset', 'reason': 'type', 'expected': 'integer'}]
    # A huge integer keeps the other field's error; fallback paths appear once.
    assert errors('telegram', 'send_location', {'latitude': 'bad', 'longitude': 10**400}) == [
        {'field': 'latitude', 'reason': 'type', 'expected': 'finite number'},
        {'field': 'longitude', 'reason': 'maximum', 'expected': 'at most 180'}]
    crowded = errors('telegram', 'rename_group', {'title': 'ok', **{'bad-%d' % i: 1 for i in range(25)}, 'foo': 1})
    assert [e['field'] for e in crowded] == ['parameters', 'foo']
    assert errors('telegram', 'get_contact', {'user_id': str(2**63)})[0]['reason'] == 'format'
    assert errors('telegram', 'chat_info', {'seconds': 1}) == [
        {'field': 'seconds', 'reason': 'unknown_field', 'expected': 'no parameters'}]
    # At most 20 entries, in schema order.
    many = errors('telegram', 'create_group', {'user_ids': ['person_%d' % i for i in range(20)]})
    assert len(many) == 20 and many[0]['field'] == 'title' and many[-1]['field'] == 'user_ids/18'
    s, key, chat, message = operations
    with pytest.raises(APIError) as failure:
        s.preview_messenger_operation(key, account='fields-telegram', operation='edit_message', chat_id=chat,
            message_id=message['id'], expected_revision=0, parameters={'text': 'Synthetic edit'})
    assert failure.value.field_errors == [{'field': 'expected_revision', 'reason': 'minimum',
        'expected': 'integer of at least 1: the current revision of the target message'}]
    with pytest.raises(APIError) as failure:
        s.preview_messenger_operation(key, account='fields-telegram', operation='create_group', chat_id=chat,
            parameters={'title': 'Synthetic group', 'user_ids': ['424242']})
    assert failure.value.field_errors == [{'field': 'chat_id', 'reason': 'unknown_field',
        'expected': 'empty for an account-level operation'}]
    with pytest.raises(APIError) as failure:
        s.preview_messenger_operation(key, account='fields-telegram', operation='rename_group', chat_id=chat,
            message_id=message['id'], parameters={'title': 'Synthetic group'})
    assert failure.value.field_errors == [{'field': 'message_id', 'reason': 'unknown_field',
        'expected': 'empty for a chat-level operation'}]
    with pytest.raises(APIError) as failure:
        s.execute_messenger_operation(key, account='fields-telegram', operation='rename_group', chat_id=chat,
            parameters={'title': CANARY * 2000}, request_id='synthetic-fields-0001')
    assert failure.value.field_errors == [{'field': 'parameters', 'reason': 'size',
        'expected': 'canonical request of at most 16000 bytes'}]
