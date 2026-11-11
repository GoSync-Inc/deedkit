"""ANYM-646: a send or operation refused for a missing or changed preview names the call to make next.

A reference is echoed only after it resolved for the caller's key. The refusal keeps its code and status and gains the ANYM-531 `next_call` (reference-only `arguments`, the
caller's own values named in `caller_supplies`), a fixed `next_action` and a fixed `hint`. The digest is never
handed out (it binds content, not approval) and no caller content appears: only argument NAMES, the account and
the archive IDs the caller already named. The server only names the call; it never runs one."""

REFUSALS = ('preview_required', 'preview_mismatch')
ACTION = 'preview_then_send_with_digest'
SEND_HINT = ('If next_call is present, call its tool with its arguments plus your own values for caller_supplies, '
             'show the preview to the owner and, after the owner confirms, repeat this send unchanged with the '
             'returned preview_digest; if it is absent, this key or route cannot run the preview tool.')
OPERATION_HINT = ('If next_call is present, call its tool with its arguments plus your own values for caller_supplies, '
                  'show the preview to the owner and, after the owner confirms, repeat this operation unchanged with '
                  'the returned preview_digest; if it is absent, this key or route cannot run the preview tool.')


def _own(principal, account):
    """An account is echoed only when it is one of the caller's own: a caller's content never rides back in it.
    The chat and message references are the caller's to resolve first (outgoing.target, commands._resolve)."""
    return isinstance(account, str) and account in principal['accounts']


def _named(error, tool, arguments, supplies, hint):
    error.next_call = {'tool': tool, 'arguments': arguments, 'caller_supplies': supplies}
    error.hint = hint
    # The phone and username mismatch keeps its own next_action (preview_message_again).
    if not hasattr(error, 'next_action'):
        error.next_action = ACTION
    return error


def send(error, principal, account, chat_id='', *, phone='', username='', reply_to='', format='plain',
         link_preview=False, allow_lossy=False, silent=False, mentions=None, schedule_at=None):
    """`error` with the preview_message continuation when it is a missing or changed preview; else unchanged.
    caller_supplies names every argument the refused call set that the digest binds."""
    if getattr(error, 'code', None) not in REFUSALS or not _own(principal, account):
        return error
    arguments = {'account': account}
    if isinstance(chat_id, str) and chat_id:
        arguments['chat_id'] = chat_id
    given = (('text', True), ('phone', bool(phone)), ('username', bool(username)), ('reply_to', bool(reply_to)),
             ('format', format != 'plain'), ('link_preview', link_preview is True), ('allow_lossy', allow_lossy is True),
             ('silent', silent is True), ('mentions', bool(mentions)), ('schedule_at', schedule_at is not None))
    supplies = [name for name, is_set in given if is_set]
    return _named(error, 'preview_message', arguments, supplies, SEND_HINT)


def operation(error, principal, account, operation, chat_id='', message_id='', expected_revision=0, parameters=None):
    """`error` with the preview_messenger_operation continuation when it is a missing or changed preview."""
    if (getattr(error, 'code', None) not in REFUSALS or not _own(principal, account)
            or not isinstance(operation, str) or not operation):
        return error
    arguments: dict[str, str | int] = {'account': account, 'operation': operation}
    if isinstance(chat_id, str) and chat_id:
        arguments['chat_id'] = chat_id
    if isinstance(message_id, str) and message_id:
        arguments['message_id'] = message_id
    if type(expected_revision) is int and expected_revision:
        arguments['expected_revision'] = expected_revision
    supplies = ['parameters'] if parameters else []
    return _named(error, 'preview_messenger_operation', arguments, supplies, OPERATION_HINT)
