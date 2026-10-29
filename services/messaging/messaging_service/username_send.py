"""ANYM-643: preview_message and send_message to a Telegram @username with no archived chat.

The preview runs one open_chat_by_username lookup (create=false) and binds the resolved user ID
into its digest. A send with that digest queues one create=true step, which resolves the username
at Telegram again and opens the private chat only when it still names the same user, then runs the
unchanged send pipeline under the caller's request_id. It follows phone_send (ANYM-532) step for
step: every wait happens outside a transaction and the lookup runs in the collector's bounded
command lane. The username and user ID are never logged.
"""
import asyncio
import hashlib
import re
import uuid

from .envelope import canonical, stable_id
from .phone_send import DIGEST_VALID_SECONDS, PENDING, _failure as _lookup_failure, _fresh, _step, _wait
from .store import APIError, serial

OPERATION = 'open_chat_by_username'


def recipient(chat_id, phone, username):
    """The normalized username, or None when the call names chat_id or phone (phone_send.recipient checks those)."""
    if not all(isinstance(v, str) for v in (chat_id, phone, username)) or username and (chat_id or phone):
        failure = APIError('invalid_arguments'); failure.fields = ['chat_id', 'phone', 'username']
        raise failure
    if not username: return None
    from .messenger_operations import telegram_username
    name = telegram_username(username)
    if not name: raise APIError('invalid_username')
    return name


def _telegram(c, p, account):
    if not isinstance(account, str) or account not in p['accounts']: raise APIError('not_found', 404)
    row = c.execute('SELECT platform FROM accounts WHERE id=%s', (account,)).fetchone()
    if not row: raise APIError('not_found', 404)
    if row['platform'] != 'telegram':
        failure = APIError('unsupported_operation')
        failure.hint, failure.next_action = 'telegram_accounts_only', 'use_phone_or_existing_chat'
        raise failure


def _admit(store, c, p, account, username, create):
    """The separate operation's guards (permissions, full account, opt-in, policy, collector), as reads only."""
    from .commands import _resolve
    _resolve(store, c, p, account, OPERATION, '', '', 0, {'username': username, 'create': create}, ready=True)


def _lookup(c, p, account, username):
    """This key's latest username preview lookup inside the digest's validity."""
    return c.execute('''SELECT *,finished_at>clock_timestamp()-%s*interval '1 second' AS fresh
        FROM messenger_commands WHERE principal=%s AND key_id=%s AND account=%s
        AND operation='open_chat_by_username' AND payload->>'username_preview'='true'
        AND payload->'parameters'->>'username'=%s AND created_at>clock_timestamp()-%s*interval '1 second'
        ORDER BY created_at DESC,id DESC LIMIT 1''',
        (DIGEST_VALID_SECONDS, p['id'], p['key_id'], account, username, DIGEST_VALID_SECONDS)).fetchone()


def _failure(receipt):
    failure = _lookup_failure(receipt)
    data = (receipt.get('result') or {}).get('data') or {}
    if failure.code == 'rate_limited' and isinstance(data.get('retry_after'), int):
        failure.retry_after_s = data['retry_after']  # A Telegram flood wait on the resolve.
    return failure


def _resolved(data):
    """The user ID of a lookup result, or None."""
    user = data.get('user_id') if data.get('found') is True else None
    return user if isinstance(user, str) and re.fullmatch(r'[1-9][0-9]{0,18}', user) else None


def _draft(c, p, account, username, user_id, lookup_id, text, reply_to, format, link_preview, allow_lossy):
    """The ordinary rendered draft for the user's private chat, with a digest that also binds the username,
    the resolved user ID and the one lookup it came from (a later lookup never revives an expired digest)."""
    from .outgoing import reviewed, target
    chat_id = stable_id('chat', account, user_id)
    existed = c.execute('SELECT 1 FROM chats WHERE id=%s AND account=%s', (chat_id, account)).fetchone() is not None
    reply_id = None
    if reply_to:
        if not existed: raise APIError('not_found', 404)
        _, reply_id = target(c, p, account, chat_id, reply_to)
    draft = reviewed(c, p, account, chat_id, text, reply_to, format, link_preview, allow_lossy,
                     {'platform': 'telegram', 'provider_chat': user_id}, reply_id)
    digest = hashlib.sha256(canonical({'recipient': 'username', 'account': account, 'username': username,
                                       'user_id': user_id, 'lookup': str(lookup_id),
                                       'draft': draft['preview_digest']})).hexdigest()
    return {**draft, 'preview_digest': digest}, chat_id, existed


