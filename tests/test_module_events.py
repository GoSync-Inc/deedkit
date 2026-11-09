"""Module events journal (ANYM-562): contract, storage, streams, old readers; synthetic PostgreSQL only."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
from uuid import uuid4

from hypothesis import given, settings, strategies as st
from psycopg import errors, sql
import psycopg
import pytest

from messaging_service import module_events
from messaging_service.module_events import KINDS, ModuleContractError, read_page, validate_event
from messaging_service.subscriptions import match_subscriptions, prune_subscriptions

SOURCE = Path(__file__).resolve().parents[1] / 'services/messaging/messaging_service'
REFS = st.from_regex(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,40}', fullmatch=True)
TIMES = st.sampled_from(['2026-10-29T10:00:00Z', '2026-10-29T10:15:00+00:00', '2026-11-01T08:30:00.123Z'])


def call_event(seq, call_id='call-1', kind='call.finished', **changes):
    data = {'call.finished': {'call_id': call_id, 'status': 'finished', 'end_reason': 'completed',
            'duration_s': 42, 'outcome': 'task_completed', 'transcript': 'final'},
        'call.owner_request': {'call_id': call_id, 'decision_id': 'decision-1', 'state': 'pending'},
        'call.not_answered': {'call_id': call_id, 'end_reason': 'no_answer'}}[kind]
    event = {'specversion': '1.0', 'id': f'calls:{seq}', 'source': 'deedkit:calls', 'type': kind,
        'subject': call_id, 'time': '2026-10-29T10:00:00Z', 'data': data,
        'urgency': 'now' if kind == 'call.owner_request' else 'batch',
        'expires_at': '2099-10-29T10:30:00Z' if kind == 'call.owner_request' else None}
    return {**event, **changes}


def starting_event(seq, start='2026-10-29T10:15:00Z'):
    return {'specversion': '1.0', 'id': f'calendar:{seq}', 'source': 'deedkit:calendar',
        'type': 'calendar.event.starting', 'subject': 'event-1', 'time': '2026-10-29T10:00:00Z',
        'data': {'calendar_ref': 'family', 'event_ref': 'event-1', 'start_at': start, 'lead_minutes': 15},
        'urgency': 'now', 'expires_at': start}


RECORDING_SCOPE = 'recordings:omi-a.0123456789abcdef'


def recording_event(seq, kind='recording.text.archived', **changes):
    """An ANYM-726 feed event (SUBSCRIPTIONS.md "Module events"): references only."""
    text = kind == 'recording.text.archived'
    data = ({'recording_ref': 'rec-1', 'account_ref': 'omi-a', 'revision': 1} if text else
        {'account_ref': 'omi-a', 'principal_ref': 'agent-1', 'reason': 'permission_removed'})
    event = {'specversion': '1.0', 'id': f'recordings:{seq}', 'source': 'deedkit:recordings', 'type': kind,
        'subject': data['recording_ref' if text else 'account_ref'], 'time': '2026-11-03T10:00:00Z', 'data': data,
        'urgency': 'batch' if text else 'now', 'expires_at': None}
    return {**event, **changes}


# spec: ANYM-562
@settings(max_examples=200)
@given(seq=st.integers(1, 10**12), call_id=REFS, extra=st.from_regex(r'[a-z_]{1,20}', fullmatch=True),
    text=st.text(min_size=1, max_size=300))
def test_event_schema_admits_references_and_refuses_keys_and_values_outside_bounds(seq, call_id, extra, text):
    event = call_event(seq, call_id)
    assert validate_event(event) == seq
    if extra not in event['data']:
        with pytest.raises(ModuleContractError, match='data_key_not_allowed'):
            validate_event({**event, 'data': {**event['data'], extra: 'x'}})
    # A free-text value never fits an enumeration or a token: titles, transcripts and names stay out.
    if text not in ('finished', 'failed'):
        with pytest.raises(ModuleContractError, match='data_value_out_of_bounds'):
            validate_event({**event, 'data': {**event['data'], 'status': text}})
    for bad in (-1, 86401, 1.5, '42', True):
        with pytest.raises(ModuleContractError, match='data_value_out_of_bounds'):
            validate_event({**event, 'data': {**event['data'], 'duration_s': bad}})
    with pytest.raises(ModuleContractError):
        validate_event({**event, 'id': f'calendar:{seq}'})
    with pytest.raises(ModuleContractError):
        validate_event({**event, 'subject': call_id + 'x'})


# spec: ANYM-562
@given(start=TIMES)
def test_owner_request_is_now_with_deadline_and_starting_expires_at_start(start):
    assert validate_event(call_event(1, kind='call.owner_request'))
    with pytest.raises(ModuleContractError, match='owner_request_needs_now_and_deadline'):
        validate_event(call_event(1, kind='call.owner_request', urgency='batch'))
    with pytest.raises(ModuleContractError, match='owner_request_needs_now_and_deadline'):
        validate_event(call_event(1, kind='call.owner_request', expires_at=None))
    assert validate_event(starting_event(2, start)) == 2
    with pytest.raises(ModuleContractError, match='starting_expires_at_start'):
        validate_event({**starting_event(2, start), 'expires_at': '2026-12-01T00:00:00Z'})


def producer(journal, floor, journal_id):
    """A reference producer: a gapless journal 1..len, a filter, a pruned floor and a limit."""
    def read(after, limit):
        rows = [seq for seq in range(after + 1, len(journal) + 1)]
        page, last = [], after
        for seq in rows:
            if len(page) == limit:
                break
            last = seq
            if journal[seq - 1]:
                page.append(call_event(seq, f'call-{seq}'))
        return {'events': page, 'next_after': last, 'has_more': last < len(journal),
            'retention_floor': floor, 'journal_id': journal_id}
    return read


# spec: ANYM-562
@settings(max_examples=150)
@given(journal=st.lists(st.booleans(), max_size=60), floor=st.integers(0, 70), start=st.integers(0, 70),
    limit=st.integers(1, 7))
def test_cursor_reads_every_visible_event_once_and_names_pruned_ranges(journal, floor, start, limit):
    floor, start = min(floor, len(journal)), min(start, len(journal))
    read = producer(journal, floor, 'j1')
    cursor, seen, gaps = {'after': start, 'journal_id': None}, [], []
    for _ in range(200):
        result = read_page(cursor, read(cursor['after'], limit), limit=limit, max_bytes=64000)
        if result['status'] == 'gap':
            gaps.append(result['gap'])
            assert result['gap']['through'] > result['gap']['after']
        else:
            assert result['status'] == 'events'
            seen += [int(event['id'].split(':')[1]) for event in result['events']]
        if result['next']['after'] == cursor['after'] and not result.get('events') and result['status'] == 'events':
            break
        assert result['next']['after'] > cursor['after']
        cursor = result['next']
    expected = [seq for seq in range(max(start, floor) + 1, len(journal) + 1) if journal[seq - 1]]
    assert seen == expected
    assert bool(gaps) == (start < floor)


# spec: ANYM-562
def test_cursor_refuses_contract_breaks_and_detects_a_source_reset():
    read = producer([True] * 5, 0, 'j1')
    page = read(0, 5)
    assert read_page({'after': 0, 'journal_id': 'j1'}, {**page, 'journal_id': 'j2'}, limit=5,
        max_bytes=64000)['status'] == 'reset'
    assert read_page({'after': 7, 'journal_id': 'j1'}, read(7, 5) | {'next_after': 3}, limit=5,
        max_bytes=64000)['status'] == 'reset'
    for broken, code in ((page, 'page_over_limit'), ({**page, 'events': page['events'][::-1]}, 'seq_not_increasing'),
            ({**page, 'next_after': 2}, 'next_after_behind_events'),
            ({**page, 'events': [], 'next_after': 0, 'has_more': True}, 'next_after_not_advancing')):
        with pytest.raises(ModuleContractError, match=code):
            read_page({'after': 0, 'journal_id': 'j1'}, broken, limit=4 if code == 'page_over_limit' else 5,
                max_bytes=64000)
    with pytest.raises(ModuleContractError, match='page_over_bytes'):
        read_page({'after': 0, 'journal_id': 'j1'}, page, limit=5, max_bytes=500)
    with pytest.raises(ModuleContractError, match='seq_not_after_cursor'):
        read_page({'after': 2, 'journal_id': 'j1'}, page, limit=5, max_bytes=64000)


@pytest.fixture()
def modules(seeded):
    store = seeded[0]
    with store.pool.connection() as c:
        c.execute('TRUNCATE module_events,module_source_health CASCADE')
        c.execute('UPDATE module_event_counter SET value=0 WHERE id=1')
        c.execute("DELETE FROM component_heartbeats WHERE component='dispatcher'")
    return seeded


def beat(store, schema=None):
    """The dispatcher's heartbeat: this runtime's schema, or an older dispatcher's."""
    from messaging_service.heartbeats import record
    if schema is None:
        assert record(store, 'dispatcher')
        return
    with store.pool.connection() as c:
        c.execute('''INSERT INTO component_heartbeats(component,version,schema_version,beat_at)
            VALUES('dispatcher','0.9.18',%s,clock_timestamp()) ON CONFLICT(component) DO UPDATE
            SET schema_version=EXCLUDED.schema_version,beat_at=EXCLUDED.beat_at''', (schema,))


def bound(store, token, sid, tmp_path):
    from messaging_service.event_delivery import register_receiver
    secret = tmp_path / 'receiver.key'
    secret.write_bytes(b'synthetic receiver secret bytes 0123456789')
    secret.chmod(0o600)
    with store.authorized(token) as (_, p):
        key_id = str(p['key_id'])
    rid = str(uuid4())
    register_receiver(store, rid, 'module-reader', key_id, 'https://hooks.example.test/wake', str(secret),
        resolver=lambda *a, **k: ['93.184.216.34'])
    store.configure_subscription_delivery(token, sid, store.get_subscription(token, sid)['version'], rid)


