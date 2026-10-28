"""Operator-registered signed hints. Hints never ACK events or assert a host turn."""
from __future__ import annotations
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import time
import threading
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .egress import EgressNotSent, answer, deadline_after, post
from .subscriptions import _owned, _state, _view, _worker_identity


_DNS_SLOTS = threading.BoundedSemaphore(4)
# ANYM-534: operator receivers resolve from their own slots, so hanging agent endpoint lookups never starve them.
OPERATOR_DNS_SLOTS = threading.BoundedSemaphore(2)
_DNS_TIMEOUT_S = 2
# ANYM-534: queued self-service hints per key; past it, a subscription that already has one queued stages no more.
ENDPOINT_QUEUE_PER_KEY = 32
ENDPOINT_LANE_LOCK = 'deedkit-delivery-endpoint-lane'
# A live leased endpoint hint or endpoint test of the key: one attempt in flight per key across both (ANYM-534).
KEY_IN_FLIGHT = '''(EXISTS (SELECT 1 FROM subscription_outbox f JOIN subscriptions k ON k.id=f.subscription
    WHERE f.endpoint IS NOT NULL AND f.state='leased' AND f.lease_until>clock_timestamp()
    AND k.principal={principal} AND k.key_id={key_id})
    OR EXISTS (SELECT 1 FROM delivery_endpoint_tests f JOIN delivery_endpoints k ON k.id=f.endpoint
    WHERE f.state='leased' AND f.lease_until>clock_timestamp() AND k.principal={principal} AND k.key_id={key_id}))'''
# ANYM-519: is_global accepts these; they translate to or embed another (possibly internal) address.
_REFUSED_V6 = tuple(ipaddress.ip_network(net) for net in ('64:ff9b::/96', '64:ff9b:1::/48', '2002::/16', '2001::/32'))


def _refused(address):
    if not address.is_global or address.is_multicast:
        return True
    return isinstance(address, ipaddress.IPv6Address) and bool(address.ipv4_mapped or address.sixtofour or
        address.teredo or any(address in net for net in _REFUSED_V6))


