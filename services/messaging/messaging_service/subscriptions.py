"""Bounded key-owned subscriptions over the committed archive journal.

This matcher atomically enqueues registered opaque hints; it never runs agents.
Definitions and exact journal references are durable; the independent matcher
advances each checkpoint in the same transaction as its matching references.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
import json
import os
from uuid import UUID

from psycopg.types.json import Jsonb

from .policy import effective_permissions, lock_principal, targets

EVENT_KINDS = frozenset(('message.created', 'message.edited', 'message.deleted',
    'message.status', 'attachment.ready', 'file.text.ready', 'file.transcript.ready',
    'file.description.ready', 'file.processing.failed', 'send.failed', 'source.health'))
# ANYM-535: journal rows that name no archived message, matched by their own rule and never by default.
FAILURE_KINDS = frozenset(('send.failed', 'source.health'))
# A journal write the send or collector path survived, per kind, since process start.
JOURNAL_FAILURES = Counter()
ORIGINS = frozenset(('live', 'fixture', 'backfill', 'reconcile'))
WAKE_RULES = frozenset(('incoming_message', 'owner_mention', 'reply_to_owner', 'reply_to_confirmed_send',
    'reply_to_own_send'))
# ANYM-522: the operator's switch for dynamic sources (set-dynamic-subscriptions); creation reads it under
# the shared lock, so --disable waits for every creation that read it enabled.
DYNAMIC_GATE = 'dynamic_subscriptions'
DYNAMIC_LOCK = 'dynamic-subscriptions'
SOURCE_OPTIONS = frozenset(('all_visible_chats', 'account_ids', 'include_channels', 'include_bots'))


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
    return ('coalesce(NOT ' + alias + '.hidden AND ' + alias + '.account=ANY(%s) AND (%s::text[] IS NULL OR ' +
        alias + '.chat=ANY(%s)),false)', [accounts, chats, chats])


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
    return serial({'subscription_id': row['id'], 'state': row['state'], 'version': row['version'],
        'label': row['label'], **row['selector'], 'created_at': row['created_at'],
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



def _hint_counts(c, row, p, ids=None):
    """{state: n} of the definition's hints the key can see now; ids narrows it to those outbox rows."""
    if dynamic(row):
        if p is None:
            return {}  # ANYM-447: no key can read it now, so the worker counts none and discloses nothing.
        # ANYM-522: only events hints that cover a reference the key can see now count, in every state.
        where, args = visible_filter(row, p)
        counts = c.execute('''SELECT o.state,count(*) AS n FROM subscription_outbox o WHERE o.subscription=%s
            AND (%s::uuid[] IS NULL OR o.id=ANY(%s))
            AND o.error_code IS DISTINCT FROM 'scope_changed' AND (o.kind<>'events' OR EXISTS(
                SELECT FROM subscription_matches m WHERE m.subscription=o.subscription AND m.event<=o.through_position
                AND m.event>''' + _PREVIOUS_HINT + ''' AND ''' + where + ''')) GROUP BY o.state''',
            (row['id'], ids, ids, hint_floor(row), *args)).fetchall()
    else:
        counts = c.execute('''SELECT state,count(*) AS n FROM subscription_outbox WHERE subscription=%s
            AND (%s::uuid[] IS NULL OR id=ANY(%s)) GROUP BY state''', (row['id'], ids, ids)).fetchall()
    return {item['state']: item['n'] for item in counts}


