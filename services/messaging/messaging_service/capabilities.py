"""The capabilities payload of the messaging service: the full document for one credential, its sections
and the summary index. Store.capabilities delegates here. STARTED_AT and build() stay in store.py, which
tests patch; this module reads them through the store module at call time."""
from datetime import timezone

from . import __version__
from .field_errors import refusal
from .policy import LEGACY_PERMISSIONS, OPERATION_PERMISSIONS


# capabilities sections (ANYM-270): each key of the full payload sits in exactly one
# named block, with today's shape; summary is the short index an agent reads first.
CAPABILITY_SECTIONS={
    'identity':('service','version','build','principal','scope_version','credential_id','permissions','expires_at','key_permission_ceiling',
                'key_permission_version','chat_scope','auth_mode'),
    'accounts':('accounts',),'coverage':('synthetic','sources','coverage','file_policy'),
    'tool_manifest':('tool_manifest','available'),'search_options':('search_options',),'sending':('sending',),
    'file_uploads':('file_uploads',),'subscriptions':('subscriptions','events'),'feedback':('feedback',),
    'messenger_operations':('messenger_operations',),'limits':('limits','unavailable')}


def capability_view(full, detail='full', sections=None, permitted=None):
    """`full` unchanged, only the named `sections`, or the summary index. A section lists
    an account's operations only where `permitted` (the key may use them there) names them."""
    from .store import APIError
    if sections is not None:
        if not sections:
            raise refusal('invalid_filter',[('sections','min_items')],hint='omit sections to get all sections')
        if any(name not in CAPABILITY_SECTIONS for name in sections):
            raise refusal('invalid_filter',[('sections','enum',None,tuple(CAPABILITY_SECTIONS))])
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
    return {'service':full['service'],'version':full['version'],'build':full['build'],'principal':full['principal'],
        'credential_id':full['credential_id'],'permissions':full['permissions'],'expires_at':full['expires_at'],
        # ANYM-470: the key's expiry and each owner's ID stay in the default view.
        'accounts':[{k:a[k] for k in ('id','platform','label','self_provider_id')} for a in full['accounts']],
        'chat_scope':{'all_in_permitted_accounts':scope['all_in_permitted_accounts'],
                      'chat_count':None if scope['chat_ids'] is None else len(scope['chat_ids'])},
        'tool_manifest':{'version':full['tool_manifest']['version']},
        'detail':'summary','sections':list(CAPABILITY_SECTIONS),
        'next_action':'call capabilities again with sections=[names] for those blocks or detail=full for everything'}


def _file_policy(c, p, account_ids):
    """ANYM-812: each permitted account's collection policy, from the run's published window and the enabled
    configured recognition kinds only (no queue query); None for a chat-scoped key (account-wide facts)."""
    if p['chat_ids'] is not None:
        return None
    from .coverage import CONFIGURED_DAYS,policy
    from .media_policy import published_window
    rows=c.execute('''SELECT a.id,a.platform,coalesce(mp.published,false) AS published,mp.days,mp.since,
            ARRAY(SELECT pp.kind FROM processing_profiles pp WHERE pp.account=a.id
                AND pp.observed_at>=clock_timestamp()-make_interval(days=>%s) ORDER BY pp.kind) AS kinds
        FROM accounts a '''+published_window('a.id')+''' WHERE a.id=ANY(%s) ORDER BY a.id''',(CONFIGURED_DAYS,account_ids)).fetchall()
    return {r['id']:{**policy(r['platform'],r['since'].isoformat() if r['since'] else None,
            int(r['days']) if r['published'] and r['days'] is not None else None,r['kinds']),
        'published':r['published'],'status_call':'get_sync_status'} for r in rows}


