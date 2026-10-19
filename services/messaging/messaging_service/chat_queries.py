"""Scoped catalogue and evidence-backed, deliberately heuristic reply queue."""
from psycopg.types.json import Jsonb

from .policy import scope_filter,targets
from .search_query import coverage,cursor_binding,decode_cursor,encode_cursor,filter_values,valid_text
from .conversation import meaningful_sql,options
from .conversation_state import conversation_state,group_thread_query


def meaningful(alias='m',metadata='md'):
    return meaningful_sql(alias,metadata)


def number(alias):
    return 'coalesce('+index_number(alias)+',0::bigint)'


def index_number(alias):
    # Exact expression from migration 27; provider selection stays outside the
    # indexed ORDER BY, and WhatsApp never uses this as chronology.
    return f"CASE WHEN {alias}.provider_id ~ '^[0-9]{{1,18}}$' THEN {alias}.provider_id::bigint END"


def latest_sql(sql,*,both_directions=False,platform='a.platform',limit=1):
    """Use a bounded indexed tail per provider and direction, then merge heads.

    Keeping platform selection outside the indexed ORDER BY is essential: a
    per-message accounts subquery or CASE there forces sorting complete chats.
    """
    branches=[]
    directions=('incoming','outgoing') if both_directions else (None,)
    for direction in directions:
        selected=sql+(" AND m.direction='"+direction+"'" if direction else '')
        for telegram in (True,False):
            selected_platform=platform+('=' if telegram else '<>')+"'telegram'"
            order="m.sent_at DESC,"+(index_number('m')+' DESC NULLS LAST,' if telegram else '')+'m.id DESC'
            eligible=selected+' AND '+selected_platform
            tail=eligible+' ORDER BY '+order+' LIMIT '+str(limit)
            if telegram:
                # Legacy/synthetic IDs may be nonnumeric. They and numeric zero
                # share the historical ID tie breaker, whereas the index puts
                # SQL NULL after zero. Revisit only zero-valued timestamps when
                # such an edge reaches the indexed tail; normal IDs stay bounded.
                tail='''WITH direction_tail AS MATERIALIZED ('''+tail+'''),
                    zero_times AS MATERIALIZED (SELECT DISTINCT sent_at FROM direction_tail WHERE numeric_id=0)
                    SELECT * FROM (SELECT * FROM direction_tail WHERE numeric_id<>0 UNION ALL ('''+eligible+'''
                      AND EXISTS(SELECT 1 FROM zero_times) AND m.sent_at IN (SELECT sent_at FROM zero_times)
                      AND '''+number('m')+'''=0 ORDER BY m.sent_at DESC,m.id DESC LIMIT '''+str(limit)+''')) repaired
                    ORDER BY sent_at DESC,numeric_id DESC,id DESC LIMIT '''+str(limit)
            branches.append('('+tail+')')
    # There is at most one live platform branch per direction. This outer sort
    # combines at most two message heads, never a chat's retained history.
    return 'SELECT * FROM ('+' UNION ALL '.join(branches)+') candidate ORDER BY sent_at DESC,coalesce(numeric_id,0) DESC,id DESC LIMIT '+str(limit)


def raw_title_sql(chat='c'):
    """A private chat titled only by its provider ID (for example a WhatsApp LID)."""
    return f"({chat}.chat_type='private' AND ({chat}.title='' OR {chat}.title={chat}.provider_chat))"


def peer_name_sql(chat='c'):
    # Latest provider-reported name of an incoming sender in this exact chat;
    # a label for reading, never identity proof. The owner's profile is excluded.
    return f"""(SELECT peer.display_name FROM sender_profiles peer JOIN messages peer_source
          ON peer_source.id=peer.source_message
        WHERE peer.account={chat}.account AND peer.chat={chat}.id AND peer.display_name<>''
          AND peer_source.direction='incoming' ORDER BY peer.source_sent_at DESC,peer.id LIMIT 1)"""


SELF_CHAT_TITLE = 'Saved messages'


def self_chat_sql(chat='c'):
    # The provider marks the owner's own chat on the chat observation or on its
    # recent messages (a WhatsApp LID self chat is only known per message).
    return f"""(EXISTS(SELECT 1 FROM chat_read_observations self_ro WHERE self_ro.account={chat}.account
          AND self_ro.chat={chat}.id AND self_ro.is_self_chat) OR EXISTS(SELECT 1 FROM (SELECT recent.id,recent.revision
          FROM messages recent WHERE recent.account={chat}.account AND recent.chat={chat}.id
          ORDER BY recent.sent_at DESC LIMIT 20) recent JOIN message_metadata self_md ON self_md.message=recent.id
          AND self_md.revision=recent.revision WHERE self_md.metadata->'is_self_chat'='true'::jsonb))"""


