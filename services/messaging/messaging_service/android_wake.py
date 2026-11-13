"""Paused-by-default Android metadata outbox in the EXISTING delivery process. No subscription or native action."""
from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import UUID

from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import delivery_endpoints as endpoints
from .store import APIError

REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
FIELDS = frozenset(('wake_id','task_id','request_id','order_id','owner_id','device_id','source_id','auth_epoch','kind',
                    'native_thread_id','native_turn_id','native_call_id','item_id','message_id','native_request_id','question_ids',
                    'expires_at'))


def tables(c):
    row=c.execute("SELECT to_regclass('public.android_wake_intents') IS NOT NULL AS ready").fetchone()
    return bool(row and row['ready'])


def ready(c, *, dispatcher=False):
    if not tables(c): return False
    from .store import RUNTIME_SCHEMA, schema_version
    from . import heartbeats
    if schema_version(c) != RUNTIME_SCHEMA or RUNTIME_SCHEMA < 97: return False
    if not dispatcher: return heartbeats.mismatch(c) is None
    return c.execute('''SELECT 1 FROM component_heartbeats WHERE component='dispatcher'
        AND schema_version=%s AND version=%s AND beat_at>clock_timestamp()-interval '120 seconds' ''',
        (RUNTIME_SCHEMA, heartbeats.identity())).fetchone() is not None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def origin(item):
    """Actual native questions/control calls cannot escape an uncertain attempt by minting another wake ID."""
    common=[item[k] for k in ('device_id','task_id','request_id','native_thread_id')]
    if item['kind']=='question':
        if item['native_request_id'] is None and not (item['item_id'] or item['message_id']):
            raise APIError('invalid_android_wake')
        value=common+[item[k] for k in ('kind','native_turn_id','item_id','message_id','native_request_id','question_ids')]
    elif item['native_call_id'] is not None:
        value=common+['control',item['native_call_id']]
    else: return None  # Observation wakes are coalesced/persisted by the trusted source, never regenerated here.
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate(item):
    if not isinstance(item, dict) or set(item) != FIELDS: raise APIError('invalid_android_wake')
    for field in FIELDS - {'order_id','native_turn_id','native_call_id','item_id','message_id','native_request_id',
                            'question_ids','auth_epoch','expires_at'}:
        if not isinstance(item[field], str) or REF.fullmatch(item[field]) is None:
            raise APIError('invalid_android_wake')
    for field in ('order_id','native_turn_id','native_call_id','item_id','message_id'):
        if item[field] is not None and (not isinstance(item[field], str) or not REF.fullmatch(item[field])):
            raise APIError('invalid_android_wake')
    if type(item['native_request_id']) not in (int,str,type(None)):
        raise APIError('invalid_android_wake')
    if isinstance(item['native_request_id'], str) and not REF.fullmatch(item['native_request_id']):
        raise APIError('invalid_android_wake')
    if (type(item['auth_epoch']) is not int or item['auth_epoch'] < 1 or item['kind'] not in
            ('question','status','handoff','observation') or not isinstance(item['question_ids'], list)
            or len(item['question_ids'])>32
            or any(not isinstance(q,str) or not REF.fullmatch(q) for q in item['question_ids'])
            or len(set(item['question_ids']))!=len(item['question_ids'])):
        raise APIError('invalid_android_wake')
    try:
        if str(UUID(item['wake_id'])) != item['wake_id']: raise ValueError
        until = datetime.fromisoformat(item['expires_at'].replace('Z','+00:00'))
        if until.tzinfo is None or not 1970 <= until.year <= 9998: raise ValueError
        text = canonical(item)
        if len(text.encode()) > 4096: raise ValueError
    except (ValueError,TypeError,AttributeError): raise APIError('invalid_android_wake') from None
    return hashlib.sha256(text.encode()).hexdigest(), until


def _binding(c,p,device,*,update=False):
    if not isinstance(device,str) or not REF.fullmatch(device): raise APIError('invalid_android_wake')
    query=('''SELECT *,valid_until>clock_timestamp() AS live FROM android_wake_bindings
        WHERE principal=%s AND key_id=%s AND device_id=%s FOR UPDATE''' if update else
        '''SELECT *,valid_until>clock_timestamp() AS live
        FROM public.lock_android_wake_binding(%s,%s,%s)''')
    row=c.execute(query,(p['id'],p['key_id'],device)).fetchone()
    if (not row or row['state']!='active' or not row['routine_verified'] or not row['installed_verified']
            or not row['live']): raise APIError('android_wake_unavailable',409)
    return row


