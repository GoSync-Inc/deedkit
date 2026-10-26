"""Deployment policy verification, not live-host or provider acceptance checks.

Parse the distributed configuration and exercise its guards with synthetic values.
The Chromium preflight is run against injected offline browser collaborators.
"""
import atexit
import json
import logging
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import threading
import tomllib
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class ComposeLoader(yaml.SafeLoader):
    """Read Compose override tags without constructing arbitrary Python objects."""


ComposeLoader.add_constructor('!override', lambda loader, node:
    loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode)
    else loader.construct_mapping(node))


def test_native_overlay_removes_browser_seccomp_without_relaxing_privilege_boundary():
    native = document('deploy/compose.whatsapp-native.yaml')['services']
    for unit in native.values():
        assert unit['security_opt'] == ['no-new-privileges:true']
        assert 'whatsapp-native:' in unit['image']
        assert unit['pids_limit'] == 64 and unit['mem_limit'].endswith(':-768m}')


def document(relative):
    return yaml.load((ROOT / relative).read_text(), Loader=ComposeLoader)


def units():
    result = {}
    for path in sorted((ROOT / 'deploy').glob('compose*.yaml')):
        for name, settings in document(path.relative_to(ROOT))['services'].items():
            if name not in result or 'user' in settings:
                result[name] = settings
    return result


def volumes(unit):
    result = []
    for value in unit.get('volumes', []):
        if isinstance(value, str):
            source, target, *mode = value.split(':')
            result.append({'source': source, 'target': target, 'read_only': mode == ['ro']})
        else:
            result.append(value)
    return result


def test_application_units_enforce_sandbox_resource_and_log_bounds():
    applications = units()
    assert set(applications) == {'api', 'db', 'telegram', 'whatsapp1', 'whatsapp2',
        'indexer', 'enricher', 'enricher-local', 'operator', 'subscriptions',
        'calendar-db', 'calendar-api', 'calendar-worker', 'gateway', 'calls',
        'file-lifecycle', 'file-understanding', 'cold-archive', 'cold-restore', 'message-purge'}
    for name, unit in applications.items():
        if name in {'db', 'calendar-db'}:
            continue
        assert unit['read_only'] is True, name
        assert unit['user'] == '10001:10001', name
        assert unit['cap_drop'] == ['ALL'], name
        assert 'no-new-privileges:true' in unit['security_opt'], name
        assert 0 < unit['pids_limit'] <= 256, name
        assert unit['mem_limit'], name
        assert unit.get('cpu_shares', unit.get('cpus', 0)) or name == 'operator', name
        assert all('size=' in mount and 'noexec' in mount and 'nosuid' in mount
                   for mount in unit['tmpfs']), name
        if name == 'operator':
            assert unit['logging'] == {'driver': 'none'}
        else:
            assert unit['logging'] == {'driver': 'json-file', 'options': {'max-size': '5m', 'max-file': '2'}}, name


def test_database_and_optional_workers_have_isolated_networks_and_role_mounts():
    base = document('deploy/compose.yaml')
    db = base['services']['db']
    assert base['networks']['backend']['internal'] is True
    assert db['networks'] == ['backend']
    assert not db.get('ports')
    assert len(db['volumes']) == 1 and db['volumes'][0].startswith('database:')
    assert db['healthcheck']['test'][0] == 'CMD-SHELL'
    assert 'pg_isready' in db['healthcheck']['test'][1]
    assert db['mem_limit'] and db['cpu_shares']
    assert db['logging']['options'] == {'max-size': '5m', 'max-file': '2'}
    # Official PostgreSQL manages its own writable data/user rather than app UID.
    assert 'read_only' not in db and 'user' not in db
    apps = units()
    role_files = {'api': 'app.dsn', 'telegram': 'collector.dsn', 'whatsapp1': 'collector.dsn',
        'whatsapp2': 'collector.dsn', 'indexer': 'indexer.dsn', 'enricher': 'enrichment.dsn',
        'enricher-local': 'enrichment-local.dsn', 'subscriptions': 'subscriptions.dsn', 'operator': 'owner.dsn'}
    for name, filename in role_files.items():
        mounts = volumes(apps[name])
        dsns = [v for v in mounts if v['target'] == '/run/secrets/database_url']
        assert len(dsns) == 1 and dsns[0]['read_only'] is True
        assert Path(dsns[0]['source']).name == filename
        if name in {'telegram', 'whatsapp1', 'whatsapp2', 'enricher', 'enricher-local', 'operator'}:
            assert apps[name]['profiles']
        if name != 'api':
            assert not apps[name].get('ports')
    for name in ('telegram', 'whatsapp1', 'whatsapp2'):
        assert set(apps[name]['networks']) == {'backend', 'collector_egress'}
    assert apps['enricher-local']['networks'] == ['backend']
    assert apps['subscriptions']['networks'] == ['backend']
    for name in ('api', 'indexer'):
        originals = [v for v in volumes(apps[name]) if v['source'] == 'files']
        assert originals and all(v['read_only'] for v in originals)
    indexer = apps['indexer']
    lock = next(v for v in volumes(indexer) if v['target'] == '/processing-locks')
    assert lock['bind']['create_host_path'] is False
    assert indexer['stop_grace_period'] == '60s'


