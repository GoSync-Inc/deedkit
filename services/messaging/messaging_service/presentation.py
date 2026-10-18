"""Bounded agent summaries; full evidence remains available by explicit opt-in."""
import re

MESSAGE_FIELDS = ('id','message','account','chat','sender','sent_at','revision','direction',
    'provider_message_id','kind','origin','historical','is_backfill','event_sequence','sender_name','sender_name_basis','username',
    'chat_title','chat_type','reply_to_provider_id','mentions_owner','reply_to_owner','forwarded','media_kind','body_truncated',
    'sender_identity_basis','agent_send','delivery_receipt','formatting')
CONVERSATION_FIELDS = ('body','kind','is_service','display_text','position','order_basis',
    'order_status','timestamp_resolution','first_observed_at','first_observed_basis',
    'ingested_at','ordering_warning','conversation_view_version','provider_message_type',
    'media_duration_seconds','provider_id','timestamp_precision','first_observed_at_basis',
    'ordering','display_text_truncated')
FILE_FIELDS = ('attachment_id','id','message_id','account','chat','revision','sha256','mime',
    'bytes','display_name','download_status','extraction_status','enrichment_status',
    'truncated','error_code','title','document_type','snippet','match_type',
    'matched_content','match_sources','message_context','processing_profile','error_detail','attempts','next_due')


TRANSCRIPT_BODY_CHARS = 2000
LEAN_FILE_FIELDS = ('id','display_name','mime','bytes','download_status','extraction_status','error_code')


def options(method, arguments):
    arguments=dict(arguments)
    detail=arguments.pop('detail','compact')
    # Explicit legacy options remain valid; otherwise compact is the default.
    if 'compact' in arguments:
        if arguments['compact'] is None:arguments['compact']=detail=='compact'
        else:detail='compact' if arguments['compact'] else 'full'
    if detail=='transcript':
        # The numbered transcript exists only in the conversation view.
        if arguments.get('view')!='conversation':
            from .store import APIError
            raise APIError('transcript_requires_conversation_view')
        if 'body_chars' in arguments and arguments['body_chars'] is None:
            arguments['body_chars']=TRANSCRIPT_BODY_CHARS
    if 'include_coverage' in arguments and arguments['include_coverage'] is None:
        arguments['include_coverage']=detail=='full'
    return arguments,detail


def file_summary(row):
    result={k:row[k] for k in FILE_FIELDS if k in row and row[k] is not None}
    if row.get('excerpt'):result['snippet']=row['excerpt']
    for key in ('display_name','title','snippet'):
        if key in result:
            value=result[key]
            match=value.find('[[') if key=='snippet' else -1
            start=max(0,match-40) if match>=160 else 0
            result[key]=value[start:start+160]
    return result


def forward_fields(row, *, brief=False):
    """A forwarded message's sender is the forwarder; the origin names the stated author.

    Queue rows and chat heads (brief) keep the kind and the name; message rows keep every stated field.
    """
    if row.get('forwarded') is not True or not row.get('forward_origin'):return {}
    origin={k:v for k,v in row['forward_origin'].items() if not brief or k in ('kind','sender_name')}
    for key in ('sender_name','post_author'):
        # Control characters would serialize as six-character escapes; names are display text only.
        if key in origin:origin[key]=re.sub(r'[\x00-\x1f]','\ufffd',origin[key][:64])
    return {'forward_origin':origin,'authorship':row.get('authorship') or 'forwarded_by_sender'}


def message_summary(row):
    result={k:row[k] for k in MESSAGE_FIELDS if k in row and row[k] is not None}
    if row.get('conversation_view_version')==1:
        result.update({k:row[k] for k in CONVERSATION_FIELDS if k in row})
    if 'formatting' in result:
        from .incoming_formatting import summary
        result['formatting']=summary(result['formatting'])
    for key,maximum in [('sender_name',64),('username',64),('chat_title',72)]:
        if key in result:result[key]=result[key][:maximum]
    result.update(forward_fields(row))
    snippet=row.get('snippet') or row.get('body') or ''
    files=row.get('attachments') or []
    if not snippet and (files or row.get('media_kind')):
        snippet='['+(row.get('media_kind') or 'attachment')+']'
        if files:snippet+=' '+(files[0].get('display_name') or files[0].get('filename') or '')
        result['display_text_is_media_label']=True
    match=snippet.find('[[')
    start=max(0,match-32) if match>=96 else 0
    result['snippet']=snippet[start:start+96]
    if not result.get('body_truncated'):result.pop('body_truncated',None)
    evidence=row.get('evidence') or {}
    # Never silently remove the reasons that make a result uncertain.
    result['integrity']={'provider_currentness_verified':False,
        **{k:evidence[k] for k in ('active_conflicts','revision_changed_during_read') if evidence.get(k)}}
    if files:
        result['attachments']=[file_summary(f) for f in files[:2]]
        result['attachment_count']=len(files)
        if len(files)>2:result['attachments_truncated']=True
    return result


