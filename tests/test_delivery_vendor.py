"""Vendor hosts for self-service endpoints (ANYM-582): Grok Bot routine webhooks, synthetic DNS and transport only."""
import asyncio
import base64
import json

import pytest

from messaging_service.delivery_endpoints import EgressNotSent, finish_inline_test, load_policy, vendor_of
from messaging_service.event_delivery import dispatch_hints
from messaging_service.store import APIError
from messaging_service.subscriptions import match_subscriptions

PUBLIC = lambda *a, **k: ['93.184.216.34']  # noqa: E731
URL = 'https://routine.vendor.test/automations/webhook/synthetic'
GENERIC = 'https://hooks.example.test/deedkit'
SECRET = 'whsec_' + base64.b64encode(b'synthetic-endpoint-secret-0123456789').decode()
BEARER = {'Authorization': 'Bearer synthetic-routine-key-0001'}
PERMISSIONS = ['read', 'subscription-write', 'subscription-ack', 'delivery-write']


def identity(store, name='vendor-agent'):
    return store.issue_key(name, ['telegram-demo'], permissions=PERMISSIONS, key_permissions=PERMISSIONS)


@pytest.fixture()
def policy(tmp_path, monkeypatch):
    def write(**data):
        path = tmp_path / 'delivery-policy.json'
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        monkeypatch.setenv('DELIVERY_POLICY_FILE', str(path))
        return path
    write(allow_hosts=['example.test', 'vendor.test'], vendors={'grok-bot-routine': ['routine.vendor.test']})
    return write


@pytest.fixture()
def gate(seeded):
    store = seeded[0]
    store.set_delivery_features(True)
    yield store
    store.set_delivery_features(False)


def register(store, token, request_id='vendor-request-1', url=URL, headers=BEARER, **kwargs):
    return store.register_delivery_endpoint(token, request_id, url, SECRET, headers=headers, resolver=PUBLIC, **kwargs)


def age(store):
    with store.pool.connection() as c:
        c.execute("UPDATE delivery_endpoints SET created_at=created_at-interval '2 minutes'")
        c.execute("UPDATE delivery_endpoint_tests SET created_at=created_at-interval '2 minutes'")


def bound(store, token, url=URL, headers=BEARER):
    """A registered endpoint whose inline test was delivered, bound to one subscription with one matched event."""
    endpoint = register(store, token, url=url, headers=headers)
    assert dispatch_hints(store, transport=lambda *a, **k: 200, resolver=PUBLIC)['endpoint_tests']['delivered'] == 1
    sid = store.create_subscription(token, ['telegram-demo-chat'],
        delivery={'endpoint_id': endpoint['endpoint_id']})['subscription_id']
    with store.pool.connection() as c:
        store.append_event(c, 'telegram-demo', 'demo-tg-1', origin='live')
    match_subscriptions(store)
    return endpoint, sid


def hint(store):
    with store.pool.connection() as c:
        return c.execute("SELECT state,error_code,attempts FROM subscription_outbox WHERE kind='events'").fetchone()


def due_now(store):
    with store.pool.connection() as c:
        c.execute("UPDATE subscription_outbox SET next_attempt_at=clock_timestamp() WHERE state='pending'")


