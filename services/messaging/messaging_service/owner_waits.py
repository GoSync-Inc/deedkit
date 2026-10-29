"""ANYM-537: private chats where the owner asked and the peer has not written back, longest wait first.

The unit is the owner's run: the meaningful outgoing messages after the peer's last meaningful incoming one
(the conversation-state head rules; a reaction, service event or deleted message neither answers nor starts a
wait). Read-only: archive rows only, no provider call, no new state.
"""
from .conversation import meaningful_sql, visible_sql

MIN_WAIT_HOURS = 24
MAX_WAIT_HOURS = 720
# Run receipts whose message may not have reached the peer (receipts.RANK); others mean await_peer.
CHECK_DELIVERY = ('error', 'pending', 'server')
# Head codes that leave a chat out (conversation_state.derive_state); counted in uncertain_total.
UNCERTAIN = ('head_candidates_truncated', 'unclassified_latest_message', 'ambiguous_message_order',
    'message_version_conflict', 'message_revision_changed_during_read', 'no_archived_messages')
HINT = "awaiting_their_reply=true and order='waiting_longest' go together; not combinable with awaiting_reply"


def refuse(field):
    from .store import APIError
    failure=APIError('invalid_filter');failure.fields=[field];failure.hint=HINT
    raise failure


def validate(order, min_wait_hours, *, awaiting_their_reply, awaiting_reply, others):
    """The queue's own arguments; `others` names filters that read the reply queue's hint or the archive head."""
    from .store import APIError
    if type(awaiting_their_reply) is not bool:raise APIError('invalid_filter')
    if min_wait_hours is not None and (type(min_wait_hours) is not int or not 0<=min_wait_hours<=MAX_WAIT_HOURS):
        raise APIError('invalid_filter')
    if not awaiting_their_reply:
        if order=='waiting_longest':refuse('awaiting_their_reply')
        if min_wait_hours is not None:refuse('min_wait_hours')
        return None
    if order!='waiting_longest':refuse('order')
    if awaiting_reply:refuse('awaiting_reply')
    for name,value in others.items():
        if value:refuse(name)
    return MIN_WAIT_HOURS if min_wait_hours is None else min_wait_hours


def number(alias):
    from .chat_queries import number as numeric
    return numeric(alias)


def paired(sql, aliases, order):
    """One chat's lookup, or both chats of a WhatsApp phone/LID conversation (ANYM-278) merged by `order`."""
    if not aliases:return sql
    other=sql.replace('m.chat=c.id',"m.chat=(score_clock.aliases->>c.id)")
    return f'SELECT * FROM (({sql}) UNION ALL ({other})) pair ORDER BY {order} LIMIT 1'


def chats(aliases, alias='m'):
    """Both chats of a WhatsApp phone/LID conversation (ANYM-278), else the listed chat."""
    return f"{alias}.chat IN (c.id,score_clock.aliases->>c.id)" if aliases else f'{alias}.chat=c.id'


def floor(alias='m'):
    # An index bound for the run lookups; after_peer keeps the exact same-second rule.
    return f"{alias}.sent_at>=coalesce(wp.sent_at,'-infinity'::timestamptz)"


