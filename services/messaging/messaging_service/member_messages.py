"""delete_member_messages (ANYM-934): the archive side of deleting other people's Telegram messages.

The operation names 1-20 archived messages of other people in one chat with their current revisions. `targets`
binds each one's archive ID, revision and provider ID into the reviewed payload, so `preview_digest` covers exactly
what the owner saw and a revision changed since then refuses with operation_target_changed. `shown` is what the
owner reads before confirming (chat type, senders, text) and is not part of the digest. After the collector's
read-back, `record_deletions` sends each confirmed deletion through the archive's own provider-deletion path
(tombstone, deleted mark, ANYM-510 purge of the content).
"""
import asyncio
from datetime import datetime, timezone
import logging

from .field_errors import refusal
from .store import APIError

log = logging.getLogger('messaging_service.member_messages')
PREVIEW_TEXT = 200
PURGE_LINE = 'the archive copy is purged as well'
# The archive write after a confirmed deletion shares the command's 30 s; past this it is left to the provider's own
# deletion event and the tombstone sweep, and the receipt stays as settled.
ARCHIVE_SECONDS = 10
OTHER_CHAT = 'a message of this chat; a channel post\'s comments are messages of its discussion group'


def _refused(*errors):
    return refusal('invalid_operation_parameters', errors)


def chat_type(c, account, chat):
    """How the preview names the chat: channel, supergroup, basic group or discussion group of <channel title>.

    From the archive alone: a group whose archived messages include one sent as an archived broadcast channel of the
    account (the post Telegram copies into a channel's discussion group) is that channel's discussion group."""
    if chat['chat_type'] == 'channel':
        return 'channel'
    if chat['chat_type'] != 'group':
        return chat['chat_type'] or 'unknown'
    if not str(chat['provider_chat']).startswith('-100'):
        return 'basic group'
    linked = c.execute('''SELECT ch.title FROM chats ch WHERE ch.account=%s AND ch.chat_type='channel'
        AND ch.provider_chat LIKE '-100%%' AND EXISTS(SELECT 1 FROM messages m WHERE m.account=ch.account
            AND m.sender=ch.provider_chat AND m.chat=%s AND NOT m.deleted) ORDER BY ch.id LIMIT 1''',
        (account, chat['id'])).fetchone()
    return 'discussion group of ' + linked['title'] if linked else 'supergroup'


def resolve(c, p, account, chat, parameters, *, permitted):
    """The reviewed targets of one call, in the caller's order, or a refusal naming each bad item.

    Each item must be another person's live archived message of this chat at its current revision; a message the
    key may not read is not_found, never another chat's (nothing of a chat outside the key's scope is shown)."""
    from .collector_state import CollectorState
    policy = c.execute('SELECT * FROM account_policies WHERE account=%s', (account,)).fetchone()
    if chat['chat_type'] not in ('group', 'channel'):
        raise APIError('unsupported_operation')
    if not policy or not CollectorState.allows(policy, chat['provider_chat']):
        raise _refused(('chat_id', 'chat_not_collected', 'a group, discussion group or channel the archive collects'))
    ids, revisions = parameters['message_ids'], parameters['expected_revisions']
    rows = {r['id']: r for r in c.execute('''SELECT m.id,m.chat,m.provider_id,m.revision,m.direction,m.deleted,
        ch.chat_type,ch.provider_chat,ch.title FROM messages m JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
        WHERE m.account=%s AND m.id=ANY(%s)''', (account, list(ids))).fetchall()}
    errors, targets, changed, other = [], [], False, []
    for index, (message_id, revision) in enumerate(zip(ids, revisions)):
        path, row = 'message_ids/%d' % index, rows.get(message_id)
        if row is None or row['deleted'] or not permitted(p, account, row['chat']):
            errors.append((path, 'message_not_found', 'an archived message that is not deleted, from get_messages'))
        elif row['chat'] != chat['id']:
            errors.append((path, 'message_from_other_chat', OTHER_CHAT))
            elsewhere = {'id': row['chat'], 'chat_type': row['chat_type'], 'provider_chat': row['provider_chat']}
            other.append({'field': path, 'chat_id': row['chat'], 'chat_type': chat_type(c, account, elsewhere)})
        elif row['direction'] == 'outgoing':
            errors.append((path, 'own_message', 'another person\'s message; own messages use delete_message'))
        else:
            changed = changed or row['revision'] != revision
            targets.append({'message_id': message_id, 'revision': revision, 'provider_message_id': row['provider_id']})
    if errors:
        failure = _refused(*errors)
        if other:  # chats the key may read: their archive IDs and types, never their text
            setattr(failure, 'other_chats', other)
        raise failure
    if changed:
        raise APIError('operation_target_changed', 409)
    return targets


