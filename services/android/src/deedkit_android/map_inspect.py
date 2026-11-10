"""Read-only map authoring aids: a compact hierarchy and suggested selectors."""

from collections import Counter
import re

FLAGS = ('clickable', 'scrollable')
TEXT = ('text', 'content-desc')


def compact(root, max_nodes):
    nodes = []

    def walk(node, depth):
        for child in node:
            if child.tag != 'node':
                continue
            item = {'index': len(nodes), 'depth': depth}
            for key in ('class', 'resource-id', 'text', 'content-desc', 'bounds'):
                if child.get(key) and not (key in TEXT and child.get('password') == 'true'):
                    item[key] = child.get(key)
            for key in FLAGS:
                if child.get(key) == 'true':
                    item[key] = True
            if child.get('password') == 'true':
                item['password'] = True
            nodes.append(item)
            walk(child, depth + 1)
    walk(root, 0)
    return {'nodes': nodes[:max_nodes], 'total': len(nodes), 'truncated': len(nodes) > max_nodes}


def readable(node, key):
    return node.get(key) if node.get(key) and node.get('password') != 'true' else None


def suggest(root):
    every = list(root.iter('node'))
    counts = {key: Counter(readable(node, key) for node in every if readable(node, key))
              for key in ('resource-id', 'text', 'content-desc')}
    selectors = []
    for node in every:
        for key, field in (('resource-id', 'resourceId'), ('text', 'textExact'), ('content-desc', 'description')):
            value = readable(node, key)
            if value and counts[key][value] == 1:
                selectors.append({'selector': {field: value}, 'bounds': node.get('bounds', ''),
                                  'clickable': node.get('clickable') == 'true'})
                break
        if len(selectors) == 50:
            break
    lists = []
    for parent in root.iter():
        groups = {}
        for child in parent:
            if child.tag == 'node':
                key = ('resourceId', child.get('resource-id')) if child.get('resource-id') else ('className', child.get('class', ''))
                groups.setdefault(key, []).append(child)
        for (field, value), items in groups.items():
            if len(items) < 2 or not value:
                continue
            present = Counter(rid for item in items for rid in {d.get('resource-id') for d in item.iter('node') if d is not item and d.get('resource-id')})
            fields = {}
            for rid, seen in present.items():
                if seen * 2 >= len(items):
                    name = re.sub(r'[^A-Za-z0-9_.-]', '_', rid.split('/')[-1])[:72] or 'field'
                    while name in fields:
                        name += '_'
                    fields[name] = {'resourceId': rid}
            has_text = any(readable(d, key) for item in items for d in item.iter('node') for key in TEXT)
            entry = {'item': {field: value}, 'count': len(items), 'bounds': items[0].get('bounds', ''),
                     'fields': fields, 'has_text': has_text}
            if not has_text:
                entry['hint'] = 'Items expose no text. Try map hierarchy {compressed: false}, then readScreenText within an item.'
            lists.append(entry)
    lists.sort(key=lambda entry: ('resourceId' not in entry['item'], not entry['has_text'], -entry['count']))
    return {'selectors': selectors, 'lists': lists[:50]}
