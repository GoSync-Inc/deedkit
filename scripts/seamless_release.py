#!/usr/bin/env python3
"""Verified blue-green messaging HTTP/API releases, with private Unix control.

This operator never upgrades collectors, calendar, voice, databases or the shared
public ingress. Install its dedicated router through the documented bootstrap.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import socket
import stat
import time
import urllib.parse
import uuid

try:
    from scripts import production as p
except ModuleNotFoundError:  # installed beside production.py on the host
    import production as p

Refused = p.Refused
GLOBAL_LOCK = Path('/var/lib/deedkit-production')
IMAGE = re.compile(r'sha256:[0-9a-f]{64}')
GENERATION = re.compile(r'[0-9a-f]{12}')
CONTROL_LIMIT = 8192
ROUTER_LIMIT = 1024 * 1024


def private_path(path, *, directory=False, owners=None):
    path = Path(path)
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or info.st_uid not in (owners or {os.geteuid()})
            or info.st_mode & 0o077
            or (directory and not stat.S_ISDIR(info.st_mode))
            or (not directory and not stat.S_ISREG(info.st_mode))):
        raise Refused('release paths must be private, operator-owned and not symlinks')
    return path


def validate_settings(settings):
    release = settings.get('http_release')
    required = {'api', 'gateway', 'gateway_policy', 'gateway_public_url', 'router', 'legacy'}
    if not isinstance(release, dict) or not required <= release.keys() or release.keys() - (required | {'drain_timeout', 'ready_timeout'}):
        raise Refused('invalid http_release configuration')
    selected = [(project['name'], service, target) for project in settings['projects']
                for service, target in project['services'].items()]
    if len(selected) != 2 or {item[2] for item in selected} != {'runtime', 'gateway'}:
        raise Refused('select exactly two HTTP services: runtime API and gateway')
    for role, target in [('api', 'runtime'), ('gateway', 'gateway')]:
        item = release[role]
        if not isinstance(item, dict) or set(item) != {'project', 'service'} or (item['project'], item['service'], target) not in selected:
            raise Refused('HTTP roles must match the two selected services')
    public = urllib.parse.urlsplit(release['gateway_public_url'])
    if public.scheme != 'https' or not public.hostname or public.username or public.password or public.query or public.fragment or public.path.rstrip('/') != '/deedkit':
        raise Refused('gateway_public_url must be an HTTPS origin with /deedkit prefix')
    router = release['router']
    if not isinstance(router, dict) or set(router) != {'container', 'image', 'network', 'directory', 'config_file', 'admin_socket', 'listen'}:
        raise Refused('invalid dedicated router configuration')
    if not IMAGE.fullmatch(str(router['image'])) or not all(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', str(router[key])) for key in ['container', 'network']):
        raise Refused('router needs an immutable image and explicit container/network')
    if type(router['listen']) is not int or router['listen'] != 8080:
        raise Refused('dedicated router must listen internally on port 8080')
    directory = Path(router['directory'])
    if (not directory.is_absolute() or Path(router['config_file']) != directory / 'router.json'
            or Path(router['admin_socket']) != directory / 'admin.sock'):
        raise Refused('router config and socket must be in its designated parent directory')
    private_path(directory, directory=True)
    policy = Path(release['gateway_policy'])
    if not policy.is_absolute():
        raise Refused('gateway policy must be an absolute private file')
    # Preserve the existing file and its runtime readability; the application
    # bind is read-only. Router/admin/state paths remain operator-owned only.
    private_path(policy, owners={os.geteuid(), 10001})
    # The gateway reads the policy through its directory mount (ANYM-424), so
    # the directory is as private as the file.
    private_path(policy.parent, directory=True, owners={os.geteuid(), 10001})
    if not isinstance(release['legacy'], dict) or set(release['legacy']) != {'api', 'gateway'}:
        raise Refused('explicit legacy HTTP upstreams are required')
    for role, port in [('api', 8080), ('gateway', 13240)]:
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*:' + str(port), str(release['legacy'][role])):
            raise Refused('legacy upstream must be an explicit Docker alias and canonical HTTP port')
    for key, default in [('drain_timeout', 300), ('ready_timeout', 120)]:
        value = release.get(key, default)
        if type(value) not in (int, float) or not 0 <= value <= 3600:
            raise Refused('release timeouts must be between zero and 3600 seconds')
    return settings


def validate_control(value, generation, sha):
    fields = {'protocol', 'generation', 'sha', 'phase', 'ready', 'active_requests', 'pending_tickets'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['protocol']) is not int or value['protocol'] != 1
            or value['generation'] != generation or value['sha'] != sha
            or value['phase'] not in {'warming', 'active', 'draining'}
            or type(value['ready']) is not bool
            or any(type(value[key]) is not int or value[key] < 0 for key in ['active_requests', 'pending_tickets'])):
        raise Refused('invalid or foreign release control response')
    return value


def control(path, action, generation, sha):
    if action not in {'status', 'activate', 'drain'}:
        raise Refused('unknown release control action')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(path))
        client.sendall((json.dumps({'action': action}) + '\n').encode())
        data = b''
        while b'\n' not in data:
            chunk = client.recv(min(1024, CONTROL_LIMIT + 1 - len(data)))
            if not chunk:
                raise Refused('incomplete release control response')
            data += chunk
            if len(data) > CONTROL_LIMIT:
                raise Refused('release control response exceeded the safety limit')
        line, remainder = data.split(b'\n', 1)
        if remainder.strip():
            raise Refused('unexpected release control response data')
    return validate_control(json.loads(line), generation, sha)


def route_document(settings, generation=None, retained=None, *, baseline=None):
    release = settings['http_release']
    ids = list(dict.fromkeys([*([generation] if generation else []), *(retained or [])]))
    for identifier in ids:
        if not GENERATION.fullmatch(identifier):
            raise Refused('invalid release generation')
    generations = copy.deepcopy(baseline['generations']) if baseline else {}
    for identifier in ids:
        expected = {'api': 'http-api-' + identifier + ':8080',
                    'gateway': 'http-gateway-' + identifier + ':13240', 'admit_files': True}
        if identifier in generations and generations[identifier] != expected:
            raise Refused('a fenced issuer identity cannot be reopened or reassigned')
        generations[identifier] = expected
    return {'version': 1, 'listen': ':8080', 'admin_socket': '/run/deedkit-router/admin.sock',
            'active': generation, 'legacy': copy.deepcopy(release['legacy']),
            'generations': generations}


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('127.0.0.1', timeout=5)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def router_request(settings, method, value=None, etag=None, *, path='/config/'):
    connection = UnixHTTPConnection(settings['http_release']['router']['admin_socket'])
    headers = {'Content-Type': 'application/json'}
    if etag:
        headers['If-Match'] = etag
    try:
        connection.request(method, path, None if value is None else json.dumps(value), headers)
        response = connection.getresponse()
        data = response.read(ROUTER_LIMIT + 1)
        if len(data) > ROUTER_LIMIT or response.status not in {200, 201, 204}:
            raise Refused('dedicated router rejected the configuration transaction')
        return json.loads(data) if data else None, response.getheader('ETag')
    finally:
        connection.close()


def router_get(settings):
    router_kind(settings)
    value, etag = router_request(settings, 'GET')
    if not isinstance(value, dict) or not etag:
        raise Refused('dedicated router did not provide a conditional configuration identity')
    return value, etag


def router_kind(settings):
    value, _ = router_request(settings, 'GET', path='/status')
    if (not isinstance(value, dict) or value != {'protocol': 1, 'kind': 'deedkit-release-router'}
            or type(value.get('protocol')) is not int):
        raise Refused('dedicated router does not implement the trusted atomic admission protocol')


def router_cas(settings, value, etag):
    if not etag:
        raise Refused('router conditional configuration identity is missing')
    router_request(settings, 'POST', value, etag)


def router_fence(settings, generation, etag):
    value, _ = router_request(settings, 'POST', {}, etag, path='/generations/' + generation + '/retire')
    return value


def router_inflight(settings, generation, role):
    upstream = 'http-' + role + '-' + generation + (':8080' if role == 'api' else ':13240')
    values, _ = router_request(settings, 'GET', path='/reverse_proxy/upstreams')
    if not isinstance(values, list):
        raise Refused('dedicated router upstream census is unavailable')
    matches = [item for item in values if isinstance(item, dict) and item.get('address') == upstream]
    if len(matches) != 1 or type(matches[0].get('num_requests')) is not int or matches[0]['num_requests'] < 0:
        raise Refused('dedicated router upstream census is missing or ambiguous')
    return matches[0]['num_requests']


def router_identity(settings):
    router = settings['http_release']['router']
    actual = json.loads(p.run(['docker', 'inspect', router['container']]))[0]
    mounts = actual.get('Mounts', [])
    if (actual['Image'] != router['image'] or not actual['State']['Running']
            or actual.get('HostConfig', {}).get('PortBindings')
            or set(actual.get('NetworkSettings', {}).get('Networks', {})) != {router['network']}
            or len(mounts) != 1 or mounts[0].get('Type') != 'bind'
            or mounts[0].get('Source') != router['directory']
            or mounts[0].get('Destination') != '/run/deedkit-router'
            or actual.get('Config', {}).get('Entrypoint') != ['/deedkit-release-router']
            or actual.get('Config', {}).get('Cmd') != ['--config', '/run/deedkit-router/router.json']):
        raise Refused('dedicated router identity/topology does not match its private configuration')
    router_kind(settings)
    return actual['Id']


def durable_write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x') as handle:
            os.chmod(temporary, 0o600)
            json.dump(value, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def bootstrap(settings, sha):
    validate_settings(settings)
    evidence = p.promotable(sha)
    router = settings['http_release']['router']
    receipt_path = Path(settings['state_directory']) / ('router-' + sha) / 'router-build.json'
    try:
        private_path(receipt_path.parent, directory=True)
        private_path(receipt_path)
        receipt = p.read(receipt_path)
        if (type(receipt.get('version')) is not int or receipt.get('version') != 1 or receipt.get('target') != 'http-router'
                or receipt.get('sha') != sha or receipt.get('image') != router['image']
                or receipt.get('ci', {}).get('sha') != sha):
            raise Refused('router build receipt does not match the verified image')
    except (OSError, ValueError, AttributeError) as exc:
        raise Refused('a private clean-source router build receipt is required') from exc
    image = json.loads(p.run(['docker', 'image', 'inspect', router['image']]))[0]
    labels = image.get('Config', {}).get('Labels', {})
    if (image['Id'] != router['image'] or labels.get('org.opencontainers.image.revision') != sha
            or labels.get('org.opencontainers.image.source') != 'https://github.com/' + p.REPOSITORY):
        raise Refused('bootstrap router image is not the verified main revision')
    path = Path(router['config_file'])
    if path.exists():
        raise Refused('bootstrap refuses to overwrite an existing router configuration')
    durable_write(path, route_document(settings))
    document = {'name': 'deedkit-release-router', 'services': {'router': {
        'image': router['image'], 'container_name': router['container'],
        'entrypoint': ['/deedkit-release-router'], 'command': ['--config', '/run/deedkit-router/router.json'],
        'volumes': [{'type': 'bind', 'source': router['directory'], 'target': '/run/deedkit-router'}],
        'networks': ['frontend'], 'restart': 'unless-stopped', 'read_only': True,
        'tmpfs': ['/tmp:size=16m,noexec,nosuid'], 'cap_drop': ['ALL'],
        'security_opt': ['no-new-privileges:true'],
        'logging': {'driver': 'json-file', 'options': {'max-size': '5m', 'max-file': '2'}}}},
        'networks': {'frontend': {'external': True, 'name': router['network']}}}
    compose = Path(router['directory']) / 'compose.json'
    durable_write(compose, p.escaped(document))
    return {'status': 'bootstrap-artifacts-only', 'sha': sha, 'ci': evidence, 'compose': str(compose), 'config': str(path)}


def argv(value):
    return shlex.split(value) if isinstance(value, str) else list(value or [])


def canonical(unit, role, settings):
    p.validate_runtime(unit)
    entrypoint = argv(unit.get('entrypoint'))
    command = argv(unit.get('command'))
    if role == 'api':
        if entrypoint or command not in ([], ['python', '-m', 'messaging_service', 'serve']):
            raise Refused('only canonical messaging HTTP serve can overlap')
        env = unit.get('environment') or {}
        if str(env.get('PROBE_PORT', '8080')) != '8080' or env.get('PROBE_BIND', '0.0.0.0') != '0.0.0.0':
            raise Refused('canonical messaging HTTP binding is required')
        unit['entrypoint'] = []
        unit['command'] = ['python', '-m', 'messaging_service', 'serve']
    else:
        if entrypoint not in ([], ['deedkit-gateway']):
            raise Refused('only canonical gateway entrypoint can overlap')
        expected = {'--policy', '--public-url', '--host', '--port'}
        if len(command) != 8 or set(command[::2]) != expected:
            raise Refused('canonical gateway arguments are required')
        options = dict(zip(command[::2], command[1::2], strict=True))
        if options['--public-url'].rstrip('/') != settings['http_release']['gateway_public_url'].rstrip('/') or options['--host'] != '0.0.0.0' or options['--port'] != '13240':
            raise Refused('canonical gateway public URL and HTTP binding are required')
        unit['entrypoint'] = ['deedkit-gateway']
        unit['command'] = command
    return unit


def policy_mount(unit, settings):
    """Translate the gateway's complete `--policy` path through its mounts (ANYM-424).

    Exactly one mount (volume, tmpfs, secret or config) may cover the path: a
    read-only bind of the policy file's own directory, whose host file is
    `gateway_policy`. A single-file bind pins the inode it started with, so an
    atomic rename beside the live file would never reach the running gateway.
    """
    command = dict(zip(unit['command'][::2], unit['command'][1::2], strict=True))
    target = PurePosixPath(command['--policy'])
    if not target.is_absolute() or '..' in target.parts or str(target) != command['--policy']:
        raise Refused('gateway --policy must be a normalized absolute path')
    def place(path, base='/'):
        # Docker cleans a mount destination (`..`, `.`, repeated slashes) before mounting.
        return PurePosixPath('/' + posixpath.normpath(posixpath.join('/', base, path)).lstrip('/'))
    tmpfs = unit.get('tmpfs') or []
    mounts = [(place(mount['target']), mount) for mount in unit.get('volumes', [])]
    mounts += [(place(path.split(':', 1)[0]), path) for path in ([tmpfs] if isinstance(tmpfs, str) else tmpfs)]
    # A file-backed secret or config is a bind too and can shadow the policy file.
    mounts += [(place(item.get('target') or item['source'], '/run/secrets' if kind == 'secrets' else '/'), item)
        for kind in ('secrets', 'configs') for item in unit.get(kind, []) if isinstance(item, dict)]
    covering = [(where, mount) for where, mount in mounts if where in {target, *target.parents}]
    if len(covering) != 1 or not isinstance(covering[0][1], dict):
        raise Refused('exactly one mount may provide the gateway policy')
    mounted, mount = covering[0]
    if mounted == target:
        raise Refused('gateway policy must be mounted through its directory, not as a single file')
    if mount.get('type') != 'bind' or not mount.get('read_only') or mounted != target.parent:
        raise Refused('gateway policy directory must be a read-only bind of the policy file\'s own directory')
    if PurePosixPath(mount['source']) / target.name != PurePosixPath(settings['http_release']['gateway_policy']):
        raise Refused('gateway policy must be the exact configured read-only bind mount')


def isolate(settings, documents, images, release, generation, sha):
    result = {'name': 'deedkit-http-' + generation, 'services': {}, 'networks': {}, 'volumes': {}}
    private = {'controls': {}}
    policy_bytes = Path(settings['http_release']['gateway_policy']).read_bytes()
    private['source_policy_digest'] = hashlib.sha256(policy_bytes).hexdigest()
    for role, target in [('api', 'runtime'), ('gateway', 'gateway')]:
        selection = settings['http_release'][role]
        document = documents[selection['project']]
        unit = canonical(copy.deepcopy(document['services'][selection['service']]), role, settings)
        if unit.get('network_mode') or unit.get('mac_address'):
            raise Refused('HTTP overlap requires named networks without a fixed network namespace or MAC')
        name = 'http-' + role + '-' + generation
        for key in ('build', 'ports', 'container_name', 'depends_on', 'links', 'external_links', 'hostname', 'domainname', 'profiles', 'network_mode', 'deploy', 'develop'):
            unit.pop(key, None)
        unit['image'] = images[target]
        unit.setdefault('labels', {}).update({'org.opencontainers.image.revision': sha, 'ai.deedkit.http-release': generation})
        unit['networks'] = {selection['project'] + '-' + key: {'aliases': [name]} for key in document['services'][selection['service']].get('networks', {})}
        for key in document['services'][selection['service']].get('networks', {}):
            source = document.get('networks', {}).get(key, {})
            actual = source.get('name')
            if not actual:
                raise Refused('all existing Docker networks need resolved external names')
            result['networks'][selection['project'] + '-' + key] = {'external': True, 'name': actual}
            # Keep the parser's empty default so the isolated snapshot remains
            # idempotent across Compose versions without copying managed IPAM.
            if source.get('ipam') == {}:
                result['networks'][selection['project'] + '-' + key]['ipam'] = {}
        if settings['http_release']['router']['network'] not in {value['name'] for key, value in result['networks'].items() if key in unit['networks']}:
            raise Refused('both candidates must already share the dedicated router network')
        for mount in unit.get('volumes', []):
            if mount.get('type') == 'volume':
                key = mount.get('source')
                actual = document.get('volumes', {}).get(key, {}).get('name')
                if not actual:
                    raise Refused('anonymous or unresolved volumes cannot be shared safely')
                new_key = selection['project'] + '-' + key
                result['volumes'][new_key] = {'external': True, 'name': actual}
                mount['source'] = new_key
        for kind in ('secrets', 'configs'):
            for item in unit.get(kind, []):
                key = item if isinstance(item, str) else item['source']
                source = document[kind][key]
                new_key = selection['project'] + '-' + key
                result.setdefault(kind, {})[new_key] = source
                if isinstance(item, str):
                    raise Refused('secrets/configs require explicit unchanged target paths')
                item['source'] = new_key
        if role == 'gateway':
            policy_mount(unit, settings)
        directory = Path(release) / role
        directory.mkdir(mode=0o700)
        if os.geteuid() == 0:
            os.chown(directory, 10001, 10001)
        private['controls'][role] = str(directory / 'control.sock')
        unit.setdefault('volumes', []).append({'type': 'bind', 'source': str(directory), 'target': '/run/deedkit/release'})
        environment = unit.get('environment') or {}
        if not isinstance(environment, dict):
            raise Refused('resolved Compose environment must be an object')
        if any(key.startswith('DEEDKIT_RELEASE_') for key in environment):
            raise Refused('existing release control settings cannot be overridden')
        environment.update({'DEEDKIT_RELEASE_ID': generation, 'DEEDKIT_RELEASE_SHA': sha,
                            'DEEDKIT_RELEASE_SOCKET': '/run/deedkit/release/control.sock'})
        if role == 'gateway':
            # Keep the original live policy (including revocations) mounted.
            # Only the generation-bound client's dial target is overridden.
            environment['DEEDKIT_RELEASE_MESSAGING_URL'] = 'http://http-api-' + generation + ':8080'
        unit['environment'] = environment
        result['services'][name] = unit
    return result, private


SCHEMA_READER = """import hashlib,importlib.util,json,pathlib
s=importlib.util.find_spec('messaging_service'); p=pathlib.Path(s.origin).parent
files=[p/'schema.sql',*sorted((p/'migrations').glob('*.sql'))]
print(json.dumps({str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files},sort_keys=True))
"""


def schema_inventory(image):
    value = json.loads(p.run(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--entrypoint', 'python', image, '-c', SCHEMA_READER]))
    if not isinstance(value, dict) or 'schema.sql' not in value or len(value) < 2 or any(not re.fullmatch(r'[0-9a-f]{64}', str(item)) for item in value.values()):
        raise Refused('invalid immutable image schema inventory')
    return value


def migration_version(name):
    match = re.fullmatch(r'migrations/([0-9]{3})_[^/]+\.sql', name)
    return int(match.group(1)) if match else None


def applied_prefix(old, candidate):
    """An older generation's inventory followed only by later migrations, which plan A has already applied."""
    versions = [migration_version(name) for name in old if name != 'schema.sql']
    added = [migration_version(name) for name in candidate if name not in old]
    if not added or None in versions + added or any(candidate.get(name) != sha for name, sha in old.items()):
        return False
    # Readiness keys the applied ledger by number, so every number must name one file;
    # the added migrations continue the old numbering without gaps.
    newest = max((v for v in versions if v is not None), default=0)
    if len(set(versions)) != len(versions):
        return False
    return sorted(v for v in added if v is not None) == list(range(newest + 1, newest + 1 + len(added)))


