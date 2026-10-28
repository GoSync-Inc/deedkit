"""One target for any identifier (ANYM-545): exact IDs resolve, names and partial matches ask the agent to choose.

Read-only: no provider call and no write. Every read goes through the caller's visible projection (`scope_filter`,
`visible_members`), never an owner-level link query, so a hidden identity is the same `not_found` as a missing one.
Errors follow the agent rulebook (AGENT_API.md, ANYM-544): `selection_required` carries candidates, each with its
own exact `next`, and never a mutating top-level `next`; `not_found` names a read-only `next`.
"""
import re

from .person_links import UUID_TEXT, annotate, visible_members
from .policy import scope_filter, targets
from .search_query import valid_text
from .whatsapp_identity import personal_id

TOOL = 'platform_resolve_target'
PEOPLE = 'messages_search_people'  # The read-only tool that lists more names (gateway catalog name).
PURPOSES = ('message', 'call')
MAX_CANDIDATES = 10
SCAN_LIMIT = 200
JID = re.compile(r'([0-9]{5,20})(?::[0-9]{1,5})?@(c\.us|s\.whatsapp\.net|lid|g\.us)')
USERNAME = re.compile(r'@([A-Za-z0-9_]{3,64})')
E164_DIGITS = re.compile(r'[1-9][0-9]{7,14}')
# Seven digits with any separators between them (repeated spaces, NBSP, dashes): never in a candidate label.
PHONE_LIKE = re.compile(r'\d(?:[\W_]*\d){6,}')
CHAT_COLUMNS = '''c.id,c.account,c.title,c.chat_type,c.provider_chat,a.platform,
    (SELECT m.sent_at FROM messages m WHERE m.account=c.account AND m.chat=c.id AND NOT m.deleted
     ORDER BY m.sent_at DESC LIMIT 1) AS last_at'''


def _error(code, status, **fields):
    from .store import APIError
    error = APIError(code, status)
    for name, value in fields.items():
        setattr(error, name, value)
    return error


def _not_found(hint=None, **arguments):
    """The same answer for hidden and missing; the read-only next lists names the caller can read."""
    return _error('not_found', 404, next={'tool': PEOPLE, 'arguments': arguments}, **({'hint': hint} if hint else {}))


def _label(value, network, kind):
    value = value.strip()[:200] if isinstance(value, str) else ''
    return value if value and not PHONE_LIKE.search(value) else network + ' ' + kind


def _candidate(id, label, kind, network, account, at, purpose, *, follow=True):
    item = {'id': id, 'label': _label(label, network, kind), 'kind': kind, 'network': network, 'account': account}
    if at is not None:
        item['last_activity'] = at.isoformat()
    if follow:
        item['next'] = {'tool': TOOL, 'arguments': {'to': id, 'purpose': purpose}}
    return item


def _choose(candidates, query=''):
    """Ambiguity: at most 10 candidates by last activity; more of them only through a read-only listing."""
    candidates = sorted(candidates, key=lambda x: x.get('last_activity') or '', reverse=True)
    fields: dict[str, object] = {'candidates': candidates[:MAX_CANDIDATES]}
    if len(candidates) > MAX_CANDIDATES:
        fields.update(more=True, next={'tool': PEOPLE, 'arguments': {'query': query} if query else {}})
    return _error('selection_required', 409, **fields)


def _chats(c, p, where, args, account='', limit=SCAN_LIMIT):
    scope, scope_args = scope_filter(p, 'c', chat_column='id')
    sql = ('SELECT ' + CHAT_COLUMNS + ' FROM chats c JOIN accounts a ON a.id=c.account WHERE ' + scope
           + ' AND c.purged_at IS NULL AND ' + where)
    values = [*scope_args, *args]
    if account:
        sql += ' AND c.account=%s'
        values.append(account)
    return c.execute(sql + ' ORDER BY last_at DESC NULLS LAST,c.id LIMIT ' + str(int(limit)), values).fetchall()


def _peer(row):
    """The private chat's other party as a message sender ID (WhatsApp device suffix ignored)."""
    if row['chat_type'] not in ('private', None):
        return None
    return personal_id(row['provider_chat']) or row['provider_chat']


def _title(c, p, row):
    from .chat_titles import labels
    found = labels(c, p, [(row['account'], row['id'])]).get((row['account'], row['id']))
    return found[0] if found and found[1] else row['title']


