"""Real MCP dispatch and conversation backend functions, fake BB transport and no phone process."""
import hashlib
import json
from pathlib import Path
import uuid

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG, public
from agent_gateway.diagnostics import phone_state
from test_policy import config
from test_transport import rpc


@pytest.fixture
def chain(tmp_path, monkeypatch):
    # Source contract integration only
    # Android is not a gateway runtime dependency.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'android/src'))
    from deedkit_android import bb_conversation as bb
    from deedkit_android.config import Configuration, Strict
    from deedkit_android.store import Store

    target = {'principal_id': 'backend-owner', 'device_id': 'home', 'node': '/synthetic/node',
              'cli': '/synthetic/bb', 'cli_sha256': '0' * 64, 'server_url': 'http://127.0.0.1:38890',
              'thread_id': 'original-bb', 'project_id': 'project', 'environment_id': 'environment',
              'host_id': 'host', 'workspace': '/synthetic/phone'}
    settings = {'adb': '/forbidden/adb', 'state_dir': str(tmp_path / 'state'),
                'devices': {'home': 'synthetic'}, 'principals': [{'id': 'backend-owner',
                    'key_sha256': hashlib.sha256(b'backend-key').hexdigest(), 'devices': ['home'],
                    'permissions': ['android:read', 'android:control']}], 'bb_conversation': target}
    source = tmp_path / 'android.json'
    source.write_text(json.dumps(settings))
    source.chmod(0o600)
    configuration = Configuration(source)
    store = Store(tmp_path / 'state')
    calls, requests = [], []
    control = {'status': 'active', 'unknown': False}

    def cli(selected, args, text=None):
        calls.append((args, text))
        if args[1] == 'show':
            return {'thread': {'id': target['thread_id'], 'projectId': target['project_id'],
                               'environmentId': target['environment_id'], 'providerId': 'codex',
                               'status': control['status'], 'queuedMessageCount': 0},
                    'environment': {'id': target['environment_id'], 'hostId': target['host_id'],
                                    'path': target['workspace']}}
        if args[1] == 'tell':
            if control['unknown']:
                raise TimeoutError('synthetic lost response')
            return {'ok': True, 'delivery': 'sent'}
        if args[1] == 'output':
            return {'output': 'Original conversation answer'}
        raise AssertionError(args)

    monkeypatch.setattr(bb, 'run_cli', cli)
    schemas = {'capabilities': Strict, 'task': bb.TaskCall}

    async def backend(request):
        requests.append(request.url.path)
        header = request.headers['authorization']
        principal = configuration.authenticate(header)
        if request.url.path in ('/v1/capabilities', '/internal/v1/bb-conversation-capabilities'):
            return httpx.Response(200, json=bb.capabilities(configuration, header, schemas))
        assert request.url.path == '/v1/task'
        assert 'android:control' in principal.permissions
        values = bb.TaskCall.model_validate(json.loads(request.content)).model_dump()
        return httpx.Response(200, json=await bb.handle(configuration, header, store, values))

    value = config()
    key = tmp_path / 'android.key'
    key.write_text('backend-key')
    key.chmod(0o600)
    value['deployment'] = {'modules': ['android'], 'backends': {'android': {'url': 'http://android'}}}
    value['profiles']['reader']['tools'] = ['platform_capabilities', 'android_task']
    actor = value['principals'][0]
    actor['grants']['android'] = ['android:read', 'android:control']
    actor['credentials'] = {'android': {'key_file': str(key), 'key_sha256': hashlib.sha256(b'backend-key').hexdigest()}}
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps(value))
    policy.chmod(0o600)
    app = create_app(policy, 'http://testserver', transports={'android': httpx.MockTransport(backend)})
    try:
        yield app, calls, requests, control, source
    finally:
        store.close()


def task(client, action, **fields):
    return rpc(client, 'tools/call', {'name': 'android_task',
                                    'arguments': {'action': action, 'device_id': 'home', **fields}}).json()['result']


