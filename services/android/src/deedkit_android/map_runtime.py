"""Generic map actions. Application-specific navigation belongs to scenarios."""

import re
import time
from xml.etree import ElementTree

from .config import Refused

ATTRIBUTES = {'textExact': 'text', 'text': 'text', 'id': 'resource-id', 'resourceId': 'resource-id',
              'description': 'content-desc', 'className': 'class', 'enabled': 'enabled',
              'selected': 'selected', 'checked': 'checked', 'focused': 'focused',
              'clickable': 'clickable', 'scrollable': 'scrollable'}


def matches(node, selector):
    for key, expected in selector.items():
        if key == 'index':
            continue
        actual = node.get(ATTRIBUTES[key], '')
        if key in {'text', 'id'}:
            if re.fullmatch(expected, actual) is None:
                return False
        elif actual != (str(expected).lower() if isinstance(expected, bool) else expected):
            return False
    return True


def find(root, selector):
    nodes = [node for node in root.iter('node') if matches(node, selector)]
    if 'index' in selector:
        nodes = nodes[selector['index']:selector['index'] + 1]
    return nodes


def resolve_selector(root, candidates, multiple=False):
    trace = []
    for index, selector in enumerate(candidates):
        if 'point' in selector:
            trace.append({'candidate': index, 'coordinate_fallback': True})
            return selector, trace
        nodes = find(root, selector)
        trace.append({'candidate': index, 'matches': len(nodes)})
        if len(nodes) > 1 and not multiple:
            error = Refused('ambiguous_selector')
            error.details = {'candidates': trace}
            raise error
        if nodes:
            return nodes if multiple else nodes[0], trace
    return [] if multiple else None, trace


def snapshot(device):
    return ElementTree.fromstring(device.jsonrpc_call('dumpWindowHierarchy', (True, 50)))


def screen_state(root, app_map, name, activity=None):
    screen = app_map['screens'][name]
    missing, forbidden = [], []
    for field, results in (('require', missing), ('forbid', forbidden)):
        for index, condition in enumerate(screen.get(field, [])):
            candidates = app_map['selectors'][condition] if isinstance(condition, str) else [condition]
            node, _ = resolve_selector(root, candidates, multiple=field == 'forbid')
            if (field == 'require' and node is None) or (field == 'forbid' and node):
                results.append(condition if isinstance(condition, str) else index)
    if 'activity' in screen and re.fullmatch(screen['activity'], activity or '') is None:
        missing.append('activity')
    return {'matched': not missing and not forbidden, 'missing': missing, 'forbidden': forbidden}


def wait_screen(device, app_map, name, timeout):
    deadline = time.monotonic() + timeout / 1000
    while True:
        activity = device.app_current().get('activity') if 'activity' in app_map['screens'][name] else None
        result = screen_state(snapshot(device), app_map, name, activity)
        if result['matched']:
            return {'screen': name, **result}
        if time.monotonic() >= deadline:
            error = Refused('screen_not_found')
            error.details = {'screen': name, **result}
            raise error
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def wait_element(device, candidates, timeout):
    deadline = time.monotonic() + timeout / 1000
    while True:
        node, trace = resolve_selector(snapshot(device), candidates)
        if node is not None:
            return node, trace
        if time.monotonic() >= deadline:
            error = Refused('element_not_found')
            error.details = {'candidates': trace}
            raise error
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def tap(device, node):
    if isinstance(node, dict):
        from .ui_worker import point
        x, y = point(device, node['point'])
    else:
        bounds = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds', ''))
        if not bounds:
            raise Refused('invalid_element_bounds')
        left, top, right, bottom = map(int, bounds.groups())
        if right <= left or bottom <= top:
            raise Refused('invalid_element_bounds')
        x, y = (left + right) // 2, (top + bottom) // 2
    device.click(x, y)


