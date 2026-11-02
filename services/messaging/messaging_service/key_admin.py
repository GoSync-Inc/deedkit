"""Key administration of the messaging service: issue, restrict, revoke and extend credentials and set a
principal's access. Store keeps a method of each name and delegates here. Names the store module defines
(audit, serial, key helpers, errors) are read through it at call time, so tests that patch them there still
see the patched value."""
from datetime import datetime, timedelta, timezone
import secrets

from . import store as _store


def issue_key(self, principal, accounts, days=7, *, chats=_store.UNSET, permissions=_store.UNSET,
              key_permissions=_store.UNSET, token=None, operation_id=None, check=False, new_principal=False,
              key_permissions_of=None):
    """A new key; returns its token. With the caller's token and an operation ID (ANYM-495) it stores
        only the digest and returns public fields, and the same request again returns the same record.
        `new_principal` (ANYM-496) refuses an existing principal under its lock instead of rewriting its scope;
        `key_permissions_of` (ANYM-496) gives the new key that key's stored ceiling as is, a NULL legacy one too."""
    _store.principal_id(principal)
    if key_permissions_of is not None:
        key_permissions_of=_store.key_id(key_permissions_of)
        if key_permissions is not _store.UNSET:raise ValueError('key_permissions_conflict')
    # Up to one year (owner 2026-10-24, ANYM-499); the default stays 7 days.
    if type(days) is not int or not 1<=days<=365:
        raise ValueError('invalid_key_lifetime')
    once=operation_id is not None
    if once!=(token is not None):raise ValueError('token_and_operation_id_required')
    if once:
        token,operation_id=_store.caller_token(token),_store.operation_name(operation_id)
        request=_store.issue_request(principal,accounts,days,chats,permissions,key_permissions,key_permissions_of)
    else:token = 'pos_probe_'+secrets.token_urlsafe(32)
    try:
        issued,replayed=self._issue_key(principal,accounts,days,chats,permissions,key_permissions,token,
                                        (operation_id,request) if once else None,check,new_principal,
                                        key_permissions_of)
    except _store.OperationTaken:
        # A concurrent call won the operation ID or the token (either commit is visible now).
        with self.pool.connection() as c:issued=_store.key_operation(c,operation_id)
        if issued is None:raise ValueError('token_in_use') from None
        replayed=True
    if check and issued is None:return {'check':'ok'}
    if not once:return token
    if replayed and (issued['operation_request']!=request or issued['digest'] is None or
                     not secrets.compare_digest(issued['digest'],_store.digest(token))):
        raise ValueError('operation_conflict')
    return _store.serial({'key_id':issued['id'],'principal':issued['principal'],'expires_at':issued['expires_at'],
                   'revoked':issued['revoked'],'operation_id':operation_id,'replayed':replayed})


def _issue_key(self,principal,accounts,days,chats,permissions,key_permissions,token,operation,check=False,
               new_principal=False,key_of=None):
    with self.pool.connection() as c:
        if operation:_store.require_schema(c,_store.KEY_OPERATION_SCHEMA)
        _store.lock_principal(c,principal)
        if operation and (prior:=_store.key_operation(c,operation[0])) is not None:return prior,True
        old = c.execute('SELECT * FROM principals WHERE id=%s FOR UPDATE', (principal,)).fetchone()
        if new_principal and old:raise ValueError('principal_exists')
        if chats is _store.UNSET:chats=old['chat_ids'] if old else None
        if permissions is _store.UNSET:permissions=old['permissions'] if old else _store.DEFAULT_PERMISSIONS
        accounts,chats,permissions=_store.normalize_policy(c,accounts,chats,permissions)
        if key_of is not None:
            # A clone repeats the source key's stored ceiling, never wider: effective access is still the
            # principal's permissions narrowed by it, exactly as for the source.
            like=c.execute('SELECT permissions FROM api_keys WHERE id=%s',(key_of,)).fetchone()
            if like is None:raise ValueError('unknown_key')
            key_permissions=like['permissions']
        else:
            implicit_ceiling=key_permissions is _store.UNSET
            if implicit_ceiling:key_permissions=sorted(_store.LEGACY_PERMISSIONS|set(permissions))
            key_permissions=_store.normalize_list(key_permissions,maximum=len(_store.PERMISSIONS))
            if set(key_permissions)-(set(permissions)|(_store.LEGACY_PERMISSIONS if implicit_ceiling else set())):
                raise ValueError('invalid_key_permissions')
        if check:
            # ANYM-495 check mode: the same validation as an issue, then nothing is written.
            if operation and c.execute('SELECT 1 FROM api_keys WHERE digest=%s',(_store.digest(token),)).fetchone():
                raise ValueError('token_in_use')
            return None,False
        self._write_policy(c,principal,old,accounts,chats,permissions,old['enabled'] if old else True)
        operation_id,request=operation or (None,None)
        # A taken operation ID or token is not an error statement, so no key digest reaches the database log.
        issued=c.execute('INSERT INTO api_keys(digest,principal,expires_at,permissions) VALUES(%s,%s,%s,%s)'+
            (' ON CONFLICT DO NOTHING' if operation else '')+' RETURNING id,principal,expires_at,revoked',
            (_store.digest(token),principal,datetime.now(timezone.utc)+timedelta(days=days),key_permissions)).fetchone()
        if issued is None or operation and not c.execute('''INSERT INTO api_key_operations(operation_id,
                    operation_request,key_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING key_id''',
                (operation_id,request,issued['id'])).fetchone():
            raise _store.OperationTaken
        _store.audit(c,{'id':principal,'key_id':issued['id']},'issue_key','ok')
        return issued,False


