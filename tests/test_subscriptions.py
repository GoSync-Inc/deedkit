"""Durable structural subscriptions; isolated synthetic PostgreSQL only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading

from psycopg.types.json import Jsonb
import pytest

from messaging_service.store import APIError
from messaging_service.subscriptions import match_subscriptions
from test_sending import sender  # noqa: F401


def key(store, principal='subscriber', accounts=None, **kwargs):
    return store.issue_key(principal, accounts or ['telegram-demo'],
        permissions=['read', 'subscription-write'], key_permissions=['read', 'subscription-write'], **kwargs)


def create(store, token, **kwargs):
    return store.create_subscription(token, ['telegram-demo-chat'], **kwargs)


def emit(store, message='demo-tg-1', kind='message.created', origin='live'):
    with store.pool.connection() as c:
        account = c.execute('SELECT account FROM messages WHERE id=%s', (message,)).fetchone()['account']
        return store.append_event(c, account, message, kind, origin)


def test_defaults_start_at_committed_head_and_do_not_ack(seeded):
    store, old, _ = seeded
    token = key(store)
    subscription = create(store, token, label='news')
    assert subscription['state'] == 'active' and subscription['version'] == 1
    assert subscription['start_position'] == 4
    assert subscription['kinds'] == ['message.created'] and subscription['origins'] == ['live']
    historical = emit(store, origin='backfill')
    live = emit(store)
    match_subscriptions(store)
    page = store.get_subscription_events(token, subscription['subscription_id'])
    assert [item['event_id'] for item in page['items']] == [live]
    assert historical not in [item['event_id'] for item in page['items']]
    assert page['subscription']['matcher_caught_up']
    assert page['items'][0]['sender'] == 'demo-person-1'
    assert 'body' not in page['items'][0]
    assert page['consumption'] == 'passive_read_no_ack'
    assert store.get_subscription_events(token, subscription['subscription_id']) == page
    assert len(store.get_updates(old, 'existing')['items']) == 5


def sent_ago(store, message, interval):
    """The message was sent this long ago; an event appended afterwards snapshots that time."""
    with store.pool.connection() as c:
        c.execute('UPDATE messages SET sent_at=clock_timestamp()-%s::interval WHERE id=%s', (interval, message))


# spec: ANYM-841
def test_matcher_runs_a_backlog_without_sleeping_and_sleeps_only_after_an_empty_pass(seeded, tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from messaging_service import subscription_worker
    store, _, _ = seeded
    token = key(store)
    sid = create(store, token)['subscription_id']
    events = [emit(store) for _ in range(3)]
    waits = []

    class Stop:
        def is_set(self):
            return bool(waits) and waits[-1] > 0

        def set(self):
            waits.append(-1)

        def wait(self, seconds):
            waits.append(seconds)

    class Kept:
        pool = store.pool

        def close(self):
            pass

    dsn = tmp_path / 'synthetic.dsn'
    dsn.write_text('postgresql://synthetic.invalid/unused')
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn))
    monkeypatch.setattr(sys, 'argv', ['subscription_worker', '--batch-size', '1'])
    monkeypatch.setattr(subscription_worker, 'Store', lambda *a, **k: Kept())
    monkeypatch.setattr(subscription_worker, 'threading', SimpleNamespace(Event=Stop))
    monkeypatch.setattr(subscription_worker, 'signal', SimpleNamespace(SIGINT=2, SIGTERM=15, signal=lambda *a: None))
    # No listener: the fake stop's waits are the matcher's only sleeps.
    monkeypatch.setattr(subscription_worker.wakes, 'listen', lambda *a: None)
    subscription_worker.main()
    # Three one-event batches back to back, then one empty pass that sleeps the interval.
    assert waits[-1] == 2 and waits[:-1] and set(waits[:-1]) == {0} and len(waits) >= 4
    assert store.get_subscription(token, sid)['matched_through'] == events[-1]


def wake_listener(store, channel, **kwargs):
    import threading
    import time
    from messaging_service import wake
    stop = threading.Event()
    listener = wake.Listener.for_store(store, channel, stop, **kwargs)
    due = time.monotonic() + 10
    while not listener.listening and time.monotonic() < due:
        time.sleep(0.02)
    assert listener.listening
    # The (re)connect ring is not a commit wake.
    listener.wait(0.2, listener.token())
    return listener, stop


# spec: ANYM-841
def test_a_committed_event_wakes_the_matcher_before_the_interval_and_a_rolled_back_one_does_not(seeded):
    import time
    from messaging_service import wake
    store, _, _ = seeded
    listener, stop = wake_listener(store, wake.EVENTS)
    try:
        token = listener.token()
        with pytest.raises(RuntimeError):
            with store.pool.connection() as c:
                store.append_event(c, 'telegram-demo', 'demo-tg-1', origin='live')
                raise RuntimeError('synthetic rollback')
        assert listener.wait(1.0, token) is False
        started = time.monotonic()
        emit(store)
        assert listener.wait(2.0, token) is True and time.monotonic() - started < 2.0
    finally:
        stop.set()
        listener.close()


# spec: ANYM-841
def test_a_failing_notify_leaves_the_event_committed_and_is_logged_as_a_count(seeded, monkeypatch, capsys):
    import json
    import time
    from messaging_service import wake
    store, _, _ = seeded
    before = wake.failures()
    # An invalid statement_timeout makes the notifier's NOTIFY step raise inside PostgreSQL.
    monkeypatch.setattr(wake, 'NOTIFY_TIMEOUT', 'not-a-duration')
    capsys.readouterr()
    event = emit(store)
    with store.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM events WHERE id=%s', (event,)).fetchone()['n'] == 1
    due = time.monotonic() + 5
    while wake.failures() == before and time.monotonic() < due:
        time.sleep(0.02)
    assert wake.failures() == before + 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [{'error': 'wake_notify_failed', 'class': 'InvalidParameterValue', 'count': before + 1}]


# spec: ANYM-841
def test_a_stalled_notify_or_a_broken_log_never_holds_or_fails_the_writer(seeded, monkeypatch):
    import sys
    import threading
    import time
    from messaging_service import wake
    store, _, _ = seeded
    listener, stop = wake_listener(store, wake.EVENTS)
    real_send, entered, release = wake.send, threading.Event(), threading.Event()
    before, saved = wake.failures(), sys.stdout

    def stalled(pool, channels):
        entered.set()
        release.wait(30)
        return real_send(pool, channels)

    class Broken:
        def write(self, _):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError

    try:
        monkeypatch.setattr(wake, 'send', stalled)
        started = time.monotonic()
        emit(store)
        assert entered.wait(5)
        emit(store)  # coalesced behind the stalled send, never waiting for it
        assert time.monotonic() - started < 2
        # The stalled send then fails inside PostgreSQL and its log line hits a broken stdout.
        monkeypatch.setattr(wake, 'NOTIFY_TIMEOUT', 'not-a-duration')
        monkeypatch.setattr(sys, 'stdout', Broken())
        release.set()
        due = time.monotonic() + 5
        while wake.failures() == before and time.monotonic() < due:
            time.sleep(0.02)
        assert wake.failures() > before
        monkeypatch.setattr(sys, 'stdout', saved)
        monkeypatch.setattr(wake, 'NOTIFY_TIMEOUT', '500ms')
        monkeypatch.setattr(wake, 'send', real_send)
        # The notifier thread survived both: the next commit still wakes the listener.
        token = listener.token()
        emit(store)
        assert listener.wait(5, token) is True
    finally:
        release.set()
        stop.set()
        listener.close()


# spec: ANYM-841
def test_notifies_follow_exactly_the_committed_marked_transactions(seeded):
    from hypothesis import given, settings, strategies as st, HealthCheck
    import psycopg
    from messaging_service import wake
    store, _, _ = seeded
    steps = st.lists(st.tuples(st.sets(st.sampled_from(wake.CHANNELS)), st.booleans()), max_size=6)

    @settings(max_examples=15, deadline=None, derandomize=True,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(steps)
    def check(transactions):
        with psycopg.connect(store.pool.conninfo, autocommit=True) as listener:
            for channel in wake.CHANNELS:
                listener.execute('LISTEN ' + channel)
            for channels, commit in transactions:
                try:
                    with store.pool.connection() as c:
                        c.execute('SELECT 1')
                        for channel in channels:
                            wake.mark(c, channel)
                        if not commit:
                            raise RuntimeError('synthetic rollback')
                except RuntimeError:
                    pass
            got = list(listener.notifies(timeout=0.3))
            # The notifier coalesces: every channel a committed transaction marked gets an empty notification, a
            # channel only rolled-back transactions marked gets none.
            assert {n.channel for n in got} == {
                channel for channels, commit in transactions if commit for channel in channels}
            assert all(n.payload == '' for n in got)

    check()


# spec: ANYM-733
@pytest.mark.parametrize('origin,kind,sent_at,expected', [
    ('reconcile', 'message.created', '2026-11-03T06:01:00+00:00', 'live_recovered'),
    ('reconcile', 'message.created', '2026-11-03T06:00:00+00:00', 'live_recovered'),
    ('reconcile', 'message.created', '2026-11-03T05:59:00+00:00', 'reconcile'),
    ('reconcile', 'message.created', '2026-11-03T12:00:00+00:00', 'live_recovered'),
    ('reconcile', 'message.created', '2026-11-03T12:00:01+00:00', 'reconcile'),
    ('reconcile', 'message.created', '2026-11-03T06:01:00', 'reconcile'),
    ('reconcile', 'message.created', 'not a time', 'reconcile'),
    ('reconcile', 'message.created', None, 'reconcile'),
    ('reconcile', 'message.edited', '2026-11-03T11:00:00+00:00', 'reconcile'),
    ('reconcile', 'message.deleted', '2026-11-03T11:00:00+00:00', 'reconcile'),
    ('backfill', 'message.created', '2026-11-03T11:00:00+00:00', 'backfill'),
    ('live', 'message.created', '2026-11-03T11:00:00+00:00', 'live'),
    ('fixture', 'message.created', '2026-11-03T11:00:00+00:00', 'fixture')])
def test_view_origin_marks_only_a_recent_reconcile_created_message_as_recovered(origin, kind, sent_at, expected):
    from messaging_service.subscriptions import view_origin
    committed = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    assert view_origin(origin, kind, sent_at, committed) == expected


# spec: ANYM-733
def test_live_recovered_events_match_live_and_reconcile_selectors_and_are_not_historical(seeded):
    store, _, _ = seeded
    token = key(store)
    selectors = {'default': None, 'live': ['live'], 'recovered': ['live_recovered'], 'reconcile': ['reconcile'],
        'mixed': ['live', 'reconcile'], 'old_only': ['backfill', 'fixture']}
    sids = {name: create(store, token, label=name, origins=origins)['subscription_id']
        for name, origins in selectors.items()}
    plain = emit(store)
    # Sent 1 hour ago and first stored by reconcile: recovered. Sent in October and first stored by reconcile: historical.
    sent_ago(store, 'demo-tg-2', '1 hour')
    recovered = emit(store, 'demo-tg-2', origin='reconcile')
    old = emit(store, 'demo-tg-1', origin='reconcile')
    match_subscriptions(store)
    pages = {name: store.get_subscription_events(token, sid)['items'] for name, sid in sids.items()}
    assert {name: [item['event_id'] for item in items] for name, items in pages.items()} == {
        'default': [plain, recovered], 'live': [plain, recovered], 'recovered': [recovered],
        'reconcile': [recovered, old], 'mixed': [plain, recovered, old], 'old_only': []}
    item = pages['recovered'][0]
    assert item['origin'] == 'live_recovered' and item['historical'] is False
    assert 'historical_event' not in item['wake_reason']
    # A selector that lists `reconcile` asked for the stored origin: every reconcile message stays historical for it.
    for name in ('reconcile', 'mixed'):
        by_event = {item['event_id']: item for item in pages[name]}
        assert [(by_event[event]['origin'], by_event[event]['historical']) for event in (recovered, old)] == [
            ('reconcile', True)] * 2, name
    assert [(item['origin'], item['historical']) for item in pages['live']] == [('live', False), ('live_recovered', False)]
    with store.pool.connection() as c:
        assert c.execute('SELECT origin FROM events WHERE id=%s', (recovered,)).fetchone()['origin'] == 'reconcile'
    with pytest.raises(APIError) as error:
        create(store, token, origins=['live_recovered', 'unknown'])
    assert error.value.code == 'invalid_filter'


# spec: ANYM-733
def test_a_recovered_message_wakes_like_a_live_one_and_an_old_reconcile_one_does_not(seeded):
    store, _, _ = seeded
    token = key(store)
    sid = create(store, token, origins=['live', 'live_recovered'], wake_rules=['incoming_message'])['subscription_id']
    # A selector that lists `reconcile` keeps the stored origin, so a reconcile message never satisfies a live rule.
    listed = create(store, token, origins=['live', 'reconcile'], wake_rules=['incoming_message'])['subscription_id']
    old = emit(store, origin='reconcile')
    sent_ago(store, 'demo-tg-2', '1 hour')
    recovered = emit(store, 'demo-tg-2', origin='reconcile')
    match_subscriptions(store)
    items = store.get_subscription_events(token, sid)['items']
    assert [item['event_id'] for item in items] == [recovered]
    assert items[0]['wake_reason'] == ['incoming_message']
    assert old not in [item['event_id'] for item in items]
    assert store.get_subscription_events(token, listed)['items'] == []


def test_independent_source_sets_senders_and_event_revisions(seeded):
    store, _, _ = seeded
    token = key(store, accounts=['telegram-demo', 'whatsapp-demo-1'])
    subscription = store.create_subscription(token, ['telegram-demo-chat', 'whatsapp-demo-1-chat'],
        sender_ids=['demo-person-2'], kinds=['message.created', 'message.edited'])
    ignored = emit(store, 'demo-tg-1')
    selected = emit(store, 'demo-tg-2')
    with store.pool.connection() as c:
        c.execute("UPDATE messages SET sender='changed-sender',revision=revision+1 WHERE id='demo-tg-2'")
    changed = emit(store, 'demo-tg-2', kind='message.edited')
    match_subscriptions(store, batch_size=1)
    match_subscriptions(store)
    page = store.get_subscription_events(token, subscription['subscription_id'])
    assert [item['event_id'] for item in page['items']] == [selected]
    assert page['items'][0]['sender'] == 'demo-person-2' and page['items'][0]['revision'] == 1
    assert page['subscription']['matched_through'] == changed
    assert ignored < selected < changed


def test_same_sender_string_across_accounts_is_not_a_person_merge(seeded):
    store, _, _ = seeded
    token = key(store, accounts=['telegram-demo', 'whatsapp-demo-1'])
    with pytest.raises(APIError, match='ambiguous_reference'):
        store.create_subscription(token, ['telegram-demo-chat', 'whatsapp-demo-1-chat'],
            sender_ids=['demo-person-1'])


def test_key_ownership_and_scope_denials(seeded):
    store, old, other = seeded
    with pytest.raises(APIError, match='forbidden'):
        create(store, old)
    token = key(store)
    subscription = create(store, token)
    second_key = key(store)
    assert store.list_subscriptions(second_key)['items'] == []
    for unauthorized in (second_key, other):
        with pytest.raises(APIError, match='not_found'):
            store.get_subscription(unauthorized, subscription['subscription_id'])
        with pytest.raises(APIError, match='not_found'):
            store.get_subscription_events(unauthorized, subscription['subscription_id'])
    with pytest.raises(APIError, match='not_found'):
        store.create_subscription(token, ['whatsapp-demo-1-chat'])
    with pytest.raises(APIError, match='not_found'):
        create(store, token, sender_ids=['unknown-person'])
    reader = store.issue_key('read-only', ['telegram-demo'], permissions=['read'], key_permissions=['read'])
    with pytest.raises(APIError, match='forbidden'):
        create(store, reader)


def test_legacy_ceiling_does_not_inherit_new_permission(seeded):
    store, old, _ = seeded
    store.set_access('agent-a', permissions=['read', 'subscription-write'])
    with pytest.raises(APIError, match='forbidden'):
        create(store, old)
    assert 'subscription-write' not in store.capabilities(old)['permissions']


@pytest.mark.parametrize('changes', [
    {'chat_ids': []}, {'chat_ids': 'telegram-demo-chat'}, {'sender_ids': []},
    {'kinds': []}, {'kinds': ['message.created', 'arbitrary']}, {'origins': ['bad']},
    {'direction': 'both'}, {'label': 'x' * 129}, {'label': 'unsafe\nlabel'},
    {'expires_at': '2020-01-01T00:00:00Z'}, {'expires_at': '2030-01-01'},
])
def test_creation_strict_validation(seeded, changes):
    store, _, _ = seeded
    token = key(store)
    values = {'chat_ids': ['telegram-demo-chat'], **changes}
    with pytest.raises(APIError):
        store.create_subscription(token, **values)


def test_pause_resume_catchup_skip_close_and_version_conflict(seeded):
    store, _, _ = seeded
    token = key(store)
    sid = create(store, token)['subscription_id']
    paused = store.update_subscription(token, sid, 1, action='pause')
    missed = emit(store)
    assert match_subscriptions(store)['checked'] == 0
    with pytest.raises(APIError, match='subscription_conflict'):
        store.update_subscription(token, sid, 1, action='resume')
    resumed = store.update_subscription(token, sid, paused['version'], action='resume')
    match_subscriptions(store)
    assert [e['event_id'] for e in store.get_subscription_events(token, sid)['items']] == [missed]
    paused = store.update_subscription(token, sid, resumed['version'], action='pause')
    skipped = emit(store)
    resumed = store.update_subscription(token, sid, paused['version'], action='resume', resume_policy='skip')
    assert resumed['gap']['reason'] == 'explicit_resume_skip'
    future = emit(store)
    match_subscriptions(store)
    assert [e['event_id'] for e in store.get_subscription_events(token, sid)['items']] == [missed, future]
    assert skipped not in [e['event_id'] for e in store.get_subscription_events(token, sid)['items']]
    closed = store.update_subscription(token, sid, resumed['version'], action='close')
    assert closed['state'] == 'closed'
    emit(store)
    assert match_subscriptions(store)['checked'] == 0
    with pytest.raises(APIError, match='subscription_closed'):
        store.update_subscription(token, sid, closed['version'], action='resume')


def test_scope_changes_mask_and_never_revive_definition(seeded):
    store, _, _ = seeded
    token = key(store)
    sid = create(store, token, label='private label')['subscription_id']
    emit(store)
    match_subscriptions(store)
    store.set_access('subscriber', chats=[])
    page = store.get_subscription_events(token, sid)
    assert page['items'] == [] and page['scope_masked']
    assert set(page['subscription']) == {'subscription_id', 'state', 'version', 'recreate_required', 'scope_masked'}
    assert 'private label' not in str(page)
    store.set_access('subscriber', chats=None)
    emit(store)
    assert match_subscriptions(store)['checked'] == 0
    assert store.get_subscription(token, sid)['state'] == 'access_changed'
    with pytest.raises(APIError, match='subscription_access_changed'):
        store.update_subscription(token, sid, 2, action='resume')


def test_key_permission_change_revocation_and_expiry_stop_matcher(seeded):
    store, _, _ = seeded
    token = key(store)
    sid = create(store, token)['subscription_id']
    credential = store.capabilities(token)['credential_id']
    store.set_key_permissions(credential, ['read'])
    emit(store)
    assert match_subscriptions(store)['access_changed'] == 1
    assert store.get_subscription_events(token, sid)['scope_masked']
    revoked = key(store, 'revoked')
    revoked_id = create(store, revoked)['subscription_id']
    store.revoke(revoked)
    assert match_subscriptions(store)['access_changed'] == 1
    with store.pool.connection() as c:
        assert c.execute('SELECT state FROM subscriptions WHERE id=%s', (revoked_id,)).fetchone()['state'] == 'access_changed'
    expiring = key(store, 'expiring')
    expiring_id = create(store, expiring)['subscription_id']
    with store.pool.connection() as c:
        c.execute("UPDATE subscriptions SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (expiring_id,))
    assert match_subscriptions(store)['expired'] == 1
    assert store.get_subscription(expiring, expiring_id)['state'] == 'expired'


ALL_VISIBLE = {'all_visible_chats': True}


def dynamic_key(store, principal='dynamic-reader', accounts=('telegram-demo', 'whatsapp-demo-1')):
    permissions = ['read', 'subscription-write', 'subscription-ack']
    return store.issue_key(principal, list(accounts), permissions=permissions, key_permissions=permissions)


def chat_message(store, chat, message, *, chat_type=None, metadata=None, account='telegram-demo'):
    """A synthetic chat (created once) and an incoming message in it."""
    with store.pool.connection() as c:
        c.execute('INSERT INTO chats(id,account,title,chat_type) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
            (chat, account, 'Synthetic chat', chat_type))
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            VALUES(%s,%s,%s,%s,'demo-person-9','Synthetic text',clock_timestamp())''', (message, account, chat, message))
        if metadata:
            c.execute('INSERT INTO message_metadata(message,revision,metadata) VALUES(%s,1,%s)', (message, Jsonb(metadata)))
    return message


def event_ids(store, token, sid):
    return [item['event_id'] for item in store.get_subscription_events(token, sid)['items']]


# spec: ANYM-522
def test_dynamic_sources_show_chats_visible_now_and_a_readded_chat_only_new_matches(seeded):
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token = dynamic_key(store)
        chat_message(store, 'telegram-demo-chat-2', 'demo-tg-x')
        created = store.create_subscription(token, sources=ALL_VISIBLE)
        sid = created['subscription_id']
        assert created['sources'] == {'all_visible_chats': True, 'account_ids': None, 'include_channels': False,
            'include_bots': False}
        assert 'chat_ids' not in created and created['state'] == 'active'
        with store.pool.connection() as c:
            assert c.execute('SELECT count(*) AS n FROM subscription_sources WHERE subscription=%s',
                (sid,)).fetchone()['n'] == 0
        first, removed, other = emit(store), emit(store, 'demo-tg-x'), emit(store, 'demo-wa-1')
        match_subscriptions(store)
        assert event_ids(store, token, sid) == [first, removed, other]
        store.set_access('dynamic-reader', chats=['telegram-demo-chat', 'whatsapp-demo-1-chat'])
        view = store.get_subscription(token, sid)
        assert view['state'] == 'active' and view['matched_event_count'] == 2
        assert event_ids(store, token, sid) == [first, other]
        outside, inside = emit(store, 'demo-tg-x'), emit(store)
        match_subscriptions(store)
        assert event_ids(store, token, sid) == [first, other, inside]
        store.set_access('dynamic-reader', chats=None)
        readded = emit(store, 'demo-tg-x')
        match_subscriptions(store)
        # The chat is back: only what matched after the re-add shows, never the earlier or the outside match.
        assert event_ids(store, token, sid) == [first, other, inside, readded]
        assert outside not in event_ids(store, token, sid)
        store.set_access('dynamic-reader', accounts=['telegram-demo'])
        assert event_ids(store, token, sid) == [first, inside, readded]
        assert store.get_subscription(token, sid)['matched_event_count'] == 3
    finally:
        store.set_dynamic_subscriptions(False)


# spec: ANYM-522
def test_dynamic_definition_survives_version_changes_and_fails_closed_on_lost_access(seeded):
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token = dynamic_key(store)
        sid = store.create_subscription(token, sources=ALL_VISIBLE)['subscription_id']
        explicit = create(store, token)['subscription_id']
        store.set_access('dynamic-reader', chats=['telegram-demo-chat'])
        credential = store.capabilities(token)['credential_id']
        store.set_key_permissions(credential, ['read', 'subscription-write'])
        event = emit(store)
        match_subscriptions(store)
        assert store.get_subscription(token, sid)['state'] == 'active'
        assert event_ids(store, token, sid) == [event]
        # Explicit chat_ids definitions keep today's behaviour.
        assert store.get_subscription(token, explicit)['state'] == 'access_changed'
        assert store.update_subscription(token, sid, 1, action='pause')['state'] == 'paused'
        assert store.update_subscription(token, sid, 2, action='resume')['state'] == 'active'
        store.set_key_permissions(credential, ['read'])
        masked = store.get_subscription(token, sid)
        assert masked['state'] == 'access_changed' and masked['scope_masked']
        assert store.get_subscription_events(token, sid)['items'] == []
    finally:
        store.set_dynamic_subscriptions(False)


# spec: ANYM-522
def test_dynamic_sources_wait_for_the_operator_switch_and_validate_their_form(seeded):
    from messaging_service import __version__
    from messaging_service.store import RUNTIME_SCHEMA
    store, _, _ = seeded
    token = dynamic_key(store)
    with pytest.raises(APIError, match='dynamic_sources_disabled'):
        store.create_subscription(token, sources=ALL_VISIBLE)
    with store.pool.connection() as c:
        saved = c.execute('SELECT * FROM component_heartbeats').fetchall()
    def beat(matcher_version):
        with store.pool.connection() as c:
            for component, version in (('api', __version__), ('matcher', matcher_version), ('dispatcher', __version__)):
                c.execute('''INSERT INTO component_heartbeats(component,version,schema_version,beat_at)
                    VALUES(%s,%s,%s,clock_timestamp()) ON CONFLICT(component) DO UPDATE SET version=EXCLUDED.version,
                    schema_version=EXCLUDED.schema_version,beat_at=EXCLUDED.beat_at''', (component, version, RUNTIME_SCHEMA))
    try:
        # Mixed versions: an older matcher would scan a dynamic definition, match nothing and advance.
        beat('0.0.1')
        assert store.set_dynamic_subscriptions(True, verify_components=True) == {'error': 'component_version_mismatch',
            'component': 'matcher', 'version': '0.0.1', 'expected': __version__}
        with pytest.raises(APIError, match='dynamic_sources_disabled'):
            store.create_subscription(token, sources=ALL_VISIBLE)
        beat(__version__)
        assert store.set_dynamic_subscriptions(True, verify_components=True) == {'dynamic_subscriptions': 'enabled'}
        for arguments, code in [
                ({'chat_ids': ['telegram-demo-chat'], 'sources': ALL_VISIBLE}, 'invalid_filter'),
                ({}, 'invalid_filter'),
                ({'sources': {'all_visible_chats': False}}, 'invalid_filter'),
                # ANYM-825: a valid chat_types waits for its own operator switch.
                ({'sources': {**ALL_VISIBLE, 'chat_types': ['group']}}, 'source_filters_disabled'),
                ({'sources': {**ALL_VISIBLE, 'include_bots': 'yes'}}, 'invalid_filter'),
                ({'sources': {**ALL_VISIBLE, 'account_ids': ['whatsapp-demo-2']}}, 'not_found'),
                ({'sources': ALL_VISIBLE, 'sender_ids': ['demo-person-1']}, 'invalid_filter'),
                ({'sources': ALL_VISIBLE, 'reply_to_send': 'send_synthetic'}, 'invalid_subscription'),
                ({'chat_ids': ['telegram-demo-chat'], 'wake_rules': ['reply_to_own_send']}, 'invalid_subscription')]:
            with pytest.raises(APIError, match=code):
                store.create_subscription(token, **arguments)
        sid = store.create_subscription(token, sources={**ALL_VISIBLE, 'account_ids': ['whatsapp-demo-1']})['subscription_id']
        emit(store)
        selected = emit(store, 'demo-wa-1')
        match_subscriptions(store)
        assert event_ids(store, token, sid) == [selected]
    finally:
        store.set_dynamic_subscriptions(False)
        with store.pool.connection() as c:
            c.execute('DELETE FROM component_heartbeats')
            for row in saved:
                c.execute('INSERT INTO component_heartbeats(component,version,schema_version,beat_at) VALUES(%s,%s,%s,%s)',
                    (row['component'], row['version'], row['schema_version'], row['beat_at']))


# spec: ANYM-522
def test_dynamic_sources_leave_out_channels_and_bots_unless_included(seeded):
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token = dynamic_key(store)
        chat_message(store, 'telegram-demo-channel', 'demo-tg-channel', chat_type='channel')
        chat_message(store, 'telegram-demo-chat', 'demo-tg-bot', metadata={'sender_is_bot': True})
        default = store.create_subscription(token, sources=ALL_VISIBLE)['subscription_id']
        everything = store.create_subscription(token, sources={**ALL_VISIBLE, 'include_channels': True,
            'include_bots': True})['subscription_id']
        person, channel, bot = emit(store), emit(store, 'demo-tg-channel'), emit(store, 'demo-tg-bot')
        match_subscriptions(store)
        assert event_ids(store, token, default) == [person]
        assert event_ids(store, token, everything) == [person, channel, bot]
    finally:
        store.set_dynamic_subscriptions(False)


class source_filters:
    """ANYM-825: both operator switches on for the block, off afterwards."""
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self.store.set_dynamic_subscriptions(True)
        self.store.set_source_filters(True)

    def __exit__(self, *exc):
        self.store.set_source_filters(False)
        self.store.set_dynamic_subscriptions(False)


def typed_archive(store):
    """Synthetic accounts whose chats are created by the real adapters and archive ingest path."""
    from messaging_service.archive import Archive
    archive = Archive(store)
    archive.register('tg-types', 'telegram', 'Synthetic typed chats', discover=True)
    archive.register('wa-types', 'whatsapp', 'Synthetic typed chats', discover=True)
    return archive


def telegram_in(archive, chat, number, *, group=False):
    """An incoming Telegram message through telegram_message: the adapter types the chat private or group."""
    from types import SimpleNamespace
    from telethon import types
    from messaging_service.adapters import telegram_message
    entity = types.Chat(id=chat, title='Synthetic group', photo=types.ChatPhotoEmpty(), participants_count=3,
        date=None, version=1) if group else None
    message = SimpleNamespace(id=number, chat_id=-chat if group else chat, sender_id=4242, message='Synthetic text',
        entities=[], out=False, peer_id=types.PeerChat(chat) if group else types.PeerUser(chat), chat=entity,
        date=datetime(2026, 11, 8, 9, number % 60, tzinfo=timezone.utc), edit_date=None, file=None, document=None,
        photo=None, media=None)
    return archive.ingest(telegram_message('tg-types', message))['message']


def whatsapp_in(archive, chat, number):
    """An incoming WhatsApp bridge event through whatsapp_message: the adapter emits no chat type."""
    from messaging_service.adapters import whatsapp_message
    event = {'messageId': 'false_%s_SYNTHETIC%d' % (chat, number), 'chatId': chat, 'senderId': '4242@c.us',
        'timestamp': 1794128400 + number, 'body': 'Synthetic text', 'hasMedia': False}
    return archive.ingest(whatsapp_message('wa-types', event))['message']


def typed_key(store, principal='types-reader'):
    return dynamic_key(store, principal, accounts=('tg-types', 'wa-types'))


def message_events(store, token, sid):
    """Message references only: a send.failed item names a send, not a message."""
    return [item['message'] for item in store.get_subscription_events(token, sid)['items'] if 'message' in item]


# spec: ANYM-825
def test_the_heartbeat_identity_fits_the_stored_version_constraint():
    import re
    from messaging_service import __version__
    from messaging_service.heartbeats import identity
    # Migration 050: version ~ '^[0-9A-Za-z.+-]{1,64}$'; a refused write would silence every heartbeat.
    assert re.fullmatch(r'[0-9A-Za-z.+-]{1,64}', identity()) and identity().startswith(__version__ + '+')


# spec: ANYM-825
def test_chat_types_validate_their_form_and_wait_for_the_source_filters_switch(seeded):
    from messaging_service import __version__
    from messaging_service.heartbeats import identity
    from messaging_service.store import RUNTIME_SCHEMA
    store, _, _ = seeded
    token = dynamic_key(store)
    private = {**ALL_VISIBLE, 'chat_types': ['private']}
    with pytest.raises(APIError, match='dynamic_sources_disabled'):
        store.create_subscription(token, sources=private)
    store.set_dynamic_subscriptions(True)
    with store.pool.connection() as c:
        saved = c.execute('SELECT * FROM component_heartbeats').fetchall()
    current = identity()
    def beat(matcher_version):
        with store.pool.connection() as c:
            for component, version in (('api', current), ('matcher', matcher_version), ('dispatcher', current)):
                c.execute('''INSERT INTO component_heartbeats(component,version,schema_version,beat_at)
                    VALUES(%s,%s,%s,clock_timestamp()) ON CONFLICT(component) DO UPDATE SET version=EXCLUDED.version,
                    schema_version=EXCLUDED.schema_version,beat_at=EXCLUDED.beat_at''', (component, version, RUNTIME_SCHEMA))
    try:
        with pytest.raises(APIError, match='source_filters_disabled'):
            store.create_subscription(token, sources=private)
        # Without chat_types nothing changes: no second switch, the stored selector has no new key.
        plain = store.create_subscription(token, sources=ALL_VISIBLE)
        assert plain['sources'] == {'all_visible_chats': True, 'account_ids': None, 'include_channels': False,
            'include_bots': False}
        # Mixed versions: the deployed matcher reports the same package version and no build features; it would
        # store group matches for such a definition. The dynamic and delivery switches still accept it.
        assert current == __version__ + '+source-filters.source-exclusions'
        beat(__version__)
        assert store.set_source_filters(True, verify_components=True) == {'error': 'component_feature_missing',
            'component': 'matcher', 'version': __version__, 'expected': current}
        assert store.set_dynamic_subscriptions(True, verify_components=True) == {'dynamic_subscriptions': 'enabled'}
        with pytest.raises(APIError, match='source_filters_disabled'):
            store.create_subscription(token, sources=private)
        beat(current)
        assert store.set_source_filters(True, verify_components=True) == {'source_filters': 'enabled'}
        for chat_types, extra in [([], {}), (['private'] * 2, {}), (['private', 'group', 'channel', 'unknown', 'bot'], {}),
                (['bot'], {}), ('private', {}), ([1], {}), (['private'], {'include_channels': True})]:
            with pytest.raises(APIError, match='invalid_filter'):
                store.create_subscription(token, sources={**ALL_VISIBLE, 'chat_types': chat_types, **extra})
        # Explicit chats refuse it with every other dynamic source option.
        with pytest.raises(APIError, match='invalid_filter'):
            store.create_subscription(token, ['telegram-demo-chat'], sources=private)
        created = store.create_subscription(token, sources={**ALL_VISIBLE, 'chat_types': ['unknown', 'private']})
        assert created['sources']['chat_types'] == ['private', 'unknown']
        channels = store.create_subscription(token, sources={**ALL_VISIBLE, 'include_channels': True,
            'chat_types': ['channel', 'private']})
        assert store.get_subscription(token, channels['subscription_id'])['sources']['chat_types'] == ['channel', 'private']
        only_channels = store.create_subscription(token, sources={**ALL_VISIBLE, 'chat_types': ['channel']})
        channel = emit(store, chat_message(store, 'telegram-demo-channel', 'demo-tg-channel', chat_type='channel'))
        untyped = emit(store)
        match_subscriptions(store)
        # Naming channel lets channels in without include_channels; the seeded chat is untyped (unknown).
        assert event_ids(store, token, only_channels['subscription_id']) == [channel]
        assert event_ids(store, token, created['subscription_id']) == [untyped]
        assert event_ids(store, token, channels['subscription_id']) == [channel]
    finally:
        store.set_source_filters(False)
        store.set_dynamic_subscriptions(False)
        with store.pool.connection() as c:
            c.execute('DELETE FROM component_heartbeats')
            for row in saved:
                c.execute('INSERT INTO component_heartbeats(component,version,schema_version,beat_at) VALUES(%s,%s,%s,%s)',
                    (row['component'], row['version'], row['schema_version'], row['beat_at']))


# spec: ANYM-825
def test_chat_types_leave_out_groups_before_the_match_on_both_platforms(seeded, tmp_path, monkeypatch):
    from messaging_service import subscriptions
    store, _, _ = seeded
    archive = typed_archive(store)
    left = []
    monkeypatch.setattr(subscriptions, 'left_out', lambda row, event, reason: left.append(
        (str(row['id']), event['id'], reason)))
    # One stored match per definition: a group event that took the budget would block the private one.
    monkeypatch.setenv('SUBSCRIPTION_MAX_MATCHES', '1')
    with source_filters(store):
        token = typed_key(store)
        private = store.create_subscription(token, sources={**ALL_VISIBLE, 'chat_types': ['private']})['subscription_id']
        untyped = store.create_subscription(token, sources={**ALL_VISIBLE, 'chat_types': ['private', 'unknown']},
            kinds=['message.created'])['subscription_id']
        bind(store, token, private, tmp_path, 'types-reader')
        tg_group = telegram_in(archive, 500, 1, group=True)
        wa_group = whatsapp_in(archive, '120363000000000001@g.us', 2)
        wa_private = whatsapp_in(archive, '4243@c.us', 3)
        with store.pool.connection() as c:
            head = c.execute('SELECT value FROM feed_counter WHERE id=1').fetchone()['value']
            typed = {row['provider_chat']: row['chat_type'] for row in c.execute(
                "SELECT provider_chat,chat_type FROM chats WHERE account IN ('tg-types','wa-types')").fetchall()}
        # Telegram types the group at its first message; WhatsApp chats are stored untyped (unknown).
        assert typed == {'-500': 'group', '120363000000000001@g.us': None, '4243@c.us': None}
        match_subscriptions(store)
        view = store.get_subscription(token, private)
        assert view['matched_event_count'] == 0 and view['matched_through'] == head and view['blocked_reason'] is None
        assert store.get_subscription(token, private)['delivery']['hint_counts'] == {}
        assert message_events(store, token, untyped) == [wa_group]
        assert store.get_subscription(token, untyped)['blocked_reason'] == 'match_budget'
        tg_private = telegram_in(archive, 4244, 4)
        match_subscriptions(store)
        view = store.get_subscription(token, private)
        assert message_events(store, token, private) == [tg_private] and view['matched_event_count'] == 1
        assert view['blocked_reason'] is None and view['delivery']['hint_counts'] == {'pending': 1}
        with store.pool.connection() as c:
            events = {row['message']: row['id'] for row in c.execute(
                'SELECT id,message FROM events WHERE message=ANY(%s)', ([tg_group, wa_group, wa_private],)).fetchall()}
        # The hook gets the reason for every left-out event, once per definition and pass.
        assert sorted((sid, event) for sid, event, reason in left if sid == private and reason == 'chat_type') == sorted(
            (private, events[message]) for message in (tg_group, wa_group, wa_private))
        assert [(sid, event, reason) for sid, event, reason in left if sid == untyped] == [
            (untyped, events[tg_group], 'chat_type')]


# spec: ANYM-825
def test_whatsapp_chats_count_as_unknown_and_failure_events_keep_their_own_rule(seeded, monkeypatch):
    from messaging_service import subscriptions
    from messaging_service.subscriptions import journal_send_failures
    store, _, _ = seeded
    archive = typed_archive(store)
    left = []
    monkeypatch.setattr(subscriptions, 'left_out', lambda row, event, reason: left.append(reason))
    with source_filters(store):
        token = typed_key(store)
        private = store.create_subscription(token, sources={**ALL_VISIBLE, 'chat_types': ['private']},
            kinds=['message.created', 'send.failed'])['subscription_id']
        untyped = store.create_subscription(token, sources={**ALL_VISIBLE, 'chat_types': ['private', 'unknown']})[
            'subscription_id']
        wa_private, wa_group = whatsapp_in(archive, '4245@c.us', 5), whatsapp_in(archive, '120363000000000002@g.us', 6)
        tg_group = telegram_in(archive, 501, 7, group=True)
        with store.authorized(token) as (_, p):
            owner = p
        with store.pool.connection() as c:
            group_chat = c.execute("SELECT id FROM chats WHERE account='tg-types' AND provider_chat='-501'").fetchone()['id']
            journal_send_failures(c, [{'status': 'failed', 'account': 'tg-types', 'chat': group_chat,
                'id': 'send_synthetic_group', 'error_code': 'send_not_started', 'principal': owner['id'],
                'key_id': owner['key_id']}])
        match_subscriptions(store)
        assert message_events(store, token, untyped) == [wa_private, wa_group]
        items = store.get_subscription_events(token, private)['items']
        # A send.failed in a group chat matches by its own audience rule; the group messages do not.
        assert [(item['kind'], item.get('send_id')) for item in items] == [('send.failed', 'send_synthetic_group')]
        assert tg_group not in message_events(store, token, private)
        assert left.count('chat_type') == 4


# spec: ANYM-825
def test_a_private_chat_first_seen_after_creation_matches_on_both_platforms(seeded):
    store, _, _ = seeded
    archive = typed_archive(store)
    with source_filters(store):
        token = typed_key(store)
        private = store.create_subscription(token, sources={**ALL_VISIBLE, 'chat_types': ['private']})['subscription_id']
        untyped = store.create_subscription(token, sources={**ALL_VISIBLE, 'chat_types': ['private', 'unknown']})[
            'subscription_id']
        before = store.get_subscription(token, private)['version']
        # Neither chat existed at creation.
        tg_new, wa_new = telegram_in(archive, 4246, 8), whatsapp_in(archive, '4247@c.us', 9)
        match_subscriptions(store)
        assert message_events(store, token, private) == [tg_new]
        assert message_events(store, token, untyped) == [tg_new, wa_new]
        assert store.get_subscription(token, private)['version'] == before


def utc_day(store, back=0):
    with store.pool.connection() as c:
        return c.execute("SELECT ((clock_timestamp() AT TIME ZONE 'UTC')::date-%s)::text AS d", (back,)).fetchone()['d']


def activity(store, token, sid):
    """Today's per-platform counts of the definition (these tests write no other day)."""
    days = store.get_subscription(token, sid)['activity']['days']
    assert [day['day'] for day in days] in ([], [utc_day(store)])
    return days[0]['platforms'] if days else {}


