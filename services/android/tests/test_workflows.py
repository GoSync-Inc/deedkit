"""Regression coverage for agent-written flows at the USB host boundary."""

import time

import pytest

from deedkit_android.config import Refused
from deedkit_android.flows import compile_flow
from deedkit_android.store import Store


# spec: ANYM-951
def test_flow_compilation_keeps_text_literal_and_refuses_host_execution():
    commands = compile_flow('- inputText: ${QUERY}\n- pressKey: Enter', {'QUERY': 'чай ☕'})
    assert commands[0] == {'inputText': 'чай ☕'}
    for source in ('- runScript: /etc/passwd', '- evalScript: ${1+1}',
                   '- runFlow: ../secret.yaml', '- inputText: ${env.HOME}',
                   '- tapOn: {text: hi, file: /etc/passwd}',
                   '- inputText: &x hi\n- inputText: *x'):
        with pytest.raises(Refused):
            compile_flow(source, {})
    with pytest.raises(Refused):
        compile_flow('- inputText: ${QUERY}', {'QUERY': '${java.lang.System.exit(0)}'})


# spec: ANYM-951
def test_checkpoint_is_explicit_and_launch_preserves_current_app_state():
    commands = compile_flow('- launchApp: com.example.synthetic\n- checkpoint: verify\n- back', {})
    assert commands[0]['launchApp']['stopApp'] is False
    assert commands[0]['launchApp']['permissions'] == {'all': 'unset'}
    assert commands[1] == {'checkpoint': 'verify'}
    with pytest.raises(Refused):
        compile_flow('- repeat: {times: 2, commands: [{checkpoint: nested}]}', {})


# spec: ANYM-951
def test_activity_renewal_retains_token_isolation_and_bounds_abandonment(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, 'time', lambda: now[0])
    store = Store(tmp_path / 'state')
    try:
        session = store.reserve('one', 'phone', 'acquire', seconds=30)
        now[0] += 20
        store.touch('one', 'phone', session['session_id'])
        with pytest.raises(Refused):
            store.touch('one', 'phone', 'another-agent-token')
        now[0] += 20
        store.check_reservation('one', 'phone', session['session_id'])
        now[0] += 31
        with pytest.raises(Refused):
            store.check_reservation('one', 'phone', session['session_id'])
    finally:
        store.close()


# spec: ANYM-951
def test_saved_flows_are_owner_scoped_and_versioned(tmp_path):
    store = Store(tmp_path / 'state')
    try:
        first = store.flow('one', 'put', 'search', '- back', None)
        assert store.flow('one', 'list', None, None, None)['flows'][0]['version'] == first['version']
        with pytest.raises(Refused):
            store.flow('two', 'get', 'search', None, None)
        with pytest.raises(Refused):
            store.flow('one', 'put', 'search', '- scroll', 'stale')
        assert store.flow('one', 'get', 'search', None, first['version'])['yaml'] == '- back'
    finally:
        store.close()


# spec: ANYM-951
def test_compact_maestro_hierarchy_preserves_legacy_nodes_and_masks_inputs(tmp_path, monkeypatch):
    # Maestro's compact hierarchy was removed (owner 11-11); uiautomator2 keeps ADB names and masks inputs.
    import asyncio
    from types import SimpleNamespace
    from deedkit_android.device import Devices

    async def scenario():
        store = Store(tmp_path / 'state')
        devices = Devices(SimpleNamespace(devices={'phone': 'SERIAL'}, adb='/synthetic', uiautomator2_jar='/synthetic',
                                          state_dir=str(tmp_path / 'state')), store)
        async def call(device, command, **options):
            return {'xml': '<hierarchy><node text="Search" bounds="[0,0][10,20]" clickable="true" />'
                           '<node class="android.widget.EditText" text="private" content-desc="private" password="true" />'
                           '</hierarchy>'}
        monkeypatch.setattr(devices.ui, 'call', call)
        try:
            return await devices.capture_hierarchy('phone')
        finally:
            await devices.close()
            store.close()
    result = asyncio.run(scenario())
    nodes = result['elements']
    assert result['hierarchy_source'] == 'uiautomator2'
    assert nodes[0]['text'] == 'Search' and nodes[0]['clickable'] == 'true'
    assert nodes[0]['bounds'] == '[0,0][10,20]'
    assert nodes[1]['text'] == '' and nodes[1]['content-desc'] == ''


