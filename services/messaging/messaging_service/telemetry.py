"""Opt-in Tiden diagnostics. Only an allowlisted code location leaves the host."""
from __future__ import annotations

import atexit
from collections import OrderedDict
import logging
import math
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time

from . import __version__
from .operational import TELEMETRY_STOP_CODES as STOP_CODES

COMPONENTS = {'gateway', 'telegram', 'whatsapp', 'indexer', 'enricher', 'subscriptions'}
CODES = {'request_failed', 'tool_failed', 'collector_failed', 'indexer_failed',
         'enrichment_failed', 'subscription_match_failed', 'subscription_hint_failed',
         'subscription_retention_failed', 'subscription_journal_failed',
         'uncaught_exception', 'setup_check'}
# Tiden run environments; an unknown slug would silently create a duplicate (TIDEN-CI.md).
ENVIRONMENTS = {'production', 'local-synthetic'}
ROOT = Path(__file__).resolve().parent
SOURCE_FILES = frozenset(p.name for p in ROOT.glob('*.py'))
# Repository-root paths, matching Tiden component paths.
FRAME_PREFIX = 'services/messaging/messaging_service/'
EXCEPTION_TYPES = {'RuntimeError', 'ValueError', 'TypeError', 'KeyError', 'AttributeError',
    'OSError', 'TimeoutError', 'ConnectionError', 'PermissionError', 'FileNotFoundError',
    'OperationalError', 'InterfaceError', 'PoolTimeout', 'IntegrityError', 'ProgrammingError',
    'AssertionError', 'MemoryError', 'RecursionError', 'OverflowError', 'IndexError'}
_reporter = None
log = logging.getLogger('anymsg.telemetry')


def source_sha():
    """The exact DeedKit source revision built into the image (40 lowercase hex), else None."""
    sha = os.environ.get('DEEDKIT_SOURCE_SHA', '')
    # An invalid value is never sent; it reads like an absent one.
    return sha if re.fullmatch('[0-9a-f]{40}', sha) else None


def release():
    """The exact DeedKit source revision built into the image, else the historical package version."""
    sha = source_sha()
    return 'deedkit@' + sha if sha else 'anymsg@' + __version__


def safe_event(error, component, code):
    """Never stringify exceptions, inspect locals, read source lines or retain a traceback."""
    # A collector stop also reports its fixed stop code, so Tiden groups each cause (ANYM-292).
    # Only built-in strings: a str subclass can fake equality with an allowlisted value.
    if type(component) is not str or type(code) is not str:
        return None
    if component not in COMPONENTS or (code not in CODES and code not in STOP_CODES.get(component, ())):
        return None
    frames = []
    tb = error.__traceback__ if isinstance(error, BaseException) else None
    # Bound traversal even if a pathological exception has an enormous stack.
    for _ in range(128):
        if tb is None:
            break
        filename = Path(tb.tb_frame.f_code.co_filename)
        if filename.parent == ROOT and filename.name in SOURCE_FILES:
            frames.append({'filename': FRAME_PREFIX + filename.name,
                           'lineno': tb.tb_lineno, 'in_app': True})
        tb = tb.tb_next
    kind = type(error).__name__
    if kind not in EXCEPTION_TYPES:
        kind = 'UnexpectedError'
    event = {'platform': 'python', 'level': 'error', 'release': release(),
             'tags': {'component': component, 'error_code': code},
             'exception': {'values': [{'type': kind, 'value': code,
                                      'stacktrace': {'frames': frames[-16:]}}]}}
    # Tiden groups by exception type and frames; a collector stop code needs its own issue (ANYM-410).
    if component in STOP_CODES:
        event['fingerprint'] = ['collector-stop', component, code]
    return event


