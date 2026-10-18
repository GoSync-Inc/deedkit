"""Messaging rejections name the field, the reason and the allowed values, and never echo input."""
import json

import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.field_errors import MAX_FIELD_ERRORS, field_error, one_per_field
from messaging_service.store import APIError
from tests.test_audit_api import Store
from tests.test_event_delivery import call, identity
from tests.test_subscription_api import rpc

CANARY = 'synthetic-canary-do-not-echo'
HEADERS = {'Authorization': 'Bearer synthetic-token'}
CATEGORIES = ['bug', 'feature', 'quality', 'other']
REF_KINDS = ['message_ids', 'chat_ids', 'subscription_ids']
GOOD = dict(request_id='synthetic-request', category='bug', description='ok')


def rejected(store, token, request_id='synthetic-request', category='bug', description='ok', **changes):
    with pytest.raises(APIError, match='invalid_feedback') as failure:
        call(store, token, 'submit_feedback', request_id, category, description, **changes)
    return failure.value.field_errors


# spec: ANYM-295
def test_field_error_accepts_only_bounded_paths_and_reasons():
    assert field_error('refs/message_ids/0', 'format', 'x') == {'field': 'refs/message_ids/0', 'reason': 'format', 'expected': 'x'}
    assert field_error('user_ids/0', 'format')['field'] == 'user_ids/0'
    for field, reason in (('refs//0', 'format'), ('a' * 129, 'format'), ('naïve', 'format'), ('a-b', 'format'), ('ok', 'invented')):
        with pytest.raises(ValueError, match='invalid_field_error'):
            field_error(field, reason)
    many = [field_error('f%d' % i, 'type') for i in range(30)] + [field_error('f1', 'enum')]
    assert [e['field'] for e in one_per_field(many)] == ['f%d' % i for i in range(MAX_FIELD_ERRORS)]


# spec: ANYM-295
def test_feedback_backend_names_every_failing_field_once(seeded):
    store, _, _ = seeded
    token = identity(store)
    assert rejected(store, token, category='bugs', description='  \n') == [
        {'field': 'category', 'reason': 'enum', 'allowed': CATEGORIES},
        {'field': 'description', 'reason': 'blank', 'expected': 'text with a non-space character'}]
    assert rejected(store, token, request_id='short', description='x' * 4001, operation='nope') == [
        {'field': 'request_id', 'reason': 'min_length', 'expected': '8–128 characters: letters, digits and _ . : -'},
        {'field': 'description', 'reason': 'max_length', 'expected': '1–4000 characters'},
        {'field': 'operation', 'reason': 'enum', 'expected': 'a documented operation name'}]
    assert rejected(store, token, description='')[0]['reason'] == 'min_length'
    assert rejected(store, token, description='bad\x01')[0] == {'field': 'description', 'reason': 'pattern',
        'expected': 'no control characters except newline and tab'}
    assert rejected(store, token, request_id=5) == [{'field': 'request_id', 'reason': 'type', 'expected': 'string'}]
    assert rejected(store, token, operation=5) == [{'field': 'operation', 'reason': 'type', 'expected': 'string'}]


# spec: ANYM-295
def test_feedback_backend_refs_errors_name_the_path(seeded):
    store, _, _ = seeded
    token = identity(store)
    assert rejected(store, token, refs=[1]) == [{'field': 'refs', 'reason': 'type', 'expected': 'object'}]
    assert rejected(store, token, refs={'other_ids': ['x']}) == [{'field': 'refs', 'reason': 'unknown_field',
        'expected': 'only the listed reference types', 'allowed': ['message_ids', 'chat_ids', 'subscription_ids']}]
    assert rejected(store, token, refs={'chat_ids': ['x'] * 33, 'message_ids': 'x', 'subscription_ids': ['', 7, 'a\nb']}) == [
        {'field': 'refs/message_ids', 'reason': 'type', 'expected': 'array'},
        {'field': 'refs/chat_ids', 'reason': 'max_items', 'expected': 'at most 32 items'},
        {'field': 'refs/subscription_ids/0', 'reason': 'min_length', 'expected': '1–256 characters'},
        {'field': 'refs/subscription_ids/1', 'reason': 'type', 'expected': 'string'},
        {'field': 'refs/subscription_ids/2', 'reason': 'pattern', 'expected': 'no control characters'}]


# spec: ANYM-295
def test_feedback_backend_errors_never_echo_input(seeded):
    store, _, _ = seeded
    token = identity(store)
    errors = rejected(store, token, request_id=CANARY[:7], category=CANARY, description=CANARY * 400,
        operation=CANARY, refs={CANARY: [CANARY], 'chat_ids': [CANARY * 20]})
    assert errors and CANARY not in json.dumps(errors)