def balanced(platforms):
    """scanned = matched + every left_out_* for each platform."""
    return all(counts.get('scanned', 0) == counts.get('matched', 0) + sum(
        n for metric, n in counts.items() if metric.startswith('left_out_')) for counts in platforms.values())


# spec: ANYM-824
def test_activity_counts_each_selected_event_once_by_platform_and_first_rejecting_rule(seeded):
    from messaging_service.subscriptions import journal_send_failures
    store, _, _ = seeded
    with source_filters(store):
        token = dynamic_key(store, 'activity-reader')
        # Archived before the definitions (a named sender must be known in its chat); journaled after them.
        chat_message(store, 'telegram-activity-channel', 'activity-channel', chat_type='channel')
        chat_message(store, 'telegram-activity-bots', 'activity-bot', metadata={'sender_is_bot': True})
        chat_message(store, 'telegram-demo-chat', 'activity-out')
        with store.pool.connection() as c:
            c.execute("UPDATE messages SET direction='outgoing' WHERE id='activity-out'")
        everything = store.create_subscription(token, sources=ALL_VISIBLE,
            kinds=['message.created', 'send.failed'])['subscription_id']
        private = store.create_subscription(token, sources={**ALL_VISIBLE, 'chat_types': ['private']})['subscription_id']
        # An account restriction: the WhatsApp account's events are outside the definition, counted nowhere.
        telegram = store.create_subscription(token, sources={**ALL_VISIBLE, 'account_ids': ['telegram-demo']})[
            'subscription_id']
        senders = store.create_subscription(token, ['telegram-demo-chat'], sender_ids=['demo-person-9'])['subscription_id']
        mentions = store.create_subscription(token, ['telegram-demo-chat'], wake_rules=['owner_mention'])['subscription_id']
        emit(store)
        emit(store, 'demo-wa-1')
        emit(store, kind='message.edited')
        emit(store, origin='backfill')
        emit(store, 'activity-channel')
        emit(store, 'activity-bot')
        emit(store, 'activity-out')
        # An account the key cannot see: counted nowhere.
        emit(store, 'demo-private-1')
        with store.authorized(token) as (_, p):
            owner = p
        with store.pool.connection() as c:
            journal_send_failures(c, [{'status': 'failed', 'account': 'telegram-demo', 'chat': 'telegram-demo-chat',
                'id': 'send_synthetic_activity', 'error_code': 'send_not_started', 'principal': owner['id'],
                'key_id': owner['key_id']}])
        assert match_subscriptions(store)['activity_failed'] == 0
        counts = {sid: activity(store, token, sid) for sid in (everything, private, telegram, senders, mentions)}
        assert counts[everything] == {
            'telegram': {'scanned': 6, 'matched': 1, 'left_out_kind': 1, 'left_out_origin': 1, 'left_out_channel': 1,
                'left_out_bot': 1, 'left_out_direction': 1, 'matched_failure': 1},
            'whatsapp': {'scanned': 1, 'matched': 1}}
        # chat_types (ANYM-825) leaves every untyped chat and the channel out; the kind and origin rules come first.
        assert counts[private] == {
            'telegram': {'scanned': 6, 'left_out_kind': 1, 'left_out_origin': 1, 'left_out_chat_type': 4},
            'whatsapp': {'scanned': 1, 'left_out_chat_type': 1}}
        assert counts[telegram] == {'telegram': {'scanned': 6, 'matched': 1, 'left_out_kind': 1, 'left_out_origin': 1,
            'left_out_channel': 1, 'left_out_bot': 1, 'left_out_direction': 1}}
        # Explicit sources scan only their own chat; direction comes before the sender rule.
        assert counts[senders] == {'telegram': {'scanned': 4, 'left_out_kind': 1, 'left_out_origin': 1,
            'left_out_direction': 1, 'left_out_sender': 1}}
        assert counts[mentions] == {'telegram': {'scanned': 4, 'left_out_kind': 1, 'left_out_origin': 1,
            'left_out_direction': 1, 'left_out_wake_rule': 1}}
        assert all(balanced(platforms) for platforms in counts.values())
        with store.pool.connection() as c:
            columns = [row['column_name'] for row in c.execute('''SELECT column_name FROM information_schema.columns
                WHERE table_name='subscription_activity' ORDER BY ordinal_position''').fetchall()]
            platforms = {row['platform'] for row in c.execute('SELECT DISTINCT platform FROM subscription_activity').fetchall()}
        # Numbers, platform names and days only: no chat, sender, message or account ID and no text.
        assert columns == ['subscription', 'day', 'platform', 'scope_version', 'metric', 'n']
        assert platforms == {'telegram', 'whatsapp'}


