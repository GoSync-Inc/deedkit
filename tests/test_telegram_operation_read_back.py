"""ANYM-666: a Telegram delete or reaction is settled from one bounded read after the one accepted request.
A fake Telethon client, the real command worker and the disposable database; no provider is touched."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from telethon import errors, functions, types

from messaging_service.blobs import BlobWriter
from messaging_service.collector_state import CaptureSpool
from messaging_service.commands import VERIFY, CommandWorker
from messaging_service.envelope import stable_id
from messaging_service.telegram_governor import ProviderRateLimited, RequestGovernor
from messaging_service.telegram_collector import TelegramCollector
from messaging_service.telegram_operations import execute
from test_archive import event
from test_messenger_commands import operation_args, operator as operator  # noqa: F401  (fixture)
from test_telegram_governor import Clock, State
from test_telegram_operations import Client

POLICY = {'enabled': True, 'discover_chats': True, 'include_chats': [], 'exclude_chats': []}
NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


def reactions_update(*emoji, chosen=True, min_copy=False):
    results = [types.ReactionCount(reaction=types.ReactionEmoji(e), count=1, chosen_order=0 if chosen else None)
        for e in emoji]
    return types.UpdateMessageReactions(peer=types.PeerUser(100), msg_id=1,
        reactions=types.MessageReactions(results=results, min=min_copy or None))


def updates(*items):
    return types.Updates(list(items), [], [], NOW, 0)


class ReadBackClient(Client):
    """present: what a read after the request returns; hooks run inside the fake provider call."""

    def __init__(self, present=False):
        super().__init__()
        self.message = NS(id=1, chat_id=100, message='hello', out=True, entities=[], date=NOW, media=None)
        self.present = present
        self.read_error = None
        self.after_delete = None
        self.deleted = False
        self.stall = False  # the read-back never answers; `cancelled` records that the worker's timeout reached it
        self.cancelled = False

    async def hang(self):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def __call__(self, request):
        if self.stall and isinstance(request, functions.messages.GetMessagesReactionsRequest):
            self.calls.append(request)
            await self.hang()
        return await super().__call__(request)

    async def get_messages(self, peer, *, ids):
        self.calls.append(('get_messages', peer, ids))
        if self.deleted:
            if self.stall:
                await self.hang()
            if self.read_error is not None:
                raise self.read_error
            return self.message if self.present else None
        return self.message

    async def delete_messages(self, peer, ids, **kwargs):
        self.calls.append(('delete_messages', peer, ids, kwargs))
        self.deleted = True
        if self.after_delete:
            self.after_delete()
        return []

    def mutations(self):
        return [c for c in self.calls if (isinstance(c, tuple) and c[0] in ('delete_messages', 'edit_message'))
            or isinstance(c, functions.messages.SendReactionRequest)]

    def reads(self):
        return [c for c in self.calls if isinstance(c, functions.messages.GetMessagesReactionsRequest)]


def run(operator, client, operation='delete_message', parameters=None, governor=None, request_id='read-back-0001',
        chat='100', message='1', collector=None):
    s, archive, key, state = operator
    message_id = stable_id('msg', 'command-fixture', chat, message)
    with s.pool.connection() as c:
        revision = c.execute('SELECT revision FROM messages WHERE id=%s', (message_id,)).fetchone()['revision']
    args = operation_args(operation=operation, parameters=parameters or {}, expected_revision=revision,
        chat_id=stable_id('chat', 'command-fixture', chat), message_id=message_id)
    draft = s.preview_messenger_operation(key, **args)
    queued = s.execute_messenger_operation(key, **args, request_id=request_id, preview_digest=draft['preview_digest'])

    async def provider(job):
        job['_policy'] = POLICY
        return await execute(client, job, governor=governor)
    if collector is not None:
        # The collector's own operation path (it reads the policy itself), as production dispatches it.
        collector.provider = NS(execute_operation=lambda job: execute(client, job, governor=governor), verified_self=None)
    assert asyncio.run(CommandWorker(state, collector.execute_operation if collector is not None else provider).once())
    return s.get_messenger_operation(key, queued['operation_id'])


EDITED = datetime(2026, 11, 4, 9, 30, tzinfo=timezone.utc)


def provider_message(text, *, entities=(), edited=EDITED, out=True, chat_id=100, message_id=1):
    """A Telethon-shaped copy of the fixture's archived outgoing message 1 in private chat 100."""
    return NS(id=message_id, chat_id=chat_id, sender_id=42, message=text, entities=list(entities),
        date=datetime(2026, 10, 1, 12, tzinfo=timezone.utc), edit_date=edited, out=out, peer_id=types.PeerUser(chat_id),
        file=None, document=None, photo=None, media=None)


