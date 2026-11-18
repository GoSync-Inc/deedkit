"""A request whose USB connector ended after USB loss never acquires again while a new request is admitted.

Same synthetic flow as test_usb_loss_keeps_phone_collection_held_until_reconnected_normal_request_supersedes_it_once.
The only barrier holds the new request's run before its native turn is admitted (app skill offer).
"""
import threading
import time

from agent_harness import Service
from test_order_supervision import until
from test_usb_order_restart_recovery import count, forwards
from test_usb_order_restart_recovery import rig  # noqa: F401 - shared synthetic listener/ADB fixture


# spec: ANYM-997
def test_an_ended_request_handle_never_reacquires_while_the_new_request_awaits_admission(rig, monkeypatch):  # noqa: F811
    from deedkit_android import app_skill_bundle
    original, entered, release, offers = app_skill_bundle.offer, threading.Event(), threading.Event(), []

    def offer(*args, **kwargs):
        offers.append(args)
        if len(offers) == 2:  # the reconnecting request, before its native turn is admitted
            entered.set()
            assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(app_skill_bundle, 'offer', offer)
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
            lost, = until(lambda: [r for r in forwards(service) if r['state'] == 'held'])
            usb = service.app.state.agents.usb
            ended = usb.entries[('one', 'home')]
            assert ended.task == first and ended.connector.task.done()
            rig.phone.plug()
            second = service.start(token, goal='native-control')['request_id']
            assert entered.wait(5)
            time.sleep(.3)  # ordinary supervision ticks while the new request is not admitted yet
            rows = forwards(service)
            assert [r['request_id'] for r in rows[1:]] == [], 'the ended request reacquired: %r' % [
                (r['request_id'] == first, r['state']) for r in rows]
            assert count(rig, '--no-rebind') == 1
            release.set()
            service.finished(second)
            old, new = until(lambda: (lambda r: r if len(r) == 2 and r[0]['state'] == 'closed' else None)(
                forwards(service)))
            assert old['id'] == lost['id'] and new['request_id'] == second
    finally:
        release.set()