# spec: ANYM-824
def test_activity_commits_with_the_checkpoint_so_a_rolled_back_pass_counts_nothing(seeded):
    from messaging_service.subscriptions import journal_send_failures
    store, _, _ = seeded
    token = key(store, 'activity-explicit')
    sid = create(store, token, kinds=['message.created', 'send.failed'])['subscription_id']
    emit(store)
    with store.authorized(token) as (_, p):
        owner = p
    with store.pool.connection() as c:
        journal_send_failures(c, [{'status': 'failed', 'account': 'telegram-demo', 'chat': 'telegram-demo-chat',
            'id': 'send_synthetic_rollback', 'error_code': 'send_not_started', 'principal': owner['id'],
            'key_id': owner['key_id']}])
        before = c.execute('SELECT matched_through FROM subscriptions WHERE id=%s', (sid,)).fetchone()['matched_through']
        # Fires at COMMIT, after the counts, the references and the checkpoint were all written.
        c.execute("""CREATE FUNCTION activity_test_failure() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic commit failure'; END $$""")
        c.execute('''CREATE CONSTRAINT TRIGGER activity_test_failure AFTER INSERT ON subscription_activity
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION activity_test_failure()''')
    try:
        with pytest.raises(Exception, match='synthetic commit failure'):
            match_subscriptions(store)
        with store.pool.connection() as c:
            assert c.execute('SELECT count(*) AS n FROM subscription_activity WHERE subscription=%s',
                (sid,)).fetchone()['n'] == 0
            assert c.execute('SELECT count(*) AS n FROM subscription_matches WHERE subscription=%s',
                (sid,)).fetchone()['n'] == 0
            assert c.execute('SELECT matched_through FROM subscriptions WHERE id=%s',
                (sid,)).fetchone()['matched_through'] == before
    finally:
        with store.pool.connection() as c:
            c.execute('DROP TRIGGER activity_test_failure ON subscription_activity')
            c.execute('DROP FUNCTION activity_test_failure()')
    match_subscriptions(store)
    # One message match and one failure match: the failure is never scanned.
    assert activity(store, token, sid) == {'telegram': {'scanned': 1, 'matched': 1, 'matched_failure': 1}}
    match_subscriptions(store)
    assert activity(store, token, sid) == {'telegram': {'scanned': 1, 'matched': 1, 'matched_failure': 1}}


