"""A maintainer key is woken the moment an agent submits feedback or the loop records its summary (ANYM-989)."""
import asyncio
from contextlib import contextmanager
from copy import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from messaging_service import feedback_events, module_events, module_links
from messaging_service import store as store_module
from messaging_service import wake
from messaging_service.store import APIError
from messaging_service.subscription_consumption import wait_for_subscription_events
from messaging_service.subscriptions import JOURNAL_FAILURES, match_subscriptions
from test_db_role_isolation import as_role, install, remove
from test_event_delivery import call, identity
from test_feedback_events import feedback_subscription, key_id, matched, submit
from test_feedback_export_metadata import projection, projection_rows, record

SOURCE = Path(store_module.__file__).parent
PERMISSION = 'feedback-maintainer-read'
BRIDGE = [PERMISSION, 'subscription-write', 'subscription-ack']  # the Ryzen bridge key: no `read`
FULL = ['read', 'subscription-write', 'subscription-ack', PERMISSION]
MAINTAINER = {'maintainer': True}


@pytest.fixture()
def modules(seeded):
    store = seeded[0]
    with store.pool.connection() as c:
        c.execute('TRUNCATE module_events,module_source_health CASCADE')
        c.execute('UPDATE module_event_counter SET value=0 WHERE id=1')
        c.execute('TRUNCATE agent_feedback CASCADE')
    return seeded


def key(store, name, permissions):
    return store.issue_key(name, ['telegram-demo'], permissions=permissions, key_permissions=permissions)


def events(store, kind):
    with store.pool.connection() as c:
        return c.execute('SELECT seq,event_key,source,type,subject,scope_ref,data,urgency,expires_at FROM module_events '
            'WHERE type=%s ORDER BY seq', (kind,)).fetchall()


def submissions(store):
    return [row for row in events(store, 'feedback.received') if 'previous_status' not in row['data']]


def maintainer_subscription(store, token, **arguments):
    return store.create_subscription(token, feedback=MAINTAINER, **arguments)['subscription_id']


def types(store, token, sid):
    match_subscriptions(store)
    return sorted((item['type'], item['data']['feedback_id']) for item in
        store.fetch_subscription_events(token, sid)['module']['items'])


def refused(code, function, *args, **kwargs):
    with pytest.raises(APIError) as failure:
        function(*args, **kwargs)
    assert failure.value.args[0] == code, failure.value.args[0]


def count(store, table='subscriptions'):
    with store.pool.connection() as c:
        return c.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n']


# spec: ANYM-989
def test_a_submission_publishes_one_received_event_and_a_replayed_request_publishes_none(modules):
    store = modules[0]
    token = identity(store)
    first = submit(store, token, 1)
    [event] = submissions(store)
    assert (event['source'], event['type'], event['subject'], event['scope_ref']) == (
        'deedkit:feedback', 'feedback.received', first, 'feedback:submitted')
    assert (event['urgency'], event['expires_at']) == ('now', None)
    assert event['data'] == {'feedback_id': first, 'status': 'received', 'category': 'bug'}
    assert event['event_key'] == 'feedback:%d' % event['seq']
    assert submit(store, token, 1) == first and len(submissions(store)) == 1  # the same request_id: no second event
    second = submit(store, identity(store, 'second-agent'), 2)
    rows = submissions(store)
    assert [row['subject'] for row in rows] == [first, second] and rows[1]['seq'] == rows[0]['seq'] + 1
    assert key_id(store, token) not in json.dumps(rows, default=str) and 'Synthetic report' not in json.dumps(rows, default=str)


