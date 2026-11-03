"""Opt-in serial text collector. All failures stay inside the archive boundary."""
import asyncio
import hashlib
from pathlib import Path
import time

from . import archive_feed
from .archive_store import Store, MAX_TEXT, namespace
from . import plaud, archive_plaud
from .omi import metadata, pagination, content, fingerprint
from .security import Failure, RateLimited, provider_token, read_document

PAGE = 200          # list page size the collector asks for (Omi clamps to 1-200)
HEAD_RESERVE = 6    # reads per hour only the head refresh and brand-new conversations may use
HEAD_EVERY = 300    # seconds between head refreshes once the history traversal is under way
REFRESH_EVERY = 86400  # a finished history is walked again this often (list rows only) to notice changed older records
HARD = ('archive_capacity', 'archive_unavailable', 'archive_disabled', 'archive_authorization_changed',
        'provider_auth_required', 'ledger_uncertain')


def monotonic():
    """One seam for the pass clock (tests move it without touching the event loop's own)."""
    return time.monotonic()


class ArchiveRuntime:
    def __init__(self, path, provider, remote):
        self.path, self.provider, self.remote = Path(path), provider, remote
        self.store, self.task, self.error = None, None, None
        self.index_error = None  # the schema-4 index build's own last failure; it never stops collection
        self.feed_error = None  # the event feed's own last failure (ANYM-726); it never stops collection
        self.collecting, self.turn, self.ledger = asyncio.Lock(), 0, asyncio.Lock()
        self.errors = {}
        self.started = monotonic()

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

    async def discover_legacy(self, account, now):
        """PLAUD: head page and one history page per pass, as before schema 3."""
        checkpoint = await self.db('checkpoint', account)
        # Head refresh and one history page, with their discovered work committed
        # before continuation advances. A replay after interruption is harmless.
        for position in ([None, checkpoint] if checkpoint is not None else [None]):
            token = self.current(account)
            fingerprint = hashlib.sha256(token.encode()).hexdigest()
            page = position if position is not None else 1
            if type(page) is not int or not 1 <= page <= 100000:
                raise Failure('archive_unavailable')
            value = await self.remote.call(token,'list_files',{'page':page,'page_size':25})
            try:
                items, _, next_page = plaud.listing(value,25,page)
            except (ValueError,TypeError,OverflowError,UnicodeError,RecursionError):
                raise Failure('invalid_provider_response') from None
            # Refreshing the head must not replace an unfinished history cursor.
            next_checkpoint = checkpoint if position is None and checkpoint is not None else next_page
            await self.db('discover', account, items, next_checkpoint, now,
                          lambda: self.current(account, fingerprint), 'legacy')

    async def pass_legacy(self, account, now):
        discovery_error = None
        try:
            await self.discover_legacy(account, now)
        except Failure as error:
            if error.code in HARD:
                raise
            discovery_error = error.code
        for recording_id in await self.db('work', account, now, 8, lambda: self.current(account)):
            await self.observe(account, recording_id, now)
        if discovery_error:
            raise Failure(discovery_error)

    def clock(self, now):
        """The pass's `now` moved on by the time this pass has really spent (earlier accounts, slow calls): a
        reservation or a 429 is stamped with when it is made, not with when the pass began. Whole seconds, rounded
        down: the ledger's drift margin covers the fraction."""
        return now + int(monotonic() - self.started)

    async def reserve(self, credential, specs, now):
        """Count the collector's own provider call before it is made; False when its share of a window is spent."""
        async with self.ledger:
            return await self.db('reserve', credential, specs, now)

    async def count(self, token, detail, now=None):
        """Foreground Omi reads are written to the same ledger but never refused by it and never fail the read."""
        try:
            if self.store is None:
                return
            buckets = ['reads', 'transcripts'] if detail else ['reads']
            async with self.ledger:
                await self.db('record_use', hashlib.sha256(token.encode()).hexdigest(), buckets,
                              time.time() if now is None else now)
        except Exception:
            pass

    def sharing(self, credential):
        """Namespaces of the archived Omi accounts whose token file holds this credential."""
        names = []
        for account in self.document().accounts:
            if account.provider == 'omi' and account.archive_namespace is not None:
                try:
                    if hashlib.sha256(provider_token(account).encode()).hexdigest() == credential:
                        names.append(namespace(account))
                except Exception:
                    pass
        return names

    async def throttle(self, credential, seconds, received):
        """Pause the collector for this credential until `received + seconds` (measured from the 429's arrival)."""
        await self.db('throttle', credential, self.sharing(credential), received + seconds)

    async def note_rate_limit(self, token, seconds):
        """An on-demand read met a 429: the collector pauses for that credential too. Best effort."""
        try:
            if self.store is not None:
                await self.throttle(hashlib.sha256(token.encode()).hexdigest(), seconds, time.time())
        except Exception:
            pass

    async def list_page(self, account, token, credential, now, settings, *, kind, cursor, limit):
        """One cheap list call (IDs and list metadata, never a transcript); None when the budget has no slot."""
        reads = settings.omi_budget.reads_per_hour
        if not await self.reserve(credential, [('reads', reads - HEAD_RESERVE if kind in ('first', 'history') else reads)],
                                  self.clock(now)):
            return None
        params = {'limit': limit, 'offset': 0, 'include_transcript': 'false'}
        if cursor is not None:
            params['cursor'] = cursor
        values, headers = await self.provider.get(token, params=params)
        if not isinstance(values, list) or len(values) > limit:
            raise Failure('invalid_provider_response')
        items = [metadata(value) for value in values]
        fingerprints = {item['id']: fingerprint(value) for item, value in zip(items, values)}
        info, next_page = pagination(headers, len(items), limit)
        # A truncated page is re-read, never taken as the end. The end needs affirmative evidence (no more pages, not
        # truncated, an effective limit); a page that proves neither a cursor nor the end keeps what it listed but
        # leaves the position where it was and is reported.
        unproven = False
        if info['list_truncated'] is True:
            complete = False
        elif next_page is not None or info['complete'] is True:
            complete = True
        else:
            complete, unproven = False, True
        done = await self.db('discover', account, items, next_page, now, lambda: self.current(account, credential),
                             kind, fingerprints, complete, cursor)
        if unproven:
            raise Failure('invalid_provider_response')
        return done['inserted']

    async def discover_omi(self, account, token, credential, now, settings, plan):
        """Cheap enumeration. The head (newest pages) and the history traversal are separate, so a full queue
        pauses only the history and never blocks a conversation created now."""
        queued, cap, reserve = plan['queued'], plan['cap'], plan['slice']
        if plan['state'] == 'not_started':
            free = cap - reserve - queued
            if free >= 1:
                await self.list_page(account, token, credential, now, settings, kind='first', cursor=None,
                                     limit=min(PAGE, free))
            return
        head_due = plan['head_cursor'] is not None or plan['last_head_at'] is None or not 0 <= now - plan['last_head_at'] < HEAD_EVERY
        if head_due:
            free = cap - queued
            if free >= 1:
                inserted = await self.list_page(account, token, credential, now, settings, kind='head',
                                                cursor=plan['head_cursor'] or None, limit=min(PAGE, free))
                queued += inserted or 0
            else:
                await self.db('head_blocked', account, now)
        room = cap - reserve - queued
        # A finished history is walked again once a day: list rows only, so a changed title or overview of an older
        # record is noticed without a transcript call (the head alone only sees the newest pages).
        again = plan['state'] == 'reached_end' and plan['last_page_at'] is not None and \
            now - plan['last_page_at'] >= REFRESH_EVERY
        if (plan['state'] == 'in_progress' or again) and room >= 1:
            await self.list_page(account, token, credential, now, settings, kind='history',
                                 cursor=None if again else plan['checkpoint'], limit=min(PAGE, room))

    async def pass_omi(self, account, now, settings):
        token = self.current(account)
        credential = hashlib.sha256(token.encode()).hexdigest()
        plan = await self.db('plan', account, credential, now)
        if plan['throttled_until'] is not None and plan['throttled_until'] > now:
            return  # Retry-After is honoured: no provider call, no error, no record touched
        budget, discovery_error = settings.omi_budget, None
        try:
            try:
                await self.discover_omi(account, token, credential, now, settings, plan)
            except RateLimited:
                raise
            except Failure as error:
                if error.code in HARD:
                    raise
                discovery_error = error.code
            items = await self.db('work_items', account, now, 8, lambda: self.current(account), False)
            items.sort(key=lambda item: not item[1])  # conversations of the last 24 h first: they own the reserved reads
            rotated = False
            for recording_id, fresh in items:
                reads = budget.reads_per_hour if fresh else budget.reads_per_hour - HEAD_RESERVE
                if not await self.reserve(credential, [('reads', reads), ('transcripts', budget.transcript_per_hour)],
                                          self.clock(now)):
                    break
                if not rotated:
                    # Fairness advances with dispatched work, not with passes a spent window denied.
                    await self.db('rotate', account)
                    rotated = True
                await self.observe(account, recording_id, now)
        except RateLimited as limited:
            await self.throttle(credential, limited.retry_after_s, self.clock(now))
            return
        if discovery_error:
            raise Failure(discovery_error)

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
        except RateLimited:
            raise  # a 429 is the provider's pause, not a failed record
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

    async def status(self, account, now=None):
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
            budget = doc.archive.omi_budget if current.provider == 'omi' else None
            result.update(await self.db('status', current, time.time() if now is None else now, budget))
            # This overlay needs no write, including when quota prevents writing
            # account_error. Account-specific failures never leak another account.
            if 'index' in result:
                result['index']['error'] = self.index_error
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


    async def maintain_feed(self, doc):
        """Keep the event feed's identity and access-change events current (ANYM-726). Contained like the index build:
        a failure is recorded and collection goes on."""
        if doc.event_router is None:
            self.feed_error = None
            return
        try:
            async with asyncio.timeout(30):
                await self.db('feed_sync', archive_feed.access_view(doc, namespace))
            self.feed_error = None
        except Failure as error:
            self.feed_error = error.code
        except Exception:
            self.feed_error = 'archive_unavailable'

    async def build_index(self):
        """Advance the schema-4 index build by a bounded step. A failure here is recorded for the status and never
        reaches collection: the old index (or the published one) keeps serving reads."""
        try:
            async with asyncio.timeout(30):
                await self.db('index_step')
            self.index_error = None
        except Failure as error:
            self.index_error = error.code
        except Exception:
            self.index_error = 'archive_unavailable'

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
                self.store.feed_enabled = doc.event_router is not None
                await self.maintain_feed(doc)
                await self.build_index()
                accounts = [a for a in doc.accounts if a.archive_namespace is not None]
                if not accounts:
                    self.error = None
                    return
                now = time.time() if now is None else now
                self.started = monotonic()
                indexes = [(self.turn + i) % len(accounts) for i in range(min(4, len(accounts)))]
                self.error = None
                async with asyncio.timeout(300 if any(a.provider == 'plaud' for a in accounts) else 60):
                    for index in indexes:
                        self.turn = (index + 1) % len(accounts)
                        account = accounts[index]
                        try:
                            await self.db('capacity')
                            if account.provider == 'plaud':
                                await self.pass_legacy(account, now)
                            else:
                                await self.pass_omi(account, now, doc.archive)
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
