"""A normal request's own USB source across that request's success-to-retention handoff.

Merge-queue run 43883072117 timed out on the supersession wait of
test_usb_loss_keeps_phone_collection_held_until_reconnected_normal_request_supersedes_it_once. The second request's
run-originated USB entry can be admitted after AgentRun.end has set 'succeeded' and before Supervision.finished has
retained that request's handle (PersistentRun.final first awaits the final device reads). Event barriers fix that
order here; the scenario and its assertions repeat the original test.
"""

import asyncio
import threading
import time
from types import SimpleNamespace

from agent_harness import Service
from test_order_supervision import until
from test_usb_order_restart_recovery import before_acquisition, count, forwards, observation_wakes, second_request, settled_wake
from test_usb_order_restart_recovery import rig  # noqa: F401 - `rig` is the shared fixture


async def released(event):
    deadline = time.monotonic() + 5  # a bound, not a delay: an absent barrier leaves the test to its assertions
    while not event.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(.01)


def handoff(monkeypatch, first):
    """Order the next foreground request other than `first` around its own run-originated USB entry.

    1. The supervision tick first observes that request once its native turn is admitted (not the earlier handle).
    2. Its turn completes only after the tick built the entry from the still running request.
    3. That entry's connector takes its first step only once the run is 'succeeded' and its handle is not retained.
    4. Retention waits until the entry was admitted inside that window, by its connector or by the tick.
    """
    from deedkit_android import order_usb_host
    from deedkit_android.codex_session import PersistentRun
    from deedkit_android.order_events_usb import UsbOrderConnector
    window = SimpleNamespace(run=None, open=threading.Event(), admitted=threading.Event())
    refresh, consume, final = order_usb_host.UsbLifecycle.refresh, PersistentRun.consume, PersistentRun.final
    connect, admit = UsbOrderConnector._run, order_usb_host.Entry.admit

    def new(run):
        return type(run) is PersistentRun and run.id != first

    def refreshing(usb):
        if not any(new(r) and r.state == 'running' and r.native_admitted_turn is None for r in usb.agents.runs.values()):
            refresh(usb)

    async def consuming(run, event):
        if window.run is None and new(run) and event.get('method') == 'turn/completed':
            window.run = run
            deadline = time.monotonic() + 5
            while not any(e.origin is run for e in run.agents.usb.entries.values()) and time.monotonic() < deadline:
                await asyncio.sleep(.01)
        return await consume(run, event)

    async def finishing(run, state, extra):
        if run is window.run and not window.open.is_set():
            window.open.set()  # 'succeeded' is set; Supervision.finished has not retained the handle yet
            await released(window.admitted)
        return await final(run, state, extra)

    async def connecting(connector):
        await released(window.open)
        return await connect(connector)

    def admitting(entry):
        try:
            return admit(entry)
        finally:
            if window.open.is_set() and entry.origin is window.run:
                window.admitted.set()
    monkeypatch.setattr(order_usb_host.UsbLifecycle, 'refresh', refreshing)
    monkeypatch.setattr(PersistentRun, 'consume', consuming)
    monkeypatch.setattr(PersistentRun, 'final', finishing)
    monkeypatch.setattr(UsbOrderConnector, '_run', connecting)
    monkeypatch.setattr(order_usb_host.Entry, 'admit', admitting)
    return window


# spec: ANYM-997
def test_request_succeeding_before_its_usb_source_first_admission_still_supersedes_the_held_forward_once(rig, monkeypatch):  # noqa: F811
    journal, rig.phone.journal = rig.phone.journal, []  # the notification arrives while USB is lost
    with Service(rig.config) as service:
        token = service.lease()
        service.finished(service.start(token, goal='native-control')['request_id'])
        until(lambda: rig.phone.packages and forwards(service)[0]['collection'] == 'unconfirmed')
        rig.phone.journal = journal
        rig.phone.unplug()
        rig.table.clear()  # the ADB server drops the transport's forward
        lost, = until(lambda: [r for r in forwards(service) if r['state'] == 'held'])
        assert lost['reason'] == 'collection_cleanup_unconfirmed' and lost['collection'] == 'unconfirmed'
        assert rig.phone.packages == ['app.food']
        rig.phone.plug()
        start = len(rig.calls)
        window = handoff(monkeypatch, lost['request_id'])
        second_request(service, token)
        assert window.run is not None and window.admitted.is_set()  # admitted between 'succeeded' and retention
        old, new = until(lambda: (lambda r: r if len(r) == 2 and r[0]['state'] == 'closed' else None)(forwards(service)))
        assert old['id'] == lost['id'] and old['recovery'] == 'collection_superseded'
        assert old['reason'] == 'collection_cleanup_unconfirmed' and new['id'] != old['id']
        assert count(rig, '--no-rebind', start) >= 1 and not [c for c in before_acquisition(rig, start) if '--remove' in c]
        settled_wake(service)
        time.sleep(.2)
        woken = [w['order_id'] for w in observation_wakes(service)]
        assert woken and len(woken) == len(set(woken))  # at most one wake per order bound at observation time
