"""Coverage of the requested chats; traversal evidence is not provider completeness."""
from .policy import scope_filter, targets
from .search_query import valid_text


def backfill(c):
    """Called once by migration17, before new collectors start. No source rewrite."""
    from .envelope import stable_id
    pairs = c.execute('SELECT account,provider_chat FROM collector_chats').fetchall()
    if pairs:
        with c.cursor() as cur:
            cur.executemany('UPDATE chats SET provider_chat=%s WHERE id=%s AND account=%s',
                [(r['provider_chat'],stable_id('chat',r['account'],r['provider_chat']),r['account']) for r in pairs])
    c.execute('''UPDATE chats ch SET provider_chat=x.provider_chat FROM (
        SELECT DISTINCT ON (m.chat) m.chat,r.snapshot->>'provider_chat' AS provider_chat
        FROM messages m JOIN message_revisions r ON r.message=m.id AND r.revision=m.revision
        WHERE r.snapshot->>'provider_chat' IS NOT NULL ORDER BY m.chat,m.id) x
        WHERE ch.id=x.chat AND ch.provider_chat IS NULL''')
    c.execute('''INSERT INTO message_conflicts(receipt_digest,message,observed_revision,
        candidate_updated_at,observed_at,resolved_at,resolved_revision)
        SELECT ir.digest,m.id,coalesce(rev.revision,m.revision),
            (ir.envelope->>'updated_at')::timestamptz,ir.received_at,
            CASE WHEN m.provider_updated_at>(ir.envelope->>'updated_at')::timestamptz THEN now() END,
            CASE WHEN m.provider_updated_at>(ir.envelope->>'updated_at')::timestamptz THEN m.revision END
        FROM ingest_receipts ir JOIN chats ch ON ch.account=ir.account
            AND ch.provider_chat=ir.envelope->>'chat'
        JOIN messages m ON m.account=ch.account AND m.chat=ch.id
            AND m.provider_id=ir.envelope->>'provider_id'
        LEFT JOIN LATERAL (SELECT r.revision FROM message_revisions r
            WHERE r.message=m.id AND r.observed_at<=ir.received_at
            ORDER BY r.revision DESC LIMIT 1) rev ON true
        WHERE ir.outcome='conflict' ''')


def resolve_chat(c,p,account,provider_chat_id):
    """Provider chat IDs are exact marked IDs, not usernames or positive channel aliases."""
    valid_text(account,64,empty=False);valid_text(provider_chat_id,160,empty=False)
    where,args=scope_filter(p,'c',chat_column='id')
    row=c.execute('SELECT c.id,c.account,c.title,c.provider_chat,a.platform FROM chats c '
        'JOIN accounts a ON a.id=c.account WHERE c.account=%s AND c.provider_chat=%s AND '+where,
        [account,provider_chat_id,*args]).fetchone()
    if not row:
        from .whatsapp_identity import resolve_alias
        row=resolve_alias(c,p,account,provider_chat_id)
        if row:row={**row,'reference_resolution':{'method':'verified_whatsapp_alias',
            'requested_provider_chat_id':provider_chat_id,'original_provider_chat_id':row['provider_chat']}}
    if not row:
        from .outgoing import phone_not_found
        raise phone_not_found(provider_chat_id,c,p,account)
    # ANYM-361: the same self-chat label as list_chats; the stored title is not returned for it.
    from .chat_queries import SELF_CHAT_TITLE,self_chat_sql
    if c.execute("SELECT c.chat_type='private' AND "+self_chat_sql()+' AS self FROM chats c WHERE c.id=%s',
            (row['id'],)).fetchone()['self']:row={**row,'title':SELF_CHAT_TITLE,'title_basis':'self_chat'}
    else:
        from .chat_titles import labels
        label=labels(c,p,[(row['account'],row['id'])]).get((row['account'],row['id']))
        if label and label[1]:row={**row,'title':label[0],'title_basis':label[1]}
    targets(p,[row['id']]);return row


def rows(c,p,chat_ids=None,accounts=None,chat_types=None):
    where,args=scope_filter(p,'c',chat_column='id')
    if chat_ids is not None:where+=' AND c.id=ANY(%s)';args.append(chat_ids)
    if accounts is not None:where+=' AND c.account=ANY(%s)';args.append(accounts)
    if chat_types is not None:where+=" AND coalesce(c.chat_type,'unknown')=ANY(%s)";args.append(chat_types)
    return c.execute('''SELECT c.id AS chat_id,c.account,c.provider_chat,a.platform,a.synthetic,
        cc.initialized,cc.history_since,cc.window_complete,cc.history_exhausted,
        cc.high_water,cc.history_before,cc.last_scan_at,cc.last_attempt_at,cc.last_head_scan_at,
        cc.last_error_code,cc.last_error_at,
        wa.progress AS wa_progress,wa.last_scan_at AS wa_scan_at
        FROM chats c JOIN accounts a ON a.id=c.account
        LEFT JOIN collector_chats cc ON cc.account=c.account AND cc.provider_chat=c.provider_chat
        LEFT JOIN whatsapp_checkpoints wa ON wa.account=c.account AND wa.provider_chat=c.provider_chat
        WHERE '''+where+' ORDER BY c.id',args).fetchall()