def test_gateway_proxy_and_owner_pairing_exposure_is_explicit():
    api = document('deploy/compose.yaml')['services']['api']
    assert api['ports'] == ['127.0.0.1:18940:8080']
    assert ':?' in api['environment']['PROBE_PUBLIC_URL']
    assert ':?' in api['environment']['PROBE_ALLOWED_HOSTS']
    operator = document('deploy/compose.operator.yaml')['services']['operator']
    assert operator['profiles'] == ['operator'] and operator['command'] == ['--help']
    assert not operator.get('ports') and operator['logging'] == {'driver': 'none'}
    pairing = document('deploy/compose.whatsapp-pair.yaml')['services']
    collectors = document('deploy/compose.whatsapp.yaml')['services']
    private_targets = {'/run/secrets/whatsapp_config', '/run/secrets/whatsapp_permit', '/sessions', '/spool'}
    account_mounts = []
    for name in ('whatsapp1', 'whatsapp2'):
        unit = collectors[name]
        assert unit['profiles'] == ['whatsapp'] and unit['init'] is True
        assert unit['security_opt'] == ['no-new-privileges:true', 'seccomp=./chromium-seccomp.json']
        assert unit['tmpfs'] == ['/tmp:size=256m,noexec,nosuid'] and unit['shm_size'] == '128m'
        assert unit['mem_limit'].endswith(':-2560m}') and unit['pids_limit'] == 256
        assert unit['stop_grace_period'] == '25s'
        account_mounts.append({v['source'] for v in volumes(unit) if v['target'] in private_targets})
        assert pairing[name]['logging'] == {'driver': 'none'}
        assert pairing[name]['restart'] == 'no'
        assert 'messaging_service.whatsapp_pair' in pairing[name]['command']
    assert account_mounts[0].isdisjoint(account_mounts[1])


# spec: ANYM-304
def test_calls_overlay_passes_the_trusted_host_peer_and_keeps_loopback_publishing():
    calls = document('deploy/compose.calls.yaml')
    unit = calls['services']['calls']
    # Empty by default: only loopback is trusted until the operator names the host-side peer.
    assert unit['environment']['VOICE_TRUSTED_PEERS'] == '${CALLS_TRUSTED_PEERS:-}'
    assert unit['ports'] == ['127.0.0.1:13842:13842']
    assert unit['networks'] == ['integration', 'calls-egress']
    assert calls['networks']['integration'] == {'external': True, 'name': '${DEEDKIT_NETWORK:-deedkit-integration}'}
    # The image starts the entry point that turns proxy-header rewriting off.
    dockerfile = (ROOT / 'services/calls/Dockerfile').read_text()
    assert dockerfile.rstrip().endswith('CMD ["python", "app.py"]') and 'VOICE_BIND=0.0.0.0' in dockerfile
    assert 'proxy_headers=False' in (ROOT / 'services/calls/app.py').read_text()
    # The launcher interpolates CALLS_TRUSTED_PEERS from calls.env, where production.md tells the operator to set it.
    import importlib.util
    spec = importlib.util.spec_from_file_location('deedkit_launcher', ROOT / 'scripts/deedkit.py')
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    plan = launcher.resolve({'version': 1, 'id': 'synthetic', 'exposure': {'tools': ['platform_capabilities']},
                             'deployment': {'modules': ['calls'], 'connectors': ['calls-phone'], 'external': []}})
    up = next(argv for argv in launcher.commands(plan, 'up') if 'deedkit-calls' in argv)
    assert up[up.index('--env-file') + 1] == str(ROOT / 'deploy/private/calls.env')
    assert 'CALLS_TRUSTED_PEERS' in (ROOT / 'docs/operations/production.md').read_text()



# spec: ANYM-496
def test_calls_registry_is_mounted_through_its_directory_so_a_rename_is_live():
    unit = document('deploy/compose.calls.yaml')['services']['calls']
    registry = PurePosixPath(unit['environment']['VOICE_AGENT_REGISTRY'])
    # Exactly one read-only bind covers the registry, and it is the registry's own directory.
    covering = [v for v in volumes(unit)
                if registry == PurePosixPath(v['target']) or PurePosixPath(v['target']) in registry.parents]
    assert covering == [{'type': 'bind', 'source': './private/calls-agents', 'target': str(registry.parent),
                         'read_only': True, 'bind': {'create_host_path': False}}]
    # The launcher preflight checks the file the service reads through that mount.
    module = tomllib.loads((ROOT / 'config/modules/calls.toml').read_text())
    assert f'deploy/private/calls-agents/{registry.name}' in module['required_files']
    admin = json.loads((ROOT / 'deploy/admin.example.json').read_text())
    assert admin['calls_registry'].endswith(f'/deploy/private/calls-agents/{registry.name}')
    assert admin['paths'][str(registry.parent)].endswith('/deploy/private/calls-agents')

