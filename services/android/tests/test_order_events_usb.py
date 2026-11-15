"""Real loopback framing and local journals; no ADB, phone, native client or provider."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from deedkit_android.order_events import OrderEvents
from deedkit_android.order_events_control import OrderControl
from deedkit_android.order_events_usb import SourceAccess, UsbForward, UsbOrderConnector


# spec: ANYM-997
def test_usb_stream_reaches_durable_observation_and_existing_supervision_hold(tmp_path):
    from deedkit_android.order_supervision import Supervision

    async def scenario():
        db = sqlite3.connect(tmp_path / 'orders.db')
        principal = SimpleNamespace(id='owner')
        events = OrderEvents(db, lambda *args: True, clock=lambda: 1000)
        control = OrderControl(events, source_id='usb', auth_epoch=1, original_words=lambda ctx: 'Original goal')
        events.bind('owner', owner='owner', device_id='phone', order_id='order', package='app.food',
                    source_id='usb', source_order_id='real-order', auth_epoch=1,
                    original_request_id='request', native_thread_id='thread',
                    original_user_words='Original goal', next_check_at=1100)
        received = asyncio.Event()
        closed = []
        async def serve(reader, writer):
            watch = json.loads(await reader.readline())
            writer.write((json.dumps(dict(type='hello', version=1, source_epoch='epoch',
                                          oldest_sequence=1, last_sequence=0, gap=False)) + '\n').encode())
            if watch['packages']:
                writer.write((json.dumps(dict(type='observation', source_epoch='epoch', sequence=1,
                    package='app.food', observed_at=1000, observation_ref='opaque')) + '\n').encode())
            await writer.drain()
            received.set()
            await reader.read()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        async def cleanup():
            closed.append('owned')
            return True
        async def bind(access):
            return UsbForward(access.serial, port, cleanup, receipt='host-private-receipt')
        connector = UsbOrderConnector(events, owner='owner', device_id='phone', source_id='usb',
            auth_epoch=1, admit=lambda: SourceAccess(principal, 'serial'), bind_usb=bind,
            interval_s=.01, reconnect_s=.02, cleanup_s=.2)
        try:
            connector.start()
            await asyncio.wait_for(received.wait(), 1)
            async with asyncio.timeout(1):
                while not events.observations(principal, 'owner', 'order'):
                    await asyncio.sleep(.005)
            agents = SimpleNamespace(db=SimpleNamespace(db=db), order_control=lambda: control)
            supervision = Supervision(agents, wall=lambda: 1000)
            await supervision.tick()
            wake = dict(db.execute("SELECT * FROM android_order_wakes").fetchone())
            assert wake['state'] == 'pending' and wake['hold_reason'] == 'lease_unavailable'
            assert supervision.receipt(wake['wake_id']) is None
            assert events.order(principal, 'owner', 'order')['next_check_at'] == 1100
            await connector.close()
            assert connector.status()['cleanup'] == 'confirmed'
            assert closed == ['owned']
        finally:
            await connector.close()
            server.close()
            await server.wait_closed()
            db.close()
    asyncio.run(scenario())


@pytest.fixture
def world(tmp_path):
    db = sqlite3.connect(tmp_path / 'usb.sqlite')
    principal = SimpleNamespace(id='owner')
    live = [True]
    serial = ['test-serial']
    fresh = [True]
    def authorize(p, owner, device, order, source, epoch):
        return live[0] and getattr(p, 'id', p) == owner and device == 'phone' and source == 'usb' and epoch == 1
    events = OrderEvents(db, authorize, clock=lambda: 1000)
    control = OrderControl(events, source_id='usb', auth_epoch=1, original_words=lambda ctx: 'Exact task words')
    def bind_order(order='order', package='app.food', owner='owner', device='phone'):
        events.bind(owner, owner=owner, device_id=device, order_id=order, package=package,
                    source_id='usb', source_order_id=order, auth_epoch=1, original_request_id='task',
                    native_thread_id='thread', original_user_words='Exact task words', next_check_at=1200)
    bind_order()
    def admit():
        if not live[0] or not fresh[0]:
            raise PermissionError('synthetic revoked original generation')
        return SourceAccess(principal, serial[0])
    yield SimpleNamespace(db=db, events=events, control=control, principal=principal, live=live,
                          serial=serial, fresh=fresh, admit=admit, bind_order=bind_order)
    db.close()


def hello(last=0, *, gap=False, epoch='epoch'):
    return dict(type='hello', version=1, source_epoch=epoch, oldest_sequence=1, last_sequence=last, gap=gap)


def observation(seq, *, package='app.food', ref='same-hashed-key'):
    return dict(type='observation', source_epoch='epoch', sequence=seq, package=package,
                observed_at=1000, observation_ref=ref)


async def send(writer, *frames):
    for frame in frames:
        writer.write((json.dumps(frame) + '\n').encode())
    await writer.drain()


async def until(check, timeout=1):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(.005)


class Endpoint:
    """Synthetic real TCP socket; only the trusted host forward capability is substituted."""
    def __init__(self, world, handler):
        self.world, self.handler = world, handler
        self.watches, self.writers, self.tasks = [], set(), set()
        self.binds = self.removals = 0
        self.remove_result = True
        self.cleanup_delay = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.serve, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        try:
            raw = await reader.readline()
            if not raw:
                return
            watch = json.loads(raw)
            self.watches.append(watch)
            if watch['packages']:
                await self.handler(self, reader, writer, watch)
            else:
                await asyncio.sleep(self.cleanup_delay)
                await send(writer, hello(77))
                await reader.read()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            self.writers.discard(writer)
            self.tasks.discard(task)

    async def bind(self, access):
        self.binds += 1
        return UsbForward(access.serial, self.port, self.remove, receipt=object())

    async def remove(self):
        self.removals += 1
        return self.remove_result

    def connector(self, **kwargs):
        return UsbOrderConnector(self.world.events, owner='owner', device_id='phone', source_id='usb',
            auth_epoch=1, admit=self.world.admit, bind_usb=self.bind, interval_s=.01,
            reconnect_s=.01, cleanup_s=.3, **kwargs)

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def quiet(endpoint, reader, writer, watch):
    await send(writer, hello())
    await reader.read()


# spec: ANYM-997
def test_delayed_hello_heartbeat_and_same_key_os_updates_are_not_discarded(world):
    async def scenario():
        async def stream(endpoint, reader, writer, watch):
            await asyncio.sleep(.06)  # many guard ticks; no immediate heartbeat assumption
            await send(writer, hello(3), observation(1))
            await asyncio.sleep(.06)
            await send(writer, {'type': 'heartbeat', 'source_epoch': 'epoch', 'last_sequence': 19},
                       observation(2), observation(3))
            await reader.read()
        async with Endpoint(world, stream) as endpoint:
            connector = endpoint.connector()
            try:
                connector.start()
                await until(lambda: connector.status()['cursor'] == 3)
                observations = world.events.observations(world.principal, 'owner', 'order')
                assert len(observations) == 3 and {r['observation_ref'] for r in observations} == {'same-hashed-key'}
                wakes = world.db.execute("SELECT * FROM android_order_wakes").fetchall()
                assert len(wakes) == 1 and wakes[0]['through'] == 3 and wakes[0]['state'] == 'pending'
                assert connector.status()['state'] == 'reading' and endpoint.binds == 1
                assert connector.status()['cursor'] == 3  # heartbeat's moving head is not observed-through
            finally:
                await connector.close()
    asyncio.run(scenario())


# spec: ANYM-997
def test_restart_replays_original_sequence_without_new_wake_and_keeps_gap(world):
    async def scenario():
        async def stream(endpoint, reader, writer, watch):
            await send(writer, hello(2, gap=True), observation(1), observation(2))
            await reader.read()
        async with Endpoint(world, stream) as endpoint:
            first = endpoint.connector()
            first.start()
            await until(lambda: first.status()['cursor'] == 2)
            wake = world.db.execute("SELECT wake_id FROM android_order_wakes").fetchone()[0]
            await first.close()
            second = endpoint.connector()
            try:
                second.start()
                await until(lambda: len(endpoint.watches) == 3)
                await asyncio.sleep(.03)
                assert endpoint.watches[2]['after'] == 2 and endpoint.watches[2]['source_epoch'] == 'epoch'
                assert len(world.events.observations(world.principal, 'owner', 'order')) == 2
                assert world.db.execute("SELECT wake_id FROM android_order_wakes").fetchone()[0] == wake
                assert second.status()['observation_gap'] is True
            finally:
                await second.close()
    asyncio.run(scenario())


# spec: ANYM-997
def test_revoke_during_blocked_read_stops_host_admission_and_retains_timer(world):
    async def scenario():
        async with Endpoint(world, quiet) as endpoint:
            connector = endpoint.connector()
            connector.start()
            await until(lambda: connector.source.epoch is not None)
            world.live[0] = False
            await until(lambda: connector.task.done())
            assert connector.status()['reason'] == 'source_access_changed'
            assert connector.status()['cleanup'] == 'cleanup_unconfirmed'
            assert len(endpoint.watches) == 1  # revoked access cannot send an emptywatch
            assert endpoint.removals == 1
            assert world.db.execute("SELECT next_check_at FROM android_order_obligations").fetchone()[0] == 1200
            assert world.db.execute("SELECT count(*) FROM android_order_observations").fetchone()[0] == 0
            await connector.close()
    asyncio.run(scenario())


# spec: ANYM-997
def test_original_generation_loss_after_bind_refuses_even_same_owner_and_serial(world):
    async def scenario():
        async with Endpoint(world, quiet) as endpoint:
            async def bind(access):
                result = await endpoint.bind(access)
                world.fresh[0] = False
                return result
            connector = endpoint.connector()
            connector.bind_usb = bind
            connector.start()
            await until(lambda: connector.task.done())
            assert endpoint.watches == [] and endpoint.removals == 1
            assert connector.status()['reason'] == 'source_access_changed'
            assert connector.status()['cleanup'] == 'cleanup_unconfirmed'
    asyncio.run(scenario())


# spec: ANYM-997
def test_one_serial_slot_spans_owners_and_release_requires_owned_cleanup(world):
    async def scenario():
        async with Endpoint(world, quiet) as endpoint:
            first = endpoint.connector()
            first.start()
            await until(lambda: endpoint.watches)
            other = UsbOrderConnector(world.events, owner='other', device_id='phone', source_id='usb', auth_epoch=1,
                admit=lambda: SourceAccess(SimpleNamespace(id='other'), world.serial[0]), bind_usb=endpoint.bind,
                interval_s=.01, reconnect_s=.01, cleanup_s=.3)
            world.bind_order('other-order', 'app.other', owner='other')
            other.start()
            await until(lambda: other.task.done())
            assert other.status()['reason'] == 'serial_subscriber_busy'
            assert endpoint.binds == 1 and endpoint.removals == 0
            await first.close()
            replacement = endpoint.connector()
            replacement.start()
            await until(lambda: endpoint.binds == 2)
            await replacement.close()
    asyncio.run(scenario())


# spec: ANYM-997
def test_unknown_forward_acquisition_is_sticky_without_retry_or_foreign_cleanup(world):
    async def scenario():
        attempts = []
        async def uncertain(access):
            attempts.append(access.serial)
            raise OSError('synthetic acquisition reply lost')
        connector = UsbOrderConnector(world.events, owner='owner', device_id='phone', source_id='usb', auth_epoch=1,
            admit=world.admit, bind_usb=uncertain, interval_s=.01, reconnect_s=.01, cleanup_s=.1)
        # A distinct actual-serial fixture keeps this intentionally unresolved slot out of other tests.
        world.serial[0] = 'unknown-acquisition-serial'
        connector.start()
        await until(lambda: connector.task.done())
        await connector.close()
        second = UsbOrderConnector(world.events, owner='owner', device_id='phone', source_id='usb', auth_epoch=1,
            admit=world.admit, bind_usb=uncertain, interval_s=.01, reconnect_s=.01, cleanup_s=.1)
        second.start()
        await until(lambda: second.task.done())
        assert attempts == ['unknown-acquisition-serial']
        assert connector.status()['reason'] == 'forward_acquisition_unknown'
        assert second.status()['reason'] == 'serial_subscriber_busy'
        assert connector.status()['cleanup'] == 'cleanup_unconfirmed'
    asyncio.run(scenario())


# spec: ANYM-997
def test_package_limit_refuses_before_forward_without_truncating_obligations(world):
    async def scenario():
        for i in range(32):
            world.bind_order(f'order-{i}', f'app.package_{i}')
        async with Endpoint(world, quiet) as endpoint:
            connector = endpoint.connector()
            connector.start()
            await until(lambda: connector.task.done())
            assert connector.status()['reason'] == 'watch_package_limit'
            assert endpoint.binds == 0
            assert world.db.execute("SELECT count(*) FROM android_order_obligations WHERE lifecycle='active'").fetchone()[0] == 33
    asyncio.run(scenario())


# spec: ANYM-997
def test_new_package_rebind_uses_same_owned_forward_and_terminal_order_emptywatch(world):
    async def scenario():
        async with Endpoint(world, quiet) as endpoint:
            connector = endpoint.connector()
            connector.start()
            await until(lambda: endpoint.watches)
            world.bind_order('second', 'app.shop')
            await until(lambda: len(endpoint.watches) == 2)
            assert endpoint.watches[1]['packages'] == ['app.food', 'app.shop'] and endpoint.binds == 1
            with world.events.tx():
                world.db.execute("UPDATE android_order_obligations SET lifecycle='received'")
            await until(lambda: connector.task.done())
            assert endpoint.watches[-1]['packages'] == []
            assert connector.status()['cleanup'] == 'confirmed' and endpoint.removals == 1
    asyncio.run(scenario())


# spec: ANYM-997
def test_cleanup_waits_delayed_emptywatch_hello_and_never_claims_timeout_confirmed(world):
    async def scenario():
        async with Endpoint(world, quiet) as endpoint:
            endpoint.cleanup_delay = .06  # > guard interval, within total cleanup deadline
            first = endpoint.connector()
            first.start()
            await until(lambda: first.source.epoch)
            await first.close()
            assert first.status()['cleanup'] == 'confirmed'
            endpoint.cleanup_delay = 2
            second = endpoint.connector()
            second.start()
            await until(lambda: second.source.epoch)
            start = asyncio.get_running_loop().time()
            await second.close()
            assert asyncio.get_running_loop().time() - start < .5
            assert second.status()['cleanup'] == 'cleanup_unconfirmed' and endpoint.removals == 2
    asyncio.run(scenario())


# spec: ANYM-997
def test_disconnect_retries_are_bounded_on_one_capability_and_timer_survives(world):
    async def scenario():
        async def disconnected(endpoint, reader, writer, watch):
            await send(writer, hello())
        async with Endpoint(world, disconnected) as endpoint:
            connector = endpoint.connector(attempts=2)
            connector.start()
            await until(lambda: connector.task.done())
            assert endpoint.binds == 1 and len([w for w in endpoint.watches if w['packages']]) == 2
            assert endpoint.removals == 1 and connector.status()['reason'] == 'source_reconnect_exhausted'
            assert world.db.execute("SELECT next_check_at FROM android_order_obligations").fetchone()[0] == 1200
    asyncio.run(scenario())


# spec: ANYM-997
def test_unrelated_app_and_other_device_orders_cannot_widen_watch_or_wake(world):
    async def scenario():
        async def stream(endpoint, reader, writer, watch):
            await send(writer, hello(1), observation(1, package='app.unrelated'))
            await reader.read()
        async with Endpoint(world, stream) as endpoint:
            world.db.execute("INSERT INTO android_order_obligations SELECT owner,order_id||'-other',"
                " 'other-phone',package||'.other',source_id,source_order_id,auth_epoch,original_request_id,"
                "native_thread_id,native_turn_id,question_id,original_user_words,next_check_at,state_json,lifecycle,"
                "responsible,recipient,revision,seen,handled,task_id,provider_order_id,origin_json,task_only "
                "FROM android_order_obligations WHERE order_id='order'")
            world.db.commit()
            connector = endpoint.connector()
            connector.start()
            await until(lambda: connector.status()['cursor'] == 1)
            assert endpoint.watches[0]['packages'] == ['app.food']
            assert world.db.execute("SELECT count(*) FROM android_order_wakes").fetchone()[0] == 0
            await connector.close()
    asyncio.run(scenario())


# spec: ANYM-997
def test_generation_revoked_while_read_awaits_refuses_buffered_observation(world):
    async def scenario():
        ready, release = asyncio.Event(), asyncio.Event()
        async def stream(endpoint, reader, writer, watch):
            await send(writer, hello(1))
            ready.set()
            await release.wait()
            await send(writer, observation(1))
            await reader.read()
        async with Endpoint(world, stream) as endpoint:
            connector = endpoint.connector()
            connector.start()
            await ready.wait()
            # Backend principal still exists and event-level ACL is unchanged. The
            # original retained lease/run generation alone has become invalid.
            world.fresh[0] = False
            release.set()
            await until(lambda: connector.task.done())
            assert connector.status()['reason'] == 'source_access_changed'
            assert world.db.execute("SELECT count(*) FROM android_order_observations").fetchone()[0] == 0
            assert world.db.execute("SELECT next_check_at FROM android_order_obligations").fetchone()[0] == 1200
            assert endpoint.removals == 1 and len(endpoint.watches) == 1
    asyncio.run(scenario())


# spec: ANYM-997
def test_reauthorize_after_socket_await_before_watch_write(world, monkeypatch):
    import deedkit_android.order_events_usb as usb

    async def scenario():
        original = asyncio.open_connection
        async def changed(*args, **kwargs):
            pair = await original(*args, **kwargs)
            world.serial[0] = 'replaced-serial'
            return pair
        async with Endpoint(world, quiet) as endpoint:
            monkeypatch.setattr(usb.asyncio, 'open_connection', changed)
            connector = endpoint.connector()
            connector.start()
            await until(lambda: connector.task.done())
            assert endpoint.watches == [] and endpoint.removals == 1
            assert connector.status()['reason'] == 'source_access_changed'
            assert connector.status()['cleanup'] == 'cleanup_unconfirmed'
    asyncio.run(scenario())


# spec: ANYM-997
def test_unproved_forward_capability_cannot_remove_foreign_forward(world):
    async def scenario():
        closed = []
        world.serial[0] = 'mismatched-capability-serial'
        async def remove():
            closed.append('foreign')
            return True
        async def bind(access):
            return UsbForward('some-other-serial', 12345, remove, receipt='foreign')
        connector = UsbOrderConnector(world.events, owner='owner', device_id='phone', source_id='usb', auth_epoch=1,
            admit=world.admit, bind_usb=bind, interval_s=.01, reconnect_s=.01, cleanup_s=.1)
        connector.start()
        await until(lambda: connector.task.done())
        assert connector.status()['reason'] == 'forward_acquisition_unknown' and closed == []
    asyncio.run(scenario())


# spec: ANYM-997
def test_forward_cleanup_unknown_retains_original_capability_and_serial_hold(world):
    async def scenario():
        world.serial[0] = 'unknown-cleanup-serial'
        async with Endpoint(world, quiet) as endpoint:
            endpoint.remove_result = None
            connector = endpoint.connector()
            connector.start()
            await until(lambda: connector.source.epoch)
            receipt = connector.forward.receipt
            await connector.close()
            assert connector.forward.receipt is receipt
            assert connector.status()['reason'] == 'forward_cleanup_unknown'
            second = endpoint.connector()
            second.start()
            await until(lambda: second.task.done())
            assert second.status()['reason'] == 'serial_subscriber_busy'
            await connector.close()  # no second removal attempt
            assert endpoint.removals == 1
    asyncio.run(scenario())


# spec: ANYM-997
def test_unknown_native_delivery_is_not_resubmitted_by_new_source_updates(world):
    async def scenario():
        release = asyncio.Event()
        async def stream(endpoint, reader, writer, watch):
            await send(writer, hello(2), observation(1))
            await release.wait()
            await send(writer, observation(2))
            await reader.read()
        async with Endpoint(world, stream) as endpoint:
            connector = endpoint.connector()
            connector.start()
            await until(lambda: connector.status()['cursor'] == 1)
            wake_id = world.db.execute("SELECT wake_id FROM android_order_wakes").fetchone()[0]
            with world.events.tx():
                world.db.execute("UPDATE android_order_wakes SET state='unknown',payload='{}' WHERE wake_id=?", (wake_id,))
            release.set()
            await until(lambda: connector.status()['cursor'] == 2)
            wakes = world.db.execute("SELECT wake_id,state,through FROM android_order_wakes").fetchall()
            assert len(wakes) == 1 and tuple(wakes[0]) == (wake_id, 'unknown', 1)
            assert world.db.execute("SELECT seen FROM android_order_obligations").fetchone()[0] == 2
            await connector.close()
    asyncio.run(scenario())


# spec: ANYM-997
def test_denied_reader_and_no_active_orders_never_acquire_forward(world):
    async def scenario():
        async with Endpoint(world, quiet) as endpoint:
            world.live[0] = False
            denied = endpoint.connector()
            denied.start()
            await until(lambda: denied.task.done())
            assert denied.status()['reason'] == 'source_access_changed'
            world.live[0] = True
            with world.events.tx():
                world.db.execute("UPDATE android_order_obligations SET lifecycle='cancelled'")
            empty = endpoint.connector()
            empty.start()
            await until(lambda: empty.task.done())
            assert empty.status()['reason'] == 'no_active_packages'
            assert endpoint.binds == 0 and endpoint.watches == []
    asyncio.run(scenario())