def shown(c, account, chat, targets):
    """What the owner confirms: the chat type and, per message, its sender, time, kind and first 200 characters."""
    rows = {r['id']: r for r in c.execute('''SELECT m.id,m.sender,m.sent_at,m.body,
        r.snapshot->'metadata'->>'message_kind' AS kind,r.snapshot->'sender_profile'->>'display_name' AS snapshot_name,
        r.snapshot->'sender_profile'->>'username' AS snapshot_username,sp.display_name,sp.username
        FROM messages m JOIN message_revisions r ON r.message=m.id AND r.revision=m.revision
        LEFT JOIN sender_profiles sp ON sp.account=m.account AND sp.chat=m.chat AND sp.sender=m.sender
        WHERE m.account=%s AND m.id=ANY(%s)''', (account, [t['message_id'] for t in targets])).fetchall()}
    messages = []
    for target in targets:
        row = rows[target['message_id']]
        name = (row['display_name'] or '').strip() or (row['snapshot_name'] or '').strip() or None
        username = (row['username'] or '').strip() or (row['snapshot_username'] or '').strip() or None
        messages.append({'message_id': target['message_id'], 'revision': target['revision'],
            'sender_id': row['sender'], 'sender_name': name, 'sender_username': username,
            'sent_at': row['sent_at'], 'kind': row['kind'] or 'unknown', 'text': (row['body'] or '')[:PREVIEW_TEXT]})
    return {'chat': {'chat_id': chat['id'], 'title': chat['title'], 'chat_type': chat_type(c, account, chat)},
            'count': len(messages), 'messages': messages, 'archive': PURGE_LINE,
            'untrusted': ['chat.title', 'messages.sender_name', 'messages.sender_username', 'messages.text']}


def confirmed_ids(job, answer):
    """Provider IDs of the targets the read-back confirmed deleted; anything malformed confirms nothing."""
    data = answer.get('data') if isinstance(answer, dict) else None
    rows = data.get('results') if isinstance(data, dict) else None
    targets = {t.get('message_id'): t.get('provider_message_id') for t in job.get('targets') or []
               if isinstance(t, dict)}
    ids = []
    for row in rows if isinstance(rows, list) else ():
        provider = targets.get(row.get('message_id')) if isinstance(row, dict) else None
        if row.get('state') == 'deleted' and isinstance(provider, str) and provider.isdigit():
            ids.append(int(provider))
    return sorted(set(ids))


async def record_deletions(state, job, answer):
    """Each deletion the read-back confirmed enters the archive as the provider's own deletion would (tombstone,
    deleted mark, purge). Best effort and total: the receipt is already settled, and the provider's deletion event
    and the tombstone sweep still apply it if this write fails or runs out of time."""
    ids = confirmed_ids(job, answer)
    if not ids:
        return 0
    capture = {'kind': 'telegram_delete', 'account': state.account, 'chat': str(job.get('provider_chat')),
               'ids': ids, 'observed_at': datetime.now(timezone.utc).isoformat()}
    try:
        await asyncio.wait_for(asyncio.to_thread(state.apply, capture), ARCHIVE_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning('member_messages archive_unrecorded count=%d error=%s', len(ids), type(exc).__name__)
        return 0
    log.info('member_messages archive_recorded count=%d', len(ids))
    return len(ids)