# spec: ANYM-824
def test_activity_shows_seven_days_of_the_current_scope_to_the_owning_key_only(seeded):
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token, other = dynamic_key(store, 'activity-scope'), dynamic_key(store, 'activity-other')
        sid = store.create_subscription(token, sources=ALL_VISIBLE)['subscription_id']
        theirs = store.create_subscription(other, sources=ALL_VISIBLE)['subscription_id']
        emit(store)
        match_subscriptions(store)
        assert activity(store, token, sid) == activity(store, other, theirs) == {'telegram': {'scanned': 1, 'matched': 1}}
        with pytest.raises(APIError, match='not_found'):
            store.get_subscription(other, sid)
        assert all('activity' not in item for item in store.list_subscriptions(token)['items'])
        with store.authorized(token) as (_, p):
            version = p['scope_version']
        with store.pool.connection() as c:
            for back, metric, n in ((7, 'scanned', 7), (6, 'scanned', 6), (1, 'matched', 0)):
                c.execute('''INSERT INTO subscription_activity(subscription,day,platform,scope_version,metric,n)
                    VALUES(%s,(clock_timestamp() AT TIME ZONE 'UTC')::date-%s,'telegram',%s,%s,%s)''',
                    (sid, back, version, metric, n))
        # The last 7 UTC days, oldest first; a zero metric (and a day of only zeros) is omitted.
        assert store.get_subscription(token, sid)['activity'] == {'days': [
            {'day': utc_day(store, 6), 'platforms': {'telegram': {'scanned': 6}}},
            {'day': utc_day(store), 'platforms': {'telegram': {'scanned': 1, 'matched': 1}}}]}
        store.set_access('activity-scope', chats=['telegram-demo-chat'])
        assert store.get_subscription(token, sid)['activity'] == {'days': []}
        emit(store)
        match_subscriptions(store)
        assert activity(store, token, sid) == {'telegram': {'scanned': 1, 'matched': 1}}
        # Re-expansion resurrects neither the first scope's counts nor the narrowed scope's.
        store.set_access('activity-scope', chats=None)
        assert store.get_subscription(token, sid)['activity'] == {'days': []}
        emit(store)
        match_subscriptions(store)
        assert activity(store, token, sid) == {'telegram': {'scanned': 1, 'matched': 1}}
        assert activity(store, other, theirs) == {'telegram': {'scanned': 3, 'matched': 3}}
    finally:
        store.set_dynamic_subscriptions(False)


# spec: ANYM-824
def test_a_failed_activity_write_changes_no_match_checkpoint_or_hint_and_logs_once_per_pass(seeded, tmp_path, capsys):
    store, _, _ = seeded
    token = lag_key(store, 'activity-failure')
    sids = [bind(store, token, create(store, token)['subscription_id'], tmp_path, 'activity-failure') for _ in range(2)]
    emit(store)
    def acks():
        with store.pool.connection() as c:
            rows = c.execute('SELECT * FROM subscription_consumption WHERE subscription=ANY(%s::uuid[]) ORDER BY subscription',
                (sids,)).fetchall()
        return rows, [store.get_subscription(token, sid)['acked_through'] for sid in sids]
    acked = acks()
    with store.pool.connection() as c:
        c.execute("""CREATE FUNCTION activity_write_failure() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic activity failure'; END $$""")
        c.execute('''CREATE TRIGGER activity_write_failure BEFORE INSERT ON subscription_activity
            FOR EACH ROW EXECUTE FUNCTION activity_write_failure()''')
    capsys.readouterr()
    try:
        stats = match_subscriptions(store)
    finally:
        with store.pool.connection() as c:
            c.execute('DROP TRIGGER activity_write_failure ON subscription_activity')
            c.execute('DROP FUNCTION activity_write_failure()')
    logged = [line for line in capsys.readouterr().out.splitlines() if 'subscription_activity_failed' in line]
    # The error code only, once for the whole pass.
    assert logged == ['{"error": "subscription_activity_failed", "code": "P0001"}']
    assert stats['matched'] == 2 and stats['activity_failed'] == 2
    with store.pool.connection() as c:
        head = c.execute('SELECT value FROM feed_counter WHERE id=1').fetchone()['value']
    for sid in sids:
        view = store.get_subscription(token, sid)
        assert view['matched_event_count'] == 1 and view['matched_through'] == head and view['blocked_reason'] is None
        assert view['delivery']['hint_counts'] == {'pending': 1} and view['activity'] == {'days': []}
    assert acks() == acked
    # The next pass counts its own events only: a lost write is not replayed.
    emit(store)
    assert match_subscriptions(store)['activity_failed'] == 0
    assert [activity(store, token, sid) for sid in sids] == [{'telegram': {'scanned': 1, 'matched': 1}}] * 2


# spec: ANYM-824
def test_a_database_before_schema_70_still_matches_and_answers_get_subscription_without_activity(database, tmp_path):
    from messaging_service.subscriptions import retire_subscriptions
    from test_subscription_retention import restore_database
    with restore_database(database, tmp_path, through=69) as old:
        token = key(old, 'activity-old-schema')
        sid = create(old, token)['subscription_id']
        emit(old)
        stats = match_subscriptions(old)
        assert stats['matched'] == 1 and stats['activity_failed'] == 1
        view = old.get_subscription(token, sid)
        assert 'activity' not in view and view['matched_event_count'] == 1
        assert retire_subscriptions(old)['retire_failed'] == 0


# spec: ANYM-824
def test_migration_70_runs_twice_and_keeps_the_counts_and_every_grant(seeded):
    store, _, _ = seeded
    token = key(store, 'activity-migration')
    sid = create(store, token)['subscription_id']
    emit(store)
    match_subscriptions(store)
    with store.pool.connection() as c:
        c.execute('DELETE FROM archive_migrations WHERE version IN (70,71,72,73,74,75,76,77,78,79,80,81)')
        c.execute("UPDATE probe_metadata SET value='69' WHERE name='schema_version'")
        grants = c.execute("SELECT oid,relacl::text FROM pg_class WHERE relnamespace='public'::regnamespace ORDER BY oid").fetchall()
    store.migrate()
    store.migrate()
    with store.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '81'
        assert c.execute('SELECT count(*) AS n FROM archive_migrations WHERE version=70').fetchone()['n'] == 1
        assert c.execute("SELECT oid,relacl::text FROM pg_class WHERE relnamespace='public'::regnamespace ORDER BY oid").fetchall() == grants
    assert activity(store, token, sid) == {'telegram': {'scanned': 1, 'matched': 1}}


def failure_event(store, kind, token):
    """A synthetic send.failed of the key (through the journal writer) or source.health row of its account."""
    from messaging_service.subscriptions import journal_send_failures
    with store.authorized(token) as (_, p):
        owner = p
    with store.pool.connection() as c:
        if kind == 'send.failed':
            journal_send_failures(c, [{'status': 'failed', 'account': 'telegram-demo', 'chat': 'telegram-demo-chat',
                'id': 'send_synthetic_budget', 'error_code': 'send_not_started', 'principal': owner['id'],
                'key_id': owner['key_id']}])
        else:
            value = c.execute('UPDATE feed_counter SET value=value+1 WHERE id=1 RETURNING value').fetchone()['value']
            c.execute('''INSERT INTO events(id,account,message,kind,revision,origin,snapshot,event_key)
                VALUES(%s,'telegram-demo',NULL,'source.health',1,'live',%s,%s)''',
                (value, Jsonb({'account': 'telegram-demo', 'status': 'degraded'}), 'source.health:synthetic-%d' % value))


# spec: ANYM-824
@pytest.mark.parametrize('kind', ['send.failed', 'source.health'])
def test_a_budget_stop_counts_once_and_a_failure_event_at_the_budget_never_counts_as_blocked(seeded, monkeypatch, kind):
    store, _, _ = seeded
    token = key(store, 'activity-budget')
    sid = create(store, token, kinds=['message.created', kind])['subscription_id']
    monkeypatch.setenv('SUBSCRIPTION_MAX_MATCHES', '2')
    for _ in range(3):
        emit(store)
    assert match_subscriptions(store)['blocked'] == 1
    # The event that hit the budget is not scanned yet.
    assert activity(store, token, sid) == {'telegram': {'scanned': 2, 'matched': 2, 'match_budget_blocked': 1}}
    monkeypatch.setenv('SUBSCRIPTION_MAX_MATCHES', '3')
    assert match_subscriptions(store)['blocked'] == 0
    assert activity(store, token, sid) == {'telegram': {'scanned': 3, 'matched': 3, 'match_budget_blocked': 1}}
    failure_event(store, kind, token)
    assert match_subscriptions(store)['blocked'] == 1
    assert activity(store, token, sid) == {'telegram': {'scanned': 3, 'matched': 3, 'match_budget_blocked': 1}}
    monkeypatch.setenv('SUBSCRIPTION_MAX_MATCHES', '4')
    match_subscriptions(store)
    assert activity(store, token, sid) == {'telegram': {'scanned': 3, 'matched': 3, 'match_budget_blocked': 1,
        'matched_failure': 1}}


# spec: ANYM-824
def test_one_upsert_puts_all_its_counts_on_one_utc_day_across_midnight(seeded):
    from messaging_service.subscriptions import record_activity
    store, _, _ = seeded
    sid = create(store, key(store, 'activity-midnight'))['subscription_id']
    # Synthetic clocks on the search path ahead of pg_catalog: the first call is before midnight, the rest after.
    with store.pool.connection() as c:
        c.execute('CREATE SCHEMA activity_clock')
        c.execute('CREATE SEQUENCE activity_clock.calls')
        for name in ('clock_timestamp', 'statement_timestamp', 'now'):
            c.execute(sql_function(name))
    try:
        with store.pool.connection() as c:
            c.execute('SET LOCAL search_path=activity_clock,public,pg_catalog')
            record_activity(c, sid, 1, {('telegram', 'scanned'): 2, ('telegram', 'matched'): 1,
                ('whatsapp', 'scanned'): 1, ('whatsapp', 'left_out_kind'): 1})
        with store.pool.connection() as c:
            days = {row['day'].isoformat() for row in c.execute(
                'SELECT day FROM subscription_activity WHERE subscription=%s', (sid,)).fetchall()}
        assert days == {'2026-11-08'}
    finally:
        with store.pool.connection() as c:
            c.execute('DROP SCHEMA activity_clock CASCADE')


def sql_function(name):
    return ('CREATE FUNCTION activity_clock.' + name + """() RETURNS timestamptz LANGUAGE sql VOLATILE AS $$
        SELECT CASE WHEN nextval('activity_clock.calls')=1 THEN timestamptz '2026-11-08 23:59:59.999+00'
        ELSE timestamptz '2026-11-09 00:00:00.001+00' END $$""")


# spec: ANYM-824
def test_a_pass_writes_at_most_one_activity_upsert_per_definition(seeded, monkeypatch):
    import psycopg
    store, _, _ = seeded
    token = key(store, 'activity-cost', accounts=['telegram-demo', 'whatsapp-demo-1'])
    for _ in range(3):
        create(store, token)
    # A definition whose chat had no event this pass writes nothing.
    store.create_subscription(token, ['whatsapp-demo-1-chat'])
    for _ in range(5):
        emit(store)
    statements = []
    execute = psycopg.Connection.execute
    def counted(self, query, *args, **kwargs):
        statements.append(str(query))
        return execute(self, query, *args, **kwargs)
    monkeypatch.setattr(psycopg.Connection, 'execute', counted)
    # The worker's defaults (subscription_worker.py: 16 subscriptions, 200 events per pass).
    assert match_subscriptions(store, batch_size=200, max_subscriptions=16)['matched'] == 15
    assert sum('INSERT INTO subscription_activity' in statement for statement in statements) == 3
    statements.clear()
    assert match_subscriptions(store, batch_size=200, max_subscriptions=16)['matched'] == 0
    assert sum('INSERT INTO subscription_activity' in statement for statement in statements) == 0


# spec: ANYM-824
def test_the_worker_role_writes_and_prunes_activity_with_its_named_grants(seeded):
    from contextlib import contextmanager
    from copy import copy
    from pathlib import Path
    from uuid import uuid4
    import psycopg
    from psycopg import sql
    from messaging_service.subscriptions import retire_subscriptions
    store, _, _ = seeded
    role = 'test_subscription_activity_' + uuid4().hex
    source = Path(__file__).resolve().parents[1] / 'services/messaging/messaging_service/subscription_role.sql'

    class RestrictedPool:
        @contextmanager
        def connection(self):
            with store.pool.connection() as connection:
                connection.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                yield connection

    worker = copy(store)
    worker.pool = RestrictedPool()
    token = key(store, 'activity-role')
    sid = create(store, token)['subscription_id']
    emit(store)
    created = False
    try:
        with store.pool.connection() as c:
            c.execute(source.read_text().replace('messaging_subscriptions', role))
        created = True
        assert match_subscriptions(worker)['activity_failed'] == 0
        assert activity(store, token, sid) == {'telegram': {'scanned': 1, 'matched': 1}}
        store.set_subscription_retention(True)
        with store.pool.connection() as c:
            c.execute('''INSERT INTO subscription_activity(subscription,day,platform,scope_version,metric,n)
                VALUES(%s,(clock_timestamp() AT TIME ZONE 'UTC')::date-36,'telegram',1,'scanned',1)''', (sid,))
        stats = retire_subscriptions(worker)
        assert stats['activity_deleted'] == 1 and stats['retire_failed'] == 0
        with store.pool.connection() as c:
            assert c.execute('SELECT count(*) AS n FROM subscription_activity WHERE day<%s::date',
                (utc_day(store),)).fetchone()['n'] == 0
        # The table grant adds no right on the definitions themselves.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with worker.pool.connection() as c:
                c.execute('DELETE FROM subscriptions WHERE id=%s', (sid,))
    finally:
        with store.pool.connection() as c:
            c.execute("DELETE FROM probe_metadata WHERE name='subscription_retention'")
            if created:
                c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


