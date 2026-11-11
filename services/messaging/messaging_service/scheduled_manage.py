"""ANYM-929: an agent lists the Telegram messages waiting to be sent and cancels one DeedKit scheduled, after the
owner's confirmation.

list_scheduled without chat_id answers from the archive: DeedKit's own `scheduled` sends and send_file operations in
the chats the key may read, no provider request. With chat_id it adds one live GetScheduledHistoryRequest of that chat
through the governor and marks each waiting message made_by_deedkit or not.

cancel_scheduled names one scheduled-list ID. The preview binds DeedKit's send of that ID (a scheduled message the
owner made in the app is not_scheduled_here); the collector reads the message once (GetScheduledMessagesRequest) and
deletes it (DeleteScheduledMessagesRequest). The send then reads scheduled_cancelled (cancel_reason agent); one already
published is already_sent with its message ID; one the owner already deleted settles scheduled_cancelled (owner_app),
not an error. A delete without Telegram's confirmation, or a timeout, is unknown and never retried.
"""
import asyncio
import re

LIST, CANCEL = 'list_scheduled', 'cancel_scheduled'
OPERATIONS = (LIST, CANCEL)
TEXT_CHARS = 200
LIST_MAX = 100
SCHEDULED_ID = re.compile(r'[1-9][0-9]{0,9}')
ID_FORMAT = ('Telegram scheduled-list message ID, a decimal string of 1-10 digits: scheduled_message_id from '
             'list_scheduled or from the scheduled send')
REASON = 'the send layer\'s own scheduled sends (ANYM-929): reading or deleting a message waiting to be sent'
LIST_LIMITATION = ('Telegram only. Without chat_id: DeedKit\'s own scheduled sends and files in the chats this key may '
    'read, from the archive with no provider request. With chat_id: one live read of that chat\'s Scheduled messages, '
    'including those the owner made in the app. Each item: scheduled_message_id, schedule_at, text (first 200 '
    'characters), made_by_deedkit and, for DeedKit\'s own, send_id or operation_id. At most 100 items. A flood wait is '
    'rate_limited with retry_after.')
CANCEL_LIMITATION = ('Telegram only: deletes one message DeedKit scheduled (scheduled_message_id from list_scheduled) '
    'from the owner\'s Scheduled messages after the owner\'s exact confirmation; the preview shows its text and time and '
    'binds that send. A scheduled message the owner made in the app is not_scheduled_here: the owner cancels it there. '
    'The result state is scheduled_cancelled (cancel_reason agent, or owner_app when the owner had already deleted it) '
    'or already_sent with provider_message_id when Telegram had published it. A timeout is unknown and never retried.')
TABLES = {'send': 'outgoing_messages', 'operation': 'messenger_commands'}
HANDLE = {'send': 'send_id', 'operation': 'operation_id'}
DONE = ('sent', 'succeeded')

# DeedKit's scheduled sends and send_file operations, newest per kind first; `chat` and `ids` narrow it.
ROWS_SQL = """SELECT * FROM (
    SELECT 'send' AS kind,o.id,o.chat,ch.provider_chat,o.status,o.scheduled_message_id,o.schedule_at,o.body AS text,
           o.edited_in_app,o.cancel_reason,o.provider_message_id,o.created_at
      FROM outgoing_messages o JOIN chats ch ON ch.id=o.chat AND ch.account=o.account
     WHERE o.account=%(account)s AND o.scheduled_message_id IS NOT NULL
    UNION ALL
    SELECT 'operation',x.id,x.chat,ch.provider_chat,x.status,x.scheduled_message_id,x.schedule_at,
           coalesce(x.payload->'parameters'->>'caption',''),x.edited_in_app,x.cancel_reason,x.result->>'provider_message_id',
           x.created_at
      FROM messenger_commands x JOIN chats ch ON ch.id=x.chat AND ch.account=x.account
     WHERE x.account=%(account)s AND x.operation='send_file' AND x.scheduled_message_id IS NOT NULL) r
  WHERE (%(chat)s::text IS NULL OR r.chat=%(chat)s) AND (%(ids)s::text[] IS NULL OR r.scheduled_message_id=ANY(%(ids)s))
    AND (%(status)s::text IS NULL OR r.status=%(status)s) AND (%(scope)s::text[] IS NULL OR r.chat=ANY(%(scope)s))
  ORDER BY r.schedule_at,r.created_at DESC,r.id LIMIT %(limit)s"""


