"""Retention of the subscription tables: pruning ACKed match references and retiring finished hints and
closed definitions. subscriptions.py keeps a function of each name that delegates here, and keeps the constants.
Names that module defines (RETENTION_*, TERMINAL_STATES, the helpers and the delegates themselves) are read
through it at call time, so tests that patch them there still see the patched value."""
import json

from psycopg.types.json import Jsonb

from . import subscriptions as _subs


def lock_retention(c, *, shared):
    """Advisory, so the worker needs no UPDATE right on probe_metadata just for a row lock."""
    if not shared:
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (_subs.RETENTION_LOCK,))
        return True
    # A queued disable makes this fail at once, so batches never starve the operator.
    return c.execute('SELECT pg_try_advisory_xact_lock_shared(hashtextextended(%s,0)) AS locked',
        (_subs.RETENTION_LOCK,)).fetchone()['locked']


def acked_retention_days():
    """Checked at worker startup, so an invalid value fails before any deletion."""
    return _subs._bound_setting('SUBSCRIPTION_ACKED_RETENTION_DAYS', 7, 1, 366)


def prune_subscriptions(store, *, max_subscriptions=16):
    """One bounded pass deleting old ACKed match references; never raises for a single batch.

    Only references at or below the consumer's acked_through and older than the retention window
    are deleted, as a contiguous prefix above the floor, and the floor moves in the same
    transaction. Held (quarantine-restored), closed or expired and passive-only subscriptions are
    never touched. Nothing is pruned until an operator enables retention (probe_metadata
    'subscription_retention'), after every serving reader reports retention_gap.
    """
    from psycopg import errors
    from .store import bounded
    bounded(max_subscriptions, 128)
    days = _subs.acked_retention_days()
    stats = {'pruned': 0, 'pruned_subscriptions': 0, 'retention_skipped': 0, 'retention_failed': 0}
    try:
        with store.pool.connection() as c:
            # The oldest retained ACKed reference is one primary-key probe per subscription.
            candidates = c.execute('''SELECT s.id,s.principal FROM subscriptions s
                JOIN subscription_consumption sc ON sc.subscription=s.id
                CROSS JOIN LATERAL (SELECT m.matched_at FROM subscription_matches m WHERE m.subscription=s.id
                    AND m.event>s.retention_floor AND m.event<=sc.acked_through ORDER BY m.event LIMIT 1) oldest
                WHERE NOT s.retention_hold AND s.state NOT IN ('closed','expired','access_changed')
                    AND (s.expires_at IS NULL OR s.expires_at>clock_timestamp()+%s::interval)
                    AND oldest.matched_at<clock_timestamp()-make_interval(days=>%s)
                    AND EXISTS(SELECT FROM probe_metadata WHERE name='subscription_retention' AND value='enabled')
                ORDER BY oldest.matched_at,s.id LIMIT %s''',
                (_subs.RETENTION_EXPIRY_MARGIN, days, max_subscriptions)).fetchall()
    except errors.UndefinedColumn:
        # Schema before 40: the migration precedes this worker, so nothing is pruned meanwhile.
        return stats
    try:
        with store.pool.connection() as c:
            from . import module_events
            # ANYM-562: the module stream keeps its own ACK position and floor; same window and rules.
            modules = c.execute('''SELECT s.id,s.principal,true AS modules FROM subscriptions s
                JOIN subscription_module_streams ms ON ms.subscription=s.id
                CROSS JOIN LATERAL (SELECT m.matched_at FROM subscription_module_matches m WHERE m.subscription=s.id
                    AND m.event>ms.retention_floor AND m.event<=ms.acked_through ORDER BY m.event LIMIT 1) oldest
                WHERE NOT s.retention_hold AND s.state NOT IN ('closed','expired','access_changed')
                    AND (s.expires_at IS NULL OR s.expires_at>clock_timestamp()+%s::interval)
                    AND oldest.matched_at<clock_timestamp()-make_interval(days=>%s)
                    AND EXISTS(SELECT FROM probe_metadata WHERE name='subscription_retention' AND value='enabled')
                ORDER BY oldest.matched_at,s.id LIMIT %s''',
                (_subs.RETENTION_EXPIRY_MARGIN, days, max_subscriptions)).fetchall() if module_events.ready(c) else []
    except Exception as error:
        from .telemetry import capture
        capture(error, 'subscription_retention_failed')
        modules = []
    for candidate in [*candidates, *modules]:
        try:
            with store.pool.connection() as c:
                deleted = _subs._prune_batch(c, candidate, days)
        except Exception as error:
            # The batch rolled back alone (deletion and floor together); matching continues.
            from .telemetry import capture
            capture(error, 'subscription_retention_failed')
            print(json.dumps({'error': 'subscription_retention_failed', 'class': type(error).__name__}), flush=True)
            stats['retention_failed'] += 1
            continue
        if deleted is None:
            stats['retention_skipped'] += 1
        elif deleted:
            stats['pruned'] += deleted
            stats['pruned_subscriptions'] += 1
    return stats


