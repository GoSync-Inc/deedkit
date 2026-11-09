"""ANYM-867: a download that ends for good commits one attachment.failed event per message and terminal sweep.

The callers mark the jobs failed and call publish() in that same transaction, only for the jobs that statement moved
into their terminal state, so a replay, a concurrent sweep and finish, or a crash and retry give no second event:
the job is either still non-terminal (the transaction rolled back, nothing committed) or already failed (no
transition, no call). The event key names the message, the job IDs and each job's terminal episode (1 + the
attachment.failed events that already name the job), so a requeue that fails again commits a new event and the key's
unique index stops any duplicate the transition rule missed.

The event carries attachment IDs, fixed reason codes, a next action and the attempt count: no file name or content.
A publication that keeps failing never holds the job or the sweep: after PUBLISH_TRIES tries in savepoints, each under
BUDGET lock and statement timeouts, the message's jobs stay failed without an event (set aside), counted in SET_ASIDE
and JOURNAL_FAILURES by reason code. An unusable connection propagates and rolls the transition back.

Lock order: the event's insert takes the message's key-share lock (its foreign key), and ingest and the purge lock
the message before its jobs. Every caller therefore holds the message before it locks the job rows or, where the job
row is already locked (a claim), takes the message with NOWAIT (lock_message) and leaves the job for a later pass.
"""
from collections import Counter
import json
import re

KIND = 'attachment.failed'
PUBLISH_TRIES = 3
# Per statement of one publication: far below the pool's 5 s statement timeout and a claim's lock holding time.
BUDGET = '500ms'
# Jobs set aside since process start, by the publication's reason code.
SET_ASIDE = Counter()
SIZE_CODES = frozenset({'size_limit', 'media_size_limit'})
TIMEOUTS = ("SELECT current_setting('statement_timeout') AS statement,"
    "current_setting('lock_timeout') AS lock")
SET_TIMEOUTS = "SELECT set_config('statement_timeout',%s,true),set_config('lock_timeout',%s,true)"


def reason_of(code):
    """The card's fixed vocabulary: a size refusal, else the end of the job's retry budget."""
    return 'size_limit' if code in SIZE_CODES else 'retry_exhausted'


def _code(error):
    text = error.args[0] if error.args and isinstance(error.args[0], str) else ''
    return text if re.fullmatch(r'[a-z0-9_]{1,64}', text) else type(error).__name__


def _unusable(c):
    from psycopg import pq
    return c.closed or c.broken or c.info.transaction_status != pq.TransactionStatus.INTRANS


def bounded(c):
    """This transaction's lock and statement timeouts become BUDGET (a sweep transaction of its own)."""
    c.execute(SET_TIMEOUTS, (BUDGET, BUDGET))


def lock_message(c, message):
    """The message's key-share lock without waiting, for a caller that already holds a job row of it: False when an
    edit, delete or purge holds the message (that transaction would then wait for this one's job lock)."""
    import psycopg
    try:
        with c.transaction():
            c.execute('SELECT FROM messages WHERE id=%s FOR KEY SHARE NOWAIT', (message,))
        return True
    except psycopg.errors.LockNotAvailable:
        if _unusable(c):
            raise
        return False


def publish(c, jobs):
    """jobs: dicts with id, account, message, attempts, reason (reason_of), each just moved to failed on `c`, whose
    messages `c` already holds (module docstring, lock order).

    One event per message, each in its own savepoint. Never raises for a publication error of one message."""
    if not jobs:
        return []
    by_message = {}
    for job in jobs:
        by_message.setdefault((job['account'], job['message']), []).append(job)
    before = c.execute(TIMEOUTS).fetchone()
    bounded(c)
    published = []
    for (account, message), group in by_message.items():
        error = None
        for _ in range(PUBLISH_TRIES):
            try:
                with c.transaction():
                    published.append(_append(c, account, message, group))
                error = None
                break
            except Exception as exc:  # one message's event never stops the sweep or the worker loop
                if _unusable(c):
                    raise
                error = exc
        if error is not None:
            _set_aside(group, error)
    c.execute(SET_TIMEOUTS, (before['statement'], before['lock']))
    return published


def keep_null_actions(items):
    """The page SQL strips null fields (jsonb_strip_nulls): a failed file's next_action is null, not absent."""
    for item in items:
        processing = item.get('processing') if item.get('kind') == KIND else None
        entries = processing.get('failed') if isinstance(processing, dict) else None
        for entry in entries if isinstance(entries, list) else ():
            if isinstance(entry, dict):
                entry.setdefault('next_action', None)


def _set_aside(group, error):
    from .subscriptions import JOURNAL_FAILURES
    code = _code(error)
    SET_ASIDE[code] += len(group)
    JOURNAL_FAILURES[KIND] += 1
    try:  # diagnostics never fail the transition
        print(json.dumps({'error': 'attachment_failed_event_set_aside', 'code': code, 'jobs': len(group)}), flush=True)
    except Exception:
        pass


def _append(c, account, message, group):
    from .envelope import stable_id
    from .store import Store
    ids = sorted({job['id'] for job in group})
    earlier = {r['id']: r['n'] for r in c.execute('''SELECT f->>'attachment' AS id,count(*) AS n FROM events e
        CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(e.snapshot->'processing'->'failed')='array'
            THEN e.snapshot->'processing'->'failed' ELSE '[]'::jsonb END) f
        WHERE e.message=%s AND e.kind=%s AND f->>'attachment'=ANY(%s) GROUP BY 1''', (message, KIND, ids))}
    key = stable_id('event', KIND, message, [[ident, earlier.get(ident, 0) + 1] for ident in ids])
    existing = c.execute('SELECT id FROM events WHERE event_key=%s', (key,)).fetchone()
    if existing:
        return existing['id']
    # ANYM-866's vocabulary: a failed job with no stored original has no next action; one whose original is stored
    # (this was a re-download) comes back on request (media_requests.request). Only the collector's own grants are read.
    stored = {r['id'] for r in c.execute('SELECT id FROM attachments WHERE id=ANY(%s) AND active', (ids,))}
    reasons = {job['id']: job['reason'] for job in group}
    failed = [{'attachment': ident, 'reason': reasons[ident],
        'next_action': 'request_file_download' if ident in stored else None} for ident in ids]
    source = c.execute('''SELECT r.snapshot->>'origin' AS origin FROM messages m
        JOIN message_revisions r ON r.message=m.id AND r.revision=m.revision WHERE m.id=%s''', (message,)).fetchone()
    return Store.append_event(c, account, message, KIND, (source or {}).get('origin') or 'reconcile', key,
        snapshot_metadata={'processing': {'stage': 'original', 'state': 'failed', 'failed': failed,
            'attempts': max(int(job.get('attempts') or 0) for job in group)}})
