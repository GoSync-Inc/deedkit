"""Inline keyboards of a Telegram message as read-only evidence (ANYM-658).

Labels, kinds and links only. The callback data, an inline query and every other press token are never read:
each button is built from named attributes, never serialized or logged as a Telethon object. Nothing here
presses, opens, fetches or previews a button.
"""
import os
import re

from .envelope import MAX_ENVELOPE, canonical
from .incoming_formatting import capture_url, credential_link, safe_url

KINDS = ('url', 'callback', 'web_app', 'login_url', 'switch_inline', 'buy', 'copy', 'game', 'unknown')
LINK_KINDS = ('url', 'web_app', 'login_url')
MAX_BUTTONS = 100
MAX_TEXT = 128
MAX_COPY_TEXT = 256
COMPACT_BUTTONS = 8
# Traversal bound for hostile markup: rows and buttons examined, not only kept.
MAX_SCAN = 1000
# Room the capture record around the envelope takes (kind, peer_kind, captured_at, keys).
WRAPPER_RESERVE = 1024
_CONTROL = re.compile('[\x00-\x1f\x7f-\x9f​-‏  ‪-‮⁦-⁩﻿]')
_KIND = {'InlineButtonTypeUrl': 'url', 'InlineButtonTypeCallback': 'callback', 'InlineButtonTypeWebView': 'web_app',
         'ButtonTypeSimpleWebView': 'web_app', 'InlineButtonTypeUrlAuth': 'login_url',
         'InlineButtonTypeSwitchInline': 'switch_inline', 'InlineButtonTypeBuy': 'buy',
         'InlineButtonTypeCopy': 'copy', 'InlineButtonTypeGame': 'game'}


def buttons_capture():
    # Off until the release a rollback would return to accepts `buttons` and tg:// links (ANYM-658; as ANYM-274/611).
    return os.environ.get('ANYMSG_CAPTURE_BUTTONS') == '1'


def clean(value, maximum):
    """Display text: control and bidi-control characters become spaces; stripped and cut."""
    if not isinstance(value, str):
        return ''
    return _CONTROL.sub(' ', value[:maximum * 4]).strip()[:maximum]


def build_button(raw):
    """One button from named attributes only; None when it is not a keyboard button."""
    from telethon import types
    if not isinstance(raw, types.KeyboardInlineButton):
        return None
    kind_object = getattr(raw, 'type', None)
    kind = _KIND.get(type(kind_object).__name__, 'unknown')
    item: dict[str, str] = {'text': clean(getattr(raw, 'text', None), MAX_TEXT), 'kind': kind}
    if kind in LINK_KINDS:
        link = getattr(kind_object, 'url', None)
        if capture_url(link):
            item['url'] = link
    elif kind == 'copy':
        # The text copied to the clipboard is shown to the user; it is not a press token.
        copy_text = clean(getattr(kind_object, 'copy_text', None), MAX_COPY_TEXT)
        if copy_text:
            item['copy_text'] = copy_text
    return item


def build_rows(markup):
    """({rows}, truncated) from an inline markup; ([], False) for anything else. Bounded by MAX_SCAN and MAX_BUTTONS."""
    from telethon import types
    if not isinstance(markup, types.ReplyInlineMarkup) or not isinstance(markup.rows, (list, tuple)):
        return [], False
    rows, kept, scanned, truncated = [], 0, 0, False
    for raw_row in markup.rows[:MAX_SCAN]:
        if not isinstance(raw_row, types.KeyboardInlineButtonRow) or not isinstance(raw_row.buttons, (list, tuple)):
            continue
        row = []
        for raw in raw_row.buttons[:MAX_SCAN]:
            scanned += 1
            item = build_button(raw)
            if item is None:
                continue
            if kept >= MAX_BUTTONS:
                truncated = True
                break
            row.append(item)
            kept += 1
        if row:
            rows.append(row)
        if truncated or scanned >= MAX_SCAN:
            break
    return rows, truncated


def _block(rows, truncated):
    return {'rows': rows, **({'truncated': True} if truncated else {})}


def _size(block):
    return len(canonical(block)) + len(b',"buttons":')


def _first(rows, count):
    result = []
    for row in rows:
        if count <= 0:
            break
        result.append(row[:count])
        count -= len(row)
    return result


def fit(rows, truncated, budget):
    """The keyboard in at most `budget` serialized bytes: whole, else without links, else fewer trailing buttons
    (truncated: true). None when not even one button fits."""
    block = _block(rows, truncated)
    if _size(block) <= budget:
        return block
    bare = [[{k: v for k, v in button.items() if k != 'url'} for button in row] for row in rows]
    block = _block(bare, True)
    if _size(block) <= budget:
        return block
    low, high = 0, sum(len(row) for row in bare)
    while low < high:
        middle = (low + high + 1) // 2
        if _size(_block(_first(bare, middle), True)) <= budget:
            low = middle
        else:
            high = middle - 1
    return _block(_first(bare, low), True) if low else None


def capture(markup, envelope):
    """The `buttons` block for the envelope being built, or None. Never raises: a failure anywhere in the keyboard
    code costs the keyboard, never the message (ANYM-658). `envelope` is the dict without buttons; its serialized
    size and the capture record around it set the room left."""
    try:
        if not buttons_capture() or markup is None:
            return None
        rows, truncated = build_rows(markup)
        if not rows:
            return None
        budget = MAX_ENVELOPE - WRAPPER_RESERVE - len(canonical(envelope))
        block = fit(rows, truncated, budget)
        return validate_buttons(block) if block else None
    except Exception:  # noqa: BLE001  (deliberately broad; the message must survive any keyboard)
        return None


def _text(value, maximum):
    if not isinstance(value, str) or len(value) > maximum or _CONTROL.search(value):
        raise ValueError('invalid_metadata')
    return value


def validate_buttons(value):
    """The strict envelope shape: {rows: [[{text, kind, url?, copy_text?}]], truncated?: true}."""
    if not isinstance(value, dict) or set(value) - {'rows', 'truncated'} or 'rows' not in value:
        raise ValueError('invalid_metadata')
    rows = value['rows']
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_BUTTONS:
        raise ValueError('invalid_metadata')
    if 'truncated' in value and value['truncated'] is not True:
        raise ValueError('invalid_metadata')
    count, result = 0, []
    for row in rows:
        if not isinstance(row, list) or not row:
            raise ValueError('invalid_metadata')
        out = []
        for button in row:
            count += 1
            if (count > MAX_BUTTONS or not isinstance(button, dict) or not {'text', 'kind'} <= set(button)
                    or set(button) - {'text', 'kind', 'url', 'copy_text'}
                    or not isinstance(button['kind'], str) or button['kind'] not in KINDS):
                raise ValueError('invalid_metadata')
            item = {'text': _text(button['text'], MAX_TEXT), 'kind': button['kind']}
            if 'url' in button:
                if button['kind'] not in LINK_KINDS or not safe_url(button['url']) or credential_link(button['url']):
                    raise ValueError('invalid_metadata')
                item['url'] = button['url']
            if 'copy_text' in button:
                if button['kind'] != 'copy':
                    raise ValueError('invalid_metadata')
                item['copy_text'] = _text(button['copy_text'], MAX_COPY_TEXT)
            out.append(item)
        result.append(out)
    return _block(result, 'truncated' in value)


def compact(block):
    """Compact read view: the count and the text and kind of the first COMPACT_BUTTONS buttons, no link."""
    flat = [button for row in block['rows'] for button in row]
    return {'buttons_count': len(flat), 'buttons': [{'text': b['text'], 'kind': b['kind']} for b in flat[:COMPACT_BUTTONS]]}
