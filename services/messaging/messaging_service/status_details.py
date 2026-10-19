"""Shared safe vocabulary for collector and archive observations."""
from datetime import datetime,timezone

DETAILS={
    'version_conflict':('Conflicting provider observations; current archive revision is retained. Read the message context and scoped sync evidence.','get_context'),
    'coverage_not_reconciled':('Archived observations do not establish complete provider history.','get_sync_status_for_chat'),
    'whatsapp_catchup_gap':('Provider traversal did not reach a previous checkpoint. Live collection may still work.','inspect_chat_gaps'),
    'whatsapp_history_partial':('WhatsApp history remains a partial provider traversal.','get_sync_status_for_chat'),
    'telegram_history_partial':('Telegram history traversal is in progress or limited.','get_sync_status_for_chat'),
    'telegram_history':('Telegram is traversing archived history.','get_sync_status_for_chat'),
    'telegram_discovery':('Telegram chat catalogue discovery is in progress.','wait_for_discovery'),
    'rate_limited':('Provider requested a cooldown; do not bypass it.','wait_for_provider_cooldown'),
    'configured_chat_unavailable':('At least one configured chat was unavailable.','check_provider_access'),
    'provider_temporarily_unavailable':('A provider operation failed; collection will retry.','check_collector_health'),
    'dialog_discovery_limited':('Chat discovery reached its configured bound.','inspect_catalog_coverage'),
    'whatsapp_chat_unavailable':('Some WhatsApp history operations failed; live capture is independent.','inspect_chat_gaps'),
    'provider_disconnected':('The collector transport closed; bounded retry is scheduled.','wait_for_retry'),
    'provider_not_ready':('The provider has not established a usable connection.','wait_for_retry'),
    'provider_initialization_failed':('The provider failed during startup.','wait_for_retry'),
    'provider_connect_timeout':('Connection exceeded its bounded startup deadline.','wait_for_retry'),
    'provider_page_stalled':('A browser history operation stalled; the owned child is restarted.','wait_for_retry'),
    'whatsapp_logged_out':('WhatsApp disconnected this linked session. Automatic relinking is disabled.','relink_in_owner_terminal'),
    'whatsapp_pairing_required':('WhatsApp requires owner pairing; collector connection is blocked.','relink_in_owner_terminal'),
    'provider_auth_failed':('WhatsApp rejected the linked session; collector connection is blocked.','relink_in_owner_terminal'),
    'whatsapp_identity_mismatch':('The linked account differs from the configured identity.','check_configured_account_identity'),
    'media_timeout':('The media transfer exceeded its deadline; bounded retry is scheduled.','wait_for_media_retry'),
    'media_network_error':('The media CDN request failed; bounded retry is scheduled.','wait_for_media_retry'),
    'media_metadata_unavailable':('The provider did not supply usable media metadata.','wait_for_media_retry'),
    'media_unavailable':('The provider no longer exposes this original.','inspect_source_attachment'),
    'media_reference_changed':('The current provider attachment differs from the queued reference.','reread_message'),
    'invalid_media_path':('The provider media path failed the fixed-host validation.','check_collector_health'),
    'media_size_limit':('The original exceeds the configured download size bound.','inspect_download_limits'),
    'media_hash_mismatch':('Media integrity verification failed; no original was published.','wait_for_media_retry'),
    'media_size_mismatch':('The downloaded original size did not match its descriptor.','wait_for_media_retry'),
    'network_error':('The media request failed; bounded retry is scheduled.','wait_for_media_retry'),
    'integrity_error':('Media integrity verification failed; no original was published.','wait_for_media_retry'),
    'unavailable':('The provider did not expose this original.','inspect_source_attachment'),
    'size_limit':('The original exceeds the configured download size bound.','inspect_download_limits'),
    'retry_exhausted':('The bounded download attempts are exhausted. Automatic retry is stopped.','inspect_download_failure'),
    'spool_full':('The durable capture queue reached its bound.','check_capture_backlog'),
    'disk_reserve':('The configured disk reserve prevents additional writes.','check_free_disk'),
    'capture_quarantined':('Some captured items could not be applied and were moved to the spool quarantine folder. Later captures continue; the held items are not in the archive.','inspect_capture_quarantine'),
    'storage_unavailable':('The local originals or media temporary directory could not be written (missing, permissions or read-only). Provider attempts are not consumed; downloads resume after the storage path is fixed.','check_media_storage_path'),
    'media_storage_unavailable':('The native media temporary directory could not be written. Provider attempts are not consumed.','check_media_storage_path'),
    'paused_storage':('Original download is deferred by local storage admission. Message capture and searchable metadata remain independent; provider attempts are not consumed.','check_media_storage_capacity'),
    'durable_capture_failed':('A callback could not be durably saved; reconnect reconciliation is required.','check_collector_health'),
    'outside_prefetch_window':('The message is outside the collector\'s automatic prefetch window. The original is a known occurrence, downloaded only on an explicit request; a recorded earlier reason is history.','request_file_download'),
    'outside_history_bounds':('The message is older than the collector\'s configured history bound. The original is not downloaded, even on request.','get_sync_status_for_chat'),
    'collection_excluded':('Collection is disabled for this account or chat; the original is not downloaded.','check_collection_policy'),
    'history_bound_unknown':('The collector has not confirmed the history bound of this chat; the request is recorded, but whether the collector can download the original is unknown.','get_attachment'),
}


def describe(code, *, job_state=None):
    explanation,action=DETAILS.get(code,('No specific diagnosis recorded.' if not code else
        'Provider or archive observation; inspect the scoped sync evidence.','get_sync_status_for_chat'))
    if job_state is not None:
        retry=job_state=='queued' and code!='retry_exhausted'
        if job_state=='failed':
            explanation='The original download failed. Automatic retry is stopped; inspect the recorded reason before any operator intervention.'
            action='inspect_download_failure'
        elif job_state=='leased':
            explanation='An original transfer is in progress; this reason belongs to its earlier attempt.'
            action='get_attachment'
        return {'explanation':explanation,'next_action':action,'retry_scheduled':retry}
    return {'explanation':explanation,'next_action':action}


def freshness(scanned,now=None,*,operational=None):
    now=now or datetime.now(timezone.utc)
    age=max(0,(now-scanned).total_seconds()) if scanned is not None else None
    scan_state='scan_unknown' if age is None else 'scan_stale' if age>300 else 'recent_scan'
    phases=operational or {}
    capture=phases.get('live_capture',{})
    state=scan_state
    if capture.get('collector_running') is False:state='collector_not_running'
    elif capture.get('pending_events',0)>0 or capture.get('state')=='backlogged':state='capture_backlogged'
    elif capture.get('state') in ('paused','stopped','not_running'):state='capture_paused'
    qualifications=['media_storage_paused'] if phases.get('media',{}).get('storage_paused_jobs') else []
    return {'state':state,'scan_state':scan_state,
        'last_successful_scan_at':scanned,'scan_age_seconds':round(age,1) if age is not None else None,
        'pending_capture_events':capture.get('pending_events'),
        'last_capture_commit_at':(capture.get('last_commit') or {}).get('committed_by'),
        'qualifications':qualifications,
        'warning_after_seconds':300,'basis':'successful_scan_not_last_message_time',
        'notice':'Scan age is separate from live capture backlog and collector availability. Media storage pauses affect originals independently. An account scan does not prove every chat is current; inspect the specific chat.'}