# spec: ANYM-989
def test_a_publish_error_keeps_the_item_and_is_counted_and_a_replay_then_publishes_nothing(modules, monkeypatch, capsys):
    store = modules[0]
    token = identity(store)

    def broken(c, kind):
        raise ZeroDivisionError
    before = JOURNAL_FAILURES[feedback_events.SUBMISSION_FAILURE_KIND]
    with monkeypatch.context() as patched:
        patched.setattr(wake, 'mark', broken)
        first = submit(store, token, 1)
    assert JOURNAL_FAILURES[feedback_events.SUBMISSION_FAILURE_KIND] == before + 1
    assert submissions(store) == []  # the savepoint took the event with it...
    assert call(store, token, 'list_feedback')['items'][0]['id'] == first  # ...and the item stays
    line = [json.loads(text) for text in capsys.readouterr().out.splitlines() if 'subscription_journal_failed' in text]
    assert line == [{'error': 'subscription_journal_failed', 'kind': 'feedback.received', 'class': 'ZeroDivisionError'}]
    assert submit(store, token, 1) == first and submissions(store) == []  # a replay publishes nothing, even after the error


# spec: ANYM-989
def test_an_own_key_received_subscription_is_not_woken_by_a_submission_but_still_by_an_operator_change(modules):
    store = modules[0]
    mine = identity(store)
    sid = feedback_subscription(store, mine, {'statuses': ['received']})
    fid = submit(store, mine, 1)
    assert matched(store, mine, sid) == []
    store.set_feedback_status([fid], 'accepted', spec_ref='ANYM-989')
    store.set_feedback_status([fid], 'received')
    assert matched(store, mine, sid) == [('feedback.received', fid)]