# spec: ANYM-295
@pytest.mark.parametrize('args,expected', [
    (dict(GOOD, category='bugs'), [{'field': 'category', 'reason': 'enum', 'allowed': CATEGORIES}]),
    (dict(GOOD, request_id='short', description=''), [
        {'field': 'request_id', 'reason': 'min_length', 'expected': '8–128 characters'},
        {'field': 'description', 'reason': 'min_length', 'expected': '1–4000 characters'}]),
    (dict(GOOD, operation='Bad Name'), [{'field': 'operation', 'reason': 'pattern', 'expected': 'matching ^[a-z][a-z0-9_]*$'}]),
    (dict(GOOD, refs={'other_ids': ['x']}), [{'field': 'refs', 'reason': 'unknown_field', 'allowed': REF_KINDS}]),
    (dict(GOOD, refs={'chat_ids': ['x'] * 33}), [{'field': 'refs/chat_ids', 'reason': 'max_items', 'expected': 'at most 32 items'}]),
    (dict(GOOD, refs={'chat_ids': [3]}), [{'field': 'refs/chat_ids/0', 'reason': 'type', 'expected': 'string'}]),
    (dict(GOOD, description='  '), [{'field': 'description', 'reason': 'blank', 'expected': 'text with a non-space character'}]),
    (dict(GOOD, refs={'chat_ids': ['x' * 130] * 32, 'message_ids': ['y' * 130] * 32}),
        [{'field': 'refs', 'reason': 'size', 'expected': 'at most 8 KiB'}]),
    (dict(GOOD, refs={'synthetic-canary': ['\n']}), [{'field': 'refs', 'reason': 'unknown_field', 'allowed': REF_KINDS}]),
    (dict(GOOD, refs={'request_id': ['x']}), [{'field': 'refs', 'reason': 'unknown_field', 'allowed': REF_KINDS}]),
    (dict(GOOD, refs={'subscription_ids': [3], 'message_ids': [4]}), [
        {'field': 'refs/message_ids/0', 'reason': 'type', 'expected': 'string'},
        {'field': 'refs/subscription_ids/0', 'reason': 'type', 'expected': 'string'}]),
    (dict(GOOD, refs={'message_ids': [3], 'subscription_ids': [4]}), [
        {'field': 'refs/message_ids/0', 'reason': 'type', 'expected': 'string'},
        {'field': 'refs/subscription_ids/0', 'reason': 'type', 'expected': 'string'}]),
    ({'category': 'bug'}, [{'field': 'request_id', 'reason': 'required'}, {'field': 'description', 'reason': 'required'}]),
    (dict(GOOD, unexpected=1), [{'field': 'unexpected', 'reason': 'unknown_field'}]),
])
def test_boundary_rejections_carry_field_errors_over_rest_and_mcp(args, expected):
    store = Store()
    with TestClient(create_app(store, ['testserver'])) as client:
        rest = client.post('/v1/submit_feedback', headers=HEADERS, json=args)
        mcp = rpc(client, 'submit_feedback', args)
    assert rest.status_code == 400 and rest.json()['error'] == 'invalid_parameters'
    assert mcp.json()['error']['code'] == -32602
    assert rest.json()['field_errors'] == expected == mcp.json()['error']['data']['field_errors']
    assert rest.json()['fields'] == mcp.json()['error']['data']['fields']
    assert store.calls == []


# spec: ANYM-295
def test_boundary_field_errors_never_echo_input():
    args = dict(request_id=CANARY * 10, category=CANARY, description=CANARY * 400, operation=CANARY,
        refs={CANARY: [CANARY], 'chat_ids': [CANARY] * 40}, **{CANARY: CANARY})
    store = Store()
    with TestClient(create_app(store, ['testserver'])) as client:
        rest = client.post('/v1/submit_feedback', headers=HEADERS, json=args)
        mcp = rpc(client, 'submit_feedback', args)
    assert rest.status_code == 400 and rest.json()['field_errors']
    assert CANARY not in rest.text and CANARY not in mcp.text
    assert rest.json()['field_errors'] == mcp.json()['error']['data']['field_errors']
    assert {e['field'] for e in rest.json()['field_errors']} >= {'category', 'refs', 'arguments'}
    assert store.calls == []


# spec: ANYM-295
def test_integer_map_keys_are_never_named_and_never_raise():
    from messaging_service.field_errors import pydantic_field_errors
    from pydantic import BaseModel, ValidationError

    class Model(BaseModel):
        refs: dict[str, list[str]]
        others: dict[str, int]

    with pytest.raises(ValidationError) as failure:
        Model.model_validate({'refs': {12345: [4]}, 'others': 1}, strict=True)
    assert pydantic_field_errors(Model, failure.value.errors(include_input=False)) == [
        {'field': 'refs', 'reason': 'unknown_field'}, {'field': 'others', 'reason': 'type', 'expected': 'object'}]
