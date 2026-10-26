"""Messaging `principal-info` for the admin CLI's clone (ANYM-496): the scope and each key's public fields."""
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


# spec: ANYM-496
def test_principal_info_reports_the_scope_and_key_permissions_without_a_token_or_digest(seeded, run):
    store, one, two = seeded
    store.set_access('agent-a', permissions=['download', 'read'])
    narrow = store.issue_key('agent-a', ['telegram-demo', 'whatsapp-demo-1'], key_permissions=['read'])
    reply, text = run('principal-info', '--principal', 'agent-a')
    assert {k: reply[k] for k in ('found', 'principal', 'enabled', 'chat_ids')} == {
        'found': True, 'principal': 'agent-a', 'enabled': True, 'chat_ids': None}
    assert sorted(reply['accounts']) == ['telegram-demo', 'whatsapp-demo-1']
    assert sorted(reply['permissions']) == ['download', 'read']
    assert len(reply['keys']) == 2 and all(
        set(k) == {'key_id', 'permissions', 'effective_permissions', 'expires_at', 'revoked'} for k in reply['keys'])
    assert ['read'] in [k['permissions'] for k in reply['keys']]
    for secret in (one, two, narrow, digest(one), digest(narrow)):
        assert secret not in text


# spec: ANYM-496
def test_principal_info_answers_an_unknown_principal_and_check_mode_writes_nothing(seeded, run):
    reply, _ = run('principal-info', '--principal', 'agent-new')
    assert reply == {'found': False, 'principal': 'agent-new'}
    reply, _ = run('principal-info', '--principal', 'agent-a', '--check')
    assert reply['check'] == 'ok' and reply['schema_version'] >= reply['required']
    with pytest.raises(SystemExit) as done:
        run('principal-info')
    assert done.value.code == 2


# spec: ANYM-496
def test_principal_info_reports_effective_permissions_of_null_and_implicit_ceilings(seeded):
    store, *_ = seeded
    store.set_access('agent-a', permissions=['read', 'upload'])
    store.issue_key('agent-a', ['telegram-demo', 'whatsapp-demo-1'])
    legacy = store.issue_key('agent-a', ['telegram-demo', 'whatsapp-demo-1'])
    with store.pool.connection() as c:
        c.execute('UPDATE api_keys SET permissions=NULL WHERE digest=%s', (digest(legacy),))
    keys = {k['key_id']: k for k in store.principal_info('agent-a')['keys']}
    # A NULL legacy ceiling never grants a newer capability (upload); an implicit one is cut to the principal's.
    assert [k['effective_permissions'] for k in keys.values() if k['permissions'] is None] == [['read']]
    assert ['read', 'upload'] in [k['effective_permissions'] for k in keys.values()]
    keys = list(keys.values())
    # A clone issued with the effective set explicitly is accepted (no invalid_key_permissions).
    for key in keys:
        store.issue_key('agent-a', ['telegram-demo'], key_permissions=key['effective_permissions'])


# spec: ANYM-496
def test_issue_key_new_principal_refuses_an_existing_principal_and_leaves_its_scope(seeded):
    store, *_ = seeded
    before = store.principal_info('agent-a')
    token = 'pos_probe_' + 'n' * 43
    for check in ((), ('--check',)):
        with pytest.raises(ValueError, match='principal_exists'):
            store.issue_key('agent-a', ['telegram-demo'], 30, permissions=['read'], key_permissions=['read'],
                            token=token, operation_id='op/new-principal', check=bool(check), new_principal=True)
    assert store.principal_info('agent-a') == before
    issued = store.issue_key('agent-fresh', ['telegram-demo'], 30, permissions=['read'], key_permissions=['read'],
                             token=token, operation_id='op/fresh', new_principal=True)
    again = store.issue_key('agent-fresh', ['telegram-demo'], 30, permissions=['read'], key_permissions=['read'],
                            token=token, operation_id='op/fresh', new_principal=True)
    assert again['replayed'] is True and again['key_id'] == issued['key_id']


# spec: ANYM-496
def test_issue_key_permissions_of_repeats_the_stored_ceiling_null_and_wider_included(seeded):
    store, *_ = seeded
    store.set_access('agent-a', permissions=['read'])
    wide = store.issue_key('agent-a', ['telegram-demo'], key_permissions=['read'])
    legacy = store.issue_key('agent-a', ['telegram-demo'], key_permissions=['read'])
    with store.pool.connection() as c:
        # A ceiling wider than the principal's permissions (set-key-permissions allows it) and a NULL legacy one.
        c.execute('UPDATE api_keys SET permissions=%s WHERE digest=%s', (['read', 'send'], digest(wide)))
        c.execute('UPDATE api_keys SET permissions=NULL WHERE digest=%s', (digest(legacy),))
        ids = {name: str(c.execute('SELECT id FROM api_keys WHERE digest=%s', (digest(token),)).fetchone()['id'])
               for name, token in (('wide', wide), ('legacy', legacy))}
    for n, (name, expected) in enumerate((('wide', ['read', 'send']), ('legacy', None))):
        request = dict(permissions=['read'], key_permissions_of=ids[name], token='pos_probe_' + str(n) * 43,
                       operation_id=f'op/ceiling-{n}', new_principal=True)
        issued = store.issue_key(f'agent-clone-{n}', ['telegram-demo'], 30, **request)
        with store.pool.connection() as c:
            stored = c.execute('SELECT permissions FROM api_keys WHERE id=%s', (issued['key_id'],)).fetchone()
        assert stored['permissions'] == expected
        assert store.issue_key(f'agent-clone-{n}', ['telegram-demo'], 30, **request)['replayed'] is True
        with pytest.raises(ValueError, match='operation_conflict'):
            store.issue_key(f'agent-clone-{n}', ['telegram-demo'], 30, **{**request, 'key_permissions_of': None,
                                                                          'key_permissions': ['read']})
    with pytest.raises(ValueError, match='key_permissions_conflict'):
        store.issue_key('agent-x', ['telegram-demo'], key_permissions=['read'], key_permissions_of=ids['wide'])
    with pytest.raises(ValueError, match='unknown_key'):
        store.issue_key('agent-y', ['telegram-demo'], key_permissions_of='00000000-0000-0000-0000-000000000000')
    assert store.principal_info('agent-y')['found'] is False
