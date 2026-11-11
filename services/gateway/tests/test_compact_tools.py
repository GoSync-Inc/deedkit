"""Stateless catalog navigation retains normal operation admission."""
import json

from starlette.testclient import TestClient

from test_module_selection import call, listed

pytest_plugins = ("test_module_selection",)


# spec: ANYM-952
def test_public_tool_adds_gateway_guidance_without_changing_backend_metadata():
    from agent_gateway.catalog import CATALOG, public

    tool = dict(CATALOG['calendar_calendars'], guidance='Reuse the returned calendar_ref.')
    original = tool['description']
    published = public(tool)
    assert published['description'] == original + ' ' + tool['guidance']
    assert tool['description'] == original
    assert 'guidance' not in published


# spec: ANYM-952
def test_every_module_has_entry_guidance_and_valid_example_arguments():
    from agent_gateway.catalog import CATALOG, arguments, public

    entry_points = {
        'messaging': ('messages_capabilities', 'messages_list_chats'),
        'calendar': ('calendar_capabilities', 'calendar_calendars'),
        'calls': ('calls_capabilities', 'calls_call'),
        'recordings': ('recordings_capabilities', 'recordings_list'),
        'agents': ('agents_capabilities', 'agents_start_task'),
        'android': ('android_capabilities', 'android_session'),
        'gateway': ('platform_capabilities', 'platform_search_tools'),
    }
    assert set(entry_points) == {tool['module'] for tool in CATALOG.values()}
    for entry, next_tool in entry_points.values():
        assert next_tool in public(CATALOG[entry])['description']
    for name, tool in CATALOG.items():
        _, marker, example = tool['description'].partition('Example: ')
        if marker:
            values = json.JSONDecoder().raw_decode(example)[0]
            arguments(tool, values)


# spec: ANYM-952
def test_compact_descriptions_guide_a_runnable_first_call(tmp_path):
    from test_android import setup
    from test_module_selection import mcp

    app, _ = setup(tmp_path)
    path = app.state.policy.path
    policy = json.loads(path.read_text())
    policy['profiles']['reader']['tools'].extend(['platform_search_tools', 'platform_call_tool'])
    policy['revision'] += 1
    path.write_text(json.dumps(policy))
    with TestClient(app) as client:
        initialized = mcp(client, 'initialize', {
            'protocolVersion': '2025-06-18', 'capabilities': {},
            'clientInfo': {'name': 'synthetic-agent', 'version': '1'},
        }, path='/mcp/compact').json()
        assert 'result' in initialized, initialized
        instructions = initialized['result']['instructions']
        for name in ('platform_capabilities', 'platform_search_tools', 'platform_call_tool'):
            assert name in instructions
        tools = {tool['name']: tool for tool in mcp(client, 'tools/list', path='/mcp/compact').json()['result']['tools']}
        capabilities = tools['platform_capabilities']['description']
        assert 'modules_available' in capabilities
        assert 'platform_search_tools' in capabilities
        assert 'platform_call_tool' in capabilities
        assert 'next_offset' in tools['platform_search_tools']['description']
        assert 'inputSchema' in tools['platform_call_tool']['description']
        examples = {name: json.JSONDecoder().raw_decode(tool['description'].partition('Example: ')[2])[0]
                    for name, tool in tools.items()}
        start = call(client, 'platform_capabilities', examples['platform_capabilities'], path='/mcp/compact')
        assert not start.get('isError'), start
        assert 'android' in {module['id'] for module in start['structuredContent']['modules_available']}
        search = call(client, 'platform_search_tools', examples['platform_search_tools'], path='/mcp/compact')
        assert not search.get('isError'), search
        target = examples['platform_call_tool']['name']
        schema = next(tool['inputSchema'] for tool in search['structuredContent']['tools'] if tool['name'] == target)
        assert schema['type'] == 'object'
        result = call(client, 'platform_call_tool', examples['platform_call_tool'], path='/mcp/compact')
        assert not result.get('isError'), result
        assert target in result['structuredContent']['gateway_tools']
        section = call(client, 'platform_search_tools', {'module': 'android', 'detail': 'full'},
                       path='/mcp/compact')['structuredContent']
        descriptions = {tool['name']: tool['description'] for tool in section['tools']}
        assert 'android_session' in descriptions['android_capabilities']
        assert 'android_observe' in descriptions['android_capabilities']
        assert 'android_maestro' in descriptions['android_run_flow']
        assert 'android_run_flow' in descriptions['android_maestro']
        assert 'android_download' in descriptions['android_observe']


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
def test_exact_tool_search_does_not_return_tools_that_only_reference_it(gateway):
    app, _, write, _ = gateway
    write(lambda doc: doc['profiles']['reader']['tools'].extend([
        'platform_search_tools', 'platform_call_tool', 'calendar_capabilities']))
    with TestClient(app) as client:
        result = call(client, 'platform_search_tools', {'query': ' CALENDAR_CALENDARS ', 'detail': 'full'},
                      path='/mcp/compact')['structuredContent']
        assert [tool['name'] for tool in result['tools']] == ['calendar_calendars']


