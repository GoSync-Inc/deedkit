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
                reset_filters=False,expected_ack_version=None,start_from='beginning'):
    from .store import APIError,bounded,date_bound,digest,serial
    from .message_queries import enrich
    bounded(limit);cap=output_options(compact,body_chars,include_coverage)
    if not isinstance(consumer,str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',consumer):
        raise APIError('invalid_consumer')
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
