"""Per-card reading: the repeated cards of a scrolling container, each from the tree's own text or its own crop.

Read-only and local: the crops are cut from the OCR copy of the screenshot and never leave the worker.
"""

from .config import Refused
from .screen_structure import LIST_CLASSES, area, box, describe, intersection, normal, text as bounds_text

DEFAULT_CARDS = 12


def scrolling(node):
    return node.get('scrollable') == 'true' or (node.get('class') or '').endswith(LIST_CLASSES)


def find_cards(root, read):
    """Cards in reading order, clipped to the read box (left, top, right, bottom).

    A scrolling container with two or more visible children is a list and its children are the cards (the rule
    structure.lists uses). A list inside another list's card is part of that card, not a second list.
    Each item: {'list': number, 'node': element, 'box': clipped box}."""
    cards, inside, number = [], set(), 0
    for container in root.iter('node'):
        if container in inside or not scrolling(container):
            continue
        found = []
        for child in container:
            bounds = box(child.get('bounds')) if child.tag == 'node' and child.get('visible-to-user') != 'false' else None
            shown = intersection(bounds, read) if bounds else None
            if shown and area(shown):
                found.append({'list': number, 'node': child, 'box': shown})
        if len(found) >= 2:
            found.sort(key=lambda card: (card['box'][1], card['box'][0]))
            cards += found
            number += 1
            for card in found:
                inside.update(card['node'].iter())
    return cards


def tree_lines(node):
    """The text the tree holds for one card: the leaf-most text nodes in tree order, never a password, no repeats.

    A container's own text or description counts only when no node under it holds text."""
    lines, seen = [], set()

    def walk(element):
        held = False
        for child in element:
            if child.tag == 'node':
                held = walk(child) or held
        own = '' if element.get('password') == 'true' else ' '.join((element.get('text') or element.get('content-desc') or '').split())
        if own and not held:
            held = True
            bounds = box(element.get('bounds'))
            if bounds and normal(own) not in seen:
                seen.add(normal(own))
                lines.append({'text': own, 'bounds': bounds_text(*bounds), 'confidence': 100.0, 'source': 'tree'})
        return held
    walk(node)
    return lines


def read_cards(cards, root, image, origin, ocr, min_confidence, limit, recognize):
    """One record per card: {list, bounds, node, source: tree|ocr, lines}; a failed crop read has error: ocr_failed.

    A card whose visible part holds tree text is built from the tree and not recognized. Raises
    Refused('ocr_failed') when every card that needed recognition failed."""
    indexes = {node: index for index, node in enumerate(root.iter('node'))}
    records = []
    for card in cards:
        left, top, right, bottom = card['box']
        record = {'list': card['list'], 'bounds': bounds_text(*card['box']),
                  'node': describe({'index': indexes[card['node']], 'node': card['node']})}
        lines = [line for line in tree_lines(card['node']) if area(intersection(box(line['bounds']), card['box']))]
        if lines:
            records.append({**record, 'source': 'tree', 'lines': lines[:limit]})
            continue
        crop = image.crop((left - origin[0], top - origin[1], right - origin[0], bottom - origin[1]))
        try:
            found, _ = recognize(crop, ocr, (left, top), min_confidence)
        except Refused as error:
            if error.code != 'ocr_failed':
                raise
            records.append({**record, 'source': 'ocr', 'lines': [], 'error': 'ocr_failed'})
            continue
        records.append({**record, 'source': 'ocr',
                        'lines': [{**{key: line[key] for key in ('text', 'bounds', 'confidence')}, 'source': 'ocr'}
                                  for line in found[:limit]]})
    read = [record for record in records if record['source'] == 'ocr']
    if read and all('error' in record for record in read):
        raise Refused('ocr_failed')
    return records
