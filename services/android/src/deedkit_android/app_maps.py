"""Validated application data and compilation into the existing batch runner."""

from datetime import date
import hashlib
import math
import re

import yaml
from yaml.tokens import AliasToken, AnchorToken

from .config import Refused
from .flows import VARIABLE, compile_flow
from .uiautomator import UI_FIELDS, UNSUPPORTED_HINT, validate_commands, validate_selector

MAP_ACTIONS = {
    'openApp': {'cold', 'expect_screen', 'timeout', 'settle_ms'},
    'openDeepLink': {'name', 'timeout', 'settle_ms'},
    'waitSettled': {'stable_ms', 'timeout'},
    'tapElement': {'selector', 'expect_screen', 'timeout'},
    'typeInto': {'selector', 'text', 'clear', 'submit', 'expect_screen', 'timeout'},
    'waitScreen': {'screen', 'timeout'}, 'assertScreen': {'screen'},
    'readList': {'list', 'fields', 'limit', 'scroll_pages'},
    'inspectMap': {'hierarchy', 'suggest', 'max_nodes'}, 'dismissPopups': {'max_rounds'},
}
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}')


def reference():
    return {
        'actions': {name: sorted(options) for name, options in MAP_ACTIONS.items()},
        'map_example': 'package: example.shop\nselectors:\n  orders: [{textExact: Orders}]\n'
                       'screens:\n  orders:\n    require: [{textExact: Orders, selected: true}]',
        'scenario_example': 'map: shop\nside_effects: none\nsteps:\n'
                            '  - openApp: {cold: false}\n'
                            '  - tapElement: {selector: orders, expect_screen: orders}',
        'workflow': [
            'android_flows(action=draft, kind=map, name=shop, yaml=<map>, base_version=null). Keep its version.',
            'android_flows(action=draft, name=orders, yaml=<scenario>, base_version=null). Keep its version.',
            'Acquire android_session. Run android_run_flow(name=orders, version=<draft version>, stage=draft, request_id=<UUID>).',
            'Wait for succeeded with android_get_operation. A paused checkpoint is not a passing test.',
            'Publish the tested map, then the tested flow with android_flows(action=publish, kind=map|flow, name=..., version=..., test_request_id=<same UUID>, base_version=<previous published version or null>).',
            'Use android_run_flow(name=orders, request_id=<new UUID>) for normal runs. Release the session after work.',
        ],
        'inspect': 'inspectMap returns match counts per selector and screen. Add hierarchy: true for compact nodes (index, depth, class, resource-id, text, content-desc, bounds, clickable, scrollable; password text omitted; max_nodes 1-500, default 200) and suggest: true for unique selectors (resourceId, then textExact, then description) and list candidates (an id or class repeated under one parent, its count and field ids present in at least half the items; has_text false means try the map hierarchy option or readScreenText). At most 50 of each.',
        'repair': 'Get the current source with android_flows(action=get, stage=draft). Save a draft with base_version equal to the current draft version, or published version when no draft exists. Inspect with a draft scenario containing inspectMap, test the intended scenario, then publish. history returns versions and audit events; rollback takes a previously published version and the current published base_version.',
        'hierarchy': 'Optional map field hierarchy: {compressed: false, max_depth: 1-200}. Every snapshot for that map then dumps the full accessibility tree; the default (compressed true, depth 50) hides nodes that are not important for accessibility and deep subtrees. Try it when inspectMap finds no text, before readScreenText. Plain selector commands later in the same batch may then also see the full tree.',
        'maps': 'Fields: package, selectors, screens, deep_links, popups, lists, hierarchy, meta. Named selectors are ordered candidate lists. text/id are full regular expressions, textExact/resourceId are exact. An ambiguous first matching candidate stops the action. index explicitly selects one match. A point candidate requires tapElement.expect_screen. Screens require all require conditions and no forbid conditions; each condition is a selector name or inline selector. Optional activity is a regex.',
        'lists': 'lists.<name> has item (selector or candidate list), fields (field names mapped to selectors), and optional key (field names identifying one item across pages). Without key, every visible row is preserved. readList returns rows under both its step result and receipt.data.<list>. limit defaults to 20, at most 100; scroll_pages defaults to 0, at most 5. Password fields return null.',
        'loading': 'A screen signature can match while content still loads. Prefer forbid: [<spinner selector>] on the screen so waits continue until it is gone. waitSettled {stable_ms: 100-5000 (500), timeout} waits until the hierarchy (class, id, text, description, bounds) is unchanged for stable_ms and fails with screen_not_settled. openApp and openDeepLink accept settle_ms (0-5000) to settle after their screen check. openDeepLink returns phases {open_ms, screen_ms, settle_ms} and polls. The plain sleep command (milliseconds, at most 10000) is a last resort.',
        'popups': 'popups.<name> has detect (selector name) and either dismiss (selector name) or press: back. dismissPopups is explicit and bounded by max_rounds, default 3.',
        'parameters': 'params maps UPPER_CASE names to {type: string|integer|boolean, default?, required?, enum?, max_length?}. Values substitute literally as ${NAME}. Existing string parameters work without declarations.',
        'continuation': 'A checkpoint returns runnable remaining_yaml with explicit historical map versions. Submit it under a new request_id after resolving the checkpoint. Returned runFlow groups may contain map, map_version and commands to retain their map context.',
        'versions': 'Every run records library.pins. Unversioned references resolve once at admission. stage=draft uses current drafts with published fallback. Explicit map_version and runFlow.version pin historical dependencies. Publication rejects stale floating dependencies.',
        'effects': 'side_effects is advisory metadata (none, purchase, payment, send, account). The caller decides when user approval is required.',
    }


