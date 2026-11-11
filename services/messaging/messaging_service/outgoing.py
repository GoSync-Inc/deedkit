"""Opt-in text outbox. Only the existing fenced collector can dispatch it.

No server approval gate: the acting agent must obtain exact owner approval.
Ambiguous sends are terminal unknown, never automatically retried.
"""
import asyncio
import hashlib
import re
import time
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from .identifiers import ACCOUNT_RE, SEND_ID_RE, WA_CHAT_RE
from .envelope import canonical, stable_id
from .policy import audit, effective_permissions, lock_principal, targets
from .store import APIError, serial
from . import group_mentions
from .silent_send import check as silent_check, render_policy, unsupported as silent_unsupported
from .subscriptions import journal_send_failures


class SendRejected(RuntimeError):
    """A local rejection before invoking the provider send operation."""


class DispatchNotStarted(Exception):
    """The provider never received the request, so the outcome is certain: not sent."""


PROVIDER_PHASES=frozenset({'waiting','started','refused'})
# ANYM-546: request IDs of composite steps (messages_send child keys); a caller route refuses them.
CHILD_KEY_PREFIX='agent-child:'
# One absolute budget covers expiry pool acquisition and its server transaction.
EXPIRY_SECONDS=2.0
EXPIRY_GRACE=0.5


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


def phone_step_id(request_id):
    """The request_id of a phone send's chat step: one per caller request_id, so a repeat opens nothing twice."""
    return 'phone-send:'+hashlib.sha256(request_id.encode()).hexdigest()[:40]


def valid_text(text):
    if not isinstance(text, str) or not text.strip() or any(
            ord(c)<32 and c not in '\n\r\t' or 0xD800<=ord(c)<=0xDFFF for c in text):
        raise APIError('invalid_message_text')
    if len(text.encode('utf-16-le'))//2>4000:
        raise APIError('invalid_message_text')


def permitted(p, account, chat):
    return account in p['accounts'] and (p['chat_ids'] is None or chat in p['chat_ids'])


def format_options(format,link_preview,allow_lossy,preview_digest='',silent=False):
    if not isinstance(format,str) or format not in ('plain','markdown','html'):
        raise APIError('invalid_message_format')
    if type(link_preview) is not bool or type(allow_lossy) is not bool or type(silent) is not bool:
        raise APIError('invalid_parameters')
    if not isinstance(preview_digest,str) or (preview_digest and not re.fullmatch(r'[a-f0-9]{64}',preview_digest)):
        raise APIError('preview_mismatch',409)


def phone_not_found(value,c=None,p=None,account=None):
    """not_found; a phone-shaped reference also names the operation that opens it
    and, given the caller's scope, the exact next call or why there is none (ANYM-531)."""
    from .messenger_operations import PHONE_SHAPED
    failure=APIError('not_found',404)
    if isinstance(value,str) and PHONE_SHAPED.fullmatch(value):
        failure.hint='open_chat_by_phone'
        if c is not None:
            from . import commands
            try:
                with c.transaction():step=commands.phone_next_step(c,p,account,value)
            except Exception:step={}  # The suggestion never turns not_found into another error.
            for name,item in step.items():setattr(failure,name,item)
    return failure


def target(c,p,account,chat_id,reply_to):
    if not all(isinstance(v,str) and 1<=len(v)<=256 for v in (account,chat_id)):
        raise APIError('not_found',404)
    if not isinstance(reply_to,str) or len(reply_to)>256:
        raise APIError('not_found',404)
    if not permitted(p,account,chat_id):raise phone_not_found(chat_id,c,p,account)
    # Named columns: a prepared c.* fails once a migration adds a chats column (046, ANYM-533).
    chat=c.execute('''SELECT c.id,c.account,c.title,c.chat_type,c.provider_chat,c.purged_at,a.platform
        FROM chats c JOIN accounts a ON a.id=c.account
        WHERE c.id=%s AND c.account=%s''',(chat_id,account)).fetchone()
    if not chat or not chat['provider_chat']:raise phone_not_found(chat_id,c,p,account)
    reply_id=None
    if reply_to:
        source=c.execute('SELECT provider_id FROM messages WHERE id=%s AND chat=%s AND account=%s AND NOT deleted',
            (reply_to,chat_id,account)).fetchone()
        if not source:raise APIError('not_found',404)
        reply_id=source['provider_id']
    targets(p,[chat_id,*([reply_to] if reply_to else [])])
    return chat,reply_id


def reviewed(c,p,account,chat_id,text,reply_to,format,link_preview,allow_lossy,chat,reply_id,menu=None,silent=False,
             mentions=None,fresh_s=group_mentions.SEND_FRESH_S):
    """menu (ANYM-837): a resolved menu_choice; its menu, revision, type, option and label join the digest.
    silent (ANYM-927): Telegram only; it joins the digest through the render policy (silent_send.py).
    mentions (ANYM-928): group members to @-mention; the request and provider forms join the digest (group_mentions.py)."""
    from .formatting import render_message
    from .whatsapp_capabilities import account_provider
    silent_check(silent,chat['platform'])
    if (link_preview and chat['platform']=='whatsapp' and
        account_provider(c,account,'whatsapp')=='whatsmeow'):
        raise APIError('link_preview_unsupported',409)
    artifact=render_message(chat['platform'],text,format=format,
        link_preview=link_preview,allow_lossy=allow_lossy)
    notice={}
    if mentions:
        artifact,notified,basis=group_mentions.apply(c,p,chat,text,format,link_preview,allow_lossy,artifact,mentions,fresh_s)
        notice={'mentions_notified':notified,'membership_basis':basis}
    policy=render_policy(link_preview,allow_lossy,silent,mentions)
    # This binds a scoped draft, never the owner's identity or approval.
    bound={'principal':p['id'],'scope_version':p['scope_version'],
        'account':account,'chat_id':chat_id,'provider_chat':chat['provider_chat'],
        'platform':chat['platform'],'reply_to':reply_to,'reply_provider_id':reply_id,
        'source_text':text,'artifact':artifact,'render_policy':policy}
    if menu is not None:
        bound['menu_choice']={name:menu[name] for name in ('message_id','provider_message_id','revision','menu_type',
            'option_id','label')}
    digest=hashlib.sha256(canonical(bound)).hexdigest()
    draft={**artifact,'account':account,'chat_id':chat_id,'provider_chat_id':chat['provider_chat'],
        'reply_to':reply_to,'reply_provider_id':reply_id,'render_policy':policy,
        'preview_digest':digest,'owner_approval_verified_by_server':False}
    if silent:draft['silent']=True
    draft.update(notice,**group_mentions.notices(chat['platform'],artifact))
    if menu is not None:
        from . import menu_choice
        draft['menu_choice']=menu_choice.preview_block(menu,chat)
    return draft


