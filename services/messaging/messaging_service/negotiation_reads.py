"""ANYM-909: `list_negotiations`, every negotiation being arranged now (ANYM-907 rows) in one read.

Read-only: SELECT on negotiations, negotiation_sources and negotiation_times (gateway_role.sql, schema 76) and the
archive reads every read makes. Every query goes through `policy.scope_filter`; a negotiation with a purged source is
left out at once (negotiation_store.VISIBLE), and a deleted or purged message never gives a phrase. No HTTP request:
`next` only names read calls (get_context, calendar_find_time) for the agent to make; the gateway drops each one the
caller's tool list lacks.

A page key is (order key, id). Later pages of one read show only negotiations unchanged since its first page
(`as_of`); a negotiation that changed after it is never repeated or lost: the last page names how many changed and
the read that returns them (`changed`).
"""
import os
from datetime import datetime, time, timedelta

from .chat_queries import CHAT_TYPES, title_sql
from .negotiation_store import VISIBLE
from .person_links import annotate, member_clause, visible_members
from .policy import scope_filter, targets
from .scheduling_rules import signals
from .time_references import owner_zone

STATES = ('open', 'upcoming', 'past', 'cancelled', 'stale')
ORDERS = ('awaiting_owner_first', 'updated', 'soonest')
DETAILS = ('compact', 'full')
CONFIDENCES = ('low', 'medium', 'high')
MAX_LIMIT = 50
MAX_IDS = 50
PHRASE = 120
# The calendar's find_time limits (gateway catalog: people at most 32, duration 5-1440, window at most 31 days).
WIDEN = timedelta(minutes=30)
MAX_FIND_TIME = 5
READ_TABLES = ('negotiations', 'negotiation_sources', 'negotiation_times')

# The end that tells upcoming from past: the agreed end, else (an ambiguous agreement) the latest option's end.
END_SQL = '''coalesce(n.agreed_end,(SELECT max(et.ends_at) FROM negotiation_times et WHERE et.negotiation=n.id
    AND et.status IN ('current','agreed')))'''
PHASE_SQL = f'''CASE WHEN n.state IN ('proposed','counter') THEN 'open'
    WHEN n.state='agreed' THEN CASE WHEN {END_SQL}<=%s THEN 'past' ELSE 'upcoming' END ELSE n.state END'''
SOON_SQL = '''coalesce(n.agreed_start,(SELECT min(st.starts_at) FROM negotiation_times st WHERE st.negotiation=n.id
    AND st.status IN ('current','agreed')),'9999-12-31T00:00:00Z'::timestamptz)'''
# A negotiation with no known time sorts last in `soonest`.
RANK_SQL = "CASE n.confidence WHEN 'high' THEN 2 WHEN 'medium' THEN 1 ELSE 0 END"


def _error(code, status, **fields):
    from .store import APIError
    error = APIError(code, status)
    for name, value in fields.items():setattr(error, name, value)
    return error


def read_ready(c):
    """The three read tables present and readable by this connection's role, each checked alone; never a schema number."""
    for table in READ_TABLES:
        row = c.execute('''SELECT to_regclass(%s) IS NOT NULL AND has_table_privilege(%s,'SELECT') AS ok''',
                        (f'public.{table}', f'public.{table}')).fetchone()
        if row['ok'] is not True:return False
    return True


def _texts(name, values, maximum=100, allowed=None):
    from .search_query import filter_values, valid_text
    if values is None:return None
    values = filter_values(values)
    if not values or len(values) > maximum:raise _refused(name, 'size')
    for v in values:
        valid_text(v, 256, empty=False)
        if allowed is not None and v not in allowed:raise _refused(name, 'enum', list(allowed))
    return list(dict.fromkeys(values))


def _refused(field, reason, allowed=None):
    from .field_errors import refusal
    return refusal('invalid_filter', [(field, reason, allowed)])


def _time_bound(name, value):
    from .store import APIError, date_bound
    if value in (None, ''):return None
    try:
        return date_bound(value)
    except APIError:
        raise _refused(name, 'format', 'ISO 8601 date-time with an offset') from None


