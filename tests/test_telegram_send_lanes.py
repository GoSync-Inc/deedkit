"""A single synthetic Telegram client, independent network lanes and real disposable outbox."""
import asyncio
import copy
import json
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from types import SimpleNamespace as NS

import pytest
from psycopg.errors import LockNotAvailable

from messaging_service import outgoing, subscriptions
from messaging_service.collector_state import CollectorState
from messaging_service.envelope import stable_id
from messaging_service.telegram_collector import TelegramCollector, TelethonProvider
from messaging_service.telegram_governor import ProviderRateLimited, RequestGovernor
from test_sending import sender  # noqa: F401
from test_subscriptions import failure_keys, match_subscriptions
from test_telegram_governor import Clock, State


def provider_with(governor, send):
    provider = object.__new__(TelethonProvider)
    provider.governor = governor
    provider.client = NS(send_message=send)
    return provider


def job():
    return {'provider_chat':'100', 'body':'Synthetic text', 'expires_at':time.time()+60}


# spec: ANYM-655
@pytest.mark.parametrize('phase', ['telegram_media', 'telegram_history', 'telegram_discovery'])
def test_text_send_finishes_while_background_network_read_is_stalled(phase):
    async def scenario():
        clock = Clock(); state = State(clock); governor = RequestGovernor(state, clock=clock, sleep=clock.sleep)
        entered = asyncio.Event(); calls = []; preflights = []
        async def background():
            entered.set()
            await asyncio.Event().wait()
        async def send(peer, text, **options):
            calls.append((peer, text, clock()))
            return NS(chat_id=peer, id=42)
        async def preflight(): preflights.append(True)
        provider = provider_with(governor, send)
        task = asyncio.create_task(provider.rpc(background, phase))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            receipt = await asyncio.wait_for(provider.send_text({**job(), '_preflight':preflight}), .3)
            assert receipt['provider_message_id'] == '42' and not task.done()
            assert calls == [(100, 'Synthetic text', 1)] and preflights == [True]
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


# spec: ANYM-655
def test_text_sends_are_serial_and_share_background_admission_pacing():
    async def scenario():
        clock = Clock(); state = State(clock); governor = RequestGovernor(state, clock=clock, sleep=clock.sleep)
        first = asyncio.Event(); release = asyncio.Event(); calls = []; active = 0
        async def send(peer, text, **options):
            nonlocal active
            active += 1; assert active == 1
            calls.append(clock())
            if len(calls) == 1: first.set(); await release.wait()
            active -= 1
            return NS(chat_id=peer, id=len(calls))
        provider = provider_with(governor, send)
        tasks = [asyncio.create_task(provider.send_text(job()))]
        try:
            await asyncio.wait_for(first.wait(), 1)
            tasks.append(asyncio.create_task(provider.send_text(job())))
            await asyncio.sleep(.02)
            assert calls == [0]
            release.set(); await asyncio.wait_for(asyncio.gather(*tasks), 1)
            assert calls == [0, 1]
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(scenario())


# spec: ANYM-655
def test_send_respects_persisted_pause_while_background_read_is_unfinished():
    async def scenario():
        clock = Clock(); state = State(clock); governor = RequestGovernor(state, clock=clock, sleep=clock.sleep)
        entered = asyncio.Event(); calls = []
        async def background(): entered.set(); await asyncio.Event().wait()
        async def send(peer, text, **options):
            calls.append(clock()); return NS(chat_id=peer, id=42)
        provider = provider_with(governor, send)
        task = asyncio.create_task(provider.rpc(background, 'telegram_media'))
        try:
            await asyncio.wait_for(entered.wait(), 1); state.defer(90)
            await asyncio.wait_for(provider.send_text(job()), .3)
            assert calls == [90] and not task.done()
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