def rows(c, account, *, chat=None, ids=None, status=None, scope=None, limit=LIST_MAX):
    return c.execute(ROWS_SQL, {'account': account, 'chat': chat, 'ids': ids, 'status': status, 'scope': scope,
                                'limit': limit}).fetchall()


def item(row, made_by_deedkit=True):
    """One listed message: IDs, time and the first TEXT_CHARS characters."""
    at = row['schedule_at']
    value = {'scheduled_message_id': row['scheduled_message_id'],
             'schedule_at': at.isoformat() if hasattr(at, 'isoformat') else at,
             'text': (row.get('text') or '')[:TEXT_CHARS], 'made_by_deedkit': made_by_deedkit}
    if made_by_deedkit:
        value.update({HANDLE[row['kind']]: row['id'], 'chat_id': row['chat'], 'status': row['status'],
                      'edited_in_app': row.get('edited_in_app') is True})
    return value


# --- Preview and enqueue: the API process binds the send a cancel deletes. ---

def _already_sent(row):
    from .store import APIError
    failure = APIError('already_sent', 409)
    failure.state = 'already_sent'
    failure.next_step = 'nothing to cancel: Telegram published it as provider message %s' % row['provider_message_id'] \
        if row.get('provider_message_id') else 'nothing to cancel: Telegram already published it'
    return failure


def bind(c, p, account, chat, operation, parameters, payload, saved):
    """cancel_scheduled: the payload (and so the preview digest) binds DeedKit's send of this scheduled-list ID."""
    if operation != CANCEL:
        return
    if saved is not None:
        payload['scheduled_send'] = saved.get('scheduled_send')
        return
    found = rows(c, account, chat=chat['id'], ids=[parameters['scheduled_message_id']], limit=1)
    if not found:
        from .store import APIError
        failure = APIError('not_scheduled_here', 409)
        failure.next_step = 'the owner made this scheduled message in the app; the owner cancels it there'
        raise failure
    row = found[0]
    if row['status'] in DONE:
        raise _already_sent(row)
    payload['scheduled_send'] = {'kind': row['kind'], 'id': row['id'], 'scheduled_message_id': row['scheduled_message_id']}


def shown(c, account, payload):
    """What the owner confirms beside the digest: the message's text (first 200 characters), time and state."""
    bound = payload.get('scheduled_send') or {}
    found = [row for row in rows(c, account, chat=payload['chat_id'], ids=[bound.get('scheduled_message_id')])
             if row['id'] == bound.get('id')]
    return {'scheduled': item(found[0])} if found else {}


# --- Provider side: telegram_operations handlers inside the guarded _execute. ---

def target_id(job):
    value = (job.get('parameters') or {}).get('scheduled_message_id')
    if not isinstance(value, str) or not SCHEDULED_ID.fullmatch(value) or int(value) > 2**31 - 1:
        from .commands import CommandRejected
        raise CommandRejected('invalid_parameters')
    return int(value)


async def _limited(call):
    """A flood wait before anything changed is rate_limited with retry_after; nothing was sent."""
    from telethon.errors import FloodWaitError
    from .telegram_governor import ProviderRateLimited
    from .commands import CommandRejected
    try:
        return await call
    except (FloodWaitError, ProviderRateLimited) as exc:
        raise CommandRejected('rate_limited', retry_after=exc.seconds) from None


def live_messages(answer):
    from telethon import types
    found = []
    for message in getattr(answer, 'messages', [])[:LIST_MAX]:
        if isinstance(message, types.Message) and getattr(message, 'date', None) is not None and message.id > 0:
            found.append({'scheduled_message_id': str(message.id), 'schedule_at': message.date.isoformat(),
                          'text': (message.message or '')[:TEXT_CHARS]})
    return found


