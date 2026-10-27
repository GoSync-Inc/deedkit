"""Agent self-registered webhook endpoints (ANYM-519), synthetic data and injected DNS/transport only."""
import asyncio
import base64
import hashlib
import hmac
import json
import threading

import pytest

from messaging_service.delivery_endpoints import canonical_host, finish_inline_test, host_allowed, sign
from messaging_service.event_delivery import dispatch_hints
from messaging_service.store import APIError
from messaging_service.subscriptions import match_subscriptions

PUBLIC = lambda *a, **k: ['93.184.216.34']  # noqa: E731
URL = 'https://hooks.example.test/deedkit'
SECRET = 'whsec_' + base64.b64encode(b'synthetic-endpoint-secret-0123456789').decode()
OTHER_SECRET = 'whsec_' + base64.b64encode(b'synthetic-rotated-secret-abcdefghijk').decode()
PERMISSIONS = ['read', 'subscription-write', 'subscription-ack', 'delivery-write']


def identity(store, name='endpoint-agent', permissions=PERMISSIONS):
    return store.issue_key(name, ['telegram-demo'], permissions=permissions, key_permissions=permissions)


@pytest.fixture()
def policy(tmp_path, monkeypatch):
    def write(**data):
        path = tmp_path / 'delivery-policy.json'
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        monkeypatch.setenv('DELIVERY_POLICY_FILE', str(path))
        return path
    write(allow_hosts=['example.test'])
    return write


@pytest.fixture()
def gate(seeded):
    store = seeded[0]
    store.set_delivery_features(True)
    yield store
    store.set_delivery_features(False)


def register(store, token, request_id='endpoint-request-1', url=URL, secret=SECRET, **kwargs):
    return store.register_delivery_endpoint(token, request_id, url, secret, resolver=kwargs.pop('resolver', PUBLIC),
        **kwargs)


def age(store, minutes=2):
    # Registration and tests are 1/min per key; move earlier rows out of that window.
    with store.pool.connection() as c:
        c.execute("UPDATE delivery_endpoints SET created_at=created_at-make_interval(mins=>%s) ",
            (minutes,))
        c.execute("UPDATE delivery_endpoint_tests SET created_at=created_at-make_interval(mins=>%s) ",
            (minutes,))


def verified(secret, headers, body):
    """A receiver's Standard Webhooks check, independent of the implementation."""
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
    expected = 'v1,' + base64.b64encode(hmac.new(base64.b64decode(secret[6:]), signed, hashlib.sha256).digest()).decode()
    return expected in headers['webhook-signature'].split(' ')


def emit(store, count=1):
    with store.pool.connection() as c:
        return [store.append_event(c, 'telegram-demo', 'demo-tg-1', origin='live') for _ in range(count)]


# spec: ANYM-519
def test_publishes_standard_webhooks_vector_and_a_fresh_one():
    secret = 'whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw'
    assert sign(secret, 'msg_p5jXN8AQM9LWM0D4loKWxJek', 1614265330, b'{"test": 2432232314}') == \
        'v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE='
    body = b'{"type":"test"}'
    headers = {'webhook-id': 'synthetic-id', 'webhook-timestamp': '1700000000',
        'webhook-signature': sign(SECRET, 'synthetic-id', 1700000000, body)}
    assert verified(SECRET, headers, body) and not verified(OTHER_SECRET, headers, body)


# spec: ANYM-519
def test_tools_need_read_and_delivery_write_and_the_exact_key(gate, policy):
    store = gate
    legacy = store.issue_key('endpoint-legacy', ['telegram-demo'])
    with pytest.raises(APIError, match='forbidden'):
        store.list_delivery_endpoints(legacy)
    # A principal may hold delivery-write; a key ceiling without it still refuses.
    narrow = store.issue_key('endpoint-agent', ['telegram-demo'], permissions=PERMISSIONS,
        key_permissions=['read', 'subscription-write'])
    with pytest.raises(APIError, match='forbidden'):
        register(store, narrow)
    no_read = identity(store, 'endpoint-no-read', ['delivery-write'])
    with pytest.raises(APIError, match='forbidden'):
        store.list_delivery_endpoints(no_read)
    token = identity(store)
    created = register(store, token)
    other = identity(store)  # another key of the same principal
    assert store.list_delivery_endpoints(other)['items'] == []
    for call in (lambda: store.update_delivery_endpoint(other, created['endpoint_id'], 1, 'pause'),
            lambda: store.delete_delivery_endpoint(other, created['endpoint_id'], 1),
            lambda: store.test_delivery_endpoint(other, created['endpoint_id'], 'endpoint-test-1')):
        with pytest.raises(APIError, match='not_found'):
            call()
    listed = store.list_delivery_endpoints(token)
    assert [item['endpoint_id'] for item in listed['items']] == [created['endpoint_id']]
    assert listed['max_endpoints'] == 4 and listed['has_more'] is False
    # Endpoints do not carry over to a reissued key (ANYM-495): a new key starts empty.
    assert store.list_delivery_endpoints(identity(store))['items'] == []


