"""Module links (ANYM-564): a key sees call events only through a link the owner set (calls:own); synthetic only."""
import json
from uuid import uuid4

from psycopg import sql
import pytest
from starlette.testclient import TestClient

from messaging_service import module_events, module_links
from messaging_service.app import create_app
from messaging_service.event_delivery import dispatch_hints
from messaging_service.module_events import validate_event
from messaging_service.store import APIError
from messaging_service.subscriptions import match_subscriptions
from test_capabilities_summary import structured, tool_error
from test_client_contract import rpc
from test_module_events import SOURCE, beat, bound, call_event, emit, empty_module_journal, module_hints, publish

PERMISSIONS = ['read', 'subscription-write', 'subscription-ack']


@pytest.fixture()
def modules(seeded):
    """An empty module journal and no dispatcher heartbeat (as test_module_events' fixture)."""
    empty_module_journal(seeded[0])
    return seeded


def key(store, principal):
    return store.issue_key(principal, ['telegram-demo'], permissions=PERMISSIONS, key_permissions=PERMISSIONS)


def calls_subscription(store, token, types=('call.finished', 'call.owner_request')):
    return store.create_subscription(token, ['telegram-demo-chat'],
        module_sources=[{'scope': 'calls:own', 'types': list(types)}])['subscription_id']


def access_changed(seq, scope_ref='owner-a', reason='permission_removed'):
    return {'specversion': '1.0', 'id': f'calls:{seq}', 'source': 'deedkit:calls', 'type': 'call.access.changed',
        'subject': scope_ref, 'time': '2026-11-11T10:00:00Z', 'data': {'principal_ref': scope_ref, 'reason': reason},
        'urgency': 'now', 'expires_at': None}


def dispatch(store, sent, resolver=None):
    return dispatch_hints(store, transport=lambda endpoint, body, headers, **k: sent.append(json.loads(body)) or 204,
        resolver=resolver or (lambda *a, **k: ['93.184.216.34']))


def sources(store, token, sid):
    return [(item['scope_ref'], item['state']) for item in store.get_subscription(token, sid)['module_stream']['sources']]


def link_row(store, principal):
    """The owner's view of the principal's calls link (principal-info): state active, stale or revoked."""
    return next(row for row in store.principal_info(principal)['module_links'] if row['module'] == 'calls')


# spec: ANYM-564
def test_without_a_link_no_call_source_is_admitted_and_the_refusal_names_the_owner_step(modules):
    store = modules[0]
    token = key(store, 'module-reader')
    with pytest.raises(APIError) as refused:
        calls_subscription(store, token)
    assert str(refused.value) == 'module_not_linked' and refused.value.status == 409
    assert 'link calls for this key' in refused.value.next
    with store.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM subscriptions WHERE principal='module-reader'").fetchone()['n'] == 0
        # The internal path refuses too: no call source exists without the principal's link.
        sid = store.create_subscription(token, ['telegram-demo-chat'])['subscription_id']
    with store.pool.connection() as c, pytest.raises(APIError, match='module_not_linked'):
        module_events.attach_sources(c, sid, [('deedkit:calls', 'owner-a', ['call.finished'])])
    store.link_module('module-reader', 'calls', 'owner-a')
    # Only calls:own, never a scope the request names; only call types.
    for module_sources in ([{'scope': 'calls:owner-b', 'types': ['call.finished']}],
            [{'scope': 'calls:own', 'types': ['calendar.event.created']}], [{'scope': 'calls:own', 'types': []}],
            [{'scope': 'calls:own', 'types': ['call.finished'], 'scope_ref': 'owner-b'}]):
        with pytest.raises(APIError, match='invalid_filter'):
            store.create_subscription(token, ['telegram-demo-chat'], module_sources=module_sources)
    view = store.get_subscription(token, calls_subscription(store, token))
    assert [(s['source'], s['scope_ref'], s['state']) for s in view['module_stream']['sources']] == [
        ('deedkit:calls', 'owner-a', 'active')]


def module_events_block(store, token):
    """What `capabilities` tells this key about module events, and whether a call kind is named anywhere in it."""
    full = store.capabilities(token, detail='full')
    return full['subscriptions'].get('module_events'), 'call.' in json.dumps(full)