def preview(c,p,account,chat_id,text,reply_to='',format='plain',link_preview=False,allow_lossy=False,menu_choice=None,
            silent=False,mentions=None):
    """Render a scoped draft without provider calls, readiness or enqueueing."""
    format_options(format,link_preview,allow_lossy,silent=silent)
    mentions=group_mentions.normalized(mentions);group_mentions.conflicts(mentions,menu_choice)
    if menu_choice is not None:
        # A menu is answered only on WhatsApp, which has no silent send (ANYM-927).
        if silent:raise silent_unsupported()
        chat,menu=menu_target(c,p,account,chat_id,text,reply_to,format,link_preview,allow_lossy,menu_choice)
        return reviewed(c,p,account,chat_id,menu['label'],menu['message_id'],'plain',False,False,chat,
            menu['provider_message_id'],menu)
    chat,reply_id=target(c,p,account,chat_id,reply_to)
    return reviewed(c,p,account,chat_id,text,reply_to,format,link_preview,allow_lossy,chat,reply_id,silent=silent,
        mentions=mentions,fresh_s=group_mentions.PREVIEW_FRESH_S)


def menu_arguments(text,reply_to,format,link_preview,allow_lossy,menu_choice):
    """ANYM-837: a menu choice is a plain reply to its menu; anything else beside it is invalid_parameters."""
    from . import menu_choice as menus
    message_id,_=menus.validate(menu_choice)
    if (format!='plain' or link_preview is not False or allow_lossy is not False or
            not isinstance(reply_to,str) or reply_to not in ('',message_id)):
        from .field_errors import refusal
        raise refusal('invalid_parameters',[('menu_choice','format','chat_id, plain format, reply_to empty or the menu')])
    if not isinstance(text,str) or any(ord(v)<32 and v not in '\n\r\t' or 0xD800<=ord(v)<=0xDFFF for v in text):
        raise APIError('invalid_message_text')


def menu_target(c,p,account,chat_id,text,reply_to,format,link_preview,allow_lossy,menu_choice):
    """The chat and the resolved menu choice of a preview or send (archive reads only)."""
    from . import menu_choice as menus
    menu_arguments(text,reply_to,format,link_preview,allow_lossy,menu_choice)
    chat,_=target(c,p,account,chat_id,'')
    menu=menus.resolve(c,account,chat_id,chat,menu_choice,text)
    targets(p,[chat_id,menu['message_id']])
    return chat,menu


