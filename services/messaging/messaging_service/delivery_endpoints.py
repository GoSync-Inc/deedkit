"""Agent self-registered webhook endpoints (ANYM-519): registry, operator host policy, Standard Webhooks.

Header values and signing secrets are write-only for the API role (gateway_role.sql grants INSERT and
UPDATE, never SELECT); the API reads back fingerprints only and the dispatcher (delivery_role.sql) is
the one reader. This is privilege separation, not encryption: database owners and backups hold them.
Nothing stages an endpoint hint or test until the operator enables delivery_features.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import idna

TOOLS = ['register_delivery_endpoint', 'list_delivery_endpoints', 'update_delivery_endpoint',
    'delete_delivery_endpoint', 'test_delivery_endpoint']
SCHEME = 'standard_webhooks_v1'
EXPECTED_URL = 'https on port 443, a public address, a host the operator allows'
RELAY_STEP = 'for a tailnet or private host ask the operator for the wake relay (ANYM-228)'
FAILED_STEP = 'fix the receiver, then call test_delivery_endpoint with a new request_id'
ROTATION_S = 86400
RESERVED = frozenset(('host', 'content-type', 'content-length', 'transfer-encoding', 'connection', 'upgrade',
    'te', 'trailer', 'cookie'))
RESERVED_PREFIXES = ('webhook-', 'x-deedkit-', 'mcp-')
STATE_ERRORS = {'agent_paused': 'endpoint_paused', 'auto_paused': 'endpoint_auto_paused',
    'operator_disabled': 'operator_disabled'}
BODY_LIMIT = 4096
MAX_REFERENCES = 20
# Never secret, private or header-value columns: the API role cannot read those.
COLUMNS = ('id,principal,key_id,request_id,request_hash,url,host,label,header_names,headers_fingerprint,'
    'secret_fingerprint,pending_fingerprint,rotation_started_at,state,version,created_at,updated_at')
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}")
_VALUE = re.compile(r'[\x21-\x7e](?:[\x20-\x7e]{0,1022}[\x21-\x7e])?')
_REQUEST = re.compile(r'[a-zA-Z0-9_.:-]{8,128}')
_SECRET = re.compile(r'whsec_[A-Za-z0-9+/]+={0,2}')
_NON_PUBLIC_SUFFIXES = ('localhost', 'local', 'internal', 'lan', 'home.arpa', 'ts.net', 'intranet', 'corp', 'private')
_CGNAT = ipaddress.ip_network('100.64.0.0/10')
# ANYM-582: vendor hosts the operator names in the policy. Grok Bot routine webhooks take a Bearer key, start a run
# on 200 and document every other status as "no run started"; they do not deduplicate a repeated request.
VENDORS = frozenset(('grok-bot-routine',))
# ANYM-585: a vendor recognised by its webhook URL, with no `vendors` entry. Grok Bot routines publish no host; the
# shape is Hookdeck's Grok Bot guide (fetched 2026-10-28). It only selects the at-most-once table and the Bearer check.
VENDOR_URLS = (('grok-bot-routine', 'api2.cursor.sh', re.compile(r'/automations/webhook/[^/]+')),)
_BEARER = re.compile(r'Bearer [\x21-\x7e]+')
try:
    from .egress import EgressNotSent
except ImportError:  # Before the egress transport (ANYM-534): no failure is known to be unsent.
    class EgressNotSent(OSError):
        pass


def _error(code, status, **extra):
    from .store import APIError
    error = APIError(code, status)
    for name, value in extra.items():
        setattr(error, name, value)
    return error


def gate_on(c, *, fence=True):
    """The operator's activation. With fence, a shared lock held to commit: set_delivery_features and
    reload_delivery_policy take it exclusive, so they wait for every staging transaction and in-flight POST
    that read the old gate or policy, and nothing reads the old one after they commit."""
    if fence:
        c.execute('SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))', (FEATURES_LOCK,))
    return c.execute('''SELECT EXISTS(SELECT FROM probe_metadata WHERE name='delivery_features'
        AND value='enabled') AS enabled''').fetchone()['enabled']


FEATURES_LOCK = 'delivery-features'


def lock_endpoint(c, endpoint_id, *, shared=False):
    """API changes take it exclusive; the dispatcher holds it shared across each POST."""
    function = 'pg_advisory_xact_lock_shared' if shared else 'pg_advisory_xact_lock'
    c.execute('SELECT ' + function + '(hashtextextended(%s,0))', ('delivery-endpoint:' + str(endpoint_id),))


def canonical_host(value):
    """IDNA UTS 46, lowercase, one trailing dot removed; ValueError otherwise."""
    if not isinstance(value, str) or not 1 <= len(value) <= 254:
        raise ValueError('invalid_host')
    value = value[:-1] if value.endswith('.') else value
    try:
        host = idna.encode(value, uts46=True).decode('ascii').lower()
    except (idna.IDNAError, UnicodeError):
        raise ValueError('invalid_host') from None
    if not 1 <= len(host) <= 253:
        raise ValueError('invalid_host')
    return host


def _matches(host, suffix):
    return host == suffix or host.endswith('.' + suffix)


def load_policy():
    """The operator's private JSON file, or None (unset). Read at registration and before each attempt."""
    name = os.environ.get('DELIVERY_POLICY_FILE')
    if not name or not Path(name).is_absolute():
        return None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
            return None
        data = json.loads(os.read(fd, 65537))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    if not isinstance(data, dict) or set(data) - {'allow_hosts', 'deny_hosts', 'allow_any_public', 'vendors',
            'self_registration'}:
        return None
    allow, deny, public = data.get('allow_hosts', []), data.get('deny_hosts', []), data.get('allow_any_public', False)
    vendors, self_registration = data.get('vendors', {}), data.get('self_registration', False)
    if type(public) is not bool or type(self_registration) is not bool or any(not isinstance(v, list) or len(v) > 1000 for v in (allow, deny)):
        return None
    if not isinstance(vendors, dict) or set(vendors) - VENDORS or any(
            not isinstance(v, list) or len(v) > 1000 for v in vendors.values()):
        return None
    try:
        allow, deny = [canonical_host(h) for h in allow], [canonical_host(h) for h in deny]
        vendors = {name: [canonical_host(h) for h in hosts] for name, hosts in vendors.items()}
    except ValueError:
        return None
    if not allow and not public and not self_registration:
        return None
    return {'allow': allow, 'deny': deny, 'allow_any_public': public, 'vendors': vendors,
        'self_registration': self_registration}


