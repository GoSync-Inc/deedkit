"""Typed Telegram operation tests; never connect a provider session."""
import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from telethon import errors, functions, types

from messaging_service.telegram_operations import execute


class Client:
    def __init__(self):
        self.calls = []
        self.responses = {}
        self.messages = {}
        self.message = NS(id=23, chat_id=100, message='hello', out=True, entities=[],
                          date=datetime(2026, 10, 14, tzinfo=timezone.utc), media=None)

    async def get_input_entity(self, peer):
        self.calls.append(('get_input_entity', peer))
        if peer <= -1000000000000:
            return types.InputPeerChannel(-peer - 1000000000000, 1)
        if peer < 0:
            return types.InputPeerChat(-peer)
        return types.InputPeerUser(peer, 1)

    async def get_entity(self, peer):
        self.calls.append(('get_entity', peer))
        return types.User(id=100, first_name='Synthetic', username='fixture')

    async def get_messages(self, peer, *, ids):
        self.calls.append(('get_messages', peer, ids))
        return self.messages.get((getattr(peer, 'user_id', None), ids), self.message)

    async def __call__(self, request):
        self.calls.append(request)
        if isinstance(request, (functions.upload.SaveFilePartRequest, functions.upload.SaveBigFilePartRequest)):
            return True
        if isinstance(request, functions.messages.SendMediaRequest) and type(request) not in self.responses:
            return types.UpdateShortSentMessage(23, 1, 1, datetime.now(timezone.utc), out=True)
        response = self.responses.get(type(request), types.Updates([], [], [],
            datetime(2026, 10, 14, tzinfo=timezone.utc), 0))
        if isinstance(response, Exception):
            raise response
        return response

    async def send_file(self, peer, media, **kwargs):
        self.calls.append(('send_file', peer, media, kwargs))
        return self.message

    async def edit_message(self, peer, message, text, **kwargs):
        self.calls.append(('edit_message', peer, message, text, kwargs))
        self.message.message = text
        return self.message

    async def forward_messages(self, peer, messages, from_peer=None, **kwargs):
        self.calls.append(('forward_messages', peer, messages, from_peer, kwargs))
        return [self.message]

    async def delete_messages(self, peer, ids, **kwargs):
        self.calls.append(('delete_messages', peer, ids, kwargs))
        return []

    async def pin_message(self, peer, message, **kwargs):
        self.calls.append(('pin_message', peer, message, kwargs))
        return None

    async def unpin_message(self, peer, message, **kwargs):
        self.calls.append(('unpin_message', peer, message, kwargs))
        return None

    async def edit_folder(self, peer, folder):
        self.calls.append(('edit_folder', peer, folder))
        return types.Updates([], [], [], datetime(2026, 10, 14, tzinfo=timezone.utc), 0)


def job(operation, parameters=None, **kwargs):
    return {'operation': operation, 'parameters': parameters or {}, 'provider_chat': '100',
            'provider_message_id': '23', 'expires_at': time.time() + 60,
            '_policy': {'enabled': True, 'discover_chats': True, 'include_chats': [], 'exclude_chats': []},
            **kwargs}


def run(client, operation, parameters=None, **kwargs):
    return asyncio.run(execute(client, job(operation, parameters, **kwargs)))


def test_unknown_operation_and_expiry_reject_without_any_provider_call():
    client = Client()
    for candidate in (job('raw_api'), job('delete_message', expires_at=1)):
        with pytest.raises(Exception, match='unsupported_operation|operation_expired'):
            asyncio.run(execute(client, candidate))
    assert client.calls == []


@pytest.mark.parametrize('parameters', [
    {'emoji': {'document_id': 1}}, {'emoji': 'x' * 65}, {'emoji': '👍', 'paid': True},
])
def test_paid_custom_or_unknown_reaction_parameters_rejected_without_network(parameters):
    client = Client()
    with pytest.raises(Exception, match='invalid_parameters'):
        run(client, 'send_reaction', parameters)
    assert client.calls == []


@pytest.mark.parametrize('emoji, expected_count', [('👍', 1), ('', 0)])
def test_reaction_is_typed_and_never_claims_unverified_effect(emoji, expected_count):
    client = Client()
    result = run(client, 'send_reaction', {'emoji': emoji})
    request = next(c for c in client.calls if isinstance(c, functions.messages.SendReactionRequest))
    assert request.msg_id == 23 and len(request.reaction) == expected_count
    assert all(isinstance(r, types.ReactionEmoji) for r in request.reaction)
    assert request.big is False and request.add_to_recent is False
    assert result['outcome'] == 'submitted'


def test_expiry_is_rechecked_after_governor_wait_before_mutation():
    client = Client()
    candidate = job('delete_message')

    class Governor:
        async def call(self, fn, phase):
            assert phase == 'provider_command'
            candidate['expires_at'] = 1
            return await fn()

    with pytest.raises(Exception, match='operation_expired'):
        asyncio.run(execute(client, candidate, governor=Governor()))
    assert not any(isinstance(c, tuple) and c[0] == 'delete_messages' for c in client.calls)


def test_pin_void_result_is_submitted_not_confirmed():
    client = Client()
    result = run(client, 'pin_message')
    assert result['outcome'] == 'submitted'
    assert [c for c in client.calls if isinstance(c, tuple) and c[0] == 'pin_message'][0][3] == {'notify': False}


def test_post_mutation_failure_is_unknown_and_not_retryable():
    client = Client()
    client.responses[functions.messages.SendReactionRequest] = OSError('private provider error')
    result = run(client, 'send_reaction', {'emoji': '👍'})
    assert result['outcome'] == 'unknown'
    assert 'private' not in str(result)
    assert len([c for c in client.calls if isinstance(c, functions.messages.SendReactionRequest)]) == 1


def test_poll_uses_pinned_native_shapes_and_plain_question_options():
    client = Client()
    result = run(client, 'send_poll', {'question': 'Which?', 'options': ['First', 'Second'], 'multiple': True})
    request = next(c for c in client.calls if isinstance(c, functions.messages.SendMediaRequest))
    assert isinstance(request.media, types.InputMediaPoll)
    assert request.media.poll.multiple_choice is True
    assert request.media.poll.question.text == 'Which?' and request.media.poll.question.entities == []
    assert [a.text.text for a in request.media.poll.answers] == ['First', 'Second']
    assert len({a.option for a in request.media.poll.answers}) == 2
    assert request.entities == [] and request.allow_paid_stars is None
    assert result['outcome'] == 'confirmed'


def test_vote_resolves_actual_poll_option_bytes_and_bounds_indices():
    client = Client()
    poll = types.Poll(1, types.TextWithEntities('Question', []),
        [types.PollAnswer(types.TextWithEntities('A', []), b'custom-a'),
         types.PollAnswer(types.TextWithEntities('B', []), b'custom-b')], hash=1,
        multiple_choice=True)
    client.message.media = types.MessageMediaPoll(poll, types.PollResults())
    run(client, 'vote_poll', {'option_indices': [1]})
    request = next(c for c in client.calls if isinstance(c, functions.messages.SendVoteRequest))
    assert request.options == [b'custom-b']
    client.calls.clear()
    with pytest.raises(Exception, match='invalid_parameters'):
        run(client, 'vote_poll', {'option_indices': [2]})
    assert not any(isinstance(c, functions.messages.SendVoteRequest) for c in client.calls)


def test_message_lookup_checks_exact_chat_and_id_without_disclosing_body_on_mismatch():
    client = Client()
    client.message.chat_id = 999
    with pytest.raises(Exception, match='provider_reference_mismatch'):
        run(client, 'get_message_info')


