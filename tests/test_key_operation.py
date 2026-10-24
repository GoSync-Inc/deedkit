"""Repeatable messaging key issue (ANYM-495): caller token file, operation ID replay and conflict, check mode."""
import json
import os
from pathlib import Path
import sys

import psycopg
import pytest

from messaging_service import __main__ as operator
from messaging_service import store as store_module
from messaging_service.store import digest

TOKEN = 'pos_probe_' + 'a1' * 24


@pytest.fixture
def run(seeded, monkeypatch, capsys):
    monkeypatch.setenv('DATABASE_URL_FILE', os.environ['PROBE_TEST_DSN_FILE'])

    def invoke(*arguments):
        monkeypatch.setattr(sys, 'argv', ['anymsg', *arguments])
        operator.main()
        out = capsys.readouterr()
        return json.loads(out.out), out.out+out.err
    return invoke


def token_file(tmp_path, value=TOKEN, name='agent.token', mode=0o600):
    path = tmp_path/name
    path.write_text(value+'\n')
    path.chmod(mode)
    return path


def rows(store):
    with store.pool.connection() as c:
        keys = c.execute('SELECT id,principal,digest,operation_id,operation_request FROM api_keys ORDER BY id').fetchall()
        audit = c.execute('SELECT count(*) AS n FROM audit_log').fetchone()['n']
    return keys, audit


def issue(run, path, operation, principal='agent-c', *extra):
    return run('issue-key', '--principal', principal, '--accounts', 'telegram-demo', '--token-file', str(path),
               '--operation-id', operation, *extra)


# spec: ANYM-495
def test_first_call_stores_only_the_digest_of_the_callers_token_and_prints_public_fields(seeded, run, tmp_path):
    store = seeded[0]
    path = token_file(tmp_path)
    before = path.read_bytes()
    reply, text = issue(run, path, 'op-1')
    assert set(reply) == {'key_id', 'principal', 'expires_at', 'revoked', 'operation_id', 'replayed'}
    assert (reply['principal'], reply['revoked'], reply['operation_id'], reply['replayed']) == (
        'agent-c', False, 'op-1', False)
    keys, _ = rows(store)
    issued = [k for k in keys if k['operation_id'] == 'op-1']
    assert len(issued) == 1 and str(issued[0]['id']) == reply['key_id'] and issued[0]['digest'] == digest(TOKEN)
    assert path.read_bytes() == before and TOKEN not in text and digest(TOKEN) not in text
    assert store.key_info([TOKEN])[0]['key_id'] == reply['key_id']


# spec: ANYM-495
def test_the_same_operation_and_request_replays_the_record_and_creates_nothing(seeded, run, tmp_path):
    store = seeded[0]
    path = token_file(tmp_path)
    first, _ = issue(run, path, 'op-1')
    keys, audit = rows(store)
    again, _ = issue(run, path, 'op-1')
    assert again == dict(first, replayed=True)
    assert rows(store) == (keys, audit)


# spec: ANYM-495
@pytest.mark.parametrize('change', ['days', 'accounts', 'principal', 'token'])
def test_the_same_operation_with_another_request_or_token_is_refused_as_a_conflict(seeded, run, tmp_path, change):
    store = seeded[0]
    issue(run, token_file(tmp_path), 'op-1')
    keys, audit = rows(store)
    path, extra, principal = token_file(tmp_path), [], 'agent-c'
    if change == 'days':
        extra = ['--days', '30']
    elif change == 'principal':
        principal = 'agent-d'
    elif change == 'token':
        path = token_file(tmp_path, 'pos_probe_' + 'b2' * 24, 'other.token')
    with pytest.raises(ValueError, match='^operation_conflict$'):
        if change == 'accounts':
            run('issue-key', '--principal', 'agent-c', '--accounts', 'telegram-demo', 'whatsapp-demo-1',
                '--token-file', str(path), '--operation-id', 'op-1')
        else:
            issue(run, path, 'op-1', principal, *extra)
    assert rows(store) == (keys, audit)


# spec: ANYM-495
def test_a_token_already_stored_under_another_operation_is_refused_and_creates_nothing(seeded, run, tmp_path):
    store = seeded[0]
    path = token_file(tmp_path)
    issue(run, path, 'op-1')
    keys, audit = rows(store)
    with pytest.raises(ValueError, match='^token_in_use$'):
        issue(run, path, 'op-2', 'agent-d')
    assert rows(store) == (keys, audit)


# spec: ANYM-495
def test_one_parent_operation_issues_main_and_archive_keys_and_each_replays_after_both_commits(seeded, run, tmp_path):
    store = seeded[0]
    main = token_file(tmp_path, 'pos_probe_' + 'c3' * 24, 'main.token')
    archive = token_file(tmp_path, 'pos_probe_' + 'd4' * 24, 'archive.token')
    first, _ = issue(run, main, 'issue-7/messaging', 'agent-c')
    second, _ = issue(run, archive, 'issue-7/archive', 'agent-c-calls-archive')
    assert first['key_id'] != second['key_id']
    keys, audit = rows(store)
    assert issue(run, archive, 'issue-7/archive', 'agent-c-calls-archive')[0] == dict(second, replayed=True)
    assert issue(run, main, 'issue-7/messaging', 'agent-c')[0] == dict(first, replayed=True)
    assert rows(store) == (keys, audit)