def _fallback_basis(row, key='sender_name_basis'):
    # Compact rows state the basis only when the name is not this chat's own observation.
    return row.get(key) if row.get(key) not in (None,'message') else None


def lean_file(row):
    return {k:row[k] for k in LEAN_FILE_FIELDS if row.get(k) is not None}


def lean_message(row, *, search=False, files=True):
    """One readable text; order and integrity details only when they qualify it."""
    result={k:row[k] for k in ('id','account','chat','sender','sender_name','sent_at','direction','kind','revision')
        if row.get(k) is not None}
    if 'sender_name' not in result and row.get('username'):result['username']=row['username']
    if result.get('sender_name') and _fallback_basis(row):result['sender_name_basis']=row['sender_name_basis']
    for key,maximum in [('sender_name',64),('username',64)]:
        if key in result:result[key]=result[key][:maximum]
    if search:
        for key in ('chat_title','chat_type'):
            if row.get(key):result[key]=row[key][:72]
        if row.get('match_type') not in (None,'history'):result['match_type']=row['match_type']
    text=row.get('display_text') or ''
    result['display_text']=text
    if row.get('display_text_truncated') or row.get('body_truncated'):result['truncated']=True
    snippet=row.get('snippet') or ''
    # A search excerpt is kept only when it locates a match in a long text.
    if search and len(text)>200 and snippet and not text.startswith(snippet):
        match=snippet.find('[[')
        start=max(0,match-48) if match>=120 else 0
        result['snippet']=snippet[start:start+160]
    for flag in ('mentions_owner','reply_to_owner','forwarded'):
        if row.get(flag) is True:result[flag]=True
    result.update(forward_fields(row))
    if row.get('reply_to_provider_id'):result['reply_to_provider_id']=row['reply_to_provider_id']
    if row.get('agent_send'):
        result['agent_send']={k:row['agent_send'][k] for k in ('send_id','basis') if k in row['agent_send']}
    receipt=row.get('delivery_receipt') or {}
    if receipt.get('state') not in (None,'unknown'):
        result['delivery']={'state':receipt['state'],
            'recipient_delivery_confirmed':receipt.get('recipient_delivery_confirmed') is True}
    if row.get('order_status')=='ambiguous':result['order_status']='ambiguous'
    evidence=row.get('evidence') or {}
    problems={k:evidence[k] for k in ('active_conflicts','revision_changed_during_read') if evidence.get(k)}
    if problems:result['integrity']=problems
    attachments=row.get('attachments') or []
    if files and attachments:
        result['attachments']=[lean_file(f) for f in attachments[:4]]
        if len(attachments)>4:result['attachment_count']=len(attachments)
    return result


def lean_view(view):
    result={k:view[k] for k in ('view','contract_version','order','system_filter','warnings','content_is_untrusted') if k in view}
    # Applies to every returned message; detail=full repeats it per message.
    result['provider_currentness_verified']=False
    return result


