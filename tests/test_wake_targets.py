"""Agent self-registered wake targets (ANYM-848): synthetic keys, subscriptions and threads only."""
import json
import sys
import threading
import time
from uuid import uuid4

import pytest

from messaging_service.store import APIError

PERMISSIONS = ['read', 'subscription-write', 'delivery-write']
THREAD = '01a1bec2-e9e5-7fd0-8745-b49a9fc19c0e'
OTHER_THREAD = 'Project chat 2'
CODE = 'a' * 32


def identity(store, name='wake-agent', permissions=PERMISSIONS):
    return store.issue_key(name, ['telegram-demo'], permissions=permissions, key_permissions=permissions)


def subscription(store, token):
    return store.create_subscription(token, ['telegram-demo-chat'])['subscription_id']


def register(store, token, sids, request_id=None, thread=THREAD, **kwargs):
    return store.register_wake_target(token, request_id or 'wake-' + uuid4().hex[:12], 'codex_thread', thread, sids,
        **kwargs)


def refusal(call):
    with pytest.raises(APIError) as refused:
        call()
    return refused.value


@pytest.fixture()
def gate(seeded):
    store = seeded[0]
    store.set_delivery_features(True)
    yield store
    store.set_delivery_features(False)


# spec: ANYM-848
def test_a_key_registers_lists_and_replays_its_own_wake_target(gate):
    store = gate
    token = identity(store)
    sid = subscription(store, token)
    created = register(store, token, [sid], request_id='wake-request-1', label='Project X group')
    assert {key: created[key] for key in ('kind', 'thread', 'label', 'hourly_budget', 'state', 'version',
        'subscription_ids', 'bound', 'confirmation', 'disabled_reason')} == {'kind': 'codex_thread', 'thread': THREAD,
        'label': 'Project X group', 'hourly_budget': 12, 'state': 'enabled', 'version': 1, 'subscription_ids': [sid],
        'bound': 1, 'confirmation': None, 'disabled_reason': None}
    # The same request again is the same row; the same request_id with other fields is a conflict.
    assert register(store, token, [sid], request_id='wake-request-1', label='Project X group') == created
    assert refusal(lambda: register(store, token, [sid], request_id='wake-request-1', label='other')).code == \
        'idempotency_conflict'
    listed = store.list_wake_targets(token)
    assert listed['max_wake_targets'] == 8 and listed['has_more'] is False
    assert [item['wake_target_id'] for item in listed['items']] == [created['wake_target_id']]
    # The subscription names its target; DeedKit stores the rule only.
    assert store.get_subscription(token, sid)['delivery']['wake_target_id'] == created['wake_target_id']
    deleted = store.delete_wake_target(token, created['wake_target_id'], 1)
    assert deleted == {'wake_target_id': created['wake_target_id'], 'deleted': True, 'unbound_subscriptions': 1}
    # A deleted target's replay returns it as it is now; a new registration needs a new request_id.
    replay = register(store, token, [sid], request_id='wake-request-1', label='Project X group')
    assert replay['state'] == 'deleted' and replay['bound'] == 0
    assert store.list_wake_targets(token)['items'] == []
    assert store.get_subscription(token, sid)['delivery']['wake_target_id'] is None


# spec: ANYM-848
def test_every_tool_needs_read_delivery_write_and_the_exact_key(gate):
    store = gate
    token = identity(store)
    sid = subscription(store, token)
    target = register(store, token, [sid])
    tid = target['wake_target_id']
    calls = (lambda t: register(store, t, [sid]), lambda t: store.list_wake_targets(t),
             lambda t: store.update_wake_target(t, tid, 1, 'pause'), lambda t: store.delete_wake_target(t, tid, 1),
             lambda t: store.confirm_wake_target(t, tid, CODE))
    for permissions in (['read', 'subscription-write'], ['delivery-write', 'subscription-write']):
        weak = identity(store, 'wake-weak-' + permissions[0], permissions)
        for call in calls:
            assert refusal(lambda: call(weak)).code == 'forbidden'
    # Another key of the same principal and a key of another principal see nothing of this key's.
    sibling = store.issue_key('wake-agent', ['telegram-demo'], permissions=PERMISSIONS, key_permissions=PERMISSIONS)
    stranger = identity(store, 'wake-stranger')
    for other in (sibling, stranger):
        assert store.list_wake_targets(other)['items'] == []
        for call in calls[2:]:
            assert refusal(lambda: call(other)).code == 'not_found'
        # Another key's subscription is not_found whether or not it is bound.
        unbound = subscription(store, token)
        bound_refusal = refusal(lambda: register(store, other, [sid]))
        unbound_refusal = refusal(lambda: register(store, other, [unbound]))
        assert bound_refusal.code == unbound_refusal.code == 'not_found'
        assert bound_refusal.field_errors == unbound_refusal.field_errors and not hasattr(bound_refusal, 'wake_target')
    assert store.list_wake_targets(token)['items'][0]['state'] == 'enabled'


