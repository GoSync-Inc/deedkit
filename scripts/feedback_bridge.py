#!/usr/bin/env python3
"""Two-way bridge between DeedKit agent feedback and BB Tasks, run on the owner's Ryzen host (ANYM-990).

    feedback_bridge.py watch      long-poll the maintainer subscription; apply, ACK, reverse (systemd --user unit)
    feedback_bridge.py run        one forward + reverse cycle (the 10-minute bb script automation; exit 75 when busy)
    feedback_bridge.py reverse    one reverse pass (task changes to the feedback status)
    feedback_bridge.py check      read-only: env, file modes, tracker, labels

Reads feedback metadata through the DeedKit gateway with a maintainer key (messages_export_feedback_metadata, the
maintainer subscription of ANYM-989) and writes tasks through the local `bb tasks` CLI. No raw feedback text is ever
read: only the whitelisted metadata fields and the loop's released brief reach a task. State lives in one SQLite
ledger under ~/.local/state/deedkit-feedback-bridge; one flock serializes every writer. The host listens on nothing.
Exit 0 done, 1 error (one fixed line on stderr), 75 busy. Standard library only.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import random
import re
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

LABEL = 'deedkit-feedback-bridge'
AUTHOR = 'deedkit-feedback-bridge'
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
ID_LINE = re.compile(r'Feedback-ID: (%s)' % UUID)
DUP_LINE = re.compile(r'^Duplicate-Of: (%s)[ \t]*$' % UUID, re.MULTILINE)
WORD = re.compile(r'[A-Za-z0-9_.:@/ -]{1,64}')
STAMP = re.compile(r'[0-9TZ:.+-]{10,40}')
SPEC = re.compile(r'ANYM-[1-9][0-9]*')
SLUG = re.compile(r'[a-z0-9][a-z0-9-]{2,60}')
SHA = re.compile(r'[0-9a-f]{40}')
CODE = re.compile(r'[a-z][a-z0-9_]{0,63}')
FORWARD = {'received': 'backlog', 'accepted': 'todo', 'fixed': 'in_review', 'verify_requested': 'in_review',
    'verified': 'done', 'declined': 'canceled', 'duplicate': 'canceled', 'reopened': 'backlog'}
COMPATIBLE = {'received': {'backlog'}, 'accepted': {'todo', 'in_progress', 'in_review'}, 'fixed': {'in_review'},
    'verify_requested': {'in_review'}, 'verified': {'done'}, 'declined': {'canceled'}, 'duplicate': {'canceled'},
    'reopened': {'backlog'}}
OPEN = ('received', 'accepted', 'reopened', 'fixed', 'verify_requested')
STATUSES = 'backlog,todo,in_progress,in_review,done,canceled'
CLOSED = ('subscription_closed', 'subscription_access_changed', 'not_found')
UNAVAILABLE = ('forbidden', 'unauthorized', 'unsupported_operation', 'not_found', 'unknown_tool')
MAX_EXPORT_PAGES, MAX_LIST_PAGES, DEAD = 50, 20, ('closed', 'expired', 'access_changed', 'revoked')
BACKOFF_MIN, BACKOFF_MAX, MIN_PASS, REVERSE_EVERY, LOCK_WAIT = 1, 60, 1, 120, 300
DONE_NOTE = ('Done in BB is not fixed or deployed in DeedKit: `fixed` and `verify_requested` come only from release '
    'evidence, so this task\'s status was not written back (DeedKit status: %s).')


class BridgeError(Exception):
    """A failure with a fixed code; never carries a value, a path or a response body."""

    def __init__(self, code, status=None):
        super().__init__(code)
        self.code, self.status = code, status


class ConfigError(BridgeError):
    pass


class Uncertain(BridgeError):
    """The outcome of a call is unknown (timeout, connection loss, 5xx, an unreadable answer)."""


class Refused(BridgeError):
    """The other side definitely did not act."""


class Incomplete(BridgeError):
    """A listing could not be read to its end: it proves nothing, least of all an absence."""


class Busy(Exception):
    pass


def log(code):
    print('feedback-bridge: %s' % code, file=sys.stderr, flush=True)


@dataclass
class Config:
    gateway_url: str
    key: str
    subscription_id: str | None
    tracker: str
    backfill_days: int
    state_dir: Path


def _private(path):
    """The text of a regular file that only its owner can touch (no symlink, no group or other bit)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ConfigError('file_unreadable') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ConfigError('file_mode')
        return os.read(fd, 65536).decode('utf-8', 'replace')
    finally:
        os.close(fd)