def admission(policy, host):
    """Why the policy admits a host: 'allow_hosts' (listed, or allow_any_public), 'self_registration' (ANYM-585: only
    the switch admits it, so switching it off stops the host's endpoints) or None (refused)."""
    if policy is None or any(_matches(host, suffix) for suffix in policy['deny']):
        return None
    if policy['allow_any_public'] or any(_matches(host, suffix) for suffix in policy['allow']):
        return 'allow_hosts'
    return 'self_registration' if policy.get('self_registration') else None


def host_allowed(policy, host):
    return admission(policy, host) is not None


def vendor_of(policy, host, url=None):
    """The vendor whose outcome table an endpoint uses (ANYM-582), by the policy's `vendors` or the URL's shape
    (ANYM-585), or None. Never widens host_allowed."""
    if not host_allowed(policy, host):
        return None
    named = next((name for name, hosts in sorted(policy.get('vendors', {}).items())
        if any(_matches(host, suffix) for suffix in hosts)), None)
    if named or not isinstance(url, str):
        return named
    path = urlsplit(url).path
    return next((name for name, shape_host, shape in VENDOR_URLS if host == shape_host and shape.fullmatch(path)), None)


def _vendor_headers(policy, host, url, names, values):
    """A vendor endpoint needs its key as `Authorization: Bearer <key>`; the value never appears in the error."""
    if vendor_of(policy, host, url) and not any(name.lower() == 'authorization' and _BEARER.fullmatch(value)
            for name, value in zip(names, values)):
        raise _error('invalid_header', 400, header='Authorization')


def vendor_result(status=None, failure=None):
    """A vendor host's attempt (ANYM-582): ('delivered' | 'retry' | 'final', error code).

    Only a documented "no run started" answer or a failure before the first request byte is retried; anything
    after the request was written may have started a run and is never sent again.
    """
    if failure is not None:
        return ('retry', 'receiver_unavailable') if isinstance(failure, EgressNotSent) else ('final', 'outcome_unknown')
    if type(status) is not int or not 200 <= status <= 599:
        # A 1xx (100 Continue) is not a final answer: the vendor may still start the run.
        return 'final', 'outcome_unknown'
    if 200 <= status < 300:
        return 'delivered', None
    if 300 <= status < 400:
        return 'final', 'redirect_rejected'
    return ('final', 'vendor_refused') if status in (401, 403) else ('retry', 'receiver_status')


def at_most_once(policy, host, url):
    """Whether an endpoint's attempts use the at-most-once table (vendor_result): a vendor endpoint (ANYM-582) or one
    admitted only by self-registration (ANYM-585), whose receiver no operator vouched for, so a request that may have
    arrived is never sent again whatever its URL turns out to be."""
    return bool(vendor_of(policy, host, url)) or admission(policy, host) == 'self_registration'


def at_most_once_endpoint(c, endpoint_id):
    """at_most_once for an endpoint ID under the current policy."""
    row = c.execute('SELECT host,url FROM delivery_endpoints WHERE id=%s', (endpoint_id,)).fetchone()
    return at_most_once(load_policy(), row['host'], row['url']) if row else False


def _private_answer(address):
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip in _CGNAT


def canonical_url(parsed, host):
    """The URL with its UTS 46 A-label host: the policy, DNS, TLS and Host all see one host (Python's codec is
    IDNA 2003, so a stored U-label URL would reach a different host at POST)."""
    port = parsed.port
    return parsed._replace(netloc=host if port is None else host + ':' + str(port)).geturl()


def resolve(url, policy, *, resolver):
    """(host, addresses, path) for a URL inside the policy, else ValueError('unsafe_endpoint' | 'relay')."""
    from .event_delivery import validate_endpoint
    seen = []
    def capture(host, port):
        answers = resolver(host, port)
        seen.extend(answers or [])
        return answers
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        host = canonical_host(parsed.hostname or '') if parsed else None
    except ValueError:
        host = None
    if host is None:
        raise ValueError('unsafe_endpoint')
    if parsed.username is not None or parsed.password is not None:
        raise ValueError('unsafe_endpoint')
    non_public = '.' not in host or any(_matches(host, suffix) for suffix in _NON_PUBLIC_SUFFIXES)
    if not non_public and host_allowed(policy, host):
        try:
            resolved = validate_endpoint(canonical_url(parsed, host), resolver=capture)
            if resolved[0] == host:
                return resolved
        except ValueError:
            pass
    try:
        private = any(_private_answer(address) for address in seen)
    except ValueError:
        private = False
    raise ValueError('relay' if non_public or private else 'unsafe_endpoint')


