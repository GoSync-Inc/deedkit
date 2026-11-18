"""A request whose USB connector ended never acquires again through its retained handle.

Request A's collection ends on USB loss; request B reconnects and its collection ends on a second USB loss.
Both still have active orders and retained handles; neither acquires again. A different, ordinarily admitted
request C reconnects once. Synthetic ADB forward table and phone listener; no notification is replayed.
"""
import threading

from agent_harness import Service
from test_order_supervision import until
from test_usb_order_restart_recovery import before_acquisition, count, forwards, second_request
from test_usb_order_restart_recovery import rig  # noqa: F401 - shared synthetic listener/ADB fixture


def lose_usb(synthetic, service, row):
    """Unplug once the given forward's watch is established; return that row once it is held."""
    until(lambda: len(forwards(service)) > row and forwards(service)[row]['collection'] == 'unconfirmed')
    synthetic.phone.unplug()
    synthetic.table.clear()  # the ADB server drops the transport's forward
    return until(lambda: (lambda r: r[row] if r[row]['state'] == 'held' else None)(forwards(service)))


# spec: ANYM-997
def test_ended_requests_never_reacquire_through_retained_handles_and_a_new_request_reconnects_once(rig, monkeypatch):  # noqa: F811,E501
    from deedkit_android.order_usb_host import UsbLifecycle
    original, ticks, lock = UsbLifecycle.refresh, [0], threading.Lock()

    def refresh(manager):
        original(manager)
        with lock:
            ticks[0] += 1
    monkeypatch.setattr(UsbLifecycle, 'refresh', refresh)
    rig.phone.journal = []  # no notification: every order stays active, so every handle stays retained
    with Service(rig.config) as service:
        supervision = service.app.state.agents.supervision
        token = service.lease()
        first = service.finished(service.start(token, goal='native-control')['request_id'])['request_id']
        lose_usb(rig, service, 0)
        rig.phone.plug()
        second = second_request(service, token)['request_id']
        lost = lose_usb(rig, service, 1)  # the reconnected collection ends too
        assert lost['request_id'] == second
        rig.phone.plug()
        acquired = count(rig, '--no-rebind')
        assert {('one', first), ('one', second)} <= set(supervision.handles)  # both ended requests retained
        seen = ticks[0]
        until(lambda: ticks[0] >= seen + 25)  # ordinary supervision ticks with the phone plugged in again
        assert {('one', first), ('one', second)} <= set(supervision.handles)
        assert count(rig, '--no-rebind') == acquired, 'an ended request acquired again: %r' % [
            (r['request_id'] == first, r['request_id'] == second, r['state']) for r in forwards(service)]
        assert len(forwards(service)) == 2
        start = len(rig.calls)
        third = second_request(service, token)['request_id']  # a different, ordinarily admitted request
        rows = until(lambda: (lambda r: r if len(r) == 3 and r[1]['state'] == 'closed' else None)(forwards(service)))
        assert rows[2]['request_id'] == third and rows[1]['recovery'] == 'collection_superseded'
        assert count(rig, '--no-rebind', start) == 1
        assert not [c for c in before_acquisition(rig, start) if '--remove' in c]