def docker_stages():
    # Parse Dockerfile instructions after folding continuations; do not execute builds.
    lines = (ROOT / 'services/messaging/Dockerfile').read_text().replace('\\\n', ' ').splitlines()
    stages, defaults, current = {}, {}, None
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        instruction, _, value = line.partition(' ')
        if instruction == 'FROM':
            parent, _, name = value.partition(' AS ')
            current = {'parent': parent, 'instructions': []}
            stages[name] = current
        elif current is None and instruction == 'ARG':
            name, _, value = value.partition('=')
            defaults[name] = value
        elif current is not None:
            current['instructions'].append((instruction, value))
    return stages, defaults


def inherited(stages, target):
    stage = stages[target]
    parent = inherited(stages, stage['parent']) if stage['parent'] in stages else []
    return parent + stage['instructions']


def test_images_pin_supply_chain_inputs_and_keep_heavy_tools_out_of_gateway():
    stages, defaults = docker_stages()
    assert {'runtime', 'indexer', 'test', 'enricher', 'enricher-local', 'enricher-test', 'whatsapp', 'backup'} <= stages.keys()
    for name in ('PYTHON_IMAGE', 'NODE_IMAGE', 'POSTGRES_IMAGE'):
        assert re.fullmatch(r'[^ ]+@sha256:[0-9a-f]{64}', defaults[name])
    for name in ('runtime', 'indexer', 'test', 'enricher', 'enricher-local', 'enricher-test', 'whatsapp'):
        instructions = inherited(stages, name)
        assert [value for command, value in instructions if command == 'USER'][-1] == 'probe'
        assert any(command == 'RUN' and 'useradd -u 10001' in value for command, value in instructions)
        for command, value in instructions:
            if command == 'RUN' and 'pip install' in value:
                assert '--require-hashes' in value
    gateway_and_indexer = inherited(stages, 'runtime') + inherited(stages, 'indexer')
    assert not any('ffmpeg' in value or 'requirements-enrichment.lock' in value for _, value in gateway_and_indexer)
    assert any('tesseract-ocr-eng tesseract-ocr-rus' in value and 'poppler-utils' in value
               for _, value in stages['indexer']['instructions'])
    assert any('--only-binary=:all:' in value and '--require-hashes' in value
               for _, value in stages['enricher-local']['instructions'])
    env = ' '.join(v for command, v in inherited(stages, 'enricher-local') if command == 'ENV')
    assert 'HF_HUB_OFFLINE=1' in env and 'HF_HUB_DISABLE_TELEMETRY=1' in env
    assert any('npm ci' in value and '--ignore-scripts' in value for _, value in stages['whatsapp-dependencies']['instructions'])
    assert any('PUPPETEER_SKIP_DOWNLOAD=true' in value for _, value in inherited(stages, 'whatsapp'))
    assert any(command == 'ARG' and re.fullmatch(r'CHROMIUM_VERSION=\S+', value)
               for command, value in stages['whatsapp']['instructions'])
    backup = inherited(stages, 'backup')
    assert any('age-v1.3.2-linux-amd64.tar.gz' in value and 'hashlib.sha256(b).hexdigest()==' in value
               and re.search(r"=='[0-9a-f]{64}'", value) for _, value in backup)
    assert not any('/var/run/docker.sock' in value or 'id_ed25519' in value or 'agekey' in value for _, value in backup)
    copies = [shlex.split(value) for command, value in stages['test']['instructions'] if command == 'COPY']
    assert ['services/messaging/Dockerfile', './services/messaging/Dockerfile'] in copies
    assert ['deploy', './deploy'] in copies
    assert ['.github/workflows', './.github/workflows'] in copies


def test_optional_telemetry_overlay_requires_explicit_build_and_private_dsn():
    stages, _ = docker_stages()
    dependency_stage = stages['python-dependencies']['instructions']
    assert ('ARG', 'ANYMSG_INSTALL_TELEMETRY=0') in dependency_stage
    install = next(value for command, value in dependency_stage if command == 'RUN' and 'requirements-telemetry.lock' in value)
    assert '"$ANYMSG_INSTALL_TELEMETRY" = "1"' in install and '--require-hashes' in install
    overlay = document('deploy/compose.telemetry.yaml')['services']['api']
    assert overlay['build']['args']['ANYMSG_INSTALL_TELEMETRY'] == '1'
    assert overlay['environment']['ANYMSG_TIDEN_ENABLED'] == '1'
    assert overlay['environment']['ANYMSG_TIDEN_DSN_FILE'] == '/run/secrets/tiden_dsn'
    assert overlay['environment']['ANYMSG_TIDEN_ENVIRONMENT'] == '${ANYMSG_TIDEN_ENVIRONMENT:-production}'
    assert overlay['volumes'] == [{'type': 'bind', 'source': './private/tiden.dsn',
        'target': '/run/secrets/tiden_dsn', 'read_only': True, 'bind': {'create_host_path': False}}]


