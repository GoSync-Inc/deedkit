"""Characterization of Telegram `_execute` branches the other suites leave uncovered (refactor W0-4).

These tests pin today's behaviour with synthetic clients; they never connect a provider. Where a
branch looks odd the test records it as it is and does not judge it.
"""
import asyncio
import hashlib
import os
from datetime import datetime, timezone
from itertools import chain, repeat
from types import SimpleNamespace as NS

import pytest
from telethon import functions, types

from messaging_service import telegram_operations
from messaging_service.telegram_operations import execute
from test_telegram_operations import (
    EXPECTED, SUPERGROUP, Client, GroupClient, LinkClient, _channel, job, link_run, run)

BASIC = '-5'
DATE = datetime(2026, 10, 20, tzinfo=timezone.utc)


def reads_only(client):
    """True when the client saw peer resolution and nothing else (no read request, no mutation)."""
    return bool(client.calls) and all(isinstance(c, tuple) and c[0] == 'get_input_entity' for c in client.calls)


# spec: ANYM-549
@pytest.mark.parametrize('candidate', [
    [],
    {**job('pin_message'), 'parameters': []},
    job('pin_message', {'raw_api': 1}),
    job('pin_message', expires_at=True),
    job('pin_message', expires_at='soon'),
    job('pin_message', expires_at=float('nan')),
    job('pin_message', expires_at=float('inf')),
], ids=['job-not-object', 'parameters-not-object', 'unknown-parameter', 'expiry-bool', 'expiry-text',
        'expiry-nan', 'expiry-inf'])
def test_malformed_envelope_rejects_before_any_provider_call(candidate):
    client = Client()
    with pytest.raises(Exception, match='invalid_parameters'):
        asyncio.run(execute(client, candidate))
    assert client.calls == []


# spec: ANYM-549
def test_non_callable_preflight_fails_closed_before_any_provider_call():
    client = Client()
    with pytest.raises(Exception, match='operation_access_changed'):
        run(client, 'pin_message', _preflight='not callable')
    assert client.calls == []


# spec: ANYM-549
@pytest.mark.parametrize('operation, parameters, extra', [
    ('set_group_messages_admins_only', {'enabled': 'yes'}, {'provider_chat': SUPERGROUP}),
    ('set_group_info_admins_only', {}, {'provider_chat': SUPERGROUP}),
    ('approve_join_requests', {'user_ids': []}, {'provider_chat': SUPERGROUP}),
    ('approve_join_requests', {'user_ids': ['5', '5']}, {'provider_chat': SUPERGROUP}),
    ('reject_join_requests', {'user_ids': [str(i) for i in range(1, 22)]}, {'provider_chat': SUPERGROUP}),
    ('reject_join_requests', {'user_ids': [5]}, {'provider_chat': SUPERGROUP}),
    ('send_poll', {'question': 'Q', 'options': ['a', 'b'], 'multiple': 'yes'}, {}),
    ('send_file', {'mode': 'gif'}, {}),
    ('forward_message', {}, {'source_provider_chat': '200', 'source_provider_message': str(2**31)}),
], ids=['setter-enabled-not-bool', 'setter-enabled-missing', 'join-batch-empty', 'join-batch-duplicate',
        'join-batch-too-long', 'join-batch-non-string', 'poll-multiple-not-bool', 'file-mode-unknown',
        'forward-source-message-too-large'])
def test_parameter_shape_is_rejected_before_any_provider_call(operation, parameters, extra):
    client = Client()
    with pytest.raises(Exception, match='invalid_parameters'):
        run(client, operation, parameters, **extra)
    assert client.calls == []


