"""Exact caller input at the engine protocol boundary; synthetic engine and phone only."""

import json
import sqlite3

import pytest

from agent_harness import Service, recorded
from test_agent_bare import bare_host
from test_codex_session import calls, live_host


# spec: ANYM-993
@pytest.mark.parametrize('persistent', [False, True])
def test_exact_nonsecret_input_and_provenance_reach_engine(tmp_path, persistent):
    config, record = (live_host if persistent else bare_host)(tmp_path)
    fields = dict(goal='Read basket\nthen ask which address.',
                  owner_instruction='Не выбирай товар до уточнения.\nKeep courier contact until received.',
                  params={'COUNT': 2, 'CONFIRMED': False, 'NOTE': 'Keep "original"\nwords'},
                  apps=['example.food'], outputs=['total'], max_minutes=3, max_actions=20)
    with Service(config) as service:
        session = service.lease()
        start = service.start(session, **fields)
        assert service.finished(start['request_id'])['state'] == 'succeeded'
        if persistent:
            turn, = [row['message'] for row in calls(record) if row['message']['method'] == 'turn/start']
            prompt = turn['params']['input'][0]['text']
            assert turn['params']['approvalPolicy'] == 'never'
        else:
            prompt = recorded(record)['stdin']
        body = json.loads(prompt.split('Caller input (nonsecret JSON):\n', 1)[1])
        assert body == {key: value for key, value in fields.items() if key not in {'max_minutes', 'max_actions'}}
        assert type(body['params']['COUNT']) is int and type(body['params']['CONFIRMED']) is bool
        assert session not in prompt  # device lease token is transport-only
        with sqlite3.connect(tmp_path / 'state' / 'state.sqlite3') as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM agent_runs WHERE owner=? AND id=?', ('one', start['request_id'])).fetchone()
        saved = json.loads(row['request'])
        assert row['owner'] == 'one' and row['device'] == 'home'
        assert saved['request_id'] == start['request_id']
        assert all(saved[key] == value for key, value in fields.items())


# spec: ANYM-993
def test_changed_parameter_type_conflicts_without_another_native_turn(tmp_path):
    config, record = live_host(tmp_path)
    with Service(config) as service:
        session = service.lease()
        first = service.start(session, params={'COUNT': 2})
        service.finished(first['request_id'])
        replay = service.start(session, request_id=first['request_id'], params={'COUNT': 2})
        assert replay['request_id'] == first['request_id']
        refused = service.start(session, request_id=first['request_id'], params={'COUNT': '2'}, code=409)
        assert refused['error'] == 'request_id_conflict'
        assert len([row for row in calls(record) if row['message']['method'] == 'turn/start']) == 1