def _retention_batch(c, *gates):
    """The bounds and the operator activations of one retention transaction; False when one is off."""
    # Set first, so it bounds the expiry read through COMMIT: a stalled batch is rolled back.
    c.execute("SELECT set_config('transaction_timeout',%s,true)", (_subs.RETENTION_TRANSACTION_TIMEOUT,))
    # Bounds well under one matcher pass; a contended subscription waits for the next pass.
    c.execute("SET LOCAL lock_timeout='100ms'")
    c.execute("SET LOCAL statement_timeout='1s'")
    # The activation is reread under the shared lock: once --disable returns, no batch deletes.
    names = ['subscription_retention', *gates]
    return _subs.lock_retention(c, shared=True) and c.execute('''SELECT count(*)=%s AS enabled FROM probe_metadata
        WHERE name=ANY(%s) AND value='enabled' ''', (len(names), names)).fetchone()['enabled']


def _prune_batch(c, candidate, days):
    """One transaction. Returns rows deleted, or None when retention is off or the subscription
    is busy, held or past its deadline."""
    if not _subs._retention_batch(c):
        return None
    # The matcher's order: the shared principal lock, then the subscription row.
    _subs.lock_principal(c, candidate['principal'], shared=True)
    row = c.execute('''SELECT id,state,retention_floor,retention_hold,
        expires_at IS NOT NULL AND expires_at<=clock_timestamp()+%s::interval AS lapsed
        FROM subscriptions WHERE id=%s FOR UPDATE SKIP LOCKED''',
        (_subs.RETENTION_EXPIRY_MARGIN, candidate['id'])).fetchone()
    # A quarantine closes every definition: terminal ones keep their references (ANYM-446 F2) until
    # retire_subscriptions takes them 30 days after closure (ANYM-447). A passed deadline is terminal before the
    # matcher stores 'expired'.
    if row is None or row['retention_hold'] or row['lapsed'] or row['state'] in _subs.TERMINAL_STATES:
        return None
    if candidate.get('modules'):
        from .module_events import prune
        return prune(c, row['id'], days, _subs.RETENTION_BATCH)
    # ACK and fetch hold the subscription row, so acked_through cannot move under this batch.
    consumption = c.execute('SELECT acked_through FROM subscription_consumption WHERE subscription=%s',
        (row['id'],)).fetchone()
    if consumption is None:
        return 0
    refs = c.execute('''SELECT event,matched_at<clock_timestamp()-make_interval(days=>%s) AS expired
        FROM subscription_matches WHERE subscription=%s AND event>%s AND event<=%s ORDER BY event LIMIT %s''',
        (days, row['id'], row['retention_floor'], consumption['acked_through'], _subs.RETENTION_BATCH)).fetchall()
    # A contiguous prefix only: a newer reference stops the batch, so nothing above the floor is missing.
    through = None
    for ref in refs:
        if not ref['expired']:
            break
        through = ref['event']
    if through is None:
        return 0
    deleted = c.execute('DELETE FROM subscription_matches WHERE subscription=%s AND event>%s AND event<=%s',
        (row['id'], row['retention_floor'], through)).rowcount
    c.execute('UPDATE subscriptions SET retention_floor=%s WHERE id=%s', (through, row['id']))
    return deleted


