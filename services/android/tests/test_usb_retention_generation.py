"""A retained handle is the same request even if its connector ended before retention."""
import asyncio
import threading

from agent_harness import Service
from test_order_supervision import until
from test_usb_order_restart_recovery import count, forwards, second_request
from test_usb_order_restart_recovery import rig  # noqa: F401 - shared synthetic listener/ADB fixture


# spec: ANYM-997
def test_usb_loss_before_retention_never_reacquires_for_the_same_request_handle(rig, monkeypatch):  # noqa: F811
    from deedkit_android.agent_bare import BareRun
    from deedkit_android.codex_session import PersistentRun
    from deedkit_android.order_usb_host import UsbLifecycle

    entered, release, refreshed = threading.Event(), threading.Event(), threading.Event()
    original_final, original_refresh, original_consume = BareRun.final, UsbLifecycle.refresh, PersistentRun.consume
    first, prior, snapshots = [], [], []

    async def consume(run, event):
        if not first and event.get('method') == 'turn/completed':
            first.append(run)
            # Establish this request's owned watch while it is still running, before holding retention.
            async with asyncio.timeout(5):
                while not rig.phone.packages:
                    await asyncio.sleep(.002)
        return await original_consume(run, event)

    async def final(run, state, extra):
        result = await original_final(run, state, extra)
        if first and run is first[0]:
            entered.set()
            async with asyncio.timeout(5):
                while not release.is_set():
                    await asyncio.sleep(.002)
        return result

    def refresh(manager):
        original_refresh(manager)
        if (prior and first and ('one', first[0].id) in manager.agents.supervision.handles
                and not refreshed.is_set()):
            snapshots.append(manager.entries.get(('one', 'home')) is prior[0])
            refreshed.set()

    monkeypatch.setattr(BareRun, 'final', final)
    monkeypatch.setattr(PersistentRun, 'consume', consume)
    monkeypatch.setattr(UsbLifecycle, 'refresh', refresh)
    journal, rig.phone.journal = rig.phone.journal, []
    try:
        with Service(rig.config) as service:
            token = service.lease()
            request = service.start(token, goal='native-control')['request_id']
            assert entered.wait(5)
            agents = service.app.state.agents
            prior.append(agents.usb.entries[('one', 'home')])
            assert prior[0].handle is None  # the run is still in final(), before retention
            rig.phone.unplug()
            rig.table.clear()
            lost, = until(lambda: [r for r in forwards(service) if r['state'] == 'held'])
            until(lambda: prior[0].connector.task.done())
            assert lost['reason'] == 'collection_cleanup_unconfirmed'
            release.set()
            assert refreshed.wait(5)
            assert snapshots == [True], 'retaining the original request must not replace its ended USB entry'
            assert count(rig, '--no-rebind') == 1

            rig.phone.journal = journal
            rig.phone.plug()
            second_request(service, token)
            old, new = until(lambda: (lambda rows: rows if len(rows) == 2 and rows[0]['state'] == 'closed'
                                     else None)(forwards(service)))
            assert old['id'] == lost['id'] and old['recovery'] == 'collection_superseded'
            assert new['request_id'] != request and count(rig, '--no-rebind') == 2
    finally:
        release.set()
