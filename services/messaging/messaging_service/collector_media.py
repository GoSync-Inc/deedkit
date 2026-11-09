"""The Telegram collector's media path: claiming, transferring and finishing originals, the idle media loop
and the disk floors it publishes (moved from telegram_collector.py, W5-4a; WhatsAppCollector inherits it).

Its log records keep the telegram_collector logger name.
"""
import asyncio
from contextlib import suppress
from datetime import datetime,timezone
import errno
import logging
import time

from .archive import MEDIA_CONTENDED
from .collector_common import DbBackoff
from .telegram_common import ProviderUnavailable,chat_error_evidence,database_errors
from .telegram_governor import BACKGROUND,LIVE_MEDIA,ProviderRateLimited
from .operational import MEDIA_ERRORS
from .media_retry import MEDIA_PROVIDER_PERMANENT_CODES

log=logging.getLogger('messaging_service.telegram_collector')
# ANYM-877: a live job's message fetch also takes the messages of up to BURST_FETCH other queued live jobs of its chat.
BURST_FETCH=20
# Out-of-window backfill: at most one original per MEDIA_BACKFILL_INTERVAL seconds, and a
# MEDIA_BACKFILL_IDLE pause once nothing is left, on top of the governor's request pacing.
MEDIA_BACKFILL_INTERVAL=15; MEDIA_BACKFILL_IDLE=600
# An idle media loop claims again after this many seconds, or at once when its own collector stores a message with
# media; a contended claim (Archive.claim_media) is retried at once (ANYM-839).
MEDIA_IDLE_SECONDS=5


def media_deadline(size):
    """Whole-transfer bound that still lets a large original finish on a paced session."""
    # 512 KiB chunks at most one paced request per second, shared with history.
    expected=(size or 0)/(256*1024)
    return max(3600,min(6*3600,int(expected*1.5)+600))


