"""Explicit credential-bound subscription pages and independent compare-and-swap ACKs."""
from __future__ import annotations
import asyncio
import hashlib
import json
import secrets
import time
import threading
from psycopg.types.json import Jsonb
from .subscriptions import _owned, dynamic, get_subscription_events

_WAIT_SLOTS = threading.BoundedSemaphore(32)
_IMMEDIATE_READ_TIMEOUT_S = 5
_RECEIPT_LIMIT = 128
# ANYM-827: an expired receipt is kept this long after its expiry, so positions can still name it; it never ACKs.
_EXPIRED_RECEIPT_KEPT = '1 hour'


class _Admission:
    def __init__(self):
        from .store import APIError
        self.semaphore = _WAIT_SLOTS
        if not self.semaphore.acquire(blocking=False):
            raise APIError('service_busy', 503)
        self.lock = threading.Lock()
        self.closed = False
        self.reading = False

    def read(self, store, token, subscription_id, limit):
        """(page, the scope version it was read under)."""
        with self.lock:
            if self.closed:
                return None
            self.reading = True
        try:
            READ_SCOPE.version = None
            page = store.fetch_subscription_events(token, subscription_id, limit=limit)
            return page, READ_SCOPE.version
        finally:
            with self.lock:
                self.reading = False
                if self.closed:
                    self.semaphore.release()

    def close(self):
        with self.lock:
            self.closed = True
            if not self.reading:
                self.semaphore.release()


def _consume_result(task):
    if not task.cancelled():
        task.exception()


def wake_reason(item, selector=None):
    """Only immutable native evidence, never words in a message body."""
    if item.get('historical'):
        return ['historical_event']
    reasons = []
    arrival = item.get('kind') == 'message.created' and item.get('direction') == 'incoming'
    if arrival:
        reasons.append('incoming_message')
    metadata = item.get('metadata') or {}
    if arrival and metadata.get('mentions_owner') is True:
        reasons.append('owner_mention')
    proof = item.get('reply_evidence') or {}
    if arrival and (metadata.get('reply_to_owner') is True or
            proof.get('basis') == 'exact_native_reply_to_archived_outgoing'):
        reasons.append('reply_to_owner')
    anchor = (selector or {}).get('reply_anchor')
    if (arrival and anchor and item.get('account') == anchor['account'] and
            item.get('chat') == anchor['chat'] and
            (metadata.get('reply_to_provider_id') == anchor['provider_id'] or
                anchor.get('native_reply_id') is not None and
                metadata.get('reply_to_owner') is not False and
                metadata.get('reply_to_provider_id') == anchor['native_reply_id'])):
        reasons.append('reply_to_confirmed_send')
    # ANYM-522: the matcher's evidence that this arrival replies to any confirmed send of the key.
    if arrival and item.get('reply_to_own_send') is True:
        reasons.append('reply_to_own_send')
    if item.get('kind', '').startswith(('attachment.', 'file.')):
        reasons.append('processing_state_changed')
    return reasons or ['selected_event']


# ANYM-826: the metadata keys the server itself reads (wake_reason above; subscriptions._dynamic_match and
# _own_send_replies): a compact item keeps exactly these, so routing never depends on an omitted key. reply_evidence
# is filter evidence and stays whole.
COMPACT_METADATA = ('mentions_owner', 'reply_to_owner', 'reply_to_provider_id', 'sender_is_bot')
COMPACT_SUBSCRIPTION = ('subscription_id', 'state', 'version', 'label', 'matched_through', 'matcher_caught_up',
    'acked_through', 'blocked_reason', 'gap', 'scope_masked')
COMPACT_ITEM = ('event_id', 'kind', 'origin', 'revision', 'observed_at', 'matched_at', 'delivered_at', 'historical',
    'wake_reason', 'message', 'account', 'chat', 'sender', 'provider_id', 'sent_at', 'direction', 'deleted',
    'processing', 'attachments', 'send_id', 'error_code', 'status')
PAGE_INCLUDE = ('subscription', 'delivery', 'metadata', 'reply_evidence')


def page_detail(detail, include):
    """ANYM-826: validated (detail, include); None means the default full page."""
    from .store import APIError
    detail = 'full' if detail is None else detail
    if detail not in ('full', 'compact') or include is not None and (
            not isinstance(include, list) or any(not isinstance(name, str) or name not in PAGE_INCLUDE
                for name in include)):
        raise APIError('invalid_parameters')
    return detail, frozenset(include or ())


