"""ANYM-736: the optional chat-head precondition of send_message, like HTTP If-Match.

The caller names the archive ID of the newest message it read (`expected_last_message_id`). When eligible
messages reached the archive after it, nothing is queued and the call is refused `chat_changed_since_seen`
with counts, archive IDs and a get_context call; the body carries no message text, name or file name.
Honest scope: the check sees only what the archive holds at its query; it never reads the provider, and a
message arriving between the check and the provider send is a race it cannot close.
"""
from .field_errors import refusal
from .store import APIError

GUARDS = ('any_message', 'outgoing_only')
COUNT_CAP = 20
TIMEOUT_MS = 1000
NEXT_ACTION = 'read_newer_messages_then_decide'


def unknown():
    """One body for an expected ID that does not exist, sits in another chat or account, is out of scope or
    cannot name a chat (a phone or username recipient), and for a key without read."""
    return refusal('invalid_parameters', [('expected_last_message_id', 'expected_message_unknown')],
        next_action='check_parameters')


def validate(expected, guard):
    """Argument shape only; no lookup. A guard with no expected ID would silently protect nothing."""
    if expected is None:
        if guard is not None:
            raise refusal('invalid_parameters', [('expected_last_message_id', 'required', 'string')])
        return
    if not isinstance(expected, str) or not 1 <= len(expected) <= 256:
        raise unknown()
    if guard is not None and (not isinstance(guard, str) or guard not in GUARDS):
        raise refusal('invalid_parameters', [('guard', 'enum', None, GUARDS)])


def newer(c, account, chat_id, platform, expected, guard):
    """The oldest COUNT_CAP+1 eligible messages after `expected` in the archive's order (sent_at; on Telegram
    the same sent_at and a larger numeric ID; never on WhatsApp or without a numeric order), and the newest.
    Returns None when `expected` is not a message of this chat, else (rows, newest_id)."""
    from .chat_queries import index_number
    from .conversation import meaningful_sql
    ref = c.execute('SELECT m.sent_at,' + index_number('m') + ' AS numeric_id FROM messages m '
        'WHERE m.id=%s AND m.account=%s AND m.chat=%s', (expected, account, chat_id)).fetchone()
    if ref is None:
        return None
    telegram = platform == 'telegram'
    number = 'coalesce(' + index_number('m') + ',0::bigint)'
    tie = ('(' + index_number('m') + ' IS NOT NULL AND %s::bigint IS NOT NULL AND ' + index_number('m') + '>%s)'
           if telegram else 'false')
    where = ('FROM messages m LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision '
        'WHERE m.account=%s AND m.chat=%s AND m.sent_at>=%s AND m.id<>%s AND ' + meaningful_sql() +
        ' AND (m.sent_at>%s OR ' + tie + ')' + (" AND m.direction='outgoing'" if guard == 'outgoing_only' else ''))
    args = [account, chat_id, ref['sent_at'], expected, ref['sent_at']] + (
        [ref['numeric_id'], ref['numeric_id']] if telegram else [])
    ascending = 'm.sent_at,' + (number + ',' if telegram else '') + 'm.id'
    descending = 'm.sent_at DESC,' + (number + ' DESC,' if telegram else '') + 'm.id DESC'
    rows = c.execute('SELECT m.id,m.direction ' + where + ' ORDER BY ' + ascending + ' LIMIT ' + str(COUNT_CAP + 1),
        args).fetchall()
    if len(rows) <= COUNT_CAP:
        return rows, rows[-1]['id'] if rows else None
    newest = c.execute('SELECT m.id ' + where + ' ORDER BY ' + descending + ' LIMIT 1', args).fetchone()
    return rows, newest['id']


def check(c, p, account, chat_id, platform, expected, guard):
    """Refuse (APIError) when the chat moved past `expected`; return None when it did not.

    Runs with no principal lock held, in its own savepoint under a local statement_timeout; any failure
    other than the two refusals is `chat_guard_unavailable` (HTTP 503) and nothing was queued."""
    if ('read' not in p['permissions'] or account not in p['accounts']
            or (p['chat_ids'] is not None and chat_id not in p['chat_ids'])):
        raise unknown()
    try:
        with c.transaction():
            previous = c.execute("SELECT current_setting('statement_timeout') AS v").fetchone()['v']
            c.execute("SELECT set_config('statement_timeout',%s,true)", (str(TIMEOUT_MS),))
            found = newer(c, account, chat_id, platform, expected, guard)
            c.execute("SELECT set_config('statement_timeout',%s,true)", (previous,))
    except Exception:
        raise APIError('chat_guard_unavailable', 503) from None
    if found is None:
        raise unknown()
    rows, newest = found
    if not rows:
        return
    counted = rows[:COUNT_CAP]
    failure = APIError('chat_changed_since_seen', 409)
    failure.newer_count = len(counted)
    failure.newer_count_capped = len(rows) > COUNT_CAP
    failure.newer_outgoing_count = sum(1 for row in counted if row['direction'] == 'outgoing')
    failure.first_newer_message_id = rows[0]['id']
    failure.newest_message_id = newest
    failure.next_action = NEXT_ACTION
    # view=conversation orders a window like this guard (on Telegram the numeric provider ID at one instant); a
    # direct REST caller defaults to the archive order, which would start the window past a counted message.
    failure.next_call = {'tool': 'get_context', 'arguments': {'message_id': rows[0]['id'],
        'before_count': 0, 'after_count': COUNT_CAP, 'view': 'conversation'}, 'caller_supplies': []}
    raise failure
