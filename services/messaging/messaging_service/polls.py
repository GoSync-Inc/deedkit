"""Polls in the archive (ANYM-921; the tables and read shape of ANYM-920; Telegram: ANYM-920).

A Telegram poll message keeps its question, options and flags in `metadata.poll` and its provider state in
`telegram_polls` (migration 084). Telegram states the counts: the captured media's results and each pushed
UpdateMessagePoll replace `poll_results` (an older observation never wins); a public poll's voter list is read in
the background (telegram_polls.py) and replaces its `poll_voters`.

A WhatsApp poll message keeps its question and options in `metadata.poll`. Each vote arrives as its own capture
(`kind='poll_vote'`, whatsmeow polls.go), never as a chat message: it replaces that voter's rows in `poll_voters`,
recomputes the poll's `poll_results` and, when a count changed, commits one `poll.updated` event, which wakes only
subscriptions that list it. Option texts and voter IDs are member-controlled data, stored bounded, never instructions.
"""
import logging
import re
from datetime import datetime,timedelta

from psycopg.types.json import Jsonb

from .envelope import utc
from .identifiers import ACCOUNT_RE

MAX_OPTIONS=12
MAX_OPTION_TEXT=256
MAX_QUESTION=512
MAX_VOTED_BY=50
VOTE_KEYS=frozenset(('kind','account','chat','poll_provider_id','voter','from_me','option_indices','voted_at','observed_at',
    'origin'))
WHATSAPP_CHAT=re.compile(r'[a-zA-Z0-9_.:-]{1,140}@(c.us|g.us|lid)')
WHATSAPP_POLL_KEYS=frozenset(('question','options','selectable_count','truncated'))
TELEGRAM_FLAGS=('multiple_choice','public_voters','quiz','closed')
TELEGRAM_POLL_KEYS=frozenset(('provider_poll_id','question','options','truncated',*TELEGRAM_FLAGS))
# Telegram's poll ID in its unsigned 64-bit decimal form; option keys are the option bytes in hex (at most 100 bytes).
POLL_ID=re.compile(r'[0-9]{1,20}')
OPTION_KEY=re.compile(r'(?:[0-9a-f]{2}){1,100}')
TELEGRAM_CHAT=re.compile(r'-?[0-9]{1,20}')
# ANYM-920: the public voter list is read at most once per poll per 10 minutes, for polls younger than 30 days.
VOTES_READ_EVERY=timedelta(minutes=10)
VOTES_MAX_AGE=timedelta(days=30)
# A message snapshot's counts replace stored ones only when this much newer (Astra r1 M2; a capture slot waits 30 s).
SNAPSHOT_MARGIN=timedelta(minutes=2)
MAX_POLL_VOTES=200
MAX_VOTER_NAME=512


def _text(value,maximum,empty=False):
    return isinstance(value,str) and (empty or value) and len(value)<=maximum and '\x00' not in value


def poll_block(value):
    """WhatsApp {question, options:[{index, text}], selectable_count, truncated} or Telegram (ANYM-920)
    {provider_poll_id, question, options, multiple_choice, public_voters, quiz, closed, truncated}: at most 12
    options of at most 256 characters."""
    keys=set(value) if isinstance(value,dict) else None
    telegram=keys==TELEGRAM_POLL_KEYS
    if ((not telegram and keys!=WHATSAPP_POLL_KEYS)
            or not _text(value['question'],MAX_QUESTION,empty=True) or type(value['truncated']) is not bool
            or not isinstance(value['options'],list) or len(value['options'])>MAX_OPTIONS):
        raise ValueError('invalid_metadata')
    if telegram:
        if (not isinstance(value['provider_poll_id'],str) or not POLL_ID.fullmatch(value['provider_poll_id'])
                or any(type(value[k]) is not bool for k in TELEGRAM_FLAGS)):
            raise ValueError('invalid_metadata')
    elif type(value['selectable_count']) is not int or not 0<=value['selectable_count']<=2**31:
        raise ValueError('invalid_metadata')
    options=[]
    for position,option in enumerate(value['options']):
        if (not isinstance(option,dict) or set(option)!={'index','text'} or option['index']!=position
                or type(option['index']) is not int or not _text(option['text'],MAX_OPTION_TEXT,empty=True)):
            raise ValueError('invalid_metadata')
        options.append({'index':position,'text':option['text']})
    if telegram:
        return {'provider_poll_id':value['provider_poll_id'],'question':value['question'],'options':options,
            **{k:value[k] for k in TELEGRAM_FLAGS},'truncated':value['truncated']}
    return {'question':value['question'],'options':options,'selectable_count':value['selectable_count'],
            'truncated':value['truncated']}


