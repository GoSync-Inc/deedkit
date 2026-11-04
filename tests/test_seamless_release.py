"""Synthetic release proof: never connects to a provider or production database."""
import contextlib
import copy
import fcntl
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import tempfile
import time
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


# spec: ANYM-711
def test_the_gateway_candidate_joins_the_recordings_network_and_keeps_its_others(tmp_path):
    cfg = settings(tmp_path)
    source = documents(cfg)
    source['gateway']['services']['gateway']['networks']['recordings-backend'] = {}
    source['gateway']['networks']['recordings-backend'] = {'name': 'deedkit-recordings-backend'}
    release = Path(cfg['state_directory']) / SHA
    release.mkdir()
    doc, _ = s.isolate(cfg, source, {'runtime': IMAGE, 'gateway': IMAGE}, release, GEN, SHA)
    unit = doc['services']['http-gateway-' + GEN]
    assert set(unit['networks']) == {'gateway-integration', 'gateway-frontend', 'gateway-recordings-backend'}
    assert all(net['aliases'] == ['http-gateway-' + GEN] for net in unit['networks'].values())
    assert doc['networks']['gateway-recordings-backend'] == {'external': True, 'name': 'deedkit-recordings-backend'}
    assert doc['networks']['gateway-integration'] == {'external': True, 'name': 'integration'}


# spec: ANYM-711
def test_the_recordings_project_cannot_become_a_third_selected_http_service(tmp_path):
    cfg = settings(tmp_path)
    overlay = str(tmp_path / 'compose.gateway-recordings.yaml')
    cfg['projects'][1]['files'].append(overlay)
    assert s.validate_settings(cfg) is cfg  # the gateway overlay changes no selection
    cfg['projects'].append({'name': 'recordings', 'directory': str(tmp_path), 'files': [overlay],
                            'services': {'recordings-api': 'recordings'}})
    with pytest.raises(s.Refused, match='two'):
        s.validate_settings(cfg)


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


def retired_chain(tmp_path, monkeypatch, count=5):
    """`count` HTTP generations, the last serving with the one before it as `previous`; every older one retired by its
    successor's receipt. Returns the config, the generations (oldest first), the fake docker's containers and the argv log."""
    cfg = settings(tmp_path)
    state = Path(cfg['state_directory'])
    generations = ['%012x' % (number + 1) for number in range(count)]
    containers, calls, previous = {}, [], None
    for number, generation in enumerate(generations):
        sha = '%040x' % (number + 1)
        identities = {role: {'container': '%064x' % (0x100 + number * 2 + (role == 'gateway')), 'image': IMAGE} for role in ['api', 'gateway']}
        receipt = {'sha': sha, 'generation': generation, 'status': 'serving', 'containers': identities,
                   'retired_roles': ['gateway', 'api'] if previous else [], 'previous': previous}
        (state / sha).mkdir()
        s.p.write(state / sha / 'http-result.json', receipt)
        s.p.write(state / sha / 'http-attempted.json', {'status': 'serving', 'sha': sha})
        current = number == count - 1
        for role, identity in identities.items():
            containers[identity['container']] = {'Id': identity['container'], 'Image': IMAGE,
                'State': {'Status': 'running' if current else 'exited', 'Running': current},
                'Config': {'Labels': {'com.docker.compose.project': 'deedkit-http-' + generation,
                    'com.docker.compose.service': 'http-' + role + '-' + generation, 'ai.deedkit.http-release': generation}}}
        previous = {key: copy.deepcopy(value) for key, value in receipt.items() if key != 'previous'} | {'previous': None}
        s.p.write(state / 'http-current.json', receipt)

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ['docker', 'inspect']:
            if argv[2] not in containers:
                raise s.Refused('docker failed (exit 1): Error: No such object: ' + argv[2])
            return json.dumps([containers[argv[2]]])
        if argv[:2] == ['docker', 'rm']:
            del containers[argv[2]]
            return ''
        if argv[:3] == ['docker', 'ps', '--all']:
            return ''.join('%s %s %s\n' % (identity, value['Config']['Labels'].get('ai.deedkit.http-release', ''), value['State']['Status'])
                           for identity, value in containers.items())
        raise AssertionError(argv)
    monkeypatch.setattr(s.p, 'run', run)
    return cfg, generations, containers, calls


