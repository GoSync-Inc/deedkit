"""Scoped durable commands dispatched only by the existing session owner.

Preview binds immutable intent, never human approval. Ambiguous provider actions
are terminal unknown and are never retried automatically.
"""
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import stat
import time

import psycopg
import psycopg.errors
from psycopg.types.json import Jsonb

from .envelope import canonical, stable_id
from .error_codes import CODES
from .policy import audit, effective_permissions, lock_principal, targets
from .store import APIError, serial
from .outgoing import finish_durably
from .open_chat_policy import (  # noqa: F401  (the moved names stay importable from here)
    _link_check, _join_guard, _phone_policy, _username_policy, _lookup_budget, _other_account_chat,
    other_account_chats, _open_chat_refusal, phone_next_step, _open_chat_next, _check_saved)

MAX_FILE_BYTES = 32 * 1024**2
LIVE_SEARCH = 'search_messages_live'
HISTORY = 'request_chat_history'
INSPECT_BUTTONS = 'inspect_message_buttons'
PRESS = 'press_button'
# ANYM-475: governor waits, resolution, the provider request and the receipt together.
LIVE_SEARCH_TIMEOUT = 15
# Kept free inside LIVE_SEARCH_TIMEOUT for the foreground loop's pacing, so a queued send starts within it.
LIVE_FOREGROUND_RESERVE = 1.0
# Preflight, governor waits and the request end this long after the claim; the rest is the receipt's.
LIVE_EXECUTION_TIMEOUT = 8
PHONE_LOOKUPS_PER_HOUR = 10
# ANYM-643: Telegram username lookups, preview and send recheck alike.
USERNAME_LOOKUPS_PER_HOUR = 20
LOOKUP_BUDGETS = {'open_chat_by_phone':PHONE_LOOKUPS_PER_HOUR,'open_chat_by_username':USERNAME_LOOKUPS_PER_HOUR}
# ANYM-532, ANYM-643: server-set fields of a phone or username preview lookup or a send's chat step, never
# caller parameters.
PHONE_INTERNAL = ('phone_preview','expected_lid','send_request_hash','preview_lookup','verified_name',
                  'username_preview','expected_user_id','preview_name')
OPEN_CHAT_ENABLE_STEP = ('owner enables: anymsg set-messenger-operations --account <account> '
    '--operations <current operations> open_chat_by_phone')
OPEN_USERNAME_ENABLE_STEP = ('owner enables: anymsg set-messenger-operations --account <account> '
    '--operations <current operations> open_chat_by_username')
log = logging.getLogger('anymsg.live_search')
# ANYM-755: an edit's send-path format options; markdown, html, a link preview or lossy rendering are Telegram only.
EDIT_FORMAT_OPTIONS = ('format','link_preview','allow_lossy')
EDIT_FORMAT_NEXT_STEP = ('WhatsApp edits are plain text: preview and execute edit_message again with only text '
    '(no format, link_preview or allow_lossy)')
# The messenger_commands.payload CHECK (migrations/025_operations.sql). A formatted edit's row repeats its source and
# rendering, so it can pass the canonical request cap and still not be storable.
STORED_PAYLOAD_BYTES = 65536
BLOCKED_RESULT_KEYS = frozenset(('token','access_token','refresh_token','session','session_data',
    'password','secret','stack','traceback','diagnostic','diagnostics','credentials'))


class CommandRejected(RuntimeError):
    """Known local rejection before invoking a provider operation."""
    def __init__(self,code,*,retry_after=None,data=None):
        super().__init__(code);self.retry_after=retry_after;self.data=data


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


def _required_permissions(permission,operation,parameters=None,provider=None):
    values={permission}
    if operation=='send_file':
        values.update(('read','upload') if isinstance(parameters,dict) and 'upload_id' in parameters else ('download',))
    if operation in ('forward_message','delete_member_messages') or (operation,provider)==('vote_poll','whatsmeow'):values.add('read')  # ANYM-934, ANYM-315: its preview shows the messages / the archived poll
    return sorted(values)


def live_read(operation,platform):
    """ANYM-475/477: an agent-requested Telegram read that answers within the live deadline or is refused."""
    return operation==LIVE_SEARCH or operation in (HISTORY,INSPECT_BUTTONS) and platform=='telegram'


def _spec(operation,platform=None):
    from .messenger_operations import operation_spec
    try:return operation_spec(operation,platform)
    except (ValueError,TypeError):raise APIError('unsupported_operation') from None


def _account(c,p,account):
    if not isinstance(account,str) or not 1<=len(account)<=64 or account not in p['accounts']:
        raise APIError('not_found',404)
    row=c.execute('SELECT * FROM accounts WHERE id=%s',(account,)).fetchone()
    if not row:raise APIError('not_found',404)
    return row


