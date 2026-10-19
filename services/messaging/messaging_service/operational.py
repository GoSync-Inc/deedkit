"""Bounded operational diagnostics; never provider payloads or credentials."""
from datetime import datetime, timezone
from .status_details import describe

TERMINAL = {
    'whatsapp_logged_out':'relink_in_owner_terminal',
    'whatsapp_pairing_required':'relink_in_owner_terminal',
    'provider_auth_failed':'relink_in_owner_terminal',
    'whatsapp_identity_mismatch':'check_configured_account_identity',
    'telegram_pairing_required':'relink_in_owner_terminal',
    'telegram_identity_mismatch':'check_configured_account_identity',
}
CONNECTION_ERRORS = set(TERMINAL) | {'provider_disconnected','provider_not_ready',
    'provider_initialization_failed','provider_connect_timeout','provider_page_stalled',
    'provider_protocol_error','durable_capture_failed','capture_overload','spool_full',
    'disk_reserve','spool_corrupt','collector_lease_lost'}
MEDIA_ERRORS = {'media_unavailable','media_reference_changed','unsupported_media_type',
    'media_hash_mismatch','media_size_mismatch','media_size_limit','media_metadata_unavailable',
    'invalid_media_path','media_timeout','media_network_error','media_storage_unavailable'}
# Why a queued original is not automatic work of the current run (ANYM-284).
INELIGIBLE = ('outside_prefetch_window','outside_history_bounds','history_bound_unknown','collection_excluded')
LOCAL_STORAGE_ERRORS = {'storage_unavailable','media_storage_unavailable'}
# The fixed causes of a fail-closed live capture write (ANYM-352).
CAPTURE_STOP_DETAILS = {'spool_full','disk_reserve','spool_io_error','capture_error'}
# Which bound a capture_overload stop tripped (ANYM-291); status and supervisor line only, not telemetry.
OVERLOAD_STOP_DETAILS = {'waiters_limit','slot_wait_timeout'}
# Fixed codes each collector prints to its supervisor when it stops.
SUPERVISOR_STOP_CODES = {
    'telegram':frozenset({'collector_disabled','session_handover_not_confirmed','telegram_pairing_required',
        'telegram_identity_mismatch','private_session_required','live_account_required',
        'collector_already_owned','session_already_owned','collector_lease_lost',
        'durable_capture_failed','capture_overload','collection_disabled','provider_disconnected'}),
    'whatsapp':frozenset({'collector_disabled','session_handover_not_confirmed','whatsapp_pairing_required',
        'whatsapp_identity_mismatch','private_session_required','live_account_required',
        'collector_already_owned','session_already_owned','collector_lease_lost',
        'collection_disabled','provider_disconnected'}),
}
PHASE_STATES = {
    'connection':{'connecting','connected','retry_wait','blocked','stopped'},
    'live_capture':{'starting','receiving','backlogged','paused','stopped'},
    'history':{'scanning','waiting','error','stopped'},
}
PHASE_REASONS = CONNECTION_ERRORS | {'provider_ready','capture_ready','capture_draining','capture_quarantined',
    'collector_shutdown','history_scan','history_scan_finished','provider_temporarily_unavailable',
    'collector_starting','operator_resumed'}
# Stop codes a collector's telemetry event may carry (ANYM-292): never exception text.
# Only Telegram records a fixed capture detail, so only it has qualified codes.
TELEMETRY_STOP_CODES = {component:frozenset(codes|CONNECTION_ERRORS) for component,codes in SUPERVISOR_STOP_CODES.items()}
TELEMETRY_STOP_CODES['telegram'] |= {'durable_capture_failed.'+detail for detail in CAPTURE_STOP_DETAILS}


def collector_stop(exc, component):
    """Supervisor code, fixed capture detail and telemetry code of a collector stop; never raises."""
    # Exception strings from providers can contain private requests; only an exact
    # built-in str equal to a fixed code passes (a str subclass could fake equality).
    try:text=str(exc)
    except Exception:text=''
    if type(text) is not str:text=''
    code=text if text in SUPERVISOR_STOP_CODES[component] else 'collector_failed'
    detail=None
    if component=='telegram' and text in ('durable_capture_failed','capture_overload'):
        try:detail=getattr(exc,'detail',None)
        except Exception:detail=None
        allowed=CAPTURE_STOP_DETAILS if text=='durable_capture_failed' else OVERLOAD_STOP_DETAILS
        if type(detail) is not str or detail not in allowed:detail=None
    # A qualified code comes only from the fixed detail, never from exception text.
    event=(text+'.'+detail if detail and text=='durable_capture_failed' else
        text if text in SUPERVISOR_STOP_CODES[component] or text in CONNECTION_ERRORS else 'collector_failed')
    return code,detail,event


