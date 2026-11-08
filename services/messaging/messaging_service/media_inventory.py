"""Failed WhatsApp originals: the read-time expiry proxy and a read-only operator inventory (ANYM-815).

Schema 67 records no first-attempt time (ANYM-699), so the only basis for "the CDN copy had likely expired" is
`age_when_queued`: the message's age when its job was created. Nothing here rewrites a job or asks the provider;
the media key column comes from the native adapter's read-only capture journal lookup (`--media-keys`).
"""
import json
import subprocess

import psycopg
from psycopg.rows import dict_row

from .enrichment_providers import private_text
from .identifiers import ACCOUNT_RE
from .media_policy import ON_REQUEST_CHATS
from .media_retry import LIKELY_EXPIRED_CODES, MEDIA_MAX_ATTEMPTS, PROVIDER_MEDIA_RETENTION_DAYS

RETENTION_DAYS = PROVIDER_MEDIA_RETENTION_DAYS['whatsapp']
# Needs aliases j (media_jobs) and m (messages). Constants only, no caller input.
LIKELY_EXPIRED = ("((SELECT lea.platform FROM accounts lea WHERE lea.id=j.account)='whatsapp'"
    " AND j.created_at-m.sent_at>interval '"+str(RETENTION_DAYS)+" days'"
    " AND coalesce(j.error_code,'retry_exhausted') IN ("+','.join("'"+code+"'" for code in sorted(LIKELY_EXPIRED_CODES))+'))')
# A job the read paths report as failed: terminal, or its attempts are spent.
FAILED_JOB = "(j.state='failed' OR j.state='queued' AND j.attempts>="+str(MEDIA_MAX_ATTEMPTS)+')'
KEY_BATCH = 200
KEY_TIMEOUT_SECONDS = 60
AGE_BUCKETS = ((30, 'under_30d'), (90, '30d_to_90d'), (365, '90d_to_1y'))


def now_age(days):
    for bound, name in AGE_BUCKETS:
        if days < bound: return name
    return 'over_1y'


def mime_class(mime):
    kind = (mime or '').split(';', 1)[0].strip().lower()
    if kind.startswith(('image/', 'audio/', 'video/')): return kind.split('/', 1)[0]
    return 'document' if kind else 'unknown'


def failed_jobs(c, account):
    """The account's failed WhatsApp jobs; plain reads only."""
    return c.execute('''SELECT j.id,coalesce(j.error_code,'retry_exhausted') AS code,
            ch.provider_chat,m.provider_id,j.descriptor->>'mime' AS mime,
            coalesce(aa.bytes,CASE WHEN jsonb_typeof(j.descriptor->'bytes')='number' THEN (j.descriptor->>'bytes')::bigint END) AS bytes,
            eor.chat IS NOT NULL AS on_request,
            j.created_at-m.sent_at>make_interval(days=>%s) AS queued_old,
            extract(epoch FROM clock_timestamp()-m.sent_at)/86400 AS age_days
        FROM media_jobs j JOIN accounts ac ON ac.id=j.account AND ac.platform='whatsapp'
        JOIN messages m ON m.id=j.message AND m.account=j.account
        JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
        LEFT JOIN attachments aa ON aa.id=j.id AND aa.message=j.message AND aa.active
        LEFT JOIN ('''+ON_REQUEST_CHATS+''') eor ON eor.chat=m.chat
        WHERE j.account=%s AND aa.id IS NULL AND NOT m.deleted AND '''+FAILED_JOB+'''
        ORDER BY j.id''', (RETENTION_DAYS, account)).fetchall()


KEY_STATES = ('held', 'none', 'unknown')


def adapter_keys(executable, config, refs, *, run=None):
    """held/none/unknown per (chat, message) from the native adapter's journal lookup, None when it cannot answer.

    One request line per batch of at most KEY_BATCH ids; the adapter opens its journal read-only and no client."""
    if not refs: return []
    run = run or subprocess.run
    lines = [json.dumps({'id': n+1, 'method': 'media_keys', 'params': {'messages': [
        {'chat': chat, 'message': message} for chat, message in refs[start:start+KEY_BATCH]]}}, separators=(',', ':'))
        for n, start in enumerate(range(0, len(refs), KEY_BATCH))]
    try:
        done = run([executable, '--config', str(config), '--media-keys'], input=''.join(x+'\n' for x in lines),
            capture_output=True, text=True, timeout=KEY_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    answers = done.stdout.splitlines() if done.returncode == 0 else []
    if len(answers) != len(lines): return None
    keys = []
    for n, (line, start) in enumerate(zip(answers, range(0, len(refs), KEY_BATCH))):
        try: reply = json.loads(line)
        except ValueError: return None
        result = reply.get('result') if isinstance(reply, dict) and reply.get('id') == n+1 else None
        values = result.get('keys') if isinstance(result, dict) else None
        if not isinstance(values, list) or len(values) != min(KEY_BATCH, len(refs)-start) or \
                not all(v in KEY_STATES for v in values):
            return None
        keys.extend(values)
    return keys


def native_adapter(config, account):
    """(executable, config path) of the account's native WhatsApp collector config, or None.

    A config of another account is refused: its journal would answer for the wrong messages."""
    if config is None: return None
    try: value = json.loads(private_text(config, 1 << 20))
    except (OSError, ValueError): return None
    if isinstance(value, dict) and value.get('account') != account: raise ValueError('collector_config_mismatch')
    if not isinstance(value, dict) or value.get('provider') != 'whatsmeow' or not isinstance(value.get('native_executable'), str):
        return None
    return value['native_executable'], config


def inventory(c, account, *, adapter=None, run=None):
    """Grouped counts of the account's failed WhatsApp jobs; no message, chat or file identity is printed."""
    rows = failed_jobs(c, account)
    keys = adapter_keys(adapter[0], adapter[1], [(r['provider_chat'], r['provider_id']) for r in rows], run=run) \
        if adapter and rows else None
    groups = {}
    for n, row in enumerate(rows):
        key = (row['code'], 'on_request' if row['on_request'] else 'automatic',
            'over_30d' if row['queued_old'] else 'under_30d', now_age(float(row['age_days'])), mime_class(row['mime']))
        item = groups.setdefault(key, {'code': key[0], 'chat_kind': key[1], 'age_when_queued': key[2],
            'now_age': key[3], 'mime_class': key[4], 'jobs': 0, 'bytes_known': 0, 'bytes': 0,
            'media_key_held': 0, 'media_key_none': 0, 'media_key_unknown': 0})
        item['jobs'] += 1
        if row['bytes'] is not None:
            item['bytes_known'] += 1; item['bytes'] += int(row['bytes'])
        item['media_key_'+('unknown' if keys is None else keys[n])] += 1
    return {'account': account, 'failed_jobs': len(rows), 'retention_days_estimate': RETENTION_DAYS,
        'age_basis': 'age_when_queued', 'media_key_basis': 'native_journal' if keys is not None else 'unavailable',
        'groups': [groups[k] for k in sorted(groups)]}


def run_inventory(dsn_file, account, config=None):
    if not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account): raise ValueError('invalid_account')
    with psycopg.connect(private_text(dsn_file).strip(), row_factory=dict_row, connect_timeout=10) as c:
        # A read-only session: any write the inventory attempted would fail instead of landing.
        c.read_only = True
        if not c.execute("SELECT 1 FROM accounts WHERE id=%s AND platform='whatsapp'", (account,)).fetchone():
            raise ValueError('invalid_account')
        return inventory(c, account, adapter=native_adapter(config, account))