def module_hints(store, sid):
    with store.pool.connection() as c:
        return c.execute('''SELECT through_position,state,error_code FROM subscription_outbox
            WHERE subscription=%s AND kind='module_events' ORDER BY created_at''', (sid,)).fetchall()


def subscriber(store):
    permissions = ['read', 'subscription-write', 'subscription-ack']
    return store.issue_key('module-reader', ['telegram-demo'], permissions=permissions, key_permissions=permissions)


def mixed(store, token, types=('call.finished', 'call.owner_request')):
    sid = store.create_subscription(token, ['telegram-demo-chat'])['subscription_id']
    with store.pool.connection() as c:
        module_events.attach_sources(c, sid, [('deedkit:calls', 'owner-calls', list(types))])
    return sid


def publish(store, event, scope='owner-calls'):
    with store.pool.connection() as c:
        return module_events.publish(c, event, scope)


def emit(store, message='demo-tg-1'):
    with store.pool.connection() as c:
        return store.append_event(c, 'telegram-demo', message, 'message.created', 'live')


# spec: ANYM-562
def test_publish_is_gapless_idempotent_and_never_waits_for_the_message_counter(modules):
    store = modules[0]
    first = publish(store, call_event(1))
    assert publish(store, call_event(1)) == first
    with pytest.raises(errors.UniqueViolation, match='module_event_conflict'):
        publish(store, call_event(1, 'call-2'))
    with pytest.raises(errors.CheckViolation, match='invalid_module_event'), store.pool.connection() as c:
        # The definer function refuses an unlisted key even when a caller skips the Python contract.
        c.execute('SELECT public.publish_module_event(%s,%s,%s,%s,%s,%s,%s,%s,%s)', ('calls:9', 'deedkit:calls',
            'call.not_answered', 'call-1', 'owner-calls', '2026-10-29T10:00:00Z',
            json.dumps({'call_id': 'call-1', 'end_reason': 'busy', 'destination': '+10000000000'}), 'batch', None))
    with store.pool.connection() as holder:
        holder.execute('UPDATE feed_counter SET value=value WHERE id=1')  # a message write's open transaction
        with psycopg.connect(store.pool.conninfo, autocommit=True) as other:
            other.execute("SET lock_timeout='1s'")
            seq = other.execute("SELECT public.publish_module_event('calls:2','deedkit:calls','call.not_answered',"
                "'call-2','owner-calls','2026-10-29T10:00:00Z','{\"call_id\":\"call-2\",\"end_reason\":\"busy\"}',"
                "'batch',NULL)").fetchone()[0]
    with store.pool.connection() as c:
        assert [row['seq'] for row in c.execute('SELECT seq FROM module_events ORDER BY seq')] == [first, seq] == [1, 2]
        assert c.execute('SELECT value FROM module_event_counter').fetchone()['value'] == 2


# spec: ANYM-732
def test_recording_events_publish_idempotently_conflict_on_other_fields_and_leave_calls_and_calendar_rows_alone(modules):
    store = modules[0]
    call = publish(store, call_event(1))
    start = publish(store, starting_event(2), 'family-calendar')
    with store.pool.connection() as c:
        before = c.execute('SELECT * FROM module_events ORDER BY seq').fetchall()
    text = publish(store, recording_event(7), RECORDING_SCOPE)
    assert publish(store, recording_event(7), RECORDING_SCOPE) == text == 3
    access = publish(store, recording_event(8, 'recording.access.changed'), RECORDING_SCOPE)
    assert access == 4
    with pytest.raises(errors.UniqueViolation, match='module_event_conflict'):
        publish(store, recording_event(7, data={'recording_ref': 'rec-1', 'account_ref': 'omi-a', 'revision': 2}),
            RECORDING_SCOPE)
    with pytest.raises(errors.UniqueViolation, match='module_event_conflict'):
        publish(store, recording_event(7), 'recordings:omi-a.fedcba9876543210')
    with pytest.raises(ModuleContractError, match='data_key_not_allowed'):
        publish(store, data_of(recording_event(9), title='private words'), RECORDING_SCOPE)
    with pytest.raises(errors.CheckViolation, match='invalid_module_event'), store.pool.connection() as c:
        # The definer function refuses free text even when a caller skips the Python contract.
        c.execute('SELECT public.publish_module_event(%s,%s,%s,%s,%s,%s,%s,%s,%s)', ('recordings:9',
            'deedkit:recordings', 'recording.text.archived', 'rec-1', RECORDING_SCOPE, '2026-11-03T10:00:00Z',
            json.dumps({'recording_ref': 'rec-1', 'account_ref': 'omi-a', 'revision': 1, 'title': 'private words'}),
            'batch', None))
    with store.pool.connection() as c:
        rows = c.execute('SELECT * FROM module_events ORDER BY seq').fetchall()
        assert c.execute('SELECT value FROM module_event_counter').fetchone()['value'] == 4
    assert [(r['seq'], r['event_key'], r['source'], r['type'], r['urgency'], r['expires_at']) for r in rows[2:]] == [
        (3, 'recordings:7', 'deedkit:recordings', 'recording.text.archived', 'batch', None),
        (4, 'recordings:8', 'deedkit:recordings', 'recording.access.changed', 'now', None)]
    assert rows[:2] == before and (call, start) == (1, 2)


def data_of(event, **values):
    return {**event, 'data': {**event['data'], **values}}


# The constraints and the function that 056 and 058 wrote with the module list, as written there (ANYM-732 inventory).
OLD_MODULE_LISTS = {
    'module_events': [
        ('module_events_event_key_check', "event_key ~ '^(calendar|calls):[1-9][0-9]{0,17}$'"),
        ('module_events_source_check', "source IN ('deedkit:calendar','deedkit:calls')"),
        ('module_events_type_check', "type IN ('calendar.event.created','calendar.event.changed','calendar.event.cancelled',"
            "'calendar.event.starting','call.finished','call.failed','call.not_answered','call.interrupted','call.owner_request')"),
        ('module_events_check', "split_part(event_key,':',1)=split_part(source,':',2)"),
        ('module_events_check1', "split_part(type,'.',1)=CASE source WHEN 'deedkit:calendar' THEN 'calendar' ELSE 'call' END")],
    **{table: [(f'{table}_source_check', "source IN ('deedkit:calendar','deedkit:calls')")] for table in (
        'subscription_module_sources', 'module_source_health', 'module_router_cursors', 'module_router_gaps')}}


def _constraint_expression(c, table, name):
    found = c.execute("SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint WHERE conrelid=%s::regclass AND conname=%s",
        (table, name)).fetchone()
    assert found is not None, (table, name)
    assert found['d'].startswith('CHECK ')
    return found['d'][len('CHECK '):]


# spec: ANYM-732
def test_every_module_list_constraint_admits_recordings_and_each_new_one_is_a_superset_of_the_old(modules):
    store = modules[0]
    modules_seen = ('calendar', 'calls', 'recordings', 'messages')
    keys = [f'{m}:{n}' for m in modules_seen for n in ('1', '0', '01', '9' * 18, '9' * 19)]
    sources = [f'deedkit:{m}' for m in modules_seen] + ['', 'calls']
    types = sorted(KINDS) + ['recording.other', 'recording.', 'messages.x', 'call.', 'calendar.']
    grid = [(k, s, t) for k in keys for s in sources for t in types]
    def evaluate(c, expression, rows):
        result = c.execute(f'SELECT ({expression}) AS ok FROM unnest(%s::text[], %s::text[], %s::text[]) '
            'AS t(event_key, source, type)', [[row[i] for row in rows] for i in range(3)]).fetchall()
        return [row['ok'] for row in result]
    with store.pool.connection() as c:
        for table, olds in OLD_MODULE_LISTS.items():
            old = ' AND '.join(f'({expression})' for _, expression in olds)
            new = ' AND '.join(_constraint_expression(c, table, name) for name, _ in olds)
            before, after = evaluate(c, old, grid), evaluate(c, new, grid)
            # A superset: nothing the old list admitted is refused now (the whole table's checks together: the old
            # source list shut out the rows only 'deedkit:recordings' could otherwise change).
            assert not [row for row, was, now in zip(grid, before, after) if was and not now], table
            admitted = {row for row, now in zip(grid, after) if now}
            if table == 'module_events':
                for kind in ('recording.text.archived', 'recording.access.changed'):
                    assert ('recordings:1', 'deedkit:recordings', kind) in admitted
                    assert ('recordings:' + '9' * 18, 'deedkit:recordings', kind) in admitted
                assert not {row for row in admitted if row[1] == 'deedkit:recordings' and (
                    not row[0].startswith('recordings:') or not row[2].startswith('recording.'))}
                assert not {row for row in admitted if row[0].startswith('recordings:') and row[1] != 'deedkit:recordings'}
                assert not {row for row in admitted if row[0].split(':')[0] not in ('calendar', 'calls', 'recordings')}
            else:
                assert {row[1] for row in admitted} == {'deedkit:calendar', 'deedkit:calls', 'deedkit:recordings'}, table
        # Inventory: no CHECK of any table and no function still names the two modules without the third.
        stale = c.execute("""SELECT conrelid::regclass::text AS t, conname FROM pg_constraint WHERE contype='c'
            AND connamespace='public'::regnamespace AND pg_get_constraintdef(oid) ~ 'deedkit:calls|calendar\\|calls'
            AND pg_get_constraintdef(oid) !~ 'recordings'""").fetchall()
        assert stale == []
        functions = c.execute("""SELECT proname, prosrc ~ 'recordings' AS names_it FROM pg_proc
            WHERE pronamespace='public'::regnamespace AND prosrc ~ 'deedkit:calls|calendar\\|calls|call\\.finished'""").fetchall()
        assert [row['proname'] for row in functions] == ['module_event_valid'] and functions[0]['names_it']


