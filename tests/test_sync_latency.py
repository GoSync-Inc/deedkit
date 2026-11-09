"""ANYM-865: get_sync_status(include_latency=true) per-stage latency and what waits now (real store, fixed times)."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest
from starlette.testclient import TestClient

from messaging_service import stage_latency
from messaging_service.app import create_app
from messaging_service.store import APIError
from test_archive import archive as archive, event
from test_delivery_policy import bound, identity, local_window, matched
from test_sync_coverage import ACCOUNT, cov as cov, group, message, publish, stored

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
PENDING_KEYS = {'account', 'stage', 'pending', 'on_request_n', 'held_n'}


def latency(s, key, window=24):
    return s.get_sync_status(key, refresh=True, include_latency=True, latency_window_h=window)


def age_journal(s):
    """Events the fixtures wrote before the test's own timeline move 400 h back, so ids keep their commit order."""
    with s.pool.connection() as c:
        c.execute("UPDATE events SET created_at=clock_timestamp()-interval '400 hours'")


def created(s, mid, when, sent=None):
    with s.pool.connection() as c:
        c.execute("UPDATE events SET created_at=%s WHERE message=%s AND kind='message.created'", (when, mid))
        if sent is not None:
            c.execute('UPDATE messages SET sent_at=%s WHERE id=%s', (sent, mid))


def stage_event(s, mid, kind, attachment, when, state='ready'):
    with s.pool.connection() as c:
        stage = {'attachment.ready': 'original', 'file.text.ready': 'text',
                 'file.transcript.ready': 'transcript'}.get(kind, 'text')
        event_id = s.append_event(c, ACCOUNT, mid, kind, 'live', f'latency-{kind}-{attachment}',
            snapshot_metadata={'processing': {'attachment_id': attachment, 'stage': stage, 'state': state}})
        c.execute('UPDATE events SET created_at=%s WHERE id=%s', (when, event_id))


def timeline(ar, s):
    """Live arrivals 0 (clipped from -1), 1, 2 and 10 s, one service message 3 s and one 30 h ago (5 s); downloads
    5 and 15 s; text 3 s; transcript 4 s; one failure. Times are relative to the database clock."""
    age_journal(s)
    now = datetime.now(UTC)
    base = now - timedelta(hours=2)

    def ingest(n, sent, lag, **changes):
        at = sent.isoformat()
        mid = ar.ingest(event(provider_id=f'lat-{n}', sent_at=at, updated_at=at, **changes))['message']
        created(s, mid, sent + timedelta(seconds=lag), sent)
        return mid

    old = ingest(6, now - timedelta(hours=30, seconds=5), 5)
    mids = [ingest(n, base + timedelta(seconds=10 * n), lag) for n, lag in ((1, 1), (2, 2), (3, 10), (4, -1))]
    ingest(5, base + timedelta(seconds=50), 3, metadata={'is_service': True, 'message_kind': 'system'})
    with s.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type='private' WHERE account=%s", (ACCOUNT,))
        times = {r['message']: r['created_at'] for r in c.execute(
            "SELECT message,created_at FROM events WHERE kind='message.created' AND message=ANY(%s)", (mids,))}
    first, second = times[mids[0]], times[mids[1]]
    stage_event(s, mids[0], 'attachment.ready', 'lat-a1', first + timedelta(seconds=5))
    stage_event(s, mids[1], 'attachment.ready', 'lat-a2', second + timedelta(seconds=15))
    stage_event(s, mids[0], 'file.text.ready', 'lat-a1', first + timedelta(seconds=8))
    stage_event(s, mids[1], 'file.transcript.ready', 'lat-a2', second + timedelta(seconds=19))
    stage_event(s, mids[0], 'file.processing.failed', 'lat-a1', first + timedelta(seconds=9), state='failed')
    return old


def rows(result, account=ACCOUNT):
    return {(r['stage'], r['message_kind']): (r['n'], r['p50_s'], r['p90_s'])
            for r in result['latency'] if r['account'] == account}


