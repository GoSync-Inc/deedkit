"""Module event router (ANYM-575): pulls each configured module's journal and publishes into messaging.

Per page: the HTTP read, the page checks and every event's contract check run first, with no database
transaction open; then one short database-only transaction under server-side deadlines publishes the
events through `publish_module_event` and advances the stored cursor `(module, after_seq)`. A crash on
either side of that commit replays from the stored cursor, and `event_key` makes the replay idempotent.
A malformed event is quarantined with its seq, the page continues and the source's coverage stays
incomplete until a re-fetch publishes it or the operator abandons it. The router writes no message row
and takes no message lock, so a dead, slow or restarting module never stalls message hints.
Logs and telemetry carry fixed codes only: never a URL, token, page or event body.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import threading
import time
from urllib.parse import urlsplit

import httpx
import psycopg
from psycopg import errors
from psycopg.rows import dict_row

from . import module_events
from .module_events import _ID, _SCOPE, MAX_SEQ, SOURCES, ModuleContractError, validate_event

LIMIT = 100
MAX_PAGE_BYTES = 256 * 1024
PAGES_PER_PASS = 10
REFETCH_PER_PASS = 4
PAGE_KEYS = frozenset(('events', 'next_after', 'has_more', 'retention_floor', 'journal_id'))
# Server-side bounds of every router transaction: a lock wait, one statement, the whole transaction and
# idling inside it. Nothing but database work runs inside one.
DEADLINES = (('lock_timeout', '1s'), ('statement_timeout', '2s'), ('transaction_timeout', '5s'),
    ('idle_in_transaction_session_timeout', '5s'))


class ModuleUnavailable(Exception):
    """The module did not answer with a usable page; `code` is fixed text, never the cause's message."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def load_config(path):
    """{'modules': [{'source', 'url', 'token_file'}], 'timeout_s', 'interval_s'}; refuses anything else."""
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict) or set(raw) - {'modules', 'timeout_s', 'interval_s'}:
        raise ValueError('invalid_router_config')
    modules, seen = raw.get('modules'), set()
    if not isinstance(modules, list) or not 1 <= len(modules) <= len(SOURCES):
        raise ValueError('invalid_router_config')
    for module in modules:
        if not isinstance(module, dict) or set(module) != {'source', 'url', 'token_file'}:
            raise ValueError('invalid_router_config')
        url = urlsplit(module['url']) if isinstance(module['url'], str) else None
        if (module['source'] not in SOURCES or module['source'] in seen or url is None or
                url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or
                url.query or url.fragment or not isinstance(module['token_file'], str) or
                not module['token_file'].startswith('/')):
            raise ValueError('invalid_router_config')
        seen.add(module['source'])
    timeout, interval = raw.get('timeout_s', 5), raw.get('interval_s', 5)
    if (type(timeout) not in (int, float) or not 0.2 <= timeout <= 30 or
            type(interval) not in (int, float) or not 0.1 <= interval <= 300):
        raise ValueError('invalid_router_config')
    return {'modules': modules, 'timeout_s': float(timeout), 'interval_s': float(interval)}


