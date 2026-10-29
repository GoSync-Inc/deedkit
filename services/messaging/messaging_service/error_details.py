"""Safe error classification; no SQL, payloads, filenames or credentials."""
from uuid import uuid4

from psycopg import OperationalError
from psycopg.errors import QueryCanceled, TransactionTimeout
from psycopg_pool import PoolTimeout


def classified(error):
    if isinstance(error,(QueryCanceled,TransactionTimeout)):return 'query_timeout'
    if isinstance(error,PoolTimeout):return 'service_busy'
    if isinstance(error,OperationalError):return 'service_unavailable'
    return getattr(error,'code','temporarily_unavailable')


EXPECTED={'invalid_utc_date':'ISO 8601 date-time with an offset, for example 2026-10-15T09:00:00Z',
    'invalid_limit':'integer from 1 to 100; get_context limit and counts from 0/1 to 50',
    'transcript_requires_conversation_view':'view=conversation (the MCP default; REST and SDKs default to view=archive)'}


def payload(error,operation='',request_id=None):
    code=classified(error)
    retry=code in ('query_timeout','service_busy','service_unavailable','temporarily_unavailable')
    info={'error':code,'request_id':request_id or uuid4().hex,'retryable':retry}
    if hasattr(error,'fields'):info['fields']=error.fields
    if hasattr(error,'field_errors'):info['field_errors']=error.field_errors
    for name in ('hint','resets_in_seconds'):  # Reviewed constants and counters, never input values.
        if hasattr(error,name):info[name]=getattr(error,name)
    # ANYM-531: the caller's own account and normalized number, and archive IDs the key may read.
    # ANYM-532: the normalized number the caller sent and the phone lookup's operation_id.
    for name in ('next_call','found_on_other_account','account','phone','operation_id'):
        if hasattr(error,name):info[name]=getattr(error,name)
    # ANYM-519: reviewed counters, states, steps and a header NAME, never a header value or secret.
    for name in ('retry_after_s','next_step','state','header'):
        if hasattr(error,name):info[name]=getattr(error,name)
    # ANYM-520: a refused resume's test: its error code and HTTP status, never a response body.
    for name in ('test_error','http_status'):
        if hasattr(error,name):info[name]=getattr(error,name)
    # ANYM-545: rulebook v1 (AGENT_API.md) next, candidates and more; archive IDs and labels the key may read.
    for name in ('next','candidates','more'):
        if hasattr(error,name):info[name]=getattr(error,name)
    # ANYM-546: preview_stale's changed fields, fresh preview and its new confirm_token (the caller's own send).
    for name in ('changed','preview','confirm_token'):
        if hasattr(error,name):info[name]=getattr(error,name)
    if retry:info['retry_after_seconds']=2
    if code in EXPECTED:info['expected']=EXPECTED[code]
    if operation in ('search','search_files'):info['results_state']='not_returned_due_to_error'
    # Narrowing helps a slow or busy query, not a rejected parameter.
    if operation in ('search','search_files') and retry:
        info['next_action']='narrow_account_chat_or_date_filters'
        info['fallback']='get_messages_for_known_chat_then_get_context' if operation=='search' else 'get_file_text_for_known_attachment_then_get_context'
    elif hasattr(error,'next_action'):info['next_action']=error.next_action
    elif code in ('not_found','forbidden'):
        info['next_action']='check_reference_and_permitted_scope'
    elif code=='cursor_changed':info['next_action']='restart_query_without_cursor'
    elif code=='subscription_conflict':
        info['next_action']='get_subscription_then_retry_with_current_version'
    elif code in ('subscription_access_changed','subscription_closed'):
        info['next_action']='create_new_subscription_after_checking_scope'
    elif code=='subscription_limit':
        info['next_action']='ask_operator_to_review_subscription_budget'
    else:info['next_action']='retry_after_delay' if retry else 'check_parameters'
    return info
