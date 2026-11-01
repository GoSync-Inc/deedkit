"""The collector side of press_button (ANYM-676): re-check the LIVE message against the preview, press once, report.

The callback data lives only in local variables here. It is never put in a result, a log line or an exception
text, and no failure path formats an exception: only fixed codes leave this module. Nothing is retried.
"""
import asyncio
import contextlib
import hmac
import logging
import math
import re
import time
import urllib.parse
from datetime import datetime, timezone

from .button_fingerprint import fingerprint
from .button_press import layout_digest, text_digest
from .incoming_formatting import capture_url
from .message_buttons import MAX_BUTTONS, MAX_TEXT, build_rows, clean

log = logging.getLogger('anymsg.press_button')
PRESS_KEYS = ('row', 'column', 'label', 'fp', 'edit_date', 'layout_digest', 'text_sha256')
ANSWER_TEXT = 256
EXCERPT = 1000
AFTER_DELAY = 2.0
AFTER_BUTTONS = 50
MIN_ECHO = 8  # Callback data shorter than this is not a credential and is not suppressed.
NEXT_ACTION = ('read the chat after pressed_at; a bot often answers with a new message or an edit; re-read the '
               'message until a new revision appears before the next press (it needs that expected_revision)')
AFTER_GRACE = 1.0  # kept free inside the command's own timeout for the receipt
_CONTROL = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏  ‪-‮⁦-⁩﻿]')


def scrub(value, maximum):
    """Bot text that the receipt validator accepts: no control characters (newlines stay), no lone surrogates."""
    if not isinstance(value, str):
        return ''
    return _CONTROL.sub(' ', value[:maximum * 4]).encode('utf-8', 'replace').decode('utf-8')[:maximum]


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def moment(message):
    value = getattr(message, 'edit_date', None) or getattr(message, 'date', None)
    return value.astimezone(timezone.utc).replace(microsecond=0) if isinstance(value, datetime) else None


def raw_button(markup, row, column):
    """The raw inline button at the raw (row, column) of ANYM-681's inspection, else None."""
    from telethon import types
    if not isinstance(markup, types.ReplyInlineMarkup) or not isinstance(markup.rows, (list, tuple)):
        return None
    if row >= len(markup.rows[:MAX_BUTTONS]):
        return None
    raw_row = markup.rows[row]
    if not isinstance(raw_row, types.KeyboardInlineButtonRow) or not isinstance(raw_row.buttons, (list, tuple)):
        return None
    if column >= len(raw_row.buttons[:MAX_BUTTONS]):
        return None
    raw = raw_row.buttons[column]
    return raw if isinstance(raw, types.KeyboardInlineButton) else None


def live_view(message):
    """What the owner needs to reconfirm: the live text and labels (bot-supplied, untrusted), no data, no links."""
    rows, _ = build_rows(getattr(message, 'reply_markup', None))
    moment_value = moment(message)
    return {'live': {'edit_date': moment_value.isoformat() if moment_value else None,
                     'text': scrub(getattr(message, 'message', '') or '', EXCERPT),
                     'labels': [button['text'] for row in rows for button in row][:MAX_BUTTONS]}}


def check_live(message, chat, message_id, press):
    """The callback data of the live button when it still is what the owner previewed; else a refusal.

    Returns bytes. Raises CommandRejected('message_changed' | 'password_required' | 'unsupported_operation')."""
    from .commands import CommandRejected

    def changed():
        return CommandRejected('message_changed', data=live_view(message))

    if getattr(message, 'via_bot_id', None):
        # The callback would go to the inline bot, which the preview could not name (via_bot is not archived).
        raise CommandRejected('unsupported_operation')
    raw = raw_button(getattr(message, 'reply_markup', None), press['row'], press['column'])
    inner = getattr(raw, 'type', None)
    if raw is None or type(inner).__name__ != 'InlineButtonTypeCallback':
        raise changed()
    if getattr(inner, 'requires_password', None) is True:
        raise CommandRejected('password_required')
    data = getattr(inner, 'data', None)
    if not isinstance(data, (bytes, bytearray)):
        raise changed()
    data = bytes(data)
    rows, _ = build_rows(message.reply_markup)
    try:
        expected_moment = datetime.fromisoformat(press['edit_date']).astimezone(timezone.utc).replace(microsecond=0)
    except (TypeError, ValueError):
        raise changed() from None
    if (moment(message) != expected_moment or clean(getattr(raw, 'text', None), MAX_TEXT) != press['label'] or
            layout_digest(rows) != press['layout_digest'] or
            text_digest(getattr(message, 'message', '') or '') != press['text_sha256'] or
            not hmac.compare_digest(fingerprint(chat, message_id, press['row'], press['column'], data), press['fp'])):
        raise changed()
    return data