class MediaCollector:
    """TelegramCollector's media path; it reads the collector's archive, state, provider, writers and spools."""

    async def media_once(self):
        governor=getattr(self.provider,'governor',None)
        if governor:await governor.wait()
        priority=({'live_writer':self.live_writer,'live_window_seconds':self.live_window_seconds}
                  if self.live_writer is not None else {})
        job=await asyncio.to_thread(self.archive.claim_media,self.owner,account=self.account,
                                    collector_lease=self.state.lease,since=self.history_since,writer=self.writer,
                                    media_prefetch_days=self.media_prefetch_days,**priority)
        if job is None and self.media_backfill and time.monotonic()>=self._backfill_due:
            # Old voice notes and small files, one at a time between idle window claims,
            # paced so live media and history keep the shared provider budget.
            job=await asyncio.to_thread(self.archive.claim_media,self.owner,account=self.account,
                collector_lease=self.state.lease,since=self.history_since,writer=self.writer,
                media_prefetch_days=self.media_prefetch_days,backfill=True,**priority)
            # A contended backfill claim leased nothing: it keeps its turn for the next, immediate claim.
            if job is not MEDIA_CONTENDED:
                self._backfill_due=time.monotonic()+(MEDIA_BACKFILL_IDLE if job is None else MEDIA_BACKFILL_INTERVAL)
        # A contended claim is not idle: the loop claims again at once.
        if not job: return job is MEDIA_CONTENDED
        writer=self.live_writer if job.get('_live_media') else self.writer
        async def renew():
            while True:
                await asyncio.sleep(30)
                await asyncio.to_thread(self.archive.renew_media,job,collector_lease=self.state.lease)
        async def transfer():
            source=await asyncio.to_thread(self.archive.media_source,job,collector_lease=self.state.lease)
            if getattr(self.provider,'fetches_bursts',False):
                # ANYM-877: someone waits for a live or requested original; one fetch serves the chat's burst.
                requested=job.get('requested_until') is not None and job['requested_until']>datetime.now(timezone.utc)
                source={**source,'_tier':LIVE_MEDIA if job.get('_live_media') or requested else BACKGROUND}
                if job.get('_live_media'):
                    # Only an optimisation: a failed lookup fetches this one message, as before.
                    try:source['_burst']=await asyncio.to_thread(self.archive.media_burst,job,
                        window_seconds=self.live_window_seconds,limit=BURST_FETCH)
                    except Exception as exc:
                        log.warning('media_burst_failed error=%s',type(exc).__name__)
            stream=self.provider.chunks(source,job['descriptor'])
            try:
                return await writer.write_async(stream,expected_bytes=job['descriptor']['bytes'],
                    reservation=job.get('_storage_reservation'))
            finally:
                if hasattr(stream,'aclose'): await stream.aclose()
        renewal=asyncio.create_task(renew()); download=asyncio.create_task(transfer())
        try:
            done,_=await asyncio.wait([renewal,download],return_when=asyncio.FIRST_COMPLETED,
                timeout=media_deadline(job['descriptor'].get('bytes')))
            if not done: raise TimeoutError('media_timeout')
            if renewal in done: renewal.result()
            blob=await download
            for attempt in range(3):
                # The original is already verified on disk; do not repeat the provider transfer.
                try:
                    await asyncio.to_thread(self.archive.finish_media,job,blob,writer,collector_lease=self.state.lease)
                    break
                except database_errors():
                    if attempt==2:raise
                    await asyncio.sleep(1+attempt)
        except (ProviderUnavailable,ProviderRateLimited,OSError,ValueError,RuntimeError,TimeoutError) as exc:
            if str(exc)=='media_lease_lost': return True
            if str(exc)=='collector_lease_lost': raise
            if isinstance(exc,ProviderUnavailable): code,permanent='unavailable',True
            elif isinstance(exc,ProviderRateLimited): code,permanent='rate_limited',False
            elif str(exc)=='media_size_limit': code,permanent='size_limit',True
            elif str(exc)=='disk_reserve': code,permanent='disk_reserve',False
            elif isinstance(exc,OSError) and exc.errno in (errno.ENOSPC,errno.EDQUOT):
                code,permanent='disk_reserve',False
            elif isinstance(exc,OSError) and (exc.filename is not None or exc.errno==errno.EROFS):
                # A missing directory, permission or read-only mount is local
                # storage, not the provider network.
                code,permanent='storage_unavailable',False
            elif str(exc)=='media_reference_expired': code,permanent='media_reference_expired',False
            elif str(exc) in ('media_size_mismatch','media_hash_mismatch','blob_corrupt'):
                code,permanent='integrity_error',False
            else: code,permanent='network_error',False
            if self.retry_connect and str(exc) in MEDIA_ERRORS:
                code=str(exc)
                permanent=code in MEDIA_PROVIDER_PERMANENT_CODES
            with suppress(RuntimeError):
                await asyncio.to_thread(self.archive.fail_media,job,code,permanent=permanent,
                    retry_after=exc.seconds if isinstance(exc,ProviderRateLimited) else None,collector_lease=self.state.lease)
        except Exception as exc:
            # An unclassified provider error fails only this original, never the collector;
            # archive faults still reach media_loop and a session-terminal error (as in chat_page)
            # still stops the collector. Class name only, never provider text.
            from telethon.errors import AuthKeyDuplicatedError,UnauthorizedError
            if isinstance(exc,database_errors()+(UnauthorizedError,AuthKeyDuplicatedError)): raise
            log.warning('media_transfer_failed error=%s',chat_error_evidence(exc)['error_class'])
            try:await asyncio.to_thread(self.archive.fail_media,job,'network_error',collector_lease=self.state.lease)
            except RuntimeError as lost:
                if str(lost)=='collector_lease_lost':raise
        finally:
            renewal.cancel(); download.cancel()
            await asyncio.gather(renewal,download,return_exceptions=True)
            with suppress(RuntimeError,OSError):
                await asyncio.to_thread(self.archive.release_media_reservation,job)
        return True

    def media_ingested(self):
        """Wakes an idle media loop. The state calls it, from any thread, after it stored a message with media."""
        wake=getattr(self,'_media_wake',None)
        if wake is None:return
        loop,event=wake
        with suppress(RuntimeError):loop.call_soon_threadsafe(event.set)  # a closed loop has no media loop left

    async def media_loop(self):
        backoff=DbBackoff(self)
        wake=asyncio.Event(); self._media_wake=(asyncio.get_running_loop(),wake)
        if self.state is not None:self.state.media_ingested=self.media_ingested
        while True:
            # Cleared before the claim, so an ingest during the claim ends the next wait at once.
            wake.clear()
            try:active=await self.media_once()
            except database_errors():
                # An unfinished job's lease expires and it is claimed again later.
                await backoff.wait(); continue
            backoff.reset()
            if not active:
                with suppress(TimeoutError):await asyncio.wait_for(wake.wait(),MEDIA_IDLE_SECONDS)

    def disk_floors(self):
        """Media and capture floors of this run per volume (ANYM-350)."""
        from .storage_admission import disk_floors
        places=[(w.root,w.reserve_bytes) for w in (self.writer,self.live_writer) if w is not None]
        places+=[(s.path,s.reserve_bytes) for s in (self.spool,getattr(self,'events_spool',None),
            getattr(self,'history_spool',None)) if s is not None and hasattr(s,'path') and hasattr(s,'reserve_bytes')]
        return disk_floors(places)

    async def publish_disk_floors(self):
        """Best effort, again each heartbeat: until it lands cold restores wait (ANYM-350)."""
        with suppress(Exception):
            await asyncio.to_thread(self.state.disk_floors,self.disk_floors())