# spec: ANYM-519
def test_gate_off_refuses_every_tool_the_delivery_field_and_staging(seeded, policy):
    store = seeded[0]
    token = identity(store)
    for call in (lambda: register(store, token), lambda: store.list_delivery_endpoints(token),
            lambda: store.update_delivery_endpoint(token, '00000000-0000-4000-8000-000000000000', 1, 'pause'),
            lambda: store.delete_delivery_endpoint(token, '00000000-0000-4000-8000-000000000000', 1),
            lambda: store.test_delivery_endpoint(token, '00000000-0000-4000-8000-000000000000', 'endpoint-test-1'),
            lambda: store.create_subscription(token, ['telegram-demo-chat'],
                delivery={'endpoint_id': '00000000-0000-4000-8000-000000000000'})):
        with pytest.raises(APIError, match='delivery_disabled'):
            call()
    # Registered while on, then switched off: nothing stages and the queued test is cancelled.
    store.set_delivery_features(True)
    try:
        endpoint = register(store, token)
        sid = store.create_subscription(token, ['telegram-demo-chat'],
            delivery={'endpoint_id': endpoint['endpoint_id']})['subscription_id']
    finally:
        assert store.set_delivery_features(False)['cancelled'] == 1
    with store.pool.connection() as c:
        assert c.execute('SELECT state,error_code FROM delivery_endpoint_tests').fetchone() == \
            {'state': 'cancelled', 'error_code': 'delivery_disabled'}
    with pytest.raises(APIError, match='delivery_disabled'):
        store.configure_subscription_delivery(token, sid, 2, None, None, endpoint['endpoint_id'])
    emit(store)
    match_subscriptions(store)
    with store.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM subscription_outbox').fetchone()['n'] == 0


# spec: ANYM-519
def test_operator_policy_canonical_hosts_label_boundaries_and_deny_wins(gate, policy):
    store = gate
    assert canonical_host('Hooks.Example.TEST.') == 'hooks.example.test'
    assert canonical_host('BÜCHER.example') == 'xn--bcher-kva.example'
    rules = {'allow': ['example.com'], 'deny': ['bad.example.com'], 'allow_any_public': False}
    assert host_allowed(rules, 'example.com') and host_allowed(rules, 'a.example.com')
    assert not host_allowed(rules, 'badexample.com') and not host_allowed(rules, 'x.bad.example.com')
    assert host_allowed({**rules, 'allow': [], 'allow_any_public': True}, 'any.public.test')
    assert not host_allowed({**rules, 'allow_any_public': True}, 'bad.example.com')
    token = identity(store)
    for data in ({'allow_hosts': []}, {'allow_hosts': ['example.test'], 'surprise': True}, {'allow_any_public': 'yes'}):
        policy(**data)
        with pytest.raises(APIError, match='delivery_policy_unset'):
            register(store, token)
    path = policy(allow_hosts=['example.test'])
    path.chmod(0o644)
    with pytest.raises(APIError, match='delivery_policy_unset'):
        register(store, token)
    policy(allow_hosts=['example.test'], deny_hosts=['hooks.example.test'])
    with pytest.raises(APIError, match='unsafe_endpoint') as refused:
        register(store, token)
    assert refused.value.field_errors == [{'field': 'url', 'reason': 'format',
        'expected': 'https on port 443, a public address, a host the operator allows'}]
    policy(allow_hosts=[], allow_any_public=True)
    assert register(store, token, url='https://elsewhere.public.test/hook')['host'] == 'elsewhere.public.test'


# spec: ANYM-519
def test_private_answers_name_the_relay_and_rebinding_is_refused_at_attempt(gate, policy):
    from messaging_service.error_details import payload
    store = gate
    token = identity(store)
    for addresses in (['10.0.0.7'], ['100.100.1.1'], ['127.0.0.1'], ['93.184.216.34', 'fd7a:115c:a1e0::1']):
        with pytest.raises(APIError, match='unsafe_endpoint') as refused:
            register(store, token, resolver=lambda *a, answers=addresses, **k: answers)
        assert 'wake relay (ANYM-228)' in payload(refused.value)['next_step']
    with pytest.raises(APIError, match='unsafe_endpoint') as refused:
        register(store, token, resolver=lambda *a, **k: ['ff0e::1'])
    assert not hasattr(refused.value, 'next_step')
    policy(allow_hosts=['example.test', 'ts.net'])
    with pytest.raises(APIError, match='unsafe_endpoint') as refused:
        register(store, token, url='https://box.tail1234.ts.net/hook')
    assert refused.value.next_step.startswith('for a tailnet or private host')
    # Public at registration, private at the attempt: no POST, the test fails.
    endpoint = register(store, token)
    sends = []
    dispatch_hints(store, transport=lambda *a, **k: sends.append(a) or 204, resolver=lambda *a, **k: ['10.0.0.7'])
    assert sends == []
    with store.pool.connection() as c:
        assert c.execute('SELECT state,error_code FROM delivery_endpoint_tests').fetchone() == \
            {'state': 'failed', 'error_code': 'unsafe_endpoint'}
    assert store.list_delivery_endpoints(token)['items'][0]['health'] == 'failing'
    # The same change for an events hint dead-letters it without a POST.
    age(store)
    sid = store.create_subscription(token, ['telegram-demo-chat'],
        delivery={'endpoint_id': endpoint['endpoint_id']})['subscription_id']
    emit(store)
    match_subscriptions(store)
    assert dispatch_hints(store, transport=lambda *a, **k: sends.append(a) or 204,
        resolver=lambda *a, **k: ['64:ff9b::a9fe:a9fe'])['dead_letter'] == 1
    assert sends == [] and store.get_subscription(token, sid)['delivery']['hint_counts'] == {'dead_letter': 1}


