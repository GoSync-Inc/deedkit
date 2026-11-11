"""Module links (ANYM-564): a key sees call events only through a link the operator set for its principal.

`module_links(principal, module, module_principal, generation, state)` names, per principal, the calls principal
whose calls may wake its keys (`calls:own`: calls whose `owner_principal`, stamped by the calls service as the
event's `scope_ref`, is the linked one). A subscription's call source records the generation it was admitted under
(`subscription_module_links`); it is live only while the link is active with that generation and names the source's
scope. Revoking, a calls-side `call.access.changed` event (stale) or a regrant moves the generation, so the source,
its matches, fetch pages and queued hints stay refused; a regrant needs a new subscription. Only the owner CLI
writes links (`link-module`, `unlink-module`) through `set_module_link`; the API never does, and nothing here makes a
remote call: the check is a local read under the ACL fence the request or delivery already holds.
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
        " AND ml.state='active' AND ml.generation=sl.generation AND ml.module_principal=sl.scope_ref))")


def live_sql(c, subscription, source, scope):
    return live(subscription, source, scope, linked=ready(c))


def not_linked():
    from .store import APIError
    error = APIError('module_not_linked', 409)
    setattr(error, 'next', NEXT_LINK)  # error_details lists `next` (AGENT_API.md rulebook)
    return error


def call_sources(c, p, module_sources):
    """Validate an API request's module sources: only `calls:own`, with the key's call types.

    Returns [(source, None, types)]; the scope comes from the principal's link at admission, never from the
    request or router configuration. Refuses with module_not_linked when the principal has no active calls link.
    """
    from .module_events import KINDS
    from .store import APIError
    if not isinstance(module_sources, list) or not 1 <= len(module_sources) <= 1:
        raise APIError('invalid_filter')
    item = module_sources[0]
    if not isinstance(item, dict) or set(item) - {'scope', 'types'} or item.get('scope') != CALL_SCOPE:
        raise APIError('invalid_filter')
    types = item.get('types')
    if (not isinstance(types, list) or not 1 <= len(types) <= 16 or len(set(map(str, types))) != len(types) or
            any(not isinstance(kind, str) or kind not in KINDS or not kind.startswith('call.') for kind in types)):
        raise APIError('invalid_filter')
    link = active_link(c, p['id'])
    if link is None:
        raise not_linked()
    return [(MODULES['calls'], link['module_principal'], sorted(types))]


def active_link(c, principal):
    if not ready(c):
        return None
    return c.execute('''SELECT module_principal,generation FROM module_links WHERE principal=%s AND module='calls'
        AND state='active' ''', (principal,)).fetchone()


def admit(c, subscription_id, source, scope_ref, types):
    """Give the subscription a call source under the link's current generation (definer function, link checked)."""
    from psycopg import errors
    try:
        with c.transaction():
            return c.execute('SELECT public.admit_module_source(%s,%s,%s,%s) AS generation',
                (subscription_id, source, scope_ref, list(types))).fetchone()['generation']
    except errors.InsufficientPrivilege as error:
        if 'module_not_linked' in str(error):
            raise not_linked() from None
        raise


def source_states(c, subscription_id):
    """{(source, scope_ref): 'active' | 'access_changed'} of a subscription's module sources."""
    rows = c.execute('SELECT s.source,s.scope_ref,' + live_sql(c, 's.subscription', 's.source', 's.scope_ref') +
        ' AS live FROM subscription_module_sources s WHERE s.subscription=%s', (subscription_id,)).fetchall()
    return {(row['source'], row['scope_ref']): 'active' if row['live'] else 'access_changed' for row in rows}


# --- Owner CLI (the admin tool's `module link`/`module unlink` run these) ------------------------------------------

def _check(principal, module, module_principal=None):
    from .store import principal_id
    principal_id(principal)
    if module not in MODULES:
        raise ValueError('invalid_module')
    if module_principal is not None and (not isinstance(module_principal, str) or
            _PRINCIPAL.fullmatch(module_principal) is None):
        raise ValueError('invalid_module_principal')


def _view(row, **extra):
    from .store import serial
    return serial({'principal': row['principal'], 'module': row['module'], 'module_principal': row['module_principal'],
        'generation': row['generation'], 'state': row['state'], 'updated_at': row['updated_at'], **extra})


def link(c, principal, module, module_principal):
    """Grant (or regrant) a principal's link; an unchanged active link is returned as it is (no new generation)."""
    from .policy import lock_principal
    _check(principal, module, module_principal)
    lock_principal(c, principal)
    if c.execute('SELECT 1 FROM principals WHERE id=%s', (principal,)).fetchone() is None:
        raise ValueError('unknown_principal')
    old = c.execute('SELECT * FROM module_links WHERE principal=%s AND module=%s', (principal, module)).fetchone()
    if old and old['state'] == 'active' and old['module_principal'] == module_principal:
        return _view(old, changed=False)
    row = c.execute('SELECT * FROM public.set_module_link(%s,%s,%s,%s)', (principal, module, module_principal,
        'active')).fetchone()
    return _view(row, changed=True)


def unlink(c, principal, module):
    """Revoke at once: the generation moves, so every source admitted under the link stays refused."""
    from .policy import lock_principal
    _check(principal, module)
    lock_principal(c, principal)
    old = c.execute('SELECT * FROM module_links WHERE principal=%s AND module=%s', (principal, module)).fetchone()
    if old is None:
        raise ValueError('module_link_not_found')
    if old['state'] == 'revoked':
        return _view(old, changed=False)
    row = c.execute('SELECT * FROM public.set_module_link(%s,%s,%s,%s)', (principal, module, old['module_principal'],
        'revoked')).fetchone()
    return _view(row, changed=True)


def links(c, principal):
    if not ready(c):
        return []
    return [_view(row) for row in c.execute('SELECT * FROM module_links WHERE principal=%s ORDER BY module',
        (principal,))]
