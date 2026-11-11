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

MODULES = {'calls': 'deedkit:calls', 'calendar': 'deedkit:calendar'}
CALL_SCOPE = 'calls:own'
CALENDAR_ALL = 'calendar:all'
_PRINCIPAL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,255}')
NEXT_LINK = ('Ask the owner to link calls for this key: deedkit admin module-links link --principal <principal> '
    '--module calls --module-principal <calls principal>; then create the subscription again.')
NEXT_CALENDAR = ('Ask the owner to link calendar for this key: deedkit admin module-links link --principal <principal> '
    '--module calendar --module-principal <calendar principal>; the linked principal must read the calendar with '
    'details visibility; then create the subscription again.')


def ready(c):
    """Schema 90 present: before it no call source is live (fail closed) and none can be admitted."""
    return c.execute("SELECT to_regclass('public.module_links') IS NOT NULL AS ready").fetchone()['ready']


def stale(link):
    """SQL: the calls service journaled an access change of the link's calls principal after the grant."""
    return ("EXISTS(SELECT FROM module_events x WHERE x.type='call.access.changed' AND x.scope_ref=" + link +
        '.module_principal AND x.seq>' + link + '.granted_through)')


def _latest_access(principal, scope, before=None):
    """SQL scalar: the data of the newest module.access.changed event the calendar journaled for (principal, scope),
    before the module seq `before` when given."""
    return ("(SELECT x.data FROM module_events x WHERE x.type='module.access.changed' AND x.scope_ref=" + scope +
        ' AND x.subject=' + principal + (' AND x.seq<' + before if before else '') + ' ORDER BY x.seq DESC LIMIT 1)')


def _holds(data):
    """SQL: an access event's data says details and its `until` (the principal's expiry) has not passed."""
    return ("(" + data + "->>'access'='details' AND (" + data + "->>'until' IS NULL OR (" + data +
        "->>'until')::timestamptz>clock_timestamp()))")


def access_holds(c, principal, scope):
    """Whether the calendar principal's access to the calendar `scope` (`calendar:<ref>`) holds now, from the journal."""
    return c.execute('SELECT 1 FROM (SELECT %s::text AS p,%s::text AS s) q WHERE ' + _holds(_latest_access('q.p', 'q.s')),
        (principal, scope)).fetchone() is not None


def _calendar_live(subscription, source, scope, seq):
    """SQL: a calendar source (or, with `seq`, one calendar event) of a subscription is entitled (ANYM-577).

    The source is live while the principal's calendar link is active with the generation it was admitted under and,
    for a calendar named exactly, the principal's access to it holds now. `calendar:all` names no calendar: each event
    is judged on its own calendar. An event (seq) is visible only if the access in force when it happened was details,
    no access:none was journaled after it (so a revocation followed by a regrant never revives it) and the access
    holds now. Everything is read from the module journal, which the calendar service stamps: nothing here trusts a
    label of the router's configuration, and another principal's events never count (`subject` is the link's principal).
    """
    principal = 'ml.module_principal'
    entitled = _holds(_latest_access(principal, scope))
    if seq is None:
        # Source level: `calendar:all` is judged per event; an exact calendar by its access now.
        entitled = '(' + scope + "='calendar:all' OR " + entitled + ')'
        named = 'sl.scope_ref=' + scope
    else:
        entitled += (' AND ' + _latest_access(principal, scope, seq) + "->>'access'='details'"
            " AND NOT EXISTS(SELECT FROM module_events y WHERE y.type='module.access.changed' AND y.scope_ref=" + scope +
            ' AND y.subject=' + principal + ' AND y.seq>' + seq + " AND y.data->>'access'='none')")
        named = '(sl.scope_ref=' + scope + " OR sl.scope_ref='calendar:all')"
    return ('EXISTS(SELECT FROM subscription_module_links sl JOIN subscriptions su ON su.id=sl.subscription '
        "JOIN module_links ml ON ml.principal=su.principal AND ml.module='calendar' "
        'WHERE sl.subscription=' + subscription + ' AND sl.source=' + source + ' AND ' + named +
        " AND ml.state='active' AND ml.generation=sl.generation AND " + entitled + ')')


