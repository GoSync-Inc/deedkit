#!/usr/bin/env python3
"""Starts a process worker for a feedback task of the BB tracker, run on the owner's Ryzen host (ANYM-991).

    feedback_dispatch.py run         one tick: reconcile, attach, start (the bb script automation, every 10 minutes)
    feedback_dispatch.py gate KEY    a worker's check before a Tiden write, push or merge arm: exit 0 go, 1 stop
    feedback_dispatch.py status      the start journal, one line per start
    feedback_dispatch.py check       read-only: config, tracker, brief files

The bridge (feedback_bridge.py, ANYM-990) writes a task per feedback item into the tracker DK. This program lists those
tasks through the local `bb` CLI and starts one `bb thread spawn` worker for a task that is inside the feedback-autoloop
envelope (docs/operations/feedback-autoloop.md, unchanged): a permitted principal, a released summary, a defect or a
feature, no worker before. It makes no model call and no gateway call, and the spawn prompt holds only paths, the
orchestrator thread and the task key. Every start is journaled before the spawn; an uncertain spawn is reconciled by the
thread list, never repeated. Off by default: `dispatch` in ~/.config/deedkit/feedback-dispatch.json, a PAUSE file in the
state directory and a hold list stop it. Exit 0 done, 1 error (one fixed line on stderr), 75 busy, 64 usage.
Standard library only; it imports feedback_bridge.py from the same directory.
"""
from __future__ import annotations

try:
    from . import feedback_bridge as fb
except ImportError:  # run as a script from the install directory
    import feedback_bridge as fb

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

PROJECT = 'proj_nfixav7q2f'  # the bb project of the workers' threads
PROVIDER, MODEL, EFFORT, BASE = 'claude-code', 'claude-sonnet-5-5', 'xhigh', 'main'
AUTHOR = 'deedkit-feedback-dispatch'
OPEN = ('backlog', 'todo', 'in_progress')  # task statuses that hold a slot and may be relaunched
FIRST = ('backlog', 'todo')  # a first start only; in_progress without a start of ours is a person's claim
ENDED = ('in_review', 'done', 'canceled')
KEY = re.compile(r'[A-Z][A-Z0-9]{1,9}-[1-9][0-9]*')
THREAD = re.compile(r'thr_[a-z0-9]{6,40}')
MACHINE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
CLASSES = ('reported-bug', 'feature')  # the reader's classes that name work; the others get an answer or the owner
STATUS_OK = ('received', 'accepted', 'reopened')
SKIP_LABELS = frozenset({'duplicate', 'hold', 'epic'})
CONFIG_KEYS = frozenset({'dispatch', 'orchestrator_thread', 'principals', 'tracker', 'hold', 'worker_machine', 'max_live',
    'daily_starts'})
MAX_LIVE, GRACE, SLACK, CHUNK = 2, 300, 60, 50
BRIEF_FILES = ('common.md', 'bb-overlay.md', 'feedback-task.md')
PROMPT = ('Read and follow {b}/common.md, then {b}/bb-overlay.md (it wins on conflict), then the task brief '
    '{b}/feedback-task.md. Your orchestrator is bb thread {o}. BB task: {k}.')
REPORT_ONLY = ("feedback-dispatch: report-only. This task's principal is not on the dispatch list, so no worker was "
    'started for it; a person may start one by hand.')
SUMMARY = "Summary (the loop's released brief"
SOURCE = re.compile(r'Source: ([A-Za-z0-9_.:@/ -]{1,64}) · [^·]+ · [^·]+ · received \S+')
STATUS_LINE = re.compile(r'DeedKit status: ([a-z_]+) \(changed [^)]*\)')
TRIAGE = re.compile(r'Triage: (\S+) / \S+; card (ANYM-[1-9][0-9]*|none); new card ([a-z0-9][a-z0-9-]{2,60}|none)')


def log(code):
    print('feedback-dispatch: %s' % code, file=sys.stderr, flush=True)