def _delivery_view(c, row, p=None):
    # Every column: before schema 49 the binding has no endpoint column (rolling upgrade).
    binding = c.execute('SELECT * FROM subscription_delivery WHERE subscription=%s', (row['id'],)).fetchone()
    # ANYM-447: counters that never decrease: the hints the worker deleted plus the rows still kept.
    live, deleted = _hint_counts(c, row, p), row.get('deleted_hint_counts') or {}
    # An unbound subscription reads no policy state, so its view needs no schema-38 column.
    from .delivery_policy import UNBOUND, delivery_state
    view = {'passive_read': True, 'ack': True, 'push': binding is not None,
        'receiver_id': str(binding['receiver']) if binding and binding['receiver'] else None, 'host_wakeup': 'unverified',
        'host_turn_verified': False,
        'hint_counts': {state: live.get(state, 0) + deleted.get(state, 0) for state in sorted({*live, *deleted})},
        'deleted_hint_counts': deleted, 'hints_deleted_at': row.get('hints_deleted_at'),
        **(delivery_state(c, row) if binding else UNBOUND)}
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
    return {'all_visible_chats': True, 'account_ids': accounts,
        'include_channels': sources.get('include_channels', False), 'include_bots': sources.get('include_bots', False)}


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
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
        ('subscriptions:' + str(p['key_id']),))
    maximum = _bound_setting('SUBSCRIPTION_MAX_DEFINITIONS', 64, 1, 1000)
    # ANYM-447: a tombstone older than 30 days no longer counts (schema 62; an older database has none).
    aged = tombstones_ready(c)
    count = c.execute('SELECT count(*) AS n FROM subscriptions WHERE principal=%s AND key_id=%s' + (
        ' AND (tombstoned_at IS NULL OR tombstoned_at>=clock_timestamp()-make_interval(days=>%s))' if aged else ''),
        (p['id'], p['key_id'], *((CLOSED_RETENTION_DAYS,) if aged else ()))).fetchone()['n']
    if count >= maximum:
        raise APIError('subscription_limit', 429)
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
    if delivery is not None:
        from .event_delivery import configure_subscription_delivery
        result = configure_subscription_delivery(store, c, p, str(row['id']), row['version'], None,
            delivery.get('delivery_policy'), delivery['endpoint_id'])
        targets(p, [str(row['id'])])
        return result
    return _view(c, row, p)


def list_subscriptions(store, c, p, *, limit=50):
    from .store import bounded
    bounded(limit)
    rows = c.execute('''SELECT * FROM subscriptions WHERE principal=%s AND key_id=%s
        ORDER BY created_at DESC,id DESC LIMIT %s FOR UPDATE''', (p['id'], p['key_id'], limit + 1)).fetchall()
    result = [_view(c, _state(c, row, p), p) for row in rows[:limit]]
    targets(p, [str(row['id']) for row in rows[:limit]])
    return {'items': result, 'has_more': len(rows) > limit}


def get_subscription(store, c, p, subscription_id):
    return _view(c, _owned(c, p, subscription_id), p)


def update_subscription(store, c, p, subscription_id, version, *, action, resume_policy='catch_up'):
    from .store import APIError
    if 'read' not in p['permissions']:
        raise APIError('forbidden', 403)
    if type(version) is not int or not 1 <= version <= 2**53 - 1:
        raise APIError('invalid_version')
    if action not in ('pause', 'resume', 'close') or resume_policy not in ('catch_up', 'skip'):
        raise APIError('invalid_subscription_state')
    if action != 'resume' and resume_policy != 'catch_up':
        raise APIError('invalid_subscription_state')
    row = _owned(c, p, subscription_id)
    if row['state'] == 'access_changed':
        # Do not include the old version, selectors or counts in an error.
        raise APIError('subscription_access_changed', 409)
    if row['version'] != version:
        raise APIError('subscription_conflict', 409)
    if row['state'] in ('closed', 'expired'):
        raise APIError('subscription_closed', 409)
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
    row = c.execute('''UPDATE subscriptions SET state=%s,version=version+1,
        matched_through=%s,gap=%s,blocked_reason=NULL,updated_at=clock_timestamp()
        WHERE id=%s RETURNING *''', (state, position, Jsonb(gap) if gap else None, row['id'])).fetchone()
    return _view(c, row, p)