def live(subscription, source, scope, *, linked=True, seq=None):
    """SQL: the module source (source, scope) of a subscription may match, be read and be announced.

    The arguments are column expressions of the caller's query (code constants, never request values). `seq` is the
    event's module seq column when the caller judges one event (a calendar source is entitled per event, ANYM-577).
    Recordings and feedback sources are left as they are.
    """
    linked_sources = "('deedkit:calls','deedkit:calendar')"
    if not linked:
        return '(' + source + ' NOT IN ' + linked_sources + ')'
    return ('(' + source + ' NOT IN ' + linked_sources + " OR " + source + "='deedkit:calls' AND EXISTS(SELECT FROM "
        'subscription_module_links sl JOIN subscriptions su ON su.id=sl.subscription '
        "JOIN module_links ml ON ml.principal=su.principal AND ml.module='calls' "
        'WHERE sl.subscription=' + subscription + ' AND sl.source=' + source + ' AND sl.scope_ref=' + scope +
        " AND ml.state='active' AND ml.generation=sl.generation AND ml.module_principal=sl.scope_ref AND NOT " +
        stale('ml') + ') OR ' + source + "='deedkit:calendar' AND " + _calendar_live(subscription, source, scope, seq)
        + ')')


def live_sql(c, subscription, source, scope, seq=None):
    return live(subscription, source, scope, linked=ready(c), seq=seq)


def event_live(c, subscription_id, event):
    """Whether one module event (a journal row with source, scope_ref and seq) may reach the subscription now."""
    return c.execute('SELECT 1 FROM (SELECT %s::uuid AS sub,%s::text AS source,%s::text AS scope,%s::bigint AS seq) q '
        'WHERE ' + live_sql(c, 'q.sub', 'q.source', 'q.scope', 'q.seq'),
        (subscription_id, event['source'], event['scope_ref'], event['seq'])).fetchone() is not None


def fence_use(c):
    """Take the access fence shared: held to the end of the use's transaction, it makes a calendar access change
    (publish takes the same lock exclusively) wait for this use, and a later use read the change (ANYM-577)."""
    from .module_events import ACCESS_FENCE
    c.execute('SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))', (ACCESS_FENCE,))


def not_linked(module='calls'):
    from .store import APIError
    error = APIError('module_not_linked', 409)
    next_step = NEXT_CALENDAR if module == 'calendar' else NEXT_LINK
    setattr(error, 'next', next_step)  # error_details lists `next` (AGENT_API.md rulebook)
    # The gateway relays a `next` only when it is a call it can run; this prose step reaches the agent as the same
    # reviewed hint (dispatch_shaping.REVIEWED_HINTS holds this exact text, ANYM-566).
    setattr(error, 'hint', next_step)
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
            link, calendar = active_link(c, p['id']), calendar_link(c, p['id'])
    except psycopg.Error:
        return None
    shape = {'create': 'create_subscription', 'argument': 'module_sources', 'consume': 'fetch_subscription_events',
        'refused_without_link': 'module_not_linked'}
    block = {}
    if link is not None:
        block['calls'] = {'source': MODULES['calls'], 'scope': CALL_SCOPE, 'kinds': call_kinds(), **shape}
    if calendar is not None:  # ANYM-577: the calendar kinds, `calendar:all` or one `calendar:<calendar_ref>`
        block['calendar'] = {'source': MODULES['calendar'], 'scope': CALENDAR_ALL + ' | calendar:<calendar_ref>',
            'kinds': calendar_kinds(), **shape}
    return block or None


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


def calendar_kinds():
    """The calendar types a subscription may ask for (never the access control events)."""
    from .module_events import KINDS
    return sorted(kind for kind in KINDS if kind.startswith('calendar.'))


