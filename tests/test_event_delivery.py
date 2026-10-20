"""Isolated credential-owned event consumption, hints and local feedback."""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from messaging_service.store import APIError
from messaging_service.subscriptions import match_subscriptions


def identity(store, name='event-reader', **changes):
    permissions = ['read', 'subscription-write', 'subscription-ack', 'feedback-write']
    return store.issue_key(name, ['telegram-demo'], permissions=permissions,
        key_permissions=permissions, **changes)


def call(store, token, op, *args, **kwargs):
    if op in ('submit_feedback', 'list_feedback'):
        from messaging_service import feedback as module
    elif op in ('configure_subscription_delivery', 'test_subscription'):
        from messaging_service import event_delivery as module
    else:
        from messaging_service import subscription_consumption as module
    with store.authorized(token, op) as (c, p):
        return getattr(module, op)(store, c, p, *args, **kwargs)


def subscription(store, token):
    return store.create_subscription(token, ['telegram-demo-chat'])['subscription_id']


def emit(store):
    with store.pool.connection() as c:
        return store.append_event(c, 'telegram-demo', 'demo-tg-1', origin='live')


def test_page_receipt_ack_is_explicit_independent_and_idempotent(seeded):
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    first, second = emit(store), emit(store)
    match_subscriptions(store)
    page = call(store, token, 'fetch_subscription_events', sid, limit=1)
    assert [e['event_id'] for e in page['items']] == [first]
    assert page['items'][0]['wake_reason'] == ['incoming_message']
    assert page['ack_version'] == 1 and page['has_more']
    assert len(store.get_subscription_events(token, sid)['items']) == 2
    assert call(store, token, 'fetch_subscription_events', sid, limit=1)['items'] == page['items']
    receipt = page['receipt']
    with store.pool.connection() as c:
        issued = c.execute('SELECT receipt_hash FROM subscription_receipts').fetchone()
        assert issued['receipt_hash'] == hashlib.sha256(receipt.encode()).hexdigest()
        assert receipt not in str(c.execute('SELECT * FROM subscription_receipts').fetchall())
    ack = call(store, token, 'ack_subscription_events', sid, receipt, page['ack_version'])
    assert ack['acked_through'] == first and ack['ack_version'] == 2
    assert call(store, token, 'ack_subscription_events', sid, receipt, 1) == ack
    following = call(store, token, 'fetch_subscription_events', sid)
    assert [e['event_id'] for e in following['items']] == [second]
    assert len(store.get_subscription_events(token, sid)['items']) == 2


def test_receipts_fence_key_versions_page_and_lifecycle(seeded):
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    emit(store)
    match_subscriptions(store)
    page = call(store, token, 'fetch_subscription_events', sid)
    other = identity(store)
    with pytest.raises(APIError, match='not_found'):
        call(store, other, 'ack_subscription_events', sid, page['receipt'], 1)
    with pytest.raises(APIError, match='invalid_receipt'):
        call(store, token, 'ack_subscription_events', sid, 'forged-token-that-is-not-a-receipt', 1)
    with pytest.raises(APIError, match='stale_ack'):
        call(store, token, 'ack_subscription_events', sid, page['receipt'], 2)
    with store.pool.connection() as c:
        c.execute("UPDATE subscription_receipts SET expires_at=clock_timestamp()-interval '1 second'")
    with pytest.raises(APIError, match='invalid_receipt'):
        call(store, token, 'ack_subscription_events', sid, page['receipt'], 1)
    page = call(store, token, 'fetch_subscription_events', sid)
    store.update_subscription(token, sid, 1, action='pause')
    with pytest.raises(APIError, match='subscription_closed'):
        call(store, token, 'ack_subscription_events', sid, page['receipt'], 1)


def test_new_permissions_never_enter_legacy_ceilings(seeded):
    store, old, _ = seeded
    store.set_access('agent-a', permissions=['read', 'subscription-ack', 'feedback-write'])
    assert 'subscription-ack' not in store.capabilities(old)['permissions']
    with pytest.raises(APIError, match='forbidden'):
        call(store, old, 'submit_feedback', 'synthetic-feedback', 'bug', 'Synthetic failure')