def _person_filter(store, c, p, person):
    """(clause, args) for negotiations one of whose participants is this person, or whose chat is the person's chat.
    A UUID is a person; anything else goes through resolve_target (ambiguous: selection_required; unknown: not_found)."""
    from .resolve_target import classify, resolve_target
    from .search_query import valid_text
    valid_text(person, 256, empty=False)
    if classify(person.strip())[0] == 'person':
        _, members, _ = visible_members(c, p, person.strip())
        chats = []
    else:
        target = resolve_target(store, c, p, person.strip())['target']
        chats = [target['chat_id']]
        members = visible_members(c, p, target['person'])[1] if target.get('person') else []
    clauses, args = [], []
    if members:
        clause, margs = member_clause('pm', 'account', 'sender', members)
        clauses.append(f'''EXISTS (SELECT 1 FROM negotiation_sources ps JOIN messages pm ON pm.id=ps.message
            WHERE ps.negotiation=n.id AND ps.removed_at IS NULL AND {clause})''')
        args += margs
    if chats:
        clauses.append('n.chat=ANY(%s)')
        args.append(chats)
    return '(' + ' OR '.join(clauses) + ')', args


def _key_sql(order):
    """(order key expressions, ORDER BY) for one order; the id breaks every tie."""
    if order == 'awaiting_owner_first':
        return "(CASE WHEN n.awaiting='owner' THEN 0 ELSE 1 END)", 'n.k0 ASC, n.updated_at DESC, n.id DESC'
    if order == 'updated':
        return '0', 'n.updated_at DESC, n.id DESC'
    return SOON_SQL, 'n.k0 ASC, n.id ASC'


def _after_sql(order):
    """The keyset condition after the cursor's (k0, at, id); its arguments are `_after_args`."""
    if order == 'soonest':
        return '(n.k0>%s OR (n.k0=%s AND n.id>%s))'
    return '(n.k0>%s OR (n.k0=%s AND (n.updated_at<%s OR (n.updated_at=%s AND n.id<%s))))'


def _phrase(body, sent_at, direction, zone, stored_cue):
    """At most 120 characters of the body around its first time span, else the cue span, else its start."""
    body = body or ''
    found = signals(body, sent_at, 'out' if direction == 'outgoing' else 'in', zone)
    span = next((t['span'] for t in found.get('times') or [] if t.get('span')), None) or found.get('cue_span') or stored_cue
    if not span:return body[:PHRASE].strip() or None
    start, end = span
    if end - start >= PHRASE:return body[start:start + PHRASE].strip() or None
    pad = (PHRASE - (end - start)) // 2
    lo = max(0, start - pad)
    hi = min(len(body), lo + PHRASE)
    lo = max(0, hi - PHRASE)
    return body[lo:hi].strip() or None


def _time(row):
    return {'start': row['starts_at'].isoformat(), 'end': row['ends_at'].isoformat(), 'granularity': row['granularity'],
            'zone': row['zone'], 'zone_source': row['zone_source'], 'assumed': list(row['assumed'] or [])}


def _window(t):
    """calendar_find_time's window for one time: its own span widened by 30 minutes each side; a day 08:00-20:00 and a
    week Monday 00:00 to Sunday 24:00, in the time's zone."""
    from zoneinfo import ZoneInfo
    start, end = datetime.fromisoformat(t['start']), datetime.fromisoformat(t['end'])
    if t['granularity'] in ('day', 'week'):
        try:
            tz = ZoneInfo(t['zone'])
        except (ValueError, KeyError, OSError):
            tz = start.tzinfo
        local = start.astimezone(tz)
        if t['granularity'] == 'day':
            day = local.date()
            start, end = datetime.combine(day, time(8), tz), datetime.combine(day, time(20), tz)
        else:
            monday = local.date() - timedelta(days=local.weekday())
            start, end = datetime.combine(monday, time(), tz), datetime.combine(monday + timedelta(days=7), time(), tz)
    else:
        start, end = start - WIDEN, end + WIDEN
    return {'from': start.isoformat(), 'to': end.isoformat()}


