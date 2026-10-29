"""Re-evaluation of open version conflicts under each new rule, run by each account's collector (ANYM-621).

Production deploy applies SQL files only, so a rule fix cannot resolve the open rows it would no longer raise from a
migration hook. A rule card registers a job here instead: (name, version, kind, class patterns, a pure function of one
row's stored evidence). Each collector runs its account's pending jobs once per start on one dedicated thread with its
own connection, in batches of two phases:

- evaluate: read at most BATCH candidate rows in a read-only snapshot without any row or advisory lock, then call the
  job function on each row outside any transaction, on an evaluator thread with a per-row deadline;
- apply: one short transaction that takes the account's archive lock with a try-lock (busy: retry next tick), checks
  the collector's lease and writes only SQL, guarded by the state the evaluation read. `classify` and `resolve` never
  take the global lifecycle lock; `accept` writes one row per transaction through ingest's own entry.

The progress (watermark: the newest `observed_at` scanned; cursor: that row's receipt digest) commits with the apply
transaction, so a crash resumes after the last committed batch, and every later start scans rows newer than it.
Errors are fixed codes in `last_error`, never row content.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from fnmatch import fnmatchcase
import logging
import re
import threading
import time
from types import MappingProxyType
from typing import Callable

import psycopg

from .conflict_classes import EXPLANATIONS, REASON_VERSION, UNCLASSIFIED, reclassify

log = logging.getLogger(__name__)

SCHEMA = 57  # conflict_reevaluations and message_conflicts.resolution
BATCH = 50
EVALUATION_TIMEOUT = 10
TICK = 1
KINDS = ('classify', 'resolve', 'accept')
# What ingest's own resolves record from schema 57; NULL is unknown (legacy, or resolved by an N-1 writer).
RESOLUTIONS = ('newer_observation', 'sender_completed')
# A resolve or accept job selects open rows by stored class; `unclassified` names no rule's evidence.
CLASSES = tuple(sorted(name for name in EXPLANATIONS if name != UNCLASSIFIED))
NAME = re.compile(r'[a-z][a-z0-9_]{0,47}')
# The archive's version relative to a conflict's candidate: newer, the same, older (still_raised tries each).
ORDERS = (timedelta(microseconds=-1), timedelta(0), timedelta(microseconds=1))
BOUNDS = ("SET LOCAL lock_timeout='1s'", "SET LOCAL statement_timeout='2s'")
# An apply transaction's budget: past WALL seconds it rolls back with `wall_deadline`; the server ends it (and the
# runner's session) at TRANSACTION_TIMEOUT whatever the client does.
WALL = 3
TRANSACTION_TIMEOUT = "SET LOCAL transaction_timeout='5s'"


def stored(c):
    from .store import schema_version
    return schema_version(c) >= SCHEMA


def resolution_set(c, value):
    """The SET item ingest adds to a resolve from schema 57; nothing below it, where N-1 rows read as unknown."""
    if value not in RESOLUTIONS: raise ValueError('invalid_resolution')
    return f"resolution='{value}'," if stored(c) else ''


@dataclass(frozen=True)
class Job:
    """`function(row)` gets a read-only mapping of one row's stored evidence (`envelope`: the receipt envelope,
    `observed`: the observed revision's snapshot, `observed_revision`, `candidate_updated_at`, `reason`,
    `archive_version`: the message's provider version now, `tombstoned`: a provider tombstone names it) and returns
    a class name (`classify`), 'keep' or 'resolve' (`resolve`), or None or a candidate envelope the current rule
    accepts as a new revision (`accept`). `classes` are fnmatch patterns over ANYM-557's classes."""
    name: str
    version: int
    kind: str
    function: Callable
    classes: tuple = ()

    def __post_init__(self):
        if not isinstance(self.name, str) or not NAME.fullmatch(self.name): raise ValueError('invalid_job_name')
        if type(self.version) is not int or not 0 < self.version < 2**15: raise ValueError('invalid_job_version')
        if self.kind not in KINDS or not callable(self.function): raise ValueError('invalid_job_kind')
        if self.kind == 'classify':
            if self.classes: raise ValueError('invalid_job_classes')
            return
        patterns = (self.classes,) if isinstance(self.classes, str) else tuple(self.classes)
        matched = tuple(name for name in CLASSES if any(fnmatchcase(name, p) for p in patterns))
        if not patterns or not all(isinstance(p, str) for p in patterns) or not matched:
            raise ValueError('invalid_job_classes')
        object.__setattr__(self, 'classes', matched)

    @property
    def resolution(self):
        return f'rule_changed:{self.name}@{self.version}'


