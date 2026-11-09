"""W5-6: today's _list_chats statement builder, frozen (copied from chat_queries._list_chats at main 783de5b9).

The oracle of tests/test_list_chats_sql_pin.py: every statement the live function sends up to and including the page
statement must stay byte-equal, text and parameters, to this copy's. It ends at the page statement; the row
post-processing after it is not part of the pin. The module helpers it calls (latest_sql, title_sql, read_state_sql,
owner_waits, ...) and the page plan helpers (_page_first, _alias_pairs) are looked up on the live module at call time.
"""
# ruff: noqa
from messaging_service import chat_queries as cq
from messaging_service.chat_queries import *  # noqa: F403


def legacy_list_chats(store,c,p,after='',limit=25,*,accounts=None,query='',chat_types=None,
        order='id',exclude_channels=False,awaiting_reply=False,from_time='',to_time='',
        include_coverage=True,awaiting_reply_days=14,exclude_chats=None,exclude_senders=None,
        only_questions=False,only_unread=False,selected_chat=None,include_dismissed=False,
        view='archive',exclude_likely_broadcasts=False,include_muted=False,promotional='exclude',
        my_roles=None,can_post=None,awaiting_their_reply=False,min_wait_hours=None,courtesies='include',plan='page_first'):
    from messaging_service.store import APIError,bounded,date_bound,serial
    from messaging_service import owner_waits
    bounded(limit);valid_text(after,4096);valid_text(query,200)
    options(view)
    if order=='last_activity_desc':order='activity'
    if order not in ('id','activity','priority','waiting_longest') or any(type(v) is not bool for v in
            (exclude_channels,awaiting_reply,include_coverage,only_questions,only_unread,exclude_likely_broadcasts,include_muted)):
        raise APIError('invalid_filter')
    if type(awaiting_reply_days) is not int or not 0<=awaiting_reply_days<=365:
        raise APIError('invalid_filter')
    # ANYM-537: the owner's unanswered runs, refused with any filter that reads the reply queue's hint or the head time.
    min_wait_hours=owner_waits.validate(order,min_wait_hours,awaiting_their_reply=awaiting_their_reply,
        awaiting_reply=awaiting_reply,others={'only_unread':only_unread,'exclude_senders':exclude_senders is not None,
        'exclude_likely_broadcasts':exclude_likely_broadcasts,'from_time':from_time,'to_time':to_time,
        'my_roles':my_roles is not None,'can_post':can_post is not None,'promotional':promotional!='exclude',
        'courtesies':courtesies!='include'})
    if awaiting_reply and order=='id':order='activity'
    if (order=='priority' or exclude_likely_broadcasts) and not awaiting_reply:raise APIError('invalid_filter')
    if include_muted and not (awaiting_reply or awaiting_their_reply):raise APIError('invalid_filter')
    # ANYM-427: likely promotional rows leave the queue by default; include returns them marked, only lists just them.
    if promotional not in PROMOTIONAL or promotional!='exclude' and (exclude_likely_broadcasts or not awaiting_reply):
        raise APIError('invalid_filter')
    # ANYM-810: courtesy closes stay listed by default; exclude and only need the reply queue.
    if courtesies not in COURTESIES or courtesies!='include' and not awaiting_reply:raise APIError('invalid_filter')
    c.execute('SET LOCAL jit=off')
    accounts=filter_values(accounts);chat_types=filter_values(chat_types)
    exclude_chats=filter_values(exclude_chats);exclude_senders=filter_values(exclude_senders)
    # ANYM-533: the account's own role in a Telegram channel or group; before migration 046 every row reads unknown.
    from messaging_service.chat_roles import CHAT_ROLE_SCHEMA,ROLES
    from messaging_service.store import schema_version
    my_roles=filter_values(my_roles);roles_ready=schema_version(c)>=CHAT_ROLE_SCHEMA
    # ANYM-660: the independent filter checks are collected, so one response names every bad filter.
    # An empty role list asks for nothing: refused, never an empty IN ().
    bad=[]
    if chat_types is not None and any(t not in CHAT_TYPES for t in chat_types):bad.append(('chat_types','enum',None,CHAT_TYPES))
    if my_roles is not None and not my_roles:bad.append(('my_roles','min_items',None,ROLES))
    elif my_roles is not None and any(r not in ROLES for r in my_roles):bad.append(('my_roles','enum',None,ROLES))
    if can_post is not None and type(can_post) is not bool:bad.append(('can_post','type','boolean'))
    if bad:raise refusal('invalid_filter',bad,hint='omit my_roles for all roles' if my_roles==[] else None)
    role_sql="coalesce(c.my_role,'unknown')" if roles_ready else "'unknown'::text"
    lower,upper=date_bound(from_time),date_bound(to_time)
    if lower and upper and lower>=upper:raise date_range_refusal()
    scope,args=scope_filter(p,'c',chat_column='id');clauses=[scope]
    # ANYM-878: a catalog page in activity order is found from the newest messages (_page_first), and the WhatsApp
    # pairs are read for its candidates only. A principal with no more chats than the first window keeps the per-chat
    # plan: it reads no more heads than the window would.
    page_first=(plan=='page_first' and order=='activity' and not (awaiting_reply or only_questions or only_unread or awaiting_their_reply or query
        or selected_chat) and exclude_senders is None and (p['chat_ids'] is None or len(p['chat_ids'])>PAGE_WINDOW*(limit+1)))
    aliases,listed=({},{}) if page_first else cq._alias_pairs(accounts,c,p)
    hidden={v['chat']['id']:shown for shown,v in listed.items()}
    if hidden and not selected_chat:clauses.append('NOT c.id=ANY(%s)');args.append(list(hidden))
    if selected_chat:clauses.append('c.id=%s');args.append(selected_chat)
    if accounts is not None:clauses.append('c.account=ANY(%s)');args.append(accounts)
    # A chat whose every archived message is purged stays unlisted until a new message arrives and
    # clears the mark (ANYM-511, 045 purge_chat_reopen). A selected chat stays addressable: reply
    # controls look up their own chat through this query.
    if not selected_chat:clauses.append('c.purged_at IS NULL')
    if query:
        pattern='%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        match="(c.title ILIKE %s ESCAPE E'\\\\' OR "+raw_title_sql()+" AND "+peer_name_sql()+" ILIKE %s ESCAPE E'\\\\'"
        matched=[pattern,pattern];name=query.removeprefix('@')
        if roles_ready and name:
            # A channel's @username, with or without the @ (ANYM-533).
            match+=" OR c.username ILIKE %s ESCAPE E'\\\\'"
            matched.append('%'+name.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
        match+=')'
        # A listed row also matches by its hidden alias chat's title.
        found=[hidden[r['id']] for r in c.execute('SELECT c.id FROM chats c WHERE c.id=ANY(%s) AND '+match,
            [list(hidden),*matched])] if hidden else []
        clauses.append('('+match+(' OR c.id=ANY(%s)' if found else '')+')')
        args.extend([*matched,*([found] if found else [])])
    if chat_types is not None:clauses.append("coalesce(c.chat_type,'unknown')=ANY(%s)");args.append(chat_types)
    # Literals from ROLES, so the unknown-role count below can drop them without touching args.
    role_clauses=[*([role_sql+' IN ('+','.join(sql_literal(r) for r in my_roles)+')'] if my_roles is not None else []),
        *(['c.can_post IS '+('TRUE' if can_post else 'FALSE') if roles_ready else 'false'] if can_post is not None else [])]
    clauses.extend(role_clauses)
    if exclude_channels:clauses.append("c.chat_type IS DISTINCT FROM 'channel'")
    if exclude_chats is not None:
        # Excluding either chat of a conversation excludes the conversation.
        excluded_chats=[*exclude_chats,*(hidden[i] for i in exclude_chats if i in hidden)]
        clauses.append('NOT c.id=ANY(%s)');args.append(excluded_chats)
    if exclude_senders is not None:
        clauses.append('(attention.sender IS NULL OR NOT attention.sender=ANY(%s))');args.append(exclude_senders)
    binding=cursor_binding(p,'chats',{'accounts':accounts,'query':query,'types':chat_types,'order':order,
        'exclude_channels':exclude_channels,'awaiting_reply':awaiting_reply,'awaiting_reply_days':awaiting_reply_days,
        'from':lower,'to':upper,'exclude_chats':exclude_chats,'exclude_senders':exclude_senders,
        'only_questions':only_questions,'only_unread':only_unread,
        **({'exclude_likely_broadcasts':True} if exclude_likely_broadcasts else {}),
        **({'include_muted':True} if include_muted else {}),
        **({'promotional':promotional} if awaiting_reply else {}),
        **({'courtesies':courtesies} if awaiting_reply and courtesies!='include' else {}),
        **({'score_formula':SCORE_FORMULA} if order=='priority' else {}),
        **({'my_roles':my_roles} if my_roles is not None else {}),**({'can_post':can_post} if can_post is not None else {}),
        **({'awaiting_their_reply':True,'min_wait_hours':min_wait_hours} if awaiting_their_reply else {})})
    if view=='conversation':
        binding=cursor_binding(p,'chats-conversation-v1',{'archive_binding':binding})
    pos=decode_cursor(after,binding) if after and order!='id' else None
    now=c.execute('SELECT CURRENT_TIMESTAMP AS t').fetchone()['t'];score_at=now
    if pos and order in ('activity','waiting_longest') and set(pos)!={'id','at'}:raise cursor_refusal()
    if pos and order=='priority':
        if set(pos)!={'id','at','score','as_of'} or type(pos['score']) is not int:raise cursor_refusal()
        score_at=position_date(pos['as_of'])
        if score_at is None or score_at>now:raise cursor_refusal()
    last_filter=meaningful() if view=='conversation' else 'NOT m.deleted'
    last_sql=f'''SELECT m.sent_at,m.id,m.direction,left(m.body,160) AS snippet,
        CASE WHEN a.platform='telegram' THEN {number('m')} ELSE 0::bigint END AS numeric_id FROM messages m
        LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
        WHERE m.account=c.account AND m.chat=c.id AND {last_filter}'''
    found=None
    if page_first:
        found=cq._page_first(c,p,clauses,args,accounts=accounts,limit=limit,last_sql=last_sql,last_filter=last_filter,
            pos=pos,lower=lower,upper=upper,exclude_chats=exclude_chats)
        if found:
            aliases,listed=found['aliases'],found['listed']
        else:
            # Today's per-chat plan, with the catalog's WhatsApp pairs.
            aliases,listed=cq._alias_pairs(accounts,c,p)
            hidden={v['chat']['id']:shown for shown,v in listed.items()}
            if hidden:clauses.append('NOT c.id=ANY(%s)');args.append(list(hidden))
            partners=[hidden[i] for i in exclude_chats or () if i in hidden]
            if partners:clauses.append('NOT c.id=ANY(%s)');args.append(partners)
    age='true' if not awaiting_reply_days else f"attention.sent_at>=CURRENT_TIMESTAMP-interval '{awaiting_reply_days} days'"
    attention_age='true' if not awaiting_reply_days else f"m.sent_at>=CURRENT_TIMESTAMP-interval '{awaiting_reply_days} days'"
    # ANYM-281: the key's snoozes and the principal's mutes. Literals, because the
    # fragments sit inside laterals whose argument order the callers share.
    key=sql_literal(str(p['key_id']))+'::uuid';principal=sql_literal(p['id'])
    queue=awaiting_reply or include_dismissed
    snoozes=queue and c.execute('SELECT EXISTS(SELECT 1 FROM reply_snoozes WHERE key_id=%s) AS x',(p['key_id'],)).fetchone()['x']
    mutes=queue and c.execute('SELECT EXISTS(SELECT 1 FROM reply_mutes WHERE principal=%s) AS x',(p['id'],)).fetchone()['x']
    def snooze_of(message,chat,due):
        return (f"EXISTS(SELECT 1 FROM reply_snoozes sz WHERE sz.key_id={key} AND sz.chat={chat} AND sz.message={message}.id"
            f" AND sz.revision={message}.revision AND sz.snoozed_until{'<=' if due else '>'}CURRENT_TIMESTAMP)")
    if snoozes and awaiting_reply_days:
        # A due snooze returns its hint even when it is older than the age window,
        # only while it still names the chat's latest candidate. The lookup's lower
        # bound moves back to the snoozed message (a private chat only while no newer
        # incoming arrived), so a newer hint stays the target and the bound stays an
        # index condition over the messages since that hint.
        age=f'({age} OR {snooze_of("attention","c.id",True)})'
        attention_age=(f"m.sent_at>=least(CURRENT_TIMESTAMP-interval '{awaiting_reply_days} days',"
            f'(SELECT sm.sent_at FROM reply_snoozes sz JOIN messages sm ON sm.id=sz.message WHERE sz.key_id={key}'
            " AND sz.chat=c.id AND sz.snoozed_until<=CURRENT_TIMESTAMP AND (c.chat_type='group' OR sz.incoming_head=incoming.id)))")
    # The standalone group count has no incoming lateral; for a group the clause is true.
    group_attention_age=attention_age.replace(" AND (c.chat_type='group' OR sz.incoming_head=incoming.id)",'')
    sender_skip=(f'NOT EXISTS(SELECT 1 FROM reply_mutes rm WHERE rm.principal={principal} AND rm.account=m.account'
        ' AND rm.sender=m.sender)') if mutes else 'true'
    chat_muted=f'EXISTS(SELECT 1 FROM reply_mutes rc WHERE rc.principal={principal} AND rc.chat=c.id)'
    # A key dismisses this exact owner-attention item. Unrelated group chatter
    # cannot reopen it when the same credential switches API representations.
    incoming_head="CASE WHEN c.chat_type='group' THEN attention.id ELSE incoming.id END"
    dismissed='(d.message=attention.id AND d.revision=attention.revision AND d.incoming_head=('+incoming_head+'))'
    pending="(attention.id IS NOT NULL AND NOT coalesce(ro.is_self_chat,false) AND NOT coalesce(a.platform='telegram' AND c.provider_chat='777000',false) AND NOT coalesce(attention.is_self_chat,false) AND "+age
    snoozed=snooze_of('attention','c.id',False)[:-1]+' AND sz.incoming_head=('+incoming_head+'))'
    if not include_dismissed:
        pending+=' AND NOT coalesce('+dismissed+',false)'
        if snoozes:pending+=' AND NOT '+snoozed
    pending+=')'
    # The muted count reads the queue without the chat-mute exclusion.
    unmuted_pending=pending
    if mutes and not include_dismissed and not include_muted:pending=pending[:-1]+' AND NOT '+chat_muted+')'
    read_sources,fresh,unread=read_state_sql(c)
    # Waiting points grow with the square root of hours (1h=1, 1d=4, 4d=9) up to
    # 20 at about 17 days, so recent requests differ before a day has passed.
    waited="greatest(0,extract(epoch FROM (score_clock.at-attention.sent_at))/3600)"
    waiting="least(20,floor(sqrt("+waited+"))::integer)"
    # Each component is a CASE yielding its points or NULL; the score is their sum and the reasons their keys.
    own_sender="coalesce((score_clock.owners->(a.platform||'|'||attention.sender))-c.account<>'[]'::jsonb,false)"
    person_mention=f"c.chat_type='group' AND attention.basis='owner_mention' AND NOT coalesce(attention.bot,false) AND NOT coalesce(attention.business,false) AND NOT {own_sender}"
    def demotion(code,condition):return code,f'CASE WHEN {condition} THEN {DEMOTIONS[code]} END'
    components=[('private_chat',"CASE WHEN c.chat_type='private' THEN 100 END"),
        ('group_attention',"CASE WHEN c.chat_type IS DISTINCT FROM 'private' THEN 25 END"),
        ('question_mark','CASE WHEN attention.question THEN 25 END'),
        ('provider_contact','CASE WHEN attention.known_contact THEN 10 END'),
        ('owner_question',f"CASE WHEN {person_mention} AND attention.question THEN {OWNER_QUESTION} END"),
        # A closing courtesy ("@owner thanks") is not a request (ANYM-427).
        ('owner_request',f"CASE WHEN {person_mention} AND NOT attention.question AND NOT coalesce(attention.courtesy,false) THEN {OWNER_REQUEST} END"),
        ('waiting_age_capped',f'CASE WHEN {waiting}>=20 THEN 20 END'),
        ('waiting_age',f'CASE WHEN {waiting} BETWEEN 1 AND 19 THEN {waiting} END'),
        demotion('one_way_chat',"c.chat_type='private' AND own_last.sent_at IS NULL"),
        demotion('provider_business_or_verified','coalesce(attention.business,false)'),
        demotion('observed_bot',"c.chat_type='group' AND coalesce(attention.bot,false)"),
        demotion('own_account',own_sender),
        demotion('closing_courtesy','coalesce(attention.courtesy,false)')]
    score='('+'+'.join(f'coalesce({sql},0)' for _,sql in components)+')'
    broadcast="(c.chat_type='private' AND own_last.sent_at IS NULL OR coalesce(attention.business,false))"
    # ANYM-427: one-way private chat from a non-contact that is a business or verified sender or writes on several UTC days.
    # Each step seeks the latest meaningful incoming message before the UTC day of the step before it
    # (message_reply_order), so the day count reads PROMOTIONAL_DAYS index probes, not the mailing history.
    day_walk="'infinity'::timestamptz"
    for step in range(PROMOTIONAL_DAYS):
        before=day_walk if not step else f"date_trunc('day',{day_walk} AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
        day_walk=f'''(SELECT pm{step}.sent_at FROM messages pm{step} LEFT JOIN message_metadata pmd{step}
          ON pmd{step}.message=pm{step}.id AND pmd{step}.revision=pm{step}.revision
          WHERE pm{step}.account=c.account AND pm{step}.chat=c.id AND pm{step}.direction='incoming'
          AND pm{step}.sent_at<{before} AND {meaningful(f'pm{step}',f'pmd{step}')} ORDER BY pm{step}.sent_at DESC LIMIT 1)'''
    likely_promotional=f'''(c.chat_type='private' AND own_last.sent_at IS NULL AND NOT coalesce(attention.known_contact,false)
        AND (coalesce(attention.business,false) OR {day_walk} IS NOT NULL))'''
    promotional_filter={'exclude':'NOT '+likely_promotional,'only':likely_promotional}.get(promotional) if awaiting_reply else None
    if awaiting_reply:clauses.append(pending)
    if only_questions and not awaiting_their_reply:clauses.append('attention.question')
    if (awaiting_reply or only_questions and not awaiting_their_reply) and awaiting_reply_days:
        # Attention needs a recent incoming message. One indexed probe per chat
        # skips the attention, head and owner-thread lookups for quiet chats,
        # which are most of a large catalogue, before both the count and page.
        # OFFSET 0 keeps it a chat filter instead of a semi-join after them.
        clauses.append(f'''(EXISTS(SELECT 1 FROM messages recent WHERE recent.account=c.account
            AND recent.chat=c.id AND recent.direction='incoming' AND NOT recent.deleted
            AND recent.sent_at>=CURRENT_TIMESTAMP-interval '{awaiting_reply_days} days' OFFSET 0)'''+(
            f''' OR EXISTS(SELECT 1 FROM reply_snoozes sz WHERE sz.key_id={key} AND sz.chat=c.id
            AND sz.snoozed_until<=CURRENT_TIMESTAMP)''' if snoozes else '')+')')
    for bound,op in [(lower,'>='),(upper,'<')]:
        if bound:clauses.append('last.sent_at'+op+'%s');args.append(bound)
    after_owner=f'''(own_last.sent_at IS NULL OR m.sent_at>own_last.sent_at OR
        (a.platform='telegram' AND m.sent_at=own_last.sent_at AND {number('m')}>own_last.numeric_id)
        OR (a.platform='whatsapp' AND m.sent_at=own_last.sent_at))'''
    message_projection=f'''m.id,m.provider_id,m.revision,m.sender,m.sent_at,m.direction,
        CASE WHEN a.platform='telegram' THEN {number('m')} ELSE 0::bigint END AS numeric_id,
        coalesce(nullif(left(m.body,160),''),'['||coalesce(md.metadata->>'media_kind','attachment')||']') AS snippet,
        md.metadata->'is_self_chat'='true'::jsonb AS is_self_chat,
        md.metadata->'sender_is_contact'='true'::jsonb AS known_contact,
        {question_sql('m')} AS question,
        md.metadata->'sender_is_business_or_verified'='true'::jsonb AS business,
        md.metadata->'sender_is_bot'='true'::jsonb AS bot,
        {courtesy_sql('m')} AS courtesy,
        CASE WHEN c.chat_type='private' THEN 'last_archived_message_incoming'
          WHEN md.metadata->'mentions_owner'='true'::jsonb THEN 'owner_mention' ELSE 'reply_to_owner_message' END AS basis'''
    select_message='SELECT '+message_projection+f'''
        FROM messages m LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
        WHERE m.account=c.account AND m.chat=c.id AND {meaningful()}'''
    attention_filter='''(c.chat_type='private' AND NOT coalesce(md.metadata->'sender_is_bot'='true'::jsonb,false)
        OR c.chat_type='group' AND (md.metadata->'mentions_owner'='true'::jsonb
          OR md.metadata->'reply_to_owner'='true'::jsonb OR (md.metadata->>'reply_to_provider_id' IS NOT NULL AND EXISTS(
            SELECT 1 FROM messages quoted WHERE quoted.account=m.account AND quoted.chat=m.chat
              AND NOT quoted.deleted AND quoted.direction='outgoing' AND
              (quoted.provider_id=md.metadata->>'reply_to_provider_id' OR (right(c.provider_chat,5)='@g.us'
                AND left(quoted.provider_id,length('true_'||c.provider_chat||'_'))='true_'||c.provider_chat||'_'
                AND split_part(substr(quoted.provider_id,length('true_'||c.provider_chat||'_')+1),'_',1)
                  =md.metadata->>'reply_to_provider_id')) OFFSET 0))))'''
    attention_after=after_owner
    if view=='conversation':
        attention_after=f"(c.chat_type='private' AND {after_owner})"
    owner_sql=f'''SELECT m.sent_at,m.id,{number('m')} AS numeric_id FROM messages m
        LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
        WHERE m.account=c.account AND m.chat=c.id AND m.direction='outgoing' AND {meaningful()}'''
    def attention_for(skip):
        # A sender mute skips that sender's messages when choosing the hint (ANYM-281).
        criteria=attention_age+(' AND '+skip if skip!='true' else '')
        attention_sql=select_message+" AND m.direction='incoming' AND "+attention_after+' AND '+criteria+' AND '+attention_filter
        if view!='conversation':
            attention_sql+=" AND m.sent_at>=coalesce(own_last.sent_at,'-infinity'::timestamptz)"
        else:
            attention_sql+=" AND m.sent_at>=CASE WHEN c.chat_type='private' THEN coalesce(own_last.sent_at,'-infinity'::timestamptz) ELSE '-infinity'::timestamptz END"
        lookup=latest_sql(attention_sql)
        if view=='conversation':
            lookup='SELECT * FROM (('+lookup+') UNION ALL ('+group_thread_query(
                message_projection,enabled="c.chat_type='group'",criteria=criteria,limit=1)+')) owner_attention'
        return lookup
    attention_lookup=attention_for(sender_skip)
    # Rank 1 is the hint a sender mute skipped, read only for the muted count and view.
    # The skipping hint ranks first only while it meets the request's own row filters,
    # so a row the mute removed stays counted and visible under those filters.
    ranked=ranked_args=None;skipping_lookup=attention_lookup
    def ranked_for(broadcasts=exclude_likely_broadcasts,promotional_rows=promotional_filter):
        kept=[unmuted_pending,*(['attention.question'] if only_questions else []),
            *(['(attention.sender IS NULL OR NOT attention.sender=ANY(%s))'] if exclude_senders is not None else []),
            *(['NOT '+broadcast] if broadcasts else []),*([promotional_rows] if promotional_rows else [])]
        kept=re.sub(r'\battention\.','e.',' AND '.join(kept))
        return ('SELECT * FROM ((SELECT e.*,0 AS mute_rank FROM ('+skipping_lookup+') e WHERE '+kept+') UNION ALL (SELECT r.*,1 FROM ('+
            attention_for('true')+') r)) ranked ORDER BY mute_rank LIMIT 1')
    if mutes and awaiting_reply:
        ranked_args=[exclude_senders] if exclude_senders is not None else []
        ranked=ranked_for()
    if include_muted and ranked:attention_lookup=ranked
    muted_flag=f"CASE WHEN {chat_muted} THEN 'chat' WHEN attention.mute_rank=1 THEN 'sender' END"
    owner_lookup=latest_sql(owner_sql)
    if aliases:
        # The owner's latest message in either chat of a conversation (ANYM-278).
        alias_owner=latest_sql(owner_sql.replace('m.chat=c.id',"m.chat=(score_clock.aliases->>c.id)"))
        owner_lookup=('SELECT * FROM (('+owner_lookup+') UNION ALL ('+alias_owner+
            ')) owner_heads ORDER BY sent_at DESC,numeric_id DESC,id DESC LIMIT 1')
    # ANYM-810: a private chat closes with a courtesy when X, the latest of its last COURTESY_DEPTH meaningful incoming
    # messages that is neither a courtesy nor a body-less sticker, is followed by an owner message, no owner message from
    # X on has question punctuation, and only courtesies or stickers came in after it. Both chats of a WhatsApp pair
    # count. Each lookup reads at most COURTESY_SCAN stored rows per chat; whatever it cannot prove keeps the chat.
    # Read only for exclude and only.
    closing=awaiting_reply and courtesies!='include'
    closing_laterals=courtesy_closed=''
    if closing:
        alias_chat="m.chat=(score_clock.aliases->>c.id)"
        def pair(sql):return [sql,*([sql.replace('m.chat=c.id',alias_chat)] if aliases else [])]
        def tagged(sqls):return ' UNION ALL '.join(f'SELECT {n} AS b,t.* FROM ({sql}) t' for n,sql in enumerate(sqls))
        stored=f'''SELECT m.id,m.revision,m.deleted,m.body,m.sent_at,CASE WHEN a.platform='telegram' THEN {number('m')} ELSE 0::bigint END
              AS numeric_id FROM messages m WHERE m.account=c.account AND m.chat=c.id AND c.chat_type='private' AND m.direction='incoming' AND NOT m.deleted
            ORDER BY m.sent_at DESC,{index_number('m')} DESC NULLS LAST,m.id DESC LIMIT {COURTESY_SCAN}'''
        meta=metadata_sql('p')
        # Each tail is one range of message_reply_order in its full key order, whatever the platform's tie rules, so
        # it reads at most COURTESY_SCAN rows. A tail that filled the scan is known only after its oldest scanned
        # timestamp (the horizon): rows at it may tie with unread ones, so they are dropped too.
        closing_from=f'''WITH tails AS MATERIALIZED ({tagged(pair(stored))}),
          horizon AS (SELECT max(oldest) AS at FROM (SELECT min(sent_at) AS oldest FROM tails GROUP BY b
            HAVING count(*)>={COURTESY_SCAN}) full_tail),
          recent AS (SELECT p.id,p.sent_at,p.numeric_id,coalesce({courtesy_sql('p')} OR length(btrim(p.body))=0
              AND ({meta}->>'media_kind'='sticker' OR {meta}->>'message_kind'='sticker'),false) AS closer
            FROM tails p LEFT JOIN message_metadata md ON md.message=p.id AND md.revision=p.revision
            WHERE p.sent_at>coalesce((SELECT at FROM horizon),'-infinity'::timestamptz) AND {meaningful_sql('p','md')}
            ORDER BY p.sent_at DESC,p.numeric_id DESC,p.id DESC LIMIT {COURTESY_DEPTH})
          SELECT id,sent_at,numeric_id FROM recent WHERE NOT closer ORDER BY sent_at DESC,numeric_id DESC,id DESC LIMIT 1'''
        # The owner's stored messages from X on; one row more than the scan in either chat leaves the rest unread.
        owner_stored=f'''SELECT m.id,m.revision,m.deleted,m.body,m.sent_at,{number('m')} AS numeric_id FROM messages m
            WHERE m.account=c.account AND m.chat=c.id AND m.direction='outgoing' AND NOT m.deleted
              AND m.sent_at>=closing_from.sent_at ORDER BY m.sent_at DESC,{index_number('m')} DESC NULLS LAST,m.id DESC
            LIMIT {COURTESY_SCAN+1}'''
        # A WhatsApp owner message at X's own second is not proven after X, so it never answers (it may still ask).
        answered="p.sent_at>closing_from.sent_at OR a.platform='telegram' AND p.sent_at=closing_from.sent_at AND p.numeric_id>closing_from.numeric_id"
        closing_owner=f'''WITH owned AS MATERIALIZED ({tagged(pair(owner_stored))})
          SELECT coalesce((SELECT max(k) FROM (SELECT count(*) AS k FROM owned GROUP BY b) per_chat),0) AS scanned,
            count(*) FILTER(WHERE {answered}) AS answered,coalesce(bool_or({question_sql('p')}),false) AS asked
          FROM owned p LEFT JOIN message_metadata md ON md.message=p.id AND md.revision=p.revision WHERE {meaningful_sql('p','md')}'''
        closing_laterals=f'''
      LEFT JOIN LATERAL ({closing_from}) closing_from ON true
      LEFT JOIN LATERAL ({closing_owner}) closing_owner ON true'''
        courtesy_closed=(f"(c.chat_type='private' AND closing_from.id IS NOT NULL AND closing_owner.answered>0"
            f" AND closing_owner.scanned<={COURTESY_SCAN} AND NOT closing_owner.asked)")
    # The broadcast and promotional counts run before the courtesy filter, so they never read its lookups.
    def base_for(attention_lookup,closing=True):return f''' FROM chats c JOIN accounts a ON a.id=c.account
      CROSS JOIN (SELECT %s::timestamptz AS at,%s::jsonb AS owners,%s::jsonb AS aliases) score_clock
      LEFT JOIN LATERAL ({latest_sql(last_sql,both_directions=True)}) last ON true
      LEFT JOIN LATERAL ({owner_lookup}) own_last ON true
      LEFT JOIN LATERAL ({latest_sql(select_message+" AND m.direction='incoming'")}) incoming ON true{closing_laterals if closing else ''}
      LEFT JOIN chat_read_observations ro ON ro.account=c.account AND ro.chat=c.id
      LEFT JOIN reply_dismissals d ON d.key_id=%s AND d.chat=c.id
      LEFT JOIN LATERAL ({attention_lookup}) attention ON true
      LEFT JOIN sender_profiles sp ON sp.account=c.account AND sp.chat=c.id AND sp.sender=attention.sender WHERE '''
    base=base_for(attention_lookup)
    base_args=[score_at,owner_senders(c,p),Jsonb(aliases),p['key_id']]
    ranked_base_args=[*base_args,*(ranked_args or [])]
    if attention_lookup is ranked:base_args=ranked_base_args
    # A first queue page counts the same filtered rows it sorts, in one pass.
    inline_totals=awaiting_reply and not only_unread and not after
    excluded=None
    if exclude_likely_broadcasts:
        # Counted in the caller's scope and filters only, then removed from the rows, the totals and the cursor.
        excluded=c.execute('SELECT count(*) AS n'+base_for(ranked_for(promotional_rows=None) if attention_lookup is ranked else attention_lookup,False)+' AND '.join([*clauses,broadcast,*([unread+' IS TRUE'] if only_unread else [])]),[*base_args,*args]).fetchone()['n']
        clauses.append('NOT '+broadcast)
    excluded_promotional=None
    if promotional=='exclude' and awaiting_reply:
        # Counted before the broadcast filter, also in the muted hint choice, so each total is what its own filter removes (ANYM-427).
        excluded_promotional=c.execute('SELECT count(*) AS n'+base_for(ranked_for(broadcasts=False) if attention_lookup is ranked else attention_lookup,False)+' AND '.join([*(v for v in clauses if v!='NOT '+broadcast),
            likely_promotional,*([unread+' IS TRUE'] if only_unread else [])]),[*base_args,*args]).fetchone()['n']
    if promotional_filter:clauses.append(promotional_filter)
    excluded_courtesies=None
    if closing:
        if courtesies=='exclude':
            # Counted after every other filter (promotional included), so the shown and left-out totals add up to include's.
            excluded_courtesies=c.execute('SELECT count(*) AS n'+base+' AND '.join([*clauses,courtesy_closed,
                *([unread+' IS TRUE'] if only_unread else [])]),[*base_args,*args]).fetchone()['n']
        clauses.append(('NOT ' if courtesies=='exclude' else '')+courtesy_closed)
    totals=c.execute('SELECT count(*) AS n,count(*) FILTER(WHERE '+unread+' IS NULL) AS unknown'+base+
        ' AND '.join(clauses),[*base_args,*args]).fetchone() if awaiting_reply and not inline_totals else None
    if only_unread:clauses.append(unread+' IS TRUE')
    total=c.execute('SELECT count(*) AS n'+base+' AND '.join(clauses),[*base_args,*args]).fetchone()['n'] if awaiting_reply and only_unread else (totals['n'] if totals else None)
    # Reported only while the principal has a mute, so other queue responses keep their shape.
    muted_total=None
    if ranked:
        muted_total=c.execute('SELECT count(*) AS n'+base_for(ranked)+' AND '.join([*(unmuted_pending if v==pending else v for v in clauses),
            '('+chat_muted+' OR attention.mute_rank=1)']),[*ranked_base_args,*args]).fetchone()['n']
    unknown_roles=None
    if role_clauses:
        # An empty filtered list must not read as "no such channels": count the Telegram channels and groups
        # the other filters keep whose role is not observed yet (ANYM-533).
        unknown_roles=c.execute('SELECT count(*) AS n'+base+' AND '.join([*(v for v in clauses if v not in role_clauses),
            role_sql+"='unknown' AND c.chat_type IN ('group','channel') AND a.platform='telegram'"]),
            [*base_args,*args]).fetchone()['n']
    waits=None
    if awaiting_their_reply:
        # ANYM-537: one statement reads the candidates once and takes the totals and the page from that one
        # snapshot, so a page row left out later (owner_waits.finish) is always one the totals counted.
        waits_mutes=c.execute('SELECT EXISTS(SELECT 1 FROM reply_mutes WHERE principal=%s) AS x',(p['id'],)).fetchone()['x']
        rules=owner_waits.conditions(aliases=aliases,min_wait_hours=min_wait_hours,days=awaiting_reply_days,
            principal=p['id'],mutes=waits_mutes)
        shown='w.muted IS NULL' if rules['muted'] and not include_muted else 'true'
        candidates=('WITH waits_all AS MATERIALIZED (SELECT c.id AS chat_id,ws.sent_at AS since,wh.id AS last_id,'
            'wh.sent_at AS last_at,wp.sent_at AS peer_at,wp.numeric_id AS peer_numeric,'+(rules['muted'] or 'NULL::text')+
            ' AS muted,wl.listed,'+('CASE WHEN wl.listed THEN '+rules['question']+' ELSE false END' if only_questions else 'wl.listed')+
            ' AS asked,'+rules['uncertain']+' AS uncertain FROM chats c JOIN accounts a ON a.id=c.account'
            ' CROSS JOIN (SELECT %s::jsonb AS owners,%s::jsonb AS aliases) score_clock'
            ' LEFT JOIN chat_read_observations ro ON ro.account=c.account AND ro.chat=c.id'+owner_waits.laterals(c,aliases)+
            ' CROSS JOIN LATERAL (SELECT '+rules['listed']+' AS listed) wl WHERE '+' AND '.join([*clauses,rules['chat']])+'),'
            f''' waits_totals AS (SELECT count(*) FILTER(WHERE w.listed AND w.asked AND {shown}) AS listed,
              count(*) FILTER(WHERE w.uncertain AND {shown}) AS uncertain,
              count(*) FILTER(WHERE w.listed AND NOT w.asked AND {shown}) AS without_question,
              count(*) FILTER(WHERE w.listed AND w.asked AND w.muted IS NOT NULL) AS muted FROM waits_all w)''')
        waits={'rules':rules,'candidates':candidates,'shown':shown,'mutes':waits_mutes,'cursor':None,
            'args':[owner_senders(c,p),Jsonb(aliases),*args]}
    if after:
        cid=valid_text(after,empty=False) if order=='id' else position_text(pos.get('id'))
        if not c.execute('SELECT 1 FROM chats c WHERE c.id=%s AND '+scope,[cid,*scope_filter(p,'c',chat_column='id')[1]]).fetchone():raise APIError('not_found',404)
        if order=='id':clauses.append('c.id>%s');args.append(cid)
        else:
            at=None if pos['at'] is None and order=='activity' else position_date(pos['at'])
            if order=='activity':
                clauses.append("(coalesce(last.sent_at,'-infinity'),c.id)<(coalesce(%s::timestamptz,'-infinity'),%s)");args.extend([at,cid])
            elif order=='waiting_longest':waits['cursor']=[at,cid]
            else:
                clauses.append('(-'+score+',attention.sent_at,c.id)>(-%s,%s,%s)');args.extend([pos['score'],at,cid])
    sort={'id':'c.id','activity':"coalesce(last.sent_at,'-infinity') DESC,c.id DESC",
        'priority':score+' DESC,attention.sent_at,c.id','waiting_longest':'c.wait_since,c.id'}[order]
    prefix='';where=' AND '.join(clauses);query_args=[*base_args,*args,limit+1]
    if not awaiting_reply and not only_questions and not only_unread and exclude_senders is None and (
            order=='activity' or order=='id' and lower is None and upper is None):
        # Catalog ordering needs only the last head. Owner/reply/read evidence
        # belongs to the returned page, not every permitted chat before LIMIT.
        page_from=' FROM chats c'
        if order=='activity':
            page_from+=' JOIN accounts a ON a.id=c.account LEFT JOIN LATERAL ('+latest_sql(last_sql,both_directions=True)+') last ON true'
        prefix='WITH chat_page AS MATERIALIZED (SELECT c.*'+page_from+' WHERE '+where+' ORDER BY '+sort+' LIMIT %s) '
        query_args=[*args,limit+1,*base_args,limit+1]
        if found:
            # ANYM-878: the page is chosen over the candidates and proven in this one statement's snapshot.
            prefix=('WITH chat_page AS MATERIALIZED (SELECT c.*,last.sent_at AS page_head'+page_from+' WHERE '+where+
                ' AND c.id=ANY(%s) AND NOT c.id=ANY(%s) ORDER BY '+sort+' LIMIT %s), page_proof AS MATERIALIZED (SELECT count(page_head)>%s AND '+
                found['proof']+' AS proven FROM chat_page) ')
            query_args=[*args,found['found'],found['excluded'],limit+1,limit,*found['proof_args'],*base_args,limit+1]
        base=base.replace(' FROM chats c JOIN accounts a',' FROM chat_page c JOIN accounts a',1)
        where='true'
    if waits:
        # The page is chosen over the run lookups alone; the row's head, read and hint evidence is read for it only.
        # One statement picks the page and counts the totals; the row query then reads only the picked chats.
        picked=c.execute(waits['candidates']+''', picked AS (SELECT w.chat_id,w.since,w.last_id,w.last_at,w.peer_at,
            w.peer_numeric,w.muted FROM waits_all w WHERE w.listed AND w.asked AND '''+waits['shown']+
            (' AND (w.since,w.chat_id)>(%s::timestamptz,%s)' if waits['cursor'] else '')+''' ORDER BY w.since,w.chat_id LIMIT %s)
            SELECT (SELECT to_jsonb(t) FROM waits_totals t) AS totals,
              coalesce((SELECT jsonb_agg(to_jsonb(k) ORDER BY k.since,k.chat_id) FROM picked k),'[]'::jsonb) AS page''',
            [*waits['args'],*(waits['cursor'] or []),limit+1]).fetchone()
        waits['totals']=dict(picked['totals'])
        prefix='''WITH chat_page AS MATERIALIZED (SELECT c.*,w.since AS wait_since,w.last_id AS wait_last_id,
            w.last_at AS wait_last_at,w.peer_at AS wait_peer_at,w.peer_numeric AS wait_peer_numeric,w.muted AS wait_muted
            FROM jsonb_to_recordset(%s::jsonb) AS w(chat_id text,since timestamptz,last_id text,last_at timestamptz,
              peer_at timestamptz,peer_numeric bigint,muted text) JOIN chats c ON c.id=w.chat_id) '''
        base=base.replace(' FROM chats c JOIN accounts a',' FROM chat_page c JOIN accounts a',1)
        query_args=[Jsonb(picked['page']),*base_args,limit+1];where='true'
    rows=c.execute(prefix+'''SELECT c.id,c.account,c.title,c.provider_chat AS provider_chat_id,c.chat_type AS type,
        '''+role_sql+''' AS my_role,'''+('c.can_post,c.role_observed_at,' if roles_ready else
          'NULL::boolean AS can_post,NULL::timestamptz AS role_observed_at,')+'''
        last.sent_at AS last_activity,last.id AS last_message_id,last.direction AS last_message_direction,
        last.snippet AS last_message_snippet,ro.unread_count AS provider_unread_count,ro.unread_count AS unread_count,
        '''+unread+''' AS provider_unread,ro.observed_at AS read_state_observed_at,ro.basis AS read_state_basis,
        CASE WHEN '''+fresh+''' THEN 'observed' WHEN ro.observed_at IS NULL THEN 'unknown' ELSE 'stale' END AS read_state,
        CASE '''+('''WHEN attention.id IS NOT NULL AND EXISTS(SELECT 1 FROM owner_message_reads omr
            WHERE omr.chat=c.id AND omr.provider_id=attention.provider_id) THEN true
          ''' if read_sources else '')+'''WHEN '''+fresh+''' AND attention.id IS NOT NULL THEN
          CASE WHEN ro.read_inbox_max_id IS NOT NULL AND attention.provider_id ~ '^[0-9]{1,18}$'
            THEN attention.provider_id::bigint<=ro.read_inbox_max_id
          END END AS read_by_owner,'''+('ro.count_basis AS read_state_count_basis,ro.marked_unread AS read_state_marked_unread,'
            if read_sources else 'NULL::text AS read_state_count_basis,NULL::boolean AS read_state_marked_unread,')+'''
        NULL::boolean AS muted,NULL::boolean AS archived,'''+('false' if waits else pending)+''' AS awaiting_reply,
        '''+(muted_flag if include_muted and ranked else 'NULL::text')+''' AS awaiting_reply_muted,
        '''+(f'''(SELECT sz.snoozed_until FROM reply_snoozes sz WHERE sz.key_id={key} AND sz.chat=c.id
          AND sz.message=attention.id AND sz.revision=attention.revision AND sz.incoming_head=({incoming_head})
          AND sz.snoozed_until<=CURRENT_TIMESTAMP)''' if snoozes else 'NULL::timestamptz')+''' AS awaiting_reply_snoozed_until,
        attention.basis AS awaiting_reply_basis,attention.id AS awaiting_reply_message_id,
        attention.revision AS awaiting_reply_revision,attention.provider_id AS awaiting_reply_provider_message_id,
        attention.sent_at AS awaiting_reply_sent_at,attention.snippet AS awaiting_reply_snippet,
        attention.sender AS awaiting_reply_sender,sp.display_name AS awaiting_reply_sender_name,
        sp.username AS awaiting_reply_username,attention.question AS awaiting_reply_has_question,
        attention.known_contact AS awaiting_reply_known_contact,'''+score+''' AS awaiting_reply_priority,
        '''+(f"CASE WHEN {likely_promotional} THEN CASE WHEN coalesce(attention.business,false) THEN 'business_sender' ELSE 'repeated_days' END END"
          if awaiting_reply and promotional!='exclude' else 'NULL::text')+''' AS awaiting_reply_promotional_basis,
        array_remove(ARRAY['''+','.join(f"CASE WHEN {sql} IS NOT NULL THEN '{code}' END" for code,sql in components)+'''],NULL)
          AS awaiting_reply_priority_reasons,jsonb_strip_nulls(jsonb_build_object('''+','.join(f"'{code}',{sql}" for code,sql in components)+'''))
          AS awaiting_reply_priority_components,floor('''+waited+''')::integer AS awaiting_reply_waiting_hours,
        '''+incoming_head+''' AS _incoming_head,own_last.sent_at AS _own_sent_at,own_last.numeric_id AS _own_numeric_id,
        a.platform AS _platform,coalesce('''+dismissed+''',false) AS awaiting_reply_dismissed,
        CASE WHEN a.platform='whatsapp' AND attention.sent_at=own_last.sent_at THEN
          'ambiguous_message_order' ELSE 'heuristic_not_obligation' END AS awaiting_reply_confidence'''+(
        ''',c.wait_since AS _wait_since,c.wait_last_id AS _wait_last_id,c.wait_last_at AS _wait_last_at,
        c.wait_peer_at AS _wait_peer_at,c.wait_peer_numeric AS _wait_peer_numeric,c.wait_muted AS _wait_muted''' if waits else '')+(
        ',count(*) OVER() AS _total,count(*) FILTER(WHERE '+unread+' IS NULL) OVER() AS _unknown' if inline_totals else '')+(
        ',(SELECT proven FROM page_proof) AS _page_proof' if found else '')+base+where+
        ' ORDER BY '+sort+' LIMIT %s',query_args).fetchall()
    return rows