# spec: ANYM-951
def test_flow_nested_expressions_and_file_matchers_are_rejected():
    for source in ('- runFlow: {when: {script: "java.lang.System.exit(0)"}, commands: [back]}',
                   '- tapOn: {text: hi, below: {image: /etc/passwd}}',
                   '- scrollUntilVisible: {element: {image: /etc/passwd}}',
                   "- launchApp: {appId: com.example, arguments: {'${1+1}': x}}"):
        with pytest.raises(Refused):
            compile_flow(source, {})


# spec: ANYM-951
def test_workflow_execution_checkpoint_and_replay_use_one_device_lock(tmp_path, monkeypatch):
    import asyncio
    import uuid
    from types import SimpleNamespace
    from deedkit_android.device import Devices

    async def scenario():
        store = Store(tmp_path / 'state')
        devices = Devices(SimpleNamespace(devices={'phone': 'SERIAL'}, adb='/synthetic', uiautomator2_jar='/synthetic',
                                          state_dir=str(tmp_path / 'state')), store)
        calls = []
        async def run(device, command, **options):
            calls.append(command)
            await asyncio.sleep(0.02)
            return {'success': True}
        monkeypatch.setattr(devices.ui, 'call', run)
        token = store.reserve('one', 'phone', 'acquire')['session_id']
        request_id = str(uuid.uuid4())
        flow = compile_flow('- inputText: hello\n- checkpoint: verify\n- back', {})
        try:
            first = await devices.execute('one', 'phone', token, request_id, None, 3, 0, flow)
            assert first['state'] == 'running'
            with pytest.raises(Refused, match='device_busy'):
                await devices.execute('one', 'phone', token, str(uuid.uuid4()), None, 3, 0, flow)
            completed = await devices.receipt('one', request_id, 3)
            assert completed['state'] == 'paused' and completed['remaining_yaml'].strip() == '- back'
            assert calls == [{'inputText': 'hello'}, '__guard__']  # Then the guard's screen check.
            repeated = await devices.execute('one', 'phone', token, request_id, None, 3, 3, flow)
            assert repeated == completed and len(calls) == 2  # A replay calls the device no more.
        finally:
            await devices.close()
            store.close()
    asyncio.run(scenario())


# spec: ANYM-951
def test_maestro_lock_wait_is_bounded_without_stopping_another_device(tmp_path):
    # The Maestro engine was removed (owner 11-11): a host config that still names it is refused at start.
    import hashlib
    import json
    from deedkit_android.config import Configuration

    base = {'adb': '/synthetic/adb', 'state_dir': str(tmp_path / 'state'), 'devices': {'home': 'SERIAL'},
            'principals': [{'id': 'one', 'key_sha256': hashlib.sha256(b'one').hexdigest(), 'devices': ['home'],
                            'permissions': ['android:read']}]}
    config = tmp_path / 'config.json'
    for extra in ({'maestro': '/opt/maestro/bin/maestro'}, {'java_home': '/opt/java'}):
        config.write_text(json.dumps({**base, **extra}))
        config.chmod(0o600)
        with pytest.raises(ValueError, match='maestro_engine_removed'):
            Configuration(config)
    config.write_text(json.dumps(base))
    assert Configuration(config).initial.maestro is None


