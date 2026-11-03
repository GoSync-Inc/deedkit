"""Bounded local text extraction. Untrusted originals never become commands.

The subprocess has resource limits and a scrubbed environment. This is not an
OS/network sandbox: production must also use a restricted worker container.
Only this module and the standard library are imported in the parser child.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import io
import json
import math
import os
import posixpath
from pathlib import Path
import re
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from xml.etree import ElementTree

MAX_INPUT_BYTES = 32 * 1024**2
MAX_TEXT_CHARS = 1_000_000
MAX_ARCHIVE_BYTES = 32 * 1024**2
MAX_ARCHIVE_ENTRIES = 512
MAX_XML_BYTES = 8 * 1024**2
MAX_IMAGE_PIXELS = 20_000_000
EXTRACTION_TIMEOUT = 40
MAX_RESULT_BYTES = MAX_TEXT_CHARS * 12 + 4 * 1024**2
OCR_LANGUAGES = 'eng+rus'
IMAGE_OCR_EXTRACTOR = 'tesseract-eng-rus-v1'
# ANYM-669: mean word confidence (0-100) below which image OCR text is probably noise. The constant sits between the
# clean fixtures (>= 80) and the noise/rotated ones (<= 50) of tests/test_ocr_quality.py.
OCR_LOW_CONFIDENCE = 60
MAX_TSV_BYTES = 16 * 1024**2
TSV_COLUMNS = ['level', 'page_num', 'block_num', 'par_num', 'line_num', 'word_num', 'left', 'top', 'width', 'height',
               'conf', 'text']
PROCESSOR_VERSION = 'bounded-v3'
# ANYM-669: image OCR rows that carry the word counters. A release from before the counters validates and reuses only
# 'bounded-v3' rows, so after a rollback it never meets a row whose metadata it would reject; this release reuses both.
MEASURED_PROCESSOR_VERSION = 'bounded-v3+ocr1'
MAX_PDF_PAGES = 100
MAX_OCR_PAGES = 5
MAX_CELL_SPANS = 20000


@dataclass(frozen=True)
class Extraction:
    state: str
    text: str = ''
    extractor: str | None = None
    truncated: bool = False
    error_code: str | None = None
    metadata: dict = field(default_factory=dict)


def valid_metadata(value):
    if not isinstance(value, dict) or set(value) - {
            'pages_total', 'pages_processed', 'ocr_pages', 'ocr_attempts', 'ocr_pages_limit', 'pdf_page_limit', 'page_spans',
            'text_char_limit', 'sheet_spans', 'cell_spans', 'sheets_total', 'sheets_processed', 'provenance_truncated',
            'ocr_words', 'ocr_confidence'}:
        return False
    for key, item in value.items():
        if key == 'page_spans':
            if not isinstance(item, list) or len(item) > MAX_PDF_PAGES:
                return False
            for span in item:
                if (not isinstance(span, dict) or set(span) != {'page', 'start', 'end', 'method'}
                        or any(type(span[k]) is not int for k in ('page', 'start', 'end'))
                        or not 1 <= span['page'] <= MAX_PDF_PAGES
                        or not 0 <= span['start'] <= span['end'] <= MAX_TEXT_CHARS
                        or span['method'] not in ('text', 'ocr', 'unavailable')):
                    return False
        elif key in ('sheet_spans', 'cell_spans'):
            if not isinstance(item, list) or len(item) > (MAX_ARCHIVE_ENTRIES if key == 'sheet_spans' else MAX_CELL_SPANS):
                return False
            for span in item:
                required = {'sheet_index', 'start', 'end'}
                required |= {'part', 'sheet_name'} if key == 'sheet_spans' else {'row', 'cell', 'coordinates_source'}
                if (not isinstance(span, dict) or set(span) != required
                        or any(type(span[k]) is not int for k in ('sheet_index', 'start', 'end'))
                        or not 1 <= span['sheet_index'] <= MAX_ARCHIVE_ENTRIES
                        or not 0 <= span['start'] <= span['end'] <= MAX_TEXT_CHARS
                        or (span.get('sheet_name') is not None and (not isinstance(span['sheet_name'], str)
                            or not 1 <= len(span['sheet_name']) <= 128 or any(ord(c) < 32 for c in span['sheet_name'])))):
                    return False
                if key == 'sheet_spans':
                    if (not isinstance(span['part'], str) or len(span['part']) > 512
                            or not span['part'].startswith('xl/worksheets/') or '..' in span['part'].split('/')):
                        return False
                elif (type(span['row']) is not int or not 1 <= span['row'] <= 1048576
                        or not isinstance(span['cell'], str) or not re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]{0,6}', span['cell'])
                        or span['coordinates_source'] not in ('explicit', 'position')):
                    return False
        elif key == 'provenance_truncated':
            if type(item) is not bool:
                return False
        elif key == 'ocr_confidence':
            if type(item) is not int or not 0 <= item <= 100:
                return False
        elif type(item) is not int or not 0 <= item <= MAX_TEXT_CHARS:
            return False
    return True


def ocr_quality(extractor, metadata):
    """ANYM-669, derived at read time and never stored: `low` or `ok` from the image OCR's mean word confidence,
    `unmeasured` for an image OCR row without the count (extracted before this field, or an unusable TSV), None for
    text that did not come from image OCR and for a photo with no recognized words. `ok` means the engine was
    confident, not that the text is correct."""
    if extractor != IMAGE_OCR_EXTRACTOR:
        return None
    metadata = metadata if isinstance(metadata, dict) else {}
    words = metadata.get('ocr_words')
    if type(words) is not int:
        return 'unmeasured'
    if words == 0:
        return None
    confidence = metadata.get('ocr_confidence')
    if type(confidence) is not int:
        return 'unmeasured'
    return 'low' if confidence < OCR_LOW_CONFIDENCE else 'ok'


def _ocr_measure(path):
    """The word count and mean word confidence of Tesseract's TSV, or {} when it is missing, empty or unparseable:
    a measurement never fails or changes the extraction. A word is a row with confidence >= 0 and a non-empty text."""
    try:
        if not path.is_file() or path.stat().st_size > MAX_TSV_BYTES:
            return {}
        lines = path.read_bytes().decode('utf-8').split('\n')
    except (OSError, UnicodeDecodeError):
        return {}
    if lines[0].rstrip('\r').split('\t') != TSV_COLUMNS:
        return {}
    words = 0
    total = 0.0
    for line in lines[1:]:
        line = line.rstrip('\r')
        if not line:
            continue
        cells = line.split('\t', len(TSV_COLUMNS) - 1)
        # Every row is complete (an empty text column keeps its tab); a truncated row discards the measurement.
        if len(cells) != len(TSV_COLUMNS):
            return {}
        try:
            confidence = float(cells[10])
        except ValueError:
            return {}
        # Tesseract writes -1 for rows that are not words and 0-100 for words; anything else is not its TSV.
        if not math.isfinite(confidence) or not -1 <= confidence <= 100:
            return {}
        if confidence >= 0 and cells[11].strip():
            words += 1
            total += confidence
    measured = {'ocr_words': min(words, MAX_TEXT_CHARS)}
    if words:
        measured['ocr_confidence'] = max(0, min(100, round(total / words)))
    return measured


def extraction_profile(mime, filename):
    """Conservative, context-free cache key for classification inputs."""
    suffix = Path(filename).suffix.lower()
    if suffix == '.docx' or mime == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
        return 'docx'
    if suffix == '.xlsx' or mime == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':
        return 'xlsx'
    if (mime.startswith('text/') or suffix in ('.txt', '.md', '.csv', '.json', '.log', '.xml', '.html')
            or mime in ('application/json', 'application/xml')):
        return 'text'
    # PDF/PNG/JPEG detection uses bytes, not MIME or filename. Unsupported
    # binary formats may share this profile but terminal failures aren't cached.
    return 'magic'


class Rejected(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class TextSink:
    def __init__(self, limit=MAX_TEXT_CHARS):
        if type(limit) is not int or not 1000 <= limit <= MAX_TEXT_CHARS:
            raise Rejected('invalid_text_limit')
        self.limit = limit
        self.parts = []
        self.length = 0
        self.truncated = False

    def add(self, value):
        if not value:
            return
        # PostgreSQL text cannot contain NUL. Keep newlines/tabs but discard
        # control characters; the raw immutable original remains unchanged.
        value = ''.join(c for c in value if c in '\n\r\t' or (ord(c) >= 32 and ord(c) != 127))
        available = self.limit - self.length
        self.truncated |= len(value) > available
        value = value[:available]
        self.parts.append(value)
        self.length += len(value)

    def result(self, extractor):
        return Extraction('ready', ''.join(self.parts), extractor, self.truncated,
                          metadata={'text_char_limit': self.limit})


def _read_original(path, digest, expected_bytes):
    if (not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)
            or type(expected_bytes) is not int or not 0 <= expected_bytes <= MAX_INPUT_BYTES):
        raise Rejected('input_size_limit')
    if Path(path).name != digest:
        raise Rejected('invalid_original')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Rejected('original_unavailable') from None
    with os.fdopen(fd, 'rb') as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_bytes:
            raise Rejected('original_integrity')
        data = file.read(MAX_INPUT_BYTES + 1)
    if len(data) != expected_bytes or hashlib.sha256(data).hexdigest() != digest:
        raise Rejected('original_integrity')
    return data


def _xml(raw):
    if len(raw) > MAX_XML_BYTES:
        raise Rejected('archive_size_limit')
    # OOXML parts use XML 1.0. No DTD/entities (including expansion bombs).
    if b'\x00' in raw or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise Rejected('unsafe_xml')
    try:
        return ElementTree.fromstring(raw)
    except (ElementTree.ParseError, ValueError):
        raise Rejected('invalid_document') from None


def _archive(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
        names = [e.filename for e in entries]
        if (len(entries) > MAX_ARCHIVE_ENTRIES or len(set(names)) != len(names)
                or sum(e.file_size for e in entries) > MAX_ARCHIVE_BYTES):
            raise Rejected('archive_size_limit')
        for entry in entries:
            # Nothing is extracted to disk; these checks also reject ambiguous
            # paths and encrypted/polyglot archives rather than guessing.
            if (entry.filename.startswith('/') or '\\' in entry.filename
                    or '..' in entry.filename.split('/') or entry.flag_bits & 1
                    or entry.file_size > MAX_XML_BYTES
                    or entry.file_size > max(entry.compress_size, 1) * 200):
                raise Rejected('unsafe_archive')
        return archive
    except (zipfile.BadZipFile, NotImplementedError, ValueError):
        raise Rejected('invalid_document') from None


def _workbook_sheets(archive, names):
    """Resolve only in-archive worksheet relationships; never fetch external links."""
    if 'xl/workbook.xml' not in names:
        # Old/minimal exports may omit workbook metadata. Never invent a display name.
        return [(None, n) for n in sorted(names) if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n)]
    workbook = _xml(archive.read('xl/workbook.xml'))
    relationships = _xml(archive.read('xl/_rels/workbook.xml.rels'))
    targets = {}
    for relation in relationships:
        if relation.tag.rsplit('}', 1)[-1] != 'Relationship':
            continue
        key = relation.get('Id')
        if not key or key in targets:
            raise Rejected('invalid_document')
        target = relation.get('Target', '')
        if relation.get('TargetMode', 'Internal') != 'Internal' or not relation.get('Type', '').endswith('/worksheet'):
            continue
        if not target or '\\' in target or ':' in target or '?' in target or '#' in target:
            raise Rejected('invalid_document')
        part = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
        if not part.startswith('xl/worksheets/') or part not in names:
            raise Rejected('invalid_document')
        targets[key] = part
    sheets = []
    seen = set()
    for element in workbook.iter():
        if element.tag.rsplit('}', 1)[-1] != 'sheet':
            continue
        name = element.get('name')
        relationship = next((v for k, v in element.attrib.items() if k.rsplit('}', 1)[-1] == 'id'), None)
        if (not name or len(name) > 128 or any(ord(c) < 32 for c in name)
                or relationship not in targets or targets[relationship] in seen):
            raise Rejected('invalid_document')
        sheets.append((name, targets[relationship])); seen.add(targets[relationship])
    if len(sheets) > MAX_ARCHIVE_ENTRIES:
        raise Rejected('archive_size_limit')
    return sheets


def _column_name(number):
    result = ''
    while number:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result


def _cell_position(cell, row_number, next_column):
    reference = cell.get('r')
    if reference is None:
        if next_column > 16384:
            raise Rejected('invalid_document')
        return _column_name(next_column) + str(row_number), next_column, 'position'
    match = re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]{0,6})', reference)
    if not match or int(match[2]) != row_number:
        raise Rejected('invalid_document')
    column = 0
    for char in match[1]:
        column = column * 26 + ord(char) - 64
    if not 1 <= column <= 16384:
        raise Rejected('invalid_document')
    return reference, column, 'explicit'


def _office(data, kind, text_char_limit=MAX_TEXT_CHARS):
    sink = TextSink(text_char_limit)
    metadata = {'text_char_limit': text_char_limit}
    try:
        with _archive(data) as archive:
            names = archive.namelist()
            if kind == 'docx':
                if 'word/document.xml' not in names:
                    raise Rejected('invalid_document')
                # Relationships, external links, macros and embedded objects
                # are never followed. Include headers/footers/footnotes text.
                parts = ['word/document.xml'] + sorted(n for n in names if re.fullmatch(
                    r'word/(header\d+|footer\d+|footnotes|endnotes)\.xml', n))
                for part in parts:
                    root = _xml(archive.read(part))
                    for element in root.iter():
                        tag = element.tag.rsplit('}', 1)[-1]
                        if tag == 't':
                            sink.add(element.text or '')
                        elif tag in ('br', 'p'):
                            sink.add('\n')
                        elif tag == 'tab':
                            sink.add('\t')
                    sink.add('\n')
            else:
                worksheets = _workbook_sheets(archive, names)
                if not worksheets:
                    raise Rejected('invalid_document')
                metadata.update({'sheets_total': len(worksheets), 'sheets_processed': 0,
                    'sheet_spans': [], 'cell_spans': [], 'provenance_truncated': False})
                shared = []
                if 'xl/sharedStrings.xml' in names:
                    root = _xml(archive.read('xl/sharedStrings.xml'))
                    for entry in root:
                        shared.append(''.join(x.text or '' for x in entry.iter()
                                              if x.tag.rsplit('}', 1)[-1] == 't'))
                for sheet_index, (sheet_name, worksheet) in enumerate(worksheets, 1):
                    sheet_start = sink.length
                    sink.add('\n[' + (sheet_name or 'unnamed sheet ' + str(sheet_index)) + ']\n')
                    root = _xml(archive.read(worksheet))
                    previous_row = 0
                    for row in root.iter():
                        if row.tag.rsplit('}', 1)[-1] != 'row':
                            continue
                        raw_number = row.get('r', str(previous_row + 1))
                        if not re.fullmatch(r'[1-9][0-9]{0,6}', raw_number) or not previous_row < int(raw_number) <= 1048576:
                            raise Rejected('invalid_document')
                        row_number = int(raw_number); previous_row = row_number
                        next_column = 1
                        for cell in row:
                            if cell.tag.rsplit('}', 1)[-1] != 'c':
                                continue
                            coordinate, column, origin = _cell_position(cell, row_number, next_column)
                            if column < next_column:
                                raise Rejected('invalid_document')
                            next_column = column + 1
                            if row.get('r') is None:
                                origin = 'position'
                            values = [x.text or '' for x in cell.iter()
                                      if x.tag.rsplit('}', 1)[-1] in ('v', 't')]
                            value = ''.join(values)
                            if cell.get('t') == 's':
                                if not value.isdecimal() or len(value) > 8 or int(value) >= len(shared):
                                    raise Rejected('invalid_document')
                                value = shared[int(value)]
                            start = sink.length
                            sink.add(value)
                            end = sink.length
                            if end > start:
                                if len(metadata['cell_spans']) < MAX_CELL_SPANS:
                                    metadata['cell_spans'].append({'sheet_index': sheet_index,
                                        'row': row_number, 'cell': coordinate, 'start': start, 'end': end,
                                        'coordinates_source': origin})
                                else:
                                    metadata['provenance_truncated'] = True
                            sink.add('\t')
                        sink.add('\n')
                    metadata['sheet_spans'].append({'sheet_index': sheet_index, 'sheet_name': sheet_name,
                        'part': worksheet, 'start': sheet_start, 'end': sink.length})
                    metadata['sheets_processed'] += 1
                    if sink.length >= sink.limit:
                        sink.truncated |= sheet_index < len(worksheets)
                        break
    except (KeyError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise Rejected('invalid_document') from None
    return Extraction('ready', ''.join(sink.parts), 'stdlib-' + kind + '-v2', sink.truncated, metadata=metadata)


def _image_dimensions(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24:
        if data[12:16] != b'IHDR':
            raise Rejected('invalid_document')
        return struct.unpack('>II', data[16:24]), 'png'
    if data.startswith(b'\xff\xd8'):
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 255:
                raise Rejected('invalid_document')
            while offset < len(data) and data[offset] == 255:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in (0xD9, 0xDA):
                break
            if marker in (0x01, *range(0xD0, 0xD8)):
                continue
            if offset + 2 > len(data):
                break
            length = int.from_bytes(data[offset:offset+2], 'big')
            if length < 2 or offset + length > len(data):
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if length < 7:
                    break
                height, width = struct.unpack('>HH', data[offset+3:offset+7])
                return (width, height), 'jpg'
            offset += length
        raise Rejected('invalid_document')
    return None, None


def _resource_fds():
    value=os.environ.get('PROCESSING_RESOURCE_FD')
    return (int(value),) if value is not None else ()


def _run_native(command, timeout=10):
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False, pass_fds=_resource_fds())
    except FileNotFoundError:
        raise Rejected('extractor_unavailable') from None
    except subprocess.TimeoutExpired:
        raise Rejected('extraction_timeout') from None
    if completed.returncode:
        raise Rejected('invalid_document')


def _pdf(source, root, text_char_limit=MAX_TEXT_CHARS):
    """OCR only blank-text pages, with explicit page/time/size coverage."""
    output = root / 'output.txt'
    _run_native(['pdftotext', '-enc', 'UTF-8', '-f', '1', '-l', str(MAX_PDF_PAGES+1),
                 str(source), str(output)], timeout=12)
    if not output.is_file():
        raise Rejected('invalid_document')
    with output.open('rb') as stream:
        raw = stream.read(MAX_TEXT_CHARS*4+4)
    decoded = raw.decode('utf-8', errors='replace')
    pages = decoded.split('\f')
    if pages and pages[-1] == '':
        pages.pop()
    # A page with no text still emits a form feed. A bounded read may end in
    # the middle of a page, so never claim a complete source page count then.
    total = len(pages)
    truncated = output.stat().st_size > len(raw) or total > MAX_PDF_PAGES
    metadata = {'pages_processed': 0, 'ocr_pages': 0, 'ocr_attempts': 0, 'ocr_pages_limit': MAX_OCR_PAGES,
                'pdf_page_limit': MAX_PDF_PAGES, 'page_spans': [], 'text_char_limit': text_char_limit}
    if not truncated:
        metadata['pages_total'] = total
    sink = TextSink(text_char_limit)
    sink.truncated = truncated
    deadline = time.monotonic() + 23
    partial_error = None
    for page_number, content in enumerate(pages[:MAX_PDF_PAGES], 1):
        method = 'text'
        if not content.strip():
            method = 'unavailable'
            if metadata['ocr_attempts'] >= MAX_OCR_PAGES:
                partial_error = 'ocr_page_limit'
            elif time.monotonic() >= deadline:
                partial_error = 'ocr_time_limit'
            else:
                prefix = root / 'page'
                image = root / 'page.png'
                text_file = root / 'page-text.txt'
                try:
                    metadata['ocr_attempts'] += 1
                    _run_native(['pdftoppm', '-f', str(page_number), '-l', str(page_number),
                        '-singlefile', '-scale-to', '1600', '-png', str(source), str(prefix)],
                        timeout=max(0.1, min(6, deadline-time.monotonic())))
                    with image.open('rb') as stream:
                        header = stream.read(32)
                    dimensions, _ = _image_dimensions(header)
                    if not dimensions or max(dimensions) > 1600:
                        raise Rejected('image_size_limit')
                    _run_native(['tesseract', str(image), str(root/'page-text'), '-l', OCR_LANGUAGES,
                        '--dpi', '150', '--psm', '3'], timeout=max(0.1, min(6, deadline-time.monotonic())))
                    with text_file.open('rb') as stream:
                        page_raw = stream.read(MAX_TEXT_CHARS*4+4)
                    content = page_raw.decode('utf-8', errors='replace')
                    sink.truncated |= text_file.stat().st_size > len(page_raw)
                    metadata['ocr_pages'] += 1
                    method = 'ocr'
                except (Rejected, OSError) as error:
                    partial_error = error.code if isinstance(error, Rejected) else 'extraction_failed'
                finally:
                    image.unlink(missing_ok=True)
                    text_file.unlink(missing_ok=True)
            if method == 'unavailable':
                sink.truncated = True
        start = sink.length
        sink.add('[page ' + str(page_number) + ']\n')
        sink.add(content)
        sink.add('\n')
        metadata['page_spans'].append({'page': page_number, 'start': start, 'end': sink.length,
                                       'method': method})
        if method != 'unavailable':
            metadata['pages_processed'] += 1
        if sink.length >= sink.limit:
            sink.truncated |= page_number < min(total, MAX_PDF_PAGES)
            break
    engine = 'poppler-tesseract-eng-rus-v3' if metadata['ocr_pages'] else 'poppler-pdftotext-v2'
    if metadata['pages_processed'] == 0 and partial_error:
        return Extraction('unsupported' if partial_error == 'extractor_unavailable' else 'failed',
            extractor=engine, truncated=True, error_code=partial_error, metadata=metadata)
    return Extraction('ready', ''.join(sink.parts), engine, sink.truncated, partial_error, metadata)


def _native(data, kind, text_char_limit=MAX_TEXT_CHARS):
    # Child only. Use generated constant filenames; never pass an archive
    # filename, URL or user-selected executable to an external program.
    with tempfile.TemporaryDirectory(prefix='extract-') as directory:
        root = Path(directory)
        source = root / ('input.' + kind)
        source.write_bytes(data)
        output = root / 'output.txt'
        if kind == 'pdf':
            try:
                return _pdf(source, root, text_char_limit)
            except Rejected as error:
                if error.code == 'extractor_unavailable':
                    return Extraction('unsupported', error_code=error.code)
                raise
        else:
            # ANYM-669: the TSV (per-word confidence) comes from the same run; the text is the txt output as before.
            command = ['tesseract', str(source), str(root / 'output'), '-l', OCR_LANGUAGES,
                       '--dpi', '150', '--psm', '6', 'txt', 'tsv']
            engine = IMAGE_OCR_EXTRACTOR
        try:
            completed = subprocess.run(command, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False, pass_fds=_resource_fds())
        except FileNotFoundError:
            return Extraction('unsupported', error_code='extractor_unavailable')
        except subprocess.TimeoutExpired:
            raise Rejected('extraction_timeout') from None
        if completed.returncode or not output.is_file():
            raise Rejected('invalid_document')
        with output.open('rb') as file:
            raw = file.read(MAX_TEXT_CHARS * 4 + 4)
        sink = TextSink(text_char_limit)
        text = raw.decode('utf-8', errors='replace')
        sink.add(text)
        sink.truncated |= output.stat().st_size > len(raw)
        result = sink.result(engine)
        try:
            measured = _ocr_measure(root / 'output.tsv')
        except Exception:  # a measurement never fails or changes the extraction
            measured = {}
        return replace(result, metadata={**result.metadata, **measured}) if measured else result


def _extract(data, mime, filename, text_char_limit=MAX_TEXT_CHARS):
    suffix = Path(filename).suffix.lower()
    if data.startswith(b'%PDF-'):
        return _native(data, 'pdf', text_char_limit)
    dimensions, image_kind = _image_dimensions(data)
    if dimensions:
        if min(dimensions) < 1 or dimensions[0] * dimensions[1] > MAX_IMAGE_PIXELS:
            raise Rejected('image_size_limit')
        return _native(data, image_kind, text_char_limit)
    if suffix == '.docx' or mime == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
        return _office(data, 'docx', text_char_limit)
    if suffix == '.xlsx' or mime == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':
        return _office(data, 'xlsx', text_char_limit)
    if (mime.startswith('text/') or suffix in ('.txt', '.md', '.csv', '.json', '.log', '.xml', '.html')
            or mime in ('application/json', 'application/xml')):
        prefix = data[:MAX_TEXT_CHARS * 4 + 8]
        if data.startswith((b'\xff\xfe', b'\xfe\xff')):
            value = prefix.decode('utf-16', errors='replace')
        else:
            if b'\x00' in data:
                raise Rejected('binary_text')
            value = prefix.decode('utf-8-sig', errors='replace')
        sink = TextSink(text_char_limit)
        sink.add(value)
        sink.truncated |= len(prefix) < len(data)
        return sink.result('stdlib-text-v1')
    return Extraction('unsupported', error_code='unsupported_format')


def _limits():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (32, 32))
    # Input staging must admit every accepted 32 MiB original. Native temporary
    # output is bounded by this same limit; returned text is much smaller.
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_INPUT_BYTES, MAX_INPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.platform.startswith('linux'):
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))


def _child():
    _limits()
    try:
        request = json.loads(sys.stdin.buffer.read(16385))
        data = _read_original(request['path'], request['digest'], request['bytes'])
        result = _extract(data, request['mime'], request['filename'], request['text_char_limit'])
    except Rejected as error:
        result = Extraction('failed', error_code=error.code)
    except Exception:
        # Never serialize parser traces, paths or document content as errors.
        result = Extraction('failed', error_code='extraction_failed')
    sys.stdout.write(json.dumps(asdict(result), ensure_ascii=True))


def extract_original(path, *, digest, bytes, mime, filename, text_char_limit=MAX_TEXT_CHARS, resource_fd=None):
    """Return a bounded Extraction; never read archive bytes in the DB worker.

    Production uses Linux RLIMIT_AS in addition to the worker's cgroup limit.
    macOS supports CPU/file/descriptor/time limits; it has no AS limit here.
    """
    if type(text_char_limit) is not int or not 1000 <= text_char_limit <= MAX_TEXT_CHARS:
        return Extraction('failed', error_code='invalid_text_limit')
    if (type(bytes) is not int or not 0 <= bytes <= MAX_INPUT_BYTES):
        return Extraction('failed', error_code='input_size_limit')
    if (not isinstance(filename, str) or len(filename) > 4096
            or not isinstance(mime, str) or len(mime) > 256):
        return Extraction('failed', error_code='invalid_metadata')
    request = {'path': str(path), 'digest': digest, 'bytes': bytes, 'mime': mime, 'filename': filename, 'text_char_limit': text_char_limit}
    encoded = json.dumps(request).encode()
    if len(encoded) > 16384:
        return Extraction('failed', error_code='invalid_metadata')
    with tempfile.TemporaryDirectory(prefix='message-extraction-') as directory:
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
               'HOME': directory, 'TMPDIR': directory, 'OMP_THREAD_LIMIT': '1'}
        if resource_fd is not None:env['PROCESSING_RESOURCE_FD']=str(resource_fd)
        # A tempfile bounds output storage independently of communicate(). The
        # child FSIZE cap applies to stdout and every native parser output.
        with tempfile.TemporaryFile() as output:
            child = subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve()), '--child'],
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL, env=env,
                cwd=directory, start_new_session=True, close_fds=True,
                pass_fds=(resource_fd,) if resource_fd is not None else ())
            try:
                child.communicate(encoded, timeout=EXTRACTION_TIMEOUT)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                return Extraction('failed', error_code='extraction_timeout')
            finally:
                # Native parser descendants must not survive even if the
                # Python child died due to its own CPU/memory/file limits.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if child.returncode:
                return Extraction('failed', error_code='extraction_resource_limit')
            output.seek(0)
            raw = output.read(MAX_RESULT_BYTES + 1)
        try:
            result = json.loads(raw) if len(raw) <= MAX_RESULT_BYTES else None
            if (not isinstance(result, dict) or set(result) != {'state', 'text', 'extractor', 'truncated', 'error_code', 'metadata'}
                    or result['state'] not in ('ready', 'failed', 'unsupported')
                    or not isinstance(result['text'], str) or len(result['text']) > text_char_limit
                    or '\x00' in result['text'] or type(result['truncated']) is not bool
                    or (result['extractor'] is not None and not re.fullmatch('[a-z0-9-]{1,64}', result['extractor']))
                    or (result['error_code'] is not None and not re.fullmatch('[a-z_]{1,64}', result['error_code']))
                    or not valid_metadata(result['metadata'])):
                raise ValueError()
            return Extraction(**result)
        except (ValueError, TypeError):
            return Extraction('failed', error_code='invalid_extraction_result')


if __name__ == '__main__':
    if sys.argv[1:] != ['--child']:
        raise SystemExit('internal extractor only')
    _child()
