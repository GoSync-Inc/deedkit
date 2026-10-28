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


def _state(c, row, p):
    """ACL changes never revive a previous definition, even after restoration."""
    changed = (row['scope_version'] != p['scope_version'] or
        row['key_permission_version'] != p['key_permission_version'] or
        not {'read', 'subscription-write'} <= set(p['permissions']))
    if not changed:
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


def _view(c, row):
    from .store import serial
    if row['state'] == 'access_changed':
        return {'subscription_id': str(row['id']), 'state': 'access_changed',
            'version': row['version'], 'recreate_required': True, 'scope_masked': True}
    head = c.execute('SELECT value FROM feed_counter WHERE id=1').fetchone()['value']
    count = c.execute('SELECT count(*) AS n FROM subscription_matches WHERE subscription=%s',
        (row['id'],)).fetchone()['n']
    # ANYM-518: the consumer's ACK position; before its first fetch, the start.
    consumption = c.execute('SELECT acked_through FROM subscription_consumption WHERE subscription=%s',
        (row['id'],)).fetchone()
    return serial({'subscription_id': row['id'], 'state': row['state'], 'version': row['version'],
        'label': row['label'], **row['selector'], 'created_at': row['created_at'],
        'updated_at': row['updated_at'], 'expires_at': row['expires_at'],
        'start_position': row['start_position'], 'matched_through': row['matched_through'],
        'matcher_caught_up': row['state'] == 'active' and row['matched_through'] == head,
        'matched_event_count': count,
        'acked_through': consumption['acked_through'] if consumption else row['start_position'],
        'blocked_reason': row['blocked_reason'], 'gap': row['gap'],
        'matching_evidence': 'exact_event_revision',
        'missing_revision_evidence_events': row['missing_evidence'],
        'last_matched_at': row['last_matched_at'], 'scope_masked': False,
        'freshness_basis': 'committed_archive_journal_not_provider_freshness',
        'delivery': _delivery_view(c, row), **_module_view(c, row)})


def _module_view(c, row):
    # ANYM-562: only a subscription with a module stream shows one; message-only views keep their shape.
    from . import module_events
    module = module_events.stream(c, row['id'])
    return {'module_stream': module_events.view(c, row, module)} if module else {}



def _delivery_view(c, row):
    # Every column: before schema 49 the binding has no endpoint column (rolling upgrade).
    binding = c.execute('SELECT * FROM subscription_delivery WHERE subscription=%s', (row['id'],)).fetchone()
    counts = c.execute('''SELECT state,count(*) AS n FROM subscription_outbox WHERE subscription=%s
        GROUP BY state''', (row['id'],)).fetchall()
    # An unbound subscription reads no policy state, so its view needs no schema-38 column.
    from .delivery_policy import UNBOUND, delivery_state
    view = {'passive_read': True, 'ack': True, 'push': binding is not None,
        'receiver_id': str(binding['receiver']) if binding and binding['receiver'] else None, 'host_wakeup': 'unverified',
        'host_turn_verified': False, 'hint_counts': {item['state']: item['n'] for item in counts},
        **(delivery_state(c, row) if binding else UNBOUND)}
    if binding and binding.get('endpoint'):
        # ANYM-519: a deleted endpoint leaves the subscription pull-only; a paused one names its state.
        endpoint = c.execute('SELECT state FROM delivery_endpoints WHERE id=%s', (binding['endpoint'],)).fetchone()
        state = endpoint['state'] if endpoint else 'deleted'
        view.update(endpoint_id=str(binding['endpoint']), target='endpoint', push=state == 'enabled',
            reason=None if state == 'enabled' else 'endpoint_deleted' if state == 'deleted' else state)
        if state == 'deleted':
            view.update(target='none', endpoint_id=None)
    return view


