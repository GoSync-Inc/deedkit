"""Per-account coverage under the current collection policy (ANYM-812).

One verdict per account: is everything the policy gives the collector, the indexer and the enrichment worker done?
Files the policy leaves to an agent's request are counted apart as available on request, never as waiting work.
The SQL fragments below are the one definition of each bucket; sync status and enrichment_health both use them.
The workers' claims keep their own order, caps and leases (tests/test_sync_coverage.py holds them to the same rows).
"""
from .file_lifecycle import PROCESSABLE_ORIGINAL

# WhatsApp publishes no media retention; integrators report about 30 days (an estimate, not a provider guarantee).
WHATSAPP_MEDIA_RETENTION_DAYS = 30
RETRY_CEILING = 3  # EnrichmentWorker.claim: a failed row is retried while attempts < 3 (enrichment.py)
RECOGNITION_KINDS = ('audio', 'image', 'document')

# The enrichment kind of an attachment (alias a), as EnrichmentWorker.claim and sync status decide it. Escaped for
# statements that carry parameters.
KIND = ("CASE WHEN a.mime LIKE 'audio/%%' OR a.mime='application/ogg' THEN 'audio' "
        "WHEN a.mime LIKE 'image/%%' THEN 'image' ELSE 'document' END")
# An enabled profile for the attachment's kind on the message's account (alias m).
PROFILE_ON = ('EXISTS(SELECT FROM processing_profiles cpp WHERE cpp.account=m.account AND cpp.enabled AND cpp.kind='
              + KIND + ')')
# Extracted text a document description can start from (alias f: the attachment's current text row).
DOCUMENT_TEXT = "(f.state='ready' AND f.source_digest=a.digest AND f.source_revision=m.revision AND f.text_content<>'')"
EVICTED = 'NOT ' + PROCESSABLE_ORIGINAL


# enrichment.FINAL_LIMIT_CODES: a failure the same profile never retries (a test keeps the two equal, so the API
# does not import the worker module).
FINAL_LIMIT_CODES = ('original_size_limit', 'audio_duration_limit', 'audio_transcode_size_limit')


def recognition_bucket(withheld):
    """The bucket of one enrichment row e (aliases a, m, e, f), first match wins (card ANYM-812, precedence).

    `withheld` is media_policy.recognition_withheld(c): the on-request rule, or 'false' for a login that cannot
    read its columns."""
    final = ','.join("'" + code + "'" for code in FINAL_LIMIT_CODES)
    return ('''CASE WHEN e.state IS NULL THEN 'unknown'
        WHEN e.state='ready' THEN 'ready'
        WHEN e.state='failed' AND (e.attempts>=''' + str(RETRY_CEILING) + " OR coalesce(e.error_code,'') IN ("
            + final + ''')) THEN 'failed_final'
        WHEN e.state='unsupported' THEN 'unsupported'
        WHEN e.state='leased' THEN 'leased'
        WHEN ''' + withheld + ''' THEN 'withheld'
        WHEN ''' + EVICTED + ''' THEN 'evicted'
        WHEN NOT ''' + PROFILE_ON + ''' THEN 'recognition_off'
        WHEN ''' + KIND + "='document' AND NOT coalesce(" + DOCUMENT_TEXT + ''',false) THEN 'no_text'
        WHEN e.state='failed' THEN 'failed_retry'
        ELSE 'due' END''')


def extraction_bucket(withheld):
    """The bucket of one text row f (aliases a, m, f)."""
    return ('''CASE WHEN f.state IS NULL THEN 'unknown'
        WHEN f.state IN ('ready','unsupported','failed','leased') THEN f.state
        WHEN ''' + withheld + ''' THEN 'withheld'
        WHEN ''' + EVICTED + ''' THEN 'evicted'
        ELSE 'due' END''')


# What each bucket shows in the files block: leased, ready, failed and unsupported keep their counts; a row that
# was reported queued splits into due work, work waiting for a request and work no worker can do.
RECOGNITION_FILES = {'unknown': 'unknown', 'ready': 'ready', 'failed_final': 'failed', 'failed_retry': 'failed',
                     'unsupported': 'unsupported', 'leased': 'leased', 'withheld': 'available_on_request',
                     'evicted': 'available_on_request', 'recognition_off': 'unconfigured',
                     'no_text': 'not_recognizable', 'due': 'queued'}
EXTRACTION_FILES = {'unknown': 'unknown', 'ready': 'ready', 'unsupported': 'unsupported', 'failed': 'failed',
                    'leased': 'leased', 'withheld': 'available_on_request', 'evicted': 'available_on_request',
                    'due': 'queued'}
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
    kinds = [p['kind'] for p in row.get('processing_profiles') or [] if p.get('configured')]
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
    stored_waiting = total('withheld', 'evicted')
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
    """Compact detail: the verdict, the policy, due counts and one on-request total (ANYM-812 criterion 1)."""
    result = {k: v for k, v in full.items() if k != 'available_on_request'}
    result['available_on_request'] = {'total': full['available_on_request']['total'],
                                      'next_action': full['available_on_request']['next_action']}
    return result
