"""delete_member_messages on Telegram (ANYM-934): other people's messages deleted for everyone, by the owner's rights.

One rights read of the chat, one `delete_messages(..., revoke=True)` for the listed IDs (channels.DeleteMessages for
a supergroup, discussion group or channel, messages.DeleteMessages for a basic group; at most 20 IDs, one request),
then one read-back of the same IDs. Each target is `deleted` (the read-back finds no message), `failed` (the request
was refused or never sent, or the message is still there) or `unknown`; nothing is retried. Logs and results carry
IDs, states and counts, never message content.
"""
import asyncio

from telethon import errors, types

from .telegram_operations import number_id, peer_id, read_back_evidence

MAX_TARGETS = 20
# Definite refusals of the whole delete request: Telegram deleted nothing.
REFUSALS = {errors.MessageDeleteForbiddenError: 'MESSAGE_DELETE_FORBIDDEN',
            errors.ChatAdminRequiredError: 'CHAT_ADMIN_REQUIRED', errors.ChannelPrivateError: 'CHANNEL_PRIVATE'}


def refuse(code, **evidence):
    from .commands import CommandRejected
    raise CommandRejected(code, data={'evidence': evidence} if evidence else None)


def targets(job):
    """(archive ID, provider ID) of each reviewed target, in order; a malformed job refuses before any request."""
    items = job.get('targets')
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_TARGETS:
        refuse('invalid_parameters')
    rows = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('message_id'), str):
            refuse('invalid_parameters')
        provider = number_id(item.get('provider_message_id'))
        if provider > 2**31 - 1:
            refuse('invalid_parameters')
        rows.append((item['message_id'], provider))
    if len({provider for _, provider in rows}) != len(rows):
        refuse('invalid_parameters')
    return rows


def rights_refusal(entity):
    """None when the owner may delete other people's messages here, else (code, evidence)."""
    if isinstance(entity, types.Chat):
        # A basic group: its creator and every admin may delete any message.
        if entity.creator is True or entity.admin_rights is not None:
            return None
        return 'not_admin', {}
    if isinstance(entity, types.Channel):
        if entity.creator is True:
            return None
        rights = entity.admin_rights
        if rights is None:
            return 'not_admin', {}
        return None if rights.delete_messages is True else ('missing_admin_right', {'missing_right': 'delete_messages'})
    return 'not_admin', {}  # left, forbidden or not a group


def rows(items, state, **evidence):
    return [{'message_id': message_id, 'state': state, **({'evidence': read_back_evidence(**evidence)}
             if evidence else {})} for message_id, _ in items]


def unsent(job, error):
    """Astra r1: a flood wait in the peer lookup, before the handler runs and before any delete request, is each
    target `failed` with retry_after_s (safe to repeat after the wait); None for any other error."""
    from .telegram_governor import ProviderRateLimited
    if not isinstance(error, ProviderRateLimited):
        return None
    return settled(rows(targets(job), 'failed', retry_after_s=error.seconds))


def settled(results):
    deleted = sum(row['state'] == 'deleted' for row in results)
    return {'outcome': 'confirmed' if all(row['state'] in ('deleted', 'failed') for row in results) else 'unknown',
            'results': results, 'deleted': deleted, 'all_deleted': deleted == len(results)}


async def delete_member_messages(op):
    from .telegram_governor import ProviderRateLimited
    items = targets(op.job)
    if op.channel is None and op.basic_chat is None:
        refuse('unsupported_operation')
    ids = [provider for _, provider in items]
    try:
        entity = await op.rpc(lambda: op.client.get_entity(op.peer))
    except ProviderRateLimited as error:
        # Nothing was sent: safe to repeat after the wait.
        return settled(rows(items, 'failed', retry_after_s=error.seconds))
    if peer_id(entity) != str(op.chat):
        refuse('provider_reference_mismatch')
    refusal = rights_refusal(entity)
    if refusal is not None:
        refuse(refusal[0], **refusal[1])
    attempted = False

    def delete():
        nonlocal attempted
        attempted = True
        return op.client.delete_messages(op.peer, ids, revoke=True)
    try:
        await op.rpc(delete, mutation=True)
    except asyncio.CancelledError:
        raise
    except ProviderRateLimited as error:
        return settled(rows(items, 'unknown' if attempted else 'failed', retry_after_s=error.seconds))
    except Exception as error:
        code = REFUSALS.get(type(error))
        if attempted and code:
            return settled([{**row, 'provider_code': code} for row in rows(items, 'failed')])
        if not attempted:
            raise
        return settled(rows(items, 'unknown'))
    return settled(await read_back(op, items))


async def read_back(op, items):
    """ANYM-666's read for a batch: one scoped get_messages of the same IDs; only a definite answer settles a row."""
    from .telegram_governor import ProviderRateLimited
    ids = [provider for _, provider in items]
    try:
        found = await op.rpc(lambda: op.client.get_messages(op.peer, ids=ids), scope='read_back')
    except asyncio.CancelledError:
        raise
    except ProviderRateLimited as error:
        return rows(items, 'unknown', retry_after_s=error.seconds)
    except Exception:
        return rows(items, 'unknown')
    if not isinstance(found, list) or len(found) != len(items):
        return rows(items, 'unknown')
    results = []
    for (message_id, provider), message in zip(items, found):
        if message is None or isinstance(message, types.MessageEmpty):
            results.append({'message_id': message_id, 'state': 'deleted'})
        elif getattr(message, 'id', None) == provider and str(getattr(message, 'chat_id', None)) == str(op.chat):
            results.append({'message_id': message_id, 'state': 'failed',
                            'evidence': read_back_evidence(message_present=True)})
        else:
            results.append({'message_id': message_id, 'state': 'unknown'})
    return results
