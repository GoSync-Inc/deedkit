"""The Telegram collector's live capture path: the history and edit spools, the capture write slots (ANYM-291), the
rejection record, and the deletion, owner-read and receipt callbacks (moved from telegram_collector.py, W5-4b;
WhatsAppCollector inherits it). capture() and deferred_receipts stay there, beside the telegram_message and
DEFERRED_RECEIPT_BATCH they read: tests patch both names on that module.
"""
import asyncio
from contextlib import suppress
from datetime import datetime,timezone
import re

from .collector_state import LIVE_REJECT_CODES
from .spool import InvalidCapture

# Deterministic failures of a pure parser on malformed provider input. Resource,
# I/O and unexpected errors are not among them and still fail capture closed.
PARSE_ERRORS=(ValueError,TypeError,KeyError,AttributeError,IndexError,RecursionError)
# Live capture backpressure (ANYM-291): at most CAPTURE_WRITES spool writes run at once;
# further callbacks wait in arrival order. More than CAPTURE_WAITERS waiting callbacks, or one
# wait longer than CAPTURE_WAIT_SECONDS, fails the collector closed with capture_overload.
CAPTURE_WRITES=16; CAPTURE_WAITERS=512; CAPTURE_WAIT_SECONDS=30


class CaptureCollector:
    @property
    def history_spool(self):
        # Replayed history pages keep capture durability, but a pending page is
        # not live capture lag. Created lazily beside the live spool.
        if getattr(self,'_history_spool',None) is None:
            from .spool import DurableSpool
            live=self.spool
            self._history_spool=DurableSpool(live.path/'history',validator=live.validator,
                max_bytes=live.max_bytes,max_entries=live.max_entries,reserve_bytes=live.reserve_bytes)
        return self._history_spool

    @property
    def edit_spool(self):
        # ANYM-747: a confirmed edit's archive hand-off, beside the live spool with its own publication lock, so a
        # stalled hand-off write never holds the lock live captures take. Drained with the live spool.
        if getattr(self,'_edit_spool',None) is None:
            from .spool import DurableSpool
            live=self.spool
            self._edit_spool=DurableSpool(live.path/'edits',validator=live.validator,
                max_bytes=live.max_bytes,max_entries=live.max_entries,reserve_bytes=live.reserve_bytes)
        return self._edit_spool

    def capture_failed(self,exc):
        # Capacity, I/O and anything unexpected fail closed; only the cause class is kept.
        detail=(str(exc) if isinstance(exc,RuntimeError) and str(exc) in ('spool_full','disk_reserve')
            else 'spool_io_error' if isinstance(exc,OSError) else 'capture_error')
        self.fail('durable_capture_failed',detail)

    async def capture_slot(self):
        """Take one capture write slot, waiting in arrival order; False when the collector stops instead."""
        if self.capture_count<CAPTURE_WRITES and not self.capture_waiters:
            self.capture_count+=1; return True
        if len(self.capture_waiters)>=CAPTURE_WAITERS: self.fail('capture_overload','waiters_limit'); return False
        waiter=asyncio.get_running_loop().create_future(); self.capture_waiters.append(waiter)
        try:
            async with asyncio.timeout(self.capture_wait_seconds): await waiter
        except BaseException as exc:
            # A slot granted in the same tick as the timeout or a cancel is kept or passed on, never leaked.
            granted=waiter.done() and not waiter.cancelled()
            if not granted:
                with suppress(ValueError): self.capture_waiters.remove(waiter)
                self.grant_captures()
            if not isinstance(exc,TimeoutError):
                if granted: self.release_capture()
                raise
            if not granted: self.fail('capture_overload','slot_wait_timeout'); return False
        if self.stopping.is_set() or self.failed.is_set(): self.release_capture(); return False
        return True

    def release_capture(self):
        self.capture_count-=1; self.grant_captures()

    def grant_captures(self):
        # Free slots go to the oldest waiters; a granted waiter already counts as a running write.
        while self.capture_count<CAPTURE_WRITES and self.capture_waiters:
            waiter=self.capture_waiters.popleft()
            if not waiter.done(): waiter.set_result(None); self.capture_count+=1

    def reject(self,code,chat=None,message_id=None):
        """Count one deterministically unstorable live item; a restart cannot fix it."""
        # Only fixed codes reach status, never text that merely looks like one.
        if code not in LIVE_REJECT_CODES:code='invalid_capture'
        self.rejected[code]=self.rejected.get(code,0)+1
        if (chat is not None and type(message_id) is int and 0<message_id<2**63
                and re.fullmatch(r'-?[0-9]{1,20}',chat) and self.policy and self.state.allows(self.policy,chat)):
            # Identity only, so reconciliation or an operator can re-fetch it later.
            self.rejected_messages=[*self.rejected_messages,{'chat':chat,'message_id':message_id,
                'reason_code':code,'observed_at':datetime.now(timezone.utc).isoformat()}][-20:]

    async def deleted(self,event):
        if self.stopping.is_set() or self.failed.is_set(): return
        if not await self.capture_slot(): return
        try:
            chat=str(event.chat_id) if event.chat_id is not None else ''
            if chat and not self.state.allows(self.policy,chat): return
            ids=list(event.deleted_ids); observed=datetime.now(timezone.utc).isoformat()
            # One update may delete more ids than a record holds; store it in parts.
            for start in range(0,max(1,len(ids)),1000):
                try:
                    await asyncio.to_thread(self.spool.put,{'kind':'telegram_delete','account':self.account,
                        'chat':chat,'ids':ids[start:start+1000],'observed_at':observed})
                except InvalidCapture as exc: self.reject(str(exc))
        except Exception as exc: self.capture_failed(exc)
        finally: self.release_capture()

    async def owner_read(self,event):
        if self.stopping.is_set() or self.failed.is_set():return
        pending=False
        try:
            from .reply_state import telegram_read_capture
            try:
                capture=telegram_read_capture(self.account,event,verified_self=getattr(self.provider,'verified_self',None),
                    expected_user_id=getattr(self.provider,'expected_user_id',None))
            except PARSE_ERRORS as exc:
                if isinstance(exc,OSError):raise
                self.reject('read_parse_failed');return
            if capture is None or not self.policy or self.policy.get('account')!=self.account:return
            if not self.state.allows(self.policy,capture['chat']):return
            if not await self.capture_slot():return
            pending=True
            await asyncio.to_thread(self.spool.put,capture)
        except InvalidCapture as exc:self.reject(str(exc))
        except Exception as exc:self.capture_failed(exc)
        finally:
            if pending:self.release_capture()

    async def receipt(self,event,*,normalize=None):
        if self.stopping.is_set() or self.failed.is_set(): return
        pending=False
        try:
            from .telegram_receipts import UnsupportedReceiptPeer,telegram_receipt
            try:
                capture=(normalize or telegram_receipt)(self.account,event,
                    verified_self=getattr(self.provider,'verified_self',None),
                    expected_user_id=getattr(self.provider,'expected_user_id',None))
            except UnsupportedReceiptPeer as exc:self.reject(exc.code);return
            except PARSE_ERRORS as exc:
                if isinstance(exc,OSError): raise
                self.reject('receipt_parse_failed'); return
            if capture is None: return
            if not self.policy or self.policy.get('account')!=self.account: return
            if getattr(self.state,'account',self.account)!=self.account: return
            if not self.state.allows(self.policy,capture['chat']): return
            if not await self.capture_slot(): return
            pending=True
            await asyncio.to_thread(self.spool.put,capture)
        except InvalidCapture as exc: self.reject(str(exc))
        except Exception as exc: self.capture_failed(exc)
        finally:
            if pending:self.release_capture()

    async def group_receipt(self,event):
        """A group's outbox boundary (ANYM-868) through the private receipt's own spool, policy and capture slot."""
        from .telegram_receipts import telegram_group_receipt
        await self.receipt(event,normalize=telegram_group_receipt)