SHAPES = ('plain', 'mention', 'reply', 'photo', 'document', 'voice', 'forwarded')
WAKE_CASES = {
    # rule: the message shapes that wake it; media kinds and forwarding never change the result.
    'incoming_message': list(SHAPES),
    'owner_mention': ['mention'],
    'reply_to_owner': ['reply'],
    'reply_to_own_send': list(SHAPES),
}


def every_arrival_replies_to_an_own_send(monkeypatch):
    """reply_to_own_send: every arrival carries the evidence, so only chat_types can leave a group out."""
    from messaging_service import subscriptions
    monkeypatch.setattr(subscriptions, '_own_send_replies', lambda c, row, events, visible: {e['id'] for e in events})


# spec: ANYM-825
@pytest.mark.parametrize('rule', sorted(WAKE_CASES))
def test_chat_types_with_each_wake_rule_match_telegram_private_messages_only(seeded, monkeypatch, rule):
    store, _, _ = seeded
    every_arrival_replies_to_an_own_send(monkeypatch)
    metadata = {'plain': {}, 'mention': {'mentions_owner': True}, 'reply': {'reply_to_owner': True},
        'photo': {'message_kind': 'photo'}, 'document': {'message_kind': 'document'},
        'voice': {'message_kind': 'voice'}, 'forwarded': {'message_kind': 'photo', 'forwarded': True}}
    with source_filters(store):
        token = dynamic_key(store)
        sid = store.create_subscription(token, sources={**ALL_VISIBLE, 'chat_types': ['private']},
            wake_rules=[rule])['subscription_id']
        expected = {}
        for kind in ('private', 'group'):
            for shape in SHAPES:
                message = chat_message(store, 'telegram-demo-%s-chat' % kind, 'demo-tg-%s-%s' % (kind, shape),
                    chat_type=kind, metadata=metadata[shape])
                expected[(kind, shape)] = emit(store, message)
        # Channels and bot senders keep their defaults, also with every wake evidence present.
        emit(store, chat_message(store, 'telegram-demo-channel', 'demo-tg-channel-1', chat_type='channel',
            metadata={'mentions_owner': True, 'reply_to_owner': True}))
        emit(store, chat_message(store, 'telegram-demo-private-chat', 'demo-tg-bot-1', chat_type='private',
            metadata={'mentions_owner': True, 'reply_to_owner': True, 'sender_is_bot': True}))
        match_subscriptions(store)
        assert event_ids(store, token, sid) == [expected[('private', shape)] for shape in WAKE_CASES[rule]]


# spec: ANYM-825
@pytest.mark.parametrize('rule', sorted(WAKE_CASES))
def test_whatsapp_wake_rules_with_chat_types_follow_the_unknown_type(seeded, monkeypatch, rule):
    from messaging_service.adapters import whatsapp_message
    store, _, _ = seeded
    archive = typed_archive(store)
    every_arrival_replies_to_an_own_send(monkeypatch)
    fields = {'plain': {}, 'mention': {'mentionsOwner': True}, 'reply': {'replyToOwner': True},
        'photo': {'mediaKind': 'photo'}, 'document': {'mediaKind': 'document'}, 'voice': {'mediaKind': 'voice'},
        'forwarded': {'mediaKind': 'photo', 'forwarded': True}}
    with source_filters(store):
        token = typed_key(store)
        private = store.create_subscription(token, sources={**ALL_VISIBLE, 'chat_types': ['private']},
            wake_rules=[rule])['subscription_id']
        untyped = store.create_subscription(token, sources={**ALL_VISIBLE, 'chat_types': ['private', 'unknown']},
            wake_rules=[rule])['subscription_id']
        expected = {}
        for number, (chat, shape) in enumerate([(chat, shape) for chat in ('4248@c.us', '120363000000000003@g.us')
                for shape in SHAPES]):
            event = {'messageId': 'false_%s_SYNTHETIC%d' % (chat, number), 'chatId': chat, 'senderId': '4242@c.us',
                'timestamp': 1794128500 + number, 'body': 'Synthetic text', 'hasMedia': False, **fields[shape]}
            expected[(chat, shape)] = archive.ingest(whatsapp_message('wa-types', event))['message']
        match_subscriptions(store)
        # Every WhatsApp chat is unknown: [private] takes none, [private, unknown] groups and private chats alike.
        assert message_events(store, token, private) == []
        assert message_events(store, token, untyped) == [expected[(chat, shape)]
            for chat in ('4248@c.us', '120363000000000003@g.us') for shape in WAKE_CASES[rule]]


# spec: ANYM-825
def test_disabling_source_filters_masks_chat_type_definitions_and_cancels_their_hints(seeded, tmp_path):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    with source_filters(store):
        token = dynamic_key(store)
        sid = store.create_subscription(token, sources={**ALL_VISIBLE, 'chat_types': ['private', 'unknown']},
            label='Synthetic filtered')['subscription_id']
        plain = store.create_subscription(token, sources=ALL_VISIBLE)['subscription_id']
        explicit = create(store, token)['subscription_id']
        bind(store, token, sid, tmp_path, 'dynamic-reader')
        emit(store)
        group = emit(store, chat_message(store, 'telegram-demo-group', 'demo-tg-group', chat_type='group'))
        match_subscriptions(store)
        # What an older matcher would have stored for the definition: the group event, which readers do not filter.
        with store.pool.connection() as c:
            c.execute('''INSERT INTO subscription_matches(subscription,event,account,chat,own_send_reply)
                VALUES(%s,%s,'telegram-demo','telegram-demo-group',false)''', (sid, group))
        assert store.get_subscription(token, sid)['delivery']['hint_counts'] == {'pending': 1}
        result = store.set_source_filters(False)
        assert result['source_filters'] == 'disabled' and result['access_changed'] == [sid]
        assert len(result['cancelled_hints']) == 1
        view = store.get_subscription(token, sid)
        assert set(view) == {'subscription_id', 'state', 'version', 'recreate_required', 'scope_masked'}
        assert view['state'] == 'access_changed' and 'Synthetic filtered' not in str(store.list_subscriptions(token))
        page = store.get_subscription_events(token, sid)
        assert page['items'] == [] and page['scope_masked']
        with pytest.raises(APIError, match='subscription_access_changed'):
            store.update_subscription(token, sid, view['version'], action='resume')
        with pytest.raises(APIError, match='source_filters_disabled'):
            store.create_subscription(token, sources={**ALL_VISIBLE, 'chat_types': ['private']})
        # Definitions without chat_types are untouched.
        assert store.get_subscription(token, plain)['state'] == 'active'
        assert store.get_subscription(token, explicit)['state'] == 'active'
        posts = []
        dispatch_hints(store, transport=lambda *a, **k: posts.append(a) or 204, resolver=lambda *a, **k: ['93.184.216.34'])
        assert posts == []


class source_exclusions:
    """ANYM-830: the dynamic and exclusion switches on for the block, off afterwards."""
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self.store.set_dynamic_subscriptions(True)
        self.store.set_source_exclusions(True)

    def __exit__(self, *exc):
        self.store.set_source_exclusions(False)
        self.store.set_dynamic_subscriptions(False)


DEMO_CHAT = {'account': 'telegram-demo', 'chat': 'telegram-demo-chat'}
NOISY_CHAT = {'account': 'telegram-demo', 'chat': 'telegram-demo-chat-830'}


def set_exclusions(store, token, sid, pairs, revision, version=None):
    version = store.get_subscription(token, sid)['version'] if version is None else version
    return store.update_subscription(token, sid, version, action='set_exclusions', exclude_chats=pairs,
        exclusions_revision=revision)


# spec: ANYM-830
def test_an_exclusion_list_is_bounded_by_forty_pairs_and_six_thousand_bytes():
    import json
    from messaging_service.subscriptions import _exclusion_pairs
    def size(pairs):
        return len(json.dumps(pairs, ensure_ascii=False).encode())
    # Maximum-length identifiers: the byte limit stops the list long before 40 pairs.
    longest = [{'account': 'a' * 62 + '%02d' % n, 'chat': 'c' * 256} for n in range(40)]
    fitting = max(n for n in range(41) if size(longest[:n]) <= 6000)
    assert 10 < fitting < 40
    assert len(_exclusion_pairs(longest[:fitting])) == fitting
    with pytest.raises(APIError, match='invalid_filter'):
        _exclusion_pairs(longest[:fitting + 1])
    # Many short ones: the 40-pair limit stops it.
    short = [{'account': 'a', 'chat': 'c%d' % n} for n in range(41)]
    assert size(short) < 6000 and len(_exclusion_pairs(short[:40])) == 40
    with pytest.raises(APIError, match='invalid_filter'):
        _exclusion_pairs(short)
    # Duplicates collapse, the order is canonical, and the selector stays inside its 14,000 bytes.
    assert _exclusion_pairs([NOISY_CHAT, DEMO_CHAT, NOISY_CHAT]) == [DEMO_CHAT, NOISY_CHAT]
    assert size({'sources': {**ALL_VISIBLE, 'exclude_chats': longest[:fitting]}}) < 14000
    for bad in ('telegram-demo-chat', [{'account': 'telegram-demo'}], [{**DEMO_CHAT, 'title': 'x'}],
            [{'account': '', 'chat': 'c'}], [{'account': 'a' * 65, 'chat': 'c'}], [{'account': 'a', 'chat': 'c' * 257}],
            [{'account': 'a', 'chat': 'c\x00'}], [{'account': 1, 'chat': 'c'}], ['telegram-demo-chat']):
        with pytest.raises(APIError, match='invalid_filter'):
            _exclusion_pairs(bad)


# spec: ANYM-830
def test_exclude_chats_wait_for_the_source_exclusions_switch_and_name_chats_the_key_can_see(seeded):
    from messaging_service import __version__
    from messaging_service.heartbeats import identity
    from messaging_service.store import RUNTIME_SCHEMA
    store, _, _ = seeded
    token = dynamic_key(store)
    narrow = dynamic_key(store, 'narrow-reader')
    chat_message(store, 'telegram-demo-chat-830', 'demo-tg-830')
    listed = {**ALL_VISIBLE, 'exclude_chats': [NOISY_CHAT]}
    store.set_dynamic_subscriptions(True)
    with store.pool.connection() as c:
        saved = c.execute('SELECT * FROM component_heartbeats').fetchall()
    current = identity()
    def beat(matcher_version):
        with store.pool.connection() as c:
            for component, version in (('api', current), ('matcher', matcher_version), ('dispatcher', current)):
                c.execute('''INSERT INTO component_heartbeats(component,version,schema_version,beat_at)
                    VALUES(%s,%s,%s,clock_timestamp()) ON CONFLICT(component) DO UPDATE SET version=EXCLUDED.version,
                    schema_version=EXCLUDED.schema_version,beat_at=EXCLUDED.beat_at''', (component, version, RUNTIME_SCHEMA))
    try:
        with pytest.raises(APIError, match='source_exclusions_disabled'):
            store.create_subscription(token, sources=listed)
        plain = store.create_subscription(token, sources=ALL_VISIBLE)
        assert 'exclude_chats' not in plain['sources'] and 'exclusions_revision' not in plain['sources']
        with pytest.raises(APIError, match='source_exclusions_disabled'):
            set_exclusions(store, token, plain['subscription_id'], [NOISY_CHAT], 0)
        # Mixed versions: the ANYM-825 build reports source-filters only; its matcher would match listed chats.
        assert current == __version__ + '+source-filters.source-exclusions'
        beat(__version__ + '+source-filters')
        assert store.set_source_exclusions(True, verify_components=True) == {'error': 'component_feature_missing',
            'component': 'matcher', 'version': __version__ + '+source-filters', 'expected': current}
        with pytest.raises(APIError, match='source_exclusions_disabled'):
            store.create_subscription(token, sources=listed)
        beat(current)
        assert store.set_source_exclusions(True, verify_components=True) == {'source_exclusions': 'enabled'}
        # An unknown pair, another account's chat and a chat outside the key's chat scope are not_found.
        store.set_access('narrow-reader', chats=['telegram-demo-chat'])
        for who, pair in ((token, {'account': 'telegram-demo', 'chat': 'no-such-chat'}),
                (token, {'account': 'whatsapp-demo-1', 'chat': 'telegram-demo-chat'}), (narrow, NOISY_CHAT)):
            with pytest.raises(APIError, match='not_found'):
                store.create_subscription(who, sources={**ALL_VISIBLE, 'exclude_chats': [pair]})
        created = store.create_subscription(token, sources=listed)
        assert created['sources']['exclude_chats'] == [NOISY_CHAT] and created['sources']['exclusions_revision'] == 1
        explicit = create(store, token)
        with pytest.raises(APIError, match='invalid_filter'):
            set_exclusions(store, token, explicit['subscription_id'], [NOISY_CHAT], 0)
        with pytest.raises(APIError, match='invalid_filter'):
            store.create_subscription(token, ['telegram-demo-chat'], sources=listed)
    finally:
        store.set_source_exclusions(False)
        store.set_dynamic_subscriptions(False)
        with store.pool.connection() as c:
            c.execute('DELETE FROM component_heartbeats')
            for row in saved:
                c.execute('INSERT INTO component_heartbeats(component,version,schema_version,beat_at) VALUES(%s,%s,%s,%s)',
                    (row['component'], row['version'], row['schema_version'], row['beat_at']))


# spec: ANYM-830
def test_excluded_chats_never_match_cost_no_budget_and_count_as_left_out_on_both_platforms(seeded, monkeypatch):
    from messaging_service import subscriptions
    store, _, _ = seeded
    archive = typed_archive(store)
    left = []
    monkeypatch.setattr(subscriptions, 'left_out', lambda row, event, reason: left.append(reason))
    # One stored match per definition: a listed chat's event that took the budget would block the kept one.
    monkeypatch.setenv('SUBSCRIPTION_MAX_MATCHES', '1')
    with source_exclusions(store):
        token = typed_key(store)
        telegram_in(archive, 830, 1)
        whatsapp_in(archive, '4830@c.us', 2)
        with store.pool.connection() as c:
            pairs = [{'account': row['account'], 'chat': row['id']} for row in c.execute(
                "SELECT id,account FROM chats WHERE account IN ('tg-types','wa-types')").fetchall()]
        assert len(pairs) == 2
        sid = store.create_subscription(token, sources={**ALL_VISIBLE, 'exclude_chats': pairs})['subscription_id']
        telegram_in(archive, 830, 3)
        whatsapp_in(archive, '4830@c.us', 4)
        match_subscriptions(store)
        view = store.get_subscription(token, sid)
        assert view['matched_event_count'] == 0 and view['blocked_reason'] is None
        assert view['delivery']['hint_counts'] == {}
        assert activity(store, token, sid) == {'telegram': {'scanned': 1, 'left_out_excluded_chat': 1},
            'whatsapp': {'scanned': 1, 'left_out_excluded_chat': 1}}
        assert left == ['excluded_chat', 'excluded_chat']
        kept = telegram_in(archive, 831, 5)
        match_subscriptions(store)
        assert message_events(store, token, sid) == [kept]
        assert store.get_subscription(token, sid)['blocked_reason'] is None


