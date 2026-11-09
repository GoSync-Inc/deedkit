"""ANYM-908: the one writer of negotiations (ANYM-907), in its own process, role and container.

`python -m messaging_service.negotiation_worker` reads the event journal in id order from its own cursor
(message.created, message.edited, message.deleted) and the rebuild queue. Each pass runs, in this order:
1. the ANYM-907 cleanup checkpoint (again before each apply transaction, at most once a second);
2. one ANYM-907 `apply` per affected chat, in a short bounded transaction that also moves the cursor;
3. publish (ANYM-911 sets PUBLISH; until then nothing publishes).
Every 10 minutes it sweeps open negotiations for the stale rule and rechecks its start fence. It never opens a
provider, sends, calls a model or locks a message row; collectors, sends and hints never import it. Logs carry
counts, message IDs and error classes only, never message text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from . import heartbeats, negotiation_state, negotiation_store
from .conversation_state import group_attention_sql
from .time_references import owner_zone

log = logging.getLogger(__name__)
COMPONENT = 'negotiations'
KINDS = ('message.created', 'message.edited', 'message.deleted')
BATCH = 200  # journal events per batch, rebuild chats per pass
IDLE_S = 2.0  # an empty pass sleeps this long; a busy one loops at once
PERIOD_S = 600  # the stale sweep and the fence recheck
# A chat change that fails is retried after 1, 4 and 16 s; when the third retry fails too, it is skipped and counted.
BACKOFF_S = (1, 4, 16)
WINDOW = negotiation_store.WINDOW
# A session advisory lock on the worker's name: a second instance waits for it.
LOCK_KEY = int.from_bytes(hashlib.sha256(b'negotiation-worker:negotiations').digest()[:8], 'big', signed=True)
# Each grant negotiation_role.sql gives, checked alone (has_column_privilege / has_table_privilege).
COLUMNS = {'probe_metadata': ('name', 'value'),
           'events': ('id', 'account', 'message', 'kind', 'created_at'),
           'messages': ('id', 'account', 'chat', 'provider_id', 'sender', 'direction', 'body', 'sent_at', 'revision',
                        'deleted', 'purged'),
           'chats': ('id', 'account', 'chat_type', 'provider_chat'),
           'message_metadata': ('message', 'revision', 'metadata')}
TABLE_GRANTS = {'component_heartbeats': ('SELECT', 'INSERT', 'UPDATE')}
# ANYM-911's functions: checked only once installed, so this worker ships first.
FUNCTIONS = ('publish_negotiation_event', 'scrub_negotiation_events')
# ANYM-911: a callable(c, transition_row) that publishes one transition inside the caller's transaction.
PUBLISH = None

EVENTS_SQL = '''SELECT e.id, e.kind, e.message, m.account, m.chat, coalesce(m.purged, false) AS purged
  FROM events e LEFT JOIN messages m ON m.id=e.message WHERE e.id>%s ORDER BY e.id LIMIT %s'''
# The journal's events of the last 14 days start the first cursor (events_created_at, 074).
BACKFILL_SQL = '''SELECT coalesce((SELECT min(id) FROM events WHERE created_at>=%s),
  (SELECT max(id)+1 FROM events), 1)-1 AS start'''
SCOPE_SQL = f'''SELECT c.chat_type,
  EXISTS (SELECT 1 FROM messages m WHERE m.account=c.account AND m.chat=c.id AND m.direction<>'outgoing'
    AND m.sent_at>=%(since)s AND NOT m.deleted) AS peer,
  EXISTS (SELECT 1 FROM messages m WHERE m.account=c.account AND m.chat=c.id AND m.direction='outgoing'
    AND m.sent_at>=%(since)s AND NOT m.deleted) AS owner_spoke,
  EXISTS (SELECT 1 FROM messages m LEFT JOIN message_metadata md ON md.message=m.id
    WHERE m.id=ANY(%(messages)s) AND m.account=c.account AND m.chat=c.id
    AND coalesce({group_attention_sql('m', 'md', 'c')}, false)) AS addressed
  FROM chats c WHERE c.account=%(account)s AND c.id=%(chat)s'''
OPEN_SQL = '''SELECT DISTINCT account, chat FROM negotiations WHERE state=ANY(%s) ORDER BY account, chat'''
UNPUBLISHED_SQL = '''SELECT * FROM negotiation_transitions WHERE published_at IS NULL AND invalidated_at IS NULL
  ORDER BY created_at, negotiation, version LIMIT %s'''


class LockLost(Exception):
    """The session holding the worker lock is gone; stop writing until it is taken again."""


class Stopped(Exception):
    """A stop signal during a pass: it ends there, the cursor stays where the last commit put it, nothing skips."""


def connect(dsn):
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row, prepare_threshold=None, connect_timeout=5,
                           options='-c statement_timeout=10000 -c timezone=UTC')


def fence(c):
    """None when every table of ANYM-907 is present with its grants and each grant above is held, else the first
    missing one. ANYM-911's functions count only once installed. Reads only; never writes."""
    try:
        if not negotiation_store.schema_ready(c):return 'negotiation_tables'
        for table, columns in COLUMNS.items():
            if c.execute('SELECT to_regclass(%s) IS NOT NULL AS ok', (f'public.{table}',)).fetchone()['ok'] is not True:
                return table
            for column in columns:
                if c.execute('SELECT has_column_privilege(%s,%s,%s) AS ok', (f'public.{table}', column, 'SELECT')
                             ).fetchone()['ok'] is not True:
                    return f'{table}.{column}'
        for table, privileges in TABLE_GRANTS.items():
            if c.execute('SELECT to_regclass(%s) IS NOT NULL AS ok', (f'public.{table}',)).fetchone()['ok'] is not True:
                return table
            for privilege in privileges:
                if c.execute('SELECT has_table_privilege(%s,%s) AS ok', (f'public.{table}', privilege)
                             ).fetchone()['ok'] is not True:
                    return f'{table}:{privilege}'
        for row in c.execute('''SELECT p.proname, has_function_privilege(p.oid,'EXECUTE') AS ok FROM pg_proc p
                WHERE p.pronamespace='public'::regnamespace AND p.proname=ANY(%s) ORDER BY p.proname''',
                             (list(FUNCTIONS),)).fetchall():
            if row['ok'] is not True:return f"{row['proname']}:EXECUTE"
        return None
    except psycopg.Error as error:
        return 'fence_unreadable:' + type(error).__name__


