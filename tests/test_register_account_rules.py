"""Archive.register: the account ID's 1-64 character rule and the synthetic flag as part of identity (ANYM-80).

Existing tests pin the CLI output, the empty policy and a platform change; these pin the ID boundary itself and the
synthetic flag, through the real Archive.register on the disposable database. No provider is reached.
"""
import pytest

from messaging_service.archive import Archive


def footprint(store, account):
    """Every row registration writes for one account: identity, policy and source status."""
    with store.pool.connection() as c:
        return {
            'account': c.execute('SELECT platform,label,synthetic FROM accounts WHERE id=%s', (account,)).fetchone(),
            'policy': c.execute('''SELECT enabled,include_chats,exclude_chats,discover_chats FROM account_policies
                WHERE account=%s''', (account,)).fetchone(),
            'status': c.execute('SELECT status FROM source_status WHERE account=%s', (account,)).fetchone()}


def account_count(store):
    with store.pool.connection() as c:
        return c.execute('''SELECT (SELECT count(*) FROM accounts)+(SELECT count(*) FROM account_policies)
            +(SELECT count(*) FROM source_status) AS n''').fetchone()['n']


# spec: ANYM-80
@pytest.mark.parametrize('account', ['x', 'a' * 64, 'Az09_.-mixed', '.-_'], ids=['one', 'sixty-four', 'mixed', 'marks'])
def test_register_accepts_one_to_64_letters_digits_dots_dashes_and_underscores(seeded, account):
    store, _, _ = seeded
    Archive(store).register(account, 'telegram', 'Synthetic label', include_chats=['synthetic-chat'])
    assert footprint(store, account) == {
        'account': {'platform': 'telegram', 'label': 'Synthetic label', 'synthetic': True},
        'policy': {'enabled': True, 'include_chats': ['synthetic-chat'], 'exclude_chats': [], 'discover_chats': False},
        'status': {'status': 'not_started'}}


# spec: ANYM-80
@pytest.mark.parametrize('account', ['', 'a' * 65, 'two words', 'tab\there', 'line\nbreak', 'trailing\n', 'slash/name',
                                     'semi;colon', 'ünicode', 'emoji-🙂', "quote'd"],
                         ids=['empty', 'sixty-five', 'space', 'tab', 'newline', 'trailing-newline', 'slash', 'semicolon',
                              'non-ascii', 'emoji', 'quote'])
def test_register_refuses_any_other_account_id_and_writes_nothing(seeded, account):
    store, _, _ = seeded
    before = account_count(store)
    with pytest.raises(ValueError, match='^invalid_account$'):
        Archive(store).register(account, 'telegram', 'Synthetic label', include_chats=['synthetic-chat'])
    assert account_count(store) == before


# spec: ANYM-80
@pytest.mark.parametrize('first', [True, False], ids=['synthetic-to-real', 'real-to-synthetic'])
def test_re_registering_with_the_other_synthetic_flag_is_an_identity_change_that_changes_nothing(seeded, first):
    store, _, _ = seeded
    archive = Archive(store)
    account = 'synthetic-flag-' + ('on' if first else 'off')
    archive.register(account, 'whatsapp', 'Original label', synthetic=first, include_chats=['kept-chat'])
    registered = footprint(store, account)
    with pytest.raises(ValueError, match='^account_identity_change$'):
        archive.register(account, 'whatsapp', 'Changed label', synthetic=not first, include_chats=['other-chat'],
                         exclude_chats=['kept-chat'], discover=True)
    assert footprint(store, account) == registered
    # Control: the same identity re-registers, and only the policy changes.
    archive.register(account, 'whatsapp', 'Changed label', synthetic=first, include_chats=['other-chat'], discover=True)
    assert footprint(store, account) == {**registered, 'policy': {
        'enabled': True, 'include_chats': ['other-chat'], 'exclude_chats': [], 'discover_chats': True}}
