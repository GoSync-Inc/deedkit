"""Live-agent feedback, reproduced without a phone or a provider session."""

import asyncio
from types import SimpleNamespace
import uuid

import pytest

from deedkit_android.config import Refused
from deedkit_android.flows import compile_flow
from deedkit_android.store import Store


# spec: ANYM-951
def test_required_and_optional_lookups_use_native_bounded_probes():
    # The Maestro engine was removed (owner 11-11); uiautomator2 bounds the same lookups natively.
    from deedkit_android.ui_worker import execute

    device = LookupDevice()
    assert execute(device, {'tapOn': {'text': 'missing', 'optional': True}}, 5000, 700) == {
        'skipped': True, 'reason': 'element_not_found'}
    assert device.waits == [('waitForExists', 700)] and device.clicks == []  # Never tapped.
    with pytest.raises(Refused, match='element_not_found'):
        execute(device, {'tapOn': {'text': 'missing', 'timeout': 2200}}, 5000, 700)
    assert device.waits[-1] == ('waitForExists', 2200) and device.clicks == []


class LookupDevice:
    """A synthetic uiautomator2 device whose selector waits never match."""

    def __init__(self):
        self.waits, self.clicks = [], []

    def __call__(self, **selector):
        return SimpleNamespace(selector=selector)

    def jsonrpc_call(self, method, params, timeout=10):
        self.waits.append((method, params[1]))
        return False

    def click(self, x, y):
        self.clicks.append((x, y))


def ui_devices(tmp_path, store):
    from deedkit_android.device import Devices
    return Devices(SimpleNamespace(devices={'phone': 'SERIAL'}, adb='/synthetic', uiautomator2_jar='/synthetic',
                                   state_dir=str(tmp_path / 'state')), store)


# spec: ANYM-952
def test_condition_timeout_nested_steps_and_driver_failure_are_not_silently_skipped():
    from deedkit_android.uiautomator import UiRunner

    async def scenario():
        calls = []
        async def call(command, **options):
            calls.append(command)
            raise Refused('uiautomator_transport_error')
        runner = UiRunner(call, 5000, 700)
        flow = compile_flow('- runFlow: {when: {visible: Popup, timeout: 250}, commands: [back]}', {})
        with pytest.raises(Refused, match='uiautomator_transport_error'):
            await runner.run(flow)
        assert calls == [{'extendedWaitUntil': {'visible': 'Popup', 'timeout': 250}}]
    asyncio.run(scenario())


# spec: ANYM-951
def test_failed_flow_persists_one_snapshot_and_replay_does_not_capture_again(tmp_path, monkeypatch):

    async def scenario():
        store = Store(tmp_path / 'state')
        devices = ui_devices(tmp_path, store)
        captures = []
        async def call(*args, **options):
            raise Refused('element_not_found')
        async def image(owner, device, mode, crop):
            captures.append(mode)
            return {'image_file': {**store.save(owner, b'fake PNG'), 'mime_type': 'image/png'}}
        async def hierarchy(device):
            return {'elements': [{'text': 'Home'}], 'hierarchy_source': 'uiautomator2'}
        monkeypatch.setattr(devices.ui, 'call', call)
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

    async def scenario():
        store = Store(tmp_path / 'state')
        devices = ui_devices(tmp_path, store)
        async def call(*args, **options):
            raise Refused('element_not_found')
        async def unavailable(*args):
            raise Refused('file_quota_exceeded')
        monkeypatch.setattr(devices.ui, 'call', call)
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
def test_completed_lookup_failure_keeps_maestro_transport_warm(tmp_path):
    # The Maestro transport is gone; the uiautomator2 worker stays warm after a definite miss.
    from deedkit_android.uiautomator import Uiautomator

    ui = Uiautomator(SimpleNamespace(devices={'phone': 'SERIAL'}, uiautomator2_jar='/synthetic',
                                     adb='/synthetic/adb', state_dir=str(tmp_path)), 0)

    class Process:
        returncode = None
        stdin = SimpleNamespace(write=lambda data: None, drain=lambda: asyncio.sleep(0))
        stdout = SimpleNamespace(readline=lambda: asyncio.sleep(0, b'{"error": "element_not_found"}\n'))
    process = ui.processes['phone'] = Process()

    async def scenario():
        for _ in range(2):
            with pytest.raises(Refused, match='element_not_found'):
                await ui.call('phone', {'tapOn': 'missing'})
    asyncio.run(scenario())
    assert ui.processes['phone'] is process


# spec: ANYM-952
def test_nested_repeats_conditions_and_explicit_retries_keep_command_paths():
    from deedkit_android.uiautomator import UiRunner

    async def scenario():
        seen = []
        async def call(command, **options):
            seen.append(command)
            if isinstance(command, dict) and 'extendedWaitUntil' in command:
                return {'matched': False}
            if isinstance(command, dict) and 'tapOn' in command:
                raise Refused('element_not_found')
            return {'success': True}
        runner = UiRunner(call, 5000, 1000)
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

    async def scenario():
        store = Store(tmp_path / 'state')
        devices = ui_devices(tmp_path, store)
        calls = []
        async def call(*args, **options):
            calls.append(True)
            await asyncio.sleep(30)
        async def image(*args):
            return {'screenshot_error': 'unavailable'}
        async def hierarchy(*args):
            return {'elements': []}
        monkeypatch.setattr(devices.ui, 'call', call)
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
