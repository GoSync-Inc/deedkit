"""Module events (ANYM-562): calendar and calls events in a journal of their own.

The module journal (schema 56) has its own gapless counter, so publishing never takes the
feed_counter lock of the message journal. A subscription's module part is a second stream with
its own matched, acknowledged, hinted and retained positions. Events carry references only.
This module holds the contract (CloudEvents attribute names, the per-type data schema and the
producers' cursor rules), the matcher pass, the stream's reads and its retention.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re

from psycopg.types.json import Jsonb

SOURCES = {'deedkit:calendar': 'calendar', 'deedkit:calls': 'calls'}
URGENCIES = frozenset(('now', 'batch'))
_REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
_SCOPE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,255}')
_TOKEN = re.compile(r'[a-z][a-z0-9_]{0,47}')
_ID = re.compile(r'(calendar|calls):([1-9][0-9]{0,15})')
_TIME = re.compile(r'(19[7-9][0-9]|[2-9][0-9]{3})-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d{1,6})?(Z|[+-](0\d|1[0-5]):[0-5]\d)')
# Every time is finite and inside this range (UTC), so every reader decodes it; the definer function
# enforces the same bounds.
_EARLIEST = datetime(1970, 1, 1, tzinfo=timezone.utc)
_LATEST = datetime(9999, 12, 30, tzinfo=timezone.utc)
MAX_SEQ = 2**53 - 1


def _ref(value):
    return isinstance(value, str) and _REF.fullmatch(value) is not None


def _time(value):
    if not isinstance(value, str) or _TIME.fullmatch(value) is None:
        return False
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return False
    return _EARLIEST <= moment <= _LATEST


def _int(low, high):
    return lambda value: type(value) is int and low <= value <= high


def _enum(*values):
    return lambda value: value in values


def _changed(value):
    return (isinstance(value, list) and 1 <= len(value) <= 3 and len(set(value)) == len(value) and
        all(item in ('start', 'end', 'details') for item in value))


_token = lambda value: isinstance(value, str) and _TOKEN.fullmatch(value) is not None  # noqa: E731
_CALENDAR = {'calendar_ref': _ref, 'event_ref': _ref, 'start_at': _time}
# type: (data checks, optional keys, the key the CloudEvents subject names). Values are references,
# enumerations, bounded numbers and times only: no titles, places, names, destinations or text.
KINDS = {
    'calendar.event.created': ({**_CALENDAR, 'version': _int(0, MAX_SEQ)}, frozenset(), 'event_ref'),
    'calendar.event.changed': ({**_CALENDAR, 'version': _int(0, MAX_SEQ), 'changed': _changed}, frozenset(),
        'event_ref'),
    'calendar.event.cancelled': (dict(_CALENDAR), frozenset(), 'event_ref'),
    'calendar.event.starting': ({**_CALENDAR, 'lead_minutes': _int(1, 1440)}, frozenset(), 'event_ref'),
    'call.finished': ({'call_id': _ref, 'status': _enum('finished'), 'end_reason': _token,
        'duration_s': _int(0, 86400), 'outcome': _token, 'transcript': _enum('final')},
        frozenset(('duration_s', 'outcome')), 'call_id'),
    'call.failed': ({'call_id': _ref, 'status': _enum('failed', 'finished'), 'end_reason': _token}, frozenset(),
        'call_id'),
    'call.not_answered': ({'call_id': _ref, 'end_reason': _enum('no_answer', 'busy', 'canceled')}, frozenset(),
        'call_id'),
    'call.interrupted': ({'call_id': _ref, 'end_reason': _enum('service_restart')}, frozenset(), 'call_id'),
    'call.owner_request': ({'call_id': _ref, 'decision_id': _ref, 'state': _enum('pending')}, frozenset(),
        'call_id'),
}
ENVELOPE = frozenset(('specversion', 'id', 'source', 'type', 'subject', 'time', 'data', 'urgency', 'expires_at'))
# Measured as PostgreSQL prints jsonb (', ' and ': '), so the definer function's bound is the same number.
DATA_LIMIT_BYTES = 1024


class ModuleContractError(ValueError):
    """A producer's page or event outside the contract; the code names the rule, never the value."""


def _fail(code):
    raise ModuleContractError(code)