# spec: ANYM-732
def test_schema67_replaces_only_the_module_lists_keeps_every_acl_and_is_repeatable(modules):
    from messaging_service import store as store_module
    store = modules[0]
    root = Path(store_module.__file__).parent / 'migrations'
    text = (root / '067_recording_module_source.sql').read_text()
    function = "'public.module_event_valid(text,text,text,text,text,jsonb,text,text)'::regprocedure"
    other = "'public.publish_module_event(text,text,text,text,text,text,jsonb,text,text)'::regprocedure"
    with store.pool.connection() as c:
        c.execute('DELETE FROM archive_migrations WHERE version IN (67,68,69,70,71)')
        c.execute("UPDATE probe_metadata SET value='66' WHERE name='schema_version'")
        acl = [c.execute(f'SELECT proacl::text AS acl, proowner::regrole::text AS owner, prosrc FROM pg_proc WHERE oid={f}').fetchone()
            for f in (function, other)]
        grants = c.execute("SELECT oid,relacl::text FROM pg_class WHERE relnamespace='public'::regnamespace ORDER BY oid").fetchall()
        shape = c.execute("""SELECT conrelid::regclass::text AS t, conname FROM pg_constraint
            WHERE connamespace='public'::regnamespace ORDER BY 1,2""").fetchall()
        columns = c.execute("""SELECT table_name,column_name,data_type FROM information_schema.columns
            WHERE table_schema='public' ORDER BY 1,2""").fetchall()
    store.migrate()
    store.migrate()
    with store.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '71'
        assert c.execute('SELECT count(*) AS n FROM archive_migrations WHERE version=67').fetchone()['n'] == 1
        now = [c.execute(f'SELECT proacl::text AS acl, proowner::regrole::text AS owner, prosrc FROM pg_proc WHERE oid={f}').fetchone()
            for f in (function, other)]
        assert [(row['acl'], row['owner']) for row in now] == [(row['acl'], row['owner']) for row in acl]
        assert now[1]['prosrc'] == acl[1]['prosrc']  # publish_module_event itself is not replaced
        assert c.execute("SELECT oid,relacl::text FROM pg_class WHERE relnamespace='public'::regnamespace ORDER BY oid").fetchall() == grants
        assert c.execute("""SELECT conrelid::regclass::text AS t, conname FROM pg_constraint
            WHERE connamespace='public'::regnamespace ORDER BY 1,2""").fetchall() == shape  # same names, widened bodies
        assert c.execute("""SELECT table_name,column_name,data_type FROM information_schema.columns
            WHERE table_schema='public' ORDER BY 1,2""").fetchall() == columns
    # Risk tier and rollout class are named; only the eight widened constraints are dropped; no row, grant or table is touched.
    assert text.startswith('-- rollout: manual') and 'Risk tier: DANGEROUS' in text
    dropped = re.findall(r'DROP\s+CONSTRAINT\s+(\w+)', text)
    # module_events_check (id module = source module) names no module, so it is kept as it is.
    assert sorted(dropped) == sorted(name for olds in OLD_MODULE_LISTS.values() for name, _ in olds
        if name != 'module_events_check')
    assert len(re.findall(r'\bDROP\b', text, re.I)) == len(dropped)
    assert not re.search(r'\b(GRANT|REVOKE|CREATE\s+(TABLE|INDEX)|INSERT|UPDATE|DELETE|TRUNCATE)\b', text, re.I)
    assert 'octet_length(p_data::text)>1024' in text


# spec: ANYM-732
def test_calls_and_calendar_decisions_of_the_validator_are_those_of_schema_65_for_every_case(modules):
    from messaging_service import store as store_module
    store = modules[0]
    old = (Path(store_module.__file__).parent / 'migrations/065_call_failed_provider_error.sql').read_text()
    start = old.index('CREATE OR REPLACE FUNCTION public.module_event_valid(')
    previous = old[start:old.index('END $$;', start) + len('END $$;')].replace('public.module_event_valid(',
        'public.anym732_previous_valid(', 1)
    arguments = '%s,%s,%s,%s,%s,%s,%s,%s'
    cases = {name: event for name, event in _mutations().items() if 'recording' not in name}
    cases.update({f'provider_{label}': failed_event(1, **data) for label, data in (('five', {'provider_cause':
        'geo_permission_missing', 'provider_code': 21215}), ('code_alone', {'provider_code': 21215}),
        ('text', {'provider_text': 'x'}))})
    with store.pool.connection() as c:
        c.execute(previous)
        try:
            assert len(cases) > 40
            for name, event in cases.items():
                values = (event['id'], event['source'], event['type'], event['subject'], event['time'],
                    json.dumps(event['data']), event['urgency'], event['expires_at'])
                kept = c.execute(f'SELECT public.anym732_previous_valid({arguments}) AS ok', values).fetchone()['ok']
                now = c.execute(f'SELECT public.module_event_valid({arguments}) AS ok', values).fetchone()['ok']
                assert kept == now, name
        finally:
            c.execute('DROP FUNCTION public.anym732_previous_valid(text,text,text,text,text,jsonb,text,text)')


# spec: ANYM-562
def test_router_role_publishes_but_cannot_write_messages_sources_or_read_content(modules):
    store = modules[0]
    role = 'anym562_router_' + uuid4().hex[:10]
    with store.pool.connection() as c:
        c.execute(SOURCE.joinpath('module_router_role.sql').read_text().replace('messaging_module_router', role))
    try:
        attempts = {'publish': None, 'events': 'INSERT INTO events(id,account,message,kind,revision,origin,snapshot) '
            "VALUES(999,'telegram-demo','demo-tg-1','message.created',1,'live','{}')",
            'counter': 'UPDATE feed_counter SET value=value+1 WHERE id=1',
            'content': 'SELECT body FROM messages LIMIT 1',
            'sources': "UPDATE subscription_module_sources SET scope_ref='other' WHERE true",
            'journal': "UPDATE module_events SET scope_ref='other' WHERE true"}
        for name, statement in attempts.items():
            with store.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                if statement is None:
                    assert module_events.publish(c, call_event(5), 'owner-calls') == 1
                    module_events.report_reachability(c, 'deedkit:calls', False)
                    continue
                with pytest.raises(errors.InsufficientPrivilege):
                    c.execute(statement)
    finally:
        with store.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


# spec: ANYM-562
def test_mixed_subscription_fetches_and_acks_each_stream_and_a_module_wake_reaches_the_reader(modules, tmp_path):
    from messaging_service.event_delivery import dispatch_hints, register_receiver
    store = modules[0]
    token = subscriber(store)
    sid = mixed(store, token)
    secret = tmp_path / 'receiver.key'
    secret.write_bytes(b'synthetic receiver secret bytes 0123456789')
    secret.chmod(0o600)
    with store.authorized(token) as (_, p):
        key_id = str(p['key_id'])
    rid = str(uuid4())
    register_receiver(store, rid, 'module-reader', key_id, 'https://hooks.example.test/wake', str(secret),
        resolver=lambda *a, **k: ['93.184.216.34'])
    store.configure_subscription_delivery(token, sid, 1, rid)
    beat(store)
    ignored = publish(store, call_event(1, 'call-9'), scope='someone-else')
    wake = publish(store, call_event(2, kind='call.owner_request'))
    message = emit(store)
    stats = match_subscriptions(store)
    assert stats['module_matched'] == 1 and stats['matched'] == 1
    sent = []
    dispatch_hints(store, transport=lambda endpoint, body, headers, **k: sent.append(json.loads(body)) or 204,
        resolver=lambda *a, **k: ['93.184.216.34'])
    module_hint = [hint for hint in sent if hint['kind'] == 'module_events']
    assert module_hint and module_hint[0]['through'] == wake
    with store.pool.connection() as c:
        assert c.execute("SELECT quiet_bypass FROM subscription_outbox WHERE kind='module_events'").fetchone()[
            'quiet_bypass'] is True
    page = store.fetch_subscription_events(token, sid)
    assert [item['event_id'] for item in page['items']] == [message]
    assert [item['module_event_id'] for item in page['module']['items']] == [wake]
    item = page['module']['items'][0]
    assert item['id'] == 'calls:2' and item['type'] == 'call.owner_request' and item['urgency'] == 'now'
    assert item['data'] == {'call_id': 'call-1', 'decision_id': 'decision-1', 'state': 'pending'}
    assert ignored not in [entry['module_event_id'] for entry in page['module']['items']]
    acked = store.ack_subscription_events(token, sid, page['receipt'], page['ack_version'])
    assert acked['acked_through'] == message and acked['module_acked_through'] == wake
    # Only the module stream moves now: a new module event, no new message.
    later = publish(store, call_event(3, 'call-3'))
    match_subscriptions(store)
    page = store.fetch_subscription_events(token, sid)
    assert page['items'] == [] and [item['module_event_id'] for item in page['module']['items']] == [later]
    acked = store.ack_subscription_events(token, sid, page['receipt'], page['ack_version'])
    assert acked['acked_through'] == message and acked['module_acked_through'] == later
    view = store.get_subscription(token, sid)
    assert view['acked_through'] == message and view['module_stream']['acked_through'] == later
    assert view['module_stream']['matcher_caught_up'] and view['module_stream']['health'] == []


# spec: ANYM-562
def test_wait_returns_on_a_module_event(modules):
    import asyncio
    from messaging_service.subscription_consumption import wait_for_subscription_events
    store = modules[0]
    token = subscriber(store)
    sid = mixed(store, token)
    publish(store, call_event(1))
    match_subscriptions(store)
    result = asyncio.run(wait_for_subscription_events(store, token, sid, timeout=2))
    assert result['wait_status'] == 'events_available' and result['module']['items']