# spec: ANYM-519
def test_headers_are_bounded_reserved_and_write_only_with_a_canary(gate, policy, caplog, capsys, monkeypatch):
    from messaging_service import telemetry
    from messaging_service.error_details import payload
    store = gate
    token = identity(store)
    for name in ('Host', 'content-type', 'Content-Length', 'Transfer-Encoding', 'Connection', 'Upgrade', 'TE',
            'Trailer', 'Cookie', 'webhook-id', 'Webhook-Anything', 'X-DeedKit-Hint-ID', 'MCP-Session-Id'):
        with pytest.raises(APIError, match='reserved_header') as refused:
            register(store, token, headers={name: 'synthetic'})
        assert payload(refused.value)['header'] == name
    for headers in ({'Bad Name': 'x'}, {'X-Auth': 'a\r\nInjected: 1'}, {'X-Auth': ''}, {'X-Auth': 'x' * 1025},
            {'X-Auth': 'é'}, {f'X-{n}': 'v' for n in range(5)}, {'X-Auth': 'a', 'x-auth': 'b'}):
        with pytest.raises(APIError, match='invalid_header'):
            register(store, token, headers=headers)
    canary = 'Bearer canary-7f3a9c1e-header-value'
    seen = []
    monkeypatch.setattr(telemetry, 'capture', lambda error, code: seen.append((repr(error), code)))
    responses = [register(store, token, headers={'Authorization': canary, 'X-Receiver': 'second-canary-value'})]
    endpoint = responses[0]
    assert endpoint['header_names'] == ['Authorization', 'X-Receiver']
    assert len(endpoint['headers_fingerprint']) == 8 and len(endpoint['secret_fingerprint']) == 8
    received = []
    def transport(url, body, headers, **kwargs):
        received.append(headers)
        return 204
    dispatch_hints(store, transport=transport, resolver=PUBLIC)
    assert received[0]['Authorization'] == canary
    responses.append(store.list_delivery_endpoints(token))
    responses.append(store.update_delivery_endpoint(token, endpoint['endpoint_id'], 1, 'rotate_secret',
        new_secret=OTHER_SECRET))
    responses.append(store.update_delivery_endpoint(token, endpoint['endpoint_id'], 2, 'set_headers',
        headers={'Authorization': canary + '-2'}))
    for call in (lambda: store.update_delivery_endpoint(token, endpoint['endpoint_id'], 3, 'rotate_secret',
            new_secret=OTHER_SECRET), lambda: register(store, token, headers={'Authorization': canary})):
        with pytest.raises(APIError) as refused:
            call()
        responses.append(payload(refused.value))
    age(store)
    responses.append(store.test_delivery_endpoint(token, endpoint['endpoint_id'], 'endpoint-test-2'))
    responses.append(store.capabilities(token))
    responses.append(store.list_delivery_endpoints_admin())
    responses.append(store.delete_delivery_endpoint(token, endpoint['endpoint_id'], 3))
    with store.pool.connection() as c:
        audit = c.execute('SELECT * FROM audit_log').fetchall()
        stored = c.execute('SELECT signing_secret,pending_secret,header_values FROM delivery_endpoints').fetchone()
    assert stored == {'signing_secret': None, 'pending_secret': None, 'header_values': None}
    text = json.dumps([responses, audit, seen], default=str) + caplog.text + ''.join(capsys.readouterr())
    for value in (canary, 'second-canary-value', SECRET, SECRET[6:], OTHER_SECRET, OTHER_SECRET[6:]):
        assert value not in text
    assert {row['operation'] for row in audit} >= {'register_delivery_endpoint', 'update_delivery_endpoint',
        'test_delivery_endpoint', 'delete_delivery_endpoint', 'list_delivery_endpoints'}


