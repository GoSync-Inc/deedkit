"""Call events for the module router (ANYM-563): terminal states and owner requests as a sequenced journal.

Each row has a fresh gapless seq (SQLite AUTOINCREMENT inside the writer's transaction: a rolled back or
ignored insert takes no number). Rows hold references and enumerations only, never transcript text, decision
questions or conditions, owner answers or destinations. GET /v1/events serves the producers' cursor contract
of ANYM-562 (SUBSCRIPTIONS.md, Module events): the 562 envelope plus a top-level scope_ref, the call's
owner_principal. This wakes an agent; it never notifies the owner.
"""
from datetime import datetime,timedelta,timezone
import json
import re
import sys
import uuid

SOURCE='deedkit:calls'
TERMINAL_TYPES=('call.finished','call.failed','call.not_answered','call.interrupted')
TYPES=(*TERMINAL_TYPES,'call.owner_request')
# Owner request states the ANYM-562 contract accepts now (module_events.py KINDS); the journal also keeps
# answered, expired and superseded rows, served once messaging widens the enum (follow-up card).
SERVED_STATES=frozenset(('pending',))
NOT_ANSWERED={'provider_no-answer':'no_answer','provider_busy':'busy','provider_canceled':'canceled'}
# Closed sets: an unlisted value is 'other', so no free text reaches an event.
END_REASONS=frozenset(('owner_stop','agent_stop','model_error','service_shutdown','service_restart','regression_end',
    'keypress_handover_failed','provider_rejected','provider_completed','provider_failed','dial_unknown',
    'duration_limit','credentials_removed','disconnected','connection_error','media_disconnected','media_error',
    'task_completed','awaiting_owner','counterparty_hangup','completed','needs_owner','refused','unavailable'))
OUTCOMES=frozenset(('unresolved','needs_owner','needs_summary','followup_ready','refused','unavailable','incomplete',
    'collected_needs_review'))
# An owner request is answerable for a day (owner decision 10-29, option A); then it is expired.
DECISION_TTL_S=86400
MAX_SEQ=2**53-1
# One scan holds the service lock for at most SCAN_ROWS primary-key rows (criterion 4, test_call_events).
MAX_LIMIT=200
SCAN_ROWS=200
# The router refuses a page over 256 KiB (compact JSON); a page stops below it.
PAGE_BYTES=240*1024
_SCOPE=re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,255}')


def create(db):
    db.execute('''CREATE TABLE IF NOT EXISTS call_events (seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT UNIQUE NOT NULL, call_id TEXT NOT NULL, type TEXT NOT NULL, occurred_at TEXT NOT NULL,
        owner_principal TEXT, rehearsal INTEGER NOT NULL, data TEXT NOT NULL, urgency TEXT NOT NULL, expires_at TEXT)''')
    db.execute('CREATE TABLE IF NOT EXISTS call_event_journal (id INTEGER PRIMARY KEY CHECK (id=1), journal_id TEXT NOT NULL, started_at TEXT NOT NULL)')
    db.execute('INSERT OR IGNORE INTO call_event_journal VALUES (1,?,?)',(uuid.uuid4().hex,datetime.now(timezone.utc).isoformat()))
    return db.execute('SELECT journal_id,started_at FROM call_event_journal WHERE id=1').fetchone()


def _time(value):
    """RFC 3339 in UTC with a Z, as the 562 validator reads it."""
    moment=datetime.fromisoformat(value).astimezone(timezone.utc)
    return moment.strftime('%Y-%m-%dT%H:%M:%S.%f')+'Z'


def deadline(decision):
    """The decision's stored deadline; a decision from before ANYM-563 gets created_at plus the TTL."""
    if decision.get('expires_at'):
        return decision['expires_at']
    return (datetime.fromisoformat(decision['created_at'])+timedelta(seconds=DECISION_TTL_S)).isoformat()


def terminal(v):
    """(type, data) of a terminal call; end_reason decides before status (card crit 2)."""
    reason=v.get('end_reason') or ''
    if reason in NOT_ANSWERED:
        return 'call.not_answered',{'call_id':v['id'],'end_reason':NOT_ANSWERED[reason]}
    if reason=='service_restart' or v['status']=='interrupted':
        return 'call.interrupted',{'call_id':v['id'],'end_reason':'service_restart'}
    token=reason if reason in END_REASONS else 'other'
    if reason=='provider_failed' or v['status']=='failed':
        return 'call.failed',{'call_id':v['id'],'status':v['status'],'end_reason':token}
    if v['status']!='finished':
        raise ValueError('not_terminal')
    data={'call_id':v['id'],'status':'finished','end_reason':token,
          'outcome':v['outcome'] if v.get('outcome') in OUTCOMES else 'other','transcript':'final'}
    try:
        seconds=(datetime.fromisoformat(v['ended_at'])-datetime.fromisoformat(v['started_at'])).total_seconds()
        data['duration_s']=min(86400,max(0,int(seconds)))
    except (KeyError,TypeError,ValueError):
        pass
    return 'call.finished',data


