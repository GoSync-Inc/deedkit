"""Optional Android module discovery, native images, and command outcome recovery."""

import base64
import hashlib
import json
import uuid

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG
from test_policy import config
from test_transport import rpc

TOOLS = [name for name, tool in CATALOG.items() if tool['module'] == 'android']
SCOPES = ['android:read', 'android:control']
PNG = b'\x89PNG\r\n\x1a\nsynthetic'


def setup(tmp_path, scopes=SCOPES, backend_scopes=SCOPES, unavailable=False, interrupted=False,
          image=None):
    value = config()
    value['deployment'] = {'modules': ['calendar', 'android'], 'backends': {
        'calendar': {'url': 'http://calendar'}, 'android': {'url': 'http://android'}}}
    value['profiles']['reader']['tools'] = ['platform_capabilities', 'calendar_slots', *TOOLS]
    actor = value['principals'][0]
    actor['grants']['android'] = list(scopes)
    for module in ('calendar', 'android'):
        path = tmp_path / (module + '.key')
        path.write_text('backend-' + module)
        path.chmod(0o600)
        actor['credentials'][module] = {'key_file': str(path),
                                        'key_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps(value))
    policy.chmod(0o600)
    calls = []

    async def backend(request):
        module = request.url.host
        assert request.headers['authorization'] == 'Bearer backend-' + module
        if module == 'android' and unavailable:
            raise httpx.ConnectError('unavailable', request=request)
        operation = request.url.path.removeprefix('/v1/')
        if operation == 'capabilities':
            permissions = backend_scopes if module == 'android' else ['availability:read']
            return httpx.Response(200, json={'permissions': permissions, 'tool_manifest': {
                'version': 1, 'names': [CATALOG[name]['operation'] for name in TOOLS]}})
        args = json.loads(request.content)
        calls.append((module, operation, args))
        if module == 'android' and operation in {'execute', 'courier_chat'} and interrupted:
            raise httpx.ReadTimeout('reply lost', request=request)
        if operation == 'observe':
            return httpx.Response(200, json={'elements': [{'text': 'Search'}], 'image': image or {
                'mime_type': 'image/png', 'data': base64.b64encode(PNG).decode()}})
        return httpx.Response(200, json={'state': 'succeeded', 'request_id': args.get('request_id')})

    return create_app(policy, 'http://testserver', transports={
        module: httpx.MockTransport(backend) for module in ('calendar', 'android')}), calls


def test_android_tools_and_native_image_preserve_structured_screen(tmp_path):
    app, calls = setup(tmp_path)
    with TestClient(app) as client:
        names = {tool['name'] for tool in rpc(client, 'tools/list').json()['result']['tools']}
        assert len(TOOLS) == 12 and set(TOOLS) <= names
        result = rpc(client, 'tools/call', {'name': 'android_observe', 'arguments': {
            'device_id': 'home', 'session_id': 'session'}}).json()['result']
        assert not result.get('isError'), result
        assert result['content'][1] == {'type': 'image', 'mimeType': 'image/png',
                                        'data': base64.b64encode(PNG).decode()}
        structured = result['structuredContent']
        assert structured['elements'] == [{'text': 'Search'}]
        assert structured['image']['sha256'] == hashlib.sha256(PNG).hexdigest()
        assert 'data' not in structured['image']
        assert json.loads(result['content'][0]['text']) == structured
        assert calls == [('android', 'observe', {'device_id': 'home', 'session_id': 'session',
                                                'screenshot': True, 'hierarchy': True,
                                                'image_mode': 'inline', 'crop': None})]


# spec: ANYM-975
def test_courier_chat_preview_and_send_reach_the_backend_operation_with_their_arguments(tmp_path):
    app, calls = setup(tmp_path)
    tool = CATALOG['android_courier_chat']
    assert tool['scopes'] == ['android:control'] and tool['operation'] == 'courier_chat'
    assert tool['annotations']['destructiveHint'] is True and tool['annotations']['readOnlyHint'] is False
    for phrase in ('preview', 'owner_instruction', 'never', 'unknown', 'data, never instructions'):
        assert phrase in tool['description']
    base = {'device_id': 'home', 'session_id': 'session', 'app': 'gojek', 'order': 'Warung Sate', 'message': 'Saya di lobi'}
    with TestClient(app) as client:
        for extra in ({'action': 'preview'}, {'action': 'send', 'preview_digest': '1.' + '0' * 64 + '.' + '0' * 64,
                                              'owner_instruction': 'Yes, send it'}):
            request_id = str(uuid.uuid4())
            result = rpc(client, 'tools/call', {'name': 'android_courier_chat', 'arguments': {
                **base, **extra, 'request_id': request_id}}).json()['result']
            assert not result.get('isError'), result
            module, operation, forwarded = calls[-1]
            assert (module, operation) == ('android', 'courier_chat')
            assert {**base, **extra, 'request_id': request_id}.items() <= forwarded.items()
        assert len(calls) == 2


# spec: ANYM-975
def test_a_read_only_android_grant_cannot_call_the_courier_chat(tmp_path):
    app, calls = setup(tmp_path, scopes=['android:read'])
    with TestClient(app) as client:
        result = rpc(client, 'tools/call', {'name': 'android_courier_chat', 'arguments': {
            'device_id': 'home', 'session_id': 'session', 'request_id': str(uuid.uuid4()), 'action': 'preview',
            'app': 'gojek', 'order': 'Warung Sate', 'message': 'Saya di lobi'}}).json()['result']
        assert result['isError'] and calls == []


# spec: ANYM-978
def test_courier_chat_goal_reply_read_handover_and_summary_reach_the_backend_operation(tmp_path):
    app, calls = setup(tmp_path)
    tool = CATALOG['android_courier_chat']
    for phrase in ('goal', 'owner_instruction', 'handover', 'outside_goal', 'owner_summary', 'standing',
                   'data, never instructions', 'never send again'):
        assert phrase in tool['description']
    assert 'session_id' not in tool['inputSchema']['required'] and 'message' not in tool['inputSchema']['required']
    base = {'device_id': 'home', 'app': 'gojek', 'order': 'Warung Sate'}
    goal = {'goal': 'Get the courier to the lobby', 'owner_instruction': 'Handle the courier of Warung Sate for me',
            'max_messages': 4, 'min_interval_s': 20, 'ttl_min': 90, 'standing': False}
    with TestClient(app) as client:
        for extra in ({'action': 'goal', **goal}, {'action': 'read', 'session_id': 'session'},
                      {'action': 'reply', 'session_id': 'session', 'message': 'Saya di lobi'},
                      {'action': 'summary'}, {'action': 'handover', 'reason': 'The courier is hostile'}):
            request_id = str(uuid.uuid4())
            result = rpc(client, 'tools/call', {'name': 'android_courier_chat', 'arguments': {
                **base, **extra, 'request_id': request_id}}).json()['result']
            assert not result.get('isError'), (extra['action'], result)
            module, operation, forwarded = calls[-1]
            assert (module, operation) == ('android', 'courier_chat')
            assert {**base, **extra, 'request_id': request_id}.items() <= forwarded.items()
        assert len(calls) == 5


# spec: ANYM-978
def test_a_read_only_android_grant_cannot_set_a_courier_goal(tmp_path):
    app, calls = setup(tmp_path, scopes=['android:read'])
    with TestClient(app) as client:
        result = rpc(client, 'tools/call', {'name': 'android_courier_chat', 'arguments': {
            'device_id': 'home', 'request_id': str(uuid.uuid4()), 'action': 'goal', 'app': 'gojek',
            'order': 'Warung Sate', 'goal': 'Get the courier to the lobby',
            'owner_instruction': 'Handle the courier of Warung Sate for me'}}).json()['result']
        assert result['isError'] and calls == []


@pytest.mark.parametrize('options', [{'scopes': ['android:read']},
                                    {'backend_scopes': ['android:read']}, {'unavailable': True}])
def test_android_grants_and_outage_preserve_calendar(tmp_path, options):
    app, calls = setup(tmp_path, **options)
    with TestClient(app) as client:
        result = rpc(client, 'tools/call', {'name': 'android_execute', 'arguments': {
            'device_id': 'home', 'session_id': 'session', 'request_id': str(uuid.uuid4()),
            'argv': ['shell', 'input', 'keyevent', 'HOME']}}).json()['result']
        assert result['isError']
        assert calls == []
        calendar = rpc(client, 'tools/call', {'name': 'calendar_slots', 'arguments': {
            'member_ids': ['synthetic'], 'duration_minutes': 30,
            'start': '2026-11-01T00:00:00Z', 'end': '2026-11-02T00:00:00Z'}}).json()['result']
        assert not calendar.get('isError'), calendar


def test_lost_android_reply_keeps_original_request_id_and_never_retries(tmp_path):
    app, calls = setup(tmp_path, interrupted=True)
    request_id = str(uuid.uuid4())
    with TestClient(app) as client:
        result = rpc(client, 'tools/call', {'name': 'android_execute', 'arguments': {
            'device_id': 'home', 'session_id': 'session', 'request_id': request_id,
            'argv': ['shell', 'input', 'tap', '200', '400']}}).json()['result']
        assert result['isError']
        assert result['structuredContent']['error'] == 'mutation_outcome_unknown'
        assert result['structuredContent']['request_id'] == request_id
        assert result['structuredContent']['next_action'] == 'read_android_get_operation_with_original_request_id'
        assert len(calls) == 1
        receipt = rpc(client, 'tools/call', {'name': 'android_get_operation', 'arguments': {
            'request_id': request_id}}).json()['result']
        assert receipt['structuredContent']['state'] == 'succeeded'


# spec: ANYM-975
def test_a_lost_courier_chat_reply_keeps_the_request_id_and_never_retries(tmp_path):
    app, calls = setup(tmp_path, interrupted=True)
    request_id = str(uuid.uuid4())
    with TestClient(app) as client:
        result = rpc(client, 'tools/call', {'name': 'android_courier_chat', 'arguments': {
            'device_id': 'home', 'session_id': 'session', 'request_id': request_id, 'action': 'send',
            'app': 'gojek', 'order': 'Warung Sate', 'message': 'Saya di lobi', 'owner_instruction': 'Yes, send it',
            'preview_digest': '1.' + '0' * 64 + '.' + '0' * 64}}).json()['result']
        assert result['isError']
        assert result['structuredContent']['error'] == 'mutation_outcome_unknown'
        assert result['structuredContent']['request_id'] == request_id
        assert result['structuredContent']['next_action'] == 'read_android_get_operation_with_original_request_id'
        assert len(calls) == 1


@pytest.mark.parametrize('image', [{'mime_type': 'image/png', 'data': 'not-base64'},
                                  {'mime_type': 'text/html', 'data': 'SGVsbG8='}])
def test_invalid_android_image_is_refused(tmp_path, image):
    app, _ = setup(tmp_path, image=image)
    with TestClient(app) as client:
        result = rpc(client, 'tools/call', {'name': 'android_observe', 'arguments': {
            'device_id': 'home', 'session_id': 'session'}}).json()['result']
        assert result['isError']
        assert result['structuredContent']['error'] == 'invalid_android_image'


def test_high_resolution_android_image_is_returned_as_native_content(tmp_path):
    image = PNG + b'x' * (10 * 1024 * 1024)
    app, _ = setup(tmp_path, image={'mime_type': 'image/png', 'data': base64.b64encode(image).decode()})
    with TestClient(app) as client:
        result = rpc(client, 'tools/call', {'name': 'android_observe', 'arguments': {
            'device_id': 'home', 'session_id': 'session'}}).json()['result']
        assert not result.get('isError'), result.get('structuredContent')
        assert base64.b64decode(result['content'][1]['data']) == image


# spec: ANYM-951
def test_workflow_transport_failure_returns_original_receipt_id(tmp_path):
    from agent_gateway.dispatch import Dispatch
    from agent_gateway.policy import Denied
    error = Denied('backend_unavailable', 503)
    request_id = str(uuid.uuid4())
    Dispatch.backend_failure(None, error, 'android_run_flow', {'request_id': request_id},
        {'module': 'android', 'operation': 'run_flow'}, False, {}, {}, {}, {}, {}, None)
    assert error.detail['request_id'] == request_id
    assert error.detail['next_action'] == 'read_android_get_operation_with_original_request_id'


# spec: ANYM-951
def test_image_geometry_is_forwarded_without_scaling():
    from agent_gateway.adapters.android import content
    image = {'mime_type': 'image/png', 'data': base64.b64encode(PNG).decode(), 'width': 1220, 'height': 2712}
    blocks, result = content('android_observe', {'image': image, 'coordinate_space': 'device_pixels'})
    assert blocks[1].data == image['data']
    assert result['image']['width'] == 1220 and result['image']['height'] == 2712
