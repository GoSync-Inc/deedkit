"""The Telegram collector's live reads: a new group's catalog write, a confirmed edit's archive hand-off, an
agent-requested history page and the archiving of live search hits (moved from telegram_collector.py, W5-4c).

Tests patch STORE_GRACE, CATALOG_SECONDS, bounded_state and telegram_message on telegram_collector, so the moved
methods read those names, and the helpers defined there, on that module at call time (`tc`), never a copy taken at
import. Their log records keep telegram_collector.py's logger (collector_log).
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import datetime,timedelta,timezone
import importlib
import math
import sys
import time

from .collector_capture import CAPTURE_WRITES
from .collector_media import collector_log
from .envelope import stable_id

log=collector_log()
# Seconds before a live read's deadline in which no archive write starts (ANYM-477).
STORE_MARGIN=0.5
# ANYM-747: seconds a confirmed edit's archive hand-off (one spool write) may take inside the command's timeout.
EDIT_ARCHIVE_SECONDS=5.0
# Its own thread: a stalled hand-off write never holds a thread of the default executor (ANYM-747, Astra r1).
EDIT_ARCHIVE_EXECUTOR=ThreadPoolExecutor(max_workers=1,thread_name_prefix='edit-archive')


def collector_module():
    """telegram_collector.py itself: __main__ when Compose starts it with `python -m` (as collector_log reads it),
    else the imported module. Never a second copy of it."""
    name='messaging_service.telegram_collector'
    main=sys.modules.get('__main__')
    if getattr(getattr(main,'__spec__',None),'name',None)==name:return main
    return importlib.import_module(name)


class CollectorNames:
    """`tc.NAME` reads NAME on telegram_collector.py when the line runs, so a test's patch there applies."""
    def __getattr__(self,name):return getattr(collector_module(),name)


tc=CollectorNames()


