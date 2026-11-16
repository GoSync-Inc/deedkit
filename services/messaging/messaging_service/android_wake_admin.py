"""DK-53 (ANYM-997) operator CLI for one Android wake binding: an exact read, and enrollment that only ever creates a
PAUSED, unverified row. No activation, verified flag, rebind, endpoint, grant, secret, delivery or source read here:
migration 097 reserves activation for root's separate routine/installed verification."""
from datetime import datetime
import json
from uuid import UUID

from .android_wake import REF, tables
from .policy import key_id, principal_id

FIELDS = frozenset(('principal', 'key_id', 'device_id', 'owner_id', 'source_id', 'auth_epoch', 'endpoint_id', 'valid_until'))
MAX_FILE = 4096
RIGHTS = frozenset(('read', 'delivery-write'))
BINDING = '''principal,key_id,device_id,owner_id,source_id,auth_epoch,endpoint,generation,state,routine_verified,
    installed_verified,valid_until,journal_id,after_seq,valid_until>clock_timestamp() AS live'''


class Refused(Exception):
    def __init__(self, code, binding=None):
        super().__init__(code)
        self.code, self.binding = code, binding


def _view(row):
    from .store import serial
    return serial({'principal': row['principal'], 'key_id': row['key_id'], 'device_id': row['device_id'],
        'owner_id': row['owner_id'], 'source_id': row['source_id'], 'auth_epoch': row['auth_epoch'],
        'endpoint_id': row['endpoint'], 'generation': row['generation'], 'state': row['state'],
        'routine_verified': row['routine_verified'], 'installed_verified': row['installed_verified'],
        'valid_until': row['valid_until'], 'live': row['live'], 'journal_id': row['journal_id'], 'after_seq': row['after_seq']})


def _key(c, principal, credential):
    """The key's public state; another principal's key is indistinguishable from a missing one."""
    from .policy import effective_permissions
    row = c.execute('''SELECT p.enabled,p.permissions AS ceiling,k.permissions,k.revoked,k.expires_at,
        k.expires_at>clock_timestamp() AS live FROM api_keys k JOIN principals p ON p.id=k.principal
        WHERE k.id=%s AND k.principal=%s''', (credential, principal)).fetchone()
    if row is None: return None
    return {'found': True, 'principal_enabled': row['enabled'], 'revoked': row['revoked'], 'expires_at': row['expires_at'],
            'live': row['live'], 'wake_rights': RIGHTS <= set(effective_permissions(row['ceiling'], row['permissions']))}


def _endpoint(c, principal, credential, endpoint):
    """Same-key endpoint metadata only: never its URL, host, headers, secrets or fingerprints."""
    from .delivery_endpoints import at_most_once_endpoint
    row = c.execute('SELECT id,principal,key_id,state,version,created_at,updated_at FROM delivery_endpoints WHERE id=%s',
                    (endpoint,)).fetchone()
    if row is None or row['principal'] != principal or str(row['key_id']) != credential:
        return {'endpoint_id': str(endpoint), 'same_key': False}
    return {'endpoint_id': str(row['id']), 'same_key': True, 'state': row['state'], 'version': row['version'],
            'created_at': row['created_at'], 'updated_at': row['updated_at'],
            'at_most_once_under_policy': at_most_once_endpoint(c, row['id'])}


def _blockers(binding, key, endpoint):
    """What android_wake._binding/dispatch would refuse on now. An empty list is not delivery proof."""
    checks = (('state_' + binding['state'], binding['state'] != 'active'),
              ('routine_unverified', not binding['routine_verified']),
              ('installed_unverified', not binding['installed_verified']), ('binding_expired', not binding['live']),
              ('key_unavailable', not key or key['revoked'] or not key['live'] or not key['principal_enabled']),
              ('rights_missing', not key or not key['wake_rights']),
              ('endpoint_not_same_key', not endpoint['same_key']),
              ('endpoint_not_enabled', endpoint['same_key'] and endpoint['state'] != 'enabled'),
              ('endpoint_not_at_most_once', endpoint['same_key'] and not endpoint['at_most_once_under_policy']))
    return [name for name, blocked in checks if blocked]


def inspect(store, principal, credential, device):
    """One exact binding; read-only transaction, no lock helper, no row lock, no write."""
    from .store import serial
    principal, credential = principal_id(principal), key_id(credential)
    if not isinstance(device, str) or not REF.fullmatch(device): raise Refused('invalid_device_id')
    with store.pool.connection() as c, c.transaction():
        c.execute('SET TRANSACTION READ ONLY')
        if not tables(c): raise Refused('android_wake_not_installed')
        row = c.execute('SELECT ' + BINDING + ' FROM android_wake_bindings WHERE principal=%s AND key_id=%s AND device_id=%s',
                        (principal, credential, device)).fetchone()
        if row is None: return {'found': False, 'principal': principal, 'key_id': credential, 'device_id': device}
        key, endpoint = _key(c, principal, credential), _endpoint(c, principal, credential, row['endpoint'])
        outbox = {r['state']: r['n'] for r in c.execute('''SELECT state,count(*) AS n FROM android_wake_intents
            WHERE principal=%s AND key_id=%s AND device_id=%s GROUP BY state ORDER BY state''',
            (principal, credential, device))}
    return serial({'found': True, 'binding': _view(row), 'key': key or {'found': False}, 'endpoint': endpoint,
                   'outbox': outbox, 'activation_blockers': _blockers(row, key, endpoint),
                   'stored_flags_are_not_delivery_proof': True})


