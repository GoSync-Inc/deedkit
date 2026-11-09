"""Best-effort commit wake-ups for the matcher and the delivery lanes (ANYM-841).

A writer marks its connection; the Store pool sends an empty `NOTIFY` after that transaction commits, in a
transaction of its own, so a full notification queue can never fail the write. A worker listens on a dedicated
connection and wakes on it; its `--interval` poll stays the fallback, so a lost notification, a failed NOTIFY or a
dropped listener costs at most one poll. Payloads stay empty: channel names and payloads are visible to every
database role. LISTEN and NOTIFY need no grant.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import threading

from psycopg_pool import ConnectionPool

EVENTS = 'anymsg_events'
OUTBOX = 'anymsg_outbox'
CHANNELS = (EVENTS, OUTBOX)
_ATTRIBUTE = '_anymsg_wake'
# The NOTIFY step after a commit: bounded, so a stalled notification queue never holds a writer.
NOTIFY_TIMEOUT = '500ms'

_failed = 0
_failed_lock = threading.Lock()


def mark(c, channel):
    """Ask for a NOTIFY on `channel` once the transaction on `c` commits (a WakingPool connection; any other
    connection keeps the poll). Only an unknown channel raises."""
    if channel not in CHANNELS:
        raise ValueError('invalid_wake_channel')
    try:
        c.__dict__.setdefault(_ATTRIBUTE, set()).add(channel)
    except Exception:
        pass


def _take(c):
    try:
        return c.__dict__.pop(_ATTRIBUTE, None)
    except Exception:
        return None


def failures():
    return _failed


def notify(c, channels):
    """One empty NOTIFY per channel, on the committed connection in its own transaction. A failure is counted and
    logged (class only), never raised: the write it follows has committed already."""
    global _failed
    try:
        with c.transaction():
            c.execute("SELECT set_config('statement_timeout',%s,true),set_config('lock_timeout',%s,true)",
                (NOTIFY_TIMEOUT, NOTIFY_TIMEOUT))
            for channel in sorted(channels):
                if channel in CHANNELS:
                    c.execute('NOTIFY ' + channel)
    except Exception as error:
        with _failed_lock:
            _failed += 1
            count = _failed
        print(json.dumps({'error': 'wake_notify_failed', 'class': type(error).__name__, 'count': count}), flush=True)


class WakingPool(ConnectionPool):
    """ConnectionPool whose `connection()` sends the marked NOTIFYs after its commit, before the connection goes
    back to the pool. A rolled-back or failed transaction sends nothing; a savepoint rolled back inside a committed
    transaction may send one spare wake, which costs one empty pass."""

    @contextmanager
    def connection(self, timeout=None):
        conn = self.getconn(timeout=timeout)
        try:
            with conn:
                yield conn
            channels = _take(conn)
            # A closed connection committed nothing (transaction_timeout and a lost session end it).
            if channels and not conn.closed:
                notify(conn, channels)
        finally:
            _take(conn)
            self.putconn(conn)


class Listener:
    """LISTEN on one channel on a dedicated autocommit connection; reconnects after `retry_s` when it is lost.

    `token()` before a pass and `wait(timeout, token)` after it: a notification that arrived during the pass ends
    the wait at once, so none is lost between a pass and its sleep. `wait` also ends when `stop` is set and
    `ring()` is called (the workers' signal handlers do both)."""

    def __init__(self, connect, channel, stop, *, slice_s=1.0, retry_s=5.0):
        if channel not in CHANNELS:
            raise ValueError('invalid_wake_channel')
        self._connect, self.channel, self.stop = connect, channel, stop
        self.slice_s, self.retry_s = slice_s, retry_s
        self._cond = threading.Condition()
        self._rings = 0
        self._closed = threading.Event()
        self.listening = False
        self.failures = 0
        self.pid = None
        self._thread = threading.Thread(target=self._run, daemon=True, name='anymsg-wake-' + channel)
        self._thread.start()

    @classmethod
    def for_store(cls, store, channel, stop, **kwargs):
        import psycopg
        conninfo, options = store.pool.conninfo, {**store.pool.kwargs, 'autocommit': True}
        return cls(lambda: psycopg.connect(conninfo, **options), channel, stop, **kwargs)

    def token(self):
        with self._cond:
            return self._rings

    def ring(self):
        with self._cond:
            self._rings += 1
            self._cond.notify_all()

    def wait(self, timeout, token):
        """True when woken (a notification, a reconnect or stop), False after the full timeout."""
        with self._cond:
            return self._cond.wait_for(lambda: self._rings != token or self.stop.is_set(), timeout)

    def close(self, timeout=5):
        self._closed.set()
        self._thread.join(timeout)

    def _done(self):
        return self._closed.is_set() or self.stop.is_set()

    def _run(self):
        while not self._done():
            conn = None
            try:
                conn = self._connect()
                conn.execute('LISTEN ' + self.channel)
                self.pid, self.listening = conn.info.backend_pid, True
                # Commits while the listener was away get one pass at once.
                self.ring()
                while not self._done():
                    for _ in conn.notifies(timeout=self.slice_s, stop_after=1):
                        self.ring()
            except Exception as error:
                self.failures += 1
                # Class and count only: a connection error can name the DSN's host or role.
                print(json.dumps({'error': 'wake_listen_failed', 'channel': self.channel,
                    'class': type(error).__name__, 'count': self.failures}), flush=True)
            finally:
                self.listening = False
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            self._closed.wait(self.retry_s)


def listen(store, channel, stop):
    """A started Listener for the store's database, or None (the poll alone) when it cannot even be built."""
    try:
        return Listener.for_store(store, channel, stop)
    except Exception as error:
        print(json.dumps({'error': 'wake_listen_failed', 'channel': channel, 'class': type(error).__name__}),
            flush=True)
        return None


def pause(stop, wake, seconds, token):
    """Sleep up to `seconds`: at once on stop, early on a notification after `token`, the full poll without a
    listener (or while it reconnects: the poll is the fallback)."""
    if wake is None or seconds <= 0:
        return stop.wait(seconds)
    return wake.wait(seconds, token)