def _next(item, owner_person):
    """Read calls only, each whole as its tool's input schema takes it: get_context on the latest source; per current
    or agreed time, calendar_find_time for the owner's person UUID (the calendar resolves only its owner-made link)."""
    calls = []
    if item['_latest']:
        calls.append({'tool': 'get_context', 'arguments': {'message_id': item['_latest']}})
    times = [item['agreed_time']] if item['agreed_time'] else item['current_times']
    if owner_person and times:
        duration = 30 if item['medium'] in ('call', 'video_call') else 60
        for t in times[:MAX_FIND_TIME]:
            calls.append({'tool': 'calendar_find_time',
                          'arguments': {'people': [owner_person], 'window': _window(t), 'duration': duration}})
    return calls


def _owner_senders(c, p, accounts):
    """Each account's owner sender ID: the sender of its newest outgoing message the key can read."""
    if not accounts:return {}
    scope, args = scope_filter(p, 'm')
    rows = c.execute(f'''SELECT a.account, o.sender FROM unnest(%s::text[]) AS a(account) CROSS JOIN LATERAL (
        SELECT m.sender FROM messages m WHERE m.account=a.account AND {scope} AND m.direction='outgoing'
        AND NOT m.deleted AND m.sender<>'' ORDER BY m.sent_at DESC, m.id DESC LIMIT 1) o''',
        [sorted(accounts), *args]).fetchall()
    return {r['account']: r['sender'] for r in rows}


def list_negotiations(store, c, p, *, states=None, chat_types=None, chat_ids=None, accounts=None, person=None,
                      from_time=None, to_time=None, updated_since=None, negotiation_ids=None, min_confidence='medium',
                      order='awaiting_owner_first', detail='compact', limit=20, cursor=None):
    from .search_query import cursor_binding, decode_cursor, encode_cursor
    from .store import bounded
    if not read_ready(c):
        raise _error('not_ready', 503, hint='negotiation_tables_not_ready')
    states = _texts('states', states, allowed=STATES) or ['open', 'upcoming']
    chat_types = _texts('chat_types', chat_types, allowed=CHAT_TYPES) or ['private']
    chat_ids = _texts('chat_ids', chat_ids)
    accounts = _texts('accounts', accounts)
    negotiation_ids = _texts('negotiation_ids', negotiation_ids, MAX_IDS)
    if min_confidence not in CONFIDENCES:raise _refused('min_confidence', 'enum', list(CONFIDENCES))
    if order not in ORDERS:raise _refused('order', 'enum', list(ORDERS))
    if detail not in DETAILS:raise _refused('detail', 'enum', list(DETAILS))
    bounded(limit, MAX_LIMIT)
    start, end = _time_bound('from_time', from_time), _time_bound('to_time', to_time)
    if start and end and start >= end:raise _refused('to_time', 'after_from_time')
    since = _time_bound('updated_since', updated_since)
    filters = {'states': sorted(states), 'chat_types': sorted(chat_types), 'chat_ids': chat_ids, 'accounts': accounts,
               'person': person, 'from_time': from_time, 'to_time': to_time, 'updated_since': updated_since,
               'negotiation_ids': negotiation_ids, 'min_confidence': min_confidence, 'order': order}
    binding = cursor_binding(p, 'negotiations', filters)
    position = decode_cursor(cursor, binding) if cursor else None
    if position is not None and set(position) != {'as_of', 'k0', 'at', 'id'}:
        from .field_errors import cursor_refusal
        raise cursor_refusal()
    now = (datetime.fromisoformat(position['as_of']) if position else
           c.execute('SELECT CURRENT_TIMESTAMP AS t').fetchone()['t'])

    scope, scope_args = scope_filter(p, 'c', chat_column='id')
    where = [scope, "n.state<>'removed'", VISIBLE, "coalesce(c.chat_type,'unknown')=ANY(%s)"]
    args = [*scope_args, chat_types]
    if chat_ids is not None:
        where.append('n.chat=ANY(%s)')
        args.append(chat_ids)
    if accounts is not None:
        where.append('n.account=ANY(%s)')
        args.append(accounts)
    if negotiation_ids is not None:
        # Another key's or account's ID is the same answer as an unknown one, and so is a hidden or removed one.
        known = c.execute(f'''SELECT n.id FROM negotiations n JOIN chats c ON c.id=n.chat AND c.account=n.account
            WHERE {scope} AND n.state<>'removed' AND {VISIBLE} AND n.id=ANY(%s)''',
            [*scope_args, negotiation_ids]).fetchall()
        if len(known) != len(negotiation_ids):raise _error('not_found_or_forbidden', 404)
        where.append('n.id=ANY(%s)')
        args.append(negotiation_ids)
    if person is not None:
        clause, pargs = _person_filter(store, c, p, person)
        where.append(clause)
        args += pargs
    if start or end:
        where.append('''(EXISTS (SELECT 1 FROM negotiation_times ft WHERE ft.negotiation=n.id
            AND ft.status IN ('current','agreed') AND (%s::timestamptz IS NULL OR ft.ends_at>%s)
            AND (%s::timestamptz IS NULL OR ft.starts_at<%s))
          OR (n.agreed_start IS NOT NULL AND (%s::timestamptz IS NULL OR n.agreed_end>%s)
            AND (%s::timestamptz IS NULL OR n.agreed_start<%s)))''')
        args += [start, start, end, end, start, start, end, end]
    if since:
        where.append('n.updated_at>=%s')
        args.append(since)
    where.append(f'{RANK_SQL}>=%s')
    args.append(CONFIDENCES.index(min_confidence))
    key, order_by = _key_sql(order)
    base = f'''SELECT n.*, c.chat_type, {title_sql('c')} AS chat_title, ({PHASE_SQL}) AS phase,
        {key} AS k0 FROM negotiations n JOIN chats c ON c.id=n.chat AND c.account=n.account
        WHERE {' AND '.join(where)}'''
    args = [now, *args]
    page = f'SELECT * FROM ({base}) n WHERE n.phase=ANY(%s)'
    page_args = [*args, states]
    if position:
        # Later pages: only what was there unchanged at the first page, after its key.
        page += ' AND n.updated_at<=%s AND ' + _after_sql(order)
        page_args += [now] + _after_args(order, position)
    rows = c.execute(page + f' ORDER BY {order_by} LIMIT %s', [*page_args, limit + 1]).fetchall()
    rows, more = rows[:limit], len(rows) > limit
    items = _items(c, p, rows, detail)
    result = {'items': items, 'as_of': now.isoformat(), 'cursor': None}
    if more:
        last = rows[-1]
        k0 = last['k0'].isoformat() if isinstance(last['k0'], datetime) else last['k0']
        result['cursor'] = encode_cursor(binding, {'as_of': now.isoformat(), 'k0': k0,
                                                   'at': last['updated_at'].isoformat(), 'id': last['id']})
    elif position:
        changed = c.execute(f'SELECT count(*) AS n FROM ({base}) n WHERE n.phase=ANY(%s) AND n.updated_at>%s',
                            [*args, states, now]).fetchone()['n']
        if changed:
            again = {k: v for k, v in filters.items() if v is not None and k != 'updated_since'}
            again.update(detail=detail, limit=limit, updated_since=now.isoformat())
            result['changed'] = {'count': changed, 'next': {'tool': 'list_negotiations', 'arguments': again}}
    targets(p, [i['id'] for i in items])
    return result