# spec: ANYM-848
def test_hostile_threads_labels_and_lists_are_field_errors(gate):
    store = gate
    token = identity(store)
    sid = subscription(store, token)
    for thread in ('-c', '$(x)', 'a' * 65, 'line\nbreak', '', ' leading', 7):
        failure = refusal(lambda: register(store, token, [sid], thread=thread))
        assert failure.code == 'invalid_wake_target' and failure.field_errors[0]['field'] == 'thread'
    for sids, field in (([], 'subscription_ids'), ([sid, sid], 'subscription_ids'), (['not-a-uuid'], 'subscription_ids/0'),
                        ([str(uuid4()) for _ in range(17)], 'subscription_ids'), (sid, 'subscription_ids')):
        failure = refusal(lambda: register(store, token, sids))
        assert failure.code == 'invalid_wake_target' and failure.field_errors[0]['field'] == field
    for label in ('x' * 81, 'tab\tlabel'):
        assert refusal(lambda: register(store, token, [sid], label=label)).field_errors[0]['field'] == 'label'
    for budget in (0, 61, True, '12'):
        assert refusal(lambda: register(store, token, [sid], hourly_budget=budget)).field_errors[0]['field'] == \
            'hourly_budget'
    assert refusal(lambda: store.register_wake_target(token, 'wake-kind-1', 'webhook', THREAD, [sid])).field_errors[0] \
        == {'field': 'kind', 'reason': 'enum', 'allowed': ['codex_thread']}
    assert refusal(lambda: store.confirm_wake_target(token, str(uuid4()), 'A' * 32)).field_errors[0]['field'] == 'code'
    assert refusal(lambda: store.update_wake_target(token, str(uuid4()), 1, 'pause')).code == 'not_found'
    assert refusal(lambda: store.update_wake_target(token, str(uuid4()), 1, 'reset')).field_errors[0]['field'] == \
        'action'
    with gate.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM wake_targets').fetchone()['n'] == 0
    assert register(store, token, [sid], thread='Session name 2', hourly_budget=60)['hourly_budget'] == 60


# spec: ANYM-848
def test_the_limit_answer_lists_the_keys_targets(gate, monkeypatch):
    store = gate
    monkeypatch.setenv('WAKE_MAX_TARGETS_PER_KEY', '2')
    token = identity(store)
    first = register(store, token, [subscription(store, token)], label='one')
    register(store, token, [subscription(store, token)], label='two')
    failure = refusal(lambda: register(store, token, [subscription(store, token)]))
    assert failure.code == 'wake_target_limit' and failure.status == 429 and failure.max == 2
    assert [(item['label'], item['bound'], item['state']) for item in failure.wake_targets] == \
        [('two', 1, 'enabled'), ('one', 1, 'enabled')]
    assert store.list_wake_targets(token)['max_wake_targets'] == 2
    store.delete_wake_target(token, first['wake_target_id'], 1)
    assert register(store, token, [subscription(store, token)])['state'] == 'enabled'


def waiting_on_lock(store, workers, count, timeout=5):
    deadline = time.monotonic() + timeout
    with store.pool.connection() as c:
        while c.execute("SELECT count(*) AS n FROM pg_locks WHERE locktype='advisory' AND NOT granted").fetchone()['n'] < count:
            assert time.monotonic() < deadline and all(w.is_alive() for w in workers), 'the registers never waited'
            time.sleep(0.02)


# spec: ANYM-848
def test_two_concurrent_registers_at_the_limit_leave_one_new_row(gate, monkeypatch):
    store = gate
    monkeypatch.setenv('WAKE_MAX_TARGETS_PER_KEY', '2')
    token = identity(store)
    register(store, token, [subscription(store, token)])
    sids = [subscription(store, token), subscription(store, token)]
    with store.pool.connection() as c:
        key_id = c.execute('SELECT key_id FROM wake_targets').fetchone()['key_id']
    results = []
    def worker(sid):
        try:
            results.append(register(store, token, [sid])['state'])
        except APIError as error:
            results.append(error.code)
    # Hold the key's lock, so both registers queue on it before either counts the rows.
    with store.pool.connection() as holder:
        holder.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('wake-targets:' + str(key_id),))
        workers = [threading.Thread(target=worker, args=(sid,)) for sid in sids]
        for w in workers:
            w.start()
        waiting_on_lock(store, workers, 2)
    for w in workers:
        w.join(10)
    assert sorted(results) == ['enabled', 'wake_target_limit']
    assert len(store.list_wake_targets(token)['items']) == 2


