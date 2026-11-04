"""Reviewed recordings failures: relay codes/guidance, never provider response text."""
from .policy import Denied

# Permanent failures must not invite a blind retry. Only explicitly transient states do.
RECOVERY = {
    'archive_disabled': (False, 'operator_enable_recording_archive'),
    'archive_unavailable': (False, 'operator_check_recording_archive'),
    'archive_schema_unsupported': (False, 'operator_check_recording_archive_version'),
    'archive_capacity': (False, 'operator_check_recording_archive_capacity'),
    'archive_busy': (True, 'retry_after_delay'),
    'index_building': (True, 'retry_after_delay'),
    'query_or_filter_required': (False, 'correct_arguments'),
    'transcript_window_exceeded': (False, 'provider_transcript_window_limit'),
    'unsupported_plaud_filters_or_limit': (False, 'correct_arguments'),
    'audio_unsupported': (False, 'provider_audio_unsupported'),
    'response_too_large': (False, 'source_exceeds_read_limit'),
    'provider_auth_required': (False, 'owner_reauthorize_provider'),
    'invalid_provider_response': (False, 'operator_check_provider_contract'),
    'configuration_unavailable': (False, 'operator_check_recordings_configuration'),
    'provider_timeout': (True, 'retry_after_delay'),
    'provider_busy': (True, 'retry_after_delay'),
    'provider_unavailable': (True, 'retry_after_delay'),
    'source_changed': (False, 'restart_read_without_cursor'),
    'invalid_cursor': (False, 'restart_read_without_cursor'),
    'not_found_or_forbidden': (False, 'check_account_and_recording_access'),
    'unauthorized': (False, 'operator_check_recordings_credential'),
    'invalid_parameters': (False, 'correct_arguments'),
    'request_too_large': (False, 'reduce_request_size'),
    'request_timeout': (True, 'retry_after_delay'),
}
# ANYM-753: the closed vocabulary naming which part of a provider body the backend rejected. Anything else is dropped.
DETAIL_CODES = frozenset({
    'not_object', 'id_invalid', 'id_mismatch', 'metadata_field', 'segment_shape', 'segment_text', 'segment_speaker',
    'segment_offset', 'list_shape', 'list_pagination', 'encoding', 'not_json', 'projection_error',
})


def failure(value, status):
    code = value.get('error') if isinstance(value, dict) else None
    if not isinstance(code, str) or code not in RECOVERY:
        return None
    retry, action = RECOVERY[code]
    error = Denied(code, status)
    error.detail = {'error': code, 'retryable': retry, 'next_action': action}
    if retry:
        error.detail['retry_after_seconds'] = 2
    detail_code = value.get('detail_code')
    if code == 'invalid_provider_response' and isinstance(detail_code, str) and detail_code in DETAIL_CODES:
        error.detail['detail_code'] = detail_code
    return error
