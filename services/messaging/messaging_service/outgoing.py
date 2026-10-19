"""Opt-in text outbox. Only the existing fenced collector can dispatch it.

No server approval gate: the acting agent must obtain exact owner approval.
Ambiguous sends are terminal unknown, never automatically retried.
"""
import asyncio
import hashlib
import re
import time

from psycopg.types.json import Jsonb

from .envelope import canonical, stable_id
from .policy import audit, effective_permissions, lock_principal, targets
from .store import APIError, serial


class SendRejected(RuntimeError):
    """A local rejection before invoking the provider send operation."""


class DispatchNotStarted(Exception):
    """The provider never received the request, so the outcome is certain: not sent."""


PROVIDER_PHASES=frozenset({'waiting','started','refused'})


async def dispatch(job,call,timeout,*,passthrough=()):
    """Run call(job), counting `timeout` from the moment the provider is invoked.

    A provider opts in to exact outcomes by calling job['_provider_phase'] with
    'waiting' before local checks or a shared rate gate, 'started' right before
    the request leaves the process, and 'refused' when the provider rejected it
    without acting (a Telegram flood wait). Local waiting is bounded by the job
    expiry instead of the provider timeout, and a failure outside 'started'
    raises DispatchNotStarted. Providers that never report keep one timeout.
    """
    loop=asyncio.get_running_loop()
    phase=None
    def report(value):
        nonlocal phase
        if value not in PROVIDER_PHASES:raise ValueError('invalid_provider_phase')
        phase=value
        if value=='started':scope.reschedule(loop.time()+timeout)
        elif value=='waiting':scope.reschedule(loop.time()+max(0.,job['expires_at']-time.time())+1)
    try:
        async with asyncio.timeout(timeout) as scope:
            job['_provider_phase']=report
            return await call(job)
    except passthrough:raise
    except Exception as exc:
        if phase in ('waiting','refused'):raise DispatchNotStarted() from exc
        raise


async def finish_durably(finish,*args,attempts=4):
    """Record a dispatch outcome, retrying transient database faults.

    The outcome exists only in this process: losing it to one pool timeout
    would turn a confirmed send into `sender_interrupted`. The update matches
    only the dispatching row of this lease, so a retry after an unseen commit
    changes nothing. If every attempt fails the collector must stop: the
    next lease marks the row interrupted instead of leaving it dispatching.
    """
    import psycopg
    from psycopg_pool import PoolTimeout
    for attempt in range(attempts):
        try:return await asyncio.to_thread(finish,*args)
        except (psycopg.OperationalError,PoolTimeout) as exc:
            if attempt==attempts-1:raise RuntimeError('dispatch_result_unrecorded') from exc
            await asyncio.sleep(2**attempt)


def valid_text(text):
    if not isinstance(text, str) or not text.strip() or any(
            ord(c)<32 and c not in '\n\r\t' or 0xD800<=ord(c)<=0xDFFF for c in text):
        raise APIError('invalid_message_text')
    if len(text.encode('utf-16-le'))//2>4000:
        raise APIError('invalid_message_text')


def permitted(p, account, chat):
    return account in p['accounts'] and (p['chat_ids'] is None or chat in p['chat_ids'])


def format_options(format,link_preview,allow_lossy,preview_digest=''):
    if not isinstance(format,str) or format not in ('plain','markdown','html'):
        raise APIError('invalid_message_format')
    if type(link_preview) is not bool or type(allow_lossy) is not bool:
        raise APIError('invalid_parameters')
    if not isinstance(preview_digest,str) or (preview_digest and not re.fullmatch(r'[a-f0-9]{64}',preview_digest)):
        raise APIError('preview_mismatch',409)


def phone_not_found(value):
    """not_found; a phone-shaped reference also names the operation that opens it."""
    from .messenger_operations import PHONE_SHAPED
    failure=APIError('not_found',404)
    if isinstance(value,str) and PHONE_SHAPED.fullmatch(value):failure.hint='open_chat_by_phone'
    return failure


