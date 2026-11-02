"""Durable provider receipt evidence. No network calls or message mutations."""
from datetime import datetime
import hashlib
import re

from psycopg.types.json import Jsonb

from .identifiers import ACCOUNT_RE, WA_CHAT_RE
from .envelope import canonical, utc

RANK={'unknown':-2,'pending':-1,'error':0,'server':1,'delivered':2,'read':3,'played':4}
FIELDS={'kind','account','platform','chat','provider_id','max_id','state','basis','audience',
        'observed_at','provider_at'}
RANGE_SQL='''SELECT m.* FROM messages m WHERE m.account=%s AND m.chat=%s
    AND m.direction='outgoing' AND NOT m.deleted AND
    CASE WHEN m.provider_id ~ '^[1-9][0-9]{0,18}$' THEN m.provider_id::numeric ELSE NULL END>%s
    AND CASE WHEN m.provider_id ~ '^[1-9][0-9]{0,18}$' THEN m.provider_id::numeric ELSE NULL END<=%s
    ORDER BY CASE WHEN m.provider_id ~ '^[1-9][0-9]{0,18}$' THEN m.provider_id::numeric ELSE NULL END,m.id
    LIMIT %s'''
SELF_CHAT_SQL='''(coalesce(ro.is_self_chat,false) OR coalesce(md.metadata->'is_self_chat'='true'::jsonb,false))'''
ROW_PROOFS_SQL='''SELECT m.id,r.state,r.basis,r.audience,r.observed_at,'''+SELF_CHAT_SQL+''' AS self_chat
    FROM messages m JOIN chats ch ON ch.id=m.chat AND ch.account=m.account JOIN accounts a ON a.id=m.account
    LEFT JOIN chat_read_observations ro ON ro.chat=m.chat
    LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
    LEFT JOIN LATERAL (
        (SELECT r.state,r.basis,r.audience,r.observed_at FROM message_receipts r
            WHERE r.account=m.account AND r.provider_chat=ch.provider_chat AND r.platform='telegram'
            AND a.platform='telegram' AND m.direction='outgoing'
            AND r.max_id>=CASE WHEN m.provider_id ~ '^[1-9][0-9]{0,18}$'
                THEN CASE WHEN m.provider_id::numeric<9223372036854775808
                    THEN m.provider_id::bigint ELSE NULL END ELSE NULL END ORDER BY r.max_id LIMIT 1)
        UNION ALL
        (SELECT DISTINCT ON(r.state) r.state,r.basis,r.audience,r.observed_at FROM message_receipts r
            WHERE r.account=m.account AND r.provider_chat=ch.provider_chat AND r.platform='whatsapp'
            AND a.platform='whatsapp' AND m.direction='outgoing' AND r.provider_id=m.provider_id
            ORDER BY r.state,r.observed_at,r.id)
    ) r ON true WHERE m.id=ANY(%s)'''
REFERENCE_TG_SQL='''SELECT r.state,r.basis,r.audience,r.observed_at FROM message_receipts r
    JOIN accounts a ON a.id=r.account AND a.platform=r.platform
    WHERE r.account=%s AND r.provider_chat=%s AND r.platform='telegram' AND r.max_id>=%s
    ORDER BY r.max_id LIMIT 1'''


def validate_capture(value):
    if not isinstance(value,dict) or set(value)!=FIELDS or value['kind']!='message_receipt':
        raise ValueError('invalid_receipt')
    if not isinstance(value['account'],str) or not ACCOUNT_RE.fullmatch(value['account']):
        raise ValueError('invalid_receipt_account')
    chat=value['chat']
    if value['provider_at'] is not None:raise ValueError('unsupported_receipt_provider_time')
    if value['platform']=='telegram':
        if not isinstance(chat,str) or not re.fullmatch(r'[1-9][0-9]{0,18}',chat) or int(chat)>=2**63:
            raise ValueError('invalid_receipt_chat')
        if (value['provider_id'] is not None or type(value['max_id']) is not int or not 0<value['max_id']<2**63
                or value['state']!='read' or value['basis']!='telegram_outbox_boundary'
                or value['audience']!='private_peer'):
            raise ValueError('invalid_receipt_reference')
    elif value['platform']=='whatsapp':
        if not isinstance(chat,str) or not WA_CHAT_RE.fullmatch(chat):
            raise ValueError('invalid_receipt_chat')
        reference=value['provider_id']
        prefix='true_'+chat+'_'
        if (not isinstance(reference,str) or not 1<=len(reference)<=256
                or not re.fullmatch(r'[a-zA-Z0-9_.:@-]+',reference)
                or not reference.startswith(prefix) or len(reference)<=len(prefix)
                or value['max_id'] is not None or not isinstance(value['state'],str)
                or value['state'] not in RANK or value['state']=='unknown'
                or value['basis']!='whatsapp_message_ack'
                or value['audience']!=('provider_aggregate' if chat.endswith('@g.us') else 'private_peer')):
            raise ValueError('invalid_receipt_reference')
    else:raise ValueError('invalid_receipt_platform')
    return {**value,'observed_at':utc(value['observed_at'])}


