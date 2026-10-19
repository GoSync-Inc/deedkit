"""Missing Telegram contract assertions using synthetic clients and originals only."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from telethon import events, functions, types

from messaging_service.collector_state import CaptureSpool, session_lock
from messaging_service.telegram_collector import TelegramCollector, TelethonProvider, configuration, private_json
from messaging_service.telegram_operations import execute
from test_collectors import message, worker
from test_telegram_operations import Client, job, run


@pytest.fixture
def private_config(tmp_path):
    def write(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value)); path.chmod(0o600)
        return path
    credentials = write('credentials.json', {'api_id': 123, 'api_hash': 'a' * 32, 'expected_user_id': 42})
    permit = write('permit.json', {'account': 'synthetic-tg', 'expected_user_id': 42,
                                  'prior_clients_stopped': True})
    config = {'enabled': True, 'account': 'synthetic-tg', 'credentials_file': str(credentials),
              'permit_file': str(permit), 'session_file': str(tmp_path / 'fixture.session'),
              'spool_directory': str(tmp_path / 'spool')}
    return write, config


def test_private_configuration_accepts_bounded_owner_files_and_utc_window(private_config):
    write, config = private_config
    config.update(batch_size=100, scan_interval_seconds=10, max_dialogs=10000,
                  history_since='2026-07-15T08:00:00+08:00')
    cfg, auth = configuration(write('config.json', config))
    assert cfg['batch_size'] == 100 and cfg['max_dialogs'] == 10000
    assert cfg['history_since'] == datetime(2026, 7, 15, tzinfo=timezone.utc)
    assert auth['expected_user_id'] == 42


@pytest.mark.parametrize('key,value,error', [
    ('unknown', True, 'invalid_collector_config'), ('enabled', False, 'collector_disabled'),
    ('session_file', 'relative.session', 'invalid_private_path'),
    ('session_file', '/tmp/not-a-session.sqlite', 'invalid_session_filename'),
    ('credentials_file', 'relative.json', 'invalid_private_path'),
    ('permit_file', 'relative.json', 'invalid_private_path'),
    ('spool_directory', 'relative', 'invalid_private_path'),
    ('batch_size', 0, 'invalid_batch_size'), ('batch_size', 101, 'invalid_batch_size'),
    ('batch_size', True, 'invalid_batch_size'),
    ('scan_interval_seconds', 9, 'invalid_scan_interval_seconds'),
    ('scan_interval_seconds', 3601, 'invalid_scan_interval_seconds'),
    ('max_media_bytes', 0, 'invalid_max_media_bytes'),
    ('max_media_bytes', 4 * 1024**3 + 1, 'invalid_max_media_bytes'),
    ('disk_reserve_bytes', 1024**3 - 1, 'invalid_disk_reserve_bytes'),
    ('disk_reserve_bytes', 100 * 1024**3 + 1, 'invalid_disk_reserve_bytes'),
    ('media_disk_reserve_bytes', 0, 'invalid_media_disk_reserve_bytes'),
    ('max_dialogs', 0, 'invalid_max_dialogs'), ('max_dialogs', 10001, 'invalid_max_dialogs'),
    ('max_dialogs', True, 'invalid_max_dialogs'),
])
def test_configuration_rejects_invalid_paths_keys_and_resource_bounds(private_config, key, value, error):
    write, config = private_config
    config[key] = value
    with pytest.raises((ValueError, RuntimeError), match=error):
        configuration(write('config.json', config))


@pytest.mark.parametrize('field,value', [
    ('account', 'another-tg'), ('expected_user_id', 99), ('prior_clients_stopped', False),
    ('prior_clients_stopped', 1),
])
def test_configuration_requires_exact_identity_and_explicit_handover(private_config, field, value):
    write, config = private_config
    permit = {'account': 'synthetic-tg', 'expected_user_id': 42, 'prior_clients_stopped': True}
    permit[field] = value
    write('permit.json', permit)
    with pytest.raises(RuntimeError, match='session_handover_not_confirmed'):
        configuration(write('config.json', config))


@pytest.mark.parametrize('kind', ['group-readable', 'symlink', 'oversize', 'directory'])
def test_private_json_refuses_unsafe_file_handles(tmp_path, kind):
    path = tmp_path / 'config.json'
    path.write_text('{}'); path.chmod(0o600)
    if kind == 'group-readable': path.chmod(0o640)
    elif kind == 'oversize': path.write_bytes(b' ' * 65537)
    elif kind == 'directory': path.unlink(); path.mkdir()
    elif kind == 'symlink':
        target = tmp_path / 'target'; path.rename(target); path.symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        private_json(path)


def test_pairing_requires_both_terminal_streams_before_configuration(monkeypatch):
    from messaging_service import telegram_pair as pairing
    parsed = []
    monkeypatch.setattr(pairing, 'configuration', lambda path: parsed.append(path))
    for stdin_tty, stderr_tty in [(False, True), (True, False), (False, False)]:
        monkeypatch.setattr(pairing.sys, 'stdin', NS(isatty=lambda: stdin_tty))
        monkeypatch.setattr(pairing.sys, 'stderr', NS(isatty=lambda: stderr_tty))
        with pytest.raises(RuntimeError, match='owner_terminal_required'):
            asyncio.run(pairing.pair_config('not-read.json'))
    assert parsed == []


@pytest.mark.parametrize('scenario', ['paired', 'wrong-identity', 'synthetic-account', 'missing-account',
                                    'wrong-platform', 'lease-lost', 'heartbeat-lost'])
def test_terminal_pairing_rechecks_secret_lease_identity_and_closes_every_resource(
        tmp_path, monkeypatch, capsys, scenario):
    from messaging_service import telegram_pair as pairing
    calls = []; session = tmp_path / 'owner.session'; session.write_text('synthetic session')
    session.chmod(0o644)
    cfg = {'account': 'synthetic-tg', 'session_file': str(session)}
    auth = {'expected_user_id': 42}
    monkeypatch.setattr(pairing, 'require_owner_terminal', lambda: None)
    monkeypatch.setattr(pairing, 'configuration', lambda _: (cfg, auth))
    dsn = tmp_path / 'test.dsn'; dsn.write_text('synthetic-not-a-connection')
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn)); monkeypatch.setenv('PROBE_FILES', str(tmp_path))
    if scenario == 'heartbeat-lost':
        original_sleep = asyncio.sleep
        async def heartbeat_tick(seconds):
            assert seconds == 15
            calls.append('heartbeat-15s')
            await original_sleep(0)
        monkeypatch.setattr(pairing.asyncio, 'sleep', heartbeat_tick)

    class Pool:
        @contextmanager
        def connection(self):
            row = None if scenario == 'missing-account' else {
                'platform': 'whatsapp' if scenario == 'wrong-platform' else 'telegram',
                'synthetic': scenario == 'synthetic-account'}
            yield NS(execute=lambda *args: NS(fetchone=lambda: row))

    store = NS(pool=Pool(), close=lambda: calls.append('store-close'))
    monkeypatch.setattr(pairing, 'Store', lambda *args: store)

    class Archive:
        def acquire(self, account, owner): calls.append('acquire'); return 7
        def renew(self, account, owner, epoch):
            assert account == 'synthetic-tg' and epoch == 7
            calls.append('renew')
            if scenario in ('lease-lost', 'heartbeat-lost'): raise RuntimeError('collector_lease_lost')
        def release(self, account, owner, epoch): calls.append('release')

    monkeypatch.setattr(pairing, 'Archive', lambda _: Archive())
    monkeypatch.setattr(pairing.getpass, 'getpass', lambda prompt: calls.append('hidden-prompt') or 'hidden-value')

    class Provider:
        def __init__(self, config, credentials, *, receive_updates):
            assert receive_updates is False
            with pytest.raises(RuntimeError, match='session_already_owned'):
                with session_lock(session): pytest.fail('pairing did not own the local session')
            calls.append('provider'); self.client = self
        async def start(self, *, phone, code_callback, password, max_attempts):
            assert max_attempts == 3
            if scenario == 'heartbeat-lost': await asyncio.Event().wait()
            for callback in [phone, code_callback, password]:
                secret = await callback()
                assert calls[-1] == 'renew' and secret == 'hidden-value'
                calls.append('provider-secret')
        async def get_me(self): return NS(id=99 if scenario == 'wrong-identity' else 42)
        async def disconnect(self): calls.append('disconnect')

    monkeypatch.setattr(pairing, 'TelethonProvider', Provider)
    expected = {'wrong-identity': 'telegram_identity_mismatch', 'synthetic-account': 'live_account_required',
                'missing-account': 'live_account_required', 'wrong-platform': 'live_account_required',
                'lease-lost': 'collector_lease_lost', 'heartbeat-lost': 'collector_lease_lost'}.get(scenario)
    if expected:
        with pytest.raises(RuntimeError, match=expected): asyncio.run(pairing.pair_config('ignored'))
        assert 'paired' not in capsys.readouterr().out
    else:
        asyncio.run(pairing.pair_config('ignored'))
        assert json.loads(capsys.readouterr().out) == {'status': 'paired', 'collector_started': False}
        assert session.stat().st_mode & 0o777 == 0o600
        assert calls.count('provider-secret') == 3 and calls.count('renew') == 4
    assert calls[-1] == 'store-close'
    if scenario in ('synthetic-account', 'missing-account', 'wrong-platform'):
        assert 'acquire' not in calls and 'provider' not in calls
    else: assert calls[-3:] == ['disconnect', 'release', 'store-close']
    if scenario in ('lease-lost', 'heartbeat-lost'): assert 'provider-secret' not in calls
    if scenario == 'heartbeat-lost': assert 'heartbeat-15s' in calls
    with session_lock(session): pass  # Teardown released local session ownership too.


@pytest.mark.parametrize('failure,expected', [('telegram_identity_mismatch', 'telegram_identity_mismatch'),
                                          ('provider sent PRIVATE_SENTINEL', 'pairing_failed')])
def test_pairing_cli_failure_output_has_only_safe_error_code(monkeypatch, capsys, failure, expected):
    from messaging_service import telegram_pair as pairing
    async def failed(_): raise RuntimeError(failure)
    monkeypatch.setattr(pairing, 'pair_config', failed)
    monkeypatch.setattr(pairing.sys, 'argv', ['telegram-pair', '--config', '/unused/synthetic.json'])
    old_mask = os.umask(0o077)
    try:
        with pytest.raises(SystemExit) as error: pairing.main()
    finally: os.umask(old_mask)
    assert error.value.code == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {'status': 'not_paired', 'code': expected}
    assert 'PRIVATE_SENTINEL' not in output.out + output.err


def test_telegram_login_secrets_have_no_gateway_tool_or_rest_pairing_route():
    from messaging_service.app import create_app, validate_arguments
    from messaging_service.store import APIError
    from starlette.testclient import TestClient
    app = create_app(NS(public_url='https://example.invalid', authenticate=lambda token: 'synthetic-principal',
                        audit_boundary=lambda code: None), ['testserver'])
    fields = {'api_hash', 'api_id', 'expected_user_id', 'login_code', 'password', 'session_file', 'credentials_file'}
    assert not any('pair' in name or 'login' in name for name in app.models)
    assert all(not fields.intersection(model.model_fields) for model in app.models.values())
    with pytest.raises(APIError, match='invalid_parameters'):
        validate_arguments(app.models, 'search', {'query': '', 'api_hash': 'PRIVATE_SENTINEL'})
    with TestClient(app) as client:
        response = client.post('/v1/telegram_pair', headers={'Authorization': 'Bearer synthetic'},
                               json={'api_hash': 'PRIVATE_SENTINEL'})
    assert response.status_code == 404 and 'PRIVATE_SENTINEL' not in response.text


def test_telegram_connect_missing_session_verification_never_catches_up():
    calls = []
    async def connect(): calls.append('connect')
    async def me(): calls.append('get_me'); return None
    async def catch_up(): pytest.fail('unverified account must not collect')
    provider = TelethonProvider.__new__(TelethonProvider)
    provider.client = NS(connect=connect, get_me=me, catch_up=catch_up)
    provider.expected_user_id = 42; provider.verified_self = types.User(42, is_self=True)
    with pytest.raises(RuntimeError, match='telegram_pairing_required'): asyncio.run(provider.connect())
    assert provider.verified_self is None and calls == ['connect', 'get_me']


def test_native_telegram_client_disables_request_retries_and_hidden_flood_sleeps(monkeypatch):
    clients = []
    def native(*args, **kwargs): clients.append((args, kwargs)); return object()
    monkeypatch.setattr('telethon.TelegramClient', native)
    provider = TelethonProvider({'session_file': '/unused/synthetic.session'},
                               {'api_id': 123, 'api_hash': 'a' * 32, 'expected_user_id': 42})
    assert provider.verified_self is None and provider.expected_user_id == 42
    assert len(clients) == 1
    options = clients[0][1]
    assert options['request_retries'] == 0 and options['flood_sleep_threshold'] == 0
    assert options['raise_last_call_error'] is True and options['catch_up'] is False


def test_telegram_provider_subscribes_all_live_message_kinds_without_connecting():
    captured = []; provider = TelethonProvider.__new__(TelethonProvider)
    provider.client = NS(add_event_handler=lambda callback, kind: captured.append((callback, kind)))
    message_callback, deletion_callback = object(), object()
    provider.subscribe(message_callback, deletion_callback)
    assert [type(kind) for _, kind in captured] == [events.NewMessage, events.MessageEdited, events.MessageDeleted]
    assert [callback for callback, _ in captured] == [message_callback, message_callback, deletion_callback]


def test_live_capture_is_durable_before_database_and_overload_fails_closed(tmp_path):
    spool = CaptureSpool(tmp_path / 'captures', reserve_bytes=0)
    collector = TelegramCollector(None, 'synthetic-tg', NS(), spool, writer=object())
    collector.state = NS(allows=lambda *args: True)
    collector.policy = {'account': 'synthetic-tg'}
    asyncio.run(collector.capture(NS(message=message(12))))
    writes = list(spool.path.glob('*.json'))
    assert len(writes) == 1
    envelope = json.loads(writes[0].read_text())
    # DurableSpool stores the canonical payload under its checksum envelope.
    payload = envelope['payload'] if 'payload' in envelope else envelope
    if isinstance(payload, str): payload = json.loads(payload)
    assert payload['kind'] == 'message' and payload['peer_kind'] == 'user'
    assert datetime.fromisoformat(payload['captured_at']).tzinfo is not None
    assert payload['envelope']['provider_id'] == '12'
    collector.capture_count = 16; collector.capture_wait_seconds = 0  # ANYM-291: no slot frees
    asyncio.run(collector.capture(NS(message=message(13))))
    assert collector.failed.is_set() and collector.failure_code == 'capture_overload'
    assert list(spool.path.glob('*.json')) == writes


def test_live_drain_retries_database_failure_and_then_applies_tombstones(monkeypatch):
    import psycopg
    collector = TelegramCollector.__new__(TelegramCollector)
    collector.spool = object(); collector._history_spool = object(); seen = []; sleeps = []
    async def drain(spools, history_spools):
        assert spools == [collector.spool] and history_spools == [collector._history_spool]
        seen.append('drain')
        if seen.count('drain') == 1: raise psycopg.OperationalError('synthetic database failure')
    collector.drain_captures = drain
    collector.state = NS(apply_tombstones=lambda: seen.append('tombstones'))
    async def sleep(seconds):
        sleeps.append(seconds)
        if seen.count('drain') == 2: raise asyncio.CancelledError
    monkeypatch.setattr('messaging_service.telegram_collector.asyncio.sleep', sleep)
    with pytest.raises(asyncio.CancelledError): asyncio.run(collector.drain_loop())
    assert seen == ['drain', 'drain', 'tombstones'] and sleeps == [1, .25, .25]


def test_dialog_discovery_uses_cached_catalog_until_fifteen_minute_boundary(monkeypatch):
    clock = [100.0]; catalog_calls = []; history = []; statuses = []
    monkeypatch.setattr('messaging_service.telegram_collector.time.monotonic', lambda: clock[0])
    async def dialogs(limit):
        catalog_calls.append(limit); return [('100', 'user', 'One'), ('200', 'chat', 'Two')]
    collector = TelegramCollector(None, 'synthetic-tg', NS(dialogs=dialogs), NS(), writer=object(), max_dialogs=2)
    collector.state = NS(policy=lambda: {'include_chats': [], 'exclude_chats': []},
        allows=lambda *args: True, inventory=lambda rows, *args, **kwargs: [r[0] for r in rows],
        discovery_started=lambda: None, discovery_finished=lambda limited: statuses.append(limited),
        attempted=lambda chat: None, status=lambda *args: statuses.append(args))
    async def sync(chat, kind, title): history.append(chat)
    collector.sync_chat = sync
    asyncio.run(collector.sync_once()); clock[0] = 999.9; asyncio.run(collector.sync_once())
    assert catalog_calls == [2] and history == ['100', '200', '100', '200']
    clock[0] = 1000; asyncio.run(collector.sync_once())
    assert catalog_calls == [2, 2] and statuses.count(True) == 2
    assert statuses[-1] == ('receiving', 'dialog_discovery_limited')


def test_unavailable_dialog_does_not_starve_other_chats_and_missing_include_is_visible():
    from telethon.errors import ChannelPrivateError
    attempts = []; errors = []; statuses = []
    async def dialogs(limit): return [('100', 'user', 'One'), ('200', 'channel', 'Two')]
    collector = TelegramCollector(None, 'synthetic-tg', NS(dialogs=dialogs), NS(), writer=object())
    collector.state = NS(policy=lambda: {'include_chats': ['300'], 'exclude_chats': []},
        allows=lambda *args: True, inventory=lambda *args, **kwargs: ['100', '200'],
        discovery_started=lambda: None, discovery_finished=lambda limited: None,
        attempted=lambda chat: attempts.append(chat), failed=lambda *args: errors.append(args),
        status=lambda *args: statuses.append(args))
    async def sync(chat, kind, title):
        if chat == '100': raise ChannelPrivateError(None)
    collector.sync_chat = sync
    asyncio.run(collector.sync_once())
    assert attempts == ['100', '200'] and errors == [('100', 'provider_unavailable')]
    assert statuses[-1] == ('degraded', 'configured_chat_unavailable')


def test_catalog_title_updates_are_nonempty_and_inventory_batches_remain_fenced(worker):
    from messaging_service.envelope import stable_id
    collector, store, _ = worker
    collector.state.status('receiving', 'telegram_discovery')
    collector.state.discovery_started()
    rows = [(str(100 + n), ['user', 'chat', 'channel'][n % 3], f'Title {n}',
             ['private', 'group', 'channel'][n % 3]) for n in range(205)]
    fences = []; original_fence = collector.archive.fence
    def fenced(*args): fences.append(True); return original_fence(*args)
    collector.archive.fence = fenced
    order = collector.state.inventory(rows, None, discovery=True)
    assert len(fences) == 4  # Three bounded write transactions + fenced order query.
    assert set(order) == {row[0] for row in rows}
    collector.state.inventory([('100', 'user', '', 'private'), ('101', 'chat', '101', 'group')], None)
    with store.pool.connection() as connection:
        assert connection.execute('SELECT peer_kind FROM collector_chats WHERE account=%s AND provider_chat=%s',
            (collector.account, '100')).fetchone()['peer_kind'] == 'user'
        assert connection.execute('SELECT chat_type FROM chats WHERE id=%s',
            (stable_id('chat', collector.account, '101'),)).fetchone()['chat_type'] == 'group'
        assert connection.execute('SELECT chat_type FROM chats WHERE id=%s',
            (stable_id('chat', collector.account, '102'),)).fetchone()['chat_type'] == 'channel'
        assert connection.execute('SELECT title FROM chats WHERE id=%s',
            (stable_id('chat', collector.account, '100'),)).fetchone()['title'] == 'Title 0'
        assert connection.execute('SELECT title FROM chats WHERE id=%s',
            (stable_id('chat', collector.account, '101'),)).fetchone()['title'] == 'Title 1'
        assert connection.execute('SELECT discovery_count FROM collector_health WHERE account=%s',
            (collector.account,)).fetchone()['discovery_count'] == 205
    collector.state.inventory([('100', 'user', 'Renamed', 'private')], None)
    collector.state.attempted('100')
    assert collector.state.inventory([], None)[-1] == '100'
    with store.pool.connection() as connection:
        assert connection.execute('SELECT title FROM chats WHERE id=%s',
            (stable_id('chat', collector.account, '100'),)).fetchone()['title'] == 'Renamed'


def test_live_read_pages_expose_bounds_and_hide_restricted_account_totals():
    client = Client()
    client.responses[functions.contacts.GetBlockedRequest] = NS(count=999,
        blocked=[types.PeerBlocked(types.PeerUser(i), datetime.now(timezone.utc)) for i in range(200, 300)])
    policy = {'enabled': True, 'discover_chats': False, 'include_chats': ['200'], 'exclude_chats': []}
    data = run(client, 'list_blocked', {'limit': 100}, _policy=policy)['data']
    assert data['reported_count'] is None and data['page_limit'] == 50
    assert [row['peer_id'] for row in data['blocked']] == ['200']
    assert data['coverage'] == 'filtered_provider_page'
    assert next(call for call in client.calls if isinstance(call, functions.contacts.GetBlockedRequest)).limit == 50
    client.responses[functions.messages.GetFullChatRequest] = NS(full_chat=NS(participants=None))
    data = run(client, 'list_participants', {'limit': 100}, provider_chat='-100')['data']
    assert data == {'participants': [], 'coverage': 'unavailable', 'next_offset': None}


def test_channel_participant_request_and_result_are_capped_without_complete_membership_claim():
    client = Client()
    client.responses[functions.channels.GetParticipantsRequest] = NS(
        count=999, participants=[NS(user_id=i) for i in range(200, 300)],
        users=[types.User(i, first_name='Member') for i in range(200, 300)] +
              [types.User(999, first_name='Unrelated')])
    data = run(client, 'list_participants', {'limit': 100, 'offset': 17},
               provider_chat='-1000000000100')['data']
    requests = [call for call in client.calls if isinstance(call, functions.channels.GetParticipantsRequest)]
    assert len(requests) == 1
    request = requests[0]
    assert request.channel.channel_id == 100 and request.offset == 17
    assert request.limit == 50 and request.hash == 0
    assert isinstance(request.filter, types.ChannelParticipantsSearch) and request.filter.q == ''
    assert len(data['participants']) == 50
    assert [row['id'] for row in data['participants']] == [str(i) for i in range(200, 250)]
    assert data['page_limit'] == 50 and data['next_offset'] == 67
    assert data['reported_count'] == 999
    assert data['coverage'] == 'provider_page_not_complete_membership'
    assert 'Unrelated' not in str(data)


def test_contact_result_page_honors_exact_fifty_profile_bound():
    client = Client()
    client.responses[functions.contacts.GetContactsRequest] = NS(
        contacts=[NS(user_id=i) for i in range(200, 300)],
        users=[types.User(i, first_name='Contact') for i in range(200, 300)] +
              [types.User(999, first_name='Unrelated')])
    data = run(client, 'list_contacts', {'limit': 100})['data']
    requests = [call for call in client.calls if isinstance(call, functions.contacts.GetContactsRequest)]
    assert len(requests) == 1 and requests[0].hash == 0
    assert len(data['contacts']) == 50 and data['page_limit'] == 50
    assert [row['id'] for row in data['contacts']] == [str(i) for i in range(200, 250)]
    assert data['reported_count'] == 100 and data['next_offset'] == 50
    assert 'Unrelated' not in str(data)


@pytest.mark.parametrize('account', [None, '', 'a' * 65, 'two accounts', 42])
def test_telegram_receipt_rejects_invalid_account_names(account):
    from messaging_service.telegram_receipts import telegram_receipt
    update = types.UpdateReadHistoryOutbox(types.PeerUser(100), 23, 10, 1)
    assert telegram_receipt(account, update, verified_self=types.User(42, is_self=True),
        expected_user_id=42, observed_at=datetime(2026, 10, 14, 12, tzinfo=timezone.utc)) is None


@pytest.mark.parametrize('observed', [datetime(2026, 10, 14, 12), '2026-10-14T12:00:00+00:00', 42, True])
def test_telegram_receipt_ignores_naive_or_non_datetime_observation(observed):
    from messaging_service.telegram_receipts import telegram_receipt
    update = types.UpdateReadHistoryOutbox(types.PeerUser(100), 23, 10, 1)
    assert telegram_receipt('tg-fixture', update, verified_self=types.User(42, is_self=True),
        expected_user_id=42, observed_at=observed) is None


def test_telegram_receipt_normalizes_zoned_observation_to_utc_without_provider_timestamp():
    from messaging_service.telegram_receipts import telegram_receipt
    observed = datetime(2026, 10, 14, 20, 15, 30, tzinfo=timezone(timedelta(hours=8)))
    update = types.UpdateReadHistoryOutbox(types.PeerUser(100), 23, 10, 1)
    assert telegram_receipt('tg-fixture', update, verified_self=types.User(42, is_self=True),
        expected_user_id=42, observed_at=observed) == {
        'kind': 'message_receipt', 'account': 'tg-fixture', 'platform': 'telegram',
        'chat': '100', 'provider_id': None, 'max_id': 23, 'state': 'read',
        'basis': 'telegram_outbox_boundary', 'audience': 'private_peer',
        'observed_at': '2026-10-14T12:15:30+00:00', 'provider_at': None,
    }


def test_owner_profile_requires_verified_identity_and_matching_provider_user():
    client = Client()
    with pytest.raises(Exception, match='provider_identity_unverified'): run(client, 'get_profile')
    assert client.calls == []
    with pytest.raises(Exception, match='provider_reference_mismatch'):
        asyncio.run(execute(client, job('get_profile'), verified_self=types.User(42, is_self=True)))


def test_live_chat_and_message_observations_select_metadata_without_native_objects():
    client = Client()
    client.responses[functions.messages.GetFullChatRequest] = NS(
        full_chat=NS(about='Synthetic group', participants_count=7, secret='PRIVATE_SENTINEL'),
        chats=[types.Chat(100, 'Group', types.ChatPhotoEmpty(), 7, datetime.now(timezone.utc), 1)])
    result = run(client, 'chat_info', provider_chat='-100')
    data = result['data']
    assert data['provider_chat_id'] == '-100' and data['title'] == 'Group'
    assert data['description'] == 'Synthetic group' and data['participant_count'] == 7
    assert data['basis'] == 'live_provider_observation' and 'PRIVATE_SENTINEL' not in str(result)
    client.message.reply_to = NS(reply_to_msg_id=5, reply_to_top_id=3)
    client.message.grouped_id = 999
    client.message.native_secret = 'PRIVATE_SENTINEL'
    data = run(client, 'get_message_info')['data']
    assert data['provider_message_id'] == '23' and data['provider_chat_id'] == '100'
    assert data['reply_to_provider_id'] == '5' and data['topic_provider_id'] == '3' and data['album_id'] == '999'
    assert data['outgoing'] is True and data['provider_at'] and 'PRIVATE_SENTINEL' not in str(data)


def test_reactions_and_first_topic_page_are_bounded_without_completeness_claim():
    from messaging_service.telegram_operations import reaction_state
    now = datetime.now(timezone.utc)
    observations = types.MessageReactions(
        [types.ReactionCount(types.ReactionEmoji('👍'), n) for n in range(100)], min=True,
        recent_reactions=[types.MessagePeerReaction(types.PeerUser(n + 200), now,
                                                   types.ReactionEmoji('👍')) for n in range(100)])
    data = reaction_state(observations)
    assert len(data['counts']) == len(data['recent']) == 50
    assert data['state'] == 'partial' and data['actors_complete'] is False and data['truncated'] is True
    client = Client()
    client.responses[functions.messages.GetForumTopicsRequest] = NS(topics=[], count=999)
    data = run(client, 'list_topics', {'limit': 100}, provider_chat='-1000000000100')['data']
    assert data['page_limit'] == 50 and data['pagination_supported'] is False
    assert data['next_offset'] is None and data['coverage'] == 'first_provider_page'
    assert next(call for call in client.calls if isinstance(call, functions.messages.GetForumTopicsRequest)).limit == 50
    with pytest.raises(Exception, match='unsupported_operation'): run(client, 'list_topics')


def test_inclusion_only_contact_reads_cannot_disclose_unselected_users():
    client = Client()
    client.responses[functions.contacts.GetContactsRequest] = NS(
        contacts=[NS(user_id=n) for n in (200, 300)],
        users=[types.User(200, first_name='Selected'), types.User(300, first_name='Unselected')])
    policy = {'enabled': True, 'discover_chats': False, 'include_chats': ['200'], 'exclude_chats': []}
    data = run(client, 'list_contacts', _policy=policy)['data']
    assert [user['id'] for user in data['contacts']] == ['200'] and data['reported_count'] == 1
    client.calls.clear()
    assert run(client, 'get_contact', {'user_id': '300'}, _policy=policy)['data']['found'] is False
    assert client.calls == []


@pytest.mark.parametrize('mode,mime,media_class,attribute', [
    ('photo', 'image/png', types.InputMediaUploadedPhoto, None),
    ('video', 'video/mp4', types.InputMediaUploadedDocument, types.DocumentAttributeVideo),
    ('audio', 'audio/ogg', types.InputMediaUploadedDocument, types.DocumentAttributeAudio),
    ('voice', 'audio/ogg', types.InputMediaUploadedDocument, types.DocumentAttributeAudio),
    ('video_note', 'video/mp4', types.InputMediaUploadedDocument, types.DocumentAttributeVideo),
])
def test_registered_original_modes_use_bounded_native_shapes(tmp_path, mode, mime, media_class, attribute):
    path = tmp_path / 'original'; path.write_bytes(b'synthetic original')
    client = Client()
    data = run(client, 'send_file', {'mode': mode}, registered_file_path=str(path),
               file_digest=hashlib.sha256(path.read_bytes()).hexdigest(), file_mime=mime)
    request = next(call for call in client.calls if isinstance(call, functions.messages.SendMediaRequest))
    assert isinstance(request.media, media_class) and data['outcome'] == 'confirmed'
    if attribute:
        item = next(value for value in request.media.attributes if isinstance(value, attribute))
        if attribute is types.DocumentAttributeAudio: assert item.voice is (mode == 'voice')
        else: assert item.round_message is (mode == 'video_note')


@pytest.mark.parametrize('mode,mime,filename', [
    ('photo', 'application/pdf', 'a.pdf'), ('voice', 'video/mp4', 'a.mp4'),
    ('video', 'audio/ogg', 'a.ogg'), ('document', 'bad mime', 'a'),
    ('document', 'application/pdf', '../file'), ('document', 'application/pdf', 'bad\x00name'),
])
def test_registered_original_invalid_type_or_display_metadata_never_uploads(tmp_path, mode, mime, filename):
    path = tmp_path / 'original'; path.write_bytes(b'synthetic original')
    client = Client()
    with pytest.raises(Exception, match='unsupported_operation|invalid_parameters|attachment_unavailable'):
        run(client, 'send_file', {'mode': mode}, registered_file_path=str(path),
            file_digest=hashlib.sha256(path.read_bytes()).hexdigest(), file_mime=mime, file_name=filename)
    assert not any(isinstance(call, (functions.upload.SaveFilePartRequest,
                                   functions.messages.SendMediaRequest)) for call in client.calls)


def test_registered_photo_over_ten_mib_and_empty_original_are_not_uploaded(tmp_path):
    path = tmp_path / 'original'; path.touch()
    client = Client()
    with pytest.raises(Exception, match='attachment_too_large'):
        run(client, 'send_file', registered_file_path=str(path), file_digest=hashlib.sha256(b'').hexdigest())
    with path.open('wb') as stream: stream.truncate(10 * 1024**2 + 1)
    with pytest.raises(Exception, match='unsupported_operation'):
        run(client, 'send_file', {'mode': 'photo'}, registered_file_path=str(path), file_mime='image/png')
    assert not any(isinstance(call, (functions.upload.SaveFilePartRequest,
                                   functions.messages.SendMediaRequest)) for call in client.calls)


@pytest.mark.parametrize('kind', ['relative', 'directory', 'missing-sha', 'malformed-sha'])
def test_registered_original_path_and_sha_must_be_verified_before_upload(tmp_path, kind):
    path = tmp_path / 'original'; path.write_bytes(b'original')
    candidate = str(path); sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if kind == 'relative': candidate = 'relative-original'
    elif kind == 'directory': candidate = str(tmp_path)
    elif kind == 'missing-sha': sha = None
    else: sha = 'not-a-digest'
    client = Client()
    with pytest.raises(Exception, match='attachment_unavailable'):
        run(client, 'send_file', registered_file_path=candidate, file_digest=sha)
    assert not any(isinstance(call, (functions.upload.SaveFilePartRequest,
                                   functions.messages.SendMediaRequest)) for call in client.calls)


def test_original_changed_during_upload_is_unknown_and_never_sent(tmp_path):
    path = tmp_path / 'original'; path.write_bytes(b'a' * (512 * 1024 + 3))
    class ChangingClient(Client):
        async def __call__(self, request):
            result = await super().__call__(request)
            if isinstance(request, functions.upload.SaveFilePartRequest) and request.file_part == 0:
                with path.open('r+b') as source: source.seek(512 * 1024); source.write(b'bbb')
            return result
    client = ChangingClient()
    result = run(client, 'send_file', {'mode': 'document'}, registered_file_path=str(path),
                 file_digest=hashlib.sha256(path.read_bytes()).hexdigest())
    assert result['outcome'] == 'unknown' and result['data']['error'] == 'attachment_changed_during_upload'
    assert not any(isinstance(call, functions.messages.SendMediaRequest) for call in client.calls)


def test_unverified_upload_response_blocks_final_message(tmp_path):
    path = tmp_path / 'original'; path.write_bytes(b'original')
    class Unverified(Client):
        async def __call__(self, request):
            self.calls.append(request)
            if isinstance(request, functions.upload.SaveFilePartRequest): return False
            return await super().__call__(request)
    client = Unverified()
    result = run(client, 'send_file', registered_file_path=str(path),
                 file_digest=hashlib.sha256(path.read_bytes()).hexdigest())
    assert result['outcome'] == 'unknown' and result['data']['error'] == 'provider_upload_unverified'
    assert not any(isinstance(call, functions.messages.SendMediaRequest) for call in client.calls)


@pytest.mark.parametrize('evidence', ['correlated', 'wrong-target', 'not-outgoing', 'wrong-random-id'])
def test_send_confirmation_correlates_random_id_outgoing_and_exact_target(evidence):
    class Correlated(Client):
        async def __call__(self, request):
            if isinstance(request, functions.messages.SendMediaRequest):
                self.calls.append(request)
                now = datetime.now(timezone.utc)
                native = types.Message(81, types.PeerUser(999 if evidence == 'wrong-target' else 100),
                                       now, 'sent', out=evidence != 'not-outgoing')
                random_id = request.random_id + (1 if evidence == 'wrong-random-id' else 0)
                return types.Updates([types.UpdateMessageID(81, random_id),
                                      types.UpdateNewMessage(native, 1, 1)], [], [], now, 0)
            return await super().__call__(request)
    client = Correlated()
    result = run(client, 'send_contact', {'phone': '+123456789', 'first_name': 'Contact'})
    assert result['outcome'] == ('confirmed' if evidence == 'correlated' else 'submitted')
    if evidence == 'correlated': assert result['provider_message_id'] == '81'
    else: assert 'provider_message_id' not in result
    assert sum(isinstance(call, functions.messages.SendMediaRequest) for call in client.calls) == 1


@pytest.mark.parametrize('operation,parameters', [
    ('send_file', {'caption': '😀' * 513}),
    ('send_poll', {'question': 'x' * 256, 'options': ['A', 'B']}),
    ('send_poll', {'question': 'Q', 'options': ['A' * 101, 'B']}),
    ('send_poll', {'question': 'Q', 'options': ['A']}),
    ('send_poll', {'question': 'Q', 'options': [str(n) for n in range(11)]}),
    ('send_poll', {'question': 'Q', 'options': ['', 'A']}),
    ('send_contact', {'phone': 'invalid', 'first_name': 'Name'}),
    ('send_contact', {'phone': '+123456789', 'first_name': ''}),
    ('send_location', {'latitude': 90.1, 'longitude': 0}),
    ('send_location', {'latitude': 0, 'longitude': 180.1}),
    ('send_location', {'latitude': 0, 'longitude': float('inf')}),
    ('create_group', {'title': 'x' * 129, 'user_ids': ['200']}),
    ('create_group', {'title': 'Group', 'user_ids': [str(i) for i in range(200, 221)]}),
    ('set_group_description', {'description': 'x' * 256}),
    ('send_reaction', {'emoji': 'two words'}), ('edit_message', {'text': '😀' * 2001}),
    ('update_profile', {'first_name': 'x' * 65}), ('update_profile', {'bio': 'x' * 140}),
    ('update_profile', {}),
])
def test_structured_message_and_mutation_limits_reject_before_any_rpc(operation, parameters):
    client = Client()
    with pytest.raises(Exception, match='invalid_parameters'):
        run(client, operation, parameters)
    assert client.calls == []


@pytest.mark.parametrize('changes', [{'provider_chat': 'not-numeric'}, {'provider_chat': None},
                                   {'provider_message_id': '0'}, {'provider_message_id': str(2**31)},
                                   {'provider_message_id': True}])
def test_malformed_prepared_native_identifiers_never_reach_rpc(changes):
    client = Client()
    with pytest.raises(Exception, match='invalid_parameters'):
        asyncio.run(execute(client, job('pin_message', **changes)))
    assert client.calls == []


def test_poll_is_anonymous_and_channel_admin_grants_remain_narrow():
    client = Client()
    run(client, 'send_poll', {'question': 'Question', 'options': ['One', 'Two']})
    poll = next(call.media.poll for call in client.calls if isinstance(call, functions.messages.SendMediaRequest))
    assert poll.public_voters is False and poll.multiple_choice is False
    run(client, 'promote_participant', {'user_id': '200'}, provider_chat='-1000000000100')
    rights = next(call.admin_rights for call in client.calls if isinstance(call, functions.channels.EditAdminRequest))
    assert rights.change_info is True and rights.invite_users is True and rights.pin_messages is True
    assert all(getattr(rights, field) is False for field in ('post_messages', 'edit_messages', 'delete_messages',
                  'ban_users', 'add_admins', 'anonymous', 'manage_call', 'other'))


@pytest.mark.parametrize('operation', ['delete_contact', 'block_contact', 'unblock_contact',
                                     'add_participant', 'remove_participant',
                                     'promote_participant', 'demote_participant'])
def test_owner_cannot_be_target_of_participant_or_contact_changes(operation):
    client = Client()
    with pytest.raises(Exception, match='invalid_parameters'):
        asyncio.run(execute(client, job(operation, {'user_id': '42'},
            provider_chat='-1000000000100'), verified_self=types.User(42, is_self=True)))
    forbidden = (functions.channels.InviteToChannelRequest, functions.channels.EditAdminRequest,
                 functions.contacts.DeleteContactsRequest, functions.contacts.BlockRequest,
                 functions.contacts.UnblockRequest, functions.messages.DeleteChatUserRequest)
    assert not any(isinstance(call, forbidden) for call in client.calls)


@pytest.mark.parametrize('poll_changes,error', [
    ({'closed': True}, 'invalid_parameters'), ({'multiple_choice': False}, 'invalid_parameters'),
    ({'subscribers_only': True}, 'unsupported_operation'),
    ({'countries_iso2': ['ID']}, 'unsupported_operation'),
])
def test_poll_vote_refuses_closed_restricted_or_invalid_multiple_choice(poll_changes, error):
    client = Client()
    poll = types.Poll(1, types.TextWithEntities('Q', []), [
        types.PollAnswer(types.TextWithEntities('A', []), b'a'),
        types.PollAnswer(types.TextWithEntities('B', []), b'b')], hash=1, multiple_choice=True)
    for key, value in poll_changes.items(): setattr(poll, key, value)
    client.message.media = types.MessageMediaPoll(poll, types.PollResults())
    with pytest.raises(Exception, match=error): run(client, 'vote_poll', {'option_indices': [0, 1]})
    assert not any(isinstance(call, functions.messages.SendVoteRequest) for call in client.calls)


def test_explicit_unpin_archive_unarchive_unmute_and_delete_shapes():
    client = Client()
    for operation in ['unpin_message', 'archive_chat', 'unarchive_chat', 'unmute_chat', 'delete_message']:
        assert run(client, operation)['outcome'] == 'submitted'
    assert ('unpin_message', types.InputPeerUser(100, 1), 23, {}) in client.calls
    assert ('edit_folder', types.InputPeerUser(100, 1), 1) in client.calls
    assert ('edit_folder', types.InputPeerUser(100, 1), 0) in client.calls
    assert ('delete_messages', types.InputPeerUser(100, 1), [23], {'revoke': True}) in client.calls
    settings = next(call.settings for call in client.calls
                    if isinstance(call, functions.account.UpdateNotifySettingsRequest))
    assert settings.mute_until.timestamp() == 0
    with pytest.raises(Exception, match='invalid_parameters'): run(client, 'mute_chat', {'seconds': 2**31 - 1})


def test_channel_removal_is_unsupported_and_unavailable_invite_never_revokes():
    client = Client()
    with pytest.raises(Exception, match='unsupported_operation'):
        run(client, 'remove_participant', {'user_id': '200'}, provider_chat='-1000000000100')
    assert not any(isinstance(call, functions.channels.EditBannedRequest) for call in client.calls)
    client.responses[functions.messages.GetFullChatRequest] = NS(full_chat=NS(exported_invite=None))
    with pytest.raises(Exception, match='invite_link_unavailable'):
        run(client, 'revoke_invite_link', provider_chat='-100')
    assert not any(isinstance(call, functions.messages.EditExportedChatInviteRequest) for call in client.calls)


def test_forwarding_source_policy_is_checked_before_provider_lookup():
    client = Client()
    policy = {'enabled': True, 'discover_chats': False, 'include_chats': ['100'], 'exclude_chats': []}
    with pytest.raises(Exception, match='operation_access_changed'):
        run(client, 'forward_message', source_provider_chat='200', source_provider_message='23', _policy=policy)
    assert client.calls == []
