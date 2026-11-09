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
# ANYM-915: a group with at most ATTENTION_WINDOW incoming messages since the owner's latest one is walked as before.
# A longer one reads only its candidates (the owner-attention boolean keys and the replies to the chat's outgoing
# messages) while both sides stay bounded: at most ATTENTION_OWNER_MESSAGES outgoing messages to probe replies for, and
# at most ATTENTION_ARCHIVE_KEYS boolean-key rows in the whole archive (the partial index's own pg_class count), since
# each costs one primary-key probe whatever account it is in. Otherwise the history is walked, as before.
ATTENTION_WINDOW = 100
ATTENTION_OWNER_MESSAGES = 200
ATTENTION_ARCHIVE_KEYS = 10000
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
    # A shown message keeps its known-conflict caveat whatever the block's basis is (ANYM-616).
    known=known_conflict(message.get('evidence'))
    if known:
        snapshot['conflict']=known
    value=snapshot.get('body')
    snapshot['body_truncated']=isinstance(value,str) and len(value)>1000
    if snapshot['body_truncated']:
        snapshot['body']=value[:1000]
    snapshot['display_text_truncated']=False
    if isinstance(message.get('display_text'),str):
        from .conversation import bound_display_text
        snapshot['display_text'],snapshot['display_text_truncated']=bound_display_text(message,1200)
    if message.get('display_text_media'):
        snapshot['display_text_media']=message['display_text_media']
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


def quoted_stanza_sql(quoted='quoted', provider_chat='c.provider_chat'):
    """The reply reference a WhatsApp group stanza ID answers to, the inverse of provider_reply_matches' scoped arm."""
    prefix=f"CASE WHEN left({quoted}.provider_id,5)='true_' THEN 'true_' ELSE 'false_' END"
    return f'''CASE WHEN right({provider_chat},5)='@g.us'
        AND (left({quoted}.provider_id,length('true_'||{provider_chat}||'_'))='true_'||{provider_chat}||'_'
          OR left({quoted}.provider_id,length('false_'||{provider_chat}||'_'))='false_'||{provider_chat}||'_')
        THEN split_part(substr({quoted}.provider_id,length(({prefix})||{provider_chat}||'_')+1),'_',1) END'''