def bold(text):
    return [types.MessageEntityBold(0, len(text))]


class EditClient(ReadBackClient):
    """ANYM-747: `before` is what the dispatch check reads, `after` what the read-back shows, `response` the edit's return."""

    def __init__(self, after=None, *, before=None, response=None):
        super().__init__()
        self.before = before or provider_message('Original synthetic text', edited=None)
        self.after, self.response, self.edited = after, response, False

    async def get_messages(self, peer, *, ids):
        self.calls.append(('get_messages', peer, ids))
        if not self.edited:
            return self.before
        if self.stall:
            await self.hang()
        if self.read_error is not None:
            raise self.read_error
        return self.after

    async def edit_message(self, peer, message, text, **kwargs):
        self.calls.append(('edit_message', peer, message, text, kwargs))
        self.edited = True
        return self.response

    def edit_reads(self):
        return [c for c in self.calls if isinstance(c, tuple) and c[0] == 'get_messages']


def collector_for(operator, spool):
    s, archive, key, state = operator
    collector = TelegramCollector(archive, 'command-fixture', NS(verified_self=None), spool,
        writer=BlobWriter(s.files, reserve_bytes=0), owner='synthetic-command-worker')
    collector.state, collector.policy = state, state.policy()
    return collector


def spooled(spool):
    items = []
    spool.drain(items.append)
    return items


EDIT = {'text': 'Edited synthetic text'}
UNKNOWN_EDIT = {'error': 'provider_result_unverified'}


# spec: ANYM-666
@pytest.mark.parametrize('archive_marks_deleted', [False, True])
def test_delete_settles_succeeded_when_the_message_is_gone(operator, archive_marks_deleted):
    s, archive, key, state = operator
    client = ReadBackClient(present=False)
    if archive_marks_deleted:
        # the collector sees the deletion right after the provider call, before the receipt settles
        client.after_delete = lambda: archive.ingest(event(account='command-fixture', chat='100', provider_id='1',
            direction='outgoing', body='', deleted=True, updated_at='2026-10-01T12:01:00Z'))
    receipt = run(operator, client)
    assert receipt['status'] == 'succeeded' and receipt['error_code'] is None
    assert receipt['result']['outcome'] == 'confirmed'
    assert receipt['result']['data'] == {'basis': 'provider_read_back', 'effect_verified': True,
        'evidence': {'message_present': False}}


# spec: ANYM-666
def test_delete_read_back_uses_the_scalar_ids_form(operator):
    client = ReadBackClient(present=False)
    run(operator, client)
    reads = [c for c in client.calls if isinstance(c, tuple) and c[0] == 'get_messages']
    assert len(reads) == 2 and all(type(c[2]) is int for c in reads)  # the dispatch read and the read-back


# spec: ANYM-666
def test_delete_stays_unknown_when_the_message_is_still_there(operator):
    client = ReadBackClient(present=True)
    receipt = run(operator, client)
    assert receipt['status'] == 'unknown' and receipt['error_code'] == 'operation_result_unknown'
    assert receipt['result']['outcome'] == 'submitted'
    assert receipt['result']['data']['effect_verified'] is False
    assert receipt['result']['data']['evidence'] == {'basis': 'provider_read_back', 'message_present': True}
    assert receipt['verify']['next_step'] == 'new_request' and 'new request ID' in receipt['verify']['decides']
    assert 'operation' not in receipt['verify']


