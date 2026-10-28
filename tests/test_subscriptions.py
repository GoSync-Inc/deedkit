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
    # A connect and a new disconnect between two calls are not lost (the collector counts connects).
    state.phase('connection', 'connected', 'provider_ready')
    state.connection_failed('provider_disconnected')
    assert journal_source_health(store) == 2
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
            'connected', 'disconnected', 'connected', 'disconnected', 'connected', 'stale', 'connected',
            'auth_required')]
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
