"""Apple Vision text recognition on a macOS USB host (the readScreenText default where it is installed).

VNRecognizeTextRequest (accurate) runs in this process; the image and text never leave the host. Lines come back in
the shape of screen_text.engine_lines, with each word's box tightened to its ink so the ANYM-960 structure builder sees
the same geometry as with Tesseract (Vision's own boxes span the full line height and merge list rows).
"""

import importlib.util
import io
import sys
import threading

from PIL import ImageOps

LANGUAGES = {'eng': 'en-US', 'ind': 'id-ID', 'msa': 'ms-MY', 'rus': 'ru-RU', 'ukr': 'uk-UA', 'deu': 'de-DE',
             'fra': 'fr-FR', 'spa': 'es-ES', 'ita': 'it-IT', 'por': 'pt-BR', 'nld': 'nl-NL', 'pol': 'pl-PL',
             'tur': 'tr-TR', 'vie': 'vi-VT', 'tha': 'th-TH', 'chi_sim': 'zh-Hans', 'chi_tra': 'zh-Hant',
             'jpn': 'ja-JP', 'kor': 'ko-KR', 'ara': 'ar-SA'}
DEFAULT_MIN_CONFIDENCE = 50  # Vision reports 30, 50 or 100; 50 keeps short chips such as courier names
TIMEOUT_SECONDS = 20
INK = 96  # local-contrast level (0-255, inverted) above which a pixel is a stroke


def available():
    """True on macOS when the optional `vision` extra (PyObjC Vision and Quartz) is installed."""
    return sys.platform == 'darwin' and all(importlib.util.find_spec(name) for name in ('Vision', 'Quartz'))


def languages(codes):
    """Tesseract codes from the host config (eng+ind) to Vision's, in order; unknown codes are skipped."""
    return [LANGUAGES[code] for code in codes.split('+') if code in LANGUAGES] or ['en-US']


def observations(image, codes):
    """[(text, confidence 0-100, [(word, (left, top, right, bottom))])] in image pixels."""
    import Quartz
    import Vision
    from Foundation import NSData

    buffer = io.BytesIO()
    image.convert('RGB').save(buffer, format='PNG', compress_level=1)
    data = NSData.dataWithBytes_length_(buffer.getvalue(), len(buffer.getvalue()))
    cg_image = Quartz.CGImageSourceCreateImageAtIndex(Quartz.CGImageSourceCreateWithData(data, None), 0, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(languages(codes))
    request.setUsesLanguageCorrection_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, None)
    done, _ = handler.performRequests_error_([request], None)
    if not done:
        raise RuntimeError('vision request failed')
    width, height = image.size

    def pixels(rect):
        (x, y), (w, h) = rect.origin, rect.size
        return round(x * width), round((1 - y - h) * height), round((x + w) * width), round((1 - y) * height)

    found = []
    for observation in request.results() or []:
        candidate = observation.topCandidates_(1)[0]
        string, words, start = candidate.string(), [], None
        for index, char in enumerate(string + ' '):
            if not char.isspace():
                start = index if start is None else start
                continue
            if start is not None:
                box, _ = candidate.boundingBoxForRange_error_((start, index - start), None)
                if box is not None:
                    words.append((string[start:index], pixels(box.boundingBox())))
                start = None
        found.append((string, round(float(candidate.confidence()) * 100, 1), words))
    return found


def ink_box(ink, box):
    """The box shrunk to the strokes inside it, or the box itself when it holds none."""
    left, top, right, bottom = max(0, box[0]), max(0, box[1]), min(ink.width, box[2]), min(ink.height, box[3])
    if right <= left or bottom <= top:
        return box
    found = ink.crop((left, top, right, bottom)).getbbox()
    return (left + found[0], top + found[1], left + found[2], top + found[3]) if found else (left, top, right, bottom)


def engine_lines(image, codes, origin, min_confidence, normalize):
    """Vision lines like screen_text.engine_lines: text, bounds, confidence, box and words, top to bottom."""
    result, failure = [], []

    def run():
        try:
            result.append(observations(image, codes))
        except Exception as error:  # noqa: BLE001 - any framework failure is ocr_failed for the caller
            failure.append(error)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(TIMEOUT_SECONDS)
    if not result:
        raise RuntimeError('vision timed out' if worker.is_alive() else f'vision failed: {failure[:1]}')
    ink = ImageOps.invert(normalize(image)).point(lambda value: 255 if value > INK else 0)
    found = []
    for _, confidence, words in result[0]:
        if confidence < min_confidence or not words:
            continue
        boxes = [ink_box(ink, box) for _, box in words]
        shifted = [(b[0] + origin[0], b[1] + origin[1], b[2] + origin[0], b[3] + origin[1]) for b in boxes]
        left, top = min(b[0] for b in shifted), min(b[1] for b in shifted)
        right, bottom = max(b[2] for b in shifted), max(b[3] for b in shifted)
        found.append(((top, left), {
            'text': ' '.join(word for word, _ in words), 'bounds': f'[{left},{top}][{right},{bottom}]',
            'confidence': confidence, 'box': (left, top, right, bottom),
            'words': [{'text': word, 'bounds': f'[{b[0]},{b[1]}][{b[2]},{b[3]}]', 'confidence': confidence}
                      for (word, _), b in zip(words, shifted)]}))
    return [line for _, line in sorted(found, key=lambda item: item[0])]
