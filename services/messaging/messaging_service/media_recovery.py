"""Operator recovery of failed WhatsApp originals (ANYM-816).

An operator queues at most 50 failed jobs of one WhatsApp account (collector_control --media-recover); the collector
claims a queued recovery job only when no other job of the account is claimable (Archive.claim_media), so live,
windowed and agent-requested downloads are never delayed. The durable state is the media_recovery table (069): the
job's error_code keeps ANYM-815's fixed cause and its attempts keep the eight-attempt cap; a recovery spends its own
budget of RECOVERY_BUDGET attempts on the ANYM-699 schedule. A recovery lease is a media_jobs lease whose owner
carries LEASE_PREFIX, so the ordinary claim, sweep and fail paths tell it apart without reading this table.

It creates no agent request (request_key_digest stays), asks no provider and changes no ANYM-806 lifecycle rule.
"""
import re
import secrets
from datetime import datetime, timezone

from .identifiers import ACCOUNT_RE
from .media_inventory import FAILED_JOB
from .media_policy import on_request_chat
from .operational import RECOVERY_STATES
from .media_retry import MEDIA_MAX_ATTEMPTS, MEDIA_PERMANENT_CODES, MEDIA_PROVIDER_PERMANENT_CODES, retry_delay

RECOVERY_BUDGET = 3
RECOVERY_LIMIT = 50
STATES = RECOVERY_STATES
# A recovery that ends in one of these failed again: a new attachment.failed episode (ANYM-867).
FAILING_STATES = frozenset({'provider_unavailable', 'still_failing'})
# A rerun never touches these; a refused job is checked again (its reason may be gone, e.g. a storage pause).
FINAL_STATES = frozenset({'recovered', 'provider_unavailable', 'still_failing'})
REFUSALS = ('lease_running', 'message_deleted', 'collection_disabled', 'chat_not_collected', 'on_request_chat',
            'storage_paused', 'job_changed')
# A media_jobs lease owner of a recovery claim; ':' is outside the worker owner pattern (archive.claim_media).
LEASE_PREFIX = 'recovery:'
# media_jobs.lease_until of a recovery lease: a schema-67 collector (a compatible rollback) claims or sweeps a leased
# job only once its lease_until has passed, so it never takes, re-budgets or rewrites a held recovery job. The real
# expiry is media_recovery.lease_until; job_fence still checks owner and epoch. Finite: psycopg cannot load infinity.
LEASE_HOLD = '9999-12-31 00:00:00+00'
# The CDN no longer has the file (the native adapter maps 403/404/410 to media_unavailable).
PROVIDER_UNAVAILABLE_CODES = frozenset({'unavailable', 'media_unavailable'})
CODE_RE = re.compile(r'[a-z0-9_]{1,64}')
# Rows one claim may settle (refuse, exhaust) before it gives the turn back.
CLAIM_SCAN = 20


def recovery_lease(job):
    return str(job.get('lease_owner') or '').startswith(LEASE_PREFIX)


def refusal(row, *, storage_paused=False):
    """The first check a job fails, or None. `row` carries the columns of CHECKS."""
    if row.get('lease_live'): return 'lease_running'
    if row['deleted']: return 'message_deleted'
    if not row['enabled']: return 'collection_disabled'
    if not row['collected']: return 'chat_not_collected'
    if row['on_request']: return 'on_request_chat'
    if storage_paused: return 'storage_paused'
    return None


# Needs aliases j (media_jobs), m (messages), ch (chats) and p (account_policies, LEFT JOIN). Constants only.
CHECKS = ('m.deleted,p.enabled IS TRUE AS enabled,'
    'coalesce(NOT(ch.provider_chat=ANY(p.exclude_chats)) AND (p.discover_chats OR ch.provider_chat=ANY(p.include_chats)),false) AS collected,'
    + on_request_chat('m')+' AS on_request')
JOINS = '''JOIN messages m ON m.id=j.message AND m.account=j.account
    JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
    LEFT JOIN account_policies p ON p.account=j.account'''


def validate(account, codes, sent_before, limit):
    if not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account): raise ValueError('invalid_account')
    if codes is not None and (not codes or not all(isinstance(code, str) and CODE_RE.fullmatch(code) for code in codes)):
        raise ValueError('invalid_code')
    if sent_before is not None and (not isinstance(sent_before, datetime) or sent_before.tzinfo is None):
        raise ValueError('invalid_sent_before')
    if type(limit) is not int or not 1 <= limit <= RECOVERY_LIMIT: raise ValueError('invalid_limit')


def parse_date(value):
    try: return datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except (TypeError, ValueError): raise ValueError('invalid_sent_before') from None


