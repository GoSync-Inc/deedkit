"""Opt-in serial text collector. All failures stay inside the archive boundary."""
import asyncio
import hashlib
from pathlib import Path
import time

from .archive_store import Store, MAX_TEXT, namespace
from . import plaud, archive_plaud
from .omi import metadata, pagination, content
from .security import Failure, provider_token, read_document


class ArchiveRuntime:
    def __init__(self, path, provider, remote):
        self.path, self.provider, self.remote = Path(path), provider, remote
        self.store, self.task, self.error = None, None, None
        self.collecting, self.turn = asyncio.Lock(), 0
        self.errors = {}

    def document(self):
        return read_document(self.path)[0]

    async def db(self, method, *args):
        if self.store is None:
            raise Failure(self.error or 'archive_disabled')
        # Cancellation must not release the writer lock while a bounded SQLite
        # operation is still running in its thread.
        task = asyncio.create_task(asyncio.to_thread(self.store.run, method, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                pass
            raise

    async def start(self, *, background=True):
        if self.store is None:
            try:
                doc = self.document()
                if doc.archive is not None:
                    opening = asyncio.create_task(asyncio.to_thread(Store, doc.archive))
                    try:
                        self.store = await asyncio.shield(opening)
                    except asyncio.CancelledError:
                        try:
                            opened = await opening
                            await asyncio.to_thread(opened.close)
                        except Exception:
                            pass
                        raise
                self.error = None
            except Exception as error:
                self.error = error.code if isinstance(error, Failure) else 'archive_unavailable'
        if background and self.task is None:
            self.task = asyncio.create_task(self.loop())

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        async with self.collecting:
            if self.store is not None:
                await asyncio.to_thread(self.store.close)
                self.store = None

    async def loop(self):
        while True:
            await self.run_once()
            try:
                settings = self.document().archive
                interval = settings.poll_seconds if settings else 300
            except Exception:
                interval = 300
            await asyncio.sleep(interval)

    def current(self, account, fingerprint=None):
        doc = self.document()
        if doc.archive is None:
            raise Failure('archive_disabled')
        if self.store is None or doc.archive.directory != str(self.store.directory):
            raise Failure('archive_unavailable')
        current = next((a for a in doc.accounts if a.id == account.id), None)
        if current is None or current.archive_namespace is None or namespace(current) != namespace(account):
            raise Failure('archive_authorization_changed')
        token = provider_token(current)
        if fingerprint is not None and hashlib.sha256(token.encode()).hexdigest() != fingerprint:
            raise Failure('archive_authorization_changed')
        self.store.settings = doc.archive
        return token

    async def discover(self, account, now):
        checkpoint = await self.db('checkpoint', account)
        # Head refresh and one history page, with their discovered work committed
        # before continuation advances. A replay after interruption is harmless.
        for position in ([None, checkpoint] if checkpoint is not None else [None]):
            token = self.current(account)
            fingerprint = hashlib.sha256(token.encode()).hexdigest()
            if account.provider == 'plaud':
                page = position if position is not None else 1
                if type(page) is not int or not 1 <= page <= 100000:
                    raise Failure('archive_unavailable')
                value = await self.remote.call(token,'list_files',{'page':page,'page_size':25})
                try:
                    items, _, next_page = plaud.listing(value,25,page)
                except (ValueError,TypeError,OverflowError,UnicodeError,RecursionError):
                    raise Failure('invalid_provider_response') from None
            else:
                params = {'limit':25, 'offset':0, 'include_transcript':'false'}
                if position is not None:
                    params['cursor'] = position
                values, headers = await self.provider.get(token, params=params)
                if not isinstance(values, list) or len(values) > 25:
                    raise Failure('invalid_provider_response')
                items = [metadata(value) for value in values]
                _, next_page = pagination(headers, len(items), 25)
            # Refreshing the head must not replace an unfinished history cursor.
            next_checkpoint = checkpoint if position is None and checkpoint is not None else next_page
            await self.db('discover', account, items, next_checkpoint, now,
                          lambda: self.current(account, fingerprint))

    async def observe(self, account, recording_id, now):
        if account.provider == 'plaud':
            return await self.observe_plaud(account, recording_id, now)
        old = await self.db('record', account, recording_id)
        projected, observations = old['metadata'], {}
        token = self.current(account)
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        try:
            async with asyncio.timeout(60):
                value, _ = await self.provider.get(token, recording_id, {'include_transcript':'true'})
                projected = metadata(value)
                if projected['id'] != recording_id:
                    raise Failure('invalid_provider_response')
                for kind in ('transcript', 'summary'):
                    try:
                        state, text = content(value, kind)
                        if len(text.encode()) > MAX_TEXT:
                            raise Failure('archive_content_limit')
                        observations[kind] = state, text, None
                    except (Failure, ValueError, TypeError, OverflowError, UnicodeError, RecursionError) as error:
                        code = error.code if isinstance(error, Failure) else 'invalid_provider_response'
                        observations[kind] = 'error', '', code
        except (Failure, TimeoutError, ValueError, TypeError, UnicodeError, RecursionError) as error:
            code = error.code if isinstance(error, Failure) else (
                'provider_timeout' if isinstance(error, TimeoutError) else 'invalid_provider_response')
            observations = {kind: ('error', '', code) for kind in ('transcript', 'summary')}
        def guard():
            self.current(account, fingerprint)
        try:
            await self.db('observe', account, recording_id, projected, observations, now, guard)
        except Failure as error:
            if error.code != 'archive_content_limit':
                raise
            # Combined limits reject this attempt, not the previous accepted copy.
            rejected = {kind: ('error', '', 'archive_content_limit') for kind in observations}
            await self.db('observe', account, recording_id, old['metadata'], rejected, now, guard)

    async def status(self, account):
        account_id = account if isinstance(account,str) else account.id
        result = {'account':account_id,'enabled':False,'coverage':'unknown','record_count':None,
                  'queued_count':None,'retry_count':None,'last_success':None,'error':None,'collector_error':self.error}
        try:
            doc = self.document()
            current = next((a for a in doc.accounts if a.id == account_id),None)
            if doc.archive is None or current is None or current.archive_namespace is None:
                return {**result,'error':'archive_disabled'}
            result['enabled'] = True
            if self.store is None or doc.archive.directory != str(self.store.directory):
                return {**result,'error':self.error or 'archive_unavailable'}
            result.update(await self.db('status',current))
            # This overlay needs no write, including when quota prevents writing
            # account_error. Account-specific failures never leak another account.
            result['error'] = self.errors.get(namespace(current),result['error'])
            if self.error in ('archive_capacity','archive_unavailable','archive_schema_unsupported'):
                result['error'] = self.error
            return result
        except Failure as error:
            return {**result,'error':error.code}

    async def observe_plaud(self, account, recording_id, now):
        token = self.current(account)
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        def guard():
            self.current(account, fingerprint)
        async def call(name, args):
            return await self.remote.call(self.current(account,fingerprint), name, args)
        # Commit each part after its own completed attempt, without waiting for
        # the other part's readiness. Notes get their own bounded request even
        # if a transcript window later fails or the collector is cancelled.
        async with asyncio.timeout(60):
            for kind in ('summary','transcript'):
                old = await self.db('record',account,recording_id)
                try:
                    async with asyncio.timeout(12 if kind == 'summary' else 40):
                        if kind == 'summary':
                            value = await call('get_note',{'file_id':recording_id})
                            state,text = plaud.content(value,'summary',recording_id)
                        else:
                            state,text = await archive_plaud.transcript(call,recording_id)
                        if len(text.encode())>MAX_TEXT:
                            raise Failure('archive_content_limit')
                    observation = state,text,None
                except (Failure,TimeoutError,ValueError,TypeError,UnicodeError,RecursionError) as error:
                    code = error.code if isinstance(error,Failure) else (
                        'provider_timeout' if isinstance(error,TimeoutError) else 'invalid_provider_response')
                    observation = 'error','',code
                consistency = {'transcript':'observed_equal_reads_not_atomic_snapshot'} if kind == 'transcript' else {}
                try:
                    observations = {kind:observation}
                    if kind == 'summary' and 'transcript' not in old['parts']:
                        observations['transcript'] = 'pending','',None
                    await self.db('observe',account,recording_id,old['metadata'],observations,now,guard,consistency)
                except Failure as error:
                    if error.code != 'archive_content_limit':
                        raise
                    await self.db('observe',account,recording_id,old['metadata'],
                                  {kind:('error','','archive_content_limit')},now,guard)


    async def run_once(self, *, now=None):
        if self.collecting.locked():
            return
        async with self.collecting:
            try:
                if self.store is None:
                    await self.start(background=False)
                doc = self.document()
                if doc.archive is None or self.store is None:
                    return
                if doc.archive.directory != str(self.store.directory):
                    raise Failure('archive_unavailable')
                self.store.settings = doc.archive
                accounts = [a for a in doc.accounts if a.archive_namespace is not None]
                if not accounts:
                    self.error = None
                    return
                now = time.time() if now is None else now
                indexes = [(self.turn + i) % len(accounts) for i in range(min(4, len(accounts)))]
                self.error = None
                async with asyncio.timeout(300 if any(a.provider == 'plaud' for a in accounts) else 60):
                    for index in indexes:
                        self.turn = (index + 1) % len(accounts)
                        account = accounts[index]
                        try:
                            await self.db('capacity')
                            discovery_error = None
                            try:
                                await self.discover(account, now)
                            except Failure as error:
                                if error.code in ('archive_capacity','archive_unavailable','archive_disabled',
                                                  'archive_authorization_changed','provider_auth_required'):
                                    raise
                                discovery_error = error.code
                            for recording_id in await self.db('work', account, now, 8, lambda: self.current(account)):
                                await self.observe(account, recording_id, now)
                            if discovery_error:
                                raise Failure(discovery_error)
                            self.errors.pop(namespace(account),None)
                        except Failure as error:
                            self.error = error.code
                            self.errors[namespace(account)] = error.code
                            try:
                                await self.db('account_error', account, error.code)
                            except Failure:
                                pass
            except Exception as error:
                self.error = error.code if isinstance(error, Failure) else (
                    'provider_timeout' if isinstance(error, TimeoutError) else 'archive_unavailable')