def bind_whatsapp_vote(c,payload,message,indices):
    """ANYM-315: binds a native WhatsApp vote to the archived poll at the message revision the owner reviewed. The
    poll goes into the operation intent as `expected_poll`, so the preview digest binds the exact poll and options and
    the adapter refuses when its captured poll no longer archives as this block. The preview shows archived poll text,
    so the operation also requires `read` (commands._required_permissions, as forward_message). An option past the
    archived ones or more selections than `selectable_count` (0: any number) is refused before queueing."""
    from .field_errors import refusal
    from .store import APIError
    row=c.execute('''SELECT coalesce(mm.metadata,mr.snapshot->'metadata')->'poll' AS poll FROM messages m
        LEFT JOIN message_revisions mr ON mr.message=m.id AND mr.revision=m.revision
        LEFT JOIN message_metadata mm ON mm.message=m.id AND mm.revision=m.revision
        WHERE m.id=%s AND m.revision=%s''',(message['id'],message['revision'])).fetchone()
    try:poll=poll_block(row['poll']) if row and row['poll'] is not None else None
    except ValueError:poll=None
    if poll is None or 'provider_poll_id' in poll:raise APIError('unsupported_operation')
    if any(i>=len(poll['options']) for i in indices):
        raise refusal('invalid_operation_parameters',[('option_indices','maximum',
            'zero-based positions of the poll\'s archived options (metadata.poll.options)')])
    if poll['selectable_count'] and len(indices)>poll['selectable_count']:
        raise refusal('invalid_operation_parameters',[('option_indices','max_items',
            'at most the poll\'s selectable_count options; an empty list retracts the vote')])
    payload['expected_poll']=poll


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


def _count(value,maximum=2**31-1):
    return type(value) is int and 0<=value<=maximum


def _indices(value,count):
    if (not isinstance(value,list) or len(value)>MAX_OPTIONS or len(set(value))!=len(value)
            or any(type(i) is not int or not 0<=i<count for i in value)):raise ValueError('invalid_metadata')
    return sorted(value)


def telegram_results(value,poll):
    """A captured Telegram poll's results (ANYM-920), transient: the archive applies them to the poll tables and never
    stores them in the message metadata. {observed_at, option_keys:[hex], voters:[int]|null, total_voters, chosen, min};
    `voters` is null when Telegram sent no per-option counts, `min` when its `chosen` flags are not the owner's."""
    count=len(poll['options']) if isinstance(poll,dict) and isinstance(poll.get('options'),list) else -1
    if (not isinstance(value,dict) or set(value)!={'observed_at','option_keys','voters','total_voters','chosen','min'}
            or not isinstance(value['option_keys'],list) or len(value['option_keys'])!=count
            or any(not isinstance(k,str) or not OPTION_KEY.fullmatch(k) for k in value['option_keys'])
            or len(set(value['option_keys']))!=count or type(value['min']) is not bool
            or value['voters'] is not None and (not isinstance(value['voters'],list) or len(value['voters'])!=count
                or not all(_count(v) for v in value['voters']))
            or value['total_voters'] is not None and not _count(value['total_voters'])):
        raise ValueError('invalid_metadata')
    return {**value,'observed_at':utc(value['observed_at']),'chosen':_indices(value['chosen'],count),
            'option_keys':list(value['option_keys']),'voters':None if value['voters'] is None else list(value['voters'])}


