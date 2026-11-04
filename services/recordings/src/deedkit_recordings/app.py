"""Private HTTP backend; the gateway is the agent-facing MCP surface."""
import asyncio
import hashlib
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as clock, timezone
from pathlib import Path
from typing import Literal

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from pydantic import Field, ValidationError

from .omi import Provider, metadata, pagination, content
from . import plaud
from .security import (Strict, Failure, RateLimited, configuration, provider_token, cursor, continuation, canonical, loads,
                       read_document, router_authorized)
from . import archive_feed

READ, CONTENT, AUDIO = 'recordings:read', 'recordings:content', 'recordings:audio'
PLAIN_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
# ANYM-754: a dated Omi list follows the provider past pages that leave no row, at most this many pages and never
# into a provider call that could outlast the gateway's 35-second backend deadline.
DATED_PAGES, DATED_DEADLINE_S = 5, 30


class Empty(Strict):
    pass


class Listing(Strict):
    account: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    limit: int = Field(default=25, ge=1, le=50)
    start_date: str | None = Field(default=None, max_length=64)
    end_date: str | None = Field(default=None, max_length=64)
    cursor: str | None = Field(default=None, min_length=1, max_length=8192)


class Reading(Strict):
    account: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    recording_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,256}$')
    kind: Literal['transcript', 'summary'] = 'transcript'
    page_chars: int = Field(default=4000, ge=100, le=10000)
    cursor: str | None = Field(default=None, min_length=1, max_length=8192)


class ArchiveStatus(Strict):
    account: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')


class Searching(ArchiveStatus):
    query: str | None = Field(default=None, min_length=1, max_length=256)
    limit: int = Field(default=10, ge=1, le=50)
    created_after: str | None = Field(default=None, max_length=64)
    created_before: str | None = Field(default=None, max_length=64)
    speaker: str | None = Field(default=None, min_length=1, max_length=64)
    order: Literal['relevance', 'newest'] = 'relevance'


class Context(ArchiveStatus):
    recording_ref: str = Field(pattern=r'^[0-9a-f]{32}$')
    kind: Literal['transcript', 'summary'] = 'transcript'
    page_chars: int = Field(default=4000, ge=100, le=10000)
    cursor: str | None = Field(default=None, min_length=1, max_length=8192)


class Audio(Strict):
    account: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    recording_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,256}$')


def instant(value):
    """An aware ISO timestamp as UTC, or None (a missing, naive or unparsable value cannot be placed)."""
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return moment.astimezone(timezone.utc) if moment.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def dates(args):
    """Validated bounds as UTC instants. A plain `YYYY-MM-DD` covers that whole UTC day (ANYM-754)."""
    result = {}
    for key in ('start_date', 'end_date'):
        value = getattr(args, key)
        if value is None:
            continue
        try:
            if PLAIN_DATE.fullmatch(value):
                moment = datetime.combine(date.fromisoformat(value), clock.min if key == 'start_date' else clock.max,
                                          timezone.utc)
            else:
                moment = instant(value) if 'T' in value else None
                if moment is None:
                    raise ValueError
            result[key] = moment
        except ValueError:
            raise Failure('invalid_parameters', 422) from None
    if len(result) == 2 and result['start_date'] > result['end_date']:
        raise Failure('invalid_parameters', 422)
    return result


def outside(item, bounds):
    """True only for a row whose `created_at` places it outside the asked interval; an unplaceable row stays."""
    moment = instant(item.get('created_at'))
    if moment is None:
        return False
    return ('start_date' in bounds and moment < bounds['start_date']) or (
        'end_date' in bounds and moment > bounds['end_date'])


