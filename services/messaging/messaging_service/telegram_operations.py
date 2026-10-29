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
import logging
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time

from telethon import errors, functions, types, utils

from .envelope import canonical, stable_id
from .messenger_operations import HISTORY, LIVE_SEARCH, live_cursor_binding

log = logging.getLogger('anymsg.live_search')
history_log = logging.getLogger('anymsg.chat_history')


MAX_ORIGINAL_BYTES = 32 * 1024 * 1024
MAX_PROFILE_PAGE = 50
READ_OPERATIONS = frozenset({
    'chat_info', 'list_participants', 'get_reactions', 'get_message_info',
    'list_topics', 'get_profile', 'list_contacts', 'get_contact', 'list_blocked',
    'get_invite_link', 'check_chat_link', LIVE_SEARCH, HISTORY,
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
    'check_chat_link': {'link'}, 'join_chat': {'link'},
    'open_chat_by_username': {'username', 'create'},
    LIVE_SEARCH: {'query', 'chat_types', 'filter', 'from_time', 'to_time', 'limit', 'cursor'},
    HISTORY: {'count', 'before_message', 'around_message', 'from_time', 'to_time'},
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
# No batch mutation starts later than this after the adapter was entered. The worker
# cancels at 30 s (commands.py). The check runs after the governor wait and the preflight,
# right before the call, so only the last request's round trip (a stall stays the
# documented unknown) and the receipt write follow it: 22 s leaves 8 s for both. An idle
# 20-ID batch of cached requesters (group lookup + 20 mutations, 1 s apart) starts its
# last mutation at 20 s, so it still completes.
JOIN_BATCH_BUDGET_SECONDS = 22.0


# ANYM-243: the normalized link key (messenger_operations.chat_link_key), and the definite refusals of
# a join: the provider applied nothing, so they fail the receipt; any other error after the call is unknown.
CHAT_LINK = re.compile(r'\+[A-Za-z0-9_-]{8,64}|@[a-z][a-z0-9_]{4,31}', re.ASCII)
JOIN_CHAT_REFUSALS = ((errors.InviteHashExpiredError, 'invite_expired'), (errors.InviteHashInvalidError, 'invite_invalid'),
    (errors.InviteHashEmptyError, 'invite_invalid'), (errors.UsersTooMuchError, 'chat_full'),
    (errors.ChannelsTooMuchError, 'join_not_allowed'), (errors.ChannelPrivateError, 'join_not_allowed'),
    (errors.UserAlreadyParticipantError, 'already_member'))
JOIN_NEXT_STEP = "the operator adds this chat to the account's collection policy"
# ANYM-643: the normalized username (messenger_operations.telegram_username).
USERNAME = re.compile(r'[a-z][a-z0-9_]{3,31}', re.ASCII)


def link_chat(chat, membership, basis):
    """The check projection of a chat: never its ID, members, inviter or description."""
    approval = getattr(chat, 'join_request', None) if isinstance(chat, types.Channel) else None
    return {'title': safe_text(getattr(chat, 'title', None), 255),
            'kind': 'channel' if isinstance(chat, types.Channel) and chat.broadcast else 'group',
            'member_count': safe_int(getattr(chat, 'participants_count', None)),
            'approval_required': approval if approval is None else bool(approval), 'paid': False,
            'membership': membership, 'membership_basis': basis}


class JoinBatchBudgetSpent(Exception):
    """The batch budget ran out before this mutation started; nothing was sent."""


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
    # The governor's clock paces the calls, so the batch budget reads the same clock.
    clock = getattr(governor, 'clock', time.monotonic)
    entered = clock()
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

    async def rpc(fn, *, mutation=False, budget=False, scope=None):
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
            if budget and clock() - entered >= JOIN_BATCH_BUDGET_SECONDS:
                raise JoinBatchBudgetSpent()
            if mutation:
                started_mutation = True
                if report is not None:
                    report('started')
            return await fn()
        if governor is None:
            return await guarded()
        if scope is not None:
            return await governor.scoped_call(guarded, 'provider_command', scope)
        return await governor.call(guarded, 'provider_command')

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
        if operation in ('check_chat_link', 'join_chat') and (not isinstance(parameters.get('link'), str) or
                not CHAT_LINK.fullmatch(parameters['link'])):
            reject('invalid_parameters')

        if operation == 'open_chat_by_username':
            return await open_by_username(client, job, parameters, rpc, collection_policy, allowed_provider_id)
        if operation == LIVE_SEARCH:
            return await live_search(client, job, parameters, governor, rpc, allowed_provider_id,
                                     referenced_peers, check_policy)
        if operation == HISTORY:
            return await chat_history(client, job, parameters, governor, rpc, referenced_peers, check_policy)

        account_only = {'list_contacts', 'list_blocked', 'get_contact', 'get_profile',
                        'add_contact', 'delete_contact', 'block_contact', 'unblock_contact',
                        'update_profile', 'create_group', 'check_chat_link', 'join_chat'}
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

        if operation in ('check_chat_link', 'join_chat'):
            link = parameters['link']
            if link[0] == '+':
                try:
                    invite = await invoke(functions.messages.CheckChatInviteRequest(link[1:]))
                except errors.InviteHashExpiredError:
                    reject('invite_expired')
                except (errors.InviteHashInvalidError, errors.InviteHashEmptyError):
                    reject('invite_invalid')
                if isinstance(invite, types.ChatInvite):
                    target = None
                    observed = {'title': safe_text(invite.title, 255),
                                'kind': 'channel' if invite.broadcast else 'group',
                                'member_count': safe_int(invite.participants_count),
                                'approval_required': bool(invite.request_needed),
                                'paid': invite.subscription_pricing is not None,
                                'membership': 'not_member', 'membership_basis': 'provider_invite'}
                elif (isinstance(invite, (types.ChatInviteAlready, types.ChatInvitePeek)) and
                        isinstance(invite.chat, (types.Chat, types.Channel))):
                    target = invite.chat
                    observed = link_chat(target, 'member' if isinstance(invite, types.ChatInviteAlready) else 'not_member',
                                         'provider_invite')
                else:
                    reject('provider_reference_mismatch')
            else:
                try:
                    target = await rpc(lambda: client.get_entity(link[1:]))
                except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError):
                    reject('invite_invalid')
                if not isinstance(target, types.Channel):
                    reject('invite_invalid')  # A user or bot name is not a chat link.
                observed = link_chat(target, {True: 'not_member', False: 'member'}.get(target.left, 'unknown'),
                                     'provider_public_chat')
            if operation == 'check_chat_link':
                return {'outcome': 'confirmed', **observed}
            # The digest the owner confirmed names this chat; the link must still lead to it.
            expected = job.get('expected_chat')
            if not isinstance(expected, dict) or (observed['title'], observed['kind']) != (
                    expected.get('title'), expected.get('kind')):
                reject('operation_target_changed')
            if observed['membership'] == 'member':
                reject('already_member')
            if observed['paid']:
                reject('join_not_allowed')
            if observed['membership'] != 'not_member':
                reject('operation_target_changed')
            request = (functions.messages.ImportChatInviteRequest(link[1:]) if link[0] == '+' else
                       functions.channels.JoinChannelRequest(utils.get_input_channel(target)))
            try:
                response = await invoke(request, mutation=True)
            except errors.InviteRequestSentError:
                return {'outcome': 'confirmed', 'state': 'pending_approval', 'basis': 'provider_join_request_sent'}
            except tuple(kind for kind, _ in JOIN_CHAT_REFUSALS) as error:
                started_mutation = False  # A definite refusal: the provider joined nothing.
                reject(next(code for kind, code in JOIN_CHAT_REFUSALS if isinstance(error, kind)))
            joined = [c for c in getattr(response, 'chats', [])[:100] if isinstance(c, (types.Chat, types.Channel)) and
                      (target is None or utils.get_peer_id(c) == utils.get_peer_id(target))]
            if len(joined) != 1 or peer_id(joined[0]) is None:
                return {'outcome': 'unknown', 'error': 'provider_result_unverified'}
            provider = peer_id(joined[0])
            if allowed_provider_id(provider):
                return {'outcome': 'confirmed', 'state': 'joined', 'archive': 'collected', 'provider_chat_id': provider,
                        'basis': 'provider_joined_chat'}
            return {'outcome': 'confirmed', 'state': 'joined', 'archive': 'not_collected', 'next_step': JOIN_NEXT_STEP,
                    'basis': 'provider_joined_chat'}

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
            # The account's own role, by the catalog's rules, from the entity this request returned (ANYM-533).
            from .chat_roles import derive
            my_role, can_post = derive(observed, 'private' if not (channel or basic_chat) else
                                       'group' if basic_chat or getattr(observed, 'megagroup', False) else 'channel')
            return {'outcome': 'confirmed', 'provider_chat_id': str(chat), 'my_role': my_role, 'can_post': can_post,
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
            approved, ids, rows = operation == 'approve_join_requests', parameters['user_ids'], []

            async def requester(value):
                # A requester cached in the session file (list_join_requests stores it) resolves
                # locally without a paced call, so 20 IDs fit the worker's 30 s; only an uncached
                # one costs a paced lookup.
                uid = number_id(value)
                try:
                    cached = await utils.maybe_async(client.session.get_input_entity(types.PeerUser(uid)))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    cached = None
                if isinstance(cached, types.InputPeerUser) and cached.user_id == uid:
                    return utils.get_input_user(cached)
                return await user_input(value)
            for index, value in enumerate(ids):
                attempted = False
                if clock() - entered >= JOIN_BATCH_BUDGET_SECONDS:
                    rows.extend({'id': later, 'state': 'not_attempted'} for later in ids[index:])
                    break

                async def hide():
                    nonlocal attempted
                    attempted = True
                    return await client(functions.messages.HideChatJoinRequestRequest(peer, user, approved=approved))
                try:
                    # The mutation's rpc rechecks expiry, access and the trusted preflight right before it.
                    user = await requester(value)
                    # A spent budget raises JoinBatchBudgetSpent: this ID and the rest are not_attempted.
                    await rpc(hide, mutation=True, budget=True)
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


LIVE_FILTERS = {'documents': types.InputMessagesFilterDocument, 'photos': types.InputMessagesFilterPhotos,
                'links': types.InputMessagesFilterUrl, 'voice': types.InputMessagesFilterVoice}
LIVE_FLAGS = {'private': 'users_only', 'group': 'groups_only', 'channel': 'broadcasts_only'}
LIVE_TEXT = 300
# Issued cursors: random token -> (binding, position). In memory and bounded: the position may
# name a message or chat the policy excludes, so an agent sees only the token.
LIVE_CURSORS = {}
LIVE_CURSORS_MAX = 1024
# Leaves room for the archive fields ANYM-476 adds to each hit after the request (about 60 bytes x 50).
LIVE_RECEIPT_BYTES = 52000


def chat_kind(entity):
    if isinstance(entity, types.User):
        return 'private'
    if isinstance(entity, (types.Chat, types.ChatForbidden)):
        return 'group'
    if isinstance(entity, (types.Channel, types.ChannelForbidden)):
        return 'group' if getattr(entity, 'megagroup', False) or getattr(entity, 'gigagroup', False) else 'channel'
    return None


def entity_name(entity):
    if isinstance(entity, types.User):
        return safe_text(' '.join(p for p in (entity.first_name, entity.last_name) if p) or entity.username, 128)
    return safe_text(getattr(entity, 'title', None), 128)


def cursor_peer(entity):
    """The offset peer of the next searchGlobal page as plain integers, never a TL object."""
    if isinstance(entity, types.User) and entity.access_hash is not None:
        return ['u', entity.id, entity.access_hash]
    if isinstance(entity, types.Chat):
        return ['c', entity.id, 0]
    if isinstance(entity, types.Channel) and entity.access_hash is not None:
        return ['h', entity.id, entity.access_hash]
    return None


def remember_cursor(binding, position):
    token = secrets.token_urlsafe(16)
    LIVE_CURSORS[token] = (binding, position)
    while len(LIVE_CURSORS) > LIVE_CURSORS_MAX:
        LIVE_CURSORS.pop(next(iter(LIVE_CURSORS)))
    return token


def recall_cursor(token, binding):
    """The position of a cursor issued for exactly this binding; unknown (restart) or other: invalid_cursor."""
    entry = LIVE_CURSORS.get(token) if isinstance(token, str) else None
    if entry is None or entry[0] != binding:
        reject('invalid_cursor')
    return dict(entry[1])


def input_offset_peer(value):
    if value is None:
        return types.InputPeerEmpty()
    kind, number, access_hash = value
    return {'u': lambda: types.InputPeerUser(number, access_hash), 'c': lambda: types.InputPeerChat(number),
            'h': lambda: types.InputPeerChannel(number, access_hash)}[kind]()


def utc_seconds(value):
    return datetime.fromisoformat(value).timestamp() if value else None


async def live_search(client, job, parameters, governor, rpc, allowed_provider_id, referenced_peers, check_policy):
    """ANYM-475: one governed Telegram search request; hits the collection policy excludes are dropped.

    The query leaves the process only to Telegram: no log line, telemetry or error
    names it. Only counts, duration and FloodWait seconds are logged."""
    from .commands import CommandRejected
    from .telegram_governor import ProviderRateLimited
    bounded_text(parameters.get('query'), 256)
    limit = integer(parameters.get('limit', 20), 1, 50)
    types_wanted = parameters.get('chat_types')
    if types_wanted is not None and (not isinstance(types_wanted, list) or not types_wanted or
                                     set(types_wanted) - set(LIVE_FLAGS)):
        reject('invalid_parameters')
    if parameters.get('filter') is not None and parameters['filter'] not in LIVE_FILTERS:
        reject('invalid_parameters')
    try:
        since, until = utc_seconds(parameters.get('from_time')), utc_seconds(parameters.get('to_time'))
    except (TypeError, ValueError):
        reject('invalid_parameters')
    account, chat_id = job.get('account'), job.get('chat_id') or ''
    chat = number_id(job.get('provider_chat'), marked=True) if chat_id else None
    binding = live_cursor_binding(account, chat_id, parameters)
    position = recall_cursor(parameters['cursor'], binding) if 'cursor' in parameters else {}
    if chat is not None:
        referenced_peers.add(str(chat))
    else:
        # Without a chat the default leaves channels out, as the archive search does.
        types_wanted = types_wanted or ['private', 'group']
    check_policy()
    started, method = time.monotonic(), 'search' if chat is not None else 'search_global'
    counts = {'returned': 0, 'dropped': 0, 'flood_wait_s': 0}

    def finish(state):
        log.info('live_search method=%s state=%s returned=%d dropped=%d flood_wait_s=%d duration_ms=%d', method,
                 state, counts['returned'], counts['dropped'], counts['flood_wait_s'],
                 int((time.monotonic() - started) * 1000))

    def limited(seconds, state):
        finish(state)
        raise CommandRejected('rate_limited', retry_after=max(1, int(seconds)))

    if governor is not None:
        # Admission before any provider call counts every request, entity resolution included.
        wait = await governor.admit('search', 2 if chat is not None else 1)
        if wait:
            limited(wait, 'refused')
    search_filter = LIVE_FILTERS[parameters['filter']]() if parameters.get('filter') else types.InputMessagesFilterEmpty()
    # Telegram dates are whole seconds: min_date and max_date are both exclusive.
    min_date = math.ceil(since) - 1 if since is not None else None
    max_date = math.ceil(until) if until is not None else None
    try:
        if chat is not None:
            peer = await rpc(lambda: client.get_input_entity(chat), scope='search')
            request = functions.messages.SearchRequest(peer, parameters['query'], search_filter, min_date, max_date,
                position.get('i', 0), 0, limit, 0, 0, 0)
        else:
            flags = {LIVE_FLAGS[types_wanted[0]]: True} if len(types_wanted) == 1 else {}
            request = functions.messages.SearchGlobalRequest(parameters['query'], search_filter, min_date, max_date,
                position.get('r', 0), input_offset_peer(position.get('p')), position.get('i', 0), limit, **flags)
        response = await rpc(lambda: client(request), scope='search')
    except ProviderRateLimited as exc:
        counts['flood_wait_s'] = exc.seconds
        limited(exc.seconds, 'flood_wait')
    except (errors.ChannelPrivateError, errors.ChatAdminRequiredError, errors.PeerIdInvalidError):
        finish('refused')
        reject('not_found')
    except errors.RPCError:
        finish('provider_error')
        reject('operation_rejected')
    entities = {}
    for entity in list(getattr(response, 'chats', None) or []) + list(getattr(response, 'users', None) or []):
        try:
            entities[str(utils.get_peer_id(entity))] = entity
        except (TypeError, ValueError):
            continue
    raw = [m for m in (getattr(response, 'messages', None) or [])[:limit] if isinstance(m, types.Message)]
    hits, kept = [], job.get('_live_hits')
    known = {int(key): entity for key, entity in entities.items()}
    for message in raw:
        try:
            provider_chat = str(utils.get_peer_id(message.peer_id))
        except (TypeError, ValueError):
            counts['dropped'] += 1
            continue
        if not allowed_provider_id(provider_chat) or chat is not None and provider_chat != str(chat):
            counts['dropped'] += 1
            continue
        entity = entities.get(provider_chat)
        kind = chat_kind(entity)
        moment = message.date.timestamp() if message.date else None
        if (types_wanted and kind not in types_wanted or moment is None or since is not None and moment < since or
                until is not None and moment >= until):
            continue
        sender = str(utils.get_peer_id(message.from_id)) if message.from_id else provider_chat if kind == 'private' else None
        hits.append({'chat': stable_id('chat', account, provider_chat), 'chat_title': entity_name(entity),
                     'chat_type': kind, 'provider_id': str(message.id),
                     'time': message.date.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
                     'sender': {'id': sender, 'name': entity_name(entities.get(sender)) if sender else None},
                     'text': safe_text(message.message, LIVE_TEXT) or ''})
        if kept is not None:
            # ANYM-476: the collector archives these after the request; the receipt never carries them.
            # Initialized as iter_messages does for a history page: cached sender and chat, outgoing
            # Saved Messages. It uses only this response's entities, never the network.
            try:
                message._finish_init(client, known, None)
            except Exception:
                message = None
            kept.append((message, hits[-1], kind, entity_name(entity)))
    next_cursor = None
    if len(raw) >= limit and raw and not isinstance(response, types.messages.Messages):
        last = raw[-1]
        place = {'i': last.id}
        if chat is None:
            # Without next_rate Telegram continues from the last returned message's date.
            place.update(r=getattr(response, 'next_rate', None) or (int(last.date.timestamp()) if last.date else 0),
                         p=cursor_peer(entities.get(str(utils.get_peer_id(last.peer_id)))))
        next_cursor = remember_cursor(binding, place)
    total = getattr(response, 'count', None)
    result = {'outcome': 'confirmed', 'hits': hits, 'next_cursor': next_cursor,
              'provider_total_approx': total if type(total) is int else len(raw), 'basis': 'live_provider_search'}
    width = LIVE_TEXT
    while len(canonical(result)) > LIVE_RECEIPT_BYTES and width:
        # Bound the receipt (commands.py safe_result: 60 KB) by shortening every free-text field, never by
        # dropping hits: the fixed fields of 50 hits stay far below the bound, so width 0 always fits.
        width //= 2
        for hit in hits:
            hit['text'] = hit['text'][:width]
            for holder, field in ((hit, 'chat_title'), (hit['sender'], 'name')):
                if holder[field]:
                    holder[field] = holder[field][:width]
    counts['returned'] = len(hits)
    finish('done')
    return result


async def chat_history(client, job, parameters, governor, rpc, referenced_peers, check_policy):
    """ANYM-477: one governed history page of one archived chat at one anchor.

    The backend resolved an explicit anchor to provider_message_id and the collector the default
    one (the oldest archived message; none in an empty chat, which starts from the newest). The
    messages go to the collector (job['_history']) for the archive; the receipt has counts only."""
    from .commands import CommandRejected
    from .telegram_governor import ProviderRateLimited
    count = integer(parameters.get('count', 50), 1, 100)
    around, timed = 'around_message' in parameters, 'from_time' in parameters or 'to_time' in parameters
    if ('before_message' in parameters) + around + timed > 1:
        reject('invalid_parameters')
    try:
        since, until = utc_seconds(parameters.get('from_time')), utc_seconds(parameters.get('to_time'))
    except (TypeError, ValueError):
        reject('invalid_parameters')
    chat = number_id(job.get('provider_chat'), marked=True)
    anchor = 0 if timed or not job.get('provider_message_id') else number_id(job['provider_message_id'])
    if (around or 'before_message' in parameters) and not anchor:
        reject('invalid_parameters')
    referenced_peers.add(str(chat))
    check_policy()
    started, counts = time.monotonic(), {'fetched': 0, 'flood_wait_s': 0}

    def finish(state):
        history_log.info('chat_history state=%s fetched=%d flood_wait_s=%d duration_ms=%d', state, counts['fetched'],
                         counts['flood_wait_s'], int((time.monotonic() - started) * 1000))

    def limited(seconds, state):
        finish(state)
        raise CommandRejected('rate_limited', retry_after=max(1, int(seconds)))

    if governor is not None:
        # Entity resolution and the page: both are counted before any provider call.
        wait = await governor.admit('chat_history', 2)
        if wait:
            limited(wait, 'refused')
    # Telegram returns messages older than offset_id or sent before offset_date (whole seconds), newest first.
    offset_date = datetime.fromtimestamp(math.ceil(until), timezone.utc) if until is not None else None
    try:
        peer = await rpc(lambda: client.get_input_entity(chat), scope='chat_history')
        request = functions.messages.GetHistoryRequest(peer, anchor, offset_date, -(count // 2) if around else 0,
                                                       count, 0, 0, 0)
        response = await rpc(lambda: client(request), scope='chat_history')
    except ProviderRateLimited as exc:
        counts['flood_wait_s'] = exc.seconds
        limited(exc.seconds, 'flood_wait')
    except (errors.ChannelPrivateError, errors.ChatAdminRequiredError, errors.PeerIdInvalidError):
        finish('refused')
        reject('not_found')
    except errors.RPCError:
        finish('provider_error')
        reject('operation_rejected')
    entities = {}
    for entity in list(getattr(response, 'chats', None) or []) + list(getattr(response, 'users', None) or []):
        try:
            entities[str(utils.get_peer_id(entity))] = entity
        except (TypeError, ValueError):
            continue
    known = {int(key): entity for key, entity in entities.items()}
    entity = entities.get(str(chat))
    returned = list(getattr(response, 'messages', None) or [])[:count]
    kept, below, found = [], False, job.get('_history')
    for message in returned:
        moment = message.date.timestamp() if getattr(message, 'date', None) else None
        if since is not None and moment is not None and moment < since:
            below = True
            continue
        try:
            same = str(utils.get_peer_id(message.peer_id)) == str(chat)
        except (TypeError, ValueError):
            same = False
        # Service messages (joins, titles, calls) are history as iter_messages returns them.
        if not isinstance(message, (types.Message, types.MessageService)) or not same or (until is not None and (moment is None or moment >= until)):
            continue
        kept.append(message)
        if found is not None:
            # Initialized as iter_messages does for a history page, from this response's entities only.
            try:
                message._finish_init(client, known, None)
            except Exception:
                message = None
            found.append((message, {}, chat_kind(entity), entity_name(entity)))
    result = {'outcome': 'confirmed', 'delivery': 'immediate', 'fetched': len(kept), 'basis': 'live_provider_history'}
    if timed:
        result['range_exhausted'] = below or len(returned) < count
    elif not around:
        result['provider_history_start_reached'] = len(returned) < count
    counts['fetched'] = len(kept)
    finish('done')
    return result


async def open_by_username(client, job, parameters, rpc, collection_policy, allowed_provider_id):
    """ANYM-643: one contacts.ResolveUsername, a person only (never a bot, channel or group). The resolve is a read
    in its own governor scope, so its flood wait pauses no send; the chat row is the worker's, after it rechecks
    authority (commands.finish_open_username). Telethon keeps the user's access hash in its session."""
    from .commands import CommandRejected
    from .telegram_governor import ProviderRateLimited
    username = parameters.get('username')
    if (not isinstance(username, str) or not USERNAME.fullmatch(username) or
            type(parameters.get('create', False)) is not bool):
        reject('invalid_parameters')
    if not collection_policy()['discover_chats']:
        reject('chat_not_collected')
    if job.get('lookup_allowed') is not True:
        reject('rate_limited')
    try:
        resolved = await rpc(lambda: client(functions.contacts.ResolveUsernameRequest(username)),
                             scope='resolve_username')
    except ProviderRateLimited as exc:
        raise CommandRejected('rate_limited', retry_after=exc.seconds) from None
    except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
        reject('username_not_found')
    peer = getattr(resolved, 'peer', None)
    if not isinstance(peer, types.PeerUser):
        reject('recipient_not_user')
    user = next((u for u in (getattr(resolved, 'users', None) or [])[:100]
                 if isinstance(u, types.User) and u.id == peer.user_id), None)
    if user is None or type(user.id) is not int or user.id <= 0:
        return {'outcome': 'unknown', 'error': 'provider_result_unverified'}
    if user.deleted:
        reject('username_not_found')
    if user.bot:
        reject('recipient_not_user')
    if not allowed_provider_id(user.id):
        reject('chat_not_collected')
    name = safe_text(' '.join(n for n in (user.first_name, user.last_name) if isinstance(n, str) and n.strip()), 255)
    return {'outcome': 'confirmed', 'found': True, 'user_id': str(user.id), 'username': username,
            'name': name if name and name.strip() else None, 'provider_lookup': True}


async def execute(client, job, *, verified_self=None, governor=None):
    """Return a bounded backend result, never native objects or transport secrets."""
    result = await _execute(client, job, verified_self=verified_self, governor=governor)
    outcome = result.pop('outcome')
    output = {'outcome': outcome, 'data': result}
    if 'provider_message_id' in result:
        output['provider_message_id'] = result['provider_message_id']
    return output
