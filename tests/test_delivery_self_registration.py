"""Self-registration of agent webhook endpoints on any public host (ANYM-585): synthetic DNS and transport only."""
import json
import threading

import pytest

from messaging_service import egress_proxy
from messaging_service.delivery_endpoints import host_allowed, load_policy, vendor_of
from messaging_service.event_delivery import dispatch_hints
from messaging_service.store import APIError
from messaging_service.subscriptions import match_subscriptions
from test_delivery_vendor import BEARER, PUBLIC, SECRET, due_now, hint, identity, lost_lease

SELF = 'https://hooks.agent-chosen.test/wake'
LISTED = 'https://hooks.example.test/wake'
ROUTINE = 'https://api2.cursor.sh/automations/webhook/synthetic-routine-0001'


@pytest.fixture()
def policy(tmp_path, monkeypatch):
    def write(**data):
        path = tmp_path / 'delivery-policy.json'
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        monkeypatch.setenv('DELIVERY_POLICY_FILE', str(path))
        return path
    write(self_registration=True)
    return write


@pytest.fixture()
def gate(seeded):
    store = seeded[0]
    store.set_delivery_features(True)
    yield store
    store.set_delivery_features(False)


def register(store, token, url=SELF, request_id='self-request-1', headers=None, resolver=PUBLIC):
    return store.register_delivery_endpoint(token, request_id, url, SECRET, headers=headers, resolver=resolver)


def subscribed(store, token, url=SELF, headers=None):
    """An endpoint whose inline test was delivered, bound to one subscription."""
    endpoint = register(store, token, url=url, headers=headers)
    assert dispatch_hints(store, transport=lambda *a, **k: 200, resolver=PUBLIC)['endpoint_tests']['delivered'] == 1
    store.create_subscription(token, ['telegram-demo-chat'], delivery={'endpoint_id': endpoint['endpoint_id']})
    return endpoint


def emit(store):
    with store.pool.connection() as c:
        store.append_event(c, 'telegram-demo', 'demo-tg-1', origin='live')
    match_subscriptions(store)


def age(store):
    with store.pool.connection() as c:
        c.execute("UPDATE delivery_endpoints SET created_at=created_at-interval '2 minutes'")
        c.execute("UPDATE delivery_endpoint_tests SET created_at=created_at-interval '2 minutes'")


# spec: ANYM-585
def test_self_registration_switch_parses_strictly_and_admits_unlisted_hosts_unless_denied(policy):
    for data in ({'allow_hosts': []}, {'self_registration': False}, {'self_registration': 'yes'},
            {'self_registration': 1, 'allow_hosts': ['example.test']}):
        policy(**data)
        assert load_policy() is None
    policy(self_registration=True, deny_hosts=['denied.test'])
    loaded = load_policy()
    assert loaded['self_registration'] is True and loaded['allow'] == []
    assert host_allowed(loaded, 'hooks.agent-chosen.test') and not host_allowed(loaded, 'a.denied.test')
    policy(allow_hosts=['example.test'])
    assert load_policy()['self_registration'] is False
    assert not host_allowed(load_policy(), 'hooks.agent-chosen.test')
    # The egress proxy reads the same file: it tunnels a self-registered host only while the switch is on.
    proxy = egress_proxy.Proxy(policy=load_policy)
    with pytest.raises(egress_proxy.Refused, match='policy_refused'):
        proxy.admit('hooks.agent-chosen.test:443')
    policy(self_registration=True, deny_hosts=['denied.test'])
    assert proxy.admit('hooks.agent-chosen.test:443') == 'hooks.agent-chosen.test'
    with pytest.raises(egress_proxy.Refused, match='policy_refused'):
        proxy.admit('a.denied.test:443')


# spec: ANYM-585
def test_agent_registers_an_unlisted_public_host_and_the_existing_limits_still_hold(gate, policy, monkeypatch):
    store = gate
    token = identity(store)
    endpoint = register(store, token)
    assert (endpoint['state'], endpoint['host'], endpoint['vendor'], endpoint['at_most_once']) == \
        ('enabled', 'hooks.agent-chosen.test', None, True)
    assert dispatch_hints(store, transport=lambda *a, **k: 200, resolver=PUBLIC)['endpoint_tests']['delivered'] == 1
    with pytest.raises(APIError, match='rate_limited'):
        register(store, token, url=SELF + '-2', request_id='self-request-2')
    age(store)
    policy(self_registration=True, deny_hosts=['denied.test'])
    with pytest.raises(APIError, match='unsafe_endpoint'):
        register(store, token, url='https://hooks.denied.test/wake', request_id='self-request-2')
    with pytest.raises(APIError, match='unsafe_endpoint') as private:
        register(store, token, url='https://internal.agent-chosen.test/wake', request_id='self-request-2',
            resolver=lambda *a, **k: ['10.0.0.5'])
    assert 'relay' in private.value.next_step
    monkeypatch.setenv('DELIVERY_MAX_ENDPOINTS_PER_KEY', '1')
    with pytest.raises(APIError, match='endpoint_limit'):
        register(store, token, url=SELF + '-2', request_id='self-request-2')


