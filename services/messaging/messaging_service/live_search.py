"""ANYM-478: search with live: the archive answer first, then one bounded wait for live Telegram searches.

The archive answer never depends on the live part: a failure to queue, a provider error or the end of the
wait only changes result_evidence.live. No database connection or lock is held while the call waits, and
its admission slots are its own (never the send waits of send_wait.py).
"""
import asyncio
import hashlib
import logging
import math
import re
import threading
import uuid

from .store import APIError


log = logging.getLogger(__name__)


MODES = ('off', 'auto', 'on')
DEFAULT_WAIT_S = 15
MAX_WAIT_S = 20
MAX_CONCURRENT_WAITS = 8
POLL_INTERVAL_S = 0.5
_WAIT_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_WAITS)
OPERATION = 'search_messages_live'
# empty_reason values of chat_evidence.query_evidence for an archive answer that may be missing messages.
THIN = frozenset(('history_loading', 'history_not_scanned', 'history_has_gaps', 'no_matches_in_partial_archive'))
HISTORY_STATES = frozenset(('gaps', 'loading', 'not_scanned'))
HISTORY_CHATS = 5
LIVE_CHAT_TYPES = ('private', 'group', 'channel')
PLAN_DEGRADED = frozenset(('query_timeout', 'service_busy', 'service_unavailable'))  # store.access 503 codes
SEARCH_SUGGESTION = {'tool': 'messages_search', 'args': {'live': 'on'}}


def validate(live, wait_s, request_id):
    """Checked before the archive search, so a bad value never queues anything."""
    if live not in MODES: raise APIError('invalid_filter')
    if wait_s is None: wait_s = DEFAULT_WAIT_S
    if (isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)) or not math.isfinite(wait_s)
            or not 0 <= wait_s <= MAX_WAIT_S):
        raise APIError('invalid_wait_s')
    if not isinstance(request_id, str) or request_id and not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,128}', request_id):
        raise APIError('invalid_request_id')
    return float(wait_s)


def accounts_in_scope(c, p, arguments):
    """Accounts the archive search can match: named chats, account/accounts and chat types ANDed as in filters()."""
    from .message_queries import CHAT_TYPES
    wanted = set(p['accounts'])
    if arguments.get('account'): wanted &= {arguments['account']}
    if arguments.get('accounts') is not None: wanted &= set(arguments['accounts'])
    chat = arguments.get('chat') or ''; chats = arguments.get('chats')
    named = [chat] if chat else chats or None  # chats=[] is refused as unsupported_filter, never an empty scope
    if named is not None:
        # A named chat skips the channel default, never an explicit chat_types or exclude_channels.
        types = arguments.get('chat_types')
        if arguments.get('exclude_channels'): types = [t for t in (CHAT_TYPES if types is None else types) if t != 'channel']
        typed = '' if types is None else " AND coalesce(ch.chat_type,'unknown')=ANY(%s)"
        return c.execute('''SELECT DISTINCT a.id,a.platform FROM chats ch JOIN accounts a ON a.id=ch.account
            WHERE ch.id=ANY(%s) AND a.id=ANY(%s)''' + typed + ' ORDER BY a.id',
            (list(named), sorted(wanted), *([list(types)] if types is not None else []))).fetchall()
    return c.execute('SELECT id,platform FROM accounts WHERE id=ANY(%s) ORDER BY id', (sorted(wanted),)).fetchall()


def unsupported(arguments):
    """Search fields the live Telegram search cannot apply; any of them keeps the call archive-only."""
    from .messenger_operations import timestamp
    fields = []
    query = arguments.get('query') or ''
    if not query.strip() or len(query) > 256: fields.append('query')
    if arguments.get('query_mode', 'all') not in ('all', 'phrase'): fields.append('query_mode')
    if arguments.get('fuzzy'): fields.append('fuzzy')
    if arguments.get('sender'): fields.append('sender')
    if arguments.get('senders') is not None: fields.append('senders')
    chats = arguments.get('chats')
    if chats is not None and (len(chats) != 1 or arguments.get('chat')): fields.append('chats')
    if arguments.get('direction'): fields.append('direction')
    for name in ('from_time', 'to_time'):
        if arguments.get(name) and timestamp(arguments[name]) is None: fields.append(name)
    if not fields and parameters(arguments)[1] is None: fields.append('chat_types')
    return fields


