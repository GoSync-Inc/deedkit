"""ban_member, unban_member and get_member_state (ANYM-935): the archive side, beside member_messages (ANYM-934).

`bind` puts the reviewed person and messages into the payload, so `preview_digest` binds user, chat, end and message
IDs with revisions: the chat is a supergroup, discussion group or channel the archive collects, `user` is an archived
sender of it (a person who posted and left counts), and `message_ids` pass delete_member_messages' checks and are all
that person's. At dispatch the reviewed person and messages stay as confirmed: the delete step runs before the ban,
and the archive may mark the messages deleted in between. `shown` is what the owner reads; `preview_with_state` adds
the person's live state from one get_member_state read, which is shown and never part of the digest.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import time

from .field_errors import refusal
from .store import APIError

MODERATION, BAN, UNBAN, MEMBER_STATE = 'delete_member_messages', 'ban_member', 'unban_member', 'get_member_state'
FAMILY = (MODERATION, BAN, UNBAN, MEMBER_STATE)
STATE_WAIT_S = 10
STATE_FRESH_S = 30  # one live read per person and chat in this window, however often the preview is asked
SENDER = 'the sender_id of a person whose messages this chat\'s archive holds, from get_messages or search'
REMOVES = 'A ban removes this person from the chat if they are in it and keeps them out, whether or not they are a member now'
UNBAN_EFFECT = 'Lifting the ban does not add this person back; they may rejoin by an invite link or by search.'
CHANNEL_SCOPE = ('In a channel a ban acts on its subscribers only. Comments are written in its discussion group, a chat '
                 'with its own ban list: banning there stops comments.')
PROTECTED = 'target_protected: demote this person in the Telegram app first'


def bind(c, p, account, chat, operation, parameters, payload, saved):
    from . import member_messages
    if operation == MODERATION:
        payload['targets'] = member_messages.resolve(c, p, account, chat, parameters)
        return
    if saved is not None:
        payload['person'] = saved.get('person')
        if 'targets' in saved:
            payload['targets'] = saved['targets']
        return
    from .collector_state import CollectorState
    if chat['chat_type'] not in ('group', 'channel') or (
            chat['chat_type'] == 'group' and not str(chat['provider_chat']).startswith('-100')):
        raise APIError('unsupported_chat_type')
    policy = c.execute('SELECT * FROM account_policies WHERE account=%s', (account,)).fetchone()
    if not policy or not CollectorState.allows(policy, chat['provider_chat']):
        raise refusal('invalid_operation_parameters',
                      [('chat_id', 'chat_not_collected', 'a supergroup, discussion group or channel the archive collects')])
    user, targets = parameters['user'], None
    if operation == BAN and 'message_ids' in parameters:
        targets = member_messages.resolve(c, p, account, chat, parameters)
        senders = {r['id']: r['sender'] for r in c.execute('SELECT id,sender FROM messages WHERE account=%s AND id=ANY(%s)',
                                                         (account, [t['message_id'] for t in targets])).fetchall()}
        other = [('message_ids/%d' % i, 'message_from_other_sender', 'a message sent by user')
                 for i, t in enumerate(targets) if senders.get(t['message_id']) != user]
        if other:
            raise refusal('invalid_operation_parameters', other)
        payload['targets'] = targets
    first = c.execute('''SELECT provider_id,direction FROM messages WHERE account=%s AND chat=%s AND sender=%s
        ORDER BY deleted,sent_at,provider_id LIMIT 1''', (account, chat['id'], user)).fetchone()
    if not first:
        raise refusal('invalid_operation_parameters', [('user', 'sender_not_found', SENDER)])
    if first['direction'] == 'outgoing' and operation == BAN:
        failure = APIError('target_protected')
        setattr(failure, 'reason', 'owner')
        raise failure
    payload['person'] = {'user': user,
                         'provider_message_id': targets[0]['provider_message_id'] if targets else first['provider_id']}


def person_profile(c, account, chat, user):
    profile = c.execute('SELECT display_name,username FROM sender_profiles WHERE account=%s AND chat=%s AND sender=%s',
                        (account, chat['id'], user)).fetchone() or {}
    snapshot = c.execute('''SELECT r.snapshot->'sender_profile'->>'display_name' AS name,
        r.snapshot->'sender_profile'->>'username' AS username FROM messages m
        JOIN message_revisions r ON r.message=m.id AND r.revision=m.revision
        WHERE m.account=%s AND m.chat=%s AND m.sender=%s ORDER BY m.sent_at DESC LIMIT 1''',
                         (account, chat['id'], user)).fetchone() or {}
    name = (profile.get('display_name') or '').strip() or (snapshot.get('name') or '').strip() or None
    username = (profile.get('username') or '').strip() or (snapshot.get('username') or '').strip() or None
    return name, username


def duration_words(until, now=None):
    seconds = int((datetime.fromisoformat(until.replace('Z', '+00:00')) - (now or datetime.now(timezone.utc)))
                  .total_seconds())
    parts = []
    for name, size in (('day', 86400), ('hour', 3600), ('minute', 60)):
        count, seconds = divmod(seconds, size)
        if count:
            parts.append('%d %s%s' % (count, name, 's' * (count != 1)))
    return 'for ' + (' '.join(parts[:2]) or 'less than a minute')


def shown(c, p, account, chat, operation, payload):
    """What the owner confirms: the person, the chat and its type, the duration and UTC end, the messages."""
    from . import member_messages
    if operation == MODERATION:
        return member_messages.shown(c, p, account, chat, payload['targets'])
    user = payload['parameters']['user']
    name, username = person_profile(c, account, chat, user)
    kind = member_messages.chat_type(c, p, account, chat)
    info = {'person': {'sender_id': user, 'display_name': name, 'username': username, 'state': 'unknown',
                       'state_basis': 'not_read'},
            'chat': {'chat_id': chat['id'], 'title': chat['title'], 'chat_type': kind},
            'untrusted': ['person.display_name', 'person.username', 'chat.title']}
    if operation == BAN:
        until = payload['parameters'].get('until')
        info.update(duration='forever' if until is None else duration_words(until), until=until,
                    effect=REMOVES + (', for good.' if until is None else ', until %s (UTC).' % until))
        if payload.get('targets'):
            messages = member_messages.shown(c, p, account, chat, payload['targets'])
            info.update(count=messages['count'], messages=messages['messages'], archive=messages['archive'],
                        order='the listed messages are deleted first, then the ban is sent')
            info['untrusted'] += ['messages.sender_name', 'messages.sender_username', 'messages.text']
    else:
        info['effect'] = UNBAN_EFFECT
    if kind == 'channel':
        info.update(channel_scope=CHANNEL_SCOPE, discussion_group_chat_id=None)
    return info


async def preview_with_state(store, token, arguments):
    """The ban or unban preview, then one live read of the person's state shown beside it (never in the digest).
    A refused preview reads nothing; an unavailable read leaves the state unknown with its reason."""
    draft = await asyncio.to_thread(store.preview_messenger_operation, token, **arguments)
    read = await live_state(store, token, draft)
    return await asyncio.to_thread(annotate, store, token, draft, read)


async def live_state(store, token, draft):
    from .send_wait import wait_for_operation
    account, chat_id, user = draft['account'], draft['chat_id'], draft['parameters']['user']
    window = int(time.time() // STATE_FRESH_S)
    key = hashlib.sha256(f'{account}\0{chat_id}\0{user}\0{window}'.encode()).hexdigest()[:40]
    try:
        receipt = await asyncio.to_thread(store.execute_messenger_operation, token, account, MEMBER_STATE, chat_id,
                                          parameters={'user': user}, request_id='ban-state:' + key)
        if receipt.get('status') in ('queued', 'dispatching'):
            receipt = await wait_for_operation(store, token, receipt['operation_id'], STATE_WAIT_S)
    except asyncio.CancelledError:
        raise
    except APIError as error:
        return {'unavailable': error.code}
    except Exception:
        return {'unavailable': 'state_read_failed'}
    data = (receipt.get('result') or {}).get('data') if receipt.get('status') == 'succeeded' else None
    if not isinstance(data, dict) or data.get('state') not in ('member', 'left', 'banned'):
        pending = receipt.get('status') in ('queued', 'dispatching')
        return {'unavailable': 'state_read_pending' if pending else receipt.get('error_code') or 'state_read_failed'}
    return data


def annotate(store, token, draft, read):
    from .commands import permitted
    shown_info = draft['moderation']
    person = shown_info['person']
    if 'unavailable' in read:
        person.update(state='unknown', state_basis='unavailable', state_unavailable=read['unavailable'])
        return draft
    unban = draft['operation'] == UNBAN
    if read['state'] == 'banned':
        state = 'banned forever' if read.get('banned_until') is None else 'banned until %s' % read['banned_until']
    else:
        state = 'not_banned' if unban else read['state']
    person.update(state=state, role=read.get('role'), state_basis='live_provider_observation')
    if not unban and read.get('role') in ('owner', 'admin', 'creator'):
        person['protected'] = PROTECTED
    linked = read.get('linked_chat')
    if 'channel_scope' in shown_info and isinstance(linked, str):
        with store.authorized(token) as (c, p):
            row = c.execute('SELECT id FROM chats WHERE account=%s AND provider_chat=%s',
                            (draft['account'], linked)).fetchone()
            visible = bool(row) and permitted(p, draft['account'], row['id'])
        shown_info['discussion_group_chat_id'] = row['id'] if visible else None
        if not visible:
            shown_info['discussion_group'] = 'a discussion group this key cannot read or the archive does not collect'
    return draft
