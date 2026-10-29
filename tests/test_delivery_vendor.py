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
def test_vendor_refused_key_dead_letters_under_the_delivery_role_until_new_headers_pass_a_test(gate, policy):
    from contextlib import contextmanager
    from copy import copy
    from pathlib import Path
    from uuid import uuid4

    from psycopg import sql
    store = gate
    token = identity(store)
    endpoint, sid = bound(store, token)
    # The dispatcher runs as the deployed delivery role: the refusal needs no grant it lacks.
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
        dispatch_hints(worker, transport=lambda *a, **k: 401, resolver=PUBLIC)
        assert hint(store) == {'state': 'dead_letter', 'error_code': 'vendor_refused', 'attempts': 1}
        assert store.get_subscription(token, sid)['delivery']['hint_counts'] == {'dead_letter': 1}
        view = store.list_delivery_endpoints(token)['items'][0]
        assert (view['state'], view['vendor']) == ('enabled', 'grok-bot-routine')
        view = store.update_delivery_endpoint(token, endpoint['endpoint_id'], view['version'], 'set_headers',
            headers={'Authorization': 'Bearer synthetic-routine-key-0002'})
        age(store)
        store.test_delivery_endpoint(token, endpoint['endpoint_id'], 'vendor-retest-0001')
        assert dispatch_hints(worker, transport=lambda *a, **k: 200, resolver=PUBLIC)['endpoint_tests']['delivered'] == 1
        assert store.list_delivery_endpoints(token)['items'][0]['health'] == 'ok'
    finally:
        with store.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def quiet(store, token, bind):
    """An endpoint whose own inline test and matched hint no longer count: only the rows a case adds decide."""
    if bind:
        endpoint = bound(store, token)[0]
    else:
        endpoint = register(store, token)
    with store.pool.connection() as c:
        c.execute('''UPDATE delivery_endpoint_tests SET state='cancelled',created_at=created_at-interval '1 day',
            finished_at=clock_timestamp()-interval '1 day' ''')
        c.execute("UPDATE subscription_outbox SET state='cancelled',finished_at=clock_timestamp()-interval '1 day'")
    return endpoint


def finished_rows(store, endpoint_id, items, *, base=None):
    """Synthetic finished hints and tests of one endpoint: (table, state, error, created, finished), the times in
    minutes before one base instant. Returns the base."""
    from datetime import timedelta
    with store.pool.connection() as c:
        base = base or c.execute('SELECT clock_timestamp() AS t').fetchone()['t']
        template = c.execute('''SELECT subscription,subscription_version,through_position FROM subscription_outbox
            WHERE endpoint=%s ORDER BY created_at LIMIT 1''', (endpoint_id,)).fetchone()
        for n, (table, state, error, created, finished) in enumerate(items):
            times = (base - timedelta(minutes=created), base - timedelta(minutes=finished))
            if table == 'hint':
                c.execute('''INSERT INTO subscription_outbox(subscription,subscription_version,endpoint,through_position,
                    kind,request_id,state,error_code,attempts,created_at,finished_at)
                    VALUES(%s,%s,%s,%s,'events',%s,%s,%s,1,%s,%s)''', (template['subscription'],
                    template['subscription_version'], endpoint_id, template['through_position'], f'synthetic-{n}',
                    state, error, *times))
            else:
                status = 401 if error == 'vendor_refused' else 200 if state == 'delivered' else 500
                c.execute('''INSERT INTO delivery_endpoint_tests(endpoint,request_id,state,error_code,http_status,
                    created_at,finished_at) VALUES(%s,%s,%s,%s,%s,%s,%s)''',
                    (endpoint_id, f'synthetic-test-{n:04d}', state, error, status, *times))
    return base


REFUSED_HINT = ('hint', 'dead_letter', 'vendor_refused')
REFUSED_TEST = ('test', 'failed', 'vendor_refused')
DELIVERED_HINT = ('hint', 'delivered', None)
DELIVERED_TEST = ('test', 'delivered', None)