def get_subscription_events(store, c, p, subscription_id, *, after=0, limit=50, module_after=None):
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
    if after < floor:
        state['gap'] = {'reason': 'retention_gap', 'after': after, 'through': floor,
            'recoverable': False, 'floor': floor}
        return serial({'subscription': state, 'items': [], 'has_more': True, 'next_after': floor,
            'consumption': 'passive_read_no_ack', 'scope_masked': False, **modules})
    # ANYM-522: a dynamic definition returns only references the key can see now.
    where, args = visible_filter(row, p, 'sm')
    events = c.execute('''SELECT e.id,e.kind,e.origin,e.revision,e.created_at,''' +
        ('sm.own_send_reply' if dynamic(row) else 'false AS own_send_reply') + ''',
        jsonb_strip_nulls(jsonb_build_object(
            'message',e.snapshot->'message','account',e.snapshot->'account','chat',e.snapshot->'chat',
            'sender',e.snapshot->'sender','provider_id',e.snapshot->'provider_id',
            'sent_at',e.snapshot->'sent_at','direction',e.snapshot->'direction',
            'deleted',e.snapshot->'deleted','processing',e.snapshot->'processing',
            -- ANYM-529: a shared contact card is message content: the journal keeps it, references do not.
            'metadata',(e.snapshot->'metadata')-'contact','reply_evidence',e.snapshot->'reply_evidence',
            -- ANYM-535: IDs and codes only; the sending key stays in the journal.
            'send_id',CASE WHEN e.kind='send.failed' THEN e.snapshot->'send_id' END,
            'error_code',CASE WHEN e.kind='send.failed' THEN e.snapshot->'error_code' END,
            'status',CASE WHEN e.kind IN ('send.failed','source.health') THEN e.snapshot->'status' END)) AS snapshot
        FROM subscription_matches sm JOIN events e ON e.id=sm.event
        WHERE sm.subscription=%s AND sm.event>%s AND ''' + where + ''' ORDER BY sm.event LIMIT %s''',
        (row['id'], after, *args, limit + 1)).fetchall()
    fields = ('message', 'account', 'chat', 'sender', 'provider_id', 'revision', 'sent_at',
        'direction', 'deleted', 'processing', 'metadata', 'reply_evidence', 'send_id', 'error_code', 'status')
    items = [{**{key: event['snapshot'][key] for key in fields if key in event['snapshot']},
        'event_id': event['id'], 'kind': event['kind'], 'origin': event['origin'],
        'revision': event['revision'], 'observed_at': event['created_at'],
        'historical': event['origin'] in ('backfill', 'reconcile'),
        'matching_basis': 'exact_event_revision'} for event in events[:limit]]
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
        'expired': 0, 'blocked': 0, 'missing_evidence': 0, 'module_matched': 0, 'module_blocked': 0, 'scope_failed': 0}
    with store.pool.connection() as c:
        from . import module_events
        modules = module_events.ready(c)
        candidates = c.execute('''SELECT id,principal,key_id FROM subscriptions WHERE state='active'
            ORDER BY last_matched_at NULLS FIRST,created_at,id LIMIT %s''', (max_subscriptions,)).fetchall()
    for candidate in candidates:
        with store.pool.connection() as c:
            # ConnectionPool exits commit. Locks cover exactly one bounded
            # subscription batch, not the whole pass or any provider operation.
            p = _worker_identity(c, candidate)
            row = c.execute("SELECT * FROM subscriptions WHERE id=%s AND state='active' FOR UPDATE SKIP LOCKED",
                (candidate['id'],)).fetchone()
            if row is None:
                continue
            stats['checked'] += 1
            if p is None:
                c.execute("""UPDATE subscriptions SET state='access_changed',version=version+1,
                    updated_at=clock_timestamp() WHERE id=%s""", (row['id'],))
                stats['access_changed'] += 1
                continue
            row = _state(c, row, p)
            if row['state'] != 'active':
                stats[row['state']] += 1
                continue
            sources = [] if dynamic(row) else c.execute('''SELECT account,chat,sender_ids FROM subscription_sources
                WHERE subscription=%s''', (row['id'],)).fetchall()
            # Version equality is the fence, with a full source-scope check as a
            # defensive boundary if records are restored or repaired externally.
            if any(source['account'] not in p['accounts'] or
                    p['chat_ids'] is not None and source['chat'] not in p['chat_ids'] for source in sources):
                c.execute("""UPDATE subscriptions SET state='access_changed',version=version+1,
                    updated_at=clock_timestamp() WHERE id=%s""", (row['id'],))
                stats['access_changed'] += 1
                continue
            head = c.execute('SELECT value FROM feed_counter WHERE id=1').fetchone()['value']
            events = c.execute('''SELECT id,account,kind,origin,
                jsonb_strip_nulls(jsonb_build_object('chat',snapshot->'chat',
                    'sender',snapshot->'sender','direction',snapshot->'direction',
                    'metadata',snapshot->'metadata','reply_evidence',snapshot->'reply_evidence',
                    'principal',snapshot->'principal','key_id',snapshot->'key_id')) AS snapshot
                FROM events WHERE id>%s AND id<=%s ORDER BY id LIMIT %s''',
                (row['matched_through'], head, batch_size)).fetchall()
            count = c.execute('SELECT count(*) AS n FROM subscription_matches WHERE subscription=%s',
                (row['id'],)).fetchone()['n']
            lookup = {(source['account'], source['chat']): source['sender_ids'] for source in sources}
            selector = row['selector']
            # ANYM-522: one locked scope snapshot per batch; the shared principal lock holds it until commit.
            visible, own = None, set()
            if dynamic(row):
                try:
                    with c.transaction():
                        visible = _batch_scope(c, row, p, events)
                        if visible and 'reply_to_own_send' in selector.get('wake_rules', ()):
                            own = _own_send_replies(c, row, events, visible)
                except Exception as error:
                    # Only this definition waits: it goes to the back of the queue, the pass goes on.
                    from .telemetry import capture
                    capture(error, 'subscription_scope_failed')
                    print(json.dumps({'error': 'subscription_scope_failed', 'class': type(error).__name__}), flush=True)
                    c.execute('UPDATE subscriptions SET last_matched_at=clock_timestamp() WHERE id=%s', (row['id'],))
                    stats['scope_failed'] += 1
                    continue
            position = row['matched_through']
            newly_matched = False
            missing = 0
            reason = None
            gap = row['gap'] if row['gap'] and row['gap'].get('intentional') else None
            for event in events:
                if event['id'] != position + 1:
                    reason = 'journal_gap'
                    gap = {'reason': reason, 'after': position, 'through': event['id'] - 1,
                        'recoverable': False, 'requires_operator': True}
                    break
                snapshot = event['snapshot']
                source = (event['account'], snapshot.get('chat'))
                selected = source in lookup if visible is None else source in visible
                if event['kind'] in FAILURE_KINDS:
                    # Direction and senders describe messages; these kinds have their own audience.
                    matches = (event['kind'] in selector['kinds'] and event['origin'] in selector['origins'] and
                        _failure_audience(event, selected, row, p))
                else:
                    matches = (selected and event['kind'] in selector['kinds'] and
                        event['origin'] in selector['origins'])
                    if matches and visible is not None:
                        matches = _dynamic_match(selector['sources'], visible[source], snapshot)
                    if matches and selector['direction']:
                        if 'direction' not in snapshot:
                            missing += 1
                            matches = False
                        else:
                            matches = snapshot['direction'] == selector['direction']
                    if matches and lookup.get(source) is not None:
                        if 'sender' not in snapshot:
                            missing += 1
                            matches = False
                        else:
                            matches = snapshot['sender'] in lookup[source]
                if matches and selector.get('wake_rules'):
                    from .subscription_consumption import wake_reason
                    evidence = {**snapshot, 'account': event['account'], 'kind': event['kind'],
                        'historical': event['origin'] in ('backfill', 'reconcile'), 'reply_to_own_send': event['id'] in own}
                    matches = bool(set(selector['wake_rules']) & set(wake_reason(evidence, selector)))
                if matches:
                    if count >= maximum:
                        reason = 'match_budget'
                        gap = {'reason': reason, 'after': position, 'through': head,
                            'recoverable': True, 'requires_operator': True}
                        break
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
            if modules:
                _match_modules(c, row, batch_size, maximum, stats)
    return stats