# spec: ANYM-495
def test_a_crash_after_the_commit_then_a_replay_returns_the_committed_key(seeded, run, tmp_path, monkeypatch):
    store = seeded[0]
    path = token_file(tmp_path)

    def crash(value):
        raise SystemExit('killed after commit')
    monkeypatch.setattr(store_module, 'serial', crash)
    with pytest.raises(SystemExit):
        issue(run, path, 'op-1')
    monkeypatch.undo()
    monkeypatch.setenv('DATABASE_URL_FILE', os.environ['PROBE_TEST_DSN_FILE'])
    keys, audit = rows(store)
    committed = [k for k in keys if k['operation_id'] == 'op-1']
    assert len(committed) == 1
    reply, _ = issue(run, path, 'op-1')
    assert reply['replayed'] is True and reply['key_id'] == str(committed[0]['id'])
    assert rows(store) == (keys, audit)


# spec: ANYM-495
def test_key_info_by_operation_id_reads_back_public_fields_only(seeded, run, tmp_path):
    issued, _ = issue(run, token_file(tmp_path), 'op-1')
    reply, text = run('key-info', '--operation-id', 'op-1')
    assert reply == {'keys': [{'found': True, 'key_id': issued['key_id'], 'principal': 'agent-c',
                               'expires_at': issued['expires_at'], 'revoked': False, 'operation_id': 'op-1'}]}
    assert run('key-info', '--operation-id', 'op-unknown')[0] == {'keys': [{'found': False,
                                                                            'operation_id': 'op-unknown'}]}
    assert TOKEN not in text and digest(TOKEN) not in text


# spec: ANYM-495
@pytest.mark.parametrize('problem,code', [('group-readable', 'unsafe_token_file'), ('two-lines', 'invalid_key'),
                                          ('short', 'invalid_key'), ('operation', 'invalid_operation_id')])
def test_an_unsafe_token_file_or_a_bad_operation_id_is_refused_before_any_write(seeded, run, tmp_path, problem, code):
    store = seeded[0]
    before = rows(store)
    path = token_file(tmp_path, mode=0o640 if problem == 'group-readable' else 0o600)
    if problem == 'two-lines':
        path.write_text(TOKEN+'\n'+TOKEN+'\n')
    elif problem == 'short':
        path.write_text('pos_probe_short\n')
    with pytest.raises(ValueError, match='^'+code+'$'):
        issue(run, path, 'bad id!' if problem == 'operation' else 'op-1')
    assert rows(store) == before


# spec: ANYM-495
@pytest.mark.parametrize('arguments', [
    ['--token-file', 'agent.token'],
    ['--operation-id', 'op-1', '--key-file', 'new.key'],
    ['--token-file', 'agent.token', '--operation-id', 'op-1', '--key-file', 'new.key'],
], ids=['token-without-operation', 'operation-with-key-file', 'both-files'])
def test_token_file_and_operation_id_go_together_and_exclude_key_file(seeded, run, tmp_path, monkeypatch, arguments):
    monkeypatch.chdir(tmp_path)
    token_file(tmp_path)
    with pytest.raises(SystemExit):
        run('issue-key', '--principal', 'agent-c', '--accounts', 'telegram-demo', *arguments)
    assert not (tmp_path/'new.key').exists()


def set_schema(store, version):
    with store.pool.connection() as c:
        c.execute("UPDATE probe_metadata SET value=%s WHERE name='schema_version'", (str(version),))


CHECKS = {
    'issue-key': lambda ids, path: ['--principal', 'agent-c', '--accounts', 'telegram-demo', '--token-file', str(path),
                                    '--operation-id', 'op-1'],
    'extend-key': lambda ids, path: ['--key-id', ids['agent-a'], '--days', '30'],
    'revoke-key': lambda ids, path: ['--key-id', ids['agent-a']],
    'list-keys': lambda ids, path: ['--all'],
    'key-info': lambda ids, path: ['--operation-id', 'op-1'],
}


# spec: ANYM-495
@pytest.mark.parametrize('command', list(CHECKS))
@pytest.mark.parametrize('version,answer', [(44, 'ok'), (43, 'schema_not_ready')], ids=['current', 'n-1'])
def test_check_validates_and_reports_the_schema_and_writes_nothing(seeded, run, tmp_path, command, version, answer):
    store = seeded[0]
    with store.pool.connection() as c:
        ids = {r['principal']: str(r['id']) for r in c.execute('SELECT principal,id FROM api_keys')}
    path = token_file(tmp_path)
    before = rows(store)
    set_schema(store, version)
    try:
        reply, _ = run(command, *CHECKS[command](ids, path), '--check')
    finally:
        set_schema(store, 44)
    assert reply == {'check': answer, 'schema_version': version, 'required': 44}
    assert rows(store) == before


# spec: ANYM-495
def test_issue_by_operation_on_an_n_minus_1_schema_is_refused_before_any_write(seeded, run, tmp_path):
    store = seeded[0]
    before = rows(store)
    set_schema(store, 43)
    try:
        with pytest.raises(RuntimeError, match='^schema_not_ready$'):
            issue(run, token_file(tmp_path), 'op-1')
    finally:
        set_schema(store, 44)
    assert rows(store) == before


# spec: ANYM-495
def test_migration_044_is_additive_and_a_second_migrate_changes_nothing(seeded):
    store = seeded[0]
    keys, _ = rows(store)
    assert keys and all(k['operation_id'] is None and k['operation_request'] is None for k in keys)
    store.migrate()
    assert rows(store)[0] == keys
    with store.pool.connection() as c:
        columns = {r['column_name']: r['is_nullable'] for r in c.execute(
            "SELECT column_name,is_nullable FROM information_schema.columns WHERE table_name='api_keys' "
            "AND column_name IN ('operation_id','operation_request')")}
    assert columns == {'operation_id': 'YES', 'operation_request': 'YES'}
    with pytest.raises(psycopg.errors.CheckViolation), store.pool.connection() as c:
        c.execute("UPDATE api_keys SET operation_id='only-half' WHERE id=%s", (keys[0]['id'],))
    assert Path(store_module.__file__).with_name('migrations').joinpath('044_key_operation_id.sql').exists()