def check_schema(settings, receipt):
    baseline = {item['running'][settings['http_release']['api']['service']]['image'] for item in receipt['baseline'] if item['name'] == settings['http_release']['api']['project']}
    candidate = schema_inventory(receipt['images']['runtime'])
    if candidate != receipt['schema'] or any(schema_inventory(image) != candidate for image in baseline):
        raise Refused('messaging schema inventory mismatch; seamless promotion cannot run migrations')
    # The plan A API already runs the candidate inventory against the database. A serving
    # generation from before plan A's migrations may differ only by those later migrations;
    # the candidate's passive readiness still compares the database inventory exactly.
    previous = receipt['previous']['images']['runtime'] if receipt.get('previous') else None
    if previous and previous not in baseline:
        old = schema_inventory(previous)
        if old != candidate and not applied_prefix(old, candidate):
            raise Refused('messaging schema inventory mismatch; seamless promotion cannot run migrations')


def prepare(settings, sha):
    validate_settings(settings)
    original = p.prepare(settings, sha)
    release = Path(settings['state_directory']) / sha
    current_path = Path(settings['state_directory']) / 'http-current.json'
    previous = p.read(current_path) if current_path.exists() else None
    if previous and previous['status'] != 'serving' and previous.get('previous') is not None:
        raise Refused('finish pending retirement/recovery before preparing another generation')
    router_container = router_identity(settings)
    baseline, _ = router_get(settings)
    source = p.read(settings['http_release']['router']['config_file'])
    expected = previous['router_after'] if previous else route_document(settings)
    if baseline != expected or source != baseline:
        raise Refused('dedicated router configuration drifted from the managed baseline')
    generation = uuid.uuid4().hex[:12]
    documents = {item['name']: p.read(release / (item['name'] + '.json')) for item in original['projects']}
    # Snapshot files contain escaped dollar signs. Resolve them through Compose.
    documents = {name: json.loads(p.run(['docker', 'compose', '--profile', '*', '-f', str(release / (name + '.json')), 'config', '--format', 'json'])) for name in documents}
    document, private = isolate(settings, documents, original['images'], release, generation, sha)
    compose = release / 'http.json'
    p.write(compose, p.escaped(document))
    if json.loads(p.run(['docker', 'compose', '--profile', '*', '-f', str(compose), 'config', '--format', 'json'])) != document:
        raise Refused('isolated HTTP Compose snapshot did not round-trip exactly')
    receipt = {'version': 1, 'sha': sha, 'generation': generation, 'config_digest': p.digest(settings),
        'compose': str(compose), 'compose_digest': p.digest(p.read(compose)), 'images': original['images'],
        'private_files': {settings['http_release']['gateway_policy']: private['source_policy_digest']},
        'controls': private['controls'], 'schema': schema_inventory(original['images']['runtime']),
        'router_before': baseline, 'router_source_digest': p.digest(source), 'router_container': router_container,
        'baseline': original['projects'],
        'retained': ([previous['generation']] if previous else []), 'previous': previous, 'ci': original['ci']}
    check_schema(settings, receipt)
    p.promotable(sha)
    durable_write(release / 'http-prepared.json', receipt)
    return {'sha': sha, 'generation': generation, 'ci': original['ci'], 'images': original['images'], 'status': 'prepared'}


