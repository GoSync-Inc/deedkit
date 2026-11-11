"""Bounded key-owned subscriptions over the committed archive journal.

This matcher atomically enqueues registered opaque hints; it never runs agents.
Definitions and exact journal references are durable; the independent matcher
advances each checkpoint in the same transaction as its matching references.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
import json
import math
import os
from uuid import UUID

from psycopg.types.json import Jsonb

from .policy import effective_permissions, lock_principal, targets

EVENT_KINDS = frozenset(('message.created', 'message.edited', 'message.deleted', 'poll.updated',
    'message.status', 'attachment.ready', 'file.text.ready', 'file.transcript.ready',
    'file.description.ready', 'file.processing.failed', 'send.failed', 'source.health', 'attachment.failed', 'call',
    'contact.birthday'))
# ANYM-867: a kind a selector receives without naming it, because it ends the same wait as a kind it names.
IMPLIED_KINDS = {'attachment.failed': 'attachment.ready'}
# ANYM-535: journal rows that name no archived message, matched by their own rule and never by default; ANYM-919's
# contact.birthday names a private chat and reaches a definition that selects that chat (not a dynamic exclusion).
FAILURE_KINDS = frozenset(('send.failed', 'source.health', 'contact.birthday'))
# A journal write the send or collector path survived, per kind, since process start.
JOURNAL_FAILURES = Counter()
ORIGINS = frozenset(('live', 'live_recovered', 'fixture', 'backfill', 'reconcile'))
# ANYM-733: a message the live stream lost and a reconcile pass first stored soon after is live for a subscription. The
# journal keeps origin `reconcile` (the feed and an older matcher read it unchanged); `view_origin` derives
# `live_recovered` from the event's own times, so it needs no stored value, schema or writer change.
RECOVERED_WINDOW = timedelta(hours=6)
RECOVERED_SELECTORS = frozenset(('live', 'live_recovered'))


def view_origin(origin, kind, sent_at, committed_at):
    """The origin a subscription sees: `live_recovered` for a reconcile-created message committed 0 to 6 h after it
    was sent; the stored origin otherwise, also for a missing, naive or unparsable sent_at."""
    if origin != 'reconcile' or kind != 'message.created' or not isinstance(sent_at, str):
        return origin
    try:
        age = committed_at - datetime.fromisoformat(sent_at)
    except (TypeError, ValueError):
        return origin
    return 'live_recovered' if timedelta(0) <= age <= RECOVERED_WINDOW else origin


def seen_origin(selected, origin, kind, sent_at, committed_at):
    """view_origin for a subscription whose selector lists `selected`: one that lists `reconcile` asked for the stored
    origin and keeps it (historical, as before); every other selector sees `live_recovered`. Exact for any matched event:
    a reconcile-origin message that matched a selector without `reconcile` matched as recovered."""
    return origin if 'reconcile' in selected else view_origin(origin, kind, sent_at, committed_at)


def origin_selected(origin, selected):
    return not RECOVERED_SELECTORS.isdisjoint(selected) if origin == 'live_recovered' else origin in selected


def quiet_group_change(snapshot):
    """ANYM-869: a WhatsApp group change row (rename, join, add...) wakes no subscription; one 300-member group's
    joins would wake every agent. Only `joined_group` (a new chat for the owner) wakes as a message does. The
    opt-in for the other changes belongs to the service-kinds card (ANYM-857). Rows of any other kind are untouched."""
    group = (snapshot.get('metadata') or {}).get('group_change')
    return isinstance(group, dict) and group.get('change') != 'joined_group'


def call_outcome(snapshot):
    """ANYM-923: a WhatsApp call row's outcome, else None. Such a row wakes only a selector whose kinds list `call`,
    and only for a missed call; `message.created` alone never wakes on a call."""
    call = (snapshot.get('metadata') or {}).get('call')
    return call.get('outcome') if isinstance(call, dict) else None


WAKE_RULES = frozenset(('incoming_message', 'owner_mention', 'reply_to_owner', 'reply_to_confirmed_send',
    'reply_to_own_send'))
# ANYM-522: the operator's switch for dynamic sources (set-dynamic-subscriptions); creation reads it under
# the shared lock, so --disable waits for every creation that read it enabled.
DYNAMIC_GATE = 'dynamic_subscriptions'
DYNAMIC_LOCK = 'dynamic-subscriptions'
SOURCE_OPTIONS = frozenset(('all_visible_chats', 'account_ids', 'include_channels', 'include_bots', 'chat_types',
    'exclude_chats'))
# ANYM-825: the chat types of a dynamic definition (the ANYM-272 vocabulary; an untyped chat is unknown) and
# the operator's second switch (set-source-filters), read and fenced like the dynamic one.
CHAT_TYPES = frozenset(('private', 'group', 'channel', 'unknown'))
SOURCE_FILTER_GATE = 'source_filters'
SOURCE_FILTER_LOCK = 'source-filters'
# ANYM-830: chats a dynamic definition leaves out ({account, chat} pairs the key can see), behind a third switch
# (set-source-exclusions); `exclusions_revision` in the sources is the list's own conflict counter.
SOURCE_EXCLUSION_GATE = 'source_exclusions'
SOURCE_EXCLUSION_LOCK = 'source-exclusions'
EXCLUDE_CHATS_MAX = 40
EXCLUDE_CHATS_BYTES = 6000


def _bound_setting(name, default, minimum, maximum):
    """Operator configuration fails closed instead of disabling a work bound."""
    value = os.environ.get(name, str(default))
    if not value.isdecimal() or not minimum <= int(value) <= maximum:
        raise ValueError('invalid_subscription_setting')
    return int(value)


def _ids(values, *, required=False):
    from .store import APIError
    if values is None and not required:
        return None
    if (not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 64 or
        any(not isinstance(v, str) or not 1 <= len(v) <= 256 or
            any(ord(ch) < 32 or ord(ch) == 127 for ch in v) for v in values)):
        raise APIError('invalid_filter')
    return sorted(set(values))


def _subscription_id(value):
    from .store import APIError
    try:
        if not isinstance(value, str):
            raise ValueError
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise APIError('not_found', 404) from None


def dynamic(row):
    """ANYM-522: the definition follows the key's current scope instead of frozen sources."""
    return 'sources' in row['selector']


def dynamic_gate_on(c, *, fence=True):
    if fence:
        c.execute('SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))', (DYNAMIC_LOCK,))
    return c.execute("""SELECT EXISTS(SELECT FROM probe_metadata WHERE name=%s AND value='enabled')
        AS enabled""", (DYNAMIC_GATE,)).fetchone()['enabled']


def source_filters_on(c, *, fence=True):
    if fence:
        c.execute('SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))', (SOURCE_FILTER_LOCK,))
    return c.execute("""SELECT EXISTS(SELECT FROM probe_metadata WHERE name=%s AND value='enabled')
        AS enabled""", (SOURCE_FILTER_GATE,)).fetchone()['enabled']


def source_exclusions_on(c, *, fence=True):
    if fence:
        c.execute('SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))', (SOURCE_EXCLUSION_LOCK,))
    return c.execute("""SELECT EXISTS(SELECT FROM probe_metadata WHERE name=%s AND value='enabled')
        AS enabled""", (SOURCE_EXCLUSION_GATE,)).fetchone()['enabled']


def excluded_chats(row):
    """ANYM-830: ([accounts], [chats]) of the pairs a dynamic definition leaves out; ([], []) when none."""
    pairs = ((row['selector'].get('sources') or {}).get('exclude_chats') or []) if dynamic(row) else []
    return [pair['account'] for pair in pairs], [pair['chat'] for pair in pairs]


def scope(row, p):
    """(accounts, chats) a definition may disclose now: a dynamic one the key's current scope narrowed by
    its account_ids; chats None means every chat of those accounts."""
    if not dynamic(row):
        return p['accounts'], p['chat_ids']
    selected = row['selector']['sources'].get('account_ids')
    return [account for account in p['accounts'] if selected is None or account in selected], p['chat_ids']


def visible_filter(row, p, alias='m'):
    """(SQL, args) over subscription_matches (ANYM-522). A dynamic definition shows a reference only while its
    chat is in the key's scope and the operator did not hide it (hide_out_of_scope); an explicit definition
    is masked as a whole instead (access_changed), so all of its references show."""
    if alias not in ('m', 'sm'):
        raise ValueError('invalid_reference_alias')
    if not dynamic(row):
        return 'true', []
    accounts, chats = scope(row, p)
    where, args = ('NOT ' + alias + '.hidden AND ' + alias + '.account=ANY(%s) AND (%s::text[] IS NULL OR ' +
        alias + '.chat=ANY(%s))', [accounts, chats, chats])
    left_accounts, left_chats = excluded_chats(row)
    if left_accounts:
        # ANYM-830: a listed chat's stored references stay out of every read, count and hint fence.
        where += (' AND NOT EXISTS(SELECT FROM unnest(%s::text[],%s::text[]) AS x(account,chat) WHERE x.account=' +
            alias + '.account AND x.chat=' + alias + '.chat)')
        args += [left_accounts, left_chats]
    return 'coalesce(' + where + ',false)', args


def reference_columns(row, p, alias='m'):
    """(SQL, args) of the `visible` and `own_send_reply` columns of each match reference."""
    where, args = visible_filter(row, p, alias)
    # Explicit definitions name no schema-60 column, so they keep working before the migration.
    return where + ' AS visible,' + (alias + '.own_send_reply' if dynamic(row) else 'false AS own_send_reply'), args


