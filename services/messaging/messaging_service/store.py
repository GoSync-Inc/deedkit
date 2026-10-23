"""PostgreSQL archive: one policy layer for HTTP and MCP.

No provider sessions, network fetch, arbitrary SQL or server approval gate is
exposed to agents. Reviewed opt-in operations use the existing fenced collectors.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import os
from pathlib import Path
import re
import secrets
import time
import json
import threading
from collections import OrderedDict
from uuid import UUID

from psycopg.errors import TransactionTimeout
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from . import __version__
from .policy import (DEFAULT_PERMISSIONS, OPERATION_PERMISSIONS, UNSET, audit,
                     key_id, lock_principal, normalize_policy, principal_id,
                     scope_filter, targets, effective_permissions, PERMISSIONS, normalize_list,
                     LEGACY_PERMISSIONS)


class APIError(Exception):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


# capabilities sections (ANYM-270): each key of the full payload sits in exactly one
# named block, with today's shape; summary is the short index an agent reads first.
CAPABILITY_SECTIONS={
    'identity':('service','version','principal','scope_version','credential_id','permissions','expires_at','key_permission_ceiling',
                'key_permission_version','chat_scope','auth_mode'),
    'accounts':('accounts',),'coverage':('synthetic','sources','coverage'),
    'tool_manifest':('tool_manifest','available'),'search_options':('search_options',),'sending':('sending',),
    'file_uploads':('file_uploads',),'subscriptions':('subscriptions','events'),'feedback':('feedback',),
    'messenger_operations':('messenger_operations',),'limits':('limits','unavailable')}


def capability_view(full, detail='full', sections=None, permitted=None):
    """`full` unchanged, only the named `sections`, or the summary index. A section lists
    an account's operations only where `permitted` (the key may use them there) names them."""
    if sections is not None:
        if not sections or any(name not in CAPABILITY_SECTIONS for name in sections):raise APIError('invalid_filter')
        result={key:full[key] for name in CAPABILITY_SECTIONS if name in sections
                for key in CAPABILITY_SECTIONS[name] if key in full}
        if 'messenger_operations' in result:
            block=dict(result['messenger_operations']);accounts={}
            for account,row in block['supported_by_account'].items():
                allowed=set((permitted or {}).get(account,()))
                operations=[name for name in row['operations'] if name in allowed]
                accounts[account]={**row,'operations':operations,'count':len(operations),
                    'available_operations':[name for name in row['available_operations'] if name in allowed]}
            block['supported_by_account']=accounts;result['messenger_operations']=block
        return result
    if detail=='full':return full
    if detail!='summary':raise APIError('invalid_filter')
    scope=full['chat_scope']
    return {'service':full['service'],'version':full['version'],'principal':full['principal'],
        'credential_id':full['credential_id'],'permissions':full['permissions'],'expires_at':full['expires_at'],
        # ANYM-470: the key's expiry and each owner's ID stay in the default view.
        'accounts':[{k:a[k] for k in ('id','platform','label','self_provider_id')} for a in full['accounts']],
        'chat_scope':{'all_in_permitted_accounts':scope['all_in_permitted_accounts'],
                      'chat_count':None if scope['chat_ids'] is None else len(scope['chat_ids'])},
        'tool_manifest':{'version':full['tool_manifest']['version']},
        'detail':'summary','sections':list(CAPABILITY_SECTIONS),
        'next_action':'call capabilities again with sections=[names] for those blocks or detail=full for everything'}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def bounded(value: int, maximum: int = 100) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise APIError('invalid_limit')
    return value


def date_bound(value: str) -> datetime | None:
    if value=='':
        return None
    if not isinstance(value,str):raise APIError('invalid_utc_date')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timezone required')
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise APIError('invalid_utc_date') from None


def serial(rows):
    if isinstance(rows, dict):
        return {k: serial(v) for k, v in rows.items()}
    if isinstance(rows, list):
        return [serial(v) for v in rows]
    if isinstance(rows, datetime):
        return rows.isoformat()
    if isinstance(rows, UUID):
        return str(rows)
    return rows