def validate_prepared(settings, receipt):
    if receipt['config_digest'] != p.digest(settings) or p.digest(p.read(receipt['compose'])) != receipt['compose_digest']:
        raise Refused('prepared HTTP configuration changed')
    for filename, expected in receipt['private_files'].items():
        path = Path(filename)
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise Refused('prepared private HTTP policy changed')
    for image in receipt['images'].values():
        actual = json.loads(p.run(['docker', 'image', 'inspect', image]))[0]
        labels = actual['Config'].get('Labels', {})
        if actual['Id'] != image or labels.get('org.opencontainers.image.revision') != receipt['sha'] or labels.get('org.opencontainers.image.source') != 'https://github.com/' + p.REPOSITORY:
            raise Refused('prepared HTTP image identity changed')
    for project, item in zip(settings['projects'], receipt['baseline'], strict=True):
        if p.legacy_identities(item['running']):
            raise Refused('HTTP baseline was prepared by an older production.py with order-dependent container settings; prepare again')
        document, running = p.snapshot(project)
        if item['name'] != project['name'] or p.digest(document) != item['before_digest'] or running != item['running']:
            raise Refused('existing HTTP runtime/configuration changed')
    if receipt.get('previous'):
        for role, identity in receipt['previous']['containers'].items():
            owned_container(identity, receipt['previous'], role)
    if router_identity(settings) != receipt['router_container']:
        raise Refused('dedicated router identity changed')
    live, _ = router_get(settings)
    source = p.read(settings['http_release']['router']['config_file'])
    if live != receipt['router_before'] or p.digest(source) != receipt['router_source_digest']:
        raise Refused('dedicated router configuration changed')