def retire_subscriptions(store, *, max_subscriptions=16):
    """ANYM-447: one bounded pass deleting finished hints older than 30 days and, 30 days after closure, a closed,
    expired or access_changed definition's hints, matches, receipts and consumption; never raises for a single
    batch.

    Active and paused definitions lose only finished hints, whose per-state counts move to deleted_hint_counts.
    A closed definition ends as a tombstone whose floor is its matched_through. Held (quarantine-restored)
    definitions are skipped until the operator's release. Same lock and bounds as prune_subscriptions, under
    both activations (TOMBSTONE_GATE is set only once every component runs schema 62); `more` says a batch
    reached its bound, so the worker runs the next pass at once.
    """
    from psycopg import errors
    from .store import bounded
    bounded(max_subscriptions, 128)
    stats = {'hints_deleted': 0, 'rows_deleted': 0, 'tombstoned': 0, 'retire_skipped': 0, 'retire_failed': 0, 'activity_deleted': 0,
        'more': False}
    gates = ['subscription_retention', _subs.TOMBSTONE_GATE]
    try:
        with store.pool.connection() as c:
            candidates = c.execute('''SELECT id,principal,true AS closed FROM subscriptions
                WHERE state IN ('closed','expired','access_changed') AND NOT retention_hold AND tombstoned_at IS NULL
                    AND closed_at<clock_timestamp()-make_interval(days=>%s)
                    AND (SELECT count(*) FROM probe_metadata WHERE name=ANY(%s) AND value='enabled')=2
                ORDER BY closed_at,id LIMIT %s''', (_subs.CLOSED_RETENTION_DAYS, gates, max_subscriptions)).fetchall()
            # One probe per definition on the outbox's (subscription,...) unique key; test hints stay (below).
            candidates += c.execute('''SELECT s.id,s.principal,false AS closed FROM subscriptions s
                WHERE s.state IN ('active','paused') AND NOT s.retention_hold
                    AND EXISTS(SELECT FROM subscription_outbox o WHERE o.subscription=s.id AND o.kind<>'test'
                        AND o.state IN ('delivered','cancelled','dead_letter')
                        AND o.finished_at<clock_timestamp()-make_interval(days=>%s))
                    AND (SELECT count(*) FROM probe_metadata WHERE name=ANY(%s) AND value='enabled')=2
                ORDER BY s.id LIMIT %s''', (_subs.CLOSED_RETENTION_DAYS, gates, max_subscriptions)).fetchall()
    except errors.UndefinedColumn:
        return stats  # Schema before 62: the migration precedes this worker, so nothing is deleted meanwhile.
    for candidate in candidates:
        try:
            with store.pool.connection() as c:
                result = (_subs._retire_definition if candidate['closed'] else _subs._retire_hints)(c, candidate)
        except Exception as error:
            # The batch rolled back alone (deletions, floors and counters together); matching continues.
            from .telemetry import capture
            capture(error, 'subscription_retention_failed')
            print(json.dumps({'error': 'subscription_retention_failed', 'class': type(error).__name__}), flush=True)
            stats['retire_failed'] += 1
            continue
        if result is None:
            stats['retire_skipped'] += 1
            continue
        stats['hints_deleted'] += result['hints']
        stats['rows_deleted'] += result['rows']
        stats['tombstoned'] += result['tombstoned']
        stats['more'] = stats['more'] or result['more']
    try:
        with store.pool.connection() as c:
            deleted = _prune_activity(c)
    except errors.UndefinedTable:
        deleted = None  # Schema before 70: nothing to delete.
    except Exception as error:
        from .telemetry import capture
        capture(error, 'subscription_retention_failed')
        print(json.dumps({'error': 'subscription_retention_failed', 'class': type(error).__name__}), flush=True)
        stats['retire_failed'] += 1
        deleted = None
    if deleted:
        stats['activity_deleted'] = deleted
        stats['more'] = stats['more'] or deleted >= _subs.RETENTION_BATCH
    return stats


ACTIVITY_RETENTION_DAYS = 35


def _prune_activity(c):
    """ANYM-824: one bounded batch of per-day matcher counts older than 35 UTC days; a held (quarantine-restored)
    definition keeps its rows. Same activation, lock and bounds as the other batches; None when retention is off."""
    if not _subs._retention_batch(c):
        return None
    return c.execute('''DELETE FROM subscription_activity a USING (SELECT x.subscription,x.day,x.platform,
            x.scope_version,x.metric FROM subscription_activity x JOIN subscriptions s ON s.id=x.subscription
            WHERE NOT s.retention_hold AND x.day<(clock_timestamp() AT TIME ZONE 'UTC')::date-%s LIMIT %s) old
        WHERE a.subscription=old.subscription AND a.day=old.day AND a.platform=old.platform
            AND a.scope_version=old.scope_version AND a.metric=old.metric''',
        (ACTIVITY_RETENTION_DAYS, _subs.RETENTION_BATCH)).rowcount


