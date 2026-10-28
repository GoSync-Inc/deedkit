"""Render opt-in deployment without opening secrets or starting any service."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

@pytest.mark.skipif(
    shutil.which('docker') is None and not os.environ.get('CI'),
    reason='Compose rendering needs the host Docker CLI; the isolated test image intentionally has no Docker CLI or socket.',
)
def test_subscription_overlay_is_opt_in_and_has_bounded_runtime_authority(tmp_path):
    assert shutil.which('docker') is not None, 'CI must provide the Docker CLI for the Compose deployment proof'
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PROBE_PUBLIC_URL': 'https://example.invalid',
           'PROBE_ALLOWED_HOSTS': 'example.invalid'}
    overlay = yaml.safe_load((root / 'deploy/compose.subscriptions.yaml').read_text())
    source_volumes = overlay['services']['subscriptions']['volumes']
    assert len(source_volumes) == 1
    # Raw Compose defaults host-path creation to true: require explicit false.
    assert source_volumes[0]['bind']['create_host_path'] is False
    # Canonical rendering resolves env_file on older Compose versions. Give it
    # only a synthetic DB fixture; retain the real project directory so builds
    # and bind mounts still resolve against the original deployment template.
    base = yaml.safe_load((root / 'deploy/compose.yaml').read_text())
    assert base['services']['db']['env_file'] == './private/postgres.env'
    database_environment = {'POSTGRES_USER': 'synthetic-render-owner',
                            'POSTGRES_PASSWORD': 'synthetic-render-only',
                            'POSTGRES_DB': 'synthetic-render'}
    fixture_env = tmp_path / 'postgres.synthetic.env'
    fixture_env.write_text(''.join(f'{key}={value}\n' for key, value in database_environment.items()))
    base['services']['db']['env_file'] = str(fixture_env)
    fixture_base = tmp_path / 'compose.synthetic.yaml'
    fixture_base.write_text(yaml.safe_dump(base, sort_keys=False))

    def render(*files):
        command = ['docker', 'compose', '--project-directory', str(root / 'deploy')]
        for file in files:
            command += ['-f', str(fixture_base if file == 'compose.yaml' else root / 'deploy' / file)]
        result = subprocess.run(command + ['config', '--format', 'json'],
                                env=env, text=True, capture_output=True, timeout=20)
        assert result.returncode == 0, f'Compose render failed ({result.returncode}): {result.stderr}'
        config = json.loads(result.stdout)
        assert config['services']['db']['environment'] == database_environment
        return config

    assert 'subscriptions' not in render('compose.yaml')['services']
    config = render('compose.yaml', 'compose.subscriptions.yaml')
    service = config['services']['subscriptions']
    assert service['build']['target'] == 'runtime'
    assert Path(service['build']['context']) == root
    assert service['command'] == ['python', '-m', 'messaging_service.subscription_worker']
    assert service['init'] is True and service['restart'] == 'unless-stopped'
    assert service['depends_on']['db']['condition'] == 'service_healthy'
    assert set(service['networks']) == {'backend'}
    assert config['networks']['backend']['internal'] is True
    assert not service.get('ports')
    assert service['environment'] == {'DATABASE_URL_FILE': '/run/secrets/database_url'}
    assert len(service['volumes']) == 1
    secret = service['volumes'][0]
    assert secret['type'] == 'bind' and secret['read_only'] is True
    assert Path(secret['source']) == root / 'deploy/private/subscriptions.dsn'
    assert secret['target'] == '/run/secrets/database_url'
    # Older compose-go bool/omitempty serialization omits false from JSON.
    # The strict raw-YAML check above distinguishes omission from its default.
    assert secret.get('bind', {}).get('create_host_path', False) is False
    assert service['read_only'] is True and service['user'] == '10001:10001'
    assert service['cap_drop'] == ['ALL']
    assert 'no-new-privileges:true' in service['security_opt']
    assert service['tmpfs'] == ['/tmp:size=16m,noexec,nosuid']
    assert int(service['mem_limit']) == 256 * 1024**2
    assert service['cpu_shares'] == 512 and service['pids_limit'] == 32
    assert service['stop_grace_period'] == '15s'
    assert service['logging'] == {'driver': 'json-file',
                                   'options': {'max-size': '5m', 'max-file': '2'}}


# spec: ANYM-534
@pytest.mark.skipif(
    shutil.which('docker') is None and not os.environ.get('CI'),
    reason='Compose rendering needs the host Docker CLI; the isolated test image intentionally has no Docker CLI or socket.',
)
def test_delivery_overlay_isolates_the_dispatcher_database_path_and_egress(tmp_path):
    assert shutil.which('docker') is not None, 'CI must provide the Docker CLI for the Compose deployment proof'
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PROBE_PUBLIC_URL': 'https://example.invalid', 'PROBE_ALLOWED_HOSTS': 'example.invalid'}
    raw = yaml.safe_load((root / 'deploy/compose.delivery.yaml').read_text())
    for name in ('delivery', 'delivery-egress', 'api'):
        # Raw Compose defaults host-path creation to true: require explicit false on every bind.
        assert all(volume['bind']['create_host_path'] is False for volume in raw['services'][name]['volumes']), name
    base = yaml.safe_load((root / 'deploy/compose.yaml').read_text())
    fixture_env = tmp_path / 'postgres.synthetic.env'
    fixture_env.write_text('POSTGRES_USER=synthetic\nPOSTGRES_PASSWORD=synthetic\nPOSTGRES_DB=synthetic\n')
    base['services']['db']['env_file'] = str(fixture_env)
    fixture_base = tmp_path / 'compose.synthetic.yaml'
    fixture_base.write_text(yaml.safe_dump(base, sort_keys=False))
    command = ['docker', 'compose', '--project-directory', str(root / 'deploy'), '-f', str(fixture_base)]
    for name in ('compose.subscriptions.yaml', 'compose.delivery.yaml'):
        command += ['-f', str(root / 'deploy' / name)]
    result = subprocess.run(command + ['config', '--format', 'json'], env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, f'Compose render failed ({result.returncode}): {result.stderr}'
    config = json.loads(result.stdout)
    services, networks = config['services'], config['networks']
    service = services['delivery']
    assert service['command'] == ['python', '-m', 'messaging_service.delivery_worker']
    assert service['build']['target'] == 'runtime' and service['init'] is True and service['restart'] == 'unless-stopped'
    # ANYM-581: only internal networks, the database one and the proxy one; never backend, never an egress bridge.
    assert set(service['networks']) == {'delivery_db', 'delivery_proxy'}
    assert {name for name, item in services.items() if 'delivery_db' in item['networks']} == {'db', 'delivery'}
    assert {name for name, item in services.items() if 'delivery_proxy' in item['networks']} == {'delivery', 'delivery-egress'}
    assert {name for name, item in services.items() if 'delivery_egress' in item['networks']} == {'delivery-egress'}
    assert networks['delivery_proxy']['internal'] is True and networks['delivery_proxy'].get('enable_ipv6') is False
    assert networks['delivery_proxy']['driver_opts'] == {'com.docker.network.bridge.name': 'dk-deliverypx0',
                                                     'com.docker.network.bridge.gateway_mode_ipv4': 'isolated'}
    assert set(services['db']['networks']) == {'backend', 'delivery_db'}
    assert set(services['subscriptions']['networks']) == {'backend'}
    assert networks['delivery_db']['internal'] is True and networks['delivery_db'].get('enable_ipv6') is False
    assert networks['delivery_db']['driver_opts'] == {'com.docker.network.bridge.name': 'dk-deliverydb0',
                                                     'com.docker.network.bridge.gateway_mode_ipv4': 'isolated'}
    # No resolver of its own: names outside its networks never resolve; the proxy resolves.
    assert 'dns' not in service
    assert not networks['delivery_egress'].get('internal') and networks['delivery_egress'].get('enable_ipv6') is False
    assert networks['delivery_egress']['driver_opts'] == {'com.docker.network.bridge.name': 'dk-delivery0'}
    assert not service.get('ports')
    assert service['environment'] == {'DATABASE_URL_FILE': '/run/secrets/database_url',
                                      'DELIVERY_POLICY_FILE': '/run/secrets/delivery-policy.json',
                                      'DELIVERY_EGRESS_PROXY': 'delivery-egress:3128'}
    assert service['depends_on']['delivery-egress']['condition'] == 'service_started'
    mounts = {volume['target']: volume for volume in service['volumes']}
    assert set(mounts) == {'/run/secrets/database_url', '/run/secrets/delivery-policy.json', '/run/secrets/receivers'}
    assert all(volume['type'] == 'bind' and volume['read_only'] is True for volume in mounts.values())
    assert Path(mounts['/run/secrets/database_url']['source']) == root / 'deploy/private/delivery.dsn'
    assert Path(mounts['/run/secrets/delivery-policy.json']['source']) == root / 'deploy/private/delivery-policy.json'
    assert Path(mounts['/run/secrets/receivers']['source']) == root / 'deploy/private/delivery-receivers'
    assert service['read_only'] is True and service['user'] == '10001:10001' and service['cap_drop'] == ['ALL']
    assert 'no-new-privileges:true' in service['security_opt']
    assert service['tmpfs'] == ['/tmp:size=16m,noexec,nosuid']
    assert int(service['mem_limit']) == 256 * 1024**2 and service['cpu_shares'] == 512 and service['pids_limit'] == 64
    assert service['logging'] == {'driver': 'json-file', 'options': {'max-size': '5m', 'max-file': '2'}}
    # The proxy: no database and no secret, the policy file only; the egress bridge with an explicit resolver.
    proxy = services['delivery-egress']
    assert proxy['command'] == ['python', '-m', 'messaging_service.egress_proxy'] and proxy['image'] == service['image']
    assert set(proxy['networks']) == {'delivery_proxy', 'delivery_egress'} and proxy['dns'] == ['1.1.1.1']
    assert proxy['environment'] == {'DELIVERY_POLICY_FILE': '/run/secrets/delivery-policy.json'}
    assert [(v['target'], v['read_only']) for v in proxy['volumes']] == [('/run/secrets/delivery-policy.json', True)]
    assert Path(proxy['volumes'][0]['source']) == root / 'deploy/private/delivery-policy.json'
    assert proxy['read_only'] is True and proxy['user'] == '10001:10001' and proxy['cap_drop'] == ['ALL']
    assert 'no-new-privileges:true' in proxy['security_opt'] and proxy['init'] is True and not proxy.get('ports')
    assert proxy['tmpfs'] == ['/tmp:size=16m,noexec,nosuid'] and int(proxy['mem_limit']) == 128 * 1024**2
    assert proxy['pids_limit'] == 128 and proxy['logging'] == service['logging']
    # The API reads the same host policy for registration; nothing else of the API changes.
    api = services['api']
    assert api['environment']['DELIVERY_POLICY_FILE'] == '/run/secrets/delivery-policy.json'
    policy = [volume for volume in api['volumes'] if volume['target'] == '/run/secrets/delivery-policy.json']
    assert len(policy) == 1 and policy[0]['read_only'] is True