def target(c,p,account,chat_id,reply_to):
    if not all(isinstance(v,str) and 1<=len(v)<=256 for v in (account,chat_id)):
        raise APIError('not_found',404)
    if not isinstance(reply_to,str) or len(reply_to)>256:
        raise APIError('not_found',404)
    if not permitted(p,account,chat_id):raise phone_not_found(chat_id)
    chat=c.execute('''SELECT c.*,a.platform FROM chats c JOIN accounts a ON a.id=c.account
        WHERE c.id=%s AND c.account=%s''',(chat_id,account)).fetchone()
    if not chat or not chat['provider_chat']:raise phone_not_found(chat_id)
    reply_id=None
    if reply_to:
        source=c.execute('SELECT provider_id FROM messages WHERE id=%s AND chat=%s AND account=%s AND NOT deleted',
            (reply_to,chat_id,account)).fetchone()
        if not source:raise APIError('not_found',404)
        reply_id=source['provider_id']
    targets(p,[chat_id,*([reply_to] if reply_to else [])])
    return chat,reply_id


def reviewed(c,p,account,chat_id,text,reply_to,format,link_preview,allow_lossy,chat,reply_id):
    from .formatting import render_message
    from .whatsapp_capabilities import account_provider
    if (link_preview and chat['platform']=='whatsapp' and
        account_provider(c,account,'whatsapp')=='whatsmeow'):
        raise APIError('link_preview_unsupported',409)
    artifact=render_message(chat['platform'],text,format=format,
        link_preview=link_preview,allow_lossy=allow_lossy)
    policy={'link_preview':link_preview,'allow_lossy':allow_lossy}
    # This binds a scoped draft, never the owner's identity or approval.
    digest=hashlib.sha256(canonical({'principal':p['id'],'scope_version':p['scope_version'],
        'account':account,'chat_id':chat_id,'provider_chat':chat['provider_chat'],
        'platform':chat['platform'],'reply_to':reply_to,'reply_provider_id':reply_id,
        'source_text':text,'artifact':artifact,'render_policy':policy})).hexdigest()
    return {**artifact,'account':account,'chat_id':chat_id,'provider_chat_id':chat['provider_chat'],
        'reply_to':reply_to,'reply_provider_id':reply_id,'render_policy':policy,
        'preview_digest':digest,'owner_approval_verified_by_server':False}


def preview(c,p,account,chat_id,text,reply_to='',format='plain',link_preview=False,allow_lossy=False):
    """Render a scoped draft without provider calls, readiness or enqueueing."""
    format_options(format,link_preview,allow_lossy)
    chat,reply_id=target(c,p,account,chat_id,reply_to)
    return reviewed(c,p,account,chat_id,text,reply_to,format,link_preview,allow_lossy,chat,reply_id)


def dispatch_payload(job,platform):
    """Validate persisted output without invoking the renderer again."""
    from .formatting import validate_entities,validate_semantic_entities,validate_provider_payload
    try:
        artifact=job.get('render_artifact')
        if artifact is None:
            # A pre-migration plain row has no render artifact or preview policy.
            if job.get('source_format','plain')!='plain' or job.get('render_policy') is not None:
                raise ValueError('invalid_legacy_payload')
            payload={'text':job['body'],'entities':[],'link_preview':False}
        else:
            policy=job.get('render_policy')
            if (not isinstance(artifact,dict) or not isinstance(policy,dict) or
                set(artifact)!={'format','source_text','rendered_text','provider_text','entities',
                    'provider_payload','warnings','links','utf16_units','renderer_version'} or
                set(policy)!={'link_preview','allow_lossy'} or
                any(type(v) is not bool for v in policy.values()) or
                artifact.get('format') not in ('plain','markdown','html') or
                artifact.get('format')!=job.get('source_format') or
                not isinstance(artifact.get('source_text'),str) or
                artifact.get('source_text')!=job.get('source_text') or
                artifact.get('provider_text')!=job.get('body') or
                not isinstance(artifact.get('rendered_text'),str) or
                not isinstance(artifact.get('renderer_version'),str) or
                not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}',artifact['renderer_version']) or
                not isinstance(artifact.get('warnings'),list) or len(artifact['warnings'])>32 or
                any(not isinstance(v,str) or not re.fullmatch(r'[a-z_]{1,64}',v) for v in artifact['warnings']) or
                not isinstance(artifact.get('links'),list) or len(artifact['links'])>128):
                raise ValueError('invalid_persisted_artifact')
            payload=artifact['provider_payload']
            if (payload.get('text')!=job['body'] or
                (platform=='telegram' and payload.get('entities')!=artifact.get('entities')) or
                payload.get('link_preview') is not policy['link_preview'] or
                type(artifact['utf16_units']) is not int or
                artifact['utf16_units']!=len(payload['text'].encode('utf-16-le'))//2 or
                (platform=='telegram' and artifact['rendered_text']!=payload['text'])):
                raise ValueError('invalid_persisted_payload')
            # WhatsApp's wire uses native delimiters, while preview spans describe
            # visible text. Those semantic spans are not provider entities.
            validator=validate_entities if platform=='telegram' else validate_semantic_entities
            validator(artifact['rendered_text'],artifact['entities'])
            if (artifact['format']!='plain' or policy['link_preview'] or policy['allow_lossy']) and (
                not isinstance(job.get('preview_digest'),str) or
                not re.fullmatch(r'[a-f0-9]{64}',job['preview_digest'])):
                raise ValueError('invalid_persisted_digest')
        return validate_provider_payload(platform,payload)
    except (APIError,ValueError,TypeError,KeyError,AttributeError):
        raise SendRejected('send_target_invalid') from None