def test_feedback_is_bounded_key_owned_and_reference_scoped(seeded):
    store, _, _ = seeded
    token = identity(store)
    result = call(store, token, 'submit_feedback', 'synthetic-feedback', 'bug',
        'Synthetic tool failure', operation='get_messages', refs={'message_ids': ['demo-tg-1']})
    assert result['status'] == 'recorded' and result['external_delivery'] is False
    assert call(store, token, 'submit_feedback', 'synthetic-feedback', 'bug',
        'Synthetic tool failure', operation='get_messages', refs={'message_ids': ['demo-tg-1']}) == result
    with pytest.raises(APIError, match='idempotency_conflict'):
        call(store, token, 'submit_feedback', 'synthetic-feedback', 'bug', 'Different')
    assert len(call(store, token, 'list_feedback')['items']) == 1
    assert call(store, identity(store), 'list_feedback')['items'] == []
    with pytest.raises(APIError, match='not_found'):
        call(store, token, 'submit_feedback', 'synthetic-hidden', 'bug', 'Hidden',
            refs={'chat_ids': ['whatsapp-demo-1-chat']})
    with pytest.raises(APIError, match='invalid_feedback'):
        call(store, token, 'submit_feedback', 'synthetic-large', 'bug', 'x' * 4001)


def receiver(store, token, tmp_path):
    from messaging_service.event_delivery import register_receiver
    secret = tmp_path / 'receiver.key'
    secret.write_bytes(b'synthetic receiver secret bytes 0123456789')
    secret.chmod(0o600)
    with store.authorized(token) as (_, p):
        key_id = str(p['key_id'])
    rid = str(uuid4())
    register_receiver(store, rid, 'event-reader', key_id, 'https://hooks.example.test/wake',
        str(secret), resolver=lambda *a, **k: ['93.184.216.34'])
    return rid, secret


def test_signed_opaque_hints_retry_and_do_not_ack_events(seeded, tmp_path):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    rid, secret = receiver(store, token, tmp_path)
    call(store, token, 'configure_subscription_delivery', sid, 1, rid)
    event = emit(store)
    match_subscriptions(store)
    attempts = []
    def transport(endpoint, body, headers, **kwargs):
        attempts.append((endpoint, body, headers))
        return 503 if len(attempts) == 1 else 204
    assert dispatch_hints(store, transport=transport, resolver=lambda *a, **k: ['93.184.216.34'])['retried'] == 1
    with store.pool.connection() as c:
        c.execute('UPDATE subscription_outbox SET next_attempt_at=clock_timestamp()')
    assert dispatch_hints(store, transport=transport, resolver=lambda *a, **k: ['93.184.216.34'])['delivered'] == 1
    assert attempts[0][1] == attempts[1][1]
    payload = json.loads(attempts[0][1])
    assert payload['host_turn_verified'] is False
    assert set(payload) == {'hint_id', 'subscription_id', 'subscription_version', 'through', 'kind', 'host_turn_verified'}
    assert b'demo-person' not in attempts[0][1] and b'body' not in attempts[0][1]
    for _, body, headers in attempts:
        signed = headers['X-DeedKit-Timestamp'].encode() + b'.' + body
        assert headers['X-DeedKit-Signature'] == 'sha256=' + hmac.new(secret.read_bytes(), signed, hashlib.sha256).hexdigest()
    assert call(store, token, 'fetch_subscription_events', sid)['items'][0]['event_id'] == event
    assert not call(store, token, 'fetch_subscription_events', sid).get('acked')


def test_receiver_validation_and_revocation_fail_closed(seeded, tmp_path):
    from messaging_service.event_delivery import validate_endpoint, dispatch_hints
    for endpoint, addresses in [('http://example.com/wake', ['93.184.216.34']),
            ('https://user@example.com/wake', ['93.184.216.34']),
            ('https://example.com/wake', ['127.0.0.1']),
            ('https://example.com/wake', ['93.184.216.34', '::1'])]:
        with pytest.raises(ValueError, match='unsafe_receiver'):
            validate_endpoint(endpoint, resolver=lambda *a, **k: addresses)
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    rid, _ = receiver(store, token, tmp_path)
    call(store, token, 'configure_subscription_delivery', sid, 1, rid)
    emit(store)
    match_subscriptions(store)
    store.revoke(token)
    assert dispatch_hints(store, transport=lambda *a, **k: pytest.fail('revoked hint sent'),
        resolver=lambda *a, **k: ['93.184.216.34'])['cancelled'] == 1