def lean_conversation(state, *, shown=()):
    """Heuristic chat-head state without repeating message bodies already shown."""
    if not state:return state
    result={'last_turn':state['last_turn'],'suggested_action':state['suggested_action'],
        'basis':state['basis']['code'],'head_freshness':state['head_freshness']}
    if state.get('provider_head_verified_at'):result['provider_head_verified_at']=state['provider_head_verified_at']
    if state.get('order_status')=='ambiguous':
        result['order_status']='ambiguous'
        result['candidate_message_ids']=[m['message_id'] for m in state['basis'].get('evidence',[])]
    latest=state.get('latest_known_meaningful_message')
    if latest:
        result['latest_message_id']=latest['id']
        if latest['id'] not in shown:
            result['latest_message']={'direction':latest.get('direction'),'sender_name':latest.get('sender_name'),
                'sender_name_basis':_fallback_basis(latest),
                'sent_at':latest.get('sent_at'),'display_text':(latest.get('display_text') or '')[:160],
                **forward_fields(latest,brief=True)}
            result['latest_message']={k:v for k,v in result['latest_message'].items() if v is not None}
    attention=state.get('owner_attention')
    if attention:
        target=attention.get('target_message') or {}
        result['owner_attention']={'last_turn':attention['last_turn'],'suggested_action':attention['suggested_action'],
            **({'target_message_id':target['id']} if target.get('id') else {}),
            **({'truncated':True} if attention.get('truncated') else {})}
    coverage=state.get('coverage') or {}
    problems={k:coverage[k] for k in ('gaps','last_error_code') if coverage.get(k)}
    if problems:result['coverage']=problems
    return result


def lean_chat(row):
    result={k:row[k] for k in ('id','account','title','title_basis','type','last_activity') if row.get(k) is not None}
    result['title']=result['title'][:64]
    state=row.get('conversation')
    latest=(state or {}).get('latest_known_meaningful_message')
    if latest:
        last={'id':latest['id'],'direction':latest.get('direction'),'sender_name':latest.get('sender_name'),
            'sender_name_basis':_fallback_basis(latest),
            'display_text':latest.get('display_text') or '',**forward_fields(latest,brief=True)}
    elif row.get('last_message_id'):
        last={'id':row['last_message_id'],'direction':row.get('last_message_direction'),
            'display_text':row.get('last_message_snippet') or ''}
    else:last=None
    if last:
        if len(last['display_text'])>160:last.update(display_text=last['display_text'][:160],truncated=True)
        result['last_message']={k:v for k,v in last.items() if v is not None}
    if row.get('read_state') in ('observed','stale'):
        result['read_state']=row['read_state']
        for key in ('unread_count','read_by_owner'):
            if row['read_state']=='observed' and row.get(key) is not None:result[key]=row[key]
    if row.get('awaiting_reply'):
        hint={'message_id':row.get('awaiting_reply_message_id'),'revision':row.get('awaiting_reply_revision'),
            'sent_at':row.get('awaiting_reply_sent_at'),
            'sender_id':row.get('awaiting_reply_sender'),
            'sender_name':row.get('awaiting_reply_sender_name') or row.get('awaiting_reply_username'),
            'sender_name_basis':_fallback_basis(row,'awaiting_reply_sender_name_basis'),
            'priority':row.get('awaiting_reply_priority'),'reasons':row.get('awaiting_reply_priority_reasons'),
            'waiting_hours':row.get('awaiting_reply_waiting_hours'),'incoming_count':row.get('awaiting_reply_incoming_count'),
            'basis':row.get('awaiting_reply_basis')}
        if row.get('awaiting_reply_has_question'):hint['has_question']=True
        if row.get('awaiting_reply_dismissed'):hint['dismissed']=True
        if row.get('awaiting_reply_confidence') not in (None,'heuristic_not_obligation'):
            hint['confidence']=row['awaiting_reply_confidence']
        if last and last['id']==hint['message_id']:
            # The hint is the head: it carries the text once and last_message is omitted.
            hint['display_text']=result.pop('last_message')['display_text']
            if last.get('truncated'):hint['truncated']=True
        else:hint['display_text']=(row.get('awaiting_reply_snippet') or '')[:160]
        hint.update(forward_fields({'forwarded':True,'forward_origin':row.get('awaiting_reply_forward_origin'),
            'authorship':row.get('awaiting_reply_authorship')},brief=True))
        if hint['sent_at']==result.get('last_activity'):hint.pop('sent_at')
        result['awaiting_reply']={k:v for k,v in hint.items() if v is not None}
    for key in ('last_message','awaiting_reply'):
        if result.get(key,{}).get('sender_name'):result[key]['sender_name']=result[key]['sender_name'][:64]
    if state:result['conversation']=lean_queue_conversation(state,last,result.get('awaiting_reply'))
    return result