@dataclass
class Config:
    dispatch: bool
    orchestrator_thread: str | None
    principals: list
    tracker: str
    hold: list
    worker_machine: str | None
    max_live: int
    daily_starts: int
    state_dir: Path
    briefs_dir: Path


def load_config(path=None, environ=None):
    """The private JSON config (0600, no symlink, known keys only); an absent file is all defaults, and dispatch is off."""
    environ = os.environ if environ is None else environ
    path = Path(path or environ.get('DEEDKIT_FEEDBACK_DISPATCH_CONFIG') or Path.home() / '.config/deedkit/feedback-dispatch.json')
    state = Path(environ.get('DEEDKIT_FEEDBACK_DISPATCH_STATE_DIR') or Path.home() / '.local/state/deedkit-feedback-dispatch')
    briefs = Path(environ.get('DEEDKIT_FEEDBACK_DISPATCH_BRIEFS') or Path.home() / 'deedkit-orchestrator/briefs')
    try:
        raw = json.loads(fb._private(path)) if os.path.lexists(path) else {}
    except ValueError:
        raise fb.ConfigError('config_invalid') from None
    if not isinstance(raw, dict) or set(raw) - CONFIG_KEYS:
        raise fb.ConfigError('config_invalid')
    values = {'dispatch': False, 'orchestrator_thread': None, 'principals': [], 'tracker': 'DK', 'hold': [],
        'worker_machine': None, 'max_live': MAX_LIVE, 'daily_starts': 6} | raw
    listed = lambda name, pattern: isinstance(values[name], list) and all(isinstance(x, str) and pattern.fullmatch(x) for x in values[name])
    valid = (type(values['dispatch']) is bool and listed('principals', fb.WORD) and listed('hold', KEY)
        and (values['orchestrator_thread'] is None or (isinstance(values['orchestrator_thread'], str) and THREAD.fullmatch(values['orchestrator_thread'])))
        and (values['worker_machine'] is None or (isinstance(values['worker_machine'], str) and MACHINE.fullmatch(values['worker_machine'])))
        and isinstance(values['tracker'], str) and re.fullmatch(r'[A-Z][A-Z0-9]{1,9}', values['tracker'])
        and type(values['max_live']) is int and 0 <= values['max_live'] <= MAX_LIVE
        and type(values['daily_starts']) is int and 0 <= values['daily_starts'] <= 100
        and not (values['dispatch'] and (not values['principals'] or not values['orchestrator_thread'])))
    if not valid:
        raise fb.ConfigError('config_invalid')
    return Config(state_dir=state, briefs_dir=briefs, **values)


def parse_task(task):
    """The structured header the bridge wrote (fb.render), or None: the exact `Feedback-ID:` first line, then the `Source:`
    and `DeedKit status:` lines at their places, an optional `Triage:` line and the `Summary` header. Only the first
    lines count and nothing after the Summary header is read, so a brief (one quoted line) forges nothing."""
    text = task.get('description')
    lines = [x.rstrip('\r') for x in str(text or '').split('\n')]
    feedback_id = fb.first_line_id(text)
    summary = next((i for i, x in enumerate(lines) if x.startswith(SUMMARY)), None)
    if feedback_id is None or not isinstance(task.get('key'), str) or summary is None or summary < 4 or lines[1] != '':
        return None
    source, status = SOURCE.fullmatch(lines[2]), STATUS_LINE.fullmatch(lines[3])
    if not source or not status:
        return None
    triage = next((m for m in map(TRIAGE.fullmatch, lines[4:summary]) if m), None)
    classification, card, new_card = triage.groups() if triage else (None, 'none', 'none')
    cluster = 'new:' + new_card if new_card != 'none' else 'spec:' + card if card != 'none' else 'fid:' + feedback_id
    after = lines[summary + 1] if summary + 1 < len(lines) else ''
    return {'key': task['key'], 'number': task.get('number') or 0, 'status': task.get('status'), 'fid': feedback_id,
        'labels': [x for x in task.get('labels') or [] if isinstance(x, str)], 'principal': source.group(1),
        'dk_status': status.group(1), 'classification': classification, 'cluster': cluster,
        'released': after.startswith('> ') and len(after) > 2}