# spec: ANYM-519
def test_caller_secret_idempotent_retry_and_rotation_window(gate, policy):
    store = gate
    token = identity(store)
    for secret in ('whsec_' + base64.b64encode(b'x' * 23).decode(), 'whsec_' + base64.b64encode(b'x' * 65).decode(),
            base64.b64encode(b'x' * 32).decode(), 'whsec_not*base64', None):
        with pytest.raises(APIError, match='invalid_signing_secret'):
            register(store, token, secret=secret)
    first = register(store, token, label='Synthetic hook', headers={'X-Auth': 'synthetic-auth'})
    # The lost response is recovered by the same request, before any rate limit.
    again = register(store, token, label='Synthetic hook', headers={'X-Auth': 'synthetic-auth'})
    assert again['endpoint_id'] == first['endpoint_id'] and again['test']['test_id'] == first['test']['test_id']
    for changed in ({'label': 'Other'}, {'headers': {'X-Auth': 'other-auth'}}, {'secret': OTHER_SECRET}):
        with pytest.raises(APIError, match='idempotency_conflict'):
            register(store, token, **{'label': 'Synthetic hook', 'headers': {'X-Auth': 'synthetic-auth'}, **changed})
    with pytest.raises(APIError, match='invalid_parameters'):
        store.update_delivery_endpoint(token, first['endpoint_id'], 1, 'rotate_secret')
    rotated = store.update_delivery_endpoint(token, first['endpoint_id'], 1, 'rotate_secret', new_secret=OTHER_SECRET)
    assert rotated['rotation']['pending_fingerprint'] != rotated['secret_fingerprint']
    with pytest.raises(APIError, match='rotation_in_progress') as busy:
        store.update_delivery_endpoint(token, first['endpoint_id'], 2, 'rotate_secret', new_secret=SECRET)
    assert 86000 < busy.value.retry_after_s <= 86400
    received = []
    def transport(url, body, headers, **kwargs):
        received.append((headers, body))
        return 204
    dispatch_hints(store, transport=transport, resolver=PUBLIC)
    headers, body = received[-1]
    assert len(headers['webhook-signature'].split(' ')) == 2
    assert verified(SECRET, headers, body) and verified(OTHER_SECRET, headers, body)
    # After 24 h the dispatcher promotes the pending secret by itself.
    with store.pool.connection() as c:
        c.execute("UPDATE delivery_endpoints SET rotation_started_at=clock_timestamp()-interval '25 hours' WHERE id=%s",
            (first['endpoint_id'],))
    age(store)
    store.test_delivery_endpoint(token, first['endpoint_id'], 'endpoint-test-3')
    dispatch_hints(store, transport=transport, resolver=PUBLIC)
    headers, body = received[-1]
    assert ' ' not in headers['webhook-signature'] and verified(OTHER_SECRET, headers, body)
    view = store.list_delivery_endpoints(token)['items'][0]
    assert view['rotation'] is None and view['secret_fingerprint'] == rotated['rotation']['pending_fingerprint']


# spec: ANYM-519
def test_states_operator_disable_and_configuring_a_paused_endpoint(gate, policy):
    store = gate
    token = identity(store)
    endpoint = register(store, token)
    eid = endpoint['endpoint_id']
    sid = store.create_subscription(token, ['telegram-demo-chat'])['subscription_id']
    paused = store.update_delivery_endpoint(token, eid, 1, 'pause')
    assert paused['state'] == 'agent_paused' and paused['version'] == 2
    with pytest.raises(APIError, match='endpoint_conflict'):
        store.update_delivery_endpoint(token, eid, 1, 'resume')
    with pytest.raises(APIError, match='endpoint_paused') as refused:
        store.configure_subscription_delivery(token, sid, 1, None, None, eid)
    assert refused.value.state == 'agent_paused' and 'resume' in refused.value.next_step
    with pytest.raises(APIError, match='invalid_parameters'):
        store.configure_subscription_delivery(token, sid, 1, '00000000-0000-4000-8000-000000000000', None, eid)
    assert store.update_delivery_endpoint(token, eid, 2, 'resume')['state'] == 'enabled'
    with pytest.raises(APIError, match='invalid_endpoint_state'):
        store.update_delivery_endpoint(token, eid, 3, 'resume')
    assert store.set_delivery_endpoint_operator_state(eid, False)['state'] == 'operator_disabled'
    for call in (lambda: store.update_delivery_endpoint(token, eid, 4, 'resume'),
            lambda: store.update_delivery_endpoint(token, eid, 4, 'pause'),
            lambda: store.test_delivery_endpoint(token, eid, 'endpoint-test-4'),
            lambda: store.configure_subscription_delivery(token, sid, 1, None, None, eid)):
        with pytest.raises(APIError, match='operator_disabled') as refused:
            call()
        assert refused.value.state == 'operator_disabled'
    # Only the operator path clears it, and only from operator_disabled.
    assert store.set_delivery_endpoint_operator_state(eid, True)['state'] == 'enabled'
    with pytest.raises(ValueError, match='invalid_endpoint_state'):
        store.set_delivery_endpoint_operator_state(eid, True)
    assert store.list_delivery_endpoints(token)['items'][0]['state'] == 'enabled'
    with store.pool.connection() as c:
        assert c.execute('SELECT state,error_code FROM delivery_endpoint_tests').fetchone() == \
            {'state': 'cancelled', 'error_code': 'endpoint_paused'}


