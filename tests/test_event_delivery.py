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
        # The revocation is observed waiting on the fence before the attempt ends.
        with store.pool.connection() as c:
            deadline = time.monotonic() + 5
            while not c.execute("SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted").fetchone():
                assert time.monotonic() < deadline, 'revocation never waited on the fence'
                time.sleep(0.02)
        time.sleep(blocked)
        ended.append(time.monotonic())
        return 204
    try:
        stats = dispatch_hints(store, transport=transport, resolver=slow_dns)
    finally:
        for thread in threads:
            thread.join(10)
    assert not fenced_during_dns
    assert stats['delivered'] == 1 and len(sends) == 1
    # The concurrent revocation committed only after the in-flight attempt ended.
    assert revoked and revoked[0] >= ended[0] > sends[0]
    hold = stats['fence_hold_ms'][0] / 1000
    assert blocked <= hold <= min(5, ended[0] - sends[0]) + 1
    with store.pool.connection() as c:
        c.execute("UPDATE subscription_outbox SET state='pending',next_attempt_at=clock_timestamp(),finished_at=NULL")
    after = dispatch_hints(store, transport=lambda *a, **k: pytest.fail('hint sent after revocation'),
        resolver=lambda *a, **k: ['93.184.216.34'])
    assert after['cancelled'] == 1 and len(sends) == 1


# spec: ANYM-257
def test_fence_hold_excludes_waiting_for_the_principal_lock(seeded, tmp_path, monkeypatch):
    import threading
    import time
    from messaging_service import event_delivery
    from messaging_service.event_delivery import dispatch_hints
    from messaging_service.policy import lock_principal
    identity_read = event_delivery._worker_identity
    def slow_identity(*args):
        # The authorization read runs under the fence and counts toward its hold.
        time.sleep(1)
        return identity_read(*args)
    monkeypatch.setattr(event_delivery, '_worker_identity', slow_identity)
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    rid, _ = receiver(store, token, tmp_path)
    call(store, token, 'configure_subscription_delivery', sid, 1, rid)
    emit(store)
    match_subscriptions(store)
    with store.authorized(token) as (_, p):
        principal = p['id']
    held, threads = threading.Event(), []
    def exclusive():
        with store.pool.connection() as c:
            lock_principal(c, principal)
            held.set()
            time.sleep(1.5)
    def resolver(*args, **kwargs):
        # An ACL change takes the principal lock after the reservation, before the fence.
        threads.append(threading.Thread(target=exclusive))
        threads[0].start()
        assert held.wait(5)
        return ['93.184.216.34']
    started = time.monotonic()
    try:
        stats = dispatch_hints(store, transport=lambda *a, **k: 204, resolver=resolver)
    finally:
        for thread in threads:
            thread.join(10)
    assert stats['delivered'] == 1
    assert time.monotonic() - started >= 2.5
    assert 1000 <= stats['fence_hold_ms'][0] < 2000


# spec: ANYM-519
@pytest.mark.parametrize('address', ['ff0e::1', '64:ff9b::a9fe:a9fe', '64:ff9b:1::a00:1', '2002:c000:204::1',
    '2001:0:4136:e378:8000:63bf:3fff:fdd2', '::ffff:93.184.216.34', '100.64.0.1', '224.0.0.251'])
def test_address_check_refuses_nat64_6to4_teredo_mapped_multicast_and_cgnat(address):
    from messaging_service.event_delivery import validate_endpoint
    with pytest.raises(ValueError, match='unsafe_receiver'):
        validate_endpoint('https://hooks.example.test/wake', resolver=lambda *a, **k: ['93.184.216.34', address])
    assert validate_endpoint('https://hooks.example.test/wake', resolver=lambda *a, **k: ['2606:2800:220:1::1'])[1] == [
        '2606:2800:220:1::1']


