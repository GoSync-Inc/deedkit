"""Module events journal (ANYM-562): contract, storage, streams, old readers; synthetic PostgreSQL only."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from uuid import uuid4

from hypothesis import given, settings, strategies as st
from psycopg import errors, sql
import psycopg
import pytest

from messaging_service import module_events
from messaging_service.module_events import ModuleContractError, read_page, validate_event
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
        'ref_too_long': data(finished, call_id='c' * 129) | {'subject': 'c' * 129}}


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
            database = c.execute('SELECT public.module_event_valid(%s,%s,%s,%s,%s,%s,%s) AS ok', (event['id'],
                event['source'], event['type'], event['subject'], json.dumps(event['data']), event['urgency'],
                event['expires_at'])).fetchone()['ok']
            assert python == database == name.startswith('valid_'), name


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
    # Emulates a runtime that does not know schema 57: every module path is off (module_events.ready is the
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
    functions = ('public.publish_module_event(text,text,text,text,text,timestamptz,jsonb,text,timestamptz)',
        'public.report_module_reachability(text,boolean)', 'public.module_hints_ready()',
        'public.module_event_valid(text,text,text,text,jsonb,text,timestamptz)')
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