def split_capture_results(e):
    """The envelope without its transient `poll_results` (so receipts, revisions and metadata never carry counts), and
    the results with the poll's ID and voter flag, or None."""
    metadata=e.get('metadata') or {}
    if 'poll_results' not in metadata:return e,None
    results=metadata['poll_results'];poll=metadata.get('poll')
    e={**e,'metadata':{k:v for k,v in metadata.items() if k!='poll_results'}}
    if e['deleted'] or not isinstance(poll,dict) or 'provider_poll_id' not in poll:return e,None
    return e,{**results,'provider_poll_id':poll['provider_poll_id'],'public_voters':poll['public_voters']}


def _apply_results(c,account,mid,*,voters,total,chosen,minimal,at,origin,margin=timedelta(0)):
    """Replace one archived Telegram poll's counts; an observation older than the stored one (by `margin` for a
    message snapshot) changes nothing. A change of an option's count or of Telegram's total commits one
    `poll.updated` event."""
    from .store import Store
    row=c.execute('''SELECT results_at,total_voters,chosen FROM telegram_polls WHERE message=%s AND account=%s
        FOR UPDATE''',(mid,account)).fetchone()
    if row is None:return 'poll_unknown'
    if row['results_at']+margin>=at:return 'stale'
    total=row['total_voters'] if total is None else total
    chosen=sorted(row['chosen']) if minimal else chosen
    c.execute('UPDATE telegram_polls SET results_at=%s,total_voters=%s,chosen=%s WHERE message=%s AND account=%s',
        (at,total,chosen,mid,account))
    changed=total!=row['total_voters']
    if voters is not None:
        stored={r['option_index']:r['voters'] for r in c.execute(
            'SELECT option_index,voters FROM poll_results WHERE message=%s AND account=%s',(mid,account))}
        if dict(enumerate(voters))!=stored:
            _store_counts(c,account,mid,voters,at);changed=True
    if not changed:return 'poll_results_unchanged'
    Store.append_event(c,account,mid,'poll.updated',origin=origin)
    return 'poll_updated'


def _store_counts(c,account,mid,voters,at):
    for index,count in enumerate(voters):
        c.execute('''INSERT INTO poll_results(account,message,option_index,voters,updated_at) VALUES(%s,%s,%s,%s,%s)
            ON CONFLICT(message,option_index) DO UPDATE SET voters=excluded.voters,updated_at=excluded.updated_at''',
            (account,mid,index,count,at))


def apply_capture_results(store,e,mid,results,lease):
    """A captured Telegram poll's results, after its message committed (archive.ingest). The first capture keeps the
    poll's ID, option keys and counts without an event (the message's own event announced it); a later one replaces
    the counts only when it is more than SNAPSHOT_MARGIN newer than the stored observation: a snapshot is stamped when
    normalized, so one that waited for a capture slot or behind its history page never undoes a newer pushed update
    (Astra r1 M2). A database outage or a lost lease raises, so the capture stays journaled and its replay (a
    duplicate receipt) applies the results (Astra r1 B2); any other error is logged by class and never fails the
    message."""
    import psycopg
    from psycopg_pool import PoolTimeout
    from .archive import Archive
    account=e['account']
    try:
        with store.pool.connection() as c:
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:'+account,))
            Archive.fence(c,account,lease)
            if c.execute('SELECT 1 FROM messages WHERE id=%s AND account=%s AND NOT deleted AND NOT purged',
                    (mid,account)).fetchone() is None:return 'poll_unknown'
            at=datetime.fromisoformat(results['observed_at'])
            created=c.execute('''INSERT INTO telegram_polls(account,message,provider_poll_id,option_keys,public_voters,
                total_voters,chosen,results_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(message) DO NOTHING
                RETURNING message''',(account,mid,results['provider_poll_id'],results['option_keys'],
                results['public_voters'],results['total_voters'],[] if results['min'] else results['chosen'],at)).fetchone()
            if created:
                if results['voters'] is not None:_store_counts(c,account,mid,results['voters'],at)
                return 'poll_recorded'
            return _apply_results(c,account,mid,voters=results['voters'],total=results['total_voters'],
                chosen=results['chosen'],minimal=results['min'],at=at,origin=e['origin'],margin=SNAPSHOT_MARGIN)
    except (psycopg.OperationalError,PoolTimeout):raise
    except RuntimeError as exc:
        if str(exc)=='collector_lease_lost':raise
        logging.getLogger(__name__).warning('poll_results_skipped: %s',type(exc).__name__)
        return 'poll_results_skipped'
    except Exception as exc:
        logging.getLogger(__name__).warning('poll_results_skipped: %s',type(exc).__name__)
        return 'poll_results_skipped'


