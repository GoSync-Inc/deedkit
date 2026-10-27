"""Per-subscription hint timing: debounce, batch, quiet hours, silence timers, per-key budget.

A policy decides only when the matcher commits a hint. It never changes which events match,
ACK positions or what passive reads return. Every deadline uses the database clock. Held
state is one durable row per subscription, so a restarted or second worker re-plans the same
row, and outbox uniqueness on (subscription, version, kind, window) prevents a double emit.
"""
from __future__ import annotations

from datetime import timedelta
import re

from psycopg.types.json import Jsonb

from .subscriptions import _bound_setting, _state, _worker_identity

WEEKDAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
BYPASS = ('incoming_message', 'owner_mention', 'reply_to_owner', 'reply_to_confirmed_send', 'silence')
LIMITS = {'debounce_s': (0, 600), 'max_wait_s': (1, 3600), 'batch_window_s': (0, 86400),
    'batch_count': (1, 1000), 'expect_reply_within_s': (60, 14 * 86400)}
HELD_REASONS = ('debounce', 'batch', 'quiet_hours', 'budget')
UNBOUND = {'policy': None, 'held_reason': None, 'next_hint_at': None, 'silence': None}
_CLOCK = re.compile(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]')
_ZONE = re.compile(r'[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+){0,2}')
_EMPTY = dict(first_match_at=None, last_match_at=None, match_count=0, through_position=None,
    reasons=[], due_at=None, held_reason=None)
_NO_SILENCE = dict(silence_state=None, silence_fired_at=None, silence_due_at=None, silence_held_reason=None)


def normalize_policy(c, policy, selector):
    """Bounded, validated policy or None. Unset (None) fields mean the default."""
    from .store import APIError
    invalid = APIError('invalid_delivery_policy')
    if policy is None:
        return None
    if not isinstance(policy, dict):
        raise invalid
    policy = {key: value for key, value in policy.items() if value is not None}
    if set(policy) - {*LIMITS, 'quiet_hours', 'quiet_bypass'}:
        raise invalid
    result = {}
    for name, (low, high) in LIMITS.items():
        if name in policy:
            if type(policy[name]) is not int or not low <= policy[name] <= high:
                raise invalid
            result[name] = policy[name]
    debounce = result.get('debounce_s', 0)
    if debounce and (result.get('batch_window_s') or 'batch_count' in result):
        raise invalid
    if debounce or 'batch_count' in result or 'max_wait_s' in result:
        result['max_wait_s'] = result.get('max_wait_s', max(300, debounce))
        if result['max_wait_s'] < debounce:
            raise invalid
    quiet = policy.get('quiet_hours')
    if quiet is not None:
        zone = quiet.get('timezone') if isinstance(quiet, dict) else None
        if (not isinstance(quiet, dict) or set(quiet) != {'timezone', 'windows'} or not isinstance(zone, str) or
                len(zone) > 64 or not _ZONE.fullmatch(zone) or not isinstance(quiet['windows'], list) or
                not 1 <= len(quiet['windows']) <= 4):
            raise invalid
        windows = []
        for item in quiet['windows']:
            if (not isinstance(item, dict) or set(item) != {'days', 'start', 'end'} or
                    not isinstance(item['days'], list) or not 1 <= len(item['days']) <= 7 or
                    any(not isinstance(day, str) or day not in WEEKDAYS for day in item['days']) or
                    len(set(item['days'])) != len(item['days']) or
                    any(not isinstance(item[key], str) or not _CLOCK.fullmatch(item[key]) for key in ('start', 'end')) or
                    item['start'] == item['end']):
                raise invalid
            windows.append({'days': sorted(item['days'], key=WEEKDAYS.index),
                'start': item['start'], 'end': item['end']})
        if c.execute('SELECT 1 FROM pg_timezone_names WHERE name=%s', (zone,)).fetchone() is None:
            raise invalid
        result['quiet_hours'] = {'timezone': zone, 'windows': windows}
    bypass = policy.get('quiet_bypass')
    if bypass is not None:
        if ('quiet_hours' not in result or not isinstance(bypass, list) or not 1 <= len(bypass) <= len(BYPASS) or
                any(not isinstance(reason, str) or reason not in BYPASS for reason in bypass) or
                len(set(bypass)) != len(bypass)):
            raise invalid
        result['quiet_bypass'] = sorted(bypass, key=BYPASS.index)
    if 'expect_reply_within_s' in result and not (selector or {}).get('reply_anchor'):
        raise invalid
    return result or None