def publisher_installed(c):
    return c.execute('''SELECT EXISTS (SELECT 1 FROM pg_proc WHERE pronamespace='public'::regnamespace
        AND proname='publish_negotiation_event') AS ok''').fetchone()['ok']


def in_scope(c, account, chat, messages, now):
    """Private chats with a non-owner sender, groups the owner took part in (an outgoing message within 14 days, or
    a message here that mentions or replies to the owner); channels and unknown types never."""
    row = c.execute(SCOPE_SQL, {'account': account, 'chat': chat, 'messages': list(messages),
                                'since': now - WINDOW}).fetchone()
    if row is None:return False
    if row['chat_type'] == 'private':return bool(row['peer'])
    if row['chat_type'] == 'group':return bool(row['owner_spoke'] or row['addressed'])
    return False


def utcnow():
    return datetime.now(timezone.utc)


class Worker:
    def __init__(self, dsn, zone=None, *, stop=None, sleep=None, clock=time.monotonic, now=utcnow):
        self.dsn, self.zone = dsn, zone or owner_zone(os.environ)
        self.stop = stop or threading.Event()
        self.sleep = sleep or self.stop.wait
        self.clock, self.now = clock, now
        self.c = self.lock_c = None
        self.checkpoint = negotiation_store.Checkpoint()
        self.processed = self.skipped = 0
        self.last_sweep = self.last_fence = self.last_beat = self.fence_reason = None

    # Connections -------------------------------------------------------------------------------------------------
    def open(self):
        if self.c is None or self.c.closed or self.c.broken:
            self.close_work()
            self.c = connect(self.dsn)
        return self.c

    def close_work(self):
        if self.c is not None:
            try:self.c.close()
            except psycopg.Error:pass
        self.c = None

    def close(self):
        self.close_work()
        if self.lock_c is not None:
            try:self.lock_c.close()
            except psycopg.Error:pass
        self.lock_c = None

    def try_lock(self):
        """Take the worker lock on a session of its own; False while another instance holds it."""
        if self.lock_c is None or self.lock_c.closed or self.lock_c.broken:self.lock_c = connect(self.dsn)
        return bool(self.lock_c.execute('SELECT pg_try_advisory_lock(%s) AS ok', (LOCK_KEY,)).fetchone()['ok'])

    def acquire(self):
        """Wait for the worker lock; returns False when stopped first."""
        waiting = False
        while not self.stop.is_set():
            if self.try_lock():return True
            if not waiting:log.info('negotiation_worker_waiting lock=held_by_another_instance')
            waiting = True
            self.sleep(IDLE_S)
        return False

    def lock_alive(self):
        try:self.lock_c.execute('SELECT 1')
        except (psycopg.Error, AttributeError) as error:raise LockLost(type(error).__name__) from None

    # Start -------------------------------------------------------------------------------------------------------
    def start(self):
        """The start fence, then the lock, then the first cursor. Returns None, or the fence's reason with nothing
        written."""
        c = self.open()
        reason = fence(c)
        if reason is not None:return reason
        if not self.acquire():return 'stopped'
        self.init_cursor(c)
        self.last_fence = self.clock()
        version = c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()
        log.info('negotiation_worker_started owner_zone=%s schema=%s', self.zone[1], version and version['value'])
        return None

    def init_cursor(self, c):
        """On first start the cursor begins at the journal's events of the last 14 days."""
        with c.transaction():
            negotiation_store._bounded(c)
            if c.execute('SELECT 1 FROM negotiation_cursor WHERE id=1').fetchone() is None:
                start = c.execute(BACKFILL_SQL, (self.now() - WINDOW,)).fetchone()['start']
                c.execute('INSERT INTO negotiation_cursor(id,event_id,updated_at) VALUES(1,%s,%s)',
                          (max(start, 0), self.now()))
                log.info('negotiation_backfill_started from_event=%d', max(start, 0))

    # One pass ----------------------------------------------------------------------------------------------------
    def check(self):
        """The ANYM-907 cleanup checkpoint; a failure is logged, and its next call retries it."""
        try:return self.checkpoint(self.open(), self.now())
        except Exception as error:  # noqa: BLE001 - a failed cleanup never stops apply; reads hide purges anyway
            log.warning('negotiation_cleanup_failed error=%s', type(error).__name__)
            return 0

    def change(self, work, message=None):
        """One chat change in its own bounded transaction, retried after 1, 4 and 16 s; when the third retry fails
        too it is skipped (counted, logged with its message ID and error class). True when it committed."""
        for failure in range(1, len(BACKOFF_S) + 2):
            self.check()
            c = self.open()
            try:
                with c.transaction():
                    negotiation_store._bounded(c)
                    work(c, self.now())
                return True
            except Exception as error:  # noqa: BLE001 - one chat never stops the journal
                name = type(error).__name__
                if failure <= len(BACKOFF_S):
                    log.warning('negotiation_apply_failed message=%s error=%s attempt=%d', message, name, failure)
                    if c.closed or c.broken:self.close_work()
                    self.sleep(BACKOFF_S[failure - 1])
                    if self.stop.is_set():raise Stopped() from None
                    continue
                if c.closed or c.broken:self.close_work()
                self.skipped += 1
                log.warning('negotiation_event_skipped message=%s error=%s', message, name)
        return False

    def move(self, c, event_id, now):
        c.execute('UPDATE negotiation_cursor SET event_id=greatest(event_id,%s),updated_at=%s WHERE id=1',
                  (event_id, now))

    def journal(self):
        """One batch of at most BATCH events: each affected chat's apply moves the cursor to that chat's last event,
        chats in the order of their last event, so a stop never moves the cursor past a chat not yet applied."""
        c = self.open()
        row = c.execute('SELECT event_id FROM negotiation_cursor WHERE id=1').fetchone()
        events = c.execute(EVENTS_SQL, (row['event_id'], BATCH)).fetchall()
        if not events:return 0
        chats = {}
        for e in events:
            if e['kind'] not in KINDS or e['chat'] is None:continue
            item = chats.setdefault((e['account'], e['chat']), {'last': 0, 'messages': [], 'deletion': False})
            item['last'], item['message'] = e['id'], e['message']
            item['messages'].append(e['message'])
            item['deletion'] = item['deletion'] or e['kind'] == 'message.deleted' or e['purged']
            self.processed += 1
        for (account, chat), item in sorted(chats.items(), key=lambda pair: pair[1]['last']):
            if self.stop.is_set():raise Stopped()
            # Deletions refold before the scope check, also in a chat that is now out of scope.
            if not item['deletion'] and not in_scope(self.open(), account, chat, item['messages'], self.now()):continue
            def work(c, now, account=account, chat=chat, last=item['last']):
                negotiation_store.apply(c, account, chat, now, self.zone)
                self.move(c, last, now)
            self.change(work, item['message'])
        last = events[-1]['id']
        self.change(lambda c, now: self.move(c, last, now))
        return len(events)

    def rebuild(self):
        """The rebuild queue (a purge's chats), oldest first, each taken and applied in one transaction."""
        done = 0
        while done < BATCH and not self.stop.is_set():
            taken = []
            def work(c, now):
                taken[:] = negotiation_store.take_rebuild(c, 1)
                for account, chat in taken:negotiation_store.apply(c, account, chat, now, self.zone)
            if not self.change(work):
                # Skipped after its failures: drop that one chat from the queue, so the next one goes on.
                if taken:
                    account, chat = taken[0]
                    self.change(lambda c, now: c.execute(
                        'DELETE FROM negotiation_rebuild WHERE account=%s AND chat=%s', (account, chat)))
                    done += 1
                    continue
                break
            if not taken:break
            done += 1
        return done

    def sweep(self):
        """ANYM-893's stale rule for every chat with an open negotiation, each under its chat lock (apply)."""
        chats = self.open().execute(OPEN_SQL, (list(negotiation_state.OPEN),)).fetchall()
        for row in chats:
            if self.stop.is_set():break
            self.change(lambda c, now, a=row['account'], ch=row['chat']: negotiation_store.apply(c, a, ch, now, self.zone))
        return len(chats)

    def publish(self):
        """ANYM-911: each unpublished transition in its own transaction, cleaned first when a source is purged."""
        publish_one = PUBLISH
        if publish_one is None or not publisher_installed(self.open()):return 0
        published = 0
        for t in self.open().execute(UNPUBLISHED_SQL, (BATCH,)).fetchall():
            sent = []
            def work(c, now, t=t, sent=sent):
                sent.clear()  # a rolled-back attempt published nothing
                if negotiation_store.clean_if_purged(c, t['negotiation'], now):return
                # Read again under the chat lock: a cleanup may have invalidated it since the list was read.
                row = c.execute('''SELECT * FROM negotiation_transitions WHERE negotiation=%s AND version=%s
                    AND published_at IS NULL AND invalidated_at IS NULL''', (t['negotiation'], t['version'])).fetchone()
                if row is not None:
                    publish_one(c, row)
                    sent.append(1)
            self.change(work, t['trigger_message'])
            published += len(sent)
        return published

    def run_pass(self):
        result = {'cleaned': self.check()}
        result['events'] = self.journal()
        result['rebuilt'] = self.rebuild()
        at = self.clock()
        if self.last_sweep is None or at - self.last_sweep >= PERIOD_S:
            self.last_sweep = at
            result['swept'] = self.sweep()
            log.info('negotiation_worker_totals processed=%d skipped=%d', self.processed, self.skipped)
        result['published'] = self.publish()
        return result

    @staticmethod
    def busy(result):
        return bool(result.get('events') or result.get('rebuilt') or result.get('published'))

    def beat(self):
        at = self.clock()
        if self.last_beat is None or at - self.last_beat >= heartbeats.HEARTBEAT_S:
            # A failed write never stops the worker.
            try:heartbeats.beat(self.open(), COMPONENT)
            except Exception:pass  # noqa: BLE001,S110
            self.last_beat = at

    def fenced(self):
        """The fence again every 10 minutes; while a grant is missing the worker writes nothing and rechecks."""
        at = self.clock()
        if self.last_fence is not None and at - self.last_fence < PERIOD_S:return False
        reason = fence(self.open())
        if reason is None:
            if self.fence_reason is not None:log.info('negotiation_worker_ready')
            self.last_fence, self.fence_reason = at, None
            return False
        if reason != self.fence_reason:log.warning('negotiation_worker_not_ready reason=%s', reason)
        self.last_fence, self.fence_reason = None, reason
        return True

    def run(self, once=False):
        try:
            reason = self.start()
            if reason == 'stopped':return 0
            if reason is not None:
                print(json.dumps({'status': 'not_ready', 'reason': reason}), flush=True)
                return 3
            while not self.stop.is_set():
                try:
                    self.lock_alive()
                    if self.fenced():
                        self.sleep(IDLE_S)
                        continue
                    result = self.run_pass()
                    self.beat()
                    if once:
                        print(json.dumps(result), flush=True)
                        return 0
                    if not self.busy(result):self.sleep(IDLE_S)
                except Stopped:
                    break
                except LockLost:
                    log.warning('negotiation_worker_lock_lost')
                    self.close()
                    if not self.acquire():break
                except Exception as error:  # noqa: BLE001 - a failed pass is logged by class and retried
                    log.warning('negotiation_pass_failed error=%s', type(error).__name__)
                    if self.c is not None and (self.c.closed or self.c.broken):self.close_work()
                    if once:return 1
                    self.sleep(IDLE_S)
            return 0
        finally:
            self.close()


def status(dsn, environ=os.environ):
    """`production.py status`: the owner-zone label and this worker's heartbeat row; read-only."""
    with connect(dsn) as c:
        row = c.execute('SELECT version, beat_at FROM component_heartbeats WHERE component=%s', (COMPONENT,)).fetchone()
    return {'owner_zone': owner_zone(environ)[1],
            'heartbeat': {'version': row['version'], 'beat_at': row['beat_at'].isoformat()} if row else None}


def main(argv=None):
    from .store import name_sessions
    name_sessions('negotiations')
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true', help='print the owner-zone label and the heartbeat, read-only')
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format='%(message)s')
    dsn = Path(os.environ['DATABASE_URL_FILE']).read_text().strip()
    if args.status:
        print(json.dumps(status(dsn)), flush=True)
        return 0
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):signal.signal(sig, lambda *_: stop.set())
    return Worker(dsn, stop=stop).run(once=args.once)


if __name__ == '__main__':
    sys.exit(main())