def _telegram_policy(state,c):
    state.archive.fence(c,state.account,state.lease)
    policy=c.execute('''SELECT p.*,a.platform FROM account_policies p JOIN accounts a ON a.id=p.account
        WHERE p.account=%s AND p.enabled''',(state.account,)).fetchone()
    if not policy or policy['platform']!='telegram':raise RuntimeError('collection_disabled')
    return policy


def _counted(state,code):
    counts=state.__dict__.setdefault('poll_counts',{})
    counts[code]=counts.get(code,0)+1


UPDATE_KEYS=frozenset(('kind','account','poll_provider_id','results','total_voters','min','observed_at'))


def validate_telegram_update(value):
    """A pushed UpdateMessagePoll: the poll ID, per option key its count and the owner's chosen flag, Telegram's total."""
    results=value.get('results') if isinstance(value,dict) else None
    if (not isinstance(value,dict) or set(value)!=UPDATE_KEYS or not isinstance(value['account'],str)
            or not ACCOUNT_RE.fullmatch(value['account']) or not isinstance(value['poll_provider_id'],str)
            or not POLL_ID.fullmatch(value['poll_provider_id']) or type(value['min']) is not bool
            or value['total_voters'] is not None and not _count(value['total_voters'])
            or results is not None and (not isinstance(results,list) or len(results)>32 or any(
                not isinstance(r,dict) or set(r)!={'option','voters','chosen'} or not isinstance(r['option'],str)
                or not OPTION_KEY.fullmatch(r['option']) or not _count(r['voters']) or type(r['chosen']) is not bool
                for r in results))):
        raise ValueError('invalid_poll_update')
    try:return {**value,'observed_at':utc(value['observed_at'])}
    except (TypeError,ValueError):raise ValueError('invalid_poll_update') from None


def apply_telegram_update(state,capture):
    """Telegram's pushed counts of one poll, applied to every archived message showing it (a forwarded poll keeps its
    ID). An update for a poll the archive never captured is counted (`poll_update_unknown`) and skipped."""
    account=state.account
    with state.archive.store.pool.connection() as c:
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:'+account,))
        policy=_telegram_policy(state,c)
        rows=c.execute('''SELECT t.message,t.option_keys,ch.provider_chat FROM telegram_polls t
            JOIN messages m ON m.id=t.message AND m.account=t.account JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
            WHERE t.account=%s AND t.provider_poll_id=%s AND NOT m.deleted AND NOT m.purged ORDER BY t.message''',
            (account,capture['poll_provider_id'])).fetchall()
        if not rows:
            _counted(state,'poll_update_unknown')
            return {'outcome':'poll_update_unknown'}
        at=datetime.fromisoformat(capture['observed_at']);results=capture['results'];outcomes=[]
        for row in rows:
            if not state.allows(policy,row['provider_chat']):continue
            index={key:i for i,key in enumerate(row['option_keys'])}
            voters=None if results is None else [0]*len(index)
            for item in results or ():
                if voters is not None and item['option'] in index:voters[index[item['option']]]=item['voters']
            chosen=sorted({index[r['option']] for r in results or () if r['chosen'] and r['option'] in index})
            outcomes.append(_apply_results(c,account,row['message'],voters=voters,total=capture['total_voters'],
                chosen=chosen,minimal=capture['min'] or results is None,at=at,origin='live'))
        outcome='poll_updated' if 'poll_updated' in outcomes else outcomes[0] if outcomes else 'excluded'
        _counted(state,outcome)
        return {'outcome':outcome}