# spec: ANYM-865
def test_each_stage_reports_hand_computed_percentiles_over_the_chosen_window(archive):
    ar, s, key, _ = archive
    timeline(ar, s)
    result = latency(s, key)
    assert result['latency_window_h'] == 24 and result['latency_unavailable'] == []
    assert rows(result) == {('arrival', 'message'): (4, 1.5, 7.6), ('arrival', 'service'): (1, 3.0, 3.0),
                            ('download', None): (2, 10.0, 14.0), ('text', None): (1, 3.0, 3.0),
                            ('transcript', None): (1, 4.0, 4.0), ('failed', None): (1, None, None)}
    arrival = next(r for r in result['latency'] if r['stage'] == 'arrival')
    assert (arrival['origin'], arrival['chat_type'], arrival['policy'], arrival['window_h']) == ('live', 'private', None, 24)
    # The 30 h old arrival (5 s) joins at 72 h; a stage with no rows (description) has no row at all.
    assert rows(latency(s, key, 72))[('arrival', 'message')] == (5, 2.0, 8.0)
    assert not any(r['stage'] == 'description' for r in result['latency'])
    assert rows(latency(s, key, 1)) == {}


# spec: ANYM-865
def test_rows_carry_counts_and_seconds_only(archive):
    ar, s, key, _ = archive
    timeline(ar, s)
    result = latency(s, key)
    assert result['latency'] and all(set(r) == set(stage_latency.ROW_KEYS) for r in result['latency'])
    assert all(set(r) <= PENDING_KEYS for r in result['latency_pending'])
    text = json.dumps([result['latency'], result['latency_pending']])
    with s.pool.connection() as c:
        chats = [r['id'] for r in c.execute('SELECT id FROM chats WHERE account=%s', (ACCOUNT,))]
    assert chats and not any(chat in text for chat in chats)
    assert 'Fixture room' not in text and 'lighthouse' not in text and '"42"' not in text


# spec: ANYM-865
def test_the_window_is_one_of_four_and_latency_needs_the_account_summary(archive):
    ar, s, key, _ = archive
    assert 'latency' not in s.get_sync_status(key, refresh=True)
    for window in (0, 2, 25, 169, '24', True, 24.0):
        with pytest.raises(APIError) as refused:
            s.get_sync_status(key, include_latency=True, latency_window_h=window)
        assert refused.value.code == 'invalid_argument'
    with pytest.raises(APIError) as refused:
        s.get_sync_status(key, chat_id='any', include_latency=True)
    assert refused.value.code == 'invalid_argument'
    with pytest.raises(APIError) as refused:
        s.get_sync_status(key, include_latency='yes')
    assert refused.value.code == 'invalid_filter'
    client = TestClient(create_app(s, ['testserver']))
    answer = client.post('/v1/get_sync_status', json={'include_latency': True, 'latency_window_h': 168},
                         headers={'Authorization': 'Bearer ' + key})
    assert answer.status_code == 200 and answer.json()['latency_window_h'] == 168
    assert client.post('/v1/get_sync_status', json={'include_latency': True, 'latency_window_h': 48},
                       headers={'Authorization': 'Bearer ' + key}).json()['error'] == 'invalid_argument'


# spec: ANYM-865
def test_a_stuck_description_and_queued_downloads_show_in_the_now_view(cov):
    ar, s, key = cov
    publish(ar)
    ar.ingest(event(chat='200', provider_id='lat-group-seed'))
    group(s)
    stored(ar, s, 'image/jpeg', enrichment='leased', waited='2 hours')
    message(ar)
    message(ar, chat='200')
    pending = {r['stage']: r for r in latency(s, key)['latency_pending'] if r['account'] == ACCOUNT}
    description = pending['description']
    assert description['pending']['n'] == 1 and description['pending']['oldest_s'] >= 7200
    assert description['on_request_n'] == 0
    download = pending['download']
    assert download['pending']['n'] == 1 and download['on_request_n'] == 1
    assert 0 <= download['pending']['oldest_s'] < 600
    assert 'hint' not in pending