def redact(value, data, limit):
    """(text, redacted): the bot may echo the callback data; 8 bytes or longer never reaches a receipt."""
    if not isinstance(value, str) or not value:
        return None, False
    if len(data) >= MIN_ECHO:
        encoded = value.encode('utf-8', 'replace')
        forms = {data, urllib.parse.quote_from_bytes(data).encode(), urllib.parse.quote_from_bytes(data, safe='').encode()}
        if len(data.strip()) >= MIN_ECHO:
            forms.add(data.strip())  # a bot may trim the whitespace around what it echoes
        if any(form in encoded for form in forms):
            return '[redacted]', True
    return scrub(value, limit), False


def answer_of(response, data):
    """The bounded, redacted BotCallbackAnswer: {message, alert, url[, url_dropped][, answer_redacted]}."""
    text, hidden = redact(getattr(response, 'message', None), data, ANSWER_TEXT)
    result = {'message': text, 'alert': getattr(response, 'alert', None) is True, 'url': None}
    link = getattr(response, 'url', None)
    if isinstance(link, str) and link:
        shown, echoed = redact(link, data, 2048)
        if echoed:
            result['url'], hidden = shown, True
        elif capture_url(link):
            result['url'] = link
        else:
            result['url_dropped'] = True
    if hidden:
        result['answer_redacted'] = True
    return result


def redacted_text(value, data, limit):
    """Display text of the message after the press with the callback data (8 bytes or longer) removed."""
    text, hidden = redact(value, data, limit)
    return ('' if text is None else text), hidden


def display_buttons(markup, data):
    """[{text, kind}] for display, the echo of the callback data looked for in the RAW label (before the cleaning
    and cut that build_rows applies), so whitespace, control characters or truncation cannot hide it."""
    from telethon import types
    from .message_buttons import MAX_SCAN, _KIND
    shown, hidden = [], False
    if not isinstance(markup, types.ReplyInlineMarkup) or not isinstance(markup.rows, (list, tuple)):
        return shown, hidden
    for raw_row in markup.rows[:MAX_SCAN]:
        if not isinstance(raw_row, types.KeyboardInlineButtonRow) or not isinstance(raw_row.buttons, (list, tuple)):
            continue
        for raw in raw_row.buttons[:MAX_SCAN]:
            if not isinstance(raw, types.KeyboardInlineButton) or len(shown) >= AFTER_BUTTONS:
                continue
            label, echoed = redacted_text(getattr(raw, 'text', None), data, MAX_TEXT)
            hidden = hidden or echoed
            shown.append({'text': label, 'kind': _KIND.get(type(getattr(raw, 'type', None)).__name__, 'unknown')})
    return shown, hidden


async def newest_id(observe, client, peer):
    latest = await observe(lambda: client.get_messages(peer, limit=1))
    if isinstance(latest, (list, tuple)):
        latest = latest[0] if latest else None
    newest = getattr(latest, 'id', None)
    return newest if type(newest) is int else None


async def message_after(observe, client, peer, chat, message_id, press, data, baseline):
    """ONE bounded re-read after AFTER_DELAY; a failure is a code here and never changes the outcome.

    `observe` runs a read through the governor and the collection policy only: the command's saved revision and
    expiry no longer apply once the press has been delivered (the bot's edit may already be archived)."""
    await asyncio.sleep(AFTER_DELAY)
    live = await observe(lambda: client.get_messages(peer, ids=message_id))
    if live is None:
        return {'edited': False, 'deleted': True, 'text': '', 'buttons': [], 'new_messages': False}
    if getattr(live, 'id', None) != message_id or str(getattr(live, 'chat_id', None)) != str(chat):
        raise ValueError('reference_mismatch')
    rows, _ = build_rows(getattr(live, 'reply_markup', None))  # the layout digest uses the cleaned rows
    newest = await newest_id(observe, client, peer)
    body = getattr(live, 'message', '') or ''
    try:
        before = datetime.fromisoformat(press['edit_date']).astimezone(timezone.utc).replace(microsecond=0)
    except (TypeError, ValueError):
        before = None
    shown, hidden = display_buttons(getattr(live, 'reply_markup', None), data)
    text, echoed = redacted_text(body, data, EXCERPT)
    result = {'edited': (moment(live) != before or text_digest(body) != press['text_sha256'] or
                         layout_digest(rows) != press['layout_digest']),
              'deleted': False, 'text': text, 'buttons': shown,
              # Newer than the newest message seen BEFORE the press (else None: unknown, never a change).
              'new_messages': (newest > baseline) if newest is not None and baseline is not None else None}
    if hidden or echoed:
        result['redacted'] = True
    return result


