"""Turn OCR lines into the screen's layout: blocks, rows, lines and words, merged with the tree.

Geometry and the OCR engine's own words only; no model reconstructs the screen.
"""

import re
from statistics import median

TOLERANCE = 4
MAX_ITEMS = 50


def box(bounds):
    match = re.fullmatch(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]', bounds or '')
    return tuple(int(value) for value in match.groups()) if match else None


def text(left, top, right, bottom):
    return f'[{left},{top}][{right},{bottom}]'


def union(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def contains(outer, inner):
    return (outer[0] - TOLERANCE <= inner[0] and outer[1] - TOLERANCE <= inner[1]
            and outer[2] + TOLERANCE >= inner[2] and outer[3] + TOLERANCE >= inner[3])


def area(value):
    return max(0, value[2] - value[0]) * max(0, value[3] - value[1])


def normal(value):
    return ' '.join((value or '').split()).casefold()


def tree_nodes(root):
    nodes = []
    for index, node in enumerate(root.iter('node') if root is not None else []):
        bounds = box(node.get('bounds'))
        if bounds and area(bounds):
            secret = node.get('password') == 'true'
            nodes.append({'index': index, 'box': bounds, 'node': node,
                          'text': '' if secret else node.get('text') or node.get('content-desc') or '',
                          'container': node.get('clickable') == 'true' or bool(node.get('resource-id'))})
    return nodes


def smallest(nodes, target, only=None):
    found = [node for node in nodes if contains(node['box'], target) and (only is None or only(node))]
    return min(found, key=lambda node: area(node['box'])) if found else None


def describe(node):
    element = node['node']
    return {'index': node['index'], 'class': element.get('class', ''),
            **({'resource-id': element.get('resource-id')} if element.get('resource-id') else {}),
            'bounds': element.get('bounds')}


def rows_of(lines):
    """Lines whose vertical extents overlap by half of the smaller height share a row."""
    rows = []
    for line in sorted(lines, key=lambda item: (item['box'][1], item['box'][0])):
        top, bottom = line['box'][1], line['box'][3]
        if rows:
            row_top, row_bottom = rows[-1]['span']
            overlap = min(bottom, row_bottom) - max(top, row_top)
            if overlap * 2 >= min(bottom - top, row_bottom - row_top):
                rows[-1]['lines'].append(line)
                rows[-1]['span'] = (min(top, row_top), max(bottom, row_bottom))
                continue
        rows.append({'span': (top, bottom), 'lines': [line]})
    return rows


def build(lines, root, screen):
    """lines: OCR lines with box, words and confidence, top to bottom. root: hierarchy or None."""
    nodes = [node for node in tree_nodes(root) if area(node['box']) < 0.9 * area(screen)]
    texts = [node for node in nodes if node['text']]
    for line in lines:
        same = next((node for node in texts if normal(node['text']) == normal(line['text'])
                     and area(intersection(node['box'], line['box'])) * 2 >= area(line['box'])), None)
        line['source'] = 'tree' if same else 'ocr'
        if same:
            line['node'] = same['index']
    # A clickable or identified tree container groups its lines; elsewhere a
    # vertical gap above 1.5 line heights starts a new block.
    groups, loose = {}, []
    for line in lines:
        container = smallest(nodes, line['box'], lambda node: node['container'])
        if container is None:
            loose.append(line)
        else:
            groups.setdefault(container['index'], []).append(line)
    blocks = list(groups.values())
    gap = 1.5 * median([line['box'][3] - line['box'][1] for line in lines]) if lines else 0
    current = []
    for line in sorted(loose, key=lambda item: (item['box'][1], item['box'][0])):
        if current and line['box'][1] - max(item['box'][3] for item in current) > gap:
            blocks.append(current)
            current = []
        current.append(line)
    if current:
        blocks.append(current)
    output = []
    for members in sorted(blocks, key=lambda group: (union([line['box'] for line in group])[1],
                                                     union([line['box'] for line in group])[0])):
        bounds = union([line['box'] for line in members])
        ordered = []
        for number, row in enumerate(rows_of(members)):
            for line in sorted(row['lines'], key=lambda item: item['box'][0]):
                ordered.append({key: value for key, value in line.items() if key != 'box'} | {'row': number})
        block = {'bounds': text(*bounds), 'box': bounds, 'rows': len(rows_of(members)),
                 'confidence': round(sum(line['confidence'] for line in members) / len(members), 1), 'lines': ordered}
        node = smallest(nodes, bounds)
        block['element'] = None if node is None else node['node']
        if node is not None:
            block['node'] = describe(node)
        output.append(block)
    lists = card_lists(output, root)
    taken = {index for item in lists for card in item['cards'] for index in card}
    lists += [item for item in repeated(output) if not taken.intersection(item['items'])]
    lists.sort(key=lambda item: min(item['items']))
    for block in output:
        del block['box'], block['rows'], block['element']
    return output, lists


LIST_CLASSES = ('RecyclerView', 'ListView', 'GridView', 'ViewPager', 'ViewPager2')


def card_lists(blocks, root):
    """Blocks in two or more cards (children) of one scrolling container form a list, one item per card."""
    if root is None:
        return []
    parents = {child: parent for parent in root.iter() for child in parent}
    found = {}
    for index, block in enumerate(blocks):
        element = block['element']
        for _ in range(16):
            parent = parents.get(element) if element is not None else None
            if parent is None or parent.tag != 'node':
                break
            if parent.get('scrollable') == 'true' or (parent.get('class') or '').endswith(LIST_CLASSES):
                found.setdefault(parent, {}).setdefault(element, []).append(index)
                break
            element = parent
    lists = []
    for cards in found.values():
        if len(cards) >= 2:
            # Reading order of the cards themselves (rows of a grid, then columns).
            order = sorted(cards, key=lambda card: (box(card.get('bounds')) or (0, 0))[1::-1])
            groups = [cards[card] for card in order][:MAX_ITEMS]
            lists.append({'bounds': text(*union([blocks[i]['box'] for group in groups for i in group])),
                          'items': [group[0] for group in groups], 'cards': groups})
    return lists


def intersection(a, b):
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def repeated(blocks):
    """Consecutive blocks of the same shape (rows, left edge, height) form a list."""
    def same(a, b):
        height_a, height_b = a['box'][3] - a['box'][1], b['box'][3] - b['box'][1]
        return (a['rows'] == b['rows'] and abs(a['box'][0] - b['box'][0]) <= 24
                and abs(height_a - height_b) <= 0.25 * max(height_a, height_b))
    lists, run = [], [0] if blocks else []
    for index in range(1, len(blocks) + 1):
        if index < len(blocks) and same(blocks[run[0]], blocks[index]):
            run.append(index)
            continue
        if len(run) >= 2:
            lists.append({'bounds': text(*union([blocks[i]['box'] for i in run])), 'items': run[:MAX_ITEMS]})
        run = [index]
    return lists