def _unsafe(reason):
    extra: dict[str, object] = {'field_errors': [{'field': 'url', 'reason': 'format', 'expected': EXPECTED_URL}]}
    if reason == 'relay':
        extra['next_step'] = RELAY_STEP
    return _error('unsafe_endpoint', 400, **extra)


def decoded_secret(value):
    """24-64 decoded bytes of a caller-supplied `whsec_<base64>`; DeedKit never generates one."""
    if not isinstance(value, str) or len(value) > 128 or not _SECRET.fullmatch(value):
        raise _error('invalid_signing_secret', 400)
    try:
        key = base64.b64decode(value[6:], validate=True)
    except (binascii.Error, ValueError):
        raise _error('invalid_signing_secret', 400) from None
    if not 24 <= len(key) <= 64:
        raise _error('invalid_signing_secret', 400)
    return key


def _fingerprint(data):
    return hashlib.sha256(data).hexdigest()[:8]


def normalize_headers(headers):
    """Sorted (names, values) of at most 4 caller headers; values never appear in an error."""
    if headers is None:
        return [], []
    if not isinstance(headers, dict) or len(headers) > 4:
        raise _error('invalid_header', 400)
    seen = set()
    for name, value in headers.items():
        if not isinstance(name, str) or not _TOKEN.fullmatch(name):
            raise _error('invalid_header', 400)
        lowered = name.lower()
        if lowered in RESERVED or lowered.startswith(RESERVED_PREFIXES):
            raise _error('reserved_header', 400, header=name)
        if lowered in seen or not isinstance(value, str) or not _VALUE.fullmatch(value):
            raise _error('invalid_header', 400, header=name)
        seen.add(lowered)
    names = sorted(headers)
    return names, [headers[name] for name in names]


def headers_fingerprint(names, values):
    if not names:
        return None
    return _fingerprint(json.dumps(dict(zip(names, values)), sort_keys=True, separators=(',', ':')).encode())


def sign(secret, message_id, timestamp, body):
    """Standard Webhooks v1 over the exact body bytes."""
    key = base64.b64decode(secret[6:])
    digest = hmac.new(key, f'{message_id}.{timestamp}.'.encode() + body, hashlib.sha256).digest()
    return 'v1,' + base64.b64encode(digest).decode()


def signed_headers(secrets, message_id, body, headers=(), *, timestamp=None):
    """Caller headers, then the reserved ones; both secrets sign during a rotation."""
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    result = dict(headers)
    result.update({'Content-Type': 'application/json', 'webhook-id': str(message_id), 'webhook-timestamp': timestamp,
        'webhook-signature': ' '.join(sign(secret, message_id, timestamp, body) for secret in secrets if secret)})
    return result


def _request_id(value):
    if not isinstance(value, str) or not _REQUEST.fullmatch(value):
        raise _error('invalid_request_id', 400)
    return value


def _endpoint_id(value):
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


def _allowed(c, p):
    if 'read' not in p['permissions'] or 'delivery-write' not in p['permissions']:
        raise _error('forbidden', 403)
    if not gate_on(c):
        raise _error('delivery_disabled', 409)


def max_endpoints():
    from .subscriptions import _bound_setting
    return _bound_setting('DELIVERY_MAX_ENDPOINTS_PER_KEY', 4, 1, 32)


def _test_view(row):
    if row is None:
        return None
    status = 'pending' if row['state'] in ('pending', 'leased') else row['state']
    view = {'test_id': str(row['id']), 'endpoint_id': str(row['endpoint']), 'request_id': row['request_id'],
        'test_status': status, 'http_status': row['http_status'], 'latency_ms': row['latency_ms'],
        'error_code': row['error_code'], 'created_at': row['created_at'], 'finished_at': row['finished_at']}
    if status == 'pending':
        view['next_step'] = {'tool': 'test_delivery_endpoint', 'endpoint_id': str(row['endpoint']),
            'request_id': row['request_id']}
    elif status == 'failed':
        view['next_step'] = FAILED_STEP
    from .store import serial
    return serial(view)


def _tests(c, endpoint_id):
    return c.execute('''SELECT id,endpoint,request_id,state,http_status,latency_ms,error_code,created_at,finished_at
        FROM delivery_endpoint_tests WHERE endpoint=%s ORDER BY created_at DESC,id DESC LIMIT 1''', (endpoint_id,)).fetchone()


def _view(c, row):
    from .store import serial
    last, policy = _tests(c, row['id']), load_policy()
    finished = c.execute('''SELECT state FROM delivery_endpoint_tests WHERE endpoint=%s AND state IN ('delivered','failed')
        ORDER BY finished_at DESC,id DESC LIMIT 1''', (row['id'],)).fetchone()
    rotation = None if row['rotation_started_at'] is None else {'pending_fingerprint': row['pending_fingerprint'],
        'started_at': row['rotation_started_at'], 'promotes_at': row['rotation_started_at'] + _day()}
    return serial({'endpoint_id': row['id'], 'version': row['version'], 'state': row['state'], 'url': row['url'],
        'host': row['host'], 'label': row['label'], 'header_names': list(row['header_names']),
        'headers_fingerprint': row['headers_fingerprint'], 'secret_fingerprint': row['secret_fingerprint'],
        'rotation': rotation, 'created_at': row['created_at'], 'updated_at': row['updated_at'],
        'health': {'delivered': 'ok', 'failed': 'failing'}[finished['state']] if finished else 'unknown',
        'last_test': _test_view(last), 'signature_scheme': SCHEME,
        'vendor': vendor_of(policy, row['host'], row['url']), 'at_most_once': at_most_once(policy, row['host'], row['url'])})


