"""Module event router (ANYM-575): pull, quarantine, cursor, isolation, delivery; synthetic producers only."""
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from hypothesis import given, settings, strategies as st
from psycopg import errors, sql
from psycopg.rows import dict_row
import psycopg
import pytest

from messaging_service import event_router, module_events
from messaging_service.event_router import HttpModule, split_page
from messaging_service.module_events import ModuleContractError
from messaging_service.subscriptions import match_subscriptions

SOURCE = Path(__file__).resolve().parents[1] / 'services/messaging/messaging_service'
TOKEN = 'synthetic-router-token-0123456789'
MARKER = 'SYNTHETIC-PRIVATE-MARKER'
PUBLIC = lambda *a, **k: ['93.184.216.34']  # noqa: E731


def call_event(seq, call_id='call-1', kind='call.finished', scope='owner-calls', **changes):
    data = {'call.finished': {'call_id': call_id, 'status': 'finished', 'end_reason': 'completed',
            'duration_s': 42, 'outcome': 'task_completed', 'transcript': 'final'},
        'call.owner_request': {'call_id': call_id, 'decision_id': 'decision-1', 'state': 'pending'}}[kind]
    event = {'specversion': '1.0', 'id': f'calls:{seq}', 'source': 'deedkit:calls', 'type': kind,
        'subject': call_id, 'time': '2026-10-29T10:00:00Z', 'data': data,
        'urgency': 'now' if kind == 'call.owner_request' else 'batch',
        'expires_at': '2099-10-29T10:30:00Z' if kind == 'call.owner_request' else None, 'scope_ref': scope}
    return {**event, **changes}


class Producer:
    """A reference producer of the shared contract (SUBSCRIPTIONS.md, Producers' read endpoint) over HTTP."""

    def __init__(self, source='deedkit:calls', journal_id='journal-1'):
        self.source, self.journal_id, self.rows, self.floor = source, journal_id, [], 0
        self.stall, self.status, self.error_body, self.requests = 0, 200, b'', []
        producer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                producer.requests.append(self.path)
                if producer.stall:
                    time.sleep(producer.stall)
                if self.headers.get('Authorization') != 'Bearer ' + TOKEN:
                    return self._send(401, b'{}')
                if producer.status != 200:
                    return self._send(producer.status, producer.error_body)
                url = urlsplit(self.path)
                if url.path == '/v1/events/head':
                    return self._send(200, json.dumps({'journal_id': producer.journal_id, 'head': len(producer.rows)}))
                query = parse_qs(url.query)
                return self._send(200, json.dumps(producer.page(int(query['after'][0]), int(query['limit'][0]))))

            def _send(self, status, body):
                body = body.encode() if isinstance(body, str) else body
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        ThreadingHTTPServer.request_queue_size = 64
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'

    def add(self, event=None):
        """Append one journal event (default a valid call.finished); returns its seq."""
        seq = len(self.rows) + 1
        self.rows.append(event if event is not None else call_event(seq, f'call-{seq}'))
        return seq

    def filtered(self):
        self.rows.append(None)
        return len(self.rows)

    def page(self, after, limit):
        events, last = [], max(after, self.floor)
        for seq in range(max(after, self.floor) + 1, len(self.rows) + 1):
            if len(events) == limit:
                break
            last = seq
            if self.rows[seq - 1] is not None:
                events.append(self.rows[seq - 1])
        return {'events': events, 'next_after': last, 'has_more': last < len(self.rows),
            'retention_floor': self.floor, 'journal_id': self.journal_id}

    def module(self, tmp_path, timeout_s=2):
        token = tmp_path / ('router-' + uuid4().hex + '.token')
        token.write_text(TOKEN + '\n')
        token.chmod(0o600)
        return HttpModule(self.source, self.url, str(token), timeout_s)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def producers():
    made = []

    def make(*args, **kwargs):
        made.append(Producer(*args, **kwargs))
        return made[-1]
    yield make
    for producer in made:
        producer.stall = 0
        producer.close()


@pytest.fixture()
def routed(seeded):
    """The seeded store and a connection factory under the router's restricted role (module_router_role.sql)."""
    store = seeded[0]
    role = 'anym575_router_' + uuid4().hex[:10]
    with store.pool.connection() as c:
        c.execute('TRUNCATE module_events,module_source_health,module_router_cursors,module_router_gaps CASCADE')
        c.execute('UPDATE module_event_counter SET value=0 WHERE id=1')
        c.execute("DELETE FROM component_heartbeats WHERE component='dispatcher'")
        c.execute(SOURCE.joinpath('module_router_role.sql').read_text().replace('messaging_module_router', role))

    def connect():
        conn = psycopg.connect(store.pool.conninfo, autocommit=True, row_factory=dict_row,
            application_name='module-router')
        conn.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)))
        return conn
    yield store, connect
    with store.pool.connection() as c:
        # Open gaps would show as module_gap in other modules' subscription views.
        c.execute('TRUNCATE module_router_cursors,module_router_gaps')
        c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def sweep(connect, *modules):
    event_router.run(connect, list(modules), interval=0, stop=threading.Event(), once=True)