# spec: ANYM-519
def test_limits_cap_rate_and_the_key_hint_budget(gate, policy, monkeypatch):
    store = gate
    token = identity(store)
    first = register(store, token)
    with pytest.raises(APIError, match='rate_limited') as limited:
        register(store, token, request_id='endpoint-request-2')
    assert 1 <= limited.value.retry_after_s <= 60
    with pytest.raises(APIError, match='rate_limited'):
        store.test_delivery_endpoint(token, first['endpoint_id'], 'endpoint-test-5')
    monkeypatch.setenv('DELIVERY_MAX_ENDPOINTS_PER_KEY', '2')
    age(store)
    # The same URL under another request ID is a separate endpoint and counts toward the cap.
    second = register(store, token, request_id='endpoint-request-2')
    assert second['endpoint_id'] != first['endpoint_id'] and second['url'] == first['url']
    age(store)
    with pytest.raises(APIError, match='endpoint_limit'):
        register(store, token, request_id='endpoint-request-3')
    store.delete_delivery_endpoint(token, second['endpoint_id'], 1)
    assert register(store, token, request_id='endpoint-request-3')['state'] == 'enabled'
    # The per-key hint budget counts endpoint hints.
    monkeypatch.setenv('SUBSCRIPTION_MAX_HINTS_PER_HOUR', '1')
    sid = store.create_subscription(token, ['telegram-demo-chat'],
        delivery={'endpoint_id': first['endpoint_id']})['subscription_id']
    emit(store)
    match_subscriptions(store)
    assert dispatch_hints(store, transport=lambda *a, **k: 204, resolver=PUBLIC)['delivered'] == 1
    emit(store)
    match_subscriptions(store)
    assert store.get_subscription(token, sid)['delivery']['held_reason'] == 'budget'


# spec: ANYM-519
def test_two_keys_share_a_url_each_with_its_own_secret(gate, policy):
    store = gate
    one, two = identity(store, 'endpoint-one'), identity(store, 'endpoint-two')
    register(store, one)
    register(store, two, secret=OTHER_SECRET)
    received = []
    dispatch_hints(store, transport=lambda url, body, headers, **k: received.append((headers, body)) or 204,
        resolver=PUBLIC)
    assert len(received) == 2
    assert sorted((verified(SECRET, h, b), verified(OTHER_SECRET, h, b)) for h, b in received) == \
        [(False, True), (True, False)]


# spec: ANYM-519
def test_deleting_a_bound_endpoint_leaves_the_subscription_pull_only(gate, policy):
    store = gate
    token = identity(store)
    endpoint = register(store, token)
    sid = store.create_subscription(token, ['telegram-demo-chat'],
        delivery={'endpoint_id': endpoint['endpoint_id']})['subscription_id']
    emit(store)
    match_subscriptions(store)
    deleted = store.delete_delivery_endpoint(token, endpoint['endpoint_id'], 1)
    assert deleted == {'endpoint_id': endpoint['endpoint_id'], 'deleted': True, 'unbound_subscriptions': 1}
    delivery = store.get_subscription(token, sid)['delivery']
    assert (delivery['push'], delivery['target'], delivery['reason']) == (False, 'none', 'endpoint_deleted')
    assert delivery['hint_counts'] == {'cancelled': 1}
    emit(store)
    match_subscriptions(store)
    with store.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM subscription_outbox WHERE state<>'cancelled'").fetchone()['n'] == 0
    assert dispatch_hints(store, transport=lambda *a, **k: pytest.fail('sent to a deleted endpoint'),
        resolver=PUBLIC)['delivered'] == 0
    with pytest.raises(APIError, match='not_found'):
        store.update_delivery_endpoint(token, endpoint['endpoint_id'], 2, 'resume')