def _day():
    from datetime import timedelta
    return timedelta(seconds=ROTATION_S)


def _owned(c, p, endpoint_id, *, lock=False):
    endpoint_id = _endpoint_id(endpoint_id)
    if lock:
        lock_endpoint(c, endpoint_id)
    row = c.execute('SELECT ' + COLUMNS + ''' FROM delivery_endpoints WHERE id=%s AND principal=%s AND key_id=%s
        AND state<>'deleted' ''' + ('FOR UPDATE' if lock else ''), (endpoint_id, p['id'], p['key_id'])).fetchone()
    if row is None:
        raise _error('not_found', 404)
    from .policy import targets
    targets(p, [str(row['id'])])
    return row


def _retry_after(c, at, seconds):
    left = c.execute('SELECT extract(epoch FROM %s+make_interval(secs=>%s)-clock_timestamp()) AS s',
        (at, seconds)).fetchone()['s']
    return max(1, math.ceil(left))


def _stage_test(c, endpoint_id, request_id):
    return c.execute('''INSERT INTO delivery_endpoint_tests(endpoint,request_id) VALUES(%s,%s)
        RETURNING id,endpoint,request_id,state,http_status,latency_ms,error_code,created_at,finished_at''',
        (endpoint_id, request_id)).fetchone()


def _test_rate(c, p):
    recent = c.execute('''SELECT t.created_at FROM delivery_endpoint_tests t JOIN delivery_endpoints e ON e.id=t.endpoint
        WHERE e.principal=%s AND e.key_id=%s AND t.created_at>clock_timestamp()-interval '1 minute'
        ORDER BY t.created_at DESC LIMIT 1''', (p['id'], p['key_id'])).fetchone()
    if recent:
        raise _error('rate_limited', 429, retry_after_s=_retry_after(c, recent['created_at'], 60))


def _state_error(row):
    code = STATE_ERRORS[row['state']]
    step = ('ask the operator to enable the endpoint' if code == 'operator_disabled' else
        'call update_delivery_endpoint with action resume')
    return _error(code, 409, state=row['state'], next_step=step)


