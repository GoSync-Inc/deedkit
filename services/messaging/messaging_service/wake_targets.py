"""Agent self-registered wake targets (ANYM-848): which of a key's own subscriptions wake which of its local chats.

DeedKit stores and returns the rules; a host waker on the owner's machine (packages/wake-relay, ANYM-849) reads
them with the agent's own key and wakes the chat. DeedKit never contacts a target, stages no hint for one and
leaves the subscription's push delivery (subscription_delivery) alone. A target wakes its chat only once that chat
returned the waker's one-use challenge code through confirm_wake_target; DeedKit only stores and returns that code.
"""
from __future__ import annotations

import hashlib
import json
import re
from uuid import UUID

TOOLS = ('register_wake_target', 'list_wake_targets', 'update_wake_target', 'delete_wake_target',
         'confirm_wake_target')
KINDS = ('codex_thread',)
ACTIONS = ('pause', 'resume', 'set_thread', 'set_subscriptions')
# packages/wake-relay/src/codex.mjs validThread: a Codex session UUID or an exact session name, never an option.
_THREAD_UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
_THREAD_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}')
_CODE = re.compile(r'[0-9a-f]{32}')
_REQUEST = re.compile(r'[a-zA-Z0-9_.:-]{8,128}')
MAX_LABEL = 80
MAX_SUBSCRIPTIONS = 16
DEFAULT_BUDGET = 12
MAX_BUDGET = 60
LIST_LIMIT = 32
COLUMNS = ('id,principal,key_id,request_id,request_hash,kind,thread,label,hourly_budget,state,disabled_reason,'
           'confirmation,version,created_at,updated_at')
OPERATOR_STEP = {'operator': 'ask the owner to run: deedkit admin wake-targets enable <wake_target_id>',
                 'restore': 'ask the owner to run: deedkit admin wake-targets enable --all (after a backup restore)'}


def _error(code, status, **extra):
    from .delivery_endpoints import _error as error
    return error(code, status, **extra)


def _invalid(errors):
    from .field_errors import refusal
    failure = refusal('invalid_wake_target', errors)
    failure.status = 400
    return failure


def schema_ready(c):
    """Migration 073 is applied and this role may read the tables; before it (rolling upgrade) every path is off."""
    return c.execute("""SELECT to_regclass('public.wake_target_subscriptions') IS NOT NULL
        AND has_table_privilege('public.wake_target_subscriptions','SELECT') AS ready""").fetchone()['ready']


def max_targets():
    from .subscriptions import _bound_setting
    return _bound_setting('WAKE_MAX_TARGETS_PER_KEY', 8, 1, 32)


def _allowed(c, p):
    # The same permission pair and operator switch as self-service endpoints (delivery_endpoints.py:374-378).
    from .delivery_endpoints import _allowed as allowed
    allowed(c, p)


def _lock_key(c, p):
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('wake-targets:' + str(p['key_id']),))


def valid_thread(value):
    return isinstance(value, str) and bool(_THREAD_UUID.fullmatch(value) or _THREAD_NAME.fullmatch(value))