# spec: ANYM-830
def test_a_send_failure_in_an_excluded_chat_matches_but_changes_no_count_and_no_hint(seeded, tmp_path):
    store, _, _ = seeded
    with source_exclusions(store):
        token = dynamic_key(store)
        sid = store.create_subscription(token, sources={**ALL_VISIBLE, 'exclude_chats': [DEMO_CHAT]},
            kinds=['message.created', 'send.failed'])['subscription_id']
        bind(store, token, sid, tmp_path, 'dynamic-reader')
        failure_event(store, 'send.failed', token)
        match_subscriptions(store)
        with store.pool.connection() as c:
            stored = c.execute('SELECT chat FROM subscription_matches WHERE subscription=%s', (sid,)).fetchall()
        # The matcher keeps the failure's own rule; every read, count and hint leaves the listed chat out.
        assert [row['chat'] for row in stored] == ['telegram-demo-chat']
        assert activity(store, token, sid) == {'telegram': {'matched_failure': 1}}
        view = store.get_subscription(token, sid)
        assert view['matched_event_count'] == 0 and event_ids(store, token, sid) == []
        assert view['delivery']['hint_counts'] == {}
        assert store.fetch_subscription_events(token, sid)['items'] == []


# spec: ANYM-830
def test_set_exclusions_replaces_the_list_keeps_the_version_and_refuses_a_stale_revision(seeded):
    store, _, _ = seeded
    with source_exclusions(store):
        token = dynamic_key(store)
        chat_message(store, 'telegram-demo-chat-830', 'demo-tg-830')
        sid = store.create_subscription(token, sources=ALL_VISIBLE)['subscription_id']
        view = store.get_subscription(token, sid)
        version = view['version']
        for kwargs in ({'action': 'set_exclusions', 'exclusions_revision': 0},
                {'action': 'set_exclusions', 'exclude_chats': [DEMO_CHAT]},
                {'action': 'pause', 'exclude_chats': [], 'exclusions_revision': 0}):
            with pytest.raises(APIError, match='invalid_parameters'):
                store.update_subscription(token, sid, version, **kwargs)
        with pytest.raises(APIError, match='subscription_conflict'):
            set_exclusions(store, token, sid, [DEMO_CHAT], 1)
        with pytest.raises(APIError, match='subscription_conflict'):
            set_exclusions(store, token, sid, [DEMO_CHAT], 0, version=version + 1)
        first = set_exclusions(store, token, sid, [DEMO_CHAT], 0)
        assert first['version'] == version and first['updated_at'] > view['updated_at']
        assert first['sources']['exclude_chats'] == [DEMO_CHAT] and first['sources']['exclusions_revision'] == 1
        # An unchanged list is a no-op: no revision, no updated_at.
        same = set_exclusions(store, token, sid, [DEMO_CHAT], 1)
        assert same['updated_at'] == first['updated_at'] and same['sources']['exclusions_revision'] == 1
        with pytest.raises(APIError, match='subscription_conflict'):
            set_exclusions(store, token, sid, [NOISY_CHAT], 0)
        both = set_exclusions(store, token, sid, [NOISY_CHAT, DEMO_CHAT], 1)
        assert both['sources']['exclude_chats'] == [DEMO_CHAT, NOISY_CHAT] and both['version'] == version
        paused = store.update_subscription(token, sid, version, action='pause')
        cleared = set_exclusions(store, token, sid, [], 2)
        assert cleared['state'] == 'paused' and cleared['version'] == paused['version']
        assert cleared['sources']['exclude_chats'] == [] and cleared['sources']['exclusions_revision'] == 3
        closed = store.update_subscription(token, sid, cleared['version'], action='close')
        with pytest.raises(APIError, match='invalid_subscription_state'):
            set_exclusions(store, token, sid, [DEMO_CHAT], 3, version=closed['version'])


# spec: ANYM-830
def test_an_exclusion_hides_stored_references_until_removed_and_an_ack_past_them_skips_them(seeded):
    store, _, _ = seeded
    with source_exclusions(store):
        token = dynamic_key(store)
        chat_message(store, 'telegram-demo-chat-830', 'demo-tg-830')
        acked, unacked = (store.create_subscription(token, sources=ALL_VISIBLE)['subscription_id'] for _ in range(2))
        noisy, kept = emit(store, 'demo-tg-830'), emit(store)
        match_subscriptions(store)
        for sid in (acked, unacked):
            assert event_ids(store, token, sid) == [noisy, kept]
            hidden = set_exclusions(store, token, sid, [NOISY_CHAT], 0)
            assert hidden['matched_event_count'] == 1 and event_ids(store, token, sid) == [kept]
        page = store.fetch_subscription_events(token, acked)
        assert [item['event_id'] for item in page['items']] == [kept]
        ack = store.ack_subscription_events(token, acked, page['receipt'], page['ack_version'])
        assert ack['acked_through'] >= kept
        for sid in (acked, unacked):
            shown = set_exclusions(store, token, sid, [], 1)
            assert shown['matched_event_count'] == 2 and event_ids(store, token, sid) == [noisy, kept]
        # Acknowledged while hidden: never fetched again. Not acknowledged: fetched normally.
        assert store.fetch_subscription_events(token, acked)['items'] == []
        assert [item['event_id'] for item in store.fetch_subscription_events(token, unacked)['items']] == [noisy, kept]
        # A stored pair whose chat left the key's scope is not shown; scope applies first.
        set_exclusions(store, token, unacked, [NOISY_CHAT, DEMO_CHAT], 2)
        store.set_access('dynamic-reader', chats=['telegram-demo-chat'])
        assert store.get_subscription(token, unacked)['sources']['exclude_chats'] == [DEMO_CHAT]
        assert event_ids(store, token, unacked) == []


# spec: ANYM-830
def test_disabling_source_exclusions_masks_definitions_with_a_list_and_cancels_their_hints(seeded, tmp_path):
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    with source_exclusions(store):
        token = dynamic_key(store)
        chat_message(store, 'telegram-demo-chat-830', 'demo-tg-830')
        listed = store.create_subscription(token, sources={**ALL_VISIBLE, 'exclude_chats': [NOISY_CHAT]},
            label='Synthetic excluded')['subscription_id']
        cleared = store.create_subscription(token, sources=ALL_VISIBLE)['subscription_id']
        set_exclusions(store, token, cleared, [], 0)
        plain = store.create_subscription(token, sources=ALL_VISIBLE)['subscription_id']
        bind(store, token, listed, tmp_path, 'dynamic-reader')
        emit(store)
        match_subscriptions(store)
        assert store.get_subscription(token, listed)['delivery']['hint_counts'] == {'pending': 1}
        result = store.set_source_exclusions(False)
        # A definition whose list was cleared still carries its revision: an older matcher must not serve it either.
        assert result['source_exclusions'] == 'disabled' and result['access_changed'] == sorted([listed, cleared])
        assert len(result['cancelled_hints']) == 1
        view = store.get_subscription(token, listed)
        assert set(view) == {'subscription_id', 'state', 'version', 'recreate_required', 'scope_masked'}
        assert 'Synthetic excluded' not in str(store.list_subscriptions(token))
        with pytest.raises(APIError, match='source_exclusions_disabled'):
            store.create_subscription(token, sources={**ALL_VISIBLE, 'exclude_chats': [NOISY_CHAT]})
        assert store.get_subscription(token, plain)['state'] == 'active'
        posts = []
        dispatch_hints(store, transport=lambda *a, **k: posts.append(a) or 204, resolver=lambda *a, **k: ['93.184.216.34'])
        assert posts == []


# spec: ANYM-522
def test_reply_to_own_send_wakes_on_a_reply_to_any_confirmed_send_of_the_key_only(seeded):
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token, other = dynamic_key(store), dynamic_key(store, 'other-reader')
        def send(owner_token, send_id, provider_id, status='sent'):
            with store.authorized(owner_token) as (_, p):
                owner = p
            with store.pool.connection() as c:
                c.execute('''INSERT INTO outgoing_messages(id,principal,key_id,scope_version,key_permission_version,
                    request_id,request_hash,account,chat,provider_chat,body,status,provider_message_id)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,'telegram-demo','telegram-demo-chat','provider-chat','Synthetic',%s,%s)''',
                    (send_id, owner['id'], owner['key_id'], owner['scope_version'], owner['key_permission_version'],
                     'request-' + send_id, 'a' * 64, status, provider_id))
        send(token, 'send_own', 'native-own')
        send(token, 'send_unconfirmed', 'native-unconfirmed', 'unknown')
        send(other, 'send_other', 'native-other')
        send(token, 'send_archived', 'native-archived')
        sid = store.create_subscription(token, sources=ALL_VISIBLE, wake_rules=['reply_to_own_send'])['subscription_id']
        replies = [chat_message(store, 'telegram-demo-chat', 'demo-reply-' + reference,
            metadata={'reply_to_provider_id': reference})
            for reference in ('native-own', 'native-unconfirmed', 'native-other', 'unrelated')]
        events = [emit(store, message) for message in replies]
        # The same provider ID in another chat is not a reply to that send.
        elsewhere = emit(store, chat_message(store, 'telegram-demo-chat-2', 'demo-reply-elsewhere',
            metadata={'reply_to_provider_id': 'native-own'}))
        chat_message(store, 'telegram-demo-chat', 'demo-reply-archived')
        with store.pool.connection() as c:
            archived = store.append_event(c, 'telegram-demo', 'demo-reply-archived', origin='live', snapshot_metadata={
                'reply_evidence': {'basis': 'exact_native_reply_to_archived_outgoing', 'target_message': 'demo-out-1',
                    'target_revision': 1, 'target_provider_id': 'native-archived'}})
        match_subscriptions(store)
        page = store.get_subscription_events(token, sid)
        assert [item['event_id'] for item in page['items']] == [events[0], archived]
        assert elsewhere not in [item['event_id'] for item in page['items']]
        assert all('reply_to_own_send' in item['wake_reason'] for item in page['items'])
    finally:
        store.set_dynamic_subscriptions(False)


# spec: ANYM-522
def test_dynamic_matching_costs_one_scope_query_per_event_batch(seeded, monkeypatch):
    import psycopg
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token = dynamic_key(store)
        for _ in range(2):
            store.create_subscription(token, sources=ALL_VISIBLE)
        for _ in range(3):
            emit(store)
        emit(store, 'demo-wa-1')
        statements = []
        execute = psycopg.Connection.execute
        def counted(self, query, *args, **kwargs):
            statements.append(str(query))
            return execute(self, query, *args, **kwargs)
        monkeypatch.setattr(psycopg.Connection, 'execute', counted)
        # The worker's defaults (subscription_worker.py: 16 subscriptions, 200 events per pass).
        stats = match_subscriptions(store, batch_size=200, max_subscriptions=16)
        assert stats['matched'] == 8
        assert sum('FROM chats' in statement for statement in statements) == 2
        statements.clear()
        assert match_subscriptions(store, batch_size=200, max_subscriptions=16)['matched'] == 0
        assert sum('FROM chats' in statement for statement in statements) == 0
    finally:
        monkeypatch.undo()
        store.set_dynamic_subscriptions(False)


# spec: ANYM-522
def test_a_failing_dynamic_scope_query_never_stops_other_definitions(seeded, monkeypatch):
    from messaging_service import subscriptions
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token = dynamic_key(store)
        sid = store.create_subscription(token, sources=ALL_VISIBLE)['subscription_id']
        explicit = create(store, token)['subscription_id']
        def failing(*args, **kwargs):
            raise RuntimeError('synthetic scope failure')
        monkeypatch.setattr(subscriptions, '_batch_scope', failing)
        event = emit(store)
        for _ in range(2):
            assert match_subscriptions(store, max_subscriptions=1)['scope_failed'] <= 1
        assert event_ids(store, token, explicit) == [event]
        assert event_ids(store, token, sid) == []
        monkeypatch.undo()
        match_subscriptions(store)
        assert event_ids(store, token, sid) == [event]
    finally:
        store.set_dynamic_subscriptions(False)


# spec: ANYM-522
@pytest.mark.parametrize('level', ['key', 'principal'])
def test_a_lost_subscription_write_ends_a_dynamic_definition_even_when_restored_unobserved(seeded, level):
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token = dynamic_key(store)
        sid = store.create_subscription(token, sources=ALL_VISIBLE)['subscription_id']
        assert store.update_subscription(token, sid, 1, action='pause')['state'] == 'paused'
        credential = store.capabilities(token)['credential_id']
        if level == 'key':
            store.set_key_permissions(credential, ['read', 'subscription-ack'])
            store.set_key_permissions(credential, ['read', 'subscription-write', 'subscription-ack'])
        else:
            store.set_access('dynamic-reader', permissions=['read', 'subscription-ack'])
            store.set_access('dynamic-reader', permissions=['read', 'subscription-write', 'subscription-ack'])
        view = store.get_subscription(token, sid)
        assert view['state'] == 'access_changed' and view['recreate_required']
        with pytest.raises(APIError, match='subscription_access_changed'):
            store.update_subscription(token, sid, view['version'], action='resume')
    finally:
        store.set_dynamic_subscriptions(False)