JOBS = {}


def register(job):
    """Real jobs come from the rule cards and the classification card; one version per name."""
    if job.name in JOBS: raise ValueError('duplicate_job')
    JOBS[job.name] = job
    return job


def registered():
    """Classify jobs first: an account's rule jobs start only after its classification is done."""
    return sorted(JOBS.values(), key=lambda job: (job.kind != 'classify', job.name))


class Failed(Exception):
    """A fixed `last_error` code for this job's run."""


def still_raised(row):
    """Whether the current ingest rule (archive.ingest_outcome) could raise this conflict again: the receipt envelope
    against the revision it contradicted. The provider version the archive held then is not stored; it was at least
    the revision's own (unchanged re-observations only raise it), so each order to the candidate it allows (older,
    the same, newer) is tried, and any one that raises keeps the row open (a stored class's relation may come from a
    later classification of the snapshot alone). Unreadable evidence counts as raised."""
    from datetime import datetime
    from .archive import ingest_outcome
    from .conflict_classes import SNAPSHOT_FIELDS
    from .envelope import fingerprint, validate
    try:
        candidate, snapshot = validate(row['envelope']), row['observed']
        retained = validate({**{k: v for k, v in snapshot.items() if k not in SNAPSHOT_FIELDS},
                             'chat': snapshot['provider_chat']})
        own, at = datetime.fromisoformat(retained['updated_at']), row['candidate_updated_at']
        old = {'sent_at': datetime.fromisoformat(retained['sent_at']), 'direction': retained['direction'],
               'sender': retained['sender'], 'deleted': retained['deleted'], 'body': retained['body'],
               'content_hash': fingerprint(retained)}
        return any(ingest_outcome({**old, 'provider_updated_at': max(own, at + step)}, candidate,
                                  snapshot)[0] == 'conflict' for step in ORDERS)
    except Exception:
        return True


def _decide(job, row):
    evidence = MappingProxyType({key: deepcopy(row[key]) for key in
        ('envelope', 'observed', 'observed_revision', 'candidate_updated_at', 'reason', 'archive_version', 'tombstoned')})
    try: result = job.function(evidence)
    except Exception: raise Failed('evaluation_error') from None
    if job.kind == 'classify' and isinstance(result, str) and result in EXPLANATIONS: return result
    # No job resolves a row the current rule still raises, whatever its function returns.
    if job.kind == 'resolve' and result in ('keep', 'resolve'): return 'veto' if result == 'resolve' and still_raised(row) else result
    if job.kind == 'accept' and (result is None or isinstance(result, dict)): return result
    raise Failed('invalid_result')


# The decision for a row whose evaluation ran past the deadline: a class nobody could read, else no change.
STALLED = {'classify': UNCLASSIFIED, 'resolve': 'keep', 'accept': None}
SCOPE = '''FROM message_conflicts mc JOIN messages m ON m.id=mc.message
    WHERE m.account=%(account)s AND mc.resolved_at IS NULL AND {selector}
    AND (%(at)s::timestamptz IS NULL OR (mc.observed_at,mc.receipt_digest)>(%(at)s::timestamptz,%(digest)s::text))'''