def journal(store):
    with store.pool.connection() as c:
        return [(row['seq'], row['event_key'], row['scope_ref']) for row in
            c.execute('SELECT seq,event_key,scope_ref FROM module_events ORDER BY seq')]


def router_state(store, source='deedkit:calls'):
    with store.pool.connection() as c:
        cursor = c.execute('SELECT after_seq,journal_id,origin FROM module_router_cursors WHERE source=%s',
            (source,)).fetchone()
        gaps = c.execute('''SELECT kind,after_seq,through_seq,code,resolution FROM module_router_gaps
            WHERE source=%s ORDER BY after_seq''', (source,)).fetchall()
    return cursor, gaps


def subscriber(store):
    permissions = ['read', 'subscription-write', 'subscription-ack']
    return store.issue_key('module-reader', ['telegram-demo'], permissions=permissions, key_permissions=permissions)


def mixed(store, token, types=('call.finished', 'call.owner_request'), **create):
    sid = store.create_subscription(token, ['telegram-demo-chat'], **create)['subscription_id']
    with store.pool.connection() as c:
        module_events.attach_sources(c, sid, [('deedkit:calls', 'owner-calls', list(types))])
    return sid


def emit(store, message='demo-tg-1'):
    with store.pool.connection() as c:
        return store.append_event(c, 'telegram-demo', message, 'message.created', 'live')


def beat(store):
    from messaging_service.heartbeats import record
    assert record(store, 'dispatcher')


# Each journal row of a property case: a valid event, a refused one that names its seq, a refused one
# that names none, or a row the producer filtered.
ROWS = st.lists(st.sampled_from(['ok', 'named', 'unnamed', 'filtered']), min_size=0, max_size=30)


# spec: ANYM-575
@settings(max_examples=300, deadline=None)
@given(rows=ROWS, after=st.integers(0, 50))
def test_every_seq_of_a_page_is_published_quarantined_once_or_filtered(rows, after):
    events, kinds = [], {}
    for offset, kind in enumerate(rows, start=1):
        seq = after + offset
        kinds[seq] = kind
        if kind == 'ok':
            events.append(call_event(seq, f'call-{seq}'))
        elif kind == 'named':
            events.append(call_event(seq, f'call-{seq}', data={'call_id': f'call-{seq}', 'title': MARKER}))
        elif kind == 'unnamed':
            events.append({'id': MARKER, 'scope_ref': 'owner-calls'})
    next_after = after + len(rows)
    page = {'events': events, 'next_after': next_after, 'has_more': False, 'retention_floor': 0,
        'journal_id': 'journal-1'}
    published, quarantined, cursor = split_page('deedkit:calls', after, page)
    assert cursor == next_after
    assert [seq for seq, _, _ in published] == [seq for seq, kind in kinds.items() if kind == 'ok']
    assert all(scope == 'owner-calls' and 'scope_ref' not in envelope for _, envelope, scope in published)
    covered = [seq for low, high, _ in quarantined for seq in range(low + 1, high + 1)]
    # Ranges are disjoint, inside the page and never hold a published event; each refused event is in one.
    assert len(covered) == len(set(covered)) and all(after < seq <= next_after for seq in covered)
    assert not set(covered) & {seq for seq, _, _ in published}
    assert {seq for seq, kind in kinds.items() if kind in ('named', 'unnamed')} <= set(covered)
    # A refused event that names its seq is held as exactly that seq.
    for seq, kind in kinds.items():
        if kind == 'named' and (seq - 1 not in kinds or kinds[seq - 1] != 'unnamed'):
            assert any(low == seq - 1 and high == seq for low, high, _ in quarantined) or any(
                low < seq <= high and high - low > 1 for low, high, _ in quarantined)


# spec: ANYM-575
def test_page_breaks_refuse_the_whole_page():
    page = {'events': [call_event(3), call_event(2)], 'next_after': 3, 'has_more': False, 'retention_floor': 0,
        'journal_id': 'journal-1'}
    with pytest.raises(ModuleContractError, match='seq_not_increasing'):
        split_page('deedkit:calls', 0, page)
    with pytest.raises(ModuleContractError, match='next_after_behind_events'):
        split_page('deedkit:calls', 0, {**page, 'events': [call_event(5)]})
    with pytest.raises(ModuleContractError, match='next_after_not_advancing'):
        event_router.check_page({**page, 'events': [], 'next_after': 4, 'has_more': True}, 4, 100)
    with pytest.raises(ModuleContractError, match='page_over_limit'):
        event_router.check_page(page, 0, 1)
    # An event of another module is refused per event, never published under this source.
    other = {**call_event(1), 'source': 'deedkit:calendar'}
    assert split_page('deedkit:calls', 0, {**page, 'events': [other], 'next_after': 1})[0] == []


