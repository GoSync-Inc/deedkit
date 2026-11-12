"""Feedback status events (ANYM-962): the reporting key is woken when the operator changes its report's status."""
import json
from pathlib import Path
import re

from hypothesis import given, settings, strategies as st
import pytest

from messaging_service import feedback_events, module_events
from messaging_service import store as store_module
from messaging_service.feedback import FIXED_IN, SPEC_REF
from messaging_service.module_events import KINDS, ModuleContractError, validate_event
from messaging_service.store import APIError
from messaging_service.subscriptions import JOURNAL_FAILURES, match_subscriptions
from test_event_delivery import call, identity

SOURCE = Path(store_module.__file__).parent
MERGED = 'deedkit@' + 'a' * 40
FEEDBACK_ID = '0b8f7c1e-3d0a-4c55-9e1a-2f4f6f2b9a10'
PUBLIC = lambda *a, **k: ['93.184.216.34']  # noqa: E731


@pytest.fixture()
def modules(seeded):
    store = seeded[0]
    with store.pool.connection() as c:
        c.execute('TRUNCATE module_events,module_source_health CASCADE')
        c.execute('UPDATE module_event_counter SET value=0 WHERE id=1')
        c.execute('TRUNCATE agent_feedback CASCADE')
    return seeded


def submit(store, token, n):
    return call(store, token, 'submit_feedback', 'synthetic-feedback-%d' % n, 'bug', 'Synthetic report %d' % n)['feedback_id']


def journal(store):
    """The status events (ANYM-962); the submission events of ANYM-989 are read by `submissions`."""
    with store.pool.connection() as c:
        return c.execute('SELECT seq,event_key,source,type,subject,scope_ref,data,urgency,expires_at FROM module_events '
            "WHERE NOT (type='feedback.received' AND NOT data ? 'previous_status') ORDER BY seq").fetchall()


def key_id(store, token):
    with store.authorized(token) as (_, p):
        return str(p['key_id'])


def feedback_event(status='fixed', **data):
    previous = 'received' if status != 'received' else 'accepted'
    values = {'feedback_id': FEEDBACK_ID, 'status': status, 'previous_status': previous, 'spec_ref': 'ANYM-962',
        'fixed_in': MERGED, **data}
    values = {key: value for key, value in values.items() if value is not None}
    return {'specversion': '1.0', 'id': 'feedback:1', 'source': 'deedkit:feedback', 'type': 'feedback.' + status,
        'subject': values.get('feedback_id'), 'time': '2026-11-11T10:00:00+00:00', 'data': values, 'urgency': 'now',
        'expires_at': None}


def both(c, event):
    try:
        validate_event(event)
        python = True
    except ModuleContractError:
        python = False
    database = c.execute('SELECT public.module_event_valid(%s,%s,%s,%s,%s,%s,%s,%s) AS ok', (event['id'],
        event['source'], event['type'], event['subject'], event['time'], json.dumps(event['data']),
        event['urgency'], event['expires_at'])).fetchone()['ok']
    return python, database


def _feedback_cases():
    event = feedback_event()
    return {
        **{'valid_' + status: feedback_event(status, fixed_in=MERGED if status in ('fixed', 'verify_requested') else None)
            for status in feedback_events.STATUSES},
        'valid_without_spec_and_release': feedback_event('declined', spec_ref=None, fixed_in=None),
        'valid_from_an_agent_verdict': feedback_event('fixed', previous_status='reopened'),
        'valid_semver_release': feedback_event('fixed', fixed_in='anymsg@1.2.3-rc.1+build.7'),
        'unchanged_status': feedback_event('fixed', previous_status='fixed'),
        'note_key': {**event, 'data': {**event['data'], 'note': 'private words'}},
        'principal_key': {**event, 'data': {**event['data'], 'principal': 'agent-a'}},
        'status_of_another_type': {**event, 'type': 'feedback.declined'},
        'agent_verdict_type': {**event, 'type': 'feedback.verified', 'data': {**event['data'], 'status': 'verified'}},
        'previous_outside_enum': feedback_event('fixed', previous_status='closed'),
        'free_text_spec': feedback_event('fixed', spec_ref='the calls card'),
        'free_text_release': feedback_event('fixed', fixed_in='main'),
        'release_too_long': feedback_event('fixed', fixed_in='anymsg@1.0.0+' + 'b' * 70),
        'batched': {**event, 'urgency': 'batch'}, 'expiring': {**event, 'expires_at': '2099-01-01T00:00:00Z'},
        'subject_mismatch': {**event, 'subject': 'other'}, 'missing_previous': feedback_event('fixed', previous_status=None),
        'id_of_calls': {**event, 'id': 'calls:1'}, 'calls_source': {**event, 'id': 'calls:1', 'source': 'deedkit:calls'},
        'call_type_under_feedback': {**event, 'type': 'call.interrupted',
            'data': {'call_id': 'call-1', 'end_reason': 'service_restart'}, 'subject': 'call-1'},
        'id_above_2_53': {**event, 'id': 'feedback:9007199254740992'},
        **_submission_cases(),
    }