def validate_event(event):
    """Return the event's module seq, or raise ModuleContractError. References only (criterion 2)."""
    if not isinstance(event, dict) or set(event) != ENVELOPE:
        _fail('invalid_envelope')
    if event['specversion'] != '1.0' or event['source'] not in SOURCES:
        _fail('invalid_envelope')
    match = _ID.fullmatch(event['id']) if isinstance(event['id'], str) else None
    if match is None or match.group(1) != SOURCES[event['source']] or int(match.group(2)) > MAX_SEQ:
        _fail('invalid_id')
    kind = KINDS.get(event['type']) if isinstance(event['type'], str) else None
    if kind is None or event['type'].split('.')[0] != ('calendar' if match.group(1) == 'calendar' else 'call'):
        _fail('invalid_type')
    checks, optional, subject = kind
    data = event['data']
    if not isinstance(data, dict) or set(data) - set(checks):
        _fail('data_key_not_allowed')
    if set(checks) - optional - set(data):
        _fail('data_key_missing')
    if any(not checks[key](value) for key, value in data.items()):
        _fail('data_value_out_of_bounds')
    if event['subject'] != data[subject] or not _time(event['time']):
        _fail('invalid_envelope')
    if event['urgency'] not in URGENCIES or event['expires_at'] is not None and not _time(event['expires_at']):
        _fail('invalid_envelope')
    if event['type'] == 'call.owner_request' and (event['urgency'] != 'now' or event['expires_at'] is None):
        # The decision's stored deadline: an owner request is never held by batching or kept past it.
        _fail('owner_request_needs_now_and_deadline')
    if event['type'] == 'calendar.event.starting' and (event['expires_at'] is None or
            _at(event['expires_at']) != _at(data['start_at'])):
        _fail('starting_expires_at_start')
    if len(json.dumps(data, separators=(', ', ': ')).encode()) > DATA_LIMIT_BYTES:
        _fail('data_too_large')
    return int(match.group(2))