# spec: ANYM-575
def test_router_starts_at_the_module_head_publishes_with_scope_and_an_earlier_after_replays_once(routed, producers,
        tmp_path):
    store, connect = routed
    producer = producers()
    for _ in range(3):
        producer.add()
    module = producer.module(tmp_path)
    sweep(connect, module)
    assert journal(store) == [] and router_state(store)[0]['after_seq'] == 3
    producer.add()
    producer.filtered()
    producer.add(call_event(6, 'call-6', scope='other-owner'))
    sweep(connect, module)
    assert journal(store) == [(1, 'calls:4', 'owner-calls'), (2, 'calls:6', 'other-owner')]
    assert router_state(store)[0]['after_seq'] == 6
    # The operator's earlier `after`: the replay publishes the older events once and the newer ones again
    # idempotently (event_key), with no duplicate row.
    with store.pool.connection() as c:
        assert event_router.set_cursor(c, 'deedkit:calls', 1)['previous_after'] == 6
    sweep(connect, module)
    assert [key for _, key, _ in journal(store)] == ['calls:4', 'calls:6', 'calls:2', 'calls:3']
    assert router_state(store)[0] == {'after_seq': 6, 'journal_id': 'journal-1', 'origin': 'router'}


# spec: ANYM-575
@pytest.mark.parametrize('crash', ['before_commit', 'after_commit'])
def test_a_crash_before_or_after_the_cursor_commit_replays_without_a_duplicate(routed, producers, tmp_path,
        monkeypatch, crash):
    store, connect = routed
    producer = producers()
    module = producer.module(tmp_path)
    sweep(connect, module)
    for _ in range(4):
        producer.add()
    publish, calls = module_events.publish, []

    def failing(c, event, scope):
        calls.append(event['id'])
        if crash == 'before_commit' and len(calls) == 3:
            raise RuntimeError('crash inside the publish transaction')
        return publish(c, event, scope)
    monkeypatch.setattr(module_events, 'publish', failing)
    if crash == 'after_commit':
        monkeypatch.setattr(event_router, 'report', lambda *a: (_ for _ in ()).throw(RuntimeError('crash')))
    sweep(connect, module)
    if crash == 'before_commit':
        # The whole page rolled back: nothing published, the cursor where it was.
        assert journal(store) == [] and router_state(store)[0]['after_seq'] == 0
    else:
        assert len(journal(store)) == 4 and router_state(store)[0]['after_seq'] == 4
    monkeypatch.undo()
    sweep(connect, module)
    sweep(connect, module)
    assert [key for _, key, _ in journal(store)] == ['calls:1', 'calls:2', 'calls:3', 'calls:4']
    with store.pool.connection() as c:
        assert c.execute('SELECT value FROM module_event_counter').fetchone()['value'] == 4


# spec: ANYM-575
def test_a_stalled_module_never_holds_a_database_transaction(routed, producers, tmp_path):
    store, connect = routed
    producer = producers()
    module = producer.module(tmp_path, timeout_s=3)
    sweep(connect, module)
    producer.add()
    producer.stall = 2
    worker = threading.Thread(target=sweep, args=(connect, module))
    worker.start()
    try:
        deadline = time.monotonic() + 1.5
        while len(producer.requests) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)
        with store.pool.connection() as c:
            rows = c.execute('''SELECT state,xact_start FROM pg_stat_activity WHERE application_name='module-router'
                AND pid<>pg_backend_pid()''').fetchall()
        assert rows and all(row['state'] == 'idle' and row['xact_start'] is None for row in rows)
    finally:
        worker.join(10)
    assert [key for _, key, _ in journal(store)] == ['calls:1']
    with connect() as conn, conn.transaction():
        event_router._bounded(conn)
        settings = {row['name']: row['setting'] for row in conn.execute('''SELECT name,setting FROM pg_settings
            WHERE name IN ('lock_timeout','statement_timeout','transaction_timeout',
            'idle_in_transaction_session_timeout')''')}
    assert settings == {'lock_timeout': '1000', 'statement_timeout': '2000', 'transaction_timeout': '5000',
        'idle_in_transaction_session_timeout': '5000'}