def candidate_command(receipt):
    return ['docker', 'compose', '--project-name', 'deedkit-http-' + receipt['generation'], '-f', receipt['compose']]


def owned_container(identity, receipt, role):
    actual = json.loads(p.run(['docker', 'inspect', identity['container']]))[0]
    labels = actual['Config'].get('Labels', {})
    if (actual.get('Id') != identity['container'] or actual['Image'] != identity['image'] or not actual['State']['Running']
            or labels.get('com.docker.compose.project') != 'deedkit-http-' + receipt['generation']
            or labels.get('com.docker.compose.service') != 'http-' + role + '-' + receipt['generation']
            or labels.get('ai.deedkit.http-release') != receipt['generation']):
        raise Refused('owned HTTP container identity changed')
    return actual


def candidate_identities(receipt):
    result = {}
    for role, target in [('api', 'runtime'), ('gateway', 'gateway')]:
        ids = p.run(candidate_command(receipt) + ['ps', '-q', 'http-' + role + '-' + receipt['generation']]).split()
        if len(ids) != 1:
            raise Refused('candidate HTTP service is missing')
        actual = json.loads(p.run(['docker', 'inspect', ids[0]]))[0]
        identity = {'container': actual['Id'], 'image': receipt['images'][target]}
        owned_container(identity, receipt, role)
        result[role] = identity
    return result