def _evidence(rows):
    proof={'state':'unknown','basis':None,'audience':None,'recipient_delivery_confirmed':False,
        'observed_at':None,'provider_at':None,
        'delivered_at':None,'read_at':None,'played_at':None,
        'delivered_observed_at':None,'read_observed_at':None,'played_observed_at':None}
    if not rows:return proof
    strongest=max(rows,key=lambda r:RANK[r['state']])
    observed=strongest['observed_at']
    proof.update(state=strongest['state'],basis=strongest['basis'],audience=strongest['audience'],
        observed_at=observed.isoformat() if isinstance(observed,datetime) else observed,
        recipient_delivery_confirmed=strongest['audience']=='private_peer' and RANK[strongest['state']]>=RANK['delivered'])
    for state in ('delivered','read','played'):
        # A later stage implies arrival but does not invent the exact earlier
        # timestamp. Only a directly observed stage supplies its observation.
        times=[r['observed_at'] for r in rows if r['state']==state]
        if times:
            at=min(times);proof[state+'_observed_at']=at.isoformat() if isinstance(at,datetime) else at
    return proof


def self_chat_evidence(proof):
    """A message to one's own chat has no recipient. Provider ACKs from the owner's
    devices differ by account and device, so recipient delivery is not applicable."""
    return {**proof,'provider_state':proof['state'],'state':'not_applicable','basis':'self_chat',
        'audience':'self','recipient_delivery_confirmed':False}


def unknown_evidence():
    """Fresh explicit unknown object for queued/unobserved messages."""
    return _evidence([])


def record(c,raw):
    """Caller holds the account's archive lock and fenced collector lease."""
    value=validate_capture(raw)
    digest=hashlib.sha256(canonical({k:v for k,v in value.items() if k!='observed_at'})).hexdigest()
    previous=c.execute('''SELECT max(max_id) AS max_id FROM message_receipts
        WHERE account=%s AND provider_chat=%s AND platform='telegram' ''',
        (value['account'],value['chat'])).fetchone()['max_id'] or 0
    exact=[]
    if value['platform']=='whatsapp':
        exact=c.execute('''SELECT state FROM message_receipts WHERE account=%s
            AND provider_chat=%s AND provider_id=%s AND platform='whatsapp' ''',
            (value['account'],value['chat'],value['provider_id'])).fetchall()
    added=c.execute('''INSERT INTO message_receipts(id,account,platform,provider_chat,provider_id,max_id,
        state,basis,audience,observed_at,provider_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL)
        ON CONFLICT(id) DO NOTHING RETURNING id''',
        (digest,value['account'],value['platform'],value['chat'],value['provider_id'],value['max_id'],
         value['state'],value['basis'],value['audience'],value['observed_at'])).fetchone()
    advances=(value['max_id']>previous if value['platform']=='telegram'
        else not exact or RANK[value['state']]>max(RANK[r['state']] for r in exact))
    if added and advances:
        c.execute('''INSERT INTO receipt_event_jobs(receipt,account,previous_max,cursor_id)
            VALUES(%s,%s,%s,%s)''',(digest,value['account'],previous,previous))
    return {'outcome':'receipt_recorded' if added else 'duplicate_receipt','receipt':digest}


def for_rows(c,rows):
    """One bounded query over authorized message IDs, independent of projections."""
    ids=list({r.get('id') or r.get('message') for r in rows}-{None})
    if not ids:return {}
    found=c.execute(ROW_PROOFS_SQL,(ids,)).fetchall()
    grouped={r['id']:[] for r in found}
    own={r['id'] for r in found if r['self_chat']}
    for row in found:
        if row['state'] is not None:grouped[row['id']].append(row)
    return {mid:self_chat_evidence(_evidence(proofs)) if mid in own else _evidence(proofs)
        for mid,proofs in grouped.items()}


def _reference_self_chat(c,account,provider_chat,provider_id):
    row=c.execute('''SELECT coalesce(bool_or('''+SELF_CHAT_SQL+'''),false) AS self_chat
        FROM chats ch LEFT JOIN chat_read_observations ro ON ro.chat=ch.id
        LEFT JOIN messages m ON m.chat=ch.id AND m.account=ch.account AND m.provider_id=%s
        LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
        WHERE ch.account=%s AND ch.provider_chat=%s''',(provider_id,account,provider_chat)).fetchone()
    return bool(row and row['self_chat'])


