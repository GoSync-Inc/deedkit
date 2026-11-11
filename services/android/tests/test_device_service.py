import base64
import hashlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from deedkit_android.app import create_app
from deedkit_android.config import Refused
from deedkit_android.store import Store


@pytest.fixture
def service(tmp_path):
    adb = tmp_path / 'adb'
    adb.write_text('''#!/usr/bin/env python3
import os,sys,time
args=sys.argv[1:]
if args == ['devices','-l']:
 print('List of devices attached\\nSERIAL device product:synthetic')
elif args[2:4] == ['shell','count']:
 path=os.path.join(os.path.dirname(__file__),'executions')
 with open(path,'a') as f: f.write('x')
 print('done')
elif args[2:4] == ['shell','slow']:
 time.sleep(5)
elif args[2:4] == ['shell','echo']:
 print(' '.join(args[4:]))
elif args[2:4] == ['exec-out','screencap']:
 image=os.path.join(os.path.dirname(__file__),'screenshot.png')
 sys.stdout.buffer.write(open(image,'rb').read() if os.path.exists(image) else b'\\x89PNG\\r\\n\\x1a\\nsynthetic')
elif args[2:4] == ['exec-out','cat']:
 sys.stdout.buffer.write(b'\\x00\\xffsynthetic file')
elif args[2] == 'shell' and 'uiautomator' in args[3]:
 print('<?xml version="1.0"?><hierarchy><node text="secret" password="true"/><node text="Search" resource-id="search"/></hierarchy>')
elif args[2] in ('install', 'install-multiple'):
 paths=[arg for arg in args[3:] if arg.endswith('.apk')]
 if not paths or not args[-1].endswith('.apk'):
  sys.exit('need APK file on command line')
 for path in paths:
  with open(path, 'rb') as stream:
   assert stream.read() == b'APK'
 print('Success')
else:
 print('arguments:',repr(args))
''')
    adb.chmod(0o700)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({
        'adb': str(adb), 'state_dir': str(tmp_path / 'state'), 'devices': {'home': 'SERIAL'},
        'principals': [
            {'id': 'one', 'key_sha256': hashlib.sha256(b'one').hexdigest(),
             'devices': ['home'], 'permissions': ['android:read', 'android:control']},
            {'id': 'two', 'key_sha256': hashlib.sha256(b'two').hexdigest(),
             'devices': ['home'], 'permissions': ['android:read', 'android:control']},
            {'id': 'reader', 'key_sha256': hashlib.sha256(b'reader').hexdigest(),
             'devices': ['home'], 'permissions': ['android:read']},
        ]}))
    config.chmod(0o600)
    app = create_app(config)
    with TestClient(app) as client:
        yield client, tmp_path, config


def post(service, operation, values=None, key='one'):
    return service[0].post('/v1/' + operation, json=values or {}, headers={'Authorization': 'Bearer ' + key})


def session(service):
    response = post(service, 'session', {'device_id': 'home', 'action': 'acquire'})
    assert response.status_code == 200, response.text
    return response.json()['session_id']


def execute(service, token, request_id, argv, **kwargs):
    return post(service, 'execute', {'device_id': 'home', 'session_id': token,
                                   'request_id': request_id, 'argv': argv, **kwargs})


def test_auth_device_grants_and_reservation(service):
    assert post(service, 'capabilities', key='invalid').status_code == 401
    assert post(service, 'session', {'device_id': 'home', 'action': 'acquire'}, key='reader').status_code == 403
    assert post(service, 'session', {'device_id': 'other', 'action': 'acquire'}).status_code == 404
    token = session(service)
    assert post(service, 'session', {'device_id': 'home', 'action': 'acquire'}, key='two').status_code == 409
    assert post(service, 'observe', {'device_id': 'home', 'session_id': token}, key='two').status_code == 409
    observed = post(service, 'observe', {'device_id': 'home', 'session_id': token}).json()
    assert observed['elements'][0]['text'] == ''
    assert observed['elements'][1]['text'] == 'Search'
    assert base64.b64decode(observed['image']['data']).startswith(b'\x89PNG')