def laterals(c, aliases):
    """Joins over chats c, accounts a and score_clock(owners, aliases): head wh, its tie wt, peer wp, run start ws.

    A pair's head, tie, peer and run read both chats: the newest message of the pair decides."""
    from .chat_queries import latest_sql
    from .conflict_classes import class_sql,known_classes
    meaningful=meaningful_sql()
    observed=f'''SELECT m.sent_at,m.id,m.direction,m.provider_id,{number('m')} AS numeric_id,({meaningful}) AS meaningful,
        coalesce(md.metadata->'is_self_chat'='true'::jsonb,false) AS is_self_chat
        FROM messages m LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
        WHERE m.account=c.account AND m.chat=c.id AND NOT m.deleted AND {visible_sql()}'''
    # The head's same-second candidates, as the conversation state reads them (HEAD_LIMIT 64, one more shows truncation).
    tie=f'''SELECT count(*) AS n,bool_and(t.meaningful) AS all_meaningful,bool_and(t.provider_id ~ '^[0-9]{{1,18}}$') AS numeric_ids
        FROM (SELECT m.provider_id,({meaningful}) AS meaningful FROM messages m
          LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
          WHERE m.account=c.account AND {chats(aliases)} AND NOT m.deleted AND {visible_sql()} AND m.sent_at=wh.sent_at LIMIT 65) t'''
    peer=f'''SELECT m.sent_at,m.id,m.sender,{number('m')} AS numeric_id,
        coalesce(md.metadata->'sender_is_bot'='true'::jsonb,false) AS bot FROM messages m
        LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
        WHERE m.account=c.account AND m.chat=c.id AND m.direction='incoming' AND {meaningful} AND wh.direction='outgoing' '''
    start=f'''SELECT m.sent_at,{number('m')} AS numeric_id,m.id FROM messages m
        LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
        WHERE m.account=c.account AND m.chat=c.id AND m.direction='outgoing' AND {meaningful} AND {floor()} AND {after_peer()}
          AND wh.direction='outgoing' ORDER BY m.sent_at,{number('m')},m.id LIMIT 1'''
    known=','.join("'"+name+"'" for name in known_classes())
    newest='sent_at DESC,numeric_id DESC,id DESC'
    return f''' LEFT JOIN LATERAL ({paired(latest_sql(observed,both_directions=True),aliases,newest)}) wh ON true
      LEFT JOIN LATERAL ({tie}) wt ON true
      LEFT JOIN LATERAL ({paired(latest_sql(peer),aliases,newest)}) wp ON true
      LEFT JOIN LATERAL ({paired(start,aliases,'sent_at,numeric_id,id')}) ws ON true
      CROSS JOIN LATERAL (SELECT CASE WHEN wh.id IS NULL THEN NULL
          WHEN wt.n>64 THEN 'head_candidates_truncated'
          WHEN NOT wh.meaningful OR wt.n>1 AND NOT (a.platform='telegram' AND wt.numeric_ids) AND NOT wt.all_meaningful
            THEN 'unclassified_latest_message'
          WHEN wt.n>1 AND NOT (a.platform='telegram' AND wt.numeric_ids) THEN 'ambiguous_message_order'
          WHEN EXISTS(SELECT 1 FROM message_conflicts mc WHERE mc.message=wh.id AND mc.resolved_at IS NULL
            AND NOT '''+class_sql(c)+f''' IN ({known})) THEN 'message_version_conflict' END AS code) wu'''


def after_peer(alias='m'):
    # Strictly after the peer's last message: a WhatsApp same-second outgoing message cannot be ordered after it.
    return f'''(wp.sent_at IS NULL OR {alias}.sent_at>wp.sent_at OR
        (a.platform='telegram' AND {alias}.sent_at=wp.sent_at AND {number(alias)}>wp.numeric_id))'''


def run_has(condition, aliases):
    """Any message of the run meets `condition` (meaningful outgoing after the peer, in either chat of a pair)."""
    return f'''EXISTS(SELECT 1 FROM messages m LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
        WHERE m.account=c.account AND {chats(aliases)} AND m.direction='outgoing' AND {meaningful_sql()} AND {floor()}
          AND {after_peer()}
          AND {condition})'''


