"""Owner-read snapshots and credential-local dismissals, no provider actions."""
from datetime import datetime, timezone

from .envelope import stable_id, utc
from .policy import scope_filter, targets


# Passive WhatsApp read sources (ANYM-275). Each keeps its own basis: the owner's app-state
# mark-read is a whole-chat baseline (0 or a marked-unread flag), a history-sync count is a
# provider count at its snapshot time and an own-device receipt is per-message evidence only.
WHATSAPP_READ_BASES=('whatsapp_app_state_read','whatsapp_owner_receipt','whatsapp_history_sync')
BASELINE_BASES=('telegram_dialog','whatsapp_cached_chat','whatsapp_history_sync','whatsapp_app_state_read')


def validate_observation(value):
    if not isinstance(value, dict) or set(value)-{'observed_at','unread_count','read_inbox_max_id','basis','is_self_chat',
            'marked_unread','read_message_ids'}:
        raise ValueError('invalid_read_observation')
    basis=value.get('basis')
    # Native discovery is a local WhatsApp cache observation too. Preserve the
    # existing stored basis/schema; it does not imply a known unread count.
    if basis=='whatsmeow_captured_chat':basis='whatsapp_cached_chat'
    if basis not in ('telegram_dialog','whatsapp_cached_chat',*WHATSAPP_READ_BASES):
        raise ValueError('invalid_read_observation')
    at=utc(value.get('observed_at'))
    if datetime.fromisoformat(at)>datetime.now(timezone.utc):
        raise ValueError('invalid_read_observation')
    for key, maximum in [('unread_count',2**31-1),('read_inbox_max_id',2**63-1)]:
        number=value.get(key)
        if number is not None and (type(number) is not int or not 0<=number<=maximum):
            raise ValueError('invalid_read_observation')
    for key in ('is_self_chat','marked_unread'):
        if value.get(key) is not None and type(value[key]) is not bool:
            raise ValueError('invalid_read_observation')
    if basis!='telegram_dialog' and value.get('read_inbox_max_id') is not None:
        raise ValueError('invalid_read_observation')
    if value.get('marked_unread') is not None and basis not in ('whatsapp_app_state_read','whatsapp_history_sync'):
        raise ValueError('invalid_read_observation')
    ids=value.get('read_message_ids')
    if (basis=='whatsapp_owner_receipt')!=(ids is not None):raise ValueError('invalid_read_observation')
    if ids is not None and (not isinstance(ids,list) or not 0<=len(ids)<=1000 or value.get('unread_count') is not None
            or any(not isinstance(i,str) or not 0<len(i)<=256 or '\x00' in i for i in ids)):
        raise ValueError('invalid_read_observation')
    # A mark-read is exactly 0 and not marked unread; a mark-unread is a flag, never a count.
    if basis=='whatsapp_app_state_read' and (value.get('unread_count'),value.get('marked_unread')) not in ((0,False),(None,True)):
        raise ValueError('invalid_read_observation')
    if basis=='whatsapp_history_sync' and value.get('unread_count') is None and value.get('marked_unread') is not True:
        raise ValueError('invalid_read_observation')
    return {**value,'observed_at':at,'basis':basis,**({'read_message_ids':sorted(set(ids))} if ids is not None else {})}


