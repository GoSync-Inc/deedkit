from datetime import datetime, timedelta, timezone

import pytest

from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState, validate_capture
from messaging_service.envelope import stable_id
from messaging_service.presentation import render
from messaging_service.reply_state import observe, validate_observation
from test_archive import event


def ago(**delta):
    return datetime.now(timezone.utc)-timedelta(**delta)


@pytest.fixture()
def live(seeded):
    """A Telegram chat whose newest incoming is 4 h old and a collector in a receiving period."""
    s, _, _ = seeded
    ar = Archive(s)
    ar.register('fresh-test', 'telegram', 'Synthetic freshness', discover=True)
    key = s.issue_key('fresh-agent', ['fresh-test'])
    at = ago(hours=4).isoformat()
    ar.ingest(event(account='fresh-test', chat='100', provider_id='1', sent_at=at, updated_at=at,
        metadata={'chat_type':'private'}))
    epoch = ar.acquire('fresh-test', 'synthetic-collector', 60)
    state = CollectorState(ar, 'fresh-test', ('synthetic-collector', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    state.phase('live_capture', 'receiving', 'capture_ready')
    with s.pool.connection() as c:
        # The period began before the observation: simulate a run that has received since then.
        c.execute("UPDATE collector_health SET receiving_since=now()-interval '3 hours 30 minutes' WHERE account='fresh-test'")
        observe(c, 'fresh-test', '100', {'observed_at':ago(hours=3).isoformat(), 'unread_count':2,
            'read_inbox_max_id':0, 'basis':'telegram_dialog'})
    state.capture_progress(None, 0, 0)
    def row(**kwargs):
        items = s.list_chats(key, awaiting_reply=True, include_coverage=False, **kwargs)['items']
        return items[0] if items else None
    return s, ar, state, row


# spec: ANYM-275
def test_continuous_receiving_keeps_an_older_observation_current(live):
    s, ar, state, row = live
    assert row()['read_state'] == 'observed' and row()['provider_unread'] is True
    assert row(only_unread=True)['provider_unread_count'] == 2


# spec: ANYM-275
def test_restart_or_disconnect_after_the_observation_ends_continuity(live):
    s, ar, state, row = live
    state.phase('connection', 'connected', 'provider_ready')
    assert row()['read_state'] == 'stale' and row(only_unread=True) is None
    state.connection_failed('provider_disconnected')
    assert row()['read_state'] == 'stale'


# spec: ANYM-275
def test_abrupt_death_and_takeover_end_continuity(live):
    s, ar, state, row = live
    with s.pool.connection() as c:
        c.execute("UPDATE collector_leases SET expires_at=now()-interval '1 second' WHERE account='fresh-test'")
    # An expired lease with no new holder.
    assert row()['read_state'] == 'stale'
    # A takeover gets a new epoch; the old period never proves the new holder's receiving.
    ar.acquire('fresh-test', 'synthetic-successor', 60)
    assert row()['read_state'] == 'stale'


# spec: ANYM-275
def test_backlog_or_provider_gap_after_the_observation_ends_continuity(live):
    s, ar, state, row = live
    state.capture_progress(None, 0, 3)
    assert row()['read_state'] == 'stale'
    state.capture_progress(None, 0, 0)
    assert row()['read_state'] == 'observed'
    state.apply(validate_capture({'kind':'read_state_gap', 'account':'fresh-test',
        'observed_at':datetime.now(timezone.utc).isoformat(), 'reason':'app_state_sync_error'}))
    assert row()['read_state'] == 'stale'


# spec: ANYM-275
def test_stale_heartbeat_ends_continuity(live):
    s, ar, state, row = live
    with s.pool.connection() as c:
        c.execute("UPDATE collector_health SET receiving_heartbeat_at=now()-interval '2 minutes' WHERE account='fresh-test'")
    assert row()['read_state'] == 'stale'


# spec: ANYM-275
def test_continuity_never_extends_past_the_maximum_age(live):
    s, ar, state, row = live
    with s.pool.connection() as c:
        c.execute("UPDATE collector_health SET receiving_since=now()-interval '30 hours' WHERE account='fresh-test'")
        c.execute("UPDATE chat_read_observations SET observed_at=now()-interval '25 hours' WHERE account='fresh-test'")
        c.execute("UPDATE messages SET sent_at=now()-interval '26 hours' WHERE account='fresh-test'")
    assert row()['read_state'] == 'stale'


# spec: ANYM-275
def test_newer_incoming_than_the_observation_is_stale_with_continuity(live):
    s, ar, state, row = live
    at = ago(hours=1).isoformat()
    ar.ingest(event(account='fresh-test', chat='100', provider_id='2', sent_at=at, updated_at=at,
        metadata={'chat_type':'private'}))
    assert row()['read_state'] == 'stale' and row()['provider_unread_count'] == 2


@pytest.fixture()
def wa(seeded):
    s, _, _ = seeded
    ar = Archive(s)
    ar.register('wa-read', 'whatsapp', 'Synthetic WhatsApp', discover=True)
    key = s.issue_key('wa-agent', ['wa-read'])
    epoch = ar.acquire('wa-read', 'synthetic-native', 60)
    state = CollectorState(ar, 'wa-read', ('synthetic-native', epoch))
    chat = '15550001111@c.us'
    def message(mid, minutes):
        at = ago(minutes=minutes).isoformat()
        ar.ingest(event(account='wa-read', platform='whatsapp', chat=chat, provider_id=mid, sender='15550001111@c.us',
            sent_at=at, updated_at=at, metadata={'chat_type':'private'}))
    def read(basis, minutes, **fields):
        at = ago(minutes=minutes).isoformat()
        capture = validate_capture({'kind':'owner_read_state', 'account':'wa-read', 'chat':chat, 'observed_at':at,
            'observation':{'observed_at':at, 'basis':basis, **fields}})
        assert state.apply(capture)['outcome'] == 'owner_read_observed'
    def row():
        return s.list_chats(key, awaiting_reply=True, include_coverage=False)['items'][0]
    def reads():
        with s.pool.connection() as c:
            return sorted(r['provider_id'] for r in c.execute('SELECT provider_id FROM owner_message_reads WHERE chat=%s',
                (stable_id('chat', 'wa-read', chat),)))
    return s, message, read, row, reads


# spec: ANYM-275
def test_whatsapp_read_sources_pass_validation():
    at = ago(minutes=1).isoformat()
    assert validate_observation({'observed_at':at, 'basis':'whatsapp_app_state_read', 'unread_count':0, 'marked_unread':False})
    assert validate_observation({'observed_at':at, 'basis':'whatsapp_app_state_read', 'unread_count':None, 'marked_unread':True})
    assert validate_observation({'observed_at':at, 'basis':'whatsapp_history_sync', 'unread_count':3, 'marked_unread':False})
    assert validate_observation({'observed_at':at, 'basis':'whatsapp_owner_receipt', 'unread_count':None,
        'read_message_ids':['B', 'A', 'A']})['read_message_ids'] == ['A', 'B']
    # A receipt whose messages are no longer retained still invalidates an older count.
    assert validate_observation({'observed_at':at, 'basis':'whatsapp_owner_receipt', 'read_message_ids':[]})['read_message_ids'] == []
    for bad in ({'basis':'whatsapp_app_state_read', 'unread_count':2, 'marked_unread':False},
                {'basis':'whatsapp_app_state_read', 'unread_count':0, 'marked_unread':True},
                {'basis':'whatsapp_owner_receipt', 'unread_count':1, 'read_message_ids':['A']},
                {'basis':'whatsapp_history_sync', 'unread_count':None},
                {'basis':'telegram_dialog', 'unread_count':1, 'marked_unread':True},
                {'basis':'whatsapp_history_sync', 'unread_count':1, 'read_inbox_max_id':4}):
        with pytest.raises(ValueError):
            validate_observation({'observed_at':at, **bad})
    with pytest.raises(ValueError):
        validate_capture({'kind':'owner_read_state', 'account':'wa-read', 'chat':'100', 'observed_at':at,
            'observation':{'observed_at':at, 'basis':'whatsapp_history_sync', 'unread_count':1}})


# spec: ANYM-275
def test_mark_read_baseline_then_newer_incoming_is_stale_without_arithmetic(wa):
    s, message, read, row, reads = wa
    message('A', 10)
    read('whatsapp_app_state_read', 3, unread_count=0, marked_unread=False)
    r = row()
    assert (r['read_state'], r['provider_unread_count'], r['read_state_basis']) == ('observed', 0, 'whatsapp_app_state_read')
    message('B', 2); message('C', 1)
    r = row()
    assert (r['read_state'], r['provider_unread_count']) == ('stale', 0)
    # A newer whole-chat mark-read is again an exact 0.
    read('whatsapp_app_state_read', 0, unread_count=0, marked_unread=False)
    assert (row()['read_state'], row()['provider_unread_count']) == ('observed', 0)


# spec: ANYM-275
def test_mark_unread_is_a_flag_and_only_unread_includes_it(wa):
    s, message, read, row, reads = wa
    message('A', 10)
    read('whatsapp_app_state_read', 2, unread_count=0, marked_unread=False)
    read('whatsapp_app_state_read', 1, unread_count=None, marked_unread=True)
    r = row()
    assert r['read_state'] == 'observed' and r['provider_unread_count'] is None and r['provider_unread'] is True
    assert r['read_state_count_basis'] == 'stale_since_baseline'
    compact = render('list_chats', {'items':[r]}, 'compact')['items'][0]
    assert compact['read_state_marked_unread'] is True and 'unread_count' not in compact


# spec: ANYM-275
def test_owner_receipt_alone_is_partial_evidence(wa):
    s, message, read, row, reads = wa
    message('A', 10)
    read('whatsapp_owner_receipt', 1, unread_count=None, read_message_ids=['A'])
    r = row()
    assert r['provider_unread_count'] is None and r['read_state_count_basis'] == 'partial' and r['read_state'] == 'stale'
    assert r['read_by_owner'] is True and reads() == ['A']
    assert render('list_chats', {'items':[r]}, 'compact')['items'][0]['read_state_count_basis'] == 'partial'


# spec: ANYM-275
def test_selective_receipt_invalidates_a_history_count_instead_of_subtracting(wa):
    s, message, read, row, reads = wa
    for minutes, mid in ((40, '1'), (30, '2'), (20, '3'), (10, '4')):message(mid, minutes)
    read('whatsapp_history_sync', 4, unread_count=3, marked_unread=False)
    assert (row()['read_state'], row()['provider_unread_count']) == ('observed', 3)
    read('whatsapp_owner_receipt', 1, unread_count=None, read_message_ids=['2', '4'])
    r = row()
    assert r['provider_unread_count'] is None and r['read_state_count_basis'] == 'stale_since_baseline'
    assert reads() == ['2', '4']
    # The hinted message 4 was listed; 1 and 3 never become read.
    assert r['read_by_owner'] is True


# spec: ANYM-275
def test_history_count_with_newer_incoming_is_stale_and_keeps_the_count(wa):
    s, message, read, row, reads = wa
    message('A', 20)
    read('whatsapp_history_sync', 10, unread_count=3, marked_unread=False)
    message('B', 5)
    assert (row()['read_state'], row()['provider_unread_count']) == ('stale', 3)


# spec: ANYM-275
@pytest.mark.parametrize('order', [('A', 'B', 'A'), ('B', 'A'), ('A', 'B')])
def test_receipts_in_any_order_give_the_in_order_result(wa, order):
    s, message, read, row, reads = wa
    message('X', 30)
    read('whatsapp_app_state_read', 20, unread_count=0, marked_unread=False)
    message('A', 10); message('B', 5)
    times = {'A':3, 'B':2}
    for mid in order:read('whatsapp_owner_receipt', times[mid], unread_count=None, read_message_ids=[mid])
    r = row()
    assert r['provider_unread_count'] is None and r['read_state_count_basis'] == 'stale_since_baseline'
    assert reads() == sorted(set(order)) and r['read_by_owner'] is True


# spec: ANYM-275
def test_receipt_for_an_earlier_message_leaves_the_hint_unread(wa):
    s, message, read, row, reads = wa
    message('X', 30)
    read('whatsapp_app_state_read', 20, unread_count=0, marked_unread=False)
    message('A', 10); message('B', 5)
    read('whatsapp_owner_receipt', 3, unread_count=None, read_message_ids=['A'])
    r = row()
    assert r['provider_unread_count'] is None and r['read_state_count_basis'] == 'stale_since_baseline'
    assert reads() == ['A'] and r['read_by_owner'] is not True


# spec: ANYM-275
def test_captured_chat_without_a_source_keeps_the_known_count(wa):
    s, message, read, row, reads = wa
    message('A', 10)
    read('whatsapp_history_sync', 5, unread_count=1, marked_unread=False)
    with s.pool.connection() as c:
        observe(c, 'wa-read', '15550001111@c.us', {'observed_at':ago(seconds=1).isoformat(), 'unread_count':None,
            'basis':'whatsmeow_captured_chat', 'is_self_chat':False})
    assert (row()['provider_unread_count'], row()['read_state_basis']) == (1, 'whatsapp_history_sync')


# spec: ANYM-275
@pytest.mark.parametrize('reason', ['provider_disconnected', 'provider_reconnected', 'read_state_write_failed'])
def test_native_connection_gaps_end_continuity(live, reason):
    s, ar, state, row = live
    assert row()['read_state'] == 'observed'
    state.apply(validate_capture({'kind':'read_state_gap', 'account':'fresh-test',
        'observed_at':datetime.now(timezone.utc).isoformat(), 'reason':reason}))
    assert row()['read_state'] == 'stale'


# spec: ANYM-275
def test_quarantined_or_invalid_capture_ends_continuity(live):
    s, ar, state, row = live
    state.capture_progress(None, 0, 0, quarantined=0)
    assert row()['read_state'] == 'observed'
    state.capture_progress(None, 0, 0, quarantined=1)
    assert row()['read_state'] == 'stale'
    state.phase('connection', 'connected', 'provider_ready')
    with s.pool.connection() as c:
        c.execute("UPDATE collector_health SET receiving_since=now()-interval '3 hours 30 minutes' WHERE account='fresh-test'")
    state.capture_progress(None, 0, 0, quarantined=1)
    assert row()['read_state'] == 'observed'
    state.capture_progress(None, 0, 0, invalid=1)
    assert row()['read_state'] == 'stale'


# spec: ANYM-275
def test_telegram_auto_reconnect_ends_continuity():
    import asyncio
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl import types
    from messaging_service.telegram_collector import TelethonProvider
    # A real, never-connected client: the hooks are the ones Telethon itself calls.
    client = TelegramClient(StringSession(), 1, 'synthetic', receive_updates=False)
    assert client._sender._auto_reconnect_callback == client._handle_auto_reconnect
    calls = []
    async def telethon_reconnect(): calls.append('telethon')
    client._sender._auto_reconnect_callback = telethon_reconnect
    class State:
        def __init__(self, fail=False): self.fail = fail
        def update_gap(self):
            calls.append('gap')
            if self.fail: raise RuntimeError('synthetic')
    provider = object.__new__(TelethonProvider)
    provider.client = client
    provider.bind_state(State())
    provider.bind_state(State(fail=True))
    async def scenario():
        await client._sender._auto_reconnect_callback()
        box = client._message_box
        box.map[1] = box.map.get(1) or type('Entry', (), {'pts': 0})()
        request = type("Request", (), {"channel": types.InputChannel(1, 0)})()
        box.apply_channel_difference(request, types.updates.ChannelDifferenceTooLong(
            dialog=type('Dialog', (), {'pts': 5})(),
            messages=[], chats=[], users=[], final=True, timeout=None), type('Hashes', (), {'extend': lambda *a: None})())
        await asyncio.sleep(0.05)
    asyncio.run(scenario())
    # Bound twice: one hook each; a failed gap write never stops Telethon's own handler.
    assert calls == ['gap', 'telethon', 'gap']


# spec: ANYM-275
def test_receipt_without_retained_messages_invalidates_the_count(wa):
    s, message, read, row, reads = wa
    message('A', 10)
    read('whatsapp_history_sync', 5, unread_count=1, marked_unread=False)
    read('whatsapp_owner_receipt', 1, unread_count=None, read_message_ids=[])
    r = row()
    assert r['provider_unread_count'] is None and r['read_state_count_basis'] == 'stale_since_baseline' and reads() == []


# spec: ANYM-275
def test_discovery_without_evidence_never_hides_an_older_baseline(wa):
    s, message, read, row, reads = wa
    message('A', 10)
    with s.pool.connection() as c:
        observe(c, 'wa-read', '15550001111@c.us', {'observed_at':ago(seconds=1).isoformat(), 'unread_count':None,
            'basis':'whatsapp_cached_chat', 'is_self_chat':False})
    read('whatsapp_history_sync', 5, unread_count=3, marked_unread=False)
    assert (row()['provider_unread_count'], row()['read_state_basis']) == (3, 'whatsapp_history_sync')
    # A receipt row is evidence: an older baseline does not replace it.
    read('whatsapp_owner_receipt', 1, unread_count=None, read_message_ids=['A'])
    read('whatsapp_history_sync', 6, unread_count=4, marked_unread=False)
    assert row()['provider_unread_count'] is None
