"""A maintainer key moves a feedback item to accepted, declined or duplicate through one guarded write (ANYM-992)."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import copy
import json
from pathlib import Path
import re
import threading
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from messaging_service import feedback_events
from messaging_service import store as store_module
from messaging_service.feedback_triage import scrub_note
from messaging_service.policy import AUDIT_OPERATIONS, OPERATION_PERMISSIONS, PERMISSIONS, effective_permissions
from messaging_service.store import APIError
from messaging_service.subscriptions import JOURNAL_FAILURES
from test_db_role_isolation import as_role, install, remove
from test_event_delivery import call, identity
from test_feedback_events import feedback_subscription, journal, key_id, matched, submit

SOURCE = Path(store_module.__file__).parent
WRITE = 'feedback-maintainer-write'
READ = 'feedback-maintainer-read'
SPEC = 'ANYM-992'
MERGED = 'deedkit@' + 'a' * 40


@pytest.fixture()
def modules(seeded):
    store = seeded[0]
    with store.pool.connection() as c:
        c.execute('TRUNCATE module_events,module_source_health CASCADE')
        c.execute('UPDATE module_event_counter SET value=0 WHERE id=1')
        c.execute('TRUNCATE agent_feedback CASCADE')
    return seeded


def writer(store, name='triage-writer', extra=()):
    permissions = [WRITE, *extra]
    return store.issue_key(name, ['telegram-demo'], permissions=permissions, key_permissions=permissions)


def row(store, fid):
    with store.pool.connection() as c:
        return c.execute('SELECT status,status_note,fixed_in,spec_ref,duplicate_of,status_changed_at FROM agent_feedback '
            'WHERE id=%s', (fid,)).fetchone()


def triage(store, token, fid, status, expected_status='received', expected_changed_at=None, request_id=None, **options):
    return store.set_feedback_triage(token, fid, status, expected_status, expected_changed_at,
        request_id or 'triage-' + uuid4().hex[:12], **options)


def requests(store):
    with store.pool.connection() as c:
        return c.execute('SELECT key_id,request_id,feedback_id,status,spec_ref FROM feedback_triage_requests '
            'ORDER BY created_at').fetchall()


def code(failure):
    return failure.value.args[0]


def refused(expected, function, *args, **kwargs):
    with pytest.raises(APIError) as failure:
        function(*args, **kwargs)
    assert code(failure) == expected, code(failure)
    return failure.value


def untouched(store, fid, before, events=0):
    assert row(store, fid) == before and requests(store) == [] and len(journal(store)) == events


# spec: ANYM-992
def test_the_write_permission_is_known_gates_the_tool_and_is_never_inherited(modules):
    store = modules[0]
    assert WRITE in PERMISSIONS and OPERATION_PERMISSIONS['set_feedback_triage'] == WRITE
    assert 'set_feedback_triage' in AUDIT_OPERATIONS
    assert effective_permissions([WRITE, 'read'], None) == ['read']  # a NULL ceiling inherits no new permission
    agent = identity(store)
    fid = submit(store, agent, 1)
    before = row(store, fid)
    for token in (agent, store.issue_key('read-only-maintainer', ['telegram-demo'], permissions=['read', READ],
                  key_permissions=['read', READ])):
        refused('forbidden', triage, store, token, fid, 'accepted', spec_ref=SPEC)
    untouched(store, fid, before)
    assert triage(store, writer(store), fid, 'accepted', spec_ref=SPEC)['status'] == 'accepted'  # the permission alone is enough


# spec: ANYM-992
def test_each_allowed_status_follows_the_operators_rules_and_answers_with_the_new_version(modules):
    store = modules[0]
    agent = identity(store)
    first, second, third, fourth = (submit(store, agent, n) for n in range(1, 5))
    token = writer(store)
    accepted = triage(store, token, first, 'accepted', spec_ref=SPEC, note='Planned for the next release')
    assert (accepted['feedback_id'], accepted['status'], accepted['spec_ref'], accepted['duplicate_of'],
            accepted['replayed']) == (first, 'accepted', SPEC, None, False)
    stored = row(store, first)
    assert (stored['status'], stored['status_note'], stored['spec_ref'], stored['fixed_in']) == (
        'accepted', 'Planned for the next release', SPEC, None)
    assert accepted['status_changed_at'] == stored['status_changed_at'].isoformat()
    declined = triage(store, token, second, 'declined')
    assert (row(store, second)['status'], row(store, second)['status_note'], row(store, second)['spec_ref']) == (
        'declined', None, None) and declined['status'] == 'declined'
    by_item = triage(store, token, third, 'duplicate', duplicate_of=first)
    assert row(store, third)['duplicate_of'] == uuid_of(first) and by_item['duplicate_of'] == first
    by_card = triage(store, token, fourth, 'duplicate', spec_ref='ANYM-5', note='Same as the planned card')
    assert (row(store, fourth)['spec_ref'], row(store, fourth)['duplicate_of']) == ('ANYM-5', None) and by_card['spec_ref'] == 'ANYM-5'


def uuid_of(value):
    from uuid import UUID
    return UUID(value)


# spec: ANYM-992
@pytest.mark.parametrize('status', ['fixed', 'verify_requested', 'verified', 'reopened', 'received', 'closed', '', None])
def test_every_other_status_is_refused_and_nothing_is_written(modules, status):
    store = modules[0]
    agent = identity(store)
    fid = submit(store, agent, 1)
    before = row(store, fid)
    error = refused('invalid_feedback', triage, store, writer(store), fid, status, spec_ref=SPEC)
    assert any(item['field'] == 'status' for item in error.field_errors)
    untouched(store, fid, before)


# spec: ANYM-992
def test_the_operators_rules_for_the_three_statuses_refuse_with_the_field_and_write_nothing(modules):
    store = modules[0]
    agent, other = identity(store), identity(store, 'other-reporter')
    fid, twin, foreign = submit(store, agent, 1), submit(store, agent, 2), submit(store, other, 3)
    token = writer(store)
    before = row(store, fid)
    cases = {
        'spec_ref': [dict(status='accepted'), dict(status='accepted', spec_ref='card'), dict(status='accepted', spec_ref='ANYM-0'),
                     dict(status='declined', spec_ref='ANYM-1234567')],
        'duplicate_of': [dict(status='duplicate'), dict(status='accepted', spec_ref=SPEC, duplicate_of=twin),
                         dict(status='declined', duplicate_of=twin), dict(status='duplicate', duplicate_of='not-a-uuid')],
        'note': [dict(status='declined', note=''), dict(status='declined', note='x' * 281), dict(status='declined', note='  '),
                 dict(status='declined', note='line\nbreak'), dict(status='declined', note='tab\there'),
                 dict(status='declined', note='nul\x00')],
        'expected_changed_at': [dict(status='declined', expected_changed_at='yesterday'),
                                dict(status='declined', expected_changed_at='2026-11-12T09:00:00'),
                                dict(status='declined', expected_changed_at=7)],
        'expected_status': [dict(status='declined', expected_status='closed'), dict(status='declined', expected_status=None)],
        'request_id': [dict(status='declined', request_id='short'), dict(status='declined', request_id='bad id with spaces'),
                       dict(status='declined', request_id='x' * 129)],
        'feedback_id': [dict(status='declined', fid='not-a-uuid'), dict(status='declined', fid='')],
    }
    for field, attempts in cases.items():
        for attempt in attempts:
            arguments = dict(attempt)
            target = arguments.pop('fid', fid)
            status = arguments.pop('status')
            error = refused('invalid_feedback', triage, store, token, target, status, **arguments)
            assert field in {item['field'] for item in error.field_errors}, (field, attempt)
    # A duplicate names the same key's other item, never itself or a stranger's: the function says so.
    for target in (fid, foreign, str(uuid4())):
        refused('invalid_feedback', triage, store, token, fid, 'duplicate', duplicate_of=target)
    assert before == row(store, fid) and requests(store) == [] and journal(store) == []
    refused('not_found', triage, store, token, str(uuid4()), 'declined')


# spec: ANYM-992
def test_a_note_loses_every_url_and_path_before_it_is_stored_and_a_blank_one_is_stored_as_none(modules):
    store = modules[0]
    agent = identity(store)
    token = writer(store)
    canaries = ['https://canary.example/secret?x=1', 'http://127.0.0.1:8080/a', 'ftp://host/file', 'www.canary.example/p',
                '/etc/passwd', '~/notes/plan.md', './tmp/x', '../up/two', 'services/messaging/feedback.py', 'C:\\Users\\canary\\x.txt']
    for number, canary in enumerate(canaries, 1):
        fid = submit(store, agent, number)
        triage(store, token, fid, 'declined', note='Out of scope, see %s for details and/or later' % canary)
        stored = row(store, fid)['status_note']
        assert stored == 'Out of scope, see for details and/or later', (canary, stored)
    blank = submit(store, agent, 20)
    answer = triage(store, token, blank, 'declined', note='https://canary.example/only')
    assert row(store, blank)['status_note'] is None and answer['status'] == 'declined'
    assert scrub_note('yes/no and a/b stay') == 'yes/no and a/b stay'
    listed = {item['id']: item for item in call(store, agent, 'list_feedback')['items']}
    assert 'canary' not in json.dumps(listed, default=str)


# spec: ANYM-992
def test_compare_and_set_refuses_a_stale_version_a_stale_status_and_an_item_in_an_evidence_state(modules):
    store = modules[0]
    agent = identity(store)
    token = writer(store)
    fid = submit(store, agent, 1)
    first = triage(store, token, fid, 'accepted', spec_ref=SPEC)
    version, events = first['status_changed_at'], len(journal(store))
    before = row(store, fid)
    # never-changed expects null; a changed item with null, another status or another time is a lost race
    refused('version_conflict', triage, store, token, fid, 'declined', 'accepted', None)
    refused('version_conflict', triage, store, token, fid, 'declined', 'received', version)
    refused('version_conflict', triage, store, token, fid, 'declined', 'accepted', '2026-11-12T09:00:00+00:00')
    assert row(store, fid) == before and len(requests(store)) == 1 and len(journal(store)) == events
    assert triage(store, token, fid, 'declined', 'accepted', version)['status'] == 'declined'
    # the operator moves the item on meanwhile: the stale caller loses, the item is never overwritten
    other = submit(store, agent, 2)
    stale = triage(store, token, other, 'accepted', spec_ref=SPEC)
    store.set_feedback_status([other], 'fixed', spec_ref=SPEC, fixed_in=MERGED)
    fixed = row(store, other)
    refused('version_conflict', triage, store, token, other, 'declined', 'accepted', stale['status_changed_at'])
    # the evidence states are locked even for a caller who has read them correctly
    for state in ('fixed', 'verify_requested'):
        refused('version_conflict', triage, store, token, other, 'declined', state, fixed['status_changed_at'].isoformat())
        store.set_feedback_status([other], 'verify_requested', spec_ref=SPEC, fixed_in=MERGED)
        fixed = row(store, other)
    assert row(store, other)['status'] == 'verify_requested' and row(store, other)['fixed_in'] == MERGED
    with store.pool.connection() as c:
        c.execute("UPDATE agent_feedback SET status='verified' WHERE id=%s", (other,))
        verified = row(store, other)
    refused('version_conflict', triage, store, token, other, 'declined', 'verified', verified['status_changed_at'].isoformat())


# spec: ANYM-992
def test_two_concurrent_writes_of_one_item_let_one_win_and_the_other_lose_the_race(modules):
    store = modules[0]
    agent = identity(store)
    token = writer(store)
    fid = submit(store, agent, 1)
    start = threading.Barrier(2)

    def attempt(status, **options):
        start.wait()
        try:
            return triage(store, token, fid, status, **options)
        except APIError as failure:
            return failure.args[0]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda args: attempt(args[0], **args[1]),
            [('accepted', {'spec_ref': SPEC}), ('declined', {})]))
    assert sorted(type(result).__name__ for result in results) == ['dict', 'str']
    assert [result for result in results if isinstance(result, str)] == ['version_conflict']
    assert len(requests(store)) == 1 and len(journal(store)) == 1
    assert row(store, fid)['status'] == next(result['status'] for result in results if isinstance(result, dict))


# spec: ANYM-992
def test_a_request_id_is_idempotent_per_key_and_payload_whatever_the_item_looks_like_now(modules):
    store = modules[0]
    agent = identity(store)
    token = writer(store)
    fid = submit(store, agent, 1)
    first = triage(store, token, fid, 'accepted', request_id='triage-request-1', spec_ref=SPEC, note='Planned')
    events = len(journal(store))
    moved = triage(store, token, fid, 'declined', 'accepted', first['status_changed_at'], note='Changed my mind')
    assert moved['status'] == 'declined'
    after, count = row(store, fid), len(requests(store))
    # The replay answers from the stored record, though the item has moved on and the CAS would now fail.
    again = triage(store, token, fid, 'accepted', request_id='triage-request-1', spec_ref=SPEC, note='Planned')
    assert again == {**first, 'replayed': True} and row(store, fid) == after
    refused('idempotency_conflict', triage, store, token, fid, 'accepted', request_id='triage-request-1', spec_ref='ANYM-7')
    refused('idempotency_conflict', triage, store, token, fid, 'accepted', request_id='triage-request-1', spec_ref=SPEC,
        note='Another note')
    assert row(store, fid) == after and len(requests(store)) == count and len(journal(store)) == events + 1
    # the same instant written another way (Z for +00:00) is the same payload
    second = submit(store, agent, 5)
    start = triage(store, token, second, 'accepted', spec_ref=SPEC)
    plus = triage(store, token, second, 'declined', 'accepted', start['status_changed_at'], request_id='triage-request-z')
    assert triage(store, token, second, 'declined', 'accepted', start['status_changed_at'].replace('+00:00', 'Z'),
        request_id='triage-request-z')['replayed'] is True and plus['replayed'] is False
    # another key may use the same ID: nothing is shared between keys
    second = writer(store, 'second-writer')
    elsewhere = submit(store, agent, 2)
    assert triage(store, second, elsewhere, 'accepted', request_id='triage-request-1', spec_ref=SPEC)['replayed'] is False


# spec: ANYM-992
def test_a_changed_status_publishes_the_status_event_once_and_a_refusal_or_replay_none(modules):
    store = modules[0]
    reporter = identity(store)
    maintainer = key_id(store, reporter)
    own = feedback_subscription(store, reporter, {'statuses': ['accepted', 'declined']})
    fid = submit(store, reporter, 1)
    token = writer(store)
    first = triage(store, token, fid, 'accepted', request_id='event-request-1', spec_ref=SPEC)
    [event] = journal(store)
    assert (event['type'], event['subject'], event['scope_ref'], event['urgency'], event['expires_at']) == (
        'feedback.accepted', fid, maintainer, 'now', None)
    assert event['data'] == {'feedback_id': fid, 'status': 'accepted', 'previous_status': 'received', 'spec_ref': SPEC}
    assert event['event_key'] == 'feedback:%d' % event['seq']
    triage(store, token, fid, 'accepted', request_id='event-request-1', spec_ref=SPEC)  # replay
    with pytest.raises(APIError):
        triage(store, token, fid, 'declined')  # stale
    assert len(journal(store)) == 1
    assert matched(store, reporter, own) == [('feedback.accepted', fid)]
    # the same status with a new note replaces the disposition and publishes nothing (as the operator's)
    triage(store, token, fid, 'accepted', 'accepted', first['status_changed_at'], spec_ref=SPEC, note='Still planned')
    assert row(store, fid)['status_note'] == 'Still planned' and len(journal(store)) == 1
    triage(store, token, fid, 'declined', 'accepted', row(store, fid)['status_changed_at'].isoformat(), note='Out of scope')
    assert [item['type'] for item in journal(store)] == ['feedback.accepted', 'feedback.declined']
    assert journal(store)[1]['data'] == {'feedback_id': fid, 'status': 'declined', 'previous_status': 'accepted'}


# spec: ANYM-992
def test_a_publish_error_keeps_the_write_and_is_counted(modules):
    store = modules[0]
    reporter = identity(store)
    fid = submit(store, reporter, 1)
    with store.pool.connection() as c:
        head = c.execute('SELECT value FROM module_event_counter WHERE id=1').fetchone()['value']
        # a stored event under the next key with other fields: the publisher refuses it inside the function's sub-block
        c.execute("""INSERT INTO module_events(seq,event_key,source,type,subject,scope_ref,occurred_at,data,urgency)
            VALUES(%s,%s,'deedkit:feedback','feedback.declined','x','y',now(),'{"feedback_id":"x","status":"declined",
            "previous_status":"received"}','now')""", (head + 1000, 'feedback:%d' % (head + 1)))
    before = JOURNAL_FAILURES[feedback_events.FAILURE_KIND]
    try:
        answer = triage(store, writer(store), fid, 'accepted', spec_ref=SPEC)
        assert answer['status'] == 'accepted' and row(store, fid)['status'] == 'accepted' and len(requests(store)) == 1
        assert JOURNAL_FAILURES[feedback_events.FAILURE_KIND] == before + 1
        assert [item['type'] for item in journal(store)] == ['feedback.declined']  # only the planted row
    finally:
        with store.pool.connection() as c:
            c.execute('DELETE FROM module_events WHERE seq=%s', (head + 1000,))