def _match_modules(c, row, batch_size, maximum, stats):
    """ANYM-562: the module stream after the message pass, in a savepoint of its own: a failure is
    logged and rolls back only the module step; the message matches above still commit."""
    from . import module_events
    try:
        with c.transaction():
            module_events.match(c, row, batch_size=batch_size, maximum=maximum, stats=stats)
    except Exception as error:
        from .telemetry import capture
        capture(error, 'subscription_module_match_failed')
        print(json.dumps({'error': 'subscription_module_match_failed', 'class': type(error).__name__}), flush=True)


def _failure_audience(event, selected_chat, row, p):
    """send.failed: only the key that made the send, in a selected chat. source.health: a key that
    sees the whole account; a chat-restricted key reads no collector health (sync_status)."""
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
    """Channels (Telegram broadcast channels have chat_type channel) and bot senders stay out unless asked for."""
    if chat_type == 'channel' and not sources['include_channels']:
        return False
    return sources['include_bots'] or (snapshot.get('metadata') or {}).get('sender_is_bot') is not True


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
    """ANYM-535: one send.failed row per failed or uncertain send, in the caller's transaction.

    Each row has its own savepoint: an error rolls back that row and its counter step only, is logged
    and counted, and the send status commits anyway. IDs and codes only, never text.
    """
    for row in rows:
        if row['status'] not in ('failed', 'unknown'):
            continue
        try:
            with c.transaction():
                value = c.execute('UPDATE feed_counter SET value=value+1 WHERE id=1 RETURNING value').fetchone()['value']
                c.execute('''INSERT INTO events(id,account,message,kind,revision,origin,snapshot,event_key)
                    VALUES(%s,%s,NULL,'send.failed',1,'live',%s,%s)''', (value, row['account'], Jsonb({
                        'account': row['account'], 'chat': row['chat'], 'send_id': str(row['id']),
                        'status': row['status'], 'error_code': row['error_code'],
                        'principal': row['principal'], 'key_id': str(row['key_id'])}), 'send.failed:' + str(row['id'])))
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
    """Advisory, so the worker needs no UPDATE right on probe_metadata just for a row lock."""
    if not shared:
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (RETENTION_LOCK,))
        return True
    # A queued disable makes this fail at once, so batches never starve the operator.
    return c.execute('SELECT pg_try_advisory_xact_lock_shared(hashtextextended(%s,0)) AS locked',
        (RETENTION_LOCK,)).fetchone()['locked']