def archive_reference(job,receipt):
    """Private provider proof; never replace the immutable requested destination.

    Native WhatsApp verifies the PN↔LID pair against its session store. Here we
    additionally bind the returned wire reference to the requested stanza, so a
    malformed IPC receipt cannot attach a different message or group.
    """
    if 'archive_reference' not in receipt:return None  # Legacy providers/rows.
    ref=receipt['archive_reference']
    requested=job['provider_chat']; public=receipt['provider_message_id']
    if (not isinstance(ref,dict) or set(ref)!={'provider_chat_id','provider_message_id'} or
        not all(isinstance(v,str) and 1<=len(v)<=256 for v in ref.values())):
        raise RuntimeError('send_receipt_mismatch')
    chat,mid=ref['provider_chat_id'],ref['provider_message_id']
    if not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,140}@(c\.us|g\.us|lid)',chat):
        raise RuntimeError('send_receipt_mismatch')
    def suffix(target,value):
        prefix='true_'+target+'_'
        if not re.fullmatch(r'[a-zA-Z0-9_.:@-]{1,256}',value) or not value.startswith(prefix):
            raise RuntimeError('send_receipt_mismatch')
        rest=value[len(prefix):]
        if not rest:raise RuntimeError('send_receipt_mismatch')
        return rest
    original,wire=suffix(requested,public),suffix(chat,mid)
    if chat==requested:
        # In groups only the verified owner's PN/LID suffix may differ; this is
        # checked by the native provider. The stanza ID must still be identical.
        if (original!=wire and (not chat.endswith('@g.us') or
            original.rsplit('_',1)[0]!=wire.rsplit('_',1)[0])):
            raise RuntimeError('send_receipt_mismatch')
    elif (not re.fullmatch(r'[0-9]{1,20}@c\.us',requested) or
          not re.fullmatch(r'[0-9]{1,20}@lid',chat) or original!=wire):
        raise RuntimeError('send_receipt_mismatch')
    return ref


def validate_send_receipt(value):
    """Private exact correlation journal, not a public API or body/time heuristic."""
    from .envelope import utc
    if (not isinstance(value,dict) or set(value)!={'kind','account','send_id','requested_chat',
        'provider_message_id','archive_reference','observed_at'} or value['kind']!='outgoing_send_receipt'
        or not isinstance(value['account'],str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',value['account'])
        or not isinstance(value['send_id'],str) or not re.fullmatch(r'send_[a-f0-9]{32}',value['send_id'])
        or not isinstance(value['requested_chat'],str) or
        not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,140}@(c\.us|g\.us|lid)',value['requested_chat'])
        or not isinstance(value['provider_message_id'],str)):
        raise ValueError('invalid_send_receipt')
    try:
        reference=archive_reference({'provider_chat':value['requested_chat']},value)
    except (RuntimeError,TypeError,KeyError):raise ValueError('invalid_send_receipt') from None
    return {**value,'archive_reference':reference,'observed_at':utc(value['observed_at'])}


