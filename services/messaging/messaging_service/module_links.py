"""Module links (ANYM-564): a key sees call events only through a link the operator set for its principal.

`module_links(principal, module, module_principal, generation, state, granted_through)` names, per principal, the
calls principal whose calls may wake its keys (`calls:own`: calls whose `owner_principal`, stamped by the calls
service as the event's `scope_ref`, is the linked one). A subscription's call source records the generation it was
admitted under (`subscription_module_links`); it is live only while the link is active with that generation, names
the source's scope and is not stale. Stale is computed, never written: a `call.access.changed` event for the calls
principal after `granted_through` (the module journal head at the grant). A revocation or regrant moves the
generation, so the source, its matches, fetch pages and queued hints stay refused; a regrant needs a new
subscription. Only the owner CLI writes links (`link-module`, `unlink-module`); the API never does, and nothing here
makes a remote call: the check is a local read under the ACL fence the request or delivery already holds.
"""
from __future__ import annotations

import re

MODULES = {'calls': 'deedkit:calls'}
CALL_SCOPE = 'calls:own'
_PRINCIPAL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,255}')
NEXT_LINK = ('Ask the owner to link calls for this key: deedkit admin module-links link --principal <principal> '
    '--module calls --module-principal <calls principal>; then create the subscription again.')


def ready(c):
    """Schema 90 present: before it no call source is live (fail closed) and none can be admitted."""
    return c.execute("SELECT to_regclass('public.module_links') IS NOT NULL AS ready").fetchone()['ready']


def stale(link):
    """SQL: the calls service journaled an access change of the link's calls principal after the grant."""
    return ("EXISTS(SELECT FROM module_events x WHERE x.type='call.access.changed' AND x.scope_ref=" + link +
        '.module_principal AND x.seq>' + link + '.granted_through)')


def live(subscription, source, scope, *, linked=True):
    """SQL: the module source (source, scope) of a subscription may match, be read and be announced.

    The arguments are column expressions of the caller's query (code constants, never request values). A
    non-calls source is not admitted by any API (ANYM-577 brings calendar scopes) and is left as it is.
    """
    if not linked:
        return '(' + source + "<>'deedkit:calls')"
    return ('(' + source + "<>'deedkit:calls' OR EXISTS(SELECT FROM subscription_module_links sl "
        'JOIN subscriptions su ON su.id=sl.subscription '
        "JOIN module_links ml ON ml.principal=su.principal AND ml.module='calls' "
        'WHERE sl.subscription=' + subscription + ' AND sl.source=' + source + ' AND sl.scope_ref=' + scope +
        " AND ml.state='active' AND ml.generation=sl.generation AND ml.module_principal=sl.scope_ref AND NOT " +
        stale('ml') + '))')


def live_sql(c, subscription, source, scope):
    return live(subscription, source, scope, linked=ready(c))


def not_linked():
    from .store import APIError
    error = APIError('module_not_linked', 409)
    setattr(error, 'next', NEXT_LINK)  # error_details lists `next` (AGENT_API.md rulebook)
    # The gateway relays a `next` only when it is a call it can run; this prose step reaches the agent as the same
    # reviewed hint (dispatch_shaping.REVIEWED_HINTS holds this exact text, ANYM-566).
    setattr(error, 'hint', NEXT_LINK)
    return error


def call_kinds():
    """The call types a subscription may ask for: what `call_sources` admits and `capabilities` advertises."""
    from .module_events import KINDS
    return sorted(kind for kind in KINDS if kind.startswith('call.'))


def advertised(c, p):
    """ANYM-566: the `subscriptions.module_events` block of a key's capabilities, or None.

    Only a principal with a live calls link is told that calls kinds exist, and then exactly the kinds
    `call_sources` admits. A failed read advertises nothing (a savepoint keeps the request's transaction usable):
    capabilities never fail on a module read.
    """
    import psycopg
    try:
        with c.transaction():
            link = active_link(c, p['id'])
    except psycopg.Error:
        return None
    if link is None:
        return None
    return {'calls': {'source': MODULES['calls'], 'scope': CALL_SCOPE, 'kinds': call_kinds(),
        'create': 'create_subscription', 'argument': 'module_sources', 'consume': 'fetch_subscription_events',
        'refused_without_link': 'module_not_linked'}}


def call_sources(c, p, module_sources):
    """Validate an API request's module sources: only `calls:own`, with call types.

    Returns [(source, scope_ref, types)]; the scope is the principal's link, never the request's or router
    configuration's. Refuses with module_not_linked when the principal has no live calls link.
    """
    from .store import APIError
    if not isinstance(module_sources, list) or len(module_sources) != 1:
        raise APIError('invalid_filter')
    item = module_sources[0]
    if not isinstance(item, dict) or set(item) - {'scope', 'types'} or item.get('scope') != CALL_SCOPE:
        raise APIError('invalid_filter')
    types = item.get('types')
    kinds = call_kinds()
    if (not isinstance(types, list) or not 1 <= len(types) <= 16 or
            any(not isinstance(kind, str) or kind not in kinds for kind in types) or len(set(types)) != len(types)):
        raise APIError('invalid_filter')
    link = active_link(c, p['id'])
    if link is None:
        raise not_linked()
    return [(MODULES['calls'], link['module_principal'], sorted(types))]


