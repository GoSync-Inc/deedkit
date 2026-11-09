"""Read-only sync evidence, scoped by the caller's existing archive permissions."""
from datetime import datetime, timezone

from .envelope import stable_id
from .policy import scope_filter
from .status_details import describe,freshness
from .conflict_classes import grouped, report
from .enrichment_health import read as enrichment_health
from .operational import identity_safe, liveness, operations
from .media_policy import ELIGIBILITY_JOIN, published_window
from .media_inventory import LIKELY_EXPIRED


DOWNLOAD_STATES = ('queued', 'leased', 'ready', 'failed', 'superseded')
EXTRACTION_STATES = ('queued', 'leased', 'ready', 'unsupported', 'failed', 'unknown')
ENRICHMENT_STATES = ('unconfigured', 'queued', 'leased', 'ready', 'unsupported', 'failed', 'unknown')
# ANYM-812 keys, present only when non-zero, so a file block without such rows reads as before (ANYM-667 snapshots).
# download counts each current file occurrence once: ready equals stored_files;
# current_files sums every state except superseded (replaced older revisions).
DOWNLOAD_BASIS = 'current_file_occurrences'


def _latest(previous, value):
    return max(previous, value) if previous is not None and value is not None else previous or value


def _attempt_time(value):
    if type(value) not in (int, float):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _history(platform, catalog_ids, rows):
    """Provider exhaustion is evidence of a traversal, never proof of full history."""
    checkpoint_ids = {stable_id('chat', row['account'], row['provider_chat']) for row in rows}
    result = {'observed_chats': len(catalog_ids | checkpoint_ids),
              'checkpoint_chats': len(rows), 'unknown_chats': len(catalog_ids-checkpoint_ids),
              'full_history_verified': False, 'last_attempt_at': None, 'last_scan_at': None}
    if platform == 'telegram':
        window = {'chats': 0, 'complete': 0, 'in_progress': 0, 'since_min': None, 'since_max': None}
        unbounded = {'chats': 0, 'provider_exhausted': 0, 'in_progress': 0}
        result.update(configured_window=window, unbounded_history=unbounded, provider_history_exhausted=0)
        for row in rows:
            result['last_attempt_at'] = _latest(result['last_attempt_at'], row['last_attempt_at'])
            result['last_scan_at'] = _latest(result['last_scan_at'], row['last_scan_at'])
            result['provider_history_exhausted'] += bool(row['history_exhausted'])
            since = row['history_since']
            if since is not None:
                window['chats'] += 1
                window['complete' if row['window_complete'] or row['history_exhausted'] else 'in_progress'] += 1
                window['since_min'] = min(window['since_min'], since) if window['since_min'] else since
                window['since_max'] = _latest(window['since_max'], since)
            elif row['initialized']:
                unbounded['chats'] += 1
                unbounded['provider_exhausted' if row['history_exhausted'] else 'in_progress'] += 1
            else:
                # A live callback also creates collector_chats with a NULL floor;
                # that alone is not evidence of a configured full-history scan.
                result['unknown_chats'] += 1
    elif platform == 'whatsapp':
        result['checkpoint_chats'] = sum(isinstance(row['wa_progress'], dict) for row in rows)
        result.update(successful_scan_chats=0, not_scanned_chats=0,
                      provider_history_exhausted=0, catchup_pending_chats=0, gap_chats=0, gap_cleared_chats=0,
                      unverified_span_chats=0)
        spans, gap_ids, repair_counts, gap_spans = [], [], {}, {}
        for row in rows:
            progress = row['wa_progress']
            if not isinstance(progress, dict):
                result['unknown_chats'] += 1
                continue
            result['last_attempt_at'] = _latest(result['last_attempt_at'], _attempt_time(progress.get('last_attempt_at')))
            # last_scan_at has a DB default even before the first success. Only a
            # committed page records this boolean, including an empty exhausted page.
            if type(progress.get('history_exhausted')) is not bool:
                result['not_scanned_chats'] += 1
                continue
            result['successful_scan_chats'] += 1
            result['last_scan_at'] = _latest(result['last_scan_at'], row['wa_last_scan_at'])
            result['provider_history_exhausted'] += progress['history_exhausted']
            # ANYM-813: a catch-up whose pointer did not move in its last scan is not pending; once stalled
            # it is listed by its unverified span instead.
            result['catchup_pending_chats'] += bool(progress.get('catchup_before')) and not progress.get('catchup_stalled_scans')
            result['gap_chats'] += progress.get('gap') is True
            chat_id = stable_id('chat', row['account'], row['provider_chat'])
            if progress.get('gap') is True:
                gap_ids.append(chat_id)
            span = unverified_span(progress)
            if span:
                repair = gap_repair_state(progress)
                spans.append({'chat_id': chat_id, **span, 'repair': repair})
                repair_counts[repair['state']] = repair_counts.get(repair['state'], 0)+1
                if progress.get('gap') is True:
                    gap_spans[chat_id] = repair
            result['gap_cleared_chats'] += progress.get('gap') is not True and progress.get('gap_cleared_at') is not None
        result['unverified_span_chats'] = len(spans)
        # Longest first by the exact interval; `days` is rounded for display only.
        spans.sort(key=lambda span: (datetime.fromisoformat(span['from'])-datetime.fromisoformat(span['to']),
                                     span['chat_id']))
        result['unverified_spans'] = spans[:20]
        # A flagged gap has no recorded span: the archive cannot say how much is missing (ANYM-700).
        # ANYM-814: a flagged gap whose recorded span the phone verified keeps its flag; the entry names the
        # evidence so the owner can decide on --clear-gap (the only clear).
        result['gap_chat_ids'] = [{'chat_id': chat_id, 'span': 'recorded' if chat_id in gap_spans else 'unknown',
                                   **({'repair': gap_spans[chat_id]} if chat_id in gap_spans else {})}
                                  for chat_id in sorted(gap_ids)[:20]]
        result['gap_repair'] = {'enabled': False, 'paused_until': None,
                                **{state: repair_counts.get(state, 0) for state in GAP_REPAIR_STATES}}
    else:
        result['unknown_chats'] = result['observed_chats']
    return result


