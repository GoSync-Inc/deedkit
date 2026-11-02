"""ANYM-671: get_sync_status shows whether sending works and how long sends wait.
Synthetic rows on the disposable database; no provider is touched."""
import json

import pytest

from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState
from messaging_service.envelope import stable_id
from messaging_service.presentation import render
from messaging_service.store import APIError
from test_archive import event

ACCOUNTS = {'out-tg': ('telegram', '100'), 'out-wa': ('whatsapp', '100@c.us')}
CANARY = 'OUTBOUND_BODY_CANARY'


class Outbound:
    def __init__(self, store, archive, key, states):
        self.store, self.archive, self.key, self.states = store, archive, key, states
        self.counter = 0

    def send(self, account, count=1, text=CANARY):
        platform, chat = ACCOUNTS[account]
        ids = []
        for _ in range(count):
            self.counter += 1
            ids.append(self.store.send_message(self.key, account, stable_id('chat', account, chat), text,
                'outbound-request-%04d' % self.counter)['send_id'])
        return ids

    def sql(self, statement, *args):
        with self.store.pool.connection() as c:
            c.execute(statement, args)

    def wait(self, account, seconds):
        """Make every queued send of the account `seconds` older, keeping its ten-minute window."""
        self.sql("UPDATE outgoing_messages SET created_at=created_at-%s*interval '1 second',"
            "expires_at=expires_at-%s*interval '1 second' WHERE account=%s AND status='queued'", seconds, seconds, account)

    def store_send_ids(self, account):
        with self.store.pool.connection() as c:
            return [r['id'] for r in c.execute("SELECT id FROM outgoing_messages WHERE account=%s AND status='queued' "
                "ORDER BY created_at,id", (account,))]

    def expire(self, send_id):
        self.sql("UPDATE outgoing_messages SET expires_at=clock_timestamp()-interval '5 seconds' WHERE id=%s", send_id)

    def connection(self, account, state, code, retry_at=None):
        self.states[account][0].phase('connection', state, code)
        if retry_at:
            self.sql("UPDATE collector_health SET operations=jsonb_set(operations,'{connection,retry_at}',to_jsonb(%s::text)) "
                "WHERE account=%s", retry_at, account)

    def status(self, account=None, key=None):
        result = self.store.get_sync_status(key or self.key, refresh=True)
        if account is None:
            return result
        return next(source for source in result['sources'] if source['account'] == account)

    def outbound(self, account):
        return self.status(account)['outbound']


@pytest.fixture
def out(seeded):
    store, _, _ = seeded
    archive = Archive(store)
    states = {}
    for account, (platform, chat) in ACCOUNTS.items():
        archive.register(account, platform, 'Synthetic ' + account, discover=True)
        archive.ingest(event(account=account, platform=platform, chat=chat, provider_id='1'))
        epoch = archive.acquire(account, 'worker-' + account)
        state = CollectorState(archive, account, ('worker-' + account, epoch))
        state.phase('connection', 'connected', 'provider_ready')
        archive.set_sending(account, True)
        states[account] = (state, epoch)
    key = store.issue_key('out-agent', list(ACCOUNTS), permissions=['read', 'send'])
    yield Outbound(store, archive, key, states)
    for account, (_, epoch) in states.items():
        archive.release(account, 'worker-' + account, epoch)


# spec: ANYM-671
def test_outbound_block_counts_the_queue_and_its_age(out):
    out.send('out-tg', 2)
    out.send('out-wa', 1)
    out.wait('out-tg', 30)
    out.wait('out-wa', 45)
    telegram, whatsapp = out.outbound('out-tg'), out.outbound('out-wa')
    assert telegram['sender'] == 'ready' and telegram['queued'] == 2
    assert 30 <= telegram['oldest_queued_age_s'] < 40
    assert 560 <= telegram['expires_in_s'] <= 570 and telegram['expired_unsettled'] == 0
    assert whatsapp['sender'] == 'ready' and whatsapp['queued'] == 1
    assert 45 <= whatsapp['oldest_queued_age_s'] < 55
    assert 'next_action' in telegram and 'same thing again' in telegram['next_action']
    out.sql("UPDATE outgoing_messages SET status='sent' WHERE account='out-tg'")
    empty = out.outbound('out-tg')
    assert empty['queued'] == 0 and empty['oldest_queued_age_s'] is None and empty['expires_in_s'] is None
    assert 'next_action' not in empty


# spec: ANYM-671
@pytest.mark.parametrize('case,code,reason_code', [
    ('policy', 'sending_disabled', 'sending_disabled'),
    ('lease', 'sender_not_ready', 'collector_not_running'),
    ('connection', 'sender_not_ready', 'provider_temporarily_unavailable'),
])
def test_outbound_sender_matches_the_admission_check(out, case, code, reason_code):
    account = 'out-tg'
    assert out.outbound(account)['sender'] == 'ready'
    out.send(account)
    if case == 'policy':
        out.archive.set_sending(account, False)
    elif case == 'lease':
        out.sql("UPDATE collector_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE account=%s", account)
    else:
        out.connection(account, 'retry_wait', 'provider_temporarily_unavailable')
    block = out.outbound(account)
    assert block['sender'] == 'not_ready' and block['reason_code'] == reason_code and block['reason']
    with pytest.raises(APIError) as error:
        out.send(account)
    assert error.value.code == code
    assert out.outbound('out-wa')['sender'] == 'ready'