def active_link(c, principal):
    """The principal's live calls link, or None (before schema 90 too)."""
    if not ready(c):
        return None
    return c.execute('''SELECT ml.module_principal,ml.generation FROM module_links ml WHERE ml.principal=%s
        AND ml.module='calls' AND ml.state='active' AND NOT ''' + stale('ml'), (principal,)).fetchone()


def admit(c, subscription_id, source, scope_ref, types):
    """Give the subscription a call source under its principal's live link and record the link's generation.

    The caller holds the principal's shared ACL lock (the request's or the operator's), which a revocation's
    exclusive lock waits for, so the link read here is the one in force when the source commits.
    """
    if not ready(c):
        raise not_linked()
    link = c.execute('''SELECT ml.generation FROM subscriptions s JOIN module_links ml ON ml.principal=s.principal
        AND ml.module='calls' WHERE s.id=%s AND %s=%s AND ml.state='active' AND ml.module_principal=%s AND NOT ''' +
        stale('ml'), (subscription_id, source, MODULES['calls'], scope_ref)).fetchone()
    if link is None:
        raise not_linked()
    c.execute('''INSERT INTO subscription_module_sources(subscription,source,scope_ref,types) VALUES(%s,%s,%s,%s)
        ON CONFLICT DO NOTHING''', (subscription_id, source, scope_ref, list(types)))
    c.execute('''INSERT INTO subscription_module_links(subscription,source,scope_ref,generation) VALUES(%s,%s,%s,%s)
        ON CONFLICT DO NOTHING''', (subscription_id, source, scope_ref, link['generation']))
    return link['generation']


def source_states(c, subscription_id):
    """{(source, scope_ref): 'active' | 'access_changed'} of a subscription's module sources."""
    rows = c.execute('SELECT s.source,s.scope_ref,' + live_sql(c, 's.subscription', 's.source', 's.scope_ref') +
        ' AS live FROM subscription_module_sources s WHERE s.subscription=%s', (subscription_id,)).fetchall()
    return {(row['source'], row['scope_ref']): 'active' if row['live'] else 'access_changed' for row in rows}


# --- Owner CLI (the admin tool's `module-links link|unlink` run these) ---------------------------------------------

def _check(principal, module, module_principal=None):
    from .store import principal_id
    principal_id(principal)
    if module not in MODULES:
        raise ValueError('invalid_module')
    if module_principal is not None and (not isinstance(module_principal, str) or
            _PRINCIPAL.fullmatch(module_principal) is None):
        raise ValueError('invalid_module_principal')


_ROW = 'SELECT ml.*,ml.state=%s AND ' + stale('ml') + ' AS stale FROM module_links ml WHERE ml.principal=%s'


def _row(c, principal, module):
    return c.execute(_ROW + ' AND ml.module=%s', ('active', principal, module)).fetchone()


def _view(row, **extra):
    from .store import serial
    return serial({'principal': row['principal'], 'module': row['module'], 'module_principal': row['module_principal'],
        'generation': row['generation'], 'state': 'stale' if row['stale'] else row['state'],
        'granted_through': row['granted_through'], 'updated_at': row['updated_at'], **extra})


def _write(c, principal, module, module_principal, state):
    """Upsert the link with a new generation (never below the previous plus one, from the clock, so a regrant after
    a restore passes every earlier generation) and expire the principal's open receipts with a module range, so no
    receipt issued before the change ACKs past it."""
    c.execute('''INSERT INTO module_links AS l(principal,module,module_principal,generation,state,granted_through)
        VALUES(%s,%s,%s,(extract(epoch FROM clock_timestamp())*1000000)::bigint,%s,
            (SELECT value FROM module_event_counter WHERE id=1))
        ON CONFLICT(principal,module) DO UPDATE SET module_principal=excluded.module_principal,
            generation=greatest(l.generation+1,excluded.generation),state=excluded.state,
            granted_through=excluded.granted_through,updated_at=clock_timestamp()''',
        (principal, module, module_principal, state))
    c.execute('''UPDATE subscription_receipts r SET expires_at=clock_timestamp() FROM subscriptions s
        WHERE r.subscription=s.id AND s.principal=%s AND r.ack_result IS NULL
        AND r.module_through_position IS NOT NULL AND r.expires_at>clock_timestamp()''', (principal,))
    return _row(c, principal, module)


def link(c, principal, module, module_principal):
    """Grant or regrant a principal's link; an unchanged live link is returned as it is (no new generation)."""
    from .policy import lock_principal
    _check(principal, module, module_principal)
    lock_principal(c, principal)
    if c.execute('SELECT 1 FROM principals WHERE id=%s', (principal,)).fetchone() is None:
        raise ValueError('unknown_principal')
    old = _row(c, principal, module)
    if old and old['state'] == 'active' and not old['stale'] and old['module_principal'] == module_principal:
        return _view(old, changed=False)
    return _view(_write(c, principal, module, module_principal, 'active'), changed=True)


def unlink(c, principal, module):
    """Revoke at once: the generation moves, so every source admitted under the link stays refused."""
    from .policy import lock_principal
    _check(principal, module)
    lock_principal(c, principal)
    old = _row(c, principal, module)
    if old is None:
        raise ValueError('module_link_not_found')
    if old['state'] == 'revoked':
        return _view(old, changed=False)
    return _view(_write(c, principal, module, old['module_principal'], 'revoked'), changed=True)


def links(c, principal):
    if not ready(c):
        return []
    return [_view(row) for row in c.execute(_ROW + ' ORDER BY ml.module', ('active', principal))]
