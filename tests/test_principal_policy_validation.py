"""A principal's policy accepts only known permissions, its own accounts and chats of those accounts (ANYM-12):
an unknown permission, an unknown account or a chat outside the granted accounts is refused and nothing changes."""
import pytest


def policy(store, principal):
    with store.pool.connection() as c:
        row = c.execute('SELECT accounts,chat_ids,permissions,enabled,scope_version FROM principals WHERE id=%s',
                        (principal,)).fetchone()
        audit = c.execute("SELECT count(*) AS n FROM audit_log WHERE operation='set_access'").fetchone()['n']
    return row, audit


# spec: ANYM-12
@pytest.mark.parametrize('change,code', [
    ({'permissions': ['read', 'arbitrary-admin']}, 'invalid_scope'),
    ({'permissions': ['read', 'READ']}, 'invalid_scope'),
    ({'accounts': ['telegram-demo', 'no-such-account']}, 'unknown_account_scope'),
    ({'chats': ['whatsapp-demo-1-chat']}, 'unknown_chat_scope'),
    ({'chats': ['telegram-demo-chat', 'no-such-chat']}, 'unknown_chat_scope'),
    ({'accounts': ['telegram-demo'], 'chats': ['whatsapp-demo-1-chat']}, 'unknown_chat_scope'),
], ids=['unknown-permission', 'wrong-case-permission', 'unknown-account', 'chat-of-another-account', 'unknown-chat',
        'chat-outside-the-new-accounts'])
def test_set_access_refuses_an_unknown_permission_account_or_out_of_account_chat_and_changes_nothing(seeded, change, code):
    store, _, key = seeded
    before = policy(store, 'agent-b')
    capabilities = store.capabilities(key)
    with pytest.raises(ValueError, match='^' + code + '$'):
        store.set_access('agent-b', **change)
    assert policy(store, 'agent-b') == before
    assert store.capabilities(key) == capabilities


# spec: ANYM-12
def test_set_access_accepts_known_permissions_and_a_chat_of_a_granted_account(seeded):
    store, _, key = seeded
    (row, audit) = policy(store, 'agent-b')
    store.set_access('agent-b', chats=['telegram-demo-chat'], permissions=['read', 'download', 'read'])
    (after, audited) = policy(store, 'agent-b')
    assert after['chat_ids'] == ['telegram-demo-chat'] and after['permissions'] == ['download', 'read']
    assert after['scope_version'] == row['scope_version'] + 1 and audited == audit + 1
    assert store.capabilities(key)['permissions'] == ['download', 'read']  # cursor-write dropped at once