def acked_retention_days():
    """Checked at worker startup, so an invalid value fails before any deletion."""
    return _bound_setting('SUBSCRIPTION_ACKED_RETENTION_DAYS', 7, 1, 366)


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
    days = acked_retention_days()
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
                (RETENTION_EXPIRY_MARGIN, days, max_subscriptions)).fetchall()
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
                (RETENTION_EXPIRY_MARGIN, days, max_subscriptions)).fetchall() if module_events.ready(c) else []
    except Exception as error:
        from .telemetry import capture
        capture(error, 'subscription_retention_failed')
        modules = []
    for candidate in [*candidates, *modules]:
        try:
            with store.pool.connection() as c:
                deleted = _prune_batch(c, candidate, days)
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
    c.execute("SELECT set_config('transaction_timeout',%s,true)", (RETENTION_TRANSACTION_TIMEOUT,))
    # Bounds well under one matcher pass; a contended subscription waits for the next pass.
    c.execute("SET LOCAL lock_timeout='100ms'")
    c.execute("SET LOCAL statement_timeout='1s'")
    # The activation is reread under the shared lock: once --disable returns, no batch deletes.
    names = ['subscription_retention', *gates]
    return lock_retention(c, shared=True) and c.execute('''SELECT count(*)=%s AS enabled FROM probe_metadata
        WHERE name=ANY(%s) AND value='enabled' ''', (len(names), names)).fetchone()['enabled']