# spec: ANYM-583
@pytest.mark.parametrize(('bind', 'items', 'shown', 'http_status', 'health'), [
    pytest.param(True, [(*REFUSED_HINT, 30, 30), (*DELIVERED_TEST, 10, 10)], None, None, 'ok', id='refusal-then-delivered-test'),
    pytest.param(True, [(*DELIVERED_HINT, 30, 30), (*REFUSED_HINT, 10, 10)], 10, None, 'failing',
        id='delivered-hint-then-refusal'),
    pytest.param(False, [(*REFUSED_TEST, 10, 10)], 10, 401, 'failing', id='refused-test-no-hints'),
    pytest.param(True, [(*DELIVERED_HINT, 20, 15), (*REFUSED_HINT, 30, 10)], 10, None, 'failing',
        id='refusal-created-earlier-finished-later'),
    pytest.param(True, [(*REFUSED_HINT, 20, 15), (*DELIVERED_HINT, 30, 10)], None, None, 'unknown',
        id='delivered-hint-created-earlier-finished-later'),
    pytest.param(True, [(*REFUSED_TEST, 30, 30), ('hint', 'dead_letter', 'outcome_unknown', 20, 20),
        ('hint', 'dead_letter', 'delivery_budget_exhausted', 15, 15), ('hint', 'dead_letter', 'redirect_rejected', 12, 12),
        ('test', 'failed', 'receiver_status', 10, 10)], 30, 401, 'failing', id='only-a-delivery-clears'),
])
def test_endpoint_refusal_is_the_newest_refusal_after_the_newest_delivery_by_finish_time(gate, policy, bind, items,
        shown, http_status, health):
    from datetime import timedelta

    from messaging_service.store import serial
    store = gate
    token = identity(store)
    endpoint = quiet(store, token, bind)
    base = finished_rows(store, endpoint['endpoint_id'], items)
    view = store.list_delivery_endpoints(token)['items'][0]
    assert view['health'] == health
    if shown is None:
        assert view['last_error'] is None
        return
    error = view['last_error']
    assert (error['code'], error['at']) == ('vendor_refused', serial(base - timedelta(minutes=shown)))
    assert error.get('http_status') == http_status and ('http_status' in error) == (http_status is not None)


def templates(text):
    """Each `tool(arguments)` call template of a next_step text as (tool, arguments), its placeholders filled in."""
    import ast
    import re
    filled = {'<version>': '7', '<new version>': '8', '<new key>': 'synthetic-key', '<value>': 'synthetic-value',
        '<new request id>': 'synthetic-request-0001'}
    calls = []
    for tool, arguments in re.findall(r'`(\w+)\((.*?)\)`', text):
        for placeholder, value in filled.items():
            arguments = arguments.replace(placeholder, value)
        assert '<' not in arguments, arguments
        call = ast.parse(f'f({arguments})', mode='eval').body
        assert not call.args
        calls.append((tool, {k.arg: ast.literal_eval(k.value) for k in call.keywords}))
    return calls


def catalog_validate(tool, arguments):
    from pathlib import Path

    import jsonschema
    catalog = json.loads(Path('services/gateway/src/agent_gateway/catalog.json').read_text())
    schema = next(item['inputSchema'] for item in catalog if item['operation'] == tool)
    jsonschema.validate(arguments, schema)


# spec: ANYM-583
@pytest.mark.parametrize(('state', 'actions'), [
    pytest.param('enabled', ['set_headers', 'test'], id='enabled'),
    pytest.param('agent_paused', ['set_headers', 'resume', 'test'], id='agent-paused'),
    pytest.param('auto_paused', ['set_headers', 'resume', 'test'], id='auto-paused'),
    pytest.param('operator_disabled', ['set_headers', 'test'], id='operator-disabled'),
])
def test_refusal_next_step_names_the_cause_header_names_and_valid_calls_for_the_state(state, actions):
    from messaging_service.delivery_endpoints import refusal_step
    endpoint_id = '0b9f2c3e-8a4d-4c1e-9f7a-2d6b5e8c1a30'
    text = refusal_step(endpoint_id, state, ['Authorization', 'X-Routine'])
    assert ('the vendor refused the key with HTTP 401 or 403: the key was rotated or revoked, or the routine was '
        'deleted or lost permission') in text
    assert 'Authorization' in text and 'X-Routine' in text
    assert ('only the operator can re-enable it' in text) == (state == 'operator_disabled')
    calls = templates(text)
    assert [arguments.get('action', 'test') for _, arguments in calls] == actions
    for tool, arguments in calls:
        assert arguments['endpoint_id'] == endpoint_id
        catalog_validate(tool, arguments)
    set_headers = calls[0][1]
    assert sorted(set_headers['headers']) == ['Authorization', 'X-Routine']
    assert set_headers['headers']['Authorization'].startswith('Bearer ')
    if 'resume' in actions:
        assert calls[1][1]['version'] == 8


