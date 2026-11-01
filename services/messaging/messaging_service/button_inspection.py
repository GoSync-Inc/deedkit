"""A live, read-only inspection of one message's inline keyboard (ANYM-681).

Returns labels, kinds, links and a keyed fingerprint per callback button, so that the press of ANYM-676 is bound to
the callback bytes that were inspected. The callback data and the key are never part of a result, a log line or an
exception text. Nothing is pressed, opened or fetched.
"""
import hashlib

from .button_fingerprint import fingerprint
from .message_buttons import MAX_BUTTONS, MAX_TEXT, clean
from .incoming_formatting import capture_url

# safe_result (commands.py) refuses a result over 60000 bytes; leave room for its wrapper.
RESULT_BUDGET = 56000
LINK_KINDS = ('url', 'web_app')


def _button(raw, chat, message_id, row, column):
    from telethon import types
    if not isinstance(raw, types.KeyboardInlineButton):
        return None
    inner = getattr(raw, 'type', None)
    from .message_buttons import _KIND
    kind = _KIND.get(type(inner).__name__, 'unknown')
    item = {'row': row, 'column': column, 'kind': kind, 'text': clean(getattr(raw, 'text', None), MAX_TEXT)}
    if kind in LINK_KINDS:
        link = getattr(inner, 'url', None)
        if capture_url(link):
            item['url'] = link
    elif kind == 'callback':
        data = getattr(inner, 'data', None)
        if getattr(inner, 'requires_password', None) is True:
            # DeedKit never handles the owner's 2FA password: no fingerprint, so nothing can be pressed.
            item['kind'] = 'callback_password'
        elif isinstance(data, (bytes, bytearray)):
            item['fp'] = fingerprint(chat, message_id, row, column, data)
        else:
            item['kind'] = 'unknown'
    return item


def _size(rows, truncated):
    import json
    return len(json.dumps(_wrap(rows, truncated), ensure_ascii=False, separators=(',', ':')).encode())


def _wrap(rows, truncated):
    return {'rows': rows, **({'truncated': True} if truncated else {})}


def _first(rows, count):
    result = []
    for row in rows:
        if count <= 0:
            break
        result.append(row[:count])
        count -= len(row)
    return result


def fit(rows, truncated, budget=RESULT_BUDGET):
    """Whole, else without links, else fewer trailing buttons (`truncated`)."""
    if _size(rows, truncated) <= budget:
        return rows, truncated
    bare = [[{k: v for k, v in button.items() if k != 'url'} for button in row] for row in rows]
    if _size(bare, True) <= budget:
        return bare, True
    low, high = 0, sum(len(row) for row in bare)
    while low < high:
        middle = (low + high + 1) // 2
        if _size(_first(bare, middle), True) <= budget:
            low = middle
        else:
            high = middle - 1
    return _first(bare, low), True


def inspect(message, chat, message_id):
    """The result data for a live Telethon message: {edit_date, text_sha256, rows[, truncated]}."""
    from telethon import types
    markup = getattr(message, 'reply_markup', None)
    rows, kept, truncated = [], 0, False
    if isinstance(markup, types.ReplyInlineMarkup) and isinstance(markup.rows, (list, tuple)):
        for row_index, raw_row in enumerate(markup.rows[:MAX_BUTTONS]):
            if not isinstance(raw_row, types.KeyboardInlineButtonRow) or not isinstance(raw_row.buttons, (list, tuple)):
                continue
            row = []
            for column, raw in enumerate(raw_row.buttons[:MAX_BUTTONS]):
                item = _button(raw, chat, message_id, row_index, column)
                if item is None:
                    continue
                if kept >= MAX_BUTTONS:
                    truncated = True
                    break
                row.append(item)
                kept += 1
            if row:
                rows.append(row)
            if truncated:
                break
    rows, truncated = fit(rows, truncated)
    moment = getattr(message, 'edit_date', None) or getattr(message, 'date', None)
    body = getattr(message, 'message', '') or ''
    return {'edit_date': moment.isoformat() if hasattr(moment, 'isoformat') else None,
            'text_sha256': hashlib.sha256(body.encode('utf-8')).hexdigest(), **_wrap(rows, truncated)}