def _addresses(host, port, *, slots=None):
    # libc DNS may outlive a socket deadline. Bound response latency and retain
    # one of four slots until that lookup really ends, without an unbounded queue.
    slots = _DNS_SLOTS if slots is None else slots
    if not slots.acquire(blocking=False):
        raise OSError('receiver_dns_busy')
    finished = threading.Event()
    result = {}
    def lookup():
        try:
            result['addresses'] = sorted({record[4][0] for record in
                socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        except OSError:
            result['error'] = True
        finally:
            slots.release()
            finished.set()
    try:
        threading.Thread(target=lookup, daemon=True, name='deedkit-receiver-dns').start()
    except BaseException:
        slots.release()
        raise
    if not finished.wait(_DNS_TIMEOUT_S) or result.get('error'):
        raise OSError('receiver_dns_unavailable')
    return result['addresses']


def validate_endpoint(endpoint, *, resolver=_addresses):
    """Validate every returned address; the transport pins one validated address."""
    try:
        if not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 2048:
            raise ValueError
        if any(ord(ch) < 33 or ord(ch) == 127 for ch in endpoint):
            raise ValueError
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or
                parsed.fragment or parsed.query or parsed.port not in (None, 443)):
            raise ValueError
        host = parsed.hostname.encode('idna').decode('ascii')
        # The request line is ASCII-encoded at send time; reject paths that never could be.
        (parsed.path or '/').encode('ascii')
        if host.endswith('.'):
            host = host[:-1]
        addresses = resolver(host, 443)
        if not addresses or len(addresses) > 32:
            raise ValueError
        addresses = [str(ipaddress.ip_address(address)) for address in addresses]
        if any(_refused(ipaddress.ip_address(address)) for address in addresses):
            raise ValueError
        return host, addresses, parsed.path or '/'
    except (ValueError, UnicodeError, TypeError):
        raise ValueError('unsafe_receiver') from None


def _secret(filename):
    """Secrets stay in private operator configuration, never in an agent response."""
    if not isinstance(filename, str) or not Path(filename).is_absolute():
        raise ValueError('unsafe_receiver_secret')
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not 32 <= info.st_size <= 4096:
            raise ValueError('unsafe_receiver_secret')
        value = os.read(fd, 4097)
        if not 32 <= len(value) <= 4096:
            raise ValueError('unsafe_receiver_secret')
        return value
    finally:
        os.close(fd)


def register_receiver(store, receiver_id, principal, key_id, endpoint, secret_file, *, resolver=_addresses):
    """Operator API only. Registration cannot be called through agent MCP/HTTP."""
    receiver_id, key_id = str(UUID(receiver_id)), str(UUID(key_id))
    validate_endpoint(endpoint, resolver=resolver)
    _secret(secret_file)
    with store.pool.connection() as c:
        from .policy import lock_principal
        lock_principal(c, principal)
        owner = c.execute('''SELECT 1 FROM api_keys WHERE id=%s AND principal=%s
            AND NOT revoked AND expires_at>clock_timestamp()''', (key_id, principal)).fetchone()
        if owner is None:
            raise ValueError('invalid_receiver_owner')
        # IDs identify immutable exact credentials/endpoints. Rotation creates a
        # new ID, then explicit versioned subscription reconfiguration.
        c.execute('''INSERT INTO subscription_receivers(id,principal,key_id,endpoint,secret_file)
            VALUES(%s,%s,%s,%s,%s)''', (receiver_id, principal, key_id, endpoint, secret_file))
    return {'receiver_id': receiver_id, 'registered': True}



def disable_receiver(store, receiver_id):
    """Operator revocation uses the same ACL fence as in-flight delivery."""
    from .policy import lock_principal
    receiver_id = str(UUID(receiver_id))
    with store.pool.connection() as c:
        owner = c.execute('SELECT principal FROM subscription_receivers WHERE id=%s', (receiver_id,)).fetchone()
        if owner is None:
            raise ValueError('invalid_receiver')
        lock_principal(c, owner['principal'])
        c.execute('UPDATE subscription_receivers SET enabled=false WHERE id=%s', (receiver_id,))
        c.execute("""UPDATE subscription_outbox SET state='cancelled',finished_at=clock_timestamp(),
            error_code='receiver_disabled',lease_token=NULL,lease_until=NULL
            WHERE receiver=%s AND state IN ('pending','leased')""", (receiver_id,))
    return {'receiver_id': receiver_id, 'enabled': False}

def configure_subscription_delivery(store, c, p, subscription_id, version, receiver_id=None, delivery_policy=None,
        endpoint_id=None):
    from .store import APIError
    if 'read' not in p['permissions']:
        raise APIError('forbidden', 403)
    if receiver_id is not None and endpoint_id is not None:
        raise APIError('invalid_parameters')
    if endpoint_id is not None:
        # ANYM-519: a self-service endpoint needs delivery-write and the operator's activation.
        from .delivery_endpoints import gate_on
        if 'delivery-write' not in p['permissions']:
            raise APIError('forbidden', 403)
        if not gate_on(c):
            raise APIError('delivery_disabled', 409)
    if type(version) is not int or not 1 <= version <= 2**53 - 1:
        raise APIError('invalid_version')
    row = _owned(c, p, subscription_id)
    if row['state'] == 'access_changed':
        raise APIError('subscription_access_changed', 409)
    if row['state'] not in ('active', 'paused'):
        raise APIError('subscription_closed', 409)
    if row['version'] != version:
        raise APIError('subscription_conflict', 409)
    from .delivery_policy import carry_window, normalize_policy
    policy = normalize_policy(c, delivery_policy, row['selector'])
    if policy is not None and receiver_id is None and endpoint_id is None:
        raise APIError('invalid_delivery_policy')
    previous = c.execute('SELECT * FROM subscription_delivery WHERE subscription=%s', (row['id'],)).fetchone()
    if receiver_id is None and endpoint_id is None:
        c.execute('DELETE FROM subscription_delivery WHERE subscription=%s', (row['id'],))
    elif endpoint_id is not None:
        from .delivery_endpoints import _endpoint_id, _state_error, lock_endpoint
        endpoint_id = _endpoint_id(endpoint_id)
        # Shared, so a concurrent pause, delete or operator disable commits before or after this binding.
        lock_endpoint(c, endpoint_id, shared=True)
        endpoint = c.execute("""SELECT id,state FROM delivery_endpoints WHERE id=%s AND principal=%s AND key_id=%s
            AND state<>'deleted'""", (endpoint_id, p['id'], p['key_id'])).fetchone()
        if endpoint is None:
            raise APIError('not_found', 404)
        if endpoint['state'] != 'enabled':
            raise _state_error(endpoint)
    else:
        try:
            receiver_id = str(UUID(receiver_id))
        except (ValueError, TypeError, AttributeError):
            raise APIError('not_found', 404) from None
        allowed = c.execute('''SELECT id FROM subscription_receivers WHERE id=%s AND principal=%s
            AND key_id=%s AND enabled''', (receiver_id, p['id'], p['key_id'])).fetchone()
        if allowed is None:
            raise APIError('not_found', 404)
    if receiver_id is not None or endpoint_id is not None:
        # The whole delivery configuration belongs to the next version; a silence timer starts
        # at this commit and any later version change cancels it.
        expect = (policy or {}).get('expect_reply_within_s')
        # Before schema 49 there is no endpoint column; a receiver binding then writes only its own.
        endpoint_column = endpoint_id is not None or 'endpoint' in (previous or {})
        c.execute('''INSERT INTO subscription_delivery(subscription,receiver,''' + ('endpoint,' if endpoint_column else '') +
            '''policy,policy_version,silence_deadline)
            VALUES(%s,%s,''' + ('%s,' if endpoint_column else '') + '''%s,%s,CASE WHEN %s::integer IS NULL THEN NULL
                ELSE clock_timestamp()+%s::integer*interval '1 second' END)
            ON CONFLICT(subscription) DO UPDATE SET receiver=excluded.receiver,''' +
            ('endpoint=excluded.endpoint,' if endpoint_column else '') + '''
            configured_at=clock_timestamp(),policy=excluded.policy,policy_version=excluded.policy_version,
            silence_deadline=excluded.silence_deadline''',
            (row['id'], receiver_id, *([endpoint_id] if endpoint_column else []), Jsonb(policy) if policy else None,
             row['version'] + 1, expect, expect))
    row = c.execute('''UPDATE subscriptions SET version=version+1,updated_at=clock_timestamp()
        WHERE id=%s RETURNING *''', (row['id'],)).fetchone()
    c.execute("""UPDATE subscription_outbox SET state='cancelled',finished_at=clock_timestamp(),
        error_code='configuration_changed' WHERE subscription=%s AND state IN ('pending','leased')""", (row['id'],))
    if previous is not None and (receiver_id or endpoint_id) is not None and (
            str(previous['receiver']), str(previous.get('endpoint'))) == (str(receiver_id), str(endpoint_id)):
        carry_window(c, row, version)
    result = _view(c, row)
    result['delivery'] = {'passive_read': True, 'ack': True, 'push': receiver_id is not None or endpoint_id is not None,
        'receiver_id': receiver_id, 'host_wakeup': 'unverified', 'host_turn_verified': False}
    if endpoint_id is not None:
        result['delivery'].update(endpoint_id=endpoint_id, target='endpoint')
    if row['state'] != 'access_changed':
        from .delivery_policy import delivery_state
        from .store import serial
        result['delivery'].update(serial(delivery_state(c, row)))
    return result


def _hint_target(c, row):
    """(receiver, endpoint) of a binding that may stage a hint now, else None (ANYM-519 gate)."""
    binding = c.execute('SELECT * FROM subscription_delivery WHERE subscription=%s', (row['id'],)).fetchone()
    if binding is None or binding.get('endpoint') is None:
        return binding
    from .delivery_endpoints import endpoint_stageable
    return binding if endpoint_stageable(c, binding['endpoint']) else None


def endpoint_queue_full(c, row):
    """True when the key holds ENDPOINT_QUEUE_PER_KEY queued endpoint hints of any kind and subscription.

    Callers hold the key's subscription-hints lock (budget_slot), so no writer passes the count concurrently.
    """
    return c.execute('''SELECT count(*)>=%s AS full
        FROM subscription_outbox o JOIN subscriptions s ON s.id=o.subscription WHERE o.endpoint IS NOT NULL
        AND o.state IN ('pending','leased') AND s.principal=%s AND s.key_id=%s''',
        (ENDPOINT_QUEUE_PER_KEY, row['principal'], row['key_id'])).fetchone()['full']


def enqueue_hint(c, row, through, *, kind='events', request_id=''):
    binding = _hint_target(c, row)
    if binding is None:
        return None
    target = 'receiver' if binding['receiver'] is not None else 'endpoint'
    if target == 'endpoint' and endpoint_queue_full(c, row):
        from .store import APIError
        raise APIError('rate_limited', 429)
    return c.execute('''INSERT INTO subscription_outbox(subscription,subscription_version,''' + target + ''',
        through_position,kind,request_id) VALUES(%s,%s,%s,%s,%s,%s)
        ON CONFLICT(subscription,subscription_version,kind,through_position,request_id)
        DO NOTHING RETURNING *''',
        (row['id'], row['version'], binding[target], through, kind, request_id)).fetchone()


def _probe_view(row):
    return {'hint_id': str(row['id']), 'subscription_id': str(row['subscription']), 'kind': 'test',
        'status': 'queued', 'host_turn_verified': False, 'event_acknowledged': False}


def test_subscription(store, c, p, subscription_id, request_id):
    from .store import APIError
    if 'read' not in p['permissions']:
        raise APIError('forbidden', 403)
    if not isinstance(request_id, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,128}', request_id):
        raise APIError('invalid_request_id')
    row = _owned(c, p, subscription_id)
    if row['state'] == 'access_changed':
        raise APIError('subscription_access_changed', 409)
    if row['state'] != 'active':
        raise APIError('subscription_closed', 409)
    existing = c.execute('''SELECT * FROM subscription_outbox WHERE subscription=%s AND kind='test'
        AND request_id=%s ORDER BY created_at LIMIT 1''', (row['id'], request_id)).fetchone()
    if existing:
        return _probe_view(existing)
    if c.execute('''SELECT 1 FROM subscription_outbox WHERE subscription=%s AND kind='test'
        AND created_at>clock_timestamp()-interval '1 minute' LIMIT 1''', (row['id'],)).fetchone():
        raise APIError('rate_limited', 429)
    # A probe is an explicit request, not a held event: over the key's budget it is refused.
    from .delivery_policy import budget_slot
    if budget_slot(c, row, c.execute('SELECT clock_timestamp() AS t').fetchone()['t']) is not None:
        raise APIError('rate_limited', 429)
    hint = enqueue_hint(c, row, row['matched_through'], kind='test', request_id=request_id)
    if hint is None:
        raise APIError('delivery_not_configured', 409)
    return _probe_view(hint)


def _transport(endpoint, body, headers, *, resolved, timeout=5):
    """Pinned TLS socket; no proxies, redirects, URL credentials or second DNS lookup. Failures before the first
    request byte are EgressNotSent (ANYM-581); the dispatcher in Compose uses egress.proxy_transport instead."""
    host, addresses, path = resolved
    remaining = deadline_after(timeout)
    try:
        raw = socket.create_connection((addresses[0], 443), timeout=remaining())
    except OSError as error:
        raise EgressNotSent('receiver_connect_failed') from error
    with raw:
        try:
            raw.settimeout(remaining())
            connection = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        except OSError as error:
            raise EgressNotSent('receiver_tls_failed') from error
        with connection:
            return post(connection, host, path, body, headers, remaining, retry_after=True)


# ANYM-518: webhook delivery counts as working while the dispatcher's last pass is this recent.
HEARTBEAT_MAX_AGE_S = 120
# Budget of each optional delivery-status read in capabilities (two reads, so at most 0.4 s).
DIAGNOSTIC_STATEMENT_MS = 200
DIAGNOSTIC_LOCK_MS = 100
TRANSPORT_TOOLS = {'pull': ['fetch_subscription_events', 'ack_subscription_events'],
    'long_poll': ['wait_for_subscription_events'], 'webhook': ['configure_subscription_delivery']}


def beat(store):
    """The dispatcher's liveness row; a failed write never stops hint delivery (schema 48)."""
    import psycopg
    try:
        with store.pool.connection() as c:
            c.execute('''INSERT INTO subscription_dispatcher_heartbeat(id,beat_at) VALUES(1,clock_timestamp())
                ON CONFLICT(id) DO UPDATE SET beat_at=EXCLUDED.beat_at''')
        return True
    except psycopg.Error:
        return False


def delivery_transports(c, p):
    """Each way to consume subscription events: whether this caller may use it (`authorized`), has it set up
    (`configured`) and it works now (`operational`); `reason` names the first failing condition (ANYM-518)."""
    import psycopg
    from .policy import OPERATION_PERMISSIONS
    permitted = set(p['permissions'])
    # Optional diagnostics: a failed or slow read (lock, timeout, privilege, missing table) marks webhook status
    # unavailable inside its own savepoint and never fails capabilities, which every gateway call needs (gateway
    # discovery waits 2 s). Each read gets DIAGNOSTIC_STATEMENT_MS; the caller's limits come back afterwards.
    limits = c.execute("SELECT current_setting('statement_timeout') AS s,current_setting('lock_timeout') AS l").fetchone()
    try:
        with c.transaction():
            c.execute("SELECT set_config('statement_timeout',%s,true),set_config('lock_timeout',%s,true)",
                (f'{DIAGNOSTIC_STATEMENT_MS}ms', f'{DIAGNOSTIC_LOCK_MS}ms'))
            beat_row = c.execute('''SELECT clock_timestamp()-beat_at<=make_interval(secs=>%s) AS fresh
                FROM subscription_dispatcher_heartbeat WHERE id=1''', (HEARTBEAT_MAX_AGE_S,)).fetchone()
            # Only bindings of subscriptions still valid for this key: not expired, same scope and permissions.
            # ANYM-519: a bound enabled self-service endpoint counts as configured too (schema 49).
            from .delivery_endpoints import gate_on, schema_ready
            endpoints = schema_ready(c)
            bound = c.execute('''SELECT coalesce(bool_or(r.enabled),false) AS enabled,''' + ('''
                coalesce(bool_or(e.state='enabled'),false)''' if endpoints else 'false') + ''' AS endpoint,count(*) AS n
                FROM subscriptions s JOIN subscription_delivery d ON d.subscription=s.id
                LEFT JOIN subscription_receivers r ON r.id=d.receiver''' + (''' LEFT JOIN delivery_endpoints e ON e.id=d.endpoint'''
                if endpoints else '') + '''
                WHERE s.principal=%s AND s.key_id=%s AND s.state IN ('active','paused')
                AND (s.expires_at IS NULL OR s.expires_at>clock_timestamp())
                AND s.scope_version=%s AND s.key_permission_version=%s''',
                (p['id'], p['key_id'], p['scope_version'], p['key_permission_version'])).fetchone()
            gate = gate_on(c, fence=False)
            c.execute("SELECT set_config('statement_timeout',%s,true),set_config('lock_timeout',%s,true)",
                (limits['s'], limits['l']))
    except psycopg.Error:
        # The savepoint's rollback also restores the caller's limits.
        beat_row = bound = gate = None
    from .delivery_endpoints import TOOLS, load_policy, max_endpoints
    policy_set = load_policy() is not None
    try:
        limit = max_endpoints()
    except ValueError:
        limit = None
    rows = []
    for name, tools in TRANSPORT_TOOLS.items():
        # Every consumption tool also needs read (the backend checks it before its own permission).
        authorized = all({'read', OPERATION_PERMISSIONS[tool]} <= permitted for tool in tools)
        failures = [] if authorized else ['not_authorized']
        configured = operational = True
        if name == 'webhook' and bound is None:
            configured = operational = False
            failures.append('delivery_status_unavailable')
        elif name == 'webhook':
            configured = bound['enabled'] or bool(bound['endpoint'] and gate and policy_set)
            operational = configured and bool(beat_row and beat_row['fresh'])
            if not bound['enabled'] and not gate:
                failures.append('delivery_disabled')
            if not bound['enabled'] and not policy_set:
                failures.append('delivery_policy_unset')
            if not bound['n']:
                failures.append('no_receiver_bound')
            elif not configured:
                failures.append('receiver_disabled')
            if beat_row is None:
                failures.append('dispatcher_heartbeat_absent')
            elif not beat_row['fresh']:
                failures.append('dispatcher_heartbeat_stale')
        row = {'name': name, 'tools': tools, 'authorized': authorized, 'configured': configured,
            'operational': operational, 'reason': failures[0] if failures else None}
        if name == 'long_poll':
            row['max_wait_s'] = 25
        if name == 'webhook':
            # ANYM-519: self-service once the operator enables the feature and sets the host policy.
            self_service = bool(gate) and policy_set
            row.update(registration='self_service' if self_service else 'operator_only',
                next_step=('register_delivery_endpoint, then create_subscription with delivery {endpoint_id}'
                    if self_service else 'ask the operator to register a receiver'),
                heartbeat_max_age_s=HEARTBEAT_MAX_AGE_S,
                self_service={'enabled': bool(gate), 'policy_set': policy_set, 'tools': list(TOOLS),
                    'max_endpoints': limit, 'signature_scheme': 'standard_webhooks_v1',
                    'reissued_key_carries_endpoints': False})
        rows.append(row)
    return rows


def endpoint_tests(store, *, transport=_transport, resolver=_addresses, limit=8, served=None):
    """ANYM-519 rotation promotion and endpoint tests; a failure there never stops hints (None)."""
    try:
        from .delivery_endpoints import dispatch_tests, promote_rotations, schema_ready
        with store.pool.connection() as c:
            ready = schema_ready(c) and promote_rotations(c) is not None
        if ready:
            return dispatch_tests(store, transport=transport, resolver=resolver, limit=limit, served=served)
    except Exception as error:
        from .telemetry import capture
        capture(error, 'endpoint_tests_failed')
        print(json.dumps({'error': 'endpoint_tests_failed', 'class': type(error).__name__}), flush=True)
    return None


_DUE = "(o.state='pending' AND o.next_attempt_at<=clock_timestamp() OR o.state='leased' AND o.lease_until<=clock_timestamp())"


# Each key's oldest due endpoint hint or pending endpoint test, keys with an attempt in flight left out (ANYM-534).
_ENDPOINT_QUEUE = '''SELECT DISTINCT ON (q.principal,q.key_id) q.kind,q.id,q.principal,q.key_id FROM (
    SELECT 'hint' AS kind,o.id,s.principal,s.key_id,o.next_attempt_at AS due,o.created_at
    FROM subscription_outbox o JOIN subscriptions s ON s.id=o.subscription WHERE o.endpoint IS NOT NULL AND ''' + _DUE + '''
    UNION ALL SELECT 'test',t.id,e.principal,e.key_id,t.created_at,t.created_at
    FROM delivery_endpoint_tests t JOIN delivery_endpoints e ON e.id=t.endpoint
    WHERE t.state='pending' OR t.state='leased' AND t.lease_until<=clock_timestamp()) q
    WHERE NOT ''' + KEY_IN_FLIGHT.format(principal='q.principal', key_id='q.key_id') + '''
    ORDER BY q.principal,q.key_id,q.due,q.created_at,q.id'''


def next_endpoint_item(c, served, kind, lock, strict=True):
    """The endpoint lanes' one scheduler over hints and endpoint tests: lock the item of the least recently served key.

    One endpoint lane (or the endpoint test pass) picks at a time, so a key never has two attempts in flight.
    Strict: when the next key's item is of the other kind, None, so neither queue overtakes a waiting key (the
    first endpoint lane runs a hint pick and a test pick each pass). Non-strict skips to this kind's keys.
    """
    c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (ENDPOINT_LANE_LOCK,))
    for row in sorted(c.execute(_ENDPOINT_QUEUE).fetchall(), key=lambda row: served.get((row['principal'], row['key_id']), 0)):
        if row['kind'] != kind:
            if strict:
                return None
            continue
        picked = c.execute(lock, (row['id'],)).fetchone()
        if picked is not None:
            served[(row['principal'], row['key_id'])] = max(served.values(), default=0) + 1
            return picked
    return None