# spec: ANYM-582
def test_policy_vendors_are_canonical_known_and_never_widen_the_allowlist(gate, policy):
    store = gate
    token = identity(store)
    policy(allow_hosts=['vendor.test'], vendors={'grok-bot-routine': ['Routine.Vendor.TEST.']})
    rules = load_policy()
    assert rules['vendors'] == {'grok-bot-routine': ['routine.vendor.test']}
    assert vendor_of(rules, 'routine.vendor.test') == 'grok-bot-routine'
    assert vendor_of(rules, 'eu.routine.vendor.test') == 'grok-bot-routine'
    assert vendor_of(rules, 'badroutine.vendor.test') is None and vendor_of(rules, 'other.vendor.test') is None
    assert vendor_of(None, 'routine.vendor.test') is None
    # An unknown vendor or a malformed value leaves the whole policy unset, like any unknown key.
    for vendors in ({'surprise-vendor': ['routine.vendor.test']}, {'grok-bot-routine': 'routine.vendor.test'},
            ['routine.vendor.test'], {'grok-bot-routine': ['x' * 300]}):
        policy(allow_hosts=['vendor.test'], vendors=vendors)
        assert load_policy() is None
        with pytest.raises(APIError, match='delivery_policy_unset'):
            register(store, token)
    # A vendor entry never allows a host by itself, and deny still wins over it.
    policy(allow_hosts=['example.test'], vendors={'grok-bot-routine': ['routine.vendor.test']})
    assert vendor_of(load_policy(), 'routine.vendor.test') is None
    with pytest.raises(APIError, match='unsafe_endpoint'):
        register(store, token)
    policy(allow_hosts=['vendor.test'], deny_hosts=['routine.vendor.test'],
        vendors={'grok-bot-routine': ['routine.vendor.test']})
    with pytest.raises(APIError, match='unsafe_endpoint'):
        register(store, token)


# spec: ANYM-582
def test_vendor_host_needs_a_bearer_authorization_header_kept_write_only(gate, policy, caplog, capsys):
    from messaging_service.error_details import payload
    store = gate
    token = identity(store)
    for headers in (None, {'X-Other': 'synthetic'}, {'Authorization': 'Basic c3ludGhldGlj'}, {'authorization': 'Bearer'}):
        with pytest.raises(APIError, match='invalid_header') as refused:
            register(store, token, headers=headers)
        assert payload(refused.value)['header'] == 'Authorization'
    with store.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM delivery_endpoints').fetchone()['n'] == 0
    canary = 'Bearer canary-5d1e-vendor-routine-key'
    responses = [register(store, token, headers={'authorization': canary})]
    endpoint = responses[0]
    assert endpoint['vendor'] == 'grok-bot-routine' and endpoint['header_names'] == ['authorization']
    received = []
    dispatch_hints(store, transport=lambda url, body, headers, **k: received.append(headers) or 200, resolver=PUBLIC)
    assert received[0]['authorization'] == canary
    responses.append(store.list_delivery_endpoints(token))
    assert responses[-1]['items'][0]['vendor'] == 'grok-bot-routine'
    # Replacing the headers cannot drop the vendor's Authorization.
    with pytest.raises(APIError, match='invalid_header') as refused:
        store.update_delivery_endpoint(token, endpoint['endpoint_id'], 1, 'set_headers', headers={})
    responses.append(payload(refused.value))
    responses.append(store.update_delivery_endpoint(token, endpoint['endpoint_id'], 1, 'set_headers',
        headers={'Authorization': canary + '-2'}))
    # A generic host needs no Authorization and shows no vendor.
    age(store)
    generic = register(store, token, request_id='vendor-request-2', url=GENERIC, headers=None)
    assert generic['vendor'] is None
    with store.pool.connection() as c:
        audit = c.execute('SELECT * FROM audit_log').fetchall()
    text = json.dumps([responses, audit], default=str) + caplog.text + ''.join(capsys.readouterr())
    assert canary not in text and 'canary-5d1e' not in text


class _Sent(OSError):
    """A failure after the request was written: the vendor may have started a run."""