# spec: ANYM-655
def test_background_flood_blocks_subsequent_sends_but_not_previously_admitted_send():
    async def scenario():
        clock = Clock(); state = State(clock); governor = RequestGovernor(state, clock=clock, sleep=clock.sleep)
        background_entered = asyncio.Event(); flood = asyncio.Event(); admitted = asyncio.Event()
        finish = asyncio.Event(); calls = []
        async def background():
            background_entered.set(); await flood.wait(); raise ProviderRateLimited(120)
        async def send(peer, text, **options):
            calls.append(clock()); admitted.set()
            if len(calls) == 1: await finish.wait()
            return NS(chat_id=peer, id=len(calls))
        provider = provider_with(governor, send)
        tasks = [asyncio.create_task(provider.rpc(background, 'telegram_media'))]
        try:
            await asyncio.wait_for(background_entered.wait(), 1)
            tasks.append(asyncio.create_task(provider.send_text(job())))
            await asyncio.wait_for(admitted.wait(), .3)
            flood.set()
            with pytest.raises(ProviderRateLimited): await tasks[0]
            assert state.until == 121
            finish.set(); await tasks[1]
            await provider.send_text(job())
            assert calls == [1, 121]
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(scenario())


# spec: ANYM-655
@pytest.mark.parametrize('path', ['claim', 'sweep', 'finish'])
def test_expiry_reaches_only_originating_key_and_never_replays(sender, path):  # noqa: F811
    s, _, _, _, state = sender; mine, sibling = failure_keys(s)
    chat = stable_id('chat', 'send-fixture', '100')
    selected = {name:s.create_subscription(token, [chat], kinds=['send.failed'])['subscription_id']
        for name, token in [('mine', mine), ('sibling', sibling)]}
    queued = s.send_message(mine, account='send-fixture', chat_id=chat, text='Private synthetic failed text',
        request_id='synthetic-expiry-'+path)
    calls = []
    async def send(value): calls.append(value); raise AssertionError('expired request reached provider')
    worker = outgoing.OutgoingWorker(state, send)
    if path == 'finish':
        claimed = worker.claim(); worker.finish(claimed, 'expired', None, 'send_expired')
    else:
        with s.pool.connection() as c:
            c.execute("UPDATE outgoing_messages SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                (queued['send_id'],))
        if path == 'sweep': assert worker.expire_queued() == 1
        else: assert asyncio.run(worker.once()) is True
    assert s.get_send_status(mine, queued['send_id'])['status'] == 'expired'
    assert asyncio.run(worker.once()) is False and calls == []
    match_subscriptions(s)
    items = s.get_subscription_events(mine, selected['mine'])['items']
    assert len(items) == 1
    assert (items[0]['send_id'], items[0]['status'], items[0]['error_code']) == (
        queued['send_id'], 'expired', 'send_expired')
    assert 'Private synthetic failed text' not in json.dumps(items, default=str)
    assert s.get_subscription_events(sibling, selected['sibling'])['items'] == []


# spec: ANYM-655
def test_expiry_journal_failure_cannot_undo_terminal_status(sender):  # noqa: F811
    s, _, _, _, state = sender; mine, _ = failure_keys(s)
    chat = stable_id('chat', 'send-fixture', '100')
    queued = s.send_message(mine, account='send-fixture', chat_id=chat, text='Synthetic expired text',
        request_id='synthetic-expiry-journal')
    with s.pool.connection() as c:
        c.execute("UPDATE outgoing_messages SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (queued['send_id'],))
        c.execute('''CREATE FUNCTION pg_temp.refuse_expired_event() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic unavailable journal'; END $$''')
        c.execute('''CREATE TRIGGER refuse_expired_event BEFORE INSERT ON events FOR EACH ROW
            WHEN (NEW.kind='send.failed') EXECUTE FUNCTION pg_temp.refuse_expired_event()''')
    before = subscriptions.JOURNAL_FAILURES['send.failed']
    try:
        assert outgoing.OutgoingWorker(state, None).expire_queued() == 1
    finally:
        with s.pool.connection() as c: c.execute('DROP TRIGGER refuse_expired_event ON events')
    assert s.get_send_status(mine, queued['send_id'])['status'] == 'expired'
    assert subscriptions.JOURNAL_FAILURES['send.failed'] == before+1


# spec: ANYM-655
def test_expiry_loop_runs_while_sender_is_stalled_and_account_paused(sender):  # noqa: F811
    s, _, _, _, state = sender
    worker = TelegramCollector.__new__(TelegramCollector)
    worker.state = state; worker.database_retry = .01
    entered = asyncio.Event()
    async def scenario():
        mine, _ = failure_keys(s); chat = stable_id('chat', 'send-fixture', '100')
        queued = s.send_message(mine, account='send-fixture', chat_id=chat, text='Synthetic late intent',
            request_id='synthetic-expiry-independent')
        state.defer(120)
        with s.pool.connection() as c:
            c.execute("UPDATE outgoing_messages SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                (queued['send_id'],))
        async def dispatch(): entered.set(); await asyncio.Event().wait()
        worker.outgoing_dispatch_loop = dispatch
        task = asyncio.create_task(worker.outgoing_loop())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            async def terminal():
                while s.get_send_status(mine, queued['send_id'])['status'] != 'expired': await asyncio.sleep(.01)
                # Status's expiry projection alone is insufficient: prove durable sweep and journal.
                while True:
                    with s.pool.connection() as c:
                        if c.execute('SELECT status FROM outgoing_messages WHERE id=%s',
                            (queued['send_id'],)).fetchone()['status'] == 'expired': return
                    await asyncio.sleep(.01)
            await asyncio.wait_for(terminal(), 2)
            assert not task.done() and state.cooldown_remaining() > 0
            with s.pool.connection() as c:
                assert c.execute("SELECT count(*) AS n FROM events WHERE event_key=%s",
                    ('send.failed:'+queued['send_id'],)).fetchone()['n'] == 1
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


# spec: ANYM-655
def test_expiry_sweep_is_bounded_fenced_and_does_not_touch_other_attempts(sender):  # noqa: F811
    s, _, key, _, state = sender; chat = stable_id('chat', 'send-fixture', '100')
    ids = [s.send_message(key, account='send-fixture', chat_id=chat, text='Synthetic expiry',
        request_id='synthetic-expiry-batch-%02d'%n)['send_id'] for n in range(20)]
    with s.pool.connection() as c:
        c.execute("UPDATE outgoing_messages SET expires_at=clock_timestamp()-interval '1 second' WHERE id=ANY(%s)", (ids,))
        c.execute("UPDATE outgoing_messages SET status='unknown' WHERE id=%s", (ids[-1],))
        c.execute("UPDATE outgoing_messages SET status='dispatching',lease_epoch=%s WHERE id=%s", (state.lease[1],ids[-2]))
    worker = outgoing.OutgoingWorker(state, None); lease = state.lease
    state.lease = lease[0], lease[1]+1
    try:
        with pytest.raises(RuntimeError, match='collector_lease_lost'): worker.expire_queued()
    finally: state.lease = lease
    assert worker.expire_queued() == 16 and worker.expire_queued() == 2 and worker.expire_queued() == 0
    with s.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM outgoing_messages WHERE status='expired'").fetchone()['n'] == 18
        assert c.execute('SELECT status FROM outgoing_messages WHERE id=%s',(ids[-1],)).fetchone()['status'] == 'unknown'
        assert c.execute('SELECT status FROM outgoing_messages WHERE id=%s',(ids[-2],)).fetchone()['status'] == 'dispatching'


class RecordingPool:
    """Real connections; records each statement, and raises `fail` in place of the first outgoing_messages statement."""
    def __init__(self, pool, fail=None): self.pool, self.fail, self.statements = pool, fail, []

    @contextmanager
    def connection(self, timeout=None):
        recorder = self
        with self.pool.connection(timeout=timeout) as c:
            class Proxy:
                def __getattr__(self, name): return getattr(c, name)
                def execute(self, query, params=None):
                    recorder.statements.append(query)
                    if recorder.fail and 'outgoing_messages' in query: raise recorder.fail
                    return c.execute(query, params)
            yield Proxy()

    def kinds(self):
        def kind(query):
            if 'SET LOCAL' in query: return 'bound'
            if 'collector_leases' in query: return 'fence'
            if 'SKIP LOCKED' in query: return 'claim'
            if 'outgoing_messages' in query: return 'read'
        return [k for k in map(kind, self.statements) if k]


def recording_state(state, fail=None):
    archive = copy.copy(state.archive); archive.store = copy.copy(archive.store)
    archive.store.pool = recorder = RecordingPool(state.archive.store.pool, fail)
    return NS(archive=archive, account=state.account, lease=state.lease), recorder


def queue_expiry(s, key, name, *, due):
    chat = stable_id('chat', 'send-fixture', '100')
    send_id = s.send_message(key, account='send-fixture', chat_id=chat, text='Synthetic expiry',
        request_id='synthetic-expiry-'+name)['send_id']
    if due:
        with s.pool.connection() as c:
            c.execute("UPDATE outgoing_messages SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (send_id,))
    return send_id


def send_state(s, send_id):
    with s.pool.connection() as c:
        return c.execute('SELECT status FROM outgoing_messages WHERE id=%s', (send_id,)).fetchone()['status']


# spec: ANYM-788
def test_expiry_pass_with_nothing_due_takes_no_lease_lock(sender):  # noqa: F811
    s, archive, key, _, state = sender
    waiting = queue_expiry(s, key, 'waiting', due=False)
    # Rows the pass must not count as due: past expires_at but no longer queued, or another account's.
    unknown, dispatching = queue_expiry(s, key, 'unknown', due=True), queue_expiry(s, key, 'dispatching', due=True)
    with s.pool.connection() as c:
        c.execute("UPDATE outgoing_messages SET status='unknown' WHERE id=%s", (unknown,))
        c.execute("UPDATE outgoing_messages SET status='dispatching',lease_epoch=%s WHERE id=%s", (state.lease[1], dispatching))
    with s.pool.connection() as holder:
        holder.execute('SELECT 1 FROM collector_leases WHERE account=%s FOR UPDATE', (state.account,))
        assert outgoing.OutgoingWorker(state, None).expire_queued() == 0
    assert (send_state(s, waiting), send_state(s, unknown), send_state(s, dispatching)) == ('queued', 'unknown', 'dispatching')
    archive.register('send-other', 'telegram', 'Synthetic other', discover=True)
    other = CollectorState(archive, 'send-other', ('synthetic-other-worker', archive.acquire('send-other', 'synthetic-other-worker')))
    with s.pool.connection() as c:
        c.execute("UPDATE outgoing_messages SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (waiting,))
    with s.pool.connection() as holder:
        holder.execute('SELECT 1 FROM collector_leases WHERE account=%s FOR UPDATE', (other.account,))
        assert outgoing.OutgoingWorker(other, None).expire_queued() == 0
    assert send_state(s, waiting) == 'queued'


# spec: ANYM-788
def test_expiry_due_row_keeps_bounds_and_fence_and_a_later_due_row_expires_next_pass(sender):  # noqa: F811
    s, _, key, _, state = sender
    queued = queue_expiry(s, key, 'later', due=False)
    bound, recorder = recording_state(state)
    worker = outgoing.OutgoingWorker(bound, None)
    assert worker.expire_queued() == 0
    assert recorder.kinds() == ['bound', 'bound', 'read']
    with s.pool.connection() as c:
        c.execute("UPDATE outgoing_messages SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (queued,))
    # No write without the fence: a busy lease row leaves the due row queued.
    with s.pool.connection() as holder:
        holder.execute('SELECT 1 FROM collector_leases WHERE account=%s FOR UPDATE', (state.account,))
        with pytest.raises(LockNotAvailable): worker.expire_queued()
    assert send_state(s, queued) == 'queued'
    recorder.statements.clear()
    assert worker.expire_queued() == 1 and send_state(s, queued) == 'expired'
    assert recorder.kinds() == ['bound', 'bound', 'read', 'fence', 'claim']


# spec: ANYM-788
def test_lost_lease_with_nothing_due_still_ends_the_collector_through_heartbeat(sender, monkeypatch):  # noqa: F811
    s, archive, _, _, state = sender; passes = []; first = Event(); original = outgoing.OutgoingWorker.expire_queued
    def counted(self, **kwargs):
        passes.append(original(self, **kwargs)); first.set(); return passes[-1]
    monkeypatch.setattr(outgoing.OutgoingWorker, 'expire_queued', counted)
    worker = TelegramCollector.__new__(TelegramCollector); worker.state = state; worker.database_retry = .01
    worker.archive = archive; worker.account = state.account; worker.owner = state.lease[0]
    worker.lease_ttl = 60; worker.heartbeat_interval = .05; worker._lease_valid_until = None
    lease = state.lease; state.lease = lease[0], lease[1]+1
    async def scenario():
        expiry = asyncio.create_task(worker.outgoing_expiry_loop())
        beat = None
        try:
            # The empty pass does not notice the lost lease; the heartbeat started after it does.
            assert await asyncio.to_thread(first.wait, 2) and passes == [0]
            beat = asyncio.create_task(worker.heartbeat())
            done, _ = await asyncio.wait({expiry, beat}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
            assert done == {beat}
            with pytest.raises(RuntimeError, match='collector_lease_lost'): beat.result()
        finally:
            for task in filter(None, (expiry, beat)): task.cancel()
            await asyncio.gather(*filter(None, (expiry, beat)), return_exceptions=True)
    try: asyncio.run(scenario())
    finally: state.lease = lease


# spec: ANYM-788
@pytest.mark.parametrize('error,quiet', [('LockNotAvailable', True), ('QueryCanceled', False)])
def test_failed_expiry_read_reaches_the_loops_existing_branches(sender, monkeypatch, caplog, error, quiet):  # noqa: F811
    from psycopg import errors
    from messaging_service import telegram_collector
    _, _, _, _, state = sender; waits = []
    bound, recorder = recording_state(state, getattr(errors, error)('synthetic'))
    class Backoff:
        def __init__(self, owner): pass
        async def wait(self): waits.append(True); await asyncio.sleep(.01)
        def reset(self): pass
    monkeypatch.setattr(telegram_collector, 'DbBackoff', Backoff)
    worker = TelegramCollector.__new__(TelegramCollector); worker.state = bound; worker.database_retry = .01
    caplog.set_level('WARNING', 'messaging_service.telegram_collector')
    async def scenario():
        task = asyncio.create_task(worker.outgoing_expiry_loop())
        try:
            for _ in range(500):
                if recorder.kinds().count('read') >= 3: break
                assert not task.done()
                await asyncio.sleep(.01)
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
    # The read fails first: inside the pass's bounds, with no lease statement sent.
    assert recorder.kinds()[:3] == ['bound', 'bound', 'read'] and recorder.kinds().count('read') >= 3
    assert 'fence' not in recorder.kinds() and 'claim' not in recorder.kinds()
    assert ('outgoing_expiry_failed' not in caplog.text) is quiet
    assert (waits == []) is quiet


# spec: ANYM-655
@pytest.mark.parametrize('old_guard', [False, True])
def test_bound_collector_terminal_events_and_schema62_guard_isolation(sender, old_guard):  # noqa: F811
    from messaging_service.archive import Archive
    from messaging_service.collector_state import CollectorState
    from messaging_service import store as store_module
    from contextlib import contextmanager
    from psycopg import sql
    from test_db_role_isolation import install, refused, remove
    from psycopg.types.json import Jsonb
    s, _, _, _, state = sender; mine, _ = failure_keys(s)
    chat = stable_id('chat', 'send-fixture', '100')
    queued = s.send_message(mine, account='send-fixture', chat_id=chat, text='Synthetic private expiry',
        request_id='synthetic-bound-expiry-'+str(old_guard))['send_id']
    root = Path(store_module.__file__).parent/'migrations'
    if old_guard:
        previous = (root/'055_failure_events.sql').read_text()
        start = previous.index('CREATE OR REPLACE FUNCTION public.collector_account_guard()')
        end = previous.index('\nEND $$;', start)+len('\nEND $$;')
        with s.pool.connection() as c: c.execute(previous[start:end])
    role = install(s, 'collector_role.sql', 'messaging_collector', account='send-fixture')
    @contextmanager
    def connection(*, timeout=None):
        with s.pool.connection(timeout=timeout) as c:
            c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
            yield c
    try:
        with s.pool.connection() as c:
            c.execute("UPDATE outgoing_messages SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(queued,))
        bound = CollectorState(Archive(NS(pool=NS(connection=connection), append_event=s.append_event, files=s.files)),
            'send-fixture', state.lease)
        before = subscriptions.JOURNAL_FAILURES['send.failed']
        assert outgoing.OutgoingWorker(bound, None).expire_queued() == 1
        assert s.get_send_status(mine, queued)['status'] == 'expired'
        with s.pool.connection() as c:
            row = c.execute('SELECT * FROM outgoing_messages WHERE id=%s', (queued,)).fetchone()
            count = c.execute('SELECT count(*) AS n FROM events WHERE event_key=%s',('send.failed:'+queued,)).fetchone()['n']
        assert count == (0 if old_guard else 1)
        assert subscriptions.JOURNAL_FAILURES['send.failed'] == before+int(old_guard)
        snapshot = {'send_id':queued,'account':row['account'],'chat':row['chat'],
            'principal':row['principal'],'key_id':str(row['key_id']),'status':'expired'}
        # Guard expansion does not grant arbitrary message-less events or another key's identity.
        snapshot['key_id'] = '00000000-0000-0000-0000-000000000000'
        refused(s, role, '''INSERT INTO events(id,account,message,kind,revision,origin,snapshot)
            VALUES(0,%s,NULL,'send.failed',1,'live',%s)''', ('send-fixture',Jsonb(snapshot)))
    finally:
        remove(s, role)
        if old_guard:
            with s.pool.connection() as c: c.execute((root/'063_send_terminal_events.sql').read_text().split('\n\n',1)[1])


# spec: ANYM-655
@pytest.mark.parametrize('error,quiet',[('LockNotAvailable',True),('OperationalError',False)])
def test_expiry_skips_a_busy_lease_row_quietly_and_still_reports_other_database_errors(monkeypatch,caplog,error,quiet):
    from psycopg import errors
    from messaging_service import telegram_collector
    calls = []; waits = []
    class Backoff:
        def __init__(self,owner): pass
        async def wait(self): waits.append(True)
        def reset(self): pass
    def failing(self,**kwargs): calls.append(True); raise getattr(errors,error)('synthetic')
    monkeypatch.setattr(outgoing.OutgoingWorker, 'expire_queued', failing)
    monkeypatch.setattr(telegram_collector, 'DbBackoff', Backoff)
    worker = TelegramCollector.__new__(TelegramCollector); worker.state = None; worker.database_retry = .01
    caplog.set_level('WARNING','messaging_service.telegram_collector')
    async def scenario():
        task = asyncio.create_task(worker.outgoing_expiry_loop())
        try:
            for _ in range(200):
                if len(calls) >= 4: break
                await asyncio.sleep(.01)
        finally:
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
    asyncio.run(scenario())
    assert len(calls) >= 4
    # A busy collector_leases row (ingest holds it) is housekeeping contention: no warning, no growing backoff.
    assert ('outgoing_expiry_failed' not in caplog.text) is quiet
    assert (waits == []) is quiet


# spec: ANYM-655
def test_expiry_shutdown_waits_for_owned_sql_thread(monkeypatch):
    entered = Event(); release = Event()
    def stalled(self,**kwargs): entered.set(); assert release.wait(2)
    monkeypatch.setattr(outgoing.OutgoingWorker, 'expire_queued', stalled)
    worker = TelegramCollector.__new__(TelegramCollector); worker.state = None; worker.database_retry = .01
    async def scenario():
        task = asyncio.create_task(worker.outgoing_expiry_loop())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel(); await asyncio.sleep(.02)
            assert not task.done()
        finally: release.set()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(scenario())


# spec: ANYM-655
def test_send_rechecks_authorization_after_pause_discovered_during_preflight():
    async def scenario():
        clock = Clock(); state = State(clock); governor = RequestGovernor(state, clock=clock, sleep=clock.sleep)
        checks = []; calls = []
        async def preflight():
            checks.append(clock())
            if len(checks) == 1: state.defer(30)
            else: raise outgoing.SendRejected('send_access_changed')
        async def send(*args, **kwargs): calls.append(True)
        provider = provider_with(governor, send)
        with pytest.raises(outgoing.SendRejected, match='send_access_changed'):
            await provider.send_text({**job(),'_preflight':preflight})
        assert checks == [0,30] and calls == []
    asyncio.run(scenario())


# spec: ANYM-655
def test_cancelled_claim_notifies_sending_key_without_calling_provider(sender):  # noqa: F811
    s, _, _, _, state = sender; mine, _ = failure_keys(s)
    chat = stable_id('chat', 'send-fixture', '100')
    selected = s.create_subscription(mine, [chat], kinds=['send.failed'])['subscription_id']
    queued = s.send_message(mine, account='send-fixture', chat_id=chat, text='Synthetic revoked send',
        request_id='synthetic-cancelled-claim')
    with s.pool.connection() as c:
        c.execute("UPDATE account_policies SET send_enabled=false WHERE account='send-fixture'")
    async def forbidden(value): raise AssertionError('cancelled claim reached provider')
    assert asyncio.run(outgoing.OutgoingWorker(state, forbidden).once()) is True
    match_subscriptions(s)
    events = s.get_subscription_events(mine, selected)['items']
    assert [(row['send_id'],row['status'],row['error_code']) for row in events] == [
        (queued['send_id'],'cancelled','sending_disabled')]


# spec: ANYM-655
def test_schema63_upgrade_preserves_grants_and_backups_and_is_repeatable(sender, tmp_path):  # noqa: F811
    import sys
    from messaging_service import backup, store as store_module
    from messaging_service import backup_r2_verifier
    assert 63 in backup_r2_verifier.SCHEMAS
    s, _, _, _, _ = sender; root = Path(store_module.__file__).parent/'migrations'
    previous = (root/'055_failure_events.sql').read_text()
    start = previous.index('CREATE OR REPLACE FUNCTION public.collector_account_guard()')
    end = previous.index('\nEND $$;', start)+len('\nEND $$;')
    # Synthetic disposable database only: reconstruct the exact N-1 guard and ledger.
    with s.pool.connection() as c:
        c.execute(previous[start:end]); c.execute('DROP INDEX outgoing_expiry'); c.execute('DROP INDEX messenger_commands_press')
        c.execute('DELETE FROM archive_migrations WHERE version IN (63,64,65,66,67,68,69,70)')
        c.execute("UPDATE probe_metadata SET value='62' WHERE name='schema_version'")
        grants = c.execute("SELECT oid,relacl::text FROM pg_class WHERE relnamespace='public'::regnamespace ORDER BY oid").fetchall()
        guard_acl = c.execute("SELECT proacl::text FROM pg_proc WHERE oid='public.collector_account_guard()'::regprocedure").fetchone()
        old_ledger = c.execute('SELECT * FROM archive_migrations ORDER BY version').fetchall()
    s.migrate(); s.migrate()
    with s.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '70'
        assert c.execute('SELECT * FROM archive_migrations WHERE version<63 ORDER BY version').fetchall() == old_ledger
        assert c.execute("SELECT oid,relacl::text FROM pg_class WHERE relnamespace='public'::regnamespace AND relname NOT IN ('outgoing_expiry','messenger_commands_press') ORDER BY oid").fetchall() == grants
        assert c.execute("SELECT proacl::text FROM pg_proc WHERE oid='public.collector_account_guard()'::regprocedure").fetchone() == guard_acl
    destination = tmp_path/'schema63-snapshot'
    backup.create_snapshot(s,destination,[sys.executable,'-c','print("synthetic dump stub")'])
    assert backup.verify_snapshot(s,destination)['status'] == 'verified'
    assert backup.verify_bundle(destination)['schema_version'] == 70


# spec: ANYM-655
@pytest.mark.parametrize('scoped', [False, True])
def test_send_flood_during_background_status_write_blocks_background_admission(scoped):
    async def scenario():
        clock = Clock(); state = State(clock); gate = RequestGovernor(state,clock=clock,sleep=clock.sleep)
        sending = asyncio.Event(); flood = asyncio.Event(); entered = Event(); release = Event(); calls = []
        phase = 'live_search' if scoped else 'telegram_history'
        def status(kind, current):
            if current == phase: entered.set(); assert release.wait(2)
        state.status = status
        async def send(): sending.set(); await flood.wait(); raise ProviderRateLimited(60)
        async def background(): calls.append(clock())
        tasks = [asyncio.create_task(gate.call(send,'provider_send'))]
        try:
            await asyncio.wait_for(sending.wait(),1)
            tasks.append(asyncio.create_task(gate.scoped_call(background,phase,'search') if scoped
                else gate.call(background,phase)))
            assert await asyncio.to_thread(entered.wait,1)
            flood.set()
            with pytest.raises(ProviderRateLimited): await tasks[0]
            release.set()
            if scoped:
                with pytest.raises(ProviderRateLimited): await tasks[1]
                assert calls == []
            else:
                await tasks[1]
                assert calls == [61]
        finally:
            release.set()
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
    asyncio.run(scenario())


# spec: ANYM-655
def test_expiry_pool_acquisition_obeys_one_absolute_deadline(sender, monkeypatch):  # noqa: F811
    import copy
    from psycopg_pool import ConnectionPool, PoolTimeout
    s, _, _, _, state = sender
    monkeypatch.setattr(outgoing,'EXPIRY_SECONDS',.2,raising=False)
    with ConnectionPool(s.pool.conninfo,min_size=1,max_size=1) as pool:
        archive = copy.copy(state.archive); archive.store = copy.copy(archive.store); archive.store.pool = pool
        worker = outgoing.OutgoingWorker(NS(archive=archive,account=state.account,lease=state.lease),None)
        with pool.connection():
            began = time.monotonic()
            with pytest.raises(PoolTimeout): worker.expire_queued()
            assert time.monotonic()-began < .6


# spec: ANYM-655
def test_stalled_expiry_transaction_releases_fence_and_cancellation_is_bounded(sender, monkeypatch):  # noqa: F811
    from messaging_service.archive import Archive
    s, _, key, _, state = sender; entered = Event(); release = Event(); original = Archive.fence
    queue_expiry(s, key, 'stalled', due=True)  # ANYM-788: an empty pass no longer reaches the fence this test stalls in.
    monkeypatch.setattr(outgoing,'EXPIRY_SECONDS',.2,raising=False)
    monkeypatch.setattr(outgoing,'EXPIRY_GRACE',.1,raising=False)
    def stalled(c, account, lease):
        original(c,account,lease); entered.set(); assert release.wait(2)
    monkeypatch.setattr(Archive,'fence',staticmethod(stalled))
    worker = TelegramCollector.__new__(TelegramCollector); worker.state = state; worker.database_retry = .01
    async def scenario():
        task = asyncio.create_task(worker.outgoing_expiry_loop())
        try:
            assert await asyncio.to_thread(entered.wait,1)
            began = time.monotonic(); task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert time.monotonic()-began < .5 and not release.is_set()
            # The SQL client is still stalled: only the server deadline can free this row.
            with s.pool.connection() as c:
                c.execute("SET LOCAL lock_timeout='100ms'")
                assert c.execute('SELECT 1 FROM collector_leases WHERE account=%s FOR UPDATE',
                    (state.account,)).fetchone()
        finally:
            release.set(); task.cancel(); await asyncio.gather(task,return_exceptions=True)
    asyncio.run(scenario())