def active_candidate(receipt):
    if candidate_identities(receipt) != receipt['containers']:
        raise Refused('candidate HTTP container identity changed after activation')
    for path in receipt['controls'].values():
        value = control(path, 'status', receipt['generation'], receipt['sha'])
        if value['phase'] != 'active' or not value['ready']:
            raise Refused('candidate active HTTP readiness no longer passes')


def wait_ready(settings, receipt):
    deadline = time.monotonic() + settings['http_release'].get('ready_timeout', 120)
    while True:
        try:
            values = [control(path, 'status', receipt['generation'], receipt['sha']) for path in receipt['controls'].values()]
            if all(value['ready'] and value['phase'] == 'warming' for value in values):
                return
        except (OSError, Refused):
            pass
        if time.monotonic() >= deadline:
            raise Refused('candidate HTTP readiness did not pass; serving release is unchanged')
        time.sleep(.25)


def promote(settings, sha):
    validate_settings(settings)
    p.promotable(sha)
    release = Path(settings['state_directory']) / sha
    receipt = p.read(release / 'http-prepared.json')
    if receipt['sha'] != sha or (release / 'http-attempted.json').exists():
        raise Refused('HTTP release was already attempted or has a different revision; inspect recovery state')
    validate_prepared(settings, receipt)
    check_schema(settings, receipt)
    p.promotable(sha)  # final remote gate before the first runtime startup
    durable_write(release / 'http-attempted.json', {'status': 'starting', 'sha': sha})
    try:
        p.run(candidate_command(receipt) + ['up', '-d', '--no-build', '--pull', 'never', '--no-deps',
            'http-api-' + receipt['generation'], 'http-gateway-' + receipt['generation']])
        receipt['containers'] = candidate_identities(receipt)
        wait_ready(settings, receipt)
        for role in ['api', 'gateway']:
            value = control(receipt['controls'][role], 'activate', receipt['generation'], sha)
            if not value['ready'] or value['phase'] != 'active':
                raise Refused('candidate activation was not accepted')
        # Guard any changed image/container/router immediately before traffic admission.
        validate_prepared(settings, receipt)
        active_candidate(receipt)
        live, etag = router_get(settings)
        if live != receipt['router_before']:
            raise Refused('dedicated router changed before traffic admission')
    except Exception:
        durable_write(release / 'http-attempted.json', {'status': 'startup-failed', 'sha': sha})
        raise
    proposed = route_document(settings, receipt['generation'], receipt['retained'], baseline=receipt['router_before'])
    durable_write(release / 'router-proposed.json', proposed)
    durable_write(release / 'http-attempted.json', {'status': 'switching', 'sha': sha})
    try:
        if p.digest(p.read(settings['http_release']['router']['config_file'])) != receipt['router_source_digest']:
            raise Refused('persisted router configuration changed before traffic admission')
        p.promotable(sha)  # main contains it and its images are attested, immediately before traffic
        active_candidate(receipt)
        if router_identity(settings) != receipt['router_container']:
            raise Refused('dedicated router identity changed before traffic admission')
        router_cas(settings, proposed, etag)
        # Parent-directory bind mount makes atomic replacement visible at restart.
        durable_write(Path(settings['http_release']['router']['config_file']), proposed)
        confirmed, _ = router_get(settings)
        if confirmed != proposed:
            raise Refused('dedicated router did not retain the committed configuration')
    except Exception as exc:
        durable_write(release / 'http-attempted.json', {'status': 'recovery-required', 'sha': sha})
        raise Refused('router transaction needs recovery inspection; no blind retry or retirement') from exc
    receipt.update({'status': 'serving-retirement-pending', 'router_after': proposed})
    durable_write(release / 'http-result.json', receipt)
    durable_write(release / 'http-attempted.json', {'status': 'serving', 'sha': sha})
    durable_write(Path(settings['state_directory']) / 'http-current.json', receipt)
    return retire(settings, sha)