def test_operator_backup_worker_has_narrow_privilege_exception_and_private_mounts(tmp_path):
    from test_backup_operations import runner_fixture
    runner, docker = runner_fixture(tmp_path)
    runner.run()
    command = next(call for call in docker.calls if call[0] == 'create')
    values = lambda flag: [command[n + 1] for n, item in enumerate(command[:-1]) if item == flag]
    assert '--read-only' in command
    assert values('--user') == ['0']
    assert values('--cap-drop') == ['ALL'] and values('--cap-add') == ['DAC_OVERRIDE']
    assert values('--security-opt') == ['no-new-privileges']
    assert values('--network') == ['backend']
    assert values('--pids-limit') and values('--memory') and values('--cpus')
    mounts = values('--mount')
    assert any('src=files' in mount and 'readonly' in mount for mount in mounts)
    assert any('/private/owner.dsn' in mount and 'readonly' in mount for mount in mounts)
    assert not any('docker.sock' in mount or 'id_ed25519' in mount or 'agekey' in mount for mount in mounts)
    assert not any(flag in command for flag in ('-p', '--publish', '--privileged', '--pid', '--ipc'))


HEALTHCHECKS = {'api': 'deploy/compose.yaml', 'gateway': 'deploy/compose.gateway.yaml',
                'calendar-api': 'deploy/compose.calendar.yaml'}


def healthcheck(name):
    check = document(HEALTHCHECKS[name])['services'][name]['healthcheck']
    # Exec form, image Python only: no shell, curl or writes on the read-only rootfs.
    assert check['test'][:3] == ['CMD', 'python', '-c']
    assert check['interval'] == '30s' and check['timeout'] == '10s' and check['retries'] == 3
    assert check['start_period'] == '40s'
    return check['test'][3], check['test'][4:]


def probe(code, *arguments, **environment):
    # A proxy in the environment must be ignored: probes stay on loopback.
    env = {'PATH': '/usr/bin:/bin', 'HTTP_PROXY': 'http://127.0.0.1:9', 'http_proxy': 'http://127.0.0.1:9',
           **environment}
    result = subprocess.run([sys.executable, '-I', '-c', code, *arguments], env=env, capture_output=True, timeout=30)
    assert result.stdout == b'' and result.stderr == b''
    return result.returncode


class HostBoundServer:
    """Synthetic loopback server with the service's Host rule and health path."""
    def __init__(self, path, allowed):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                ok = self.path == path and self.headers.get('Host') in allowed and 'Origin' not in self.headers
                self.send_response(200 if ok else 403)
                self.send_header('Content-Length', '2'); self.end_headers(); self.wfile.write(b'{}')
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
    def __enter__(self):
        self.thread.start(); return self
    def __exit__(self, *_):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)


def test_api_healthcheck_passes_the_real_host_boundary_and_fails_closed(seeded):
    import socket
    import uvicorn
    from messaging_service.app import create_app
    code, arguments = healthcheck('api')
    assert arguments == []
    store, *_ = seeded
    listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(store, ['anymsg.example.invalid', f'127.0.0.1:{port}']),
                                           log_level='warning', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True); thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            threading.Event().wait(.05)
        settings = {'PROBE_BIND': '0.0.0.0', 'PROBE_PORT': str(port)}
        assert probe(code, PROBE_ALLOWED_HOSTS='anymsg.example.invalid,other.invalid', **settings) == 0
        # A Host the boundary does not allow is a failed probe, never a pass.
        assert probe(code, PROBE_ALLOWED_HOSTS='other.invalid,anymsg.example.invalid', **settings) == 1
    finally:
        server.should_exit = True; thread.join(timeout=10); listener.close()
    assert probe(code, PROBE_ALLOWED_HOSTS='anymsg.example.invalid', **settings) == 1


def test_gateway_healthcheck_uses_the_public_host_and_configured_port():
    code, arguments = healthcheck('gateway')
    command = document('deploy/compose.gateway.yaml')['services']['gateway']['command']
    assert arguments == [command[command.index('--public-url') + 1], command[command.index('--port') + 1]]
    with HostBoundServer('/healthz', {'gateway.example.invalid'}) as server:
        assert probe(code, 'https://gateway.example.invalid/deedkit', str(server.port)) == 0
        assert probe(code, 'https://other.example.invalid', str(server.port)) == 1
    assert probe(code, 'https://gateway.example.invalid', str(server.port)) == 1


@pytest.mark.parametrize('configured', [True, False])
def test_calendar_healthcheck_uses_the_service_port_bind_and_allowed_hosts(tmp_path, configured):
    code, arguments = healthcheck('calendar-api')
    assert arguments == []
    allowed = set()
    with HostBoundServer('/health', allowed) as server:
        # Without allowed_hosts the service accepts localhost/127.0.0.1 with its port.
        allowed.add('calendar-api:13232' if configured else f'127.0.0.1:{server.port}')
        settings = {'dsn': 'postgresql://synthetic-secret.invalid/calendar', 'port': server.port, 'bind': '0.0.0.0',
                    **({'allowed_hosts': ['calendar-api:13232', 'localhost:13232']} if configured else {})}
        (tmp_path / 'service').mkdir(mode=0o700)
        (tmp_path / 'service/gateway.json').write_text(json.dumps(settings))
        assert probe(code, FCS_DATA_DIR=str(tmp_path)) == 0
        allowed.clear()
        assert probe(code, FCS_DATA_DIR=str(tmp_path)) == 1
    assert probe(code, FCS_DATA_DIR=str(tmp_path / 'missing')) == 1


