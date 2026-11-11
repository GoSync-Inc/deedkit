"""ANYM-929: list and cancel the owner's scheduled Telegram sends; isolated PostgreSQL and synthetic providers only."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from telethon import errors, functions, types

from messaging_service import scheduled_send
from messaging_service.commands import CommandWorker
from messaging_service.envelope import stable_id
from messaging_service.scheduled_manage import ScheduledCollector
from messaging_service.store import APIError
from messaging_service.telegram_operations import execute
from test_archive import event
from test_scheduled_send import ahead, events_of, no_pending_updates as no_pending_updates, scheduled_row
from test_sending import sender as sender

CHAT = stable_id('chat', 'send-fixture', '100')
ACTIONS = Path(__file__).resolve().parents[1] / 'services/gateway/src/agent_gateway/actions.json'
LONG = 'Synthetic scheduled greeting ' + 'x' * 250


@pytest.fixture(autouse=True)
def no_agent_cancels(monkeypatch):
    monkeypatch.setattr(scheduled_send, '_agent_cancels', {}, raising=False)


class Client:
    """Telegram's scheduled-list answers for chat 100: `held` IDs wait, `error` fails the delete."""
    def __init__(self, held=(7,), owner=(99,), sent=None, error=None, read_error=None, on_delete=None):
        self.held, self.owner, self.sent, self.error, self.read_error = held, owner, sent, error, read_error
        self.on_delete, self.calls = on_delete, []

    async def get_input_entity(self, peer):
        return types.InputPeerUser(peer, 1)

    def message(self, number, text):
        return types.Message(id=number, peer_id=types.PeerUser(100), message=text,
                             date=datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1))

    async def __call__(self, request):
        self.calls.append(type(request).__name__)
        if isinstance(request, functions.messages.GetScheduledHistoryRequest):
            if self.read_error:
                raise self.read_error
            return types.messages.Messages([self.message(n, LONG) for n in (*self.held, *self.owner)], [], [], [])
        if isinstance(request, functions.messages.GetScheduledMessagesRequest):
            return types.messages.Messages([self.message(n, LONG) if n in self.held else types.MessageEmpty(id=n, peer_id=types.PeerUser(100))
                                            for n in request.id], [], [], [])
        if isinstance(request, functions.messages.DeleteScheduledMessagesRequest):
            if self.on_delete:
                self.on_delete()
            if self.error:
                raise self.error
            update = types.UpdateDeleteScheduledMessages(types.PeerUser(100), list(request.id), self.sent)
            return types.Updates([update], [], [], datetime.now(timezone.utc), 0)
        raise AssertionError('unexpected provider request')


class Provider:
    def __init__(self, client):
        self.client = client

    async def execute_operation(self, job):
        return await execute(self.client, job)


class Collector(ScheduledCollector):
    """The TelegramCollector dispatch for these two operations, over a synthetic Telethon client."""
    def __init__(self, state, client):
        self.state, self.provider = state, Provider(client)

    async def execute_operation(self, job):
        job['_policy'] = await asyncio.to_thread(self.state.policy)
        return await self.scheduled_operation(job)


@pytest.fixture
def scheduled(sender):
    s, archive, key, reader, state = sender
    archive.ingest(event(account='send-fixture', chat='200', provider_id='2', body='Other synthetic chat'))
    s.set_messenger_operations('send-fixture', ['list_scheduled', 'cancel_scheduled'])
    yield s, key, state


def run(s, key, state, client, operation, chat_id='', parameters=None, request_id='scheduled-op-0001', digest=''):
    queued = s.execute_messenger_operation(key, account='send-fixture', operation=operation, chat_id=chat_id,
                                           parameters=parameters or {}, request_id=request_id, preview_digest=digest)
    assert asyncio.run(CommandWorker(state, Collector(state, client).execute_operation).once())
    return s.get_messenger_operation(key, queued['operation_id'])


def cancel(s, key, state, client, scheduled_id='7'):
    parameters = {'scheduled_message_id': scheduled_id}
    preview = s.preview_messenger_operation(key, account='send-fixture', operation='cancel_scheduled', chat_id=CHAT,
                                            parameters=parameters)
    return preview, run(s, key, state, client, 'cancel_scheduled', CHAT, parameters, 'scheduled-cancel-0001',
                        preview['preview_digest'])