def hide_out_of_scope(c, principal, accounts, chats):
    """ANYM-522, in the operator's scope change under the exclusive principal lock, which waits for every
    read, matcher batch and hint POST of the principal in flight: references of its dynamic definitions to a
    chat or account now outside the scope stay hidden for good, so a chat removed and added again shows only
    events matched after it came back. A held hint range is rebuilt from the visible references (its count,
    reasons and match times), a pending hint that covers no visible reference is cancelled, and every other
    queued hint keeps its range."""
    if not c.execute("SELECT EXISTS(SELECT FROM subscriptions WHERE principal=%s AND selector ? 'sources') AS e",
            (principal,)).fetchone()['e']:
        return
    touched = sorted({row['subscription'] for row in c.execute('''UPDATE subscription_matches m SET hidden=true
        FROM subscriptions s WHERE s.principal=%s AND s.selector ? 'sources' AND m.subscription=s.id AND NOT m.hidden
        AND NOT coalesce(m.account=ANY(%s) AND (%s::text[] IS NULL OR m.chat=ANY(%s)),false)
        RETURNING m.subscription''', (principal, accounts, chats, chats)).fetchall()})
    for row in c.execute('SELECT * FROM subscriptions WHERE id=ANY(%s) ORDER BY id FOR UPDATE', (touched,)).fetchall():
        # The next matcher or recovery tick re-absorbs the range above hinted_through, visible references only.
        c.execute('''UPDATE subscription_hint_windows SET first_match_at=NULL,last_match_at=NULL,match_count=0,
            through_position=NULL,reasons='{}',due_at=NULL,held_reason=NULL,updated_at=clock_timestamp()
            WHERE subscription=%s AND through_position IS NOT NULL''', (row['id'],))
        p = {'accounts': accounts, 'chat_ids': chats}
        # A mixed hint keeps its id and position (never re-emitted: the outbox key would drop it), as does a
        # lapsed lease, which may have reached its receiver; the dispatcher rechecks visibility under its fence.
        # A dynamic definition has no quiet-hours bypass (delivery_policy.normalize_policy).
        for hint in c.execute('''SELECT id,through_position,request_id FROM subscription_outbox WHERE subscription=%s
                AND kind='events' AND state='pending' ORDER BY through_position,id''', (row['id'],)).fetchall():
            if not hint_visible(c, row, p, hint['through_position'],
                    acked=(hint['request_id'] or '').startswith('resume:')):
                c.execute('''UPDATE subscription_outbox SET state='cancelled',finished_at=clock_timestamp(),
                    error_code='scope_changed',lease_token=NULL,lease_until=NULL WHERE id=%s''', (hint['id'],))


def protection_lock(c):
    """ANYM-806: serialize a change that starts the protection of named chats' files (media_policy.ON_REQUEST_CHATS)
    with the lifecycle worker's recheck and unlink, which hold the same lock: an owner's pause, resume or close, and
    the operator's retention release. It is the last lock those transactions take.

    Create, lazy expiry and access invalidation (_state, the matcher) take no lock. Create's subscription_sources
    rows hold a KEY SHARE on their chats, which Archive.ingest updates while it holds the lifecycle lock, so waiting
    for the lock there could deadlock. Lazy expiry and access invalidation run under many other locks, and they
    never protect a chat that was unprotected except through the 24-hour grace of a subscription that was already
    not protecting it (paused, or expired more than 24 hours before). An eviction racing any of them loses nothing
    the verified R2 copy cannot restore on request (request_file_download, cold-first rehydration)."""
    from .file_lifecycle import lifecycle_lock
    lifecycle_lock(c)


def invalidate_dynamic(c, principal, key_id=None):
    """ANYM-522, in the operator's change under the exclusive principal lock: losing read or subscription-write
    (or a disabled principal) ends the dynamic definitions at once, so restoring the right cannot revive them."""
    c.execute("""UPDATE subscriptions SET state='access_changed',version=version+1,updated_at=clock_timestamp()
        WHERE principal=%s AND (%s::uuid IS NULL OR key_id=%s) AND selector ? 'sources'
        AND state<>'access_changed'""", (principal, key_id, key_id))


# The range an events hint o covers starts above the previous one's position, or the start (one argument);
# a hint the scope change re-planned (scope_changed) no longer bounds a range.
_PREVIOUS_HINT = '''coalesce((SELECT max(p.through_position) FROM subscription_outbox p WHERE p.subscription=o.subscription
    AND p.kind='events' AND p.through_position<o.through_position AND p.error_code IS DISTINCT FROM 'scope_changed'),%s)'''
_ACK_POSITION = '''coalesce((SELECT k.acked_through FROM subscription_consumption k WHERE k.subscription=o.subscription),%s)'''


def hint_visible(c, row, p, through, *, acked=False):
    """ANYM-522: an events hint of a dynamic definition goes out only while a reference it covers is visible
    now: above the previous events hint's position (or the start) and at or below its own. acked (ANYM-520, a
    resume's backlog wake): above the ACK position instead, since a paused endpoint's cancelled hints woke no one."""
    where, args = visible_filter(row, p)
    lower = _ACK_POSITION if acked else _PREVIOUS_HINT
    return c.execute('''SELECT EXISTS(SELECT FROM (SELECT %s::uuid AS subscription,%s::bigint AS through_position) o
        JOIN subscription_matches m ON m.subscription=o.subscription AND m.event<=o.through_position
        AND m.event>''' + lower + ' AND ' + where + ') AS shown',
        (row['id'], through, row['start_position'] if acked else hint_floor(row), *args)).fetchone()['shown']


def hint_floor(row):
    """ANYM-447: the lowest bound of an events hint's range: the start, or the highest position of the events
    hints the worker deleted, which it deletes only as a prefix, so every kept range stays as it was."""
    return max(row['start_position'], row.get('hints_deleted_through') or 0)


def _state(c, row, p):
    """ACL changes never revive a previous definition, even after restoration."""
    if dynamic(row):
        # ANYM-522: scope and permission versions move without invalidating it; losing read or
        # subscription-write still does (fail closed). Each disclosure point applies the current scope.
        changed = not {'read', 'subscription-write'} <= set(p['permissions'])
    else:
        changed = (row['scope_version'] != p['scope_version'] or
            row['key_permission_version'] != p['key_permission_version'] or
            not {'read', 'subscription-write'} <= set(p['permissions']))
    if not changed and not dynamic(row):
        sources = c.execute('SELECT account,chat FROM subscription_sources WHERE subscription=%s',
            (row['id'],)).fetchall()
        changed = any(source['account'] not in p['accounts'] or
            p['chat_ids'] is not None and source['chat'] not in p['chat_ids'] for source in sources)
    if changed and row['state'] != 'access_changed':
        row = c.execute("""UPDATE subscriptions SET state='access_changed',
            version=version+1,updated_at=clock_timestamp() WHERE id=%s RETURNING *""",
            (row['id'],)).fetchone()
    elif (row['state'] in ('active', 'paused') and row['expires_at'] is not None and
            row['expires_at'] <= c.execute('SELECT clock_timestamp() AS t').fetchone()['t']):
        row = c.execute("""UPDATE subscriptions SET state='expired',version=version+1,
            updated_at=clock_timestamp() WHERE id=%s RETURNING *""", (row['id'],)).fetchone()
    return row


def _owned(c, p, subscription_id):
    from .store import APIError
    row = c.execute('''SELECT * FROM subscriptions WHERE id=%s AND principal=%s
        AND key_id=%s FOR UPDATE''', (_subscription_id(subscription_id), p['id'], p['key_id'])).fetchone()
    if row is None:
        raise APIError('not_found', 404)
    targets(p, [str(row['id'])])
    return _state(c, row, p)


def tombstones_ready(c):
    """Schema 62 present: new code tolerates an older database during a rolling upgrade."""
    return c.execute('''SELECT EXISTS(SELECT FROM pg_attribute WHERE attrelid='public.subscriptions'::regclass
        AND attname='tombstoned_at' AND NOT attisdropped) AS ready''').fetchone()['ready']


def _tombstone(row):
    """ANYM-447: a definition whose data was deleted 30 days after closure; the date says when, never not_found."""
    from .store import serial
    view = {'subscription_id': row['id'], 'state': row['state'], 'version': row['version'],
        'closed_at': row['closed_at'], 'data_deleted': True, 'deleted_at': row['tombstoned_at'],
        'deletion_reason': 'closed_over_30_days'}
    if row['state'] == 'access_changed':
        return serial({**view, 'recreate_required': True, 'scope_masked': True})
    return serial({**view, 'retention_floor': row['retention_floor'],
        'hint_counts': row['deleted_hint_counts'], 'scope_masked': False})


def _view(c, row, p=None):
    from .store import serial
    if row.get('tombstoned_at') is not None:
        return _tombstone(row)
    if row['state'] == 'access_changed':
        return {'subscription_id': str(row['id']), 'state': 'access_changed',
            'version': row['version'], 'recreate_required': True, 'scope_masked': True}
    head = c.execute('SELECT value FROM feed_counter WHERE id=1').fetchone()['value']
    if dynamic(row) and p is None:
        raise ValueError('dynamic_view_needs_scope')
    # ANYM-522: a dynamic definition counts only the references the key can see now.
    where, args = visible_filter(row, p)
    count = c.execute('SELECT count(*) AS n FROM subscription_matches m WHERE m.subscription=%s AND ' + where,
        (row['id'], *args)).fetchone()['n']
    # ANYM-518: the consumer's ACK position; before its first fetch, the start.
    consumption = c.execute('SELECT acked_through FROM subscription_consumption WHERE subscription=%s',
        (row['id'],)).fetchone()
    selector = row['selector']
    if dynamic(row) and 'exclude_chats' in selector['sources']:
        # ANYM-830: a stored pair whose chat left the key's scope is not shown.
        accounts, chats = scope(row, p)
        selector = {**selector, 'sources': {**selector['sources'], 'exclude_chats': [pair for pair in
            selector['sources']['exclude_chats'] if pair['account'] in accounts and (chats is None or pair['chat'] in chats)]}}
    return serial({'subscription_id': row['id'], 'state': row['state'], 'version': row['version'],
        'label': row['label'], **selector, 'created_at': row['created_at'],
        'updated_at': row['updated_at'], 'expires_at': row['expires_at'], 'closed_at': row.get('closed_at'),
        'start_position': row['start_position'], 'matched_through': row['matched_through'],
        'matcher_caught_up': row['state'] == 'active' and row['matched_through'] == head,
        'matched_event_count': count,
        'acked_through': consumption['acked_through'] if consumption else row['start_position'],
        'blocked_reason': row['blocked_reason'], 'gap': row['gap'],
        'matching_evidence': 'exact_event_revision',
        'missing_revision_evidence_events': row['missing_evidence'],
        'last_matched_at': row['last_matched_at'], 'scope_masked': False,
        'freshness_basis': 'committed_archive_journal_not_provider_freshness',
        'delivery': _delivery_view(c, row, p), **_module_view(c, row)})


def _module_view(c, row):
    # ANYM-562: only a subscription with a module stream shows one; message-only views keep their shape.
    from . import module_events
    module = module_events.stream(c, row['id'])
    return {'module_stream': module_events.view(c, row, module)} if module else {}



# ANYM-752: per state, the creation time of its oldest hint and its earliest attempt; a leased hint is scheduled by
# lease_until, because leasing leaves next_attempt_at unchanged.
_HINT_AGES = ''',min(o.created_at) AS oldest,
    min(CASE o.state WHEN 'leased' THEN coalesce(o.lease_until,o.next_attempt_at) ELSE o.next_attempt_at END) AS due'''