# spec: ANYM-666
@pytest.mark.parametrize('error', [OSError('provider down'), asyncio.TimeoutError(), errors.RPCError(None, 'x', 500)])
def test_delete_stays_unknown_when_the_read_back_fails(operator, error):
    client = ReadBackClient()
    client.read_error = error
    receipt = run(operator, client)
    assert receipt['status'] == 'unknown' and receipt['error_code'] == 'operation_result_unknown'
    assert receipt['result']['outcome'] == 'submitted'
    assert receipt['result']['data'] == {'basis': 'provider_request_returned', 'effect_verified': False}
    assert receipt['verify']['operation'] == 'get_message_info'
    assert len(client.mutations()) == 1


# spec: ANYM-666
@pytest.mark.parametrize('operation', ['delete_message', 'send_reaction'])
def test_stalled_read_back_is_cut_by_the_worker_timeout_and_stays_unknown(operator, monkeypatch, operation):
    original = asyncio.wait_for

    async def short(awaitable, timeout):
        return await original(awaitable, 0.5 if timeout == 30 else timeout)  # the worker's 30 s operation timeout
    monkeypatch.setattr(asyncio, 'wait_for', short)
    client = ReadBackClient()
    client.stall = True
    receipt = run(operator, client, operation, {'emoji': '👍'} if operation == 'send_reaction' else None)
    assert receipt['status'] == 'unknown' and receipt['error_code'] == 'operation_result_unknown'
    assert client.cancelled  # the cancellation reached the read-back and was not swallowed
    # A swallowed cancellation would return `submitted` and store it; the worker's timeout path stores no result.
    assert receipt['result'] is None
    assert len(client.mutations()) == 1
    s = operator[0]
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n'] == 1


# spec: ANYM-747
@pytest.mark.parametrize('response', ['other_text', 'none'])
def test_edit_settles_succeeded_from_the_read_back(operator, response):
    client = EditClient(provider_message('Edited synthetic text'),
        response=provider_message('Edited synthetic text, normalised') if response == 'other_text' else None)
    receipt = run(operator, client, 'edit_message', EDIT)
    assert receipt['status'] == 'succeeded' and receipt['error_code'] is None
    assert receipt['result']['outcome'] == 'confirmed' and receipt['result']['data'] == {'basis': 'provider_read_back',
        'effect_verified': True, 'provider_message_id': '1', 'provider_chat_id': '100'}
    assert receipt['next_action'] == 'none' and 'verify' not in receipt
    assert len(client.mutations()) == 1
    reads = client.edit_reads()
    assert len(reads) == 2 and all(type(c[2]) is int for c in reads)  # the dispatch check and one scalar read-back


# spec: ANYM-747
def test_edit_read_back_strips_surrounding_whitespace_and_ignores_text_derived_entities(operator):
    text = 'See https://example.invalid'
    client = EditClient(provider_message(f'  {text}\n', entities=[types.MessageEntityUrl(6, 21)]))
    receipt = run(operator, client, 'edit_message', {'text': text})
    assert receipt['status'] == 'succeeded' and receipt['result']['data']['effect_verified'] is True


