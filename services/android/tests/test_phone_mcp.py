"""The local agent's tools (ANYM-971 criterion 3): the stdio server, the per-run bearer, and what each tool may do.
Fake phone, no model; the engine's calls come from a fake binary or straight over loopback."""

import io
import json
import subprocess
import sys
import time

import httpx
import pytest

from agent_harness import PACKAGE, SECRET, TOOLS, Service, fake_engine, pass_probe, set_secret, write_config
from deedkit_android import phone_mcp

CANARY = 'canary-tool-value-7'


def running(tmp_path, steps=None, **options):
    """A service with one live run whose fake engine only sleeps; the tests call its tools with the run's bearer."""
    binary, _ = fake_engine(tmp_path, 'claude', steps or [{'sleep': 30}])
    config = write_config(tmp_path, claude=binary, **options)
    pass_probe(tmp_path, 'claude')
    return config


def refused(reply, code):
    """A refusal inside a tool is an error result the engine reads, not an HTTP failure."""
    assert reply.status_code == 200 and reply.json()['is_error'] is True and 'error: ' + code in reply.json()['text'], reply.text


def live(service, **fields):
    service.session = getattr(service, 'session', None) or service.lease()
    view = service.start(service.session, **fields)
    bearer = next(iter(service.app.state.agents.by_bearer))
    return view['request_id'], bearer


def tool(service, bearer, name, arguments=None):
    return httpx.post(service.url + '/agent-tool/' + name, json=arguments or {}, headers={'Authorization': 'Bearer ' + bearer},
                      timeout=30)


