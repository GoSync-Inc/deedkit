"""Single-owner outbound pacing; the provider's required pause lives in PostgreSQL."""
import asyncio
import re
import time

# ANYM-475: a FloodWait of at most this many seconds on an agent-requested read
# pauses only that read's method scope; a longer one pauses the whole account.
SCOPED_FLOOD_MAX_SECONDS=3600
# An unknown 420 answer without its own wait pauses the account this long.
UNKNOWN_FLOOD_SECONDS=60
# Per-scope admission: burst, refill seconds per request and requests per UTC day.
# In memory: a collector restart resets them (MESSENGER_OPERATIONS.md).
LIVE_BUDGETS={'search':(3,10.0,100)}


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
    def __init__(self,state,*,interval=1.0,clock=time.monotonic,sleep=asyncio.sleep,wall=time.time,budgets=None):
        self.state,self.interval,self.clock,self.sleep,self.wall=state,interval,clock,sleep,wall
        self.lock=asyncio.Lock(); self.next_call=0
        self.scoped={}  # scope -> monotonic end of a method-scoped FloodWait
        self.budgets={scope:LiveBudget(*limits,clock=clock,wall=wall) for scope,limits in (budgets or LIVE_BUDGETS).items()}

    async def wait(self):
        # Re-read after waking: another operation/operator may extend the pause.
        # Cancellation interrupts sleep immediately; no provider/lease is opened here.
        while True:
            remaining=await asyncio.to_thread(self.state.cooldown_remaining)
            if remaining<=0:return
            await self.sleep(min(remaining,30))

    def scoped_remaining(self,scope):
        return max(0.0,self.scoped.get(scope,0)-self.clock())

    async def admit(self,scope,requests):
        """Seconds the scope must wait (account cooldown, its own FloodWait, its budget) or 0.

        Never sleeps and never calls the provider; a refusal spends no budget."""
        remaining=max(self.scoped_remaining(scope),await asyncio.to_thread(self.state.cooldown_remaining))
        if remaining>0:return max(1,int(remaining+0.999))
        return self.budgets[scope].take(requests)

    async def _record(self,seconds):
        # Commit BEFORE releasing the shared gate or propagating the error.
        recording=asyncio.create_task(asyncio.to_thread(self.state.defer,seconds))
        try:await asyncio.shield(recording)
        except asyncio.CancelledError:
            # to_thread cancellation does not stop its SQL. Wait for the
            # bounded transaction before shutdown releases account ownership.
            await recording
            raise

    async def call(self,operation,phase):
        from telethon.errors import FloodWaitError
        async with self.lock:
            await self.wait()
            delay=self.next_call-self.clock()
            if delay>0:await self.sleep(delay)
            # A concurrent operator can extend the persisted cooldown while pacing.
            await self.wait()
            await asyncio.to_thread(self.state.status,'receiving',phase)
            self.next_call=self.clock()+self.interval
            try:return await operation()
            except (FloodWaitError,ProviderRateLimited) as exc:
                limited=ProviderRateLimited(exc.seconds)
                await self._record(limited.seconds)
                raise limited from None

    async def scoped_call(self,operation,phase,scope):
        """One agent-requested read that never waits out or sets a pause for other work.

        It refuses (ProviderRateLimited) instead of waiting for an account or scope
        pause. Its own FloodWait up to SCOPED_FLOOD_MAX_SECONDS pauses only the scope;
        a longer one or any other 420 answer pauses the account as call() does."""
        from telethon.errors import FloodError,FloodWaitError
        async with self.lock:
            async def refusal():
                remaining=max(self.scoped_remaining(scope),await asyncio.to_thread(self.state.cooldown_remaining))
                if remaining>0:raise ProviderRateLimited(remaining+0.999)
            await refusal()
            delay=self.next_call-self.clock()
            if delay>0:await self.sleep(delay)
            await refusal()
            await asyncio.to_thread(self.state.status,'receiving',phase)
            self.next_call=self.clock()+self.interval
            try:return await operation()
            except FloodWaitError as exc:
                limited=ProviderRateLimited(exc.seconds)
                if limited.seconds>SCOPED_FLOOD_MAX_SECONDS:await self._record(limited.seconds)
                else:self.scoped[scope]=max(self.scoped.get(scope,0),self.clock()+limited.seconds)
                raise limited from None
            except (FloodError,ProviderRateLimited) as exc:
                limited=ProviderRateLimited(flood_seconds(exc))
                await self._record(limited.seconds)
                raise limited from None
