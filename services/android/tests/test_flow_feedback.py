"""Live-agent feedback, reproduced without a phone or a provider session."""

import asyncio
from types import SimpleNamespace
import uuid

import pytest
import yaml

from deedkit_android.config import Refused
from deedkit_android.flows import compile_flow
from deedkit_android.store import Store


# spec: ANYM-951
def test_required_and_optional_lookups_use_native_bounded_probes():
    from deedkit_android.flow_runner import FlowRunner

    async def scenario():
        calls = []
        async def call(name, args, timeout):
            commands = list(yaml.safe_load_all(args['yaml']))[1]
            calls.extend(commands)
            if 'extendedWaitUntil' in commands[0]:
                raise RefusedWithDiagnostic('Failed to run flow: Assertion is false: missing is visible')
            return {'success': True}
        runner = FlowRunner(call, 'serial', 5000, 700)
        await runner.run(compile_flow('- tapOn: {text: missing, optional: true}\n- back', {}))
        assert calls[0] == {'extendedWaitUntil': {'visible': {'text': 'missing'}, 'timeout': 700}}
        assert calls[1:] == ['back']  # Missing optional target is never tapped.
        with pytest.raises(Refused, match='element_not_found') as failure:
            await runner.run(compile_flow('- tapOn: {text: missing, timeout: 2200}', {}))
        assert calls[-1]['extendedWaitUntil']['timeout'] == 2200
        assert failure.value.command_path == [0]
    asyncio.run(scenario())


class RefusedWithDiagnostic(Refused):
    def __init__(self, diagnostic):
        super().__init__('maestro_action_failed')
        self.diagnostic = diagnostic


# spec: ANYM-952
def test_condition_timeout_nested_steps_and_driver_failure_are_not_silently_skipped():
    from deedkit_android.flow_runner import FlowRunner

    async def scenario():
        calls = []
        async def call(name, args, timeout):
            command = list(yaml.safe_load_all(args['yaml']))[1][0]
            calls.append(command)
            if 'extendedWaitUntil' in command:
                raise RefusedWithDiagnostic('Failed to run flow: device transport disconnected')
            return {'success': True}
        runner = FlowRunner(call, 'serial', 5000, 700)
        flow = compile_flow('- runFlow: {when: {visible: Popup, timeout: 250}, commands: [back]}', {})
        with pytest.raises(Refused, match='maestro_action_failed'):
            await runner.run(flow)
        assert calls == [{'extendedWaitUntil': {'visible': 'Popup', 'timeout': 250}}]
    asyncio.run(scenario())


# spec: ANYM-951
def test_failed_flow_persists_one_snapshot_and_replay_does_not_capture_again(tmp_path, monkeypatch):
    from deedkit_android.device import Devices

    async def scenario():
        store = Store(tmp_path / 'state')
        devices = Devices(SimpleNamespace(devices={'phone': 'SERIAL'}, maestro='/synthetic', adb='/synthetic'), store)
        captures = []
        async def call(*args):
            raise RefusedWithDiagnostic('Failed to run flow: Assertion is false: missing is visible')
        async def image(owner, device, mode, crop):
            captures.append(mode)
            return {'image_file': {**store.save(owner, b'fake PNG'), 'mime_type': 'image/png'}}
        async def hierarchy(device):
            return {'elements': [{'text': 'Home'}], 'hierarchy_source': 'maestro'}
        monkeypatch.setattr(devices.maestro, 'call', call)
        monkeypatch.setattr(devices, 'capture_image', image)
        monkeypatch.setattr(devices, 'capture_hierarchy', hierarchy)
        token = store.reserve('owner', 'phone', 'acquire')['session_id']
        request = str(uuid.uuid4())
        flow = compile_flow('- tapOn: missing', {})
        try:
            receipt = await devices.execute('owner', 'phone', token, request, None, 5, 5, flow)
            assert receipt['state'] == 'failed'
            assert receipt['error'] == 'element_not_found'
            assert receipt['failed_command_index'] == 0
            assert receipt['failed_command'] == {'tapOn': 'missing'}
            assert receipt['automatic_retry'] is False
            snapshot = receipt['observation']
            assert snapshot['elements'] == [{'text': 'Home'}]
            assert 'image' not in snapshot and captures == ['file']
            file_id = snapshot['image_file']['file_id']
            assert store.file('owner', file_id).read_bytes() == b'fake PNG'
            with pytest.raises(Refused):
                store.file('other', file_id)
            assert await devices.execute('owner', 'phone', token, request, None, 5, 5, flow) == receipt
            assert await devices.receipt('owner', request, 0) == receipt
            assert captures == ['file']
            assert not devices.locks['phone'].locked()
        finally:
            await devices.close()
            store.close()
    asyncio.run(scenario())


