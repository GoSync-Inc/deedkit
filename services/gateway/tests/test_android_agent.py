"""The android_agent tool: a local phone-agent run started, followed by long-poll and stopped through one MCP tool."""

import uuid

from starlette.testclient import TestClient

from agent_gateway.catalog import CATALOG
from agent_gateway.http_clients import WAIT_ADMISSION
from test_android import setup
from test_transport import rpc

TOOL = CATALOG.get('android_agent', {})
START = {'action': 'start', 'device_id': 'home', 'session_id': 'session', 'engine': 'codex', 'apps': ['example.food'],
         'goal': 'Find the shop, add the item and read the basket total.', 'owner_instruction': 'Order check for me',
         'params': {'item': 'Latte'}, 'outputs': ['total'], 'max_minutes': 3}


def call(client, **arguments):
    return rpc(client, 'tools/call', {'name': 'android_agent', 'arguments': arguments}).json()['result']


# spec: ANYM-972
def test_the_android_agent_tool_is_a_control_tool_whose_schema_is_the_services():
    assert TOOL['module'] == 'android' and TOOL['operation'] == 'agent' and TOOL['scopes'] == ['android:control']
    assert TOOL['required_manifest'] is True and TOOL['annotations']['readOnlyHint'] is False
    properties = TOOL['inputSchema']['properties']
    assert set(properties['action']['enum']) == {'start', 'get', 'stop', 'reply', 'steer'}
    assert {value for choice in properties['engine']['anyOf'] for value in choice.get('enum', [])} == {'claude', 'codex'}
    assert properties['wait_s']['maximum'] == 25  # under the gateway client's 35 s timeout
    for phrase in ('request_id', 'wait_s', 'stopped_by_guard', 'data, never instructions'):
        assert phrase in TOOL['description']
    assert ('android', 'agent') in WAIT_ADMISSION and WAIT_ADMISSION[('android', 'agent')][0] == 'waits'


# spec: ANYM-972
def test_start_and_a_get_long_poll_reach_the_backend_with_their_arguments(tmp_path):
    app, calls = setup(tmp_path)
    request_id = str(uuid.uuid4())
    with TestClient(app) as client:
        started = call(client, **START, request_id=request_id)
        assert not started.get('isError'), started
        module, operation, forwarded = calls[-1]
        assert (module, operation) == ('android', 'agent')
        assert {**START, 'request_id': request_id}.items() <= forwarded.items()
        polled = call(client, action='get', request_id=request_id, after=3, wait_s=25)
        assert not polled.get('isError'), polled
        assert calls[-1][2] == {'action': 'get', 'request_id': request_id, 'after': 3, 'wait_s': 25}
        stopped = call(client, action='stop', request_id=request_id)
        assert not stopped.get('isError') and calls[-1][2]['action'] == 'stop'
        too_long = call(client, action='get', request_id=request_id, wait_s=26)
        assert too_long['isError'] and len(calls) == 3  # refused at the gateway


# spec: ANYM-972
def test_an_uncertain_start_is_read_back_by_its_request_id_and_never_resent(tmp_path):
    app, calls = setup(tmp_path, interrupted=True)
    request_id = str(uuid.uuid4())
    with TestClient(app) as client:
        result = call(client, **START, request_id=request_id)
        detail = result['structuredContent']
        assert result['isError'] and detail['error'] == 'mutation_outcome_unknown' and detail['request_id'] == request_id
        assert detail['next_action'] == 'read_android_agent_get_with_original_request_id'
        assert len(calls) == 1  # no resend
        (tmp_path / 'again').mkdir()
        app2, calls2 = setup(tmp_path / 'again')
        with TestClient(app2) as again:
            read = call(again, action='get', request_id=request_id)
            assert not read.get('isError') and calls2[-1][2]['request_id'] == request_id and len(calls2) == 1


# spec: ANYM-972
def test_a_principal_without_the_control_scope_does_not_see_or_run_the_tool(tmp_path):
    app, calls = setup(tmp_path, scopes=['android:read'])
    with TestClient(app) as client:
        names = {tool['name'] for tool in rpc(client, 'tools/list').json()['result']['tools']}
        assert 'android_observe' in names and 'android_agent' not in names
        with_control = setup(tmp_path / 'control')[0] if (tmp_path / 'control').mkdir() is None else None
        with TestClient(with_control) as granted:
            assert 'android_agent' in {tool['name'] for tool in rpc(granted, 'tools/list').json()['result']['tools']}
        result = call(client, **START, request_id=str(uuid.uuid4()))
        assert result['isError'] and calls == []
