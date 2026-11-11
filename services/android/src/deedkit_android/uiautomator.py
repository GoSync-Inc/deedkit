"""Persistent, single-attempt UI worker behind the existing device lease."""

import asyncio
import json
import os
import re
from pathlib import Path
import sys
import time

from .config import Refused
from .flow_runner import FlowRunner

# Reject unsupported semantics before a batch performs its first action.
SUPPORTED = {'tapOn', 'doubleTapOn', 'longPressOn', 'assertVisible', 'assertNotVisible',
             'inputText', 'setText', 'eraseText', 'pressKey', 'launchApp', 'stopApp', 'killApp',
             'clearState', 'openLink', 'swipe', 'scroll', 'scrollUntilVisible',
             'extendedWaitUntil', 'waitForAnimationToEnd', 'back', 'hideKeyboard',
             'copyTextFrom', 'exists', 'setOrientation', 'runFlow', 'repeat', 'retry', 'checkpoint',
             'readScreenText', 'sleep'}
SELECTORS = {'text', 'id', 'textExact', 'resourceId', 'description', 'className', 'index',
             'enabled', 'selected', 'checked', 'focused', 'clickable', 'scrollable'}
OPTIONS = SELECTORS | {'point', 'optional', 'timeout', 'label', 'retryTapIfNoChange',
                       'waitToSettleTimeoutMs', 'longPress'}
UI_FIELDS = {
    **{name: OPTIONS for name in ('tapOn', 'doubleTapOn', 'longPressOn', 'assertVisible',
                                 'assertNotVisible', 'copyTextFrom', 'exists')},
    'setText': OPTIONS | {'value'}, 'inputText': {'text', 'label'},
    'eraseText': {'charactersToErase', 'label'}, 'pressKey': {'key', 'label'},
    'launchApp': {'appId', 'stopApp', 'clearState', 'clearKeychain', 'permissions', 'label'},
    'stopApp': {'appId', 'label'}, 'killApp': {'appId', 'label'}, 'clearState': {'appId', 'label'},
    'openLink': {'link', 'label'}, 'swipe': {'start', 'end', 'direction', 'duration', 'label'},
    'scrollUntilVisible': {'element', 'direction', 'timeout', 'label'},
    'extendedWaitUntil': {'visible', 'notVisible', 'timeout', 'label'},
    'waitForAnimationToEnd': {'timeout', 'label'},
    'setOrientation': {'orientation', 'label'},
    'runFlow': {'commands', 'when', 'label', 'name', 'version'}, 'repeat': {'commands', 'times', 'while', 'label'},
    'retry': {'commands', 'maxRetries', 'label'}, 'checkpoint': set(),
    'readScreenText': {'region', 'within', 'min_confidence', 'limit', 'key', 'structure', 'label'},
    'sleep': set(),
}
UNSUPPORTED_HINT = 'Use android_execute for this operation. The reference lists supported UI commands.'
# Definite read/lookup failures: nothing acted, so the persistent worker stays.
DEFINITE = {'element_not_found', 'screen_not_found', 'ambiguous_selector', 'invalid_element_bounds',
            'ocr_unavailable', 'ocr_failed', 'screenshot_unavailable', 'region_outside_screen',
            'screen_not_settled', 'focused_field_not_found'}



def validate_selector(value):
    if isinstance(value, str):
        return
    if not isinstance(value, dict) or not value or set(value) - SELECTORS:
        raise Refused('unsupported_uiautomator_selector', 422)
    for key, item in value.items():
        expected = int if key == 'index' else bool if key in {
            'enabled', 'selected', 'checked', 'focused', 'clickable', 'scrollable'} else str
        if type(item) is not expected or (key == 'index' and item < 0):
            raise Refused('invalid_uiautomator_selector', 422)