def test_mark_read_uses_exact_boundary_not_an_unbounded_read_all():
    client = Client()
    run(client, 'mark_read')
    request = next(c for c in client.calls if isinstance(c, functions.messages.ReadHistoryRequest))
    assert request.max_id == 23


def test_mark_unread_constructs_input_dialog_and_explicit_flag():
    client = Client()
    run(client, 'mark_unread')
    request = next(c for c in client.calls if isinstance(c, functions.messages.MarkDialogUnreadRequest))
    assert isinstance(request.peer, types.InputDialogPeer) and request.unread is True


def test_mute_changes_only_mute_until_preserving_other_notification_fields():
    client = Client()
    result = run(client, 'mute_chat', {'seconds': 3600})
    request = next(c for c in client.calls if isinstance(c, functions.account.UpdateNotifySettingsRequest))
    assert isinstance(request.settings, types.InputPeerNotifySettings)
    assert request.settings.mute_until > datetime.now(timezone.utc)
    assert request.settings.show_previews is None and request.settings.stories_muted is None
    assert result['outcome'] == 'submitted'


def test_list_topics_uses_messages_namespace_of_actual_telethon_version():
    client = Client()
    client.responses[functions.messages.GetForumTopicsRequest] = NS(topics=[], count=0)
    result = run(client, 'list_topics', {'limit': 20}, provider_chat='-1000000000100')
    request = next(c for c in client.calls if isinstance(c, functions.messages.GetForumTopicsRequest))
    assert request.limit == 20 and request.offset_id == 0 and request.offset_topic == 0
    assert result['outcome'] == 'confirmed' and result['data']['topics'] == []


def test_registered_original_is_streamed_without_path_in_result(tmp_path):
    original = tmp_path / 'original.bin'
    original.write_bytes(b'synthetic original')
    import hashlib
    client = Client()
    result = run(client, 'send_file', {'mode': 'document', 'caption': 'Text'},
                 registered_file_path=str(original), file_digest=hashlib.sha256(original.read_bytes()).hexdigest())
    call = next(c for c in client.calls if isinstance(c, functions.messages.SendMediaRequest))
    assert call.entities == [] and call.media.force_file is True
    assert call.message == 'Text'
    assert result['outcome'] == 'confirmed' and result['provider_message_id'] == '23'
    assert str(original) not in str(result)
    assert isinstance(call.media.file, types.InputFile)


def test_registered_original_symlink_or_oversize_rejected_before_provider(tmp_path):
    original = tmp_path / 'original.bin'
    original.write_bytes(b'file')
    link = tmp_path / 'link.bin'
    link.symlink_to(original)
    client = Client()
    with pytest.raises(Exception, match='attachment_unavailable'):
        run(client, 'send_file', {'mode': 'document'}, registered_file_path=str(link))
    assert client.calls == []
    with original.open('wb') as output:
        output.truncate(32 * 1024 * 1024 + 1)
    with pytest.raises(Exception, match='attachment_too_large'):
        run(client, 'send_file', {'mode': 'document'}, registered_file_path=str(original))
    assert client.calls == []


def test_forward_uses_prepared_source_and_preserves_author():
    client = Client()
    client.messages[(200, 7)] = NS(id=7, chat_id=200)
    result = run(client, 'forward_message', {}, source_provider_chat='200', source_provider_message='7')
    call = next(c for c in client.calls if isinstance(c, tuple) and c[0] == 'forward_messages')
    assert call[2] == [7] and call[3].user_id == 200
    assert call[4] == {'drop_author': False, 'drop_media_captions': False}
    assert result['outcome'] == 'confirmed'


def test_edit_plain_text_checks_result_identity_and_no_preview_fetch():
    client = Client()
    result = run(client, 'edit_message', {'text': 'new text'})
    call = next(c for c in client.calls if isinstance(c, tuple) and c[0] == 'edit_message')
    assert call[3] == 'new text' and call[4] == {'parse_mode': None, 'link_preview': False}
    assert result['outcome'] == 'confirmed'


def test_promote_does_not_grant_owner_or_add_admins_and_basic_chat_is_unsupported():
    client = Client()
    run(client, 'promote_participant', {'user_id': '200'}, provider_chat='-1000000000100')
    request = next(c for c in client.calls if isinstance(c, functions.channels.EditAdminRequest))
    assert request.admin_rights.change_info is True
    assert request.admin_rights.invite_users is True
    assert request.admin_rights.pin_messages is True
    assert request.admin_rights.add_admins is False and request.admin_rights.anonymous is False
    client.calls.clear()
    with pytest.raises(Exception, match='unsupported_operation'):
        run(client, 'promote_participant', {'user_id': '200'}, provider_chat='-100')
    assert not any(isinstance(c, functions.messages.EditChatAdminRequest) for c in client.calls)


def test_get_invite_link_reads_existing_link_without_creating_or_exporting():
    client = Client()
    client.responses[functions.messages.GetFullChatRequest] = NS(
        full_chat=NS(exported_invite=types.ChatInviteExported('https://t.me/+fixture', 42, datetime.now(timezone.utc))))
    result = run(client, 'get_invite_link', provider_chat='-100')
    assert result['outcome'] == 'confirmed' and result['data']['link'] == 'https://t.me/+fixture'
    assert not any(isinstance(c, functions.messages.ExportChatInviteRequest) for c in client.calls)


def test_full_provider_objects_are_not_serialized_in_profile():
    client = Client()
    client.responses[functions.users.GetFullUserRequest] = NS(full_user=NS(about='About', secret='session!'))
    result = asyncio.run(execute(client, job('get_profile'), verified_self=types.User(100, is_self=True)))
    assert result['outcome'] == 'confirmed'
    assert result['data']['profile']['id'] == '100' and result['data']['profile']['bio'] == 'About'
    assert 'session!' not in str(result)


@pytest.mark.parametrize('operation, parameters, request_type', [
    ('send_contact', {'phone': '+1234567890', 'first_name': 'Person'}, functions.messages.SendMediaRequest),
    ('send_location', {'latitude': 12.5, 'longitude': -45.1, 'description': 'Place'}, functions.messages.SendMediaRequest),
    ('mark_unread', {}, functions.messages.MarkDialogUnreadRequest),
    ('unmute_chat', {}, functions.account.UpdateNotifySettingsRequest),
    ('rename_group', {'title': 'Group'}, functions.messages.EditChatTitleRequest),
    ('set_group_description', {'description': 'About'}, functions.messages.EditChatAboutRequest),
    ('add_participant', {'user_id': '200'}, functions.messages.AddChatUserRequest),
    ('remove_participant', {'user_id': '200'}, functions.messages.DeleteChatUserRequest),
    ('demote_participant', {'user_id': '200'}, functions.messages.EditChatAdminRequest),
    ('leave_group', {}, functions.messages.DeleteChatUserRequest),
    ('create_group', {'user_ids': ['200'], 'title': 'New group'}, functions.messages.CreateChatRequest),
    ('add_contact', {'phone': '+1234567890', 'first_name': 'Contact'}, functions.contacts.ImportContactsRequest),
    ('delete_contact', {'user_id': '200'}, functions.contacts.DeleteContactsRequest),
    ('block_contact', {'user_id': '200'}, functions.contacts.BlockRequest),
    ('unblock_contact', {'user_id': '200'}, functions.contacts.UnblockRequest),
    ('update_profile', {'first_name': 'New', 'bio': 'About'}, functions.account.UpdateProfileRequest),
])
def test_admin_contact_location_operation_shapes_are_native_and_result_bounded(operation, parameters, request_type):
    from messaging_service.commands import safe_result
    client = Client()
    result = run(client, operation, parameters, provider_chat='-100')
    request = next(c for c in client.calls if isinstance(c, request_type))
    assert safe_result(result) == result
    if operation == 'send_location':
        assert request.media.geo_point.lat == 12.5 and request.media.geo_point.long == -45.1
        assert request.message == 'Place'
    elif operation == 'send_contact':
        assert request.media.phone_number == '+1234567890' and request.media.vcard == ''
    elif operation == 'unmute_chat':
        assert request.settings.mute_until.timestamp() == 0
    elif operation == 'add_participant':
        assert request.fwd_limit == 0
    elif operation == 'leave_group':
        assert isinstance(request.user_id, types.InputUserSelf) and request.revoke_history is False
    elif operation == 'demote_participant':
        assert request.is_admin is False
    elif operation == 'update_profile':
        assert request.about == 'About' and request.last_name is None


