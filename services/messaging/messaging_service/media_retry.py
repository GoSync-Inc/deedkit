"""Attempt budget and retry schedule of a media original (ANYM-699). Imports nothing, so every media module can share it."""
from random import random

# A claim adds one attempt; a job is claimed only while attempts < MEDIA_MAX_ATTEMPTS.
MEDIA_MAX_ATTEMPTS = 8
# Seconds to wait after attempt 1..7 of a transient failure: 30 s, 5 min, 30 min, 2 h, 6 h, 12 h, 12 h (32.6 h).
MEDIA_RETRY_DELAYS = (30, 300, 1800, 7200, 21600, 43200, 43200)
# Jitter only lengthens a wait, so a provider-requested wait stays a floor.
MEDIA_RETRY_JITTER = 0.10
# A provider-requested wait (rate_limited) spends no attempt until the job is this old.
MEDIA_RATE_LIMIT_FREE_DAYS = 7
# Codes that end the job at the first failure.
MEDIA_PERMANENT_CODES = frozenset({'unavailable', 'size_limit'})
# WhatsApp adapter codes that end the job at the first failure: the same answer would come again (ANYM-815:
# media_decrypt_failed means the stored media key does not open bytes whose hash matched).
MEDIA_PROVIDER_PERMANENT_CODES = frozenset({'media_unavailable', 'media_reference_changed', 'unsupported_media_type',
    'media_size_limit', 'invalid_media_path', 'media_decrypt_failed'})
# CDN answers the native adapter reports by status (ANYM-815); each is transient and follows the schedule.
MEDIA_HTTP_CODES = frozenset('media_http_'+str(status) for status in (400, 401, 408, 429, 500, 502, 503, 504))
# WhatsApp publishes no media retention; integrators report about 30 days (ANYM-815). An estimate, not a promise.
PROVIDER_MEDIA_RETENTION_DAYS = {'whatsapp': 30}
# Recorded codes of a failed WhatsApp fetch that an expired CDN copy explains (NULL reads as retry_exhausted).
# Local, size, integrity and reference codes have their own cause and keep their wording.
LIKELY_EXPIRED_CODES = frozenset({'media_unavailable', 'media_network_error', 'media_timeout', 'network_error',
    'unavailable', 'retry_exhausted'}) | MEDIA_HTTP_CODES
# A code with a smaller budget than MEDIA_MAX_ATTEMPTS: the last attempt that may fail and retry.
# media_reference_expired: every attempt reads the message again, so a fresh reference gets two quick retries
# (30 s, 5 min) and a third failure ends the job; the 32.6 h schedule would only spin on it.
MEDIA_ATTEMPT_CAPS = {'media_reference_expired': 3}
# Explicit requests wake a job whose backoff is at least this long.
MEDIA_REQUEST_WAKE_SECONDS = 3600


def retry_delay(attempts, retry_after=None, *, jitter=random):
    """Seconds until the next try after ``attempts`` spent attempts: schedule or provider wait, whichever is longer, plus jitter."""
    base = MEDIA_RETRY_DELAYS[min(max(attempts, 1), len(MEDIA_RETRY_DELAYS)) - 1]
    return max(base, retry_after or 0) * (1 + MEDIA_RETRY_JITTER * jitter())