def operations(row, history, files, now=None, *, eligibility=None):
    now=now or datetime.now(timezone.utc)
    phases=dict(row.get('operations') or {})
    lease=row.get('collector_lease_until')
    running=lease is not None and lease>now
    for key in ('connection','live_capture'):
        phase=dict(phases.get(key) or {'state':'unknown','reason_code':'not_checked',
            'changed_at':None,'observed_at':None,'next_action':'check_collector_health'})
        phase['collector_running']=running
        phase['detail']=describe(phase['reason_code'])
        # Retain the actual failure and retry/action evidence even when stopped.
        if not running and phase['state'] not in ('blocked','retry_wait','unknown'):
            phase['reported_state']=phase['state'];phase['state']='not_running'
        phases[key]=phase
    gaps=history.get('gap_chats',0)
    phase=dict(phases.get('history') or {})
    phase.update(state='partial_with_gaps' if gaps else
        'partial_with_errors' if row.get('detail_code') in ('configured_chat_unavailable',
            'whatsapp_chat_unavailable','provider_temporarily_unavailable') else
        'traversing' if history.get('catchup_pending_chats') or
            history.get('configured_window',{}).get('in_progress') or
            history.get('unbounded_history',{}).get('in_progress') else 'partial',
        full_history_verified=False, collector_running=running)
    phases['history']=phase
    counts=files['download'];errors=files.get('download_errors',{})
    error_states=files.get('download_error_states',{})
    ineligible={}
    if eligibility is not None:
        # Pending work is what the current run can download by itself: an unexpired
        # request or the published window (ANYM-284). Other queued jobs keep their
        # recorded code in `errors` but are reported by why they wait instead.
        eligible=eligibility['queued'].get('eligible',{})
        error_states={code:{state:n for state,n in {**error_states.get(code,{}),'queued':eligible.get(code,0)}.items() if n}
            for code in set(error_states)|{code for code in eligible if code is not None}}
        error_states={code:states for code,states in error_states.items() if states}
        ineligible={reason:sum(eligibility['queued'].get(reason,{}).values())
            for reason in INELIGIBLE}
    diagnostics=[]
    for code in sorted(errors if eligibility is None else error_states):
        states=error_states.get(code)
        if states:
            diagnostics.extend({'reason_code':code,'count':number,'job_state':state,
                **describe(code,job_state=state)} for state,number in sorted(states.items()))
        else:diagnostics.append({'reason_code':code,'count':errors[code],**describe(code)})
    diagnostics.extend({'reason_code':reason,'count':number,'job_state':'queued',**describe(reason)}
        for reason,number in ineligible.items() if number)
    fallback=errors if eligibility is None else {}
    storage_paused=error_states.get('paused_storage',{}).get('queued',fallback.get('paused_storage',0))
    storage_unavailable=error_states.get('storage_unavailable',{}).get('queued',fallback.get('storage_unavailable',0))
    retry_queued=sum(states.get('queued',0) for code,states in error_states.items()
        if code not in ('paused_storage','storage_unavailable','retry_exhausted'))
    eligible_queued=sum(eligibility['queued'].get('eligible',{}).values()) if eligibility is not None else None
    phases['media']={'state':'processing' if counts['leased'] else
        'storage_unavailable' if storage_unavailable else
        'paused_storage' if (storage_paused if eligibility is not None else errors.get('paused_storage')) else
        'needs_attention' if counts['failed'] else
        'queued' if (eligible_queued if eligibility is not None else counts['queued']) else 'idle',
        'counts':counts,'errors':errors,'diagnostics':diagnostics,'independent_of_connection':True,
        'storage_paused_jobs':storage_paused,'storage_paused':bool(storage_paused),
        'storage_paused_basis':'unknown' if eligibility is None else 'eligible_jobs',
        'prefetch_window_days':eligibility['prefetch_days'] if eligibility is not None else None,
        'eligible_queued_jobs':eligible_queued,
        **{reason+'_jobs':ineligible.get(reason) for reason in INELIGIBLE},
        'storage_unavailable_jobs':storage_unavailable,
        'terminal_failed_jobs':counts['failed'],'needs_attention':bool(counts['failed']),
        'retry_queued_jobs':retry_queued}
    return phases


LIVE_CONFIRM_SECONDS=60
RECENT_LIVE_EVENT_SECONDS=300


def _time(value):
    if isinstance(value,datetime):return value
    try:return datetime.fromisoformat(value) if isinstance(value,str) else None
    except ValueError:return None


def liveness(phases, now=None):
    """Live-session health, independent of new messages and of history coverage."""
    now=now or datetime.now(timezone.utc)
    connection=phases.get('connection') or {}
    capture=phases.get('live_capture') or {}
    # The drain loop refreshes live_capture about every 15 seconds and runs only
    # while the provider session is open; a disconnect ends the collector run.
    confirmed=_time(capture.get('observed_at')) if capture.get('state') in ('receiving','backlogged') else None
    age=round(max(0,(now-confirmed).total_seconds()),1) if confirmed else None
    last=capture.get('last_commit') or {}
    event=_time(last.get('committed_by'))
    pending=capture.get('pending_events')
    if connection.get('collector_running') is not True:state='collector_not_running'
    elif connection.get('state')=='blocked':state='blocked'
    elif connection.get('state')!='connected':state='not_connected'
    elif age is None or age>LIVE_CONFIRM_SECONDS:state='unconfirmed'
    elif pending:state='capture_backlogged'
    elif event and (now-event).total_seconds()<=RECENT_LIVE_EVENT_SECONDS:state='receiving'
    else:state='idle_connected'
    return {'state':state,'connected':state in ('receiving','idle_connected','capture_backlogged'),
        'quiet_but_healthy':state=='idle_connected',
        'session_confirmed_at':confirmed.isoformat() if confirmed else None,'confirmation_age_seconds':age,
        'confirm_window_seconds':LIVE_CONFIRM_SECONDS,
        'last_live_event_at':event.isoformat() if event else None,'last_live_event_origin':last.get('origin'),
        'pending_live_events':pending,'basis':'collector_drain_refresh_while_provider_session_open',
        'notice':'Independent of new messages, history gaps and catch-up. Confirms an open provider session, '
            'not that every provider callback was delivered.'}
