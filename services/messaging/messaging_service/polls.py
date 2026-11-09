"""Polls in the archive (ANYM-921; the tables and read shape of ANYM-920).

A WhatsApp poll message keeps its question and options in `metadata.poll`. Each vote arrives as its own capture
(`kind='poll_vote'`, whatsmeow polls.go), never as a chat message: it replaces that voter's rows in `poll_voters`,
recomputes the poll's `poll_results` and, when a count changed, commits one `poll.updated` event, which wakes only
subscriptions that list it. Option texts and voter IDs are member-controlled data, stored bounded, never instructions.
"""
import re
from datetime import datetime

from .envelope import utc
from .identifiers import ACCOUNT_RE

MAX_OPTIONS=12
MAX_OPTION_TEXT=256
MAX_QUESTION=512
MAX_VOTED_BY=50
VOTE_KEYS=frozenset(('kind','account','chat','poll_provider_id','voter','from_me','option_indices','voted_at','observed_at',
    'origin'))
WHATSAPP_CHAT=re.compile(r'[a-zA-Z0-9_.:-]{1,140}@(c.us|g.us|lid)')


def _text(value,maximum,empty=False):
    return isinstance(value,str) and (empty or value) and len(value)<=maximum and '\x00' not in value


def poll_block(value):
    """{question, options:[{index, text}], selectable_count, truncated}: at most 12 options of at most 256 characters."""
    if (not isinstance(value,dict) or set(value)!={'question','options','selectable_count','truncated'}
            or not _text(value['question'],MAX_QUESTION,empty=True) or type(value['truncated']) is not bool
            or type(value['selectable_count']) is not int or not 0<=value['selectable_count']<=2**31
            or not isinstance(value['options'],list) or len(value['options'])>MAX_OPTIONS):
        raise ValueError('invalid_metadata')
    options=[]
    for position,option in enumerate(value['options']):
        if (not isinstance(option,dict) or set(option)!={'index','text'} or option['index']!=position
                or type(option['index']) is not int or not _text(option['text'],MAX_OPTION_TEXT,empty=True)):
            raise ValueError('invalid_metadata')
        options.append({'index':position,'text':option['text']})
    return {'question':value['question'],'options':options,'selectable_count':value['selectable_count'],
            'truncated':value['truncated']}


def validate_vote(value):
    """One vote capture: the poll's provider ID, the voter's archive sender ID and the chosen option indices."""
    if not isinstance(value,dict) or set(value)!=VOTE_KEYS:raise ValueError('invalid_poll_vote')
    indices=value['option_indices']
    if (not isinstance(value['account'],str) or not ACCOUNT_RE.fullmatch(value['account'])
            or not isinstance(value['chat'],str) or not WHATSAPP_CHAT.fullmatch(value['chat'])
            or not _text(value['poll_provider_id'],256) or not _text(value['voter'],160)
            or type(value['from_me']) is not bool or value['origin'] not in ('live','backfill')
            or not isinstance(indices,list) or len(indices)>MAX_OPTIONS
            or any(type(i) is not int or not 0<=i<MAX_OPTIONS for i in indices) or len(set(indices))!=len(indices)):
        raise ValueError('invalid_poll_vote')
    try:return {**value,'option_indices':sorted(indices),'voted_at':utc(value['voted_at']),'observed_at':utc(value['observed_at'])}
    except (TypeError,ValueError):raise ValueError('invalid_poll_vote') from None


