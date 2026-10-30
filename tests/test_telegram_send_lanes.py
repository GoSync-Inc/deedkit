"""A single synthetic Telegram client, independent network lanes and real disposable outbox."""
import asyncio
import json
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace as NS

import pytest

from messaging_service import outgoing, subscriptions
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


# spec: ANYM-655
@pytest.mark.parametrize('old_guard', [False, True])
def test_bound_collector_terminal_events_and_schema62_guard_isolation(sender, old_guard):  # noqa: F811
    from messaging_service.archive import Archive
    from messaging_service.collector_state import CollectorState
    from messaging_service import store as store_module
    from test_db_role_isolation import RolePool, install, refused, remove
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
    try:
        with s.pool.connection() as c:
            c.execute("UPDATE outgoing_messages SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(queued,))
        bound = CollectorState(Archive(NS(pool=RolePool(s, role), append_event=s.append_event, files=s.files)),
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
def test_expiry_shutdown_waits_for_owned_sql_thread(monkeypatch):
    entered = Event(); release = Event()
    def stalled(self): entered.set(); assert release.wait(2)
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
        c.execute(previous[start:end]); c.execute('DROP INDEX outgoing_expiry')
        c.execute('DELETE FROM archive_migrations WHERE version=63')
        c.execute("UPDATE probe_metadata SET value='62' WHERE name='schema_version'")
        grants = c.execute("SELECT oid,relacl::text FROM pg_class WHERE relnamespace='public'::regnamespace ORDER BY oid").fetchall()
        guard_acl = c.execute("SELECT proacl::text FROM pg_proc WHERE oid='public.collector_account_guard()'::regprocedure").fetchone()
        old_ledger = c.execute('SELECT * FROM archive_migrations ORDER BY version').fetchall()
    s.migrate(); s.migrate()
    with s.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '63'
        assert c.execute('SELECT * FROM archive_migrations WHERE version<63 ORDER BY version').fetchall() == old_ledger
        assert c.execute("SELECT oid,relacl::text FROM pg_class WHERE relnamespace='public'::regnamespace AND relname<>'outgoing_expiry' ORDER BY oid").fetchall() == grants
        assert c.execute("SELECT proacl::text FROM pg_proc WHERE oid='public.collector_account_guard()'::regprocedure").fetchone() == guard_acl
    destination = tmp_path/'schema63-snapshot'
    backup.create_snapshot(s,destination,[sys.executable,'-c','print("synthetic dump stub")'])
    assert backup.verify_snapshot(s,destination)['status'] == 'verified'
    assert backup.verify_bundle(destination)['schema_version'] == 63
