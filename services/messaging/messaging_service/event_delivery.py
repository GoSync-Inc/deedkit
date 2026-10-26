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

from .subscriptions import _owned, _state, _view, _worker_identity


_DNS_SLOTS = threading.BoundedSemaphore(4)
_DNS_TIMEOUT_S = 2


def _addresses(host, port):
    # libc DNS may outlive a socket deadline. Bound response latency and retain
    # one of four slots until that lookup really ends, without an unbounded queue.
    if not _DNS_SLOTS.acquire(blocking=False):
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
            _DNS_SLOTS.release()
            finished.set()
    try:
        threading.Thread(target=lookup, daemon=True, name='deedkit-receiver-dns').start()
    except BaseException:
        _DNS_SLOTS.release()
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
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
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

def configure_subscription_delivery(store, c, p, subscription_id, version, receiver_id, delivery_policy=None):
    from .store import APIError
    if 'read' not in p['permissions']:
        raise APIError('forbidden', 403)
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
    if policy is not None and receiver_id is None:
        raise APIError('invalid_delivery_policy')
    previous = c.execute('SELECT receiver FROM subscription_delivery WHERE subscription=%s', (row['id'],)).fetchone()
    if receiver_id is None:
        c.execute('DELETE FROM subscription_delivery WHERE subscription=%s', (row['id'],))
    else:
        try:
            receiver_id = str(UUID(receiver_id))
        except (ValueError, TypeError, AttributeError):
            raise APIError('not_found', 404) from None
        allowed = c.execute('''SELECT id FROM subscription_receivers WHERE id=%s AND principal=%s
            AND key_id=%s AND enabled''', (receiver_id, p['id'], p['key_id'])).fetchone()
        if allowed is None:
            raise APIError('not_found', 404)
        # The whole delivery configuration belongs to the next version; a silence timer starts
        # at this commit and any later version change cancels it.
        expect = (policy or {}).get('expect_reply_within_s')
        c.execute('''INSERT INTO subscription_delivery(subscription,receiver,policy,policy_version,silence_deadline)
            VALUES(%s,%s,%s,%s,CASE WHEN %s::integer IS NULL THEN NULL
                ELSE clock_timestamp()+%s::integer*interval '1 second' END)
            ON CONFLICT(subscription) DO UPDATE SET receiver=excluded.receiver,configured_at=clock_timestamp(),
            policy=excluded.policy,policy_version=excluded.policy_version,silence_deadline=excluded.silence_deadline''',
            (row['id'], receiver_id, Jsonb(policy) if policy else None, row['version'] + 1, expect, expect))
    row = c.execute('''UPDATE subscriptions SET version=version+1,updated_at=clock_timestamp()
        WHERE id=%s RETURNING *''', (row['id'],)).fetchone()
    c.execute("""UPDATE subscription_outbox SET state='cancelled',finished_at=clock_timestamp(),
        error_code='configuration_changed' WHERE subscription=%s AND state IN ('pending','leased')""", (row['id'],))
    if previous is not None and str(previous['receiver']) == receiver_id:
        carry_window(c, row, version)
    result = _view(c, row)
    result['delivery'] = {'passive_read': True, 'ack': True, 'push': receiver_id is not None,
        'receiver_id': receiver_id, 'host_wakeup': 'unverified', 'host_turn_verified': False}
    if row['state'] != 'access_changed':
        from .delivery_policy import delivery_state
        from .store import serial
        result['delivery'].update(serial(delivery_state(c, row)))
    return result


def enqueue_hint(c, row, through, *, kind='events', request_id=''):
    binding = c.execute('SELECT receiver FROM subscription_delivery WHERE subscription=%s', (row['id'],)).fetchone()
    if binding is None:
        return None
    return c.execute('''INSERT INTO subscription_outbox(subscription,subscription_version,receiver,
        through_position,kind,request_id) VALUES(%s,%s,%s,%s,%s,%s)
        ON CONFLICT(subscription,subscription_version,kind,through_position,request_id)
        DO NOTHING RETURNING *''',
        (row['id'], row['version'], binding['receiver'], through, kind, request_id)).fetchone()


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
    """Pinned TLS socket; no proxies, redirects, URL credentials or second DNS lookup."""
    host, addresses, path = resolved
    deadline = time.monotonic() + timeout
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError
        return value
    with socket.create_connection((addresses[0], 443), timeout=remaining()) as raw:
        raw.settimeout(remaining())
        with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as connection:
            connection.settimeout(remaining())
            request = ('POST ' + path + ' HTTP/1.1\r\nHost: ' + ('[' + host + ']' if ':' in host else host) + '\r\nConnection: close\r\n' +
                'Content-Length: ' + str(len(body)) + '\r\n' +
                ''.join(key + ': ' + value + '\r\n' for key, value in headers.items()) + '\r\n').encode('ascii')
            connection.sendall(request + body)
            status = b''
            while b'\r\n' not in status:
                connection.settimeout(remaining())
                part = connection.recv(256)
                if not part or len(status) + len(part) > 1024:
                    raise ValueError('invalid_receiver_response')
                status += part
            line = status.split(b'\r\n', 1)[0]
            match = re.fullmatch(rb'HTTP/1\.[01] ([0-9]{3})(?: [\x20-\x7e]*)?', line)
            if match is None:
                raise ValueError('invalid_receiver_response')
            return int(match[1])