CANDIDATES = '''SELECT mc.receipt_digest AS digest,mc.message,mc.observed_at,mc.observed_revision,mc.reason,
    mc.candidate_updated_at,r.envelope,v.snapshot AS observed,m.provider_updated_at AS archive_version,
    EXISTS(SELECT 1 FROM provider_tombstones t WHERE t.account=m.account AND t.provider_id=m.provider_id
        AND t.provider_chat IN ((SELECT provider_chat FROM chats WHERE id=m.chat),'')) AS tombstoned
    FROM message_conflicts mc JOIN messages m ON m.id=mc.message
    LEFT JOIN ingest_receipts r ON r.digest=mc.receipt_digest
    LEFT JOIN message_revisions v ON v.message=mc.message AND v.revision=mc.observed_revision
    WHERE m.account=%(account)s AND mc.resolved_at IS NULL AND {selector}
    AND (%(at)s::timestamptz IS NULL OR (mc.observed_at,mc.receipt_digest)>(%(at)s::timestamptz,%(digest)s::text))
    ORDER BY mc.observed_at,mc.receipt_digest LIMIT %(limit)s'''
# The first open row of the applied range the evaluation did not see; for a rule job also a row without a class, seen
# or not (one an older writer reset after the snapshot would otherwise fall behind the cursor before it is classified).
BLOCKER = '''SELECT mc.observed_at,mc.receipt_digest AS digest,mc.reason '''+SCOPE+'''
    AND (mc.observed_at,mc.receipt_digest)<=(%(end)s::timestamptz,%(end_digest)s::text)
    AND {unseen} ORDER BY mc.observed_at,mc.receipt_digest LIMIT 1'''
SELECTORS = {'classify': 'mc.reason IS NULL', 'resolve': 'mc.reason=ANY(%(classes)s)', 'accept': 'mc.reason=ANY(%(classes)s)'}
CLASSIFY = '''UPDATE message_conflicts mc SET reason=d.reason,reason_version=%(version)s
    FROM unnest(%(digests)s::text[],%(decisions)s::text[],%(revisions)s::integer[]) AS d(digest,reason,revision)
    WHERE mc.receipt_digest=d.digest AND mc.resolved_at IS NULL AND mc.reason IS NULL AND mc.observed_revision=d.revision'''
# The retained revision stands; only a row still open with the class it was judged by.
RESOLVE = '''UPDATE message_conflicts mc SET resolved_at=clock_timestamp(),resolved_revision=mc.observed_revision,
    resolution=%(resolution)s
    FROM unnest(%(digests)s::text[],%(reasons)s::text[],%(revisions)s::integer[]) AS d(digest,reason,revision)
    WHERE mc.receipt_digest=d.digest AND mc.resolved_at IS NULL AND mc.reason=d.reason AND mc.reason=ANY(%(classes)s)
    AND mc.observed_revision=d.revision'''


class _Dedicated:
    """The store as ingest sees it, with the runner's connection inside the runner's transaction as its pool."""
    def __init__(self, store, c): self._store, self._c = store, c
    def __getattr__(self, name): return getattr(self._store, name)
    @property
    def pool(self): return self
    @contextmanager
    def connection(self):
        with self._c.transaction(): yield self._c


