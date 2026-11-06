"""Mini enrichment stall, starvation and no-worker alert (ANYM-785). Synthetic rows only."""
import hashlib
import logging
from pathlib import Path
import threading
import time

from hypothesis import given, strategies as st
import pytest
from starlette.testclient import TestClient
from tiden_telemetry import Client

from messaging_service import enrichment_health as health, telemetry
from messaging_service.app import create_app
from messaging_service.presentation import render
from test_db_role_isolation import RolePool, install, remove

TG, WA = 'telegram-demo', 'whatsapp-demo-1'
MESSAGE = {TG: 'demo-tg-1', WA: 'demo-wa-1'}
HASH = 'profile-a'
MINI = health.MINI_PROVIDER
_serial = iter(range(10**9))


@pytest.fixture(autouse=True)
def clean_result(monkeypatch):
    monkeypatch.setattr(health, '_last', None)
    monkeypatch.setattr(health, '_episodes', health.Episodes())


def profile(s, account=TG, kind='audio', provider=MINI, enabled=True, expires='1 hour', observed='0 seconds'):
    with s.pool.connection() as c:
        c.execute('''INSERT INTO processing_profiles(account,kind,provider,model,enabled,observed_at,expires_at)
            VALUES(%s,%s,%s,'synthetic',%s,clock_timestamp()-%s::interval,clock_timestamp()+%s::interval)
            ON CONFLICT(account,kind) DO UPDATE SET provider=excluded.provider,enabled=excluded.enabled,
            observed_at=excluded.observed_at,expires_at=excluded.expires_at''',
            (account, kind, provider, enabled, observed, expires))


def job(s, state='queued', account=TG, kind='audio', provider=MINI, age='1 minute', profile_hash=HASH,
        sent='1 minute', active=True):
    """One attachment on its own message, so each row carries its own sent_at."""
    n = next(_serial)
    identifier, message = f'health-{n}', f'health-message-{n}'
    digest = hashlib.sha256(identifier.encode()).hexdigest()
    with s.pool.connection() as c:
        columns = [r['column_name'] for r in c.execute('''SELECT column_name FROM information_schema.columns
            WHERE table_name='messages' AND is_generated='NEVER' AND table_schema='public' ''')]
        chosen = ['%(id)s' if name == 'id' else '%(provider)s' if name == 'provider_id' else name for name in columns]
        c.execute('INSERT INTO messages('+','.join(columns)+') SELECT '+','.join(chosen)+' FROM messages WHERE id=%(source)s',
            {'id': message, 'provider': identifier, 'source': MESSAGE[account]})
        c.execute('UPDATE messages SET sent_at=clock_timestamp()-%s::interval WHERE id=%s', (sent, message))
        revision = c.execute('SELECT revision FROM messages WHERE id=%s', (message,)).fetchone()['revision']
        c.execute('''INSERT INTO attachments(id,message,digest,filename,mime,bytes,active)
            VALUES(%s,%s,%s,'synthetic.wav','audio/wav',1,%s)''', (identifier, message, digest, active))
        c.execute('''INSERT INTO attachment_enrichment(attachment,source_digest,source_revision,state,profile_hash,
                kind,provider,model,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,'synthetic',clock_timestamp()-%s::interval)
            ON CONFLICT(attachment) DO UPDATE SET source_digest=excluded.source_digest,
            source_revision=excluded.source_revision,state=excluded.state,profile_hash=excluded.profile_hash,
            kind=excluded.kind,provider=excluded.provider,model=excluded.model,updated_at=excluded.updated_at''',
            (identifier, digest, revision, state, profile_hash, kind, provider, age))
    return identifier


def codes(s):
    return health.evaluate(s)['codes']


@pytest.fixture()
def s(seeded):
    with seeded[0].pool.connection() as c:
        c.execute('DELETE FROM processing_profiles')
    return seeded[0]


# spec: ANYM-785
def test_stall_raises_for_a_scope_whose_newest_ready_is_old(s):
    profile(s);job(s);job(s, 'ready', age='7 hours')
    assert codes(s) == {'enrichment_stalled'}


