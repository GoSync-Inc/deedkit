"""Local text recognition of one screenshot. Image and text stay on the USB host."""

import io
from pathlib import Path
import subprocess
import tempfile
import time

from PIL import Image, ImageChops, ImageFilter, ImageOps

from .config import Refused

PNG = b'\x89PNG\r\n\x1a\n'
HINT = ('Text recognition needs ocr_command (absolute Tesseract path) and optional ocr_languages, '
        'for example eng+ind, in the USB host configuration.')


def lines(tsv, origin, min_confidence):
    """Group Tesseract word rows (level 5) into lines in full-screen pixels."""
    return [{key: line[key] for key in ('text', 'bounds', 'confidence')}
            for line in engine_lines(tsv, origin, min_confidence)]


def engine_lines(tsv, origin, min_confidence):
    """Lines with their words, as the engine laid them out, top to bottom."""
    groups = {}
    for row in tsv.splitlines()[1:]:
        cells = row.split('\t')
        if len(cells) != 12 or cells[0] != '5' or not cells[11].strip():
            continue
        try:
            left, top, width, height = (int(value) for value in cells[6:10])
            confidence = float(cells[10])
        except ValueError:
            continue
        if confidence >= 0:
            groups.setdefault(tuple(cells[1:5]), []).append(
                (left + origin[0], top + origin[1], left + width + origin[0], top + height + origin[1],
                 confidence, cells[11].strip()))
    found = []
    for words in groups.values():
        confidence = round(sum(word[4] for word in words) / len(words), 1)
        if confidence < min_confidence:
            continue
        left, top = min(word[0] for word in words), min(word[1] for word in words)
        right, bottom = max(word[2] for word in words), max(word[3] for word in words)
        found.append(((top, left), {'text': ' '.join(word[5] for word in words),
                                    'bounds': f'[{left},{top}][{right},{bottom}]', 'confidence': confidence,
                                    'box': (left, top, right, bottom),
                                    'words': [{'text': word[5], 'bounds': f'[{word[0]},{word[1]}][{word[2]},{word[3]}]',
                                               'confidence': round(word[4], 1)} for word in words]}))
    return [line for _, line in sorted(found, key=lambda item: item[0])]


def normalize(image):
    """Text of any colour on any background becomes dark on white, at the same size.

    Each pixel's distance from its local background: a median at quarter scale, taking the nearest of the
    neighbouring background cells, so the edges of buttons, chips and cards cancel and only strokes stay.
    Without it Tesseract's global threshold loses light text on coloured buttons (ANYM-961 corpus:
    character error rate 2.47% before, 0.41% after).
    """
    gray = image.convert('L')
    small = gray.resize((max(1, gray.width // 4), max(1, gray.height // 4)), Image.BILINEAR)
    small = small.filter(ImageFilter.MedianFilter(13))
    nearest = None
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            distance = ImageChops.difference(gray, ImageChops.offset(small, dx, dy).resize(gray.size, Image.NEAREST))
            nearest = distance if nearest is None else ImageChops.darker(nearest, distance)
    return ImageOps.autocontrast(ImageOps.invert(nearest))


def recognize(image, ocr, origin=(0, 0), min_confidence=60):
    """Engine lines of one image in full-screen pixels. Raises Refused('ocr_failed')."""
    # A private directory per call; removed on success, failure and timeout.
    with tempfile.TemporaryDirectory(prefix='deedkit-ocr-') as directory:
        path = Path(directory) / 'screen.png'
        normalize(image).save(path, format='PNG', compress_level=1)
        try:
            done = subprocess.run([ocr['command'], str(path), 'stdout', '-l', ocr['languages'], '--psm', '11', 'tsv'],
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise Refused('ocr_failed') from None
    if done.returncode:
        raise Refused('ocr_failed')
    return engine_lines(done.stdout.decode('utf-8', 'replace'), origin, min_confidence)


def read_screen_text(device, options, ocr, locate):
    if not ocr:
        error = Refused('ocr_unavailable')
        error.hint = HINT
        raise error
    started = time.monotonic()
    region = locate(options['within']) if 'within' in options else options.get('region')
    capture = device._dev.shell2(['screencap', '-p'], timeout=8, encoding=None)
    if capture.returncode or not capture.output.startswith(PNG):
        raise Refused('screenshot_unavailable')
    with Image.open(io.BytesIO(capture.output)) as screen:
        width, height = screen.size
        if region is None:
            region = {'x': 0, 'y': 0, 'w': width, 'h': height}
        elif 'within' in options:
            # An element partly outside the screen is read where it is visible.
            right, bottom = min(region['x'] + region['w'], width), min(region['y'] + region['h'], height)
            region = {'x': region['x'], 'y': region['y'], 'w': right - region['x'], 'h': bottom - region['y']}
        if region['w'] < 1 or region['h'] < 1 or region['x'] + region['w'] > width or region['y'] + region['h'] > height:
            raise Refused('region_outside_screen')
        image = screen.crop((region['x'], region['y'], region['x'] + region['w'], region['y'] + region['h']))
    found = recognize(image, ocr, (region['x'], region['y']), options.get('min_confidence', 60))
    limit = options.get('limit', 100)
    result = {'key': options.get('key', 'screen_text'), 'region': region,
              'lines': [{key: line[key] for key in ('text', 'bounds', 'confidence')} for line in found[:limit]],
              'truncated': len(found) > limit}
    if options.get('structure'):
        from xml.etree import ElementTree
        from .screen_structure import MAX_ITEMS, build
        try:
            root = ElementTree.fromstring(device.jsonrpc_call('dumpWindowHierarchy', (True, 50)))
        except Exception:  # noqa: BLE001 - read-only: the OCR layout still stands without the tree
            root = None
        blocks, lists = build(found[:limit], root, (0, 0, width, height))
        result['structure'] = {
            'blocks': blocks, 'lists': lists,
            'tree': 'unavailable' if root is None else 'merged' if any(True for _ in root.iter('node')) else 'none',
            'truncated': len(found) > limit or any(len(item['items']) >= MAX_ITEMS for item in lists)}
    return {**result,
            'ocr': {'engine': 'tesseract', 'languages': ocr['languages'], 'preprocess': 'local_contrast',
                    'elapsed_ms': round((time.monotonic() - started) * 1000, 2)}}