# spec: ANYM-992
def test_the_tool_and_the_operator_command_store_the_same_disposition_and_publish_the_same_event(modules):
    store = modules[0]
    reporter = identity(store)
    target = submit(store, reporter, 99)
    token = writer(store)
    grid = [('accepted', dict(spec_ref=SPEC)), ('accepted', dict(spec_ref=SPEC, note='Planned')),
            ('declined', {}), ('declined', dict(note='Out of scope')), ('declined', dict(spec_ref='ANYM-5', note='Later')),
            ('duplicate', dict(duplicate_of=target)), ('duplicate', dict(spec_ref='ANYM-5')),
            ('duplicate', dict(duplicate_of=target, spec_ref='ANYM-5', note='Same as'))]
    for number, (status, options) in enumerate(grid, 1):
        via_tool, via_operator = submit(store, reporter, 100 + number), submit(store, reporter, 200 + number)
        triage(store, token, via_tool, status, **options)
        store.set_feedback_status([via_operator], status, **options)
        a, b = row(store, via_tool), row(store, via_operator)
        assert {k: v for k, v in a.items() if k != 'status_changed_at'} == {k: v for k, v in b.items() if k != 'status_changed_at'}
        assert a['status_changed_at'] is not None and b['status_changed_at'] is not None
        events = [e for e in journal(store) if e['subject'] in (via_tool, via_operator)]
        assert len(events) == 2 and {e['scope_ref'] for e in events} == {key_id(store, reporter)}
        shape = lambda e: (e['type'], {k: v for k, v in e['data'].items() if k != 'feedback_id'}, e['urgency'], e['expires_at'])  # noqa: E731
        assert shape(events[0]) == shape(events[1]), (status, options)
    # a second step from a stored disposition: fixed_in and the old note are replaced, never kept
    one, two = submit(store, reporter, 300), submit(store, reporter, 301)
    first = triage(store, token, one, 'accepted', spec_ref=SPEC, note='Planned')
    store.set_feedback_status([two], 'accepted', spec_ref=SPEC, note='Planned')
    triage(store, token, one, 'declined', 'accepted', first['status_changed_at'])
    store.set_feedback_status([two], 'declined')
    assert {k: v for k, v in row(store, one).items() if k != 'status_changed_at'} == {
        k: v for k, v in row(store, two).items() if k != 'status_changed_at'}