def test_synthetic_subscription_probe_is_idempotent_and_rate_bounded(seeded, tmp_path):
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    rid, _ = receiver(store, token, tmp_path)
    call(store, token, 'configure_subscription_delivery', sid, 1, rid)
    first = call(store, token, 'test_subscription', sid, 'synthetic-probe')
    assert first['host_turn_verified'] is False and first['kind'] == 'test'
    assert call(store, token, 'test_subscription', sid, 'synthetic-probe') == first
    with pytest.raises(APIError, match='rate_limited'):
        call(store, token, 'test_subscription', sid, 'another-probe')
    assert store.get_subscription_events(token, sid)['items'] == []


def test_wait_uses_short_transactions_and_cancels_without_connection():
    from messaging_service.subscription_consumption import wait_for_subscription_events
    class FakeStore:
        def __init__(self): self.calls = 0
        def fetch_subscription_events(self, token, sid, *, limit):
            self.calls += 1
            return {'items': [], 'subscription': {'state': 'active'}}
    async def run():
        store = FakeStore()
        result = await wait_for_subscription_events(store, 'opaque', str(uuid4()), timeout=0.02)
        assert result['wait_status'] == 'timeout' and store.calls >= 1
        task = asyncio.create_task(wait_for_subscription_events(store, 'opaque', str(uuid4()), timeout=1))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(run())