def quiet_until(c, policy, at):
    """End of the quiet time containing at, or None (PostgreSQL tz rules, first occurrence)."""
    quiet = (policy or {}).get('quiet_hours')
    if not quiet:
        return None
    windows = [{**item, 'days': [WEEKDAYS.index(day) + 1 for day in item['days']]} for item in quiet['windows']]
    return c.execute('SELECT subscription_quiet_until(%s,%s) AS t',
        (Jsonb({'timezone': quiet['timezone'], 'windows': windows}), at)).fetchone()['t']


def _slot(c, owner, column, now):
    limit = _bound_setting('SUBSCRIPTION_MAX_HINTS_PER_HOUR', 20, 1, 1000)
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('subscription-hints:' + str(owner['key_id']),))
    charged = c.execute('''SELECT o.{0} AS t FROM subscription_outbox o JOIN subscriptions s ON s.id=o.subscription
        WHERE s.principal=%s AND s.key_id=%s AND o.{0}>%s-interval '1 hour'
        ORDER BY o.{0} DESC OFFSET %s LIMIT 1'''.format(column),
        (owner['principal'], owner['key_id'], now, limit - 1)).fetchone()
    return None if charged is None else charged['t'] + timedelta(hours=1)


def budget_slot(c, row, now):
    """None if the key may commit a hint now, else when its next hourly slot opens.

    Every outbox row of the key counts once, at creation, in the transaction that commits it;
    the per-key lock serializes the count with that insert across workers and the gateway.
    """
    return _slot(c, row, 'created_at', now)


def admission_slot(c, owner, now):
    """Dispatcher side of the same budget: None if a hint ID may make its first POST now.

    Counts the key's hints admitted in the rolling hour, so a backlog after downtime still
    sends at most the budget of distinct hints per hour. Retries are not new admissions.
    """
    return _slot(c, owner, 'admitted_at', now)


def _now(c):
    return c.execute('SELECT clock_timestamp() AS t').fetchone()['t']


def _binding(c, row, *, stageable=True):
    # Every column: before schema 49 there is no endpoint column (rolling upgrade).
    binding = c.execute('SELECT * FROM subscription_delivery WHERE subscription=%s', (row['id'],)).fetchone()
    # ANYM-519: an endpoint binding stages nothing while the endpoint is not enabled or the gate is off.
    if stageable and binding is not None and binding.get('endpoint') is not None:
        from .delivery_endpoints import endpoint_stageable
        return binding if endpoint_stageable(c, binding['endpoint']) else None
    return binding


def _armed(row, binding):
    # A timer belongs to the configuration that set it; any later version change cancels it.
    return binding['silence_deadline'] is not None and binding['policy_version'] == row['version']


def _window(c, row):
    # No row means no hint step ever committed (migration 037 backfilled existing subscriptions),
    # so every committed match above the subscription's start is still unhinted.
    c.execute('''INSERT INTO subscription_hint_windows(subscription,subscription_version,hinted_through)
        VALUES(%s,%s,%s) ON CONFLICT DO NOTHING''', (row['id'], row['version'], row['start_position']))
    window = c.execute('SELECT * FROM subscription_hint_windows WHERE subscription=%s FOR UPDATE',
        (row['id'],)).fetchone()
    if window['subscription_version'] != row['version']:
        # Held hints of an older version are cancelled, not retried; the window counter never goes back.
        window.update(_EMPTY, **_NO_SILENCE, subscription_version=row['version'],
            hinted_through=max(window['hinted_through'] or 0, window['through_position'] or 0))
    return window