class HttpModule:
    """One producer's read endpoint behind its router token (read per request, so a rotation needs no restart)."""

    def __init__(self, source, url, token_file, timeout_s):
        self.source, self.url, self.token_file, self.timeout = source, url.rstrip('/'), Path(token_file), timeout_s
        # One client per module: building one loads the TLS trust store, which costs more than a page read.
        self.client = self._client()

    def _client(self):
        return httpx.Client(timeout=httpx.Timeout(self.timeout), follow_redirects=False, trust_env=False)

    def head(self):
        body = self._get('/v1/events/head', {})
        if (not isinstance(body, dict) or set(body) != {'journal_id', 'head'} or not isinstance(body['journal_id'], str)
                or not 1 <= len(body['journal_id']) <= 64 or type(body['head']) is not int or
                not 0 <= body['head'] <= MAX_SEQ):
            raise ModuleUnavailable('invalid_head')
        return body

    def events(self, after, limit):
        return self._get('/v1/events', {'after': after, 'limit': limit})

    def _get(self, path, params):
        """One request under a total deadline over connect, headers and body.

        httpx bounds each phase, not the whole: a module that trickles its headers or body could hold the
        worker for many phases. The read runs on a thread; past the deadline the client is closed, which
        closes its socket and ends the abandoned read, and the next request gets a new client.
        """
        try:
            token = self.token_file.read_text().strip()
        except OSError:
            raise ModuleUnavailable('token_unreadable') from None
        outcome, client = {}, self.client

        def read():
            try:
                outcome['body'] = self._read(client, path, params, token)
            except BaseException as error:
                outcome['error'] = error
        worker = threading.Thread(target=read, name='module-read', daemon=True)
        worker.start()
        worker.join(self.timeout)
        if worker.is_alive():
            self.client = self._client()
            try:
                client.close()
            except Exception:
                pass
            raise ModuleUnavailable('module_timeout')
        if 'error' in outcome:
            raise outcome['error']
        return outcome['body']

    def _read(self, client, path, params, token):
        deadline, body = time.monotonic() + self.timeout, bytearray()
        try:
            with client.stream('GET', self.url + path, params=params,
                    headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}) as response:
                if response.status_code in (401, 403):
                    raise ModuleUnavailable('module_auth_refused')
                if response.status_code != 200:
                    raise ModuleUnavailable('module_http_status')
                # A total deadline and a byte cap: a module that trickles bytes cannot hold the router.
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES:
                        raise ModuleUnavailable('page_over_bytes')
                    if time.monotonic() > deadline:
                        raise ModuleUnavailable('module_timeout')
        except httpx.TimeoutException:
            raise ModuleUnavailable('module_timeout') from None
        except httpx.HTTPError:
            raise ModuleUnavailable('module_unreachable') from None
        try:
            return json.loads(body)
        except ValueError:
            raise ModuleUnavailable('invalid_json') from None


def check_page(page, after, limit):
    """The page's own shape (ANYM-562 read_page rules); raises ModuleContractError."""
    if not isinstance(page, dict) or set(page) != PAGE_KEYS:
        raise ModuleContractError('invalid_page')
    if (not isinstance(page['journal_id'], str) or not 1 <= len(page['journal_id']) <= 64 or
            not isinstance(page['events'], list) or type(page['has_more']) is not bool or
            any(type(page[key]) is not int or not 0 <= page[key] <= MAX_SEQ for key in ('next_after',
                'retention_floor'))):
        raise ModuleContractError('invalid_page')
    if len(page['events']) > limit:
        raise ModuleContractError('page_over_limit')
    if page['has_more'] and page['next_after'] == after:
        raise ModuleContractError('next_after_not_advancing')


def producer_event(raw, source):
    """(seq, envelope, scope_ref) of one producer event: the ANYM-562 envelope plus a top-level scope_ref."""
    if not isinstance(raw, dict) or 'scope_ref' not in raw:
        raise ModuleContractError('invalid_scope_ref')
    envelope = {key: value for key, value in raw.items() if key != 'scope_ref'}
    try:
        seq = validate_event(envelope)
    except ModuleContractError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError):
        # A value of the wrong JSON type (a list as source, an object as urgency) is a refused event too.
        raise ModuleContractError('invalid_envelope') from None
    if envelope['source'] != source:
        raise ModuleContractError('source_mismatch')
    if not isinstance(raw['scope_ref'], str) or _SCOPE.fullmatch(raw['scope_ref']) is None:
        raise ModuleContractError('invalid_scope_ref')
    return seq, envelope, raw['scope_ref']


def _claimed_seq(raw, source):
    """The seq a refused event's id names, when it names one of this module; else None."""
    value = raw.get('id') if isinstance(raw, dict) else None
    match = _ID.fullmatch(value) if isinstance(value, str) else None
    if match is None or SOURCES[source] != match.group(1) or int(match.group(2)) > MAX_SEQ:
        return None
    return int(match.group(2))


