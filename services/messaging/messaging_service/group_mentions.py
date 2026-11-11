"""ANYM-928: an agent's message in a Telegram or WhatsApp group @-mentions members.

`mentions` is up to 20 `{sender, offset, length}`: an archive sender ID and a span of the source text counted in UTF-16
code units, as formatting entities are. The request form joins the send's render policy; the provider form (the span
in the visible text on Telegram with the user ID, the `@<user part>` that replaces it in the WhatsApp wire text with the
JID) joins the render artifact. The preview_digest binds both, so adding, removing or moving a mention invalidates it,
and a send without mentions keeps every digest, request hash, duplicate match and stored row of before.

Membership: the chat's current participants (a list_participants read the server makes before a preview when it holds
none from the last 10 minutes); only when no such read is held does the archive sighting in that chat
(`sender_profiles`) decide. Every refusal comes before the queue: nothing is queued on any error.
"""
import asyncio
import hashlib
import logging
import re
import time

from .store import APIError

log = logging.getLogger(__name__)

MAX_MENTIONS = 20
PREVIEW_FRESH_S = 600  # a preview reads participants itself when it holds no read of the chat this recent
SEND_FRESH_S = 86400  # the send checks against the newest read of the last day: the owner may confirm later
READ_WAIT_S = 12
PAGE_LIMIT = 100
_now = time.time
TELEGRAM_USER = re.compile(r'[1-9][0-9]{0,19}')
WHATSAPP_USER = re.compile(r'([0-9]{1,20})(?::[0-9]{1,4})?@(c\.us|lid)')
TELEGRAM_USERNAME = re.compile(r'(?<![A-Za-z0-9_@/.])@([A-Za-z][A-Za-z0-9_]{3,31})(?![A-Za-z0-9_])')
WHATSAPP_PLAIN_AT = re.compile(r'(?<![A-Za-z0-9_@])@[0-9]{5,20}(?![0-9])')
SHAPE = '{sender: archive sender ID, offset: UTF-16 units from 0, length: at least 1}'

# In the argument's schema: the preview_message and send_message descriptions are at their cap.
DESCRIPTION = ('Group chat_id sends: @-mention members, up to 20 {sender, offset, length}. sender is an archive '
    'sender ID (search_people sender_id, a message sender); offset and length count UTF-16 code units of text, as '
    'formatting entities do, and mark the span shown as the mention (for example "@Anna"). The member is notified as '
    'when the owner mentions them in the app. Telegram links the span to the user; WhatsApp (native adapter) replaces '
    'it with @<number> and the phone shows the name. preview_message checks membership (a list_participants read, '
    'else the archive: membership_basis) and returns mentions_notified, so show it to the owner; its preview_digest '
    'binds the mentions. Errors queue nothing: mention_out_of_range, mention_overlap, mentions_not_group, '
    'mention_not_member, mention_unresolvable, mentions_unsupported, mentions_conflict (with menu_choice).')


def _refusal(code, index=None, reason='format', expected=SHAPE, text_units=None, next_call=None):
    from .field_errors import refusal
    failure = refusal(code, [('mentions' if index is None else f'mentions/{index}', reason, expected)])
    for name, value in (('index', index), ('text_utf16_length', text_units), ('next_call', next_call)):
        if value is not None: setattr(failure, name, value)
    return failure


def normalized(mentions):
    """The request form, or None when omitted or empty; a malformed list is invalid_parameters naming its item."""
    if mentions is None: return None
    if not isinstance(mentions, list): raise _refusal('invalid_parameters', reason='type', expected='a list of ' + SHAPE)
    if not mentions: return None
    if len(mentions) > MAX_MENTIONS:
        raise _refusal('invalid_parameters', reason='max_items', expected=f'at most {MAX_MENTIONS} mentions')
    result = []
    for index, item in enumerate(mentions):
        if (not isinstance(item, dict) or set(item) != {'sender', 'offset', 'length'} or
                not isinstance(item['sender'], str) or not 1 <= len(item['sender']) <= 160 or
                type(item['offset']) is not int or type(item['length']) is not int or
                not 0 <= item['offset'] <= 16000 or not 1 <= item['length'] <= 16000):
            raise _refusal('invalid_parameters', index)
        result.append({'sender': item['sender'], 'offset': item['offset'], 'length': item['length']})
    return result