def synthetic_release_clone(tmp_path):
    """A local clone: v1.2.3 is on origin/main, v1.2.5 only on an unmerged branch."""
    clone = tmp_path / 'clone'
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_AUTHOR_NAME': 'Synthetic', 'GIT_AUTHOR_EMAIL': 'synthetic@example.invalid',
           'GIT_COMMITTER_NAME': 'Synthetic', 'GIT_COMMITTER_EMAIL': 'synthetic@example.invalid'}
    def git(*args):
        return subprocess.run(['git', '-C', str(clone), *args], env=env, check=True, capture_output=True,
                              text=True, timeout=30).stdout.strip()
    clone.mkdir()
    git('init', '--quiet', '--initial-branch=main')
    commits = {}
    for name in ('released', 'main-head'):
        git('commit', '--quiet', '--allow-empty', '-m', name); commits[name] = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', commits['main-head'])
    git('tag', 'v1.2.3', commits['released'])
    git('checkout', '--quiet', '-b', 'unmerged', commits['released'])
    git('commit', '--quiet', '--allow-empty', '-m', 'unmerged'); commits['unmerged'] = git('rev-parse', 'HEAD')
    git('tag', 'v1.2.5', commits['unmerged'])
    return clone, commits


def run_release_guard(guard, ref, version, release, sha=None):
    clone, commits = release
    # github.sha of a tag dispatch is the tag's commit; default to that.
    tagged = {'1.2.3': commits['released'], '1.2.5': commits['unmerged']}.get(version, commits['main-head'])
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(clone.parent), 'GIT_CONFIG_NOSYSTEM': '1',
           'VERSION': version, 'REF': ref, 'SHA': sha or tagged}
    return subprocess.run(['bash', '-e', '-c', guard], cwd=clone, env=env, capture_output=True, timeout=30)


def test_publish_workflow_guards_version_ref_and_uses_verified_artifact_oidc(tmp_path):
    release_clone = synthetic_release_clone(tmp_path)
    checks = document('.github/workflows/checks.yml')
    verify = checks['jobs']['verify']
    steps = verify['steps']
    assert verify['services']['postgres']['image'].split('@sha256:')[1]
    for required in ('Build packages', 'Validate release inventory and metadata', 'Python/PostgreSQL tests',
        'Node collector tests', 'Node HTTP client tests', 'Real synthetic MCP and HTTP with restricted gateway role',
        'Installed distributions outside checkout'):
        assert any(step.get('name') == required for step in steps)
    verification = {step['id']: step for step in steps if step.get('id')}
    assert set(('build', 'release', 'python', 'collector', 'npm', 'smoke', 'distributions')) <= verification.keys()
    assert 'uv build' in verification['build']['run']
    assert 'npm pack --pack-destination ../../dist --ignore-scripts' in verification['build']['run']
    assert 'scripts/check_release.py' in verification['release']['run']
    assert 'twine check dist/*.whl dist/*.tar.gz' in verification['release']['run']
    assert 'pytest' in verification['python']['run'] and '--junitxml=' in verification['python']['run']
    assert verification['python']['env']['ANYMSG_TEST_POSTGRES_CONTAINER'] == '${{ job.services.postgres.id }}'
    assert 'node --test --test-reporter=junit whatsapp/tests/*.test.mjs' in verification['collector']['run']
    assert verification['npm']['working-directory'] == 'packages/npm'
    assert 'node --test --test-reporter=junit test/*.test.js' in verification['npm']['run']
    assert 'scripts/smoke.py --dsn-file' in verification['smoke']['run']
    installed = verification['distributions']['run']
    assert 'uv pip install' in installed and 'dist/*.whl' in installed
    assert 'cd "$RUNNER_TEMP"' in installed and '-m anymsg --version' in installed
    assert 'npm install --ignore-scripts --no-audit --no-fund' in installed
    assert './node_modules/.bin/anymsg --version' in installed
    assert all(not verification[key].get('continue-on-error') for key in verification)
    # The release publisher has no checkout/build step: only the checks artifact.
    for kind, artifact in [('pypi', 'python-dist'), ('npm', 'npm-dist')]:
        workflow = document(f'.github/workflows/publish-{kind}.yml')
        # PyYAML 1.1 treats the unquoted GitHub 'on' key as boolean true.
        trigger = workflow.get('on', workflow.get(True))
        assert set(trigger) == {'workflow_dispatch'}
        assert trigger['workflow_dispatch']['inputs']['version']['required'] is True
        jobs = workflow['jobs']
        assert jobs['checks']['needs'] == 'tag'
        assert jobs['checks']['uses'] == './.github/workflows/checks.yml'
        assert jobs['checks']['with']['version'] == '${{ inputs.version }}'
        publish = jobs['publish']
        assert publish['needs'] == 'checks'
        assert publish['environment']['name'] == kind
        assert publish['permissions'] == {'contents': 'read', 'id-token': 'write'}
        assert any(step.get('with', {}).get('name') == artifact for step in publish['steps'])
        assert not any('checkout' in step.get('uses', '') for step in publish['steps'])
        assert not any('secrets.' in json.dumps(step) or 'NODE_AUTH_TOKEN' in json.dumps(step) for step in publish['steps'])
        # Changed assertions (name kept as the Tiden identity): the guard is found by
        # name, runs like a GitHub step (bash -e) in a synthetic clone whose release
        # tag is on main, and npm from refs/heads/main is no longer asserted allowed.
        guard = next(step for step in jobs['tag']['steps'] if step.get('name') == 'Require the version tag')['run']
        allowed = ['refs/tags/v1.2.3']
        for ref in [*allowed, 'refs/heads/other', 'refs/tags/v1.2.4']:
            result = run_release_guard(guard, ref, '1.2.3', release_clone)
            assert (result.returncode == 0) == (ref in allowed), (kind, ref)
        if kind == 'npm':
            assert jobs['checks']['with']['source_ref'] == 'refs/tags/v${{ inputs.version }}'
            assert publish['steps'][-1]['run'] == 'npm publish ./dist/*.tgz --access public --provenance'