def dispatch_payload(job,platform):
    """Validate persisted output without invoking the renderer again."""
    from .formatting import validate_entities,validate_semantic_entities,validate_provider_payload
    menu=job.get('menu_choice')
    artifact=job.get('render_artifact')
    if menu is not None or isinstance(artifact,dict) and 'menu_choice' in artifact:
        # ANYM-837: a menu choice goes only to a WhatsApp adapter; the collector also checks it is the native one.
        if platform!='whatsapp':raise SendRejected('menu_choice_unsupported')
    try:
        if menu is not None or isinstance(artifact,dict) and 'menu_choice' in artifact:
            choice=artifact.get('menu_choice') if isinstance(artifact,dict) else None
            if (not isinstance(menu,dict) or set(menu)!={'message_id','provider_message_id','menu_type'} or
                not isinstance(choice,dict) or set(choice)!={'option_id','label','menu_type'} or
                not all(isinstance(v,str) and 1<=len(v)<=256 for v in (*menu.values(),choice['option_id'],choice['menu_type'])) or
                not isinstance(choice['label'],str) or not 1<=len(choice['label'])<=1024 or
                choice['menu_type']!=menu['menu_type'] or menu['provider_message_id']!=job.get('reply_provider_id') or
                choice['label']!=job.get('source_text') or job.get('source_format')!='plain' or
                not isinstance(job.get('preview_digest'),str) or not re.fullmatch(r'[a-f0-9]{64}',job['preview_digest'])):
                raise ValueError('invalid_persisted_menu_choice')
            artifact={k:v for k,v in artifact.items() if k!='menu_choice'}
        else:choice=None
        if artifact is None:
            # A pre-migration plain row has no render artifact or preview policy.
            if job.get('source_format','plain')!='plain' or job.get('render_policy') is not None:
                raise ValueError('invalid_legacy_payload')
            payload={'text':job['body'],'entities':[],'link_preview':False}
        else:
            policy=job.get('render_policy')
            if (not isinstance(artifact,dict) or not isinstance(policy,dict) or
                set(artifact)-{'mentions'}!={'format','source_text','rendered_text','provider_text','entities',
                    'provider_payload','warnings','links','utf16_units','renderer_version'} or
                set(policy)-{'silent','mentions'}!={'link_preview','allow_lossy'} or
                any(type(v) is not bool for k,v in policy.items() if k!='mentions') or policy.get('silent',True) is not True or
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
            if (artifact['format']!='plain' or policy['link_preview'] or policy['allow_lossy'] or 'mentions' in policy) and (
                not isinstance(job.get('preview_digest'),str) or
                not re.fullmatch(r'[a-f0-9]{64}',job['preview_digest'])):
                raise ValueError('invalid_persisted_digest')
        payload=validate_provider_payload(platform,payload)
        if artifact is not None:payload=group_mentions.dispatch_mentions(platform,artifact,job['render_policy'],payload)
        if isinstance(job.get('render_policy'),dict) and job['render_policy'].get('silent') is True:
            # ANYM-927: a silent row was previewed for a Telegram chat; no other platform sends it, loud or not.
            if platform!='telegram':raise ValueError('silent_unsupported')
            payload={**payload,'silent':True}
        if choice is not None:
            if payload.get('text')!=choice['label'] or payload.get('link_preview') is not False:
                raise ValueError('invalid_persisted_menu_choice')
            payload={**payload,'menu_choice':dict(choice)}
        return payload
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
    if not WA_CHAT_RE.fullmatch(chat):
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
        or not isinstance(value['account'],str) or not ACCOUNT_RE.fullmatch(value['account'])
        or not isinstance(value['send_id'],str) or not SEND_ID_RE.fullmatch(value['send_id'])
        or not isinstance(value['requested_chat'],str) or
        not WA_CHAT_RE.fullmatch(value['requested_chat'])
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


def telegram_reference(c,row):
    """ANYM-751: a sent Telegram row with no stored reference names the chat and message id the provider confirmed.

    Derived at read time, never written; the archive row may still lag the receipt by the capture delay (the
    send does not wait for the archive commit). Returns (reference or None, the account's platform or None)."""
    if c is None or row['status']!='sent' or not row['provider_message_id'] or row.get('archive_reference'):
        return None,None
    found=c.execute('SELECT platform FROM accounts WHERE id=%s',(row['account'],)).fetchone()
    platform=found['platform'] if found else None
    if platform!='telegram':return None,platform
    return {'provider_chat_id':row['provider_chat'],'provider_message_id':row['provider_message_id']},platform


def result(row,c=None,p=None):
    from .receipts import for_reference,hint_chat_facts,receipt_hint,unknown_evidence
    derived,platform=telegram_reference(c,row)
    actual,reference_state=project_reference(c,p,row['account'],row.get('archive_reference') or derived)
    context={}
    receipt=for_reference(c,row['account'],
        actual['provider_chat_id'] if actual else row['provider_chat'],
        actual['provider_message_id'] if actual else row['provider_message_id'],platform=platform,context=context) \
        if c is not None and row['status']=='sent' and row['provider_message_id'] else unknown_evidence()
    if reference_state=='scope_restricted':receipt=unknown_evidence()
    artifact=row.get('render_artifact')
    artifact=artifact if isinstance(artifact,dict) else {}
    warning_codes=artifact.get('warnings',[]) if isinstance(artifact,dict) else []
    saved=row.get('menu_choice');chosen=artifact.get('menu_choice')
    menu={'message_id':saved.get('message_id'),'option_id':chosen.get('option_id'),'label':chosen.get('label')} \
        if isinstance(saved,dict) and isinstance(chosen,dict) else None
    hint={}
    if row['status']=='sent':
        # ANYM-659: a pure function of facts already read plus one guarded, bounded chat lookup; a restricted scope
        # hides the evidence, and any failure gives the unknown pair.
        try:
            facts=hint_chat_facts(c,row['account'],actual['provider_chat_id'] if actual else row['provider_chat']) if c is not None and context.get('platform') else {}
            reports,state=receipt_hint(context.get('platform'),facts.get('chat_type'),facts.get('bot'),
                True if receipt['basis']=='self_chat' else facts.get('self_chat'),receipt['state'],
                row.get('finished_at'),datetime.now(timezone.utc))
        except Exception:reports,state=None,'unknown'
        hint={'messenger_reports':reports,'receipt_state':'unknown' if reference_state=='scope_restricted' else state}
    return serial({
        **hint,
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
            'verify_history_before_any_new_send' if row['status']=='unknown' else
            MENU_SENT_NEXT if row['status']=='sent' and menu else
            menu_failed_next(row['error_code']) if row['status']=='failed' and menu else 'none',
        **({'menu_choice':menu} if menu else {}),
    })


# ANYM-837: sent means WhatsApp accepted the reply, not that the bot acted on it.
MENU_SENT_NEXT=('the bot answers within seconds with a new message: read the chat after the send time (get_messages) '
    'before the next step')


def menu_failed_next(code):
    """ANYM-837: the recovery a failed menu choice's refusal names; never a resend of the same choice."""
    from . import menu_choice
    return menu_choice.FAILED_NEXT.get(code,'none')


def policy_blocks(policy):
    """The account-policy gate of send admission; the outbound status (ANYM-671) uses the same predicate."""
    return not policy or not policy['enabled'] or not policy['send_enabled']


def connection_blocks(connected):
    """The sender gate of send admission: a live collector lease and a connected provider session."""
    return not connected or not connected['active'] or connected['operations'].get('connection',{}).get('state')!='connected'


STALL_AFTER_S=120
OUTBOUND_SQL='''SELECT a.id AS account,p.account IS NOT NULL AS has_policy,p.enabled,p.send_enabled,
    l.account IS NOT NULL AS has_lease,l.expires_at>clock_timestamp() AS active,h.account IS NOT NULL AS has_health,h.operations,
    greatest(0,extract(epoch FROM h.cooldown_until-clock_timestamp())) AS cooldown_s,
    q.queued,q.oldest_age,q.expires_in,q.last_expires_in,x.expired_unsettled
    FROM accounts a LEFT JOIN account_policies p ON p.account=a.id
    LEFT JOIN collector_leases l ON l.account=a.id LEFT JOIN collector_health h ON h.account=a.id
    CROSS JOIN LATERAL (SELECT count(*) AS queued,extract(epoch FROM clock_timestamp()-min(created_at)) AS oldest_age,
        extract(epoch FROM (array_agg(expires_at ORDER BY created_at,id))[1]-clock_timestamp()) AS expires_in,
        extract(epoch FROM max(expires_at)-clock_timestamp()) AS last_expires_in
        FROM outgoing_messages WHERE account=a.id AND status='queued' AND expires_at>clock_timestamp()) q
    CROSS JOIN LATERAL (SELECT count(*) AS expired_unsettled FROM outgoing_messages
        WHERE account=a.id AND status='queued' AND expires_at<=clock_timestamp()) x
    WHERE a.id=ANY(%s)'''
UNAVAILABLE={'sender':'unavailable','queued':None,'oldest_queued_age_s':None,'expires_in_s':None,'expired_unsettled':None}


def _outbound_block(row):
    from .status_details import describe
    account=row['account']
    policy={'enabled':row['enabled'],'send_enabled':row['send_enabled']} if row['has_policy'] else None
    connected={'active':bool(row['active']),'operations':row['operations'] or {}} if row['has_lease'] and row['has_health'] else None
    queued=int(row['queued']);expired=int(row['expired_unsettled'])
    age=None if not queued else max(0.0,round(float(row['oldest_age']),1))
    left=None if not queued else max(0.0,round(float(row['expires_in']),1))
    block={'sender':'ready','queued':queued,'oldest_queued_age_s':age,'expires_in_s':left,'expired_unsettled':expired}
    if policy_blocks(policy):
        block.update(sender='not_ready',reason_code='sending_disabled',reason='Sending is disabled for this account by its policy.')
    elif connection_blocks(connected):
        phase=(connected or {}).get('operations',{}).get('connection') or {}
        code='collector_not_running' if not connected or not connected['active'] else phase.get('reason_code') or 'not_checked'
        block.update(sender='not_ready',reason_code=code,
            reason='The collector is not running, so nothing can be sent.' if code=='collector_not_running'
                else 'The provider session is not connected: '+describe(code)['explanation'])
        if code!='collector_not_running' and phase.get('retry_at'):block['retry_at']=phase['retry_at']
    elif row['cooldown_s'] and row['cooldown_s']>0:
        block.update(sender='cooling_down',cooldown_remaining_s=round(float(row['cooldown_s']),1),
            reason='The provider asked the account to slow down; the sender resumes when the cooldown ends.')
    sender=block['sender']
    # One instruction per state, composed: expired rows, the sender's recovery and the waiting queue all apply at once.
    parts=[]
    if expired:
        parts.append(str(expired)+' queued send(s) are past their expiry and will not be sent'
            +(' even once the account is back' if sender!='ready' else '')+'; a new action with the owner\'s approval is needed for them.')
    if sender=='not_ready':
        parts.append('Wait until '+str(block['retry_at'])+' and read this status again.' if block.get('retry_at')
            else 'Tell the owner that sending from account '+account+' is not available.')
    elif sender=='cooling_down' and queued and block['cooldown_remaining_s']>left:
        last=max(0.0,float(row['last_expires_in']))
        parts.append('Nothing was sent: '+('every queued send will expire before the cooldown ends; ask for a new action with the '
            'owner\'s approval after the cooldown.' if block['cooldown_remaining_s']>last else
            'the oldest queued send will expire before the cooldown ends and later ones may still go out; check each with '
            'get_send_status, and for each that expires ask for a new action with the owner\'s approval.'))
    elif queued:
        parts.append('Queued sends are waiting; if one expires (send.failed or status expired), a new action with the '
            'owner\'s approval is needed.')
    if queued:parts.append('Do not send the same thing again while sends wait.')
    if parts:block['next_action']=' '.join(parts)
    if sender=='ready' and queued and age>STALL_AFTER_S:
        block['reason']='Oldest queued send has waited '+str(int(age))+' s with the sender connected and no cooldown.'
    return block


def outbound_alerts(block):
    """Alert codes for one outbound block (ANYM-671); none during a cooldown, which is a sender state."""
    if not block.get('queued'):return []
    if block['sender']=='not_ready':return ['outbound_sender_not_ready']
    if block['sender']=='ready' and block['oldest_queued_age_s']>STALL_AFTER_S:return ['outbound_queue_stalled']
    return []


def outbound_status(c,accounts):
    """Per-account outbound block from one read-only query in its own savepoint with a 2 s statement timeout:
    an error or timeout gives the unavailable block for every account and never reaches the caller (ANYM-671)."""
    accounts=list(accounts)
    try:
        with c.transaction():
            previous=c.execute("SELECT current_setting('statement_timeout') AS v").fetchone()['v']
            c.execute("SELECT set_config('statement_timeout','2000',true)")
            rows=c.execute(OUTBOUND_SQL,(accounts,)).fetchall()
            c.execute("SELECT set_config('statement_timeout',%s,true)",(previous,))
        found={row['account']:_outbound_block(row) for row in rows}
    except Exception:
        found={}
    return {account:found.get(account) or dict(UNAVAILABLE) for account in accounts}


def enqueue(c,p,account,chat_id,text,request_id,reply_to='',format='plain',
            preview_digest='',link_preview=False,allow_lossy=False,phone_hash=None,child=False,
            expected_last_message_id=None,guard=None,duplicate_guard=False,allow_duplicate=None,menu_choice=None,silent=False,
            mentions=None):
    """menu_choice (ANYM-837): {message_id, option_id} of an archived WhatsApp menu, answered with its structured
    reply; it needs its own preview_digest and stores the archived label as the text.
    expected_last_message_id, guard (ANYM-736): the optional chat-head precondition, send_guard.py.
    duplicate_guard (ANYM-748): only the send_message tool passes True; allow_duplicate=True skips it for one call.
    phone_hash: a phone send (ANYM-532) whose digest the caller checked; it is the request hash.
    child: a messages_send step (ANYM-546) under its child key; only such a call may use the child prefix.
    silent (ANYM-927): Telegram without a notification sound, with its own preview_digest (silent_send.py).
    mentions (ANYM-928): group members to @-mention, with their own preview_digest (group_mentions.py)."""
    def refused(error,resolved=False):
        # ANYM-646: a caller's own send names the preview call; a phone step or a messages_send child keeps its body.
        # The chat is echoed only once it resolves for this key; a malformed digest is refused before that.
        if phone_hash is not None or child:return error
        if not resolved:
            try:target(c,p,account,chat_id,reply_to)
            except Exception:return error
        from . import preview_refusal
        return preview_refusal.send(error,p,account,chat_id,reply_to=reply_to if isinstance(reply_to,str) else '',
            format=format,link_preview=link_preview,allow_lossy=allow_lossy,silent=silent,mentions=mentions)
    mentions=group_mentions.normalized(mentions);group_mentions.conflicts(mentions,menu_choice)
    if menu_choice is not None:
        if phone_hash is not None or child:
            from .field_errors import refusal as field_refusal
            raise field_refusal('invalid_parameters',[('menu_choice','format','a chat_id send_message')])
        format_options(format,link_preview,allow_lossy,preview_digest,silent)
        if silent:raise silent_unsupported()
        menu_arguments(text,reply_to,format,link_preview,allow_lossy,menu_choice)
        return enqueue_menu_choice(c,p,account,chat_id,text,request_id,preview_digest,expected_last_message_id,guard,
            duplicate_guard,allow_duplicate,menu_choice)
    try:format_options(format,link_preview,allow_lossy,preview_digest,silent)
    except APIError as error:raise refused(error) from None
    # Source length is bounded by the renderer; the actual provider wire payload
    # must satisfy the conservative 4000 UTF-16-unit limit.
    if not isinstance(text,str) or not text.strip() or any(
            ord(v)<32 and v not in '\n\r\t' or 0xD800<=ord(v)<=0xDFFF for v in text):
        raise APIError('invalid_message_text')
    if format=='plain':valid_text(text)
    if not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,128}',request_id) or (
            request_id.startswith(CHILD_KEY_PREFIX)!=child):
        raise APIError('invalid_request_id')
    if allow_duplicate is not None and not isinstance(allow_duplicate,bool):
        from .field_errors import refusal
        raise refusal('invalid_parameters',[('allow_duplicate','type','boolean')])
    guarded=expected_last_message_id is not None or guard is not None
    if guarded:
        from . import send_guard
        send_guard.validate(expected_last_message_id,guard)
    chat,reply_id=target(c,p,account,chat_id,reply_to);silent_check(silent,chat['platform'])
    guard_refusal=None
    if guarded and not c.execute('SELECT 1 FROM outgoing_messages WHERE principal=%s AND request_id=%s',
            (p['id'],request_id)).fetchone():
        # ANYM-736: before the principal lock, so a slow guard read never delays another send; a request_id with a
        # saved row skips it and returns the saved result below, whatever arrived since. A refusal waits for the
        # locked lookup below: a concurrent twin that committed this request_id meanwhile wins over it. The race
        # between this read and the insert is the guard's documented limit.
        try:send_guard.check(c,p,account,chat_id,chat['platform'],expected_last_message_id,guard)
        except APIError as failure:guard_refusal=failure
    default_policy=format=='plain' and not link_preview and not allow_lossy and not silent and not mentions
    legacy_hash=hashlib.sha256(canonical([account,chat_id,text,reply_id])).hexdigest()
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('outgoing:'+p['id'],))
    existing=c.execute('SELECT * FROM outgoing_messages WHERE principal=%s AND request_id=%s',
        (p['id'],request_id)).fetchone()
    targets(p,[chat_id])
    # A phone send (ANYM-532) reserves its request_id from the chat step on: never a chat_id send, even before its row.
    if phone_hash is None and c.execute('SELECT 1 FROM messenger_commands WHERE principal=%s AND request_id=%s',
            (p['id'],phone_step_id(request_id))).fetchone():raise APIError('idempotency_conflict',409)
    if existing:
        # Check saved intent BEFORE calling a renderer: upgrades or restarts must
        # neither alter nor reject an exact retry of an already queued action.
        if phone_hash is not None:matches=existing['request_hash']==phone_hash
        elif existing.get('render_artifact') is None:
            matches=default_policy and not preview_digest and existing['request_hash']==legacy_hash
        else:
            matches=('menu_choice' not in existing['render_artifact'] and
                existing['account']==account and existing['chat']==chat_id and
                existing['provider_chat']==chat['provider_chat'] and existing['reply_provider_id']==reply_id and
                existing.get('source_text')==text and existing.get('source_format')==format and
                existing.get('render_policy')==render_policy(link_preview,allow_lossy,silent,mentions) and
                (existing.get('preview_digest') or '')==preview_digest)
        if not matches:raise APIError('idempotency_conflict',409)
        return result(existing,c,p)
    if guard_refusal is not None:raise guard_refusal
    if duplicate_guard and allow_duplicate is not True:
        refuse_unresolved_duplicate(c,p,account,chat_id,request_id,text,format,link_preview,allow_lossy,reply_id,silent=silent,
            mentions=mentions)
    if not default_policy and not preview_digest:raise refused(APIError('preview_required',409),True)
    draft=reviewed(c,p,account,chat_id,text,reply_to,format,link_preview,allow_lossy,chat,reply_id,silent=silent,
        mentions=mentions)
    if phone_hash is None and preview_digest and preview_digest!=draft['preview_digest']:
        raise refused(APIError('preview_mismatch',409),True)
    artifact={name:draft[name] for name in ('format','source_text','rendered_text','provider_text','entities',
        'provider_payload','warnings','links','utf16_units','renderer_version',*(('mentions',) if mentions else ()))}
    request_hash=phone_hash or legacy_hash if default_policy else phone_hash or hashlib.sha256(canonical([
        account,chat_id,chat['provider_chat'],reply_id,artifact,draft['render_policy'],preview_digest])).hexdigest()
    admit(c,account,chat)
    row=c.execute('''INSERT INTO outgoing_messages(principal,key_id,scope_version,key_permission_version,request_id,request_hash,
        account,chat,provider_chat,body,reply_provider_id,source_text,source_format,render_artifact,render_policy,
        preview_digest) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
        (p['id'],p['key_id'],p['scope_version'],p['key_permission_version'],request_id,request_hash,account,chat_id,chat['provider_chat'],
         artifact['provider_text'],reply_id,text,format,Jsonb(artifact),Jsonb(draft['render_policy']),preview_digest or None)).fetchone()
    targets(p,[chat_id,row['id']])
    return result(row,c,p)


def admit(c,account,chat):
    """The account policy, collector, connection and 60-a-minute gates every chat_id send passes before its insert."""
    policy=c.execute('SELECT * FROM account_policies WHERE account=%s',(account,)).fetchone()
    if policy_blocks(policy):raise APIError('sending_disabled',409)
    from .collector_state import CollectorState
    if not CollectorState.allows(policy,chat['provider_chat']):raise APIError('not_found',404)
    connected=c.execute('''SELECT l.expires_at>clock_timestamp() AS active,h.operations
        FROM collector_leases l JOIN collector_health h USING(account) WHERE l.account=%s''',
        (account,)).fetchone()
    if connection_blocks(connected):raise APIError('sender_not_ready',409)
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('outgoing-account:'+account,))
    count=c.execute("SELECT count(*) AS n FROM outgoing_messages WHERE account=%s AND created_at>clock_timestamp()-interval '1 minute'",
        (account,)).fetchone()['n']
    if count>=60:raise APIError('rate_limited',429)


def menu_matches(existing,account,chat_id,text,preview_digest,menu_choice):
    """ANYM-837: an exact retry of a saved menu choice: same chat, menu, option, accepted text and digest."""
    artifact=existing.get('render_artifact');saved=existing.get('menu_choice')
    if not isinstance(artifact,dict) or not isinstance(artifact.get('menu_choice'),dict) or not isinstance(saved,dict):
        return False
    label=artifact['menu_choice'].get('label')
    return (existing['account']==account and existing['chat']==chat_id and
        saved.get('message_id')==menu_choice['message_id'] and
        artifact['menu_choice'].get('option_id')==menu_choice['option_id'] and isinstance(label,str) and
        (text in ('',label) or label.startswith(text+' - ')) and (existing.get('preview_digest') or '')==preview_digest)


def enqueue_menu_choice(c,p,account,chat_id,text,request_id,preview_digest,expected_last_message_id,guard,
                        duplicate_guard,allow_duplicate,menu_choice):
    """ANYM-837: queue one structured answer to an archived menu; the steps and their order follow enqueue."""
    from . import menu_choice as menus
    if not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,128}',request_id) or (
            request_id.startswith(CHILD_KEY_PREFIX)):
        raise APIError('invalid_request_id')
    if allow_duplicate is not None and not isinstance(allow_duplicate,bool):
        from .field_errors import refusal
        raise refusal('invalid_parameters',[('allow_duplicate','type','boolean')])
    guarded=expected_last_message_id is not None or guard is not None
    if guarded:
        from . import send_guard
        send_guard.validate(expected_last_message_id,guard)
    chat,_=target(c,p,account,chat_id,'')
    guard_refusal=None
    if guarded and not c.execute('SELECT 1 FROM outgoing_messages WHERE principal=%s AND request_id=%s',
            (p['id'],request_id)).fetchone():
        try:send_guard.check(c,p,account,chat_id,chat['platform'],expected_last_message_id,guard)
        except APIError as failure:guard_refusal=failure
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('outgoing:'+p['id'],))
    existing=c.execute('SELECT * FROM outgoing_messages WHERE principal=%s AND request_id=%s',
        (p['id'],request_id)).fetchone()
    targets(p,[chat_id])
    if c.execute('SELECT 1 FROM messenger_commands WHERE principal=%s AND request_id=%s',
            (p['id'],phone_step_id(request_id))).fetchone():raise APIError('idempotency_conflict',409)
    if existing:
        # The saved row decides an exact retry, even once the menu is gone or newer.
        if not menu_matches(existing,account,chat_id,text,preview_digest,menu_choice):
            raise APIError('idempotency_conflict',409)
        return result(existing,c,p)
    # A new choice reads the archived menu, and a refusal may name its label: only a key that may read the chat
    # (preview_message's permission) resolves it; a saved row above stays send-only (ANYM-837).
    if 'read' not in p['permissions']:raise APIError('forbidden',403)
    if guard_refusal is not None:raise guard_refusal
    # Before any menu read: a database without the column stores nothing (plain sends never ask).
    if not menus.column_present(c):raise menus.refusal('menu_choice_unavailable',503)
    menu=menus.resolve(c,account,chat_id,chat,menu_choice,text)
    reply_id=menu['provider_message_id']
    targets(p,[chat_id,menu['message_id']])
    if duplicate_guard and allow_duplicate is not True:
        refuse_unresolved_duplicate(c,p,account,chat_id,request_id,menu['label'],'plain',False,False,reply_id,
            option_id=menu['option_id'])
    if not preview_digest:
        failure=APIError('preview_required',409)
        setattr(failure,'next_action','preview_message with the same account, chat_id and menu_choice')
        raise failure
    draft=reviewed(c,p,account,chat_id,menu['label'],menu['message_id'],'plain',False,False,chat,reply_id,menu)
    if preview_digest!=draft['preview_digest']:raise APIError('preview_mismatch',409)
    artifact={name:draft[name] for name in ('format','source_text','rendered_text','provider_text','entities',
        'provider_payload','warnings','links','utf16_units','renderer_version')}
    artifact['menu_choice']=menus.artifact_key(menu)
    request_hash=hashlib.sha256(canonical([account,chat_id,chat['provider_chat'],reply_id,artifact,
        draft['render_policy'],preview_digest])).hexdigest()
    admit(c,account,chat)
    row=c.execute('''INSERT INTO outgoing_messages(principal,key_id,scope_version,key_permission_version,request_id,request_hash,
        account,chat,provider_chat,body,reply_provider_id,source_text,source_format,render_artifact,render_policy,
        preview_digest,menu_choice) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
        (p['id'],p['key_id'],p['scope_version'],p['key_permission_version'],request_id,request_hash,account,chat_id,
         chat['provider_chat'],artifact['provider_text'],reply_id,menu['label'],'plain',Jsonb(artifact),
         Jsonb(draft['render_policy']),preview_digest,Jsonb(menus.column(menu)))).fetchone()
    targets(p,[chat_id,row['id']])
    return result(row,c,p)


