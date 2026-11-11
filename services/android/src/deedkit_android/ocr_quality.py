"""Recognition quality on a labelled screen set: character error rate, list items and time.

A set is a directory of <name>.png screens, each with <name>.json ground truth:
{"languages": "eng+ind", "lines": [{"text": ..., "bounds": [left, top, right, bottom]}], "lists": [item counts]}.
The summary holds numbers and screen names only, never recognised or expected text.
"""

import json
from pathlib import Path
from statistics import median
import time

from PIL import Image

from .screen_structure import build, rows_of
from .screen_text import recognize


def distance(expected, found):
    """Levenshtein distance in characters."""
    previous = list(range(len(found) + 1))
    for row, left in enumerate(expected, 1):
        current = [row]
        for column, right in enumerate(found, 1):
            current.append(min(previous[column] + 1, current[column - 1] + 1, previous[column - 1] + (left != right)))
        previous = current
    return previous[-1]


def reading_text(lines):
    """Lines in reading order (rows top to bottom, left to right in a row), spaces collapsed, one per line."""
    ordered = []
    for row in rows_of(lines):
        ordered.extend(' '.join(line['text'].split()) for line in sorted(row['lines'], key=lambda line: line['box'][0]))
    return '\n'.join(ordered)


def score(truth, found, size):
    """One screen: (edit distance, expected characters, list item counts equal, has lists)."""
    expected = reading_text([{'text': line['text'], 'box': tuple(line['bounds'])} for line in truth['lines']])
    _, lists = build(found, None, (0, 0, *size))
    return (distance(expected, reading_text(found)), len(expected),
            [len(item['items']) for item in lists] == truth['lists'], bool(truth['lists']))


def evaluate(directory, command, languages=None, recognizer=recognize):
    """Recognise every labelled screen in directory and return the summary numbers."""
    edits = characters = lists_exact = list_screens = 0
    seconds, misses = [], []
    for path in sorted(Path(directory).glob('*.png')):
        truth = json.loads(path.with_suffix('.json').read_text())
        with Image.open(path) as image:
            image.load()
        started = time.monotonic()
        found = recognizer(image, {'command': command, 'languages': languages or truth['languages']})
        seconds.append(time.monotonic() - started)
        edit, length, exact, has_lists = score(truth, found, image.size)
        edits, characters = edits + edit, characters + length
        lists_exact, list_screens = lists_exact + (exact and has_lists), list_screens + has_lists
        if (has_lists and not exact) or edit > 0.05 * length:
            misses.append(path.stem)
    return {'screens': len(seconds), 'cer_percent': round(100 * edits / max(characters, 1), 2),
            'list_screens': list_screens, 'list_items_exact': lists_exact,
            'list_exact_percent': round(100 * lists_exact / list_screens, 1) if list_screens else None,
            'median_seconds': round(median(seconds), 3) if seconds else None, 'misses': misses}