def conflicts(mentions, menu_choice):
    """A menu answer is a structured reply with the archived label: it mentions nobody."""
    if mentions and menu_choice is not None:
        raise _refusal('mentions_conflict', reason='enum', expected='omitted beside menu_choice')


def chat_id_only(arguments, phone, username):
    """A phone or username recipient is a private chat: mentions are for groups. An empty list is the omitted one."""
    if (phone is None and username is None) or 'mentions' not in arguments: return
    if arguments.pop('mentions') not in (None, []):
        raise _refusal('mentions_not_group', reason='enum', expected='a group chat_id')


def _boundaries(text):
    """UTF-16 offset -> Python index, at code point boundaries only."""
    result, units = {0: 0}, 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        result[units] = index + 1
    return result


def check_spans(text, mentions):
    """Each span lies inside the source text on code point boundaries, without outer whitespace or overlap."""
    bounds = _boundaries(text); units = max(bounds)
    for index, m in enumerate(mentions):
        end = m['offset'] + m['length']
        if end > units:
            raise _refusal('mention_out_of_range', index, 'maximum', 'offset + length within text', units)
        if m['offset'] not in bounds or end not in bounds:
            raise _refusal('mention_out_of_range', index, 'format', 'a span that does not split a surrogate pair', units)
        span = text[bounds[m['offset']]:bounds[end]]
        if span != span.strip():
            raise _refusal('mention_out_of_range', index, 'format', 'a span without outer whitespace', units)
    order = sorted(range(len(mentions)), key=lambda i: (mentions[i]['offset'], i))
    for first, second in zip(order, order[1:]):
        if mentions[second]['offset'] < mentions[first]['offset'] + mentions[first]['length']:
            raise _refusal('mention_overlap', max(first, second), 'unique', 'spans that do not overlap', units)
    return bounds


def _units(text): return len(text.encode('utf-16-le')) // 2


def _place(platform, format, text, link_preview, allow_lossy, artifact, mentions, bounds):
    """Each mention's (visible offset, wire offset) in UTF-16 units, checked against the reviewed artifact."""
    from .formatting import render_message
    units = max(bounds); rendered, wire = artifact['rendered_text'], artifact['provider_text']
    mark = next(chr(v) for v in range(0xE000, 0xF900) if chr(v) not in text)
    places = []
    for index, m in enumerate(mentions):
        start, end = bounds[m['offset']], bounds[m['offset'] + m['length']]
        span = text[start:end]
        overlap = _refusal('mention_overlap', index, 'format', 'a span of plain text, outside links and code', units)
        if format == 'plain':
            seen, wire_seen = m['offset'], m['offset']
        else:
            # The span alone becomes one private-use mark: everything before it renders as in the reviewed artifact,
            # so the mark's place is the span's place. A span the renderer would change is refused below.
            try: shadow = render_message(platform, text[:start] + mark + text[end:], format, link_preview, allow_lossy)
            except APIError: raise overlap from None
            if shadow['rendered_text'].count(mark) != 1 or shadow['provider_text'].count(mark) != 1: raise overlap
            seen = _units(shadow['rendered_text'][:shadow['rendered_text'].index(mark)])
            wire_seen = _units(shadow['provider_text'][:shadow['provider_text'].index(mark)])
        length = m['length']
        if (rendered.encode('utf-16-le')[seen * 2:(seen + length) * 2].decode('utf-16-le', 'replace') != span or
                wire.encode('utf-16-le')[wire_seen * 2:(wire_seen + length) * 2].decode('utf-16-le', 'replace') != span):
            raise overlap
        for entity in artifact['entities']:
            e_start, e_end = entity['offset'], entity['offset'] + entity['length']
            if e_end <= seen or seen + length <= e_start: continue
            inside, around = e_start <= seen and seen + length <= e_end, seen <= e_start and e_end <= seen + length
            if entity['type'] in ('code', 'pre', 'text_link') or not (inside or around): raise overlap
        places.append((seen, wire_seen))
    return places


