"""ANYM-532: preview_message and send_message to a WhatsApp phone number.

The preview runs one open_chat_by_phone lookup (create=false) and binds the resolved
LID into its digest. A send with that digest opens or reuses the canonical chat from the
session store's mapping, spending no second lookup, then runs the unchanged send
pipeline under the caller's request_id. Every wait happens outside a transaction and
the lookup itself runs in the collector's bounded command lane. The number is never logged.
"""
import asyncio
import hashlib
import re
import uuid

from .envelope import canonical, stable_id
from .store import APIError, serial

DIGEST_VALID_SECONDS = 600
LOOKUP_WAIT_SECONDS = 20
PENDING = ('queued', 'dispatching')


def recipient(chat_id, phone):
    """Exactly one of chat_id and phone; the phone's digits, or None for a chat_id call."""
    if not isinstance(chat_id, str) or not isinstance(phone, str) or bool(chat_id) == bool(phone):
        failure = APIError('invalid_arguments'); failure.fields = ['chat_id', 'phone']
        raise failure
    if not phone: return None
    match = (re.fullmatch(r'\+?([1-9][0-9]{7,14})', phone, re.ASCII) or
             re.fullmatch(r'([1-9][0-9]{7,14})@(?:c\.us|s\.whatsapp\.net)', phone, re.ASCII))
    if not match: raise APIError('invalid_phone')
    return match.group(1)


def _native(c, p, account):
    from .whatsapp_capabilities import account_provider
    if not isinstance(account, str) or account not in p['accounts']: raise APIError('not_found', 404)
    row = c.execute('SELECT platform FROM accounts WHERE id=%s', (account,)).fetchone()
    if not row: raise APIError('not_found', 404)
    if row['platform'] != 'whatsapp' or account_provider(c, account, 'whatsapp') != 'whatsmeow':
        failure = APIError('unsupported_operation')
        failure.hint, failure.next_action = 'native_whatsapp_accounts_only', 'use_username_or_existing_chat'
        raise failure


def _admit(store, c, p, account, phone, create):
    """The separate operation's guards (permissions, full account, opt-in, policy, collector), as reads only."""
    from .commands import _resolve
    _resolve(store, c, p, account, 'open_chat_by_phone', '', '', 0, {'phone': '+'+phone, 'create': create},
             ready=True)


def _lookup(c, p, account, phone):
    """This key's latest phone preview lookup of the number inside the digest's validity."""
    return c.execute('''SELECT *,finished_at>clock_timestamp()-%s*interval '1 second' AS fresh
        FROM messenger_commands WHERE principal=%s AND key_id=%s AND account=%s
        AND operation='open_chat_by_phone' AND payload->>'phone_preview'='true'
        AND payload->'parameters'->>'phone'=%s AND created_at>clock_timestamp()-%s*interval '1 second'
        ORDER BY created_at DESC,id DESC LIMIT 1''',
        (DIGEST_VALID_SECONDS, p['id'], p['key_id'], account, phone, DIGEST_VALID_SECONDS)).fetchone()


def _failure(receipt):
    code = receipt.get('error_code') or 'operation_result_unknown'
    failure = APIError(code, 429 if code == 'rate_limited' else 409)
    data = (receipt.get('result') or {}).get('data') or {}
    if isinstance(data.get('resets_in_seconds'), int): failure.resets_in_seconds = data['resets_in_seconds']
    if code == 'identity_changed': failure.next_action = 'preview_message_again'
    return failure


def _fresh(c, lookup_id):
    """Whether the preview lookup a digest binds is still inside the digest's validity."""
    row = c.execute("SELECT finished_at>clock_timestamp()-%s*interval '1 second' AS fresh FROM messenger_commands "
                    "WHERE id=%s AND status='succeeded'", (DIGEST_VALID_SECONDS, lookup_id)).fetchone()
    return bool(row and row['fresh'])


def _draft(c, p, account, phone, lid, lookup_id, text, reply_to, format, link_preview, allow_lossy):
    """The ordinary rendered draft for the number's canonical chat, with a digest that also binds the number
    and the one lookup it came from (a later lookup of the same identity never revives an expired digest)."""
    from .outgoing import reviewed, target
    chat_id = stable_id('chat', account, lid)
    existed = c.execute('SELECT 1 FROM chats WHERE id=%s AND account=%s', (chat_id, account)).fetchone() is not None
    reply_id = None
    if reply_to:
        if not existed: raise APIError('not_found', 404)
        _, reply_id = target(c, p, account, chat_id, reply_to)
    draft = reviewed(c, p, account, chat_id, text, reply_to, format, link_preview, allow_lossy,
                     {'platform': 'whatsapp', 'provider_chat': lid}, reply_id)
    digest = hashlib.sha256(canonical({'recipient': 'phone', 'account': account, 'phone': phone,
                                       'lookup': str(lookup_id), 'draft': draft['preview_digest']})).hexdigest()
    return {**draft, 'preview_digest': digest}, chat_id, existed