def _chat_target(c, p, row, how, purpose):
    if purpose == 'call' and row['chat_type'] in ('group', 'channel'):
        raise _error('unsupported_target', 400, hint='calls_need_a_private_chat_or_phone')
    peer = _peer(row)
    target = {'account': row['account'], 'chat_id': row['id'], 'network': row['platform'],
              'label': _title(c, p, row), 'how_resolved': how}
    if peer:
        linked = [{'account': row['account'], 'sender_id': peer}]
        annotate(c, p, linked)
        if 'person' in linked[0]:
            target['person'] = linked[0]['person']
        if row['platform'] == 'whatsapp' and peer.endswith('@c.us'):
            target['phone'] = '+' + peer.split('@')[0]
    targets(p, [row['id']])
    return target


def _one_or_choose(c, p, rows, how, purpose, query=''):
    if len(rows) == 1:
        return _chat_target(c, p, rows[0], how, purpose)
    raise _choose([_candidate(r['id'], _title(c, p, r), 'chat', r['platform'], r['account'], r['last_at'], purpose)
                   for r in rows], query)


def _whatsapp_accounts(c, p, account):
    rows = c.execute("SELECT id FROM accounts WHERE platform='whatsapp' AND id=ANY(%s) ORDER BY id",
                     (p['accounts'],)).fetchall()
    return [r['id'] for r in rows if not account or r['id'] == account]


def _opted_in(store, c, p, account, digits):
    """The open_chat_by_phone guards (ANYM-532) as reads: a native account with the opt-in, policy and permissions."""
    from .phone_send import _admit, _native
    from .store import APIError
    try:
        with c.transaction():
            _native(c, p, account)
            _admit(store, c, p, account, digits, True)
        return True
    except APIError as error:
        if error.code != 'chat_not_collected':
            return False
    # The last admission guard (commands._phone_policy) also reads exclusions of the number's known LIDs, which this
    # key may not see: answer from the account's own discovery setting only, so a hidden exclusion and an unknown
    # number look the same. The send still runs every guard and refuses an excluded number (chat_not_collected).
    row = c.execute('SELECT discover_chats FROM account_policies WHERE account=%s', (account,)).fetchone()
    return bool(row and row['discover_chats'])


def _alias_chats(c, p, account, jid):
    """The chats of the number's PN/LID pair, read from observations this key can see only.

    A conflicting pair in a visible source blocks the link. Hidden or deleted sources never change the answer, so it
    is no oracle (whatsapp_identity.resolve_alias also fences on them: it serves sends, this read serves the agent).
    """
    scope, scope_args = scope_filter(p, 'm')
    pairs = c.execute('''WITH seen AS NOT MATERIALIZED (
        SELECT DISTINCT wi.pn,wi.lid FROM whatsapp_identity_observations wi
        JOIN messages m ON m.id=wi.source_message JOIN chats ic ON ic.id=m.chat
        WHERE wi.account=%s AND m.account=wi.account AND NOT m.deleted
          AND (wi.identity_id=m.sender OR wi.identity_id=ic.provider_chat) AND ''' + scope + ''')
        SELECT pair.pn,pair.lid FROM seen pair WHERE pair.pn=%s AND NOT EXISTS(SELECT 1 FROM seen other
          WHERE (other.pn=pair.pn AND other.lid<>pair.lid) OR (other.lid=pair.lid AND other.pn<>pair.pn))''',
                      [account, *scope_args, jid]).fetchall()
    ids = sorted({value for pair in pairs for value in (pair['pn'], pair['lid'])})
    return _chats(c, p, "a.platform='whatsapp' AND messenger_whatsapp_personal_id(c.provider_chat)=ANY(%s)",
                  [ids], account) if ids else []


def _phone(store, c, p, digits, account, purpose, how):
    jid = digits + '@c.us'
    rows = _chats(c, p, "a.platform='whatsapp' AND c.provider_chat=%s", [jid], account)
    exact = {r['account'] for r in rows}
    # An account's own phone-number chat wins; elsewhere the visible pair names its chats (several: candidates).
    for name in [name for name in _whatsapp_accounts(c, p, account) if name not in exact]:
        rows += _alias_chats(c, p, name, jid)
    if rows:
        return _one_or_choose(c, p, rows, how, purpose)
    opted = [name for name in _whatsapp_accounts(c, p, account) if _opted_in(store, c, p, name, digits)]
    if len(opted) == 1:
        return {'account': opted[0], 'chat_id': None, 'phone': '+' + digits, 'network': 'whatsapp',
                'label': 'WhatsApp +' + digits, 'how_resolved': how + '_opens_chat', 'opens_chat': True}
    if opted:
        # The caller's own number stays out of candidates and next: the agent repeats the call with `account`.
        raise _choose([_candidate(name, name, 'account', 'whatsapp', name, None, purpose, follow=False)
                       for name in opted])
    raise _not_found('no_chat_and_no_opted_in_account')


