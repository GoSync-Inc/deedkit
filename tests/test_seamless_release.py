"""Synthetic release proof: never connects to a provider or production database."""
import copy
import json
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import tempfile
from types import SimpleNamespace

import pytest

from scripts import seamless_release as s

SHA = 'a' * 40
IMAGE = 'sha256:' + 'b' * 64
OLD = 'sha256:' + 'c' * 64
GEN = '123456abcdef'
ROUTER_ID = 'e' * 64


def settings(tmp_path):
    router = tmp_path / 'router'
    router.mkdir(mode=0o700)
    (tmp_path / 'gateway-policy').mkdir(mode=0o700)
    policy = tmp_path / 'gateway-policy' / 'policy.json'
    policy.write_text(json.dumps({'revision': 1, 'keys': {'secret': 'synthetic'}, 'deployment': {'backends': {
        'messaging': {'url': 'https://example.invalid/pos-probe', 'download_origin': 'https://example.invalid/pos-probe'},
        'calendar': {'url': 'http://calendar-api:8080'}}}}))
    policy.chmod(0o600)
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    config = {'version': 1, 'state_directory': str(state), 'projects': [
        {'name': 'messages', 'directory': str(tmp_path), 'files': [str(policy)], 'services': {'api': 'runtime'}},
        {'name': 'gateway', 'directory': str(tmp_path), 'files': [str(policy)], 'services': {'gateway': 'gateway'}}],
        'http_release': {'api': {'project': 'messages', 'service': 'api'},
        'gateway': {'project': 'gateway', 'service': 'gateway'}, 'gateway_policy': str(policy),
        'gateway_public_url': 'https://example.invalid/deedkit', 'drain_timeout': 0,
        'router': {'container': 'release-router', 'image': IMAGE, 'network': 'frontend',
                   'directory': str(router), 'config_file': str(router / 'router.json'),
                   'admin_socket': str(router / 'admin.sock'), 'listen': 8080},
        'legacy': {'api': 'legacy-api:8080', 'gateway': 'legacy-gateway:13240'}}}
    return config


def status(*, phase='warming', ready=True, active=0, tickets=0):
    return {'protocol': 1, 'generation': GEN, 'sha': SHA, 'phase': phase,
            'ready': ready, 'active_requests': active, 'pending_tickets': tickets}


def router_build_receipt(cfg):
    directory = Path(cfg['state_directory']) / ('router-'+SHA)
    directory.mkdir(mode=0o700)
    path = directory / 'router-build.json'
    s.p.write(path, {'version': 1, 'target': 'http-router', 'sha': SHA, 'image': IMAGE, 'ci': {'sha': SHA}})
    return path


@pytest.mark.parametrize('field,bad', [('protocol', True), ('generation', 'other'), ('sha', 'b'*40),
    ('phase', 'unknown'), ('ready', 1), ('active_requests', True), ('active_requests', -1),
    ('pending_tickets', 1.5)])
def test_operator_rejects_ambiguous_or_foreign_control_status(field, bad):
    value = status()
    value[field] = bad
    with pytest.raises(s.Refused):
        s.validate_control(value, GEN, SHA)


def test_router_pins_ticket_issuers_and_switches_only_general_http(tmp_path):
    cfg = settings(tmp_path)
    document = s.route_document(cfg, GEN, ['fedcba654321'])
    text = json.dumps(document)
    assert document['active'] == GEN
    assert set(document['generations']) == {GEN, 'fedcba654321'}
    assert all(value['admit_files'] is True for value in document['generations'].values())
    assert 'legacy-gateway:13240' in text
    assert 'http-gateway-123456abcdef:13240' in text
    assert 'http-api-123456abcdef:8080' in text
    assert 'pos-voice' not in text and 'calendar-api' not in text
    assert document['admin_socket'] == '/run/deedkit-router/admin.sock'
    assert document['listen'] == ':8080' and document['version'] == 1
    assert document['legacy'] == cfg['http_release']['legacy']


def test_only_the_two_canonical_http_services_are_allowed(tmp_path):
    cfg = settings(tmp_path)
    assert s.validate_settings(cfg) is cfg
    cfg['projects'][0]['services']['telegram'] = 'runtime'
    with pytest.raises(s.Refused, match='two'):
        s.validate_settings(cfg)


def documents(cfg):
    return {'messages': {'name': 'messages', 'services': {'api': {
        'image': OLD, 'command': ['python', '-m', 'messaging_service', 'serve'],
        'environment': {'PROBE_PORT': '8080', 'PROBE_BIND': '0.0.0.0'},
        'ports': [{'target': 8080, 'published': '18940'}], 'container_name': 'old-api',
        'depends_on': {'db': {'condition': 'service_healthy'}},
        'volumes': [{'type': 'volume', 'source': 'files', 'target': '/data', 'read_only': True}],
        'networks': {'backend': {}, 'frontend': {'aliases': ['api', 'old-api']}}}},
        'networks': {'backend': {'name': 'messages_backend'}, 'frontend': {'name': 'frontend'}},
        'volumes': {'files': {'name': 'messages_files'}}},
        'gateway': {'name': 'gateway', 'services': {'gateway': {
        'image': OLD, 'command': ['--policy', '/run/deedkit/policy/policy.json', '--public-url', cfg['http_release']['gateway_public_url'], '--host', '0.0.0.0', '--port', '13240'],
        'volumes': [{'type': 'bind', 'source': str(Path(cfg['http_release']['gateway_policy']).parent), 'target': '/run/deedkit/policy', 'read_only': True}],
        'networks': {'integration': {}, 'frontend': {'aliases': ['deedkit-gateway']}}}},
        'networks': {'integration': {'name': 'integration'}, 'frontend': {'name': 'frontend'}}}}


def test_candidate_has_unique_aliases_no_ports_and_reuses_external_state(tmp_path):
    cfg = settings(tmp_path)
    release = Path(cfg['state_directory']) / SHA
    release.mkdir()
    doc, private = s.isolate(cfg, documents(cfg), {'runtime': IMAGE, 'gateway': IMAGE}, release, GEN, SHA)
    assert set(doc['services']) == {'http-api-'+GEN, 'http-gateway-'+GEN}
    for name, unit in doc['services'].items():
        assert not unit.get('ports') and 'container_name' not in unit and 'depends_on' not in unit
        assert all(net['aliases'] == [name] for net in unit['networks'].values())
        assert unit['environment']['DEEDKIT_RELEASE_ID'] == GEN
        assert unit['image'] == IMAGE
    assert doc['volumes']['messages-files'] == {'external': True, 'name': 'messages_files'}
    assert doc['networks']['messages-backend'] == {'external': True, 'name': 'messages_backend'}
    gateway = doc['services']['http-gateway-'+GEN]
    mount = next(value for value in gateway['volumes'] if value['target'] == '/run/deedkit/policy')
    assert mount['source'] == str(Path(cfg['http_release']['gateway_policy']).parent) and mount['read_only'] is True
    assert gateway['environment']['DEEDKIT_RELEASE_MESSAGING_URL'] == 'http://http-api-'+GEN+':8080'
    assert 'policy' not in private


