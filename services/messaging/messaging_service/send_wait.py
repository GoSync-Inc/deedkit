"""Bounded observation of an already queued send; this module never dispatches."""
import asyncio
import math
import threading

from .store import APIError


POLL_INTERVAL_S = 0.25
MAX_WAIT_S = 25
MAX_CONCURRENT_WAITS = 32
_WAIT_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_WAITS)
_PENDING_STATES = frozenset(('queued', 'dispatching'))


def validate_wait_s(wait_s):
    """Validate before enqueueing so an invalid wait never creates an action."""
    if (isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)) or
            not 0 <= wait_s <= MAX_WAIT_S or not math.isfinite(wait_s)):
        raise APIError('invalid_wait_s')
    return float(wait_s)


class _Admission:
    """A cancelled request retains its slot until its active DB read finishes."""
    def __init__(self):
        self.semaphore = _WAIT_SLOTS
        if not self.semaphore.acquire(blocking=False):
            raise APIError('send_wait_busy', 503)
        self.lock = threading.Lock()
        self.closed = False
        self.reading = False

    def read(self, read, token, handle):
        with self.lock:
            # A queued thread can start after the HTTP request's deadline. In
            # that case it must not acquire a database connection at all.
            if self.closed:
                return None
            self.reading = True
        try:
            # Store owns the short authorization/connection context. No DB
            # connection or authorization lock survives into the poll sleep.
            return read(token, handle)
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
    # asyncio cannot stop a running thread. Observe its eventual exception even
    # after the client has cancelled or the bounded response has been returned.
    if not task.cancelled():
        task.exception()


async def wait_for_send(store, token, send_id, wait_s):
    return await _wait(store.get_send_status, token, send_id, wait_s)


async def wait_for_operation(store, token, operation_id, wait_s):
    """ANYM-549: the same bounded observation of an already queued messenger operation; it never dispatches."""
    return await _wait(store.get_messenger_operation, token, operation_id, wait_s)


async def _wait(read, token, handle, wait_s):
    """Return authorized status plus elapsed/timeout metadata, without resending.

    ``wait_s=0`` performs one status read. Enqueue callers can skip this helper
    entirely for their default immediate response. Unknown is terminal for this
    wait: only existing exact-receipt reconciliation can subsequently change it.
    """
    wait_s = validate_wait_s(wait_s)
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + wait_s
    admission = _Admission()
    latest = None

    def result(timed_out):
        return {**latest, 'wait_elapsed_s': round(loop.time() - started, 3),
            'wait_timed_out': timed_out}

    try:
        while True:
            remaining = deadline - loop.time()
            if wait_s and remaining <= 0:
                if latest is None:
                    raise APIError('send_status_timeout', 503)
                return result(True)
            task = asyncio.create_task(asyncio.to_thread(admission.read, read, token, handle))
            task.add_done_callback(_consume_result)
            try:
                latest = await asyncio.wait_for(asyncio.shield(task), remaining) if wait_s else await task
            except TimeoutError:
                if latest is None:
                    raise APIError('send_status_timeout', 503) from None
                return result(True)
            if latest['status'] not in _PENDING_STATES or not wait_s:
                return result(False)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return result(True)
            await asyncio.sleep(min(POLL_INTERVAL_S, remaining))
    finally:
        admission.close()