# spec: ANYM-168
@pytest.mark.parametrize('operation, parameters, extra, code', [
    ('edit_message', {'text': 'x' * 4001}, {}, 'invalid_parameters'),
    ('send_file', {'caption': 'x' * 1025}, {}, 'invalid_parameters'),
    ('add_contact', {'phone': '12', 'first_name': 'A'}, {}, 'invalid_parameters'),
    ('block_contact', {'user_id': '0'}, {}, 'invalid_parameters'),
    ('list_contacts', {'limit': 101}, {}, 'invalid_parameters'),
    ('list_topics', {'offset': -1}, {}, 'invalid_parameters'),
    ('list_topics', {'offset': 5}, {}, 'unsupported_operation'),
    ('mute_chat', {'seconds': 0}, {}, 'invalid_parameters'),
    ('set_slow_mode', {'seconds': 15}, {'provider_chat': SUPERGROUP}, 'invalid_parameters'),
    ('set_slow_mode', {'seconds': True}, {'provider_chat': SUPERGROUP}, 'invalid_parameters'),
    ('send_file', {}, {'registered_file_path': 'relative.bin'}, 'attachment_unavailable'),
    ('send_reaction', {'emoji': 'a b'}, {}, 'invalid_parameters'),
    ('send_reaction', {}, {}, 'invalid_parameters'),
    ('send_poll', {'question': 'Q' * 256, 'options': ['a', 'b']}, {}, 'invalid_parameters'),
    ('send_poll', {'question': 'Q', 'options': ['a']}, {}, 'invalid_parameters'),
    ('send_poll', {'question': 'Q', 'options': ['a', 'a']}, {}, 'invalid_parameters'),
    ('vote_poll', {'option_indices': [10]}, {}, 'invalid_parameters'),
    ('vote_poll', {'option_indices': [1, 1]}, {}, 'invalid_parameters'),
    ('send_location', {'latitude': 91, 'longitude': 0}, {}, 'invalid_parameters'),
    ('send_location', {'latitude': 0, 'longitude': float('nan')}, {}, 'invalid_parameters'),
    ('create_group', {'title': 'G', 'user_ids': ['5', '5']}, {}, 'invalid_parameters'),
    ('create_group', {'user_ids': ['5']}, {}, 'invalid_parameters'),
    ('check_chat_link', {'link': 'not a link'}, {}, 'invalid_parameters'),
    ('join_chat', {}, {}, 'invalid_parameters'),
], ids=['text-too-long', 'caption-too-long', 'phone-too-short', 'user-id-zero', 'limit-over-100',
        'offset-negative', 'topics-offset-not-zero', 'mute-seconds-zero', 'slow-mode-not-a-step',
        'slow-mode-bool', 'file-path-relative', 'reaction-with-space', 'reaction-missing', 'poll-question-over-255',
        'poll-one-option', 'poll-duplicate-options', 'vote-index-out-of-range', 'vote-duplicate-index',
        'latitude-over-90', 'longitude-nan', 'group-duplicate-invitee', 'group-title-missing', 'link-malformed',
        'link-missing'])
def test_each_parameter_check_answers_its_code_before_any_provider_call(operation, parameters, extra, code):
    """W5-5a pin: one row per check of the parameter table, with the code each answers today."""
    client = Client()
    with pytest.raises(Exception, match=code):
        run(client, operation, parameters, **extra)
    assert client.calls == []


# spec: ANYM-168
def test_send_file_closes_the_descriptor_of_a_refused_attachment(tmp_path, monkeypatch):
    pipe = tmp_path / 'pipe'
    os.mkfifo(pipe)
    opened = []

    class Proxy:
        """The module's `os`, except that fdopen records each stream it opens."""
        def __getattr__(self, name):
            return getattr(os, name)

        @staticmethod
        def fdopen(fd, mode):
            opened.append(os.fdopen(fd, mode))
            return opened[-1]

    monkeypatch.setattr(telegram_operations, 'os', Proxy())
    with pytest.raises(Exception, match='attachment_unavailable'):
        run(Client(), 'send_file', {'mode': 'document'}, registered_file_path=str(pipe))
    assert len(opened) == 1 and opened[0].closed


# spec: ANYM-549
def test_send_file_rejects_a_registered_path_that_is_not_a_regular_file(tmp_path):
    client = Client()
    with pytest.raises(Exception, match='attachment_unavailable'):
        run(client, 'send_file', {'mode': 'document'}, registered_file_path=str(tmp_path))
    assert client.calls == []