def conditions(*, aliases, min_wait_hours, days, principal, mutes):
    """SQL predicates: chat (the queue's chats), listed, uncertain, question, muted (a flag expression or None)."""
    from .chat_queries import courtesy_pattern,self_chat_sql,sql_literal
    pair="(c.id,score_clock.aliases->>c.id)" if aliases else '(c.id)'
    peer_id="coalesce(wp.sender,c.provider_chat)"
    chat=(f"""c.chat_type='private' AND NOT coalesce(ro.is_self_chat,false) AND NOT (a.platform='telegram' AND c.provider_chat='777000')
        AND NOT coalesce(wh.is_self_chat,false) AND NOT {self_chat_sql()} AND NOT coalesce(wp.bot,false)
        AND NOT coalesce((score_clock.owners->(a.platform||'|'||{peer_id}))-c.account<>'[]'::jsonb,false)"""+(
        # One indexed probe skips chats without a recent owner message before the run lookups.
        f''' AND EXISTS(SELECT 1 FROM messages recent WHERE recent.account=c.account AND {chats(aliases,'recent')}
          AND recent.direction='outgoing' AND NOT recent.deleted
          AND recent.sent_at>=CURRENT_TIMESTAMP-interval '{days} days' OFFSET 0)''' if days else ''))
    courtesy=(f"length(btrim(m.body)) BETWEEN 1 AND 40 AND position('?' in m.body)=0 AND position('？' in m.body)=0"
        f" AND lower(btrim(m.body)) ~ {sql_literal(courtesy_pattern())}")
    listed=(f"""wu.code IS NULL AND wh.direction='outgoing' AND ws.sent_at IS NOT NULL
        AND ws.sent_at<=CURRENT_TIMESTAMP-interval '{min_wait_hours} hours'"""+(
        f" AND ws.sent_at>=CURRENT_TIMESTAMP-interval '{days} days'" if days else '')+
        ' AND '+run_has(f'NOT ({courtesy})',aliases))
    question=run_has("(position('?' in m.body)>0 OR position('？' in m.body)>0)",aliases)
    muted=None
    if mutes:
        principal=sql_literal(principal)
        muted=(f"CASE WHEN EXISTS(SELECT 1 FROM reply_mutes rc WHERE rc.principal={principal} AND rc.chat IN {pair}) THEN 'chat'"
            f" WHEN EXISTS(SELECT 1 FROM reply_mutes rm WHERE rm.principal={principal} AND rm.account=c.account"
            " AND rm.sender=wp.sender) THEN 'sender' END")
    return {'chat':chat,'listed':listed,'uncertain':'wu.code IS NOT NULL','question':question,'muted':muted}


def run_count(c, row, aliases):
    """The run's message count, for a returned row only."""
    return c.execute(f'''SELECT count(*) AS n FROM messages m
        LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
        WHERE m.account=%s AND m.chat=ANY(%s) AND m.direction='outgoing' AND {meaningful_sql()}
          AND m.sent_at>=coalesce(%s::timestamptz,'-infinity'::timestamptz) AND (%s::timestamptz IS NULL OR m.sent_at>%s::timestamptz OR
            (%s='telegram' AND m.sent_at=%s::timestamptz AND {number('m')}>%s::bigint))''',
        (row['account'],[row['id'],*([aliases[row['id']]] if row['id'] in aliases else [])],row['_wait_peer_at'],
         row['_wait_peer_at'],row['_wait_peer_at'],row['_platform'],row['_wait_peer_at'],row['_wait_peer_numeric'])).fetchone()['n']


def finish(row, state, receipts):
    """The row's `waiting` from the run facts the page query carried; the head's state gives an unverified head's code.

    The state is read after the page: a chat whose head changed meanwhile (the peer wrote, a revision changed during
    the read) is no longer the owner's turn. Returns None for a kept row, else 'uncertain' or 'answered' (the caller
    drops it and corrects the totals)."""
    from .presentation import waiting_summary
    if state['last_turn']!='owner':
        return 'uncertain' if state['basis']['code'] in UNCERTAIN else 'answered'
    last=row.pop('_wait_last_id')
    facts={'since':row.pop('_wait_since'),'count':row.pop('_wait_count'),'receipt':(receipts.get(last) or {}).get('state'),
        'last_message_id':last,'last_at':row.pop('_wait_last_at'),'muted':row.pop('_wait_muted')}
    if state['suggested_action']=='unclear':facts['confidence']=state['basis']['code']
    for key in ('_wait_peer_at','_wait_peer_numeric'):row.pop(key)
    row['waiting']=waiting_summary(facts,row.get('last_message_id'))
    return None


def summary(store, c, p, waits, accounts, min_wait_hours, only_questions):
    """Response totals and each scoped account's source state, so an empty list after a stopped collector reads as
    nothing archived (ANYM-458)."""
    totals=waits['totals']
    sources=[{k:v for k,v in {'account':r['id'],'status':r['status'],'last_ingest_at':r['last_ingest_at']}.items() if v is not None}
        for r in store.dataset(c,p)['sources'] if accounts is None or r['id'] in accounts]
    return {'awaiting_their_reply_total':totals['listed'],'uncertain_total':totals['uncertain'],'min_wait_hours':min_wait_hours,
        'awaiting_their_reply_notice':'no_written_reply_in_archived_chat',
        **({'excluded_without_question_total':totals['without_question']} if only_questions else {}),
        **({'muted_total':totals['muted']} if waits['mutes'] else {}),'accounts':sources}
