"""Private UIA2 child. JSON lines are local IPC, never caller-supplied Python."""

import atexit
import json
import logging
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid
from xml.etree import ElementTree

import adbutils
import uiautomator2 as u2
from uiautomator2.core import _jsonrpc_call
from uiautomator2.exceptions import HTTPError as HTTPError, UiObjectNotFoundError

from .config import Refused
from .uiautomator import SELECTORS, stop_script

# Accessibility classes of editable fields (EditText subclasses report EditText).
TEXT_FIELDS = ('EditText', 'AutoCompleteTextView')
# Bound for a launched app's window to take focus after `am start -W` returns.
LAUNCH_FOREGROUND_S = 10


class Device(u2.Device):
    """Own the patched server; a lost response never repeats a mutation."""

    def __init__(self, serial, jar, marker):
        self.jar, self.marker = jar, Path(marker)
        self.server = None
        self.token = 'deedkit-uia2-' + uuid.uuid4().hex
        super().__init__(adbutils.device(serial), port=19000 + int(uuid.uuid4().hex[:6], 16) % 40000)

    def start_uiautomator(self):
        target = '/data/local/tmp/' + self.token + '.jar'
        self._dev.sync.push(self.jar, target, check=True)
        self.server = subprocess.Popen(
            ['adb', '-s', self._dev.serial, 'shell',
             f'echo $$; export CLASSPATH={target}; exec app_process / --nice-name={self.token} '
             f'com.github.uiautomator2.Console -p {self._device_server_port}'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        pid = int(self.server.stdout.readline().strip())
        self.marker.write_text(json.dumps({'pid': pid, 'token': self.token}))
        self.marker.chmod(0o600)
        # Consume server diagnostics without logging screen/typed content.
        import threading
        threading.Thread(target=lambda: self.server.stdout.read(), daemon=True).start()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and self.server.poll() is None:
            if self._check_alive():
                self.jsonrpc.setConfigurator({'waitForIdleTimeout': 0, 'waitForSelectorTimeout': 0})
                return
            time.sleep(0.1)
        raise Refused('uiautomator_start_failed', 503)

    def stop_uiautomator(self, wait=True):
        # The parent verifies and stops the unique device PID as well, including
        # after a SIGKILL. Never use killall or adopt an already-running server.
        if self.server is not None:
            self.server.terminate()
            try:
                self.server.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait()
            self.server = None
        if self.marker.exists():
            state = json.loads(self.marker.read_text())
            result = self._dev.shell2(stop_script(int(state['pid']), self.token), timeout=6)
            if result.returncode:
                raise Refused('uiautomator_cleanup_required', 503)
            self.marker.unlink(missing_ok=True)
        self._dev.shell(['rm', '-f', '/data/local/tmp/' + self.token + '.jar'], timeout=2)

    def jsonrpc_call(self, method, params=None, timeout=10):
        return _jsonrpc_call(self._dev, self._device_server_port, method, params, timeout, False)

    def app_current(self):
        # Android 16 omits focus from `dumpsys window windows`. The upstream
        # fallback dumps every activity's view tree, adding seconds per launch.
        output = shell(self, ['dumpsys', 'window'])
        focused = set(re.findall(r'^\s*mCurrentFocus=Window\{[^}\n]*\s([^\s/]+)/([^}\s]+)\}',
                                 output, re.MULTILINE))
        if len(focused) != 1:
            raise Refused('app_foreground_unavailable')
        package, activity = focused.pop()
        return {'package': package, 'activity': activity}


def reaches_foreground(device, package):
    """A cold start shows Android 12+'s splash window (no activity) or no focus at first."""
    deadline = time.monotonic() + LAUNCH_FOREGROUND_S
    while True:
        try:
            if device.app_current().get('package') == package:
                return True
        except Refused as error:
            if error.code != 'app_foreground_unavailable':
                raise
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def selector(device, value):
    value = {'text': value} if isinstance(value, str) else value
    from .uiautomator import validate_selector
    validate_selector(value)
    mapping = {'text': 'textMatches', 'id': 'resourceIdMatches', 'textExact': 'text',
               'index': 'instance'}
    return device(**{mapping.get(k, k): v for k, v in value.items() if k in SELECTORS})


def wait_for(device, value, timeout, visible=True):
    value = {k: v for k, v in value.items() if k in SELECTORS} if isinstance(value, dict) else value
    target = selector(device, value)
    method = 'waitForExists' if visible else 'waitUntilGone'
    matched = device.jsonrpc_call(method, (target.selector, timeout), timeout=timeout / 1000 + 2)
    return bool(matched), target


def point(device, value):
    parts = value.split(',') if isinstance(value, str) else value
    if not isinstance(parts, (list, tuple)) or len(parts) != 2:
        raise Refused('invalid_point', 422)
    width, height = device.window_size()
    return tuple(round(float(str(v).strip().rstrip('%')) * size / 100) if str(v).strip().endswith('%')
                 else round(float(v)) for v, size in zip(parts, (width, height)))


def shell(device, args):
    result = device._dev.shell2(args, timeout=15)
    if result.returncode:
        raise Refused('uiautomator_shell_failed')
    return result.output


def element_region(device, value, timeout):
    matched, target = wait_for(device, value, timeout)
    if not matched:
        raise Refused('element_not_found')
    bounds = target.info['bounds']
    region = {'x': bounds['left'], 'y': bounds['top'], 'w': bounds['right'] - bounds['left'], 'h': bounds['bottom'] - bounds['top']}
    if region['w'] < 1 or region['h'] < 1:
        raise Refused('invalid_element_bounds')
    return region


def execute(device, command, element_timeout_ms=5000, optional_timeout_ms=1000, ocr=None):
    from .app_maps import MAP_ACTIONS
    from .map_runtime import execute_map
    if isinstance(command, dict) and next(iter(command)) in MAP_ACTIONS:
        name, options = next(iter(command.items()))
        return execute_map(device, name, options, execute)
    name, value = next(iter(command.items())) if isinstance(command, dict) else (command, None)
    options = value if isinstance(value, dict) else {}
    timeout = options.get('timeout', optional_timeout_ms if options.get('optional') else element_timeout_ms)
    if name == 'readScreenText':
        # Read-only: one screenshot, recognized by the host's configured engine.
        from .screen_text import read_screen_text
        return read_screen_text(device, options, ocr, lambda value: element_region(device, value, element_timeout_ms))
    lookup = {'tapOn', 'doubleTapOn', 'longPressOn', 'assertVisible', 'assertNotVisible', 'copyTextFrom', 'exists', 'setText'}
    if name == 'extendedWaitUntil':
        visible = 'visible' in value
        matched, _ = wait_for(device, value['visible' if visible else 'notVisible'], timeout, visible)
        return {'matched': matched}
    if name in lookup:
        target = None
        if 'point' not in options:
            matched, target = wait_for(device, value, timeout, name != 'assertNotVisible')
            if name == 'exists':
                return {'exists': matched}
            if not matched:
                if options.get('optional'):
                    return {'skipped': True, 'reason': 'element_not_found'}
                raise Refused('element_not_found')
        if name in {'assertVisible', 'assertNotVisible'}:
            return {'matched': True}
        if name == 'copyTextFrom':
            return {'text': target.get_text(timeout=0)}
        if name == 'setText':
            target.set_text(value['value'], timeout=0)
        else:
            x, y = point(device, options['point']) if 'point' in options else target.center()
            if name == 'longPressOn' or options.get('longPress'):
                device.long_click(x, y)
            elif name == 'doubleTapOn':
                device.double_click(x, y)
            else:
                device.click(x, y)
            time.sleep(options.get('waitToSettleTimeoutMs', 0) / 1000)
    elif name in {'inputText', 'eraseText'}:
        # Direct accessibility setText handles Unicode without installing an IME
        # or falling back to a second send after an ambiguous transport failure.
        # A keyboard key can report focus too, and the uiautomator2 server clicks its
        # setText target first: only a focused text field is a target, else nothing acts.
        root = ElementTree.fromstring(device.jsonrpc_call('dumpWindowHierarchy', (True, 50)))
        focused = next((node for node in root.iter('node') if node.get('focused') == 'true'
                        and (node.get('class') or '').endswith(TEXT_FIELDS)), None)
        if focused is None:
            raise Refused('focused_field_not_found')
        selector = {'focused': True, 'className': focused.get('class')}
        if focused.get('package'):
            selector['packageName'] = focused.get('package')
        target = device(**selector)
        text = target.get_text(timeout=0) or ''
        if focused.get('hint') == text:
            text = ''
        if name == 'inputText':
            text += value['text'] if isinstance(value, dict) else str(value)
        else:
            count = value.get('charactersToErase', len(text)) if isinstance(value, dict) else value
            text = text[:-int(count)] if int(count) else text
        target.set_text(text, timeout=0)
    elif name == 'pressKey':
        key = options.get('key', value)
        aliases = {'Enter': 'enter', 'Back': 'back', 'Home': 'home', 'Backspace': 'del', 'Delete': 'del', 'Tab': 'tab'}
        device.press(key if type(key) is int else aliases.get(key, key.lower()))
    elif name == 'back':
        device.press('back')
    elif name == 'hideKeyboard':
        # BACK only when a keyboard is actually shown.
        if re.search(r'mInputShown=true|isInputViewShown=true', shell(device, ['dumpsys', 'input_method'])):
            device.press('back')
    elif name == 'launchApp':
        package = value['appId']
        component = shell(device, ['cmd', 'package', 'resolve-activity', '--brief', '-a',
                                   'android.intent.action.MAIN', '-c', 'android.intent.category.LAUNCHER', package]).strip().splitlines()
        if not component or '/' not in component[-1]:
            raise Refused('app_launch_failed')
        if value.get('clearState'):
            if shell(device, ['pm', 'clear', package]).strip() != 'Success':
                raise Refused('app_clear_failed')
        if value.get('stopApp'):
            shell(device, ['am', 'force-stop', package])
        for permission, grant in value.get('permissions', {}).items():
            if permission != 'all' and grant != 'unset':
                permission = {'notifications': 'android.permission.POST_NOTIFICATIONS'}.get(permission, permission)
                shell(device, ['pm', 'grant' if grant == 'allow' else 'revoke', package, permission])
        output = shell(device, ['am', 'start', '-W', '-n', component[-1]])
        if re.search(r'(^|\n)(Error|Exception)', output) or not reaches_foreground(device, package):
            raise Refused('app_launch_failed')
    elif name in {'stopApp', 'killApp', 'clearState'}:
        package = options.get('appId', value)
        output = shell(device, ['pm', 'clear', package] if name == 'clearState' else ['am', 'force-stop', package])
        if name == 'clearState' and output.strip() != 'Success':
            raise Refused('app_clear_failed')
    elif name == 'openLink':
        shell(device, ['am', 'start', '-W', '-a', 'android.intent.action.VIEW', '-d', options.get('link', value)])
    elif name in {'swipe', 'scroll'}:
        if 'start' in options and 'end' in options:
            device.swipe(*point(device, options['start']), *point(device, options['end']),
                         duration=options.get('duration', 400) / 1000)
        else:
            device.swipe_ext(str(options.get('direction', 'UP')).lower(), scale=0.6,
                             duration=options.get('duration', 400) / 1000)
    elif name == 'scrollUntilVisible':
        deadline = time.monotonic() + options.get('timeout', 10000) / 1000
        while True:
            matched, _ = wait_for(device, value['element'], 0)
            if matched:
                break
            if time.monotonic() >= deadline:
                raise Refused('element_not_found')
            device.swipe_ext({'down': 'up', 'up': 'down', 'left': 'right', 'right': 'left'}[
                str(options.get('direction', 'DOWN')).lower()], scale=0.6, duration=0.3)
    elif name == 'sleep':
        time.sleep(value / 1000)
        return {'success': True, 'slept_ms': value}
    elif name == 'waitForAnimationToEnd':
        # Explicit idle wait, independent of the zero-wait default configurator.
        device.jsonrpc_call('waitForIdle', (options.get('timeout', 1000),))
    elif name == 'setOrientation':
        device.set_orientation({'portrait': 'n', 'landscape': 'l'}.get(
            str(options.get('orientation', value)).lower(), str(options.get('orientation', value)).lower()))
    else:
        raise Refused('unsupported_uiautomator_command', 422)
    return {'success': True}


def main():
    logging.disable(logging.CRITICAL)
    device = None
    # SIGTERM must unwind a blocked RPC so the parent can fence its device PID.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        for line in sys.stdin:
            try:
                if device is None:
                    device = Device(*sys.argv[1:])
                args = json.loads(line)
                if args['command'] == '__hierarchy__':
                    result = {'xml': device.jsonrpc_call('dumpWindowHierarchy', (True, 50))}
                else:
                    result = execute(device, **args)
            except Refused as error:
                result = {'error': error.code, **({'details': error.details} if hasattr(error, 'details') else {}),
                          **({'hint': error.hint} if getattr(error, 'hint', None) else {})}
            except UiObjectNotFoundError:
                result = {'error': 'element_not_found'}
            except Exception:
                result = {'error': 'uiautomator_transport_error'}
            print(json.dumps(result, ensure_ascii=True), flush=True)
    finally:
        if device is not None:
            atexit.unregister(device.stop_uiautomator)
            device.stop_uiautomator()


if __name__ == '__main__':
    main()