# spec: ANYM-562
def test_unacked_module_matches_stay_and_a_pruned_range_is_a_gap_of_its_stream(modules):
    store = modules[0]
    token = subscriber(store)
    sid = mixed(store, token)
    seqs = [publish(store, call_event(n, f'call-{n}')) for n in range(1, 4)]
    match_subscriptions(store)
    page = store.fetch_subscription_events(token, sid, limit=2)
    store.ack_subscription_events(token, sid, page['receipt'], page['ack_version'])
    with store.pool.connection() as c:
        c.execute("UPDATE subscription_module_matches SET matched_at=matched_at-interval '30 days'")
        c.execute("INSERT INTO probe_metadata(name,value) VALUES('subscription_retention','enabled') "
            "ON CONFLICT(name) DO UPDATE SET value='enabled'")
    try:
        stats = prune_subscriptions(store)
    finally:
        with store.pool.connection() as c:
            c.execute("DELETE FROM probe_metadata WHERE name='subscription_retention'")
    assert stats['pruned'] == 2
    with store.pool.connection() as c:
        left = [row['event'] for row in c.execute('SELECT event FROM subscription_module_matches WHERE subscription=%s',
            (sid,))]
    assert left == [seqs[2]]
    stale = store.get_subscription_events(token, sid, after=0, module_after=0)
    assert stale['module']['gap']['reason'] == 'retention_gap' and stale['module']['gap']['stream'] == 'modules'
    assert stale['module']['items'] == [] and stale['module']['has_more'] is True
    assert stale['subscription']['gap'] is None  # the message stream has no gap
    assert [item['module_event_id'] for item in store.fetch_subscription_events(token, sid)['module']['items']] == [
        seqs[2]]


# spec: ANYM-562
def test_old_readers_ignore_module_rows_and_an_old_resume_cannot_revive_the_module_stream(modules):
    store, old, _ = modules
    token = subscriber(store)
    sid = mixed(store, token)
    before = store.get_updates(old, 'probe')['items']
    publish(store, call_event(1))
    message = emit(store)
    match_subscriptions(store)
    # The message journal, get_updates and the message matches hold no module row.
    with store.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM events WHERE id>%s', (message,)).fetchone()['n'] == 0
        assert c.execute('SELECT value FROM feed_counter').fetchone()['value'] == message
        assert [row['event'] for row in c.execute('SELECT event FROM subscription_matches WHERE subscription=%s',
            (sid,))] == [message]
    after = store.get_updates(old, 'probe')['items']
    assert len(after) == len(before) + 1
    # The schema-55 resume statement, verbatim: a paused subscription made active by code that does
    # not know module streams. The fence holds the module stream; message matching goes on.
    store.update_subscription(token, sid, store.get_subscription(token, sid)['version'], action='pause')
    with store.pool.connection() as c:
        c.execute('''UPDATE subscriptions SET state=%s,version=version+1,
        matched_through=%s,gap=%s,blocked_reason=NULL,updated_at=clock_timestamp()
        WHERE id=%s RETURNING *''', ('active', message, None, sid))
    publish(store, call_event(2, 'call-2'))
    newer = emit(store)
    match_subscriptions(store)
    view = store.get_subscription(token, sid)
    assert view['matched_through'] == newer
    assert view['module_stream']['state'] == 'held_after_rollback' and view['module_stream']['matched_through'] == 1
    # Only module-aware code re-arms it.
    view = store.update_subscription(token, sid, view['version'], action='resume')
    match_subscriptions(store)
    assert store.get_subscription(token, sid)['module_stream']['matched_through'] == 2
    assert store.get_subscription(token, sid)['module_stream']['state'] == 'active'


# spec: ANYM-562
def test_module_unreachable_is_per_source_self_clearing_and_messages_keep_flowing(modules, monkeypatch):
    store = modules[0]
    token = subscriber(store)
    sid = mixed(store, token)
    with store.pool.connection() as c:
        module_events.report_reachability(c, 'deedkit:calls', False)
    first = store.get_subscription(token, sid)['module_stream']['health']
    with store.pool.connection() as c:
        module_events.report_reachability(c, 'deedkit:calls', False)
    view = store.get_subscription(token, sid)
    assert view['module_stream']['health'] == first and first[0]['state'] == 'module_unreachable'
    assert first[0]['source'] == 'deedkit:calls' and first[0]['since']
    assert view['blocked_reason'] is None
    # A failing module pass rolls back alone: the message stream still matches.
    monkeypatch.setattr(module_events, 'match', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('module down')))
    message = emit(store)
    match_subscriptions(store)
    view = store.get_subscription(token, sid)
    assert view['matched_through'] == message and view['blocked_reason'] is None
    with store.pool.connection() as c:
        module_events.report_reachability(c, 'deedkit:calls', True)
    assert store.get_subscription(token, sid)['module_stream']['health'] == []


# spec: ANYM-562
def test_expired_module_event_is_never_matched(modules):
    store = modules[0]
    token = subscriber(store)
    sid = mixed(store, token)
    publish(store, call_event(1, kind='call.owner_request', expires_at='2020-01-01T00:00:00Z'))
    match_subscriptions(store)
    view = store.get_subscription(token, sid)['module_stream']
    assert view['matched_through'] == 1 and view['matched_event_count'] == 0


# spec: ANYM-562
def test_backup_manifest_covers_the_module_journal_streams_and_sources(modules):
    from messaging_service.backup import database_manifest
    store = modules[0]
    token = subscriber(store)
    mixed(store, token)
    publish(store, call_event(1))
    match_subscriptions(store)
    with store.pool.connection() as c:
        tables = database_manifest(c)
    for name in ('module_event_counter', 'module_events', 'subscription_module_sources',
            'subscription_module_streams', 'subscription_module_matches'):
        assert tables[name]['rows'] >= 1


# spec: ANYM-562
def test_concurrent_publishers_keep_the_module_sequence_gapless(modules):
    store = modules[0]
    with ThreadPoolExecutor(8) as pool:
        seqs = list(pool.map(lambda n: publish(store, call_event(n, f'call-{n}')), range(1, 41)))
    assert sorted(seqs) == list(range(1, 41))


def _mutations():
    finished = call_event(1)
    changed = {'specversion': '1.0', 'id': 'calendar:3', 'source': 'deedkit:calendar',
        'type': 'calendar.event.changed', 'subject': 'event-1', 'time': '2026-10-29T10:00:00Z',
        'data': {'calendar_ref': 'family', 'event_ref': 'event-1', 'start_at': '2026-10-29T10:15:00Z',
            'version': 2, 'changed': ['start']}, 'urgency': 'batch', 'expires_at': None}
    def data(event, **values):
        return {**event, 'data': {**event['data'], **values}}
    without = {key: value for key, value in finished['data'].items() if key != 'end_reason'}
    return {'valid_finished': finished, 'valid_changed': changed, 'valid_owner': call_event(2, kind='call.owner_request'),
        'valid_starting': starting_event(4), 'free_text_transcript': data(finished, transcript='private words'),
        'missing_key': {**finished, 'data': without}, 'status_outside_enum': data(finished, status='done'),
        'duration_too_long': data(finished, duration_s=86401), 'duration_negative': data(finished, duration_s=-1),
        'duration_fraction': data(finished, duration_s=1.5), 'duration_text': data(finished, duration_s='42'),
        'duration_bool': data(finished, duration_s=True), 'token_with_space': data(finished, end_reason='has space'),
        'extra_key': data(finished, destination='+10000000000'), 'subject_mismatch': {**finished, 'subject': 'other'},
        'changed_repeat': data(changed, changed=['start', 'start']), 'changed_number': data(changed, changed=['start', 1]),
        'changed_unknown': data(changed, changed=['title']), 'changed_empty': data(changed, changed=[]),
        'start_not_time': data(changed, start_at='tomorrow'), 'version_negative': data(changed, version=-1),
        'owner_batched': call_event(2, kind='call.owner_request', urgency='batch'),
        'starting_late_expiry': {**starting_event(4), 'expires_at': '2026-12-01T00:00:00Z'},
        'id_of_other_module': {**finished, 'id': 'calendar:1'}, 'id_above_2_53': {**finished, 'id': 'calls:9999999999999999'},
        'ref_too_long': data(finished, call_id='c' * 129) | {'subject': 'c' * 129},
        'valid_created': {**changed, 'type': 'calendar.event.created', 'data': {k: v for k, v in changed['data'].items()
            if k != 'changed'}},
        'valid_cancelled': {**changed, 'type': 'calendar.event.cancelled', 'data': {k: changed['data'][k]
            for k in ('calendar_ref', 'event_ref', 'start_at')}},
        'valid_failed': {**finished, 'type': 'call.failed', 'data': {'call_id': 'call-1', 'status': 'failed',
            'end_reason': 'provider_error'}},
        'valid_not_answered': call_event(5, kind='call.not_answered'),
        'valid_interrupted': {**finished, 'type': 'call.interrupted', 'data': {'call_id': 'call-1',
            'end_reason': 'service_restart'}},
        'valid_latest_expiry': {**finished, 'expires_at': '9999-12-30T00:00:00Z'},
        # ANYM-732: the recordings source, the exact key sets and the urgency and expiry rules of ANYM-726.
        'valid_recording_text': recording_event(1), 'valid_recording_max_seq': recording_event(2**53 - 1),
        'valid_recording_access': recording_event(2, 'recording.access.changed'),
        'recording_extra_key': data(recording_event(1), title='private words'),
        'recording_free_text_ref': {**data(recording_event(1), recording_ref='private words'), 'subject': 'private words'},
        'recording_ref_too_long': {**data(recording_event(1), recording_ref='r' * 129), 'subject': 'r' * 129},
        'recording_missing_revision': {**recording_event(1), 'data': {'recording_ref': 'rec-1', 'account_ref': 'omi-a'}},
        'recording_revision_negative': data(recording_event(1), revision=-1),
        'recording_revision_text': data(recording_event(1), revision='1'),
        'recording_revision_bool': data(recording_event(1), revision=True),
        'recording_revision_fraction': data(recording_event(1), revision=1.5),
        'recording_access_extra_key': data(recording_event(1, 'recording.access.changed'), note='text'),
        'recording_reason_outside_enum': data(recording_event(1, 'recording.access.changed'), reason='title_changed'),
        'recording_subject_mismatch': recording_event(1, subject='other'),
        'recording_access_subject_is_the_recording': recording_event(1, 'recording.access.changed', subject='rec-1'),
        'recording_text_now': recording_event(1, urgency='now'),
        'recording_access_batch': recording_event(1, 'recording.access.changed', urgency='batch'),
        'recording_text_expiring': recording_event(1, expires_at='2099-10-29T10:30:00Z'),
        'recording_access_expiring': recording_event(1, 'recording.access.changed', expires_at='2099-10-29T10:30:00Z'),
        'recording_id_of_calls': recording_event(1, id='calls:1'),
        'recording_id_zero': recording_event(1, id='recordings:0'),
        'recording_id_leading_zero': recording_event(1, id='recordings:01'),
        'recording_id_above_2_53': recording_event(1, id='recordings:9007199254740992'),
        'recording_type_under_the_calls_source': recording_event(1, id='calls:1', source='deedkit:calls'),
        'call_type_under_the_recordings_source': {**finished, 'id': 'recordings:1', 'source': 'deedkit:recordings'},
        'calendar_type_under_the_recordings_source': {**starting_event(4), 'id': 'recordings:4',
            'source': 'deedkit:recordings'},
        'time_infinite': {**finished, 'time': 'infinity'}, 'expiry_infinite': {**finished, 'expires_at': 'infinity'},
        'time_before_1970': {**finished, 'time': '1969-12-31T23:59:59Z'},
        'expiry_past_latest': {**finished, 'expires_at': '9999-12-31T23:59:59Z'},
        'start_before_1970': data(changed, start_at='1969-12-31T23:59:59Z'),
        'start_infinite': data(changed, start_at='infinity'),
        # Component limits PostgreSQL and Python parse differently: both validators refuse each one.
        'valid_offset_15_59': {**finished, 'time': '2026-10-29T10:00:00+15:59'},
        **{f'{field}_{label}': ({**finished, field: moment} if field != 'start' else data(changed, start_at=moment))
            for field in ('time', 'expires_at', 'start') for label, moment in (
                ('hour_24', '2026-10-29T24:00:00Z'), ('second_60', '2026-10-29T10:00:60Z'),
                ('offset_minute_60', '2026-10-29T10:00:00+00:60'), ('offset_16', '2026-10-29T10:00:00+16:00'),
                ('february_30', '2026-02-30T10:00:00Z'), ('no_offset', '2026-10-29T10:00:00'))}}