def _run(argv, timeout, stdin=None):
    extra = {'input': stdin} if stdin is not None else {'stdin': subprocess.DEVNULL}
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **extra)
    return done.returncode, done.stdout


class Tasks(fb.BbTasks):
    """The tracker calls the dispatcher needs, over the bridge's `bb tasks` client (its JSON and error handling)."""

    def candidates(self):
        found, cursor = [], None
        for _ in range(fb.MAX_LIST_PAGES):
            args = ['list', '--project', self.tracker, '--status', ','.join(OPEN), '--label', 'feedback', '--limit', '500']
            data = self._call(args + (['--cursor', cursor] if cursor else []))
            if not isinstance(data.get('tasks'), list):
                raise fb.Uncertain('tasks_bad_output')
            found += [self._task({'task': t}) for t in data['tasks']]
            cursor = data.get('nextCursor')
            if not cursor:
                return found
        raise fb.Incomplete('tasks_listing_incomplete')

    def show(self, ref):
        data = self._call(['show', ref])
        if not isinstance(data.get('taskThreads'), list):
            raise fb.Uncertain('tasks_bad_output')  # no answer about attached threads is no permission to start
        return self._task(data) | {'taskThreads': [t for t in data['taskThreads'] if isinstance(t, dict)]}

    def attach(self, ref, thread):
        self._call(['attach', ref, '--thread', thread])

    def comment(self, ref, body):
        with self._file(body) as path:
            self._call(['comment', ref, '--body-file', path, '--author', AUTHOR])


class Threads:
    """`bb thread` for one project: the listing (with archived threads) and the spawn."""

    def __init__(self, project=PROJECT, runner=None):
        self.project, self.runner = project, runner or _run

    def _call(self, argv, timeout=60, stdin=None):
        try:
            code, out = self.runner(['bb', *argv, '--json'], timeout, stdin)
        except subprocess.TimeoutExpired:
            raise fb.Uncertain('threads_timeout') from None
        except OSError:
            raise fb.Refused('threads_unavailable') from None
        try:
            data = json.loads(out)
        except ValueError:
            raise fb.Uncertain('threads_bad_output') from None
        if isinstance(data, dict) and data.get('ok') is False:
            error = data.get('error')
            named = error.get('code') if isinstance(error, dict) else None
            raise fb.Refused(named if isinstance(named, str) and fb.CODE.fullmatch(named) else 'threads_error')
        if code != 0:
            raise fb.Uncertain('threads_failed')
        return data

    def rows(self, ids=None):
        ids = None if ids is None else list(dict.fromkeys(ids))
        batches = [['--project', self.project, '--include-archived']] if ids is None else [
            ['--include-archived'] + [x for i in ids[n:n + CHUNK] for x in ('--thread', i)] for n in range(0, len(ids), CHUNK)]
        found = []
        for batch in batches:
            data = self._call(['thread', 'list', *batch])
            if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
                raise fb.Uncertain('threads_bad_output')
            found += data
        return found

    def spawn(self, title, prompt, orchestrator, machine=None):
        argv = ['thread', 'spawn', '--project', self.project, '--parent-thread', orchestrator, '--new-environment', 'worktree',
            '--base-branch', BASE, '--provider', PROVIDER, '--model', MODEL, '--reasoning-level', EFFORT, '--title', title,
            '--prompt-file', '-'] + (['--machine', machine] if machine else [])
        thread = self._call(argv, timeout=180, stdin=prompt)
        if not isinstance(thread, dict) or not isinstance(thread.get('id'), str) or not THREAD.fullmatch(thread['id']):
            raise fb.Uncertain('spawn_bad_reply')
        return thread['id']


