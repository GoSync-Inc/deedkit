"""ANYM-659: a `sent` result says which receipts the messenger can report and the receipt situation in one word.
Synthetic rows and a fixed clock; the database tests use a fake provider and never touch a live one."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from messaging_service.envelope import stable_id
from messaging_service.outgoing import OutgoingWorker, result
from messaging_service.receipts import messenger_reports, receipt_hint
from test_archive import event
from test_receipt_api import read_capture
from test_sending import sender as sender, submit

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def hint(platform, chat_type=None, evidence='unknown', age_min=5, bot=False, self_chat=False, sent_at=...):
    sent = NOW - timedelta(minutes=age_min) if sent_at is ... else sent_at
    return receipt_hint(platform, chat_type, bot, self_chat, evidence, sent, NOW)


# spec: ANYM-659
@pytest.mark.parametrize('args,expected', [
    (('whatsapp', 'private'), ['delivered', 'read']),
    (('whatsapp', 'group'), ['delivered', 'read']),
    (('whatsapp', None), ['delivered', 'read']),
    (('telegram', 'private'), ['read']),
    (('telegram', 'private', True), []),
    (('telegram', 'group'), []),
    (('telegram', 'channel'), []),
    (('telegram', 'private', False, True), []),
    (('whatsapp', 'private', False, True), []),
    (('telegram', None), None),
    (('telegram', 'unknown'), None),
    (('signal', 'private'), None),
    ((None, None), None),
])
def test_messenger_reports_table(args, expected):
    assert messenger_reports(*args) == expected


# spec: ANYM-659
@pytest.mark.parametrize('platform,chat_type,evidence,age,state', [
    ('whatsapp', 'private', 'unknown', 5, 'pending'),
    ('whatsapp', 'private', 'server', 5, 'pending'),
    ('whatsapp', 'private', 'pending', 11, 'overdue'),
    ('whatsapp', 'group', 'server', 11, 'overdue'),
    ('whatsapp', 'private', 'delivered', 5, 'delivered'),
    ('whatsapp', 'private', 'delivered', 60, 'delivered'),
    ('whatsapp', 'private', 'read', 11, 'read'),
    ('whatsapp', 'private', 'played', 11, 'played'),
    ('whatsapp', 'private', 'error', 5, 'receipt_error'),
    ('whatsapp', 'private', 'error', 11, 'receipt_error'),
    ('telegram', 'private', 'unknown', 5, 'not_reported'),
    ('telegram', 'private', 'unknown', 11, 'not_reported'),
    ('telegram', 'private', 'read', 11, 'read'),
    ('telegram', 'group', 'unknown', 11, 'not_reported'),
    ('telegram', 'channel', 'unknown', 5, 'not_reported'),
    ('telegram', None, 'unknown', 5, 'unknown'),
    ('signal', 'private', 'unknown', 5, 'unknown'),
])
def test_receipt_state_one_word(platform, chat_type, evidence, age, state):
    assert hint(platform, chat_type, evidence, age)[1] == state


# spec: ANYM-659
def test_receipt_state_switches_to_overdue_at_ten_minutes():
    assert hint('whatsapp', 'private', age_min=9.99)[1] == 'pending'
    assert hint('whatsapp', 'private', age_min=10)[1] == 'overdue'


# spec: ANYM-659
def test_self_chat_and_bot_peer_never_report_delivery():
    assert hint('telegram', 'private', self_chat=True) == ([], 'not_reported')
    assert hint('whatsapp', 'private', evidence='not_applicable', self_chat=True) == ([], 'not_reported')
    assert hint('telegram', 'private', bot=True) == ([], 'not_reported')


# spec: ANYM-659
@pytest.mark.parametrize('bad', [None, 'x', 1, [], {}])
def test_hint_is_total_for_missing_or_malformed_input(bad):
    assert receipt_hint(bad, bad, bad, bad, bad, bad, bad)[1] in ('unknown', 'receipt_error', 'not_reported')
    assert hint('whatsapp', 'private', sent_at=None) == (['delivered', 'read'], 'unknown')
    assert receipt_hint('whatsapp', 'private', False, False, 'unknown', NOW, None) == (['delivered', 'read'], 'unknown')
    assert receipt_hint('signal', None, False, False, 'unknown', None, None) == (None, 'unknown')


def row(**changes):
    value = {'id': 'send_' + 'a' * 32, 'status': 'sent', 'account': 'send-fixture', 'chat': 'c', 'provider_chat': '100',
        'provider_message_id': None, 'created_at': NOW, 'expires_at': NOW, 'finished_at': NOW, 'error_code': None,
        'principal': 'p', 'key_id': 'k', 'request_id': 'synthetic-request-0001', 'source_format': 'plain'}
    value.update(changes)
    return value


# spec: ANYM-659
@pytest.mark.parametrize('status', ['queued', 'dispatching', 'unknown', 'failed', 'expired', 'cancelled'])
def test_non_sent_results_get_no_new_fields(status):
    out = result(row(status=status))
    assert 'messenger_reports' not in out and 'receipt_state' not in out


# spec: ANYM-659
def test_sent_result_without_a_database_is_unknown_and_next_action_is_unchanged():
    out = result(row())
    assert out['messenger_reports'] is None and out['receipt_state'] == 'unknown'
    assert out['next_action'] == 'none'
    assert result(row(status='queued'))['next_action'] == 'get_send_status'
    assert result(row(status='unknown'))['next_action'] == 'verify_history_before_any_new_send'


# spec: ANYM-659
def test_telegram_private_send_reports_read_only(sender):
    store, _, key, _, state = sender
    with store.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type='private' WHERE account='send-fixture'")
    queued = submit(store, key)
    assert 'receipt_state' not in queued and queued['next_action'] == 'get_send_status'

    async def provider(job):
        return {'provider_message_id': '42'}
    assert asyncio.run(OutgoingWorker(state, provider).once())
    sent = store.get_send_status(key, queued['send_id'])
    assert sent['status'] == 'sent' and sent['next_action'] == 'none'
    assert sent['messenger_reports'] == ['read'] and sent['receipt_state'] == 'not_reported'
    assert sent['recipient_delivery_confirmed'] is False
    state.apply(read_capture())
    read = store.get_send_status(key, queued['send_id'])
    assert read['receipt_state'] == 'read' and read['next_action'] == 'none'
    assert submit(store, key)['receipt_state'] == 'read'


# spec: ANYM-659
@pytest.mark.parametrize('chat_type,bot,reports', [('group', False, []), ('channel', False, []), ('private', True, [])])
def test_telegram_group_channel_and_bot_report_nothing(sender, chat_type, bot, reports):
    store, archive, key, _, state = sender
    with store.pool.connection() as c:
        c.execute('UPDATE chats SET chat_type=%s WHERE account=%s', (chat_type, 'send-fixture'))
    if bot:
        archive.ingest(event(account='send-fixture', provider_id='2', body='From a bot',
            metadata={'sender_is_bot': True}))
    queued = submit(store, key)

    async def provider(job):
        return {'provider_message_id': '42'}
    assert asyncio.run(OutgoingWorker(state, provider).once())
    sent = store.get_send_status(key, queued['send_id'])
    assert sent['messenger_reports'] == reports and sent['receipt_state'] == 'not_reported'
    state.apply(read_capture())
    # a boundary captured for a chat that cannot report is still evidence; the state follows the evidence
    assert store.get_send_status(key, queued['send_id'])['receipt_state'] == 'read'


# spec: ANYM-659
def test_telegram_chat_without_a_stored_type_is_unknown(sender):
    store, _, key, _, state = sender
    queued = submit(store, key)

    async def provider(job):
        return {'provider_message_id': '42'}
    assert asyncio.run(OutgoingWorker(state, provider).once())
    sent = store.get_send_status(key, queued['send_id'])
    assert sent['messenger_reports'] is None and sent['receipt_state'] == 'unknown'


# spec: ANYM-659
def test_whatsapp_send_walks_pending_overdue_delivered_and_error(seeded):
    from messaging_service.archive import Archive
    from messaging_service.collector_state import CollectorState
    store, _, _ = seeded
    archive = Archive(store)
    account = 'receipt-hint-wa'
    chat = '100@c.us'
    provider_id = 'true_' + chat + '_synthetic'
    archive.register(account, 'whatsapp', 'Synthetic hint account', discover=True)
    archive.ingest(event(account=account, platform='whatsapp', chat=chat, provider_id='existing'))
    key = store.issue_key('hint-wa-reader', [account], permissions=['read', 'send'])
    epoch = archive.acquire(account, 'hint-wa-worker')
    state = CollectorState(archive, account, ('hint-wa-worker', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    archive.set_sending(account, True)
    action = store.send_message(key, account, stable_id('chat', account, chat), 'Synthetic hint payload',
        'receipt-hint-wa-0001')

    async def provider(job):
        return {'provider_message_id': provider_id}
    assert asyncio.run(OutgoingWorker(state, provider).once())
    status = lambda: store.get_send_status(key, action['send_id'])
    sent = status()
    assert sent['messenger_reports'] == ['delivered', 'read'] and sent['receipt_state'] == 'pending'
    assert sent['next_action'] == 'none'
    with store.pool.connection() as c:
        c.execute("UPDATE outgoing_messages SET finished_at=clock_timestamp()-interval '11 minutes' WHERE id=%s",
            (action['send_id'],))
    assert status()['receipt_state'] == 'overdue'
    capture = {'kind': 'message_receipt', 'account': account, 'platform': 'whatsapp', 'chat': chat,
        'provider_id': provider_id, 'max_id': None, 'state': 'server', 'basis': 'whatsapp_message_ack',
        'audience': 'private_peer', 'observed_at': '2026-10-14T10:00:00Z', 'provider_at': None}
    state.apply({**capture, 'state': 'error', 'observed_at': '2026-10-14T10:00:00Z'})
    assert status()['receipt_state'] == 'receipt_error'
    # the existing evidence ranking puts a server ack above an error, so the state follows the strongest evidence
    state.apply({**capture, 'state': 'server', 'observed_at': '2026-10-14T10:00:30Z'})
    assert status()['receipt_state'] == 'overdue'
    state.apply({**capture, 'state': 'delivered', 'observed_at': '2026-10-14T10:01:00Z'})
    assert status()['receipt_state'] == 'delivered'
    state.apply({**capture, 'state': 'read', 'observed_at': '2026-10-14T10:02:00Z'})
    final = status()
    assert final['receipt_state'] == 'read' and final['next_action'] == 'none'
    assert final['recipient_delivery_confirmed'] is True
    assert store.send_message(key, account, stable_id('chat', account, chat), 'Synthetic hint payload',
        'receipt-hint-wa-0001')['receipt_state'] == 'read'
