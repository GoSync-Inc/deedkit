"""ANYM-736: send_message's optional chat-head precondition (expected_last_message_id, guard).

Synthetic accounts and chats in an isolated PostgreSQL; no provider is started and nothing is sent.
"""
import threading
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from starlette.testclient import TestClient

from messaging_service import send_guard
from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState
from messaging_service.envelope import stable_id
from messaging_service.error_details import payload
from messaging_service.store import APIError
from test_archive import event, wa_event

ACCOUNT = 'guard-fixture'
CHAT = stable_id('chat', ACCOUNT, '100')
OTHER_CHAT = stable_id('chat', ACCOUNT, '200')
BASE = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
CANARY = 'Synthetic private canary text'


def stamp(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def guarded(seeded):
    s, _, _ = seeded
    archive = Archive(s)
    archive.register(ACCOUNT, 'telegram', 'Synthetic guard fixture', discover=True)
    archive.register('guard-other', 'telegram', 'Synthetic second account', discover=True)
    key = s.issue_key('guard-agent', [ACCOUNT], permissions=['read', 'send'])
    epoch = archive.acquire(ACCOUNT, 'synthetic-guard-worker')
    state = CollectorState(archive, ACCOUNT, ('synthetic-guard-worker', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    archive.set_sending(ACCOUNT, True)
    yield s, archive, key
    archive.release(ACCOUNT, 'synthetic-guard-worker', epoch)


def say(archive, provider_id, seconds, direction='incoming', chat='100', account=ACCOUNT, **changes):
    """Ingest one synthetic message and return its archive ID."""
    return archive.ingest(event(account=account, chat=chat, provider_id=str(provider_id), body=f'{CANARY} {provider_id}',
        sent_at=stamp(seconds), updated_at=stamp(seconds), direction=direction, **changes))['message']


def send(s, key, request_id='guard-request-0001', text='Exact synthetic text', **changes):
    return s.send_message(key, account=ACCOUNT, chat_id=CHAT, text=text, request_id=request_id, **changes)


def rows(s):
    with s.pool.connection() as c:
        return c.execute('SELECT * FROM outgoing_messages ORDER BY created_at').fetchall()


def refused(s, key, **changes):
    with pytest.raises(APIError) as failure:
        send(s, key, **changes)
    return failure.value


# spec: ANYM-736
def test_newer_incoming_refuses(guarded):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    newer = say(archive, 2, 10)
    error = refused(s, key, expected_last_message_id=seen)
    assert (error.code, error.status) == ('chat_changed_since_seen', 409)
    assert (error.newer_count, error.newer_count_capped, error.newer_outgoing_count) == (1, False, 0)
    assert error.first_newer_message_id == error.newest_message_id == newer
    assert error.next_call == {'tool': 'get_context', 'arguments': {'message_id': newer, 'before_count': 0,
        'after_count': 20}, 'caller_supplies': []}
    body = payload(error, 'send_message')
    assert body['error'] == 'chat_changed_since_seen' and body['next_call'] == error.next_call
    assert CANARY not in str(body) and 'Exact synthetic text' not in str(body)
    assert rows(s) == []
    # Naming the newest message clears the guard.
    assert send(s, key, expected_last_message_id=newer)['status'] == 'queued'


# spec: ANYM-736
def test_outgoing_only_counts_owner_outgoing(guarded):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    say(archive, 2, 10)
    assert send(s, key, 'guard-request-0002', expected_last_message_id=seen, guard='outgoing_only')['status'] == 'queued'
    owner = say(archive, 3, 20, direction='outgoing')
    error = refused(s, key, request_id='guard-request-0003', expected_last_message_id=seen, guard='outgoing_only')
    assert (error.newer_count, error.newer_outgoing_count, error.first_newer_message_id) == (1, 1, owner)
    both = refused(s, key, request_id='guard-request-0004', expected_last_message_id=seen)
    assert (both.newer_count, both.newer_outgoing_count) == (2, 1)


# spec: ANYM-736
def test_own_earlier_send_counts(guarded):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    assert send(s, key, expected_last_message_id=seen)['status'] == 'queued'
    # The collector ingests the agent's own send: the next guarded call must name it, there is no exemption.
    mine = say(archive, 2, 5, direction='outgoing')
    error = refused(s, key, request_id='guard-request-0002', expected_last_message_id=seen)
    assert error.code == 'chat_changed_since_seen' and error.newest_message_id == mine
    assert send(s, key, request_id='guard-request-0003', expected_last_message_id=mine)['status'] == 'queued'


# spec: ANYM-736
def test_whatsapp_equal_timestamp_not_counted(seeded):
    s, _, _ = seeded
    archive = Archive(s)
    archive.register('guard-wa', 'whatsapp', 'Synthetic WhatsApp guard', discover=True)
    chat = stable_id('chat', 'guard-wa', '900@lid')

    def put(provider_id, seconds):
        return archive.ingest(wa_event(account='guard-wa', chat='900@lid', provider_id=provider_id, body=CANARY,
            sent_at=stamp(seconds), updated_at=stamp(seconds)))['message']
    seen, same = put('false_900@lid_3EB0AAA1', 0), put('false_900@lid_3EB0AAA2', 0)
    later = put('false_900@lid_3EB0AAA3', 1)
    with s.pool.connection() as c:
        # Provider IDs are never chronology on WhatsApp: the same instant is invisible to the guard.
        found = send_guard.newer(c, 'guard-wa', chat, 'whatsapp', seen, 'any_message')
        assert [r['id'] for r in found[0]] == [later] and found[1] == later and same not in found[0]
        assert send_guard.newer(c, 'guard-wa', chat, 'whatsapp', later, 'any_message') == ([], None)
        assert send_guard.newer(c, 'guard-wa', chat, 'whatsapp', same, 'any_message')[1] == later


# spec: ANYM-736
def test_telegram_numeric_tiebreak(guarded):
    s, archive, key = guarded
    five, nine = say(archive, 5, 0), say(archive, 9, 0)
    word = say(archive, 'synthetic-word-id', 0)
    with s.pool.connection() as c:
        newer = lambda expected: send_guard.newer(c, ACCOUNT, CHAT, 'telegram', expected, 'any_message')
        assert [r['id'] for r in newer(five)[0]] == [nine]
        assert newer(nine) == ([], None)
        # Without a numeric order on either side the same instant is not counted.
        assert newer(word) == ([], None)
    assert refused(s, key, expected_last_message_id=five).newest_message_id == nine
    assert send(s, key, request_id='guard-request-0002', expected_last_message_id=nine)['status'] == 'queued'


# spec: ANYM-736
def test_deleted_and_service_rows_do_not_count(guarded):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    say(archive, 2, 10, deleted=True)
    say(archive, 3, 20, metadata={'message_kind': 'system', 'is_service': True})
    assert send(s, key, expected_last_message_id=seen)['status'] == 'queued'
    real = say(archive, 4, 30)
    error = refused(s, key, request_id='guard-request-0002', expected_last_message_id=seen)
    assert (error.newer_count, error.first_newer_message_id) == (1, real)


# spec: ANYM-736
@pytest.mark.parametrize('extra,count,capped', [(20, 20, False), (21, 20, True)])
def test_count_cap_20_21_boundary(guarded, extra, count, capped):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    ids = [say(archive, 100 + n, 10 + n) for n in range(extra)]
    error = refused(s, key, expected_last_message_id=seen)
    assert (error.newer_count, error.newer_count_capped) == (count, capped)
    assert (error.first_newer_message_id, error.newest_message_id) == (ids[0], ids[-1])


# spec: ANYM-736
@pytest.mark.parametrize('guard', ['any_message', 'outgoing_only'])
def test_next_call_window_starts_at_first_counted_message(guarded, guard):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    say(archive, 2, 5, metadata={'message_kind': 'system', 'is_service': True})
    say(archive, 3, 6, deleted=True)
    mixed = [say(archive, 10 + n, 10 + n, direction='outgoing' if n % 3 == 0 else 'incoming') for n in range(8)]
    counted = [m for n, m in enumerate(mixed) if n % 3 == 0] if guard == 'outgoing_only' else mixed
    error = refused(s, key, expected_last_message_id=seen, guard=guard)
    assert error.first_newer_message_id == counted[0] and error.newest_message_id == counted[-1]
    step = error.next_call['arguments']
    window = s.get_context(key, step['message_id'], before_count=step['before_count'],
        after_count=step['after_count'], view='archive')['context']
    ids = [row['id'] for row in window]
    assert ids[0] == error.first_newer_message_id and set(counted) <= set(ids)


def normalized(response):
    body = response.json()
    body.pop('request_id', None)
    return response.status_code, body


# spec: ANYM-736
def test_unknown_expected_identical_bodies(guarded):
    s, archive, key = guarded
    other_chat = say(archive, 7, 0, chat='200')
    other_account = say(archive, 8, 0, chat='100', account='guard-other')
    seen = say(archive, 1, 0)
    readless = s.issue_key('guard-sendonly', [ACCOUNT], permissions=['send'])
    narrow = s.issue_key('guard-narrow', [ACCOUNT], permissions=['read', 'send'])
    s.set_access('guard-narrow', chats=[CHAT])
    with TestClient(create_app(s, ['testserver'])) as client:
        def call(token, **changes):
            body = {'account': ACCOUNT, 'chat_id': CHAT, 'text': 'Exact synthetic text',
                    'request_id': 'guard-request-0001', **changes}
            return client.post('/v1/send_message', headers={'Authorization': 'Bearer ' + token}, json=body)
        responses = [
            call(key, expected_last_message_id='msg_missing_synthetic'),   # does not exist
            call(key, expected_last_message_id=other_chat),                # another chat
            call(key, expected_last_message_id=other_account),             # another account
            call(narrow, expected_last_message_id=other_chat),             # a chat outside the key's scope
            call(readless, expected_last_message_id=seen),                 # no read permission
            call(readless, expected_last_message_id='msg_missing_synthetic'),
            call(key, chat_id='', phone='+12025550147', expected_last_message_id=seen),   # no archive chat yet
            call(key, chat_id='', username='synthetic_user', expected_last_message_id=seen),
        ]
    first = normalized(responses[0])
    assert first[0] == 400 and first[1]['error'] == 'invalid_parameters'
    assert first[1]['field_errors'] == [{'field': 'expected_last_message_id', 'reason': 'expected_message_unknown'}]
    assert [normalized(r) for r in responses] == [first] * len(responses)
    assert rows(s) == []


# spec: ANYM-736
def test_send_only_key_no_lookup(guarded, monkeypatch):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    readless = s.issue_key('guard-sendonly', [ACCOUNT], permissions=['send'])

    def spy(*args, **kwargs):
        raise AssertionError('an unauthorized guarded call looked up the chat')
    monkeypatch.setattr(send_guard, 'newer', spy)
    error = refused(s, readless, expected_last_message_id=seen)
    assert error.code == 'invalid_parameters'
    assert error.field_errors == [{'field': 'expected_last_message_id', 'reason': 'expected_message_unknown'}]
    assert rows(s) == []


# spec: ANYM-736
def test_retry_after_owner_message_returns_first_result(guarded):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    first = send(s, key, expected_last_message_id=seen)
    say(archive, 2, 10)
    again = send(s, key, expected_last_message_id=seen)
    assert again['send_id'] == first['send_id'] and len(rows(s)) == 1


# spec: ANYM-736
def test_unknown_status_never_resent(guarded):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    first = send(s, key, expected_last_message_id=seen)
    with s.pool.connection() as c:
        c.execute("UPDATE outgoing_messages SET status='unknown',error_code='sender_interrupted' WHERE id=%s",
            (first['send_id'],))
    say(archive, 2, 10)
    again = send(s, key, expected_last_message_id=seen)
    assert again['send_id'] == first['send_id'] and again['status'] == 'unknown'
    assert [r['status'] for r in rows(s)] == ['unknown']


# spec: ANYM-736
def test_payload_conflict_still_conflict(guarded):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    send(s, key, expected_last_message_id=seen)
    say(archive, 2, 10)
    with pytest.raises(APIError, match='idempotency_conflict'):
        send(s, key, text='Different synthetic text', expected_last_message_id=seen)


# spec: ANYM-736
def test_concurrent_twin_returns_saved_result(guarded, monkeypatch):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    twin = {}
    real = send_guard.check

    def racing(*args, **kwargs):
        # The same request_id is committed by a concurrent twin after the lock-free look and before the lock.
        twin.update(send(s, key))
        return real(*args, **kwargs)
    monkeypatch.setattr(send_guard, 'check', racing)
    result = send(s, key, expected_last_message_id=seen)
    assert result['send_id'] == twin['send_id'] and len(rows(s)) == 1


# spec: ANYM-736
def test_guard_timeout_refuses_nothing_queued(guarded, monkeypatch):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    monkeypatch.setattr(send_guard, 'TIMEOUT_MS', 50)
    monkeypatch.setattr(send_guard, 'newer', lambda c, *args: c.execute('SELECT pg_sleep(2)'))
    error = refused(s, key, expected_last_message_id=seen)
    assert (error.code, error.status) == ('chat_guard_unavailable', 503)
    assert payload(error, 'send_message')['retryable'] is True and rows(s) == []
    monkeypatch.setattr(send_guard, 'newer', lambda *args: (_ for _ in ()).throw(psycopg.OperationalError('synthetic')))
    assert refused(s, key, expected_last_message_id=seen).code == 'chat_guard_unavailable'
    assert rows(s) == []


# spec: ANYM-736
def test_stalled_guarded_requests_do_not_delay_unguarded_send(guarded, monkeypatch):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    entered, release = threading.Semaphore(0), threading.Event()
    real = send_guard.newer

    def stalled(*args):
        entered.release()
        assert release.wait(20)
        return real(*args)
    monkeypatch.setattr(send_guard, 'newer', stalled)
    outcomes = []

    def guarded_call(n):
        try:
            outcomes.append(send(s, key, f'guard-stalled-{n:04d}', expected_last_message_id=seen)['status'])
        except Exception as error:  # reported through the assertion below
            outcomes.append(error)
    threads = [threading.Thread(target=guarded_call, args=(n,)) for n in (1, 2)]
    try:
        for thread in threads:
            thread.start()
        assert entered.acquire(timeout=10) and entered.acquire(timeout=10)
        # Both guard reads are in flight; the principal lock is free, so an unguarded send completes.
        assert send(s, key, 'guard-unguarded-0001')['status'] == 'queued'
    finally:
        release.set()
        for thread in threads:
            thread.join(20)
    assert outcomes == ['queued', 'queued'] and len(rows(s)) == 3


# spec: ANYM-736
def test_refusal_creates_no_row(guarded):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    say(archive, 2, 10)
    refused(s, key, expected_last_message_id=seen)
    refused(s, key, request_id='guard-request-0002', expected_last_message_id='msg_missing_synthetic')
    assert rows(s) == []


# spec: ANYM-736
def test_unguarded_call_never_reaches_the_guard(guarded, monkeypatch):
    s, archive, key = guarded
    say(archive, 1, 0)

    def forbidden(*args, **kwargs):
        raise AssertionError('the guard ran for a call without its arguments')
    for name in ('check', 'newer', 'validate'):
        monkeypatch.setattr(send_guard, name, forbidden)
    plain = send(s, key)
    assert plain['status'] == 'queued'


# spec: ANYM-736
def test_guard_arguments_are_no_part_of_the_stored_request(guarded):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    plain = send(s, key, 'guard-request-0001')
    with_guard = send(s, key, 'guard-request-0002', expected_last_message_id=seen, guard='outgoing_only')
    assert set(plain) == set(with_guard)
    stored = rows(s)
    assert stored[0]['request_hash'] == stored[1]['request_hash']
    assert stored[0]['preview_digest'] == stored[1]['preview_digest']
    assert [r['body'] for r in stored] == ['Exact synthetic text'] * 2


# spec: ANYM-736
@pytest.mark.parametrize('changes,field,reason', [
    ({'guard': 'any_message'}, 'expected_last_message_id', 'required'),
    ({'expected_last_message_id': 'msg_x', 'guard': 'newest'}, 'guard', 'enum'),
])
def test_malformed_guard_arguments_are_refused(guarded, changes, field, reason):
    s, archive, key = guarded
    error = refused(s, key, **changes)
    assert error.code == 'invalid_parameters' and error.field_errors[0]['field'] == field
    assert error.field_errors[0]['reason'] == reason and rows(s) == []


# spec: ANYM-736
def test_null_guard_arguments_over_http_equal_omitted(guarded):
    s, archive, key = guarded
    say(archive, 1, 0)
    with TestClient(create_app(s, ['testserver'])) as client:
        headers = {'Authorization': 'Bearer ' + key}
        body = {'account': ACCOUNT, 'chat_id': CHAT, 'text': 'Exact synthetic text', 'request_id': 'guard-request-0001'}
        first = client.post('/v1/send_message', headers=headers, json=body)
        again = client.post('/v1/send_message', headers=headers,
            json={**body, 'expected_last_message_id': None, 'guard': None})
        tools = client.post('/mcp', headers={**headers, 'Accept': 'application/json, text/event-stream'},
            json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}).json()['result']['tools']
    assert first.status_code == again.status_code == 200 and first.json()['send_id'] == again.json()['send_id']
    properties = next(t for t in tools if t['name'] == 'send_message')['inputSchema']['properties']
    # No schema default: the gateway inserts defaults into routed arguments.
    assert 'expected_last_message_id' in properties and 'guard' in properties
    assert 'default' not in properties['expected_last_message_id'] and 'default' not in properties['guard']
