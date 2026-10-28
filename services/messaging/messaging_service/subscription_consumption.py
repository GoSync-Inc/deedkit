"""Explicit credential-bound subscription pages and independent compare-and-swap ACKs."""
from __future__ import annotations
import asyncio
import hashlib
import secrets
import time
import threading
from psycopg.types.json import Jsonb
from .subscriptions import _owned, get_subscription_events

_WAIT_SLOTS = threading.BoundedSemaphore(32)
_IMMEDIATE_READ_TIMEOUT_S = 5
_RECEIPT_LIMIT = 128


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
        with self.lock:
            if self.closed:
                return None
            self.reading = True
        try:
            return store.fetch_subscription_events(token, subscription_id, limit=limit)
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
    if item.get('kind', '').startswith(('attachment.', 'file.')):
        reasons.append('processing_state_changed')
    return reasons or ['selected_event']


def _active(row):
    from .store import APIError
    if row['state'] == 'access_changed':
        raise APIError('subscription_access_changed', 409)
    if row['state'] != 'active':
        raise APIError('subscription_closed', 409)


def fetch_subscription_events(store, c, p, subscription_id, *, limit=50):
    from .store import APIError, bounded
    bounded(limit)
    row = _owned(c, p, subscription_id)
    _active(row)
    c.execute('''INSERT INTO subscription_consumption(subscription,acked_through)
        VALUES(%s,%s) ON CONFLICT DO NOTHING''', (row['id'], row['start_position']))
    consumption = c.execute('SELECT * FROM subscription_consumption WHERE subscription=%s FOR UPDATE',
        (row['id'],)).fetchone()
    from . import module_events
    module = module_events.stream(c, row['id'], lock=True)
    page = get_subscription_events(store, c, p, subscription_id, after=consumption['acked_through'], limit=limit,
        module_after=module['acked_through'] if module else None)
    if module is not None:
        page['module'].update(acked_through=module['acked_through'])
    page.update(consumption='explicit_subscription_ack', ack_version=consumption['version'], receipt=None,
        acked_through=consumption['acked_through'], receipt_expires_at=None,
        ack_available='subscription-ack' in p['permissions'])
    moved = module is not None and page['module']['next_after'] != module['acked_through']
    if not page['items'] and not moved or 'subscription-ack' not in p['permissions']:
        return page
    c.execute('DELETE FROM subscription_receipts WHERE subscription=%s AND expires_at<=clock_timestamp()',
        (row['id'],))
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
    page.update(receipt=receipt, receipt_expires_at=issued['expires_at'].isoformat())
    return page


def ack_subscription_events(store, c, p, subscription_id, receipt, version):
    from .store import APIError
    if 'read' not in p['permissions']:
        raise APIError('forbidden', 403)
    if type(version) is not int or not 1 <= version <= 2**53 - 1:
        raise APIError('invalid_version')
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
    if issued.get('module_through_position') is not None:
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


async def wait_for_subscription_events(store, token, subscription_id, *, timeout=25, limit=50):
    """Each poll authenticates independently and returns its connection before waiting."""
    from .store import APIError, bounded
    bounded(limit)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout <= 25:
        raise APIError('invalid_parameters')
    admission = _Admission()
    deadline = time.monotonic() + (timeout or _IMMEDIATE_READ_TIMEOUT_S)
    latest = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if timeout and remaining <= 0:
                if latest is None:
                    raise APIError('subscription_wait_timeout', 503)
                return {**latest, 'wait_status': 'timeout'}
            poll = asyncio.create_task(asyncio.to_thread(admission.read, store, token, subscription_id, limit))
            poll.add_done_callback(_consume_result)
            try:
                latest = await asyncio.wait_for(asyncio.shield(poll), remaining)
            except TimeoutError:
                if latest is None:
                    raise APIError('subscription_wait_timeout', 503) from None
                return {**latest, 'wait_status': 'timeout'}
            if latest['items'] or (latest.get('module') or {}).get('items'):
                return {**latest, 'wait_status': 'events_available'}
            if latest.get('subscription', {}).get('state') != 'active':
                return {**latest, 'wait_status': 'subscription_inactive'}
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not timeout:
                return {**latest, 'wait_status': 'timeout'}
            await asyncio.sleep(min(0.25, remaining))
    finally:
        admission.close()