VOTES_KEYS=frozenset(('kind','account','chat','message_id','votes','complete','observed_at'))


def validate_telegram_votes(value):
    """One read of a public poll's voter list: at most 200 votes, each a voter's archive sender ID, the display name
    Telegram returned and the chosen option indices; `votes` null records a refused read (the throttle still holds)."""
    votes=value.get('votes') if isinstance(value,dict) else None
    if (not isinstance(value,dict) or set(value)!=VOTES_KEYS or not isinstance(value['account'],str)
            or not ACCOUNT_RE.fullmatch(value['account']) or not isinstance(value['chat'],str)
            or not TELEGRAM_CHAT.fullmatch(value['chat']) or type(value['message_id']) is not int
            or not 0<value['message_id']<2**63 or type(value['complete']) is not bool
            or votes is not None and (not isinstance(votes,list) or len(votes)>MAX_POLL_VOTES or any(
                not isinstance(v,dict) or set(v)!={'voter','name','options','from_me'} or not isinstance(v['voter'],str)
                or not TELEGRAM_CHAT.fullmatch(v['voter']) or not _text(v['name'],MAX_VOTER_NAME,empty=True)
                or type(v['from_me']) is not bool for v in votes))
            or votes is not None and len({v['voter'] for v in votes})!=len(votes)):
        raise ValueError('invalid_poll_votes')
    try:
        return {**value,'observed_at':utc(value['observed_at']),
            'votes':None if votes is None else [{**v,'options':_indices(v['options'],MAX_OPTIONS)} for v in votes]}
    except (TypeError,ValueError):raise ValueError('invalid_poll_votes') from None


def apply_telegram_votes(state,capture):
    """Replace a public poll's voters with one read, on every archived copy of that poll in a chat the policy still
    allows (a forwarded poll keeps its ID; Astra r1 M3); an older read changes nothing. `complete` says Telegram
    returned every vote within the cap (Astra r1 M4). Only a poll Telegram marks public has voters."""
    account=state.account
    with state.archive.store.pool.connection() as c:
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:'+account,))
        policy=_telegram_policy(state,c)
        if not state.allows(policy,capture['chat']):return {'outcome':'excluded'}
        source=c.execute('''SELECT t.provider_poll_id,t.public_voters FROM telegram_polls t
            JOIN messages m ON m.id=t.message AND m.account=t.account JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
            WHERE t.account=%s AND ch.provider_chat=%s AND m.provider_id=%s AND NOT m.deleted AND NOT m.purged''',
            (account,capture['chat'],str(capture['message_id']))).fetchone()
        if not source or not source['public_voters']:return {'outcome':'poll_unknown'}
        copies=[r for r in c.execute('''SELECT t.message,t.votes_read_at,cardinality(t.option_keys) AS options,
                ch.provider_chat FROM telegram_polls t JOIN messages m ON m.id=t.message AND m.account=t.account
                JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
                WHERE t.account=%s AND t.provider_poll_id=%s AND t.public_voters AND NOT m.deleted AND NOT m.purged
                ORDER BY t.message FOR UPDATE OF t''',(account,source['provider_poll_id']))
            if state.allows(policy,r['provider_chat'])]
        at=datetime.fromisoformat(capture['observed_at'])
        if any(r['votes_read_at'] is not None and r['votes_read_at']>=at for r in copies):return {'outcome':'stale'}
        for row in copies:
            if capture['votes'] is None:
                c.execute('UPDATE telegram_polls SET votes_read_at=%s WHERE message=%s AND account=%s',(at,row['message'],account))
                continue
            # Deletes: this public poll's previous voter rows, replaced by the newer read (ANYM-920 crit. 3).
            c.execute('DELETE FROM poll_voters WHERE message=%s AND account=%s',(row['message'],account))
            names={}
            for vote in capture['votes']:
                for index in vote['options']:
                    if index<row['options']:
                        c.execute('''INSERT INTO poll_voters(account,message,option_index,voter,from_me,updated_at)
                            VALUES(%s,%s,%s,%s,%s,%s)''',(account,row['message'],index,vote['voter'],vote['from_me'],at))
                if vote['name']:names[vote['voter']]=vote['name']
            c.execute('''UPDATE telegram_polls SET votes_read_at=%s,voters_complete=%s,voter_names=%s
                WHERE message=%s AND account=%s''',(at,capture['complete'],Jsonb(names),row['message'],account))
        if capture['votes'] is None:return {'outcome':'poll_votes_refused'}
        _counted(state,'poll_votes_read')
        return {'outcome':'poll_voters_replaced'}