# spec: ANYM-582
@pytest.mark.parametrize(('outcome', 'state', 'error', 'resent'), [
    pytest.param(200, 'delivered', None, False, id='accepted'),
    pytest.param(302, 'dead_letter', 'redirect_rejected', False, id='redirect'),
    pytest.param(401, 'dead_letter', 'vendor_refused', False, id='unauthorized'),
    pytest.param(403, 'dead_letter', 'vendor_refused', False, id='forbidden'),
    pytest.param(429, 'pending', 'receiver_status', True, id='rate-limited'),
    pytest.param(500, 'pending', 'receiver_status', True, id='no-run-status'),
    pytest.param(EgressNotSent('connect_failed'), 'pending', 'receiver_unavailable', True, id='not-sent'),
    pytest.param(TimeoutError(), 'dead_letter', 'outcome_unknown', False, id='timeout'),
    pytest.param(_Sent('reset'), 'dead_letter', 'outcome_unknown', False, id='reset'),
    pytest.param(ValueError('invalid_receiver_response'), 'dead_letter', 'outcome_unknown', False, id='bad-response'),
])
def test_vendor_hint_is_never_resent_after_the_first_byte(gate, policy, outcome, state, error, resent):
    store = gate
    token = identity(store)
    bound(store, token)
    posts = []
    def transport(url, body, headers, **kwargs):
        posts.append(json.loads(body)['type'])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    dispatch_hints(store, transport=transport, resolver=PUBLIC)
    assert posts == ['events']
    assert hint(store) == {'state': state, 'error_code': error, 'attempts': 1}
    due_now(store)
    dispatch_hints(store, transport=transport, resolver=PUBLIC)
    assert posts == (['events', 'events'] if resent else ['events'])


# spec: ANYM-582
def test_vendor_hint_from_an_expired_lease_is_unknown_without_a_post(gate, policy):
    store = gate
    token = identity(store)
    bound(store, token)
    with store.pool.connection() as c:
        # A dispatcher that crashed during its attempt: the lease ran out with the attempt reserved.
        c.execute("""UPDATE subscription_outbox SET state='leased',attempts=1,lease_token=gen_random_uuid(),
            lease_until=clock_timestamp()-interval '1 second',admitted_at=clock_timestamp() WHERE kind='events'""")
    dispatch_hints(store, transport=lambda *a, **k: pytest.fail('re-sent to a vendor after a lost lease'),
        resolver=PUBLIC)
    assert hint(store) == {'state': 'dead_letter', 'error_code': 'outcome_unknown', 'attempts': 1}


# spec: ANYM-582
def test_generic_endpoint_keeps_at_least_once_retries(gate, policy):
    store = gate
    token = identity(store)
    bound(store, token, url=GENERIC, headers=None)
    posts = []
    def transport(url, body, headers, **kwargs):
        posts.append(url)
        raise TimeoutError
    dispatch_hints(store, transport=transport, resolver=PUBLIC)
    assert hint(store)['state'] == 'pending'
    due_now(store)
    dispatch_hints(store, transport=transport, resolver=PUBLIC)
    assert posts == [GENERIC, GENERIC]