def register_delivery_endpoint(store, c, p, request_id, url, signing_secret, label='', headers=None, *,
        resolver=None):
    _allowed(c, p)
    request_id = _request_id(request_id)
    if not isinstance(url, str) or not 1 <= len(url) <= 2048:
        raise _unsafe('unsafe_endpoint')
    if not isinstance(label, str) or len(label) > 128 or any(ord(ch) < 32 or ord(ch) == 127 for ch in label):
        raise _error('invalid_parameters', 400)
    key = decoded_secret(signing_secret)
    names, values = normalize_headers(headers)
    # Secret and header values enter the request hash hashed, so a retry compares without storing them readable.
    request_hash = hashlib.sha256(json.dumps({'url': url, 'label': label,
        'secret': hashlib.sha256(key).hexdigest(),
        'headers': {name: hashlib.sha256(value.encode()).hexdigest() for name, value in zip(names, values)}},
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('delivery-endpoints:' + str(p['key_id']),))
    existing = c.execute('SELECT ' + COLUMNS + ' FROM delivery_endpoints WHERE key_id=%s AND request_id=%s',
        (p['key_id'], request_id)).fetchone()
    if existing:
        # A lost response is recovered by repeating the same request, before any rate or cap check.
        if existing['request_hash'] != request_hash or existing['principal'] != p['id']:
            raise _error('idempotency_conflict', 409)
        from .policy import targets
        targets(p, [str(existing['id'])])
        test = c.execute('''SELECT id,endpoint,request_id,state,http_status,latency_ms,error_code,created_at,finished_at
            FROM delivery_endpoint_tests WHERE endpoint=%s AND request_id=%s''', (existing['id'], request_id)).fetchone()
        return {**_view(c, existing), 'test': _test_view(test)}
    policy = load_policy()
    if policy is None:
        raise _error('delivery_policy_unset', 409)
    from .event_delivery import _addresses
    try:
        resolved = resolve(url, policy, resolver=resolver or _addresses)
    except ValueError as error:
        raise _unsafe(str(error)) from None
    except OSError:
        # DNS that does not answer is not proof of a public host.
        raise _unsafe('unsafe_endpoint') from None
    _vendor_headers(policy, resolved[0], canonical_url(urlsplit(url), resolved[0]), names, values)
    count = c.execute('''SELECT count(*) AS n FROM delivery_endpoints WHERE principal=%s AND key_id=%s
        AND state<>'deleted' ''', (p['id'], p['key_id'])).fetchone()['n']
    if count >= max_endpoints():
        raise _error('endpoint_limit', 429)
    recent = c.execute('''SELECT created_at FROM delivery_endpoints WHERE principal=%s AND key_id=%s
        AND created_at>clock_timestamp()-interval '1 minute' ORDER BY created_at DESC LIMIT 1''',
        (p['id'], p['key_id'])).fetchone()
    if recent:
        raise _error('rate_limited', 429, retry_after_s=_retry_after(c, recent['created_at'], 60))
    _test_rate(c, p)
    row = c.execute('''INSERT INTO delivery_endpoints(principal,key_id,request_id,request_hash,url,host,label,
        header_names,headers_fingerprint,secret_fingerprint,header_values,signing_secret)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING ''' + COLUMNS,
        (p['id'], p['key_id'], request_id, request_hash, canonical_url(urlsplit(url), resolved[0]), resolved[0],
         label, names,
         headers_fingerprint(names, values), _fingerprint(key), values or None, signing_secret)).fetchone()
    from .policy import targets
    targets(p, [str(row['id'])])
    # The inline test: the dispatcher sends it; the caller waits for it (finish_inline_test).
    test = _stage_test(c, row['id'], request_id)
    return {**_view(c, row), 'test': _test_view(test)}


def list_delivery_endpoints(store, c, p):
    _allowed(c, p)
    rows = c.execute('SELECT ' + COLUMNS + ''' FROM delivery_endpoints WHERE principal=%s AND key_id=%s
        AND state<>'deleted' ORDER BY created_at DESC,id DESC LIMIT 32''', (p['id'], p['key_id'])).fetchall()
    from .policy import targets
    targets(p, [str(row['id']) for row in rows])
    return {'items': [_view(c, row) for row in rows], 'has_more': False, 'max_endpoints': max_endpoints()}


def cancel_queued(c, endpoint_id, code):
    """Queued and in-flight hints and tests of the endpoint; the caller holds its exclusive lock."""
    hints = c.execute("""UPDATE subscription_outbox SET state='cancelled',finished_at=clock_timestamp(),error_code=%s
        WHERE endpoint=%s AND state IN ('pending','leased')""", (code, endpoint_id)).rowcount
    tests = c.execute("""UPDATE delivery_endpoint_tests SET state='cancelled',finished_at=clock_timestamp(),error_code=%s
        WHERE endpoint=%s AND state IN ('pending','leased')""", (code, endpoint_id)).rowcount
    return hints + tests


def update_delivery_endpoint(store, c, p, endpoint_id, version, action, new_secret=None, headers=None):
    _allowed(c, p)
    version = _version(version)
    if action not in ('rotate_secret', 'set_headers', 'pause', 'resume'):
        raise _error('invalid_parameters', 400)
    if (new_secret is not None) != (action == 'rotate_secret') or (headers is not None) != (action == 'set_headers'):
        raise _error('invalid_parameters', 400)
    row = _owned(c, p, endpoint_id, lock=True)
    if row['version'] != version:
        raise _error('endpoint_conflict', 409)
    changes, values = 'version=version+1,updated_at=clock_timestamp()', []
    if action == 'rotate_secret':
        key = decoded_secret(new_secret)
        if row['rotation_started_at'] is not None:
            raise _error('rotation_in_progress', 409, retry_after_s=_retry_after(c, row['rotation_started_at'], ROTATION_S))
        changes += ',pending_secret=%s,pending_fingerprint=%s,rotation_started_at=clock_timestamp()'
        values = [new_secret, _fingerprint(key)]
    elif action == 'set_headers':
        names, header_values = normalize_headers(headers)
        _vendor_headers(load_policy(), row['host'], row['url'], names, header_values)
        changes += ',header_names=%s,header_values=%s,headers_fingerprint=%s'
        values = [names, header_values or None, headers_fingerprint(names, header_values)]
    elif action == 'pause':
        if row['state'] == 'operator_disabled':
            raise _state_error(row)
        if row['state'] not in ('enabled', 'auto_paused'):
            raise _error('invalid_endpoint_state', 409, state=row['state'])
        changes += ",state='agent_paused'"
        cancel_queued(c, row['id'], 'endpoint_paused')
    else:
        if row['state'] == 'operator_disabled':
            raise _state_error(row)
        if row['state'] not in ('agent_paused', 'auto_paused'):
            raise _error('invalid_endpoint_state', 409, state=row['state'])
        changes += ",state='enabled'"
    row = c.execute('UPDATE delivery_endpoints SET ' + changes + ' WHERE id=%s RETURNING ' + COLUMNS,
        (*values, row['id'])).fetchone()
    return _view(c, row)


def delete_delivery_endpoint(store, c, p, endpoint_id, version):
    _allowed(c, p)
    version = _version(version)
    row = _owned(c, p, endpoint_id, lock=True)
    if row['version'] != version:
        raise _error('endpoint_conflict', 409)
    # Soft delete: history and foreign keys stay, the private values go. Bindings stay as a record, so
    # get_subscription shows delivery none with reason endpoint_deleted; nothing stages for them.
    c.execute('''UPDATE delivery_endpoints SET state='deleted',version=version+1,updated_at=clock_timestamp(),
        signing_secret=NULL,pending_secret=NULL,header_values=NULL,pending_fingerprint=NULL,rotation_started_at=NULL
        WHERE id=%s''', (row['id'],))
    cancel_queued(c, row['id'], 'endpoint_deleted')
    bound = c.execute('SELECT count(*) AS n FROM subscription_delivery WHERE endpoint=%s', (row['id'],)).fetchone()['n']
    return {'endpoint_id': str(row['id']), 'deleted': True, 'unbound_subscriptions': bound}


def test_delivery_endpoint(store, c, p, endpoint_id, request_id):
    _allowed(c, p)
    request_id = _request_id(request_id)
    row = _owned(c, p, endpoint_id)
    lock_endpoint(c, row['id'], shared=True)
    row = c.execute('SELECT ' + COLUMNS + ' FROM delivery_endpoints WHERE id=%s', (row['id'],)).fetchone()
    if row['state'] == 'operator_disabled':
        # Criterion 10: refused even for a replayed request ID.
        raise _state_error(row)
    existing = c.execute('''SELECT id,endpoint,request_id,state,http_status,latency_ms,error_code,created_at,finished_at
        FROM delivery_endpoint_tests WHERE endpoint=%s AND request_id=%s''', (row['id'], request_id)).fetchone()
    if existing:
        return _test_view(existing)
    if row['state'] != 'enabled':
        raise _state_error(row) if row['state'] in STATE_ERRORS else _error('not_found', 404)
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('delivery-endpoints:' + str(p['key_id']),))
    _test_rate(c, p)
    return _test_view(_stage_test(c, row['id'], request_id))


