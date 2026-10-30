"""Only documented Omi GET routes. Bounded, cancellable, no retries or persistence."""
import asyncio
import json
import math
import re

import httpx

from .security import Failure, loads

ORIGIN = 'https://api.omi.me'
ROUTE = '/v1/dev/user/conversations'
ID = re.compile(r'[A-Za-z0-9_-]{1,256}')


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
                        if response.status_code == 404 and recording:
                            raise Failure('not_found_or_forbidden', 404)
                        if response.status_code != 200:
                            raise Failure('provider_unavailable')
                        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                            raise Failure('invalid_provider_response')
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
            raise Failure('invalid_provider_response') from None
        finally:
            self.active -= 1


def metadata(value):
    if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not ID.fullmatch(value['id']):
        raise Failure('invalid_provider_response')
    result = {'id': value['id']}
    for name in ('created_at', 'started_at', 'finished_at', 'language', 'source'):
        item = value.get(name)
        if item is not None:
            if not isinstance(item, str) or len(item) > 128:
                raise Failure('invalid_provider_response')
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


def pagination(headers, count, limit):
    def boolean(name):
        value = headers.get(name, '').lower()
        return {'true': True, 'false': False}.get(value)
    more, truncated = boolean('x-omi-has-more'), boolean('x-omi-list-truncated')
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
        raise Failure('invalid_provider_response')
    return {'has_more': more, 'list_truncated': truncated, 'effective_limit': effective,
            'complete': complete, 'ordering': 'provider_order_not_snapshot',
            'staleness_seconds': 10, 'date_field': 'created_at'}, next_page if more is True else None


def content(value, kind):
    if not isinstance(value, dict):
        raise Failure('invalid_provider_response')
    if kind == 'summary':
        if value.get('processing_deferred') is True:
            return 'pending', ''
        structured = value.get('structured')
        text = structured.get('overview') if isinstance(structured, dict) else None
        if not isinstance(text, str):
            return 'unavailable', ''
        return ('available' if text else 'empty'), text
    if value.get('transcript_included') is not True:
        return 'unavailable', ''
    segments = value.get('transcript_segments')
    if not isinstance(segments, list):
        return 'unavailable', ''
    rows = []
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get('text'), str):
            raise Failure('invalid_provider_response')
        # Preserve supplied diarization, without claiming an identity inferred from prose.
        speaker = next((segment[field] for field in ('speaker_name', 'speaker_id', 'speaker')
                        if segment.get(field) is not None and segment[field] != ''), 'unknown')
        if not isinstance(speaker, (str, int)) or isinstance(speaker, bool) or len(str(speaker)) > 256:
            raise Failure('invalid_provider_response')
        offsets = []
        for field in ('start', 'end'):
            number = segment.get(field)
            if number is not None and (type(number) not in (int, float) or not 0 <= number <= 1e10 or not math.isfinite(number)):
                raise Failure('invalid_provider_response')
            offsets.append(number)
        if segment['text']:
            rows.append(json.dumps({'start': offsets[0], 'end': offsets[1], 'speaker': str(speaker),
                                    'text': segment['text']}, ensure_ascii=False, separators=(',', ':')))
    return ('available' if rows else 'empty'), '\n'.join(rows)
