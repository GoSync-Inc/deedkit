"""Foreground verification does not dump an application's complete view tree."""

from types import SimpleNamespace

import pytest

from deedkit_android.config import Refused
from deedkit_android.ui_worker import Device


# spec: ANYM-953
def test_foreground_on_android_16_uses_the_current_window_only():
    calls = []

    def shell(args, timeout):
        calls.append((args, timeout))
        assert args == ['dumpsys', 'window']
        return SimpleNamespace(returncode=0, output=(
            'mCurrentFocus=Window{abc u0 example.shop/.MainActivity}\n'
            'mFocusedApp=ActivityRecord{def u0 example.other/.PreviousActivity t10}\n'))

    device = object.__new__(Device)
    device._dev = SimpleNamespace(shell2=shell)
    assert device.app_current() == {'package': 'example.shop', 'activity': '.MainActivity'}
    assert len(calls) == 1


# spec: ANYM-953
def test_foreground_without_one_current_window_fails_closed():
    device = object.__new__(Device)
    for output in ('mCurrentFocus=null', 'mFocusedApp=ActivityRecord{abc u0 example.shop/.Main t10}',
                   'mCurrentFocus=Window{a u0 example.one/.Main}\n'
                   'mCurrentFocus=Window{b u0 example.two/.Main}'):
        device._dev = SimpleNamespace(shell2=lambda args, timeout: SimpleNamespace(returncode=0, output=output))
        with pytest.raises(Refused, match='app_foreground_unavailable'):
            device.app_current()


class ColdStart:
    # A cold start shows Android 12+'s "Splash Screen <package>" window first, which has no
    # activity, so the foreground is briefly unreadable before the app's own window takes focus.
    def __init__(self, unreadable, package='example.shop'):
        self.unreadable, self.package, self.reads = unreadable, package, 0
        self._dev = SimpleNamespace(shell2=self.shell)
    def shell(self, args, timeout):
        if args[:2] == ['cmd', 'package']:
            return SimpleNamespace(returncode=0, output='priority=0\nexample.shop/.MainActivity\n')
        return SimpleNamespace(returncode=0, output='Status: ok\n')
    def app_current(self):
        self.reads += 1
        if self.reads <= self.unreadable:
            raise Refused('app_foreground_unavailable')
        return {'package': self.package, 'activity': '.MainActivity'}


# spec: ANYM-953
def test_cold_launch_waits_through_the_splash_window(monkeypatch):
    from deedkit_android import ui_worker

    monkeypatch.setattr(ui_worker.time, 'sleep', lambda seconds: None)
    device = ColdStart(unreadable=3)
    assert ui_worker.execute(device, {'launchApp': {'appId': 'example.shop', 'stopApp': True}}) == {'success': True}
    assert device.reads == 4


# spec: ANYM-953
def test_launch_whose_app_never_takes_the_foreground_fails_after_a_bounded_wait(monkeypatch):
    from deedkit_android import ui_worker

    clock = iter(range(0, 1000))
    monkeypatch.setattr(ui_worker.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(ui_worker.time, 'sleep', lambda seconds: None)
    for device in (ColdStart(unreadable=10 ** 6), ColdStart(unreadable=0, package='example.other')):
        with pytest.raises(Refused, match='app_launch_failed'):
            ui_worker.execute(device, {'launchApp': {'appId': 'example.shop'}})
        assert 1 < device.reads <= ui_worker.LAUNCH_FOREGROUND_S + 2


# spec: ANYM-954
def test_screen_wait_keeps_polling_while_the_foreground_is_unreadable(monkeypatch):
    from deedkit_android import map_runtime

    monkeypatch.setattr(map_runtime.time, 'sleep', lambda seconds: None)
    device = ColdStart(unreadable=2)
    app_map = {'screens': {'home': {'activity': r'\.MainActivity'}}}
    monkeypatch.setattr(map_runtime, 'snapshot', lambda device, app_map: None)
    monkeypatch.setattr(map_runtime, 'screen_state', lambda root, app_map, name, activity: {
        'matched': activity == '.MainActivity', 'missing': [] if activity else ['activity'], 'forbidden': []})
    assert map_runtime.wait_screen(device, app_map, 'home', 60_000)['polls'] == 3