class Runner:
    """One collector's re-evaluation of its account; `start()` returns at once, `stop()` never waits."""

    def __init__(self, store, account, lease, *, jobs=None, evaluation_timeout=EVALUATION_TIMEOUT, tick=TICK, wall=WALL):
        self.store, self.account, self.lease = store, account, lease
        self.jobs = registered() if jobs is None else sorted(jobs, key=lambda job: (job.kind != 'classify', job.name))
        self.evaluation_timeout, self.tick, self.wall = evaluation_timeout, tick, wall
        self.stopping, self.thread, self.code, self._deadline, self._vetoed = threading.Event(), None, None, None, False
        self._opened = []

    def start(self):
        if not self.jobs:
            self.code = 'no_jobs'
            return self
        self.thread = threading.Thread(target=self._main, name='conflict-reevaluation', daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stopping.set()

    def _connect(self):
        return psycopg.connect(self.store.pool.conninfo, **{**self.store.pool.kwargs, 'autocommit': True})

    def _replace(self, c):
        c.close()
        self._opened.append(self._connect())
        return self._opened[-1]

    def _main(self):
        try:
            with self._connect() as c: self.code = self.run(c)
        except Exception as exc:
            self.code = str(exc) if str(exc) == 'collector_lease_lost' else 'runner_failed'
        if self.code not in ('done', 'stopped', 'no_jobs'): log.warning('conflict_reevaluation code=%s', self.code)

    def run(self, c):
        """Each job until its scan is empty; a job's error ends only that job for this start. Rule jobs run only after
        every classify job reached its end in this start, so none of them passes a row that is still without a class."""
        try: return self._run(c)
        finally:
            for opened in self._opened: opened.close()
            self._opened = []

    def _run(self, c):
        if not stored(c): return 'schema_not_ready'
        classified = None
        for job in self.jobs:
            if self.stopping.is_set(): return 'stopped'
            try:
                try:
                    if job.kind != 'classify':
                        if classified is None: classified = self._classified(c)
                        if not classified: raise Failed('classify_pending')
                    self._run_job(c, job)
                except psycopg.errors.LockNotAvailable: raise Failed('lock_timeout') from None
                except psycopg.errors.QueryCanceled: raise Failed('statement_timeout') from None
                except psycopg.Error as exc:
                    # The server ends the session at TRANSACTION_TIMEOUT: record on a new connection, go on.
                    if not (c.closed or c.broken): raise
                    c = self._replace(c)
                    raise Failed('wall_deadline' if isinstance(exc, psycopg.errors.TransactionTimeout) else 'connection_lost') from None
            except Failed as failed:
                if job.kind == 'classify': classified = False
                code = str(failed)
                self._record(c, job, code)
                if code == 'evaluation_timeout': return code  # the evaluator thread may never return
        return 'stopped' if self.stopping.is_set() else 'done'

    def _classified(self, c):
        if any(job.kind == 'classify' for job in self.jobs): return True  # each reached its end in this start
        return not c.execute('''SELECT 1 FROM message_conflicts mc JOIN messages m ON m.id=mc.message
            WHERE m.account=%s AND mc.resolved_at IS NULL AND mc.reason IS NULL LIMIT 1''', (self.account,)).fetchone()

    def _run_job(self, c, job):
        self._vetoed = False
        while not self.stopping.is_set():
            start, rows = self._read(c, job)
            if not rows:
                # A vetoed decision is a job defect: the row stays open and the job says so.
                self._write(c, job, done=True, error='rule_still_raises' if self._vetoed else None)
                return
            decisions, scanned, error = self._evaluate(job, rows)
            if not scanned: raise Failed(error)  # the whole batch rolls back; the next start retries it
            while True:
                applied = (self._accept if job.kind == 'accept' else self._apply)(c, job, start, rows[:scanned], decisions, error)
                if applied or self.stopping.wait(self.tick): break
            if error: raise Failed(error)

    def _params(self, job, start, **extra):
        return {'account': self.account, 'at': start[0], 'digest': start[1], 'classes': list(job.classes), **extra}

    def _read(self, c, job):
        with c.transaction():
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            c.execute(BOUNDS[1])
            state = c.execute('''SELECT watermark,cursor FROM conflict_reevaluations
                WHERE job=%s AND job_version=%s AND account=%s''', (job.name, job.version, self.account)).fetchone()
            start = (state['watermark'], state['cursor']) if state else (None, None)
            rows = c.execute(CANDIDATES.format(selector=SELECTORS[job.kind]), self._params(job, start, limit=BATCH)).fetchall()
        return start, rows

    def _evaluate(self, job, rows):
        """(decisions per row, rows scanned, error). A row past the deadline is scanned with its STALLED decision."""
        decisions, current, done, failure = [], [(0, time.monotonic())], threading.Event(), []

        def work():
            try:
                for index, row in enumerate(rows):
                    current[0] = (index, time.monotonic())
                    decisions.append(_decide(job, row))
            except Failed as failed: failure.append(str(failed))
            finally: done.set()
        threading.Thread(target=work, name='conflict-reevaluation-evaluate', daemon=True).start()
        while not done.wait(0.05):
            index, started = current[0]
            if time.monotonic() - started > self.evaluation_timeout:
                return decisions[:index] + [STALLED[job.kind]], index + 1, 'evaluation_timeout'
        if failure: return [], 0, failure[0]
        return decisions, len(rows), None

    def _begin(self, c, *locks):
        """Bounds, the lease and then try-locks in ingest's order: the transaction never waits on an advisory lock,
        so it never holds one while waiting. False when a lock is busy. The server ends it at TRANSACTION_TIMEOUT."""
        self._deadline = time.monotonic() + self.wall
        for statement in (*BOUNDS, TRANSACTION_TIMEOUT): c.execute(statement)
        from .archive import Archive
        Archive.fence(c, self.account, self.lease)
        for name in locks:
            if not c.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked', (name,)).fetchone()['locked']:
                return False
        return True

    def _in_time(self):
        if time.monotonic() > self._deadline: raise Failed('wall_deadline')

    def _cut(self, c, job, start, scanned):
        """The prefix of `scanned` before the first row of its range the evaluation did not see (committed after its
        snapshot) or, for a rule job, before a row still without a class; `classify_pending` when that is the first."""
        if not scanned: return scanned
        rule = job.kind != 'classify'
        selector = f'({SELECTORS[job.kind]} OR mc.reason IS NULL)' if rule else SELECTORS[job.kind]
        unseen = '(NOT mc.receipt_digest=ANY(%(seen)s) OR mc.reason IS NULL)' if rule else 'NOT mc.receipt_digest=ANY(%(seen)s)'
        blocker = c.execute(BLOCKER.format(selector=selector, unseen=unseen), self._params(job, start, end=scanned[-1]['observed_at'],
            end_digest=scanned[-1]['digest'], seen=[row['digest'] for row in scanned])).fetchone()
        if not blocker: return scanned
        prefix = [row for row in scanned if (row['observed_at'], row['digest']) < (blocker['observed_at'], blocker['digest'])]
        if not prefix and blocker['reason'] is None and rule: raise Failed('classify_pending')
        return prefix

    def _apply(self, c, job, start, scanned, decisions, error):
        with c.transaction():
            if not self._begin(c, 'archive:' + self.account): return False
            scanned = self._cut(c, job, start, scanned)
            pairs = list(zip(scanned, decisions))
            self._in_time()
            if job.kind == 'classify' and pairs:
                c.execute(CLASSIFY, {'version': job.version, 'digests': [r['digest'] for r, _ in pairs],
                    'decisions': [d for _, d in pairs], 'revisions': [r['observed_revision'] for r, _ in pairs]})
            chosen = [r for r, d in pairs if d == 'resolve']
            if job.kind == 'resolve' and chosen:
                c.execute(RESOLVE, {'resolution': job.resolution, 'classes': list(job.classes),
                    'digests': [r['digest'] for r in chosen], 'reasons': [r['reason'] for r in chosen],
                    'revisions': [r['observed_revision'] for r in chosen]})
            if any(d == 'veto' for _, d in pairs): self._vetoed = True
            self._in_time()
            self._progress(c, job, scanned[-1] if scanned else None, error if len(scanned) == len(decisions) else None)
            self._in_time()
        return True

    def _accept(self, c, job, start, scanned, decisions, error):
        """One row per transaction through ingest's own entry (a stored candidate replays its receipt), after the
        lease and ingest's locks in ingest's order as try-locks: it never waits while holding the lifecycle lock."""
        from .archive import Archive
        from .envelope import stable_id
        from .file_lifecycle import LOCK_NAME
        archive = Archive(_Dedicated(self.store, c))
        for index, row in enumerate(scanned):
            candidate = decisions[index]
            with c.transaction():
                if not self._begin(c, LOCK_NAME, 'archive:' + self.account): return False
                if not self._cut(c, job, start, [row]): return True  # read again from the last committed row
                open_row = c.execute('''SELECT 1 FROM message_conflicts WHERE receipt_digest=%s AND resolved_at IS NULL
                    AND reason=%s AND reason=ANY(%s) AND observed_revision=%s FOR UPDATE NOWAIT''',
                    (row['digest'], row['reason'], list(job.classes), row['observed_revision'])).fetchone()
                if candidate is not None and open_row:
                    if (candidate.get('account') != self.account or not isinstance(candidate.get('chat'), str)
                            or stable_id('msg', self.account, candidate['chat'], str(candidate.get('provider_id'))) != row['message']):
                        raise Failed('invalid_result')
                    try: result = archive.ingest(candidate, lease=self.lease, replay=candidate == row['envelope'])
                    except (ValueError, KeyError, TypeError): raise Failed('accept_rejected') from None
                    if result['outcome'] != 'edited': raise Failed('accept_rejected')
                    # Ingest's own resolve of this row in this transaction (resolved_at at or after its start) is the
                    # same acceptance: the row names the job's rule. The row lock keeps any other writer out.
                    c.execute('''UPDATE message_conflicts SET resolved_at=coalesce(resolved_at,clock_timestamp()),
                        resolved_revision=(SELECT revision FROM messages WHERE id=%s),resolution=%s
                        WHERE receipt_digest=%s AND (resolved_at IS NULL OR resolved_at>=now())''',
                        (row['message'], job.resolution, row['digest']))
                self._in_time()
                self._progress(c, job, row, error if index == len(scanned) - 1 else None)
            start = (row['observed_at'], row['digest'])
        return True

    def _progress(self, c, job, row, error):
        c.execute('''INSERT INTO conflict_reevaluations(job,job_version,account,cursor,watermark,last_error)
            VALUES(%(job)s,%(version)s,%(account)s,%(digest)s,%(at)s,%(error)s)
            ON CONFLICT(job,job_version,account) DO UPDATE SET
            cursor=coalesce(excluded.cursor,conflict_reevaluations.cursor),
            watermark=coalesce(excluded.watermark,conflict_reevaluations.watermark),last_error=excluded.last_error''',
            {'job': job.name, 'version': job.version, 'account': self.account, 'error': error,
             'digest': row['digest'] if row else None, 'at': row['observed_at'] if row else None})

    def _write(self, c, job, *, done=False, error=None):
        with c.transaction():
            self._begin(c)
            c.execute('''INSERT INTO conflict_reevaluations(job,job_version,account,done_at,last_error)
                VALUES(%s,%s,%s,CASE WHEN %s THEN clock_timestamp() END,%s)
                ON CONFLICT(job,job_version,account) DO UPDATE SET last_error=excluded.last_error,
                done_at=coalesce(conflict_reevaluations.done_at,excluded.done_at)''',
                (job.name, job.version, self.account, done, error))

    def _record(self, c, job, code):
        try: self._write(c, job, error=code)
        except Exception as exc:
            if str(exc) == 'collector_lease_lost': raise
            log.warning('conflict_reevaluation_record_failed code=%s', code)


# ANYM-622: a class for each open row stored without one (before schema 54, or by an N-1 writer later).
register(Job('classify', REASON_VERSION, 'classify', reclassify))


def start(archive, account, lease):
    """Started by a collector after it holds its lease; None when no job is registered. Never raises."""
    try:
        store = getattr(archive, 'store', None)
        if store is None or not JOBS: return None
        return Runner(store, account, lease).start()
    except Exception:
        log.warning('conflict_reevaluation code=runner_failed')
        return None


def status(c, accounts):
    """{account: [job progress]} for sync status: version, done, rows resolved, last error. Empty below schema 57
    and for a reader without the grant, so a missing role grant never breaks the rest of the status read."""
    if not stored(c) or not c.execute("SELECT has_table_privilege('public.conflict_reevaluations','SELECT') AS ok").fetchone()['ok']:
        return {}
    result = {}
    for row in c.execute('''SELECT r.account,r.job,r.job_version,r.done_at,r.last_error,
            (SELECT count(*) FROM message_conflicts mc JOIN messages m ON m.id=mc.message WHERE m.account=r.account
                AND mc.resolution='rule_changed:'||r.job||'@'||r.job_version) AS rows_resolved
            FROM conflict_reevaluations r WHERE r.account=ANY(%s) ORDER BY r.account,r.job,r.job_version''',
            (list(accounts),)):
        result.setdefault(row['account'], []).append({'job': row['job'], 'version': row['job_version'],
            'done': row['done_at'] is not None, 'done_at': row['done_at'], 'rows_resolved': row['rows_resolved'],
            'last_error': row['last_error']})
    return result
