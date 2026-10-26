"""Scoped message queries and presentation, separate from authentication/ACK."""
from __future__ import annotations

import re

from .chat_queries import title_sql
from .chat_titles import retitle
from .policy import scope_filter, targets
from .sender_names import resolve
from .search_query import (compile_query, coverage, cursor_binding, decode_cursor,
    encode_cursor, filter_values, fingerprint, output_options, position_date, position_text, shape_messages, valid_text)

CHAT_TYPES = ('private','group','channel','unknown')

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
    CASE WHEN NOT m.deleted AND coalesce(mm.metadata,mr.snapshot->'metadata')->>'message_kind'='contact'
        AND jsonb_typeof(coalesce(mm.metadata,mr.snapshot->'metadata')->'contact')='object'
      THEN coalesce(mm.metadata,mr.snapshot->'metadata')->'contact' END AS contact,
    (SELECT platform FROM accounts WHERE id=m.account) AS platform,
    (SELECT observed_at FROM message_revisions WHERE message=m.id AND revision=1) AS first_observed_at,
    'archive_revision_observation'::text AS first_observed_at_basis'''
BASE = 'm.id,m.account,m.chat,m.sender,m.body,m.sent_at,m.revision,m.direction'
# ANYM-529: a shared contact matches by name, or by phone digits: equal or a suffix of at least 6 digits.
CONTACT_MATCH = '''EXISTS(SELECT 1 FROM jsonb_array_elements(x.metadata->'contact'->'contacts') ct
    WHERE lower(ct->>'name') LIKE lower(q.literal_pattern) ESCAPE E'\\\\'
      OR lower(ct->>'vcard_name') LIKE lower(q.literal_pattern) ESCAPE E'\\\\'
      OR (%s::text IS NOT NULL AND EXISTS(SELECT 1 FROM jsonb_array_elements(ct->'phones') ph
        WHERE regexp_replace(ph->>'number','[^0-9]','','g') LIKE '%%'||%s::text OR ph->>'whatsapp_id' LIKE '%%'||%s::text)))'''


def contact_digits(query):
    """Digits of a phone-like query, leading zeros dropped (00 prefix, local trunk 0); None below 6 digits."""
    if not re.fullmatch(r'[0-9+()\-.\s]+',query):return None
    digits=re.sub(r'[^0-9]','',query).lstrip('0')
    return digits if len(digits)>=6 else None

# ANYM-273: forwarded copies group only by the provider-stated origin, never by text alone. Keys:
# p = origin chat and message ID, u = origin sender, second and text hash (one sender can post two
# messages in one second). Platform, not account: one message
# received by two owner accounts of one platform is one group.
# Times compare as UTC text to the second (envelope.utc stores them so); no cast can fail.
COPIES_LISTED = 20
FORWARD_KEY = '''CASE WHEN md.v->'forwarded'='true'::jsonb AND jsonb_typeof(md.v->'forward_origin')='object'
      AND md.v->'forward_origin'->>'kind' IN ('user','chat','channel','newsletter') THEN
      CASE WHEN nullif(md.v->'forward_origin'->>'chat_id','') IS NOT NULL AND nullif(md.v->'forward_origin'->>'provider_message_id','') IS NOT NULL
        THEN a.platform||'|p|'||(md.v->'forward_origin'->>'chat_id')||'|'||(md.v->'forward_origin'->>'provider_message_id')
      WHEN md.v->'forward_origin'->>'kind'='user' AND nullif(md.v->'forward_origin'->>'sender_id','') IS NOT NULL
          AND length(md.v->'forward_origin'->>'sent_at')>=19
        THEN a.platform||'|u|'||(md.v->'forward_origin'->>'sender_id')||'|'||left(md.v->'forward_origin'->>'sent_at',19)||'|'||md5(m.body)
      END END'''


def collapse_ctes(position):
    """Groups over every match before the cursor cut, so a page never repeats or skips a group.

    One row per group shows the archived original (the earliest one per key) or else the earliest
    copy. The group sits, and its cursor anchors, where its first member in the page order sits
    (position), so a fresh forward of an old original is not pushed back by the original's date.
    Members are matches only, so they already passed the caller's scope and the search filters.
    """
    return ['''keyed AS MATERIALIZED (SELECT x.*,m.chat AS member_chat,m.sender AS member_sender,'''+FORWARD_KEY+''' AS fkey,
          coalesce(md.v->'forwarded'='true'::jsonb,false) AS is_forward,
          a.platform||'|p|'||ch.provider_chat||'|'||m.provider_id AS post_key,
          CASE WHEN m.sender<>'' THEN a.platform||'|u|'||m.sender||'|'||to_char(m.sent_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS')||'|'||md5(m.body) END AS person_key
        FROM matches x JOIN messages m ON m.id=x.id JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
        JOIN accounts a ON a.id=m.account
        LEFT JOIN message_revisions mr ON mr.message=m.id AND mr.revision=m.revision
        LEFT JOIN message_metadata mm ON mm.message=m.id AND mm.revision=m.revision
        CROSS JOIN LATERAL (SELECT coalesce(mm.metadata,mr.snapshot->'metadata') AS v) md)''',
        '''forward_keys AS MATERIALIZED (SELECT DISTINCT fkey FROM keyed WHERE fkey IS NOT NULL)''',
        '''originals AS MATERIALIZED (SELECT o.*,coalesce(fp.fkey,fu.fkey) AS okey FROM keyed o
          LEFT JOIN forward_keys fp ON fp.fkey=o.post_key LEFT JOIN forward_keys fu ON fu.fkey=o.person_key
          WHERE NOT o.is_forward)''',
        '''members AS MATERIALIZED (SELECT k.*,CASE WHEN k.is_forward THEN k.fkey
            WHEN o.okey IS NOT NULL AND o.rank=1 THEN o.okey END AS gkey FROM keyed k
          LEFT JOIN (SELECT id,okey,row_number() OVER (PARTITION BY okey ORDER BY sent_at,id) AS rank
            FROM originals WHERE okey IS NOT NULL) o ON o.id=k.id)''',
        '''grouped AS MATERIALIZED (SELECT *,row_number() OVER (PARTITION BY gkey ORDER BY is_forward,sent_at,id) AS grank,
          row_number() OVER (PARTITION BY gkey ORDER BY '''+position+''') AS prank,
          first_value(id) OVER (PARTITION BY gkey ORDER BY is_forward,sent_at,id) AS shown,
          count(*) OVER (PARTITION BY gkey) AS group_size,max(sent_at) OVER (PARTITION BY gkey) AS latest_at FROM members)''',
        '''results AS NOT MATERIALIZED (SELECT id,sent_at,revision,source_order,score,gkey,
          CASE WHEN gkey IS NOT NULL THEN shown END AS shown,
          CASE WHEN gkey IS NOT NULL THEN group_size END AS copies_total,
          CASE WHEN gkey IS NOT NULL THEN latest_at END AS latest_copy_at FROM grouped WHERE gkey IS NULL OR prank=1)''']


LISTED_CTE=('''listed AS MATERIALIZED (SELECT gkey,array_agg(id ORDER BY grank) AS copy_ids,
          array_agg(member_chat ORDER BY grank) AS copy_chats,array_agg(member_sender ORDER BY grank) AS copy_senders,
          array_agg(sent_at ORDER BY grank) AS copy_times FROM grouped
          WHERE grank BETWEEN 2 AND '''+str(COPIES_LISTED+1)+''' AND gkey IN (SELECT gkey FROM page WHERE copies_total>1) GROUP BY gkey)''')


def attach_copies(items):
    """Shown rows gain copies_total, latest_copy_at and up to COPIES_LISTED other members, earliest first."""
    for item in items:
        item.pop('gkey',None);total=item.pop('copies_total',None);latest=item.pop('latest_copy_at',None)
        listed=[item.pop(k,None) for k in ('copy_ids','copy_chats','copy_senders','copy_times')]
        if listed[0] and total and total>1:
            item['copies_total']=total;item['latest_copy_at']=latest
            item['copies']=[{'chat':chat,'message_id':mid,'sender':sender,'sent_at':at} for mid,chat,sender,at in zip(*listed)]
    return items


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


REPLY_SNIPPET_CHARS = 80


def reply_context(c, p, rows):
    """Rows that answer an archived message gain reply_to: {message_id, snippet, sender_name}.

    The target is looked up inside the replying message's own account and chat and the caller's
    scope. A missing, deleted or unreadable target adds nothing, so those cases look alike.
    The snippet is untrusted message text, cut to REPLY_SNIPPET_CHARS.
    """
    asking=[r for r in rows if r.get('reply_to_provider_id') and r.get('account') and r.get('chat')]
    if not asking:
        return rows
    where,scope=scope_filter(p,'m')
    # WhatsApp stores a native reference as true|false_<chat>_<stanza>[_<participant>].
    native='''EXISTS(SELECT 1 FROM unnest(ARRAY['true_','false_']) side
        WHERE left(m.provider_id,length(side||r.pchat||'_'))=side||r.pchat||'_'
          AND split_part(substr(m.provider_id,length(side||r.pchat||'_')+1),'_',1)=r.ref)'''
    found={}
    for exact in (True,False):
        todo=[(i,r) for i,r in enumerate(asking) if i not in found
            and (exact or r.get('platform')=='whatsapp' and r.get('provider_chat_id'))]
        if not todo:
            continue
        for hit in c.execute('''SELECT r.idx,m.id,m.account,m.chat,m.sender,left(m.body,400) AS body,nullif(sp.display_name,'') AS sender_name
            FROM unnest(%s::integer[],%s::text[],%s::text[],%s::text[],%s::text[]) AS r(idx,account,chat,ref,pchat)
            JOIN messages m ON m.account=r.account AND m.chat=r.chat AND NOT m.deleted AND '''+where+'''
              AND '''+('m.provider_id=r.ref' if exact else native)+'''
            LEFT JOIN sender_profiles sp ON sp.account=m.account AND sp.chat=m.chat AND sp.sender=m.sender
              AND EXISTS(SELECT 1 FROM messages source WHERE source.id=sp.source_message
                AND source.account=m.account AND source.chat=m.chat AND source.sender=m.sender AND NOT source.deleted)
            ORDER BY r.idx,m.sent_at,m.id''',
                [[i for i,_ in todo],[r['account'] for _,r in todo],[r['chat'] for _,r in todo],
                 [r['reply_to_provider_id'] for _,r in todo],[r.get('provider_chat_id') or '' for _,r in todo],*scope]).fetchall():
            found.setdefault(hit['idx'],hit)
    if found:
        resolve(c,p,list(found.values()))
    for index,hit in found.items():
        reply={'message_id':hit['id'],'snippet':' '.join((hit['body'] or '').split())[:REPLY_SNIPPET_CHARS]}
        if hit.get('sender_name'):
            reply['sender_name']=hit['sender_name'][:64]
        asking[index]['reply_to']=reply
    return rows


def chat_titles(c, items, *, cap=64, p=None):
    """Each item that names an authorized account and chat gains chat_title (at most cap characters)."""
    pairs=sorted({(i['account'],i['chat']) for i in items if i.get('account') and i.get('chat')})
    if not pairs:
        return items
    titles={(r['account'],r['id']):r['title'] for r in c.execute(
        'SELECT ch.account,ch.id,'+title_sql('ch')+' AS title FROM unnest(%s::text[],%s::text[]) AS k(account,chat) '
        'JOIN chats ch ON ch.account=k.account AND ch.id=k.chat',([a for a,_ in pairs],[x for _,x in pairs]))}
    for item in items:
        title=titles.get((item.get('account'),item.get('chat')))
        if title:
            item['chat_title']=title[:cap]
    if p is not None:
        retitle(c,p,items,cap=cap)
    return items


def enrich(c, rows, p, *, titles=None, order_key=None):
    """Rows were individually authorized; metadata comes from the same chat.

    A missing sender name falls back only to names the caller can read (sender_names).
    """
    if not rows:
        return rows
    metadata=c.execute('SELECT m.id,m.account,m.chat,m.sender,'+META+' FROM messages m '+JOINS+' WHERE m.id=ANY(%s)',
        ([r['id'] for r in rows],)).fetchall()
    resolve(c,p,forward_presence(metadata))
    reply_context(c,p,metadata)
    retitle(c,p,metadata,titles=titles)
    by_id={r['id']:r for r in metadata}
    for row in rows:
        row.update(by_id.get(row['id'],{}))
    from .result_evidence import decorate
    decorate(c,[r for r in rows if 'revision' in r],order_key=order_key)
    return rows


def filters(p, *, account='',chat='',sender='',accounts=None,chats=None,senders=None,
            from_time='',to_time='',direction='',exclude_channels=False,chat_types=None,include_channels=None):
    """include_channels=None (internal reads) applies no chat type default; False is
    the public search default: channels excluded unless chat or chats names a chat."""
    from .store import APIError,date_bound
    if (not isinstance(direction,str) or direction not in ('','incoming','outgoing') or type(exclude_channels) is not bool
            or include_channels is not None and type(include_channels) is not bool):
        raise APIError('invalid_filter')
    types=filter_values(chat_types)
    if types is not None and any(t not in CHAT_TYPES for t in types):raise APIError('invalid_filter')
    if include_channels and (exclude_channels or types is not None and 'channel' not in types):raise APIError('invalid_filter')
    if (types is None and include_channels is False and not chat and chats is None) or exclude_channels:
        types=[t for t in (CHAT_TYPES if types is None else types) if t!='channel']
    lower,upper=date_bound(from_time),date_bound(to_time)
    if lower and upper and lower>=upper:
        raise APIError('invalid_date_range')
    scope,args=scope_filter(p,'m'); clauses=[scope,'NOT m.deleted']
    normalized={'direction':direction,'from_time':lower,'to_time':upper}
    if include_channels:normalized['include_channels']=True
    if types is not None:
        normalized['chat_types']=sorted(types)
        if set(types)!=set(CHAT_TYPES):
            # The bare column keeps chats.chat_type statistics. coalesce() got a fixed 0.5% guess per type, so the
            # planner walked every message of the (supposedly few) chats instead of the text indexes (ANYM-272, ANYM-501).
            clauses.append("EXISTS(SELECT 1 FROM chats ch WHERE ch.id=m.chat AND ch.account=m.account AND (ch.chat_type=ANY(%s)"+
                (' OR ch.chat_type IS NULL' if 'unknown' in types else '')+'))')
            args.append(sorted(types))
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


def named_chats(c, p, chat='', chats=None):
    """Every explicitly named chat that is unknown or outside the key's scope is one not_found.

    The code and text are the same for both, so the answer never says whether the chat exists.
    chats=[] names nothing and matches nothing. Malformed values keep invalid_filter.
    """
    from .store import APIError
    for parameter,names in (('chat',[valid_text(chat)] if chat else []),('chats',filter_values(chats) or [])):
        if not names:
            continue
        where,args=scope_filter(p,'c',chat_column='id')
        known={r['id'] for r in c.execute('SELECT c.id FROM chats c WHERE c.id=ANY(%s) AND '+where,[names,*args])}
        if any(name not in known for name in names):
            failure=APIError('not_found',404);failure.fields=[parameter]
            raise failure


def search(store,c,p,query='',account='',chat='',sender='',from_time='',to_time='',after='',limit=20,
           *,accounts=None,chats=None,senders=None,query_mode='all',order='asc',before='',direction='',
           search_language='auto',fuzzy=False,compact=False,body_chars=None,include_coverage=True,view='archive',include_system=False,
           exclude_channels=False,chat_types=None,include_channels=None,collapse_forwards=True,person=''):
    from .store import APIError,bounded,serial
    import re
    from .conversation import options,numeric_order_sql,visible_sql,decorate_messages,presentation
    options(view,include_system)
    conversation=view=='conversation'
    bounded(limit)
    cap=output_options(compact,body_chars,include_coverage)
    plan=compile_query(query,query_mode,search_language)
    if order not in ('asc','desc','relevance') or type(fuzzy) is not bool or type(collapse_forwards) is not bool:
        raise APIError('invalid_filter')
    valid_text(after,4096);valid_text(before,4096)
    if after and before or before and order=='relevance':raise APIError('invalid_filter')
    if fuzzy and (query_mode!='all' or not re.fullmatch(r'\w{3,64}',query,flags=re.UNICODE)):
        raise APIError('invalid_fuzzy_query')
    # A common word over a large archive costs the plan past jit_optimize_above_cost: about 4.6 s of LLVM
    # compilation under the pool's 5 s statement_timeout, for a query that runs in well under a second (as list_chats; ANYM-501).
    c.execute('SET LOCAL jit=off')
    clauses,args,normalized=filters(p,account=account,chat=chat,sender=sender,accounts=accounts,
        chats=chats,senders=senders,from_time=from_time,to_time=to_time,direction=direction,exclude_channels=exclude_channels,
        chat_types=chat_types,include_channels=include_channels)
    # ANYM-283: an owner-made person narrows to the exact (account,sender) pairs this key can read.
    members=digest=None
    if person!='':
        from .person_links import member_clause,visible_members
        person,members,digest=visible_members(c,p,person)
        clause,values=member_clause('m','account','sender',members);clauses.append(clause);args.extend(values)
    # An explicit type filter binds the cursor; the default exclusion keeps bare ID cursors.
    typed=exclude_channels or chat_types is not None or include_channels is True
    # ANYM-273: an explicit flat view binds its cursor, so a page cannot continue in the other view.
    flat=bool(plan.query) and not collapse_forwards
    binding=cursor_binding(p,'messages',{**normalized,'query':query,'query_mode':query_mode,
        'language':search_language,'order':order,'fuzzy':fuzzy,
        # Only the non-default exact view binds, so cursors issued before ANYM-273 stay valid.
        **({'collapse_forwards':False} if flat else {}),
        **({'person':person} if members is not None else {}),
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
    head='SELECT m.id,m.sent_at,m.revision,'+(numeric_order_sql('m') if conversation else '0::bigint')+' AS source_order,'+score+' AS score FROM '
    base_sql=head+'messages m '+extra_join+' CROSS JOIN search_query q WHERE '+\
        ' AND '.join(clauses)+' AND '+condition
    ctes=[plan.cte];match_args=args
    if fuzzy_used:
        ctes.append('matches AS MATERIALIZED ('+base_sql+' ORDER BY score DESC,m.sent_at DESC,m.id DESC LIMIT 200)')
    elif plan.query:
        # ANYM-529: a shared contact matches by its name or phone digits, under the same scope clauses.
        digits=contact_digits(query)
        contact_sql=head+'message_metadata x JOIN messages m ON m.id=x.message AND m.revision=x.revision '+extra_join+\
            " CROSS JOIN search_query q WHERE x.metadata ? 'contact' AND NOT m.deleted AND "+' AND '.join(clauses)+' AND '+CONTACT_MATCH
        match_args=[*args,*args,digits,digits,digits]
        if plan.mode!='exact':match_type='coalesce('+match_type+",'contact'::text)"
        # Separate selective indexed matching from the chronological LIMIT.
        # Otherwise the planner may prefer walking the date index, expecting
        # early hits; a rare or absent query then traverses the entire archive.
        ctes.append('matches AS MATERIALIZED ('+base_sql+' UNION '+contact_sql+')')
    else:
        ctes.append('matches AS NOT MATERIALIZED ('+base_sql+')')
    cursor_clause='';cursor_args=[]
    snapshot=None
    snapshot_ctes=list(ctes)
    # ANYM-273: a query search returns forwarded copies once; a chat listing keeps every copy.
    collapsing=bool(plan.query) and collapse_forwards
    if collapsing:ctes.extend(collapse_ctes('score DESC,sent_at DESC,id DESC' if order=='relevance' else
        ','.join(column+' '+order.upper() for column in ('sent_at',*(('source_order',) if conversation else ()),'id'))))
    def read_snapshot():
        value=c.execute('WITH '+','.join(snapshot_ctes)+''' SELECT count(*) AS n,
            coalesce(sum(hashtextextended(id||'|'||sent_at::text||'|'||revision::text||'|'||source_order::text,0)::numeric),0)::text AS checksum
            FROM matches''',[*plan.params,*match_args]).fetchone()
        return fingerprint(value)
    if after or before:
        cursor=after or before
        if cursor.startswith('q1.'):
            position=decode_cursor(cursor,binding)
            # A visible membership change is cursor_changed; a hidden one leaves the digest alone.
            if members is not None and position.pop('members',None)!=digest:raise APIError('cursor_changed',409)
            if set(position)!=({'id','at','score','revision','fuzzy','source_order','snapshot'} if conversation else {'id','at','score','revision','fuzzy'}):
                raise APIError('invalid_cursor')
            mid=position_text(position['id'])
            at=position_date(position['at'])
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
            if order=='relevance' or conversation or typed or flat or members is not None:raise APIError('invalid_cursor')
            anchor=store.message(c,p,cursor)
            cursor_clause='WHERE (sent_at,id)'+('<' if before or order=='desc' else '>')+'(%s,%s)'
            cursor_args=[anchor['sent_at'],anchor['id']]
    sort='score DESC,sent_at DESC,id DESC' if order=='relevance' else ('sent_at '+order.upper()+(',source_order '+order.upper() if conversation else '')+',id '+order.upper())
    if conversation:
        # Aggregate and page share one statement snapshot. A late backfill
        # cannot slip between reading this page and creating its continuation.
        ctes.append("view_snapshot AS MATERIALIZED (SELECT count(*) AS n,coalesce(sum(hashtextextended(id||'|'||sent_at::text||'|'||revision::text||'|'||source_order::text,0)::numeric),0)::text AS checksum FROM matches)")
    ctes.append('page AS MATERIALIZED (SELECT * FROM '+('results ' if collapsing else 'matches ')+cursor_clause+' ORDER BY '+sort+' LIMIT %s)')
    if collapsing:ctes.append(LISTED_CTE)
    final_sort='page.score DESC,m.sent_at DESC,m.id DESC' if order=='relevance' else ('m.sent_at '+order.upper()+(',page.source_order '+order.upper() if conversation else '')+',m.id '+order.upper())
    if collapsing:final_sort=re.sub(r'\bm\.','page.',final_sort)
    snippet='left('+plan.snippet('m.body')+',800)'
    projection=BASE if cap is None else BASE.replace('m.body','left(m.body,'+str(cap)+') AS body,length(m.body)>'+str(cap)+' AS body_truncated')
    rows=c.execute('WITH '+','.join(ctes)+' SELECT '+projection+','+META+',page.score,'+match_type+
        ' AS match_type,'+snippet+' AS snippet,page.source_order'+(',page.gkey,page.copies_total,page.latest_copy_at,page.id AS position_id,page.sent_at AS position_at,page.revision AS position_revision,listed.copy_ids,listed.copy_chats,listed.copy_senders,listed.copy_times' if collapsing else '')+(',view_snapshot.n AS snapshot_count,view_snapshot.checksum AS snapshot_checksum' if conversation else '')+' FROM page JOIN messages m ON m.id='+('coalesce(page.shown,page.id) ' if collapsing else 'page.id ')+JOINS+(' LEFT JOIN listed ON listed.gkey=page.gkey' if collapsing else '')+
        ' CROSS JOIN search_query q'+(' CROSS JOIN view_snapshot' if conversation else '')+' ORDER BY '+final_sort,[*plan.params,*match_args,*cursor_args,limit+1]).fetchall()
    if conversation and rows:
        current_snapshot=fingerprint({'n':rows[0]['snapshot_count'],'checksum':rows[0]['snapshot_checksum']})
        if snapshot is not None and snapshot!=current_snapshot:raise APIError('cursor_changed',409)
        snapshot=current_snapshot
    items=reply_context(c,p,resolve(c,p,forward_presence(rows[:limit])))
    if collapsing:attach_copies(items)
    retitle(c,p,items)
    next_cursor=None
    extra={'members':digest} if members is not None else {}
    if len(rows)>limit:
        last=items[-1]
        # A group's cursor anchors on the member that holds its place in this order (ANYM-273).
        if collapsing:last={**last,'id':last['position_id'],'sent_at':last['position_at'],'revision':last['position_revision']}
        if conversation:
            snapshot=snapshot or read_snapshot()
            next_cursor=encode_cursor(binding,{'id':last['id'],'at':last['sent_at'],'score':last['score'],
                'revision':last['revision'],'fuzzy':fuzzy_used,'source_order':last['source_order'],'snapshot':snapshot,**extra})
        else:next_cursor=encode_cursor(binding,{'id':last['id'],'at':last['sent_at'],'score':last['score'],
            'revision':last['revision'],'fuzzy':fuzzy_used,**extra}) if order=='relevance' or fuzzy or typed or flat or members is not None else last['id']
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
        for internal in ('source_order','snapshot_count','snapshot_checksum','position_id','position_at','position_revision'):item.pop(internal,None)
    targets(p,[i for r in items for i in [r['id'],*(x['message_id'] for x in r.get('copies',()))]])
    return serial({'items':items,'next_cursor':next_cursor,
        'result_evidence':query_evidence(c,p,normalized,items,paged=bool(after or before),include=include_coverage),
        **({'conversation_view':presentation(items,order=order,include_system=include_system)} if conversation else {}),
        'search':{'language':search_language,'query_mode':query_mode,'order':order,'fuzzy_used':fuzzy_used,
            'fuzzy_candidate_limit':200 if fuzzy_used else None,'collapse_forwards':collapsing},**coverage(store,c,p,include_coverage)})


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
