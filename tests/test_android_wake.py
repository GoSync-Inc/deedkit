"""Android's own outbox; disposable DB/fake egress only. No transport subscription."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from uuid import uuid4

import pytest

from messaging_service import android_wake as aw
from messaging_service.store import APIError

REAL_READY = aw.ready


@pytest.fixture
def world(seeded, monkeypatch):
    store = seeded[0]
    permissions = ['read', 'delivery-write', 'subscription-write']
    token = store.issue_key('android-owner', ['telegram-demo'], permissions=permissions,
                            key_permissions=permissions)
    with store.authorized(token) as (c, p):
        actor = dict(p)
        endpoint = uuid4()
        c.execute('''INSERT INTO delivery_endpoints(id,principal,key_id,request_id,request_hash,url,host,signing_secret,state)
            VALUES(%s,%s,%s,'synthetic-request',%s,'https://example.com/wake','example.com',%s,'enabled')''',
            (endpoint, p['id'], p['key_id'], 'a'*64, 'whsec_eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg='))
        c.execute('''INSERT INTO android_wake_bindings
            (principal,key_id,device_id,owner_id,source_id,auth_epoch,endpoint,state,routine_verified,
             installed_verified,valid_until) VALUES(%s,%s,'phone','android-owner','native',1,%s,'active',true,true,
             clock_timestamp()+interval '1 day')''', (p['id'], p['key_id'], endpoint))
    monkeypatch.setattr(aw, 'ready', lambda c, **kwargs: True)
    monkeypatch.setattr(aw.endpoints, 'prepare', lambda *args: (('https://example.com/wake', ['synthetic']), None))

    def fenced(c, p, ident, target, **kwargs):
        row = c.execute('SELECT * FROM delivery_endpoints WHERE id=%s', (ident,)).fetchone()
        if not p or row['state'] != 'enabled' or row['principal'] != p['id'] or row['key_id'] != p['key_id']:
            return None, 'access_or_configuration_changed'
        return dict(row) | {'at_most_once': True, 'vendor': 'grok-bot-routine'}, None

    monkeypatch.setattr(aw.endpoints, 'fenced', fenced)
    yield store, token, actor, endpoint


def event(**changes):
    return dict(wake_id=str(uuid4()), task_id='original-task', request_id='original-request', order_id=None, owner_id='android-owner',
                device_id='phone', source_id='native', auth_epoch=1, kind='question',
                native_thread_id='native-thread', native_turn_id='turn', native_call_id=None, item_id='item', message_id='message',
                native_request_id=None, question_ids=[],
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()) | changes


def stage(world, item, *, epoch='epoch-one', after=0, sequence=1):
    store, token, *_ = world
    return aw.call(store, token, 'stage', dict(device_id='phone', journal_id=epoch, after=after,
        next_after=sequence, events=[item | {'sequence': sequence}]))


def rows(world):
    with world[0].pool.connection() as c:
        return c.execute('SELECT * FROM android_wake_intents ORDER BY created_at').fetchall()


# spec: ANYM-997
def test_same_original_wake_survives_lost_reply_epoch_and_overlapping_gateway_generations(world):
    item = event()
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: stage(world, item), range(8)))
    assert len(rows(world)) == 1
    assert len({r['items'][0]['wake_id'] for r in receipts}) == 1
    original = aw.call(world[0], world[1], 'receipt', {'wake_id': item['wake_id']})
    assert original['identity']['order_id'] is None
    assert original['identity']['native_request_id'] is None
    assert original['identity']['question_ids'] == []
    assert stage(world, item, epoch='restored-epoch')['items'][0]['wake_id'] == original['wake_id']
    assert len(rows(world)) == 1


# spec: ANYM-997
def test_changed_payload_rebind_and_caller_override_cannot_create_another_attempt(world):
    item = event(native_request_id=42, question_ids=['q1', 'q2'])
    stage(world, item)
    with pytest.raises(APIError, match='android_wake_identity_conflict'):
        stage(world, item | {'native_request_id': '42'})
    with world[0].pool.connection() as c:
        c.execute('UPDATE android_wake_bindings SET generation=generation+1')
    with pytest.raises(APIError, match='android_wake_binding_changed'):
        stage(world, item)
    with pytest.raises(APIError):
        aw.call(world[0], world[1], 'stage', dict(device_id='phone', endpoint_id=str(world[3])))
    assert len(rows(world)) == 1


# spec: ANYM-997
def test_ingest_cursor_and_intent_roll_back_together_without_any_subscription(world):
    good, bad = event(), event(owner_id='other')
    with pytest.raises(APIError):
        aw.call(world[0], world[1], 'stage', dict(device_id='phone', journal_id='one', after=0, next_after=2,
            events=[good | {'sequence': 1}, bad | {'sequence': 2}]))
    assert rows(world) == []
    state = aw.call(world[0], world[1], 'state', {'device_id': 'phone'})
    assert state['after'] == 0
    with world[0].pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM subscriptions WHERE principal=%s',
                         (world[2]['id'],)).fetchone()['n'] == 0


# spec: ANYM-997
@pytest.mark.parametrize('change', [{'state': 'paused'}, {'routine_verified': False}, {'installed_verified': False},
                                   {'state': 'revoked'}])
def test_missing_unverified_or_revoked_binding_is_unavailable_before_external_write(world, change):
    with world[0].pool.connection() as c:
        key, value = next(iter(change.items()))
        c.execute('UPDATE android_wake_bindings SET ' + key + '=%s', (value,))
    with pytest.raises(APIError, match='android_wake_unavailable'):
        stage(world, event())
    assert rows(world) == []


# spec: ANYM-997
def test_external_acceptance_lost_reply_and_restart_never_resend_or_ack_native(world):
    item = event()
    stage(world, item)
    attempts = []

    def accepted_lost(*args, **kwargs):
        attempts.append(json.loads(args[1]))
        raise TimeoutError('synthetic lost HTTP reply')

    aw.dispatch(world[0], transport=accepted_lost, resolver=lambda *args: [])
    aw.dispatch(world[0], transport=accepted_lost, resolver=lambda *args: [])
    result = aw.call(world[0], world[1], 'receipt', {'wake_id': item['wake_id']})
    assert len(attempts) == 1
    assert result['transport_state'] == 'unknown'
    assert result['native_status'] == 'not_observed'
    assert attempts[0] == {'module': 'android', 'type': 'android_wake', 'wake_id': item['wake_id'],
                           'kind': 'question', 'next_call': 'android_agent'}


# spec: ANYM-997
def test_sending_crash_expiry_access_change_and_older_readiness_stop_before_post(world, monkeypatch):
    item = event()
    stage(world, item)
    with world[0].pool.connection() as c:
        c.execute("UPDATE android_wake_intents SET state='sending',lease_until=clock_timestamp()-interval '1 second'")
    attempts = []
    aw.dispatch(world[0], transport=lambda *a, **kw: attempts.append(a), resolver=lambda *a: [])
    assert rows(world)[0]['state'] == 'unknown' and not attempts
    other = event(item_id='second-item', message_id='second-message')
    stage(world, other, after=1, sequence=2)
    monkeypatch.setattr(aw, 'ready', lambda *a, **kw: False)
    aw.dispatch(world[0], transport=lambda *a, **kw: attempts.append(a), resolver=lambda *a: [])
    assert not attempts
    with world[0].pool.connection() as c:
        c.execute('UPDATE api_keys SET revoked=true WHERE id=%s', (world[2]['key_id'],))
    monkeypatch.setattr(aw, 'ready', lambda *a, **kw: True)
    aw.dispatch(world[0], transport=lambda *a, **kw: attempts.append(a), resolver=lambda *a: [])
    assert not attempts


# spec: ANYM-997
def test_metadata_schema_rejects_raw_question_answer_or_secret_fields(world):
    for field in ('questions', 'answer', 'owner_words', 'secret'):
        with pytest.raises(APIError, match='invalid_android_wake'):
            stage(world, event(**{field: 'SYNTHETIC_PROTECTED_VALUE'}))
    assert rows(world) == []


# spec: ANYM-997
def test_unknown_original_question_or_control_call_cannot_be_reissued_under_a_new_wake_id(world):
    question = event()
    stage(world, question)
    aw.dispatch(world[0], transport=lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()), resolver=lambda *a: [])
    with pytest.raises(APIError, match='android_wake_origin_conflict'):
        stage(world, question | {'wake_id': str(uuid4())}, after=1, sequence=2)
    control = event(kind='status', native_call_id='actual-control-call')
    stage(world, control, after=1, sequence=2)
    with pytest.raises(APIError, match='android_wake_origin_conflict'):
        stage(world, control | {'wake_id': str(uuid4())}, after=2, sequence=3)
    assert len(rows(world)) == 2


def old_hint(world, *, state='delivered', admitted=True):
    """A genuine synthetic message subscription, used only to prove shared lanes/budgets."""
    store, token, _, endpoint = world
    sid = store.create_subscription(token, ['telegram-demo-chat'])['subscription_id']
    with store.pool.connection() as c:
        return c.execute('''INSERT INTO subscription_outbox(subscription,subscription_version,endpoint,
            through_position,kind,state,admitted_at,lease_until) VALUES(%s,1,%s,0,'events',%s,
            CASE WHEN %s THEN clock_timestamp() END,clock_timestamp()+interval '1 minute') RETURNING id''',
            (sid, endpoint, state, admitted)).fetchone()['id']


# spec: ANYM-997
def test_shared_key_host_admission_and_queue_include_both_real_outboxes(world, monkeypatch):
    from messaging_service import delivery_policy as policy
    from messaging_service.event_delivery import endpoint_queue_full
    old_hint(world)
    # Creation and admission are separate limits: age creation while retaining one recent admission.
    with world[0].pool.connection() as c:
        c.execute("UPDATE subscription_outbox SET created_at=clock_timestamp()-interval '2 hours'")
    monkeypatch.setenv('SUBSCRIPTION_MAX_HINTS_PER_HOUR', '1')
    monkeypatch.setenv('DELIVERY_MAX_HINTS_PER_HOST_PER_HOUR', '1')
    stage(world, event())
    attempts = []
    assert aw.dispatch(world[0], transport=lambda *a, **kw: attempts.append(a) or 200,
                       resolver=lambda *a: []) == {'deferred': 1}
    assert not attempts
    with world[0].pool.connection() as c:
        c.execute("UPDATE subscription_outbox SET admitted_at=clock_timestamp()-interval '2 hours'")
        c.execute('UPDATE android_wake_intents SET next_attempt_at=clock_timestamp()')
    aw.dispatch(world[0], transport=lambda *a, **kw: attempts.append(a) or 200, resolver=lambda *a: [])
    assert len(attempts) == 1 and rows(world)[0]['state'] == 'accepted'
    with world[0].pool.connection() as c:
        owner = {'principal': world[2]['id'], 'key_id': world[2]['key_id']}
        now = datetime.now(timezone.utc)
        assert policy.admission_slot(c, owner, now) is not None
        assert policy.host_admission_slot(c, world[3], now) is not None
        # Accepted rows leave the queue; pending Android and message rows share its unchanged cap.
        c.execute("UPDATE android_wake_intents SET state='pending'")
        c.execute("UPDATE subscription_outbox SET state='pending'")
        assert endpoint_queue_full(c, owner, adding=31)
        assert not endpoint_queue_full(c, owner, adding=30)


# spec: ANYM-997
def test_endpoint_test_inflight_blocks_android_and_android_blocks_existing_scheduler(world):
    from messaging_service.event_delivery import next_endpoint_item
    old_hint(world, state='pending', admitted=False)
    stage(world, event())
    with world[0].pool.connection() as c:
        c.execute('''INSERT INTO delivery_endpoint_tests(endpoint,request_id,state,lease_until)
            VALUES(%s,'synthetic-endpoint-test','leased',clock_timestamp()+interval '1 minute')''', (world[3],))
    attempts = []
    assert aw.dispatch(world[0], transport=lambda *a, **kw: attempts.append(a), resolver=lambda *a: []) == {}
    assert not attempts
    with world[0].pool.connection() as c:
        c.execute("UPDATE delivery_endpoint_tests SET state='cancelled'")
        c.execute("UPDATE android_wake_intents SET state='sending',lease_until=clock_timestamp()+interval '1 minute'")
        assert next_endpoint_item(c, {}, 'hint', 'SELECT * FROM subscription_outbox WHERE id=%s FOR UPDATE') is None


# spec: ANYM-997
def test_concurrent_senders_reserve_one_durable_attempt_then_reconcile_without_resend(world):
    stage(world, event())
    started, release = threading.Event(), threading.Event()
    attempts = []
    def transport(*args, **kwargs):
        attempts.append(args)
        started.set()
        assert release.wait(5)
        return 200
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(aw.dispatch, world[0], transport=transport, resolver=lambda *a: [])
        assert started.wait(5)
        try:
            assert aw.dispatch(world[0], transport=transport, resolver=lambda *a: []) == {}
        finally:
            release.set()
        assert first.result(5) == {'delivered': 1}
    assert len(attempts) == 1
    aw.dispatch(world[0], transport=transport, resolver=lambda *a: [])
    assert len(attempts) == 1


# spec: ANYM-997
def test_new_table_role_counts_readlock_and_no_binding_or_identity_write(world):
    from test_db_role_isolation import install, remove, as_role, refused
    from messaging_service import delivery_policy as policy
    stage(world, event())
    store, _, actor, _ = world
    roles = []
    try:
        gateway = install(store, 'gateway_role.sql', 'messaging_gateway'); roles.append(gateway)
        delivery = install(store, 'delivery_role.sql', 'messaging_event_delivery'); roles.append(delivery)
        matcher = install(store, 'subscription_role.sql', 'messaging_subscriptions'); roles.append(matcher)
        for role in roles:
            with as_role(store, role) as c:
                owner = {'principal': actor['id'], 'key_id': actor['key_id']}
                assert policy.budget_slot(c, owner, datetime.now(timezone.utc)) is None
                assert policy.admission_slot(c, owner, datetime.now(timezone.utc)) is None
            refused(store, role, "UPDATE android_wake_bindings SET state='active'")
        refused(store, matcher, 'SELECT identity FROM android_wake_intents')
        refused(store, matcher, 'SELECT * FROM public.lock_android_wake_binding(%s,%s,%s)',
                (actor['id'], actor['key_id'], 'phone'))
        refused(store, delivery, 'UPDATE android_wake_bindings SET after_seq=999')
        refused(store, delivery, "UPDATE android_wake_intents SET identity='{}'")
        with as_role(store, delivery) as c:
            assert aw._binding(c, actor, 'phone')['device_id'] == 'phone'
            # A real competing DB transaction cannot rebind past this read-lock.
            with store.pool.connection() as other:
                other.execute("SET LOCAL lock_timeout='100ms'")
                import psycopg
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    other.execute('UPDATE android_wake_bindings SET generation=generation+1')
                other.rollback()
        with store.pool.connection() as c:
            c.execute('UPDATE android_wake_bindings SET generation=generation+1')
            c.execute('UPDATE android_wake_bindings SET generation=generation-1')
        # Exercise the actual existing sender role, not only owner-backed direct helper calls.
        from copy import copy
        from test_db_role_isolation import RolePool
        deployed = copy(store)
        deployed.pool = RolePool(store, delivery)
        attempts = []
        assert aw.dispatch(deployed, transport=lambda *a, **kw: attempts.append(a) or 200,
                           resolver=lambda *a: []) == {'delivered': 1}
        assert len(attempts) == 1
    finally:
        for role in reversed(roles): remove(store, role)


# spec: ANYM-997
def test_authenticated_private_route_and_readiness_never_register_public_sender(world, monkeypatch):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    monkeypatch.setattr(aw, 'ready', REAL_READY)
    from messaging_service import heartbeats
    heartbeats.record(world[0], 'dispatcher')
    with world[0].pool.connection() as c:
        c.execute("UPDATE component_heartbeats SET schema_version=96 WHERE component='dispatcher'")
    with TestClient(create_app(world[0], ['testserver'])) as client:
        path = '/v1/android-wakes/state'
        assert client.post(path, json={'device_id': 'phone'}).status_code == 401
        headers = {'Authorization': 'Bearer ' + world[1]}
        assert client.post(path, headers=headers, json={'device_id': 'phone'}).status_code == 200
        item = event()
        response = client.post('/v1/android-wakes/stage', headers=headers,
            json=dict(device_id='phone', journal_id='one', after=0, next_after=1,
                      events=[item | {'sequence': 1}]))
        assert response.status_code == 409 and response.json()['error'] == 'android_wake_not_ready'
    assert rows(world) == []


# spec: ANYM-997
def test_rebind_and_principal_revocation_wait_for_the_existing_send_fences(world):
    from messaging_service.policy import lock_principal
    import psycopg
    stage(world, event())
    started, release = threading.Event(), threading.Event()
    def transport(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return 200
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(aw.dispatch, world[0], transport=transport, resolver=lambda *a: [])
        assert started.wait(5)
        try:
            with world[0].pool.connection() as c:
                c.execute("SET LOCAL lock_timeout='100ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    c.execute('UPDATE android_wake_bindings SET generation=generation+1')
                c.rollback()
                c.execute("SET LOCAL lock_timeout='100ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    lock_principal(c, world[2]['id'])
                c.rollback()
        finally: release.set()
        assert pending.result(5) == {'delivered': 1}
    world[0].revoke(credential_id=str(world[2]['key_id']))
    assert aw.dispatch(world[0], transport=lambda *a, **kw: pytest.fail('no replay'), resolver=lambda *a: []) == {}


# spec: ANYM-997
def test_migration_and_backup_inventory_preserve_own_outbox_and_revoke_restored_public_function(world):
    from messaging_service import backup, migration_rollout
    from test_db_role_isolation import install, remove, refused
    source = Path(aw.__file__).with_name('migrations') / '097_android_wake_intents.sql'
    assert migration_rollout.header(source.read_text()) == migration_rollout.classify(source.read_text()) == 'manual'
    assert backup.V97_TABLES == {'android_wake_bindings': 'principal,key_id,device_id',
                                 'android_wake_intents': 'principal,key_id,wake_id'}
    assert 97 in backup.SCHEMAS
    role = install(world[0], 'subscription_role.sql', 'messaging_subscriptions')
    try:
        with world[0].pool.connection() as c:
            c.execute('GRANT EXECUTE ON FUNCTION public.lock_android_wake_binding(text,uuid,text) TO PUBLIC')
        world[0].migrate()
        refused(world[0], role, 'SELECT * FROM public.lock_android_wake_binding(%s,%s,%s)',
                (world[2]['id'], world[2]['key_id'], 'phone'))
    finally: remove(world[0], role)


# spec: ANYM-997
def test_real_snapshot_restore_keeps_unknown_original_wake_and_android_cursor(world, tmp_path):
    from messaging_service.backup import database_manifest, quarantine_restore
    from test_subscription_retention import snapshot_restored
    item = event(native_call_id='original-native-call')
    stage(world, item)
    with world[0].pool.connection() as c:
        c.execute("UPDATE android_wake_intents SET state='unknown',hold_reason='outcome_unknown',attempts=1")
        before = database_manifest(c)
    with snapshot_restored(world[0], tmp_path) as restored:
        with restored.pool.connection() as c:
            after = database_manifest(c)
            assert all(after[name] == before[name] for name in ('android_wake_bindings', 'android_wake_intents'))
            row = c.execute('SELECT * FROM android_wake_intents').fetchone()
            assert row['state'] == 'unknown' and row['attempts'] == 1 and str(row['wake_id']) == item['wake_id']
            assert c.execute('SELECT after_seq FROM android_wake_bindings').fetchone()['after_seq'] == 1
        quarantine_restore(restored)
        restored.migrate()
        assert aw.dispatch(restored, transport=lambda *a, **kw: pytest.fail('unknown restored wake replay'),
                           resolver=lambda *a: []) == {}