def _after_args(order, position):
    k0 = position['k0']
    if order == 'soonest':
        k0 = datetime.fromisoformat(k0)
        return [k0, k0, position['id']]
    at = datetime.fromisoformat(position['at'])
    return [k0, k0, at, at, position['id']]


def _items(c, p, rows, detail):
    if not rows:return []
    zone = owner_zone(os.environ)
    ids = [r['id'] for r in rows]
    times = {}
    for t in c.execute('''SELECT t.*, m.sender, m.direction FROM negotiation_times t LEFT JOIN messages m ON m.id=t.message
        WHERE t.negotiation=ANY(%s) ORDER BY t.negotiation, t.ord''', (ids,)).fetchall():
        times.setdefault(t['negotiation'], []).append(t)
    sources = {}
    for s in c.execute('''SELECT s.negotiation, s.message, s.revision, s.intent, s.cue_start, s.cue_end, m.sent_at,
          m.direction, m.sender, m.account, m.chat, m.deleted, m.purged, m.body, nullif(sp.display_name,'') AS sender_name
        FROM negotiation_sources s JOIN messages m ON m.id=s.message
        LEFT JOIN sender_profiles sp ON sp.account=m.account AND sp.chat=m.chat AND sp.sender=m.sender
        WHERE s.negotiation=ANY(%s) AND s.removed_at IS NULL ORDER BY s.negotiation, m.sent_at, m.id''',
                          (ids,)).fetchall():
        sources.setdefault(s['negotiation'], []).append(s)
    owners = _owner_senders(c, p, {r['account'] for r in rows})
    out, people = [], []
    for r in rows:
        own = sources.get(r['id'], [])
        mine = times.get(r['id'], [])
        participants, seen = [], set()
        owner = next((s['sender'] for s in own if s['direction'] == 'outgoing' and s['sender']), None) or owners.get(
            r['account'])
        for s in own:
            sender = owner if s['direction'] == 'outgoing' else s['sender']
            if not sender or sender in seen:continue
            seen.add(sender)
            participants.append({'account': r['account'], 'sender_id': sender, 'name': s['sender_name'],
                                 'is_owner': s['direction'] == 'outgoing'})
        if owner and owner not in seen:
            participants.insert(0, {'account': r['account'], 'sender_id': owner, 'name': None, 'is_owner': True})
        people += participants
        agreed = next((t for t in mine if t['status'] == 'agreed'), None)
        agreed_time = _time(agreed) if agreed else ({'start': r['agreed_start'].isoformat(), 'end': r['agreed_end'].isoformat(),
            'granularity': None, 'zone': r['agreed_zone'], 'zone_source': None, 'assumed': []} if r['agreed_start'] else None)
        previous = ({'start': r['previous_agreed_start'].isoformat(), 'end': r['previous_agreed_end'].isoformat(),
                     'zone': r['previous_agreed_zone']} if r['previous_agreed_start'] else None)
        latest = own[-1] if own else None
        live = latest is not None and not latest['deleted'] and not latest['purged']
        phrase = _phrase(latest['body'], latest['sent_at'], latest['direction'], zone, _cue(latest)) if latest and live else None
        item = {'id': r['id'], 'version': r['version'], 'account': r['account'],
                'chat': {'id': r['chat'], 'title': r['chat_title'], 'type': r['chat_type'] or 'unknown'},
                'state': r['state'], 'phase': r['phase'], 'medium': r['medium'], 'confidence': r['confidence'],
                'awaiting': r['awaiting'], 'participants': participants,
                'current_times': [_time(t) for t in mine if t['status'] == 'current'], 'agreed_time': agreed_time,
                'previous_agreed_time': previous, 'rescheduled': r['rescheduled'], 'ambiguous': r['ambiguous'],
                'phrase': phrase,
                'opened_at': r['opened_at'].isoformat(), 'updated_at': r['updated_at'].isoformat(),
                '_latest': latest['message'] if latest else None}
        if detail == 'full':
            def proposed(t, owner=owner):
                return {'sender_id': owner if t['direction'] == 'outgoing' else t['sender'],
                        'is_owner': t['direction'] == 'outgoing'}
            item['countered_times'] = [_time(t) | {'proposed_by': proposed(t)} for t in mine if t['status'] == 'countered']
            item['declined_times'] = [_time(t) | {'proposed_by': proposed(t)} for t in mine
                                      if t['status'] in ('declined', 'declined_only')]
            item['sources'] = [{'message_id': s['message'], 'revision': s['revision'], 'sent_at': s['sent_at'].isoformat(),
                                'direction': s['direction'], 'sender_id': owner if s['direction'] == 'outgoing' else s['sender'],
                                'sender_name': s['sender_name'], 'intent': s['intent'], 'deleted': bool(s['deleted']),
                                'phrase': None if s['deleted'] or s['purged'] else _phrase(
                                    s['body'], s['sent_at'], s['direction'], zone, _cue(s))} for s in own]
        out.append(item)
    # A person UUID only where a member is visible to the key (person_links.annotate).
    annotate(c, p, people)
    for person in people:
        person['person_id'] = person.pop('person', None)
    for item in out:
        owner_person = next((x['person_id'] for x in item['participants'] if x['is_owner']), None)
        item['next'] = _next(item, owner_person)
        del item['_latest']
    return out


def _cue(source):
    if source['cue_start'] is None or source['cue_end'] is None:return None
    return [source['cue_start'], source['cue_end']]
