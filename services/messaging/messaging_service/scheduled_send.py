"""ANYM-464: an owner-confirmed Telegram message or file scheduled for a set time; Telegram holds it.

`schedule_at` is part of a send's render policy (a send_file's parameters) as the UTC time, so the preview digest,
the request hash, the exact-retry match and the duplicate rule bind it. Telegram keeps the message in the owner's
Scheduled messages: the owner sees, moves, edits or deletes it in the app. A send rests in `scheduled`, then ends
`sent` (a send_file operation `succeeded`) with the published message, or `scheduled_cancelled` when it is deleted
before the time. Nothing here ever sends or resends: the collector only records what Telegram reports, and one
overdue check reads the chat's scheduled list once.
"""
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

MIN_AHEAD_S = 60
MAX_AHEAD = timedelta(days=365)
# At dispatch Telegram must still get a future time; closer than this is refused before the provider. Checked
# again inside the provider call itself, after every governor wait (r1 B1), so the margin covers only the call.
DISPATCH_MARGIN_S = 30
# The one live scheduled-list read gives up after this; it holds the shared background lane (r1 B3).
READ_TIMEOUT_S = 20
# A scheduled-list update for an ID no row holds yet is kept this long, for a receipt not yet committed (r1 M4).
PENDING_UPDATE_S = 120
PENDING_UPDATE_CAP = 512
# The collector checks a send this long after its time when no update settled it (criterion 6).
OVERDUE_S = 120
# An archived outgoing message counts as the published one from this long before the time on.
ARCHIVE_SLACK_S = 60
SWEEP_INTERVAL_S = 30
TABLES = {'outgoing': 'outgoing_messages', 'operation': 'messenger_commands'}
RFC3339 = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?([Zz]|[+-][0-9]{2}:[0-9]{2})')
WAITING_NEXT = 'wait_until_schedule_at'
CANCELLED_NEXT = 'ask_owner_before_scheduling_again'

# In the argument's schema: the preview_message and send_message descriptions are at their cap.
DESCRIPTION = ('Telegram chat_id sends: an absolute time, RFC 3339 with an offset (2026-11-12T09:00:00+03:00), 60 s '
    'to 365 days ahead. Telegram holds the message in the owner\'s Scheduled messages and publishes it then; the '
    'owner can move, edit or delete it in the app. preview_message shows schedule_at_utc, schedule_at_local in the '
    'owner\'s zone and owner_zone_source; its preview_digest binds the UTC time, so send with the same schedule_at. '
    'Status: scheduled (nothing to do until the time), then sent, or scheduled_cancelled when deleted before it. '
    'WhatsApp: schedule_unsupported, nothing queued. Omitted: send now.')


MESSAGES_SEND = ('An archived Telegram chat only: every step (files, then text) is held by Telegram until this '
    'absolute time, RFC 3339 with an offset, 60 s to 365 days ahead; the preview shows it as schedule and the '
    'confirm_token binds it. Each step then reads scheduled. A phone or @username without a chat, or WhatsApp, is '
    'refused. Omitted: send now.')


def _now():
    return datetime.now(timezone.utc)


def still_ahead(at, now=None):
    """True while Telegram still gets a time at least DISPATCH_MARGIN_S ahead; False for a missing time."""
    return at is not None and at - (now or _now()) >= timedelta(seconds=DISPATCH_MARGIN_S)


def _refusal(code, reason, expected, allowed=None, next_action=None):
    from .field_errors import refusal
    return refusal(code, [('schedule_at', reason, expected, *((allowed,) if allowed is not None else ()))],
                   next_action=next_action)


def parse(value):
    """The UTC time of a well-formed schedule_at (whole seconds, as Telegram keeps it) and its own offset; the
    format only, never the range, so an exact retry of a saved send still matches after its time."""
    if not isinstance(value, str) or len(value) > 40 or not RFC3339.fullmatch(value):
        raise _refusal('invalid_parameters', 'format', 'RFC 3339 date-time with an offset, e.g. 2026-11-12T09:00:00+03:00')
    try:
        at = datetime.fromisoformat(value.upper().replace('Z', '+00:00'))
    except ValueError:
        raise _refusal('invalid_parameters', 'format', 'RFC 3339 date-time with an offset') from None
    return at.astimezone(timezone.utc).replace(microsecond=0), at.utcoffset()