# spec: ANYM-549
def test_send_file_rejects_a_registered_path_that_is_a_pipe(tmp_path):
    pipe = tmp_path / 'pipe'
    os.mkfifo(pipe)
    client = Client()
    with pytest.raises(Exception, match='attachment_unavailable'):
        run(client, 'send_file', {'mode': 'document'}, registered_file_path=str(pipe))
    assert client.calls == []


# spec: ANYM-549
def test_send_file_stops_when_the_file_is_shorter_than_its_reported_size(tmp_path, monkeypatch):
    original = tmp_path / 'original.bin'
    original.write_bytes(b'synthetic')

    class Proxy:
        """The module's `os`, except that fstat reports a larger file than the descriptor holds."""
        def __getattr__(self, name):
            return getattr(os, name)

        @staticmethod
        def fstat(fd):
            real = os.fstat(fd)
            return NS(st_mode=real.st_mode, st_size=real.st_size + 10, st_mtime_ns=real.st_mtime_ns)

    monkeypatch.setattr(telegram_operations, 'os', Proxy())
    client = Client()
    with pytest.raises(Exception, match='attachment_unavailable'):
        run(client, 'send_file', {'mode': 'document'}, registered_file_path=str(original),
            file_digest=hashlib.sha256(b'synthetic').hexdigest())
    assert reads_only(client)


# spec: ANYM-549
def test_forward_answered_from_another_chat_reports_the_provider_reference_mismatch():
    class Misrouted(Client):
        async def forward_messages(self, peer, messages, from_peer=None, **kwargs):
            self.calls.append(('forward_messages', peer, messages, from_peer, kwargs))
            return [NS(id=23, chat_id=999, message='source', out=True)]

    client = Misrouted()
    client.messages[(300, 23)] = NS(id=23, chat_id=300, message='source', out=False)
    result = run(client, 'forward_message', {}, provider_chat='200', source_provider_chat='300',
                 source_provider_message='23')
    assert result == {'outcome': 'unknown', 'data': {'error': 'provider_reference_mismatch'}}
    assert any(isinstance(c, tuple) and c[0] == 'forward_messages' for c in client.calls)


# spec: ANYM-549
def test_reaction_with_a_false_provider_answer_is_unverified_not_submitted():
    client = Client()
    client.responses[functions.messages.SendReactionRequest] = False
    result = run(client, 'send_reaction', {'emoji': '👍'})
    assert result == {'outcome': 'unknown', 'data': {'error': 'provider_result_unverified'}}


# spec: ANYM-549
def test_send_contact_without_a_first_name_rejects_after_resolving_the_peer_only():
    client = Client()
    with pytest.raises(Exception, match='invalid_parameters'):
        run(client, 'send_contact', {'phone': '+15550100123'})
    assert reads_only(client)


# spec: ANYM-549
def test_vote_on_a_message_without_a_poll_is_unsupported_and_sends_no_vote():
    client = Client()
    with pytest.raises(Exception, match='unsupported_operation'):
        run(client, 'vote_poll', {'option_indices': [0]})
    assert not any(isinstance(c, functions.messages.SendVoteRequest) for c in client.calls)


# spec: ANYM-242
@pytest.mark.parametrize('operation, parameters', [
    ('rename_group', {'title': 'Title'}),
    ('set_group_description', {'description': 'About'}),
    ('add_participant', {'user_id': '7'}),
    ('demote_participant', {'user_id': '7'}),
    ('leave_group', {}),
    ('revoke_invite_link', {}),
    ('set_group_messages_admins_only', {'enabled': True}),
    ('set_slow_mode', {'seconds': 10}),
    ('list_join_requests', {}),
    ('approve_join_requests', {'user_ids': ['7']}),
])
def test_group_operations_on_a_private_chat_are_unsupported_before_any_group_request(operation, parameters):
    client = Client()
    with pytest.raises(Exception, match='unsupported_operation'):
        run(client, operation, parameters)
    assert reads_only(client)


# spec: ANYM-242
def test_get_invite_link_on_a_private_chat_is_unsupported():
    client = Client()
    with pytest.raises(Exception, match='unsupported_operation'):
        run(client, 'get_invite_link')
    assert reads_only(client)


