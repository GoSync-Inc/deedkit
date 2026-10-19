"""Read-only sync evidence, scoped by the caller's existing archive permissions."""
from datetime import datetime, timezone

from .envelope import stable_id
from .policy import scope_filter
from .status_details import describe,freshness
from .operational import liveness, operations
from .media_policy import ELIGIBILITY, ELIGIBILITY_JOIN, published_window


DOWNLOAD_STATES = ('queued', 'leased', 'ready', 'failed', 'superseded')
EXTRACTION_STATES = ('queued', 'leased', 'ready', 'unsupported', 'failed', 'unknown')
ENRICHMENT_STATES = ('unconfigured', 'queued', 'leased', 'ready', 'unsupported', 'failed', 'unknown')
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
                      provider_history_exhausted=0, catchup_pending_chats=0, gap_chats=0)
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
            result['catchup_pending_chats'] += bool(progress.get('catchup_before'))
            result['gap_chats'] += progress.get('gap') is True
    else:
        result['unknown_chats'] = result['observed_chats']
    return result


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


def _files(c, p, *, group_by_chat=False):
    where, args = scope_filter(p, alias='m')
    key = "m.chat" if group_by_chat else "m.account"
    # One row per file occurrence of a non-deleted message, in exactly one state
    # (the per-file download_status rule): a registered active original is ready
    # whatever its job says (an edit can re-queue an original that is still
    # stored), a jobless legacy original is ready and a ready job without a
    # registered original is unavailable. Superseded jobs are older revisions.
    downloads = {}
    errors={};error_states={}
    for row in c.execute('SELECT account,state,error_code,count(*) AS count FROM (SELECT '+key+''' AS account,
            CASE WHEN a.id IS NOT NULL THEN 'ready' WHEN j.state='ready' THEN 'unavailable' ELSE j.state END AS state,
            CASE WHEN a.id IS NULL AND j.state IN ('queued','leased','failed') THEN j.error_code END AS error_code
        FROM media_jobs j JOIN messages m ON m.id=j.message AND m.account=j.account
        LEFT JOIN attachments a ON a.id=j.id AND a.message=j.message AND a.active
        WHERE NOT m.deleted AND '''+where+' UNION ALL SELECT '+key+''','ready',NULL
        FROM attachments a JOIN messages m ON m.id=a.message WHERE a.active AND NOT m.deleted
        AND NOT EXISTS(SELECT FROM media_jobs j WHERE j.id=a.id) AND '''+where+''') occurrence
        GROUP BY account,state,error_code''', args+args):
        counts=downloads.setdefault(row['account'], {})
        counts[row['state']]=counts.get(row['state'], 0)+row['count']
        if row['error_code'] is not None:
            account_errors=errors.setdefault(row['account'],{})
            account_errors[row['error_code']]=account_errors.get(row['error_code'],0)+row['count']
            error_states.setdefault(row['account'],{}).setdefault(row['error_code'],{})[row['state']]=row['count']
    # Count current occurrences, not global digests. Stale extraction/enrichment
    # rows must never inflate ready counts or expose another chat's derived state.
    indexed = c.execute('SELECT '+key+''' AS account,
            coalesce(f.state,'unknown') AS extraction_state,
            CASE WHEN e.state='unconfigured' AND EXISTS(SELECT FROM processing_profiles pp
                WHERE pp.account=m.account AND pp.enabled AND pp.kind=CASE
                    WHEN a.mime LIKE 'audio/%%' OR a.mime='application/ogg' THEN 'audio'
                    WHEN a.mime LIKE 'image/%%' THEN 'image' ELSE 'document' END)
                THEN 'queued' ELSE coalesce(e.state,'unknown') END AS enrichment_state,
            count(*) AS count,sum(a.bytes) AS bytes,
            count(*) FILTER (WHERE f.state='ready' AND f.truncated) AS extraction_truncated,
            count(*) FILTER (WHERE e.state='ready' AND e.truncated) AS enrichment_truncated
        FROM attachments a JOIN messages m ON m.id=a.message
        LEFT JOIN attachment_text f ON f.attachment=a.id AND f.source_digest=a.digest
            AND f.source_revision=m.revision AND f.source_filename=a.filename
            AND f.source_mime=a.mime AND f.source_bytes=a.bytes AND f.state<>'superseded'
        LEFT JOIN attachment_enrichment e ON e.attachment=a.id AND e.source_digest=a.digest
            AND e.source_revision=m.revision AND e.state<>'superseded'
        WHERE a.active AND NOT m.deleted AND '''+where+'''
        GROUP BY '''+key+',f.state,enrichment_state', args)
    files = {}
    for row in indexed:
        item = files.setdefault(row['account'], empty_files())
        item['stored_files'] += row['count']
        item['stored_bytes'] += int(row['bytes'])
        item['extraction'][row['extraction_state']] += row['count']
        item['enrichment'][row['enrichment_state']] += row['count']
        item['extraction_truncated'] += row['extraction_truncated']
        item['enrichment_truncated'] += row['enrichment_truncated']
    for account in downloads:
        item=files.setdefault(account,empty_files())
        item['current_files']=sum(n for state,n in downloads[account].items() if state!='superseded')
        item['download_errors']=errors.get(account,{})
        item['download_error_states']=error_states.get(account,{})
    return downloads, files