# spec: ANYM-519
def test_end_to_end_register_test_subscribe_and_receive_a_signed_hint(gate, policy):
    store = gate
    token = identity(store)
    received = []
    def receiver(url, body, headers, *, resolved, timeout):
        assert url == URL and resolved[1] == ['93.184.216.34'] and verified(SECRET, headers, body)
        received.append((headers, json.loads(body), len(body)))
        return 204
    stop = threading.Event()
    def dispatcher():
        while not stop.is_set():
            dispatch_hints(store, transport=receiver, resolver=PUBLIC)
            stop.wait(0.2)
    worker = threading.Thread(target=dispatcher)
    worker.start()
    try:
        result = asyncio.run(finish_inline_test(store, token, register(store, token, label='Synthetic e2e')))
    finally:
        stop.set()
        worker.join(10)
    assert result['test']['test_status'] == 'delivered' and result['test']['http_status'] == 204
    assert result['health'] == 'ok' and result['state'] == 'enabled' and result['test']['latency_ms'] >= 0
    assert received[0][1] == {'type': 'test', 'test_id': result['test']['test_id'], 'endpoint_id': result['endpoint_id']}
    assert received[0][0]['webhook-id'] == result['test']['test_id']
    # The register request ID reads its inline test back.
    assert store.test_delivery_endpoint(token, result['endpoint_id'], 'endpoint-request-1')['test_status'] == 'delivered'
    delivery = {'endpoint_id': result['endpoint_id']}
    first = store.create_subscription(token, ['telegram-demo-chat'], label='first', delivery=delivery)
    second = store.create_subscription(token, ['telegram-demo-chat'], label='second', delivery=delivery)
    assert first['delivery']['push'] and first['delivery']['endpoint_id'] == result['endpoint_id']
    assert first['version'] == 2 and second['delivery']['target'] == 'endpoint'
    events = emit(store, 3)
    match_subscriptions(store)
    received.clear()
    assert dispatch_hints(store, transport=receiver, resolver=PUBLIC)['delivered'] == 2
    with store.pool.connection() as c:
        text = c.execute("SELECT body FROM messages WHERE id='demo-tg-1'").fetchone()['body']
    labels = set()
    for headers, body, size in received:
        assert size <= 4096 and body['type'] == 'events' and len(body['references']) <= 20
        assert [ref['event_id'] for ref in body['references']] == events and body['references_omitted'] == 0
        assert body['next_call'] == {'tool': 'messages_fetch_subscription_events', 'subscription_id': body['subscription_id']}
        assert 'host_turn_verified' not in json.dumps(body) and text not in json.dumps(body)
        assert headers['webhook-id'] == body['hint_id']
        labels.add(body['label'])
    assert labels == {'first', 'second'}


# spec: ANYM-519
def test_failed_inline_test_still_creates_the_endpoint_and_a_slow_one_stays_pending(gate, policy):
    store = gate
    token = identity(store)
    pending = asyncio.run(finish_inline_test(store, token, register(store, token), wait_s=0.5))
    assert pending['test']['test_status'] == 'pending'
    assert pending['next_step'] == {'tool': 'test_delivery_endpoint', 'endpoint_id': pending['endpoint_id'],
        'request_id': 'endpoint-request-1'}
    dispatch_hints(store, transport=lambda *a, **k: 500, resolver=PUBLIC)
    again = store.test_delivery_endpoint(token, pending['endpoint_id'], 'endpoint-request-1')
    assert (again['test_status'], again['http_status'], again['error_code']) == ('failed', 500, 'receiver_status')
    assert again['next_step'] == 'fix the receiver, then call test_delivery_endpoint with a new request_id'
    view = store.list_delivery_endpoints(token)['items'][0]
    assert (view['state'], view['health'], view['last_test']['test_status']) == ('enabled', 'failing', 'failed')
    # The retry needs no new registration request ID: a new test request ID only.
    age(store)
    assert store.test_delivery_endpoint(token, pending['endpoint_id'], 'endpoint-test-6')['test_status'] == 'pending'
    assert dispatch_hints(store, transport=lambda *a, **k: 204, resolver=PUBLIC)['endpoint_tests']['delivered'] == 1
    assert store.list_delivery_endpoints(token)['items'][0]['health'] == 'ok'


# spec: ANYM-519
def test_a_policy_change_cancels_queued_hints_and_fails_tests_without_a_post(gate, policy):
    store = gate
    token = identity(store)
    endpoint = register(store, token)
    sid = store.create_subscription(token, ['telegram-demo-chat'],
        delivery={'endpoint_id': endpoint['endpoint_id']})['subscription_id']
    emit(store)
    match_subscriptions(store)
    policy(allow_hosts=['example.test'], deny_hosts=['hooks.example.test'])
    stats = dispatch_hints(store, transport=lambda *a, **k: pytest.fail('POST outside the operator policy'),
        resolver=PUBLIC)
    assert stats['cancelled'] == 1 and stats['endpoint_tests']['failed'] == 1
    with store.pool.connection() as c:
        assert c.execute('SELECT error_code FROM subscription_outbox WHERE subscription=%s', (sid,)).fetchone() == \
            {'error_code': 'endpoint_policy_refused'}
        assert c.execute('SELECT error_code FROM delivery_endpoint_tests').fetchone() == {'error_code': 'endpoint_policy_refused'}


def waits_on_the_fence(store, started, timeout=5):
    """True once another transaction waits for an advisory lock (the features fence)."""
    import time
    deadline = time.monotonic() + timeout
    with store.pool.connection() as c:
        while not c.execute("SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted").fetchone():
            assert time.monotonic() < deadline and started.is_alive(), 'the disable never waited on the fence'
            time.sleep(0.02)


