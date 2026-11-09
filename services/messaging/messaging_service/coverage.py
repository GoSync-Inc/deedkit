"""Per-account coverage under the current collection policy (ANYM-812).

One verdict per account: is everything the policy gives the collector, the indexer and the enrichment worker done?
Files the policy leaves to an agent's request are counted apart as available on request, never as waiting work.
The SQL fragments below are the one definition of each bucket; sync status and enrichment_health both use them.
The workers' claims keep their own order, caps and leases (tests/test_sync_coverage.py holds them to the same rows).
"""
from datetime import datetime, timedelta, timezone

from .file_lifecycle import PROCESSABLE_ORIGINAL
from .media_policy import description_skipped_row

# WhatsApp publishes no media retention; integrators report about 30 days (an estimate, not a provider guarantee).
WHATSAPP_MEDIA_RETENTION_DAYS = 30
RETRY_CEILING = 3  # EnrichmentWorker.claim: a failed row is retried while attempts < 3 (enrichment.py)
RECOGNITION_KINDS = ('audio', 'image', 'document')

# The enrichment kind of an attachment (alias a), as EnrichmentWorker.claim and sync status decide it. Escaped for
# statements that carry parameters.
KIND = ("CASE WHEN a.mime LIKE 'audio/%%' OR a.mime='application/ogg' THEN 'audio' "
        "WHEN a.mime LIKE 'image/%%' THEN 'image' ELSE 'document' END")
# A configured kind: the account has a processing_profiles row for it observed within CONFIGURED_DAYS, enabled or
# not. A clean worker stop sets enabled=false (EnrichmentWorker.unpublish_profiles) and must read as a stopped worker
# whose files are still due, not as recognition switched off. Same bound as enrichment_health.NO_WORKER_MAX_AGE_S.
CONFIGURED_DAYS = 7
# Extracted text a document description can start from (alias f: the attachment's current text row).
DOCUMENT_TEXT = "(f.state='ready' AND f.source_digest=a.digest AND f.source_revision=m.revision AND f.text_content<>'')"
EVICTED = 'NOT ' + PROCESSABLE_ORIGINAL


# Criterion 6. Production 2026-11-08: the policy predicates as per-row CASE subplans took 3.6-5.7 s against the
# 0.5 s of the statements before this card, and even alone, applied row by row, the withheld rule costs about 1.1 s.
# So each set is computed once per statement and a row reads hash-joined flags:
# - on-request chats: media_policy.ON_REQUEST_CHATS verbatim (about 0.2 s, ANYM-806's lane: imported, not copied);
# - withheld (media_policy.RECOGNITION_WITHHELD): the row's chat is in that set and its media job carries no
#   request; the job is joined by its id, which is the attachment's id;
# - evicted: file_lifecycle.PROCESSABLE_ORIGINAL verbatim, applied to attachment_residency (alias a: digest, bytes).
# tests/test_sync_coverage.py holds these flags to the claims, which apply the predicates themselves.
def exclusion_sets(withheld):
    """The `on_request_chats` and `evicted_originals` CTEs, escaped for parameterized statements.

    `withheld` is media_policy.recognition_withheld(c): the on-request rule, or 'false' for a login that cannot
    read its columns (then no chat is on request, as for the claims)."""
    from .media_policy import ON_REQUEST_CHATS
    chats = ('SELECT DISTINCT orc.chat FROM (' + ON_REQUEST_CHATS + ') orc' if withheld != 'false'
             else 'SELECT NULL::text AS chat WHERE false')
    return ('on_request_chats AS MATERIALIZED (' + chats.replace('%', '%%') + '),'
            '\n    evicted_originals AS MATERIALIZED (SELECT a.digest,a.bytes FROM attachment_residency a WHERE '
            + EVICTED.replace('%', '%%') + ')')


def policy_sets(withheld):
    """Every CTE the bucket expressions read (sync status)."""
    return (exclusion_sets(withheld) + ',\n    configured_kinds AS MATERIALIZED (SELECT account,kind'
            " FROM processing_profiles WHERE observed_at>=clock_timestamp()-interval '"
            + str(CONFIGURED_DAYS) + " days')")


# The joins that give a row its flags (aliases a, m); each matches at most one row (chat id, job id, residency
# digest, profile key), so no row is counted twice.
EXCLUSION_JOINS = ('\n        LEFT JOIN on_request_chats orq ON orq.chat=m.chat'
                   '\n        LEFT JOIN media_jobs wj ON wj.id=a.id'
                   '\n        LEFT JOIN evicted_originals eo ON eo.digest=a.digest AND eo.bytes=a.bytes')
POLICY_JOINS = (EXCLUSION_JOINS
                + '\n        LEFT JOIN configured_kinds ck ON ck.account=m.account AND ck.kind=' + KIND)