def submission_event(**changes):
    """ANYM-989: a feedback.received event of a submission: no previous status, a category."""
    data = {'feedback_id': FEEDBACK_ID, 'status': 'received', 'category': 'bug'}
    event = feedback_event('received')
    return {**event, 'data': {**data, **changes.pop('data', {})}, **changes}


def projection_event(**data):
    event = feedback_event('received')
    return {**event, 'type': 'feedback.projection_recorded',
        'data': {'feedback_id': FEEDBACK_ID, 'classification': 'reported-bug', **data}}


def _submission_cases():
    """ANYM-989: the two new shapes and what stays refused beside them."""
    operator = feedback_event('received')  # the operator's change back to received: previous status, no category
    return {
        'valid_submission': submission_event(),
        **{'valid_submission_' + category: submission_event(data={'category': category})
            for category in ('bug', 'feature', 'quality', 'other')},
        'valid_operator_change_back_to_received': operator,
        'valid_projection_recorded': projection_event(),
        **{'valid_projection_' + name: projection_event(classification=name) for name in
            ('reported-bug', 'feature', 'question', 'not-actionable', 'already-live', 'enable-only')},
        **{'submission_with_' + key: submission_event(data={key: value}) for key, value in
            (('description', 'private words'), ('note', 'private words'), ('refs', 'x'), ('principal', 'agent-a'),
             ('previous_status', 'accepted'), ('spec_ref', 'ANYM-962'), ('fixed_in', MERGED))},
        'submission_without_category': {**submission_event(), 'data': {'feedback_id': FEEDBACK_ID, 'status': 'received'}},
        'submission_with_a_new_category': submission_event(data={'category': 'idea'}),
        'operator_received_with_a_category': {**operator, 'data': {**operator['data'], 'category': 'bug'}},
        'operator_received_unchanged': feedback_event('received', previous_status='received'),
        'submission_of_another_type': {**submission_event(), 'type': 'feedback.accepted'},
        'submission_batched': submission_event(urgency='batch'),
        'submission_expiring': submission_event(expires_at='2099-01-01T00:00:00Z'),
        'accepted_without_previous': {**feedback_event('accepted'), 'data': {'feedback_id': FEEDBACK_ID, 'status': 'accepted',
            'category': 'bug'}},
        'projection_with_a_status': projection_event(status='received'),
        'projection_with_a_brief': projection_event(released_brief='private words'),
        'projection_with_a_new_classification': projection_event(classification='bogus'),
        'projection_without_classification': {**projection_event(), 'data': {'feedback_id': FEEDBACK_ID}},
        'projection_batched': {**projection_event(), 'urgency': 'batch'},
        'projection_expiring': {**projection_event(), 'expires_at': '2099-01-01T00:00:00Z'},
        'projection_under_calls': {**projection_event(), 'id': 'calls:1', 'source': 'deedkit:calls'},
    }


# spec: ANYM-962
def test_feedback_event_contract_is_one_in_the_python_check_and_the_definer_function(modules):
    store = modules[0]
    with store.pool.connection() as c:
        for name, event in _feedback_cases().items():
            assert both(c, event) == (name.startswith('valid_'),) * 2, name
    assert {'feedback.' + status for status in feedback_events.STATUSES} | {'feedback.projection_recorded'} == {
        kind for kind in KINDS if kind.startswith('feedback.')}


