"""Outbound fake providers only: no credentials, real clients, network or clock sleeps."""
import asyncio
from threading import Event
from types import SimpleNamespace as NS

import pytest
from telethon.errors import FloodWaitError

from messaging_service.telegram_governor import ProviderRateLimited,RequestGovernor
from messaging_service.telegram_collector import TelegramCollector,TelethonProvider


class Clock:
    def __init__(self):self.now=0;self.sleeps=[]
    def __call__(self):return self.now
    async def sleep(self,seconds):
        self.sleeps.append(seconds);self.now+=seconds
        await asyncio.sleep(0)


class State:
    def __init__(self,clock):self.clock=clock;self.until=0;self.phases=[]
    def cooldown_remaining(self):return max(0,self.until-self.clock())
    def defer(self,seconds):self.until=max(self.until,self.clock()+seconds)
    def status(self,status,phase):self.phases.append((status,phase))
    def phase(self,name,state,code,**identity):self.phases.append((name,state,code))
    def connection_gate(self):return {'blocked':False,'remaining_seconds':0}


@pytest.mark.parametrize('real_error',[False,True])
def test_flood_pause_survives_new_governor_and_gates_other_operations(real_error):
    async def scenario():
        clock=Clock();state=State(clock)
        governor=RequestGovernor(state,clock=clock,sleep=clock.sleep)
        async def flooded():
            raise FloodWaitError(None,capture=120) if real_error else ProviderRateLimited(120)
        with pytest.raises(ProviderRateLimited):await governor.call(flooded,'telegram_history')
        assert state.until==120
        # A fresh process/governor uses the persisted deadline, not its own memory.
        restarted=RequestGovernor(state,clock=clock,sleep=clock.sleep)
        calls=[]
        async def media():calls.append(clock());return 'file'
        assert await restarted.call(media,'telegram_media')=='file'
        assert calls==[120]
        assert await restarted.call(media,'telegram_discovery')=='file'
        assert calls==[120,121]
    asyncio.run(scenario())


def test_shared_gate_blocks_media_until_history_flood_is_recorded():
    async def scenario():
        clock=Clock();state=State(clock);gate=RequestGovernor(state,clock=clock,sleep=clock.sleep)
        entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def history():
            entered.set();await release.wait();raise ProviderRateLimited(90)
        async def media():calls.append(clock())
        first=asyncio.create_task(gate.call(history,'telegram_history'))
        await entered.wait()
        second=asyncio.create_task(gate.call(media,'telegram_media'))
        await asyncio.sleep(0);assert not calls
        release.set()
        with pytest.raises(ProviderRateLimited):await first
        await second
        assert calls==[90]
    asyncio.run(scenario())


def tiered(clock,state=None):
    return RequestGovernor(state or State(clock),clock=clock,sleep=clock.sleep)


def busy(gate,clock,log,name,tier,phase='telegram_history'):
    """A caller that always waits again at once: an endless reconcile, media or live queue."""
    async def loop():
        async def request():log.append((name,clock()))
        while True:await gate.call(request,phase,tier=tier)
    return asyncio.create_task(loop())


async def admitted(log,name,count):
    while sum(1 for entry in log if entry[0]==name)<count:await asyncio.sleep(0.001)


async def stop(*tasks):
    for task in tasks:task.cancel()
    await asyncio.gather(*tasks,return_exceptions=True)


# spec: ANYM-877
def test_a_one_chunk_live_file_finishes_in_two_intervals_while_reconcile_always_waits():
    from messaging_service.telegram_governor import BACKGROUND,LIVE_MEDIA
    async def scenario():
        clock=Clock();gate=tiered(clock);log=[]
        reconcile=[busy(gate,clock,log,'reconcile',BACKGROUND) for _ in range(3)]
        await admitted(log,'reconcile',6)
        async def media():log.append(('media',clock()))
        start=clock()
        for _ in range(2):await gate.call(media,'telegram_media',tier=LIVE_MEDIA)  # get_messages, the one chunk
        assert [at for name,at in log if name=='media'][-1]<=start+2
        await stop(*reconcile)
        assert not gate.lock.locked()
    asyncio.run(scenario())