def test_all_requests_including_uploads_run_through_governor(tmp_path):
    import hashlib
    original = tmp_path / 'file'
    original.write_bytes(b'synthetic' * 100000)
    client = Client()
    candidate = job('send_file', {'mode': 'document'}, registered_file_path=str(original),
                    file_digest=hashlib.sha256(original.read_bytes()).hexdigest())

    class Governor:
        def __init__(self): self.count = 0
        async def call(self, fn, phase):
            assert phase == 'provider_command'
            self.count += 1
            return await fn()

    gate = Governor()
    result = asyncio.run(execute(client, candidate, governor=gate))
    assert result['outcome'] == 'confirmed'
    assert gate.count == len(client.calls) == 4  # entity, 2 uploads, final send


def test_expiry_after_upload_blocks_final_chat_send(tmp_path):
    import hashlib
    original = tmp_path / 'file'
    original.write_bytes(b'synthetic')
    client = Client()
    candidate = job('send_file', {'mode': 'document'}, registered_file_path=str(original),
                    file_digest=hashlib.sha256(original.read_bytes()).hexdigest())

    class Governor:
        async def call(self, fn, phase):
            answer = await fn()
            if any(isinstance(c, functions.upload.SaveFilePartRequest) for c in client.calls):
                candidate['expires_at'] = 1
            return answer

    result = asyncio.run(execute(client, candidate, governor=Governor()))
    assert result['outcome'] == 'unknown'
    assert not any(isinstance(c, functions.messages.SendMediaRequest) for c in client.calls)


def test_unrelated_provider_update_does_not_confirm_new_send():
    client = Client()
    foreign = types.Message(55, types.PeerUser(999), datetime.now(timezone.utc), 'unrelated', out=True)
    client.responses[functions.messages.SendMediaRequest] = types.Updates(
        [types.UpdateNewMessage(foreign, 1, 1)], [], [], datetime.now(timezone.utc), 0)
    result = run(client, 'send_contact', {'phone': '+1234567890', 'first_name': 'Person'})
    assert result['outcome'] == 'submitted' and 'provider_message_id' not in result


def test_registered_original_hash_mismatch_never_uploads(tmp_path):
    original = tmp_path / 'file'
    original.write_bytes(b'synthetic')
    client = Client()
    with pytest.raises(Exception, match='attachment_unavailable'):
        run(client, 'send_file', {'mode': 'document'}, registered_file_path=str(original), file_digest='0' * 64)
    assert not any(isinstance(c, (functions.upload.SaveFilePartRequest,
                                 functions.upload.SaveBigFilePartRequest)) for c in client.calls)


def test_revoke_invite_uses_existing_link_and_no_implicit_new_invite():
    client = Client()
    client.responses[functions.messages.GetFullChatRequest] = NS(full_chat=NS(
        exported_invite=types.ChatInviteExported('https://t.me/+fixture', 42, datetime.now(timezone.utc))))
    result = run(client, 'revoke_invite_link', provider_chat='-100')
    request = next(c for c in client.calls if isinstance(c, functions.messages.EditExportedChatInviteRequest))
    assert request.link == 'https://t.me/+fixture' and request.revoked is True
    assert result['outcome'] == 'submitted'
    assert not any(isinstance(c, functions.messages.ExportChatInviteRequest) for c in client.calls)


def test_read_reactions_returns_unknown_when_provider_omits_observation():
    client = Client()
    result = run(client, 'get_reactions')
    assert result['data']['reactions']['state'] == 'unknown'


def test_read_reactions_retains_known_counts_and_actor_time_without_full_user_object():
    client = Client()
    observed = types.MessageReactions([types.ReactionCount(types.ReactionEmoji('👍'), 2)],
        recent_reactions=[types.MessagePeerReaction(types.PeerUser(200), datetime.now(timezone.utc),
                                                    types.ReactionEmoji('👍'))])
    client.responses[functions.messages.GetMessagesReactionsRequest] = types.Updates(
        [types.UpdateMessageReactions(types.PeerUser(100), 23, observed)], [], [], datetime.now(timezone.utc), 0)
    result = run(client, 'get_reactions')
    assert result['data']['reactions']['counts'][0]['count'] == 2
    assert result['data']['reactions']['recent'][0]['peer_id'] == '200'
    assert result['data']['reactions']['actors_complete'] is False


def test_topic_nonzero_offset_rejected_before_any_provider_rpc():
    client = Client()
    with pytest.raises(Exception, match='unsupported_operation'):
        run(client, 'list_topics', {'offset': 10}, provider_chat='-1000000000100')
    assert client.calls == []


def test_edit_delete_require_live_own_message_before_mutating():
    client = Client()
    client.message.out = False
    for operation, parameters in [('edit_message', {'text': 'changed'}), ('delete_message', {})]:
        with pytest.raises(Exception, match='message_not_owned'):
            run(client, operation, parameters)
    assert not any(isinstance(c, tuple) and c[0] in ('edit_message', 'delete_messages') for c in client.calls)


@pytest.mark.parametrize('operation, parameters', [
    ('send_location', {'latitude': float('nan'), 'longitude': 0}),
    ('create_group', {'title': 'Group', 'user_ids': ['200', '200']}),
    ('update_profile', {'first_name': 'x', 'password': 'secret'}),
    ('edit_message', {'text': 'x', 'parse_mode': 'html'}),
    ('send_poll', {'question': 'x', 'options': ['A', 'A']}),
    ('vote_poll', {'option_indices': [True]}),
])
def test_invalid_operations_are_rejected_without_provider_calls(operation, parameters):
    client = Client()
    with pytest.raises(Exception, match='invalid_parameters'):
        run(client, operation, parameters)
    assert client.calls == []


def test_account_lists_use_contact_membership_and_bounded_provider_pages():
    client = Client()
    client.responses[functions.contacts.GetContactsRequest] = NS(
        contacts=[NS(user_id=200)],
        users=[types.User(200, first_name='Contact'), types.User(999, first_name='Unrelated')])
    result = run(client, 'list_contacts', {'limit': 1})
    assert result['data']['contacts'][0]['id'] == '200'
    assert 'Unrelated' not in str(result)
    result = run(client, 'get_contact', {'user_id': '999'})
    assert result['data']['found'] is False
    client.responses[functions.contacts.GetBlockedRequest] = NS(
        blocked=[types.PeerBlocked(types.PeerUser(300), datetime.now(timezone.utc))], count=1)
    result = run(client, 'list_blocked', {'limit': 1})
    assert result['data']['blocked'][0]['peer_id'] == '300'


def test_private_participant_queries_do_not_claim_a_group_member_list():
    client = Client()
    with pytest.raises(Exception, match='unsupported_operation'):
        run(client, 'list_participants')