def apply_vote(state,capture):
    """Replace one voter's choice of one poll. An older vote than the stored one for the same voter (a late or
    replayed delivery) changes nothing; an empty selection removes the voter (a retracted vote)."""
    from .store import Store
    account,chat=state.account,capture['chat']
    with state.archive.store.pool.connection() as c:
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:'+account,))
        state.archive.fence(c,account,state.lease)
        policy=c.execute('''SELECT p.*,a.platform FROM account_policies p JOIN accounts a ON a.id=p.account
            WHERE p.account=%s AND p.enabled''',(account,)).fetchone()
        if not policy or policy['platform']!='whatsapp':raise RuntimeError('collection_disabled')
        if not state.allows(policy,chat):return {'outcome':'excluded'}
        poll=c.execute('''SELECT m.id,m.deleted OR m.purged AS gone,coalesce(mm.metadata,mr.snapshot->'metadata')->'poll' AS poll
            FROM messages m JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
            LEFT JOIN message_revisions mr ON mr.message=m.id AND mr.revision=m.revision
            LEFT JOIN message_metadata mm ON mm.message=m.id AND mm.revision=m.revision
            WHERE m.account=%s AND ch.provider_chat=%s AND m.provider_id=%s''',
            (account,chat,capture['poll_provider_id'])).fetchone()
        if not poll or poll['gone'] or not isinstance(poll['poll'],dict):return {'outcome':'poll_unknown'}
        mid,voter,at=poll['id'],capture['voter'],datetime.fromisoformat(capture['voted_at'])
        options=[o['index'] for o in poll['poll'].get('options',[])]
        last=c.execute('SELECT voted_at FROM poll_votes WHERE message=%s AND voter=%s FOR UPDATE',(mid,voter)).fetchone()
        if last and last['voted_at']>=at:return {'outcome':'stale'}
        c.execute('''INSERT INTO poll_votes(account,message,voter,voted_at) VALUES(%s,%s,%s,%s)
            ON CONFLICT(message,voter) DO UPDATE SET voted_at=excluded.voted_at''',(account,mid,voter,at))
        # Deletes: this voter's previous choice of this poll (replaced by the newer vote; ANYM-921 crit. 3).
        c.execute('DELETE FROM poll_voters WHERE message=%s AND voter=%s',(mid,voter))
        for index in capture['option_indices']:
            if index in options:
                c.execute('''INSERT INTO poll_voters(account,message,option_index,voter,from_me,updated_at)
                    VALUES(%s,%s,%s,%s,%s,%s)''',(account,mid,index,voter,capture['from_me'],at))
        counts={r['option_index']:r['n'] for r in c.execute('''SELECT option_index,count(*) AS n FROM poll_voters
            WHERE message=%s AND account=%s GROUP BY option_index''',(mid,account))}
        stored={r['option_index']:r['voters'] for r in c.execute(
            'SELECT option_index,voters FROM poll_results WHERE message=%s AND account=%s',(mid,account))}
        current={index:counts.get(index,0) for index in options}
        if current==stored:return {'outcome':'poll_vote_recorded'}
        for index,voters in current.items():
            c.execute('''INSERT INTO poll_results(account,message,option_index,voters,updated_at)
                VALUES(%s,%s,%s,%s,clock_timestamp()) ON CONFLICT(message,option_index)
                DO UPDATE SET voters=excluded.voters,updated_at=excluded.updated_at''',(account,mid,index,voters))
        Store.append_event(c,account,mid,'poll.updated',origin=capture['origin'])
        return {'outcome':'poll_updated'}


def attach(c,rows):
    """The read shape of each row's archived poll (rows were individually authorized; the poll rows are read by
    those message IDs and only where their account is the message's): per option its `voters` count and a
    `voted_by` list of at most 50 people (archive sender ID and display name), `total_voters`, `results_as_of`
    and the owner's own `chosen` options. A row without a poll carries no `poll` key."""
    polls={}
    for row in rows:
        if isinstance(row.get('poll'),dict):polls[row['id']]=row
        elif 'poll' in row:row.pop('poll')
    if not polls:return rows
    ids=list(polls)
    readable=c.execute('''SELECT CASE WHEN to_regclass('public.poll_voters') IS NULL THEN false
        ELSE has_table_privilege('public.poll_results','SELECT') AND has_table_privilege('public.poll_voters','SELECT')
        END AS ok''').fetchone()['ok']
    counts,as_of,people,voters,chosen={},{},{},{},{}
    if readable:
        for r in c.execute('''SELECT r.message,r.option_index,r.voters,r.updated_at FROM poll_results r
                JOIN messages m ON m.id=r.message AND m.account=r.account WHERE r.message=ANY(%s)''',(ids,)):
            counts.setdefault(r['message'],{})[r['option_index']]=r['voters']
            as_of[r['message']]=max(as_of.get(r['message'],r['updated_at']),r['updated_at'])
        for r in c.execute('''SELECT v.message,v.option_index,v.voter,v.from_me,nullif(sp.display_name,'') AS name
                FROM poll_voters v JOIN messages m ON m.id=v.message AND m.account=v.account
                LEFT JOIN sender_profiles sp ON sp.account=v.account AND sp.chat=m.chat AND sp.sender=v.voter
                  AND EXISTS(SELECT 1 FROM messages source WHERE source.id=sp.source_message
                    AND source.account=v.account AND source.chat=m.chat AND source.sender=v.voter AND NOT source.deleted)
                WHERE v.message=ANY(%s) ORDER BY v.updated_at DESC,v.voter''',(ids,)):
            people.setdefault(r['message'],{}).setdefault(r['option_index'],[]).append({'sender':r['voter'],'name':r['name']})
            voters.setdefault(r['message'],set()).add(r['voter'])
            if r['from_me']:chosen.setdefault(r['message'],set()).add(r['option_index'])
    for mid,row in polls.items():
        block=row['poll'];options=[]
        for option in block.get('options',[]):
            index=option.get('index');listed=people.get(mid,{}).get(index,[])
            options.append({'index':index,'text':option.get('text'),'voters':counts.get(mid,{}).get(index,0),
                'voted_by':listed[:MAX_VOTED_BY],'voted_by_truncated':len(listed)>MAX_VOTED_BY})
        at=as_of.get(mid)
        row['poll']={'question':block.get('question'),'options':options,'selectable_count':block.get('selectable_count'),
            'truncated':block.get('truncated',False),'total_voters':len(voters.get(mid,())),
            'results_as_of':at.isoformat() if at else None,'chosen':sorted(chosen.get(mid,()))}
    return rows
