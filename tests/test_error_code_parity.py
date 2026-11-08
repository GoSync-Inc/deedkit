"""The error-code allow-lists of the Python service (error_codes.json) and the Go and JS adapters stay one set.

Python reads its lists from messaging_service/error_codes.json. The native Go adapter (runtime.go) and the
browser fallback (transport-state.mjs, operations.mjs, provider.mjs, media.mjs) keep their own copies, read here
as source text so no provider is built or started. A difference not listed in an EXPECTED_* constant fails the
test, and so does a listed difference that no longer exists. Resolving one changes which codes a collector
retries, rejects or surfaces: a defect card, not a refactor.
"""
from importlib.resources import files
import json
from pathlib import Path
import re

from messaging_service import commands, operational, whatsapp_collector

ROOT = Path(__file__).resolve().parents[1]
CODES = json.loads(files('messaging_service').joinpath('error_codes.json').read_text(encoding='utf-8'))
GO_RUNTIME = (ROOT / 'whatsapp/whatsmeow/runtime.go').read_text()
JS = {name: (ROOT / 'whatsapp' / name).read_text()
      for name in ('transport-state.mjs', 'operations.mjs', 'provider.mjs', 'media.mjs')}

# Connection errors the JS fallback lacks: the Telegram pairing and identity codes (no WhatsApp adapter raises them)
# and the codes only the Python collector itself raises (lease, connect timeout, protocol, spool corruption).
EXPECTED_JS_CONNECTION_MISSING = {
    'collector_lease_lost', 'provider_connect_timeout', 'provider_protocol_error', 'spool_corrupt',
    'telegram_identity_mismatch', 'telegram_pairing_required'}
# The same for the Go adapter, which also lacks provider_page_stalled (a browser-fallback condition).
EXPECTED_GO_CONNECTION_MISSING = EXPECTED_JS_CONNECTION_MISSING | {'provider_page_stalled'}
# Python reply codes the Go safe-error lists (both initializers in runtime.go) do not carry: the Python-only
# collector conditions above, and codes Go never answers with. Without an entry Go would turn the code into
# provider_operation_failed (send_receipt_missing and provider_operation_failed are Python-side outcomes).
EXPECTED_GO_SAFE_MISSING = {
    'collector_lease_lost', 'forbidden', 'provider_connect_timeout', 'provider_operation_failed',
    'provider_page_stalled', 'provider_protocol_error', 'send_receipt_missing', 'spool_corrupt',
    'telegram_identity_mismatch', 'telegram_pairing_required'}
# The other direction: codes Go answers with that no Python list maps; Python reports them as provider_operation_failed.
EXPECTED_GO_SAFE_ONLY = {
    'collector_disabled', 'history_request_unknown', 'invalid_cursor', 'invalid_limit', 'invalid_policy',
    'invalid_request', 'message_target_unavailable', 'operation_invalid', 'operation_outcome_unknown',
    'owner_terminal_required', 'policy_expired', 'provider_already_started', 'provider_response_too_large',
    'session_already_owned', 'unsupported_method'}


def _strings(text, quote):
    return set(re.findall(quote + r'([a-z][a-z0-9_]*)' + quote, text))  # media_http_<status> has digits


def _js_set(source, pattern):
    match = re.search(pattern, JS[source], re.S)
    assert match, (source, pattern)
    return _strings(match.group(1), "'")


def _go_set(pattern):
    match = re.search(pattern, GO_RUNTIME, re.S)
    assert match, pattern
    return _strings(match.group(1), '"')


def _go_safe_errors():
    """Every code of every `for ... range []string{...} { safeErrors[...] = true }` initializer in runtime.go."""
    lists = re.findall(r'range \[\]string\{(.*?)\} \{\n\t\tsafeErrors\[', GO_RUNTIME, re.S)
    assert len(lists) == 2, len(lists)  # a third initializer must be read too: update this count with it
    return set().union(*(_strings(codes, '"') for codes in lists))


def _python_reply_codes():
    reply = CODES['provider_reply']
    return (set(CODES['connection_errors']) | set(CODES['media_errors']) | set(reply['command_rejected'])
            | set(reply['send_rejected']) | set(reply['operation_safe']))