# spec: ANYM-877
def test_an_agent_read_during_a_twenty_file_burst_is_admitted_at_the_next_interval():
    from messaging_service.telegram_governor import BACKGROUND,LIVE_MEDIA
    async def scenario():
        clock=Clock();gate=tiered(clock);log=[]
        reconcile=busy(gate,clock,log,'reconcile',BACKGROUND)
        async def burst():
            async def part():log.append(('media',clock()))
            for _ in range(21):await gate.call(part,'telegram_media',tier=LIVE_MEDIA)
        media=asyncio.create_task(burst())
        await admitted(log,'media',8)
        async def read():log.append(('agent',clock()));return 'page'
        arrived=clock()
        assert await gate.scoped_call(read,'live_search','search')=='page'
        # The next slot, or the one after it when that slot is reconcile's due floor slot.
        at=[at for name,at in log if name=='agent'][0]
        assert at<=arrived+1 or at==arrived+2 and ('reconcile',arrived+1) in log,(arrived,log)
        await media;await stop(reconcile)
    asyncio.run(scenario())


# spec: ANYM-877
@pytest.mark.parametrize('flood,callers',[('live',100),('live_media',100),('live',1)],
    ids=['live','live_media','one_live_loop'])
def test_with_a_hundred_queued_live_calls_lower_tiers_get_one_admission_in_every_five(flood,callers):
    from messaging_service.telegram_governor import BACKGROUND,LIVE,LIVE_MEDIA
    async def scenario():
        clock=Clock();gate=tiered(clock);log=[]
        lower=['reconcile']+(['media'] if flood=='live' else [])
        tasks=[busy(gate,clock,log,'reconcile',BACKGROUND)]
        if flood=='live':tasks.append(busy(gate,clock,log,'media',LIVE_MEDIA,'telegram_media'))
        top=LIVE if flood=='live' else LIVE_MEDIA
        tasks+=[busy(gate,clock,log,'flood',top,'provider_command' if flood=='live' else 'telegram_media')
            for _ in range(callers)]
        await admitted(log,'flood',200)
        await stop(*tasks)
        names=[name for name,_ in log]
        times=[at for _,at in log]
        assert times==sorted(times) and len(set(times))==len(times)  # one admission per interval, all tiers
        for start in range(len(names)-4):
            window=names[start:start+5]
            for name in lower:assert name in window,(start,window)
        assert names.count('flood')>=len(names)//2  # the top tier still goes first
    asyncio.run(scenario())


# spec: ANYM-877
@pytest.mark.parametrize('granted',[False,True])
@pytest.mark.parametrize('tier',[0,1,2])
def test_a_cancelled_waiter_in_any_tier_leaves_no_slot_taken(tier,granted):
    from messaging_service.telegram_governor import BACKGROUND
    async def scenario():
        clock=Clock();gate=tiered(clock);entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def holding():entered.set();await release.wait()
        holder=asyncio.create_task(gate.call(holding,'telegram_history'))
        await entered.wait()
        async def cancelled():calls.append('cancelled')
        waiter=asyncio.create_task(gate.call(cancelled,'telegram_media',tier=tier))
        await asyncio.sleep(0)
        if granted:
            # The holder hands the lane to this waiter, which is cancelled before it runs.
            release.set()
            while gate.lock.holder!=tier or not holder.done():await asyncio.sleep(0)
            waiter.cancel()
        else:
            waiter.cancel();await asyncio.sleep(0);release.set()
        await asyncio.gather(holder,waiter,return_exceptions=True)
        async def later():calls.append('later')
        await gate.call(later,'telegram_history',tier=BACKGROUND)
        assert calls==['later'] and not gate.lock.locked()
        assert not any(gate.lock.waiters)
    asyncio.run(scenario())


# spec: ANYM-877
def test_an_account_cooldown_or_flood_wait_stops_every_tier():
    from messaging_service.telegram_governor import BACKGROUND,LIVE,LIVE_MEDIA
    async def scenario():
        clock=Clock();state=State(clock);gate=tiered(clock,state);calls=[]
        async def flooded():raise FloodWaitError(None,capture=30)
        with pytest.raises(ProviderRateLimited):await gate.call(flooded,'telegram_media',tier=LIVE_MEDIA)
        assert state.until==30
        async def read():raise AssertionError('no provider request during the pause')
        with pytest.raises(ProviderRateLimited):await gate.scoped_call(read,'live_search','search')
        for tier in (LIVE,LIVE_MEDIA,BACKGROUND):
            async def request(tier=tier):calls.append((tier,clock()))
            await gate.call(request,'telegram_history',tier=tier)
        assert calls==[(LIVE,30),(LIVE_MEDIA,31),(BACKGROUND,32)]
    asyncio.run(scenario())