def title_sql(chat='c'):
    # ANYM-361: self-chat evidence labels a private chat whatever its stored title;
    # a group or channel is never relabelled by stray message metadata.
    return f"""CASE WHEN {chat}.chat_type='private' AND {self_chat_sql(chat)} THEN '{SELF_CHAT_TITLE}'
        WHEN {raw_title_sql(chat)} THEN coalesce({peer_name_sql(chat)},{chat}.title) ELSE {chat}.title END"""


# ANYM-276: one fixed integer per component; a row's priority is the sum of its non-zero components.
DEMOTIONS={'one_way_chat':-60,'provider_business_or_verified':-40,'own_account':-80,'closing_courtesy':-30,'observed_bot':-20}
# A person's question that mentions the owner in a group outranks a demoted one-way private mailing (owner decision 2026-10-19).
OWNER_QUESTION=40
COURTESY_WORDS=('спасибо','спс','благодарю','ок','окей','хорошо','ясно','понятно','понял','поняла','принято','отлично','супер',
    'взаимно','пожалуйста','thanks','thank you','thx','ty','ok','okay','noted','got it','great','perfect','cool','appreciated',
    'terima kasih','makasih','trims','oke','siap','baik','sip','mantap','ya','iya')
COURTESY_FILLER=('большое','огромное','вам','тебе','you','so much','very much','banyak','sangat')
COURTESY_EMOJI=r'\U0001F300-\U0001FAFF☀-➿⭐️‍'


def courtesy_pattern():
    """A whole short message of closing words and emoji, never a question (the word list is documented with the queue weights)."""
    word='(?:'+'|'.join(sorted(COURTESY_WORDS,key=len,reverse=True))+')'
    filler='(?:'+'|'.join(sorted(COURTESY_FILLER,key=len,reverse=True))+')'
    tail=r'[\s,.!)\-'+COURTESY_EMOJI+']'
    return (r'^\s*(?:'+filler+r'\s+)*'+word+'(?:'+tail+'+(?:'+word+'|'+filler+'))*'+tail+'*$|'+
        r'^\s*['+COURTESY_EMOJI+']['+COURTESY_EMOJI+r'\s]*$')


def sql_literal(value):
    # Constants only (never request values); a quote is doubled and no percent sign is allowed by psycopg.
    assert '%' not in value
    return "'"+value.replace("'","''")+"'"


def owner_senders(c,p):
    """Sender IDs seen on recent outgoing messages the key may read: {platform|sender: [account, ...]}.

    The key's account and chat scope applies before the 20-message limit, so a chat it cannot read never changes a score.
    """
    scope,args=scope_filter(p,'m')
    owners={}
    for r in c.execute(f'''SELECT DISTINCT a.platform||'|'||o.sender AS k,a.id AS account FROM accounts a
        CROSS JOIN LATERAL (SELECT m.sender FROM messages m WHERE m.account=a.id AND {scope} AND m.direction='outgoing'
          AND NOT m.deleted AND m.sender<>'' ORDER BY m.sent_at DESC,m.id DESC LIMIT 20) o WHERE a.id=ANY(%s)''',
        (*args,p['accounts'])):owners.setdefault(r['k'],[]).append(r['account'])
    return Jsonb(owners)


