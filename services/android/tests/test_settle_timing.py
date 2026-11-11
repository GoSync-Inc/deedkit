"""Screen stability waits, pauses and deep-link phases with a synthetic clock and hierarchy."""

from types import SimpleNamespace

import pytest

from deedkit_android.app_maps import validate_action, validate_map
from deedkit_android.config import Refused
from deedkit_android.flows import compile_flow
from deedkit_android.uiautomator import UI_FIELDS, validate_commands

MAP = '''package: example.shop
selectors:
  orders: [{textExact: Orders}]
screens:
  orders:
    require: [orders]
deep_links:
  orders: {url: 'example://orders', screen: orders}
'''


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Device:
    """Each dump advances the clock 50 ms; the screen changes `changes` times, then holds."""

    def __init__(self, clock, changes):
        self.clock, self.changes, self.dumps, self.opened = clock, changes, 0, []

    def jsonrpc_call(self, method, args, timeout=10):
        self.clock.now += 0.05
        self.dumps += 1
        rows = min(self.dumps, self.changes + 1)
        return ('<hierarchy><node text="Orders" bounds="[0,0][10,10]" />'
                + ''.join(f'<node text="row {i}" bounds="[0,{i}][10,{i + 1}]" />' for i in range(rows)) + '</hierarchy>')


@pytest.fixture
def clock(monkeypatch):
    import deedkit_android.map_runtime as runtime
    fake = Clock()
    monkeypatch.setattr(runtime, 'time', SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep))
    return fake


# spec: ANYM-956
def test_wait_settled_returns_after_a_stable_period(clock):
    from deedkit_android.map_runtime import execute_map

    device = Device(clock, changes=3)
    result = execute_map(device, 'waitSettled', {'app_map': validate_map(MAP), 'stable_ms': 300}, None)
    assert result['settled'] is True and result['polls'] >= 6
    assert 300 <= result['elapsed_ms'] < 1000


# spec: ANYM-956
def test_wait_settled_fails_definitely_when_the_screen_keeps_changing(clock):
    from deedkit_android.map_runtime import execute_map
    from deedkit_android.uiautomator import DEFINITE

    device = Device(clock, changes=1000)
    with pytest.raises(Refused) as failure:
        execute_map(device, 'waitSettled', {'app_map': validate_map(MAP), 'stable_ms': 300, 'timeout': 1000}, None)
    assert failure.value.code == 'screen_not_settled' and failure.value.details['polls'] > 1
    assert 'screen_not_settled' in DEFINITE


# spec: ANYM-956
def test_open_deep_link_reports_phases_and_optional_settle(clock):
    from deedkit_android.map_runtime import execute_map

    app_map = validate_map(MAP)
    device = Device(clock, changes=0)

    def native(device, command):
        device.opened.append(command)
    result = execute_map(device, 'openDeepLink', {'app_map': app_map, 'name': 'orders'}, native)
    assert device.opened == [{'openLink': 'example://orders'}]
    assert set(result['phases']) == {'open_ms', 'screen_ms'} and result['polls'] == 1
    assert result['matched'] is True and device.dumps == 1

    device = Device(clock, changes=2)
    result = execute_map(device, 'openDeepLink', {'app_map': app_map, 'name': 'orders', 'settle_ms': 200}, native)
    assert set(result['phases']) == {'open_ms', 'screen_ms', 'settle_ms'}
    assert result['phases']['settle_ms'] >= 200 and device.dumps > 3

    device = Device(clock, changes=2)
    result = execute_map(device, 'openApp', {'app_map': app_map, 'settle_ms': 200}, native)
    assert result['settled'] is True and device.opened[-1] == {'launchApp': {'appId': 'example.shop', 'stopApp': False}}


# spec: ANYM-956
def test_settle_and_sleep_options_are_validated_before_admission():
    app_map = validate_map(MAP)
    for name, options in (('waitSettled', {'stable_ms': 50}), ('waitSettled', {'stable_ms': 5001}),
                          ('openDeepLink', {'name': 'orders', 'settle_ms': 5001}), ('openApp', {'settle_ms': -1}),
                          ('waitSettled', {'polls': 3})):
        with pytest.raises(Refused):
            validate_action(name, dict(options), app_map)
    validate_action('waitSettled', {'stable_ms': '400', 'timeout': 2000}, app_map)
    for source in ('- sleep: 10001', '- sleep: -1', '- sleep: {ms: 10}', '- sleep: soon'):
        with pytest.raises(Refused):
            validate_commands(compile_flow(source, {}, fields=UI_FIELDS))
    validate_commands(compile_flow('- sleep: 250\n- sleep: 0', {}, fields=UI_FIELDS))


# spec: ANYM-956
def test_sleep_pauses_inside_the_batch(monkeypatch):
    import deedkit_android.ui_worker as worker

    slept = []
    monkeypatch.setattr(worker.time, 'sleep', slept.append)
    assert worker.execute(object(), {'sleep': 250}) == {'success': True, 'slept_ms': 250}
    assert slept == [0.25]