def _hint_states(c, row, p, ids=None):
    """Per-state rows (state, n, oldest, due) of the definition's hints the key can see now; ids narrows them."""
    if dynamic(row):
        if p is None:
            return []  # ANYM-447: no key can read it now, so the worker counts none and discloses nothing.
        # ANYM-522: only events hints that cover a reference the key can see now count, in every state; a resume's
        # backlog wake covers it from the ACK position, as hint_visible sends it.
        where, args = visible_filter(row, p)
        return c.execute('''SELECT o.state,count(*) AS n''' + _HINT_AGES + ''' FROM subscription_outbox o
            WHERE o.subscription=%s AND (%s::uuid[] IS NULL OR o.id=ANY(%s))
            AND o.error_code IS DISTINCT FROM 'scope_changed' AND (o.kind<>'events' OR EXISTS(
                SELECT FROM subscription_matches m WHERE m.subscription=o.subscription AND m.event<=o.through_position
                AND m.event>CASE WHEN starts_with(o.request_id,'resume:') THEN ''' + _ACK_POSITION + '''
                ELSE ''' + _PREVIOUS_HINT + ''' END AND ''' + where + ''')) GROUP BY o.state''',
            (row['id'], ids, ids, row['start_position'], hint_floor(row), *args)).fetchall()
    return c.execute('''SELECT o.state,count(*) AS n''' + _HINT_AGES + ''' FROM subscription_outbox o
        WHERE o.subscription=%s AND (%s::uuid[] IS NULL OR o.id=ANY(%s)) GROUP BY o.state''',
        (row['id'], ids, ids)).fetchall()


def _hint_counts(c, row, p, ids=None):
    """{state: n} of the definition's hints the key can see now; ids narrows it to those outbox rows."""
    return {item['state']: item['n'] for item in _hint_states(c, row, p, ids)}


def _observed_at(c):
    """The database clock the lag is read at (tests freeze it)."""
    return c.execute('SELECT clock_timestamp() AS now').fetchone()['now']


def _lag(c, states):
    """ANYM-752: the queue lag over exactly the hints hint_counts counts; unfinished means pending or leased."""
    unfinished = [item for item in states if item['state'] in ('pending', 'leased')]
    if not unfinished:
        return {'pending_hints': 0, 'oldest_unfinished_age_s': None, 'next_attempt_in_s': None}
    now = _observed_at(c)
    age = (now - min(item['oldest'] for item in unfinished)).total_seconds()
    wait = (min(item['due'] for item in unfinished) - now).total_seconds()
    # A future attempt rounds up, so a running lease never reads as due now; a due or past one reads 0.
    return {'pending_hints': sum(item['n'] for item in unfinished), 'oldest_unfinished_age_s': max(0, math.floor(age)),
        'next_attempt_in_s': max(0, math.ceil(wait))}


def _delivery_view(c, row, p=None):
    # Every column: before schema 49 the binding has no endpoint column (rolling upgrade).
    binding = c.execute('SELECT * FROM subscription_delivery WHERE subscription=%s', (row['id'],)).fetchone()
    # ANYM-447: counters that never decrease: the hints the worker deleted plus the rows still kept.
    states = _hint_states(c, row, p)
    live, deleted = {item['state']: item['n'] for item in states}, row.get('deleted_hint_counts') or {}
    # An unbound subscription reads no policy state, so its view needs no schema-38 column.
    from .delivery_policy import UNBOUND, delivery_state
    view = {'passive_read': True, 'ack': True, 'push': binding is not None,
        'receiver_id': str(binding['receiver']) if binding and binding['receiver'] else None, 'host_wakeup': 'unverified',
        'host_turn_verified': False,
        'hint_counts': {state: live.get(state, 0) + deleted.get(state, 0) for state in sorted({*live, *deleted})},
        'deleted_hint_counts': deleted, 'hints_deleted_at': row.get('hints_deleted_at'), 'lag': _lag(c, states),
        **(delivery_state(c, row) if binding else UNBOUND)}
    # ANYM-848: the agent's own wake target bound to this subscription; null before schema 73 or for a role without
    # the grant, and never a change to push delivery.
    from .wake_targets import subscription_wake_target
    view['wake_target_id'] = subscription_wake_target(c, row['id'])
    if binding and binding.get('endpoint'):
        # ANYM-519: a deleted endpoint leaves the subscription pull-only; a paused one names its state.
        endpoint = c.execute('SELECT state FROM delivery_endpoints WHERE id=%s', (binding['endpoint'],)).fetchone()
        state = endpoint['state'] if endpoint else 'deleted'
        view.update(endpoint_id=str(binding['endpoint']), target='endpoint', push=state == 'enabled',
            reason=None if state == 'enabled' else 'endpoint_deleted' if state == 'deleted' else state)
        if state == 'deleted':
            view.update(target='none', endpoint_id=None)
        else:
            # ANYM-520: the bound endpoint's health in every subscription read (get, wait and fetch).
            from .delivery_endpoints import endpoint_health
            view['delivery_health'] = endpoint_health(c, binding['endpoint'])
    return view


def _sources(p, sources, sender_ids, reply_to_send):
    """ANYM-522: all chats the key can see, optionally per account; channels (Telegram broadcasts
    included) and bot senders only when asked for."""
    from .store import APIError
    if (not isinstance(sources, dict) or set(sources) - SOURCE_OPTIONS or sources.get('all_visible_chats') is not True or
            any(type(sources.get(name, False)) is not bool for name in ('include_channels', 'include_bots'))):
        raise APIError('invalid_filter')
    # Sender IDs are account-scoped and a send anchor names one chat: both belong to explicit sources.
    if sender_ids is not None:
        raise APIError('invalid_filter')
    if reply_to_send is not None:
        raise APIError('invalid_subscription')
    accounts = _ids(sources.get('account_ids'))
    if accounts is not None and set(accounts) - set(p['accounts']):
        raise APIError('not_found', 404)
    result = {'all_visible_chats': True, 'account_ids': accounts,
        'include_channels': sources.get('include_channels', False), 'include_bots': sources.get('include_bots', False)}
    if 'chat_types' in sources:
        # ANYM-825: absent keeps today's rule; naming channel is what lets channels in.
        named = sources['chat_types']
        if (not isinstance(named, list) or not 1 <= len(named) <= 4 or any(not isinstance(t, str) for t in named) or
                len(set(named)) != len(named) or set(named) - CHAT_TYPES or
                result['include_channels'] and 'channel' not in named):
            raise APIError('invalid_filter')
        result['chat_types'] = sorted(named)
    if 'exclude_chats' in sources:
        result['exclude_chats'] = _exclusion_pairs(sources['exclude_chats'])
        result['exclusions_revision'] = 1
    return result


def _exclusion_pairs(value):
    """ANYM-830: the form of an exclusion list: at most 40 {account, chat} pairs and 6,000 serialized bytes;
    sorted and without duplicates. Visibility is checked against the key's scope by `_visible_exclusions`."""
    from .store import APIError
    if not isinstance(value, list) or len(value) > EXCLUDE_CHATS_MAX:
        raise APIError('invalid_filter')
    pairs = set()
    for pair in value:
        if not isinstance(pair, dict) or set(pair) != {'account', 'chat'}:
            raise APIError('invalid_filter')
        for name, longest in (('account', 64), ('chat', 256)):
            text = pair[name]
            if (not isinstance(text, str) or not 1 <= len(text) <= longest or
                    any(ord(ch) < 32 or ord(ch) == 127 for ch in text)):
                raise APIError('invalid_filter')
        pairs.add((pair['account'], pair['chat']))
    result = [{'account': account, 'chat': chat} for account, chat in sorted(pairs)]
    if len(json.dumps(result, ensure_ascii=False).encode()) > EXCLUDE_CHATS_BYTES:
        raise APIError('invalid_filter')
    return result


def _visible_exclusions(c, p, pairs):
    """An unknown pair, or one outside the key's scope, is not_found (as account_ids are)."""
    from .store import APIError
    if not pairs:
        return
    found = c.execute('''SELECT count(*) AS n FROM chats c JOIN unnest(%s::text[],%s::text[]) AS x(account,chat)
        ON c.account=x.account AND c.id=x.chat WHERE c.account=ANY(%s) AND (%s::text[] IS NULL OR c.id=ANY(%s))''',
        ([pair['account'] for pair in pairs], [pair['chat'] for pair in pairs], p['accounts'], p['chat_ids'],
         p['chat_ids'])).fetchone()['n']
    if found != len(pairs):
        raise APIError('not_found', 404)


def _check_capacity(c, p):
    """Creation and resume share a key's active-only admission lock; retained history occupies no slot."""
    from .store import APIError
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
        ('subscriptions:' + str(p['key_id']),))
    maximum = _bound_setting('SUBSCRIPTION_MAX_DEFINITIONS', 100, 1, 1000)
    count = c.execute('''SELECT count(*) AS n FROM subscriptions WHERE principal=%s AND key_id=%s
        AND state='active' AND (expires_at IS NULL OR expires_at>clock_timestamp())''',
        (p['id'], p['key_id'])).fetchone()['n']
    if count >= maximum:
        error = APIError('subscription_limit', 429)
        error.active, error.max = count, maximum  # Grok 17e07270: the caller's own usage and cap.
        raise error


