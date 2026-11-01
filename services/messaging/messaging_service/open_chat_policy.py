"""Open-chat policy and the saved-operation recheck of the scoped durable commands, moved out of commands.py.

The names these functions share with commands.py (permissions, account and chat reads, the resolver and the
policy gate) are imported inside each function: commands.py re-exports this module, so a module-level import
would be a cycle, and a patch of the commands name keeps being honored at call time.
"""
from .store import APIError


def _link_check(c,p,account,link):
    """The chat a join may target: this key's latest successful check of the link key within 60 minutes."""
    row=c.execute('''SELECT result FROM messenger_commands WHERE principal=%s AND key_id=%s AND account=%s
        AND operation='check_chat_link' AND payload->'parameters'->>'link'=%s AND status='succeeded'
        AND finished_at>clock_timestamp()-interval '60 minutes' ORDER BY finished_at DESC,created_at DESC LIMIT 1''',
        (p['id'],p['key_id'],account,link)).fetchone()
    data=(row['result'] or {}).get('data') if row else None
    if not isinstance(data,dict):raise APIError('preview_required',409)
    if data.get('membership')=='member':raise APIError('already_member',409)
    if (data.get('membership')!='not_member' or not isinstance(data.get('title'),str) or
        data.get('kind') not in ('group','channel')):raise APIError('preview_required',409)
    count=data.get('member_count')
    return {'title':data['title'],'kind':data['kind'],'member_count':count if type(count) is int else None}


def _join_guard(c,account,link):
    """An earlier join of this link key on the account, from any key, that may have happened blocks a new
    one until a later check settles membership (member or not_member); no time limit ends it."""
    if c.execute('''SELECT 1 FROM messenger_commands j WHERE j.account=%s AND j.operation='join_chat'
        AND j.payload->'parameters'->>'link'=%s AND (j.status IN ('queued','dispatching') OR j.status='unknown'
        AND NOT EXISTS (SELECT 1 FROM messenger_commands k WHERE k.account=j.account AND k.operation='check_chat_link'
            AND k.payload->'parameters'->>'link'=%s AND k.status='succeeded'
            AND k.result->'data'->>'membership' IN ('member','not_member')
            AND k.started_at>coalesce(j.finished_at,j.started_at,j.created_at))) LIMIT 1''',
        (account,link,link)).fetchone():
        raise APIError('operation_result_unknown',409)


def _phone_policy(c,account,phone):
    """Conservative admission: the LID is unknown before the query, so it can
    only be collected under discovery; exclusions apply to every known namespace."""
    from .collector_state import CollectorState
    row=c.execute('SELECT * FROM account_policies WHERE account=%s',(account,)).fetchone()
    known=[phone+'@c.us',*(r['lid'] for r in c.execute('''SELECT DISTINCT lid FROM whatsapp_identity_observations
        WHERE account=%s AND pn=%s LIMIT 20''',(account,phone+'@c.us')).fetchall())]
    if not row or not row['discover_chats'] or any(not CollectorState.allows(row,chat) for chat in known):
        raise APIError('chat_not_collected',409)


def _username_policy(c,account):
    """ANYM-643: the user ID is unknown before the lookup, so it can only be collected under discovery; the
    collector and the chat step refuse a resolved ID the exclusions name."""
    row=c.execute('SELECT discover_chats FROM account_policies WHERE account=%s',(account,)).fetchone()
    if not row or not row['discover_chats']:raise APIError('chat_not_collected',409)


def _lookup_budget(c,account,command,operation='open_chat_by_phone'):
    """Whether this dispatch may query the provider (WhatsApp by phone, Telegram by username), and when the oldest counted query
    leaves the window. A dispatched row counts until an hour after it finished
    (a query runs between claim and finish; an unfinished row counts now), so
    neither a queue delay, a slow store read nor a restart resets the bound.
    Dispatch is serial per account (one fenced worker), so two principals cannot
    both take the last slot. Local answers and bound refusals do not count."""
    from .commands import LOOKUP_BUDGETS
    rows=c.execute('''SELECT coalesce(finished_at,clock_timestamp()) AS at FROM messenger_commands
        WHERE account=%s AND operation=%s AND id<>%s AND started_at IS NOT NULL
        AND coalesce(finished_at,clock_timestamp())>clock_timestamp()-interval '1 hour'
        AND coalesce(result->'data'->>'provider_lookup','true')<>'false'
        ORDER BY 1 DESC LIMIT %s''',(account,operation,command,LOOKUP_BUDGETS[operation])).fetchall()
    if len(rows)<LOOKUP_BUDGETS[operation]:return True,None
    return False,c.execute('''SELECT greatest(1,ceil(extract(epoch FROM
        %s::timestamptz+interval '1 hour'-clock_timestamp())))::int AS s''',(rows[-1]['at'],)).fetchone()['s']


def _other_account_chat(c,p,account,phone):
    """ANYM-531: an archived chat of this number on another account the key reads; archive only."""
    return next(other_account_chats(c,p,account,phone),None)


