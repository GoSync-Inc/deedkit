"""Scoped durable commands dispatched only by the existing session owner.

Preview binds immutable intent, never human approval. Ambiguous provider actions
are terminal unknown and are never retried automatically.
"""
import asyncio
import hashlib
import math
import os
import re
import stat

from psycopg.types.json import Jsonb

from .envelope import canonical, stable_id
from .policy import audit, effective_permissions, lock_principal, targets
from .store import APIError, serial
from .outgoing import finish_durably

MAX_FILE_BYTES = 32 * 1024**2
PHONE_LOOKUPS_PER_HOUR = 10
OPEN_CHAT_ENABLE_STEP = ('owner enables: anymsg set-messenger-operations --account <account> '
    '--operations <current operations> open_chat_by_phone')
BLOCKED_RESULT_KEYS = frozenset(('token','access_token','refresh_token','session','session_data',
    'password','secret','stack','traceback','diagnostic','diagnostics','credentials'))


class CommandRejected(RuntimeError):
    """Known local rejection before invoking a provider operation."""


def safe_result(value):
    """Bound provider data; exception strings and credentials are never results."""
    def walk(item,depth=0):
        if depth>8:raise ValueError('invalid_operation_result')
        if item is None or type(item) is bool:return item
        if type(item) in (int,float):
            if not math.isfinite(item) or abs(item)>2**63:raise ValueError('invalid_operation_result')
            return item
        if isinstance(item,str):
            if len(item)>4096 or any(ord(v)<32 and v not in '\n\r\t' for v in item):
                raise ValueError('invalid_operation_result')
            return item
        if isinstance(item,list) and len(item)<=100:return [walk(v,depth+1) for v in item]
        if isinstance(item,dict) and len(item)<=100:
            result={}
            for key,entry in item.items():
                if not isinstance(key,str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',key) or key.lower() in BLOCKED_RESULT_KEYS:
                    raise ValueError('invalid_operation_result')
                result[key]=walk(entry,depth+1)
            return result
        raise ValueError('invalid_operation_result')
    if not isinstance(value,dict) or value.get('outcome') not in ('confirmed','submitted','unknown'):
        raise ValueError('invalid_operation_result')
    result={key:walk(value[key]) for key in ('outcome','data','provider_message_id') if key in value}
    if len(canonical(result))>60000:raise ValueError('invalid_operation_result')
    return result


def _rejected(*errors):
    """invalid_operation_parameters naming reviewed fields; submitted values are never returned."""
    from .messenger_operations import field_error
    failure=APIError('invalid_operation_parameters');failure.field_errors=[field_error(*e) for e in errors]
    return failure


REVISION='integer of at least 1: the current revision of the target message'


def _empty(spec,chat_id,message_id,expected_revision):
    # Account targets take no chat or message; chat targets take no message.
    where='an account-level' if spec['target']=='account' else 'a chat-level'
    names=[n for n,v in (('chat_id',chat_id),('message_id',message_id),('expected_revision',expected_revision))
        if v and (n!='chat_id' or spec['target']=='account')]
    return _rejected(*[(n,'unknown_field','empty for '+where+' operation') for n in names])


def permitted(p,account,chat):
    return account in p['accounts'] and (p['chat_ids'] is None or chat in p['chat_ids'])


def _required_permissions(permission,operation,parameters=None):
    values={permission}
    if operation=='send_file':
        values.update(('read','upload') if isinstance(parameters,dict) and 'upload_id' in parameters else ('download',))
    if operation=='forward_message':values.add('read')
    return sorted(values)


def _spec(operation):
    from .messenger_operations import operation_spec
    try:return operation_spec(operation)
    except (ValueError,TypeError):raise APIError('unsupported_operation') from None


def _account(c,p,account):
    if not isinstance(account,str) or not 1<=len(account)<=64 or account not in p['accounts']:
        raise APIError('not_found',404)
    row=c.execute('SELECT * FROM accounts WHERE id=%s',(account,)).fetchone()
    if not row:raise APIError('not_found',404)
    return row


def _policy(c,account,operation,provider_chats=(),*,ready=False):
    from .collector_state import CollectorState
    row=c.execute('SELECT * FROM account_policies WHERE account=%s',(account,)).fetchone()
    if not row or not row['enabled'] or operation not in row['operations_enabled']:
        failure=APIError('operation_disabled',409)
        if operation=='open_chat_by_phone':failure.hint=OPEN_CHAT_ENABLE_STEP
        raise failure
    if any(not CollectorState.allows(row,chat) for chat in provider_chats):
        raise APIError('not_found',404)
    if ready:
        connected=c.execute('''SELECT l.expires_at>clock_timestamp() AS active,h.operations
            FROM collector_leases l JOIN collector_health h USING(account) WHERE l.account=%s''',
            (account,)).fetchone()
        if (not connected or not connected['active'] or
            connected['operations'].get('connection',{}).get('state')!='connected'):
            raise APIError('sender_not_ready',409)


def _chat(c,p,account,chat_id):
    if not isinstance(chat_id,str) or not 1<=len(chat_id)<=256 or not permitted(p,account,chat_id):
        raise APIError('not_found',404)
    row=c.execute('SELECT * FROM chats WHERE id=%s AND account=%s',(chat_id,account)).fetchone()
    if not row or not row['provider_chat']:raise APIError('not_found',404)
    return row


def _message(c,p,account,message_id,expected_revision,chat_id=None,field='expected_revision'):
    if not isinstance(message_id,str) or not 1<=len(message_id)<=256:
        raise APIError('not_found',404)
    if type(expected_revision) is not int or expected_revision<1:
        raise _rejected((field,'type' if type(expected_revision) is not int else 'minimum',REVISION))
    row=c.execute('SELECT * FROM messages WHERE id=%s AND account=%s AND NOT deleted',
                  (message_id,account)).fetchone()
    if not row or (chat_id is not None and row['chat']!=chat_id) or not permitted(p,account,row['chat']):
        raise APIError('not_found',404)
    if row['revision']!=expected_revision:raise APIError('operation_target_changed',409)
    return row


def _registered_file(store,c,p,account,parameters,*,verify=False):
    aid=parameters['attachment_id']
    row=c.execute('''SELECT a.*,m.account,m.chat,m.revision,m.provider_id FROM attachments a
        JOIN messages m ON m.id=a.message WHERE a.id=%s AND a.active AND NOT m.deleted''',(aid,)).fetchone()
    if not row or row['account']!=account or not permitted(p,account,row['chat']):
        raise APIError('not_found',404)
    if row['digest']!=parameters['sha256']:raise APIError('operation_target_changed',409)
    if not 1<=row['bytes']<=MAX_FILE_BYTES:raise APIError('attachment_unavailable',409)
    path=store.files/row['digest']
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size!=row['bytes']:
            raise APIError('attachment_unavailable',409)
        if verify:
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size!=row['bytes']:
                    raise APIError('attachment_unavailable',409)
                sha=hashlib.sha256();size=0
                with os.fdopen(fd,'rb',closefd=False) as stream:
                    for chunk in iter(lambda:stream.read(1024*1024),b''):
                        size+=len(chunk)
                        if size>MAX_FILE_BYTES:raise APIError('attachment_unavailable',409)
                        sha.update(chunk)
                if size!=row['bytes'] or sha.hexdigest()!=row['digest']:
                    raise APIError('attachment_unavailable',409)
            finally:os.close(fd)
    except OSError:raise APIError('attachment_unavailable',409) from None
    return row


def _resolve(store,c,p,account,operation,chat_id,message_id,expected_revision,parameters,
             *,ready=False,verify_file=False,validate_parameters=True,retained_upload=False):
    from .messenger_operations import validate_operation,operation_manifest,CATALOG_VERSION,ParameterError
    from .whatsapp_capabilities import account_provider
    spec=_spec(operation);account_row=_account(c,p,account)
    provider=account_provider(c,account,account_row['platform'])
    required=_required_permissions(spec['permission'],operation,parameters)
    if set(required)-set(p['permissions']):raise APIError('forbidden',403)
    if spec['full_account'] and p['chat_ids'] is not None:raise APIError('forbidden',403)
    if validate_parameters:
        try:parameters=validate_operation(account_row['platform'],operation,parameters,provider=provider)
        except (ValueError,TypeError,UnicodeError) as exc:
            code=str(exc) if str(exc) in ('unsupported_operation','invalid_operation_parameters','invalid_phone') else 'invalid_operation_parameters'
            failure=APIError(code)
            if isinstance(exc,ParameterError):failure.field_errors=exc.field_errors
            if getattr(exc,'hint',None):failure.hint=exc.hint
            if code=='unsupported_operation' and operation=='open_chat_by_phone':
                failure.hint='native_whatsapp_accounts_only'
            raise failure from None
    else:
        # Persisted normalized parameters are immutable. Do not apply a newer
        # normalizer to an idempotent retry or a collector dispatch.
        supported={entry['operation'] for entry in operation_manifest(account_row['platform'],provider=provider) if entry['supported']}
        if operation not in supported:raise APIError('unsupported_operation')
        if not isinstance(parameters,dict):raise _rejected(('parameters','type','object'))
    invalid=[(n,'type','string') for n,v in (('chat_id',chat_id),('message_id',message_id)) if not isinstance(v,str)]
    if type(expected_revision) is not int or expected_revision<0:
        invalid.append(('expected_revision','type' if type(expected_revision) is not int else 'minimum',
                        'integer of at least 0; 0 when the operation has no message target'))
    if invalid:raise _rejected(*invalid)
    payload={'platform':account_row['platform'],'catalog_version':CATALOG_VERSION,
        'operation':operation,'account':account,**({'adapter':provider} if provider=='whatsmeow' else {}),
        'authorization_binding':{'target':spec['target'],'full_account':spec['full_account'],
                                 'required_permissions':required},
        'chat_id':chat_id,'message_id':message_id,'expected_revision':expected_revision,
        'parameters':parameters,'provider_chat':None,'provider_message_id':None}
    refs=[];provider_chats=[]
    if spec['target']=='account':
        if chat_id or message_id or expected_revision:raise _empty(spec,chat_id,message_id,expected_revision)
    else:
        chat=_chat(c,p,account,chat_id)
        payload['provider_chat']=chat['provider_chat'];refs.append(chat_id);provider_chats.append(chat['provider_chat'])
        if spec['target']=='message':
            message=_message(c,p,account,message_id,expected_revision,chat_id)
            if operation in ('edit_message','delete_message') and message['direction']!='outgoing':
                raise APIError('forbidden',403)
            payload['provider_message_id']=message['provider_id'];refs.append(message_id)
        elif message_id or expected_revision:raise _empty(spec,chat_id,message_id,expected_revision)
    if operation=='forward_message':
        source=_message(c,p,account,parameters['source_message_id'],parameters['source_revision'],field='source_revision')
        source_chat=_chat(c,p,account,source['chat'])
        payload.update(source_chat_id=source['chat'],source_provider_chat=source_chat['provider_chat'],
            source_provider_message=source['provider_id'])
        provider_chats.append(source_chat['provider_chat']);refs.extend((source['chat'],source['id']))
    if operation=='send_file':
        if 'upload_id' in parameters:
            from .uploads import source
            original=source(store,c,p,account,chat_id,parameters,verify=verify_file,retained=retained_upload)
            payload.update(file_upload_id=original['id'],file_digest=original['sha256'],file_bytes=original['bytes'],
                file_mime=original['mime'],file_name=original['filename'])
            refs.append(original['id'])
        else:
            original=_registered_file(store,c,p,account,parameters,verify=verify_file)
            source_chat=_chat(c,p,account,original['chat'])
            payload.update(file_message_id=original['message'],file_message_revision=original['revision'],
                file_chat_id=original['chat'],file_digest=original['digest'],file_bytes=original['bytes'],
                file_mime=original['mime'],file_name=original['filename'])
            provider_chats.append(source_chat['provider_chat']);refs.extend((original['chat'],original['message'],original['id']))
        mode=parameters['mode'];mime=payload['file_mime']
        family={'photo':'image/','video':'video/','video_note':'video/','audio':'audio/','voice':'audio/','sticker':'image/'}.get(mode)
        if family and not mime.startswith(family):
            raise _rejected(('mode','format','a mode matching the original MIME family: photo and sticker image/*, '
                'video and video_note video/*, audio and voice audio/*; document for any file'))
        if account_row['platform']=='telegram' and mode=='photo' and (
            mime not in ('image/jpeg','image/png','image/webp') or payload['file_bytes']>10*1024**2):
            raise APIError('unsupported_operation')
        if provider=='whatsmeow' and (mode=='sticker' and mime!='image/webp' or
            mode in ('audio','voice','sticker') and parameters['caption']):raise APIError('unsupported_operation')
    _policy(c,account,operation,provider_chats,ready=ready)
    if operation=='open_chat_by_phone':_phone_policy(c,account,parameters['phone'])
    targets(p,refs)
    return payload,spec


def _phone_policy(c,account,phone):
    """Conservative admission: the LID is unknown before the query, so it can
    only be collected under discovery; exclusions apply to every known namespace."""
    from .collector_state import CollectorState
    row=c.execute('SELECT * FROM account_policies WHERE account=%s',(account,)).fetchone()
    known=[phone+'@c.us',*(r['lid'] for r in c.execute('''SELECT DISTINCT lid FROM whatsapp_identity_observations
        WHERE account=%s AND pn=%s LIMIT 20''',(account,phone+'@c.us')).fetchall())]
    if not row or not row['discover_chats'] or any(not CollectorState.allows(row,chat) for chat in known):
        raise APIError('chat_not_collected',409)


def _lookup_budget(c,account,command):
    """Whether this dispatch may query WhatsApp, and when the oldest counted query
    leaves the window. A dispatched row counts until an hour after it finished
    (a query runs between claim and finish; an unfinished row counts now), so
    neither a queue delay, a slow store read nor a restart resets the bound.
    Dispatch is serial per account (one fenced worker), so two principals cannot
    both take the last slot. Local answers and bound refusals do not count."""
    rows=c.execute('''SELECT coalesce(finished_at,clock_timestamp()) AS at FROM messenger_commands
        WHERE account=%s AND operation='open_chat_by_phone' AND id<>%s AND started_at IS NOT NULL
        AND coalesce(finished_at,clock_timestamp())>clock_timestamp()-interval '1 hour'
        AND coalesce(result->'data'->>'provider_lookup','true')<>'false'
        ORDER BY 1 DESC LIMIT %s''',(account,command,PHONE_LOOKUPS_PER_HOUR)).fetchall()
    if len(rows)<PHONE_LOOKUPS_PER_HOUR:return True,None
    return False,c.execute('''SELECT greatest(1,ceil(extract(epoch FROM
        %s::timestamptz+interval '1 hour'-clock_timestamp())))::int AS s''',(rows[-1]['at'],)).fetchone()['s']


def _digest(p,payload):
    return hashlib.sha256(canonical({'principal':p['id'],'key_id':str(p['key_id']),
        'scope_version':p['scope_version'],'key_permission_version':p['key_permission_version'],
        'intent':payload})).hexdigest()


def preview(store,c,p,account,operation,chat_id='',message_id='',expected_revision=0,parameters=None):
    payload,spec=_resolve(store,c,p,account,operation,chat_id,message_id,expected_revision,parameters,
                          verify_file=True)
    return serial({**payload,'permission':spec['permission'],'mutation':spec['mutation'],
        'required_permissions':payload['authorization_binding']['required_permissions'],
        'preview_digest':_digest(p,payload),'owner_approval_verified_by_server':False,
        'changes_provider_state':spec['mutation'],'limitation':spec.get('limitation','')})


def manifest(c,p,account):
    from .messenger_operations import operation_manifest
    from .whatsapp_capabilities import account_provider
    row=_account(c,p,account)
    provider=account_provider(c,account,row['platform'])
    policy=c.execute('SELECT * FROM account_policies WHERE account=%s',(account,)).fetchone()
    items=[]
    for entry in operation_manifest(row['platform'],provider=provider):
        if not entry['supported']:continue
        required=_required_permissions(entry['permission'],entry['operation'])
        permitted_here=(not(set(required)-set(p['permissions'])) and
                        (not entry['full_account'] or p['chat_ids'] is None) and
                        (entry['target']=='account' or p['chat_ids']!=[]))
        if entry['operation']=='send_file' and {'read','send','upload'}<=set(p['permissions']) and p['chat_ids']!=[]:
            permitted_here=True
        enabled=bool(policy and policy['enabled'] and entry['operation'] in policy['operations_enabled'])
        items.append({**entry,'required_permissions':required,
            **({'source_permissions':{'attachment_id':['download','send'],'upload_id':['read','send','upload']}} if entry['operation']=='send_file' else {}),
            'key_permitted':permitted_here,'account_enabled':enabled,
            'available':bool(entry['supported'] and permitted_here and enabled)})
    return {'account':account,'platform':row['platform'],'provider':provider,'items':items,
            'owner_approval_verified_by_server':False,'automatic_retry':False}


def _public(row,c=None,p=None):
    answer=row['result']
    if isinstance(answer,dict) and isinstance(answer.get('data'),dict) and 'archive_reference' in answer['data']:
        from .outgoing import project_reference
        actual,state=project_reference(c,p,row['account'],answer['data']['archive_reference'])
        answer={**answer,'data':{**answer['data'],'archive_reference':actual,'archive_reference_state':state}}
    return serial({'operation_id':row['id'],'operation':row['operation'],'account':row['account'],
        'chat_id':row['chat'],'message_id':row['message'],'expected_revision':row['expected_revision'],
        'status':row['status'],'created_at':row['created_at'],'expires_at':row['expires_at'],
        'finished_at':row['finished_at'],'error_code':row['error_code'],'result':answer,
        'request_id':row['request_id'],'preview_digest':row['preview_digest'],
        'owner_approval_verified_by_server':False,'automatic_retry':False,
        'next_action':'get_messenger_operation' if row['status'] in ('queued','dispatching') else
            'verify_provider_state_before_any_new_action' if row['status']=='unknown' else 'none'})


def _input(account,operation,chat_id,message_id,expected_revision,parameters,preview_digest):
    if parameters is not None and not isinstance(parameters,dict):raise _rejected(('parameters','type','object'))
    value={'account':account,'operation':operation,'chat_id':chat_id,'message_id':message_id,
        'expected_revision':expected_revision,'parameters':{} if parameters is None else parameters,'preview_digest':preview_digest}
    try:
        data=canonical(value)
    except (ValueError,TypeError,UnicodeError):raise _rejected(('parameters','type','JSON object of documented parameters')) from None
    if len(data)>16000:raise _rejected(('parameters','size','canonical request of at most 16000 bytes'))
    return value,hashlib.sha256(data).hexdigest()


def enqueue(store,c,p,account,operation,chat_id='',message_id='',expected_revision=0,parameters=None,
            request_id='',preview_digest=''):
    if not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,128}',request_id):
        raise APIError('invalid_request_id')
    if not isinstance(preview_digest,str) or (preview_digest and not re.fullmatch(r'[a-f0-9]{64}',preview_digest)):
        raise APIError('preview_mismatch',409)
    request_input,request_hash=_input(account,operation,chat_id,message_id,expected_revision,parameters,preview_digest)
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('commands:'+str(p['key_id']),))
    existing=c.execute('SELECT * FROM messenger_commands WHERE principal=%s AND key_id=%s AND request_id=%s',
        (p['id'],p['key_id'],request_id)).fetchone()
    if existing:
        if existing['request_hash']!=request_hash:raise APIError('idempotency_conflict',409)
        _check_saved(store,c,p,existing,ready=False,check_versions=False)
        return _public(existing,c,p)
    payload,spec=_resolve(store,c,p,account,operation,chat_id,message_id,expected_revision,parameters,
                         ready=True,verify_file=True)
    expected=_digest(p,payload)
    if spec['mutation'] and not preview_digest:raise APIError('preview_required',409)
    if preview_digest and preview_digest!=expected:raise APIError('preview_mismatch',409)
    payload['request_input']=request_input
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('commands-account:'+account,))
    count=c.execute("SELECT count(*) AS n FROM messenger_commands WHERE account=%s AND created_at>clock_timestamp()-interval '1 minute'",
                    (account,)).fetchone()['n']
    if count>=60:raise APIError('rate_limited',429)
    row=c.execute('''INSERT INTO messenger_commands(principal,key_id,scope_version,key_permission_version,
        request_id,request_hash,account,chat,message,expected_revision,operation,permission,mutation,payload,preview_digest)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
        (p['id'],p['key_id'],p['scope_version'],p['key_permission_version'],request_id,request_hash,
         account,chat_id or None,message_id or None,expected_revision,operation,spec['permission'],
         spec['mutation'],Jsonb(payload),preview_digest or None)).fetchone()
    targets(p,[row['id'],*p.get('_audit_targets',[])])
    return _public(row,c,p)


def _check_saved(store,c,p,row,*,ready=True,check_versions=True,verify_file=True,check_dispatch=True):
    if row['principal']!=p['id'] or str(row['key_id'])!=str(p['key_id']):raise APIError('not_found',404)
    payload=row['payload']
    binding=payload.get('authorization_binding')
    if (not isinstance(binding,dict) or binding.get('target') not in ('account','chat','message') or
        type(binding.get('full_account')) is not bool or
        not isinstance(binding.get('required_permissions'),list)):
        raise APIError('operation_access_changed',409)
    required=set(binding['required_permissions'])|set(_required_permissions(row['permission'],row['operation'],payload['parameters']))
    if required-set(p['permissions']):raise APIError('forbidden',403)
    if row['key_permission_version']!=p['key_permission_version'] or (
        check_versions and row['scope_version']!=p['scope_version']):
        raise APIError('operation_access_changed',409)
    _account(c,p,row['account'])
    if binding['full_account'] and p['chat_ids'] is not None:raise APIError('forbidden',403)
    if not check_dispatch or row['status'] not in ('queued','dispatching'):
        # The completed operation may itself edit/delete the target, archive a
        # chat, or revoke its invitation. Receipts remain readable without
        # authorizing another action or requiring its former revision to exist.
        # Passive status reads use the same scope checks while still pending;
        # worker admission/preflight independently revalidate dispatch support.
        def scoped_message(message_id,chat_id):
            message=c.execute('SELECT account,chat FROM messages WHERE id=%s',(message_id,)).fetchone()
            if not message or message['account']!=row['account'] or message['chat']!=chat_id:
                raise APIError('not_found',404)
            _chat(c,p,row['account'],chat_id)
        if row['chat']:
            chat=_chat(c,p,row['account'],row['chat'])
            if chat['provider_chat']!=payload['provider_chat']:raise APIError('not_found',404)
        if row['message']:scoped_message(row['message'],row['chat'])
        if row['operation']=='forward_message':
            scoped_message(payload['parameters']['source_message_id'],payload['source_chat_id'])
        if row['operation']=='send_file':
            if 'upload_id' in payload['parameters']:
                from .uploads import owned
                original=owned(c,p,payload['parameters']['upload_id'])
                if original['account']!=row['account'] or original['chat']!=row['chat']:raise APIError('not_found',404)
            else:
                scoped_message(payload['file_message_id'],payload['file_chat_id'])
                original=c.execute('SELECT message FROM attachments WHERE id=%s',
                                   (payload['parameters']['attachment_id'],)).fetchone()
                if not original or original['message']!=payload['file_message_id']:raise APIError('not_found',404)
        return {key:value for key,value in payload.items() if key!='request_input'}
    current,spec=_resolve(store,c,p,row['account'],row['operation'],row['chat'] or '',row['message'] or '',
        row['expected_revision'],payload['parameters'],ready=ready,verify_file=verify_file,
        validate_parameters=False,retained_upload=True)
    # A catalog release must not rewrite reviewed, persisted normalized intent.
    current['catalog_version']=payload['catalog_version']
    saved={key:value for key,value in payload.items() if key!='request_input'}
    if current!=saved or spec['permission']!=row['permission'] or spec['mutation']!=row['mutation']:
        raise APIError('operation_target_changed',409)
    return current


def status(store,c,p,operation_id):
    if not isinstance(operation_id,str) or not re.fullmatch(r'op_[a-f0-9]{32}',operation_id):
        raise APIError('not_found',404)
    row=c.execute('SELECT * FROM messenger_commands WHERE id=%s AND principal=%s AND key_id=%s',
                  (operation_id,p['id'],p['key_id'])).fetchone()
    if not row:raise APIError('not_found',404)
    observed=c.execute('SELECT clock_timestamp() AS now').fetchone()['now']
    if row['status']=='queued' and row['expires_at']<=observed:
        row={**row,'status':'expired','error_code':'operation_expired'}
    elif row['status']=='dispatching':
        lease=c.execute('SELECT epoch,expires_at FROM collector_leases WHERE account=%s',(row['account'],)).fetchone()
        if (row['expires_at']<=observed or not lease or lease['epoch']!=row['lease_epoch'] or
            lease['expires_at']<=observed):
            row={**row,'status':'unknown','error_code':'operation_result_unknown'}
    _check_saved(store,c,p,row,ready=False,check_versions=False,check_dispatch=False)
    targets(p,[row['id']])
    return _public(row,c,p)


class CommandWorker:
    def __init__(self,state,execute,gate=None):
        self.state,self.execute,self.gate=state,execute,gate

    def _identity(self,c,row):
        lock_principal(c,row['principal'],shared=True)
        p=c.execute('''SELECT p.*,k.id AS key_id,k.permissions AS key_permissions,
            k.permission_version AS key_permission_version FROM principals p JOIN api_keys k ON k.principal=p.id
            WHERE p.id=%s AND k.id=%s AND p.enabled AND NOT k.revoked AND k.expires_at>clock_timestamp()''',
            (row['principal'],row['key_id'])).fetchone()
        if not p:raise APIError('operation_access_changed',409)
        p['permissions']=effective_permissions(p['permissions'],p['key_permissions'])
        return p

    def claim(self):
        st=self.state;store=st.archive.store
        with store.pool.connection() as c:
            st.archive.fence(c,st.account,st.lease)
            c.execute("""UPDATE messenger_commands SET status='unknown',error_code='operation_result_unknown',
                finished_at=clock_timestamp() WHERE account=%s AND status='dispatching'
                AND (lease_epoch IS DISTINCT FROM %s OR expires_at<=clock_timestamp())""",(st.account,st.lease[1]))
            row=c.execute("""SELECT * FROM messenger_commands WHERE account=%s AND status='queued'
                ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED""",(st.account,)).fetchone()
            if not row:return None
            try:
                now=c.execute('SELECT clock_timestamp() AS now').fetchone()['now']
                if row['expires_at']<=now:raise APIError('operation_expired',409)
                p=self._identity(c,row)
                payload=_check_saved(store,c,p,row)
            except (APIError,OSError,ValueError,KeyError) as exc:
                reason=exc.code if isinstance(exc,APIError) else 'operation_target_changed'
                c.execute('UPDATE messenger_commands SET status=%s,error_code=%s,finished_at=clock_timestamp() WHERE id=%s',
                    ('expired' if reason=='operation_expired' else 'cancelled',reason,row['id']))
                audit(c,{'id':row['principal'],'key_id':row['key_id']},'messenger_dispatch','denied',reason)
                return {'cancelled':True}
            c.execute("UPDATE messenger_commands SET status='dispatching',started_at=clock_timestamp(),lease_epoch=%s WHERE id=%s",
                      (st.lease[1],row['id']))
            audit(c,{'id':row['principal'],'key_id':row['key_id'],'_audit_targets':[row['id']]},'messenger_dispatch','ok')
            return {**row,**payload,'expires_at':row['expires_at'].timestamp(),'lease_epoch':st.lease[1]}

    def preflight(self,job,*,verify_file=True):
        """Repeat authorization immediately before provider mutation or RPC."""
        st=self.state;store=st.archive.store
        try:
            with store.pool.connection() as c:
                st.archive.fence(c,st.account,st.lease)
                row=c.execute("SELECT * FROM messenger_commands WHERE id=%s AND account=%s AND status='dispatching' AND lease_epoch=%s",
                              (job['id'],st.account,st.lease[1])).fetchone()
                if not row:raise APIError('operation_access_changed',409)
                if row['expires_at']<=c.execute('SELECT clock_timestamp() AS now').fetchone()['now']:
                    raise APIError('operation_expired',409)
                p=self._identity(c,row);payload=_check_saved(store,c,p,row,verify_file=verify_file)
                policy=c.execute('''SELECT enabled,include_chats,exclude_chats,discover_chats,operations_enabled
                    FROM account_policies WHERE account=%s''',(st.account,)).fetchone()
                if not policy:raise APIError('operation_disabled',409)
                payload['_policy']=dict(policy)
                if payload['operation']=='open_chat_by_phone':
                    payload['known_chats']=[]
                    payload['lookup_allowed'],job['_lookup_resets_in']=_lookup_budget(c,st.account,job['id'])
                if payload['operation']=='send_file':
                    # Only collectors get a filesystem path; previews/status never do.
                    root=store.files
                    if payload.get('file_upload_id'):
                        from .uploads import _root
                        root=_root(store,payload['file_upload_id'])
                    payload.update(registered_file_path=str(root/payload['file_digest']),trusted_blob_directory=str(root))
                job.update(payload)
                return job
        except (APIError,OSError,ValueError,KeyError) as exc:
            raise CommandRejected(exc.code if isinstance(exc,APIError) else 'operation_target_changed') from None

    def finish(self,job,status,result=None,code=None):
        st=self.state
        with st.archive.store.pool.connection() as c:
            st.archive.fence(c,st.account,st.lease)
            c.execute('''UPDATE messenger_commands SET status=%s,result=%s,error_code=%s,finished_at=clock_timestamp()
                WHERE id=%s AND status='dispatching' AND lease_epoch=%s''',
                (status,Jsonb(result) if result is not None else None,code,job['id'],st.lease[1]))

    def archived(self,chat):
        """Whether the archive holds this exact chat (the adapter's local LID)."""
        with self.state.archive.store.pool.connection() as c:
            return c.execute('SELECT 1 FROM chats WHERE account=%s AND provider_chat=%s',
                             (self.state.account,chat)).fetchone() is not None

    def finish_open_chat(self,job,answer):
        """Recheck authority and policy against the resolved LID, then create or
        reuse its chat and record the receipt in one fenced transaction."""
        from .collector_state import CollectorState
        st=self.state;store=st.archive.store
        with store.pool.connection() as c:
            st.archive.fence(c,st.account,st.lease)
            row=c.execute('''SELECT * FROM messenger_commands WHERE id=%s AND account=%s AND status='dispatching'
                AND lease_epoch=%s FOR UPDATE''',(job['id'],st.account,st.lease[1])).fetchone()
            if not row:return
            data=answer.get('data') if isinstance(answer.get('data'),dict) else {}
            name=data.get('verified_name')
            try:
                if type(data.get('on_whatsapp')) is not bool or type(data.get('provider_lookup')) is not bool or (
                    name is not None and (not isinstance(name,str) or not name.strip() or len(name)>512)):
                    raise APIError('operation_result_unknown')
                public={'on_whatsapp':data['on_whatsapp'],'verified_name':name,'provider_lookup':data['provider_lookup']}
                if data['on_whatsapp']:
                    chat=data.get('provider_chat')
                    if not isinstance(chat,str) or not re.fullmatch(r'[0-9]{1,30}@lid',chat):
                        raise APIError('operation_result_unknown')
                    _check_saved(store,c,self._identity(c,row),row,ready=False,verify_file=False)
                    policy=c.execute('SELECT * FROM account_policies WHERE account=%s',(st.account,)).fetchone()
                    if not policy or not policy['discover_chats'] or not CollectorState.allows(policy,chat):
                        raise APIError('chat_not_collected')
                    chat_id=stable_id('chat',st.account,chat)
                    existing=c.execute('SELECT 1 FROM chats WHERE id=%s',(chat_id,)).fetchone() is not None
                    if not existing and row['payload']['parameters'].get('create') is True:
                        existing=c.execute('''INSERT INTO chats(id,account,title,chat_type,provider_chat)
                            VALUES(%s,%s,%s,'private',%s) ON CONFLICT DO NOTHING RETURNING id''',
                            (chat_id,st.account,name or '+'+row['payload']['parameters']['phone'],chat)).fetchone() is None
                        public.update(chat_id=chat_id,existing=existing)
                    elif existing:public.update(chat_id=chat_id,existing=True)
                status,result,code='succeeded',{'outcome':'confirmed','data':public},None
            except APIError as exc:status,result,code='failed',None,exc.code
            c.execute('''UPDATE messenger_commands SET status=%s,result=%s,error_code=%s,finished_at=clock_timestamp()
                WHERE id=%s''',(status,Jsonb(result) if result is not None else None,code,row['id']))

    async def once(self):
        if await asyncio.to_thread(self.state.cooldown_remaining)>0:return False
        if self.gate is not None:
            async with self.gate:return await self._once()
        return await self._once()

    async def _once(self):
        job=await asyncio.to_thread(self.claim)
        if not job:return False
        if job.get('cancelled'):return True
        phases=[]
        try:
            await asyncio.to_thread(self.preflight,job)
            async def last_moment_preflight():
                await asyncio.to_thread(self.preflight,job,verify_file=False)
            job['_preflight']=last_moment_preflight
            if job['operation']=='open_chat_by_phone':
                async def archived(chat):return await asyncio.to_thread(self.archived,chat)
                job['_archived']=archived
            # Providers report 'waiting' and then 'started' before the first mutation.
            job['_provider_phase']=phases.append
            timeout=180 if job['operation']=='send_file' else 30
            answer=safe_result(await asyncio.wait_for(self.execute(job),timeout))
            if isinstance(answer.get('data'),dict) and 'archive_reference' in answer['data']:
                from .outgoing import archive_reference
                archive_reference(job,{'provider_message_id':answer.get('provider_message_id'),
                    'archive_reference':answer['data']['archive_reference']})
        except CommandRejected as exc:
            code=str(exc) if str(exc) in ('operation_expired','operation_disabled','operation_access_changed',
                'operation_target_changed','sender_not_ready','attachment_unavailable','unsupported_operation',
                'forbidden','not_found','operation_rejected','identity_unresolved','chat_not_collected',
                'rate_limited','provider_timeout','invalid_phone') else 'operation_rejected'
            # Nothing was sent, so a lookup that ran out of time is a retryable read.
            result={'data':{'code':code,'retry':True}} if code=='provider_timeout' else None
            if code=='rate_limited' and job['operation']=='open_chat_by_phone':
                result={'data':{'code':code,'resets_in_seconds':job.get('_lookup_resets_in') or 1,'provider_lookup':False}}
            await finish_durably(self.finish,job,'expired' if code=='operation_expired' else 'failed',result,code)
        except Exception:
            # A provider that reports no phase stays conservative: any mutation may have run.
            if job['mutation'] and phases and phases[-1]!='started':
                await finish_durably(self.finish,job,'failed',None,'operation_not_started')
            else:
                await finish_durably(self.finish,job,'unknown' if job['mutation'] else 'failed',None,'operation_result_unknown')
        else:
            if job['operation']=='open_chat_by_phone' and answer['outcome']=='confirmed':
                await finish_durably(self.finish_open_chat,job,answer)
                return True
            confirmed=answer['outcome']=='confirmed'
            await finish_durably(self.finish,job,'succeeded' if confirmed else
                'unknown' if job['mutation'] else 'failed',answer,None if confirmed else 'operation_result_unknown')
        return True