# spec: ANYM-566
def test_capabilities_name_call_kinds_only_to_a_key_with_a_live_link_and_exactly_the_kinds_it_can_use(modules):
    store = modules[0]
    token = key(store, 'module-reader')
    assert module_events_block(store, token) == (None, False)
    assert 'module_events' not in store.capabilities(token, sections=['subscriptions'])['subscriptions']
    store.link_module('module-reader', 'calls', 'owner-a')
    block, named = module_events_block(store, token)
    kinds = block['calls']['kinds']
    assert named and kinds == sorted(kinds) and 'call.finished' in kinds
    assert block == {'calls': {'source': 'deedkit:calls', 'scope': 'calls:own', 'kinds': kinds,
        'create': 'create_subscription', 'argument': 'module_sources', 'consume': 'fetch_subscription_events',
        'refused_without_link': 'module_not_linked'}}
    assert store.capabilities(token, sections=['subscriptions'])['subscriptions']['module_events'] == block
    # Exactly the kinds a subscription admits: every kind the module journal knows is advertised iff it is admitted.
    for kind in module_events.KINDS:
        if kind in kinds:
            calls_subscription(store, token, types=(kind,))
        else:
            with pytest.raises(APIError, match='invalid_filter'):
                calls_subscription(store, token, types=(kind,))
    assert not any(kind.startswith(('calendar.', 'recording.')) for kind in kinds)
    # A key of another principal that has no link still sees nothing (the link is the principal's, not the service's).
    assert module_events_block(store, key(store, 'other-reader')) == (None, False)
    # A stale link (the calls side journaled an access change after the grant) and a revoked one advertise nothing.
    publish(store, access_changed(566), scope='owner-a')  # a number no other test publishes: the journal outlives a test
    assert module_events_block(store, token) == (None, False)
    store.link_module('module-reader', 'calls', 'owner-a')
    assert module_events_block(store, token)[0] == block
    store.unlink_module('module-reader', 'calls')
    assert module_events_block(store, token) == (None, False)


# spec: ANYM-566
def test_a_failed_link_read_leaves_capabilities_answering_without_the_block(modules, monkeypatch):
    store = modules[0]
    token = key(store, 'module-reader')
    store.link_module('module-reader', 'calls', 'owner-a')
    monkeypatch.setattr(module_links, 'active_link', lambda c, principal: c.execute('SELECT 1/0'))
    full = store.capabilities(token, detail='full')
    assert full['subscriptions']['implemented'] is True and 'module_events' not in full['subscriptions']
    assert 'call.' not in json.dumps(full) and full['events']['fetch'] == 'fetch_subscription_events'


# spec: ANYM-566
@pytest.mark.parametrize('transport', ['rest', 'mcp'])
def test_create_subscription_takes_module_sources_over_rest_and_mcp_and_refuses_without_a_link(modules, transport):
    store = modules[0]
    token = key(store, 'module-reader')
    wanted = {'chat_ids': ['telegram-demo-chat'], 'module_sources': [{'scope': 'calls:own', 'types': ['call.finished']}]}
    with TestClient(create_app(store, ['testserver'])) as client:
        def call(**arguments):
            if transport == 'mcp':
                response = rpc(client, token, 'create_subscription', arguments)
                if 'error' in response.json():  # a schema violation is a protocol error before any operation
                    error = response.json()['error']
                    assert error['code'] == -32602
                    return 400, {'error': 'invalid_parameters', 'fields': error['data']['fields']}
                if response.json()['result'].get('isError'):
                    refused = tool_error(response)
                    return refused.get('status', 409), refused
                return 200, structured(response)
            response = client.post('/v1/create_subscription', headers={'Authorization': 'Bearer ' + token}, json=arguments)
            return response.status_code, response.json()
        status, refused = call(**wanted)
        assert (status, refused['error']) == (409, 'module_not_linked') and 'link calls for this key' in refused['next']
        # The gateway relays the owner step as a reviewed hint (dispatch_shaping.REVIEWED_HINTS holds this exact text).
        assert refused['hint'] == module_links.NEXT_LINK == (
            'Ask the owner to link calls for this key: deedkit admin module-links link --principal <principal> '
            '--module calls --module-principal <calls principal>; then create the subscription again.')
        store.link_module('module-reader', 'calls', 'owner-a')
        # The schema is closed: a scope of the request's own, an unknown key or a non-list is refused before the store.
        for bad in ([{'scope': 'calls:own', 'types': ['call.finished'], 'scope_ref': 'owner-b'}],
                {'scope': 'calls:own', 'types': ['call.finished']}, [{'scope': 'calls:own'}]):
            status, invalid = call(chat_ids=['telegram-demo-chat'], module_sources=bad)
            assert status == 400 and invalid['error'] == 'invalid_parameters' and 'module_sources' in invalid['fields']
        # ANYM-577: the schema takes up to 16 sources (calendar); a second calls source is refused by the server.
        status, invalid = call(chat_ids=['telegram-demo-chat'],
            module_sources=[{'scope': 'calls:own', 'types': ['call.finished']}] * 2)
        assert invalid['error'] == 'invalid_filter'
        status, created = call(**wanted)
        assert status == 200
        sid = created['subscription_id']
    assert sources(store, token, sid) == [('owner-a', 'active')]