def routed_search(query, actions, visible=None):
    from agent_gateway.catalog import CATALOG
    from agent_gateway.catalog_search import search_tools

    details = {'actions': actions, 'unavailable_modules': []}
    names = list(CATALOG) if visible is None else visible
    return search_tools(names, details, {'query': query, 'offset': 0, 'limit': 10, 'detail': 'compact'})['tools']


# spec: ANYM-982
def test_search_finds_the_button_operations_a_key_may_use_through_their_routed_tools():
    granted = ['inspect_message_buttons', 'press_button']
    for query, expected in (('inspect buttons', ['inspect_message_buttons']),
                            ('inline keyboard', granted), ('bot keyboard', granted), ('reply_markup', granted)):
        rows = routed_search(query, granted)
        assert 'messages_execute_messenger_operation' in [row['name'] for row in rows[:3]], query
        row = next(row for row in rows if row['name'] == 'messages_execute_messenger_operation')
        assert row['operations'] == expected, query
    rows = routed_search('press inline button', granted)
    assert {'messages_change_message', 'messages_execute_messenger_operation'} <= {row['name'] for row in rows[:3]}
    assert next(row for row in rows if row['name'] == 'messages_change_message')['operations'] == ['press_button']
    # A row that matched on its own description names no operations.
    assert all('operations' not in row for row in routed_search('buttons', []))


# spec: ANYM-982
def test_search_by_operation_never_widens_what_a_key_can_see_or_do():
    from agent_gateway.catalog import CATALOG

    # The operation is not among the key's actions: the routed tool does not answer for those words.
    for query in ('inspect buttons', 'inline keyboard', 'reply_markup'):
        names = [row['name'] for row in routed_search(query, ['rename_group'])]
        assert 'messages_execute_messenger_operation' not in names, query
    # The tool is not visible to the key: no row, whatever its actions say.
    visible = [name for name in CATALOG if name != 'messages_execute_messenger_operation']
    granted = ['inspect_message_buttons', 'press_button']
    assert 'messages_execute_messenger_operation' not in [r['name'] for r in routed_search('inspect buttons', granted, visible)]
    # A routed risk-class tool answers only for the operations it routes: press_button, not inspect_message_buttons.
    rows = routed_search('inline keyboard', granted)
    assert next(row for row in rows if row['name'] == 'messages_change_message')['operations'] == ['press_button']
    # Words must all match one operation: a word of another operation does not complete a match.
    assert 'messages_execute_messenger_operation' not in [r['name'] for r in routed_search('inspect press', granted)]


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


# spec: ANYM-952
def test_maestro_discovery_and_forwarding_require_current_backend_manifest(tmp_path):
    import httpx
    from test_android import setup
    from test_transport import rpc
    app, calls = setup(tmp_path)
    path = app.state.policy.path
    policy = json.loads(path.read_text())
    policy['profiles']['reader']['tools'].extend(['platform_search_tools', 'platform_call_tool'])
    policy['revision'] += 1
    path.write_text(json.dumps(policy))
    original = app.state.clients.transports['android']

    async def old_backend(request):
        response = await original.handle_async_request(request)
        await response.aread()
        if request.url.path == '/v1/capabilities':
            body = json.loads(response.content)
            body['tool_manifest']['names'].remove('maestro')
            return httpx.Response(200, json=body)
        return response

    app.state.clients.transports['android'] = httpx.MockTransport(old_backend)
    with TestClient(app) as client:
        names = {tool['name'] for tool in rpc(client, 'tools/list').json()['result']['tools']}
        assert 'android_maestro' not in names
        result = call(client, 'platform_search_tools', {'query': 'android_maestro'}, path='/mcp/compact')
        assert result['structuredContent']['tools'] == []
        for name, arguments in [('android_maestro', {}),
                                ('platform_call_tool', {'name': 'android_maestro', 'arguments': {}})]:
            result = call(client, name, arguments, path='/mcp/compact')
            assert result['isError']
            assert result['structuredContent']['error'] == 'operation_not_in_module_build'
        assert calls == []