def unverified_span(progress):
    """The chat's recorded unverified span (ANYM-813) with its length in days, or None when absent or malformed."""
    span = progress.get('unverified_span') if isinstance(progress, dict) else None
    if not isinstance(span, dict):
        return None
    try:
        start, end = (datetime.fromisoformat(span[k]) for k in ('from', 'to'))
        days = round(max(0, (end-start).total_seconds())/86400, 1)
    except (KeyError, TypeError, ValueError):
        return None
    return {'from': span['from'], 'to': span['to'], 'days': days,
            'stalled_since': span.get('stalled_since') if isinstance(span.get('stalled_since'), str) else None}


GAP_REPAIR_STATES = ('waiting', 'outstanding', 'verified', 'phone_has_no_older', 'budget_exhausted',
                     'request_uncertain', 'incomplete_answer')


def gap_repair_state(progress):
    """ANYM-814: one span's phone repair state; `waiting` until the repair has asked, also with the flag off."""
    repair = progress.get('gap_repair') if isinstance(progress.get('gap_repair'), dict) else {}
    state = repair.get('state') if repair.get('state') in GAP_REPAIR_STATES else 'waiting'
    ids = [v for v in repair.get('request_ids') or [] if isinstance(v, str)][:10]
    value = {'state': state, 'request_ids': ids}
    if state == 'verified':
        value['verified_by'] = 'phone_history'
    if state in ('budget_exhausted', 'incomplete_answer') and isinstance(repair.get('remaining'), dict):
        value['remaining'] = {k: repair['remaining'].get(k) for k in ('from', 'to')}
    return value


def whatsapp_gap_repair(history, operations):
    """ANYM-814: the account's switch and pause from the collector's last repair step."""
    repair = (operations or {}).get('gap_repair') or {}
    if isinstance(history.get('gap_repair'), dict) and isinstance(repair, dict):
        history['gap_repair'].update(enabled=repair.get('enabled') is True,
            paused_until=repair.get('paused_until') if isinstance(repair.get('paused_until'), str) else None)


# ANYM-813: what a WhatsApp archive is built from. WhatsApp keeps no server history to list: the phone's
# batch after pairing (device props, whatsapp/whatsmeow/runtime.go), live capture since, explicit requests.
PAIRING_BATCH_DAYS, PAIRING_BATCH_PER_CHAT = 7, 50