def history(row,lower=None,upper=None):
    scanned=row['last_head_scan_at']
    gaps=[];floor=row['history_since'];complete=False;attempt=row['last_attempt_at']
    if row['platform']=='telegram':
        complete=bool(row['initialized'] and (row['window_complete'] or row['history_exhausted']))
        state='window_traversed' if complete else ('loading' if row['initialized'] else 'not_scanned')
    else:
        from .sync_status import _attempt_time
        progress=row['wa_progress'] or {}
        attempt=_attempt_time(progress.get('last_attempt_at'))
        if progress.get('gap') is True:gaps.append('catchup_gap')
        if progress.get('catchup_before'):gaps.append('catchup_pending')
        complete=progress.get('history_exhausted') is True and not gaps
        state='provider_exhausted' if complete else ('loading' if type(progress.get('history_exhausted')) is bool else 'not_scanned')
        if gaps:state='gaps'
    error=row['last_error_code']
    if error:state='provider_unavailable' if error=='provider_unavailable' else 'rate_limited'
    range_inside=complete and scanned is not None and not gaps and not row['synthetic'] and not error
    if floor is not None and (lower is None or lower<floor):range_inside=False
    # Only an explicit finite interval can be marked traversed. An open upper
    # bound includes changes after the last scan; an open lower bound does not
    # define the requested history floor even after provider exhaustion.
    if lower is None or upper is None or scanned is None or upper>scanned:range_inside=False
    return {'state':state,'synthetic':row['synthetic'],'configured_since':floor,'last_successful_page_at':row['wa_scan_at']
        if row['platform']=='whatsapp' and type((row['wa_progress'] or {}).get('history_exhausted')) is bool
        else row['last_scan_at'] if row['platform']=='telegram' else None,
        'last_head_scan_at':scanned,'last_attempt_at':attempt,
        'archived_through_provider_message_id':str(row['high_water']) if row['platform']=='telegram' and row['high_water'] else None,
        'older_page_before_provider_message_id':str(row['history_before']) if row['platform']=='telegram' and row['history_before'] else None,
        'gaps':gaps,'last_error_code':error,'last_error_at':row['last_error_at'],
        'history_traversal_complete':complete,
        'requested_range_traversed':bool(range_inside),'full_history_verified':False,
        'older_edits_and_deletions_verified':False}


def chat_statuses(c,p,chat_ids,lower=None,upper=None):
    from .sync_status import _files,empty_files
    records=rows(c,p,chat_ids);result={}
    ids=[r['chat_id'] for r in records]
    if not ids:return result
    counts={r['chat']:r for r in c.execute('''SELECT chat,count(*) AS messages,
        min(sent_at) AS oldest_message_at,max(sent_at) AS newest_message_at
        FROM messages WHERE chat=ANY(%s) AND NOT deleted GROUP BY chat''',(ids,))}
    conflicts={r['chat']:r for r in c.execute('''SELECT m.chat,
        count(*) FILTER(WHERE mc.resolved_at IS NULL) AS active,
        count(*) FILTER(WHERE mc.resolved_at IS NOT NULL) AS historical
        FROM message_conflicts mc JOIN messages m ON m.id=mc.message
        WHERE m.chat=ANY(%s) GROUP BY m.chat''',(ids,))}
    downloads,files=_files(c,{**p,'chat_ids':ids},group_by_chat=True)
    for row in records:
        chat=row['chat_id'];h=history(row,lower,upper)
        count=counts.get(chat,{'messages':0,'oldest_message_at':None,'newest_message_at':None})
        conflict=conflicts.get(chat,{'active':0,'historical':0})
        file_data=files.get(chat) or {**empty_files(),'extraction':{},'enrichment':{}}
        file_data['download']=downloads.get(chat,{})
        result[chat]={'chat_id':chat,'account':row['account'],'provider_chat_id':row['provider_chat'],
            'history':h,'messages':{k:v for k,v in count.items() if k!='chat'},'files':file_data,
            'conflicts':{k:v for k,v in conflict.items() if k!='chat'},
            'next_action':'check_provider_access' if h['last_error_code']=='provider_unavailable' else
                'wait_for_provider_cooldown' if h['last_error_code']=='rate_limited' else
                'get_context' if conflict['active'] else
                'wait_for_history' if not h['history_traversal_complete'] else 'read_archived_history'}
    return result


