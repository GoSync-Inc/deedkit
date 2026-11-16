"""A normal request's own USB source across that request's success-to-retention handoff.

Merge-queue run 43883072117 timed out on the supersession wait of
test_usb_loss_keeps_phone_collection_held_until_reconnected_normal_request_supersedes_it_once. The second request's
run-originated USB entry can be admitted after AgentRun.end has set 'succeeded' and before Supervision.finished has
retained that request's handle (PersistentRun.final first awaits the final device reads). Event barriers fix that
order here; the first scenario and its assertions repeat the original test. That window continues the run's own
admitted generation only while the run is still live, holds the device and has completed its admitted turn.
"""

import asyncio
import functools
import threading
import time
from types import SimpleNamespace

import pytest

from agent_harness import Service
from test_order_supervision import until
from test_usb_order_restart_recovery import before_acquisition, count, forwards, observation_wakes, second_request, settled_wake
from test_usb_order_restart_recovery import rig  # noqa: F401 - `rig` is the shared fixture


async def released(ready):
    deadline = time.monotonic() + 5  # a bound, not a delay: an absent barrier leaves the test to its assertions
    while not ready() and time.monotonic() < deadline:
        await asyncio.sleep(.01)


def handoff(monkeypatch, first, ready=lambda run: True):
    """Order the next foreground request other than `first` around its own run-originated USB entry.

    1. The supervision tick first observes that request once its native turn is admitted (not the earlier handle).
    2. Its turn completes only after the tick built the entry from the still running request.
    3. That entry's connector takes its first step only once the run is 'succeeded' and its handle is not retained.
    4. Retention waits until the entry was admitted inside that window, by its connector or by the tick, and ready(run).
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
            await released(lambda: any(e.origin is run for e in run.agents.usb.entries.values()))
        return await consume(run, event)

    async def finishing(run, state, extra):
        if run is window.run and not window.open.is_set():
            window.open.set()  # 'succeeded' is set; Supervision.finished has not retained the handle yet
            await released(lambda: window.admitted.is_set() and ready(run))
        return await final(run, state, extra)

    async def connecting(connector):
        await released(window.open.is_set)
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


# spec: ANYM-997
def test_denied_retention_ends_the_admitted_run_collection_at_release_without_a_second_forward(rig, monkeypatch):  # noqa: F811
    from deedkit_android import order_usb_host
    from deedkit_android.config import Refused
    from deedkit_android.order_supervision import Supervision
    rig.phone.journal = []
    # The phone holds the request's own watch before retention is denied; the reader's own once-a-second recheck is
    # set aside so the tick's refusal after release is the one stop, and the cleanup it starts runs uninterrupted.
    monkeypatch.setattr(order_usb_host, 'UsbOrderConnector', functools.partial(order_usb_host.UsbOrderConnector, interval_s=60))
    window = handoff(monkeypatch, None, ready=lambda run: rig.phone.packages == ['app.food'] and any(
        e.origin is run and e.connector.state == 'reading' for e in run.agents.usb.entries.values()))
    retain = Supervision.retain

    def denied(supervision, run):
        if run is window.run:
            raise Refused('supervision_lease_changed')
        return retain(supervision, run)
    monkeypatch.setattr(Supervision, 'retain', denied)
    with Service(rig.config) as service:
        request = service.start(service.lease(), goal='native-control')['request_id']
        assert service.finished(request)['state'] == 'succeeded'
        assert window.admitted.is_set() and service.app.state.agents.supervision.refusals[('one', request)] == 'supervision_lease_changed'
        row, = until(lambda: [r for r in forwards(service) if r['state'] == 'closed'])
        assert row['request_id'] == request and row['collection'] == 'confirmed'
        assert rig.phone.packages == [] and count(rig, '--no-rebind') == 1  # the empty watch's hello, no reacquisition


def awaiting_retention():
    """A run-originated entry whose run succeeded and still holds the device; every outer authority check passes."""
    from deedkit_android.order_usb_host import Entry
    entry, held = Entry.__new__(Entry), [True]
    principal = SimpleNamespace(id='one', devices=['home'], permissions=['android:control'])
    session = SimpleNamespace(process=SimpleNamespace(returncode=None), thread_id='thread', disconnected=False,
                              policy_proven=True, request_id=None, turn_id=None)
    origin = SimpleNamespace(state='succeeded', native_admitted_turn='turn-1', native_completed_turn='turn-1')
    config = SimpleNamespace(agent=SimpleNamespace(enabled=True, guarded=False, persistent=True, order_events='source'),
                             devices={'home': 'serial'}, adb='adb')
    supervision = SimpleNamespace(handles={}, authorities=[], boundary=lambda *_: 'boundary')
    supervision.authority = supervision.authorities.append
    agents = SimpleNamespace(read_config=lambda: config, runs={('one', 'task'): origin},
                             store=SimpleNamespace(check_reservation=lambda *_: None),
                             sessions={('one', 'home'): session}, supervision=supervision,
                             devices=SimpleNamespace(locks={'home': SimpleNamespace(locked=lambda: held[0])}))
    entry.__dict__.update(manager=SimpleNamespace(entries={}), agents=agents, origin=origin, handle=None, owner='one',
                          device='home', task='task', request=SimpleNamespace(session_id='lease'),
                          authorize=lambda: principal, session=session, process=session.process, thread='thread',
                          source='source', serial='serial', adb='adb', boundary='boundary', connector=None, closing=False)
    entry.manager.entries[('one', 'home')] = entry
    return entry, held


def matching(entry):
    return SimpleNamespace(authorize=entry.authorize, session=entry.session, process=entry.process,
                           request=entry.request, boundary=entry.boundary)


# spec: ANYM-997
@pytest.mark.parametrize('case', ['awaiting_retention', 'retained', 'released', 'replaced_run', 'turn_not_completed',
                                  'other_turn', 'device_free', 'foreign_handle'])
def test_success_awaiting_retention_admits_only_the_live_run_holding_the_device_after_its_completed_turn(case):
    from deedkit_android.config import Refused
    entry, held = awaiting_retention()
    agents, origin, key = entry.agents, entry.origin, ('one', 'task')
    if case == 'retained':
        agents.supervision.handles[key] = handle = matching(entry)
    elif case == 'released':  # Agents.release: the run leaves the table and its device lock is free
        del agents.runs[key]
        held[0] = False
    elif case == 'replaced_run':
        agents.runs[key] = SimpleNamespace(**vars(origin))
    elif case == 'turn_not_completed':
        origin.native_completed_turn = None
    elif case == 'other_turn':
        origin.native_completed_turn = 'turn-0'
    elif case == 'device_free':
        held[0] = False
    elif case == 'foreign_handle':
        agents.supervision.handles[key] = SimpleNamespace(**{**vars(matching(entry)), 'session': SimpleNamespace()})
    if case in ('awaiting_retention', 'retained'):
        assert entry.admit().serial == 'serial'
        assert (entry.handle, agents.supervision.authorities) == ((handle, [handle]) if case == 'retained' else (None, []))
    else:
        with pytest.raises(Refused) as refused:
            entry.admit()
        assert refused.value.code == 'usb_source_generation_changed' and entry.handle is None