def _view(row):
    return {'wake_id':str(row['wake_id']),'identity_hash':row['identity_hash'],'identity':row['identity'],
            'transport_state':row['state'],'hold_reason':row['hold_reason'],'native_status':'not_observed'}


def _owned(c,p,wake):
    try: ident=str(UUID(wake))
    except (ValueError,TypeError,AttributeError): raise APIError('invalid_android_wake') from None
    return c.execute('SELECT * FROM android_wake_intents WHERE principal=%s AND key_id=%s AND wake_id=%s',
                     (p['id'],p['key_id'],ident)).fetchone()


def call(store,token,action,body):
    if not isinstance(body,dict): raise APIError('invalid_android_wake')
    with store.authorized(token) as (c,p):
        if not {'read','delivery-write'}<=set(p['permissions']): raise APIError('forbidden',403)
        if not tables(c): raise APIError('android_wake_unavailable',409)
        if action=='receipt':
            if set(body)!={'wake_id'}: raise APIError('invalid_android_wake')
            row=_owned(c,p,body['wake_id'])
            return _view(row) if row else {'transport_state':'not_found'}
        if action not in ('state','stage'): raise APIError('not_found',404)
        if action=='state' and set(body)!={'device_id'}: raise APIError('invalid_android_wake')
        binding=_binding(c,p,body.get('device_id'),update=action=='stage')
        if action=='state': return {'journal_id':binding['journal_id'],'after':binding['after_seq']}
        return _stage(c,p,binding,body)