# jsonb holds no NUL character, so no stored event can carry one.
TEXT = st.characters(exclude_characters='\x00')


# spec: ANYM-962
@settings(max_examples=150, deadline=None)
@given(release=st.one_of(st.from_regex(FIXED_IN, fullmatch=True), st.text(TEXT, max_size=90),
    st.from_regex(FIXED_IN, fullmatch=True).map(lambda value: value + '!')),
    spec=st.one_of(st.from_regex(SPEC_REF, fullmatch=True), st.text(TEXT, max_size=12)))
def test_spec_and_release_values_get_the_same_verdict_in_python_and_in_the_database(database, release, spec):
    # Oracle: the operator command's own patterns (feedback.FIXED_IN, SPEC_REF) and the definer function agree.
    event = feedback_event('fixed', fixed_in=release, spec_ref=spec)
    expected = (len(release) <= 80 and FIXED_IN.fullmatch(release) is not None and SPEC_REF.fullmatch(spec) is not None)
    with database.pool.connection() as c:
        assert both(c, event) == (expected, expected)


OLD_LISTS = {
    'module_events': [
        ('module_events_event_key_check', "event_key ~ '^(calendar|calls|recordings):[1-9][0-9]{0,17}$'"),
        ('module_events_source_check', "source IN ('deedkit:calendar','deedkit:calls','deedkit:recordings')"),
        ('module_events_type_check', "type IN ('calendar.event.created','calendar.event.changed','calendar.event.cancelled',"
            "'calendar.event.starting','call.finished','call.failed','call.not_answered','call.interrupted',"
            "'call.owner_request','recording.text.archived','recording.access.changed','call.access.changed')"),
        ('module_events_check', "split_part(event_key,':',1)=split_part(source,':',2)"),
        ('module_events_check1', "split_part(type,'.',1)=CASE source WHEN 'deedkit:calendar' THEN 'calendar' "
            "WHEN 'deedkit:recordings' THEN 'recording' ELSE 'call' END")],
    **{table: [(f'{table}_source_check', "source IN ('deedkit:calendar','deedkit:calls','deedkit:recordings')")]
        for table in ('subscription_module_sources', 'module_source_health', 'module_router_cursors', 'module_router_gaps')}}


def _expression(c, table, name):
    found = c.execute("SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint WHERE conrelid=%s::regclass AND conname=%s",
        (table, name)).fetchone()
    assert found is not None and found['d'].startswith('CHECK '), (table, name)
    return found['d'][len('CHECK '):]


# spec: ANYM-962
def test_every_widened_module_list_admits_what_the_old_one_did_and_feedback_only_under_its_own_source(modules):
    store = modules[0]
    names = ('calendar', 'calls', 'recordings', 'feedback', 'messages')
    keys = [f'{m}:{n}' for m in names for n in ('1', '0', '01', '9' * 18, '9' * 19)]
    sources = [f'deedkit:{m}' for m in names] + ['', 'feedback']
    types = sorted(set(KINDS) | {'call.access.changed'}) + ['feedback.verified', 'feedback.', 'call.', 'messages.x']
    grid = [(k, s, t) for k in keys for s in sources for t in types]
    with store.pool.connection() as c:
        def evaluate(expression):
            return [row['ok'] for row in c.execute(f'SELECT ({expression}) AS ok FROM unnest(%s::text[], %s::text[], '
                '%s::text[]) AS t(event_key, source, type)', [[row[i] for row in grid] for i in range(3)]).fetchall()]
        for table, olds in OLD_LISTS.items():
            before = evaluate(' AND '.join(f'({expression})' for _, expression in olds))
            after = evaluate(' AND '.join(_expression(c, table, name) for name, _ in olds))
            assert not [row for row, was, now in zip(grid, before, after) if was and not now], table
            admitted = {row for row, now in zip(grid, after) if now}
            if table == 'module_events':
                for status in feedback_events.STATUSES:
                    assert ('feedback:1', 'deedkit:feedback', 'feedback.' + status) in admitted
                assert not {row for row in admitted if row[1] == 'deedkit:feedback' and (
                    not row[0].startswith('feedback:') or not row[2].startswith('feedback.'))}
                assert ('feedback:1', 'deedkit:feedback', 'call.finished') not in admitted
                assert not {row for row in admitted if row[0].startswith('feedback:') and row[1] != 'deedkit:feedback'}
                assert not {row for row in admitted if row[2] == 'feedback.verified'}
            else:
                assert {row[1] for row in admitted} == {f'deedkit:{m}' for m in names[:4]}, table
        stale = c.execute("""SELECT conrelid::regclass::text AS t, conname FROM pg_constraint WHERE contype='c'
            AND connamespace='public'::regnamespace AND pg_get_constraintdef(oid) ~ 'deedkit:calls|calendar\\|calls'
            AND pg_get_constraintdef(oid) !~ 'feedback'""").fetchall()
        assert stale == []
        functions = c.execute("""SELECT proname, prosrc ~ 'deedkit:feedback' AS names_it FROM pg_proc
            WHERE pronamespace='public'::regnamespace AND prosrc ~ 'deedkit:calls|calendar\\|calls|call\\.finished'""").fetchall()
        assert [row['proname'] for row in functions] == ['module_event_valid'] and functions[0]['names_it']