def _group(chat):
    kind = chat.get('chat_type')
    return kind == 'group' or kind is None and chat['platform'] == 'whatsapp' and chat['provider_chat'].endswith('@g.us')


def _next_call(account, chat_id):
    return {'tool': 'list_participants', 'arguments': {'account': account, 'chat_id': chat_id}}


def _member_id(platform, sender):
    """The provider user a sender ID names, or None when it cannot be mentioned (a channel, a group, a malformed ID)."""
    if platform == 'telegram': return sender if TELEGRAM_USER.fullmatch(sender) else None
    match = WHATSAPP_USER.fullmatch(sender)
    return f'{match.group(1)}@{match.group(2)}' if match else None


def participants_read(c, p, account, chat_id, fresh_s):
    """The newest first-page list_participants read of this chat by this principal: ({id: name}, complete) or None."""
    rows = c.execute('''SELECT payload,result FROM messenger_commands WHERE principal=%s AND account=%s AND chat=%s
        AND operation='list_participants' AND status='succeeded' AND created_at>clock_timestamp()-make_interval(secs=>%s)
        ORDER BY created_at DESC LIMIT 5''', (p['id'], account, chat_id, fresh_s)).fetchall()
    for row in rows:
        parameters = (row['payload'] or {}).get('parameters') or {}
        data = (row['result'] or {}).get('data')
        if parameters.get('offset', 0) != 0 or not isinstance(data, dict) or data.get('coverage') == 'unavailable': continue
        items = data.get('participants', data.get('items'))
        if not isinstance(items, list): continue
        members = {}
        for item in items:
            if isinstance(item, dict) and isinstance(item.get('id'), str):
                name = ' '.join(v for v in (item.get('first_name'), item.get('last_name')) if isinstance(v, str) and v)
                members[item['id']] = name or None
        return members, data.get('next_offset') is None and data.get('has_more') is not True
    return None


def _membership(c, p, chat, members, fresh_s):
    """(basis, {member: display name or None}) of the members found; one not in the dict is not a member."""
    read = participants_read(c, p, chat['account'], chat['id'], fresh_s)
    found, rest, basis = {}, list(members), 'participants'
    if read is not None:
        listed, complete = read
        found = {m: listed[m] for m in members if m in listed}
        rest = [m for m in members if m not in found]
        if not rest or complete: rest = []
        else: basis = 'archive'
    else: basis = 'archive'
    names = {r['sender']: r['display_name'] or None for r in c.execute('''SELECT sender,display_name FROM sender_profiles
        WHERE account=%s AND chat=%s AND sender=ANY(%s)''', (chat['account'], chat['id'], list(members))).fetchall()}
    for member in members:
        if member in found and names.get(member): found[member] = names[member]
    for member in rest:
        if member in names: found[member] = names[member]
    return basis, found