# spec: ANYM-747
@pytest.mark.parametrize('shown', ['old_text', 'missing', 'empty', 'other_chat', 'other_id', 'incoming', 'formatted'])
def test_edit_stays_unknown_when_the_read_back_does_not_show_it(operator, shown):
    after = {'old_text': lambda: provider_message('Original synthetic text', edited=None), 'missing': lambda: None,
        'empty': lambda: types.MessageEmpty(1, types.PeerUser(100)),
        'other_chat': lambda: provider_message('Edited synthetic text', chat_id=200),
        'other_id': lambda: provider_message('Edited synthetic text', message_id=7),
        'incoming': lambda: provider_message('Edited synthetic text', out=False),
        'formatted': lambda: provider_message('Edited synthetic text', entities=bold('Edited synthetic text'))}[shown]()
    # a formatted message edited plain: a read-back that still carries the bold is not the plain edit
    before = provider_message('Original synthetic text', entities=bold('Original synthetic text'), edited=None)
    client = EditClient(after, before=before if shown == 'formatted' else None)
    receipt = run(operator, client, 'edit_message', EDIT)
    assert receipt['status'] == 'unknown' and receipt['error_code'] == 'operation_result_unknown'
    assert receipt['result'] == {'outcome': 'unknown', 'data': UNKNOWN_EDIT}
    assert receipt['verify'] == VERIFY['edit_message']
    assert receipt['next_action'] == 'verify_provider_state_before_any_new_action'
    assert len(client.mutations()) == 1


# spec: ANYM-747
@pytest.mark.parametrize('error', [OSError('provider down'), asyncio.TimeoutError(), errors.RPCError(None, 'x', 500)])
def test_edit_stays_unknown_when_the_read_back_fails(operator, error):
    client = EditClient(provider_message('Edited synthetic text'))
    client.read_error = error
    receipt = run(operator, client, 'edit_message', EDIT)
    assert receipt['status'] == 'unknown' and receipt['result'] == {'outcome': 'unknown', 'data': UNKNOWN_EDIT}
    assert receipt['verify'] == {'operation': 'get_message_info', 'requires': 'read-live',
        'decides': 'text equal to the requested text means the edit applied'}
    assert len(client.mutations()) == 1


# spec: ANYM-747
def test_edit_read_back_flood_wait_carries_retry_after(operator):
    client = EditClient(provider_message('Edited synthetic text'))

    class Refusing:
        """A governor that serves the dispatch and the mutation but refuses the scoped read."""
        async def call(self, fn, phase):
            return await fn()

        async def scoped_call(self, fn, phase, scope):
            assert scope == 'read_back'
            raise ProviderRateLimited(45)
    receipt = run(operator, client, 'edit_message', EDIT, governor=Refusing())
    assert receipt['status'] == 'unknown' and receipt['result']['data'] == {**UNKNOWN_EDIT,
        'evidence': {'basis': 'provider_read_back', 'retry_after_s': 45}}
    assert receipt['verify'] == {**VERIFY['edit_message'], 'retry_after_s': 45}
    assert len(client.mutations()) == 1


# spec: ANYM-747
def test_edit_whose_requested_state_was_already_there_stays_unknown(operator):
    client = EditClient(provider_message('Edited synthetic text'),
        before=provider_message(' Edited synthetic text ', edited=None))
    receipt = run(operator, client, 'edit_message', EDIT)
    assert receipt['status'] == 'unknown' and receipt['result'] == {'outcome': 'unknown', 'data': UNKNOWN_EDIT}
    assert len(client.mutations()) == 1 and len(client.edit_reads()) == 1  # no read can prove this request did it


# spec: ANYM-747
def test_plain_edit_of_the_same_text_confirms_the_formatting_removal(operator):
    text = 'Edited synthetic text'
    client = EditClient(provider_message(text), before=provider_message(text, entities=bold(text), edited=None))
    receipt = run(operator, client, 'edit_message', {'text': text})
    assert receipt['status'] == 'succeeded' and receipt['result']['data']['basis'] == 'provider_read_back'
    assert len(client.mutations()) == 1


# spec: ANYM-747
def test_stalled_edit_read_back_is_cut_by_the_worker_timeout(operator, monkeypatch):
    original = asyncio.wait_for

    async def short(awaitable, timeout):
        return await original(awaitable, 0.5 if timeout == 30 else timeout)  # the worker's 30 s operation timeout
    monkeypatch.setattr(asyncio, 'wait_for', short)
    client = EditClient(provider_message('Edited synthetic text'))
    client.stall = True
    receipt = run(operator, client, 'edit_message', EDIT)
    assert receipt['status'] == 'unknown' and receipt['error_code'] == 'operation_result_unknown'
    assert client.cancelled and receipt['result'] is None  # the cancellation reached the read and was not swallowed
    assert len(client.mutations()) == 1