# spec: ANYM-805
def test_withheld_group_rows_raise_no_stall_until_an_agent_asks(s):
    profile(s);queued=job(s);job(s, 'ready', age='7 hours')
    with s.pool.connection() as c:
        c.execute('''INSERT INTO media_jobs(id,account,message,descriptor,state)
            SELECT id,%s,message,'{}'::jsonb,'ready' FROM attachments WHERE id=%s''', (TG, queued))
        c.execute("UPDATE chats SET chat_type='group' FROM messages m WHERE m.id=%s AND chats.id=m.chat", (MESSAGE[TG],))
    assert codes(s) == set()
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET request_key_digest='synthetic-key' WHERE id=%s", (queued,))
    assert codes(s) == {'enrichment_stalled'}


# spec: ANYM-785
def test_another_scopes_fresh_ready_does_not_hide_a_stall(s):
    profile(s);profile(s, WA)
    job(s);job(s, 'ready', age='7 hours')
    job(s, 'ready', account=WA)
    job(s, 'ready', profile_hash='profile-b')
    job(s, 'ready', provider='openai')
    assert codes(s) == {'enrichment_stalled'}


# spec: ANYM-785
def test_fresh_ready_means_no_stall(s):
    profile(s);job(s, age='7 hours');job(s, 'ready', age='5 hours')
    assert codes(s) == set()


# spec: ANYM-785
def test_a_new_scope_with_seconds_old_rows_raises_nothing_and_an_old_one_does(s):
    profile(s);job(s, age='5 seconds')
    assert codes(s) == set()
    job(s, age='7 hours')
    assert codes(s) == {'enrichment_stalled'}


# spec: ANYM-785
def test_a_paused_scope_past_six_hours_is_a_stall(s):
    profile(s);job(s, age='7 hours')
    with s.pool.connection() as c:
        c.execute("UPDATE processing_profiles SET pause_reason='rate_limited',paused_until=clock_timestamp()+interval '1 hour'")
    assert codes(s) == {'enrichment_stalled'}


# spec: ANYM-785
def test_another_providers_backlog_and_unconfigured_rows_count_for_nothing(s):
    profile(s)
    job(s, provider='openai', age='7 hours', sent='7 hours')
    job(s, 'unconfigured', age='7 hours', sent='7 hours')
    assert codes(s) == set()


# spec: ANYM-785
def test_a_live_worker_of_another_provider_account_or_kind_hides_nothing(s):
    profile(s, expires='-1 hour', observed='31 minutes')
    profile(s, kind='image', provider='openai')
    profile(s, kind='document')
    profile(s, WA)
    job(s, age='7 hours')
    # The scope's worker is down, so the rule that reports it is no-worker, not stall.
    assert codes(s) == {'enrichment_no_worker'}


# spec: ANYM-785
def test_a_row_that_can_never_finish_counts_for_nothing(s):
    profile(s);job(s, age='7 hours', active=False)
    assert codes(s) == set()


# spec: ANYM-785
def test_starvation_needs_fifty_rows_sent_between_six_and_forty_eight_hours_ago(s):
    profile(s);job(s, 'ready', age='1 minute')
    for _ in range(health.STARVED_ROWS - 1):job(s, sent='7 hours')
    assert codes(s) == set()
    job(s, sent='7 hours')
    assert codes(s) == {'enrichment_starved'}


# spec: ANYM-785
def test_an_old_history_backfill_is_not_starvation(s):
    profile(s);job(s, 'ready')
    for _ in range(health.STARVED_ROWS):job(s, sent='49 hours')
    assert codes(s) == set()


# spec: ANYM-785
def test_a_draining_backlog_clears_starvation(s):
    profile(s);job(s, 'ready')
    rows = [job(s, sent='7 hours') for _ in range(health.STARVED_ROWS)]
    assert codes(s) == {'enrichment_starved'}
    with s.pool.connection() as c:
        c.execute("UPDATE attachment_enrichment SET state='ready',updated_at=clock_timestamp() WHERE attachment=%s", (rows[0],))
    assert codes(s) == set()