# spec: ANYM-992
def test_the_api_role_changes_a_status_through_the_function_alone_and_the_function_is_no_wider_than_the_tool(modules):
    store = modules[0]
    api = install(store, 'gateway_role.sql', 'messaging_gateway')
    plain = 'anym992_plain_' + uuid4().hex[:8]
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
        reporter = identity(store)
        fid = submit(store, reporter, 1)
        token = writer(store)
        assert gateway.set_feedback_triage(token, fid, 'accepted', 'received', None, 'role-request-1', spec_ref=SPEC)[
            'status'] == 'accepted'
        assert [item['type'] for item in journal(store)] == ['feedback.accepted']
        for statement in ("UPDATE agent_feedback SET status='declined'", "UPDATE agent_feedback SET status_note='x'",
                          'SELECT * FROM feedback_triage_requests', 'DELETE FROM feedback_triage_requests',
                          "INSERT INTO feedback_triage_requests(key_id,request_id,request_hash,feedback_id,status,status_changed_at)"
                          " VALUES(gen_random_uuid(),'x'||repeat('y',8),repeat('a',64),gen_random_uuid(),'declined',now())"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), as_role(store, api) as c:
                c.execute(statement)
        call_text = 'SELECT public.set_feedback_triage(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)'
        with pytest.raises(psycopg.errors.InsufficientPrivilege), as_role(store, plain) as c:
            c.execute(call_text, (uuid4(), 'plain-request-1', 'a' * 64, fid, 'declined', 'accepted', None, None, None, None))
        # Called directly by the API role the function still refuses a status outside the three and an evidence state.
        for status in ('fixed', 'verify_requested', 'verified', 'reopened', 'received'):
            with pytest.raises(psycopg.errors.RaiseException, match='invalid_feedback'), as_role(store, api) as c:
                c.execute(call_text, (uuid4(), 'direct-request-1', 'a' * 64, fid, status, 'accepted', None, SPEC, None, None))
        with pytest.raises(psycopg.errors.RaiseException, match='version_conflict'), as_role(store, api) as c:
            c.execute(call_text, (uuid4(), 'direct-request-2', 'a' * 64, fid, 'declined', 'received', None, None, None, None))
    finally:
        remove(store, api)
        with store.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(plain)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(plain)))


