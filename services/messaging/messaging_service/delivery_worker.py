"""Explicit operator-launched hint dispatcher with a dedicated restricted DSN."""
import argparse
import json
import os
from pathlib import Path
import signal
import threading
import time
from . import heartbeats
from .egress import proxy_lookup, proxy_transport
from .event_delivery import (OPERATOR_DNS_SLOTS, _addresses, _transport, beat, dispatch_hints, disable_receiver,
    endpoint_tests, register_receiver)
from .store import Store

HEARTBEAT_S = heartbeats.HEARTBEAT_S
# A lane that has not finished a pass for this long stops the heartbeat, so capabilities report webhook down.
LANE_STALL_S = 60
ENDPOINT_LANES = 3


def pulse(store, progress, *, now=None):
    """Write both heartbeats only while every lane has finished a pass within LANE_STALL_S (ANYM-534)."""
    now = time.monotonic() if now is None else now
    if any(now - last > LANE_STALL_S for last in list(progress.values())):
        return False
    alive = beat(store)
    return heartbeats.record(store, 'dispatcher') and alive


def _lane(store, stop, name, progress, interval, step):
    while not stop.is_set():
        busy = False
        try:
            result = step()
            busy = any(result.get(key) for key in ('delivered', 'retried', 'dead_letter', 'cancelled', 'deferred'))
            if busy or result.get('endpoint_tests'):
                print(json.dumps(dict(result, lane=name), default=str), flush=True)
        except Exception as error:
            # One endpoint's failure (or a database blip) never stops the lane; its lease expires and retries.
            print(json.dumps({'error': 'delivery_lane_failed', 'lane': name, 'class': type(error).__name__}), flush=True)
        finally:
            progress[name] = time.monotonic()
        if not busy:
            stop.wait(interval)


def run(store, stop, *, transport=_transport, resolver=None, interval=2, proxy=None):
    """Operator receivers and self-service endpoints in separate lanes until stop is set (ANYM-534). With `proxy`
    (DELIVERY_EGRESS_PROXY) every lookup and connection goes through the egress proxy, the operator lane on its own
    proxy slots (ANYM-581)."""
    operator_transport = transport
    if proxy:
        transport, operator_transport = proxy_transport(proxy), proxy_transport(proxy, lane='operator')
        resolver, operator_resolver = proxy_lookup(proxy), proxy_lookup(proxy, lane='operator')
    else:
        operator_resolver = resolver or (lambda host, port: _addresses(host, port, slots=OPERATOR_DNS_SLOTS))
        resolver = resolver or _addresses
    served = {}
    def endpoint_step(first):
        def step():
            result = dispatch_hints(store, lane='endpoint', limit=1, served=served, transport=transport,
                resolver=resolver)
            if first:
                # One test per pass, in the lanes' shared key order: a key's tests never run a batch ahead.
                tests = endpoint_tests(store, transport=transport, resolver=resolver, limit=1, served=served)
                if tests is not None:
                    result['endpoint_tests'] = tests
            return result
        return step
    steps = {'operator': lambda: dispatch_hints(store, lane='operator', transport=operator_transport,
        resolver=operator_resolver)}
    steps.update({f'endpoint-{n}': endpoint_step(n == 0) for n in range(ENDPOINT_LANES)})
    started = time.monotonic()
    progress = dict.fromkeys(steps, started)
    threads = [threading.Thread(target=_lane, args=(store, stop, name, progress, interval, step), daemon=True,
        name='deedkit-delivery-' + name) for name, step in steps.items()]
    for thread in threads:
        thread.start()
    try:
        while not stop.is_set():
            try:
                pulse(store, progress)
            except Exception as error:
                # Local stdout only: the class, never the message (it can hold a DSN or an endpoint).
                print(json.dumps({'error': 'delivery_heartbeat_failed', 'class': type(error).__name__}), flush=True)
            stop.wait(HEARTBEAT_S)
    finally:
        stop.set()
        for thread in threads:
            thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--register-receiver', type=Path, metavar='PRIVATE_JSON')
    group.add_argument('--disable-receiver', metavar='UUID')
    args = parser.parse_args()
    dsn_file = os.environ.get('DATABASE_URL_FILE')
    if not dsn_file:
        raise SystemExit('DATABASE_URL_FILE is required')
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    store = Store(Path(dsn_file).read_text().strip(), Path('/nonexistent-originals'), 'http://unused')
    proxy = os.environ.get('DELIVERY_EGRESS_PROXY') or None
    try:
        if args.register_receiver:
            info = args.register_receiver.stat()
            if info.st_mode & 0o077 or info.st_size > 8192:
                raise SystemExit('Receiver configuration must be private and bounded')
            config = json.loads(args.register_receiver.read_text())
            if not isinstance(config, dict) or set(config) != {'receiver_id','principal','key_id','endpoint','secret_file'}:
                raise SystemExit('Invalid receiver configuration')
            # The dispatcher container resolves only through the proxy (ANYM-581).
            lookup = {'resolver': proxy_lookup(proxy, lane='operator')} if proxy else {}
            print(json.dumps(register_receiver(store, **config, **lookup)))
            return
        if args.disable_receiver:
            print(json.dumps(disable_receiver(store, args.disable_receiver)))
            return
        if args.once:
            once = {'transport': proxy_transport(proxy), 'resolver': proxy_lookup(proxy)} if proxy else {}
            print(json.dumps(dispatch_hints(store, **once)), flush=True)
            return
        run(store, stop, proxy=proxy)
    finally:
        store.close()


if __name__ == '__main__':
    main()