# spec: ANYM-575
def test_a_malformed_event_is_quarantined_the_page_continues_and_coverage_stays_incomplete(routed, producers,
        tmp_path):
    store, connect = routed
    token = subscriber(store)
    sid = mixed(store, token)
    producer = producers()
    module = producer.module(tmp_path)
    sweep(connect, module)
    producer.add()
    bad = producer.add(call_event(2, 'call-2', data={'call_id': 'call-2', 'title': MARKER}))
    producer.add()
    lost = producer.add({'id': 'garbage', 'scope_ref': 'owner-calls'})
    producer.add()
    sweep(connect, module)
    assert [key for _, key, _ in journal(store)] == ['calls:1', 'calls:3', 'calls:5']
    cursor, gaps = router_state(store)
    assert cursor['after_seq'] == 5
    assert [(g['kind'], g['after_seq'], g['through_seq'], g['code'], g['resolution']) for g in gaps] == [
        ('quarantine', bad - 1, bad, 'data_key_not_allowed', None),
        ('quarantine', lost - 1, lost, 'invalid_envelope', None)]
    view = store.get_subscription(token, sid)['module_stream']
    assert [(h['source'], h['state']) for h in view['health']] == [('deedkit:calls', 'module_gap')]
    assert view['health'][0]['since'] and view['sources'][0]['coverage'] == 'incomplete'
    assert MARKER not in json.dumps(view)
    # The producer serves seq 2 correctly now: the re-fetch publishes it and resolves its range.
    producer.rows[bad - 1] = call_event(bad, 'call-2')
    sweep(connect, module)
    assert [key for _, key, _ in journal(store)] == ['calls:1', 'calls:3', 'calls:5', 'calls:2']
    assert [g['resolution'] for g in router_state(store)[1]] == ['refetched', None]
    assert store.get_subscription(token, sid)['module_stream']['sources'][0]['coverage'] == 'incomplete'
    # Seq 4 stays broken: only the operator's explicit abandon (admin CLI) completes the coverage.
    with store.pool.connection() as c:
        with pytest.raises(ValueError, match='module_gap_not_open'):
            event_router.abandon_gap(c, 'deedkit:calls', 'quarantine', bad - 1)
        event_router.abandon_gap(c, 'deedkit:calls', 'quarantine', lost - 1)
    assert [g['resolution'] for g in router_state(store)[1]] == ['refetched', 'abandoned']
    view = store.get_subscription(token, sid)['module_stream']
    assert view['health'] == [] and view['sources'][0]['coverage'] == 'complete'


# spec: ANYM-575
def test_a_pruned_range_and_a_source_reset_are_named_and_never_read_as_new_events(routed, producers, tmp_path):
    store, connect = routed
    producer = producers()
    module = producer.module(tmp_path)
    sweep(connect, module)
    for _ in range(5):
        producer.add()
    producer.floor = 3
    sweep(connect, module)
    assert [key for _, key, _ in journal(store)] == ['calls:4', 'calls:5']
    assert [(g['kind'], g['after_seq'], g['through_seq']) for g in router_state(store)[1]] == [('retention_gap', 0, 3)]
    producer.journal_id, producer.rows, producer.floor = 'journal-2', [], 0
    producer.add()
    sweep(connect, module)
    sweep(connect, module)
    assert len(journal(store)) == 2
    assert [g['kind'] for g in router_state(store)[1] if g['resolution'] is None] == ['retention_gap', 'source_reset']
    with store.pool.connection() as c:
        assert event_router.set_cursor(c, 'deedkit:calls', 0)['reset_holds_lifted'] == 1
    sweep(connect, module)
    assert [key for _, key, _ in journal(store)][-1] == 'calls:1' and router_state(store)[0]['journal_id'] == 'journal-2'


# spec: ANYM-575
def test_dead_slow_or_restarting_modules_leave_message_hints_flowing_within_a_latency_bound(routed, producers,
        tmp_path):
    from messaging_service.event_delivery import dispatch_hints
    store, connect = routed
    token = subscriber(store)
    sid = mixed(store, token)
    _receiver(store, token, sid, tmp_path)
    calls = producers()
    calls_module = calls.module(tmp_path, timeout_s=0.5)
    sweep(connect, calls_module)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        dead_port = probe.getsockname()[1]
    dead = HttpModule('deedkit:calendar', f'http://127.0.0.1:{dead_port}', str(calls_module.token_file), 0.5)
    calls.stall = 3  # slow: every read runs past the router's deadline
    stop = threading.Event()
    worker = threading.Thread(target=event_router.run, args=(connect, [calls_module, dead]),
        kwargs={'interval': 0.05, 'stop': stop})
    worker.start()
    latencies, last = [], None
    try:
        for n in range(20):
            started = time.monotonic()
            last = emit(store, 'demo-tg-1')
            latencies.append(time.monotonic() - started)
            match_subscriptions(store)
            time.sleep(0.05)
        # A message write never waits for the router: 2 s holds with both modules failing, while one
        # stalled read alone lasts 3 s (the bound leaves room for a loaded CI host).
        assert max(latencies) < 2.0, latencies
        assert store.get_subscription(token, sid)['matched_through'] == last
        sent = []
        dispatch_hints(store, transport=lambda url, body, headers, **k: sent.append(json.loads(body)) or 204,
            resolver=PUBLIC, limit=32)
        # Message hints reach the receiver while both modules fail, up to the newest message.
        throughs = [hint['through'] for hint in sent if hint['kind'] == 'events']
        assert throughs and throughs == sorted(throughs) and throughs[-1] == last
        with store.pool.connection() as c:
            unreachable = {row['source'] for row in c.execute(
                'SELECT source FROM module_source_health WHERE unreachable_since IS NOT NULL')}
        assert unreachable == {'deedkit:calls', 'deedkit:calendar'}
        with store.pool.connection() as c:
            # The dead module fails on the connection (a readable token was sent nowhere), not before it.
            assert c.execute("SELECT 1 FROM module_router_cursors WHERE source='deedkit:calendar'").fetchone() is None
        view = store.get_subscription(token, sid)
        assert view['module_stream']['health'][0]['state'] == 'module_unreachable' and view['blocked_reason'] is None
        # Restarting: 503 keeps it unreachable; the answer after the restart clears it.
        calls.stall, calls.status = 0, 503
        time.sleep(0.6)
        calls.status = 200
        calls.add()
        deadline = time.monotonic() + 5
        while journal(store) == [] and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        worker.join(10)
    assert [key for _, key, _ in journal(store)] == ['calls:1']
    assert store.get_subscription(token, sid)['module_stream']['health'] == []