def _compact_item(item, include):
    view = {key: item[key] for key in COMPACT_ITEM if key in item}
    metadata = item.get('metadata')
    if 'metadata' in include:
        if metadata is not None:
            view['metadata'] = metadata
    elif isinstance(metadata, dict):
        kept = {key: metadata[key] for key in COMPACT_METADATA if key in metadata}
        origin = metadata.get('forward_origin')
        if isinstance(origin, dict) and 'kind' in origin:
            kept['forward_origin'] = {'kind': origin['kind']}
        if kept:
            view['metadata'] = kept
    if 'reply_evidence' in item:
        view['reply_evidence'] = item['reply_evidence']
    return view


def _compact_subscription(state, include):
    if 'subscription' in include:
        return state
    view = {key: state[key] for key in COMPACT_SUBSCRIPTION if key in state}
    delivery = state.get('delivery')
    if 'delivery' in include and delivery is not None:
        view['delivery'] = delivery
    elif isinstance(delivery, dict) and isinstance(delivery.get('delivery_health'), dict):
        # ANYM-520: a compact poller with a bound endpoint still sees that its endpoint is failing.
        view['delivery_health'] = {'state': delivery['delivery_health'].get('state')}
    return view


def project_page(page, detail='full', include=frozenset()):
    """ANYM-826: a compact page drops repeated definition, delivery and metadata blocks; cursors, receipt and
    page-level fields stay as they are, so a compact fetch ACKs exactly what a full one does."""
    if detail != 'compact':
        return page
    view = dict(page)
    if isinstance(page.get('subscription'), dict):
        view['subscription'] = _compact_subscription(page['subscription'], include)
    if isinstance(page.get('items'), list):
        view['items'] = [_compact_item(item, include) for item in page['items']]
    return view


def server_positions(c, row, acked_through):
    """ANYM-827: where the server stands for this definition: ACKed, fetched (the highest retained receipt) and
    the receipts still open or expired without an ACK. Numbers and one time only; no receipt or hash."""
    # One instant for every filter, so a receipt at its expiry is either open or expired, never both.
    found = c.execute('''WITH t AS (SELECT clock_timestamp() AS now) SELECT max(r.through_position) AS fetched,
        count(*) FILTER (WHERE r.expires_at>t.now AND r.ack_result IS NULL) AS open,
        min(r.expires_at) FILTER (WHERE r.expires_at>t.now AND r.ack_result IS NULL) AS oldest,
        count(*) FILTER (WHERE r.expires_at<=t.now AND r.ack_result IS NULL) AS expired
        FROM subscription_receipts r CROSS JOIN t WHERE r.subscription=%s''', (row['id'],)).fetchone()
    return {'acked_through': acked_through, 'fetched_through': max(acked_through, found['fetched'] or acked_through),
        'open_receipts': found['open'], 'oldest_open_receipt_expires_at':
            found['oldest'].isoformat() if found['oldest'] else None, 'expired_unacked': found['expired']}


def _active(row):
    from .store import APIError
    if row['state'] == 'access_changed':
        raise APIError('subscription_access_changed', 409)
    if row['state'] != 'active':
        raise APIError('subscription_closed', 409)


