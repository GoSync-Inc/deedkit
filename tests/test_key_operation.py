"""Repeatable messaging key issue (ANYM-495): caller token file, operation ID replay and conflict, check mode."""
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import sys
import threading
import time

import psycopg
import pytest

from messaging_service import __main__ as operator
from messaging_service import store as store_module
from messaging_service.store import digest

TOKEN = 'pos_probe_' + 'a1' * 24
MIGRATION_044 = Path(store_module.__file__).with_name('migrations')/'044_key_operation_id.sql'


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
        keys = c.execute('''SELECT k.id,k.principal,k.digest,o.operation_id,o.operation_request FROM api_keys k
            LEFT JOIN api_key_operations o ON o.key_id=k.id ORDER BY k.id''').fetchall()
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
@pytest.mark.parametrize('change', ['days', 'accounts', 'principal', 'token', 'chats', 'permissions',
                                    'key-permissions'])
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
    elif change == 'chats':
        extra = ['--all-chats']
    elif change in ('permissions', 'key-permissions'):
        extra = ['--'+change, 'read']
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


@contextmanager
def schema_43(store):
    # A real schema-43 shape (no ANYM-495 table), then migration 044 again.
    with store.pool.connection() as c:
        c.execute('DROP TABLE api_key_operations')
        c.execute("UPDATE probe_metadata SET value='43' WHERE name='schema_version'")
    try:
        yield
    finally:
        with store.pool.connection() as c:
            c.execute(MIGRATION_044.read_text())
            c.execute("UPDATE probe_metadata SET value='50' WHERE name='schema_version'")


def rows_any(store):
    with store.pool.connection() as c:
        keys = c.execute('SELECT to_jsonb(k) AS k FROM api_keys k ORDER BY id').fetchall()
        audit = c.execute('SELECT count(*) AS n FROM audit_log').fetchone()['n']
    return keys, audit


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
@pytest.mark.parametrize('version,answer', [(50, 'ok'), (43, 'schema_not_ready')], ids=['current', 'n-1'])
def test_check_validates_and_reports_the_schema_and_writes_nothing(seeded, run, tmp_path, command, version, answer):
    store = seeded[0]
    with store.pool.connection() as c:
        ids = {r['principal']: str(r['id']) for r in c.execute('SELECT principal,id FROM api_keys')}
    path = token_file(tmp_path)
    with schema_43(store) if version == 43 else nullcontext():
        before = rows_any(store)
        reply, _ = run(command, *CHECKS[command](ids, path), '--check')
        assert rows_any(store) == before
    assert reply == {'check': answer, 'schema_version': version, 'required': 44}


# spec: ANYM-495
def test_issue_by_operation_on_an_n_minus_1_schema_is_refused_before_any_write(seeded, run, tmp_path):
    store = seeded[0]
    with schema_43(store):
        before = rows_any(store)
        with pytest.raises(RuntimeError, match='^schema_not_ready$'):
            issue(run, token_file(tmp_path), 'op-1')
        assert rows_any(store) == before


# spec: ANYM-495
def test_migration_044_is_additive_and_a_second_migrate_changes_nothing(seeded):
    store = seeded[0]
    keys = rows_any(store)
    with store.pool.connection() as c:
        # 044 takes no lock on api_keys, which every authentication reads: no foreign key, no change to it.
        c.execute('DROP TABLE api_key_operations')
        c.execute(MIGRATION_044.read_text())
        locks = c.execute("SELECT mode FROM pg_locks WHERE pid=pg_backend_pid() AND relation='api_keys'::regclass"
                          ).fetchall()
    assert locks == []
    store.migrate()
    assert rows_any(store) == keys
    with store.pool.connection() as c:
        foreign = c.execute("SELECT count(*) AS n FROM pg_constraint WHERE contype='f' "
                            "AND conrelid='api_key_operations'::regclass").fetchone()['n']
    assert foreign == 0
    with pytest.raises(psycopg.errors.CheckViolation), store.pool.connection() as c:
        c.execute("INSERT INTO api_key_operations VALUES('bad id',%s,gen_random_uuid())", ('0' * 64,))
    assert MIGRATION_044.read_text().startswith('-- rollout: expand\n')


# spec: ANYM-495
@pytest.mark.parametrize('problem,code', [('unknown-account', 'unknown_account_scope'), ('token-in-use', 'token_in_use'),
                                          ('conflict', 'operation_conflict')])
def test_issue_check_runs_the_issue_validation_and_writes_nothing(seeded, run, tmp_path, problem, code):
    store = seeded[0]
    issue(run, token_file(tmp_path), 'op-1')
    before = rows(store)
    path, operation, accounts = token_file(tmp_path, 'pos_probe_' + 'e5' * 24, 'new.token'), 'op-2', ['telegram-demo']
    if problem == 'unknown-account':
        accounts = ['no-such-account']
    elif problem == 'token-in-use':
        path = token_file(tmp_path)
    else:
        operation = 'op-1'
    with pytest.raises(ValueError) as failure:
        run('issue-key', '--principal', 'agent-d', '--accounts', *accounts, '--token-file', str(path),
            '--operation-id', operation, '--check')
    assert str(failure.value) == code
    assert rows(store) == before


# spec: ANYM-495
@pytest.mark.parametrize('problem,code', [('missing', None), ('too-large', 'key_file_too_large'),
                                          ('empty', 'invalid_key_count')])
def test_key_info_check_reads_and_bounds_the_key_file(seeded, run, tmp_path, problem, code):
    path = tmp_path/'keys.txt'
    if problem == 'too-large':
        path.write_text((TOKEN+'\n')*2000)
    elif problem == 'empty':
        path.write_text('')
    with pytest.raises(FileNotFoundError if code is None else ValueError) as failure:
        run('key-info', '--key-file', str(path), '--check')
    if code:
        assert str(failure.value) == code