# spec: ANYM-562
def test_definer_function_enforces_the_same_schema_as_the_contract(modules):
    store = modules[0]
    with store.pool.connection() as c:
        for name, event in _mutations().items():
            try:
                validate_event(event)
                python = True
            except ModuleContractError:
                python = False
            database = c.execute('SELECT public.module_event_valid(%s,%s,%s,%s,%s,%s,%s,%s) AS ok', (event['id'],
                event['source'], event['type'], event['subject'], event['time'], json.dumps(event['data']),
                event['urgency'], event['expires_at'])).fetchone()['ok']
            assert python == database == name.startswith('valid_'), name
    assert {event['type'] for name, event in _mutations().items() if name.startswith('valid_')} == set(KINDS)


CAUSES = ('geo_permission_missing', 'invalid_destination', 'destination_not_reachable', 'destination_not_dialable',
    'destination_unverified', 'source_number_invalid', 'destination_blocked', 'rate_limited', 'provider_auth_failed',
    'provider_rejected_other', 'call_failed_other')


def failed_event(seq=1, **data):
    """A call.failed event: today's three keys plus whatever provider keys the case names."""
    base = {'call_id': 'call-1', 'status': 'finished', 'end_reason': 'provider_rejected'}
    return {**call_event(seq), 'type': 'call.failed', 'data': {**base, **data}}


def _provider_error_cases():
    ok = {'provider_cause': 'geo_permission_missing', 'provider_code': 21215}
    valid = {'three_keys': {}, 'five_keys': ok, 'cause_only': {'provider_cause': 'call_failed_other'},
        'code_smallest': {**ok, 'provider_code': 1}, 'code_largest': {**ok, 'provider_code': 999999},
        **{f'cause_{cause}': {'provider_cause': cause, 'provider_code': 21215} for cause in CAUSES}}
    refused = {'cause_capital': {**ok, 'provider_cause': 'Geo_permission'}, 'cause_space': {**ok, 'provider_cause': 'a b'},
        'cause_digit_first': {**ok, 'provider_cause': '1abc'}, 'cause_empty': {**ok, 'provider_cause': ''},
        'cause_too_long': {**ok, 'provider_cause': 'a' * 49}, 'cause_number': {**ok, 'provider_cause': 5},
        'cause_null': {**ok, 'provider_cause': None}, 'code_zero': {**ok, 'provider_code': 0},
        'code_above_range': {**ok, 'provider_code': 1000000}, 'code_negative': {**ok, 'provider_code': -1},
        'code_text': {**ok, 'provider_code': '21215'}, 'code_float': {**ok, 'provider_code': 21215.0},
        'code_fraction': {**ok, 'provider_code': 21215.5}, 'code_bool': {**ok, 'provider_code': True},
        'code_null': {**ok, 'provider_code': None}, 'code_without_cause': {'provider_code': 21215},
        'unknown_key': {**ok, 'provider_text': 'x'}, 'provider_error_object': {'provider_error': ok}}
    return valid, refused


# spec: ANYM-695
def test_call_failed_provider_keys_are_one_contract_in_the_python_check_and_the_definer_function(modules):
    store = modules[0]
    valid, refused = _provider_error_cases()
    assert not set(valid) & set(refused)
    with store.pool.connection() as c:
        for name, data in {**valid, **refused}.items():
            event = failed_event(1, **data)
            try:
                validate_event(event)
                python = True
            except ModuleContractError:
                python = False
            database = c.execute('SELECT public.module_event_valid(%s,%s,%s,%s,%s,%s,%s,%s) AS ok', (event['id'],
                event['source'], event['type'], event['subject'], event['time'], json.dumps(event['data']),
                event['urgency'], event['expires_at'])).fetchone()['ok']
            assert python == database == (name in valid), name


# spec: ANYM-695
def test_a_five_key_call_failed_publishes_beside_the_three_key_one(modules):
    store = modules[0]
    three = failed_event(1)
    five = failed_event(2, provider_cause='geo_permission_missing', provider_code=21215)
    assert validate_event(five) == 2
    assert [publish(store, three), publish(store, five)] == [1, 2]
    assert publish(store, five) == 2
    with store.pool.connection() as c:
        stored = [row['data'] for row in c.execute('SELECT data FROM module_events ORDER BY seq')]
    assert stored == [three['data'], five['data']]
    with pytest.raises(errors.CheckViolation, match='invalid_module_event'):
        with store.pool.connection() as c:
            c.execute('SELECT public.publish_module_event(%s,%s,%s,%s,%s,%s,%s,%s,%s)', ('calls:9', 'deedkit:calls',
                'call.failed', 'call-1', 'owner-calls', '2026-10-29T10:00:00Z',
                json.dumps({**three['data'], 'provider_code': 21215}), 'batch', None))


# spec: ANYM-695
def test_the_python_kinds_and_the_newest_sql_spec_agree_on_keys_optional_keys_and_subject_for_every_kind():
    newest = [path for path in sorted((SOURCE / 'migrations').glob('*.sql'))
        if 'FUNCTION public.module_event_valid(' in path.read_text()][-1]
    found = re.search(r"specs constant jsonb := '(\{.*?\})';", newest.read_text(), re.S)
    spec = json.loads(found[1])
    assert set(spec) == set(KINDS)
    for kind, (checks, optional, subject) in KINDS.items():
        assert (set(spec[kind]['keys']), set(spec[kind]['optional']), spec[kind]['subject']) == (
            set(checks), set(optional), subject), kind


# spec: ANYM-695
def test_schema65_replaces_only_the_validator_function_keeps_its_acl_and_is_repeatable(modules):
    from messaging_service import store as store_module
    store = modules[0]
    root = Path(store_module.__file__).parent / 'migrations'
    old = (root / '056_module_events.sql').read_text()
    start = old.index('CREATE FUNCTION public.module_event_valid(')
    previous = old[start:old.index('REVOKE ALL ON FUNCTION public.module_event_valid', start)]
    previous = previous.replace('CREATE FUNCTION', 'CREATE OR REPLACE FUNCTION', 1)
    five = failed_event(1, provider_cause='geo_permission_missing', provider_code=21215)
    function = "'public.module_event_valid(text,text,text,text,text,jsonb,text,text)'::regprocedure"
    def accepts(c, event):
        return c.execute('SELECT public.module_event_valid(%s,%s,%s,%s,%s,%s,%s,%s) AS ok', (event['id'],
            event['source'], event['type'], event['subject'], event['time'], json.dumps(event['data']),
            event['urgency'], event['expires_at'])).fetchone()['ok']
    with store.pool.connection() as c:
        # Synthetic disposable database only: the schema-64 function, ledger and version.
        c.execute(previous)
        assert accepts(c, failed_event(1)) and not accepts(c, five)
        c.execute('DELETE FROM archive_migrations WHERE version IN (65,66,67,68,69,70,71)')
        c.execute("UPDATE probe_metadata SET value='64' WHERE name='schema_version'")
        acl = c.execute(f'SELECT proacl::text AS acl, proowner::regrole::text AS owner FROM pg_proc WHERE oid={function}').fetchone()
        grants = c.execute("SELECT oid,relacl::text FROM pg_class WHERE relnamespace='public'::regnamespace ORDER BY oid").fetchall()
    store.migrate()
    store.migrate()
    with store.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '71'
        assert c.execute('SELECT count(*) AS n FROM archive_migrations WHERE version=65').fetchone()['n'] == 1
        assert accepts(c, five) and accepts(c, failed_event(1))
        assert not accepts(c, failed_event(1, provider_code=21215)) and not accepts(c, failed_event(1, provider_text='x'))
        assert c.execute(f'SELECT proacl::text AS acl, proowner::regrole::text AS owner FROM pg_proc WHERE oid={function}').fetchone() == acl
        assert c.execute("SELECT oid,relacl::text FROM pg_class WHERE relnamespace='public'::regnamespace ORDER BY oid").fetchall() == grants
    # The data bound of migration 056 stays, and the migration changes no table, column, index or grant.
    text = (root / '065_call_failed_provider_error.sql').read_text()
    assert 'octet_length(p_data::text)>1024' in text
    assert not re.search(r'\b(GRANT|REVOKE|CREATE\s+TABLE|CREATE\s+INDEX|ALTER\s+TABLE|DROP|INSERT|UPDATE|DELETE)\b', text, re.I)