def parameters(arguments):
    """(chat_id, operation parameters); parameters is None when the chat types leave nothing to search."""
    chat = arguments.get('chat') or (arguments.get('chats') or [''])[0]
    result = {'query': arguments['query'], 'limit': min(max(arguments.get('limit', 20), 1), 50)}
    for name in ('from_time', 'to_time'):
        if arguments.get(name): result[name] = arguments[name]
    if not chat:
        # The archive's channel default: channels only when include_channels or chat_types names them.
        types = arguments.get('chat_types')
        if types is None and arguments.get('include_channels') is True: types = list(LIVE_CHAT_TYPES)
        if types is not None:
            types = [t for t in LIVE_CHAT_TYPES if t in types and not (t == 'channel' and arguments.get('exclude_channels'))]
            if not types: return chat, None
            result['chat_types'] = types
    return chat, result


def operation_request_id(request_id, account):
    """The same caller request_id gives the same operation per account, so a lost response repeats nothing."""
    return 'search-live:' + hashlib.sha256((request_id + '\0' + account).encode()).hexdigest()[:40]


def history_suggestion(c, p, account, arguments):
    """A WhatsApp account enters result_evidence.live only when chats in scope still load or have gaps."""
    from .chat_evidence import rows, history
    chat = arguments.get('chat') or ''; chats = arguments.get('chats')
    named = [chat] if chat else chats
    records = rows(c, p, named, [account], arguments.get('chat_types') if named is None else None)
    ids = [r['chat_id'] for r in records if history(r)['state'] in HISTORY_STATES][:HISTORY_CHATS]
    if not ids: return None
    return {'account': account, 'state': 'not_supported_by_provider', 'stored_new': 0, 'hits': [],
            'suggested_call': {'tool': 'messages_execute_messenger_operation',
                               'args': {'account': account, 'operation': 'request_chat_history'}, 'chat_ids': ids}}


def suggest(c, p, arguments, evidence):
    """live: off and a thin answer: name the call that may find more (S3.7). Existing fields stay unchanged."""
    if evidence.get('empty_reason') not in THIN: return
    scope = accounts_in_scope(c, p, arguments)
    if any(r['platform'] == 'telegram' for r in scope):
        evidence['suggested_call'] = SEARCH_SUGGESTION
        return
    for row in scope:
        if row['platform'] == 'whatsapp':
            entry = history_suggestion(c, p, row['id'], arguments)
            if entry:
                evidence['suggested_call'] = entry['suggested_call']
                return


def plan(c, p, arguments, *, paged, request_id):
    """Per account in scope: a final entry, or an operation to queue. Only reads; queues nothing."""
    entries, queue = [], []
    fields = unsupported(arguments)
    enabled = {r['account']: OPERATION in (r['operations_enabled'] or []) and r['enabled'] for r in c.execute(
        'SELECT account,enabled,operations_enabled FROM account_policies WHERE account=ANY(%s)',
        (p['accounts'],)).fetchall()}
    for row in accounts_in_scope(c, p, arguments):
        account = row['id']
        if row['platform'] == 'whatsapp':
            entry = history_suggestion(c, p, account, arguments)
            if entry: entries.append(entry)
            continue
        if row['platform'] != 'telegram': continue
        base = {'account': account, 'stored_new': 0, 'hits': []}
        if paged: entries.append({**base, 'state': 'first_page_only'})
        elif 'read-live' not in p['permissions']: entries.append({**base, 'state': 'not_permitted', 'reason': 'key_permission'})
        elif fields: entries.append({**base, 'state': 'unsupported_filter', 'fields': fields})
        elif not enabled.get(account): entries.append({**base, 'state': 'not_enabled'})
        else:
            chat, values = parameters(arguments)
            entries.append({**base, 'state': 'pending'})
            queue.append({'index': len(entries) - 1, 'account': account, 'chat_id': chat, 'parameters': values,
                          'request_id': operation_request_id(request_id, account)})
    return entries, queue


def from_receipt(entry, receipt):
    """Map one operation receipt to its live entry; a non-final receipt stays pending with its operation_id."""
    data = (receipt.get('result') or {}).get('data')
    data = data if isinstance(data, dict) else {}
    result = {'account': entry['account'], 'operation_id': receipt['operation_id'], 'stored_new': 0, 'hits': []}
    status = receipt['status']
    if status == 'succeeded':
        result.update(state='done', stored_new=data.get('stored_new', 0), hits=data.get('hits') or [],
                      next_cursor=data.get('next_cursor'), provider_total_approx=data.get('provider_total_approx'))
    elif status in ('queued', 'dispatching'): result['state'] = 'pending'
    elif receipt.get('error_code') == 'rate_limited':
        result.update(state='rate_limited', retry_after=data.get('retry_after'))
    else: result.update(state='unavailable', reason=receipt.get('error_code') or status)
    return result