def _trickling_server():
    """Accepts, sends a status line, then one header byte every 0.1 s: each read is fast, the whole is not."""
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(4)
    stop, served = threading.Event(), []

    def serve():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                client, _ = listener.accept()
            except OSError:
                continue
            def trickle(client=client):
                try:
                    client.sendall(b'HTTP/1.1 200 OK\r\n')
                    while not stop.is_set():
                        client.sendall(b'X')
                        time.sleep(0.1)
                except OSError:
                    pass
                finally:
                    client.close()
            served.append(threading.Thread(target=trickle, daemon=True))
            served[-1].start()
    threading.Thread(target=serve, daemon=True).start()
    return listener, stop, served


# spec: ANYM-575
def test_wrong_json_types_are_quarantined_and_one_module_never_stops_another(routed, producers, tmp_path, capsys):
    store, connect = routed
    calls = producers()
    calendar = producers('deedkit:calendar')
    calls_module, calendar_module = calls.module(tmp_path), calendar.module(tmp_path)
    sweep(connect, calls_module, calendar_module)
    calls.add({**call_event(1), 'source': []})
    calls.add({**call_event(2), 'urgency': {'now': True}})
    calls.add({**call_event(3), 'data': [1, 2]})
    calls.add()
    sweep(connect, calls_module, calendar_module)
    assert [key for _, key, _ in journal(store)] == ['calls:4']
    assert [(g['after_seq'], g['through_seq']) for g in router_state(store)[1]] == [(0, 1), (1, 2), (2, 3)]
    # An unexpected failure inside one module's pass: the next module is still read and reported.
    listener, stop, served = _trickling_server()
    slow = HttpModule('deedkit:calls', f'http://127.0.0.1:{listener.getsockname()[1]}', str(calls_module.token_file),
        0.3)
    calendar.rows.append(None)
    with store.pool.connection() as c:
        c.execute("UPDATE module_source_health SET unreachable_since=clock_timestamp() WHERE source='deedkit:calendar'")
    try:
        started = time.monotonic()
        sweep(connect, slow, calendar_module)
        # A total deadline: headers trickled faster than any single read timeout still end the read, and the
        # router closed its socket with it (the server's next write fails) and left no reader running.
        assert time.monotonic() - started < 2.0
        assert served and all(thread.join(2) is None and not thread.is_alive() for thread in served)
        assert not [thread for thread in threading.enumerate() if thread.name == 'module-read']
    finally:
        stop.set()
        listener.close()
    assert router_state(store, 'deedkit:calendar')[0]['after_seq'] == 1
    with store.pool.connection() as c:
        health = {row['source']: row['unreachable_since'] for row in c.execute('SELECT * FROM module_source_health')}
    assert health['deedkit:calendar'] is None and health['deedkit:calls'] is not None
    assert '"code": "module_timeout"' in capsys.readouterr().out


# spec: ANYM-575
def test_an_unexpected_failure_of_one_module_leaves_the_next_module_polled(routed, producers, tmp_path, monkeypatch):
    store, connect = routed
    calls, calendar = producers(), producers('deedkit:calendar')
    calls_module, calendar_module = calls.module(tmp_path), calendar.module(tmp_path)
    sweep(connect, calls_module, calendar_module)
    calls.add()
    calendar.rows.append(None)
    real = event_router.split_page
    monkeypatch.setattr(event_router, 'split_page', lambda source, *a: (_ for _ in ()).throw(RuntimeError('bug'))
        if source == 'deedkit:calls' else real(source, *a))
    sweep(connect, calls_module, calendar_module)
    assert router_state(store)[0]['after_seq'] == 0
    assert router_state(store, 'deedkit:calendar')[0]['after_seq'] == 1


