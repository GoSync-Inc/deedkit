"""Bounded, device-only Maestro YAML. Host scripts and file references stay private."""

import re

import yaml
from yaml.tokens import AliasToken, AnchorToken

from .config import Refused

SELECTOR = {'text', 'id', 'index', 'point', 'enabled', 'selected', 'checked', 'focused',
            'optional', 'retryTapIfNoChange', 'waitToSettleTimeoutMs', 'label', 'longPress',
            'repeat', 'delay', 'width', 'height', 'below', 'above', 'leftOf', 'rightOf',
            'containsChild', 'containsDescendants', 'childOf', 'traits', 'tolerance', 'waitUntilVisible', 'timeout'}
FIELDS = {
    'tapOn': SELECTOR, 'doubleTapOn': SELECTOR, 'longPressOn': SELECTOR,
    'assertVisible': SELECTOR, 'assertNotVisible': SELECTOR,
    'inputText': {'text', 'label'}, 'eraseText': {'charactersToErase', 'label'},
    'pressKey': {'key', 'label'},
    'launchApp': {'appId', 'stopApp', 'clearState', 'clearKeychain', 'permissions', 'arguments', 'label'},
    'stopApp': {'appId', 'label'}, 'openLink': {'link', 'autoVerify', 'browser', 'label'},
    'swipe': {'start', 'end', 'direction', 'duration', 'from', 'label'},
    'scrollUntilVisible': {'element', 'direction', 'timeout', 'speed', 'visibilityPercentage',
                           'centerElement', 'label'},
    'extendedWaitUntil': {'visible', 'notVisible', 'timeout', 'label'},
    'waitForAnimationToEnd': {'timeout', 'label'},
    'runFlow': {'commands', 'name', 'version', 'when', 'label'}, 'repeat': {'times', 'commands', 'while', 'label'},
    'retry': {'maxRetries', 'commands', 'label'}, 'checkpoint': {'label'},
    'copyTextFrom': SELECTOR, 'clearState': {'appId', 'label'}, 'killApp': {'appId', 'label'},
    'setOrientation': {'orientation', 'label', 'optional'}, 'setLocation': {'latitude', 'longitude', 'label', 'optional'},
    'setAirplaneMode': {'value', 'label', 'optional'}, 'toggleAirplaneMode': {'label', 'optional'},
    'setDarkMode': {'value', 'label', 'optional'}, 'toggleDarkMode': {'label', 'optional'},
    'assertDarkMode': {'label', 'optional'}, 'assertLightMode': {'label', 'optional'},
    'setClipboard': {'text', 'label', 'optional'},
    'setPermissions': {'appId', 'permissions', 'label', 'optional'},
    'travel': {'points', 'speed', 'label', 'optional'},
    'inputRandomText': {'length', 'label', 'optional'}, 'inputRandomNumber': {'length', 'label', 'optional'},
}
BARE = {'back', 'scroll', 'hideKeyboard', 'pasteText', 'toggleAirplaneMode', 'toggleDarkMode', 'assertDarkMode', 'assertLightMode'}
VARIABLE = re.compile(r'\$\{([A-Z][A-Z0-9_]{0,63})\}')