def whatsapp_sources(operations):
    live = (operations or {}).get('live_capture') or {}
    since = live.get('native_live_since')
    return {'pairing_batch_days': PAIRING_BATCH_DAYS, 'pairing_batch_per_chat': PAIRING_BATCH_PER_CHAT,
            'live_since': since if isinstance(since, str) else None, 'older_messages_call': 'request_chat_history'}


def _live_only(c, account, rows):
    """Chats with archived messages that no reconcile ever attempted: only live capture reached them."""
    candidates = [stable_id('chat', account, row['provider_chat']) for row in rows
                  if not row['initialized'] and row['last_attempt_at'] is None]
    found = c.execute('''SELECT candidate.id FROM unnest(%s::text[]) AS candidate(id)
        WHERE EXISTS (SELECT 1 FROM messages m WHERE m.account=%s AND m.chat=candidate.id)
        ORDER BY candidate.id''', (candidates, account)).fetchall() if candidates else []
    return {'live_only_chats': len(found), 'live_only_chat_ids': [row['id'] for row in found[:20]]}


def _checkpoints(c, p, allowed_chats):
    # collector_chats predates archive chat IDs. Resolve only identifiers first,
    # then fetch checkpoint state exclusively for permitted catalog entries. No
    # private names, progress or account-wide health are read by this resolution.
    pairs = [(r['account'], r['provider_chat']) for r in c.execute(
        'SELECT account,provider_chat FROM collector_chats WHERE account=ANY(%s)', (p['accounts'],))
        if p['chat_ids'] is None or stable_id('chat', r['account'], r['provider_chat']) in allowed_chats]
    if not pairs:
        return {}
    accounts, chats = zip(*pairs)
    rows = c.execute('''SELECT cc.*,wa.progress AS wa_progress,wa.last_scan_at AS wa_last_scan_at
        FROM unnest(%s::text[],%s::text[]) AS permitted(account,provider_chat)
        JOIN collector_chats cc USING(account,provider_chat)
        LEFT JOIN whatsapp_checkpoints wa USING(account,provider_chat)''', (list(accounts), list(chats)))
    grouped = {}
    for row in rows:
        grouped.setdefault(row['account'], []).append(row)
    return grouped


def empty_files():
    return {'stored_files': 0, 'stored_bytes': 0, 'current_files': 0, 'download_basis': DOWNLOAD_BASIS,
            'extraction': dict.fromkeys(EXTRACTION_STATES, 0), 'enrichment': dict.fromkeys(ENRICHMENT_STATES, 0),
            'extraction_truncated': 0, 'enrichment_truncated': 0}


def _jobs(key, where, claims, by_chat):
    """ANYM-917: the one pass over media jobs without their active original (aliases j, m).

    A job with its original is counted by the file pass instead. Each row: the job's download state (a ready job
    without a registered original is unavailable), its error code while queued, leased or failed, how many are likely
    expired (ANYM-815) and, when `claims` is set, the automatic-download claim of a queued or leased job whose chat
    row exists (media_policy.ELIGIBILITY_JOIN: what _eligibility and the latency block's download wait read).
    Parameters: _jobs_args."""
    claim = "CASE WHEN j.state IN ('queued','leased') AND ch.id IS NOT NULL THEN el.claim END" if claims else 'NULL'
    joins = ('LEFT JOIN chats ch ON ch.id=m.chat AND ch.account=m.account ' + ELIGIBILITY_JOIN) if claims else ''
    if by_chat:
        # A chat-restricted scope is driven from its chats' messages, as before ANYM-917.
        prefix, source = '', '''media_jobs j JOIN messages m ON m.id=j.message AND m.account=j.account
            AND j.account=ANY(%s) AND NOT EXISTS(SELECT FROM attachments a WHERE a.id=j.id AND a.message=j.message
                AND a.active)'''
    else:
        # The jobs without their original first (prod 11-09: about 20k of 75k), then their messages. Joined the other
        # way round the plan joined every job to its message and claim before dropping the stored ones (650 ms, not
        # 355); a lookup by key per job took 470-560 ms against the hash join's one read of the scope's messages.
        prefix = '''WITH unstored AS MATERIALIZED (SELECT j.id,j.account,j.message,j.state,j.error_code,j.created_at,
            j.requested_until FROM media_jobs j WHERE j.account=ANY(%s)
                AND NOT EXISTS(SELECT FROM attachments a WHERE a.id=j.id AND a.message=j.message AND a.active))
        '''
        source = 'unstored j JOIN messages m ON m.id=j.message AND m.account=j.account'
    return (prefix + 'SELECT ' + key + ''' AS account,CASE WHEN j.state='ready' THEN 'unavailable' ELSE j.state END AS state,
            CASE WHEN j.state IN ('queued','leased','failed') THEN j.error_code END AS error_code,
            ''' + claim + ''' AS eligibility,count(*) AS count,
            count(*) FILTER (WHERE j.state='failed' AND j.error_code IS NOT NULL AND ''' + LIKELY_EXPIRED + ''') AS expired,
            count(*) FILTER (WHERE m.sent_at<clock_timestamp()-make_interval(days=>%s)) AS old,
            min(j.created_at) AS since
        FROM ''' + source + ' ' + joins + '''
        WHERE NOT m.deleted AND ''' + where + ' GROUP BY 1,2,3,4')


