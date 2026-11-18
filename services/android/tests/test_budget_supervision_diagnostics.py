"""Why test_exhausted_active_budget_refuses_due_input_before_any_native_preflight can read no budget (CI run 44355808074).

Same synthetic native flow and the same .3 s timer as that test (host(delay=.3)). A gate on Supervision.tick (closed:
the whole tick is skipped, so nothing is pruned or admitted) orders the two events the test races: the test thread's
budget read and the timer wake's admission. The real lifecycle runs; only the tick's start is held.
"""
import threading
import time

import pytest

from agent_harness import Service
from test_order_supervision import host, messages, rows, until, wakes


@pytest.fixture
def gated(monkeypatch):
    from deedkit_android import order_supervision
    real, gate, ticks = order_supervision.Supervision.tick, threading.Event(), []

    async def tick(self):
        if not gate.is_set():
            return  # held: no refresh, no budget pruning, no due-wake admission
        ticks.append(1)
        return await real(self)
    monkeypatch.setattr(order_supervision.Supervision, 'tick', tick)
    return gate, ticks


def turns(record):
    return len([r for r in messages(record) if r['message'].get('method') == 'turn/start'])


# spec: ANYM-993, ANYM-997
def test_budget_exhausted_before_the_first_tick_refuses_the_due_wake_before_any_native_input(tmp_path, monkeypatch,
                                                                                             gated, record_property):
    """Positive control: the ordering the existing test intends, without depending on how late its read is."""
    gate, ticks = gated
    config, record = host(tmp_path, monkeypatch, delay=.3)
    with Service(config) as service:
        request = service.start(service.lease(), goal='native-control')
        service.finished(request['request_id'])
        supervision, key = service.app.state.agents.supervision, ('one', request['request_id'])
        assert not ticks and key in supervision.budgets and key in supervision.handles  # retain made both
        due, = rows(service, 'SELECT next_check_at FROM android_order_obligations')
        slack = round(due['next_check_at'] - time.time(), 3)  # what an ungated test has left for its read
        budget = supervision.budgets[key]
        budget['seconds_used'] = budget['seconds_limit']
        before, foreground = record.read_text(), turns(record)
        gate.set()
        until(lambda: wakes(service) and wakes(service)[0]['hold_reason'] == 'lease_unavailable')
        assert record.read_text() == before and turns(record) == foreground == 1
        assert not rows(service, 'SELECT * FROM android_order_native_inputs')
        assert not supervision.handles and not service.app.state.devices.locks['home'].locked()
        record_property('dk90_control', {'slack_s_at_read': slack, 'wake': wakes(service)[0]['hold_reason']})


# spec: ANYM-993, ANYM-997
def test_a_budget_read_after_the_due_wake_was_admitted_finds_it_already_pruned(tmp_path, monkeypatch, gated,
                                                                              record_property):
    """The CI state: the timer wake was admitted while the budget still had time, its native turn received the
    obligation, the handle lost its refs and the same tick pruned the budget. A read now raises KeyError."""
    gate, ticks = gated
    config, record = host(tmp_path, monkeypatch, delay=.3)
    with Service(config) as service:
        request = service.start(service.lease(), goal='native-control')
        service.finished(request['request_id'])
        supervision, key = service.app.state.agents.supervision, ('one', request['request_id'])
        assert key in supervision.budgets and key in supervision.handles  # what a prompt read would have found
        gate.set()  # a late test thread: ticks run before it reads
        until(lambda: rows(service, 'SELECT * FROM android_order_native_inputs'))
        until(lambda: key not in supervision.budgets)
        with pytest.raises(KeyError):
            supervision.budgets[key]  # line 566's read, at this point
        obligation, = rows(service, 'SELECT lifecycle FROM android_order_obligations')
        wake, = wakes(service)
        state = {'budget': key in supervision.budgets, 'handles': len(supervision.handles), 'ticks': len(ticks) > 0,
                 'native_inputs': len(rows(service, 'SELECT * FROM android_order_native_inputs')),
                 'obligation': obligation['lifecycle'], 'wake': (wake['state'], wake['hold_reason']),
                 'turns': turns(record)}
        record_property('dk90_late_read', state)
        assert state == {'budget': False, 'handles': 0, 'ticks': True, 'native_inputs': 1, 'obligation': 'received',
                         'wake': ('accepted', None), 'turns': 2}, state
