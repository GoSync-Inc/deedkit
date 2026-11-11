# ruff: noqa: F811
"""ANYM-986: a send asks, in the same call, to be woken when no reply comes (synthetic data, fake providers only)."""
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from anymsg import Client
from messaging_service import await_reply
from messaging_service.delivery_policy import flush_hints
from messaging_service.envelope import stable_id
from messaging_service.store import APIError
from test_delivery_endpoints import age, gate, identity, policy, register  # noqa: F401
from test_delivery_policy import hints, shift
from test_sending import preview_of, queued_count, sender, submit  # noqa: F401

SOURCE = Path(__file__).resolve().parents[1] / 'services' / 'messaging' / 'messaging_service'
SIX_HOURS = {'within': '6h'}


def stored(s):
    with s.pool.connection() as c:
        return c.execute('SELECT * FROM outgoing_messages ORDER BY created_at').fetchall()


# spec: ANYM-986
def test_the_preview_shows_await_reply_and_its_digest_binds_it(sender):
    s, _, key, _, _ = sender
    plain, asked = preview_of(s, key), preview_of(s, key, await_reply=SIX_HOURS)
    assert asked['await_reply']['within_s'] == 21600 and 'await_reply' not in plain
    assert asked['preview_digest'] != plain['preview_digest']
    assert preview_of(s, key, await_reply={'within': '360m'})['preview_digest'] == asked['preview_digest']
    assert preview_of(s, key, await_reply={'within': '7h'})['preview_digest'] != asked['preview_digest']
    assert asked['render_policy'] == plain['render_policy']  # its own bound field: the wire payload is unchanged
    assert preview_of(s, key, await_reply=None) == plain


# spec: ANYM-986
@pytest.mark.parametrize('bad', [{'within': '59s'}, {'within': '0m'}, {'within': '15d'}, {'within': '337h'}, {'within': '6x'},
                                 {'within': 6}, {}, 'six hours', {'within': '6h', 'wake': 'true'}, ['6h']])
def test_an_invalid_within_is_a_field_error_and_nothing_is_queued(sender, bad):
    s, _, key, _, _ = sender
    with pytest.raises(APIError, match='invalid_parameters') as error:
        preview_of(s, key, await_reply=bad)
    assert [entry['field'] for entry in error.value.field_errors][0].startswith('await_reply')
    with pytest.raises(APIError, match='invalid_parameters'):
        submit(s, key, await_reply=bad, preview_digest=preview_of(s, key)['preview_digest'])
    assert queued_count(s) == 0


# spec: ANYM-986
def test_the_digest_confirms_await_reply_a_retry_is_exact_and_a_changed_value_conflicts(sender):
    s, _, key, _, _ = sender
    asked, plain = preview_of(s, key, await_reply=SIX_HOURS), preview_of(s, key)
    with pytest.raises(APIError, match='preview_required'):
        submit(s, key, await_reply=SIX_HOURS)
    with pytest.raises(APIError, match='preview_mismatch'):
        submit(s, key, await_reply=SIX_HOURS, preview_digest=plain['preview_digest'])
    with pytest.raises(APIError, match='preview_mismatch'):
        submit(s, key, preview_digest=asked['preview_digest'])
    assert queued_count(s) == 0
    receipt = submit(s, key, await_reply=SIX_HOURS, preview_digest=asked['preview_digest'])
    assert receipt['status'] == 'queued' and receipt['await_reply'] == {'state': 'pending'}
    assert submit(s, key, await_reply=SIX_HOURS, preview_digest=asked['preview_digest'])['send_id'] == receipt['send_id']
    with pytest.raises(APIError, match='idempotency_conflict'):
        submit(s, key, await_reply={'within': '7h'}, preview_digest=preview_of(s, key, await_reply={'within': '7h'})['preview_digest'])
    with pytest.raises(APIError, match='idempotency_conflict'):
        submit(s, key, preview_digest=asked['preview_digest'])
    (row,) = stored(s)
    assert row['await_reply'] == {'within_s': 21600} and row['await_state'] == 'pending'
    assert s.get_send_status(key, receipt['send_id'])['await_reply'] == {'state': 'pending'}


