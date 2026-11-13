"""Existing policy/credential admission owns the bounded private Android feed producer."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

from agent_gateway.adapters.android import wake_step
from agent_gateway.dispatch import Dispatch
from agent_gateway.policy import Policy
from test_android_wake_bridge import source
from test_policy import config


def bridge(tmp_path, *, enabled=True, paused_server=False, revoke_during_read=False):
    document = config()
    document['deployment'] = {'modules': ['android', 'messaging'], 'backends': {
        'android': {'url': 'http://android'}, 'messaging': {'url': 'http://messaging'}}}
    document['profiles']['reader']['tools'] = ['android_agent']
    actor = document['principals'][0]
    actor['grants'] = {'android': ['android:read', 'android:control'], 'messaging': []}
    actor['credentials'] = {}
    for module in ('android', 'messaging'):
        key = tmp_path / (module + '.key')
        key.write_text('synthetic-' + module)
        key.chmod(0o600)
        actor['credentials'][module] = {'key_file': str(key), 'key_sha256': hashlib.sha256(key.read_bytes()).hexdigest()}
    document['android_wakes'] = [{'principal_id': actor['id'], 'owner_id': 'owner', 'device_id': 'phone',
        'source_id': 'native', 'auth_epoch': 1, 'enabled': enabled}]
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    policy = Policy(path)
    raw = source(owner_words='MUST_NOT_LEAVE_ANDROID', questions=[{'title': 'Which address?', 'options': None}])
    calls = []
    saved = {}
    class Clients:
        deployment = policy.load()['deployment']
        async def call(self, module, operation, key, body):
            assert key == 'synthetic-' + module
            calls.append((module, operation, body))
            if operation == 'android-wakes/state':
                return {'status': 'unavailable', 'reason': 'android_wake_paused'} if paused_server else {'after': 0}
            if operation == 'order_source':
                if revoke_during_read:
                    document['revision'] += 1
                    document['principals'][0]['enabled'] = False
                    path.write_text(json.dumps(document))
                return {'journal_id': 'original-journal', 'after': 0, 'next_after': 1, 'events': [raw], 'gap': False}
            if operation == 'android-wakes/receipt':
                return saved.get(body['wake_id'], {'transport_state': 'not_found'})
            assert operation == 'android-wakes/stage'
            for event in body['events']:
                saved[event['wake_id']] = {'wake_id': event['wake_id'], 'transport_state': 'pending',
                    'native_status': 'not_observed'}
            return {'items': list(saved.values()), 'after': body['next_after']}
    result = SimpleNamespace(policy=policy, clients=Clients())
    result.call_credential = lambda snapshot, principal, module: Dispatch.call_credential(result, snapshot, principal, module)
    return result, calls, raw


# spec: ANYM-997
def test_configured_private_producer_uses_existing_credentials_and_projects_metadata_only(tmp_path):
    dispatch, calls, raw = bridge(tmp_path)
    status, = asyncio.run(wake_step(dispatch))
    assert status['status'] == 'staged'
    source_call = next(body for module, op, body in calls if op == 'order_source')
    assert source_call == {'device_id': 'phone', 'source_id': 'native', 'auth_epoch': 1, 'after': 0, 'limit': 4}
    staged = next(body for module, op, body in calls if op == 'android-wakes/stage')
    event, = staged['events']
    assert event['request_id'] == raw['request_id'] and event['task_id'] == raw['task_id']
    assert event['native_request_id'] is None and event['question_ids'] == [] and event['order_id'] is None
    assert not {'questions', 'owner_words', 'secret'} & event.keys()
    assert 'MUST_NOT_LEAVE_ANDROID' not in json.dumps(staged)
    assert status['receipt']['items'][0]['native_status'] == 'not_observed'


# spec: ANYM-997
def test_default_paused_route_never_reads_backend_keys_or_makes_calls(tmp_path):
    dispatch, calls, _ = bridge(tmp_path, enabled=False)
    for key in tmp_path.glob('*.key'):
        key.unlink()
    status, = asyncio.run(wake_step(dispatch))
    assert status == {'status': 'unavailable', 'reason': 'android_wake_paused'} and calls == []


# spec: ANYM-997
def test_missing_actual_server_binding_stops_before_android_source_or_staging(tmp_path):
    dispatch, calls, _ = bridge(tmp_path, paused_server=True)
    status, = asyncio.run(wake_step(dispatch))
    assert status['reason'] == 'android_wake_paused'
    assert [op for _, op, _ in calls] == ['android-wakes/state']


# spec: ANYM-997
def test_policy_revocation_during_source_wait_refuses_before_ingestion(tmp_path):
    dispatch, calls, _ = bridge(tmp_path, revoke_during_read=True)
    status, = asyncio.run(wake_step(dispatch))
    assert status['status'] == 'held'
    assert not any(op == 'android-wakes/stage' for _, op, _ in calls)