# spec: ANYM-951
def test_capture_failure_keeps_original_error_and_releases_device(tmp_path, monkeypatch):
    from deedkit_android.device import Devices

    async def scenario():
        store = Store(tmp_path / 'state')
        devices = Devices(SimpleNamespace(devices={'phone': 'SERIAL'}, maestro='/synthetic', adb='/synthetic'), store)
        async def call(*args):
            raise RefusedWithDiagnostic('Failed to run flow: Assertion is false: absent is visible')
        async def unavailable(*args):
            raise Refused('file_quota_exceeded')
        monkeypatch.setattr(devices.maestro, 'call', call)
        monkeypatch.setattr(devices, 'capture_image', unavailable)
        monkeypatch.setattr(devices, 'capture_hierarchy', unavailable)
        token = store.reserve('owner', 'phone', 'acquire')['session_id']
        try:
            receipt = await devices.execute('owner', 'phone', token, str(uuid.uuid4()), None, 5, 5,
                                            compile_flow('- tapOn: absent', {}))
            assert receipt['state'] == 'failed' and receipt['error'] == 'element_not_found'
            assert receipt['observation']['screenshot_error'] == 'unavailable'
            assert not devices.locks['phone'].locked()
        finally:
            await devices.close()
            store.close()
    asyncio.run(scenario())


# spec: ANYM-952
def test_native_yaml_and_checkpoint_errors_explain_the_accepted_dialect():
    for source, fragment in [('appId: example\n---\n- back', 'command list'),
                             ('- checkpoint: {label: verify}', '- checkpoint: verify')]:
        with pytest.raises(Refused) as failure:
            compile_flow(source, {})
        assert fragment in failure.value.hint


# spec: ANYM-951
def test_session_legacy_lease_alias_is_normalized_without_ambiguous_precedence():
    from deedkit_android.app import Session
    from pydantic import ValidationError
    value = {'device_id': 'phone', 'action': 'renew', 'session_id': 'session', 'lease_s': 30}
    assert Session.model_validate(value).model_dump()['seconds'] == 30
    with pytest.raises(ValidationError):
        Session.model_validate({**value, 'seconds': 60})


# spec: ANYM-951
def test_completed_lookup_failure_keeps_maestro_transport_warm(monkeypatch):
    from deedkit_android.maestro import Maestro

    async def scenario():
        client = Maestro(SimpleNamespace())
        closed = []
        async def start():
            pass
        async def exchange(*args):
            return {'isError': True, 'content': [{'type': 'text', 'text': 'Failed to run flow: Assertion is false: missing'}]}
        async def close():
            closed.append(True)
        monkeypatch.setattr(client, 'start', start)
        monkeypatch.setattr(client, 'exchange', exchange)
        monkeypatch.setattr(client, 'close', close)
        for _ in range(2):
            with pytest.raises(Refused):
                await client.call('run', {}, 1)
        assert closed == []
    asyncio.run(scenario())


# spec: ANYM-952
def test_nested_repeats_conditions_and_explicit_retries_keep_command_paths():
    from deedkit_android.flow_runner import FlowRunner

    async def scenario():
        seen = []
        async def call(name, args, timeout):
            command = list(yaml.safe_load_all(args['yaml']))[1][0]
            seen.append(command)
            if 'extendedWaitUntil' in command:
                raise RefusedWithDiagnostic('Failed to run flow: Assertion is false: absent')
            return {'success': True}
        runner = FlowRunner(call, 'serial', 5000, 1000)
        await runner.run(compile_flow('- repeat: {times: 2, commands: [back]}\n'
            '- runFlow: {when: {visible: absent, timeout: 100}, commands: [hideKeyboard]}', {}))
        assert seen[:2] == ['back', 'back'] and len(seen) == 3
        with pytest.raises(Refused) as failed:
            await runner.run(compile_flow('- retry: {maxRetries: 1, commands: [{tapOn: absent}]}', {}))
        assert failed.value.command_path == [0, 0]
        assert len(seen) == 5
    asyncio.run(scenario())


# spec: ANYM-951
def test_total_timeout_retains_uncertainty_and_custom_wait_changes_conflict(tmp_path, monkeypatch):
    from deedkit_android.device import Devices

    async def scenario():
        store = Store(tmp_path / 'state')
        devices = Devices(SimpleNamespace(devices={'phone': 'SERIAL'}, maestro='/synthetic', adb='/synthetic'), store)
        calls = []
        async def call(*args):
            calls.append(True)
            await asyncio.sleep(30)
        async def image(*args):
            return {'screenshot_error': 'unavailable'}
        async def hierarchy(*args):
            return {'elements': []}
        monkeypatch.setattr(devices.maestro, 'call', call)
        monkeypatch.setattr(devices, 'capture_image', image)
        monkeypatch.setattr(devices, 'capture_hierarchy', hierarchy)
        token = store.reserve('owner', 'phone', 'acquire')['session_id']
        request = str(uuid.uuid4())
        flow = compile_flow('- back', {})
        try:
            receipt = await devices.execute('owner', 'phone', token, request, None, .01, 1, flow,
                                             element_timeout_ms=2300)
            assert receipt['state'] == 'unknown' and receipt['error'] == 'command_timeout'
            assert receipt['failed_command_index'] == 0 and receipt['automatic_retry'] is False
            with pytest.raises(Refused, match='request_id_conflict'):
                await devices.execute('owner', 'phone', token, request, None, .01, 1, flow,
                                      element_timeout_ms=2400)
            assert len(calls) == 1 and not devices.locks['phone'].locked()
        finally:
            await devices.close()
            store.close()
    asyncio.run(scenario())
