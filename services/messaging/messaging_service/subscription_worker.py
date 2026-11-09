"""Small, restartable journal matcher; never opens a provider or calls a model."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import threading
import time

from . import heartbeats
from .store import Store
from .delivery_policy import flush_hints
from .subscriptions import (acked_retention_days, journal_source_health, match_subscriptions,
    prune_subscriptions, retire_subscriptions, source_health_stale_seconds)
from .telemetry import configure, capture
from . import wake as wakes

HEALTH_INTERVAL_S = 30
RETIRE_INTERVAL_S = 60
# ANYM-841: a pass that scanned, matched or advanced anything (messages or modules) is followed at once by the
# next; only an empty pass sleeps.
BUSY_KEYS = ('scanned', 'matched', 'module_scanned', 'module_matched')


def busy(result):
    return isinstance(result, dict) and any(result.get(key) for key in BUSY_KEYS)


def main():
    from .store import name_sessions
    name_sessions('matcher')  # ANYM-842: this service's sessions carry its name
    configure('subscriptions')
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=float, default=2)
    parser.add_argument('--batch-size', type=int, default=200)
    parser.add_argument('--max-subscriptions', type=int, default=16)
    args = parser.parse_args()
    if not 0.1 <= args.interval <= 60:
        parser.error('interval must be between 0.1 and 60 seconds')
    if not 1 <= args.batch_size <= 1000:
        parser.error('batch-size must be between 1 and 1000')
    if not 1 <= args.max_subscriptions <= 100:
        parser.error('max-subscriptions must be between 1 and 100')
    # An out-of-bounds retention window fails startup instead of pruning with a wrong one.
    acked_retention_days()
    source_health_stale_seconds()
    stop = threading.Event()
    wake = None
    def halt(*_):
        stop.set()
        if wake is not None:
            wake.ring()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, halt)
    dsn = Path(os.environ['DATABASE_URL_FILE']).read_text().strip()
    store = Store(dsn, Path(os.environ.get('PROBE_FILES', '/tmp/unused-files')),
                  'http://localhost')
    last_beat = last_health = last_retire = last_prune = None
    more = False
    try:
        if not args.once:
            # ANYM-841: committed events wake the matcher early; the --interval poll stays the fallback.
            wake = wakes.listen(store, wakes.EVENTS, stop)
        while not stop.is_set():
            # Taken before the pass, so a commit during the pass ends the sleep after it at once.
            token = wake.token() if wake is not None else None
            result = None
            if not args.once:
                # Retention first frees match budget; its failures never stop matching or hints.
                try:
                    # ANYM-841: a busy loop keeps the poll's pruning cadence, at most once per --interval.
                    if last_prune is None or time.monotonic() - last_prune >= args.interval:
                        last_prune = time.monotonic()
                        prune_subscriptions(store, max_subscriptions=args.max_subscriptions)
                    # ANYM-447: 30-day housekeeping runs once a minute, at once again while a batch was full.
                    if last_retire is None or more or time.monotonic() - last_retire >= RETIRE_INTERVAL_S:
                        last_retire = time.monotonic()
                        more = retire_subscriptions(store, max_subscriptions=args.max_subscriptions)['more']
                except Exception as error:
                    capture(error, 'subscription_retention_failed')
                    print(json.dumps({'error': 'subscription_retention_failed'}), flush=True)
            # ANYM-535: account health changes join the journal before matching; never raises.
            if last_health is None or time.monotonic() - last_health >= HEALTH_INTERVAL_S:
                journal_source_health(store)
                last_health = time.monotonic()
            try:
                result = match_subscriptions(store, batch_size=args.batch_size,
                                             max_subscriptions=args.max_subscriptions)
                if args.once:
                    print(json.dumps(result))
                    return
                # ANYM-534: the version heartbeat follows a successful match; a failed write never stops matching.
                if last_beat is None or time.monotonic() - last_beat >= heartbeats.HEARTBEAT_S:
                    heartbeats.record(store, 'matcher')
                    last_beat = time.monotonic()
            except Exception as error:
                capture(error, 'subscription_match_failed')
                # No SQL, DSN, content, selectors or subscription IDs in logs.
                if args.once:
                    raise RuntimeError('subscription_match_failed') from None
                print(json.dumps({'error': 'subscription_match_failed'}), flush=True)
            try:
                # Held hints and silence timers fire from durable rows, independent of matching.
                flush_hints(store)
            except Exception as error:
                capture(error, 'subscription_hint_failed')
                print(json.dumps({'error': 'subscription_hint_failed'}), flush=True)
            wakes.pause(stop, wake, 0 if busy(result) else args.interval, token)
    finally:
        if wake is not None:
            wake.close()
        store.close()


if __name__ == '__main__':
    main()