def _request(path):
    try:
        with open(path, 'rb') as source: raw = source.read(MAX_FILE + 1)
        document = json.loads(raw) if len(raw) <= MAX_FILE else None
    except (OSError, ValueError): document = None
    if not isinstance(document, dict) or set(document) != FIELDS: raise Refused('invalid_binding_request')
    try:
        principal_id(document['principal'])
        if (key_id(document['key_id']) != document['key_id'] or str(UUID(document['endpoint_id'])) != document['endpoint_id']
                or any(not isinstance(document[k], str) or not REF.fullmatch(document[k])
                       for k in ('device_id', 'owner_id', 'source_id'))
                or type(document['auth_epoch']) is not int or not 1 <= document['auth_epoch'] < 2**63
                or not isinstance(document['valid_until'], str)): raise ValueError
        until = datetime.fromisoformat(document['valid_until'])
        if until.tzinfo is None or until.utcoffset() is None: raise ValueError
    except (ValueError, TypeError, AttributeError): raise Refused('invalid_binding_request') from None
    return {**document, 'valid_until': until}


def _same(row, request):
    return (all(row[k] == request[k] for k in ('owner_id', 'source_id', 'auth_epoch', 'valid_until'))
            and str(row['endpoint']) == request['endpoint_id'])


def _existing(c, request, *, lock):
    row = c.execute('SELECT ' + BINDING + ' FROM android_wake_bindings WHERE principal=%s AND key_id=%s AND device_id=%s' +
                    (' FOR UPDATE' if lock else ''), (request['principal'], request['key_id'], request['device_id'])).fetchone()
    if row is None: return None
    if row['state'] != 'paused': raise Refused('android_wake_binding_' + row['state'], _view(row))
    if not _same(row, request): raise Refused('android_wake_binding_conflict', _view(row))
    return row


def enroll(store, path, *, check=False):
    """Create one PAUSED, unverified binding (cursor 0) or replay an identical paused one. --check writes nothing."""
    from .policy import lock_principal
    try:
        request = _request(path)
        with store.pool.connection() as c, c.transaction():
            if check: c.execute('SET TRANSACTION READ ONLY')
            if not tables(c): raise Refused('android_wake_not_installed')
            # The same shared ACL lock the API takes: a concurrent revoke/narrowing commits wholly before or after.
            if not check: lock_principal(c, request['principal'], shared=True)
            key = _key(c, request['principal'], request['key_id'])
            if key is None: raise Refused('android_wake_key_not_found')
            if not key['principal_enabled']: raise Refused('android_wake_principal_disabled')
            if key['revoked'] or not key['live']: raise Refused('android_wake_key_revoked' if key['revoked'] else 'android_wake_key_expired')
            if not key['wake_rights']: raise Refused('android_wake_rights_missing')
            if not c.execute('SELECT %s>clock_timestamp() AND %s<=%s AS ok', (request['valid_until'],)*2 +
                             (key['expires_at'],)).fetchone()['ok']: raise Refused('invalid_binding_lifetime')
            endpoint = c.execute('SELECT principal,key_id,state FROM delivery_endpoints WHERE id=%s' +
                                 ('' if check else ' FOR SHARE'), (request['endpoint_id'],)).fetchone()
            if not endpoint or endpoint['principal'] != request['principal'] or str(endpoint['key_id']) != request['key_id']:
                raise Refused('android_wake_endpoint_not_same_key')
            if endpoint['state'] != 'enabled': raise Refused('android_wake_endpoint_not_enabled')
            row = _existing(c, request, lock=not check)
            if row is None and check:
                return {'check': 'ok', 'would_create': {k: v for k, v in request.items() if k != 'valid_until'} | {
                    'valid_until': request['valid_until'].isoformat(), 'state': 'paused', 'routine_verified': False,
                    'installed_verified': False, 'after_seq': 0, 'generation': 1}}
            created = False
            if row is None:
                # Every other column keeps migration 097's defaults: paused, unverified, generation 1, cursor 0.
                row = c.execute('''INSERT INTO android_wake_bindings(principal,key_id,device_id,owner_id,source_id,auth_epoch,
                    endpoint,valid_until) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING ''' + BINDING,
                    tuple(request[k] for k in ('principal', 'key_id', 'device_id', 'owner_id', 'source_id', 'auth_epoch',
                                               'endpoint_id', 'valid_until'))).fetchone()
                created = row is not None
                # A concurrent enrollment committed first: the same request replays it, any other is refused.
                row = row or _existing(c, request, lock=True)
        return {'check': 'ok', 'exists': True, 'binding': _view(row)} if check else {'created': created, 'binding': _view(row)}
    except Refused as refusal:
        return {'error': refusal.code} | ({'binding': refusal.binding} if refusal.binding else {})


def command(store, name, *, principal=None, credential=None, device=None, path=None, check=False):
    try:
        if name == 'android-wake-binding-info': return inspect(store, principal, credential, device)
        return enroll(store, path, check=check)
    except Refused as refusal: return {'error': refusal.code}
    except ValueError as error: return {'error': str(error)}
