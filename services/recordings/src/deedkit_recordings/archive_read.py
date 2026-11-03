"""Bounded, credential-current local text retrieval. Never reads provider tokens."""
import asyncio
from datetime import datetime
import re
import unicodedata

from . import archive_feed
from .archive_store import namespace, utc_text
from .security import Failure, read_document, principal, cursor, continuation


def expression(query):
    words = re.findall(r'[^\W_]+', unicodedata.normalize('NFC', query), re.UNICODE)
    if not words or len(words) > 16 or any(len(word) > 64 for word in words):
        raise Failure('invalid_parameters', 422)
    # All tokens are quoted literal words, ANDed. Caller FTS operators,
    # column selectors, prefixes and grouping never reach the query parser.
    return ' AND '.join('"' + word + '"' for word in words)


def instant(value):
    """A timezone-aware ISO instant as the fixed-width UTC text the archive stores; anything else is a fixed refusal."""
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if 'T' not in value or moment.tzinfo is None:
            raise ValueError
        return utc_text(moment)
    except (ValueError, OverflowError):
        raise Failure('invalid_parameters', 422) from None


def evidence(part):
    return {key: value for key, value in part.items() if key != 'text'}


class LocalArchive:
    def __init__(self, runtime):
        self.runtime = runtime
        self.budget = asyncio.Semaphore(4)

    def authorized(self, key, account_id, *, content=True, expected=None):
        doc, secret = read_document(self.runtime.path)
        actor, accounts = principal(doc, key)
        required = {'recordings:read', 'recordings:content'} if content else {'recordings:read'}
        if account_id not in actor.accounts or not required <= set(actor.permissions):
            raise Failure('not_found_or_forbidden', 403)
        account = accounts[account_id]
        if doc.archive is None or account.archive_namespace is None:
            raise Failure('archive_disabled')
        if self.runtime.store is None or doc.archive.directory != str(self.runtime.store.directory):
            raise Failure(self.runtime.error or 'archive_unavailable')
        identity = {'principal': actor.key_sha256, 'namespace': namespace(account),
                    'directory': doc.archive.directory, 'secret': secret}
        if expected is not None and identity != expected:
            raise Failure('not_found_or_forbidden', 403)
        return account, identity

    async def feed(self, doc, head, after=0, limit=100):
        """The router's read of the journal (ANYM-726): the same four-slot admission and 2-second SQL deadline as every local
        read; a busy or broken archive is a fixed code and nothing else (the collector never waits for it)."""
        if self.budget.locked():
            raise Failure('archive_busy')
        async with self.budget:
            if doc.archive is None:
                raise Failure('archive_disabled')
            if self.runtime.store is None or doc.archive.directory != str(self.runtime.store.directory):
                raise Failure(self.runtime.error or 'archive_unavailable')
            view = archive_feed.access_view(doc, namespace)
            if head:
                return await self.runtime.db('feed_head', view)
            return await self.runtime.db('feed_page', view, after, limit)

    async def invoke(self, operation, args, key):
        account, identity = self.authorized(key, args.account, content=operation != 'archive_status')
        if self.budget.locked():
            raise Failure('archive_busy')
        async with self.budget:
            if operation == 'search':
                after = instant(args.created_after) if args.created_after is not None else None
                before = instant(args.created_before) if args.created_before is not None else None
                if after is not None and before is not None and after > before:
                    raise Failure('invalid_parameters', 422)
                if args.query is None and after is None and before is None and args.speaker is None:
                    raise Failure('query_or_filter_required', 422)
                items = await self.runtime.db('search', account, expression(args.query) if args.query is not None else None, args.limit,
                                              after, before, args.speaker.lower() if args.speaker is not None else None, args.order)
                result = {'account': account.id, 'provider': account.provider, 'items': [
                    {**item, 'account': account.id, 'provider': account.provider, 'source_data': True} for item in items],
                    'search_mode': 'lexical', 'coverage': 'local_observed_unknown', 'limit': args.limit}
            elif operation == 'context':
                binding = {'operation': 'archive_context', 'principal': identity['principal'],
                           'namespace': identity['namespace'], 'account': account.id, 'provider': account.provider,
                           'arguments': args.model_dump(exclude={'cursor'})}
                previous = cursor(args.cursor, binding, identity['secret'], revocation_fields=('principal', 'namespace')) if args.cursor else None
                record = await self.runtime.db('context', account, args.recording_ref)
                part = record['parts'].get(args.kind, {'state': 'unavailable', 'text': '', 'digest': None})
                text = part.get('text', '')
                version = f"{record['revision']}:{part.get('digest') or ''}"
                offset = 0
                if previous:
                    if previous.get('digest') != version:
                        raise Failure('source_changed', 409)
                    offset = previous.get('position')
                    if type(offset) is not int or not 0 < offset < len(text):
                        raise Failure('invalid_cursor', 400)
                end = min(len(text), offset + args.page_chars)
                result = {'account': account.id, 'provider': account.provider, 'recording_ref': record['ref'],
                          'provider_recording_id': record['provider_id'], 'revision': record['revision'],
                          'kind': args.kind, 'text': text[offset:end], 'offset': offset, 'source_data': True,
                          'source_digest': part.get('digest'), 'part': evidence(part),
                          'coverage': 'local_observed_unknown', 'complete': end == len(text),
                          'provenance': 'stored_vendor_text_with_original_speaker_and_offsets_when_provided',
                          'next_cursor': continuation(binding, end, identity['secret'], version) if end < len(text) else None}
            else:
                result = await self.runtime.status(account)
            # A revoke/namespace switch while SQLite waits must not return old
            # data. This check reads only current grants and local settings.
            self.authorized(key, args.account, content=operation != 'archive_status', expected=identity)
            return result