# spec: ANYM-986
def test_a_send_with_await_reply_is_byte_identical_to_one_without_except_the_field_itself(sender):
    s, _, key, _, _ = sender
    submit(s, key, await_reply=SIX_HOURS, preview_digest=preview_of(s, key, await_reply=SIX_HOURS)['preview_digest'])
    s.send_message(key, account='send-fixture', chat_id=stable_id('chat', 'send-fixture', '100'),
                   text='Exact synthetic text', request_id='synthetic-request-0002')
    asked, plain = stored(s)
    for column in ('body', 'source_text', 'source_format', 'render_artifact', 'render_policy', 'provider_chat'):
        assert asked[column] == plain[column], column
    assert plain['await_reply'] is None and plain['await_state'] is None


# spec: ANYM-986
def test_migration_092_only_adds_columns_and_the_gateway_grants_name_only_them(sender):
    s, _, key, _, _ = sender
    text = (SOURCE / 'migrations' / '092_send_await_reply.sql').read_text()
    code = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('--')).upper()
    assert 'DROP' not in code and 'GRANT' not in code and 'DELETE' not in code and 'UPDATE ' not in code
    grants = [line for line in (SOURCE / 'gateway_role.sql').read_text().splitlines() if 'await_' in line and 'GRANT' in line]
    assert grants == ['GRANT INSERT(await_reply,await_state) ON outgoing_messages TO messaging_gateway;',
                      'GRANT UPDATE(await_state,await_subscription_id,await_reason) ON outgoing_messages TO messaging_gateway;']
    submit(s, key)
    with s.pool.connection() as c:
        columns = {r['column_name'] for r in c.execute("""SELECT column_name FROM information_schema.columns
            WHERE table_name='outgoing_messages' AND column_name LIKE 'await\\_%'""").fetchall()}
        assert columns == {'await_reply', 'await_state', 'await_subscription_id', 'await_reason'}
        assert c.execute('SELECT await_reply,await_state FROM outgoing_messages').fetchone() == {
            'await_reply': None, 'await_state': None}
        for bad in ("await_reply='{\"within_s\":5}'::jsonb,await_state='pending'", "await_reply='{\"within_s\":90}'::jsonb",
                    "await_state='created'", "await_state='bogus',await_reply='{\"within_s\":90}'::jsonb"):
            with pytest.raises(Exception, match='violates check constraint'), c.transaction():
                c.execute('UPDATE outgoing_messages SET ' + bad)


def asked_row(store, token, status='sent', within=3600, chat='telegram-demo-chat'):
    send = 'send_' + uuid4().hex[:12]
    with store.authorized(token) as (_, p):
        owner = p
    with store.pool.connection() as c:
        c.execute('''INSERT INTO outgoing_messages(id,principal,key_id,scope_version,key_permission_version,request_id,
            request_hash,account,chat,provider_chat,body,status,provider_message_id,await_reply,await_state)
            VALUES(%s,%s,%s,%s,%s,%s,%s,'telegram-demo',%s,'provider-chat','Synthetic',%s,'native-anchor',%s,'pending')''',
            (send, owner['id'], owner['key_id'], owner['scope_version'], owner['key_permission_version'],
             'synthetic-' + send, 'a' * 64, chat, status, json.dumps({'within_s': within})))
    return send


def settled(store, send):
    with store.pool.connection() as c:
        row = c.execute('SELECT await_state,await_subscription_id,await_reason FROM outgoing_messages WHERE id=%s',
                        (send,)).fetchone()
    return row['await_state'], row['await_subscription_id'], row['await_reason']


def subscriptions(store):
    with store.pool.connection() as c:
        return c.execute('SELECT id FROM subscriptions').fetchall()


@pytest.fixture()
def wired(gate, policy):  # noqa: F811
    store = gate
    token = identity(store)
    endpoint = register(store, token)['endpoint_id']
    with store.pool.connection() as c:
        c.execute("UPDATE delivery_endpoints SET state='enabled' WHERE id=%s", (endpoint,))
    return store, token, endpoint


# spec: ANYM-986
def test_a_sent_row_gets_one_reply_subscription_whose_silence_fires_at_the_stated_time_and_a_rerun_adds_none(wired):
    store, token, _ = wired
    send = asked_row(store, token, within=60)
    assert await_reply.process(store) == {'created': 1}
    state, sid, reason = settled(store, send)
    assert state == 'created' and reason is None and len(subscriptions(store)) == 1
    silence = store.get_subscription(token, sid)['delivery']['silence']
    assert silence['state'] == 'pending' and silence['send_id'] == send
    assert await_reply.process(store) == {} and len(subscriptions(store)) == 1
    assert await_reply.result_fields(stored(store)[0]) == {'await_reply': {'state': 'created', 'subscription_id': sid}}
    flush_hints(store)
    assert hints(store, sid, 'silence') == []
    shift(store, sid, 61)
    flush_hints(store)
    assert len(hints(store, sid, 'silence')) == 1