# spec: ANYM-242
def test_leave_a_channel_sends_the_channel_request_and_claims_only_submission():
    client = Client()
    result = run(client, 'leave_group', provider_chat=SUPERGROUP)
    assert [type(c) for c in client.calls if not isinstance(c, tuple)] == [functions.channels.LeaveChannelRequest]
    assert result['outcome'] == 'submitted' and result['data']['effect_verified'] is False


# spec: ANYM-242
def test_group_setter_fails_when_the_provider_answer_lacks_the_chat():
    class Missing(GroupClient):
        async def __call__(self, request):
            if isinstance(request, functions.channels.GetFullChannelRequest):
                self.calls.append(request)
                return NS(full_chat=NS(slowmode_seconds=None, about=''), chats=[], users=[])
            return await super().__call__(request)

    client = Missing()
    with pytest.raises(Exception, match='provider_reference_mismatch'):
        run(client, 'set_group_messages_admins_only', {'enabled': True}, provider_chat=SUPERGROUP)
    assert client.mutations() == []


# spec: ANYM-242
def test_group_setter_on_a_chat_whose_rights_the_provider_did_not_report_is_unsupported():
    client = GroupClient()
    client.rights = None
    with pytest.raises(Exception, match='unsupported_operation'):
        run(client, 'set_group_messages_admins_only', {'enabled': True}, provider_chat=SUPERGROUP)
    assert client.mutations() == []


# spec: ANYM-242
def test_join_request_list_stops_at_the_receipt_byte_budget_and_says_there_is_more():
    client = GroupClient()
    importers = [types.ChatInviteImporter(user_id=1000 + i, date=DATE, requested=True, about='я' * 200)
                 for i in range(100)]
    users = [types.User(id=1000 + i, first_name='И' * 64, last_name='Ф' * 64) for i in range(100)]
    client.responses[functions.messages.GetChatInviteImportersRequest] = types.messages.ChatInviteImporters(
        count=100, importers=importers, users=users)
    data = run(client, 'list_join_requests', {'limit': 100}, provider_chat=SUPERGROUP)['data']
    assert 0 < len(data['requests']) < 100 and data['has_more'] is True


# spec: ANYM-242
def test_join_batch_with_an_exhausted_budget_attempts_nothing():
    class Governor:
        def __init__(self):
            self.ticks = chain([0.0], repeat(100.0))
            self.clock = lambda: next(self.ticks)

        async def call(self, fn, phase):
            return await fn()

    client = GroupClient()
    result = asyncio.run(execute(client, job('approve_join_requests', {'user_ids': ['201', '202']},
                                             provider_chat=SUPERGROUP), governor=Governor()))
    assert result == {'outcome': 'unknown', 'data': {'all_applied': False, 'results': [
        {'id': '201', 'state': 'not_attempted'}, {'id': '202', 'state': 'not_attempted'}]}}
    assert client.mutations() == []


# spec: ANYM-242
def test_join_batch_never_swallows_a_cancellation_from_the_session_lookup():
    class Session:
        def get_input_entity(self, peer):
            raise asyncio.CancelledError()

    client = GroupClient()
    client.session = Session()
    with pytest.raises(asyncio.CancelledError):
        run(client, 'approve_join_requests', {'user_ids': ['201']}, provider_chat=SUPERGROUP)
    assert client.mutations() == []


# spec: ANYM-549
def test_basic_chat_participants_are_the_listed_members_that_the_provider_sent_users_for():
    client = Client()
    client.responses[functions.messages.GetFullChatRequest] = NS(
        full_chat=NS(participants=types.ChatParticipants(chat_id=5, version=1, participants=[
            types.ChatParticipant(user_id=7, inviter_id=1, date=DATE)])),
        users=[types.User(id=7, first_name='Member'), types.User(id=8, first_name='Stranger')])
    data = run(client, 'list_participants', {'limit': 10}, provider_chat=BASIC)['data']
    assert [p['id'] for p in data['participants']] == ['7']
    assert data['reported_count'] == 1 and data['next_offset'] is None
    assert data['coverage'] == 'provider_page_not_complete_membership'


