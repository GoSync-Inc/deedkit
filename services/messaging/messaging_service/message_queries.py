"""Scoped message queries and presentation, separate from authentication/ACK."""
from __future__ import annotations

from .chat_queries import title_sql
from .policy import scope_filter, targets
from .sender_names import resolve
from .search_query import (compile_query, coverage, cursor_binding, decode_cursor,
    encode_cursor, filter_values, fingerprint, output_options, shape_messages, valid_text)


JOINS = '''JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
    LEFT JOIN sender_profiles sp ON sp.account=m.account AND sp.chat=m.chat AND sp.sender=m.sender
      AND EXISTS(SELECT 1 FROM messages source WHERE source.id=sp.source_message
        AND source.account=m.account AND source.chat=m.chat AND source.sender=m.sender AND NOT source.deleted)
    LEFT JOIN message_revisions mr ON mr.message=m.id AND mr.revision=m.revision
    LEFT JOIN message_metadata mm ON mm.message=m.id AND mm.revision=m.revision'''
# A forwarded message stored before origin capture reports not_captured, never a guess.
FORWARD = '''CASE WHEN coalesce(mm.metadata,mr.snapshot->'metadata')->'forwarded'='true'::jsonb
      THEN CASE WHEN jsonb_typeof(coalesce(mm.metadata,mr.snapshot->'metadata')->'forward_origin')='object'
        THEN coalesce(mm.metadata,mr.snapshot->'metadata')->'forward_origin'
        ELSE '{"kind":"not_captured"}'::jsonb END END AS forward_origin,
    CASE WHEN coalesce(mm.metadata,mr.snapshot->'metadata')->'forwarded'='true'::jsonb
      THEN 'forwarded_by_sender'::text END AS authorship'''
META = '''nullif(sp.display_name,'') AS sender_name,nullif(sp.username,'') AS username,
    '''+title_sql('ch')+''' AS chat_title,ch.chat_type,ch.provider_chat AS provider_chat_id,m.provider_id,
    coalesce(mm.metadata,mr.snapshot->'metadata')->>'reply_to_provider_id' AS reply_to_provider_id,
    CASE WHEN jsonb_typeof(coalesce(mm.metadata,mr.snapshot->'metadata')->'forwarded')='boolean'
      THEN (coalesce(mm.metadata,mr.snapshot->'metadata')->>'forwarded')::boolean END AS forwarded,
    '''+FORWARD+''',
    CASE WHEN jsonb_typeof(coalesce(mm.metadata,mr.snapshot->'metadata')->'mentions_owner')='boolean'
      THEN (coalesce(mm.metadata,mr.snapshot->'metadata')->>'mentions_owner')::boolean END AS mentions_owner,
    CASE WHEN jsonb_typeof(coalesce(mm.metadata,mr.snapshot->'metadata')->'reply_to_owner')='boolean'
      THEN (coalesce(mm.metadata,mr.snapshot->'metadata')->>'reply_to_owner')::boolean END AS reply_to_owner,
    coalesce(mm.metadata,mr.snapshot->'metadata')->>'media_kind' AS media_kind,
    coalesce(mm.metadata,mr.snapshot->'metadata')->>'sender_identity_basis' AS sender_identity_basis,
    coalesce(mm.metadata,mr.snapshot->'metadata')->'is_service'='true'::jsonb AS is_service,
    coalesce(mm.metadata,mr.snapshot->'metadata')->>'message_kind' AS message_kind,
    coalesce(mm.metadata,mr.snapshot->'metadata')->>'provider_message_type' AS provider_message_type,
    coalesce(mm.metadata,mr.snapshot->'metadata')->>'timestamp_resolution' AS timestamp_resolution,
    CASE WHEN jsonb_typeof(coalesce(mm.metadata,mr.snapshot->'metadata')->'media_duration_seconds')='number'
      THEN (coalesce(mm.metadata,mr.snapshot->'metadata')->>'media_duration_seconds')::double precision END AS media_duration_seconds,
    (SELECT platform FROM accounts WHERE id=m.account) AS platform,
    (SELECT observed_at FROM message_revisions WHERE message=m.id AND revision=1) AS first_observed_at,
    'archive_revision_observation'::text AS first_observed_at_basis'''