def _delete_hints(c, row, limit, *, days=None):
    """Delete up to limit finished hints (finished more than days ago, if given); their counts the key could see
    move to deleted_hint_counts in the same transaction. Returns rows deleted.

    Bounding events hints go only as a prefix by position, at or below every one that stays, and
    hints_deleted_through takes the highest deleted position no kept hint bounds, so each kept hint's range
    (hint_floor) and hint_visible stay exactly as they were. With days (an open definition) test hints stay: their request IDs replay."""
    finished = _subs._FINISHED + ' AND (%s::integer IS NULL OR finished_at<clock_timestamp()-make_interval(days=>%s))'
    # No row lock first: the worker role has no UPDATE on the outbox, and finished rows no longer change.
    keep = c.execute('SELECT min(through_position) AS p FROM subscription_outbox WHERE subscription=%s AND ' +
        _subs._BOUNDING + ' AND NOT (' + finished + ')', (row['id'], days, days)).fetchone()['p']
    rows = c.execute('SELECT id,through_position,' + _subs._BOUNDING + ''' AS bounding FROM subscription_outbox
        WHERE subscription=%s AND ''' + finished + ''' AND (%s OR kind<>'test')
        AND (NOT (''' + _subs._BOUNDING + ''') OR %s::bigint IS NULL OR through_position<%s)
        ORDER BY through_position,id LIMIT %s''', (row['id'], days, days, days is None, keep, keep, limit)).fetchall()
    if not rows:
        return 0
    ids = [item['id'] for item in rows]
    # The view's own filter at deletion time, references still present, so hint_counts never drops.
    counts = _subs._hint_counts(c, row, _subs._worker_identity(c, row) if _subs.dynamic(row) else None, ids)
    deleted = c.execute('DELETE FROM subscription_outbox WHERE id=ANY(%s) AND ' + _subs._FINISHED, (ids,)).rowcount
    kept = row['deleted_hint_counts']
    positions = {item['through_position'] for item in rows if item['bounding']}
    # The limit may cut inside equal positions: the floor then stays below the position a kept hint still bounds,
    # so its range starts where it did, and a later batch takes the rest of that position.
    if positions and c.execute('SELECT EXISTS(SELECT FROM subscription_outbox WHERE subscription=%s AND ' + _subs._BOUNDING +
            ' AND through_position=%s) AS left', (row['id'], max(positions))).fetchone()['left']:
        positions.remove(max(positions))
    through = max(positions, default=0)
    c.execute('''UPDATE subscriptions SET deleted_hint_counts=%s,hints_deleted_at=clock_timestamp(),
        hints_deleted_through=greatest(hints_deleted_through,%s) WHERE id=%s''',
        (Jsonb({state: kept.get(state, 0) + counts.get(state, 0) for state in sorted({*kept, *counts})}), through,
         row['id']))
    return deleted


def _retire_hints(c, candidate):
    """One transaction: finished hints older than 30 days of an active or paused definition."""
    if not _subs._retention_batch(c, _subs.TOMBSTONE_GATE):
        return None
    # The matcher's order: the shared principal lock, then the subscription row.
    _subs.lock_principal(c, candidate['principal'], shared=True)
    row = c.execute('SELECT * FROM subscriptions WHERE id=%s FOR UPDATE SKIP LOCKED', (candidate['id'],)).fetchone()
    # Rechecked under the row lock: a hold or a closure since selection wins.
    if row is None or row['retention_hold'] or row['state'] not in ('active', 'paused'):
        return None
    hints = _subs._delete_hints(c, row, _subs.RETENTION_BATCH, days=_subs.CLOSED_RETENTION_DAYS)
    return {'hints': hints, 'rows': hints, 'tombstoned': 0, 'more': hints >= _subs.RETENTION_BATCH}