def split_page(source, after, page):
    """Events to publish and ranges to quarantine of one checked page after the exclusive cursor `after`.

    Returns (published [(seq, envelope, scope_ref)], quarantined [(after, through, code)], next_after). Every
    seq in (after, next_after] is either published, inside one quarantined range, or a row the producer
    filtered. A refused event that names its seq is quarantined as exactly that seq; one that does not is
    bounded by its neighbours. Ordering breaks (a valid seq not above the previous one, events past
    next_after) refuse the whole page.
    """
    items = []
    for raw in page['events']:
        try:
            items.append((*producer_event(raw, source), None))
        except ModuleContractError as error:
            items.append((_claimed_seq(raw, source), None, None, str(error)))
    published, quarantined, position, pending = [], [], after, None
    for index, (seq, envelope, scope, code) in enumerate(items):
        following = next((item[0] for item in items[index + 1:] if item[3] is None), None)
        upper = page['next_after'] if following is None else following - 1
        if code is None:
            if seq <= position or pending is not None and seq - 1 <= position:
                raise ModuleContractError('seq_not_increasing')
            if pending is not None:
                quarantined.append((position, seq - 1, pending))
                pending = None
            published.append((seq, envelope, scope))
            position = seq
        elif pending is None and seq is not None and position < seq <= upper:
            quarantined.append((seq - 1, seq, code))
            position = seq
        else:
            pending = pending or code
    if pending is not None:
        if page['next_after'] <= position:
            raise ModuleContractError('next_after_behind_events')
        quarantined.append((position, page['next_after'], pending))
        position = page['next_after']
    if position > page['next_after']:
        raise ModuleContractError('next_after_behind_events')
    return published, quarantined, page['next_after']


def _bounded(conn):
    for name, value in DEADLINES:
        conn.execute('SELECT set_config(%s,%s,true)', (name, value))


def _log(source, code):
    print(json.dumps({'event': 'module_router', 'source': source, 'code': code}), flush=True)


def report(conn, source, reachable):
    with conn.transaction():
        _bounded(conn)
        module_events.report_reachability(conn, source, reachable)


def cursor(conn, source):
    return conn.execute('SELECT after_seq,journal_id FROM module_router_cursors WHERE source=%s',
        (source,)).fetchone()


def _open_gap(conn, source, kind, journal_id, after, through, code):
    """Open a gap unless the same one is open; the caller holds the source's cursor row lock."""
    conn.execute('''INSERT INTO module_router_gaps(source,kind,journal_id,after_seq,through_seq,code)
        SELECT %s,%s,%s,%s,%s,%s WHERE NOT EXISTS(SELECT FROM module_router_gaps WHERE source=%s AND kind=%s
        AND after_seq=%s AND resolved_at IS NULL)''', (source, kind, journal_id, after, through, code, source, kind,
        after))


def _locked_cursor(conn, source):
    return conn.execute('SELECT after_seq,journal_id FROM module_router_cursors WHERE source=%s FOR UPDATE',
        (source,)).fetchone()


def _publish(conn, seq, envelope, scope, quarantined):
    try:
        with conn.transaction():
            module_events.publish(conn, envelope, scope)
    except (errors.UniqueViolation, errors.CheckViolation, ModuleContractError) as error:
        # The same id with other fields, or a definer-side refusal: held like a malformed event.
        quarantined.append((seq - 1, seq, 'module_event_conflict' if isinstance(error, errors.UniqueViolation)
            else 'invalid_module_event'))


def commit_page(conn, source, expected, journal_id, next_after, published, quarantined, kind='quarantine'):
    """The only write step of a page: publish, hold refused ranges, advance the cursor; all or nothing.

    Compare-and-set on the cursor read before the network step: a second router or an operator who moved
    it in between makes this page a no-op (False), never a double advance.
    """
    with conn.transaction():
        _bounded(conn)
        row = _locked_cursor(conn, source)
        if row is None or (row['after_seq'], row['journal_id']) != (expected['after_seq'], expected['journal_id']):
            return False
        quarantined = list(quarantined)
        for seq, envelope, scope in published:
            _publish(conn, seq, envelope, scope, quarantined)
        for after, through, code in quarantined:
            _open_gap(conn, source, kind, journal_id, after, through, code)
        conn.execute('''UPDATE module_router_cursors SET after_seq=%s,journal_id=%s,origin='router',
            updated_at=clock_timestamp() WHERE source=%s''', (next_after, journal_id, source))
    return True