# spec: ANYM-848
def test_a_bound_subscription_names_its_holder_and_rebind_moves_it(gate):
    store = gate
    token = identity(store)
    sid, spare = subscription(store, token), subscription(store, token)
    holder = register(store, token, [sid], label='old chat')
    failure = refusal(lambda: register(store, token, [sid], thread=OTHER_THREAD))
    assert failure.code == 'subscription_already_bound' and failure.status == 409
    assert failure.field_errors[0]['field'] == 'subscription_ids' and failure.field_errors[0]['reason'] == 'unique'
    assert failure.wake_target == {'wake_target_id': holder['wake_target_id'], 'version': 1, 'subscription_id': sid}
    moved = register(store, token, [sid], thread=OTHER_THREAD, rebind=True)
    assert moved['subscription_ids'] == [sid]
    old = next(item for item in store.list_wake_targets(token)['items'] if item['label'] == 'old chat')
    assert old['bound'] == 0 and old['subscription_ids'] == [] and old['version'] == 2
    # set_subscriptions replaces the target's own bindings, with rebind for one held elsewhere.
    assert refusal(lambda: store.update_wake_target(token, old['wake_target_id'], 2, 'set_subscriptions',
        subscription_ids=[sid, spare])).code == 'subscription_already_bound'
    back = store.update_wake_target(token, old['wake_target_id'], 2, 'set_subscriptions', subscription_ids=[sid, spare],
        rebind=True)
    assert back['subscription_ids'] == sorted([sid, spare]) and back['version'] == 3
    assert store.list_wake_targets(token)['items'][0]['bound'] == 0
    with gate.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM wake_target_subscriptions WHERE subscription=%s', (sid,)).fetchone()['n'] == 1


# spec: ANYM-848
def test_state_transitions_and_the_operators_disable(gate):
    store = gate
    token = identity(store)
    sid = subscription(store, token)
    target = register(store, token, [sid], request_id='wake-transitions-1')
    tid = target['wake_target_id']
    assert refusal(lambda: store.update_wake_target(token, tid, 1, 'resume')).code == 'invalid_wake_target_state'
    paused = store.update_wake_target(token, tid, 1, 'pause')
    assert paused['state'] == 'agent_paused' and paused['version'] == 2
    stale = refusal(lambda: store.update_wake_target(token, tid, 1, 'resume'))
    assert stale.code == 'wake_target_conflict' and stale.status == 409 and stale.version == 2
    assert refusal(lambda: store.update_wake_target(token, tid, 2, 'pause')).code == 'invalid_wake_target_state'
    assert store.update_wake_target(token, tid, 2, 'resume')['state'] == 'enabled'
    assert store.set_wake_target_operator_state(tid, False) == {'wake_target_id': tid, 'state': 'operator_disabled'}
    version = store.list_wake_targets(token)['items'][0]['version']
    for call in (lambda: store.update_wake_target(token, tid, version, 'pause'),
                 lambda: store.update_wake_target(token, tid, version, 'resume'),
                 lambda: store.update_wake_target(token, tid, version, 'set_thread', thread=OTHER_THREAD),
                 lambda: store.update_wake_target(token, tid, version, 'set_subscriptions', subscription_ids=[sid]),
                 lambda: store.confirm_wake_target(token, tid, CODE)):
        failure = refusal(call)
        assert failure.code == 'invalid_wake_target_state' and failure.disabled_reason == 'operator'
        assert 'deedkit admin wake-targets enable' in failure.next_step
    # A replay never re-enables it; only the operator does.
    assert register(store, token, [sid], request_id='wake-transitions-1')['state'] == 'operator_disabled'
    with pytest.raises(ValueError, match='invalid_wake_target_state'):
        store.set_wake_target_operator_state(store.set_wake_target_operator_state(tid, True)['wake_target_id'], True)
    assert store.list_wake_targets(token)['items'][0]['state'] == 'enabled'
    store.set_wake_target_operator_state(tid, False)
    version = store.list_wake_targets(token)['items'][0]['version']
    assert store.delete_wake_target(token, tid, version)['deleted'] is True