def test_checks_verify_fails_on_a_commit_it_did_not_test():
    checks = document('.github/workflows/checks.yml')
    trigger = checks.get('on', checks.get(True))['workflow_call']
    assert trigger['outputs']['source_sha']['value'] == '${{ jobs.verify.outputs.source_sha }}'
    verify = checks['jobs']['verify']
    assert verify['outputs']['source_sha'] == '${{ steps.source.outputs.sha }}'
    steps = verify['steps']
    source = next(step for step in steps if step.get('id') == 'source')
    assert 'echo "sha=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"' in source['run']
    index = next(i for i, step in enumerate(steps) if step.get('name', '').startswith('Require the tested source'))
    guard = steps[index]
    # Runs even after failed checks, after baseline evidence, before any artifact a release could use.
    assert guard['if'] == '${{ always() }}'
    assert steps[index - 1]['name'] == 'Retain test evidence even when a check fails'
    assert [step['with']['name'] for step in steps[index + 1:]] == ['python-dist', 'npm-dist']
    assert guard['env'] == {'SOURCE_SHA': '${{ steps.source.outputs.sha }}', 'EXPECTED_SHA': '${{ github.sha }}'}
    for source_sha, passes in [('a' * 40, True), ('b' * 40, False), ('', False)]:
        result = subprocess.run(['bash', '-e', '-c', guard['run']],
                                env={'SOURCE_SHA': source_sha, 'EXPECTED_SHA': 'a' * 40}, capture_output=True)
        assert (result.returncode == 0) == passes, source_sha


@pytest.mark.parametrize('kind', ['pypi', 'npm'])
def test_release_tag_must_be_the_dispatched_commit_already_on_main(tmp_path, kind):
    steps = document(f'.github/workflows/publish-{kind}.yml')['jobs']['tag']['steps']
    checkout, guard = steps
    assert checkout['uses'].startswith('actions/checkout@') and re.fullmatch(r'[0-9a-f]{40}', checkout['uses'].split('@')[1])
    assert checkout['with'] == {'persist-credentials': False, 'fetch-depth': 0}
    assert guard['name'] == 'Require the version tag'
    assert guard['env'] == {'VERSION': '${{ inputs.version }}', 'REF': '${{ github.ref }}', 'SHA': '${{ github.sha }}'}
    release = synthetic_release_clone(tmp_path)
    commits = release[1]
    cases = [('refs/tags/v1.2.3', '1.2.3', None, True),
             ('refs/heads/main', '1.2.3', commits['main-head'], False),  # npm no longer publishes from main
             ('refs/tags/v1.2.5', '1.2.5', None, False),  # tag outside main
             ('refs/tags/v1.2.3', '1.2.3', commits['main-head'], False),  # tag moved after dispatch
             ('refs/tags/v1.2.9', '1.2.9', commits['main-head'], False)]  # tag missing
    for ref, version, sha, passes in cases:
        result = run_release_guard(guard['run'], ref, version, release, sha)
        assert (result.returncode == 0) == passes, (kind, ref, version)


@pytest.mark.parametrize('name', ['mcp.new-client', 'uvicorn.error', 'psycopg.pool.later',
    'httpx.transport', 'httpcore.http11', 'telethon.client', 'personal_os.telegram.transport.new'])
def test_new_library_logger_redacts_payload_arguments_exception_and_stack(name):
    from messaging_service.security_logging import redact_library_logs
    previous = logging.getLogRecordFactory()
    try:
        redact_library_logs()
        installed = logging.getLogRecordFactory()
        redact_library_logs()
        assert logging.getLogRecordFactory() is installed
        try:
            raise ValueError('synthetic-private-exception')
        except ValueError:
            record = logging.getLogger(name).makeRecord(name, logging.ERROR, 'private-path', 1,
                'synthetic-private-body %r', ({'token': 'synthetic-private-token'},),
                sys.exc_info(), func='synthetic', sinfo='synthetic-private-stack')
        assert record.getMessage() == 'library_event exception_type=ValueError'
        assert record.name.startswith('personal_os_messaging.library.')
        assert record.exc_info is record.exc_text is record.stack_info is None
        assert 'synthetic-private' not in logging.Formatter().format(record)
    finally:
        logging.setLogRecordFactory(previous)