def blocked_by(dsn, pid):
    # True once another backend waits on a lock that `pid` holds; polled in autocommit, since a transaction
    # keeps one pg_stat_activity snapshot.
    deadline = time.monotonic() + 10
    with psycopg.connect(dsn, autocommit=True) as c:
        while time.monotonic() < deadline:
            if c.execute("SELECT 1 FROM pg_stat_activity WHERE wait_event_type='Lock' AND %s=ANY(pg_blocking_pids(pid))",
                         (pid,)).fetchone():
                return True
            time.sleep(0.05)
    return False


def race(store, monkeypatch, first, second):
    # The first issue pauses after its INSERT, inside its transaction; the second starts and is seen waiting on the
    # first's lock, then the first commits.
    paused, release, original, results, winner = threading.Event(), threading.Event(), store_module.audit, {}, {}

    def audit(c, principal, operation, *args, **kwargs):
        if operation == 'issue_key' and not paused.is_set():
            winner['pid'] = c.info.backend_pid
            paused.set()
            assert release.wait(30)
        return original(c, principal, operation, *args, **kwargs)
    monkeypatch.setattr(store_module, 'audit', audit)

    def call(name, kwargs):
        try:
            results[name] = store.issue_key(accounts=['telegram-demo'], **kwargs)
        except Exception as error:
            results[name] = error
    one = threading.Thread(target=call, args=('first', first))
    one.start()
    assert paused.wait(30)
    two = threading.Thread(target=call, args=('second', second))
    two.start()
    try:
        assert blocked_by(store.pool.conninfo, winner['pid'])
    finally:
        release.set()
    one.join(30)
    two.join(30)
    return results


# spec: ANYM-495
def test_a_concurrent_identical_issue_waits_and_replays_the_winner(seeded, monkeypatch):
    store = seeded[0]
    request = dict(principal='agent-c', token=TOKEN, operation_id='op-race')
    results = race(store, monkeypatch, request, request)
    assert results['first']['replayed'] is False and results['second']['replayed'] is True
    assert results['second']['key_id'] == results['first']['key_id']
    assert [k['operation_id'] for k in rows(store)[0]].count('op-race') == 1


# spec: ANYM-495
@pytest.mark.parametrize('collision,code', [('operation', 'operation_conflict'), ('token', 'token_in_use')])
def test_a_concurrent_issue_that_loses_the_operation_or_token_is_refused_without_a_unique_violation(
        seeded, monkeypatch, collision, code):
    store = seeded[0]
    first = dict(principal='agent-c', token=TOKEN, operation_id='op-race')
    second = dict(principal='agent-d', token='pos_probe_' + 'f6' * 24 if collision == 'operation' else TOKEN,
                  operation_id='op-race' if collision == 'operation' else 'op-other')
    results = race(store, monkeypatch, first, second)
    assert results['first']['replayed'] is False
    assert isinstance(results['second'], ValueError) and str(results['second']) == code
    keys = rows(store)[0]
    assert [k['principal'] for k in keys if k['principal'] in ('agent-c', 'agent-d')] == ['agent-c']


# spec: ANYM-495
def test_a_crash_between_the_parent_operations_two_commits_then_a_rerun_completes_it(seeded, run, tmp_path):
    store = seeded[0]
    main = token_file(tmp_path, 'pos_probe_' + 'c3' * 24, 'main.token')
    archive = token_file(tmp_path, 'pos_probe_' + 'd4' * 24, 'archive.token')
    first, _ = issue(run, main, 'issue-8/messaging', 'agent-c')
    # The parent operation stops here; a rerun replays the first key and issues the second once.
    assert issue(run, main, 'issue-8/messaging', 'agent-c')[0] == dict(first, replayed=True)
    second, _ = issue(run, archive, 'issue-8/archive', 'agent-c-calls-archive')
    assert second['replayed'] is False
    assert issue(run, archive, 'issue-8/archive', 'agent-c-calls-archive')[0] == dict(second, replayed=True)
    operations = sorted(k['operation_id'] for k in rows(store)[0] if k['operation_id'])
    assert operations == ['issue-8/archive', 'issue-8/messaging']


# spec: ANYM-495
@pytest.mark.parametrize('problem,code', [('principal', 'invalid_principal'), ('days', 'invalid_key_lifetime'),
                                          ('operation', 'invalid_operation_id')])
def test_issue_check_on_an_n_minus_1_schema_still_validates_the_arguments(seeded, run, tmp_path, problem, code):
    store = seeded[0]
    principal, extra, operation = 'agent-c', [], 'op-1'
    if problem == 'principal':
        principal = 'Bad Principal!'
    elif problem == 'days':
        extra = ['--days', '400']
    else:
        operation = 'bad id!'
    with schema_43(store):
        with pytest.raises(ValueError) as failure:
            issue(run, token_file(tmp_path), operation, principal, *extra, '--check')
    assert str(failure.value) == code


# spec: ANYM-495
@pytest.mark.parametrize('content', ['short', ''])
def test_revoke_check_refuses_the_key_file_that_revoke_refuses(seeded, run, tmp_path, content):
    store = seeded[0]
    path = tmp_path/'revoke.txt'
    path.write_text(content+'\n')
    before = rows_any(store)
    for extra in (['--check'], []):
        with pytest.raises(ValueError) as failure:
            run('revoke-key', '--key-file', str(path), *extra)
        assert str(failure.value) == 'invalid_key'
    assert rows_any(store) == before
