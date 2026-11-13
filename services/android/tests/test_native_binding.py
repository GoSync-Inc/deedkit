"""Host/native conversation binding at a synthetic stdio boundary; no live adoption."""

import json

import pytest

from agent_harness import Service
from test_codex_session import live_host, calls


def native_calls(record, method):
    return [row['message'] for row in calls(record) if row['message'].get('method') == method]


# spec: ANYM-993
def test_workspace_thread_hint_cannot_redirect_restart_to_foreign_conversation(tmp_path):
    config, record = live_host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        first = service.finished(service.start(lease)['request_id'])
    hint, = (tmp_path / 'state' / 'agent' / 'sessions').glob('run-live-*/thread.json')
    hint.write_text(json.dumps({'thread_id': 'foreign-thread'}))
    with Service(config) as service:
        service.finished(service.start(lease)['request_id'])
    assert all(row['params']['threadId'] != 'foreign-thread' for row in native_calls(record, 'thread/resume'))
    assert all(row['params']['threadId'] == first['native_session']['thread_id']
               for row in native_calls(record, 'turn/start'))


# spec: ANYM-993
def test_deleted_workspace_hint_never_creates_replacement_thread_after_restart(tmp_path):
    config, record = live_host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        first = service.finished(service.start(lease)['request_id'])
    hint, = (tmp_path / 'state' / 'agent' / 'sessions').glob('run-live-*/thread.json')
    hint.unlink()
    with Service(config) as service:
        service.finished(service.start(lease)['request_id'])
    assert len(native_calls(record, 'thread/start')) == 1
    assert all(row['params']['threadId'] == first['native_session']['thread_id']
               for row in native_calls(record, 'turn/start'))


# spec: ANYM-993
def test_other_principal_saved_hint_is_never_adopted_after_restart(tmp_path):
    config, record = live_host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        first = service.finished(service.start(lease)['request_id'])
        service.post('session', dict(action='release', device_id='home', session_id=lease))
        other_lease = service.lease(key='two')
        other = service.finished(service.start(other_lease, key='two')['request_id'], key='two')
    hints = list((tmp_path / 'state' / 'agent' / 'sessions').glob('run-live-*/thread.json'))
    for hint in hints:
        if json.loads(hint.read_text())['thread_id'] == first['native_session']['thread_id']:
            hint.write_text(json.dumps({'thread_id': other['native_session']['thread_id']}))
    before = len(native_calls(record, 'turn/start'))
    with Service(config) as service:
        service.post('session', dict(action='release', device_id='home', session_id=other_lease), key='two')
        service.finished(service.start(service.lease())['request_id'])
    assert all(row['params']['threadId'] == first['native_session']['thread_id']
               for row in native_calls(record, 'turn/start')[before:])


def change_thread_receipt(config, fields):
    """Change the synthetic native response before JSON serialization, not the adapter result."""
    from pathlib import Path
    binary = Path(json.loads(config.read_text())['engines']['codex']['binary'])
    source = binary.read_text()
    old = 'def emit(**message):\n    print(json.dumps(message), flush=True)'
    assert old in source
    binary.write_text(source.replace(old, 'def emit(**message):\n'
        "    if 'thread' in message.get('result', {}):\n"
        f"        message['result'].update({fields!r})\n"
        '    print(json.dumps(message), flush=True)'))


# spec: ANYM-993
def test_unverified_legacy_binding_stops_before_new_native_process_and_preserves_original_result(tmp_path):
    import sqlite3
    config, record = live_host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        original = service.start(lease)['request_id']
        first = service.finished(original)
    db = sqlite3.connect(tmp_path / 'state' / 'state.sqlite3')
    with db:
        db.execute('DELETE FROM agent_native_bindings')  # synthetic legacy row, not an attestation/migration
    db.close()
    before = len(calls(record))
    with Service(config) as service:
        assert service.finished(original) == first
        result = service.finished(service.start(lease)['request_id'])
        assert result['state'] == 'failed' and result['error'] == 'native_binding_unverified'
    assert len(calls(record)) == before
    assert len(native_calls(record, 'thread/start')) == 1


# spec: ANYM-993
def test_lost_thread_creation_receipt_is_not_retried_or_replaced_by_new_task_id(tmp_path):
    from pathlib import Path
    config, record = live_host(tmp_path)
    binary = Path(json.loads(config.read_text())['engines']['codex']['binary'])
    source = binary.read_text()
    binary.write_text(source.replace("thread_id = params.get('threadId', thread_id)", 'raise SystemExit(1)'))
    with Service(config) as service:
        lease = service.lease()
        original = service.start(lease)['request_id']
        assert service.finished(original)['state'] == 'failed'
    with Service(config) as service:
        service.start(lease, request_id=original)
        failed = service.finished(service.start(lease)['request_id'])
        assert failed['state'] == 'failed' and failed['error'] == 'native_binding_unverified'
    assert len(native_calls(record, 'thread/start')) == 1
    assert native_calls(record, 'thread/resume') == [] and native_calls(record, 'turn/start') == []