def load_config(path=None, environ=None):
    """The private env file (0600, no shell evaluation) and the key file it names; refuses anything looser."""
    environ = os.environ if environ is None else environ
    path = Path(path or environ.get('DEEDKIT_FEEDBACK_BRIDGE_ENV') or Path.home() / '.config/deedkit/feedback-bridge.env')
    values = {}
    for line in _private(path).splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            name, _, value = line.partition('=')
            values[name.strip()] = value.strip().strip('\'"')
    url, key_file = values.get('DEEDKIT_GATEWAY_URL'), values.get('DEEDKIT_FEEDBACK_KEY_FILE')
    if not url or not key_file:
        raise ConfigError('env_incomplete')
    parts = urllib.parse.urlsplit(url)
    if not (parts.scheme == 'https' or (parts.scheme == 'http' and parts.hostname in ('127.0.0.1', 'localhost', '::1'))):
        raise ConfigError('gateway_url_not_https')
    key = _private(key_file).strip()
    if not key:
        raise ConfigError('key_empty')
    try:
        days = int(values.get('DEEDKIT_BRIDGE_BACKFILL_DAYS', '14'))
    except ValueError:
        raise ConfigError('env_invalid') from None
    state = Path(values.get('DEEDKIT_BRIDGE_STATE_DIR') or Path.home() / '.local/state/deedkit-feedback-bridge')
    return Config(url.rstrip('/'), key, values.get('DEEDKIT_FEEDBACK_SUBSCRIPTION_ID') or None,
        values.get('DEEDKIT_BRIDGE_TRACKER') or 'DK', days, state)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Gateway:
    """Bounded REST calls (POST /v1/<tool>) with the maintainer key; no redirects, no retries, no body in any error."""

    def __init__(self, url, key):
        self.url, self.key = url, key
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    def call(self, tool, args, timeout=30):
        request = urllib.request.Request('%s/v1/%s' % (self.url, tool), data=json.dumps(args).encode(), method='POST',
            headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                body = response.read(8 << 20)
        except urllib.error.HTTPError as error:
            status, body = error.code, error.read(65536)
            if 300 <= status < 400:
                raise Refused('redirect_rejected', status) from None
            if status >= 500:
                raise Uncertain('gateway_http_%d' % status, status) from None
            try:
                code = json.loads(body).get('error')
            except (ValueError, AttributeError):
                code = None
            raise Refused(code if isinstance(code, str) and CODE.fullmatch(code) else 'http_error', status) from None
        except (urllib.error.URLError, OSError, socket.timeout, http.client.HTTPException):
            raise Uncertain('gateway_unreachable') from None
        try:
            value = json.loads(body)
        except ValueError:
            raise Uncertain('gateway_bad_response') from None
        if not isinstance(value, dict):
            raise Uncertain('gateway_bad_response')
        return value


def _run(argv, timeout):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return done.returncode, done.stdout


class BbTasks:
    """The local `bb tasks` CLI of one tracker project. A create whose outcome is unknown raises Uncertain."""

    def __init__(self, tracker, runner=None, state_dir=None):
        self.tracker, self.runner = tracker, runner or _run
        self.state_dir = Path(state_dir or Path.home() / '.local/state/deedkit-feedback-bridge')

    def _call(self, args, timeout=60):
        try:
            code, out = self.runner(['bb', 'tasks', *args, '--json'], timeout)
        except subprocess.TimeoutExpired:
            raise Uncertain('tasks_timeout') from None
        except OSError:
            raise Refused('tasks_unavailable') from None
        try:
            data = json.loads(out)
        except ValueError:
            raise Uncertain('tasks_bad_output') from None
        if isinstance(data, dict) and data.get('ok') is False:
            error = data.get('error')
            code = error.get('code') if isinstance(error, dict) else None
            raise Refused(code if isinstance(code, str) and CODE.fullmatch(code) else 'tasks_error')
        if code != 0 or not isinstance(data, dict):
            raise Uncertain('tasks_failed')
        return data

    @contextmanager
    def _file(self, text):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.state_dir, prefix='tmp', delete=False)
        try:
            with handle:
                handle.write(text)
            yield handle.name
        finally:
            Path(handle.name).unlink(missing_ok=True)

    @staticmethod
    def _task(data):
        task = data.get('task', data)
        if not isinstance(task, dict) or not isinstance(task.get('id'), str) or not isinstance(task.get('key'), str):
            raise Uncertain('tasks_bad_output')
        names = data.get('labels') if isinstance(data.get('labels'), list) else task.get('labels')
        return task | {'labels': [x if isinstance(x, str) else x.get('name') for x in names or []]}

    def create(self, title, description, labels):
        with self._file(description) as path:
            args = ['create', '--project', self.tracker, '--title', title, '--description-file', path, '--priority', 'none']
            return self._task(self._call(args + [x for name in labels for x in ('--label', name)]))

    def get(self, ref):
        return self._task(self._call(['show', ref]))

    def update(self, ref, status=None, title=None, description=None, add_labels=()):
        args = ['update', ref] + (['--status', status] if status else []) + (['--title', title] if title else [])
        args += [x for name in add_labels for x in ('--add-label', name)]
        if description is None:
            return self._task(self._call(args))
        with self._file(description) as path:
            return self._task(self._call(args + ['--description-file', path]))

    def comment(self, ref, body):
        with self._file(body) as path:
            self._call(['comment', ref, '--body-file', path, '--author', AUTHOR])

    def list_tasks(self, label=None):
        found, cursor = [], None
        for _ in range(MAX_LIST_PAGES):
            args = ['list', '--project', self.tracker, '--status', STATUSES, '--limit', '500']
            data = self._call(args + (['--label', label] if label else []) + (['--cursor', cursor] if cursor else []))
            if not isinstance(data.get('tasks'), list):
                raise Uncertain('tasks_bad_output')
            found += [self._task({'task': t}) for t in data['tasks']]
            cursor = data.get('nextCursor')
            if not cursor:
                return found
        raise Incomplete('tasks_listing_incomplete')

    def projects(self):
        return self._call(['project', 'list']).get('projects') or []

    def label_names(self):
        return [x.get('name') for x in self._call(['label', 'list', '--project', self.tracker]).get('labels') or []]

    def ensure_labels(self, names):
        have = set(self.label_names())
        for name in names:
            if name not in have:
                self._call(['label', 'create', '--project', self.tracker, '--name', name])