# spec: ANYM-785
@pytest.mark.parametrize('settings,raised', [
    ({'expires': '-1 hour', 'observed': '31 minutes'}, True),
    ({'enabled': False, 'expires': '1 hour', 'observed': '31 minutes'}, True),
    ({'expires': '-1 hour', 'observed': '8 days'}, False),
    ({'expires': '-1 hour', 'observed': '29 minutes'}, False),
    ({'expires': '1 hour', 'observed': '31 minutes'}, False),
    ({'expires': '-1 hour', 'observed': '31 minutes', 'provider': 'openai'}, False)])
def test_no_worker_needs_no_queue(s, settings, raised):
    profile(s, **settings)
    assert codes(s) == ({'enrichment_no_worker'} if raised else set())


# spec: ANYM-785
def test_the_thresholds_are_named_with_the_card_defaults():
    assert (health.STALL_S, health.STARVED_ROWS, health.STARVED_MAX_AGE_S, health.NO_WORKER_S, health.NO_WORKER_MAX_AGE_S,
            health.CHECK_PERIOD_S, health.REPEAT_S) == (6*3600, 50, 48*3600, 1800, 7*86400, 300, 86400)


# spec: ANYM-785
def test_one_event_per_episode_and_a_repeat_after_a_day(s, monkeypatch):
    sent = []
    monkeypatch.setattr(telemetry, 'capture', lambda error, code: sent.append(code) or True)
    profile(s, expires='-1 hour', observed='31 minutes')
    health.check(s, now=0)
    health.check(s, now=300)
    assert sent == ['enrichment_no_worker']
    health.check(s, now=health.REPEAT_S)
    assert sent == ['enrichment_no_worker'] * 2
    profile(s)
    health.check(s, now=health.REPEAT_S + 300)
    profile(s, expires='-1 hour', observed='31 minutes')
    health.check(s, now=health.REPEAT_S + 600)
    assert sent == ['enrichment_no_worker'] * 3


# spec: ANYM-785
def test_an_event_the_local_budget_refused_is_tried_at_the_next_check():
    episodes = health.Episodes()
    answers = iter([False, True])
    assert episodes.report({'enrichment_starved'}, 0, lambda code: next(answers)) == ['enrichment_starved']
    assert episodes.report({'enrichment_starved'}, 300, lambda code: next(answers)) == ['enrichment_starved']
    assert episodes.report({'enrichment_starved'}, 600, lambda code: next(answers)) == []


# spec: ANYM-785
@given(st.lists(st.tuples(st.booleans(), st.integers(0, 3*86400)), max_size=40))
def test_episode_events_are_a_day_apart_unless_the_condition_cleared(steps):
    episodes = health.Episodes()
    now, last, cleared = 0, None, True
    for active, delta in steps:
        now += delta
        sent = episodes.report({'enrichment_stalled'} if active else set(), now, lambda code: True)
        if not active:
            assert sent == []
            cleared = True
            continue
        assert bool(sent) == (cleared or now - last >= health.REPEAT_S)
        if sent:last = now
        cleared = False


class Transport:
    def __init__(self):
        self.envelopes = []

    def send(self, envelope):
        self.envelopes.append(envelope)


# spec: ANYM-785
@pytest.mark.parametrize('code', sorted(telemetry.ENRICHMENT_HEALTH_CODES))
def test_the_final_event_has_exactly_the_allowed_keys_and_its_fingerprint(code, monkeypatch):
    captured = []
    monkeypatch.setattr(telemetry, 'capture', lambda error, c: captured.append(error) or True)
    assert health.raise_event(code)
    event = telemetry.safe_event(captured[0], 'gateway', code)
    event.update(event_id='a'*32, timestamp=1, environment='local-synthetic')
    final = telemetry.before_send(event)
    assert set(final) == {'platform', 'level', 'release', 'environment', 'tags', 'exception', 'fingerprint',
                          'event_id', 'timestamp', 'sdk'}
    assert final['fingerprint'] == ['enrichment-health', 'gateway', code]
    assert final['tags'] == {'component': 'gateway', 'error_code': code}
    value = final['exception']['values'][0]
    assert set(value) == {'type', 'value', 'stacktrace'} and (value['type'], value['value']) == ('RuntimeError', code)
    frames = value['stacktrace']['frames']
    assert frames and all(set(f) == {'filename', 'lineno', 'in_app'} for f in frames)
    assert frames[-1]['filename'] == telemetry.FRAME_PREFIX + 'enrichment_health.py'


