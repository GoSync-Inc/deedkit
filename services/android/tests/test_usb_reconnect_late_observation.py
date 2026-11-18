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


def observed(service, synthetic, roles):
    """Bounded synthetic state for a failed wait: forward rows, USB entries, runs, handles and fake ADB calls."""
    agents, role = service.app.state.agents, lambda request: roles.get(request, 'other')
    state = {}
    try:
        state['forwards'] = [(role(r['request_id']), r['state'], r['collection'], r['reason'], r['recovery'])
                             for r in forwards(service)]
        state['entries'] = [(role(e.task), e.handle is not None, e.receipt is not None, e.connector and (
            e.connector.state, e.connector.reason, e.connector.cleanup, e.connector.task and e.connector.task.done()))
            for e in list(agents.usb.entries.values())]
        state['ended'] = sorted(role(task) for _, task in list(agents.usb.ended))
        state['runs'] = sorted((role(r.id), r.state) for r in list(agents.runs.values()))
        state['handles'] = sorted((role(task), h.reason) for (_, task), h in list(agents.supervision.handles.items()))
        state['calls'] = {flag: count(synthetic, flag) for flag in ('--no-rebind', '--remove', '--list')}
        state['table'], state['packages'] = len(synthetic.table), list(synthetic.phone.packages)
    except (RuntimeError, AttributeError, KeyError) as error:  # a concurrent change: report what was read
        state['incomplete'] = type(error).__name__
    return state


def until_or_state(predicate, service, synthetic, roles):
    """until(), adding the bounded synthetic state to its timeout message."""
    try:
        return until(predicate)
    except AssertionError as error:
        raise AssertionError('%s; state=%r' % (error, observed(service, synthetic, roles))) from None


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
        first = service.finished(service.start(token, goal='native-control')['request_id'])['request_id']
        lost = lose_usb(rig, service, 0)
        rig.phone.plug()
        start = len(rig.calls)
        reconnected = second_request(service, token)['request_id']
        second = lose_usb(rig, service, 1)  # the reconnected collection is lost again
        rig.phone.journal = journal
        rig.phone.plug()
        later = len(rig.calls)
        third = second_request(service, token)['request_id']  # another ordinary request reconnects
        rows = until_or_state(lambda: (lambda r: r if len(r) >= 3 and r[1]['state'] == 'closed' else None)(forwards(service)),
                              service, rig, {first: 'first', reconnected: 'second', third: 'third'})
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