# spec: ANYM-671
def test_cooldown_is_a_sender_state_with_its_remaining_time(out):
    out.states['out-tg'][0].defer(300)
    block = out.outbound('out-tg')
    assert block['sender'] == 'cooling_down' and 280 <= block['cooldown_remaining_s'] <= 300
    assert out.outbound('out-wa')['sender'] == 'ready'
    # admission ignores a cooldown (outgoing.py), so the queue still accepts the request
    assert out.send('out-tg')


# spec: ANYM-671
def test_rows_past_expiry_are_not_counted_as_queued(out):
    first, second = out.send('out-tg', 2)
    out.expire(first)
    block = out.outbound('out-tg')
    assert block['queued'] == 1 and block['expired_unsettled'] == 1
    assert 'will not be sent' in block['next_action'] and 'even once the account is back' not in block['next_action']
    out.connection('out-tg', 'retry_wait', 'provider_temporarily_unavailable')
    assert 'even once the account is back' in out.outbound('out-tg')['next_action']
    out.expire(second)
    assert out.outbound('out-tg')['queued'] == 0


# spec: ANYM-671
def test_outbound_block_has_no_message_content(out):
    ids = out.send('out-tg', 2, text=CANARY)
    out.wait('out-tg', 200)
    source = out.status('out-tg')
    text = json.dumps({'outbound': source['outbound'], 'alerts': source['alerts']})
    for forbidden in [CANARY, 'out-agent', 'outbound-request', stable_id('chat', 'out-tg', '100'), *ids]:
        assert forbidden not in text
    assert set(source['outbound']) <= {'sender', 'queued', 'oldest_queued_age_s', 'expires_in_s', 'expired_unsettled',
        'reason_code', 'reason', 'next_action', 'retry_at', 'cooldown_remaining_s'}


# spec: ANYM-671
def test_next_action_when_the_cooldown_outlasts_the_queue(out):
    out.send('out-tg')
    out.states['out-tg'][0].defer(900)
    block = out.outbound('out-tg')
    assert block['sender'] == 'cooling_down' and block['cooldown_remaining_s'] > block['expires_in_s']
    assert 'Nothing was sent' in block['next_action'] and 'expire before the cooldown ends' in block['next_action']
    assert 'every queued send' in block['next_action']
    out.sql("UPDATE collector_health SET cooldown_until=clock_timestamp()+interval '30 seconds' WHERE account='out-tg'")
    short = out.outbound('out-tg')
    assert short['sender'] == 'cooling_down' and 'same thing again' in short['next_action']
    assert 'Nothing was sent' not in short['next_action']


# spec: ANYM-671
def test_not_ready_next_action_waits_for_retry_at_else_tells_the_owner(out):
    out.connection('out-tg', 'retry_wait', 'provider_temporarily_unavailable', retry_at='2026-11-02T12:30:00+00:00')
    waiting = out.outbound('out-tg')
    assert waiting['sender'] == 'not_ready' and waiting['retry_at'] == '2026-11-02T12:30:00+00:00'
    assert waiting['next_action'].startswith('Wait until 2026-11-02T12:30:00')
    out.connection('out-wa', 'blocked', 'provider_temporarily_unavailable')
    owner = out.outbound('out-wa')
    assert 'retry_at' not in owner and 'Tell the owner' in owner['next_action'] and 'out-wa' in owner['next_action']


# spec: ANYM-671
def test_stalled_queue_raises_an_alert_after_120_seconds(out):
    out.send('out-tg')
    out.wait('out-tg', 100)
    assert not [a for a in out.status('out-tg')['alerts'] if a.startswith('outbound_')]
    out.wait('out-tg', 30)
    source = out.status('out-tg')
    assert 'outbound_queue_stalled' in source['alerts']
    assert 'with the sender connected and no cooldown' in source['outbound']['reason']
    assert 'outbound_queue_stalled' not in out.status('out-wa')['alerts']


# spec: ANYM-671
def test_a_burst_of_thirty_sends_raises_no_alert(out):
    out.send('out-tg', 30)
    out.wait('out-tg', 30)
    source = out.status('out-tg')
    assert source['outbound']['queued'] == 30
    assert not [a for a in source['alerts'] if a.startswith('outbound_')]


# spec: ANYM-671
def test_cooldown_explains_a_waiting_queue_without_a_stall_alert(out):
    out.send('out-tg')
    out.wait('out-tg', 200)
    out.states['out-tg'][0].defer(600)
    source = out.status('out-tg')
    assert source['outbound']['sender'] == 'cooling_down'
    assert not [a for a in source['alerts'] if a.startswith('outbound_')]