def utc_text(at):
    return at.strftime('%Y-%m-%dT%H:%M:%SZ')


def from_text(text):
    return datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)


def stored(value):
    """The persisted UTC text of a policy or parameters, or None when it is malformed."""
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', value):
        return None
    try:
        return from_text(value)
    except ValueError:
        return None


def normalized(value):
    """The UTC text the digest binds, or None when schedule_at is omitted."""
    return None if value is None else utc_text(parse(value)[0])


def in_range(value, now=None, *, digest_bound=False):
    """Refuse a time under 60 s or over 365 days ahead (criterion 2); a digest-bound send names its way on."""
    at = parse(value)[0]
    now = now or _now()
    if not timedelta(seconds=MIN_AHEAD_S) <= at - now <= MAX_AHEAD:
        raise _refusal('schedule_at_out_of_range', 'minimum' if at - now < MAX_AHEAD else 'maximum',
                       'a time 60 s to 365 days ahead',
                       next_action='send_now_or_preview_again' if digest_bound else None)
    return at


def unsupported():
    return _refusal('schedule_unsupported', 'enum', 'omitted: WhatsApp has no scheduled send', [None])


def check(schedule_at, platform):
    """Refuse a schedule for any chat but a Telegram one; nothing is queued."""
    if schedule_at is not None and platform != 'telegram':
        raise unsupported()


def preview_block(value, environ=None):
    """What the owner confirms: the UTC time and, when the owner's zone is set, the same time there; on the UTC
    fallback the local line is omitted and the caller's own offset is echoed (criterion 1)."""
    from .time_references import owner_zone
    at, offset = parse(value)
    zone, label = owner_zone(os.environ if environ is None else environ)
    block = {'schedule_at_utc': utc_text(at)}
    if label == 'UTC_fallback':
        total = int((offset or timedelta(0)).total_seconds())
        sign, total = '-' if total < 0 else '+', abs(total)
        block.update(owner_zone_source='utc_fallback', schedule_at_offset=f'{sign}{total // 3600:02d}:{total % 3600 // 60:02d}')
    else:
        block.update(owner_zone_source='owner', owner_zone=label, schedule_at_local=at.astimezone(zone).isoformat())
    return block


def chat_id_only(arguments, phone, username):
    """schedule_at rides only a chat_id send, as silent does: a phone or username opens a chat first."""
    if arguments.get('schedule_at') is None:
        arguments.pop('schedule_at', None)
        return
    if phone is None and username is None:
        return
    from .field_errors import refusal
    if phone is not None:
        raise unsupported()
    raise refusal('invalid_parameters', [('schedule_at', 'format', 'a chat_id recipient')])


def dispatch_time(value, platform, now=None):
    """The stored UTC time a dispatch passes to Telegram; ValueError for a malformed or non-Telegram row and
    `schedule_at_out_of_range` (a local refusal, nothing sent) when the time is no longer ahead."""
    at = stored(value)
    if at is None or platform != 'telegram':
        raise ValueError('invalid_persisted_schedule')
    if not still_ahead(at, now):
        from .outgoing import SendRejected
        raise SendRejected('schedule_at_out_of_range')
    return at


def operation_time(value, now=None):
    """A send_file's stored UTC time for SendMediaRequest, or None when it is malformed or no longer ahead."""
    at = stored(value)
    return at if still_ahead(at, now) else None


async def file_send(op, raw, request_of):
    """A send_file's SendMediaRequest (`request_of(schedule_date)`) through the governed call. A time no longer
    DISPATCH_MARGIN_S ahead is refused unsent, before the call and again inside it after every governor wait
    (r1 B1); the uploaded parts are no message, so the refusal clears the started mark."""
    at = stored(raw) if raw is not None else None

    def too_near():
        if raw is not None and not still_ahead(at):
            from .commands import CommandRejected
            op.clear_started()
            raise CommandRejected('schedule_at_out_of_range')
    too_near()
    request = request_of(at)

    async def send():
        too_near()
        return await op.client(request)
    return await op.rpc(send, mutation=True)