def test_get_message_info_poll_includes_question_and_provider_option_order():
    client = Client()
    client.message.media = types.MessageMediaPoll(types.Poll(1,
        types.TextWithEntities('Question', []), [types.PollAnswer(types.TextWithEntities('One', []), b'a'),
        types.PollAnswer(types.TextWithEntities('Two', []), b'b')], hash=1),
        types.PollResults(results=[types.PollAnswerVoters(b'b', voters=3, chosen=True)], total_voters=3))
    result = run(client, 'get_message_info')
    assert result['data']['poll']['question'] == 'Question'
    assert result['data']['poll']['options'][1]['voters'] == 3
    assert result['data']['poll']['options'][1]['chosen'] is True


def test_provider_contact_pages_remain_bounded_and_control_free():
    from messaging_service.commands import safe_result
    client = Client()
    users = [types.User(i, first_name='😀' * 256 + '\x00', last_name='😀' * 256,
                        username='😀' * 128, phone='😀' * 64) for i in range(200, 300)]
    client.responses[functions.contacts.GetContactsRequest] = NS(
        contacts=[NS(user_id=u.id) for u in users], users=users)
    result = run(client, 'list_contacts', {'limit': 100})
    assert safe_result(result) == result
    assert result['data']['next_offset'] == len(result['data']['contacts'])
    assert result['data']['next_offset'] < 100
    assert '\x00' not in str(result)


def test_message_reactions_and_poll_payload_stay_under_result_budget():
    from messaging_service.commands import safe_result
    client = Client()
    client.message.message = '😀' * 4000
    emoji = '😀' * 64
    client.message.reactions = types.MessageReactions(
        [types.ReactionCount(types.ReactionEmoji(emoji), count=1) for _ in range(100)],
        recent_reactions=[types.MessagePeerReaction(types.PeerUser(200), datetime.now(timezone.utc),
                         types.ReactionEmoji(emoji)) for _ in range(100)])
    client.message.media = types.MessageMediaPoll(types.Poll(1,
        types.TextWithEntities('😀' * 512, []),
        [types.PollAnswer(types.TextWithEntities('😀' * 256, []), bytes([i])) for i in range(12)], hash=1),
        types.PollResults())
    result = run(client, 'get_message_info')
    assert safe_result(result) == result
    assert result['data']['reactions']['truncated'] is True


def test_create_group_unwraps_native_invited_users_and_reports_partial_invites():
    client = Client()
    created = types.Chat(23, 'Group', types.ChatPhotoEmpty(), 1, datetime.now(timezone.utc), 1)
    updates = types.Updates([], [], [created], datetime.now(timezone.utc), 0)
    client.responses[functions.messages.CreateChatRequest] = types.messages.InvitedUsers(
        updates, [types.MissingInvitee(200, premium_would_allow_invite=True)])
    result = run(client, 'create_group', {'title': 'Group', 'user_ids': ['200']})
    assert result['outcome'] == 'confirmed'
    assert result['data']['provider_chat_id'] == '-23'
    assert result['data']['missing_invitee_ids'] == ['200']
    assert result['data']['all_members_added'] is False


def test_channel_participants_are_taken_from_exact_provider_membership_page():
    client = Client()
    client.responses[functions.channels.GetParticipantsRequest] = NS(count=1,
        participants=[NS(user_id=200)],
        users=[types.User(200, first_name='Member'), types.User(999, first_name='Unrelated')])
    result = run(client, 'list_participants', {'limit': 100}, provider_chat='-1000000000100')
    assert [u['id'] for u in result['data']['participants']] == ['200']
    assert 'Unrelated' not in str(result)


def test_edit_does_not_confirm_an_unchanged_or_incoming_provider_message():
    client = Client()

    async def stale_edit(*args, **kwargs):
        return client.message

    client.edit_message = stale_edit
    result = run(client, 'edit_message', {'text': 'Requested new text'})
    assert result['outcome'] == 'unknown'


def test_trusted_preflight_runs_after_governor_wait_before_each_mutation():
    from messaging_service.commands import CommandRejected
    client = Client()
    candidate = job('send_reaction', {'emoji': '👍'})
    allowed = True
    preflights = []

    async def preflight():
        preflights.append(True)
        if not allowed:
            raise CommandRejected('operation_access_changed')

    class Governor:
        async def call(self, fn, phase):
            nonlocal allowed
            if client.calls:
                allowed = False
            return await fn()

    candidate['_preflight'] = preflight
    with pytest.raises(CommandRejected, match='operation_access_changed'):
        asyncio.run(execute(client, candidate, governor=Governor()))
    assert preflights
    assert not any(isinstance(c, functions.messages.SendReactionRequest) for c in client.calls)


def test_account_contacts_and_blocked_lists_respect_collection_exclusions():
    client = Client()
    client.responses[functions.contacts.GetContactsRequest] = NS(
        contacts=[NS(user_id=200), NS(user_id=300)],
        users=[types.User(200, first_name='Excluded'), types.User(300, first_name='Allowed')])
    client.responses[functions.contacts.GetBlockedRequest] = NS(count=2,
        blocked=[types.PeerBlocked(types.PeerUser(i), datetime.now(timezone.utc)) for i in (200, 300)])
    policy = {'enabled': True, 'discover_chats': True, 'include_chats': [], 'exclude_chats': ['200']}
    assert [u['id'] for u in run(client, 'list_contacts', _policy=policy)['data']['contacts']] == ['300']
    assert [u['peer_id'] for u in run(client, 'list_blocked', _policy=policy)['data']['blocked']] == ['300']
    assert run(client, 'get_contact', {'user_id': '200'}, _policy=policy)['data']['found'] is False


@pytest.mark.parametrize('operation, parameters', [
    ('create_group', {'title': 'Group', 'user_ids': ['200']}),
    ('block_contact', {'user_id': '200'}),
    ('add_participant', {'user_id': '200'}),
    ('add_contact', {'phone': '+123456789', 'first_name': 'New'}),
])
def test_excluded_contact_or_unresolvable_phone_never_mutates(operation, parameters):
    client = Client()
    policy = {'enabled': True, 'discover_chats': True, 'include_chats': [], 'exclude_chats': ['200']}
    with pytest.raises(Exception, match='operation_access_changed|unsupported_operation'):
        run(client, operation, parameters, _policy=policy, provider_chat='-100')
    assert client.calls == []


def test_authorization_revoked_after_first_upload_part_prevents_final_message(tmp_path):
    import hashlib
    from messaging_service.commands import CommandRejected
    original = tmp_path / 'fixture.bin'
    original.write_bytes(b'a' * (512 * 1024 + 1))
    client = Client()
    candidate = job('send_file', {'mode': 'document'}, registered_file_path=str(original),
                    file_digest=hashlib.sha256(original.read_bytes()).hexdigest())

    async def preflight():
        if any(isinstance(c, functions.upload.SaveFilePartRequest) for c in client.calls):
            raise CommandRejected('operation_access_changed')

    candidate['_preflight'] = preflight
    result = asyncio.run(execute(client, candidate))
    assert result['outcome'] == 'unknown'
    assert len([c for c in client.calls if isinstance(c, functions.upload.SaveFilePartRequest)]) == 1
    assert not any(isinstance(c, functions.messages.SendMediaRequest) for c in client.calls)


def test_policy_changed_during_governor_wait_prevents_mutation():
    client = Client()
    candidate = job('block_contact', {'user_id': '200'})

    async def preflight():
        if client.calls:
            candidate['_policy']['exclude_chats'] = ['200']

    candidate['_preflight'] = preflight
    with pytest.raises(Exception, match='operation_access_changed'):
        asyncio.run(execute(client, candidate))
    assert not any(isinstance(c, functions.contacts.BlockRequest) for c in client.calls)


