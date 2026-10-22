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