# spec: ANYM-549
def test_reactions_ignore_updates_for_other_messages_and_report_unknown_state():
    other = types.UpdateMessageReactions(peer=types.PeerUser(100), msg_id=99, reactions=types.MessageReactions(results=[]))
    client = Client()
    client.responses[functions.messages.GetMessagesReactionsRequest] = types.Updates(
        [other, types.UpdateMessageID(1, 2)], [], [], DATE, 0)
    assert run(client, 'get_reactions')['data'] == {
        'provider_message_id': '23', 'reactions': {'state': 'unknown', 'counts': [], 'recent': []}}


# spec: ANYM-243
def test_invite_answer_that_is_neither_invite_nor_known_chat_is_a_reference_mismatch():
    client = LinkClient(invite=NS())
    with pytest.raises(Exception, match='provider_reference_mismatch'):
        link_run(client, 'check_chat_link', '+AbCd1234')
    assert client.joins() == []


# spec: ANYM-243
def test_join_by_public_name_with_unknown_membership_refuses_without_join_call():
    client = LinkClient(entity=_channel(left=None, megagroup=True, join_request=None))
    with pytest.raises(Exception, match='operation_target_changed'):
        link_run(client, 'join_chat', '@synthetic_link', expected_chat=EXPECTED)
    assert client.joins() == []


# spec: ANYM-549
def test_group_creation_rejects_an_invitee_that_is_not_a_user_peer():
    class Channelish(Client):
        async def get_input_entity(self, peer):
            return types.InputPeerChannel(7, 1) if peer == 7 else await super().get_input_entity(peer)

    client = Channelish()
    with pytest.raises(Exception, match='invalid_parameters'):
        run(client, 'create_group', {'title': 'Synthetic', 'user_ids': ['7']}, provider_chat=None)
    assert not any(isinstance(c, functions.messages.CreateChatRequest) for c in client.calls)


# spec: ANYM-549
@pytest.mark.parametrize('operation', ['delete_contact', 'block_contact', 'unblock_contact'])
def test_contact_operations_reject_a_target_that_is_not_a_user_peer(operation):
    class Channelish(Client):
        async def get_input_entity(self, peer):
            return types.InputPeerChannel(7, 1) if peer == 7 else await super().get_input_entity(peer)

    client = Channelish()
    with pytest.raises(Exception, match='invalid_parameters'):
        run(client, operation, {'user_id': '7'}, provider_chat=None)
    assert not [c for c in client.calls if not isinstance(c, tuple)]


# spec: ANYM-549
def test_add_contact_needs_phone_and_first_name_and_never_reaches_the_provider_without_them():
    client = Client()
    with pytest.raises(Exception, match='invalid_parameters'):
        run(client, 'add_contact', {'phone': '+15550100123'}, provider_chat=None)
    assert client.calls == []


# spec: ANYM-549
def test_add_contact_confirms_with_the_imported_user_the_provider_matched_to_the_request():
    class Importing(Client):
        async def __call__(self, request):
            if isinstance(request, functions.contacts.ImportContactsRequest):
                self.calls.append(request)
                return NS(imported=[NS(client_id=request.contacts[0].client_id, user_id=77),
                                    NS(client_id=-1, user_id=78)])
            return await super().__call__(request)

    result = run(Importing(), 'add_contact', {'phone': '+15550100123', 'first_name': 'Synthetic'}, provider_chat=None)
    assert result == {'outcome': 'confirmed', 'data': {'user_id': '77', 'basis': 'provider_imported_contact'}}


# spec: ANYM-549
def test_update_profile_returns_the_projected_profile_when_the_provider_returned_the_own_user():
    client = Client()
    client.responses[functions.account.UpdateProfileRequest] = types.User(id=42, first_name='Renamed', is_self=True)
    self_user = types.User(id=42, first_name='Before', is_self=True)
    result = asyncio.run(execute(client, job('update_profile', {'first_name': 'Renamed'}, provider_chat=None),
                                 verified_self=self_user))
    assert result['outcome'] == 'confirmed'
    assert result['data']['basis'] == 'provider_profile' and result['data']['profile']['first_name'] == 'Renamed'