def drain(settings, receipt, role, *, fence=None):
    identity = receipt['containers'][role]
    owned_container(identity, receipt, role)
    deadline = time.monotonic() + settings['http_release'].get('drain_timeout', 300)
    # A proxy may already own a request which has not reached ASGI yet. Keep
    # ordinary admission active until those forwarded requests have finished.
    while router_inflight(settings, receipt['generation'], role):
        if time.monotonic() >= deadline:
            return False
        time.sleep(.25)
    value = control(receipt['controls'][role], 'drain', receipt['generation'], receipt['sha'])
    if value['phase'] != 'draining':
        raise Refused('old HTTP instance did not acknowledge draining')
    while value['active_requests'] or value['pending_tickets'] or router_inflight(settings, receipt['generation'], role):
        if time.monotonic() >= deadline:
            return False
        time.sleep(.25)
        value = control(receipt['controls'][role], 'status', receipt['generation'], receipt['sha'])
        if value['phase'] != 'draining':
            raise Refused('old HTTP instance left its draining phase')
    if role == 'gateway':
        if fence is None:
            raise Refused('gateway retirement requires an atomic router admission fence')
        fence()
        # The fence and route selection share the router's admission mutex.
        # Any request selected before the fence owns a visible lease; requests
        # selected afterward cannot enter this generation.
        while router_inflight(settings, receipt['generation'], role):
            if time.monotonic() >= deadline:
                return False
            time.sleep(.25)
        value = control(receipt['controls'][role], 'status', receipt['generation'], receipt['sha'])
        if value['phase'] != 'draining' or value['active_requests'] or value['pending_tickets']:
            return False
    owned_container(identity, receipt, role)
    # Infinite Docker stop grace: never SIGKILL a request on a guessed deadline.
    p.run(['docker', 'stop', '--time', '-1', identity['container']])
    return True