def _save(c, window):
    c.execute('''UPDATE subscription_hint_windows SET subscription_version=%(subscription_version)s,
        next_window=%(next_window)s,first_match_at=%(first_match_at)s,last_match_at=%(last_match_at)s,
        match_count=%(match_count)s,through_position=%(through_position)s,reasons=%(reasons)s,
        hinted_through=%(hinted_through)s,
        due_at=%(due_at)s,held_reason=%(held_reason)s,silence_state=%(silence_state)s,
        silence_fired_at=%(silence_fired_at)s,silence_due_at=%(silence_due_at)s,
        silence_held_reason=%(silence_held_reason)s,updated_at=clock_timestamp()
        WHERE subscription=%(subscription)s''', window)


def _due(policy, window):
    first, last = window['first_match_at'], window['last_match_at']
    if policy.get('debounce_s'):
        return min(last + timedelta(seconds=policy['debounce_s']),
            first + timedelta(seconds=policy['max_wait_s'])), 'debounce'
    if policy.get('batch_window_s') or policy.get('batch_count'):
        if policy.get('batch_count') and window['match_count'] >= policy['batch_count']:
            return last, 'batch'
        return first + timedelta(seconds=policy.get('batch_window_s') or policy['max_wait_s']), 'batch'
    return first, None


def _gate(c, row, policy, due, reason, bypass, now):
    """(None, None) if a due hint may be committed now, else (next time, held reason).

    Aggregation sets due; quiet hours hold a due hint to the window end (even past max_wait);
    the budget holds it to the next slot. Each tick re-plans, so an overdue hint fires once.
    """
    if not bypass:
        quiet = quiet_until(c, policy, max(due, now))
        if quiet is not None:
            return quiet, 'quiet_hours'
    if due > now:
        return due, reason
    slot = budget_slot(c, row, now)
    return (slot, 'budget') if slot else (None, None)


def _bypass(policy, reasons):
    return bool(set(reasons) & set(policy.get('quiet_bypass', ())))


def _emit(c, row, binding, window, kind, through, bypass):
    # The target column is a constant: receiver, or (schema 49) endpoint.
    target = 'receiver' if binding['receiver'] is not None else 'endpoint'
    hint = c.execute('''INSERT INTO subscription_outbox(subscription,subscription_version,''' + target + ''',
        through_position,kind,window_id,quiet_bypass) VALUES(%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT DO NOTHING RETURNING id''',
        (row['id'], row['version'], binding[target], through, kind, window['next_window'], bypass)).fetchone()
    window['next_window'] += 1
    return hint is not None


def _settle(c, row, binding, window, now, through):
    policy = binding['policy'] or {}
    emitted = 0
    if window['first_match_at'] is not None:
        due, reason = _due(policy, window)
        # The current policy decides the bypass from the range's reasons, also after a reconfiguration.
        bypass = _bypass(policy, window['reasons'])
        window['due_at'], window['held_reason'] = _gate(c, row, policy, due, reason, bypass, now)
        if window['due_at'] is None:
            emitted += _emit(c, row, binding, window, 'events', window['through_position'], bypass)
            window['hinted_through'] = max(window['hinted_through'] or 0, window['through_position'])
            window.update(_EMPTY)
    if _armed(row, binding):
        if window['silence_state'] is None and now >= binding['silence_deadline']:
            # The committed silence reference: anchor and deadline only, no content.
            window['silence_state'], window['silence_fired_at'] = 'fired', now
        if window['silence_state'] == 'fired':
            bypass = 'silence' in policy.get('quiet_bypass', ())
            window['silence_due_at'], window['silence_held_reason'] = _gate(c, row, policy, now, None, bypass, now)
            if window['silence_due_at'] is None:
                emitted += _emit(c, row, binding, window, 'silence', through, bypass)
                window['silence_state'] = 'hinted'
    return emitted