def endpoint_setup(store, tmp_path, monkeypatch, *, events=1):
    import base64
    policy = tmp_path / 'delivery-policy.json'
    policy.write_text(json.dumps({'allow_hosts': ['example.test']}))
    policy.chmod(0o600)
    monkeypatch.setenv('DELIVERY_POLICY_FILE', str(policy))
    store.set_delivery_features(True)
    permissions = ['read', 'subscription-write', 'subscription-ack', 'delivery-write']
    token = store.issue_key('endpoint-reader', ['telegram-demo'], permissions=permissions, key_permissions=permissions)
    secret = 'whsec_' + base64.b64encode(b'synthetic-endpoint-secret-0123456789').decode()
    endpoint = store.register_delivery_endpoint(token, 'endpoint-request-1', 'https://hooks.example.test/hook', secret,
        resolver=lambda *a, **k: ['93.184.216.34'])
    with store.pool.connection() as c:
        # Only the hint is under test here, not the inline test.
        c.execute("UPDATE delivery_endpoint_tests SET state='cancelled' WHERE endpoint=%s", (endpoint['endpoint_id'],))
    sid = store.create_subscription(token, ['telegram-demo-chat'], label='Synthetic endpoint',
        delivery={'endpoint_id': endpoint['endpoint_id']})['subscription_id']
    with store.pool.connection() as c:
        ids = [store.append_event(c, 'telegram-demo', 'demo-tg-1', origin='live') for _ in range(events)]
    match_subscriptions(store)
    return token, endpoint, sid, ids


# spec: ANYM-519
def test_endpoint_hint_body_pins_the_reference_fields_and_the_cap(seeded, tmp_path, monkeypatch):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    try:
        token, endpoint, sid, ids = endpoint_setup(store, tmp_path, monkeypatch, events=25)
        sent = []
        assert dispatch_hints(store, transport=lambda url, body, headers, **k: sent.append((body, headers)) or 204,
            resolver=lambda *a, **k: ['93.184.216.34'])['delivered'] == 1
    finally:
        store.set_delivery_features(False)
    body, headers = sent[0]
    payload = json.loads(body)
    assert len(body) <= 4096 and set(payload) == {'type', 'hint_id', 'subscription_id', 'version', 'label', 'references',
        'references_omitted', 'next_call'}
    assert payload['type'] == 'events' and payload['label'] == 'Synthetic endpoint' and payload['version'] == 2
    # At most 20 references, dropped from the end into references_omitted until the body fits 4 KiB.
    listed = [ref['event_id'] for ref in payload['references']]
    assert 1 <= len(listed) <= 20 and listed == ids[:len(listed)] and payload['references_omitted'] == 25 - len(listed)
    assert {key for ref in payload['references'] for key in ref} == {'event_id', 'kind', 'occurred_at', 'wake_reason',
        'account_id', 'chat_id', 'message_id', 'sender_id'}
    assert payload['references'][0]['wake_reason'] == ['incoming_message']
    assert payload['next_call'] == {'tool': 'messages_fetch_subscription_events', 'subscription_id': sid}
    assert set(headers) == {'Content-Type', 'webhook-id', 'webhook-timestamp', 'webhook-signature'}
    assert not any(name.lower().startswith('x-deedkit') for name in headers) and b'host_turn_verified' not in body


# spec: ANYM-519
@pytest.mark.parametrize('change', ['operator_disable', 'delivery_write_lost', 'feature_disable', 'policy_reload'])
def test_endpoint_change_waits_for_the_inflight_post_and_no_post_after(seeded, tmp_path, monkeypatch, change):
    import threading
    import time
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    try:
        token, endpoint, sid, _ = endpoint_setup(store, tmp_path, monkeypatch)
        changed, ended, threads = [], [], []
        def apply():
            if change == 'operator_disable':
                store.set_delivery_endpoint_operator_state(endpoint['endpoint_id'], False)
            elif change == 'feature_disable':
                store.set_delivery_features(False)
            elif change == 'policy_reload':
                # The operator replaces the policy file, then reloads: the change counts as made when it returns.
                (tmp_path / 'delivery-policy.json').write_text(json.dumps({'allow_hosts': ['example.test'],
                    'deny_hosts': ['hooks.example.test']}))
                store.reload_delivery_policy()
            else:
                store.set_access('endpoint-reader', permissions=['read', 'subscription-write', 'subscription-ack'])
            changed.append(time.monotonic())
        def transport(*args, **kwargs):
            threads.append(threading.Thread(target=apply))
            threads[0].start()
            with store.pool.connection() as c:
                deadline = time.monotonic() + 5
                while not c.execute("SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted").fetchone():
                    assert time.monotonic() < deadline, 'the change never waited on the fence'
                    time.sleep(0.02)
            time.sleep(0.5)
            ended.append(time.monotonic())
            return 204
        try:
            assert dispatch_hints(store, transport=transport, resolver=lambda *a, **k: ['93.184.216.34'])['delivered'] == 1
        finally:
            for thread in threads:
                thread.join(10)
        assert changed and changed[0] >= ended[0]
        with store.pool.connection() as c:
            c.execute("""UPDATE subscription_outbox SET state='pending',next_attempt_at=clock_timestamp(),finished_at=NULL
                WHERE subscription=%s""", (sid,))
        after = dispatch_hints(store, transport=lambda *a, **k: pytest.fail('POST after the change committed'),
            resolver=lambda *a, **k: ['93.184.216.34'])
        assert after['cancelled'] == 1
    finally:
        store.set_delivery_features(False)