# spec: ANYM-785
def test_without_telemetry_the_read_still_works(s, monkeypatch):
    monkeypatch.setattr(telemetry, '_reporter', None)
    profile(s, expires='-1 hour', observed='31 minutes')
    health.check(s, now=0)
    assert health.read([TG])['accounts'][TG]['codes'] == ['enrichment_no_worker']


# spec: ANYM-785
def test_sdk_client_accepts_the_event():
    transport = Transport()
    client = Client('https://synthetic-key@ingest.example.test/synthetic-project',
                    before_send=telemetry.before_send, transport=transport)
    worker = telemetry.Reporter(client, 'gateway', 'local-synthetic')
    try:
        try:raise RuntimeError('enrichment_stalled')
        except RuntimeError as error:assert worker.capture(error, 'enrichment_stalled')
        deadline = time.monotonic() + 5
        while not transport.envelopes and time.monotonic() < deadline:time.sleep(.01)
    finally:
        worker.close()
    assert transport.envelopes


# spec: ANYM-785
def test_sync_status_shows_only_the_principals_accounts_and_runs_no_query(seeded):
    s, one, two = seeded
    with s.pool.connection() as c:c.execute('DELETE FROM processing_profiles')
    assert s.get_sync_status(two, refresh=True)['enrichment_health'] is None
    profile(s);job(s, age='7 hours');job(s, account=WA, age='7 hours');profile(s, WA)
    health.check(s, now=0)
    wide = s.get_sync_status(one, refresh=True)['enrichment_health']
    assert set(wide['accounts']) == {TG, WA} and wide['checked_at']
    narrow = s.get_sync_status(two, refresh=True)['enrichment_health']
    assert set(narrow['accounts']) == {TG}
    assert narrow['accounts'][TG] == {'codes': ['enrichment_stalled'],
                                      'counts': {'audio': {'queued': 1, 'sent_6h_to_48h': 0}}}
    assert render('get_sync_status', s.get_sync_status(two, refresh=True), 'compact')['enrichment_health'] == narrow


# spec: ANYM-785
def test_a_principal_restricted_to_one_chat_gets_null(seeded):
    s, _, _ = seeded
    with s.pool.connection() as c:
        c.execute('DELETE FROM processing_profiles')
        chat = c.execute("SELECT chat FROM messages WHERE id='demo-tg-1'").fetchone()['chat']
    key = s.issue_key('agent-chat-785', [TG])
    s.set_access('agent-chat-785', accounts=[TG], chats=[chat])
    profile(s, expires='-1 hour', observed='31 minutes')
    health.check(s, now=0)
    assert s.get_sync_status(key, refresh=True)['enrichment_health'] is None


# spec: ANYM-785
def test_the_check_runs_under_the_gateway_role_and_writes_nothing(s):
    role = install(s, 'gateway_role.sql', 'messaging_gateway')
    try:
        profile(s);job(s, age='7 hours')
        assert health.evaluate(type('Pool', (), {'pool': RolePool(s, role)})())['codes'] == {'enrichment_stalled'}
    finally:
        remove(s, role)


# spec: ANYM-785
def test_the_transaction_is_read_only_and_bounded(s):
    with health.read_only(s) as c:
        row = c.execute("""SELECT current_setting('transaction_read_only') AS ro,current_setting('statement_timeout') AS st,
            current_setting('lock_timeout') AS lt""").fetchone()
    assert row == {'ro': 'on', 'st': '5s', 'lt': '1s'}


class LifespanStore:
    public_url = 'http://testserver'

    def __init__(self, pool):
        self.pool = pool

    def authenticate(self, token):
        return 'synthetic'

    def audit_boundary(self, code):
        pass

    def capabilities(self, token, detail='full', sections=None):
        return {}