# spec: ANYM-585
def test_kill_switch_stops_self_registered_endpoints_and_keeps_allowlisted_ones(gate, policy):
    store = gate
    policy(allow_hosts=['example.test'], self_registration=True)
    listed, chosen = identity(store, 'listed-agent'), identity(store, 'chosen-agent')
    subscribed(store, listed, url=LISTED)
    own = subscribed(store, chosen)
    emit(store)
    policy(allow_hosts=['example.test'], self_registration=False)
    reloaded = store.reload_delivery_policy()
    assert (reloaded['refused_endpoints'], reloaded['cancelled'], reloaded['self_registration']) == (1, 1, False)
    posts = []
    dispatch_hints(store, transport=lambda url, *a, **k: posts.append(url) or 200, resolver=PUBLIC)
    assert posts == [LISTED]
    with store.pool.connection() as c:
        assert c.execute('''SELECT o.state,o.error_code FROM subscription_outbox o JOIN delivery_endpoints e
            ON e.id=o.endpoint WHERE e.host='hooks.agent-chosen.test' ''').fetchall() == \
            [{'state': 'cancelled', 'error_code': 'endpoint_policy_refused'}]
    # The endpoint is kept, not deleted, and delivers again once the switch is back on.
    assert store.list_delivery_endpoints(chosen)['items'][0]['state'] == 'enabled'
    policy(allow_hosts=['example.test'], self_registration=True)
    store.reload_delivery_policy()
    emit(store)
    posts.clear()
    dispatch_hints(store, transport=lambda url, *a, **k: posts.append(url) or 200, resolver=PUBLIC)
    assert sorted(posts) == sorted([LISTED, own['url']])


# spec: ANYM-585
def test_per_host_budget_spans_keys_and_holds_a_hint_without_spending_an_attempt(gate, policy, monkeypatch):
    store = gate
    monkeypatch.setenv('DELIVERY_MAX_HINTS_PER_HOST_PER_HOUR', '1')
    subscribed(store, identity(store, 'first-agent'), url=SELF)
    subscribed(store, identity(store, 'second-agent'), url=SELF + '-second')
    emit(store)
    stats = dispatch_hints(store, transport=lambda *a, **k: 200, resolver=PUBLIC)
    assert (stats['delivered'], stats['deferred']) == (1, 1)
    with store.pool.connection() as c:
        held = c.execute('''SELECT state,attempts,admitted_at,next_attempt_at>clock_timestamp()+interval '50 minutes'
            AS next_hour FROM subscription_outbox WHERE state<>'delivered' ''').fetchall()
    assert held == [{'state': 'pending', 'attempts': 0, 'admitted_at': None, 'next_hour': True}]
    # Another host is not held by this host's budget.
    subscribed(store, identity(store, 'third-agent'), url='https://other.agent-chosen.test/wake')
    emit(store)
    posts = []
    dispatch_hints(store, transport=lambda url, *a, **k: posts.append(url) or 200, resolver=PUBLIC)
    assert posts == ['https://other.agent-chosen.test/wake']


# spec: ANYM-585
def test_grok_routine_url_shape_is_a_vendor_endpoint_without_a_vendors_entry(gate, policy):
    store = gate
    token = identity(store)
    loaded = load_policy()
    assert vendor_of(loaded, 'api2.cursor.sh', ROUTINE) == 'grok-bot-routine'
    for url in ('https://api2.cursor.sh/automations/other/x', 'https://api2.cursor.sh/automations/webhook/a/b',
            'https://api2.cursor.sh/automations/webhook/', 'https://other.cursor.sh/automations/webhook/x'):
        assert vendor_of(loaded, url.split('/')[2], url) is None
    with pytest.raises(APIError, match='invalid_header'):
        register(store, token, url=ROUTINE)
    endpoint = register(store, token, url=ROUTINE, headers=BEARER)
    assert endpoint['vendor'] == 'grok-bot-routine'
    assert dispatch_hints(store, transport=lambda *a, **k: 200, resolver=PUBLIC)['endpoint_tests']['delivered'] == 1
    store.create_subscription(token, ['telegram-demo-chat'], delivery={'endpoint_id': endpoint['endpoint_id']})
    emit(store)
    posts = []
    def timeout(url, *a, **k):
        posts.append(url)
        raise TimeoutError
    dispatch_hints(store, transport=timeout, resolver=PUBLIC)
    due_now(store)
    dispatch_hints(store, transport=timeout, resolver=PUBLIC)
    assert posts == [ROUTINE] and hint(store) == {'state': 'dead_letter', 'error_code': 'outcome_unknown',
        'attempts': 1}
    # deny_hosts still wins over the shape.
    policy(self_registration=True, deny_hosts=['cursor.sh'])
    assert vendor_of(load_policy(), 'api2.cursor.sh', ROUTINE) is None


