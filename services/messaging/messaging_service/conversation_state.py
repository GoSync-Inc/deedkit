"""Scoped archive head facts and explicitly heuristic conversation suggestions.

A database read is not a provider synchronization. Source timestamps retain their
observed resolution, and neither hash IDs nor ingest order resolve WhatsApp ties.
"""
import math
from datetime import datetime, timedelta, timezone

from .conflict_classes import known_conflict
from .policy import scope_filter


HEAD_FRESHNESS = timedelta(minutes=5)
HEAD_LIMIT = 64
CATCHUP_WINDOW = timedelta(minutes=30)
CLOCK_SKEW = timedelta(minutes=5)  # as_of is the read transaction's start; a marker may be a moment newer
# ANYM-454: what an agent does next for each reason a conversation block is unclear. Reading a chat
# cannot refresh a stale head (the collector does), hence answer_with_caveat rather than a retry; nor does it
# change a stored version conflict (ANYM-616), so only a revision that changed during this read is reread_now.
# CONVERSATION.md and the list_chats description list exactly these pairs (tests pin all three).
NEXT_STEP = {
    'provider_head_stale':'answer_with_caveat', 'provider_head_not_verified':'answer_with_caveat',
    'history_has_gaps':'answer_with_caveat', 'ambiguous_message_order':'answer_with_caveat',
    'ambiguous_attention_reply_order':'answer_with_caveat', 'head_candidates_truncated':'answer_with_caveat',
    'message_version_conflict':'answer_with_caveat', 'message_revision_changed_during_read':'reread_now',
    'rate_limited':'wait_for_provider_cooldown', 'provider_temporarily_unavailable':'wait_for_provider_cooldown',
    'provider_unavailable':'check_provider_access', 'provider_chat_error':'check_provider_access',
    'unclassified_latest_message':'get_context', 'chat_type_unknown':'get_context',
    'no_archived_messages':'wait_for_history'}