def group_thread_query(projection, *, account='c.account',chat='c.id',
        provider_chat='c.provider_chat',platform='a.platform',enabled='true',
        criteria='true',limit=None,count=False):
    """Read explicit owner attention after the latest owner message in one chat.

    Any later meaningful owner message in the group answers earlier attention,
    as in the archive view; others' chatter neither answers nor renews it. The
    owner head is one bounded indexed tail read. Up to ATTENTION_WINDOW incoming
    messages since it are walked as they are; beyond that (ANYM-915), while the
    route is bounded, the candidates are the messages whose metadata names the
    owner and the replies to this chat's outgoing messages, a superset of the
    answer that the same filters below then decide, so the result never depends
    on the route. Scoped messages drive every other read.
    """
    from .conversation import meaningful_sql
    from .chat_queries import latest_sql,number
    owner=f'''SELECT m.sent_at,m.id,{number('m')} AS numeric_id FROM messages m
        LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
        WHERE {enabled} AND m.account={account} AND m.chat={chat} AND m.direction='outgoing'
          AND {meaningful_sql()}'''
    window=ATTENTION_WINDOW
    scoped=f"m.account={account} AND m.chat={chat} AND m.direction='incoming'"
    thread=f'''SELECT m.id,m.account,m.chat,m.provider_id,m.revision,m.sender,m.body,m.sent_at,m.direction,
          md.metadata AS thread_metadata FROM @source JOIN LATERAL (
          SELECT md.metadata FROM message_metadata md WHERE md.message=m.id AND md.revision=m.revision
            AND (md.metadata->'mentions_owner'='true'::jsonb OR md.metadata->'reply_to_owner'='true'::jsonb
              OR md.metadata->>'reply_to_provider_id' IS NOT NULL) OFFSET 0) md ON true
        LEFT JOIN owner_last ON true
        WHERE @route AND {enabled} AND {scoped}
          AND m.sent_at>=coalesce(owner_last.sent_at,'-infinity'::timestamptz) AND {group_after_owner_sql(platform)}
          AND {meaningful_sql()} AND {criteria} AND {group_attention_sql(provider_chat=provider_chat)}'''
    walked=f'(SELECT count(*) FROM attention_window)<={window}'
    candidates='(SELECT candidates FROM attention_route)'
    sql=f'''WITH owner_last AS MATERIALIZED ({latest_sql(owner,platform=platform)}),
      attention_window AS MATERIALIZED (
        SELECT m.* FROM messages m WHERE {enabled} AND {scoped} AND NOT m.deleted AND {criteria}
          AND m.sent_at>=coalesce((SELECT sent_at FROM owner_last),'-infinity'::timestamptz)
        ORDER BY m.sent_at DESC LIMIT {window+1}),
      attention_route AS MATERIALIZED (
        SELECT CASE WHEN {walked} THEN false ELSE (SELECT count(*) FROM (SELECT FROM messages quoted
            WHERE {enabled} AND quoted.account={account} AND quoted.chat={chat} AND quoted.direction='outgoing'
              AND NOT quoted.deleted LIMIT {ATTENTION_OWNER_MESSAGES+1}) owned)<={ATTENTION_OWNER_MESSAGES}
          AND coalesce((SELECT x.reltuples BETWEEN 0 AND {ATTENTION_ARCHIVE_KEYS} FROM pg_catalog.pg_class x
            WHERE x.oid=to_regclass('public.message_metadata_owner_attention')),false) END AS candidates),
      attention_candidates AS MATERIALIZED (
        -- Each candidate is one primary-key read of its message (OFFSET 0 keeps the planner from hashing the chat).
        SELECT md.message AS id FROM message_metadata md CROSS JOIN LATERAL (SELECT 1 FROM messages m
            WHERE m.id=md.message AND m.revision=md.revision AND {scoped} AND {criteria} OFFSET 0) m
          WHERE {candidates} AND (md.metadata->'mentions_owner'='true'::jsonb OR md.metadata->'reply_to_owner'='true'::jsonb)
        UNION
        SELECT md.message FROM messages quoted
          CROSS JOIN LATERAL (VALUES (quoted.provider_id),({quoted_stanza_sql('quoted',provider_chat)})) reference(id)
          -- One reply_reference index probe per reference, never a join over the metadata table.
          CROSS JOIN LATERAL (SELECT md.message FROM message_metadata md CROSS JOIN LATERAL (SELECT 1 FROM messages m
              WHERE m.id=md.message AND m.revision=md.revision AND {scoped} AND {criteria} OFFSET 0) m
            WHERE md.metadata->>'reply_to_provider_id'=reference.id OFFSET 0) md
          WHERE {candidates} AND {enabled} AND quoted.account={account} AND quoted.chat={chat}
            AND quoted.direction='outgoing' AND NOT quoted.deleted),
      thread_messages AS MATERIALIZED (
        {thread.replace('@source','attention_window m').replace('@route',walked)}
        UNION ALL
        {thread.replace('@source','attention_candidates ac CROSS JOIN LATERAL (SELECT * FROM messages m WHERE m.id=ac.id OFFSET 0) m').replace('@route',candidates)}
        UNION ALL
        {thread.replace('@source','messages m').replace('@route','NOT '+walked+' AND NOT '+candidates)})
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


def _ranked(found):
    """A page read's rows as lists keyed by pair ordinal, in statement order; the batch columns removed."""
    listed={}
    for row in found:
        listed.setdefault(row.pop('_batch_n'),[]).append(row);row.pop('_batch_position',None)
    return listed


def conversation_states(c,p,pairs,*,titles=None):
    """ANYM-889: conversation_state for a page of (account, chat) pairs in a fixed number of statements.

    Each read runs once for the page; the head and fallback reads once per platform, whose SQL the platform
    shapes. enrich and decorate_messages run once for the heads and once for the fallbacks and group attention,
    each list decorated as its own call would be (its excerpt budget, positions and attachment items). Every row
    equals conversation_state's answer for its chat in the same transaction (the oracle in
    tests/test_conversation_state.py). A failed read fails the page, as a failed per-chat call failed it.
    """
    from .store import APIError
    from .conversation import decorate_messages, meaningful_sql, visible_sql
    from .chat_queries import latest_sql,number
    from .message_queries import BASE, enrich
    from .chat_evidence import rows, history as coverage_history
    requested=[(account,chat) for account,chat in pairs]
    pairs=list(dict.fromkeys(requested))
    if not pairs:return []
    accounts=[account for account,_ in pairs];chats=[chat for _,chat in pairs]
    scope,args=scope_filter(p,'c',chat_column='id')
    infos={(r['account'],r['id']):r for r in c.execute('''SELECT c.id,c.account,c.chat_type,c.provider_chat,a.platform,
        coalesce(ro.is_self_chat,false) AS self_chat,CURRENT_TIMESTAMP AS as_of
        FROM unnest(%s::text[],%s::text[]) AS k(account,chat) JOIN chats c ON c.account=k.account AND c.id=k.chat
        JOIN accounts a ON a.id=c.account LEFT JOIN chat_read_observations ro
        ON ro.account=c.account AND ro.chat=c.id WHERE '''+scope,[accounts,chats,*args]).fetchall()}
    if any(pair not in infos for pair in pairs):
        raise APIError('not_found',404)
    ordinal={pair:n for n,pair in enumerate(pairs,1)}
    page='''SELECT k.n AS _batch_n,found.* FROM unnest(%s::integer[],%s::text[],%s::text[]) AS k(n,account,chat)
        CROSS JOIN LATERAL ('''
    def per_platform(chosen,query):
        """One statement per platform over the chosen pairs, each pair's rows in its own statement's order."""
        found={}
        for telegram in (True,False):
            part=[pair for pair in chosen if (infos[pair]['platform']=='telegram')==telegram]
            if part:
                platform="'telegram'" if telegram else "'whatsapp'"
                listed=_ranked(c.execute(query(platform,number('m') if telegram else '0::bigint'),
                    [[ordinal[pair] for pair in part],[a for a,_ in part],[x for _,x in part]]).fetchall())
                found.update({pair:listed.get(ordinal[pair],[]) for pair in part})
        return found
    head_query=''' FROM messages m LEFT JOIN message_metadata md ON md.message=m.id
        AND md.revision=m.revision WHERE m.account=k.account AND m.chat=k.chat AND NOT m.deleted AND '''+visible_sql()
    def heads(platform,numeric):
        # conversation_state's head read for each pair: the newest second, then its bounded per-direction tails.
        head_sql='SELECT '+BASE+','+numeric+' AS numeric_id'+head_query+' AND m.sent_at=(SELECT sent_at FROM newest)'
        return (page+'''SELECT h.*,row_number() OVER (ORDER BY h.sent_at DESC,coalesce(h.numeric_id,0) DESC,h.id DESC)
            AS _batch_position FROM (WITH newest AS (SELECT m.sent_at'''+head_query+''' ORDER BY m.sent_at DESC,m.id DESC LIMIT 1) '''+
            latest_sql(head_sql,both_directions=True,platform=platform,limit=HEAD_LIMIT+1)+''') h) found
            ORDER BY k.n,found._batch_position''')
    observed={};truncated={}
    for pair,listed in per_platform(pairs,heads).items():
        truncated[pair]=len(listed)>HEAD_LIMIT and listed[0]['sent_at']==listed[-1]['sent_at']
        observed[pair]=listed[:HEAD_LIMIT]
    groups=[observed[pair] for pair in pairs]
    flat=[m for group in groups for m in group]
    enrich(c,flat,p,titles=titles,groups=groups);decorate_messages(c,p,flat,groups=groups)
    def fallbacks(platform,numeric):
        fallback_sql='SELECT '+BASE+','+numeric+''' AS numeric_id FROM messages m LEFT JOIN message_metadata md
            ON md.message=m.id AND md.revision=m.revision WHERE m.account=k.account AND m.chat=k.chat
            AND '''+meaningful_sql()
        return page+latest_sql(fallback_sql,both_directions=True,platform=platform)+') found ORDER BY k.n'
    fallback={pair:listed[0] for pair,listed in per_platform(
        [pair for pair in pairs if not any(_meaningful(m) for m in observed[pair])],fallbacks).items() if listed}
    attention={pair:[] for pair in pairs}
    grouped=[pair for pair in pairs if infos[pair]['chat_type']=='group']
    if grouped:
        # The per-chat statement's ORDER BY, as a rank: the outer sort keeps each chat's thread order.
        projection=(BASE+", CASE WHEN gs.platform='whatsapp' AND "+group_after_owner_sql('gs.platform',same_time=True)+
            " THEN 'ambiguous' ELSE 'open' END AS attention_status,row_number() OVER (ORDER BY m.sent_at DESC,"
            "CASE WHEN gs.platform='telegram' THEN "+number('m')+" ELSE 0::bigint END DESC,m.id DESC) AS _batch_position")
        listed=_ranked(c.execute('''SELECT gs.n AS _batch_n,threads.* FROM unnest(%s::integer[],%s::text[],%s::text[],
            %s::text[],%s::text[]) AS gs(n,account,chat,provider_chat,platform) CROSS JOIN LATERAL ('''+group_thread_query(
                projection,account='gs.account',chat='gs.chat',provider_chat='gs.provider_chat',platform='gs.platform',limit=17)+
            ''') threads ORDER BY gs.n,threads._batch_position''',[[ordinal[pair] for pair in grouped],[a for a,_ in grouped],
                [x for _,x in grouped],[infos[pair]['provider_chat'] for pair in grouped],
                [infos[pair]['platform'] for pair in grouped]]).fetchall())
        attention.update({pair:listed.get(ordinal[pair],[]) for pair in grouped})
    groups=[[fallback[pair]] for pair in pairs if pair in fallback]+[attention[pair] for pair in pairs]
    flat=[m for group in groups for m in group]
    enrich(c,flat,p,titles=titles,groups=groups);decorate_messages(c,p,flat,groups=groups)
    coverage={(r['account'],r['chat_id']):r for r in rows(c,p,chats,sorted(set(accounts)))}
    restricted=p.get('chat_ids') is not None
    health,failed={},False
    if not restricted:
        try:
            # A savepoint, as in collector_facts: a failed SELECT must not leave the queue's transaction aborted.
            with c.transaction():
                health={r.pop('account'):r for r in c.execute(
                    'SELECT account,status,detail_code,cooldown_until FROM collector_health WHERE account=ANY(%s)',
                    (sorted(set(accounts)),)).fetchall()}
        except Exception:
            health,failed={},True
    results={}
    for pair in pairs:
        account,chat=pair;info=infos[pair];head=observed[pair];row=coverage.get(pair)
        # collector_facts' answer for this chat from the page's one health read.
        progress=(row or {}).get('wa_progress')
        facts={'catchup_progress_at':progress.get('catchup_progress_at') if isinstance(progress,dict) else None}
        collector=facts if restricted or failed else {**(health.get(account) or {}),**facts}
        result=derive_state(head, platform=info['platform'], chat_type=info['chat_type'],
            as_of=info['as_of'],history=coverage_history(row) if row else {},self_chat=info['self_chat'] or
                any(m.get('is_self_chat') is True for m in head) or
                info['platform']=='telegram' and info['provider_chat']=='777000',
            attention=attention[pair],truncated=truncated[pair],meaningful_fallback=fallback.get(pair),collector=collector)
        result.update(account=account,chat_id=chat)
        results[pair]=result
    return [results[pair] for pair in requested]
