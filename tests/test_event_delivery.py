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


def second_chat(store, account='telegram-demo'):
    """A second synthetic chat with one incoming message (demo-tg-x)."""
    with store.pool.connection() as c:
        c.execute("INSERT INTO chats(id,account,title) VALUES('telegram-demo-chat-2',%s,'Synthetic chat')", (account,))
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at) VALUES('demo-tg-x',%s,
            'telegram-demo-chat-2','demo-tg-x','demo-person-9','Synthetic text',clock_timestamp())''', (account,))


def emit_in(store, message):
    with store.pool.connection() as c:
        return store.append_event(c, 'telegram-demo', message, origin='live')


def pending_hints(store, sid):
    with store.pool.connection() as c:
        return c.execute("""SELECT count(*) AS n FROM subscription_outbox WHERE subscription=%s
            AND state='pending'""", (sid,)).fetchone()['n']


# spec: ANYM-522
def test_dynamic_hint_staged_before_a_scope_removal_or_revocation_is_never_posted(seeded, tmp_path):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token = identity(store)
        second_chat(store)
        sid = store.create_subscription(token, sources={'all_visible_chats': True})['subscription_id']
        rid, _ = receiver(store, token, tmp_path)
        call(store, token, 'configure_subscription_delivery', sid, 1, rid)
        emit_in(store, 'demo-tg-x')
        match_subscriptions(store)
        assert pending_hints(store, sid) == 1
        store.set_access('event-reader', chats=['telegram-demo-chat'])
        posts = []
        def transport(endpoint, body, headers, **kwargs):
            posts.append(body)
            return 204
        resolver = lambda *a, **k: ['93.184.216.34']  # noqa: E731
        # The scope change cancels the queued hint under the exclusive principal lock; nothing is posted.
        assert pending_hints(store, sid) == 0
        assert dispatch_hints(store, transport=transport, resolver=resolver)['delivered'] == 0 and posts == []
        visible = emit(store)
        match_subscriptions(store)
        assert dispatch_hints(store, transport=transport, resolver=resolver)['delivered'] == 1
        assert json.loads(posts[0])['through'] == visible
        emit(store)
        match_subscriptions(store)
        assert pending_hints(store, sid) == 1
        store.revoke(token)
        assert dispatch_hints(store, transport=transport, resolver=resolver)['delivered'] == 0 and len(posts) == 1
    finally:
        store.set_dynamic_subscriptions(False)


# spec: ANYM-522
def test_a_held_hint_range_is_rebuilt_from_references_visible_after_a_scope_removal(seeded, tmp_path):
    from psycopg.types.json import Jsonb
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token = identity(store)
        second_chat(store)
        with store.pool.connection() as c:
            c.execute("INSERT INTO message_metadata(message,revision,metadata) VALUES('demo-tg-x',1,%s)",
                (Jsonb({'mentions_owner': True}),))
        sid = store.create_subscription(token, sources={'all_visible_chats': True})['subscription_id']
        rid, _ = receiver(store, token, tmp_path)
        call(store, token, 'configure_subscription_delivery', sid, 1, rid, {'batch_count': 2})
        emit_in(store, 'demo-tg-x')
        match_subscriptions(store)
        assert pending_hints(store, sid) == 0
        store.set_access('event-reader', chats=['telegram-demo-chat'])
        emit(store)
        match_subscriptions(store)
        # One visible match: the hidden owner mention gives the range no count, reason or match time.
        assert pending_hints(store, sid) == 0
        assert store.get_subscription(token, sid)['delivery']['held_reason'] == 'batch'
        with store.pool.connection() as c:
            window = c.execute('SELECT match_count,reasons FROM subscription_hint_windows WHERE subscription=%s',
                (sid,)).fetchone()
        assert window['match_count'] == 1 and 'owner_mention' not in window['reasons']
        emit(store)
        match_subscriptions(store)
        assert pending_hints(store, sid) == 1
    finally:
        store.set_dynamic_subscriptions(False)


# spec: ANYM-522
def test_a_queued_hint_of_hidden_references_is_cancelled_and_not_counted(seeded, tmp_path):
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token = identity(store)
        second_chat(store)
        sid = store.create_subscription(token, sources={'all_visible_chats': True})['subscription_id']
        rid, _ = receiver(store, token, tmp_path)
        call(store, token, 'configure_subscription_delivery', sid, 1, rid)
        emit_in(store, 'demo-tg-x')
        match_subscriptions(store)
        assert store.get_subscription(token, sid)['delivery']['hint_counts'] == {'pending': 1}
        store.set_access('event-reader', chats=['telegram-demo-chat'])
        assert pending_hints(store, sid) == 0
        assert store.get_subscription(token, sid)['delivery']['hint_counts'] == {}
    finally:
        store.set_dynamic_subscriptions(False)


# spec: ANYM-522
def test_a_queued_mixed_hint_keeps_its_position_and_is_delivered_after_a_scope_removal(seeded, tmp_path):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token = identity(store)
        second_chat(store)
        sid = store.create_subscription(token, sources={'all_visible_chats': True})['subscription_id']
        rid, _ = receiver(store, token, tmp_path)
        call(store, token, 'configure_subscription_delivery', sid, 1, rid)
        emit(store)
        through = emit_in(store, 'demo-tg-x')
        match_subscriptions(store)
        store.set_access('event-reader', chats=['telegram-demo-chat'])
        # The hint over a visible and a now hidden reference stays queued: same id, same position, no re-emission.
        with store.pool.connection() as c:
            assert [hint['through_position'] for hint in c.execute("""SELECT through_position FROM subscription_outbox
                WHERE subscription=%s AND state='pending'""", (sid,)).fetchall()] == [through]
        assert store.get_subscription(token, sid)['delivery']['hint_counts'] == {'pending': 1}
        match_subscriptions(store)
        posts = []
        def transport(endpoint, body, headers, **kwargs):
            posts.append(body)
            return 204
        assert dispatch_hints(store, transport=transport, resolver=lambda *a, **k: ['93.184.216.34'])['delivered'] == 1
        assert [json.loads(body)['through'] for body in posts] == [through]
    finally:
        store.set_dynamic_subscriptions(False)


# spec: ANYM-522
def test_a_dynamic_definition_refuses_a_quiet_hours_bypass(seeded, tmp_path):
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token = identity(store)
        sid = store.create_subscription(token, sources={'all_visible_chats': True})['subscription_id']
        rid, _ = receiver(store, token, tmp_path)
        quiet = {'timezone': 'UTC', 'windows': [{'days': ['mon'], 'start': '22:00', 'end': '07:00'}]}
        with pytest.raises(APIError, match='invalid_delivery_policy'):
            call(store, token, 'configure_subscription_delivery', sid, 1, rid,
                {'quiet_hours': quiet, 'quiet_bypass': ['owner_mention']})
        # Quiet hours alone hold its hints as for any definition.
        call(store, token, 'configure_subscription_delivery', sid, 1, rid, {'quiet_hours': quiet})
    finally:
        store.set_dynamic_subscriptions(False)


# spec: ANYM-522
def test_hint_counts_of_a_dynamic_definition_leave_out_finished_hints_of_hidden_references(seeded, tmp_path):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token = identity(store)
        second_chat(store)
        sid = store.create_subscription(token, sources={'all_visible_chats': True})['subscription_id']
        rid, _ = receiver(store, token, tmp_path)
        call(store, token, 'configure_subscription_delivery', sid, 1, rid)
        resolver = lambda *a, **k: ['93.184.216.34']  # noqa: E731
        emit_in(store, 'demo-tg-x')
        match_subscriptions(store)
        assert dispatch_hints(store, transport=lambda *a, **k: 204, resolver=resolver)['delivered'] == 1
        emit(store)
        match_subscriptions(store)
        assert dispatch_hints(store, transport=lambda *a, **k: 204, resolver=resolver)['delivered'] == 1
        assert store.get_subscription(token, sid)['delivery']['hint_counts'] == {'delivered': 2}
        store.set_access('event-reader', chats=['telegram-demo-chat'])
        assert store.get_subscription(token, sid)['delivery']['hint_counts'] == {'delivered': 1}
    finally:
        store.set_dynamic_subscriptions(False)


# spec: ANYM-522
def test_scope_removal_waits_for_an_inflight_fetch_and_the_cursor_passes_hidden_references(seeded):
    import threading
    from messaging_service.subscription_consumption import fetch_subscription_events
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token = identity(store)
        second_chat(store)
        sid = store.create_subscription(token, sources={'all_visible_chats': True})['subscription_id']
        kept, gone = emit(store), emit_in(store, 'demo-tg-x')
        match_subscriptions(store)
        with store.authorized(token, 'fetch_subscription_events') as (c, p):
            page = fetch_subscription_events(store, c, p, sid)
            change = threading.Thread(target=store.set_access, args=('event-reader',),
                kwargs={'chats': ['telegram-demo-chat']})
            change.start()
            change.join(0.5)
            # The removal waits for the read in flight, which saw the old scope.
            assert change.is_alive()
        change.join(10)
        assert not change.is_alive()
        assert [item['event_id'] for item in page['items']] == [kept, gone]
        with pytest.raises(APIError, match='stale_ack'):
            call(store, token, 'ack_subscription_events', sid, page['receipt'], page['ack_version'])
        after = call(store, token, 'fetch_subscription_events', sid)
        assert [item['event_id'] for item in after['items']] == [kept] and after['next_after'] == gone
        assert call(store, token, 'ack_subscription_events', sid, after['receipt'], after['ack_version'])['acked_through'] == gone
        following = call(store, token, 'fetch_subscription_events', sid)
        assert following['items'] == [] and following['receipt'] is None
    finally:
        store.set_dynamic_subscriptions(False)


# spec: ANYM-522
def test_disabling_dynamic_sources_masks_them_for_todays_readers_and_cancels_their_hints(seeded, tmp_path):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token = identity(store)
        sid = store.create_subscription(token, sources={'all_visible_chats': True}, label='Synthetic dynamic')['subscription_id']
        explicit = subscription(store, token)
        rid, _ = receiver(store, token, tmp_path)
        call(store, token, 'configure_subscription_delivery', sid, 1, rid)
        emit(store)
        match_subscriptions(store)
        assert pending_hints(store, sid) == 1
    finally:
        result = store.set_dynamic_subscriptions(False)
    assert result['dynamic_subscriptions'] == 'disabled' and result['access_changed'] == [sid]
    assert len(result['cancelled_hints']) == 1 and pending_hints(store, sid) == 0
    view = store.get_subscription(token, sid)
    assert set(view) == {'subscription_id', 'state', 'version', 'recreate_required', 'scope_masked'}
    assert view['state'] == 'access_changed' and 'Synthetic dynamic' not in str(store.list_subscriptions(token))
    assert [item['state'] for item in store.list_subscriptions(token)['items'] if item['subscription_id'] == sid] == [
        'access_changed']
    page = store.get_subscription_events(token, sid)
    assert page['items'] == [] and page['scope_masked']
    with pytest.raises(APIError, match='subscription_access_changed'):
        store.update_subscription(token, sid, view['version'], action='resume')
    with pytest.raises(APIError, match='subscription_access_changed'):
        call(store, token, 'fetch_subscription_events', sid)
    with pytest.raises(APIError, match='dynamic_sources_disabled'):
        store.create_subscription(token, sources={'all_visible_chats': True})
    # Explicit definitions are untouched.
    assert store.get_subscription(token, explicit)['state'] == 'active'
    posts = []
    dispatch_hints(store, transport=lambda *a, **k: posts.append(a) or 204, resolver=lambda *a, **k: ['93.184.216.34'])
    assert posts == []


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


# spec: ANYM-522
def test_endpoint_hint_of_a_dynamic_definition_lists_only_references_visible_now(seeded, tmp_path, monkeypatch):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token, endpoint, _, _ = endpoint_setup(store, tmp_path, monkeypatch, events=0)
        second_chat(store)
        sid = store.create_subscription(token, sources={'all_visible_chats': True},
            delivery={'endpoint_id': endpoint['endpoint_id']})['subscription_id']
        kept, hidden = emit(store), emit_in(store, 'demo-tg-x')
        match_subscriptions(store)
        # Removed and added again: the earlier match stays hidden although its chat is visible.
        store.set_access('endpoint-reader', chats=['telegram-demo-chat'])
        store.set_access('endpoint-reader', chats=None)
        sent = []
        assert dispatch_hints(store, transport=lambda url, body, headers, **k: sent.append(body) or 204,
            resolver=lambda *a, **k: ['93.184.216.34'])['delivered'] == 1
    finally:
        store.set_delivery_features(False)
        store.set_dynamic_subscriptions(False)
    payload = json.loads(sent[0])
    assert payload['subscription_id'] == sid
    assert [ref['event_id'] for ref in payload['references']] == [kept] and payload['references_omitted'] == 0
    assert hidden not in [ref['event_id'] for ref in payload['references']]


# spec: ANYM-535
def test_failure_kind_hints_carry_only_reference_fields(seeded, tmp_path, monkeypatch):
    from messaging_service.archive import Archive
    from messaging_service.collector_state import CollectorState
    from messaging_service.event_delivery import dispatch_hints
    from messaging_service.subscriptions import journal_send_failures, journal_source_health
    store, _, _ = seeded
    try:
        token, endpoint, _, _ = endpoint_setup(store, tmp_path, monkeypatch, events=0)
        sid = store.create_subscription(token, ['telegram-demo-chat'], kinds=['send.failed', 'source.health'],
            label='Synthetic failures', delivery={'endpoint_id': endpoint['endpoint_id']})['subscription_id']
        archive = Archive(store)
        epoch = archive.acquire('telegram-demo', 'synthetic-health')
        state = CollectorState(archive, 'telegram-demo', ('synthetic-health', epoch))
        state.phase('connection', 'connected', 'provider_ready')
        state.capture_progress(None, 0, 0)
        assert journal_source_health(store) == 0
        with store.pool.connection() as c:
            owner = c.execute("SELECT id FROM api_keys WHERE principal='endpoint-reader'").fetchone()['id']
            journal_send_failures(c, [{'id': 'synthetic-send-1', 'account': 'telegram-demo', 'chat': 'telegram-demo-chat',
                'principal': 'endpoint-reader', 'key_id': owner, 'status': 'failed', 'error_code': 'send_not_started'}])
        state.connection_failed('provider_disconnected')
        assert journal_source_health(store) == 1
        archive.release('telegram-demo', 'synthetic-health', epoch)
        match_subscriptions(store)
        sent = []
        assert dispatch_hints(store, transport=lambda url, body, headers, **k: sent.append(body) or 204,
            resolver=lambda *a, **k: ['93.184.216.34'])['delivered'] == 1
    finally:
        store.set_delivery_features(False)
    payload = json.loads(sent[0])
    assert payload['subscription_id'] == sid and payload['references_omitted'] == 0
    common = {'event_id', 'kind', 'occurred_at', 'wake_reason', 'account_id', 'chat_id', 'message_id', 'sender_id'}
    failed, health = payload['references']
    assert set(failed) == common | {'send_id', 'status'} and set(health) == common | {'status'}
    assert (failed['kind'], failed['account_id'], failed['chat_id'], failed['send_id'], failed['status']) == (
        'send.failed', 'telegram-demo', 'telegram-demo-chat', 'synthetic-send-1', 'failed')
    assert failed['message_id'] is None and failed['sender_id'] is None
    assert (health['kind'], health['account_id'], health['chat_id'], health['status']) == (
        'source.health', 'telegram-demo', None, 'disconnected')
    # The error code, the sending key and the collector reason stay out of the push body.
    assert not any(value in sent[0] for value in (b'send_not_started', str(owner).encode(), b'provider_disconnected'))


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


def delivery_role_worker(store):
    """(a copy of store whose connections run as a fresh role made from delivery_role.sql, a cleanup)."""
    from contextlib import contextmanager
    from copy import copy
    from psycopg import sql
    role = 'synthetic_dispatcher_' + uuid4().hex
    ddl = Path('services/messaging/messaging_service/delivery_role.sql').read_text().replace('messaging_event_delivery', role)
    class RestrictedPool:
        @contextmanager
        def connection(self):
            with store.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                yield c
    with store.pool.connection() as c:
        c.execute(ddl)
    worker = copy(store)
    worker.pool = RestrictedPool()
    def cleanup():
        with store.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
    return worker, cleanup


def endpoint_state(store, endpoint_id):
    with store.pool.connection() as c:
        return c.execute('SELECT state,version FROM delivery_endpoints WHERE id=%s', (endpoint_id,)).fetchone()


# spec: ANYM-520
def test_three_dead_letters_auto_pause_the_endpoint_as_the_deployed_dispatcher_role(seeded, tmp_path, monkeypatch):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    [(token, eid, sids)] = endpoint_keys(store, tmp_path, monkeypatch, [4])
    worker, cleanup = delivery_role_worker(store)
    try:
        before = endpoint_state(store, eid)
        # A redirect is final at once: three hints dead-letter in one pass, the fourth is still queued.
        stats = dispatch_hints(worker, limit=3, transport=lambda *a, **k: 301, resolver=PUBLIC)
        assert (stats['dead_letter'], stats['auto_paused']) == (3, 1)
        assert endpoint_state(store, eid) == {'state': 'auto_paused', 'version': before['version'] + 1}
        with store.pool.connection() as c:
            hints = c.execute("SELECT state,error_code FROM subscription_outbox ORDER BY finished_at").fetchall()
            acked = c.execute('SELECT count(*) AS n FROM subscription_consumption').fetchone()['n']
        assert hints[-1] == {'state': 'cancelled', 'error_code': 'endpoint_auto_paused'} and acked == 0
        attempts = store.list_delivery_attempts(token, eid)['items']
        assert [(a['outcome'], a['http_status'], a['error_code']) for a in attempts] == [
            ('paused', None, 'endpoint_auto_paused'), *[('dead_letter', 301, 'redirect')] * 3]
        health = store.get_subscription(token, sids[3])['delivery']['delivery_health']
        assert (health['state'], health['consecutive_failures']) == ('auto_paused', 3)
        assert 'action="resume"' in health['next_step']
        # Events stay fetchable and nothing more is sent.
        assert len(call(store, token, 'fetch_subscription_events', sids[3])['items']) == 1
        assert dispatch_hints(worker, transport=lambda *a, **k: pytest.fail('sent while auto_paused'),
            resolver=PUBLIC)['delivered'] == 0
    finally:
        cleanup()
        store.set_delivery_features(False)


# spec: ANYM-520
def test_a_day_of_failures_without_a_success_auto_pauses_the_endpoint_as_the_deployed_role(seeded, tmp_path, monkeypatch):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    token, endpoint, sid, _ = endpoint_setup(store, tmp_path, monkeypatch)
    eid = endpoint['endpoint_id']
    worker, cleanup = delivery_role_worker(store)
    def failing_pass():
        with store.pool.connection() as c:
            c.execute("UPDATE subscription_outbox SET next_attempt_at=clock_timestamp() WHERE state='pending'")
        return dispatch_hints(worker, transport=lambda *a, **k: 500, resolver=PUBLIC)
    try:
        assert failing_pass()['retried'] == 1 and endpoint_state(store, eid)['state'] == 'enabled'
        # One failure under 24 h never pauses; once the first failure since the last success is a day old, it does.
        with store.pool.connection() as c:
            c.execute("UPDATE delivery_attempts SET created_at=created_at-interval '23 hours'")
        assert failing_pass().get('auto_paused') is None and endpoint_state(store, eid)['state'] == 'enabled'
        with store.pool.connection() as c:
            c.execute("UPDATE delivery_attempts SET created_at=created_at-interval '1 hour 1 minute'")
        assert failing_pass()['auto_paused'] == 1 and endpoint_state(store, eid)['state'] == 'auto_paused'
        with store.pool.connection() as c:
            assert c.execute("SELECT state,error_code FROM subscription_outbox").fetchone() == {
                'state': 'cancelled', 'error_code': 'endpoint_auto_paused'}
        assert len(call(store, token, 'fetch_subscription_events', sid)['items']) == 1
    finally:
        cleanup()
        store.set_delivery_features(False)


# spec: ANYM-520
def test_resume_sends_a_signed_test_inline_rearms_once_it_is_delivered_and_wakes_the_backlog(seeded, tmp_path,
        monkeypatch):
    from messaging_service.delivery_endpoints import finish_resume
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    [(token, eid, sids)] = endpoint_keys(store, tmp_path, monkeypatch, [2])
    received = []
    def receiver(status):
        def send(url, body, headers, **kwargs):
            received.append(json.loads(body))
            return status
        return send
    def tested():
        with store.pool.connection() as c:
            c.execute("UPDATE delivery_endpoint_tests SET created_at=created_at-interval '2 minutes'")
    try:
        with store.pool.connection() as c:
            c.execute("UPDATE delivery_endpoints SET state='auto_paused' WHERE id=%s", (eid,))
            c.execute("UPDATE subscription_outbox SET state='cancelled',error_code='endpoint_auto_paused'")
        version = endpoint_state(store, eid)['version']
        arguments = {'endpoint_id': eid, 'version': version, 'action': 'resume'}
        # A failed test refuses the resume with the test's error and leaves the endpoint paused.
        tested()
        pending = store.update_delivery_endpoint(token, eid, version, 'resume')
        assert (pending['state'], pending['test']['test_status'], pending['woken_subscriptions']) == (
            'auto_paused', 'pending', [])
        assert pending['test']['next_step'] == {'tool': 'update_delivery_endpoint', **arguments}
        dispatch_hints(store, transport=receiver(500), resolver=PUBLIC)
        assert received == [{'type': 'test', 'test_id': pending['test']['test_id'], 'endpoint_id': eid}]
        with pytest.raises(APIError, match='endpoint_test_failed') as refused:
            asyncio.run(finish_resume(store, token, arguments, pending, wait_s=1))
        # The settled test moved the paused endpoint one version on: the refusal names it.
        assert (refused.value.test_error, refused.value.http_status, refused.value.state, refused.value.version) == (
            'receiver_status', 500, 'auto_paused', version + 1)
        assert endpoint_state(store, eid) == {'state': 'auto_paused', 'version': version + 1}
        # A delivered test re-arms it and stages one events hint per subscription with unACKed events, at once.
        tested()
        version += 1
        arguments['version'] = version
        pending = store.update_delivery_endpoint(token, eid, version, 'resume')
        dispatch_hints(store, transport=receiver(204), resolver=PUBLIC)
        resumed = asyncio.run(finish_resume(store, token, arguments, pending, wait_s=1))
        assert (resumed['state'], resumed['version'], resumed['test']['test_status']) == ('enabled', version + 1,
            'delivered')
        assert sorted(resumed['woken_subscriptions']) == sorted(sids)
        assert resumed['delivery_health']['state'] == 'ok'
        with store.pool.connection() as c:
            woken = c.execute("""SELECT subscription,request_id,next_attempt_at<=clock_timestamp() AS due
                FROM subscription_outbox WHERE state='pending'""").fetchall()
        assert sorted(str(row['subscription']) for row in woken) == sorted(sids)
        assert all(row['request_id'] == f'resume:{eid}:{version + 1}' and row['due'] for row in woken)
        received.clear()
        assert dispatch_hints(store, transport=receiver(204), resolver=PUBLIC)['delivered'] == 2
        assert {body['type'] for body in received} == {'events'}
        # Only a subscription whose events are not all ACKed is woken again.
        page = call(store, token, 'fetch_subscription_events', sids[0])
        call(store, token, 'ack_subscription_events', sids[0], page['receipt'], page['ack_version'])
        paused = store.update_delivery_endpoint(token, eid, version + 1, 'pause')
        tested()
        pending = store.update_delivery_endpoint(token, eid, paused['version'], 'resume')
        dispatch_hints(store, transport=receiver(204), resolver=PUBLIC)
        resumed = asyncio.run(finish_resume(store, token, {**arguments, 'version': paused['version']}, pending, wait_s=1))
        assert resumed['woken_subscriptions'] == [sids[1]]
        # Resume never clears operator_disabled.
        store.set_delivery_endpoint_operator_state(eid, False)
        with pytest.raises(APIError, match='operator_disabled'):
            store.update_delivery_endpoint(token, eid, endpoint_state(store, eid)['version'], 'resume')
    finally:
        store.set_delivery_features(False)


def paused_endpoint(store, eid):
    """The endpoint auto_paused with its queued hints cancelled, and the tests past the per-key test rate."""
    with store.pool.connection() as c:
        c.execute("UPDATE delivery_endpoints SET state='auto_paused' WHERE id=%s", (eid,))
        c.execute("UPDATE subscription_outbox SET state='cancelled',error_code='endpoint_auto_paused' WHERE endpoint=%s",
            (eid,))
    aged_tests(store)
    return endpoint_state(store, eid)['version']


def aged_tests(store):
    with store.pool.connection() as c:
        c.execute("UPDATE delivery_endpoint_tests SET created_at=created_at-interval '2 minutes'")


def resume_tests(store, eid):
    with store.pool.connection() as c:
        return [row['id'] for row in c.execute("""SELECT id FROM delivery_endpoint_tests WHERE endpoint=%s
            AND starts_with(request_id,'resume/') ORDER BY created_at""", (eid,)).fetchall()]


# spec: ANYM-520
def test_a_repeated_resume_after_its_test_failed_replays_the_refusal_and_sends_no_second_test(seeded, tmp_path,
        monkeypatch):
    from messaging_service.delivery_endpoints import finish_resume
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    [(token, eid, _)] = endpoint_keys(store, tmp_path, monkeypatch, [1])
    sent = []
    def timing_out(url, body, headers, **kwargs):
        # The receiver may have taken it: the answer never came back.
        sent.append(json.loads(body))
        raise TimeoutError('synthetic receiver deadline')
    try:
        version = paused_endpoint(store, eid)
        arguments = {'endpoint_id': eid, 'version': version, 'action': 'resume'}
        pending = store.update_delivery_endpoint(token, eid, version, 'resume')
        # Repeated while pending, the same test is read back.
        assert store.update_delivery_endpoint(token, eid, version, 'resume')['test']['test_id'] == pending['test']['test_id']
        dispatch_hints(store, transport=timing_out, resolver=PUBLIC)
        with pytest.raises(APIError, match='endpoint_test_failed') as refused:
            asyncio.run(finish_resume(store, token, arguments, pending, wait_s=1))
        assert (refused.value.test_error, refused.value.version) == ('receiver_unavailable', version + 1)
        # The client's retry of the same call, also past the test rate, refuses the same way and sends nothing.
        for _ in range(2):
            aged_tests(store)
            with pytest.raises(APIError, match='endpoint_test_failed') as again:
                store.update_delivery_endpoint(token, eid, version, 'resume')
            assert (again.value.test_error, again.value.version, again.value.state) == (
                'receiver_unavailable', version + 1, 'auto_paused')
        dispatch_hints(store, transport=timing_out, resolver=PUBLIC)
        assert len(sent) == 1 and len(resume_tests(store, eid)) == 1
        # Only a resume that names the new version sends another test.
        fresh = store.update_delivery_endpoint(token, eid, version + 1, 'resume')
        assert fresh['test']['test_status'] == 'pending' and len(resume_tests(store, eid)) == 2
        assert endpoint_state(store, eid) == {'state': 'auto_paused', 'version': version + 1}
    finally:
        store.set_delivery_features(False)


# spec: ANYM-520
def test_a_resume_test_the_deployed_dispatcher_cancelled_unsent_is_staged_again_for_the_same_version(seeded, tmp_path,
        monkeypatch):
    from messaging_service.delivery_endpoints import finish_resume
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    [(token, eid, sids)] = endpoint_keys(store, tmp_path, monkeypatch, [1])
    try:
        version = paused_endpoint(store, eid)
        arguments = {'endpoint_id': eid, 'version': version, 'action': 'resume'}
        pending = store.update_delivery_endpoint(token, eid, version, 'resume')
        # The endpoint view's pending test names the resume to repeat, never a request ID no agent may send.
        [view] = store.list_delivery_endpoints(token)['items']
        assert view['last_test']['next_step'] == {'tool': 'update_delivery_endpoint', 'endpoint_id': eid,
            'version': version, 'action': 'resume'}
        # The dispatcher at the deployed SHA serves enabled endpoints only: it cancels the test before sending it.
        with store.pool.connection() as c:
            c.execute("""UPDATE delivery_endpoint_tests SET state='cancelled',error_code='access_or_configuration_changed',
                finished_at=clock_timestamp() WHERE id=%s""", (pending['test']['test_id'],))
        with pytest.raises(APIError, match='endpoint_test_failed') as refused:
            asyncio.run(finish_resume(store, token, arguments, pending, wait_s=1))
        assert (refused.value.test_error, refused.value.version) == ('access_or_configuration_changed', version)
        assert endpoint_state(store, eid) == {'state': 'auto_paused', 'version': version}
        # Never sent, so the repeat stages a new test for the same version, which this dispatcher delivers.
        aged_tests(store)
        again = store.update_delivery_endpoint(token, eid, version, 'resume')
        assert again['test']['test_id'] != pending['test']['test_id']
        dispatch_hints(store, transport=lambda *a, **k: 204, resolver=PUBLIC)
        resumed = asyncio.run(finish_resume(store, token, arguments, again, wait_s=1))
        assert (resumed['state'], resumed['woken_subscriptions']) == ('enabled', sids)
    finally:
        store.set_delivery_features(False)


# spec: ANYM-520
def test_a_resume_refuses_busy_instead_of_waiting_for_a_subscription_a_reconfigure_holds(seeded, tmp_path,
        monkeypatch):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    [(token, eid, sids)] = endpoint_keys(store, tmp_path, monkeypatch, [1])
    try:
        version = paused_endpoint(store, eid)
        store.update_delivery_endpoint(token, eid, version, 'resume')
        dispatch_hints(store, transport=lambda *a, **k: 204, resolver=PUBLIC)
        with store.pool.connection() as other:
            # configure_subscription_delivery holds the subscription row, then waits for the endpoint lock.
            other.execute('SELECT id FROM subscriptions WHERE id=%s FOR UPDATE', (sids[0],))
            with pytest.raises(APIError, match='service_busy'):
                store.update_delivery_endpoint(token, eid, version, 'resume')
        assert endpoint_state(store, eid) == {'state': 'auto_paused', 'version': version}
        resumed = store.update_delivery_endpoint(token, eid, version, 'resume')
        assert (resumed['state'], resumed['woken_subscriptions']) == ('enabled', sids)
    finally:
        store.set_delivery_features(False)


# spec: ANYM-520
def test_a_resume_wakes_every_bound_subscription_past_the_key_queue_cap(seeded, tmp_path, monkeypatch):
    from messaging_service import event_delivery
    store, _, _ = seeded
    [(token, eid, sids)] = endpoint_keys(store, tmp_path, monkeypatch, [3])
    monkeypatch.setattr(event_delivery, 'ENDPOINT_QUEUE_PER_KEY', 1)
    try:
        version = paused_endpoint(store, eid)
        store.update_delivery_endpoint(token, eid, version, 'resume')
        event_delivery.dispatch_hints(store, transport=lambda *a, **k: 204, resolver=PUBLIC)
        resumed = store.update_delivery_endpoint(token, eid, version, 'resume')
        assert sorted(resumed['woken_subscriptions']) == sorted(sids)
        with store.pool.connection() as c:
            assert c.execute("SELECT count(*) AS n FROM subscription_outbox WHERE state='pending'").fetchone()['n'] == 3
    finally:
        store.set_delivery_features(False)


# spec: ANYM-520
def test_a_resume_wake_of_a_dynamic_definition_covers_every_visible_event_above_its_ack_position(seeded, tmp_path,
        monkeypatch):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token, endpoint, _, _ = endpoint_setup(store, tmp_path, monkeypatch, events=0)
        eid = endpoint['endpoint_id']
        second_chat(store)
        sid = store.create_subscription(token, sources={'all_visible_chats': True},
            delivery={'endpoint_id': eid})['subscription_id']
        kept = emit(store)
        match_subscriptions(store)
        paused = store.update_delivery_endpoint(token, eid, endpoint_state(store, eid)['version'], 'pause')
        # While paused, the frontier passes an event the key no longer sees; the hint for `kept` was cancelled.
        store.set_access('endpoint-reader', chats=['telegram-demo-chat'])
        emit_in(store, 'demo-tg-x')
        match_subscriptions(store)
        aged_tests(store)
        store.update_delivery_endpoint(token, eid, paused['version'], 'resume')
        dispatch_hints(store, transport=lambda *a, **k: 204, resolver=PUBLIC)
        resumed = store.update_delivery_endpoint(token, eid, paused['version'], 'resume')
        assert resumed['woken_subscriptions'] == [sid]
        sent = []
        assert dispatch_hints(store, transport=lambda url, body, headers, **k: sent.append(json.loads(body)) or 204,
            resolver=PUBLIC)['delivered'] == 1
        assert [ref['event_id'] for ref in sent[0]['references']] == [kept]
    finally:
        store.set_delivery_features(False)
        store.set_dynamic_subscriptions(False)


# spec: ANYM-520
def test_auto_pause_skips_an_endpoint_while_an_attempt_holds_it_and_reads_that_attempts_success(seeded, tmp_path,
        monkeypatch):
    from messaging_service.delivery_endpoints import auto_pause, lock_endpoint
    store, _, _ = seeded
    [(_, eid, _)] = endpoint_keys(store, tmp_path, monkeypatch, [1])
    def dead_letters(c, hint):
        for _ in range(3):
            c.execute('''INSERT INTO delivery_attempts(endpoint,hint,subscription,attempt,outcome,http_status,error_code)
                VALUES(%s,%s,%s,1,'dead_letter',301,'redirect')''', (eid, hint['id'], hint['subscription']))
    try:
        with store.pool.connection() as c:
            hint = c.execute('SELECT id,subscription FROM subscription_outbox WHERE endpoint=%s', (eid,)).fetchone()
            dead_letters(c, hint)
        with store.pool.connection() as attempt:
            # An attempt holds the endpoint lock shared until its outcome commits: here a delivery after them.
            lock_endpoint(attempt, eid, shared=True)
            attempt.execute("""UPDATE subscription_outbox SET state='delivered',finished_at=clock_timestamp()
                WHERE id=%s""", (hint['id'],))
            assert auto_pause(store, eid) is False and endpoint_state(store, eid)['state'] == 'enabled'
        assert auto_pause(store, eid) is False and endpoint_state(store, eid)['state'] == 'enabled'
        # Three dead letters after that success pause it.
        with store.pool.connection() as c:
            dead_letters(c, hint)
        assert auto_pause(store, eid) is True and endpoint_state(store, eid)['state'] == 'auto_paused'
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


def answering(head):
    """A transport whose receiver answers `head`, read by the real HTTP reader (egress.post)."""
    from messaging_service.egress import deadline_after, post
    class Connection:
        def __init__(self): self.replies = [head]
        def settimeout(self, value): pass
        def sendall(self, data): pass
        def recv(self, size): return self.replies.pop(0) if self.replies else b''
    def transport(url, body, headers, *, resolved, timeout=5):
        return post(Connection(), 'hooks.example.test', '/hook', body, headers, deadline_after(timeout), retry_after=True)
    return transport


def retry_delay(store):
    with store.pool.connection() as c:
        row = c.execute("""SELECT state, error_code,
            extract(epoch FROM next_attempt_at-clock_timestamp())::float AS seconds FROM subscription_outbox""").fetchone()
    return row['state'], row['seconds']


def http_date(hours):
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    return format_datetime(datetime.now(timezone.utc) + timedelta(hours=hours), usegmt=True)


# spec: ANYM-604
@pytest.mark.parametrize('status,header,delay', [
    (429, 'Retry-After: 600', 600), (503, 'Retry-After: ' + http_date(2), 3600), (429, 'Retry-After: abc', 2),
    (429, 'Retry-After: -5', 2), (503, 'Retry-After: ' + http_date(-1), 2), (429, 'Retry-After: 1', 2),
    (500, 'Retry-After: 600', 2), (429, None, 2)],
    ids=['delta', 'date-capped', 'unparsable', 'negative', 'past-date', 'below-own', 'other-status', 'bare-int'])
def test_endpoint_retry_waits_for_the_receivers_retry_after_up_to_an_hour(seeded, tmp_path, monkeypatch, status, header, delay):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    try:
        endpoint_setup(store, tmp_path, monkeypatch)
        transport = (lambda *a, **k: status) if header is None else answering(
            b'HTTP/1.1 %d Busy\r\n%s\r\nContent-Length: 0\r\n\r\n' % (status, header.encode()))
        assert dispatch_hints(store, lane='endpoint', transport=transport, resolver=PUBLIC)['retried'] == 1
        state, seconds = retry_delay(store)
        assert state == 'pending' and delay - 30 < seconds <= delay
    finally:
        store.set_delivery_features(False)


# spec: ANYM-604
def test_operator_receiver_retry_keeps_its_own_backoff_despite_retry_after(seeded, tmp_path):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    token = identity(store)
    sid = subscription(store, token)
    rid, _ = receiver(store, token, tmp_path)
    call(store, token, 'configure_subscription_delivery', sid, 1, rid)
    emit(store)
    match_subscriptions(store)
    transport = answering(b'HTTP/1.1 429 Busy\r\nRetry-After: 600\r\n\r\n')
    assert dispatch_hints(store, transport=transport, resolver=PUBLIC)['retried'] == 1
    state, seconds = retry_delay(store)
    assert state == 'pending' and 0 < seconds <= 2


# spec: ANYM-604
def test_post_reads_retry_after_only_from_a_complete_429_or_503_head():
    from messaging_service.egress import deadline_after, post, retry_after_seconds
    from datetime import datetime, timezone
    class Connection:
        def __init__(self, *replies): self.replies = list(replies)
        def settimeout(self, value): pass
        def sendall(self, data): pass
        def recv(self, size): return self.replies.pop(0) if self.replies else b''
    def answer(*replies, **kwargs):
        return post(Connection(*replies), 'hooks.example.test', '/', b'{}', {}, deadline_after(5), **kwargs)
    # A final status other than 429/503 returns at its status line, as before: the rest of the head is never read.
    assert answer(b'HTTP/1.1 200 OK\r\n', retry_after=True) == (200, None)
    assert answer(b'HTTP/1.1 429 Busy\r\nRetry-After: 600\r\n\r\n') == 429
    assert answer(b'HTTP/1.1 429 Busy\r\nRetry-', b'After: 600\r\n\r\n', retry_after=True) == (429, 600)
    # A head cut short, over the limit or with two values keeps the dispatcher's own backoff.
    assert answer(b'HTTP/1.1 503 Busy\r\nRetry-After: 600\r\n', retry_after=True) == (503, None)
    assert answer(b'HTTP/1.1 429 Busy\r\n', b'X: ' + b'y' * 5000 + b'\r\n\r\n', retry_after=True) == (429, None)
    assert answer(b'HTTP/1.1 429 Busy\r\nRetry-After: 60\r\nretry-after: 600\r\n\r\n', retry_after=True) == (429, None)
    now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    assert retry_after_seconds(b'HTTP/1.1 503 x\r\nretry-after:  120 ', now) == 120
    assert retry_after_seconds(b'HTTP/1.1 503 x\r\nRetry-After: Wed, 28 Oct 2026 12:10:00 GMT', now) == 600
    assert retry_after_seconds(b'HTTP/1.1 503 x\r\nRetry-After: ' + b'9' * 4000, now) > 3600
    assert retry_after_seconds(b'HTTP/1.1 503 x\r\nRetry-After: 0000000120', now) == 120
    assert retry_after_seconds(b'HTTP/1.1 503 x\r\nRetry-After: 0000000000', now) == 0
    assert retry_after_seconds(b'HTTP/1.1 503 x\r\nRetry-After: Wednesday, 28-Oct-26 12:10:00 GMT', now) == 600
    assert retry_after_seconds(b'HTTP/1.1 503 x\r\nRetry-After: Wed Oct 28 12:10:00 2026', now) == 600
    # RFC 9110 section 5.6.7: a two-digit year is at most 50 years ahead, so 69 is 2069 and 99 is 1999.
    assert retry_after_seconds(b'HTTP/1.1 503 x\r\nRetry-After: Monday, 28-Oct-69 12:10:00 GMT', now) > 3600
    assert retry_after_seconds(b'HTTP/1.1 503 x\r\nRetry-After: Thursday, 28-Oct-99 12:10:00 GMT', now) is None
    for value in (b'', b'1.5', b'-1', b'abc', b'Wed, 28 Oct 2026 11:00:00 GMT', b'Wed, 99 Oct 2026 12:00:00 GMT',
                  b'Wed, 28 Oct 2026 12:10:00 GMT, Wed, 28 Oct 2026 12:20:00 GMT', b'Wed, 28 Oct 2026 12:10:00 GMT \xc3\xa9',
                  b'Wed, 28 Xyz 2026 12:10:00 GMT', b'Wed, 28 Oct 2026 12:10:00 +0000', b'600 seconds'):
        assert retry_after_seconds(b'HTTP/1.1 503 x\r\nRetry-After: ' + value, now) is None