# spec: ANYM-877
def test_every_account_cooldown_is_appended_to_the_cooldown_ring_with_its_phase():
    from telethon.errors import FloodError
    from messaging_service.telegram_governor import SCOPED_FLOOD_MAX_SECONDS
    async def scenario():
        clock=Clock();state=State(clock);ring=[]
        state.record_cooldown=lambda seconds,phase:ring.append((seconds,phase))
        gate=tiered(clock,state)
        async def flood_wait():raise FloodWaitError(None,capture=20)
        with pytest.raises(ProviderRateLimited):await gate.call(flood_wait,'telegram_history')
        clock.now+=20
        async def unknown():raise FloodError(None,'FLOOD')
        with pytest.raises(ProviderRateLimited):await gate.scoped_call(unknown,'live_search','search')
        clock.now+=60
        async def short():raise FloodWaitError(None,capture=5)
        with pytest.raises(ProviderRateLimited):await gate.scoped_call(short,'live_search','chat_history')
        async def long():raise FloodWaitError(None,capture=SCOPED_FLOOD_MAX_SECONDS+1)
        with pytest.raises(ProviderRateLimited):await gate.scoped_call(long,'live_search','inspect_buttons')
        # A scope-only wait is no account cooldown, so the ring has three entries.
        assert ring==[(20,'telegram_history'),(60,'live_search'),(SCOPED_FLOOD_MAX_SECONDS+1,'live_search')]
    asyncio.run(scenario())


# spec: ANYM-877
def test_a_failing_cooldown_ring_write_never_changes_the_cooldown():
    async def scenario():
        clock=Clock();state=State(clock)
        def broken(seconds,phase):raise RuntimeError('synthetic')
        state.record_cooldown=broken
        gate=tiered(clock,state)
        async def flooded():raise ProviderRateLimited(40)
        with pytest.raises(ProviderRateLimited):await gate.call(flooded,'telegram_history')
        assert state.until==40 and not gate.lock.locked()
    asyncio.run(scenario())


def test_cancelled_cooldown_never_calls_provider():
    async def scenario():
        clock=Clock();state=State(clock);state.defer(3600)
        sleeping=asyncio.Event();called=[]
        async def sleep(_):sleeping.set();await asyncio.Event().wait()
        async def operation():called.append(True)
        gate=RequestGovernor(state,clock=clock,sleep=sleep)
        task=asyncio.create_task(gate.call(operation,'telegram_history'))
        await sleeping.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert not called and state.until==3600
    asyncio.run(scenario())


def test_cancellation_waits_for_flood_persistence_before_releasing_gate():
    async def scenario():
        clock=Clock();state=State(clock);entered=Event();release=Event()
        def defer(seconds):
            entered.set();assert release.wait(3);state.until=clock()+seconds
        state.defer=defer
        gate=RequestGovernor(state,clock=clock,sleep=clock.sleep)
        async def flooded():raise ProviderRateLimited(120)
        task=asyncio.create_task(gate.call(flooded,'telegram_history'))
        try:
            assert await asyncio.to_thread(entered.wait,1)
            task.cancel();await asyncio.sleep(0)
            assert not task.done() and gate.lock.locked()
        finally:release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert state.until==120 and not gate.lock.locked()
    asyncio.run(scenario())


def test_connect_wait_is_outside_network_deadline():
    async def scenario():
        clock=Clock();state=State(clock);state.defer(300)
        gate=RequestGovernor(state,clock=clock,sleep=clock.sleep);calls=[]
        async def connect():calls.append(clock())
        collector=TelegramCollector.__new__(TelegramCollector)
        collector.state=state
        collector.provider=NS(governor=gate,connect=connect);collector.connect_timeout=.01
        await collector.connect_provider()
        assert calls==[300]
        closed=[]
        async def hung():
            try:await asyncio.Event().wait()
            finally:closed.append(True)
        collector.provider.connect=hung
        with pytest.raises(TimeoutError,match='provider_connect_timeout'):await collector.connect_provider()
        assert closed==[True]
    asyncio.run(scenario())


def test_connect_flood_is_persisted_and_retried_without_false_pairing_failure():
    async def scenario():
        clock=Clock();state=State(clock);gate=RequestGovernor(state,clock=clock,sleep=clock.sleep)
        calls=[]
        async def connect():
            calls.append(clock())
            if len(calls)==1:raise FloodWaitError(None,capture=10)
        async def me():return NS(id=42)
        async def catch_up():pass
        provider=TelethonProvider.__new__(TelethonProvider)
        provider.client=NS(connect=connect,get_me=me,catch_up=catch_up)
        provider.governor=gate;provider.expected_user_id=42
        collector=TelegramCollector.__new__(TelegramCollector)
        collector.state=state
        collector.provider=provider;collector.connect_timeout=1
        await collector.connect_provider()
        assert calls==[0,10] and state.until==10
        assert provider.verified_self.id==42
    asyncio.run(scenario())