def scheduled_receipt(message, mapped, chat):
    """The receipt of a file Telegram now holds: its scheduled-list ID and time, only for our own random_id's
    message in the requested chat; None otherwise (the operation then stays uncertain, never retried)."""
    from telethon import utils
    identifier = getattr(message, 'id', None)
    peer = getattr(message, 'peer_id', None)
    if (type(identifier) is not int or identifier <= 0 or identifier not in mapped or peer is None or
            str(utils.get_peer_id(peer)) != str(chat) or getattr(message, 'date', None) is None):
        return None
    return {'outcome': 'scheduled', 'basis': 'provider_scheduled_message', 'provider_message_id': str(identifier),
            'provider_chat_id': str(chat), 'schedule_at': message.date.astimezone(timezone.utc).isoformat()}


def result_fields(row, policy_time=None):
    """The schedule facts of a send or operation row; empty for an unscheduled one."""
    if row.get('schedule_at') is None and policy_time is None:
        return {}
    at = row.get('schedule_at') or stored(policy_time)
    fields = {'schedule_at': at, 'scheduled_message_id': row.get('scheduled_message_id'),
              'edited_in_app': row.get('edited_in_app') is True}
    if row.get('cancel_reason'):
        fields['cancel_reason'] = row['cancel_reason']
    return fields


def operation_preview(operation, parameters, digest_bound=None):
    """A send_file's schedule as the owner confirms it (empty without one); refuses a time out of range. With
    `digest_bound` (a new operation's enqueue) it only checks the range."""
    if operation != 'send_file' or not isinstance(parameters, dict) or parameters.get('schedule_at') is None:
        return {}
    in_range(parameters['schedule_at'], digest_bound=bool(digest_bound))
    return {} if digest_bound is not None else {'schedule': preview_block(parameters['schedule_at'])}


def operation_fields(row):
    """A send_file operation's schedule facts and next action for get_messenger_operation; empty otherwise."""
    parameters = (row.get('payload') or {}).get('parameters') if isinstance(row.get('payload'), dict) else None
    policy_time = parameters.get('schedule_at') if isinstance(parameters, dict) else None
    fields = result_fields(row, policy_time) if row.get('operation') == 'send_file' else {}
    action = next_action(row['status']) if fields else None
    return {**fields, **({'next_action': action} if action else {})}


def next_action(status):
    return WAITING_NEXT if status == 'scheduled' else CANCELLED_NEXT if status == 'scheduled_cancelled' else None


# Criterion 7: an earlier send of the same account, chat, exact content and UTC time still held by Telegram,
# whatever its age. The time is the current schedule_at, which follows a move in the app; render_policy keeps the
# confirmed time for exact retries, so it is compared without it (r1 M3).
DUPLICATE_SQL = """SELECT principal,request_id,scheduled_message_id FROM outgoing_messages
    WHERE account=%s AND chat=%s AND source_text=%s AND source_format=%s AND render_policy-'schedule_at'=%s
      AND schedule_at=%s
      AND reply_provider_id IS NOT DISTINCT FROM %s AND status='scheduled' AND NOT (principal=%s AND request_id=%s)
    ORDER BY created_at DESC LIMIT 1"""


def refuse_duplicate(c, p, account, chat_id, request_id, text, format, policy, reply_id):
    """duplicate_scheduled (409) naming the earlier send; its request_id only when it is the caller's own."""
    rest = {k: v for k, v in policy.items() if k != 'schedule_at'}
    earlier = c.execute(DUPLICATE_SQL, (account, chat_id, text, format, Jsonb(rest), from_text(policy['schedule_at']),
                                        reply_id, p['id'], request_id)).fetchone()
    if earlier is None:
        return
    from .store import APIError
    failure = APIError('duplicate_scheduled', 409)
    fields = {'earlier_request_id': earlier['request_id']} if earlier['principal'] == p['id'] else {}
    fields.update(scheduled_message_id=earlier['scheduled_message_id'], next_action='check_scheduled_list_or_ask_owner',
                  next_call={'tool': 'execute_messenger_operation', 'arguments': {  # ANYM-929
                      'account': account, 'operation': 'list_scheduled', 'chat_id': chat_id}, 'caller_supplies': ['request_id']})
    for name, value in fields.items():
        setattr(failure, name, value)
    raise failure


