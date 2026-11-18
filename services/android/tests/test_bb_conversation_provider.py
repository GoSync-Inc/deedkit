"""The configured BB conversation target names the provider its thread must report: codex by default, or claude-code.

The real HTTP app and strict config with a synthetic BB CLI: no phone, ADB, device, model or real BB call.
"""
import hashlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from deedkit_android.app import create_app

TARGET = {'principal_id': 'one', 'device_id': 'home', 'node': '/synthetic/node', 'cli': '/synthetic/bb',
          'cli_sha256': '0' * 64, 'server_url': 'http://127.0.0.1:38890', 'thread_id': 'thread-claude',
          'project_id': 'project-one', 'environment_id': 'env-one', 'host_id': 'host-one',
          'workspace': '/synthetic/android-lab'}


class BB:
    """`bb thread show|tell|output` of one synthetic thread; `reported` is what its metadata says."""

    def __init__(self, provider):
        self.calls, self.fail, self.status = [], False, 'idle'
        self.reported = {'thread': {'id': TARGET['thread_id'], 'projectId': TARGET['project_id'],
                                    'environmentId': TARGET['environment_id'], 'providerId': provider,
                                    'queuedMessageCount': 0},
                         'environment': {'id': TARGET['environment_id'], 'hostId': TARGET['host_id'],
                                         'path': TARGET['workspace']}}

    def __call__(self, target, args, text=None):
        self.calls.append((list(args), text))
        if args[1] == 'show':
            return json.loads(json.dumps({**self.reported, 'thread': {**self.reported['thread'], 'status': self.status}}))
        if args[1] == 'tell':
            if self.fail:
                raise TimeoutError('synthetic')
            return {'ok': True, 'delivery': 'sent'}
        if args[1] == 'output':
            return {'output': 'synthetic basket summary'}
        raise AssertionError(args)

    def sends(self):
        return [call for call in self.calls if call[0][1] == 'tell']


def backend(tmp_path, monkeypatch, provider, configured=None, absent=False):
    from deedkit_android import bb_conversation, device

    async def no_phone(*args, **kwargs):
        raise AssertionError('no phone/ADB/process in conversation HTTP')

    monkeypatch.setattr(device, 'run_process', no_phone)
    bb = BB(provider)
    monkeypatch.setattr(bb_conversation, 'run_cli', bb)
    target = dict(TARGET) if absent else {**TARGET, 'provider': configured}
    write(tmp_path, target)
    return tmp_path / 'config.json', bb


def write(tmp_path, target):
    config = tmp_path / 'config.json'
    value = {'adb': '/synthetic/forbidden-adb', 'state_dir': str(tmp_path / 'state'),
             'devices': {'home': 'synthetic-home', 'other': 'synthetic-other'}, 'principals': [
                 {'id': name, 'key_sha256': hashlib.sha256(name.encode()).hexdigest(), 'devices': ['home'],
                  'permissions': ['android:read', 'android:control']} for name in ('one', 'two')],
             'bb_conversation': target}
    config.write_text(json.dumps(value))
    config.chmod(0o600)


def post(client, action, key='one', device='home', **kwargs):
    return client.post('/v1/task', json={'device_id': device, 'action': action, **kwargs},
                       headers={'Authorization': 'Bearer ' + key})


# spec: ANYM-993
def test_a_claude_code_target_carries_message_status_result_and_receipt_in_its_one_existing_thread(tmp_path, monkeypatch):
    config, bb = backend(tmp_path, monkeypatch, 'claude-code', 'claude-code')
    request = str(uuid.uuid4())
    with TestClient(create_app(config)) as client:
        sent = post(client, 'message', request_id=request, text='Prepare basket only\nOwner words')
        assert sent.status_code == 200, sent.text
        assert sent.json()['task_id'] == TARGET['thread_id'] and sent.json()['delivery'] == 'sent'
        assert post(client, 'message', request_id=request, text='Prepare basket only\nOwner words').json() == sent.json()
        assert post(client, 'receipt', request_id=request).json() == sent.json()
        assert post(client, 'status').json()['status'] == 'idle'
        assert post(client, 'result').json()['result'] == 'synthetic basket summary'
    assert [call[0] for call in bb.sends()] == [
        ['thread', 'tell', TARGET['thread_id'], '--mode', 'auto', '--message-file', '-', '--json']]
    assert bb.sends()[0][1] == 'Prepare basket only\nOwner words'


# spec: ANYM-993
@pytest.mark.parametrize('configured', ['absent', 'codex'])
def test_a_codex_target_by_default_or_explicitly_binds_its_codex_thread_as_before(tmp_path, monkeypatch, configured):
    config, bb = backend(tmp_path, monkeypatch, 'codex', 'codex', absent=configured == 'absent')
    with TestClient(create_app(config)) as client:
        sent = post(client, 'message', request_id=str(uuid.uuid4()), text='Original')
        assert sent.status_code == 200 and sent.json()['delivery'] == 'sent'
        assert post(client, 'status').json()['status'] == 'idle'
    assert len(bb.sends()) == 1