# spec: ANYM-929
def test_both_are_telegram_send_operations_needing_read_and_the_gateway_catalog_lists_them():
    from messaging_service.commands import _required_permissions
    from messaging_service.messenger_operations import ADVANCED_ONLY, operation_manifest, validate_operation
    telegram = {row['operation']: row for row in operation_manifest('telegram')}
    whatsapp = {row['operation']: row for row in operation_manifest('whatsapp')}
    assert telegram['list_scheduled']['mutation'] is False and telegram['cancel_scheduled']['mutation'] is True
    assert telegram['list_scheduled']['supported'] and telegram['cancel_scheduled']['supported']
    assert not whatsapp['list_scheduled']['supported'] and not whatsapp['cancel_scheduled']['supported']
    assert 'list_scheduled' in ADVANCED_ONLY and 'cancel_scheduled' in ADVANCED_ONLY
    actions = json.loads(ACTIONS.read_text())
    for name in ('list_scheduled', 'cancel_scheduled'):
        assert _required_permissions('send', name) == actions[name] == ['read', 'send']
    assert validate_operation('telegram', 'cancel_scheduled', {'scheduled_message_id': '7'}) == {'scheduled_message_id': '7'}
    for bad in ('0', '07', '2147483648', 7, 'abc'):
        with pytest.raises(ValueError):
            validate_operation('telegram', 'cancel_scheduled', {'scheduled_message_id': bad})


# spec: ANYM-929
def test_list_without_chat_reads_deedkits_own_scheduled_sends_from_the_archive_only(scheduled):
    s, key, state = scheduled
    queued = scheduled_row(s, key, state, ahead(days=1), text=LONG)
    client = Client()
    receipt = run(s, key, state, client, 'list_scheduled')
    assert receipt['status'] == 'succeeded' and client.calls == []
    data = receipt['result']['data']
    assert data['source'] == 'archive'
    [listed] = data['scheduled']
    assert listed['send_id'] == queued['send_id'] and listed['scheduled_message_id'] == '7'
    assert listed['made_by_deedkit'] is True and listed['text'] == LONG[:200] and listed['chat_id'] == CHAT
    # A key scoped to another chat lists none of this chat's sends.
    other = s.issue_key('other-chat-agent', ['send-fixture'], permissions=['read', 'send'],
                        chats=[stable_id('chat', 'send-fixture', '200')])
    assert run(s, other, state, client, 'list_scheduled', request_id='scheduled-op-0002')['result']['data'][
        'scheduled'] == []


# spec: ANYM-929
def test_list_with_chat_reads_telegram_once_and_marks_which_messages_deedkit_made(scheduled):
    s, key, state = scheduled
    queued = scheduled_row(s, key, state, ahead(days=1), text=LONG)
    client = Client()
    receipt = run(s, key, state, client, 'list_scheduled', CHAT)
    assert client.calls == ['GetScheduledHistoryRequest']
    listed = {item['scheduled_message_id']: item for item in receipt['result']['data']['scheduled']}
    assert listed['7']['made_by_deedkit'] is True and listed['7']['send_id'] == queued['send_id']
    assert listed['99']['made_by_deedkit'] is False and 'send_id' not in listed['99']
    assert all(len(item['text']) == 200 and item['schedule_at'] for item in listed.values())


# spec: ANYM-929
def test_a_flood_wait_on_the_live_list_is_rate_limited_with_retry_after(scheduled):
    s, key, state = scheduled
    client = Client(read_error=errors.FloodWaitError(request=None, capture=30))
    receipt = run(s, key, state, client, 'list_scheduled', CHAT)
    assert receipt['status'] == 'failed' and receipt['error_code'] == 'rate_limited'
    assert receipt['result']['data']['retry_after'] == 30 and client.calls == ['GetScheduledHistoryRequest']