# spec: ANYM-575
def test_a_lock_or_statement_timeout_on_one_cursor_never_stops_the_next_module(routed, producers, tmp_path):
    store, connect = routed
    calls, calendar = producers(), producers('deedkit:calendar')
    calls_module, calendar_module = calls.module(tmp_path), calendar.module(tmp_path)
    sweep(connect, calls_module, calendar_module)
    calls.add()
    calendar.rows.append(None)
    with store.pool.connection() as holder:
        # Another session holds the calls cursor row: the router's publish step meets its lock_timeout.
        holder.execute("SELECT 1 FROM module_router_cursors WHERE source='deedkit:calls' FOR UPDATE")
        sweep(connect, calls_module, calendar_module)
    assert journal(store) == [] and router_state(store)[0]['after_seq'] == 0
    assert router_state(store, 'deedkit:calendar')[0]['after_seq'] == 1
    sweep(connect, calls_module, calendar_module)
    assert [key for _, key, _ in journal(store)] == ['calls:1']


# spec: ANYM-575
def test_a_refetch_resolves_only_a_published_event_and_no_broken_range_starves_a_repaired_one(routed, producers,
        tmp_path):
    store, connect = routed
    producer = producers()
    module = producer.module(tmp_path)
    sweep(connect, module)
    broken = [producer.add(call_event(n, f'call-{n}', data={'call_id': f'call-{n}', 'title': MARKER}))
        for n in range(1, 7)]
    sweep(connect, module)
    assert len([g for g in router_state(store)[1] if g['resolution'] is None]) == 6
    # The producer now filters seq 1: an empty re-fetch publishes nothing, so the range stays open.
    producer.rows[0] = None
    # Only the last broken event is repaired: the rotation reaches it although four earlier ones stay broken.
    producer.rows[5] = call_event(6, 'call-6')
    for _ in range(3):
        sweep(connect, module)
    gaps = {g['after_seq'] + 1: g['resolution'] for g in router_state(store)[1]}
    assert gaps == {seq: ('refetched' if seq == broken[-1] else None) for seq in broken}
    assert [key for _, key, _ in journal(store)] == ['calls:6']


# spec: ANYM-575
def test_a_second_reset_at_the_same_cursor_opens_a_new_gap_and_holds_the_source(routed, producers, tmp_path):
    store, connect = routed
    producer = producers()
    module = producer.module(tmp_path)
    sweep(connect, module)
    producer.add()
    sweep(connect, module)
    producer.journal_id, producer.rows = 'journal-2', []
    sweep(connect, module)
    with store.pool.connection() as c:
        event_router.set_cursor(c, 'deedkit:calls', 1)
    sweep(connect, module)  # adopts journal-2 at the operator's cursor
    assert router_state(store)[0] == {'after_seq': 1, 'journal_id': 'journal-2', 'origin': 'router'}
    producer.journal_id = 'journal-3'
    sweep(connect, module)  # a reset again, at the same cursor
    resets = [g for g in router_state(store)[1] if g['kind'] == 'source_reset']
    assert [(g['after_seq'], g['resolution']) for g in resets] == [(1, 'abandoned'), (1, None)]
    producer.add()
    producer.add()
    sweep(connect, module)
    assert [key for _, key, _ in journal(store)] == ['calls:1'] and router_state(store)[0]['after_seq'] == 1


def _receiver(store, token, sid, tmp_path, policy=None, name='module-reader'):
    from messaging_service.event_delivery import register_receiver
    secret = tmp_path / ('receiver-' + uuid4().hex + '.key')
    secret.write_bytes(b'synthetic receiver secret bytes 0123456789')
    secret.chmod(0o600)
    with store.authorized(token) as (_, p):
        key_id = str(p['key_id'])
    rid = str(uuid4())
    register_receiver(store, rid, name, key_id, 'https://hooks.example.test/wake', str(secret),
        resolver=PUBLIC)
    store.configure_subscription_delivery(token, sid, store.get_subscription(token, sid)['version'], rid,
        delivery_policy=policy)


def _quiet_now():
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo('Europe/Moscow'))
    return {'quiet_hours': {'timezone': 'Europe/Moscow', 'windows': [{'days': ['mon', 'tue', 'wed', 'thu', 'fri',
        'sat', 'sun'], 'start': (now - timedelta(minutes=60)).strftime('%H:%M'),
        'end': (now + timedelta(minutes=60)).strftime('%H:%M')}]}}


def _outbox(store, sid):
    with store.pool.connection() as c:
        return c.execute('''SELECT through_position,state,quiet_bypass,error_code FROM subscription_outbox
            WHERE subscription=%s AND kind='module_events' ORDER BY created_at,id''', (sid,)).fetchall()


