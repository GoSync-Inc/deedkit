"""Live subscription capacity is independent of retained history; synthetic PostgreSQL only."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from messaging_service.store import APIError
from messaging_service.subscriptions import match_subscriptions
from test_subscriptions import create, emit, key


def paused(store, token):
    row = create(store, token)
    return store.update_subscription(token, row['subscription_id'], row['version'], action='pause')


# spec: ANYM-183
def test_default_capacity_allows_one_hundred_active_subscriptions(seeded, monkeypatch):
    store, _, _ = seeded
    token = key(store)
    monkeypatch.delenv('SUBSCRIPTION_MAX_DEFINITIONS', raising=False)
    for _ in range(100):
        assert create(store, token)['state'] == 'active'
    with pytest.raises(APIError, match='subscription_limit') as error:
        create(store, token)
    assert error.value.status == 429
    assert len(store.list_subscriptions(token, limit=100)['items']) == 100


# spec: ANYM-183
@pytest.mark.parametrize('state', ['paused', 'closed', 'expired', 'access_changed'])
def test_inactive_definitions_free_capacity_without_losing_history(seeded, monkeypatch, state):
    store, _, _ = seeded
    token = key(store)
    monkeypatch.setenv('SUBSCRIPTION_MAX_DEFINITIONS', '1')
    old = create(store, token)
    sid = old['subscription_id']
    event = emit(store)
    match_subscriptions(store)
    if state in ('paused', 'closed'):
        store.update_subscription(token, sid, old['version'], action={'paused': 'pause', 'closed': 'close'}[state])
    else:
        with store.pool.connection() as c:
            c.execute('UPDATE subscriptions SET state=%s,version=version+1 WHERE id=%s', (state, sid))
    assert create(store, token)['state'] == 'active'
    view = store.get_subscription(token, sid)
    assert view['state'] == state
    page = store.get_subscription_events(token, sid)
    if state == 'access_changed':
        assert view['scope_masked'] and page['items'] == []
    else:
        assert [item['event_id'] for item in page['items']] == [event]
    with store.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM subscription_matches WHERE subscription=%s',
            (sid,)).fetchone()['n'] == 1


# spec: ANYM-183
def test_elapsed_expiry_frees_capacity_before_the_worker_updates_state(seeded, monkeypatch):
    store, _, _ = seeded
    token = key(store)
    monkeypatch.setenv('SUBSCRIPTION_MAX_DEFINITIONS', '1')
    sid = create(store, token)['subscription_id']
    with store.pool.connection() as c:
        c.execute("UPDATE subscriptions SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (sid,))
    assert create(store, token)['state'] == 'active'
    with store.pool.connection() as c:
        assert c.execute('SELECT state FROM subscriptions WHERE id=%s', (sid,)).fetchone()['state'] == 'active'
    assert store.get_subscription(token, sid)['state'] == 'expired'


# spec: ANYM-183
@pytest.mark.parametrize('resume_policy', ['catch_up', 'skip'])
def test_resume_requires_capacity_and_preserves_state_after_a_refusal(seeded, monkeypatch, resume_policy):
    store, _, _ = seeded
    token = key(store)
    held = paused(store, token)
    active = create(store, token)
    monkeypatch.setenv('SUBSCRIPTION_MAX_DEFINITIONS', '1')
    event = emit(store)
    with pytest.raises(APIError, match='subscription_limit'):
        store.update_subscription(token, held['subscription_id'], held['version'], action='resume',
            resume_policy=resume_policy)
    current = store.get_subscription(token, held['subscription_id'])
    assert (current['state'], current['version'], current['matched_through'], current['gap']) == (
        held['state'], held['version'], held['matched_through'], held['gap'])
    store.update_subscription(token, active['subscription_id'], active['version'], action='close')
    resumed = store.update_subscription(token, held['subscription_id'], held['version'], action='resume',
        resume_policy=resume_policy)
    assert (resumed['state'], resumed['version']) == ('active', held['version'] + 1)
    assert resumed['matched_through'] == (event if resume_policy == 'skip' else held['matched_through'])


# spec: ANYM-183
def test_pausing_frees_capacity_and_resuming_checks_the_current_version_first(seeded, monkeypatch):
    store, _, _ = seeded
    token = key(store)
    monkeypatch.setenv('SUBSCRIPTION_MAX_DEFINITIONS', '1')
    held = paused(store, token)
    replacement = create(store, token)
    with pytest.raises(APIError, match='subscription_conflict'):
        store.update_subscription(token, held['subscription_id'], held['version'] - 1, action='resume')
    with pytest.raises(APIError, match='subscription_limit'):
        store.update_subscription(token, held['subscription_id'], held['version'], action='resume')
    store.update_subscription(token, replacement['subscription_id'], replacement['version'], action='pause')
    assert store.update_subscription(token, held['subscription_id'], held['version'], action='resume')['state'] == 'active'


# spec: ANYM-183
@pytest.mark.parametrize('actions', [('create', 'create'), ('create', 'resume'), ('resume', 'resume')])
def test_concurrent_creates_and_resumes_share_the_same_capacity_lock(seeded, monkeypatch, actions):
    store, _, _ = seeded
    token = key(store)
    held = [paused(store, token) if action == 'resume' else None for action in actions]
    monkeypatch.setenv('SUBSCRIPTION_MAX_DEFINITIONS', '1')
    ready = threading.Barrier(2)

    def admit(index):
        ready.wait(timeout=10)
        try:
            if actions[index] == 'create':
                return create(store, token)['state']
            row = held[index]
            return store.update_subscription(token, row['subscription_id'], row['version'], action='resume')['state']
        except APIError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(admit, index) for index in range(2)]
        assert sorted(future.result(timeout=15) for future in futures) == ['active', 'subscription_limit']
    assert sum(row['state'] == 'active' for row in store.list_subscriptions(token)['items']) == 1


# spec: ANYM-183
def test_capacity_is_independent_for_each_key_of_the_same_principal(seeded, monkeypatch):
    store, _, _ = seeded
    tokens = [key(store), key(store)]
    monkeypatch.setenv('SUBSCRIPTION_MAX_DEFINITIONS', '1')
    for token in tokens:
        assert create(store, token)['state'] == 'active'
        with pytest.raises(APIError, match='subscription_limit'):
            create(store, token)


# spec: ANYM-183
@pytest.mark.parametrize('value', ['0', '1001', '-1', 'invalid'])
def test_invalid_capacity_refuses_admission_without_mutating_paused_subscriptions(seeded, monkeypatch, value):
    store, _, _ = seeded
    token = key(store)
    held = paused(store, token)
    monkeypatch.setenv('SUBSCRIPTION_MAX_DEFINITIONS', value)
    with pytest.raises(ValueError, match='invalid_subscription_setting'):
        create(store, token)
    with pytest.raises(ValueError, match='invalid_subscription_setting'):
        store.update_subscription(token, held['subscription_id'], held['version'], action='resume')
    assert store.get_subscription(token, held['subscription_id'])['state'] == 'paused'