# spec: ANYM-747
def test_confirmed_edit_queues_one_archive_item_and_an_unknown_one_none(operator, tmp_path):
    spool = CaptureSpool(tmp_path / 'capture', reserve_bytes=0)
    collector = collector_for(operator, spool)
    receipt = run(operator, EditClient(provider_message('Edited synthetic text')), 'edit_message', EDIT,
        collector=collector)
    assert receipt['status'] == 'succeeded' and receipt['result']['data']['archive'] == 'queued'
    [item] = spooled(spool)
    assert item['kind'] == 'message' and item['peer_kind'] == 'user'
    envelope = item['envelope']
    assert (envelope['account'], envelope['chat'], envelope['provider_id']) == ('command-fixture', '100', '1')
    assert envelope['body'] == 'Edited synthetic text' and envelope['direction'] == 'outgoing'
    assert envelope['updated_at'] == '2026-11-04T09:30:00+00:00' and envelope['origin'] == 'live'
    unknown = run(operator, EditClient(provider_message('Original synthetic text', edited=None)), 'edit_message',
        EDIT, collector=collector, request_id='read-back-0002')
    assert unknown['status'] == 'unknown' and 'archive' not in unknown['result']['data']
    assert spooled(spool) == []


# spec: ANYM-747
def test_a_failing_archive_hand_off_keeps_the_edit_succeeded(operator, tmp_path):
    spool = CaptureSpool(tmp_path / 'capture', reserve_bytes=0)
    collector = collector_for(operator, spool)

    def broken(item):
        raise OSError('disk gone')
    spool.put = broken
    receipt = run(operator, EditClient(provider_message('Edited synthetic text')), 'edit_message', EDIT,
        collector=collector)
    assert receipt['status'] == 'succeeded' and receipt['error_code'] is None
    assert receipt['result']['data']['archive'] == 'not_queued'
    assert not collector.failed.is_set() and collector.capture_count == 0


# spec: ANYM-666
def test_reaction_with_a_malformed_update_keeps_submitted(operator):
    client = ReadBackClient()
    bad = types.UpdateMessageReactions(peer=types.PeerUser(100), msg_id=1,
        reactions=types.MessageReactions(results=None))
    client.responses[functions.messages.SendReactionRequest] = updates(bad)
    client.responses[functions.messages.GetMessagesReactionsRequest] = updates(bad)
    receipt = run(operator, client, 'send_reaction', {'emoji': '👍'})
    assert receipt['status'] == 'unknown' and receipt['error_code'] == 'operation_result_unknown'
    assert receipt['result']['outcome'] == 'submitted'
    assert receipt['result']['data'] == {'basis': 'provider_request_returned', 'effect_verified': False}
    assert len(client.mutations()) == 1


# spec: ANYM-666
def test_reaction_settles_succeeded_from_the_send_response(operator):
    client = ReadBackClient()
    client.responses[functions.messages.SendReactionRequest] = updates(reactions_update('👍'))
    receipt = run(operator, client, 'send_reaction', {'emoji': '👍'})
    assert receipt['status'] == 'succeeded' and receipt['error_code'] is None
    assert receipt['result']['data']['basis'] == 'provider_response' and receipt['result']['data']['effect_verified'] is True
    assert client.reads() == []


# spec: ANYM-666
def test_reaction_settles_succeeded_from_the_read_back(operator):
    client = ReadBackClient()
    client.responses[functions.messages.GetMessagesReactionsRequest] = updates(reactions_update('👍'))
    receipt = run(operator, client, 'send_reaction', {'emoji': '👍'})
    assert receipt['status'] == 'succeeded' and receipt['result']['data']['basis'] == 'provider_read_back'
    assert len(client.reads()) == 1 and len(client.mutations()) == 1