# spec: ANYM-562
def test_router_role_cannot_publish_free_text_by_calling_sql_directly(modules):
    store = modules[0]
    role = 'anym562_router_' + uuid4().hex[:10]
    with store.pool.connection() as c:
        c.execute(SOURCE.joinpath('module_router_role.sql').read_text().replace('messaging_module_router', role))
    try:
        with pytest.raises(errors.CheckViolation, match='invalid_module_event'), store.pool.connection() as c:
            c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
            c.execute('SELECT public.publish_module_event(%s,%s,%s,%s,%s,%s,%s,%s,%s)', ('calls:1', 'deedkit:calls',
                'call.finished', 'call-1', 'owner-calls', '2026-10-29T10:00:00Z', json.dumps({'call_id': 'call-1',
                'status': 'finished', 'end_reason': 'completed', 'transcript': 'private words'}), 'batch', None))
        with store.pool.connection() as c:
            assert c.execute('SELECT count(*) AS n FROM module_events').fetchone()['n'] == 0
    finally:
        with store.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


# spec: ANYM-562
def test_no_module_hint_is_staged_until_a_dispatcher_that_knows_the_kind_beats(modules, tmp_path):
    store = modules[0]
    token = subscriber(store)
    sid = mixed(store, token)
    bound(store, token, sid, tmp_path)
    beat(store, 55)  # an older dispatcher would read a module position as a message position
    seq = publish(store, call_event(1))
    match_subscriptions(store)
    assert module_hints(store, sid) == []
    assert store.get_subscription(token, sid)['module_stream']['matched_event_count'] == 1
    beat(store)
    match_subscriptions(store)  # no new match: the waiting hint is staged now
    assert [row['through_position'] for row in module_hints(store, sid)] == [seq]


# spec: ANYM-562
def test_an_event_that_expires_after_matching_is_never_announced(modules, tmp_path):
    from messaging_service.event_delivery import dispatch_hints
    store = modules[0]
    token = subscriber(store)
    sid = mixed(store, token)
    bound(store, token, sid, tmp_path)
    beat(store)
    seq = publish(store, call_event(1, kind='call.owner_request'))
    match_subscriptions(store)
    assert [row['state'] for row in module_hints(store, sid)] == ['pending']
    with store.pool.connection() as c:
        c.execute("UPDATE module_events SET expires_at=clock_timestamp()-interval '1 minute' WHERE seq=%s", (seq,))
    stats = dispatch_hints(store, transport=lambda *a, **k: pytest.fail('an expired event was announced'),
        resolver=lambda *a, **k: ['93.184.216.34'])
    assert stats['cancelled'] == 1
    assert [(row['state'], row['error_code']) for row in module_hints(store, sid)] == [
        ('cancelled', 'module_events_expired')]
    assert store.get_subscription_events(token, sid)['module']['items'][0]['expired'] is True


# spec: ANYM-562
def test_a_failing_hint_step_keeps_the_module_matches(modules, monkeypatch):
    store = modules[0]
    token = subscriber(store)
    sid = mixed(store, token)
    seq = publish(store, call_event(1))
    monkeypatch.setattr(module_events, '_hint', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('hint down')))
    stats = match_subscriptions(store)
    view = store.get_subscription(token, sid)['module_stream']
    assert stats['module_matched'] == 1 and view['matched_through'] == seq and view['matched_event_count'] == 1
    assert [item['module_event_id'] for item in store.fetch_subscription_events(token, sid)['module']['items']] == [seq]


# spec: ANYM-562
def test_a_database_error_in_the_hint_step_keeps_the_message_and_module_matches(modules, monkeypatch):
    store = modules[0]
    token = subscriber(store)
    sid = mixed(store, token)
    message = emit(store)
    seq = publish(store, call_event(1))
    monkeypatch.setattr(module_events, '_hint', lambda c, *a: c.execute('SELECT 1/0'))
    match_subscriptions(store)
    assert store.get_subscription(token, sid)['module_stream']['matched_through'] == seq
    events = store.fetch_subscription_events(token, sid)
    assert [item['module_event_id'] for item in events['module']['items']] == [seq]
    assert [item['event_id'] for item in events['items']] == [message]


# spec: ANYM-562
def test_an_event_that_expires_while_a_hint_is_prepared_is_cancelled_under_the_fence(modules, tmp_path):
    from messaging_service.event_delivery import dispatch_hints
    store = modules[0]
    token = subscriber(store)
    sid = mixed(store, token)
    bound(store, token, sid, tmp_path)
    beat(store)
    seq = publish(store, call_event(1, kind='call.owner_request'))
    match_subscriptions(store)

    def resolver(*a, **k):  # runs after the lease, before the fence: the deadline passes meanwhile
        with store.pool.connection() as c:
            c.execute("UPDATE module_events SET expires_at=clock_timestamp()-interval '1 second' WHERE seq=%s", (seq,))
        return ['93.184.216.34']
    stats = dispatch_hints(store, transport=lambda *a, **k: pytest.fail('an expired event was announced'),
        resolver=resolver)
    assert stats['cancelled'] == 1
    with store.pool.connection() as c:
        assert [(r['state'], r['error_code'], r['attempts']) for r in c.execute('''SELECT state,error_code,attempts
            FROM subscription_outbox WHERE subscription=%s AND kind='module_events' ''', (sid,))] == [
            ('cancelled', 'module_events_expired', 1)]


# spec: ANYM-562
def test_passive_module_pages_advance_and_never_repeat_a_retention_gap(modules):
    store = modules[0]
    token = subscriber(store)
    sid = mixed(store, token)
    seqs = [publish(store, call_event(n, f'call-{n}')) for n in range(1, 6)]
    match_subscriptions(store)
    seen, after = [], None
    for _ in range(5):
        page = store.get_subscription_events(token, sid, limit=2, module_after=after)['module']
        seen += [item['module_event_id'] for item in page['items']]
        after = page['next_after']
        if not page['has_more']:
            break
    assert seen == seqs
    page = store.fetch_subscription_events(token, sid, limit=3)
    store.ack_subscription_events(token, sid, page['receipt'], page['ack_version'])
    with store.pool.connection() as c:
        c.execute("UPDATE subscription_module_matches SET matched_at=matched_at-interval '30 days' WHERE subscription=%s",
            (sid,))
        c.execute("INSERT INTO probe_metadata(name,value) VALUES('subscription_retention','enabled') "
            "ON CONFLICT(name) DO UPDATE SET value='enabled'")
    try:
        prune_subscriptions(store)
    finally:
        with store.pool.connection() as c:
            c.execute("DELETE FROM probe_metadata WHERE name='subscription_retention'")
    # The default passive cursor is the ACK position, so the pruned range is never read again.
    default = store.get_subscription_events(token, sid)['module']
    assert default['gap'] is None and [item['module_event_id'] for item in default['items']] == seqs[3:]
    # An explicit cursor below the floor gets the gap once; its next_after reads on.
    stale = store.get_subscription_events(token, sid, module_after=0)['module']
    assert stale['gap']['reason'] == 'retention_gap' and stale['next_after'] == seqs[2]
    resumed = store.get_subscription_events(token, sid, module_after=stale['next_after'])['module']
    assert [item['module_event_id'] for item in resumed['items']] == seqs[3:]


# spec: ANYM-562
def test_code_without_module_streams_reads_messages_only_beside_module_rows(modules, monkeypatch):
    # Emulates a runtime that does not know schema 56: every module path is off (module_events.ready is the
    # only switch); the message matcher, get_updates, passive read, fetch, ACK and retention run unchanged.
    store, old, _ = modules
    token = subscriber(store)
    sid = mixed(store, token)
    publish(store, call_event(1))
    match_subscriptions(store)
    with store.pool.connection() as c:
        module_rows = {name: c.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(sql.Identifier(name))).fetchone()['n']
            for name in ('module_events', 'subscription_module_matches', 'subscription_module_streams')}
    monkeypatch.setattr(module_events, 'ready', lambda c: False)
    before = len(store.get_updates(old, 'probe-old')['items'])
    message = emit(store)
    publish(store, call_event(2, 'call-2'))
    stats = match_subscriptions(store)
    assert stats['matched'] == 1 and stats['module_matched'] == 0
    view = store.get_subscription(token, sid)
    assert 'module_stream' not in view and view['matched_through'] == message
    page = store.fetch_subscription_events(token, sid)
    assert 'module' not in page and [item['event_id'] for item in page['items']] == [message]
    acked = store.ack_subscription_events(token, sid, page['receipt'], page['ack_version'])
    assert acked['acked_through'] == message and 'module_acked_through' not in acked
    assert 'module' not in store.get_subscription_events(token, sid)
    prune_subscriptions(store)
    assert len(store.get_updates(old, 'probe-old')['items']) == before + 1
    with store.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM subscription_module_matches').fetchone()['n'] == \
            module_rows['subscription_module_matches']
        assert c.execute('SELECT count(*) AS n FROM module_events').fetchone()['n'] == module_rows['module_events'] + 1