def _instant(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _evidence(messages):
    result=[]
    for m in messages:
        item={'message_id':m['id'], 'revision':m['revision']}
        known=known_conflict(m.get('evidence'))
        if known:
            item['conflict']=known
        result.append(item)
    return result


def _snapshot(message):
    if message is None:
        return None
    snapshot={key:message.get(key) for key in ('id', 'revision', 'direction', 'sender',
        'sender_name', 'sender_name_basis', 'username', 'sent_at', 'body', 'kind', 'display_text',
        'order_basis', 'order_status', 'timestamp_precision')} | {
            'provider_message_id':message.get('provider_id')}
    # A forwarded head keeps its attribution: the sender is the forwarder, not the author.
    if message.get('authorship'):
        snapshot.update(forwarded=True,forward_origin=message.get('forward_origin'),authorship=message['authorship'])
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
    # A reviewed known class stands until a later provider edit (ANYM-616): no reread changes it, and the
    # block's evidence carries its meaning instead. Any other open conflict, a NULL class included, stays unstable.
    if any((m.get('evidence') or {}).get('active_conflicts',0) and not known_conflict(m.get('evidence'))
            for m in messages):
        return 'message_version_conflict'
    return None


def _marker(value, as_of):
    """A checkpoint epoch as an instant; a malformed or future value reads as absent."""
    try:
        if type(value) not in (int, float) or not math.isfinite(value) or value<=0:
            return None
        at=datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return at if at<=as_of+CLOCK_SKEW else None


def retry_after(collector, as_of):
    """Whole seconds until the account's rate limit ends, when the collector recorded it."""
    until=(collector or {}).get('cooldown_until')
    if not isinstance(until, datetime):
        return None
    seconds=math.ceil((until-as_of).total_seconds())
    return seconds if seconds>0 else None


def catchup_state(history, collector, as_of):
    """Where a WhatsApp catch-up stands: moving, queued, stalled (with cause) or unknown."""
    collector=collector or {}
    progress=_marker(collector.get('catchup_progress_at'), as_of)
    attempt=_instant(history.get('last_attempt_at'))
    cause=history.get('last_error_code')
    if not cause and retry_after(collector, as_of):
        cause='rate_limited'
    elif not cause and collector.get('status')=='degraded':
        cause=collector.get('detail_code')
    if progress is None:
        state='queued' if attempt is None else 'unknown'
    elif as_of-progress<=CATCHUP_WINDOW:
        state='moving'
    elif cause or attempt is not None and attempt>progress:
        state='stalled'
    else:
        state='queued'
    result={'state':state, 'last_progress_at':progress}
    if state=='stalled' and cause:
        result['cause']=cause
    return result


def _annotate(result, platform, history, collector, as_of):
    """Advisory fields only: a failure here drops the field and never the row."""
    if result['suggested_action']=='unclear':
        code=result['basis']['code']
        try:
            step=NEXT_STEP.get(code, 'get_context')
            result['next_step']=step
            if 'owner_attention' in result:
                result['owner_attention']['next_step']=step
        except Exception:
            pass
        if code=='rate_limited' and 'next_step' in result:
            try:
                wait=retry_after(collector, as_of)
                if wait:
                    result['retry_after_s']=wait
            except Exception:
                pass
    if platform=='whatsapp' and 'catchup_pending' in (history.get('gaps') or ()):
        try:
            result['catchup']=catchup_state(history, collector, as_of)
        except Exception:
            pass
    return result


def derive_state(messages, *, platform, chat_type, as_of, history,
        self_chat=False, attention=None, truncated=False, meaningful_fallback=None, collector=None):
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
    return _annotate(result, platform, history, collector, as_of)


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


def collector_facts(c,p,account,row):
    """Collector-side facts for next_step and catch-up; the advisory read never poisons the page.

    A chat-restricted principal gets no account-wide health (as sync_status), only its chat's marker.
    """
    progress=(row or {}).get('wa_progress')
    facts={'catchup_progress_at':progress.get('catchup_progress_at') if isinstance(progress,dict) else None}
    if p.get('chat_ids') is not None:
        return facts
    try:
        # A savepoint: a failed SELECT must not leave the queue's transaction aborted.
        with c.transaction():
            health=c.execute('SELECT status,detail_code,cooldown_until FROM collector_health WHERE account=%s',
                (account,)).fetchone()
        return {**(health or {}),**facts}
    except Exception:
        return facts


def conversation_state(c,p,account,chat,*,titles=None):
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
    enrich(c,observed,p,titles=titles);decorate_messages(c,p,observed)
    fallback=None
    if not any(_meaningful(m) for m in observed):
        fallback_sql='SELECT '+BASE+','+numeric+''' AS numeric_id FROM messages m CROSS JOIN scope LEFT JOIN message_metadata md
            ON md.message=m.id AND md.revision=m.revision WHERE m.account=scope.account AND m.chat=scope.chat
            AND '''+meaningful_sql()
        fallback=c.execute('WITH scope AS (SELECT %s::text AS account,%s::text AS chat) '+
            latest_sql(fallback_sql,both_directions=True,platform=platform),[account,chat]).fetchone()
        if fallback:
            enrich(c,[fallback],p,titles=titles);decorate_messages(c,p,[fallback])
    attention=[]
    if info['chat_type']=='group':
        projection=(BASE+", CASE WHEN gs.platform='whatsapp' AND "+group_after_owner_sql('gs.platform',same_time=True)+
            " THEN 'ambiguous' ELSE 'open' END AS attention_status")
        attention=c.execute('''WITH group_scope AS (SELECT %s::text AS account,%s::text AS chat,
            %s::text AS provider_chat,%s::text AS platform)
            SELECT threads.* FROM group_scope gs CROSS JOIN LATERAL ('''+group_thread_query(projection,
                account='gs.account',chat='gs.chat',provider_chat='gs.provider_chat',platform='gs.platform',limit=17)+
            ') threads',[account,chat,info['provider_chat'],info['platform']]).fetchall()
        enrich(c,attention,p,titles=titles);decorate_messages(c,p,attention)
    coverage_rows=rows(c,p,[chat],[account])
    covered=coverage_history(coverage_rows[0]) if coverage_rows else {}
    collector=collector_facts(c,p,account,coverage_rows[0] if coverage_rows else None)
    result=derive_state(observed, platform=info['platform'], chat_type=info['chat_type'],
        as_of=info['as_of'],history=covered,self_chat=info['self_chat'] or
            any(m.get('is_self_chat') is True for m in observed) or
            info['platform']=='telegram' and info['provider_chat']=='777000',
        attention=attention,truncated=truncated,meaningful_fallback=fallback,collector=collector)
    result.update(account=account,chat_id=chat)
    return result