def lean_queue_conversation(state, last, hint):
    """Compact omits a conversation field when it equals a shown message or hint field."""
    shown=[m for m in (last and last['id'],hint and hint.get('message_id')) if m]
    result=lean_conversation(state,shown=shown)
    if result.get('latest_message_id') in shown:result.pop('latest_message_id')
    attention=result.get('owner_attention')
    if attention:
        # The target is the hint's message, or the shown head in a row without a hint.
        target=hint['message_id'] if hint else last and last['id']
        if target and attention.get('target_message_id')==target:attention.pop('target_message_id')
        if attention['suggested_action']==result['suggested_action']:attention.pop('suggested_action')
    if 'candidate_message_ids' in result:
        # Tied candidates list only the IDs not already stated by the hint or the attention target.
        stated={hint and hint['message_id'],(attention or {}).get('target_message_id')}
        result['candidate_message_ids']=[m for m in result['candidate_message_ids'] if m not in stated]
        if not result['candidate_message_ids']:result.pop('candidate_message_ids')
    if hint and result['basis'] in (hint.get('confidence'),hint.get('basis')):result.pop('basis')
    return result


def lean(method, result):
    """Conversation-view compact: every message text once, qualifiers only when they apply."""
    result=dict(result)
    for key in ('sources','chat_sync'):result.pop(key,None)
    if method=='list_chats':
        result['items']=[lean_chat(row) for row in result['items']]
        if result.get('awaiting_reply_scope'):result['awaiting_reply_notice']='heuristic_not_obligation'
        else:
            for key in ('awaiting_reply_scope','awaiting_reply_total','awaiting_reply_days','bot_filter_coverage',
                    'group_attention_coverage'):result.pop(key,None)
        for key in ('read_state_unknown_total','only_unread_notice'):
            if result.get(key) is None:result.pop(key,None)
        result['detail']='compact'
        return result
    search=method=='search'
    result['conversation_view']=lean_view(result['conversation_view'])
    if method=='get_context':
        anchor=result['message']
        rows=result['context']
        result['message']=lean_message(anchor,files=False)
        # The anchor is returned once; anchor_index says where it sits in context.
        result['context']=[lean_message(row) for row in rows if row['id']!=anchor['id']]
        window={k:v for k,v in result['context_window'].items()
            if k not in ('view','include_system','requested_before','requested_after')}
        window['anchor_index']=sum(1 for row in rows if row['id']!=anchor['id'] and
            (row['sent_at'],row['id'])<(anchor['sent_at'],anchor['id']))
        result['context_window']=window
        for key in ('attachments','pending_attachments'):
            result[key]=[lean_file(row) for row in result[key]]
            if not result[key]:result.pop(key)
        if not (result.get('reviews') or {}).get('items'):result.pop('reviews',None)
        result['conversation']=lean_conversation(result.get('conversation'),
            shown=[anchor['id'],*(row['id'] for row in rows)])
    else:
        result['items']=[lean_message(row,search=search) for row in result['items']]
    result['detail']='compact'
    return result


def transcript(method, result):
    """Only the numbered transcript, its cursors and file references."""
    view=result['conversation_view']
    rows=result['context'] if method=='get_context' else result['items']
    output={'transcript':view['transcript'],'order':view.get('order'),'warnings':view.get('warnings',[]),
        'content_is_untrusted':True}
    files=[{'message_id':row['id'],**lean_file(f)} for row in rows for f in row.get('attachments') or []]
    if method=='get_context':
        files+=[{'message_id':result['message']['id'],**lean_file(f)}
            for f in result['attachments']+result['pending_attachments']
            if result['message']['id'] not in {row['id'] for row in rows}]
        output['message_id']=result['message']['id']
        output['context_window']={k:v for k,v in result['context_window'].items()
            if k in ('older_cursor','newer_cursor','older_order','newer_order','has_more_before','has_more_after','anchor_hidden')}
        output['conversation']=lean_conversation(result.get('conversation'),shown=[row['id'] for row in rows])
    else:
        output['next_cursor']=result.get('next_cursor')
        empty=(result.get('result_evidence') or {}).get('empty_reason')
        if empty:output['empty_reason']=empty
    if files:output['files']=files[:20]
    output.update({k:result[k] for k in ('coverage','synthetic') if k in result})
    output['detail']='transcript'
    return output