async def read_scheduled(op):
    """list_scheduled with chat_id: one GetScheduledHistoryRequest of the chat."""
    from telethon.tl import functions
    answer = await _limited(op.invoke(functions.messages.GetScheduledHistoryRequest(peer=op.peer, hash=0)))
    return {'outcome': 'confirmed', 'basis': 'provider_scheduled_list', 'messages': live_messages(answer)}


def cancel_answer(answer, target, chat):
    """The delete's result: deleted, or already_sent when Telegram reports it published; else unknown."""
    from telethon import types, utils
    for update in getattr(answer, 'updates', [])[:100]:
        if (not isinstance(update, types.UpdateDeleteScheduledMessages) or target not in update.messages or
                str(utils.get_peer_id(update.peer)) != str(chat)):
            continue
        sent = update.sent_messages or []
        if len(sent) == len(update.messages):
            return {'outcome': 'confirmed', 'basis': 'provider_scheduled_published', 'state': 'already_sent',
                    'provider_message_id': str(sent[update.messages.index(target)]), 'provider_chat_id': str(chat)}
        return {'outcome': 'confirmed', 'basis': 'provider_scheduled_deleted', 'state': 'deleted',
                'provider_chat_id': str(chat)}
    return {'outcome': 'unknown', 'error': 'provider_result_unverified'}


async def write_cancel_scheduled(op):
    """cancel_scheduled: read the scheduled message once, then delete it once; never retried."""
    from telethon import types
    from telethon.tl import functions
    target = target_id(op.job)
    found = await _limited(op.invoke(functions.messages.GetScheduledMessagesRequest(peer=op.peer, id=[target])))
    if not any(isinstance(m, types.Message) and m.id == target for m in getattr(found, 'messages', [])):
        return {'outcome': 'confirmed', 'basis': 'provider_scheduled_not_found', 'state': 'not_found'}
    answer = await op.invoke(functions.messages.DeleteScheduledMessagesRequest(peer=op.peer, id=[target]), mutation=True)
    return cancel_answer(answer, target, op.chat)


HANDLERS = {LIST: read_scheduled, CANCEL: write_cancel_scheduled}


# --- Collector side: archive reads and the cancelled send's settlement, in the account's fenced collector. ---