# spec: ANYM-564
def test_two_keys_and_one_call_event_deliver_only_to_the_linked_key(modules, tmp_path):
    store = modules[0]
    first, second = key(store, 'module-reader'), key(store, 'other-reader')
    store.link_module('module-reader', 'calls', 'owner-a')
    store.link_module('other-reader', 'calls', 'owner-b')
    mine, theirs = calls_subscription(store, first), calls_subscription(store, second)
    bound(store, first, mine, tmp_path)
    beat(store)
    seq = publish(store, call_event(1), scope='owner-a')
    match_subscriptions(store)
    sent = []
    dispatch(store, sent)
    assert [(hint['kind'], hint['through']) for hint in sent] == [('module_events', seq)]
    assert [item['module_event_id'] for item in store.fetch_subscription_events(first, mine)['module']['items']] == [seq]
    assert store.fetch_subscription_events(second, theirs)['module']['items'] == []
    assert module_hints(store, theirs) == []


# spec: ANYM-564
def test_revocation_between_match_and_send_refuses_the_hint_and_message_sources_keep_flowing(modules, tmp_path):
    store = modules[0]
    token = key(store, 'module-reader')
    store.link_module('module-reader', 'calls', 'owner-a')
    sid = calls_subscription(store, token)
    bound(store, token, sid, tmp_path)
    beat(store)
    publish(store, call_event(1, kind='call.owner_request'), scope='owner-a')
    match_subscriptions(store)

    def resolver(*a, **k):  # after the lease, before the fence: the owner revokes meanwhile
        store.unlink_module('module-reader', 'calls')
        return ['93.184.216.34']
    stats = dispatch_hints(store, transport=lambda *a, **k: pytest.fail('a revoked link was announced'),
        resolver=resolver)
    assert stats['cancelled'] == 1
    assert [(h['state'], h['error_code']) for h in module_hints(store, sid)] == [('cancelled', 'module_not_linked')]
    assert sources(store, token, sid) == [('owner-a', 'access_changed')]
    # The subscription stays active and its message source keeps flowing; later call events never match.
    message = emit(store)
    publish(store, call_event(2, 'call-2'), scope='owner-a')
    match_subscriptions(store)
    page = store.fetch_subscription_events(token, sid)
    assert page['subscription']['state'] == 'active'
    assert [item['event_id'] for item in page['items']] == [message] and page['module']['items'] == []
    acked = store.ack_subscription_events(token, sid, page['receipt'], page['ack_version'])
    assert acked['acked_through'] == message
    with store.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM subscription_module_matches').fetchone()['n'] == 1


