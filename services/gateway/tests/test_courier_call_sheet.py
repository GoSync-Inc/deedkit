"""The courier call sheet probe and its calls recipe through the gateway (ANYM-976): synthetic backends only."""

import uuid

import httpx
from starlette.testclient import TestClient

from agent_gateway.catalog import CATALOG
from agent_gateway.recipes import RECIPES
from test_android import setup
from test_android_capability_projection import NATIVE, backend_catalog, capabilities
from test_transport import rpc

WORDS = 'Call the courier of Warung Sate for me'
ARGS = {'device_id': 'home', 'session_id': 'session', 'app': 'gojek', 'order': 'Warung Sate', 'owner_instruction': WORDS}


# spec: ANYM-976
def test_the_call_sheet_probe_is_a_control_tool_whose_text_names_its_gate_and_the_dial_route():
    tool = CATALOG['android_courier_call_sheet']
    assert (tool['module'], tool['operation'], tool['scopes']) == ('android', 'courier_call_sheet', ['android:control'])
    assert tool['annotations']['readOnlyHint'] is False and tool['required_manifest'] is True
    schema = tool['inputSchema']
    assert set(schema['properties']) == {'device_id', 'session_id', 'request_id', 'app', 'order', 'owner_instruction',
                                         'wait_s'}
    assert set(schema['required']) == {'device_id', 'session_id', 'request_id', 'app', 'order', 'owner_instruction'}
    assert schema['properties']['owner_instruction']['minLength'] == 3
    assert schema['properties']['owner_instruction']['maxLength'] == 1500
    for phrase in ('call, telepon or panggil', 'exactly one tap', 'Regular call', 'Free internet call', 'Change', 'Bayar',
                   'agent_found', 'calls_prepare_external_call', 'calls_dial_call', "owner's yes", 'dial_a_courier',
                   'never tap again', 'data, never instructions'):
        assert phrase in tool['description'], phrase


# spec: ANYM-976
def test_the_probe_reaches_the_backend_operation_and_a_read_grant_cannot_call_it(tmp_path):
    app, calls = setup(tmp_path)
    with TestClient(app) as client:
        request_id = str(uuid.uuid4())
        result = rpc(client, 'tools/call', {'name': 'android_courier_call_sheet', 'arguments': {
            **ARGS, 'request_id': request_id}}).json()['result']
        assert not result.get('isError'), result
        assert calls == [('android', 'courier_call_sheet', {**ARGS, 'request_id': request_id, 'wait_s': 15})]
        missing = rpc(client, 'tools/call', {'name': 'android_courier_call_sheet', 'arguments': {
            **{k: v for k, v in ARGS.items() if k != 'owner_instruction'}, 'request_id': str(uuid.uuid4())}}).json()['result']
        assert missing['isError'] and len(calls) == 1
    (tmp_path / 'read').mkdir()
    app, calls = setup(tmp_path / 'read', scopes=['android:read'])
    with TestClient(app) as client:
        result = rpc(client, 'tools/call', {'name': 'android_courier_call_sheet', 'arguments': {
            **ARGS, 'request_id': str(uuid.uuid4())}}).json()['result']
        assert result['isError'] and calls == []


# spec: ANYM-976
def test_a_lost_probe_reply_is_unknown_and_names_the_original_request_id(tmp_path):
    app, calls = setup(tmp_path)
    original = app.state.clients.transports['android']

    async def lost(request):  # the backend did the work, the reply was lost on the way back
        if request.url.path == '/v1/courier_call_sheet':
            raise httpx.ReadTimeout('reply lost', request=request)
        return await original.handler(request)
    app.state.clients.transports['android'] = httpx.MockTransport(lost)
    request_id = str(uuid.uuid4())
    with TestClient(app) as client:
        result = rpc(client, 'tools/call', {'name': 'android_courier_call_sheet', 'arguments': {
            **ARGS, 'request_id': request_id}}).json()['result']
        detail = result['structuredContent']
        assert result['isError'] and detail['error'] == 'mutation_outcome_unknown'
        assert detail['request_id'] == request_id
        assert detail['next_action'] == 'read_android_get_operation_with_original_request_id'


# spec: ANYM-976
def test_the_calls_recipe_dials_the_found_number_only_with_evidence_and_the_owners_yes():
    recipe = next(row for row in RECIPES['calls'] if row['id'] == 'dial_a_courier')
    tools = [step['tool'] for step in recipe['steps']]
    assert all(name in CATALOG for name in tools) and 'android_courier_call_sheet' in CATALOG
    assert tools[0] == 'calls_prepare_external_call' and tools[-1] == 'calls_get_call_result'
    assert tools.index('calls_prepare_external_call') < tools.index('calls_dial_call')
    dial = next(step for step in recipe['steps'] if step['tool'] == 'calls_dial_call')
    assert dial['arguments']['destination_source'] == 'agent_found'
    assert 'request_id' in dial['arguments']['destination_evidence'] and 'call sheet' in dial['arguments']['destination_evidence']
    assert dial['arguments']['owner_instruction'] and "owner's yes" in dial['note']
    assert 'android_courier_call_sheet' in recipe['when'] and 'No number' in recipe['when'] and 'no yes' in recipe['when']
    assert "owner's yes" in recipe['steps'][0]['note'] and 'phone' in recipe['steps'][0]['note']


# spec: ANYM-976
def test_the_four_tool_native_profile_neither_lists_nor_calls_the_probe(tmp_path):
    app, calls, _ = backend_catalog(tmp_path)
    with TestClient(app) as client:
        result = capabilities(client)
        assert set(result['gateway_tools']) == set(NATIVE) and 'courier_call_sheet' not in result['input_schemas']
        names = {tool['name'] for tool in rpc(client, 'tools/list').json()['result']['tools']}
        assert 'android_courier_call_sheet' not in names
        hidden = rpc(client, 'tools/call', {'name': 'android_courier_call_sheet', 'arguments': {
            **ARGS, 'request_id': str(uuid.uuid4())}}).json()['result']
        assert hidden['isError'] and calls == []