class LiveReadsCollector:
    async def catalog_new_chat(self,job,answer):
        """A group this account just created or joined enters the archive catalog now, not at the next dialog
        discovery (15 minutes or more later; a new basic group's only message is a service message, which live
        capture skips), and its receipt names the archive chat_id. The provider change has already happened:
        a failure or an exhausted time budget here leaves the chat to that discovery and never changes the
        operation's outcome. A confirmed create_group receipt always says which (ANYM-750): `archive` is
        `collected` with `chat_id`, or `not_collected` with an `archive_reason` from a closed vocabulary and its
        fixed `next_step`; the collection policy's reason comes first, never exception text."""
        from psycopg.errors import LockNotAvailable,QueryCanceled,TransactionTimeout
        from psycopg_pool import PoolTimeout
        from telethon import types,utils
        data=answer.get('data') if isinstance(answer,dict) and answer.get('outcome')=='confirmed' else None
        if not isinstance(data,dict):return
        failure,policy='catalog_failed',job.get('_policy')
        try:
            chat=data.get('provider_chat_id')
            if not isinstance(chat,str) or job['operation']=='join_chat' and data.get('archive')!='collected':return
            _,peer=utils.resolve_id(int(chat))
            if peer not in (types.PeerChat,types.PeerChannel):return
            expected=job.get('expected_chat') if isinstance(job.get('expected_chat'),dict) else {}
            title=(job.get('parameters') or {}).get('title') if job['operation']=='create_group' else expected.get('title')
            chat_type='group' if job['operation']=='create_group' or expected.get('kind')=='group' else 'channel'
            # Inside the command's own timeout, so a slow archive never turns a confirmed change into unknown.
            budget=min(tc.CATALOG_SECONDS,job.get('_ends_at',math.inf)-time.monotonic()-tc.STORE_GRACE-1)
            if budget<=0:
                failure='catalog_timeout';return
            state=tc.bounded_state(self.state,time.monotonic()+budget)
            if await asyncio.wait_for(tc.finished(state.catalog,chat,'chat' if peer is types.PeerChat else 'channel',
                    title if isinstance(title,str) and title else chat,chat_type,self.history_since),budget):
                data['chat_id']=stable_id('chat',self.account,chat)
            # A refusal is named by the policy the write was judged by, not the one read before the provider call.
            policy=getattr(state,'evaluated_policy',policy)
        except asyncio.CancelledError:
            # A shutdown during the catalog write: the worker settles the confirmed receipt first, then stops.
            job['_cancel_after_settle']=True
            log.warning('new_chat_catalog_failed operation=%s error=CancelledError',job.get('operation'))
        except (TimeoutError,QueryCanceled,LockNotAvailable,TransactionTimeout,PoolTimeout) as exc:
            failure='catalog_timeout'
            log.warning('new_chat_catalog_failed operation=%s error=%s',job.get('operation'),type(exc).__name__)
        except Exception as exc:
            log.warning('new_chat_catalog_failed operation=%s error=%s',job.get('operation'),type(exc).__name__)
        finally:
            if job.get('operation')=='create_group':
                with suppress(Exception):tc.archive_receipt(data,policy,failure)

    async def queue_edit_for_archive(self,job,answer):
        """ANYM-747: a confirmed edit reaches the archive through the edit spool, which the drain loop applies with
        the live spool, so the drain makes it the next revision. Never through capture(): this is best-effort and
        total, it never fails the collector, and the operation stays confirmed whatever happens here (the live
        MessageEdited event and reconcile still apply). A capture slot is taken only when one is free at once (waiting
        for one could fail the collector) and is held until the spool write itself ends, also past a timeout. The
        write runs on its own one-thread executor, at most one at a time, under the edit spool's own lock: a stalled
        disk never takes the threads or the lock that receipts, sends, lease renewal and live captures use."""
        data=answer.get('data') if isinstance(answer,dict) and answer.get('outcome')=='confirmed' else None
        message=job.pop('_read_back_message',None)
        if not isinstance(data,dict) or message is None:return
        data['archive']='not_queued'
        try:
            budget=min(EDIT_ARCHIVE_SECONDS,job.get('_ends_at',math.inf)-time.monotonic()-tc.STORE_GRACE-1)
            if budget<=0 or self.stopping.is_set() or self.failed.is_set():return
            pending=getattr(self,'_edit_archive_write',None)
            if pending is not None and not pending.done():return
            if self.capture_count>=CAPTURE_WRITES or self.capture_waiters:return
            e=tc.telegram_message(self.account,message,verified_self=getattr(self.provider,'verified_self',None))
            policy=job.get('_policy') or self.policy
            if not policy or not self.state.allows(policy,e['chat']):return
            item={'kind':'message','peer_kind':tc.peer_kind(message),'envelope':e,
                'captured_at':datetime.now(timezone.utc).isoformat()}
            self.capture_count+=1
            try:
                # The property is read on that thread too: creating the directory is filesystem work.
                write=asyncio.get_running_loop().run_in_executor(EDIT_ARCHIVE_EXECUTOR,lambda:self.edit_spool.put(item))
            except BaseException:
                self.release_capture();raise
            self._edit_archive_write=write
            # The slot follows the write, not this call: a timed-out or cancelled wait leaves it held until the end.
            write.add_done_callback(lambda done:(self.release_capture(),done.cancelled() or done.exception()))
            await asyncio.wait_for(asyncio.shield(write),budget)
            data['archive']='queued'
        except asyncio.CancelledError:
            # A shutdown during the spool write: the worker settles the confirmed receipt first, then stops.
            job['_cancel_after_settle']=True
            log.warning('edit_archive_queue_failed error=CancelledError')
        except Exception as exc:
            log.warning('edit_archive_queue_failed error=%s',type(exc).__name__)

    async def chat_history(self,job):
        """ANYM-477: one agent-requested history page, archived as live search hits are; stored_new counts new rows."""
        parameters=job.get('parameters') or {}
        default=None
        if not any(k in parameters for k in ('before_message','around_message','from_time','to_time')):
            # The default anchor is the oldest archived message, so each repeat reaches further back. It is
            # kept here too: each provider request's preflight replaces the job's payload fields (release-042 M1).
            default=job['provider_message_id']=await asyncio.to_thread(self.oldest_archived,job.get('chat_id'))
        job['_history']=found=[]
        answer=await self.provider.execute_operation(job)
        # Owner 2026-10-23: an explicit request for one named chat also stores its page below history_since;
        # background collection and live search hits keep the boundary.
        created=await self.store_live_hits(found,label='chat_history',deadline=job.get('_deadline'),window=False)
        data=answer.get('data')
        if isinstance(data,dict):
            data['stored_new']=created
            below=sum(1 for m,_,_,_ in found if m is not None and m.date and not self.in_window([m]))
            if below:data['below_history_since']=below
            # not_stored with retry_parameters covers only storage errors and the deadline (F-477a).
            missed=sum(1 for _,hit,_,_ in found if not hit.get('stored'))
            if missed:
                # A later default request starts below the oldest archived message and would skip these,
                # so the receipt names the request that loads this page again (a rerun adds no duplicates):
                # the request's own anchor, the default anchor it used, or for a chat with no archived
                # message a time range over the messages not stored at the largest page.
                data['not_stored']=missed
                anchors=('before_message','around_message','from_time','to_time')
                if any(k in parameters for k in anchors):
                    data['retry_parameters']={k:v for k,v in parameters.items() if k in ('count',*anchors)}
                elif default:
                    data['retry_parameters']={'count':parameters.get('count',50),'before_message':
                        stable_id('msg',self.account,str(job.get('provider_chat')),str(default))}
                else:
                    dates=[m.date for m,hit,_,_ in found if m is not None and not hit.get('stored') and m.date]
                    if dates:
                        data['retry_parameters']={'count':100,
                            'from_time':min(dates).strftime('%Y-%m-%dT%H:%M:%SZ'),
                            'to_time':(max(dates)+timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%SZ')}
        return answer

    def oldest_archived(self,chat):
        with self.state.archive.store.pool.connection() as c:
            row=c.execute('''SELECT provider_id FROM messages WHERE account=%s AND chat=%s
                ORDER BY length(provider_id),provider_id LIMIT 1''',(self.account,chat)).fetchone()
        return row['provider_id'] if row else None

    async def store_live_hits(self,found,*,label='live_search',deadline=None,window=True):
        """ANYM-476: archive live search hits as backfill through the history page commit's archive apply.

        No spool: a hit that cannot be stored is reported in its receipt and is not replayed, so it never
        reaches the drain loop, fails the search or stops the collector. A rerun stores it again.
        Checkpoints and coverage stay with the history traversal. Returns the number of new messages.

        A live read's deadline (loop time) bounds it: no write starts in its last STORE_MARGIN seconds,
        every transaction ends at the deadline on the server (DeadlinePool), and a cancelled read waits
        at most STORE_GRACE for its thread, so no archive transaction holding the lease fence outlives
        the read's receipt or holds up the next send claim (ANYM-477 review F1)."""
        loop=asyncio.get_running_loop()
        state=self.state
        if deadline is not None:state=tc.bounded_state(self.state,time.monotonic()+deadline-loop.time())
        def late():return deadline is not None and deadline-loop.time()<STORE_MARGIN
        stored=skipped=failed=created=0
        for message,hit,chat_type,title in found:
            if window and message is not None and not self.in_window([message]):
                hit.update(stored=False,reason='outside_history_window'); skipped+=1; continue
            if late():
                hit.update(stored=False,reason='deadline'); failed+=1; continue
            outcome=None
            try:
                if message is None: raise ValueError('uninitialized_message')
                chat=str(message.chat_id);kind=tc.peer_kind(message)
                await tc.finished(state.catalog,chat,kind,title or chat,chat_type,self.history_since)
                e=tc.telegram_message(self.account,message,chat_title=title or '',origin='backfill',
                    verified_self=getattr(self.provider,'verified_self',None))
                if late():raise TimeoutError('live_store_deadline')
                outcome=(await tc.finished(state.apply,{'kind':'message','peer_kind':kind,'envelope':e}))['outcome']
            except Exception as exc:
                log.warning('%s_store_failed error=%s',label,type(exc).__name__)
            if outcome in (None,'excluded'):
                hit.update(stored=False,reason='not_stored'); failed+=1; continue
            hit.update(stored=True,message_id=stable_id('msg',self.account,chat,str(message.id))); stored+=1
            created+=outcome=='created'
        if found: log.info('%s_store stored=%d outside_window=%d not_stored=%d',label,stored,skipped,failed)
        return created