def validate_screen_text(value):
    options = {} if value is None else value
    if not isinstance(options, dict) or ('region' in options and 'within' in options):
        raise Refused('invalid_screen_text_options', 422)
    if 'region' in options:
        region = options['region']
        if not isinstance(region, dict) or set(region) != {'x', 'y', 'w', 'h'} or any(
                type(region[key]) is not int or region[key] < (1 if key in 'wh' else 0) or region[key] > 16384 for key in region):
            raise Refused('invalid_screen_text_region', 422)
    if 'within' in options:
        validate_selector(options['within'])
    if 'structure' in options and type(options['structure']) is not bool:
        raise Refused('invalid_screen_text_options', 422)
    for key, low, high in (('min_confidence', 0, 100), ('limit', 1, 300)):
        if key in options and (type(options[key]) is not int or not low <= options[key] <= high):
            raise Refused('invalid_screen_text_options', 422)
    if 'key' in options and (not isinstance(options['key'], str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', options['key'])):
        raise Refused('invalid_screen_text_options', 422)


def validate_commands(commands):
    for command in commands:
        name, value = next(iter(command.items())) if isinstance(command, dict) else (command, None)
        if name not in SUPPORTED:
            error = Refused('unsupported_uiautomator_command', 422)
            error.hint = UNSUPPORTED_HINT
            raise error
        if isinstance(value, dict) and set(value) - UI_FIELDS.get(name, set()):
            raise Refused('unsupported_uiautomator_option', 422)
        options = value if isinstance(value, dict) else {}
        for key in ('optional', 'retryTapIfNoChange', 'longPress', 'stopApp', 'clearState', 'clearKeychain'):
            if key in options and type(options[key]) is not bool:
                raise Refused('invalid_uiautomator_option', 422)
        for key in ('timeout', 'waitToSettleTimeoutMs', 'duration'):
            if key in options and (type(options[key]) is not int or not 0 <= options[key] <= 60000):
                raise Refused('invalid_uiautomator_timeout', 422)
        if name in {'extendedWaitUntil', 'scrollUntilVisible'} and not isinstance(value, dict):
            raise Refused('invalid_wait_condition', 422)
        if name == 'scrollUntilVisible' and 'element' not in options:
            raise Refused('invalid_wait_condition', 422)
        if name in {'swipe', 'scrollUntilVisible'}:
            if str(options.get('direction', 'up')).lower() not in {'up', 'down', 'left', 'right'}:
                raise Refused('invalid_swipe_direction', 422)
        if name == 'swipe' and ('start' in options) != ('end' in options):
            raise Refused('invalid_swipe_points', 422)
        if name == 'inputText' and not isinstance(options.get('text') if isinstance(value, dict) else value, str):
            raise Refused('invalid_input_text', 422)
        if name == 'eraseText':
            count = options.get('charactersToErase', 100) if isinstance(value, dict) else value
            if type(count) is not int or not 0 <= count <= 100000:
                raise Refused('invalid_erase_count', 422)
        if name == 'pressKey' and type(options.get('key', value)) not in (str, int):
            raise Refused('invalid_key', 422)
        if name == 'openLink' and not isinstance(options.get('link', value), str):
            raise Refused('invalid_link', 422)
        if name == 'setOrientation' and str(options.get('orientation', value)).lower() not in {
                'portrait', 'landscape', 'natural', 'left', 'right', 'upsidedown', 'n', 'l', 'r', 'u'}:
            raise Refused('invalid_orientation', 422)
        if name == 'launchApp':
            permissions = options.get('permissions', {})
            if not isinstance(permissions, dict) or any(
                not isinstance(permission, str) or not isinstance(setting, str) or
                setting not in {'allow', 'deny', 'unset'} or
                (permission == 'all' and setting != 'unset') or
                (permission not in {'all', 'notifications'} and not permission.startswith('android.permission.'))
                for permission, setting in permissions.items()):
                raise Refused('unsupported_uiautomator_permissions', 422)
        if name in {'tapOn', 'doubleTapOn', 'longPressOn', 'assertVisible', 'assertNotVisible',
                    'copyTextFrom', 'exists', 'setText'}:
            options = value if isinstance(value, dict) else {}
            if 'point' in options:
                if name not in {'tapOn', 'doubleTapOn', 'longPressOn'} or set(options) & SELECTORS:
                    raise Refused('invalid_point_selector', 422)
                point = options['point']
                if not isinstance(point, str) or not re.fullmatch(
                        r'\s*\d+(?:\.\d+)?%?\s*,\s*\d+(?:\.\d+)?%?\s*', point):
                    raise Refused('invalid_point', 422)
            else:
                validate_selector({k: v for k, v in options.items() if k in SELECTORS}
                                  if isinstance(value, dict) else value)
            if name == 'setText' and (not isinstance(value, dict) or not isinstance(value.get('value'), str)):
                raise Refused('invalid_set_text', 422)
        if name in {'extendedWaitUntil', 'scrollUntilVisible', 'runFlow', 'repeat'}:
            conditions = [value] if name in {'extendedWaitUntil', 'scrollUntilVisible'} else [
                value.get('when', {}), value.get('while', {})]
            for condition in conditions:
                for key in ('visible', 'notVisible', 'element'):
                    if key in condition:
                        validate_selector(condition[key])
            if name == 'extendedWaitUntil' and len(set(value) & {'visible', 'notVisible'}) != 1:
                raise Refused('invalid_wait_condition', 422)
        if name == 'readScreenText':
            validate_screen_text(value)
        if name == 'sleep' and (type(value) is not int or not 0 <= value <= 10000):
            raise Refused('invalid_sleep_ms', 422)
        if name in {'repeat', 'retry', 'runFlow'}:
            validate_commands(value['commands'])
        if name == 'launchApp' and value.get('clearKeychain'):
            raise Refused('unsupported_uiautomator_option', 422)
        if name in {'launchApp', 'stopApp', 'killApp', 'clearState'}:
            package = value.get('appId') if isinstance(value, dict) else value
            if not isinstance(package, str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_.]+', package):
                raise Refused('invalid_app_id', 422)


def reference():
    from .app_maps import reference as map_reference
    return {'engine': 'uiautomator2', 'dialect_version': 4, 'library': map_reference(),
            'commands': {name: sorted(UI_FIELDS.get(name, set())) for name in sorted(SUPPORTED)},
            'selector_fields': sorted(SELECTORS),
            'format': 'YAML command list or structured scenario with map, params, side_effects and steps. All steps run on the USB host in one batch.',
            'selectors': 'text and id are Android regular expressions; textExact and resourceId are exact. Combine fields with AND.',
            'examples': {'search': '- tapOn: Search\n- inputText: ${QUERY}\n- pressKey: Enter',
                         'set_text': '- setText: {resourceId: "example:id/search", value: "${QUERY}"}',
                         'exists': '- exists: {text: Popup, timeout: 100}',
                         'sleep': '- sleep: 500  # milliseconds, at most 10000; prefer waitSettled or a screen forbid condition',
                         'orders': '- launchApp: {appId: com.tokopedia.tkpd, stopApp: false}\n- tapOn: "Transaction|Transaksi"\n- checkpoint: orders'},
            'lookup_timeouts': {'element_timeout_ms': 5000, 'optional_timeout_ms': 1000,
                                'override': 'selector.timeout or when/while.timeout, in milliseconds'},
            'compatibility': 'Saved command lists and literal parameters remain usable. Unsupported commands fail validation before execution. Legacy retryTapIfNoChange is accepted but never repeats a tap. waitToSettleTimeoutMs is an explicit post-tap wait.',
            'receipts': 'steps contains path, command name, elapsed_ms and result. First failure has a durable screenshot and compact hierarchy. Poll running/unknown with the same request_id; never replay uncertain mutations.',
            'parameters': 'Literal ${UPPER_CASE} substitution, including saved subflows.',
            'screen_text': 'readScreenText: {region: {x, y, w, h} | within: <selector>, min_confidence: 0-100 (60), limit: 1-300 (100), key: name (screen_text)} '
                           'recognizes text in one screenshot on the USB host with Tesseract when the host config sets ocr_command. '
                           'Use it when the hierarchy has no text, e.g. canvas-drawn list cards. It returns lines {text, bounds, confidence} in full-screen '
                           'device pixels, top to bottom, also under receipt.data.<key>. Read-only; it never taps. ocr_unavailable or ocr_failed is a definite failure. '
                           'structure: true adds structure {blocks: [{bounds, confidence, node?, lines: [{text, bounds, confidence, row, source: ocr|tree, node?, words: [{text, bounds, confidence}]}]}], '
                           'lists: [{bounds, items: [block indexes]}], tree: merged|none|unavailable, truncated}, built from the OCR layout and geometry only. '
                           'A clickable or identified tree container groups the text inside it; node is the smallest tree node containing a block; source tree marks text the tree already has. '
                           'Repeated blocks of the same shape form lists, e.g. order cards. receipt.data.<key> then holds {lines, blocks, lists, tree, truncated}.',
            'limits': {'source_bytes': 32768, 'expanded_commands': 200, 'reference_depth': 8, 'executed_steps': 2000}}


def stop_script(pid, token):
    """A token check on both sides of kill prevents acting on a reused PID."""
    return (f'case "$(cat /proc/{pid}/cmdline 2>/dev/null)" in *{token}*) '
            f'kill {pid}; i=0; while kill -0 {pid} 2>/dev/null && [ "$i" -lt 20 ]; '
            'do sleep 0.1; i=$((i+1)); done; '
            f'if kill -0 {pid} 2>/dev/null; then kill -9 {pid}; sleep 0.2; fi;; esac; '
            f'case "$(cat /proc/{pid}/cmdline 2>/dev/null)" in *{token}*) exit 1;; esac')


class UiRunner(FlowRunner):
    def __init__(self, call, element_timeout_ms, optional_timeout_ms):
        super().__init__(call, element_timeout_ms, optional_timeout_ms)
        self.steps = []

    async def probe(self, selector, timeout, visible=True):
        result = await self.call({'extendedWaitUntil': {
            'visible' if visible else 'notVisible': selector, 'timeout': timeout}},
            element_timeout_ms=self.element_timeout_ms, optional_timeout_ms=self.optional_timeout_ms)
        return result['matched']

    async def step(self, command, path):
        name = next(iter(command)) if isinstance(command, dict) else command
        if name in {'runFlow', 'repeat', 'retry'}:
            return await super().step(command, path)
        if len(self.steps) >= 2000:
            raise Refused('flow_step_limit')
        started = time.monotonic()
        step = {'path': path, 'command': name}
        if isinstance(command, dict) and isinstance(command[name], dict) and command[name].get('map_ref'):
            step['map'] = command[name]['map_ref']
        try:
            step['result'] = await self.call(command, element_timeout_ms=self.element_timeout_ms,
                                             optional_timeout_ms=self.optional_timeout_ms)
            if name == 'extendedWaitUntil' and not step['result']['matched']:
                raise Refused('element_not_found')
        except Refused as error:
            step['error'] = error.code
            if hasattr(error, 'details'):
                step['details'] = error.details
            raise
        finally:
            step['elapsed_ms'] = round((time.monotonic() - started) * 1000, 2)
            self.steps.append(step)


class Uiautomator:
    def __init__(self, config, lock_fd):
        self.config, self.lock_fd = config, lock_fd
        self.processes = {}
        self.locks = {}

    async def close(self, device=None):
        from .device import stop_process
        names = [device] if device else list(self.processes)
        for name in names:
            process = self.processes.pop(name, None)
            if process:
                # EOF lets the worker stop only its own Android server in finally.
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    await stop_process(process)
            await self.cleanup(name)

    def marker(self, device):
        return Path(self.config.state_dir) / ('uia2-' + str(list(self.config.devices).index(device)) + '.json')

    async def ensure_fenced_process_stopped(self, device):
        if device not in self.processes:
            await self.cleanup(device)

    async def cleanup(self, device):
        from .device import run_process
        marker = self.marker(device)
        if not marker.exists():
            return
        state = json.loads(marker.read_text())
        pid, token = int(state['pid']), state['token']
        # PID reuse is harmless: only a process bearing our unique startup token is stopped.
        if not token.startswith('deedkit-uia2-') or not token.replace('-', '').isalnum() or pid < 2:
            raise Refused('uiautomator_cleanup_required', 503)
        script = stop_script(pid, token)
        code, _, _ = await run_process([self.config.adb, '-s', self.config.devices[device], 'shell', script], 6)
        if code:
            raise Refused('uiautomator_cleanup_required', 503)
        marker.unlink(missing_ok=True)

    async def call(self, device, command, **options):
        lock = self.locks.setdefault(device, asyncio.Lock())
        async with lock:
            try:
                process = self.processes.get(device)
                if process is None or process.returncode is not None:
                    await self.cleanup(device)
                    env = {key: os.environ[key] for key in ('HOME', 'TMPDIR', 'LANG') if key in os.environ}
                    env['PATH'] = str(Path(self.config.adb).parent) + ':/usr/bin:/bin'
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, '-m', 'deedkit_android.ui_worker', self.config.devices[device],
                        self.config.uiautomator2_jar, str(self.marker(device)),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True,
                        limit=4 * 1024 * 1024, pass_fds=(self.lock_fd,))
                    self.processes[device] = process
                if getattr(self.config, 'ocr_command', None):
                    options = {**options, 'ocr': {'command': self.config.ocr_command, 'languages': self.config.ocr_languages}}
                process.stdin.write(json.dumps({'command': command, **options}).encode() + b'\n')
                await process.stdin.drain()
                response = await process.stdout.readline()
                if not response:
                    raise Refused('uiautomator_disconnected', 503)
                result = json.loads(response)
                if 'error' in result:
                    error = Refused(result['error'], 409)
                    if 'details' in result:
                        error.details = result['details']
                    if 'hint' in result:
                        error.hint = result['hint']
                    raise error
                return result
            except Refused as error:
                if error.code not in DEFINITE | {'unsupported_uiautomator_command', 'invalid_point'}:
                    await self.close(device)
                raise
            except BaseException:
                # No library retry. Fence the old Android server before releasing admission.
                await self.close(device)
                raise