class Store:
    def __init__(self, dsn: str, files: Path, public_url: str):
        from .security_logging import redact_library_logs
        redact_library_logs()
        self._status_cache=OrderedDict()
        self._status_cache_lock=threading.Lock()
        self.files = files.resolve()
        self.public_url = public_url.rstrip('/')
        # Keepalives and tcp_user_timeout turn a half-open connection into a
        # prompt OperationalError instead of a call that outlives the lease.
        self.pool = ConnectionPool(dsn, min_size=1, max_size=6, timeout=5, open=True,
            kwargs={'row_factory': dict_row, 'connect_timeout': 5,
                    'keepalives': 1, 'keepalives_idle': 30, 'keepalives_interval': 10,
                    'keepalives_count': 3, 'tcp_user_timeout': 30000,
                    'options': '-c statement_timeout=5000 -c timezone=UTC'})
        self.pool.wait(timeout=10)

    def close(self):
        self.pool.close()

    @staticmethod
    def identity(c, token: str):
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise APIError('unauthorized', 401)
        return Store.identity_digest(c, digest(token))

    @staticmethod
    def identity_digest(c, key_digest, *, missing='unauthorized', status=401):
        known = c.execute('SELECT principal,id FROM api_keys WHERE digest=%s', (key_digest,)).fetchone()
        if not known:
            raise APIError(missing, status)
        lock_principal(c, known['principal'], shared=True)
        # READ COMMITTED rechecks after a concurrent operator changed access.
        # clock_timestamp also rejects credentials that expired while waiting.
        row = c.execute('''SELECT p.*, k.digest AS key_digest,k.id AS key_id,
            k.permissions AS key_permissions,k.permission_version AS key_permission_version,
            k.expires_at AS key_expires_at FROM api_keys k
            JOIN principals p ON p.id=k.principal
            WHERE k.digest=%s AND NOT k.revoked AND k.expires_at>clock_timestamp() AND p.enabled''',
            (key_digest,)).fetchone()
        if not row:
            error = APIError(missing, status)
            error.audit_identity = {'id': known['principal'], 'key_id': known['id']}
            raise error
        row['principal_permissions'] = row['permissions']
        row['permissions'] = effective_permissions(row['permissions'], row['key_permissions'])
        return row

    @contextmanager
    def access(self, operation, resolve_identity):
        failure = None
        p = None
        late = None
        try:
            with self.pool.connection() as c:
                with c.transaction():
                    try:
                        with c.transaction():
                            p = resolve_identity(c)
                            permission = OPERATION_PERMISSIONS[operation] if operation else None
                            if permission and permission not in p['permissions']:
                                raise APIError('forbidden', 403)
                            from .result_evidence import FILES_ROOT
                            root = FILES_ROOT.set(self.files)
                            try:
                                yield c, p
                            finally:
                                FILES_ROOT.reset(root)
                    except Exception as error:
                        # Roll back the attempted action, but commit its denial audit
                        # outside the savepoint. Successful mutations + audit commit
                        # atomically; a broken audit sink never becomes silent success.
                        from .error_details import classified
                        code=classified(error)
                        failure = APIError(code,503) if code in ('query_timeout','service_busy','service_unavailable') else error
                        p = p or getattr(error, 'audit_identity', None)
                        # transaction_timeout ends the server session (ANYM-265): nothing was committed, and the
                        # failure is audited on a fresh connection instead of the closed one.
                        late = isinstance(error, TransactionTimeout) and c.closed
                    if operation:
                        outcome = 'ok' if failure is None else ('denied' if isinstance(failure, APIError) else 'error')
                        entry = (p, operation, outcome,
                                 failure.code if isinstance(failure, APIError) else ('operation_failed' if failure else None))
                        if not late:
                            audit(c, *entry)
        except TransactionTimeout:
            # The audit write or the commit ran past transaction_timeout: the server rolled the whole
            # transaction back and closed the session, so the timeout is audited on a fresh connection.
            if not operation:
                raise
            failure, late = APIError('query_timeout', 503), True
            entry = (p, operation, 'denied', 'query_timeout')
        if late and operation:
            with self.pool.connection() as fresh:
                audit(fresh, *entry)
        if failure is not None:
            failure.operation=operation
            if isinstance(failure,APIError) and operation:failure.audited=True
            raise failure

    def authorized(self, token, operation=None):
        return self.access(operation, lambda c: self.identity(c, token))

    def audit_boundary(self, code):
        with self.pool.connection() as c:
            audit(c, None, 'http_boundary', 'denied', code)

    def authenticate(self, token):
        with self.authorized(token) as (_, p):
            return p['id']

    @staticmethod
    def dataset(c,p):
        if p['chat_ids'] is not None:
            # Account-wide last_ingest_at/detail may change only because of a
            # hidden chat. Do not disclose those aggregates to a chat-scoped key.
            rows=c.execute('''SELECT a.id,a.synthetic,'chat_scope_only' AS coverage,
                NULL AS status,NULL AS last_ingest_at,NULL AS detail_code FROM accounts a
                WHERE a.id=ANY(%s) AND EXISTS(SELECT 1 FROM chats c
                    WHERE c.account=a.id AND c.id=ANY(%s)) ORDER BY a.id''',
                (p['accounts'],p['chat_ids'])).fetchall()
            return {'synthetic':all(r['synthetic'] for r in rows),'sources':rows,'coverage':'chat_scope_only'}
        rows=c.execute('''SELECT a.id,a.synthetic,coalesce(s.coverage,'synthetic_fixture_only') AS coverage,
            s.status,s.last_ingest_at,s.detail_code FROM accounts a LEFT JOIN source_status s ON s.account=a.id
            WHERE a.id=ANY(%s) ORDER BY a.id''',(p['accounts'],)).fetchall()
        conflicts={r['account']:r for r in c.execute('''SELECT m.account,
            count(*) FILTER(WHERE mc.resolved_at IS NULL) AS active_conflicts,
            count(*) FILTER(WHERE mc.resolved_at IS NOT NULL) AS historical_conflicts
            FROM message_conflicts mc JOIN messages m ON m.id=mc.message
            WHERE m.account=ANY(%s) GROUP BY m.account''',(p['accounts'],))}
        for row in rows:
            row.update({k:v for k,v in conflicts.get(row['id'],{'active_conflicts':0,'historical_conflicts':0}).items() if k!='account'})
            row['detail_scope']='account_summary_not_this_result'
            if row['detail_code']=='version_conflict' and not row['active_conflicts']:
                row['detail_code']='coverage_not_reconciled'
            from .status_details import describe
            row['detail']=describe(row['detail_code'])
        return serial({'synthetic':all(r['synthetic'] for r in rows),'sources':rows,
                       'coverage':'partial_or_unknown' if any(r['status'] for r in rows) else 'synthetic_fixture_only'})

    @staticmethod
    def message(c, p, mid):
        if not isinstance(mid,str) or not 1<=len(mid)<=256:
            raise APIError('not_found',404)
        where,args=scope_filter(p)
        row = c.execute('SELECT * FROM messages WHERE id=%s AND '+where, [mid,*args]).fetchone()
        if not row:
            raise APIError('not_found', 404)

        for key in list(row):
            if key=='document' or key.startswith('document_'):row.pop(key)
        targets(p,[row['id']])
        return row

    def capabilities(self, token, detail='full', sections=None):
        # Python callers keep today's payload; MCP asks for the summary by default (ANYM-270).
        with self.authorized(token,'capabilities') as (c, p):
            from .messenger_operations import CATALOG_VERSION,OPERATIONS,operation_manifest
            from .commands import manifest
            dataset=self.dataset(c,p)
            from .operational import identity_safe,self_provider_id
            # Same visible accounts as before; each adds the owner's ID its collector
            # verified at connect, or null (ANYM-470). Never derived from messages. Only
            # identity's written shape leaves SQL: the driver never decodes another one.
            accounts = c.execute('''SELECT a.id,a.platform,a.label,a.synthetic,
                ('''+identity_safe('h.operations')+''')->'identity'->>'self_provider_id' AS self_provider_id FROM accounts a
                LEFT JOIN collector_health h ON h.account=a.id WHERE a.id=ANY(%s) ORDER BY a.id''',
                ([r['id'] for r in dataset['sources']],)).fetchall()
            for account in accounts:
                account['self_provider_id']=self_provider_id(account['platform'],account['self_provider_id'])
            expires_at=p['key_expires_at']
            supported_by_account={};permitted={}
            for account in accounts:
                # Same rows as list_messenger_operations: supported, plus what this key
                # can use now (key permitted and opted in for the account).
                listed=manifest(c,p,account['id'])
                supported=[row['operation'] for row in listed['items']]
                permitted[account['id']]=[row['operation'] for row in listed['items'] if row['key_permitted']]
                supported_by_account[account['id']]={'provider':listed['provider'],'count':len(supported),
                    'operations':supported,'available_operations':[row['operation'] for row in listed['items'] if row['available']]}
            features={'read':['search','history','context','updates','reviews','search_people','search_files','get_file_text','get_file_occurrences','get_sync_status','get_file_segments','resolve_reference','preview_message'], 'download':['attachment','request_file_download'], 'cursor-write':['ack'], 'send':['send_message','get_send_status']}
            if 'read' in p['permissions']:
                features['review-write']=['record_review']
                features['cursor-write'].append('dismiss_reply')
                features['cursor-write'].extend(['snooze_reply','restore_reply','mute_reply_source','unmute_reply_source'])
                features['read'].append('list_reply_dismissals')
                features['read'].extend(['list_subscriptions','get_subscription','get_subscription_events','fetch_subscription_events','wait_for_subscription_events','list_feedback'])
                features['subscription-ack']=['ack_subscription_events']
                features['feedback-write']=['submit_feedback']
                features['subscription-write']=['create_subscription','update_subscription','configure_subscription_delivery','test_subscription']
            available=[feature for permission,names in features.items() if permission in p['permissions'] for feature in names]
            if {'read','send','upload'}<=set(p['permissions']):
                available.extend(['prepare_file_upload','upload_file_chunk','get_file_upload'])
            operation_tools=['list_messenger_operations','preview_messenger_operation',
                             'execute_messenger_operation','get_messenger_operation']
            if any(spec['permission'] in p['permissions'] for spec in OPERATIONS.values()):
                available.extend(operation_tools)
            else:available.append('list_messenger_operations')
            return capability_view({'service': 'anymsg', 'version': __version__, **dataset,
                'tool_manifest':{'version':__version__,'names':sorted(OPERATION_PERMISSIONS.keys()-{'download'}),
                    'discovery_action':'refresh_connection_if_tools_list_differs',
                    'rest_fallback':'POST /v1/{tool_name} with the same bearer and JSON parameters'},
                'principal': p['id'], 'scope_version': p['scope_version'], 'accounts': accounts,
                'credential_id':str(p['key_id']), 'permissions':p['permissions'],
                'expires_at':expires_at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ') if expires_at else None,
                'key_permission_ceiling':p['key_permissions'] if p['key_permissions'] is not None else sorted(LEGACY_PERMISSIONS),
                'key_permission_version':p['key_permission_version'],
                'chat_scope':{'all_in_permitted_accounts':p['chat_ids'] is None,'chat_ids':p['chat_ids']},
                'auth_mode': 'managed_bearer_keys',
                'search_options':{'filters':['accounts','chats','senders','from_time','to_time','direction','exclude_channels',
                        'chat_types','include_channels'],
                    'search_channels_default':'excluded_unless_include_channels_chat_types_or_named_chats',
                    'query_modes':['all','phrase','web','exact'],'orders':['asc','desc','relevance'],
                    'languages':['auto','simple','russian','english','indonesian'],
                    'fuzzy':'explicit_bounded_fallback','compact':True,'optional_coverage':True,
                    'context':'independent_before_and_after_counts','chat_orders':['id','activity','last_activity_desc','priority'],
                    'chat_order_default':'activity','awaiting_reply_default_days':14,
                    'awaiting_reply_bots':'exclude_observed_bot_senders_not_name_guesses',
                    'reply_queue_filters':['exclude_chats','exclude_senders','only_questions','only_unread','chat_types','exclude_likely_broadcasts','promotional'],
                    'reply_queue_dismissal':'credential_local_until_next_meaningful_incoming',
                    'owner_read_state':'timestamped_provider_observations_unknown_or_stale_explicit',
                    'sync_status_details':['compact','full'],
                    'updates':'version_checked_filter_reset_preserves_ack_position',
                    'update_orders':['event','sent_at_within_delivered_batch'],
                    'updates_exclude_channels':True,'updates_start_from':['beginning','now_creation_only'],'is_backfill':True,
                    'detail_default':'compact','details':['compact','full'],
                    'message_details':['compact','full','transcript'],
                    'collector_operations':['connection','live_capture','history','media'],
                'connection_failures':'durable_terminal_gate_and_capped_retry','status_cache_seconds':10,'status_refresh':True,
                    'people_candidates':['transliteration','same_sender_grouping'],
                    'list_limit':100,'people_lookup':'observed_name_username_or_sender_id',
                    'indexed_content':['message_text','filename','bounded_file_text','configured_model_results'],
                    'model_processing':'optional_operator_configured_local_or_api',
                    'file_content_search':True,'file_indexing':'asynchronous_local_worker',
                    'file_search_sources':['extracted','enriched','understanding','message_context'],
                    'file_caption_provenance':'current_parent_message_revision_only',
                    'chat_coverage':True,'provider_id_lookup':'resolve_reference',
                    'empty_result_diagnostics':True,'message_conflicts':'scoped_active_and_historical',
                    'forwarded_messages':{'origin_field':'forward_origin','authorship':'forwarded_by_sender',
                        'rule':'sender_is_the_forwarder_never_quote_forwarded_words_as_written_by_sender',
                        'origin_kinds':['user','chat','channel','hidden_user','status','newsletter','unknown','not_captured'],
                        'origin_grants_chat_access':False}},
                'available':available,
                'file_uploads':{'implemented':True,'permission':'upload','required_permissions':['read','send','upload'],
                    'new_permission_inherited':False,'ownership':'originating_key_account_destination_chat',
                    'expires_seconds':3600,'chunk_bytes':262144,'max_bytes':32*1024**2,
                    'changes_provider_state':False,'paths_and_urls_accepted':False,
                    'send_via':'preview_and_execute_messenger_operation_send_file'},
                'subscriptions':{'implemented':True,'definitions':True,'passive_events':True,
                    'source_selection':'frozen_explicit_chats_and_account_scoped_sender_ids',
                    'event_selection':'structural_exact_revision','origin_default':'live',
                    'mutation_permission':'subscription-write','read_permission_required':True,
                    'ownership':'originating_credential_and_principal','new_permission_inherited':False,
                    'source_scope_change':'masked_access_changed_recreate_required',
                    'ack':True,'ack_permission':'subscription-ack','push':'operator_registered_signed_hints','wait_max_seconds':25,'verified_host':False,'native_reply_preset':True,
                    'provider_polling':False,'start_boundary':'committed_archive_frontier',
                    'existing_update_consumers_unchanged':True,
                    'delivery_policy':{'tool':'configure_subscription_delivery','argument':'delivery_policy',
                        'changes':'hint_timing_only','changes_matches_ack_or_reads':False,'requires_receiver':True,
                        'bounds':{'debounce_s':[0,600],'max_wait_s':[1,3600],'batch_window_s':[0,86400],
                            'batch_count':[1,1000],'expect_reply_within_s':[60,1209600],'quiet_windows':[1,4]},
                        'debounce_and_batch_exclusive':True,'max_wait_default':'max(300,debounce_s)',
                        'quiet_hours':'iana_timezone_weekday_windows_held_not_dropped',
                        'quiet_bypass':['incoming_message','owner_mention','reply_to_owner','reply_to_confirmed_send','silence'],
                        'silence_timer':'reply_to_send_subscriptions_only','silence_hint_kind':'silence',
                        'key_budget':{'setting':'SUBSCRIPTION_MAX_HINTS_PER_HOUR','default':20,'bounds':[1,1000],
                            'scope':'every_hint_of_the_key','applies_without_policy':True,'over_budget':'coalesced_never_dropped'},
                        'held_reasons':['debounce','batch','quiet_hours','budget'],'clock':'database_utc',
                        'idempotency':'one_hint_per_subscription_version_window'}},
                'events':{'fetch':'fetch_subscription_events','ack':'ack_subscription_events','wait':'wait_for_subscription_events','wait_max_seconds':25,'test':'test_subscription','delivery':'operator_registered_signed_hints','host_turn_verified':False},
                'feedback':{'implemented':True,'write_permission':'feedback-write','ownership':'exact_credential','external_delivery':False},
                'sending':{'implemented':True,'wait_max_seconds':25,'approval_mode':'acting_agent_obtains_exact_owner_confirmation',
                    'server_approval_gate':False,'text_only':False,'legacy_send_message_text_only':True,
                    'request_id_required':True,
                    'formatting':{'formats':['plain','markdown','html'],
                        'preview_tool':'preview_message','preview_permission':'read',
                        'preview_contacts_provider':False,'plain_default':True,
                        'rich_preview_digest_required':True,'digest_proves_owner_approval':False,
                        'digest_binds':['principal','scope_version','account','chat','reply_target',
                            'source','format','rendered_payload','renderer_version','link_preview','allow_lossy'],
                        'link_preview_default':False,'automatic_splitting':False,
                        'whatsapp_link_preview_control':'provider_multi_device_limitation',
                        'whatsapp_plain_native_markup':'provider_may_interpret_delimiters'},
                    'ambiguous_result_policy':'unknown_no_automatic_retry',
                    'delivery_observations':{'telegram':'private_outbox_read_boundary',
                        'whatsapp':'message_ack_private_or_group_aggregate',
                        'provider_timestamps_available':False,'historical_coverage_guaranteed':False,
                        'event_kind':'message.status','changes_provider_read_state':False},
                    'read_permission_does_not_allow_send':True},
                'messenger_operations':{'implemented':True,'catalog_version':CATALOG_VERSION,
                    'operation_count':len(OPERATIONS),'tools':operation_tools,
                    'supported_count_by_platform':{platform:sum(item['supported'] for item in operation_manifest(platform))
                                                   for platform in ('telegram','whatsapp')},
                    'platform_counts_basis':'legacy_defaults_use_account_manifest_for_actual_support',
                    'supported_by_account':supported_by_account,
                    'availability_tool':'list_messenger_operations','account_opt_in_required':True,
                    'independent_key_permissions':True,'new_permissions_inherited_by_existing_keys':False,
                    'administration':['groups','contacts','account_profile'],
                    'archive_read_remains_passive':True,'provider_session_owner':'existing_fenced_collector',
                    'mutation_preview_digest_required':True,'preview_digest_proves_owner_approval':False,
                    'ambiguous_mutations':'unknown_no_automatic_retry','registered_file_limit_bytes':32*1024**2,
                    'read_state_requires_explicit_permission':True},
                'unavailable': ['guaranteed_full_history', 'drafts',
                                'owner_approval', 'project_memory', 'schedules', 'oauth'],
                'limits': {'page_size': 100, 'query_chars': 200,
                           'download_grant_seconds': 120, 'delivery_seconds': 900,
                           'review_sources':20, 'context_reviews':5,'send_text_utf16_units':4000,
                           'format_source_utf16_units':16000,'http_json_body_bytes':16384,
                           'upload_chunk_http_json_body_bytes':360448,
                           'send_queue_ttl_seconds':600,'send_per_account_per_minute':60}},detail,sections,permitted)

    def preview_message(self,token,account,chat_id,text,reply_to='',format='plain',
                        link_preview=False,allow_lossy=False):
        from .outgoing import preview
        with self.authorized(token,'preview_message') as (c,p):
            return preview(c,p,account,chat_id,text,reply_to,format,link_preview,allow_lossy)

    def send_message(self,token,account,chat_id,text,request_id,reply_to='',format='plain',
                     preview_digest='',link_preview=False,allow_lossy=False):
        from .outgoing import enqueue
        with self.authorized(token,'send_message') as (c,p):
            return enqueue(c,p,account,chat_id,text,request_id,reply_to,format,
                preview_digest,link_preview,allow_lossy)

    def get_send_status(self,token,send_id):
        from .outgoing import status
        with self.authorized(token,'get_send_status') as (c,p):
            return status(c,p,send_id)

    def prepare_file_upload(self,token,account,chat_id,filename,mime,bytes,sha256,request_id):
        from .uploads import prepare
        with self.authorized(token,'prepare_file_upload') as (c,p):
            return prepare(self,c,p,account,chat_id,filename,mime,bytes,sha256,request_id)

    def upload_file_chunk(self,token,upload_id,offset,data_base64):
        from .uploads import chunk
        with self.authorized(token,'upload_file_chunk') as (c,p):
            return chunk(self,c,p,upload_id,offset,data_base64)

    def get_file_upload(self,token,upload_id):
        from .uploads import status
        with self.authorized(token,'get_file_upload') as (c,p):
            return status(self,c,p,upload_id)

    def cleanup_file_uploads(self):
        """API-owned maintenance; retains staged sources of pending sends."""
        from .uploads import cleanup
        with self.pool.connection() as c:
            cleanup(self,c)

    def list_messenger_operations(self,token,account):
        from .commands import manifest
        with self.authorized(token,'list_messenger_operations') as (c,p):
            return manifest(c,p,account)

    def preview_messenger_operation(self,token,account,operation,chat_id='',message_id='',
                                   expected_revision=0,parameters=None):
        from .commands import preview
        with self.authorized(token,'preview_messenger_operation') as (c,p):
            return preview(self,c,p,account,operation,chat_id,message_id,expected_revision,parameters)

    def execute_messenger_operation(self,token,account,operation,chat_id='',message_id='',
                                   expected_revision=0,parameters=None,request_id='',preview_digest=''):
        from .commands import enqueue
        with self.authorized(token,'execute_messenger_operation') as (c,p):
            return enqueue(self,c,p,account,operation,chat_id,message_id,expected_revision,
                           parameters,request_id,preview_digest)

    def get_messenger_operation(self,token,operation_id):
        from .commands import status
        with self.authorized(token,'get_messenger_operation') as (c,p):
            return status(self,c,p,operation_id)

    def set_messenger_operations(self,account,operations):
        """Owner-only account opt-in; this method is not exposed through MCP."""
        from .messenger_operations import operation_manifest
        from .whatsapp_capabilities import account_provider
        values=normalize_list(operations,maximum=100)
        with self.pool.connection() as c:
            row=c.execute('SELECT platform FROM accounts WHERE id=%s',(account,)).fetchone()
            if not row:raise ValueError('unknown_account')
            supported={entry['operation'] for entry in operation_manifest(row['platform'],provider=account_provider(c,account,row['platform'])) if entry['supported']}
            if set(values)-supported:raise ValueError('unsupported_operation')
            changed=c.execute('UPDATE account_policies SET operations_enabled=%s WHERE account=%s',
                              (values,account)).rowcount
            if not changed:raise ValueError('unknown_account')
            audit(c,None,'set_operations','ok')

    def list_chats(self, token, after='', limit=25, *, accounts=None,query='',chat_types=None,
                   order='id',exclude_channels=False,awaiting_reply=False,from_time='',to_time='',
                   include_coverage=True,awaiting_reply_days=14,exclude_chats=None,exclude_senders=None,
                   only_questions=False,only_unread=False,view='archive',exclude_likely_broadcasts=False,
                   include_muted=False,promotional='exclude'):
        from .chat_queries import list_chats
        from .message_queries import hint_forward_origin
        with self.authorized(token,'list_chats') as (c,p):
            result=list_chats(self,c,p,after,limit,accounts=accounts,query=query,chat_types=chat_types,
                order=order,exclude_channels=exclude_channels,awaiting_reply=awaiting_reply,
                from_time=from_time,to_time=to_time,include_coverage=include_coverage,
                awaiting_reply_days=awaiting_reply_days,exclude_chats=exclude_chats,exclude_senders=exclude_senders,
                only_questions=only_questions,only_unread=only_unread,view=view,
                exclude_likely_broadcasts=exclude_likely_broadcasts,include_muted=include_muted,promotional=promotional)
            hint_forward_origin(c,result['items'])
            return result

    def dismiss_reply(self,token,chat_id,message_id,expected_revision,reason=None,note=None):
        from .reply_state import dismiss
        with self.authorized(token,'dismiss_reply') as (c,p):
            return dismiss(self,c,p,chat_id,message_id,expected_revision,reason,note)

    def snooze_reply(self,token,chat_id,message_id,expected_revision,until,note=None):
        from .reply_state import snooze
        with self.authorized(token,'snooze_reply') as (c,p):
            return snooze(self,c,p,chat_id,message_id,expected_revision,until,note)

    def restore_reply(self,token,chat_id,message_id,expected_revision):
        from .reply_state import restore
        with self.authorized(token,'restore_reply') as (c,p):
            return restore(self,c,p,chat_id,message_id,expected_revision)

    def mute_reply_source(self,token,chat_id=None,account=None,sender_id=None,reason=None,note=None):
        from .reply_state import mute
        with self.authorized(token,'mute_reply_source') as (c,p):
            return mute(self,c,p,chat_id,account,sender_id,reason,note)

    def unmute_reply_source(self,token,chat_id=None,account=None,sender_id=None):
        from .reply_state import unmute
        with self.authorized(token,'unmute_reply_source') as (c,p):
            return unmute(self,c,p,chat_id,account,sender_id)

    def list_reply_dismissals(self,token,kind='dismissed',after='',limit=20):
        from .reply_state import list_entries
        with self.authorized(token,'list_reply_dismissals') as (c,p):
            return list_entries(self,c,p,kind,after,limit)

    def search_people(self,token,query='',*,accounts=None,chats=None,after='',limit=20,
                      transliterate=False,group_by_sender=False,person=''):
        from .people_candidates import search_people
        with self.authorized(token,'search_people') as (c,p):
            if type(transliterate) is not bool or type(group_by_sender) is not bool:
                raise APIError('invalid_filter')
            return search_people(self,c,p,query,accounts=accounts,chats=chats,after=after,limit=limit,
                transliterate=transliterate,group_by_sender=group_by_sender,person=person)

    def search_files(self,token,query='',account='',chat='',sender='',from_time='',to_time='',
                     after='',limit=20,*,accounts=None,chats=None,senders=None,query_mode='all',
                     order='asc',before='',direction='',filename='',search_language='auto',include_coverage=True):
        from .file_index import search_files
        with self.authorized(token,'search_files') as (c,p):
            return search_files(self,c,p,query,account,chat,sender,from_time,to_time,after,limit,
                accounts=accounts,chats=chats,senders=senders,query_mode=query_mode,order=order,
                before=before,direction=direction,filename=filename,search_language=search_language,
                include_coverage=include_coverage)

    def get_file_text(self,token,attachment_id,offset=0,limit=4000,content=None):
        from .file_index import get_file_text
        with self.authorized(token,'get_file_text') as (c,p):
            return get_file_text(self,c,p,attachment_id,offset=offset,limit=limit,content=content)

    def get_file_occurrences(self,token,attachment_id,after='',limit=20):
        from .file_index import get_file_occurrences
        with self.authorized(token,'get_file_occurrences') as (c,p):
            return get_file_occurrences(self,c,p,attachment_id,after=after,limit=limit)

    def get_file_segments(self,token,attachment_id,offset=0,limit=100):
        from .file_index import get_file_segments
        with self.authorized(token,'get_file_segments') as (c,p):
            return get_file_segments(self,c,p,attachment_id,offset=offset,limit=limit)

    def get_sync_status(self,token,chat_id='',from_time='',to_time='',refresh=True):
        from .sync_status import get_sync_status
        with self.authorized(token,'get_sync_status') as (c,p):
            if type(refresh) is not bool:raise APIError('invalid_filter')
            if chat_id:
                from .chat_evidence import chat_statuses
                lower,upper=date_bound(from_time),date_bound(to_time)
                if lower and upper and lower>=upper:raise APIError('invalid_date_range')
                if not isinstance(chat_id,str) or len(chat_id)>256:raise APIError('invalid_filter')
                data=chat_statuses(c,p,[chat_id],lower,upper)
                if not data:raise APIError('not_found',404)
                targets(p,[chat_id]);return serial(data[chat_id])
            if from_time or to_time:raise APIError('chat_required')
            cache_key=(p['key_id'],p['scope_version'])
            now=time.monotonic()
            with self._status_cache_lock:
                cached=self._status_cache.get(cache_key)
                if not refresh and cached and now-cached[0]<10:
                    self._status_cache.move_to_end(cache_key)
                    result=json.loads(cached[1])
                    result['cache']={'hit':True,'age_seconds':round(now-cached[0],3),'max_age_seconds':10}
                    return result
            result=serial(get_sync_status(c,p))
            result['observed_at']=datetime.now(timezone.utc).isoformat()
            with self._status_cache_lock:
                self._status_cache[cache_key]=(time.monotonic(),json.dumps(result))
                self._status_cache.move_to_end(cache_key)
                if len(self._status_cache)>128:self._status_cache.popitem(last=False)
            result['cache']={'hit':False,'age_seconds':0,'max_age_seconds':10}
            return result

    def resolve_reference(self,token,account,provider_chat_id,provider_message_id=''):
        from .chat_evidence import resolve_chat,chat_statuses
        from .message_queries import enrich
        from .search_query import valid_text
        with self.authorized(token,'resolve_reference') as (c,p):
            chat=resolve_chat(c,p,account,provider_chat_id)
            valid_text(provider_message_id,256)
            result={'chat':chat,'sync':chat_statuses(c,p,[chat['id']])[chat['id']]}
            if provider_message_id:
                row=c.execute('SELECT id FROM messages WHERE account=%s AND chat=%s AND provider_id=%s',
                    (account,chat['id'],provider_message_id)).fetchone()
                if not row:raise APIError('not_found',404)
                message=self.message(c,p,row['id']);enrich(c,[message],p);result['message']=message
            return serial(result)

    def search(self, token, query='', account='', chat='', sender='', from_time='',
               to_time='', after='', limit=20, *, accounts=None, chats=None, senders=None, collapse_forwards=True,
               query_mode='all', order='asc', before='', direction='', search_language='auto',
               fuzzy=False, compact=False, body_chars=None, include_coverage=True,view='archive',include_system=False,exclude_channels=False,
               chat_types=None,include_channels=False,person='',_scope=None):
        # None is the internal no-default sentinel of message_queries.search, never a public value.
        if type(include_channels) is not bool:raise APIError('invalid_filter')
        from .message_queries import named_chats
        from .live_search import suggest, scope
        with self.authorized(token,'search') as (c,p):
            # ANYM-478: the live search keeps the scope this page was read under (F-478a); never in the answer.
            if isinstance(_scope,list):_scope.append(scope(p))
            named_chats(c,p,chat,chats)
            result=self._search(c,p,query,account,chat,sender,from_time,to_time,after,limit,
                accounts=accounts,chats=chats,senders=senders,collapse_forwards=collapse_forwards,query_mode=query_mode,
                order=order,before=before,direction=direction,search_language=search_language,
                fuzzy=fuzzy,compact=compact,body_chars=body_chars,include_coverage=include_coverage,view=view,include_system=include_system,exclude_channels=exclude_channels,
                chat_types=chat_types,include_channels=include_channels,person=person)
            # ANYM-478: a thin archive answer names the call that may find more; the hint is optional, so its
            # failure (in a savepoint) never costs the archive answer.
            try:
                with c.transaction():
                    suggest(c,p,{'account':account,'accounts':accounts,'chat':chat,'chats':chats,'chat_types':chat_types,
                        'exclude_channels':exclude_channels},result['result_evidence'])
            except Exception as error:
                import logging
                logging.getLogger(__name__).warning('search_suggestion_failed exception_type=%s',type(error).__name__)
            return result

    def live_search_plan(self,token,arguments,*,paged,request_id):
        """ANYM-478: per-account live entries and the operations to queue; reads only."""
        from .live_search import plan
        with self.authorized(token,'search') as (c,p):
            return plan(c,p,arguments,paged=paged,request_id=request_id)

    def live_search_scope(self,token):
        """ANYM-478: the caller's current search scope, checked as the archive search checks it."""
        from .live_search import scope
        with self.authorized(token,'search') as (_,p):
            return scope(p)

    def live_search_receipts(self,token,operation_ids):
        """ANYM-478: this key's receipts of its live searches, one short read; a missing one is left out."""
        from .commands import status
        result={}
        with self.authorized(token) as (c,p):
            for operation_id in operation_ids:
                try:
                    with c.transaction():result[operation_id]=status(self,c,p,operation_id)
                except APIError:pass
        return result

    def _search(self,c,p,*args,**kwargs):
        from .message_queries import search
        return search(self,c,p,*args,**kwargs)

    def get_messages(self, token, chat_id, after='', limit=20, *, before='', order='asc',
                     from_time='',to_time='',compact=False,body_chars=None,include_coverage=True,view='archive',include_system=False):
        with self.authorized(token,'get_messages') as (c,p):
            if not isinstance(chat_id,str) or not 1<=len(chat_id)<=256:raise APIError('not_found',404)
            return self._search(c,p,chat=chat_id,after=after,limit=limit,before=before,
                order=order,from_time=from_time,to_time=to_time,compact=compact,
                body_chars=body_chars,include_coverage=include_coverage,view=view,include_system=include_system)

    def get_context(self, token, message_id, limit=10, *, before_count=None,after_count=None,
                    compact=False,body_chars=None,include_coverage=True,view='archive',include_system=False):
        from .message_queries import enrich
        from .search_query import coverage,output_options,shape_messages
        from .conversation import options,order_terms,visible_sql,sort_key,decorate_messages,presentation
        options(view,include_system)
        conversation=view=='conversation'
        with self.authorized(token,'get_context') as (c,p):
            bounded(limit,50)
            cap=output_options(compact,body_chars,include_coverage)
            explicit=before_count is not None or after_count is not None
            if explicit:
                before_count=5 if before_count is None else before_count
                after_count=5 if after_count is None else after_count
                if any(type(v) is not int or not 0<=v<=50 for v in (before_count,after_count)):
                    raise APIError('invalid_limit')
            anchor=self.message(c,p,message_id)
            snapshot=None
            if conversation:
                from .message_queries import history_snapshot
                snapshot=history_snapshot(c,p,anchor['chat'],include_system=include_system)
                # Authorization remains READ COMMITTED. Re-read source evidence
                # after pinning the view, then reject changes around all scans.
                anchor=self.message(c,p,message_id)
                enrich(c,[anchor],p)
            provider=str(anchor['provider_id'])
            numeric=int(provider) if anchor.get('platform')=='telegram' and provider.isascii() and provider.isdigit() and len(provider)<=18 else 0
            scans=[]
            for op,sort,count in [('<','DESC',before_count if explicit else limit),
                                  ('>','ASC',after_count if explicit else limit)]:
                if conversation:
                    terms=order_terms('m')
                    selected=c.execute('SELECT m.id,m.account,m.chat,m.sender,m.body,m.sent_at,m.revision,m.direction '
                        'FROM messages m LEFT JOIN message_metadata cmd ON cmd.message=m.id AND cmd.revision=m.revision '
                        'WHERE m.account=%s AND m.chat=%s AND NOT m.deleted AND '+
                        (visible_sql('m','cmd')+' AND ' if not include_system else '')+
                        '('+','.join(terms)+')'+op+'(%s,%s,%s) ORDER BY '+
                        ','.join(t+' '+sort for t in terms)+' LIMIT %s',
                        (anchor['account'],anchor['chat'],anchor['sent_at'],numeric,anchor['id'],count+1)).fetchall()
                else:
                    selected=c.execute('SELECT id,account,chat,sender,body,sent_at,revision,direction FROM messages '
                        'WHERE account=%s AND chat=%s AND NOT deleted AND (sent_at,id)'+op+'(%s,%s) '
                        'ORDER BY sent_at '+sort+',id '+sort+' LIMIT %s',
                        (anchor['account'],anchor['chat'],anchor['sent_at'],anchor['id'],count+1)).fetchall()
                scans.append((selected[:count],len(selected)>count))
            older,newer=scans[0][0],scans[1][0]
            center=[] if anchor['deleted'] or (conversation and not include_system and (anchor.get('is_service') or anchor.get('message_kind')=='system')) else [{key:anchor[key] for key in
                ('id','account','chat','sender','body','sent_at','revision','direction')}]
            rows=older+center+newer
            if not explicit:
                rows=sorted(rows,key=lambda r:(r['id']!=anchor['id'],
                    abs((r['sent_at']-anchor['sent_at']).total_seconds())))[:limit]
            if conversation:
                # Excerpts spend their budget in the returned order (ANYM-490).
                enrich(c,rows,p,order_key=sort_key);rows.sort(key=sort_key)
                shape_messages(rows,compact=False,body_chars=cap)
                shape_messages([anchor],compact=False,body_chars=cap)
                decorate_messages(c,p,rows,include_system=include_system);decorate_messages(c,p,[anchor],include_system=include_system)
                if cap is not None:
                    for row in [*rows,anchor]:
                        if len(row['display_text'])>cap:
                            row['display_text']=row['display_text'][:cap];row['display_text_truncated']=True
            else:
                rows.sort(key=lambda r:(r['sent_at'],r['id']))
                enrich(c,rows,p);enrich(c,[anchor],p)
                shape_messages(rows,compact=compact,body_chars=cap)
                shape_messages([anchor],compact=compact,body_chars=cap)
            files=[f for f in anchor['attachments'] if f['download_status']=='ready']
            jobs=[f for f in anchor['attachments'] if f['download_status']!='ready']
            from .reviews import list_reviews
            reviews=list_reviews(c,p,message_id,limit=5)
            window={'older_cursor':rows[0]['id'] if rows else message_id,
                'newer_cursor':rows[-1]['id'] if rows else message_id}
            if explicit:
                window.update({'before_count':len(older),'after_count':len(newer),
                    'has_more_before':scans[0][1],'has_more_after':scans[1][1],
                    'requested_before':before_count,'requested_after':after_count,
                    'neighbors_coverage':'archived_messages_only'})
            from .chat_evidence import chat_statuses
            extra={}
            if conversation:
                from .conversation_state import conversation_state
                extra={'conversation_view':presentation(rows,order='asc',include_system=include_system),
                    'conversation':conversation_state(c,p,anchor['account'],anchor['chat'])}
                from .message_queries import history_cursor
                # A hidden/tombstoned anchor still defines usable boundaries
                # when the requested visible window is empty.
                window.update(older_cursor=history_cursor(c,p,rows[0] if rows else anchor,order='desc',include_system=include_system,snapshot=snapshot),
                    newer_cursor=history_cursor(c,p,rows[-1] if rows else anchor,order='asc',include_system=include_system,snapshot=snapshot),
                    older_order='desc',newer_order='asc')
                window.update(view='conversation',include_system=include_system,
                    anchor_hidden=bool(anchor['deleted'] or (not include_system and anchor['is_service'])))
            result=serial({'message':anchor,'context':rows,'context_window':window,
                'attachments':files,'pending_attachments':jobs,'reviews':reviews,
                **extra,'chat_sync':chat_statuses(c,p,[anchor['chat']])[anchor['chat']] if include_coverage else None,
                **coverage(self,c,p,include_coverage)})
            if conversation and history_snapshot(c,p,anchor['chat'],include_system=include_system)!=snapshot:
                raise APIError('cursor_changed',409)
            return result

    def get_reviews(self, token, message_id, after='', limit=20):
        from .reviews import list_reviews
        with self.authorized(token,'get_reviews') as (c,p):
            self.message(c,p,message_id)
            return serial({**list_reviews(c,p,message_id,after,limit),**self.dataset(c,p)})

    def record_review(self, token, message_id, revision, result_ref, request_id, evidence=None):
        from .reviews import record
        with self.authorized(token,'record_review') as (c,p):
            return serial(record(c,p,message_id,revision,result_ref,request_id,evidence))

    def request_file_download(self, token, attachment_id):
        from .media_requests import request
        with self.authorized(token,'request_file_download') as (c,p):
            return request(self,c,p,attachment_id)

    def get_attachment(self, token, attachment_id):
        with self.authorized(token,'get_attachment') as (c, p):
            if not isinstance(attachment_id,str) or not 1<=len(attachment_id)<=256:raise APIError('not_found',404)
            where,args=scope_filter(p,'m')
            row = c.execute('''SELECT a.*,m.account,m.chat,m.provider_id AS provider_message_id,
                m.revision AS message_revision,ch.provider_chat AS provider_chat_id
                FROM attachments a JOIN messages m ON m.id=a.message
                JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
                WHERE a.id=%s AND a.active AND NOT m.deleted AND '''+where,[attachment_id,*args]).fetchone()
            if not row:
                from .media_requests import occurrence
                pending=occurrence(c,p,attachment_id)
                if pending:
                    from .attachment_metadata import attachment_fields
                    fields=attachment_fields(pending)
                    targets(p,[pending['id']]);return serial({**fields,
                        'message_id':pending['message'],'reason':fields['download_reason'],
                        **self.dataset(c,p)})
                raise APIError('not_found', 404)
            from .file_index import _available,_absent
            from .attachment_metadata import attachment_fields,processing_phases
            from .file_lifecycle import lifecycle_lock,touch_original
            lifecycle_lock(c)
            row.update(processing_phases(c,row))
            if not _available(self.files,{'sha256':row['digest'],'bytes':row['bytes']}):
                if not _absent(self.files,{'sha256':row['digest']}):
                    row['original_residency']='missing'
                elif row.get('original_residency')=='evicted':
                    # A re-download is bound by the collector's history bound (ANYM-284).
                    from .media_requests import occurrence
                    job=occurrence(c,p,row['id'])
                    if job:row['download_eligibility']=job['download_eligibility']
                targets(p,[row['id']])
                return serial({**attachment_fields(row,original_present=False),
                    'status':'unavailable',**self.dataset(c,p)})
            touch_original(c,row['digest'],row['bytes'])
            grant = secrets.token_urlsafe(32)
            c.execute('DELETE FROM download_grants WHERE expires_at<now()')
            c.execute('''INSERT INTO download_grants VALUES (%s,%s,%s,%s,now()+interval '120 seconds')''',
                      (digest(grant), p['key_digest'], p['scope_version'], row['id']))
            targets(p,[row['id']])
            fields=attachment_fields(row,original_present=True)
            return {**serial(row),**serial(fields), 'status': 'ready', 'message_id':row['message'],
                    **self.dataset(c,p),
                    'download_url': f'{self.public_url}/v1/download/{grant}',
                    'download_expires_in': 120,
                    'notice': 'Temporary bearer link. Do not share or put it in public logs.'}

    def download(self, grant, *, active=None):
        """Resolve a bearer grant to (attachment row, original path, binding).

        The grant's TTL limits admission. `active` is the binding returned when
        a GET on this grant was admitted: revalidating that running transfer
        checks the current key, download permission, scope version and resource
        instead of the TTL, so it outlives the grant but not a revocation.
        """
        def resolve(c):
            if not isinstance(grant,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',grant):raise APIError('not_found',404)
            if active is None:
                row=c.execute('SELECT key_digest FROM download_grants WHERE digest=%s AND expires_at>clock_timestamp()',
                              (digest(grant),)).fetchone()
                if not row:raise APIError('not_found',404)
            p=self.identity_digest(c,row['key_digest'] if active is None else active[0],missing='not_found',status=404)
            if 'download' not in p['permissions']:
                error=APIError('not_found',404);error.audit_identity=p;raise error
            return p
        with self.access('download',resolve) as (c,p):
            from .file_lifecycle import lifecycle_lock,touch_original
            lifecycle_lock(c)
            where,args=scope_filter(p,'m')
            columns='''a.*,m.provider_id AS provider_message_id,
                (SELECT platform FROM accounts WHERE id=m.account) AS provider_platform,
                (SELECT metadata->>'media_kind' FROM message_metadata
                    WHERE message=m.id AND revision=m.revision) AS media_kind'''
            if active is None:
                row = c.execute('SELECT '+columns+''' FROM download_grants g
                    JOIN attachments a ON a.id=g.attachment JOIN messages m ON m.id=a.message
                    WHERE g.digest=%s AND g.expires_at>clock_timestamp() AND g.key_digest=%s
                    AND g.scope_version=%s AND a.active AND NOT m.deleted AND '''+where,
                    [digest(grant),p['key_digest'],p['scope_version'],*args]).fetchone()
            elif active[1]!=p['scope_version']:row=None
            else:
                # The expired grant row may already be deleted; its admitted
                # binding carries the same key, scope version and attachment.
                row = c.execute('SELECT '+columns+''' FROM attachments a JOIN messages m ON m.id=a.message
                    WHERE a.id=%s AND a.active AND NOT m.deleted AND '''+where,
                    [active[2],*args]).fetchone()
            if not row:
                raise APIError('not_found', 404)
            path = self.files/row['digest']
            if path.is_symlink() or not path.is_file() or path.stat().st_size != row['bytes']:
                raise APIError('attachment_unavailable', 503)
            touch_original(c,row['digest'],row['bytes'])
            targets(p,[row['id']])
            return row, path, (p['key_digest'],p['scope_version'],row['id'])

    def get_updates(self, token, consumer, limit=20, *, accounts=None,chats=None,senders=None,
                    origins=None,kinds=None,direction='',from_time='',to_time='',compact=False,
                    body_chars=None,include_coverage=True,exclude_channels=False,order='event',
                    reset_filters=False,expected_ack_version=None,start_from='beginning'):
        from .update_queries import get_updates
        with self.authorized(token,'get_updates') as (c,p):
            return get_updates(self,c,p,consumer,limit,accounts=accounts,chats=chats,senders=senders,
                origins=origins,kinds=kinds,direction=direction,from_time=from_time,to_time=to_time,
                compact=compact,body_chars=body_chars,include_coverage=include_coverage,
                exclude_channels=exclude_channels,order=order,reset_filters=reset_filters,
                expected_ack_version=expected_ack_version,start_from=start_from)

    def create_subscription(self, token, chat_ids, *, sender_ids=None, kinds=None,
                            direction='incoming', origins=None, label='', expires_at=None, wake_rules=None, reply_to_send=None):
        from .subscriptions import create_subscription
        with self.authorized(token, 'create_subscription') as (c, p):
            return create_subscription(self, c, p, chat_ids, sender_ids=sender_ids, kinds=kinds,
                direction=direction, origins=origins, label=label, expires_at=expires_at, wake_rules=wake_rules, reply_to_send=reply_to_send)

    def list_subscriptions(self, token, *, limit=50):
        from .subscriptions import list_subscriptions
        with self.authorized(token, 'list_subscriptions') as (c, p):
            return list_subscriptions(self, c, p, limit=limit)

    def get_subscription(self, token, subscription_id):
        from .subscriptions import get_subscription
        with self.authorized(token, 'get_subscription') as (c, p):
            return get_subscription(self, c, p, subscription_id)

    def update_subscription(self, token, subscription_id, version, *, action, resume_policy='catch_up'):
        from .subscriptions import update_subscription
        with self.authorized(token, 'update_subscription') as (c, p):
            return update_subscription(self, c, p, subscription_id, version,
                action=action, resume_policy=resume_policy)

    def get_subscription_events(self, token, subscription_id, *, after=0, limit=50):
        from .subscriptions import get_subscription_events
        with self.authorized(token, 'get_subscription_events') as (c, p):
            return get_subscription_events(self, c, p, subscription_id, after=after, limit=limit)

    def fetch_subscription_events(self, token, subscription_id, *, limit=50):
        from .subscription_consumption import fetch_subscription_events
        with self.authorized(token, 'fetch_subscription_events') as (c, p):
            return fetch_subscription_events(self, c, p, subscription_id, limit=limit)

    def ack_subscription_events(self, token, subscription_id, receipt, version):
        from .subscription_consumption import ack_subscription_events
        with self.authorized(token, 'ack_subscription_events') as (c, p):
            return ack_subscription_events(self, c, p, subscription_id, receipt, version)

    def configure_subscription_delivery(self, token, subscription_id, version, receiver_id, delivery_policy=None):
        from .event_delivery import configure_subscription_delivery
        with self.authorized(token, 'configure_subscription_delivery') as (c, p):
            return configure_subscription_delivery(self, c, p, subscription_id, version, receiver_id, delivery_policy)

    def test_subscription(self, token, subscription_id, request_id):
        from .event_delivery import test_subscription
        with self.authorized(token, 'test_subscription') as (c, p):
            return test_subscription(self, c, p, subscription_id, request_id)

    def submit_feedback(self, token, request_id, category, description, *, operation=None, refs=None):
        from .feedback import submit_feedback
        with self.authorized(token, 'submit_feedback') as (c, p):
            return submit_feedback(self, c, p, request_id, category, description, operation=operation, refs=refs)

    def list_feedback(self, token, *, limit=50, status=None):
        from .feedback import list_feedback
        with self.authorized(token, 'list_feedback') as (c, p):
            return list_feedback(self, c, p, limit=limit, status=status)

    def export_feedback(self, **options):
        # Operator CLI only (ANYM-265): every principal's feedback, read-only; no MCP tool, REST route or gateway action.
        from .feedback import export_feedback
        with self.pool.connection() as c, c.transaction():
            return export_feedback(c, **options)

    def set_feedback_status(self, ids, status, *, fixed_in=None, spec_ref=None, duplicate_of=None, note=None,
                            unchanged_since=None, not_after=None):
        # Operator CLI only (ANYM-343): no operation permission, MCP tool, REST route or gateway action.
        from .feedback import set_feedback_status
        with self.pool.connection() as c:
            return set_feedback_status(c, ids, status, fixed_in=fixed_in, spec_ref=spec_ref,
                duplicate_of=duplicate_of, note=note, unchanged_since=unchanged_since, not_after=not_after)

    # Operator CLI only (ANYM-283): owner-made person links; no operation permission, MCP tool,
    # REST route or gateway action, and the gateway role has no write grant on person_links.
    def link_person(self, account, sender, person=None):
        from .person_links import link
        with self.pool.connection() as c:
            return link(c, account, sender, person)

    def unlink_person(self, account, sender):
        from .person_links import unlink
        with self.pool.connection() as c:
            return unlink(c, account, sender)

    def delete_person(self, person):
        from .person_links import delete
        with self.pool.connection() as c:
            return delete(c, person)

    def list_people(self, person=None):
        from .person_links import list_people
        with self.pool.connection() as c:
            return list_people(c, person)

    def ack_updates(self, token, consumer, receipt, expected_version):
        with self.authorized(token,'ack_updates') as (c, p):
            if not isinstance(expected_version,int) or isinstance(expected_version,bool) or expected_version<0:
                raise APIError('invalid_version')
            if not isinstance(receipt,str) or len(receipt)>128:raise APIError('invalid_receipt')
            if not isinstance(consumer,str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',consumer):
                raise APIError('invalid_consumer')
            # Same lock order for GET and ACK; serialize concurrent ACKs of different receipts.
            state = c.execute('''SELECT * FROM consumers WHERE principal=%s AND name=%s AND scope_version=%s
                FOR UPDATE''', (p['id'], consumer, p['scope_version'])).fetchone()
            row = c.execute('''SELECT * FROM deliveries WHERE digest=%s AND principal=%s
                AND consumer=%s AND scope_version=%s AND expires_at>now()''',
                (digest(receipt), p['id'], consumer, p['scope_version'])).fetchone()
            if not state or not row or row['expected_version'] != expected_version:
                raise APIError('invalid_receipt', 409)
            if row['acked']:
                return {'acked': True, 'replayed': True, 'ack_version': state['version']}
            if state['version']!=expected_version or state['position']!=row['start_position']:
                raise APIError('stale_ack', 409)
            c.execute('''UPDATE consumers SET position=%s,version=version+1
                WHERE principal=%s AND name=%s AND scope_version=%s''',
                (row['end_position'], p['id'], consumer, p['scope_version']))
            c.execute('UPDATE deliveries SET acked=true WHERE digest=%s', (digest(receipt),))
            return {'acked': True, 'replayed': False, 'ack_version': expected_version+1}

    def release_retention_hold(self, subscription_ids=None):
        # Operator CLI only (ANYM-446): reviewed restored definitions become prunable again.
        from uuid import UUID
        ids = None if subscription_ids is None else sorted({str(UUID(value)) for value in subscription_ids})
        if ids is not None and not 1 <= len(ids) <= 1000:
            raise ValueError('invalid_subscription_ids')
        with self.pool.connection() as c:
            released = c.execute('''UPDATE subscriptions SET retention_hold=false
                WHERE retention_hold AND (%s::uuid[] IS NULL OR id=ANY(%s::uuid[])) RETURNING id''',
                (ids, ids)).fetchall()
        return {'released': len(released)}

    def set_subscription_retention(self, enabled):
        # Operator CLI only (ANYM-446): enable after every serving reader reports retention_gap.
        from .subscriptions import lock_retention
        with self.pool.connection() as c:
            # Waits for in-flight pruning batches; every later batch reads this value.
            lock_retention(c, shared=False)
            version = int(c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'])
            if version < 40:
                raise RuntimeError('subscription_retention_needs_schema_40')
            c.execute('''INSERT INTO probe_metadata(name,value) VALUES('subscription_retention',%s)
                ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value''', ('enabled' if enabled else 'disabled',))
        return {'subscription_retention': 'enabled' if enabled else 'disabled'}

    def migrate(self):
        with self.pool.connection() as c:
            c.execute('SELECT pg_advisory_xact_lock(74509137)')
            c.execute("SET LOCAL statement_timeout='180s'")
            c.execute(Path(__file__).with_name('schema.sql').read_text())
            version=int(c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'])
            if version>43: raise RuntimeError('database_newer_than_runtime')
            for target,name in [(2,'002_archive.sql'),(3,'003_collectors.sql'),(4,'004_whatsapp.sql'),(5,'005_access.sql'),(6,'006_reviews.sql'),(7,'007_search_indexes.sql'),(8,'008_people.sql'),(9,'009_file_index.sql'),(10,'010_search_audit.sql'),(11,'011_telegram_window.sql'),(12,'012_search.sql'),(13,'013_file_search.sql'),(14,'014_collector_health.sql'),(15,'015_enrichment.sql'),(16,'016_file_content.sql'),(17,'017_chat_evidence.sql'),(18,'018_collector_operations.sql'),(19,'019_whatsapp_identities.sql'),(20,'020_processing_admission.sql'),(21,'021_media_storage.sql'),(22,'022_outgoing.sql'),(23,'023_receipts.sql'),(24,'024_formatting.sql'),(25,'025_operations.sql'),(26,'026_subscriptions.sql'),(27,'027_reply_queue.sql'),(28,'028_send_reference.sql'),(29,'029_file_uploads.sql'),(30,'030_whatsapp_native_identities.sql'),(31,'031_media_requests.sql'),(32,'032_event_delivery.sql'),(33,'033_file_lifecycle.sql'),(34,'034_file_understanding.sql'),(35,'035_cold_archive.sql'),(36,'036_extend_key_audit.sql'),(37,'037_narrow_worker_roles.sql'),(38,'038_hint_policy.sql'),(39,'039_feedback_status.sql'),(40,'040_subscription_retention.sql'),(41,'041_reply_queue_controls.sql'),(42,'042_read_state_freshness.sql'),(43,'043_person_links.sql')]:
                migration=Path(__file__).with_name('migrations')/name
                sha=hashlib.sha256(migration.read_bytes()).hexdigest()
                if version<target:
                    c.execute(migration.read_text())
                    if target==17:
                        from .chat_evidence import backfill
                        backfill(c)
                    c.execute('INSERT INTO archive_migrations(version,sha256) VALUES(%s,%s)',(target,sha))
                    c.execute("UPDATE probe_metadata SET value=%s WHERE name='schema_version'",(str(target),))
                else:
                    applied=c.execute('SELECT sha256 FROM archive_migrations WHERE version=%s',(target,)).fetchone()
                    if not applied or applied['sha256']!=sha: raise RuntimeError('migration_checksum_mismatch')
            # Restores run without ACLs and reopen new functions to PUBLIC (ANYM-254).
            c.execute('''DO $$ BEGIN
                IF pg_has_role((SELECT proowner FROM pg_proc WHERE oid=to_regprocedure(
                    'public.publish_processing_event(text[],text,text,text,text,jsonb)')),'USAGE') THEN
                 REVOKE ALL ON FUNCTION public.publish_processing_event(text[],text,text,text,text,jsonb),
                  public.publish_file_text_event(text,text,text,text,jsonb),
                  public.publish_file_enrichment_event(text,text,text,text,jsonb) FROM PUBLIC;
                END IF;
            END $$''')

    def issue_key(self, principal, accounts, days=7, *, chats=UNSET, permissions=UNSET,
                  key_permissions=UNSET):
        principal_id(principal)
        if type(days) is not int or not 1<=days<=30:
            raise ValueError('invalid_key_lifetime')
        token = 'pos_probe_'+secrets.token_urlsafe(32)
        with self.pool.connection() as c:
            lock_principal(c,principal)
            old = c.execute('SELECT * FROM principals WHERE id=%s FOR UPDATE', (principal,)).fetchone()
            if chats is UNSET:chats=old['chat_ids'] if old else None
            if permissions is UNSET:permissions=old['permissions'] if old else DEFAULT_PERMISSIONS
            accounts,chats,permissions=normalize_policy(c,accounts,chats,permissions)
            implicit_ceiling=key_permissions is UNSET
            if implicit_ceiling:key_permissions=sorted(LEGACY_PERMISSIONS|set(permissions))
            key_permissions=normalize_list(key_permissions,maximum=len(PERMISSIONS))
            if set(key_permissions)-(set(permissions)|(LEGACY_PERMISSIONS if implicit_ceiling else set())):
                raise ValueError('invalid_key_permissions')
            self._write_policy(c,principal,old,accounts,chats,permissions,old['enabled'] if old else True)
            issued=c.execute('INSERT INTO api_keys(digest,principal,expires_at,permissions) VALUES(%s,%s,%s,%s) RETURNING id',
                (digest(token),principal,datetime.now(timezone.utc)+timedelta(days=days),key_permissions)).fetchone()
            audit(c,{'id':principal,'key_id':issued['id']},'issue_key','ok')
        return token

    def set_key_permissions(self,credential_id,permissions):
        credential_id=key_id(credential_id)
        permissions=normalize_list(permissions,maximum=len(PERMISSIONS))
        if set(permissions)-PERMISSIONS:raise ValueError('invalid_scope')
        with self.pool.connection() as c:
            known=c.execute('SELECT principal FROM api_keys WHERE id=%s',(credential_id,)).fetchone()
            if not known:raise ValueError('unknown_key')
            lock_principal(c,known['principal'])
            principal=c.execute('SELECT permissions FROM principals WHERE id=%s',
                                (known['principal'],)).fetchone()
            if set(permissions)-set(principal['permissions']):raise ValueError('invalid_key_permissions')
            row=c.execute('''UPDATE api_keys SET permissions=%s,
                permission_version=permission_version+1 WHERE id=%s
                RETURNING id,principal,permissions,permission_version''',
                (permissions,credential_id)).fetchone()
            audit(c,{'id':known['principal'],'key_id':credential_id},'set_key_permissions','ok')
            return serial(row)

    @staticmethod
    def _write_policy(c,principal,old,accounts,chats,permissions,enabled):
        if old:
            changed=(set(old['accounts'])!=set(accounts) or old['chat_ids']!=chats or
                     set(old['permissions'])!=set(permissions) or old['enabled']!=enabled)
            if changed:
                c.execute('''UPDATE principals SET accounts=%s,chat_ids=%s,permissions=%s,enabled=%s,
                    scope_version=scope_version+1 WHERE id=%s''',(accounts,chats,permissions,enabled,principal))
        else:
            c.execute('INSERT INTO principals(id,accounts,chat_ids,permissions,enabled) VALUES(%s,%s,%s,%s,%s)',
                      (principal,accounts,chats,permissions,enabled))

    def set_access(self,principal,*,accounts=UNSET,chats=UNSET,permissions=UNSET,enabled=UNSET):
        principal_id(principal)
        with self.pool.connection() as c:
            lock_principal(c,principal)
            old=c.execute('SELECT * FROM principals WHERE id=%s FOR UPDATE',(principal,)).fetchone()
            if not old:raise ValueError('unknown_principal')
            if accounts is UNSET:accounts=old['accounts']
            if chats is UNSET:chats=old['chat_ids']
            if permissions is UNSET:permissions=old['permissions']
            if enabled is UNSET:enabled=old['enabled']
            if type(enabled) is not bool:raise ValueError('invalid_enabled')
            accounts,chats,permissions=normalize_policy(c,accounts,chats,permissions)
            self._write_policy(c,principal,old,accounts,chats,permissions,enabled)
            audit(c,{'id':principal},'set_access','ok')
            return serial(c.execute('SELECT * FROM principals WHERE id=%s',(principal,)).fetchone())

    def revoke(self, token=None, *, credential_id=None):
        if (token is None)==(credential_id is None):raise ValueError('one_key_selector_required')
        if token is not None:
            if not isinstance(token,str) or not 32<=len(token)<=256:raise ValueError('invalid_key')
            clause,selector='digest=%s',digest(token)
        else:clause,selector='id=%s',key_id(credential_id)
        with self.pool.connection() as c:
            row=c.execute('SELECT principal,id FROM api_keys WHERE '+clause,(selector,)).fetchone()
            if not row:return 0
            lock_principal(c,row['principal'])
            changed=c.execute('UPDATE api_keys SET revoked=true WHERE '+clause,(selector,)).rowcount
            audit(c,{'id':row['principal'],'key_id':row['id']},'revoke_key','ok')
            return changed

    def extend_key(self,credential_id,days):
        # Renewal in place (ANYM-422): token, digest, permissions and scope stay unchanged.
        credential_id=key_id(credential_id)
        if type(days) is not int or not 1<=days<=183:raise ValueError('invalid_key_lifetime')
        with self.pool.connection() as c:
            known=c.execute('SELECT principal FROM api_keys WHERE id=%s',(credential_id,)).fetchone()
            if not known:raise ValueError('unknown_key')
            lock_principal(c,known['principal'])
            # The clock is read after the lock, as authentication does.
            row=c.execute('''SELECT id,principal,revoked,expires_at,clock_timestamp() AS now,
                clock_timestamp()+make_interval(days=>%s) AS extended FROM api_keys WHERE id=%s FOR UPDATE''',
                (days,credential_id)).fetchone()
            if row['revoked']:raise ValueError('key_revoked')
            if row['expires_at']<=row['now']:raise ValueError('key_expired')
            if row['extended']<row['expires_at']:raise ValueError('key_lifetime_would_shorten')
            c.execute('UPDATE api_keys SET expires_at=%s WHERE id=%s',(row['extended'],credential_id))
            audit(c,{'id':row['principal'],'key_id':row['id']},'extend_key','ok')
            return serial({'key_id':row['id'],'previous_expires_at':row['expires_at'],'expires_at':row['extended']})

    def list_keys(self,principal):
        principal_id(principal)
        with self.pool.connection() as c:
            return serial(c.execute('SELECT id,principal,expires_at,revoked,permissions,permission_version FROM api_keys WHERE principal=%s ORDER BY expires_at,id',
                                    (principal,)).fetchall())

    def audit_tail(self,principal='',limit=50):
        bounded(limit)
        if principal:principal_id(principal)
        with self.pool.connection() as c:
            return serial(c.execute('''SELECT * FROM audit_log WHERE (%s='' OR principal=%s)
                ORDER BY occurred_at DESC,id DESC LIMIT %s''',(principal,principal,limit)).fetchall())

    @staticmethod
    def event_snapshot(c, account, message, snapshot_metadata=None):
        row=c.execute('''SELECT id AS message,account,chat,sender,provider_id,body,revision,sent_at,deleted,direction
            FROM messages WHERE id=%s AND account=%s''',(message,account)).fetchone()
        if not row: raise ValueError('invalid_event_message')
        if snapshot_metadata is not None:
            if (not isinstance(snapshot_metadata,dict) or set(snapshot_metadata)-{'processing','reply_evidence'} or
                    len(json.dumps(snapshot_metadata,ensure_ascii=False).encode())>16384):
                raise ValueError('invalid_event_metadata')
            if 'reply_evidence' in snapshot_metadata:
                from .reply_evidence import validate as validate_reply_evidence
                validate_reply_evidence(snapshot_metadata['reply_evidence'])
            row.update(snapshot_metadata)
        from .result_evidence import formatting_evidence
        formatting_evidence(c,[row])
        return serial(row)

    @staticmethod
    def append_event(c, account, message, kind='message.created', origin='fixture', event_key=None,
                     snapshot_metadata=None, publisher=None):
        from psycopg.types.json import Jsonb
        row=Store.event_snapshot(c, account, message, snapshot_metadata)
        if publisher is not None and publisher not in ('publish_file_text_event','publish_file_enrichment_event'):
            raise ValueError('invalid_event_publisher')
        if publisher and c.execute(
                "SELECT coalesce(has_function_privilege(to_regprocedure(%s),'EXECUTE'),false) AS ok",
                ('public.'+publisher+'(text,text,text,text,jsonb)',)).fetchone()['ok']:
            # Schema 37 file workers: a fenced function re-checks the snapshot, kind and
            # stage, derives the origin and alone writes the journal and feed counter.
            # Until the reapplied template grants it (schema 36, or 37 with the previous
            # grants) the worker keeps the direct write those grants allow.
            return c.execute('SELECT public.'+publisher+'(%s,%s,%s,%s,%s) AS id',
                (account,message,kind,event_key,Jsonb(row))).fetchone()['id']
        value = c.execute('UPDATE feed_counter SET value=value+1 WHERE id=1 RETURNING value').fetchone()['value']
        c.execute('''INSERT INTO events(id,account,message,kind,revision,origin,snapshot,event_key)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
            (value,account,message,kind,row['revision'],origin,Jsonb(row),event_key))
        return value

    def seed(self):
        self.files.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = b'PERSONAL OS SYNTHETIC PROBE\nFile integrity check: lighthouse-42\nNo personal data.\n'
        sha = hashlib.sha256(content).hexdigest()
        target = self.files/sha
        if not target.exists():
            temporary = self.files/('pending-'+secrets.token_hex(8))
            fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(fd,'wb') as f:
                f.write(content); f.flush(); os.fsync(f.fileno())
            os.replace(temporary,target)
            fd = os.open(self.files, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        if hashlib.sha256(target.read_bytes()).hexdigest()!=sha:
            raise ValueError('Fixture file corrupt')
        with self.pool.connection() as c:
            c.execute('SELECT value FROM feed_counter WHERE id=1 FOR UPDATE')
            for aid, platform in [('telegram-demo','telegram'),('whatsapp-demo-1','whatsapp'),('whatsapp-demo-2','whatsapp')]:
                c.execute('INSERT INTO accounts(id,platform,label) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (aid,platform,'Synthetic '+aid))
                c.execute('INSERT INTO chats VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (aid+'-chat',aid,'Synthetic chat'))
            for mid, aid, text, sender, day in [
                ('demo-tg-1','telegram-demo','Тестовый проект Маяк: встреча во вторник. Это вымышленные данные.','demo-person-1',1),
                ('demo-tg-2','telegram-demo','Файл lighthouse-42 приложен для проверки скачивания.','demo-person-2',2),
                ('demo-wa-1','whatsapp-demo-1','Synthetic project Atlas: budget review and planning.','demo-person-1',3),
                ('demo-private-1','whatsapp-demo-2','PRIVATE_SCOPE_SENTINEL synthetic hidden message.','demo-person-3',4)]:
                inserted = c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id''',
                    (mid,aid,aid+'-chat',mid,sender,text,datetime(2026,10,day,12,tzinfo=timezone.utc))).fetchone()
                if inserted:
                    eid=self.append_event(c, aid, mid)
                    c.execute('''INSERT INTO message_revisions(message,revision,snapshot)
                        SELECT message,revision,snapshot FROM events WHERE id=%s''',(eid,))
            c.execute('INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                      ('demo-file-1','demo-tg-2',sha,'probe-note.txt','text/plain',len(content)))