def fence_previous(settings, receipt, release):
    generation = receipt['previous']['generation']
    if router_identity(settings) != receipt['router_container']:
        raise Refused('dedicated router identity changed before predecessor admission fence')
    live, etag = router_get(settings)
    if live != receipt['router_after'] or p.read(settings['http_release']['router']['config_file']) != live:
        raise Refused('router changed before predecessor admission fence')
    if live['generations'][generation]['admit_files'] is False:
        return  # A prior bounded retirement already persisted this fence.
    proposed = copy.deepcopy(live)
    proposed['generations'][generation]['admit_files'] = False
    durable_write(release / 'http-attempted.json', {'status': 'retiring-fence', 'sha': receipt['sha']})
    try:
        actual = router_fence(settings, generation, etag)
        if actual != proposed:
            raise Refused('router did not acknowledge the predecessor admission fence')
        durable_write(Path(settings['http_release']['router']['config_file']), proposed)
        confirmed, _ = router_get(settings)
        if confirmed != proposed:
            raise Refused('router admission fence changed before persistence')
    except Exception as exc:
        durable_write(release / 'http-attempted.json', {'status': 'recovery-required', 'sha': receipt['sha']})
        raise Refused('router admission fence needs recovery inspection') from exc
    receipt['router_after'] = proposed
    durable_write(release / 'http-result.json', receipt)
    durable_write(Path(settings['state_directory']) / 'http-current.json', receipt)
    durable_write(release / 'http-attempted.json', {'status': 'serving', 'sha': receipt['sha']})