WITHHELD_ROW = '(orq.chat IS NOT NULL AND wj.id IS NOT NULL AND wj.request_key_digest IS NULL)'
EVICTED_ROW, CONFIGURED_ROW = 'eo.digest IS NOT NULL', 'ck.kind IS NOT NULL'
# Owner 11-09: an image whose description waits for a request (media_policy.DESCRIPTION_SKIPPED), read on the job
# joined above: no extra pass over attachments; a sticker probes message_metadata by its key.
SKIPPED_ROW = description_skipped_row('wj')


# enrichment.FINAL_LIMIT_CODES: a failure the same profile never retries (a test keeps the two equal, so the API
# does not import the worker module).
FINAL_LIMIT_CODES = ('original_size_limit', 'audio_duration_limit', 'audio_transcode_size_limit')


def recognition_bucket():
    """The bucket of one enrichment row e (aliases a, m, e, f and POLICY_JOINS), first match wins (ANYM-812)."""
    final = ','.join("'" + code + "'" for code in FINAL_LIMIT_CODES)
    return ('''CASE WHEN e.state IS NULL THEN 'unknown'
        WHEN e.state='ready' THEN 'ready'
        WHEN e.state='failed' AND (e.attempts>=''' + str(RETRY_CEILING) + " OR coalesce(e.error_code,'') IN ("
            + final + ''')) THEN 'failed_final'
        WHEN e.state='unsupported' THEN 'unsupported'
        WHEN e.state='leased' THEN 'leased'
        WHEN ''' + WITHHELD_ROW + ''' THEN 'withheld'
        WHEN ''' + EVICTED_ROW + ''' THEN 'evicted'
        WHEN NOT ''' + CONFIGURED_ROW + ''' THEN 'recognition_off'
        WHEN ''' + SKIPPED_ROW + ''' THEN 'skipped'
        WHEN ''' + KIND + "='document' AND NOT coalesce(" + DOCUMENT_TEXT + ''',false) THEN 'no_text'
        WHEN e.state='failed' THEN 'failed_retry'
        ELSE 'due' END''')


def extraction_bucket():
    """The bucket of one text row f (aliases a, m, f and POLICY_JOINS)."""
    return ('''CASE WHEN f.state IS NULL THEN 'unknown'
        WHEN f.state IN ('ready','unsupported','failed','leased') THEN f.state
        WHEN ''' + WITHHELD_ROW + ''' THEN 'withheld'
        WHEN ''' + EVICTED_ROW + ''' THEN 'evicted'
        ELSE 'due' END''')


# What each bucket shows in the files block: leased, ready, failed and unsupported keep their counts; a row that
# was reported queued splits into due work, work waiting for a request and work no worker can do.
RECOGNITION_FILES = {'unknown': 'unknown', 'ready': 'ready', 'failed_final': 'failed', 'failed_retry': 'failed',
                     'unsupported': 'unsupported', 'leased': 'leased', 'withheld': 'available_on_request',
                     'evicted': 'available_on_request', 'skipped': 'available_on_request',
                     'recognition_off': 'unconfigured',
                     'no_text': 'not_recognizable', 'due': 'queued'}
EXTRACTION_FILES = {'unknown': 'unknown', 'ready': 'ready', 'unsupported': 'unsupported', 'failed': 'failed',
                    'leased': 'leased', 'withheld': 'available_on_request', 'evicted': 'available_on_request',
                    'due': 'queued'}
KEPT_STATES = ('leased', 'ready', 'failed', 'unsupported')


def histogram_key(state, bucket, mapping):
    """The files-histogram key of a row: a leased, ready, failed or unsupported row keeps its stored state (a policy
    exclusion never hides a failure there, Astra r1 finding 3); only queued and unconfigured rows split by bucket."""
    return state if state in KEPT_STATES else mapping[bucket]


RECOGNITION_DUE = ('due', 'failed_retry', 'leased')
EXTRACTION_DUE = ('due', 'leased')
ON_REQUEST_DOWNLOADS = ('group_chat_on_request', 'channel_on_request', 'outside_prefetch_window',
                        'outside_history_bounds', 'history_bound_unknown')


def empty_tally():
    return {'recognition': {}, 'extraction': {}, 'since': None, 'ready_last_24h': 0}


def _earliest(a, b):
    return b if a is None else a if b is None else min(a, b)


def add_row(tally, row):
    """Fold one grouped row of sync_status._files into the account's tally."""
    rec = tally['recognition'].setdefault(row['recognition_bucket'], {})
    rec[row['kind']] = rec.get(row['kind'], 0) + row['count']
    ext = tally['extraction']
    ext[row['extraction_bucket']] = ext.get(row['extraction_bucket'], 0) + row['count']
    tally['ready_last_24h'] += row['ready_last_24h']
    if row['recognition_bucket'] in RECOGNITION_DUE:
        tally['since'] = _earliest(tally['since'], row['recognition_waiting_since'])
    if row['extraction_bucket'] in EXTRACTION_DUE:
        tally['since'] = _earliest(tally['since'], row['extraction_waiting_since'])


