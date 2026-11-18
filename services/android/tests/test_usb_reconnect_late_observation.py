"""USB loss then reconnect, read for the first time only after a later ordinary reconnect added more rows.

Same synthetic flow as test_usb_loss_keeps_phone_collection_held_until_reconnected_normal_request_supersedes_it_once,
then a second USB loss and another ordinary request, so the forward table deterministically holds at least three
rows before the first reconnect's outcome is read. Rows are never removed: that outcome stays in the first two rows.
The notification reaches the phone only before the last reconnect, so no wake can end collection earlier.
"""
import time

from agent_harness import Service
from test_order_supervision import until
from test_usb_order_restart_recovery import before_acquisition, count, forwards, observation_wakes, second_request
from test_usb_order_restart_recovery import settled_wake
from test_usb_order_restart_recovery import rig  # noqa: F401 - shared synthetic listener/ADB fixture


def lose_usb(synthetic, service, row):
    """Unplug once the given forward's watch is established; return that row once it is held."""
    until(lambda: synthetic.phone.packages and len(forwards(service)) > row
          and forwards(service)[row]['collection'] == 'unconfirmed')
    synthetic.phone.unplug()
    synthetic.table.clear()  # the ADB server drops the transport's forward
    return until(lambda: (lambda r: r[row] if r[row]['state'] == 'held' else None)(forwards(service)))


# spec: ANYM-997
def test_usb_reconnect_supersession_reads_the_same_when_first_observed_after_a_later_reconnect(rig):  # noqa: F811
    journal, rig.phone.journal = rig.phone.journal, []  # no notification until the last reconnect
    with Service(rig.config) as service:
        token = service.lease()
        service.finished(service.start(token, goal='native-control')['request_id'])
        lost = lose_usb(rig, service, 0)
        rig.phone.plug()
        start = len(rig.calls)
        second_request(service, token)
        second = lose_usb(rig, service, 1)  # the reconnected collection is lost again
        rig.phone.journal = journal
        rig.phone.plug()
        later = len(rig.calls)
        second_request(service, token)  # another ordinary request reconnects
        rows = until(lambda: (lambda r: r if len(r) >= 3 and r[1]['state'] == 'closed' else None)(forwards(service)))
        # The first observation of the first reconnect's outcome, made only after later rows exist.
        assert rows[0]['state'] == 'closed'
        old, new = rows[:2]
        assert old['id'] == lost['id'] and old['recovery'] == 'collection_superseded'
        assert old['reason'] == 'collection_cleanup_unconfirmed' and new['id'] != old['id']
        assert new['id'] == second['id'] and new['recovery'] == 'collection_superseded'
        assert count(rig, '--no-rebind', start) >= 2
        assert not [c for c in before_acquisition(rig, start) if '--remove' in c]
        assert not [c for c in before_acquisition(rig, later) if '--remove' in c]
        settled_wake(service)
        time.sleep(.2)
        woken = [w['order_id'] for w in observation_wakes(service)]
        assert woken and len(woken) == len(set(woken))  # at most one wake per order bound at observation time
