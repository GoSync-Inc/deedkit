"""The self-service library is reachable through the normal HTTP entry points."""

import hashlib
import json
from types import SimpleNamespace
import uuid

from fastapi.testclient import TestClient

from deedkit_android.app import create_app


# spec: ANYM-954
def test_draft_test_publish_and_replay_after_repair_through_http(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'adb': '/synthetic/adb', 'state_dir': str(tmp_path / 'state'),
        'devices': {'home': 'SERIAL'}, 'principals': [{'id': 'one',
        'key_sha256': hashlib.sha256(b'one').hexdigest(), 'devices': ['home'],
        'permissions': ['android:read', 'android:control']}]}))
    config.chmod(0o600)
    app = create_app(config)
    seen = []

    async def call(device, command, **options):
        seen.append(command)
        return {'success': True}

    async def noop(*args):
        pass

    with TestClient(app) as client:
        app.state.devices.ui = SimpleNamespace(call=call, close=noop, ensure_fenced_process_stopped=noop)

        def post(name, args, code=200):
            response = client.post('/v1/' + name, json=args, headers={'Authorization': 'Bearer one'})
            assert response.status_code == code, response.text
            return response.json()

        map_args = {'action': 'draft', 'kind': 'map', 'name': 'shop',
                    'yaml': 'package: example.shop\nselectors: {orders: [{textExact: Orders}]}\n'
                            'screens: {orders: {require: [orders]}}'}
        map_version = post('flows', map_args)['version']
        source = 'map: shop\nsteps:\n  - tapElement: {selector: orders, expect_screen: orders}'
        version = post('flows', {'action': 'draft', 'name': 'orders', 'yaml': source})['version']
        session = post('session', {'action': 'acquire', 'device_id': 'home'})['session_id']
        args = {'device_id': 'home', 'session_id': session, 'request_id': str(uuid.uuid4()),
                'name': 'orders', 'version': version, 'stage': 'draft'}
        result = post('run_flow', args)
        assert result['state'] == 'succeeded' and len(result['library']['pins']) == 2
        post('flows', {'action': 'publish', 'kind': 'map', 'name': 'shop',
                       'version': map_version, 'test_request_id': args['request_id']})
        post('flows', {'action': 'publish', 'name': 'orders',
                       'version': version, 'test_request_id': args['request_id']})
        post('flows', {**map_args, 'base_version': map_version, 'yaml': map_args['yaml'] + '\n# repaired'})
        # Replay precedes all mutable head reads and lease checks.
        post('session', {'action': 'release', 'device_id': 'home', 'session_id': session})
        assert post('run_flow', args) == result
        assert len(seen) == 1
        post('run_flow', {**args, 'name': 'different'}, 409)


# spec: ANYM-954
def test_raw_input_cannot_inject_internal_maps_or_nested_checkpoints(tmp_path):
    from deedkit_android.app_maps import compile_scenario
    from deedkit_android.config import Refused
    from deedkit_android.store import Store
    import pytest

    store = Store(tmp_path / 'state')
    try:
        for source in ('- tapElement: {app_map: {}, selector: x}',
                       '- runFlow: {commands: [{checkpoint: nested}]}'):
            with pytest.raises(Refused):
                compile_scenario(store.library, 'one', source, {})
    finally:
        store.close()


# spec: ANYM-954
def test_typed_parameters_validate_through_the_legacy_backend(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'adb': '/synthetic/adb', 'state_dir': str(tmp_path / 'state'),
        'devices': {'home': 'SERIAL'}, 'principals': [{'id': 'one',
        'key_sha256': hashlib.sha256(b'one').hexdigest(), 'devices': ['home'],
        'permissions': ['android:read', 'android:control']}]}))
    config.chmod(0o600)
    with TestClient(create_app(config)) as client:
        response = client.post('/v1/maestro', headers={'Authorization': 'Bearer one'}, json={
            'action': 'validate', 'yaml': '- inputText: ${COUNT} ${ENABLED}',
            'parameters': {'COUNT': 3, 'ENABLED': True}})
        assert response.status_code == 200
        assert response.json()['commands'] == [{'inputText': '3 True'}]


# spec: ANYM-954
def test_uiautomator_yaml_validation_keeps_the_repair_hint():
    import pytest
    from deedkit_android.app_maps import load
    from deedkit_android.config import Refused

    with pytest.raises(Refused, match='invalid_flow_yaml') as failure:
        load('appId: example.shop\n---\n- back')
    assert 'one YAML document' in failure.value.hint
    assert 'steps' in failure.value.hint