# spec: ANYM-583
def test_refusal_survives_a_secret_rotation_a_pause_and_a_resume_with_the_same_text(gate, policy):
    from messaging_service.delivery_endpoints import refusal_step
    store = gate
    token = identity(store)
    endpoint, _ = bound(store, token)
    dispatch_hints(store, transport=lambda *a, **k: 401, resolver=PUBLIC)
    assert hint(store)['error_code'] == 'vendor_refused'
    view = store.list_delivery_endpoints(token)['items'][0]
    refused = view['last_error']
    assert refused['next_step'] == refusal_step(endpoint['endpoint_id'], 'enabled', ['Authorization'])
    assert view['health'] == 'failing' and 'http_status' not in refused
    other = 'whsec_' + base64.b64encode(b'synthetic-rotated-secret-abcdefghijk').decode()
    view = store.update_delivery_endpoint(token, endpoint['endpoint_id'], view['version'], 'rotate_secret',
        new_secret=other)
    assert view['last_error'] == refused
    view = store.update_delivery_endpoint(token, endpoint['endpoint_id'], view['version'], 'pause')
    assert view['last_error']['next_step'] == refusal_step(endpoint['endpoint_id'], 'agent_paused', ['Authorization'])
    view = store.update_delivery_endpoint(token, endpoint['endpoint_id'], view['version'], 'resume')
    assert (view['last_error'], view['health']) == (refused, 'failing')


def plan_nodes(plan):
    yield plan
    for child in plan.get('Plans', []):
        yield from plan_nodes(child)


# spec: ANYM-583
def test_endpoint_signal_is_one_statement_with_every_read_on_its_partial_index(gate, policy):
    from messaging_service.delivery_endpoints import SIGNAL
    store = gate
    token = identity(store)
    endpoint = quiet(store, token, True)
    age(store)
    second = register(store, token, request_id='vendor-request-2')
    finished_rows(store, endpoint['endpoint_id'], [(*REFUSED_HINT, 525600, 525600), (*REFUSED_TEST, 525600, 525600)])
    with store.pool.connection() as c:
        # The pool's 5 s statement timeout is for the service; 40,000 synthetic rows may take longer on a busy runner.
        c.execute("SET LOCAL statement_timeout='120s'")
        c.execute('''INSERT INTO delivery_endpoint_tests(endpoint,request_id,state,error_code,http_status,created_at,
            finished_at) SELECT %s,'synthetic-load-'||g,CASE WHEN g%%2=0 THEN 'delivered' ELSE 'failed' END,
            CASE WHEN g%%2=0 THEN NULL ELSE 'receiver_status' END,CASE WHEN g%%2=0 THEN 200 ELSE 500 END,
            clock_timestamp()-make_interval(secs=>g),clock_timestamp()-make_interval(secs=>g)
            FROM generate_series(1,20000) g''', (endpoint['endpoint_id'],))
        c.execute('''INSERT INTO subscription_outbox(subscription,subscription_version,endpoint,through_position,kind,
            request_id,state,error_code,attempts,created_at,finished_at)
            SELECT o.subscription,o.subscription_version,o.endpoint,o.through_position,'events','synthetic-load-'||g,
            CASE WHEN g%%2=0 THEN 'delivered' ELSE 'dead_letter' END,CASE WHEN g%%2=0 THEN NULL ELSE 'outcome_unknown' END,
            1,clock_timestamp()-make_interval(secs=>g),clock_timestamp()-make_interval(secs=>g)
            FROM subscription_outbox o CROSS JOIN generate_series(1,20000) g WHERE o.endpoint=%s AND o.request_id=''
            ''', (endpoint['endpoint_id'],))
    with store.pool.connection() as c:
        c.execute("SET LOCAL statement_timeout='120s'")
        c.execute('ANALYZE delivery_endpoint_tests')
        c.execute('ANALYZE subscription_outbox')
        for endpoint_id in (endpoint['endpoint_id'], second['endpoint_id']):
            plan = c.execute('EXPLAIN (FORMAT JSON) ' + SIGNAL, {'endpoint': endpoint_id}).fetchone()
            nodes = list(plan_nodes(next(iter(plan.values()))[0]['Plan']))
            scans = [(n['Node Type'], n.get('Index Name')) for n in nodes
                if n.get('Relation Name') in ('delivery_endpoint_tests', 'subscription_outbox')]
            assert {kind for kind, _ in scans} <= {'Index Scan', 'Index Only Scan'}, scans
            assert sorted(name for _, name in scans) == sorted((
                'delivery_endpoint_test_latest', 'delivery_endpoint_test_finished', 'delivery_endpoint_test_delivered',
                'delivery_endpoint_test_refused', 'subscription_hint_endpoint_delivered',
                'subscription_hint_endpoint_refused')), scans
            assert not [n for n in nodes if 'Sort' in n['Node Type'] or n['Node Type'] == 'Seq Scan']
    views = {item['endpoint_id']: item for item in store.list_delivery_endpoints(token)['items']}
    assert views[endpoint['endpoint_id']]['last_error'] is None and views[second['endpoint_id']]['last_error'] is None