def _jobs_args(p, args):
    """_jobs' parameters in statement order: the unscoped pass names its accounts first (in its CTE)."""
    from .coverage import WHATSAPP_MEDIA_RETENTION_DAYS
    accounts = list(p['accounts'])
    return ([WHATSAPP_MEDIA_RETENTION_DAYS, accounts] if p['chat_ids'] is not None
            else [accounts, WHATSAPP_MEDIA_RETENTION_DAYS]) + args


def _files(c, p, *, group_by_chat=False, tallies=None, claims=None):
    """(downloads, files) of scope p, by account or by chat. `claims` (a dict; by account only) receives each queued
    or leased job group's download claim for _eligibility and the latency block (ANYM-917)."""
    tallies = {} if tallies is None else tallies
    where, args = scope_filter(p, alias='m')
    key = "m.chat" if group_by_chat else "m.account"
    if group_by_chat and claims is not None:
        raise ValueError('claims_by_account_only')
    # One row per file occurrence of a non-deleted message, in exactly one state
    # (the per-file download_status rule): a registered active original is ready
    # whatever its job says (an edit can re-queue an original that is still
    # stored), a jobless legacy original is ready and a ready job without a
    # registered original is unavailable. Superseded jobs are older revisions.
    # ANYM-917: the ready occurrences are counted by the file pass below (its rows are the active originals of
    # non-deleted messages, each with its job joined by id); this pass reads only the jobs without their original.
    downloads = {}
    errors={};error_states={}
    # ANYM-815: failed WhatsApp jobs queued when their message was older than the estimated media retention;
    # read-time wording only, the recorded codes and counts stay as they are.
    expired={}
    # Count current occurrences, not global digests. Stale extraction/enrichment
    # rows must never inflate ready counts or expose another chat's derived state.
    from .media_policy import recognition_withheld
    from . import coverage
    withheld = recognition_withheld(c)
    # ANYM-812 criterion 6 (production 2026-11-08): this statement's expressions push its cost over the JIT
    # thresholds, and JIT inlining and optimization then took 4-5 s of a 0.8 s pass. Off for this transaction only.
    c.execute("SELECT set_config('jit','off',true)")
    # ANYM-812: each row's bucket under the current policy (coverage.py); `files` keeps leased, ready, failed and
    # unsupported counts and splits what was reported queued into due work, work waiting for a request and work no
    # worker can do.
    indexed = c.execute('WITH '+coverage.policy_sets(withheld)+'\n    SELECT '+key+''' AS account,'''+coverage.KIND+''' AS kind,
            '''+coverage.extraction_bucket()+''' AS extraction_bucket,
            '''+coverage.recognition_bucket()+''' AS recognition_bucket,
            coalesce(f.state,'unknown') AS extraction_state,coalesce(e.state,'unknown') AS enrichment_state,
            count(*) AS count,sum(a.bytes) AS bytes,
            count(*) FILTER (WHERE f.state='ready' AND f.truncated) AS extraction_truncated,
            count(*) FILTER (WHERE e.state='ready' AND e.truncated) AS enrichment_truncated,
            count(*) FILTER (WHERE e.state='ready' AND e.updated_at>=clock_timestamp()-interval '1 day') AS ready_last_24h,
            min(e.updated_at) AS recognition_waiting_since,min(f.updated_at) AS extraction_waiting_since,
            count(*) FILTER (WHERE wj.id IS NULL OR wj.message=a.message AND wj.account=m.account) AS ready_occurrences
        FROM attachments a JOIN messages m ON m.id=a.message
        LEFT JOIN attachment_text f ON f.attachment=a.id AND f.source_digest=a.digest
            AND f.source_revision=m.revision AND f.source_filename=a.filename
            AND f.source_mime=a.mime AND f.source_bytes=a.bytes AND f.state<>'superseded'
        LEFT JOIN attachment_enrichment e ON e.attachment=a.id AND e.source_digest=a.digest
            AND e.source_revision=m.revision AND e.state<>'superseded' '''+coverage.POLICY_JOINS+'''
        WHERE a.active AND NOT m.deleted AND '''+where+'''
        GROUP BY 1,2,3,4,5,6''', args)
    files = {}
    for row in indexed:
        item = files.setdefault(row['account'], empty_files())
        item['stored_files'] += row['count']
        item['stored_bytes'] += int(row['bytes'])
        extraction = coverage.histogram_key(row['extraction_state'], row['extraction_bucket'], coverage.EXTRACTION_FILES)
        enrichment = coverage.histogram_key(row['enrichment_state'], row['recognition_bucket'], coverage.RECOGNITION_FILES)
        item['extraction'][extraction] = item['extraction'].get(extraction, 0) + row['count']
        item['enrichment'][enrichment] = item['enrichment'].get(enrichment, 0) + row['count']
        item['extraction_truncated'] += row['extraction_truncated']
        item['enrichment_truncated'] += row['enrichment_truncated']
        coverage.add_row(tallies.setdefault(row['account'], coverage.empty_tally()), row)
        # A jobless legacy original, or one whose job of the same message and account it is (ANYM-917).
        if row['ready_occurrences']:
            counts = downloads.setdefault(row['account'], {})
            counts['ready'] = counts.get('ready', 0) + row['ready_occurrences']
    jobs = _jobs(key, where, claims is not None, p['chat_ids'] is not None)
    for row in c.execute(jobs, _jobs_args(p, args)):
        account = row['account']
        counts = downloads.setdefault(account, {})
        counts[row['state']] = counts.get(row['state'], 0) + row['count']
        code = row['error_code']
        if code is not None:
            account_errors = errors.setdefault(account, {})
            account_errors[code] = account_errors.get(code, 0) + row['count']
            states = error_states.setdefault(account, {}).setdefault(code, {})
            states[row['state']] = states.get(row['state'], 0) + row['count']
        if row['expired']:
            account_expired = expired.setdefault(account, {})
            account_expired[code] = account_expired.get(code, 0) + row['expired']
        if claims is not None and row['eligibility'] is not None:
            claims.setdefault(account, []).append(row)
    # ANYM-816: operator recovery states of each permitted account, also one with no current media. Account-wide,
    # so never for a chat-restricted principal or a per-chat view; None when this database cannot read them.
    if not group_by_chat and p['chat_ids'] is None:
        from .media_recovery import counts as recovery_counts
        recovery=recovery_counts(c,list(p['accounts']))
        for account in p['accounts']:
            files.setdefault(account,empty_files())['download_recovery']=None if recovery is None else recovery[account]
    for account in downloads:
        item=files.setdefault(account,empty_files())
        item['current_files']=sum(n for state,n in downloads[account].items() if state!='superseded')
        item['download_errors']=errors.get(account,{})
        item['download_error_states']=error_states.get(account,{})
        if expired.get(account):item['download_likely_expired']=expired[account]
    return downloads, files