def calendar_link(c, principal):
    """The principal's active calendar link, or None (before schema 93 too)."""
    if not ready(c):
        return None
    return c.execute("SELECT ml.module_principal,ml.generation FROM module_links ml WHERE ml.principal=%s "
        "AND ml.module='calendar' AND ml.state='active'", (principal,)).fetchone()


def calendar_sources(c, p, module_sources):
    """Validate an API request's calendar sources (ANYM-577): `calendar:all` or `calendar:<calendar_ref>` with
    calendar types, only under the principal's active calendar link, and an exact calendar only while the linked
    calendar principal's access to it holds now (read from the journal the calendar service stamps).

    Returns [(source, scope_ref, types)]; the scope is the request's calendar, checked here, never a router label.
    """
    from .store import APIError
    from .module_events import _REF
    kinds = calendar_kinds()
    seen = set()
    for item in module_sources:
        scope = item.get('scope') if isinstance(item, dict) else None
        types = item.get('types') if isinstance(item, dict) else None
        if (set(item) - {'scope', 'types'} or not isinstance(scope, str) or scope in seen or
                scope != CALENDAR_ALL and (not scope.startswith('calendar:') or
                    _REF.fullmatch(scope[len('calendar:'):]) is None) or
                not isinstance(types, list) or not 1 <= len(types) <= 16 or len(set(types)) != len(types) or
                any(not isinstance(kind, str) or kind not in kinds for kind in types)):
            raise APIError('invalid_filter')
        seen.add(scope)
    link = calendar_link(c, p['id'])
    if link is None:
        raise not_linked('calendar')
    result = []
    for item in module_sources:
        scope = item['scope']
        if scope != CALENDAR_ALL and not access_holds(c, link['module_principal'], scope):
            raise not_linked('calendar')
        result.append((MODULES['calendar'], scope, sorted(item['types'])))
    return result


def api_sources(c, p, module_sources):
    """The sources of create_subscription's `module_sources`: one `calls:own` item, or 1-16 calendar items."""
    from .store import APIError
    if (isinstance(module_sources, list) and module_sources and all(isinstance(item, dict) for item in module_sources)
            and any(str(item.get('scope', '')).startswith('calendar:') for item in module_sources)):
        if len(module_sources) > 16:
            raise APIError('invalid_filter')
        return calendar_sources(c, p, module_sources)
    return call_sources(c, p, module_sources)


def admit(c, subscription_id, source, scope_ref, types):
    """Give the subscription a module source under its principal's live link and record the link's generation.

    The caller holds the principal's shared ACL lock (the request's or the operator's), which a revocation's
    exclusive lock waits for, so the link read here is the one in force when the source commits.
    """
    if not ready(c):
        raise not_linked()
    if source == MODULES['calendar']:
        return _admit_calendar(c, subscription_id, scope_ref, types)
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


def _admit_calendar(c, subscription_id, scope_ref, types):
    """A calendar source under the subscription principal's active calendar link, with the access fence held shared
    so an access change of the calendar principal is ordered against this admission."""
    fence_use(c)
    link = c.execute("SELECT ml.generation,ml.module_principal FROM subscriptions s JOIN module_links ml "
        "ON ml.principal=s.principal AND ml.module='calendar' WHERE s.id=%s AND ml.state='active'",
        (subscription_id,)).fetchone()
    if link is None:
        raise not_linked('calendar')
    if scope_ref != CALENDAR_ALL and not access_holds(c, link['module_principal'], scope_ref):
        raise not_linked('calendar')
    c.execute('''INSERT INTO subscription_module_sources(subscription,source,scope_ref,types) VALUES(%s,%s,%s,%s)
        ON CONFLICT(subscription,source,scope_ref) DO UPDATE SET types=excluded.types''',
        (subscription_id, MODULES['calendar'], scope_ref, list(types)))
    c.execute('''INSERT INTO subscription_module_links(subscription,source,scope_ref,generation) VALUES(%s,%s,%s,%s)
        ON CONFLICT DO NOTHING''', (subscription_id, MODULES['calendar'], scope_ref, link['generation']))
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