def _retire_definition(c, candidate):
    """One transaction of at most RETENTION_BATCH rows of a definition closed more than 30 days ago: finished hints
    first (counted while their references exist); once no hint is left, matches from the bottom (the floor follows,
    so a reader in between gets retention_gap) and module matches; then receipts and consumption, and the floor
    becomes matched_through (the tombstone)."""
    if not _subs._retention_batch(c, _subs.TOMBSTONE_GATE):
        return None
    _subs.lock_principal(c, candidate['principal'], shared=True)
    row = c.execute('''SELECT *,closed_at<clock_timestamp()-make_interval(days=>%s) AS due FROM subscriptions
        WHERE id=%s FOR UPDATE SKIP LOCKED''', (_subs.CLOSED_RETENTION_DAYS, candidate['id'])).fetchone()
    if (row is None or row['retention_hold'] or not row['due'] or row['tombstoned_at'] is not None or
            row['state'] not in _subs.TERMINAL_STATES):
        return None
    hints = _subs._delete_hints(c, row, _subs.RETENTION_BATCH)
    budget = _subs.RETENTION_BATCH - hints
    result = {'hints': hints, 'rows': hints, 'tombstoned': 0, 'more': budget <= 0}
    # An unfinished hint waits for the dispatcher, which settles a closed definition's hints first.
    if budget <= 0 or c.execute('SELECT EXISTS(SELECT FROM subscription_outbox WHERE subscription=%s) AS left',
            (row['id'],)).fetchone()['left']:
        return result
    events = [item['event'] for item in c.execute('''DELETE FROM subscription_matches WHERE subscription=%s
        AND event IN (SELECT event FROM subscription_matches WHERE subscription=%s ORDER BY event LIMIT %s)
        RETURNING event''', (row['id'], row['id'], budget)).fetchall()]
    if events:
        c.execute('UPDATE subscriptions SET retention_floor=greatest(retention_floor,%s) WHERE id=%s',
            (max(events), row['id']))
    budget -= len(events)
    from . import module_events
    module = module_events.ready(c) and module_events.stream(c, row['id'])
    if module and budget > 0:
        # ANYM-562: the module stream's references, with its own floor.
        seqs = [item['event'] for item in c.execute('''DELETE FROM subscription_module_matches WHERE subscription=%s
            AND event IN (SELECT event FROM subscription_module_matches WHERE subscription=%s ORDER BY event LIMIT %s)
            RETURNING event''', (row['id'], row['id'], budget)).fetchall()]
        if seqs:
            c.execute('''UPDATE subscription_module_streams SET retention_floor=greatest(retention_floor,%s)
                WHERE subscription=%s''', (max(seqs), row['id']))
        budget -= len(seqs)
    if budget > 0 and _subs.activity_ready(c):
        # ANYM-824: the tombstone keeps no per-day counts; the parent row stays, so they go explicitly.
        budget -= c.execute('''DELETE FROM subscription_activity a USING (SELECT day,platform,scope_version,metric
            FROM subscription_activity WHERE subscription=%s LIMIT %s) old WHERE a.subscription=%s AND a.day=old.day
            AND a.platform=old.platform AND a.scope_version=old.scope_version AND a.metric=old.metric''',
            (row['id'], budget, row['id'])).rowcount
    result['rows'] = _subs.RETENTION_BATCH - budget
    # Each DELETE took up to the budget left, so a batch with budget left drained both reference tables.
    # Receipts (at most 128 per definition) and consumption count toward the batch too; a fresh batch takes them.
    tail = c.execute('''SELECT (SELECT count(*) FROM subscription_receipts WHERE subscription=%s)+
        (SELECT count(*) FROM subscription_consumption WHERE subscription=%s) AS n''', (row['id'], row['id'])).fetchone()['n']
    if budget <= 0 or tail > budget and budget < _subs.RETENTION_BATCH:
        result['more'] = True
        return result
    result['rows'] += c.execute('DELETE FROM subscription_receipts WHERE subscription=%s', (row['id'],)).rowcount
    result['rows'] += c.execute('DELETE FROM subscription_consumption WHERE subscription=%s', (row['id'],)).rowcount
    if module:
        c.execute('''UPDATE subscription_module_streams SET retention_floor=greatest(retention_floor,matched_through)
            WHERE subscription=%s''', (row['id'],))
    c.execute('''UPDATE subscriptions SET retention_floor=greatest(retention_floor,matched_through),
        tombstoned_at=clock_timestamp() WHERE id=%s''', (row['id'],))
    result['tombstoned'] = 1
    return result