def preview_lookup(store, c, p, account, phone, text, reply_to, format, link_preview, allow_lossy):
    """Check the arguments and every guard, then reuse this key's pending or fresh lookup or queue one."""
    from .commands import enqueue
    from .outgoing import format_options, reviewed
    _native(c, p, account)
    format_options(format, link_preview, allow_lossy)
    if not isinstance(reply_to, str) or len(reply_to) > 256: raise APIError('not_found', 404)
    # The text renders before any lookup, so a bad draft spends none.
    reviewed(c, p, account, '', text, '', format, link_preview, allow_lossy,
             {'platform': 'whatsapp', 'provider_chat': ''}, None)
    _admit(store, c, p, account, phone, False)
    row = _lookup(c, p, account, phone)
    if row and (row['status'] in PENDING or row['status'] == 'succeeded' and row['fresh']): return row['id']
    return enqueue(store, c, p, account, 'open_chat_by_phone', parameters={'phone': '+'+phone, 'create': False},
                   request_id='phone-preview:'+uuid.uuid4().hex, internal={'phone_preview': True})['operation_id']


def preview_result(store, c, p, account, phone, operation_id, text, reply_to, format, link_preview, allow_lossy):
    from .commands import other_account_chats, status
    _native(c, p, account)
    _admit(store, c, p, account, phone, False)
    receipt = status(store, c, p, operation_id)
    if receipt['status'] in PENDING:
        failure = APIError('phone_lookup_pending', 409)
        failure.operation_id, failure.next_action = operation_id, 'repeat_preview_message'
        raise failure
    if receipt['status'] != 'succeeded': raise _failure(receipt)
    row = c.execute('SELECT result,finished_at FROM messenger_commands WHERE id=%s', (operation_id,)).fetchone()
    data = row['result']['data']
    if data.get('on_whatsapp') is not True:
        failure = APIError('not_on_whatsapp', 404)
        failure.phone, failure.next_action = '+'+phone, 'use_username_or_existing_chat'
        raise failure
    if not isinstance(data.get('provider_chat'), str): raise APIError('identity_unresolved', 409)
    draft, chat_id, existed = _draft(c, p, account, phone, data['provider_chat'], operation_id, text, reply_to,
                                     format, link_preview, allow_lossy)
    others = []
    for item in other_account_chats(c, p, account, phone):
        last = c.execute('''SELECT sent_at FROM messages WHERE account=%s AND chat=%s AND NOT deleted
            ORDER BY sent_at DESC LIMIT 1''', (item['account'], item['chat_id'])).fetchone()
        others.append({**item, 'last_message_at': last['sent_at'] if last else None})
    if not existed: draft.update(chat_id=None, provider_chat_id=None)
    expires = c.execute("SELECT %s::timestamptz+%s*interval '1 second' AS at",
                        (row['finished_at'], DIGEST_VALID_SECONDS)).fetchone()['at']
    return serial({**draft, 'phone': '+'+phone, 'on_whatsapp': True, 'verified_name': data.get('verified_name'),
            'chat_existed': existed, 'chats_on_other_accounts': others, 'preview_contacts_provider': True,
            'lookup_operation_id': operation_id, 'preview_digest_expires_at': expires})


def send_hash(account, phone, text, reply_to, format, preview_digest, link_preview, allow_lossy):
    """The request hash of a phone send: every caller field, the normalized number included."""
    return hashlib.sha256(canonical({'recipient': 'phone', 'account': account, 'phone': phone, 'text': text,
        'reply_to': reply_to, 'format': format, 'preview_digest': preview_digest,
        'render_policy': {'link_preview': link_preview, 'allow_lossy': allow_lossy}})).hexdigest()


def _step(c, p, request_id):
    """The chat step reserving this request_id. Send request IDs are per principal, so another key's step
    (taken under the same principal lock) is a conflict, never a second reservation."""
    from .outgoing import phone_step_id
    step = c.execute('SELECT * FROM messenger_commands WHERE principal=%s AND request_id=%s ORDER BY created_at LIMIT 1',
                     (p['id'], phone_step_id(request_id))).fetchone()
    if step and step['key_id'] != p['key_id']: raise APIError('idempotency_conflict', 409)
    return step