# spec: ANYM-986
def test_a_row_that_is_not_sent_yet_waits_and_one_that_never_is_refused_send_not_sent(wired):
    store, token, _ = wired
    queued, unknown, failed = (asked_row(store, token, status=name) for name in ('queued', 'unknown', 'failed'))
    assert await_reply.process(store) == {'refused': 1}
    assert settled(store, queued)[0] == 'pending' and settled(store, unknown)[0] == 'pending'
    assert settled(store, failed) == ('refused', None, 'send_not_sent') and subscriptions(store) == []


# spec: ANYM-986
def test_no_endpoint_two_endpoints_and_a_full_key_each_end_refused_with_their_code(wired, monkeypatch):
    store, token, endpoint = wired
    full, extra = asked_row(store, token), None
    monkeypatch.setenv('SUBSCRIPTION_MAX_DEFINITIONS', '1')
    store.create_subscription(token, ['telegram-demo-chat'])
    await_reply.process(store)
    assert settled(store, full) == ('refused', None, 'subscription_limit')
    monkeypatch.delenv('SUBSCRIPTION_MAX_DEFINITIONS')
    age(store)
    second = register(store, token, request_id='endpoint-request-2')['endpoint_id']
    with store.pool.connection() as c:
        c.execute("UPDATE delivery_endpoints SET state='enabled' WHERE id=%s", (second,))
    extra = asked_row(store, token)
    await_reply.process(store)
    assert settled(store, extra) == ('refused', None, 'ambiguous_delivery_endpoint')
    with store.pool.connection() as c:
        c.execute("UPDATE delivery_endpoints SET state='operator_disabled'")
    none = asked_row(store, token)
    await_reply.process(store)
    assert settled(store, none) == ('refused', None, 'no_delivery_endpoint') and len(subscriptions(store)) == 1


# spec: ANYM-986
def test_a_revoked_key_and_a_changed_grant_end_refused_and_create_nothing(wired):
    store, token, _ = wired
    changed = asked_row(store, token)
    with store.authorized(token) as (_, p):
        credential = str(p['key_id'])
    store.set_key_permissions(credential, ['read', 'subscription-write'])
    await_reply.process(store)
    assert settled(store, changed) == ('refused', None, 'permission_changed')
    revoked = asked_row(store, token)
    store.revoke(credential_id=credential)
    await_reply.process(store)
    assert settled(store, revoked) == ('refused', None, 'key_inactive') and subscriptions(store) == []


# spec: ANYM-986
def test_a_job_that_raises_leaves_the_send_sent_and_the_row_pending_and_counts_it(wired, monkeypatch):
    store, token, _ = wired
    send = asked_row(store, token)

    def broken(*args):
        raise RuntimeError('synthetic')

    monkeypatch.setattr(await_reply, '_subscribe', broken)
    before = await_reply.FAILURES['await_reply_settle']
    assert await_reply.process(store) == {}
    assert await_reply.FAILURES['await_reply_settle'] == before + 1
    with store.pool.connection() as c:
        assert c.execute('SELECT status FROM outgoing_messages WHERE id=%s', (send,)).fetchone()['status'] == 'sent'
    assert settled(store, send)[0] == 'pending' and subscriptions(store) == []


# spec: ANYM-986
def test_the_python_client_forwards_await_reply_and_omits_it_when_absent():
    seen = []

    def transport(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'status': 'queued'})
    send = dict(account='synthetic', chat_id='chat_synthetic', text='Confirmed')
    with Client('https://archive.example', 'synthetic-token', transport=httpx.MockTransport(transport)) as client:
        client.preview_message(**send, await_reply=SIX_HOURS)
        client.send_message(**send, request_id='synthetic-send-0001', await_reply=SIX_HOURS)
        client.preview_message(**send)
        client.send_message(**send, request_id='synthetic-send-0002')
    assert seen[0]['await_reply'] == SIX_HOURS and seen[1]['await_reply'] == SIX_HOURS
    assert 'await_reply' not in seen[2] and 'await_reply' not in seen[3]