# --- Collector side: every function below runs in the account's fenced collector and only records facts. ---

def journal(c, table, row, status):
    """One `message.status` event per transition (criterion 4): IDs, the status and the time, never text; only
    the key that made the send receives it (subscriptions._failure_audience). Its own savepoint: a failure is
    logged and the transition commits anyway."""
    handle = 'send_id' if table == 'outgoing_messages' else 'operation_id'
    try:
        with c.transaction():
            value = c.execute('UPDATE feed_counter SET value=value+1 WHERE id=1 RETURNING value').fetchone()['value']
            snapshot = {'account': row['account'], 'chat': row['chat'], handle: str(row['id']), 'status': status,
                        'principal': row['principal'], 'key_id': str(row['key_id']),
                        'schedule_at': row['schedule_at'].isoformat() if row.get('schedule_at') else None,
                        'scheduled_message_id': row.get('scheduled_message_id'),
                        'cancel_reason': row.get('cancel_reason')}
            c.execute('''INSERT INTO events(id,account,message,kind,revision,origin,snapshot,event_key)
                VALUES(%s,%s,NULL,'message.status',1,'live',%s,%s) ON CONFLICT DO NOTHING''',
                      (value, row['account'], Jsonb({k: v for k, v in snapshot.items() if v is not None}),
                       'scheduled:' + str(row['id']) + ':' + status))
            from .wake import EVENTS, mark
            mark(c, EVENTS)
    except Exception as error:  # noqa: BLE001 - an event never blocks the status
        log.warning('scheduled_event_failed exception_type=%s', type(error).__name__)


RETURNING = 'RETURNING id,account,chat,principal,key_id,status,schedule_at,scheduled_message_id,cancel_reason'


# Updates for IDs no scheduled row holds yet, by (account, chat, ID): Telegram can report a delete or move before the
# receipt commits (r1 M4). One lock orders the lookup-and-keep against the commit-and-replay.
_pending = {}
_pending_lock = threading.Lock()


# ANYM-929: scheduled-list IDs an agent's confirmed cancel_scheduled is deleting, by (account, chat, ID), so the pushed
# delete records the agent as the reason even when it lands before the cancel's receipt.
_agent_cancels = {}
AGENT_CANCEL_S = 300


def expect_agent_cancel(account, chat, scheduled_id):
    with _pending_lock:
        cutoff = time.monotonic() - AGENT_CANCEL_S
        for key in [key for key, at in _agent_cancels.items() if at < cutoff]:
            del _agent_cancels[key]
        if len(_agent_cancels) < PENDING_UPDATE_CAP:
            _agent_cancels[(account, str(chat), str(scheduled_id))] = time.monotonic()


def _cancel_reason(account, chat, scheduled_id):
    at = _agent_cancels.get((account, str(chat), str(scheduled_id)))
    return 'agent' if at is not None and at >= time.monotonic() - AGENT_CANCEL_S else 'owner_app'


def _keep(account, update, ids):
    cutoff = time.monotonic() - PENDING_UPDATE_S
    for key in [key for key, items in _pending.items() if items[-1][0] < cutoff]:
        del _pending[key]
    published = _sent_of(update) if update['kind'] == 'deleted' else {}
    for identifier in ids:
        key = (account, update['chat'], identifier)
        if len(_pending) >= PENDING_UPDATE_CAP and key not in _pending:
            return
        sent = published.get(identifier)
        part = update if update['kind'] == 'new' else {**update, 'ids': [identifier], 'sent': [sent] if sent else None}
        _pending.setdefault(key, []).append((time.monotonic(), part))