def serve(monkeypatch, store, seen, database=False):
    """A fast API whose heartbeat proves the other lifespan tasks keep running."""
    import messaging_service.heartbeats as heartbeats
    monkeypatch.setattr(health, 'CHECK_PERIOD_S', .01)
    monkeypatch.setattr(heartbeats, 'HEARTBEAT_S', .01)
    real = heartbeats.record
    monkeypatch.setattr(heartbeats, 'record', lambda *args: seen.set() or real(*args) if database else seen.set() or True)
    return TestClient(create_app(store, ['testserver']))


# spec: ANYM-785
def test_a_database_error_leaves_the_api_serving_and_logs_one_fixed_line(monkeypatch, caplog):
    secret = 'SYNTHETIC-PRIVATE-TEXT'
    failed, beat = threading.Event(), threading.Event()

    def boom(store):
        failed.set()
        raise RuntimeError(secret)
    monkeypatch.setattr(health, 'check', boom)
    with caplog.at_level(logging.ERROR), serve(monkeypatch, LifespanStore(object()), beat) as client:
        assert failed.wait(5)
        beat.clear()
        assert beat.wait(5)
        assert client.post('/v1/capabilities', json={}, headers={'Authorization': 'Bearer synthetic'}).status_code == 200
    assert 'enrichment_health_unavailable' in caplog.text and secret not in caplog.text


# spec: ANYM-785
def test_a_query_over_its_timeout_is_cancelled_and_the_old_result_stays(seeded, monkeypatch, caplog):
    s, key, _ = seeded
    with s.pool.connection() as c:c.execute('DELETE FROM processing_profiles')
    profile(s, expires='-1 hour', observed='31 minutes')
    health.check(s, now=0)
    before = health.read([TG])
    monkeypatch.setattr(health, 'STATEMENT_TIMEOUT_MS', 1000)
    monkeypatch.setattr(health, 'WORKERS_SQL', 'SELECT pg_sleep(30)')
    entered, done, beat = threading.Event(), threading.Event(), threading.Event()
    real = health.check

    def timed(store):
        entered.set()
        try:return real(store)
        finally:done.set()
    monkeypatch.setattr(health, 'check', timed)
    started = time.monotonic()
    with caplog.at_level(logging.ERROR), serve(monkeypatch, s, beat, database=True) as client:
        assert entered.wait(5)
        # A database-backed request is served while the check's query is running.
        served = client.post('/v1/get_sync_status', json={'refresh': True}, headers={'Authorization': 'Bearer '+key})
        assert served.status_code == 200 and served.json()['enrichment_health']['accounts'] == before['accounts']
        assert done.wait(10) and time.monotonic() - started < 10
        beat.clear()
        assert beat.wait(5)
    assert 'enrichment_health_unavailable' in caplog.text
    assert health.read([TG]) == before


# spec: ANYM-785
@pytest.mark.parametrize('phase,runs', [('active', True), ('draining', False)])
def test_the_check_runs_only_in_the_active_generation(phase, runs, monkeypatch):
    from types import SimpleNamespace
    called, beat = threading.Event(), threading.Event()
    monkeypatch.setattr(health, 'check', lambda store: called.set())
    import messaging_service.heartbeats as heartbeats
    monkeypatch.setattr(health, 'CHECK_PERIOD_S', .01)
    monkeypatch.setattr(heartbeats, 'record', lambda *_: beat.set() or True)
    from platform_contracts.release import ReleaseRuntime
    monkeypatch.setenv('DEEDKIT_RELEASE_ID', 'synthetic')
    monkeypatch.setattr(ReleaseRuntime, 'from_env', lambda *a, **k: SimpleNamespace(phase=phase, wrap=lambda boundary: boundary))
    with TestClient(create_app(LifespanStore(object()), ['testserver'])):
        assert called.wait(.5 if not runs else 5) == runs


# spec: ANYM-785
def test_the_documents_name_the_three_codes():
    root = Path(__file__).resolve().parents[1]
    production = (root/'docs/operations/production.md').read_text()
    assert 'Nothing alerts anyone' not in production
    for code in telemetry.ENRICHMENT_HEALTH_CODES:
        assert code in (root/'TELEMETRY.md').read_text() and code in production
    assert 'enrichment_health' in (root/'COLLECTOR_STATES.md').read_text()