# spec: ANYM-562
def test_restored_mixed_subscription_keeps_each_stream_and_reopens_no_function(modules, tmp_path):
    from messaging_service.backup import database_manifest, quarantine_restore
    from test_subscription_retention import snapshot_restored
    store = modules[0]
    token = subscriber(store)
    sid = mixed(store, token)
    bound(store, token, sid, tmp_path)
    beat(store)
    first, second = publish(store, call_event(1)), publish(store, call_event(2, 'call-2'))
    messages = [emit(store), emit(store, 'demo-tg-2')]
    match_subscriptions(store)
    page = store.fetch_subscription_events(token, sid, limit=1)
    store.ack_subscription_events(token, sid, page['receipt'], page['ack_version'])
    store.fetch_subscription_events(token, sid)  # an outstanding receipt
    tables = ('module_event_counter', 'module_events', 'subscription_module_sources', 'subscription_module_streams',
        'subscription_module_matches', 'subscription_consumption')
    with store.pool.connection() as c:
        source = database_manifest(c)
        streams = c.execute('SELECT acked_through,matched_through FROM subscription_module_streams').fetchone()
    assert streams == {'acked_through': first, 'matched_through': second}
    assert [row['state'] for row in module_hints(store, sid)] == ['pending']
    functions = ('public.publish_module_event(text,text,text,text,text,text,jsonb,text,text)',
        'public.report_module_reachability(text,boolean)', 'public.module_hints_ready()',
        'public.module_event_valid(text,text,text,text,text,jsonb,text,text)')
    probe = 'anym562_probe_' + uuid4().hex[:10]
    with snapshot_restored(store, tmp_path) as restored:
        with restored.pool.connection() as c:
            c.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(probe)))
        try:
            with restored.pool.connection() as c:
                # pg_restore --no-acl leaves new functions executable by PUBLIC.
                assert all(c.execute('SELECT has_function_privilege(%s,%s,%s) AS x', (probe, name, 'EXECUTE'))
                    .fetchone()['x'] for name in functions)
            quarantine_restore(restored)
            restored.migrate()
            with restored.pool.connection() as c:
                assert not any(c.execute('SELECT has_function_privilege(%s,%s,%s) AS x', (probe, name, 'EXECUTE'))
                    .fetchone()['x'] for name in functions)
                after = database_manifest(c)
                hints = c.execute("SELECT state,error_code FROM subscription_outbox WHERE kind='module_events'").fetchall()
                assert c.execute('SELECT count(*) AS n FROM subscription_receipts').fetchone()['n'] == 0
                assert c.execute('SELECT state FROM subscriptions WHERE id=%s', (sid,)).fetchone()['state'] == 'closed'
                assert c.execute('SELECT acked_through FROM subscription_consumption').fetchone()['acked_through'] == \
                    messages[0]
        finally:
            with restored.pool.connection() as c:
                c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(probe)))
    assert all(after[name] == source[name] for name in tables)
    assert hints == [{'state': 'cancelled', 'error_code': 'restored_disabled'}]


# The deployed release (production runs schema 53, SUBSCRIPTIONS.md "Rollout"). Its source is a sibling
# checkout named by ANYMSG_PREVIOUS_SOURCE, or `git archive` of the commit.
PREVIOUS_RELEASE = '0107b218f035fd011596a5ded901623e8dbb1849'
PREVIOUS_DRIVER = '''
import json, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from messaging_service import heartbeats
from messaging_service.event_delivery import dispatch_hints
from messaging_service.store import RUNTIME_SCHEMA, Store
from messaging_service.subscriptions import match_subscriptions, prune_subscriptions
a = json.loads(sys.argv[2])
store, token, sid, sent = Store(Path(a['dsn']).read_text().strip(), Path(a['files']), 'http://testserver'), \\
    a['token'], a['sid'], []
out = {'schema': RUNTIME_SCHEMA, 'ack_new_receipt': store.ack_subscription_events(token, sid, a['receipt'], a['version'])}
heartbeats.record(store, 'dispatcher')
version = store.update_subscription(token, sid, store.get_subscription(token, sid)['version'], action='pause')['version']
store.update_subscription(token, sid, version, action='resume')
out['matched'] = match_subscriptions(store)
out['dispatch'] = dispatch_hints(store, transport=lambda url, body, headers, **k: sent.append(json.loads(body)) or 204,
    resolver=lambda *x, **k: ['93.184.216.34'])
out['updates'] = [e['event_sequence'] for e in store.get_updates(a['old'], 'probe-previous')['items']]
out['passive'] = store.get_subscription_events(token, sid)
page = store.fetch_subscription_events(token, sid)
out['fetch'] = page
out['ack'] = store.ack_subscription_events(token, sid, page['receipt'], page['ack_version'])
with store.pool.connection() as c:  # every match past the retention window, the module ones too
    for table in ('subscription_matches', 'subscription_module_matches'):
        c.execute(f"UPDATE {table} SET matched_at=matched_at-interval '30 days' WHERE subscription=%s", (sid,))
out['sent'], out['pruned'] = sent, prune_subscriptions(store)
store.close()
print(json.dumps(out, default=str))
'''


def _previous_source(tmp_path):
    import os
    import subprocess
    named = os.environ.get('ANYMSG_PREVIOUS_SOURCE')
    if named:
        return Path(named) / 'services/messaging'
    root = Path(__file__).resolve().parents[1]
    def missing():
        return subprocess.run(['git', 'cat-file', '-e', PREVIOUS_RELEASE + '^{commit}'], cwd=root,
            capture_output=True).returncode
    if missing():  # CI's shallow clone: the repository is public, so one commit is fetched without credentials
        subprocess.run(['git', 'fetch', '--quiet', '--depth=1', 'origin',
            PREVIOUS_RELEASE], cwd=root, capture_output=True, timeout=120)
    if missing():
        # CI must carry the evidence: there an unreachable release fails; a local run may skip.
        (pytest.fail if os.environ.get('CI') else pytest.skip)(
            f'the deployed release {PREVIOUS_RELEASE} is not reachable; set ANYMSG_PREVIOUS_SOURCE')
    archive = subprocess.run(['git', 'archive', PREVIOUS_RELEASE, 'services/messaging'], cwd=root,
        capture_output=True, check=True).stdout
    tmp_path.mkdir()
    subprocess.run(['tar', '-x', '-C', str(tmp_path)], input=archive, check=True)
    return tmp_path / 'services/messaging'


# spec: ANYM-562
def test_the_deployed_release_runs_beside_module_rows_after_the_documented_rollback(modules, tmp_path):
    # Criterion 5 against the deployed code itself: rollback step "cancel pending module hints", then the
    # previous runtime's ACK of a module-bearing receipt, matcher, pause/resume, get_updates, passive read,
    # fetch, ACK, delivery and retention. Module rows stay untouched and wait for module-aware code.
    import os
    import subprocess
    import sys
    source = _previous_source(tmp_path / 'previous')
    store, old, _ = modules
    token = subscriber(store)
    sid = mixed(store, token)
    bound(store, token, sid, tmp_path)
    beat(store)
    message = emit(store)
    seq = publish(store, call_event(1))
    # ANYM-732: a recordings row beside it; the previous runtime ignores it and leaves it untouched.
    recorded = publish(store, recording_event(2), RECORDING_SCOPE)
    assert recorded == seq + 1
    match_subscriptions(store)
    page = store.fetch_subscription_events(token, sid)
    assert [item['module_event_id'] for item in page['module']['items']] == [seq]
    with store.pool.connection() as c:
        c.execute('''UPDATE subscription_outbox SET state='cancelled',error_code='module_rollback',
            finished_at=clock_timestamp() WHERE kind='module_events' AND state IN ('pending','leased')''')
        tables = ('module_events', 'subscription_module_matches', 'subscription_module_sources')
        rows = {name: c.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(sql.Identifier(name))).fetchone()['n']
            for name in tables}
    newer = emit(store, 'demo-tg-2')
    files = tmp_path / 'previous-files'
    files.mkdir()
    store.set_subscription_retention(True)
    try:
        run = subprocess.run([sys.executable, '-c', PREVIOUS_DRIVER, str(source), json.dumps({
            'dsn': os.environ['PROBE_TEST_DSN_FILE'], 'files': str(files), 'token': token, 'sid': sid, 'old': old,
            'receipt': page['receipt'], 'version': page['ack_version']})], capture_output=True, text=True,
            timeout=120)
    finally:
        with store.pool.connection() as c:
            c.execute("DELETE FROM probe_metadata WHERE name='subscription_retention'")
    assert run.returncode == 0, run.stderr[-2000:]
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out['schema'] < 56
    assert out['ack_new_receipt']['acked_through'] == message
    assert out['matched']['matched'] == 1
    for read, expected in ((out['passive'], [message, newer]), (out['fetch'], [newer])):
        assert 'module' not in read and [item['event_id'] for item in read['items']] == expected
    assert out['ack']['acked_through'] == newer
    assert {message, newer} <= set(out['updates']) and out['updates'] == [e['event_sequence']
        for e in store.get_updates(old, 'probe-current')['items']]
    assert out['pruned']['pruned'] == 2 and out['pruned']['retention_failed'] == 0
    assert out['sent'] and all(body['kind'] != 'module_events' for body in out['sent']), (out['dispatch'], _outbox(store))
    with store.pool.connection() as c:
        assert {name: c.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(sql.Identifier(name))).fetchone()['n']
            for name in tables} == rows
    with store.pool.connection() as c:
        assert c.execute('SELECT retention_floor FROM subscriptions WHERE id=%s', (sid,)).fetchone()[
            'retention_floor'] == newer
        assert c.execute('SELECT count(*) AS n FROM subscription_matches WHERE subscription=%s',
            (sid,)).fetchone()['n'] == 0
    view = store.get_subscription(token, sid)
    assert view['module_stream']['state'] == 'held_after_rollback' and view['module_stream']['acked_through'] == 0
    view = store.update_subscription(token, sid, view['version'], action='resume')
    assert [item['module_event_id'] for item in store.fetch_subscription_events(token, sid)['module']['items']] == [seq]