def create_app(config_path, *, transport=None, provider_timeout=12, provider_bytes=2 * 1024 * 1024,
               archive_background=True):
    from .archive import ArchiveRuntime

    @asynccontextmanager
    async def lifespan(app):
        await app.state.archive.start(background=archive_background)
        try:
            yield
        finally:
            await app.state.archive.close()

    app = Starlette(lifespan=lifespan)
    provider = Provider(transport, provider_timeout, provider_bytes)
    app.state.provider = provider
    remote = plaud.Provider(provider, transport, provider_timeout, provider_bytes)
    app.state.plaud = remote
    path = Path(config_path).absolute()
    app.state.archive = ArchiveRuntime(path, provider, remote)
    from .archive_read import LocalArchive
    local = LocalArchive(app.state.archive)

    async def failure(request, error):
        body = {'error': error.code}
        if error.detail_code:
            body['detail_code'] = error.detail_code
        if isinstance(error, RateLimited):
            body['retry_after_s'] = error.retry_after_s
        return JSONResponse(body, status_code=error.status)

    async def omi_get(token, recording=None, params=None):
        # Foreground Omi reads share the provider's windows with the collector: counted in the ledger, never refused
        # by it, and a 429 also pauses the collector for this credential.
        await app.state.archive.count(token, recording is not None)
        try:
            return await provider.get(token, recording, params)
        except RateLimited as limited:
            await app.state.archive.note_rate_limit(token, limited.retry_after_s)
            raise

    app.add_exception_handler(Failure, failure)

    async def invoke(request: Request):
        operation = request.path_params['operation']
        authorization = request.headers.get('authorization', '')
        if not authorization.startswith('Bearer ') or not 1 <= len(authorization[7:]) <= 512:
            raise Failure('unauthorized', 401)
        actor, accounts, secret = configuration(path, authorization[7:])
        models = {'capabilities': Empty, 'list_recordings': Listing, 'read_recording': Reading, 'audio': Audio,
                  'archive_status': ArchiveStatus, 'search': Searching, 'context': Context}
        model = models.get(operation)
        if model is None:
            raise Failure('not_found_or_forbidden', 404)
        required = {READ, CONTENT} if operation in ('read_recording', 'search', 'context') else ({READ, AUDIO} if operation == 'audio' else {READ})
        if not required <= set(actor.permissions):
            raise Failure('not_found_or_forbidden', 403)
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > 16384:
                        raise Failure('request_too_large', 413)
                    raw.extend(chunk)
        except TimeoutError:
            raise Failure('request_timeout', 408) from None
        try:
            args = model.model_validate(loads(raw))
        except (ValueError, TypeError, RecursionError, ValidationError):
            raise Failure('invalid_parameters', 422) from None
        if operation == 'capabilities':
            return {'service': 'deedkit-recordings', 'version': '0.1.0', 'principal': actor.id,
                    'permissions': actor.permissions, 'tool_manifest': {'names': [n for n in models
                        if (n not in ('read_recording', 'search', 'context') or CONTENT in actor.permissions) and (n != 'audio' or AUDIO in actor.permissions)]},
                    'accounts': [{'id': a.id, 'provider': a.provider, 'label': a.label}
                                 for a in accounts.values() if a.id in actor.accounts],
                    'audio': 'plaud_link_requires_audio_scope' if any(a.provider == 'plaud' and a.id in actor.accounts for a in accounts.values()) else 'unsupported', 'coverage': 'on_demand_provider_reads_not_archive',
                    'local_archive': {'search_mode': 'lexical', 'coverage': 'local_observed_unknown',
                                      'provider_calls_for_local_reads': False},
                    'limits': {'provider_timeout_seconds': 12, 'provider_response_bytes': 2 * 1024 * 1024,
                               'active_calls_per_process': 4, 'cursor_ttl_seconds': 900}}
        if args.account not in actor.accounts:
            raise Failure('not_found_or_forbidden', 403)
        account = accounts[args.account]
        if operation in ('search', 'context', 'archive_status'):
            return await local.invoke(operation, args, authorization[7:])
        request.state.provider = account.provider
        token = provider_token(account)
        bounds = dates(args) if operation == 'list_recordings' else {}
        params = {key: value.isoformat().replace('+00:00', 'Z') for key, value in bounds.items()}
        binding = {'provider': account.provider, 'account': account.id, 'principal': actor.key_sha256,
                   'provider_identity': hashlib.sha256(token.encode()).hexdigest(), 'operation': operation,
                   'arguments': {**args.model_dump(exclude={'cursor'}), **params}}
        previous = cursor(args.cursor, binding, secret) if getattr(args, 'cursor', None) else None
        if operation == 'audio':
            if account.provider != 'plaud':
                raise Failure('audio_unsupported', 422)
            value = await remote.call(token, 'get_file', {'file_id': args.recording_id})
            return {'account': account.id, 'provider': 'plaud', 'recording_id': args.recording_id,
                    **plaud.audio(value, args.recording_id)}
        if operation == 'list_recordings':
            if account.provider == 'plaud':
                if params or args.limit < 10:
                    raise Failure('unsupported_plaud_filters_or_limit', 422)
                page = previous['position'] if previous else 1
                if type(page) is not int or not 1 <= page <= 100000:
                    raise Failure('invalid_cursor', 400)
                value = await remote.call(token, 'list_files', {'page': page, 'page_size': args.limit})
                items, evidence, next_page = plaud.listing(value, args.limit, page)
                return {'account': account.id, 'provider': 'plaud', 'items': items, 'pagination': evidence,
                        'next_cursor': continuation(binding, next_page, secret) if next_page else None}
            params.update(limit=args.limit, offset=0, include_transcript='false')
            if previous:
                if not isinstance(previous.get('position'), str):
                    raise Failure('invalid_cursor', 400)
                params['cursor'] = previous['position']
            started, read, dropped = time.monotonic(), 0, 0
            while True:
                values, headers = await omi_get(token, params=params)
                if not isinstance(values, list) or len(values) > args.limit:
                    raise Failure('invalid_provider_response', detail_code='list_shape')
                evidence, next_page = pagination(headers, len(values), args.limit)
                items, read = [metadata(v) for v in values], read + 1
                if not bounds:
                    break
                # Omi's own date parameters are passed but not trusted: drop what created_at places outside them.
                kept = [item for item in items if not outside(item, bounds)]
                dropped, items = dropped + len(items) - len(kept), kept
                if (items or next_page is None or read >= DATED_PAGES
                        or time.monotonic() - started + provider.timeout > DATED_DEADLINE_S):
                    break
                params['cursor'] = next_page
            result = {'account': account.id, 'provider': 'omi', 'items': items, 'pagination': evidence,
                      'next_cursor': continuation(binding, next_page, secret) if next_page else None}
            if bounds:
                result['date_filter'] = {'requested': True, 'filtered_locally': dropped, 'pages_read': read}
            return result
        if account.provider == 'plaud':
            tool = 'get_transcript' if args.kind == 'transcript' else 'get_note'
            parameters = {'file_id': args.recording_id}
            if args.kind == 'transcript':
                parameters.update(block='transaction', limit=500)
            value = await remote.call(token, tool, parameters)
            state, text = plaud.content(value, args.kind, args.recording_id)
            record = {'id': args.recording_id}
        else:
            value, _ = await omi_get(token, args.recording_id, {'include_transcript': 'true'})
            record = metadata(value)
            if record['id'] != args.recording_id:
                raise Failure('invalid_provider_response', detail_code='id_mismatch')
            state, text = content(value, args.kind)
        digest = hashlib.sha256(canonical({'state': state, 'text': text})).hexdigest()
        offset = 0
        if previous:
            if previous.get('digest') != digest:
                raise Failure('source_changed', 409)
            offset = previous.get('position')
            if type(offset) is not int or not 0 < offset < len(text):
                raise Failure('invalid_cursor', 400)
        end = min(len(text), offset + args.page_chars)
        return {'account': account.id, 'provider': account.provider, 'recording': record, 'kind': args.kind,
                'content_state': state, 'text': text[offset:end], 'offset': offset,
                'source_digest': digest, 'complete': end == len(text),
                'next_cursor': continuation(binding, end, secret, digest) if end < len(text) else None}

    async def feed_route(request, head):
        # ANYM-726. Without `event_router` configuration these routes do not exist; with it, only the router token opens
        # them (an agent key is `unauthorized`, and the router token opens no other route).
        doc, _ = read_document(path)
        if doc.event_router is None:
            raise Failure('not_found_or_forbidden', 404)
        authorization = request.headers.get('authorization', '')
        if (not authorization.startswith('Bearer ') or not 1 <= len(authorization[7:]) <= 512
                or not router_authorized(doc, authorization[7:])):
            raise Failure('unauthorized', 401)
        query = request.query_params
        after, limit = query.get('after', '0'), query.get('limit', '100')
        if head:
            if query:
                raise Failure('invalid_parameters', 422)
        elif (set(query) - {'after', 'limit'} or any(len(query.getlist(key)) > 1 for key in query)
              or not (after.isascii() and after.isdigit() and limit.isascii() and limit.isdigit()) or len(after) > 16 or len(limit) > 6
              or int(after) > archive_feed.MAX_SEQ or not 1 <= int(limit) <= archive_feed.MAX_LIMIT):
            raise Failure('invalid_parameters', 422)
        return JSONResponse(await local.feed(doc, head, int(after), int(limit)))

    async def events_page(request):
        return await feed_route(request, False)

    async def events_head(request):
        return await feed_route(request, True)

    async def endpoint(request):
        try:
            return JSONResponse(await invoke(request))
        except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
            # Provider projection and JSON encoding can fail after the HTTP client boundary.
            # Never expose their values through an unhandled exception or server traceback.
            # ANYM-753 names Omi body parts only; a PLAUD body keeps the bare code.
            omi = getattr(request.state, 'provider', None) == 'omi'
            raise Failure('invalid_provider_response', detail_code='projection_error' if omi else None) from None

    app.router.routes.append(Route('/v1/events', events_page, methods=['GET']))
    app.router.routes.append(Route('/v1/events/head', events_head, methods=['GET']))
    app.router.routes.append(Route('/v1/{operation}', endpoint, methods=['POST']))
    return app