def test_concurrent_receipts_cannot_skip_a_page_and_revocation_invalidates(seeded):
    from concurrent.futures import ThreadPoolExecutor
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    emit(store)
    emit(store)
    match_subscriptions(store)
    small = call(store, token, 'fetch_subscription_events', sid, limit=1)
    larger = call(store, token, 'fetch_subscription_events', sid, limit=2)
    def ack(page):
        try:
            return call(store, token, 'ack_subscription_events', sid, page['receipt'], 1)
        except APIError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(ack, [small, larger]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count('stale_ack') == 1
    page = call(store, token, 'fetch_subscription_events', sid)
    if not page['items']:
        emit(store)
        match_subscriptions(store)
        page = call(store, token, 'fetch_subscription_events', sid)
    store.set_access('event-reader', chats=[])
    with pytest.raises(APIError, match='subscription_access_changed'):
        call(store, token, 'ack_subscription_events', sid, page['receipt'], page['ack_version'])


def test_hint_lease_recovers_and_attempt_budget_and_redirect_are_terminal(seeded, tmp_path):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    rid, _ = receiver(store, token, tmp_path)
    call(store, token, 'configure_subscription_delivery', sid, 1, rid)
    emit(store)
    match_subscriptions(store)
    with store.pool.connection() as c:
        c.execute("UPDATE subscription_outbox SET state='leased',lease_until=clock_timestamp()-interval '1 second',attempts=7")
    assert dispatch_hints(store, transport=lambda *a, **k: 503,
        resolver=lambda *a, **k: ['93.184.216.34'])['dead_letter'] == 1
    emit(store)
    match_subscriptions(store)
    assert dispatch_hints(store, transport=lambda *a, **k: 302,
        resolver=lambda *a, **k: ['93.184.216.34'])['dead_letter'] == 1
    with store.pool.connection() as c:
        reasons = {row['error_code'] for row in c.execute('SELECT error_code FROM subscription_outbox')}
    assert reasons == {'delivery_budget_exhausted', 'redirect_rejected'}


def test_first_native_metadata_is_immutable_wake_evidence(seeded):
    from messaging_service.archive import Archive
    from messaging_service.envelope import stable_id
    store, _, _ = seeded
    archive = Archive(store)
    archive.register('synthetic-native', 'telegram', 'Synthetic native', include_chats=['100'])
    token = store.issue_key('native-reader', ['synthetic-native'],
        permissions=['read', 'subscription-write'], key_permissions=['read', 'subscription-write'])
    envelope = dict(account='synthetic-native', platform='telegram', chat='100', chat_title='Synthetic',
        provider_id='17', sender='42', body='Synthetic', sent_at='2026-10-01T12:00:00Z',
        updated_at='2026-10-01T12:00:00Z', deleted=False, direction='incoming', origin='live', media=[],
        metadata={'mentions_owner': True, 'reply_to_owner': True, 'reply_to_provider_id': '16'})
    archive.ingest({**envelope, 'provider_id': '16', 'metadata': {}})
    sid = store.create_subscription(token, [stable_id('chat', 'synthetic-native', '100')])['subscription_id']
    mid = archive.ingest(envelope)['message']
    match_subscriptions(store)
    page = call(store, token, 'fetch_subscription_events', sid)
    assert page['items'][0]['wake_reason'] == ['incoming_message', 'owner_mention', 'reply_to_owner']
    archive.ingest({**envelope, 'metadata': {'mentions_owner': False, 'reply_to_owner': False}})
    assert call(store, token, 'fetch_subscription_events', sid)['items'] == page['items']
    with store.pool.connection() as c:
        assert c.execute('SELECT snapshot FROM events WHERE message=%s ORDER BY id LIMIT 1',
            (mid,)).fetchone()['snapshot']['metadata']['mentions_owner'] is True


def test_wait_deadline_and_cancellation_keep_slow_read_admission(monkeypatch):
    import threading
    import time
    from messaging_service import subscription_consumption as consumption
    monkeypatch.setattr(consumption, '_WAIT_SLOTS', threading.BoundedSemaphore(1))
    entered, release = threading.Event(), threading.Event()
    class SlowStore:
        def fetch_subscription_events(self, *args, **kwargs):
            entered.set()
            release.wait(2)
            return {'items': [], 'subscription': {'state': 'active'}}
    async def run():
        store = SlowStore()
        started = time.monotonic()
        with pytest.raises(APIError, match='subscription_wait_timeout'):
            await consumption.wait_for_subscription_events(store, 'opaque', str(uuid4()), timeout=0.03)
        assert time.monotonic() - started < 0.3 and entered.is_set()
        with pytest.raises(APIError, match='service_busy'):
            await consumption.wait_for_subscription_events(store, 'opaque', str(uuid4()), timeout=0.03)
        release.set()
        for _ in range(100):
            if consumption._WAIT_SLOTS.acquire(blocking=False):
                consumption._WAIT_SLOTS.release()
                break
            await asyncio.sleep(0.001)
        else:
            pytest.fail('Detached read leaked its admission slot')
        entered.clear()
        release.clear()
        task = asyncio.create_task(consumption.wait_for_subscription_events(store, 'opaque', str(uuid4()), timeout=1))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        with pytest.raises(APIError, match='service_busy'):
            await consumption.wait_for_subscription_events(store, 'opaque', str(uuid4()), timeout=0.03)
        release.set()
    asyncio.run(run())


def test_wake_rules_require_native_proof_and_confirmed_exact_key_send(seeded):
    from psycopg.types.json import Jsonb
    store, _, _ = seeded
    token = identity(store)
    sid = store.create_subscription(token, ['telegram-demo-chat'], wake_rules=['owner_mention'])['subscription_id']
    ignored = emit(store)
    with store.pool.connection() as c:
        c.execute("INSERT INTO message_metadata(message,revision,metadata) VALUES('demo-tg-1',1,%s)",
            (Jsonb({'mentions_owner': True}),))
    selected = emit(store)
    match_subscriptions(store)
    assert [e['event_id'] for e in store.get_subscription_events(token, sid)['items']] == [selected]
    assert ignored < selected
    with pytest.raises(APIError, match='invalid_subscription'):
        store.create_subscription(token, ['telegram-demo-chat'], wake_rules=['reply_to_confirmed_send'])
    with store.authorized(token) as (_, p):
        owner = p
    with store.pool.connection() as c:
        c.execute('''INSERT INTO outgoing_messages(id,principal,key_id,scope_version,key_permission_version,
            request_id,request_hash,account,chat,provider_chat,body,status,provider_message_id)
            VALUES('send_synthetic',%s,%s,%s,%s,'synthetic-anchor',%s,'telegram-demo',
            'telegram-demo-chat','provider-chat','Synthetic','unknown','native-anchor')''',
            (owner['id'],owner['key_id'],owner['scope_version'],owner['key_permission_version'],'a'*64))
    with pytest.raises(APIError, match='not_found'):
        store.create_subscription(token, ['telegram-demo-chat'], reply_to_send='send_synthetic')
    with store.pool.connection() as c:
        c.execute("UPDATE outgoing_messages SET status='sent' WHERE id='send_synthetic'")
    sid = store.create_subscription(token, ['telegram-demo-chat'], wake_rules=['reply_to_confirmed_send'],
        reply_to_send='send_synthetic')['subscription_id']
    with pytest.raises(APIError, match='not_found'):
        store.create_subscription(identity(store), ['telegram-demo-chat'], reply_to_send='send_synthetic')
    with store.pool.connection() as c:
        c.execute("UPDATE message_metadata SET metadata=%s WHERE message='demo-tg-1'", (Jsonb({'reply_to_provider_id':'other'}),))
    emit(store)
    with store.pool.connection() as c:
        c.execute("UPDATE message_metadata SET metadata=%s WHERE message='demo-tg-1'", (Jsonb({'reply_to_provider_id':'native-anchor'}),))
    selected = emit(store)
    match_subscriptions(store)
    result = store.get_subscription_events(token, sid)
    assert [e['event_id'] for e in result['items']] == [selected]
    assert 'reply_to_confirmed_send' in result['items'][0]['wake_reason']


def test_dedicated_dispatcher_role_cannot_read_archive_or_mutate_ack(seeded, tmp_path):
    from contextlib import contextmanager
    from copy import copy
    import psycopg
    from psycopg import sql
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    rid, _ = receiver(store, token, tmp_path)
    call(store, token, 'configure_subscription_delivery', sid, 1, rid)
    emit(store)
    match_subscriptions(store)
    role = 'synthetic_dispatcher_' + uuid4().hex
    ddl = Path('services/messaging/messaging_service/delivery_role.sql').read_text().replace('messaging_event_delivery', role)
    class RestrictedPool:
        @contextmanager
        def connection(self):
            with store.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                yield c
    worker = copy(store)
    worker.pool = RestrictedPool()
    try:
        with store.pool.connection() as c:
            c.execute(ddl)
        assert dispatch_hints(worker, transport=lambda *a, **k: 204,
            resolver=lambda *a, **k: ['93.184.216.34'])['delivered'] == 1
        for statement in ('SELECT body FROM messages', 'SELECT digest FROM api_keys',
                'SELECT * FROM subscription_receipts', 'UPDATE subscription_consumption SET version=version+1',
                'INSERT INTO outgoing_messages DEFAULT VALUES', 'UPDATE subscription_receivers SET enabled=true',
                'DELETE FROM subscription_matches'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with worker.pool.connection() as c:
                    c.execute(statement)
    finally:
        with store.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def test_dns_deadline_retains_bounded_lookup_slot(monkeypatch):
    import socket
    import threading
    from messaging_service import event_delivery as delivery
    entered, release = threading.Event(), threading.Event()
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(delivery, '_DNS_SLOTS', slots)
    monkeypatch.setattr(delivery, '_DNS_TIMEOUT_S', 0.02)
    def slow_dns(*args, **kwargs):
        entered.set()
        release.wait(2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, '', ('93.184.216.34', 443))]
    monkeypatch.setattr(socket, 'getaddrinfo', slow_dns)
    try:
        with pytest.raises(OSError, match='receiver_dns_unavailable'):
            delivery._addresses('synthetic.invalid', 443)
        assert entered.is_set()
        with pytest.raises(OSError, match='receiver_dns_busy'):
            delivery._addresses('synthetic.invalid', 443)
    finally:
        release.set()
        assert slots.acquire(timeout=1)
        slots.release()


def test_transient_dns_failure_retries_instead_of_dead_letter(seeded, tmp_path):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    rid, _ = receiver(store, token, tmp_path)
    call(store, token, 'configure_subscription_delivery', sid, 1, rid)
    emit(store)
    match_subscriptions(store)
    def unavailable(*args, **kwargs):
        raise OSError('synthetic DNS unavailable')
    assert dispatch_hints(store, transport=lambda *a, **k: pytest.fail('unresolved destination sent'),
        resolver=unavailable)['retried'] == 1


def test_operator_receiver_cli_registers_and_disables_without_dispatch(seeded, tmp_path, monkeypatch, capsys):
    import socket
    import sys
    from messaging_service import delivery_worker
    store, _, _ = seeded
    token = identity(store)
    key_id = store.capabilities(token)['credential_id']
    secret = tmp_path / 'synthetic.key'
    secret.write_bytes(b'synthetic receiver secret bytes 0123456789')
    secret.chmod(0o600)
    receiver_id = str(uuid4())
    config = tmp_path / 'receiver.json'
    config.write_text(json.dumps(dict(receiver_id=receiver_id, principal='event-reader', key_id=key_id,
        endpoint='https://synthetic.invalid/wake', secret_file=str(secret))))
    config.chmod(0o600)
    dsn = tmp_path / 'operator.dsn'
    dsn.write_text(store.pool.conninfo)
    dsn.chmod(0o600)
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn))
    monkeypatch.setattr(delivery_worker.signal, 'signal', lambda *a: None)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, port, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 0, '', ('93.184.216.34', 443))]
        if host == 'synthetic.invalid' else pytest.fail('Unexpected network lookup'))
    # Existing pool connectivity is retained; suppress fresh psycopg resolution
    # by reusing this isolated Store, with close owned by the fixture.
    class OperatorStore:
        pool = store.pool
        def close(self): pass
    monkeypatch.setattr(delivery_worker, 'Store', lambda *a: OperatorStore())
    monkeypatch.setattr(sys, 'argv', ['delivery_worker', '--register-receiver', str(config)])
    delivery_worker.main()
    assert json.loads(capsys.readouterr().out) == {'receiver_id': receiver_id, 'registered': True}
    monkeypatch.setattr(sys, 'argv', ['delivery_worker', '--disable-receiver', receiver_id])
    delivery_worker.main()
    assert json.loads(capsys.readouterr().out) == {'receiver_id': receiver_id, 'enabled': False}
    with store.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM subscription_outbox').fetchone()['n'] == 0