# spec: ANYM-575
def test_routed_events_honour_expiry_urgency_and_the_budget(routed, producers, tmp_path, monkeypatch):
    from messaging_service import delivery_policy
    from messaging_service.event_delivery import dispatch_hints
    store, connect = routed
    token = subscriber(store)
    sid = mixed(store, token)
    _receiver(store, token, sid, tmp_path, _quiet_now())
    beat(store)
    producer = producers()
    module = producer.module(tmp_path)
    sweep(connect, module)
    # A deadline that passed before the router read it: published, never matched, never delivered.
    producer.add(call_event(1, kind='call.owner_request', expires_at='2020-01-01T00:00:00Z'))
    sweep(connect, module)
    match_subscriptions(store)
    assert len(journal(store)) == 1 and _outbox(store, sid) == []
    sent = []
    transport = lambda url, body, headers, **k: sent.append(json.loads(body)) or 204  # noqa: E731
    # batch: held by quiet hours like any hint.
    producer.add()
    sweep(connect, module)
    match_subscriptions(store)
    assert dispatch_hints(store, transport=transport, resolver=PUBLIC)['deferred'] == 1 and sent == []
    # now: bypasses quiet hours, and still waits for the key's budget slot.
    producer.add(call_event(3, kind='call.owner_request'))
    sweep(connect, module)
    match_subscriptions(store)
    assert [(row['state'], row['quiet_bypass']) for row in _outbox(store, sid)] == [('pending', False),
        ('pending', True)]
    monkeypatch.setattr(delivery_policy, 'admission_slot', lambda c, owner, now: now + timedelta(minutes=5))
    stats = dispatch_hints(store, transport=transport, resolver=PUBLIC)
    # The batch hint is not due before the quiet window ends; the urgent one waits for the budget slot.
    assert stats['deferred'] == 1 and sent == []
    monkeypatch.undo()
    with store.pool.connection() as c:
        c.execute("UPDATE subscription_outbox SET next_attempt_at=clock_timestamp() WHERE quiet_bypass")
    dispatch_hints(store, transport=transport, resolver=PUBLIC)
    assert [hint['kind'] for hint in sent] == ['module_events'] and sent[0]['through'] == 3


# spec: ANYM-575
def test_a_receiver_gets_kind_subject_and_module_only_and_logs_carry_no_body_url_or_token(routed, producers, tmp_path,
        monkeypatch, capsys):
    from messaging_service import telemetry
    from messaging_service.event_delivery import dispatch_hints
    store, connect = routed
    policy = tmp_path / 'delivery-policy.json'
    policy.write_text(json.dumps({'allow_hosts': ['example.test']}))
    policy.chmod(0o600)
    monkeypatch.setenv('DELIVERY_POLICY_FILE', str(policy))
    store.set_delivery_features(True)
    captured = []
    monkeypatch.setattr(telemetry, 'capture', lambda error, code: captured.append((error, code)))
    try:
        permissions = ['read', 'subscription-write', 'subscription-ack', 'delivery-write']
        token = store.issue_key('endpoint-reader', ['telegram-demo'], permissions=permissions,
            key_permissions=permissions)
        endpoint = store.register_delivery_endpoint(token, 'endpoint-request-575', 'https://hooks.example.test/hook',
            'whsec_c3ludGhldGljLWVuZHBvaW50LXNlY3JldC0wMTIzNDU2Nzg5', resolver=PUBLIC)
        with store.pool.connection() as c:
            c.execute("UPDATE delivery_endpoint_tests SET state='cancelled' WHERE endpoint=%s",
                (endpoint['endpoint_id'],))
        mixed(store, token, delivery={'endpoint_id': endpoint['endpoint_id']})
        operator = mixed(store, token)
        _receiver(store, token, operator, tmp_path, name='endpoint-reader')
        beat(store)
        producer = producers()
        module = producer.module(tmp_path)
        sweep(connect, module)
        producer.add(call_event(1, kind='call.owner_request'))
        producer.add(call_event(2, 'call-2'))
        producer.add(call_event(3, 'call-3', data={'call_id': 'call-3', 'note': MARKER}))
        sweep(connect, module)
        producer.status, producer.error_body = 500, (MARKER + ' ' + TOKEN).encode()
        sweep(connect, module)
        match_subscriptions(store)
        sent = []
        dispatch_hints(store, transport=lambda url, body, headers, **k: sent.append((url, json.loads(body))) or 204,
            resolver=PUBLIC)
    finally:
        store.set_delivery_features(False)
    bodies = {url: body for url, body in sent}
    endpoint_body = bodies['https://hooks.example.test/hook']
    references = endpoint_body['references']
    assert [ref['subject'] for ref in references] == ['call-1', 'call-2']
    # Criterion 4: the kind, the subject reference and the module name, nothing else of the event.
    assert all(set(ref) == {'type', 'source', 'subject'} for ref in references)
    assert references[0] == {'type': 'call.owner_request', 'source': 'deedkit:calls', 'subject': 'call-1'}
    assert set(endpoint_body) == {'type', 'hint_id', 'subscription_id', 'version', 'label', 'references',
        'references_omitted', 'next_call'}
    # The operator receiver gets positions and the hint kind only.
    assert set(bodies['https://hooks.example.test/wake']) == {'hint_id', 'subscription_id', 'subscription_version',
        'through', 'kind', 'host_turn_verified'}
    text = json.dumps(sent)
    for value in ('decision-1', 'task_completed', 'completed', 'owner-calls', '2099-10-29', MARKER):
        assert value not in text
    logs = capsys.readouterr()
    for secret in (MARKER, TOKEN, producer.url, '127.0.0.1'):
        assert secret not in logs.out + logs.err
    assert all(code == 'module_router_failed' for _, code in captured)
    assert {json.loads(line)['code'] for line in logs.out.splitlines() if 'module_router' in line} >= {
        'module_http_status'}


