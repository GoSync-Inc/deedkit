"""Reviewed operation catalog, never an unrestricted provider RPC proxy.

Archive reads retain their existing passive contract. These operations run on the
existing session owner with an independent key grant and account opt-in.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import re

from .field_errors import FIELD_NAME, field_error, one_per_field

MAX_REGISTERED_FILE_BYTES = 32 * 1024**2
CATALOG_VERSION = 1
PHONE_LIMITATION = ('Native WhatsApp (whatsmeow) accounts only. One number per call: create=false checks it and '
    'writes nothing; create=true after the owner confirms returns the chat_id for send_message. At most 10 provider '
    'lookups per account per rolling hour; requires discover_chats. Sends still need exact owner confirmation.')
# A phone-shaped chat reference: the not_found hint names open_chat_by_phone from the
# input's shape alone, never from what the archive holds.
PHONE_SHAPED = re.compile(r'\+?[0-9]{5,20}|[0-9]{5,20}@(?:c\.us|s\.whatsapp\.net)')


def _field(kind, *, required=False, default=None, **limits):
    return {'type': kind, 'required': required, 'default': default, **limits}


def _spec(permission, target='chat', *, mutation=False, full_account=False,
          platforms=('telegram', 'whatsapp'), fields=None, limitation=''):
    return dict(permission=permission, target=target, mutation=mutation,
        full_account=full_account, platforms=list(platforms), fields=fields or {},
        limitation=limitation)


_page = {'limit': _field('integer', default=50, minimum=1, maximum=100),
         'offset': _field('integer', default=0, minimum=0, maximum=100000)}
_user = {'user_id': _field('contact_id', required=True)}
_title = {'title': _field('string', required=True, maximum=128, nonempty=True)}
_duration = {'seconds': _field('integer', default=86400, minimum=1, maximum=2147483647)}
_enabled = {'enabled': _field('boolean', required=True)}
_users = {'user_ids': _field('contact_ids', required=True)}
SLOW_MODE_SECONDS = [0, 10, 30, 60, 300, 900, 3600]
RIGHTS_RACE = ('Changes only this restriction; on Telegram an administrator edit made between the read and the '
    'write can be overwritten.')
_link = {'link': _field('chat_link', required=True)}
JOIN_LIMITATION = ('Needs a check_chat_link of this link by the same key within 60 minutes showing not_member; '
    'pending_approval is not a join. A joined chat is visible to no chat-scoped key and to full-account keys '
    'only when the collection policy collects it. Leave with leave_group.')
LIVE_SEARCH = 'search_messages_live'
LIVE_SEARCH_LIMITATION = ('Telegram only. Searches Telegram itself: an empty chat_id searches all the owner\'s chats '
    '(full-account key; channels only when chat_types names channel), a chat_id searches that archived chat. One '
    'provider request per call, no automatic paging: pass next_cursor with the same parameters for more. Per account '
    '3 requests at once, then one per 10 s, 100 per UTC day (in memory; a collector restart resets them); over it, '
    'rate_limited with retry_after. Hits in chats the collection policy excludes are dropped. Writes nothing to the '
    'archive.')
LIVE_CHAT_TYPES = ('private', 'group', 'channel')
LIVE_FILTERS = ('documents', 'photos', 'links', 'voice')
BATCH_LIMITATION = ('One row per ID: applied, refused, unknown or not_attempted; an uncertain ID is never retried.')
OPERATIONS = {
    'chat_info': _spec('read-live'),
    'list_participants': _spec('groups-read', fields=_page),
    'get_reactions': _spec('read-live', 'message'),
    'get_message_info': _spec('read-live', 'message'),
    'get_pinned_messages': _spec('read-live', fields=_page, platforms=('whatsapp',)),
    'list_topics': _spec('groups-read', fields=_page, platforms=('telegram',)),
    'get_profile': _spec('profile-read', 'account', full_account=True),
    'list_contacts': _spec('contacts-read', 'account', full_account=True, fields=_page),
    'get_contact': _spec('contacts-read', 'account', full_account=True, fields=_user),
    'list_blocked': _spec('contacts-read', 'account', full_account=True, fields=_page),
    'send_file': _spec('send', mutation=True, fields={
        'attachment_id': _field('attachment_id'),
        'upload_id': _field('upload_id'),
        'sha256': _field('sha256', required=True),
        'mode': _field('file_mode', default='document'),
        'caption': _field('string', default='', maximum=1024)},
        limitation='Exactly one accessible attachment_id or own finalized destination-bound upload_id; SHA-256 verified original only, 32 MiB bound.'),
    'forward_message': _spec('send', mutation=True, fields={
        'source_message_id': _field('message_id', required=True),
        'source_revision': _field('integer', required=True, minimum=1, maximum=2147483647)},
        limitation='Both source and destination must be authorized in the same account; provider restrictions apply.'),
    'send_poll': _spec('send', mutation=True, fields={
        'question': _field('string', required=True, maximum=255, nonempty=True),
        'options': _field('poll_options', required=True),
        'multiple': _field('boolean', default=False)}),
    'send_contact': _spec('send', mutation=True, full_account=True),  # Contains contact data.
    'send_location': _spec('send', mutation=True, fields={
        'latitude': _field('number', required=True, minimum=-90, maximum=90),
        'longitude': _field('number', required=True, minimum=-180, maximum=180),
        'description': _field('string', default='', maximum=255)}),
    'edit_message': _spec('message-edit', 'message', mutation=True, fields={
        'text': _field('string', required=True, maximum=4000, nonempty=True)},
        limitation='Own outgoing message at the exact archive revision; plain text only.'),
    'delete_message': _spec('message-delete', 'message', mutation=True,
        limitation='Own outgoing message only; global revoke must not fall back to local deletion.'),
    'send_reaction': _spec('message-react', 'message', mutation=True, fields={
        'emoji': _field('string', required=True, maximum=64)},
        limitation='Empty emoji removes the reaction; no paid or custom-emoji transactions.'),
    'vote_poll': _spec('message-react', 'message', mutation=True, platforms=('telegram',),
        fields={'option_indices': _field('poll_indices', required=True)}),
    'pin_message': _spec('message-pin', 'message', mutation=True, fields=_duration),
    'unpin_message': _spec('message-pin', 'message', mutation=True),
    'mark_read': _spec('read-state', 'message', mutation=True,
        limitation='Telegram read boundary is the target message; WhatsApp marks the whole chat read.'),
    'mark_unread': _spec('read-state', mutation=True),
    'archive_chat': _spec('chat-manage', mutation=True),
    'unarchive_chat': _spec('chat-manage', mutation=True),
    'mute_chat': _spec('chat-manage', mutation=True, fields=_duration),
    'unmute_chat': _spec('chat-manage', mutation=True),
    'pin_chat': _spec('chat-manage', mutation=True, platforms=('whatsapp',)),
    'unpin_chat': _spec('chat-manage', mutation=True, platforms=('whatsapp',)),
    'star_message': _spec('chat-manage', 'message', mutation=True, platforms=('whatsapp',)),
    'unstar_message': _spec('chat-manage', 'message', mutation=True, platforms=('whatsapp',)),
    'create_group': _spec('groups-manage', 'account', mutation=True, full_account=True,
        fields={**_title, 'user_ids': _field('contact_ids', required=True)}),
    'rename_group': _spec('groups-manage', mutation=True, fields=_title),
    'set_group_description': _spec('groups-manage', mutation=True,
        fields={'description': _field('string', required=True, maximum=255)}),
    'add_participant': _spec('groups-manage', mutation=True, fields=_user),
    'remove_participant': _spec('groups-manage', mutation=True, fields=_user),
    'promote_participant': _spec('groups-manage', mutation=True, fields=_user,
        limitation='Conservative administrator rights only; Telegram basic-group promotion is unsupported.'),
    'demote_participant': _spec('groups-manage', mutation=True, fields=_user),
    'leave_group': _spec('groups-manage', mutation=True),
    'get_invite_link': _spec('groups-manage', mutation=True,
        limitation='Sensitive invitation link; WhatsApp may create/export a link. Telegram reads existing invite only.'),
    'revoke_invite_link': _spec('groups-manage', mutation=True),
    'set_group_messages_admins_only': _spec('groups-manage', mutation=True, fields=_enabled, limitation=RIGHTS_RACE),
    'set_group_info_admins_only': _spec('groups-manage', mutation=True, fields=_enabled, limitation=RIGHTS_RACE),
    'set_slow_mode': _spec('groups-manage', mutation=True, platforms=('telegram',),
        fields={'seconds': _field('slow_mode_seconds', required=True)}, limitation='Supergroups only; 0 turns slow mode off.'),
    'set_join_approval': _spec('groups-manage', mutation=True, platforms=('whatsapp',), fields=_enabled),
    'list_join_requests': _spec('groups-read', fields={'limit': _field('integer', default=50, minimum=1, maximum=100)},
        limitation='First page of pending requests; names and notes are untrusted requester text.'),
    'approve_join_requests': _spec('groups-manage', mutation=True, fields=_users, limitation=BATCH_LIMITATION),
    'reject_join_requests': _spec('groups-manage', mutation=True, fields=_users, limitation=BATCH_LIMITATION),
    'check_chat_link': _spec('groups-read', 'account', full_account=True, fields=_link,
        limitation='Passive live read of an invite or public link; no member list, inviter or description.'),
    'join_chat': _spec('groups-manage', 'account', mutation=True, full_account=True, fields=_link,
        limitation=JOIN_LIMITATION),
    'add_contact': _spec('contacts-manage', 'account', mutation=True, full_account=True,
        fields={
            'phone': _field('phone', required=True),
            'first_name': _field('string', required=True, maximum=64, nonempty=True),
            'last_name': _field('string', default='', maximum=64)}),
    'delete_contact': _spec('contacts-manage', 'account', mutation=True, full_account=True,
        fields=_user, limitation='WhatsApp address-book deletion requires a phone-number ID, not a LID.'),
    'block_contact': _spec('contacts-manage', 'account', mutation=True, full_account=True, fields=_user),
    'unblock_contact': _spec('contacts-manage', 'account', mutation=True, full_account=True, fields=_user),
    'update_profile': _spec('profile-manage', 'account', mutation=True, full_account=True),
    'request_chat_history': _spec('read-live', mutation=True, platforms=('whatsapp',),
        fields={'count':_field('integer',default=50,minimum=1,maximum=50)},
        limitation='Native only: explicit phone history request, at most 50 messages and one per account per minute; partial history is not guaranteed.'),
    LIVE_SEARCH: _spec('read-live', platforms=('telegram',), fields={
        'query': _field('string', required=True, maximum=256, nonempty=True),
        'chat_types': _field('chat_types'), 'filter': _field('search_filter'),
        'from_time': _field('timestamp'), 'to_time': _field('timestamp'),
        'limit': _field('integer', default=20, minimum=1, maximum=50),
        'cursor': _field('search_cursor')}, limitation=LIVE_SEARCH_LIMITATION),
    'get_labels': _spec('contacts-read', 'account', full_account=True,
        platforms=('whatsapp',), limitation='Business feature; availability depends on the account.'),
    'set_chat_labels': _spec('chat-manage', mutation=True, platforms=('whatsapp',),
        fields={'label_ids': _field('label_ids', required=True)},
        limitation='Business feature; availability depends on the account.'),
    'open_chat_by_phone': _spec('send', 'account', full_account=True, platforms=('whatsapp',), fields={
        'phone': _field('phone_number', required=True),
        'create': _field('boolean', default=False)},
        limitation=PHONE_LIMITATION),
}


def operation_spec(name):
    if not isinstance(name, str) or name not in OPERATIONS:
        raise ValueError('unsupported_operation')
    return deepcopy(OPERATIONS[name])


# Address book and label operations need a verified provider-side equivalent.
# A local name update/empty cache is not a successful remote operation.
NATIVE_UNSUPPORTED=frozenset(('add_contact','delete_contact','get_labels','set_chat_labels'))
# ANYM-242: native adapter only on WhatsApp; the browser fallback never substitutes another action.
GROUP_ADMIN_OPERATIONS=('set_group_messages_admins_only','set_group_info_admins_only','set_slow_mode',
    'set_join_approval','list_join_requests','approve_join_requests','reject_join_requests')
# ANYM-243: likewise native only; the browser fallback never joins through another path.
CHAT_LINK_OPERATIONS=('check_chat_link','join_chat')


def _supported(platform, name, spec, provider=None):
    if platform not in spec['platforms']:return False
    if platform!='whatsapp':return True
    if provider=='whatsmeow':return name not in NATIVE_UNSUPPORTED
    return name not in ('delete_message','request_chat_history','open_chat_by_phone')+GROUP_ADMIN_OPERATIONS+CHAT_LINK_OPERATIONS


def _fields(platform, name, spec):
    if platform == 'telegram' and name == 'pin_message':
        return {}  # Telegram pins have no expiry parameter.
    if name == 'send_contact':
        return ({'contact_ids': _field('contact_ids', required=True)} if platform == 'whatsapp' else
            {'phone': _field('phone', required=True),
             'first_name': _field('string', required=True, maximum=64, nonempty=True),
             'last_name': _field('string', default='', maximum=64)})
    if name == 'update_profile':
        names = ('name', 'status') if platform == 'whatsapp' else ('first_name', 'last_name', 'bio')
        return {n: _field('string', maximum=64 if n in ('name', 'first_name', 'last_name') else 139,
                         nonempty=n in ('name', 'first_name')) for n in names}
    return spec['fields']


def _schema(platform, fields):
    # Published metadata only; validation and persisted parameters never read it.
    schema = deepcopy(fields)
    for descriptor in schema.values():
        text = parameter_format(platform, descriptor['type'])
        if text: descriptor['format'] = text
    return schema


def operation_manifest(platform, *, provider=None):
    if platform not in ('telegram', 'whatsapp'):
        raise ValueError('unsupported_platform')
    rows = []
    for name, spec in sorted(OPERATIONS.items()):
        row = {k: deepcopy(v) for k, v in spec.items() if k not in ('fields', 'platforms')}
        row.update(operation=name, supported=_supported(platform, name, spec, provider),
                   parameters_schema=_schema(platform, _fields(platform, name, spec)))
        if platform=='whatsapp' and provider=='whatsmeow' and name in NATIVE_UNSUPPORTED:
            row['limitation']='Not implemented by the native adapter; never substituted with a local cache mutation.'
        elif platform=='whatsapp' and provider=='whatsmeow' and name in ('get_reactions','get_message_info','get_pinned_messages'):
            row['limitation']='Observed local evidence only; missing history and baseline are explicitly unknown.'
        elif platform=='whatsapp' and provider=='whatsmeow' and name=='delete_message':
            row['limitation']='Own outgoing message only; explicit global revoke, never local-delete fallback.'
        elif platform=='whatsapp' and provider=='whatsmeow' and name=='send_file':
            row['limitation']='Archived attachment or own destination-bound uploaded SHA-256 original only; 32 MiB bound. Sticker requires existing WebP. Audio/voice/sticker captions are unsupported; no implicit conversion.'
        elif platform=='whatsapp' and provider=='whatsmeow' and name=='forward_message':
            row['limitation']='Authorized retained ordinary content only; control, view-once and poll replay are unsupported.'
        elif platform=='whatsapp' and provider!='whatsmeow' and name in GROUP_ADMIN_OPERATIONS+CHAT_LINK_OPERATIONS and platform in spec['platforms']:
            row['limitation']='Unsupported here: native WhatsApp (whatsmeow) accounts only.'
        elif name == 'open_chat_by_phone' and not row['supported']:
            row['limitation'] = 'Unsupported here: native WhatsApp (whatsmeow) accounts only.'
        elif platform == 'whatsapp' and name == 'delete_message':
            row['limitation'] = 'Unsupported: upstream global revoke can silently fallback to local deletion.'
        elif platform not in spec['platforms']:
            row['limitation'] = 'No implemented adapter for this provider-specific operation.'
        rows.append(row)
    return rows


class ParameterError(ValueError):
    """invalid_operation_parameters with bounded field_errors; never input values."""
    def __init__(self, *errors, code='invalid_operation_parameters', hint=None):
        super().__init__(code)
        self.field_errors = one_per_field(errors)  # One entry per field path, so fallback paths never crowd out others.
        if hint: self.hint = hint


_SEPARATORS = re.compile(r'[\s\-\u2010-\u2015\u2212.()]')


def phone_digits(value):
    """Digits of an international number, or None and the reviewed hint of the problem."""
    if not isinstance(value, str) or len(value) > 64: return None, None
    text = _SEPARATORS.sub('', value)
    if text.startswith('+'): text, international = text[1:], True
    elif text.startswith('00'): text, international = text[2:], True
    else: international = False
    if not re.fullmatch(r'[0-9]+', text, re.ASCII): return None, None
    if text.startswith('0'): return None, None if international else 'country_code_required'
    return (text, None) if 7 <= len(text) <= 15 else (None, None)


def chat_link_key(platform, value):
    """The normalized link key (Telegram +CODE or lowercase @name; WhatsApp CODE), or None. No URL is fetched."""
    if not isinstance(value, str) or len(value) > 128: return None
    if platform == 'telegram':
        code = re.fullmatch(r'https://t\.me/(?:\+|joinchat/)([A-Za-z0-9_-]{8,64})', value, re.ASCII)
        if code: return '+' + code.group(1)
        name = re.fullmatch(r'(?:https://t\.me/|@)([A-Za-z][A-Za-z0-9_]{4,31})', value, re.ASCII)
        return '@' + name.group(1).lower() if name else None
    code = re.fullmatch(r'(?:https://chat\.whatsapp\.com/)?([A-Za-z0-9]{8,64})', value, re.ASCII)
    return code.group(1) if code else None


def _modes(platform):
    return ['document', 'photo', 'video', 'audio', 'voice'] + (['video_note'] if platform == 'telegram' else ['sticker'])


def parameter_format(platform, kind):
    """Reviewed format text for identifier and structured parameter types."""
    if kind == 'contact_id':
        return ('Telegram user ID as a decimal string of a positive 64-bit integer (1-19 digits, no leading zero), '
                'such as sender_id from search_people; '
                'not person_*, @username or a phone number' if platform == 'telegram' else
                'WhatsApp user JID digits@c.us or digits@lid, such as sender_id from search_people; '
                'not person_* or a bare phone number')
    return {'contact_ids': '1-20 unique items, each a ' + parameter_format(platform, 'contact_id'),
        'message_id': 'archive message ID msg_ plus 32 lowercase hex characters, from get_messages or search',
        'attachment_id': 'archive attachment ID att_ plus 32 lowercase hex characters, from message attachments or search_files',
        'upload_id': 'own upload ID up_ plus 32 lowercase hex characters, from prepare_file_upload',
        'sha256': '64 lowercase hex characters: the SHA-256 of the exact original',
        'phone': 'international number of 5-20 digits, the first 1-9, with an optional leading +, no spaces or punctuation',
        'phone_number': 'international number with its country code, such as +49 151 1234-5678 or 0049 (151) 12345678; '
            'spaces, dashes, dots and parentheses are ignored; 7-15 digits, no national trunk 0',
        'file_mode': 'one of ' + ', '.join(_modes(platform)),
        'chat_link': ('https://t.me/+CODE, https://t.me/joinchat/CODE (CODE 8-64 letters, digits, _ or -), '
            'https://t.me/NAME or @NAME (NAME 5-32 letters, digits or _, starting with a letter); '
            'no query string or fragment' if platform == 'telegram' else
            'https://chat.whatsapp.com/CODE or bare CODE (8-64 letters or digits); no query string or fragment; '
            'WhatsApp channel links are not yet supported'),
        'slow_mode_seconds': 'one of ' + ', '.join(map(str, SLOW_MODE_SECONDS)) + ' seconds; 0 turns slow mode off',
        'poll_options': '2-10 unique non-blank strings of at most 100 UTF-16 code units',
        'poll_indices': 'up to 20 unique integers from 0 to 9 (zero-based option positions)',
        'chat_types': '1-3 unique items of ' + ', '.join(LIVE_CHAT_TYPES),
        'search_filter': 'one of ' + ', '.join(LIVE_FILTERS),
        'timestamp': 'ISO 8601 date and time with a UTC offset, such as 2026-10-22T08:00:00Z; from_time is inclusive, '
            'to_time exclusive',
        'search_cursor': 'next_cursor of an earlier search_messages_live with the same account, chat_id and parameters',
        'label_ids': 'up to 20 unique label IDs of 1-64 letters, digits, _ or -, from get_labels'}.get(kind)


def _problem(platform, descriptor, value, field):
    """First failing check for one parameter as a field error, or None when valid."""
    kind = descriptor['type']
    def fail(reason, expected=None, allowed=None, path=field):
        return [field_error(path, reason, expected, allowed)]
    if kind == 'string':
        if not isinstance(value, str): return fail('type', 'string')
        try: size = len(value.encode('utf-16-le')) // 2
        except UnicodeError: return fail('format', 'valid Unicode text')
        if size > descriptor['maximum']:
            return fail('max_length', 'at most %d UTF-16 code units; most emoji count as 2' % descriptor['maximum'])
        if any(ord(c) < 32 and c not in '\n\r\t' or ord(c) == 127 for c in value):
            return fail('pattern', 'no control characters except newline, carriage return and tab')
        if descriptor.get('nonempty') and not value.strip(): return fail('blank', 'non-blank text')
        return None
    if kind in ('integer', 'number'):
        if kind == 'integer' and type(value) is not int: return fail('type', 'integer')
        # An int is always finite; math.isfinite would overflow on a huge one.
        if kind == 'number' and (type(value) not in (int, float) or type(value) is float and not math.isfinite(value)):
            return fail('type', 'finite number')
        if value < descriptor['minimum']: return fail('minimum', 'at least %s' % descriptor['minimum'])
        if value > descriptor['maximum']: return fail('maximum', 'at most %s' % descriptor['maximum'])
        return None
    if kind == 'boolean':
        return None if type(value) is bool else fail('type', 'boolean')
    if kind == 'phone_number':
        if not isinstance(value, str): return fail('type', 'string')
        return None if phone_digits(value)[0] else fail('format', parameter_format(platform, kind))
    if kind == 'chat_link':
        if not isinstance(value, str): return fail('type', 'string')
        if chat_link_key(platform, value): return None
        if platform == 'whatsapp' and re.match(r'https://(www\.)?whatsapp\.com/channel/', value):
            return fail('format', 'WhatsApp channel links are not yet supported; ' + parameter_format(platform, kind))
        return fail('format', parameter_format(platform, kind))
    if kind == 'slow_mode_seconds':
        if type(value) is not int: return fail('type', 'integer')
        return None if value in SLOW_MODE_SECONDS else fail('enum', allowed=SLOW_MODE_SECONDS)
    if kind == 'file_mode':
        if not isinstance(value, str): return fail('type', 'string')
        return None if value in _modes(platform) else fail('enum', allowed=_modes(platform))
    pattern = {'upload_id': r'up_[a-f0-9]{32}', 'attachment_id': r'att_[a-f0-9]{32}', 'message_id': r'msg_[a-f0-9]{32}',
        'sha256': r'[a-f0-9]{64}', 'phone': r'\+?[1-9][0-9]{4,19}',
        'contact_id': r'[1-9][0-9]{0,18}' if platform == 'telegram' else r'[0-9]{1,30}@(c\.us|lid)'}.get(kind)
    if pattern:
        if not isinstance(value, str): return fail('type', 'string')
        if not re.fullmatch(pattern, value) or kind == 'contact_id' and platform == 'telegram' and int(value) >= 2**63:
            return fail('format', parameter_format(platform, kind))
        return None
    if kind in ('contact_ids', 'poll_options', 'poll_indices', 'label_ids'):
        if not isinstance(value, list): return fail('type', 'array')
        maximum = 10 if kind == 'poll_options' else 20
        if len(value) > maximum: return fail('max_items', 'at most %d items' % maximum)
        minimum = 2 if kind == 'poll_options' else 1 if kind == 'contact_ids' else 0
        if len(value) < minimum: return fail('min_items', 'at least %d item%s' % (minimum, 's' * (minimum > 1)))
        item = {'poll_options': _field('string', maximum=100, nonempty=True), 'contact_ids': _field('contact_id'),
                'poll_indices': _field('integer', minimum=0, maximum=9), 'label_ids': _field('label_id')}[kind]
        errors = [e for i, x in enumerate(value) for e in (_problem(platform, item, x, '%s/%d' % (field, i)) or ())]
        if errors: return errors
        return None if len(set(value)) == len(value) else fail('unique', 'no duplicate items')
    if kind == 'chat_types':
        if not isinstance(value, list): return fail('type', 'array')
        if not 1 <= len(value) <= 3: return fail('min_items' if not value else 'max_items', parameter_format(platform, kind))
        if any(item not in LIVE_CHAT_TYPES for item in value): return fail('enum', allowed=list(LIVE_CHAT_TYPES))
        return None if len(set(value)) == len(value) else fail('unique', 'no duplicate items')
    if kind == 'search_filter':
        if not isinstance(value, str): return fail('type', 'string')
        return None if value in LIVE_FILTERS else fail('enum', allowed=list(LIVE_FILTERS))
    if kind == 'timestamp':
        if not isinstance(value, str): return fail('type', 'string')
        return None if timestamp(value) else fail('format', parameter_format(platform, kind))
    if kind == 'search_cursor':
        if not isinstance(value, str): return fail('type', 'string')
        return None if re.fullmatch(r'[A-Za-z0-9_-]{8,512}', value) else fail('format', parameter_format(platform, kind))
    if kind == 'label_id':
        if not isinstance(value, str): return fail('type', 'string')
        return None if re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', value) else fail('format', parameter_format(platform, 'label_ids'))
    return fail('type')


def timestamp(value):
    """Normalized UTC text of an ISO 8601 time with an offset, or None."""
    if not isinstance(value, str) or len(value) > 40: return None
    try: parsed = datetime.fromisoformat(value.replace('Z', '+00:00') if value.endswith('Z') else value)
    except ValueError: return None
    if parsed.tzinfo is None or not 2000 <= parsed.year <= 2100: return None
    return parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def live_cursor_binding(account, chat_id, parameters):
    """The account, operation, chat and every other normalized field a live-search cursor was issued for.

    The collector keeps it next to the cursor's position, so a caller cannot rewrite it."""
    fields = {k: v for k, v in parameters.items() if k != 'cursor'}
    return json.dumps([account, LIVE_SEARCH, chat_id or '', fields], sort_keys=True, ensure_ascii=False)