def recover(c, account, *, codes=None, sent_before=None, limit=RECOVERY_LIMIT, dry_run=False):
    """Queue up to `limit` failed jobs of `account` for one recovery, in this one transaction.

    Each job is checked under the account's archive lock (no edit or delete lands meanwhile); one that fails a check
    is recorded `refused` with its reason. Two runs serialize on an advisory lock; a job whose recovery is queued,
    running or final is never touched. A dry run evaluates the same jobs and writes nothing."""
    validate(account, codes, sent_before, limit)
    c.execute("SET LOCAL lock_timeout='5s'"); c.execute("SET LOCAL statement_timeout='60s'")
    found = c.execute('SELECT platform FROM accounts WHERE id=%s', (account,)).fetchone()
    if not found: raise ValueError('account_not_found')
    if found['platform'] != 'whatsapp': raise ValueError('not_whatsapp_account')
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('media-recovery:'+account,))
    # The same account lock as message edits and finish_media.
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('archive:'+account,))
    # Storage admission writes paused_storage on the jobs it holds back; while any waits, a recovery would only
    # compete for the same capacity.
    paused = c.execute('''SELECT EXISTS(SELECT FROM media_jobs WHERE state='queued' AND error_code='paused_storage')
        AS paused''').fetchone()['paused']
    rows = c.execute('''SELECT j.id,coalesce(j.lease_until>clock_timestamp(),false) AS lease_live,'''+CHECKS+''',
            CASE WHEN jsonb_typeof(j.descriptor->'bytes')='number' THEN (j.descriptor->>'bytes')::bigint END AS bytes,
            r.state AS recovery_state
        FROM media_jobs j '''+JOINS+'''
        LEFT JOIN attachments aa ON aa.id=j.id AND aa.message=j.message AND aa.active
        LEFT JOIN media_recovery r ON r.job=j.id
        WHERE j.account=%(account)s AND aa.id IS NULL AND '''+FAILED_JOB+''' AND (r.job IS NULL OR r.state='refused')
        AND (%(codes)s::text[] IS NULL OR coalesce(j.error_code,'retry_exhausted')=ANY(%(codes)s::text[]))
        AND (%(before)s::timestamptz IS NULL OR m.sent_at<%(before)s::timestamptz)
        ORDER BY r.job IS NOT NULL,m.sent_at DESC,j.id LIMIT %(limit)s FOR UPDATE OF j''',
        {'account': account, 'codes': codes, 'before': sent_before, 'limit': limit}).fetchall()
    run = None if dry_run else secrets.token_hex(16)
    summary = {'account': account, 'dry_run': dry_run, 'run_id': run, 'matching': len(rows),
               'bytes': sum(r['bytes'] for r in rows if r['bytes'] is not None),
               'bytes_unknown': sum(1 for r in rows if r['bytes'] is None), 'queued': 0, 'refused': {}}
    for row in rows:
        reason = refusal(row, storage_paused=paused)
        if reason: summary['refused'][reason] = summary['refused'].get(reason, 0)+1
        else: summary['queued'] += 1
        if dry_run: continue
        c.execute('''INSERT INTO media_recovery(job,account,run_id,state,refusal_reason,next_due)
            VALUES(%s,%s,%s,%s,%s,clock_timestamp())
            ON CONFLICT(job) DO UPDATE SET run_id=EXCLUDED.run_id,state=EXCLUDED.state,
                refusal_reason=EXCLUDED.refusal_reason,next_due=EXCLUDED.next_due,updated_at=clock_timestamp()
            WHERE media_recovery.state='refused' ''',
            (row['id'], account, run, 'refused' if reason else 'queued', reason))
    return summary


def _settle(c, job, state, *, reason=None, code=None, leased=False):
    c.execute('''UPDATE media_recovery SET state=%s,refusal_reason=%s,last_code=coalesce(%s,last_code),
        lease_until=NULL,updated_at=clock_timestamp() WHERE job=%s''', (state, reason, code, job))
    if leased:
        c.execute("UPDATE media_jobs SET state='failed',lease_until=NULL WHERE id=%s", (job,))


def next_job(c, account):
    """The next claimable recovery job (its media_jobs row, locked), or None.

    Called by Archive.claim_media inside its locks, after it found no other job. A queued recovery whose job is
    no longer failed, or whose message or chat no longer qualifies, is refused here; a running recovery whose lease
    expired resumes, or ends still_failing when its budget is spent."""
    for _ in range(CLAIM_SCAN):
        row = c.execute('''SELECT j.*,r.state AS _recovery_state,r.attempts AS _recovery_attempts,'''+CHECKS+''',
                (j.state='failed' OR j.state='queued' AND j.attempts>=%(max)s) AS _job_failed,
                (j.state='leased' AND coalesce(j.lease_owner,'') LIKE %(prefix)s) AS _recovery_leased
            FROM media_recovery r JOIN media_jobs j ON j.id=r.job AND j.account=r.account '''+JOINS+'''
            WHERE (%(account)s::text IS NULL OR r.account=%(account)s)
            AND (r.state='queued' AND r.next_due<=clock_timestamp() OR r.state='running' AND NOT
                (j.state='leased' AND coalesce(j.lease_owner,'') LIKE %(prefix)s
                 AND coalesce(r.lease_until,'-infinity')>=clock_timestamp()))
            ORDER BY r.next_due,r.job FOR UPDATE OF r,j SKIP LOCKED LIMIT 1''',
            {'account': account, 'max': MEDIA_MAX_ATTEMPTS, 'prefix': LEASE_PREFIX+'%'}).fetchone()
        if not row: return None
        leased = row['_recovery_leased']
        valid = row['_job_failed'] if row['_recovery_state'] == 'queued' else leased or row['_job_failed']
        reason = refusal(row) or (None if valid else 'job_changed')
        if reason:
            _settle(c, row['id'], 'refused', reason=reason, leased=leased)
            continue
        if row['_recovery_attempts'] >= RECOVERY_BUDGET:
            # Its last attempt's lease expired: the collector stopped or lost it mid-transfer.
            _settle(c, row['id'], 'still_failing', code='lease_expired', leased=leased)
            from . import attachment_failures
            attachment_failures.publish(c, [{**row, 'reason': 'retry_exhausted'}])
            continue
        return row
    return None