# spec: ANYM-522
def test_reply_to_own_send_follows_the_whatsapp_native_stanza_rules(seeded):
    store, _, _ = seeded
    store.set_dynamic_subscriptions(True)
    try:
        token = dynamic_key(store, accounts=('whatsapp-demo-1',))
        with store.authorized(token) as (_, p):
            owner = p
        with store.pool.connection() as c:
            c.execute("UPDATE chats SET provider_chat='111111@c.us' WHERE id='whatsapp-demo-1-chat'")
            c.execute('''INSERT INTO chats(id,account,title,chat_type,provider_chat) VALUES('whatsapp-demo-1-group',
                'whatsapp-demo-1','Synthetic group','group','120363000000000001@g.us')''')
            for chat, provider_chat, archived, sender in (
                    ('whatsapp-demo-1-chat', '111111@c.us', 'true_111111@c.us_STANZAPRIVATE', 'owner'),
                    ('whatsapp-demo-1-group', '120363000000000001@g.us',
                     'true_120363000000000001@g.us_STANZAGROUP_222222@c.us', '222222@c.us')):
                c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
                    VALUES(%s,'whatsapp-demo-1',%s,%s,%s,'Synthetic sent text',clock_timestamp(),'outgoing')''',
                    ('out-' + chat, chat, archived, sender))
                c.execute('''INSERT INTO outgoing_messages(id,principal,key_id,scope_version,key_permission_version,
                    request_id,request_hash,account,chat,provider_chat,body,status,provider_message_id)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,'whatsapp-demo-1',%s,%s,'Synthetic','sent',%s)''',
                    ('send-' + chat, owner['id'], owner['key_id'], owner['scope_version'],
                     owner['key_permission_version'], 'request-' + chat, 'a' * 64, chat, provider_chat, archived))
        sid = store.create_subscription(token, sources=ALL_VISIBLE, wake_rules=['reply_to_own_send'])['subscription_id']
        private = chat_message(store, 'whatsapp-demo-1-chat', 'wa-reply-private', account='whatsapp-demo-1',
            metadata={'reply_to_provider_id': 'STANZAPRIVATE'})
        group = chat_message(store, 'whatsapp-demo-1-group', 'wa-reply-group', account='whatsapp-demo-1',
            metadata={'reply_to_provider_id': 'STANZAGROUP'})
        # The provider says this quotes no owner message: never projected onto the stanza.
        denied = chat_message(store, 'whatsapp-demo-1-chat', 'wa-reply-denied', account='whatsapp-demo-1',
            metadata={'reply_to_provider_id': 'STANZAPRIVATE', 'reply_to_owner': False})
        with store.pool.connection() as c:
            selected = [store.append_event(c, 'whatsapp-demo-1', message, origin='live') for message in (private, group)]
            refused = store.append_event(c, 'whatsapp-demo-1', denied, origin='live')
        match_subscriptions(store)
        page = store.get_subscription_events(token, sid)
        assert [item['event_id'] for item in page['items']] == selected and refused not in selected
        assert all('reply_to_own_send' in item['wake_reason'] for item in page['items'])
    finally:
        store.set_dynamic_subscriptions(False)


def test_bounded_matching_pagination_restart_and_concurrent_workers(seeded):
    store, _, _ = seeded
    token = key(store)
    sid = create(store, token)['subscription_id']
    events = [emit(store) for _ in range(8)]
    assert match_subscriptions(store, batch_size=2)['matched'] == 2
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: match_subscriptions(store, batch_size=2), range(2)))
    match_subscriptions(store)
    assert match_subscriptions(store)['matched'] == 0
    page = store.get_subscription_events(token, sid, limit=3)
    assert page['has_more'] and [e['event_id'] for e in page['items']] == events[:3]
    next_page = store.get_subscription_events(token, sid, after=page['next_after'])
    assert [e['event_id'] for e in next_page['items']] == events[3:]
    assert next_page['subscription']['matched_event_count'] == 8


def test_match_rows_and_checkpoint_rollback_atomically(seeded, monkeypatch):
    store, _, _ = seeded
    token = key(store)
    sid = create(store, token)['subscription_id']
    event = emit(store)
    with store.pool.connection() as c:
        c.execute("""CREATE FUNCTION subscription_test_failure() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic matcher failure'; END $$""")
        c.execute('CREATE TRIGGER subscription_test_failure BEFORE UPDATE OF matched_through ON subscriptions FOR EACH ROW EXECUTE FUNCTION subscription_test_failure()')
    try:
        with pytest.raises(Exception, match='synthetic matcher failure'):
            match_subscriptions(store)
        with store.pool.connection() as c:
            assert c.execute('SELECT count(*) AS n FROM subscription_matches WHERE subscription=%s', (sid,)).fetchone()['n'] == 0
            assert c.execute('SELECT matched_through FROM subscriptions WHERE id=%s', (sid,)).fetchone()['matched_through'] == 4
    finally:
        with store.pool.connection() as c:
            c.execute('DROP TRIGGER subscription_test_failure ON subscriptions')
            c.execute('DROP FUNCTION subscription_test_failure()')
    match_subscriptions(store)
    assert [e['event_id'] for e in store.get_subscription_events(token, sid)['items']] == [event]


def test_creation_does_not_skip_uncommitted_event(seeded):
    store, _, _ = seeded
    token = key(store)
    inserted = threading.Event()
    release = threading.Event()
    def publisher():
        with store.pool.connection() as c:
            event = store.append_event(c, 'telegram-demo', 'demo-tg-1', origin='live')
            inserted.set()
            assert release.wait(timeout=5)
        return event
    with ThreadPoolExecutor(max_workers=1) as pool:
        publishing = pool.submit(publisher)
        assert inserted.wait(timeout=5)
        try:
            subscription = create(store, token)
            assert subscription['start_position'] == 4
        finally:
            release.set()
        event = publishing.result(timeout=5)
    match_subscriptions(store)
    assert [e['event_id'] for e in store.get_subscription_events(token, subscription['subscription_id'])['items']] == [event]


def test_match_budget_does_not_drop_events_and_recovers(seeded, monkeypatch):
    store, _, _ = seeded
    token = key(store)
    sid = create(store, token)['subscription_id']
    monkeypatch.setenv('SUBSCRIPTION_MAX_MATCHES', '2')
    events = [emit(store) for _ in range(4)]
    assert match_subscriptions(store)['blocked'] == 1
    blocked = store.get_subscription(token, sid)
    assert blocked['blocked_reason'] == 'match_budget'
    assert blocked['gap']['recoverable'] and blocked['matched_through'] == events[1]
    monkeypatch.setenv('SUBSCRIPTION_MAX_MATCHES', '4')
    match_subscriptions(store)
    page = store.get_subscription_events(token, sid)
    assert [e['event_id'] for e in page['items']] == events
    assert page['subscription']['gap'] is None


def test_definition_budget_is_atomic_and_visible(seeded, monkeypatch):
    store, _, _ = seeded
    token = key(store)
    monkeypatch.setenv('SUBSCRIPTION_MAX_DEFINITIONS', '1')
    create(store, token)
    with pytest.raises(APIError, match='subscription_limit'):
        create(store, token)
    assert len(store.list_subscriptions(token)['items']) == 1


def test_removed_journal_event_is_not_complete_empty_result(seeded):
    store, _, _ = seeded
    token = key(store)
    sid = create(store, token)['subscription_id']
    event = emit(store)
    with store.pool.connection() as c:
        c.execute('DELETE FROM events WHERE id=%s', (event,))
    assert match_subscriptions(store)['blocked'] == 1
    state = store.get_subscription_events(token, sid)['subscription']
    assert not state['matcher_caught_up'] and state['gap']['reason'] == 'journal_gap'
    assert state['matched_through'] == 4


def test_old_writer_trigger_captures_identity_without_provider_restart(seeded):
    store, _, _ = seeded
    token = key(store)
    sid = create(store, token, sender_ids=['demo-person-1'])['subscription_id']
    with store.pool.connection() as c:
        event = c.execute('UPDATE feed_counter SET value=value+1 WHERE id=1 RETURNING value').fetchone()['value']
        c.execute('''INSERT INTO events(id,account,message,kind,revision,origin,snapshot)
            VALUES(%s,'telegram-demo','demo-tg-1','message.created',1,'live',%s)''',
            (event, Jsonb({'message': 'demo-tg-1', 'account': 'telegram-demo', 'chat': 'telegram-demo-chat', 'direction': 'incoming'})))
        c.execute("UPDATE messages SET sender='different',revision=revision+1 WHERE id='demo-tg-1'")
    match_subscriptions(store)
    page = store.get_subscription_events(token, sid)
    assert page['items'][0]['sender'] == 'demo-person-1'
    assert page['items'][0]['provider_id'] == 'demo-tg-1'


def test_missing_legacy_sender_snapshot_is_explicit_not_mutable_fallback(seeded):
    store, _, _ = seeded
    token = key(store)
    person = create(store, token, sender_ids=['demo-person-1'])['subscription_id']
    chat = create(store, token)['subscription_id']
    event = emit(store)
    with store.pool.connection() as c:
        c.execute("UPDATE events SET snapshot=snapshot-'sender' WHERE id=%s", (event,))
    assert match_subscriptions(store)['missing_evidence'] == 1
    person_page = store.get_subscription_events(token, person)
    assert person_page['items'] == [] and person_page['subscription']['missing_revision_evidence_events'] == 1
    assert [e['event_id'] for e in store.get_subscription_events(token, chat)['items']] == [event]


def test_fair_matcher_visits_sources_beyond_one_batch(seeded):
    store, _, _ = seeded
    token = key(store)
    subscriptions = [create(store, token)['subscription_id'] for _ in range(5)]
    event = emit(store)
    for _ in range(3):
        match_subscriptions(store, max_subscriptions=2)
    assert all([e['event_id'] for e in store.get_subscription_events(token, sid)['items']] == [event] for sid in subscriptions)


def test_processing_refs_are_independent_of_message_readiness(seeded):
    store, _, _ = seeded
    token = key(store)
    sid = create(store, token, kinds=['message.created', 'file.transcript.ready'])['subscription_id']
    message = emit(store)
    with store.pool.connection() as c:
        ready = store.append_event(c, 'telegram-demo', 'demo-tg-1', 'file.transcript.ready', 'live',
            snapshot_metadata={'processing': {'attachment_id': 'synthetic-voice', 'stage': 'transcript',
                'state': 'ready', 'artifact_version': 'synthetic-v1'}})
    match_subscriptions(store)
    events = store.get_subscription_events(token, sid)['items']
    assert [e['event_id'] for e in events] == [message, ready]
    assert 'processing' not in events[0] and events[1]['processing']['stage'] == 'transcript'


def test_no_push_or_reply_preset_claimed(seeded):
    # Preserve the historical case identity. The refined contract advertises
    # native evidence and operator hints while leaving host execution unverified.
    store, _, _ = seeded
    capabilities = store.capabilities(key(store))['subscriptions']
    assert capabilities['implemented'] and capabilities['passive_events']
    assert capabilities['ack'] and capabilities['ack_permission'] == 'subscription-ack'
    assert capabilities['push'] == 'operator_registered_signed_hints'
    assert capabilities['native_reply_preset'] and not capabilities['verified_host']


# spec: ANYM-252
def test_debounce_flushes_at_max_wait(seeded, tmp_path):
    from datetime import timedelta
    from uuid import uuid4
    from messaging_service.delivery_policy import flush_hints
    from messaging_service.event_delivery import register_receiver
    store, _, _ = seeded
    token = key(store)
    sid = create(store, token)['subscription_id']
    secret = tmp_path / 'receiver.key'
    secret.write_bytes(b'synthetic receiver secret bytes 0123456789')
    secret.chmod(0o600)
    with store.authorized(token) as (_, p):
        key_id = str(p['key_id'])
    rid = str(uuid4())
    register_receiver(store, rid, 'subscriber', key_id, 'https://hooks.example.test/wake', str(secret),
        resolver=lambda *a, **k: ['93.184.216.34'])
    store.configure_subscription_delivery(token, sid, 1, rid, {'debounce_s': 30, 'max_wait_s': 300})

    def elapse(seconds):
        # Durable deadlines move back by the simulated interval; the database clock stays real.
        step = timedelta(seconds=seconds)
        with store.pool.connection() as c:
            c.execute('''UPDATE subscription_hint_windows SET first_match_at=first_match_at-%s,
                last_match_at=last_match_at-%s,due_at=due_at-%s WHERE subscription=%s''', (step, step, step, sid))
            c.execute('UPDATE subscription_outbox SET created_at=created_at-%s WHERE subscription=%s', (step, sid))

    def hints():
        with store.pool.connection() as c:
            return [row['through_position'] for row in c.execute('''SELECT through_position FROM subscription_outbox
                WHERE subscription=%s AND kind='events' ORDER BY created_at,id''', (sid,)).fetchall()]

    # Matches at 0, 5 and 10 s: one hint at 40 s, 30 s after the last match.
    events = []
    for second in (0, 5, 10):
        if second:
            elapse(5)
        events.append(emit(store))
        match_subscriptions(store)
    elapse(29)
    flush_hints(store)
    assert hints() == []
    elapse(1)
    flush_hints(store)
    assert hints() == [events[-1]]
    # One match every 5 s for 10 minutes: a hint at least every 300 s.
    elapse(3600)
    emitted_at = []
    for tick in range(122):
        emit(store)
        match_subscriptions(store)
        flush_hints(store)
        if len(hints()) > 1 + len(emitted_at):
            emitted_at.append(tick * 5)
        elapse(5)
    # Each hint comes at most 300 s after the first match it covers (tick 0, then the tick after
    # the previous hint). Real test runtime adds to the simulated clock, so it may come early.
    assert len(emitted_at) == 2 and 270 <= emitted_at[0] <= 300
    assert all(later - first <= 300 for first, later in zip([0] + [t + 5 for t in emitted_at], emitted_at))


NO_LAG = {'pending_hints': 0, 'oldest_unfinished_age_s': None, 'next_attempt_in_s': None}


def lag_key(store, principal='lag-reader', accounts=('telegram-demo',)):
    permissions = ['read', 'subscription-write', 'subscription-ack']
    return store.issue_key(principal, list(accounts), permissions=permissions, key_permissions=permissions)


def bind(store, token, sid, tmp_path, principal='lag-reader'):
    """A synthetic receiver of the key bound to the subscription."""
    from uuid import uuid4
    from messaging_service.event_delivery import register_receiver
    secret = tmp_path / 'receiver.key'
    secret.write_bytes(b'synthetic receiver secret bytes 0123456789')
    secret.chmod(0o600)
    with store.authorized(token) as (_, p):
        key_id = str(p['key_id'])
    rid = str(uuid4())
    register_receiver(store, rid, principal, key_id, 'https://hooks.example.test/wake', str(secret),
        resolver=lambda *a, **k: ['93.184.216.34'])
    store.configure_subscription_delivery(token, sid, store.get_subscription(token, sid)['version'], rid)
    return sid


def lags(store, token, sid):
    """The delivery view's lag through get, wait and fetch, in that order."""
    import asyncio
    from messaging_service.subscription_consumption import wait_for_subscription_events
    waited = asyncio.run(wait_for_subscription_events(store, token, sid, timeout=0))
    return [store.get_subscription(token, sid)['delivery']['lag'], waited['subscription']['delivery']['lag'],
        store.fetch_subscription_events(token, sid)['subscription']['delivery']['lag']]


def freeze(store, monkeypatch):
    """The lag's clock stops at the database's current time; hint times are set relative to it."""
    from messaging_service import subscriptions
    with store.pool.connection() as c:
        now = c.execute('SELECT clock_timestamp() AS now').fetchone()['now']
    monkeypatch.setattr(subscriptions, '_observed_at', lambda c: now)
    return now


def seconds(value):
    from datetime import timedelta
    return timedelta(seconds=value)


def hints_set(store, sid, assignments, *args):
    with store.pool.connection() as c:
        c.execute('UPDATE subscription_outbox SET ' + assignments + ' WHERE subscription=%s', (*args, sid))


def hint_copy(store, sid, state, request_id, created, due, lease_until=None):
    """A copy of the subscription's hint in another state."""
    with store.pool.connection() as c:
        c.execute('''INSERT INTO subscription_outbox(subscription,subscription_version,receiver,through_position,kind,
            request_id,state,created_at,next_attempt_at,lease_until,finished_at)
            SELECT subscription,subscription_version,receiver,through_position,kind,%s,%s,%s,%s,%s,
            CASE WHEN %s IN ('delivered','dead_letter','cancelled') THEN clock_timestamp() END
            FROM subscription_outbox WHERE subscription=%s ORDER BY created_at LIMIT 1''',
            (request_id, state, created, due, lease_until, state, sid))


# spec: ANYM-752
def test_lag_reads_nulls_without_unfinished_hints_and_the_age_of_the_oldest_pending_one(seeded, tmp_path,
        monkeypatch):
    store, _, _ = seeded
    token = lag_key(store)
    now = freeze(store, monkeypatch)
    assert store.get_subscription(token, create(store, token)['subscription_id'])['delivery']['lag'] == NO_LAG
    sid = bind(store, token, create(store, token)['subscription_id'], tmp_path)
    assert lags(store, token, sid) == [NO_LAG] * 3
    emit(store)
    match_subscriptions(store)
    # The age rounds down: 3600.7 s is 3600.
    hints_set(store, sid, 'created_at=%s,next_attempt_at=%s', now - seconds(3600.7), now)
    assert lags(store, token, sid) == [{'pending_hints': 1, 'oldest_unfinished_age_s': 3600,
        'next_attempt_in_s': 0}] * 3
    assert store.get_subscription(token, sid)['delivery']['hint_counts'] == {'pending': 1}


# spec: ANYM-752
def test_lag_counts_leased_hints_and_leaves_out_delivered_dead_lettered_and_cancelled_ones(seeded, tmp_path,
        monkeypatch):
    store, _, _ = seeded
    token = lag_key(store)
    now = freeze(store, monkeypatch)
    sid = bind(store, token, create(store, token)['subscription_id'], tmp_path)
    emit(store)
    match_subscriptions(store)
    hints_set(store, sid, 'created_at=%s,next_attempt_at=%s', now - seconds(60), now + seconds(3600))
    # Finished hints older and due sooner than any unfinished one change nothing.
    for number, state in enumerate(('delivered', 'dead_letter', 'cancelled')):
        hint_copy(store, sid, state, f'lag-finished-{number}', now - seconds(10800), now - seconds(10800))
    hint_copy(store, sid, 'leased', 'lag-leased', now - seconds(7200), now - seconds(3600), now + seconds(600))
    assert store.get_subscription(token, sid)['delivery']['hint_counts'] == {
        'cancelled': 1, 'dead_letter': 1, 'delivered': 1, 'leased': 1, 'pending': 1}
    # The leased copy is the oldest unfinished hint and the end of its lease the earliest attempt.
    assert lags(store, token, sid) == [{'pending_hints': 2, 'oldest_unfinished_age_s': 7200,
        'next_attempt_in_s': 600}] * 3


# spec: ANYM-752
def test_lag_schedules_a_pending_hint_by_next_attempt_and_a_leased_one_by_its_lease(seeded, tmp_path, monkeypatch):
    store, _, _ = seeded
    token = lag_key(store)
    now = freeze(store, monkeypatch)
    sid = bind(store, token, create(store, token)['subscription_id'], tmp_path)
    emit(store)
    match_subscriptions(store)
    cases = [
        # A future attempt counts down to it, rounded up: 0.4 s is 1, never "due now".
        ('pending', now + seconds(3600), None, 3600),
        ('pending', now + seconds(0.4), None, 1),
        # A due or past attempt reads 0.
        ('pending', now, None, 0),
        ('pending', now - seconds(1), None, 0),
        # A leased hint is scheduled by its lease, though leasing left next_attempt_at in the past.
        ('leased', now - seconds(3600), now + seconds(600), 600),
        ('leased', now - seconds(3600), now + seconds(0.4), 1),
        # An ended lease is due again.
        ('leased', now - seconds(3600), now, 0),
        ('leased', now + seconds(3600), now - seconds(0.5), 0)]
    for state, due, lease_until, expected in cases:
        hints_set(store, sid, 'state=%s,next_attempt_at=%s,lease_until=%s', state, due, lease_until)
        assert [lag['next_attempt_in_s'] for lag in lags(store, token, sid)] == [expected] * 3, (state, due, lease_until)


# spec: ANYM-752
def test_lag_of_two_subscriptions_does_not_mix(seeded, tmp_path, monkeypatch):
    store, _, _ = seeded
    token = lag_key(store)
    now = freeze(store, monkeypatch)
    old = bind(store, token, create(store, token)['subscription_id'], tmp_path)
    new = bind(store, token, create(store, token)['subscription_id'], tmp_path)
    emit(store)
    match_subscriptions(store)
    hints_set(store, old, 'created_at=%s,next_attempt_at=%s', now - seconds(3600), now + seconds(30))
    hints_set(store, new, 'created_at=%s,next_attempt_at=%s', now - seconds(5), now + seconds(90))
    assert lags(store, token, old) == [{'pending_hints': 1, 'oldest_unfinished_age_s': 3600,
        'next_attempt_in_s': 30}] * 3
    assert lags(store, token, new) == [{'pending_hints': 1, 'oldest_unfinished_age_s': 5,
        'next_attempt_in_s': 90}] * 3


# spec: ANYM-752
def test_lag_leaves_out_a_hint_of_a_reference_the_key_cannot_see(seeded, tmp_path, monkeypatch):
    store, _, _ = seeded
    now = freeze(store, monkeypatch)
    store.set_dynamic_subscriptions(True)
    try:
        token = lag_key(store, 'dynamic-reader')
        chat_message(store, 'telegram-demo-chat-2', 'demo-tg-x')
        sid = bind(store, token, store.create_subscription(token, sources=ALL_VISIBLE)['subscription_id'], tmp_path,
            'dynamic-reader')
        emit(store, 'demo-tg-x')
        match_subscriptions(store)
        hints_set(store, sid, 'created_at=%s,next_attempt_at=%s', now - seconds(3600), now + seconds(30))
        assert lags(store, token, sid) == [{'pending_hints': 1, 'oldest_unfinished_age_s': 3600,
            'next_attempt_in_s': 30}] * 3
        store.set_access('dynamic-reader', chats=['telegram-demo-chat'])
        # Back to pending, as if the scope change had not cancelled it: its only reference is hidden now.
        hints_set(store, sid, "state='pending',error_code=NULL,finished_at=NULL")
        assert lags(store, token, sid) == [NO_LAG] * 3
        assert store.get_subscription(token, sid)['delivery']['hint_counts'] == {}
    finally:
        store.set_dynamic_subscriptions(False)


def failure_keys(store):
    permissions = ['read', 'send', 'subscription-write']
    return [store.issue_key('failure-agent', ['send-fixture'], permissions=permissions,
        key_permissions=permissions) for _ in range(2)]


def failing_provider(outcome):
    async def provider(job):
        if outcome == 'rejected':
            job['_provider_phase']('refused')
        raise RuntimeError('synthetic provider failure')
    return provider


# spec: ANYM-535
@pytest.mark.parametrize('outcome,status,code', [('rejected', 'failed', 'send_not_started'),
    ('uncertain', 'unknown', 'send_result_unknown')], ids=['failed', 'uncertain'])
def test_send_failed_reaches_only_the_sending_key(sender, outcome, status, code):  # noqa: F811
    import asyncio
    import json
    from messaging_service import outgoing
    from messaging_service.envelope import stable_id
    s, _, _, _, state = sender
    mine, sibling = failure_keys(s)
    chat = stable_id('chat', 'send-fixture', '100')
    selected = {name: s.create_subscription(token, [chat], kinds=['send.failed'])['subscription_id']
        for name, token in (('mine', mine), ('sibling', sibling))}
    default = s.create_subscription(mine, [chat], direction='')['subscription_id']
    queued = s.send_message(mine, account='send-fixture', chat_id=chat, text='Synthetic failing text',
        request_id='synthetic-failure-0001')
    asyncio.run(outgoing.OutgoingWorker(state, failing_provider(outcome)).once())
    assert s.get_send_status(mine, queued['send_id'])['status'] == status
    match_subscriptions(s)
    items = s.get_subscription_events(mine, selected['mine'])['items']
    assert len(items) == 1
    item = items[0]
    assert (item['kind'], item['send_id'], item['status'], item['error_code']) == ('send.failed', queued['send_id'], status, code)
    assert item['account'] == 'send-fixture' and item['chat'] == chat and item['origin'] == 'live'
    assert not {'principal', 'key_id', 'message', 'body', 'sender'} & set(item)
    assert 'Synthetic failing text' not in json.dumps(item, default=str)
    # Another key of the same principal, and the default kinds of the sending key, see nothing.
    assert s.get_subscription_events(sibling, selected['sibling'])['items'] == []
    assert s.get_subscription_events(mine, default)['items'] == []


# spec: ANYM-535
def test_send_failed_journal_error_never_blocks_the_send_status(sender, monkeypatch):  # noqa: F811
    import asyncio
    from messaging_service import outgoing, subscriptions
    from messaging_service.envelope import stable_id
    s, _, _, _, state = sender
    mine, _ = failure_keys(s)
    chat = stable_id('chat', 'send-fixture', '100')
    sid = s.create_subscription(mine, [chat], kinds=['send.failed'])['subscription_id']
    queued = s.send_message(mine, account='send-fixture', chat_id=chat, text='Synthetic failing text',
        request_id='synthetic-failure-0002')
    with s.pool.connection() as c:
        head = c.execute('SELECT value FROM feed_counter WHERE id=1').fetchone()['value']
        c.execute('''CREATE FUNCTION pg_temp.refuse_send_failed() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic journal failure'; END $$''')
        c.execute('''CREATE TRIGGER refuse_send_failed BEFORE INSERT ON events FOR EACH ROW
            WHEN (NEW.kind='send.failed') EXECUTE FUNCTION pg_temp.refuse_send_failed()''')
    before = subscriptions.JOURNAL_FAILURES['send.failed']

    def closed_log(*args, **kwargs):
        raise BrokenPipeError('synthetic closed log pipe')
    # Its diagnostics fail too: neither may undo the send status.
    monkeypatch.setattr(subscriptions, 'print', closed_log, raising=False)
    try:
        asyncio.run(outgoing.OutgoingWorker(state, failing_provider('uncertain')).once())
    finally:
        with s.pool.connection() as c:
            c.execute('DROP TRIGGER refuse_send_failed ON events')
    assert subscriptions.JOURNAL_FAILURES['send.failed'] == before + 1
    status = s.get_send_status(mine, queued['send_id'])
    assert status['status'] == 'unknown' and status['error_code'] == 'send_result_unknown'
    with s.pool.connection() as c:
        # The counter step rolled back with the row: no journal gap for the matcher.
        assert c.execute('SELECT value FROM feed_counter WHERE id=1').fetchone()['value'] == head
    match_subscriptions(s)
    page = s.get_subscription_events(mine, sid)
    assert page['items'] == [] and page['subscription']['gap'] is None
    assert page['subscription']['blocked_reason'] is None