def extract_list(root, listing, fields, limit):
    nodes, _ = resolve_selector(root, listing['item'], multiple=True)
    rows = []
    for node in nodes[:limit]:
        row = {}
        for name in fields:
            child, _ = resolve_selector(node, listing['fields'][name])
            row[name] = None if child is None or child.get('password') == 'true' else child.get('text') or child.get('content-desc') or None
        rows.append(row)
    return rows


def execute_map(device, name, options, native):
    app_map = options['app_map']
    timeout = options.get('timeout', 3000)
    result = {'success': True}
    if name == 'openApp':
        native(device, {'launchApp': {'appId': app_map['package'], 'stopApp': options.get('cold', False)}})
    elif name == 'openDeepLink':
        link = app_map['deep_links'][options['name']]
        native(device, {'openLink': link['url']})
        return wait_screen(device, app_map, link['screen'], timeout)
    elif name in {'waitScreen', 'assertScreen'}:
        return wait_screen(device, app_map, options['screen'], 0 if name == 'assertScreen' else timeout)
    elif name in {'tapElement', 'typeInto'}:
        node, trace = wait_element(device, app_map['selectors'][options['selector']], timeout)
        result.update(selector=options['selector'], candidates=trace)
        if name == 'tapElement':
            tap(device, node)
        else:
            # An explicit ordinal selects the exact node from this snapshot.
            # Direct accessibility setText avoids keyboard/clipboard fallbacks.
            root = snapshot(device)
            node, _ = resolve_selector(root, app_map['selectors'][options['selector']])
            if node is None:
                raise Refused('element_not_found')
            same_class = [item for item in root.iter('node') if item.get('class') == node.get('class')]
            index = next(i for i, item in enumerate(same_class) if item is node)
            value = options['text']
            if not options.get('clear', True) and node.get('password') != 'true':
                value = node.get('text', '') + value
            device(className=node.get('class'), instance=index).set_text(value, timeout=0)
            if options.get('submit'):
                device.press('enter')
    elif name == 'readList':
        listing = app_map['lists'][options['list']]
        rows, seen = [], set()
        for page in range(options.get('scroll_pages', 0) + 1):
            page_rows = extract_list(snapshot(device), listing, options.get('fields', list(listing['fields'])), options.get('limit', 20))
            for row in page_rows:
                key = tuple(row.items())
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
            if len(rows) >= options.get('limit', 20) or page == options.get('scroll_pages', 0):
                break
            device.swipe_ext('up', scale=0.6, duration=0.3)
        return {'list': options['list'], 'rows': rows[:options.get('limit', 20)]}
    elif name == 'inspectMap':
        root = snapshot(device)
        selectors, screens = {}, {}
        for key, candidates in app_map['selectors'].items():
            try:
                node, trace = resolve_selector(root, candidates)
                selectors[key] = {'found': node is not None, 'candidates': trace}
            except Refused as error:
                selectors[key] = {'error': error.code, **error.details}
        activity = device.app_current().get('activity') if any('activity' in screen for screen in app_map['screens'].values()) else None
        for key in app_map['screens']:
            try:
                screens[key] = screen_state(root, app_map, key, activity)
            except Refused as error:
                screens[key] = {'error': error.code, **error.details}
        return {'selectors': selectors, 'screens': screens}
    elif name == 'dismissPopups':
        dismissed = []
        for _ in range(options.get('max_rounds', 3)):
            root = snapshot(device)
            present = False
            for key, popup in app_map['popups'].items():
                node, _ = resolve_selector(root, app_map['selectors'][popup['detect']])
                if node is None:
                    continue
                present = True
                if 'press' in popup:
                    device.press(popup['press'])
                else:
                    node, _ = wait_element(device, app_map['selectors'][popup['dismiss']], timeout)
                    tap(device, node)
                dismissed.append(key)
                break
            if not present:
                break
        result['dismissed'] = dismissed
    if 'expect_screen' in options:
        result.update(wait_screen(device, app_map, options['expect_screen'], timeout))
    return result