def _policy(c,account,operation,provider_chats=(),*,ready=False,live=False):
    from .collector_state import CollectorState
    row=c.execute('SELECT * FROM account_policies WHERE account=%s',(account,)).fetchone()
    if not row or not row['enabled'] or operation not in row['operations_enabled']:
        failure=APIError('operation_disabled',409)
        if operation=='open_chat_by_phone':failure.hint=OPEN_CHAT_ENABLE_STEP
        if operation=='open_chat_by_username':failure.hint=OPEN_USERNAME_ENABLE_STEP
        raise failure
    if any(not CollectorState.allows(row,chat) for chat in provider_chats):
        raise APIError('not_found',404)
    if ready:
        connected=c.execute('''SELECT l.expires_at>clock_timestamp() AS active,h.operations
            FROM collector_leases l JOIN collector_health h USING(account) WHERE l.account=%s''',
            (account,)).fetchone()
        if (not connected or not connected['active'] or
            connected['operations'].get('connection',{}).get('state')!='connected'):
            # ANYM-475: a live read is refused at once instead of waiting in the queue.
            raise APIError('session_unavailable' if live else 'sender_not_ready',409)


def _chat(c,p,account,chat_id):
    if not isinstance(chat_id,str) or not 1<=len(chat_id)<=256 or not permitted(p,account,chat_id):
        raise APIError('not_found',404)
    # Named columns: a prepared SELECT * fails once a migration adds a chats column (046, ANYM-533).
    row=c.execute('SELECT id,account,title,chat_type,provider_chat,purged_at FROM chats WHERE id=%s AND account=%s',
                  (chat_id,account)).fetchone()
    if not row or not row['provider_chat']:raise APIError('not_found',404)
    return row


def _message(c,p,account,message_id,expected_revision,chat_id=None,field='expected_revision',*,revision_optional=False,
             allow_deleted=False):
    if not isinstance(message_id,str) or not 1<=len(message_id)<=256:
        raise APIError('not_found',404)
    # ANYM-681: a live read of the keyboard names no archive revision (0); a given one must still be current.
    unchecked=revision_optional and expected_revision==0 and type(expected_revision) is int
    if not unchecked and (type(expected_revision) is not int or expected_revision<1):
        raise _rejected((field,'type' if type(expected_revision) is not int else 'minimum',REVISION))
    # ANYM-666: get_message_info asks whether a message still exists, so an archive-deleted row is a valid target.
    row=c.execute('SELECT * FROM messages WHERE id=%s AND account=%s'+('' if allow_deleted else ' AND NOT deleted'),
                  (message_id,account)).fetchone()
    if not row or (chat_id is not None and row['chat']!=chat_id) or not permitted(p,account,row['chat']):
        raise APIError('not_found',404)
    if not unchecked and row['revision']!=expected_revision:raise APIError('operation_target_changed',409)
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
             *,ready=False,verify_file=False,validate_parameters=True,retained_upload=False,saved=None):
    from .messenger_operations import validate_operation,operation_manifest,CATALOG_VERSION,ParameterError
    from .whatsapp_capabilities import account_provider
    spec=_spec(operation);account_row=_account(c,p,account);spec=_spec(operation,account_row['platform'])
    if operation==LIVE_SEARCH and not chat_id:
        # ANYM-475: without a chat the live search spans every chat of the account.
        spec.update(target='account',full_account=True)
    if operation==LIVE_SEARCH and chat_id and p['chat_ids'] is not None and chat_id not in p['chat_ids']:
        # A chat-scoped key learns nothing about chats outside its scope, existing or not.
        raise APIError('forbidden',403)
    provider=account_provider(c,account,account_row['platform'])
    required=_required_permissions(spec['permission'],operation,parameters,provider)
    if set(required)-set(p['permissions']):raise APIError('forbidden',403)
    if spec['full_account'] and p['chat_ids'] is not None:raise APIError('forbidden',403)
    requested=parameters
    if validate_parameters:
        try:parameters=validate_operation(account_row['platform'],operation,parameters,provider=provider)
        except (ValueError,TypeError,UnicodeError) as exc:
            code=str(exc) if str(exc) in CODES['operation_validation'] else 'invalid_operation_parameters'
            failure=APIError(code)
            if isinstance(exc,ParameterError):failure.field_errors=exc.field_errors
            if getattr(exc,'hint',None):failure.hint=exc.hint
            if code=='unsupported_operation' and operation=='open_chat_by_phone':
                failure.hint='native_whatsapp_accounts_only'
            if code=='unsupported_operation' and operation=='open_chat_by_username':
                failure.hint='telegram_accounts_only'
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
            message=_message(c,p,account,message_id,expected_revision,chat_id,
                             revision_optional=operation==INSPECT_BUTTONS,allow_deleted=operation=='get_message_info')
            if operation in ('edit_message','delete_message') and message['direction']!='outgoing':
                raise APIError('forbidden',403)
            payload['provider_message_id']=message['provider_id'];refs.append(message_id)
        elif message_id or expected_revision:raise _empty(spec,chat_id,message_id,expected_revision)
    if operation=='delete_member_messages':from . import member_messages;payload['targets']=member_messages.resolve(c,p,account,chat,parameters)
    if operation=='vote_poll' and provider=='whatsmeow':from .polls import bind_whatsapp_vote;bind_whatsapp_vote(c,payload,message,parameters['option_indices'])
    if operation==HISTORY and account_row['platform']=='telegram':
        anchor=parameters.get('before_message') or parameters.get('around_message')
        if anchor is not None:
            # ANYM-477: an archived message of this chat; another chat's or an unknown one looks the same.
            row=c.execute('SELECT provider_id FROM messages WHERE id=%s AND account=%s AND chat=%s',
                          (anchor,account,chat_id)).fetchone()
            if not row:raise APIError('invalid_anchor')
            payload['provider_message_id']=row['provider_id']
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
    if operation==PRESS:
        from . import button_press
        payload['press']=button_press.payload(c,chat,message,parameters)
    if operation=='edit_message':
        if validate_parameters:
            artifact=_edit_artifact(account_row['platform'],parameters)
            if artifact is not None:
                payload['edit_artifact']=artifact
                _check_formatted_edit_size(payload,requested)
        elif saved is not None and 'edit_artifact' in saved:
            # ANYM-755: the reviewed rendering is persisted intent; a dispatch never renders again (as a send).
            payload['edit_artifact']=saved['edit_artifact']
    _policy(c,account,operation,provider_chats,ready=ready,live=live_read(operation,account_row['platform']))
    if operation=='open_chat_by_phone':
        _phone_policy(c,account,parameters['phone'])
        if saved is not None:payload.update({k:saved[k] for k in PHONE_INTERNAL if k in saved})
    if operation=='open_chat_by_username':
        _username_policy(c,account)
        if saved is not None:payload.update({k:saved[k] for k in PHONE_INTERNAL if k in saved})
    if operation=='join_chat':
        # Saved intent keeps the chat the owner confirmed; a new preview binds the key's latest check.
        payload['expected_chat']=saved.get('expected_chat') if saved is not None else _link_check(c,p,account,parameters['link'])
    targets(p,refs)
    return payload,spec


