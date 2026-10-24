"""Messaging inventory and key-file lookup for the admin CLI (ANYM-494): public fields only, never a digest."""
import json
import os
import sys

import pytest

from messaging_service import __main__ as operator
from messaging_service.store import digest


@pytest.fixture
def run(seeded, monkeypatch, capsys):
    monkeypatch.setenv('DATABASE_URL_FILE', os.environ['PROBE_TEST_DSN_FILE'])

    def invoke(*arguments):
        monkeypatch.setattr(sys, 'argv', ['anymsg', *arguments])
        operator.main()
        out = capsys.readouterr()
        return json.loads(out.out), out.out+out.err
    return invoke


def key_ids(store):
    with store.pool.connection() as c:
        return {row['principal']: str(row['id']) for row in c.execute('SELECT principal,id FROM api_keys')}


# spec: ANYM-494
def test_list_keys_all_lists_every_principals_keys_with_public_fields_only(seeded, run):
    store, one, two = seeded
    ids = key_ids(store)
    store.revoke(two)
    rows, text = run('list-keys', '--all')
    assert [(row['principal'], row['id']) for row in rows] == [('agent-a', ids['agent-a']), ('agent-b', ids['agent-b'])]
    assert all(set(row) == {'id', 'principal', 'created_at', 'expires_at', 'revoked'} for row in rows)
    assert [row['revoked'] for row in rows] == [False, True]
    # created_at is the operator's issue_key audit row.
    with store.pool.connection() as c:
        issued = c.execute("SELECT min(occurred_at) AS at FROM audit_log WHERE operation='issue_key' AND key_id=%s",
                           (ids['agent-a'],)).fetchone()['at']
    assert rows[0]['created_at'] == issued.isoformat()
    for secret in (one, two, digest(one), digest(two)):
        assert secret not in text


# spec: ANYM-494
def test_key_info_answers_each_line_of_a_key_file_in_order_without_the_token_or_digest(seeded, run, tmp_path):
    store, one, two = seeded
    ids = key_ids(store)
    batch = tmp_path/'batch'
    batch.write_text(f'{two}\npos_probe_{"x"*40}\nnot a key\n{one}\n')
    reply, text = run('key-info', '--key-file', str(batch))
    keys = reply['keys']
    assert [k['found'] for k in keys] == [True, False, False, True]
    assert (keys[0]['key_id'], keys[0]['principal'], keys[0]['revoked']) == (ids['agent-b'], 'agent-b', False)
    assert set(keys[0]) == {'found', 'key_id', 'principal', 'expires_at', 'revoked'}
    assert keys[1] == {'found': False} and keys[2] == {'found': False, 'error': 'invalid_key'}
    assert keys[3]['key_id'] == ids['agent-a']
    for secret in (one, two, digest(one), digest(two)):
        assert secret not in text


# spec: ANYM-494
@pytest.mark.parametrize('arguments', [['list-keys'], ['list-keys', '--all', '--principal', 'agent-a'], ['key-info']])
def test_inventory_commands_refuse_ambiguous_selectors(seeded, run, arguments):
    with pytest.raises(SystemExit) as done:
        run(*arguments)
    assert done.value.code == 2
