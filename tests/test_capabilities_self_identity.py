"""capabilities shows the key's own expiry and each visible account's verified owner ID (ANYM-SPEC)."""
import asyncio
from types import SimpleNamespace

import psycopg
import pytest

from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState
from messaging_service.sync_status import get_sync_status
from messaging_service.telegram_collector import TelethonProvider
from messaging_service.whatsapp_collector import NodeProvider
from test_collectors import worker  # noqa: F401  (fixture)


def verify(store, account, platform, provider_id):
    """The collector's verified connect, as recorded after get_me/start succeeded."""
    archive = Archive(store)
    archive.register(account, platform, 'Synthetic '+account, discover=True)
    epoch = archive.acquire(account, 'identity-fixture')
    CollectorState(archive, account, ('identity-fixture', epoch)).phase(
        'connection', 'connected', 'provider_ready', self_provider_id=provider_id)


def add_chat(store, account, chat):
    with store.pool.connection() as c:
        c.execute('INSERT INTO chats(id,account,title) VALUES(%s,%s,%s)', (chat, account, 'Synthetic chat'))


def key_expiry(store, token):
    with store.pool.connection() as c:
        return c.execute("SELECT to_char(expires_at AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') AS at "
            'FROM api_keys WHERE id=%s', (store.capabilities(token)['credential_id'],)).fetchone()['at']


def accounts(caps):
    return {row['id']: row['self_provider_id'] for row in caps['accounts']}


# spec: ANYM-SPEC
def test_chat_scoped_key_over_several_chats_in_two_accounts_sees_each_owner_id_and_its_expiry(seeded):
    store, _, _ = seeded
    verify(store, 'telegram-demo', 'telegram', '123456789')
    verify(store, 'whatsapp-demo-1', 'whatsapp', '15550000001@c.us')
    verify(store, 'whatsapp-demo-2', 'whatsapp', '15550000002@c.us')
    add_chat(store, 'telegram-demo', 'telegram-demo-group')
    add_chat(store, 'whatsapp-demo-1', 'whatsapp-demo-1-group')
    token = store.issue_key('between', ['telegram-demo', 'whatsapp-demo-1', 'whatsapp-demo-2'],
        chats=['telegram-demo-chat', 'telegram-demo-group', 'whatsapp-demo-1-chat', 'whatsapp-demo-1-group'],
        permissions=['read'])
    caps = store.capabilities(token)
    # whatsapp-demo-2 is in the account list but no chat of it is: it stays invisible, ID included.
    assert accounts(caps) == {'telegram-demo': '123456789', 'whatsapp-demo-1': '15550000001@c.us'}
    assert all(set(row) == {'id', 'platform', 'label', 'synthetic', 'self_provider_id'} for row in caps['accounts'])
    assert caps['coverage'] == 'chat_scope_only'
    assert sorted(caps['chat_scope']['chat_ids']) == sorted(['telegram-demo-chat', 'telegram-demo-group',
        'whatsapp-demo-1-chat', 'whatsapp-demo-1-group'])
    assert caps['expires_at'] == key_expiry(store, token) and caps['expires_at'].endswith('Z')


# spec: ANYM-SPEC
def test_account_scoped_key_sees_the_same_fields_and_null_for_an_unverified_account(seeded):
    store, token, _ = seeded
    verify(store, 'telegram-demo', 'telegram', '123456789')
    caps = store.capabilities(token)
    # whatsapp-demo-1 never had a verified collector connect: unknown, never guessed from messages.
    assert accounts(caps) == {'telegram-demo': '123456789', 'whatsapp-demo-1': None}
    assert caps['expires_at'] == key_expiry(store, token)


# spec: ANYM-SPEC
def test_extend_key_shows_on_the_next_capabilities_call(seeded):
    store, token, _ = seeded
    before = store.capabilities(token)['expires_at']
    store.extend_key(store.capabilities(token)['credential_id'], 30)
    after = store.capabilities(token)['expires_at']
    assert after > before and after == key_expiry(store, token)


# spec: ANYM-SPEC
def test_empty_chat_list_shows_no_account_and_no_owner_id(seeded):
    store, _, _ = seeded
    verify(store, 'telegram-demo', 'telegram', '123456789')
    caps = store.capabilities(store.issue_key('nothing', ['telegram-demo'], chats=[], permissions=['read']))
    assert caps['accounts'] == [] and '123456789' not in str(caps)


# spec: ANYM-SPEC
def test_a_key_without_an_expiry_cannot_exist(seeded):
    store, token, _ = seeded
    with pytest.raises(psycopg.errors.NotNullViolation), store.pool.connection() as c:
        c.execute('UPDATE api_keys SET expires_at=NULL WHERE id=%s', (store.capabilities(token)['credential_id'],))


# spec: ANYM-SPEC
@pytest.mark.parametrize('platform,value', [('telegram', '15550000001@c.us'), ('telegram', '0123'),
    ('telegram', 123456789), ('whatsapp', '123456789'), ('whatsapp', '15550000001@lid'), (None, None)])
def test_a_malformed_or_missing_id_is_not_written_and_the_connect_still_counts(seeded, platform, value):
    store, token, _ = seeded
    account = 'telegram-demo' if platform != 'whatsapp' else 'whatsapp-demo-1'
    verify(store, account, platform or 'telegram', value)
    with store.pool.connection() as c:
        row = c.execute('SELECT operations FROM collector_health WHERE account=%s', (account,)).fetchone()
    assert row['operations']['connection']['state'] == 'connected' and 'identity' not in row['operations']
    assert accounts(store.capabilities(token))[account] is None


# spec: ANYM-SPEC
def test_only_a_connected_phase_records_the_id(seeded):
    store, token, _ = seeded
    archive = Archive(store)
    archive.register('telegram-demo', 'telegram', 'Synthetic', discover=True)
    state = CollectorState(archive, 'telegram-demo', ('fixture', archive.acquire('telegram-demo', 'fixture')))
    state.phase('connection', 'connecting', 'collector_starting', self_provider_id='123456789')
    assert accounts(store.capabilities(token))['telegram-demo'] is None
    state.phase('connection', 'connected', 'provider_ready', self_provider_id='123456789')
    assert accounts(store.capabilities(token))['telegram-demo'] == '123456789'


# spec: ANYM-SPEC
def test_collector_records_the_verified_id_on_connect_and_sync_status_does_not_echo_it(worker):  # noqa: F811
    w, store, key = worker
    w.provider.self_provider_id = lambda: '4242424242'
    asyncio.run(w.connect_provider())
    assert accounts(store.capabilities(key)) == {w.account: '4242424242'}
    with store.pool.connection() as c:
        source = get_sync_status(c, {'accounts': [w.account], 'chat_ids': None})['sources'][0]
    assert source['operations']['connection']['state'] == 'connected'
    assert 'identity' not in source['operations'] and '4242424242' not in str(source)


# spec: ANYM-SPEC
def test_providers_report_only_the_identity_they_verified(tmp_path):
    telegram = TelethonProvider.__new__(TelethonProvider)
    telegram.verified_self = None
    assert telegram.self_provider_id() is None
    telegram.verified_self = SimpleNamespace(id=123456789)
    assert telegram.self_provider_id() == '123456789'
    whatsapp = NodeProvider({'expected_wid': '15550000001@c.us'}, tmp_path/'config.json', -1)
    assert whatsapp.self_provider_id() is None
    whatsapp.process = SimpleNamespace(returncode=None)
    whatsapp.started = True
    assert whatsapp.self_provider_id() == '15550000001@c.us'