def retire(settings, sha):
    validate_settings(settings)
    release = Path(settings['state_directory']) / sha
    attempted = p.read(release / 'http-attempted.json')
    if attempted.get('status') != 'serving':
        raise Refused('HTTP release needs recovery inspection before retirement')
    receipt = p.read(release / 'http-result.json')
    if receipt['config_digest'] != p.digest(settings):
        raise Refused('HTTP retirement configuration changed')
    current = p.read(Path(settings['state_directory']) / 'http-current.json')
    if current['generation'] != receipt['generation'] or current['sha'] != sha:
        raise Refused('only the currently serving generation can retire its predecessor')
    if router_identity(settings) != receipt['router_container']:
        raise Refused('dedicated router identity changed before retirement')
    live, _ = router_get(settings)
    if live != receipt['router_after'] or p.read(settings['http_release']['router']['config_file']) != live:
        raise Refused('router changed; predecessor retirement is unsafe')
    for role, identity in receipt['containers'].items():
        owned_container(identity, receipt, role)
    if receipt['status'] == 'serving':
        return public_status(receipt)
    previous = receipt.get('previous')
    if not previous:
        receipt['retirement_reason'] = 'legacy instances have no trusted drain protocol; retained with legacy file route'
    else:
        # Issuer gateway must retire before its paired API: old downloads rely on
        # process-local API bindings even after a backend GET has completed.
        progress = receipt.setdefault('retired_roles', [])
        for role in ['gateway', 'api']:
            if role in progress:
                continue
            if not drain(settings, previous, role,
                         fence=(lambda: fence_previous(settings, receipt, release)) if role == 'gateway' else None):
                receipt['retirement_reason'] = role + ' still has admitted requests or unexpired tickets'
                break
            progress.append(role)
            durable_write(release / 'http-result.json', receipt)
        if progress == ['gateway', 'api']:
            receipt['status'] = 'serving'
            receipt['retirement_reason'] = 'managed predecessor drained and stopped; legacy pair remains retained'
            # The persisted fence rejects all later requests for this issuer.
    durable_write(release / 'http-result.json', receipt)
    durable_write(Path(settings['state_directory']) / 'http-current.json', receipt)
    return public_status(receipt)


def public_status(receipt):
    return {key: receipt[key] for key in ['sha', 'generation', 'status', 'images', 'ci', 'retirement_reason'] if key in receipt}


def status(settings, sha=None):
    path = Path(settings['state_directory']) / sha / 'http-attempted.json' if sha else Path(settings['state_directory']) / 'http-current.json'
    if not path.exists():
        return {'status': 'no managed HTTP release'}
    value = p.read(path)
    return public_status(value) if 'generation' in value else value


@contextmanager
def locks(settings):
    with p.lock(GLOBAL_LOCK):
        scoped = Path(settings['state_directory'])
        if scoped.resolve() == GLOBAL_LOCK.resolve():
            yield
        else:
            with p.lock(scoped):
                yield


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['bootstrap', 'prepare', 'promote', 'retire', 'status'])
    parser.add_argument('--sha')
    parser.add_argument('--config', default='/etc/deedkit/http-production.json')
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if os.geteuid() != 0:
            raise Refused('the local production operator must run as root')
        settings = validate_settings(p.config(args.config))
        if args.action in {'bootstrap', 'prepare', 'promote', 'retire'} and not re.fullmatch(r'[0-9a-f]{40}', args.sha or ''):
            raise Refused('an exact lowercase 40-character main SHA is required')
        with locks(settings):
            result = {'bootstrap': lambda: bootstrap(settings, args.sha), 'prepare': lambda: prepare(settings, args.sha),
                'promote': lambda: promote(settings, args.sha), 'retire': lambda: retire(settings, args.sha),
                'status': lambda: status(settings, args.sha)}[args.action]()
        print(json.dumps(result, indent=2))
    except Exception as exc:
        parser.exit(1, 'HTTP release refused: ' + (str(exc) if isinstance(exc, Refused) else type(exc).__name__) + '\n')


if __name__ == '__main__':
    main()