def _at(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def read_page(cursor, page, *, limit, max_bytes):
    """Apply the producers' cursor contract (criterion 3) to one page of GET /v1/events?after=.

    cursor: {'after': int, 'journal_id': str or None}. Returns {'status': 'events', 'events', 'next'},
    {'status': 'gap', 'gap', 'next'} or {'status': 'reset'}; raises ModuleContractError on a page
    that breaks the contract. 'after' is exclusive; seq is the module's gapless journal number;
    next_after always moves past rows the producer filtered; a retention floor above the cursor is
    an explicit gap, never an empty complete page; a new journal_id or a cursor going back is a
    source reset and is never read as new events.
    """
    after = cursor['after']
    if not isinstance(page, dict) or set(page) != {'events', 'next_after', 'has_more', 'retention_floor',
            'journal_id'}:
        _fail('invalid_page')
    if (not isinstance(page['journal_id'], str) or not 1 <= len(page['journal_id']) <= 64 or
            not isinstance(page['events'], list) or type(page['has_more']) is not bool or
            any(type(page[key]) is not int or not 0 <= page[key] <= MAX_SEQ for key in ('next_after',
                'retention_floor'))):
        _fail('invalid_page')
    if cursor.get('journal_id') is not None and page['journal_id'] != cursor['journal_id']:
        return {'status': 'reset', 'journal_id': page['journal_id']}
    if page['next_after'] < after:
        return {'status': 'reset', 'journal_id': page['journal_id']}
    if len(page['events']) > limit:
        _fail('page_over_limit')
    if len(json.dumps(page, separators=(',', ':')).encode()) > max_bytes:
        _fail('page_over_bytes')
    next_cursor = {'after': page['next_after'], 'journal_id': page['journal_id']}
    if page['retention_floor'] > after:
        # Rows the producer pruned before this cursor read them: name the range, read none of it.
        return {'status': 'gap', 'gap': {'reason': 'retention_gap', 'after': after,
            'through': page['retention_floor'], 'recoverable': False},
            'next': {'after': page['retention_floor'], 'journal_id': page['journal_id']}}
    previous = after
    for event in page['events']:
        seq = validate_event(event)
        if seq <= previous:
            _fail('seq_not_increasing' if previous > after else 'seq_not_after_cursor')
        previous = seq
    if previous > page['next_after']:
        _fail('next_after_behind_events')
    if page['has_more'] and page['next_after'] == after:
        # A page that would be read again forever: filtered rows must still move the cursor.
        _fail('next_after_not_advancing')
    return {'status': 'events', 'events': page['events'], 'next': next_cursor}


def ready(c):
    """Schema 56 present: new code tolerates an older database during a rolling upgrade."""
    return c.execute("SELECT to_regclass('public.subscription_module_streams') IS NOT NULL AS ready").fetchone()['ready']


def publish(c, event, scope_ref):
    """Validate, then publish through the definer function; returns the journal seq (idempotent)."""
    validate_event(event)
    if not isinstance(scope_ref, str) or _SCOPE.fullmatch(scope_ref) is None:
        _fail('invalid_scope_ref')
    return c.execute('SELECT public.publish_module_event(%s,%s,%s,%s,%s,%s,%s,%s,%s) AS seq',
        (event['id'], event['source'], event['type'], event['subject'], scope_ref, event['time'],
         Jsonb(event['data']), event['urgency'], event['expires_at'])).fetchone()['seq']


def report_reachability(c, source, reachable):
    if source not in SOURCES or type(reachable) is not bool:
        _fail('invalid_source')
    c.execute('SELECT public.report_module_reachability(%s,%s)', (source, reachable))


def attach_sources(c, subscription_id, sources):
    """Give a subscription a module stream starting at the module journal's head.

    For ANYM-564, which checks each scope against the key's links first; no API reaches this
    without that check. sources: [(source, scope_ref, [types])].
    """
    from .store import APIError
    if not isinstance(sources, (list, tuple)) or not 1 <= len(sources) <= 64:
        raise APIError('invalid_filter')
    for source, scope_ref, types in sources:
        if (source not in SOURCES or not isinstance(scope_ref, str) or _SCOPE.fullmatch(scope_ref) is None or
                not isinstance(types, (list, tuple)) or not 1 <= len(types) <= 16 or
                any(kind not in KINDS or kind.split('.')[0] != ('calendar' if source == 'deedkit:calendar' else
                    'call') for kind in types)):
            raise APIError('invalid_filter')
    head = c.execute('SELECT value FROM module_event_counter WHERE id=1').fetchone()['value']
    c.execute('''INSERT INTO subscription_module_streams(subscription,start_position,matched_through,
        acked_through,hinted_through) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
        (subscription_id, head, head, head, head))
    for source, scope_ref, types in sources:
        c.execute('''INSERT INTO subscription_module_sources(subscription,source,scope_ref,types)
            VALUES(%s,%s,%s,%s) ON CONFLICT(subscription,source,scope_ref) DO UPDATE SET types=excluded.types''',
            (subscription_id, source, scope_ref, sorted(set(types))))


def stream(c, subscription_id, *, lock=False):
    if not ready(c):
        return None
    return c.execute('SELECT * FROM subscription_module_streams WHERE subscription=%s' +
        (' FOR UPDATE' if lock else ''), (subscription_id,)).fetchone()


def view(c, row, module):
    """The module stream block of get_subscription; absent for a message-only subscription."""
    from .store import serial
    head = c.execute('SELECT value FROM module_event_counter WHERE id=1').fetchone()['value']
    sources = c.execute('''SELECT s.source,s.scope_ref,s.types,h.unreachable_since FROM subscription_module_sources s
        LEFT JOIN module_source_health h ON h.source=s.source WHERE s.subscription=%s
        ORDER BY s.source,s.scope_ref''', (row['id'],)).fetchall()
    count = c.execute('SELECT count(*) AS n FROM subscription_module_matches WHERE subscription=%s',
        (row['id'],)).fetchone()['n']
    # Per source and self-clearing: the router's last report decides; the matcher's blocked_reason is untouched.
    health = [{'source': source, 'state': 'module_unreachable', 'since': since} for source, since in
        sorted({(item['source'], item['unreachable_since']) for item in sources if item['unreachable_since']})]
    state = ('held_after_rollback' if module['rollback_hold'] else row['state'])
    return serial({'state': state, 'start_position': module['start_position'],
        'matched_through': module['matched_through'], 'acked_through': module['acked_through'],
        'matcher_caught_up': state == 'active' and module['matched_through'] == head,
        'matched_event_count': count, 'blocked_reason': module['blocked_reason'], 'gap': module['gap'],
        'retention_floor': module['retention_floor'], 'last_matched_at': module['last_matched_at'],
        'sources': [{'source': item['source'], 'scope_ref': item['scope_ref'], 'types': item['types']}
            for item in sources], 'health': health,
        'next_action': 'resume' if module['rollback_hold'] else None})


def match(c, row, *, batch_size, maximum, stats):
    """The module pass of one subscription, inside the matcher's transaction and locks.

    The caller runs it in a savepoint after the message pass, so a failure here never undoes or
    stops the message matches of the same subscription.
    """
    module = c.execute('SELECT * FROM subscription_module_streams WHERE subscription=%s FOR UPDATE',
        (row['id'],)).fetchone()
    if module is None or module['rollback_hold']:
        return
    sources = c.execute('SELECT source,scope_ref,types FROM subscription_module_sources WHERE subscription=%s',
        (row['id'],)).fetchall()
    lookup = {(source['source'], source['scope_ref']): set(source['types']) for source in sources}
    head = c.execute('SELECT value FROM module_event_counter WHERE id=1').fetchone()['value']
    events = c.execute('''SELECT seq,source,type,scope_ref,urgency,
        expires_at IS NOT NULL AND expires_at<=clock_timestamp() AS expired FROM module_events
        WHERE seq>%s AND seq<=%s ORDER BY seq LIMIT %s''', (module['matched_through'], head, batch_size)).fetchall()
    count = c.execute('SELECT count(*) AS n FROM subscription_module_matches WHERE subscription=%s',
        (row['id'],)).fetchone()['n']
    position, reason = module['matched_through'], None
    gap = module['gap'] if module['gap'] and module['gap'].get('intentional') else None
    for event in events:
        if event['seq'] != position + 1:
            reason = 'journal_gap'
            gap = {'reason': reason, 'stream': 'modules', 'after': position, 'through': event['seq'] - 1,
                'recoverable': False, 'requires_operator': True}
            break
        # An expired event (a reminder after its start) is never matched, so never hinted late.
        if event['type'] in lookup.get((event['source'], event['scope_ref']), ()) and not event['expired']:
            if count >= maximum:
                reason = 'match_budget'
                gap = {'reason': reason, 'stream': 'modules', 'after': position, 'through': head,
                    'recoverable': True, 'requires_operator': True}
                break
            if c.execute('''INSERT INTO subscription_module_matches(subscription,event) VALUES(%s,%s)
                    ON CONFLICT DO NOTHING RETURNING event''', (row['id'], event['seq'])).fetchone():
                count += 1
                stats['module_matched'] += 1
        position = event['seq']
    if reason is None and len(events) < batch_size and position < head:
        reason = 'journal_gap'
        gap = {'reason': reason, 'stream': 'modules', 'after': position, 'through': head,
            'recoverable': False, 'requires_operator': True}
    if reason:
        stats['module_blocked'] += 1
    c.execute('''UPDATE subscription_module_streams SET matched_through=%s,blocked_reason=%s,gap=%s,
        last_matched_at=clock_timestamp() WHERE subscription=%s''',
        (position, reason, Jsonb(gap) if gap else None, row['id']))
    try:
        # Its own savepoint around every statement of the step: a database error (a statement timeout
        # included) rolls back to here, so the transaction stays usable and the matches above commit.
        with c.transaction():
            _hint(c, row, module)
    except Exception as error:
        # The matches and frontier above stay; hinted_through stays, so a later pass offers the hint again.
        from .telemetry import capture
        capture(error, 'subscription_module_hint_failed')
        print(json.dumps({'error': 'subscription_module_hint_failed', 'class': type(error).__name__}), flush=True)


def _hint(c, row, module):
    """One module_events hint up to the newest module match not hinted yet; retried each pass.

    A savepoint of its own: a refused hint (a full endpoint queue) leaves hinted_through, so the
    next pass offers it again, and never undoes the matches above.
    """
    last = c.execute('''SELECT max(m.event) AS through,bool_or(e.urgency='now') AS urgent
        FROM subscription_module_matches m JOIN module_events e ON e.seq=m.event
        WHERE m.subscription=%s AND m.event>%s''', (row['id'], module['hinted_through'])).fetchone()
    if last['through'] is None:
        return
    from .event_delivery import _hint_target, endpoint_queue_full
    with c.transaction():
        binding = _hint_target(c, row)
        if binding is not None:
            if not c.execute('SELECT public.module_hints_ready() AS ready').fetchone()['ready']:
                # No dispatcher that knows the kind beats: an older one would read a module position as a
                # message position. Nothing is staged; hinted_through stays, so the hint waits for one.
                return
            target = 'receiver' if binding['receiver'] is not None else 'endpoint'
            # The key's hint lock (delivery_policy._slot) makes the queue count and the insert one step.
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                ('subscription-hints:' + str(row['key_id']),))
            if target == 'endpoint' and endpoint_queue_full(c, row):
                return
            # urgency=now passes quiet hours like a quiet_bypass reason; the dispatcher's key budget still applies.
            c.execute('''INSERT INTO subscription_outbox(subscription,subscription_version,''' + target + ''',
                through_position,kind,quiet_bypass) VALUES(%s,%s,%s,%s,'module_events',%s) ON CONFLICT DO NOTHING''',
                (row['id'], row['version'], binding[target], last['through'], bool(last['urgent'])))
        c.execute('UPDATE subscription_module_streams SET hinted_through=%s WHERE subscription=%s',
            (last['through'], row['id']))


def _item(event):
    return {'module_event_id': event['seq'], 'specversion': '1.0', 'id': event['event_key'],
        'source': event['source'], 'type': event['type'], 'subject': event['subject'], 'time': event['occurred_at'],
        'data': event['data'], 'urgency': event['urgency'], 'expires_at': event['expires_at'],
        'expired': event['expired']}


def page(c, row, module, after, limit):
    """Module stream page after an exclusive module position; a pruned range is a gap, never empty."""
    floor = module['retention_floor']
    if after < floor:
        return {'items': [], 'has_more': True, 'next_after': floor,
            'gap': {'reason': 'retention_gap', 'stream': 'modules', 'after': after, 'through': floor,
                'recoverable': False, 'floor': floor}}
    events = c.execute('''SELECT e.*,e.expires_at IS NOT NULL AND e.expires_at<=clock_timestamp() AS expired
        FROM subscription_module_matches m JOIN module_events e ON e.seq=m.event
        WHERE m.subscription=%s AND m.event>%s ORDER BY m.event LIMIT %s''', (row['id'], after, limit + 1)).fetchall()
    items = [_item(event) for event in events[:limit]]
    return {'items': items, 'has_more': len(events) > limit,
        'next_after': items[-1]['module_event_id'] if items else after, 'gap': module['gap']}


def hint_references(c, subscription_id, through):
    """Unexpired references of a module_events hint: the type, the subject reference and the module only.

    Expiry is rechecked at every delivery attempt, so a reminder after its start or an owner request
    past its deadline is never announced, even after budget waits or retries.
    """
    module = c.execute('SELECT acked_through FROM subscription_module_streams WHERE subscription=%s',
        (subscription_id,)).fetchone()
    after = module['acked_through'] if module else through
    return c.execute('''SELECT e.seq,e.source,e.type,e.subject,e.urgency,e.expires_at,count(*) OVER () AS total
        FROM subscription_module_matches m JOIN module_events e ON e.seq=m.event
        WHERE m.subscription=%s AND m.event>%s AND m.event<=%s
        AND (e.expires_at IS NULL OR e.expires_at>clock_timestamp()) ORDER BY m.event LIMIT 20''',
        (subscription_id, after, through)).fetchall()


def resume(c, row, policy):
    """Module-aware activation: the fence trigger leaves the stream alone, and a held stream re-arms."""
    c.execute("SELECT set_config('messaging.module_aware','on',true)")
    module = c.execute('SELECT * FROM subscription_module_streams WHERE subscription=%s FOR UPDATE',
        (row['id'],)).fetchone()
    if module is None:
        return
    position, gap = module['matched_through'], module['gap']
    if policy == 'skip':
        head = c.execute('SELECT value FROM module_event_counter WHERE id=1').fetchone()['value']
        if position < head:
            gap = {'reason': 'explicit_resume_skip', 'stream': 'modules', 'after': position, 'through': head,
                'recoverable': False, 'intentional': True}
        position = head
    c.execute('''UPDATE subscription_module_streams SET rollback_hold=false,matched_through=%s,gap=%s,
        blocked_reason=NULL WHERE subscription=%s''', (position, Jsonb(gap) if gap else None, row['id']))


def prune(c, candidate, days, batch):
    """Delete ACKed module references older than the window as a contiguous prefix; move the floor.

    Runs inside subscriptions._prune_batch, after its locks and checks; returns rows deleted.
    """
    module = c.execute('''SELECT retention_floor,acked_through FROM subscription_module_streams
        WHERE subscription=%s FOR UPDATE''', (candidate,)).fetchone()
    if module is None:
        return 0
    refs = c.execute('''SELECT event,matched_at<clock_timestamp()-make_interval(days=>%s) AS expired
        FROM subscription_module_matches WHERE subscription=%s AND event>%s AND event<=%s ORDER BY event LIMIT %s''',
        (days, candidate, module['retention_floor'], module['acked_through'], batch)).fetchall()
    through = None
    for ref in refs:
        if not ref['expired']:
            break
        through = ref['event']
    if through is None:
        return 0
    deleted = c.execute('DELETE FROM subscription_module_matches WHERE subscription=%s AND event>%s AND event<=%s',
        (candidate, module['retention_floor'], through)).rowcount
    c.execute('UPDATE subscription_module_streams SET retention_floor=%s WHERE subscription=%s', (through, candidate))
    return deleted