@contextlib.contextmanager
def without_transport_replay(client, request):
    """Telethon's sender re-queues every pending request after a reconnect (its own message and session IDs),
    whatever request_retries says, and keeps a request pending after its awaiting future was cancelled: for a press
    that is a second callback nobody confirmed. While the press is in flight a reconnect first takes THIS request
    out of the pending set and fails its future; when the call ends, however it ends (answer, error, timeout,
    cancellation), every trace of the request is purged from the pending set and the send queue, so no later
    reconnect can send it. A late response is ignored by the sender (no parent request). Other requests, and the
    reconnect itself, are untouched."""
    sender = getattr(client, '_sender', None)
    original = getattr(sender, '_reconnect', None)
    if sender is None or original is None:
        yield
        return

    def ours(state):
        asked = getattr(state, 'request', None)
        return asked is request or getattr(asked, 'query', None) is request

    def forget():
        pending = getattr(sender, '_pending_state', None)
        if isinstance(pending, dict):
            for key in [key for key, state in pending.items() if ours(state)]:
                state = pending.pop(key)
                if not state.future.done():
                    state.future.set_exception(ConnectionError('press_not_replayed'))
        queued = getattr(getattr(sender, '_send_queue', None), '_deque', None)
        if queued is not None:
            for state in [state for state in queued if ours(state)]:
                queued.remove(state)
                if not state.future.done():
                    state.future.cancel()

    async def reconnect(last_error):
        forget()
        return await original(last_error)
    sender._reconnect = reconnect
    try:
        yield
    finally:
        forget()
        if sender.__dict__.get('_reconnect') is reconnect:
            del sender.__dict__['_reconnect']


def submitted(answer, pressed_at, after, after_code, *, timed_out=False):
    data = {'effect_verified': False, 'answer': answer, 'pressed_at': pressed_at, 'message_after': after,
            'next_action': NEXT_ACTION}
    if timed_out:
        data['answer_timeout'] = True
    if after_code:
        data['message_after_code'] = after_code
    return {'outcome': 'submitted', **data}  # execute() puts everything but the outcome under `data`


async def admit(governor):
    """Per account 3 presses at once, then one per 5 s, 200 per UTC day (in memory); over it, nothing is sent."""
    from .commands import CommandRejected
    if governor is not None:
        wait = await governor.admit('press_button', 1)
        if wait:
            raise CommandRejected('rate_limited', retry_after=max(1, int(wait)))


async def press(client, job, governor, rpc, invoke, peer, chat, message_id, clear_started, check_policy):
    """Check, press once, report. `clear_started` marks a definite refusal as not started."""
    from telethon import errors, functions
    from .commands import CommandRejected
    from .telegram_governor import ProviderRateLimited
    block = job.get('press')
    if (not isinstance(block, dict) or any(key not in block for key in PRESS_KEYS) or
            type(block['row']) is not int or type(block['column']) is not int):
        raise CommandRejected('invalid_parameters')

    async def observe(call):
        async def guarded():
            check_policy()
            return await call()
        return await (governor.call(guarded, 'provider_command') if governor is not None else guarded())

    try:
        message = await rpc(lambda: client.get_messages(peer, ids=message_id))
    except ProviderRateLimited as exc:
        raise CommandRejected('rate_limited', retry_after=max(1, int(exc.seconds))) from None
    if (message is None or getattr(message, 'id', None) != message_id or
            str(getattr(message, 'chat_id', None)) != str(chat)):
        raise CommandRejected('provider_reference_mismatch')
    data = check_live(message, chat, message_id, block)
    # The newest message before the press: a message that already exists is not the bot's answer.
    try:
        newest = await newest_id(rpc, client, peer)
    except CommandRejected:
        raise
    except Exception:
        newest = None
    baseline = max(message_id, newest) if newest is not None else None
    request = functions.messages.GetBotCallbackAnswerRequest(peer, message_id, data=data)
    pressed_at = now_utc().isoformat()  # before the request leaves
    try:
        with without_transport_replay(client, request):
            response = await invoke(request, mutation=True)
    except (errors.BotResponseTimeoutError, errors.rpcerrorlist.TimeoutError):
        response, timed_out = None, True
    except errors.DataInvalidError:
        clear_started()
        raise CommandRejected('message_changed') from None
    except errors.PasswordMissingError:
        clear_started()
        raise CommandRejected('password_required') from None
    except (errors.MessageIdInvalidError, errors.MsgIdInvalidError, errors.PeerIdInvalidError,
            errors.ChannelInvalidError, errors.ChannelPrivateError):
        clear_started()
        raise CommandRejected('not_found') from None
    else:
        timed_out = False
    # The press is delivered. Nothing below may change the outcome or raise.
    try:
        answer = None if timed_out else answer_of(response, data)
    except Exception:
        answer = {'message': None, 'alert': False, 'url': None, 'answer_unreadable': True}
    after, after_code = None, None
    budget = job.get('_ends_at', math.inf) - time.monotonic() - AFTER_GRACE
    if budget <= AFTER_DELAY:
        after_code = 'reread_skipped'
    else:
        try:
            after = await asyncio.wait_for(message_after(observe, client, peer, chat, message_id, block, data, baseline),
                                           budget)
        except asyncio.CancelledError:
            # A shutdown during the optional re-read: the worker settles the delivered press first, then stops.
            job['_cancel_after_settle'] = True
            after_code = 'reread_interrupted'
        except TimeoutError:
            after_code = 'reread_timeout'
        except Exception:
            after_code = 'reread_failed'
    log.info('press_button state=submitted answer=%s timeout=%s after=%s', answer is not None, timed_out,
             after_code or 'ok')
    del data
    return submitted(answer, pressed_at, after, after_code, timed_out=timed_out)