# spec: ANYM-292
def test_python_constants_are_the_json_lists():
    assert operational.CONNECTION_ERRORS == set(CODES['connection_errors'])
    assert operational.MEDIA_ERRORS == set(CODES['media_errors'])
    assert {k: set(v) for k, v in operational.SUPERVISOR_STOP_CODES.items()} == {
        k: set(v) for k, v in CODES['supervisor_stop_codes'].items()}
    assert all(type(v) is frozenset for v in operational.SUPERVISOR_STOP_CODES.values())
    assert whatsapp_collector.PROVIDER_REPLY == CODES['provider_reply']
    assert commands.CODES['operation_validation'] == CODES['operation_validation']
    # Every terminal code is a connection error, and no list repeats a code.
    assert set(operational.TERMINAL) <= operational.CONNECTION_ERRORS
    for name in ('connection_errors', 'media_errors', 'operation_validation'):
        assert len(CODES[name]) == len(set(CODES[name])), name
    for name, codes in CODES['supervisor_stop_codes'].items():
        assert len(codes) == len(set(codes)), name
    for name, codes in CODES['provider_reply'].items():
        assert len(codes) == len(set(codes)), name
    # The classes a provider reply is sorted into do not overlap, or the first branch would shadow the others.
    reply = CODES['provider_reply']
    assert not set(reply['command_rejected']) & set(reply['send_rejected'])
    assert not set(reply['command_rejected']) & operational.CONNECTION_ERRORS
    assert not set(reply['send_rejected']) & operational.CONNECTION_ERRORS


# spec: ANYM-815
def test_fixed_media_codes_reach_every_consumer():
    from messaging_service import media_retry, status_details
    new = {'media_decrypt_failed'} | {'media_http_' + str(n) for n in (400, 401, 408, 429, 500, 502, 503, 504)}
    assert new <= set(CODES['media_errors']) and new <= operational.MEDIA_ERRORS
    assert new <= _go_safe_errors()
    assert media_retry.MEDIA_HTTP_CODES == new - {'media_decrypt_failed'}
    # A decrypt failure repeats on the same copy; an HTTP answer follows the retry schedule.
    assert 'media_decrypt_failed' in media_retry.MEDIA_PROVIDER_PERMANENT_CODES
    assert not media_retry.MEDIA_HTTP_CODES & media_retry.MEDIA_PERMANENT_CODES
    assert not media_retry.MEDIA_HTTP_CODES & media_retry.MEDIA_PROVIDER_PERMANENT_CODES
    for code in new:
        assert code in status_details.DETAILS, code
    assert status_details.describe('provider_media_likely_expired', job_state='failed')['next_action'] is None
    # The browser fallback never invents a native-only code.
    assert not new & _js_set('media.mjs', r"const safe=new Set\(\[(.*?)\]\)")


# spec: ANYM-292
def test_js_connection_errors_are_a_subset_with_the_listed_gap():
    js = _js_set('transport-state.mjs', r'CONNECTION_ERRORS=new Set\(\[(.*?)\]\)')
    python = set(CODES['connection_errors'])
    assert js <= python, sorted(js - python)
    assert python - js == EXPECTED_JS_CONNECTION_MISSING


# spec: ANYM-292
def test_go_transport_failures_are_a_subset_with_the_listed_gap():
    go = _go_set(r'var transportFailures = map\[string\]bool\{(.*?)\}\n')
    python = set(CODES['connection_errors'])
    assert go <= python, sorted(go - python)
    assert python - go == EXPECTED_GO_CONNECTION_MISSING


# spec: ANYM-292
def test_js_operation_and_media_errors_are_known_to_python():
    operation = _js_set('operations.mjs', r'OPERATION_ERRORS=new Set\(\[(.*?)\]\)')
    assert operation <= set(CODES['provider_reply']['command_rejected']), sorted(operation)
    media = _js_set('media.mjs', r"const safe=new Set\(\[(.*?)\]\)")
    assert media <= set(CODES['media_errors']), sorted(media)
    # The provider's own safe list carries named codes only; the one Python does not know is Go's too.
    safe = _js_set('provider.mjs', r'const SAFE=new Set\(\[(.*?)\]\);')
    assert safe - _python_reply_codes() == {'provider_response_too_large'}


# spec: ANYM-292
def test_go_safe_errors_cover_the_python_codes_but_the_listed_ones():
    go = _go_safe_errors()
    python = _python_reply_codes()
    assert python - go == EXPECTED_GO_SAFE_MISSING
    assert go - python == EXPECTED_GO_SAFE_ONLY
    # Every media code Python accepts is a code the native adapter can answer with.
    assert set(CODES['media_errors']) <= go