# ANYM-748: an earlier send of the same content counts while it is unresolved and this recent. The window uses the
# stable statement_timestamp(), so it bounds the outgoing_principal_time(principal,created_at) index scan.
DUPLICATE_WINDOW_MINUTES=60
DUPLICATE_TIMEOUT_MS=1000
DUPLICATE_SQL=f"""SELECT request_id,status FROM outgoing_messages
    WHERE principal=%s AND account=%s AND chat=%s AND source_text=%s AND source_format=%s AND render_policy=%s
      AND reply_provider_id IS NOT DISTINCT FROM %s AND request_id<>%s AND left(request_id,{len(CHILD_KEY_PREFIX)})<>%s
      AND (status IN ('dispatching','unknown') OR status='queued' AND expires_at>clock_timestamp())
      AND created_at>statement_timestamp()-interval '{DUPLICATE_WINDOW_MINUTES} minutes'
    ORDER BY created_at DESC LIMIT 1"""


# ANYM-837: a menu choice is the same intent for the same option of the same menu, whatever its label now says:
# a bot that renames an option cannot make one action run twice.
MENU_DUPLICATE_SQL=f"""SELECT request_id,status FROM outgoing_messages
    WHERE principal=%s AND account=%s AND chat=%s AND reply_provider_id=%s AND menu_choice IS NOT NULL
      AND render_artifact->'menu_choice'->>'option_id'=%s
      AND request_id<>%s AND left(request_id,{len(CHILD_KEY_PREFIX)})<>%s
      AND (status IN ('dispatching','unknown') OR status='queued' AND expires_at>clock_timestamp())
      AND created_at>statement_timestamp()-interval '{DUPLICATE_WINDOW_MINUTES} minutes'
    ORDER BY created_at DESC LIMIT 1"""