def _absorb(c, row, binding, window, *, limit=1000, pages=16):
    """Fold every committed match above the held range into it; False if pages ran out first.

    The matcher and the recovery tick share this path, so a failed hint step loses no reason,
    count, match time or on-time reply: the next tick replays the same committed matches. A
    caller settles nothing on False: the progress is saved and the next tick continues.
    """
    for _ in range(pages):
        if _absorb_page(c, row, binding, window, limit) < limit:
            return True
    # A last full page may have taken the final match: only a match still above the range defers
    # settling, and that match keeps the subscription a recovery candidate for the next tick.
    return c.execute('SELECT 1 FROM subscription_matches WHERE subscription=%s AND event>%s LIMIT 1',
        (row['id'], window['through_position'])).fetchone() is None


def _absorb_page(c, row, binding, window, limit):
    start = window['through_position']
    if start is None:
        start = window['hinted_through'] if window['hinted_through'] is not None else row['start_position']
    matches = c.execute('''SELECT m.event,m.matched_at,e.account,e.kind,e.origin,
        jsonb_strip_nulls(jsonb_build_object('chat',e.snapshot->'chat',
            'sender',e.snapshot->'sender','direction',e.snapshot->'direction',
            'metadata',e.snapshot->'metadata','reply_evidence',e.snapshot->'reply_evidence')) AS snapshot
        FROM subscription_matches m JOIN events e ON e.id=m.event
        WHERE m.subscription=%s AND m.event>%s ORDER BY m.event LIMIT %s''',
        (row['id'], start, limit)).fetchall()
    if not matches:
        return 0
    from .subscription_consumption import wake_reason
    reasons = set(window['reasons'])
    for item in matches:
        found = wake_reason({**item['snapshot'], 'account': item['account'], 'kind': item['kind'],
            'historical': item['origin'] in ('backfill', 'reconcile')}, row['selector'])
        reasons.update(found)
        if (binding is not None and _armed(row, binding) and window['silence_state'] is None and
                'reply_to_confirmed_send' in found and
                binding['configured_at'] <= item['matched_at'] < binding['silence_deadline']):
            window['silence_state'] = 'replied'
    if window['first_match_at'] is None:
        window['first_match_at'] = matches[0]['matched_at']
    window['last_match_at'] = max(window['last_match_at'] or matches[-1]['matched_at'], matches[-1]['matched_at'])
    window['match_count'] = min(1000000, window['match_count'] + len(matches))
    window['through_position'] = matches[-1]['event']
    window['reasons'] = sorted(reasons)[:16]
    return len(matches)


def after_match(c, row, through):
    """Matcher transaction, after new matches: hold or commit the hint. Returns hints committed."""
    binding = _binding(c, row)
    window = _window(c, row)
    if binding is None:
        # Nothing to hint: these matches are handled, so a later binding does not reopen them.
        window['hinted_through'] = max(window['hinted_through'] or 0, through)
        _save(c, window)
        return 0
    emitted = _settle(c, row, binding, window, _now(c), through) if _absorb(c, row, binding, window) else 0
    _save(c, window)
    return emitted