def policy(platform, history_since, prefetch_days, kinds):
    """The account's collection policy as an agent reads it (status and capabilities)."""
    whatsapp = platform == 'whatsapp'
    return {'messages_since': history_since,
            'messages_source': 'phone_pairing_batch_and_live' if whatsapp else 'provider_history',
            'files_auto_days': prefetch_days,
            'files_auto_chats': 'personal_and_subscribed', 'files_other': 'on_request',
            'files_request_call': 'request_file_download', 'older_messages_call': 'request_chat_history',
            'recognition': sorted(kinds),
            'provider_media_retention_days': WHATSAPP_MEDIA_RETENTION_DAYS if whatsapp else None}


def _policy(row, platform, eligibility):
    published = ((row.get('operations') or {}).get('media_policy') or {}) if eligibility is not None else {}
    now = datetime.now(timezone.utc)
    kinds = [p['kind'] for p in row.get('processing_profiles') or []
             if p.get('observed_at') and now - p['observed_at'] <= timedelta(days=CONFIGURED_DAYS)]
    return policy(platform, published.get('history_since'),
                  eligibility['prefetch_days'] if eligibility is not None else None, kinds), published


def block(row, platform, files, tally, eligibility, health_codes, lease_live):
    """The account's coverage block (full detail). `eligibility` is sync_status._eligibility's entry or None."""
    rules, published = _policy(row, platform, eligibility)
    rec, ext = tally['recognition'], tally['extraction']

    def kinds(bucket):
        return rec.get(bucket, {})

    def total(*buckets):
        return sum(sum(kinds(b).values()) for b in buckets)
    to_recognize = {kind: sum(kinds(b).get(kind, 0) for b in RECOGNITION_DUE) for kind in RECOGNITION_KINDS}
    to_recognize['total'] = sum(to_recognize.values())
    downloads = files['download']
    queued = (eligibility or {}).get('queued', {})
    expired = (eligibility or {}).get('likely_expired', {})
    eligible_waiting = sum(queued.get('eligible', {}).values())
    reasons = {reason: sum(queued.get(reason, {}).values()) for reason in ON_REQUEST_DOWNLOADS}
    likely_expired = sum(expired.get(reason, 0) for reason in ON_REQUEST_DOWNLOADS) if platform == 'whatsapp' else 0
    not_downloaded = {reason: n for reason, n in reasons.items() if n}
    stored_waiting = total('withheld', 'evicted', 'skipped')
    due = {'to_download': eligible_waiting + downloads.get('leased', 0),
           'to_index': sum(ext.get(b, 0) for b in EXTRACTION_DUE),
           'to_recognize': to_recognize,
           'failed': {'download': downloads.get('failed', 0), 'index': ext.get('failed', 0),
                      'recognize': total('failed_final')}}
    if eligibility is None or not lease_live:
        state = 'unknown'
    else:
        state = 'behind' if due['to_download'] or due['to_index'] or to_recognize['total'] else 'done'
    since = None
    if state == 'behind':
        since = _earliest(tally['since'], (eligibility or {}).get('download_since'))
    on_request_total = sum(reasons.values()) - likely_expired + stored_waiting
    return {'state': state, 'since': since, 'as_of': published.get('published_at'), 'health': list(health_codes),
            'policy': rules, 'due': due,
            'done': {'stored': files['stored_files'], 'indexed': ext.get('ready', 0),
                     'transcribed': kinds('ready').get('audio', 0),
                     'described': kinds('ready').get('image', 0) + kinds('ready').get('document', 0),
                     'ready_last_24h': tally['ready_last_24h']},
            'available_on_request': {'total': on_request_total, 'not_downloaded': not_downloaded,
                                     'stored_not_recognized': stored_waiting,
                                     'likely_expired': likely_expired if platform == 'whatsapp' else None,
                                     'next_action': 'request_file_download'},
            'not_recognizable': {'no_text': total('no_text'), 'unsupported': total('unsupported'),
                                 'recognition_off': total('recognition_off')}}


def compact(full):
    """Compact detail: the verdict and its totals (ANYM-812 criterion 1); detail=full has the policy and every bucket.

    Kept small: the compact account overview has an 8 KiB budget over MCP (tests/test_grok_followup.py). None while
    the state is unknown (nothing to act on yet; full detail says why); a done account names only its on-request
    total."""
    if full['state'] == 'unknown':
        return None
    if full['state'] == 'done':
        return {'state': 'done', 'available_on_request': full['available_on_request']['total']}
    due = full['due']
    return {'state': full['state'], 'since': full['since'],
            'due': {'to_download': due['to_download'], 'to_index': due['to_index'],
                    'to_recognize': due['to_recognize']['total']},
            'available_on_request': full['available_on_request']['total']}
