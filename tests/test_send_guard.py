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
        'after_count': 20, 'view': 'conversation'}, 'caller_supplies': []}
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
        after_count=step['after_count'], view=step['view'])['context']
    ids = [row['id'] for row in window]
    assert ids[0] == error.first_newer_message_id and set(counted) <= set(ids)


# spec: ANYM-736
def test_next_call_window_holds_every_counted_message_at_one_instant(guarded):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    # One sent_at: the guard orders Telegram messages by numeric provider ID, the window must do the same.
    counted = [say(archive, 30 + n, 10) for n in range(10)]
    error = refused(s, key, expected_last_message_id=seen)
    assert error.newer_count == 10 and error.first_newer_message_id == counted[0]
    step = error.next_call['arguments']
    window = s.get_context(key, step['message_id'], before_count=step['before_count'],
        after_count=step['after_count'], view=step['view'])['context']
    assert [row['id'] for row in window][0] == counted[0] and set(counted) <= {row['id'] for row in window}


def listed(context):
    return {context['message']['id']} | {row['id'] for row in context['context']}


# spec: ANYM-736
def test_returned_continuation_reads_every_counted_message_over_rest_and_mcp(guarded):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    counted = [say(archive, 30 + n, 10) for n in range(10)]
    headers = {'Authorization': 'Bearer ' + key}
    with TestClient(create_app(s, ['testserver'])) as client:
        refusal = client.post('/v1/send_message', headers=headers, json={'account': ACCOUNT, 'chat_id': CHAT,
            'text': 'Exact synthetic text', 'request_id': 'guard-request-0001', 'expected_last_message_id': seen})
        body = refusal.json()
        assert refusal.status_code == 409 and body['error'] == 'chat_changed_since_seen'
        step = body['next_call']
        assert step['tool'] == 'get_context' and step['arguments']['view'] == 'conversation'
        rest = client.post('/v1/get_context', headers=headers, json=step['arguments'])
        mcp = client.post('/mcp', headers={**headers, 'Accept': 'application/json, text/event-stream'},
            json={'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                  'params': {'name': step['tool'], 'arguments': step['arguments']}})
    assert rest.status_code == 200 and set(counted) <= listed(rest.json())
    assert mcp.json()['result']['isError'] is False
    assert set(counted) <= listed(mcp.json()['result']['structuredContent'])
    assert rows(s) == []


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
@pytest.mark.parametrize('failure', ['changed', 'timeout'])
def test_refusal_never_hides_a_twin_committed_meanwhile(guarded, monkeypatch, failure):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    say(archive, 2, 10)
    twin = {}
    real = send_guard.newer

    def racing(c, *args):
        # A concurrent twin commits this request_id after the lock-free look, while the guard read runs.
        twin.update(send(s, key))
        if failure == 'timeout':
            raise psycopg.errors.QueryCanceled('synthetic')
        return real(c, *args)
    monkeypatch.setattr(send_guard, 'newer', racing)
    result = send(s, key, expected_last_message_id=seen)
    assert result['send_id'] == twin['send_id'] and len(rows(s)) == 1
    # A different payload under that ID is the conflict, never the guard's refusal.
    with pytest.raises(APIError, match='idempotency_conflict'):
        send(s, key, text='Different synthetic text', expected_last_message_id=seen)


# spec: ANYM-736
def test_status_by_request_id_recovers_a_lost_response_without_read_access(guarded):
    s, archive, key = guarded
    seen = say(archive, 1, 0)
    first = send(s, key, expected_last_message_id=seen)   # the response is "lost": only the request_id is kept
    s.set_access('guard-agent', permissions=['send'])
    say(archive, 2, 10)
    # The retry would also return the saved row, but the gateway refuses it without read access: the status call
    # by request_id is the recovery path, and it needs neither read access nor a guard.
    saved = s.get_send_status(key, request_id='guard-request-0001')
    assert saved['send_id'] == first['send_id'] and saved['status'] == first['status'] and len(rows(s)) == 1


# spec: ANYM-736
@pytest.mark.parametrize('arguments', [{}, {'send_id': 'send_x', 'request_id': 'guard-request-0001'}])
def test_status_takes_exactly_one_of_send_id_and_request_id(guarded, arguments):
    s, archive, key = guarded
    with pytest.raises(APIError) as failure:
        s.get_send_status(key, **arguments)
    assert failure.value.code == 'invalid_parameters'
    assert failure.value.field_errors == [{'field': 'send_id', 'reason': 'required'}]


# spec: ANYM-736
def test_status_by_request_id_is_scoped_to_the_principal(guarded):
    s, archive, key = guarded
    say(archive, 1, 0)
    send(s, key)
    other = s.issue_key('guard-other-agent', [ACCOUNT], permissions=['read', 'send'])
    for request_id in ('guard-request-0001', 'guard-missing-0001', 'x', 'child:' + 'a' * 10):
        with pytest.raises(APIError) as failure:
            s.get_send_status(other, request_id=request_id)
        assert (failure.value.code, failure.value.status) == ('not_found', 404)


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


# ANYM-748: the unresolved-duplicate refusal. Store calls pass duplicate_guard=True as the send_message tool does.
@pytest.fixture
def duplicated(guarded):
    """The guard fixture with one archived message, so the chat exists for a send."""
    s, archive, key = guarded
    say(archive, 999, -10)
    return guarded


def dup(s, key, request_id='guard-request-0001', **changes):
    return send(s, key, request_id, duplicate_guard=True, **changes)


def dup_refused(s, key, request_id='guard-request-0002', **changes):
    with pytest.raises(APIError) as failure:
        dup(s, key, request_id, **changes)
    return failure.value


def settle(s, rid, status, /, **columns):
    """Move one saved send to `status` (and any other column), as the worker or an operator would."""
    columns = {'status': status, **columns}
    names = ','.join(name + '=%s' for name in columns)
    with s.pool.connection() as c:
        c.execute('UPDATE outgoing_messages SET ' + names + ' WHERE request_id=%s', (*columns.values(), rid))


def aged(s, request_id, minutes):
    with s.pool.connection() as c:
        c.execute("UPDATE outgoing_messages SET created_at=clock_timestamp()-make_interval(mins=>%s),"
                  "expires_at=clock_timestamp()+interval '10 minutes' WHERE request_id=%s", (minutes, request_id))


# spec: ANYM-748
def test_unknown_earlier_send_refuses(duplicated):
    s, archive, key = duplicated
    first = dup(s, key)
    settle(s, 'guard-request-0001', 'unknown', error_code='sender_interrupted')
    error = dup_refused(s, key)
    assert (error.code, error.status) == ('duplicate_send_unresolved', 409)
    assert (error.earlier_request_id, error.earlier_status) == ('guard-request-0001', 'unknown')
    assert error.next_call == {'tool': 'get_send_status', 'arguments': {'request_id': 'guard-request-0001'},
                               'caller_supplies': []}
    body = payload(error, 'send_message')
    assert body['error'] == 'duplicate_send_unresolved' and body['next_call'] == error.next_call
    assert (body['earlier_request_id'], body['earlier_status']) == ('guard-request-0001', 'unknown')
    # No message text and never the internal send id.
    assert 'Exact synthetic text' not in str(body) and first['send_id'] not in str(body)
    assert len(rows(s)) == 1
    # No guard state is kept: once the earlier send settles, the same call succeeds.
    settle(s, 'guard-request-0001', 'sent', provider_message_id='9001')
    assert dup(s, key, 'guard-request-0002')['status'] == 'queued'


# spec: ANYM-748
def test_queued_and_dispatching_refuse(duplicated):
    s, archive, key = duplicated
    dup(s, key)
    assert dup_refused(s, key).earlier_status == 'queued'
    settle(s, 'guard-request-0001', 'dispatching')
    assert dup_refused(s, key).earlier_status == 'dispatching'
    assert len(rows(s)) == 1


# spec: ANYM-748
@pytest.mark.parametrize('status', ['sent', 'failed'])
def test_sent_and_failed_do_not_block(duplicated, status):
    s, archive, key = duplicated
    dup(s, key)
    settle(s, 'guard-request-0001', status, error_code=None if status == 'sent' else 'provider_rejected')
    assert dup(s, key, 'guard-request-0002')['status'] == 'queued'
    assert len(rows(s)) == 2


def connected(archive, account, worker):
    epoch = archive.acquire(account, worker)
    CollectorState(archive, account, (worker, epoch)).phase('connection', 'connected', 'provider_ready')
    archive.set_sending(account, True)
    return lambda: archive.release(account, worker, epoch)


# spec: ANYM-748
def test_other_chat_text_account_principal_do_not_block(duplicated):
    s, archive, key = duplicated
    say(archive, 1, 0, chat='200')
    say(archive, 2, 0, chat='100', account='guard-other')
    release = connected(archive, 'guard-other', 'synthetic-guard-other-worker')
    try:
        both = s.issue_key('guard-both-agent', [ACCOUNT, 'guard-other'], permissions=['read', 'send'])
        other_principal = s.issue_key('guard-twin-agent', [ACCOUNT], permissions=['read', 'send'])
        dup(s, key)
        settle(s, 'guard-request-0001', 'unknown')
        assert dup(s, key, 'guard-request-0002', text='Changed synthetic text')['status'] == 'queued'
        assert s.send_message(key, account=ACCOUNT, chat_id=OTHER_CHAT, text='Exact synthetic text',
                              request_id='guard-request-0003', duplicate_guard=True)['status'] == 'queued'
        assert dup(s, other_principal, 'guard-request-0004')['status'] == 'queued'
        other_chat = stable_id('chat', 'guard-other', '100')
        assert s.send_message(both, account='guard-other', chat_id=other_chat, text='Exact synthetic text',
                              request_id='guard-request-0005', duplicate_guard=True)['status'] == 'queued'
        # The control: the same content again by the first principal is still refused.
        assert dup_refused(s, key, 'guard-request-0006').earlier_request_id == 'guard-request-0001'
    finally:
        release()


# spec: ANYM-748
def test_expired_queued_does_not_block(duplicated):
    s, archive, key = duplicated
    dup(s, key)
    with s.pool.connection() as c:
        # Expiry is projected at read time only; the stored status stays queued.
        c.execute("UPDATE outgoing_messages SET expires_at=clock_timestamp()-interval '1 second'")
    assert dup(s, key, 'guard-request-0002')['status'] == 'queued'


# spec: ANYM-748
def test_different_reply_target_format_or_render_policy_does_not_block(duplicated):
    s, archive, key = duplicated
    target = say(archive, 1, 0)
    dup(s, key)
    assert dup(s, key, 'guard-request-0002', reply_to=target)['status'] == 'queued'
    for n, policy in enumerate(({'format': 'markdown'}, {'link_preview': True}, {'allow_lossy': True}), 3):
        digest = s.preview_message(key, account=ACCOUNT, chat_id=CHAT, text='Exact synthetic text',
                                   **policy)['preview_digest']
        assert dup(s, key, f'guard-request-{n:04d}', preview_digest=digest, **policy)['status'] == 'queued'
    # The control: the same reply target again is refused and names the reply send.
    assert dup_refused(s, key, 'guard-request-0009', reply_to=target).earlier_request_id == 'guard-request-0002'


# spec: ANYM-748
def test_internal_child_request_rows_are_ignored(duplicated):
    s, archive, key = duplicated
    dup(s, key)
    # A messages_send step's row: get_send_status answers not_found for it, so a refusal could not name it.
    settle(s, 'guard-request-0001', 'unknown', request_id='agent-child:synthetic-0001')
    assert dup(s, key, 'guard-request-0002')['status'] == 'queued'


# spec: ANYM-748
def test_phone_username_and_send_intent_paths_unchanged(duplicated):
    import inspect
    from messaging_service import outgoing, phone_send, send_intent, username_send
    s, archive, key = duplicated
    assert inspect.signature(outgoing.enqueue).parameters['duplicate_guard'].default is False
    assert inspect.signature(type(s).send_message).parameters['duplicate_guard'].default is False
    for module in (phone_send, username_send, send_intent):
        assert 'duplicate_guard' not in inspect.getsource(module) and 'allow_duplicate' not in inspect.getsource(module)
    # The shared enqueue without the keyword queues identical unresolved content as before.
    with s.authorized(key, 'send_message') as (c, p):
        first = outgoing.enqueue(c, p, ACCOUNT, CHAT, 'Exact synthetic text', 'guard-request-0001')
        second = outgoing.enqueue(c, p, ACCOUNT, CHAT, 'Exact synthetic text', 'guard-request-0002')
    assert first['status'] == second['status'] == 'queued' and len(rows(s)) == 2


# spec: ANYM-748
@pytest.mark.parametrize('minutes,blocks', [(59, True), (61, False)])
def test_61_minutes_does_not_block(duplicated, minutes, blocks):
    s, archive, key = duplicated
    dup(s, key)
    settle(s, 'guard-request-0001', 'unknown')
    aged(s, 'guard-request-0001', minutes)
    if blocks:
        assert dup_refused(s, key).code == 'duplicate_send_unresolved'
    else:
        assert dup(s, key, 'guard-request-0002')['status'] == 'queued'


# spec: ANYM-748
def test_same_request_id_returns_saved_result_before_guard(duplicated, monkeypatch):
    from messaging_service import outgoing
    s, archive, key = duplicated
    first = dup(s, key)
    settle(s, 'guard-request-0001', 'unknown')

    def forbidden(*args, **kwargs):
        raise AssertionError('the duplicate lookup ran for a saved request_id')
    monkeypatch.setattr(outgoing, 'unresolved_send', forbidden)
    again = dup(s, key)
    assert again['send_id'] == first['send_id'] and again['status'] == 'unknown' and len(rows(s)) == 1


# spec: ANYM-748
def test_allow_duplicate_sends(duplicated):
    s, archive, key = duplicated
    plain = dup(s, key)
    settle(s, 'guard-request-0001', 'unknown')
    assert dup_refused(s, key, allow_duplicate=False).code == 'duplicate_send_unresolved'
    allowed = dup(s, key, 'guard-request-0002', allow_duplicate=True)
    assert allowed['status'] == 'queued' and set(allowed) == set(plain)
    stored = rows(s)
    # No part of the stored request: the same hash and digest as the first send.
    assert stored[0]['request_hash'] == stored[1]['request_hash']
    assert stored[0]['preview_digest'] == stored[1]['preview_digest']
    # Its retry without the opt-out returns the saved row.
    assert dup(s, key, 'guard-request-0002')['send_id'] == allowed['send_id'] and len(rows(s)) == 2
    error = refused(s, key, request_id='guard-request-0003', allow_duplicate='yes', duplicate_guard=True)
    assert error.code == 'invalid_parameters' and error.field_errors[0]['field'] == 'allow_duplicate'


# spec: ANYM-748
def test_duplicate_refusal_creates_no_row(duplicated):
    s, archive, key = duplicated
    dup(s, key)
    settle(s, 'guard-request-0001', 'unknown')
    for n in range(2, 5):
        dup_refused(s, key, f'guard-request-{n:04d}')
    assert [r['request_id'] for r in rows(s)] == ['guard-request-0001']


# spec: ANYM-748
def test_concurrent_identical_calls_one_wins(duplicated, monkeypatch):
    import time
    from messaging_service import outgoing
    s, archive, key = duplicated
    real = outgoing.unresolved_send

    def slow(*args):
        # Widens the window: a lookup before the lock would let both calls see nothing and both queue.
        time.sleep(0.3)
        return real(*args)
    monkeypatch.setattr(outgoing, 'unresolved_send', slow)
    start, outcomes = threading.Barrier(2), []

    def call(n):
        start.wait(10)
        try:
            outcomes.append(dup(s, key, f'guard-concurrent-{n:04d}')['status'])
        except APIError as error:
            outcomes.append(error.code)
    threads = [threading.Thread(target=call, args=(n,)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert sorted(outcomes) == ['duplicate_send_unresolved', 'queued'] and len(rows(s)) == 1


# spec: ANYM-748
def test_lookup_timeout_refuses_nothing_queued(duplicated, monkeypatch):
    from messaging_service import outgoing
    s, archive, key = duplicated
    monkeypatch.setattr(outgoing, 'DUPLICATE_TIMEOUT_MS', 50)
    monkeypatch.setattr(outgoing, 'unresolved_send', lambda c, *args: c.execute('SELECT pg_sleep(2)'))
    error = dup_refused(s, key, 'guard-request-0001')
    assert (error.code, error.status) == ('chat_guard_unavailable', 503)
    assert payload(error, 'send_message')['retryable'] is True and rows(s) == []
    monkeypatch.setattr(outgoing, 'unresolved_send',
                        lambda *args: (_ for _ in ()).throw(psycopg.OperationalError('synthetic')))
    assert dup_refused(s, key, 'guard-request-0001').code == 'chat_guard_unavailable'
    assert rows(s) == []
    # The session's own statement timeout is back after a successful lookup.
    monkeypatch.undo()
    with s.authorized(key, 'send_message') as (c, p):
        before = c.execute("SELECT current_setting('statement_timeout') AS v").fetchone()['v']
        outgoing.refuse_unresolved_duplicate(c, p, ACCOUNT, CHAT, 'guard-request-0001', 'Exact synthetic text',
                                             'plain', False, False, None)
        assert c.execute("SELECT current_setting('statement_timeout') AS v").fetchone()['v'] == before


# spec: ANYM-748
def test_the_60_minute_window_bounds_the_principal_time_index(duplicated):
    import json
    from psycopg.types.json import Jsonb
    from messaging_service import outgoing
    s, archive, key = duplicated
    dup(s, key)
    with s.authorized(key, 'send_message') as (c, p):
        c.execute('SET LOCAL enable_seqscan=off')
        plan = c.execute('EXPLAIN (FORMAT JSON) ' + outgoing.DUPLICATE_SQL, (p['id'], ACCOUNT, CHAT,
            'Exact synthetic text', 'plain', Jsonb({'link_preview': False, 'allow_lossy': False}), None,
            'guard-request-0002', outgoing.CHILD_KEY_PREFIX)).fetchone()
    def conditions(node):
        yield node.get('Index Cond', '')
        for child in node.get('Plans', []):
            yield from conditions(child)
    found = list(conditions(plan['QUERY PLAN'][0]['Plan']))
    # A volatile clock in the bound could never be an index condition: the scan would read the whole history.
    assert any('created_at' in condition for condition in found), json.dumps(plan)


# spec: ANYM-748
def test_a_lost_connection_during_the_lookup_is_chat_guard_unavailable_and_audited(duplicated, monkeypatch):
    from messaging_service import outgoing
    s, archive, key = duplicated
    # The lookup's own session ends, as a server restart or network loss would end it.
    monkeypatch.setattr(outgoing, 'unresolved_send',
                        lambda c, *args: c.execute('SELECT pg_terminate_backend(pg_backend_pid())'))
    error = dup_refused(s, key, 'guard-request-0001')
    assert (error.code, error.status) == ('chat_guard_unavailable', 503) and error.audited
    with TestClient(create_app(s, ['testserver'])) as client:
        rest = client.post('/v1/send_message', headers={'Authorization': 'Bearer ' + key}, json={
            'account': ACCOUNT, 'chat_id': CHAT, 'text': 'Exact synthetic text', 'request_id': 'guard-request-0002'})
    assert rest.status_code == 503 and rest.json()['error'] == 'chat_guard_unavailable'
    assert rows(s) == []
    with s.pool.connection() as c:
        audits = c.execute("SELECT outcome,error_code FROM audit_log WHERE operation='send_message'").fetchall()
    assert [(a['outcome'], a['error_code']) for a in audits] == [('denied', 'chat_guard_unavailable')] * 2


# spec: ANYM-748
def test_send_message_tool_runs_the_duplicate_guard_over_rest_and_mcp(duplicated):
    s, archive, key = duplicated
    with TestClient(create_app(s, ['testserver'])) as client:
        headers = {'Authorization': 'Bearer ' + key}
        mcp = {**headers, 'Accept': 'application/json, text/event-stream'}
        body = {'account': ACCOUNT, 'chat_id': CHAT, 'text': 'Exact synthetic text', 'request_id': 'guard-request-0001'}
        assert client.post('/v1/send_message', headers=headers, json=body).status_code == 200
        settle(s, 'guard-request-0001', 'unknown')
        rest = client.post('/v1/send_message', headers=headers, json={**body, 'request_id': 'guard-request-0002'})
        tool = client.post('/mcp', headers=mcp, json={'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
            'name': 'send_message', 'arguments': {**body, 'request_id': 'guard-request-0003'}}}).json()['result']
        null = client.post('/v1/send_message', headers=headers,
                           json={**body, 'request_id': 'guard-request-0004', 'allow_duplicate': None})
        allowed = client.post('/v1/send_message', headers=headers,
                              json={**body, 'request_id': 'guard-request-0005', 'allow_duplicate': True})
        tools = client.post('/mcp', headers=mcp,
            json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}).json()['result']['tools']
    assert rest.status_code == 409 and rest.json()['error'] == 'duplicate_send_unresolved'
    assert rest.json()['earlier_request_id'] == 'guard-request-0001' and rest.json()['earlier_status'] == 'unknown'
    assert tool['isError'] is True and 'duplicate_send_unresolved' in str(tool)
    assert null.status_code == 409 and allowed.status_code == 200
    assert [r['request_id'] for r in rows(s)] == ['guard-request-0001', 'guard-request-0005']
    properties = next(t for t in tools if t['name'] == 'send_message')['inputSchema']['properties']
    # No schema default: the gateway inserts defaults into routed arguments.
    assert 'allow_duplicate' in properties and 'default' not in properties['allow_duplicate']


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
