"""Small, restartable journal matcher; never opens a provider or calls a model."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import threading

from .store import Store
from .delivery_policy import flush_hints
from .subscriptions import match_subscriptions
from .telemetry import configure, capture


def main():
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
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    dsn = Path(os.environ['DATABASE_URL_FILE']).read_text().strip()
    store = Store(dsn, Path(os.environ.get('PROBE_FILES', '/tmp/unused-files')),
                  'http://localhost')
    try:
        while not stop.is_set():
            try:
                result = match_subscriptions(store, batch_size=args.batch_size,
                                             max_subscriptions=args.max_subscriptions)
                if args.once:
                    print(json.dumps(result))
                    return
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
            stop.wait(args.interval)
    finally:
        store.close()


if __name__ == '__main__':
    main()