def telegram_read_capture(account,update,*,verified_self,expected_user_id):
    """Observe inbox read boundaries from the verified existing client, no RPC."""
    import re
    from telethon import types,utils
    if (not isinstance(account,str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',account)
        or type(expected_user_id) is not int or not 0<expected_user_id<2**63
        or not isinstance(verified_self,types.User) or verified_self.is_self is not True
        or verified_self.id!=expected_user_id):return None
    if isinstance(update,types.UpdateReadHistoryInbox):peer=update.peer
    elif isinstance(update,types.UpdateReadChannelInbox):peer=types.PeerChannel(update.channel_id)
    else:return None
    if not isinstance(peer,(types.PeerUser,types.PeerChat,types.PeerChannel)):return None
    peer_id=getattr(peer,'user_id',getattr(peer,'chat_id',getattr(peer,'channel_id',None)))
    if type(peer_id) is not int or not 0<peer_id<2**63:return None
    try:
        observed=validate_observation({'observed_at':datetime.now(timezone.utc).isoformat(),
            'read_inbox_max_id':update.max_id,'unread_count':update.still_unread_count,
            'basis':'telegram_dialog','is_self_chat':isinstance(peer,types.PeerUser) and peer_id==expected_user_id})
    except (ValueError,AttributeError):return None
    return {'kind':'owner_read_state','account':account,'chat':str(utils.get_peer_id(peer)),
        'observed_at':observed['observed_at'],'observation':observed}


# A row with no read evidence at all (chat discovery stamps its cache time) never
# outranks an older WhatsApp baseline or flag; a receipt row ('partial') does.
EMPTY_ROW='''(chat_read_observations.unread_count IS NULL AND chat_read_observations.marked_unread IS NULL
    AND chat_read_observations.read_inbox_max_id IS NULL AND chat_read_observations.count_basis IS NULL)'''


def observe(c,account,provider_chat,value):
    value=validate_observation(value)
    platform=c.execute('SELECT platform FROM accounts WHERE id=%s',(account,)).fetchone()
    if not platform or value['basis'].split('_',1)[0]!=platform['platform']:
        raise ValueError('invalid_read_observation')
    chat=stable_id('chat',account,provider_chat)
    args=(chat,account,value['observed_at'],value.get('unread_count'),value.get('read_inbox_max_id'),
        value['basis'],value.get('is_self_chat'),value.get('marked_unread'))
    insert='''INSERT INTO chat_read_observations(chat,account,observed_at,unread_count,read_inbox_max_id,basis,
        is_self_chat,marked_unread,baseline_at,count_basis) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'''
    if value['basis']=='whatsapp_owner_receipt':
        # Evidence for exactly the listed messages; the first read time is kept on replay.
        for provider_id in value['read_message_ids']:
            c.execute('''INSERT INTO owner_message_reads(chat,account,provider_id,read_at,basis) VALUES(%s,%s,%s,%s,%s)
                ON CONFLICT(chat,provider_id) DO UPDATE SET read_at=least(owner_message_reads.read_at,excluded.read_at)''',
                (chat,account,provider_id,value['observed_at'],value['basis']))
        # A receipt never sets or adjusts a count: one newer than the chat's baseline only
        # invalidates it. Receipts in any order give the same row.
        c.execute(insert+'''NULL,'partial') ON CONFLICT(chat) DO UPDATE SET
            unread_count=CASE WHEN excluded.observed_at>coalesce(chat_read_observations.baseline_at,chat_read_observations.observed_at)
                THEN NULL ELSE chat_read_observations.unread_count END,
            count_basis=CASE WHEN chat_read_observations.unread_count IS NOT NULL
                AND excluded.observed_at>coalesce(chat_read_observations.baseline_at,chat_read_observations.observed_at)
                THEN 'stale_since_baseline' WHEN chat_read_observations.unread_count IS NULL
                THEN coalesce(chat_read_observations.count_basis,'partial') ELSE chat_read_observations.count_basis END,
            marked_unread=CASE WHEN excluded.observed_at>chat_read_observations.observed_at THEN NULL
                ELSE chat_read_observations.marked_unread END,
            basis=CASE WHEN excluded.observed_at>chat_read_observations.observed_at THEN excluded.basis
                ELSE chat_read_observations.basis END,
            observed_at=greatest(chat_read_observations.observed_at,excluded.observed_at)''',args)
    elif value['basis'] in WHATSAPP_READ_BASES and value.get('marked_unread') and value.get('unread_count') is None:
        # A flag, not a count: a newer mark-unread clears the count it supersedes.
        c.execute(insert+'''NULL,'partial') ON CONFLICT(chat) DO UPDATE SET
            observed_at=excluded.observed_at,unread_count=NULL,read_inbox_max_id=NULL,basis=excluded.basis,marked_unread=true,
            count_basis=CASE WHEN chat_read_observations.unread_count IS NOT NULL
                OR chat_read_observations.count_basis='stale_since_baseline' THEN 'stale_since_baseline' ELSE 'partial' END
            WHERE excluded.observed_at>chat_read_observations.observed_at OR '''+EMPTY_ROW,args)
    elif value['basis'] in WHATSAPP_READ_BASES:
        # A whole-chat baseline: a mark-read (0) or a history-sync count at its snapshot time.
        c.execute(insert+'''%s,'provider') ON CONFLICT(chat) DO UPDATE SET
            observed_at=excluded.observed_at,unread_count=excluded.unread_count,read_inbox_max_id=NULL,
            basis=excluded.basis,marked_unread=excluded.marked_unread,baseline_at=excluded.baseline_at,count_basis='provider'
            WHERE excluded.observed_at>chat_read_observations.observed_at OR '''+EMPTY_ROW,(*args,value['observed_at']))
    else:
        # One timestamp covers both read fields. An incomplete observation must not
        # discard known evidence or make retained numbers look newly verified.
        known=value.get('unread_count') is not None
        c.execute(insert+'''%s,%s) ON CONFLICT(chat) DO UPDATE SET
            observed_at=excluded.observed_at,unread_count=excluded.unread_count,
            read_inbox_max_id=excluded.read_inbox_max_id,basis=excluded.basis,
            is_self_chat=coalesce(excluded.is_self_chat,chat_read_observations.is_self_chat),
            marked_unread=NULL,baseline_at=excluded.baseline_at,count_basis=excluded.count_basis
            WHERE excluded.observed_at>chat_read_observations.observed_at
              AND (excluded.unread_count IS NOT NULL OR excluded.read_inbox_max_id IS NOT NULL)
              AND (chat_read_observations.unread_count IS NULL OR excluded.unread_count IS NOT NULL)
              AND (chat_read_observations.read_inbox_max_id IS NULL OR excluded.read_inbox_max_id IS NOT NULL)''',
            (*args,value['observed_at'] if known else None,'provider' if known else None))
    if value.get('is_self_chat') is not None:
        # Identity evidence is independent of read evidence. Never refresh the
        # count timestamp when discovery only proves whether this is self-chat.
        c.execute('''UPDATE chat_read_observations SET is_self_chat=%s
            WHERE chat=%s AND account=%s AND observed_at<=%s''',
            (value['is_self_chat'],chat,account,value['observed_at']))


def dismiss(store,c,p,chat_id,message_id,expected_revision,reason=None,note=None):
    from .chat_queries import list_chats
    from .search_query import valid_text
    from .store import APIError
    valid_text(chat_id,empty=False);valid_text(message_id,empty=False)
    if type(expected_revision) is not int or expected_revision<1:
        raise APIError('invalid_version')
    reason=_reason(reason);note=_note(note)
    if 'read' not in p['permissions']:raise APIError('forbidden',403)
    scope,args=scope_filter(p,'c',chat_column='id')
    # Share the archive's account lock: ingest cannot change the target between
    # the compare and write. Separate credentials cannot dismiss one another.
    chat=c.execute('SELECT c.account FROM chats c WHERE c.id=%s AND '+scope,[chat_id,*args]).fetchone()
    if not chat:raise APIError('not_found',404)
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:'+chat['account'],))
    result=list_chats(store,c,p,include_coverage=False,awaiting_reply_days=0,selected_chat=chat_id,
        include_dismissed=True,view='conversation')
    row=result['items'][0]
    if (not row['awaiting_reply'] or row.get('awaiting_reply_message_id')!=message_id or
        row.get('awaiting_reply_revision')!=expected_revision):
        raise APIError('reply_target_changed',409)
    c.execute('''INSERT INTO reply_dismissals(key_id,chat,message,revision,incoming_head,reason,note)
        VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(key_id,chat) DO UPDATE SET
        message=excluded.message,revision=excluded.revision,incoming_head=excluded.incoming_head,
        reason=excluded.reason,note=excluded.note,dismissed_at=clock_timestamp()''',
        (p['key_id'],chat_id,message_id,expected_revision,row['_incoming_head'],reason,note))
    # A dismissal replaces this key's snooze of the chat (ANYM-281).
    c.execute('DELETE FROM reply_snoozes WHERE key_id=%s AND chat=%s',(p['key_id'],chat_id))
    targets(p,[chat_id])
    return {'dismissed':True,'chat_id':chat_id,'message_id':message_id,'revision':expected_revision,
        'scope':'this_credential','until':'next_meaningful_incoming_or_attention_revision',
        'provider_read_changed':False,**({'reason':reason} if reason else {}),**({'note':note} if note else {}),
        **({'suggest':'mute_reply_source'} if reason=='spam_or_mailing' else {})}


# ANYM-281: reasons, snoozes, undo and mutes. Archive-local; never a provider action.
REASONS=('no_reply_needed','spam_or_mailing','handled_elsewhere','other')
MUTE_LIMIT=1000


def _reason(reason):
    from .store import APIError
    if reason is not None and reason not in REASONS:raise APIError('invalid_filter')
    return reason


def _note(note):
    from .store import APIError
    # Untrusted text, stored and returned as is; never interpreted.
    if note is not None and (not isinstance(note,str) or not 1<=len(note)<=200 or
            any(ord(ch)<32 or ord(ch)==127 for ch in note)):
        raise APIError('invalid_filter')
    return note


def _queue_chat(c,p,chat_id):
    """The scoped chat's account, under the archive account lock that ingest shares."""
    from .search_query import valid_text
    from .store import APIError
    valid_text(chat_id,empty=False)
    if 'read' not in p['permissions']:raise APIError('forbidden',403)
    scope,args=scope_filter(p,'c',chat_column='id')
    chat=c.execute('SELECT c.account FROM chats c WHERE c.id=%s AND '+scope,[chat_id,*args]).fetchone()
    if not chat:raise APIError('not_found',404)
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:'+chat['account'],))
    return chat['account']


def _target(store,c,p,chat_id):
    """The chat's current hint as the key's queue chooses it, the principal's sender mutes applied."""
    from .chat_queries import list_chats
    return list_chats(store,c,p,include_coverage=False,awaiting_reply_days=0,selected_chat=chat_id,
        include_dismissed=True,view='conversation')['items'][0]


def _current(row,entry):
    return bool(row['awaiting_reply'] and row.get('awaiting_reply_message_id')==entry['message'] and
        row.get('awaiting_reply_revision')==entry['revision'] and row.get('_incoming_head')==entry['incoming_head'])


def _checked_target(store,c,p,chat_id,message_id,expected_revision):
    from .search_query import valid_text
    from .store import APIError
    valid_text(message_id,empty=False)
    if type(expected_revision) is not int or expected_revision<1:raise APIError('invalid_version')
    _queue_chat(c,p,chat_id)
    row=_target(store,c,p,chat_id)
    if (not row['awaiting_reply'] or row.get('awaiting_reply_message_id')!=message_id or
        row.get('awaiting_reply_revision')!=expected_revision):
        raise APIError('reply_target_changed',409)
    return row


def snooze(store,c,p,chat_id,message_id,expected_revision,until,note=None):
    from .store import APIError,date_bound
    note=_note(note)
    try:at=date_bound(until) if until else None
    except APIError:at=None
    if at is None:raise APIError('invalid_date_range')
    if not c.execute("SELECT %s>clock_timestamp() AND %s<=clock_timestamp()+interval '90 days' AS ok",(at,at)).fetchone()['ok']:
        raise APIError('invalid_date_range')
    row=_checked_target(store,c,p,chat_id,message_id,expected_revision)
    c.execute('''INSERT INTO reply_snoozes(key_id,chat,message,revision,incoming_head,snoozed_until,note)
        VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(key_id,chat) DO UPDATE SET message=excluded.message,
        revision=excluded.revision,incoming_head=excluded.incoming_head,snoozed_until=excluded.snoozed_until,
        note=excluded.note,snoozed_at=clock_timestamp()''',
        (p['key_id'],chat_id,message_id,expected_revision,row['_incoming_head'],at,note))
    # A snooze replaces this key's dismissal of the chat.
    c.execute('DELETE FROM reply_dismissals WHERE key_id=%s AND chat=%s',(p['key_id'],chat_id))
    targets(p,[chat_id])
    return {'snoozed':True,'chat_id':chat_id,'message_id':message_id,'revision':expected_revision,
        'snoozed_until':at.isoformat().replace('+00:00','Z'),'scope':'this_credential',
        'returns':'at_snoozed_until_or_next_meaningful_incoming','provider_read_changed':False}


def _entry(kind,entry,active):
    return {'kind':kind,'message_id':entry['message'],'revision':entry['revision'],'reason':entry.get('reason'),
        'note':entry.get('note'),'snoozed_until':entry['snoozed_until'].astimezone(timezone.utc).isoformat().replace('+00:00','Z')
            if entry.get('snoozed_until') else None,
        'active':active}


def restore(store,c,p,chat_id,message_id,expected_revision):
    """Declarative undo: this key no longer hides this hint. An entry for another hint is never removed."""
    from .search_query import valid_text
    from .store import APIError
    valid_text(message_id,empty=False)
    if type(expected_revision) is not int or expected_revision<1:raise APIError('invalid_version')
    _queue_chat(c,p,chat_id)
    row=_target(store,c,p,chat_id)
    removed=None
    for kind,table in (('dismissed','reply_dismissals'),('snoozed','reply_snoozes')):
        # Table names are constants.
        entry=c.execute(f'DELETE FROM {table} WHERE key_id=%s AND chat=%s AND message=%s AND revision=%s RETURNING *',
            (p['key_id'],chat_id,message_id,expected_revision)).fetchone()
        if entry and removed is None:
            active=_current(row,entry) and (kind=='dismissed' or entry['snoozed_until']>c.execute(
                'SELECT CURRENT_TIMESTAMP AS t').fetchone()['t'])
            removed=_entry(kind,entry,active)
    targets(p,[chat_id])
    return {'restored':removed is not None,'chat_id':chat_id,**({'removed':removed} if removed else {}),
        'scope':'this_credential','provider_read_changed':False}


def _mute_target(c,p,chat_id,account,sender_id):
    """(account, chat, sender) inside the principal's current scope, else not_found."""
    from .search_query import valid_text
    from .store import APIError
    if 'read' not in p['permissions']:raise APIError('forbidden',403)
    if chat_id is not None:
        if account is not None or sender_id is not None:raise APIError('invalid_filter')
        valid_text(chat_id,empty=False)
        scope,args=scope_filter(p,'c',chat_column='id')
        chat=c.execute('SELECT c.account FROM chats c WHERE c.id=%s AND '+scope,[chat_id,*args]).fetchone()
        if not chat:raise APIError('not_found',404)
        return chat['account'],chat_id,None
    if account is None or sender_id is None:raise APIError('invalid_filter')
    valid_text(account,empty=False);valid_text(sender_id,512,empty=False)
    scope,args=scope_filter(p,'m')
    # A sender exists for this key only through a message in a chat it can read.
    if account not in p['accounts'] or not c.execute('SELECT 1 FROM messages m WHERE m.account=%s AND m.sender=%s AND '+scope+' LIMIT 1',
            [account,sender_id,*args]).fetchone():
        raise APIError('not_found',404)
    return account,None,sender_id


def _mute_where(chat_id):
    return 'principal=%s AND chat=%s' if chat_id is not None else 'principal=%s AND account=%s AND sender=%s'


def _mute_args(p,account,chat_id,sender_id):
    return (p['id'],chat_id) if chat_id is not None else (p['id'],account,sender_id)


def _mute_view(account,chat_id,sender_id):
    return {'target':'chat','chat_id':chat_id} if chat_id is not None else {'target':'sender','account':account,'sender_id':sender_id}


def mute(store,c,p,chat_id=None,account=None,sender_id=None,reason=None,note=None):
    from .store import APIError
    reason=_reason(reason);note=_note(note)
    account,chat_id,sender_id=_mute_target(c,p,chat_id,account,sender_id)
    # One writer per principal counts and inserts; distinct from the shared authorization lock.
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('reply-mutes:'+p['id'],))
    where=_mute_where(chat_id);args=_mute_args(p,account,chat_id,sender_id)
    updated=c.execute('UPDATE reply_mutes SET reason=%s,note=%s,updated_at=clock_timestamp() WHERE '+where+' RETURNING id',
        (reason,note,*args)).fetchone()
    if not updated:
        if c.execute('SELECT count(*) AS n FROM reply_mutes WHERE principal=%s',(p['id'],)).fetchone()['n']>=MUTE_LIMIT:
            raise APIError('mute_limit_reached',409)
        c.execute('INSERT INTO reply_mutes(principal,account,chat,sender,reason,note) VALUES(%s,%s,%s,%s,%s,%s)',
            (p['id'],account,chat_id,sender_id,reason,note))
    if chat_id:targets(p,[chat_id])
    return {'muted':True,**_mute_view(account,chat_id,sender_id),'created':not updated,'reason':reason,'note':note,
        'scope':'this_principal','until':'unmute_reply_source','provider_changed':False}


