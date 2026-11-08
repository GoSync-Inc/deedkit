"""Read-only check of the mini's enrichment queue: stall, starvation, no worker (ANYM-785)."""
from contextlib import contextmanager
import time

from . import telemetry
from .file_lifecycle import PROCESSABLE_ORIGINAL
from .media_policy import RECOGNITION_WITHHELD
from .coverage import DOCUMENT_TEXT, EVICTED_ROW, EXCLUSION_JOINS, KIND, WITHHELD_ROW, exclusion_sets

MINI_PROVIDER = 'deedkit_inference'
CHECK_PERIOD_S = 300
STALL_S = 6 * 3600
STARVED_ROWS = 50
STARVED_MAX_AGE_S = 48 * 3600
NO_WORKER_S = 30 * 60
NO_WORKER_MAX_AGE_S = 7 * 86400
REPEAT_S = 86400
STATEMENT_TIMEOUT_MS = 5000
LOCK_TIMEOUT_MS = 1000

STALLED, STARVED, NO_WORKER = 'enrichment_stalled', 'enrichment_starved', 'enrichment_no_worker'

# A scope is (account, kind, profile_hash). Only current rows a claim could take count (a withheld file of an
# on-request chat no agent asked for, ANYM-805, is not one): a row of a
# replaced revision, a deleted message or an evicted original waits forever and would alert forever.
SCOPES_SQL = 'WITH '+exclusion_sets(RECOGNITION_WITHHELD)+''',
    queued AS MATERIALIZED (
        SELECT m.account,e.kind,e.profile_hash,count(*) AS queued,min(e.updated_at) AS oldest_queued,
            count(*) FILTER (WHERE m.sent_at<=now()-make_interval(secs=>%(stall)s)
                AND m.sent_at>=now()-make_interval(secs=>%(starved_age)s)) AS window_rows
        FROM attachment_enrichment e JOIN attachments a ON a.id=e.attachment JOIN messages m ON m.id=a.message
        WHERE e.state='queued' AND e.provider=%(provider)s AND e.kind IS NOT NULL AND e.profile_hash IS NOT NULL
            AND a.active AND NOT m.deleted AND e.source_digest=a.digest AND e.source_revision=m.revision
            AND '''+PROCESSABLE_ORIGINAL.replace('%', '%%')+''' AND NOT '''+RECOGNITION_WITHHELD.replace('%', '%%')+'''
        GROUP BY 1,2,3),
    ready AS (
        SELECT m.account,e.kind,e.profile_hash,max(e.updated_at) AS newest
        FROM attachment_enrichment e JOIN attachments a ON a.id=e.attachment JOIN messages m ON m.id=a.message
        WHERE e.state='ready' AND e.provider=%(provider)s
            AND (e.kind,e.profile_hash) IN (SELECT kind,profile_hash FROM queued)
        GROUP BY 1,2,3),
    -- ANYM-812: due rows still unconfigured (no claim has touched them yet) are waiting work too. Their scope is
    -- (account, kind from mime, the provider of the account's configured profile); progress is any ready row of it.
    -- It stalls only while that profile is enabled and unexpired; a stopped worker is enrichment_no_worker.
    -- A row waits from its updated_at, so a row stored seconds ago does not count at once. The withheld and evicted
    -- rules are read from their sets (coverage.exclusion_sets), joined once by hash. The set counts every account's
    -- due unconfigured rows in one pass, like sync status's file pass; the mini's profiles select scopes below
    -- (production 2026-11-08: a profile join inside this pass made the planner probe row by row, 16 s).
    waiting AS MATERIALIZED (
        SELECT m.account,'''+KIND+''' AS kind,count(*) AS queued,min(e.updated_at) AS oldest,
            count(*) FILTER (WHERE m.sent_at<=now()-make_interval(secs=>%(stall)s)
                AND m.sent_at>=now()-make_interval(secs=>%(starved_age)s)) AS window_rows
        FROM attachments a JOIN messages m ON m.id=a.message
        JOIN attachment_enrichment e ON e.attachment=a.id AND e.source_digest=a.digest AND e.source_revision=m.revision
        LEFT JOIN attachment_text f ON f.attachment=a.id'''+EXCLUSION_JOINS+'''
        WHERE e.state='unconfigured' AND a.active AND NOT m.deleted
            AND NOT '''+WITHHELD_ROW+''' AND NOT '''+EVICTED_ROW+'''
            AND ('''+KIND+"""<>'document' OR coalesce("""+DOCUMENT_TEXT+''',false))
        GROUP BY 1,2),
    waiting_ready AS (
        SELECT m.account,e.kind,max(e.updated_at) AS newest
        FROM attachment_enrichment e JOIN attachments a ON a.id=e.attachment JOIN messages m ON m.id=a.message
        WHERE e.state='ready' AND e.provider=%(provider)s AND (m.account,e.kind) IN (SELECT account,kind FROM waiting)
        GROUP BY 1,2)
    SELECT q.account,q.kind,q.queued,q.window_rows,
        coalesce(p.enabled AND p.expires_at>now(),false)
            AND coalesce(r.newest,q.oldest_queued)<=now()-make_interval(secs=>%(stall)s) AS stalled
    FROM queued q LEFT JOIN ready r USING(account,kind,profile_hash)
    LEFT JOIN processing_profiles p ON p.account=q.account AND p.kind=q.kind AND p.provider=%(provider)s
    UNION ALL
    SELECT w.account,w.kind,w.queued,w.window_rows,
        coalesce(p.enabled AND p.expires_at>now(),false) AND w.oldest<=now()-make_interval(secs=>%(stall)s)
            AND coalesce(wr.newest,'-infinity')<=now()-make_interval(secs=>%(stall)s) AS stalled
    FROM waiting w JOIN processing_profiles p ON p.account=w.account AND p.kind=w.kind AND p.provider=%(provider)s
        AND p.observed_at>=now()-make_interval(secs=>%(no_worker_age)s)
    LEFT JOIN waiting_ready wr ON wr.account=w.account AND wr.kind=w.kind'''