def create_subscription(store, c, p, chat_ids=None, *, sources=None, sender_ids=None, kinds=None,
        direction='incoming', origins=None, label='', expires_at=None, wake_rules=None, reply_to_send=None,
        delivery=None):
    from .store import APIError, date_bound
    if 'read' not in p['permissions']:
        raise APIError('forbidden', 403)
    if (chat_ids is None) == (sources is None):
        raise APIError('invalid_filter')
    if delivery is not None:
        # ANYM-519: bind a self-service endpoint in the same transaction (configure_subscription_delivery's checks).
        if not isinstance(delivery, dict) or 'endpoint_id' not in delivery or set(delivery) - {'endpoint_id', 'delivery_policy'}:
            raise APIError('invalid_parameters')
        from .delivery_endpoints import gate_on
        if 'delivery-write' not in p['permissions']:
            raise APIError('forbidden', 403)
        if not gate_on(c):
            raise APIError('delivery_disabled', 409)
    dynamic_sources = None if sources is None else _sources(p, sources, sender_ids, reply_to_send)
    chats = [] if dynamic_sources else _ids(chat_ids, required=True)
    senders = _ids(sender_ids)
    kinds = _ids(['message.created'] if kinds is None else kinds, required=True)
    origins = _ids(['live'] if origins is None else origins, required=True)
    if set(kinds) - EVENT_KINDS or set(origins) - ORIGINS or direction not in ('', 'incoming', 'outgoing'):
        raise APIError('invalid_filter')
    if (not isinstance(label, str) or len(label) > 128 or
        any(ord(ch) < 32 or ord(ch) == 127 for ch in label)):
        raise APIError('invalid_subscription')
    expiry = date_bound(expires_at) if expires_at is not None else None
    now = c.execute('SELECT clock_timestamp() AS t').fetchone()['t']
    if expires_at is not None and (expiry is None or not now < expiry <= now + timedelta(days=366)):
        raise APIError('invalid_utc_date')
    if dynamic_sources and not dynamic_gate_on(c):
        # Until the operator enables them after the API, matcher and dispatcher run this version.
        raise APIError('dynamic_sources_disabled', 409)
    if dynamic_sources and 'chat_types' in dynamic_sources and not source_filters_on(c):
        # An older matcher would store every type's matches for it, and readers do not filter stored ones.
        raise APIError('source_filters_disabled', 409)
    if dynamic_sources and 'exclude_chats' in dynamic_sources:
        if not source_exclusions_on(c):
            # An older matcher would match listed chats, and older readers would show their references.
            raise APIError('source_exclusions_disabled', 409)
        _visible_exclusions(c, p, dynamic_sources['exclude_chats'])
    sources = c.execute('SELECT id,account FROM chats WHERE id=ANY(%s) AND account=ANY(%s)',
        (chats, p['accounts'])).fetchall()
    if (len(sources) != len(chats) or
        (p['chat_ids'] is not None and set(chats) - set(p['chat_ids']))):
        raise APIError('not_found', 404)
    # Provider sender identifiers are account-scoped. Equal IDs observed in two
    # accounts are ambiguous, not proof of the same person. Subscribe separately.
    sender_accounts = {}
    if senders is not None:
        known = c.execute('''SELECT DISTINCT account,sender FROM messages
            WHERE chat=ANY(%s) AND sender=ANY(%s) UNION SELECT DISTINCT account,sender
            FROM sender_profiles WHERE chat=ANY(%s) AND sender=ANY(%s)''',
            (chats, senders, chats, senders)).fetchall()
        for sender in senders:
            accounts = {r['account'] for r in known if r['sender'] == sender}
            if not accounts:
                raise APIError('not_found', 404)
            if len(accounts) != 1:
                raise APIError('ambiguous_reference', 409)
            sender_accounts[sender] = next(iter(accounts))
    _check_capacity(c, p)
    # The counter is serialized by event publishers. An MVCC read sees only a
    # committed frontier and cannot skip an uncommitted event transaction.
    head = c.execute('SELECT value FROM feed_counter WHERE id=1').fetchone()['value']
    selector = (dict(sources=dynamic_sources, sender_ids=None, kinds=kinds, origins=origins, direction=direction)
        if dynamic_sources else dict(chat_ids=chats, sender_ids=senders, kinds=kinds, origins=origins, direction=direction))
    if wake_rules is not None:
        if not isinstance(wake_rules, (list, tuple)) or not 1 <= len(wake_rules) <= 4:
            raise APIError('invalid_subscription')
        rules = _ids(wake_rules, required=True)
        # reply_to_own_send needs the matcher of this version: dynamic sources only, behind their switch.
        if set(rules) - WAKE_RULES or 'reply_to_own_send' in rules and not dynamic_sources:
            raise APIError('invalid_subscription')
        selector['wake_rules'] = rules
    if reply_to_send is not None:
        if not isinstance(reply_to_send, str) or not 1 <= len(reply_to_send) <= 128:
            raise APIError('not_found', 404)
        anchor = c.execute('''SELECT o.id,o.account,o.chat,o.provider_chat,o.provider_message_id,a.platform
            FROM outgoing_messages o JOIN accounts a ON a.id=o.account
            WHERE o.id=%s AND o.principal=%s AND o.key_id=%s AND o.status='sent'
            AND o.provider_message_id IS NOT NULL AND o.provider_message_id<>'' ''',
            (reply_to_send, p['id'], p['key_id'])).fetchone()
        if anchor is None or anchor['chat'] not in chats:
            raise APIError('not_found', 404)
        selector['reply_anchor'] = {'send_id': anchor['id'], 'account': anchor['account'],
            'chat': anchor['chat'], 'provider_id': anchor['provider_message_id'],
            'basis': 'confirmed_exact_provider_receipt'}
        from .reply_evidence import confirmed_native_reference
        selector['reply_anchor'].update(confirmed_native_reference(c, anchor))
    if 'reply_to_confirmed_send' in selector.get('wake_rules', []) and 'reply_anchor' not in selector:
        raise APIError('invalid_subscription')
    if len(json.dumps(selector, ensure_ascii=False).encode()) > 14000:
        raise APIError('invalid_filter')
    row = c.execute('''INSERT INTO subscriptions(principal,key_id,scope_version,key_permission_version,
        selector,label,start_position,matched_through,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING *''', (p['id'], p['key_id'], p['scope_version'], p['key_permission_version'],
            Jsonb(selector), label, head, head, expiry)).fetchone()
    for source in sources:
        selected = None if senders is None else [sid for sid in senders if sender_accounts[sid] == source['account']]
        c.execute('INSERT INTO subscription_sources(subscription,account,chat,sender_ids) VALUES(%s,%s,%s,%s)',
            (row['id'], source['account'], source['id'], selected))
    targets(p, [str(row['id'])])
    result = None
    if delivery is not None:
        from .event_delivery import configure_subscription_delivery
        result = configure_subscription_delivery(store, c, p, str(row['id']), row['version'], None,
            delivery.get('delivery_policy'), delivery['endpoint_id'])
        targets(p, [str(row['id'])])
    # ANYM-806: create takes no lifecycle lock (see protection_lock): its source rows hold the chats' keys, which
    # Archive.ingest updates under that lock. An eviction racing a create is restored from R2 on request.
    return result if delivery is not None else _view(c, row, p)


def list_subscriptions(store, c, p, *, limit=50):
    from .store import bounded
    bounded(limit)
    rows = c.execute('''SELECT * FROM subscriptions WHERE principal=%s AND key_id=%s
        ORDER BY created_at DESC,id DESC LIMIT %s FOR UPDATE''', (p['id'], p['key_id'], limit + 1)).fetchall()
    result = [_view(c, _state(c, row, p), p) for row in rows[:limit]]
    targets(p, [str(row['id']) for row in rows[:limit]])
    return {'items': result, 'has_more': len(rows) > limit}


def get_subscription(store, c, p, subscription_id):
    row = _owned(c, p, subscription_id)
    view = _view(c, row, p)
    if row['state'] != 'access_changed' and row.get('tombstoned_at') is None:
        from .subscription_consumption import server_positions
        view['positions'] = server_positions(c, row, view['acked_through'])
        # ANYM-824: per-day matcher counts, in this view only (list_subscriptions carries none).
        activity = _activity(c, row, p)
        if activity is not None:
            view['activity'] = activity
    return view


def update_subscription(store, c, p, subscription_id, version, *, action, resume_policy='catch_up',
        exclude_chats=None, exclusions_revision=None):
    from .store import APIError
    if 'read' not in p['permissions']:
        raise APIError('forbidden', 403)
    if type(version) is not int or not 1 <= version <= 2**53 - 1:
        raise APIError('invalid_version')
    if action not in ('pause', 'resume', 'close', 'set_exclusions') or resume_policy not in ('catch_up', 'skip'):
        raise APIError('invalid_subscription_state')
    if action != 'resume' and resume_policy != 'catch_up':
        raise APIError('invalid_subscription_state')
    if (action == 'set_exclusions') != (exclude_chats is not None) or (
            action == 'set_exclusions') != (exclusions_revision is not None):
        raise APIError('invalid_parameters')
    if action == 'set_exclusions':
        pairs = _exclusion_pairs(exclude_chats)
        if type(exclusions_revision) is not int or not 0 <= exclusions_revision <= 2**53 - 1:
            raise APIError('invalid_version')
        if not source_exclusions_on(c):
            raise APIError('source_exclusions_disabled', 409)
    row = _owned(c, p, subscription_id)
    if row['state'] == 'access_changed':
        # Do not include the old version, selectors or counts in an error.
        raise APIError('subscription_access_changed', 409)
    if row['version'] != version:
        raise APIError('subscription_conflict', 409)
    if action == 'set_exclusions':
        return _set_exclusions(c, p, row, pairs, exclusions_revision)
    if row['state'] in ('closed', 'expired'):
        raise APIError('subscription_closed', 409)
    if action == 'resume' and row['state'] == 'paused':
        _check_capacity(c, p)
    from . import module_events
    module = module_events.stream(c, row['id'], lock=True)
    if action == 'resume' and row['state'] == 'active' and module and module['rollback_hold']:
        # ANYM-562: an older runtime resumed it; the owner of the key re-arms the held module stream.
        module_events.resume(c, row, resume_policy)
        row = c.execute('''UPDATE subscriptions SET version=version+1,updated_at=clock_timestamp()
            WHERE id=%s RETURNING *''', (row['id'],)).fetchone()
        return _view(c, row, p)
    if action == 'pause' and row['state'] != 'active' or action == 'resume' and row['state'] != 'paused':
        raise APIError('invalid_subscription_state', 409)
    if action == 'resume' and module is not None:
        module_events.resume(c, row, resume_policy)
    state = {'pause': 'paused', 'resume': 'active', 'close': 'closed'}[action]
    position = row['matched_through']
    gap = row['gap']
    if action == 'resume' and resume_policy == 'skip':
        head = c.execute('SELECT value FROM feed_counter WHERE id=1').fetchone()['value']
        if position < head:
            gap = {'reason': 'explicit_resume_skip', 'after': position, 'through': head,
                'recoverable': False, 'intentional': True}
        position = head
    # ANYM-805/806: resume protects its chats' originals again, and close starts their 24-hour grace (see create).
    protection_lock(c)
    row = c.execute('''UPDATE subscriptions SET state=%s,version=version+1,
        matched_through=%s,gap=%s,blocked_reason=NULL,updated_at=clock_timestamp()
        WHERE id=%s RETURNING *''', (state, position, Jsonb(gap) if gap else None, row['id'])).fetchone()
    return _view(c, row, p)


