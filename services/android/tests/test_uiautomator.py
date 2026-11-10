"""Persistent UI batches using synthetic device responses only."""

import asyncio
from types import SimpleNamespace

import pytest

from deedkit_android.config import Refused
from deedkit_android.flows import compile_flow as compile_base
from deedkit_android.uiautomator import UI_FIELDS


def compile_flow(source, parameters):
    return compile_base(source, parameters, fields=UI_FIELDS)


# spec: ANYM-953
def test_uiautomator_batches_keep_paths_values_and_step_timings():
    from deedkit_android.uiautomator import UiRunner

    async def scenario():
        calls = []
        async def call(command, **options):
            calls.append((command, options))
            return {'exists': False} if 'exists' in command else {'success': True}
        runner = UiRunner(call, 700, 150)
        await runner.run(compile_flow('- tapOn: {text: Search, timeout: 300}\n'
                                     '- exists: {id: optional}\n- inputText: Привет', {}))
        assert len(calls) == 3
        assert calls[0][1] == {'element_timeout_ms': 700, 'optional_timeout_ms': 150}
        assert [step['path'] for step in runner.steps] == [[0], [1], [2]]
        assert runner.steps[1]['result'] == {'exists': False}
        assert all(step['elapsed_ms'] >= 0 for step in runner.steps)
    asyncio.run(scenario())


# spec: ANYM-953
def test_uiautomator_validation_rejects_unsupported_options_before_any_action():
    from deedkit_android.uiautomator import validate_commands

    for source in ('- travel: {points: []}', '- tapOn: {image: local.png}',
                   '- launchApp: {appId: example, arguments: {a: b}}'):
        with pytest.raises(Refused):
            validate_commands(compile_flow(source, {}))
    validate_commands(compile_flow('- launchApp: {appId: example, stopApp: true, permissions: {notifications: allow}}\n'
                                   '- tapOn: {text: Transaction, retryTapIfNoChange: true, waitToSettleTimeoutMs: 500}', {}))


# spec: ANYM-953
def test_uiautomator_failure_is_timed_and_never_retried():
    from deedkit_android.uiautomator import UiRunner

    async def scenario():
        calls = []
        async def call(command, **options):
            calls.append(command)
            raise Refused('uiautomator_transport_error')
        runner = UiRunner(call, 700, 150)
        with pytest.raises(Refused) as failure:
            await runner.run(compile_flow('- tapOn: Buy\n- back', {}))
        assert calls == [{'tapOn': 'Buy'}]
        assert runner.completed == 0 and failure.value.command_path == [0]
        assert runner.steps[0]['error'] == 'uiautomator_transport_error'
    asyncio.run(scenario())


# spec: ANYM-953
def test_uiautomator_uses_single_attempt_rpc_for_mutations():
    from deedkit_android.ui_worker import Device

    device = object.__new__(Device)
    device._dev = SimpleNamespace()
    device._device_server_port = 9008
    device._debug = False
    import deedkit_android.ui_worker as worker
    calls = []
    def rpc(*args):
        calls.append(args)
        raise worker.HTTPError('synthetic dropped response')
    original = worker._jsonrpc_call
    worker._jsonrpc_call = rpc
    try:
        with pytest.raises(worker.HTTPError):
            device.jsonrpc_call('click', [10, 20])
        assert len(calls) == 1
    finally:
        worker._jsonrpc_call = original


# spec: ANYM-953
def test_ui_missing_wait_fails_and_condition_miss_skips_without_mutation():
    from deedkit_android.uiautomator import UiRunner

    async def scenario():
        seen = []
        async def call(command, **options):
            seen.append(command)
            return {'matched': False}
        runner = UiRunner(call, 700, 150)
        await runner.run(compile_flow('- runFlow: {when: {visible: Absent}, commands: [back]}', {}))
        assert len(seen) == 1 and runner.completed == 1
        with pytest.raises(Refused, match='element_not_found'):
            await runner.run(compile_flow('- extendedWaitUntil: {visible: Absent, timeout: 100}', {}))
        assert len(seen) == 2 and runner.steps[-1]['error'] == 'element_not_found'
    asyncio.run(scenario())