def finish_scheduled(state, kind, job, scheduled_id, at, result=None):
    """The provider accepted a scheduled send: the row rests in `scheduled` with Telegram's scheduled-list ID; an
    update that came before this commit is applied right after it."""
    table = TABLES[kind]
    at = datetime.fromisoformat(at) if isinstance(at, str) else at
    chat = str(job.get('provider_chat') or (result or {}).get('provider_chat_id') or '')
    with _pending_lock:
        with state.archive.store.pool.connection() as c:
            state.archive.fence(c, state.account, state.lease)
            extra, args = (',result=%s', (Jsonb(result),)) if kind == 'operation' else ('', ())
            rows = c.execute(f'''UPDATE {table} SET status='scheduled',scheduled_message_id=%s,schedule_at=%s,
                error_code=NULL{extra} WHERE id=%s AND status='dispatching' AND lease_epoch=%s {RETURNING}''',
                             (scheduled_id, at, *args, job['id'], state.lease[1])).fetchall()
            for row in rows:
                journal(c, table, row, 'scheduled')
        held = _pending.pop((state.account, chat, str(scheduled_id)), []) if rows else []
        for _, update in held:
            try:
                _apply(state, update)
            except Exception as error:  # noqa: BLE001 - the receipt is committed; the overdue check settles it
                log.warning('scheduled_replay_failed exception_type=%s', type(error).__name__)


def _settle(c, table, row, status, provider_id=None, reason=None, code=None):
    done = 'sent' if table == 'outgoing_messages' else 'succeeded'
    status = done if status == 'sent' else status
    if table == 'outgoing_messages':
        settled = c.execute(f'''UPDATE {table} SET status=%s,provider_message_id=coalesce(%s,provider_message_id),
            cancel_reason=%s,error_code=%s,finished_at=clock_timestamp() WHERE id=%s AND status='scheduled' {RETURNING}''',
                            (status, provider_id, reason, code, row['id'])).fetchall()
    else:
        published = {'outcome': 'confirmed', 'basis': 'provider_scheduled_published',
                     'provider_message_id': provider_id, 'provider_chat_id': row['provider_chat']} if provider_id else None
        settled = c.execute(f'''UPDATE {table} SET status=%s,result=coalesce(%s,result),cancel_reason=%s,error_code=%s,
            finished_at=clock_timestamp() WHERE id=%s AND status='scheduled' {RETURNING}''',
                            (status, Jsonb(published) if published else None, reason, code, row['id'])).fetchall()
    for item in settled:
        journal(c, table, item, 'sent' if status == done else status)
    return bool(settled)


def _rows(c, account, provider_chat, scheduled_ids):
    """The scheduled sends and operations of this chat with these scheduled-list IDs."""
    rows = []
    for table in TABLES.values():
        rows += [{**row, 'table': table} for row in c.execute(f'''SELECT t.*,ch.provider_chat AS provider_chat FROM {table} t
            JOIN chats ch ON ch.id=t.chat AND ch.account=t.account WHERE t.account=%s AND ch.provider_chat=%s
            AND t.status='scheduled' AND t.scheduled_message_id=ANY(%s) FOR UPDATE OF t''',
                                                                 (account, provider_chat, scheduled_ids)).fetchall()]
    return rows


def _sent_of(update):
    ids = [str(v) for v in update['ids']]
    sent = update.get('sent')
    return dict(zip(ids, [str(v) for v in sent])) if sent and len(sent) == len(ids) else {}


def apply_update(state, update):
    """Record one pushed scheduled-list change (criterion 5). `update` is plain data:
    {'kind': 'deleted', 'chat', 'ids': [...], 'sent': [...] or None} or
    {'kind': 'new', 'chat', 'id', 'date': datetime, 'text'}. An ID no scheduled row holds (the owner's own
    scheduled message, or a receipt not yet committed) is kept for PENDING_UPDATE_S and otherwise left alone.
    Returns how many rows changed."""
    with _pending_lock:
        changed, matched = _apply(state, update)
        ids = [str(v) for v in update['ids']] if update['kind'] == 'deleted' else [str(update['id'])]
        _keep(state.account, update, [identifier for identifier in ids if identifier not in matched])
    return changed