def _identities(c, p, where, args, account):
    scope, scope_args = scope_filter(p, 'm')
    sql = ('SELECT DISTINCT sp.account,sp.sender FROM sender_profiles sp JOIN messages m ON m.id=sp.source_message '
           'WHERE ' + scope + ' AND NOT m.deleted AND ' + where)
    values = [*scope_args, *args]
    if account:
        sql += ' AND sp.account=%s'
        values.append(account)
    return c.execute(sql + ' ORDER BY 1,2 LIMIT ' + str(SCAN_LIMIT), values).fetchall()


def _private_chats(c, p, identities):
    pairs = sorted({(r['account'], personal_id(r['sender']) or r['sender']) for r in identities})
    if not pairs:
        return []
    return _chats(c, p, "coalesce(c.chat_type,'private')='private' AND (c.account,c.provider_chat) IN "
                  '(SELECT * FROM unnest(%s::text[],%s::text[]))', [[a for a, _ in pairs], [s for _, s in pairs]])


def _person(c, p, person, account, purpose):
    from .store import APIError
    try:
        person, members, _ = visible_members(c, p, person)
    except APIError:
        raise _not_found() from None
    members = [{'account': a, 'sender': s} for a, s in members if not account or a == account]
    rows = _private_chats(c, p, members)
    if not rows:
        raise _not_found('no_usable_channel', person=person)
    if len(rows) == 1:
        return {**_chat_target(c, p, rows[0], 'person', purpose), 'person': person}
    # Several channels and no preference (ANYM-548 adds one): the agent picks the channel.
    raise _choose([_candidate(r['id'], _title(c, p, r), 'chat', r['platform'], r['account'], r['last_at'], purpose)
                   for r in rows])


def _message(store, c, p, message_id, account, purpose, sender):
    scope, scope_args = scope_filter(p, 'm')
    row = c.execute('''SELECT m.id,m.account,m.chat,m.sender,m.direction,a.platform,
        coalesce(mm.metadata,mr.snapshot->'metadata') AS metadata FROM messages m JOIN accounts a ON a.id=m.account
        LEFT JOIN message_revisions mr ON mr.message=m.id AND mr.revision=m.revision
        LEFT JOIN message_metadata mm ON mm.message=m.id AND mm.revision=m.revision
        WHERE m.id=%s AND NOT m.deleted AND ''' + scope, [message_id, *scope_args]).fetchone()
    if not row or (account and row['account'] != account):
        raise _not_found()
    metadata = row['metadata'] if isinstance(row['metadata'], dict) else {}
    if sender:
        if row['direction'] == 'outgoing':
            raise _not_found('message_from_owner')
        chats = _private_chats(c, p, [row])
        if chats:
            return _chat_target(c, p, chats[0], 'message_sender', purpose)
        own = personal_id(row['sender'])
        if own and own.endswith('@c.us'):
            return _phone(store, c, p, own.split('@')[0], row['account'], purpose, 'message_sender_phone')
        raise _not_found('no_private_chat')
    if metadata.get('message_kind') == 'contact':
        return _card(store, c, p, row, metadata.get('contact'), account, purpose)
    rows = _chats(c, p, 'c.id=%s', [row['chat']])
    if not rows:
        raise _not_found()
    return _chat_target(c, p, rows[0], 'message_chat', purpose)


def _card(store, c, p, row, card, account, purpose):
    """A shared contact card (ANYM-529) resolves to its WhatsApp ID or phone, or its Telegram user's chat."""
    options = []
    contacts = card.get('contacts') if isinstance(card, dict) and card.get('status') in ('parsed', 'truncated') else []
    for contact in contacts if isinstance(contacts, list) else []:
        name = contact.get('name') or contact.get('vcard_name') or ''
        if contact.get('telegram_user_id') and row['platform'] == 'telegram':
            options.append(('telegram', contact['telegram_user_id'], name))
        for phone in contact.get('phones') or []:
            digits = phone.get('whatsapp_id') or (phone['number'].lstrip('+') if phone.get('normalized') else '')
            if E164_DIGITS.fullmatch(digits or ''):
                options.append(('phone', digits, name))
    options = list({(kind, value): (kind, value, name) for kind, value, name in options}.values())
    if not options:
        raise _not_found('contact_card_fields_missing')
    if len(options) > 1:
        raise _choose([_candidate('+' + value if kind == 'phone' else value, name, kind, row['platform'],
                                  row['account'], None, purpose, follow=kind == 'phone') for kind, value, name in options])
    kind, value, _ = options[0]
    if kind == 'phone':
        return _phone(store, c, p, value, account, purpose, 'contact_card_phone')
    chats = _chats(c, p, 'c.provider_chat=%s', [value], row['account'])
    if not chats:
        raise _not_found('no_private_chat')
    return _chat_target(c, p, chats[0], 'contact_card_telegram_user', purpose)