def endpoint_test(c, p, endpoint_id, request_id):
    """Read-only poll of one test of an owned endpoint (finish_inline_test); None if absent."""
    if not {'read', 'delivery-write'} <= set(p['permissions']):
        raise _error('forbidden', 403)
    row = c.execute('''SELECT t.id,t.endpoint,t.request_id,t.state,t.http_status,t.latency_ms,t.error_code,t.created_at,
        t.finished_at FROM delivery_endpoint_tests t JOIN delivery_endpoints e ON e.id=t.endpoint
        WHERE t.endpoint=%s AND t.request_id=%s AND e.principal=%s AND e.key_id=%s''',
        (_endpoint_id(endpoint_id), request_id, p['id'], p['key_id'])).fetchone()
    return _test_view(row)


async def finish_inline_test(store, token, result, *, wait_s=10.0):
    """Wait up to wait_s for the dispatcher to finish the staged test; past it the test stays pending."""
    block = result.get('test') if 'test' in result else result
    if not block or block.get('test_status') != 'pending':
        return result
    loop = asyncio.get_running_loop()
    deadline, current = loop.time() + wait_s, block
    while loop.time() < deadline:
        await asyncio.sleep(0.2)
        current = await asyncio.to_thread(store.delivery_endpoint_test, token, block['endpoint_id'], block['request_id'])
        if current is None or current['test_status'] != 'pending':
            break
    current = current or block
    if 'test' not in result:
        return current
    result = {**result, 'test': current, 'last_test': current}
    if current['test_status'] in ('delivered', 'failed'):
        result['health'] = 'ok' if current['test_status'] == 'delivered' else 'failing'
    if current.get('next_step'):
        result['next_step'] = current['next_step']
    return result


def set_delivery_features(store, enabled, *, verify_components=False):
    """Operator only. Disabling cancels every queued endpoint hint and test under each endpoint's lock. With
    verify_components (the CLI), enabling refuses unless the API, matcher and dispatcher heartbeats are fresh and
    report this version on the current schema (ANYM-534); the refusal names the first component and changes nothing."""
    with store.pool.connection() as c:
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (FEATURES_LOCK,))
        if enabled and verify_components:
            from .heartbeats import mismatch
            problem = mismatch(c)
            if problem is not None:
                return problem
        c.execute('''INSERT INTO probe_metadata(name,value) VALUES('delivery_features',%s)
            ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value''', ('enabled' if enabled else 'disabled',))
        cancelled = 0
        if not enabled:
            for row in c.execute("SELECT id FROM delivery_endpoints WHERE state<>'deleted' ORDER BY id").fetchall():
                lock_endpoint(c, row['id'])
                cancelled += cancel_queued(c, row['id'], 'delivery_disabled')
    return {'delivery_features': 'enabled' if enabled else 'disabled', 'cancelled': cancelled}


def reload_delivery_policy(store):
    """Operator only, after editing the policy file. Waits for every in-flight POST and staging transaction that
    read the old policy (exclusive fence), then cancels queued hints and tests of endpoints the new policy refuses.
    The change counts as made when this returns."""
    with store.pool.connection() as c:
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (FEATURES_LOCK,))
        policy = load_policy()
        refused = cancelled = 0
        for row in c.execute("SELECT id,host FROM delivery_endpoints WHERE state<>'deleted' ORDER BY id").fetchall():
            if host_allowed(policy, row['host']):
                continue
            refused += 1
            lock_endpoint(c, row['id'])
            cancelled += cancel_queued(c, row['id'], 'endpoint_policy_refused')
    # The vendor map as loaded, so an operator sees a host left out of `vendors` at the moment of the edit.
    return {'policy_set': policy is not None, 'refused_endpoints': refused, 'cancelled': cancelled,
        'vendors': policy['vendors'] if policy else {}, 'self_registration': bool(policy and policy['self_registration'])}


def list_endpoints_admin(store, principal=None):
    with store.pool.connection() as c:
        rows = c.execute('SELECT ' + COLUMNS + ''' FROM delivery_endpoints WHERE state<>'deleted'
            AND (%s::text IS NULL OR principal=%s) ORDER BY principal,created_at,id''', (principal, principal)).fetchall()
        policy = load_policy()
        # ANYM-585: `admission` shows which endpoints exist only through self-registration (the kill switch stops them).
        return {'items': [{**_view(c, row), 'principal': row['principal'], 'key_id': str(row['key_id']),
            'admission': admission(policy, row['host'])} for row in rows],
            'delivery_features': 'enabled' if gate_on(c, fence=False) else 'disabled', 'policy_set': policy is not None,
            'self_registration': bool(policy and policy['self_registration'])}