def ensure_cursor(conn, module):
    """The stored cursor; on first contact the module's current head, unless the operator set one."""
    row = cursor(conn, module.source)
    if row is not None:
        return row
    head = module.head()
    with conn.transaction():
        _bounded(conn)
        conn.execute('''INSERT INTO module_router_cursors(source,journal_id,after_seq,origin)
            VALUES(%s,%s,%s,'head') ON CONFLICT DO NOTHING''', (module.source, head['journal_id'], head['head']))
    return cursor(conn, module.source)


def _held(conn, source):
    return conn.execute('''SELECT 1 FROM module_router_gaps WHERE source=%s AND kind='source_reset'
        AND resolved_at IS NULL''', (source,)).fetchone() is not None


def read_page(conn, module, row):
    """One page from the stored cursor. Returns 'events', 'more', 'gap', 'reset', 'moved'."""
    after = row['after_seq']
    page = module.events(after, LIMIT)
    check_page(page, after, LIMIT)
    if row['journal_id'] is not None and page['journal_id'] != row['journal_id'] or page['next_after'] < after:
        # A source reset is reported and never read as new events; the operator sets the cursor again.
        with conn.transaction():
            _bounded(conn)
            _locked_cursor(conn, module.source)
            _open_gap(conn, module.source, 'source_reset', page['journal_id'], after, None, 'source_reset')
        return 'reset'
    if page['retention_floor'] > after:
        # Pruned before this cursor read it: name the range, read none of it.
        done = commit_page(conn, module.source, row, page['journal_id'], page['retention_floor'], [],
            [(after, page['retention_floor'], 'retention_gap')], kind='retention_gap')
        return 'gap' if done else 'moved'
    published, quarantined, next_after = split_page(module.source, after, page)
    if not commit_page(conn, module.source, row, page['journal_id'], next_after, published, quarantined):
        return 'moved'
    return 'more' if page['has_more'] else 'events'


def refetch(conn, module, journal_id):
    """Re-read a few quarantined ranges, least recently tried first, so a range that stays broken never
    starves a repaired one. A range resolves only when the module serves at least one event inside it, every
    event inside it passes and is published, and the page covers it; a range the producer now filters
    stays open until the operator abandons it.
    """
    rows = conn.execute('''SELECT id,after_seq,through_seq FROM module_router_gaps WHERE source=%s
        AND kind='quarantine' AND resolved_at IS NULL ORDER BY attempted_at NULLS FIRST,after_seq LIMIT %s''',
        (module.source, REFETCH_PER_PASS)).fetchall()
    for row in rows:
        after, through = row['after_seq'], row['through_seq']
        with conn.transaction():
            _bounded(conn)
            conn.execute('UPDATE module_router_gaps SET attempted_at=clock_timestamp() WHERE id=%s', (row['id'],))
        page = module.events(after, min(LIMIT, through - after))
        check_page(page, after, LIMIT)
        if page['journal_id'] != journal_id or page['retention_floor'] > after:
            continue
        try:
            published, quarantined, next_after = split_page(module.source, after, page)
        except ModuleContractError:
            continue
        inside = [item for item in published if item[0] <= through]
        if not inside or any(low < through for low, _, _ in quarantined) or next_after < through:
            continue
        try:
            with conn.transaction():
                _bounded(conn)
                failed = []
                for seq, envelope, scope in inside:
                    _publish(conn, seq, envelope, scope, failed)
                if failed:
                    raise _Rollback()
                conn.execute('''UPDATE module_router_gaps SET resolved_at=clock_timestamp(),resolution='refetched'
                    WHERE id=%s AND resolved_at IS NULL''', (row['id'],))
        except _Rollback:
            continue


class _Rollback(Exception):
    pass


def poll(conn, module):
    """One pass over one module. Never raises for a module's failure; reports reachability per source."""
    source = module.source
    try:
        if _held(conn, source):
            _log(source, 'source_reset_held')
            return
        row, outcome = ensure_cursor(conn, module), 'events'
        for _ in range(PAGES_PER_PASS):
            outcome = read_page(conn, module, row)
            if outcome not in ('more', 'gap'):
                break
            row = cursor(conn, source)
        report(conn, source, True)
        if outcome in ('reset', 'moved'):
            _log(source, outcome)
            return
        refetch(conn, module, cursor(conn, source)['journal_id'])
    except (ModuleUnavailable, ModuleContractError) as error:
        _log(source, getattr(error, 'code', None) or str(error))
        report(conn, source, False)