def test_missing_trusted_collection_policy_fails_closed_before_provider_call():
    client = Client()
    candidate = job('list_contacts')
    del candidate['_policy']
    with pytest.raises(Exception, match='operation_access_changed'):
        asyncio.run(execute(client, candidate))
    assert client.calls == []


SUPERGROUP = '-1000000000100'


class GroupClient(Client):
    """Synthetic group whose restrictions change only through the requests that own them."""
    def __init__(self, *, admin=True, megagroup=True, apply=True):
        super().__init__()
        self.rights = types.ChatBannedRights(until_date=datetime(2038, 1, 1, tzinfo=timezone.utc), send_messages=False,
            change_info=False, send_media=True, send_polls=True, invite_users=True, pin_messages=False)
        self.slow, self.admin, self.megagroup, self.apply = 0, admin, megagroup, apply
        self.hide = {}

    def entity(self, basic=False):
        admin = types.ChatAdminRights(ban_users=True) if self.admin else None
        if basic:
            return types.Chat(id=100, title='Synthetic', photo=types.ChatPhotoEmpty(), participants_count=3, date=None,
                              version=1, admin_rights=admin, default_banned_rights=self.rights)
        return types.Channel(id=100, title='Synthetic', photo=types.ChatPhotoEmpty(), date=None, megagroup=self.megagroup,
                             access_hash=1, admin_rights=admin, default_banned_rights=self.rights)

    async def __call__(self, request):
        self.calls.append(request)
        if isinstance(request, functions.channels.GetFullChannelRequest):
            return NS(full_chat=NS(slowmode_seconds=self.slow or None, about=''), chats=[self.entity()], users=[])
        if isinstance(request, functions.messages.GetFullChatRequest):
            return NS(full_chat=NS(about=''), chats=[self.entity(basic=True)], users=[])
        if isinstance(request, functions.messages.EditChatDefaultBannedRightsRequest) and self.apply:
            self.rights = request.banned_rights
        if isinstance(request, functions.channels.ToggleSlowModeRequest) and self.apply:
            self.slow = request.seconds
        if isinstance(request, functions.messages.HideChatJoinRequestRequest):
            outcome = self.hide.get(request.user_id.user_id)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome == 'hang':
                await asyncio.sleep(30)
        self.calls.pop()  # The base client records it again.
        return await super().__call__(request)

    def mutations(self):
        return [c for c in self.calls if isinstance(c, (functions.messages.EditChatDefaultBannedRightsRequest,
            functions.channels.ToggleSlowModeRequest, functions.messages.HideChatJoinRequestRequest))]


# spec: ANYM-242
@pytest.mark.parametrize('operation,flag', [('set_group_messages_admins_only', 'send_messages'),
                                            ('set_group_info_admins_only', 'change_info')])
@pytest.mark.parametrize('chat', [SUPERGROUP, '-100'])
def test_group_settings_telegram_owned_flag_confirmed_from_read_back(operation, flag, chat):
    client = GroupClient()
    result = run(client, operation, {'enabled': True}, provider_chat=chat)
    assert result == {'outcome': 'confirmed', 'data': {'enabled': True, 'previous': False, 'changed': True}}
    (request,) = client.mutations()
    expected = types.InputPeerChannel(100, 1) if chat == SUPERGROUP else types.InputPeerChat(100)
    assert request.peer == expected and getattr(request.banned_rights, flag) is True
    # Turning it off again sends the opposite polarity.
    assert run(client, operation, {'enabled': False}, provider_chat=chat)['data'] == {
        'enabled': False, 'previous': True, 'changed': True}
    assert getattr(client.mutations()[-1].banned_rights, flag) is False


# spec: ANYM-242
@pytest.mark.parametrize('operation,parameters', [('set_group_messages_admins_only', {'enabled': False}),
    ('set_group_info_admins_only', {'enabled': False}), ('set_slow_mode', {'seconds': 0})])
def test_group_settings_telegram_already_in_state_makes_no_mutation(operation, parameters):
    client = GroupClient()
    result = run(client, operation, parameters, provider_chat=SUPERGROUP)
    assert result['outcome'] == 'confirmed' and result['data']['changed'] is False
    assert result['data']['previous'] == next(iter(parameters.values()))
    assert client.mutations() == []


# spec: ANYM-242
@pytest.mark.parametrize('operation,parameters', [('set_group_messages_admins_only', {'enabled': True}),
    ('set_group_info_admins_only', {'enabled': True}), ('set_slow_mode', {'seconds': 30})])
def test_group_settings_telegram_not_admin_refuses_before_mutation(operation, parameters):
    client = GroupClient(admin=False)
    with pytest.raises(Exception, match='^forbidden$'):
        run(client, operation, parameters, provider_chat=SUPERGROUP)
    assert client.mutations() == []


# spec: ANYM-242
def test_group_settings_telegram_slow_mode_supergroups_only():
    client = GroupClient()
    assert run(client, 'set_slow_mode', {'seconds': 60}, provider_chat=SUPERGROUP)['data'] == {
        'seconds': 60, 'previous': 0, 'changed': True}
    assert run(client, 'set_slow_mode', {'seconds': 0}, provider_chat=SUPERGROUP)['data'] == {
        'seconds': 0, 'previous': 60, 'changed': True}
    assert [c.seconds for c in client.mutations()] == [60, 0]
    assert all(c.channel == types.InputChannel(100, 1) for c in client.mutations())
    for chat, candidate in (('-100', GroupClient()), (SUPERGROUP, GroupClient(megagroup=False))):
        with pytest.raises(Exception, match='^unsupported_operation$'):
            run(candidate, 'set_slow_mode', {'seconds': 10}, provider_chat=chat)
        assert candidate.mutations() == []
    for seconds in (45, 1, True, None):
        with pytest.raises(Exception, match='^invalid_parameters$'):
            run(GroupClient(), 'set_slow_mode', {'seconds': seconds}, provider_chat=SUPERGROUP)
    with pytest.raises(Exception, match='^unsupported_operation$'):
        run(GroupClient(), 'set_join_approval', {'enabled': True}, provider_chat=SUPERGROUP)


# spec: ANYM-242
def test_group_settings_telegram_unmatched_read_back_is_submitted():
    client = GroupClient(apply=False)
    result = run(client, 'set_group_messages_admins_only', {'enabled': True}, provider_chat=SUPERGROUP)
    assert result['outcome'] == 'submitted' and result['data']['changed'] is True
    assert result['data']['effect_verified'] is False and len(client.mutations()) == 1


# spec: ANYM-242
def test_group_settings_telegram_chat_info_reports_group_settings():
    client = GroupClient()
    client.slow = 300
    assert run(client, 'chat_info', provider_chat=SUPERGROUP)['data']['group_settings'] == {
        'messages_admins_only': False, 'info_admins_only': False, 'slow_mode_seconds': 300, 'join_approval': None}
    assert run(client, 'chat_info', provider_chat='-100')['data']['group_settings']['slow_mode_seconds'] is None
    client.responses[functions.users.GetFullUserRequest] = NS(full_user=NS(about=None), users=[])
    assert run(client, 'chat_info', provider_chat='100')['data']['group_settings'] is None


# spec: ANYM-242
def test_default_rights_keep_unowned_flags():
    client = GroupClient()
    before = client.rights
    run(client, 'set_group_info_admins_only', {'enabled': True}, provider_chat=SUPERGROUP)
    (request,) = client.mutations()
    after = request.banned_rights
    assert after is not before and after.change_info is True
    for name in ('until_date', 'view_messages', 'send_messages', 'send_media', 'send_polls', 'invite_users',
                 'pin_messages', 'send_plain', 'manage_topics'):
        assert getattr(after, name) == getattr(before, name), name


