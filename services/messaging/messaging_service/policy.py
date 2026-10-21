"""Shared scoped policy and content-free audit records for reviewed operations."""
import re
from uuid import UUID

DEFAULT_PERMISSIONS = ('cursor-write', 'download', 'read')
LEGACY_PERMISSIONS = frozenset((*DEFAULT_PERMISSIONS, 'review-write', 'send'))
MESSENGER_PERMISSIONS = frozenset(('read-live', 'contacts-read', 'profile-read', 'groups-read',
    'message-edit', 'message-delete', 'message-react', 'message-pin', 'read-state',
    'chat-manage', 'contacts-manage', 'groups-manage', 'profile-manage'))
SUBSCRIPTION_PERMISSIONS = frozenset(('subscription-write', 'subscription-ack', 'feedback-write'))
UPLOAD_PERMISSIONS = frozenset(('upload',))
PERMISSIONS = LEGACY_PERMISSIONS | MESSENGER_PERMISSIONS | SUBSCRIPTION_PERMISSIONS | UPLOAD_PERMISSIONS


def effective_permissions(principal_permissions, key_permissions):
    """NULL legacy ceilings can never inherit a newly introduced capability."""
    ceiling = LEGACY_PERMISSIONS if key_permissions is None else set(key_permissions)
    return sorted(set(principal_permissions) & ceiling)
OPERATION_PERMISSIONS = {
    'capabilities': None,
    'list_chats': 'read', 'search': 'read', 'get_messages': 'read',
    'get_context': 'read', 'get_updates': 'read',
    'get_attachment': 'download', 'download': 'download', 'request_file_download':'download',
    'ack_updates': 'cursor-write', 'dismiss_reply':'cursor-write',
    'get_reviews': 'read', 'record_review': 'review-write',
    'search_people': 'read', 'search_files': 'read', 'get_file_text': 'read',
    'get_file_occurrences':'read', 'get_file_segments':'read', 'get_sync_status':'read',
    'resolve_reference':'read',
    'preview_message':'read','send_message':'send','get_send_status':'send',
    'list_messenger_operations':None, 'preview_messenger_operation':None,
    'execute_messenger_operation':None, 'get_messenger_operation':None,
    'create_subscription':'subscription-write', 'list_subscriptions':'read',
    'get_subscription':'read', 'update_subscription':'subscription-write',
    'get_subscription_events':'read',
    'fetch_subscription_events':'read','ack_subscription_events':'subscription-ack',
    'wait_for_subscription_events':'read','configure_subscription_delivery':'subscription-write',
    'test_subscription':'subscription-write','submit_feedback':'feedback-write','list_feedback':'read',
    'prepare_file_upload':'upload','upload_file_chunk':'upload','get_file_upload':'upload',
}
AUDIT_OPERATIONS = frozenset(OPERATION_PERMISSIONS) | {
    'issue_key', 'revoke_key', 'set_access', 'http_boundary', 'send_dispatch', 'set_sending',
    'set_key_permissions', 'set_operations', 'messenger_dispatch', 'extend_key', 'set_feedback_status',
}
AUDIT_CODES = frozenset({
    'unauthorized', 'forbidden', 'not_found', 'invalid_limit', 'invalid_query',
    'invalid_utc_date', 'invalid_date_range', 'invalid_filter', 'invalid_consumer',
    'consumer_limit', 'invalid_version', 'invalid_receipt', 'stale_ack',
    'attachment_unavailable', 'invalid_host', 'invalid_origin', 'rate_limited',
    'invalid_body', 'body_too_large', 'body_timeout', 'disconnected',
    'invalid_parameters', 'temporarily_unavailable', 'operation_failed',
    'invalid_review', 'revision_not_found', 'review_conflict',
    'query_timeout','service_busy','service_unavailable','chat_required','ambiguous_reference',
    'invalid_message_text','invalid_request_id','idempotency_conflict','sending_disabled',
    'sender_not_ready','send_access_changed','send_expired','send_result_unknown',
    'invalid_message_format','unsafe_message_markup','invalid_message_entities',
    'formatting_too_complex','unsupported_message_formatting',
    'lossy_formatting_requires_confirmation','message_too_long','preview_required','preview_mismatch',
    'unsupported_operation','invalid_operation_parameters','operation_disabled','operation_expired',
    'operation_access_changed','operation_result_unknown','operation_target_changed','operation_rejected',
    'reply_target_changed','invalid_subscription','subscription_limit','subscription_conflict','subscription_closed',
    'subscription_access_changed','invalid_subscription_state',
    'invalid_upload','invalid_upload_chunk','upload_limit','upload_capacity','upload_expired',
    'upload_offset_conflict','upload_hash_mismatch',
    'subscription_wait_timeout','consumer_already_initialized','media_request_limit','subscription_receipt_limit','invalid_feedback','feedback_limit',
    'invalid_receiver','delivery_not_configured','invalid_phone','chat_not_collected',
    'identity_unresolved','provider_timeout',
})
UNSET = object()


