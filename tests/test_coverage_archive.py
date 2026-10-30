"""Missing contract edges identified by the assertion-to-spec coverage review.

Fixtures are synthetic; provider methods and owner sessions are never activated.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState, validate_capture
from messaging_service.envelope import MAX_ENVELOPE, fingerprint, stable_id, validate
from test_archive import archive as archive, event


@pytest.mark.parametrize('field,maximum', [
    ('account', 64), ('chat', 160), ('chat_title', 512), ('provider_id', 256),
    ('sender', 160), ('body', 65536),
])
def test_envelope_text_fields_accept_boundary_and_reject_overflow_or_nul(field, maximum):
    assert validate(event(**{field: 'a' * maximum}))[field] == 'a' * maximum
    for value in ['a' * (maximum + 1), 'a\x00b']:
        with pytest.raises(ValueError, match='invalid_' + field):
            validate(event(**{field: value}))


def test_envelope_size_clock_and_identity_are_strict():
    with pytest.raises(ValueError, match='envelope_too_large'):
        validate(event(body='x' * (MAX_ENVELOPE + 1)))
    with pytest.raises(ValueError, match='unknown_envelope_fields'):
        validate(event(provider_secret='synthetic-only'))
    for account in ['space forbidden', 'unicode-ж', '']:
        with pytest.raises(ValueError, match='invalid_account'):
            validate(event(account=account))
    normalized = validate(event(sent_at='2026-10-01T20:00:00+08:00',
                                updated_at='2026-10-01T20:01:00+08:00'))
    assert normalized['sent_at'] == '2026-10-01T12:00:00+00:00'
    assert normalized['updated_at'] == '2026-10-01T12:01:00+00:00'
    with pytest.raises(ValueError, match='invalid_update_time'):
        validate(event(updated_at='2026-10-01T11:59:00Z'))
    assert stable_id('msg', 'a', '100', '17') == stable_id('msg', 'a', '100', '17')
    assert len({stable_id('msg', *parts) for parts in
                [('a', '100', '17'), ('b', '100', '17'), ('a', '101', '17'), ('a', '100', '18')]}) == 4
    before = validate(event())
    observation = validate(event(origin='backfill', updated_at='2026-10-01T12:01:00Z',
        chat_title='Observed title', sender_profile={'display_name': 'Observed name', 'username': '@hint'},
        metadata={'sender_is_bot': None, 'sender_identity_basis': None}))
    assert fingerprint(before) == fingerprint(observation)


@pytest.mark.parametrize('field,values', [
    ('platform', ['sms', None]), ('direction', ['sideways', None]),
    ('origin', ['web', None]), ('deleted', ['false', 0]),
])
def test_envelope_enum_and_boolean_fields_never_coerce(field, values):
    for value in values:
        with pytest.raises(ValueError, match='invalid_' + field):
            validate(event(**{field: value}))


def test_envelope_media_and_profile_bounds_keep_unknowns_explicit():
    media = {'provider_id': '1', 'filename': '../header\r\n.txt', 'mime': 'text/plain', 'bytes': 0}
    accepted = validate(event(media=[media], sender_profile={'display_name': 'X' * 512,
        'username': '@fixture'}, metadata={'mentions_owner': None, 'reply_to_owner': False}))
    assert accepted['sender_profile']['username'] == 'fixture'
    assert accepted['metadata'] == {'mentions_owner': None, 'reply_to_owner': False}
    assert accepted['media'][0]['filename'] == '.._header__.txt'
    for size in [True, -1, 8 * 1024**3 + 1]:
        with pytest.raises(ValueError, match='invalid_media_size'):
            validate(event(media=[{**media, 'bytes': size}]))
    assert validate(event(media=[{**media, 'bytes': 8 * 1024**3}]))['media'][0]['bytes'] == 8 * 1024**3
    assert validate(event(media=[{**media, 'bytes': None}]))['media'][0]['bytes'] is None
    with pytest.raises(ValueError, match='duplicate_media_id'):
        validate(event(media=[media, media]))
    with pytest.raises(ValueError, match='invalid_media'):
        validate(event(media=[{**media, 'provider_id': str(n)} for n in range(33)]))
    assert len(validate(event(media=[{**media, 'provider_id': str(n)} for n in range(32)]))['media']) == 32
    for mime in ['text', 'text/', '/plain', 'text/plain\r\nInjected: header']:
        with pytest.raises(ValueError, match='invalid_mime'):
            validate(event(media=[{**media, 'mime': mime}]))
    for profile in [{'password': 'synthetic'}, {'display_name': 'x' * 513}, {'username': 'x' * 129}]:
        with pytest.raises(ValueError):
            validate(event(sender_profile=profile))
    for metadata in [{'sender_is_bot': 1}, {'chat_type': 'made-up'}, {'media_kind': 'pdf'},
                     {'sender_identity_basis': 'guessed'}, {'secret': 'synthetic'}]:
        with pytest.raises(ValueError, match='invalid_metadata'):
            validate(event(metadata=metadata))


@pytest.mark.parametrize('kind,chat,ids', [
    ('telegram_delete', '', [1, 2**63 - 1]),
    ('telegram_delete', '-100123', [3, 2, 3]),
    ('whatsapp_delete', '123@c.us', ['a', 'b']),
    ('whatsapp_delete', '123@g.us', ['a']),
    ('whatsapp_delete', '123@lid', ['a']),
])
def test_delete_captures_preserve_namespace_and_deduplicate_ids(kind, chat, ids):
    value = validate_capture({'kind': kind, 'account': 'fixture', 'chat': chat,
        'ids': ids, 'observed_at': '2026-10-01T20:00:00+08:00'})
    assert value['chat'] == chat and value['ids'] == sorted(set(ids))
    assert value['observed_at'] == '2026-10-01T12:00:00+00:00'


@pytest.mark.parametrize('kind,chat,ids', [
    ('telegram_delete', '100', []), ('telegram_delete', '100', list(range(1, 1002))),
    ('telegram_delete', '100', [True]), ('telegram_delete', '100', [0]),
    ('telegram_delete', '100', [2**63]), ('telegram_delete', '100', ['1']),
    ('telegram_delete', 'channel-name', [1]), ('whatsapp_delete', '', ['a']),
    ('whatsapp_delete', '100@evil', ['a']), ('whatsapp_delete', '100@c.us', [1]),
    ('whatsapp_delete', '100@c.us', ['']), ('whatsapp_delete', '100@c.us', ['a\x00b']),
])
def test_delete_capture_invalid_bounds_never_reach_archive(kind, chat, ids):
    with pytest.raises(ValueError):
        validate_capture({'kind': kind, 'account': 'fixture', 'chat': chat,
            'ids': ids, 'observed_at': '2026-10-01T12:00:00Z'})
    for account in ['space forbidden', 'x' * 65, None]:
        with pytest.raises(ValueError, match='invalid_capture_account'):
            validate_capture({'kind': kind, 'account': account,
                'chat': '100' if kind == 'telegram_delete' else '100@c.us',
                'ids': [17] if kind == 'telegram_delete' else ['a'],
                'observed_at': '2026-10-01T12:00:00Z'})


@pytest.mark.parametrize('attribute,expected', [
    ('voice', 'voice'), ('video_note', 'video'), ('sticker', 'sticker'),
    ('gif', 'animation'), ('video', 'video'), ('audio', 'audio'),
    ('photo', 'photo'), ('document', 'document'),
])
def test_telegram_cached_normalizer_preserves_media_kind_forward_and_explicit_title(attribute, expected):
    from telethon import types
    from messaging_service.adapters import telegram_message
    def forbidden_lookup():
        raise AssertionError('normalization must not call a provider')
    message = SimpleNamespace(chat_id=100, id=17, sender_id=42, message='Synthetic message',
        date=datetime(2026, 10, 1, 12, tzinfo=timezone.utc), edit_date=None,
        out=True, file=None, document=None, photo=None, sender=None,
        peer_id=types.PeerUser(100), chat=types.User(id=100, first_name='Cached title'),
        fwd_from=object(), get_sender=forbidden_lookup, get_chat=forbidden_lookup)
    setattr(message, attribute, object())
    normalized = telegram_message('synthetic-normalizer', message, chat_title='Explicit title')
    assert normalized['chat_title'] == 'Explicit title'
    assert normalized['direction'] == 'outgoing'
    assert normalized['metadata']['forwarded'] is True
    assert normalized['metadata']['media_kind'] == expected
    assert normalized['metadata']['chat_type'] == 'private'
    assert normalized['metadata']['sender_is_bot'] is None
    if attribute == 'voice':
        message.audio = message.video = message.sticker = message.gif = object()
        assert telegram_message('synthetic-normalizer', message)['metadata']['media_kind'] == 'voice'


def test_tombstone_commit_survives_publication_failure_and_latest_time_wins(archive, monkeypatch):
    ar, store, key, _ = archive
    epoch = ar.acquire('archive-test', 'coverage-worker')
    state = CollectorState(ar, 'archive-test', ('coverage-worker', epoch))
    state.apply({'kind': 'message', 'peer_kind': 'user', 'envelope': event()})
    mid = stable_id('msg', 'archive-test', '100', '17')
    original = state.apply_tombstones
    def fail_publication():
        raise RuntimeError('synthetic-after-tombstone-commit')
    monkeypatch.setattr(state, 'apply_tombstones', fail_publication)
    deletion = {'kind': 'telegram_delete', 'account': 'archive-test', 'chat': '100',
                'ids': [17], 'observed_at': '2026-10-01T12:03:00Z'}
    with pytest.raises(RuntimeError, match='after-tombstone-commit'):
        state.apply(deletion)
    assert not store.get_context(key, mid)['message']['deleted']
    with pytest.raises(RuntimeError):
        state.apply({**deletion, 'observed_at': '2026-10-01T12:02:00Z'})
    with store.pool.connection() as c:
        row = c.execute('SELECT observed_at FROM provider_tombstones WHERE account=%s',
                        ('archive-test',)).fetchone()
        assert row['observed_at'].isoformat() == '2026-10-01T12:03:00+00:00'
    monkeypatch.setattr(state, 'apply_tombstones', original)
    assert state.apply_tombstones() == 1
    message = store.get_context(key, mid)['message']
    assert message['deleted'] and message['body'] == ''
    assert state.apply_tombstones() == 0
    assert ar.ingest(event(origin='backfill'))['outcome'] == 'unchanged'
    assert store.get_context(key, mid)['message']['deleted']


def test_sender_identity_provenance_uses_current_metadata_then_snapshot_never_names(archive):
    ar, store, key, other = archive
    mid = ar.ingest(event(direction='outgoing', sender_profile={'display_name': 'Not proof'},
        metadata={'chat_type': 'private', 'sender_identity_basis': 'authenticated_self'}))['message']
    assert store.search(key)['items'][0]['sender_identity_basis'] == 'authenticated_self'
    with store.pool.connection() as c:
        c.execute('DELETE FROM message_metadata WHERE message=%s', (mid,))
    assert store.get_context(key, mid)['message']['sender_identity_basis'] == 'authenticated_self'
    with store.pool.connection() as c:
        c.execute("UPDATE message_revisions SET snapshot=snapshot-'metadata' WHERE message=%s", (mid,))
    assert store.get_context(key, mid)['message']['sender_identity_basis'] is None
    assert all(row['id'] != mid for row in store.search(other)['items'])


def test_operator_replay_keeps_policy_and_idempotency_without_collector_lease(seeded, tmp_path, monkeypatch, capsys):
    import messaging_service.__main__ as cli
    import messaging_service.spool as spool_module
    store, _, _ = seeded
    ar = Archive(store)
    ar.register('replay-fixture', 'telegram', 'Synthetic replay', include_chats=['100'])
    infile = tmp_path / 'fixtures.jsonl'
    value = event(account='replay-fixture', origin='fixture')
    infile.write_text('\n'.join(json.dumps(x) for x in [value, value, {**value, 'chat': 'excluded'}]) + '\n')
    pending = spool_module.DurableSpool(tmp_path / 'spool')
    for provider in range(1, 102):
        pending.put({**value, 'provider_id': str(provider)})
    drained = []
    class ObservedSpool(spool_module.DurableSpool):
        def drain(self, ingest, limit=100):
            result = super().drain(ingest, limit)
            drained.append(result)
            return result
    monkeypatch.setattr(spool_module, 'DurableSpool', ObservedSpool)
    dsn = tmp_path / 'test.dsn'; dsn.write_text(store.pool.conninfo); dsn.chmod(0o600)
    class BorrowedStore:
        def __getattr__(self, name):
            return getattr(store, name)
        def close(self):
            pass  # The seeded fixture owns this connection pool.
    monkeypatch.setattr(cli, 'Store', lambda *_: BorrowedStore())
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn))
    monkeypatch.setattr(sys, 'argv', ['anymsg', 'replay', '--input', str(infile), '--spool', str(tmp_path / 'spool')])
    cli.main()
    assert json.loads(capsys.readouterr().out) == {'queued': 3, 'final_drain': 0}
    assert len(drained) == 4 and drained[0] == 100 and drained[-1] == 0
    assert all(count <= 100 for count in drained)
    with store.pool.connection() as c:
        assert c.execute("SELECT count(*) n FROM messages WHERE account='replay-fixture'").fetchone()['n'] == 101
        assert c.execute("""SELECT count(*) n FROM message_revisions r JOIN messages m ON m.id=r.message
            WHERE m.account='replay-fixture'""").fetchone()['n'] == 101
        assert c.execute("SELECT count(*) n FROM collector_leases WHERE account='replay-fixture'").fetchone()['n'] == 0
    assert not list((tmp_path / 'spool').glob('*.json'))
    infile.write_bytes(b'x' * (MAX_ENVELOPE + 2))
    with pytest.raises(ValueError, match='envelope_too_large'):
        cli.main()
    assert not list((tmp_path / 'spool').glob('*.json'))


def test_collector_role_has_only_contract_grants_and_immutable_dispatch_payloads(seeded):
    from messaging_service import archive as module
    store, _, _ = seeded
    role = 'coverage_collector_contract'
    ddl = Path(module.__file__).with_name('collector_role.sql').read_text().replace('messaging_collector', role)
    assert 'PASSWORD' not in '\n'.join(line.split('--', 1)[0] for line in ddl.splitlines()).upper()
    with store.pool.connection() as c:
        c.execute(ddl)
    try:
        with store.pool.connection() as c:
            attrs = c.execute('''SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,
                rolbypassrls,rolinherit FROM pg_roles WHERE rolname=%s''', (role,)).fetchone()
            assert attrs == {'rolcanlogin': True, 'rolsuper': False, 'rolcreatedb': False,
                'rolcreaterole': False, 'rolreplication': False, 'rolbypassrls': False, 'rolinherit': False}
            c.execute('SET LOCAL ROLE ' + role)
            def table(name, operation):
                return c.execute('SELECT has_table_privilege(current_user,%s,%s) allowed',
                                 (name, operation)).fetchone()['allowed']
            def column(name, field, operation):
                return c.execute('SELECT has_column_privilege(current_user,%s,%s,%s) allowed',
                                 (name, field, operation)).fetchone()['allowed']
            readonly = ['accounts', 'account_policies', 'probe_metadata', 'archive_migrations']
            for name in readonly:
                assert table(name, 'SELECT')
                assert not any(table(name, action) for action in ['INSERT', 'UPDATE', 'DELETE'])
            for name in ['collector_leases', 'collector_chats', 'provider_tombstones', 'whatsapp_checkpoints',
                         'chats', 'messages', 'attachments', 'ingest_receipts', 'message_revisions', 'media_jobs',
                         'sender_profiles', 'message_metadata', 'collector_health', 'message_conflicts',
                         'whatsapp_identity_observations', 'chat_read_observations']:
                assert all(table(name, action) for action in ['SELECT', 'INSERT', 'UPDATE'])
                assert not table(name, 'DELETE')
            assert all(table('media_reservations', action) for action in ['SELECT', 'INSERT', 'UPDATE', 'DELETE'])
            for name in ['source_status', 'feed_counter']:
                assert table(name, 'SELECT') and table(name, 'UPDATE')
                assert not table(name, 'INSERT') and not table(name, 'DELETE')
            for name in ['events', 'message_receipts']:
                assert table(name, 'SELECT') and table(name, 'INSERT')
                assert not table(name, 'UPDATE') and not table(name, 'DELETE')
            for name in ['consumers', 'download_grants', 'reply_dismissals']:
                assert not any(table(name, action) for action in ['SELECT', 'INSERT', 'UPDATE', 'DELETE'])
            assert all(column('api_keys', field, 'SELECT') for field in
                ['id', 'principal', 'revoked', 'expires_at', 'permissions', 'permission_version'])
            assert not column('api_keys', 'digest', 'SELECT')
            assert all(column('principals', field, 'SELECT') for field in
                ['id', 'accounts', 'chat_ids', 'permissions', 'enabled', 'scope_version'])
            assert table('outgoing_messages', 'SELECT') and table('messenger_commands', 'SELECT')
            for name, mutable, immutable in [
                ('outgoing_messages', ['status', 'started_at', 'finished_at', 'lease_epoch',
                                       'provider_message_id', 'error_code', 'archive_reference'], ['body', 'key_id', 'request_id']),
                ('messenger_commands', ['status', 'started_at', 'finished_at', 'lease_epoch',
                                        'result', 'error_code'], ['payload', 'key_id', 'request_id']),
                ('receipt_event_jobs', ['cursor_id', 'complete'], ['account', 'receipt']),
                ('receipt_event_publications', ['receipt', 'state', 'published_at'], ['message']),
            ]:
                assert all(column(name, field, 'UPDATE') for field in mutable)
                assert not any(column(name, field, 'UPDATE') for field in immutable)
            assert all(column('audit_log', field, 'INSERT') for field in
                ['principal', 'key_id', 'operation', 'outcome', 'error_code', 'resource_ids', 'actor'])
            assert not table('audit_log', 'SELECT') and not table('audit_log', 'UPDATE')
    finally:
        with store.pool.connection() as c:
            c.execute('DROP OWNED BY ' + role)
            c.execute('DROP ROLE ' + role)


def test_tombstones_publish_in_bounded_batches_without_rewinding_source_time(archive):
    ar, store, key, _ = archive
    epoch = ar.acquire('archive-test', 'coverage-batch-worker')
    state = CollectorState(ar, 'archive-test', ('coverage-batch-worker', epoch))
    for n in range(1, 102):
        state.apply({'kind': 'message', 'peer_kind': 'user', 'envelope': event(
            provider_id=str(n), updated_at='2026-10-01T12:10:00Z')})
    state.apply({'kind': 'telegram_delete', 'account': 'archive-test', 'chat': '100',
                 'ids': list(range(1, 102)), 'observed_at': '2026-10-01T12:03:00Z'})
    with store.pool.connection() as c:
        assert c.execute("SELECT count(*) n FROM messages WHERE account='archive-test' AND deleted").fetchone()['n'] == 100
    assert state.apply_tombstones() == 1
    assert state.apply_tombstones() == 0
    with store.pool.connection() as c:
        rows = c.execute("""SELECT m.deleted,m.revision,r.snapshot FROM messages m
            JOIN message_revisions r ON r.message=m.id AND r.revision=m.revision
            WHERE m.account='archive-test'""").fetchall()
    assert len(rows) == 101
    assert all(row['deleted'] and row['revision'] == 2 for row in rows)
    assert all(row['snapshot']['updated_at'] == '2026-10-01T12:10:00+00:00' for row in rows)
    assert all(row['snapshot']['body'] == '' and row['snapshot']['media'] == [] for row in rows)


@pytest.mark.parametrize('platform,chat,provider', [
    ('telegram', '100', 17), ('whatsapp', '100@c.us', 'false_100@c.us_17'),
])
def test_excluded_provider_deletion_does_not_persist_tombstone_or_change_message(seeded, platform, chat, provider):
    store, _, _ = seeded
    ar = Archive(store)
    account = 'coverage-excluded-' + platform
    ar.register(account, platform, 'Synthetic exclusion', include_chats=[chat])
    epoch = ar.acquire(account, 'coverage-excluded-worker')
    state = CollectorState(ar, account, ('coverage-excluded-worker', epoch))
    mid = state.apply({'kind': 'message', 'peer_kind': 'user', 'envelope': event(
        account=account, platform=platform, chat=chat, provider_id=str(provider))})['message']
    ar.register(account, platform, 'Synthetic exclusion', include_chats=[chat], exclude_chats=[chat])
    assert state.apply({'kind': platform + '_delete', 'account': account, 'chat': chat,
        'ids': [provider], 'observed_at': '2026-10-01T12:03:00Z'}) == {'outcome': 'excluded'}
    with store.pool.connection() as c:
        assert c.execute('SELECT count(*) n FROM provider_tombstones WHERE account=%s', (account,)).fetchone()['n'] == 0
        row = c.execute('SELECT deleted,revision FROM messages WHERE id=%s', (mid,)).fetchone()
        assert row == {'deleted': False, 'revision': 1}


def test_receipt_publication_leaves_ack_and_owner_read_observations_unchanged(seeded):
    store, _, _ = seeded
    ar = Archive(store)
    account = 'coverage-receipt-independence'
    ar.register(account, 'telegram', 'Synthetic receipts', include_chats=['100'])
    key = store.issue_key('coverage-receipt-reader', [account])
    epoch = ar.acquire(account, 'coverage-receipt-worker')
    state = CollectorState(ar, account, ('coverage-receipt-worker', epoch))
    for provider in ['10', '11']:
        state.apply({'kind': 'message', 'peer_kind': 'user', 'envelope': event(
            account=account, provider_id=provider, direction='outgoing')})
    first = store.get_updates(key, 'independent', limit=1)
    assert store.ack_updates(key, 'independent', first['receipt'], 0)['acked'] is True
    with store.pool.connection() as c:
        before = c.execute("SELECT * FROM consumers WHERE name='independent'").fetchone()
        assert c.execute('SELECT count(*) n FROM chat_read_observations WHERE account=%s', (account,)).fetchone()['n'] == 0
    state.apply({'kind': 'message_receipt', 'account': account, 'platform': 'telegram',
        'chat': '100', 'provider_id': None, 'max_id': 11, 'state': 'read',
        'basis': 'telegram_outbox_boundary', 'audience': 'private_peer',
        'observed_at': '2026-10-14T08:00:00Z', 'provider_at': None})
    assert state.publish_receipts() == 2
    with store.pool.connection() as c:
        assert c.execute("SELECT * FROM consumers WHERE name='independent'").fetchone() == before
        assert c.execute('SELECT count(*) n FROM chat_read_observations WHERE account=%s', (account,)).fetchone()['n'] == 0
        assert c.execute('SELECT count(*) n FROM message_revisions r JOIN messages m ON m.id=r.message WHERE m.account=%s', (account,)).fetchone()['n'] == 2
