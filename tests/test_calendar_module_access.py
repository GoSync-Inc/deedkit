"""Calendar events reach a key only while its module permission holds (ANYM-577); synthetic only.

The calendar service journals `module.access.changed` (stamped scope `calendar:<ref>`, subject the calendar principal) and
messaging reads entitlement from that journal alone: no mirror table, no remote call. The race tests pause a holder at a real
database barrier after its authorization, start the revocation (an access:none event published by another thread) and assert
that the revocation stays pending until the holder's effect commits, then that every later use is refused.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

from psycopg import sql
import pytest

from messaging_service import module_events
from messaging_service.event_delivery import dispatch_hints
from messaging_service.module_events import ModuleContractError, validate_event
from messaging_service.store import APIError
from messaging_service.subscriptions import match_subscriptions
from test_module_events import beat, bound, empty_module_journal, module_hints, publish
from test_module_links import key

PRINCIPAL = 'cal-principal'


@pytest.fixture()
def modules(seeded):
    """An empty module journal and no dispatcher heartbeat (as test_module_links' fixture)."""
    empty_module_journal(seeded[0])
    return seeded
KINDS = ['calendar.event.created', 'calendar.event.changed']
LOST = ('permission_removed', 'principal_removed', 'member_removed', 'connection_disabled', 'visibility_reduced')


class Seq:
    """Producer seqs: unique per test (the module journal keeps ids)."""
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return self.n


def event(seq, calendar='cal-1'):
    ref = f'event-{calendar}-{seq}'
    return {'specversion': '1.0', 'id': f'calendar:{seq}', 'source': 'deedkit:calendar',
        'type': 'calendar.event.created', 'subject': ref, 'time': '2026-11-12T10:00:00Z',
        'data': {'calendar_ref': calendar, 'event_ref': ref, 'start_at': '2026-11-20T10:00:00Z', 'version': 1},
        'urgency': 'batch', 'expires_at': None}


def access(seq, principal=PRINCIPAL, state='details', reason='granted', until=None, **changes):
    data = {'principal_ref': principal, 'access': state, 'reason': reason, **({'until': until} if until else {})}
    return {'specversion': '1.0', 'id': f'calendar:{seq}', 'source': 'deedkit:calendar',
        'type': 'module.access.changed', 'subject': principal, 'time': '2026-11-12T10:00:00Z', 'data': data,
        'urgency': 'now', 'expires_at': None, **changes}


def put(store, ev, calendar='cal-1'):
    """Publish with the scope the calendar service stamps: the event's own calendar (an access event names it here)."""
    return publish(store, ev, scope=f"calendar:{ev['data'].get('calendar_ref', calendar)}")


def grant(store, seq, calendar='cal-1', **kw):
    return put(store, access(seq(), **kw), calendar)


def calendar_sub(store, token, *scopes, types=KINDS):
    return store.create_subscription(token, ['telegram-demo-chat'],
        module_sources=[{'scope': scope, 'types': list(types)} for scope in scopes])['subscription_id']


def reader(store, linked=True):
    token = key(store, 'module-reader')
    if linked:
        store.link_module('module-reader', 'calendar', PRINCIPAL)
    return token


def seen(store, token, sid):
    return [item['module_event_id'] for item in store.fetch_subscription_events(token, sid)['module']['items']]


def until(condition, what, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.02)
    pytest.fail(what)


def waiting(store, kind):
    with store.pool.connection() as c:
        return c.execute('SELECT count(*) AS n FROM pg_locks WHERE NOT granted AND locktype=%s', (kind,)).fetchone()['n']


def losses(store):
    with store.pool.connection() as c:
        return c.execute("SELECT count(*) AS n FROM module_events WHERE type='module.access.changed' "
            "AND data->>'access'='none'").fetchone()['n']


# spec: ANYM-577
def test_calendar_sources_need_the_link_and_the_linked_principals_access_now(modules):
    store, seq = modules[0], Seq()
    token = reader(store, linked=False)
    with pytest.raises(APIError) as refused:
        calendar_sub(store, token, 'calendar:all')
    assert str(refused.value) == 'module_not_linked' and 'link calendar for this key' in refused.value.next
    store.link_module('module-reader', 'calendar', PRINCIPAL)
    calendar_sub(store, token, 'calendar:all')
    for scope in ('calendar:cal-1',):
        with pytest.raises(APIError, match='module_not_linked'):  # no access event at all
            calendar_sub(store, token, scope)
    grant(store, seq, principal='someone-else')  # another principal's entitlement never counts
    with pytest.raises(APIError, match='module_not_linked'):
        calendar_sub(store, token, 'calendar:cal-1')
    grant(store, seq, 'cal-2', state='details', until='2020-01-01T00:00:00Z')  # principal expiry has passed
    with pytest.raises(APIError, match='module_not_linked'):
        calendar_sub(store, token, 'calendar:cal-2')
    grant(store, seq)
    assert calendar_sub(store, token, 'calendar:cal-1')
    # Only calendar scopes with calendar types: never the access control events, never another module's types.
    for scopes, types in ((['calendar:'], KINDS), (['cal-1'], KINDS), (['calendar:all', 'calendar:all'], KINDS),
            (['calendar:all'], ['module.access.changed']), (['calendar:all'], ['call.finished']),
            (['calendar:all'], [])):
        with pytest.raises(APIError, match='invalid_filter'):
            calendar_sub(store, token, *scopes, types=types)


# spec: ANYM-566
def test_capabilities_name_calendar_kinds_only_to_a_key_with_a_live_calendar_link_and_exactly_the_kinds_it_can_use(modules):
    store = modules[0]
    token = key(store, 'module-reader')
    full = lambda: json.dumps(store.capabilities(token, detail='full'))  # noqa: E731
    assert 'calendar.event' not in full()
    store.link_module('module-reader', 'calendar', PRINCIPAL)
    block = store.capabilities(token, sections=['subscriptions'])['subscriptions']['module_events']
    kinds = block['calendar']['kinds']
    assert kinds == sorted(kinds) and 'calendar.event.starting' in kinds
    assert block == {'calendar': {'source': 'deedkit:calendar', 'scope': 'calendar:all | calendar:<calendar_ref>',
        'kinds': kinds, 'create': 'create_subscription', 'argument': 'module_sources',
        'consume': 'fetch_subscription_events', 'refused_without_link': 'module_not_linked'}}
    assert 'call.finished' not in full()  # no calls link: the calls kinds stay unnamed
    # Exactly the kinds a subscription admits; never the control event or another module's kinds.
    for kind in module_events.KINDS:
        if kind in kinds:
            assert calendar_sub(store, token, 'calendar:all', types=(kind,))
        else:
            with pytest.raises(APIError, match='invalid_filter'):
                calendar_sub(store, token, 'calendar:all', types=(kind,))
    assert not any(kind.startswith(('call.', 'recording.', 'module.', 'feedback.')) for kind in kinds)
    store.link_module('module-reader', 'calls', 'owner-a')  # both links: both blocks
    assert sorted(store.capabilities(token, sections=['subscriptions'])['subscriptions']['module_events']) == [
        'calendar', 'calls']
    assert 'module_events' not in store.capabilities(key(store, 'other-reader'), sections=['subscriptions'])['subscriptions']
    store.unlink_module('module-reader', 'calendar')
    assert sorted(store.capabilities(token, sections=['subscriptions'])['subscriptions']['module_events']) == ['calls']


# spec: ANYM-577
def test_all_calendars_selects_only_the_calendars_the_principal_can_read_at_match_time(modules):
    store, seq = modules[0], Seq()
    token = reader(store)
    before = put(store, event(seq()))  # before the grant: never visible
    grant(store, seq)
    grant(store, seq, 'cal-2', principal='someone-else')  # a forged entitlement of another principal
    sid = calendar_sub(store, token, 'calendar:all')
    one, two, three = put(store, event(seq())), put(store, event(seq(), 'cal-2')), put(store, event(seq(), 'cal-3'))
    match_subscriptions(store)
    assert seen(store, token, sid) == [one] and before < one and two < three
    # Access to a second calendar follows at once for `all`, and only from its grant on.
    old = put(store, event(seq(), 'cal-4'))
    grant(store, seq, 'cal-4')
    new = put(store, event(seq(), 'cal-4'))
    match_subscriptions(store)
    page = store.fetch_subscription_events(token, sid)
    assert [item['module_event_id'] for item in page['module']['items']] == [one, new] and old not in {one, new}
    # A source for one exact calendar never reads another.
    exact = calendar_sub(store, token, 'calendar:cal-1')
    other = put(store, event(seq(), 'cal-4'))
    match_subscriptions(store)
    assert other not in seen(store, token, exact)


# spec: ANYM-577
@pytest.mark.parametrize('reason', LOST)
def test_a_lost_calendar_permission_hides_queued_and_new_events_and_cancels_the_hint(modules, tmp_path, reason):
    store, seq = modules[0], Seq()
    token = reader(store)
    grant(store, seq)
    sid = calendar_sub(store, token, 'calendar:cal-1')
    bound(store, token, sid, tmp_path)
    beat(store)
    queued = put(store, event(seq()))
    match_subscriptions(store)
    grant(store, seq, state='none', reason=reason)  # busy-only, expiry, membership, grant, binding, connection: all `none`
    sent = []
    dispatch_hints(store, transport=lambda *a, **k: sent.append(1) or 204, resolver=lambda *a, **k: ['93.184.216.34'])
    assert sent == [] and [h['error_code'] for h in module_hints(store, sid)] == ['module_not_linked']
    later = put(store, event(seq()))
    match_subscriptions(store)
    assert seen(store, token, sid) == [] and queued < later
    assert [(row['scope_ref'], row['state']) for row in store.get_subscription(token, sid)['module_stream']['sources']
        ] == [('calendar:cal-1', 'access_changed')]
    # The subscription itself stays active: only the calendar source lost its entitlement.
    assert store.fetch_subscription_events(token, sid)['subscription']['state'] == 'active'


# spec: ANYM-577
def test_a_regrant_never_revives_what_was_journaled_before_the_loss_and_never_a_pre_loss_receipt(modules):
    store, seq = modules[0], Seq()
    token = reader(store)
    grant(store, seq)
    sid = calendar_sub(store, token, 'calendar:all')
    old = put(store, event(seq()))
    match_subscriptions(store)
    page = store.fetch_subscription_events(token, sid)
    assert [item['module_event_id'] for item in page['module']['items']] == [old]
    grant(store, seq, state='none', reason='permission_removed')
    grant(store, seq)  # regranted
    new = put(store, event(seq()))
    match_subscriptions(store)
    with pytest.raises(APIError, match='invalid_receipt'):  # the receipt issued before the loss is dead
        store.ack_subscription_events(token, sid, page['receipt'], page['ack_version'])
    assert seen(store, token, sid) == [new] and old < new
    assert store.get_subscription_events(token, sid, module_after=0)['module']['items'][0]['module_event_id'] == new


# spec: ANYM-577
def test_a_calendar_link_revocation_or_change_ends_its_sources_and_a_regrant_needs_a_new_subscription(modules):
    store, seq = modules[0], Seq()
    token = reader(store)
    grant(store, seq)
    sid = calendar_sub(store, token, 'calendar:all')
    store.unlink_module('module-reader', 'calendar')
    store.link_module('module-reader', 'calendar', PRINCIPAL)
    old = put(store, event(seq()))
    match_subscriptions(store)
    assert seen(store, token, sid) == []
    fresh = calendar_sub(store, token, 'calendar:all')
    new = put(store, event(seq()))
    match_subscriptions(store)
    assert seen(store, token, fresh) == [new] and old < new
    # Linking another calendar principal moves the generation too: the entitlement is never the old principal's.
    store.link_module('module-reader', 'calendar', 'another-principal')
    after = put(store, event(seq()))
    match_subscriptions(store)
    assert seen(store, token, fresh) == [] and after > new


# spec: ANYM-577
def test_the_access_event_is_a_calendar_control_event_with_a_closed_contract():
    seq = Seq()
    assert validate_event(access(seq())) > 0
    assert validate_event(access(seq(), state='none', reason='member_removed', until='2026-12-01T00:00:00Z')) > 0
    for bad, code in ((access(seq(), source='deedkit:calls', id='calls:5'), 'invalid_type'),
            (access(seq(), urgency='batch'), 'access_changed_urgency_or_expiry'),
            (access(seq(), expires_at='2026-12-01T00:00:00Z'), 'access_changed_urgency_or_expiry'),
            (access(seq(), reason='because'), 'data_value_out_of_bounds'),
            (access(seq(), state='busy_only'), 'data_value_out_of_bounds'),
            (access(seq(), subject='another'), 'invalid_envelope'),
            ({**access(seq()), 'data': {'principal_ref': PRINCIPAL, 'access': 'details'}}, 'data_key_missing'),
            ({**access(seq()), 'data': {**access(seq())['data'], 'label': 'x'}}, 'data_key_not_allowed')):
        with pytest.raises(ModuleContractError, match=code):
            validate_event(bad)


# spec: ANYM-577
def test_the_database_admits_the_access_event_only_from_the_calendar_source(modules):
    store, seq = modules[0], Seq()
    put(store, access(seq()))
    with store.pool.connection() as c:
        valid = c.execute("SELECT public.module_event_valid('calls:77','deedkit:calls','module.access.changed','p',"
            "'2026-11-12T10:00:00Z','{\"principal_ref\":\"p\",\"access\":\"none\",\"reason\":\"expired\"}'::jsonb,"
            "'now',NULL) AS ok").fetchone()['ok']
        assert valid is False


def hold_receipts(store, ctl):
    """Hold every receipt write: reads and row locks still pass, so a holder stops after its authorization and before
    its effect (the receipt insert of a fetch, the ack result of an ACK)."""
    ctl.execute('LOCK TABLE subscription_receipts IN SHARE MODE')


def prepared(store, seq, scope='calendar:cal-1'):
    token = reader(store)
    grant(store, seq)
    sid = calendar_sub(store, token, scope)
    ref = put(store, event(seq()))
    match_subscriptions(store)
    return token, sid, ref


def revoke(store, seq):
    return grant(store, seq, state='none', reason='permission_removed')


# spec: ANYM-577
def test_a_fetch_paused_after_its_authorization_completes_before_a_revocation_and_the_next_use_is_refused(modules):
    store, seq = modules[0], Seq()
    token, sid, ref = prepared(store, seq)
    with ThreadPoolExecutor(2) as pool, store.pool.connection() as ctl, ctl.transaction():
        hold_receipts(store, ctl)
        holder = pool.submit(store.fetch_subscription_events, token, sid)
        until(lambda: waiting(store, 'relation') >= 1, 'the fetch never reached its receipt write')
        revoker = pool.submit(revoke, store, seq)
        until(lambda: waiting(store, 'advisory') >= 1, 'the revocation never waited for the use')
        # Explicit pending: neither the fetch's effect nor the revocation has committed.
        assert not holder.done() and not revoker.done() and losses(store) == 0
    page, revoked = holder.result(15), revoker.result(15)
    assert [item['module_event_id'] for item in page['module']['items']] == [ref]  # the effect completed...
    with store.pool.connection() as c:  # ...before the revocation did: the receipt was issued before the event
        order = c.execute("SELECT (SELECT max(expires_at)-interval '5 minutes' FROM subscription_receipts) <= "
            "(SELECT published_at FROM module_events WHERE seq=%s) AS ok", (revoked,)).fetchone()['ok']
    assert order and losses(store) == 1
    with pytest.raises(APIError, match='invalid_receipt'):
        store.ack_subscription_events(token, sid, page['receipt'], page['ack_version'])
    assert seen(store, token, sid) == []


# spec: ANYM-577
def test_an_ack_paused_after_its_authorization_commits_before_the_revocation_and_a_pre_loss_receipt_never_acks_after(modules):
    store, seq = modules[0], Seq()
    token, sid, ref = prepared(store, seq)
    first = store.fetch_subscription_events(token, sid)
    with ThreadPoolExecutor(2) as pool, store.pool.connection() as ctl, ctl.transaction():
        hold_receipts(store, ctl)
        holder = pool.submit(store.ack_subscription_events, token, sid, first['receipt'], first['ack_version'])
        until(lambda: waiting(store, 'relation') >= 1, 'the ACK never reached its result write')
        revoker = pool.submit(revoke, store, seq)
        until(lambda: waiting(store, 'advisory') >= 1, 'the revocation never waited for the ACK')
        assert not holder.done() and not revoker.done() and losses(store) == 0
    acked, _ = holder.result(15), revoker.result(15)
    assert acked['module_acked_through'] == ref and losses(store) == 1
    # After the regrant only what follows it is offered; the acked reference stays behind the stream's position.
    grant(store, seq)
    again = put(store, event(seq()))
    match_subscriptions(store)
    second = store.fetch_subscription_events(token, sid)
    assert [item['module_event_id'] for item in second['module']['items']] == [again] and again > ref
    assert store.ack_subscription_events(token, sid, second['receipt'], second['ack_version'])[
        'module_acked_through'] == again


# spec: ANYM-577
def test_a_webhook_in_flight_completes_before_a_revocation_and_the_next_delivery_is_refused(modules, tmp_path):
    store, seq = modules[0], Seq()
    token = reader(store)
    grant(store, seq)
    sid = calendar_sub(store, token, 'calendar:cal-1')
    bound(store, token, sid, tmp_path)
    beat(store)
    ref = put(store, event(seq()))
    match_subscriptions(store)
    reached, release, bytes_out = threading.Event(), threading.Event(), []

    def transport(endpoint, body, headers, **k):  # the synthetic receiver: bytes arrive while the holder is paused
        bytes_out.append(json.loads(body))
        reached.set()
        assert release.wait(20), 'the receiver was never released'
        return 204
    with ThreadPoolExecutor(2) as pool:
        holder = pool.submit(dispatch_hints, store, transport=transport, resolver=lambda *a, **k: ['93.184.216.34'])
        assert reached.wait(15), 'the delivery never reached the receiver'
        revoker = pool.submit(revoke, store, seq)
        until(lambda: waiting(store, 'advisory') >= 1, 'the revocation never waited for the delivery')
        assert not holder.done() and not revoker.done() and losses(store) == 0 and len(bytes_out) == 1
        release.set()
        stats, revoked = holder.result(15), revoker.result(15)
    assert stats['delivered'] == 1 and revoked > ref and losses(store) == 1
    assert [h['state'] for h in module_hints(store, sid)] == ['delivered']
    # Whatever follows the loss is refused: a hint queued after it is cancelled, its bytes never leave.
    put(store, event(seq()))
    match_subscriptions(store)
    out = []
    dispatch_hints(store, transport=lambda *a, **k: out.append(1) or 204, resolver=lambda *a, **k: ['93.184.216.34'])
    assert out == [] and seen(store, token, sid) == []


# spec: ANYM-577
def test_a_crashed_holder_never_blocks_the_revocation_and_its_effect_never_commits(modules):
    store, seq = modules[0], Seq()
    token, sid, ref = prepared(store, seq)
    with ThreadPoolExecutor(2) as pool, store.pool.connection() as ctl, ctl.transaction():
        hold_receipts(store, ctl)
        holder = pool.submit(store.fetch_subscription_events, token, sid)
        until(lambda: waiting(store, 'relation') >= 1, 'the fetch never reached its receipt write')
        revoker = pool.submit(revoke, store, seq)
        until(lambda: waiting(store, 'advisory') >= 1, 'the revocation never waited for the use')
        pid = ctl.execute("SELECT pid FROM pg_locks WHERE NOT granted AND locktype='relation' LIMIT 1").fetchone()['pid']
        ctl.execute(sql.SQL('SELECT pg_terminate_backend({})').format(pid))  # the holder dies, polling or not
        with pytest.raises(Exception):
            holder.result(15)
        # No lock outlives its holder: the revocation passes the access fence and now waits only for this barrier.
        until(lambda: waiting(store, 'advisory') == 0 and waiting(store, 'relation') >= 1,
            'the revocation stayed behind a dead holder')
        assert not revoker.done()
    revoked = revoker.result(15)
    assert revoked > 0 and losses(store) == 1
    with store.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM subscription_receipts').fetchone()['n'] == 0
    assert seen(store, token, sid) == []


# spec: ANYM-577
def test_a_late_resumed_holder_with_an_expired_receipt_is_refused_even_before_the_loss_is_known(modules):
    store, seq = modules[0], Seq()
    token, sid, ref = prepared(store, seq)
    page = store.fetch_subscription_events(token, sid)
    with store.pool.connection() as c:  # its lease ran out while the holder slept
        c.execute("UPDATE subscription_receipts SET expires_at=clock_timestamp()-interval '1 second'")
    with pytest.raises(APIError, match='invalid_receipt'):
        store.ack_subscription_events(token, sid, page['receipt'], page['ack_version'])
    # A fresh fetch (no revocation yet) still works and its receipt acks: always refusing would not be a proof.
    fresh = store.fetch_subscription_events(token, sid)
    assert [item['module_event_id'] for item in fresh['module']['items']] == [ref]
    assert store.ack_subscription_events(token, sid, fresh['receipt'], fresh['ack_version'])['module_acked_through'] == ref
    revoke(store, seq)
    put(store, event(seq()))
    match_subscriptions(store)
    assert seen(store, token, sid) == []