# spec: ANYM-564
def test_a_receipt_issued_before_revocation_never_acks_after_it(modules):
    store = modules[0]
    token = key(store, 'module-reader')
    store.link_module('module-reader', 'calls', 'owner-a')
    sid = calls_subscription(store, token)
    seq = publish(store, call_event(1), scope='owner-a')
    match_subscriptions(store)
    page = store.fetch_subscription_events(token, sid)
    assert [item['module_event_id'] for item in page['module']['items']] == [seq]
    store.unlink_module('module-reader', 'calls')
    with pytest.raises(APIError, match='invalid_receipt'):
        store.ack_subscription_events(token, sid, page['receipt'], page['ack_version'])
    # A new fetch leaves the revoked source's reference out and its receipt moves past it.
    page = store.fetch_subscription_events(token, sid)
    assert page['module']['items'] == [] and page['module']['next_after'] == seq
    assert store.ack_subscription_events(token, sid, page['receipt'], page['ack_version'])['module_acked_through'] == seq
    passive = store.get_subscription_events(token, sid, module_after=0)
    assert passive['module']['items'] == []


# spec: ANYM-564
def test_a_regrant_never_delivers_rows_queued_before_the_revocation(modules, tmp_path):
    store = modules[0]
    token = key(store, 'module-reader')
    first = store.link_module('module-reader', 'calls', 'owner-a')
    assert first['changed'] and store.link_module('module-reader', 'calls', 'owner-a') == {**first, 'changed': False}
    sid = calls_subscription(store, token)
    bound(store, token, sid, tmp_path)
    beat(store)
    old = publish(store, call_event(1), scope='owner-a')
    match_subscriptions(store)
    revoked = store.unlink_module('module-reader', 'calls')
    regranted = store.link_module('module-reader', 'calls', 'owner-a')
    assert first['generation'] < revoked['generation'] < regranted['generation'] and regranted['state'] == 'active'
    sent = []
    dispatch(store, sent)
    assert sent == [] and [h['error_code'] for h in module_hints(store, sid)] == ['module_not_linked']
    assert sources(store, token, sid) == [('owner-a', 'access_changed')]
    assert store.fetch_subscription_events(token, sid)['module']['items'] == []
    # A new subscription under the new generation reads only what follows it.
    fresh = calls_subscription(store, token)
    new = publish(store, call_event(2, 'call-2'), scope='owner-a')
    match_subscriptions(store)
    items = store.fetch_subscription_events(token, fresh)['module']['items']
    assert [item['module_event_id'] for item in items] == [new] and old not in [i['module_event_id'] for i in items]
    assert store.fetch_subscription_events(token, sid)['module']['items'] == []


# spec: ANYM-564
def test_a_call_access_changed_event_marks_the_link_stale(modules):
    store = modules[0]
    token, other = key(store, 'module-reader'), key(store, 'other-reader')
    store.link_module('module-reader', 'calls', 'owner-a')
    store.link_module('other-reader', 'calls', 'owner-b')
    sid, kept = calls_subscription(store, token), calls_subscription(store, other)
    publish(store, call_event(1), scope='owner-a')
    match_subscriptions(store)
    before = link_row(store, 'module-reader')
    event = access_changed(2)
    assert validate_event(event) == 2
    assert publish(store, event, scope='owner-a') == 2
    after = link_row(store, 'module-reader')
    assert after['state'] == 'stale' and before['state'] == 'active'
    assert link_row(store, 'other-reader')['state'] == 'active'
    assert sources(store, token, sid) == [('owner-a', 'access_changed')] and sources(store, other, kept) == [
        ('owner-b', 'active')]
    # Its references leave the pages and later calls never match; the other principal's source keeps flowing.
    assert store.fetch_subscription_events(token, sid)['module']['items'] == []
    third = publish(store, call_event(3, 'call-3'), scope='owner-b')
    match_subscriptions(store)
    assert [i['module_event_id'] for i in store.fetch_subscription_events(other, kept)['module']['items']] == [third]
    # A replay of the same event is idempotent and changes nothing more; a new subscription is refused until relinked.
    assert publish(store, event, scope='owner-a') == 2 and link_row(store, 'module-reader') == after
    with pytest.raises(APIError, match='module_not_linked'):
        calls_subscription(store, token)
    relinked = store.link_module('module-reader', 'calls', 'owner-a')
    assert relinked['changed'] and relinked['state'] == 'active' and relinked['generation'] > after['generation']
    assert sources(store, token, calls_subscription(store, token)) == [('owner-a', 'active')]
    assert sources(store, token, sid) == [('owner-a', 'access_changed')]
    for wrong in ({'urgency': 'batch'}, {'expires_at': '2099-01-01T00:00:00Z'},
            {'data': {'principal_ref': 'owner-a', 'reason': 'other'}}):
        with pytest.raises(module_events.ModuleContractError):
            validate_event({**event, **wrong})