def unresolved_send(c,p,account,chat_id,request_id,text,format,link_preview,allow_lossy,reply_id,option_id=None,
                    silent=False,mentions=None):
    """The newest unresolved earlier send of this principal with the same intent, or None."""
    policy=Jsonb(render_policy(link_preview,allow_lossy,silent,mentions))
    if option_id is not None:
        return c.execute(MENU_DUPLICATE_SQL,(p['id'],account,chat_id,reply_id,option_id,request_id,
            CHILD_KEY_PREFIX)).fetchone()
    return c.execute(DUPLICATE_SQL,(p['id'],account,chat_id,text,format,
        policy,reply_id,request_id,CHILD_KEY_PREFIX)).fetchone()


def refuse_unresolved_duplicate(c,p,account,chat_id,request_id,text,format,link_preview,allow_lossy,reply_id,
                                option_id=None,silent=False,mentions=None):
    """ANYM-748: refuse a send whose same content to the same chat is still queued, dispatching or unknown.

    Runs under the principal lock, so of two concurrent identical calls one queues and the other sees it. Its own
    savepoint and a 1 s statement timeout: any failure is chat_guard_unavailable (503) and nothing was queued.
    The refusal names the earlier request_id and status only; never the message text or the send's internal id."""
    try:
        with c.transaction():
            previous=c.execute("SELECT current_setting('statement_timeout') AS v").fetchone()['v']
            c.execute("SELECT set_config('statement_timeout',%s,true)",(str(DUPLICATE_TIMEOUT_MS),))
            earlier=unresolved_send(c,p,account,chat_id,request_id,text,format,link_preview,allow_lossy,reply_id,option_id,
                silent,mentions)
            c.execute("SELECT set_config('statement_timeout',%s,true)",(previous,))
    except Exception:
        raise APIError('chat_guard_unavailable',503) from None
    if earlier is None:return
    failure=APIError('duplicate_send_unresolved',409)
    failure.earlier_request_id=earlier['request_id']
    failure.earlier_status=earlier['status']
    failure.next_action='check_earlier_send_status'
    failure.next_call={'tool':'get_send_status','arguments':{'request_id':earlier['request_id']},'caller_supplies':[]}
    raise failure


