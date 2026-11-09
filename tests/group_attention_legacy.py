"""ANYM-915: today's group-attention SQL, frozen (copied from conversation_state.py at main 2f993334).

The oracle and the baseline of tests/test_group_attention_candidates.py and tests/test_search_performance.py.
Never import the live functions here: a change to them must not move the oracle.
"""


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
    from messaging_service.chat_queries import number
    if same_time:
        return f"coalesce({platform}='whatsapp' AND owner_last.sent_at=m.sent_at,false)"
    return f'''(owner_last.sent_at IS NULL OR m.sent_at>owner_last.sent_at OR
        ({platform}='telegram' AND m.sent_at=owner_last.sent_at AND {number('m')}>owner_last.numeric_id)
        OR ({platform}='whatsapp' AND m.sent_at=owner_last.sent_at))'''


def legacy_group_thread_query(projection, *, account='c.account',chat='c.id',
        provider_chat='c.provider_chat',platform='a.platform',enabled='true',
        criteria='true',limit=None,count=False):
    """Read explicit owner attention after the latest owner message in one chat.

    Any later meaningful owner message in the group answers earlier attention,
    as in the archive view; others' chatter neither answers nor renews it. The
    owner head is one bounded indexed tail read, and scoped messages drive
    current metadata PK reads before materialization, so other accounts and
    chats cannot enlarge the candidate pool.
    """
    from messaging_service.conversation import meaningful_sql
    from messaging_service.chat_queries import latest_sql,number
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