def capabilities(self, token, detail='full', sections=None):
    from . import store as _store
    # Python callers keep today's payload; MCP asks for the summary by default (ANYM-270).
    with self.authorized(token,'capabilities') as (c, p):
        from .messenger_operations import CATALOG_VERSION,OPERATIONS,operation_manifest
        from .event_delivery import delivery_transports
        from .commands import manifest
        dataset=self.dataset(c,p)
        file_policy=_file_policy(c,p,[r['id'] for r in dataset['sources']])
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
        features={'read':['search','history','context','updates','reviews','search_people','search_files','get_file_text','get_file_occurrences','get_sync_status','get_file_segments','resolve_reference','resolve_target','preview_message'], 'download':['attachment','request_file_download'], 'cursor-write':['ack'], 'send':['send_message','messages_send','get_send_status']}
        if 'read' in p['permissions']:
            features['review-write']=['record_review']
            features['cursor-write'].append('dismiss_reply')
            features['cursor-write'].extend(['snooze_reply','restore_reply','mute_reply_source','unmute_reply_source'])
            features['read'].append('list_reply_dismissals')
            features['read'].append('list_negotiations')
            features['read'].extend(['list_subscriptions','get_subscription','get_subscription_events','fetch_subscription_events','wait_for_subscription_events','list_feedback'])
            features['subscription-ack']=['ack_subscription_events']
            features['feedback-write']=['submit_feedback']
            features['subscription-write']=['create_subscription','update_subscription','configure_subscription_delivery','test_subscription']
            from .delivery_endpoints import TOOLS as delivery_tools
            features['delivery-write']=list(delivery_tools)
        available=[feature for permission,names in features.items() if permission in p['permissions'] for feature in names]
        if {'read','send','upload'}<=set(p['permissions']):
            available.extend(['prepare_file_upload','upload_file_chunk','get_file_upload'])
        operation_tools=['list_messenger_operations','preview_messenger_operation',
                         'execute_messenger_operation','get_messenger_operation']
        if any(spec['permission'] in p['permissions'] for spec in OPERATIONS.values()):
            available.extend(operation_tools)
        else:available.append('list_messenger_operations')
        return capability_view({'service': 'anymsg', 'version': __version__, 'build': _store.build(), **dataset,
            **({'file_policy':file_policy} if file_policy is not None else {}),
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
                'query_modes':['all','any','phrase','web','exact'],'orders':['asc','desc','relevance'],
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
                'message_media_text':{'parameter':'include_media_text','default':True,'sources':['extracted','transcript'],
                    'query_modes':['all','any','phrase','web'],'max_chars_per_attachment':32000,
                    'row_fields':['matched_in','media_matches']},
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
                # ANYM-828: per-hit match arms (filename vs caption vs the file's own text) and whether the text was searched.
                'file_hit_fields':{'matched_in':['filename','caption','extracted','transcript','description'],
                    'content_searched':'ready_non_empty_text_or_ready_voice_transcript','content_pending_cap':20,
                    'matched_in_unverified':'only_when_the_page_probe_is_cancelled',
                    'excerpt_sources':['filename','caption','extracted','transcript','description']},
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
                # ANYM-825: dynamic sources may keep only some chat types; WhatsApp chats are untyped (unknown).
                'source_chat_types':{'field':'sources.chat_types','values':['private','group','channel','unknown'],
                    'untyped':'unknown','disabled_error':'source_filters_disabled'},
                # ANYM-830: chats a dynamic definition leaves out, replaced in place (update_subscription).
                'source_exclusions':{'field':'sources.exclude_chats','max_pairs':40,'max_bytes':6000,
                    'update_action':'set_exclusions','revision_field':'sources.exclusions_revision',
                    'keeps_version':True,'disabled_error':'source_exclusions_disabled'},
                # ANYM-824: get_subscription.activity, the matcher's per-day counts (numbers only, SUBSCRIPTIONS.md).
                'activity':{'field':'activity','operation':'get_subscription','days':7,'day_basis':'utc',
                    'labels':['platform','metric'],'scope':'current_scope_version_only','retention_days':35,
                    'metrics':['scanned','matched','left_out_kind','left_out_origin','left_out_chat_type',
                        'left_out_channel','left_out_bot','left_out_direction','left_out_sender','left_out_wake_rule',
                        'match_budget_blocked','matched_failure','reads_compact','left_out_excluded_chat'],
                    'counts_model_runs':False,'cost_estimate':False},
                'event_selection':'structural_exact_revision','origin_default':'live',
                'mutation_permission':'subscription-write','read_permission_required':True,
                'ownership':'originating_credential_and_principal','new_permission_inherited':False,
                'source_scope_change':'masked_access_changed_recreate_required',
                'ack':True,'ack_permission':'subscription-ack','push':'operator_registered_signed_hints','wait_max_seconds':25,'verified_host':False,'native_reply_preset':True,
                'provider_polling':False,'start_boundary':'committed_archive_frontier',
                'existing_update_consumers_unchanged':True,'transports':delivery_transports(c,p),
                'delivery_policy':{'tool':'configure_subscription_delivery','argument':'delivery_policy',
                    'changes':'hint_timing_only','changes_matches_ack_or_reads':False,'requires_receiver':True,
                    'bounds':{'debounce_s':[0,600],'max_wait_s':[1,3600],'batch_window_s':[0,86400],
                        'batch_count':[1,1000],'expect_reply_within_s':[60,1209600],'quiet_windows':[1,4]},
                    'debounce_and_batch_exclusive':True,'max_wait_default':'max(300,debounce_s)',
                    'quiet_hours':'iana_timezone_weekday_windows_held_not_dropped',
                    'quiet_bypass':['incoming_message','owner_mention','reply_to_owner','reply_to_confirmed_send','silence'],
                    'silence_timer':'reply_to_send_subscriptions_only','silence_hint_kind':'silence',
                    'key_budget':{'setting':'SUBSCRIPTION_MAX_HINTS_PER_HOUR','default':200,'bounds':[1,1000],
                        'scope':'every_hint_of_the_key','applies_without_policy':True,'over_budget':'coalesced_never_dropped'},
                    'held_reasons':['debounce','batch','quiet_hours','budget'],'clock':'database_utc',
                    'idempotency':'one_hint_per_subscription_version_window'}},
            'events':{'fetch':'fetch_subscription_events','ack':'ack_subscription_events','wait':'wait_for_subscription_events','wait_max_seconds':25,'page_detail':['full','compact'],'page_detail_default':'full','page_include':['subscription','delivery','metadata','reply_evidence'],'positions':['acked_through','fetched_through','open_receipts','oldest_open_receipt_expires_at','expired_unacked'],'test':'test_subscription','delivery':'operator_registered_signed_hints','host_turn_verified':False},
            'feedback':{'implemented':True,'write_permission':'feedback-write','ownership':'exact_credential','external_delivery':False},
            # ANYM-464: Telegram holds a scheduled send (send_message, messages_send, send_file schedule_at).
            'sending':{'implemented':True,'wait_max_seconds':25,'approval_mode':'acting_agent_obtains_exact_owner_confirmation',
                'schedules':['telegram'],
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
                # ANYM-532: chat_id or phone; a phone preview does one open_chat_by_phone lookup.
                'recipients':{'chat_id':{'preview_contacts_provider':False},
                    'phone':{'platforms':['whatsapp_native'],'preview_contacts_provider':True,
                        'requires':['send','full_account','open_chat_by_phone','gateway_action_grant'],
                        'send_requires_preview_digest':True,'digest_binds_identity':True,
                        'lookup_quota':'shared_with_open_chat_by_phone'},
                    # ANYM-643: a username preview resolves it at Telegram; the send rechecks the user ID.
                    'username':{'platforms':['telegram'],'preview_contacts_provider':True,
                        'requires':['send','full_account','open_chat_by_username','gateway_action_grant'],
                        'send_requires_preview_digest':True,'digest_binds_identity':True,
                        'send_rechecks_identity':True,'lookup_quota':'shared_with_open_chat_by_username'}},
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
                'read_state_requires_explicit_permission':True,
                # ANYM-549: execute takes wait_s up to this bound; preview takes and ignores request_id.
                'execute_wait_s_max':25,'preview_accepts_request_id':True},
            'unavailable': ['guaranteed_full_history', 'drafts',
                            'owner_approval', 'project_memory', 'oauth'],
            'limits': {'page_size': 100, 'query_chars': 200,
                       'download_grant_seconds': 120, 'delivery_seconds': 900,
                       'review_sources':20, 'context_reviews':5,'send_text_utf16_units':4000,
                       'format_source_utf16_units':16000,'http_json_body_bytes':16384,
                       'upload_chunk_http_json_body_bytes':360448,
                       'send_queue_ttl_seconds':600,'send_per_account_per_minute':60}},detail,sections,permitted)