def test_proxy_template_removes_whole_request_and_response_headers_from_log_entries():
    # Parse command/block paths, so directives in another block cannot satisfy this.
    stack, commands = [], []
    for line in (ROOT / 'deploy/Caddyfile.example').read_text().splitlines():
        tokens = shlex.split(line, comments=True)
        if not tokens:
            continue
        if tokens == ['}']:
            stack.pop()
        elif tokens[-1] == '{':
            stack.append(tuple(tokens[:-1]))
        else:
            commands.append((tuple(stack), tuple(tokens)))
    assert not stack
    field_path = ((), ('log', 'default'), ('format', 'filter'), ('fields',))
    assert (field_path, ('request', 'delete')) in commands
    assert (field_path, ('resp_headers', 'delete')) in commands
    assert (((('messaging.example.invalid',),), ('reverse_proxy', 'api:8080'))) in commands


def test_seccomp_template_is_default_deny_with_narrow_namespace_exceptions():
    profile = json.loads((ROOT / 'deploy/chromium-seccomp.json').read_text())
    assert profile['defaultAction'] == 'SCMP_ACT_ERRNO'
    unshare = [rule for rule in profile['syscalls'] if 'unshare' in rule['names']
               and rule['action'] == 'SCMP_ACT_ALLOW' and not rule.get('includes', {}).get('caps')]
    assert len(unshare) == 1
    assert unshare[0]['args'] == [{'index': 0, 'value': 268435456, 'op': 'SCMP_CMP_EQ'}]
    clone3 = [rule for rule in profile['syscalls'] if 'clone3' in rule['names']]
    assert any(rule['action'] == 'SCMP_ACT_ERRNO' and rule['errnoRet'] == 38 for rule in clone3)