def validate_operation(platform, operation, parameters, *, provider=None):
    spec = operation_spec(operation)
    if not _supported(platform, operation, spec, provider): raise ValueError('unsupported_operation')
    if parameters is None: parameters = {}
    if not isinstance(parameters, dict): raise ParameterError(field_error('parameters', 'type', 'object'))
    fields = _fields(platform, operation, spec)
    output, errors = {}, []
    try:
        for name, descriptor in fields.items():
            if name not in parameters:
                if descriptor['required']:
                    errors.append(field_error(name, 'required', parameter_format(platform, descriptor['type'])))
                    continue
                if descriptor['default'] is None: continue
                value = deepcopy(descriptor['default'])
            else: value = parameters[name]
            problem = _problem(platform, descriptor, value, name)
            if problem: errors.extend(problem)
            else: output[name] = (phone_digits(value)[0] if descriptor['type'] == 'phone_number' else
                chat_link_key(platform, value) if descriptor['type'] == 'chat_link' else
                timestamp(value) if descriptor['type'] == 'timestamp' else value)
        # Only a bounded identifier name is returned; the submitted value never is.
        allowed = 'only ' + ', '.join(fields) if fields else 'no parameters'
        for name in parameters:
            if name not in fields:
                errors.append(field_error(name if FIELD_NAME.fullmatch(str(name)) else 'parameters', 'unknown_field', allowed))
    except (UnicodeError, TypeError, OverflowError):
        raise ParameterError(field_error('parameters', 'type', 'JSON object of documented parameters')) from None
    if errors and operation == 'open_chat_by_phone' and any(e['field'] == 'phone' for e in errors):
        raise ParameterError(*errors, code='invalid_phone', hint=phone_digits(parameters.get('phone'))[1])
    if errors: raise ParameterError(*errors)
    if operation == 'update_profile' and not output:
        raise ParameterError(field_error('parameters', 'required', 'at least one of ' + ', '.join(fields)))
    if operation == 'send_file' and (('attachment_id' in output)+('upload_id' in output)!=1):
        raise ParameterError(field_error('upload_id' if 'upload_id' in output else 'attachment_id',
            'unique' if 'upload_id' in output else 'required', 'exactly one of attachment_id or upload_id'))
    if operation == LIVE_SEARCH and 'from_time' in output and 'to_time' in output and (
            datetime.fromisoformat(output['from_time']) >= datetime.fromisoformat(output['to_time'])):
        raise ParameterError(field_error('to_time', 'minimum', 'later than from_time'))
    return output