def load(source):
    if not isinstance(source, str) or len(source.encode()) > 32768:
        raise Refused('invalid_flow_source', 422)
    try:
        if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(source)):
            raise Refused('flow_aliases_not_supported', 422)
        def literal(value, depth=0):
            if depth > 16:
                raise Refused('flow_too_deep', 422)
            if isinstance(value, date):
                return value.isoformat()
            if isinstance(value, list):
                return [literal(child, depth + 1) for child in value]
            if isinstance(value, dict):
                if any(not isinstance(key, str) for key in value):
                    raise Refused('invalid_flow_key', 422)
                return {key: literal(child, depth + 1) for key, child in value.items()}
            if value is None or type(value) in (str, bool, int):
                return value
            if type(value) is float and math.isfinite(value):
                return value
            raise Refused('invalid_flow_value', 422)
        return literal(yaml.safe_load(source))
    except (yaml.YAMLError, RecursionError):
        error = Refused('invalid_flow_yaml', 422)
        error.hint = 'Use one YAML document. Use a command list such as - back, or a scenario with map and steps. Omit any appId/--- header.'
        raise error from None


def fields(value, allowed, code='invalid_map_definition'):
    if not isinstance(value, dict) or set(value) - allowed:
        raise Refused(code, 422)


def named(value):
    if not isinstance(value, dict) or len(value) > 128 or any(not isinstance(k, str) or not NAME.fullmatch(k) for k in value):
        raise Refused('invalid_map_names', 422)


def candidates(value, point=False):
    items = value if isinstance(value, list) else [value]
    if not 1 <= len(items) <= 8:
        raise Refused('invalid_map_candidates', 422)
    for item in items:
        if point and isinstance(item, dict) and set(item) == {'point'}:
            if not isinstance(item['point'], str) or not re.fullmatch(r'\d+(?:\.\d+)?%?,\s*\d+(?:\.\d+)?%?', item['point']):
                raise Refused('invalid_point', 422)
        else:
            validate_selector(item)
            if not isinstance(item, dict):
                raise Refused('map_selector_requires_fields', 422)
            for key in ('text', 'id'):
                if key in item:
                    try:
                        re.compile(item[key])
                    except re.error:
                        raise Refused('invalid_selector_regex', 422) from None
    return items