def _prune_batch(c, candidate, days):
    """One transaction. Returns rows deleted, or None when retention is off or the subscription
    is busy, held or past its deadline."""
    if not _retention_batch(c):
        return None
    # The matcher's order: the shared principal lock, then the subscription row.
    lock_principal(c, candidate['principal'], shared=True)
    row = c.execute('''SELECT id,state,retention_floor,retention_hold,
        expires_at IS NOT NULL AND expires_at<=clock_timestamp()+%s::interval AS lapsed
        FROM subscriptions WHERE id=%s FOR UPDATE SKIP LOCKED''',
        (RETENTION_EXPIRY_MARGIN, candidate['id'])).fetchone()
    # A quarantine closes every definition: terminal ones keep their references (ANYM-446 F2) until
    # retire_subscriptions takes them 30 days after closure (ANYM-447). A passed deadline is terminal before the
    # matcher stores 'expired'.
    if row is None or row['retention_hold'] or row['lapsed'] or row['state'] in TERMINAL_STATES:
        return None
    if candidate.get('modules'):
        from .module_events import prune
        return prune(c, row['id'], days, RETENTION_BATCH)
    # ACK and fetch hold the subscription row, so acked_through cannot move under this batch.
    consumption = c.execute('SELECT acked_through FROM subscription_consumption WHERE subscription=%s',
        (row['id'],)).fetchone()
    if consumption is None:
        return 0
    refs = c.execute('''SELECT event,matched_at<clock_timestamp()-make_interval(days=>%s) AS expired
        FROM subscription_matches WHERE subscription=%s AND event>%s AND event<=%s ORDER BY event LIMIT %s''',
        (days, row['id'], row['retention_floor'], consumption['acked_through'], RETENTION_BATCH)).fetchall()
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
    stats = {'hints_deleted': 0, 'rows_deleted': 0, 'tombstoned': 0, 'retire_skipped': 0, 'retire_failed': 0,
        'more': False}
    gates = ['subscription_retention', TOMBSTONE_GATE]
    try:
        with store.pool.connection() as c:
            candidates = c.execute('''SELECT id,principal,true AS closed FROM subscriptions
                WHERE state IN ('closed','expired','access_changed') AND NOT retention_hold AND tombstoned_at IS NULL
                    AND closed_at<clock_timestamp()-make_interval(days=>%s)
                    AND (SELECT count(*) FROM probe_metadata WHERE name=ANY(%s) AND value='enabled')=2
                ORDER BY closed_at,id LIMIT %s''', (CLOSED_RETENTION_DAYS, gates, max_subscriptions)).fetchall()
            # One probe per definition on the outbox's (subscription,...) unique key; test hints stay (below).
            candidates += c.execute('''SELECT s.id,s.principal,false AS closed FROM subscriptions s
                WHERE s.state IN ('active','paused') AND NOT s.retention_hold
                    AND EXISTS(SELECT FROM subscription_outbox o WHERE o.subscription=s.id AND o.kind<>'test'
                        AND o.state IN ('delivered','cancelled','dead_letter')
                        AND o.finished_at<clock_timestamp()-make_interval(days=>%s))
                    AND (SELECT count(*) FROM probe_metadata WHERE name=ANY(%s) AND value='enabled')=2
                ORDER BY s.id LIMIT %s''', (CLOSED_RETENTION_DAYS, gates, max_subscriptions)).fetchall()
    except errors.UndefinedColumn:
        return stats  # Schema before 62: the migration precedes this worker, so nothing is deleted meanwhile.
    for candidate in candidates:
        try:
            with store.pool.connection() as c:
                result = (_retire_definition if candidate['closed'] else _retire_hints)(c, candidate)
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
    return stats


_FINISHED = "state IN ('delivered','cancelled','dead_letter')"
# An events hint that bounds a later one's range (_PREVIOUS_HINT).
_BOUNDING = "kind='events' AND error_code IS DISTINCT FROM 'scope_changed'"