BASE = 'm.id,m.account,m.chat,m.sender,m.body,m.sent_at,m.revision,m.direction'


def forward_presence(rows):
    # Only a forwarded message carries an origin and the authorship marker, in every detail level.
    for row in rows:
        for key in ('forward_origin','authorship'):
            if row.get(key) is None:row.pop(key,None)
    return rows


def hint_forward_origin(c, rows):
    """The reply-queue hint states the origin of its forwarded message (rows already authorized).

    The origin is read for the hint's own revision, so a concurrent edit never relabels a shown snippet.
    """
    hints=[(r['awaiting_reply_message_id'],r['awaiting_reply_revision']) for r in rows
        if r.get('awaiting_reply_message_id') and r.get('awaiting_reply_revision') is not None]
    found={(r['id'],r['revision']):r for r in c.execute('SELECT h.id,h.revision,'+FORWARD+
        ' FROM unnest(%s::text[],%s::integer[]) AS h(id,revision) '
        'JOIN message_revisions mr ON mr.message=h.id AND mr.revision=h.revision '
        'LEFT JOIN message_metadata mm ON mm.message=h.id AND mm.revision=h.revision',
        ([h[0] for h in hints],[h[1] for h in hints]))} if hints else {}
    for row in rows:
        origin=found.get((row.get('awaiting_reply_message_id'),row.get('awaiting_reply_revision'))) or {}
        if origin.get('forward_origin'):
            row['awaiting_reply_forward_origin']=origin['forward_origin']
            row['awaiting_reply_authorship']=origin['authorship']
    return rows


def enrich(c, rows, p):
    """Rows were individually authorized; metadata comes from the same chat.

    A missing sender name falls back only to names the caller can read (sender_names).
    """
    if not rows:
        return rows
    metadata=c.execute('SELECT m.id,m.account,m.chat,m.sender,'+META+' FROM messages m '+JOINS+' WHERE m.id=ANY(%s)',
        ([r['id'] for r in rows],)).fetchall()
    resolve(c,p,forward_presence(metadata))
    by_id={r['id']:r for r in metadata}
    for row in rows:
        row.update(by_id.get(row['id'],{}))
    from .result_evidence import decorate
    decorate(c,[r for r in rows if 'revision' in r])
    return rows


def filters(p, *, account='',chat='',sender='',accounts=None,chats=None,senders=None,
            from_time='',to_time='',direction='',exclude_channels=False):
    from .store import APIError,date_bound
    if not isinstance(direction,str) or direction not in ('','incoming','outgoing') or type(exclude_channels) is not bool:
        raise APIError('invalid_filter')
    lower,upper=date_bound(from_time),date_bound(to_time)
    if lower and upper and lower>=upper:
        raise APIError('invalid_date_range')
    scope,args=scope_filter(p,'m'); clauses=[scope,'NOT m.deleted']
    normalized={'direction':direction,'from_time':lower,'to_time':upper}
    if exclude_channels:
        normalized['exclude_channels']=True
        clauses.append("EXISTS(SELECT 1 FROM chats ch WHERE ch.id=m.chat AND ch.account=m.account AND ch.chat_type IS DISTINCT FROM 'channel')")
    for column,value in [('account',account),('chat',chat),('sender',sender)]:
        valid_text(value)
        normalized[column]=value
        if value: clauses.append('m.'+column+'=%s');args.append(value)
    for column,values in [('account',accounts),('chat',chats),('sender',senders)]:
        values=filter_values(values);normalized[column+'s']=values
        if values is not None:
            clauses.append('m.'+column+'=ANY(%s)');args.append(values)
    if direction:clauses.append('m.direction=%s');args.append(direction)
    for bound,op in [(lower,'>='),(upper,'<')]:
        if bound:clauses.append('m.sent_at'+op+'%s');args.append(bound)
    return clauses,args,normalized