def status(c,p,send_id=None,request_id=None):
    """One send's saved state by its send_id, or (ANYM-736) by the request_id the caller chose: the way back to a
    result whose response was lost, with no guard and no read access needed."""
    if (send_id is None)==(request_id is None):
        from .field_errors import refusal
        raise refusal('invalid_parameters',[('send_id','required')])
    if request_id is not None:
        if (not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,128}',request_id)
                or request_id.startswith(CHILD_KEY_PREFIX)):
            raise APIError('not_found',404)
        row=c.execute('SELECT * FROM outgoing_messages WHERE principal=%s AND request_id=%s',
            (p['id'],request_id)).fetchone()
    elif not isinstance(send_id,str) or not SEND_ID_RE.fullmatch(send_id):
        raise APIError('not_found',404)
    else:
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


# Local refusals a finished row keeps by name; menu codes (ANYM-837) come from the native adapter or dispatch.
SEND_REFUSALS=('send_expired','sending_disabled','send_target_invalid','send_access_changed','menu_choice_unsupported',
    'menu_unavailable','menu_changed','menu_option_ambiguous','menu_option_id_unrepresentable','menu_option_unsupported',
    'menu_reply_shape_unverified','mentions_unsupported','mention_unresolvable')


class OutgoingWorker:
    def __init__(self,state,send,gate=None):
        self.state,self.send,self.gate=state,send,gate

    def expire_queued(self,*,deadline=None):
        """ANYM-655: bounded housekeeping, independent of the provider and cooldown.

        Row locks exclude a concurrent claim; never touch dispatching/unknown
        attempts. A terminal event uses the same savepoint isolation as finish.
        """
        from .telegram_collector import DeadlinePool
        st=self.state
        pool=DeadlinePool(st.archive.store.pool,deadline if deadline is not None else time.monotonic()+EXPIRY_SECONDS)
        with pool.connection() as c:
            c.execute("SET LOCAL lock_timeout='100ms'")
            c.execute("SET LOCAL statement_timeout='2s'")
            # ANYM-788: every ingest holds the lease row, so fencing an empty pass only fills the database
            # log with lock timeouts. A lost lease with nothing due is for heartbeat and claim() to notice.
            if not c.execute("""SELECT 1 FROM outgoing_messages WHERE account=%s AND status='queued'
                AND expires_at<=clock_timestamp() LIMIT 1""",(st.account,)).fetchone():return 0
            st.archive.fence(c,st.account,st.lease)
            rows=c.execute("""WITH due AS (
                SELECT id FROM outgoing_messages WHERE account=%s AND status='queued'
                AND expires_at<=clock_timestamp() ORDER BY expires_at,id LIMIT 16 FOR UPDATE SKIP LOCKED)
                UPDATE outgoing_messages o SET status='expired',error_code='send_expired',finished_at=clock_timestamp()
                FROM due WHERE o.id=due.id RETURNING o.id,o.account,o.chat,o.principal,o.key_id,o.status,o.error_code""",
                (st.account,)).fetchall()
            journal_send_failures(c,rows)
            return len(rows)

    def claim(self):
        st=self.state
        with st.archive.store.pool.connection() as c:
            st.archive.fence(c,st.account,st.lease)
            interrupted=c.execute("""UPDATE outgoing_messages SET status='unknown',error_code='sender_interrupted',finished_at=clock_timestamp()
                WHERE account=%s AND status='dispatching' AND lease_epoch IS DISTINCT FROM %s
                RETURNING id,account,chat,principal,key_id,status,error_code""",
                (st.account,st.lease[1])).fetchall()
            journal_send_failures(c,interrupted)
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
                terminal=c.execute("""UPDATE outgoing_messages SET status=%s,error_code=%s,
                    finished_at=clock_timestamp() WHERE id=%s
                    RETURNING id,account,chat,principal,key_id,status,error_code""",
                    ('expired' if reason=='send_expired' else 'cancelled',reason,row['id'])).fetchall()
                journal_send_failures(c,terminal)
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
            # ANYM-837: a menu choice whose menu is gone is menu_unavailable, before the generic reply check.
            menu=job.get('menu_choice')
            if menu is not None and (not isinstance(menu,dict) or not c.execute('''SELECT 1 FROM messages WHERE id=%s
                AND account=%s AND chat=%s AND provider_id=%s AND NOT deleted''',
                (menu.get('message_id'),st.account,job['chat'],job.get('reply_provider_id'))).fetchone()):
                raise SendRejected('menu_unavailable')
            if job.get('reply_provider_id') and not c.execute('''SELECT 1 FROM messages WHERE account=%s
                AND chat=%s AND provider_id=%s AND NOT deleted''',
                (st.account,job['chat'],job['reply_provider_id'])).fetchone():
                raise SendRejected('send_target_invalid')

    def finish(self,job,status,provider_id=None,code=None):
        st=self.state
        with st.archive.store.pool.connection() as c:
            st.archive.fence(c,st.account,st.lease)
            finished=c.execute('''UPDATE outgoing_messages SET status=%s,provider_message_id=%s,error_code=%s,archive_reference=%s,
                finished_at=clock_timestamp() WHERE id=%s AND status='dispatching' AND lease_epoch=%s
                RETURNING id,account,chat,principal,key_id,status,error_code''',
                (status,provider_id,code,Jsonb(job['_archive_reference']) if job.get('_archive_reference') else None,
                 job['id'],st.lease[1])).fetchall()
            # ANYM-535: a failed or uncertain send wakes its key's subscriptions; never blocks this status.
            journal_send_failures(c,finished)

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
            code=str(exc) if str(exc) in SEND_REFUSALS else 'send_rejected'
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
