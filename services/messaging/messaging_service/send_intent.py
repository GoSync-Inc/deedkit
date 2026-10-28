"""messages_send (ANYM-546): text and files to any target in one call, with a durable preview and safe resume.

A preview is a send_previews row keyed by the hash of its confirm_token (ANYM-544); the token itself is never stored.
A live call binds one send_intents row before any step runs: the canonical payload, the plan, and per step its
child key, handle and receipt. Each step is the unchanged low-level route under a child key (`agent-child:` plus
agent.child_key, which caller routes refuse): the outbox (outgoing.enqueue), the operation queue (commands.enqueue
send_file) or the phone send (phone_send). A step is enqueued and its handle recorded in one transaction, so a
repeat with the same key reads the record and continues; an uncertain step ends the parent in `unknown` and is
never resumed. No server approval: the acting agent confirms with the owner before any send (SENDING.md); the
token binds a preview, it is not that confirmation. Text leaves both tables once no unsent step needs it.
A small file may come inline (ANYM-547): {name, mime, base64}, at most INLINE_BYTES decoded per call. Its bytes become
this key's upload under a child key of the call (`stage`); previews and intents keep only its upload_id and facts,
and a successful send deletes the staged bytes (`release`), else they expire like any upload.
"""
import asyncio
import base64
import binascii
import hashlib
import re
import secrets
import time

from platform_contracts.agent import (PLACEHOLDER_REQUEST_ID, PREVIEW_TTL_S, agent_result, child_key, is_request_id,
                                      missing_request_id, next_call)
from psycopg.errors import LockNotAvailable
from psycopg.types.json import Jsonb

from .envelope import canonical
from .outgoing import CHILD_KEY_PREFIX
from .store import APIError, serial

TOOL = 'messages_send'
# allow_open_chat and allow_files are set by the gateway from its open_chat_by_phone (ANYM-532) and send_file grants
# on every call, token-only ones included; omitted means a direct caller.
CONTROL = ('dry_run', 'confirm_token', 'request_id', 'resume', 'wait_s', 'allow_open_chat', 'allow_files')
FIELDS = frozenset(('to', 'account', 'reply_to', 'text', 'files', 'format', *CONTROL))
FILE_FIELDS = frozenset(('upload_id', 'attachment_id', 'mode'))
INLINE_FIELDS = frozenset(('name', 'mime', 'base64', 'mode'))
# Inline files of one call, decoded; a larger file goes through UPLOAD_TOOL and chunks.
INLINE_BYTES = 262144
# The request body a messages_send call may need (gateway and messaging): the inline base64, text as \u escapes, room.
SEND_BODY_BYTES = 524288
UPLOAD_TOOL = 'messages_prepare_file_upload'
# Stands in for an inline file's bytes in a `next`: never canonical base64, so such a `next` never runs verbatim.
INLINE_PLACEHOLDER = 'REPLACE_WITH_THE_SAME_BASE64'
MIME = re.compile(r'[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+')
MAX_FILES = 10
# Fresh child keys `stage` tries for one inline file once earlier ones were released or expired.
STAGE_ATTEMPTS = 16
CAPTION_LIMIT = 1024
COMPARED = ('target', 'reply_to', 'text', 'files', 'scope')
PENDING = frozenset(('queued', 'dispatching'))
DEFINITE = frozenset(('failed', 'expired', 'cancelled'))


def _error(code, status=409, **fields):
    error = APIError(code, status)
    for name, value in fields.items(): setattr(error, name, value)
    return error


def _invalid(field, reason, hint=None):
    return _error('invalid_arguments', 400, field_errors=[{'field': field, 'reason': reason}],
                  **({'hint': hint} if hint else {}))


def _hash(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else canonical(value)).hexdigest()


def _inline(index, item):
    """An inline file's canonical bytes; field_errors name the field, never its value."""
    name, mime, data = item.get('name'), item.get('mime'), item.get('base64')
    if (not isinstance(name, str) or not 1 <= len(name) <= 255 or name in ('.', '..') or
            any(ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF or ch in '/\\' for ch in name)):
        raise _invalid(f'files.{index}.name', 'pattern')
    if not isinstance(mime, str) or len(mime) > 128 or not MIME.fullmatch(mime): raise _invalid(f'files.{index}.mime', 'pattern')
    if 'mode' in item and (not isinstance(item['mode'], str) or not 1 <= len(item['mode']) <= 256):
        raise _invalid(f'files.{index}.mode', 'type')
    if not isinstance(data, str): raise _invalid(f'files.{index}.base64', 'type')
    try: raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error): raise _invalid(f'files.{index}.base64', 'format') from None
    if not raw or base64.b64encode(raw).decode() != data: raise _invalid(f'files.{index}.base64', 'format')
    return raw


