"""ANYM-737: a ready-to-send Telegram HTML rendering of a stored message body (body_format=html).

The converter is untrusted until the sender's own render_message reproduces the same visible text and the
same supported entities from the generated source. A row that fails that check carries a reason instead.
No provider call, no log of message text, nothing stored.
"""
import json

from .formatting import MAX_WIRE_UNITS, _url, render_message, utf16_units
from .incoming_formatting import wire_digest
from .store import APIError

BODY_KEY = '_revision_body'
BUDGET_BYTES = 256 * 1024
# The HTTP body of a preview or send is capped at 16,384 bytes (app.py NORMAL_BODY_BYTES); the rest of the request
# (account, chat, request id, digest, format) needs room, so the generated text is limited as JSON UTF-8.
MAX_TEXT_BYTES = 15100
TAGS = {'bold': 'b', 'italic': 'i', 'underline': 'u', 'strikethrough': 's', 'spoiler': 'tg-spoiler',
        'code': 'code', 'pre': 'pre', 'blockquote': 'blockquote', 'text_link': 'a'}
BLOCKS = frozenset(('blockquote', 'pre'))
# Telegram detects these itself; they stay plain text and do not make a row lossy.
IMPLICIT = frozenset(('url', 'email', 'phone', 'mention', 'hashtag', 'cashtag', 'bot_command'))
LISTED_WARNINGS = frozenset(('native_entities_unsupported', 'native_link_unsupported', 'native_language_unsupported'))
SEPARATOR = '\n\n'