def _eligibility(c, accounts, claims):
    """Queued originals by automatic-download eligibility under the published window.

    None for an account without a current publication (basis unknown). A recorded
    error_code is history evidence: a job that left the window keeps its code but
    is not reported as waiting for storage (ANYM-284). `claims` are _files' job rows
    with a claim (ANYM-917: that pass already read every queued or leased job)."""
    result = {r['account']: {'prefetch_days': None if r['days'] is None else int(r['days']), 'queued': {}}
        for r in c.execute('SELECT a.id AS account,mp.days FROM accounts a '+published_window('a.id')
            +' WHERE a.id=ANY(%s) AND mp.published', (accounts,))}
    if not result:
        return {}
    # ANYM-812: also how many are older than WhatsApp's media retention estimate, and since when due work waits.
    for row in (row for account in result for row in claims.get(account, ())):
        item = result[row['account']]
        if row['state'] == 'queued':
            codes = item['queued'].setdefault(row['eligibility'], {})
            codes[row['error_code']] = row['count']
            expired = item.setdefault('likely_expired', {})
            expired[row['eligibility']] = expired.get(row['eligibility'], 0) + row['old']
        # A leased download is still due work waiting since its job was created (Astra r1 finding 4).
        if row['eligibility'] == 'eligible' and row['since'] is not None:
            item['download_since'] = min(item.get('download_since') or row['since'], row['since'])
    return result