def _set_exclusions(c, p, row, pairs, revision):
    """ANYM-830: replace the whole list under the definition's row lock (the matcher's pass waits, so the next
    pass already leaves the chats out). `version` stays: queued, leased and held hints are keyed to it and stay
    valid; the fence and the webhook body hide the listed chats' references. Only `exclusions_revision` and
    `updated_at` move; an unchanged list changes nothing."""
    from .store import APIError
    if not dynamic(row):
        raise APIError('invalid_filter')
    if row['state'] not in ('active', 'paused'):
        raise APIError('invalid_subscription_state', 409)
    sources = row['selector']['sources']
    if revision != sources.get('exclusions_revision', 0):
        raise APIError('subscription_conflict', 409)
    _visible_exclusions(c, p, pairs)
    if 'exclusions_revision' in sources and sources.get('exclude_chats') == pairs:
        return _view(c, row, p)
    selector = {**row['selector'], 'sources': {**sources, 'exclude_chats': pairs, 'exclusions_revision': revision + 1}}
    if len(json.dumps(selector, ensure_ascii=False).encode()) > 14000:
        raise APIError('invalid_filter')
    row = c.execute('''UPDATE subscriptions SET selector=%s,updated_at=clock_timestamp() WHERE id=%s
        RETURNING *''', (Jsonb(selector), row['id'])).fetchone()
    # A held range absorbed under the old list is marked (delivery_policy.REBUILD_DUE, already due), so the next
    # hint tick rebuilds it from the references visible under the new one before it settles; the version stays.
    # A paused definition holds no range for long: its resume bumps the version, which restarts held ranges.
    from .delivery_policy import REBUILD_DUE
    if row['state'] == 'active':
        c.execute('''UPDATE subscription_hint_windows SET due_at=%s,updated_at=clock_timestamp()
            WHERE subscription=%s AND first_match_at IS NOT NULL''', (REBUILD_DUE, row['id']))
    return _view(c, row, p)


# ANYM-866: per statement of the page's file lookup (two statements): far below a wait's 5 s deadline.
MEDIA_BUDGET = '500ms'


def get_subscription_events(store, c, p, subscription_id, *, after=0, limit=50, module_after=None,
                            with_positions=True):
    from .store import APIError, bounded, serial
    bounded(limit)
    if type(after) is not int or not 0 <= after <= 2**53 - 1:
        raise APIError('invalid_version')
    row = _owned(c, p, subscription_id)
    state = _view(c, row, p)
    if row['state'] == 'access_changed':
        return {'subscription': state, 'items': [], 'has_more': False, 'scope_masked': True}
    # Pruned references never become an empty complete page: the gap names the floor to resume from.
    floor = row.get('retention_floor', 0)
    from . import module_events
    module = module_events.stream(c, row['id'])
    if module is not None and module_after is None:
        # Passive reads start at the consumer's ACK position: never below the retention floor.
        module_after = module['acked_through']
    if module_after is not None and (type(module_after) is not int or not 0 <= module_after <= 2**53 - 1):
        raise APIError('invalid_version')
    # Each stream pages, gaps and advances on its own positions (ANYM-562).
    modules = {} if module is None else {'module': module_events.page(c, row, module, module_after, limit)}
    if with_positions and 'acked_through' in state:
        # ANYM-827: the server's positions on every event page (a fetch adds them after its receipt); a tombstone
        # has no consumption left to name.
        from .subscription_consumption import server_positions
        modules['positions'] = server_positions(c, row, state['acked_through'])
    if after < floor:
        state['gap'] = {'reason': 'retention_gap', 'after': after, 'through': floor,
            'recoverable': False, 'floor': floor}
        return serial({'subscription': state, 'items': [], 'has_more': True, 'next_after': floor,
            'consumption': 'passive_read_no_ack', 'scope_masked': False, **modules})
    # ANYM-522: a dynamic definition returns only references the key can see now.
    where, args = visible_filter(row, p, 'sm')
    events = c.execute('''SELECT e.id,e.kind,e.origin,e.revision,e.created_at,sm.matched_at,''' +
        ('sm.own_send_reply' if dynamic(row) else 'false AS own_send_reply') + ''',
        jsonb_strip_nulls(jsonb_build_object(
            'message',e.snapshot->'message','account',e.snapshot->'account','chat',e.snapshot->'chat',
            'sender',e.snapshot->'sender','provider_id',e.snapshot->'provider_id',
            'sent_at',e.snapshot->'sent_at','direction',e.snapshot->'direction',
            'deleted',e.snapshot->'deleted','processing',e.snapshot->'processing',
            -- ANYM-529, ANYM-658, ANYM-922, ANYM-924: contact cards, inline keyboards, shared places and event invitations are content the journal keeps, references do not.
            'metadata',(e.snapshot->'metadata')-'contact'-'buttons'-'location'-'event'-'event_responses','reply_evidence',e.snapshot->'reply_evidence',
            -- ANYM-535: IDs and codes only; the sending key stays in the journal.
            'send_id',CASE WHEN e.kind='send.failed' THEN e.snapshot->'send_id' END,
            'error_code',CASE WHEN e.kind='send.failed' THEN e.snapshot->'error_code' END,
            'status',CASE WHEN e.kind IN ('send.failed','source.health') THEN e.snapshot->'status' END,
            'date',CASE WHEN e.kind='contact.birthday' THEN e.snapshot->'date' END)) AS snapshot
        FROM subscription_matches sm JOIN events e ON e.id=sm.event
        WHERE sm.subscription=%s AND sm.event>%s AND ''' + where + ''' ORDER BY sm.event LIMIT %s''',
        (row['id'], after, *args, limit + 1)).fetchall()
    fields = ('message', 'account', 'chat', 'sender', 'provider_id', 'revision', 'sent_at',
        'direction', 'deleted', 'processing', 'metadata', 'reply_evidence', 'send_id', 'error_code', 'status', 'date')
    # ANYM-734: delivered_at is when the earliest delivered events hint covering the event finished. Retention deletes
    # finished hints as a prefix up to hints_deleted_through: for an event at or below it the first covering hint may be
    # gone, so the time is unknown (null), never the next hint's. One read per page, no per-row query.
    delivered = c.execute('''SELECT through_position,finished_at FROM subscription_outbox WHERE subscription=%s
        AND kind='events' AND state='delivered' AND through_position>=%s''',
        (row['id'], events[0]['id'])).fetchall() if events else []
    pruned_through = row.get('hints_deleted_through') or 0
    # Retention deletes only finished hints older than the cutoff, and a hint covering an event is created after the
    # event matched. An event matched at or after (last deletion - retention days) can therefore have lost none of its
    # covering hints, whatever states and positions the kept hints have; an older one may have lost its first (a batch
    # limit can take one of several hints at a position and leave hints_deleted_through unmoved): unknown.
    cutoff = row['hints_deleted_at'] - timedelta(days=CLOSED_RETENTION_DAYS) if row.get('hints_deleted_at') else None

    def delivered_at(event):
        if event['id'] <= pruned_through or (cutoff is not None and event['matched_at'] < cutoff):
            return None
        return min((hint['finished_at'] for hint in delivered
            if hint['through_position'] >= event['id'] and hint['finished_at']), default=None)

    seen = [seen_origin(row['selector']['origins'], event['origin'], event['kind'], event['snapshot'].get('sent_at'),
        event['created_at']) for event in events[:limit]]
    items = [{**{key: event['snapshot'][key] for key in fields if key in event['snapshot']},
        'event_id': event['id'], 'kind': event['kind'], 'origin': origin,
        'revision': event['revision'], 'observed_at': event['created_at'],
        'matched_at': event['matched_at'], 'delivered_at': delivered_at(event),
        'historical': origin in ('backfill', 'reconcile'),
        'matching_basis': 'exact_event_revision'} for event, origin in zip(events, seen)]
    from .attachment_failures import keep_null_actions
    keep_null_actions(items)
    # ANYM-866: each file's state as a message read shows it now. The lookup runs in a savepoint under its own lock
    # and statement timeouts (the caller's are restored after): a failure (a held lock, a slow plan, a bad row) leaves
    # the page, its receipt and the subscription row lock's holding time within the budget, without attachments,
    # and is counted.
    from . import result_evidence
    try:
        with c.transaction():
            before = c.execute('''SELECT current_setting('statement_timeout') AS statement,
                current_setting('lock_timeout') AS lock''').fetchone()
            c.execute("SELECT set_config('statement_timeout',%s,true),set_config('lock_timeout',%s,true)",
                (MEDIA_BUDGET, MEDIA_BUDGET))
            result_evidence.event_attachments(c, items)
            c.execute("SELECT set_config('statement_timeout',%s,true),set_config('lock_timeout',%s,true)",
                (before['statement'], before['lock']))
    except Exception:
        from .attachment_metadata import count_media_error
        count_media_error('lookup_failed')
        for item in items:
            item.pop('attachments', None)
    from .subscription_consumption import wake_reason
    for item, event in zip(items, events):
        item['wake_reason'] = wake_reason({**item, 'reply_to_own_send': event['own_send_reply']}, row['selector'])
    position = items[-1]['event_id'] if items else after
    if dynamic(row) and len(events) <= limit:
        # The page holds every visible reference: the cursor passes the hidden ones above it, so a fetch's
        # receipt moves past them. The read holds the definition's row, so the matcher adds none meanwhile.
        position = max(position, c.execute('SELECT max(event) AS e FROM subscription_matches WHERE subscription=%s',
            (row['id'],)).fetchone()['e'] or 0)
    return serial({'subscription': state, 'items': items, 'has_more': len(events) > limit,
        'next_after': position,
        'consumption': 'passive_read_no_ack', 'scope_masked': False, **modules})


def _worker_identity(c, row):
    lock_principal(c, row['principal'], shared=True)
    p = c.execute('''SELECT p.*,k.id AS key_id,k.permissions AS key_permissions,
        k.permission_version AS key_permission_version FROM principals p JOIN api_keys k ON k.principal=p.id
        WHERE p.id=%s AND k.id=%s AND p.enabled AND NOT k.revoked AND k.expires_at>clock_timestamp()''',
        (row['principal'], row['key_id'])).fetchone()
    if p is not None:
        p['permissions'] = effective_permissions(p['permissions'], p['key_permissions'])
    return p


def match_subscriptions(store, *, batch_size=250, max_subscriptions=32):
    """One bounded pass; safe to retry or run from more than one worker."""
    from .store import bounded
    bounded(batch_size, 1000)
    bounded(max_subscriptions, 128)
    maximum = _bound_setting('SUBSCRIPTION_MAX_MATCHES', 10000, 1, 1000000)
    stats = {'checked': 0, 'scanned': 0, 'matched': 0, 'access_changed': 0,
        'expired': 0, 'blocked': 0, 'missing_evidence': 0, 'module_matched': 0, 'module_scanned': 0, 'module_blocked': 0, 'scope_failed': 0,
        'activity_failed': 0}
    with store.pool.connection() as c:
        from . import module_events
        modules = module_events.ready(c)
        candidates = c.execute('''SELECT id,principal,key_id FROM subscriptions WHERE state='active'
            ORDER BY last_matched_at NULLS FIRST,created_at,id LIMIT %s''', (max_subscriptions,)).fetchall()
    for candidate in candidates:
        with store.pool.connection() as c:
            # ConnectionPool exits commit. Locks cover exactly one bounded
            # subscription batch, not the whole pass or any provider operation.
            _match_batch(c, candidate, batch_size, maximum, modules, stats)
    return stats