# spec: ANYM-953
def test_ui_transport_failure_receipt_preserves_snapshot_and_no_replay(tmp_path, monkeypatch):
    import uuid
    from deedkit_android.device import Devices
    from deedkit_android.store import Store

    async def scenario():
        store = Store(tmp_path / 'state')
        devices = Devices(SimpleNamespace(devices={'phone': 'SERIAL'}, maestro=None, adb='/synthetic',
                                           uiautomator2_jar='/synthetic', state_dir=str(tmp_path / 'state')), store)
        seen = []
        async def call(device, command, **options):
            seen.append(command)
            if len(seen) == 2:
                raise Refused('uiautomator_transport_error')
            return {'success': True}
        async def capture(*args):
            return {'image_file': store.save('owner', b'PNG')}
        async def hierarchy(*args):
            return {'elements': [{'text': 'Home'}]}
        monkeypatch.setattr(devices.ui, 'call', call)
        monkeypatch.setattr(devices, 'capture_image', capture)
        monkeypatch.setattr(devices, 'capture_hierarchy', hierarchy)
        session = store.reserve('owner', 'phone', 'acquire')['session_id']
        request = str(uuid.uuid4())
        flow = compile_flow('- back\n- tapOn: Buy\n- back', {})
        try:
            result = await devices.execute('owner', 'phone', session, request, None, 10, 10, flow)
            assert result['state'] == 'unknown' and result['automatic_retry'] is False
            assert result['engine'] == 'uiautomator2' and result['commands_executed'] == 1
            assert result['failed_command_path'] == [1] and len(result['steps']) == 2
            assert result['steps'][-1]['error'] == 'uiautomator_transport_error'
            assert 'image_file' in result['observation']
            assert await devices.execute('owner', 'phone', session, request, None, 10, 10, flow) == result
            assert len(seen) == 2
        finally:
            await devices.close()
            store.close()
    asyncio.run(scenario())


# spec: ANYM-953
def test_nested_unsupported_selectors_and_relative_points_are_rejected():
    from deedkit_android.uiautomator import validate_commands

    sources = [
        '- extendedWaitUntil: {visible: {below: {text: absent}}, timeout: 100}',
        '- scrollUntilVisible: {element: {image: local.png}}',
        '- tapOn: {text: Delete, point: "50%,50%"}',
        '- setText: {resourceId: field}',
    ]
    for source in sources:
        with pytest.raises(Refused):
            validate_commands(compile_flow(source, {}))


# spec: ANYM-953
def test_invalid_action_options_are_rejected_before_a_batch_can_tap():
    from deedkit_android.uiautomator import validate_commands

    for source in ('- tapOn: {text: Buy, waitToSettleTimeoutMs: later}',
                   '- launchApp: {appId: example.app, permissions: {all: allow}}',
                   '- scrollUntilVisible: {element: Results, direction: sideways}',
                   '- inputText: {text: 42}', '- extendedWaitUntil: false'):
        with pytest.raises(Refused):
            validate_commands(compile_flow('- tapOn: Earlier\n' + source, {}))


# spec: ANYM-953
def test_decimal_points_and_malformed_permission_values():
    from deedkit_android.ui_worker import point
    from deedkit_android.uiautomator import validate_commands

    device = SimpleNamespace(window_size=lambda: (1000, 2000))
    assert point(device, '12.5, 20.9') == (12, 21)
    assert point(device, '12.5%, 20.9%') == (125, 418)
    for source in ('- launchApp: {appId: example.app, permissions: {notifications: []}}',
                   '- launchApp: {appId: example.app, permissions: {42: allow}}'):
        with pytest.raises(Refused):
            validate_commands(compile_flow(source, {}))


# spec: ANYM-953
def test_unicode_input_uses_accessibility_without_copying_to_clipboard():
    from deedkit_android.ui_worker import execute

    values = []
    target = SimpleNamespace(get_text=lambda timeout: 'Поиск',
                             set_text=lambda value, timeout: values.append(value))
    class Device:
        def __call__(self, **selector):
            assert selector == {'focused': True}
            return target
        def jsonrpc_call(self, method, args):
            assert method == 'dumpWindowHierarchy'
            return '<hierarchy><node focused="true" hint="Поиск" /></hierarchy>'
    assert execute(Device(), {'inputText': 'Привет'}) == {'success': True}
    assert values == ['Привет']


# spec: ANYM-953
def test_failed_app_launch_stops_before_reporting_success():
    from deedkit_android.ui_worker import execute

    class Device:
        def __init__(self):
            self._dev = SimpleNamespace(shell2=self.shell)
        def shell(self, args, timeout):
            return SimpleNamespace(returncode=0, output='No activity found')
    with pytest.raises(Refused, match='app_launch_failed'):
        execute(Device(), {'launchApp': {'appId': 'missing.app'}})