# spec: ANYM-666
def test_reaction_removal_settles_succeeded_when_nothing_is_chosen(operator):
    client = ReadBackClient()
    # other people's reactions stay; none is chosen by this account
    client.responses[functions.messages.GetMessagesReactionsRequest] = updates(reactions_update('🔥', chosen=False))
    receipt = run(operator, client, 'send_reaction', {'emoji': ''})
    assert receipt['status'] == 'succeeded' and receipt['result']['data']['evidence'] == {'reaction_present': False}
    # a removal with the reaction still chosen is not done
    other = ReadBackClient()
    other.responses[functions.messages.GetMessagesReactionsRequest] = updates(reactions_update('👍'))
    again = run(operator, other, 'send_reaction', {'emoji': ''}, request_id='read-back-0002')
    assert again['status'] == 'unknown'
    assert again['result']['data']['evidence'] == {'basis': 'provider_read_back', 'reaction_present': True}


# spec: ANYM-666
def test_reaction_stays_unknown_when_the_update_is_absent(operator):
    client = ReadBackClient()  # neither the response nor the read-back carries an UpdateMessageReactions
    receipt = run(operator, client, 'send_reaction', {'emoji': '👍'})
    assert receipt['status'] == 'unknown' and receipt['error_code'] == 'operation_result_unknown'
    assert receipt['result']['data'] == {'basis': 'provider_request_returned', 'effect_verified': False}
    assert receipt['verify']['operation'] == 'get_reactions' and receipt['verify']['requires'] == 'read-live'
    # a partial (min) copy of the reactions is not authoritative either
    partial = ReadBackClient()
    partial.responses[functions.messages.SendReactionRequest] = updates(reactions_update('👍', min_copy=True))
    assert run(operator, partial, 'send_reaction', {'emoji': '👍'}, request_id='read-back-0002')['status'] == 'unknown'


# spec: ANYM-666
def test_reaction_stays_unknown_when_the_read_back_fails(operator):
    client = ReadBackClient()
    client.responses[functions.messages.GetMessagesReactionsRequest] = OSError('provider down')
    receipt = run(operator, client, 'send_reaction', {'emoji': '👍'})
    assert receipt['status'] == 'unknown' and receipt['result']['outcome'] == 'submitted'
    assert 'evidence' not in receipt['result']['data'] and len(client.mutations()) == 1
    # a read-back that shows the reaction not chosen says so and sends no one back to check again
    other = ReadBackClient()
    other.responses[functions.messages.GetMessagesReactionsRequest] = updates(reactions_update('👍', chosen=False))
    shown = run(operator, other, 'send_reaction', {'emoji': '👍'}, request_id='read-back-0002')
    assert shown['result']['data']['evidence'] == {'basis': 'provider_read_back', 'reaction_present': False}
    assert shown['verify']['next_step'] == 'new_request'


# spec: ANYM-666
@pytest.mark.parametrize('operation', ['delete_message', 'send_reaction'])
def test_succeeded_receipt_has_no_unknown_leftovers(operator, operation):
    client = ReadBackClient()
    client.responses[functions.messages.SendReactionRequest] = updates(reactions_update('👍'))
    receipt = run(operator, client, operation, {'emoji': '👍'} if operation == 'send_reaction' else None)
    assert receipt['status'] == 'succeeded' and receipt['error_code'] is None
    assert receipt['result']['outcome'] == 'confirmed' and receipt['result']['data']['effect_verified'] is True
    assert receipt['next_action'] == 'none' and 'verify' not in receipt
    text = json.dumps(receipt)
    assert 'operation_result_unknown' not in text and 'submitted' not in text and 'provider_request_returned' not in text


# spec: ANYM-666
@pytest.mark.parametrize('case', ['gone', 'present', 'read_fails', 'reaction_response', 'reaction_read_back',
    'reaction_read_fails'])