# spec: ANYM-242
def test_join_requests_projection():
    client = GroupClient()
    date = datetime(2026, 10, 20, tzinfo=timezone.utc)
    client.responses[functions.messages.GetChatInviteImportersRequest] = types.messages.ChatInviteImporters(count=3,
        importers=[types.ChatInviteImporter(user_id=200, date=date, requested=True, about='😀' * 150),
                   types.ChatInviteImporter(user_id=201, date=date, requested=True)],
        users=[types.User(id=200, first_name='Synthetic', last_name='Requester', phone='0000', username='fixture')])
    result = run(client, 'list_join_requests', {'limit': 2}, provider_chat=SUPERGROUP)
    request = client.calls[-1]
    assert request.requested is True and request.limit == 2 and request.link is None
    data = result['data']
    assert result['outcome'] == 'confirmed' and set(data) == {'requests', 'has_more', 'untrusted'}
    assert data['has_more'] is True and data['untrusted'] == ['name', 'note']
    assert data['requests'] == [
        {'id': '200', 'requested_at': int(date.timestamp()), 'name': 'Synthetic Requester', 'note': '😀' * 100},
        {'id': '201', 'requested_at': int(date.timestamp()), 'name': None, 'note': None}]
    assert client.mutations() == []


def batch(client, operation, ids, **kwargs):
    return run(client, operation, {'user_ids': [str(i) for i in ids]}, provider_chat=SUPERGROUP, **kwargs)


# spec: ANYM-242
@pytest.mark.parametrize('operation', ['approve_join_requests', 'reject_join_requests'])
def test_join_request_batch_partial_refusal_is_settled(operation):
    from telethon.errors import HideRequesterMissingError
    client = GroupClient()
    client.hide[202] = HideRequesterMissingError(None)
    result = batch(client, operation, [201, 202, 203])
    assert result['outcome'] == 'confirmed'
    assert result['data'] == {'all_applied': False, 'results': [
        {'id': '201', 'state': 'applied'},
        {'id': '202', 'state': 'refused', 'reason': 'no_pending_request', 'provider_code': 'HIDE_REQUESTER_MISSING'},
        {'id': '203', 'state': 'applied'}]}
    assert [(c.user_id.user_id, c.approved) for c in client.mutations()] == [
        (201, operation == 'approve_join_requests'), (202, operation == 'approve_join_requests'),
        (203, operation == 'approve_join_requests')]
    assert all(c.peer == types.InputPeerChannel(100, 1) for c in client.mutations())


# spec: ANYM-242
@pytest.mark.parametrize('operation', ['approve_join_requests', 'reject_join_requests'])
@pytest.mark.parametrize('failure', ['503', '500', 'local'])
def test_join_request_batch_uncertain_error_stops(operation, failure):
    from telethon.errors import RPCError, ServerError
    client = GroupClient()
    client.hide[202] = {'503': RPCError(None, 'TIMEOUT', 503), '500': ServerError(None, 'INTERNAL', 500),
                        'local': ConnectionError('synthetic')}[failure]
    result = batch(client, operation, [201, 202, 203])
    assert result['outcome'] == 'unknown'
    assert result['data'] == {'all_applied': False, 'results': [{'id': '201', 'state': 'applied'},
        {'id': '202', 'state': 'unknown'}, {'id': '203', 'state': 'not_attempted'}]}
    assert [c.user_id.user_id for c in client.mutations()] == [201, 202]


# spec: ANYM-242
@pytest.mark.parametrize('operation', ['approve_join_requests', 'reject_join_requests'])
@pytest.mark.parametrize('change', ['access', 'expiry'])
def test_join_request_batch_recheck_failure_stops_without_provider_call(operation, change):
    from messaging_service.commands import CommandRejected
    client = GroupClient()
    candidate = job(operation, {'user_ids': ['201', '202', '203']}, provider_chat=SUPERGROUP)

    async def preflight():
        if client.mutations():
            if change == 'expiry':
                candidate['expires_at'] = time.time() - 1
            else:
                raise CommandRejected('operation_access_changed')
    candidate['_preflight'] = preflight
    result = asyncio.run(execute(client, candidate))
    assert result['outcome'] == 'unknown'
    assert result['data']['results'] == [{'id': '201', 'state': 'applied'}, {'id': '202', 'state': 'not_attempted'},
                                         {'id': '203', 'state': 'not_attempted'}]
    assert [c.user_id.user_id for c in client.mutations()] == [201]
    # Before any request, a failed recheck refuses the whole batch.
    first = GroupClient()
    with pytest.raises(CommandRejected, match='operation_expired'):
        asyncio.run(execute(first, job(operation, {'user_ids': ['201']}, provider_chat=SUPERGROUP, expires_at=1)))
    assert first.mutations() == []


# spec: ANYM-242
@pytest.mark.parametrize('operation', ['approve_join_requests', 'reject_join_requests'])
def test_join_request_batch_of_20_cached_requesters_fits_worker_deadline_with_real_governor(operation):
    from telethon.sessions import MemorySession
    from messaging_service.telegram_governor import RequestGovernor
    class State:
        def cooldown_remaining(self):
            return 0

        def status(self, *args):
            pass
    clock = [0.0]
    async def sleep(delay):
        clock[0] += delay
    client = GroupClient()
    ids = list(range(201, 221))
    client.session = MemorySession()
    # list_join_requests stores the requesters in the session file like this.
    client.session.process_entities(types.messages.ChatInviteImporters(count=20, importers=[],
        users=[types.User(id=i, access_hash=7, first_name='Synthetic') for i in ids]))
    result = asyncio.run(execute(client, job(operation, {'user_ids': [str(i) for i in ids]}, provider_chat=SUPERGROUP),
                                 governor=RequestGovernor(State(), clock=lambda: clock[0], sleep=sleep)))
    assert result['outcome'] == 'confirmed' and result['data']['all_applied'] is True
    assert [c.user_id for c in client.mutations()] == [types.InputUser(i, 7) for i in ids]
    assert not any(c[0] == 'get_input_entity' and c[1] in ids for c in client.calls if isinstance(c, tuple))
    # One paced group lookup and 20 paced mutations, inside the worker's 30 s.
    assert clock[0] == 20.0 and clock[0] < 30


class LookupFailure(GroupClient):
    async def get_input_entity(self, peer):
        if peer == 201:
            raise ValueError('synthetic unresolved requester')
        return await super().get_input_entity(peer)


# spec: ANYM-242
@pytest.mark.parametrize('operation', ['approve_join_requests', 'reject_join_requests'])
@pytest.mark.parametrize('failure', ['lookup', 'revoked'])
def test_join_request_batch_first_id_failure_still_returns_rows(operation, failure):
    from messaging_service.commands import CommandRejected
    client = LookupFailure() if failure == 'lookup' else GroupClient()
    candidate = job(operation, {'user_ids': ['201', '202']}, provider_chat=SUPERGROUP)
    async def preflight():
        if failure == 'revoked' and ('get_input_entity', 201) in client.calls:
            raise CommandRejected('operation_access_changed')
    candidate['_preflight'] = preflight
    result = asyncio.run(execute(client, candidate))
    assert result == {'outcome': 'unknown', 'data': {'all_applied': False, 'results': [
        {'id': '201', 'state': 'not_attempted'}, {'id': '202', 'state': 'not_attempted'}]}}
    assert client.mutations() == []