# spec: ANYM-519
def test_disabling_during_a_registration_waits_for_it_and_cancels_its_test(gate, policy):
    store = gate
    token = identity(store)
    disabled, threads = [], []
    def resolver(*args, **kwargs):
        # The registration already passed the gate: the disable must wait for it, then cancel what it staged.
        if not threads:
            threads.append(threading.Thread(target=lambda: disabled.append(store.set_delivery_features(False))))
            threads[0].start()
            waits_on_the_fence(store, threads[0])
        return ['93.184.216.34']
    try:
        endpoint = register(store, token, resolver=resolver)
    finally:
        for thread in threads:
            thread.join(10)
    assert endpoint['test']['test_status'] == 'pending' and disabled[0]['cancelled'] == 1
    with store.pool.connection() as c:
        assert c.execute('SELECT state,error_code FROM delivery_endpoint_tests').fetchone() == \
            {'state': 'cancelled', 'error_code': 'delivery_disabled'}
    assert dispatch_hints(store, transport=lambda *a, **k: pytest.fail('POST after the disable'),
        resolver=PUBLIC)['endpoint_tests']['delivered'] == 0


# spec: ANYM-519
def test_disabling_during_matcher_staging_cancels_the_staged_hint(gate, policy, monkeypatch):
    from messaging_service import delivery_endpoints
    store = gate
    token = identity(store)
    endpoint = register(store, token)
    sid = store.create_subscription(token, ['telegram-demo-chat'],
        delivery={'endpoint_id': endpoint['endpoint_id']})['subscription_id']
    original, disabled, threads = delivery_endpoints.endpoint_stageable, [], []
    def stageable(c, endpoint_id):
        result = original(c, endpoint_id)
        if not threads:
            threads.append(threading.Thread(target=lambda: disabled.append(store.set_delivery_features(False))))
            threads[0].start()
            waits_on_the_fence(store, threads[0])
        return result
    monkeypatch.setattr(delivery_endpoints, 'endpoint_stageable', stageable)
    emit(store)
    try:
        match_subscriptions(store)
    finally:
        for thread in threads:
            thread.join(10)
    assert threads and disabled
    with store.pool.connection() as c:
        rows = c.execute('SELECT state,error_code FROM subscription_outbox WHERE subscription=%s', (sid,)).fetchall()
    assert rows and all(row == {'state': 'cancelled', 'error_code': 'delivery_disabled'} for row in rows)
    assert dispatch_hints(store, transport=lambda *a, **k: pytest.fail('POST after the disable'),
        resolver=PUBLIC)['delivered'] == 0


# spec: ANYM-519
def test_replaying_a_test_or_registration_request_after_operator_disable_is_refused(gate, policy):
    store = gate
    token = identity(store)
    eid = register(store, token)['endpoint_id']
    age(store)
    assert store.test_delivery_endpoint(token, eid, 'endpoint-test-2')['test_status'] == 'pending'
    store.set_delivery_endpoint_operator_state(eid, False)
    for request_id in ('endpoint-request-1', 'endpoint-test-2', 'endpoint-test-3'):
        with pytest.raises(APIError, match='operator_disabled') as refused:
            store.test_delivery_endpoint(token, eid, request_id)
        assert refused.value.state == 'operator_disabled'


# spec: ANYM-519
def test_before_schema_49_endpoint_tools_answer_delivery_disabled_with_a_boundary_audit(gate, policy, monkeypatch):
    from starlette.testclient import TestClient
    from messaging_service import delivery_endpoints
    from messaging_service.app import create_app
    store = gate
    token = identity(store)
    monkeypatch.setattr(delivery_endpoints, 'schema_ready', lambda c: False)
    eid = '00000000-0000-4000-8000-000000000000'
    for call in (lambda: register(store, token), lambda: store.list_delivery_endpoints(token),
            lambda: store.update_delivery_endpoint(token, eid, 1, 'pause'),
            lambda: store.delete_delivery_endpoint(token, eid, 1),
            lambda: store.test_delivery_endpoint(token, eid, 'endpoint-test-1')):
        with pytest.raises(APIError, match='delivery_disabled') as refused:
            call()
        assert refused.value.audited
    with pytest.raises(APIError, match='unauthorized'):
        store.list_delivery_endpoints('not-a-token')
    auth = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'}
    with TestClient(create_app(store, ['testserver'])) as client:
        rest = client.post('/v1/list_delivery_endpoints', headers=auth, json={})
        rpc = client.post('/mcp', headers=auth, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'list_delivery_endpoints', 'arguments': {}}}).json()['result']
    assert rest.status_code == 409 and rest.json()['error'] == 'delivery_disabled'
    assert rpc['isError'] and 'delivery_disabled' in json.dumps(rpc)
    with store.pool.connection() as c:
        # Schema 48's audit CHECK has none of the endpoint operations: each denial is an http_boundary row for the
        # caller (store, REST and MCP alike), and nothing else is audited for it.
        rows = c.execute("""SELECT operation,outcome,error_code,principal IS NOT NULL AS caller FROM audit_log
            WHERE operation LIKE '%%delivery_endpoint%%' OR operation='http_boundary' ORDER BY occurred_at""").fetchall()
    assert rows == [{'operation': 'http_boundary', 'outcome': 'denied', 'error_code': 'delivery_disabled',
        'caller': True}] * 7