def test_feedback_rechecks_subscription_scope_before_lazy_state_refresh(seeded):
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    store.set_access('event-reader', chats=[])
    with pytest.raises(APIError, match='not_found'):
        call(store, token, 'submit_feedback', 'synthetic-stale-ref', 'bug', 'Synthetic scope change',
            refs={'subscription_ids': [sid]})


def test_immediate_wait_has_finite_first_read_deadline(monkeypatch):
    import threading
    import time
    from messaging_service import subscription_consumption as consumption
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(consumption, '_WAIT_SLOTS', slots)
    monkeypatch.setattr(consumption, '_IMMEDIATE_READ_TIMEOUT_S', 0.03)
    release = threading.Event()
    class SlowStore:
        def fetch_subscription_events(self, *a, **k):
            release.wait(2)
            return {'items': [], 'subscription': {'state': 'active'}}
    async def run():
        try:
            started = time.monotonic()
            with pytest.raises(APIError, match='subscription_wait_timeout'):
                await consumption.wait_for_subscription_events(SlowStore(), 'opaque', str(uuid4()), timeout=0)
            assert time.monotonic() - started < 0.3
            assert not slots.acquire(blocking=False)
        finally:
            release.set()
    asyncio.run(run())
    assert slots.acquire(timeout=1)
    slots.release()