def preview_lookup(store, c, p, account, username, text, reply_to, format, link_preview, allow_lossy):
    """Check the arguments and every guard, then reuse this key's pending or fresh lookup or queue one."""
    from .commands import enqueue
    from .outgoing import format_options, reviewed
    _telegram(c, p, account)
    format_options(format, link_preview, allow_lossy)
    if not isinstance(reply_to, str) or len(reply_to) > 256: raise APIError('not_found', 404)
    # The text renders before any lookup, so a bad draft spends none.
    reviewed(c, p, account, '', text, '', format, link_preview, allow_lossy,
             {'platform': 'telegram', 'provider_chat': ''}, None)
    _admit(store, c, p, account, username, False)
    row = _lookup(c, p, account, username)
    if row and (row['status'] in PENDING or row['status'] == 'succeeded' and row['fresh']): return row['id']
    return enqueue(store, c, p, account, OPERATION, parameters={'username': username, 'create': False},
                   request_id='username-preview:'+uuid.uuid4().hex, internal={'username_preview': True})['operation_id']


def preview_result(store, c, p, account, username, operation_id, text, reply_to, format, link_preview, allow_lossy):
    from .commands import status
    _telegram(c, p, account)
    _admit(store, c, p, account, username, False)
    receipt = status(store, c, p, operation_id)
    if receipt['status'] in PENDING:
        failure = APIError('username_lookup_pending', 409)
        failure.operation_id, failure.next_action = operation_id, 'repeat_preview_message'
        raise failure
    if receipt['status'] != 'succeeded': raise _failure(receipt)
    row = c.execute('SELECT result,finished_at FROM messenger_commands WHERE id=%s', (operation_id,)).fetchone()
    data = row['result']['data']
    user_id = _resolved(data)
    if user_id is None: raise APIError('identity_unresolved', 409)
    draft, chat_id, existed = _draft(c, p, account, username, user_id, operation_id, text, reply_to, format,
                                     link_preview, allow_lossy)
    if not existed: draft.update(chat_id=None, provider_chat_id=None)
    expires = c.execute("SELECT %s::timestamptz+%s*interval '1 second' AS at",
                        (row['finished_at'], DIGEST_VALID_SECONDS)).fetchone()['at']
    return serial({**draft, 'username': '@'+username, 'user_id': user_id, 'name': data.get('name'),
                   'chat_existed': existed, 'preview_contacts_provider': True, 'lookup_operation_id': operation_id,
                   'preview_digest_expires_at': expires})


def send_hash(account, username, text, reply_to, format, preview_digest, link_preview, allow_lossy):
    """The request hash of a username send: every caller field, the normalized username included."""
    return hashlib.sha256(canonical({'recipient': 'username', 'account': account, 'username': username,
        'text': text, 'reply_to': reply_to, 'format': format, 'preview_digest': preview_digest,
        'render_policy': {'link_preview': link_preview, 'allow_lossy': allow_lossy}})).hexdigest()


def _sent(store, c, p, row, step):
    from .commands import status
    from .outgoing import result
    data = (status(store, c, p, step['id'])['result'] or {}).get('data') or {}
    return {**result(row, c, p), 'chat_id': row['chat'], 'chat_existed': data.get('existing') is True,
            'user_id': step['payload'].get('expected_user_id'), 'name': step['payload'].get('preview_name')}