class Journal:
    """One SQLite file (WAL, synchronous=FULL): a committed row survives a crash. One flock holds the writer."""

    def __init__(self, state_dir):
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        self.db = sqlite3.connect(state_dir / 'dispatch.sqlite3', timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS starts(id INTEGER PRIMARY KEY AUTOINCREMENT, task_key TEXT NOT NULL, '
            'fid TEXT NOT NULL, cluster TEXT NOT NULL, attempt INTEGER NOT NULL, title TEXT NOT NULL, state TEXT NOT NULL, '
            'thread_id TEXT, attached INTEGER NOT NULL DEFAULT 0, started_at REAL NOT NULL, ended_at REAL, '
            'UNIQUE(task_key, attempt))')
        self.db.execute('CREATE TABLE IF NOT EXISTS notes(key TEXT PRIMARY KEY)')
        for suffix in ('', '-wal', '-shm'):
            (state_dir / ('dispatch.sqlite3' + suffix)).exists() and (state_dir / ('dispatch.sqlite3' + suffix)).chmod(0o600)

    def rows(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM starts ORDER BY id')]

    def begin(self, key, feedback_id, cluster, attempt, title, now):
        return self.db.execute('INSERT INTO starts(task_key,fid,cluster,attempt,title,state,started_at) VALUES(?,?,?,?,?,?,?)',
            (key, feedback_id, cluster, attempt, title, 'starting', now)).lastrowid

    def set(self, row_id, **fields):
        assert set(fields) <= {'state', 'thread_id', 'attached', 'ended_at'}
        self.db.execute('UPDATE starts SET %s WHERE id=?' % ','.join('%s=?' % k for k in fields), (*fields.values(), row_id))

    def mark(self, key):
        return self.db.execute('INSERT OR IGNORE INTO notes(key) VALUES(?)', (key,)).rowcount == 1

    def unmark(self, key):
        self.db.execute('DELETE FROM notes WHERE key=?', (key,))


class Dispatcher:
    def __init__(self, cfg, tasks, threads, now=time.time):
        self.cfg, self.tasks, self.threads, self.now = cfg, tasks, threads, now
        self._journal, self.live, self.statuses = None, set(), {}

    @property
    def journal(self):
        if self._journal is None:
            self._journal = Journal(self.cfg.state_dir)
        return self._journal

    def paused(self):
        return os.path.lexists(self.cfg.state_dir / 'PAUSE')

    @contextmanager
    def lock(self, block=False):
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.state_dir.chmod(0o700)
        fd = os.open(self.cfg.state_dir / 'dispatch.lock', os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + 60
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if not block or time.monotonic() > deadline:
                        raise fb.Busy from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    # ---- one tick ----
    def run(self):
        stats = dict.fromkeys(('started', 'relaunched', 'reconciled', 'attached', 'commented'), 0)
        if not self.cfg.dispatch or self.paused():
            return stats
        with self.lock():
            self.live, self.statuses = set(), {}
            self.reconcile(stats)
            self.attach_pending(stats)
            self.start_due(stats)
        return stats

    def task_status(self, key):
        if key not in self.statuses:
            try:
                self.statuses[key] = self.tasks.show(key)['status']
            except fb.Refused:
                self.statuses[key] = 'canceled'  # a task that is gone holds no slot
        return self.statuses[key]

    def ours(self, row, thread):
        created = thread.get('createdAt')
        return (thread.get('projectId') == PROJECT and thread.get('title') == row['title'] and isinstance(thread.get('id'), str)
            and isinstance(created, (int, float)) and created / 1000 >= row['started_at'] - SLACK)

    def reconcile(self, stats):
        """`starting` rows (a spawn of unknown outcome) are found or lost by the thread list; `started` rows whose thread
        is archived or gone are finished (the task is in_review, done or canceled) or lost. Lost rows keep their attempt."""
        rows = self.journal.rows()
        starting, started = [r for r in rows if r['state'] == 'starting'], [r for r in rows if r['state'] == 'started']
        if starting:
            listing, claimed = self.threads.rows(), {r['thread_id'] for r in rows if r['thread_id']}
            for row in starting:
                found = next((t for t in listing if t['id'] not in claimed and self.ours(row, t)), None)
                if found:
                    claimed.add(found['id'])
                    self.journal.set(row['id'], state='started', thread_id=found['id'])
                    started.append(dict(row, state='started', thread_id=found['id']))
                    stats['reconciled'] += 1
                elif self.now() - row['started_at'] >= GRACE:
                    self.journal.set(row['id'], state='lost', ended_at=self.now())
                else:
                    self.live.add(row['task_key'])
        if started:
            by_id = {t.get('id'): t for t in self.threads.rows([r['thread_id'] for r in started])}
            for row in started:
                thread, status = by_id.get(row['thread_id']), self.task_status(row['task_key'])
                if thread is None or thread.get('archivedAt') or thread.get('deletedAt'):
                    self.journal.set(row['id'], state='finished' if status in ENDED else 'lost', ended_at=self.now())
                elif status in OPEN:
                    self.live.add(row['task_key'])

    def attach(self, row, stats):
        """Idempotent: a thread already listed on the task counts as attached, so a lost reply never attaches twice."""
        try:
            attached = [t.get('threadId') for t in self.tasks.show(row['task_key']).get('taskThreads') or []]
            if row['thread_id'] not in attached:
                self.tasks.attach(row['task_key'], row['thread_id'])
        except fb.BridgeError as error:
            log(error.code)
            return
        self.journal.set(row['id'], attached=1)
        stats['attached'] += 1

    def attach_pending(self, stats):
        for row in self.journal.rows():
            if row['state'] == 'started' and not row['attached']:
                self.attach(row, stats)

    # ---- candidates and starts ----
    def judge(self, task):
        """(info, 'start' | 'report' | None): the checks that need no journal."""
        info = parse_task(task)
        if info is None or not KEY.fullmatch(info['key']) or SKIP_LABELS & set(info['labels']) or info['key'] in self.cfg.hold:
            return info, None
        if info['principal'] not in self.cfg.principals:
            return info, 'report'
        if info['dk_status'] not in STATUS_OK or info['classification'] not in CLASSES or not info['released']:
            return info, None
        return info, 'start'

    def attempt(self, info):
        """The next attempt number for a task, or None: one start per task, feedback ID and cluster ever, plus one relaunch."""
        rows = self.journal.rows()
        mine = [r for r in rows if r['task_key'] == info['key']]
        if any(r['task_key'] != info['key'] and (r['fid'] == info['fid'] or r['cluster'] == info['cluster']) for r in rows):
            return None
        if not mine:
            return 1
        return 2 if len(mine) == 1 and mine[0]['attempt'] == 1 and mine[0]['state'] == 'lost' else None

    def capped(self):
        today = datetime.fromtimestamp(self.now(), timezone.utc).date()
        spawned = sum(datetime.fromtimestamp(r['started_at'], timezone.utc).date() == today for r in self.journal.rows())
        return len(self.live) >= self.cfg.max_live or spawned >= self.cfg.daily_starts

    def start_due(self, stats):
        for task in sorted(self.tasks.candidates(), key=lambda t: (t.get('number') or 0, str(t.get('key')))):
            info, action = self.judge(task)
            if action == 'report':
                self.report_only(info, stats)
            elif action == 'start' and not self.capped():
                attempt = self.attempt(info)
                if attempt is not None and info['status'] in (OPEN if attempt > 1 else FIRST):
                    self.start(info, attempt, stats)

    def report_only(self, info, stats):
        if not self.journal.mark('report-only:' + info['key']):
            return
        try:
            self.tasks.comment(info['key'], REPORT_ONLY)
        except fb.Refused:
            self.journal.unmark('report-only:' + info['key'])
            raise
        stats['commented'] += 1

    def start(self, info, attempt, stats):
        key = info['key']
        if [name for name in BRIEF_FILES if not (self.cfg.briefs_dir / name).is_file()]:
            raise fb.ConfigError('brief_missing')
        fresh = self.tasks.show(key)  # the listing may be minutes old
        again, action = self.judge(fresh)
        own = {r['thread_id'] for r in self.journal.rows() if r['task_key'] == key and r['thread_id']}
        if (action != 'start' or again['fid'] != info['fid'] or fresh['status'] not in (OPEN if attempt > 1 else FIRST)
                or {t.get('threadId') for t in fresh['taskThreads']} - own):
            return
        title = 'feedback-' + key
        row = self.journal.begin(key, info['fid'], info['cluster'], attempt, title, self.now())  # committed before the spawn
        prompt = PROMPT.format(b=self.cfg.briefs_dir, o=self.cfg.orchestrator_thread, k=key)
        try:
            thread = self.threads.spawn(title, prompt, self.cfg.orchestrator_thread, self.cfg.worker_machine)
        except fb.Refused:
            self.journal.set(row, state='lost', ended_at=self.now())  # the CLI said no: nothing was created
            raise
        # An uncertain outcome (or a crash) propagates with the row still `starting`: the next tick reconciles it.
        self.journal.set(row, state='started', thread_id=thread)
        self.live.add(key)
        stats['relaunched' if attempt > 1 else 'started'] += 1
        self.attach({'id': row, 'task_key': key, 'thread_id': thread}, stats)

    # ---- commands ----
    def gate(self, key):
        """0 while the worker of `key` may go on: no PAUSE, not held, a `starting` or `started` row. No bb call."""
        if not KEY.fullmatch(key):
            reason = 'bad_key'
        elif self.paused():
            reason = 'paused'
        elif key in self.cfg.hold:
            reason = 'held'
        elif not any(r['task_key'] == key and r['state'] in ('starting', 'started') for r in self.journal.rows()):
            reason = 'not_live'
        else:
            reason = None
        print('go' if reason is None else 'stop ' + reason)
        return 0 if reason is None else 1


def check(cfg, tasks):
    print('feedback-dispatch: config ok (dispatch %s)' % ('on' if cfg.dispatch else 'off'))
    if not any(isinstance(p, dict) and p.get('prefix') == cfg.tracker for p in tasks.projects()):
        log('tracker_missing')
        return 1
    print('feedback-dispatch: tracker %s ok' % cfg.tracker)
    missing = [name for name in BRIEF_FILES if not (cfg.briefs_dir / name).is_file()]
    print('feedback-dispatch: briefs %s' % ('ok' if not missing else 'missing: ' + ', '.join(missing)))
    return 1 if missing else 0


def main(argv=None, environ=None, tasks=None, threads=None, now=time.time):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ('run', 'gate', 'status', 'check') or len(argv) != (2 if argv[0] == 'gate' else 1):
        print(__doc__, file=sys.stderr)
        return 64
    try:
        cfg = load_config(None, os.environ if environ is None else environ)
        tasks = tasks or Tasks(cfg.tracker, state_dir=cfg.state_dir)
        dispatcher = Dispatcher(cfg, tasks, threads or Threads(), now=now)
        if argv[0] == 'check':
            return check(cfg, tasks)
        if argv[0] == 'gate':
            return dispatcher.gate(argv[1])
        if argv[0] == 'status':
            for r in dispatcher.journal.rows():
                print('%s attempt=%d state=%s thread=%s' % (r['task_key'], r['attempt'], r['state'], r['thread_id'] or '-'))
            return 0
        stats = dispatcher.run()
    except fb.Busy:
        return 75
    except fb.BridgeError as error:
        log(error.code)
        return 1
    if any(stats.values()):
        print('feedback-dispatch: started=%(started)d relaunched=%(relaunched)d reconciled=%(reconciled)d '
            'attached=%(attached)d commented=%(commented)d' % stats)
    return 0


if __name__ == '__main__':
    os.umask(0o077)
    sys.exit(main())
