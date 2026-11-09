"""Typed Telegram operation tests; never connect a provider session."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
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


# spec: ANYM-927
def test_a_silent_telegram_send_file_sends_the_media_without_sound(tmp_path):
    original = tmp_path / 'original.bin'
    original.write_bytes(b'synthetic original')
    import hashlib
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    sent = []
    for parameters in ({'mode': 'document', 'silent': True}, {'mode': 'document'}):
        client = Client()
        result = run(client, 'send_file', parameters, registered_file_path=str(original), file_digest=digest)
        assert result['outcome'] == 'confirmed'
        sent.append(next(c for c in client.calls if isinstance(c, functions.messages.SendMediaRequest)))
    assert sent[0].silent is True and sent[1].silent is None


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


def contact_book(*users):
    client = Client()
    client.responses[functions.contacts.GetContactsRequest] = NS(contacts=[NS(user_id=u.id) for u in users], users=list(users))
    return client


BOOK = (types.User(200, first_name='Федор', last_name='Петров', username='fedor_p', phone='100200300'),
        types.User(201, first_name='Фёдор', last_name='Иванов'),
        types.User(202, first_name='Anna', username='Anna_K', phone='79990001111'),
        types.User(203, first_name='Борис', phone='555'))


def found(client, query, **parameters):
    result = run(client, 'list_contacts', {'query': query, **parameters})['data']
    return [u['id'] for u in result['contacts']], result


# spec: ANYM-673
@pytest.mark.parametrize('query, expected', [
    ('федор', ['200', '201']), ('ФЁДОР', ['200', '201']), ('  фёдор  петр ', ['200']), ('фёдор иван', ['201']),
    ('fedor', ['200']), ('fyodor', ['201']), ('@anna_k', ['202']), ('ANNA', ['202']), ('борис', ['203']),
    ('boris', ['203']), ('nobody', []), ('федор%', [])])
def test_list_contacts_query_matches_name_and_username_with_yo_fold_and_latin_spelling(query, expected):
    ids, result = found(contact_book(*BOOK), query)
    assert ids == expected and result['reported_count'] == len(expected)


# spec: ANYM-673
@pytest.mark.parametrize('query', ['79990001111', '100200300', '555', '+7999'])
def test_list_contacts_query_never_matches_a_phone_number(query):
    assert found(contact_book(*BOOK), query)[0] == []


# spec: ANYM-673
def test_list_contacts_query_pages_the_filtered_list_with_one_provider_call():
    users = [types.User(300 + n, first_name='Иван %d' % n) for n in range(5)] + [types.User(400, first_name='Другой')]
    client = contact_book(*users)
    first = run(client, 'list_contacts', {'query': 'иван', 'limit': 2})['data']
    assert [u['id'] for u in first['contacts']] == ['300', '301']
    assert first['reported_count'] == 5 and first['next_offset'] == 2 and first['page_limit'] == 2
    last = run(client, 'list_contacts', {'query': 'иван', 'limit': 2, 'offset': 4})['data']
    assert [u['id'] for u in last['contacts']] == ['304'] and last['next_offset'] is None
    assert sum(isinstance(c, functions.contacts.GetContactsRequest) for c in client.calls) == 2


# spec: ANYM-673
def test_list_contacts_without_query_is_unchanged_and_the_filter_respects_the_allowed_ids():
    client = contact_book(*BOOK)
    plain = run(client, 'list_contacts', {})['data']
    assert [u['id'] for u in plain['contacts']] == ['200', '201', '202', '203'] and plain['reported_count'] == 4
    only = run(client, 'list_contacts', {'query': 'федор'}, _policy={'enabled': True, 'discover_chats': False,
        'include_chats': ['200'], 'exclude_chats': []})['data']
    assert [u['id'] for u in only['contacts']] == ['200']


# spec: ANYM-673
@pytest.mark.parametrize('query', ['', '   ', '@', 'x' * 65, 'a\x00b', 7, None, ['a']])
def test_list_contacts_query_is_refused_before_any_provider_call(query):
    client = contact_book(*BOOK)
    with pytest.raises(Exception, match='invalid_parameters'):
        run(client, 'list_contacts', {'query': query})
    assert client.calls == []


# spec: ANYM-673
def test_list_contacts_query_length_counts_characters_after_trim():
    from messaging_service.messenger_operations import validate_operation
    for query in (' ' + 'x' * 64 + ' ', '😀' * 64, '\u00a0' + 'я' * 64):
        assert validate_operation('telegram', 'list_contacts', {'query': query})['query'] == query.strip()
    assert found(contact_book(*BOOK), '  anna  ')[0] == ['202']


# spec: ANYM-673
@pytest.mark.parametrize('query, reason', [('', 'blank'), ('   ', 'blank'), ('@', 'blank'), ('x' * 65, 'max_length'),
                                           ('a\x00b', 'pattern'), (7, 'type'), (None, 'type'), (['a'], 'type'),
                                           ('a\tb', 'pattern'), ('a\nb', 'pattern'), ('a\x9bb', 'pattern'), (' ' + 'x' * 65 + ' ', 'max_length')])
def test_list_contacts_query_refusal_names_the_field(query, reason):
    from messaging_service.messenger_operations import ParameterError, validate_operation
    with pytest.raises(ParameterError) as error:
        validate_operation('telegram', 'list_contacts', {'query': query})
    entry, = error.value.field_errors
    assert entry['field'] == 'query' and entry['reason'] == reason


# spec: ANYM-673
def test_the_query_parameter_is_telegram_only_and_published_with_its_limits():
    from messaging_service.messenger_operations import ParameterError, operation_manifest, validate_operation
    entry = next(e for e in operation_manifest('telegram') if e['operation'] == 'list_contacts')
    assert entry['parameters_schema']['query']['maximum'] == 64 and entry['parameters_schema']['query']['nonempty']
    assert 'display name and username only, no phone number' in entry['limitation'] and 'once without query' in entry['limitation']
    whatsapp = next(e for e in operation_manifest('whatsapp') if e['operation'] == 'list_contacts')
    assert 'query' not in whatsapp['parameters_schema']
    with pytest.raises(ParameterError):
        validate_operation('whatsapp', 'list_contacts', {'query': 'anna'})
    assert validate_operation('telegram', 'list_contacts', {'query': ' Анна '})['query'] == 'Анна'


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


# --- ANYM-681: inspect_message_buttons -----------------------------------------------------------------------
import hashlib
import hmac
import json
import logging
import struct

from messaging_service import button_fingerprint
from messaging_service.commands import CommandRejected, safe_result
from test_live_search import governed

CALLBACK_SECRET = b'SYNTHETIC-CALLBACK-DATA-9147'
INSPECT_KEY = bytes(range(32))


def inline(*rows):
    return types.ReplyInlineMarkup(rows=[types.KeyboardInlineButtonRow(buttons=list(row)) for row in rows])


def button(text, kind):
    return types.KeyboardInlineButton(text=text, type=kind)


def callback_button(text, data=CALLBACK_SECRET, **kw):
    return button(text, types.InlineButtonTypeCallback(data=data, **kw))


def inspect(client, governor=None):
    return asyncio.run(execute(client, job('inspect_message_buttons'), governor=governor))


def keyed(monkeypatch, key=INSPECT_KEY):
    monkeypatch.setattr(button_fingerprint, '_KEY', key)


# spec: ANYM-681
def test_the_fingerprint_is_a_truncated_hmac_of_length_prefixed_fields(monkeypatch):
    fields = [b'-1001', b'23', b'0', b'1', b'payload']
    expected = hmac.new(INSPECT_KEY, b''.join(struct.pack('>I', len(f)) + f for f in fields),
                        hashlib.sha256).digest()[:16].hex()
    assert button_fingerprint.fingerprint(-1001, 23, 0, 1, b'payload', key=INSPECT_KEY) == expected
    assert len(expected) == 32 and expected == expected.lower()
    keyed(monkeypatch)
    assert button_fingerprint.fingerprint(-1001, 23, 0, 1, b'payload') == expected


# spec: ANYM-681
def test_every_fingerprint_field_changes_the_fingerprint_independently():
    base = (-1001, 23, 0, 1, b'payload')
    seen = {button_fingerprint.fingerprint(*base, key=INSPECT_KEY)}
    for index, other in enumerate((-1002, 24, 1, 2, b'payloaD')):
        changed = list(base)
        changed[index] = other
        seen.add(button_fingerprint.fingerprint(*changed, key=INSPECT_KEY))
    assert len(seen) == 6
    # Length prefixes make the encoding unambiguous: moving a byte between fields cannot collide.
    assert (button_fingerprint.fingerprint(1, 23, 0, 1, b'2payload', key=INSPECT_KEY)
            != button_fingerprint.fingerprint(12, 3, 0, 1, b'payload', key=INSPECT_KEY))


# spec: ANYM-681
def test_the_process_key_is_random_32_bytes_made_once(monkeypatch):
    monkeypatch.setattr(button_fingerprint, '_KEY', None)
    first = button_fingerprint.process_key()
    assert len(first) == 32 and button_fingerprint.process_key() is first
    monkeypatch.setattr(button_fingerprint, '_KEY', None)
    assert button_fingerprint.process_key() != first


# spec: ANYM-681
def test_inspection_lists_kinds_links_and_fingerprints_for_callback_buttons_only(monkeypatch):
    keyed(monkeypatch)
    client = Client()
    client.message.edit_date = datetime(2026, 10, 14, 1, 2, 3, tzinfo=timezone.utc)
    client.message.reply_markup = inline(
        [callback_button('Pay'), callback_button('Locked', b'x', requires_password=True)],
        [button('Site', types.InlineButtonTypeUrl(url='https://example.invalid/a')),
         button('App', types.InlineButtonTypeWebView(url='https://example.invalid/app')),
         button('Login', types.InlineButtonTypeUrlAuth(url='https://example.invalid/auth', button_id=3)),
         button('Share', types.InlineButtonTypeSwitchInline(query='SYNTHETIC-INLINE-QUERY')),
         button('Bad', types.InlineButtonTypeUrl(url='javascript:alert(1)'))])
    result = run(client, 'inspect_message_buttons')
    data = result['data']
    assert result['outcome'] == 'confirmed' and data['edit_date'] == '2026-10-14T01:02:03+00:00'
    assert data['text_sha256'] == hashlib.sha256(b'hello').hexdigest()
    pay = button_fingerprint.fingerprint(100, 23, 0, 0, CALLBACK_SECRET, key=INSPECT_KEY)
    assert data['rows'] == [
        [{'row': 0, 'column': 0, 'kind': 'callback', 'text': 'Pay', 'fp': pay},
         {'row': 0, 'column': 1, 'kind': 'callback_password', 'text': 'Locked'}],
        [{'row': 1, 'column': 0, 'kind': 'url', 'text': 'Site', 'url': 'https://example.invalid/a'},
         {'row': 1, 'column': 1, 'kind': 'web_app', 'text': 'App', 'url': 'https://example.invalid/app'},
         {'row': 1, 'column': 2, 'kind': 'login_url', 'text': 'Login'},
         {'row': 1, 'column': 3, 'kind': 'switch_inline', 'text': 'Share'},
         {'row': 1, 'column': 4, 'kind': 'url', 'text': 'Bad'}]]
    assert client.calls[-1][0] == 'get_messages' and not any(
        isinstance(call, functions.messages.GetBotCallbackAnswerRequest) for call in client.calls)


# spec: ANYM-681
def test_fingerprints_differ_only_when_callback_bytes_or_the_process_key_change(monkeypatch):
    def fingerprints(data, key=INSPECT_KEY):
        keyed(monkeypatch, key)
        client = Client()
        client.message.reply_markup = inline([callback_button('Pay', data)])
        return run(client, 'inspect_message_buttons')['data']['rows'][0][0]['fp']
    one = fingerprints(b'aaaa')
    # Same label, layout, text and second; only the callback bytes behind the button change.
    assert fingerprints(b'aaaa') == one and fingerprints(b'aaab') != one
    # Two collector processes hold two keys: a fingerprint from one is stale in the other.
    assert fingerprints(b'aaaa', bytes(reversed(range(32)))) != one


# spec: ANYM-681
def test_a_message_without_a_keyboard_has_no_rows_and_a_wrong_message_is_not_found(monkeypatch):
    keyed(monkeypatch)
    client = Client()
    assert run(client, 'inspect_message_buttons')['data']['rows'] == []
    other = Client()
    other.message.id = 24
    with pytest.raises(Exception, match='not_found'):
        run(other, 'inspect_message_buttons')
    elsewhere = Client()
    elsewhere.message.chat_id = 101
    with pytest.raises(Exception, match='not_found'):
        run(elsewhere, 'inspect_message_buttons')


# spec: ANYM-681
def test_the_result_is_capped_at_100_buttons_and_fits_the_receipt_limit(monkeypatch):
    keyed(monkeypatch)
    many = Client()
    many.message.reply_markup = inline(*[[callback_button('b%d' % (10 * r + c), b'd%d' % c) for c in range(10)]
                                         for r in range(15)])
    data = run(many, 'inspect_message_buttons')['data']
    assert sum(len(row) for row in data['rows']) == 100 and data['truncated'] is True
    heavy = Client()
    link = 'https://example.invalid/' + 'a' * 2000
    heavy.message.reply_markup = inline(*[[button('B' * 128, types.InlineButtonTypeUrl(url=link))] for _ in range(100)])
    result = run(heavy, 'inspect_message_buttons')
    shown = result['data']
    assert shown['truncated'] is True and sum(len(row) for row in shown['rows']) > 0
    assert len(json.dumps(safe_result(result))) < 60000
    assert safe_result(run(many, 'inspect_message_buttons'))


# spec: ANYM-681
def test_the_callback_data_and_the_key_appear_nowhere_on_success_or_failure(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    keyed(monkeypatch)
    secrets_seen = (CALLBACK_SECRET.decode(), CALLBACK_SECRET.hex(), INSPECT_KEY.hex(), repr(INSPECT_KEY))
    client = Client()
    client.message.reply_markup = inline([callback_button('Pay')])
    result = run(client, 'inspect_message_buttons')
    text = json.dumps(result) + repr(result) + caplog.text
    assert not any(secret in text for secret in secrets_seen)

    # A failure while fingerprinting or reading formats no exception text at all.
    def boom(*args, **kwargs):
        raise RuntimeError('exploded ' + CALLBACK_SECRET.decode() + INSPECT_KEY.hex())
    monkeypatch.setattr('messaging_service.button_inspection.fingerprint', boom)
    with pytest.raises(CommandRejected) as refused:
        run(client, 'inspect_message_buttons')
    assert refused.value.args == ('provider_preflight_failed',)
    assert not any(secret in repr(refused.value) + caplog.text for secret in secrets_seen)

    class Exploding(Client):
        async def get_messages(self, peer, *, ids):
            raise RuntimeError('provider said ' + CALLBACK_SECRET.decode())
    with pytest.raises(CommandRejected) as refused:
        run(Exploding(), 'inspect_message_buttons')
    assert refused.value.args == ('provider_preflight_failed',) and CALLBACK_SECRET.decode() not in caplog.text


# spec: ANYM-681
@pytest.mark.parametrize('error,code', [
    (errors.ChannelPrivateError(None), 'not_found'), (errors.MessageIdInvalidError(None), 'not_found'),
    (errors.RPCError(None, 'SYNTHETIC_ERROR', 400), 'operation_rejected')])
def test_a_provider_refusal_maps_to_a_safe_code(error, code):
    class Refusing(Client):
        async def get_messages(self, peer, *, ids):
            raise error
    with pytest.raises(CommandRejected) as refused:
        run(Refusing(), 'inspect_message_buttons')
    assert refused.value.args == (code,)


# spec: ANYM-681
def test_the_inspection_is_governed_in_its_own_scope(monkeypatch):
    from messaging_service.telegram_governor import LIVE_BUDGETS
    keyed(monkeypatch)
    assert LIVE_BUDGETS['inspect_buttons'] == (3, 10.0, 300)
    clock, state, governor = governed()
    client = Client()
    client.message.reply_markup = inline([callback_button('Pay')])
    inspect(client, governor)  # entity resolution and the message read: two provider requests
    before = len(client.calls)
    with pytest.raises(CommandRejected) as refused:
        inspect(client, governor)
    assert refused.value.args == ('rate_limited',) and 1 <= refused.value.retry_after <= 10
    assert len(client.calls) == before
    clock.advance(10)
    inspect(client, governor)
    assert len(client.calls) > before


# spec: ANYM-681
# spec: ANYM-681
def test_an_inspection_flood_wait_clears_only_its_own_sdk_entry_through_the_real_call(monkeypatch):
    """Telethon's own _call remembers a FloodWait per RPC constructor, shared with every other get_messages and with
    its update loop; the inspection puts back only the entry of the request that was flood-waited."""
    from telethon import TelegramClient
    from telethon.sessions import MemorySession
    keyed(monkeypatch)
    clock, state, governor = governed()
    unrelated = functions.updates.GetDifferenceRequest.CONSTRUCTOR_ID
    earlier = functions.messages.SendMessageRequest.CONSTRUCTOR_ID
    sent = []

    class Sender:
        flood = True

        def __init__(self, owner):
            self.owner = owner

        def send(self, request, ordered=False):
            sent.append(type(request).__name__)
            future = asyncio.get_running_loop().create_future()
            if self.flood:
                # The SDK's update loop records its own wait while the inspection's request is in flight.
                self.owner._flood_waited_requests[unrelated] = time.time() + 600
                future.set_exception(errors.FloodWaitError(request=request, capture=300))
            else:
                future.set_result(types.messages.Messages(messages=[], topics=[], chats=[], users=[]))
            return future

    class Real(TelegramClient):
        async def get_input_entity(self, peer):
            return types.InputPeerUser(peer, 1) if isinstance(peer, int) else peer

    client = Real(MemorySession(), 1, 'a' * 32, flood_sleep_threshold=0, request_retries=0)
    client._sender = Sender(client)
    client._flood_waited_requests[earlier] = time.time() + 900  # an older, unrelated wait
    kept_earlier = client._flood_waited_requests[earlier]
    with pytest.raises(CommandRejected) as flooded:
        asyncio.run(execute(client, job('inspect_message_buttons'), governor=governor))
    assert flooded.value.retry_after == 300 and state.deferred == []
    waits = client._flood_waited_requests
    assert functions.messages.GetMessagesRequest.CONSTRUCTOR_ID not in waits  # this read's own entry is gone
    assert unrelated in waits and waits[earlier] == kept_earlier  # nothing else was touched
    # The next get_message_info-style read reaches the provider instead of meeting a cached SDK wait.
    client._sender.flood = False
    asyncio.run(client.get_messages(types.InputPeerUser(100, 1), ids=23))
    assert sent.count('GetMessagesRequest') == 2


# spec: ANYM-681
def test_a_keyboard_over_100_rows_or_100_columns_is_marked_truncated(monkeypatch):
    keyed(monkeypatch)
    rows = Client()
    rows.message.reply_markup = inline(*[[callback_button('R%d' % n, b'd%d' % n)] for n in range(101)])
    data = run(rows, 'inspect_message_buttons')['data']
    assert len(data['rows']) == 100 and data['truncated'] is True
    wide = Client()
    wide.message.reply_markup = inline([callback_button('C%d' % n, b'd%d' % n) for n in range(101)])
    data = run(wide, 'inspect_message_buttons')['data']
    assert sum(len(row) for row in data['rows']) == 100 and data['truncated'] is True
    exact = Client()
    exact.message.reply_markup = inline(*[[callback_button('R%d' % n, b'd%d' % n)] for n in range(100)])
    assert 'truncated' not in run(exact, 'inspect_message_buttons')['data']


# spec: ANYM-681
def test_a_failed_inspection_prints_no_secret_in_its_exception_chain(monkeypatch):
    import traceback
    keyed(monkeypatch)
    client = Client()
    client.message.reply_markup = inline([callback_button('Pay')])

    def boom(*args, **kwargs):
        raise RuntimeError('exploded ' + CALLBACK_SECRET.decode() + INSPECT_KEY.hex())
    monkeypatch.setattr('messaging_service.button_inspection.fingerprint', boom)
    with pytest.raises(CommandRejected) as refused:
        run(client, 'inspect_message_buttons')

    class Exploding(Client):
        async def get_messages(self, peer, *, ids):
            raise RuntimeError('provider said ' + CALLBACK_SECRET.decode() + INSPECT_KEY.hex())
    with pytest.raises(CommandRejected) as read:
        run(Exploding(), 'inspect_message_buttons')

    class Refusing(Client):
        async def get_messages(self, peer, *, ids):
            raise errors.RPCError(None, 'SYNTHETIC_' + CALLBACK_SECRET.decode(), 400)
    with pytest.raises(CommandRejected) as mapped:
        run(Refusing(), 'inspect_message_buttons')
    assert mapped.value.args == ('operation_rejected',)
    for error in (refused.value, read.value, mapped.value):
        chain = ''.join(traceback.format_exception(error))
        assert CALLBACK_SECRET.decode() not in chain and INSPECT_KEY.hex() not in chain


# spec: ANYM-681
def test_a_scoped_flood_wait_pauses_only_the_inspection_and_a_long_one_the_account(monkeypatch):
    keyed(monkeypatch)
    clock, state, governor = governed()

    class Flooding(Client):
        async def get_messages(self, peer, *, ids):
            raise errors.FloodWaitError(None, capture=300)
    with pytest.raises(CommandRejected) as flooded:
        inspect(Flooding(), governor)
    assert flooded.value.retry_after == 300 and state.deferred == []
    calls = []

    async def other(name):
        async def work():
            calls.append(name)
        await governor.call(work, name)
    asyncio.run(other('provider_send'))
    asyncio.run(other('telegram_history'))
    assert calls == ['provider_send', 'telegram_history']  # sends and collection proceed
    paused = Client()
    with pytest.raises(CommandRejected) as refused:
        inspect(paused, governor)
    assert refused.value.retry_after >= 290 and paused.calls == []  # only inspection is refused
    # A FloodWait longer than the scoped maximum pauses the account, as search does.
    clock2, state2, governor2 = governed()

    class Long(Client):
        async def get_messages(self, peer, *, ids):
            raise errors.FloodWaitError(None, capture=7200)
    with pytest.raises(CommandRejected):
        inspect(Long(), governor2)
    assert state2.deferred == [7200]


# --- ANYM-676: press_button -------------------------------------------------------------------------------------
from messaging_service import button_press_executor
from messaging_service.button_press import layout_digest, text_digest
from messaging_service.message_buttons import build_rows

PRESS_KEY = bytes(range(32))


class PressClient(Client):
    """A bot message with a Pay and a Cancel callback button; get_messages also answers the newest-message probe."""

    def __init__(self, data=CALLBACK_SECRET):
        super().__init__()
        self.message.out = False
        self.message.edit_date = None
        self.message.message = 'Pay now'
        self.message.reply_markup = inline([callback_button('Pay', data), callback_button('Cancel', b'cancel-data')])
        self.newer = None
        self.reread_error = None
        self.reads = 0

    async def get_messages(self, peer, *, ids=None, limit=None):
        self.calls.append(('get_messages', peer, ids, limit))
        if limit is not None:
            return [self.newer or self.message]
        self.reads += 1
        if self.reads > 1 and self.reread_error:
            raise self.reread_error
        return self.message

    def pressed(self):
        return [c for c in self.calls if isinstance(c, functions.messages.GetBotCallbackAnswerRequest)]


def press_block(message, row=0, column=0, label='Pay', data=CALLBACK_SECRET):
    rows, _ = build_rows(message.reply_markup)
    moment = message.edit_date or message.date
    return {'row': row, 'column': column, 'label': label, 'edit_date': moment.isoformat(),
            'fp': button_fingerprint.fingerprint(100, message.id, row, column, data, key=PRESS_KEY),
            'layout_digest': layout_digest(rows), 'text_sha256': text_digest(message.message)}


def press(client, block=None, governor=None, **kwargs):
    block = block or press_block(client.message)
    parameters = {key: block[key] for key in ('row', 'column', 'label', 'fp')}
    return asyncio.run(execute(client, job('press_button', parameters, press=block, **kwargs), governor=governor))


@pytest.fixture
def pressing(monkeypatch):
    monkeypatch.setattr(button_fingerprint, '_KEY', PRESS_KEY)
    monkeypatch.setattr(button_press_executor, 'AFTER_DELAY', 0)


# spec: ANYM-676
def test_a_press_sends_exactly_one_request_and_returns_the_answer_and_the_message_afterwards(pressing):
    client = PressClient()
    client.responses[functions.messages.GetBotCallbackAnswerRequest] = types.messages.BotCallbackAnswer(
        cache_time=0, alert=True, message='Order placed')
    result = press(client)
    data = result['data']
    assert result['outcome'] == 'submitted' and data['effect_verified'] is False
    assert data['answer'] == {'message': 'Order placed', 'alert': True, 'url': None}
    assert data['message_after'] == {'edited': False, 'deleted': False, 'text': 'Pay now', 'new_messages': False,
                                     'buttons': [{'text': 'Pay', 'kind': 'callback'}, {'text': 'Cancel', 'kind': 'callback'}]}
    assert data['pressed_at'] and 'pressed_at' in data['next_action'] and safe_result(result)
    request, = client.pressed()
    assert request.msg_id == 23 and request.data == CALLBACK_SECRET and len(client.pressed()) == 1


# spec: ANYM-676
@pytest.mark.parametrize('change', [
    lambda m: setattr(m, 'reply_markup', inline([callback_button('Pay'), callback_button('Cancel', b'cancel-data'),
                                                 callback_button('Extra', b'e')])),  # layout
    lambda m: setattr(m, 'message', 'Pay later'),  # text
    lambda m: setattr(m, 'edit_date', datetime(2026, 10, 14, 0, 0, 5, tzinfo=timezone.utc)),  # edit_date
    lambda m: setattr(m, 'reply_markup', inline([callback_button('Pay', b'DIFFERENT-BYTES'),
                                                 callback_button('Cancel', b'cancel-data')])),  # callback bytes only
    lambda m: setattr(m, 'reply_markup', inline([callback_button('Pay', requires_password=True),
                                                 callback_button('Cancel', b'cancel-data')])) or
              setattr(m, 'message', 'x'),  # a changed message and a password button: the password code wins below
], ids=['layout', 'text', 'edit_date', 'callback_bytes_only', 'password_and_text'])
def test_a_press_is_refused_when_the_live_message_differs_from_the_preview_and_nothing_is_sent(pressing, change):
    client = PressClient()
    block = press_block(client.message)
    change(client.message)
    with pytest.raises(CommandRejected) as refused:
        press(client, block)
    assert refused.value.args[0] in ('message_changed', 'password_required') and client.pressed() == []
    if refused.value.args[0] == 'message_changed':
        live = refused.value.data['live']
        assert set(live) == {'edit_date', 'text', 'labels'} and CALLBACK_SECRET.decode() not in repr(live)


# spec: ANYM-676
def test_the_callback_bytes_swap_inside_the_same_second_changes_only_the_fingerprint(pressing):
    client = PressClient()
    block = press_block(client.message)
    client.message.reply_markup = inline([callback_button('Pay', b'SWAPPED'), callback_button('Cancel', b'cancel-data')])
    rows, _ = build_rows(client.message.reply_markup)
    assert layout_digest(rows) == block['layout_digest'] and client.message.message == 'Pay now'  # all else equal
    with pytest.raises(CommandRejected, match='message_changed'):
        press(client, block)
    assert client.pressed() == []


# spec: ANYM-676
def test_a_password_button_a_via_bot_message_and_other_kinds_are_not_pressed(pressing):
    client = PressClient()
    block = press_block(client.message)
    client.message.reply_markup = inline([callback_button('Pay', requires_password=True)])
    with pytest.raises(CommandRejected, match='password_required'):
        press(client, block)
    client = PressClient()
    block = press_block(client.message)
    client.message.via_bot_id = 777
    with pytest.raises(CommandRejected, match='unsupported_operation'):
        press(client, block)
    client = PressClient()
    block = press_block(client.message)
    client.message.reply_markup = inline([button('Pay', types.InlineButtonTypeUrl(url='https://example.invalid/a'))])
    with pytest.raises(CommandRejected, match='message_changed'):
        press(client, block)
    assert client.pressed() == []


# spec: ANYM-676
@pytest.mark.parametrize('error', [errors.BotResponseTimeoutError(None), errors.rpcerrorlist.TimeoutError(None)])
def test_a_bot_response_timeout_is_a_delivered_press(pressing, error):
    client = PressClient()
    client.responses[functions.messages.GetBotCallbackAnswerRequest] = error
    result = press(client)
    assert result['outcome'] == 'submitted' and result['data']['answer'] is None
    assert result['data']['answer_timeout'] is True and result['data']['message_after']['deleted'] is False
    assert len(client.pressed()) == 1


# spec: ANYM-676
def test_a_connection_loss_after_dispatch_is_unknown_with_exactly_one_request(pressing, caplog):
    client = PressClient()
    client.responses[functions.messages.GetBotCallbackAnswerRequest] = ConnectionError('boom ' + CALLBACK_SECRET.decode())
    result = press(client)
    assert result == {'outcome': 'unknown', 'data': {'error': 'provider_result_unverified'}}
    assert len(client.pressed()) == 1 and CALLBACK_SECRET.decode() not in repr(result) + caplog.text


# spec: ANYM-676
@pytest.mark.parametrize('error,code', [
    (errors.DataInvalidError(None), 'message_changed'), (errors.PasswordMissingError(None), 'password_required'),
    (errors.MessageIdInvalidError(None), 'not_found'), (errors.MsgIdInvalidError(None), 'not_found'),
    (errors.PeerIdInvalidError(None), 'not_found'), (errors.ChannelInvalidError(None), 'not_found'),
    (errors.ChannelPrivateError(None), 'not_found')])
def test_a_definite_provider_refusal_is_a_failed_press_not_an_unknown_one(pressing, error, code):
    client = PressClient()
    client.responses[functions.messages.GetBotCallbackAnswerRequest] = error
    with pytest.raises(CommandRejected) as refused:
        press(client)
    assert refused.value.args == (code,) and len(client.pressed()) == 1


# spec: ANYM-676
def test_callback_bytes_reach_no_result_log_or_exception_text_and_an_echoing_answer_is_redacted(pressing, caplog):
    import logging
    import traceback
    caplog.set_level(logging.DEBUG)
    client = PressClient()
    client.responses[functions.messages.GetBotCallbackAnswerRequest] = types.messages.BotCallbackAnswer(
        cache_time=0, message='you pressed ' + CALLBACK_SECRET.decode(),
        url='https://example.invalid/?q=' + CALLBACK_SECRET.decode())
    result = press(client)
    answer = result['data']['answer']
    assert answer == {'message': '[redacted]', 'alert': False, 'url': '[redacted]', 'answer_redacted': True}
    assert CALLBACK_SECRET.decode() not in repr(result) + caplog.text
    short = PressClient(data=b'ab')  # under 8 bytes is not a credential and is not suppressed
    short.responses[functions.messages.GetBotCallbackAnswerRequest] = types.messages.BotCallbackAnswer(cache_time=0, message='ab ok')
    assert press(short, press_block(short.message, data=b'ab'))['data']['answer']['message'] == 'ab ok'
    # A failure whose text carries the bytes: unknown, a fixed code, and no chain that prints them.
    failing = PressClient()
    failing.responses[functions.messages.GetBotCallbackAnswerRequest] = RuntimeError('provider said ' + CALLBACK_SECRET.decode())
    outcome = press(failing)
    assert outcome == {'outcome': 'unknown', 'data': {'error': 'provider_result_unverified'}}
    assert CALLBACK_SECRET.decode() not in repr(outcome) + caplog.text
    refused = PressClient()
    block = press_block(refused.message)
    refused.message.reply_markup = inline([callback_button('Pay', b'OTHER-BYTES-123')])
    with pytest.raises(CommandRejected) as rejected:
        press(refused, block)
    assert CALLBACK_SECRET.decode() not in ''.join(traceback.format_exception(rejected.value)) + repr(rejected.value.data)


# spec: ANYM-676
def test_the_answer_url_is_data_kept_only_when_it_passes_the_link_rule(pressing):
    client = PressClient()
    client.responses[functions.messages.GetBotCallbackAnswerRequest] = types.messages.BotCallbackAnswer(
        cache_time=0, message='x', url='https://example.invalid/ok')
    assert press(client)['data']['answer']['url'] == 'https://example.invalid/ok'
    client.responses[functions.messages.GetBotCallbackAnswerRequest] = types.messages.BotCallbackAnswer(
        cache_time=0, message='x', url='javascript:alert(1)')
    answer = press(client)['data']['answer']
    assert answer['url'] is None and answer['url_dropped'] is True
    assert not any(call[0] == 'get_url' for call in client.calls if isinstance(call, tuple))


# spec: ANYM-676
def test_a_failed_reread_never_changes_the_outcome_and_a_new_message_or_edit_is_reported(pressing):
    client = PressClient()
    client.reread_error = RuntimeError('reread failed ' + CALLBACK_SECRET.decode())
    result = press(client)
    assert result['outcome'] == 'submitted' and result['data']['message_after'] is None
    assert result['data']['message_after_code'] == 'reread_failed' and CALLBACK_SECRET.decode() not in repr(result)
    edited = PressClient()
    block = press_block(edited.message)

    class Edits(PressClient):
        async def get_messages(self, peer, *, ids=None, limit=None):
            if ids is not None and self.reads:
                self.message = NS(**{**vars(self.message), 'message': 'Paid', 'edit_date': datetime(2026, 10, 14, 0, 1, tzinfo=timezone.utc)})
            if limit is not None:
                return [NS(id=24)] if self.reads > 1 else [self.message]  # message 24 appears only after the press
            return await super().get_messages(peer, ids=ids, limit=limit)
    live = Edits()
    after = press(live, block)['data']['message_after']
    assert after['edited'] is True and after['new_messages'] is True and after['text'] == 'Paid'


# spec: ANYM-676
def test_presses_are_paced_per_account_in_memory_and_refuse_before_any_request(pressing):
    from messaging_service.telegram_governor import LIVE_BUDGETS
    assert LIVE_BUDGETS['press_button'] == (3, 5.0, 200)
    clock, state, governor = governed()
    governor.interval = 0  # the fake clock then moves only when the test moves it
    for _ in range(3):
        press(PressClient(), governor=governor)
    client = PressClient()
    with pytest.raises(CommandRejected) as limited:
        press(client, governor=governor)
    assert limited.value.args == ('rate_limited',) and limited.value.retry_after >= 1 and client.calls == []


# spec: ANYM-676
def test_a_message_that_already_existed_before_the_press_is_not_a_visible_change(pressing):
    client = PressClient()
    client.newer = NS(id=24)  # the chat already holds message 24 when the press goes out and when it is re-read
    client.responses[functions.messages.GetBotCallbackAnswerRequest] = errors.BotResponseTimeoutError(None)
    after = press(client)['data']['message_after']
    assert after['new_messages'] is False and after['edited'] is False


# spec: ANYM-676
def test_callback_bytes_echoed_by_the_bot_in_its_edited_message_are_redacted(pressing):
    client = PressClient()

    class Echo(PressClient):
        async def get_messages(self, peer, *, ids=None, limit=None):
            if ids is not None and self.reads:
                self.message = NS(**{**vars(self.message), 'message': 'echo ' + CALLBACK_SECRET.decode(),
                                     'reply_markup': inline([callback_button('pressed ' + CALLBACK_SECRET.decode(), b'z')])})
            return await super().get_messages(peer, ids=ids, limit=limit)
    echo = Echo()
    result = press(echo, press_block(client.message))
    after = result['data']['message_after']
    assert after['redacted'] is True and after['text'] == '[redacted]'
    assert after['buttons'] == [{'text': '[redacted]', 'kind': 'callback'}]
    assert CALLBACK_SECRET.decode() not in repr(result)


# spec: ANYM-676
def test_pressed_at_is_taken_before_the_request_leaves(pressing, monkeypatch):
    events = []
    dispatch_time = datetime(2026, 10, 14, 9, 0, 1, tzinfo=timezone.utc)

    def controlled():
        events.append('now')
        return dispatch_time if events.count('now') == 1 else dispatch_time + timedelta(seconds=30)

    class Slow(PressClient):
        async def __call__(self, request):
            if isinstance(request, functions.messages.GetBotCallbackAnswerRequest):
                events.append('request')
            return await super().__call__(request)
    monkeypatch.setattr(button_press_executor, 'now_utc', controlled)
    result = press(Slow())
    assert events[:2] == ['now', 'request']  # the timestamp is taken first, once
    assert result['data']['pressed_at'] == '2026-10-14T09:00:01+00:00'


# spec: ANYM-676
def test_a_preflight_that_rejects_the_saved_revision_after_the_press_does_not_block_the_reread(pressing):
    """Capture archives the bot's edit, so the command's saved expected_revision is stale once the press is delivered."""
    client = PressClient()

    class Edits(PressClient):
        async def get_messages(self, peer, *, ids=None, limit=None):
            if ids is not None and self.reads:
                self.message = NS(**{**vars(self.message), 'message': 'Paid',
                                     'edit_date': datetime(2026, 10, 14, 0, 1, tzinfo=timezone.utc)})
            return await super().get_messages(peer, ids=ids, limit=limit)
    live = Edits()
    block = press_block(client.message)

    async def preflight():
        if live.pressed():
            raise CommandRejected('operation_target_changed')
    policy_checks = []
    work = job('press_button', {k: block[k] for k in ('row', 'column', 'label', 'fp')}, press=block, _preflight=preflight)
    work['_policy'] = {'enabled': True, 'discover_chats': True, 'include_chats': [], 'exclude_chats': []}
    clock, state, governor = governed()
    governor.interval = 0
    original = governor.call

    async def watched(operation, phase, **kwargs):
        policy_checks.append(phase)
        return await original(operation, phase, **kwargs)
    governor.call = watched
    result = asyncio.run(execute(live, work, governor=governor))
    assert result['outcome'] == 'submitted' and result['data']['message_after']['text'] == 'Paid'
    assert result['data']['message_after']['edited'] is True and policy_checks  # read through the governor
    excluded = job('press_button', {k: block[k] for k in ('row', 'column', 'label', 'fp')}, press=block)
    assert excluded['_policy']['exclude_chats'] == []


# spec: ANYM-676
@pytest.mark.parametrize('echo', [CALLBACK_SECRET.decode() + ' ', '   ' + CALLBACK_SECRET.decode(),
                                  'x' * 200 + CALLBACK_SECRET.decode(), CALLBACK_SECRET.decode()[:10] + '\x07' + 'tail'])
def test_the_echo_of_callback_bytes_is_found_in_the_raw_label_before_cleaning_or_truncation(pressing, echo):
    client = PressClient(data=CALLBACK_SECRET + b' ' if echo.endswith(' ') and not echo.startswith(' ') else CALLBACK_SECRET)
    block = press_block(client.message, data=CALLBACK_SECRET + b' ' if echo.endswith(' ') and not echo.startswith(' ') else CALLBACK_SECRET)

    class Echo(PressClient):
        async def get_messages(self, peer, *, ids=None, limit=None):
            if ids is not None and self.reads:
                self.message = NS(**{**vars(self.message), 'reply_markup': inline([callback_button(echo, b'z')])})
            return await super().get_messages(peer, ids=ids, limit=limit)
    live = Echo(data=CALLBACK_SECRET + b' ' if echo.endswith(' ') and not echo.startswith(' ') else CALLBACK_SECRET)
    result = press(live, block)
    after = result['data']['message_after']
    if echo.startswith(CALLBACK_SECRET.decode()[:10] + '\x07'):
        assert not after.get('redacted')  # a control character inside is a different string: not an echo
    else:
        assert after['redacted'] is True and after['buttons'] == [{'text': '[redacted]', 'kind': 'callback'}]
    assert CALLBACK_SECRET.decode() not in repr(result)


# spec: ANYM-676
def test_an_exhausted_deadline_or_a_shutdown_keeps_the_delivered_press_submitted(pressing):
    skipped = PressClient()
    block = press_block(skipped.message)
    result = asyncio.run(execute(skipped, job('press_button', {k: block[k] for k in ('row', 'column', 'label', 'fp')},
                                              press=block, _ends_at=time.monotonic() + 0.5)))
    assert result['outcome'] == 'submitted' and result['data']['message_after'] is None
    assert result['data']['message_after_code'] == 'reread_skipped'
    interrupted = PressClient()
    interrupted.reread_error = asyncio.CancelledError()
    marker = job('press_button', {k: block[k] for k in ('row', 'column', 'label', 'fp')}, press=block)
    result = asyncio.run(execute(interrupted, marker))
    assert result['outcome'] == 'submitted' and result['data']['message_after_code'] == 'reread_interrupted'
    assert marker['_cancel_after_settle'] is True  # the worker settles the receipt, then resumes the cancellation
    slow = PressClient()
    original = button_press_executor.message_after

    async def never(*args, **kwargs):
        await asyncio.sleep(30)
    button_press_executor.message_after = never
    try:
        late = asyncio.run(execute(slow, job('press_button', {k: block[k] for k in ('row', 'column', 'label', 'fp')},
                                             press=block, _ends_at=time.monotonic() + 4.0)))
    finally:
        button_press_executor.message_after = original
    assert late['outcome'] == 'submitted' and late['data']['message_after_code'] == 'reread_timeout'


# spec: ANYM-676
def test_a_reconnect_never_replays_a_pending_press_but_replays_other_requests():
    """Telethon re-queues every pending request after a reconnect, whatever request_retries says."""
    import collections
    import logging
    from telethon.network import MTProtoSender
    from telethon.network.requeststate import RequestState
    press_request = functions.messages.GetBotCallbackAnswerRequest(types.InputPeerUser(1, 1), 23, data=b'x')
    other_request = functions.messages.GetMessagesRequest(id=[types.InputMessageID(1)])

    class Connection:
        async def disconnect(self):
            pass

    async def scenario(guarded):
        sender = MTProtoSender(None, loggers=collections.defaultdict(lambda: logging.getLogger('synthetic')))
        sender._user_connected, sender._connection = True, Connection()

        async def connect():
            pass
        sender._connect = connect
        sender._send_loop_handle = asyncio.ensure_future(asyncio.sleep(0))
        sender._recv_loop_handle = asyncio.ensure_future(asyncio.sleep(0))
        await asyncio.sleep(0.01)
        pressing_state, other_state = RequestState(press_request), RequestState(other_request)
        pressing_state.msg_id, other_state.msg_id = 1, 2
        sender._pending_state = {1: pressing_state, 2: other_state}
        client = NS(_sender=sender)
        if guarded:
            with button_press_executor.without_transport_replay(client, press_request):
                await sender._reconnect(ConnectionError('lost'))
            assert '_reconnect' not in sender.__dict__  # restored
        else:
            await sender._reconnect(ConnectionError('lost'))
        queued = [state.request for state in list(sender._send_queue._deque)]
        return queued, pressing_state
    async def cancelled_then_reconnect():
        sender = MTProtoSender(None, loggers=collections.defaultdict(lambda: logging.getLogger('synthetic')))
        sender._user_connected, sender._connection = True, Connection()

        async def connect():
            pass
        sender._connect = connect
        sender._send_loop_handle = asyncio.ensure_future(asyncio.sleep(0))
        sender._recv_loop_handle = asyncio.ensure_future(asyncio.sleep(0))
        await asyncio.sleep(0.01)
        dispatched, other_state, unsent = RequestState(press_request), RequestState(other_request), RequestState(press_request)
        dispatched.msg_id, other_state.msg_id = 1, 2
        sender._pending_state = {1: dispatched, 2: other_state}
        sender._send_queue.append(unsent)  # queued, never sent
        client = NS(_sender=sender)
        with pytest.raises(asyncio.CancelledError):  # the worker's 30 s timeout cancels the awaiting call
            with button_press_executor.without_transport_replay(client, press_request):
                raise asyncio.CancelledError()
        await sender._reconnect(ConnectionError('lost later'))  # a LATER reconnect, after the guard was removed
        return [state.request for state in list(sender._send_queue._deque)]
    later = asyncio.run(cancelled_then_reconnect())
    assert press_request not in later and later == [other_request]
    plain, _ = asyncio.run(scenario(False))
    assert press_request in plain  # the transport would have sent the callback a second time
    queued, state = asyncio.run(scenario(True))
    assert press_request not in queued and other_request in queued
    assert isinstance(state.future.exception(), ConnectionError)
