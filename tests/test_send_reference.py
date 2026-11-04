"""ANYM-751: a Telegram send that reached `sent` returns the provider-confirmed chat and message reference.

Synthetic accounts in an isolated PostgreSQL and a fake provider; nothing is sent and no live provider runs.
"""
import asyncio

import pytest

from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState
from messaging_service.envelope import stable_id
from messaging_service.outgoing import OutgoingWorker, project_reference, result
from test_archive import event, wa_event
from test_sending import sender as sender, submit

CHAT = stable_id('chat', 'send-fixture', '100')


def sent(s, state, key, provider_message_id='42'):
    async def provider(job):
        return {'provider_message_id': provider_message_id}
    queued = submit(s, key)
    assert asyncio.run(OutgoingWorker(state, provider).once()) is True
    return queued


def stored(s, send_id):
    with s.pool.connection() as c:
        return c.execute('SELECT * FROM outgoing_messages WHERE id=%s', (send_id,)).fetchone()


# spec: ANYM-751
def test_sent_telegram_send_returns_the_provider_reference(sender):
    s, _, key, _, state = sender
    queued = sent(s, state, key)
    status = s.get_send_status(key, queued['send_id'])
    message_id = stable_id('msg', 'send-fixture', '100', '42')
    assert status['status'] == 'sent' and status['archive_reference_state'] == 'provider_verified'
    assert status['archive_reference'] == {'provider_chat_id': '100', 'provider_message_id': '42',
                                           'chat_id': CHAT, 'message_id': message_id}
    # The same id the result already showed; the archive row may lag (capture delay), so none is required.
    assert status['message_id'] == message_id
    # Derived at read time: nothing is written.
    assert stored(s, queued['send_id'])['archive_reference'] is None


# spec: ANYM-751
def test_a_key_that_may_not_read_the_chat_gets_scope_restricted_and_no_ids(sender):
    s, _, key, _, state = sender
    queued = sent(s, state, key)
    row = stored(s, queued['send_id'])
    restricted = {'accounts': ['send-fixture'], 'chat_ids': [stable_id('chat', 'send-fixture', '200')]}
    with s.pool.connection() as c:
        view = result(row, c, restricted)
    assert view['archive_reference'] is None and view['archive_reference_state'] == 'scope_restricted'
    assert view['message_id'] is None and view['delivery_receipt']['state'] == 'unknown'
    assert stable_id('msg', 'send-fixture', '100', '42') not in str(view)
    # The send's own requested ids stay; only the projected archive ids are withheld.
    assert (view['chat_id'], view['provider_chat_id'], view['provider_message_id']) == (CHAT, '100', '42')


# spec: ANYM-751
@pytest.mark.parametrize('status', ['queued', 'dispatching', 'unknown', 'failed'])
def test_unsettled_telegram_rows_keep_legacy_requested(sender, status):
    s, _, key, _, _ = sender
    queued = submit(s, key)
    with s.pool.connection() as c:
        c.execute("UPDATE outgoing_messages SET status=%s,provider_message_id=%s WHERE id=%s",
                  (status, '42' if status == 'unknown' else None, queued['send_id']))
    view = s.get_send_status(key, queued['send_id'])
    assert view['archive_reference'] is None and view['archive_reference_state'] == 'legacy_requested'


@pytest.fixture
def wa_sender(seeded):
    s, _, _ = seeded
    archive = Archive(s)
    archive.register('wa-reference', 'whatsapp', 'Synthetic WhatsApp reference', discover=True)
    archive.ingest(wa_event(account='wa-reference', chat='900@lid', provider_id='false_900@lid_3EB0AAA1',
                            body='Existing synthetic message'))
    key = s.issue_key('wa-reference-agent', ['wa-reference'], permissions=['read', 'send'])
    epoch = archive.acquire('wa-reference', 'synthetic-wa-reference-worker')
    state = CollectorState(archive, 'wa-reference', ('synthetic-wa-reference-worker', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    archive.set_sending('wa-reference', True)
    yield s, key, state
    archive.release('wa-reference', 'synthetic-wa-reference-worker', epoch)


# spec: ANYM-751
def test_whatsapp_legacy_sent_row_is_unchanged(wa_sender):
    s, key, _ = wa_sender
    queued = s.send_message(key, account='wa-reference', chat_id=stable_id('chat', 'wa-reference', '900@lid'),
                            text='Exact synthetic text', request_id='wa-reference-0001')
    with s.pool.connection() as c:
        # A legacy WhatsApp receipt: sent with a provider id and no stored archive_reference.
        c.execute("UPDATE outgoing_messages SET status='sent',provider_message_id=%s,finished_at=clock_timestamp() "
                  "WHERE id=%s", ('true_900@lid_3EB0BBB1', queued['send_id']))
    view = s.get_send_status(key, queued['send_id'])
    assert view['status'] == 'sent' and view['archive_reference'] is None
    assert view['archive_reference_state'] == 'legacy_requested'
    assert view['message_id'] == stable_id('msg', 'wa-reference', '900@lid', 'true_900@lid_3EB0BBB1')


# spec: ANYM-751
def test_operation_results_that_embed_a_reference_are_unchanged(sender):
    s = sender[0]
    # commands.py projects an operation answer's own archive_reference through project_reference, which this
    # change leaves alone: a null reference stays legacy_requested and a given one keeps its projection.
    principal = {'accounts': ['send-fixture'], 'chat_ids': None}
    with s.pool.connection() as c:
        assert project_reference(c, principal, 'send-fixture', None) == (None, 'legacy_requested')
        actual, state = project_reference(c, principal, 'send-fixture',
                                          {'provider_chat_id': '100', 'provider_message_id': '7'})
    assert state == 'provider_verified' and actual['message_id'] == stable_id('msg', 'send-fixture', '100', '7')
    assert actual['chat_id'] == CHAT


# spec: ANYM-751
def test_an_old_sent_telegram_row_gets_the_reference_and_a_later_archive_row_matches(sender):
    s, archive, key, _, state = sender
    queued = sent(s, state, key, provider_message_id='43')
    view = s.get_send_status(key, queued['send_id'])
    # When the capture lands, the reference names that archive row.
    mid = archive.ingest(event(account='send-fixture', chat='100', provider_id='43', direction='outgoing',
                               body='Exact synthetic text'))['message']
    assert view['archive_reference']['message_id'] == mid == view['message_id']