PUBLIC = lambda *a, **k: ['93.184.216.34']  # noqa: E731


def endpoint_keys(store, tmp_path, monkeypatch, subscriptions):
    """One key per item, each with one bound self-service endpoint and that many subscriptions (ANYM-534)."""
    import base64
    policy = tmp_path / 'delivery-policy.json'
    policy.write_text(json.dumps({'allow_hosts': ['example.test']}))
    policy.chmod(0o600)
    monkeypatch.setenv('DELIVERY_POLICY_FILE', str(policy))
    store.set_delivery_features(True)
    permissions = ['read', 'subscription-write', 'subscription-ack', 'delivery-write']
    keys = []
    for n, count in enumerate(subscriptions):
        token = store.issue_key(f'lane-agent-{n}', ['telegram-demo'], permissions=permissions, key_permissions=permissions)
        secret = 'whsec_' + base64.b64encode(b'synthetic-lane-secret-%02d-0123456789' % n).decode()
        endpoint = store.register_delivery_endpoint(token, f'lane-request-{n}', f'https://slow{n}.example.test/hook',
            secret, resolver=PUBLIC)
        sids = [store.create_subscription(token, ['telegram-demo-chat'], label=f'Lane {n}.{i}',
            delivery={'endpoint_id': endpoint['endpoint_id']})['subscription_id'] for i in range(count)]
        keys.append((token, endpoint['endpoint_id'], sids))
    with store.pool.connection() as c:
        # Only hints are under test here, not the inline registration tests.
        c.execute("UPDATE delivery_endpoint_tests SET state='cancelled' WHERE state<>'cancelled'")
    emit(store)
    match_subscriptions(store)
    return keys


# spec: ANYM-534
def test_operator_receiver_hint_is_on_time_while_four_endpoints_hang(seeded, tmp_path, monkeypatch):
    import threading
    import time
    from messaging_service import delivery_worker
    store, _, _ = seeded
    try:
        endpoint_keys(store, tmp_path, monkeypatch, [1, 1, 1, 1, 1])
        token = identity(store)
        sid = subscription(store, token)
        rid, _ = receiver(store, token, tmp_path)
        call(store, token, 'configure_subscription_delivery', sid, 1, rid)
        # The endpoint hints are older, so a serial dispatcher would POST all of them first.
        emit(store)
        match_subscriptions(store)
        due, sent, hung = time.monotonic(), {}, []
        def transport(url, body, headers, *, resolved, timeout):
            if url.startswith('https://slow0.'):
                raise RuntimeError('synthetic endpoint bug')
            if url.startswith('https://slow'):
                hung.append(url)
                time.sleep(timeout)
                raise TimeoutError
            sent[url] = time.monotonic()
            return 204
        stop = threading.Event()
        worker = threading.Thread(target=delivery_worker.run, args=(store, stop),
            kwargs={'transport': transport, 'resolver': PUBLIC, 'interval': 0.2})
        worker.start()
        try:
            while 'https://hooks.example.test/wake' not in sent and time.monotonic() < due + 15:
                time.sleep(0.05)
            assert worker.is_alive(), 'an endpoint failure stopped the dispatcher'
        finally:
            stop.set()
            worker.join(30)
        assert not worker.is_alive()
        assert sent['https://hooks.example.test/wake'] - due < 15 and hung
    finally:
        store.set_delivery_features(False)