def set_operator_state(store, endpoint_id, enabled):
    """Operator only: operator_disabled is never set or cleared by an agent tool."""
    endpoint_id = str(UUID(endpoint_id))
    with store.pool.connection() as c:
        lock_endpoint(c, endpoint_id)
        row = c.execute("SELECT id,state FROM delivery_endpoints WHERE id=%s AND state<>'deleted' FOR UPDATE",
            (endpoint_id,)).fetchone()
        if row is None:
            raise ValueError('invalid_endpoint')
        if enabled and row['state'] != 'operator_disabled':
            raise ValueError('invalid_endpoint_state')
        state = 'enabled' if enabled else 'operator_disabled'
        if row['state'] != state:
            c.execute('UPDATE delivery_endpoints SET state=%s,version=version+1,updated_at=clock_timestamp() WHERE id=%s',
                (state, endpoint_id))
        cancelled = 0 if enabled else cancel_queued(c, endpoint_id, 'endpoint_disabled')
    return {'endpoint_id': endpoint_id, 'state': state, 'cancelled': cancelled}


# Dispatcher side (messaging_event_delivery role).

def promote_rotations(c):
    """After 24 h the pending secret becomes the current one; the API can read neither."""
    return c.execute('''UPDATE delivery_endpoints SET signing_secret=pending_secret,secret_fingerprint=pending_fingerprint,
        pending_secret=NULL,pending_fingerprint=NULL,rotation_started_at=NULL,updated_at=clock_timestamp()
        WHERE rotation_started_at<=clock_timestamp()-make_interval(secs=>%s) AND state<>'deleted'
        AND pending_secret IS NOT NULL''', (ROTATION_S,)).rowcount


def prepare(store, endpoint_id, resolver):
    """Outside the fence: (url, resolved) or an error code. DNS that does not answer retries later."""
    with store.pool.connection() as c:
        row = c.execute('SELECT url,host FROM delivery_endpoints WHERE id=%s', (endpoint_id,)).fetchone()
    if row is None:
        return None, 'access_or_configuration_changed'
    policy = load_policy()
    if not host_allowed(policy, row['host']):
        return None, 'endpoint_policy_refused'
    try:
        return (row['url'], resolve(row['url'], policy, resolver=resolver)), None
    except ValueError:
        return None, 'unsafe_endpoint'
    except (OSError, TimeoutError):
        return None, 'receiver_unavailable'


def fenced(c, p, endpoint_id, url):
    """Under the shared principal lock: the endpoint lock and every recheck. (row with secrets, None) or (None, code)."""
    # Lock order everywhere: the features fence, then the endpoint lock (set_delivery_features takes both).
    gate = gate_on(c)
    lock_endpoint(c, endpoint_id, shared=True)
    if p is None or not {'read', 'delivery-write'} <= set(p['permissions']):
        return None, 'access_or_configuration_changed'
    if not gate:
        return None, 'delivery_disabled'
    row = c.execute('SELECT * FROM delivery_endpoints WHERE id=%s', (endpoint_id,)).fetchone()
    if (row is None or row['state'] != 'enabled' or row['principal'] != p['id'] or row['key_id'] != p['key_id'] or
            url is not None and row['url'] != url or not row['signing_secret']):
        return None, 'access_or_configuration_changed'
    policy = load_policy()
    if not host_allowed(policy, row['host']):
        return None, 'endpoint_policy_refused'
    return {**row, 'vendor': vendor_of(policy, row['host'], row['url']),
        'at_most_once': at_most_once(policy, row['host'], row['url'])}, None


def caller_headers(row):
    return list(zip(row['header_names'] or [], row['header_values'] or []))


def hint_body(c, hint, row, p):
    """Content-free events/silence/test hint, at most 4 KiB; references rechecked under the fence."""
    base = {'hint_id': str(hint['id']), 'subscription_id': str(row['id']), 'version': row['version'],
        'label': row['label']}
    if hint['kind'] == 'silence':
        body = {**base, 'type': 'silence', 'send_id': (row['selector'].get('reply_anchor') or {}).get('send_id'),
            'next_call': {'tool': 'messages_get_subscription', 'subscription_id': str(row['id'])}}
        return _compact(body)
    if hint['kind'] == 'test':
        return _compact({**base, 'type': 'test', 'test_id': str(hint['id']), 'endpoint_id': str(hint['endpoint'])})
    consumption = c.execute('SELECT acked_through FROM subscription_consumption WHERE subscription=%s',
        (row['id'],)).fetchone()
    after = consumption['acked_through'] if consumption else row['start_position']
    # 049's definer function: IDs, timing and wake evidence only, never message text; scope-filtered here.
    matches = c.execute('SELECT * FROM public.subscription_hint_references(%s,%s,%s,%s,%s)',
        (row['id'], after, hint['through_position'], p['accounts'], p['chat_ids'])).fetchall()
    from .subscription_consumption import wake_reason
    from .store import serial
    references = []
    for item in matches:
        evidence = item['evidence']
        reasons = wake_reason({**evidence, 'account': item['account'], 'kind': item['kind'],
            'historical': item['origin'] in ('backfill', 'reconcile')}, row['selector'])
        reference = {'event_id': item['event'], 'kind': item['kind'], 'occurred_at': serial(item['created_at']),
            'wake_reason': reasons, 'account_id': item['account'], 'chat_id': evidence.get('chat'),
            'message_id': evidence.get('message'), 'sender_id': evidence.get('sender')}
        if 'reply_to_confirmed_send' in reasons:
            reference['reply_to_send_id'] = (row['selector'].get('reply_anchor') or {}).get('send_id')
        references.append(reference)
    total = matches[0]['total'] if matches else 0
    listed = references[:MAX_REFERENCES]
    while True:
        body = _compact({**base, 'type': 'events', 'references': listed, 'references_omitted': total - len(listed),
            'next_call': {'tool': 'messages_fetch_subscription_events', 'subscription_id': str(row['id'])}})
        if len(body) <= BODY_LIMIT or not listed:
            return body
        listed = listed[:-1]