def _target_id(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise _error('not_found', 404) from None


def _version(value):
    if type(value) is not int or not 1 <= value <= 2**53 - 1:
        raise _error('invalid_version', 400)
    return value


def _subscription_ids(value, errors, field='subscription_ids'):
    if not isinstance(value, list):
        errors.append((field, 'type', 'array of 1-16 subscription UUIDs'))
        return []
    if not 1 <= len(value) <= MAX_SUBSCRIPTIONS:
        errors.append((field, 'min_items' if not value else 'max_items', 'array of 1-16 subscription UUIDs'))
        return []
    ids = []
    for index, item in enumerate(value):
        try:
            if not isinstance(item, str):
                raise ValueError
            ids.append(str(UUID(item)))
        except (ValueError, AttributeError, TypeError):
            errors.append((f'{field}/{index}', 'format', 'subscription UUID'))
            return []
    if len(set(ids)) != len(ids):
        errors.append((field, 'unique', 'each subscription once'))
        return []
    return ids


def _thread(value, errors):
    if not valid_thread(value):
        errors.append(('thread', 'pattern', 'a Codex session UUID or a 1-64 character session name; '
                       'a Codex agent passes its CODEX_THREAD_ID'))
    return value


def _label(value, errors):
    if not isinstance(value, str) or len(value) > MAX_LABEL or (value and not value.isprintable()):
        errors.append(('label', 'max_length', '0-80 printable characters'))
    return value


def _budget(value, errors):
    if type(value) is not int or not 1 <= value <= MAX_BUDGET:
        errors.append(('hourly_budget', 'maximum', 'integer from 1 to 60'))
    return value


def _bindings(c, target_ids):
    """Each target's bindings to subscriptions that are still active (closed, expired or paused ones drop out)."""
    rows = c.execute('''SELECT b.wake_target,b.subscription FROM wake_target_subscriptions b
        JOIN subscriptions s ON s.id=b.subscription AND s.state='active' AND (s.expires_at IS NULL OR s.expires_at>clock_timestamp())
        WHERE b.wake_target=ANY(%s::uuid[]) ORDER BY b.subscription''', ([str(i) for i in target_ids],)).fetchall()
    bound = {}
    for row in rows:
        bound.setdefault(str(row['wake_target']), []).append(str(row['subscription']))
    return bound


def _view(row, subscriptions, **extra):
    from .store import serial
    return serial({**extra, 'wake_target_id': row['id'], 'version': row['version'], 'kind': row['kind'], 'thread': row['thread'],
        'label': row['label'], 'hourly_budget': row['hourly_budget'], 'state': row['state'],
        'disabled_reason': row['disabled_reason'],
        **({} if 'confirmed' in extra else {'confirmation': row['confirmation']}),
        'subscription_ids': subscriptions, 'bound': len(subscriptions),
        'created_at': row['created_at'], 'updated_at': row['updated_at']})


def _one(c, row):
    return _view(row, _bindings(c, [row['id']]).get(str(row['id']), []))


def _owned(c, p, target_id):
    row = c.execute('SELECT ' + COLUMNS + ''' FROM wake_targets WHERE id=%s AND principal=%s AND key_id=%s
        AND state<>'deleted' FOR UPDATE''', (_target_id(target_id), p['id'], p['key_id'])).fetchone()
    if row is None:
        raise _error('not_found', 404)
    from .policy import targets
    targets(p, [str(row['id'])])
    return row


def _state_error(row):
    extra = {'state': row['state']}
    if row['state'] == 'operator_disabled':
        extra.update(disabled_reason=row['disabled_reason'], next_step=OPERATOR_STEP[row['disabled_reason'] or 'operator'])
    return _error('invalid_wake_target_state', 409, **extra)


def _bind(c, p, target_id, ids, rebind):
    """Bind the key's own active subscriptions; the caller holds the key lock. A subscription belongs to one key,
    so a binding found here is always one of this key's targets."""
    owned = {str(row['id']) for row in c.execute('''SELECT id FROM subscriptions WHERE id=ANY(%s::uuid[])
        AND principal=%s AND key_id=%s AND state='active' AND (expires_at IS NULL OR expires_at>clock_timestamp())''',
        (ids, p['id'], p['key_id'])).fetchall()}
    if len(owned) != len(ids):
        # The same answer for another key's subscription, bound or not, and for an unknown, closed or expired one; the
        # field error names the list, never which entry.
        raise _field_refusal('not_found', 404, 'format', "this key's own active subscription UUIDs")
    held = c.execute('''SELECT b.subscription,t.id,t.version FROM wake_target_subscriptions b
        JOIN wake_targets t ON t.id=b.wake_target WHERE b.subscription=ANY(%s::uuid[]) AND b.wake_target<>%s
        AND t.principal=%s AND t.key_id=%s ORDER BY b.subscription''', (ids, target_id, p['id'], p['key_id'])).fetchall()
    if held and not rebind:
        first = held[0]
        raise _field_refusal('subscription_already_bound', 409, 'unique', 'subscriptions no other target of yours holds',
            wake_target={'wake_target_id': str(first['id']),
            'version': first['version'], 'subscription_id': str(first['subscription'])},
            next_step='repeat with rebind true to move it here, or update the other target first')
    for holder in {str(row['id']) for row in held}:
        c.execute('UPDATE wake_targets SET version=version+1,updated_at=clock_timestamp() WHERE id=%s', (holder,))
    c.execute('DELETE FROM wake_target_subscriptions WHERE subscription=ANY(%s::uuid[]) OR wake_target=%s',
        (ids, target_id))
    c.execute('''INSERT INTO wake_target_subscriptions(wake_target,subscription)
        SELECT %s,unnest(%s::uuid[])''', (target_id, ids))


def _field_refusal(code, status, reason, expected, **extra):
    from .field_errors import field_error
    failure = _error(code, status, **extra)
    failure.field_errors = [field_error('subscription_ids', reason, expected)]
    return failure


def _limit_error(c, p):
    rows = c.execute('SELECT ' + COLUMNS + ''' FROM wake_targets WHERE principal=%s AND key_id=%s AND state<>'deleted'
        ORDER BY created_at DESC,id DESC LIMIT %s''', (p['id'], p['key_id'], LIST_LIMIT)).fetchall()
    bound = _bindings(c, [row['id'] for row in rows])
    listed = [{'wake_target_id': str(row['id']), 'label': row['label'], 'thread': row['thread'], 'state': row['state'],
               'version': row['version'], 'bound': len(bound.get(str(row['id']), []))} for row in rows]
    return _error('wake_target_limit', 429, wake_targets=listed, max=max_targets(),
        next_step='delete a target you no longer need with delete_wake_target, then register again')


def register_wake_target(store, c, p, request_id, kind, thread, subscription_ids, label='', hourly_budget=None,
        rebind=False):
    _allowed(c, p)
    if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
        raise _error('invalid_request_id', 400)
    errors = []
    if kind not in KINDS:
        errors.append(('kind', 'enum', None, KINDS))
    _thread(thread, errors)
    ids = _subscription_ids(subscription_ids, errors)
    _label(label, errors)
    budget = DEFAULT_BUDGET if hourly_budget is None else hourly_budget
    _budget(budget, errors)
    if type(rebind) is not bool:
        errors.append(('rebind', 'type', 'boolean'))
    if errors:
        raise _invalid(errors)
    request_hash = hashlib.sha256(json.dumps({'kind': kind, 'thread': thread, 'subscription_ids': sorted(ids),
        'label': label, 'hourly_budget': budget, 'rebind': rebind}, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()
    _lock_key(c, p)
    existing = c.execute('SELECT ' + COLUMNS + ' FROM wake_targets WHERE key_id=%s AND request_id=%s',
        (p['key_id'], request_id)).fetchone()
    from .policy import targets
    if existing:
        if existing['request_hash'] != request_hash or existing['principal'] != p['id']:
            raise _error('idempotency_conflict', 409)
        # A replay returns the row as it is now, also deleted or operator-disabled; it never re-enables it.
        targets(p, [str(existing['id'])])
        return _one(c, existing)
    count = c.execute('''SELECT count(*) AS n FROM wake_targets WHERE principal=%s AND key_id=%s
        AND state<>'deleted' ''', (p['id'], p['key_id'])).fetchone()['n']
    if count >= max_targets():
        raise _limit_error(c, p)
    row = c.execute('''INSERT INTO wake_targets(id,principal,key_id,request_id,request_hash,kind,thread,label,hourly_budget,
        created_at,updated_at) VALUES(gen_random_uuid(),%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp(),clock_timestamp())
        RETURNING ''' + COLUMNS,
        (p['id'], p['key_id'], request_id, request_hash, kind, thread, label, budget)).fetchone()
    _bind(c, p, row['id'], ids, rebind)
    targets(p, [str(row['id'])])
    return _one(c, row)


def list_wake_targets(store, c, p):
    _allowed(c, p)
    rows = c.execute('SELECT ' + COLUMNS + ''' FROM wake_targets WHERE principal=%s AND key_id=%s AND state<>'deleted'
        ORDER BY created_at DESC,id DESC LIMIT %s''', (p['id'], p['key_id'], LIST_LIMIT)).fetchall()
    bound = _bindings(c, [row['id'] for row in rows])
    from .policy import targets
    targets(p, [str(row['id']) for row in rows])
    return {'items': [_view(row, bound.get(str(row['id']), [])) for row in rows], 'has_more': False,
            'max_wake_targets': max_targets()}


def update_wake_target(store, c, p, wake_target_id, version, action, thread=None, subscription_ids=None, rebind=False):
    _allowed(c, p)
    version = _version(version)
    errors = []
    if action not in ACTIONS:
        errors.append(('action', 'enum', None, ACTIONS))
    if action == 'set_thread':
        _thread(thread, errors)
    ids = _subscription_ids(subscription_ids, errors) if action == 'set_subscriptions' else []
    if type(rebind) is not bool:
        errors.append(('rebind', 'type', 'boolean'))
    if errors:
        raise _invalid(errors)
    _lock_key(c, p)
    row = _owned(c, p, wake_target_id)
    if row['version'] != version:
        raise _error('wake_target_conflict', 409, version=row['version'])
    if row['state'] == 'operator_disabled':
        raise _state_error(row)
    if action == 'pause':
        if row['state'] != 'enabled':
            raise _state_error(row)
        changes, values = ",state='agent_paused'", []
    elif action == 'resume':
        if row['state'] != 'agent_paused':
            raise _state_error(row)
        changes, values = ",state='enabled'", []
    elif action == 'set_thread':
        # A new thread needs a new confirmation from the chat that receives the new challenge.
        changes, values = ',thread=%s,confirmation=NULL', [thread]
    else:
        _bind(c, p, row['id'], ids, rebind)
        changes, values = '', []
    row = c.execute('UPDATE wake_targets SET version=version+1,updated_at=clock_timestamp()' + changes +
        ' WHERE id=%s RETURNING ' + COLUMNS, (*values, row['id'])).fetchone()
    return _one(c, row)


def delete_wake_target(store, c, p, wake_target_id, version):
    _allowed(c, p)
    version = _version(version)
    _lock_key(c, p)
    row = _owned(c, p, wake_target_id)
    if row['version'] != version:
        raise _error('wake_target_conflict', 409, version=row['version'])
    c.execute('''UPDATE wake_targets SET state='deleted',disabled_reason=NULL,confirmation=NULL,version=version+1,
        updated_at=clock_timestamp() WHERE id=%s''', (row['id'],))
    unbound = c.execute('DELETE FROM wake_target_subscriptions WHERE wake_target=%s', (row['id'],)).rowcount
    return {'wake_target_id': str(row['id']), 'deleted': True, 'unbound_subscriptions': unbound}


def confirm_wake_target(store, c, p, wake_target_id, code):
    _allowed(c, p)
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise _invalid([('code', 'pattern', 'the 32 lowercase hex characters of the challenge')])
    _lock_key(c, p)
    row = _owned(c, p, wake_target_id)
    if row['state'] == 'operator_disabled':
        raise _state_error(row)
    if row['confirmation'] != code:
        # The latest code replaces an earlier one, so a wrong or pre-filled code never blocks the right one.
        row = c.execute('''UPDATE wake_targets SET confirmation=%s,version=version+1,updated_at=clock_timestamp()
            WHERE id=%s RETURNING ''' + COLUMNS, (code, row['id'])).fetchone()
    return _one(c, row)


def subscription_wake_target(c, subscription_id):
    """The non-deleted target a subscription is bound to, or None; None also before the schema or without a grant."""
    if not schema_ready(c):
        return None
    row = c.execute('''SELECT t.id FROM wake_target_subscriptions b JOIN wake_targets t ON t.id=b.wake_target
        WHERE b.subscription=%s AND t.state<>'deleted' ''', (subscription_id,)).fetchone()
    return str(row['id']) if row else None


# Operator side (python -m messaging_service / deedkit admin).

def list_admin(store, principal=None):
    with store.pool.connection() as c:
        rows = c.execute('SELECT ' + COLUMNS + ''' FROM wake_targets WHERE state<>'deleted'
            AND (%s::text IS NULL OR principal=%s) ORDER BY principal,created_at,id''', (principal, principal)).fetchall()
        bound = _bindings(c, [row['id'] for row in rows])
        items, by_state, unbound = [], {}, 0
        for row in rows:
            subscriptions = bound.get(str(row['id']), [])
            # The operator sees whether a code was returned, never the code.
            items.append(_view(row, subscriptions, principal=row['principal'], key_id=str(row['key_id']),
                               confirmed=row['confirmation'] is not None))
            by_state[row['state']] = by_state.get(row['state'], 0) + 1
            unbound += not subscriptions
        from .delivery_endpoints import gate_on
        return {'items': items, 'targets_by_state': dict(sorted(by_state.items())), 'unbound': unbound,
                'delivery_features': 'enabled' if gate_on(c, fence=False) else 'disabled'}


def set_operator_state(store, wake_target_id, enabled):
    """Operator only: operator_disabled is never set or cleared by an agent tool."""
    wake_target_id = str(UUID(wake_target_id))
    with store.pool.connection() as c:
        row = c.execute("SELECT id,state FROM wake_targets WHERE id=%s AND state<>'deleted' FOR UPDATE",
            (wake_target_id,)).fetchone()
        if row is None:
            raise ValueError('invalid_wake_target')
        if enabled and row['state'] != 'operator_disabled':
            raise ValueError('invalid_wake_target_state')
        state = 'enabled' if enabled else 'operator_disabled'
        if row['state'] != state:
            c.execute('''UPDATE wake_targets SET state=%s,disabled_reason=%s,version=version+1,updated_at=clock_timestamp()
                WHERE id=%s''', (state, None if enabled else 'operator', wake_target_id))
    return {'wake_target_id': wake_target_id, 'state': state}


def enable_all(store, principal=None):
    """Operator only: every operator-disabled target (for example after a restore) back to enabled."""
    with store.pool.connection() as c:
        rows = c.execute('''UPDATE wake_targets SET state='enabled',disabled_reason=NULL,version=version+1,
            updated_at=clock_timestamp() WHERE state='operator_disabled' AND (%s::text IS NULL OR principal=%s)
            RETURNING id''', (principal, principal)).fetchall()
    return {'enabled': len(rows)}