def search(store,c,p,query='',account='',chat='',sender='',from_time='',to_time='',after='',limit=20,
           *,accounts=None,chats=None,senders=None,query_mode='all',order='asc',before='',direction='',
           search_language='auto',fuzzy=False,compact=False,body_chars=None,include_coverage=True,view='archive',include_system=False,
           exclude_channels=False):
    from .store import APIError,bounded,date_bound,serial
    import re
    from .conversation import options,numeric_order_sql,visible_sql,decorate_messages,presentation
    options(view,include_system)
    conversation=view=='conversation'
    bounded(limit)
    cap=output_options(compact,body_chars,include_coverage)
    plan=compile_query(query,query_mode,search_language)
    if order not in ('asc','desc','relevance') or type(fuzzy) is not bool:
        raise APIError('invalid_filter')
    valid_text(after,4096);valid_text(before,4096)
    if after and before or before and order=='relevance':raise APIError('invalid_filter')
    if fuzzy and (query_mode!='all' or not re.fullmatch(r'\w{3,64}',query,flags=re.UNICODE)):
        raise APIError('invalid_fuzzy_query')
    clauses,args,normalized=filters(p,account=account,chat=chat,sender=sender,accounts=accounts,
        chats=chats,senders=senders,from_time=from_time,to_time=to_time,direction=direction,exclude_channels=exclude_channels)
    binding=cursor_binding(p,'messages',{**normalized,'query':query,'query_mode':query_mode,
        'language':search_language,'order':order,'fuzzy':fuzzy,
        **({'view':view,'include_system':include_system,'conversation_version':1} if conversation else {})})
    extra_join=' LEFT JOIN message_metadata cmd ON cmd.message=m.id AND cmd.revision=m.revision' if conversation else ''
    if conversation and not include_system:clauses.append(visible_sql('m','cmd'))
    fuzzy_used=False
    if fuzzy:
        has_primary=c.execute('WITH '+plan.cte+' SELECT 1 FROM messages m '+extra_join+' CROSS JOIN search_query q WHERE '+
            ' AND '.join(clauses)+' AND '+plan.predicate('m')+' LIMIT 1',[*plan.params,*args]).fetchone()
        fuzzy_used=not bool(has_primary)
    score=plan.rank('m','m.body');match_type=plan.match_type('m')
    if fuzzy_used:
        # Fixed threshold, server-side candidate cap and statement timeout.
        # No silent union of approximate and exact results.
        c.execute("SELECT set_config('pg_trgm.word_similarity_threshold','0.6',true)")
        condition='lower(m.body) %%> lower(q.literal)'
        score='round((word_similarity(lower(q.literal),lower(m.body))*1000000)::numeric)::bigint'
        match_type="'trigram_fallback'::text"
    else:
        condition=plan.predicate('m')
    base_sql='SELECT m.id,m.sent_at,m.revision,'+(numeric_order_sql('m') if conversation else '0::bigint')+' AS source_order,'+score+' AS score FROM messages m '+extra_join+' CROSS JOIN search_query q WHERE '+\
        ' AND '.join(clauses)+' AND '+condition
    ctes=[plan.cte]
    if fuzzy_used:
        ctes.append('matches AS MATERIALIZED ('+base_sql+' ORDER BY score DESC,m.sent_at DESC,m.id DESC LIMIT 200)')
    elif plan.query:
        # Separate selective indexed matching from the chronological LIMIT.
        # Otherwise the planner may prefer walking the date index, expecting
        # early hits; a rare or absent query then traverses the entire archive.
        ctes.append('matches AS MATERIALIZED ('+base_sql+')')
    else:
        ctes.append('matches AS NOT MATERIALIZED ('+base_sql+')')
    cursor_clause='';cursor_args=[]
    snapshot=None
    snapshot_ctes=list(ctes)
    def read_snapshot():
        value=c.execute('WITH '+','.join(snapshot_ctes)+''' SELECT count(*) AS n,
            coalesce(sum(hashtextextended(id||'|'||sent_at::text||'|'||revision::text||'|'||source_order::text,0)::numeric),0)::text AS checksum
            FROM matches''',[*plan.params,*args]).fetchone()
        return fingerprint(value)
    if after or before:
        cursor=after or before
        if cursor.startswith('q1.'):
            position=decode_cursor(cursor,binding)
            if set(position)!=({'id','at','score','revision','fuzzy','source_order','snapshot'} if conversation else {'id','at','score','revision','fuzzy'}):
                raise APIError('invalid_cursor')
            mid=valid_text(position['id'],empty=False)
            at=date_bound(position['at'])
            if (at is None or type(position['score']) is not int or not 0<=position['score']<=1000000
                    or type(position['revision']) is not int or type(position['fuzzy']) is not bool
                    or position['fuzzy']!=fuzzy_used):
                raise APIError('invalid_cursor')
            anchor=store.message(c,p,mid)
            if conversation:
                if type(position['source_order']) is not int or not isinstance(position['snapshot'],str):raise APIError('invalid_cursor')
                snapshot=read_snapshot()
                if snapshot!=position['snapshot']:raise APIError('cursor_changed',409)
            if anchor['revision']!=position['revision'] or anchor['sent_at']!=at:
                raise APIError('cursor_changed',409)
            if order=='relevance':
                cursor_clause='WHERE (score,sent_at,id)<(%s,%s,%s)'
                cursor_args=[position['score'],at,mid]
            else:
                if conversation:
                    cursor_clause='WHERE (sent_at,source_order,id)'+('<' if before or order=='desc' else '>')+'(%s,%s,%s)'
                    cursor_args=[at,position['source_order'],mid]
                else:
                    cursor_clause='WHERE (sent_at,id)'+('<' if before or order=='desc' else '>')+'(%s,%s)'
                    cursor_args=[at,mid]
        else:
            if order=='relevance' or conversation or exclude_channels:raise APIError('invalid_cursor')
            anchor=store.message(c,p,cursor)
            cursor_clause='WHERE (sent_at,id)'+('<' if before or order=='desc' else '>')+'(%s,%s)'
            cursor_args=[anchor['sent_at'],anchor['id']]
    sort='score DESC,sent_at DESC,id DESC' if order=='relevance' else ('sent_at '+order.upper()+(',source_order '+order.upper() if conversation else '')+',id '+order.upper())
    if conversation:
        # Aggregate and page share one statement snapshot. A late backfill
        # cannot slip between reading this page and creating its continuation.
        ctes.append("view_snapshot AS MATERIALIZED (SELECT count(*) AS n,coalesce(sum(hashtextextended(id||'|'||sent_at::text||'|'||revision::text||'|'||source_order::text,0)::numeric),0)::text AS checksum FROM matches)")
    ctes.append('page AS MATERIALIZED (SELECT * FROM matches '+cursor_clause+' ORDER BY '+sort+' LIMIT %s)')
    final_sort='page.score DESC,m.sent_at DESC,m.id DESC' if order=='relevance' else ('m.sent_at '+order.upper()+(',page.source_order '+order.upper() if conversation else '')+',m.id '+order.upper())
    snippet='left('+plan.snippet('m.body')+',800)'
    projection=BASE if cap is None else BASE.replace('m.body','left(m.body,'+str(cap)+') AS body,length(m.body)>'+str(cap)+' AS body_truncated')
    rows=c.execute('WITH '+','.join(ctes)+' SELECT '+projection+','+META+',page.score,'+match_type+
        ' AS match_type,'+snippet+' AS snippet,page.source_order'+(',view_snapshot.n AS snapshot_count,view_snapshot.checksum AS snapshot_checksum' if conversation else '')+' FROM page JOIN messages m ON m.id=page.id '+JOINS+
        ' CROSS JOIN search_query q'+(' CROSS JOIN view_snapshot' if conversation else '')+' ORDER BY '+final_sort,[*plan.params,*args,*cursor_args,limit+1]).fetchall()
    if conversation and rows:
        current_snapshot=fingerprint({'n':rows[0]['snapshot_count'],'checksum':rows[0]['snapshot_checksum']})
        if snapshot is not None and snapshot!=current_snapshot:raise APIError('cursor_changed',409)
        snapshot=current_snapshot
    items=resolve(c,p,forward_presence(rows[:limit]))
    next_cursor=None
    if len(rows)>limit:
        last=items[-1]
        if conversation:
            snapshot=snapshot or read_snapshot()
            next_cursor=encode_cursor(binding,{'id':last['id'],'at':last['sent_at'],'score':last['score'],
                'revision':last['revision'],'fuzzy':fuzzy_used,'source_order':last['source_order'],'snapshot':snapshot})
        else:next_cursor=encode_cursor(binding,{'id':last['id'],'at':last['sent_at'],'score':last['score'],
            'revision':last['revision'],'fuzzy':fuzzy_used}) if order=='relevance' or fuzzy or exclude_channels else last['id']
    shape_messages(items,compact=False if conversation else compact)
    from .result_evidence import decorate
    from .chat_evidence import query_evidence
    decorate(c,items)
    if conversation:
        decorate_messages(c,p,items,include_system=include_system)
        # Explicit caps bound source excerpts and the added readable field;
        # message IDs retain access to the complete captured provider body.
        if cap is not None:
            for item in items:
                if len(item['display_text'])>cap:
                    item['display_text']=item['display_text'][:cap];item['display_text_truncated']=True
    for item in items:
        for internal in ('source_order','snapshot_count','snapshot_checksum'):item.pop(internal,None)
    targets(p,[r['id'] for r in items])
    return serial({'items':items,'next_cursor':next_cursor,
        'result_evidence':query_evidence(c,p,normalized,items,paged=bool(after or before),include=include_coverage),
        **({'conversation_view':presentation(items,order=order,include_system=include_system)} if conversation else {}),
        'search':{'language':search_language,'query_mode':query_mode,'order':order,'fuzzy_used':fuzzy_used,
            'fuzzy_candidate_limit':200 if fuzzy_used else None},**coverage(store,c,p,include_coverage)})