def compile_flow(source, parameters, *, resolve=None, _stack=(), _budget=None, _depth=0):
    if len(source.encode()) > 32768 or len(parameters) > 64:
        raise Refused('flow_too_large', 422)
    if any(not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', k) or len(v) > 4096 or '${' in v
           for k, v in parameters.items()):
        raise Refused('invalid_flow_parameters', 422)
    try:
        if any(isinstance(t, (AliasToken, AnchorToken)) for t in yaml.scan(source)):
            raise Refused('flow_aliases_not_supported', 422)
        commands = yaml.safe_load(source)
    except yaml.YAMLError:
        error = Refused('invalid_flow_yaml', 422)
        error.hint = 'Provide only a YAML command list, without appId or the --- header separator. Example: - back'
        raise error from None
    budget = [0] if _budget is None else _budget

    def value(item, depth=0):
        if depth > 16:
            raise Refused('flow_too_deep', 422)
        if isinstance(item, str):
            try:
                item = VARIABLE.sub(lambda match: parameters[match[1]], item)
            except KeyError:
                raise Refused('missing_flow_parameter', 422) from None
            if '${' in item or '\x00' in item:
                raise Refused('flow_expressions_not_supported', 422)
            return item
        if isinstance(item, list):
            return [value(child, depth + 1) for child in item]
        if isinstance(item, dict):
            if any(not isinstance(key, str) or '${' in key or '\x00' in key or key in {'script', 'file', 'path', 'image', 'runScript',
                                                        'evalScript', 'onFlowStart', 'onFlowComplete'}
                   for key in item):
                raise Refused('invalid_flow_key', 422)
            return {key: value(child, depth + 1) for key, child in item.items()}
        if item is None or type(item) in (bool, int, float):
            return item
        raise Refused('invalid_flow_value', 422)

    def sequence(items, depth=0):
        if not isinstance(items, list) or not items or depth > 8:
            error = Refused('flow_requires_command_list', 422)
            error.hint = 'Provide only a YAML command list, without appId or the --- header separator. Example: - back'
            raise error
        output = []
        for command in items:
            budget[0] += 1
            if budget[0] > 200:
                raise Refused('too_many_flow_commands', 422)
            if isinstance(command, str) and command in BARE:
                output.append(command)
                continue
            if not isinstance(command, dict) or len(command) != 1:
                raise Refused('invalid_flow_command', 422)
            name, argument = next(iter(command.items()))
            if name not in FIELDS:
                raise Refused('unsupported_flow_command', 422)
            if isinstance(argument, dict) and set(argument) - FIELDS[name]:
                raise Refused('unsupported_flow_option', 422)
            if isinstance(argument, dict):
                for condition in ('when', 'while'):
                    if condition in argument and (not isinstance(argument[condition], dict)
                            or set(argument[condition]) - {'platform', 'visible', 'notVisible', 'timeout'}):
                        raise Refused('unsupported_flow_condition', 422)
                    if condition in argument:
                        timeout = argument[condition].get('timeout', 1000)
                        if type(timeout) is not int or not 100 <= timeout <= 60000:
                            raise Refused('invalid_element_timeout', 422)
                        platform = argument[condition].get('platform', 'android')
                        if not isinstance(platform, str) or platform.lower() not in {'android', 'ios', 'web'}:
                            raise Refused('unsupported_flow_condition', 422)
                if name in {'tapOn', 'doubleTapOn', 'longPressOn', 'assertVisible', 'assertNotVisible', 'copyTextFrom'}:
                    timeout = argument.get('timeout', 5000)
                    if type(timeout) is not int or not 100 <= timeout <= 60000:
                        raise Refused('invalid_element_timeout', 422)
            if name == 'checkpoint' and (depth or not isinstance(argument, str)):
                error = Refused('checkpoint_requires_top_level_label', 422)
                error.hint = 'Use a scalar checkpoint at top level: - checkpoint: verify'
                raise error
            if name == 'runFlow' and isinstance(argument, dict) and 'name' in argument:
                reference = argument['name']
                if (resolve is None or not isinstance(reference, str) or 'commands' in argument
                        or reference in _stack or len(_stack) >= 8):
                    raise Refused('invalid_flow_reference', 422)
                nested = compile_flow(resolve(reference, argument.get('version')), parameters,
                                      resolve=resolve, _stack=(*_stack, reference), _budget=budget, _depth=depth + 1)
                argument = {key: child for key, child in argument.items() if key not in {'name', 'version'}}
                argument['commands'] = nested
                # The referenced source has already consumed the shared command budget.
                output.append({name: argument})
                continue
            if name in {'runFlow', 'repeat', 'retry'}:
                if not isinstance(argument, dict) or 'commands' not in argument:
                    raise Refused('inline_flow_required', 422)
                if name in {'repeat', 'retry'}:
                    field = 'times' if name == 'repeat' else 'maxRetries'
                    if field in argument:
                        try:
                            if isinstance(argument[field], bool) or not 0 <= float(argument[field]) <= 1000000:
                                raise ValueError
                            if name == 'retry' and float(argument[field]) != int(argument[field]):
                                raise ValueError
                        except (ValueError, TypeError, OverflowError):
                            raise Refused('invalid_flow_count', 422) from None
                argument = {**argument, 'commands': sequence(argument['commands'], depth + 1)}
            if name == 'launchApp':
                if isinstance(argument, str):
                    argument = {'appId': argument}
                if not isinstance(argument, dict):
                    raise Refused('invalid_launch_app', 422)
                argument = {'stopApp': False, 'permissions': {'all': 'unset'}, **argument}
            output.append({name: argument})
        return output

    return sequence(value(commands), _depth)


def reference():
    """The accepted dialect, derived from the compiler's own allowlists."""
    return {"dialect_version": 2, "commands": {name: sorted(FIELDS.get(name, set()))
                                               for name in sorted(FIELDS.keys() | BARE)},
            "bare_commands": sorted(BARE), "selector_fields": sorted(SELECTOR),
            "parameters": "Literal ${UPPER_CASE} substitution. The same parameters reach saved subflows.",
            "saved_subflow": {"runFlow": {"name": "saved-name", "version": "optional SHA256"}},
            "format": "YAML command list only; omit appId and ---. Checkpoints are top-level scalars: - checkpoint: verify",
            "lookup_timeouts": {"element_timeout_ms": 5000, "optional_timeout_ms": 1000,
                                "override": "run_flow arguments, selector.timeout, or when/while.timeout (100..60000 ms)",
                                "note": "Native visibility probes precede actions. These bound lookup waits, not driver startup or a tap after the probe."},
            "limits": {"source_bytes": 32768, "expanded_commands": 200, "reference_depth": 8},
            "examples": {"relative_selector": "- tapOn: {text: Buy, below: {id: product_title}}",
                         "search": "- tapOn: Search\n- inputText: ${QUERY}\n- pressKey: Enter",
                         "dismiss_known_permission": "- runFlow:\n    when: {visible: {id: 'com.android.permissioncontroller:id/permission_deny_button'}, timeout: 300}\n    commands:\n      - tapOn: {id: 'com.android.permissioncontroller:id/permission_deny_button', timeout: 300}"},
            "unsupported": ["host files", "JavaScript", "cloud runs", "viewer"]}