def _match_batch(c, candidate, batch_size, maximum, modules, stats):
    """One subscription's batch in the pass's stage order: admit, read events, scope, scan, record, modules.
    A stage that stops the batch returns before the next one; the caller's connection exit commits."""
    admitted = _admit_batch(c, candidate, stats)
    if admitted is None:
        return
    row, p, sources = admitted
    head, events = _batch_events(c, row, batch_size)
    count = c.execute('SELECT count(*) AS n FROM subscription_matches WHERE subscription=%s',
        (row['id'],)).fetchone()['n']
    lookup = {(source['account'], source['chat']): source['sender_ids'] for source in sources}
    excluded = set(zip(*excluded_chats(row)))
    scope = _batch_visibility(c, row, p, events, stats)
    if scope is None:
        return
    visible, own = scope
    scan = _scan_batch(c, row, p, events, head, count, maximum, (lookup, visible, excluded, own), stats)
    _record_batch(c, row, p, events, batch_size, head, scan, stats)
    if modules:
        _match_modules(c, row, batch_size, maximum, stats)


def _admit_batch(c, candidate, stats):
    """The locked active definition, its worker identity and fixed sources, or None when the batch stops here."""
    p = _worker_identity(c, candidate)
    row = c.execute("SELECT * FROM subscriptions WHERE id=%s AND state='active' FOR UPDATE SKIP LOCKED",
        (candidate['id'],)).fetchone()
    if row is None:
        return None
    stats['checked'] += 1
    if p is None:
        c.execute("""UPDATE subscriptions SET state='access_changed',version=version+1,
                    updated_at=clock_timestamp() WHERE id=%s""", (row['id'],))
        stats['access_changed'] += 1
        return None
    row = _state(c, row, p)
    if row['state'] != 'active':
        stats[row['state']] += 1
        return None
    sources = [] if dynamic(row) else c.execute('''SELECT account,chat,sender_ids FROM subscription_sources
                WHERE subscription=%s''', (row['id'],)).fetchall()
    # Version equality is the fence, with a full source-scope check as a
    # defensive boundary if records are restored or repaired externally.
    if any(source['account'] not in p['accounts'] or
            p['chat_ids'] is not None and source['chat'] not in p['chat_ids'] for source in sources):
        c.execute("""UPDATE subscriptions SET state='access_changed',version=version+1,
                    updated_at=clock_timestamp() WHERE id=%s""", (row['id'],))
        stats['access_changed'] += 1
        return None
    return row, p, sources


def _batch_events(c, row, batch_size):
    """The committed feed head and the next batch of events after the definition's frontier, origins as seen."""
    head = c.execute('SELECT value FROM feed_counter WHERE id=1').fetchone()['value']
    # ANYM-824: the account's platform labels the pass's counts (accounts(id,platform), schema 60 grant).
    events = c.execute('''SELECT e.id,e.account,e.kind,e.origin,e.created_at,e.snapshot->>'sent_at' AS sent_at,
                coalesce(a.platform,'unknown') AS platform,
                jsonb_strip_nulls(jsonb_build_object('chat',e.snapshot->'chat',
                    'sender',e.snapshot->'sender','direction',e.snapshot->'direction',
                    'metadata',e.snapshot->'metadata','reply_evidence',e.snapshot->'reply_evidence',
                    'principal',e.snapshot->'principal','key_id',e.snapshot->'key_id')) AS snapshot
                FROM events e LEFT JOIN accounts a ON a.id=e.account WHERE e.id>%s AND e.id<=%s ORDER BY e.id LIMIT %s''',
        (row['matched_through'], head, batch_size)).fetchall()
    for event in events:
        event['origin'] = seen_origin(row['selector']['origins'], event['origin'], event['kind'],
            event['sent_at'], event['created_at'])
    return head, events


def _batch_visibility(c, row, p, events, stats):
    """(visible, own): a dynamic definition's scope snapshot and own-send replies; (None, set()) for fixed sources;
    None when the scope read failed and only this definition waits."""
    # ANYM-522: one locked scope snapshot per batch; the shared principal lock holds it until commit.
    visible, own = None, set()
    if dynamic(row):
        try:
            with c.transaction():
                visible = _batch_scope(c, row, p, events)
                if visible and 'reply_to_own_send' in row['selector'].get('wake_rules', ()):
                    own = _own_send_replies(c, row, events, visible)
        except Exception as error:
            # Only this definition waits: it goes to the back of the queue, the pass goes on.
            from .telemetry import capture
            capture(error, 'subscription_scope_failed')
            print(json.dumps({'error': 'subscription_scope_failed', 'class': type(error).__name__}), flush=True)
            c.execute('UPDATE subscriptions SET last_matched_at=clock_timestamp() WHERE id=%s', (row['id'],))
            stats['scope_failed'] += 1
            return None
    return visible, own


def _event_verdict(event, row, p, scope):
    """(matches, metric, missing, selected, failure) for one event. ANYM-824: metric is the first rule that leaves a
    selected message event out, in this order; missing is 1 when the direction or sender evidence is absent."""
    lookup, visible, excluded, own = scope
    selector = row['selector']
    snapshot = event['snapshot']
    source = (event['account'], snapshot.get('chat'))
    selected = source in lookup if visible is None else source in visible
    failure = event['kind'] in FAILURE_KINDS
    metric, missing = None, 0
    if failure:
        # Direction and senders describe messages; these kinds have their own audience.
        matches = (event['kind'] in selector['kinds'] and origin_selected(event['origin'], selector['origins']) and
            _failure_audience(event, selected and (event['kind'] != 'contact.birthday' or source not in excluded), row, p))
    else:
        matches = selected
        outcome = call_outcome(snapshot) if event['kind'] == 'message.created' else None
        if matches and outcome is not None:
            if 'call' not in selector['kinds'] or outcome != 'missed':
                matches, metric = False, 'left_out_kind'
        elif matches and event['kind'] not in selector['kinds'] and (
                IMPLIED_KINDS.get(event['kind']) not in selector['kinds']):
            matches, metric = False, 'left_out_kind'
        if matches and quiet_group_change(snapshot):
            matches, metric = False, 'left_out_kind'
        if matches and not origin_selected(event['origin'], selector['origins']):
            matches, metric = False, 'left_out_origin'
        if matches and visible is not None:
            left = ('excluded_chat' if source in excluded else
                _dynamic_match(selector['sources'], visible[source], snapshot))
            if left is not None:
                left_out(row, event, left)
                matches, metric = False, 'left_out_' + left
        if matches and selector['direction']:
            if 'direction' not in snapshot:
                missing += 1
                matches = False
            else:
                matches = snapshot['direction'] == selector['direction']
            metric = None if matches else 'left_out_direction'
        if matches and lookup.get(source) is not None:
            if 'sender' not in snapshot:
                missing += 1
                matches = False
            else:
                matches = snapshot['sender'] in lookup[source]
            metric = None if matches else 'left_out_sender'
    if matches and selector.get('wake_rules'):
        from .subscription_consumption import wake_reason
        evidence = {**snapshot, 'account': event['account'], 'kind': event['kind'],
            'historical': event['origin'] in ('backfill', 'reconcile'), 'reply_to_own_send': event['id'] in own}
        matches = bool(set(selector['wake_rules']) & set(wake_reason(evidence, selector)))
        if not matches and not failure:
            metric = 'left_out_wake_rule'
    return matches, metric, missing, selected, failure


def _scan_batch(c, row, p, events, head, count, maximum, scope, stats):
    """Walk the batch in event order, inserting matches, until a journal gap or the match budget stops it.
    Returns (position, reason, gap, missing, activity, newly_matched)."""
    visible, own = scope[1], scope[3]
    position = row['matched_through']
    newly_matched = False
    missing = 0
    activity = Counter()
    reason = None
    gap = row['gap'] if row['gap'] and row['gap'].get('intentional') else None
    for event in events:
        if event['id'] != position + 1:
            reason = 'journal_gap'
            gap = {'reason': reason, 'after': position, 'through': event['id'] - 1,
                'recoverable': False, 'requires_operator': True}
            break
        snapshot = event['snapshot']
        matches, metric, absent, selected, failure = _event_verdict(event, row, p, scope)
        missing += absent
        if matches:
            if count >= maximum:
                reason = 'match_budget'
                gap = {'reason': reason, 'after': position, 'through': head,
                    'recoverable': True, 'requires_operator': True}
                # The event stays unscanned: the next pass after the budget frees reads it again. A failure
                # event counts only as matched_failure, so a stop at one is not counted here.
                if not failure:
                    activity[(event['platform'], 'match_budget_blocked')] += 1
                break
            metric = 'matched_failure' if failure else 'matched'
            if visible is None:
                inserted = c.execute('''INSERT INTO subscription_matches(subscription,event)
                            VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING event''', (row['id'], event['id'])).fetchone()
            else:
                inserted = c.execute('''INSERT INTO subscription_matches(subscription,event,account,chat,
                            own_send_reply) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING event''',
                    (row['id'], event['id'], event['account'], snapshot.get('chat'), event['id'] in own)).fetchone()
            if inserted:
                count += 1
                stats['matched'] += 1
                newly_matched = True
        position = event['id']
        stats['scanned'] += 1
        # Every selected message event has a metric here, so scanned = matched + left_out_*; failure events
        # count only as matched_failure, and an event of an unselected chat counts nowhere.
        if metric is not None:
            activity[(event['platform'], metric)] += 1
        if selected and not failure:
            activity[(event['platform'], 'scanned')] += 1
    return position, reason, gap, missing, activity, newly_matched


