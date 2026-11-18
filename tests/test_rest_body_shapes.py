"""ANYM-306: a REST body that is not one JSON object is refused as invalid_parameters (400, not retryable) and starts
no operation, for a read and for a state-changing call; a well-formed object still reaches the operation. Synthetic
store and local TestClient only."""
import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app

AUTH = {'Authorization': 'Bearer synthetic-token'}
VALID = {'search': {'query': 'passport'},
         'ack_updates': {'consumer': 'synthetic', 'receipt': 'synthetic-receipt', 'expected_version': 1}}
MALFORMED = [b'', b'{', b'{"query": "x",}', b"{'query': 'x'}", b'{"query": "x"', b'{"query": "x"}}', b'query=x',
             b'\xff', b'{"query": "\xc3\x28"}']
NOT_OBJECT = [b'[]', b'[{"query": "x"}]', b'"query"', b'1', b'1.5', b'null', b'true']


class Store:
    """Answers each business call and records it; authentication and audit as the real app expects them."""
    public_url = 'http://testserver'

    def __init__(self):
        self.calls = []

    def authenticate(self, token):
        return 'synthetic-principal'

    def permits(self, token, operation):
        return True

    def audit_boundary(self, code):
        pass

    def search(self, token, **params):
        self.calls.append(('search', params))
        return {'items': [], 'next_cursor': None, 'search': {}}

    def ack_updates(self, token, **params):
        self.calls.append(('ack_updates', params))
        return {'acked': True}


def post(method, content=None, json=None):
    store = Store()
    with TestClient(create_app(store, ['testserver']), raise_server_exceptions=False) as client:
        response = client.post('/v1/' + method, headers=AUTH, content=content, json=json)
    return response, store.calls


# spec: ANYM-306
@pytest.mark.parametrize('method', sorted(VALID))
def test_a_well_formed_object_body_reaches_the_operation(method):
    response, calls = post(method, json=VALID[method])
    assert response.status_code == 200 and [name for name, _ in calls] == [method]


# spec: ANYM-306
@pytest.mark.parametrize('body', MALFORMED + NOT_OBJECT,
                         ids=[f'malformed-{n}' for n in range(len(MALFORMED))] + [f'not-object-{n}' for n in range(len(NOT_OBJECT))])
@pytest.mark.parametrize('method', sorted(VALID))
def test_a_body_that_is_not_one_json_object_is_invalid_parameters_and_starts_nothing(method, body):
    response, calls = post(method, content=body)
    assert response.status_code == 400, response.text
    assert response.json()['error'] == 'invalid_parameters' and response.json()['retryable'] is False
    assert 'retry_after_seconds' not in response.json()
    assert calls == []