def _insert(db,key,v,kind,data,at,urgency,expires_at):
    db.execute('''INSERT OR IGNORE INTO call_events(event_key,call_id,type,occurred_at,owner_principal,rehearsal,data,
        urgency,expires_at) VALUES (?,?,?,?,?,?,?,?,?)''',
        (key,v['id'],kind,_time(at),v['brief'].get('owner_principal'),int(v.get('channel','browser')=='browser'),
         json.dumps(data,separators=(',',':')),urgency,expires_at and _time(expires_at)))


def _logged(write,code):
    """An event write never changes how a call ends or a decision is kept: a failure is logged only."""
    try:
        write()
        return True
    except Exception as error:
        print(json.dumps({'error':code,'class':type(error).__name__}),file=sys.stderr,flush=True)
        return False


def record_terminal(db,v):
    """One terminal row per call, in the caller's transaction (idempotent by event key)."""
    def write():
        kind,data=terminal(v)
        # The agent that placed the call waits for it: never held by batching.
        _insert(db,'terminal:'+v['id'],v,kind,data,v['ended_at'],'now',None)
    return _logged(write,'call_event_terminal_failed')


def record_owner_request(db,v,decision,state,at):
    """One row per decision state (pending, then answered, expired or superseded); no question or answer text."""
    def write():
        data={'call_id':v['id'],'decision_id':decision['id'],'state':state}
        # urgency=now and expires_at = the stored deadline: never batched, never delivered after it (562).
        _insert(db,f"owner_request:{decision['id']}:{state}",v,'call.owner_request',data,at,'now',deadline(decision))
    return _logged(write,'call_event_owner_request_failed')


def repair(db,calls,started_at,active):
    """Startup: a terminal call that ended after the journal began and has no terminal row gets one."""
    def write():
        have={row[0] for row in db.execute("SELECT call_id FROM call_events WHERE event_key LIKE 'terminal:%'")}
        for v in calls:
            if v['status'] not in active and v['id'] not in have and v.get('ended_at') and v['ended_at']>=started_at:
                record_terminal(db,v)
    return _logged(write,'call_event_repair_failed')


def head(db):
    row=db.execute("SELECT seq FROM sqlite_sequence WHERE name='call_events'").fetchone()
    return row[0] if row else 0


def _event(row,include_rehearsal):
    seq,_,_,kind,at,principal,rehearsal,data,urgency,expires_at=row
    data=json.loads(data)
    event={'specversion':'1.0','id':f'calls:{seq}','source':SOURCE,'type':kind,'subject':data['call_id'],'time':at,
           'data':data,'urgency':urgency,'expires_at':expires_at,'scope_ref':principal}
    if include_rehearsal:
        event['rehearsal']=bool(rehearsal)
    return event


def _served(row,include_rehearsal):
    _,_,_,kind,_,principal,rehearsal,data,_,_=row
    if not isinstance(principal,str) or _SCOPE.fullmatch(principal) is None:
        return False
    if rehearsal and not include_rehearsal:
        return False
    return kind!='call.owner_request' or json.loads(data).get('state') in SERVED_STATES


def scan(db,after):
    """The only step under the service lock: the head and one bounded range of rows by primary key."""
    last=head(db)
    rows=db.execute('SELECT * FROM call_events WHERE seq>? ORDER BY seq LIMIT ?',(after,SCAN_ROWS)).fetchall() if after<last else []
    return last,rows


def page(journal_id,after,limit,last,rows,include_rehearsal=False):
    """One page of the producers' cursor contract: {events, next_after, has_more, retention_floor, journal_id}.

    Built from scan() outside the lock. next_after moves past filtered rows; a cursor beyond the head reads
    the head, which the router takes as a source reset.
    """
    if after>=last:
        return {'events':[],'next_after':last,'has_more':False,'retention_floor':0,'journal_id':journal_id}
    events,position=[],after
    size=len(json.dumps({'events':[],'next_after':MAX_SEQ,'has_more':False,'retention_floor':0,
                         'journal_id':journal_id},separators=(',',':')))
    for row in rows:
        if _served(row,include_rehearsal):
            if len(events)==limit:
                break
            event=_event(row,include_rehearsal)
            grown=size+len(json.dumps(event,separators=(',',':')).encode())+1
            if grown>PAGE_BYTES:
                break
            events.append(event)
            size=grown
        position=row[0]
    return {'events':events,'next_after':position,'has_more':position<last,'retention_floor':0,'journal_id':journal_id}