def test_connect_does_not_retain_verified_owner_after_identity_mismatch():
    async def scenario():
        async def connect():pass
        async def me():return NS(id=99)
        async def catch_up():raise AssertionError('identity must be checked first')
        provider=TelethonProvider.__new__(TelethonProvider)
        provider.client=NS(connect=connect,get_me=me,catch_up=catch_up)
        provider.expected_user_id=42;provider.verified_self=NS(id=42)
        with pytest.raises(RuntimeError,match='telegram_identity_mismatch'):await provider.connect()
        assert provider.verified_self is None
    asyncio.run(scenario())


def test_discovery_flushes_partial_page_and_retries_same_iterator_after_flood():
    async def scenario():
        clock=Clock();state=State(clock)
        gate=RequestGovernor(state,interval=0,clock=clock,sleep=clock.sleep)
        class Iterator:
            def __init__(self):self.buffer=None;self.index=0;self.loads=0
            def __aiter__(self):return self
            async def __anext__(self):
                if self.buffer is None or self.index==len(self.buffer):
                    self.loads+=1
                    if self.loads==2:raise FloodWaitError(None,capture=60)
                    if self.loads==4:raise StopAsyncIteration
                    ids=[1,2] if self.loads==1 else [3]
                    self.buffer=[NS(id=i,is_channel=False,is_user=True,is_group=False,name='fixture') for i in ids]
                    self.index=0
                item=self.buffer[self.index];self.index+=1;return item
        iterator=Iterator();opens=[]
        def dialogs(**kwargs):opens.append(kwargs);return iterator
        provider=TelethonProvider.__new__(TelethonProvider)
        provider.client=NS(iter_dialogs=dialogs);provider.governor=gate
        batches=provider.dialog_batches(None)
        assert [row[0] for row in await anext(batches)]==['1','2']
        assert state.until==60 and clock()==0
        assert [row[0] for row in await anext(batches)]==['3']
        assert clock()==60 and len(opens)==1
        with pytest.raises(StopAsyncIteration):await anext(batches)
    asyncio.run(scenario())


# spec: ANYM-475
def test_a_failed_cooldown_write_keeps_the_pause_and_is_retried_until_written():
    from psycopg.errors import TransactionTimeout
    async def scenario():
        clock=Clock();state=State(clock);failures=[TransactionTimeout('synthetic'),TransactionTimeout('synthetic')]
        persist=state.defer
        def defer(seconds):
            if failures:raise failures.pop()
            persist(seconds)
        state.defer=defer
        gate=RequestGovernor(state,clock=clock,sleep=clock.sleep)
        async def flooded():raise ProviderRateLimited(120)
        # The server's transaction timeout is not a collector-stopping error: the caller sees the FloodWait.
        with pytest.raises(ProviderRateLimited):await gate.call(flooded,'telegram_history')
        assert state.until==0 and await gate.admit('search',1)==120  # retry fails again; the pause holds
        async def read():raise AssertionError('no provider request during the pause')
        with pytest.raises(ProviderRateLimited):await gate.scoped_call(read,'live_search','search')
        assert state.until==120 and gate.unrecorded==0  # that refusal's retry persisted it
    asyncio.run(scenario())


# spec: ANYM-475
def test_a_cancelled_cooldown_write_waits_one_bounded_budget(monkeypatch):
    import time
    from messaging_service import telegram_governor
    monkeypatch.setattr(telegram_governor,'RECORD_SECONDS',0.2);monkeypatch.setattr(telegram_governor,'RECORD_GRACE',0.1)
    async def scenario():
        clock=Clock();state=State(clock);entered=Event();release=Event()
        def defer(seconds):entered.set();release.wait(3)
        state.defer=defer
        gate=RequestGovernor(state,clock=clock,sleep=clock.sleep)
        async def flooded():raise ProviderRateLimited(120)
        task=asyncio.create_task(gate.call(flooded,'telegram_history'))
        try:
            assert await asyncio.to_thread(entered.wait,1)
            began=time.monotonic();await asyncio.sleep(0.2);task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            elapsed=time.monotonic()-began
        finally:release.set()
        # One budget of 0.3 s from the write start; a fresh budget after cancellation would be 0.5 s.
        assert elapsed<0.42 and not gate.lock.locked() and gate.unrecorded==120
    asyncio.run(scenario())
