"""Only documented Omi GET routes. Bounded, cancellable, no retries or persistence."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time

import httpx

from .security import Failure, RateLimited, loads

ORIGIN = 'https://api.omi.me'
ROUTE = '/v1/dev/user/conversations'
ID = re.compile(r'[A-Za-z0-9_-]{1,256}')
RETRY_DEFAULT, RETRY_CAP = 3600, 6 * 3600
UNPLACED = -1.0  # Omi's start == end sentinel for text it could not place on the timeline (UNPLACED_SEGMENT_OFFSET)
PROCESSING = 86400  # an empty title and overview this young is Omi's lazy processing, not a missing summary
SKEW = 3600  # a creation time at most this far ahead of our clock still counts; further ahead is implausible


def retry_after(headers):
    """Seconds from a `Retry-After` header; missing or anything but a plain integer gives the default, never over 6 h."""
    raw = headers.get('retry-after', '')
    if not re.fullmatch(r'[0-9]{1,9}', raw):
        return RETRY_DEFAULT
    return min(max(int(raw), 1), RETRY_CAP)


class Provider:
    def __init__(self, transport=None, timeout=12, maximum=2 * 1024 * 1024):
        self.transport, self.timeout, self.maximum = transport, timeout, maximum
        self.active = 0

    async def get(self, token, recording=None, params=None):
        if recording is not None and not ID.fullmatch(recording):
            raise Failure('invalid_parameters', 422)
        if self.active >= 4:
            raise Failure('provider_busy')
        self.active += 1
        try:
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False,
                                             timeout=self.timeout) as client:
                    async with client.stream('GET', ORIGIN + ROUTE + ('/' + recording if recording else ''),
                            params=params, headers={'Authorization': 'Bearer ' + token,
                                                    'Accept': 'application/json', 'Accept-Encoding': 'identity'}) as response:
                        if response.status_code in (401, 403):
                            raise Failure('provider_auth_required')
                        if response.status_code == 429:
                            raise RateLimited(retry_after(response.headers))
                        if response.status_code == 404 and recording:
                            raise Failure('not_found_or_forbidden', 404)
                        if response.status_code != 200:
                            raise Failure('provider_unavailable')
                        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                            raise Failure('invalid_provider_response', detail_code='encoding')
                        body = bytearray()
                        async for chunk in response.aiter_bytes(65536):
                            if len(body) + len(chunk) > self.maximum:
                                raise Failure('response_too_large')
                            body.extend(chunk)
                        return loads(body), response.headers
        except (TimeoutError, httpx.TimeoutException):
            raise Failure('provider_timeout') from None
        except httpx.HTTPError:
            raise Failure('provider_unavailable') from None
        except (ValueError, UnicodeError, RecursionError):
            raise Failure('invalid_provider_response', detail_code='not_json') from None
        finally:
            self.active -= 1


def metadata(value):
    if not isinstance(value, dict):
        raise Failure('invalid_provider_response', detail_code='not_object')
    if not isinstance(value.get('id'), str) or not ID.fullmatch(value['id']):
        raise Failure('invalid_provider_response', detail_code='id_invalid')
    result = {'id': value['id']}
    for name in ('created_at', 'started_at', 'finished_at', 'language', 'source'):
        item = value.get(name)
        if item is not None:
            if not isinstance(item, str) or len(item) > 128:
                raise Failure('invalid_provider_response', detail_code='metadata_field')
            result[name] = item
    structured = value.get('structured')
    if isinstance(structured, dict) and isinstance(structured.get('title'), str):
        title = structured['title']
        result['title'] = title[:500]
        if len(title) > 500:
            result['title_truncated'] = True
    if type(value.get('processing_deferred')) is bool:
        result['processing_deferred'] = value['processing_deferred']
    return result


def fingerprint(value):
    """What a list row says about content that can still change: the overview and the deferred-processing flag."""
    structured = value.get('structured') if isinstance(value, dict) else None
    overview = structured.get('overview') if isinstance(structured, dict) else None
    deferred = value.get('processing_deferred') if isinstance(value, dict) else None
    body = json.dumps([overview if isinstance(overview, str) else None, deferred if type(deferred) is bool else None],
                      ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(body.encode()).hexdigest()[:32]


def pagination(headers, count, limit):
    def boolean(name):
        value = headers.get(name, '').lower()
        return {'true': True, 'false': False}.get(value)
    # Omi sends `X-Omi-List-Truncated` only as `true` (docs overview.mdx:158): an absent header is not truncated, a
    # malformed value proves nothing.
    more = boolean('x-omi-has-more')
    truncated = boolean('x-omi-list-truncated') if 'x-omi-list-truncated' in headers else False
    raw_limit = headers.get('x-omi-effective-limit', '')
    effective = int(raw_limit) if re.fullmatch(r'[0-9]{1,3}', raw_limit) else None
    if effective is not None and not 1 <= effective <= 200:
        effective = None
    next_page = headers.get('x-omi-next-cursor')
    if next_page is not None and (not 1 <= len(next_page) <= 2048 or not next_page.isascii()
                                  or any(ord(c) < 33 or ord(c) > 126 for c in next_page)):
        next_page = None
    complete = False if more is True or truncated is True else (
        True if more is False and truncated is False and effective is not None else None)
    if count > min(limit, effective or limit):
        raise Failure('invalid_provider_response', detail_code='list_pagination')
    return {'has_more': more, 'list_truncated': truncated, 'effective_limit': effective,
            'complete': complete, 'ordering': 'provider_order_not_snapshot',
            'staleness_seconds': 10, 'date_field': 'created_at'}, next_page if more is True else None


def young(created, now):
    """True when `created_at` parses and is under a day old. No zone means UTC (as Omi documents and the archive index
    reads it, archive_store.parse_time); a time more than an hour ahead of our clock is implausible, so not young."""
    try:
        moment = datetime.fromisoformat(created.strip().replace('Z', '+00:00'))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return -SKEW <= now - moment.timestamp() < PROCESSING
    except (AttributeError, ValueError, TypeError, OverflowError, OSError):
        return False


def lazy_summary(value, now):
    """The collector's reading of an empty overview: a record under a day old, not flagged as deferred, has no summary
    yet, whatever its title. It waits for the stage reads or a changed list row (`deferred` keeps the pending schedule)."""
    structured = value.get('structured') if isinstance(value, dict) else None
    return isinstance(structured, dict) and structured.get('overview') == '' and \
        value.get('processing_deferred') is not True and young(value.get('created_at'), now)


def content(value, kind, now=None):
    if not isinstance(value, dict):
        raise Failure('invalid_provider_response', detail_code='not_object')
    if kind == 'summary':
        if value.get('processing_deferred') is True:
            return 'pending', ''
        structured = value.get('structured')
        text = structured.get('overview') if isinstance(structured, dict) else None
        if not isinstance(text, str):
            return 'unavailable', ''
        # GET never carries `processing_deferred`: a deferred conversation is listed with an empty structured block.
        if not text and structured.get('title') in (None, '') and young(value.get('created_at'), time.time() if now is None else now):
            return 'pending', ''
        return ('available' if text else 'empty'), text
    if value.get('transcript_included') is not True:
        return 'unavailable', ''
    segments = value.get('transcript_segments')
    if not isinstance(segments, list):
        return 'unavailable', ''
    rows = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise Failure('invalid_provider_response', detail_code='segment_shape')
        if not isinstance(segment.get('text'), str):
            raise Failure('invalid_provider_response', detail_code='segment_text')
        # Preserve supplied diarization, without claiming an identity inferred from prose.
        speaker = next((segment[field] for field in ('speaker_name', 'speaker_id', 'speaker')
                        if segment.get(field) is not None and segment[field] != ''), 'unknown')
        if not isinstance(speaker, (str, int)) or isinstance(speaker, bool) or len(str(speaker)) > 256:
            raise Failure('invalid_provider_response', detail_code='segment_speaker')
        offsets = []
        for field in ('start', 'end'):
            number = segment.get(field)
            if number is not None and (type(number) not in (int, float) or not -1e10 <= number <= 1e10 or not math.isfinite(number)):
                raise Failure('invalid_provider_response', detail_code='segment_offset')
            offsets.append(number)
        if segment['text']:
            row = {'start': offsets[0], 'end': offsets[1], 'speaker': str(speaker), 'text': segment['text']}
            if offsets[0] == UNPLACED and offsets[1] == UNPLACED:
                row.update(start=None, end=None, placed=False)  # other negative offsets are real pre-pin times: kept
            rows.append(json.dumps(row, ensure_ascii=False, separators=(',', ':')))
    return ('available' if rows else 'empty'), '\n'.join(rows)