def get_sync_status(c, p, claims=None):
    """The account summary. `claims` (a dict) receives, by account, the queued or leased job rows with their download
    claim for an unscoped principal (ANYM-917), so the latency block's download wait reads no second pass."""
    where, args = scope_filter(p, alias='c', chat_column='id')
    catalog = c.execute('SELECT c.account,c.id,a.platform FROM chats c JOIN accounts a ON a.id=c.account WHERE '+where, args).fetchall()
    catalogs = {}
    catalog_ids = {}
    for row in catalog:
        item = catalogs.setdefault(row['account'], {'account': row['account'], 'platform': row['platform'], 'catalog_chats': 0})
        item['catalog_chats'] += 1
        catalog_ids.setdefault(row['account'], set()).add(row['id'])
    where, args = scope_filter(p, alias='m')
    messages = {r['account']: r for r in c.execute('''SELECT m.account,count(*) AS messages,
        min(m.sent_at) AS oldest_message_at,max(m.sent_at) AS newest_message_at
        FROM messages m WHERE '''+where+' GROUP BY m.account', args)}
    scoped = p['chat_ids'] is not None
    if scoped:
        # No collector_health/source_status read for a chat-restricted principal.
        sources = [catalogs[k] for k in sorted(catalogs)]
    else:
        sources = c.execute('''SELECT a.id AS account,a.platform,h.status,h.detail_code,h.cooldown_until,
            h.updated_at,h.discovery_started_at,h.discovery_completed_at,h.discovery_count,h.discovery_limited,'''+identity_safe('h.operations')+''' AS operations,
            l.expires_at AS collector_lease_until,
            s.last_ingest_at,s.detail_code AS archive_detail_code
            FROM accounts a LEFT JOIN collector_health h ON h.account=a.id
            LEFT JOIN source_status s ON s.account=a.id
            LEFT JOIN collector_leases l ON l.account=a.id
            WHERE a.id=ANY(%s) ORDER BY a.id''', (p['accounts'],)).fetchall()
        for row in sources:
            row['catalog_chats'] = catalogs.get(row['account'], {}).get('catalog_chats', 0)
    if sources:
        checkpoints = _checkpoints(c, p, {row['id'] for row in catalog})
        tallies = {}
        claims = None if scoped else ({} if claims is None else claims)
        downloads, files = _files(c, p, tallies=tallies, claims=claims)
    conflicts = {}
    reevaluations = {}
    eligibility = {}
    if sources and not scoped:
        eligibility = _eligibility(c, [row['account'] for row in sources], claims)
        # The archive detail code is sticky once any conflict was observed; the
        # alert follows conflicts which are still unresolved.
        conflicts = grouped(c, 'account', [row['account'] for row in sources])
        from .conflict_reevaluation import status as reevaluation_status
        reevaluations = reevaluation_status(c, [row['account'] for row in sources])
    outbound = {}
    if sources and not scoped:
        from .outgoing import outbound_status
        outbound = outbound_status(c, [row['account'] for row in sources])
    deletion_status = {}
    if sources and not scoped:
        from .message_purge import deletions
        deletion_status = deletions(c, [row['account'] for row in sources])
    health = None if scoped else enrichment_health(p['accounts'])
    for row in sources:
        account = row['account']
        data = messages.get(account, {})
        row.update(messages=data.get('messages', 0), oldest_message_at=data.get('oldest_message_at'),
                   newest_message_at=data.get('newest_message_at'))
        row['history'] = _history(row['platform'], catalog_ids.get(account, set()), checkpoints.get(account, []))
        if row['platform'] == 'telegram':
            row['history'].update(_live_only(c, account, checkpoints.get(account, [])))
        if not scoped:
            open_by_class,row['active_conflicts'],row['historical_conflicts']=conflicts.get(account,({},0,0))
            row['conflicts']=report(open_by_class)
            # Per job: version, done, rows resolved, last error (ANYM-621); full detail only.
            row['conflict_reevaluations']=reevaluations.get(account,[])
            if row.get('archive_detail_code')=='version_conflict' and not row['active_conflicts']:
                row['archive_detail_code']='coverage_not_reconciled'
            row['deletions']=deletion_status.get(account) or {}
            if row['platform'] == 'whatsapp':
                row['history']['sources'] = whatsapp_sources(row.get('operations'))
                whatsapp_gap_repair(row['history'], row.get('operations'))
            # ANYM-809: the native journal's repair count, per account and repair version; full detail only.
            row['native_repair']=dict(row.get('operations') or {}).get('provider_repair')
            if row.get('operations'):row['operations']={k:v for k,v in row['operations'].items() if k!='provider_repair'}
            # ANYM-700: the stored code waits for the next collector pass; the flag is the truth.
            if not row['history'].get('gap_chats'):
                for key in ('detail_code','archive_detail_code'):
                    if row.get(key)=='whatsapp_catchup_gap':row[key]='whatsapp_history_partial'
            row['detail']=describe(row.get('detail_code'))
            row['archive_detail']=describe(row.get('archive_detail_code'))
            if row.get('archive_detail_code')=='version_conflict':
                row['archive_detail']['next_action']=row['conflicts']['next_step']
            row['alerts']=[]
            if row['history'].get('gap_chats'):row['alerts'].append('provider_history_gaps')
            # Known classes need nothing; active_conflicts keeps the total for older readers (ANYM-557).
            if row['conflicts']['attention']:row['alerts'].append('archive_version_conflicts')
        row['files'] = files.get(account) or empty_files()
        row['files']['download'] = {**dict.fromkeys(DOWNLOAD_STATES, 0), **downloads.get(account, {})}
        if not scoped:
            row['operations']=operations(row,row['history'],row['files'],eligibility=eligibility.get(account))
            row['liveness']=liveness(row['operations'])
            row['freshness']=freshness(row['history'].get('last_scan_at'),operational=row['operations'])
            row['outbound']=outbound[account]
            from .outgoing import outbound_alerts
            row['alerts'].extend(outbound_alerts(row['outbound']))
            if row['freshness']['scan_state']=='scan_stale':row['alerts'].append('successful_scan_older_than_5_minutes')
            if row['freshness']['state']=='capture_backlogged':row['alerts'].append('capture_backlog')
            if row['freshness']['state']=='collector_not_running':row['alerts'].append('collector_not_running')
            row['processing_profiles']=c.execute('''SELECT kind,provider,model,enabled AS configured,
                observed_at,expires_at,expires_at>clock_timestamp() AND enabled AS worker_current,
                pause_reason,paused_until
                FROM processing_profiles WHERE account=%s ORDER BY kind''',(account,)).fetchall()
            # ANYM-812: one verdict under the current policy; account-wide, so never for a chat-scoped key.
            from . import coverage
            lease = row.get('collector_lease_until')
            lease_live = lease is not None and lease > datetime.now(timezone.utc)
            codes = ((health or {}).get('accounts') or {}).get(account, {}).get('codes', [])
            row['coverage'] = coverage.block(row, row['platform'], row['files'], tallies.get(account) or coverage.empty_tally(),
                eligibility.get(account), codes, lease_live)
    return {'scope': 'permitted_chats' if scoped else 'permitted_accounts',
            'coverage': 'partial', 'sources': sources,
            # Account-wide queue facts: never for a chat-restricted principal (ANYM-785).
            'enrichment_health': health}