# spec: ANYM-929
def test_cancel_previews_the_bound_send_deletes_it_once_and_settles_scheduled_cancelled_by_the_agent(scheduled):
    s, key, state = scheduled
    queued = scheduled_row(s, key, state, ahead(days=1), text=LONG)

    def pushed():  # Telegram's pushed delete lands before the receipt: still the agent's cancel
        assert scheduled_send.apply_update(state, {'kind': 'deleted', 'chat': '100', 'ids': [7], 'sent': None}) == 1
    client = Client(on_delete=pushed)
    with pytest.raises(APIError, match='preview_required'):
        s.execute_messenger_operation(key, account='send-fixture', operation='cancel_scheduled', chat_id=CHAT,
                                      parameters={'scheduled_message_id': '7'}, request_id='scheduled-cancel-0000')
    preview, receipt = cancel(s, key, state, client)
    assert preview['mutation'] is True and preview['scheduled_send']['id'] == queued['send_id']
    assert preview['scheduled']['text'] == LONG[:200] and preview['scheduled']['scheduled_message_id'] == '7'
    assert client.calls == ['GetScheduledMessagesRequest', 'DeleteScheduledMessagesRequest']
    assert receipt['status'] == 'succeeded'
    assert receipt['result']['data'] == {'state': 'scheduled_cancelled', 'cancel_reason': 'agent',
                                         'send_id': queued['send_id'], 'scheduled_message_id': '7'}
    send = s.get_send_status(key, queued['send_id'])
    assert send['status'] == 'scheduled_cancelled' and send['cancel_reason'] == 'agent'
    assert [e['status'] for e in events_of(s, queued['send_id'])] == ['scheduled', 'scheduled_cancelled']


# spec: ANYM-929
def test_a_published_send_is_already_sent_with_its_message_id_at_preview_or_at_the_delete(scheduled):
    s, key, state = scheduled
    first = scheduled_row(s, key, state, ahead(days=1))
    scheduled_send.apply_update(state, {'kind': 'deleted', 'chat': '100', 'ids': [7], 'sent': [55]})
    with pytest.raises(APIError, match='already_sent') as refused:
        s.preview_messenger_operation(key, account='send-fixture', operation='cancel_scheduled', chat_id=CHAT,
                                      parameters={'scheduled_message_id': '7'})
    assert '55' in refused.value.next_step
    second = scheduled_row(s, key, state, ahead(days=2), provider_id='8', request_id='synthetic-request-0002')
    _, receipt = cancel(s, key, state, Client(held=(8,), sent=[56]), '8')
    assert receipt['result']['data']['state'] == 'already_sent' and receipt['result']['data']['provider_message_id'] == '56'
    send = s.get_send_status(key, second['send_id'])
    assert send['status'] == 'sent' and send['provider_message_id'] == '56' and first['send_id'] != second['send_id']


# spec: ANYM-929
def test_one_the_owner_already_deleted_settles_scheduled_cancelled_by_the_owner_without_a_delete(scheduled):
    s, key, state = scheduled
    queued = scheduled_row(s, key, state, ahead(days=1))
    client = Client(held=())
    _, receipt = cancel(s, key, state, client)
    assert client.calls == ['GetScheduledMessagesRequest']
    assert receipt['status'] == 'succeeded' and receipt['result']['data']['state'] == 'scheduled_cancelled'
    assert receipt['result']['data']['cancel_reason'] == 'owner_app'
    send = s.get_send_status(key, queued['send_id'])
    assert send['status'] == 'scheduled_cancelled' and send['cancel_reason'] == 'owner_app'


# spec: ANYM-929
def test_a_delete_timeout_is_unknown_and_never_retried(scheduled):
    s, key, state = scheduled
    queued = scheduled_row(s, key, state, ahead(days=1))
    client = Client(error=TimeoutError())
    _, receipt = cancel(s, key, state, client)
    assert receipt['status'] == 'unknown'
    assert client.calls.count('DeleteScheduledMessagesRequest') == 1
    assert not asyncio.run(CommandWorker(state, Collector(state, client).execute_operation).once())
    assert client.calls.count('DeleteScheduledMessagesRequest') == 1
    assert s.get_send_status(key, queued['send_id'])['status'] == 'scheduled'


# spec: ANYM-929
def test_a_scheduled_message_the_owner_made_in_the_app_is_not_scheduled_here_and_nothing_is_queued(scheduled):
    s, key, state = scheduled
    scheduled_row(s, key, state, ahead(days=1))
    for request in (s.preview_messenger_operation, s.execute_messenger_operation):
        extra = {'request_id': 'scheduled-cancel-0099', 'preview_digest': 'a' * 64} \
            if request == s.execute_messenger_operation else {}
        with pytest.raises(APIError, match='not_scheduled_here'):
            request(key, account='send-fixture', operation='cancel_scheduled', chat_id=CHAT,
                    parameters={'scheduled_message_id': '99'}, **extra)
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n'] == 0
