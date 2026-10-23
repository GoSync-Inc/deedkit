"""Durable event order with immutable per-consumer filter binding."""
from __future__ import annotations

import re
import secrets

from psycopg.types.json import Jsonb

from .policy import scope_filter,targets
from .search_query import coverage,filter_values,output_options,shape_messages


def get_updates(store,c,p,consumer,limit=20,*,accounts=None,chats=None,senders=None,
                origins=None,kinds=None,direction='',from_time='',to_time='',compact=False,
                body_chars=None,include_coverage=True,exclude_channels=False,order='event',
                reset_filters=False,expected_ack_version=None,start_from='beginning',view='events'):
    from .store import APIError,bounded,date_bound,digest,serial
    from .message_queries import enrich
    bounded(limit);cap=output_options(compact,body_chars,include_coverage)
    if not isinstance(consumer,str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',consumer):
        raise APIError('invalid_consumer')
    if not isinstance(view,str) or view not in ('events','by_chat'):raise APIError('invalid_filter')
    if view=='by_chat' and (origins is not None or kinds is not None or direction=='outgoing' or order!='event'
            or reset_filters is not False or expected_ack_version is not None or start_from!='beginning'):
        # The overview counts created incoming messages only and issues no receipt.
        raise APIError('invalid_filter')
    if not isinstance(direction,str) or direction not in ('','incoming','outgoing'):
        raise APIError('invalid_filter')
    if type(exclude_channels) is not bool or type(reset_filters) is not bool or order not in ('event','sent_at'):
        raise APIError('invalid_filter')
    if not isinstance(start_from,str) or start_from not in ('beginning','now'):
        raise APIError('invalid_filter')
    if start_from=='now' and 'cursor-write' not in p['permissions']:raise APIError('forbidden',403)
    if start_from=='now' and reset_filters:raise APIError('invalid_filter')
    if reset_filters:
        if 'cursor-write' not in p['permissions']:raise APIError('forbidden',403)
        if type(expected_ack_version) is not int or expected_ack_version<0:raise APIError('invalid_version')
    elif expected_ack_version is not None:raise APIError('invalid_version')
    lower,upper=date_bound(from_time),date_bound(to_time)
    if lower and upper and lower>=upper:raise APIError('invalid_date_range')
    filters={}
    for name,values in [('accounts',accounts),('chats',chats),('senders',senders),('origins',origins),('kinds',kinds)]:
        values=filter_values(values)
        allowed={'origins':{'live','backfill','reconcile','fixture'},
            'kinds':{'message.created','message.edited','message.deleted','message.status','attachment.ready'}}
        if values is not None and name in allowed and set(values)-allowed[name]:raise APIError('invalid_filter')
        if values is not None:filters[name]=values
    if exclude_channels:filters['exclude_channels']=True
    if direction:filters['direction']=direction
    if lower:filters['from_time']=lower.isoformat()
    if upper:filters['to_time']=upper.isoformat()
    # The overview's own text default (80) applies on every transport; compact's 320 does not.
    if view=='by_chat':return overview(store,c,p,consumer,limit,filters,lower,upper,80 if body_chars is None else body_chars)
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('consumers:'+p['id'],))
    total=c.execute('SELECT count(DISTINCT name) AS n FROM consumers WHERE principal=%s',(p['id'],)).fetchone()['n']
    existing=c.execute('SELECT 1 FROM consumers WHERE principal=%s AND name=%s',(p['id'],consumer)).fetchone()
    if existing and start_from=='now':raise APIError('consumer_already_initialized',409)
    if not existing and total>=64:raise APIError('consumer_limit',429)
    # The transactional sequencer's visible value is the committed event head.
    # Do not lock it: an in-flight ingestion must remain after this boundary.
    head=c.execute('SELECT value FROM feed_counter WHERE id=1').fetchone()['value']
    c.execute('INSERT INTO consumers(principal,name,scope_version,filter_spec,position) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
        (p['id'],consumer,p['scope_version'],Jsonb(filters),head if start_from=='now' else 0))
    state=c.execute('SELECT * FROM consumers WHERE principal=%s AND name=%s AND scope_version=%s FOR UPDATE',
        (p['id'],consumer,p['scope_version'])).fetchone()
    if reset_filters:
        if not existing or state['version']!=expected_ack_version:raise APIError('stale_ack',409)
        c.execute('UPDATE consumers SET filter_spec=%s,version=version+1 WHERE principal=%s AND name=%s AND scope_version=%s',
            (Jsonb(filters),p['id'],consumer,p['scope_version']))
        c.execute('DELETE FROM deliveries WHERE principal=%s AND consumer=%s AND scope_version=%s',
            (p['id'],consumer,p['scope_version']))
        state={**state,'filter_spec':filters,'version':state['version']+1}
    if state['filter_spec']!=filters:
        # Even with position zero an outstanding receipt may already bind the
        # previous selection. A new name is the explicit way to start a view.
        raise APIError('consumer_filter_mismatch',409)
    where,args=scope_filter(p,'m');clauses=[where,'m.account=e.account']
    for key,column in [('accounts','m.account'),('chats','m.chat'),('senders','m.sender'),
                       ('origins','e.origin'),('kinds','e.kind')]:
        if key in filters:clauses.append(column+'=ANY(%s)');args.append(filters[key])
    # An unspecified legacy selection remains the same five event kinds after
    # the journal gains processing events. Keep the persisted filter binding
    # unchanged so existing consumers and outstanding ACKs remain valid.
    if 'kinds' not in filters:
        clauses.append('e.kind=ANY(%s)')
        args.append(['message.created','message.edited','message.deleted',
                     'message.status','attachment.ready'])
    if direction:clauses.append('m.direction=%s');args.append(direction)
    if exclude_channels:
        clauses.append("EXISTS(SELECT 1 FROM chats ch WHERE ch.id=m.chat AND ch.account=m.account AND ch.chat_type IS DISTINCT FROM 'channel')")
    for bound,op in [(lower,'>='),(upper,'<')]:
        if bound:clauses.append('m.sent_at'+op+'%s');args.append(bound)
    events=c.execute('''SELECT e.id,e.kind,e.snapshot,e.origin,m.sender
        FROM events e JOIN messages m ON m.id=e.message WHERE e.id>%s AND e.id<=%s AND '''+
        ' AND '.join(clauses)+' ORDER BY e.id LIMIT %s',[state['position'],head,*args,limit+1]).fetchall()
    more=len(events)>limit;end=events[limit-1]['id'] if more else head
    receipt=secrets.token_urlsafe(32)
    c.execute('DELETE FROM deliveries WHERE expires_at<now()')
    c.execute("INSERT INTO deliveries VALUES(%s,%s,%s,%s,%s,%s,%s,now()+interval '15 minutes',false)",
        (digest(receipt),p['id'],consumer,p['scope_version'],state['position'],end,state['version']))
    public_events=[{**e['snapshot'],'kind':e['kind'],'origin':e['origin'],'sender':e['sender'],
        'event_sequence':e['id'],'is_backfill':e['origin'] in ('backfill','reconcile'),
        'historical':e['origin'] in ('backfill','reconcile')} for e in events[:limit]]
    names=enrich(c,[{'id':e['message']} for e in public_events],p)
    for event,named in zip(public_events,names):
        for field in ('sender_name','sender_name_basis','username','chat_title','chat_type'):
            event[field]=named.get(field)
    from .result_evidence import send_provenance,delivery_evidence,formatting_evidence
    send_provenance(c,public_events)
    delivery_evidence(c,public_events)
    formatting_evidence(c,public_events)
    shape_messages(public_events,compact=compact,body_chars=cap)
    if order=='sent_at':public_events.sort(key=lambda e:(date_bound(e['sent_at']),e['event_sequence']))
    targets(p,[e['message'] for e in public_events])
    return serial({'consumer':consumer,'items':public_events,'has_more':more,'receipt':receipt,
        'ack_version':state['version'],'filters':filters,'order':'ingestion_event_sequence',
        'presentation_order':order,'chronological_scope':'delivered_batch_only' if order=='sent_at' else None,
        'filter_binding':'version_checked_reset_preserves_ack_position',**coverage(store,c,p,include_coverage)})


