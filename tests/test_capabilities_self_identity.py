"""capabilities shows the key's own expiry and each visible account's verified owner ID (ANYM-470)."""
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


# spec: ANYM-470
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


# spec: ANYM-470
def test_account_scoped_key_sees_the_same_fields_and_null_for_an_unverified_account(seeded):
    store, token, _ = seeded
    verify(store, 'telegram-demo', 'telegram', '123456789')
    caps = store.capabilities(token)
    # whatsapp-demo-1 never had a verified collector connect: unknown, never guessed from messages.
    assert accounts(caps) == {'telegram-demo': '123456789', 'whatsapp-demo-1': None}
    assert caps['expires_at'] == key_expiry(store, token)


# spec: ANYM-470
def test_extend_key_shows_on_the_next_capabilities_call(seeded):
    store, token, _ = seeded
    before = store.capabilities(token)['expires_at']
    store.extend_key(store.capabilities(token)['credential_id'], 30)
    after = store.capabilities(token)['expires_at']
    assert after > before and after == key_expiry(store, token)


# spec: ANYM-470
def test_empty_chat_list_shows_no_account_and_no_owner_id(seeded):
    store, _, _ = seeded
    verify(store, 'telegram-demo', 'telegram', '123456789')
    caps = store.capabilities(store.issue_key('nothing', ['telegram-demo'], chats=[], permissions=['read']))
    assert caps['accounts'] == [] and '123456789' not in str(caps)


# spec: ANYM-470
def test_a_key_without_an_expiry_cannot_exist(seeded):
    store, token, _ = seeded
    with pytest.raises(psycopg.errors.NotNullViolation), store.pool.connection() as c:
        c.execute('UPDATE api_keys SET expires_at=NULL WHERE id=%s', (store.capabilities(token)['credential_id'],))


# spec: ANYM-470
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


# spec: ANYM-470
def test_only_a_connected_phase_records_the_id(seeded):
    store, token, _ = seeded
    archive = Archive(store)
    archive.register('telegram-demo', 'telegram', 'Synthetic', discover=True)
    state = CollectorState(archive, 'telegram-demo', ('fixture', archive.acquire('telegram-demo', 'fixture')))
    state.phase('connection', 'connecting', 'collector_starting', self_provider_id='123456789')
    assert accounts(store.capabilities(token))['telegram-demo'] is None
    state.phase('connection', 'connected', 'provider_ready', self_provider_id='123456789')
    assert accounts(store.capabilities(token))['telegram-demo'] == '123456789'


# spec: ANYM-470
def test_collector_records_the_verified_id_on_connect_and_sync_status_does_not_echo_it(worker):  # noqa: F811
    w, store, key = worker
    w.provider.self_provider_id = lambda: '4242424242'
    asyncio.run(w.connect_provider())
    assert accounts(store.capabilities(key)) == {w.account: '4242424242'}
    with store.pool.connection() as c:
        source = get_sync_status(c, {'accounts': [w.account], 'chat_ids': None})['sources'][0]
    assert source['operations']['connection']['state'] == 'connected'
    assert 'identity' not in source['operations'] and '4242424242' not in str(source)


# spec: ANYM-470
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


def health(store, account):
    with store.pool.connection() as c:
        return c.execute('SELECT operations,octet_length(operations::text) AS size FROM collector_health '
            'WHERE account=%s', (account,)).fetchone()


def lease_state(store, account, owner):
    archive = Archive(store)
    archive.register(account, 'telegram', 'Synthetic '+account, discover=True)
    return CollectorState(archive, account, (owner, archive.acquire(account, owner)))


def set_operations(store, account, expression, *args):
    with store.pool.connection() as c:
        c.execute('UPDATE collector_health SET operations='+expression+' WHERE account=%s', (*args, account))


# spec: ANYM-470
def test_whatsmeow_provider_reports_the_verified_id_like_the_webjs_provider(tmp_path):
    from messaging_service.whatsapp_collector import WhatsmeowProvider
    native = WhatsmeowProvider({'expected_wid': '15550000001@c.us', 'native_executable': str(tmp_path/'native')},
        tmp_path/'config.json', -1)
    assert native.self_provider_id() is None
    native.process, native.started = SimpleNamespace(returncode=None), True
    assert native.self_provider_id() == '15550000001@c.us'
    native.process = SimpleNamespace(returncode=1)
    assert native.self_provider_id() is None