# spec: ANYM-534
def test_endpoint_lane_keeps_one_attempt_per_key_in_flight_and_serves_keys_in_turn(seeded, tmp_path, monkeypatch):
    import threading
    import time
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    try:
        endpoint_keys(store, tmp_path, monkeypatch, [3, 1])
        served, inflight, release = {}, [], threading.Event()
        def blocking(url, body, headers, **kwargs):
            inflight.append(url)
            assert release.wait(10)
            return 204
        def lane():
            dispatch_hints(store, lane='endpoint', limit=1, served=served, transport=blocking, resolver=PUBLIC)
        threads = []
        for n in (1, 2):
            threads.append(threading.Thread(target=lane))
            threads[-1].start()
            deadline = time.monotonic() + 10
            while len(inflight) < n:
                assert time.monotonic() < deadline
                time.sleep(0.02)
        # Both keys have one attempt in flight: key 0's other two hints wait, and the operator lane sees none.
        idle = dispatch_hints(store, lane='endpoint', limit=1, served=served,
            transport=lambda *a, **k: pytest.fail('a second attempt for a key in flight'), resolver=PUBLIC)
        assert idle['delivered'] == idle['retried'] == 0
        assert dispatch_hints(store, lane='operator', transport=lambda *a, **k: pytest.fail('endpoint hint in the operator lane'),
            resolver=PUBLIC)['delivered'] == 0
        release.set()
        for thread in threads:
            thread.join(10)
        assert sorted(url.split('.')[0] for url in inflight) == ['https://slow0', 'https://slow1']
        rest = []
        for _ in range(3):
            dispatch_hints(store, lane='endpoint', limit=1, served=served,
                transport=lambda url, *a, **k: rest.append(url) or 204, resolver=PUBLIC)
        assert [url.split('.')[0] for url in rest] == ['https://slow0', 'https://slow0']
    finally:
        store.set_delivery_features(False)


# spec: ANYM-534
def test_the_least_recently_served_key_goes_first(seeded, tmp_path, monkeypatch):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    try:
        endpoint_keys(store, tmp_path, monkeypatch, [3, 2])
        served, order = {}, []
        for _ in range(5):
            dispatch_hints(store, lane='endpoint', limit=1, served=served,
                transport=lambda url, *a, **k: order.append(url.split('.')[0]) or 204, resolver=PUBLIC)
        # Key 0's three hints are as old as key 1's, yet the keys take turns.
        assert order in (['https://slow0', 'https://slow1', 'https://slow0', 'https://slow1', 'https://slow0'],
                         ['https://slow1', 'https://slow0', 'https://slow1', 'https://slow0', 'https://slow0'])
    finally:
        store.set_delivery_features(False)


# spec: ANYM-534
def test_a_key_endpoint_queue_is_bounded_and_events_stay_fetchable(seeded, tmp_path, monkeypatch):
    from messaging_service import event_delivery
    from messaging_service.delivery_policy import flush_hints
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    monkeypatch.setattr(event_delivery, 'ENDPOINT_QUEUE_PER_KEY', 2)
    try:
        [(token, _, sids)] = endpoint_keys(store, tmp_path, monkeypatch, [3])
        def queued():
            with store.pool.connection() as c:
                return c.execute("""SELECT count(*) AS n FROM subscription_outbox WHERE endpoint IS NOT NULL
                    AND state IN ('pending','leased')""").fetchone()['n']
        def held():
            with store.pool.connection() as c:
                return c.execute("""SELECT subscription FROM subscription_hint_windows WHERE held_reason='budget'
                    AND first_match_at IS NOT NULL""").fetchall()
        # The bound is key-wide, across subscriptions: the third subscription's wake is held, not dropped.
        assert queued() == 2 and len(held()) == 1
        for _ in range(3):
            emit(store)
            match_subscriptions(store)
            assert queued() <= 2
        # An explicit test of a full key is refused like an over-budget one.
        with pytest.raises(APIError, match='rate_limited'):
            call(store, token, 'test_subscription', sids[0], 'full-queue-probe')
        assert queued() == 2
        assert len(call(store, token, 'fetch_subscription_events', sids[0])['items']) == 4
        # After each drain the held windows fire on their next tick, never more than the bound at once.
        def drain_and_tick():
            dispatch_hints(store, lane='endpoint', limit=8, transport=lambda *a, **k: 204, resolver=PUBLIC)
            assert queued() == 0
            with store.pool.connection() as c:
                c.execute("UPDATE subscription_hint_windows SET due_at=clock_timestamp() WHERE due_at IS NOT NULL")
            flush_hints(store)
        drain_and_tick()
        assert queued() == 2 and len(held()) == 1
        drain_and_tick()
        assert queued() == 1 and not held()
    finally:
        store.set_delivery_features(False)


