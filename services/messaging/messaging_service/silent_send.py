"""ANYM-927: a Telegram send without a notification sound (`silent`), for example late in the evening.

`silent` is part of a send's render policy only when true, so a loud send keeps every digest, request hash,
duplicate match and stored row of before. WhatsApp has no silent send: it is refused by name, nothing queued.
"""

# In the argument's schema: the preview_message and send_message descriptions are at their cap.
DESCRIPTION = ('Telegram chat_id sends: true delivers without a notification sound (the recipient sees the message, '
    'their phone stays quiet), for example late in the evening. It is part of the preview: preview_message with '
    'silent true shows silent: true and its preview_digest binds it, so send with the same silent. WhatsApp has no '
    'silent send: silent_unsupported, nothing queued. Phone and username recipients: silent_unsupported or '
    'invalid_parameters; preview the archived chat_id instead. Omitted or false is the usual send with sound.')


def render_policy(link_preview, allow_lossy, silent=False):
    """The stored and digest-bound send policy."""
    return {'link_preview': link_preview, 'allow_lossy': allow_lossy, **({'silent': True} if silent else {})}


def unsupported():
    from .field_errors import refusal
    return refusal('silent_unsupported', [('silent', 'enum', 'false: WhatsApp has no silent send', [False])])


def check(silent, platform):
    """Refuse a silent send to any chat but a Telegram one."""
    if silent and platform != 'telegram':
        raise unsupported()


def chat_id_only(arguments, phone, username):
    """silent rides only a chat_id send; a phone opens a WhatsApp chat, which has no silent send.
    An explicit false is dropped: the phone and username routes keep their arguments of before."""
    if (phone is None and username is None) or 'silent' not in arguments:
        return
    if arguments.pop('silent') is not False:
        from .field_errors import refusal
        if phone is not None:
            raise unsupported()
        raise refusal('invalid_parameters', [('silent', 'format', 'a chat_id recipient')])
