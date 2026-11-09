"""Single-owner outbound pacing; the provider's required pause lives in PostgreSQL."""
import asyncio
from collections import deque
from contextlib import suppress
import contextvars
import logging
import re
import time

logger=logging.getLogger(__name__)

# ANYM-475: a FloodWait of at most this many seconds on an agent-requested read
# pauses only that read's method scope; a longer one pauses the whole account.
SCOPED_FLOOD_MAX_SECONDS=3600
# An unknown 420 answer without its own wait pauses the account this long.
UNKNOWN_FLOOD_SECONDS=60
# Per-scope admission: burst, refill seconds per request and requests per UTC day.
# In memory: a collector restart resets them (MESSENGER_OPERATIONS.md).
# ANYM-477: agent-requested chat history has its own scope, so its pause and budget never touch search.
# ANYM-681: the inspection of a message's inline keyboard has its own scope as well.
LIVE_BUDGETS={'search':(3,10.0,100),'chat_history':(3,10.0,300),'inspect_buttons':(3,10.0,300),
    'press_button':(3,5.0,200)}
# The deadline-bound CollectorState of the live read running in this task (release-042 B1).
LIVE_STATE=contextvars.ContextVar('live_state',default=None)
# A cooldown write is one transaction that PostgreSQL ends after this many seconds, so a stalled write
# never holds the lease fence; the caller waits for it at most this long plus RECORD_GRACE (release-042 B1).
RECORD_SECONDS=2.0
RECORD_GRACE=0.5
# ANYM-877: admission tiers of the non-send lane. Each tier goes before the next, FIFO inside a tier:
# someone waiting (agents' live reads, update polls marked live, agent commands), then live media,
# then background work (history reconcile, discovery, backfill media).
LIVE,LIVE_MEDIA,BACKGROUND=0,1,2
PHASE_TIERS={'provider_connect':LIVE,'provider_command':LIVE}
# A waiting lower tier is admitted at least once in every STARVATION_ADMISSIONS admissions.
STARVATION_ADMISSIONS=5


class TieredLock:
    """The non-send lane: one holder at a time, the next one chosen by tier.

    A lower tier that waited through its share of admissions is due and goes first, so live
    media and background work are never starved. A waiter cancelled in any tier takes no slot."""
    def __init__(self):
        self.holder=None
        self.waiters=[deque() for _ in (LIVE,LIVE_MEDIA,BACKGROUND)]
        self.skipped=[0,0,0]  # admissions of other tiers since this tier's last one, while it waited

    def locked(self):return self.holder is not None

    def waiting(self):
        for queue in self.waiters:
            while queue and queue[0].done():queue.popleft()  # a cancelled waiter's leftover
        return {tier for tier,queue in enumerate(self.waiters) if any(not f.done() for f in queue)}

    def best(self,holder=None):
        """The tier the next slot goes to, with the holder's tier as a candidate too; None if none waits."""
        waiting=self.waiting()|({holder} if holder is not None else set())
        if not waiting:return None
        top=min(waiting)
        # Earliest deadline first: the k-th most urgent lower tier is due when k others could still go first.
        lower=sorted((STARVATION_ADMISSIONS-1-self.skipped[tier],-tier) for tier in waiting if tier!=top)
        if any(slack<=rank for rank,(slack,_) in enumerate(lower)):return -lower[0][1]
        return top

    def admitted(self,tier):
        for other in self.waiting():
            if other!=tier:self.skipped[other]+=1
        self.skipped[tier]=0

    async def acquire(self,tier,*,first=False):
        if self.holder is None and not self.waiting():
            self.holder=tier;return
        future=asyncio.get_running_loop().create_future()
        (self.waiters[tier].appendleft if first else self.waiters[tier].append)(future)
        try:await future
        except asyncio.CancelledError:
            # Granted and cancelled before it ran: pass the lane on; else drop the waiter.
            if future.done() and not future.cancelled():self.release()
            else:
                with suppress(ValueError):self.waiters[tier].remove(future)
            raise

    def release(self,to=None):
        """Free the lane for the best waiter, or for tier `to` when it waits (a handoff's chosen tier)."""
        self.holder=None
        tier=to if to is not None and to in self.waiting() else self.best()
        if tier is None:return
        future=self.waiters[tier].popleft()
        self.holder=tier;future.set_result(None)

    def lane(self,tier):return Lane(self,tier)


class Lane:
    """One caller's hold on a TieredLock; it can give the slot to a waiter that outranks it."""
    def __init__(self,lock,tier):self.lock,self.tier,self.held=lock,tier,False

    async def __aenter__(self):
        await self.lock.acquire(self.tier);self.held=True
        return self

    async def __aexit__(self,*_):
        if self.held:self.held=False;self.lock.release()

    def outranked(self):
        best=self.lock.best(self.tier)
        return best is not None and best!=self.tier

    def admitted(self):self.lock.admitted(self.tier)

    async def handoff(self):
        """Give the lane to the waiter that outranks this caller; wait again at the head of its tier."""
        # The tier that outranked it gets the lane: the same choice it now makes with this caller back in line.
        to=self.lock.best(self.tier)
        self.held=False;self.lock.release(to)
        await self.lock.acquire(self.tier,first=True);self.held=True