# spec: ANYM-993
def test_discovery_and_status_use_phone_free_backend_capabilities(chain):
    app, calls, requests, _, _ = chain
    with TestClient(app) as client:
        names = [t['name'] for t in rpc(client, 'tools/list').json()['result']['tools']]
        assert 'android_task' in names
        requests.clear()
        result = task(client, 'status')
    assert not result.get('isError'), result
    assert result['structuredContent']['task_id'] == 'original-bb'
    assert requests == ['/internal/v1/bb-conversation-capabilities', '/v1/task']
    assert [args[1] for args, _ in calls] == ['show']
    assert phone_state({'devices': [{'device_id': 'home'}], 'device_status_observed': False}) == {}


# spec: ANYM-993
def test_goal_and_clarification_keep_the_same_bb_conversation(chain):
    app, calls, _, _, _ = chain
    with TestClient(app) as client:
        answers = [task(client, 'message', request_id=str(uuid.uuid4()), text=text)
                   for text in ('Prepare cart only', 'Use the smaller item')]
    assert all(not answer.get('isError') for answer in answers), answers
    assert {answer['structuredContent']['task_id'] for answer in answers} == {'original-bb'}
    assert [text for args, text in calls if args[1] == 'tell'] == ['Prepare cart only', 'Use the smaller item']
    assert all(args[:3] == ['thread', 'tell', 'original-bb'] for args, _ in calls if args[1] == 'tell')


# spec: ANYM-993
def test_unknown_message_receipt_never_resends_through_mcp(chain):
    app, calls, _, control, _ = chain
    control['unknown'] = True
    ident = str(uuid.uuid4())
    with TestClient(app) as client:
        first = task(client, 'message', request_id=ident, text='Original')
        receipt = task(client, 'receipt', request_id=ident)
        repeated = task(client, 'message', request_id=ident, text='Original')
    assert first['structuredContent']['delivery'] == 'unknown'
    assert receipt['structuredContent'] == repeated['structuredContent'] == first['structuredContent']
    assert len([args for args, _ in calls if args[1] == 'tell']) == 1


# spec: ANYM-993
def test_unconfigured_backend_refuses_without_generic_phone_capability_fallback(chain):
    app, calls, requests, _, source = chain
    value = json.loads(source.read_text())
    value.pop('bb_conversation')
    source.write_text(json.dumps(value))
    with TestClient(app) as client:
        result = task(client, 'status')
    assert result['isError'] and result['structuredContent']['error'] == 'android_conversation_unavailable'
    assert requests == ['/internal/v1/bb-conversation-capabilities']
    assert calls == []


# spec: ANYM-993
def test_unknown_target_fields_are_rejected_before_backend_call(chain):
    app, calls, requests, _, _ = chain
    with TestClient(app) as client:
        result = task(client, 'status', thread_id='another-thread')
    assert result['isError'] and calls == [] and requests == []


# spec: ANYM-993
def test_result_reads_latest_conversation_and_never_messages_or_starts_an_agent(chain):
    app, calls, _, control, _ = chain
    control['status'] = 'idle'
    with TestClient(app) as client:
        result = task(client, 'result')
    assert result['structuredContent']['scope'] == 'latest_conversation_result'
    assert result['structuredContent']['result'] == 'Original conversation answer'
    assert [args[1] for args, _ in calls] == ['show', 'output']


# spec: ANYM-993
def test_task_schema_and_control_boundary_are_the_packaged_contract():
    assert CATALOG['android_task']['module'] == 'android'
    assert CATALOG['android_task']['scopes'] == ['android:control']
    assert CATALOG['android_task']['annotations']['destructiveHint'] is True
    assert set(CATALOG['android_task']['inputSchema']['properties']) == {'device_id', 'action', 'request_id', 'text'}


# spec: ANYM-993
def test_discovery_leaves_android_execution_to_the_phone_agent(chain):
    app, *_ = chain
    with TestClient(app) as client:
        tools = {tool['name']: tool for tool in rpc(client, 'tools/list').json()['result']['tools']}
    description = tools['android_task']['description']
    assert "goal and the owner's constraints" in description
    assert 'Leave navigation, tool choice and screenshots to the phone agent' in description
    assert 'Only specify a payment method the owner actually requested' in description
    assert 'forwards the owner' in description
    assert 'does not grant phone, checkout or payment authority' in description

    native = public(CATALOG['android_agent'])['description']
    assert 'Leave navigation, tool choice and screenshots to the phone agent' in native
    assert 'Only specify a payment method the owner actually requested' in native
    assert 'verbatim owner_instruction/restrictions' in native