def _facts(item):
    """The identity of an inline file: what its preview, payload hash and a `next` carry instead of its bytes."""
    if 'inline' in item: return item['inline']
    raw = base64.b64decode(item['base64'])
    return {'name': item['name'], 'mime': item['mime'], 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def _staged_item(index, item):
    """A staged inline file (`stage`): its upload of this key and its facts; never a caller's form."""
    facts = item.get('inline')
    if (set(item) - {'upload_id', 'inline', 'mode'} or not isinstance(item.get('upload_id'), str)
            or not isinstance(facts, dict) or set(facts) != {'name', 'mime', 'bytes', 'sha256'}):
        raise _invalid(f'files.{index}', 'type')
    return item


def public(value):
    """The payload as its hash and comparisons see it: an inline file by its facts, whether staged or not."""
    return {**value, 'files': [{'inline': _facts(item), **({'mode': item['mode']} if 'mode' in item else {})}
                               if 'base64' in item or 'inline' in item else item for item in value['files']]}


def callable_files(files):
    """Files as a caller may send them again: a staged file by its upload_id, an inline one without its bytes."""
    if not isinstance(files, list): return files
    return [{'upload_id': item['upload_id'], **({'mode': item['mode']} if 'mode' in item else {})}
            if isinstance(item, dict) and 'inline' in item else
            {**item, 'base64': INLINE_PLACEHOLDER} if isinstance(item, dict) and 'base64' in item else item
            for item in files]


def _callable(value):
    return {**value, 'files': callable_files(value['files'])} if 'files' in value else value


def payload(arguments):
    """The canonical caller payload (control fields excluded); field_errors for a malformed call, before any read."""
    if not isinstance(arguments, dict): raise _invalid('arguments', 'type')
    unknown = sorted(set(arguments) - FIELDS)
    if unknown: raise _invalid(unknown[0], 'unknown_field')
    value = {'to': arguments.get('to', ''), 'account': arguments.get('account', ''),
             'reply_to': arguments.get('reply_to', ''), 'text': arguments.get('text', ''),
             'files': arguments.get('files', []), 'format': arguments.get('format', 'plain')}
    for name, limit in (('to', 256), ('account', 64), ('reply_to', 256), ('text', 16000)):
        if not isinstance(value[name], str): raise _invalid(name, 'type')
        if len(value[name]) > limit: raise _invalid(name, 'max_length')
    if value['reply_to'] and not value['reply_to'].startswith('msg_'): raise _invalid('reply_to', 'pattern')
    if value['format'] not in ('plain', 'markdown', 'html'): raise _invalid('format', 'enum')
    if not value['to'] and not value['reply_to']: raise _invalid('to', 'required')
    for name in ('dry_run', 'resume', 'allow_open_chat', 'allow_files'):
        if name in arguments and type(arguments[name]) is not bool: raise _invalid(name, 'type')
    files = value['files']
    if not isinstance(files, list): raise _invalid('files', 'type')
    if len(files) > MAX_FILES: raise _invalid('files', 'max_items')
    clean = []
    inline = []
    for index, item in enumerate(files):
        if not isinstance(item, dict): raise _invalid(f'files.{index}', 'type')
        if 'inline' in item:
            clean.append(_staged_item(index, item))
            continue
        if {'name', 'mime', 'base64'} & set(item):
            extra = sorted(set(item) - INLINE_FIELDS)
            if extra: raise _invalid(f'files.{index}.{extra[0]}', 'unknown_field')
            inline.append((index, len(_inline(index, item))))
            clean.append({name: item[name] for name in sorted(item)})
            continue
        extra = sorted(set(item) - FILE_FIELDS)
        if extra: raise _invalid(f'files.{index}.{extra[0]}', 'unknown_field')
        if ('upload_id' in item) == ('attachment_id' in item): raise _invalid(f'files.{index}.upload_id', 'required')
        for name in sorted(item):
            if not isinstance(item[name], str) or not 1 <= len(item[name]) <= 256:
                raise _invalid(f'files.{index}.{name}', 'type')
        clean.append({name: item[name] for name in sorted(item)})
    total = 0
    for index, size in inline:
        total += size
        if total > INLINE_BYTES:
            # The upload call that takes this file instead is added once the recipient is known (`stage`).
            raise _error('invalid_arguments', 400, field_errors=[{'field': f'files.{index}.base64', 'reason': 'max_bytes'}],
                         hint=f'Inline files take at most {INLINE_BYTES} bytes per call: upload this one with '
                              f'{UPLOAD_TOOL} and upload_file_chunk, then send it as {{upload_id}}.',
                         inline_file=_facts(files[index]))
    if len({canonical(item) for item in clean}) != len(clean): raise _invalid('files', 'unique')
    if not value['text'].strip() and not clean: raise _invalid('text', 'required')
    return {**value, 'files': clean}


def _file(store, c, p, account, chat_id, item, caption):
    """One file's identity under the low-level checks; the send_file preview raises the same refusals as
    execute_messenger_operation (permissions, opt-in, owner, destination, hidden source)."""
    from .commands import preview
    from .outgoing import permitted
    from .uploads import owned
    if 'upload_id' in item:
        row = owned(c, p, item['upload_id'])
        if row['account'] != account or row['chat'] != chat_id: raise APIError('not_found', 404)
        name, size, digest, mime = row['filename'], row['bytes'], row['sha256'], row['mime']
    else:
        row = c.execute('''SELECT a.filename,a.bytes,a.digest,a.mime,m.account,m.chat FROM attachments a
            JOIN messages m ON m.id=a.message WHERE a.id=%s AND a.active AND NOT m.deleted''',
                        (item['attachment_id'],)).fetchone()
        if not row or row['account'] != account or not permitted(p, account, row['chat']):
            raise APIError('not_found', 404)
        name, size, digest, mime = row['filename'], row['bytes'], row['digest'], row['mime']
    mode = item.get('mode', 'document')
    source = 'upload_id' if 'upload_id' in item else 'attachment_id'
    parameters = {source: item[source], 'sha256': digest, 'mode': mode, 'caption': caption}
    checked = preview(store, c, p, account, 'send_file', chat_id, parameters=parameters)
    return {'source': source, 'id': item[source], 'name': name, 'bytes': size, 'sha256': digest, 'mime': mime,
            'mode': mode, 'caption': caption, 'digest': checked['preview_digest']}


def _text(draft):
    return {'format': draft['format'], 'visible': draft['rendered_text'], 'warnings': list(draft.get('warnings') or []),
            'links': list(draft.get('links') or []), 'digest': draft['preview_digest']}


def resolve(store, c, p, value, phone_text=None, open_chat=True, files=True):
    """Everything the owner should see, from the caller's current scope. Refusals are the low-level codes.
    A phone target that opens a chat needs its lookup draft (`phone_text`); without one it returns None."""
    from .outgoing import reviewed, target, valid_text
    from .resolve_target import resolve_target
    if value['files'] and not files: raise APIError('forbidden', 403)
    reply = resolve_target(store, c, p, value['reply_to'], 'message', value['account'])['target'] \
        if value['reply_to'] else None
    to = resolve_target(store, c, p, value['to'], 'message', value['account'])['target'] if value['to'] else reply
    if reply and (to.get('chat_id') != reply['chat_id'] or to['account'] != reply['account']):
        raise _invalid('reply_to', 'format', 'reply_to must be a message in the chat that to names')
    account = to['account']
    recipient = {'account': account, 'network': to['network'], 'label': to['label']}
    scope = {'scope_version': p['scope_version'], 'key_permission_version': p['key_permission_version']}
    text = value['text'] if value['text'].strip() else ''
    if text and value['format'] == 'plain': valid_text(text)
    if to.get('opens_chat'):
        if not open_chat: raise APIError('forbidden', 403)
        if value['files']:
            raise _invalid('files', 'max_items', 'a phone without a chat takes text only: send it, then the files')
        if phone_text is None: return None
        return {'target': {**recipient, 'chat_id': None, 'opens_chat': True, 'phone': to['phone']}, 'reply_to': None,
                'text': phone_text, 'files': [], 'caption': None, 'scope': scope}
    chat, reply_id = target(c, p, account, to['chat_id'], value['reply_to'])
    recipient.update(chat_id=to['chat_id'], provider_chat=chat['provider_chat'])
    # A plain caption that fits rides on the first file; anything else (rich text, a reply) is its own last step.
    caption = bool(text and value['files'] and value['format'] == 'plain' and not value['reply_to']
                   and len(text) <= CAPTION_LIMIT)
    files = [_file(store, c, p, account, to['chat_id'], item, text if caption and not index else '')
             for index, item in enumerate(value['files'])]
    draft = None if not text or caption else _text(reviewed(c, p, account, to['chat_id'], text, value['reply_to'],
                                                             value['format'], False, False, chat, reply_id))
    return {'target': recipient, 'reply_to': {'message_id': value['reply_to'], 'provider_id': reply_id}
            if value['reply_to'] else None, 'text': draft, 'files': files,
            'caption': None if not text or not value['files'] else 'with_first_file' if caption else 'separate_text',
            'scope': scope}


def _comparable(resolution):
    """The previewed facts a token binds: per-scope digests are recomputed at execution, never compared."""
    strip = lambda item: {k: v for k, v in item.items() if k != 'digest'} if isinstance(item, dict) else item
    return {'target': resolution['target'], 'reply_to': resolution['reply_to'], 'text': strip(resolution['text']),
            'files': [strip(item) for item in resolution['files']], 'scope': resolution['scope']}


def _steps(resolution):
    if resolution['target'].get('opens_chat'): steps = [{'name': 'phone', 'kind': 'phone'}]
    else:
        steps = [{'name': f'file{index + 1}', 'kind': 'file'} for index in range(len(resolution['files']))]
        if resolution['text'] is not None: steps.append({'name': 'text', 'kind': 'text'})
    return [{**step, 'state': 'pending', 'handle': None} for step in steps]


def _visible(resolution):
    """The owner-visible preview: recipient, text, warnings, links, reply target, files and caption handling."""
    text = resolution['text']
    return serial({'recipient': {k: v for k, v in resolution['target'].items() if k != 'provider_chat'},
        'reply_to': resolution['reply_to'] and resolution['reply_to']['message_id'],
        'text': text and {k: v for k, v in text.items() if k != 'digest'},
        'files': [{k: item[k] for k in ('name', 'bytes', 'sha256', 'mime', 'mode', 'caption')} for item in resolution['files']],
        'caption': resolution['caption'], 'steps': [step['name'] for step in _steps(resolution)],
        'owner_confirmation': 'required_before_send', 'owner_approval_verified_by_server': False})


def _scrubbed(resolution, sent=None):
    """The resolution without text: all of it, or (`sent`: file indexes) only the captions already delivered."""
    if sent is not None:
        return {**resolution, 'files': [{**item, 'caption': ''} if index in sent else item
                                        for index, item in enumerate(resolution['files'])]}
    return {**resolution, 'text': None, 'files': [{**item, 'caption': ''} for item in resolution['files']]}


def save_preview(c, p, value, resolution, lineage=None):
    # Unbound previews age out 7 days past expiry (PR Deletes: line); bound ones keep their intent's replay.
    c.execute('''DELETE FROM send_previews WHERE ctid IN (SELECT ctid FROM send_previews WHERE intent IS NULL
        AND expires_at<clock_timestamp()-interval '7 days' LIMIT 100)''')
    token = secrets.token_urlsafe(32)
    # A preview_stale replacement keeps the lineage of the preview it replaces: one lineage binds one intent.
    c.execute('''INSERT INTO send_previews(token_hash,principal,key_id,tool,lineage,arguments,resolution,expires_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+%s*interval '1 second')''',
              (_hash(token), p['id'], p['key_id'], TOOL, lineage or _hash(token), Jsonb(value), Jsonb(resolution),
               PREVIEW_TTL_S))
    return agent_result('previewed', preview=_visible(resolution), confirm_token=token, expires_in_s=PREVIEW_TTL_S,
                        hint='Show this preview to the owner and get their exact confirmation; then call messages_send '
                             'with this confirm_token. The token is not the owner\'s confirmation.')


def _locked(c, key):
    if not c.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS ok', (key,)).fetchone()['ok']:
        raise _error('in_progress', 409)


def _bind(c, p, request_id, token_hash, value, resolution):
    parent = request_id or 'token:' + token_hash
    return c.execute('''INSERT INTO send_intents(principal,key_id,tool,request_id,token_hash,parent_key,request_hash,
        plan,steps) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (p['id'], p['key_id'], TOOL, request_id, token_hash, parent, _hash(public(value)),
         Jsonb({'value': value, 'resolution': resolution}), Jsonb(_steps(resolution)))).fetchone()['id']


def _bare(arguments):
    return not set(arguments) - set(CONTROL)


def _preview_required(value):
    return _error('preview_required', 409, next=next_call(TOOL, {**_callable(value), 'dry_run': True}),
                  hint='This send needs a preview: call with dry_run true, confirm with the owner, then send the token.')


def admit(store, c, p, arguments):
    """The intent this call acts on, bound before any effect; or an error to commit first (`preview_stale`)."""
    request_id, token = arguments.get('request_id'), arguments.get('confirm_token')
    open_chat, files = arguments.get('allow_open_chat') is not False, arguments.get('allow_files') is not False
    # Control fields alone replay (or resume) the intent of that key; a payload sent with them must equal it.
    value = None if _bare(arguments) and (token is not None or request_id is not None) else payload(arguments)
    if request_id is not None and not is_request_id(request_id): raise _invalid('request_id', 'pattern')
    if token is None and request_id is None:
        failure = missing_request_id(TOOL, _callable({k: v for k, v in arguments.items() if k != 'wait_s'}))
        raise _error(failure['error'], 400, field_errors=failure['field_errors'], hint=failure['hint'], next=failure['next'])
    if request_id is not None: _locked(c, f'send-intent:{p["id"]}:{request_id}')
    bound = c.execute('SELECT * FROM send_intents WHERE principal=%s AND request_id=%s',
                      (p['id'], request_id)).fetchone() if request_id is not None else None
    if token is not None:
        if not isinstance(token, str) or not 1 <= len(token) <= 256: raise _error('invalid_confirm_token', 409)
        token_hash = _hash(token)
        found = c.execute('SELECT lineage FROM send_previews WHERE token_hash=%s', (token_hash,)).fetchone()
        if found: _locked(c, 'send-preview:' + found['lineage'])
        record = found and c.execute('''SELECT *,expires_at<=clock_timestamp() AS expired FROM send_previews
            WHERE token_hash=%s''', (token_hash,)).fetchone()
        # A token is its issuing key's: another key of the same principal cannot consume it.
        if not record or record['principal'] != p['id'] or record['key_id'] != p['key_id'] or record['tool'] != TOOL:
            raise _error('invalid_confirm_token', 409)
        if record['intent'] is not None:
            # A consumed token (or one of its lineage) is its intent's key: a new request_id with it is ignored,
            # another intent's is a conflict, and so is a payload that differs from the bound one.
            if bound is not None and bound['id'] != record['intent']: raise _error('idempotency_conflict', 409)
            if value is not None and c.execute('SELECT request_hash FROM send_intents WHERE id=%s',
                                               (record['intent'],)).fetchone()['request_hash'] != _hash(public(value)):
                raise _error('idempotency_conflict', 409)
            return record['intent']
        if value is not None and public(value) != public(record['arguments']): raise _error('idempotency_conflict', 409)
        if record['expired']:
            raise _error('preview_expired', 409, next=next_call(TOOL, {**_callable(record['arguments']), 'dry_run': True}))
        value, old = record['arguments'], record['resolution']
        fresh = resolve(store, c, p, value, old['text'] if old['target'].get('opens_chat') else None, open_chat, files)
        before, after = _comparable(old), _comparable(fresh)
        changed = [name for name in COMPARED if before[name] != after[name]]
        if changed:
            stale = save_preview(c, p, value, fresh, record['lineage'])
            return _error('preview_stale', 409, changed=changed, preview=stale['preview'],
                          confirm_token=stale['confirm_token'])
        if bound is not None: raise _error('idempotency_conflict', 409)
        intent = _bind(c, p, request_id, token_hash, value, fresh)
        # Every preview of the lineage now names this intent, and none keeps text or captions.
        for row in c.execute('SELECT token_hash,resolution FROM send_previews WHERE lineage=%s AND intent IS NULL',
                             (record['lineage'],)).fetchall():
            c.execute("UPDATE send_previews SET intent=%s,arguments=arguments-'text',resolution=%s "
                      "WHERE token_hash=%s", (intent, Jsonb(_scrubbed(row['resolution'])), row['token_hash']))
        return intent
    if value is None and bound is None: payload(arguments)  # Nothing bound to this key: name the missing field.
    if bound is not None:
        if value is not None and bound['request_hash'] != _hash(public(value)) or bound['tool'] != TOOL: raise _error('idempotency_conflict', 409)
        return bound['id']
    if value['format'] != 'plain': raise _preview_required(value)
    fresh = resolve(store, c, p, value, open_chat=open_chat, files=files)
    if fresh is None: raise _preview_required(value)
    return _bind(c, p, request_id, None, value, fresh)


def _restart(c, row):
    """resume:true on a resumable failure: the next attempt re-runs failed and unsent steps under new child keys."""
    steps = [step if step['state'] == 'succeeded' else {**step, 'state': 'pending', 'handle': None, 'error': None}
             for step in row['steps']]
    return c.execute('''UPDATE send_intents SET attempt=attempt+1,state='running',result=NULL,steps=%s,
        updated_at=clock_timestamp() WHERE id=%s RETURNING *''', (Jsonb(steps), row['id'])).fetchone()


def _key(p, row, step):
    return CHILD_KEY_PREFIX + child_key(p['id'], TOOL, row['parent_key'], step['name'], row['attempt'])


def _start(store, c, p, row, step):
    """The step's handle. The previewed recipient binds it: the row the low-level route wrote must name the bound
    provider chat (and reply), else `identity_changed` and a savepoint undoes the row. Checking the written row,
    not a read before it, also holds when a remap commits between two statements (READ COMMITTED)."""
    with c.transaction():
        handle = _enqueue(store, c, p, row, step)
        if step['kind'] == 'phone': return handle
        resolution = row['plan']['resolution']
        if 'send_id' in handle:
            written = c.execute('SELECT provider_chat,reply_provider_id FROM outgoing_messages WHERE id=%s',
                                (handle['send_id'],)).fetchone()
            same = written['reply_provider_id'] == (resolution['reply_to'] or {}).get('provider_id')
        else:
            written = c.execute("SELECT payload->>'provider_chat' AS provider_chat FROM messenger_commands WHERE id=%s",
                                (handle['operation_id'],)).fetchone()
            # A file is not sent as a reply, yet a bound reply that now maps elsewhere is a changed preview too.
            reply = resolution['reply_to']
            source = reply and c.execute('SELECT provider_id FROM messages WHERE id=%s AND NOT deleted',
                                         (reply['message_id'],)).fetchone()
            same = not reply or bool(source) and source['provider_id'] == reply['provider_id']
        if not same or written['provider_chat'] != resolution['target']['provider_chat']:
            raise _error('identity_changed', 409)
    return handle


def _enqueue(store, c, p, row, step):
    from .commands import enqueue as operation
    from .outgoing import enqueue as send
    resolution, value = row['plan']['resolution'], row['plan']['value']
    target, key = resolution['target'], _key(p, row, step)
    if step['kind'] == 'text':
        rich = value['format'] != 'plain'
        return {'send_id': send(c, p, target['account'], target['chat_id'], value['text'], key, value['reply_to'],
                                value['format'], resolution['text']['digest'] if rich else '', child=True)['send_id']}
    if step['kind'] == 'file':
        item = resolution['files'][int(step['name'][4:]) - 1]
        parameters = {item['source']: item['id'], 'sha256': item['sha256'], 'mode': item['mode'], 'caption': item['caption']}
        from .commands import preview
        digest = preview(store, c, p, target['account'], 'send_file', target['chat_id'], parameters=parameters)['preview_digest']
        return {'operation_id': operation(store, c, p, target['account'], 'send_file', target['chat_id'],
                                          parameters=parameters, request_id=key, preview_digest=digest,
                                          child=True)['operation_id']}
    from .phone_send import send_begin
    begun = send_begin(store, c, p, target['account'], target['phone'][1:], value['text'], key, '', value['format'],
                       resolution['text']['digest'], False, False, child=True)
    return {'send_id': begun['sent']['send_id']} if 'sent' in begun else {'operation_id': begun['operation_id']}


def _observe(store, c, p, row, step):
    """(state, receipt or error code) of a started step; a phone step's chat opening hands over to its send."""
    from .commands import status as operation
    from .outgoing import status as send
    handle = step['handle']
    if 'send_id' in handle:
        receipt = send(c, p, handle['send_id'])
        status = receipt['status']
        done = {'send_id': receipt['send_id'], 'message_id': receipt['message_id'],
                'provider_message_id': receipt['provider_message_id']}
    else:
        receipt = operation(store, c, p, handle['operation_id'])
        status = receipt['status']
        done = {'operation_id': receipt['operation_id']}
        if step['kind'] == 'phone' and status == 'succeeded':
            from .phone_send import send_finish
            value, resolution = row['plan']['value'], row['plan']['resolution']
            target = resolution['target']
            try:
                sent = send_finish(store, c, p, target['account'], target['phone'][1:], handle['operation_id'],
                                   value['text'], _key(p, row, step), '', value['format'], resolution['text']['digest'],
                                   False, False, child=True)
            except APIError as error:
                return 'failed', error.code
            if 'send_id' not in sent or not sent['send_id']: return 'pending', None
            step['handle'] = {**handle, 'send_id': sent['send_id']}
            return _observe(store, c, p, row, step)
    if status in PENDING: return 'pending', None
    if status in ('sent', 'succeeded'): return 'succeeded', done
    if status in DEFINITE: return 'failed', receipt.get('error_code') or status
    return 'unknown', None


def _steps_public(steps):
    return [{k: v for k, v in step.items() if k in ('name', 'kind', 'state', 'handle', 'receipt', 'error') and v is not None}
            for step in steps]


def _finish(c, row, steps, state, reason=None):
    operation = {'module': 'messaging', 'id': row['id']}
    if state == 'succeeded':
        result = agent_result('succeeded', detail_state='sent', request_id=row['request_id'], operation=operation,
                              steps=_steps_public(steps))
    elif state == 'failed':
        result = agent_result('failed', detail_state=reason, request_id=row['request_id'], operation=operation,
                              resumable=True, reason=reason, steps=_steps_public(steps),
                              hint='Nothing after the failed step was sent; repeat with resume true to send the rest.')
    else:
        result = agent_result('unknown', detail_state='uncertain', request_id=row['request_id'], operation=operation,
                              steps=_steps_public(steps),
                              hint='A step may have been delivered: check the chat history before any new send.')
    plan = row['plan']
    # Text stays only while a resume may still send it: an unsent text or phone step, or an unsent file's caption.
    sent = None if state != 'failed' else \
        {int(s['name'][4:]) - 1 for s in steps if s['kind'] == 'file' and s['state'] == 'succeeded'}
    resolution = _scrubbed(plan['resolution'], sent)
    if state != 'failed' or not any(s['kind'] != 'file' and s['state'] != 'succeeded' for s in steps):
        plan = {'value': {**plan['value'], 'text': ''}, 'resolution': {**resolution, 'text': None}}
    else:
        plan = {**plan, 'resolution': resolution}
    c.execute('''UPDATE send_intents SET state=%s,result=%s,steps=%s,plan=%s,updated_at=clock_timestamp()
        WHERE id=%s''', (state, Jsonb(result), Jsonb(steps), Jsonb(plan), row['id']))
    # Every file is sent: staged inline bytes go now (`release`), after this commits. Any upload_id may be one (a
    # followed `next` names it plainly); release keeps only `stage`'s, by their child key.
    staged = [item['upload_id'] for item in plan['value']['files'] if 'upload_id' in item]
    return {'result': serial(result), **({'release': staged} if state == 'succeeded' and staged else {})}


def advance(store, c, p, intent_id, *, resume=False, first=False, open_chat=True, files=True):
    """Move one intent as far as it goes without waiting: {'result'} when final, else {'wait', 'result'}."""
    try:
        row = c.execute('SELECT * FROM send_intents WHERE id=%s AND principal=%s FOR UPDATE NOWAIT',
                        (intent_id, p['id'])).fetchone()
    except LockNotAvailable:
        raise _error('in_progress', 409) from None
    if not row: raise APIError('not_found', 404)
    if row['key_id'] != p['key_id']: raise _error('idempotency_conflict', 409)
    if resume and row['state'] == 'failed' and (row['result'] or {}).get('resumable'): row = _restart(c, row)
    if row['state'] != 'running': return {'result': row['result']}
    steps = row['steps']
    for step in steps:
        if step['state'] == 'succeeded': continue
        if step['handle'] is None:
            # The gateway's grants of this call bound every unsent step, also of an intent bound earlier.
            if step['kind'] == 'phone' and not open_chat or step['kind'] == 'file' and not files:
                raise APIError('forbidden', 403)
            try:
                step['handle'] = _start(store, c, p, row, step)
            except APIError as error:
                if error.code == 'sender_not_ready': return _finish(c, row, steps, 'failed', 'account_offline')
                # Nothing started yet in this call's first transaction: the low-level refusal, and no record.
                if first and row['attempt'] == 1 and all(s['handle'] is None for s in steps): raise
                step.update(state='failed', error=error.code)
                return _finish(c, row, steps, 'failed', 'step_failed')
        state, detail = _observe(store, c, p, row, step)
        if state == 'pending':
            step['state'] = 'running'
            c.execute('UPDATE send_intents SET steps=%s,updated_at=clock_timestamp() WHERE id=%s', (Jsonb(steps), row['id']))
            handle = step['handle']
            wait = {'kind': 'send', 'id': handle['send_id']} if 'send_id' in handle else \
                {'kind': 'operation', 'id': handle['operation_id']}
            return {'wait': wait, 'intent': row['id'], 'result': serial(agent_result(
                'running', request_id=row['request_id'], operation={'module': 'messaging', 'id': row['id']},
                steps=_steps_public(steps)))}
        if state == 'succeeded':
            step.update(state='succeeded', receipt=detail)
            continue
        if state == 'failed':
            step.update(state='failed', error=detail)
            return _finish(c, row, steps, 'failed', 'step_failed')
        step['state'] = 'unknown'
        return _finish(c, row, steps, 'unknown')
    return _finish(c, row, steps, 'succeeded')


def _decorate(result, arguments):
    """`next` for a running or resumable answer: the same call (the caller's own key), resume for a failure."""
    control = {k: arguments[k] for k in ('request_id', 'confirm_token') if arguments.get(k) is not None}
    if result['state'] == 'running':
        return {**result, 'next': next_call(TOOL, {**control, 'wait_s': 10})}
    if result['state'] == 'failed' and result.get('resumable'):
        return {**result, 'next': next_call(TOOL, {**control, 'resume': True})}
    return result


def _inline_next(store, token, arguments, error):
    """Over the inline cap: the prepare call that takes that file instead, for the recipient this call names."""
    from .resolve_target import resolve_target
    facts = error.inline_file
    try:
        with store.authorized(token, 'preview_message') as (c, p):
            target = resolve_target(store, c, p, arguments.get('to') or arguments.get('reply_to') or '', 'message',
                                    arguments.get('account') or '')['target']
    except APIError:
        return error  # No recipient this key may send to: the field error alone.
    if target.get('opens_chat') or not target.get('chat_id'): return error
    error.next = next_call(UPLOAD_TOOL, {'account': target['account'], 'chat_id': target['chat_id'],
                                         'filename': facts['name'], 'mime': facts['mime'], 'bytes': facts['bytes'],
                                         'sha256': facts['sha256'], 'request_id': PLACEHOLDER_REQUEST_ID})
    return error


def stage(store, token, arguments):
    """Inline files of a dry run or a request_id call become this key's uploads (ANYM-547), each prepared and then
    written in its own transaction, like messages_prepare_file_upload and one chunk, under a child key of the call:
    a repeat with the same request_id reuses them. A confirm_token call only compares them with its preview."""
    from .resolve_target import resolve_target
    from .uploads import chunk, prepare
    try: value = payload(arguments)
    except APIError as error:
        if getattr(error, 'inline_file', None) is None: raise
        raise _inline_next(store, token, arguments, error) from None
    indexes = [index for index, item in enumerate(value['files']) if 'base64' in item]
    request_id = arguments.get('request_id')
    if not indexes or arguments.get('confirm_token') is not None: return arguments
    if not arguments.get('dry_run') and not is_request_id(request_id): return arguments  # admit names the key
    if not arguments.get('dry_run'):
        # A bound request_id replays its intent (admit compares the payload by its facts): nothing is resolved or
        # staged again, so a recipient that changed since cannot fail the replay.
        with store.authorized(token, 'send_message') as (c, p):
            if c.execute('SELECT 1 FROM send_intents WHERE principal=%s AND request_id=%s',
                         (p['id'], request_id)).fetchone(): return arguments
    with store.authorized(token, 'prepare_file_upload') as (c, p):
        target = resolve_target(store, c, p, value['to'] or value['reply_to'], 'message', value['account'])['target']
        principal = p['id']
    if target.get('opens_chat'):
        raise _invalid('files', 'max_items', 'a phone without a chat takes text only: send it, then the files')
    parent = request_id
    if arguments.get('dry_run') and not is_request_id(request_id):
        # Repeated dry runs of one payload reuse their uploads (a key holds at most 4 active ones). A half-hour window
        # keeps a reused upload over 30 minutes from its 1 hour expiry, longer than a preview lives (PREVIEW_TTL_S).
        parent = 'dry-run:' + _hash({'account': target['account'], 'chat_id': target['chat_id'],
                                     'files': public(value)['files'], 'window': int(time.time() // 1800)})
    files = list(value['files'])
    for index in indexes:
        item, facts = files[index], _facts(files[index])
        for attempt in range(1, STAGE_ATTEMPTS + 1):
            # A released or expired upload is never reused: the next attempt stages the bytes afresh.
            key = CHILD_KEY_PREFIX + child_key(principal, TOOL, parent, f'inline{index + 1}', attempt)
            with store.authorized(token, 'prepare_file_upload') as (c, p):
                row = prepare(store, c, p, target['account'], target['chat_id'], facts['name'], facts['mime'],
                              facts['bytes'], facts['sha256'], key, child=True)
            if row['status'] != 'expired': break
        else:
            raise APIError('upload_limit', 409)
        if row['status'] == 'uploading' and row['offset'] == 0:
            with store.authorized(token, 'upload_file_chunk') as (c, p):
                chunk(store, c, p, row['upload_id'], 0, item['base64'])
        files[index] = {'upload_id': row['upload_id'], 'inline': facts, **({'mode': item['mode']} if 'mode' in item else {})}
    return {**arguments, 'files': files}


def release(store, token, upload_ids):
    """A sent intent's staged inline uploads (a child key: only `stage` makes one) are deleted, not left to expire,
    unless a live preview or an unfinished intent of this principal still names one: that one expires (ANYM-547)."""
    from .uploads import _lifecycle_lock, reap
    for upload_id in sorted(set(upload_ids)):
        # One upload per transaction, under its lifecycle lock: a preview or intent resolving it holds that lock
        # until it commits, so this sees its row or it finds the upload expired.
        with store.authorized(token, 'prepare_file_upload') as (c, p):
            _lifecycle_lock(c, upload_id)
            named = Jsonb([{'upload_id': upload_id}])
            c.execute('''UPDATE file_uploads u SET expires_at=least(u.expires_at,clock_timestamp())
                WHERE u.id=%s AND u.principal=%s AND u.key_id=%s AND u.state<>'expired' AND starts_with(u.request_id,%s)
                AND NOT EXISTS(SELECT 1 FROM send_previews v WHERE v.principal=u.principal AND v.intent IS NULL
                    AND v.expires_at>clock_timestamp() AND v.arguments->'files' @> %s)
                AND NOT EXISTS(SELECT 1 FROM send_intents i WHERE i.principal=u.principal AND i.state<>'succeeded'
                    AND i.plan->'value'->'files' @> %s)''',
                      (upload_id, p['id'], p['key_id'], CHILD_KEY_PREFIX, named, named))
    with store.authorized(token, 'prepare_file_upload') as (c, p):
        reap(store, c, upload_ids)


async def send(store, token, arguments):
    """The tool: dry_run previews; otherwise admit, advance and wait through the shared wait admission."""
    from .send_wait import MAX_WAIT_S, validate_wait_s, wait_for_operation, wait_for_send
    arguments = {k: v for k, v in arguments.items() if v is not None}
    wait_s = validate_wait_s(arguments.get('wait_s', 0))
    files = arguments.get('files')
    if isinstance(files, list):
        for index, item in enumerate(files):
            # The staged form is the server's own: a caller never names an inline file by it.
            if isinstance(item, dict) and 'inline' in item: raise _invalid(f'files.{index}.inline', 'unknown_field')
    run = asyncio.to_thread
    given = arguments
    if isinstance(files, list) and any(isinstance(item, dict) and 'base64' in item for item in files):
        arguments = await run(stage, store, token, arguments)
    if arguments.get('dry_run') or arguments.get('confirm_token') is None and not _bare(arguments): payload(arguments)
    if arguments.get('dry_run'):
        try: staged = await run(store.messages_send_preview, token, arguments)
        except APIError as error:
            # Another preview's send released the shared staged upload in between (`release`): stage afresh, once.
            if error.code != 'attachment_unavailable' or arguments is given: raise
            arguments = await run(stage, store, token, given)
            staged = await run(store.messages_send_preview, token, arguments)
        if 'phone' not in staged: return staged
        from . import phone_send
        draft = await phone_send.preview(store, token, staged['phone'])
        return await run(store.messages_send_preview, token, arguments, draft)
    answer = await run(store.messages_send_execute, token, arguments)
    if 'raise' in answer: raise answer['raise']
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while 'wait' in answer:
        remaining = min(deadline - loop.time(), MAX_WAIT_S)
        if remaining <= 0: break
        reader = wait_for_send if answer['wait']['kind'] == 'send' else wait_for_operation
        try:
            await reader(store, token, answer['wait']['id'], remaining)
        except APIError as error:
            if error.status in (401, 403, 404): raise
            break  # Busy or timed out: the record keeps the step; the answer says running.
        answer = await run(store.messages_send_advance, token, answer['intent'], arguments)
    if answer.get('release'):
        try: await run(release, store, token, answer['release'])
        except Exception: pass  # The send succeeded; unreleased staged bytes expire like any upload.
    return _decorate(answer['result'], arguments)
