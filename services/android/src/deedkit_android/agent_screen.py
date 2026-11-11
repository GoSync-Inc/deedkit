"""What the local agent reads of a screen: a compact text of the visible elements (ANYM-971 criteria 3 and 6)."""

import re
from xml.etree import ElementTree

BOUNDS = re.compile(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]')
FLAGS = ('clickable', 'scrollable', 'checked', 'focused', 'selected')
FIELDS = ('EditText', 'AutoCompleteTextView')


def clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + '…'


def cap_bytes(text, limit):
    """At most `limit` bytes of UTF-8, cut on a character boundary."""
    encoded = text.encode()
    return text if len(encoded) <= limit else encoded[:limit].decode(errors='ignore')


def compact(xml, limit=12000):
    """One line per element that has text, a description, an id of its own or an action: `[n] Class "text" (desc) id
    flags @x,y`. The center is where a tap lands. A password field shows no text; the host's masking has already run."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return 'screen unreadable'
    lines, shown, total = [], 0, 0
    package = None
    for node in root.iter('node'):
        package = package or node.get('package')
        text, description = node.get('text') or '', node.get('content-desc') or ''
        identifier = (node.get('resource-id') or '').split('/')[-1]
        flags = [flag for flag in FLAGS if node.get(flag) == 'true']
        field = (node.get('class') or '').endswith(FIELDS)
        if not (text or description or flags or field or identifier and node.get('clickable') == 'true'):
            continue
        total += 1
        match = BOUNDS.fullmatch(node.get('bounds') or '')
        where = ''
        if match:
            left, top, right, bottom = map(int, match.groups())
            where = f'@{(left + right) // 2},{(top + bottom) // 2} {right - left}x{bottom - top}'
        parts = [f'[{total}]', (node.get('class') or 'View').rsplit('.', 1)[-1]]
        if text:
            parts.append(f'"{clip(text, 160)}"')
        if description and description != text:
            parts.append(f'(desc {clip(description, 100)})')
        if identifier:
            parts.append('id=' + clip(identifier, 48))
        if node.get('password') == 'true':
            parts.append('password')
        parts += flags + ([where] if where else [])
        line = ' '.join(parts)
        if sum(map(len, lines)) + len(lines) + len(line) > limit:
            continue
        lines.append(line)
        shown += 1
    header = f'package={package or "unknown"} elements={total}' + (f' shown={shown}' if shown < total else '')
    return header + '\n' + '\n'.join(lines)


def render_lines(result, limit=80):
    """Recognized text lines (and the lists of a structure read) as plain text, with the center of each line."""
    rows = []
    for line in (result.get('lines') or [])[:limit]:
        bounds = line.get('bounds') or {}
        center = f"@{(bounds.get('left', 0) + bounds.get('right', 0)) // 2},{(bounds.get('top', 0) + bounds.get('bottom', 0)) // 2}"
        rows.append(f'"{clip(str(line.get("text", "")), 160)}" {center}')
    structure = result.get('structure') or {}
    blocks = structure.get('blocks') or []
    for number, group in enumerate(structure.get('lists') or [], 1):
        items = [' / '.join(str(row.get('text', '')) for row in (blocks[index].get('lines') or [])[:3])
                 for index in group.get('items', []) if isinstance(index, int) and index < len(blocks)]
        rows.append(f'list {number}: ' + ' | '.join(clip(item, 120) for item in items[:20]))
    return '\n'.join(rows) or 'no text recognized'