def _name(c, p, text, account, purpose):
    """A name or fragment never resolves by itself, even with one match (ANYM-545 criterion 2)."""
    query = text.lower()
    pattern = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
    scope, scope_args = scope_filter(p, 'm')
    # One query ranks every matching chat (a title, or a private chat of a matching visible identity) by activity
    # before the limit; one more row than shown tells `more`.
    rows = _chats(c, p, "(lower(c.title) LIKE %s OR (coalesce(c.chat_type,'private')='private' AND "
                  '(c.account,c.provider_chat) IN (SELECT sp.account,coalesce(messenger_whatsapp_personal_id(sp.sender),'
                  'sp.sender) FROM sender_profiles sp JOIN messages m ON m.id=sp.source_message WHERE ' + scope
                  + ' AND NOT m.deleted AND sp.search_text LIKE %s)))', [pattern, *scope_args, pattern], account,
                  limit=MAX_CANDIDATES + 1)
    if not rows:
        raise _not_found(query=text)
    raise _choose([_candidate(r['id'], _title(c, p, r), 'chat', r['platform'], r['account'], r['last_at'], purpose)
                   for r in rows], text)


def classify(to):
    """The identifier form of `to`: (kind, value). Anything not exact is a name."""
    if UUID_TEXT.fullmatch(to):
        return 'person', to
    if re.fullmatch(r'chat_[0-9a-f]{32}', to):
        return 'chat', to
    if re.fullmatch(r'msg_[0-9a-f]{32}', to):
        return 'message', to
    if USERNAME.fullmatch(to):
        return 'username', to[1:]
    match = JID.fullmatch(to)
    if match:
        return ('phone_jid', match[1]) if match[2] in ('c.us', 's.whatsapp.net') else ('jid', to)
    from .messenger_operations import phone_digits
    digits, _ = phone_digits(to)  # The open_chat_by_phone normalizer (ANYM-532): every number it accepts is exact.
    return ('phone', digits) if digits else ('name', to)


def resolve_target(store, c, p, to, purpose='message', account='', sender=False):
    valid_text(to, 256, empty=False)
    valid_text(account, 64)
    if purpose not in PURPOSES or type(sender) is not bool or not to.strip() or re.search(r'[\x00-\x1f\x7f]', to):
        raise _error('invalid_arguments', 400, fields=['to', 'purpose', 'sender'])
    if account and account not in p['accounts']:
        raise _not_found()
    kind, value = classify(to.strip())
    if sender and kind != 'message':
        raise _error('invalid_arguments', 400, fields=['sender'])
    if kind == 'person':
        target = _person(c, p, value, account, purpose)
    elif kind == 'chat':
        rows = _chats(c, p, 'c.id=%s', [value], account)
        if not rows:
            raise _not_found()
        target = _chat_target(c, p, rows[0], 'chat_id', purpose)
    elif kind == 'message':
        target = _message(store, c, p, value, account, purpose, sender)
    elif kind in ('phone', 'phone_jid'):
        target = _phone(store, c, p, value, account, purpose, 'phone' if kind == 'phone' else 'jid')
    elif kind == 'jid':
        rows = _chats(c, p, "a.platform='whatsapp' AND c.provider_chat=%s", [value], account)
        if not rows:
            raise _not_found()
        target = _one_or_choose(c, p, rows, 'jid', purpose)
    elif kind == 'username':
        identities = _identities(c, p, 'lower(sp.username)=%s', [value.lower()], account)
        rows = _private_chats(c, p, identities)
        if not rows:
            raise _not_found('no_private_chat' if identities else None, query=value)
        # Exact only when the username names one visible identity; else the agent picks.
        if len(identities) > 1:
            raise _choose([_candidate(r['id'], _title(c, p, r), 'chat', r['platform'], r['account'], r['last_at'],
                                      purpose) for r in rows])
        target = _one_or_choose(c, p, rows, 'username', purpose)
    else:
        target = _name(c, p, to.strip(), account, purpose)
    return {'state': 'succeeded', 'target': target, 'next': None}
