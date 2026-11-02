"""ANYM-666: a Telegram delete or reaction is settled from one bounded read after the one accepted request.
A fake Telethon client, the real command worker and the disposable database; no provider is touched."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from telethon import errors, functions, types

from messaging_service.commands import CommandWorker
from messaging_service.envelope import stable_id
from messaging_service.telegram_governor import ProviderRateLimited, RequestGovernor
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

    async def get_messages(self, peer, *, ids):
        self.calls.append(('get_messages', peer, ids))
        if self.deleted:
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
        chat='100', message='1'):
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
    assert asyncio.run(CommandWorker(state, provider).once())
    return s.get_messenger_operation(key, queued['operation_id'])


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
