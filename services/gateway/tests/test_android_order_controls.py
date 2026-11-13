"""Exact wake-bound Android metadata routes through the existing MCP tool."""

import uuid

import pytest
from starlette.testclient import TestClient

from test_android import setup
from test_android_agent import call


# spec: ANYM-993, ANYM-997
@pytest.mark.parametrize('action', ['fetch', 'read_receipt', 'handoff'])
def test_wake_actions_forward_only_original_wake_scope_without_invented_task_id(tmp_path, action):
    app, calls = setup(tmp_path)
    args = {'action': action, 'wake_id': str(uuid.uuid4())}
    if action != 'fetch':
        args['wake_digest'] = 'a' * 64
    if action == 'handoff':
        args.update(snapshot_digest='b' * 64, next_check_at=1999999999)
    with TestClient(app) as client:
        result = call(client, **args)
        assert not result.get('isError'), result
        assert calls == [('android', 'agent', args)]
        assert 'request_id' not in calls[0][2]


# spec: ANYM-993, ANYM-997
@pytest.mark.parametrize('args', [
    {'action': 'get'}, {'action': 'fetch', 'wake_id': 'wake', 'request_id': 'invented-task'},
    {'action': 'read_receipt', 'wake_id': 'wake'},
    {'action': 'handoff', 'wake_id': 'wake', 'wake_digest': 'a' * 64},
])
def test_invalid_wake_or_missing_original_request_is_refused_before_backend(tmp_path, args):
    app, calls = setup(tmp_path)
    with TestClient(app) as client:
        assert call(client, **args)['isError']
        assert calls == []


# spec: ANYM-993, ANYM-997
@pytest.mark.parametrize('action', ['read_receipt', 'handoff'])
def test_unknown_metadata_response_directs_fetch_of_original_receipt_and_never_resends(tmp_path, action):
    app, calls = setup(tmp_path, interrupted=True)
    args = dict(action=action, wake_id=str(uuid.uuid4()), wake_digest='a' * 64)
    if action == 'handoff':
        args.update(snapshot_digest='b' * 64, next_check_at=1999999999)
    with TestClient(app) as client:
        result = call(client, **args)
        assert result['isError']
        receipt = result['structuredContent']
        assert receipt['wake_id'] == args['wake_id'] and receipt['wake_digest'] == args['wake_digest']
        assert receipt['mutation_scope'] == 'android_metadata'
        assert receipt['next_action'] == 'fetch_original_wake_receipt' and 'request_id' not in receipt
        assert len(calls) == 1


# spec: ANYM-993, ANYM-997
def test_fetch_transport_loss_is_not_reported_as_unknown_mutation(tmp_path):
    app, calls = setup(tmp_path, interrupted=True)
    with TestClient(app) as client:
        result = call(client, action='fetch', wake_id=str(uuid.uuid4()))
        assert result['isError'] and result['structuredContent']['error'] != 'mutation_outcome_unknown'
        assert len(calls) == 1
