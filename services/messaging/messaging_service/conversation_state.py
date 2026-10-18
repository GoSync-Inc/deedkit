"""Scoped archive head facts and explicitly heuristic conversation suggestions.

A database read is not a provider synchronization. Source timestamps retain their
observed resolution, and neither hash IDs nor ingest order resolve WhatsApp ties.
"""
from datetime import datetime, timedelta

from .policy import scope_filter


HEAD_FRESHNESS = timedelta(minutes=5)
HEAD_LIMIT = 64


def _instant(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _evidence(messages):
    return [{'message_id':m['id'], 'revision':m['revision']} for m in messages]


def _snapshot(message):
    if message is None:
        return None
    snapshot={key:message.get(key) for key in ('id', 'revision', 'direction', 'sender',
        'sender_name', 'sender_name_basis', 'username', 'sent_at', 'body', 'kind', 'display_text',
        'order_basis', 'order_status', 'timestamp_precision')} | {
            'provider_message_id':message.get('provider_id')}
    for key,limit in (('body',1000),('display_text',1200)):
        value=snapshot.get(key)
        snapshot[key+'_truncated']=isinstance(value,str) and len(value)>limit
        if snapshot[key+'_truncated']:
            snapshot[key]=value[:limit]
    return snapshot


def _meaningful(message):
    return not message.get('is_service') and (
        bool((message.get('body') or '').strip()) or
        message.get('kind') not in (None, 'unknown', 'system', 'text') or
        bool(message.get('attachments')))


def _unstable(messages):
    if any((m.get('evidence') or {}).get('revision_changed_during_read') for m in messages):
        return 'message_revision_changed_during_read'
    if any((m.get('evidence') or {}).get('active_conflicts',0) for m in messages):
        return 'message_version_conflict'
    return None


def derive_state(messages, *, platform, chat_type, as_of, history,
        self_chat=False, attention=None, truncated=False, meaningful_fallback=None):
    """Derive from newest-first messages selected with the shared order resolver.

    The argument contains the actual authorized chat head, never a context page.
    Equal-time WhatsApp candidates remain a set unless verified source evidence
    supplies an order. A same-actor set still establishes who last spoke.
    """
    observed = [m for m in messages if not m.get('is_service') and not m.get('deleted')]
    significant = [m for m in observed if _meaningful(m)]
    if not significant and meaningful_fallback is not None:
        significant = [meaningful_fallback]
    latest = significant[0] if significant else None
    top = observed[0] if observed else None
    # Only compare source times. first_observed_at cannot establish chronology.
    tie = [m for m in significant if m['sent_at']==latest['sent_at']] if latest else []
    head_tie=[m for m in observed if m['sent_at']==top['sent_at']] if top else []
    order_candidates=head_tie
    source_order = platform=='telegram' and bool(order_candidates) and all(
        str(m.get('provider_id') or '').isascii() and
        str(m.get('provider_id') or '').isdigit() and len(str(m['provider_id']))<=18 for m in order_candidates)
    verified = bool(order_candidates) and all(m.get('order_basis')=='verified_provider_order' and
        m.get('order_status')=='known' for m in order_candidates)
    ambiguous = len(head_tie)>1 and not (source_order or verified)
    unclassified=bool(top) and (not _meaningful(top) or not (source_order or verified) and
        any(not _meaningful(m) for m in head_tie))
    candidates = tie if ambiguous else ([latest] if latest else [])
    directions = {m.get('direction') for m in candidates}
    actor = 'owner' if directions=={'outgoing'} else 'peer' if directions=={'incoming'} else 'unknown'
    code = 'last_archived_message_outgoing' if actor=='owner' else 'last_archived_message_incoming'
    action = 'await_peer' if actor=='owner' else 'review_incoming' if actor=='peer' else 'unclear'
    if not observed:
        actor='unknown'; action='unclear'; code='no_archived_messages'
    elif unclassified:
        actor='unknown'; action='unclear'; code='unclassified_latest_message'; candidates=head_tie
    elif ambiguous and actor=='unknown':
        action='unclear'; code='ambiguous_message_order'
    if truncated:
        actor='unknown'; action='unclear'; code='head_candidates_truncated'
    unstable=_unstable(candidates)
    if unstable:
        actor='unknown'; action='unclear'; code=unstable

    scanned = _instant(history.get('last_head_scan_at'))
    if scanned is None:
        freshness='unknown'; uncertainty='provider_head_not_verified'
    elif scanned>as_of:
        freshness='invalid_observation'; uncertainty='provider_head_not_verified'
    elif as_of-scanned>HEAD_FRESHNESS:
        freshness='stale'; uncertainty='provider_head_stale'
    elif top and scanned<_instant(top['sent_at']):
        freshness='behind_latest_message'; uncertainty='provider_head_not_verified'
    else:
        freshness='recently_verified'; uncertainty=None
    gaps=list(history.get('gaps') or [])
    if gaps:
        uncertainty='history_has_gaps'
    if history.get('last_error_code'):
        uncertainty=history['last_error_code']
    if uncertainty and code not in ('no_archived_messages', 'unclassified_latest_message',
            'ambiguous_message_order', 'head_candidates_truncated',
            'message_revision_changed_during_read', 'message_version_conflict'):
        action='unclear'; code=uncertainty
    result = {'latest_known_meaningful_message':_snapshot(latest) if not ambiguous else None,
        'latest_known_meaningful_messages':[_snapshot(m) for m in candidates if _meaningful(m)],
        'last_turn':actor, 'suggested_action':action,
        'basis':{'code':code, 'evidence':_evidence(candidates), 'heuristic':True},
        'as_of':as_of, 'provider_head_verified_at':scanned,
        'head_freshness':freshness, 'order_status':'ambiguous' if ambiguous else 'known' if latest else 'unknown',
        'coverage':{'gaps':gaps, 'history_state':history.get('state'),
            'last_error_code':history.get('last_error_code'), 'full_history_verified':False},
        'scope':'authorized_chat_head', 'contract_version':1}
    if chat_type=='channel' or self_chat:
        result.update(last_turn='not_applicable',suggested_action='not_applicable')
        result['basis']['code']='channel_or_self_chat'
    elif chat_type=='group':
        selected=list(attention or [])
        target=selected[0] if selected else None
        attention_ambiguous=any(m.get('attention_status')=='ambiguous' for m in selected)
        attention_unstable=_unstable(selected) or unstable
        group_action='review_incoming' if target else 'not_applicable'
        group_code='explicit_owner_attention' if target else 'no_observed_owner_attention'
        if target and (attention_ambiguous or uncertainty or attention_unstable):
            group_action='unclear'
            group_code=attention_unstable or ('ambiguous_attention_reply_order' if attention_ambiguous else uncertainty)
        result.update(last_turn='not_applicable',suggested_action=group_action)
        result['basis']={'code':group_code, 'evidence':_evidence(selected), 'heuristic':True}
        result['owner_attention']={'last_turn':'peer' if target and not (attention_ambiguous or attention_unstable) else
            'unknown' if attention_ambiguous or attention_unstable else 'not_applicable',
            'suggested_action':group_action, 'target_message':_snapshot(target),
            'evidence':_evidence(selected), 'thread_basis':'observed_mentions_and_replies',
            'scope':'authorized_explicit_owner_threads', 'truncated':len(selected)>16}
    elif chat_type not in ('private',):
        result['suggested_action']='unclear'
        result['basis']['code']='chat_type_unknown'
    return result


def provider_reply_matches(reply_metadata, target='m', provider_chat='ch.provider_chat'):
    """Exact Telegram ID or scoped WhatsApp group stanza ID; aliases stay local."""
    reference=f"{reply_metadata}.metadata->>'reply_to_provider_id'"
    prefix=f"CASE WHEN left({target}.provider_id,5)='true_' THEN 'true_' ELSE 'false_' END"
    return f'''({target}.provider_id={reference} OR (right({provider_chat},5)='@g.us'
        AND (left({target}.provider_id,length('true_'||{provider_chat}||'_'))='true_'||{provider_chat}||'_'
          OR left({target}.provider_id,length('false_'||{provider_chat}||'_'))='false_'||{provider_chat}||'_')
        AND split_part(substr({target}.provider_id,length(({prefix})||{provider_chat}||'_')+1),'_',1)={reference}))'''


def group_attention_sql(alias='m', metadata='md', chat_alias='c', *, provider_chat=None):
    provider_chat=provider_chat or chat_alias+'.provider_chat'
    return f'''({metadata}.metadata->'mentions_owner'='true'::jsonb OR
        {metadata}.metadata->'reply_to_owner'='true'::jsonb OR ({metadata}.metadata->>'reply_to_provider_id' IS NOT NULL AND EXISTS(
          SELECT 1 FROM messages quoted WHERE quoted.account={alias}.account AND quoted.chat={alias}.chat
            AND NOT quoted.deleted AND quoted.direction='outgoing' AND
            {provider_reply_matches(metadata, 'quoted', provider_chat)} OFFSET 0)))'''


def group_after_owner_sql(platform='a.platform', *, same_time=False):
    """Order an attention message against the chat's latest owner message.

    This is the private-chat rule: a later time, or the same second with a
    higher Telegram numeric ID. A WhatsApp same-second tie stays a candidate.
    """
    from .chat_queries import number
    if same_time:
        return f"coalesce({platform}='whatsapp' AND owner_last.sent_at=m.sent_at,false)"
    return f'''(owner_last.sent_at IS NULL OR m.sent_at>owner_last.sent_at OR
        ({platform}='telegram' AND m.sent_at=owner_last.sent_at AND {number('m')}>owner_last.numeric_id)
        OR ({platform}='whatsapp' AND m.sent_at=owner_last.sent_at))'''


def group_thread_query(projection, *, account='c.account',chat='c.id',
        provider_chat='c.provider_chat',platform='a.platform',enabled='true',
        criteria='true',limit=None,count=False):
    """Read explicit owner attention after the latest owner message in one chat.

    Any later meaningful owner message in the group answers earlier attention,
    as in the archive view; others' chatter neither answers nor renews it. The
    owner head is one bounded indexed tail read, and scoped messages drive
    current metadata PK reads before materialization, so other accounts and
    chats cannot enlarge the candidate pool.
    """
    from .conversation import meaningful_sql
    from .chat_queries import latest_sql,number
    owner=f'''SELECT m.sent_at,m.id,{number('m')} AS numeric_id FROM messages m
        LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
        WHERE {enabled} AND m.account={account} AND m.chat={chat} AND m.direction='outgoing'
          AND {meaningful_sql()}'''
    sql=f'''WITH owner_last AS MATERIALIZED ({latest_sql(owner,platform=platform)}),
      thread_messages AS MATERIALIZED (
        SELECT m.id,m.account,m.chat,m.provider_id,m.revision,m.sender,m.body,m.sent_at,m.direction,
          md.metadata AS thread_metadata FROM messages m JOIN LATERAL (
          SELECT md.metadata FROM message_metadata md WHERE md.message=m.id AND md.revision=m.revision
            AND (md.metadata->'mentions_owner'='true'::jsonb OR md.metadata->'reply_to_owner'='true'::jsonb
              OR md.metadata->>'reply_to_provider_id' IS NOT NULL) OFFSET 0) md ON true
        LEFT JOIN owner_last ON true
        WHERE {enabled} AND m.account={account} AND m.chat={chat} AND m.direction='incoming'
          AND m.sent_at>=coalesce(owner_last.sent_at,'-infinity'::timestamptz) AND {group_after_owner_sql(platform)}
          AND {meaningful_sql()} AND {criteria} AND {group_attention_sql(provider_chat=provider_chat)})
      SELECT {projection} FROM thread_messages m
      CROSS JOIN LATERAL (SELECT m.thread_metadata AS metadata) md
      LEFT JOIN owner_last ON true'''
    if not count:
        sql+=f" ORDER BY m.sent_at DESC,CASE WHEN {platform}='telegram' THEN {number('m')} ELSE 0::bigint END DESC,m.id DESC"
        if limit is not None:sql+=' LIMIT '+str(limit)
    return sql


def conversation_state(c,p,account,chat):
    """Resolve the actual archive head only after checking exact account/chat ACL."""
    from .store import APIError
    from .conversation import decorate_messages, meaningful_sql, visible_sql
    from .chat_queries import latest_sql,number
    from .message_queries import BASE, enrich
    from .chat_evidence import rows, history as coverage_history
    scope,args=scope_filter(p,'c',chat_column='id')
    info=c.execute('''SELECT c.id,c.account,c.chat_type,c.provider_chat,a.platform,
        coalesce(ro.is_self_chat,false) AS self_chat,CURRENT_TIMESTAMP AS as_of
        FROM chats c JOIN accounts a ON a.id=c.account LEFT JOIN chat_read_observations ro
        ON ro.account=c.account AND ro.chat=c.id WHERE c.account=%s AND c.id=%s AND '''+scope,
        [account,chat,*args]).fetchone()
    if info is None:
        raise APIError('not_found',404)
    head_query=''' FROM messages m LEFT JOIN message_metadata md ON md.message=m.id
        AND md.revision=m.revision WHERE m.account=%s AND m.chat=%s AND NOT m.deleted AND '''+visible_sql()
    # Only the newest timestamp is needed before resolving its actor/order.
    # The per-direction provider index then bounds that timestamp's candidates.
    # Never sort the entire conversation to choose a 64-message head.
    # The newest read binds account/chat directly so the chat date index bounds
    # it; a joined scope lets the planner walk the global date index past every
    # newer message of other accounts.
    platform="'telegram'" if info['platform']=='telegram' else "'whatsapp'"
    numeric=number('m') if info['platform']=='telegram' else '0::bigint'
    scoped_head=head_query.replace('FROM messages m','FROM messages m CROSS JOIN scope').replace(
        'm.account=%s AND m.chat=%s','m.account=scope.account AND m.chat=scope.chat')
    head_sql=('SELECT '+BASE+','+numeric+' AS numeric_id'+scoped_head+
        ' AND m.sent_at=(SELECT sent_at FROM newest)')
    observed=c.execute('WITH scope AS (SELECT %s::text AS account,%s::text AS chat),newest AS (SELECT m.sent_at'+
        head_query+
        ' ORDER BY m.sent_at DESC,m.id DESC LIMIT 1) '+
        latest_sql(head_sql,both_directions=True,platform=platform,limit=HEAD_LIMIT+1),
        [account,chat,account,chat]).fetchall()
    truncated=len(observed)>HEAD_LIMIT and observed[0]['sent_at']==observed[-1]['sent_at']
    observed=observed[:HEAD_LIMIT]
    enrich(c,observed,p);decorate_messages(c,p,observed)
    fallback=None
    if not any(_meaningful(m) for m in observed):
        fallback_sql='SELECT '+BASE+','+numeric+''' AS numeric_id FROM messages m CROSS JOIN scope LEFT JOIN message_metadata md
            ON md.message=m.id AND md.revision=m.revision WHERE m.account=scope.account AND m.chat=scope.chat
            AND '''+meaningful_sql()
        fallback=c.execute('WITH scope AS (SELECT %s::text AS account,%s::text AS chat) '+
            latest_sql(fallback_sql,both_directions=True,platform=platform),[account,chat]).fetchone()
        if fallback:
            enrich(c,[fallback],p);decorate_messages(c,p,[fallback])
    attention=[]
    if info['chat_type']=='group':
        projection=(BASE+", CASE WHEN gs.platform='whatsapp' AND "+group_after_owner_sql('gs.platform',same_time=True)+
            " THEN 'ambiguous' ELSE 'open' END AS attention_status")
        attention=c.execute('''WITH group_scope AS (SELECT %s::text AS account,%s::text AS chat,
            %s::text AS provider_chat,%s::text AS platform)
            SELECT threads.* FROM group_scope gs CROSS JOIN LATERAL ('''+group_thread_query(projection,
                account='gs.account',chat='gs.chat',provider_chat='gs.provider_chat',platform='gs.platform',limit=17)+
            ') threads',[account,chat,info['provider_chat'],info['platform']]).fetchall()
        enrich(c,attention,p);decorate_messages(c,p,attention)
    coverage_rows=rows(c,p,[chat],[account])
    covered=coverage_history(coverage_rows[0]) if coverage_rows else {}
    result=derive_state(observed, platform=info['platform'], chat_type=info['chat_type'],
        as_of=info['as_of'],history=covered,self_chat=info['self_chat'] or
            any(m.get('is_self_chat') is True for m in observed) or
            info['platform']=='telegram' and info['provider_chat']=='777000',
        attention=attention,truncated=truncated,meaningful_fallback=fallback)
    result.update(account=account,chat_id=chat)
    return result