class Unavailable(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def option(value):
    """'' (or absent) asks for nothing; html asks for the rendering."""
    from .store import APIError
    if value is None or value == '':
        return ''
    if value != 'html' or type(value) is not str:
        raise APIError('invalid_parameters')
    return value


def _text(value):
    return value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _attribute(value):
    return _text(value).replace('"', '&quot;')


def _open(span):
    tag = TAGS[span['type']]
    if span['type'] == 'text_link':
        return '<a href="' + _attribute(span['url']) + '">'
    if span['type'] == 'pre' and span.get('language'):
        return '<pre language="' + _attribute(span['language']) + '">'
    return '<' + tag + '>'


def _split(entities):
    """Supported spans to emit and the types left out. Sender-rejected links are omitted, never emitted."""
    spans, omitted = [], set()
    for entity in entities:
        kind = entity['type']
        if kind in IMPLICIT:
            continue
        if kind not in TAGS:
            omitted.add(kind)
            continue
        if kind == 'text_link':
            try:
                _url(entity.get('url'))
            except APIError:
                omitted.add(kind)
                continue
        spans.append(entity)
    return spans, omitted


def _source(body, spans):
    """HTML source for the body. Spans cross or sit on a bad boundary: Unavailable('not_reproducible')."""
    points, length = {0: 0}, 0
    for index, char in enumerate(body):
        length += 2 if ord(char) > 0xFFFF else 1
        points[length] = index + 1
    placed = []
    for number, span in enumerate(spans):
        start, end = points.get(span['offset']), points.get(span['offset'] + span['length'])
        if start is None or end is None or start >= end:
            raise Unavailable('not_reproducible')
        placed.append((start, end, number, span))
    stops = sorted({0, len(body), *(p[0] for p in placed), *(p[1] for p in placed)})
    out, stack, strip = [], [], False
    for position, following in zip(stops, stops[1:] + [None]):
        while stack and stack[-1][1] == position:
            span = stack.pop()[2]
            out.append('</' + TAGS[span['type']] + '>')
            strip = span['type'] in BLOCKS and not stack
        if any(item[1] == position for item in stack):
            raise Unavailable('not_reproducible')
        for start, end, number, span in sorted((p for p in placed if p[0] == position), key=lambda p: (-p[1], p[2])):
            if stack and end > stack[-1][1]:
                raise Unavailable('not_reproducible')
            if span['type'] in BLOCKS and not stack:
                # The sender joins a top-level block to its neighbours with a blank line of its own.
                joined = ''.join(out)
                if joined.endswith(SEPARATOR):
                    out = [joined[:-len(SEPARATOR)]]
            out.append(_open(span))
            stack.append((start, end, span))
            strip = False
        if following is not None:
            piece = body[position:following]
            if strip and not stack and piece.startswith(SEPARATOR):
                piece = piece[len(SEPARATOR):]
            if piece:
                strip = False
            out.append(_text(piece))
    if stack:
        raise Unavailable('not_reproducible')
    return ''.join(out)


def _canonical(entities):
    return sorted(json.dumps({k: e[k] for k in ('type', 'offset', 'length', 'url', 'language') if k in e},
                             sort_keys=True) for e in entities)


def build(body, evidence):
    """(artifact, None) or (None, reason)."""
    try:
        return _build(body, evidence), None
    except Unavailable as error:
        return None, error.reason


def _build(body, evidence):
    state = evidence.get('state') if isinstance(evidence, dict) else None
    if not isinstance(body, str) or state in (None, 'unknown'):
        raise Unavailable('unknown_formatting')
    if state == 'wire_observed':
        raise Unavailable('no_entities')
    if evidence.get('wire_sha256') != wire_digest(body):
        raise Unavailable('evidence_mismatch')
    if set(evidence.get('warnings') or []) - LISTED_WARNINGS:
        raise Unavailable('partial_evidence')
    try:
        units = utf16_units(body)
    except APIError:
        raise Unavailable('not_reproducible') from None
    if units > MAX_WIRE_UNITS:
        raise Unavailable('too_long')
    omitted = {e['type'] for e in evidence.get('unsupported_entities') or []}
    spans, more = _split(evidence.get('entities') or [])
    omitted |= more
    try:
        source = _source(body, spans)
        if len(json.dumps(source, ensure_ascii=False).encode()) > MAX_TEXT_BYTES:
            raise Unavailable('too_long')
        artifact = render_message('telegram', source, 'html')
    except Unavailable:
        raise
    except APIError:
        # The sender refused the generated text (limits, controls, whitespace spans): never returned.
        raise Unavailable('not_reproducible') from None
    # Any other exception is a fault of this code, not a property of the row: attach() reports render_failed.
    if artifact['rendered_text'] != body or _canonical(artifact['entities']) != _canonical(spans):
        raise Unavailable('not_reproducible')
    return {'format': 'html', 'text': source, 'lossless': not omitted, 'omitted_types': sorted(omitted)}


def _size(artifact):
    return len(json.dumps(artifact, ensure_ascii=False, separators=(',', ':')).encode())


def attach(rows):
    """Add body_rendered or body_rendered_unavailable to rows in response order; always drop the internal body.

    One conversion per (message, revision); the serialized artifacts of one response, repeats included, stay within BUDGET_BYTES.
    """
    done, spent, exceeded = {}, 0, False
    try:
        for row in rows:
            body = row.get(BODY_KEY)
            key = (row.get('id') or row.get('message'), row.get('revision'))
            if key not in done:
                try:
                    done[key] = build(body, {k: v for k, v in (row.get('formatting') or {}).items()
                                             if k != 'offsets_reference'})
                except Exception:
                    # Never the text or the exception: the type of failure is not logged either.
                    done[key] = (None, 'render_failed')
            artifact, reason = done[key]
            if artifact is not None:
                # Converted once, but every serialized occurrence (the anchor repeats in full detail) is charged.
                size = _size(artifact)
                if exceeded or spent + size > BUDGET_BYTES:
                    exceeded, artifact, reason = True, None, 'response_budget'
                else:
                    spent += size
            if artifact is not None:
                row['body_rendered'] = artifact
            else:
                row['body_rendered_unavailable'] = reason
    finally:
        for row in rows:
            row.pop(BODY_KEY, None)
    return rows