# ANYM-518: webhook delivery counts as working while the dispatcher's last pass is this recent.
HEARTBEAT_MAX_AGE_S = 120
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
    from .policy import OPERATION_PERMISSIONS
    permitted = set(p['permissions'])
    beat_row = c.execute('''SELECT clock_timestamp()-beat_at<=make_interval(secs=>%s) AS fresh
        FROM subscription_dispatcher_heartbeat WHERE id=1''', (HEARTBEAT_MAX_AGE_S,)).fetchone()
    bound = c.execute('''SELECT coalesce(bool_or(r.enabled),false) AS enabled,count(*) AS n
        FROM subscriptions s JOIN subscription_delivery d ON d.subscription=s.id
        JOIN subscription_receivers r ON r.id=d.receiver
        WHERE s.principal=%s AND s.key_id=%s AND s.state IN ('active','paused')''',
        (p['id'], p['key_id'])).fetchone()
    rows = []
    for name, tools in TRANSPORT_TOOLS.items():
        # Every consumption tool also needs read (the backend checks it before its own permission).
        authorized = all({'read', OPERATION_PERMISSIONS[tool]} <= permitted for tool in tools)
        failures = [] if authorized else ['not_authorized']
        configured = operational = True
        if name == 'webhook':
            configured = bound['enabled']
            operational = configured and bool(beat_row and beat_row['fresh'])
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
            # Until self-service endpoints ship (ANYM-519), only the operator registers a receiver.
            row.update(registration='operator_only', next_step='ask the operator to register a receiver',
                heartbeat_max_age_s=HEARTBEAT_MAX_AGE_S)
        rows.append(row)
    return rows


def dispatch_hints(store, *, limit=16, transport=_transport, resolver=_addresses):
    """Leased at-least-once hints, independently reauthorized before each send."""
    from .policy import lock_principal
    from .store import bounded
    bounded(limit, 100)
    beat(store)
    stats = dict(delivered=0, retried=0, dead_letter=0, cancelled=0, deferred=0)
    holds = []
    for _ in range(limit):
        lease = uuid4()
        with store.pool.connection() as c:
            hint = c.execute('''SELECT * FROM subscription_outbox WHERE
                (state='pending' AND next_attempt_at<=clock_timestamp() OR
                 state='leased' AND lease_until<=clock_timestamp())
                ORDER BY next_attempt_at,created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED''').fetchone()
            if hint is None:
                break
            if hint['attempts'] >= 8:
                c.execute("""UPDATE subscription_outbox SET state='dead_letter',
                    finished_at=clock_timestamp(),error_code='delivery_budget_exhausted',
                    lease_token=NULL,lease_until=NULL WHERE id=%s""", (hint['id'],))
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
            eligible = (p is not None and row is not None and row['state'] == 'active' and
                row['version'] == hint['subscription_version'] and receiver is not None and
                receiver['principal'] == p['id'] and receiver['key_id'] == p['key_id'] and
                (registered is None or (receiver['endpoint'], receiver['secret_file']) ==
                 (registered['endpoint'], registered['secret_file'])))
            live = c.execute("""SELECT 1 FROM subscription_outbox WHERE id=%s AND state='leased'
                AND lease_token=%s AND lease_until>clock_timestamp() FOR UPDATE""", (hint['id'], lease)).fetchone()
            if live:
                if not eligible:
                    state, error = 'cancelled', 'access_or_configuration_changed'
                # The retry age starts at admission: waiting for a budget slot or quiet hours never expires a hint.
                elif hint['attempts'] >= 8 or c.execute("SELECT %s<clock_timestamp()-interval '7 days' AS expired",
                        (hint['admitted_at'] or now,)).fetchone()['expired']:
                    state, error = 'dead_letter', 'delivery_budget_exhausted'
                elif unsafe:
                    state, error = 'dead_letter', 'unsafe_receiver_configuration'
                elif prepared is not None:
                    resolved, secret, body = prepared
                    try:
                        timestamp = str(int(time.time()))
                        headers = {'Content-Type': 'application/json', 'X-DeedKit-Hint-ID': str(hint['id']),
                            'X-DeedKit-Timestamp': timestamp, 'X-DeedKit-Signature': 'sha256=' +
                            hmac.new(secret, timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()}
                        status = transport(receiver['endpoint'], body, headers, resolved=resolved, timeout=5)
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
                    (state, error, min(3600, 2**(hint['attempts'] + 1)), state, hint['id'], lease))
        # Each attempt's fence hold, so a latency regression shows up in a measurement.
        holds.append(round((time.monotonic() - fenced) * 1000))
        if live:
            stats['retried' if state == 'pending' else state] += 1
    return dict(stats, fence_hold_ms=holds)