# spec: ANYM-585
def test_admin_list_and_reload_show_each_endpoints_admission(gate, policy):
    store = gate
    policy(allow_hosts=['example.test'], self_registration=True)
    listed, chosen = identity(store, 'listed-agent'), identity(store, 'chosen-agent')
    register(store, listed, url=LISTED)
    register(store, chosen)
    def admissions():
        view = store.list_delivery_endpoints_admin()
        return view['self_registration'], {item['host']: item['admission'] for item in view['items']}
    assert admissions() == (True, {'hooks.example.test': 'allow_hosts', 'hooks.agent-chosen.test': 'self_registration'})
    policy(allow_hosts=['example.test'], self_registration=False)
    assert store.reload_delivery_policy()['self_registration'] is False
    assert admissions() == (False, {'hooks.example.test': 'allow_hosts', 'hooks.agent-chosen.test': None})
    policy(allow_hosts=[], allow_any_public=True)
    assert admissions() == (False, {'hooks.example.test': 'allow_hosts', 'hooks.agent-chosen.test': 'allow_hosts'})
    # Agents see neither the switch nor the admission.
    assert 'admission' not in store.list_delivery_endpoints(chosen)['items'][0]


def outbox(store):
    with store.pool.connection() as c:
        return {row['host']: (row['state'], row['error_code'], row['attempts']) for row in c.execute('''SELECT e.host,
            o.state,o.error_code,o.attempts FROM subscription_outbox o JOIN delivery_endpoints e ON e.id=o.endpoint
            WHERE o.kind='events' ''').fetchall()}


# spec: ANYM-585
def test_self_registered_endpoints_are_at_most_once_whatever_the_url(gate, policy):
    store = gate
    policy(allow_hosts=['example.test'], self_registration=True)
    listed = subscribed(store, identity(store, 'listed-agent'), url=LISTED)
    chosen = subscribed(store, identity(store, 'chosen-agent'))
    # The agent sees which rule applies to its endpoint, so a routine URL with `vendor: null` is safe to keep.
    assert (listed['at_most_once'], chosen['at_most_once'], chosen['vendor']) == (False, True, None)
    emit(store)
    posts = []
    def timeout(url, *a, **k):
        posts.append(url)
        raise TimeoutError
    dispatch_hints(store, transport=timeout, resolver=PUBLIC)
    due_now(store)
    dispatch_hints(store, transport=timeout, resolver=PUBLIC)
    # A request that may have arrived is never sent again to a host no operator listed; a listed host retries.
    # Counts, not an exact list: on a slow host the listed hint's 2-second backoff can elapse within one pass.
    assert posts.count(SELF) == 1 and posts.count(LISTED) >= 2
    rows = outbox(store)
    assert rows['hooks.agent-chosen.test'] == ('dead_letter', 'outcome_unknown', 1)
    assert rows['hooks.example.test'][:2] == ('pending', 'receiver_unavailable')


# spec: ANYM-585
def test_self_registered_hint_and_test_from_an_expired_lease_fail_unknown_without_a_post(gate, policy):
    store = gate
    token = identity(store)
    subscribed(store, token)
    emit(store)
    lost_lease(store)
    def never(*a, **k):
        pytest.fail('re-sent to a self-registered endpoint after a lost lease')
    dispatch_hints(store, transport=never, resolver=PUBLIC)
    assert hint(store) == {'state': 'dead_letter', 'error_code': 'outcome_unknown', 'attempts': 1}
    age(store)
    register(store, token, url=SELF + '-2', request_id='self-request-2')
    with store.pool.connection() as c:
        c.execute("""UPDATE delivery_endpoint_tests SET state='leased',lease_token=gen_random_uuid(),
            lease_until=clock_timestamp()-interval '1 second' WHERE state='pending'""")
    dispatch_hints(store, transport=never, resolver=PUBLIC)
    with store.pool.connection() as c:
        assert c.execute('SELECT state,error_code FROM delivery_endpoint_tests ORDER BY created_at DESC').fetchone() == \
            {'state': 'failed', 'error_code': 'outcome_unknown'}