# spec: ANYM-242
@pytest.mark.parametrize('operation', ['approve_join_requests', 'reject_join_requests'])
def test_join_request_batch_of_20_uncached_requesters_stops_at_budget_with_real_governor(operation):
    from messaging_service.telegram_governor import RequestGovernor
    from messaging_service.telegram_operations import JOIN_BATCH_BUDGET_SECONDS
    class State:
        def cooldown_remaining(self):
            return 0

        def status(self, *args):
            pass
    clock = [0.0]
    async def sleep(delay):
        clock[0] += delay
    client = GroupClient()  # No session cache: every requester costs a paced lookup.
    ids = [str(i) for i in range(201, 221)]
    result = asyncio.run(execute(client, job(operation, {'user_ids': ids}, provider_chat=SUPERGROUP),
                                 governor=RequestGovernor(State(), clock=lambda: clock[0], sleep=sleep)))
    states = [r['state'] for r in result['data']['results']]
    applied = states.count('applied')
    assert result['outcome'] == 'unknown' and result['data']['all_applied'] is False
    assert [r['id'] for r in result['data']['results']] == ids
    assert 0 < applied < 20 and states == ['applied'] * applied + ['not_attempted'] * (20 - applied)
    # Nothing after the stop: no mutation of a not_attempted requester, no lookup past the first.
    assert [c.user_id.user_id for c in client.mutations()] == [int(i) for i in ids[:applied]]
    assert not any(c == ('get_input_entity', int(i)) for i in ids[applied + 1:] for c in client.calls)
    # The last ID started inside the budget; the adapter answered well inside the worker's 30 s.
    assert clock[0] < JOIN_BATCH_BUDGET_SECONDS + 2 < 30


# spec: ANYM-242
@pytest.mark.parametrize('operation', ['approve_join_requests', 'reject_join_requests'])
@pytest.mark.parametrize('cached', [True, False])
def test_join_request_batch_budget_counts_from_adapter_entry(operation, cached):
    """A slow paced group lookup spends the same budget; no mutation starts after it."""
    from telethon.sessions import MemorySession
    from messaging_service.telegram_governor import RequestGovernor
    from messaging_service.telegram_operations import JOIN_BATCH_BUDGET_SECONDS
    class State:
        def cooldown_remaining(self):
            return 0

        def status(self, *args):
            pass
    clock, starts = [0.0], []
    async def sleep(delay):
        clock[0] += delay
    ids = list(range(201, 221))
    class Client(GroupClient):
        async def get_input_entity(self, peer):
            if peer == int(SUPERGROUP):
                clock[0] += 12  # A 12 s group resolution before the batch.
            return await super().get_input_entity(peer)
        async def __call__(self, request):
            if isinstance(request, functions.messages.HideChatJoinRequestRequest):
                starts.append(clock[0])
            return await super().__call__(request)
    client = Client()
    if cached:
        client.session = MemorySession()
        client.session.process_entities(types.messages.ChatInviteImporters(count=20, importers=[],
            users=[types.User(id=i, access_hash=7, first_name='Synthetic') for i in ids]))
    result = asyncio.run(execute(client, job(operation, {'user_ids': [str(i) for i in ids]}, provider_chat=SUPERGROUP),
                                 governor=RequestGovernor(State(), clock=lambda: clock[0], sleep=sleep)))
    states = [r['state'] for r in result['data']['results']]
    applied = states.count('applied')
    assert result['outcome'] == 'unknown' and len(states) == 20
    assert 0 < applied < 20 and states == ['applied'] * applied + ['not_attempted'] * (20 - applied)
    assert len(starts) == applied and all(start < JOIN_BATCH_BUDGET_SECONDS for start in starts)
    assert clock[0] < 30


# spec: ANYM-242
@pytest.mark.parametrize('operation', ['approve_join_requests', 'reject_join_requests'])
def test_join_request_batch_worker_scaled_slow_lookup_keeps_rows(operation):
    """The real worker deadline with every duration scaled down: rows reach the receipt."""
    from unittest.mock import patch
    from telethon.sessions import MemorySession
    from messaging_service.commands import CommandWorker
    from messaging_service.telegram_governor import RequestGovernor
    scale, receipt, ids = 0.02, {}, list(range(201, 221))
    class State:
        def cooldown_remaining(self):
            return 0

        def status(self, *args):
            pass
    async def scaled_sleep(delay):
        await asyncio.sleep(delay * scale)
    class Client(GroupClient):
        async def get_input_entity(self, peer):
            if peer == int(SUPERGROUP):
                await asyncio.sleep(12 * scale)
            return await super().get_input_entity(peer)
    client = Client()
    client.session = MemorySession()
    client.session.process_entities(types.messages.ChatInviteImporters(count=20, importers=[],
        users=[types.User(id=i, access_hash=7, first_name='Synthetic') for i in ids]))
    candidate = job(operation, {'user_ids': [str(i) for i in ids]}, provider_chat=SUPERGROUP, mutation=True)
    class Worker(CommandWorker):
        def claim(self):
            return candidate

        def preflight(self, item, **kwargs):
            pass

        def finish(self, item, status, result=None, code=None):
            receipt.update(status=status, result=result)
    async def scenario():
        start = time.monotonic()
        governor = RequestGovernor(State(), clock=lambda: (time.monotonic() - start) / scale, sleep=scaled_sleep)
        async def provider(item):
            return await execute(client, item, governor=governor)
        original = asyncio.wait_for
        async def scaled_wait_for(awaitable, timeout):
            return await original(awaitable, timeout * scale)
        with patch('messaging_service.commands.asyncio.wait_for', scaled_wait_for):
            await Worker(None, provider)._once()
    asyncio.run(scenario())
    assert receipt['status'] == 'unknown' and receipt['result'] is not None
    rows = receipt['result']['data']['results']
    assert len(rows) == 20 and 0 < len(client.mutations()) < 20
    assert [r['state'] for r in rows].count('applied') == len(client.mutations())


def _channel(**kwargs):
    values = dict(id=500, title='Synthetic link chat', photo=types.ChatPhotoEmpty(), date=None, access_hash=7,
                  megagroup=True, participants_count=12)
    return types.Channel(**{**values, **kwargs})


class LinkClient(Client):
    """Synthetic invite and public-name links; joins answer from `join` (a response or an error)."""
    def __init__(self, invite=None, entity=None, join=None):
        super().__init__()
        self.invite = invite if invite is not None else types.ChatInvite(
            title='Synthetic link chat', photo=types.PhotoEmpty(0), participants_count=12, color=0, channel=True,
            megagroup=True, request_needed=True, about='Synthetic description', participants=[
                types.User(id=901, first_name='Synthetic member')])
        self.public = entity if entity is not None else _channel(left=True, username='synthetic_link')
        self.join = join if join is not None else types.Updates([], [], [_channel(left=False)],
            datetime(2026, 10, 20, tzinfo=timezone.utc), 0)

    async def get_entity(self, peer):
        self.calls.append(('get_entity', peer))
        if isinstance(self.public, BaseException):
            raise self.public
        return self.public

    async def __call__(self, request):
        self.calls.append(request)
        if isinstance(request, functions.messages.CheckChatInviteRequest):
            if isinstance(self.invite, BaseException):
                raise self.invite
            return self.invite
        if isinstance(request, (functions.messages.ImportChatInviteRequest, functions.channels.JoinChannelRequest)):
            if isinstance(self.join, BaseException):
                raise self.join
            return self.join
        self.calls.pop()
        return await super().__call__(request)

    def joins(self):
        return [c for c in self.calls if isinstance(c, (functions.messages.ImportChatInviteRequest,
                                                        functions.channels.JoinChannelRequest))]