def query_evidence(c,p,normalized,items,*,paged=False,include=True):
    """Small summary plus at most20 explicit chat statuses. ACLs precede every count."""
    chat=normalized.get('chat');chats=normalized.get('chats')
    ids=[chat] if chat else chats
    if chat and chats is not None:ids=[chat] if chat in chats else []
    account=normalized.get('account');accounts=normalized.get('accounts')
    accts=[account] if account else accounts
    if account and accounts is not None:accts=[account] if account in accounts else []
    lower=normalized.get('from_time');upper=normalized.get('to_time')
    # Named chats keep their own evidence; otherwise totals count only chats of the searched types.
    types=normalized.get('chat_types')
    records=rows(c,p,ids,accts,types if ids is None else None)
    histories=[history(r,lower,upper) for r in records]
    missing=ids is not None and bool(set(ids)-{r['chat_id'] for r in records})
    traversed=(ids is not None or p['chat_ids'] is not None) and bool(histories) and all(h['requested_range_traversed'] for h in histories) and not missing
    conflicts=0
    if records:
        conflicts=c.execute('''SELECT count(*) AS n FROM message_conflicts mc JOIN messages m ON m.id=mc.message
            WHERE m.chat=ANY(%s) AND mc.resolved_at IS NULL
            AND (%s::timestamptz IS NULL OR m.sent_at>=%s)
            AND (%s::timestamptz IS NULL OR m.sent_at<%s)''',
            ([r['chat_id'] for r in records],lower,lower,upper,upper)).fetchone()['n']
    reason=None
    if not items:
        if paged:reason='end_of_archived_page'
        elif ids==[] or accts==[] or types==[]:reason='empty_filter'
        elif missing or not records:reason='not_found_or_not_permitted'
        elif any(h['state']=='provider_unavailable' for h in histories):reason='provider_unavailable'
        elif any(h['state']=='rate_limited' for h in histories):reason='provider_rate_limited'
        elif any(h['state']=='not_scanned' for h in histories):reason='history_not_scanned'
        elif any(h['state']=='gaps' for h in histories):reason='history_has_gaps'
        elif any(not h['history_traversal_complete'] for h in histories):reason='history_loading'
        elif traversed and not conflicts:reason='no_matches_in_traversed_range'
        else:reason='no_matches_in_partial_archive'
    result={'basis':'archived_data','requested_range_traversed':traversed,
        'full_history_verified':False,'empty_reason':reason,'active_conflicts_in_range':conflicts,
        'message_index':'transactional_with_message','next_action':
        'read_history_or_wait_for_collection' if reason in ('history_loading','history_not_scanned','history_has_gaps')
        else 'check_provider_access' if reason=='provider_unavailable'
        else 'wait_for_provider_cooldown' if reason=='provider_rate_limited'
        else 'list_chats' if reason=='not_found_or_not_permitted'
        else 'get_context' if conflicts else 'read_context' if items else 'inspect_chat_coverage'}
    if include:
        result.update(chats_considered=len(records),chats_not_scanned=sum(h['state']=='not_scanned' for h in histories),
            chats_loading=sum(h['state']=='loading' for h in histories),chats_with_gaps=sum(bool(h['gaps']) for h in histories))
        if ids is not None and len(ids)<=20:
            permitted=[r['chat_id'] for r in records]
            result['chats']=list(chat_statuses(c,p,permitted,lower,upper).values())
    return result


def file_query_evidence(c,p,normalized,items,paged=False,include=True):
    from .sync_status import _files
    result=query_evidence(c,p,normalized,items,paged=paged,include=include)
    if not include:
        result['file_index']={'asynchronous':True,'full_content_verified':False,
            'counts_included':False,'next_action':'get_sync_status_for_selected_chat'}
        if not items:
            if result['empty_reason']=='no_matches_in_traversed_range':
                result['empty_reason']='file_index_coverage_not_checked'
            result['next_action']='get_sync_status_for_selected_chat'
        return result
    chat=normalized.get('chat');chats=normalized.get('chats')
    ids=[chat] if chat else chats
    if chat and chats is not None:ids=[chat] if chat in chats else []
    account=normalized.get('account');accounts=normalized.get('accounts')
    accts=[account] if account else accounts
    if account and accounts is not None:accts=[account] if account in accounts else []
    permitted=rows(c,p,ids,accts)
    downloads,indexed=_files(c,{**p,'chat_ids':[r['chat_id'] for r in permitted]})
    result['file_index']={'asynchronous':True,'download':{},'extraction':{},'enrichment':{}}
    for counts in downloads.values():
        for state,count in counts.items():result['file_index']['download'][state]=result['file_index']['download'].get(state,0)+count
    for counts in indexed.values():
        for kind in ('extraction','enrichment'):
            for state,count in counts[kind].items():result['file_index'][kind][state]=result['file_index'][kind].get(state,0)+count
    pending=sum(result['file_index'][kind].get(state,0)
        for kind in ('download','extraction','enrichment') for state in ('queued','leased','unknown'))
    result['file_index']['pending_stages_in_scope']=pending
    result['file_index']['full_content_verified']=False
    if not items and not paged and permitted and pending:
        result['empty_reason']='file_content_not_fully_indexed'
        result['next_action']='inspect_file_states_or_wait_for_indexing'
    elif result['empty_reason']=='no_matches_in_traversed_range':
        result['empty_reason']='no_matches_in_indexed_content'
        result['next_action']='inspect_file_coverage'
    result['file_index']['scope_note']='Counts cover permitted selected chats; pending files are not proven matches.'
    return result
