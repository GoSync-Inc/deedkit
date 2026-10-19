"""Optional transliteration candidates and account-local provider grouping."""
import re

from .policy import scope_filter,targets
from .search_query import cursor_binding,decode_cursor,encode_cursor,filter_values,position_text,valid_text
from .name_variants import patterns
from .whatsapp_identity import visible_cte,people_join
from .sender_names import resolve
from .message_queries import chat_titles


def search_people(store,c,p,query='',*,accounts=None,chats=None,after='',limit=20,
                  transliterate=False,group_by_sender=False):
    from .store import APIError,bounded,serial
    bounded(limit);valid_text(query,200);valid_text(after,4096)
    if type(transliterate) is not bool or type(group_by_sender) is not bool:raise APIError('invalid_filter')
    accounts=filter_values(accounts);chats=filter_values(chats)
    query=query.strip().lstrip('@').lower()
    pattern='%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
    scope,args=scope_filter(p,'m')
    clauses=[scope,'NOT m.deleted','sp.account=m.account','sp.chat=m.chat','sp.sender=m.sender']
    for column,values in [('account',accounts),('chat',chats)]:
        if values is not None:clauses.append('m.'+column+'=ANY(%s)');args.append(values)
    match='(sp.search_text LIKE q.pattern OR identity.search_text LIKE q.pattern)'
    if query:
        condition=match
        if transliterate:condition+=' OR messenger_transliterate(sp.search_text) LIKE ANY(q.variants)'
        clauses.append('('+condition+')')
    # Rank independently of the provider/account and archive-generated profile
    # ID. A whole transliterated name token is more useful than an incidental
    # substring, while every result remains an observation rather than identity
    # proof. Tokens use the same bounded spelling expansions as matching.
    variants=patterns(query)
    terms=[re.sub(r'\\(.)',r'\1',value[1:-1]) for value in variants]
    literal_fields="ARRAY[lower(sp.sender),lower(sp.username),lower(sp.display_name)]"
    tokens="regexp_split_to_array(lower(sp.display_name||' '||sp.username),'[^[:alnum:]]+')"
    translit_tokens="regexp_split_to_array(messenger_transliterate(sp.display_name||' '||sp.username),'[^[:alnum:]]+')"
    rank="0" if not query else """CASE
        WHEN q.literal=ANY("""+literal_fields+""") OR q.literal=ANY(string_to_array(coalesce(identity.search_text,''),' ')) THEN 0
        WHEN q.transliterate AND EXISTS(SELECT 1 FROM unnest("""+literal_fields+""") field
          WHERE messenger_transliterate(field)=ANY(q.terms)) THEN 1
        WHEN q.literal=ANY("""+tokens+""") THEN 2
        WHEN q.transliterate AND """+translit_tokens+""" && q.terms THEN 3
        WHEN EXISTS(SELECT 1 FROM unnest("""+tokens+""") token WHERE token LIKE q.prefix) THEN 4
        WHEN q.transliterate AND EXISTS(SELECT 1 FROM unnest("""+translit_tokens+""") token
          WHERE token LIKE ANY(q.prefixes)) THEN 5
        WHEN """+match+""" THEN 6 ELSE 7 END"""
    identity_cte,identity_args=visible_cte(p)
    cte='WITH '+identity_cte+''', q AS (SELECT %s::text AS pattern,%s::text[] AS variants,%s::text AS literal,
        %s::text[] AS terms,%s::text AS prefix,%s::text[] AS prefixes,%s::boolean AS transliterate), observed AS MATERIALIZED (
        SELECT sp.id,sp.account,sp.chat,sp.sender AS sender_id,sp.display_name,sp.username,
            sp.source_message,sp.source_sent_at,sp.observed_at,identity.metadata AS identity,
            CASE WHEN '''+match+''' THEN 'literal' ELSE 'transliteration_candidate' END AS match_type,
            '''+rank+''' AS match_rank
        FROM sender_profiles sp JOIN messages m ON m.id=sp.source_message '''+people_join()+''' CROSS JOIN q
        WHERE '''+' AND '.join(clauses)+') '
    binding=cursor_binding(p,'people',{'query':query,'accounts':accounts,'chats':chats,
        'transliterate':transliterate,'group_by_sender':group_by_sender,'spelling_candidates':2})
    query_args=[*identity_args,pattern,variants,query,terms,pattern[1:],
        [value[1:] for value in variants],transliterate,*args]
    cursor_args=[];cursor_clause=''
    if group_by_sender:
        cte+=", grouped AS (SELECT account,sender_id,jsonb_agg(to_jsonb(observed) ORDER BY chat,id) AS observations,"+\
            " CASE WHEN bool_or(match_type='literal') THEN 'literal' ELSE 'transliteration_candidate' END AS match_type,"+\
            " min(match_rank) AS match_rank FROM observed GROUP BY account,sender_id) "
        if after:
            pos=decode_cursor(after,binding)
            if set(pos)!= {'rank','account','sender'} or type(pos['rank']) is not int or not 0<=pos['rank']<=7:
                raise APIError('invalid_cursor')
            position_text(pos['account']);position_text(pos['sender'])
            anchor=c.execute(cte+'SELECT match_rank FROM grouped WHERE account=%s AND sender_id=%s',
                [*query_args,pos['account'],pos['sender']]).fetchone()
            if not anchor:raise APIError('not_found',404)
            if anchor['match_rank']!=pos['rank']:raise APIError('cursor_changed',409)
            cursor_clause='WHERE (match_rank,account,sender_id)>(%s,%s,%s)'
            cursor_args=[pos['rank'],pos['account'],pos['sender']]
        sql='SELECT * FROM grouped '+cursor_clause+' ORDER BY match_rank,account,sender_id LIMIT %s'
    else:
        if after:
            if query:
                pos=decode_cursor(after,binding)
                if set(pos)!= {'rank','id'} or type(pos['rank']) is not int or not 0<=pos['rank']<=7:
                    raise APIError('invalid_cursor')
                position_text(pos['id'])
                anchor=c.execute(cte+'SELECT match_rank FROM observed WHERE id=%s',[*query_args,pos['id']]).fetchone()
                if not anchor:raise APIError('not_found',404)
                if anchor['match_rank']!=pos['rank']:raise APIError('cursor_changed',409)
                cursor_clause='WHERE (match_rank,id)>(%s,%s)';cursor_args=[pos['rank'],pos['id']]
            else:
                # Empty directory traversal retains its historical ID cursor.
                if not c.execute(cte+'SELECT 1 FROM observed WHERE id=%s',[*query_args,after]).fetchone():
                    raise APIError('not_found',404)
                cursor_clause='WHERE id>%s';cursor_args=[after]
        sql='SELECT * FROM observed '+cursor_clause+' ORDER BY match_rank,id LIMIT %s'
    rows=c.execute(cte+sql,[*query_args,*cursor_args,limit+1]).fetchall();items=rows[:limit]
    for row in items:
        for item in row['observations'] if group_by_sender else [row]:
            if item['identity'] is None:del item['identity']
    cursor=None
    if len(rows)>limit:
        last=items[-1]
        cursor=encode_cursor(binding,{'rank':last['match_rank'],'account':last['account'],'sender':last['sender_id']}) if group_by_sender else (
            encode_cursor(binding,{'rank':last['match_rank'],'id':last['id']}) if query else last['id'])
    for row in items:
        row.pop('match_rank',None)
        if group_by_sender:
            for observation in row['observations']:observation.pop('match_rank',None)
    observations=[r for row in items for r in (row['observations'] if group_by_sender else [row])]
    # display_name stays this chat's observation; sender_name is the readable label.
    for row in observations:row['sender_name']=row['display_name']
    resolve(c,p,observations,sender='sender_id')
    chat_titles(c,observations)
    refs=[r['source_message'] for r in observations]
    targets(p,refs)
    return serial({'items':items,'next_cursor':cursor,'identity_model':'provider_sender_per_account_and_chat',
        'names_are_verified_identity':False,'directory_coverage':'observed_provider_names_only',
        'grouping':'same_account_provider_sender' if group_by_sender else 'per_chat_observation',
        'observations_coverage':'matching_visible_profiles_only','transliteration_is_identity_proof':False,
        **store.dataset(c,p)})