# spec: ANYM-585
def test_concurrent_dispatchers_admit_one_hint_at_a_host_cap_of_one(gate, policy, monkeypatch):
    store = gate
    monkeypatch.setenv('DELIVERY_MAX_HINTS_PER_HOST_PER_HOUR', '1')
    subscribed(store, identity(store, 'first-agent'), url=SELF)
    subscribed(store, identity(store, 'second-agent'), url=SELF + '-second')
    emit(store)
    posts, start = [], threading.Barrier(2)
    def dispatcher():
        start.wait(5)
        dispatch_hints(store, limit=1, transport=lambda url, *a, **k: posts.append(url) or 200, resolver=PUBLIC)
    threads = [threading.Thread(target=dispatcher) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(posts) == 1
    with store.pool.connection() as c:
        assert sorted(row['state'] for row in c.execute(
            "SELECT state FROM subscription_outbox WHERE kind='events'").fetchall()) == ['delivered', 'pending']


# spec: ANYM-585
def test_kill_switch_reload_waits_for_a_post_in_flight_and_no_post_follows(gate, policy):
    store = gate
    subscribed(store, identity(store))
    emit(store)
    entered, release, posts, reloaded = threading.Event(), threading.Event(), [], []
    def slow(url, *a, **k):
        posts.append(url)
        entered.set()
        release.wait(10)
        return 200
    worker = threading.Thread(target=lambda: dispatch_hints(store, transport=slow, resolver=PUBLIC))
    worker.start()
    assert entered.wait(10)
    policy(self_registration=False, allow_hosts=['example.test'])
    switch = threading.Thread(target=lambda: reloaded.append(store.reload_delivery_policy()))
    switch.start()
    switch.join(1)
    # The reload takes the endpoint fence exclusively, so it returns only after the POST in flight has finished.
    assert not reloaded
    release.set()
    worker.join(10)
    switch.join(10)
    assert reloaded and reloaded[0]['refused_endpoints'] == 1
    emit(store)
    dispatch_hints(store, transport=lambda *a, **k: pytest.fail('POST after the kill switch'), resolver=PUBLIC)
    assert posts == [SELF]


def endpoint_test_rows(store):
    with store.pool.connection() as c:
        return [(row['state'], row['error_code']) for row in c.execute(
            'SELECT state,error_code FROM delivery_endpoint_tests ORDER BY created_at').fetchall()]


def held(store, block):
    """Runs block in a thread; returns (started event, release event, thread, results)."""
    entered, release, results = threading.Event(), threading.Event(), []
    def wait(*a, **k):
        entered.set()
        release.wait(10)
    thread = threading.Thread(target=lambda: results.append(block(wait)))
    thread.start()
    return entered, release, thread, results


def reload_while_held(store, policy, entered, release, thread):
    """The kill switch flipped while another transaction holds the fence: the reload waits for it."""
    assert entered.wait(10)
    policy(self_registration=False, allow_hosts=['example.test'])
    reloaded = []
    switch = threading.Thread(target=lambda: reloaded.append(store.reload_delivery_policy()))
    switch.start()
    switch.join(1)
    assert not reloaded
    release.set()
    thread.join(10)
    switch.join(10)
    return reloaded[0]


# spec: ANYM-585
def test_kill_switch_reload_waits_for_a_test_post_and_for_a_registration_in_flight(gate, policy):
    store = gate
    token = identity(store)
    register(store, token)
    def test_post(wait):
        return dispatch_hints(store, transport=lambda *a, **k: wait() or 200, resolver=PUBLIC)
    entered, release, thread, _ = held(store, test_post)
    assert reload_while_held(store, policy, entered, release, thread)['refused_endpoints'] == 1
    assert endpoint_test_rows(store) == [('delivered', None)]
    # A registration that read the switch on commits first; the reload then cancels its staged test.
    policy(self_registration=True)
    store.reload_delivery_policy()
    age(store)
    def registration(wait):
        return register(store, token, url=SELF + '-2', request_id='self-request-2',
            resolver=lambda *a, **k: wait() or ['93.184.216.34'])
    entered, release, thread, registered = held(store, registration)
    reloaded = reload_while_held(store, policy, entered, release, thread)
    assert registered[0]['state'] == 'enabled' and reloaded['cancelled'] == 1
    assert endpoint_test_rows(store)[-1] == ('cancelled', 'endpoint_policy_refused')
    dispatch_hints(store, transport=lambda *a, **k: pytest.fail('POST after the kill switch'), resolver=PUBLIC)


# spec: ANYM-585
def test_generic_self_registered_endpoint_never_resends_after_any_answer(gate, policy):
    store = gate
    policy(allow_hosts=['example.test'], self_registration=True)
    subscribed(store, identity(store, 'listed-agent'), url=LISTED)
    subscribed(store, identity(store, 'chosen-agent'))
    routine_agent = identity(store, 'routine-agent')
    routine = register(store, routine_agent, url=ROUTINE, headers=BEARER)
    assert dispatch_hints(store, transport=lambda *a, **k: 200, resolver=PUBLIC)['endpoint_tests']['delivered'] == 1
    store.create_subscription(routine_agent, ['telegram-demo-chat'], delivery={'endpoint_id': routine['endpoint_id']})
    emit(store)
    posts = []
    # A receiver that acted and then failed: the unknown one never gets it again; the vendor's documented
    # "no run started" status and the operator-listed host still retry.
    dispatch_hints(store, transport=lambda url, *a, **k: posts.append(url) or 500, resolver=PUBLIC)
    due_now(store)
    dispatch_hints(store, transport=lambda url, *a, **k: posts.append(url) or 500, resolver=PUBLIC)
    assert posts.count(SELF) == 1 and posts.count(LISTED) >= 2 and posts.count(ROUTINE) >= 2
    rows = outbox(store)
    assert rows['hooks.agent-chosen.test'] == ('dead_letter', 'receiver_status', 1)
    assert rows['hooks.example.test'][:2] == ('pending', 'receiver_unavailable')
    assert rows['api2.cursor.sh'][:2] == ('pending', 'receiver_status')
    # A request that never left is retried.
    from messaging_service.delivery_endpoints import EgressNotSent, self_registered_result
    assert self_registered_result(failure=EgressNotSent()) == ('retry', 'receiver_unavailable')
    assert self_registered_result(status=503) == ('final', 'receiver_status')


# spec: ANYM-585
@pytest.mark.parametrize('recovery', ['dns-outage', 'over-five-minutes'])
def test_self_registered_test_lease_settles_unknown_before_dns_and_age(gate, policy, recovery):
    store = gate
    register(store, identity(store))
    with store.pool.connection() as c:
        c.execute("""UPDATE delivery_endpoint_tests SET state='leased',lease_token=gen_random_uuid(),
            lease_until=clock_timestamp()-interval '1 second',created_at=created_at-%s*interval '1 minute'
            WHERE state='pending'""", (10 if recovery == 'over-five-minutes' else 0,))
    def no_dns(*a, **k):
        raise OSError('synthetic resolver outage')
    dispatch_hints(store, transport=lambda *a, **k: pytest.fail('re-sent a test after a lost lease'),
        resolver=no_dns if recovery == 'dns-outage' else PUBLIC)
    assert endpoint_test_rows(store) == [('failed', 'outcome_unknown')]


# spec: ANYM-585
def test_allowlisted_endpoints_keep_their_recovery_whether_the_url_is_a_vendor_shape(gate, policy):
    store = gate
    # No switch: the routine host is operator-listed and named by no `vendors` entry, only by its URL shape.
    policy(allow_hosts=['cursor.sh', 'example.test'])
    routine_agent, listed = identity(store, 'routine-agent'), identity(store, 'listed-agent')
    routine = register(store, routine_agent, url=ROUTINE, headers=BEARER)
    plain = register(store, listed, url=LISTED)
    assert (routine['at_most_once'], plain['at_most_once']) == (True, False)
    assert dispatch_hints(store, transport=lambda *a, **k: 200, resolver=PUBLIC)['endpoint_tests']['delivered'] == 2
    for token, endpoint in ((routine_agent, routine), (listed, plain)):
        store.create_subscription(token, ['telegram-demo-chat'], delivery={'endpoint_id': endpoint['endpoint_id']})
    emit(store)
    lost_lease(store)
    posts = []
    dispatch_hints(store, transport=lambda url, *a, **k: posts.append(url) or 200, resolver=PUBLIC)
    # The vendor URL settles unknown with no POST; the generic allowlisted endpoint is delivered again.
    assert posts == [LISTED]
    assert outbox(store) == {'api2.cursor.sh': ('dead_letter', 'outcome_unknown', 1),
        'hooks.example.test': ('delivered', None, 2)}
    # The proxy keeps tunnelling operator-listed hosts with the switch off.
    proxy = egress_proxy.Proxy(policy=load_policy)
    assert proxy.admit('hooks.example.test:443') == 'hooks.example.test'
    with pytest.raises(egress_proxy.Refused, match='policy_refused'):
        proxy.admit('hooks.agent-chosen.test:443')