# spec: ANYM-971
def test_the_stdio_server_lists_only_the_phone_tools_and_speaks_the_mcp_basics():
    out = io.StringIO()
    messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}, {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'},
                {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'android_execute', 'arguments': {}}},
                {'jsonrpc': '2.0', 'id': 5, 'method': 'resources/list'}]
    phone_mcp.serve({'url': 'http://127.0.0.1:1', 'bearer': 'x'}, io.StringIO('\n'.join(map(json.dumps, messages)) + '\nnot json\n'), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    by_id = {reply['id']: reply for reply in replies}
    assert by_id[1]['result']['protocolVersion'] == '2025-06-18' and by_id[1]['result']['serverInfo']['name'] == 'phone'
    assert by_id[2]['result'] == {} and 2 in by_id and None in by_id  # the notification got no reply; bad JSON got an error
    names = [item['name'] for item in by_id[3]['result']['tools']]
    assert names == TOOLS and not {'android_execute', 'adb', 'shell', 'upload', 'download', 'file', 'http', 'fetch'} & set(names)
    assert all(item['inputSchema']['type'] == 'object' and item['inputSchema']['additionalProperties'] is False
               for item in by_id[3]['result']['tools'])
    assert by_id[4]['error']['code'] == -32602 and by_id[5]['error']['code'] == -32601


# spec: ANYM-971
def test_a_stdio_call_reaches_the_service_with_the_run_bearer_and_a_dead_bearer_is_an_error_result(tmp_path):
    with Service(running(tmp_path)) as service:
        request_id, bearer = live(service)
        run_file = tmp_path / 'run.json'
        run_file.write_text(json.dumps({'url': service.url, 'bearer': bearer}))
        process = subprocess.Popen([sys.executable, '-I', '-m', 'deedkit_android.phone_mcp', '--run-file', str(run_file)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        call = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'screen', 'arguments': {}}}
        assert process.stdin and process.stdout
        process.stdin.write(json.dumps(call) + '\n')
        process.stdin.flush()
        first = json.loads(process.stdout.readline())['result']
        assert first['isError'] is False and 'package=example.food' in first['content'][0]['text']
        service.post('agent', {'action': 'stop', 'request_id': request_id})
        process.stdin.write(json.dumps({**call, 'id': 2}) + '\n')
        process.stdin.flush()
        second = json.loads(process.stdout.readline())['result']
        assert second['isError'] is True and 'unauthorized' in second['content'][0]['text']
        process.stdin.close()
        assert process.wait(10) == 0
    assert subprocess.run([sys.executable, '-I', '-m', 'deedkit_android.phone_mcp', '--run-file', str(tmp_path / 'none')],
                          capture_output=True).returncode == 2


# spec: ANYM-971
def test_an_unknown_bearer_a_principals_key_and_a_bearer_after_the_run_are_refused(tmp_path):
    with Service(running(tmp_path)) as service:
        request_id, bearer = live(service)
        assert tool(service, bearer, 'screen').status_code == 200
        assert tool(service, 'f' * 64, 'screen').status_code == 401
        assert tool(service, 'one', 'screen').status_code == 401  # a principal's key is not a run bearer
        assert httpx.post(service.url + '/agent-tool/screen', json={}).status_code == 401
        assert service.get(request_id)['state'] == 'running'
        service.post('agent', {'action': 'stop', 'request_id': request_id})
        for name in ('screen', 'finish'):
            assert tool(service, bearer, name).status_code == 401  # the bearer died with the run
        other_id, other = live(service)  # a new run has its own bearer; the old one stays dead
        assert other != bearer and tool(service, bearer, 'screen').status_code == 401
        assert tool(service, other, 'screen').status_code == 200
        service.post('agent', {'action': 'stop', 'request_id': other_id})


# spec: ANYM-971
@pytest.mark.parametrize('name', ['android_execute', 'android_observe', 'adb', 'shell', 'upload', 'download', 'delete_file',
                                  'run', 'fetch', '__guard__', '..%2Fv1%2Fsession'])
def test_a_tool_outside_the_list_is_refused_and_the_device_is_not_touched(tmp_path, name):
    with Service(running(tmp_path)) as service:
        request_id, bearer = live(service)
        before = len(service.phone.commands)
        assert tool(service, bearer, name, {'argv': ['shell', 'id']}).status_code == 404
        assert len(service.phone.commands) == before
        service.post('agent', {'action': 'stop', 'request_id': request_id})


# spec: ANYM-971
@pytest.mark.parametrize('name,arguments', [
    ('tap', {}), ('tap', {'point': '1,2', 'selector': {'text': 'x'}}), ('tap', {'point': 'left'}), ('tap', {'selector': {}}),
    ('type', {'text': ''}), ('key', {'key': 'Power'}), ('swipe', {'direction': 'up', 'start': '1,1', 'end': '2,2'}),
    ('wait', {}), ('wait', {'visible': {'text': 'x'}, 'gone': {'text': 'y'}}), ('open_app', {'package': 'not a package'}),
    ('read_list', {'map': 'only-a-map'}), ('finish', {}), ('screen', {'unknown': 1}),
])
def test_a_malformed_tool_call_is_refused_before_the_phone_is_touched(tmp_path, name, arguments):
    with Service(running(tmp_path)) as service:
        request_id, bearer = live(service)
        before = len(service.phone.commands)
        assert tool(service, bearer, name, arguments).status_code == 422
        assert len(service.phone.commands) == before
        service.post('agent', {'action': 'stop', 'request_id': request_id})


# spec: ANYM-971
def test_open_app_opens_only_the_runs_apps(tmp_path):
    with Service(running(tmp_path)) as service:
        request_id, bearer = live(service, apps=[PACKAGE, 'example.maps'])
        refused(tool(service, bearer, 'open_app', {'package': 'example.bank'}), 'app_not_allowed')
        assert not any('launchApp' in str(command) for command in service.phone.commands)
        assert tool(service, bearer, 'open_app', {'package': 'example.maps'}).status_code == 200
        assert {'launchApp': {'appId': 'example.maps', 'stopApp': False}} in service.phone.commands
        service.post('agent', {'action': 'stop', 'request_id': request_id})


# spec: ANYM-971
def test_every_action_goes_through_the_guard_and_a_secret_is_typed_only_as_a_declared_whole_reference(tmp_path):
    set_secret(tmp_path, CANARY)
    with Service(running(tmp_path)) as service:
        request_id, bearer = live(service, secrets=[SECRET])
        assert tool(service, bearer, 'tap', {'selector': {'text': 'Add to cart'}}).status_code == 200
        assert service.phone.commands[-2:] == [{'tapOn': {'text': 'Add to cart'}}, '__guard__']  # the screen rule after the action
        undeclared = tool(service, bearer, 'type', {'text': '${secret:OTHER_SECRET}'})
        partial = tool(service, bearer, 'type', {'text': 'pass ${secret:%s}' % SECRET})
        refused(undeclared, 'secret_not_declared')
        refused(partial, 'secret_reference_not_allowed')
        assert not any('inputText' in str(command) for command in service.phone.commands)
        typed = tool(service, bearer, 'type', {'text': '${secret:%s}' % SECRET})
        assert typed.status_code == 200 and CANARY not in typed.text
        assert service.phone.commands[-2:] == [{'inputText': '${secret:%s}' % SECRET}, '__guard__']
        after = tool(service, bearer, 'screen', {'image': True})
        assert CANARY not in after.text and 'image withheld' in after.json()['text'] and 'image' not in after.json()
        assert CANARY not in tool(service, bearer, 'read_text', {'selector': {'text': 'x'}}).text
        service.post('agent', {'action': 'stop', 'request_id': request_id})


# spec: ANYM-971
def test_an_action_that_lands_in_an_app_outside_the_run_ends_the_run_at_the_guard(tmp_path):
    with Service(running(tmp_path)) as service:
        request_id, bearer = live(service)
        service.phone.package = 'com.google.android.apps.nexuslauncher'  # home or the system UI is neutral
        assert tool(service, bearer, 'key', {'key': 'Home'}).status_code == 200
        service.phone.package = 'example.bank'  # a tap opened an app the run did not name
        stopped = tool(service, bearer, 'tap', {'selector': {'text': 'Add to cart'}})
        assert stopped.status_code == 200 and stopped.json()['is_error'] and 'STOPPED by the guard (foreign_app)' in stopped.json()['text']
        done = service.finished(request_id)
        assert done['state'] == 'stopped_by_guard' and done['guard']['rule'] == 'foreign_app'
        assert done['guard']['matched'] == 'example.bank' and done['guard']['when'] == 'after' and done['guard']['tool'] == 'tap'


FLOWS = {
    'a scenario step runs': ('- tapOn: Add to cart', None),
    'another app is launched': ('- launchApp: {appId: example.bank}', 'app_not_allowed'),
    'a nested launch of another app': ('- runFlow:\n    commands:\n      - stopApp: example.bank', 'app_not_allowed'),
    'a link into any app': ('- openLink: https://example.test/pay', 'app_not_allowed'),
    'a checkpoint': ('- tapOn: Add to cart\n- checkpoint: here', 'checkpoint_not_supported'),
    'an undeclared secret': ('- inputText: ${secret:OTHER_SECRET}', 'secret_unknown'),
}


# spec: ANYM-971
@pytest.mark.parametrize('name', list(FLOWS))
def test_run_flow_runs_inline_yaml_through_the_flow_engine_within_the_runs_apps_and_secrets(tmp_path, name):
    source, code = FLOWS[name]
    with Service(running(tmp_path)) as service:
        request_id, bearer = live(service, apps=[PACKAGE])
        before = len(service.phone.commands)
        reply = tool(service, bearer, 'run_flow', {'yaml': source})
        if code:
            refused(reply, code)
            assert len(service.phone.commands) == before
        else:
            assert reply.status_code == 200 and json.loads(reply.json()['text'])['steps'] == 1
            assert {'tapOn': 'Add to cart'} in service.phone.commands
            assert service.phone.commands[-1] == '__guard__'
        service.post('agent', {'action': 'stop', 'request_id': request_id})


# spec: ANYM-971
def test_run_flow_needs_exactly_one_of_a_name_or_yaml_and_a_missing_scenario_is_an_error_result(tmp_path):
    with Service(running(tmp_path)) as service:
        request_id, bearer = live(service)
        refused(tool(service, bearer, 'run_flow', {}), 'provide_yaml_or_flow_name')
        refused(tool(service, bearer, 'run_flow', {'yaml': '- back', 'name': 'x'}), 'provide_yaml_or_flow_name')
        assert tool(service, bearer, 'run_flow', {'name': 'no-such-scenario'}).json()['is_error'] is True
        service.post('agent', {'action': 'stop', 'request_id': request_id})


# spec: ANYM-971
def test_a_tool_call_racing_the_end_of_the_run_gets_the_dead_bearer_and_the_lock_serializes(tmp_path):
    with Service(running(tmp_path)) as service:
        service.phone.slow = {'tapOn': 1.5}
        request_id, bearer = live(service)
        replies = []
        import threading
        caller = threading.Thread(target=lambda: replies.append(tool(service, bearer, 'tap', {'point': '5,5'})))
        caller.start()
        time.sleep(0.4)  # the tap is in flight
        assert service.post('agent', {'action': 'stop', 'request_id': request_id})['state'] == 'stopped'
        caller.join(10)
        assert replies and replies[0].status_code == 401
        assert tool(service, bearer, 'tap', {'point': '5,5'}).status_code == 401
        assert not service.app.state.devices.locks['home'].locked()
