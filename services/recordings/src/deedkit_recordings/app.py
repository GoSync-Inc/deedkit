"""Private HTTP backend; the gateway is the agent-facing MCP surface."""
import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from pydantic import Field, ValidationError

from .omi import Provider, metadata, pagination, content
from . import plaud
from .security import Strict, Failure, configuration, provider_token, cursor, continuation, canonical, loads

READ, CONTENT, AUDIO = 'recordings:read', 'recordings:content', 'recordings:audio'


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
    query: str = Field(min_length=1, max_length=256)
    limit: int = Field(default=10, ge=1, le=50)


class Context(ArchiveStatus):
    recording_ref: str = Field(pattern=r'^[0-9a-f]{32}$')
    kind: Literal['transcript', 'summary'] = 'transcript'
    page_chars: int = Field(default=4000, ge=100, le=10000)
    cursor: str | None = Field(default=None, min_length=1, max_length=8192)


class Audio(Strict):
    account: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    recording_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,256}$')


def dates(args):
    result = {}
    for key in ('start_date', 'end_date'):
        value = getattr(args, key)
        if value is None:
            continue
        try:
            moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if 'T' not in value or moment.tzinfo is None:
                raise ValueError
            result[key] = moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        except ValueError:
            raise Failure('invalid_parameters', 422) from None
    if len(result) == 2 and datetime.fromisoformat(result['start_date'].replace('Z', '+00:00')) > datetime.fromisoformat(
            result['end_date'].replace('Z', '+00:00')):
        raise Failure('invalid_parameters', 422)
    return result


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
        return JSONResponse({'error': error.code}, status_code=error.status)

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
        token = provider_token(account)
        params = dates(args) if operation == 'list_recordings' else {}
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
            values, headers = await provider.get(token, params=params)
            if not isinstance(values, list) or len(values) > args.limit:
                raise Failure('invalid_provider_response')
            evidence, next_page = pagination(headers, len(values), args.limit)
            return {'account': account.id, 'provider': 'omi', 'items': [metadata(v) for v in values],
                    'pagination': evidence,
                    'next_cursor': continuation(binding, next_page, secret) if next_page else None}
        if account.provider == 'plaud':
            tool = 'get_transcript' if args.kind == 'transcript' else 'get_note'
            parameters = {'file_id': args.recording_id}
            if args.kind == 'transcript':
                parameters.update(block='transaction', limit=500)
            value = await remote.call(token, tool, parameters)
            state, text = plaud.content(value, args.kind, args.recording_id)
            record = {'id': args.recording_id}
        else:
            value, _ = await provider.get(token, args.recording_id, {'include_transcript': 'true'})
            record = metadata(value)
            if record['id'] != args.recording_id:
                raise Failure('invalid_provider_response')
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

    async def endpoint(request):
        try:
            return JSONResponse(await invoke(request))
        except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
            # Provider projection and JSON encoding can fail after the HTTP client boundary.
            # Never expose their values through an unhandled exception or server traceback.
            raise Failure('invalid_provider_response') from None

    app.router.routes.append(Route('/v1/{operation}', endpoint, methods=['POST']))
    return app
