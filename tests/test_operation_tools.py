"""Risk-class operation tools (ANYM-549): the operation assignment, preview request_id and execute wait_s.

Synthetic only; no database and no provider."""
import json
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from anymsg.client import Client
from messaging_service.app import create_app
from messaging_service.store import APIError
from messaging_service.messenger_operations import (ADVANCED_ONLY, LOCAL_WRITE_OPERATIONS, OPERATION_TOOLS,
                                                     OPERATIONS)
from test_operations_api import RecordingStore, arguments, rpc

CATALOG = Path(__file__).resolve().parents[1] / 'services/gateway/src/agent_gateway/catalog.json'
HEADERS = {'Authorization': 'Bearer synthetic-token'}


def catalog_tools():
    value = json.loads(CATALOG.read_text())
    tools = value['tools'] if isinstance(value, dict) else value
    return {t['operation']: t for t in tools if t.get('routes_actions')}


class WaitingStore(RecordingStore):
    """execute queues; the receipt is sent on the second status read."""
    def __init__(self):
        super().__init__()
        self.reads = 0
    def get_messenger_operation(self, token, operation_id):
        self.reads += 1
        return {'operation_id': operation_id, 'status': 'queued' if self.reads < 2 else 'sent'}


# spec: ANYM-549
def test_every_operation_has_exactly_one_route_and_every_advanced_one_a_reason():
    routed = [op for ops in OPERATION_TOOLS.values() for op in ops]
    assert len(routed) == len(set(routed))
    assert set(routed).isdisjoint(ADVANCED_ONLY)
    assert set(routed) | set(ADVANCED_ONLY) == set(OPERATIONS)
    assert all(isinstance(reason, str) and reason.strip() for reason in ADVANCED_ONLY.values())


# spec: ANYM-549
def test_a_tool_never_mixes_a_read_with_an_external_effect():
    assert all(not OPERATIONS[op]['mutation'] for op in OPERATION_TOOLS['read_contacts'])
    for tool in ('change_message', 'manage_chat', 'change_contacts'):
        assert all(OPERATIONS[op]['mutation'] or op in LOCAL_WRITE_OPERATIONS for op in OPERATION_TOOLS[tool]), tool
    assert LOCAL_WRITE_OPERATIONS <= set(OPERATION_TOOLS['manage_chat'])


# spec: ANYM-549
def test_gateway_catalog_routes_exactly_the_backend_assignment_with_matching_annotations():
    tools = catalog_tools()
    assert set(tools) == set(OPERATION_TOOLS)
    for name, operations in OPERATION_TOOLS.items():
        tool = tools[name]
        assert tool['name'] == 'messages_' + name and tool['action_group'] == 'messenger_operations'
        assert sorted(tool['routes_actions']) == sorted(operations)
        assert sorted(tool['inputSchema']['properties']['operation']['enum']) == sorted(operations)
        read = name == 'read_contacts'
        assert tool['annotations']['readOnlyHint'] is read and tool['annotations']['destructiveHint'] is not read
        assert tool['annotations']['openWorldHint'] is True


# spec: ANYM-549
def test_preview_accepts_and_ignores_request_id_in_rest_and_mcp():
    store = RecordingStore()
    params = arguments('preview_messenger_operation')
    with TestClient(create_app(store, ['testserver'])) as client:
        rest = client.post('/v1/preview_messenger_operation', headers=HEADERS,
                           json={**params, 'request_id': 'synthetic-action'})
        mcp = rpc(client, 'preview_messenger_operation', {**params, 'request_id': 'synthetic-action'})
    assert rest.status_code == 200 and not mcp.json()['result'].get('isError')
    assert store.calls == [('preview_messenger_operation', params)] * 2


# spec: ANYM-549
@pytest.mark.parametrize('transport', ['rest', 'mcp'])
def test_execute_waits_for_its_receipt_and_never_queues_twice(transport):
    store = WaitingStore()
    params = arguments('execute_messenger_operation')
    with TestClient(create_app(store, ['testserver'])) as client:
        if transport == 'rest':
            response = client.post('/v1/execute_messenger_operation', headers=HEADERS, json={**params, 'wait_s': 5})
            assert response.status_code == 200
            result = response.json()
        else:
            result = rpc(client, 'execute_messenger_operation', {**params, 'wait_s': 5}).json()['result']
            assert not result.get('isError')
            result = result['structuredContent']
    assert store.calls == [('execute_messenger_operation', params)]
    assert result['status'] == 'sent' and result['wait_timed_out'] is False and store.reads == 2