def test_replay_never_repeats_device_action_and_conflicts_are_rejected(service):
    token, request_id = session(service), str(uuid.uuid4())
    first = execute(service, token, request_id, ['shell', 'count'])
    assert first.status_code == 200 and first.json()['state'] == 'succeeded'
    repeated = execute(service, token, request_id, ['shell', 'count'])
    assert repeated.json() == first.json()
    assert (service[1] / 'executions').read_text() == 'x'
    assert execute(service, token, request_id, ['shell', 'echo', 'different']).status_code == 409
    assert post(service, 'get_operation', {'request_id': request_id}, key='two').status_code == 404


def test_expiry_revocation_and_busy_do_not_admit_a_second_command(service):
    token, first_id = session(service), str(uuid.uuid4())
    first = execute(service, token, first_id, ['shell', 'slow'], wait_s=0, timeout_s=1)
    assert first.json()['state'] == 'running'
    assert execute(service, token, str(uuid.uuid4()), ['shell', 'count']).json()['error'] == 'device_busy'
    receipt = post(service, 'get_operation', {'request_id': first_id, 'wait_s': 3}).json()
    assert receipt['state'] == 'unknown' and receipt['error'] == 'command_timeout'
    assert not (service[1] / 'executions').exists()
    config = json.loads(service[2].read_text())
    config['principals'] = [p for p in config['principals'] if p['id'] != 'one']
    service[2].write_text(json.dumps(config))
    assert post(service, 'capabilities').status_code == 401


def test_binary_files_retry_offsets_scope_and_device_only_commands(service):
    token, file_id = session(service), str(uuid.uuid4())
    values = {'file_id': file_id, 'offset': 0, 'data_base64': base64.b64encode(b'APK').decode(), 'final': True}
    assert post(service, 'upload', values).status_code == 200
    assert post(service, 'upload', values).status_code == 200
    assert post(service, 'upload', {**values, 'data_base64': 'eA=='}).status_code == 409
    assert post(service, 'download', {'file_id': file_id}, key='two').status_code == 404
    installed = execute(service, token, str(uuid.uuid4()), ['install', '-r', file_id]).json()
    assert installed['state'] == 'succeeded'
    assert execute(service, token, str(uuid.uuid4()), ['install', '/etc/passwd']).status_code == 422
    assert execute(service, token, str(uuid.uuid4()), ['-H', 'another-host', 'shell', 'id']).status_code == 422
    assert execute(service, token, str(uuid.uuid4()), ['wait-for-device', 'kill-server']).status_code == 422
    pulled = execute(service, token, str(uuid.uuid4()), ['pull', '/sdcard/item']).json()
    downloaded = post(service, 'download', {'file_id': pulled['download']['file_id']}).json()
    assert base64.b64decode(downloaded['data_base64']) == b'\x00\xffsynthetic file'
    assert post(service, 'delete_file', {'file_id': file_id}, key='two').status_code == 200
    assert post(service, 'download', {'file_id': file_id}).status_code == 200
    assert post(service, 'delete_file', {'file_id': file_id}).status_code == 200
    assert post(service, 'delete_file', {'file_id': file_id}).status_code == 200
    assert post(service, 'download', {'file_id': file_id}).status_code == 404


# Regression: ADB must only select APKs staged from this principal's completed uploads.
def test_install_stages_apks_and_rejects_unvalidated_host_operands(service):
    token = session(service)
    file_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    for file_id in file_ids:
        assert post(service, 'upload', {'file_id': file_id, 'offset': 0,
                    'data_base64': base64.b64encode(b'APK').decode(), 'final': True}).status_code == 200
    for argv in (
        ['install', '--no-streaming', '/existing/host/file.apk', file_ids[0]],
        ['install', file_ids[0], file_ids[1]],
        ['install-multiple', '--no-streaming', '/existing/host/file.apk', *file_ids],
        ['install', '--fastdeploy', file_ids[0]],
        ['install', '--user'],
        ['install-multiple', '-i', 'host.apk', *file_ids],
        ['install-multiple', *file_ids, '--no-streaming'],
    ):
        assert execute(service, token, str(uuid.uuid4()), argv).status_code == 422
    for argv in (
        ['install', '--no-streaming', file_ids[0]],
        ['install', '-r', '--user', '0', '--abi', 'arm64-v8a', file_ids[0]],
        ['install-multiple', '-r', *file_ids],
    ):
        result = execute(service, token, str(uuid.uuid4()), argv).json()
        assert result['state'] == 'succeeded', result
    assert post(service, 'download', {'file_id': file_ids[0]}).status_code == 200