# spec: ANYM-962
def test_migration_091_only_widens_the_lists_and_replays_without_touching_acls(modules):
    store = modules[0]
    text = (SOURCE / 'migrations' / '091_feedback_module_source.sql').read_text()
    assert text.startswith('-- rollout: manual') and 'Risk tier: non-critical' in text
    assert not re.search(r'\b(GRANT|REVOKE|CREATE\s+(TABLE|INDEX|SEQUENCE)|INSERT|UPDATE|DELETE|TRUNCATE)\b', text, re.I)
    dropped = re.findall(r'DROP\s+CONSTRAINT\s+(\w+)', text)
    assert sorted(dropped) == sorted(name for olds in OLD_LISTS.values() for name, _ in olds if name != 'module_events_check')
    assert len(re.findall(r'\bDROP\b', text, re.I)) == len(dropped)
    function = "'public.module_event_valid(text,text,text,text,text,jsonb,text,text)'::regprocedure"
    query = f'SELECT proacl::text AS acl, proowner::regrole::text AS owner FROM pg_proc WHERE oid={function}'
    shape = """SELECT conrelid::regclass::text AS t, conname, pg_get_constraintdef(oid) AS d FROM pg_constraint
        WHERE connamespace='public'::regnamespace ORDER BY 1,2"""
    with store.pool.connection() as c:
        acl, constraints = c.execute(query).fetchone(), c.execute(shape).fetchall()
        c.execute(text)  # a repeated run (the module tests re-run migrations) changes nothing...
        c.execute((SOURCE / 'migrations' / '093_module_access_changed.sql').read_text())  # ...once the later one follows
        c.execute((SOURCE / 'migrations' / '095_feedback_submission_events.sql').read_text())  # (ANYM-989) and its successor
        assert c.execute(query).fetchone() == acl and c.execute(shape).fetchall() == constraints


# spec: ANYM-962
def test_a_changed_status_publishes_one_event_per_item_and_a_kept_status_none(modules):
    store = modules[0]
    token = identity(store)
    first, second = submit(store, token, 1), submit(store, token, 2)
    store.set_feedback_status([first, second], 'accepted', spec_ref='ANYM-962', note='Planned')
    rows = journal(store)
    assert [(row['source'], row['type'], row['subject']) for row in rows] == [
        ('deedkit:feedback', 'feedback.accepted', fid) for fid in sorted([first, second])]
    assert {row['scope_ref'] for row in rows} == {key_id(store, token)}
    assert [row['event_key'] for row in rows] == ['feedback:%d' % row['seq'] for row in rows]
    assert all(row['urgency'] == 'now' and row['expires_at'] is None for row in rows)
    assert rows[0]['data'] == {'feedback_id': rows[0]['subject'], 'status': 'accepted', 'previous_status': 'received',
        'spec_ref': 'ANYM-962'}
    # The same status with a new note (feedback_sync follow) or a rerun publishes nothing.
    store.set_feedback_status([first, second], 'accepted', spec_ref='ANYM-962', note='Still planned')
    assert len(journal(store)) == 2
    store.set_feedback_status([first], 'fixed', spec_ref='ANYM-962', fixed_in=MERGED)
    last = journal(store)[-1]
    assert last['type'] == 'feedback.fixed' and last['data'] == {'feedback_id': first, 'status': 'fixed',
        'previous_status': 'accepted', 'spec_ref': 'ANYM-962', 'fixed_in': MERGED}
    assert 'Planned' not in json.dumps([dict(row, data=row['data']) for row in journal(store)], default=str)