def _apply(state, update):
    changed, matched = 0, set()
    with state.archive.store.pool.connection() as c:
        state.archive.fence(c, state.account, state.lease)
        if update['kind'] == 'deleted':
            ids = [str(v) for v in update['ids']]
            published = _sent_of(update)
            for row in _rows(c, state.account, update['chat'], ids):
                matched.add(row['scheduled_message_id'])
                provider_id = published.get(row['scheduled_message_id'])
                changed += _settle(c, row['table'], row, 'sent', provider_id) if provider_id else \
                    _settle(c, row['table'], row, 'scheduled_cancelled',
                            reason=_cancel_reason(state.account, update['chat'], row['scheduled_message_id']))
            return changed, matched
        for row in _rows(c, state.account, update['chat'], [str(update['id'])]):
            matched.add(row['scheduled_message_id'])
            moved = update.get('date') is not None and update['date'] != row['schedule_at']
            edited = isinstance(update.get('text'), str) and update['text'] != _confirmed_text(row)
            if moved or (edited and not row['edited_in_app']):
                c.execute(f'''UPDATE {row['table']} SET schedule_at=%s,edited_in_app=edited_in_app OR %s
                    WHERE id=%s AND status='scheduled' ''', (update['date'] if moved else row['schedule_at'], edited, row['id']))
                changed += 1
    return changed, matched


def _confirmed_text(row):
    if row['table'] == 'outgoing_messages':
        return row['body']
    parameters = (row.get('payload') or {}).get('parameters') or {}
    return parameters.get('caption', '')


def due(state, now=None, limit=16):
    """This account's scheduled rows whose time has passed, oldest first; with provider_chat and table."""
    with state.archive.store.pool.connection() as c:
        rows = []
        for table in TABLES.values():
            rows += [{**row, 'table': table} for row in c.execute(f'''SELECT t.*,ch.provider_chat AS provider_chat
                FROM {table} t JOIN chats ch ON ch.id=t.chat AND ch.account=t.account
                WHERE t.account=%s AND t.status='scheduled' AND t.schedule_at<=%s ORDER BY t.schedule_at,t.id LIMIT %s''',
                                                                     (state.account, now or datetime.now(timezone.utc), limit)).fetchall()]
        return sorted(rows, key=lambda row: (row['schedule_at'], row['id']))[:limit]


ARCHIVE_SQL = """SELECT m.provider_id FROM messages m WHERE m.account=%s AND m.chat=%s AND m.direction='outgoing'
    AND NOT m.deleted AND m.body=%s AND m.sent_at>=%s
    AND NOT EXISTS(SELECT 1 FROM outgoing_messages o WHERE o.account=m.account AND o.chat=m.chat
                   AND o.provider_message_id=m.provider_id)
    AND NOT EXISTS(SELECT 1 FROM messenger_commands x WHERE x.account=m.account AND x.chat=m.chat
                   AND x.result->>'provider_message_id'=m.provider_id)
    ORDER BY m.sent_at,m.provider_id LIMIT 1"""


def archived(c, row):
    """Criterion 6: the published text message in the archive (outgoing, this chat, the confirmed text, from
    ARCHIVE_SLACK_S before the time on, not another send's); None for a file or when there is none yet."""
    if row['table'] != 'outgoing_messages':
        return None
    found = c.execute(ARCHIVE_SQL, (row['account'], row['chat'], row['body'],
                                    row['schedule_at'] - timedelta(seconds=ARCHIVE_SLACK_S))).fetchone()
    return found['provider_id'] if found else None


def reconcile(state, row, live_ids=None, now=None):
    """Settle one overdue row: `sent` through the archive; else, after the one live read (`live_ids`: the chat's
    scheduled-list IDs and their dates, or None before it), still `scheduled` when Telegram holds it, or
    `unknown` when neither can be shown. Never a resend."""
    now = now or datetime.now(timezone.utc)
    with state.archive.store.pool.connection() as c:
        state.archive.fence(c, state.account, state.lease)
        current = c.execute(f'''SELECT t.*,ch.provider_chat AS provider_chat FROM {row['table']} t JOIN chats ch
            ON ch.id=t.chat AND ch.account=t.account WHERE t.id=%s AND t.status='scheduled' FOR UPDATE OF t''',
                            (row['id'],)).fetchone()
        if not current:
            return 'settled'
        current = {**current, 'table': row['table']}
        provider_id = archived(c, current)
        if provider_id:
            _settle(c, row['table'], current, 'sent', provider_id)
            return 'sent'
        if live_ids is None:
            return 'pending'
        held = live_ids.get(current['scheduled_message_id'])
        if held is not None:
            # Telegram still holds it (the owner moved it, or publishing is late): keep it, with Telegram's time.
            c.execute(f'''UPDATE {row['table']} SET schedule_at=%s,schedule_checked_at=%s WHERE id=%s''',
                      (held, now, current['id']))
            return 'scheduled'
        c.execute(f'UPDATE {row["table"]} SET schedule_checked_at=%s WHERE id=%s', (now, current['id']))
        _settle(c, row['table'], current, 'unknown', code='scheduled_outcome_unknown')
        return 'unknown'