# The runtime production runs today (release n8, migration 066): schema 66, router and module matcher included.
DEPLOYED_RELEASE = '045f37b7fc98647ac1514c59f845104ce7843780'
DEPLOYED_DRIVER = '''
import json, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from messaging_service import backup, event_router, module_events
from messaging_service.store import RUNTIME_SCHEMA, Store
a = json.loads(sys.argv[2])
out = {'schema': RUNTIME_SCHEMA, 'backup_max': backup.SCHEMAS[-1]}
try:
    store = Store(Path(a['dsn']).read_text().strip(), Path(a['files']), 'http://testserver')
    store.migrate()
    out['migrate'] = 'opened'
    store.close()
except RuntimeError as error:
    out['migrate'] = str(error)
for name, event in a['events'].items():
    try:
        module_events.validate_event(event)
        out[name] = 'accepted'
    except module_events.ModuleContractError as error:
        out[name] = str(error)
config = Path(a['files']) / 'router.json'
config.write_text(json.dumps({'modules': [{'source': 'deedkit:recordings', 'url': 'http://recordings-api:13234',
    'token_file': '/run/secrets/t'}]}))
try:
    event_router.load_config(config)
    out['config'] = 'loaded'
except ValueError as error:
    out['config'] = str(error)
print(json.dumps(out))
'''


# spec: ANYM-732
def test_the_deployed_schema66_runtime_refuses_schema67_and_its_router_validator_and_backup_refuse_recordings(modules,
        tmp_path):
    import os
    import subprocess
    import sys
    from messaging_service import backup
    source = _release_source(tmp_path / 'deployed', DEPLOYED_RELEASE)
    files = tmp_path / 'deployed-files'
    files.mkdir()
    events = {'text': recording_event(1), 'access': recording_event(2, 'recording.access.changed')}
    run = subprocess.run([sys.executable, '-c', DEPLOYED_DRIVER, str(source), json.dumps({
        'dsn': os.environ['PROBE_TEST_DSN_FILE'], 'files': str(files), 'events': events})], capture_output=True,
        text=True, timeout=120)
    assert run.returncode == 0, run.stderr[-2000:]
    out = json.loads(run.stdout.strip().splitlines()[-1])
    # Safe refusals, all of them: the runtime will not start on schema 67, its configuration will not name the
    # source, its contract check quarantines the event, and its backup tooling stops below schema 67.
    assert out['schema'] == 66 and out['migrate'] == 'database_newer_than_runtime'
    assert out['config'] == 'invalid_router_config'
    assert (out['text'], out['access']) == ('invalid_envelope', 'invalid_envelope')
    assert out['backup_max'] == 66 and backup.SCHEMAS[-1] == 71


# The deployed consumers never call migrate(): matcher, dispatcher, API reads and retention keep running on the
# database the hand release has moved to schema 67.
DEPLOYED_CONSUMERS = '''
import json, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from messaging_service import heartbeats
from messaging_service.event_delivery import dispatch_hints
from messaging_service.store import RUNTIME_SCHEMA, Store
from messaging_service.subscriptions import match_subscriptions, prune_subscriptions
a = json.loads(sys.argv[2])
store, token, sid, sent = Store(Path(a['dsn']).read_text().strip(), Path(a['files']), 'http://testserver'), \\
    a['token'], a['sid'], []
out = {'schema': RUNTIME_SCHEMA}
heartbeats.record(store, 'dispatcher')
out['matched'] = match_subscriptions(store)
out['dispatch'] = dispatch_hints(store, transport=lambda url, body, headers, **k: sent.append(json.loads(body)) or 204,
    resolver=lambda *x, **k: ['93.184.216.34'])
out['updates'] = [e['event_sequence'] for e in store.get_updates(a['old'], 'probe-deployed')['items']]
out['view'] = store.get_subscription(token, sid)
out['passive'] = store.get_subscription_events(token, sid)
page = store.fetch_subscription_events(token, sid)
out['fetch'] = page
out['ack'] = store.ack_subscription_events(token, sid, page['receipt'], page['ack_version'])
with store.pool.connection() as c:  # every match past the retention window, the module ones too
    for table in ('subscription_matches', 'subscription_module_matches'):
        c.execute(f"UPDATE {table} SET matched_at=matched_at-interval '30 days' WHERE subscription=%s", (sid,))
out['sent'], out['pruned'] = sent, prune_subscriptions(store)
store.close()
print(json.dumps(out, default=str))
'''


# spec: ANYM-732
def test_the_deployed_schema66_consumers_run_beside_recordings_rows_in_the_schema67_database(modules, tmp_path):
    import os
    import subprocess
    import sys
    source = _release_source(tmp_path / 'deployed', DEPLOYED_RELEASE)
    store, old, _ = modules
    token = subscriber(store)
    sid = mixed(store, token)
    bound(store, token, sid, tmp_path)
    beat(store)
    message = emit(store)
    seq = publish(store, call_event(1))
    recorded = [publish(store, recording_event(2), RECORDING_SCOPE),
        publish(store, recording_event(3, 'recording.access.changed'), RECORDING_SCOPE)]
    assert recorded == [seq + 1, seq + 2]
    with store.pool.connection() as c:
        rows = c.execute('SELECT seq,event_key,source,type,data FROM module_events ORDER BY seq').fetchall()
    files = tmp_path / 'deployed-files'
    files.mkdir()
    store.set_subscription_retention(True)
    try:
        run = subprocess.run([sys.executable, '-c', DEPLOYED_CONSUMERS, str(source), json.dumps({
            'dsn': os.environ['PROBE_TEST_DSN_FILE'], 'files': str(files), 'token': token, 'sid': sid, 'old': old})],
            capture_output=True, text=True, timeout=120)
    finally:
        with store.pool.connection() as c:
            c.execute("DELETE FROM probe_metadata WHERE name='subscription_retention'")
    assert run.returncode == 0, run.stderr[-2000:]
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out['schema'] == 66
    # The deployed matcher logs a module-matching failure and carries on with the messages, so stderr is read too.
    assert not re.search(r'traceback|error|exception', run.stderr, re.I), run.stderr[-2000:]
    # The old matcher, reads, fetch, ACK, delivery and retention work, see the message and the call, and never
    # name, match or deliver a recordings row.
    assert message in out['updates'] and out['updates'] == [e['event_sequence']
        for e in store.get_updates(old, 'probe-current')['items']]
    assert [item['module_event_id'] for item in out['passive']['module']['items']] == [seq]
    assert [item['event_id'] for item in out['fetch']['items']] == [message]
    assert [item['module_event_id'] for item in out['fetch']['module']['items']] == [seq]
    text = json.dumps(out, default=str)
    assert 'recording' not in text, text[:2000]
    assert out['pruned']['retention_failed'] == 0 and out['pruned']['pruned'] >= 2 and out['sent']
    assert all(body['kind'] != 'events' or 'recording' not in json.dumps(body) for body in out['sent'])
    with store.pool.connection() as c:
        # The journal is untouched; the matcher went through the recordings rows to the head without matching one;
        # the ACK moved the call's position; retention removed the aged message and call matches, nothing else.
        assert c.execute('SELECT seq,event_key,source,type,data FROM module_events ORDER BY seq').fetchall() == rows
        stream = c.execute('SELECT matched_through,acked_through FROM subscription_module_streams WHERE subscription=%s',
            (sid,)).fetchone()
        assert (stream['matched_through'], stream['acked_through']) == (recorded[-1], seq)
        assert c.execute('SELECT count(*) AS n FROM subscription_matches WHERE subscription=%s',
            (sid,)).fetchone()['n'] == 0
        assert c.execute('SELECT count(*) AS n FROM subscription_module_matches WHERE subscription=%s',
            (sid,)).fetchone()['n'] == 0

def _release_source(tmp_path, commit):
    """A `git archive` of one release (or a sibling checkout named by ANYMSG_PREVIOUS_SOURCE), as _previous_source."""
    import os
    import subprocess
    named = os.environ.get('ANYMSG_PREVIOUS_SOURCE_66')
    if named:
        return Path(named) / 'services/messaging'
    root = Path(__file__).resolve().parents[1]
    def missing():
        return subprocess.run(['git', 'cat-file', '-e', commit + '^{commit}'], cwd=root, capture_output=True).returncode
    if missing():
        subprocess.run(['git', 'fetch', '--quiet', '--depth=1', 'origin', commit], cwd=root, capture_output=True,
            timeout=120)
    if missing():
        (pytest.fail if os.environ.get('CI') else pytest.skip)(
            f'the deployed release {commit} is not reachable; set ANYMSG_PREVIOUS_SOURCE_66')
    archive = subprocess.run(['git', 'archive', commit, 'services/messaging'], cwd=root, capture_output=True,
        check=True).stdout
    tmp_path.mkdir()
    subprocess.run(['tar', '-x', '-C', str(tmp_path)], input=archive, check=True)
    return tmp_path / 'services/messaging'


def _outbox(store):
    with store.pool.connection() as c:
        return c.execute('SELECT kind,state,error_code,through_position,attempts FROM subscription_outbox').fetchall()