# spec: ANYM-962
def test_a_failed_publish_keeps_the_status_write_and_is_counted(modules, monkeypatch, capsys):
    store = modules[0]
    token = identity(store)
    fid = submit(store, token, 1)
    def broken(c, event, scope_ref):
        c.execute('SELECT 1/0')
    monkeypatch.setattr(module_events, 'publish', broken)
    before = JOURNAL_FAILURES[feedback_events.FAILURE_KIND]
    assert store.set_feedback_status([fid], 'declined', note='Out of scope')['updated'] == 1
    assert JOURNAL_FAILURES[feedback_events.FAILURE_KIND] == before + 1
    assert journal(store) == []
    assert call(store, token, 'list_feedback')['items'][0]['status'] == 'declined'
    line = [json.loads(text) for text in capsys.readouterr().out.splitlines() if 'subscription_journal_failed' in text]
    assert line == [{'error': 'subscription_journal_failed', 'kind': 'feedback.status', 'class': 'DivisionByZero'}]


# spec: ANYM-962
def test_a_refused_status_call_publishes_nothing(modules):
    from datetime import timedelta
    store = modules[0]
    token = identity(store)
    first, second = submit(store, token, 1), submit(store, token, 2)
    store.set_feedback_status([first], 'accepted', spec_ref='ANYM-962')
    events = journal(store)
    with store.pool.connection() as c:
        now = c.execute('SELECT clock_timestamp() AS t').fetchone()['t']
    with pytest.raises(ValueError, match='not_after_passed'):
        store.set_feedback_status([second], 'declined', not_after=(now - timedelta(seconds=1)).isoformat())
    with pytest.raises(ValueError, match='changed_since_plan'):
        store.set_feedback_status([first, second], 'declined', unchanged_since=(now - timedelta(hours=1)).isoformat())
    assert journal(store) == events


def feedback_subscription(store, token, value=True, **arguments):
    return store.create_subscription(token, feedback=value, **arguments)['subscription_id']


def matched(store, token, sid):
    match_subscriptions(store)
    return [(item['type'], item['data']['feedback_id']) for item in
        store.fetch_subscription_events(token, sid)['module']['items']]


# spec: ANYM-962
def test_a_key_is_woken_by_its_own_reports_only_and_by_default_not_by_accepted(modules):
    store = modules[0]
    mine, sibling = identity(store), identity(store)  # two keys of one principal
    sid = feedback_subscription(store, mine)
    picked = feedback_subscription(store, mine, {'statuses': ['accepted']}, chat_ids=['telegram-demo-chat'])
    own, theirs = submit(store, mine, 1), submit(store, sibling, 1)
    store.set_feedback_status([own, theirs], 'accepted', spec_ref='ANYM-962')
    assert matched(store, mine, sid) == []
    assert matched(store, mine, picked) == [('feedback.accepted', own)]
    store.set_feedback_status([own, theirs], 'fixed', spec_ref='ANYM-962', fixed_in=MERGED)
    store.set_feedback_status([own, theirs], 'verify_requested', spec_ref='ANYM-962', fixed_in=MERGED)
    assert matched(store, mine, sid) == [('feedback.fixed', own), ('feedback.verify_requested', own)]
    view = store.get_subscription(mine, sid)
    assert view['module_stream']['sources'][0] | {'coverage': None} == {'source': 'deedkit:feedback',
        'scope_ref': key_id(store, mine), 'types': sorted('feedback.' + s for s in feedback_events.DEFAULT_STATUSES),
        'coverage': None} | {key: value for key, value in view['module_stream']['sources'][0].items()
            if key not in ('source', 'scope_ref', 'types', 'coverage')}