def held_back(c, job, *, size_limit=False):
    """Storage admission held a recovery claim back: a size limit ends it, a pause retries in 60 s, no attempt spent."""
    leased = job['state'] == 'leased'
    if size_limit:
        _settle(c, job['id'], 'still_failing', code='size_limit', leased=leased)
        return
    c.execute('''UPDATE media_recovery SET state='queued',next_due=clock_timestamp()+interval '60 seconds',
        lease_until=NULL,updated_at=clock_timestamp() WHERE job=%s''', (job['id'],))
    if leased: c.execute("UPDATE media_jobs SET state='failed',lease_until=NULL WHERE id=%s", (job['id'],))


def lease(c, job, owner, expires):
    """Lease the job for a recovery attempt. The job's attempts, error_code and request columns stay as they are."""
    claimed = c.execute('''UPDATE media_jobs SET state='leased',lease_owner=%s,lease_epoch=lease_epoch+1,
        lease_until=%s::timestamptz WHERE id=%s RETURNING *''', (LEASE_PREFIX+owner, LEASE_HOLD, job['id'])).fetchone()
    c.execute('''UPDATE media_recovery SET state='running',attempts=attempts+1,next_due=clock_timestamp(),
        lease_until=%s,updated_at=clock_timestamp() WHERE job=%s''', (expires, job['id']))
    return claimed


def renew(c, job, ttl):
    """renew_media of a recovery lease: the expiry moves in media_recovery; media_jobs keeps LEASE_HOLD."""
    return c.execute('''UPDATE media_recovery SET lease_until=clock_timestamp()+%s*interval '1 second'
        WHERE job=%s RETURNING lease_until''', (ttl, job['id'])).fetchone()


def finished(c, job):
    """finish_media stored the original: the recovery ends recovered (same transaction)."""
    c.execute('''UPDATE media_recovery SET state='recovered',last_code=NULL,lease_until=NULL,updated_at=clock_timestamp()
        WHERE job=%s AND state='running' ''', (job['id'],))


def outcome(attempts, code, *, permanent=False, retry_after=None, local=False):
    """(state, attempts, wait seconds) after a failed recovery attempt; `attempts` counts the one just spent."""
    if local:
        # Local storage or a pause is operator-fixable: the attempt is given back.
        return 'queued', max(0, attempts-1), 60 if code in ('disk_reserve', 'paused_storage') else 300
    if code in PROVIDER_UNAVAILABLE_CODES: return 'provider_unavailable', attempts, 0
    if permanent or code in MEDIA_PERMANENT_CODES or code in MEDIA_PROVIDER_PERMANENT_CODES or attempts >= RECOVERY_BUDGET:
        return 'still_failing', attempts, 0
    return 'queued', attempts, retry_delay(attempts, retry_after)


def failed(c, job, code, *, permanent=False, retry_after=None, local=False):
    """fail_media of a recovery lease: the job goes back to failed with its recorded cause; the outcome is here."""
    row = c.execute("SELECT attempts FROM media_recovery WHERE job=%s AND state='running' FOR UPDATE",
        (job['id'],)).fetchone()
    c.execute("UPDATE media_jobs SET state='failed',lease_until=NULL WHERE id=%s", (job['id'],))
    if not row: return None
    state, attempts, wait = outcome(row['attempts'], code, permanent=permanent, retry_after=retry_after, local=local)
    c.execute('''UPDATE media_recovery SET state=%s,attempts=%s,last_code=%s,lease_until=NULL,
        next_due=clock_timestamp()+%s*interval '1 second',updated_at=clock_timestamp() WHERE job=%s''',
        (state, attempts, code, wait, job['id']))
    return state


def counts(c, accounts):
    """Recovery state counts per account, every state present (status: account media `recovery`), or None when
    this database or role cannot read them (schema 67, a gateway role without its grant): status then reports
    recovery as unknown and keeps everything else."""
    import psycopg
    found = {account: dict.fromkeys(STATES, 0) for account in accounts}
    if not found: return found
    try:
        with c.transaction():
            rows = c.execute('''SELECT account,state,count(*) AS count FROM media_recovery WHERE account=ANY(%s)
                GROUP BY 1,2''', (list(found),)).fetchall()
    except (psycopg.errors.InsufficientPrivilege, psycopg.errors.UndefinedTable):
        return None
    for row in rows: found[row['account']][row['state']] = row['count']
    return found