# spec: ANYM-519
def test_unicode_and_a_label_hosts_register_as_one_uts46_host(gate, policy):
    store = gate
    policy(allow_hosts=['xn--fa-hia.example'])
    asked = []
    def resolver(host, port):
        asked.append(host)
        return ['93.184.216.34']
    unicode = register(store, token := identity(store), url='https://faß.example/hook', resolver=resolver)
    a_label = register(store, identity(store, 'endpoint-agent-2'), url='https://xn--fa-hia.example/hook',
        resolver=resolver)
    assert unicode['host'] == a_label['host'] == 'xn--fa-hia.example' and set(asked) == {'xn--fa-hia.example'}
    received = []
    dispatch_hints(store, transport=lambda url, *a, **k: received.append(url) or 204, resolver=PUBLIC)
    assert received and all(url.startswith('https://xn--fa-hia.example/') for url in received)
    age(store)
    with pytest.raises(APIError, match='unsafe_endpoint'):
        register(store, token, 'endpoint-request-2', url='https://agent@xn--fa-hia.example/hook')


# spec: ANYM-519
def test_endpoint_silence_hint_and_recovery_from_auto_paused(gate, policy, monkeypatch):
    from uuid import uuid4
    from messaging_service.delivery_policy import flush_hints
    store = gate
    token = identity(store)
    eid = register(store, token)['endpoint_id']
    with store.pool.connection() as c:
        c.execute("UPDATE delivery_endpoint_tests SET state='cancelled'")
    with store.authorized(token) as (_, p):
        owner = p
    send = 'send_' + uuid4().hex[:12]
    text = 'Synthetic outgoing text for the silence hint'
    with store.pool.connection() as c:
        c.execute('''INSERT INTO outgoing_messages(id,principal,key_id,scope_version,key_permission_version,
            request_id,request_hash,account,chat,provider_chat,body,status,provider_message_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,'telegram-demo','telegram-demo-chat','provider-chat',%s,'sent',
            'native-anchor')''', (send, owner['id'], owner['key_id'], owner['scope_version'],
            owner['key_permission_version'], 'synthetic-' + send, 'a' * 64, text))
    sid = store.create_subscription(token, ['telegram-demo-chat'], label='Silence', reply_to_send=send,
        delivery={'endpoint_id': eid, 'delivery_policy': {'expect_reply_within_s': 60}})['subscription_id']
    with store.pool.connection() as c:
        step = "make_interval(secs=>61)"
        c.execute('UPDATE subscription_hint_windows SET silence_due_at=silence_due_at-' + step +
            ' WHERE subscription=%s', (sid,))
        c.execute('UPDATE subscription_delivery SET silence_deadline=silence_deadline-' + step + ',configured_at='
            'configured_at-' + step + ' WHERE subscription=%s', (sid,))
    flush_hints(store)
    received = []
    def receiver(url, body, headers, **kwargs):
        received.append((headers, body))
        return 204
    assert dispatch_hints(store, transport=receiver, resolver=PUBLIC)['delivered'] == 1
    headers, body = received[0]
    payload = json.loads(body)
    assert verified(SECRET, headers, body) and headers['webhook-id'] == payload['hint_id']
    assert set(payload) == {'type', 'hint_id', 'subscription_id', 'version', 'label', 'send_id', 'next_call'}
    assert (payload['type'], payload['send_id'], payload['label']) == ('silence', send, 'Silence')
    assert payload['next_call'] == {'tool': 'messages_get_subscription', 'subscription_id': sid}
    assert text.encode() not in body and b'native-anchor' not in body and len(body) <= 4096
    # auto_paused (set by S3): refused for binding and tests, the agent may resume it or pause it.
    other = store.create_subscription(token, ['telegram-demo-chat'])['subscription_id']
    with store.pool.connection() as c:
        c.execute("UPDATE delivery_endpoints SET state='auto_paused' WHERE id=%s", (eid,))
    for call in (lambda: store.configure_subscription_delivery(token, other, 1, None, None, eid),
            lambda: store.test_delivery_endpoint(token, eid, 'endpoint-test-2')):
        with pytest.raises(APIError, match='endpoint_auto_paused') as refused:
            call()
        assert refused.value.state == 'auto_paused' and 'resume' in refused.value.next_step
    version = store.list_delivery_endpoints(token)['items'][0]['version']
    assert store.update_delivery_endpoint(token, eid, version, 'resume')['state'] == 'enabled'
    assert store.configure_subscription_delivery(token, other, 1, None, None, eid)['delivery']['target'] == 'endpoint'
    with store.pool.connection() as c:
        c.execute("UPDATE delivery_endpoints SET state='auto_paused' WHERE id=%s", (eid,))
    assert store.update_delivery_endpoint(token, eid, version + 1, 'pause')['state'] == 'agent_paused'