# spec: ANYM-848
def test_confirm_stores_replaces_and_a_new_thread_clears_it(gate):
    store = gate
    token = identity(store)
    tid = register(store, token, [subscription(store, token)])['wake_target_id']
    assert store.confirm_wake_target(token, tid, 'b' * 32)['confirmation'] == 'b' * 32
    # The same code is a no-op; a later code replaces a wrong or pre-filled one.
    assert store.confirm_wake_target(token, tid, 'b' * 32)['version'] == 2
    confirmed = store.confirm_wake_target(token, tid, CODE)
    assert confirmed['confirmation'] == CODE and confirmed['version'] == 3
    moved = store.update_wake_target(token, tid, 3, 'set_thread', thread=OTHER_THREAD)
    assert moved['thread'] == OTHER_THREAD and moved['confirmation'] is None and moved['version'] == 4


# spec: ANYM-848
def test_a_closed_subscription_drops_out_of_the_list(gate):
    store = gate
    token = identity(store)
    sid, kept = subscription(store, token), subscription(store, token)
    tid = register(store, token, [sid, kept])['wake_target_id']
    store.update_subscription(token, sid, 1, action='close')
    item = store.list_wake_targets(token)['items'][0]
    assert item['wake_target_id'] == tid and item['subscription_ids'] == [kept] and item['bound'] == 1
    # A closed subscription is no longer the key's to bind.
    assert refusal(lambda: register(store, token, [sid], thread=OTHER_THREAD)).code == 'not_found'


# spec: ANYM-848
def test_the_delivery_switch_off_refuses_every_tool(seeded):
    store = seeded[0]
    store.set_delivery_features(True)
    token = identity(store)
    sid = subscription(store, token)
    tid = register(store, token, [sid])['wake_target_id']
    store.set_delivery_features(False)
    for call in (lambda: register(store, token, [sid]), lambda: store.list_wake_targets(token),
                 lambda: store.update_wake_target(token, tid, 1, 'pause'),
                 lambda: store.delete_wake_target(token, tid, 1), lambda: store.confirm_wake_target(token, tid, CODE)):
        failure = refusal(call)
        assert failure.code == 'delivery_disabled' and failure.status == 409


# spec: ANYM-848
def test_a_bound_target_changes_neither_hints_nor_push_delivery(gate):
    store = gate
    token = identity(store)
    sid = subscription(store, token)
    with gate.pool.connection() as c:
        before = (c.execute('SELECT count(*) AS n FROM subscription_outbox WHERE subscription=%s', (sid,)).fetchone(),
                  c.execute('SELECT * FROM subscription_delivery WHERE subscription=%s', (sid,)).fetchone())
    tid = register(store, token, [sid])['wake_target_id']
    store.confirm_wake_target(token, tid, CODE)
    with gate.pool.connection() as c:
        after = (c.execute('SELECT count(*) AS n FROM subscription_outbox WHERE subscription=%s', (sid,)).fetchone(),
                 c.execute('SELECT * FROM subscription_delivery WHERE subscription=%s', (sid,)).fetchone())
    assert after == before and before[1] is None
    delivery = store.get_subscription(token, sid)['delivery']
    assert delivery['push'] is False and delivery['wake_target_id'] == tid


# spec: ANYM-848
def test_a_schema_71_database_answers_the_tools_delivery_disabled_and_every_read_as_before(database, tmp_path):
    """A genuine schema-71 database (the real schema_ready guard, no stub): the new code refuses the five tools with a
    boundary audit and answers get, events, fetch and wait with a null wake target; a snapshot of it captures, verifies
    and quarantines without the new tables; migrating it then adds them."""
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    from messaging_service.backup import create_snapshot, database_manifest, quarantine_restore, verify_snapshot
    from messaging_service.subscriptions import match_subscriptions
    from messaging_service.wake_targets import schema_ready
    from test_subscription_retention import emit, restore_database
    with restore_database(database, tmp_path / 'old', through=71) as old:
        with old.pool.connection() as c:
            assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '71'
            assert schema_ready(c) is False
        old.set_delivery_features(True)
        token = identity(old)
        sid = subscription(old, token)
        emit(old)
        match_subscriptions(old)
        tid = str(uuid4())
        for call in (lambda: register(old, token, [sid]), lambda: old.list_wake_targets(token),
                     lambda: old.update_wake_target(token, tid, 1, 'pause'), lambda: old.delete_wake_target(token, tid, 1),
                     lambda: old.confirm_wake_target(token, tid, CODE)):
            failure = refusal(call)
            assert failure.code == 'delivery_disabled' and failure.audited
        assert old.get_subscription(token, sid)['delivery']['wake_target_id'] is None
        assert len(old.get_subscription_events(token, sid)['items']) == 1
        assert old.fetch_subscription_events(token, sid)['subscription']['delivery']['wake_target_id'] is None
        auth = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'}
        with TestClient(create_app(old, ['testserver'])) as client:
            waited = client.post('/mcp', headers=auth, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'wait_for_subscription_events', 'arguments': {'subscription_id': sid, 'timeout': 0}}}
                ).json()['result']
            rest = client.post('/v1/list_wake_targets', headers=auth, json={})
        assert not waited.get('isError') and waited['structuredContent']['subscription']['delivery']['wake_target_id'] is None
        assert rest.status_code == 409 and rest.json()['error'] == 'delivery_disabled'
        with old.pool.connection() as c:
            rows = c.execute("""SELECT operation,outcome,error_code FROM audit_log WHERE operation='http_boundary'
                ORDER BY occurred_at""").fetchall()
        assert rows == [{'operation': 'http_boundary', 'outcome': 'denied', 'error_code': 'delivery_disabled'}] * 6
        # A schema-71 backup: captured, verified and quarantined by this runtime without the new tables.
        old.set_delivery_features(False)
        snapshot = tmp_path / 'snapshot71'
        create_snapshot(old, snapshot, [sys.executable, '-c', 'print("unit-dump-stub")'])
        assert verify_snapshot(old, snapshot)['status'] == 'verified'
        manifest = json.loads((snapshot / 'manifest.json').read_text())
        assert manifest['schema_version'] == 71 and 'wake_targets' not in manifest['tables']
        quarantine_restore(old)
        # Migrating the old database adds the tables; a second migrate changes nothing.
        old.migrate()
        old.migrate()
        with old.pool.connection() as c:
            assert schema_ready(c) is True and 'wake_targets' in database_manifest(c)