def _eligibility(c, accounts):
    """Queued originals by automatic-download eligibility under the published window.

    None for an account without a current publication (basis unknown). A recorded
    error_code is history evidence: a job that left the window keeps its code but
    is not reported as waiting for storage (ANYM-284)."""
    result = {r['account']: {'prefetch_days': None if r['days'] is None else int(r['days']), 'queued': {}}
        for r in c.execute('SELECT a.id AS account,mp.days FROM accounts a '+published_window('a.id')
            +' WHERE a.id=ANY(%s) AND mp.published', (accounts,))}
    if not result:
        return {}
    for row in c.execute('''SELECT m.account,CASE WHEN NOT p.enabled OR ch.provider_chat=ANY(p.exclude_chats)
            OR NOT (p.discover_chats OR ch.provider_chat=ANY(p.include_chats)) THEN 'collection_excluded'
            ELSE '''+ELIGIBILITY+''' END AS eligibility,j.error_code,count(*) AS count
        FROM media_jobs j JOIN messages m ON m.id=j.message AND m.account=j.account
        JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
        JOIN account_policies p ON p.account=m.account '''+published_window('m.account')+' '+ELIGIBILITY_JOIN+'''
        LEFT JOIN attachments a ON a.id=j.id AND a.message=j.message AND a.active
        WHERE j.state='queued' AND a.id IS NULL AND NOT m.deleted AND m.account=ANY(%s)
        GROUP BY 1,2,3''', (list(result),)):
        codes = result[row['account']]['queued'].setdefault(row['eligibility'], {})
        codes[row['error_code']] = row['count']
    return result


def get_sync_status(c, p):
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
            h.updated_at,h.discovery_started_at,h.discovery_completed_at,h.discovery_count,h.discovery_limited,h.operations,
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
        downloads, files = _files(c, p)
    conflicts = {}
    eligibility = {}
    if sources and not scoped:
        eligibility = _eligibility(c, [row['account'] for row in sources])
        # The archive detail code is sticky once any conflict was observed; the
        # alert follows conflicts which are still unresolved.
        conflicts = {r['account']: r for r in c.execute('''SELECT m.account,
            count(*) FILTER(WHERE mc.resolved_at IS NULL) AS active_conflicts,
            count(*) FILTER(WHERE mc.resolved_at IS NOT NULL) AS historical_conflicts
            FROM message_conflicts mc JOIN messages m ON m.id=mc.message
            WHERE m.account=ANY(%s) GROUP BY m.account''', ([row['account'] for row in sources],))}
    for row in sources:
        account = row['account']
        data = messages.get(account, {})
        row.update(messages=data.get('messages', 0), oldest_message_at=data.get('oldest_message_at'),
                   newest_message_at=data.get('newest_message_at'))
        row['history'] = _history(row['platform'], catalog_ids.get(account, set()), checkpoints.get(account, []))
        if not scoped:
            counts=conflicts.get(account,{})
            row['active_conflicts']=counts.get('active_conflicts',0)
            row['historical_conflicts']=counts.get('historical_conflicts',0)
            if row.get('archive_detail_code')=='version_conflict' and not row['active_conflicts']:
                row['archive_detail_code']='coverage_not_reconciled'
            row['detail']=describe(row.get('detail_code'))
            row['archive_detail']=describe(row.get('archive_detail_code'))
            row['alerts']=[]
            if row['history'].get('gap_chats'):row['alerts'].append('provider_history_gaps')
            if row['active_conflicts']:row['alerts'].append('archive_version_conflicts')
        row['files'] = files.get(account) or empty_files()
        row['files']['download'] = {**dict.fromkeys(DOWNLOAD_STATES, 0), **downloads.get(account, {})}
        if not scoped:
            row['operations']=operations(row,row['history'],row['files'],eligibility=eligibility.get(account))
            row['liveness']=liveness(row['operations'])
            row['freshness']=freshness(row['history'].get('last_scan_at'),operational=row['operations'])
            if row['freshness']['scan_state']=='scan_stale':row['alerts'].append('successful_scan_older_than_5_minutes')
            if row['freshness']['state']=='capture_backlogged':row['alerts'].append('capture_backlog')
            if row['freshness']['state']=='collector_not_running':row['alerts'].append('collector_not_running')
            row['processing_profiles']=c.execute('''SELECT kind,provider,model,enabled AS configured,
                observed_at,expires_at,expires_at>clock_timestamp() AND enabled AS worker_current,
                pause_reason,paused_until
                FROM processing_profiles WHERE account=%s ORDER BY kind''',(account,)).fetchall()
    return {'scope': 'permitted_chats' if scoped else 'permitted_accounts',
            'coverage': 'partial', 'sources': sources}