def fetch_subscription_events(store, c, p, subscription_id, *, limit=50):
    from .store import bounded
    bounded(limit)
    from .module_links import fence_use
    fence_use(c)  # ANYM-577: a calendar access change waits for this fetch, or this fetch reads the change
    row = _owned(c, p, subscription_id)
    _active(row)
    c.execute('''INSERT INTO subscription_consumption(subscription,acked_through)
        VALUES(%s,%s) ON CONFLICT DO NOTHING''', (row['id'], row['start_position']))
    consumption = c.execute('SELECT * FROM subscription_consumption WHERE subscription=%s FOR UPDATE',
        (row['id'],)).fetchone()
    from . import module_events
    module = module_events.stream(c, row['id'], lock=True)
    page = get_subscription_events(store, c, p, subscription_id, after=consumption['acked_through'], limit=limit,
        module_after=module['acked_through'] if module else None, with_positions=False)
    if module is not None:
        page['module'].update(acked_through=module['acked_through'])
    page.update(consumption='explicit_subscription_ack', ack_version=consumption['version'], receipt=None,
        acked_through=consumption['acked_through'], receipt_expires_at=None,
        ack_available='subscription-ack' in p['permissions'])
    # ANYM-522: a page of a dynamic definition can pass hidden references with no item to return.
    moved = (module is not None and page['module']['next_after'] != module['acked_through'] or
        dynamic(row) and page['next_after'] != consumption['acked_through'])
    if not page['items'] and not moved or 'subscription-ack' not in p['permissions']:
        page['positions'] = server_positions(c, row, consumption['acked_through'])
        return page
    c.execute('''DELETE FROM subscription_receipts WHERE subscription=%s
        AND expires_at<=clock_timestamp()-%s::interval''', (row['id'], _EXPIRED_RECEIPT_KEPT))
    # Repeated unacknowledged polls must not lock the consumer out: the newest page always
    # gets a receipt, and the oldest outstanding receipts are evicted to stay within the cap.
    c.execute('''DELETE FROM subscription_receipts WHERE receipt_hash IN (
        SELECT receipt_hash FROM subscription_receipts WHERE subscription=%s
        ORDER BY expires_at DESC, receipt_hash OFFSET %s)''', (row['id'], _RECEIPT_LIMIT - 1))
    receipt = secrets.token_urlsafe(32)
    issued = c.execute('''INSERT INTO subscription_receipts(receipt_hash,subscription,principal,key_id,
        scope_version,key_permission_version,subscription_version,consumption_version,
        after_position,through_position''' + (',module_after_position,module_through_position' if module else '') +
        ''') VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s''' + (',%s,%s' if module else '') + ') RETURNING expires_at',
        (hashlib.sha256(receipt.encode()).hexdigest(), row['id'], p['id'], p['key_id'],
         p['scope_version'], p['key_permission_version'], row['version'], consumption['version'],
         consumption['acked_through'], page['next_after'],
         *((module['acked_through'], page['module']['next_after']) if module else ()))).fetchone()
    page.update(receipt=receipt, receipt_expires_at=issued['expires_at'].isoformat(),
        positions=server_positions(c, row, consumption['acked_through']))
    return page


def ack_subscription_events(store, c, p, subscription_id, receipt, version):
    from .store import APIError
    if 'read' not in p['permissions']:
        raise APIError('forbidden', 403)
    if type(version) is not int or not 1 <= version <= 2**53 - 1:
        raise APIError('invalid_version')
    from .module_links import fence_use
    fence_use(c)  # ANYM-577: the ACK's commit is ordered against a calendar access change
    row = _owned(c, p, subscription_id)
    _active(row)
    if not isinstance(receipt, str) or not 32 <= len(receipt) <= 128:
        raise APIError('invalid_receipt')
    issued = c.execute('''SELECT * FROM subscription_receipts WHERE receipt_hash=%s AND subscription=%s
        AND principal=%s AND key_id=%s AND expires_at>clock_timestamp() FOR UPDATE''',
        (hashlib.sha256(receipt.encode()).hexdigest(), row['id'], p['id'], p['key_id'])).fetchone()
    if issued is None:
        raise APIError('invalid_receipt')
    if (issued['scope_version'] != p['scope_version'] or
            issued['key_permission_version'] != p['key_permission_version'] or
            issued['subscription_version'] != row['version'] or issued['consumption_version'] != version):
        raise APIError('stale_ack', 409)
    if issued['ack_result'] is not None:
        return issued['ack_result']
    updated = c.execute('''UPDATE subscription_consumption SET acked_through=%s,version=version+1,
        updated_at=clock_timestamp() WHERE subscription=%s AND version=%s AND acked_through=%s
        RETURNING acked_through,version''', (issued['through_position'], row['id'], version,
        issued['after_position'])).fetchone()
    if updated is None:
        raise APIError('stale_ack', 409)
    result = {'subscription_id': str(row['id']), 'acked_through': updated['acked_through'],
        'ack_version': updated['version'], 'provider_read_state_changed': False}
    from . import module_events
    if issued.get('module_through_position') is not None and module_events.ready(c):
        # ANYM-562: the module stream's own position, compare-and-swap under the same receipt.
        module = c.execute('''UPDATE subscription_module_streams SET acked_through=%s WHERE subscription=%s
            AND acked_through=%s RETURNING acked_through''', (issued['module_through_position'], row['id'],
            issued['module_after_position'])).fetchone()
        if module is None:
            raise APIError('stale_ack', 409)
        result['module_acked_through'] = module['acked_through']
    c.execute('UPDATE subscription_receipts SET ack_result=%s WHERE receipt_hash=%s',
        (Jsonb(result), issued['receipt_hash']))
    return result


