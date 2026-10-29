"""Call events for the module router (ANYM-563): terminal and owner-request rows, served under the 562 contract."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
import types

import httpx
import pytest

import call_events
import core
from app import create_app
from core import ServiceError, VoiceService
from outbound import ExternalBrief
from test_tasks import Archive

MESSAGING = Path(__file__).resolve().parents[2] / 'messaging' / 'messaging_service' / 'module_events.py'


def contract():
    """The merged ANYM-562 validator itself (messaging_service/module_events.py), not a copy of its rules."""
    if 'psycopg.types.json' not in sys.modules:
        try:
            import psycopg.types.json  # noqa: F401
        except ImportError:
            stub = types.ModuleType('psycopg.types.json')
            stub.Jsonb = object
            sys.modules.setdefault('psycopg', types.ModuleType('psycopg'))
            sys.modules.setdefault('psycopg.types', types.ModuleType('psycopg.types'))
            sys.modules['psycopg.types.json'] = stub
    spec = importlib.util.spec_from_file_location('module_events_562', MESSAGING)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONTRACT = contract()


def read(store, cursor, limit=100, rehearsal=False):
    """One page as the router reads it: strip scope_ref, then the 562 read_page and validate_event."""
    page = store.events_page(cursor['after'], limit, rehearsal)
    assert len(json.dumps(page, separators=(',', ':')).encode()) <= 256 * 1024
    scopes = []
    for event in page['events']:
        scopes.append(event.pop('scope_ref'))
        event.pop('rehearsal', None)
    result = CONTRACT.read_page(cursor, page, limit=limit, max_bytes=256 * 1024)
    return result, scopes


def external(store, principal='alice', approval=False, channel='phone'):
    fields = {'goal': 'Узнать время доставки', 'counterparty': 'Тестовый поставщик', 'language': 'ru',
              'success_fields': ['delivery_window', 'additional_cost'],
              'approval_fields': ['additional_cost'] if approval else []}
    if channel == 'browser':
        from core import BriefInput
        brief = store.prepare(BriefInput(**fields), [], owner_principal=principal)
    else:
        brief = store.prepare(ExternalBrief(**fields, channel=channel, account='twilio', destination='+628123456789'),
                              [], owner_principal=principal)
    call = store.start(brief['id'])
    store.connected(call['id'])
    return call['id']


def answer(store, identity, field, value, tool_id):
    store.transcript(identity, 'user', value)
    return store.tool(identity, 'record_answer', {'field': field, 'value': value, 'evidence': value}, tool_id)


def journal(store):
    return [(kind, json.loads(data)) for kind, data in store.db.execute('SELECT type,data FROM call_events ORDER BY seq')]


# spec: ANYM-563
def test_router_at_head_then_stop_yields_one_terminal_event_across_restarts_and_repeats(tmp_path):
    store = VoiceService(tmp_path)
    identity = external(store)
    head = store.events_head()
    cursor = {'after': head['head'], 'journal_id': head['journal_id']}
    store.stop(identity, 'agent_stop')
    result, scopes = read(store, cursor)
    assert result['status'] == 'events' and scopes == ['alice']
    [event] = result['events']
    assert event['type'] == 'call.finished' and event['subject'] == identity and event['urgency'] == 'now'
    assert event['data']['end_reason'] == 'agent_stop' and event['data']['transcript'] == 'final'
    store.stop(identity, 'agent_stop')
    store.close()
    for _ in range(2):
        store = VoiceService(tmp_path)
        assert store.events_head() == {'journal_id': head['journal_id'], 'head': head['head'] + 1}
        store.close()
    store = VoiceService(tmp_path)
    again, _ = read(store, result['next'])
    assert again['status'] == 'events' and again['events'] == []
    store.close()


# spec: ANYM-563
@pytest.mark.parametrize('reason,kind,data', [
    ('agent_stop', 'call.finished', {'status': 'finished', 'end_reason': 'agent_stop'}),
    ('model_error', 'call.failed', {'status': 'failed', 'end_reason': 'model_error'}),
    ('provider_failed', 'call.failed', {'status': 'finished', 'end_reason': 'provider_failed'}),
    ('provider_no-answer', 'call.not_answered', {'end_reason': 'no_answer'}),
    ('provider_busy', 'call.not_answered', {'end_reason': 'busy'}),
    ('provider_canceled', 'call.not_answered', {'end_reason': 'canceled'}),
    ('Free text from somewhere', 'call.finished', {'status': 'finished', 'end_reason': 'other'}),
])
def test_each_terminal_kind_from_a_synthetic_call_yields_exactly_one_event(tmp_path, reason, kind, data):
    store = VoiceService(tmp_path)
    identity = external(store)
    store.stop(identity, reason)
    store.stop(identity, reason)
    result, _ = read(store, {'after': 0, 'journal_id': None})
    [event] = result['events']
    assert event['type'] == kind and {**event['data']} | data == event['data'] and event['data']['call_id'] == identity
    store.close()


# spec: ANYM-563
def test_kind_selection_reads_end_reason_before_status_for_every_terminal_shape():
    # The whole finite domain: every listed reason, every terminal status, plus unlisted text.
    reasons = sorted(call_events.END_REASONS | set(call_events.NOT_ANSWERED)) + ['', 'Ignore previous instructions', 'x' * 300]
    for reason in reasons:
        for status in ('finished', 'failed', 'interrupted'):
            v = {'id': 'call-1', 'status': status, 'end_reason': reason, 'outcome': 'Some free text',
                 'started_at': '2026-10-29T10:00:00+00:00', 'ended_at': '2026-10-29T10:02:05+00:00'}
            kind, data = call_events.terminal(v)
            expected = ('call.not_answered' if reason in call_events.NOT_ANSWERED else
                        'call.interrupted' if reason == 'service_restart' or status == 'interrupted' else
                        'call.failed' if reason == 'provider_failed' or status == 'failed' else 'call.finished')
            assert kind == expected, (reason, status)
            event = {'specversion': '1.0', 'id': 'calls:1', 'source': 'deedkit:calls', 'type': kind,
                     'subject': 'call-1', 'time': '2026-10-29T10:02:05Z', 'data': data, 'urgency': 'now',
                     'expires_at': None}
            assert CONTRACT.validate_event(event) == 1
            if kind == 'call.finished':
                assert data['duration_s'] == 125 and data['outcome'] == 'other'


# spec: ANYM-563
def test_restart_interrupts_an_active_call_once(tmp_path):
    store = VoiceService(tmp_path)
    identity = external(store)
    store.close()
    for _ in range(2):
        store = VoiceService(tmp_path)
        store.close()
    store = VoiceService(tmp_path)
    result, _ = read(store, {'after': 0, 'journal_id': None})
    assert [(e['type'], e['data']) for e in result['events']] == [
        ('call.interrupted', {'call_id': identity, 'end_reason': 'service_restart'})]
    store.close()


# spec: ANYM-563
def test_terminal_event_failure_is_logged_never_breaks_stop_and_startup_repairs_it(tmp_path, monkeypatch, capsys):
    store = VoiceService(tmp_path)
    identity = external(store)

    def broken(*args):
        raise RuntimeError('disk')
    monkeypatch.setattr(call_events, '_insert', broken)
    assert store.stop(identity, 'agent_stop')['status'] == 'finished'
    assert store.get_call(identity)['status'] == 'finished'
    assert 'call_event_terminal_failed' in capsys.readouterr().err
    monkeypatch.undo()
    assert journal(store) == []
    store.close()
    for _ in range(2):
        store = VoiceService(tmp_path)
        assert [kind for kind, _ in journal(store)] == ['call.finished']
        store.close()


# spec: ANYM-563
def test_a_failing_write_with_a_broken_stderr_still_stops_the_call(tmp_path, monkeypatch):
    store = VoiceService(tmp_path)
    identity = external(store)

    class Broken:
        def write(self, text):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError

    def broken(*args):
        raise RuntimeError('disk')
    monkeypatch.setattr(call_events, '_insert', broken)
    monkeypatch.setattr(sys, 'stderr', Broken())
    assert store.stop(identity, 'agent_stop')['status'] == 'finished'
    monkeypatch.undo()
    assert store.get_call(identity)['status'] == 'finished'
    store.close()


# spec: ANYM-563
def test_a_replayed_terminal_write_takes_no_sequence_number(tmp_path):
    store = VoiceService(tmp_path)
    identity = external(store)
    store.stop(identity, 'agent_stop')
    call = store.get_call(identity)
    for _ in range(2):
        assert call_events.record_terminal(store.db, call)
        store.db.commit()
    assert store.events_head()['head'] == 1
    first, _ = read(store, {'after': 0, 'journal_id': None})
    after, _ = read(store, first['next'])
    assert [e['id'] for e in first['events']] == ['calls:1'] and after['events'] == []
    store.close()


def seed(tmp_path, calls):
    """Write call bodies straight into a calls database, as an older release left them."""
    VoiceService(tmp_path).close()
    db = sqlite3.connect(tmp_path / 'voice.sqlite3')
    db.executemany('INSERT INTO calls VALUES (?,?,?)', [(v['id'], 'brief-' + v['id'], json.dumps(v)) for v in calls])
    db.commit()
    db.close()


def archived(n, decision_at=None):
    started = '2026-10-01T10:00:00+00:00'
    decisions = [] if decision_at is None else [{'id': f'decision-{n}', 'question': 'SECRETQUESTION', 'options': ['a', 'b'],
        'status': 'pending', 'created_at': decision_at, 'conditions': {'fee': 'SECRETCOST'}}]
    return {'id': f'call-{n}', 'brief_id': f'brief-call-{n}', 'status': 'finished', 'channel': 'phone',
            'started_at': started, 'ended_at': '2026-10-01T10:03:00+00:00', 'end_reason': 'agent_stop',
            'outcome': 'needs_owner' if decisions else 'unresolved', 'decisions': decisions, 'answers': {},
            'brief': {'owner_principal': 'alice', 'channel': 'phone'}, 'event_storage': 1}


# spec: ANYM-563
def test_an_existing_calls_database_gets_terminal_rows_and_deadlines_for_its_calls(tmp_path):
    db = tmp_path / 'voice.sqlite3'
    seed(tmp_path, [archived(1), archived(2, decision_at='2026-10-01T10:01:00+00:00')])
    connection = sqlite3.connect(db)
    connection.execute('DROP TABLE call_events')
    connection.execute('DROP TABLE decision_deadlines')
    connection.commit()
    connection.close()
    for _ in range(2):
        store = VoiceService(tmp_path)
        assert [(kind, data['call_id']) for kind, data in journal(store)] == [
            ('call.finished', 'call-1'), ('call.finished', 'call-2')]
        assert store.db.execute('SELECT decision_id,expires_at FROM decision_deadlines').fetchall() == [
            ('decision-2', '2026-10-02T10:01:00.000000Z')]
        store.close()
    store = VoiceService(tmp_path)
    store.expire_decisions()
    call = store.get_call('call-2')
    assert call['decisions'][0]['status'] == 'expired' and call['outcome'] == 'unresolved'
    assert store.db.execute('SELECT count(*) FROM decision_deadlines').fetchone()[0] == 0
    assert [data.get('state') for kind, data in journal(store)][-1] == 'expired'
    store.close()


# spec: ANYM-563
def test_owner_request_follows_its_state_with_the_stored_deadline(tmp_path):
    store = VoiceService(tmp_path)
    identity = external(store, approval=True)
    answer(store, identity, 'delivery_window', '12–13', 'time')
    request = answer(store, identity, 'additional_cost', '50000 IDR', 'fee')['owner_decision']
    decision = store.get_call(identity)['decisions'][0]
    result, scopes = read(store, {'after': 0, 'journal_id': None})
    [event] = result['events']
    assert event['type'] == 'call.owner_request' and scopes == ['alice']
    assert event['data'] == {'call_id': identity, 'decision_id': request['decision_id'], 'state': 'pending'}
    assert event['urgency'] == 'now'
    stored = datetime.fromisoformat(decision['expires_at'])
    assert CONTRACT._at(event['expires_at']) == stored
    assert stored - datetime.fromisoformat(decision['created_at']) == timedelta(seconds=call_events.DECISION_TTL_S)
    store.decide(identity, request['decision_id'], 'Разрешить')
    store.decide(identity, request['decision_id'], 'Разрешить')
    states = [data['state'] for kind, data in journal(store) if kind == 'call.owner_request']
    assert states == ['pending', 'answered']
    # 562 accepts state=pending only: the answered row is journaled and skipped, the cursor moves past it.
    later, _ = read(store, result['next'])
    assert later['events'] == [] and later['next']['after'] == store.events_head()['head']
    # Corrected terms supersede the answered round: one more transition, then a new pending round.
    answer(store, identity, 'delivery_window', '14–15', 'correct-time')
    states = [(data['decision_id'] == request['decision_id'], data['state'])
              for kind, data in journal(store) if kind == 'call.owner_request']
    assert states == [(True, 'pending'), (True, 'answered'), (True, 'superseded'), (False, 'pending')]
    store.close()


# spec: ANYM-563
def test_a_decision_past_its_deadline_is_refused_and_one_before_it_is_accepted(tmp_path):
    store = VoiceService(tmp_path)
    first = external(store, approval=True)
    answer(store, first, 'delivery_window', '12–13', 'time')
    late = answer(store, first, 'additional_cost', '50000 IDR', 'fee')['owner_decision']['decision_id']
    store.stop(first, 'agent_stop')
    second = external(store, approval=True)
    answer(store, second, 'delivery_window', '14–15', 'time')
    early = answer(store, second, 'additional_cost', '70000 IDR', 'fee')['owner_decision']['decision_id']
    store.stop(second, 'agent_stop')
    # Only the first decision is past its deadline at the moment of checking.
    call = store.get_call(first)
    call['decisions'][0]['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    store._save_call(call, report=False)
    with pytest.raises(ServiceError, match='decision_expired'):
        store.decide(first, late, 'Разрешить')
    assert store.decide(second, early, 'Разрешить')['status'] == 'answered'
    expired = store.get_call(first)
    assert expired['decisions'][0]['status'] == 'expired' and expired['outcome'] != 'needs_owner'
    assert [row[0] for row in store.db.execute('SELECT decision_id FROM decision_deadlines')] == [early]
    store.expire_decisions()
    states = [(data['decision_id'], data['state']) for kind, data in journal(store) if kind == 'call.owner_request']
    assert states == [(late, 'pending'), (early, 'pending'), (late, 'expired'), (early, 'answered')]
    inbox = {item['decision']['id']: item['decision']['status'] for item in store.decision_inbox()}
    assert inbox == {late: 'expired', early: 'answered'}
    store.close()


# spec: ANYM-563
def test_expiry_never_touches_a_live_call_or_raises(tmp_path, monkeypatch, capsys):
    store = VoiceService(tmp_path)
    identity = external(store, approval=True)
    answer(store, identity, 'delivery_window', '12–13', 'time')
    request = answer(store, identity, 'additional_cost', '50000 IDR', 'fee')['owner_decision']
    monkeypatch.setattr(core, 'now', lambda: '2099-01-01T00:00:00+00:00')
    store.expire_decisions()
    assert store.get_call(identity)['decisions'][0]['status'] == 'pending'
    store.decide(identity, request['decision_id'], 'Разрешить')
    store.stop(identity, 'agent_stop')
    second = external(store, approval=True)
    answer(store, second, 'delivery_window', '14–15', 'time')
    answer(store, second, 'additional_cost', '70000 IDR', 'fee')
    store.stop(second, 'agent_stop')
    call = store.get_call(second)
    call['decisions'][0]['expires_at'] = 'not a time'
    store._save_call(call, report=False)
    store.expire_decisions()
    assert store.get_call(second)['decisions'][0]['status'] == 'pending'
    assert 'decision_expiry_unreadable' in capsys.readouterr().err
    store.close()


# spec: ANYM-563
def test_events_carry_references_only_over_every_stored_body_shape(tmp_path):
    secrets = ['SECRETWINDOW', 'SECRETCOST', 'SECRETSPEECH', 'SECRETANSWER', '+628123456789', 'Тестовый поставщик']
    store = VoiceService(tmp_path)
    identity = external(store, approval=True)
    store.transcript(identity, 'user', 'SECRETSPEECH привет')
    store.transcript_chunk(identity, 'assistant', 'SECRETSPEECH chunk')
    answer(store, identity, 'delivery_window', 'SECRETWINDOW', 'time')
    first = answer(store, identity, 'additional_cost', 'SECRETCOST', 'fee')['owner_decision']
    store.decide(identity, first['decision_id'], 'SECRETANSWER')
    answer(store, identity, 'delivery_window', 'SECRETWINDOW2', 'time-2')
    store.tool(identity, 'finish_task', {'summary': 'SECRETSPEECH summary'}, 'finish')
    store.stop(identity, 'agent_stop')
    for reason in ('model_error', 'provider_no-answer'):
        store.stop(external(store), reason)
    external(store)
    store.close()
    store = VoiceService(tmp_path)
    store.stop(external(store, channel='browser'), 'owner_stop')
    shapes = {kind: set(data) for kind, data in journal(store)}
    assert shapes == {
        'call.owner_request': {'call_id', 'decision_id', 'state'},
        'call.finished': {'call_id', 'status', 'end_reason', 'duration_s', 'outcome', 'transcript'},
        'call.failed': {'call_id', 'status', 'end_reason'},
        'call.not_answered': {'call_id', 'end_reason'},
        'call.interrupted': {'call_id', 'end_reason'},
    }
    rows = json.dumps(store.db.execute('SELECT * FROM call_events').fetchall(), ensure_ascii=False)
    page = json.dumps(store.events_page(0, 500, True), ensure_ascii=False)
    for secret in secrets:
        assert secret not in rows and secret not in page
    for kind, data in journal(store):
        if kind == 'call.owner_request':
            assert data['state'] in {'pending', 'answered', 'superseded', 'expired'}
    store.close()


# spec: ANYM-563
def test_rehearsals_and_owner_calls_without_a_principal_are_excluded_unless_asked(tmp_path):
    store = VoiceService(tmp_path)
    store.stop(external(store, channel='browser'), 'owner_stop')
    store.stop(external(store, principal=None), 'owner_stop')
    result, _ = read(store, {'after': 0, 'journal_id': None})
    assert result['events'] == [] and result['next']['after'] == 2
    page = store.events_page(0, 100, True)
    assert [(e['rehearsal'], e['scope_ref']) for e in page['events']] == [(True, 'alice')]
    assert all('rehearsal' not in e for e in store.events_page(0, 100)['events'])
    store.close()


# spec: ANYM-563
def test_page_moves_past_filtered_rows_and_reports_a_cursor_beyond_the_head_as_reset(tmp_path):
    store = VoiceService(tmp_path)
    for _ in range(3):
        store.stop(external(store), 'agent_stop')
    head = store.events_head()
    first, _ = read(store, {'after': 0, 'journal_id': head['journal_id']}, limit=2)
    assert [e['id'] for e in first['events']] == ['calls:1', 'calls:2']
    page = store.events_page(2, 2)
    assert page['has_more'] is False and page['next_after'] == 3
    reset, _ = read(store, {'after': 99, 'journal_id': head['journal_id']})
    assert reset['status'] == 'reset'
    other, _ = read(store, {'after': 0, 'journal_id': 'another-journal'})
    assert other['status'] == 'reset'
    store.close()


def registry(tmp_path):
    path = tmp_path / 'agents.json'
    principals = [('alice', ['calls:read', 'calls:stop'], ['phone']), ('router', ['calls:events'], []),
                  ('mixed', ['calls:events', 'calls:read'], [])]
    path.write_text(json.dumps({'version': 1, 'revision': 1, 'principals': [
        {'id': name, 'key_sha256': hashlib.sha256(name.encode()).hexdigest(), 'enabled': True,
         'expires_at': '2099-01-01T00:00:00Z', 'scopes': scopes, 'channels': channels}
        for name, scopes, channels in principals]}))
    path.chmod(0o600)
    return path


# spec: ANYM-563
async def test_events_endpoint_reads_only_with_the_router_token(tmp_path):
    app = create_app(tmp_path / 'data', Archive(), control_token='owner-only', agent_registry=registry(tmp_path))
    async with app.router.lifespan_context(app):
        service = app.state.agent_api.service
        service.stop(external(service), 'agent_stop')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver') as c:
            router = {'Authorization': 'Bearer router'}
            head = await c.get('/v1/events/head', headers=router)
            assert head.status_code == 200 and head.json() == {'journal_id': service.journal_id, 'head': 1}
            page = await c.get('/v1/events', params={'after': 0, 'limit': 100}, headers=router)
            assert page.status_code == 200 and [e['type'] for e in page.json()['events']] == ['call.finished']
            assert set(page.json()) == {'events', 'next_after', 'has_more', 'retention_floor', 'journal_id'}
            refetch = await c.get('/v1/events', params={'after': 0, 'limit': 1}, headers=router)
            assert refetch.json()['events'][0]['id'] == 'calls:1'
            for token in ('alice', 'mixed', 'owner-only'):
                for path in ('/v1/events', '/v1/events/head'):
                    response = await c.get(path, headers={'Authorization': 'Bearer ' + token})
                    assert response.status_code in {401, 403} and 'events' not in response.json()
            assert (await c.get('/v1/events')).status_code == 401
            # The router's token is good for nothing else.
            assert (await c.post('/v1/capabilities', json={}, headers=router)).status_code == 403
            assert (await c.post('/v1/list_calls', json={'limit': 10, 'offset': 0}, headers=router)).status_code == 403
            for params in ({'after': -1}, {'after': 'x'}, {'limit': 0}, {'limit': 201}, {'include_rehearsal': 2},
                           {'after': 2**53}, {'other': 1}):
                assert (await c.get('/v1/events', params=params, headers=router)).status_code == 422


# spec: ANYM-563
def test_reading_ten_thousand_events_keeps_event_writes_of_an_active_call_within_bound(tmp_path, monkeypatch):
    # 10k archived calls, each with a terminal row and a due owner request: every page read also runs expiry.
    seed(tmp_path, [archived(n, decision_at='2026-10-01T10:01:00+00:00') for n in range(10000)])
    monkeypatch.setattr(core, 'EXPIRY_INTERVAL_S', 0)
    store = VoiceService(tmp_path)
    assert store.events_head()['head'] == 10000
    identity = external(store)
    writer, held = threading.current_thread(), []

    class Timed:
        """The service RLock (core.py VoiceService.lock), timing how long the call's writer waits for it."""
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            started = time.perf_counter()
            self.inner.acquire()
            if threading.current_thread() is writer:
                held.append(time.perf_counter() - started)

        def __exit__(self, *args):
            self.inner.release()
    store.lock = Timed(threading.RLock())

    def write():
        started = time.perf_counter()
        store.runtime_event(identity, {'kind': 'audio_probe', 'level': 1})
        return time.perf_counter() - started
    alone = [write() for _ in range(40)]
    held.clear()
    done, pages = threading.Event(), []

    def reader():
        while not done.is_set():
            after = 0
            while True:
                page = store.events_page(after, call_events.MAX_LIMIT)
                pages.append(len(page['events']))
                after = page['next_after']
                if not page['has_more']:
                    break
    thread = threading.Thread(target=reader)
    thread.start()
    waits = []
    try:
        while thread.is_alive() and (sum(pages) < 10000 or len(waits) < 40):
            waits.append(write())
    finally:
        done.set()
        thread.join()
    # Stated bound (README): while 10k events are read back to back and expiry runs on every page, a live call's
    # event write waits for the service lock, at the 99th percentile, no longer than its own slowest write alone
    # plus 100 ms, and never a second; the whole write is never a second slower. A read section holds the lock
    # for one 200-row scan or one expiry batch of at most 20 calls with one commit.
    assert sum(pages) >= 10000 and held, sum(pages)
    assert store.db.execute("SELECT count(*) FROM call_events WHERE data LIKE '%expired%'").fetchone()[0] > 0
    p99 = sorted(held)[int(len(held) * 0.99)]
    assert p99 < max(alone) + 0.1 and max(held) < 1.0 and max(waits) < max(alone) + 1.0, (p99, max(held), max(waits), max(alone))
    store.close()