# spec: ANYM-993
@pytest.mark.parametrize('configured, reported', [('claude-code', 'codex'), ('codex', 'claude-code'),
                                                  ('absent', 'claude-code'), ('claude-code', None),
                                                  ('claude-code', 'Claude-Code')])
def test_a_provider_mismatch_in_either_direction_refuses_before_any_send(tmp_path, monkeypatch, configured, reported):
    config, bb = backend(tmp_path, monkeypatch, reported, configured, absent=configured == 'absent')
    with TestClient(create_app(config)) as client:
        for action, extra in (('message', {'request_id': str(uuid.uuid4()), 'text': 'Do not send'}),
                              ('status', {}), ('result', {})):
            response = post(client, action, **extra)
            assert response.status_code == 409 and response.json()['error'] == 'conversation_binding_changed'
    assert [call[0][1] for call in bb.calls] == ['show'] * 3
    assert not list((tmp_path / 'state').rglob('bb-message-*.json'))


# spec: ANYM-993
@pytest.mark.parametrize('field', ['thread', 'project', 'environment', 'environment record', 'host', 'workspace'])
def test_every_other_reported_identity_still_binds_a_claude_code_target(tmp_path, monkeypatch, field):
    config, bb = backend(tmp_path, monkeypatch, 'claude-code', 'claude-code')
    place, key = {'thread': ('thread', 'id'), 'project': ('thread', 'projectId'),
                  'environment': ('thread', 'environmentId'), 'environment record': ('environment', 'id'),
                  'host': ('environment', 'hostId'), 'workspace': ('environment', 'path')}[field]
    bb.reported[place][key] = 'synthetic-other'
    with TestClient(create_app(config)) as client:
        response = post(client, 'message', request_id=str(uuid.uuid4()), text='Do not send')
    assert response.status_code == 409 and response.json()['error'] == 'conversation_binding_changed'
    assert [call[0][1] for call in bb.calls] == ['show']


# spec: ANYM-993
@pytest.mark.parametrize('change', ['foreign principal', 'revoked control', 'other device', 'device not granted'])
def test_principal_device_and_permission_guards_refuse_a_claude_code_target_without_any_bb_call(
        tmp_path, monkeypatch, change):
    config, bb = backend(tmp_path, monkeypatch, 'claude-code', 'claude-code')
    value = json.loads(config.read_text())
    if change == 'revoked control':
        value['principals'][0]['permissions'] = ['android:read']
    elif change == 'device not granted':
        value['bb_conversation']['device_id'] = 'other'
    config.write_text(json.dumps(value))
    with TestClient(create_app(config)) as client:
        response = post(client, 'message', key='two' if change == 'foreign principal' else 'one',
                        device='other' if change == 'other device' else 'home',
                        request_id=str(uuid.uuid4()), text='Do not send')
    assert response.status_code in (403, 404), response.text
    assert bb.calls == []


# spec: ANYM-993
@pytest.mark.parametrize('provider', [None, '', 'claude', 'Claude-Code', 'claude_code', 'grok', 'codex ', 1])
def test_a_configured_provider_outside_codex_and_claude_code_refuses_every_action(tmp_path, monkeypatch, provider):
    config, bb = backend(tmp_path, monkeypatch, 'claude-code', provider)
    with pytest.raises(ValueError, match='provider'):  # the service does not start on it
        with TestClient(create_app(config)):
            pass
    # A valid service whose config later changes to it refuses each request without a BB call.
    write(tmp_path, {**TARGET, 'provider': 'claude-code'})
    with TestClient(create_app(config)) as client:
        write(tmp_path, {**TARGET, 'provider': provider})
        for action, extra in (('message', {'request_id': str(uuid.uuid4()), 'text': 'Do not send'}),
                              ('status', {})):
            response = post(client, action, **extra)
            assert response.status_code == 503 and response.json()['error'] == 'configuration_unavailable'
    assert bb.calls == []


# spec: ANYM-993
def test_a_caller_cannot_choose_the_provider_and_discovery_stays_phone_and_bb_free(tmp_path, monkeypatch):
    config, bb = backend(tmp_path, monkeypatch, 'claude-code', 'claude-code')
    with TestClient(create_app(config)) as client:
        for provider in ('codex', 'claude-code'):
            response = post(client, 'message', request_id=str(uuid.uuid4()), text='Do not send', provider=provider)
            assert 400 <= response.status_code < 500 and response.status_code != 404, response.text
            assert post(client, 'status', provider=provider).status_code == response.status_code
        caps = client.post('/internal/v1/bb-conversation-capabilities', json={}, headers={'Authorization': 'Bearer one'})
        assert caps.status_code == 200 and 'provider' not in json.dumps(caps.json()['input_schemas'])
        assert client.post('/v1/capabilities', json={}, headers={'Authorization': 'Bearer one'}).status_code == 200
    assert bb.calls == []