def history_snapshot(c,p,chat,*,include_system=False):
    """Fingerprint the authorized conversation that supplies context boundaries."""
    from .conversation import visible_sql,numeric_order_sql
    clauses,args,_=filters(p,chat=chat)
    if not include_system:clauses.append(visible_sql('m','cmd'))
    base='SELECT m.id,m.sent_at,m.revision,'+numeric_order_sql('m')+" AS source_order FROM messages m LEFT JOIN message_metadata cmd ON cmd.message=m.id AND cmd.revision=m.revision WHERE "+' AND '.join(clauses)
    value=c.execute('WITH matches AS ('+base+""" ) SELECT count(*) AS n,
        coalesce(sum(hashtextextended(id||'|'||sent_at::text||'|'||revision::text||'|'||source_order::text,0)::numeric),0)::text AS checksum FROM matches""",args).fetchone()
    return fingerprint(value)


def history_cursor(c,p,row,*,order='asc',include_system=False,snapshot=None):
    """Bind navigation to the checked context snapshot, not a later read."""
    from .conversation import sort_key
    _,_,normalized=filters(p,chat=row['chat'])
    if snapshot is None:snapshot=history_snapshot(c,p,row['chat'],include_system=include_system)
    binding=cursor_binding(p,'messages',{**normalized,'query':'','query_mode':'all','language':'auto',
        'order':order,'fuzzy':False,'view':'conversation','include_system':include_system,'conversation_version':1})
    return encode_cursor(binding,{'id':row['id'],'at':row['sent_at'],'revision':row['revision'],
        'score':0,'fuzzy':False,'source_order':sort_key(row)[1],'snapshot':snapshot})