REFUSAL_INDEXES = ('delivery_endpoint_test_latest', 'delivery_endpoint_test_finished', 'delivery_endpoint_test_delivered',
    'delivery_endpoint_test_refused', 'subscription_hint_endpoint_delivered', 'subscription_hint_endpoint_refused')


def refusal_migration():
    """The refusal index file, the schema before it and each index's definition as the current schema holds it."""
    from pathlib import Path

    from messaging_service import store as store_module
    migrations = sorted(Path(store_module.__file__).parent.joinpath('migrations').glob('[0-9][0-9][0-9]_*.sql'))
    path = next(p for p in migrations if p.name.endswith('_endpoint_refusal_indexes.sql'))
    return path, max(int(p.name[:3]) for p in migrations if p.name < path.name)


def index_definitions(store):
    with store.pool.connection() as c:
        return {row['name']: row['definition'] for row in c.execute('''SELECT indexrelid::regclass::text AS name,
            pg_get_indexdef(indexrelid) AS definition FROM pg_index WHERE indexrelid::regclass::text=ANY(%s)''',
            (list(REFUSAL_INDEXES),))}


# spec: ANYM-583
def test_refusal_index_migration_keeps_concurrent_builds_and_takes_no_writer_lock(database, tmp_path):
    import psycopg
    from test_extend_key import old_schema_store
    path, before = refusal_migration()
    definitions = index_definitions(database)
    assert sorted(definitions) == sorted(REFUSAL_INDEXES)
    with old_schema_store(database, tmp_path, before) as store:
        with psycopg.connect(store.pool.conninfo, autocommit=True) as owner:
            for definition in definitions.values():
                owner.execute(definition.replace('CREATE INDEX ', 'CREATE INDEX CONCURRENTLY ', 1))
            built = {r[0]: r[1] for r in owner.execute('''SELECT indexrelid::regclass::text,indexrelid::oid FROM pg_index
                WHERE indexrelid::regclass::text=ANY(%s)''', (list(REFUSAL_INDEXES),))}
        # A delivery writer holds its row lock through the migration: a CREATE INDEX would wait past lock_timeout.
        with psycopg.connect(store.pool.conninfo) as writer:
            writer.execute('LOCK TABLE subscription_outbox,delivery_endpoint_tests IN ROW EXCLUSIVE MODE')
            store.migrate()
            writer.rollback()
        with store.pool.connection() as c:
            assert int(c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value']) > before
            assert {r['name']: r['oid'] for r in c.execute('''SELECT indexrelid::regclass::text AS name,
                indexrelid::oid AS oid FROM pg_index WHERE indexrelid::regclass::text=ANY(%s)''',
                (list(REFUSAL_INDEXES),))} == built
    assert path.read_text().startswith('-- rollout: manual\n')


# spec: ANYM-583
@pytest.mark.parametrize('shape', ['invalid', 'other-definition', 'table-too-large'])
def test_refusal_index_migration_refuses_an_unusable_index_or_an_inline_build_over_its_bound(database, tmp_path,
        shape):
    import psycopg
    from test_extend_key import old_schema_store
    path, before = refusal_migration()
    with old_schema_store(database, tmp_path, before) as store:
        with store.pool.connection() as c:
            if shape == 'invalid':
                c.execute(index_definitions(database)['delivery_endpoint_test_refused'])
                # What an interrupted CREATE INDEX CONCURRENTLY leaves behind.
                c.execute("UPDATE pg_index SET indisvalid=false WHERE indexrelid='delivery_endpoint_test_refused'::regclass")
            elif shape == 'other-definition':
                c.execute('CREATE INDEX delivery_endpoint_test_refused ON delivery_endpoint_tests(endpoint)')
        if shape == 'table-too-large':
            with store.pool.connection() as c, pytest.raises(psycopg.errors.RaiseException,
                    match='too large to index inside the migration: run CREATE INDEX CONCURRENTLY'):
                c.execute("SET LOCAL deedkit.migration_inline_index_bytes='-1'")
                c.execute(path.read_text())
            return
        with pytest.raises(psycopg.errors.RaiseException, match='delivery_endpoint_test_refused is not a valid index'):
            store.migrate()
        with store.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == \
                str(before)
            c.execute('DROP INDEX delivery_endpoint_test_refused')
        store.migrate()
        assert sorted(index_definitions(store)) == sorted(REFUSAL_INDEXES)


# spec: ANYM-583
def test_refusal_index_migration_measures_a_missing_index_table_only_under_the_build_lock(database, tmp_path):
    import threading
    import time

    import psycopg
    from test_extend_key import old_schema_store
    path, before = refusal_migration()
    # The file's 2 s lock_timeout, widened so a busy test host still lets the writer commit while it waits.
    text = path.read_text().replace("SET LOCAL lock_timeout='2s'", "SET LOCAL lock_timeout='60s'", 1)
    with old_schema_store(database, tmp_path, before) as store:
        store.seed()
        store.issue_key('race-agent', ['telegram-demo'])
        conninfo = store.pool.conninfo
        with store.pool.connection() as c:
            key = c.execute('SELECT id,principal FROM api_keys LIMIT 1').fetchone()
            endpoint = c.execute('''INSERT INTO delivery_endpoints(principal,key_id,request_id,request_hash,url,host)
                VALUES(%s,%s,'race-request-0001',repeat('a',64),'https://hooks.example.test/race','hooks.example.test')
                RETURNING id''', (key['principal'], key['id'])).fetchone()['id']
        outcome = {}

        def migrate():
            with psycopg.connect(conninfo) as m:
                m.execute("SET deedkit.migration_inline_index_bytes='8192'")
                outcome['pid'] = m.info.backend_pid
                try:
                    m.execute(text)
                    outcome['result'] = 'built'
                except psycopg.Error as error:
                    outcome['result'] = str(error)
                m.rollback()
        with psycopg.connect(conninfo) as writer, psycopg.connect(conninfo, autocommit=True) as watch:
            writer.execute('LOCK TABLE delivery_endpoint_tests IN ROW EXCLUSIVE MODE')
            thread = threading.Thread(target=migrate)
            thread.start()
            deadline = time.monotonic() + 30
            while not watch.execute('''SELECT EXISTS(SELECT FROM pg_locks WHERE pid=%s AND NOT granted
                    AND relation='delivery_endpoint_tests'::regclass)''', (outcome.get('pid', 0),)).fetchone()[0]:
                assert time.monotonic() < deadline and thread.is_alive(), outcome
                time.sleep(0.05)
            # The table grows past the bound while the migration waits; a size read before the lock missed it.
            writer.execute('''INSERT INTO delivery_endpoint_tests(endpoint,request_id)
                SELECT %s,'race-test-'||g FROM generate_series(1,400) g''', (endpoint,))
            writer.commit()
            thread.join(60)
    # The first missing index already refuses: no build ran over the bound before the size was read.
    assert outcome['result'].startswith('delivery_endpoint_test_latest is missing and public.delivery_endpoint_tests '
        'is too large to index inside the migration'), outcome


# spec: ANYM-583
def test_endpoint_view_reads_one_state_while_a_test_and_a_hint_finish(gate, policy):
    from messaging_service import delivery_endpoints
    store = gate
    token = identity(store)
    endpoint, _ = bound(store, token)

    def finish_elsewhere():
        # A second connection commits a refused test, then a delivered hint, between the view's start and end.
        with store.pool.connection() as other:
            other.execute('''INSERT INTO delivery_endpoint_tests(endpoint,request_id,state,error_code,http_status,
                finished_at) VALUES(%s,'synthetic-race-0001','failed','vendor_refused',401,clock_timestamp())''',
                (endpoint['endpoint_id'],))
        with store.pool.connection() as other:
            other.execute('''UPDATE subscription_outbox SET state='delivered',finished_at=clock_timestamp()
                WHERE endpoint=%s''', (endpoint['endpoint_id'],))

    class Interleaved:
        def __init__(self, c):
            self.c, self.done = c, False

        def execute(self, *args):
            cursor = self.c.execute(*args)
            if not self.done:
                self.done = True
                finish_elsewhere()
            return cursor

    def state(view):
        return view['last_test']['test_id'], view['health'], view['last_error']

    with store.pool.connection() as c:
        row = c.execute('SELECT ' + delivery_endpoints.COLUMNS + ' FROM delivery_endpoints WHERE id=%s',
            (endpoint['endpoint_id'],)).fetchone()
        before = state(delivery_endpoints._view(c, row))
        during = state(delivery_endpoints._view(Interleaved(c), row))
        after = state(delivery_endpoints._view(c, row))
    assert before != after and during in (before, after)


# spec: ANYM-583
def test_refusal_signal_carries_no_header_value_url_path_or_response_body(gate, policy):
    store = gate
    token = identity(store)
    key, extra, body = 'Bearer sentinel-key-7f3a9c', 'sentinel-header-value-41d2', 'sentinel-response-body-93be'
    url = 'https://routine.vendor.test/automations/webhook/sentinel-path-5c1e'
    headers = {'Authorization': key, 'X-Routine': extra}
    endpoint = register(store, token, url=url, headers=headers)
    dispatch_hints(store, transport=lambda *a, **k: 401, resolver=PUBLIC)
    registered = asyncio.run(finish_inline_test(store, token, endpoint, wait_s=0.5))
    sid = store.create_subscription(token, ['telegram-demo-chat'],
        delivery={'endpoint_id': endpoint['endpoint_id']})['subscription_id']
    with store.pool.connection() as c:
        store.append_event(c, 'telegram-demo', 'demo-tg-1', origin='live')
    match_subscriptions(store)

    def receiver(*args, **kwargs):
        raise TimeoutError(body)
    dispatch_hints(store, transport=receiver, resolver=PUBLIC)
    assert hint(store)['error_code'] == 'outcome_unknown' and sid
    listed = store.list_delivery_endpoints(token)['items'][0]
    updated = store.update_delivery_endpoint(token, endpoint['endpoint_id'], listed['version'], 'pause')
    admin = store.list_delivery_endpoints_admin()['items'][0]
    views = [registered, listed, updated, admin]
    for view in views:
        assert view['last_error']['code'] == 'vendor_refused'
        text = json.dumps(view)
        assert key not in text and 'sentinel-key' not in text and extra not in text and body not in text
        signal = json.dumps(view['last_error'])
        assert 'sentinel-path' not in signal and 'routine.vendor.test' not in signal
        assert set(view['last_error']) == {'code', 'at', 'http_status', 'next_step'}


# spec: ANYM-582
def test_policy_reload_reports_the_vendor_map_it_loaded(gate, policy):
    store = gate
    assert store.reload_delivery_policy()['vendors'] == {'grok-bot-routine': ['routine.vendor.test']}
    policy(allow_hosts=['vendor.test'])
    assert store.reload_delivery_policy()['vendors'] == {}


# spec: ANYM-604
def test_vendor_rate_limit_retry_waits_for_its_retry_after(gate, policy):
    store = gate
    token = identity(store)
    bound(store, token)
    dispatch_hints(store, transport=lambda *a, **k: (429, 900), resolver=PUBLIC)
    assert hint(store) == {'state': 'pending', 'error_code': 'receiver_status', 'attempts': 1}
    with store.pool.connection() as c:
        seconds = c.execute("""SELECT extract(epoch FROM next_attempt_at-clock_timestamp())::float AS s
            FROM subscription_outbox WHERE kind='events'""").fetchone()['s']
    assert 870 < seconds <= 900