def queue_failure(entry, error):
    """A refusal to queue is an entry state; the archive answer is unaffected."""
    code = error.code if isinstance(error, APIError) else 'operation_queue_failed'
    result = {'account': entry['account'], 'stored_new': 0, 'hits': []}
    if code == 'operation_disabled': return {**result, 'state': 'not_enabled'}
    if code in ('forbidden', 'not_found'): return {**result, 'state': 'not_permitted', 'reason': code}
    if code == 'rate_limited': return {**result, 'state': 'rate_limited', 'retry_after': 60}
    return {**result, 'state': 'unavailable', 'reason': code}


class _Admission:
    """A slot is held until the in-flight receipt read returns, even after the caller has gone."""
    def __init__(self):
        self.acquired = _WAIT_SLOTS.acquire(blocking=False)
        self.lock = threading.Lock(); self.closed = False; self.reading = False

    def read(self, store, token, ids):
        with self.lock:
            if self.closed: return None
            self.reading = True
        try: return store.live_search_receipts(token, ids)
        finally:
            with self.lock:
                self.reading = False
                if self.closed: _WAIT_SLOTS.release()

    def close(self):
        if not self.acquired: return
        with self.lock:
            self.closed = True
            if not self.reading: _WAIT_SLOTS.release()


def _consume(task):
    if not task.cancelled(): task.exception()


async def wait(store, token, ids, wait_s):
    """Receipts by operation_id after one wait under one deadline; ({}, 'busy') when no slot is free."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    admission = _Admission()
    if not admission.acquired: return {}, 'busy'
    latest = {}
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0: return latest, 'timed_out'
            task = asyncio.create_task(asyncio.to_thread(admission.read, store, token, ids))
            task.add_done_callback(_consume)
            try: latest = await asyncio.wait_for(asyncio.shield(task), remaining) or latest
            except TimeoutError: return latest, 'timed_out'
            if all(latest.get(i, {}).get('status') not in ('queued', 'dispatching') for i in ids):
                return latest, 'done'
            remaining = deadline - loop.time()
            if remaining <= 0: return latest, 'timed_out'
            await asyncio.sleep(min(POLL_INTERVAL_S, remaining))
    finally: admission.close()


async def search(store, token, arguments, *, live='off', wait_s=None, request_id=''):
    """The archive answer, plus result_evidence.live when live is auto or on (ANYM-478)."""
    wait_s = validate(live, wait_s, request_id)
    run = asyncio.to_thread
    first = await run(store.search, token, **arguments)
    if live == 'off': return first
    evidence = first['result_evidence']; evidence.pop('suggested_call', None)
    paged = bool(arguments.get('after') or arguments.get('before'))
    # A continuation page goes on to the plan, which says first_page_only (S3.1).
    if live == 'auto' and not paged and evidence.get('empty_reason') not in THIN:
        evidence['live'] = []
        return first
    try:
        entries, queue = await run(store.live_search_plan, token, arguments, paged=paged,
                                   request_id=request_id or uuid.uuid4().hex)
    except Exception as error:
        # Authorization and argument errors stay the caller's; a database failure only costs the live part.
        if isinstance(error, APIError) and error.code not in PLAN_DEGRADED: raise
        log.warning('live_search_plan_failed exception_type=%s', type(error).__name__)
        evidence['live'] = []; evidence['live_wait'] = 'failed'
        return first
    pending = []
    for item in queue:
        try:
            receipt = await run(store.execute_messenger_operation, token, item['account'], OPERATION,
                                item['chat_id'], parameters=item['parameters'], request_id=item['request_id'])
        except Exception as error:
            entries[item['index']] = queue_failure(entries[item['index']], error)
            continue
        entries[item['index']] = from_receipt(entries[item['index']], receipt)
        if entries[item['index']]['state'] == 'pending': pending.append(item['index'])
    waited = None
    if pending and wait_s:
        ids = [entries[i]['operation_id'] for i in pending]
        try: receipts, waited = await wait(store, token, ids, wait_s)
        except Exception: receipts, waited = {}, 'failed'
        for i in pending:
            receipt = receipts.get(entries[i]['operation_id'])
            if receipt: entries[i] = from_receipt(entries[i], receipt)
    # The archive is read again with the caller's current authorization; never a cached page.
    try: second = await run(store.search, token, **arguments)
    except Exception as error:
        # A revoked key still fails the call; a slow or failed reread keeps the first archive answer.
        if not isinstance(error, APIError) or error.code not in PLAN_DEGRADED: raise
        log.warning('live_search_reread_failed code=%s', error.code)
        second = first; evidence['archive_reread'] = 'failed'
    evidence = second['result_evidence']; evidence.pop('suggested_call', None)
    evidence['live'] = entries
    if waited: evidence['live_wait'] = waited
    return second