def test_restart_marks_inflight_unknown_and_keeps_receipt(tmp_path):
    store = Store(tmp_path / 'state')
    request_id = str(uuid.uuid4())
    assert store.admit('one', request_id, {'argv': ['shell', 'action']})
    store.close()
    resumed = Store(tmp_path / 'state')
    try:
        assert resumed.operation('one', request_id)['state'] == 'unknown'
        assert not resumed.admit('one', request_id, {'argv': ['shell', 'action']})
        with pytest.raises(Refused):
            resumed.operation('two', request_id)
    finally:
        resumed.close()


def test_partial_chunk_recovery_and_exclusive_store(tmp_path):
    store = Store(tmp_path / 'state')
    try:
        with pytest.raises(ValueError, match='already_in_use'):
            Store(tmp_path / 'state')
        file_id = str(uuid.uuid4())
        store.upload('one', file_id, 0, '', False)
        store.path('one', file_id).write_bytes(b'par')
        result = store.upload('one', file_id, 0, base64.b64encode(b'partial').decode(), True)
        assert result['complete']
        assert store.file('one', file_id).read_bytes() == b'partial'
        assert store.upload('one', file_id, 7, '', True)['size'] == 7
        with pytest.raises(Refused, match='file_content_conflict'):
            store.upload('one', file_id, 7, 'eA==', True)
    finally:
        store.close()


def test_expired_reservation_and_shutdown_receipt(service):
    token = session(service)

    def expire():
        store = service[0].app.state.store
        store.db.execute('UPDATE reservations SET expires=0')
        store.db.commit()

    service[0].portal.call(expire)
    assert execute(service, token, str(uuid.uuid4()), ['shell', 'count']).status_code == 409
    token = session(service)
    request_id = str(uuid.uuid4())
    assert execute(service, token, request_id, ['shell', 'slow'], wait_s=0).json()['state'] == 'running'
    service[0].portal.call(service[0].app.state.devices.close)
    result = post(service, 'get_operation', {'request_id': request_id}).json()
    assert result['state'] == 'unknown' and result['error'] == 'service_stopped'


def test_gateway_catalog_matches_service_schemas():
    from pathlib import Path
    from deedkit_android.app import SCHEMAS

    catalog_path = Path(__file__).resolve().parents[2] / 'gateway/src/agent_gateway/catalog.json'
    tools = [row for row in json.loads(catalog_path.read_text()) if row['module'] == 'android']
    assert {row['operation'] for row in tools} == SCHEMAS.keys()
    for row in tools:
        schema = SCHEMAS[row['operation']].model_json_schema()
        schema.pop('title', None)
        assert row['inputSchema'] == schema


def test_hierarchy_timeout_still_returns_screenshot(service, monkeypatch):
    from deedkit_android import device

    original = device.run_process

    async def capture(argv, *args, **kwargs):
        if any('uiautomator' in arg for arg in argv):
            raise TimeoutError
        return await original(argv, *args, **kwargs)

    monkeypatch.setattr(device, 'run_process', capture)
    token = session(service)
    result = post(service, 'observe', {'device_id': 'home', 'session_id': token}).json()
    assert result['hierarchy_error'] == 'capture_failed'
    assert base64.b64decode(result['image']['data']).startswith(b'\x89PNG')


def test_high_resolution_phone_screenshot_exceeds_command_output_limit(service):
    # Regression: the real 1220x2712 home-screen PNG exceeded the command's 4 MiB limit.
    image = b'\x89PNG\r\n\x1a\n' + b'x' * (5 * 1024 * 1024)
    (service[1] / 'screenshot.png').write_bytes(image)
    result = post(service, 'observe', {'device_id': 'home', 'session_id': session(service)}).json()
    assert base64.b64decode(result['image']['data']) == image