def set_key_permissions(self,credential_id,permissions):
    credential_id=_store.key_id(credential_id)
    permissions=_store.normalize_list(permissions,maximum=len(_store.PERMISSIONS))
    if set(permissions)-_store.PERMISSIONS:raise ValueError('invalid_scope')
    with self.pool.connection() as c:
        known=c.execute('SELECT principal FROM api_keys WHERE id=%s',(credential_id,)).fetchone()
        if not known:raise ValueError('unknown_key')
        _store.lock_principal(c,known['principal'])
        principal=c.execute('SELECT permissions FROM principals WHERE id=%s',
                            (known['principal'],)).fetchone()
        if set(permissions)-set(principal['permissions']):raise ValueError('invalid_key_permissions')
        row=c.execute('''UPDATE api_keys SET permissions=%s,
                permission_version=permission_version+1 WHERE id=%s
                RETURNING id,principal,permissions,permission_version''',
            (permissions,credential_id)).fetchone()
        if not {'read','subscription-write'}<=set(permissions):
            # ANYM-522: restoring the permission later does not revive this key's dynamic definitions.
            from .subscriptions import invalidate_dynamic
            invalidate_dynamic(c,known['principal'],credential_id)
        _store.audit(c,{'id':known['principal'],'key_id':credential_id},'set_key_permissions','ok')
        return _store.serial(row)


def _write_policy(c,principal,old,accounts,chats,permissions,enabled):
    if old:
        changed=(set(old['accounts'])!=set(accounts) or old['chat_ids']!=chats or
                 set(old['permissions'])!=set(permissions) or old['enabled']!=enabled)
        if changed:
            c.execute('''UPDATE principals SET accounts=%s,chat_ids=%s,permissions=%s,enabled=%s,
                    scope_version=scope_version+1 WHERE id=%s''',(accounts,chats,permissions,enabled,principal))
            # ANYM-522: references to chats leaving the scope stay hidden, also after a re-add; a lost
            # read or subscription-write ends the dynamic definitions for good.
            from .subscriptions import hide_out_of_scope,invalidate_dynamic
            hide_out_of_scope(c,principal,accounts,chats)
            if not enabled or not {'read','subscription-write'}<=set(permissions):invalidate_dynamic(c,principal)
    else:
        c.execute('INSERT INTO principals(id,accounts,chat_ids,permissions,enabled) VALUES(%s,%s,%s,%s,%s)',
                  (principal,accounts,chats,permissions,enabled))


def set_access(self,principal,*,accounts=_store.UNSET,chats=_store.UNSET,permissions=_store.UNSET,enabled=_store.UNSET):
    _store.principal_id(principal)
    with self.pool.connection() as c:
        _store.lock_principal(c,principal)
        old=c.execute('SELECT * FROM principals WHERE id=%s FOR UPDATE',(principal,)).fetchone()
        if not old:raise ValueError('unknown_principal')
        if accounts is _store.UNSET:accounts=old['accounts']
        if chats is _store.UNSET:chats=old['chat_ids']
        if permissions is _store.UNSET:permissions=old['permissions']
        if enabled is _store.UNSET:enabled=old['enabled']
        if type(enabled) is not bool:raise ValueError('invalid_enabled')
        accounts,chats,permissions=_store.normalize_policy(c,accounts,chats,permissions)
        self._write_policy(c,principal,old,accounts,chats,permissions,enabled)
        _store.audit(c,{'id':principal},'set_access','ok')
        return _store.serial(c.execute('SELECT * FROM principals WHERE id=%s',(principal,)).fetchone())


def revoke(self, token=None, *, credential_id=None):
    if (token is None)==(credential_id is None):raise ValueError('one_key_selector_required')
    if token is not None:
        clause,selector='digest=%s',_store.digest(_store.revocable_token(token))
    else:clause,selector='id=%s',_store.key_id(credential_id)
    with self.pool.connection() as c:
        row=c.execute('SELECT principal,id FROM api_keys WHERE '+clause,(selector,)).fetchone()
        if not row:return 0
        _store.lock_principal(c,row['principal'])
        changed=c.execute('UPDATE api_keys SET revoked=true WHERE '+clause,(selector,)).rowcount
        _store.audit(c,{'id':row['principal'],'key_id':row['id']},'revoke_key','ok')
        return changed


def extend_key(self,credential_id,days):
    # Renewal in place (ANYM-422): token, digest, permissions and scope stay unchanged.
    credential_id=_store.key_id(credential_id)
    if type(days) is not int or not 1<=days<=365:raise ValueError('invalid_key_lifetime')
    with self.pool.connection() as c:
        known=c.execute('SELECT principal FROM api_keys WHERE id=%s',(credential_id,)).fetchone()
        if not known:raise ValueError('unknown_key')
        _store.lock_principal(c,known['principal'])
        # The clock is read after the lock, as authentication does.
        row=c.execute('''SELECT id,principal,revoked,expires_at,clock_timestamp() AS now,
                clock_timestamp()+make_interval(days=>%s) AS extended FROM api_keys WHERE id=%s FOR UPDATE''',
            (days,credential_id)).fetchone()
        if row['revoked']:raise ValueError('key_revoked')
        if row['expires_at']<=row['now']:raise ValueError('key_expired')
        if row['extended']<row['expires_at']:raise ValueError('key_lifetime_would_shorten')
        c.execute('UPDATE api_keys SET expires_at=%s WHERE id=%s',(row['extended'],credential_id))
        _store.audit(c,{'id':row['principal'],'key_id':row['id']},'extend_key','ok')
        return _store.serial({'key_id':row['id'],'previous_expires_at':row['expires_at'],'expires_at':row['extended']})