# spec: ANYM-582
@pytest.mark.parametrize(('outcome', 'state', 'error'), [
    pytest.param(200, 'delivered', None, id='accepted'),
    pytest.param(401, 'failed', 'vendor_refused', id='unauthorized'),
    pytest.param(500, 'failed', 'receiver_status', id='no-run-status'),
    pytest.param(EgressNotSent('tls_failed'), 'failed', 'receiver_unavailable', id='not-sent'),
    pytest.param(TimeoutError(), 'failed', 'outcome_unknown', id='timeout'),
])
def test_vendor_endpoint_tests_use_the_vendor_table(gate, policy, outcome, state, error):
    store = gate
    token = identity(store)
    def transport(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    endpoint = register(store, token)
    dispatch_hints(store, transport=transport, resolver=PUBLIC)
    result = asyncio.run(finish_inline_test(store, token, endpoint, wait_s=0.5))
    assert (result['test']['test_status'], result['test']['error_code']) == (state, error)


# spec: ANYM-582
def test_vendor_test_from_an_expired_lease_fails_unknown_without_a_post(gate, policy):
    store = gate
    token = identity(store)
    register(store, token)
    with store.pool.connection() as c:
        c.execute("""UPDATE delivery_endpoint_tests SET state='leased',lease_token=gen_random_uuid(),
            lease_until=clock_timestamp()-interval '1 second'""")
    dispatch_hints(store, transport=lambda *a, **k: pytest.fail('re-sent a test to a vendor after a lost lease'),
        resolver=PUBLIC)
    with store.pool.connection() as c:
        assert c.execute('SELECT state,error_code FROM delivery_endpoint_tests').fetchone() == \
            {'state': 'failed', 'error_code': 'outcome_unknown'}


def lost_lease(store):
    with store.pool.connection() as c:
        # A dispatcher that crashed during its attempt: the lease ran out with the attempt reserved.
        c.execute("""UPDATE subscription_outbox SET state='leased',attempts=1,lease_token=gen_random_uuid(),
            lease_until=clock_timestamp()-interval '1 second',admitted_at=clock_timestamp() WHERE kind='events'""")


def _no_dns(*args, **kwargs):
    raise OSError('synthetic resolver outage')


# spec: ANYM-582
@pytest.mark.parametrize('recovery', ['dns-outage', 'quiet-hours'])
def test_vendor_lost_lease_stays_unknown_when_dns_fails_or_quiet_hours_hold_it(gate, policy, monkeypatch, recovery):
    import datetime

    from messaging_service import delivery_policy
    store = gate
    token = identity(store)
    bound(store, token)
    lost_lease(store)
    if recovery == 'quiet-hours':
        later = lambda *a, **k: datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)  # noqa: E731
        monkeypatch.setattr(delivery_policy, 'quiet_until', later)
    dispatch_hints(store, transport=lambda *a, **k: pytest.fail('re-sent to a vendor after a lost lease'),
        resolver=_no_dns if recovery == 'dns-outage' else PUBLIC)
    assert hint(store) == {'state': 'dead_letter', 'error_code': 'outcome_unknown', 'attempts': 1}
    due_now(store)
    dispatch_hints(store, transport=lambda *a, **k: pytest.fail('re-sent to a vendor after recovery'), resolver=PUBLIC)
    assert hint(store)['state'] == 'dead_letter'


# spec: ANYM-582
def test_vendor_informational_status_is_unknown_and_never_retried(gate, policy):
    store = gate
    token = identity(store)
    endpoint = register(store, token)
    dispatch_hints(store, transport=lambda *a, **k: 100, resolver=PUBLIC)
    result = asyncio.run(finish_inline_test(store, token, endpoint, wait_s=0.5))
    assert (result['test']['test_status'], result['test']['error_code']) == ('failed', 'outcome_unknown')
    bound(store, identity(store, 'vendor-agent-2'))
    posts = []
    dispatch_hints(store, transport=lambda *a, **k: posts.append(1) or 100, resolver=PUBLIC)
    assert hint(store) == {'state': 'dead_letter', 'error_code': 'outcome_unknown', 'attempts': 1}
    due_now(store)
    dispatch_hints(store, transport=lambda *a, **k: posts.append(1) or 100, resolver=PUBLIC)
    assert posts == [1]


# spec: ANYM-582
def test_vendor_refused_key_auto_pauses_the_endpoint_until_new_headers_and_resume(gate, policy):
    store = gate
    token = identity(store)
    endpoint, sid = bound(store, token)
    dispatch_hints(store, transport=lambda *a, **k: 401, resolver=PUBLIC)
    assert hint(store)['error_code'] == 'vendor_refused'
    view = store.list_delivery_endpoints(token)['items'][0]
    assert view['state'] == 'auto_paused' and view['vendor'] == 'grok-bot-routine'
    view = store.update_delivery_endpoint(token, endpoint['endpoint_id'], view['version'], 'set_headers',
        headers={'Authorization': 'Bearer synthetic-routine-key-0002'})
    view = store.update_delivery_endpoint(token, endpoint['endpoint_id'], view['version'], 'resume')
    assert view['state'] == 'enabled'


# spec: ANYM-582
def test_policy_reload_reports_the_vendor_map_it_loaded(gate, policy):
    store = gate
    assert store.reload_delivery_policy()['vendors'] == {'grok-bot-routine': ['routine.vendor.test']}
    policy(allow_hosts=['vendor.test'])
    assert store.reload_delivery_policy()['vendors'] == {}