CHECK_FIELDS = {'title', 'kind', 'member_count', 'approval_required', 'paid', 'membership', 'membership_basis'}
EXPECTED = {'title': 'Synthetic link chat', 'kind': 'group'}


def link_job(operation, link, **kwargs):
    return job(operation, {'link': link}, provider_chat=None, provider_message_id=None, **kwargs)


def link_run(client, operation, link, **kwargs):
    return asyncio.run(execute(client, link_job(operation, link, **kwargs)))


# spec: ANYM-243
def test_check_chat_link_invite_projection_makes_no_join_call():
    client = LinkClient()
    result = link_run(client, 'check_chat_link', '+AbCd1234')
    assert result['outcome'] == 'confirmed' and set(result['data']) == CHECK_FIELDS
    assert result['data'] == {'title': 'Synthetic link chat', 'kind': 'group', 'member_count': 12,
        'approval_required': True, 'paid': False, 'membership': 'not_member', 'membership_basis': 'provider_invite'}
    assert [c.hash for c in client.calls if isinstance(c, functions.messages.CheckChatInviteRequest)] == ['AbCd1234']
    assert client.joins() == [] and 'Synthetic description' not in str(result) and '901' not in str(result)


# spec: ANYM-243
@pytest.mark.parametrize('invite,membership,kind', [
    (types.ChatInviteAlready(_channel(broadcast=True, megagroup=False)), 'member', 'channel'),
    (types.ChatInvitePeek(_channel(), None), 'not_member', 'group'),
    (types.ChatInvite(title='Synthetic link chat', photo=types.PhotoEmpty(0), participants_count=3, color=0,
                      subscription_pricing=types.StarsSubscriptionPricing(period=2592000, amount=5)), 'not_member', 'group')])
def test_check_chat_link_membership_from_invite_kind(invite, membership, kind):
    client = LinkClient(invite=invite)
    data = link_run(client, 'check_chat_link', '+AbCd1234')['data']
    assert set(data) == CHECK_FIELDS and data['membership'] == membership and data['kind'] == kind
    assert data['paid'] is (isinstance(invite, types.ChatInvite) and invite.subscription_pricing is not None)
    assert client.joins() == [] and '500' not in str(data)


# spec: ANYM-243
@pytest.mark.parametrize('left,membership', [(True, 'not_member'), (False, 'member'), (None, 'unknown')])
def test_check_chat_link_public_name_uses_left_flag(left, membership):
    client = LinkClient(entity=_channel(left=left, broadcast=True, megagroup=False, join_request=None))
    data = link_run(client, 'check_chat_link', '@synthetic_link')['data']
    assert data == {'title': 'Synthetic link chat', 'kind': 'channel', 'member_count': 12, 'approval_required': None,
                    'paid': False, 'membership': membership, 'membership_basis': 'provider_public_chat'}
    assert ('get_entity', 'synthetic_link') in client.calls and client.joins() == []


# spec: ANYM-243
@pytest.mark.parametrize('link,client,code', [
    ('+AbCd1234', LinkClient(invite=errors.InviteHashExpiredError(None)), 'invite_expired'),
    ('+AbCd1234', LinkClient(invite=errors.InviteHashInvalidError(None)), 'invite_invalid'),
    ('@synthetic_link', LinkClient(entity=errors.UsernameNotOccupiedError(None)), 'invite_invalid'),
    ('@synthetic_link', LinkClient(entity=types.User(id=7, first_name='Synthetic')), 'invite_invalid'),
    ('AbCd1234', LinkClient(), 'invalid_parameters')])
def test_check_chat_link_refusals_are_definite(link, client, code):
    with pytest.raises(Exception, match=code):
        link_run(client, 'check_chat_link', link)
    assert client.joins() == []


# spec: ANYM-243
@pytest.mark.parametrize('case', ['invite_joined', 'public_joined', 'pending', 'expired', 'invalid', 'full',
                                  'too_many', 'private', 'already', 'uncertain', 'not_collected', 'unreadable'])
def test_join_outcomes_telegram(case):
    from telethon.errors import RPCError
    join = {'pending': errors.InviteRequestSentError(None), 'expired': errors.InviteHashExpiredError(None),
            'invalid': errors.InviteHashInvalidError(None), 'full': errors.UsersTooMuchError(None),
            'too_many': errors.ChannelsTooMuchError(None), 'private': errors.ChannelPrivateError(None),
            'already': errors.UserAlreadyParticipantError(None), 'uncertain': RPCError(None, 'TIMEOUT', 503),
            'unreadable': types.Updates([], [], [], datetime(2026, 10, 20, tzinfo=timezone.utc), 0)}.get(case)
    client = LinkClient(join=join)
    link = '@synthetic_link' if case == 'public_joined' else '+AbCd1234'
    expected = {'title': 'Synthetic link chat', 'kind': 'group'}
    policy = {'enabled': True, 'discover_chats': case != 'not_collected', 'include_chats': [], 'exclude_chats': []}
    refusals = {'expired': 'invite_expired', 'invalid': 'invite_invalid', 'full': 'chat_full',
                'too_many': 'join_not_allowed', 'private': 'join_not_allowed', 'already': 'already_member'}
    if case in refusals:
        with pytest.raises(Exception, match=refusals[case]):
            link_run(client, 'join_chat', link, expected_chat=expected, _policy=policy)
        assert len(client.joins()) == 1
        return
    result = link_run(client, 'join_chat', link, expected_chat=expected, _policy=policy)
    assert len(client.joins()) == 1
    if case == 'public_joined':
        assert isinstance(client.joins()[0], functions.channels.JoinChannelRequest)
        assert client.joins()[0].channel == types.InputChannel(500, 7)
    else:
        assert client.joins()[0] == functions.messages.ImportChatInviteRequest('AbCd1234')
    if case == 'pending':
        assert result == {'outcome': 'confirmed', 'data': {'state': 'pending_approval', 'basis': 'provider_join_request_sent'}}
    elif case == 'uncertain':
        assert result['outcome'] == 'unknown'
    elif case == 'not_collected':
        assert result == {'outcome': 'confirmed', 'data': {'state': 'joined', 'archive': 'not_collected',
            'next_step': "the operator adds this chat to the account's collection policy", 'basis': 'provider_joined_chat'}}
    elif case == 'unreadable':
        assert result['outcome'] == 'unknown'
    else:
        assert result == {'outcome': 'confirmed', 'data': {'state': 'joined', 'archive': 'collected',
            'provider_chat_id': '-1000000000500', 'basis': 'provider_joined_chat'}}


# spec: ANYM-243
@pytest.mark.parametrize('change,code', [('title', 'operation_target_changed'), ('kind', 'operation_target_changed'),
                                         ('member', 'already_member'), ('paid', 'join_not_allowed'),
                                         ('missing', 'operation_target_changed')])
def test_join_target_changed_refuses_without_join_call(change, code):
    invite = {'member': types.ChatInviteAlready(_channel()),
              'paid': types.ChatInvite(title='Synthetic link chat', photo=types.PhotoEmpty(0), participants_count=3,
                  color=0, megagroup=True, subscription_pricing=types.StarsSubscriptionPricing(period=2592000, amount=5))}.get(change)
    expected = {'title': 'Synthetic link chat', 'kind': 'group'}
    if change == 'title':
        expected['title'] = 'Synthetic renamed chat'
    if change == 'kind':
        expected['kind'] = 'channel'
    client = LinkClient(invite=invite)
    with pytest.raises(Exception, match=code):
        link_run(client, 'join_chat', '+AbCd1234', **({} if change == 'missing' else {'expected_chat': expected}))
    assert client.joins() == []