def _next_endpoint_hint(c, served):
    """ANYM-534 endpoint lane: the due hint of the least recently served key with no attempt in flight."""
    return next_endpoint_item(c, served, 'hint', 'SELECT * FROM subscription_outbox WHERE id=%s FOR UPDATE SKIP LOCKED')


def dispatch_hints(store, *, limit=16, transport=_transport, resolver=_addresses, lane=None, served=None):
    """Leased at-least-once hints, independently reauthorized before each send.

    lane None is one serial pass over every hint (with the heartbeat and endpoint tests, as before ANYM-534);
    'operator' takes only operator receiver hints and 'endpoint' only self-service ones, one key at a time,
    least recently served key first (served: the lane's shared {(principal, key_id): turn}).
    """
    from .policy import lock_principal
    from .store import bounded
    bounded(limit, 100)
    if lane not in (None, 'operator', 'endpoint'):
        raise ValueError('unknown_lane')
    stats = dict(delivered=0, retried=0, dead_letter=0, cancelled=0, deferred=0)
    if lane is None:
        beat(store)
        tests = endpoint_tests(store, transport=transport, resolver=resolver)
        if tests is not None:
            stats['endpoint_tests'] = tests
    served = {} if served is None else served
    holds = []
    for _ in range(limit):
        lease = uuid4()
        with store.pool.connection() as c:
            if lane == 'endpoint':
                hint = _next_endpoint_hint(c, served)
            else:
                hint = c.execute('''SELECT o.* FROM subscription_outbox o WHERE ''' + _DUE +
                    (" AND o.receiver IS NOT NULL" if lane == 'operator' else '') +
                    ''' ORDER BY o.next_attempt_at,o.created_at,o.id LIMIT 1 FOR UPDATE SKIP LOCKED''').fetchone()
            if hint is None:
                break
            if hint['attempts'] >= 8:
                c.execute("""UPDATE subscription_outbox SET state='dead_letter',
                    finished_at=clock_timestamp(),error_code='delivery_budget_exhausted',
                    lease_token=NULL,lease_until=NULL WHERE id=%s""", (hint['id'],))
                stats['dead_letter'] += 1
                continue
            if hint['state'] == 'leased' and hint.get('endpoint') is not None:
                # ANYM-582/585: a lease that ran out mid-attempt may have reached an at-most-once receiver. Settle it
                # before DNS, quiet hours or the budget can turn it back into a pending hint that is sent again.
                from .delivery_endpoints import at_most_once_endpoint
                if at_most_once_endpoint(c, hint['endpoint']):
                    c.execute("""UPDATE subscription_outbox SET state='dead_letter',finished_at=clock_timestamp(),
                        error_code='outcome_unknown',lease_token=NULL,lease_until=NULL WHERE id=%s""", (hint['id'],))
                    stats['dead_letter'] += 1
                    continue
            now = c.execute('SELECT clock_timestamp() AS t').fetchone()['t']
            if hint['kind'] != 'test' and not hint['quiet_bypass']:
                # A hint deferred by the budget or retried after receiver downtime can reach a
                # quiet window after it was committed: it waits, no attempt is consumed.
                from .delivery_policy import quiet_until
                binding = c.execute('''SELECT policy FROM subscription_delivery WHERE subscription=%s
                    AND policy_version=%s''', (hint['subscription'], hint['subscription_version'])).fetchone()
                quiet = quiet_until(c, binding['policy'], now) if binding else None
                if quiet is not None:
                    c.execute("""UPDATE subscription_outbox SET state='pending',next_attempt_at=%s,
                        lease_token=NULL,lease_until=NULL WHERE id=%s""", (quiet, hint['id']))
                    stats['deferred'] += 1
                    continue
            if hint['admitted_at'] is None:
                # The first POST of a hint ID takes one slot of the key's rolling hour, with or
                # without a delivery policy. At the limit it waits for the next slot: no attempt
                # is consumed and it never dead-letters for the budget.
                from .delivery_policy import admission_slot
                owner = c.execute('SELECT principal,key_id FROM subscriptions WHERE id=%s',
                    (hint['subscription'],)).fetchone()
                slot = admission_slot(c, owner, now)
                if slot is None and hint.get('endpoint') is not None:
                    from .delivery_policy import host_admission_slot
                    slot = host_admission_slot(c, hint['endpoint'], now)
                if slot is not None:
                    c.execute("""UPDATE subscription_outbox SET state='pending',next_attempt_at=%s,
                        lease_token=NULL,lease_until=NULL WHERE id=%s""", (slot, hint['id']))
                    stats['deferred'] += 1
                    continue
            # Commit the reservation before any external I/O. A process crash
            # after POST can lose the result, never the consumed attempt slot.
            c.execute("""UPDATE subscription_outbox SET state='leased',lease_token=%s,attempts=attempts+1,
                admitted_at=coalesce(admitted_at,clock_timestamp()),
                lease_until=clock_timestamp()+interval '1 minute' WHERE id=%s""", (lease, hint['id']))
        # DNS validation, the secret read and the body build run before the fence, so the
        # fence hold is bounded by the transport deadline plus database time. Registrations
        # are immutable; the fence rechecks the exact binding. The body carries the hint's
        # version, which the fence requires to equal the locked subscription's.
        state, error, prepared, unsafe = 'pending', 'receiver_unavailable', None, False
        target = refused = None
        if hint.get('endpoint') is not None:
            # ANYM-519: a self-service endpoint; DNS and the host policy here, secrets and body inside the fence.
            from .delivery_endpoints import prepare
            registered, (target, problem) = None, prepare(store, hint['endpoint'], resolver)
            unsafe = problem == 'unsafe_endpoint'
            refused = problem if problem in ('endpoint_policy_refused', 'access_or_configuration_changed') else None
        else:
            with store.pool.connection() as c:
                registered = c.execute('SELECT endpoint,secret_file FROM subscription_receivers WHERE id=%s AND enabled',
                    (hint['receiver'],)).fetchone()
        if registered is not None:
            try:
                prepared = (validate_endpoint(registered['endpoint'], resolver=resolver),
                    _secret(registered['secret_file']), json.dumps(dict(hint_id=str(hint['id']),
                    subscription_id=str(hint['subscription']), subscription_version=hint['subscription_version'],
                    through=hint['through_position'], kind=hint['kind'], host_turn_verified=False),
                    sort_keys=True, separators=(',', ':')).encode())
            except (OSError, TimeoutError):
                # Includes ssl.SSLCertVerificationError, which is also a ValueError:
                # a certificate problem can be transient and keeps the bounded retry budget.
                pass
            except ValueError:
                unsafe = True
        with store.pool.connection() as c:
            # Principal lock precedes subscription lock, matching gateway ACL
            # order. The bounded TLS exchange holds this shared ACL fence so a
            # concurrent revocation cannot commit between authorization and send.
            owner = c.execute('SELECT principal,key_id FROM subscriptions WHERE id=%s',
                (hint['subscription'],)).fetchone()
            if owner:
                lock_principal(c, owner['principal'], shared=True)
            # The hold starts once the principal lock is held: waiting for it is contention, not hold.
            fenced = time.monotonic()
            p = _worker_identity(c, owner) if owner else None
            row = c.execute('SELECT * FROM subscriptions WHERE id=%s FOR UPDATE',
                (hint['subscription'],)).fetchone()
            receiver = c.execute('''SELECT r.* FROM subscription_receivers r JOIN subscription_delivery d
                ON d.receiver=r.id WHERE d.subscription=%s AND r.id=%s AND r.enabled''',
                (hint['subscription'], hint['receiver'])).fetchone()
            if p is not None and row is not None:
                row = _state(c, row, p)
            endpoint = None
            if hint.get('endpoint') is not None:
                from .delivery_endpoints import fenced as fence_endpoint
                endpoint, refused = (None, refused) if refused else fence_endpoint(c, p, hint['endpoint'],
                    target[0] if target else None)
                bound = c.execute('SELECT 1 FROM subscription_delivery WHERE subscription=%s AND endpoint=%s',
                    (hint['subscription'], hint['endpoint'])).fetchone()
                receiver = endpoint if bound else None
                if receiver is not None:
                    receiver = {'principal': p['id'], 'key_id': p['key_id']}
            eligible = (p is not None and row is not None and row['state'] == 'active' and
                row['version'] == hint['subscription_version'] and receiver is not None and
                receiver['principal'] == p['id'] and receiver['key_id'] == p['key_id'] and
                (registered is None or (receiver['endpoint'], receiver['secret_file']) ==
                 (registered['endpoint'], registered['secret_file'])))
            live = c.execute("""SELECT 1 FROM subscription_outbox WHERE id=%s AND state='leased'
                AND lease_token=%s AND lease_until>clock_timestamp() FOR UPDATE""", (hint['id'], lease)).fetchone()
            if live:
                delay = min(3600, 2**(hint['attempts'] + 1))
                if not eligible:
                    state, error = 'cancelled', refused or 'access_or_configuration_changed'
                # The retry age starts at admission: waiting for a budget slot or quiet hours never expires a hint.
                elif hint['attempts'] >= 8 or c.execute("SELECT %s<clock_timestamp()-interval '7 days' AS expired",
                        (hint['admitted_at'] or now,)).fetchone()['expired']:
                    state, error = 'dead_letter', 'delivery_budget_exhausted'
                elif unsafe:
                    state, error = 'dead_letter', 'unsafe_endpoint' if endpoint else 'unsafe_receiver_configuration'
                elif endpoint is not None and endpoint['at_most_once'] and hint['state'] == 'leased':
                    # ANYM-582/585: a lease that ran out mid-attempt may have reached the receiver; never send it again.
                    state, error = 'dead_letter', 'outcome_unknown'
                elif endpoint is not None and target is not None:
                    from .delivery_endpoints import caller_headers, hint_body, outcome, signed_headers
                    body = hint_body(c, hint, row, p)
                    headers = signed_headers((endpoint['signing_secret'], endpoint['pending_secret']), hint['id'], body,
                        caller_headers(endpoint))
                    status = failure = retry_after = None
                    try:
                        status, retry_after = answer(transport(endpoint['url'], body, headers, resolved=target[1], timeout=5))
                        if type(status) is int and 200 <= status < 300:
                            state, error = 'delivered', None
                        elif type(status) is int and 300 <= status < 400:
                            state, error = 'dead_letter', 'redirect_rejected'
                    except (OSError, TimeoutError) as caught:
                        failure = caught
                    except ValueError as caught:
                        failure, state, error = caught, 'dead_letter', 'unsafe_endpoint'
                    if endpoint['at_most_once']:
                        # A vendor endpoint retries only its documented "no run started" statuses and an unsent
                        # request; a generic self-registered one only an unsent request.
                        result, error = outcome(endpoint, status, failure)
                        state = {'delivered': 'delivered', 'retry': 'pending', 'final': 'dead_letter'}[result]
                    if status in (429, 503) and type(retry_after) is int and retry_after > 0:
                        # ANYM-604: the receiver's Retry-After, never sooner than the own backoff, capped at an hour.
                        delay = min(3600, max(delay, retry_after))
                elif prepared is not None:
                    resolved, secret, body = prepared
                    try:
                        timestamp = str(int(time.time()))
                        headers = {'Content-Type': 'application/json', 'X-DeedKit-Hint-ID': str(hint['id']),
                            'X-DeedKit-Timestamp': timestamp, 'X-DeedKit-Signature': 'sha256=' +
                            hmac.new(secret, timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()}
                        status = answer(transport(receiver['endpoint'], body, headers, resolved=resolved, timeout=5))[0]
                        if type(status) is int and 200 <= status < 300:
                            state, error = 'delivered', None
                        elif type(status) is int and 300 <= status < 400:
                            state, error = 'dead_letter', 'redirect_rejected'
                    except (OSError, TimeoutError):
                        pass
                    except ValueError:
                        state, error = 'dead_letter', 'unsafe_receiver_configuration'
                if state == 'pending' and hint['attempts'] + 1 >= 8:
                    state, error = 'dead_letter', 'delivery_budget_exhausted'
                c.execute('''UPDATE subscription_outbox SET state=%s,
                    error_code=%s,lease_token=NULL,lease_until=NULL,
                    next_attempt_at=clock_timestamp()+(%s*interval '1 second'),
                    finished_at=CASE WHEN %s='pending' THEN NULL ELSE clock_timestamp() END
                    WHERE id=%s AND state='leased' AND lease_token=%s''',
                    (state, error, delay, state, hint['id'], lease))
        # Each attempt's fence hold, so a latency regression shows up in a measurement.
        holds.append(round((time.monotonic() - fenced) * 1000))
        if live:
            stats['retried' if state == 'pending' else state] += 1
    return dict(stats, fence_hold_ms=holds)