def render(method, result, detail):
    if detail=='transcript':return transcript(method,result)
    if detail=='compact' and (result.get('view')=='conversation' if method=='list_chats' else
            method in ('search','get_messages','get_context') and 'conversation_view' in result):
        return lean(method,result)
    if detail=='full' or method not in ('list_chats','search','get_messages','get_updates',
        'get_context','search_files','get_file_text','get_file_segments','get_sync_status'):return result
    if method=='get_sync_status':
        # Keep one-chat diagnostics intact. Only the account overview is reduced;
        # the cached evidence remains unchanged and full detail is always available.
        if 'sources' not in result:return result
        compact=dict(result)
        compact['sources']=[]
        for source in result['sources']:
            row={k:source[k] for k in ('account','platform','status','detail_code',
                'archive_detail_code','oldest_message_at','newest_message_at','last_ingest_at','alerts') if k in source}
            # The compact budget carries only the live verdict; full detail has the evidence.
            if 'liveness' in source:row['liveness_state']=source['liveness']['state']
            row['freshness']={k:v for k,v in source.get('freshness',{}).items()
                if k in ('state','scan_state','last_successful_scan_at','scan_age_seconds','basis',
                    'pending_capture_events','last_capture_commit_at','qualifications')
                and v is not None and (k!='qualifications' or v)}
            row['history']={k:v for k,v in source.get('history',{}).items()
                if k in ('observed_chats','unknown_chats','full_history_verified','gap_chats',
                    'catchup_pending_chats','configured_window','last_scan_at')}
            row['operations']={name:{k:v for k,v in operation.items()
                if k in ('state','reason_code','pending_events','collector_running','retry_at','next_action',
                    'storage_paused','storage_paused_jobs','terminal_failed_jobs','retry_queued_jobs','needs_attention')
                and (k not in ('storage_paused','storage_paused_jobs','terminal_failed_jobs','retry_queued_jobs','needs_attention') or v)}
                for name,operation in source.get('operations',{}).items()}
            row['files']={k:v for k,v in source.get('files',{}).items()
                if k in ('download','download_errors','download_error_states','extraction','enrichment')
                and (k!='download_error_states' or v)}
            compact['sources'].append(row)
        compact['detail']='compact'
        compact['next_action']='get_sync_status_detail_full_or_chat_id_for_diagnostics'
        return compact
    result=dict(result)
    result.pop('sources',None)
    if method in ('search','get_messages','get_updates'):
        result['items']=[message_summary(row) for row in result['items']]
    elif method=='get_context':
        result['message']=message_summary(result['message'])
        result['context']=[message_summary(row) for row in result['context']]
        for key in ('attachments','pending_attachments'):
            result[key]=[file_summary(row) for row in result[key]]
        result.pop('chat_sync',None)
    elif method=='list_chats':
        result['items']=[{k:v for k,v in row.items() if v is not None and k!='sync'}
            for row in result['items']]
        for row in result['items']:
            row.pop('sync',None)
            row.pop('awaiting_reply_confidence',None)
            if not result.get('awaiting_reply_scope'):
                for key in tuple(row):
                    if key.startswith('awaiting_reply_'):row.pop(key,None)
                if row.get('read_state')=='unknown':row.pop('read_state',None)
            row['title']=row['title'][:64]
            if result.get('awaiting_reply_scope') and row.get('last_message_snippet'):
                row['last_message_snippet']=row['last_message_snippet'][:80]
            else:row.pop('last_message_snippet',None)
            if row.get('awaiting_reply_snippet'):
                row['awaiting_reply_snippet']=row['awaiting_reply_snippet'][:80]
            if row.get('awaiting_reply_forward_origin'):
                row['awaiting_reply_forward_origin']=forward_fields({'forwarded':True,
                    'forward_origin':row['awaiting_reply_forward_origin']},brief=True)['forward_origin']
        if result.get('awaiting_reply_scope'):result['awaiting_reply_notice']='heuristic_not_obligation'
        if result.get('awaiting_reply_scope') is None:
            result.pop('awaiting_reply_scope',None)
            result.pop('awaiting_reply_total',None)
            result.pop('awaiting_reply_days',None)
            result.pop('bot_filter_coverage',None)
            result.pop('group_attention_coverage',None)
            for key in ('read_state_unknown_total','only_unread_notice'):
                if result.get(key) is None:result.pop(key,None)
    elif method=='search_files':
        result['items']=[file_summary(row) for row in result['items']]
    if method in ('search_files','get_file_text','get_file_segments'):
        result.pop('status',None)  # Legacy extraction alias remains in full mode.
    result['detail']='compact'
    return result