# spec: ANYM-865
def test_hint_rows_split_by_policy_hold_quiet_hours_and_show_only_the_callers_subscriptions(seeded, tmp_path):
    store, _, _ = seeded
    age_journal(store)
    quiet_key, immediate_key = identity(store), identity(store, 'policy-other')
    bound(store, quiet_key, tmp_path, {'quiet_hours': local_window('Europe/Moscow', 60, 60)})
    immediate = bound(store, immediate_key, tmp_path, None, name='policy-other')
    events = [matched(store) for _ in range(5)]

    def view(key):
        result = latency(store, key)
        return ({r['stage']: r for r in result['latency_pending'] if r['account'] == 'telegram-demo'},
                [r for r in result['latency'] if r['stage'] in ('match', 'hint')])

    pending, quiet_rows = view(quiet_key)
    assert pending['hint'] == {'account': 'telegram-demo', 'stage': 'hint', 'pending': {'n': 0, 'oldest_s': None},
                               'held_n': 5}
    # The other key's subscription matched the same five events: they are absent here.
    assert [(r['stage'], r['n']) for r in quiet_rows] == [('match', 5)]
    pending, _ = view(immediate_key)
    assert pending['hint']['pending']['n'] == 5 and pending['hint']['held_n'] == 0
    base = datetime.now(UTC) - timedelta(hours=1)
    with store.pool.connection() as c:
        for i, event_id in enumerate(events):
            matched_at = base + timedelta(seconds=10 * i)
            c.execute('UPDATE events SET created_at=%s WHERE id=%s', (matched_at - timedelta(seconds=2), event_id))
            c.execute('UPDATE subscription_matches SET matched_at=%s WHERE subscription=%s AND event=%s',
                      (matched_at, immediate, event_id))
            c.execute('''UPDATE subscription_outbox SET state='delivered',finished_at=%s
                WHERE subscription=%s AND through_position=%s''', (matched_at + timedelta(seconds=i + 1), immediate,
                                                                   event_id))
    pending, immediate_rows = view(immediate_key)
    assert 'hint' not in pending
    assert [(r['stage'], r['policy'], r['n'], r['p50_s'], r['p90_s']) for r in immediate_rows] == [
        ('match', None, 5, 2.0, 2.0), ('hint', 'immediate', 5, 3.0, 4.6)]


# spec: ANYM-865
def test_a_timed_out_stage_group_is_named_and_the_rest_of_the_status_stays(archive, monkeypatch):
    ar, s, key, _ = archive
    timeline(ar, s)
    monkeypatch.setitem(stage_latency.TIMEOUTS, 24, '200ms')
    monkeypatch.setattr(stage_latency, '_files', lambda _withheld: 'SELECT pg_sleep(2) AS row')
    result = latency(s, key)
    assert result['latency_unavailable'] == ['text', 'transcript', 'description', 'failed']
    assert {r['stage'] for r in result['latency']} == {'arrival', 'download'}
    assert [r['account'] for r in result['sources']] == [ACCOUNT] and 'enrichment_health' in result
    with s.pool.connection() as c:
        assert c.execute("SELECT current_setting('statement_timeout') AS v").fetchone()['v'] == '5s'


# spec: ANYM-865
def test_the_operator_table_prints_the_same_rows(archive, tmp_path, monkeypatch, capsys):
    import messaging_service.__main__ as cli
    ar, s, key, _ = archive
    timeline(ar, s)

    class BorrowedStore:
        def __getattr__(self, name):
            return getattr(s, name)

        def close(self):
            pass  # The fixture owns this connection pool.

    dsn = tmp_path / 'test.dsn'
    dsn.write_text(s.pool.conninfo)
    dsn.chmod(0o600)
    monkeypatch.setattr(cli, 'Store', lambda *_: BorrowedStore())
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn))
    monkeypatch.setattr(sys, 'argv', ['anymsg', 'sync-latency'])
    cli.main()
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ['account', 'stage', 'origin', 'chat_type', 'message_kind', 'policy', 'n', 'p50_s',
                                'p90_s', 'pending_n', 'oldest_s', 'on_request_n', 'held_n']
    assert [ACCOUNT, 'arrival', 'live', 'private', 'message', '4', '1.5', '7.6'] in [line.split() for line in lines]
    assert lines[-1] == 'window_h=24'
    printed = [line.split() for line in lines[1:-1]]
    for row in latency(s, key)['latency']:
        assert [str(row[k]) for k in stage_latency.ROW_KEYS[:9] if row[k] is not None] in printed


def test_the_gateway_catalog_accepts_the_latency_arguments():
    catalog = json.loads((ROOT / 'services/gateway/src/agent_gateway/catalog.json').read_text())
    schema = next(t for t in catalog if t['name'] == 'messages_get_sync_status')['inputSchema']['properties']
    assert schema['include_latency'] == {'default': False, 'title': 'Include Latency', 'type': 'boolean'}
    assert schema['latency_window_h'] == {'default': 24, 'title': 'Latency Window H', 'type': 'integer'}