# spec: ANYM-534
def test_a_key_never_has_an_endpoint_test_and_a_hint_in_flight_together(seeded, tmp_path, monkeypatch):
    import base64
    from messaging_service.event_delivery import dispatch_hints, endpoint_tests
    store, _, _ = seeded
    try:
        [(token, _, _)] = endpoint_keys(store, tmp_path, monkeypatch, [1])
        # A second endpoint of the same key stages its registration test: a different URL, the same key.
        with store.pool.connection() as c:
            # Past the per-key registration and test rate limits (one a minute).
            c.execute("UPDATE delivery_endpoints SET created_at=created_at-interval '2 minutes' WHERE state<>'deleted'")
            c.execute("UPDATE delivery_endpoint_tests SET created_at=created_at-interval '2 minutes' WHERE state='cancelled'")
        store.register_delivery_endpoint(token, 'lane-request-second', 'https://second.example.test/hook',
            'whsec_' + base64.b64encode(b'synthetic-second-secret-0123456789').decode(), resolver=PUBLIC)
        def tests(state):
            with store.pool.connection() as c:
                return c.execute('SELECT count(*) AS n FROM delivery_endpoint_tests WHERE state=%s', (state,)).fetchone()['n']
        assert tests('pending') == 1
        seen = []
        def hint_transport(url, *a, **k):
            # While the key's hint is in flight, the test pass leaves the key's test pending.
            seen.append(('hint', url))
            assert endpoint_tests(store, transport=lambda *a, **k: pytest.fail('test POST beside the hint'),
                resolver=PUBLIC)['delivered'] == 0
            return 204
        assert dispatch_hints(store, lane='endpoint', limit=1, served={}, transport=hint_transport,
            resolver=PUBLIC)['delivered'] == 1
        assert tests('pending') == 1
        emit(store)
        match_subscriptions(store)
        def test_transport(url, *a, **k):
            # And while the key's test is in flight, the endpoint lane leaves the key's hint pending.
            seen.append(('test', url))
            assert dispatch_hints(store, lane='endpoint', limit=1, served={},
                transport=lambda *a, **k: pytest.fail('hint POST beside the test'), resolver=PUBLIC)['delivered'] == 0
            return 204
        assert endpoint_tests(store, transport=test_transport, resolver=PUBLIC)['delivered'] == 1
        assert [kind for kind, _ in seen] == ['hint', 'test'] and seen[1][1] == 'https://second.example.test/hook'
        # An expired test lease is not in flight: the hint goes.
        with store.pool.connection() as c:
            c.execute("""INSERT INTO delivery_endpoint_tests(endpoint,request_id,state,lease_token,lease_until)
                SELECT endpoint,'expired-lease','leased',gen_random_uuid(),clock_timestamp()-interval '1 second'
                FROM delivery_endpoint_tests LIMIT 1""")
        assert dispatch_hints(store, lane='endpoint', limit=1, served={}, transport=lambda *a, **k: 204,
            resolver=PUBLIC)['delivered'] == 1
    finally:
        store.set_delivery_features(False)


# spec: ANYM-534
def test_endpoint_tests_take_the_lanes_least_recently_served_key_order(seeded, tmp_path, monkeypatch):
    from messaging_service.event_delivery import dispatch_hints, endpoint_tests
    store, _, _ = seeded
    try:
        [(_, first, _), (_, second, _)] = endpoint_keys(store, tmp_path, monkeypatch, [1, 0])
        served, order = {}, []
        record = lambda url, *a, **k: order.append(url.split('.')[0]) or 204  # noqa: E731
        assert dispatch_hints(store, lane='endpoint', limit=1, served=served, transport=record, resolver=PUBLIC)['delivered'] == 1
        with store.pool.connection() as c:
            # Key 0's two tests are older than key 1's.
            for endpoint, request, age in ((first, 'fairness-a1', 30), (first, 'fairness-a2', 20), (second, 'fairness-b1', 10)):
                c.execute("""INSERT INTO delivery_endpoint_tests(endpoint,request_id,created_at)
                    VALUES(%s,%s,clock_timestamp()-make_interval(secs=>%s))""", (endpoint, request, age))
        # Key 0 was just served by its hint: key 1's newer test goes before key 0's older ones.
        assert endpoint_tests(store, transport=record, resolver=PUBLIC, limit=3, served=served)['delivered'] == 3
        assert order == ['https://slow0', 'https://slow1', 'https://slow0', 'https://slow0']
    finally:
        store.set_delivery_features(False)