# spec: ANYM-535
def test_source_health_journals_status_changes_for_account_readers(seeded):
    import json
    from messaging_service.archive import Archive
    from messaging_service.collector_state import CollectorState
    from messaging_service.subscriptions import journal_source_health
    store, _, _ = seeded
    permissions = ['read', 'subscription-write']
    reader = store.issue_key('health-reader', ['telegram-demo'], permissions=permissions, key_permissions=permissions)
    restricted = store.issue_key('health-chat-reader', ['telegram-demo'], chats=['telegram-demo-chat'],
        permissions=permissions, key_permissions=permissions)
    elsewhere = store.issue_key('health-elsewhere', ['whatsapp-demo-1'], permissions=permissions,
        key_permissions=permissions)
    selected = store.create_subscription(reader, ['telegram-demo-chat'], kinds=['source.health'])['subscription_id']
    default = store.create_subscription(reader, ['telegram-demo-chat'], direction='')['subscription_id']
    narrow = store.create_subscription(restricted, ['telegram-demo-chat'], kinds=['source.health'])['subscription_id']
    other = store.create_subscription(elsewhere, ['whatsapp-demo-1-chat'], kinds=['source.health'])['subscription_id']
    archive = Archive(store)
    epoch = archive.acquire('telegram-demo', 'synthetic-health')
    state = CollectorState(archive, 'telegram-demo', ('synthetic-health', epoch))

    def synthetic(path, value):
        with store.pool.connection() as c:
            c.execute("""UPDATE collector_health SET operations=jsonb_set(operations,%s,%s)
                WHERE account='telegram-demo'""", (path, Jsonb(value)))
    long_ago = str(datetime.now(timezone.utc).replace(year=2025))
    state.phase('connection', 'connected', 'provider_ready')
    state.capture_progress(None, 0, 0)
    assert journal_source_health(store) == 0  # The first observation is a baseline.
    state.connection_failed('provider_disconnected')
    assert journal_source_health(store) == 1 and journal_source_health(store) == 0
    state.phase('connection', 'connected', 'provider_ready')
    assert journal_source_health(store) == 1
    # A disconnect and reconnect between two calls is not lost.
    state.connection_failed('provider_disconnected')
    state.phase('connection', 'connected', 'provider_ready')
    assert journal_source_health(store) == 2
    # Nor is a collector restart (a new lease epoch) between two calls.
    archive.release('telegram-demo', 'synthetic-health', epoch)
    epoch = archive.acquire('telegram-demo', 'synthetic-health')
    state = CollectorState(archive, 'telegram-demo', ('synthetic-health', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    assert journal_source_health(store) == 2
    # No provider event since the connect for longer than the default 30 minutes; capture refreshes
    # without a committed event do not count.
    synthetic('{connection,changed_at}', long_ago)
    with store.pool.connection() as c:
        # The same connect as last observed, only earlier: not a reconnect.
        c.execute("UPDATE source_health_journal SET connection_changed_at=%s WHERE account='telegram-demo'", (long_ago,))
    state.capture_progress(None, 0, 0)
    assert journal_source_health(store) == 1
    synthetic('{live_capture,last_commit}', {'committed_by': str(datetime.now(timezone.utc))})
    assert journal_source_health(store) == 1
    state.connection_failed('telegram_pairing_required')
    assert journal_source_health(store) == 1
    archive.release('telegram-demo', 'synthetic-health', epoch)
    assert journal_source_health(store) == 0  # Still auth_required: not a change.
    match_subscriptions(store)
    items = store.get_subscription_events(reader, selected)['items']
    assert [(item['kind'], item['account'], item['status']) for item in items] == [
        ('source.health', 'telegram-demo', status) for status in ('disconnected', 'connected', 'disconnected',
            'connected', 'disconnected', 'connected', 'stale', 'connected', 'auth_required')]
    assert all(not {'chat', 'message', 'sender', 'send_id', 'error_code'} & set(item) for item in items)
    assert 'provider_disconnected' not in json.dumps(items, default=str)
    for token, sid in ((reader, default), (restricted, narrow), (elsewhere, other)):
        assert store.get_subscription_events(token, sid)['items'] == []


# spec: ANYM-535
def test_bound_collector_journals_only_its_own_failed_sends(sender):  # noqa: F811
    import asyncio
    from types import SimpleNamespace
    from messaging_service import outgoing
    from messaging_service.archive import Archive
    from messaging_service.collector_state import CollectorState
    from messaging_service.envelope import stable_id
    from test_db_role_isolation import RolePool, install, refused, remove
    s, _, _, _, state = sender
    mine, sibling = failure_keys(s)
    chat = stable_id('chat', 'send-fixture', '100')
    sid = s.create_subscription(mine, [chat], kinds=['send.failed'])['subscription_id']
    role = install(s, 'collector_role.sql', 'messaging_collector', account='send-fixture')

    def submit(n):
        return s.send_message(mine, account='send-fixture', chat_id=chat, text='Synthetic failing text',
            request_id='synthetic-bound-%04d' % n)['send_id']
    try:
        bound = CollectorState(Archive(SimpleNamespace(pool=RolePool(s, role), append_event=s.append_event,
            files=s.files)), 'send-fixture', state.lease)
        finished = submit(1)
        asyncio.run(outgoing.OutgoingWorker(bound, failing_provider('uncertain')).once())
        interrupted = submit(2)
        with s.pool.connection() as c:
            c.execute("UPDATE outgoing_messages SET status='dispatching',lease_epoch=NULL WHERE id=%s", (interrupted,))
        asyncio.run(outgoing.OutgoingWorker(bound, failing_provider('uncertain')).once())
        queued = submit(3)
        with s.pool.connection() as c:
            sends = {row['id']: row for row in c.execute('''SELECT id,account,chat,principal,key_id::text AS key_id,
                status,error_code FROM outgoing_messages WHERE id=ANY(%s)''', ([finished, queued],)).fetchall()}
            other_key = c.execute("SELECT id::text AS id FROM api_keys WHERE id::text<>%s AND principal='failure-agent'",
                (sends[finished]['key_id'],)).fetchone()['id']
        forged = 'INSERT INTO events(id,account,message,kind,revision,origin,snapshot) VALUES(0,%s,NULL,%s,1,%s,%s)'
        # Another key's subscription, an unfinished send, a non-send kind: refused by the guard.
        for kind, snapshot in (('send.failed', {**sends[finished], 'send_id': finished, 'key_id': other_key}),
                               ('send.failed', {**sends[queued], 'send_id': queued, 'status': 'failed'}),
                               ('source.health', {'account': 'send-fixture', 'status': 'disconnected'})):
            refused(s, role, forged, ('send-fixture', kind, 'live', Jsonb(snapshot)))
    finally:
        remove(s, role)
    match_subscriptions(s)
    items = s.get_subscription_events(mine, sid)['items']
    assert [(item['send_id'], item['status'], item['error_code']) for item in items] == [
        (finished, 'unknown', 'send_result_unknown'), (interrupted, 'unknown', 'sender_interrupted')]
    assert sibling and s.get_send_status(mine, queued)['status'] == 'queued'
