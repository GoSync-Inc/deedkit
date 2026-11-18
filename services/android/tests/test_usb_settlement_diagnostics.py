"""Why test_an_ended_request_handle_never_reacquires_while_the_new_request_awaits_admission saw a pending connector
task after its forward row read 'held' (CI run 44354309423).

Same synthetic listener/ADB flow as that test, up to the USB loss. A barrier inside ForwardJournal.save, right after
the 'held' row is committed, stops the event-loop thread there while the test thread reads what any cross-thread
observer reads at that instant. A loop callback queued at the barrier shows whether the connector task suspends again
after that commit. The real lifecycle runs; only that one commit is held, for at most 5 s.
"""
import asyncio
import threading

from agent_harness import Service
from test_order_supervision import until
from test_usb_order_restart_recovery import forwards
from test_usb_order_restart_recovery import rig  # noqa: F401 - shared synthetic listener/ADB fixture


# spec: ANYM-997
def test_a_committed_held_forward_is_visible_before_its_connector_task_completes_in_that_same_loop_step(
        rig, monkeypatch, record_property):  # noqa: F811
    from deedkit_android import order_usb_host
    real, committed, resume, held, after = order_usb_host.ForwardJournal.save, threading.Event(), threading.Event(), [], []

    def save(self, ident, **values):
        real(self, ident, **values)
        if values.get('state') == 'held' and not committed.is_set():
            task = asyncio.current_task()
            held.append((ident, values.get('reason'), task))
            # Runs after the step that committed the row: is the connector task done by then?
            asyncio.get_running_loop().call_soon(lambda: after.append(task.done()))
            committed.set()
            assert resume.wait(5)
    monkeypatch.setattr(order_usb_host.ForwardJournal, 'save', save)
    journal, rig.phone.journal = rig.phone.journal, []
    try:
        with Service(rig.config) as service:
            token = service.lease()
            first = service.start(token, goal='native-control')['request_id']
            service.finished(first)
            until(lambda: rig.phone.packages and forwards(service)[0]['collection'] == 'unconfirmed')
            rig.phone.journal = journal
            rig.phone.unplug()
            rig.table.clear()
            assert committed.wait(5)
            # What the existing test's until() can return at this instant, from its own thread:
            lost, = [r for r in forwards(service) if r['state'] == 'held']
            ended = service.app.state.agents.usb.entries[('one', 'home')]
            ident, reason, task = held[0]
            visible = {'row': (lost['request_id'] == first, lost['id'] == ident == ended.receipt, lost['reason']),
                       'entry_task_is_first': ended.task == first, 'connector_task_done': ended.connector.task.done(),
                       'saving_task_is_connector': task is ended.connector.task}
            resume.set()
            until(lambda: after)
            until(lambda: ended.connector.task.done())  # a stable read of the same condition
            record_property('dk90_held_commit', {**visible, 'done_after_that_step': after[0], 'reason': reason})
            assert visible == {'row': (True, True, 'collection_cleanup_unconfirmed'), 'entry_task_is_first': True,
                               'connector_task_done': False, 'saving_task_is_connector': True}, visible
            assert after == [True]  # no await between the commit and completion: no window inside the loop
    finally:
        resume.set()
