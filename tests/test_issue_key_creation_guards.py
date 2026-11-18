"""issue-key through the operator CLI (ANYM-11): a new key file is created exclusively 0600 and an existing one is
never overwritten, a new principal gets the default permissions and every chat of its accounts, and an unknown
account or a chat outside the granted accounts is refused before anything is written."""
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
        try:
            operator.main()
        finally:
            out = capsys.readouterr()
            invoke.output = out.out + out.err
        return json.loads(out.out)
    return invoke


def state(store):
    with store.pool.connection() as c:
        principals = c.execute('SELECT id,accounts,chat_ids,permissions,scope_version FROM principals ORDER BY id').fetchall()
        keys = c.execute('SELECT id,principal,digest FROM api_keys ORDER BY id').fetchall()
    return principals, keys


def issue(run, path, *extra, principal='agent-new'):
    return run('issue-key', '--principal', principal, '--accounts', 'telegram-demo', '--key-file', str(path), *extra)


# spec: ANYM-11
def test_a_new_principal_gets_the_default_permissions_all_chats_and_a_private_key_file(seeded, run, tmp_path):
    store = seeded[0]
    path = tmp_path / 'agent.key'
    reply = issue(run, path)
    assert reply == {'issued': True, 'principal': 'agent-new', 'key_file': str(path)}
    token = path.read_text().strip()
    assert path.stat().st_mode & 0o777 == 0o600 and token and token not in run.output
    principals, keys = state(store)
    row = next(p for p in principals if p['id'] == 'agent-new')
    assert row['permissions'] == ['cursor-write', 'download', 'read'] and row['chat_ids'] is None
    assert [k['digest'] for k in keys if k['principal'] == 'agent-new'] == [digest(token)]
    assert store.capabilities(token)['permissions'] == ['cursor-write', 'download', 'read']


# spec: ANYM-11
@pytest.mark.parametrize('existing', ['file', 'symlink', 'dangling-symlink'])
def test_an_existing_key_file_is_never_overwritten_and_no_key_is_issued(seeded, run, tmp_path, existing):
    store = seeded[0]
    path = tmp_path / 'agent.key'
    target = tmp_path / 'previous.key'
    if existing == 'file':
        path.write_text('previous credential\n')
    else:
        if existing == 'symlink':
            target.write_text('previous credential\n')
        path.symlink_to(target)
    before = state(store)
    with pytest.raises(ValueError, match='^key_file_exists$'):
        issue(run, path, '--check')
    with pytest.raises(FileExistsError):
        issue(run, path)
    assert state(store) == before
    if existing == 'dangling-symlink':
        assert path.is_symlink() and not target.exists()
    else:
        assert (path if existing == 'file' else target).read_text() == 'previous credential\n'


# spec: ANYM-11
@pytest.mark.parametrize('extra,code', [
    (('--chats', 'whatsapp-demo-1-chat'), 'unknown_chat_scope'),
    (('--chats', 'telegram-demo-chat', 'no-such-chat'), 'unknown_chat_scope'),
    (('--permissions', 'read', 'arbitrary-admin'), None),  # the CLI's own choices refuse it first (exit 2)
], ids=['chat-of-another-account', 'unknown-chat', 'unknown-permission'])
def test_a_scope_outside_the_granted_accounts_is_refused_before_any_write(seeded, run, tmp_path, extra, code):
    store = seeded[0]
    before = state(store)
    for check in (('--check',), ()):
        path = tmp_path / ('check.key' if check else 'agent.key')
        with pytest.raises(ValueError if code else SystemExit, match='^' + code + '$' if code else '^2$'):
            issue(run, path, *extra, *check)
        assert state(store) == before
        assert not path.exists() or path.read_text() == ''


# spec: ANYM-11
def test_an_unknown_account_is_refused_before_any_write(seeded, run, tmp_path):
    store = seeded[0]
    before = state(store)
    with pytest.raises(ValueError, match='^unknown_account_scope$'):
        run('issue-key', '--principal', 'agent-new', '--accounts', 'telegram-demo', 'no-such-account',
            '--key-file', str(tmp_path / 'agent.key'))
    assert state(store) == before