def validate_map(source):
    value = load(source)
    fields(value, {'package', 'selectors', 'screens', 'deep_links', 'popups', 'lists', 'meta', 'hierarchy'})
    if not isinstance(value.get('package'), str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', value['package']):
        raise Refused('invalid_app_package', 422)
    for name in ('selectors', 'screens', 'deep_links', 'popups', 'lists'):
        value.setdefault(name, {})
        named(value[name])
    value['selectors'] = {name: candidates(selector, True) for name, selector in value['selectors'].items()}
    for screen in value['screens'].values():
        fields(screen, {'require', 'forbid', 'activity'})
        if not screen.get('require'):
            raise Refused('screen_requires_signature', 422)
        for name in ('require', 'forbid'):
            conditions = screen.get(name, [])
            if not isinstance(conditions, list) or len(conditions) > 32:
                raise Refused('invalid_screen_conditions', 422)
            for condition in conditions:
                if isinstance(condition, str):
                    if condition not in value['selectors'] or any('point' in c for c in value['selectors'][condition]):
                        raise Refused('unknown_screen_selector', 422)
                else:
                    candidates(condition)
        if 'activity' in screen:
            if not isinstance(screen['activity'], str):
                raise Refused('invalid_activity_regex', 422)
            try:
                re.compile(screen['activity'])
            except re.error:
                raise Refused('invalid_activity_regex', 422) from None
    for link in value['deep_links'].values():
        fields(link, {'url', 'screen', 'verified'})
        if (not isinstance(link.get('url'), str) or not re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', link['url'])
                or not isinstance(link.get('screen'), str) or link['screen'] not in value['screens'] or type(link.get('verified', False)) is not bool):
            raise Refused('invalid_deep_link', 422)
    for popup in value['popups'].values():
        fields(popup, {'detect', 'dismiss', 'press'})
        if not isinstance(popup.get('detect'), str) or popup['detect'] not in value['selectors'] or (('dismiss' in popup) == ('press' in popup)):
            raise Refused('invalid_popup', 422)
        if ('dismiss' in popup and (not isinstance(popup['dismiss'], str) or popup['dismiss'] not in value['selectors'])) or ('press' in popup and popup['press'] != 'back'):
            raise Refused('invalid_popup', 422)
        for selector_name in (popup['detect'], popup.get('dismiss')):
            if selector_name and any('point' in c for c in value['selectors'][selector_name]):
                raise Refused('popup_requires_selector', 422)
    for listing in value['lists'].values():
        fields(listing, {'item', 'fields', 'key'})
        listing['item'] = candidates(listing.get('item'))
        named(listing.get('fields'))
        if not listing['fields']:
            raise Refused('list_requires_fields', 422)
        listing['fields'] = {name: candidates(selector) for name, selector in listing['fields'].items()}
        if 'key' in listing and (not isinstance(listing['key'], list) or not listing['key'] or any(not isinstance(key, str) or key not in listing['fields'] for key in listing['key'])):
            raise Refused('invalid_list_key', 422)
    if 'hierarchy' in value:
        options = value['hierarchy']
        fields(options, {'compressed', 'max_depth'}, 'invalid_map_hierarchy')
        if ('compressed' in options and type(options['compressed']) is not bool) or (
                'max_depth' in options and (type(options['max_depth']) is not int or not 1 <= options['max_depth'] <= 200)):
            raise Refused('invalid_map_hierarchy', 422)
    if 'meta' in value and not isinstance(value['meta'], dict):
        raise Refused('invalid_map_metadata', 422)
    return value


def validate_action(name, options, app_map):
    fields(options, MAP_ACTIONS[name] | {'app_map'}, 'invalid_map_action')
    if 'selector' in options and (not isinstance(options['selector'], str) or options['selector'] not in app_map['selectors']):
        raise Refused('unknown_map_selector', 422)
    if name in {'tapElement', 'typeInto'}:
        if not isinstance(options.get('selector'), str) or options['selector'] not in app_map['selectors']:
            raise Refused('unknown_map_selector', 422)
        points = any('point' in item for item in app_map['selectors'][options['selector']])
        if points and (name != 'tapElement' or not options.get('expect_screen')):
            raise Refused('coordinate_requires_screen', 422)
    for key in ('screen', 'expect_screen'):
        if key in options and (not isinstance(options[key], str) or options[key] not in app_map['screens']):
            raise Refused('unknown_map_screen', 422)
    if name in {'waitScreen', 'assertScreen'} and 'screen' not in options:
        raise Refused('unknown_map_screen', 422)
    if name == 'openDeepLink' and (not isinstance(options.get('name'), str) or options['name'] not in app_map['deep_links']):
        raise Refused('unknown_deep_link', 422)
    if name == 'typeInto' and not isinstance(options.get('text'), str):
        raise Refused('invalid_input_text', 422)
    for key in ('cold', 'clear', 'submit', 'hierarchy', 'suggest'):
        if key in options:
            if options[key] in ('true', 'True', 'false', 'False'):
                options[key] = options[key].lower() == 'true'
            if type(options[key]) is not bool:
                raise Refused('invalid_map_action', 422)
    for key, low, high in (('timeout', 0, 60000), ('limit', 1, 100), ('scroll_pages', 0, 5), ('max_rounds', 1, 5),
                            ('settle_ms', 0, 5000), ('stable_ms', 100, 5000), ('max_nodes', 1, 500)):
        if key in options:
            try:
                if isinstance(options[key], bool) or str(int(options[key])) != str(options[key]) or not low <= int(options[key]) <= high:
                    raise ValueError
                options[key] = int(options[key])
            except (ValueError, TypeError):
                raise Refused('invalid_map_action', 422) from None
    if name == 'readList':
        listing = app_map['lists'].get(options['list']) if isinstance(options.get('list'), str) else None
        if listing is None or ('fields' in options and (not isinstance(options['fields'], list)
                or any(not isinstance(key, str) or key not in listing['fields'] for key in options['fields']))):
            raise Refused('unknown_map_list_field', 422)


def compile_scenario(library, owner, source, parameters, *, stage='published', root=None, validate_only=False):
    pins, floating, effects = {}, {}, set()
    stack = []
    budget = [0]
    root_pin = None

    def resolve(kind, name, version=None):
        row = library.read(owner, kind, name, version, stage)
        pin = {'kind': kind, 'name': name, 'version': row['version']}
        pins[(kind, name, row['version'])] = pin
        if version is None:
            previous = floating.get((kind, name))
            if previous and previous != pin:
                raise Refused('library_changed_during_resolution')
            floating[(kind, name)] = pin
        return row['yaml']

    if root:
        source = resolve('flow', root['name'], root.get('version'))
        root_pin = {'kind': 'flow', 'name': root['name'], 'version': hashlib.sha256(source.encode()).hexdigest()}

    def compile_one(text, inherited_map=None, inherited_values=None, inherited_ref=None):
        if len(stack) > 8:
            raise Refused('flow_too_deep', 422)
        document = load(text)
        def check_input(value, depth=0):
            if depth > 16:
                raise Refused('flow_too_deep', 422)
            if isinstance(value, dict):
                if 'app_map' in value or 'map_ref' in value:
                    raise Refused('reserved_scenario_field', 422)
                for child in value.values():
                    check_input(child, depth + 1)
            elif isinstance(value, list):
                for child in value:
                    check_input(child, depth + 1)
        check_input(document)
        values = dict(inherited_values) if inherited_values is not None else {key: str(value) for key, value in parameters.items()}
        app_map, map_ref = inherited_map, inherited_ref
        if isinstance(document, dict):
            fields(document, {'map', 'map_version', 'params', 'steps', 'side_effects', 'description'}, 'invalid_scenario')
            if 'map' in document:
                map_source = resolve('map', document['map'], document.get('map_version'))
                app_map = validate_map(map_source)
                map_ref = {'kind': 'map', 'name': document['map'], 'version': hashlib.sha256(map_source.encode()).hexdigest()}
            effect = document.get('side_effects', 'none')
            if not isinstance(effect, str) or effect not in {'none', 'purchase', 'payment', 'send', 'account'}:
                raise Refused('invalid_side_effects', 422)
            effects.add(effect)
            declarations = document.get('params', {})
            named(declarations)
            for key, definition in declarations.items():
                fields(definition, {'type', 'default', 'required', 'enum', 'max_length'}, 'invalid_parameter_definition')
                if ('required' in definition and type(definition['required']) is not bool) or (
                        'max_length' in definition and (type(definition['max_length']) is not int or not 1 <= definition['max_length'] <= 4096)) or (
                        'enum' in definition and (not isinstance(definition['enum'], list) or not 1 <= len(definition['enum']) <= 128)):
                    raise Refused('invalid_parameter_definition', 422)
                kind = definition.get('type', 'string')
                if not isinstance(kind, str) or kind not in {'string', 'integer', 'boolean'} or not VARIABLE.fullmatch('${' + key + '}'):
                    raise Refused('invalid_parameter_definition', 422)
                if key not in values:
                    if 'default' in definition:
                        values[key] = str(definition['default'])
                    elif validate_only:
                        values[key] = '1' if kind == 'integer' else 'true' if kind == 'boolean' else 'placeholder'
                    elif definition.get('required', True):
                        raise Refused('missing_flow_parameter', 422)
                if key in values:
                    value = values[key]
                    if (kind == 'integer' and not re.fullmatch(r'-?\d+', value)) or (kind == 'boolean' and value.lower() not in {'true', 'false'}):
                        raise Refused('invalid_flow_parameter_type', 422)
                    if len(value) > definition.get('max_length', 4096) or ('enum' in definition and value not in [str(v) for v in definition['enum']] and not validate_only):
                        raise Refused('invalid_flow_parameter_value', 422)
            commands = document.get('steps')
        else:
            commands = document
        if validate_only:
            values.update({name: values.get(name, 'placeholder') for name in VARIABLE.findall(text)})

        # Inline saved children with their own map context before the base compiler
        # validates control flow and substitutes literal parameter values.
        def expand(items):
            if not isinstance(items, list):
                raise Refused('flow_requires_command_list', 422)
            result = []
            for item in items:
                budget[0] += 1
                if budget[0] > 200:
                    raise Refused('too_many_flow_commands', 422)
                if isinstance(item, str):
                    result.append(item)
                    continue
                if not isinstance(item, dict) or len(item) != 1:
                    raise Refused('invalid_flow_command', 422)
                name, options = next(iter(item.items()))
                if name == 'runFlow' and isinstance(options, dict) and 'name' in options:
                    reference = options['name']
                    if reference in stack or 'commands' in options or set(options) - {'name', 'version', 'when', 'label'}:
                        raise Refused('invalid_flow_reference', 422)
                    stack.append(reference)
                    child = compile_one(resolve('flow', reference, options.get('version')), app_map, values, map_ref)
                    stack.pop()
                    result.append({'runFlow': {**{k: v for k, v in options.items() if k not in {'name', 'version'}}, 'commands': child}})
                elif name == 'runFlow' and isinstance(options, dict) and 'map' in options:
                    fields(options, {'map', 'map_version', 'commands'}, 'invalid_flow_reference')
                    child_source = yaml.safe_dump({'map': options['map'], 'map_version': options.get('map_version'), 'steps': options.get('commands')})
                    result.extend(compile_one(child_source, app_map, values, map_ref))
                elif name in {'runFlow', 'repeat', 'retry'} and isinstance(options, dict) and 'commands' in options:
                    result.append({name: {**options, 'commands': expand(options['commands'])}})
                else:
                    result.append(item)
            return result

        expanded = expand(commands)
        all_fields = {**UI_FIELDS, **{name: keys | {'app_map'} for name, keys in MAP_ACTIONS.items()}}
        # Children are already literal and may carry internal map dictionaries.
        # Compile each local command separately, retaining control-flow validation.
        def prepare(items, depth=0):
            if depth > 8:
                raise Refused('flow_too_deep', 422)
            output = []
            for item in items:
                name = next(iter(item)) if isinstance(item, dict) else item
                if name == 'checkpoint' and depth:
                    raise Refused('checkpoint_requires_top_level_label', 422)
                if name in {'runFlow', 'repeat', 'retry'}:
                    options = item[name]
                    child = options['commands']
                    wrapper = {name: {**options, 'commands': ['back']}}
                    parsed = compile_flow(yaml.safe_dump([wrapper]), values, fields=all_fields)[0]
                    validate_commands([parsed])
                    parsed[name]['commands'] = prepare(child, depth + 1)
                    output.append(parsed)
                elif name in MAP_ACTIONS:
                    options = item[name] if isinstance(item, dict) and item[name] is not None else {}
                    if isinstance(options, str) and name in {'waitScreen', 'assertScreen'}:
                        options = {'screen': options}
                    if not isinstance(options, dict):
                        raise Refused('invalid_map_action', 422)
                    if 'app_map' in options:
                        output.append(item)  # already compiled saved child
                        continue
                    parsed = compile_flow(yaml.safe_dump([{name: options}]), values, fields=all_fields)[0][name]
                    if app_map is None:
                        raise Refused('scenario_requires_map', 422)
                    validate_action(name, parsed, app_map)
                    output.append({name: {**parsed, 'app_map': app_map, 'map_ref': map_ref}})
                else:
                    if isinstance(item, dict) and name not in all_fields:
                        # Includes former Maestro-only commands: the engine was removed (owner 11-11).
                        error = Refused('unsupported_uiautomator_command', 422)
                        error.hint = UNSUPPORTED_HINT
                        raise error
                    parsed = compile_flow(yaml.safe_dump([item]), values, fields=all_fields)
                    validate_commands(parsed)
                    output.extend(parsed)
            if not output:
                raise Refused('flow_requires_command_list', 422)
            return output
        return prepare(expanded)

    commands = compile_one(source)
    def count(items):
        total = len(items)
        for command in items:
            if isinstance(command, dict):
                name, options = next(iter(command.items()))
                if name in {'runFlow', 'repeat', 'retry'}:
                    total += count(options['commands'])
        return total
    if count(commands) > 200:
        raise Refused('too_many_flow_commands', 422)
    return commands, {'root': root_pin, 'pins': list(pins.values()), 'floating': list(floating.values()),
                      'side_effects': sorted(effects or {'none'})}


def continuation(commands):
    """Return ordinary source with immutable map references, never internal fields."""
    output = []
    for command in commands:
        if not isinstance(command, dict):
            output.append(command)
            continue
        name, options = next(iter(command.items()))
        if name in MAP_ACTIONS:
            pin = options['map_ref']
            arguments = {key: value for key, value in options.items() if key not in {'app_map', 'map_ref'}}
            output.append({'runFlow': {'map': pin['name'], 'map_version': pin['version'],
                                      'commands': [{name: arguments}]}})
        elif name in {'runFlow', 'repeat', 'retry'}:
            output.append({name: {**options, 'commands': continuation(options['commands'])}})
        else:
            output.append(command)
    return output