def ready(conn):
    return conn.execute("SELECT to_regclass('public.module_router_cursors') IS NOT NULL AS ready").fetchone()['ready']


def connect(dsn):
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=10,
        application_name='module-router')


def run(connect_db, modules, *, interval, stop, once=False):
    """Poll every module each interval on one connection; a database failure reconnects on the next pass."""
    from .telemetry import capture
    conn = None
    while not stop.is_set():
        try:
            if conn is None or conn.closed:
                conn = connect_db()
            if not ready(conn):
                _log(None, 'schema_not_ready')
            else:
                for module in modules:
                    try:
                        poll(conn, module)
                    except psycopg.OperationalError:
                        raise
                    except Exception as error:
                        # One module's unexpected failure never stops the others' pass.
                        capture(error, 'module_router_failed')
                        _log(module.source, 'module_router_failed')
                        if conn.closed or conn.broken:
                            raise
        except Exception as error:
            capture(error, 'module_router_failed')
            _log(None, 'module_router_failed')
            if conn is not None:
                conn.close()
            conn = None
        if once:
            break
        stop.wait(interval)
    if conn is not None:
        conn.close()


# Operator steps (admin CLI, python -m messaging_service): recorded in the router's own rows.
def set_cursor(c, source, after):
    """Set a source's cursor (an earlier `after` replays; publishing is idempotent) and lift a reset hold."""
    if source not in SOURCES or type(after) is not int or not 0 <= after <= MAX_SEQ:
        raise ValueError('invalid_module_cursor')
    previous = cursor(c, source)
    c.execute('''INSERT INTO module_router_cursors(source,journal_id,after_seq,origin) VALUES(%s,NULL,%s,'operator')
        ON CONFLICT(source) DO UPDATE SET journal_id=NULL,after_seq=excluded.after_seq,origin='operator',
        updated_at=clock_timestamp()''', (source, after))
    lifted = c.execute('''UPDATE module_router_gaps SET resolved_at=clock_timestamp(),resolution='abandoned'
        WHERE source=%s AND kind='source_reset' AND resolved_at IS NULL''', (source,)).rowcount
    return {'source': source, 'after': after, 'previous_after': previous['after_seq'] if previous else None,
        'reset_holds_lifted': lifted}


def abandon_gap(c, source, kind, after):
    """The operator accepts that a quarantined or pruned range is never published."""
    if source not in SOURCES or kind not in ('quarantine', 'retention_gap') or type(after) is not int:
        raise ValueError('invalid_module_gap')
    done = c.execute('''UPDATE module_router_gaps SET resolved_at=clock_timestamp(),resolution='abandoned'
        WHERE source=%s AND kind=%s AND after_seq=%s AND resolved_at IS NULL''', (source, kind, after)).rowcount
    if not done:
        raise ValueError('module_gap_not_open')
    return {'source': source, 'kind': kind, 'after': after, 'resolution': 'abandoned'}


def status(c):
    from .store import serial
    return serial({'cursors': c.execute('SELECT * FROM module_router_cursors ORDER BY source').fetchall(),
        'open_gaps': c.execute('''SELECT source,kind,after_seq,through_seq,code,opened_at FROM module_router_gaps
            WHERE resolved_at IS NULL ORDER BY source,after_seq''').fetchall()})


def main():
    from .telemetry import configure
    configure('module_router')
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=Path('/run/secrets/event_router.json'))
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    config = load_config(args.config)
    modules = [HttpModule(item['source'], item['url'], item['token_file'], config['timeout_s'])
        for item in config['modules']]
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    dsn = Path(os.environ['DATABASE_URL_FILE']).read_text().strip()
    run(lambda: connect(dsn), modules, interval=config['interval_s'], stop=stop, once=args.once)


if __name__ == '__main__':
    main()