# spec: ANYM-470
def test_a_reconnect_rewrites_the_same_id_and_a_failed_identity_check_keeps_the_last_verified_one(seeded):
    store, token, _ = seeded
    state = lease_state(store, 'telegram-demo', 'fixture')
    state.phase('connection', 'connected', 'provider_ready', self_provider_id='123456789')
    first = health(store, 'telegram-demo')['operations']['identity']
    state.phase('connection', 'connected', 'provider_ready', self_provider_id='123456789')
    second = health(store, 'telegram-demo')['operations']['identity']
    assert first['self_provider_id'] == second['self_provider_id'] == '123456789'
    assert second['verified_at'] >= first['verified_at']
    # A mismatch never reaches the connected phase; the ID stays the last verified one.
    state.connection_failed('telegram_identity_mismatch')
    assert accounts(store.capabilities(token))['telegram-demo'] == '123456789'


# spec: ANYM-470
def test_a_collector_that_lost_its_lease_cannot_write_an_id(seeded):
    store, token, _ = seeded
    stale = lease_state(store, 'telegram-demo', 'old-owner')
    stale.phase('connection', 'connected', 'provider_ready', self_provider_id='123456789')
    with store.pool.connection() as c:
        c.execute("UPDATE collector_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE account=%s",
            ('telegram-demo',))
    Archive(store).acquire('telegram-demo', 'new-owner')
    with pytest.raises(Exception):
        stale.phase('connection', 'connected', 'provider_ready', self_provider_id='987654321')
    assert accounts(store.capabilities(token))['telegram-demo'] == '123456789'


def pad_to(store, account, target):
    """Grow operations with a synthetic diagnostic key until its text is `target` bytes."""
    set_operations(store, account, "operations-'padding'")
    size = health(store, account)['size']
    set_operations(store, account, "operations||jsonb_build_object('padding',repeat('x',%s))",
        target-size-len(', "padding": ""'))
    assert health(store, account)['size'] == target


# spec: ANYM-470
def test_near_the_16_kib_limit_the_connect_is_saved_without_the_id(seeded):
    store, token, _ = seeded
    state = lease_state(store, 'telegram-demo', 'fixture')
    state.phase('connection', 'connected', 'provider_ready')
    pad_to(store, 'telegram-demo', 16384-8)
    # The same phase write without an ID still fits; the ID would push it over the limit.
    state.phase('connection', 'connected', 'provider_ready', self_provider_id='123456789')
    row = health(store, 'telegram-demo')
    assert row['operations']['connection']['state'] == 'connected' and 'identity' not in row['operations']
    assert accounts(store.capabilities(token))['telegram-demo'] is None


# spec: ANYM-470
def test_a_stored_id_never_makes_a_later_phase_write_fail(seeded):
    store, token, _ = seeded
    state = lease_state(store, 'telegram-demo', 'fixture')
    state.phase('connection', 'connected', 'provider_ready', self_provider_id='123456789')
    pad_to(store, 'telegram-demo', 16384-2)
    # 'connecting'/'collector_starting' is 5 bytes longer than 'connected'/'provider_ready':
    # over the 2 spare bytes with the stored ID, well within the limit without it.
    state.phase('connection', 'connecting', 'collector_starting')
    row = health(store, 'telegram-demo')
    assert row['operations']['connection']['state'] == 'connecting' and 'identity' not in row['operations']
    assert accounts(store.capabilities(token))['telegram-demo'] is None


# spec: ANYM-470
@pytest.mark.parametrize('stored', ['{"self_provider_id": 123456789}', '{"self_provider_id": {"id": "1"}}',
    '"123456789"', '{"self_provider_id": "15550000001@c.us"}'])
def test_a_malformed_stored_id_reads_as_null(seeded, stored):
    store, token, _ = seeded
    lease_state(store, 'telegram-demo', 'fixture').phase('connection', 'connected', 'provider_ready')
    set_operations(store, 'telegram-demo', "jsonb_set(operations,'{identity}',%s::jsonb)", stored)
    assert accounts(store.capabilities(token))['telegram-demo'] is None


# spec: ANYM-470
def test_two_keys_of_one_principal_each_see_their_own_expiry(seeded):
    store, _, _ = seeded
    short = store.issue_key('agent-c', ['telegram-demo'], days=3)
    long = store.issue_key('agent-c', ['telegram-demo'], days=20)
    assert store.capabilities(short)['expires_at'] == key_expiry(store, short)
    assert store.capabilities(long)['expires_at'] == key_expiry(store, long)
    assert store.capabilities(short)['expires_at'] < store.capabilities(long)['expires_at']