def test_read_back_never_issues_a_second_mutation(operator, case):
    client = ReadBackClient(present=case == 'present')
    operation, parameters = 'delete_message', None
    if case == 'read_fails':
        client.read_error = OSError('provider down')
    if case.startswith('reaction'):
        operation, parameters = 'send_reaction', {'emoji': '👍'}
        if case == 'reaction_response':
            client.responses[functions.messages.SendReactionRequest] = updates(reactions_update('👍'))
        elif case == 'reaction_read_back':
            client.responses[functions.messages.GetMessagesReactionsRequest] = updates(reactions_update('👍'))
        else:
            client.responses[functions.messages.GetMessagesReactionsRequest] = OSError('provider down')
    run(operator, client, operation, parameters)
    assert len(client.mutations()) == 1
    s = operator[0]
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n'] == 1
        assert c.execute('SELECT count(*) AS n FROM outgoing_messages').fetchone()['n'] == 0


# spec: ANYM-666
def test_unknown_delete_names_the_verifying_read_and_its_permission(operator):
    client = ReadBackClient()
    client.read_error = OSError('provider down')
    receipt = run(operator, client)
    assert receipt['verify'] == {'operation': 'get_message_info', 'requires': 'read-live',
        'decides': 'exists:false means deleted'}
    assert receipt['next_action'] == 'verify_provider_state_before_any_new_action' and receipt['automatic_retry'] is False


# spec: ANYM-666
def test_flood_wait_read_back_carries_retry_after(operator):
    client = ReadBackClient()

    class Refusing:
        """A governor that serves the dispatch and the mutation but refuses the scoped read."""
        async def call(self, fn, phase):
            return await fn()

        async def scoped_call(self, fn, phase, scope):
            assert scope == 'read_back'
            raise ProviderRateLimited(45)
    receipt = run(operator, client, governor=Refusing())
    assert receipt['status'] == 'unknown' and receipt['result']['data']['evidence'] == {
        'basis': 'provider_read_back', 'retry_after_s': 45}
    assert receipt['verify']['retry_after_s'] == 45 and receipt['verify']['operation'] == 'get_message_info'
    assert len(client.mutations()) == 1


# spec: ANYM-666
def test_get_message_info_on_a_gone_message_says_exists_false(operator):
    s, archive, key, state = operator
    client = ReadBackClient()
    client.deleted = True  # every read answers "gone"
    receipt = run(operator, client, 'get_message_info')
    assert receipt['status'] == 'succeeded' and receipt['result']['data']['exists'] is False
    assert receipt['result']['data']['provider_message_id'] == '1'
    # the archive already marks the message deleted: the read is still a valid request
    archive.ingest(event(account='command-fixture', chat='100', provider_id='1', direction='outgoing', body='',
        deleted=True, updated_at='2026-10-01T12:01:00Z'))
    again = run(operator, client, 'get_message_info', request_id='read-back-0002')
    assert again['status'] == 'succeeded' and again['result']['data']['exists'] is False
    # a wrong reference (a different message comes back) still fails as before
    wrong = ReadBackClient()
    wrong.message = NS(id=7, chat_id=100, message='x', out=True, entities=[], date=NOW, media=None)
    failed = run(operator, wrong, 'get_message_info', request_id='read-back-0003')
    assert failed['status'] == 'failed' and failed['error_code'] == 'operation_rejected'
    present = run(operator, ReadBackClient(), 'get_message_info', request_id='read-back-0004')
    assert present['result']['data']['exists'] is True


# spec: ANYM-666
def test_read_back_flood_wait_does_not_pause_sends(operator):
    clock = Clock()
    state = State(clock)
    governor = RequestGovernor(state, clock=clock, sleep=clock.sleep)
    client = ReadBackClient()
    client.read_error = errors.FloodWaitError(None, capture=30)
    receipt = run(operator, client, governor=governor)
    assert receipt['status'] == 'unknown'
    assert receipt['result']['data']['evidence'] == {'basis': 'provider_read_back', 'retry_after_s': 30}
    assert state.until == 0  # no account cooldown: sends are not paused
    assert governor.scoped_remaining('read_back') > 0 and governor.scoped_remaining('send') == 0
    assert len(client.mutations()) == 1