# spec: ANYM-992
def test_rest_and_mcp_carry_the_write_and_capabilities_name_it_only_to_a_holder(modules):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    from test_client_contract import rpc
    store = modules[0]
    reporter, plain = identity(store), identity(store, 'plain-agent')
    fid = submit(store, reporter, 1)
    token = writer(store)
    with_read = store.issue_key('write-and-read', ['telegram-demo'], permissions=['read', WRITE], key_permissions=['read', WRITE])
    assert 'set_feedback_triage' in store.capabilities(token, detail='full')['available']
    assert 'set_feedback_triage' in store.capabilities(with_read, detail='full')['available']
    assert 'set_feedback_triage' not in store.capabilities(plain, detail='full')['available']
    headers = {'Authorization': 'Bearer ' + token}
    body = {'feedback_id': fid, 'status': 'accepted', 'expected_status': 'received', 'expected_changed_at': None,
            'request_id': 'rest-request-1', 'spec_ref': SPEC}
    with TestClient(create_app(store, ['testserver'])) as client:
        done = client.post('/v1/set_feedback_triage', headers=headers, json=body)
        assert done.status_code == 200 and done.json()['status'] == 'accepted' and done.json()['replayed'] is False
        assert client.post('/v1/set_feedback_triage', headers=headers, json=body).json()['replayed'] is True
        stale = client.post('/v1/set_feedback_triage', headers=headers, json={**body, 'status': 'declined',
            'request_id': 'rest-request-2', 'spec_ref': None})
        assert stale.status_code == 409 and stale.json()['error'] == 'version_conflict'
        changed = client.post('/v1/set_feedback_triage', headers=headers, json={**body, 'spec_ref': 'ANYM-7'})
        assert changed.status_code == 409 and changed.json()['error'] == 'idempotency_conflict'
        for bad in ({**body, 'status': 'fixed', 'request_id': 'rest-request-3'}, {**body, 'extra': 1},
                    {k: v for k, v in body.items() if k != 'expected_changed_at'}):
            assert client.post('/v1/set_feedback_triage', headers=headers, json=bad).status_code in (400, 422)
        denied = client.post('/v1/set_feedback_triage', headers={'Authorization': 'Bearer ' + plain}, json=body)
        assert denied.status_code == 403
        second = submit(store, reporter, 2)
        mcp = rpc(client, token, 'set_feedback_triage', {**body, 'feedback_id': second, 'request_id': 'mcp-request-1'}).json()['result']
        assert not mcp.get('isError') and mcp['structuredContent']['status'] == 'accepted' and row(store, second)['status'] == 'accepted'
        assert rpc(client, plain, 'set_feedback_triage', body).json()['result']['isError'] is True