# spec: ANYM-564
def test_every_type_admitted_before_schema_90_still_publishes(modules):
    from test_module_events import recording_event, starting_event
    store = modules[0]
    events = [call_event(1), call_event(2, kind='call.owner_request'), call_event(3, kind='call.not_answered'),
        {**call_event(4), 'type': 'call.interrupted', 'data': {'call_id': 'call-1', 'end_reason': 'service_restart'}},
        {**call_event(5), 'type': 'call.failed', 'data': {'call_id': 'call-1', 'status': 'failed',
            'end_reason': 'provider_failed', 'provider_cause': 'rate_limited', 'provider_code': 20429}},
        starting_event(1, start='2099-10-29T10:15:00Z'), recording_event(1), recording_event(2, 'recording.access.changed')]
    scopes = ['owner-a'] * 5 + ['calendar:family'] + ['recordings:omi-a.0123456789abcdef'] * 2
    assert [publish(store, event, scope=scope) for event, scope in zip(events, scopes)] == list(range(1, 9))


# spec: ANYM-564
def test_the_gateway_role_reads_links_admits_only_through_the_link_and_never_writes_them(modules):
    store = modules[0]
    suffix = uuid4().hex[:10]
    files = {'gateway_role.sql': 'messaging_gateway', 'subscription_role.sql': 'messaging_subscriptions',
        'delivery_role.sql': 'messaging_event_delivery', 'module_router_role.sql': 'messaging_module_router'}
    roles = {name: f'anym564_{name.split("_")[0]}_{suffix}' for name in files}
    with store.pool.connection() as c:
        for name, original in files.items():
            c.execute(SOURCE.joinpath(name).read_text().replace(original, roles[name]))
    try:
        with store.pool.connection() as c:
            def can(role, kind, name, privilege):
                check = {'table': 'has_table_privilege', 'function': 'has_function_privilege'}[kind]
                return c.execute('SELECT ' + check + '(%s,%s,%s) AS x', (role, name, privilege)).fetchone()['x']
            for name, role in roles.items():
                reads, gateway = name != 'module_router_role.sql', name == 'gateway_role.sql'
                assert can(role, 'table', 'module_links', 'SELECT') is reads
                assert can(role, 'table', 'subscription_module_links', 'SELECT') is reads
                # Only the owner CLI writes links; the API inserts a call source and its generation, nothing else.
                assert not any(can(role, 'table', 'module_links', change) for change in ('INSERT', 'UPDATE', 'DELETE'))
                for table in ('subscription_module_sources', 'subscription_module_links'):
                    assert can(role, 'table', table, 'INSERT') is gateway
                    assert not can(role, 'table', table, 'UPDATE') and not can(role, 'table', table, 'DELETE')
    finally:
        with store.pool.connection() as c:
            for role in roles.values():
                c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


# spec: ANYM-564
def test_restore_from_backup_then_regrant_never_revives_a_source_admitted_before_it(modules, tmp_path):
    from test_subscription_retention import snapshot_restored
    from messaging_service.backup import quarantine_restore
    store = modules[0]
    token = key(store, 'module-reader')
    store.link_module('module-reader', 'calls', 'owner-a')
    sid = calls_subscription(store, token)
    publish(store, call_event(1), scope='owner-a')
    match_subscriptions(store)
    issued = link_row(store, 'module-reader')['generation']
    with snapshot_restored(store, tmp_path) as restored:
        quarantine_restore(restored)
        restored.migrate()
        with restored.pool.connection() as c:
            assert c.execute('SELECT generation,state FROM module_links').fetchone() == {'generation': issued,
                'state': 'active'}
            assert c.execute('SELECT generation FROM subscription_module_links').fetchone()['generation'] == issued
        regranted = restored.link_module('module-reader', 'calls', 'owner-b')
        assert regranted['generation'] > issued
        with restored.pool.connection() as c:
            from messaging_service.module_links import source_states
            assert source_states(c, sid) == {('deedkit:calls', 'owner-a'): 'access_changed'}