# spec: ANYM-993
def test_an_unknown_claude_code_delivery_stays_unknown_and_a_codex_receipt_is_never_resent_after_the_switch(
        tmp_path, monkeypatch):
    config, bb = backend(tmp_path, monkeypatch, 'codex', 'codex')
    request = str(uuid.uuid4())
    with TestClient(create_app(config)) as client:
        assert post(client, 'message', request_id=request, text='Before the switch').json()['delivery'] == 'sent'
    # The switch: the same principal and device now name the claude-code thread.
    bb.reported['thread'].update(id='thread-claude-2', providerId='claude-code')
    write(tmp_path, {**TARGET, 'thread_id': 'thread-claude-2', 'provider': 'claude-code'})
    unknown = str(uuid.uuid4())
    bb.fail = True
    with TestClient(create_app(config)) as client:
        assert post(client, 'message', request_id=request, text='Before the switch').status_code == 409
        first = post(client, 'message', request_id=unknown, text='After the switch')
        assert first.json()['delivery'] == 'unknown' and first.json()['task_id'] == 'thread-claude-2'
    bb.fail = False
    with TestClient(create_app(config)) as client:
        assert post(client, 'message', request_id=unknown, text='After the switch').json() == first.json()
    assert [call[1] for call in bb.sends()] == ['Before the switch', 'After the switch']
    # Rollback to the Codex target reads its own receipt again; nothing is resent.
    bb.reported['thread'].update(id=TARGET['thread_id'], providerId='codex')
    write(tmp_path, {**TARGET, 'provider': 'codex'})
    with TestClient(create_app(config)) as client:
        assert post(client, 'receipt', request_id=request).json()['delivery'] == 'sent'
    assert len(bb.sends()) == 2


# spec: ANYM-993
def test_a_changed_cli_refuses_a_claude_code_target_before_any_bb_process(tmp_path, monkeypatch):
    from deedkit_android import bb_conversation, device

    async def no_phone(*args, **kwargs):
        raise AssertionError('no phone/ADB/process in conversation HTTP')

    monkeypatch.setattr(device, 'run_process', no_phone)
    monkeypatch.setattr(bb_conversation.subprocess, 'Popen', lambda *a, **k: pytest.fail('no BB process'))
    cli = tmp_path / 'bb'
    cli.write_text('synthetic cli\n')
    write(tmp_path, {**TARGET, 'cli': str(cli), 'cli_sha256': hashlib.sha256(b'another cli\n').hexdigest(),
                     'provider': 'claude-code'})
    with TestClient(create_app(tmp_path / 'config.json')) as client:
        response = post(client, 'status')
    assert response.status_code == 409 and response.json()['error'] == 'bb_cli_changed'


# spec: ANYM-993
def test_a_switch_neither_migrates_nor_resends_an_unknown_codex_request_and_rollback_keeps_it_unknown(
        tmp_path, monkeypatch):
    config, bb = backend(tmp_path, monkeypatch, 'codex', 'codex')
    request = str(uuid.uuid4())
    bb.fail = True
    with TestClient(create_app(config)) as client:
        assert post(client, 'message', request_id=request, text='Codex goal').json()['delivery'] == 'unknown'
    bb.fail = False
    receipts = sorted((tmp_path / 'state').rglob('bb-message-*.json'))
    before = {path: path.read_bytes() for path in receipts}
    # Switch to the claude-code thread: the unknown Codex request is refused under its original ID, whatever the text.
    bb.reported['thread'].update(id='thread-claude-2', providerId='claude-code')
    write(tmp_path, {**TARGET, 'thread_id': 'thread-claude-2', 'provider': 'claude-code'})
    with TestClient(create_app(config)) as client:
        for action, extra in (('message', {'request_id': request, 'text': 'Codex goal'}),
                              ('message', {'request_id': request, 'text': 'Codex goal, edited'}),
                              ('receipt', {'request_id': request})):
            response = post(client, action, **extra)
            assert response.status_code == 409, response.text
            assert response.json()['error'] in ('receipt_binding_changed', 'request_id_conflict')
    assert sorted((tmp_path / 'state').rglob('bb-message-*.json')) == receipts
    assert {path: path.read_bytes() for path in receipts} == before  # no migration to the new thread
    # Rollback: the original receipt reads back still unknown; nothing was ever resent.
    bb.reported['thread'].update(id=TARGET['thread_id'], providerId='codex')
    write(tmp_path, {**TARGET, 'provider': 'codex'})
    with TestClient(create_app(config)) as client:
        receipt = post(client, 'receipt', request_id=request).json()
        assert receipt['delivery'] == 'unknown' and receipt['task_id'] == TARGET['thread_id']
        assert post(client, 'message', request_id=request, text='Codex goal').json() == receipt
    assert [call[1] for call in bb.sends()] == ['Codex goal']