def due_telegram_polls(state,limit=10):
    """One message per public poll ID younger than 30 days whose voters were never read, or whose counts changed after
    a read at least 10 minutes old, where no copy of the poll was read within those 10 minutes; newest counts first.
    The caller filters by its current policy."""
    with state.archive.store.pool.connection() as c:
        return c.execute('''SELECT * FROM (SELECT DISTINCT ON (t.provider_poll_id) t.message,t.provider_poll_id,
                t.option_keys,t.results_at,ch.provider_chat AS chat,m.provider_id FROM telegram_polls t
            JOIN messages m ON m.id=t.message AND m.account=t.account JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
            WHERE t.account=%s AND t.public_voters AND NOT m.deleted AND NOT m.purged AND m.sent_at>clock_timestamp()-%s
              AND (t.votes_read_at IS NULL OR (t.votes_read_at<=clock_timestamp()-%s AND t.results_at>t.votes_read_at))
              AND NOT EXISTS(SELECT 1 FROM telegram_polls o WHERE o.account=t.account
                AND o.provider_poll_id=t.provider_poll_id AND o.votes_read_at>clock_timestamp()-%s)
            ORDER BY t.provider_poll_id,t.results_at DESC,t.message) due
            ORDER BY results_at DESC,message LIMIT %s''',
            (state.account,VOTES_MAX_AGE,VOTES_READ_EVERY,VOTES_READ_EVERY,limit)).fetchall()