def needs_live_check(row, now=None):
    now = now or datetime.now(timezone.utc)
    return row.get('schedule_checked_at') is None and row['schedule_at'] <= now - timedelta(seconds=OVERDUE_S)


async def scheduled_list(provider, provider_chat):
    """One live GetScheduledHistoryRequest for the chat: {scheduled_message_id: date}. Paced as background work."""
    from telethon.tl import functions

    import asyncio

    async def read():
        peer = await provider.client.get_input_entity(int(provider_chat))
        return await provider.client(functions.messages.GetScheduledHistoryRequest(peer=peer, hash=0))

    async def bounded():  # inside the governed call, so a stalled read frees its lane
        return await asyncio.wait_for(read(), READ_TIMEOUT_S)
    answer = await provider.rpc(bounded, 'scheduled_check')
    return {str(message.id): message.date for message in getattr(answer, 'messages', []) if getattr(message, 'date', None)}


def reserve(state, row, now=None):
    """Mark the row's one live check as spent before the read, so a read that fails or never ends is not repeated."""
    with state.archive.store.pool.connection() as c:
        state.archive.fence(c, state.account, state.lease)
        c.execute(f'''UPDATE {row['table']} SET schedule_checked_at=%s WHERE id=%s AND status='scheduled'
            AND schedule_checked_at IS NULL''', (now or _now(), row['id']))


async def sweep(state, provider, now=None):
    """One pass: settle overdue rows through the archive, then one live read per chat for those past OVERDUE_S.
    A failed or timed-out read settles that chat's rows `unknown`; other chats go on."""
    import asyncio
    rows = await asyncio.to_thread(due, state, now)
    lists = {}
    outcomes = []
    for row in rows:
        live = None
        if needs_live_check(row, now):
            first = await asyncio.to_thread(reconcile, state, row, None, now)
            if first != 'pending':
                outcomes.append(first)
                continue
            chat = row['provider_chat']
            if chat not in lists:
                await asyncio.to_thread(reserve, state, row, now)
                try:
                    lists[chat] = await scheduled_list(provider, chat)
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - nothing shown: the row is uncertain, never resent
                    log.warning('scheduled_check_failed exception_type=%s', type(error).__name__)
                    lists[chat] = {}
            live = lists[chat]
        outcomes.append(await asyncio.to_thread(reconcile, state, row, live, now))
    return outcomes


def update_data(update):
    """Plain data of a Telethon scheduled-list update, or None for any other update."""
    from telethon import types, utils
    if isinstance(update, types.UpdateDeleteScheduledMessages):
        return {'kind': 'deleted', 'chat': str(utils.get_peer_id(update.peer)), 'ids': list(update.messages),
                'sent': list(update.sent_messages) if update.sent_messages else None}
    if isinstance(update, types.UpdateNewScheduledMessage) and getattr(update.message, 'peer_id', None) is not None:
        message = update.message
        date = message.date.astimezone(timezone.utc) if getattr(message, 'date', None) else None
        return {'kind': 'new', 'chat': str(utils.get_peer_id(message.peer_id)), 'id': message.id, 'date': date,
                'text': getattr(message, 'message', None)}
    return None


def handler(state):
    """The collector's raw-update handler; any failure is logged, never raised into the update loop."""
    import asyncio

    async def on_update(update):
        try:
            data = update_data(update)
            if data is not None:
                await asyncio.to_thread(apply_update, state, data)
        except Exception as error:  # noqa: BLE001 - the overdue check settles a missed update
            log.warning('scheduled_update_failed exception_type=%s', type(error).__name__)
    return on_update


async def loop(state, provider, interval=SWEEP_INTERVAL_S):
    """The collector's overdue check; a failed pass is logged and the next one runs."""
    import asyncio
    while True:
        try:
            await sweep(state, provider)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - never stops sending or collecting
            log.warning('scheduled_sweep_failed exception_type=%s', type(error).__name__)
        await asyncio.sleep(interval)
