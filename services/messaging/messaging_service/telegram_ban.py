"""ban_member, unban_member and get_member_state on Telegram (ANYM-935), beside telegram_moderation (ANYM-934).

ban_member: one rights read of the chat and one participant read of the person (role and current state), then the
listed messages deleted as delete_member_messages deletes them (so a ban failure never leaves the spam standing),
then one channels.EditBanned with view_messages and until_date, then one participant read-back. unban_member sends
empty rights. A person already in the requested state is confirmed with changed false and nothing is written. An
uncertain request is unknown and never retried. Results carry IDs, states and counts, never message content.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from telethon import errors, functions, types, utils

from .messenger_operations import BAN, BAN_MAX_DAYS, MEMBER_STATE, MODERATION, UNBAN
from .telegram_moderation import delete_targets, refuse, targets
from .telegram_operations import number_id, peer_id, read_back_evidence

# Telegram reads an until_date under 30 s or over 366 days ahead as a ban forever: a confirmed end that close is refused.
UNTIL_MARGIN_S = 35
# A requested end within this of the current one is the same ban.
SAME_END_S = 60
# Definite refusals of the EditBanned request: Telegram changed nothing.
BAN_REFUSALS = {errors.ChatAdminRequiredError: 'CHAT_ADMIN_REQUIRED', errors.UserAdminInvalidError: 'USER_ADMIN_INVALID',
                errors.RightForbiddenError: 'RIGHT_FORBIDDEN', errors.ChannelPrivateError: 'CHANNEL_PRIVATE',
                errors.UserIdInvalidError: 'USER_ID_INVALID', errors.ParticipantIdInvalidError: 'PARTICIPANT_ID_INVALID'}


def person(job):
    """(user ID, provider ID of an archived message of theirs or None), from the reviewed job."""
    bound = job.get('person')
    if not isinstance(bound, dict) or str(bound.get('user')) != str((job.get('parameters') or {}).get('user')):
        refuse('invalid_parameters')
    anchor = bound.get('provider_message_id')
    anchor = None if anchor is None else number_id(anchor)
    if anchor is not None and anchor > 2**31 - 1:
        refuse('invalid_parameters')
    return number_id(bound['user']), anchor


def wanted_end(job, now=None):
    """None for forever, else the confirmed end; an end that has come too close is operation_expired."""
    parameters = job.get('parameters') or {}
    if parameters.get('forever') is True and 'until' not in parameters:
        return None
    try:
        end = datetime.fromisoformat(parameters['until'].replace('Z', '+00:00'))
    except (KeyError, AttributeError, ValueError):
        refuse('invalid_parameters')
    left = (end - (now or datetime.now(timezone.utc))).total_seconds()
    if end.tzinfo is None or left > BAN_MAX_DAYS * 86400:
        refuse('invalid_parameters')
    if left < UNTIL_MARGIN_S:
        refuse('operation_expired')
    return end


def check(parameters, job, prepared):
    """PARAMETER_CHECKS: the reviewed person, end and messages, before the peer lookup."""
    if job.get('operation') == MODERATION:
        targets(job)
        return
    person(job)
    if job.get('operation') == BAN:
        wanted_end(job)
        if job.get('targets') is not None or 'message_ids' in parameters:
            targets(job)


def unsent(job, error):
    """A flood wait in the peer lookup, before the handler and before any request: delete_member_messages answers
    with failed rows; the ban operations are rate_limited with retry_after (safe to repeat after the wait)."""
    from .commands import CommandRejected
    from .telegram_governor import ProviderRateLimited
    from .telegram_moderation import unsent as member_messages_unsent
    if job.get('operation') == MODERATION:
        return member_messages_unsent(job, error)
    if isinstance(error, ProviderRateLimited):
        raise CommandRejected('rate_limited', retry_after=error.seconds)
    return None


async def moderate(op):
    if op.operation == MODERATION:
        from .telegram_moderation import delete_member_messages
        return await delete_member_messages(op)
    if op.operation == MEMBER_STATE:
        return await read_member_state(op)
    return await ban(op)


def rights_refusal(entity, with_messages):
    """None when the owner may ban here (and delete with_messages), else (code, evidence)."""
    if entity.creator is True:
        return None
    rights = entity.admin_rights
    if rights is None:
        return 'not_admin', {}
    for right in ('ban_users',) + (('delete_messages',) if with_messages else ()):
        if getattr(rights, right, None) is not True:
            return 'missing_admin_right', {'missing_right': right}
    return None


def participant_state(participant, now=None):
    """{state: member|left|banned, banned_until: UTC text or None (forever), role} of a GetParticipant answer."""
    now = now or datetime.now(timezone.utc)
    if isinstance(participant, types.ChannelParticipantCreator):
        return {'state': 'member', 'banned_until': None, 'role': 'creator'}
    if isinstance(participant, types.ChannelParticipantAdmin):
        return {'state': 'member', 'banned_until': None, 'role': 'admin'}
    if isinstance(participant, types.ChannelParticipantBanned):
        rights = participant.banned_rights
        if getattr(rights, 'view_messages', None) is True:
            end = getattr(rights, 'until_date', None)
            forever = not isinstance(end, datetime) or end.timestamp() <= 0 or end - now > timedelta(days=BAN_MAX_DAYS)
            until = None if forever else end.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
            return {'state': 'banned', 'banned_until': until, 'role': None}
        # Restricted (muted, no media), not banned.
        return {'state': 'left' if participant.left else 'member', 'banned_until': None, 'role': 'member'}
    if isinstance(participant, types.ChannelParticipantLeft):
        return {'state': 'left', 'banned_until': None, 'role': None}
    if isinstance(participant, (types.ChannelParticipant, types.ChannelParticipantSelf)):
        return {'state': 'member', 'banned_until': None, 'role': 'member'}
    return None


async def chat_entity(op):
    entity = await op.rpc(lambda: op.client.get_entity(op.peer))
    if peer_id(entity) != str(op.chat):
        refuse('provider_reference_mismatch')
    if op.channel is None or not isinstance(entity, types.Channel):
        refuse('unsupported_chat_type')
    return entity


async def input_person(op, user, anchor):
    """The person's input peer from the session's cache; for a person it does not hold (a spam bot that posted and
    left), one read of their archived message first, which brings their access hash."""
    async def cached():
        try:
            return await op.rpc(lambda: op.client.get_input_entity(types.PeerUser(user)))
        except (ValueError, TypeError):
            return None
    found = await cached()
    if found is None and anchor is not None:
        await op.rpc(lambda: op.client.get_messages(op.peer, ids=[anchor]))
        found = await cached()
    if not isinstance(found, types.InputPeerUser) or found.user_id != user:
        refuse('identity_unresolved')
    return found


async def member_state(op, peer, *, scope=None):
    try:
        answer = await op.rpc(lambda: op.client(functions.channels.GetParticipantRequest(op.channel, peer)),
                              scope=scope)
    except errors.UserNotParticipantError:
        return {'state': 'left', 'banned_until': None, 'role': None}
    state = participant_state(getattr(answer, 'participant', None))
    if state is None:
        refuse('provider_reference_mismatch')
    return state


async def read_member_state(op):
    user, anchor = person(op.job)
    entity = await chat_entity(op)
    peer = await input_person(op, user, anchor)
    state = await member_state(op, peer)
    if isinstance(op.verified_self, types.User) and op.verified_self.id == user:
        state['role'] = 'owner'
    linked = None
    if entity.broadcast:
        full = await op.invoke(functions.channels.GetFullChannelRequest(op.channel))
        chat = getattr(getattr(full, 'full_chat', None), 'linked_chat_id', None)
        linked = str(utils.get_peer_id(types.PeerChannel(chat))) if type(chat) is int and 0 < chat < 2**52 else None
    return {'outcome': 'confirmed', **state, 'chat_kind': 'channel' if entity.broadcast else 'supergroup',
            'linked_chat': linked, 'basis': 'live_provider_observation'}


def unchanged(before, unban, end):
    if unban:
        return before['state'] != 'banned'
    if before['state'] != 'banned' or (before['banned_until'] is None) != (end is None):
        return False
    return end is None or abs((datetime.fromisoformat(before['banned_until'].replace('Z', '+00:00')) - end)
                              .total_seconds()) < SAME_END_S


def outcome(answer, known):
    """confirmed when the ban's state is known and every listed message settled (deleted or failed)."""
    rows_settled = all(row['state'] in ('deleted', 'failed') for row in answer.get('results', ()))
    return 'confirmed' if known and rows_settled else 'unknown'