# spec: ANYM-671
def test_not_ready_sender_with_queued_sends_raises_an_alert(out):
    out.send('out-tg')
    out.connection('out-tg', 'retry_wait', 'provider_temporarily_unavailable')
    assert 'outbound_sender_not_ready' in out.status('out-tg')['alerts']
    out.sql("UPDATE outgoing_messages SET status='sent' WHERE account='out-tg'")
    assert 'outbound_sender_not_ready' not in out.status('out-tg')['alerts']


# spec: ANYM-671
def test_compact_outbound_when_the_sender_cannot_send_even_with_an_empty_queue(out):
    out.connection('out-tg', 'retry_wait', 'provider_temporarily_unavailable')
    compact = render('get_sync_status', out.status(), 'compact')
    by_account = {source['account']: source for source in compact['sources']}
    lean = by_account['out-tg']['outbound']
    assert lean['sender'] == 'not_ready' and lean['reason_code'] == 'provider_temporarily_unavailable'
    assert 'next_action' in lean and 'queued' not in lean and 'oldest_queued_age_s' not in lean  # zero and null are omitted
    assert 'outbound' not in by_account['out-wa']
    out.send('out-wa')
    compact = render('get_sync_status', out.status(), 'compact')
    assert {s['account']: s for s in compact['sources']}['out-wa']['outbound']['queued'] == 1


# spec: ANYM-671
def test_compact_omits_outbound_when_all_is_quiet(out):
    result = out.status()
    assert all(source['outbound']['sender'] == 'ready' for source in result['sources'])
    compact = render('get_sync_status', result, 'compact')
    assert all('outbound' not in source for source in compact['sources'])


# spec: ANYM-671
def test_scoped_status_has_no_outbound_block(out):
    out.send('out-tg')
    scoped = out.store.issue_key('out-scoped', ['out-tg'], permissions=['read'])
    out.store.set_access('out-scoped', chats=[stable_id('chat', 'out-tg', '100')])
    result = out.store.get_sync_status(scoped, refresh=True)
    assert result['scope'] == 'permitted_chats' and result['sources']
    assert all('outbound' not in source for source in result['sources'])
    assert 'outbound' not in json.dumps(render('get_sync_status', result, 'compact'))


# spec: ANYM-671
def test_outbound_query_failure_leaves_the_rest_of_the_status(out, monkeypatch):
    from messaging_service import outgoing
    baseline = out.status('out-tg')
    monkeypatch.setattr(outgoing, 'OUTBOUND_SQL', 'SELECT nope FROM nowhere WHERE a=ANY(%s)')
    source = out.status('out-tg')
    assert source['outbound'] == {'sender': 'unavailable', 'queued': None, 'oldest_queued_age_s': None,
        'expires_in_s': None, 'expired_unsettled': None}
    for field in ('history', 'files', 'liveness', 'freshness', 'processing_profiles', 'operations'):
        assert field in source
    assert source['liveness'] == baseline['liveness']
    compact = render('get_sync_status', out.status(), 'compact')
    assert all(s['outbound']['sender'] == 'unavailable' for s in compact['sources'])


# spec: ANYM-671
def test_outbound_queue_query_uses_the_pending_index(out):
    from messaging_service.outgoing import OUTBOUND_SQL
    with out.store.pool.connection() as c:
        c.execute('SET LOCAL enable_seqscan=off')
        plan = '\n'.join(row['QUERY PLAN'] for row in c.execute('EXPLAIN ' + OUTBOUND_SQL, (list(ACCOUNTS),)))
    assert 'outgoing_pending' in plan


# spec: ANYM-671
def test_staggered_expiries_qualify_the_cooldown_advice_to_the_affected_sends(out):
    first, second = out.send('out-tg', 2)
    out.sql("UPDATE outgoing_messages SET expires_at=clock_timestamp()+interval '30 seconds' WHERE id=%s", first)
    out.states['out-tg'][0].defer(60)
    block = out.outbound('out-tg')
    assert block['sender'] == 'cooling_down' and block['expires_in_s'] <= 30 and block['cooldown_remaining_s'] > 30
    assert 'oldest queued send will expire' in block['next_action'] and 'later ones may still go out' in block['next_action']
    assert 'get_send_status' in block['next_action'] and 'every queued send' not in block['next_action']
    assert "new action with the owner's approval" in block['next_action']


# spec: ANYM-671
def test_expired_rows_and_a_disconnected_sender_give_both_instructions(out):
    first, second = out.send('out-tg', 2)
    out.expire(first)
    out.connection('out-tg', 'retry_wait', 'provider_temporarily_unavailable', retry_at='2026-11-02T12:30:00+00:00')
    waiting = out.outbound('out-tg')
    assert waiting['queued'] == 1 and waiting['expired_unsettled'] == 1
    assert 'even once the account is back' in waiting['next_action']
    assert 'Wait until 2026-11-02T12:30:00' in waiting['next_action'] and 'same thing again' in waiting['next_action']
    out.send('out-wa', 2)
    out.expire(out.store_send_ids('out-wa')[0])
    out.connection('out-wa', 'blocked', 'provider_temporarily_unavailable')
    owner = out.outbound('out-wa')
    assert 'even once the account is back' in owner['next_action'] and 'Tell the owner' in owner['next_action']
    assert 'out-wa' in owner['next_action'] and 'same thing again' in owner['next_action']
