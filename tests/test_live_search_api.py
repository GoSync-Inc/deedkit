"""ANYM-475: live search authorization, session check and cursor binding at the operations API (synthetic DB)."""
import asyncio

import pytest

from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState
from messaging_service.envelope import stable_id
from messaging_service.store import APIError
from test_archive import event

ACCOUNT = 'live-search-fixture'


@pytest.fixture
def live(seeded):
    s, _, _ = seeded
    archive = Archive(s)
    archive.register(ACCOUNT, 'telegram', 'Synthetic live search', discover=True)
    for chat in ('100', '200'):
        archive.ingest(event(account=ACCOUNT, chat=chat, provider_id='1', direction='incoming', body='Synthetic'))
    key = s.issue_key('live-agent', [ACCOUNT], permissions=['read', 'read-live'])
    epoch = archive.acquire(ACCOUNT, 'synthetic-live-worker')
    state = CollectorState(archive, ACCOUNT, ('synthetic-live-worker', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    s.set_messenger_operations(ACCOUNT, ['search_messages_live'])
    yield s, archive, key, state
    archive.release(ACCOUNT, 'synthetic-live-worker', epoch)


def chat(provider):
    return stable_id('chat', ACCOUNT, provider)


def search(s, key, request_id='synthetic-live-0001', chat_id='', **parameters):
    return s.execute_messenger_operation(key, account=ACCOUNT, operation='search_messages_live', chat_id=chat_id,
                                         parameters={'query': 'synthetic', **parameters}, request_id=request_id)


async def settled(worker):
    """One worker turn, then the live receipt it writes off the foreground path."""
    active = await worker.once(); await asyncio.gather(*list(worker.settling)); return active


def queued(s):
    with s.pool.connection() as c:
        return c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n']


# spec: ANYM-475
def test_global_needs_full_account_and_chat_must_be_in_scope(live):
    s, _, key, _ = live
    assert search(s, key)['status'] == 'queued'
    assert search(s, key, 'synthetic-live-0002', chat_id=chat('100'))['status'] == 'queued'
    s.set_access('live-agent', chats=[chat('100')])
    with pytest.raises(APIError, match='forbidden'):
        search(s, key, 'synthetic-live-0003')
    for outside in (chat('200'), chat('999')):  # out of scope and unknown look the same
        with pytest.raises(APIError, match='forbidden'):
            search(s, key, 'synthetic-live-0004', chat_id=outside)
    assert search(s, key, 'synthetic-live-0005', chat_id=chat('100'))['status'] == 'queued'


# spec: ANYM-475
def test_unavailable_session_is_refused_at_once_and_nothing_is_queued(live):
    s, _, key, state = live
    state.phase('connection', 'retry_wait', 'provider_disconnected')
    with pytest.raises(APIError, match='session_unavailable'):
        search(s, key)
    assert queued(s) == 0


# spec: ANYM-475
def test_a_well_formed_cursor_is_queued_for_the_collector_to_check(live):
    s, _, key, _ = live
    # The binding lives with the cursor in collector memory (test_live_search.py); the API checks the form.
    assert search(s, key, cursor='synthetic_cursor_token_0001')['status'] == 'queued'
    with pytest.raises(APIError):
        search(s, key, 'synthetic-live-0002', cursor='not a cursor!')
    assert queued(s) == 1


# spec: ANYM-475
def test_worker_round_trip_keeps_hits_and_retry_after(live):
    from messaging_service.commands import CommandRejected, CommandWorker
    s, _, key, state = live
    first = search(s, key)
    async def provider(job):
        assert job['_policy']['operations_enabled'] == ['search_messages_live'] and job['chat_id'] == ''
        return {'outcome': 'confirmed', 'data': {'hits': [], 'next_cursor': None, 'provider_total_approx': 0}}
    assert asyncio.run(settled(CommandWorker(state, provider)))
    assert s.get_messenger_operation(key, first['operation_id'])['result']['data']['hits'] == []
    second = search(s, key, 'synthetic-live-0002')
    async def limited(job):raise CommandRejected('rate_limited', retry_after=7)
    assert asyncio.run(settled(CommandWorker(state, limited)))
    receipt = s.get_messenger_operation(key, second['operation_id'])
    assert receipt['status'] == 'failed' and receipt['result']['data'] == {'code': 'rate_limited', 'retry_after': 7}


# spec: ANYM-475
def test_a_receipt_stuck_on_a_lock_frees_the_send_claim_within_the_deadline(live, monkeypatch, caplog):
    import time
    import messaging_service.commands as commands
    from messaging_service.outgoing import OutgoingWorker
    s, _, key, state = live
    monkeypatch.setattr(commands, 'LIVE_SEARCH_TIMEOUT', 1.5); monkeypatch.setattr(commands, 'LIVE_FOREGROUND_RESERVE', 0)
    monkeypatch.setattr(commands, 'LIVE_EXECUTION_TIMEOUT', 0.5, raising=False)
    operation = search(s, key)['operation_id']
    marks, blocker = {}, s.pool.getconn()
    class Worker(commands.CommandWorker):
        def claim(self):
            job = super().claim(); marks.setdefault('claimed', time.monotonic()); return job
    async def found(job):
        # Another transaction holds the command row, so the receipt waits inside the lease fence.
        blocker.execute('SELECT 1 FROM messenger_commands WHERE id=%s FOR UPDATE', (job['id'],))
        return {'outcome': 'confirmed', 'data': {'hits': [], 'next_cursor': None, 'provider_total_approx': 0}}
    worker = Worker(state, found)
    async def scenario():
        try:
            assert await worker.once(); await asyncio.sleep(0.2)  # the receipt is waiting on the row by now
            await asyncio.to_thread(OutgoingWorker(state, None).claim)  # the real send claim takes the same fence
            marks['send'] = time.monotonic(); await asyncio.gather(*list(worker.settling))  # the receipt runs out its budget
        finally:blocker.rollback()
        assert not await worker.once()  # the worker keeps running
    with caplog.at_level('WARNING', logger='anymsg.live_search'):asyncio.run(scenario())
    s.pool.putconn(blocker)
    assert marks['send'] - marks['claimed'] < 2.5
    assert 'receipt_unrecorded' in caplog.text
    assert s.get_messenger_operation(key, operation)['status'] == 'dispatching'


# spec: ANYM-475
def test_fence_and_receipt_waits_share_one_deadline(live, monkeypatch, caplog):
    import threading
    import time
    import messaging_service.commands as commands
    from messaging_service.outgoing import OutgoingWorker
    s, _, key, state = live
    monkeypatch.setattr(commands, 'LIVE_SEARCH_TIMEOUT', 2); monkeypatch.setattr(commands, 'LIVE_FOREGROUND_RESERVE', 0)
    monkeypatch.setattr(commands, 'LIVE_EXECUTION_TIMEOUT', 0.5, raising=False)
    operation = search(s, key)['operation_id']
    marks, lease_holder, row_holder = {}, s.pool.getconn(), s.pool.getconn()
    class Worker(commands.CommandWorker):
        def claim(self):
            job = super().claim(); marks.setdefault('claimed', time.monotonic()); return job
    async def found(job):
        # The fence waits for the lease row until 1.4 s, then the UPDATE waits for the command row.
        lease_holder.execute('SELECT 1 FROM collector_leases WHERE account=%s FOR UPDATE', (ACCOUNT,))
        row_holder.execute('SELECT 1 FROM messenger_commands WHERE id=%s FOR UPDATE', (job['id'],))
        threading.Timer(1.4 - (time.monotonic() - marks['claimed']), lease_holder.rollback).start()
        return {'outcome': 'confirmed', 'data': {'hits': [], 'next_cursor': None, 'provider_total_approx': 0}}
    async def scenario():
        try:
            worker = Worker(state, found); assert await worker.once(); await asyncio.sleep(0.2)
            await asyncio.to_thread(OutgoingWorker(state, None).claim)
            marks['send'] = time.monotonic(); await asyncio.gather(*list(worker.settling))
        finally:row_holder.rollback(); lease_holder.rollback()
    with caplog.at_level('WARNING', logger='anymsg.live_search'):asyncio.run(scenario())
    s.pool.putconn(lease_holder); s.pool.putconn(row_holder)
    # Each wait alone fits the 2 s budget; together they must not pass it (Sol F2 recheck).
    assert marks['send'] - marks['claimed'] < 2.7
    assert 'receipt_unrecorded' in caplog.text
    assert s.get_messenger_operation(key, operation)['status'] == 'dispatching'


# spec: ANYM-475
def test_send_claim_proceeds_while_a_live_receipt_is_held(live):
    import threading
    import time
    from messaging_service.commands import CommandWorker
    from messaging_service.outgoing import OutgoingWorker
    s, _, key, state = live
    operation = search(s, key)['operation_id']
    worker = CommandWorker(state, None); job = worker.claim()
    holder = s.pool.getconn()
    holder.execute('SELECT 1 FROM messenger_commands WHERE id=%s FOR UPDATE', (job['id'],))
    found = {'outcome': 'confirmed', 'data': {'hits': []}}
    receipt = threading.Thread(target=worker.finish_live, args=(job, 'succeeded', found, None, 5))
    receipt.start(); time.sleep(0.3)  # the receipt now waits inside its transaction
    try:
        started = time.monotonic(); OutgoingWorker(state, None).claim(); took = time.monotonic() - started
    finally:holder.rollback()
    receipt.join(); s.pool.putconn(holder)
    assert took < 1  # the receipt holds no lock a send claim needs
    assert s.get_messenger_operation(key, operation)['status'] == 'succeeded'


# spec: ANYM-475
def test_a_late_receipt_and_new_owner_recovery_never_overwrite_each_other(live):
    import threading
    import time
    from messaging_service.commands import CommandWorker
    s, archive, key, state = live
    found = {'outcome': 'confirmed', 'data': {'hits': []}}
    def take_over():
        archive.release(ACCOUNT, *state.lease)
        lease = ('synthetic-new-worker', archive.acquire(ACCOUNT, 'synthetic-new-worker'))
        return CollectorState(archive, ACCOUNT, lease), lease
    def receipt(operation):
        row = s.get_messenger_operation(key, operation)
        return row['status'], row.get('result')
    # Recovery first: the old owner's receipt changes nothing.
    first = search(s, key)['operation_id']; old = CommandWorker(state, None); job = old.claim()
    new_state, lease = take_over()
    assert CommandWorker(new_state, None).claim() is None
    old.finish_live(job, 'succeeded', found, None, 5)
    assert receipt(first) == ('unknown', None)
    archive.release(ACCOUNT, *lease)
    # Receipt and recovery race on the command row: exactly one outcome lands, never dispatching.
    state = CollectorState(archive, ACCOUNT, ('synthetic-live-worker', archive.acquire(ACCOUNT, 'synthetic-live-worker')))
    second = search(s, key, 'synthetic-live-0002')['operation_id']; old = CommandWorker(state, None); job = old.claim()
    holder = s.pool.getconn()
    holder.execute('SELECT 1 FROM messenger_commands WHERE id=%s FOR UPDATE', (job['id'],))
    late = threading.Thread(target=old.finish_live, args=(job, 'succeeded', found, None, 5)); late.start(); time.sleep(0.2)
    new_state, lease = take_over()
    recovery = threading.Thread(target=CommandWorker(new_state, None).claim); recovery.start(); time.sleep(0.2)
    holder.rollback(); late.join(); recovery.join(); s.pool.putconn(holder)
    assert receipt(second) in (('succeeded', found), ('unknown', None))
    archive.release(ACCOUNT, *lease)


# spec: ANYM-475
def test_new_owner_recovery_skips_a_row_a_receipt_holds(live):
    import time
    from messaging_service.commands import CommandWorker
    s, archive, key, state = live
    operation = search(s, key)['operation_id']; CommandWorker(state, None).claim()
    archive.release(ACCOUNT, *state.lease)
    lease = ('synthetic-new-worker', archive.acquire(ACCOUNT, 'synthetic-new-worker'))
    new_state = CollectorState(archive, ACCOUNT, lease)
    holder = s.pool.getconn()  # stands in for a receipt's open transaction on the command row
    holder.execute('SELECT 1 FROM messenger_commands WHERE id=%s FOR UPDATE', (operation,))
    try:
        started = time.monotonic(); CommandWorker(new_state, None).claim(); took = time.monotonic() - started
    finally:holder.rollback(); s.pool.putconn(holder)
    assert took < 1  # recovery never waits on the row while it holds the send fence
    assert s.get_messenger_operation(key, operation)['status'] == 'unknown'  # read as unknown meanwhile
    CommandWorker(new_state, None).claim()  # a later claim recovers it
    with s.pool.connection() as c:
        assert c.execute('SELECT status FROM messenger_commands WHERE id=%s', (operation,)).fetchone()['status'] == 'unknown'
    archive.release(ACCOUNT, *lease)


# spec: ANYM-475
def test_overlapping_blocked_receipts_take_one_connection_and_never_delay_the_send_claim(live, caplog):
    import time
    from messaging_service.commands import CommandRejected, CommandWorker
    from messaging_service.outgoing import OutgoingWorker
    s, _, key, state = live
    for n in range(6):search(s, key, f'synthetic-live-01{n:02d}')
    holder = s.pool.getconn()  # holds every command row, so each receipt would wait on its lock
    async def refused(job):
        holder.execute('SELECT 1 FROM messenger_commands WHERE id=%s FOR UPDATE', (job['id'],))
        raise CommandRejected('rate_limited', retry_after=7)  # fast refusals spend no provider budget
    worker = CommandWorker(state, refused)
    async def scenario():
        try:
            for _ in range(6):assert await worker.once()
            started = time.monotonic(); await asyncio.to_thread(OutgoingWorker(state, None).claim)
            return time.monotonic() - started, len(worker.settling)
        finally:holder.rollback()
    with caplog.at_level('WARNING', logger='anymsg.live_search'):
        took, in_flight = asyncio.run(scenario())
    s.pool.putconn(holder)
    assert took < 1 and in_flight <= 1
    assert caplog.text.count('receipt_in_flight') == 5
