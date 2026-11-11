"""Local text recognition of one screenshot. Image and text stay on the USB host."""

import io
from pathlib import Path
import subprocess
import tempfile
import time
from xml.etree import ElementTree

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

from .config import Refused
from .screen_structure import area, box as parse_box, contains, intersection

PNG = b'\x89PNG\r\n\x1a\n'
IMAGE_CLASSES = ('ImageView', 'TextureView')
LARGE_IMAGE = 0.6  # an image over this share of the read region is a viewer or the target, not a photo to blank
HINT = ('Text recognition needs ocr_command (absolute Tesseract path, also the fallback when Apple Vision is the '
        'engine) and optional ocr_languages, for example eng+ind, in the USB host configuration.')


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


def tesseract_lines(image, ocr, origin=(0, 0), min_confidence=60):
    """Tesseract engine lines of one image in full-screen pixels. Raises Refused('ocr_failed')."""
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


def recognize_with(image, ocr, origin=(0, 0), min_confidence=None):
    """(lines, report) with the host's engine: Apple Vision when ocr['engine'] is vision, else Tesseract.

    A Vision failure falls back to Tesseract when the host has ocr_command; the report names the engine that read."""
    if ocr.get('engine') == 'vision':
        from . import screen_vision
        try:
            lines = screen_vision.engine_lines(
                image, ocr['languages'], origin,
                screen_vision.DEFAULT_MIN_CONFIDENCE if min_confidence is None else min_confidence, normalize)
            return lines, {'engine': 'apple-vision', 'preprocess': 'none', 'bounds': 'ink'}
        except Exception:  # noqa: BLE001 - Vision is replaceable; Tesseract below or ocr_failed
            if not ocr.get('command'):
                raise Refused('ocr_failed') from None
    lines = tesseract_lines(image, ocr, origin, 60 if min_confidence is None else min_confidence)
    return lines, {'engine': 'tesseract', 'preprocess': 'local_contrast'}


def recognize(image, ocr, origin=(0, 0), min_confidence=None):
    """Engine lines of one image in full-screen pixels. Raises Refused('ocr_failed')."""
    return recognize_with(image, ocr, origin, min_confidence)[0]


def dump_tree(device):
    """The hierarchy as an element tree, or None: a read-only aid never fails the read."""
    try:
        return ElementTree.fromstring(device.jsonrpc_call('dumpWindowHierarchy', (True, 50)))
    except Exception:  # noqa: BLE001 - secure screen, timeout or a broken dump; the OCR layout still stands
        return None


def is_image(node):
    return (node.get('class') or '').endswith(IMAGE_CLASSES)


def mask_plan(root, region):
    """(image boxes to paint, text boxes to keep, large images skipped), in full-screen pixels inside the read region."""
    read = (region['x'], region['y'], region['x'] + region['w'], region['y'] + region['h'])
    nodes = []
    for node in root.iter('node'):
        found = parse_box(node.get('bounds'))
        if found and area(found):
            nodes.append((node, found))
    images, skipped = [], 0
    for node, found in nodes:
        if is_image(node):
            shown = intersection(found, read)
            if area(shown) > LARGE_IMAGE * area(read):
                skipped += 1
            elif area(shown):
                images.append(shown)
    # Text the app draws over a photo (a badge, a price chip) is a node with its own text inside the photo's bounds.
    keep = [found for node, found in nodes
            if node.get('text') and node.get('password') != 'true' and not is_image(node)
            and any(contains(shown, found) and area(found) * 2 <= area(shown) for shown in images)]
    return images, keep, skipped


def paint(image, origin, boxes, keep):
    """Fill each image box with its own mean colour (so no edge appears), then restore the kept text boxes."""
    if not boxes:
        return
    original = image.copy()
    local = lambda found: (found[0] - origin[0], found[1] - origin[1], found[2] - origin[0], found[3] - origin[1])  # noqa: E731
    for found in map(local, boxes):
        colour = tuple(round(value) for value in ImageStat.Stat(image.crop(found)).mean)
        image.paste(colour[0] if len(colour) == 1 else colour, found)
    for found in map(local, keep):
        image.paste(original.crop(found), found)


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
    mask = options.get('mask_images', True)
    # One dump, right after the capture, serves the masking and the structure.
    root = dump_tree(device) if mask or options.get('structure') else None
    masked = None
    if mask:
        # Only this OCR copy is painted; the capture and anything android_observe returns stay as they were.
        images, keep, skipped = mask_plan(root, region) if root is not None else ([], [], 0)
        paint(image, (region['x'], region['y']), images, keep)
        masked = {'regions': len(images), 'skipped_large': skipped, 'tree': 'ok' if root is not None else 'unavailable'}
    found, engine = recognize_with(image, ocr, (region['x'], region['y']), options.get('min_confidence'))
    limit = options.get('limit', 100)
    result = {'key': options.get('key', 'screen_text'), 'region': region,
              'lines': [{key: line[key] for key in ('text', 'bounds', 'confidence')} for line in found[:limit]],
              'truncated': len(found) > limit}
    if options.get('structure'):
        from .screen_structure import MAX_ITEMS, build
        blocks, lists = build(found[:limit], root, (0, 0, width, height))
        result['structure'] = {
            'blocks': blocks, 'lists': lists,
            'tree': 'unavailable' if root is None else 'merged' if any(True for _ in root.iter('node')) else 'none',
            'truncated': len(found) > limit or any(len(item['items']) >= MAX_ITEMS for item in lists)}
    return {**result,
            'ocr': {**engine, 'languages': ocr['languages'], 'masked': masked,
                    'elapsed_ms': round((time.monotonic() - started) * 1000, 2)}}