def apply(c, p, chat, text, format, link_preview, allow_lossy, artifact, mentions, fresh_s):
    """The artifact with its provider mentions and the preview's notice fields; every refusal queues nothing."""
    from .whatsapp_capabilities import account_provider
    if not _group(chat): raise _refusal('mentions_not_group', reason='enum', expected='a group chat_id')
    platform, account = chat['platform'], chat['account']
    if platform == 'whatsapp' and account_provider(c, account, 'whatsapp') != 'whatsmeow':
        raise _refusal('mentions_unsupported', reason='enum', expected='omitted: only the native WhatsApp adapter mentions')
    bounds = check_spans(text, mentions)
    places = _place(platform, format, text, link_preview, allow_lossy, artifact, mentions, bounds)
    members = []
    for index, m in enumerate(mentions):
        member = _member_id(platform, m['sender'])
        if member is None:
            raise _refusal('mention_unresolvable', index, 'format', 'a person in this chat',
                           next_call=_next_call(account, chat['id']))
        members.append(member)
    basis, found = _membership(c, p, chat, list(dict.fromkeys(members)), fresh_s)
    for index, member in enumerate(members):
        if member not in found:
            raise _refusal('mention_not_member', index, 'enum', 'a current member of this chat',
                           next_call=_next_call(account, chat['id']))
    notified = [{'sender': m['sender'], 'display_name': found[member], 'span_text': artifact['rendered_text'][
        bounds_text(artifact['rendered_text'], seen, m['length'])]} for m, member, (seen, _) in zip(mentions, members, places)]
    if platform == 'telegram':
        provider = [{'offset': seen, 'length': m['length'], 'user_id': member}
                    for m, member, (seen, _) in zip(mentions, members, places)]
        return {**artifact, 'mentions': provider}, notified, basis
    # WhatsApp: each span becomes @<user part> in the wire text, the form a phone renders as the member's name.
    wire = artifact['provider_text']; at = _boundaries(wire)
    pieces, last, provider = [], 0, [{}] * len(mentions)
    for index in sorted(range(len(mentions)), key=lambda i: places[i][1]):
        start, end = at[places[index][1]], at[places[index][1] + mentions[index]['length']]
        before, after = wire[start - 1:start], wire[end:end + 1]
        if before.isalnum() or before in ('_', '@') or after.isdigit():
            # The phone renders @<number> as a mention only apart from a word before it and a digit after it.
            raise _refusal('mention_overlap', index, 'format', 'a span apart from a word before it or a digit after it',
                           max(bounds))
        pieces.append(wire[last:start]); last = end
        token = '@' + members[index].split('@')[0]
        provider[index] = {'offset': _units(''.join(pieces)), 'length': len(token), 'jid': members[index]}
        pieces.append(token)
    text_out = ''.join(pieces) + wire[last:]
    if _units(text_out) > 4000: raise APIError('message_too_long')
    return ({**artifact, 'provider_text': text_out, 'utf16_units': _units(text_out), 'mentions': provider,
             'provider_payload': {**artifact['provider_payload'], 'text': text_out}}, notified, basis)


def bounds_text(text, offset, length):
    """The Python slice of a UTF-16 span of text."""
    bounds = _boundaries(text)
    return slice(bounds[offset], bounds[offset + length])


def notices(platform, artifact):
    """People notified by other means: Telegram plain @usernames; a WhatsApp @<digits> that is no mention."""
    if platform == 'telegram':
        rendered, spans = artifact['rendered_text'], [*artifact['entities'], *artifact.get('mentions', [])]
        names = []
        for match in TELEGRAM_USERNAME.finditer(rendered):
            at = _units(rendered[:match.start()])
            if any(e['offset'] < at + 1 and at < e['offset'] + e['length'] and e.get('type') in (None, 'code', 'pre', 'text_link')
                   for e in spans): continue
            names.append(match.group(1))
        return {'notified_by_username': list(dict.fromkeys(names))[:MAX_MENTIONS]} if names else {}
    wire, taken = artifact['provider_text'], {m['offset'] for m in artifact.get('mentions', [])}
    if any(_units(wire[:match.start()]) not in taken for match in WHATSAPP_PLAIN_AT.finditer(wire)):
        return {'warning': 'plain_at_is_not_a_mention'}
    return {}