class ScheduledCollector:
    """TelegramCollector's list_scheduled and cancel_scheduled."""

    async def scheduled_operation(self, job):
        if job.get('operation') == LIST:
            live = None
            if job.get('provider_chat'):
                answer = await self.provider.execute_operation(job)
                live = (answer.get('data') or {}).get('messages')
                if answer.get('outcome') != 'confirmed' or not isinstance(live, list):
                    return answer
            return await asyncio.to_thread(self.scheduled_list, job, live)
        return await self.scheduled_cancel(job)

    def _scope(self, c, job):
        from .commands import CommandRejected
        policy = job.get('_policy')
        if not isinstance(policy, dict) or policy.get('enabled') is False or job.get('account') != self.state.account:
            raise CommandRejected('operation_access_changed')
        key = c.execute('SELECT chat_ids FROM principals WHERE id=%s AND enabled', (job.get('principal'),)).fetchone()
        if not key:
            raise CommandRejected('operation_access_changed')
        return policy, key['chat_ids']

    def scheduled_list(self, job, live):
        """DeedKit's scheduled sends from the archive; with `live` (the chat's list) every waiting message."""
        with self.state.archive.store.pool.connection() as c:
            policy, scope = self._scope(c, job)
            if live is None:
                own = [row for row in rows(c, self.state.account, status='scheduled', scope=scope)
                       if self.state.allows(policy, row['provider_chat'])]
                return {'outcome': 'confirmed', 'data': {'source': 'archive', 'scheduled': [item(row) for row in own]}}
            ours = {}
            for row in rows(c, self.state.account, chat=job.get('chat_id'),
                            ids=[m['scheduled_message_id'] for m in live]):
                ours.setdefault(row['scheduled_message_id'], row)
        listed = []
        for message in live:
            row = ours.get(message['scheduled_message_id'])
            listed.append({**item(row), **message} if row else {**message, 'made_by_deedkit': False})
        return {'outcome': 'confirmed', 'data': {'source': 'provider', 'scheduled': listed}}

    def scheduled_target(self, job):
        bound = job.get('scheduled_send') or {}
        with self.state.archive.store.pool.connection() as c:
            self._scope(c, job)
            found = [row for row in rows(c, self.state.account, chat=job.get('chat_id'),
                                         ids=[bound.get('scheduled_message_id')]) if row['id'] == bound.get('id')]
        if not found or found[0]['kind'] != bound.get('kind'):
            from .commands import CommandRejected
            raise CommandRejected('operation_target_changed')
        return found[0]

    def scheduled_settle(self, row, answer):
        """Record what Telegram reported for the bound send; returns the cancel's public result."""
        from .scheduled_send import _settle, archived
        state = answer.get('state')
        provider_id = answer.get('provider_message_id')
        with self.state.archive.store.pool.connection() as c:
            self.state.archive.fence(c, self.state.account, self.state.lease)
            table = TABLES[row['kind']]
            current = c.execute(f'''SELECT t.*,ch.provider_chat AS provider_chat FROM {table} t JOIN chats ch
                ON ch.id=t.chat AND ch.account=t.account WHERE t.id=%s FOR UPDATE OF t''', (row['id'],)).fetchone()
            current = {**current, 'table': table}
            if state == 'not_found':
                # The owner deleted it in the app, unless the archive shows it published.
                provider_id = archived(c, current) if current['status'] == 'scheduled' else None
                state = 'already_sent' if provider_id else 'owner_app'
            if state == 'already_sent':
                _settle(c, table, current, 'sent', provider_id)
            else:
                _settle(c, table, current, 'scheduled_cancelled', reason='owner_app' if state == 'owner_app' else 'agent')
            final = c.execute(f'SELECT status,cancel_reason FROM {table} WHERE id=%s', (row['id'],)).fetchone()
        return self.cancel_result(row, final['status'], final['cancel_reason'], provider_id)

    @staticmethod
    def cancel_result(row, status, reason, provider_id=None):
        state = 'already_sent' if status in DONE else 'scheduled_cancelled' if status == 'scheduled_cancelled' else status
        data = {'state': state, HANDLE[row['kind']]: row['id'], 'scheduled_message_id': row['scheduled_message_id']}
        if state == 'scheduled_cancelled':
            data['cancel_reason'] = reason
        if state == 'already_sent' and (provider_id or row.get('provider_message_id')):
            data['provider_message_id'] = str(provider_id or row['provider_message_id'])
        answer = {'outcome': 'confirmed', 'data': data}
        if 'provider_message_id' in data:
            answer['provider_message_id'] = data['provider_message_id']
        return answer

    async def scheduled_cancel(self, job):
        from .commands import CommandRejected
        from .scheduled_send import expect_agent_cancel
        row = await asyncio.to_thread(self.scheduled_target, job)
        if row['status'] in DONE or row['status'] == 'scheduled_cancelled':
            return self.cancel_result(row, row['status'], row['cancel_reason'])
        if row['status'] != 'scheduled':
            raise CommandRejected('operation_target_changed')
        # The pushed delete update may land before this receipt: it then records the agent as the reason too.
        expect_agent_cancel(self.state.account, row['provider_chat'], row['scheduled_message_id'])
        answer = await self.provider.execute_operation(job)
        data = answer.get('data') if isinstance(answer.get('data'), dict) else {}
        if answer.get('outcome') != 'confirmed' or data.get('state') not in ('deleted', 'already_sent', 'not_found'):
            return answer
        return await asyncio.to_thread(self.scheduled_settle, row,
                                       {**data, 'provider_message_id': answer.get('provider_message_id')})