async def wait_for_subscription_events(store, token, subscription_id, *, timeout=25, limit=50, detail=None,
                                       include=None):
    """Each poll authenticates independently and returns its connection before waiting."""
    from .store import APIError, bounded
    bounded(limit)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout <= 25:
        raise APIError('invalid_parameters')
    view = page_detail(detail, include)
    # Polls read full pages (the loop reads the subscription state); only the returned page is projected.
    page, scope = await _wait(store, token, subscription_id, timeout, limit)
    if view[0] == 'compact' and scope is not None:
        # ANYM-826: one count per returned page, not per poll; submitting never blocks or awaits.
        COMPACT_READS.submit(store, token, page['subscription']['subscription_id'], scope)
    return project_page(page, *view)


class _CompactReads:
    """ANYM-826: `reads_compact` counts, written after the read returned by one worker thread in its own short
    transaction (Store.count_compact_read), so a held counter row, a timeout or a terminated session never delays
    or fails a read, a receipt or an ACK. At most SLOTS counts are queued or running; a count beyond them is
    skipped (`skipped`), never waited for. The counter uses at most one pooled connection at a time."""
    SLOTS = 4

    def __init__(self):
        self.slots = threading.BoundedSemaphore(self.SLOTS)
        self.lock = threading.Lock()
        self.executor = None
        self.pending = set()
        self.skipped = 0

    def submit(self, store, token, subscription_id, scope_version):
        if not self.slots.acquire(blocking=False):
            with self.lock:
                self.skipped += 1
            return False
        try:
            with self.lock:
                if self.executor is None:
                    from concurrent.futures import ThreadPoolExecutor
                    self.executor = ThreadPoolExecutor(1, thread_name_prefix='reads-compact')
                future = self.executor.submit(_count_compact_read, store, token, subscription_id, scope_version)
                self.pending.add(future)
        except Exception:
            # An interpreter shutting down refuses new work: the count is dropped.
            self.slots.release()
            return False
        future.add_done_callback(self._finished)
        return True

    def _finished(self, future):
        with self.lock:
            self.pending.discard(future)
        self.slots.release()

    def drain(self, timeout=10):
        """Tests: wait for the counts already submitted."""
        from concurrent.futures import wait
        with self.lock:
            pending = list(self.pending)
        wait(pending, timeout)


def _count_compact_read(store, token, subscription_id, scope_version):
    try:
        store.count_compact_read(token, subscription_id, scope_version)
    except Exception as error:
        # Includes a revoked key or a lost connection: only this count is lost.
        print(json.dumps({'error': 'subscription_activity_failed',
            'code': getattr(error, 'code', None) or type(error).__name__}), flush=True)


COMPACT_READS = _CompactReads()
# ANYM-826: Store.fetch_subscription_events leaves the scope version of its read here for the wait poll that
# called it in the same thread; another store leaves none, and its page is not counted.
READ_SCOPE = threading.local()


async def _wait(store, token, subscription_id, timeout, limit):
    """(page, the scope version the returned page was read under)."""
    from .store import APIError
    admission = _Admission()
    deadline = time.monotonic() + (timeout or _IMMEDIATE_READ_TIMEOUT_S)
    latest = scope = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if timeout and remaining <= 0:
                if latest is None:
                    raise APIError('subscription_wait_timeout', 503)
                return {**latest, 'wait_status': 'timeout'}, scope
            poll = asyncio.create_task(asyncio.to_thread(admission.read, store, token, subscription_id, limit))
            poll.add_done_callback(_consume_result)
            try:
                latest, scope = await asyncio.wait_for(asyncio.shield(poll), remaining)
            except TimeoutError:
                if latest is None:
                    raise APIError('subscription_wait_timeout', 503) from None
                return {**latest, 'wait_status': 'timeout'}, scope
            if latest['items'] or (latest.get('module') or {}).get('items'):
                return {**latest, 'wait_status': 'events_available'}, scope
            if latest.get('subscription', {}).get('state') != 'active':
                return {**latest, 'wait_status': 'subscription_inactive'}, scope
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not timeout:
                return {**latest, 'wait_status': 'timeout'}, scope
            await asyncio.sleep(min(0.25, remaining))
    finally:
        admission.close()