# spec: ANYM-549
@pytest.mark.parametrize('wait_s', [-1, 26, '5', True])
def test_an_invalid_execute_wait_never_queues(wait_s):
    store = WaitingStore()
    with TestClient(create_app(store, ['testserver'])) as client:
        response = client.post('/v1/execute_messenger_operation', headers=HEADERS,
                               json={**arguments('execute_messenger_operation'), 'wait_s': wait_s})
    assert response.status_code >= 400 and store.calls == []


# spec: ANYM-549
def test_python_sdk_sends_preview_request_id_and_execute_wait_only_when_given():
    sent = []

    def handler(request):
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={'status': 'queued'})

    client = Client('http://127.0.0.1', 'synthetic-token', transport=httpx.MockTransport(handler))
    base = {k: v for k, v in arguments('preview_messenger_operation').items()}
    execute = arguments('execute_messenger_operation')
    client.preview_messenger_operation(**base)
    client.preview_messenger_operation(**base, request_id='synthetic-action')
    client.execute_messenger_operation(**execute)
    client.execute_messenger_operation(**execute, wait_s=5)
    client.call('execute_messenger_operation', {**execute, 'wait_s': 0})
    assert [body for _, body in sent] == [base, {**base, 'request_id': 'synthetic-action'}, execute,
                                          {**execute, 'wait_s': 5}, execute]
    for wait_s in ('5', -1, 26, float('nan'), True):
        with pytest.raises(ValueError):
            client.execute_messenger_operation(**execute, wait_s=wait_s)


class FailingWaitStore(RecordingStore):
    """execute queues; every status read fails with the given error."""
    def __init__(self, error):
        super().__init__()
        self.error = error
    def get_messenger_operation(self, token, operation_id):
        raise self.error


# spec: ANYM-549
@pytest.mark.parametrize('transport', ['rest', 'mcp'])
@pytest.mark.parametrize('error,code', [(APIError('query_timeout', 503), 'query_timeout'),
                                        (APIError('service_unavailable', 503), 'service_unavailable'),
                                        (RuntimeError('synthetic pool timeout'), 'status_unavailable')])
def test_a_failed_wait_keeps_the_queued_receipt(transport, error, code):
    store = FailingWaitStore(error)
    params = arguments('execute_messenger_operation')
    with TestClient(create_app(store, ['testserver'])) as client:
        if transport == 'rest':
            response = client.post('/v1/execute_messenger_operation', headers=HEADERS, json={**params, 'wait_s': 5})
            assert response.status_code == 200
            result = response.json()
        else:
            result = rpc(client, 'execute_messenger_operation', {**params, 'wait_s': 5}).json()['result']
            assert not result.get('isError')
            result = result['structuredContent']
    assert store.calls == [('execute_messenger_operation', params)]
    assert result['operation_id'] == 'op_synthetic' and result['status'] == 'queued'
    assert result['wait_timed_out'] is True and result['wait_error'] == code


# spec: ANYM-549
def test_a_wait_refused_by_authorization_stays_refused():
    store = FailingWaitStore(APIError('forbidden', 403))
    with TestClient(create_app(store, ['testserver'])) as client:
        response = client.post('/v1/execute_messenger_operation', headers=HEADERS,
                               json={**arguments('execute_messenger_operation'), 'wait_s': 5})
    assert response.status_code == 403 and len(store.calls) == 1


# spec: ANYM-549
@pytest.mark.parametrize('wait_s', [None, False, '', [], {}])
def test_python_sdk_rejects_a_falsy_invalid_wait_without_a_request(wait_s):
    sent = []
    client = Client('http://127.0.0.1', 'synthetic-token',
                    transport=httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200, json={})))
    with pytest.raises(ValueError):
        client.execute_messenger_operation(**arguments('execute_messenger_operation'), wait_s=wait_s)
    assert sent == []