# spec: ANYM-534
def test_hints_and_endpoint_tests_share_one_key_order(seeded, tmp_path, monkeypatch):
    from messaging_service.event_delivery import dispatch_hints, endpoint_tests
    store, _, _ = seeded
    try:
        [(_, first, _), (_, second, _)] = endpoint_keys(store, tmp_path, monkeypatch, [1, 1])
        with store.pool.connection() as c:
            keys = [tuple(c.execute('SELECT principal,key_id FROM delivery_endpoints WHERE id=%s', (endpoint,)).fetchone().values())
                for endpoint in (first, second)]
            c.execute("""INSERT INTO delivery_endpoint_tests(endpoint,request_id,created_at)
                VALUES(%s,'shared-order-a1',clock_timestamp()-interval '1 minute')""", (first,))
        order = []
        record = lambda url, *a, **k: order.append(url.split('.')[0]) or 204  # noqa: E731
        def hint(served):
            return dispatch_hints(store, lane='endpoint', limit=1, served=served, transport=record, resolver=PUBLIC)['delivered']
        def test(served):
            return endpoint_tests(store, transport=record, resolver=PUBLIC, limit=1, served=served)['delivered']
        # Key 0 was just served; key 1 waits with a hint only: key 0's test does not overtake it.
        served = {keys[0]: 1}
        assert test(served) == 0 and hint(served) == 1 and order == ['https://slow1']
        assert test(served) == 1 and order[-1] == 'https://slow0'
        # Now key 1 waits with a test only, key 0 with a hint: key 0's hint does not overtake it.
        with store.pool.connection() as c:
            c.execute("""INSERT INTO delivery_endpoint_tests(endpoint,request_id,created_at)
                VALUES(%s,'shared-order-b1',clock_timestamp()-interval '1 minute')""", (second,))
        served = {keys[0]: 1, keys[1]: 0}
        assert hint(served) == 0 and test(served) == 1 and order[-1] == 'https://slow1'
        assert hint(served) == 1 and order[-1] == 'https://slow0'
    finally:
        store.set_delivery_features(False)


# spec: ANYM-534
def test_dispatcher_heartbeat_reports_version_and_stops_when_a_lane_stalls(seeded):
    import time
    from messaging_service import __version__, delivery_worker
    from messaging_service.store import RUNTIME_SCHEMA
    store, _, _ = seeded
    assert delivery_worker.HEARTBEAT_S == 30
    def rows():
        with store.pool.connection() as c:
            return (c.execute('SELECT * FROM subscription_dispatcher_heartbeat').fetchall(),
                c.execute("SELECT * FROM component_heartbeats WHERE component='dispatcher'").fetchall())
    with store.pool.connection() as c:
        c.execute('DELETE FROM subscription_dispatcher_heartbeat WHERE id=1')
        c.execute("DELETE FROM component_heartbeats WHERE component IS NOT NULL")
    now = time.monotonic()
    assert delivery_worker.pulse(store, {'operator': now - 5, 'endpoint-0': now - delivery_worker.LANE_STALL_S - 1},
        now=now) is False
    assert rows() == ([], [])
    assert delivery_worker.pulse(store, {'operator': now - 5, 'endpoint-0': now}, now=now) is True
    legacy, [component] = rows()
    assert len(legacy) == 1
    assert (component['version'], component['schema_version']) == (__version__, RUNTIME_SCHEMA)


# spec: ANYM-534
def test_each_role_writes_only_its_own_component_heartbeat(seeded):
    from contextlib import contextmanager
    from copy import copy
    import psycopg
    from psycopg import sql
    from messaging_service.heartbeats import record
    store, _, _ = seeded
    files = {'dispatcher': ('delivery_role.sql', 'messaging_event_delivery'),
             'matcher': ('subscription_role.sql', 'messaging_subscriptions'),
             'api': ('gateway_role.sql', 'messaging_gateway')}
    roles = []
    try:
        for component, (filename, name) in files.items():
            role = 'synthetic_' + component + '_' + uuid4().hex
            roles.append(role)
            ddl = Path('services/messaging/messaging_service', filename).read_text().replace(name, role)
            with store.pool.connection() as c:
                c.execute(ddl)
            class RestrictedPool:
                @contextmanager
                def connection(self, role=role):
                    with store.pool.connection() as c:
                        c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                        yield c
            worker = copy(store)
            worker.pool = RestrictedPool()
            assert record(worker, component) is True
            for other in set(files) - {component}:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with worker.pool.connection() as c:
                        c.execute('''INSERT INTO component_heartbeats(component,version,schema_version,beat_at)
                            VALUES(%s,'0.0.0',1,clock_timestamp()) ON CONFLICT(component) DO UPDATE
                            SET beat_at=EXCLUDED.beat_at''', (other,))
    finally:
        with store.pool.connection() as c:
            for role in roles:
                c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