# spec: ANYM-962
def test_a_feedback_subscription_starts_at_the_head_and_counts_as_one_definition(modules, monkeypatch):
    store = modules[0]
    token = identity(store)
    fid = submit(store, token, 1)
    store.set_feedback_status([fid], 'fixed', spec_ref='ANYM-962', fixed_in=MERGED)  # before the subscription
    monkeypatch.setenv('SUBSCRIPTION_MAX_DEFINITIONS', '1')
    sid = feedback_subscription(store, token)
    assert matched(store, token, sid) == []
    with pytest.raises(APIError) as refused:
        feedback_subscription(store, token)
    assert refused.value.args[0] == 'subscription_limit'


# spec: ANYM-962
@pytest.mark.parametrize('value', [False, 'true', 1, {}, {'statuses': []}, {'statuses': ['verified']},
    {'statuses': ['fixed', 'fixed']}, {'statuses': 'fixed'}, {'statuses': ['fixed'], 'scope_ref': 'other-key'},
    {'statuses': list(feedback_events.STATUSES) + ['reopened']}])
def test_an_invalid_feedback_value_is_invalid_filter_and_writes_nothing(modules, value):
    store = modules[0]
    token = identity(store)
    with pytest.raises(APIError) as refused:
        store.create_subscription(token, feedback=value)
    assert refused.value.args[0] == 'invalid_filter'
    with store.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM subscriptions').fetchone()['n'] == 0


# spec: ANYM-962
def test_a_bound_endpoint_gets_the_feedback_reference_keys_only(modules, tmp_path, monkeypatch):
    from messaging_service.event_delivery import dispatch_hints
    from messaging_service.heartbeats import record
    store = modules[0]
    policy = tmp_path / 'delivery-policy.json'
    policy.write_text(json.dumps({'allow_hosts': ['example.test']}))
    policy.chmod(0o600)
    monkeypatch.setenv('DELIVERY_POLICY_FILE', str(policy))
    store.set_delivery_features(True)
    try:
        permissions = ['read', 'subscription-write', 'subscription-ack', 'delivery-write', 'feedback-write']
        token = store.issue_key('feedback-reader', ['telegram-demo'], permissions=permissions, key_permissions=permissions)
        endpoint = store.register_delivery_endpoint(token, 'endpoint-request-962', 'https://hooks.example.test/hook',
            'whsec_c3ludGhldGljLWVuZHBvaW50LXNlY3JldC0wMTIzNDU2Nzg5', resolver=PUBLIC)
        with store.pool.connection() as c:
            c.execute("UPDATE delivery_endpoint_tests SET state='cancelled' WHERE endpoint=%s", (endpoint['endpoint_id'],))
        store.create_subscription(token, feedback=True, delivery={'endpoint_id': endpoint['endpoint_id']})
        assert record(store, 'dispatcher')
        fid = submit(store, token, 1)
        store.set_feedback_status([fid], 'declined', spec_ref='ANYM-962', note='private operator words')
        match_subscriptions(store)
        sent = []
        dispatch_hints(store, transport=lambda url, body, headers, **k: sent.append(json.loads(body)) or 204,
            resolver=PUBLIC)
    finally:
        store.set_delivery_features(False)
    [body] = [body for body in sent if body.get('type') == 'module_events']
    [reference] = body['references']
    assert set(reference) == {'event_id', 'type', 'occurred_at', 'feedback_id', 'status'}
    assert (reference['type'], reference['feedback_id'], reference['status']) == ('feedback.declined', fid, 'declined')
    assert reference['event_id'] == journal(store)[0]['event_key'] and reference['occurred_at'].endswith('Z')
    assert 'private operator words' not in json.dumps(body) and 'ANYM-962' not in json.dumps(body)


# spec: ANYM-962
def test_the_router_refuses_to_fetch_the_in_process_feedback_source(tmp_path):
    from messaging_service import event_router
    config = tmp_path / 'router.json'
    config.write_text(json.dumps({'modules': [{'source': 'deedkit:feedback', 'url': 'http://feedback:1',
        'token_file': '/run/token'}]}))
    with pytest.raises(ValueError, match='invalid_router_config'):
        event_router.load_config(config)