def test_isolation_preserves_empty_resolved_network_ipam_metadata(tmp_path):
    cfg = settings(tmp_path)
    source = documents(cfg)
    for project in source.values():
        for network in project['networks'].values():
            network['ipam'] = {}
    before = copy.deepcopy(source)

    isolated, _ = s.isolate(cfg, source, {'runtime': IMAGE, 'gateway': IMAGE}, tmp_path, GEN, SHA)

    assert source == before
    for project, document in source.items():
        for key, network in document['networks'].items():
            assert isolated['networks'][project + '-' + key] == {
                'external': True, 'name': network['name'], 'ipam': {}}


def test_synthetic_candidate_roundtrips_through_installed_compose(tmp_path):
    docker = shutil.which('docker')
    if not docker:
        pytest.skip('Docker Compose CLI is needed for the native config parser regression')
    version = subprocess.run([docker, 'compose', 'version'], capture_output=True, timeout=15)
    if version.returncode:
        pytest.skip('Docker Compose plugin is not installed')
    cfg = settings(tmp_path)

    def resolve(document, name):
        path = tmp_path / (name + '.json')
        path.write_text(json.dumps(s.p.escaped(document)))
        result = subprocess.run([docker, 'compose', '--profile', '*', '-f', str(path),
                                 'config', '--format', 'json'],
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    source = documents(cfg)
    source['messages']['services']['api'].pop('depends_on')
    source = {name: resolve(document, 'source-' + name) for name, document in source.items()}
    isolated, _ = s.isolate(cfg, source, {'runtime': IMAGE, 'gateway': IMAGE}, tmp_path, GEN, SHA)

    assert resolve(isolated, 'isolated') == isolated
    assert all(network['external'] is True for network in isolated['networks'].values())
    assert all(not unit.get('ports') and 'depends_on' not in unit
               for unit in isolated['services'].values())


def test_candidate_rejects_collector_command_even_if_target_is_runtime(tmp_path):
    cfg = settings(tmp_path)
    docs = documents(cfg)
    docs['messages']['services']['api']['command'] = ['python', '-m', 'messaging_service.telegram_collector']
    with pytest.raises(s.Refused, match='canonical'):
        s.isolate(cfg, docs, {'runtime': IMAGE, 'gateway': IMAGE}, tmp_path, GEN, SHA)


def test_root_preparation_makes_each_private_control_directory_runtime_owned(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    release = Path(cfg['state_directory']) / SHA
    release.mkdir()
    ownership = []
    monkeypatch.setattr(s.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(s.os, 'chown', lambda path, uid, gid: ownership.append((Path(path), uid, gid)))

    document, private = s.isolate(cfg, documents(cfg), {'runtime': IMAGE, 'gateway': IMAGE}, release, GEN, SHA)

    assert ownership == [(release / role, 10001, 10001) for role in ('api', 'gateway')]
    for role in ('api', 'gateway'):
        directory = release / role
        assert directory.is_dir() and directory.stat().st_mode & 0o777 == 0o700
        assert private['controls'][role] == str(directory / 'control.sock')
        assert {'type': 'bind', 'source': str(directory), 'target': '/run/deedkit/release'} in document['services']['http-' + role + '-' + GEN]['volumes']


def test_root_preparation_aborts_when_private_control_ownership_cannot_be_set(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    release = Path(cfg['state_directory']) / SHA
    release.mkdir()
    monkeypatch.setattr(s.os, 'geteuid', lambda: 0)
    def denied(*_args):
        raise PermissionError('synthetic ownership denied')
    monkeypatch.setattr(s.os, 'chown', denied)

    with pytest.raises(PermissionError, match='synthetic ownership denied'):
        s.isolate(cfg, documents(cfg), {'runtime': IMAGE, 'gateway': IMAGE}, release, GEN, SHA)

    assert not (release / 'gateway').exists()
    assert not (release / 'http.json').exists()


def fixture(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    release = Path(cfg['state_directory']) / SHA
    release.mkdir()
    path = release / 'http.json'
    path.write_text('{}')
    live = s.route_document(cfg)
    Path(cfg['http_release']['router']['config_file']).write_text(json.dumps(live))
    calls = []
    receipt = {'version': 1, 'sha': SHA, 'generation': GEN, 'config_digest': s.p.digest(cfg),
        'compose': str(path), 'compose_digest': s.p.digest({}), 'private_files': {}, 'images': {'runtime': IMAGE, 'gateway': IMAGE},
        'schema': {'schema': 'same'}, 'router_before': live, 'router_source_digest': s.p.digest(live), 'router_container': ROUTER_ID,
        'retained': [], 'previous': None, 'ci': {'sha': SHA}, 'baseline': [],
        'controls': {name: str(release / name / 'control.sock') for name in ['api', 'gateway']}}
    s.p.write(release / 'http-prepared.json', receipt)
    monkeypatch.setattr(s.p, 'promotable', lambda sha: calls.append('verify') or {'sha': sha})
    monkeypatch.setattr(s, 'validate_prepared', lambda *a: calls.append('validate'))
    monkeypatch.setattr(s, 'check_schema', lambda *a: calls.append('schema'))
    monkeypatch.setattr(s, 'router_identity', lambda *a: ROUTER_ID)
    monkeypatch.setattr(s, 'router_inflight', lambda *a: 0)
    monkeypatch.setattr(s, 'router_get', lambda *a: (copy.deepcopy(live), '"etag"'))
    def commit(_settings, value, _etag):
        calls.append('switch')
        live.clear()
        live.update(copy.deepcopy(value))
    monkeypatch.setattr(s, 'router_cas', commit)
    def fence(_settings, generation, _etag):
        calls.append(('fence', generation))
        live['generations'][generation]['admit_files'] = False
        return copy.deepcopy(live)
    monkeypatch.setattr(s, 'router_fence', fence)
    monkeypatch.setattr(s, 'owned_container', lambda *a: None)
    monkeypatch.setattr(s, 'candidate_identities', lambda *a: {'api': {'container': 'new-api', 'image': IMAGE}, 'gateway': {'container': 'new-gateway', 'image': IMAGE}})
    monkeypatch.setattr(s.p, 'run', lambda argv, **kw: calls.append(argv) or '')
    phases = {}
    def reply(path, action, generation, sha):
        calls.append(action)
        if action == 'activate':
            phases[path] = 'active'
        return status(phase=phases.get(path, 'warming'))
    monkeypatch.setattr(s, 'control', reply)
    monkeypatch.setattr(s, 'wait_ready', lambda *a: calls.append('ready'))
    return cfg, release, calls, receipt


def test_ci_is_rechecked_before_startup_and_again_before_traffic(tmp_path, monkeypatch):
    cfg, release, calls, _ = fixture(tmp_path, monkeypatch)
    result = s.promote(cfg, SHA)
    start = next(i for i, c in enumerate(calls) if isinstance(c, list) and 'up' in c)
    switch = calls.index('switch')
    assert calls[start-1] == 'verify'
    last_gate = max(i for i, value in enumerate(calls[:switch]) if value == 'verify')
    assert calls[last_gate+1:switch] == ['status', 'status']
    assert calls.index('ready') < switch
    assert result['status'] == 'serving-retirement-pending'
    assert not any(isinstance(c, list) and 'stop' in c for c in calls)
    assert not any(isinstance(c, list) and any('telegram' in str(v) for v in c) for c in calls)


def test_unready_candidate_never_receives_traffic_or_stops_old(tmp_path, monkeypatch):
    cfg, release, calls, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(s, 'wait_ready', lambda *a: (_ for _ in ()).throw(s.Refused('not ready')))
    with pytest.raises(s.Refused, match='not ready'):
        s.promote(cfg, SHA)
    assert 'switch' not in calls
    assert not any(isinstance(c, list) and 'stop' in c for c in calls)
    assert s.p.read(release / 'http-attempted.json')['status'] == 'startup-failed'


def test_schema_mismatch_is_blocked_before_any_container_start(tmp_path, monkeypatch):
    cfg, release, calls, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(s, 'check_schema', lambda *a: (_ for _ in ()).throw(s.Refused('schema mismatch')))
    with pytest.raises(s.Refused, match='schema mismatch'):
        s.promote(cfg, SHA)
    assert not any(isinstance(c, list) and 'up' in c for c in calls)


def test_unknown_router_outcome_refuses_blind_retry_and_retirement(tmp_path, monkeypatch):
    cfg, release, calls, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(s, 'router_cas', lambda *a: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(s.Refused, match='recovery'):
        s.promote(cfg, SHA)
    assert s.p.read(release / 'http-attempted.json')['status'] == 'recovery-required'
    with pytest.raises(s.Refused):
        s.promote(cfg, SHA)
    with pytest.raises(s.Refused):
        s.retire(cfg, SHA)
    assert not any(isinstance(c, list) and 'stop' in c for c in calls)


def test_drain_timeout_retains_both_owned_old_instances(tmp_path, monkeypatch):
    cfg, release, calls, receipt = fixture(tmp_path, monkeypatch)
    old = copy.deepcopy(receipt)
    old['generation'] = 'fedcba654321'
    old['containers'] = {'api': {'container': 'old-api', 'image': OLD}, 'gateway': {'container': 'old-gateway', 'image': OLD}}
    receipt['previous'] = old
    receipt['status'] = 'serving-retirement-pending'
    receipt['router_after'] = receipt['router_before']
    receipt['containers'] = {'api': {'container': 'new-api', 'image': IMAGE}, 'gateway': {'container': 'new-gateway', 'image': IMAGE}}
    s.p.write(Path(cfg['state_directory']) / 'http-current.json', receipt)
    s.p.write(release / 'http-result.json', receipt)
    s.p.write(release / 'http-attempted.json', {'status': 'serving'})
    monkeypatch.setattr(s, 'owned_container', lambda *a: None)
    monkeypatch.setattr(s, 'control', lambda *a: status(phase='draining', active=1, tickets=1))
    result = s.retire(cfg, SHA)
    assert result['status'] == 'serving-retirement-pending'
    assert not any(isinstance(c, list) and 'stop' in c for c in calls)


def test_prepared_file_and_router_drift_are_refused(tmp_path, monkeypatch):
    cfg, release, calls, receipt = fixture(tmp_path, monkeypatch)
    monkeypatch.undo()
    Path(receipt['compose']).write_text('{"changed":true}')
    with pytest.raises(s.Refused, match='changed'):
        s.validate_prepared(cfg, receipt)


def test_local_unix_control_uses_bounded_json_protocol(tmp_path):
    directory = tempfile.TemporaryDirectory(prefix='dk-', dir='/tmp')
    path = Path(directory.name) / 'c.sock'
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(1)
    server.settimeout(3)
    def reply():
        connection, _ = server.accept()
        with connection:
            assert json.loads(connection.recv(1024)) == {'action': 'status'}
            connection.sendall((json.dumps(status())+'\n').encode())
        server.close()
    thread = threading.Thread(target=reply)
    thread.start()
    try:
        assert s.control(str(path), 'status', GEN, SHA)['ready'] is True
    finally:
        server.close()
        thread.join(3)
    assert not thread.is_alive()
    directory.cleanup()


def test_ci_failure_before_start_never_mutates_runtime(tmp_path, monkeypatch):
    cfg, release, calls, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(s.p, 'promotable', lambda sha: (_ for _ in ()).throw(s.Refused('CI failed')))
    with pytest.raises(s.Refused, match='CI failed'):
        s.promote(cfg, SHA)
    assert not calls
    assert not (release / 'http-attempted.json').exists()


def test_drain_rechecks_ticket_and_stream_counts_then_retires_gateway_before_api(tmp_path, monkeypatch):
    cfg, release, calls, receipt = fixture(tmp_path, monkeypatch)
    cfg['http_release']['drain_timeout'] = 10
    receipt['config_digest'] = s.p.digest(cfg)
    old = copy.deepcopy(receipt)
    old['generation'] = 'fedcba654321'
    old['containers'] = {'api': {'container': 'old-api', 'image': OLD}, 'gateway': {'container': 'old-gateway', 'image': OLD}}
    live = receipt['router_before']
    live.update(s.route_document(cfg, GEN, [old['generation']]))
    s.p.write(Path(cfg['http_release']['router']['config_file']), live)
    receipt.update({'previous': old, 'status': 'serving-retirement-pending', 'router_after': copy.deepcopy(live),
        'containers': {'api': {'container': 'new-api', 'image': IMAGE}, 'gateway': {'container': 'new-gateway', 'image': IMAGE}}})
    s.p.write(release / 'http-result.json', receipt)
    s.p.write(release / 'http-attempted.json', {'status': 'serving'})
    s.p.write(Path(cfg['state_directory']) / 'http-current.json', receipt)
    transitions = iter([(2, 1), (1, 0), (0, 0), (0, 0), (1, 0), (0, 0)])
    def response(path, action, generation, sha):
        active, tickets = next(transitions)
        calls.append(('control', 'gateway' if path.endswith('gateway/control.sock') else 'api', action, active, tickets))
        return {**status(phase='draining', active=active, tickets=tickets), 'generation': generation}
    monkeypatch.setattr(s, 'control', response)
    monkeypatch.setattr(s.time, 'sleep', lambda _: None)
    result = s.retire(cfg, SHA)
    assert result['status'] == 'serving'
    stops = [value for value in calls if isinstance(value, list) and 'stop' in value]
    assert stops == [['docker', 'stop', '--time', '-1', 'old-gateway'], ['docker', 'stop', '--time', '-1', 'old-api']]
    gw_stop = calls.index(stops[0])
    api_drain = next(i for i, value in enumerate(calls) if isinstance(value, tuple) and value[1] == 'api')
    assert gw_stop < api_drain
    fence = calls.index(('fence', old['generation']))
    assert fence < gw_stop
    persisted = s.p.read(cfg['http_release']['router']['config_file'])
    assert persisted['generations'][old['generation']]['admit_files'] is False
    assert not any('kill' in value or 'down' in value for value in stops)


def test_retirement_refuses_if_router_returns_to_predecessor(tmp_path, monkeypatch):
    cfg, release, calls, receipt = fixture(tmp_path, monkeypatch)
    receipt.update({'status': 'serving-retirement-pending', 'router_after': s.route_document(cfg, GEN),
        'containers': {'api': {'container': 'new-api', 'image': IMAGE}, 'gateway': {'container': 'new-gateway', 'image': IMAGE}}})
    s.p.write(release / 'http-result.json', receipt)
    s.p.write(release / 'http-attempted.json', {'status': 'serving'})
    s.p.write(Path(cfg['state_directory']) / 'http-current.json', receipt)
    with pytest.raises(s.Refused, match='router changed'):
        s.retire(cfg, SHA)
    assert not any(isinstance(value, list) and 'stop' in value for value in calls)


def test_schema_reader_is_isolated_and_compares_every_migration(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    commands = []
    inventory = {'schema.sql': '1'*64, 'migrations/002_archive.sql': '2'*64}
    def run(command):
        commands.append(command)
        return json.dumps(inventory if command[-3] == IMAGE else {**inventory, 'migrations/003_extra.sql': '3'*64})
    monkeypatch.setattr(s.p, 'run', run)
    receipt = {'images': {'runtime': IMAGE}, 'schema': inventory, 'baseline': [{'name': 'messages', 'running': {'api': {'image': OLD}}}], 'previous': None}
    with pytest.raises(s.Refused, match='schema inventory mismatch'):
        s.check_schema(cfg, receipt)
    assert len(commands) == 2
    for command in commands:
        assert ['--network', 'none'] == command[command.index('--network'):command.index('--network')+2]
        assert '--read-only' in command and '--rm' in command and '--entrypoint' in command
        assert '--volume' not in command and '-v' not in command
        assert 'migrate(' not in command[-1] and 'Store(' not in command[-1]


def test_router_identity_cannot_target_shared_ingress(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    value = {'Id': 'router-id', 'Image': IMAGE, 'State': {'Running': True},
        'HostConfig': {'PortBindings': {}}, 'NetworkSettings': {'Networks': {'frontend': {}}},
        'Mounts': [{'Type': 'bind', 'Source': cfg['http_release']['router']['directory'], 'Destination': '/run/deedkit-router'}],
        'Config': {'Entrypoint': ['/deedkit-release-router'], 'Cmd': ['--config', '/run/deedkit-router/router.json']}}
    monkeypatch.setattr(s.p, 'run', lambda _: json.dumps([value]))
    monkeypatch.setattr(s, 'router_kind', lambda _: None)
    assert s.router_identity(cfg) == 'router-id'
    value['HostConfig']['PortBindings'] = {'443/tcp': [{'HostPort': '443'}]}
    with pytest.raises(s.Refused, match='dedicated router identity'):
        s.router_identity(cfg)


def test_tampered_image_and_manual_router_edits_are_blocked(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    compose = tmp_path / 'isolated.json'
    compose.write_text('{}')
    router = s.route_document(cfg)
    Path(cfg['http_release']['router']['config_file']).write_text(json.dumps(router))
    receipt = {'sha': SHA, 'config_digest': s.p.digest(cfg), 'compose': str(compose), 'compose_digest': s.p.digest({}),
        'private_files': {}, 'images': {'runtime': IMAGE}, 'baseline': [], 'router_before': router,
        'router_source_digest': s.p.digest(router), 'router_container': ROUTER_ID}
    cfg['projects'] = []
    receipt['config_digest'] = s.p.digest(cfg)
    actual = {'Id': IMAGE, 'Config': {'Labels': {'org.opencontainers.image.revision': SHA,
        'org.opencontainers.image.source': 'https://github.com/'+s.p.REPOSITORY}}}
    monkeypatch.setattr(s.p, 'run', lambda _: json.dumps([actual]))
    monkeypatch.setattr(s, 'router_identity', lambda _: ROUTER_ID)
    monkeypatch.setattr(s, 'router_get', lambda _: (router, '"etag"'))
    s.validate_prepared(cfg, receipt)
    actual['Config']['Labels']['org.opencontainers.image.revision'] = 'c'*40
    with pytest.raises(s.Refused, match='image identity changed'):
        s.validate_prepared(cfg, receipt)
    actual['Config']['Labels']['org.opencontainers.image.revision'] = SHA
    Path(cfg['http_release']['router']['config_file']).write_text('{"manual":true}')
    with pytest.raises(s.Refused, match='router configuration changed'):
        s.validate_prepared(cfg, receipt)


@pytest.mark.parametrize('mutation', ['bind-policy', 'unknown-entrypoint', 'loader-env'])
def test_unreviewed_candidate_runtime_settings_are_refused(tmp_path, mutation):
    cfg = settings(tmp_path)
    docs = documents(cfg)
    gateway = docs['gateway']['services']['gateway']
    if mutation == 'bind-policy':
        gateway['volumes'][0]['source'] = '/unreviewed-policy.json'
    elif mutation == 'unknown-entrypoint':
        gateway['entrypoint'] = ['python', '-m', 'other']
    else:
        gateway['environment'] = {'PYTHONPATH': '/data/extensions'}
    with pytest.raises(s.Refused):
        s.isolate(cfg, docs, {'runtime': IMAGE, 'gateway': IMAGE}, tmp_path, GEN, SHA)


# spec: ANYM-424
@pytest.mark.parametrize('mutation', ['single-file', 'writable', 'other-directory', 'other-filename',
    'shadowing-file', 'shadowing-tmpfs', 'shadowing-secret', 'shadowing-config', 'dotted-file', 'dotted-secret',
    'dotted-config', 'parent-directory', 'volume', 'unnormalized'])
def test_gateway_policy_must_be_the_read_only_bind_of_its_own_directory(tmp_path, mutation):
    cfg = settings(tmp_path)
    docs = documents(cfg)
    gateway = docs['gateway']['services']['gateway']
    mount, policy = gateway['volumes'][0], cfg['http_release']['gateway_policy']
    if mutation == 'single-file':
        mount.update(source=policy, target='/run/deedkit/policy/policy.json')
    elif mutation == 'writable':
        mount['read_only'] = False
    elif mutation == 'other-directory':
        mount['source'] = str(tmp_path / 'other-policy')
    elif mutation == 'other-filename':
        gateway['command'][1] = '/run/deedkit/policy/staged.json'
    elif mutation == 'shadowing-file':
        gateway['volumes'].append({'type': 'bind', 'source': policy, 'target': '/run/deedkit/policy/policy.json', 'read_only': True})
    elif mutation == 'shadowing-tmpfs':
        gateway['tmpfs'] = ['/run/deedkit:size=1m']
    elif mutation == 'dotted-file':
        gateway['volumes'].append({'type': 'bind', 'source': policy, 'target': '/run/deedkit/policy/../policy//policy.json', 'read_only': True})
    elif mutation in ('shadowing-secret', 'shadowing-config', 'dotted-secret', 'dotted-config'):
        kind = mutation.split('-')[1] + 's'
        docs['gateway'][kind] = {'stale': {'file': policy}}
        path = '/run/deedkit/policy/../policy/./policy.json' if mutation.startswith('dotted') else '/run/deedkit/policy/policy.json'
        gateway[kind] = [{'source': 'stale', 'target': path}]
    elif mutation == 'parent-directory':
        mount.update(source=str(tmp_path), target='/run/deedkit/private')
        gateway['command'][1] = '/run/deedkit/private/gateway-policy/policy.json'
    elif mutation == 'volume':
        mount.update(type='volume', source='policy')
        docs['gateway']['volumes'] = {'policy': {'name': 'gateway_policy'}}
    else:
        gateway['command'][1] = '/run/deedkit/other/../policy/policy.json'
    with pytest.raises(s.Refused, match='policy'):
        s.isolate(cfg, docs, {'runtime': IMAGE, 'gateway': IMAGE}, tmp_path, GEN, SHA)


# spec: ANYM-424
def test_gateway_policy_directory_must_be_private(tmp_path):
    cfg = settings(tmp_path)
    Path(cfg['http_release']['gateway_policy']).parent.chmod(0o750)
    with pytest.raises(s.Refused, match='private'):
        s.validate_settings(cfg)


def test_bootstrap_writes_only_pinned_private_router_artifacts(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    router_build_receipt(cfg)
    calls = []
    monkeypatch.setattr(s.p, 'promotable', lambda sha: calls.append(('verify', sha)) or {'sha': sha})
    def inspect(command):
        calls.append(command)
        assert command == ['docker', 'image', 'inspect', IMAGE]
        return json.dumps([{'Id': IMAGE, 'Config': {'Labels': {
            'org.opencontainers.image.revision': SHA,
            'org.opencontainers.image.source': 'https://github.com/'+s.p.REPOSITORY}}}])
    monkeypatch.setattr(s.p, 'run', inspect)
    result = s.bootstrap(cfg, SHA)
    assert result['status'] == 'bootstrap-artifacts-only'
    document = s.p.read(result['compose'])
    router = document['services']['router']
    assert router['image'] == IMAGE
    assert router['cap_drop'] == ['ALL'] and 'cap_add' not in router
    assert router['entrypoint'] == ['/deedkit-release-router']
    assert router['command'] == ['--config', '/run/deedkit-router/router.json']
    assert not router.get('ports')
    assert router['volumes'][0]['source'] == cfg['http_release']['router']['directory']
    assert router['volumes'][0]['target'] == '/run/deedkit-router'
    assert Path(result['config']).stat().st_mode & 0o077 == 0
    with pytest.raises(s.Refused, match='overwrite'):
        s.bootstrap(cfg, SHA)
    assert calls[0] == ('verify', SHA)


@pytest.mark.parametrize('failure', ['ci', 'revision', 'source', 'image-id'])
def test_bootstrap_rejects_unverified_source_before_writing_artifacts(tmp_path, monkeypatch, failure):
    cfg = settings(tmp_path)
    router_build_receipt(cfg)
    def verify(sha):
        if failure == 'ci':
            raise s.Refused('CI failed')
        return {'sha': sha}
    monkeypatch.setattr(s.p, 'promotable', verify)
    image = {'Id': OLD if failure == 'image-id' else IMAGE, 'Config': {'Labels': {
        'org.opencontainers.image.revision': 'd'*40 if failure == 'revision' else SHA,
        'org.opencontainers.image.source': 'https://untrusted.invalid/code' if failure == 'source' else 'https://github.com/'+s.p.REPOSITORY}}}
    monkeypatch.setattr(s.p, 'run', lambda _: json.dumps([image]))
    with pytest.raises(s.Refused):
        s.bootstrap(cfg, SHA)
    assert list(Path(cfg['http_release']['router']['directory']).iterdir()) == []


def test_prepare_preserves_legacy_and_allows_next_managed_generation(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    release = Path(cfg['state_directory']) / SHA
    release.mkdir()
    docs = documents(cfg)
    baseline = []
    for project in cfg['projects']:
        name = project['name']
        s.p.write(release / (name+'.json'), s.p.escaped(docs[name]))
        baseline.append({'name': name, 'before_digest': s.p.digest(docs[name]), 'running': {next(iter(project['services'])): {'container': 'legacy-'+name, 'image': OLD}}})
    previous_gen = 'fedcba654321'
    live = s.route_document(cfg, previous_gen)
    previous = {'status': 'serving-retirement-pending', 'generation': previous_gen, 'previous': None,
        'router_after': live, 'images': {'runtime': OLD}}
    s.p.write(Path(cfg['state_directory']) / 'http-current.json', previous)
    s.p.write(Path(cfg['http_release']['router']['config_file']), live)
    monkeypatch.setattr(s.p, 'prepare', lambda *a: {'images': {'runtime': IMAGE, 'gateway': IMAGE}, 'projects': baseline, 'ci': {'sha': SHA}})
    monkeypatch.setattr(s, 'router_identity', lambda *a: None)
    monkeypatch.setattr(s, 'router_get', lambda *a: (live, '"etag"'))
    monkeypatch.setattr(s, 'schema_inventory', lambda *a: {'schema.sql': '1'*64, 'migrations/002_archive.sql': '2'*64})
    monkeypatch.setattr(s.p, 'promotable', lambda *a: {'sha': SHA})
    commands = []
    def resolve(command):
        commands.append(command)
        assert command[:2] == ['docker', 'compose'] and 'config' in command
        return json.dumps(s.p.read(command[command.index('-f')+1])).replace('$$', '$')
    monkeypatch.setattr(s.p, 'run', resolve)
    result = s.prepare(cfg, SHA)
    assert result['status'] == 'prepared'
    receipt = s.p.read(release / 'http-prepared.json')
    assert receipt['retained'] == [previous_gen]
    assert receipt['previous'] == previous
    assert s.p.read(cfg['http_release']['router']['config_file']) == live
    assert all('stop' not in command and 'up' not in command for command in commands)


def test_missing_readiness_control_never_counts_as_zero(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    cfg['http_release']['ready_timeout'] = 0
    receipt = {'generation': GEN, 'sha': SHA, 'controls': {'api': '/nonexistent/control.sock', 'gateway': '/nonexistent/other.sock'}}
    monkeypatch.setattr(s, 'control', lambda *a: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(s.Refused, match='readiness did not pass'):
        s.wait_ready(cfg, receipt)


@pytest.mark.parametrize('census', [[], [{'address': 'http-api-'+GEN+':8080', 'num_requests': True}],
    [{'address': 'http-api-'+GEN+':8080', 'num_requests': -1}],
    [{'address': 'http-api-'+GEN+':8080', 'num_requests': 0}]*2, {'wrong': []}])
def test_missing_or_ambiguous_proxy_census_is_not_zero(monkeypatch, census):
    monkeypatch.setattr(s, 'router_request', lambda *a, **kw: (census, None))
    with pytest.raises(s.Refused, match='census'):
        s.router_inflight({}, GEN, 'api')


def test_proxy_forwarded_request_retains_old_container_after_asgi_reaches_zero(tmp_path, monkeypatch):
    cfg, release, calls, receipt = fixture(tmp_path, monkeypatch)
    receipt['containers'] = {'api': {'container': 'old-api', 'image': OLD}, 'gateway': {'container': 'old-gateway', 'image': OLD}}
    monkeypatch.setattr(s, 'control', lambda *a: status(phase='draining'))
    monkeypatch.setattr(s, 'router_inflight', lambda *a: 1)
    assert s.drain(cfg, receipt, 'gateway') is False
    assert not any(isinstance(value, list) and 'stop' in value for value in calls)


def test_revoked_original_policy_invalidates_the_frozen_candidate(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    compose = tmp_path / 'isolated.json'
    compose.write_text('{}')
    source = Path(cfg['http_release']['gateway_policy'])
    receipt = {'config_digest': s.p.digest(cfg), 'compose': str(compose), 'compose_digest': s.p.digest({}),
        'private_files': {str(source): s.hashlib.sha256(source.read_bytes()).hexdigest()}}
    source.write_text('{"revoked":true}')
    with pytest.raises(s.Refused, match='private HTTP policy changed'):
        s.validate_prepared(cfg, receipt)


def test_nullable_backend_origin_and_host_keep_their_original_effective_values(tmp_path):
    cfg = settings(tmp_path)
    source = Path(cfg['http_release']['gateway_policy'])
    value = s.p.read(source)
    value['deployment']['backends']['messaging'].update({'host_header': None, 'download_origin': None})
    source.write_text(json.dumps(value))
    document, private = s.isolate(cfg, documents(cfg), {'runtime': IMAGE, 'gateway': IMAGE}, tmp_path, GEN, SHA)
    target = s.p.read(source)['deployment']['backends']['messaging']
    assert target['host_header'] is None and target['download_origin'] is None
    assert document['services']['http-gateway-'+GEN]['environment']['DEEDKIT_RELEASE_MESSAGING_URL'] == 'http://http-api-'+GEN+':8080'


def test_same_topology_router_recreation_invalidates_prepared_identity(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    cfg['projects'] = []
    compose = tmp_path / 'isolated.json'
    compose.write_text('{}')
    live = s.route_document(cfg)
    s.p.write(Path(cfg['http_release']['router']['config_file']), live)
    receipt = {'sha': SHA, 'config_digest': s.p.digest(cfg), 'compose': str(compose), 'compose_digest': s.p.digest({}),
        'private_files': {}, 'images': {}, 'baseline': [], 'router_before': live,
        'router_source_digest': s.p.digest(live), 'router_container': 'frozen-router'}
    monkeypatch.setattr(s, 'router_identity', lambda _: 'replacement-router')
    monkeypatch.setattr(s, 'router_get', lambda _: (live, '"etag"'))
    with pytest.raises(s.Refused, match='router identity changed'):
        s.validate_prepared(cfg, receipt)


def test_candidate_recreation_after_activation_never_reaches_traffic(tmp_path, monkeypatch):
    cfg, release, calls, receipt = fixture(tmp_path, monkeypatch)
    identities = iter([{'api': {'container': 'new-api', 'image': IMAGE}, 'gateway': {'container': 'new-gateway', 'image': IMAGE}},
        {'api': {'container': 'replacement-api', 'image': IMAGE}, 'gateway': {'container': 'replacement-gateway', 'image': IMAGE}}])
    monkeypatch.setattr(s, 'candidate_identities', lambda _: next(identities))
    with pytest.raises(s.Refused, match='candidate.*identity'):
        s.promote(cfg, SHA)
    assert 'switch' not in calls


def test_candidate_returning_to_warming_after_activation_is_not_admitted(tmp_path, monkeypatch):
    cfg, release, calls, receipt = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(s, 'control', lambda path, action, generation, sha: status(phase='active' if action == 'activate' else 'warming'))
    with pytest.raises(s.Refused, match='active.*readiness'):
        s.promote(cfg, SHA)
    assert 'switch' not in calls


def test_proxy_census_must_be_empty_before_old_gateway_enters_drain(tmp_path, monkeypatch):
    cfg, release, calls, receipt = fixture(tmp_path, monkeypatch)
    receipt['containers'] = {'gateway': {'container': 'old-gateway', 'image': OLD}}
    def response(*args):
        calls.append('drain-command')
        return status(phase='draining')
    monkeypatch.setattr(s, 'control', response)
    monkeypatch.setattr(s, 'router_inflight', lambda *a: 1)
    assert s.drain(cfg, receipt, 'gateway') is False
    assert 'drain-command' not in calls


def staged_predecessor(tmp_path, monkeypatch):
    cfg, release, calls, receipt = fixture(tmp_path, monkeypatch)
    previous = copy.deepcopy(receipt)
    previous['generation'] = 'fedcba654321'
    previous['containers'] = {role: {'container': 'old-'+role, 'image': OLD} for role in ['api', 'gateway']}
    live = receipt['router_before']
    live.update(s.route_document(cfg, GEN, [previous['generation']]))
    s.p.write(Path(cfg['http_release']['router']['config_file']), live)
    receipt.update({'previous': previous, 'status': 'serving-retirement-pending', 'router_after': copy.deepcopy(live),
        'containers': {role: {'container': 'new-'+role, 'image': IMAGE} for role in ['api', 'gateway']}})
    s.p.write(release / 'http-result.json', receipt)
    s.p.write(release / 'http-attempted.json', {'status': 'serving'})
    s.p.write(Path(cfg['state_directory']) / 'http-current.json', receipt)
    monkeypatch.setattr(s, 'control', lambda path, action, generation, sha: {**status(phase='draining'), 'generation': generation})
    return cfg, release, calls, receipt


def test_fence_closes_late_admission_then_waits_existing_router_lease(tmp_path, monkeypatch):
    cfg, release, calls, receipt = staged_predecessor(tmp_path, monkeypatch)
    cfg['http_release']['drain_timeout'] = 0
    # A file request may be selected immediately before the atomic fence after
    # the first zero census. Its lease must keep the old gateway alive.
    counts = iter([0, 0, 1])
    monkeypatch.setattr(s, 'router_inflight', lambda *a: next(counts))
    result = s.retire(cfg, SHA)
    assert result['status'] == 'serving-retirement-pending'
    assert ('fence', receipt['previous']['generation']) in calls
    assert not any(isinstance(value, list) and 'stop' in value for value in calls)
    assert s.p.read(cfg['http_release']['router']['config_file'])['generations'][receipt['previous']['generation']]['admit_files'] is False
    assert s.p.read(release / 'http-attempted.json')['status'] == 'serving'


def test_unknown_admission_fence_outcome_requires_recovery_and_never_stops_old(tmp_path, monkeypatch):
    cfg, release, calls, receipt = staged_predecessor(tmp_path, monkeypatch)
    monkeypatch.setattr(s, 'router_fence', lambda *a: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(s.Refused, match='fence needs recovery'):
        s.retire(cfg, SHA)
    assert s.p.read(release / 'http-attempted.json')['status'] == 'recovery-required'
    with pytest.raises(s.Refused, match='recovery'):
        s.retire(cfg, SHA)
    assert not any(isinstance(value, list) and 'stop' in value for value in calls)


def test_router_recreation_at_fence_is_refused_before_mutation(tmp_path, monkeypatch):
    cfg, release, calls, receipt = staged_predecessor(tmp_path, monkeypatch)
    monkeypatch.setattr(s, 'router_identity', lambda *a: 'replacement-router')
    with pytest.raises(s.Refused, match='router identity changed'):
        s.fence_previous(cfg, receipt, release)
    assert not any(isinstance(value, tuple) and value[0] == 'fence' for value in calls)


@pytest.mark.parametrize('response', [{'protocol': True, 'kind': 'deedkit-release-router'},
    {'protocol': 1, 'kind': 'caddy'}, {'protocol': 1, 'kind': 'deedkit-release-router', 'extra': 1}])
def test_router_requires_the_atomic_admission_protocol_identity(monkeypatch, response):
    monkeypatch.setattr(s, 'router_request', lambda *a, **kw: (response, None))
    with pytest.raises(s.Refused, match='atomic admission protocol'):
        s.router_kind({})


def test_original_private_policy_may_be_owned_by_the_runtime_uid(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    policy = Path(cfg['http_release']['gateway_policy'])
    runtime_owned = {policy}
    original = Path.lstat
    def lstat(path):
        value = original(path)
        if path in runtime_owned:
            return SimpleNamespace(st_uid=10001, st_mode=value.st_mode)
        return value
    monkeypatch.setattr(Path, 'lstat', lstat)
    assert s.validate_settings(cfg) is cfg
    # The same permission exception never applies to the admin directory.
    runtime_owned.add(Path(cfg['http_release']['router']['directory']))
    with pytest.raises(s.Refused, match='operator-owned'):
        s.validate_settings(cfg)


@pytest.mark.parametrize('failure', ['missing', 'sha', 'image', 'target', 'ci'])
def test_bootstrap_requires_clean_verified_router_build_receipt(tmp_path, monkeypatch, failure):
    cfg = settings(tmp_path)
    if failure != 'missing':
        path = router_build_receipt(cfg)
        value = s.p.read(path)
        value[failure] = {'sha': 'd'*40} if failure == 'ci' else 'wrong'
        s.p.write(path, value)
    monkeypatch.setattr(s.p, 'promotable', lambda sha: {'sha': sha})
    monkeypatch.setattr(s.p, 'run', lambda *a: json.dumps([{'Id': IMAGE, 'Config': {'Labels': {
        'org.opencontainers.image.revision': SHA,
        'org.opencontainers.image.source': 'https://github.com/'+s.p.REPOSITORY}}}]))
    with pytest.raises(s.Refused, match='router build receipt'):
        s.bootstrap(cfg, SHA)
    assert list(Path(cfg['http_release']['router']['directory']).iterdir()) == []


def test_promotion_preserves_every_fenced_issuer_tombstone(tmp_path, monkeypatch):
    cfg, release, calls, receipt = fixture(tmp_path, monkeypatch)
    old_generation = 'fedcba654321'
    live = receipt['router_before']
    live['generations'][old_generation] = {'api': 'http-api-'+old_generation+':8080',
        'gateway': 'http-gateway-'+old_generation+':13240', 'admit_files': False}
    expected = copy.deepcopy(live['generations'][old_generation])
    receipt['router_source_digest'] = s.p.digest(live)
    s.p.write(Path(cfg['http_release']['router']['config_file']), live)
    s.p.write(release / 'http-prepared.json', receipt)
    result = s.promote(cfg, SHA)
    assert result['status'] == 'serving-retirement-pending'
    persisted = s.p.read(cfg['http_release']['router']['config_file'])
    assert old_generation in persisted['generations']
    assert persisted['generations'][old_generation] == expected
    assert persisted['generations'][old_generation]['admit_files'] is False



def test_serving_generation_may_lack_only_migrations_plan_a_already_runs(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    base = {'schema.sql': '1'*64, 'migrations/002_archive.sql': '2'*64, 'migrations/003_more.sql': '3'*64}
    candidate = {**base, 'migrations/004_expand.sql': '4'*64}
    serving = 'sha256:' + 'd' * 64
    def check(old):
        inventories = {IMAGE: candidate, OLD: candidate, serving: old}
        monkeypatch.setattr(s.p, 'run', lambda command: json.dumps(inventories[command[-3]]))
        s.check_schema(cfg, {'images': {'runtime': IMAGE}, 'schema': candidate,
            'baseline': [{'name': 'messages', 'running': {'api': {'image': OLD}}}], 'previous': {'images': {'runtime': serving}}})
    check(base)
    check(candidate)
    refused = [
        {**base, 'migrations/003_more.sql': 'f'*64},  # an applied migration changed
        {**base, 'schema.sql': 'f'*64},
        {**candidate, 'migrations/005_newer.sql': '5'*64},  # the serving generation is newer
        {'schema.sql': '1'*64, 'migrations/002_archive.sql': '2'*64, 'migrations/004_expand.sql': '4'*64},  # gap below its newest
        {**base, 'notes.sql': '9'*64}]
    for old in refused:
        with pytest.raises(s.Refused, match='schema inventory mismatch'):
            check(old)
    # A second file under an added number, or a gap after the old newest, could hide an unapplied
    # migration from readiness, which keys the applied ledger by number.
    for extra in ({**candidate, 'migrations/004_unapplied.sql': '5'*64},
                  {**base, 'migrations/005_skips.sql': '5'*64}):
        inventories = {IMAGE: extra, OLD: extra, 'sha256:' + 'd' * 64: base}
        monkeypatch.setattr(s.p, 'run', lambda command: json.dumps(inventories[command[-3]]))
        with pytest.raises(s.Refused, match='schema inventory mismatch'):
            s.check_schema(cfg, {'images': {'runtime': IMAGE}, 'schema': extra,
                'baseline': [{'name': 'messages', 'running': {'api': {'image': OLD}}}], 'previous': {'images': {'runtime': serving}}})


def test_plan_a_api_must_run_the_candidate_inventory_even_with_a_prefix_generation(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    base = {'schema.sql': '1'*64, 'migrations/002_archive.sql': '2'*64}
    candidate = {**base, 'migrations/003_expand.sql': '3'*64}
    serving = 'sha256:' + 'd' * 64
    inventories = {IMAGE: candidate, OLD: base, serving: base}
    monkeypatch.setattr(s.p, 'run', lambda command: json.dumps(inventories[command[-3]]))
    with pytest.raises(s.Refused, match='schema inventory mismatch'):
        s.check_schema(cfg, {'images': {'runtime': IMAGE}, 'schema': candidate,
            'baseline': [{'name': 'messages', 'running': {'api': {'image': OLD}}}], 'previous': {'images': {'runtime': serving}}})


def test_prepare_after_plan_a_reuses_its_images_for_the_schema_probe(tmp_path, monkeypatch):
    from tests.test_production import ImageStore
    monkeypatch.setattr(s.p, 'IMAGE_LOCK', tmp_path / 'absent' / 'image-build.lock')
    cfg = settings(tmp_path)
    release = Path(cfg['state_directory']) / SHA
    release.mkdir()
    store = ImageStore()
    probed = []
    def run(command, **kwargs):
        if command[:2] == ['docker', 'compose']:
            return json.dumps(s.p.read(command[command.index('-f')+1])).replace('$$', '$')
        result = store.run(command, **kwargs)  # a missing image fails like Docker: "No such image"
        if s.SCHEMA_READER in command:
            probed.append(command[command.index('--entrypoint') + 2])
            return json.dumps({'schema.sql': '1'*64, 'migrations/002_archive.sql': '2'*64})
        return result
    monkeypatch.setattr(s.p, 'run', run)
    monkeypatch.setattr(s.p, 'published', store.published)
    # Plan A (production.py prepare and apply) pulls both HTTP images; its containers run on them.
    plan_a = {target: s.p.build_image(tmp_path, SHA, target) for target in ('runtime', 'gateway')}
    docs = documents(cfg)
    baseline = []
    for project in cfg['projects']:
        name = project['name']
        service, target = next(iter(project['services'].items()))
        s.p.write(release / (name+'.json'), s.p.escaped(docs[name]))
        baseline.append({'name': name, 'before_digest': s.p.digest(docs[name]),
                         'running': {service: {'container': 'plan-a-'+name, 'image': plan_a[target]}}})
    # Seamless preparation then obtains the same targets through production.py.
    monkeypatch.setattr(s.p, 'prepare', lambda *a: {'images': {target: s.p.build_image(tmp_path, SHA, target) for target in ('runtime', 'gateway')},
                                                     'projects': baseline, 'ci': {'sha': SHA}})
    live = s.route_document(cfg)
    s.p.write(Path(cfg['http_release']['router']['config_file']), live)
    monkeypatch.setattr(s, 'router_identity', lambda *a: None)
    monkeypatch.setattr(s, 'router_get', lambda *a: (live, '"etag"'))
    monkeypatch.setattr(s.p, 'promotable', lambda *a: {'sha': SHA})
    assert s.prepare(cfg, SHA)['images'] == plan_a
    assert len(store.pulls()) == 2 and store.built() == [] and set(plan_a.values()) <= set(store.images)
    # The receipt schema, the candidate and the running plan A API image are all probed, on plan A's image.
    assert probed == [plan_a['runtime']] * 3
    assert s.p.read(release / 'http-prepared.json')['images'] == plan_a


def test_promotion_accepts_a_verified_commit_that_main_has_moved_past(tmp_path, monkeypatch):
    from tests.test_production import accepting_verifier, main_repository, offline
    promotable, run = s.p.promotable, s.p.run
    history, side = main_repository(tmp_path, monkeypatch)  # two commits merged after history[0]
    accepting_verifier(tmp_path, monkeypatch)
    requests = offline(monkeypatch)
    def gate(sha, calls):
        # The real check (git and the verifier) inside the fixture's synthetic Docker.
        calls.append('verify')
        fake = s.p.run
        s.p.run = run
        try:
            return promotable(history[0] if sha == SHA else side)
        finally:
            s.p.run = fake
    (tmp_path / 'host').mkdir()
    cfg, release, calls, receipt = fixture(tmp_path / 'host', monkeypatch)
    monkeypatch.setattr(s.p, 'promotable', lambda sha: gate(sha, calls))
    s.promote(cfg, SHA)
    assert 'switch' in calls and calls.count('verify') >= 3 and requests == []
    (tmp_path / 'other').mkdir()
    cfg, release, calls, receipt = fixture(tmp_path / 'other', monkeypatch)
    monkeypatch.setattr(s.p, 'promotable', lambda sha: gate('c' * 40, calls))  # a commit off main
    with pytest.raises(s.Refused, match='not a commit on main'):
        s.promote(cfg, SHA)
    assert 'switch' not in calls and not (release / 'http-attempted.json').exists()