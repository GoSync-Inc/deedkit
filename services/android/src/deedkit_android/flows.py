"""Bounded, device-only Maestro YAML. Host scripts and file references stay private."""

import re

import yaml
from yaml.tokens import AliasToken, AnchorToken

from .config import Refused

SELECTOR = {'text', 'id', 'index', 'point', 'enabled', 'selected', 'checked', 'focused',
            'optional', 'retryTapIfNoChange', 'waitToSettleTimeoutMs', 'label', 'longPress',
            'repeat', 'delay', 'width', 'height'}
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
    'runFlow': {'commands', 'when', 'label'}, 'repeat': {'times', 'commands', 'while', 'label'},
    'retry': {'maxRetries', 'commands', 'label'}, 'checkpoint': {'label'},
}
BARE = {'back', 'scroll', 'hideKeyboard'}
VARIABLE = re.compile(r'\$\{([A-Z][A-Z0-9_]{0,63})\}')


def compile_flow(source, parameters):
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
        raise Refused('invalid_flow_yaml', 422) from None
    count = 0

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
        nonlocal count
        if not isinstance(items, list) or not items or depth > 8:
            raise Refused('flow_requires_command_list', 422)
        output = []
        for command in items:
            count += 1
            if count > 200:
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
            if name == 'checkpoint' and (depth or not isinstance(argument, str)):
                raise Refused('checkpoint_requires_top_level_label', 422)
            if name in {'runFlow', 'repeat', 'retry'}:
                if not isinstance(argument, dict) or 'commands' not in argument:
                    raise Refused('inline_flow_required', 422)
                argument = {**argument, 'commands': sequence(argument['commands'], depth + 1)}
            if name == 'launchApp':
                if isinstance(argument, str):
                    argument = {'appId': argument}
                if not isinstance(argument, dict):
                    raise Refused('invalid_launch_app', 422)
                argument = {'stopApp': False, 'permissions': {'all': 'unset'}, **argument}
            output.append({name: argument})
        return output

    return sequence(value(commands))