class Ledger:
    """One SQLite file, WAL and synchronous=FULL: a committed row survives a crash. One writer holds the flock."""

    COLUMNS = ('fb_status', 'fb_changed_at', 'fp', 'title', 'desc_sha', 'bridge_version', 'seen_version')

    def __init__(self, state_dir):
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        self.db = sqlite3.connect(state_dir / 'ledger.sqlite3', timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        for statement in (
                'CREATE TABLE IF NOT EXISTS links(fid TEXT PRIMARY KEY, task_id TEXT, task_key TEXT, skipped INTEGER NOT NULL '
                'DEFAULT 0, fb_status TEXT, fb_changed_at TEXT, fp TEXT, title TEXT, desc_sha TEXT, bridge_version TEXT, '
                'seen_version TEXT)',
                'CREATE UNIQUE INDEX IF NOT EXISTS links_task ON links(task_id) WHERE task_id IS NOT NULL',
                'CREATE TABLE IF NOT EXISTS pending(fid TEXT PRIMARY KEY, started REAL NOT NULL)',
                'CREATE TABLE IF NOT EXISTS pending_write(rid TEXT PRIMARY KEY, fid TEXT NOT NULL, version TEXT NOT NULL, '
                'status TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS notes(key TEXT PRIMARY KEY)',
                'CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)',
                'CREATE TABLE IF NOT EXISTS unresolved(fid TEXT PRIMARY KEY, attempts INTEGER NOT NULL)'):
            self.db.execute(statement)
        for suffix in ('', '-wal', '-shm'):
            (state_dir / ('ledger.sqlite3' + suffix)).exists() and (state_dir / ('ledger.sqlite3' + suffix)).chmod(0o600)

    def one(self, sql, *args):
        row = self.db.execute(sql, args).fetchone()
        return None if row is None else dict(row)

    @contextmanager
    def tx(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield self.db
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        self.db.execute('COMMIT')

    def meta(self, key):
        row = self.one('SELECT value FROM meta WHERE key=?', key)
        return None if row is None else row['value']

    def set_meta(self, key, value):
        self.db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def del_meta(self, key):
        self.db.execute('DELETE FROM meta WHERE key=?', (key,))

    def link(self, fid):
        return self.one('SELECT * FROM links WHERE fid=?', fid)

    def link_by_task(self, task_id):
        return self.one('SELECT * FROM links WHERE task_id=?', task_id)

    def skip(self, fid, status):
        self.db.execute('INSERT OR IGNORE INTO links(fid,skipped,fb_status) VALUES(?,1,?)', (fid, status))

    def pending(self, fid):
        return self.one('SELECT * FROM pending WHERE fid=?', fid)

    def begin_create(self, fid, now):
        self.db.execute('INSERT OR IGNORE INTO pending(fid,started) VALUES(?,?)', (fid, now))

    def clear_pending(self, fid):
        self.db.execute('DELETE FROM pending WHERE fid=?', (fid,))

    def link_task(self, fid, task):
        with self.tx() as db:
            db.execute('INSERT OR REPLACE INTO links(fid,task_id,task_key,skipped,title,desc_sha,bridge_version) '
                'VALUES(?,?,?,0,?,?,?)', (fid, task['id'], task['key'], task['title'], sha(task['description']),
                task.get('updatedAt')))
            db.execute('DELETE FROM pending WHERE fid=?', (fid,))

    def update_link(self, fid, **fields):
        assert set(fields) <= set(self.COLUMNS)
        if fields:
            self.db.execute('UPDATE links SET %s WHERE fid=?' % ','.join('%s=?' % k for k in fields), (*fields.values(), fid))

    def mark(self, key):
        return self.db.execute('INSERT OR IGNORE INTO notes(key) VALUES(?)', (key,)).rowcount == 1

    def unmark(self, key):
        self.db.execute('DELETE FROM notes WHERE key=?', (key,))

    def bump_unresolved(self, fid):
        self.db.execute('INSERT INTO unresolved(fid,attempts) VALUES(?,1) ON CONFLICT(fid) DO UPDATE SET attempts=attempts+1', (fid,))
        return self.unresolved(fid)

    def unresolved(self, fid):
        row = self.one('SELECT attempts FROM unresolved WHERE fid=?', fid)
        return 0 if row is None else row['attempts']

    def put_pending_write(self, rid, fid, version, status):
        self.db.execute('INSERT OR IGNORE INTO pending_write(rid,fid,version,status) VALUES(?,?,?,?)', (rid, fid, version, status))

    def drop_pending_write(self, rid):
        self.db.execute('DELETE FROM pending_write WHERE rid=?', (rid,))


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def request_id(feedback_id, task_id, version):
    return sha('\0'.join((feedback_id, task_id, str(version))))[:32]


def first_line_id(description):
    """The feedback ID of a task: only an exact first line `Feedback-ID: <uuid>` counts (no mention deeper in the text)."""
    match = ID_LINE.fullmatch(str(description).split('\n', 1)[0].rstrip('\r'))
    return match.group(1) if match else None


def word(value):
    return value if isinstance(value, str) and WORD.fullmatch(value) else 'unknown'


def pick(value, pattern):
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def clean(text, limit):
    text = ''.join(c for c in str(text) if unicodedata.category(c) not in ('Cc', 'Cf', 'Cs', 'Co') or c.isspace())
    return ' '.join(text.split())[:limit]


def stamp(value):
    return value if isinstance(value, str) and STAMP.fullmatch(value) else None


def normalize(raw):
    """The whitelisted, validated fields of one metadata row; None for a row that cannot be keyed or understood."""
    if not isinstance(raw, dict) or not isinstance(raw.get('feedback_id'), str) or not re.fullmatch(UUID, raw['feedback_id']):
        return None
    created = stamp(raw.get('created_at'))
    try:
        created_ts = datetime.fromisoformat(created.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError):
        return None
    if raw.get('status') not in FORWARD:
        return None
    given = raw.get('projection') if isinstance(raw.get('projection'), dict) else None
    brief = clean(given.get('released_brief') or '', 600) if given else ''
    shown = None if given is None else {'classification': pick(given.get('classification'), re.compile(r'[a-z_-]{1,40}')),
        'reason_code': pick(given.get('reason_code'), re.compile(r'[a-z_-]{1,40}')),
        'primary_spec': pick(given.get('primary_spec'), SPEC), 'new_card': pick(given.get('new_card'), SLUG), 'brief': brief}
    return {'feedback_id': raw['feedback_id'], 'principal': word(raw.get('principal')), 'category': word(raw.get('category')),
        'operation': word(raw.get('operation')), 'created_at': created, 'created_ts': created_ts, 'status': raw['status'],
        'status_changed_at': stamp(raw.get('status_changed_at')), 'fixed_in': pick(raw.get('fixed_in'), SHA),
        'spec_ref': pick(raw.get('spec_ref'), SPEC), 'duplicate_of': pick(raw.get('duplicate_of'), re.compile(UUID)),
        'projection': shown}


def render(raw):
    """(title, description) of a task from the whitelisted fields; the brief is quoted so it can forge no line."""
    item = raw if 'created_ts' in raw else normalize(raw)
    shown = item['projection']
    brief = shown['brief'] if shown else ''
    if brief:
        title = ''
        for part in brief.split(' '):
            if len(title) + len(part) + 1 > 80:
                title = (title or part[:79]).rstrip() + '…'
                break
            title = (title + ' ' + part).strip()
    else:
        title = '%s feedback: %s (metadata only)' % (item['category'], item['operation'])
    lines = ['Feedback-ID: ' + item['feedback_id'], '', 'Source: %s · %s · %s · received %s' % (
        item['principal'], item['category'], item['operation'], item['created_at']),
        'DeedKit status: %s (changed %s)' % (item['status'], item['status_changed_at'] or 'n/a')]
    if item['fixed_in']:
        lines.append('Fixed in: ' + item['fixed_in'])
    if item['duplicate_of']:
        lines.append('Duplicate of: ' + item['duplicate_of'])
    if shown and (shown['classification'] or shown['primary_spec'] or shown['new_card']):
        lines.append('Triage: %s / %s; card %s; new card %s' % (shown['classification'] or 'n/a', shown['reason_code'] or 'n/a',
            shown['primary_spec'] or 'none', shown['new_card'] or 'none'))
    lines += ['', 'Summary (the loop\'s released brief: scrubbed model output from untrusted text):'] + (
        ['> ' + brief] if brief else ['Metadata only: the loop has released no summary for this item yet.'])
    lines += ['', 'This task mirrors a DeedKit feedback item. Done in BB is not fixed or deployed: only release evidence '
        'sets `fixed` and `verify_requested`.', '']
    return title, '\n'.join(lines)


def reverse_status(task_status, labels, feedback_status, description):
    """(status, duplicate_of) a task-side change asks DeedKit for, or None. Only accepted, declined, duplicate exist."""
    if task_status in ('todo', 'in_progress', 'in_review') and feedback_status in ('received', 'reopened'):
        return 'accepted', None
    if task_status == 'canceled' and feedback_status in ('received', 'accepted', 'reopened'):
        if 'duplicate' in labels:
            match = DUP_LINE.search(description or '')
            return ('duplicate', match.group(1)) if match else None
        return 'declined', None
    return None


class Bridge:
    def __init__(self, cfg, gateway, tasks, now=time.time, rand=random.random):
        self.cfg, self.gateway, self.tasks, self.now, self.rand = cfg, gateway, tasks, now, rand
        self.ledger, self.labels = Ledger(cfg.state_dir), set()

    @contextmanager
    def lock(self, block=False):
        fd = os.open(self.cfg.state_dir / 'bridge.lock', os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + LOCK_WAIT
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if not block or time.monotonic() > deadline:
                        raise Busy from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    # ---- subscription ----
    def ensure_subscription(self):
        known = self.cfg.subscription_id or self.ledger.meta('subscription_id')
        if known:
            return known
        if self.ledger.meta('subscription_pending'):
            subs = self.gateway.call('messages_list_subscriptions', {'limit': 100}).get('subscriptions')
            if not isinstance(subs, list) or len(subs) >= 100:
                raise Incomplete('subscriptions_listing_incomplete')
            live = sorted(s['subscription_id'] for s in subs if isinstance(s, dict) and s.get('label') == LABEL
                and s.get('state') not in DEAD and isinstance(s.get('subscription_id'), str))
            if live:
                with self.ledger.tx():
                    self.ledger.set_meta('subscription_id', live[0])
                    self.ledger.del_meta('subscription_pending')
                return live[0]
        else:
            self.ledger.set_meta('subscription_pending', '1')
        made = self.gateway.call('messages_create_subscription', {'feedback': {'maintainer': True}, 'label': LABEL})
        if not isinstance(made.get('subscription_id'), str):
            raise Uncertain('gateway_bad_response')
        with self.ledger.tx():
            self.ledger.set_meta('subscription_id', made['subscription_id'])
            self.ledger.del_meta('subscription_pending')
        return made['subscription_id']

    def events(self, tool, sid, args, timeout=30):
        try:
            return self.gateway.call(tool, {'subscription_id': sid} | args, timeout)
        except Refused as error:
            if error.code in CLOSED and not self.cfg.subscription_id:
                self.ledger.del_meta('subscription_id')
            raise

    # ---- forward ----
    def wanted(self, item):
        return item['status'] in OPEN and self.now() - item['created_ts'] <= self.cfg.backfill_days * 86400

    def once_comment(self, key, task_key, body):
        if not self.ledger.mark(key):
            return
        try:
            self.tasks.comment(task_key, body)
        except Refused:
            self.ledger.unmark(key)
            raise

    def reconcile(self, fid):
        """A pending create: the complete listing and the exact first line decide; no listing, no create."""
        found = sorted((t for t in self.tasks.list_tasks() if first_line_id(t.get('description')) == fid),
            key=lambda t: t.get('number') or 0)
        if not found:
            self.ledger.clear_pending(fid)
            return None
        self.ledger.link_task(fid, found[0])
        for extra in found[1:]:
            self.once_comment('double:%s:%s' % (fid, extra['key']), extra['key'],
                'This task duplicates %s for feedback %s; the bridge follows %s.' % (found[0]['key'], fid, found[0]['key']))
        return found[0]

    def create(self, item):
        names = ['feedback', 'fb-' + item['category'].replace(' ', '-').replace('/', '-').replace(':', '-')]
        if not self.labels >= set(names):
            self.tasks.ensure_labels(names)
            self.labels |= set(names)
        title, description = render(item)
        self.ledger.begin_create(item['feedback_id'], self.now())
        task = {'title': title, 'description': description} | self.tasks.create(title, description, names)
        self.ledger.link_task(item['feedback_id'], task)
        return task

    def apply(self, raw, stats):
        item = normalize(raw)
        if item is None:
            log('item_malformed')
            return
        fid = item['feedback_id']
        link, task = self.ledger.link(fid), None
        if link is None:
            if not self.wanted(item):
                self.ledger.skip(fid, item['status'])
                return
            task = self.reconcile(fid) if self.ledger.pending(fid) else None
            if task is None:
                task = self.create(item)
                stats['created'] += 1
            link = self.ledger.link(fid)
        elif link['skipped']:
            return
        title, description = render(item)
        fp = sha(title + '\n' + description)
        if link['fp'] != fp:
            self.update_task(link, item, title, description, fp, task, stats)

    def update_task(self, link, item, title, description, fp, task, stats):
        fid, key = item['feedback_id'], link['task_key']
        task = task or self.tasks.get(key)
        changes = {}
        if task['status'] not in COMPATIBLE[item['status']]:
            changes['status'] = FORWARD[item['status']]
        untouched = task['title'] == link['title'] and sha(task['description'] or '') == link['desc_sha']
        if untouched:
            changes |= {name: value for name, value in (('title', title), ('description', description))
                if value != task[name]}
        elif link['fp'] != fp:
            brief = item['projection']['brief'] if item['projection'] else ''
            self.once_comment('edited:%s:%s' % (fid, fp), key, 'DeedKit status: %s.%s' % (item['status'],
                '\n> ' + brief if brief else ''))
        fields = {'fb_status': item['status'], 'fb_changed_at': item['status_changed_at'], 'fp': fp}
        if changes:
            result = self.tasks.update(key, **changes)
            stats['updated'] += 1
            fields |= {'title': result.get('title', changes.get('title', task['title'])),
                'desc_sha': sha(result['description'] if 'description' in result else changes.get('description', task['description'])),
                'bridge_version': result.get('updatedAt') or self.tasks.get(key).get('updatedAt')}
        if item['status'] == 'duplicate' and item['duplicate_of']:
            self.once_comment('dup:%s' % fid, key, 'Duplicate of feedback %s.' % item['duplicate_of'])
        self.ledger.update_link(fid, **fields)

    def export_items(self, handler, save):
        cursor = self.ledger.meta('export_cursor')
        for _ in range(MAX_EXPORT_PAGES):
            data = self.gateway.call('messages_export_feedback_metadata', {'limit': 100} | ({'cursor': cursor} if cursor else {}))
            if not isinstance(data.get('items'), list):
                raise Uncertain('gateway_bad_response')
            for raw in data['items']:
                handler(raw)
            following = data.get('next_cursor')
            if save and isinstance(following, str) and following:
                self.ledger.set_meta('export_cursor', following)
            if not data.get('has_more') or not isinstance(following, str):
                return
            cursor = following

    def forward(self, page=None):
        sid = self.ensure_subscription()
        if page is None:
            page = self.events('messages_fetch_subscription_events', sid, {'limit': 100, 'detail': 'compact'})
        stats = {'created': 0, 'updated': 0}
        listed = (page.get('module') or {}).get('items') or []
        ids = [x['data']['feedback_id'] for x in listed if isinstance(x, dict) and isinstance(x.get('data'), dict)
            and isinstance(x['data'].get('feedback_id'), str) and re.fullmatch(UUID, x['data']['feedback_id'])]
        self.export_items(lambda raw: self.apply(raw, stats), save=True)
        if listed:
            if not page.get('receipt') or not isinstance(page.get('ack_version'), int):
                log('ack_unavailable')
            elif all([self.ledger.link(f) is not None or self.ledger.bump_unresolved(f) >= 3 for f in ids]):
                self.gateway.call('messages_ack_subscription_events', {'subscription_id': sid, 'receipt': page['receipt'],
                    'version': page['ack_version']})
        return stats

    # ---- reverse ----
    def readback(self):
        """The metadata rows from the stored cursor on (nothing saved), by feedback ID: what the server holds now."""
        found = {}

        def note(raw):
            item = normalize(raw)
            if item is not None:
                found[item['feedback_id']] = item
        self.export_items(note, save=False)
        return found

    def write_status(self, args):
        try:
            return self.gateway.call('messages_set_feedback_triage', args)
        except Uncertain:
            item = self.readback().get(args['feedback_id'])
            if item is not None and item['status'] == args['status']:
                return {'status_changed_at': item['status_changed_at']}
            return self.gateway.call('messages_set_feedback_triage', args)

    def conflict(self, link, task, wanted_status):
        item = self.readback().get(link['fid'])
        if item is None:
            return
        stats = {'created': 0, 'updated': 0}
        title, description = render(item)
        self.update_task(self.ledger.link(link['fid']) | {'fp': None}, item, title, description,
            sha(title + '\n' + description), None, stats)
        self.once_comment('conflict:%s:%s' % (link['fid'], task['updatedAt']), link['task_key'],
            'DeedKit status is `%s`; the change to `%s` made here was not applied.' % (item['status'], wanted_status))

    def reverse_one(self, link, task, version):
        fid, status = link['fid'], link['fb_status']
        if task['status'] == 'done' and status != 'verified':
            self.once_comment('done:%s:%s' % (fid, status), link['task_key'], DONE_NOTE % status)
        wants = reverse_status(task['status'], task['labels'], status, task.get('description'))
        if wants is None:
            if task['status'] == 'canceled' and 'duplicate' in task['labels'] and status in ('received', 'accepted', 'reopened'):
                self.once_comment('dupline:%s' % fid, link['task_key'],
                    'To mark this duplicate add a line `Duplicate-Of: <feedback id>` to the description.')
            self.ledger.update_link(fid, seen_version=version)
            return None
        wanted_status, duplicate_of = wants
        rid = request_id(fid, task['id'], version)
        args = {'feedback_id': fid, 'status': wanted_status, 'request_id': rid, 'expected_status': status,
            'expected_changed_at': link['fb_changed_at']} | ({'duplicate_of': duplicate_of} if duplicate_of else {})
        self.ledger.put_pending_write(rid, fid, version, wanted_status)
        try:
            result = self.write_status(args)
        except Refused as error:
            self.ledger.drop_pending_write(rid)
            if error.code in UNAVAILABLE:
                return error.code
            if error.code == 'version_conflict':
                self.conflict(link, task, wanted_status)
            self.ledger.update_link(fid, seen_version=version)
            return None
        self.ledger.drop_pending_write(rid)
        self.ledger.update_link(fid, fb_status=wanted_status, fb_changed_at=result.get('status_changed_at'), seen_version=version)
        return True

    def reverse(self):
        written = 0
        for task in self.tasks.list_tasks('feedback'):
            link = self.ledger.link_by_task(task['id'])
            version = task.get('updatedAt')
            if link is None or link['skipped'] or version in (link['bridge_version'], link['seen_version']):
                continue
            outcome = self.reverse_one(link, task, version)
            if isinstance(outcome, str):
                return {'written': written, 'unavailable': outcome}
            written += outcome is True
        return {'written': written}

    def cycle(self):
        return self.forward() | self.reverse()

    def run(self):
        with self.lock(block=False):
            return self.cycle()

    # ---- watch ----
    def safe_reverse(self):
        try:
            return self.reverse()
        except BridgeError as error:
            log(error.code)

    def watch(self, stop, sleep=time.sleep, monotonic=time.monotonic):
        delay, first, last = BACKOFF_MIN, True, None
        while not stop():
            started = monotonic()
            try:
                with self.lock(block=True):
                    sid = self.ensure_subscription()
                    if first:
                        self.forward()
                        self.safe_reverse()
                        first, last = False, monotonic()
                page = self.events('messages_wait_for_subscription_events', sid,
                    {'timeout': 25, 'limit': 100, 'detail': 'compact'}, timeout=45)
                items = (page.get('module') or {}).get('items')
                due = last is None or monotonic() - last >= REVERSE_EVERY
                if items or due:
                    with self.lock(block=True):
                        if items:
                            self.forward(page)
                        self.safe_reverse()
                        last = monotonic()
            except (BridgeError, Busy) as error:
                log(getattr(error, 'code', 'lock_busy'))
                sleep(min(delay, BACKOFF_MAX) * (1 + 0.25 * self.rand()))
                delay = min(delay * 2, BACKOFF_MAX)
                continue
            delay = BACKOFF_MIN
            if monotonic() - started < MIN_PASS:
                sleep(MIN_PASS)


def check(cfg, tasks):
    print('feedback-bridge: env ok')
    if not any(isinstance(p, dict) and p.get('prefix') == cfg.tracker for p in tasks.projects()):
        log('tracker_missing')
        return 1
    print('feedback-bridge: tracker %s ok' % cfg.tracker)
    names = tasks.label_names()
    print('feedback-bridge: label feedback %s' % ('ok' if 'feedback' in names else 'missing (created on first use)'))
    return 0


def main(argv=None, environ=None, gateway=None, tasks=None, now=time.time, sleep=time.sleep, stop=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in ('watch', 'run', 'reverse', 'check'):
        print(__doc__, file=sys.stderr)
        return 64
    try:
        cfg = load_config(None, os.environ if environ is None else environ)
        tasks = tasks or BbTasks(cfg.tracker, state_dir=cfg.state_dir)
        if argv[0] == 'check':
            return check(cfg, tasks)
        bridge = Bridge(cfg, gateway or Gateway(cfg.gateway_url, cfg.key), tasks, now=now)
        if argv[0] == 'watch':
            event = threading.Event()
            for name in (signal.SIGTERM, signal.SIGINT):
                try:
                    signal.signal(name, lambda *_: event.set())
                except ValueError:
                    pass
            bridge.watch(stop or event.is_set, sleep=event.wait if sleep is time.sleep else sleep)
            return 0
        with bridge.lock(block=False):
            result = bridge.cycle() if argv[0] == 'run' else bridge.reverse()
    except Busy:
        return 75
    except BridgeError as error:
        log(error.code)
        return 1
    if result.get('unavailable'):
        log('reverse_unavailable_%s' % result['unavailable'])
    if any(result.get(name) for name in ('created', 'updated', 'written')):
        print('feedback-bridge: created=%d updated=%d written=%d' % tuple(result.get(n, 0) for n in ('created', 'updated', 'written')))
    return 0


if __name__ == '__main__':
    os.umask(0o077)
    sys.exit(main())