def list_chats(store,c,p,after='',limit=25,*,accounts=None,query='',chat_types=None,
        order='id',exclude_channels=False,awaiting_reply=False,from_time='',to_time='',
        include_coverage=True,awaiting_reply_days=14,exclude_chats=None,exclude_senders=None,
        only_questions=False,only_unread=False,selected_chat=None,include_dismissed=False,
        view='archive',exclude_likely_broadcasts=False):
    from .store import APIError,bounded,date_bound,serial
    bounded(limit);valid_text(after,4096);valid_text(query,200)
    options(view)
    if order=='last_activity_desc':order='activity'
    if order not in ('id','activity','priority') or any(type(v) is not bool for v in
            (exclude_channels,awaiting_reply,include_coverage,only_questions,only_unread,exclude_likely_broadcasts)):
        raise APIError('invalid_filter')
    if type(awaiting_reply_days) is not int or not 0<=awaiting_reply_days<=365:
        raise APIError('invalid_filter')
    if awaiting_reply and order=='id':order='activity'
    if (order=='priority' or exclude_likely_broadcasts) and not awaiting_reply:raise APIError('invalid_filter')
    c.execute('SET LOCAL jit=off')
    accounts=filter_values(accounts);chat_types=filter_values(chat_types)
    exclude_chats=filter_values(exclude_chats);exclude_senders=filter_values(exclude_senders)
    if chat_types is not None and any(t not in ('private','group','channel','unknown') for t in chat_types):
        raise APIError('invalid_filter')
    lower,upper=date_bound(from_time),date_bound(to_time)
    if lower and upper and lower>=upper:raise APIError('invalid_date_range')
    scope,args=scope_filter(p,'c',chat_column='id');clauses=[scope]
    if selected_chat:clauses.append('c.id=%s');args.append(selected_chat)
    if accounts is not None:clauses.append('c.account=ANY(%s)');args.append(accounts)
    if query:
        pattern='%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        clauses.append("(c.title ILIKE %s ESCAPE E'\\\\' OR "+raw_title_sql()+" AND "+peer_name_sql()+" ILIKE %s ESCAPE E'\\\\')")
        args.extend([pattern,pattern])
    if chat_types is not None:clauses.append("coalesce(c.chat_type,'unknown')=ANY(%s)");args.append(chat_types)
    if exclude_channels:clauses.append("c.chat_type IS DISTINCT FROM 'channel'")
    if exclude_chats is not None:clauses.append('NOT c.id=ANY(%s)');args.append(exclude_chats)
    if exclude_senders is not None:
        clauses.append('(attention.sender IS NULL OR NOT attention.sender=ANY(%s))');args.append(exclude_senders)
    binding=cursor_binding(p,'chats',{'accounts':accounts,'query':query,'types':chat_types,'order':order,
        'exclude_channels':exclude_channels,'awaiting_reply':awaiting_reply,'awaiting_reply_days':awaiting_reply_days,
        'from':lower,'to':upper,'exclude_chats':exclude_chats,'exclude_senders':exclude_senders,
        'only_questions':only_questions,'only_unread':only_unread,
        **({'exclude_likely_broadcasts':True} if exclude_likely_broadcasts else {})})
    if view=='conversation':
        binding=cursor_binding(p,'chats-conversation-v1',{'archive_binding':binding})
    pos=decode_cursor(after,binding) if after and order!='id' else None
    now=c.execute('SELECT CURRENT_TIMESTAMP AS t').fetchone()['t'];score_at=now
    if pos and order=='priority':
        if set(pos)!={'id','at','score','as_of'} or type(pos['score']) is not int:raise APIError('invalid_cursor')
        score_at=date_bound(pos['as_of'])
        if score_at is None or score_at>now:raise APIError('invalid_cursor')
    age='true' if not awaiting_reply_days else f"attention.sent_at>=CURRENT_TIMESTAMP-interval '{awaiting_reply_days} days'"
    attention_age='true' if not awaiting_reply_days else f"m.sent_at>=CURRENT_TIMESTAMP-interval '{awaiting_reply_days} days'"
    # A key dismisses this exact owner-attention item. Unrelated group chatter
    # cannot reopen it when the same credential switches API representations.
    incoming_head="CASE WHEN c.chat_type='group' THEN attention.id ELSE incoming.id END"
    dismissed='(d.message=attention.id AND d.revision=attention.revision AND d.incoming_head=('+incoming_head+'))'
    pending="(attention.id IS NOT NULL AND NOT coalesce(ro.is_self_chat,false) AND NOT coalesce(a.platform='telegram' AND c.provider_chat='777000',false) AND NOT coalesce(attention.is_self_chat,false) AND "+age
    if not include_dismissed:pending+=' AND NOT coalesce('+dismissed+',false)'
    pending+=')'
    fresh="(ro.observed_at>=CURRENT_TIMESTAMP-interval '5 minutes' AND ro.observed_at>=incoming.sent_at AND ro.unread_count IS NOT NULL)"
    unread='CASE WHEN '+fresh+' THEN ro.unread_count>0 END'
    # Waiting points grow with the square root of hours (1h=1, 1d=4, 4d=9) up to
    # 20 at about 17 days, so recent requests differ before a day has passed.
    waited="greatest(0,extract(epoch FROM (score_clock.at-attention.sent_at))/3600)"
    waiting="least(20,floor(sqrt("+waited+"))::integer)"
    # Each component is a CASE yielding its points or NULL; the score is their sum and the reasons their keys.
    own_sender="coalesce((score_clock.owners->(a.platform||'|'||attention.sender))-c.account<>'[]'::jsonb,false)"
    def demotion(code,condition):return code,f'CASE WHEN {condition} THEN {DEMOTIONS[code]} END'
    components=[('private_chat',"CASE WHEN c.chat_type='private' THEN 100 END"),
        ('group_attention',"CASE WHEN c.chat_type IS DISTINCT FROM 'private' THEN 25 END"),
        ('question_mark','CASE WHEN attention.question THEN 25 END'),
        ('provider_contact','CASE WHEN attention.known_contact THEN 10 END'),
        ('owner_question',f"CASE WHEN c.chat_type='group' AND attention.basis='owner_mention' AND attention.question AND NOT coalesce(attention.bot,false) AND NOT coalesce(attention.business,false) AND NOT {own_sender} THEN {OWNER_QUESTION} END"),
        ('waiting_age_capped',f'CASE WHEN {waiting}>=20 THEN 20 END'),
        ('waiting_age',f'CASE WHEN {waiting} BETWEEN 1 AND 19 THEN {waiting} END'),
        demotion('one_way_chat',"c.chat_type='private' AND own_last.sent_at IS NULL"),
        demotion('provider_business_or_verified','coalesce(attention.business,false)'),
        demotion('observed_bot',"c.chat_type='group' AND coalesce(attention.bot,false)"),
        demotion('own_account',own_sender),
        demotion('closing_courtesy','coalesce(attention.courtesy,false)')]
    score='('+'+'.join(f'coalesce({sql},0)' for _,sql in components)+')'
    broadcast="(c.chat_type='private' AND own_last.sent_at IS NULL OR coalesce(attention.business,false))"
    if awaiting_reply:clauses.append(pending)
    if only_questions:clauses.append('attention.question')
    if (awaiting_reply or only_questions) and awaiting_reply_days:
        # Attention needs a recent incoming message. One indexed probe per chat
        # skips the attention, head and owner-thread lookups for quiet chats,
        # which are most of a large catalogue, before both the count and page.
        # OFFSET 0 keeps it a chat filter instead of a semi-join after them.
        clauses.append(f'''EXISTS(SELECT 1 FROM messages recent WHERE recent.account=c.account
            AND recent.chat=c.id AND recent.direction='incoming' AND NOT recent.deleted
            AND recent.sent_at>=CURRENT_TIMESTAMP-interval '{awaiting_reply_days} days' OFFSET 0)''')
    for bound,op in [(lower,'>='),(upper,'<')]:
        if bound:clauses.append('last.sent_at'+op+'%s');args.append(bound)
    after_owner=f'''(own_last.sent_at IS NULL OR m.sent_at>own_last.sent_at OR
        (a.platform='telegram' AND m.sent_at=own_last.sent_at AND {number('m')}>own_last.numeric_id)
        OR (a.platform='whatsapp' AND m.sent_at=own_last.sent_at))'''
    # lower() folds Cyrillic only under a UTF-8 database collation (the postgres image default).
    courtesy=sql_literal(courtesy_pattern())
    message_projection=f'''m.id,m.provider_id,m.revision,m.sender,m.sent_at,m.direction,
        CASE WHEN a.platform='telegram' THEN {number('m')} ELSE 0::bigint END AS numeric_id,
        coalesce(nullif(left(m.body,160),''),'['||coalesce(md.metadata->>'media_kind','attachment')||']') AS snippet,
        md.metadata->'is_self_chat'='true'::jsonb AS is_self_chat,
        md.metadata->'sender_is_contact'='true'::jsonb AS known_contact,
        (position('?' in m.body)>0 OR position('？' in m.body)>0) AS question,
        md.metadata->'sender_is_business_or_verified'='true'::jsonb AS business,
        md.metadata->'sender_is_bot'='true'::jsonb AS bot,
        (length(btrim(m.body)) BETWEEN 1 AND 40 AND position('?' in m.body)=0 AND position('？' in m.body)=0
          AND lower(btrim(m.body)) ~ {courtesy}) AS courtesy,
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
    last_filter=meaningful() if view=='conversation' else 'NOT m.deleted'
    last_sql=f'''SELECT m.sent_at,m.id,m.direction,left(m.body,160) AS snippet,
        CASE WHEN a.platform='telegram' THEN {number('m')} ELSE 0::bigint END AS numeric_id FROM messages m
        LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
        WHERE m.account=c.account AND m.chat=c.id AND {last_filter}'''
    owner_sql=f'''SELECT m.sent_at,m.id,{number('m')} AS numeric_id FROM messages m
        LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
        WHERE m.account=c.account AND m.chat=c.id AND m.direction='outgoing' AND {meaningful()}'''
    attention_sql=select_message+" AND m.direction='incoming' AND "+attention_after+' AND '+attention_age+' AND '+attention_filter
    if view!='conversation':
        attention_sql+=" AND m.sent_at>=coalesce(own_last.sent_at,'-infinity'::timestamptz)"
    else:
        attention_sql+=" AND m.sent_at>=CASE WHEN c.chat_type='private' THEN coalesce(own_last.sent_at,'-infinity'::timestamptz) ELSE '-infinity'::timestamptz END"
    attention_lookup=latest_sql(attention_sql)
    if view=='conversation':
        attention_lookup='SELECT * FROM (('+attention_lookup+') UNION ALL ('+group_thread_query(
            message_projection,enabled="c.chat_type='group'",criteria=attention_age,limit=1)+')) owner_attention'
    base=f''' FROM chats c JOIN accounts a ON a.id=c.account
      CROSS JOIN (SELECT %s::timestamptz AS at,%s::jsonb AS owners) score_clock
      LEFT JOIN LATERAL ({latest_sql(last_sql,both_directions=True)}) last ON true
      LEFT JOIN LATERAL ({latest_sql(owner_sql)}) own_last ON true
      LEFT JOIN LATERAL ({latest_sql(select_message+" AND m.direction='incoming'")}) incoming ON true
      LEFT JOIN LATERAL ({attention_lookup}) attention ON true
      LEFT JOIN sender_profiles sp ON sp.account=c.account AND sp.chat=c.id AND sp.sender=attention.sender
      LEFT JOIN chat_read_observations ro ON ro.account=c.account AND ro.chat=c.id
      LEFT JOIN reply_dismissals d ON d.key_id=%s AND d.chat=c.id WHERE '''
    base_args=[score_at,owner_senders(c,p),p['key_id']]
    # A first queue page counts the same filtered rows it sorts, in one pass.
    inline_totals=awaiting_reply and not only_unread and not after
    excluded=None
    if exclude_likely_broadcasts:
        # Counted in the caller's scope and filters only, then removed from the rows, the totals and the cursor.
        excluded=c.execute('SELECT count(*) AS n'+base+' AND '.join([*clauses,broadcast,*([unread+' IS TRUE'] if only_unread else [])]),[*base_args,*args]).fetchone()['n']
        clauses.append('NOT '+broadcast)
    totals=c.execute('SELECT count(*) AS n,count(*) FILTER(WHERE '+unread+' IS NULL) AS unknown'+base+
        ' AND '.join(clauses),[*base_args,*args]).fetchone() if awaiting_reply and not inline_totals else None
    if only_unread:clauses.append(unread+' IS TRUE')
    total=c.execute('SELECT count(*) AS n'+base+' AND '.join(clauses),[*base_args,*args]).fetchone()['n'] if awaiting_reply and only_unread else (totals['n'] if totals else None)
    if after:
        cid=after if order=='id' else pos.get('id');valid_text(cid,empty=False)
        if not c.execute('SELECT 1 FROM chats c WHERE c.id=%s AND '+scope,[cid,*scope_filter(p,'c',chat_column='id')[1]]).fetchone():raise APIError('not_found',404)
        if order=='id':clauses.append('c.id>%s');args.append(cid)
        else:
            if order=='activity' and set(pos)!={'id','at'}:raise APIError('invalid_cursor')
            at=None if pos['at'] is None else date_bound(pos['at'])
            if order=='activity':
                clauses.append("(coalesce(last.sent_at,'-infinity'),c.id)<(coalesce(%s::timestamptz,'-infinity'),%s)");args.extend([at,cid])
            else:
                clauses.append('(-'+score+',attention.sent_at,c.id)>(-%s,%s,%s)');args.extend([pos['score'],at,cid])
    sort={'id':'c.id','activity':"coalesce(last.sent_at,'-infinity') DESC,c.id DESC",
        'priority':score+' DESC,attention.sent_at,c.id'}[order]
    prefix='';where=' AND '.join(clauses);query_args=[*base_args,*args,limit+1]
    if not awaiting_reply and not only_questions and not only_unread and exclude_senders is None and (
            order=='activity' or order=='id' and lower is None and upper is None):
        # Catalog ordering needs only the last head. Owner/reply/read evidence
        # belongs to the returned page, not every permitted chat before LIMIT.
        page_from=' FROM chats c'
        if order=='activity':
            page_from+=' JOIN accounts a ON a.id=c.account LEFT JOIN LATERAL ('+latest_sql(last_sql,both_directions=True)+') last ON true'
        prefix='WITH chat_page AS MATERIALIZED (SELECT c.*'+page_from+' WHERE '+where+' ORDER BY '+sort+' LIMIT %s) '
        base=base.replace(' FROM chats c JOIN accounts a',' FROM chat_page c JOIN accounts a',1)
        query_args=[*args,limit+1,*base_args,limit+1];where='true'
    rows=c.execute(prefix+'''SELECT c.id,c.account,c.title,c.provider_chat AS provider_chat_id,c.chat_type AS type,
        last.sent_at AS last_activity,last.id AS last_message_id,last.direction AS last_message_direction,
        last.snippet AS last_message_snippet,ro.unread_count AS provider_unread_count,ro.unread_count AS unread_count,
        '''+unread+''' AS provider_unread,ro.observed_at AS read_state_observed_at,ro.basis AS read_state_basis,
        CASE WHEN '''+fresh+''' THEN 'observed' WHEN ro.observed_at IS NULL THEN 'unknown' ELSE 'stale' END AS read_state,
        CASE WHEN '''+fresh+''' AND attention.id IS NOT NULL THEN
          CASE WHEN ro.read_inbox_max_id IS NOT NULL AND attention.provider_id ~ '^[0-9]{1,18}$'
            THEN attention.provider_id::bigint<=ro.read_inbox_max_id
          END END AS read_by_owner,
        NULL::boolean AS muted,NULL::boolean AS archived,'''+pending+''' AS awaiting_reply,
        attention.basis AS awaiting_reply_basis,attention.id AS awaiting_reply_message_id,
        attention.revision AS awaiting_reply_revision,attention.provider_id AS awaiting_reply_provider_message_id,
        attention.sent_at AS awaiting_reply_sent_at,attention.snippet AS awaiting_reply_snippet,
        attention.sender AS awaiting_reply_sender,sp.display_name AS awaiting_reply_sender_name,
        sp.username AS awaiting_reply_username,attention.question AS awaiting_reply_has_question,
        attention.known_contact AS awaiting_reply_known_contact,'''+score+''' AS awaiting_reply_priority,
        array_remove(ARRAY['''+','.join(f"CASE WHEN {sql} IS NOT NULL THEN '{code}' END" for code,sql in components)+'''],NULL)
          AS awaiting_reply_priority_reasons,jsonb_strip_nulls(jsonb_build_object('''+','.join(f"'{code}',{sql}" for code,sql in components)+'''))
          AS awaiting_reply_priority_components,floor('''+waited+''')::integer AS awaiting_reply_waiting_hours,
        '''+incoming_head+''' AS _incoming_head,own_last.sent_at AS _own_sent_at,own_last.numeric_id AS _own_numeric_id,
        a.platform AS _platform,coalesce('''+dismissed+''',false) AS awaiting_reply_dismissed,
        CASE WHEN a.platform='whatsapp' AND attention.sent_at=own_last.sent_at THEN
          'ambiguous_message_order' ELSE 'heuristic_not_obligation' END AS awaiting_reply_confidence'''+(
        ',count(*) OVER() AS _total,count(*) FILTER(WHERE '+unread+' IS NULL) OVER() AS _unknown' if inline_totals else '')+base+where+
        ' ORDER BY '+sort+' LIMIT %s',query_args).fetchall()
    if inline_totals:
        totals={'n':rows[0]['_total'] if rows else 0,'unknown':rows[0]['_unknown'] if rows else 0};total=totals['n']
        for row in rows:row.pop('_total');row.pop('_unknown')
    items=rows[:limit];cursor=None
    if len(rows)>limit:
        row=items[-1];position={'id':row['id'],'at':row['last_activity']}
        if order=='priority':position.update(at=row['awaiting_reply_sent_at'],score=row['awaiting_reply_priority'],as_of=score_at)
        cursor=row['id'] if order=='id' else encode_cursor(binding,position)
    # Compute run counts only for the returned page, never for every chat.
    for row in items:
        if row['awaiting_reply']:
            if view=='conversation' and row['type']=='group':
                row['awaiting_reply_incoming_count']=c.execute('''SELECT threads.n FROM chats c
                    JOIN accounts a ON a.id=c.account CROSS JOIN LATERAL ('''+group_thread_query(
                        'count(*) AS n',criteria=attention_age,count=True)+
                    ') threads WHERE c.account=%s AND c.id=%s',
                    (row['account'],row['id'])).fetchone()['n']
            else:row['awaiting_reply_incoming_count']=c.execute(f'''SELECT count(*) AS n FROM messages m
                LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
                WHERE m.account=%s AND m.chat=%s AND m.direction='incoming' AND {meaningful()}
                  AND NOT coalesce(md.metadata->'sender_is_bot'='true'::jsonb,false)
                  AND (%s::timestamptz IS NULL OR m.sent_at>%s::timestamptz OR
                    (%s='telegram' AND m.sent_at=%s::timestamptz AND {number('m')} >%s::bigint) OR
                    (%s='whatsapp' AND m.sent_at=%s::timestamptz))''',
                (row['account'],row['id'],row['_own_sent_at'],row['_own_sent_at'],
                 row['_platform'],row['_own_sent_at'],row['_own_numeric_id'],
                 row['_platform'],row['_own_sent_at'])).fetchone()['n']
        else:
            for key in tuple(row):
                if key.startswith('awaiting_reply_'):row[key]=None
        for key in ('_own_sent_at','_own_numeric_id','_platform'):row.pop(key,None)
        if not include_dismissed:row.pop('_incoming_head',None)
        if view=='conversation':
            state=conversation_state(c,p,row['account'],row['id'])
            row['conversation']=state
            latest=state['latest_known_meaningful_message']
            if latest is None:
                row['last_message_id']=None;row['last_message_snippet']=None
            if state['last_turn']=='unknown':row['last_message_direction']=None
            if row['awaiting_reply'] and state['suggested_action']=='unclear':
                row['awaiting_reply_confidence']=state['basis']['code']
    # The queue hint names its sender like a message; the raw ID stays in awaiting_reply_sender.
    from .sender_names import resolve
    resolve(c,p,items,chat='id',sender='awaiting_reply_sender',name='awaiting_reply_sender_name',
        basis='awaiting_reply_sender_name_basis')
    private=[row['id'] for row in items if row['type']=='private']
    if private:
        labels={r['id']:r for r in c.execute('SELECT c.id,CASE WHEN '+raw_title_sql()+' THEN '+peer_name_sql()+
            ' END AS name,'+self_chat_sql()+' AS self FROM chats c WHERE c.id=ANY(%s)',(private,))}
        for row in items:
            label=labels.get(row['id'])
            if label and label['self']:row.update(title=SELF_CHAT_TITLE,title_basis='self_chat')
            elif label and label['name']:row.update(title=label['name'],title_basis='latest_incoming_sender_name')
    targets(p,[r['id'] for r in items])
    if include_coverage:
        from .chat_evidence import chat_statuses
        statuses=chat_statuses(c,p,[r['id'] for r in items])
        for row in items:row['sync']=statuses[row['id']]
    return serial({'items':items,'next_cursor':cursor,'order':order,'awaiting_reply_total':total,
        **({'view':view,'conversation_view_version':1} if view=='conversation' else {}),
        'awaiting_reply_days':awaiting_reply_days if awaiting_reply else None,
        **({'excluded_likely_broadcasts_total':excluded} if exclude_likely_broadcasts else {}),
        'read_state_unknown_total':totals['unknown'] if totals else None,
        'only_unread_notice':'unknown_or_stale_read_state_excluded' if only_unread else None,
        'bot_filter_coverage':'observed_sender_flags_only' if awaiting_reply else None,
        'awaiting_reply_scope':'private_and_explicit_group_attention' if awaiting_reply else None,
        'group_attention_coverage':'observed_mentions_and_replies_only',
        'catalog_coverage':'archived_chats_only','provider_read_state':'per_chat_observation_or_unknown',
        **coverage(store,c,p,include_coverage)})