def _record_batch(c, row, p, events, batch_size, head, scan, stats):
    """Store the batch's frontier, gap and counts, record its activity, then the delivery hint for new matches."""
    position, reason, gap, missing, activity, newly_matched = scan
    # If the retained range ends below a known committed frontier, the
    # tail was removed. Never convert that absence into a complete page.
    if reason is None and len(events) < batch_size and position < head:
        reason = 'journal_gap'
        gap = {'reason': reason, 'after': position, 'through': head,
            'recoverable': False, 'requires_operator': True}
    if reason:
        stats['blocked'] += 1
    stats['missing_evidence'] += missing
    c.execute('''UPDATE subscriptions SET matched_through=%s,blocked_reason=%s,gap=%s,
                missing_evidence=missing_evidence+%s,last_matched_at=clock_timestamp()
                WHERE id=%s''', (position, reason, Jsonb(gap) if gap else None, missing, row['id']))
    if activity and not _record_pass_activity(c, row, p, activity, logged=stats['activity_failed'] > 0):
        stats['activity_failed'] += 1
    if newly_matched:
        from .delivery_policy import after_match
        try:
            # The hint step cannot undo or block the matches and frontier above:
            # a failure rolls back only the savepoint and a later tick reopens the range.
            with c.transaction():
                after_match(c, row, position, p)
        except Exception as error:
            from .telemetry import capture
            capture(error, 'subscription_hint_failed')
            print(json.dumps({'error': 'subscription_hint_failed'}), flush=True)


def _match_modules(c, row, batch_size, maximum, stats):
    """ANYM-562: the module stream after the message pass, in a savepoint of its own: a failure is
    logged and rolls back only the module step; the message matches above still commit."""
    from . import module_events
    # ANYM-841: a rolled-back module step made no progress, so its counts never keep the matcher busy.
    progress = {key: stats.get(key, 0) for key in ('module_matched', 'module_scanned')}
    try:
        with c.transaction():
            module_events.match(c, row, batch_size=batch_size, maximum=maximum, stats=stats)
    except Exception as error:
        stats.update(progress)
        from .telemetry import capture
        capture(error, 'subscription_module_match_failed')
        print(json.dumps({'error': 'subscription_module_match_failed', 'class': type(error).__name__}), flush=True)


def _failure_audience(event, selected_chat, row, p):
    """send.failed: only the key that made the send, in a selected chat. source.health: a key that
    sees the whole account; a chat-restricted key reads no collector health (sync_status). contact.birthday: a
    selected chat outside a dynamic definition's exclusions."""
    if event['kind'] == 'contact.birthday':
        return selected_chat
    if event['kind'] == 'send.failed':
        snapshot = event['snapshot']
        return (selected_chat and snapshot.get('principal') == row['principal'] and
            snapshot.get('key_id') == str(row['key_id']))
    return event['account'] in scope(row, p)[0] and p['chat_ids'] is None


def _batch_scope(c, row, p, events):
    """ANYM-522: {(account, chat): chat_type} of this batch's chats the key can see now; one query."""
    accounts, chats = scope(row, p)
    wanted = sorted({event['snapshot']['chat'] for event in events if isinstance(event['snapshot'].get('chat'), str)})
    if not wanted or not accounts:
        return {}
    rows = c.execute('''SELECT id,account,chat_type FROM chats WHERE id=ANY(%s) AND account=ANY(%s)
        AND (%s::text[] IS NULL OR id=ANY(%s))''', (wanted, accounts, chats, chats)).fetchall()
    return {(item['account'], item['id']): item['chat_type'] for item in rows}


def _dynamic_match(sources, chat_type, snapshot):
    """None when the event passes, else why it is left out. Channels (Telegram broadcast channels have
    chat_type channel) and bot senders stay out unless asked for; chat_types (ANYM-825) takes only the
    named types, an untyped chat (NULL, e.g. every WhatsApp chat today) as unknown."""
    allowed = sources.get('chat_types')
    if allowed is not None:
        if (chat_type or 'unknown') not in allowed:
            return 'chat_type'
    elif chat_type == 'channel' and not sources['include_channels']:
        return 'channel'
    if not sources['include_bots'] and (snapshot.get('metadata') or {}).get('sender_is_bot') is True:
        return 'bot'
    return None


def left_out(row, event, reason):
    """ANYM-825: the matcher reports each event a dynamic rule left out (no match, no hint, no budget);
    ANYM-824 counts it as left_out_<reason> in the pass's activity; this hook stays a no-op."""


def record_activity(c, subscription, scope_version, counts):
    """ANYM-824: one upsert of a pass's {(platform, metric): n} into today's UTC row of each pair. Numbers, platform
    names and the day only; the caller holds the definition's row lock, so concurrent passes add in turn. The day is
    read once per statement (a materialized CTE), so a pass across midnight never splits its counts over two days."""
    pairs = sorted(counts)
    c.execute('''WITH today AS MATERIALIZED (SELECT (statement_timestamp() AT TIME ZONE 'UTC')::date AS day)
        INSERT INTO subscription_activity(subscription,day,platform,scope_version,metric,n)
        SELECT %s,today.day,u.platform,%s,u.metric,u.n
        FROM today,unnest(%s::text[],%s::text[],%s::bigint[]) AS u(platform,metric,n)
        ON CONFLICT (subscription,day,platform,scope_version,metric) DO UPDATE SET n=subscription_activity.n+EXCLUDED.n''',
        (subscription, scope_version, [pair[0] for pair in pairs], [pair[1] for pair in pairs],
         [counts[pair] for pair in pairs]))


def _record_pass_activity(c, row, p, counts, *, logged):
    """In a savepoint after the checkpoint update: a failed write (a missing table before schema 70, a lost grant)
    rolls back only the counts, never a match, the checkpoint, a hint or an ACK. Logs the error code once per pass."""
    try:
        with c.transaction():
            record_activity(c, row['id'], p['scope_version'], counts)
        return True
    except Exception as error:
        if not logged:
            print(json.dumps({'error': 'subscription_activity_failed',
                'code': getattr(error, 'sqlstate', None) or type(error).__name__}), flush=True)
        return False


def record_compact_read(c, p, subscription_id):
    """ANYM-826: a compact page read adds one `reads_compact` (platform `all`) through the same upsert, in a
    savepoint, so a failed count (a missing table, a lost grant, a lock timeout) never fails the read it counts.
    Callers pass the ID of the page the key just read: the key's own subscription only."""
    try:
        with c.transaction():
            record_activity(c, subscription_id, p['scope_version'], {('all', 'reads_compact'): 1})
        return True
    except Exception as error:
        print(json.dumps({'error': 'subscription_activity_failed',
            'code': getattr(error, 'sqlstate', None) or type(error).__name__}), flush=True)
        return False


ACTIVITY_DAYS = 7


def _activity(c, row, p):
    """ANYM-824: the last 7 UTC days, oldest first, of the key's current scope version only (counts earned under an
    older scope are never shown again); None when the table is missing (a schema-69 database)."""
    from psycopg import errors
    try:
        with c.transaction():
            rows = c.execute('''SELECT day,platform,metric,n FROM subscription_activity WHERE subscription=%s
                AND scope_version=%s AND n>0 AND day>(clock_timestamp() AT TIME ZONE 'UTC')::date-%s
                ORDER BY day,platform,metric''', (row['id'], p['scope_version'], ACTIVITY_DAYS)).fetchall()
    except (errors.UndefinedTable, errors.InsufficientPrivilege):
        return None
    days = {}
    for item in rows:
        days.setdefault(item['day'].isoformat(), {}).setdefault(item['platform'], {})[item['metric']] = item['n']
    return {'days': [{'day': day, 'platforms': platforms} for day, platforms in days.items()]}


def activity_ready(c):
    return c.execute("SELECT to_regclass('public.subscription_activity') IS NOT NULL AS ready").fetchone()['ready']


def _own_send_replies(c, row, events, visible):
    """ANYM-522 reply_to_own_send: IDs of arrivals that natively reply, in the same account and chat, to a
    confirmed send of this key, by the anchor rules of reply_to_confirmed_send: the reply names the send's
    exact provider message ID; or, on WhatsApp, its native stanza, projected from the exact outgoing archive
    row as reply_evidence.confirmed_native_reference does (the known chat prefix, and in a group the exact
    participant suffix); or the exact reply evidence frozen at ingestion names that archived message. One
    query per batch."""
    wanted = []
    for event in events:
        snapshot = event['snapshot']
        source = (event['account'], snapshot.get('chat'))
        if (event['kind'] != 'message.created' or snapshot.get('direction') != 'incoming' or
                event['origin'] in ('backfill', 'reconcile') or source not in visible):
            continue
        metadata = snapshot.get('metadata') or {}
        proof = snapshot.get('reply_evidence') or {}
        reference = metadata.get('reply_to_provider_id')
        if isinstance(reference, str) and reference:
            # The native projection, as for an anchor, unless the provider says it is no reply to the owner.
            wanted.append((event['id'], *source, reference, metadata.get('reply_to_owner') is not False))
        target = proof.get('target_provider_id') if proof.get('basis') == 'exact_native_reply_to_archived_outgoing' else None
        if isinstance(target, str) and target:
            wanted.append((event['id'], *source, target, False))
    if not wanted:
        return set()
    ids, accounts, chats, references, native = (list(column) for column in zip(*wanted))
    return {item['event'] for item in c.execute(r'''SELECT DISTINCT r.event
        FROM unnest(%s::bigint[],%s::text[],%s::text[],%s::text[],%s::boolean[]) AS r(event,account,chat,reference,native)
        JOIN outgoing_messages o ON o.account=r.account AND o.chat=r.chat
        WHERE o.principal=%s AND o.key_id=%s AND o.status='sent' AND o.provider_message_id<>''
        AND (o.provider_message_id=r.reference OR r.native AND length(r.reference)<=256 AND r.reference ~ '^[a-zA-Z0-9_.:-]+$'
            AND EXISTS(SELECT FROM accounts a WHERE a.id=o.account AND a.platform='whatsapp')
            AND EXISTS(SELECT FROM messages m WHERE m.account=o.account AND m.chat=o.chat
                AND m.provider_id=o.provider_message_id AND m.direction='outgoing' AND NOT m.deleted
                AND (m.provider_id='true_'||o.provider_chat||'_'||r.reference OR right(o.provider_chat,5)='@g.us'
                    AND m.sender ~ '^[a-zA-Z0-9_.:-]{1,140}@(c\.us|lid)$'
                    AND m.provider_id='true_'||o.provider_chat||'_'||r.reference||'_'||m.sender)))''',
        (ids, accounts, chats, references, native, row['principal'], row['key_id'])).fetchall()}


def _journal_failed(kind, error):
    """Never raises: the caller's send status or matching pass must survive its own diagnostics."""
    JOURNAL_FAILURES[kind] += 1
    try:
        from .telemetry import capture
        capture(error, 'subscription_journal_failed')
        print(json.dumps({'error': 'subscription_journal_failed', 'kind': kind, 'class': type(error).__name__}),
            flush=True)
    except Exception:
        pass