class ProviderRateLimited(Exception):
    def __init__(self,seconds):
        self.seconds=max(1,int(seconds))
        super().__init__('rate_limited')


class LiveBudget:
    """Token bucket plus a UTC-day cap, counted per provider request."""
    def __init__(self,burst,refill,daily,*,clock=time.monotonic,wall=time.time):
        self.burst,self.refill,self.daily,self.clock,self.wall=burst,refill,daily,clock,wall
        self.tokens=float(burst);self.updated=clock();self.day=None;self.used=0

    def take(self,requests):
        """Spend requests and return 0, or return the seconds until they fit and spend nothing."""
        now=self.clock();self.tokens=min(self.burst,self.tokens+(now-self.updated)/self.refill);self.updated=now
        wall=self.wall();day=int(wall//86400)
        if day!=self.day:self.day,self.used=day,0
        if requests>self.burst:return 86400
        if self.used+requests>self.daily:return max(1,int((day+1)*86400-wall)+1)
        if self.tokens<requests:return max(1,int((requests-self.tokens)*self.refill+0.999))
        self.tokens-=requests;self.used+=requests
        return 0


def flood_seconds(exc):
    seconds=getattr(exc,'seconds',None)
    if type(seconds) is int and seconds>0:return seconds
    found=re.search(r'_(\d{1,10})\b',str(getattr(exc,'message','') or ''))
    return int(found.group(1)) if found else UNKNOWN_FLOOD_SECONDS


class RequestGovernor:
    def __init__(self,state,*,interval=1.0,clock=time.monotonic,sleep=asyncio.sleep,wall=time.time,budgets=None,
                 bound=None):
        self.state,self.interval,self.clock,self.sleep,self.wall=state,interval,clock,sleep,wall
        # Network completion is serial inside each lane, admission is shared.
        # A background download may wait on Telegram for minutes; it must not
        # hold the text-send lane for that entire network wait (ANYM-655).
        self.lock=TieredLock(); self.send_lock=asyncio.Lock()
        self.pacing=asyncio.Lock(); self.next_call=0
        # bound(state, monotonic deadline) gives the same state with server-bounded transactions.
        self.bound=bound
        self.unrecorded=0  # monotonic end of an account cooldown whose write failed; retried before reads
        self.scoped={}  # scope -> monotonic end of a method-scoped FloodWait
        self.rings=set()  # cooldown ring writes still running (ANYM-877)
        self.budgets={scope:LiveBudget(*limits,clock=clock,wall=wall) for scope,limits in (budgets or LIVE_BUDGETS).items()}

    @property
    def state(self):
        live=LIVE_STATE.get()
        return live if live is not None and getattr(live,'account',None)==getattr(self._state,'account',None) else self._state

    @state.setter
    def state(self,value):self._state=value

    async def wait(self):
        # Re-read after waking: another operation/operator may extend the pause.
        # Cancellation interrupts sleep immediately; no provider/lease is opened here.
        while True:
            remaining=await self.account_remaining()
            if remaining<=0:return
            await self.sleep(min(remaining,30))

    def scoped_remaining(self,scope):
        return max(0.0,self.scoped.get(scope,0)-self.clock())

    async def admit(self,scope,requests):
        """Seconds the scope must wait (account cooldown, its own FloodWait, its budget) or 0.

        Never sleeps and never calls the provider; a refusal spends no budget."""
        remaining=max(self.scoped_remaining(scope),await self.account_remaining())
        if remaining>0:return max(1,int(remaining+0.999))
        return self.budgets[scope].take(requests)

    async def account_remaining(self):
        """Seconds of account cooldown: the persisted one, or an unwritten one kept in memory."""
        left=self.unrecorded-self.clock()
        if left>0:await self._write(max(1,int(left+0.999)))
        else:self.unrecorded=0
        remaining=await asyncio.to_thread(self.state.cooldown_remaining)
        return max(remaining,self.unrecorded-self.clock())

    async def _record(self,seconds,phase):
        # Commit BEFORE releasing the shared gate or propagating the error. The pause holds in memory
        # first, so a write that fails or times out still gates this process and is retried later.
        self.unrecorded=max(self.unrecorded,self.clock()+seconds)
        # The ring entry starts only once the cooldown is written, so a stalled database is never written twice
        # and its retry never waits behind the ring; a cancelled caller leaves the entry to land unawaited.
        rings=[]
        await self._write(seconds,written=lambda:rings.append(self._ring(seconds,phase)))
        if rings and rings[0] is not None:await asyncio.wait({rings[0]},timeout=RECORD_SECONDS+RECORD_GRACE)

    def _ring(self,seconds,phase):
        """ANYM-877: start appending the cooldown to the health ring; best effort, never in the cooldown's way."""
        if getattr(self._state,'record_cooldown',None) is None:return None
        state=self.bound(self._state,time.monotonic()+RECORD_SECONDS) if self.bound else self._state
        task=asyncio.ensure_future(asyncio.to_thread(state.record_cooldown,seconds,phase))
        self.rings.add(task);task.add_done_callback(self._ring_done)
        return task

    def _ring_done(self,task):
        self.rings.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning('cooldown_ring_failed exception_type=%s',type(task.exception()).__name__)

    async def _write(self,seconds,*,written=None):
        """Persist the cooldown in one transaction bounded on the server; never past RECORD_SECONDS.

        Not bound to a live read's deadline: the cooldown is recorded even after it. A cancelled caller
        still waits, as the transaction holds the lease fence until PostgreSQL ends it; the caller waits at
        most RECORD_SECONDS+RECORD_GRACE in total."""
        # Any database error (the server's transaction timeout is an InternalError) keeps the pause in
        # memory for a later retry; a lost lease is a RuntimeError and still raises.
        from psycopg import Error
        from psycopg_pool import PoolTimeout
        state=self.bound(self._state,time.monotonic()+RECORD_SECONDS) if self.bound else self._state
        recording=asyncio.ensure_future(asyncio.to_thread(state.defer,seconds))
        loop=asyncio.get_running_loop();end=loop.time()+RECORD_SECONDS+RECORD_GRACE
        cancelled=False
        try:await asyncio.wait({recording},timeout=RECORD_SECONDS+RECORD_GRACE)
        except asyncio.CancelledError:
            # One wait budget in total, also when cancelled.
            cancelled=True
            await asyncio.wait({recording},timeout=max(0,end-loop.time()))
        if not recording.done():
            # The server has ended the transaction; the stalled thread's late error is dropped.
            recording.add_done_callback(lambda t:t.cancelled() or t.exception())
            logger.warning('cooldown_write_failed exception_type=TimeoutError')
        elif isinstance(recording.exception(),(Error,PoolTimeout,TimeoutError)):
            logger.warning('cooldown_write_failed exception_type=%s',type(recording.exception()).__name__)
        else:
            recording.result()  # a lost lease still raises
            if self.unrecorded<=self.clock()+seconds:self.unrecorded=0
            if written is not None:written()
        if cancelled:raise asyncio.CancelledError

    async def _admission(self,phase,*,refusal=None,before=None,lane=None):
        while True:
            async with self.pacing:
                if await self._paced(phase,refusal,before,lane):return
            # ANYM-877: the slot opened while a higher tier (or a due lower one) waited: it goes first.
            await lane.handoff()

    async def _paced(self,phase,refusal,before,lane):
        while True:
            await (refusal() if refusal else self.wait())
            delay=self.next_call-self.clock()
            if delay>0:await self.sleep(delay)
            # A concurrent operator or the other in-flight lane can extend
            # the account pause while pacing or checking authorization.
            await (refusal() if refusal else self.wait())
            if lane is not None and lane.outranked():return False
            if before is not None:
                await before()
                if await self.account_remaining()>0:continue
            await asyncio.to_thread(self.state.status,'receiving',phase)
            if refusal:await refusal()
            elif await self.account_remaining()>0:continue
            self.next_call=self.clock()+self.interval
            if lane is not None:lane.admitted()
            return True

    async def call(self,operation,phase,*,before=None,tier=None):
        """One paced provider request; `tier` (LIVE, LIVE_MEDIA, BACKGROUND) defaults by phase."""
        if phase=='provider_send':
            # The send lane is its own; it shares only the pacing and the account cooldown.
            async with self.send_lock:return await self._attempt(operation,phase,before=before)
        async with self.lock.lane(PHASE_TIERS.get(phase,BACKGROUND) if tier is None else tier) as lane:
            return await self._attempt(operation,phase,before=before,lane=lane)

    async def _attempt(self,operation,phase,*,before=None,lane=None):
        from telethon.errors import FloodWaitError
        await self._admission(phase,before=before,lane=lane)
        try:return await operation()
        except (FloodWaitError,ProviderRateLimited) as exc:
            limited=ProviderRateLimited(exc.seconds)
            await self._record(limited.seconds,phase)
            raise limited from None

    async def scoped_call(self,operation,phase,scope):
        """One agent-requested read that never waits out or sets a pause for other work.

        It refuses (ProviderRateLimited) instead of waiting for an account or scope
        pause. Its own FloodWait up to SCOPED_FLOOD_MAX_SECONDS pauses only the scope;
        a longer one or any other 420 answer pauses the account as call() does."""
        from telethon.errors import FloodError,FloodWaitError
        async with self.lock.lane(LIVE) as lane:
            async def refusal():
                remaining=max(self.scoped_remaining(scope),await self.account_remaining())
                if remaining>0:raise ProviderRateLimited(remaining+0.999)
            await self._admission(phase,refusal=refusal,lane=lane)
            try:return await operation()
            except FloodWaitError as exc:
                limited=ProviderRateLimited(exc.seconds)
                if limited.seconds>SCOPED_FLOOD_MAX_SECONDS:await self._record(limited.seconds,phase)
                else:self.scoped[scope]=max(self.scoped.get(scope,0),self.clock()+limited.seconds)
                raise limited from None
            except (FloodError,ProviderRateLimited) as exc:
                limited=ProviderRateLimited(flood_seconds(exc))
                await self._record(limited.seconds,phase)
                raise limited from None