def principal_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}', value):
        raise ValueError('invalid_principal')
    return value


def normalize_list(value, *, maximum=1000):
    if (not isinstance(value, (list, tuple)) or len(value) > maximum or
        any(not isinstance(v, str) or not v or len(v) > 256 or
            any(ord(c) < 32 or ord(c) == 127 for c in v) for v in value)):
        raise ValueError('invalid_scope')
    return sorted(set(value))


def normalize_policy(c, accounts, chats, permissions):
    accounts = normalize_list(accounts, maximum=100)
    chats = None if chats is None else normalize_list(chats)
    permissions = normalize_list(permissions, maximum=len(PERMISSIONS))
    if not accounts or set(permissions) - PERMISSIONS:
        raise ValueError('invalid_scope')
    known = {r['id'] for r in c.execute('SELECT id FROM accounts WHERE id=ANY(%s)', (accounts,))}
    if set(accounts) != known:
        raise ValueError('unknown_account_scope')
    if chats is not None:
        known = {r['id'] for r in c.execute(
            'SELECT id FROM chats WHERE id=ANY(%s) AND account=ANY(%s)', (chats, accounts))}
        if set(chats) != known:
            raise ValueError('unknown_chat_scope')
    return accounts, chats, permissions


def lock_principal(c, principal, *, shared=False):
    # Runtime receives no UPDATE right on principals/keys just to acquire a row
    # lock. All operator ACL/key mutations take the matching exclusive lock.
    function = 'pg_advisory_xact_lock_shared' if shared else 'pg_advisory_xact_lock'
    c.execute('SELECT ' + function + '(hashtextextended(%s,0))', ('acl:' + principal,))


def scope_filter(p, alias='', chat_column='chat'):
    # Identifiers are internal constants, never request values.
    if alias not in ('', 'm', 'c') or chat_column not in ('chat', 'id'):
        raise ValueError('invalid_scope_alias')
    prefix = alias + '.' if alias else ''
    # Scalar equality exposes the leading index key as constant. ANY(singleton)
    # can otherwise make PostgreSQL discard useful date ordering and sort/scan
    # a large permitted archive even for a small LIMIT.
    single_account=len(p['accounts'])==1
    query = prefix + ('account=%s' if single_account else 'account=ANY(%s)')
    args = [p['accounts'][0] if single_account else p['accounts']]
    if p['chat_ids'] is not None:
        single_chat=len(p['chat_ids'])==1
        query += ' AND ' + prefix + chat_column + ('=%s' if single_chat else '=ANY(%s)')
        args.append(p['chat_ids'][0] if single_chat else p['chat_ids'])
    return query, args


def targets(p, ids):
    # Only callers that have already retrieved authorized records use this.
    p['_audit_targets'] = list(dict.fromkeys(ids))[:100]


def audit(c, p, operation, outcome, code=None):
    if operation not in AUDIT_OPERATIONS or outcome not in ('ok', 'denied', 'error'):
        raise ValueError('invalid_audit_record')
    safe_code = code if code in AUDIT_CODES else ('operation_failed' if code else None)
    p = p or {}
    values = (p.get('id'), p.get('key_id'), operation, outcome, safe_code,
              p.get('_audit_targets', []) if outcome == 'ok' else [])
    if operation in ('issue_key','revoke_key','extend_key','set_access','set_sending','set_key_permissions','set_operations',
            'set_feedback_status'):
        c.execute('''INSERT INTO audit_log(principal,key_id,operation,outcome,error_code,resource_ids,actor)
            VALUES(%s,%s,%s,%s,%s,%s,'operator')''', values)
    elif operation in ('send_dispatch','messenger_dispatch'):
        c.execute('''INSERT INTO audit_log(principal,key_id,operation,outcome,error_code,resource_ids,actor)
            VALUES(%s,%s,%s,%s,%s,%s,'collector')''', values)
    else:
        c.execute('''INSERT INTO audit_log(principal,key_id,operation,outcome,error_code,resource_ids)
            VALUES(%s,%s,%s,%s,%s,%s)''', values)


def key_id(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid_key_id') from None
