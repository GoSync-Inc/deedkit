"""Android MCP orientation follows the actual caller profile, without narrowing backend compatibility."""

from copy import deepcopy
import json
import uuid

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.catalog import CATALOG
from test_android import setup
from test_android_agent import call
from test_transport import rpc

NATIVE = ['android_capabilities', 'android_session', 'android_agent', 'android_download']


def backend_catalog(tmp_path, *, native=True, scopes=None):
    app, calls = setup(tmp_path, **({'backend_scopes': scopes} if scopes is not None else {}))
    policy = tmp_path / 'policy.json'
    document = json.loads(policy.read_text())
    if native:
        document['profiles']['reader']['tools'] = [*NATIVE, 'platform_search_tools', 'platform_call_tool']
    document['revision'] += 1
    policy.write_text(json.dumps(document))
    schemas = {tool['operation']: deepcopy(tool['inputSchema']) for tool in CATALOG.values() if tool['module'] == 'android'}
    extra = {'input_schemas': schemas, 'adb_commands': ['shell', 'pull'],
             'ui': {'reference_tool': 'android_maestro', 'batch_tool': 'android_run_flow'},
             'maestro': {'removed': True}, 'agent': {'enabled': True, 'input_schema': schemas['agent']}}
    original = app.state.clients.transports['android'].handler
    async def backend(request):
        response = await original(request)
        if request.url.path == '/v1/capabilities':
            return httpx.Response(200, json={**response.json(), **deepcopy(extra)})
        return response
    app.state.clients.transports['android'] = httpx.MockTransport(backend)
    return app, calls, extra


def capabilities(client, route='/mcp'):
    params = {'name': 'android_capabilities', 'arguments': {}}
    if route.endswith('/compact'):
        params = {'name': 'platform_call_tool', 'arguments': params}
    response = client.post(route, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': params},
        headers={'Authorization': 'Bearer gateway-a', 'Accept': 'application/json, text/event-stream'})
    result = response.json()['result']
    assert not result.get('isError'), result
    assert json.loads(result['content'][0]['text']) == result['structuredContent']
    return result['structuredContent']


# spec: ANYM-993, ANYM-951
@pytest.mark.parametrize('route', ['/mcp', '/mcp/compact'])
def test_native_caller_capabilities_do_not_advertise_hidden_courier_or_phone_action_schemas(tmp_path, route):
    app, calls, _ = backend_catalog(tmp_path)
    with TestClient(app) as client:
        result = capabilities(client, route)
        assert set(result['gateway_tools']) == set(NATIVE)
        assert set(result['input_schemas']) == {'capabilities', 'session', 'agent', 'download'}
        assert all(field not in result for field in ('adb_commands', 'ui', 'maestro'))
        assert result['agent']['input_schema'] == CATALOG['android_agent']['inputSchema']
        hidden = rpc(client, 'tools/call', {'name': 'android_courier_chat', 'arguments': {
            'action': 'goal', 'app': 'gojek', 'order': 'original order', 'goal': 'Read its chat',
            'owner_instruction': 'Read this order only'}}).json()['result']
        assert hidden['isError'] and calls == []


# spec: ANYM-951
def test_legacy_authorized_capabilities_and_courier_schema_remain_available(tmp_path):
    app, _, original = backend_catalog(tmp_path, native=False)
    with TestClient(app) as client:
        result = capabilities(client)
        assert result['input_schemas'] == original['input_schemas']
        assert result['adb_commands'] == original['adb_commands'] and result['ui'] == original['ui']
        assert result['maestro'] == original['maestro']
        assert result['input_schemas']['courier_chat']['properties']['app']['enum'] == ['gojek', 'grab']


# spec: ANYM-993, ANYM-951
def test_capabilities_respect_effective_scopes_and_projection_does_not_mutate_cached_backend_schemas(tmp_path):
    app, _, original = backend_catalog(tmp_path, scopes=['android:read'])
    read = app.state.dispatch.full_capabilities
    cached = {}
    async def retained_capabilities(module, credential):
        if module not in cached:
            cached[module] = await read(module, credential)
        return cached[module]
    app.state.dispatch.full_capabilities = retained_capabilities
    with TestClient(app) as client:
        result = capabilities(client)
        assert set(result['input_schemas']) == {'capabilities', 'download'}
        assert 'agent' not in result
        assert cached['android']['input_schemas'] == original['input_schemas']
        path = tmp_path / 'policy.json'
        policy = json.loads(path.read_text())
        policy['revision'] += 1
        policy['profiles']['reader']['tools'].append('android_observe')
        path.write_text(json.dumps(policy))
        result = capabilities(client)
        assert set(result['input_schemas']) == {'capabilities', 'download', 'observe'}
        assert 'agent' not in result
        assert cached['android']['input_schemas'] == original['input_schemas']
        assert cached['android']['ui'] == original['ui']


# spec: ANYM-993, ANYM-997
@pytest.mark.parametrize('goal,words', [
    ('Continue the Halodoc doctor, pharmacy and delivery conversations for this prescription',
     'Doctor asked which medicine I take; I have not supplied that fact. Ask me. Keep prescription R-7 and the approved basket; ask before material changes.'),
    ('Continue the Tokopedia seller conversation before dispatch and follow tracking for order T-8',
     'The seller promised tomorrow dispatch. Ask where order T-8 is, supervise delivery, and tell me when my answer is needed. Do not invent a tracking number.'),
])
def test_order_participant_goals_preserve_original_words_without_legacy_courier_app_gate(tmp_path, goal, words):
    app, calls, _ = backend_catalog(tmp_path)
    request = str(uuid.uuid4())
    with TestClient(app) as client:
        result = call(client, action='start', device_id='phone', session_id='original-lease', request_id=request,
                      goal=goal, owner_instruction=words, params={'order_fact': '  original  ', 'known': False})
        assert not result.get('isError'), result
        module, operation, actual = calls[-1]
        assert module == 'android' and operation == 'agent'
        assert actual['request_id'] == request and actual['session_id'] == 'original-lease'
        assert actual['goal'] == goal and actual['owner_instruction'] == words
        assert actual['params'] == {'order_fact': '  original  ', 'known': False}
        assert actual.get('apps') is None
