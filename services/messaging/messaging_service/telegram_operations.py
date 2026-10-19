"""Allowlisted Telegram operations through the existing Telethon session owner.

No arbitrary TL proxy, provider login or automatic mutation retry lives here.
The command backend validates authorization and prepares provider references and
registered originals. Every operation still validates its bounded native inputs;
an acknowledged request without proof of its effect is only ``submitted``.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time

from telethon import errors, functions, types, utils


MAX_ORIGINAL_BYTES = 32 * 1024 * 1024
MAX_PROFILE_PAGE = 50
READ_OPERATIONS = frozenset({
    'chat_info', 'list_participants', 'get_reactions', 'get_message_info',
    'list_topics', 'get_profile', 'list_contacts', 'get_contact', 'list_blocked',
    'get_invite_link',
})
PARAMETERS = {
    'chat_info': set(), 'list_participants': {'limit', 'offset'},
    'get_reactions': set(), 'get_message_info': set(),
    'list_topics': {'limit', 'offset'},
    'get_profile': set(), 'list_contacts': {'limit', 'offset'},
    'get_contact': {'user_id'}, 'list_blocked': {'limit', 'offset'},
    'send_file': {'mode', 'caption', 'attachment_id', 'upload_id', 'sha256'},
    'forward_message': {'source_message_id', 'source_revision'},
    'send_poll': {'question', 'options', 'multiple'},
    'send_contact': {'phone', 'first_name', 'last_name'},
    'send_location': {'latitude', 'longitude', 'description'}, 'edit_message': {'text'},
    'delete_message': set(), 'send_reaction': {'emoji'},
    'vote_poll': {'option_indices'}, 'pin_message': set(), 'unpin_message': set(),
    'mark_read': set(), 'mark_unread': set(), 'archive_chat': set(),
    'unarchive_chat': set(), 'mute_chat': {'seconds'}, 'unmute_chat': set(),
    'create_group': {'title', 'user_ids'}, 'rename_group': {'title'},
    'set_group_description': {'description'}, 'add_participant': {'user_id'},
    'remove_participant': {'user_id'}, 'promote_participant': {'user_id'},
    'demote_participant': {'user_id'}, 'leave_group': set(), 'get_invite_link': set(),
    'revoke_invite_link': set(), 'add_contact': {'phone', 'first_name', 'last_name'},
    'delete_contact': {'user_id'}, 'block_contact': {'user_id'},
    'unblock_contact': {'user_id'}, 'update_profile': {'first_name', 'last_name', 'bio'},
    'set_group_messages_admins_only': {'enabled'}, 'set_group_info_admins_only': {'enabled'},
    'set_slow_mode': {'seconds'}, 'list_join_requests': {'limit'},
    'approve_join_requests': {'user_ids'}, 'reject_join_requests': {'user_ids'},
}
# ANYM-242: each setter owns one value; the default rights flag it changes, or slow mode.
GROUP_SETTERS = {'set_group_messages_admins_only': ('messages_admins_only', 'enabled', 'send_messages'),
                 'set_group_info_admins_only': ('info_admins_only', 'enabled', 'change_info'),
                 'set_slow_mode': ('slow_mode_seconds', 'seconds', None)}
SLOW_MODE_SECONDS = (0, 10, 30, 60, 300, 900, 3600)
BANNED_RIGHTS_FIELDS = tuple(inspect.signature(types.ChatBannedRights.__init__).parameters)[1:]
# Definite per-ID refusals of HideChatJoinRequest; any other error leaves that ID unknown.
JOIN_REFUSALS = {'HIDE_REQUESTER_MISSING': 'no_pending_request', 'USER_ALREADY_PARTICIPANT': 'already_participant',
                 'INPUT_USER_DEACTIVATED': 'user_unavailable', 'USER_ID_INVALID': 'user_unavailable',
                 'USER_CHANNELS_TOO_MUCH': 'too_many_chats', 'CHANNELS_TOO_MUCH': 'too_many_chats'}
# Telethon raises a typed error per known name; its message is the generic class text.
JOIN_REFUSAL_ERRORS = {errors.rpcerrorlist.rpc_errors_dict[name]: name for name in JOIN_REFUSALS}
JOIN_REQUESTS_BYTES = 48000


def reject(code):
    # Lazy import keeps the pure native adapter independent of backend startup.
    from .commands import CommandRejected
    raise CommandRejected(code)


def bounded_text(value, maximum, *, empty=False):
    if (not isinstance(value, str) or
            (not empty and not value.strip()) or any(
                ord(c) < 32 and c not in '\n\r\t' or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        reject('invalid_parameters')
    if len(value.encode('utf-16-le')) // 2 > maximum:
        reject('invalid_parameters')
    return value


def number_id(value, *, marked=False):
    if isinstance(value, int) and type(value) is not bool:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r'-?[1-9][0-9]{0,18}', value):
        reject('invalid_parameters')
    result = int(value)
    if not -(2**63) < result < 2**63 or (not marked and result <= 0):
        reject('invalid_parameters')
    return result


def integer(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        reject('invalid_parameters')
    return value


def profile(user):
    """Whitelist public/contact fields; never serialize complete TL objects."""
    if not isinstance(user, types.User):
        return None
    return {'id': str(user.id), 'first_name': safe_text(user.first_name, 64),
            'last_name': safe_text(user.last_name, 64),
            'username': safe_text(user.username, 32), 'is_bot': bool(user.bot),
            'phone': safe_text(user.phone, 32)}


def safe_text(value, maximum):
    if not isinstance(value, str):
        return None
    return ''.join(c for c in value[:maximum] if
        (ord(c) >= 32 or c in '\n\r\t') and ord(c) != 127 and not 0xD800 <= ord(c) <= 0xDFFF)


def utf16_text(value, maximum):
    """safe_text bounded to maximum UTF-16 code units."""
    text, units, out = safe_text(value, maximum), 0, []
    if text is None:
        return None
    for c in text:
        units += 2 if ord(c) > 0xFFFF else 1
        if units > maximum:
            break
        out.append(c)
    return ''.join(out)


def group_settings(entity, details):
    """Group restrictions as the provider reported them; None where it gave no value."""
    rights = getattr(entity, 'default_banned_rights', None)
    rights = rights if isinstance(rights, types.ChatBannedRights) else None
    slow = getattr(details, 'slowmode_seconds', None)
    return {'messages_admins_only': bool(rights.send_messages) if rights else None,
            'info_admins_only': bool(rights.change_info) if rights else None,
            'slow_mode_seconds': (slow if type(slow) is int else 0) if getattr(entity, 'megagroup', False) else None,
            'join_approval': None}


def group_admin(entity, right):
    rights = getattr(entity, 'admin_rights', None)
    return getattr(entity, 'creator', False) is True or bool(rights and getattr(rights, right, False))


def safe_int(value):
    return value if type(value) is int and 0 <= value < 2**63 else None


def peer_id(peer):
    try:
        return str(utils.get_peer_id(peer))
    except (TypeError, ValueError, AttributeError):
        return None


def reaction_state(reactions):
    if not isinstance(reactions, types.MessageReactions):
        return {'state': 'unknown', 'counts': [], 'recent': []}
    counts = []
    for count in reactions.results[:50]:
        if not isinstance(count, types.ReactionCount):
            continue
        reaction = count.reaction
        if isinstance(reaction, types.ReactionEmoji):
            item = {'type': 'emoji', 'emoji': safe_text(reaction.emoticon, 32)}
        elif isinstance(reaction, types.ReactionCustomEmoji):
            item = {'type': 'custom', 'document_id': str(reaction.document_id)}
        elif isinstance(reaction, types.ReactionPaid):
            item = {'type': 'paid'}
        else:
            item = {'type': 'unknown'}
        counts.append({**item, 'count': safe_int(count.count),
                       'chosen': count.chosen_order is not None})
    recent = []
    for item in (reactions.recent_reactions or [])[:50]:
        if not isinstance(item, types.MessagePeerReaction):
            continue
        recent.append({'peer_id': peer_id(item.peer_id),
                       'emoji': safe_text(getattr(item.reaction, 'emoticon', None), 32),
                       'provider_at': item.date.isoformat() if isinstance(item.date, datetime) else None,
                       'mine': bool(item.my)})
    return {'state': 'partial' if reactions.min else 'observed', 'counts': counts,
            'recent': recent, 'actor_list_available': bool(reactions.can_see_list),
            'actors_complete': False,
            'truncated': len(reactions.results) > 50 or len(reactions.recent_reactions or []) > 50}


def message_info(message):
    body = getattr(message, 'message', '') or ''
    reply = getattr(message, 'reply_to', None)
    media = getattr(message, 'media', None)
    result = {'provider_message_id': str(message.id), 'provider_chat_id': str(message.chat_id),
              'text': safe_text(body, 4000), 'text_truncated': len(body) > 4000,
              'outgoing': getattr(message, 'out', None) is True,
              'provider_at': message.date.isoformat() if isinstance(getattr(message, 'date', None), datetime) else None,
              'album_id': str(message.grouped_id) if getattr(message, 'grouped_id', None) is not None else None,
              'reply_to_provider_id': str(reply.reply_to_msg_id) if getattr(reply, 'reply_to_msg_id', None) else None,
              'topic_provider_id': str(reply.reply_to_top_id) if getattr(reply, 'reply_to_top_id', None) else None,
              'reactions': reaction_state(getattr(message, 'reactions', None)),
              'basis': 'live_provider_observation'}
    if isinstance(media, types.MessageMediaPoll):
        poll = media.poll
        votes = {item.option: item for item in (media.results.results or [])[:100]}
        result['poll'] = {'id': str(poll.id), 'question': safe_text(poll.question.text, 512),
            'closed': bool(poll.closed), 'multiple_choice': bool(poll.multiple_choice),
            'public_voters': bool(poll.public_voters),
            'options': [{'index': index, 'text': safe_text(answer.text.text, 256),
                         'voters': safe_int(getattr(votes.get(answer.option), 'voters', None)),
                         'chosen': bool(getattr(votes.get(answer.option), 'chosen', False))}
                        for index, answer in enumerate(poll.answers[:12])],
            'total_voters': safe_int(media.results.total_voters),
            'results_partial': bool(media.results.min), 'truncated': len(poll.answers) > 12}
    return result


async def _execute(client, job, *, verified_self=None, governor=None):
    """Execute exactly one prepared, bounded operation without automatic retries.

    The backend supplies trusted provider IDs and, for send_file, a registered
    original path. It is responsible for approval, ACLs, target revisions and
    leases. Expiry is rechecked inside the governor immediately before mutation.
    """
    if not isinstance(job, dict):
        reject('invalid_parameters')
    operation = job.get('operation')
    if operation not in PARAMETERS:
        reject('unsupported_operation')
    parameters = job.get('parameters', {})
    if not isinstance(parameters, dict) or set(parameters) - PARAMETERS[operation]:
        reject('invalid_parameters')
    expiry = job.get('expires_at')
    if type(expiry) not in (int, float) or not math.isfinite(expiry):
        reject('invalid_parameters')

    def check_expiry():
        if time.time() >= job['expires_at']:
            reject('operation_expired')

    check_expiry()
    started_mutation = False
    report = job.get('_provider_phase')
    if report is not None:
        # The shared rate gate and lookups may delay the first mutation.
        report('waiting')
    stream = None
    referenced_peers = set()

    def collection_policy():
        policy = job.get('_policy')
        if (not isinstance(policy, dict) or policy.get('enabled') is False or
                type(policy.get('discover_chats')) is not bool or
                not isinstance(policy.get('include_chats'), (list, tuple)) or
                not isinstance(policy.get('exclude_chats'), (list, tuple))):
            reject('operation_access_changed')
        return policy

    def allowed_provider_id(value):
        policy = collection_policy()
        return str(value) not in policy['exclude_chats'] and (
            policy['discover_chats'] or str(value) in policy['include_chats'])

    def check_policy():
        policy = collection_policy()
        if any(not allowed_provider_id(peer) for peer in referenced_peers):
            reject('operation_access_changed')
        if operation == 'add_contact' and (not policy['discover_chats'] or policy['exclude_chats']):
            # ImportContacts mutates before revealing the remote user ID. A
            # restricted collection policy cannot safely authorize that import.
            reject('unsupported_operation')

    check_policy()

    async def rpc(fn, *, mutation=False):
        async def guarded():
            nonlocal started_mutation
            check_expiry()
            preflight = job.get('_preflight')
            if preflight is not None:
                if not callable(preflight):
                    reject('operation_access_changed')
                await preflight()
            check_expiry()
            check_policy()
            if mutation:
                started_mutation = True
                if report is not None:
                    report('started')
            return await fn()
        return await governor.call(guarded, 'provider_command') if governor else await guarded()

    async def invoke(request, *, mutation=False):
        return await rpc(lambda: client(request), mutation=mutation)

    async def input_peer(value):
        return await rpc(lambda: client.get_input_entity(number_id(value, marked=True)))

    async def user_input(value):
        candidate = await input_peer(number_id(value))
        if not isinstance(candidate, types.InputPeerUser):
            reject('invalid_parameters')
        return utils.get_input_user(candidate)

    async def exact_message(peer, chat, message_id, *, own=False):
        message = await rpc(lambda: client.get_messages(peer, ids=message_id))
        if (message is None or getattr(message, 'id', None) != message_id or
                str(getattr(message, 'chat_id', None)) != str(chat)):
            reject('provider_reference_mismatch')
        if own and getattr(message, 'out', None) is not True:
            reject('message_not_owned')
        return message

    def accepted(value, *, message=False, chat=None, message_id=None):
        if message:
            if isinstance(value, list):
                value = value[0] if len(value) == 1 else None
            if (value is None or type(getattr(value, 'id', None)) is not int or value.id <= 0 or
                    str(getattr(value, 'chat_id', None)) != str(chat) or
                    (message_id is not None and value.id != message_id)):
                return {'outcome': 'unknown', 'error': 'provider_reference_mismatch'}
            return {'outcome': 'confirmed', 'basis': 'provider_message',
                    'provider_message_id': str(value.id), 'provider_chat_id': str(chat)}
        if value is False:
            return {'outcome': 'unknown', 'error': 'provider_result_unverified'}
        return {'outcome': 'submitted', 'basis': 'provider_request_returned',
                'effect_verified': False}

    def accepted_updates(value, chat, random_id):
        if isinstance(value, types.UpdateShortSentMessage) and value.out is True and safe_int(value.id):
            return {'outcome': 'confirmed', 'provider_message_id': str(value.id),
                    'provider_chat_id': str(chat), 'basis': 'provider_short_sent_request_target'}
        updates = getattr(value, 'updates', [])
        mapped = {u.id for u in updates if isinstance(u, types.UpdateMessageID) and u.random_id == random_id}
        for update in updates[:100]:
            if not isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
                continue
            candidate = update.message
            if (candidate.id in mapped and getattr(candidate, 'out', None) is True and
                    peer_id(getattr(candidate, 'peer_id', None)) == str(chat)):
                return accepted(candidate, message=True, chat=chat)
        return accepted(value)

    try:
        # Validate every parameter before the first provider read/mutation.
        text_keys = {'caption': (1024, True), 'question': (300, False),
                     'first_name': (64, False), 'last_name': (64, True),
                     'text': (4000, False), 'title': (128, False),
                     'description': (255, True), 'bio': (139, True)}
        for key, (maximum, empty) in text_keys.items():
            if key in parameters:
                bounded_text(parameters[key], maximum, empty=empty)
        if 'phone' in parameters and (not isinstance(parameters['phone'], str) or
                not re.fullmatch(r'\+?[1-9][0-9]{4,19}', parameters['phone'])):
            reject('invalid_parameters')
        if 'user_id' in parameters:
            number_id(parameters['user_id'])
        if 'limit' in parameters:
            integer(parameters['limit'], 1, 100)
        if 'offset' in parameters:
            integer(parameters['offset'], 0, 100000)
        if 'seconds' in parameters and operation != 'set_slow_mode':
            integer(parameters['seconds'], 1, 2**31 - 1)
        if operation == 'set_slow_mode' and (type(parameters.get('seconds')) is not int or
                parameters['seconds'] not in SLOW_MODE_SECONDS):
            reject('invalid_parameters')
        if operation in GROUP_SETTERS and operation != 'set_slow_mode' and type(parameters.get('enabled')) is not bool:
            reject('invalid_parameters')
        if operation in ('approve_join_requests', 'reject_join_requests'):
            users = parameters.get('user_ids')
            if not isinstance(users, list) or not 1 <= len(users) <= 20 or len(set(map(str, users))) != len(users):
                reject('invalid_parameters')
            for value in users:
                if not isinstance(value, str):
                    reject('invalid_parameters')
                number_id(value)
        if 'multiple' in parameters and type(parameters['multiple']) is not bool:
            reject('invalid_parameters')
        if operation == 'send_file':
            if parameters.get('mode', 'document') not in ('document', 'photo', 'video', 'audio', 'voice', 'video_note'):
                reject('invalid_parameters')
            original = job.get('registered_file_path')
            if not isinstance(original, (str, Path)) or not Path(original).is_absolute():
                reject('attachment_unavailable')
            try:
                fd = os.open(original, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                stream = os.fdopen(fd, 'rb')
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    reject('attachment_unavailable')
                if info.st_size == 0 or info.st_size > MAX_ORIGINAL_BYTES:
                    reject('attachment_too_large')
            except OSError:
                reject('attachment_unavailable')
        if operation == 'send_reaction':
            bounded_text(parameters.get('emoji'), 64, empty=True)
            if any(c.isspace() for c in parameters['emoji']):
                reject('invalid_parameters')
        if operation == 'send_poll':
            bounded_text(parameters.get('question'), 255)
            options = parameters.get('options')
            if not isinstance(options, list) or not 2 <= len(options) <= 10:
                reject('invalid_parameters')
            for option in options:
                bounded_text(option, 100)
            if len(set(options)) != len(options):
                reject('invalid_parameters')
        if operation == 'vote_poll':
            choices = parameters.get('option_indices')
            if (not isinstance(choices, list) or len(choices) > 10 or
                    any(type(i) is not int or not 0 <= i < 10 for i in choices) or
                    len(set(choices)) != len(choices)):
                reject('invalid_parameters')
        if operation == 'send_location':
            for key, maximum in [('latitude', 90), ('longitude', 180)]:
                value = parameters.get(key)
                if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > maximum:
                    reject('invalid_parameters')
        if operation == 'create_group':
            users = parameters.get('user_ids')
            if not isinstance(users, list) or not 1 <= len(users) <= 20:
                reject('invalid_parameters')
            ids = [number_id(value) for value in users]
            if len(ids) != len(set(ids)):
                reject('invalid_parameters')
            bounded_text(parameters.get('title'), 128)
        if operation == 'list_topics' and parameters.get('offset', 0) != 0:
            reject('unsupported_operation')

        account_only = {'list_contacts', 'list_blocked', 'get_contact', 'get_profile',
                        'add_contact', 'delete_contact', 'block_contact', 'unblock_contact',
                        'update_profile', 'create_group'}
        chat = None if operation in account_only else number_id(job.get('provider_chat'), marked=True)
        message_operations = {'get_reactions', 'get_message_info', 'edit_message', 'delete_message',
                              'send_reaction', 'vote_poll', 'pin_message', 'unpin_message', 'mark_read'}
        message_id = number_id(job.get('provider_message_id')) if operation in message_operations else None
        if message_id is not None and message_id > 2**31 - 1:
            reject('invalid_parameters')
        if operation == 'forward_message':
            source_chat = number_id(job.get('source_provider_chat'), marked=True)
            source_message = number_id(job.get('source_provider_message'))
            if source_message > 2**31 - 1:
                reject('invalid_parameters')
            referenced_peers.add(str(source_chat))
        if chat is not None:
            referenced_peers.add(str(chat))
        if 'user_id' in parameters and operation != 'get_contact':
            referenced_peers.add(str(number_id(parameters['user_id'])))
        if operation == 'create_group':
            referenced_peers.update(str(user) for user in ids)
        check_policy()
        peer = await input_peer(chat) if chat is not None else None
        channel = utils.get_input_channel(peer) if isinstance(peer, types.InputPeerChannel) else None
        basic_chat = peer.chat_id if isinstance(peer, types.InputPeerChat) else None

        if operation in ('chat_info', 'get_invite_link'):
            if channel:
                full = await invoke(functions.channels.GetFullChannelRequest(channel))
            elif basic_chat:
                full = await invoke(functions.messages.GetFullChatRequest(basic_chat))
            elif operation == 'chat_info':
                full = await invoke(functions.users.GetFullUserRequest(utils.get_input_user(peer)))
            else:
                reject('unsupported_operation')
            details = getattr(full, 'full_chat', None) or getattr(full, 'full_user', None)
            if operation == 'get_invite_link':
                invite = getattr(details, 'exported_invite', None)
                return {'outcome': 'confirmed', 'link': safe_text(getattr(invite, 'link', None), 512),
                        'existing_only': True, 'revoked': getattr(invite, 'revoked', None)}
            entities = getattr(full, 'chats', None) or getattr(full, 'users', None) or []
            observed = next((e for e in entities if peer_id(e) == str(chat)), None)
            settings = group_settings(observed, details) if (channel or basic_chat) and observed is not None else None
            return {'outcome': 'confirmed', 'provider_chat_id': str(chat),
                    'title': safe_text(getattr(observed, 'title', None), 512),
                    'username': safe_text(getattr(observed, 'username', None), 128),
                    'description': safe_text(getattr(details, 'about', None), 2048),
                    'participant_count': safe_int(getattr(details, 'participants_count', None)),
                    'is_forum': bool(getattr(observed, 'forum', False)),
                    'group_settings': settings, 'basis': 'live_provider_observation'}

        if operation == 'list_participants':
            limit, offset = min(parameters.get('limit', 50), MAX_PROFILE_PAGE), parameters.get('offset', 0)
            if channel:
                response = await invoke(functions.channels.GetParticipantsRequest(channel,
                    types.ChannelParticipantsSearch(''), offset, limit, 0))
                members = [p.user_id for p in getattr(response, 'participants', [])[:limit]
                           if type(getattr(p, 'user_id', None)) is int]
                profiles = {u.id: profile(u) for u in getattr(response, 'users', []) if isinstance(u, types.User)}
                users = [profiles.get(member) for member in members]
                users = [u for u in users if u is not None]
                total = safe_int(getattr(response, 'count', None))
            elif basic_chat:
                response = await invoke(functions.messages.GetFullChatRequest(basic_chat))
                participants = getattr(getattr(response, 'full_chat', None), 'participants', None)
                if not isinstance(participants, types.ChatParticipants):
                    return {'outcome': 'confirmed', 'participants': [], 'coverage': 'unavailable',
                            'next_offset': None}
                member_ids = {p.user_id for p in participants.participants}
                candidates = [profile(u) for u in getattr(response, 'users', [])
                              if isinstance(u, types.User) and u.id in member_ids]
                total = len(candidates)
                users = candidates[offset:offset + limit]
            else:
                reject('unsupported_operation')
            return {'outcome': 'confirmed', 'participants': users, 'reported_count': total,
                    'next_offset': offset + len(users) if len(users) == limit else None,
                    'page_limit': limit,
                    'coverage': 'provider_page_not_complete_membership'}

        if operation == 'get_message_info':
            return {'outcome': 'confirmed', **message_info(await exact_message(peer, chat, message_id))}

        if operation == 'get_reactions':
            response = await invoke(functions.messages.GetMessagesReactionsRequest(peer, [message_id]))
            for update in getattr(response, 'updates', [])[:100]:
                if isinstance(update, types.UpdateMessageReactions) and update.msg_id == message_id and peer_id(update.peer) == str(chat):
                    return {'outcome': 'confirmed', 'provider_message_id': str(message_id),
                            'reactions': reaction_state(update.reactions)}
            # An absent Update is not proof of empty reactions.
            return {'outcome': 'confirmed', 'provider_message_id': str(message_id),
                    'reactions': reaction_state(None)}

        if operation == 'list_topics':
            if channel is None:
                reject('unsupported_operation')
            limit = min(parameters.get('limit', 50), MAX_PROFILE_PAGE)
            response = await invoke(functions.messages.GetForumTopicsRequest(peer, None,
                0, 0, limit, q=None))
            topics = [{'id': str(t.id), 'title': safe_text(t.title, 128), 'closed': bool(t.closed),
                       'hidden': bool(t.hidden), 'pinned': bool(t.pinned),
                       'top_message_id': str(t.top_message)}
                      for t in getattr(response, 'topics', [])[:limit]
                      if isinstance(t, types.ForumTopic)]
            return {'outcome': 'confirmed', 'topics': topics,
                    'reported_count': safe_int(getattr(response, 'count', None)),
                    'next_offset': None, 'pagination_supported': False, 'page_limit': limit,
                    'coverage': 'first_provider_page'}

        if operation in ('get_profile', 'get_contact'):
            if operation == 'get_contact':
                user_id = number_id(parameters.get('user_id'))
                if not allowed_provider_id(user_id):
                    return {'outcome': 'confirmed', 'contact': None, 'found': False}
                response = await invoke(functions.contacts.GetContactsRequest(0))
                contact_ids = {u.user_id for u in getattr(response, 'contacts', [])}
                user = next((u for u in getattr(response, 'users', [])
                             if isinstance(u, types.User) and u.id == user_id and u.id in contact_ids), None)
                return {'outcome': 'confirmed', 'contact': profile(user), 'found': user is not None}
            if not isinstance(verified_self, types.User) or verified_self.is_self is not True:
                reject('provider_identity_unverified')
            user_id = verified_self.id
            target = types.InputUserSelf()
            user = await rpc(lambda: client.get_entity(target))
            response = await invoke(functions.users.GetFullUserRequest(target))
            result = profile(user)
            if result is None or result['id'] != str(user_id):
                reject('provider_reference_mismatch')
            result['bio'] = safe_text(getattr(getattr(response, 'full_user', None), 'about', None), 2048)
            return {'outcome': 'confirmed', 'profile': result}

        if operation in ('list_contacts', 'list_blocked'):
            limit, offset = min(parameters.get('limit', 50), MAX_PROFILE_PAGE), parameters.get('offset', 0)
            if operation == 'list_contacts':
                response = await invoke(functions.contacts.GetContactsRequest(0))
                contact_ids = {u.user_id for u in getattr(response, 'contacts', [])}
                candidates = [profile(u) for u in getattr(response, 'users', [])
                              if isinstance(u, types.User) and u.id in contact_ids and allowed_provider_id(u.id)]
                return {'outcome': 'confirmed', 'contacts': candidates[offset:offset + limit],
                        'reported_count': len(candidates), 'page_limit': limit,
                        'next_offset': offset + limit if offset + limit < len(candidates) else None}
            response = await invoke(functions.contacts.GetBlockedRequest(offset, limit))
            entries = [{'peer_id': peer_id(item.peer_id),
                        'provider_at': item.date.isoformat() if isinstance(item.date, datetime) else None}
                       for item in getattr(response, 'blocked', [])[:limit]
                       if isinstance(item, types.PeerBlocked) and allowed_provider_id(peer_id(item.peer_id))]
            raw_count = min(len(getattr(response, 'blocked', [])), limit)
            return {'outcome': 'confirmed', 'blocked': entries,
                    'reported_count': None if collection_policy()['exclude_chats'] or
                        not collection_policy()['discover_chats'] else safe_int(getattr(response, 'count', None)),
                    'page_limit': limit, 'coverage': 'filtered_provider_page',
                    'next_offset': offset + raw_count if raw_count == limit else None}

        if operation == 'send_file':
            mode = parameters.get('mode', 'document')
            name = job.get('file_name', 'attachment.bin')
            mime = job.get('file_mime', 'application/octet-stream')
            if (not isinstance(name, str) or not 1 <= len(name) <= 255 or
                    any(ord(c) < 32 or c in '/\\' for c in name) or
                    not isinstance(mime, str) or not re.fullmatch(r'[\w.+-]+/[\w.+-]+', mime)):
                reject('attachment_unavailable')
            if mode == 'photo' and (mime not in ('image/jpeg', 'image/png', 'image/webp') or info.st_size > 10 * 1024**2):
                reject('unsupported_operation')
            if mode in ('video', 'video_note') and not mime.startswith('video/'):
                reject('invalid_parameters')
            if mode in ('audio', 'voice') and not mime.startswith('audio/'):
                reject('invalid_parameters')
            expected_hash = job.get('file_digest', parameters.get('sha256'))
            if not isinstance(expected_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_hash):
                reject('attachment_unavailable')
            sha = hashlib.sha256()
            remaining = info.st_size
            while remaining:
                chunk = stream.read(min(512 * 1024, remaining))
                if not chunk:
                    reject('attachment_unavailable')
                sha.update(chunk)
                remaining -= len(chunk)
            after_hash = os.fstat(stream.fileno())
            if (stream.read(1) or sha.hexdigest() != expected_hash or
                    (after_hash.st_size, after_hash.st_mtime_ns) != (info.st_size, info.st_mtime_ns)):
                reject('attachment_unavailable')
            stream.seek(0)
            # Upload chunk requests are paced independently. Final SendMedia is a
            # separate mutation, so expiry is checked again after all uploads.
            upload_id = secrets.randbits(63)
            part_size = 512 * 1024
            parts = (info.st_size + part_size - 1) // part_size
            large = info.st_size > 10 * 1024**2
            md5 = hashlib.md5(usedforsecurity=False)
            sha = hashlib.sha256()
            for index in range(parts):
                chunk = stream.read(part_size)
                expected_size = min(part_size, info.st_size - index * part_size)
                if len(chunk) != expected_size:
                    reject('attachment_unavailable')
                md5.update(chunk)
                sha.update(chunk)
                request = functions.upload.SaveBigFilePartRequest(upload_id, index, parts, chunk) if large else \
                    functions.upload.SaveFilePartRequest(upload_id, index, chunk)
                if await invoke(request, mutation=True) is not True:
                    return {'outcome': 'unknown', 'error': 'provider_upload_unverified'}
            if stream.read(1) or sha.hexdigest() != expected_hash:
                return {'outcome': 'unknown', 'error': 'attachment_changed_during_upload'}
            uploaded = types.InputFileBig(upload_id, parts, name) if large else \
                types.InputFile(upload_id, parts, name, md5.hexdigest())
            if mode == 'photo':
                media = types.InputMediaUploadedPhoto(uploaded)
            else:
                attributes = [types.DocumentAttributeFilename(name)]
                if mode in ('audio', 'voice'):
                    attributes.append(types.DocumentAttributeAudio(0, voice=mode == 'voice'))
                elif mode in ('video', 'video_note'):
                    attributes.append(types.DocumentAttributeVideo(0, 1, 1,
                        round_message=mode == 'video_note', supports_streaming=mode == 'video'))
                media = types.InputMediaUploadedDocument(uploaded, mime, attributes, force_file=mode == 'document')
            random_id = secrets.randbits(63)
            response = await invoke(functions.messages.SendMediaRequest(peer, media,
                parameters.get('caption', ''), random_id=random_id, clear_draft=False, entities=[]), mutation=True)
            return accepted_updates(response, chat, random_id)

        if operation == 'forward_message':
            source = await input_peer(source_chat)
            await exact_message(source, source_chat, source_message)
            value = await rpc(lambda: client.forward_messages(peer, [source_message], from_peer=source,
                drop_author=False, drop_media_captions=False), mutation=True)
            return accepted(value, message=True, chat=chat)

        if operation in ('send_poll', 'send_contact', 'send_location'):
            if operation == 'send_poll':
                answers = [types.PollAnswer(types.TextWithEntities(value, []), index.to_bytes(1, 'big'))
                           for index, value in enumerate(parameters['options'])]
                media = types.InputMediaPoll(types.Poll(0,
                    types.TextWithEntities(parameters['question'], []), answers, hash=0,
                    multiple_choice=parameters.get('multiple', False), public_voters=False))
            elif operation == 'send_contact':
                if 'phone' not in parameters or 'first_name' not in parameters:
                    reject('invalid_parameters')
                media = types.InputMediaContact(parameters['phone'], parameters['first_name'],
                    parameters.get('last_name', ''), '')
            else:
                media = types.InputMediaGeoPoint(types.InputGeoPoint(parameters['latitude'], parameters['longitude']))
            random_id = secrets.randbits(63)
            response = await invoke(functions.messages.SendMediaRequest(peer, media,
                parameters.get('description', '') if operation == 'send_location' else '',
                random_id=random_id, clear_draft=False, entities=[]), mutation=True)
            return accepted_updates(response, chat, random_id)

        if operation in ('edit_message', 'delete_message'):
            await exact_message(peer, chat, message_id, own=True)
            if operation == 'edit_message':
                bounded_text(parameters.get('text'), 4000)
                response = await rpc(lambda: client.edit_message(peer, message_id, parameters['text'],
                    parse_mode=None, link_preview=False), mutation=True)
                if getattr(response, 'message', None) != parameters['text'] or getattr(response, 'out', None) is not True:
                    return {'outcome': 'unknown', 'error': 'provider_result_unverified'}
                return accepted(response, message=True, chat=chat, message_id=message_id)
            response = await rpc(lambda: client.delete_messages(peer, [message_id], revoke=True), mutation=True)
            return accepted(response)

        if operation == 'send_reaction':
            reactions = [types.ReactionEmoji(parameters['emoji'])] if parameters['emoji'] else []
            return accepted(await invoke(functions.messages.SendReactionRequest(peer, message_id,
                big=False, add_to_recent=False, reaction=reactions), mutation=True))

        if operation == 'vote_poll':
            message = await exact_message(peer, chat, message_id)
            media = getattr(message, 'media', None)
            if not isinstance(media, types.MessageMediaPoll):
                reject('unsupported_operation')
            poll = media.poll
            choices = parameters['option_indices']
            if (poll.closed or any(index >= len(poll.answers) for index in choices) or
                    (not poll.multiple_choice and len(choices) > 1)):
                reject('invalid_parameters')
            if getattr(poll, 'subscribers_only', False) or getattr(poll, 'countries_iso2', None):
                reject('unsupported_operation')
            selected = [poll.answers[index].option for index in choices]
            return accepted(await invoke(functions.messages.SendVoteRequest(peer, message_id, selected), mutation=True))

        if operation in ('pin_message', 'unpin_message'):
            fn = client.pin_message if operation == 'pin_message' else client.unpin_message
            kwargs = {'notify': False} if operation == 'pin_message' else {}
            return accepted(await rpc(lambda: fn(peer, message_id, **kwargs), mutation=True))

        if operation == 'mark_read':
            request = functions.channels.ReadHistoryRequest(channel, message_id) if channel else \
                functions.messages.ReadHistoryRequest(peer, message_id)
            return accepted(await invoke(request, mutation=True))
        if operation == 'mark_unread':
            return accepted(await invoke(functions.messages.MarkDialogUnreadRequest(
                types.InputDialogPeer(peer), unread=True), mutation=True))
        if operation in ('archive_chat', 'unarchive_chat'):
            return accepted(await rpc(lambda: client.edit_folder(peer,
                folder=1 if operation == 'archive_chat' else 0), mutation=True))
        if operation in ('mute_chat', 'unmute_chat'):
            seconds = parameters.get('seconds') if operation == 'mute_chat' else 0
            if operation == 'mute_chat':
                integer(seconds, 1, 2**31 - 1)
            # TL timestamps are signed 32-bit UTC epochs. Large requested durations
            # beyond that bound cannot be represented faithfully.
            mute_until = int(time.time()) + seconds if seconds else 0
            if mute_until > 2**31 - 1:
                reject('invalid_parameters')
            settings = types.InputPeerNotifySettings(mute_until=datetime.fromtimestamp(mute_until, timezone.utc))
            return accepted(await invoke(functions.account.UpdateNotifySettingsRequest(
                types.InputNotifyPeer(peer), settings), mutation=True))

        if operation == 'create_group':
            users = [await user_input(value) for value in ids]
            response = await invoke(functions.messages.CreateChatRequest(users, parameters['title']), mutation=True)
            updates = response.updates if isinstance(response, types.messages.InvitedUsers) else response
            chats = [c for c in getattr(updates, 'chats', []) if isinstance(c, types.Chat)]
            if len(chats) == 1:
                missing = [str(item.user_id) for item in getattr(response, 'missing_invitees', [])[:20]
                           if isinstance(item, types.MissingInvitee) and item.user_id in ids]
                return {'outcome': 'confirmed', 'provider_chat_id': peer_id(chats[0]),
                        'missing_invitee_ids': missing, 'all_members_added': not missing
                            if isinstance(response, types.messages.InvitedUsers) else None,
                        'basis': 'provider_created_chat'}
            return accepted(response)

        if operation in ('rename_group', 'set_group_description', 'add_participant', 'remove_participant',
                         'promote_participant', 'demote_participant', 'leave_group'):
            if channel is None and basic_chat is None:
                reject('unsupported_operation')
            if operation == 'rename_group':
                bounded_text(parameters.get('title'), 128)
                request = functions.channels.EditTitleRequest(channel, parameters['title']) if channel else \
                    functions.messages.EditChatTitleRequest(basic_chat, parameters['title'])
            elif operation == 'set_group_description':
                bounded_text(parameters.get('description'), 255, empty=True)
                request = functions.messages.EditChatAboutRequest(peer, parameters['description'])
            elif operation == 'leave_group':
                if channel:
                    request = functions.channels.LeaveChannelRequest(channel)
                else:
                    request = functions.messages.DeleteChatUserRequest(basic_chat, types.InputUserSelf(), revoke_history=False)
            else:
                user_id = number_id(parameters.get('user_id'))
                if isinstance(verified_self, types.User) and user_id == verified_self.id:
                    reject('invalid_parameters')
                if operation == 'promote_participant' and channel is None:
                    reject('unsupported_operation')
                user = await user_input(user_id)
                if operation == 'add_participant':
                    request = functions.channels.InviteToChannelRequest(channel, [user]) if channel else \
                        functions.messages.AddChatUserRequest(basic_chat, user, fwd_limit=0)
                elif operation == 'remove_participant':
                    if channel:
                        # Basic kick/unban can have partial effects. Do not silently
                        # turn a remove operation into a persistent ban.
                        reject('unsupported_operation')
                    request = functions.messages.DeleteChatUserRequest(basic_chat, user, revoke_history=False)
                elif channel:
                    rights = types.ChatAdminRights(change_info=operation == 'promote_participant',
                        invite_users=operation == 'promote_participant', pin_messages=operation == 'promote_participant',
                        post_messages=False, edit_messages=False, delete_messages=False, ban_users=False,
                        add_admins=False, anonymous=False, manage_call=False, other=False)
                    request = functions.channels.EditAdminRequest(channel, user, rights, rank='')
                else:
                    request = functions.messages.EditChatAdminRequest(basic_chat, user, is_admin=False)
            return accepted(await invoke(request, mutation=True))

        if operation == 'revoke_invite_link':
            if channel is None and basic_chat is None:
                reject('unsupported_operation')
            response = await invoke(functions.channels.GetFullChannelRequest(channel) if channel else
                functions.messages.GetFullChatRequest(basic_chat))
            invite = getattr(getattr(response, 'full_chat', None), 'exported_invite', None)
            link = safe_text(getattr(invite, 'link', None), 512)
            if not isinstance(link, str) or not re.fullmatch(r'https://t\.me/(?:\+|joinchat/)[A-Za-z0-9_-]+', link):
                reject('invite_link_unavailable')
            return accepted(await invoke(functions.messages.EditExportedChatInviteRequest(peer,
                link, revoked=True), mutation=True))

        if operation in GROUP_SETTERS or operation in ('list_join_requests', 'approve_join_requests', 'reject_join_requests'):
            if channel is None and basic_chat is None:
                reject('unsupported_operation')

        async def group_state():
            full = await invoke(functions.channels.GetFullChannelRequest(channel) if channel else
                functions.messages.GetFullChatRequest(basic_chat))
            entity = next((e for e in getattr(full, 'chats', None) or [] if peer_id(e) == str(chat)), None)
            if entity is None:
                reject('provider_reference_mismatch')
            return entity, group_settings(entity, getattr(full, 'full_chat', None))

        if operation in GROUP_SETTERS:
            field, key, flag = GROUP_SETTERS[operation]
            value = parameters[key]
            entity, state = await group_state()
            if (isinstance(entity, types.Channel) and not entity.megagroup) or (flag is None and channel is None):
                reject('unsupported_operation')
            # Both default rights and slow mode need the ban right; refuse before any mutation.
            if not group_admin(entity, 'ban_users'):
                reject('forbidden')
            previous = state[field]
            if previous is None:
                reject('unsupported_operation')
            if previous == value:
                return {'outcome': 'confirmed', key: value, 'previous': previous, 'changed': False}
            if flag is None:
                request = functions.channels.ToggleSlowModeRequest(channel, value)
            else:
                # The freshly read rights with only the owned flag changed; until_date and the rest stay.
                read = entity.default_banned_rights
                rights = types.ChatBannedRights(**{name: getattr(read, name) for name in BANNED_RIGHTS_FIELDS})
                setattr(rights, flag, value)
                request = functions.messages.EditChatDefaultBannedRightsRequest(peer, rights)
            await invoke(request, mutation=True)
            _, state = await group_state()
            if state[field] == value:
                return {'outcome': 'confirmed', key: value, 'previous': previous, 'changed': True}
            return {'outcome': 'submitted', key: value, 'previous': previous, 'changed': True,
                    'basis': 'provider_request_returned', 'effect_verified': False}

        if operation == 'list_join_requests':
            limit = parameters.get('limit', 50)
            response = await invoke(functions.messages.GetChatInviteImportersRequest(peer, offset_date=None,
                offset_user=types.InputUserEmpty(), limit=limit, requested=True))
            users = {u.id: u for u in getattr(response, 'users', None) or [] if isinstance(u, types.User)}
            importers = [i for i in getattr(response, 'importers', None) or []
                         if isinstance(i, types.ChatInviteImporter) and safe_int(i.user_id)][:limit]
            rows, size = [], 0
            for item in importers:
                user = users.get(item.user_id)
                name = ' '.join(filter(None, (safe_text(getattr(user, 'first_name', None), 64),
                                              safe_text(getattr(user, 'last_name', None), 64)))) or None
                row = {'id': str(item.user_id), 'name': name, 'note': utf16_text(item.about, 200),
                       'requested_at': int(item.date.timestamp()) if isinstance(item.date, datetime) else None}
                # Bounded so the receipt stays under the result budget; the rest is has_more.
                size += len(json.dumps(row, ensure_ascii=False).encode())
                if size > JOIN_REQUESTS_BYTES:
                    break
                rows.append(row)
            count = safe_int(getattr(response, 'count', None))
            return {'outcome': 'confirmed', 'requests': rows, 'untrusted': ['name', 'note'],
                    'has_more': len(rows) < len(importers) or (count is not None and count > len(rows))}

        if operation in ('approve_join_requests', 'reject_join_requests'):
            from .commands import CommandRejected
            approved, ids, rows = operation == 'approve_join_requests', parameters['user_ids'], []
            for index, value in enumerate(ids):
                attempted = False

                async def hide():
                    nonlocal attempted
                    attempted = True
                    return await client(functions.messages.HideChatJoinRequestRequest(peer, user, approved=approved))
                try:
                    # Each rpc rechecks expiry, access and the trusted preflight before its call.
                    user = await user_input(value)
                    await rpc(hide, mutation=True)
                    rows.append({'id': value, 'state': 'applied'})
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    code = JOIN_REFUSAL_ERRORS.get(type(error))
                    if attempted and code:
                        rows.append({'id': value, 'state': 'refused', 'reason': JOIN_REFUSALS[code],
                                     'provider_code': code[:64]})
                        continue
                    if not rows and not attempted:
                        if isinstance(error, CommandRejected):
                            raise
                        reject('provider_preflight_failed')
                rows.append({'id': value, 'state': 'unknown' if attempted else 'not_attempted'})
                rows.extend({'id': later, 'state': 'not_attempted'} for later in ids[index + 1:])
                break
            settled = all(row['state'] in ('applied', 'refused') for row in rows)
            return {'outcome': 'confirmed' if settled else 'unknown', 'results': rows,
                    'all_applied': all(row['state'] == 'applied' for row in rows)}

        if operation == 'add_contact':
            if 'phone' not in parameters or 'first_name' not in parameters:
                reject('invalid_parameters')
            contact = types.InputPhoneContact(secrets.randbits(63), parameters['phone'],
                parameters['first_name'], parameters.get('last_name', ''))
            response = await invoke(functions.contacts.ImportContactsRequest([contact]), mutation=True)
            imported = [p for p in getattr(response, 'imported', []) if getattr(p, 'client_id', None) == contact.client_id]
            if len(imported) == 1 and type(imported[0].user_id) is int:
                return {'outcome': 'confirmed', 'user_id': str(imported[0].user_id), 'basis': 'provider_imported_contact'}
            return accepted(response)
        if operation in ('delete_contact', 'block_contact', 'unblock_contact'):
            user_id = number_id(parameters.get('user_id'))
            if isinstance(verified_self, types.User) and user_id == verified_self.id:
                reject('invalid_parameters')
            target = await input_peer(user_id)
            if not isinstance(target, types.InputPeerUser):
                reject('invalid_parameters')
            if operation == 'delete_contact':
                request = functions.contacts.DeleteContactsRequest([utils.get_input_user(target)])
            else:
                fn = functions.contacts.BlockRequest if operation == 'block_contact' else functions.contacts.UnblockRequest
                request = fn(target, my_stories_from=False)
            return accepted(await invoke(request, mutation=True))
        if operation == 'update_profile':
            if not parameters:
                reject('invalid_parameters')
            response = await invoke(functions.account.UpdateProfileRequest(
                first_name=parameters.get('first_name'), last_name=parameters.get('last_name'),
                about=parameters.get('bio')), mutation=True)
            if isinstance(verified_self, types.User) and isinstance(response, types.User) and response.id == verified_self.id:
                return {'outcome': 'confirmed', 'profile': profile(response), 'basis': 'provider_profile'}
            return accepted(response)
        reject('unsupported_operation')
    except asyncio.CancelledError:
        raise
    except Exception as error:
        from .commands import CommandRejected
        if isinstance(error, CommandRejected):
            if started_mutation:
                return {'outcome': 'unknown', 'error': 'provider_result_unverified'}
            raise
        # Do not expose provider exception strings, session paths or invite links.
        if started_mutation:
            return {'outcome': 'unknown', 'error': 'provider_result_unverified'}
        reject('provider_preflight_failed')
    finally:
        if stream is not None:
            stream.close()


async def execute(client, job, *, verified_self=None, governor=None):
    """Return a bounded backend result, never native objects or transport secrets."""
    result = await _execute(client, job, verified_self=verified_self, governor=governor)
    outcome = result.pop('outcome')
    output = {'outcome': outcome, 'data': result}
    if 'provider_message_id' in result:
        output['provider_message_id'] = result['provider_message_id']
    return output