# spec: ANYM-848
def test_an_expired_subscription_is_neither_bound_nor_listed(gate):
    store = gate
    token = identity(store)
    sid, kept = subscription(store, token), subscription(store, token)
    register(store, token, [sid, kept])
    with gate.pool.connection() as c:
        # The deadline passed; no read has turned the stored state to expired yet.
        c.execute("UPDATE subscriptions SET expires_at=clock_timestamp()-interval '1 minute' WHERE id=%s", (sid,))
        assert c.execute('SELECT state FROM subscriptions WHERE id=%s', (sid,)).fetchone()['state'] == 'active'
    assert store.list_wake_targets(token)['items'][0]['subscription_ids'] == [kept]
    failure = refusal(lambda: register(store, token, [sid], thread=OTHER_THREAD))
    assert failure.code == 'not_found' and failure.field_errors == [{'field': 'subscription_ids', 'reason': 'format',
        'expected': "this key's own active subscription UUIDs"}]


# spec: ANYM-848
def test_the_operator_lists_disables_and_enables_through_the_cli(gate, tmp_path, monkeypatch, capsys):
    import messaging_service.__main__ as cli
    store = gate
    token = identity(store)
    first = register(store, token, [subscription(store, token)], label='chat one')
    second = register(store, token, [subscription(store, token)], label='chat two')
    store.confirm_wake_target(token, second['wake_target_id'], CODE)
    dsn = tmp_path / 'test.dsn'
    dsn.write_text(store.pool.conninfo)
    dsn.chmod(0o600)

    class BorrowedStore:
        def __getattr__(self, name):
            return getattr(store, name)

        def close(self):
            pass  # The seeded fixture owns this connection pool.
    monkeypatch.setattr(cli, 'Store', lambda *_: BorrowedStore())
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn))

    def run(*argv):
        monkeypatch.setattr(sys, 'argv', ['anymsg', *argv])
        cli.main()
        return json.loads(capsys.readouterr().out)
    run('disable-wake-target', '--id', first['wake_target_id'])
    listed = run('wake-targets-list')
    assert listed['targets_by_state'] == {'enabled': 1, 'operator_disabled': 1} and listed['unbound'] == 0
    rows = {item['label']: item for item in listed['items']}
    assert rows['chat one']['disabled_reason'] == 'operator' and rows['chat two']['confirmed'] is True
    assert 'confirmation' not in rows['chat two'] and CODE not in json.dumps(listed)
    assert {rows['chat one']['principal'], rows['chat one']['thread']} == {'wake-agent', THREAD}
    assert run('enable-wake-target', '--id', first['wake_target_id'])['state'] == 'enabled'
    run('disable-wake-target', '--id', first['wake_target_id'])
    run('disable-wake-target', '--id', second['wake_target_id'])
    assert run('enable-wake-target', '--all', '--principal', 'wake-agent') == {'enabled': 2}
    assert run('wake-targets-list', '--principal', 'wake-agent')['targets_by_state'] == {'enabled': 2}