def container_of(containers, generation, role):
    return next(identity for identity, value in containers.items() if value['Config']['Labels']['com.docker.compose.service'] == 'http-' + role + '-' + generation)


# spec: ANYM-689
def test_clean_removes_exactly_the_older_retired_generations_by_receipt_id(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    older = {generation: [container_of(containers, generation, role) for role in ['api', 'gateway']] for generation in generations[:3]}
    kept = [identity for identity in containers if identity not in sum(older.values(), [])]
    summary = s.clean(cfg, True)
    assert sorted(summary['removed']) == sorted(g + '/' + role for g in generations[:3] for role in ['api', 'gateway'])
    assert summary['generations'] == generations[:3] and summary['unresolved'] == 0
    assert sorted(argv[2] for argv in calls if argv[1] == 'rm') == sorted(sum(older.values(), []))
    assert all(argv[:2] in (['docker', 'inspect'], ['docker', 'rm']) and len(argv) == 3 for argv in calls if argv[1] != 'ps')
    assert sorted(containers) == sorted(kept)
    assert sorted(summary['ids']) == sorted(identity[:12] for identity in sum(older.values(), [])) and summary['not_selected_count'] == 0 and summary['unlabelled_count'] == 0


# spec: ANYM-689
def test_clean_never_selects_a_foreign_container_that_carries_a_receipted_label(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    foreign = containers[container_of(containers, generations[0], 'api')] | {'Id': 'f' * 64}
    containers['f' * 64] = foreign
    s.clean(cfg, True)
    assert 'f' * 64 in containers and not any('f' * 64 in argv for argv in calls)


# spec: ANYM-689
def test_clean_skips_a_generation_whose_retirement_the_successor_does_not_record(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    state = Path(cfg['state_directory'])
    successor = state / ('%040x' % 3) / 'http-result.json'
    value = s.p.read(successor)
    s.p.write(successor, value | {'status': 'serving-retirement-pending', 'retired_roles': ['gateway']})
    summary = s.clean(cfg, True)
    # Generation 2 itself reads pending with a managed predecessor, and generation 1's retirement is no longer recorded.
    assert summary['generations'] == [generations[0]] and summary['unresolved'] == 2
    assert container_of(containers, generations[1], 'api') in containers and container_of(containers, generations[2], 'api') in containers


# spec: ANYM-689
@pytest.mark.parametrize('state', ['running', 'restarting', 'created', 'dead', 'paused', 'removing'])
def test_clean_removes_nothing_that_is_not_exited(tmp_path, monkeypatch, state):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    target = container_of(containers, generations[0], 'api')
    containers[target]['State'] = {'Status': state, 'Running': state in {'running', 'restarting'}}
    summary = s.clean(cfg, True)
    assert target in containers and {'container': generations[0] + '/api', 'id': target[:12], 'reason': 'state ' + state} in summary['skipped']
    assert ['docker', 'rm', target] not in calls


# spec: ANYM-689
@pytest.mark.parametrize('change', ['image', 'project', 'service', 'release'])
def test_clean_skips_a_container_whose_identity_or_labels_changed_since_the_receipt(tmp_path, monkeypatch, change):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    target = container_of(containers, generations[1], 'gateway')
    labels = containers[target]['Config']['Labels']
    if change == 'image':
        containers[target]['Image'] = OLD
    else:
        labels[{'project': 'com.docker.compose.project', 'service': 'com.docker.compose.service', 'release': 'ai.deedkit.http-release'}[change]] = 'other'
    summary = s.clean(cfg, True)
    assert target in containers and any(item['container'] == generations[1] + '/gateway' for item in summary['skipped'])


# spec: ANYM-689
def test_a_failed_removal_is_reported_never_repeated_and_never_raises(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    first, second = container_of(containers, generations[0], 'api'), container_of(containers, generations[0], 'gateway')
    real = s.p.run
    def lost(argv, **kwargs):
        if argv == ['docker', 'rm', second]:
            calls.append(argv)
            raise TimeoutError('no answer')
        return real(argv, **kwargs)
    monkeypatch.setattr(s.p, 'run', lost)
    summary = s.clean(cfg, True)
    assert summary['unknown'] == [{'container': generations[0] + '/gateway', 'id': second[:12], 'reason': 'TimeoutError'}]
    assert calls.count(['docker', 'rm', second]) == 1 and first not in containers and second in containers
    assert len(summary['removed']) == 5
    # The next run inspects the same ID: still present and unchanged it is removable again; absent it is only reported.
    monkeypatch.setattr(s.p, 'run', real)
    assert s.clean(cfg, True)['removed'] == [generations[0] + '/gateway']
    assert s.clean(cfg, True)['removed'] == []


# spec: ANYM-689
def test_clean_skips_what_a_malformed_receipt_cannot_prove_and_raises_nothing(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    state = Path(cfg['state_directory'])
    (state / ('%040x' % 2) / 'http-result.json').write_text('not json')
    summary = s.clean(cfg, True)
    # The second generation's receipt carries the proof for the first one and its own identities: neither is removed.
    # Generation 0 lost its proof, the unreadable receipt file is counted, and so are the receipt-less labelled containers
    # of that same generation: an over-count by one is the price of never hiding one.
    assert summary['generations'] == [generations[2]] and summary['unresolved'] == 3 and summary['no_receipt_generations'] == 1
    (state / 'http-current.json').write_text('{')
    assert s.clean(cfg, True)['removed'] == [] and s.retired_generations(cfg) == ({}, 0)


# spec: ANYM-689
def test_clean_stops_at_its_time_budget_between_two_removals(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    clock, real = [0.0], s.p.run
    def slow(argv, **kwargs):
        if argv[1] == 'rm':
            clock[0] += 1000
        return real(argv, **kwargs)
    monkeypatch.setattr(s.p, 'run', slow)
    monkeypatch.setattr(s.time, 'monotonic', lambda: clock[0])
    summary = s.clean(cfg, True)
    assert summary['stopped'] == 'budget' and len(summary['removed']) == 1


# spec: ANYM-689
def test_clean_skips_a_pending_generation_that_had_a_managed_predecessor(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    receipt = Path(cfg['state_directory']) / ('%040x' % 2) / 'http-result.json'
    s.p.write(receipt, s.p.read(receipt) | {'status': 'serving-retirement-pending', 'retired_roles': ['gateway']})
    summary = s.clean(cfg, True)
    # Generation 1 never retired its own predecessor (so generation 0 has no proof either); generation 2 is still removable.
    assert summary['generations'] == [generations[2]] and summary['unresolved'] == 2
    assert container_of(containers, generations[0], 'api') in containers and container_of(containers, generations[1], 'api') in containers


# spec: ANYM-689
def test_clean_removes_the_first_managed_generation_while_its_own_retirement_is_pending(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    receipt = Path(cfg['state_directory']) / ('%040x' % 1) / 'http-result.json'
    s.p.write(receipt, s.p.read(receipt) | {'status': 'serving-retirement-pending'})
    assert s.clean(cfg, True)['generations'] == generations[:3]


# spec: ANYM-689
def test_clean_skips_a_generation_with_two_conflicting_receipts(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    state = Path(cfg['state_directory'])
    twin = state / ('%040x' % 99)
    twin.mkdir()
    s.p.write(twin / 'http-result.json', s.p.read(state / ('%040x' % 1) / 'http-result.json') | {'sha': '%040x' % 99, 'status': 'serving-retirement-pending'})
    s.p.write(twin / 'http-attempted.json', {'status': 'serving', 'sha': '%040x' % 99})
    summary = s.clean(cfg, True)
    assert summary['generations'] == generations[1:3] and container_of(containers, generations[0], 'api') in containers


# spec: ANYM-689
def test_every_docker_command_of_a_clean_gets_a_timeout_that_never_exceeds_what_is_left(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    timeouts, clock, real = [], [0.0], s.p.run
    def spy(argv, **kwargs):
        timeouts.append(kwargs.get('timeout'))
        clock[0] += 50
        return real(argv, **kwargs)
    monkeypatch.setattr(s.p, 'run', spy)
    monkeypatch.setattr(s.time, 'monotonic', lambda: clock[0])
    summary = s.clean(cfg, True)
    assert timeouts[:3] == [30, 30, 20] and summary['stopped'] == 'budget'
    assert all(value is not None and 1 <= value <= 30 for value in timeouts) and timeouts == sorted(timeouts, reverse=True)


# spec: ANYM-689
def test_clean_lists_the_labelled_containers_it_did_not_select_without_touching_them(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    # A foreign copy of a known generation, one of a generation without any receipt, and two unlabelled containers.
    copy_of = containers[container_of(containers, generations[0], 'api')]
    containers['f' * 64] = copy_of | {'Id': 'f' * 64}
    containers['e' * 64] = copy_of | {'Id': 'e' * 64, 'Config': {'Labels': {'ai.deedkit.http-release': 'ffffffffffff'}}}
    for number in range(2):
        containers['%064x' % (0xd0 + number)] = {'Id': '%064x' % (0xd0 + number), 'Image': IMAGE, 'State': {'Status': 'exited', 'Running': False}, 'Config': {'Labels': {}}}
    summary = s.clean(cfg, True)
    assert summary['not_selected_count'] == 2 and summary['unlabelled_count'] == 2
    # The generation with no receipt at all is as unresolved as one with an ineligible receipt, and counted once.
    assert summary['no_receipt_generations'] == 1 and summary['unresolved'] == 1
    assert sorted(summary['not_selected'], key=lambda item: item['id']) == [
        {'id': 'e' * 12, 'generation': 'ffffffffffff', 'state': 'exited', 'reason': 'no receipt'},
        {'id': 'f' * 12, 'generation': generations[0], 'state': 'exited', 'reason': 'generation not eligible'}]
    assert {'e' * 64, 'f' * 64} <= set(containers) and not any(argv[1] == 'rm' and argv[2] in {'e' * 64, 'f' * 64} for argv in calls if argv[1] != 'ps')
    assert len(containers) == 4 + 2 + 2


def clock_after_first_inspect(monkeypatch, spent):
    """A fake clock that jumps by `spent` seconds during the first docker command; returns it and the (verb, timeout) log."""
    clock, timeouts, real = [0.0], [], s.p.run
    def spy(argv, **kwargs):
        timeouts.append((argv[1], kwargs.get('timeout')))
        if len(timeouts) == 1:
            clock[0] += spent
        return real(argv, **kwargs)
    monkeypatch.setattr(s.p, 'run', spy)
    monkeypatch.setattr(s.time, 'monotonic', lambda: clock[0])
    return timeouts


# spec: ANYM-689
def test_no_command_starts_exactly_at_the_deadline(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    timeouts = clock_after_first_inspect(monkeypatch, s.CLEAN_BUDGET)
    summary = s.clean(cfg, True)
    assert [verb for verb, _ in timeouts] == ['inspect'] and summary['stopped'] == 'budget' and summary['removed'] == []


# spec: ANYM-689
@pytest.mark.parametrize('left', [0.2, 0.0002])
def test_the_last_command_gets_only_the_fraction_of_a_second_that_is_left(tmp_path, monkeypatch, left):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    timeouts = clock_after_first_inspect(monkeypatch, s.CLEAN_BUDGET - left)
    s.clean(cfg, True)
    # No floor: a command never gets more time than the budget has left, even below a millisecond.
    assert timeouts[0][1] == 30 and timeouts[1] == ('rm', pytest.approx(left, abs=1e-9))


def plan_a(tmp_path, monkeypatch, images):
    """A plan A configuration whose state directory exists, and a fake image pass that records the locks it was handed."""
    plan_state = tmp_path / 'plan-a'
    plan_state.mkdir()
    seen = []
    monkeypatch.setattr(s.p, 'config', lambda path: {'state_directory': str(plan_state), 'path': path})
    def remove(plan, held=()):
        seen.append((plan, held))
        return images() if callable(images) else images
    monkeypatch.setattr(s.p, 'remove_images', remove)
    return plan_state, seen


# spec: ANYM-689
def test_clean_command_loads_the_plan_a_configuration_before_removing_anything(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    monkeypatch.setattr(s.p, 'config', lambda path: (_ for _ in ()).throw(s.Refused('bad plan configuration')))
    before = dict(containers)
    with pytest.raises(s.Refused, match='bad plan configuration'):
        s.clean_command(cfg, True, 'plan.json')
    assert containers == before and not any(argv[1] == 'rm' for argv in calls)


# spec: ANYM-689
def test_clean_command_refuses_while_another_operation_holds_the_plan_a_lock(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    plan_state, seen = plan_a(tmp_path, monkeypatch, {'removed': []})
    before = dict(containers)
    with (plan_state / 'deploy.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(s.Refused, match='plan A deploy lock'):
            s.clean_command(cfg, True, 'plan.json')
    assert containers == before and seen == []


# spec: ANYM-689
def test_clean_command_ends_with_the_image_pass_and_holds_all_three_states(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    plan_state, seen = plan_a(tmp_path, monkeypatch, {'removed': [{'image': 'x'}]})
    monkeypatch.setattr(s, 'disk_free', lambda path='/var/lib/containerd': 12.5)
    result = s.clean_command(cfg, True, 'plan.json')
    assert result['mode'] == 'removed' and result['images_removed'] == 1 and len(result['containers']['removed']) == 6
    assert seen[0][1] == (cfg['state_directory'], s.GLOBAL_LOCK, plan_state)


# spec: ANYM-689
def test_a_failed_image_pass_keeps_the_container_summary(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    plan_a(tmp_path, monkeypatch, lambda: (_ for _ in ()).throw(s.Refused('another production operation holds a lock')))
    result = s.clean_command(cfg, True, 'plan.json')
    assert len(result['containers']['removed']) == 6 and result['images_removed'] == 0
    assert result['images'] == {'removed': [], 'error': 'another production operation holds a lock'}


# spec: ANYM-689
def test_report_only_changes_nothing_and_remove_needs_the_plan_a_configuration(tmp_path, monkeypatch):
    cfg, generations, containers, calls = retired_chain(tmp_path, monkeypatch)
    before = dict(containers)
    monkeypatch.setattr(s, 'disk_free', lambda path='/var/lib/containerd': 12.5)
    report = s.clean_command(cfg, False, None)
    assert report['mode'].startswith('report only, nothing removed') and len(report['containers']['would_remove']) == 6
    assert containers == before and not any(argv[1] == 'rm' for argv in calls)
    with pytest.raises(s.Refused, match='--plan-a-config'):
        s.clean_command(cfg, True, None)


# spec: ANYM-689
def test_retire_of_a_serving_generation_cleans_older_ones_again_and_a_cleanup_failure_changes_nothing(tmp_path, monkeypatch):
    cfg, release, calls, receipt = staged_predecessor(tmp_path, monkeypatch)
    receipt.update({'status': 'serving', 'retired_roles': ['gateway', 'api']})
    s.p.write(release / 'http-result.json', receipt)
    s.p.write(Path(cfg['state_directory']) / 'http-current.json', receipt)
    first = s.retire(cfg, SHA)
    assert first['status'] == 'serving' and set(first['cleanup']) >= {'removed', 'skipped', 'unknown'}
    monkeypatch.setattr(s, 'clean', lambda *a: (_ for _ in ()).throw(RuntimeError('boom')))
    again = s.retire(cfg, SHA)
    assert again['status'] == 'serving' and again['cleanup'] == {'error': 'RuntimeError'}


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


class PromoteCommand:
    """`seamless_release.py promote` with its locks, configuration and promotion replaced by recorders; the backup wait
    is the one under test."""

    def __init__(self, tmp_path, monkeypatch):
        self.events, self.settings = [], settings(tmp_path)
        self.digest = s.p.digest(self.settings)
        monkeypatch.setattr(s.os, 'geteuid', lambda: 0)
        monkeypatch.setattr(s.p, 'config', lambda path: self.settings)
        monkeypatch.setattr(s, 'validate_settings', lambda value: value)
        monkeypatch.setattr(s, 'promote', lambda value, sha, **extra: self.events.append(('promote', sha)) or {'sha': sha})
        monkeypatch.setattr(s, 'locks', self.locks)
        monkeypatch.setattr(s.p, 'wait_backup_idle', self.wait_backup_idle)
        monkeypatch.setattr(s.p, 'time', SimpleNamespace(monotonic=lambda: 1000.0))
        self.refusal = None

    @contextlib.contextmanager
    def locks(self, value):
        self.events.append('locks')
        try:
            yield
        finally:
            self.events.append('unlocks')

    @contextlib.contextmanager
    def wait_backup_idle(self, value, deadline, *, single=False):
        self.events.append(('wait', deadline - 1000.0))
        if self.refusal:
            raise self.refusal
        try:
            yield 7
        finally:
            self.events.append('released')


# spec: ANYM-744
@pytest.mark.parametrize('flags, bound', [([], 2700), (['--backup-wait', '30'], 30), (['--backup-wait', '0'], 0),
                                          (['--backup-wait', '7200'], 7200)])
def test_standalone_promote_waits_for_a_backup_inside_its_locks_with_the_bound_of_its_flag(tmp_path, monkeypatch, flags, bound):
    command = PromoteCommand(tmp_path, monkeypatch)
    s.main(['promote', '--sha', SHA, *flags])
    assert command.events == ['locks', ('wait', bound), ('promote', SHA), 'released', 'unlocks']


# spec: ANYM-744
@pytest.mark.parametrize('value', ['-1', '7201', 'ten', '1.5', ''])
def test_standalone_promote_refuses_a_bad_backup_wait_before_any_write(tmp_path, monkeypatch, capsys, value):
    command = PromoteCommand(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as stopped:
        s.main(['promote', '--sha', SHA, '--backup-wait', value])
    assert stopped.value.code == 2 and command.events == []
    assert 'backup.wait_seconds' in capsys.readouterr().err


# spec: ANYM-744
def test_standalone_promote_past_the_bound_exits_75_with_one_json_line_and_promotes_nothing(tmp_path, monkeypatch, capsys):
    command = PromoteCommand(tmp_path, monkeypatch)
    command.refusal = s.p.BackupRunning('personal-os-backup.service', 30, 'Wed 2026-11-04 18:00:01 UTC')
    with pytest.raises(SystemExit) as stopped:
        s.main(['promote', '--sha', SHA, '--backup-wait', '30'])
    printed = capsys.readouterr().out
    assert stopped.value.code == 75 and printed.count('\n') == 1
    assert json.loads(printed) == {'status': 'busy', 'code': 'backup_running', 'unit': 'personal-os-backup.service',
                                   'waited_seconds': 30, 'since': 'Wed 2026-11-04 18:00:01 UTC'}
    assert command.events == ['locks', ('wait', 30), 'unlocks']


# spec: ANYM-744
@pytest.mark.parametrize('action', ['prepare', 'status', 'bootstrap', 'retire', 'clean'])
def test_the_other_standalone_commands_never_wait_for_a_backup(tmp_path, monkeypatch, action):
    command = PromoteCommand(tmp_path, monkeypatch)
    for name in ('prepare', 'status', 'bootstrap', 'retire', 'clean_command'):
        monkeypatch.setattr(s, name, lambda *args, **extra: {})
    s.main([action, '--sha', SHA])
    assert not any(isinstance(event, tuple) and event[0] == 'wait' for event in command.events)


# spec: ANYM-744
def test_the_http_configuration_and_a_prepared_release_digest_do_not_change_with_the_backup_wait(tmp_path, monkeypatch):
    command = PromoteCommand(tmp_path, monkeypatch)
    s.main(['promote', '--sha', SHA, '--backup-wait', '30'])
    assert s.p.digest(command.settings) == command.digest and 'backup' not in command.settings


class WaitClock:
    """The fake clock for the backup wait: only `sleep` moves it, hooks run after each sleep, the rest is real time."""

    def __init__(self):
        self.now, self.hooks = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        for hook in self.hooks:
            hook(self.now)

    def __getattr__(self, name):
        return getattr(time, name)


# spec: ANYM-744
def test_promote_records_the_backup_wait_in_its_receipts_and_the_prepared_receipt_stays_unchanged(tmp_path, monkeypatch):
    cfg, release, calls, receipt = fixture(tmp_path, monkeypatch)
    prepared = (release / 'http-prepared.json').read_bytes()
    s.promote(cfg, SHA, backup_waited=1234)
    assert s.p.read(release / 'http-result.json')['backup_wait_seconds'] == 1234
    assert s.p.read(Path(cfg['state_directory']) / 'http-current.json')['backup_wait_seconds'] == 1234
    assert (release / 'http-prepared.json').read_bytes() == prepared


# spec: ANYM-744
def test_promote_without_a_wait_adds_nothing_to_its_receipts(tmp_path, monkeypatch):
    cfg, release, calls, receipt = fixture(tmp_path, monkeypatch)
    s.promote(cfg, SHA)
    assert 'backup_wait_seconds' not in s.p.read(release / 'http-result.json')


# spec: ANYM-744
def test_standalone_promote_waits_on_the_real_lock_then_runs_the_real_promotion_inside_it(tmp_path, monkeypatch, capsys):
    cfg, release, calls, receipt = fixture(tmp_path, monkeypatch)
    lock = tmp_path / 'backups' / '.runner.lock'
    lock.parent.mkdir(mode=0o700)
    monkeypatch.setenv('DEEDKIT_BACKUP_LOCK', str(lock))
    backup = {'fd': os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)}  # a running backup
    fcntl.flock(backup['fd'], fcntl.LOCK_EX | fcntl.LOCK_NB)

    def ends_at_ten_minutes(now):
        if now >= 600 and backup['fd'] is not None:
            os.close(backup['fd'])
            backup['fd'] = None

    def held():
        fd = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    clock = WaitClock()
    clock.hooks.append(ends_at_ten_minutes)
    monkeypatch.setattr(s.p, 'time', clock)
    inner = s.p.run
    monkeypatch.setattr(s.p, 'run', lambda argv, **kw: 'inactive\n' if argv[:2] == ['systemctl', 'show'] else inner(argv, **kw))
    gate, real_wait, waits, inside = s.p.promotable, s.p.wait_backup_idle, [], []
    monkeypatch.setattr(s.p, 'promotable', lambda sha: inside.append(held()) or gate(sha))
    monkeypatch.setattr(s.p, 'wait_backup_idle', lambda *args, **kwargs: waits.append(args) or real_wait(*args, **kwargs))
    monkeypatch.setattr(s.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(s.p, 'config', lambda path: cfg)
    monkeypatch.setattr(s, 'validate_settings', lambda value: value)  # root-ownership checks need a root test host
    monkeypatch.setattr(s, 'locks', lambda value: contextlib.nullcontext())
    s.main(['promote', '--sha', SHA])
    printed = json.loads(capsys.readouterr().out)
    assert 600 <= printed['backup_wait_seconds'] < 615 and len(waits) == 1
    assert inside and all(inside) and 'switch' in calls and not held()
    assert s.p.read(release / 'http-result.json')['backup_wait_seconds'] == printed['backup_wait_seconds']


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