def _stage(c,p,binding,body):
    if (set(body)!={'device_id','journal_id','after','next_after','events'}
            or not isinstance(body['journal_id'],str) or not REF.fullmatch(body['journal_id'])
            or type(body['after']) is not int or type(body['next_after']) is not int
            or not 0<=body['after']<=body['next_after'] or not isinstance(body['events'],list)
            or not 1<=len(body['events'])<=4): raise APIError('invalid_android_wake')
    if not ready(c): raise APIError('android_wake_not_ready',409)
    # Serialize identical and overlapping gateway generations before row-level state checks.
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('subscription-hints:'+str(p['key_id']),))
    binding=c.execute('''SELECT * FROM android_wake_bindings WHERE principal=%s AND key_id=%s AND device_id=%s
        FOR UPDATE''',(p['id'],p['key_id'],binding['device_id'])).fetchone()
    result=[]; previous=body['after']; new=[]; seen={}; origins=set()
    for raw in body['events']:
        if not isinstance(raw,dict) or type(raw.get('sequence')) is not int: raise APIError('invalid_android_wake')
        sequence=raw['sequence']; item={k:v for k,v in raw.items() if k!='sequence'}
        digest,until=validate(item)
        if not previous<sequence<=body['next_after']: raise APIError('invalid_android_wake')
        previous=sequence
        if any(item[k]!=binding[k] for k in ('owner_id','device_id','source_id','auth_epoch')):
            raise APIError('android_wake_binding_changed',409)
        row=_owned(c,p,item['wake_id'])
        if row:
            if row['identity_hash']!=digest: raise APIError('android_wake_identity_conflict',409)
            if row['endpoint']!=binding['endpoint'] or row['binding_generation']!=binding['generation']:
                raise APIError('android_wake_binding_changed',409)
            result.append(_view(row)); continue
        if until<=datetime.now(timezone.utc): raise APIError('android_wake_expired',409)
        original=origin(item)
        if item['wake_id'] in seen:
            if seen[item['wake_id']]!=digest: raise APIError('android_wake_identity_conflict',409)
            continue
        if original and (original in origins or c.execute('''SELECT 1 FROM android_wake_intents
                WHERE principal=%s AND key_id=%s AND origin_hash=%s''',(p['id'],p['key_id'],original)).fetchone()):
            raise APIError('android_wake_origin_conflict',409)
        seen[item['wake_id']]=digest; origins.add(original)
        new.append((item,digest,until,sequence,original))
    if not new: return {'items':result,'after':binding['after_seq']}
    if binding['journal_id'] not in (None,body['journal_id']) or binding['after_seq']!=body['after']:
        raise APIError('android_wake_source_gap',409)
    target=c.execute('''SELECT 1 FROM delivery_endpoints WHERE id=%s AND principal=%s AND key_id=%s
        AND state='enabled' ''',(binding['endpoint'],p['id'],p['key_id'])).fetchone()
    if not target: raise APIError('android_wake_unavailable',409)
    from .event_delivery import endpoint_queue_full
    from .delivery_policy import budget_slot
    owner={'principal':p['id'],'key_id':p['key_id']}
    if endpoint_queue_full(c,owner,adding=len(new)): raise APIError('rate_limited',429)
    for item,digest,until,sequence,original in new:
        if budget_slot(c,owner,datetime.now(timezone.utc)): raise APIError('rate_limited',429)
        row=c.execute('''INSERT INTO android_wake_intents(principal,key_id,wake_id,device_id,endpoint,
            binding_generation,identity_hash,origin_hash,identity,journal_id,source_seq,expires_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (p['id'],p['key_id'],item['wake_id'],binding['device_id'],binding['endpoint'],binding['generation'],
             digest,original,Jsonb(item),body['journal_id'],sequence,until)).fetchone()
        result.append(_view(row))
    c.execute('''UPDATE android_wake_bindings SET journal_id=%s,after_seq=%s
        WHERE principal=%s AND key_id=%s AND device_id=%s''',
        (body['journal_id'],body['next_after'],p['id'],p['key_id'],binding['device_id']))
    from .wake import mark,OUTBOX
    mark(c,OUTBOX)
    return {'items':result,'after':body['next_after']}


def body(row):
    return canonical({'module':'android','type':'android_wake','wake_id':str(row['wake_id']),
                      'kind':row['identity']['kind'],'next_call':'android_agent'}).encode()


def busy(c,principal,key):
    return tables(c) and c.execute('''SELECT 1 FROM android_wake_intents WHERE principal=%s AND key_id=%s
        AND state='sending' AND lease_until>clock_timestamp()''',(principal,key)).fetchone() is not None


def _settle(c,row,state,reason=None,delay=2):
    c.execute('''UPDATE android_wake_intents SET state=%s,hold_reason=%s,lease_until=NULL,
        next_attempt_at=clock_timestamp()+make_interval(secs=>%s) WHERE principal=%s AND key_id=%s AND wake_id=%s''',
        (state,reason,delay,row['principal'],row['key_id'],row['wake_id']))


def dispatch(store,*,transport,resolver,served=None):
    """One bounded sender turn in an existing endpoint lane. No standalone worker."""
    if getattr(store,'pool',None) is None: return {'android_unavailable':1}
    from .event_delivery import ENDPOINT_LANE_LOCK,KEY_IN_FLIGHT
    from .subscriptions import _worker_identity
    from .delivery_policy import admission_slot,host_admission_slot
    served={} if served is None else served
    with store.pool.connection() as c:
        if not tables(c): return {}
        # Never replay a process that may have written a request, including after worker restart.
        c.execute("""UPDATE android_wake_intents SET state='unknown',hold_reason='outcome_unknown',lease_until=NULL
            WHERE state='sending' AND lease_until<=clock_timestamp()""")
        if not ready(c,dispatcher=True): return {}
        c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(ENDPOINT_LANE_LOCK,))
        candidates=c.execute("""SELECT * FROM android_wake_intents WHERE state='pending'
            AND next_attempt_at<=clock_timestamp() ORDER BY created_at LIMIT 64 FOR UPDATE SKIP LOCKED""").fetchall()
        row=None
        for candidate in sorted(candidates,key=lambda r:served.get((r['principal'],r['key_id']),0)):
            if busy(c,candidate['principal'],candidate['key_id']): continue
            active=c.execute('SELECT '+KEY_IN_FLIGHT.format(principal='%s',key_id='%s')+' AS busy',
                (candidate['principal'],candidate['key_id'])*2).fetchone()['busy']
            if active: continue
            row=candidate;break
        if row is None: return {}
        now=datetime.now(timezone.utc)
        p=_worker_identity(c,row)
        try: binding=_binding(c,p or {'id':row['principal'],'key_id':row['key_id']},row['device_id'])
        except APIError: binding=None
        if (not p or not {'read','delivery-write'}<=set(p['permissions']) or binding is None
                or row['endpoint']!=binding['endpoint'] or row['binding_generation']!=binding['generation']
                or any(row['identity'][k]!=binding[k] for k in ('owner_id','source_id','auth_epoch'))):
            _settle(c,row,'cancelled','access_or_configuration_changed');return {'cancelled':1}
        if row['expires_at']<=now:
            _settle(c,row,'expired','expired');return {'cancelled':1}
        if row['admitted_at'] is None:
            later=admission_slot(c,row,now) or host_admission_slot(c,row['endpoint'],now)
            if later:
                c.execute('UPDATE android_wake_intents SET next_attempt_at=%s,hold_reason=\'budget\' '
                          'WHERE principal=%s AND key_id=%s AND wake_id=%s',
                          (later,row['principal'],row['key_id'],row['wake_id']))
                return {'deferred':1}
        c.execute("""UPDATE android_wake_intents SET state='sending',attempts=attempts+1,
            admitted_at=coalesce(admitted_at,clock_timestamp()),lease_until=clock_timestamp()+interval '1 minute'
            WHERE principal=%s AND key_id=%s AND wake_id=%s""",(row['principal'],row['key_id'],row['wake_id']))
        served[(row['principal'],row['key_id'])]=max(served.values(),default=0)+1
    # The durable sending intent above commits before preparation or external bytes.
    target,problem=endpoints.prepare(store,row['endpoint'],resolver)
    with store.pool.connection() as c:
        p=_worker_identity(c,row)
        live=c.execute("SELECT * FROM android_wake_intents WHERE principal=%s AND key_id=%s AND wake_id=%s "
                       "AND state='sending' FOR UPDATE",(row['principal'],row['key_id'],row['wake_id'])).fetchone()
        if live is None: return {}
        try: binding=_binding(c,p or {'id':row['principal'],'key_id':row['key_id']},row['device_id'])
        except APIError: binding=None
        if (binding is None or binding['generation']!=row['binding_generation'] or binding['endpoint']!=row['endpoint']
                or any(row['identity'][k]!=binding[k] for k in ('owner_id','source_id','auth_epoch'))):
            _settle(c,row,'cancelled','access_or_configuration_changed');return {'cancelled':1}
        if live['expires_at']<=datetime.now(timezone.utc):
            _settle(c,row,'expired','expired');return {'cancelled':1}
        if not ready(c,dispatcher=True):
            _settle(c,row,'pending','dispatcher_not_ready');return {'deferred':1}
        if problem:
            state='pending' if problem=='receiver_unavailable' else 'refused'
            _settle(c,row,state,problem);return {'deferred':1}
        endpoint,refused=endpoints.fenced(c,p,row['endpoint'],target[0])
        if refused or not endpoint or not endpoint['at_most_once']:
            _settle(c,row,'refused',refused or 'at_most_once_required');return {'cancelled':1}
        raw=body(row)
        try:
            headers=endpoints.signed_headers((endpoint['signing_secret'],endpoint['pending_secret']),row['wake_id'],raw,
                                            endpoints.caller_headers(endpoint))
        except (ValueError,TypeError):
            _settle(c,row,'refused','endpoint_configuration_invalid');return {'cancelled':1}
        status=failure=None
        try:
            response=transport(endpoint['url'],raw,headers,resolved=target[1],timeout=5)
            status=response[0] if isinstance(response,tuple) else response
        except (OSError,TimeoutError,ValueError) as error: failure=error
        outcome,reason=endpoints.outcome(endpoint,status,failure)
        # Generic HTTP refusal/error does not document 'no run started' for an Android Bot wake.
        if outcome=='retry' and failure is None: outcome,reason='final','outcome_unknown'
        state={'delivered':'accepted','retry':'pending','final':'unknown' if reason=='outcome_unknown' else 'refused'}[outcome]
        _settle(c,row,state,reason,delay=min(60,2**min(row['attempts']+1,5)))
        return {'delivered' if state=='accepted' else 'retried' if state=='pending' else 'dead_letter':1}


def routes(store):
    """Internal authenticated API only; not advertised as a public MCP send tool."""
    from .app import bearer
    async def handle(request):
        try: params=await request.json()
        except (ValueError,RecursionError): raise APIError('invalid_android_wake') from None
        result=await run_in_threadpool(call,store,bearer(request.headers),request.path_params['action'],params)
        return JSONResponse(result)
    return [Route('/v1/android-wakes/{action}',handle,methods=['POST'])]