# spec: ANYM-575
def test_router_role_reads_and_moves_only_its_own_rows_and_gateway_reads_tolerate_no_grant(routed):
    store, connect = routed
    with connect() as conn:
        for statement in ('SELECT body FROM messages LIMIT 1', 'SELECT * FROM module_events LIMIT 1',
                "UPDATE subscription_module_sources SET scope_ref='x' WHERE true"):
            with pytest.raises(errors.InsufficientPrivilege), conn.transaction():
                conn.execute(statement)
    role = 'anym575_bare_' + uuid4().hex[:10]
    with store.pool.connection() as c:
        c.execute(sql.SQL('CREATE ROLE {}').format(sql.Identifier(role)))
        try:
            c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
            # A gateway before its schema-58 grant still serves get_subscription: no module_gap, no error.
            assert module_events.router_gaps(c) == {}
            c.execute('RESET ROLE')
        finally:
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


# spec: ANYM-575
def test_a_refused_first_address_falls_back_to_the_next_within_the_deadline(producers, tmp_path, monkeypatch):
    producer = producers()
    producer.add()
    token = producer.module(tmp_path).token_file
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        refused = probe.getsockname()[1]
    port, real = producer.server.server_address[1], socket.getaddrinfo
    name = 'producer.synthetic.test'
    candidates = [
        # A family this host cannot open with this protocol: socket() itself fails (as EAFNOSUPPORT for IPv6).
        (socket.AF_UNIX, socket.SOCK_STREAM, 6, '', '/nonexistent/module.sock'),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', refused)),  # refuses
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))]    # answers
    # Only the synthetic name has several addresses; every other lookup (numeric ones included) stays real.
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a, **k: list(candidates) if host == name
        else real(host, *a, **k))
    module = HttpModule('deedkit:calls', f'http://{name}:{port}', str(token), 2)
    assert module.head() == {'journal_id': 'journal-1', 'head': 1}
    del candidates[2]
    with pytest.raises(event_router.ModuleUnavailable, match='module_unreachable'):
        module.head()


# spec: ANYM-575
def test_router_config_refuses_unsafe_or_unknown_settings(tmp_path):
    good = {'modules': [{'source': 'deedkit:calls', 'url': 'http://calls:13842', 'token_file': '/run/secrets/t'}]}
    path = tmp_path / 'router.json'
    path.write_text(json.dumps(good))
    assert event_router.load_config(path)['timeout_s'] == 5
    for bad in ({**good, 'extra': 1},
            {'modules': [{**good['modules'][0], 'url': 'https://calls:13842'}]},
            {'modules': [{**good['modules'][0], 'url': 'http://user:pw@calls:13842'}]},
            {'modules': [{**good['modules'][0], 'url': 'file:///etc/passwd'}]},
            {'modules': [{**good['modules'][0], 'source': 'deedkit:messages'}]},
            {'modules': good['modules'] * 2},
            {**good, 'timeout_s': 0}):
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError, match='invalid_router_config'):
            event_router.load_config(path)


# spec: ANYM-575
def test_router_overlay_is_opt_in_restricted_and_its_example_config_loads():
    import yaml
    root = Path(__file__).resolve().parents[1] / 'deploy'
    service = yaml.safe_load((root / 'compose.event-router.yaml').read_text())['services']['event-router']
    assert service['command'][:3] == ['python', '-m', 'messaging_service.event_router']
    assert set(service['networks']) == {'backend', 'integration'} and not service.get('ports')
    assert service['read_only'] is True and service['cap_drop'] == ['ALL'] and service['user'] == '10001:10001'
    assert all(volume['read_only'] and volume['bind']['create_host_path'] is False and
        volume['source'].startswith('./private/') for volume in service['volumes'])
    assert 'event-router' not in yaml.safe_load((root / 'compose.yaml').read_text())['services']
    config = event_router.load_config(root / 'event-router.example.json')
    assert {module['source'] for module in config['modules']} == {'deedkit:calls', 'deedkit:calendar'}
    assert all(module['token_file'].startswith('/run/secrets/module-tokens/') for module in config['modules'])
