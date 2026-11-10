"""Stateless catalog navigation retains normal operation admission."""
import json

from starlette.testclient import TestClient

from test_module_selection import gateway, call, listed  # noqa: F401


# spec: ANYM-952
def test_compact_discovery_is_stateless_for_agents_sharing_a_key(gateway):
    app, _, write, requests = gateway
    write(lambda doc: doc['profiles']['reader']['tools'].extend(['platform_search_tools', 'platform_call_tool']))
    with TestClient(app) as client:
        assert set(listed(client, '/mcp/compact')[0]) == {'platform_capabilities', 'platform_search_tools', 'platform_call_tool'}
        first = call(client, 'platform_search_tools', {'module': 'calendar', 'detail': 'full'}, path='/mcp/compact')['structuredContent']
        second = call(client, 'platform_search_tools', {'module': 'messaging'}, path='/mcp/compact')['structuredContent']
        assert {row['name'] for row in first['tools']} == {'calendar_calendars', 'calendar_slots'}
        assert all('inputSchema' in row for row in first['tools'])
        assert all(row['module'] == 'messaging' for row in second['tools'])
        assert not app.state.dispatch.selections
        assert not call(client, 'platform_call_tool', {'name': 'calendar_calendars'}, path='/mcp/compact').get('isError')
        forbidden = call(client, 'platform_call_tool', {'name': 'messages_send'}, path='/mcp/compact')
        assert forbidden['isError']
        recursive = call(client, 'platform_call_tool', {'name': 'platform_call_tool'}, path='/mcp/compact')
        assert recursive['isError']
        assert requests[-1][1] != '/v1/send_message'


# spec: ANYM-952
def test_compact_search_paginates_and_does_not_reveal_forbidden_tools(gateway):
    app, _, write, _ = gateway
    write(lambda doc: doc['profiles']['reader']['tools'].extend(['platform_search_tools', 'platform_call_tool']))
    with TestClient(app) as client:
        one = call(client, 'platform_search_tools', {'limit': 1}, path='/mcp/compact')['structuredContent']
        two = call(client, 'platform_search_tools', {'limit': 1, 'offset': one['next_offset']}, path='/mcp/compact')['structuredContent']
        assert one['tools'][0]['name'] != two['tools'][0]['name']
        result = call(client, 'platform_search_tools', {'query': 'messages_send', 'detail': 'full'}, path='/mcp/compact')['structuredContent']
        assert result['tools'] == []
        assert 'messages_send' not in json.dumps(result)


# spec: ANYM-952
def test_compact_forwarding_preserves_native_images_and_request_ids(tmp_path):
    from test_android import setup, PNG
    import base64
    import uuid
    app, requests = setup(tmp_path, interrupted=True)
    path = app.state.policy.path
    value = json.loads(path.read_text())
    value['profiles']['reader']['tools'].extend(['platform_search_tools', 'platform_call_tool'])
    value['revision'] += 1
    path.write_text(json.dumps(value))
    from test_transport import rpc
    with TestClient(app) as client:
        result = rpc(client, 'tools/call', {'name': 'platform_call_tool', 'arguments': {
            'name': 'android_observe', 'arguments': {'device_id': 'home', 'session_id': 'session'}}}).json()['result']
        assert result['content'][1]['data'] == base64.b64encode(PNG).decode()
        request_id = str(uuid.uuid4())
        lost = rpc(client, 'tools/call', {'name': 'platform_call_tool', 'arguments': {
            'name': 'android_execute', 'arguments': {'device_id': 'home', 'session_id': 'session',
            'request_id': request_id, 'argv': ['shell', 'input', 'keyevent', 'HOME']}}}).json()['result']
        assert lost['isError']
        assert lost['structuredContent']['request_id'] == request_id
        assert lost['structuredContent']['error'] == 'mutation_outcome_unknown'
        assert sum(row[1] == 'execute' for row in requests) == 1