def send_begin(store, c, p, account, username, text, request_id, reply_to, format, preview_digest,
               link_preview, allow_lossy, child=False):
    """The first send result of this request_id, or the chat step to wait for. Nothing reaches a provider here."""
    from .commands import enqueue
    from .outgoing import CHILD_KEY_PREFIX, format_options, phone_step_id, valid_text
    format_options(format, link_preview, allow_lossy, preview_digest)
    if not isinstance(text, str) or not text.strip(): raise APIError('invalid_message_text')
    if format == 'plain': valid_text(text)
    if (not isinstance(request_id, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,128}', request_id)
            or request_id.startswith(CHILD_KEY_PREFIX) != child):
        raise APIError('invalid_request_id')
    if not isinstance(reply_to, str) or len(reply_to) > 256: raise APIError('not_found', 404)
    _telegram(c, p, account)
    expected = send_hash(account, username, text, reply_to, format, preview_digest, link_preview, allow_lossy)
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('outgoing:'+p['id'],))
    sent = c.execute('SELECT * FROM outgoing_messages WHERE principal=%s AND request_id=%s',
                     (p['id'], request_id)).fetchone()
    step = _step(c, p, request_id)
    if sent:
        if sent['request_hash'] != expected or not step: raise APIError('idempotency_conflict', 409)
        return {'sent': _sent(store, c, p, sent, step)}
    if step:
        if step['payload'].get('send_request_hash') != expected: raise APIError('idempotency_conflict', 409)
        return {'operation_id': step['id']}
    if not preview_digest: raise APIError('preview_required', 409)
    policy = c.execute('SELECT enabled,send_enabled FROM account_policies WHERE account=%s', (account,)).fetchone()
    if not policy or not policy['enabled'] or not policy['send_enabled']: raise APIError('sending_disabled', 409)
    _admit(store, c, p, account, username, True)
    lookup = _lookup(c, p, account, username)
    data = (lookup['result'] or {}).get('data') or {} if lookup and lookup['status'] == 'succeeded' else {}
    user_id = _resolved(data)
    if not lookup or not lookup['fresh'] or user_id is None:
        failure = APIError('preview_expired', 409); failure.next_action = 'preview_message_again'
        raise failure
    draft, _, _ = _draft(c, p, account, username, user_id, lookup['id'], text, reply_to, format,
                         link_preview, allow_lossy)
    if draft['preview_digest'] != preview_digest:
        failure = APIError('preview_mismatch', 409); failure.next_action = 'preview_message_again'
        raise failure
    receipt = enqueue(store, c, p, account, OPERATION, parameters={'username': username, 'create': True},
                      request_id=phone_step_id(request_id), internal={'expected_user_id': user_id,
                      'send_request_hash': expected, 'preview_lookup': lookup['id'],
                      'preview_name': data.get('name')})
    return {'operation_id': receipt['operation_id']}


def send_finish(store, c, p, account, username, operation_id, text, request_id, reply_to, format, preview_digest,
                link_preview, allow_lossy, child=False):
    """After the chat step: the unchanged send pipeline under the caller's request_id."""
    from .commands import status
    from .outgoing import enqueue
    expected = send_hash(account, username, text, reply_to, format, preview_digest, link_preview, allow_lossy)
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('outgoing:'+p['id'],))
    step = _step(c, p, request_id)
    if not step or step['id'] != operation_id or step['payload'].get('send_request_hash') != expected:
        raise APIError('idempotency_conflict', 409)
    sent = c.execute('SELECT * FROM outgoing_messages WHERE principal=%s AND request_id=%s',
                     (p['id'], request_id)).fetchone()
    if sent:
        if sent['request_hash'] != expected: raise APIError('idempotency_conflict', 409)
        return _sent(store, c, p, sent, step)
    receipt = status(store, c, p, operation_id)
    if receipt['status'] in PENDING:
        return {'status': 'opening_chat', 'operation_id': operation_id, 'request_id': request_id, 'send_id': None,
                'automatic_retry': False, 'next_action': 'repeat_send_message_with_same_request_id'}
    if receipt['status'] != 'succeeded': raise _failure(receipt)
    data = receipt['result']['data']
    if _resolved(data) != step['payload']['expected_user_id'] or not isinstance(data.get('chat_id'), str):
        raise APIError('identity_unresolved', 409)
    # Before the first outbox insert: every guard again, and the preview still valid (a stop after the chat step).
    _admit(store, c, p, account, username, True)
    if not _fresh(c, step['payload'].get('preview_lookup')):
        failure = APIError('preview_expired', 409); failure.next_action = 'preview_message_again'
        raise failure
    draft, chat_id, _ = _draft(c, p, account, username, step['payload']['expected_user_id'],
                               step['payload'].get('preview_lookup'), text, reply_to, format, link_preview, allow_lossy)
    if chat_id != data['chat_id'] or draft['preview_digest'] != preview_digest: raise APIError('preview_mismatch', 409)
    row = enqueue(c, p, account, chat_id, text, request_id, reply_to, format, preview_digest, link_preview,
                  allow_lossy, phone_hash=expected, child=child)
    sent = c.execute('SELECT * FROM outgoing_messages WHERE id=%s', (row['send_id'],)).fetchone()
    return _sent(store, c, p, sent, step)


async def preview(store, token, arguments):
    run = asyncio.to_thread
    operation_id = await run(store.username_preview_lookup, token, **arguments)
    await _wait(store, token, operation_id)
    return await run(store.username_preview_result, token, operation_id=operation_id, **arguments)


async def send(store, token, arguments):
    run = asyncio.to_thread
    step = await run(store.username_send_begin, token, **arguments)
    if 'sent' in step: return step['sent']
    await _wait(store, token, step['operation_id'])
    return await run(store.username_send_finish, token, operation_id=step['operation_id'], **arguments)
