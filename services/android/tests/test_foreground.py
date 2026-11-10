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