# spec: ANYM-951
def test_guardian_stops_child_when_parent_is_gone_and_removes_private_temp(monkeypatch, capsys):
    # The Maestro guardian was removed; the uiautomator2 worker stops its device server when its parent's pipe closes.
    import io
    import logging
    import sys
    import deedkit_android.ui_worker as worker

    stopped = []

    class Device:
        def __init__(self, *args):
            self.args = args

        def jsonrpc_call(self, method, params=None, timeout=10):
            return '<hierarchy />'

        def stop_uiautomator(self):
            stopped.append(self.args)
    monkeypatch.setattr(worker, 'Device', Device)
    monkeypatch.setattr(worker.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(sys, 'argv', ['ui_worker', 'SERIAL', '/synthetic.jar', '/synthetic.marker'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"command": "__hierarchy__"}\n'))
    try:
        worker.main()
    finally:
        logging.disable(logging.NOTSET)
    assert stopped == [('SERIAL', '/synthetic.jar', '/synthetic.marker')]
    assert '<hierarchy />' in capsys.readouterr().out


# spec: ANYM-951
def test_failed_image_cancels_hierarchy_before_releasing_phone(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from deedkit_android.device import Devices

    async def scenario():
        store = Store(tmp_path / 'state')
        devices = Devices(SimpleNamespace(devices={'phone': 'SERIAL'}), store)
        ended = []
        async def image(*args):
            await asyncio.sleep(0.01)
            raise Refused('crop_outside_screen', 422)
        async def hierarchy(*args):
            try:
                await asyncio.sleep(30)
            finally:
                await asyncio.sleep(0.01)
                ended.append(True)
        monkeypatch.setattr(devices, 'capture_image', image)
        monkeypatch.setattr(devices, 'capture_hierarchy', hierarchy)
        token = store.reserve('one', 'phone', 'acquire')['session_id']
        try:
            with pytest.raises(Refused, match='crop_outside_screen'):
                await devices.observe('one', 'phone', token)
            assert ended and not devices.locks['phone'].locked()
        finally:
            await devices.close()
            store.close()
    asyncio.run(scenario())


# spec: ANYM-951
def test_guardian_stops_descendant_after_launcher_exits(tmp_path, monkeypatch):
    # After a worker is gone, its Android server is stopped by the token in the private marker before reuse.
    import asyncio
    import json
    from types import SimpleNamespace
    import deedkit_android.device as device_module
    from deedkit_android.uiautomator import Uiautomator

    ui = Uiautomator(SimpleNamespace(devices={'phone': 'SERIAL'}, adb='/synthetic/adb', uiautomator2_jar='/synthetic',
                                     state_dir=str(tmp_path)), 0)
    token = 'deedkit-uia2-' + 'a' * 32
    ui.marker('phone').write_text(json.dumps({'pid': 4242, 'token': token}))
    calls = []
    async def run_process(argv, timeout, *args, **kwargs):
        calls.append(argv)
        return 0, b'', b''
    monkeypatch.setattr(device_module, 'run_process', run_process)
    asyncio.run(ui.ensure_fenced_process_stopped('phone'))
    assert len(calls) == 1 and calls[0][:4] == ['/synthetic/adb', '-s', 'SERIAL', 'shell']
    assert 'kill 4242' in calls[0][4] and token in calls[0][4]
    assert not ui.marker('phone').exists()


# spec: ANYM-952
def test_extended_device_commands_and_relative_selectors_compile():
    commands = compile_flow('''
- tapOn: {text: Buy, below: {id: product}, childOf: {id: card}, containsChild: {text: In stock}}
- copyTextFrom: {id: order_number}
- pasteText
- setOrientation: PORTRAIT
- setLocation: {latitude: -8.5, longitude: 115.2}
- clearState: com.example.synthetic
- killApp: com.example.synthetic
''', {})
    assert commands[0]['tapOn']['below'] == {'id': 'product'}
    assert commands[2] == 'pasteText'


# spec: ANYM-952
def test_saved_flow_composition_is_bounded_and_uses_the_resolver():
    sources = {'child': '- inputText: ${QUERY}\n- back', 'cycle': '- runFlow: {name: cycle}'}
    seen = []
    def resolve(name, version):
        seen.append((name, version))
        return sources[name]
    result = compile_flow('- runFlow: {name: child, version: abc}', {'QUERY': 'чай'}, resolve=resolve)
    assert result == [{'runFlow': {'commands': [{'inputText': 'чай'}, 'back']}}]
    assert seen == [('child', 'abc')]
    with pytest.raises(Refused):
        compile_flow('- runFlow: {name: cycle}', {}, resolve=resolve)
    with pytest.raises(Refused):
        compile_flow('- runFlow: {file: /etc/passwd}', {}, resolve=resolve)


# spec: ANYM-952
def test_saved_subflows_cannot_hide_checkpoints_or_script_conditions():
    for source in ('- checkpoint: nested', '- runFlow: {when: {"true": "Java.type(1)"}, commands: [back]}'):
        with pytest.raises(Refused):
            compile_flow('- runFlow: {name: inner}', {}, resolve=lambda *args: source)
