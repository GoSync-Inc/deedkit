"""Private HTTP backend; the gateway is the agent-facing MCP surface."""
import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from pydantic import Field, ValidationError

from .omi import Provider, metadata, pagination, content
from .security import Strict, Failure, configuration, provider_token, cursor, continuation, canonical, loads

READ, CONTENT = 'recordings:read', 'recordings:content'


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


def create_app(config_path, *, transport=None, provider_timeout=12, provider_bytes=2 * 1024 * 1024):
    app = Starlette()
    provider = Provider(transport, provider_timeout, provider_bytes)
    app.state.provider = provider
    path = Path(config_path).absolute()

    async def failure(request, error):
        return JSONResponse({'error': error.code}, status_code=error.status)

    app.add_exception_handler(Failure, failure)

    async def invoke(request: Request):
        operation = request.path_params['operation']
        authorization = request.headers.get('authorization', '')
        if not authorization.startswith('Bearer ') or not 1 <= len(authorization[7:]) <= 512:
            raise Failure('unauthorized', 401)
        actor, accounts, secret = configuration(path, authorization[7:])
        models = {'capabilities': Empty, 'list_recordings': Listing, 'read_recording': Reading}
        model = models.get(operation)
        if model is None:
            raise Failure('not_found_or_forbidden', 404)
        required = {READ, CONTENT} if operation == 'read_recording' else {READ}
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
                        if n != 'read_recording' or CONTENT in actor.permissions]},
                    'accounts': [{'id': a.id, 'provider': a.provider, 'label': a.label}
                                 for a in accounts.values() if a.id in actor.accounts],
                    'audio': 'unsupported', 'coverage': 'on_demand_provider_reads_not_archive',
                    'limits': {'provider_timeout_seconds': 12, 'provider_response_bytes': 2 * 1024 * 1024,
                               'active_calls_per_process': 4, 'cursor_ttl_seconds': 900}}
        if args.account not in actor.accounts:
            raise Failure('not_found_or_forbidden', 403)
        account = accounts[args.account]
        token = provider_token(account)
        params = dates(args) if operation == 'list_recordings' else {}
        binding = {'provider': 'omi', 'account': account.id, 'principal': actor.key_sha256,
                   'provider_identity': hashlib.sha256(token.encode()).hexdigest(), 'operation': operation,
                   'arguments': {**args.model_dump(exclude={'cursor'}), **params}}
        previous = cursor(args.cursor, binding, secret) if args.cursor else None
        if operation == 'list_recordings':
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
        return {'account': account.id, 'provider': 'omi', 'recording': record, 'kind': args.kind,
                'content_state': state, 'text': text[offset:end], 'offset': offset,
                'source_digest': digest, 'complete': end == len(text),
                'next_cursor': continuation(binding, end, secret, digest) if end < len(text) else None}

    async def endpoint(request):
        return JSONResponse(await invoke(request))

    app.router.routes.append(Route('/v1/{operation}', endpoint, methods=['POST']))
    return app