# spec: ANYM-993
def test_same_live_thread_ignores_mutated_compatibility_hint_without_process_restart(tmp_path):
    config, record = live_host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        first = service.finished(service.start(lease)['request_id'])
        hint, = (tmp_path / 'state' / 'agent' / 'sessions').glob('run-live-*/thread.json')
        hint.write_text(json.dumps({'thread_id': 'foreign'}))
        second = service.finished(service.start(lease)['request_id'])
        assert second['state'] == 'succeeded'
        assert second['native_session']['thread_id'] == first['native_session']['thread_id']
        assert second['native_session']['pid'] == first['native_session']['pid']
    assert len(native_calls(record, 'thread/start')) == 1
    assert all(row['params']['threadId'] == first['native_session']['thread_id']
               for row in native_calls(record, 'turn/start'))


# spec: ANYM-993
def test_resumed_native_receipt_cannot_replace_original_thread_id(tmp_path):
    config, record = live_host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        first = service.finished(service.start(lease)['request_id'])
    change_thread_receipt(config, {'thread': {'id': 'foreign'}})
    before = len(native_calls(record, 'turn/start'))
    with Service(config) as service:
        result = service.finished(service.start(lease)['request_id'])
        assert result['state'] == 'failed' and result['error'] == 'native_binding_mismatch'
    assert len(native_calls(record, 'turn/start')) == before
    assert all(row['params']['threadId'] == first['native_session']['thread_id']
               for row in native_calls(record, 'thread/resume'))


# spec: ANYM-993
@pytest.mark.parametrize('changed', [
    {'sandbox': None},
    {'sandbox': {'type': 'dangerFullAccess'}},
    {'cwd': '/foreign-workspace'},
    {'runtimeWorkspaceRoots': ['/']},
    {'activePermissionProfile': {'id': 'unknown-profile', 'extends': ':workspace'}},
    {'sandbox': {'type': 'workspaceWrite', 'writableRoots': ['/'],
                 'excludeTmpdirEnvVar': True, 'excludeSlashTmp': True}},
    {'sandbox': {'type': 'workspaceWrite', 'writableRoots': [],
                 'excludeTmpdirEnvVar': False, 'excludeSlashTmp': True}},
    {'sandbox': {'type': 'workspaceWrite', 'writableRoots': [],
                 'excludeTmpdirEnvVar': True, 'excludeSlashTmp': False}},
])
def test_unconfirmed_native_write_boundary_never_dispatches_goal_or_establishes_resumable_binding(tmp_path, changed):
    config, record = live_host(tmp_path)
    change_thread_receipt(config, changed)
    with Service(config) as service:
        lease = service.lease()
        result = service.finished(service.start(lease)['request_id'])
        assert result['state'] == 'failed' and result['error'] == 'native_binding_boundary_unconfirmed'
    with Service(config) as service:
        result = service.finished(service.start(lease)['request_id'])
        assert result['state'] == 'failed' and result['error'] == 'native_binding_unverified'
    assert len(native_calls(record, 'thread/start')) == 1
    assert native_calls(record, 'thread/resume') == [] and native_calls(record, 'turn/start') == []


# spec: ANYM-993
def test_other_device_hint_cannot_cross_owner_device_host_binding(tmp_path):
    config, record = live_host(tmp_path)
    body = json.loads(config.read_text())
    body['devices']['travel'] = 'OTHER-SYNTHETIC-SERIAL'
    body['principals'][0]['devices'].append('travel')
    config.write_text(json.dumps(body))
    with Service(config) as service:
        home_lease, travel_lease = service.lease(), service.lease('travel')
        home = service.finished(service.start(home_lease)['request_id'])
        travel = service.finished(service.start(travel_lease, device_id='travel')['request_id'])
    for hint in (tmp_path / 'state' / 'agent' / 'sessions').glob('run-live-*/thread.json'):
        if json.loads(hint.read_text())['thread_id'] == home['native_session']['thread_id']:
            hint.write_text(json.dumps({'thread_id': travel['native_session']['thread_id']}))
    before = len(native_calls(record, 'turn/start'))
    with Service(config) as service:
        result = service.finished(service.start(home_lease)['request_id'])
        assert result['state'] == 'succeeded' and result['native_session']['thread_id'] == home['native_session']['thread_id']
    assert all(row['params']['threadId'] == home['native_session']['thread_id']
               for row in native_calls(record, 'turn/start')[before:])


# spec: ANYM-993
@pytest.mark.parametrize('corruption', ['wrong_cwd', 'unknown_version', 'incomplete'])
def test_unusable_host_binding_never_falls_back_to_native_hint_or_new_thread(tmp_path, corruption):
    import sqlite3
    config, record = live_host(tmp_path)
    with Service(config) as service:
        lease = service.lease()
        service.finished(service.start(lease)['request_id'])
    db = sqlite3.connect(tmp_path / 'state' / 'state.sqlite3')
    with db:
        if corruption == 'wrong_cwd':
            db.execute("UPDATE agent_native_bindings SET cwd='/another-owner/workspace'")
        elif corruption == 'unknown_version':
            db.execute('UPDATE agent_native_bindings SET boundary=?', (json.dumps({'version': 999}),))
        else:
            db.execute('UPDATE agent_native_bindings SET thread=NULL,boundary=NULL')
    db.close()
    before = len(calls(record))
    with Service(config) as service:
        result = service.finished(service.start(lease)['request_id'])
        assert result['state'] == 'failed' and result['error'] == 'native_binding_unverified'
    assert len(calls(record)) == before
    assert len(native_calls(record, 'thread/start')) == 1