def journal_send_failures(c, rows):
    """ANYM-535/655: one reference event per failed/uncertain/expired/cancelled send.

    Each row has its own savepoint: an error rolls back that row and its counter step only, is logged
    and counted, and the send status commits anyway. IDs and codes only, never text.
    """
    for row in rows:
        if row['status'] not in ('failed', 'unknown', 'expired', 'cancelled'):
            continue
        try:
            with c.transaction():
                value = c.execute('UPDATE feed_counter SET value=value+1 WHERE id=1 RETURNING value').fetchone()['value']
                c.execute('''INSERT INTO events(id,account,message,kind,revision,origin,snapshot,event_key)
                    VALUES(%s,%s,NULL,'send.failed',1,'live',%s,%s)''', (value, row['account'], Jsonb({
                        'account': row['account'], 'chat': row['chat'], 'send_id': str(row['id']),
                        'status': row['status'], 'error_code': row['error_code'],
                        'principal': row['principal'], 'key_id': str(row['key_id'])}), 'send.failed:' + str(row['id'])))
                from .wake import EVENTS, mark
                mark(c, EVENTS)  # ANYM-841: the matcher wakes after the commit
        except Exception as error:
            _journal_failed('send.failed', error)


# Under the pool's 5 s statement_timeout that bounds every publisher waiting on the feed counter.
HEALTH_TRANSACTION_TIMEOUT = '2s'


def source_health_stale_seconds():
    """The operator's source.health stale threshold; checked at worker startup."""
    return 60 * _bound_setting('SOURCE_HEALTH_STALE_MINUTES', 30, 1, 1440)


def journal_source_health(store):
    """One call of schema 55's definer function; returns the rows journaled and never raises."""
    from psycopg import errors
    try:
        with store.pool.connection() as c:
            # Set first: the server ends the transaction, a stalled client included, before it can hold
            # the feed counter past a publisher's statement timeout.
            c.execute("SELECT set_config('transaction_timeout',%s,true)", (HEALTH_TRANSACTION_TIMEOUT,))
            c.execute("SET LOCAL lock_timeout='500ms'")
            return c.execute('SELECT public.journal_source_health(%s) AS n',
                (source_health_stale_seconds(),)).fetchone()['n']
    except errors.UndefinedFunction:
        return 0  # Schema before 55: the migration precedes this worker.
    except Exception as error:
        _journal_failed('source.health', error)
        return 0


RETENTION_BATCH = 500
# A deadline this close counts as passed. The server ends a batch's transaction, client pauses
# included, well before it, so nothing is deleted after expires_at even when the matcher has not
# marked the row expired yet.
RETENTION_EXPIRY_MARGIN = '1 minute'
# Under the pool's 5 s statement_timeout, so a stalled batch ends before a --disable waiting on it.
RETENTION_TRANSACTION_TIMEOUT = '4s'
# Shared by each batch, exclusive by the operator's switch: a disable waits for in-flight batches.
RETENTION_LOCK = 'subscription-retention'
# ANYM-447: finished hints, a closed definition's data after its closure and a tombstone each age out after it.
CLOSED_RETENTION_DAYS = 30
TERMINAL_STATES = ('closed', 'expired', 'access_changed')
# Set by set-subscription-retention --enable only while every component runs schema 62, so no older reader
# serves hint counts without the deleted ones.
TOMBSTONE_GATE = 'subscription_tombstones'


def lock_retention(c, *, shared):
    from . import subscription_retention
    return subscription_retention.lock_retention(c, shared=shared)


def acked_retention_days():
    from . import subscription_retention
    return subscription_retention.acked_retention_days()


def prune_subscriptions(store, *, max_subscriptions=16):
    from . import subscription_retention
    return subscription_retention.prune_subscriptions(store, max_subscriptions=max_subscriptions)


def _retention_batch(c, *gates):
    from . import subscription_retention
    return subscription_retention._retention_batch(c, *gates)


def _prune_batch(c, candidate, days):
    from . import subscription_retention
    return subscription_retention._prune_batch(c, candidate, days)


def retire_subscriptions(store, *, max_subscriptions=16):
    from . import subscription_retention
    return subscription_retention.retire_subscriptions(store, max_subscriptions=max_subscriptions)


_FINISHED = "state IN ('delivered','cancelled','dead_letter')"
# An events hint that bounds a later one's range (_PREVIOUS_HINT).
_BOUNDING = "kind='events' AND error_code IS DISTINCT FROM 'scope_changed'"


def _delete_hints(c, row, limit, *, days=None):
    from . import subscription_retention
    return subscription_retention._delete_hints(c, row, limit, days=days)


def _retire_hints(c, candidate):
    from . import subscription_retention
    return subscription_retention._retire_hints(c, candidate)


def _retire_definition(c, candidate):
    from . import subscription_retention
    return subscription_retention._retire_definition(c, candidate)


def set_dynamic_subscriptions(store, enabled, *, verify_components=False):
    """Operator only (ANYM-522). With verify_components (the CLI), enabling refuses unless the API, matcher
    and dispatcher heartbeats report this version, because an older matcher would scan a dynamic definition,
    match nothing and still advance its checkpoint. Disabling is the rollback before older code is deployed:
    it moves every dynamic definition to access_changed, which older code masks and refuses to resume, and
    cancels its queued hints; agents recreate them after a new enable."""
    with store.pool.connection() as c:
        # Waits for every creation that read the switch enabled.
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (DYNAMIC_LOCK,))
        if enabled and verify_components:
            from .heartbeats import mismatch
            problem = mismatch(c)
            if problem is not None:
                return problem
        c.execute('''INSERT INTO probe_metadata(name,value) VALUES(%s,%s)
            ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value''', (DYNAMIC_GATE, 'enabled' if enabled else 'disabled'))
        if enabled:
            return {'dynamic_subscriptions': 'enabled'}
        # The row lock waits for a hint in flight under the dispatcher's fence; the version bump cancels held ones.
        moved = [str(row['id']) for row in c.execute("""UPDATE subscriptions SET state='access_changed',
            version=version+1,updated_at=clock_timestamp() WHERE selector ? 'sources' AND state<>'access_changed'
            RETURNING id""").fetchall()]
        cancelled = [str(row['id']) for row in c.execute("""UPDATE subscription_outbox SET state='cancelled',
            finished_at=clock_timestamp(),error_code='dynamic_sources_disabled',lease_token=NULL,lease_until=NULL
            WHERE subscription IN (SELECT id FROM subscriptions WHERE selector ? 'sources')
            AND state IN ('pending','leased') RETURNING id""").fetchall()]
    return {'dynamic_subscriptions': 'disabled', 'access_changed': sorted(moved), 'cancelled_hints': sorted(cancelled)}


def set_source_filters(store, enabled, *, verify_components=False):
    """Operator only (ANYM-825), built like set_dynamic_subscriptions. With verify_components (the CLI),
    enabling refuses unless the API, matcher and dispatcher heartbeats report this version, because an older
    matcher would store matches of every chat type for a definition with chat_types and readers do not filter
    stored matches by type. Disabling is the rollback before older code is deployed: every such definition
    becomes access_changed (masked by older code, refused on resume) and its queued hints are cancelled;
    agents recreate them after a new enable."""
    with store.pool.connection() as c:
        # Waits for every creation that read the switch enabled.
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (SOURCE_FILTER_LOCK,))
        if enabled and verify_components:
            from .heartbeats import mismatch
            # The package version alone cannot tell this matcher from the deployed one (both 0.9.19).
            problem = mismatch(c, features=('source-filters',))
            if problem is not None:
                return problem
        c.execute('''INSERT INTO probe_metadata(name,value) VALUES(%s,%s)
            ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value''', (SOURCE_FILTER_GATE, 'enabled' if enabled else 'disabled'))
        if enabled:
            return {'source_filters': 'enabled'}
        # The row lock waits for a hint in flight under the dispatcher's fence; the version bump cancels held ones.
        moved = [str(row['id']) for row in c.execute("""UPDATE subscriptions SET state='access_changed',
            version=version+1,updated_at=clock_timestamp() WHERE selector->'sources' ? 'chat_types'
            AND state<>'access_changed' RETURNING id""").fetchall()]
        cancelled = [str(row['id']) for row in c.execute("""UPDATE subscription_outbox SET state='cancelled',
            finished_at=clock_timestamp(),error_code='source_filters_disabled',lease_token=NULL,lease_until=NULL
            WHERE subscription IN (SELECT id FROM subscriptions WHERE selector->'sources' ? 'chat_types')
            AND state IN ('pending','leased') RETURNING id""").fetchall()]
    return {'source_filters': 'disabled', 'access_changed': sorted(moved), 'cancelled_hints': sorted(cancelled)}


def set_source_exclusions(store, enabled, *, verify_components=False):
    """Operator only (ANYM-830), built like set_source_filters. Enabling (the CLI) refuses unless the API, matcher
    and dispatcher heartbeats report the source-exclusions feature, because an older matcher matches listed chats
    and older readers show their references. Disabling is the rollback before older code is deployed: every
    definition that carries an exclusion list (or its revision) becomes access_changed and its queued hints are
    cancelled; agents recreate them after a new enable."""
    with store.pool.connection() as c:
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (SOURCE_EXCLUSION_LOCK,))
        if enabled and verify_components:
            from .heartbeats import mismatch
            problem = mismatch(c, features=('source-exclusions',))
            if problem is not None:
                return problem
        c.execute('''INSERT INTO probe_metadata(name,value) VALUES(%s,%s)
            ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value''', (SOURCE_EXCLUSION_GATE, 'enabled' if enabled else 'disabled'))
        if enabled:
            return {'source_exclusions': 'enabled'}
        listed = "(selector->'sources' ? 'exclude_chats' OR selector->'sources' ? 'exclusions_revision')"
        moved = [str(row['id']) for row in c.execute("""UPDATE subscriptions SET state='access_changed',
            version=version+1,updated_at=clock_timestamp() WHERE """ + listed + """
            AND state<>'access_changed' RETURNING id""").fetchall()]
        cancelled = [str(row['id']) for row in c.execute("""UPDATE subscription_outbox SET state='cancelled',
            finished_at=clock_timestamp(),error_code='source_exclusions_disabled',lease_token=NULL,lease_until=NULL
            WHERE subscription IN (SELECT id FROM subscriptions WHERE """ + listed + """)
            AND state IN ('pending','leased') RETURNING id""").fetchall()]
    return {'source_exclusions': 'disabled', 'access_changed': sorted(moved), 'cancelled_hints': sorted(cancelled)}
