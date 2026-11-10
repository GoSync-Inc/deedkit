"""Local text recognition of one screenshot. Image and text stay on the USB host."""

import io
from pathlib import Path
import subprocess
import tempfile
import time

from PIL import Image

from .config import Refused

PNG = b'\x89PNG\r\n\x1a\n'
HINT = ('Text recognition needs ocr_command (absolute Tesseract path) and optional ocr_languages, '
        'for example eng+ind, in the USB host configuration.')


def lines(tsv, origin, min_confidence):
    """Group Tesseract word rows (level 5) into lines in full-screen pixels."""
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
                (left, top, left + width, top + height, confidence, cells[11].strip()))
    found = []
    for words in groups.values():
        confidence = round(sum(word[4] for word in words) / len(words), 1)
        if confidence < min_confidence:
            continue
        left, top = min(word[0] for word in words) + origin[0], min(word[1] for word in words) + origin[1]
        right, bottom = max(word[2] for word in words) + origin[0], max(word[3] for word in words) + origin[1]
        found.append(((top, left), {'text': ' '.join(word[5] for word in words),
                                    'bounds': f'[{left},{top}][{right},{bottom}]', 'confidence': confidence}))
    return [line for _, line in sorted(found, key=lambda item: item[0])]


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
    # A private directory per call; removed on success, failure and timeout.
    with tempfile.TemporaryDirectory(prefix='deedkit-ocr-') as directory:
        path = Path(directory) / 'screen.png'
        image.save(path, format='PNG', compress_level=1)
        try:
            done = subprocess.run([ocr['command'], str(path), 'stdout', '-l', ocr['languages'], '--psm', '11', 'tsv'],
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise Refused('ocr_failed') from None
    if done.returncode:
        raise Refused('ocr_failed')
    found = lines(done.stdout.decode('utf-8', 'replace'), (region['x'], region['y']), options.get('min_confidence', 60))
    limit = options.get('limit', 100)
    return {'key': options.get('key', 'screen_text'), 'region': region, 'lines': found[:limit],
            'truncated': len(found) > limit,
            'ocr': {'engine': 'tesseract', 'languages': ocr['languages'],
                    'elapsed_ms': round((time.monotonic() - started) * 1000, 2)}}