def dispatch_mentions(platform, artifact, policy, payload):
    """The validated provider mentions of a persisted row (dispatch never renders again); ValueError when they are not
    exactly the previewed ones."""
    requested, provider = policy.get('mentions'), artifact.get('mentions')
    if requested is None and provider is None: return payload
    if (not isinstance(requested, list) or normalized(requested) != requested or not isinstance(provider, list) or
            len(provider) != len(requested)):
        raise ValueError('invalid_persisted_mentions')
    text = artifact['rendered_text'] if platform == 'telegram' else payload['text']
    bounds, keys = _boundaries(text), {'telegram': {'offset', 'length', 'user_id'}, 'whatsapp': {'offset', 'length', 'jid'}}
    taken = []
    for item, asked in zip(provider, requested):
        if not isinstance(item, dict) or set(item) != keys[platform] or type(item['offset']) is not int or \
                type(item['length']) is not int or item['length'] < 1 or item['offset'] not in bounds or \
                item['offset'] + item['length'] not in bounds:
            raise ValueError('invalid_persisted_mentions')
        member = item.get('user_id', item.get('jid'))
        if not isinstance(member, str) or _member_id(platform, asked['sender']) != member: raise ValueError('invalid_persisted_mentions')
        if platform == 'whatsapp' and text[bounds[item['offset']]:bounds[item['offset'] + item['length']]] != '@' + member.split('@')[0]:
            raise ValueError('invalid_persisted_mentions')
        taken.append((item['offset'], item['offset'] + item['length']))
    taken.sort()
    if any(b[0] < a[1] for a, b in zip(taken, taken[1:])): raise ValueError('invalid_persisted_mentions')
    if platform == 'telegram':
        return {**payload, 'mentions': [{k: item[k] for k in ('offset', 'length', 'user_id')} for item in provider]}
    return {**payload, 'mentions': list(dict.fromkeys(item['jid'] for item in provider))}


async def telegram_entities(client, provider_chat, mentions):
    """InputMessageEntityMentionName per previewed mention. A user the session cache does not hold is looked up once
    through the chat's participants; one still unresolvable is mention_unresolvable before the send, never plain text."""
    from telethon import types, utils
    from telethon.errors import FloodError, RPCError
    from .outgoing import SendRejected

    async def resolve(user_id):
        try: return utils.get_input_user(await client.get_input_entity(types.PeerUser(int(user_id))))
        except (ValueError, TypeError): return None

    users = {m['user_id']: await resolve(m['user_id']) for m in mentions}
    if any(v is None for v in users.values()):
        try: await client.get_participants(int(provider_chat), limit=200)
        except FloodError: raise
        except (RPCError, ValueError, TypeError): pass
        for user_id in [k for k, v in users.items() if v is None]: users[user_id] = await resolve(user_id)
    if any(v is None for v in users.values()): raise SendRejected('mention_unresolvable')
    return [types.InputMessageEntityMentionName(m['offset'], m['length'], users[m['user_id']]) for m in mentions]


async def with_mentions(client, provider_chat, payload, options):
    """Telegram send options with the previewed entities and the mentions, ordered by offset. A repeated call (the
    governor may run the step before a send again) builds the same list."""
    from telethon import types
    base = [e for e in options.get('formatting_entities', []) if not isinstance(e, types.InputMessageEntityMentionName)]
    mentioned = await telegram_entities(client, provider_chat, payload['mentions'])
    options['formatting_entities'] = sorted([*base, *mentioned], key=lambda e: (e.offset, -e.length))


async def refresh(store, token, account, chat_id):
    """Before a preview with mentions: one list_participants read of the chat, unless a read from the last 10 minutes
    is held. One request_id per chat and 10 minutes, so repeated previews queue one read. Never raises: an unavailable
    read (not enabled, rate limit, provider down) leaves the archive to decide membership."""
    from .send_wait import wait_for_operation
    def held():
        with store.authorized(token) as (c, p):
            return participants_read(c, p, account, chat_id, PREVIEW_FRESH_S) is not None
    try:
        if await asyncio.to_thread(held): return
        bucket = int(_now() // PREVIEW_FRESH_S)
        request_id = 'mention-members:' + hashlib.sha256(f'{account}\0{chat_id}\0{bucket}'.encode()).hexdigest()[:40]
        receipt = await asyncio.to_thread(store.execute_messenger_operation, token, account, 'list_participants', chat_id,
                                          parameters={'limit': PAGE_LIMIT}, request_id=request_id)
        if receipt.get('status') in ('queued', 'dispatching'):
            await wait_for_operation(store, token, receipt['operation_id'], READ_WAIT_S)
    except Exception as error:
        log.info('mention_participants_read_unavailable code=%s', getattr(error, 'code', type(error).__name__))
