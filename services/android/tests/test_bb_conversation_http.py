"""The existing Android HTTP backend forwards to one BB conversation, never a phone engine."""
import hashlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from deedkit_android.app import create_app

TARGET = {'principal_id': 'one', 'device_id': 'home', 'node': '/synthetic/node', 'cli': '/synthetic/bb',
          'cli_sha256': '0' * 64, 'server_url': 'http://127.0.0.1:38890', 'thread_id': 'thread-one',
          'project_id': 'project-one', 'environment_id': 'env-one', 'host_id': 'host-one',
          'workspace': '/synthetic/android-lab'}


class BB:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.status = 'active'
        self.wrong = False
        self.on_show = None

    def __call__(self, target, args, text=None):
        self.calls.append((list(args), text))
        if args[1] == 'show':
            if self.on_show:
                self.on_show()
            return {'thread': {'id': 'other' if self.wrong else TARGET['thread_id'],
                               'projectId': TARGET['project_id'], 'environmentId': TARGET['environment_id'],
                               'providerId': 'codex', 'status': self.status, 'queuedMessageCount': 0},
                    'environment': {'id': TARGET['environment_id'], 'hostId': TARGET['host_id'],
                                    'path': TARGET['workspace']}}
        if args[1] == 'tell':
            if self.fail:
                raise TimeoutError('synthetic')
            return {'ok': True, 'delivery': 'sent'}
        if args[1] == 'output':
            return {'output': 'synthetic basket summary'}
        raise AssertionError(args)


@pytest.fixture
def backend(tmp_path, monkeypatch):
    from deedkit_android import bb_conversation, device

    async def no_phone(*args, **kwargs):
        raise AssertionError('no phone/ADB/process in conversation HTTP')

    monkeypatch.setattr(device, 'run_process', no_phone)
    bb = BB()
    monkeypatch.setattr(bb_conversation, 'run_cli', bb)
    config = tmp_path / 'config.json'
    value = {'adb': '/synthetic/forbidden-adb', 'state_dir': str(tmp_path / 'state'),
             'devices': {'home': 'synthetic'}, 'principals': [
                 {'id': name, 'key_sha256': hashlib.sha256(name.encode()).hexdigest(), 'devices': ['home'],
                  'permissions': ['android:read', 'android:control']} for name in ('one', 'two')],
             'bb_conversation': TARGET}
    config.write_text(json.dumps(value))
    config.chmod(0o600)
    return config, bb


def post(client, action, key='one', **kwargs):
    return client.post('/v1/task', json={'device_id': 'home', 'action': action, **kwargs},
                       headers={'Authorization': 'Bearer ' + key})


# spec: ANYM-993
def test_existing_http_messages_and_clarification_use_same_bb_without_phone(backend):
    config, bb = backend
    with TestClient(create_app(config)) as client:
        first = post(client, 'message', request_id=str(uuid.uuid4()), text='Prepare basket only\nOwner words')
        second = post(client, 'message', request_id=str(uuid.uuid4()), text='Clarification: smaller size')
    assert first.status_code == second.status_code == 200
    assert first.json()['task_id'] == second.json()['task_id'] == TARGET['thread_id']
    sends = [call for call in bb.calls if call[0][1] == 'tell']
    assert len(sends) == 2 and sends[0][1] == 'Prepare basket only\nOwner words'
    assert sends[0][0] == ['thread', 'tell', TARGET['thread_id'], '--mode', 'auto', '--message-file', '-', '--json']


# spec: ANYM-993
def test_status_and_private_capabilities_are_phone_free_and_do_not_send(backend):
    config, bb = backend
    with TestClient(create_app(config)) as client:
        caps = client.post('/internal/v1/bb-conversation-capabilities', json={},
                           headers={'Authorization': 'Bearer one'})
        assert caps.status_code == 200 and caps.json()['tool_manifest']['names'] == ['task']
        assert bb.calls == []
        discovery = client.post('/v1/capabilities', json={}, headers={'Authorization': 'Bearer one'})
        assert discovery.status_code == 200
        assert discovery.json()['device_status_observed'] is False
        assert 'state' not in discovery.json()['devices'][0]
        assert bb.calls == []
        for _ in range(3):
            assert post(client, 'status').json()['status'] == 'active'
    assert [call[0][1] for call in bb.calls] == ['show'] * 3
    assert not (config.parent / 'state' / 'bb-conversation').exists()


# spec: ANYM-993
def test_transport_unknown_and_receipt_survive_http_restart_without_resending(backend):
    config, bb = backend
    request = str(uuid.uuid4())
    bb.fail = True
    with TestClient(create_app(config)) as client:
        first = post(client, 'message', request_id=request, text='Original')
        assert first.json()['delivery'] == 'unknown'
    bb.fail = False
    with TestClient(create_app(config)) as client:
        repeated = post(client, 'message', request_id=request, text='Original')
        receipt = post(client, 'receipt', request_id=request)
        assert repeated.json() == receipt.json() == first.json()
        assert post(client, 'message', request_id=request, text='Changed').status_code == 409
    assert len([call for call in bb.calls if call[0][1] == 'tell']) == 1
    assert b'Original' not in b''.join(p.read_bytes() for p in (config.parent / 'state' / 'bb-conversation').rglob('*.json'))


# spec: ANYM-993
@pytest.mark.parametrize('change', ['foreign', 'revoked', 'missing'])
def test_conversation_admission_refuses_identity_or_rights_or_missing_binding(backend, change):
    config, bb = backend
    if change != 'foreign':
        value = json.loads(config.read_text())
        if change == 'revoked':
            value['principals'][0]['permissions'] = ['android:read']
        else:
            value.pop('bb_conversation')
        config.write_text(json.dumps(value))
    with TestClient(create_app(config)) as client:
        response = post(client, 'message', key='two' if change == 'foreign' else 'one',
                        request_id=str(uuid.uuid4()), text='Do not send')
    assert response.status_code in (403, 404, 503)
    assert bb.calls == []


# spec: ANYM-993
def test_wrong_bb_target_never_starts_another_conversation(backend):
    config, bb = backend
    bb.wrong = True
    with TestClient(create_app(config)) as client:
        response = post(client, 'message', request_id=str(uuid.uuid4()), text='Original')
    assert response.status_code == 409 and response.json()['error'] == 'conversation_binding_changed'
    assert [call[0][1] for call in bb.calls] == ['show']


# spec: ANYM-993
def test_control_revocation_during_metadata_read_prevents_message_dispatch(backend):
    config, bb = backend
    def revoke():
        value = json.loads(config.read_text())
        value['principals'][0]['permissions'] = ['android:read']
        config.write_text(json.dumps(value))
    bb.on_show = revoke
    with TestClient(create_app(config)) as client:
        response = post(client, 'message', request_id=str(uuid.uuid4()), text='Original')
    assert response.status_code in (200, 403)
    if response.status_code == 200:
        assert response.json()['delivery'] == 'unknown'
    assert [call[0][1] for call in bb.calls] == ['show']


# spec: ANYM-993
def test_latest_conversation_result_is_read_only_and_explicitly_scoped(backend):
    config, bb = backend
    with TestClient(create_app(config)) as client:
        assert post(client, 'result').json()['result'] is None
        bb.status = 'idle'
        result = post(client, 'result').json()
    assert result['scope'] == 'latest_conversation_result' and result['result'] == 'synthetic basket summary'
    assert [call[0][1] for call in bb.calls] == ['show', 'show', 'output']