def test_hint_attempt_is_reserved_before_post_and_crash_cannot_exceed_budget(seeded, tmp_path):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    rid, _ = receiver(store, token, tmp_path)
    call(store, token, 'configure_subscription_delivery', sid, 1, rid)
    emit(store)
    match_subscriptions(store)
    with store.pool.connection() as c:
        c.execute('UPDATE subscription_outbox SET attempts=7')
    posts = []
    def crash_after_post(*args, **kwargs):
        posts.append(args[1])
        raise SystemExit('synthetic process crash after POST')
    with pytest.raises(SystemExit):
        dispatch_hints(store, transport=crash_after_post, resolver=lambda *a, **k: ['93.184.216.34'])
    with store.pool.connection() as c:
        row = c.execute('SELECT attempts,state FROM subscription_outbox').fetchone()
        assert row == {'attempts': 8, 'state': 'leased'}
        c.execute("UPDATE subscription_outbox SET lease_until=clock_timestamp()-interval '1 second'")
    result = dispatch_hints(store, transport=lambda *a, **k: pytest.fail('Ninth external POST'),
        resolver=lambda *a, **k: ['93.184.216.34'])
    assert result['dead_letter'] == 1 and len(posts) == 1


@pytest.mark.parametrize('kind', ['message.status', 'message.edited', 'attachment.ready', 'file.text.ready'])
def test_receipts_edits_and_file_readiness_do_not_become_new_human_reply(kind):
    from messaging_service.subscription_consumption import wake_reason
    item = {'kind': kind, 'direction': 'incoming', 'account': 'synthetic', 'chat': 'synthetic',
        'metadata': {'mentions_owner': True, 'reply_to_owner': True, 'reply_to_provider_id': 'native'}}
    selector = {'reply_anchor': {'account': 'synthetic', 'chat': 'synthetic', 'provider_id': 'native'}}
    reasons = wake_reason(item, selector)
    assert not {'incoming_message', 'owner_mention', 'reply_to_owner', 'reply_to_confirmed_send'} & set(reasons)