WORKERS_SQL = '''SELECT account FROM processing_profiles
    WHERE provider=%(provider)s AND NOT (enabled AND expires_at>now())
        AND observed_at<=now()-make_interval(secs=>%(no_worker)s)
        AND observed_at>=now()-make_interval(secs=>%(no_worker_age)s)'''

_last = None


class Episodes:
    """One event per code per episode, again every REPEAT_S while it lasts; memory only."""
    def __init__(self):
        self.sent = {}

    def report(self, raised, now, send):
        """Returns the codes that were due. A refused event stays due for the next check."""
        self.sent = {code: at for code, at in self.sent.items() if code in raised}
        due = sorted(c for c in raised if c not in self.sent or now - self.sent[c] >= REPEAT_S)
        for code in due:
            if send(code):
                self.sent[code] = now
        return due


_episodes = Episodes()


@contextmanager
def read_only(store):
    with store.pool.connection() as c:
        c.execute('SET TRANSACTION READ ONLY')
        c.execute(f'SET LOCAL statement_timeout={int(STATEMENT_TIMEOUT_MS)}')
        c.execute(f'SET LOCAL lock_timeout={int(LOCK_TIMEOUT_MS)}')
        # ANYM-812 (production 2026-11-08): JIT compilation of SCOPES_SQL took about 4 s of its 5 s budget.
        c.execute('SET LOCAL jit=off')
        yield c


def evaluate(store):
    """One full reading: {'codes', 'checked_at', 'accounts'}. No advisory lock, so no claim waits on it."""
    params = {'provider': MINI_PROVIDER, 'stall': STALL_S, 'starved_age': STARVED_MAX_AGE_S,
              'no_worker': NO_WORKER_S, 'no_worker_age': NO_WORKER_MAX_AGE_S}
    with read_only(store) as c:
        checked_at = c.execute('SELECT now() AS at').fetchone()['at']
        scopes = c.execute(SCOPES_SQL, params).fetchall()
        workers = c.execute(WORKERS_SQL, params).fetchall()
    accounts, codes = {}, set()

    def account(name):
        return accounts.setdefault(name, {'codes': set(), 'counts': {}})
    for row in scopes:
        item = account(row['account'])
        counts = item['counts'].setdefault(row['kind'], {'queued': 0, 'sent_6h_to_48h': 0})
        counts['queued'] += row['queued']
        counts['sent_6h_to_48h'] += row['window_rows']
        if row['stalled']:
            item['codes'].add(STALLED)
    # ANYM-812: starvation counts an (account, kind) of the mini as a whole, queued and due unconfigured rows
    # together, so a claim that moves a row from one to the other (and defers it) does not clear it.
    for item in accounts.values():
        if any(counts['sent_6h_to_48h'] >= STARVED_ROWS for counts in item['counts'].values()):
            item['codes'].add(STARVED)
    for row in workers:
        account(row['account'])['codes'].add(NO_WORKER)
    for item in accounts.values():
        codes |= item['codes']
        item['codes'] = sorted(item['codes'])
    return {'codes': codes, 'checked_at': checked_at, 'accounts': accounts}


def raise_event(code):
    """capture() needs an exception; this frame gives the event its component (TIDEN-CI.md)."""
    try:
        raise RuntimeError(code)
    except RuntimeError as error:
        return telemetry.capture(error, code)


def check(store, now=None):
    """One period. A failure raises before any state changes, so an episode does not end on it."""
    global _last
    result = evaluate(store)
    _last = result
    _episodes.report(result['codes'], time.monotonic() if now is None else now, raise_event)


def read(accounts):
    """The last in-memory result for these accounts; None before the first check. Runs no query."""
    last = _last
    if last is None:
        return None
    return {'checked_at': last['checked_at'],
            'accounts': {name: last['accounts'].get(name) or {'codes': [], 'counts': {}} for name in accounts}}