def before_send(event):
    """Defense in depth: discard all SDK/request/user/breadcrumb extras."""
    try:
        tags = event['tags']
        component, code = tags['component'], tags['error_code']
        clean = safe_event(None, component, code)
        environment = event.get('environment')
        if clean is None or type(environment) is not str or environment not in ENVIRONMENTS:
            return None
        value = event['exception']['values'][0]
        kind = value['type']
        clean['exception']['values'][0]['type'] = kind if type(kind) is str and kind in EXCEPTION_TYPES else 'UnexpectedError'
        frames = []
        for frame in value.get('stacktrace', {}).get('frames', [])[-16:]:
            filename, lineno = frame.get('filename'), frame.get('lineno')
            if filename not in {FRAME_PREFIX + f for f in SOURCE_FILES}:
                continue
            if type(lineno) is int and 0 < lineno <= 100000:
                frames.append({'filename': filename, 'lineno': lineno, 'in_app': True})
        clean['exception']['values'][0]['stacktrace']['frames'] = frames
        event_id, timestamp = event.get('event_id', ''), event.get('timestamp')
        if not isinstance(event_id, str) or not re.fullmatch('[0-9a-f]{32}', event_id):
            return None
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
            return None
        clean.update(event_id=event_id, timestamp=timestamp, environment=event['environment'],
                     sdk={'name': 'tiden.python', 'version': '0.1.0'})
        return clean
    except Exception:
        return None


class Reporter:
    """A lossy, bounded diagnostic queue; archive operations never wait on Tiden."""
    def __init__(self, client, component, environment='production'):
        if component not in COMPONENTS or environment not in ENVIRONMENTS:
            raise ValueError('invalid_telemetry_configuration')
        self.client, self.component, self.environment = client, component, environment
        self.queue = queue.Queue(maxsize=32)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.seen = OrderedDict()
        self.window, self.count = time.monotonic(), 0
        self.thread = threading.Thread(target=self._run, name='anymsg-telemetry', daemon=True)
        self.thread.start()

    def capture(self, error, code):
        event = safe_event(error, self.component, code)
        if event is None:
            return False
        event['environment'] = self.environment
        last = event['exception']['values'][0]
        key = (code, last['type'], tuple((f['filename'], f['lineno']) for f in last['stacktrace']['frames']))
        with self.lock:
            if self.stop.is_set():
                return False
            now = time.monotonic()
            if now - self.window >= 60:
                self.window, self.count = now, 0
            if self.count >= 20 or now - self.seen.get(key, -60) < 60:
                return False
            try:
                self.queue.put_nowait(event)
            except queue.Full:
                return False
            self.count += 1
            self.seen[key] = now
            if len(self.seen) > 128:
                self.seen.popitem(last=False)
        # Accepted locally is not a delivery receipt. The SDK swallows HTTP failures.
        return True

    def _run(self):
        while not self.stop.is_set() or not self.queue.empty():
            try:
                event = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.client.capture_event(event)
            except Exception:
                pass  # No recursive telemetry or network error payload in local logs.
            finally:
                self.queue.task_done()

    def close(self):
        with self.lock:
            if self.stop.is_set():
                return
            self.stop.set()
        self.thread.join(timeout=1.5)


def capture(error, code):
    try:
        return bool(_reporter and _reporter.capture(error, code))
    except Exception:
        return False


def configure(component):
    """No SDK import, thread, hook or network without explicit opt-in and a DSN file."""
    global _reporter
    if _reporter is not None:
        return True
    if os.environ.get('ANYMSG_TIDEN_ENABLED') != '1':
        return False
    try:
        from .security_logging import redact_library_logs
        redact_library_logs()
        from tiden_telemetry import Client
        dsn = Path(os.environ['ANYMSG_TIDEN_DSN_FILE']).read_text().strip()
        from urllib.parse import urlsplit
        url = urlsplit(dsn)
        if url.scheme != 'https' or not url.username or url.password or not url.hostname:
            raise ValueError('invalid_telemetry_dsn')
        client = Client(dsn, send_default_pii=False, before_send=before_send,
                        http_timeout=1.0, install_excepthook=False)
        reporter = Reporter(client, component, os.environ.get('ANYMSG_TIDEN_ENVIRONMENT', 'production'))
    except Exception:
        log.warning('telemetry_configuration_unavailable')
        return False
    _reporter = reporter
    previous = sys.excepthook

    def hook(kind, error, tb):
        capture(error, 'uncaught_exception')
        reporter.close()
        previous(kind, error, tb)

    sys.excepthook = hook
    previous_thread = threading.excepthook

    def thread_hook(args):
        capture(args.exc_value, 'uncaught_exception')
        previous_thread(args)

    threading.excepthook = thread_hook
    atexit.register(reporter.close)
    return True