def for_reference(c,account,provider_chat,provider_id,platform=None):
    """Authorized outbox reference, including receipts captured before ingestion."""
    if platform is None:
        found=c.execute('SELECT platform FROM accounts WHERE id=%s',(account,)).fetchone()
        platform=found['platform'] if found else None
    if platform=='telegram':
        if not isinstance(provider_id,str) or not re.fullmatch(r'[1-9][0-9]{0,18}',provider_id):
            return unknown_evidence()
        if int(provider_id)>=2**63:return unknown_evidence()
        proof=_evidence(c.execute(REFERENCE_TG_SQL,(account,provider_chat,int(provider_id))).fetchall())
    elif platform=='whatsapp':
        proof=_evidence(c.execute('''SELECT DISTINCT ON(r.state) r.state,r.basis,r.audience,r.observed_at
            FROM message_receipts r JOIN accounts a ON a.id=r.account
            WHERE r.account=%s AND r.provider_chat=%s AND r.platform=a.platform
            AND r.platform='whatsapp' AND r.provider_id=%s
            ORDER BY r.state,r.observed_at,r.id''',
            (account,provider_chat,provider_id)).fetchall())
    else:return unknown_evidence()
    return self_chat_evidence(proof) if _reference_self_chat(c,account,provider_chat,provider_id) else proof


def publish(c,account,limit=100):
    """Drain at most 100 known outgoing messages, with durable restart cursors."""
    if type(limit) is not int or not 1<=limit<=100:raise ValueError('invalid_receipt_limit')
    jobs=c.execute('''SELECT j.*,r.platform,r.provider_chat,r.provider_id,r.max_id,r.state,
        r.basis,r.audience,r.observed_at FROM receipt_event_jobs j JOIN message_receipts r ON r.id=j.receipt
        JOIN account_policies p ON p.account=j.account AND p.enabled
        WHERE j.account=%s AND NOT j.complete AND NOT(r.provider_chat=ANY(p.exclude_chats))
        AND (p.discover_chats OR r.provider_chat=ANY(p.include_chats))
        ORDER BY r.recorded_at,r.id LIMIT %s FOR UPDATE OF j''',
        (account,limit)).fetchall()
    scanned=0
    for job in jobs:
        remaining=limit-scanned
        if not remaining:break
        if job['platform']=='telegram':
            # The existing unique chat_provider_lookup preserves old/custom IDs
            # while supplying a fixed equality prefix to the numeric range index.
            chat=c.execute('SELECT id FROM chats WHERE account=%s AND provider_chat=%s',
                (account,job['provider_chat'])).fetchone()
            rows=c.execute(RANGE_SQL,(account,chat['id'],job['cursor_id'],job['max_id'],remaining)).fetchall() if chat else []
            for row in rows:row['provider_chat']=job['provider_chat']
        else:
            rows=c.execute('''SELECT m.*,ch.provider_chat FROM messages m JOIN chats ch
                ON ch.id=m.chat AND ch.account=m.account WHERE m.account=%s AND ch.provider_chat=%s
                AND m.provider_id=%s AND m.direction='outgoing' AND NOT m.deleted LIMIT 1''',
                (account,job['provider_chat'],job['provider_id'])).fetchall()
        proofs=for_rows(c,rows)
        for row in rows:
            scanned+=1
            previous=c.execute('SELECT state FROM receipt_event_publications WHERE message=%s',(row['id'],)).fetchone()
            receipt_evidence=proofs[row['id']]
            if receipt_evidence['basis']=='self_chat':continue  # No recipient status to publish.
            if previous and RANK[previous['state']]>=RANK[receipt_evidence['state']]:continue
            snapshot={k:row[k] for k in ('account','chat','body','revision','sent_at','deleted','direction','sender','provider_id')}
            snapshot.update(message=row['id'],provider_chat=row['provider_chat'],delivery_receipt=receipt_evidence)
            from .store import serial
            sequence=c.execute('UPDATE feed_counter SET value=value+1 WHERE id=1 RETURNING value').fetchone()['value']
            c.execute('''INSERT INTO events(id,account,message,kind,revision,origin,snapshot,event_key)
                VALUES(%s,%s,%s,'message.status',%s,'live',%s,%s)''',
                (sequence,account,row['id'],row['revision'],Jsonb(serial(snapshot)),
                 'receipt:'+job['receipt']+':'+row['id']))
            c.execute('''INSERT INTO receipt_event_publications(message,receipt,state) VALUES(%s,%s,%s)
                ON CONFLICT(message) DO UPDATE SET receipt=excluded.receipt,state=excluded.state,
                    published_at=clock_timestamp()''',(row['id'],job['receipt'],receipt_evidence['state']))
        cursor=int(rows[-1]['provider_id']) if rows and job['platform']=='telegram' else job['cursor_id']
        complete=job['platform']=='whatsapp' or len(rows)<remaining or cursor>=job['max_id']
        c.execute('UPDATE receipt_event_jobs SET cursor_id=%s,complete=%s WHERE receipt=%s',
            (cursor,complete,job['receipt']))
    return scanned