def attach(c,rows):
    """The read shape of each row's archived poll (rows were individually authorized; the poll rows are read by
    those message IDs and only where their account is the message's): per option its `voters` count and a
    `voted_by` list of at most 50 people (archive sender ID and display name), `total_voters`, `results_as_of`
    and the owner's own `chosen` options. A Telegram poll (ANYM-920) takes `total_voters` and `chosen` from Telegram
    (an anonymous poll has counts but no voters), a voter's name from the voter list when the chat has no profile,
    and shows its multiple_choice, public_voters, quiz and closed flags. A row without a poll carries no `poll` key."""
    polls={}
    for row in rows:
        if isinstance(row.get('poll'),dict):polls[row['id']]=row
        elif 'poll' in row:row.pop('poll')
    if not polls:return rows
    ids=list(polls)
    readable=c.execute('''SELECT CASE WHEN to_regclass('public.poll_voters') IS NULL THEN false
        ELSE has_table_privilege('public.poll_results','SELECT') AND has_table_privilege('public.poll_voters','SELECT')
        END AS ok''').fetchone()['ok']
    counts,as_of,people,voters,chosen,names,incomplete={},{},{},{},{},{},set()
    if readable:
        for r in c.execute('''SELECT r.message,r.option_index,r.voters,r.updated_at FROM poll_results r
                JOIN messages m ON m.id=r.message AND m.account=r.account WHERE r.message=ANY(%s)''',(ids,)):
            counts.setdefault(r['message'],{})[r['option_index']]=r['voters']
            as_of[r['message']]=max(as_of.get(r['message'],r['updated_at']),r['updated_at'])
        # Totals and the owner's choice are aggregates; at most 51 people per option leave the database (Astra r1 m1).
        for r in c.execute('''SELECT v.message,count(DISTINCT v.voter) AS total,
                coalesce(array_agg(DISTINCT v.option_index) FILTER (WHERE v.from_me),'{}') AS chosen
                FROM poll_voters v JOIN messages m ON m.id=v.message AND m.account=v.account
                WHERE v.message=ANY(%s) GROUP BY v.message''',(ids,)):
            voters[r['message']]=r['total'];chosen[r['message']]=r['chosen']
        for r in c.execute('''SELECT x.message,x.option_index,x.voter,nullif(sp.display_name,'') AS name FROM (
                SELECT v.message,v.option_index,v.voter,v.account,m.chat,row_number() OVER (PARTITION BY v.message,v.option_index
                  ORDER BY v.updated_at DESC,v.voter) AS n
                FROM poll_voters v JOIN messages m ON m.id=v.message AND m.account=v.account WHERE v.message=ANY(%s)) x
                LEFT JOIN sender_profiles sp ON sp.account=x.account AND sp.chat=x.chat AND sp.sender=x.voter
                  AND EXISTS(SELECT 1 FROM messages source WHERE source.id=sp.source_message
                    AND source.account=x.account AND source.chat=x.chat AND source.sender=x.voter AND NOT source.deleted)
                WHERE x.n<=%s ORDER BY x.message,x.option_index,x.n''',(ids,MAX_VOTED_BY+1)):
            people.setdefault(r['message'],{}).setdefault(r['option_index'],[]).append({'sender':r['voter'],'name':r['name']})
        telegram=c.execute('''SELECT to_regclass('public.telegram_polls') IS NOT NULL
            AND has_table_privilege('public.telegram_polls','SELECT') AS ok''').fetchone()['ok']
        if telegram:
            # Only the names of the listed people leave the database, as their profiles do.
            listed=sorted({p['sender'] for options in people.values() for group in options.values() for p in group})
            for r in c.execute('''SELECT t.message,t.total_voters,t.chosen,t.results_at,t.public_voters,t.voters_complete,
                    coalesce((SELECT jsonb_object_agg(n.key,n.value)
                    FROM jsonb_each_text(t.voter_names) n WHERE n.key=ANY(%s)),'{}') AS voter_names FROM telegram_polls t
                    JOIN messages m ON m.id=t.message AND m.account=t.account WHERE t.message=ANY(%s)''',(listed,ids)):
                if r['total_voters'] is not None:voters[r['message']]=r['total_voters']
                chosen[r['message']]=r['chosen'];names[r['message']]=r['voter_names']
                # Telegram's observation time of the stored counts (Astra r1 m1); an unread or capped voter list of a
                # public poll is incomplete, so an option with more voters than listed says so (Astra r1 M4).
                as_of[r['message']]=r['results_at']
                if r['public_voters'] and not r['voters_complete']:incomplete.add(r['message'])
    for mid,row in polls.items():
        block=row['poll'];options=[]
        for option in block.get('options',[]):
            index=option.get('index');listed=people.get(mid,{}).get(index,[])
            listed=[{**p,'name':p['name'] or names.get(mid,{}).get(p['sender'])} for p in listed]
            count=counts.get(mid,{}).get(index,0)
            options.append({'index':index,'text':option.get('text'),'voters':count,'voted_by':listed[:MAX_VOTED_BY],
                'voted_by_truncated':len(listed)>MAX_VOTED_BY or (mid in incomplete and count>len(listed))})
        at=as_of.get(mid)
        flags=TELEGRAM_FLAGS if 'provider_poll_id' in block else ('selectable_count',)
        row['poll']={'question':block.get('question'),'options':options,**{k:block.get(k) for k in flags},
            'truncated':block.get('truncated',False),'total_voters':voters.get(mid,0),
            'results_as_of':at.isoformat() if at else None,'chosen':sorted(chosen.get(mid,()))}
    return rows