def test_unacked_polls_evict_old_receipts_instead_of_locking_out(seeded):
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    emit(store)
    match_subscriptions(store)
    pages = [call(store, token, 'fetch_subscription_events', sid) for _ in range(130)]
    assert all(page['receipt'] for page in pages)
    with store.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM subscription_receipts').fetchone()['n'] == 128
    with pytest.raises(APIError, match='invalid_receipt'):
        call(store, token, 'ack_subscription_events', sid, pages[0]['receipt'], 1)
    ack = call(store, token, 'ack_subscription_events', sid, pages[-1]['receipt'], 1)
    assert ack['acked_through'] == pages[-1]['next_after']


def test_certificate_failure_retries_and_non_ascii_path_is_rejected(seeded, tmp_path):
    import ssl
    from messaging_service.event_delivery import dispatch_hints, validate_endpoint
    with pytest.raises(ValueError, match='unsafe_receiver'):
        validate_endpoint('https://hooks.example.test/wäke', resolver=lambda *a, **k: ['93.184.216.34'])
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    rid, _ = receiver(store, token, tmp_path)
    call(store, token, 'configure_subscription_delivery', sid, 1, rid)
    emit(store)
    match_subscriptions(store)
    def untrusted(*args, **kwargs):
        raise ssl.SSLCertVerificationError('synthetic certificate failure')
    assert dispatch_hints(store, transport=untrusted, resolver=lambda *a, **k: ['93.184.216.34'])['retried'] == 1


HINT_VECTOR = Path(__file__).parents[1] / 'packages/wake-relay/test/fixtures/deedkit-hint-vector.json'


# spec: ANYM-228
def test_wake_relay_hint_vector_has_the_live_dispatcher_wire_format(seeded, tmp_path):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    rid, secret = receiver(store, token, tmp_path)
    call(store, token, 'configure_subscription_delivery', sid, 1, rid)
    emit(store)
    match_subscriptions(store)
    sent = []
    def transport(endpoint, body, headers, **kwargs):
        sent.append((body, headers))
        return 202
    assert dispatch_hints(store, transport=transport, resolver=lambda *a, **k: ['93.184.216.34'])['delivered'] == 1
    body, headers = sent[0]
    if os.environ.get('DEEDKIT_WRITE_HINT_VECTOR') == '1':  # regenerate after a deliberate wire change
        HINT_VECTOR.write_text(json.dumps({'secret': secret.read_text(), 'headers': headers, 'body': body.decode()},
            indent=2, sort_keys=True) + '\n')
    vector = json.loads(HINT_VECTOR.read_text())
    # packages/wake-relay/test verifies this vector; it must keep the dispatcher's header names, body fields,
    # value types, canonical body bytes and signature rule.
    live, fixed = json.loads(body), json.loads(vector['body'])
    assert set(vector['headers']) == set(headers)
    assert {k: type(v) for k, v in fixed.items()} == {k: type(v) for k, v in live.items()}
    assert body == json.dumps(live, sort_keys=True, separators=(',', ':')).encode()
    assert vector['body'] == json.dumps(fixed, sort_keys=True, separators=(',', ':'))
    assert fixed['kind'] == live['kind'] == 'events' and fixed['host_turn_verified'] is False
    for raw, signed, key in ((body, headers, secret.read_bytes()),
            (vector['body'].encode(), vector['headers'], vector['secret'].encode())):
        assert signed['X-DeedKit-Hint-ID'] == json.loads(raw)['hint_id']
        assert signed['X-DeedKit-Signature'] == 'sha256=' + hmac.new(key,
            signed['X-DeedKit-Timestamp'].encode() + b'.' + raw, hashlib.sha256).hexdigest()