def test_body(test):
    return _compact({'type': 'test', 'test_id': str(test['id']), 'endpoint_id': str(test['endpoint'])})


def _compact(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def dispatch_tests(store, *, limit=8, transport, resolver, served=None):
    """One attempt per staged test; the result is the endpoint's health. Same fence as hints.

    ANYM-534: picked by the endpoint lanes' scheduler (next_endpoint_item; served: the lanes' shared
    {(principal, key_id): turn}), so a key's test never runs beside its hint or ahead of a waiting key's turn.
    """
    from .event_delivery import next_endpoint_item
    from .subscriptions import _worker_identity
    stats = dict(delivered=0, failed=0, cancelled=0)
    strict, served = served is not None, {} if served is None else served
    for _ in range(limit):
        lease = uuid4()
        with store.pool.connection() as c:
            # Without lanes (served None, the serial pass) tests skip keys whose next item is a hint.
            test = next_endpoint_item(c, served, 'test', '''SELECT t.*,e.principal,e.key_id FROM delivery_endpoint_tests t
                JOIN delivery_endpoints e ON e.id=t.endpoint WHERE t.id=%s FOR UPDATE OF t SKIP LOCKED''', strict)
            if test is None:
                break
            if c.execute("SELECT %s<clock_timestamp()-interval '5 minutes' AS old", (test['created_at'],)).fetchone()['old']:
                c.execute("""UPDATE delivery_endpoint_tests SET state='failed',error_code='test_expired',
                    finished_at=clock_timestamp(),lease_token=NULL,lease_until=NULL WHERE id=%s""", (test['id'],))
                stats['failed'] += 1
                continue
            c.execute("""UPDATE delivery_endpoint_tests SET state='leased',lease_token=%s,
                lease_until=clock_timestamp()+interval '1 minute' WHERE id=%s""", (lease, test['id']))
        target, problem = prepare(store, test['endpoint'], resolver)
        state, error, status, latency = 'failed', problem, None, None
        with store.pool.connection() as c:
            p = _worker_identity(c, test)
            row, cancel = fenced(c, p, test['endpoint'], target[0] if target else None)
            if cancel in ('access_or_configuration_changed', 'delivery_disabled', 'endpoint_policy_refused'):
                state, error = ('failed' if cancel == 'endpoint_policy_refused' else 'cancelled'), cancel
            live = c.execute("""SELECT 1 FROM delivery_endpoint_tests WHERE id=%s AND state='leased' AND lease_token=%s
                AND lease_until>clock_timestamp() FOR UPDATE""", (test['id'], lease)).fetchone()
            if not live:
                continue
            if row is not None and target is not None and row['at_most_once'] and test['state'] == 'leased':
                # ANYM-582/585: a lease that ran out mid-attempt may have reached the receiver; never send it again.
                state, error = 'failed', 'outcome_unknown'
            elif row is not None and target is not None:
                body = test_body(test)
                headers = signed_headers((row['signing_secret'], row['pending_secret']), test['id'], body, caller_headers(row))
                started = time.monotonic()
                try:
                    status = transport(row['url'], body, headers, resolved=target[1], timeout=5)
                    state, error = ('delivered', None) if type(status) is int and 200 <= status < 300 else (
                        'failed', 'redirect_rejected' if type(status) is int and 300 <= status < 400 else 'receiver_status')
                    if row['at_most_once']:
                        result, error = vendor_result(status)
                        state = 'delivered' if result == 'delivered' else 'failed'
                except (OSError, TimeoutError) as failure:
                    state, error = 'failed', vendor_result(failure=failure)[1] if row['at_most_once'] else 'receiver_unavailable'
                except ValueError as failure:
                    state, error = 'failed', vendor_result(failure=failure)[1] if row['at_most_once'] else 'receiver_status'
                latency = round((time.monotonic() - started) * 1000)
                status = status if type(status) is int and 100 <= status <= 599 else None
            c.execute('''UPDATE delivery_endpoint_tests SET state=%s,error_code=%s,http_status=%s,latency_ms=%s,
                finished_at=clock_timestamp(),lease_token=NULL,lease_until=NULL WHERE id=%s AND lease_token=%s''',
                (state, error, status, latency, test['id'], lease))
        stats[state] += 1
    return stats


def endpoint_stageable(c, endpoint_id):
    """Only an enabled endpoint under the operator's activation gets a hint; else the range is unbound."""
    row = c.execute('SELECT state FROM delivery_endpoints WHERE id=%s', (endpoint_id,)).fetchone()
    return row is not None and row['state'] == 'enabled' and gate_on(c)


def schema_ready(c):
    """Schema 49 is applied; before it (rolling upgrade) every endpoint path stays off."""
    return c.execute("SELECT to_regclass('public.delivery_endpoint_tests') IS NOT NULL AS ready").fetchone()['ready']