# spec: ANYM-992
def test_migration_096_widens_the_lists_adds_one_table_and_one_function_and_replays(modules):
    store = modules[0]
    text = (SOURCE / 'migrations' / '096_feedback_triage_write.sql').read_text()
    assert text.startswith('-- rollout: manual') and 'Risk tier: non-critical' in text
    assert sorted(re.findall(r'DROP\s+CONSTRAINT\s+(\w+)', text)) == ['audit_log_operation_check', 'key_supported_permissions',
        'supported_permissions']
    assert not re.search(r'\b(DELETE\s+FROM|TRUNCATE|DROP\s+(TABLE|COLUMN|FUNCTION)|ALTER\s+TABLE\s+agent_feedback)\b', text, re.I)
    assert len(re.findall(r'^CREATE\s+TABLE', text, re.I | re.M)) == 1 and len(re.findall(r'^CREATE\s+(OR\s+REPLACE\s+)?FUNCTION', text, re.I | re.M)) == 1
    old = (SOURCE / 'migrations' / '094_feedback_projections.sql').read_text()
    old_names = set(re.findall(r"'([a-z_-]+)'", old[old.index('supported_permissions CHECK'):old.index('ALTER TABLE api_keys')]))
    function = "'public.set_feedback_triage(uuid,text,text,uuid,text,text,timestamptz,text,uuid,text)'::regprocedure"
    query = f'SELECT proacl::text AS acl, prosecdef AS definer, proowner::regrole::text AS owner FROM pg_proc WHERE oid={function}'
    shape = """SELECT conrelid::regclass::text AS t, conname, pg_get_constraintdef(oid) AS d FROM pg_constraint
        WHERE connamespace='public'::regnamespace ORDER BY 1,2"""
    columns = "SELECT column_name,data_type FROM information_schema.columns WHERE table_name='feedback_triage_requests' ORDER BY 1"
    with store.pool.connection() as c:
        before, constraints, table = c.execute(query).fetchone(), c.execute(shape).fetchall(), c.execute(columns).fetchall()
        assert before['definer'] and not re.search(r'[{,]=X/', before['acl'] or '')  # PUBLIC has no EXECUTE
        for name, definition in ((row['conname'], row['d']) for row in constraints
                                 if row['conname'] in ('supported_permissions', 'key_supported_permissions')):
            assert WRITE in definition and all(permission in definition for permission in old_names if permission.islower()
                and '-' in permission or permission in ('read', 'send')), name
        audit = next(row['d'] for row in constraints if row['conname'] == 'audit_log_operation_check')
        assert "'set_feedback_triage'" in audit and "'export_feedback_metadata'" in audit
        c.execute(text)  # a repeated run changes nothing
        assert c.execute(query).fetchone() == before and c.execute(shape).fetchall() == constraints
        assert c.execute(columns).fetchall() == table
    # a deleted item takes its request rows with it
    reporter = identity(store)
    fid = submit(store, reporter, 1)
    triage(store, writer(store), fid, 'declined')
    assert len(requests(store)) == 1
    with store.pool.connection() as c:
        c.execute('DELETE FROM agent_feedback WHERE id=%s', (fid,))
    assert requests(store) == []