def test_offline_chromium_preflight_fails_closed_on_missing_sandbox(tmp_path):
    # VM dependencies are fake modules; no Chromium, session or provider network.
    harness = tmp_path / 'browser-preflight.mjs'
    harness.write_text(r'''
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const source=fs.readFileSync(process.argv[2],'utf8');
for(const missing of [null,'PID namespaces','Network namespaces','Seccomp-BPF sandbox','provider-import']){
  const seen=[];
  const page={goto:async url=>seen.push(url), title:async()=> 'sandbox-smoke',
    evaluate:async()=> ['PID namespaces','Network namespaces','Seccomp-BPF sandbox']
      .map(key=>key+'\t'+(key===missing?'No':'Yes')).join('\n')};
  const browser={newPage:async()=>page,version:async()=> 'Chromium synthetic',
    close:async()=>seen.push('closed')};
  const launch=async options=>{
    assert.equal(options.executablePath,'/usr/bin/chromium');
    assert.ok(!options.args.includes('--no-sandbox'));
    assert.equal(options.userDataDir,'/tmp/pos-browser-smoke');
    return browser;
  };
  const context=vm.createContext({console:{log:()=>{}},Error});
  const module=new vm.SourceTextModule(source,{context});
  await module.link(name=>new vm.SyntheticModule(['default'],function(){
    this.setExport('default',name==='puppeteer'?{launch}:{Client:missing==='provider-import'?null:function(){}});
  },{context}));
  if(missing)await assert.rejects(module.evaluate(),new RegExp(missing==='provider-import'?'provider_import_failed':'sandbox_incomplete'));
  else await module.evaluate();
  if(missing==='provider-import')assert.deepEqual(seen,[]);
  else assert.deepEqual(seen,['data:text/html,<title>sandbox-smoke</title>','chrome://sandbox','closed']);
}
''')
    result = subprocess.run(['node', '--experimental-vm-modules', str(harness),
        str(ROOT / 'whatsapp/browser-smoke.mjs')], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_telemetry_safe_frames_and_delivery_failures_do_not_escape(monkeypatch):
    from messaging_service import telemetry
    event = telemetry.safe_event(RuntimeError('synthetic-private'), 'gateway', 'request_failed')
    event.update(event_id='a' * 32, timestamp=1, environment='local-synthetic')
    event['exception']['values'][0]['stacktrace']['frames'] = [
        {'filename': 'services/messaging/messaging_service/app.py', 'lineno': n + 1, 'locals': {'secret': 'private'}}
        for n in range(30)]
    safe = telemetry.before_send(event)
    frames = safe['exception']['values'][0]['stacktrace']['frames']
    assert len(frames) == 16 and frames[0]['lineno'] == 15 and frames[-1]['lineno'] == 30
    assert all(set(frame) == {'filename', 'lineno', 'in_app'} for frame in frames)
    attempted = threading.Event()
    class Client:
        def capture_event(self, event):
            attempted.set()
            raise RuntimeError('synthetic-private-transport')
    worker = telemetry.Reporter(Client(), 'gateway', 'local-synthetic')
    try:
        assert worker.capture(RuntimeError('private'), 'request_failed')
        assert attempted.wait(1)
        worker.queue.join()
        assert worker.thread.is_alive()
        assert worker.capture(ValueError('private'), 'tool_failed')
    finally:
        worker.close()
    assert not worker.thread.is_alive()


def test_enabled_telemetry_retains_hooks_and_safe_sdk_configuration(monkeypatch, tmp_path):
    from messaging_service import telemetry
    calls = []
    class Client:
        def __init__(self, dsn, **settings):
            calls.append(('sdk', dsn, settings))
    class Reporter:
        def __init__(self, client, component, environment):
            calls.append(('reporter', component, environment))
        def capture(self, error, code):
            calls.append(('capture', type(error).__name__, code))
            return True
        def close(self):
            calls.append(('close',))
    monkeypatch.setitem(sys.modules, 'tiden_telemetry', SimpleNamespace(Client=Client))
    monkeypatch.setattr(telemetry, 'Reporter', Reporter)
    monkeypatch.setattr(telemetry, '_reporter', None)
    previous = lambda *args: calls.append(('previous-main', args[0].__name__))
    previous_thread = lambda args: calls.append(('previous-thread', type(args.exc_value).__name__))
    monkeypatch.setattr(sys, 'excepthook', previous)
    monkeypatch.setattr(threading, 'excepthook', previous_thread)
    monkeypatch.setattr(atexit, 'register', lambda callback: calls.append(('atexit', callback)))
    dsn = tmp_path / 'synthetic.dsn'
    dsn.write_text('https://synthetic-key@ingest.example.test/synthetic-project')
    monkeypatch.setenv('ANYMSG_TIDEN_ENABLED', '1')
    monkeypatch.setenv('ANYMSG_TIDEN_DSN_FILE', str(dsn))
    monkeypatch.setenv('ANYMSG_TIDEN_ENVIRONMENT', 'local-synthetic')
    assert telemetry.configure('gateway')
    sdk = calls[0]
    assert sdk[2] == {'send_default_pii': False, 'before_send': telemetry.before_send,
        'http_timeout': 1.0, 'install_excepthook': False}
    assert calls[1] == ('reporter', 'gateway', 'local-synthetic')
    sys.excepthook(RuntimeError, RuntimeError('private'), None)
    threading.excepthook(SimpleNamespace(exc_value=ValueError('private')))
    assert ('capture', 'RuntimeError', 'uncaught_exception') in calls
    assert ('previous-main', 'RuntimeError') in calls and ('close',) in calls
    assert ('capture', 'ValueError', 'uncaught_exception') in calls
    assert ('previous-thread', 'ValueError') in calls


def test_disabled_telemetry_keeps_hooks_and_does_not_start_thread_or_import_sdk(monkeypatch):
    import builtins
    from messaging_service import telemetry
    previous_import = builtins.__import__
    previous_hook, previous_thread_hook = sys.excepthook, threading.excepthook
    def guarded(name, *args, **kwargs):
        assert name != 'tiden_telemetry'
        return previous_import(name, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError('disabled diagnostics must not start a worker')
    monkeypatch.setattr(telemetry, '_reporter', None)
    monkeypatch.delenv('ANYMSG_TIDEN_ENABLED', raising=False)
    monkeypatch.setattr(builtins, '__import__', guarded)
    monkeypatch.setattr(threading, 'Thread', forbidden)
    assert telemetry.configure('gateway') is False
    assert telemetry.capture(RuntimeError('private'), 'request_failed') is False
    assert sys.excepthook is previous_hook and threading.excepthook is previous_thread_hook


def test_whatsapp_top_level_failure_reports_code_without_exception_text(monkeypatch, capsys):
    from messaging_service import telemetry, whatsapp_collector
    seen = []
    monkeypatch.setattr(telemetry, 'configure', lambda component: seen.append(('configure', component)))
    monkeypatch.setattr(telemetry, 'capture', lambda error, code: seen.append(('capture', type(error).__name__, code)))
    async def failed(config):
        assert config == Path('/synthetic/config.json')
        raise RuntimeError('synthetic-private-session-detail')
    monkeypatch.setattr(whatsapp_collector, 'run_config', failed)
    monkeypatch.setattr(whatsapp_collector.os, 'umask', lambda mode: None)
    monkeypatch.setattr(sys, 'argv', ['anymsg', '--config', '/synthetic/config.json'])
    with pytest.raises(SystemExit) as result:
        whatsapp_collector.main()
    assert result.value.code == 1
    assert seen == [('configure', 'whatsapp'), ('capture', 'RuntimeError', 'collector_failed')]
    assert json.loads(capsys.readouterr().out) == {'status': 'stopped', 'code': 'collector_failed'}


# spec: ANYM-310
def test_enrichment_grace_period_follows_the_teardown_bound_not_the_job_timeout():
    for name in ('enricher', 'enricher-local'):
        assert units()[name]['stop_grace_period'] == '90s'
    assert 'teardown' in (ROOT / 'deploy/compose.enrichment.yaml').read_text()