def create_subscription(store, c, p, chat_ids, *, sender_ids=None, kinds=None,
        direction='incoming', origins=None, label='', expires_at=None, wake_rules=None, reply_to_send=None,
        delivery=None):
    from .store import APIError, date_bound
    if 'read' not in p['permissions']:
        raise APIError('forbidden', 403)
    if delivery is not None:
        # ANYM-519: bind a self-service endpoint in the same transaction (configure_subscription_delivery's checks).
        if not isinstance(delivery, dict) or 'endpoint_id' not in delivery or set(delivery) - {'endpoint_id', 'delivery_policy'}:
            raise APIError('invalid_parameters')
        from .delivery_endpoints import gate_on
        if 'delivery-write' not in p['permissions']:
            raise APIError('forbidden', 403)
        if not gate_on(c):
            raise APIError('delivery_disabled', 409)
    chats = _ids(chat_ids, required=True)
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
    count = c.execute('SELECT count(*) AS n FROM subscriptions WHERE principal=%s AND key_id=%s',
        (p['id'], p['key_id'])).fetchone()['n']
    if count >= maximum:
        raise APIError('subscription_limit', 429)
    # The counter is serialized by event publishers. An MVCC read sees only a
    # committed frontier and cannot skip an uncommitted event transaction.
    head = c.execute('SELECT value FROM feed_counter WHERE id=1').fetchone()['value']
    selector = dict(chat_ids=chats, sender_ids=senders, kinds=kinds, origins=origins, direction=direction)
    if wake_rules is not None:
        if not isinstance(wake_rules, (list, tuple)) or not 1 <= len(wake_rules) <= 4:
            raise APIError('invalid_subscription')
        rules = _ids(wake_rules, required=True)
        if set(rules) - {'incoming_message', 'owner_mention', 'reply_to_owner', 'reply_to_confirmed_send'}:
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
    return _view(c, row)


def list_subscriptions(store, c, p, *, limit=50):
    from .store import bounded
    bounded(limit)
    rows = c.execute('''SELECT * FROM subscriptions WHERE principal=%s AND key_id=%s
        ORDER BY created_at DESC,id DESC LIMIT %s FOR UPDATE''', (p['id'], p['key_id'], limit + 1)).fetchall()
    result = [_view(c, _state(c, row, p)) for row in rows[:limit]]
    targets(p, [str(row['id']) for row in rows[:limit]])
    return {'items': result, 'has_more': len(rows) > limit}


def get_subscription(store, c, p, subscription_id):
    return _view(c, _owned(c, p, subscription_id))


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
        return _view(c, row)
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
    return _view(c, row)