def reconcile_send_receipt(state,value):
    """Collector-only repair after exact provider acknowledgement survived IPC loss.

    The current fenced account owner can record a previous dispatch's fact. No
    mutation is retried, and a queued/rejected/conflicting row is never promoted.
    """
    from .spool import PoisonItem
    value=validate_send_receipt(value)
    # Permanent mismatches (for example after restoring an older backup) are
    # quarantined by the spool instead of blocking every later receipt.
    if value['account']!=state.account:raise PoisonItem('foreign_send_receipt')
    if state.lease is None:raise RuntimeError('collector_lease_lost')
    with state.archive.store.pool.connection() as c:
        state.archive.fence(c,state.account,state.lease)
        row=c.execute('''SELECT o.* FROM outgoing_messages o JOIN accounts a ON a.id=o.account
            WHERE o.id=%s AND o.account=%s AND a.platform='whatsapp' FOR UPDATE OF o''',
            (value['send_id'],state.account)).fetchone()
        if not row or row['provider_chat']!=value['requested_chat']:
            raise PoisonItem('send_receipt_target_mismatch')
        reference=value['archive_reference'];provider_id=value['provider_message_id']
        if row['status']=='sent':
            if row['provider_message_id']!=provider_id or row['archive_reference']!=reference:
                raise PoisonItem('send_receipt_conflict')
            return {'outcome':'duplicate_send_receipt'}
        if row['status'] not in ('dispatching','unknown'):
            raise PoisonItem('send_receipt_state_mismatch')
        c.execute('''UPDATE outgoing_messages SET status='sent',provider_message_id=%s,
            archive_reference=%s,error_code=NULL,finished_at=clock_timestamp() WHERE id=%s''',
            (provider_id,Jsonb(reference),row['id']))
        return {'outcome':'send_reconciled'}


def project_reference(c,p,account,reference):
    """Actual wire proof is metadata, never an implicit read grant to its alias."""
    actual=None;reference_state='legacy_requested'
    if reference:
        chat=reference['provider_chat_id']
        saved=c.execute('SELECT id FROM chats WHERE account=%s AND provider_chat=%s',
            (account,chat)).fetchone() if c is not None else None
        chat_id=saved['id'] if saved else stable_id('chat',account,chat)
        if p is not None and permitted(p,account,chat_id):
            actual={**reference,'chat_id':chat_id,'message_id':stable_id('msg',account,chat,reference['provider_message_id'])}
            reference_state='provider_verified'
        else:reference_state='scope_restricted'
    return actual,reference_state


def result(row,c=None,p=None):
    from .receipts import for_reference,unknown_evidence
    actual,reference_state=project_reference(c,p,row['account'],row.get('archive_reference'))
    receipt=for_reference(c,row['account'],
        actual['provider_chat_id'] if actual else row['provider_chat'],
        actual['provider_message_id'] if actual else row['provider_message_id']) \
        if c is not None and row['status']=='sent' and row['provider_message_id'] else unknown_evidence()
    if reference_state=='scope_restricted':receipt=unknown_evidence()
    artifact=row.get('render_artifact')
    artifact=artifact if isinstance(artifact,dict) else {}
    warning_codes=artifact.get('warnings',[]) if isinstance(artifact,dict) else []
    return serial({
        'send_id':row['id'],'status':row['status'],'account':row['account'],
        'chat_id':row['chat'],'provider_chat_id':row['provider_chat'],
        'provider_message_id':row['provider_message_id'],
        'message_id':actual['message_id'] if actual else
            stable_id('msg',row['account'],row['provider_chat'],row['provider_message_id'])
            if row['provider_message_id'] and reference_state!='scope_restricted' else None,
        'archive_reference':actual,'archive_reference_state':reference_state,
        'created_at':row['created_at'],'expires_at':row['expires_at'],
        'finished_at':row['finished_at'],'error_code':row['error_code'],
        'agent_send':{'principal':row['principal'],'credential_id':row['key_id'],
            'request_id':row['request_id'],'approval_mode':'acting_agent',
            'owner_approval_verified_by_server':False},
        'delivery_receipt':receipt,
        'recipient_delivery_confirmed':receipt['recipient_delivery_confirmed'],'automatic_retry':False,
        'delivered_at':receipt['delivered_at'],'read_at':receipt['read_at'],'played_at':receipt['played_at'],
        'formatting':{'format':row.get('source_format','plain'),
            'renderer_version':artifact.get('renderer_version'),
            'warnings':[code for code in warning_codes if isinstance(code,str) and
                re.fullmatch(r'[a-z_]{1,64}',code)][:32],
            'preview_digest':row.get('preview_digest')},
        'next_action':'get_send_status' if row['status'] in ('queued','dispatching') else
            'verify_history_before_any_new_send' if row['status']=='unknown' else 'none',
    })