def other_account_chats(c,p,account,phone):
    """Archived chats of this number on the key's other accounts, by account and chat authorization;
    archive only, never a provider call (ANYM-531, ANYM-532)."""
    from .policy import scope_filter
    from .whatsapp_identity import resolve_alias
    if 'read' not in p['permissions']:return
    for other in sorted(set(p['accounts'])-{account}):
        where,args=scope_filter({**p,'accounts':[other]},'c',chat_column='id')
        row=c.execute("SELECT c.id FROM chats c JOIN accounts a ON a.id=c.account WHERE a.platform='whatsapp' "
            'AND c.provider_chat=%s AND '+where+' ORDER BY c.id LIMIT 1',[phone+'@c.us',*args]).fetchone()
        if not row:
            try:row=resolve_alias(c,p,other,phone+'@c.us')
            except APIError:row=None  # Ambiguous evidence names nothing.
        if row:yield {'account':other,'chat_id':row['id']}


def _open_chat_refusal(c,p,account,phone,parameters):
    """ANYM-531: why open_chat_by_phone with these parameters would be refused now, else None.
    The admission checks of execution, as reads only; it never dispatches a lookup."""
    from .commands import _policy,_required_permissions
    if (set(_required_permissions('send','open_chat_by_phone',parameters))-set(p['permissions'])
            or p['chat_ids'] is not None):return {'next_action':'key_cannot_open_new_chats'}
    try:_policy(c,account,'open_chat_by_phone',ready=True)
    except APIError as exc:
        if exc.code=='operation_disabled':
            return {'next_action':'ask_operator_to_enable_open_chat_by_phone','account':account}
        return {'next_action':exc.code}
    try:_phone_policy(c,account,phone)
    except APIError:return {'next_action':'chat_not_collected'}
    allowed,resets=_lookup_budget(c,account,'')
    if not allowed:return {'next_action':'rate_limited','resets_in_seconds':resets}
    return None


def phone_next_step(c,p,account,reference):
    """ANYM-531: the exact next call for a phone reference that is not found, or why there is none.
    Archive and policy reads only: it never dispatches a lookup. Only the caller's normalized number is echoed."""
    from .messenger_operations import phone_digits
    from .whatsapp_capabilities import account_provider
    if not isinstance(account,str) or account not in p['accounts']:return {}
    row=c.execute('SELECT platform FROM accounts WHERE id=%s',(account,)).fetchone()
    if not row:return {}
    phone,_=phone_digits(reference.split('@',1)[0])
    if phone is None:return {'next_action':'invalid_phone'}
    found=_other_account_chat(c,p,account,phone)
    if found:return {'found_on_other_account':found,'next_action':'use_existing_chat'}
    if row['platform']!='whatsapp' or account_provider(c,account,row['platform'])!='whatsmeow':
        return {'hint':'native_whatsapp_accounts_only','next_action':'use_username_or_existing_chat'}
    refusal=_open_chat_refusal(c,p,account,phone,{'phone':'+'+phone,'create':False})
    if refusal:return refusal
    # ANYM-532: the phone preview does the lookup; its digest then sends to the number.
    return {'next_action':'preview_message_then_send_message_with_its_digest',
        'next_call':{'tool':'preview_message','arguments':{'account':account,'phone':'+'+phone},
            'caller_supplies':['text'],'caller_may_supply':['format','reply_to']}}


def _open_chat_next(row,c=None,p=None):
    """ANYM-531: the next call after an open_chat_by_phone receipt, the shortest permitted path,
    as (next_call, refusal); a create step that execution would refuse now gives its refusal instead."""
    if row['operation']!='open_chat_by_phone':return None,None
    if row['status'] in ('queued','dispatching'):
        return {'tool':'get_messenger_operation','arguments':{'operation_id':row['id']},'caller_supplies':[]},None
    data=(row['result'] or {}).get('data') if row['status']=='succeeded' else None
    if not isinstance(data,dict) or data.get('on_whatsapp') is not True:return None,None
    if isinstance(data.get('chat_id'),str):
        if p is not None and 'read' not in p['permissions']:return None,None
        return {'tool':'preview_message','arguments':{'account':row['account'],'chat_id':data['chat_id']},
            'caller_supplies':['text']},None
    if c is None or p is None:return None,None
    phone=row['payload']['parameters']['phone'];parameters={'phone':'+'+phone,'create':True}
    try:
        with c.transaction():refusal=_open_chat_refusal(c,p,row['account'],phone,parameters)
    except Exception:return None,None  # The suggestion never makes a receipt unreadable.
    if refusal:return None,refusal
    # A new stable request ID for this separately confirmed intent, never a replacement of an uncertain one.
    return {'tool':'execute_messenger_operation','arguments':{'account':row['account'],'operation':'open_chat_by_phone',
        'parameters':parameters},'caller_supplies':['request_id'],'owner_confirmation_required':True},None


def _check_saved(store,c,p,row,*,ready=True,check_versions=True,verify_file=True,check_dispatch=True):
    from .commands import _account,_chat,_resolve,_required_permissions
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
        validate_parameters=False,retained_upload=True,saved=payload)
    # A catalog release must not rewrite reviewed, persisted normalized intent.
    current['catalog_version']=payload['catalog_version']
    saved={key:value for key,value in payload.items() if key!='request_input'}
    if current!=saved or spec['permission']!=row['permission'] or spec['mutation']!=row['mutation']:
        raise APIError('operation_target_changed',409)
    return current