# spec: ANYM-951
def test_screenshot_crop_geometry_file_mode_and_large_download(service):
    import io
    from PIL import Image
    original = Image.new('RGB', (1220, 2712), (10, 20, 30))
    original.save(service[1] / 'screenshot.png')
    token = session(service)
    args = {'device_id': 'home', 'session_id': token, 'hierarchy': False,
            'image_mode': 'both', 'crop': {'x': 100, 'y': 200, 'w': 900, 'h': 600}}
    result = post(service, 'observe', args).json()
    assert result['device_size'] == {'width': 1220, 'height': 2712}
    assert result['image_origin'] == {'x': 100, 'y': 200}
    assert result['image']['width'] == 900 and result['image']['height'] == 600
    data = base64.b64decode(result['image']['data'])
    assert Image.open(io.BytesIO(data)).getpixel((0, 0)) == (10, 20, 30)
    downloaded = post(service, 'download', {'file_id': result['image_file']['file_id'],
                                          'length': 4194304}).json()
    assert downloaded['eof'] and base64.b64decode(downloaded['data_base64']) == data
    file_only = post(service, 'observe', {**args, 'image_mode': 'file'}).json()
    assert 'image' not in file_only and file_only['image_file']['width'] == 900
    assert post(service, 'observe', {**args, 'crop': {'x': 1000, 'y': 200, 'w': 900, 'h': 600}}).status_code == 422


# spec: ANYM-957
def test_partial_download_names_the_next_chunk_and_the_larger_limit(service):
    from PIL import Image
    Image.effect_noise((400, 400), 100).save(service[1] / 'screenshot.png')
    token = session(service)
    result = post(service, 'observe', {'device_id': 'home', 'session_id': token, 'hierarchy': False,
                                       'image_mode': 'file'}).json()
    file_id = result['image_file']['file_id']
    first = post(service, 'download', {'file_id': file_id}).json()
    assert not first['eof'] and first['next_action'] == 'read_next_chunk' and first['max_length'] == 4194304
    whole = post(service, 'download', {'file_id': file_id, 'length': 4194304}).json()
    assert whole['eof'] and 'next_action' not in whole and 'max_length' not in whole


# spec: ANYM-958
def test_one_ui_engine_is_reported_and_flows_without_it_never_reach_the_device(service):
    capabilities = post(service, 'capabilities', key='reader').json()
    assert capabilities['ui']['engine'] == 'uiautomator2' and capabilities['ui']['configured'] is False
    assert capabilities['maestro']['removed'] is True
    refused = post(service, 'maestro', {'action': 'validate', 'yaml': '- setLocation: {latitude: 1, longitude: 2}'})
    assert refused.status_code == 422 and refused.json()['error'] == 'unsupported_uiautomator_command'
    assert 'android_execute' in refused.json()['hint']
    run = post(service, 'run_flow', {'device_id': 'home', 'session_id': session(service),
                                     'request_id': str(uuid.uuid4()), 'yaml': '- back'})
    assert run.status_code == 503 and run.json()['error'] == 'ui_engine_not_configured'
    assert not (service[1] / 'executions').exists()


# spec: ANYM-952
def test_maestro_authoring_requires_no_device_action_and_resolves_only_owned_flows(service):
    reference = post(service, 'maestro', {'action': 'reference'}, key='reader')
    assert reference.status_code == 200
    # The Maestro dialect reference was removed (owner 11-11); the uiautomator2 reference remains.
    assert reference.json()['engine'] == 'uiautomator2' and 'pasteText' not in reference.json()['commands']
    source = '- inputText: ${QUERY}'
    saved = post(service, 'flows', {'action': 'put', 'name': 'search', 'yaml': source}).json()
    outer = '- runFlow: {name: search}'
    assert post(service, 'flows', {'action': 'put', 'name': 'composed', 'yaml': outer}).status_code == 200
    result = post(service, 'maestro', {'action': 'validate', 'yaml': outer, 'parameters': {'QUERY': 'чай'}})
    assert result.status_code == 200 and result.json()['valid']
    assert result.json()['commands'] == [{'runFlow': {'commands': [{'inputText': 'чай'}]}}]
    assert post(service, 'maestro', {'action': 'validate', 'name': 'search',
                                    'version': saved['version'], 'parameters': {'QUERY': 'hello'}}, key='two').status_code != 200
    assert not (service[1] / 'executions').exists()