def state_fields(state):
    return {'state': 'banned' if state['state'] == 'banned' else 'not_banned', 'banned_until': state['banned_until']}


async def ban(op):
    from .telegram_governor import ProviderRateLimited
    unban = op.operation == UNBAN
    user, anchor = person(op.job)
    end = None if unban else wanted_end(op.job)
    items = targets(op.job) if not unban and op.job.get('targets') is not None else []
    entity = await chat_entity(op)
    refusal = rights_refusal(entity, bool(items))
    if refusal is not None:
        refuse(refusal[0], **refusal[1])
    if not unban and isinstance(op.verified_self, types.User) and op.verified_self.id == user:
        protected('owner')
    peer = await input_person(op, user, anchor)
    before = await member_state(op, peer)
    if not unban and before['role'] in ('creator', 'admin'):
        protected(before['role'])
    answer = {}
    if items:
        answer = await delete_targets(op, items)
        answer.pop('outcome')
        held = [row for row in answer['results'] if row['state'] == 'unknown' or 'retry_after_s' in
                (row.get('evidence') or {})]
        if held:
            # An uncertain delete or a flood wait: the ban is not sent; nothing is repeated.
            return {'outcome': outcome(answer, True), 'changed': False, 'ban_request': 'not_attempted',
                    **state_fields(before), **answer}
    if unchanged(before, unban, end):
        return {'outcome': outcome(answer, True), 'changed': False, 'ban_request': 'not_needed',
                **state_fields(before), **answer}
    if not unban:
        end = wanted_end(op.job)
    rights = types.ChatBannedRights(until_date=None) if unban else types.ChatBannedRights(until_date=end,
                                                                                            view_messages=True)
    attempted = False

    def write():
        nonlocal attempted
        attempted = True
        return op.client(functions.channels.EditBannedRequest(op.channel, peer, rights))
    try:
        await op.rpc(write, mutation=True)
    except asyncio.CancelledError:
        raise
    except ProviderRateLimited as error:
        if not attempted and not items:
            refuse_rate_limited(error.seconds)
        return {'outcome': outcome(answer, not attempted), 'changed': False,
                'ban_request': 'unknown' if attempted else 'not_attempted', **state_fields(before),
                'evidence': read_back_evidence(retry_after_s=error.seconds), **answer}
    except Exception as error:
        if not attempted:
            if not items:
                raise
            # The ban never left (a refused last-moment check): the deletions stand as reported.
            return {'outcome': outcome(answer, True), 'changed': False, 'ban_request': 'not_attempted',
                    **state_fields(before), **answer}
        code = BAN_REFUSALS.get(type(error))
        if code:
            return {'outcome': outcome(answer, True), 'changed': False, 'ban_request': 'refused',
                    'provider_code': code, **state_fields(before), **answer}
        return {'outcome': 'unknown', 'changed': False, 'ban_request': 'unknown', **state_fields(before), **answer}
    try:
        after = await member_state(op, peer, scope='read_back')
    except asyncio.CancelledError:
        raise
    except Exception:
        return {'outcome': 'unknown', 'changed': True, 'ban_request': 'applied', **answer}
    reached = (after['state'] == 'banned') != unban
    return {'outcome': outcome(answer, reached), 'changed': True, 'ban_request': 'applied', **state_fields(after),
            'evidence': read_back_evidence(state=state_fields(after)['state']), **answer}


def protected(reason):
    """target_protected with reason owner, admin or creator: demote in the app first; nothing is sent."""
    from .commands import CommandRejected
    raise CommandRejected('target_protected', data={'reason': reason})


def refuse_rate_limited(seconds):
    from .commands import CommandRejected
    raise CommandRejected('rate_limited', retry_after=seconds)