# spec: ANYM-252
def test_budget_applies_without_policy(seeded, tmp_path, monkeypatch):
    from datetime import timedelta
    from messaging_service.delivery_policy import flush_hints
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    monkeypatch.setenv('SUBSCRIPTION_MAX_HINTS_PER_HOUR', '3')
    token = identity(store)
    rid, _ = receiver(store, token, tmp_path)
    sids = [subscription(store, token) for _ in range(2)]
    for sid in sids:
        call(store, token, 'configure_subscription_delivery', sid, 1, rid)
    emit(store)
    match_subscriptions(store)
    emit(store)
    match_subscriptions(store)
    # Matcher side: the fourth hint of the key in the hour is held, not queued or dropped.
    with store.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM subscription_outbox WHERE kind='events'").fetchone()['n'] == 3
    assert {store.get_subscription(token, sid)['delivery']['held_reason'] for sid in sids} == {None, 'budget'}
    # Dispatcher side: a backlog queued during downtime is admitted at most 3 per rolling hour.
    with store.pool.connection() as c:
        c.execute('UPDATE subscription_outbox SET created_at=created_at-%s WHERE kind=%s',
            (timedelta(hours=2), 'events'))
        c.execute('UPDATE subscription_hint_windows SET due_at=due_at-%s WHERE due_at IS NOT NULL',
            (timedelta(hours=2),))
    emit(store)
    match_subscriptions(store)
    flush_hints(store)
    with store.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM subscription_outbox WHERE state='pending'").fetchone()['n'] == 5
    stats = dispatch_hints(store, transport=lambda *a, **k: 204, resolver=lambda *a, **k: ['93.184.216.34'])
    assert stats['delivered'] == 3 and stats['deferred'] == 2 and stats['dead_letter'] == 0
    with store.pool.connection() as c:
        waiting = c.execute("""SELECT attempts,next_attempt_at>clock_timestamp()+interval '50 minutes' AS later
            FROM subscription_outbox WHERE state='pending'""").fetchall()
    assert [(row['attempts'], row['later']) for row in waiting] == [(0, True), (0, True)]
    assert all(store.get_subscription_events(token, sid)['items'] for sid in sids)


# spec: ANYM-257
def test_revocation_waits_for_inflight_hint_and_no_send_after(seeded, tmp_path):
    import threading
    import time
    from psycopg.errors import LockNotAvailable
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    rid, _ = receiver(store, token, tmp_path)
    call(store, token, 'configure_subscription_delivery', sid, 1, rid)
    emit(store)
    match_subscriptions(store)
    blocked, fenced_during_dns, sends, ended, revoked, threads = 1.0, [], [], [], [], []
    def slow_dns(*args, **kwargs):
        # A lookup up to the 2 s DNS bound runs before the fence: the subscription row is free.
        try:
            with store.pool.connection() as c:
                c.execute('SELECT 1 FROM subscriptions WHERE id=%s FOR UPDATE NOWAIT', (sid,))
        except LockNotAvailable:
            fenced_during_dns.append(True)
        time.sleep(2)
        return ['93.184.216.34']
    def revoke():
        store.revoke(token)
        revoked.append(time.monotonic())
    def transport(*args, **kwargs):
        sends.append(time.monotonic())
        threads.append(threading.Thread(target=revoke))
        threads[0].start()
        time.sleep(blocked)
        ended.append(time.monotonic())
        return 204
    stats = dispatch_hints(store, transport=transport, resolver=slow_dns)
    threads[0].join(10)
    assert not fenced_during_dns
    assert stats['delivered'] == 1 and len(sends) == 1
    # The concurrent revocation committed only after the in-flight attempt ended.
    assert revoked and revoked[0] >= ended[0] > sends[0]
    hold = stats['fence_hold_ms'][0] / 1000
    assert blocked <= hold <= min(5, blocked) + 1
    with store.pool.connection() as c:
        c.execute("UPDATE subscription_outbox SET state='pending',next_attempt_at=clock_timestamp(),finished_at=NULL")
    after = dispatch_hints(store, transport=lambda *a, **k: pytest.fail('hint sent after revocation'),
        resolver=lambda *a, **k: ['93.184.216.34'])
    assert after['cancelled'] == 1 and len(sends) == 1