def _edit_artifact(platform,parameters):
    """ANYM-755: a formatted edit is rendered by the send path's renderer, so the preview digest binds the rendered
    text and entities. A plain edit with default options has none: its payload is today's, byte for byte."""
    from .formatting import render_message
    options={k:parameters[k] for k in EDIT_FORMAT_OPTIONS if k in parameters}
    if not options:return None
    if platform!='telegram':
        failure=APIError('unsupported_operation');failure.next_step=EDIT_FORMAT_NEXT_STEP
        raise failure
    return render_message('telegram',parameters['text'],format=options.get('format','plain'),
                          link_preview=options.get('link_preview',False),allow_lossy=options.get('allow_lossy',False))


def _check_formatted_edit_size(payload,requested):
    """ANYM-755 (Sol r2): a preview refuses what its execute would. The request is measured as execute stores it, with
    a digest, and the row in the text form the CHECK measures, so a preview that passes here is storable."""
    request_input,_=_input(payload['account'],payload['operation'],payload['chat_id'],payload['message_id'],
                           payload['expected_revision'],requested,'0'*64)
    if len(json.dumps({**payload,'request_input':request_input},ensure_ascii=False).encode())>STORED_PAYLOAD_BYTES:
        raise _rejected(('parameters','size','stored formatted edit of at most %d bytes'%STORED_PAYLOAD_BYTES))


def _digest(p,payload):
    return hashlib.sha256(canonical({'principal':p['id'],'key_id':str(p['key_id']),
        'scope_version':p['scope_version'],'key_permission_version':p['key_permission_version'],
        'intent':payload})).hexdigest()