def flush_hints(store, *, limit=32):
    """One bounded pass over due held hints and expired silence timers; safe with several workers."""
    from .store import bounded
    bounded(limit, 128)
    stats = dict(emitted=0, held=0, cancelled=0)
    with store.pool.connection() as c:
        candidates = c.execute('''SELECT id,principal,key_id FROM subscriptions WHERE id IN (
            SELECT subscription FROM subscription_hint_windows
            WHERE due_at<=clock_timestamp() OR silence_due_at<=clock_timestamp()
            UNION SELECT d.subscription FROM subscription_delivery d JOIN subscriptions s ON s.id=d.subscription
            LEFT JOIN subscription_hint_windows w ON w.subscription=d.subscription AND w.subscription_version=s.version
            WHERE d.silence_deadline<=clock_timestamp() AND s.state='active' AND s.version=d.policy_version
            AND w.silence_state IS NULL
            UNION SELECT s.id FROM subscriptions s JOIN subscription_delivery d ON d.subscription=s.id
            LEFT JOIN subscription_hint_windows w ON w.subscription=s.id
            WHERE s.state='active' AND EXISTS(SELECT 1 FROM subscription_matches m WHERE m.subscription=s.id
                AND m.event>coalesce(CASE WHEN w.subscription_version=s.version THEN w.through_position END,
                    w.hinted_through,s.start_position)))
            ORDER BY id LIMIT %s''', (limit,)).fetchall()
    for candidate in candidates:
        with store.pool.connection() as c:
            # Same ACL fence as matching and dispatch: the shared principal lock precedes the
            # subscription lock, so a revocation cannot commit between this check and the insert.
            p = _worker_identity(c, candidate)
            row = c.execute('SELECT * FROM subscriptions WHERE id=%s FOR UPDATE SKIP LOCKED',
                (candidate['id'],)).fetchone()
            if row is None:
                continue
            if p is not None:
                row = _state(c, row, p)
            binding = _binding(c, row)
            # A missing row means no hint step ever committed for this subscription.
            window = _window(c, row)
            if p is None or row['state'] != 'active' or binding is None:
                # Cancelled, not deferred: these matches are never hinted, so they are not reopened.
                window.update(_EMPTY, silence_due_at=None, silence_held_reason=None,
                    hinted_through=max(window['hinted_through'] or 0, row['matched_through']))
                if window['silence_state'] in (None, 'fired'):
                    window['silence_state'] = 'cancelled'
                _save(c, window)
                stats['cancelled'] += 1
                continue
            # A failed hint step left committed matches outside the held range: replay all of them
            # before any range or silence timer is settled.
            if _absorb(c, row, binding, window):
                stats['emitted'] += _settle(c, row, binding, window, _now(c), row['matched_through'])
            stats['held'] += window['due_at'] is not None or window['silence_due_at'] is not None
            _save(c, window)
    return stats


def carry_window(c, row, previous_version):
    """Same-receiver reconfiguration: keep the held range under the new version, re-plan it now."""
    c.execute('''UPDATE subscription_hint_windows SET subscription_version=%s,
        due_at=CASE WHEN first_match_at IS NULL THEN NULL ELSE clock_timestamp() END,
        silence_state=NULL,silence_fired_at=NULL,silence_due_at=NULL,silence_held_reason=NULL,
        updated_at=clock_timestamp() WHERE subscription=%s AND subscription_version=%s''',
        (row['version'], row['id'], previous_version))


def delivery_state(c, row):
    """Content-free policy, held state and silence timer for get_subscription."""
    binding = _binding(c, row, stageable=False)
    if binding is None:
        return dict(UNBOUND)
    window = c.execute('SELECT * FROM subscription_hint_windows WHERE subscription=%s', (row['id'],)).fetchone()
    if window is None or window['subscription_version'] != row['version'] or row['state'] != 'active':
        window = None
    held = [(window[at], window[reason]) for at, reason in
        (('due_at', 'held_reason'), ('silence_due_at', 'silence_held_reason'))] if window else []
    held = sorted(item for item in held if item[0] is not None)
    silence = None
    if binding['silence_deadline'] is not None:
        if window and window['silence_state']:
            state = window['silence_state']
        else:
            state = 'pending' if _armed(row, binding) and row['state'] == 'active' else 'cancelled'
        silence = {'send_id': row['selector'].get('reply_anchor', {}).get('send_id'),
            'deadline': binding['silence_deadline'], 'state': state,
            'fired_at': window['silence_fired_at'] if window else None}
    return {'policy': binding['policy'], 'held_reason': held[0][1] if held else None,
        'next_hint_at': held[0][0] if held else None, 'silence': silence}