# spec: ANYM-989
def test_the_projection_command_publishes_for_a_written_item_only_and_a_failed_publish_is_counted(modules, monkeypatch):
    store = modules[0]
    agent = identity(store)
    first, second = submit(store, agent, 1), submit(store, agent, 2)
    assert record(store, projection(first), projection(second, classification='feature'))['recorded'] == 2
    rows = events(store, 'feedback.projection_recorded')
    assert {(row['subject'], row['data']['feedback_id'], row['data']['classification']) for row in rows} == {
        (first, first, 'reported-bug'), (second, second, 'feature')}
    assert all(set(row['data']) == {'feedback_id', 'classification'} and row['scope_ref'] == 'feedback:submitted'
        and row['urgency'] == 'now' and row['expires_at'] is None for row in rows)
    unchanged = projection(first)
    assert record(store, {**unchanged, 'released_at': (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()})['recorded'] == 0
    assert len(events(store, 'feedback.projection_recorded')) == 2  # an unchanged item publishes nothing
    newer = {**projection(first, classification='question'), 'released_at': datetime.now(timezone.utc).isoformat()}
    assert record(store, newer)['recorded'] == 1 and len(events(store, 'feedback.projection_recorded')) == 3
    before = JOURNAL_FAILURES[feedback_events.PROJECTION_FAILURE_KIND]

    def broken(c, event, scope_ref):
        c.execute('SELECT 1/0')
    monkeypatch.setattr(module_events, 'publish', broken)
    later = {**projection(second), 'released_at': (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()}
    assert record(store, later)['recorded'] == 1
    assert JOURNAL_FAILURES[feedback_events.PROJECTION_FAILURE_KIND] == before + 1
    assert len(events(store, 'feedback.projection_recorded')) == 3 and len(projection_rows(store)) == 2


# spec: ANYM-989
def test_a_maintainer_subscription_sees_every_keys_submissions_projections_and_status_changes(modules):
    store = modules[0]
    bridge = key(store, 'bridge', BRIDGE)
    sid = maintainer_subscription(store, bridge)
    a, b = identity(store, 'agent-a'), identity(store, 'agent-b')
    fa, fb = submit(store, a, 1), submit(store, b, 1)
    store.set_feedback_status([fa, fb], 'accepted', spec_ref='ANYM-989')
    record(store, projection(fa))
    assert types(store, bridge, sid) == sorted([
        ('feedback.received', fa), ('feedback.received', fb), ('feedback.accepted', fa), ('feedback.accepted', fb),
        ('feedback.projection_recorded', fa)])
    own = feedback_subscription(store, a, {'statuses': ['accepted']})
    store.set_feedback_status([fa, fb], 'declined', note='Out of scope')
    assert matched(store, a, own) == []  # an own-key subscription keeps to its key and its statuses
    store.set_feedback_status([fa, fb], 'fixed', spec_ref='ANYM-989', fixed_in='deedkit@' + 'a' * 40)
    view = store.get_subscription(bridge, sid)
    assert view['feedback_maintainer'] == 'only'
    assert [(row['source'], row['scope_ref'], row['state']) for row in view['module_stream']['sources']] == [
        ('deedkit:feedback', 'feedback:all', 'active')]


# spec: ANYM-989
def test_a_default_key_and_a_key_with_read_alone_are_refused_the_maintainer_source_and_nothing_is_written(modules):
    store = modules[0]
    default = identity(store)
    before = count(store)
    for value in (MAINTAINER, {'maintainer': True, 'statuses': ['fixed']}):
        refused('forbidden', store.create_subscription, default, feedback=value)
        refused('forbidden', store.create_subscription, default, ['telegram-demo-chat'], feedback=value)
    assert count(store) == before
    # A value that is no maintainer request keeps its own checks.
    for value in ({'maintainer': False}, {'maintainer': 1}, {'maintainer': True, 'scope_ref': 'feedback:all'},
                  {'maintainer': True, 'statuses': ['verified']}):
        refused('invalid_filter', store.create_subscription, key(store, 'full-' + uuid4().hex[:6], FULL), feedback=value)
    assert count(store) == before


# spec: ANYM-989
def test_the_maintainer_source_can_share_a_definition_with_the_own_key_part_or_chats(modules):
    store = modules[0]
    full = key(store, 'full', FULL)
    both = store.create_subscription(full, feedback={'maintainer': True, 'statuses': ['fixed']})
    with_chat = store.create_subscription(full, ['telegram-demo-chat'], feedback=MAINTAINER)
    assert both['feedback_maintainer'] == 'with_others' and with_chat['feedback_maintainer'] == 'with_others'
    assert sorted((row['scope_ref'] == 'feedback:all', row['types'][:1]) for row in
        store.get_subscription(full, both['subscription_id'])['module_stream']['sources']) == [
        (False, ['feedback.fixed']), (True, ['feedback.accepted'])]
    alone = store.create_subscription(full, feedback=MAINTAINER)
    assert alone['feedback_maintainer'] == 'only'


# spec: ANYM-989
def test_a_maintainer_subscription_starts_at_the_head_and_counts_as_one_definition(modules, monkeypatch):
    store = modules[0]
    agent = identity(store)
    submit(store, agent, 1)  # before the subscription
    bridge = key(store, 'bridge', BRIDGE)
    monkeypatch.setenv('SUBSCRIPTION_MAX_DEFINITIONS', '1')
    sid = maintainer_subscription(store, bridge)
    assert types(store, bridge, sid) == []
    refused('subscription_limit', store.create_subscription, bridge, feedback=MAINTAINER)


# spec: ANYM-989
def test_a_bridge_key_without_read_creates_waits_fetches_acks_and_manages_its_own_definition(modules):
    store = modules[0]
    bridge = key(store, 'bridge', BRIDGE)
    created = store.create_subscription(bridge, feedback=MAINTAINER, label='bridge')
    sid = created['subscription_id']
    assert [item['subscription_id'] for item in store.list_subscriptions(bridge)['items']] == [sid]
    assert store.get_subscription(bridge, sid)['state'] == 'active'
    fid = submit(store, identity(store), 1)
    match_subscriptions(store)
    page = asyncio.run(wait_for_subscription_events(store, bridge, sid, timeout=2, limit=10))
    assert page['wait_status'] == 'events_available'
    assert [(item['type'], item['data']['feedback_id']) for item in page['module']['items']] == [('feedback.received', fid)]
    assert [item['module_event_id'] for item in store.get_subscription_events(bridge, sid)['module']['items']] == [
        page['module']['items'][0]['module_event_id']]
    fetched = store.fetch_subscription_events(bridge, sid)
    assert fetched['receipt'] and fetched['ack_available'] is True
    acked = store.ack_subscription_events(bridge, sid, fetched['receipt'], fetched['ack_version'])
    assert acked['module_acked_through'] == fetched['module']['next_after']
    assert store.fetch_subscription_events(bridge, sid)['module']['items'] == []
    paused = store.update_subscription(bridge, sid, created['version'], action='pause')
    resumed = store.update_subscription(bridge, sid, paused['version'], action='resume')
    closed = store.update_subscription(bridge, sid, resumed['version'], action='close')
    assert (paused['state'], resumed['state'], closed['state']) == ('paused', 'active', 'closed')


# spec: ANYM-989
def test_a_bridge_key_without_read_is_refused_everything_else(modules):
    store = modules[0]
    full = key(store, 'full', FULL)
    chat = store.create_subscription(full, ['telegram-demo-chat'])
    own = store.create_subscription(full, feedback=True)
    mixed = store.create_subscription(full, feedback={'maintainer': True, 'statuses': ['fixed']})
    only = store.create_subscription(full, feedback=MAINTAINER)
    store.set_key_permissions(store.capabilities(full)['credential_id'], BRIDGE)  # the key loses `read`
    for other in (chat, own, mixed):
        sid, version = other['subscription_id'], other['version']
        refused('forbidden', store.get_subscription, full, sid)
        refused('forbidden', store.get_subscription_events, full, sid)
        refused('forbidden', store.fetch_subscription_events, full, sid)
        refused('forbidden', store.update_subscription, full, sid, version, action='pause')
        refused('forbidden', store.ack_subscription_events, full, sid, 'r' * 43, 1)
        refused('forbidden', call, store, full, 'configure_subscription_delivery', sid, version, None, None, None)
        refused('forbidden', call, store, full, 'test_subscription', sid, 'synthetic-test-request')
        refused('forbidden', asyncio.run, wait_for_subscription_events(store, full, sid, timeout=0))
    assert [item['subscription_id'] for item in store.list_subscriptions(full)['items']] == [only['subscription_id']]
    fresh = key(store, 'bridge', BRIDGE)
    for arguments in ({'chat_ids': ['telegram-demo-chat']}, {'feedback': True}, {'feedback': {'statuses': ['fixed']}},
                      {'feedback': {'maintainer': True, 'statuses': ['fixed']}},
                      {'feedback': MAINTAINER, 'chat_ids': ['telegram-demo-chat']},
                      {'feedback': MAINTAINER, 'sources': {'all_visible_chats': True}},
                      {'feedback': MAINTAINER, 'kinds': ['message.created']},
                      {'feedback': MAINTAINER, 'direction': 'outgoing'}, {}):
        refused('forbidden', store.create_subscription, fresh, **arguments)
    sid = maintainer_subscription(store, fresh)
    version = store.get_subscription(fresh, sid)['version']
    refused('forbidden', call, store, fresh, 'configure_subscription_delivery', sid, version, None, None, None)
    refused('forbidden', call, store, fresh, 'test_subscription', sid, 'synthetic-test-request')
    refused('forbidden', call, store, fresh, 'list_feedback')
    refused('forbidden', call, store, fresh, 'submit_feedback', 'synthetic-bridge-1', 'bug', 'Synthetic report')
    refused('forbidden', store.search, fresh, 'synthetic')
    refused('forbidden', store.list_chats, fresh)
    assert store.export_feedback_metadata(fresh)['items'] is not None  # its own read stays


# spec: ANYM-989
def test_a_key_with_read_and_the_permission_works_as_before_and_a_key_with_read_alone_keeps_its_own_subscriptions(modules):
    store = modules[0]
    full = key(store, 'full', FULL)
    chat = store.create_subscription(full, ['telegram-demo-chat'])
    only = store.create_subscription(full, feedback=MAINTAINER)
    assert sorted(item['subscription_id'] for item in store.list_subscriptions(full)['items']) == sorted(
        [chat['subscription_id'], only['subscription_id']])
    plain = identity(store)
    sid = store.create_subscription(plain, ['telegram-demo-chat'])['subscription_id']
    assert store.get_subscription(plain, sid)['state'] == 'active' and len(store.list_subscriptions(plain)['items']) == 1


# spec: ANYM-989
def test_the_capabilities_of_a_bridge_key_name_only_its_tools(modules):
    store = modules[0]
    bridge = key(store, 'bridge', BRIDGE)
    caps = store.capabilities(bridge)
    # Exactly its own tools (list_messenger_operations is named to every key, as before).
    assert sorted(caps['available']) == [
        'ack_subscription_events', 'create_subscription', 'export_feedback_metadata', 'fetch_subscription_events',
        'get_subscription', 'get_subscription_events', 'list_messenger_operations', 'list_subscriptions',
        'update_subscription', 'wait_for_subscription_events']
    assert caps['permissions'] == sorted(BRIDGE)


# spec: ANYM-989
def test_losing_the_permission_hides_the_source_in_every_path_including_events_matched_before(modules):
    store = modules[0]
    full = key(store, 'full', FULL)
    sid = maintainer_subscription(store, full)
    fid = submit(store, identity(store), 1)
    assert types(store, full, sid) == [('feedback.received', fid)]
    with store.pool.connection() as c:
        row = c.execute('SELECT * FROM subscriptions WHERE id=%s', (sid,)).fetchone()
        stream = module_events.stream(c, sid)
        through = stream['matched_through']
        assert module_links.source_states(c, sid) == {('deedkit:feedback', 'feedback:all'): 'active'}
        assert [item['type'] for item in module_events.page(c, row, stream, 0, 10)['items']] == ['feedback.received']
        assert [item['type'] for item in module_events.hint_references(c, sid, through)] == ['feedback.received']
    store.set_key_permissions(store.capabilities(full)['credential_id'], ['read', 'subscription-write', 'subscription-ack'])
    with store.pool.connection() as c:
        assert module_links.source_states(c, sid) == {('deedkit:feedback', 'feedback:all'): 'access_changed'}
        assert module_events.page(c, row, stream, 0, 10)['items'] == []
        assert module_events.hint_references(c, sid, through) == []
    refused('subscription_access_changed', store.fetch_subscription_events, full, sid)
    refused('subscription_access_changed', asyncio.run, wait_for_subscription_events(store, full, sid, timeout=0))
    assert store.get_subscription(full, sid)['state'] == 'access_changed'
    # A key that never held the permission gets no read of the same events through a definition of another key.
    other = identity(store, 'other-agent')
    assert store.list_subscriptions(other)['items'] == []


# spec: ANYM-989
@pytest.mark.parametrize('how', ['revoked', 'expired', 'principal_disabled', 'principal_permission_removed', 'ceiling_null'])
def test_each_way_the_key_stops_holding_the_permission_hides_the_source(modules, how):
    store = modules[0]
    full = key(store, 'full-' + how.replace('_', '-'), FULL)
    sid = maintainer_subscription(store, full)
    submit(store, identity(store), 1)
    match_subscriptions(store)
    with store.pool.connection() as c:
        row = c.execute('SELECT * FROM subscriptions WHERE id=%s', (sid,)).fetchone()
        stream = module_events.stream(c, sid)
        assert len(module_events.page(c, row, stream, 0, 10)['items']) == 1
        if how == 'revoked':
            c.execute('UPDATE api_keys SET revoked=true WHERE id=%s', (row['key_id'],))
        elif how == 'expired':
            c.execute("UPDATE api_keys SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (row['key_id'],))
        elif how == 'principal_disabled':
            c.execute('UPDATE principals SET enabled=false WHERE id=%s', (row['principal'],))
        elif how == 'principal_permission_removed':
            c.execute("UPDATE principals SET permissions=array_remove(permissions,'feedback-maintainer-read') WHERE id=%s",
                (row['principal'],))
        else:  # a NULL ceiling inherits no new permission
            c.execute('UPDATE api_keys SET permissions=NULL WHERE id=%s', (row['key_id'],))
        assert module_events.page(c, row, stream, 0, 10)['items'] == []
        assert module_events.hint_references(c, sid, stream['matched_through']) == []
        assert module_links.source_states(c, sid) == {('deedkit:feedback', 'feedback:all'): 'access_changed'}
        c.rollback()


# spec: ANYM-989
def test_an_own_key_feedback_subscription_stays_live_as_before_whatever_its_key_holds(modules):
    store = modules[0]
    mine = identity(store)
    sid = feedback_subscription(store, mine, {'statuses': ['accepted']})
    fid = submit(store, mine, 1)
    store.set_feedback_status([fid], 'accepted', spec_ref='ANYM-989')
    assert matched(store, mine, sid) == [('feedback.accepted', fid)]
    with store.pool.connection() as c:
        assert module_links.source_states(c, sid) == {('deedkit:feedback', key_id(store, mine)): 'active'}


# spec: ANYM-989
def test_a_webhook_reference_has_the_status_only_when_the_event_has_one(modules):
    moment = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
    base = {'event_key': 'feedback:7', 'occurred_at': moment, 'data': {'feedback_id': 'fb-1'}}
    submitted = feedback_events.webhook_reference({**base, 'type': 'feedback.received',
        'data': {'feedback_id': 'fb-1', 'status': 'received', 'category': 'bug'}})
    assert submitted == {'event_id': 'feedback:7', 'type': 'feedback.received', 'occurred_at': '2026-11-12T09:00:00Z',
        'feedback_id': 'fb-1', 'status': 'received'}
    recorded = feedback_events.webhook_reference({**base, 'type': 'feedback.projection_recorded',
        'data': {'feedback_id': 'fb-1', 'classification': 'feature'}})
    assert recorded == {'event_id': 'feedback:7', 'type': 'feedback.projection_recorded',
        'occurred_at': '2026-11-12T09:00:00Z', 'feedback_id': 'fb-1'}


# spec: ANYM-989
def test_a_bound_endpoint_of_a_maintainer_subscription_gets_references_with_no_category_or_classification(modules, tmp_path,
                                                                                                         monkeypatch):
    from messaging_service.event_delivery import dispatch_hints
    from messaging_service.heartbeats import record as beat
    store = modules[0]
    policy = tmp_path / 'delivery-policy.json'
    policy.write_text(json.dumps({'allow_hosts': ['example.test']}))
    policy.chmod(0o600)
    monkeypatch.setenv('DELIVERY_POLICY_FILE', str(policy))
    store.set_delivery_features(True)
    public = lambda *a, **k: ['93.184.216.34']  # noqa: E731
    try:
        permissions = [*FULL, 'delivery-write']
        token = key(store, 'maintainer-hook', permissions)
        endpoint = store.register_delivery_endpoint(token, 'endpoint-request-989', 'https://hooks.example.test/hook',
            'whsec_c3ludGhldGljLWVuZHBvaW50LXNlY3JldC0wMTIzNDU2Nzg5', resolver=public)
        with store.pool.connection() as c:
            c.execute("UPDATE delivery_endpoint_tests SET state='cancelled' WHERE endpoint=%s", (endpoint['endpoint_id'],))
        store.create_subscription(token, feedback=MAINTAINER, delivery={'endpoint_id': endpoint['endpoint_id']})
        assert beat(store, 'dispatcher')
        fid = submit(store, identity(store), 1)
        record(store, projection(fid))
        match_subscriptions(store)
        sent = []
        dispatch_hints(store, transport=lambda url, body, headers, **k: sent.append(json.loads(body)) or 204, resolver=public)
    finally:
        store.set_delivery_features(False)
    references = [reference for body in sent if body.get('type') == 'module_events' for reference in body['references']]
    assert {(reference['type'], tuple(sorted(reference))) for reference in references} == {
        ('feedback.received', ('event_id', 'feedback_id', 'occurred_at', 'status', 'type')),
        ('feedback.projection_recorded', ('event_id', 'feedback_id', 'occurred_at', 'type'))}
    assert 'reported-bug' not in json.dumps(sent) and '"bug"' not in json.dumps(sent)


# spec: ANYM-989
def test_migration_095_widens_one_list_replaces_the_validator_and_adds_one_definer_function(modules):
    store = modules[0]
    text = (SOURCE / 'migrations' / '095_feedback_submission_events.sql').read_text()
    assert text.startswith('-- rollout: manual') and 'Risk tier: non-critical' in text
    assert text.count('DROP CONSTRAINT') == 1 and 'module_events_type_check' in text
    assert not any(word in text for word in ('GRANT ', 'DELETE ', 'UPDATE ', 'INSERT ', 'TRUNCATE', 'DROP TABLE', 'DROP FUNCTION'))
    function = "'public.publish_feedback_received(uuid)'::regprocedure"
    query = f'SELECT proacl::text AS acl, prosecdef AS definer, proowner::regrole::text AS owner FROM pg_proc WHERE oid={function}'
    shape = """SELECT conrelid::regclass::text AS t, conname, pg_get_constraintdef(oid) AS d FROM pg_constraint
        WHERE connamespace='public'::regnamespace ORDER BY 1,2"""
    with store.pool.connection() as c:
        before, constraints = c.execute(query).fetchone(), c.execute(shape).fetchall()
        assert before['definer'] and not re.search(r'[{,]=X/', before['acl'] or '')  # PUBLIC has no EXECUTE
        c.execute(text)  # a repeated run changes nothing
        assert c.execute(query).fetchone() == before and c.execute(shape).fetchall() == constraints
        assert 'feedback.projection_recorded' in c.execute("""SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint
            WHERE conrelid='module_events'::regclass AND conname='module_events_type_check'""").fetchone()['d']
        # The function reads the category itself, is idempotent per item and refuses an unknown one.
        with pytest.raises(psycopg.errors.NoDataFound):
            c.execute('SELECT public.publish_feedback_received(%s)', (str(uuid4()),))
        c.rollback()


# spec: ANYM-989
def test_the_api_role_publishes_a_submission_through_the_function_alone_and_the_other_roles_read_the_fence(modules):
    store = modules[0]
    api = install(store, 'gateway_role.sql', 'messaging_gateway')
    matcher = install(store, 'subscription_role.sql', 'messaging_subscriptions')
    dispatcher = install(store, 'delivery_role.sql', 'messaging_event_delivery')
    plain = 'anym989_plain_' + uuid4().hex[:8]
    with store.pool.connection() as c:
        c.execute(sql.SQL('CREATE ROLE {}').format(sql.Identifier(plain)))
        c.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(plain)))

    class RestrictedPool:
        @contextmanager
        def connection(self):
            with store.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(api)))
                yield c
    gateway = copy(store)
    gateway.pool = RestrictedPool()
    try:
        agent = identity(store)
        fid = submit(gateway, agent, 1)  # the API role's own submission journals its event
        assert [row['subject'] for row in submissions(store)] == [fid]
        for statement, params in (
                ("SELECT public.publish_module_event('feedback:99','deedkit:feedback','feedback.received','x','feedback:submitted',"
                 "'2026-11-12T09:00:00Z','{}'::jsonb,'now',NULL)", ()),
                ('SELECT public.publish_feedback_received(%s)', (fid,))):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), as_role(store, plain) as c:
                c.execute(statement, params)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), as_role(store, api) as c:
            c.execute("SELECT public.publish_module_event('feedback:99','deedkit:feedback','feedback.received','x',"
                "'feedback:submitted','2026-11-12T09:00:00Z','{}'::jsonb,'now',NULL)")
        # The liveness rule of the maintainer source reads only what each reading role may read.
        bridge = key(store, 'bridge', BRIDGE)
        sid = maintainer_subscription(store, bridge)
        live = module_links.live('q.sub', 'q.source', 'q.scope')
        for role in (api, matcher, dispatcher):
            with as_role(store, role) as c:
                assert c.execute(f"SELECT ({live}) AS live FROM (SELECT %s::uuid AS sub,'deedkit:feedback'::text AS source,"
                    "'feedback:all'::text AS scope) q", (sid,)).fetchone()['live'] is True, role
    finally:
        for role in (api, matcher, dispatcher):
            remove(store, role)
        with store.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(plain)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(plain)))