def preview(store,c,p,account,operation,chat_id='',message_id='',expected_revision=0,parameters=None):
    payload,spec=_resolve(store,c,p,account,operation,chat_id,message_id,expected_revision,parameters,
                          verify_file=True)
    shown={}
    if operation==PRESS:
        # ANYM-676: earlier presses of this button that may have happened; shown to the owner, not part of the digest.
        from . import button_press
        pressed=payload['parameters']
        shown['earlier_presses']=button_press.earlier_presses(c,account,message_id,pressed['row'],pressed['column'])
    if operation=='delete_member_messages':from . import member_messages;shown['moderation']=member_messages.shown(c,p,account,_chat(c,p,account,chat_id),payload['targets'])
    return serial({**payload,**shown,'permission':spec['permission'],'mutation':spec['mutation'],
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
        required=_required_permissions(entry['permission'],entry['operation'],provider=provider)
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


VERIFY={'delete_message':{'operation':'get_message_info','requires':'read-live','decides':'exists:false means deleted'},
    'send_reaction':{'operation':'get_reactions','requires':'read-live',
        'decides':'a reaction with chosen:true was set by this account'},
    'edit_message':{'operation':'get_message_info','requires':'read-live',
        'decides':'text equal to the requested text means the edit applied'},'delete_member_messages':{'operation':'get_message_info','requires':'read-live','decides':'per result row in state unknown: exists:false means deleted'}}
# ANYM-755 (Astra r1, Sol r2): get_message_info shows no formatting, and body_rendered leaves out what it cannot render.
VERIFY_FORMATTED_EDIT={'operation':'get_messages','requires':'read',
    'decides':'a revision newer than expected_revision whose body_rendered (body_format=html) has lossless true and '
        'empty omitted_types, and whose text, previewed as edit_message with format html, gives the rendered_text and '
        'entities of this edit\'s edit_artifact, means the edit applied; no newer revision, body_rendered_unavailable, '
        'lossless false or any difference is inconclusive: never send the edit again on it'}


def _verify(row,c=None):
    """ANYM-666/747: what an agent reads when a delete, reaction or edit stayed unknown (no secrets in the receipt)."""
    base=VERIFY.get(row['operation'])
    if base is None or row['status']!='unknown':return None
    if row['operation']=='edit_message' and 'edit_artifact' in (row.get('payload') or {}):base=VERIFY_FORMATTED_EDIT
    platform=c.execute('SELECT platform FROM accounts WHERE id=%s',(row['account'],)).fetchone() if c is not None else None
    if not platform or platform['platform']!='telegram':return None  # WhatsApp keeps its receipts as they are
    answer=row['result']
    data=answer.get('data') if isinstance(answer,dict) else None
    evidence=data.get('evidence') if isinstance(data,dict) else None
    evidence=evidence if isinstance(evidence,dict) else {}
    wanted=((row.get('payload') or {}).get('parameters') or {}).get('emoji')
    not_applied=False if row['operation']=='edit_message' else evidence.get('message_present') is True if row['operation']=='delete_message' else (
        evidence.get('basis')=='provider_read_back' and isinstance(evidence.get('reaction_present'),bool)
        and evidence['reaction_present']==(not wanted))
    if not_applied:
        # The read-back answered: the request did not take effect. Another check will not change that.
        return {'next_step':'new_request','decides':'The provider read after the request shows it has not taken effect. '
            'Another check will not change this: a new request needs a new request ID and the owner\'s confirmation.'}
    verify=dict(base)
    if type(evidence.get('retry_after_s')) is int:verify['retry_after_s']=evidence['retry_after_s']
    return verify


def contact_accounts(c,p):
    """ANYM-673: the Telegram accounts where this key may run list_contacts now, read from its own grants (never widened)."""
    if p['chat_ids'] is not None or 'contacts-read' not in p['permissions'] or not p['accounts']:return []
    rows=c.execute("""SELECT a.id FROM accounts a JOIN account_policies ap ON ap.account=a.id
        WHERE a.id=ANY(%s) AND a.platform='telegram' AND ap.enabled AND 'list_contacts'=ANY(ap.operations_enabled)
        ORDER BY a.id""",(list(p['accounts']),)).fetchall()
    return [row['id'] for row in rows]


def _public(row,c=None,p=None):
    answer=row['result']
    if row['operation']=='open_chat_by_phone' and isinstance(answer,dict) and isinstance(answer.get('data'),dict):
        # ANYM-532: the resolved LID stays server-side; it binds a phone preview's digest.
        answer={**answer,'data':{k:v for k,v in answer['data'].items() if k!='provider_chat'}}
    if isinstance(answer,dict) and isinstance(answer.get('data'),dict) and 'archive_reference' in answer['data']:
        from .outgoing import project_reference
        actual,state=project_reference(c,p,row['account'],answer['data']['archive_reference'])
        answer={**answer,'data':{**answer['data'],'archive_reference':actual,'archive_reference_state':state}}
    if row['operation']=='chat_info':
        from .chat_roles import gate_chat_info
        answer=gate_chat_info(c,answer)
    step,refusal=_open_chat_next(row,c,p)
    return serial({**({'next_call':step} if step else {}),**{k:v for k,v in (refusal or {}).items() if k!='next_action'},'operation_id':row['id'],'operation':row['operation'],'account':row['account'],
        'chat_id':row['chat'],'message_id':row['message'],'expected_revision':row['expected_revision'],
        'status':row['status'],'created_at':row['created_at'],'expires_at':row['expires_at'],
        'finished_at':row['finished_at'],'error_code':row['error_code'],'result':answer,
        'request_id':row['request_id'],'preview_digest':row['preview_digest'],
        'owner_approval_verified_by_server':False,'automatic_retry':False,
        **({'verify':verify} if (verify:=_verify(row,c)) else {}),
        'next_action':refusal['next_action'] if refusal else
            'get_messenger_operation' if row['status'] in ('queued','dispatching') else
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
            request_id='',preview_digest='',internal=None,child=False):
    from .outgoing import CHILD_KEY_PREFIX
    from . import preview_refusal

    def refused(error,resolved=False):
        # ANYM-646: a caller's own operation names its preview; an internal step (phone, username) or child keeps its body.
        # The references are echoed only once the same call as the preview accepts them (this also keeps a join_chat
        # without a fresh successful link check bare); a malformed digest is refused before that.
        if child or internal:return error
        if not resolved:
            try:_resolve(store,c,p,account,operation,chat_id,message_id,expected_revision,parameters,verify_file=True)
            except Exception:return error
        return preview_refusal.operation(error,p,account,operation,chat_id,message_id,expected_revision,parameters)
    # ANYM-546: the child prefix is reserved for messages_send steps (child=True), never a caller's key.
    if not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,128}',request_id) or (
            request_id.startswith(CHILD_KEY_PREFIX)!=child):
        raise APIError('invalid_request_id')
    if not isinstance(preview_digest,str) or (preview_digest and not re.fullmatch(r'[a-f0-9]{64}',preview_digest)):
        raise refused(APIError('preview_mismatch',409))
    from .silent_send import operation_parameters  # ANYM-927: an explicit silent false is the omitted one.
    parameters=operation_parameters(operation,parameters)
    request_input,request_hash=_input(account,operation,chat_id,message_id,expected_revision,parameters,preview_digest)
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('commands:'+str(p['key_id']),))
    existing=c.execute('SELECT * FROM messenger_commands WHERE principal=%s AND key_id=%s AND request_id=%s',
        (p['id'],p['key_id'],request_id)).fetchone()
    if existing:
        if existing['request_hash']!=request_hash or any(
                existing['payload'].get(k)!=v for k,v in (internal or {}).items()):
            raise APIError('idempotency_conflict',409)
        _check_saved(store,c,p,existing,ready=False,check_versions=False)
        return _public(existing,c,p)
    payload,spec=_resolve(store,c,p,account,operation,chat_id,message_id,expected_revision,parameters,
                         ready=True,verify_file=True)
    if internal:payload.update({k:v for k,v in internal.items() if k in PHONE_INTERNAL})
    expected=_digest(p,payload)
    if spec['mutation'] and not preview_digest:raise refused(APIError('preview_required',409),True)
    if preview_digest and preview_digest!=expected:raise refused(APIError('preview_mismatch',409),True)
    payload['request_input']=request_input
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('commands-account:'+account,))
    if operation=='join_chat':_join_guard(c,account,payload['parameters']['link'])
    if operation==PRESS:
        from . import button_press
        pressed=payload['parameters']
        button_press.guard(c,account,message_id,pressed['row'],pressed['column'],pressed.get('acknowledge_uncertain'))
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
        self.settling,self.unsettled=set(),None

    def _settled(self,task):
        self.settling.discard(task)
        if not task.cancelled() and task.exception() is not None:self.unsettled=task.exception()

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
            # A row locked by a live receipt is skipped, not waited for under the fence; a later claim recovers it.
            c.execute("""UPDATE messenger_commands SET status='unknown',error_code='operation_result_unknown',
                finished_at=clock_timestamp() WHERE id IN (SELECT id FROM messenger_commands WHERE account=%s
                AND status='dispatching' AND (lease_epoch IS DISTINCT FROM %s OR expires_at<=clock_timestamp())
                FOR UPDATE SKIP LOCKED)""",(st.account,st.lease[1]))
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
        pool=store.pool
        if job.get('_db_deadline') is not None:
            # A live read (release-042 B1): the server ends this transaction, and frees the lease fence it
            # holds, at the read's deadline, even when the awaiting caller has timed out and this thread stalls.
            from .telegram_collector import DeadlinePool
            pool=DeadlinePool(store.pool,job['_db_deadline'])
        try:
            with pool.connection() as c:
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
                if payload['operation']=='open_chat_by_username':
                    payload['lookup_allowed'],job['_lookup_resets_in']=_lookup_budget(
                        c,st.account,job['id'],'open_chat_by_username')
                if payload['operation']=='send_file':
                    # Only collectors get a filesystem path; previews/status never do.
                    root=store.files
                    if payload.get('file_upload_id'):
                        from .uploads import _root
                        root=_root(store,payload['file_upload_id'])
                    payload.update(registered_file_path=str(root/payload['file_digest']),trusted_blob_directory=str(root))
                job.update(payload)
                return job
        except Exception as exc:
            if job.get('_db_deadline') is not None and isinstance(exc,(TimeoutError,psycopg.Error)):
                raise CommandRejected('provider_timeout') from None
            if not isinstance(exc,(APIError,OSError,ValueError,KeyError)):raise
            raise CommandRejected(exc.code if isinstance(exc,APIError) else 'operation_target_changed') from None

    def finish(self,job,status,result=None,code=None):
        st=self.state
        with st.archive.store.pool.connection() as c:
            st.archive.fence(c,st.account,st.lease)
            c.execute('''UPDATE messenger_commands SET status=%s,result=%s,error_code=%s,finished_at=clock_timestamp()
                WHERE id=%s AND status='dispatching' AND lease_epoch=%s''',
                (status,Jsonb(result) if result is not None else None,code,job['id'],st.lease[1]))

    def finish_live(self,job,status,result,code,budget):
        """Record a live read's receipt in one attempt bounded by the budget left (ANYM-475).

        It takes no lease fence: the fence locks the lease row every send claim needs, and a
        commit can stall while holding it. The UPDATE checks ownership without a lock and writes
        only a dispatching row of this lease; a new owner's recovery skips the row while the receipt
        holds it and otherwise rechecks its status, so neither overwrites the other's outcome. It
        runs off the foreground path (see settle in _once). The pool wait and each
        statement get only what is left of one deadline. Nothing is retried: the row stays
        dispatching, and claim or the receipt read reports it unknown after the lease changes or
        the operation expires. A lost lease stops the collector at its next fenced step."""
        import psycopg
        from psycopg_pool import PoolTimeout
        st=self.state;end=time.monotonic()+budget
        def left():
            remaining=end-time.monotonic()
            if remaining<=0:raise TimeoutError('live_receipt_deadline')
            return remaining
        def bound(c):
            ms=str(max(1,int(left()*1000)))
            c.execute("SELECT set_config('lock_timeout',%s,true),set_config('statement_timeout',%s,true)",(ms,ms))
        try:
            with st.archive.store.pool.connection(timeout=left()) as c:
                bound(c);c.execute('''UPDATE messenger_commands SET status=%s,result=%s,error_code=%s,finished_at=clock_timestamp()
                    WHERE id=%s AND account=%s AND status='dispatching' AND lease_epoch=%s AND EXISTS(
                        SELECT 1 FROM collector_leases WHERE account=%s AND owner=%s AND epoch=%s
                        AND expires_at>clock_timestamp())''',
                    (status,Jsonb(result) if result is not None else None,code,job['id'],st.account,st.lease[1],
                     st.account,st.lease[0],st.lease[1]))
        except (psycopg.OperationalError,PoolTimeout,TimeoutError) as exc:
            log.warning('live_search receipt_unrecorded %s',type(exc).__name__)

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
                    title=name
                    if 'expected_lid' in row['payload']:
                        # ANYM-532: a phone send's chat step opens only the previewed identity, under the
                        # account's sending opt-in, titled with the preview's verified name.
                        if chat!=row['payload']['expected_lid']:raise APIError('identity_changed')
                        if not policy['enabled'] or not policy['send_enabled']:raise APIError('sending_disabled')
                        title=name or row['payload'].get('verified_name')
                    chat_id=stable_id('chat',st.account,chat);public['provider_chat']=chat
                    existing=c.execute('SELECT 1 FROM chats WHERE id=%s',(chat_id,)).fetchone() is not None
                    if not existing and row['payload']['parameters'].get('create') is True:
                        existing=c.execute('''INSERT INTO chats(id,account,title,chat_type,provider_chat)
                            VALUES(%s,%s,%s,'private',%s) ON CONFLICT DO NOTHING RETURNING id''',
                            (chat_id,st.account,title or '+'+row['payload']['parameters']['phone'],chat)).fetchone() is None
                        public.update(chat_id=chat_id,existing=existing)
                    elif existing:public.update(chat_id=chat_id,existing=True)
                status,result,code='succeeded',{'outcome':'confirmed','data':public},None
            except APIError as exc:status,result,code='failed',None,exc.code
            c.execute('''UPDATE messenger_commands SET status=%s,result=%s,error_code=%s,finished_at=clock_timestamp()
                WHERE id=%s''',(status,Jsonb(result) if result is not None else None,code,row['id']))

    def finish_open_username(self,job,answer):
        """ANYM-643: recheck authority and policy against the resolved Telegram user, then create or reuse its
        private chat and record the receipt in one fenced transaction."""
        from .collector_state import CollectorState
        st=self.state;store=st.archive.store
        with store.pool.connection() as c:
            st.archive.fence(c,st.account,st.lease)
            row=c.execute('''SELECT * FROM messenger_commands WHERE id=%s AND account=%s AND status='dispatching'
                AND lease_epoch=%s FOR UPDATE''',(job['id'],st.account,st.lease[1])).fetchone()
            if not row:return
            data=answer.get('data') if isinstance(answer.get('data'),dict) else {}
            user,username,name=data.get('user_id'),data.get('username'),data.get('name')
            try:
                if (not isinstance(user,str) or not re.fullmatch(r'[1-9][0-9]{0,18}',user) or
                    not isinstance(username,str) or not re.fullmatch(r'[a-z][a-z0-9_]{3,31}',username) or
                    name is not None and (not isinstance(name,str) or not name.strip() or len(name)>512)):
                    raise APIError('operation_result_unknown')
                _check_saved(store,c,self._identity(c,row),row,ready=False,verify_file=False)
                policy=c.execute('SELECT * FROM account_policies WHERE account=%s',(st.account,)).fetchone()
                if not policy or not policy['discover_chats'] or not CollectorState.allows(policy,user):
                    raise APIError('chat_not_collected')
                if 'expected_user_id' in row['payload']:
                    # A username send's chat step opens only the previewed person, under the sending opt-in.
                    if user!=row['payload']['expected_user_id']:raise APIError('identity_changed')
                    if not policy['enabled'] or not policy['send_enabled']:raise APIError('sending_disabled')
                public={'found':True,'user_id':user,'username':username,'name':name,'is_bot':data.get('is_bot') is True,
                        'provider_lookup':True}
                chat_id=stable_id('chat',st.account,user)
                existing=c.execute('SELECT 1 FROM chats WHERE id=%s',(chat_id,)).fetchone() is not None
                if not existing and row['payload']['parameters'].get('create') is True:
                    existing=c.execute('''INSERT INTO chats(id,account,title,chat_type,provider_chat)
                        VALUES(%s,%s,%s,'private',%s) ON CONFLICT DO NOTHING RETURNING id''',
                        (chat_id,st.account,name or '@'+username,user)).fetchone() is None
                    public.update(chat_id=chat_id,existing=existing)
                elif existing:public.update(chat_id=chat_id,existing=True)
                else:public['existing']=False
                status,result,code='succeeded',{'outcome':'confirmed','data':public},None
            except APIError as exc:status,result,code='failed',None,exc.code
            c.execute('''UPDATE messenger_commands SET status=%s,result=%s,error_code=%s,finished_at=clock_timestamp()
                WHERE id=%s''',(status,Jsonb(result) if result is not None else None,code,row['id']))

    async def once(self):
        if self.unsettled is not None:raise self.unsettled
        if await asyncio.to_thread(self.state.cooldown_remaining)>0:return False
        if self.gate is not None:
            async with self.gate:return await self._once()
        return await self._once()

    async def _once(self):
        job=await asyncio.to_thread(self.claim)
        if not job:return False
        if job.get('cancelled'):return True
        phases=[]
        live=live_read(job['operation'],job.get('platform'))
        # ANYM-475: one deadline from the claim bounds preflight, governor waits, the request and the receipt.
        loop=asyncio.get_running_loop();end=loop.time()+max(0.0,LIVE_SEARCH_TIMEOUT-LIVE_FOREGROUND_RESERVE)
        deadline=min(end,loop.time()+LIVE_EXECUTION_TIMEOUT)
        def left():return max(0.0,deadline-loop.time())
        # loop.time() is time.monotonic(), the clock of DeadlinePool.
        if live:job['_db_deadline']=deadline
        async def settle(*args):
            if not live:return await finish_durably(self.finish,*args)
            # One tracked attempt off the foreground path: no receipt commit holds up the send loop.
            # At most one in flight, so receipts take at most one pool connection; a skipped one
            # leaves the row dispatching, read as unknown once it expires.
            if self.settling:return log.warning('live_search receipt_unrecorded receipt_in_flight')
            task=asyncio.create_task(asyncio.to_thread(self.finish_live,*args,max(0.0,end-loop.time())))
            self.settling.add(task);task.add_done_callback(self._settled)
        try:
            if not live:await asyncio.to_thread(self.preflight,job)
            else:
                try:await asyncio.wait_for(asyncio.to_thread(self.preflight,job),left())
                except TimeoutError:raise CommandRejected('provider_timeout') from None
            async def last_moment_preflight():
                await asyncio.to_thread(self.preflight,job,verify_file=False)
            job['_preflight']=last_moment_preflight
            if job['operation']=='open_chat_by_phone':
                async def archived(chat):return await asyncio.to_thread(self.archived,chat)
                job['_archived']=archived
            # Providers report 'waiting' and then 'started' before the first mutation.
            job['_provider_phase']=phases.append
            timeout=180 if job['operation']=='send_file' else 30
            # The collector stops starting archive writes for a live read before this deadline.
            if live:timeout=left();job['_deadline']=deadline
            job['_ends_at']=loop.time()+timeout  # Work after the provider change stays inside this timeout.
            try:answer=safe_result(await asyncio.wait_for(self.execute(job),timeout))
            except (TimeoutError,psycopg.OperationalError,psycopg.errors.TransactionTimeout,
                    psycopg.errors.IdleInTransactionSessionTimeout):
                # A read changes nothing at the provider, so its timeout is a safe-to-repeat refusal. Its
                # database work ends at the deadline on the server (DeadlinePool), and that error can arrive
                # before the asyncio timer: query canceled, lock or transaction timeout, pool timeout.
                if job['mutation'] or not live:raise
                raise CommandRejected('provider_timeout') from None
            if isinstance(answer.get('data'),dict) and 'archive_reference' in answer['data']:
                from .outgoing import archive_reference
                archive_reference(job,{'provider_message_id':answer.get('provider_message_id'),
                    'archive_reference':answer['data']['archive_reference']})
        except CommandRejected as exc:
            code=str(exc) if str(exc) in ('operation_expired','operation_disabled','operation_access_changed',
                'operation_target_changed','sender_not_ready','attachment_unavailable','unsupported_operation',
                'forbidden','not_found','operation_rejected','identity_unresolved','chat_not_collected',
                'rate_limited','provider_timeout','invalid_phone','identity_changed','sending_disabled','invite_invalid','invite_expired','chat_full',
                'join_not_allowed','already_member','invalid_cursor','invalid_anchor','username_not_found',
                'recipient_not_user','message_changed','password_required','press_uncertain',
                'button_kind_unsupported','not_admin','missing_admin_right','poll_secret_unavailable') else 'operation_rejected'
            # Nothing was sent, so a lookup that ran out of time is a retryable read.
            result={'data':{'code':code,'retry':True}} if code=='provider_timeout' else {'data':{'code':code,**exc.data}} if code in ('not_admin','missing_admin_right') and isinstance(exc.data,dict) else None
            # ANYM-676: a press refused before it left (the live message differs from the preview) returns the live
            # text and labels so the owner can reconfirm in one round; nothing was sent.
            if code=='message_changed' and isinstance(exc.data,dict):
                result={'data':{'code':code,**exc.data,'next_action':'inspect again, preview and confirm again'}}
            if code=='rate_limited' and exc.retry_after:
                result={'data':{'code':code,'retry_after':exc.retry_after}}
                if getattr(exc,'provider_lookup',None) is False:result['data']['provider_lookup']=False
            # ANYM-643: a Telegram flood wait (retry_after) reached the provider, so it counts as a lookup.
            if code=='rate_limited' and (job['operation']=='open_chat_by_phone' or
                                         job['operation']=='open_chat_by_username' and not exc.retry_after):
                result={'data':{'code':code,'resets_in_seconds':job.get('_lookup_resets_in') or 1,'provider_lookup':False}}
            await settle(job,'expired' if code=='operation_expired' else 'failed',result,code)
        except Exception:
            # A provider that reports no phase stays conservative: any mutation may have run.
            if job['mutation'] and phases and phases[-1]!='started':
                await settle(job,'failed',None,'operation_not_started')
            else:
                await settle(job,'unknown' if job['mutation'] else 'failed',None,'operation_result_unknown')
        else:
            if job['operation']=='open_chat_by_phone' and answer['outcome']=='confirmed':
                await finish_durably(self.finish_open_chat,job,answer)
                return True
            if job['operation']=='open_chat_by_username' and answer['outcome']=='confirmed':
                await finish_durably(self.finish_open_username,job,answer)
                return True
            if (job['operation']==HISTORY and job.get('platform')=='whatsapp' and isinstance(answer.get('data'),dict)
                    and 'code' not in answer['data']):
                # ANYM-477: the phone sends the messages later; the agent searches the archive again after a minute.
                answer['data'].update(delivery='via_phone',search_again_after_s=60)
            confirmed=answer['outcome']=='confirmed'
            await settle(job,'succeeded' if confirmed else
                'unknown' if job['mutation'] else 'failed',answer,None if confirmed else 'operation_result_unknown')
            if job['operation']=='delete_member_messages':from .member_messages import record_deletions;await record_deletions(self.state,job,answer)
        # A cancellation that came while optional work followed a provider change resumes once the receipt is settled.
        if job.get('_cancel_after_settle') and asyncio.current_task().cancelling():raise asyncio.CancelledError
        return True