def _sent(store, c, p, row, step):
    from .commands import status
    from .outgoing import result
    data = (status(store, c, p, step['id'])['result'] or {}).get('data') or {}
    lookup = c.execute('SELECT result FROM messenger_commands WHERE id=%s',
                       (step['payload'].get('preview_lookup'),)).fetchone()
    name = ((lookup or {}).get('result') or {}).get('data', {}).get('verified_name')
    return {**result(row, c, p), 'chat_id': row['chat'], 'chat_existed': data.get('existing') is True,
            'verified_name': name}


def send_begin(store, c, p, account, phone, text, request_id, reply_to, format, preview_digest,
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
    _native(c, p, account)
    expected = send_hash(account, phone, text, reply_to, format, preview_digest, link_preview, allow_lossy)
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
    _admit(store, c, p, account, phone, True)
    lookup = _lookup(c, p, account, phone)
    data = (lookup['result'] or {}).get('data') or {} if lookup and lookup['status'] == 'succeeded' else {}
    if not lookup or not lookup['fresh'] or not isinstance(data.get('provider_chat'), str):
        failure = APIError('preview_expired', 409); failure.next_action = 'preview_message_again'
        raise failure
    draft, _, _ = _draft(c, p, account, phone, data['provider_chat'], lookup['id'], text, reply_to, format,
                         link_preview, allow_lossy)
    if draft['preview_digest'] != preview_digest:
        failure = APIError('preview_mismatch', 409); failure.next_action = 'preview_message_again'
        raise failure
    receipt = enqueue(store, c, p, account, 'open_chat_by_phone', parameters={'phone': '+'+phone, 'create': True},
                      request_id=phone_step_id(request_id), internal={'expected_lid': data['provider_chat'],
                      'send_request_hash': expected, 'preview_lookup': lookup['id'],
                      'verified_name': data.get('verified_name')})
    return {'operation_id': receipt['operation_id']}


def send_finish(store, c, p, account, phone, operation_id, text, request_id, reply_to, format, preview_digest,
                link_preview, allow_lossy, child=False):
    """After the chat step: the unchanged send pipeline under the caller's request_id."""
    from .commands import status
    from .outgoing import enqueue
    expected = send_hash(account, phone, text, reply_to, format, preview_digest, link_preview, allow_lossy)
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
    if data.get('on_whatsapp') is not True or not isinstance(data.get('chat_id'), str):
        raise APIError('identity_unresolved', 409)
    # Before the first outbox insert: every guard again, and the preview still valid (a stop after the chat step).
    _admit(store, c, p, account, phone, True)
    if not _fresh(c, step['payload'].get('preview_lookup')):
        failure = APIError('preview_expired', 409); failure.next_action = 'preview_message_again'
        raise failure
    draft, chat_id, _ = _draft(c, p, account, phone, step['payload']['expected_lid'],
                               step['payload'].get('preview_lookup'), text, reply_to, format, link_preview, allow_lossy)
    if chat_id != data['chat_id'] or draft['preview_digest'] != preview_digest: raise APIError('preview_mismatch', 409)
    row = enqueue(c, p, account, chat_id, text, request_id, reply_to, format, preview_digest, link_preview,
                  allow_lossy, phone_hash=expected, child=child)
    sent = c.execute('SELECT * FROM outgoing_messages WHERE id=%s', (row['send_id'],)).fetchone()
    return _sent(store, c, p, sent, step)


async def _wait(store, token, operation_id):
    from .live_search import wait
    try: await wait(store, token, [operation_id], LOOKUP_WAIT_SECONDS)
    except Exception: pass  # The receipt read that follows decides; a failed wait sends nothing.


async def preview(store, token, arguments):
    run = asyncio.to_thread
    operation_id = await run(store.phone_preview_lookup, token, **arguments)
    await _wait(store, token, operation_id)
    return await run(store.phone_preview_result, token, operation_id=operation_id, **arguments)


async def send(store, token, arguments):
    run = asyncio.to_thread
    step = await run(store.phone_send_begin, token, **arguments)
    if 'sent' in step: return step['sent']
    await _wait(store, token, step['operation_id'])
    return await run(store.phone_send_finish, token, operation_id=step['operation_id'], **arguments)