def _delete_hints(c, row, limit, *, days=None):
    """Delete up to limit finished hints (finished more than days ago, if given); their counts the key could see
    move to deleted_hint_counts in the same transaction. Returns rows deleted.

    Bounding events hints go only as a prefix by position, strictly below every one that stays, and
    hints_deleted_through takes the highest deleted position, so each kept hint's range (hint_floor) and hint_visible
    stay exactly as they were. With days (an open definition) test hints stay: their request IDs replay."""
    finished = _FINISHED + ' AND (%s::integer IS NULL OR finished_at<clock_timestamp()-make_interval(days=>%s))'
    # No row lock first: the worker role has no UPDATE on the outbox, and finished rows no longer change.
    keep = c.execute('SELECT min(through_position) AS p FROM subscription_outbox WHERE subscription=%s AND ' +
        _BOUNDING + ' AND NOT (' + finished + ')', (row['id'], days, days)).fetchone()['p']
    rows = c.execute('SELECT id,through_position,' + _BOUNDING + ''' AS bounding FROM subscription_outbox
        WHERE subscription=%s AND ''' + finished + ''' AND (%s OR kind<>'test')
        AND (NOT (''' + _BOUNDING + ''') OR %s::bigint IS NULL OR through_position<%s)
        ORDER BY through_position,id LIMIT %s''', (row['id'], days, days, days is None, keep, keep, limit)).fetchall()
    if not rows:
        return 0
    if len(rows) == limit and any(item['bounding'] for item in rows):
        # A cut inside equal positions would leave a kept hint at the new floor: take that position whole.
        top = max(item['through_position'] for item in rows if item['bounding'])
        chosen = {item['id'] for item in rows}
        rows += c.execute('SELECT id,through_position,true AS bounding FROM subscription_outbox WHERE subscription=%s AND ' +
            _BOUNDING + ' AND through_position=%s AND NOT id=ANY(%s) AND ' + finished,
            (row['id'], top, list(chosen), days, days)).fetchall()
    ids = [item['id'] for item in rows]
    # The view's own filter at deletion time, references still present, so hint_counts never drops.
    counts = _hint_counts(c, row, _worker_identity(c, row) if dynamic(row) else None, ids)
    deleted = c.execute('DELETE FROM subscription_outbox WHERE id=ANY(%s) AND ' + _FINISHED, (ids,)).rowcount
    kept = row['deleted_hint_counts']
    through = max([item['through_position'] for item in rows if item['bounding']], default=0)
    c.execute('''UPDATE subscriptions SET deleted_hint_counts=%s,hints_deleted_at=clock_timestamp(),
        hints_deleted_through=greatest(hints_deleted_through,%s) WHERE id=%s''',
        (Jsonb({state: kept.get(state, 0) + counts.get(state, 0) for state in sorted({*kept, *counts})}), through,
         row['id']))
    return deleted


def _retire_hints(c, candidate):
    """One transaction: finished hints older than 30 days of an active or paused definition."""
    if not _retention_batch(c, TOMBSTONE_GATE):
        return None
    # The matcher's order: the shared principal lock, then the subscription row.
    lock_principal(c, candidate['principal'], shared=True)
    row = c.execute('SELECT * FROM subscriptions WHERE id=%s FOR UPDATE SKIP LOCKED', (candidate['id'],)).fetchone()
    # Rechecked under the row lock: a hold or a closure since selection wins.
    if row is None or row['retention_hold'] or row['state'] not in ('active', 'paused'):
        return None
    hints = _delete_hints(c, row, RETENTION_BATCH, days=CLOSED_RETENTION_DAYS)
    return {'hints': hints, 'rows': hints, 'tombstoned': 0, 'more': hints >= RETENTION_BATCH}


def _retire_definition(c, candidate):
    """One transaction of at most RETENTION_BATCH rows of a definition closed more than 30 days ago: finished hints
    first (counted while their references exist); once no hint is left, matches from the bottom (the floor follows,
    so a reader in between gets retention_gap) and module matches; then receipts and consumption, and the floor
    becomes matched_through (the tombstone)."""
    if not _retention_batch(c, TOMBSTONE_GATE):
        return None
    lock_principal(c, candidate['principal'], shared=True)
    row = c.execute('''SELECT *,closed_at<clock_timestamp()-make_interval(days=>%s) AS due FROM subscriptions
        WHERE id=%s FOR UPDATE SKIP LOCKED''', (CLOSED_RETENTION_DAYS, candidate['id'])).fetchone()
    if (row is None or row['retention_hold'] or not row['due'] or row['tombstoned_at'] is not None or
            row['state'] not in TERMINAL_STATES):
        return None
    hints = _delete_hints(c, row, RETENTION_BATCH)
    budget = RETENTION_BATCH - hints
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
    result['rows'] = RETENTION_BATCH - budget
    # Each DELETE took up to the budget left, so a batch with budget left drained both reference tables.
    # Receipts (at most 128 per definition) and consumption count toward the batch too; a fresh batch takes them.
    tail = c.execute('''SELECT (SELECT count(*) FROM subscription_receipts WHERE subscription=%s)+
        (SELECT count(*) FROM subscription_consumption WHERE subscription=%s) AS n''', (row['id'], row['id'])).fetchone()['n']
    if budget <= 0 or tail > budget and budget < RETENTION_BATCH:
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
