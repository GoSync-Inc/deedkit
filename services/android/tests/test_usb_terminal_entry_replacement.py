"""A USB entry that ended before it claimed the serial or asked for a forward does not strand later requests.

Real UsbLifecycle and UsbOrderConnector over the shared synthetic ADB forward table and phone listener.
The only barrier holds the first connectors' tasks before their first statement.
"""
import asyncio
import json
import threading
import time

from agent_harness import Service
from test_order_supervision import until
from test_usb_order_restart_recovery import before_acquisition, count, forwards, observation_wakes, second_request
from test_usb_order_restart_recovery import settled_wake
from test_usb_order_restart_recovery import rig  # noqa: F401 - shared synthetic listener/ADB fixture


def hold_connectors(monkeypatch, n):
    """Keep the first n connectors' tasks from starting until their own returned event is set."""
    from deedkit_android.order_events_usb import UsbOrderConnector
    original, started, gates = UsbOrderConnector._run, [], [threading.Event() for _ in range(n)]

    async def run(connector):
        started.append(connector)
        if len(started) <= n:
            gate = gates[len(started) - 1]
            while not gate.is_set():
                await asyncio.sleep(.005)
        return await original(connector)
    monkeypatch.setattr(UsbOrderConnector, '_run', run)
    return started, gates


def hold_first_connector(monkeypatch):
    started, (gate,) = hold_connectors(monkeypatch, 1)
    return started, gate


def set_epoch(synthetic, epoch):
    body = json.loads(synthetic.config.read_text())
    body['agent']['order_events']['auth_epoch'] = epoch
    synthetic.config.write_text(json.dumps(body))


def untouched(entry):
    """The ended entry wrote no receipt, never claimed the serial and never called bind."""
    return entry.connector.task.done() and entry.receipt is None and entry.connector.cleanup == 'not_needed'


# spec: ANYM-997
def test_entry_cancelled_before_its_first_step_does_not_strand_a_new_lease_request(rig, monkeypatch):  # noqa: F811
    started, gate = hold_first_connector(monkeypatch)
    with Service(rig.config) as service:
        token = service.lease()
        first = service.start(token, goal='native-control')['request_id']
        until(lambda: started)
        entry = service.app.state.agents.usb.entries[('one', 'home')]
        service.post('session', dict(action='release', device_id='home', session_id=token))
        until(lambda: entry.connector.task.done())
        assert untouched(entry) and entry.connector.task.cancelled()
        assert rig.calls == [] and forwards(service) == []
        gate.set()
        time.sleep(.2)
        assert rig.calls == []  # the released request never resumes or acquires
        second = second_request(service, None)['request_id']
        row, = until(lambda: forwards(service))
        assert row['request_id'] == second != first
        assert count(rig, '--no-rebind') == 1 and not [c for c in before_acquisition(rig, 0) if '--remove' in c]
        settled_wake(service)
        time.sleep(.2)
        woken = [w['order_id'] for w in observation_wakes(service)]
        assert woken and len(woken) == len(set(woken))  # at most one wake per order bound at observation time
        assert len(forwards(service)) == 1 and count(rig, '--no-rebind') == 1


# spec: ANYM-997
def test_access_epoch_change_keeps_the_original_request_held_and_old_epoch_orders_uncollected(rig, monkeypatch):  # noqa: F811
    started, gate = hold_first_connector(monkeypatch)
    with Service(rig.config) as service:
        token = service.lease()
        first = service.start(token, goal='native-control')['request_id']
        until(lambda: started)
        usb = service.app.state.agents.usb
        entry = usb.entries[('one', 'home')]
        body = json.loads(rig.config.read_text())
        body['agent']['order_events']['auth_epoch'] = 2
        rig.config.write_text(json.dumps(body))
        until(lambda: entry.connector.task.done())
        gate.set()
        assert untouched(entry)
        service.finished(first)
        time.sleep(.3)
        # The original request (its run, then its retained handle) never gets another entry or acquisition.
        assert usb.entries[('one', 'home')] is entry and len(started) == 1
        assert rig.calls == [] and forwards(service) == []
        # A new request has no active order at the new epoch: nothing is collected and the ended entry
        # stays until a request is actually admitted in its place.
        view = second_request(service, token)
        time.sleep(.3)
        assert usb.entries[('one', 'home')] is entry and len(started) == 1
        assert rig.calls == [] and forwards(service) == []
        assert service.get(view['request_id'])['usb_source']['state'] == 'held'


# spec: ANYM-997
def test_bind_that_failed_before_its_receipt_stays_held_for_later_requests(rig, monkeypatch):  # noqa: F811
    from deedkit_android import order_usb_host
    fake, failed = order_usb_host.run_process, []

    async def command(argv, timeout, **kwargs):
        if argv[-1] == '--list' and not failed:
            failed.append(argv)
            return 1, b'', b''  # the pre-acquisition listing is unconfirmed
        return await fake(argv, timeout, **kwargs)
    monkeypatch.setattr(order_usb_host, 'run_process', command)
    with Service(rig.config) as service:
        token = service.lease()
        service.finished(service.start(token, goal='native-control')['request_id'])
        entry = service.app.state.agents.usb.entries[('one', 'home')]
        until(lambda: entry.connector.task.done())
        assert entry.receipt is None and entry.connector.cleanup == 'cleanup_unconfirmed'
        view = second_request(service, token)
        time.sleep(.3)
        assert service.app.state.agents.usb.entries[('one', 'home')] is entry
        assert failed and count(rig, '--list') == 0 and count(rig, '--no-rebind') == 0
        assert forwards(service) == []
        source = service.get(view['request_id'])['usb_source']
        assert (source['state'], source['reason']) == ('held', 'forward_acquisition_unknown')


# spec: ANYM-997
def test_an_ended_request_is_not_admitted_again_after_its_replacement_also_ends_untouched(rig, monkeypatch):  # noqa: F811
    started, gates = hold_connectors(monkeypatch, 2)
    with Service(rig.config) as service:
        usb = service.app.state.agents.usb
        token = service.lease()
        first = service.start(token, goal='native-control')['request_id']
        until(lambda: started)
        ended = [usb.entries[('one', 'home')]]
        for gate in gates:
            set_epoch(rig, 2)  # an access change ends the held connector before its first step
            until(lambda: ended[-1].connector.task.done())
            gate.set()
            set_epoch(rig, 1)  # access restored: the ended requests stay ended
            assert untouched(ended[-1])
            if len(ended) == 1:
                service.finished(first)
                time.sleep(.3)
                assert usb.entries[('one', 'home')] is ended[0] and len(started) == 1
                second = service.start(token, goal='native-control')['request_id']
                until(lambda: len(started) == 2)
                ended.append(usb.entries[('one', 'home')])
                assert ended[1] is not ended[0] and ended[1].task == second
        service.finished(second)
        time.sleep(.5)
        # Neither ended request (run or retained handle) takes the entry back or acquires.
        assert usb.entries[('one', 'home')] is ended[1] and len(started) == 2
        assert rig.calls == [] and forwards(service) == []
        third = second_request(service, token)['request_id']
        row, = until(lambda: forwards(service))
        assert row['request_id'] == third and count(rig, '--no-rebind') == 1
        assert {e.task for e in ended} == {first, second}