def overview(store,c,p,consumer,limit,filters,lower,upper,width):
    """Per chat counts of created incoming messages after the consumer's ACK (ANYM-458).

    Read only: no consumer row, delivery or receipt is written, so no cursor moves.
    Live and backfill (backfill or reconcile origin, as is_backfill) are counted apart.
    """
    from .store import APIError,serial
    from .chat_queries import read_state_sql
    from .message_queries import enrich
    state=c.execute('SELECT position FROM consumers WHERE principal=%s AND name=%s AND scope_version=%s',
        (p['id'],consumer,p['scope_version'])).fetchone()
    position=state['position'] if state else 0
    # At the journal start (no consumer, or one at position 0) the window is the whole
    # journal: from_time bounds it.
    if not position and lower is None:
        failure=APIError('invalid_filter');failure.fields=['from_time'];raise failure
    head=c.execute('SELECT value FROM feed_counter WHERE id=1').fetchone()['value']
    where,args=scope_filter(p,'m')
    clauses=[where,'m.account=e.account',"e.kind='message.created'","m.direction='incoming'"]
    for key,column in [('accounts','m.account'),('chats','m.chat'),('senders','m.sender')]:
        if key in filters:clauses.append(column+'=ANY(%s)');args.append(filters[key])
    if 'exclude_channels' in filters:
        clauses.append("EXISTS(SELECT 1 FROM chats ch WHERE ch.id=m.chat AND ch.account=m.account AND ch.chat_type IS DISTINCT FROM 'channel')")
    for bound,op in [(lower,'>='),(upper,'<')]:
        if bound:clauses.append('m.sent_at'+op+'%s');args.append(bound)
    read_sources,fresh,unread=read_state_sql(c)
    rows=c.execute('''WITH sel AS MATERIALIZED (SELECT m.account,m.chat,m.id,m.sent_at,
          e.origin IN ('backfill','reconcile') AS backfill
        FROM events e JOIN messages m ON m.id=e.message WHERE e.id>%s AND e.id<=%s AND '''+' AND '.join(clauses)+'''),
      per AS (SELECT account,chat,count(*) FILTER (WHERE NOT backfill) AS live,count(*) FILTER (WHERE backfill) AS backfill,
          max(sent_at) FILTER (WHERE NOT backfill) AS newest_at,max(sent_at) AS latest_at FROM sel GROUP BY account,chat)
      SELECT per.*,count(*) OVER () AS chats_total,sum(per.live) OVER () AS live_total,sum(per.backfill) OVER () AS backfill_total,
        (SELECT s.id FROM sel s WHERE s.account=per.account AND s.chat=per.chat AND NOT s.backfill
          ORDER BY s.sent_at DESC,s.id DESC LIMIT 1) AS newest_message,
        (SELECT s.id FROM sel s WHERE s.account=per.account AND s.chat=per.chat ORDER BY s.sent_at DESC,s.id DESC LIMIT 1) AS head_message,
        answer.replied AS owner_replied_after_newest,ro.unread_count AS provider_unread_count,'''+unread+''' AS provider_unread,ro.observed_at AS read_state_observed_at,
        CASE WHEN '''+fresh+''' THEN 'observed' WHEN ro.observed_at IS NULL THEN 'unknown' ELSE 'stale' END AS read_state
      FROM per JOIN chats c ON c.id=per.chat AND c.account=per.account
      LEFT JOIN LATERAL (SELECT im.sent_at FROM messages im WHERE im.account=c.account AND im.chat=c.id
        AND im.direction='incoming' ORDER BY im.sent_at DESC LIMIT 1) incoming ON true
      LEFT JOIN chat_read_observations ro ON ro.account=c.account AND ro.chat=c.id
      LEFT JOIN LATERAL (SELECT true AS replied FROM messages om WHERE om.account=c.account AND om.chat=c.id
        AND om.direction='outgoing' AND om.sent_at>per.newest_at LIMIT 1) answer ON true
      ORDER BY per.newest_at DESC NULLS LAST,per.latest_at DESC,per.chat LIMIT %s''',
        [position,head,*args,limit+1]).fetchall()
    more=len(rows)>limit;rows=rows[:limit]
    ids=[r['newest_message'] or r['head_message'] for r in rows]
    names=enrich(c,[{'id':mid} for mid in ids],p)
    bodies={r['id']:r for r in c.execute('SELECT id,body,sent_at,sender FROM messages WHERE id=ANY(%s)',
        ([r['newest_message'] for r in rows if r['newest_message']],)).fetchall()}
    labels={r['message']:'['+(r['filename'] or 'attachment')+']' for r in c.execute('''SELECT DISTINCT ON (message)
        message,filename FROM attachments WHERE message=ANY(%s) ORDER BY message,id''',(list(bodies),)).fetchall()}
    chats=[]
    for r,named in zip(rows,names):
        newest=None
        if r['newest_message']:
            # A media-only message shows its file label, as compact message rows do.
            m=bodies[r['newest_message']];text=m['body'] or labels.get(m['id'])
            if text and len(text)>width:text=text[:width].rstrip()+'…' if width else None
            newest=compact_row({'message':m['id'],'sent_at':m['sent_at'],'sender':m['sender'],
                'sender_name':(named.get('sender_name') or '')[:64] or None,'text':text})
        # An absent read field is unknown; provider mute and archive are never collected.
        chats.append({**compact_row({'chat':r['chat'],'account':r['account'],'chat_title':(named.get('chat_title') or '')[:72] or None,
            'chat_type':named.get('chat_type'),'live':r['live'],'backfill':r['backfill'],'read_state':r['read_state'],
            'owner_replied_after_newest':r['owner_replied_after_newest'],
            'provider_unread':r['provider_unread'],'provider_unread_count':r['provider_unread_count'],
            'read_state_observed_at':r['read_state_observed_at']}),'newest':newest})
    targets(p,[mid for mid in ids if mid])
    total=rows[0] if rows else {'chats_total':0,'live_total':0,'backfill_total':0}
    # An empty account may be a quiet one or a stopped collector: each scoped account's
    # source state and last ingest let the caller tell them apart.
    sources=[compact_row({'account':r['id'],'status':r['status'],'last_ingest_at':r['last_ingest_at']})
        for r in store.dataset(c,p)['sources'] if 'accounts' not in filters or r['id'] in filters['accounts']]
    basis=('acknowledged_position_and_from_time' if lower else 'acknowledged_position') if state else 'from_time'
    return serial({'consumer':consumer,'view':'by_chat','window':basis,'chats':chats,'has_more':more,'chats_total':total['chats_total'],
        'live_total':int(total['live_total']),'backfill_total':int(total['backfill_total']),'filters':filters,
        'counts':'created_incoming_messages_after_position','accounts':sources,'provider_mute_archive':'unknown','absent_read_fields':'unknown'})


def compact_row(row):return {k:v for k,v in row.items() if v is not None}