def enqueue(c,p,account,chat_id,text,request_id,reply_to='',format='plain',
            preview_digest='',link_preview=False,allow_lossy=False):
    format_options(format,link_preview,allow_lossy,preview_digest)
    # Source length is bounded by the renderer; the actual provider wire payload
    # must satisfy the conservative 4000 UTF-16-unit limit.
    if not isinstance(text,str) or not text.strip() or any(
            ord(v)<32 and v not in '\n\r\t' or 0xD800<=ord(v)<=0xDFFF for v in text):
        raise APIError('invalid_message_text')
    if format=='plain':valid_text(text)
    if not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,128}',request_id):
        raise APIError('invalid_request_id')
    chat,reply_id=target(c,p,account,chat_id,reply_to)
    default_policy=format=='plain' and not link_preview and not allow_lossy
    legacy_hash=hashlib.sha256(canonical([account,chat_id,text,reply_id])).hexdigest()
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('outgoing:'+p['id'],))
    existing=c.execute('SELECT * FROM outgoing_messages WHERE principal=%s AND request_id=%s',
        (p['id'],request_id)).fetchone()
    targets(p,[chat_id])
    if existing:
        # Check saved intent BEFORE calling a renderer: upgrades or restarts must
        # neither alter nor reject an exact retry of an already queued action.
        if existing.get('render_artifact') is None:
            matches=default_policy and not preview_digest and existing['request_hash']==legacy_hash
        else:
            matches=(existing['account']==account and existing['chat']==chat_id and
                existing['provider_chat']==chat['provider_chat'] and existing['reply_provider_id']==reply_id and
                existing.get('source_text')==text and existing.get('source_format')==format and
                existing.get('render_policy')=={'link_preview':link_preview,'allow_lossy':allow_lossy} and
                (existing.get('preview_digest') or '')==preview_digest)
        if not matches:raise APIError('idempotency_conflict',409)
        return result(existing,c,p)
    if not default_policy and not preview_digest:raise APIError('preview_required',409)
    draft=reviewed(c,p,account,chat_id,text,reply_to,format,link_preview,allow_lossy,chat,reply_id)
    if preview_digest and preview_digest!=draft['preview_digest']:raise APIError('preview_mismatch',409)
    artifact={name:draft[name] for name in ('format','source_text','rendered_text','provider_text','entities',
        'provider_payload','warnings','links','utf16_units','renderer_version')}
    request_hash=legacy_hash if default_policy else hashlib.sha256(canonical([
        account,chat_id,chat['provider_chat'],reply_id,artifact,draft['render_policy'],preview_digest])).hexdigest()
    policy=c.execute('SELECT * FROM account_policies WHERE account=%s',(account,)).fetchone()
    if not policy or not policy['enabled'] or not policy['send_enabled']:
        raise APIError('sending_disabled',409)
    from .collector_state import CollectorState
    if not CollectorState.allows(policy,chat['provider_chat']):raise APIError('not_found',404)
    connected=c.execute('''SELECT l.expires_at>clock_timestamp() AS active,h.operations
        FROM collector_leases l JOIN collector_health h USING(account) WHERE l.account=%s''',
        (account,)).fetchone()
    if not connected or not connected['active'] or connected['operations'].get('connection',{}).get('state')!='connected':
        raise APIError('sender_not_ready',409)
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('outgoing-account:'+account,))
    count=c.execute("SELECT count(*) AS n FROM outgoing_messages WHERE account=%s AND created_at>clock_timestamp()-interval '1 minute'",
        (account,)).fetchone()['n']
    if count>=60:raise APIError('rate_limited',429)
    row=c.execute('''INSERT INTO outgoing_messages(principal,key_id,scope_version,key_permission_version,request_id,request_hash,
        account,chat,provider_chat,body,reply_provider_id,source_text,source_format,render_artifact,render_policy,
        preview_digest) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
        (p['id'],p['key_id'],p['scope_version'],p['key_permission_version'],request_id,request_hash,account,chat_id,chat['provider_chat'],
         artifact['provider_text'],reply_id,text,format,Jsonb(artifact),Jsonb(draft['render_policy']),preview_digest or None)).fetchone()
    targets(p,[chat_id,row['id']])
    return result(row,c,p)


def status(c,p,send_id):
    if not isinstance(send_id,str) or not re.fullmatch(r'send_[a-f0-9]{32}',send_id):
        raise APIError('not_found',404)
    row=c.execute('SELECT * FROM outgoing_messages WHERE id=%s AND principal=%s',(send_id,p['id'])).fetchone()
    if not row or not permitted(p,row['account'],row['chat']):raise APIError('not_found',404)
    observed=c.execute('SELECT clock_timestamp() AS now').fetchone()['now']
    if row['status']=='queued' and row['expires_at']<=observed:
        row={**row,'status':'expired','error_code':'send_expired'}
    elif row['status']=='dispatching':
        lease=c.execute('SELECT epoch,expires_at FROM collector_leases WHERE account=%s',(row['account'],)).fetchone()
        if not lease or lease['epoch']!=row['lease_epoch'] or lease['expires_at']<=observed:
            row={**row,'status':'unknown','error_code':'sender_interrupted'}
    targets(p,[row['chat'],row['id']])
    return result(row,c,p)


class OutgoingWorker:
    def __init__(self,state,send,gate=None):
        self.state,self.send,self.gate=state,send,gate

    def claim(self):
        st=self.state
        with st.archive.store.pool.connection() as c:
            st.archive.fence(c,st.account,st.lease)
            c.execute("""UPDATE outgoing_messages SET status='unknown',error_code='sender_interrupted',finished_at=clock_timestamp()
                WHERE account=%s AND status='dispatching' AND lease_epoch IS DISTINCT FROM %s""",
                (st.account,st.lease[1]))
            row=c.execute("""SELECT * FROM outgoing_messages WHERE account=%s AND status='queued'
                ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED""",(st.account,)).fetchone()
            if not row:return None
            lock_principal(c,row['principal'],shared=True)
            access=c.execute('''SELECT p.*,k.revoked,k.expires_at AS key_expires,
                k.permissions AS key_permissions,k.permission_version AS key_permission_version,clock_timestamp() AS observed
                FROM principals p JOIN api_keys k ON k.principal=p.id WHERE p.id=%s AND k.id=%s''',
                (row['principal'],row['key_id'])).fetchone()
            policy=c.execute('SELECT * FROM account_policies WHERE account=%s',(st.account,)).fetchone()
            from .collector_state import CollectorState
            reason=None
            observed=c.execute('SELECT clock_timestamp() AS now').fetchone()['now']
            if row['expires_at']<=observed:reason='send_expired'
            elif (not access or not access['enabled'] or access['revoked'] or
                    access['key_expires']<=access['observed'] or
                    'send' not in effective_permissions(access['permissions'],access['key_permissions']) or
                    row['key_permission_version']!=access['key_permission_version'] or
                    row['scope_version']!=access['scope_version'] or not permitted(access,st.account,row['chat'])):
                reason='send_access_changed'
            elif not policy or not policy['enabled'] or not policy['send_enabled'] or not CollectorState.allows(policy,row['provider_chat']):
                reason='sending_disabled'
            if reason:
                c.execute("UPDATE outgoing_messages SET status=%s,error_code=%s,finished_at=clock_timestamp() WHERE id=%s",
                    ('expired' if reason=='send_expired' else 'cancelled',reason,row['id']))
                audit(c,{'id':row['principal'],'key_id':row['key_id']},'send_dispatch','denied',reason)
                return {'cancelled':True}
            c.execute("UPDATE outgoing_messages SET status='dispatching',started_at=clock_timestamp(),lease_epoch=%s WHERE id=%s",
                (st.lease[1],row['id']))
            audit(c,{'id':row['principal'],'key_id':row['key_id'],'_audit_targets':[row['id'],row['chat']]},'send_dispatch','ok')
            row['expires_at']=row['expires_at'].timestamp()
            return row

    def preflight(self,job):
        """Recheck the dispatch after waiting for a provider's rate gate."""
        st=self.state
        with st.archive.store.pool.connection() as c:
            st.archive.fence(c,st.account,st.lease)
            lock_principal(c,job['principal'],shared=True)
            row=c.execute('SELECT status,lease_epoch,expires_at FROM outgoing_messages WHERE id=%s',
                (job['id'],)).fetchone()
            access=c.execute('''SELECT p.*,k.revoked,k.expires_at AS key_expires,
                k.permissions AS key_permissions,k.permission_version AS key_permission_version,
                clock_timestamp() AS observed FROM principals p JOIN api_keys k ON k.principal=p.id
                WHERE p.id=%s AND k.id=%s''',(job['principal'],job['key_id'])).fetchone()
            if not row or row['status']!='dispatching' or row['lease_epoch']!=st.lease[1]:
                raise SendRejected('send_access_changed')
            if access and row['expires_at']<=access['observed']:
                raise SendRejected('send_expired')
            if (not access or not access['enabled'] or access['revoked'] or
                access['key_expires']<=access['observed'] or
                'send' not in effective_permissions(access['permissions'],access['key_permissions']) or
                job['scope_version']!=access['scope_version'] or
                job['key_permission_version']!=access['key_permission_version'] or
                not permitted(access,st.account,job['chat'])):
                raise SendRejected('send_access_changed')
            policy=c.execute('SELECT * FROM account_policies WHERE account=%s',(st.account,)).fetchone()
            from .collector_state import CollectorState
            if not policy or not policy['enabled'] or not policy['send_enabled'] or not CollectorState.allows(policy,job['provider_chat']):
                raise SendRejected('sending_disabled')
            if job.get('reply_provider_id') and not c.execute('''SELECT 1 FROM messages WHERE account=%s
                AND chat=%s AND provider_id=%s AND NOT deleted''',
                (st.account,job['chat'],job['reply_provider_id'])).fetchone():
                raise SendRejected('send_target_invalid')

    def finish(self,job,status,provider_id=None,code=None):
        st=self.state
        with st.archive.store.pool.connection() as c:
            st.archive.fence(c,st.account,st.lease)
            c.execute('''UPDATE outgoing_messages SET status=%s,provider_message_id=%s,error_code=%s,archive_reference=%s,
                finished_at=clock_timestamp() WHERE id=%s AND status='dispatching' AND lease_epoch=%s''',
                (status,provider_id,code,Jsonb(job['_archive_reference']) if job.get('_archive_reference') else None,
                 job['id'],st.lease[1]))

    async def once(self):
        if await asyncio.to_thread(self.state.cooldown_remaining)>0:return False
        if self.gate is not None:
            # Wait for the sending lane before claiming. WhatsApp background
            # history/media have a separate pipe and never hold this gate.
            async with self.gate:return await self._once()
        return await self._once()

    async def _once(self):
        job=await asyncio.to_thread(self.claim)
        if not job:return False
        if job.get('cancelled'):return True
        try:
            await asyncio.to_thread(self.preflight,job)
            async def preflight():await asyncio.to_thread(self.preflight,job)
            job['_preflight']=preflight
            receipt=await dispatch(job,self.send,30,passthrough=SendRejected)
            provider_id=receipt.get('provider_message_id') if isinstance(receipt,dict) else None
            if not isinstance(provider_id,str) or not 1<=len(provider_id)<=256:
                raise RuntimeError('send_receipt_missing')
            job['_archive_reference']=archive_reference(job,receipt)
        except SendRejected as exc:
            code=str(exc) if str(exc) in ('send_expired','sending_disabled','send_target_invalid','send_access_changed') else 'send_rejected'
            terminal='expired' if code=='send_expired' else 'cancelled' if code=='send_access_changed' else 'failed'
            await finish_durably(self.finish,job,terminal,None,code)
        except DispatchNotStarted:
            # Rate gate, local checks or a provider refusal: nothing left the
            # process, so the agent may safely send again with a new action ID.
            expired=time.time()>=job['expires_at']
            await finish_durably(self.finish,job,'expired' if expired else 'failed',None,
                'send_expired' if expired else 'send_not_started')
        except Exception as exc:
            # Never expose arbitrary provider error text or turn uncertainty
            # into a retry. Preserve only useful, content-free diagnostics.
            code=str(exc) if str(exc) in ('send_receipt_mismatch','send_receipt_missing',
                'provider_disconnected','provider_page_stalled','provider_operation_uncertain') else 'send_result_unknown'
            await finish_durably(self.finish,job,'unknown',None,code)
        else:
            await finish_durably(self.finish,job,'sent',provider_id)
        return True