def get_subscription_events(store, c, p, subscription_id, *, after=0, limit=50, module_after=None):
    from .store import APIError, bounded, serial
    bounded(limit)
    if type(after) is not int or not 0 <= after <= 2**53 - 1:
        raise APIError('invalid_version')
    row = _owned(c, p, subscription_id)
    state = _view(c, row)
    if row['state'] == 'access_changed':
        return {'subscription': state, 'items': [], 'has_more': False, 'scope_masked': True}
    # Pruned references never become an empty complete page: the gap names the floor to resume from.
    floor = row.get('retention_floor', 0)
    from . import module_events
    module = module_events.stream(c, row['id'])
    if module is not None and module_after is None:
        module_after = module['start_position']
    if module_after is not None and (type(module_after) is not int or not 0 <= module_after <= 2**53 - 1):
        raise APIError('invalid_version')
    # Each stream pages, gaps and advances on its own positions (ANYM-562).
    modules = {} if module is None else {'module': module_events.page(c, row, module, module_after, limit)}
    if after < floor:
        state['gap'] = {'reason': 'retention_gap', 'after': after, 'through': floor,
            'recoverable': False, 'floor': floor}
        return serial({'subscription': state, 'items': [], 'has_more': True, 'next_after': floor,
            'consumption': 'passive_read_no_ack', 'scope_masked': False, **modules})
    events = c.execute('''SELECT e.id,e.kind,e.origin,e.revision,e.created_at,
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
        WHERE sm.subscription=%s AND sm.event>%s ORDER BY sm.event LIMIT %s''',
        (row['id'], after, limit + 1)).fetchall()
    fields = ('message', 'account', 'chat', 'sender', 'provider_id', 'revision', 'sent_at',
        'direction', 'deleted', 'processing', 'metadata', 'reply_evidence', 'send_id', 'error_code', 'status')
    items = [{**{key: event['snapshot'][key] for key in fields if key in event['snapshot']},
        'event_id': event['id'], 'kind': event['kind'], 'origin': event['origin'],
        'revision': event['revision'], 'observed_at': event['created_at'],
        'historical': event['origin'] in ('backfill', 'reconcile'),
        'matching_basis': 'exact_event_revision'} for event in events[:limit]]
    from .subscription_consumption import wake_reason
    for item in items:
        item['wake_reason'] = wake_reason(item, row['selector'])
    return serial({'subscription': state, 'items': items, 'has_more': len(events) > limit,
        'next_after': items[-1]['event_id'] if items else after,
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
        'expired': 0, 'blocked': 0, 'missing_evidence': 0, 'module_matched': 0, 'module_blocked': 0}
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
            sources = c.execute('SELECT account,chat,sender_ids FROM subscription_sources WHERE subscription=%s',
                (row['id'],)).fetchall()
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
                if event['kind'] in FAILURE_KINDS:
                    # Direction and senders describe messages; these kinds have their own audience.
                    matches = (event['kind'] in selector['kinds'] and event['origin'] in selector['origins'] and
                        _failure_audience(event, source in lookup, row, p))
                else:
                    matches = (source in lookup and event['kind'] in selector['kinds'] and
                        event['origin'] in selector['origins'])
                    if matches and selector['direction']:
                        if 'direction' not in snapshot:
                            missing += 1
                            matches = False
                        else:
                            matches = snapshot['direction'] == selector['direction']
                    if matches and lookup[source] is not None:
                        if 'sender' not in snapshot:
                            missing += 1
                            matches = False
                        else:
                            matches = snapshot['sender'] in lookup[source]
                if matches and selector.get('wake_rules'):
                    from .subscription_consumption import wake_reason
                    evidence = {**snapshot, 'account': event['account'], 'kind': event['kind'],
                        'historical': event['origin'] in ('backfill', 'reconcile')}
                    matches = bool(set(selector['wake_rules']) & set(wake_reason(evidence, selector)))
                if matches:
                    if count >= maximum:
                        reason = 'match_budget'
                        gap = {'reason': reason, 'after': position, 'through': head,
                            'recoverable': True, 'requires_operator': True}
                        break
                    inserted = c.execute('''INSERT INTO subscription_matches(subscription,event)
                        VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING event''', (row['id'], event['id'])).fetchone()
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
                        after_match(c, row, position)
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
    return event['account'] in p['accounts'] and p['chat_ids'] is None


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
                WHERE NOT s.retention_hold AND s.state NOT IN ('closed','expired')
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
                WHERE NOT s.retention_hold AND s.state NOT IN ('closed','expired')
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


def _prune_batch(c, candidate, days):
    """One transaction. Returns rows deleted, or None when retention is off or the subscription
    is busy, held or past its deadline."""
    # Set first, so it bounds the expiry read through COMMIT: a stalled batch is rolled back.
    c.execute("SELECT set_config('transaction_timeout',%s,true)", (RETENTION_TRANSACTION_TIMEOUT,))
    # Bounds well under one matcher pass; a contended subscription waits for the next pass.
    c.execute("SET LOCAL lock_timeout='100ms'")
    c.execute("SET LOCAL statement_timeout='1s'")
    # The activation is reread under the shared lock: once --disable returns, no batch deletes.
    if not lock_retention(c, shared=True) or not c.execute('''SELECT EXISTS(SELECT FROM probe_metadata
            WHERE name='subscription_retention' AND value='enabled') AS enabled''').fetchone()['enabled']:
        return None
    # The matcher's order: the shared principal lock, then the subscription row.
    lock_principal(c, candidate['principal'], shared=True)
    row = c.execute('''SELECT id,state,retention_floor,retention_hold,
        expires_at IS NOT NULL AND expires_at<=clock_timestamp()+%s::interval AS lapsed
        FROM subscriptions WHERE id=%s FOR UPDATE SKIP LOCKED''',
        (RETENTION_EXPIRY_MARGIN, candidate['id'])).fetchone()
    # A quarantine closes every definition: terminal ones keep their references (ANYM-446 F2).
    # A passed deadline is terminal before the matcher stores 'expired'.
    if row is None or row['retention_hold'] or row['lapsed'] or row['state'] in ('closed', 'expired'):
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