def unmute(store,c,p,chat_id=None,account=None,sender_id=None):
    account,chat_id,sender_id=_mute_target(c,p,chat_id,account,sender_id)
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('reply-mutes:'+p['id'],))
    gone=c.execute('DELETE FROM reply_mutes WHERE '+_mute_where(chat_id)+' RETURNING id',
        _mute_args(p,account,chat_id,sender_id)).fetchone()
    if chat_id:targets(p,[chat_id])
    return {'unmuted':gone is not None,**_mute_view(account,chat_id,sender_id),'scope':'this_principal',
        'provider_changed':False}


def list_entries(store,c,p,kind='dismissed',after='',limit=20):
    """The key's dismissals or snoozes, or the principal's mutes, inside the current scope only."""
    from .search_query import cursor_binding,decode_cursor,encode_cursor,position_text
    from .store import APIError,bounded,serial
    from .sender_names import resolve
    from .chat_titles import labels
    if kind not in ('dismissed','snoozed','muted'):raise APIError('invalid_filter')
    bounded(limit)
    if 'read' not in p['permissions']:raise APIError('forbidden',403)
    binding=cursor_binding(p,'reply-entries',{'kind':kind,'key':str(p['key_id'])})
    pos=decode_cursor(after,binding) if after else None
    if pos is not None and set(pos)!={'at','id'}:raise APIError('invalid_cursor')
    if kind=='muted':
        cscope,cargs=scope_filter(p,'c',chat_column='id');mscope,margs=scope_filter(p,'m')
        source=f'''SELECT e.id::text AS id,e.created_at AS at,e.account,e.chat,e.sender,e.reason,e.note,
            NULL::text AS message,NULL::integer AS revision,NULL::text AS incoming_head,NULL::timestamptz AS snoozed_until
            FROM reply_mutes e WHERE e.principal=%s AND (e.chat IS NOT NULL AND EXISTS(SELECT 1 FROM chats c
              WHERE c.id=e.chat AND {cscope}) OR e.sender IS NOT NULL AND EXISTS(SELECT 1 FROM messages m
              WHERE m.account=e.account AND m.sender=e.sender AND {mscope}))'''
        args=[p['id'],*cargs,*margs]
    else:
        table,at=('reply_dismissals','dismissed_at') if kind=='dismissed' else ('reply_snoozes','snoozed_at')
        cscope,cargs=scope_filter(p,'c',chat_column='id')
        extra=('e.reason,e.note,NULL::timestamptz AS snoozed_until' if kind=='dismissed' else
            'NULL::text AS reason,e.note,e.snoozed_until')
        source=f'''SELECT e.chat AS id,e.{at} AS at,c.account,e.chat,m.sender,{extra},e.message,e.revision,e.incoming_head
            FROM {table} e JOIN chats c ON c.id=e.chat JOIN messages m ON m.id=e.message
            WHERE e.key_id=%s AND {cscope}'''
        args=[p['key_id'],*cargs]
    total=c.execute('SELECT count(*) AS n FROM ('+source+') s',args).fetchone()['n']
    page='SELECT * FROM ('+source+') s'
    if pos:
        page+=' WHERE (s.at,s.id)>(%s::timestamptz,%s)';args=[*args,pos['at'],position_text(pos['id'])]
    rows=c.execute(page+' ORDER BY s.at,s.id LIMIT %s',[*args,limit+1]).fetchall()
    items=rows[:limit]
    cursor=encode_cursor(binding,{'at':items[-1]['at'],'id':items[-1]['id']}) if len(rows)>limit else None
    now=c.execute('SELECT CURRENT_TIMESTAMP AS t').fetchone()['t']
    if kind=='muted':
        # A sender's name comes from a chat the scope can read.
        mscope,margs=scope_filter(p,'m')
        for row in items:
            if row['sender']:
                seen=c.execute('SELECT m.chat FROM messages m WHERE m.account=%s AND m.sender=%s AND '+mscope+
                    ' ORDER BY m.sent_at DESC LIMIT 1',[row['account'],row['sender'],*margs]).fetchone()
                row['_name_chat']=seen['chat'] if seen else None
    chats={r['id']:r for r in c.execute('SELECT id,account,title,chat_type FROM chats WHERE id=ANY(%s)',
        ([r['chat'] or r.get('_name_chat') for r in items if r['chat'] or r.get('_name_chat')],)).fetchall()}
    private=[(chats[r['chat']]['account'],r['chat']) for r in items if r['chat'] and chats[r['chat']]['chat_type']=='private']
    titles=labels(c,p,private) if private else {}
    for row in items:
        row['_resolve_chat']=row['chat'] or row.get('_name_chat');row['sender_name']=None
        if row['sender'] and row['_resolve_chat']:
            # The authorized chat's own profile name first; resolve() then replaces only raw IDs.
            named=c.execute('''SELECT sp.display_name FROM sender_profiles sp WHERE sp.account=%s AND sp.chat=%s AND sp.sender=%s
                AND EXISTS(SELECT 1 FROM messages source WHERE source.id=sp.source_message AND source.account=sp.account
                  AND source.chat=sp.chat AND source.sender=sp.sender AND NOT source.deleted)''',
                (row['account'],row['_resolve_chat'],row['sender'])).fetchone()
            row['sender_name']=named['display_name'] if named else None
    resolve(c,p,[r for r in items if r['sender'] and r['_resolve_chat']],chat='_resolve_chat',sender='sender')
    out=[]
    for row in items:
        item={'target':'sender' if kind=='muted' and row['sender'] and not row['chat'] else 'chat',
            'chat_id':row['chat'],'account':row['account'],
            'title':(titles.get((row['account'],row['chat'])) or (chats[row['chat']]['title'],))[0] if row['chat'] else None,
            'sender_id':row['sender'],'sender_name':row.get('sender_name'),'reason':row['reason'],'note':row['note'],
            'created_at':row['at']}
        if kind!='muted':
            hint=_target(store,c,p,row['chat'])
            item.update(message_id=row['message'],revision=row['revision'],
                snoozed_until=row['snoozed_until'],active=_current(hint,row) and (
                    kind=='dismissed' or row['snoozed_until']>now))
            item['snippet']=c.execute('''SELECT coalesce(nullif(left(body,160),''),'[attachment]') AS s FROM messages
                WHERE id=%s''',(row['message'],)).fetchone()['s']
        if kind=='muted' and not row['chat']:item['chat_id']=None
        out.append({k:v for k,v in item.items() if v is not None})
    targets(p,[r['chat'] for r in items if r['chat']])
    return serial({'kind':kind,'items':out,'total':total,'next_cursor':cursor,
        'scope':'this_principal' if kind=='muted' else 'this_credential'})
