"""Existing-lease supervision through synthetic native stdio and the real HTTP lifecycle."""

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from agent_harness import Service
from test_native_dialogue import messages
from test_native_order_control import controlled


def host(tmp_path, monkeypatch, *, delay=.2, lose=False, repeat=False, stall=False):
    from deedkit_android import order_supervision
    monkeypatch.setattr(order_supervision, 'TICK_S', .02)
    session, record = controlled(tmp_path, operation='bind')
    binary = Path(session.config.binary)
    source = binary.read_text().replace('import json, os, sys', 'import json, os, sys, time')
    source = source.replace("payload=dict(message='Owner update')", f"payload=dict(next_check_at=time.time()+{delay!r},state=dict(pending=True))")
    source = source.replace("if mode == 'LOSE_ANSWER_AFTER_WRITE': raise SystemExit",
        f"if mode == 'LOSE_ANSWER_AFTER_WRITE' or ({lose!r} and mode.startswith('Order supervision wake ')): raise SystemExit")
    old = "elif mode.startswith('  Home,'): finish()"
    assert old in source
    source = source.replace(old, '''elif mode.startswith('Order supervision wake '):
            wake=json.loads(mode.split('\\n',1)[1])
            event('item/started',turnId=turn,item=dict(id='timer-control',type='dynamicToolCall',tool='android_order_control',namespace=None))
            emit(id=43,method='item/tool/call',params=dict(threadId=thread,turnId=turn,callId='timer-control',namespace=None,
                 tool='android_order_control',arguments=dict(operation='update',payload=dict(obligation_ref=wake['obligation_ref'],changes=dict(lifecycle='received')))))
        ''' + old)
    if repeat:
        source = source.replace("operation='update',payload=dict(obligation_ref=wake['obligation_ref'],changes=dict(lifecycle='received'))",
            "operation='next_check',payload=dict(obligation_ref=wake['obligation_ref'],at=time.time()+.12,checked_wake_id=wake['wake_id'])")
    if stall:
        source = source.replace("wake=json.loads(mode.split('\\n',1)[1])",
            "event('item/started',turnId=turn,item=dict(id='pending-observation',type='commandExecution',command='synthetic observation')); continue")
    binary.write_text(source)
    config = tmp_path / 'config.json'
    body = json.loads(config.read_text())
    body['agent'].update(guarded=False, persistent=True, order_events={'source_id': 'supervision-source', 'auth_epoch': 1})
    config.write_text(json.dumps(body))
    return config, record


def until(predicate):
    deadline = time.monotonic() + 5
    while not (result := predicate()):
        assert time.monotonic() < deadline, 'bounded synthetic supervision did not finish'
        time.sleep(.01)
    return result


def wakes(service):
    return rows(service, "SELECT * FROM android_order_wakes WHERE kind='observation'")


def rows(service, sql, params=()):
    path = service.app.state.store.directory / 'state.sqlite3'
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(sql, params)]


def native_receipt(service, wake):
    return json.loads(rows(service, 'SELECT receipt FROM android_order_native_inputs WHERE wake_id=?', (wake,))[0]['receipt'])


def pause_selection(monkeypatch):
    from deedkit_android.codex_session import CodexSession
    original = CodexSession.select
    entered, release, once, loops = threading.Event(), threading.Event(), [True], []

    async def select(session, config):
        result = await original(session, config)
        if once[0]:
            once[0] = False
            loops.append(asyncio.get_running_loop())
            entered.set()
            while not release.is_set():
                await asyncio.sleep(.002)
        return result

    monkeypatch.setattr(CodexSession, 'select', select)
    return entered, release, loops


def stop_ticker(service, loop):
    async def stop():
        task = service.app.state.agents.supervision.task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run_coroutine_threadsafe(stop(), loop).result(3)


def pause_final(monkeypatch):
    from deedkit_android.agent_bare import BareRun
    original = BareRun.final
    entered, release, runs = threading.Event(), threading.Event(), []

    async def final(run, state, extra):
        result = await original(run, state, extra)
        if run.request.goal.startswith('  Home, gate-'):
            runs.append(run)
            entered.set()
            while not release.is_set():
                await asyncio.sleep(.002)
        return result

    monkeypatch.setattr(BareRun, 'final', final)
    return entered, release, runs


# spec: ANYM-993, ANYM-997
def test_foreground_completion_retains_same_lease_then_due_native_input_fires_once_without_idle_lock(tmp_path, monkeypatch):
    config, record = host(tmp_path, monkeypatch, delay=.35)
    with Service(config) as service:
        token = service.lease()
        request = service.start(token, goal='native-control', owner_instruction='  Follow this order until received.  ')
        first = service.finished(request['request_id'])
        agents = service.app.state.agents
        assert not service.app.state.devices.locks['home'].locked()
        assert agents.supervision.status('one', request['request_id'])['state'] == 'retained'
        until(lambda: wakes(service) and wakes(service)[0]['state'] == 'accepted')
        final = service.finished(request['request_id'], timeout=5)
        assert final['state'] == 'succeeded', final
        seen = messages(record)
        starts = [r for r in seen if r['message'].get('method') == 'turn/start']
        assert len(starts) == 2 and len({r['pid'] for r in seen}) == 1
        assert len([r for r in seen if r['message'].get('method') == 'thread/start']) == 1
        assert final['native_session']['thread_id'] == first['native_session']['thread_id']
        assert token not in record.read_text()
        reservation, = rows(service, 'SELECT * FROM reservations WHERE device=?', ('home',))
        assert reservation['token'] == token
        receipt = native_receipt(service, wakes(service)[0]['wake_id'])
        assert receipt['status'] == 'accepted' and receipt['original_request_id'] == request['request_id']
        assert receipt['native_turn_id'] == first['native_session']['turn_id']
        assert receipt['continuation_turn_id'] != receipt['native_turn_id']
        until(lambda: agents.supervision.status('one', request['request_id'])['state'] == 'unavailable')
        assert not service.app.state.devices.locks['home'].locked()


# spec: ANYM-993, ANYM-997
def test_idle_stop_removes_supervision_without_releasing_other_active_obligations(tmp_path, monkeypatch):
    config, record = host(tmp_path, monkeypatch, delay=3)
    with Service(config) as service:
        token = service.lease()
        first = service.start(token, goal='native-control')
        service.finished(first['request_id'])
        second = service.start(token, goal='native-control')
        service.finished(second['request_id'])
        agents = service.app.state.agents
        assert len(agents.supervision.handles) == 2
        service.post('agent', {'action': 'stop', 'request_id': first['request_id']})
        assert agents.supervision.status('one', first['request_id'])['state'] == 'unavailable'
        assert agents.supervision.status('one', second['request_id'])['state'] == 'retained'
        assert len(agents.supervision.handles) == 1
        assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 2


# spec: ANYM-993, ANYM-997
def test_lost_native_start_receipt_remains_unknown_across_ticks_and_restart_without_replay(tmp_path, monkeypatch):
    config, record = host(tmp_path, monkeypatch, lose=True)
    with Service(config) as service:
        token = service.lease()
        original = service.start(token, goal='native-control')
        service.finished(original['request_id'])
        until(lambda: wakes(service) and wakes(service)[0]['state'] == 'unknown')
        wake = wakes(service)[0]['wake_id']
        assert native_receipt(service, wake)['status'] == 'unknown'
        assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 2
    with Service(config) as service:
        assert not service.app.state.agents.supervision.handles
        assert native_receipt(service, wake)['status'] == 'unknown'
        assert wakes(service)[0]['state'] == 'unknown'
        assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 2


# spec: ANYM-993, ANYM-997
@pytest.mark.parametrize('change', ['release', 'expiry', 'control', 'source', 'device', 'replace_token'])
def test_authority_change_after_native_preflight_refuses_before_touch_or_input_and_keeps_process(tmp_path, monkeypatch, change):
    from deedkit_android.store import Store
    config, record = host(tmp_path, monkeypatch, delay=.35)
    with Service(config) as service:
        token = service.lease()
        original = service.start(token, goal='native-control')
        first = service.finished(original['request_id'])
        entered, release, loops = pause_selection(monkeypatch)
        assert entered.wait(3)
        stop_ticker(service, loops[0])  # isolate the post-await admission check from metadata-tick cancellation
        touches, touch = [], Store.touch

        def counted(store, *args, **kwargs):
            touches.append(1)
            return touch(store, *args, **kwargs)

        monkeypatch.setattr(Store, 'touch', counted)
        try:
            if change in ('release', 'replace_token'):
                service.post('session', {'action': 'release', 'device_id': 'home', 'session_id': token})
                if change == 'replace_token':
                    assert service.lease() != token
            elif change == 'expiry':
                with sqlite3.connect(tmp_path / 'state/state.sqlite3') as db:
                    db.execute('UPDATE reservations SET expires=0 WHERE device=?', ('home',))
            else:
                body = json.loads(config.read_text())
                if change == 'control':
                    body['principals'][0]['permissions'] = ['android:read']
                elif change == 'source':
                    body['agent']['order_events']['auth_epoch'] = 2
                else:
                    body['devices']['home'] = 'FOREIGN-SERIAL'
                config.write_text(json.dumps(body))
            release.set()
            until(lambda: not service.app.state.agents.supervision.active)
            assert not touches
            assert not rows(service, 'SELECT * FROM android_order_native_inputs')
            assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 1
            native = service.app.state.agents.sessions[('one', 'home')]
            assert native.process.pid == first['native_session']['pid'] and native.process.returncode is None
            assert not service.app.state.devices.locks['home'].locked()
        finally:
            release.set()


# spec: ANYM-993, ANYM-997
def test_foreground_demand_cancels_unsubmitted_timer_preflight_and_wins_same_process(tmp_path, monkeypatch):
    config, record = host(tmp_path, monkeypatch, delay=.35)
    with Service(config) as service:
        token = service.lease()
        request = service.start(token, goal='native-control')
        first = service.finished(request['request_id'])
        entered, release, _ = pause_selection(monkeypatch)
        assert entered.wait(3)
        try:
            foreground = service.finished(service.start(token, goal='  Home, ordinary new goal')['request_id'])
            assert foreground['state'] == 'succeeded'
            assert foreground['native_session']['pid'] == first['native_session']['pid']
            starts = [r for r in messages(record) if r['message'].get('method') == 'turn/start']
            assert len(starts) >= 2
            assert starts[1]['message']['params']['input'][0]['text'].find('ordinary new goal') >= 0
            until(lambda: wakes(service)[0]['state'] == 'accepted')
            service.finished(request['request_id'])
            starts = [r for r in messages(record) if r['message'].get('method') == 'turn/start']
            assert len(starts) == 3 and starts[-1]['message']['params']['input'][0]['text'].startswith('Order supervision wake ')
            assert not service.app.state.devices.locks['home'].locked()
        finally:
            release.set()


# spec: ANYM-993, ANYM-997
def test_idle_ticks_keep_fixed_deadline_and_cap_the_existing_reservation_expiry(tmp_path, monkeypatch):
    config, record = host(tmp_path, monkeypatch, delay=100)
    with Service(config) as service:
        token = service.post('session', {'action': 'acquire', 'device_id': 'home', 'seconds': 30})['session_id']
        request = service.start(token, goal='native-control', max_minutes=1)
        service.finished(request['request_id'])
        supervision = service.app.state.agents.supervision
        handle = supervision.handles[('one', request['request_id'])]
        interval, budget = handle.until, handle.time_limit
        expected_expiry = time.time() + interval - supervision.clock()
        until(lambda: supervision.status('one', request['request_id'])['reason'] == 'next_check_beyond_authority')
        assert handle.window == 30 and handle.until == interval and handle.time_limit == budget
        reservation, = rows(service, 'SELECT expires FROM reservations WHERE device=?', ('home',))
        assert reservation['expires'] <= expected_expiry + .05
        supervision.clock = lambda: interval + 1
        until(lambda: not supervision.handles)
        assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 1


# spec: ANYM-993, ANYM-997
def test_timer_checks_spend_original_cumulative_command_budget_without_renewing_task_deadline(tmp_path, monkeypatch):
    config, record = host(tmp_path, monkeypatch, delay=.25, repeat=True)
    with Service(config) as service:
        token = service.lease()
        request = service.start(token, goal='native-control', max_actions=10, max_minutes=1)
        service.finished(request['request_id'])
        supervision = service.app.state.agents.supervision
        handle = supervision.handles[('one', request['request_id'])]
        deadline = handle.time_limit
        until(lambda: len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 10)
        until(lambda: not supervision.handles and not supervision.active)
        assert handle.commands == 10 and handle.time_limit == deadline
        assert len([r for r in messages(record) if r['message'].get('method') == 'thread/start']) == 1
        assert len(rows(service, 'SELECT * FROM android_order_native_inputs')) == 9
        until(lambda: wakes(service)[-1]['hold_reason'] == 'supervision_budget_unavailable')
        assert not service.app.state.devices.locks['home'].locked()


# spec: ANYM-993, ANYM-997
def test_restart_never_adopts_still_live_persisted_lease_or_pending_timer(tmp_path, monkeypatch):
    config, record = host(tmp_path, monkeypatch, delay=.7)
    with Service(config) as service:
        token = service.lease()
        request = service.start(token, goal='native-control')
        service.finished(request['request_id'])
        assert service.app.state.agents.supervision.handles
    with Service(config) as service:
        until(lambda: wakes(service) and wakes(service)[0]['hold_reason'] == 'lease_unavailable')
        reservation, = rows(service, 'SELECT token,expires FROM reservations WHERE device=?', ('home',))
        assert reservation['token'] == token and reservation['expires'] > time.time()
        assert not service.app.state.agents.supervision.handles
        assert not service.app.state.agents.sessions
        assert not rows(service, 'SELECT * FROM android_order_native_inputs')
        assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 1


# spec: ANYM-993, ANYM-997
def test_owner_stop_cancels_admitted_check_before_unlock_without_reoffering_accepted_input(tmp_path, monkeypatch):
    config, record = host(tmp_path, monkeypatch, stall=True)
    with Service(config) as service:
        token = service.lease()
        request = service.start(token, goal='native-control')
        first = service.finished(request['request_id'])
        until(lambda: wakes(service) and wakes(service)[0]['state'] == 'accepted')
        stopped = service.post('agent', {'action': 'stop', 'request_id': request['request_id']})
        assert stopped['state'] == 'stopped'
        assert stopped['native_supervision']['state'] == 'unavailable'
        assert stopped['native_timing']['stop_source'] == 'owner_stop'
        assert wakes(service)[0]['state'] == 'accepted'
        assert not service.app.state.devices.locks['home'].locked()
        assert service.app.state.agents.sessions[('one', 'home')].process.pid == first['native_session']['pid']
        assert any(r['message'].get('method') == 'turn/interrupt' for r in messages(record))
        again = service.start(token, request_id=request['request_id'], goal='native-control')
        assert again['state'] == 'stopped'
        assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 2


# spec: ANYM-993, ANYM-997
@pytest.mark.parametrize('field,value', [('owner', 'two'), ('device_id', 'foreign'), ('original_request_id', 'other-task'), ('native_thread_id', 'other-thread')])
def test_changed_wake_binding_after_selection_never_routes_to_another_principal_device_task_or_thread(tmp_path, monkeypatch, field, value):
    config, record = host(tmp_path, monkeypatch, delay=.35)
    with Service(config) as service:
        token = service.lease()
        original = service.start(token, goal='native-control')
        service.finished(original['request_id'])
        entered, release, loops = pause_selection(monkeypatch)
        assert entered.wait(3)
        stop_ticker(service, loops[0])
        try:
            wake = wakes(service)[0]['wake_id']
            with sqlite3.connect(tmp_path / 'state/state.sqlite3') as db:
                db.execute(f'UPDATE android_order_wakes SET {field}=? WHERE wake_id=?', (value, wake))
            release.set()
            until(lambda: not service.app.state.agents.supervision.active)
            assert not rows(service, 'SELECT * FROM android_order_native_inputs')
            assert wakes(service)[0]['state'] == 'pending'
            assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 1
            assert not service.app.state.devices.locks['home'].locked()
        finally:
            release.set()


# spec: ANYM-993, ANYM-997
def test_observations_coalesced_during_native_preflight_are_frozen_and_delivered_losslessly(tmp_path, monkeypatch):
    from deedkit_android.order_events import Notification
    config, record = host(tmp_path, monkeypatch, delay=.35)
    # A real app source is optional until the native agent binds one; use a package for this observation fixture.
    body = json.loads(config.read_text())
    binary = Path(body['engines']['codex']['binary'])
    binary.write_text(binary.read_text().replace('state=dict(pending=True)', "state=dict(pending=True),package='app.synthetic'"))
    with Service(config) as service:
        token = service.lease()
        request = service.start(token, goal='native-control', owner_instruction='  Exact original words.\nNo replacement.  ')
        service.finished(request['request_id'])
        entered, release, loops = pause_selection(monkeypatch)
        assert entered.wait(3)
        stop_ticker(service, loops[0])
        try:
            async def observe():
                control = service.app.state.agents.order_control()
                return control.events.observe('one', Notification(event_id='late-observation', owner='one', device_id='home',
                    order_id=None, source_order_id=None, package='app.synthetic', source_id='supervision-source',
                    auth_epoch=1, observed_at=time.time(), observation_ref='opaque-untrusted-reference'))
            assert asyncio.run_coroutine_threadsafe(observe(), loops[0]).result(3) == 'recorded'
            release.set()
            until(lambda: not service.app.state.agents.supervision.active)
            starts = [r for r in messages(record) if r['message'].get('method') == 'turn/start']
            assert len(starts) == 2
            submitted = json.loads(starts[-1]['message']['params']['input'][0]['text'].split('\n', 1)[1])
            assert submitted['original_request']['owner_instruction'] == '  Exact original words.\nNo replacement.  '
            assert submitted['observation']['through'] == 1
            assert submitted['observation']['observations'][0]['event_id'] == 'late-observation'
            assert submitted['observation']['observations'][0]['observation_ref'] == 'opaque-untrusted-reference'
            wake = wakes(service)[0]
            assert json.loads(wake['payload']) == submitted['observation']
            assert native_receipt(service, wake['wake_id'])['status'] == 'accepted'
        finally:
            release.set()


# spec: ANYM-993, ANYM-997
def test_pending_async_owner_question_is_not_expired_or_bypassed_by_a_due_timer(tmp_path, monkeypatch):
    config, record = host(tmp_path, monkeypatch, delay=.2)
    body = json.loads(config.read_text())
    binary = Path(body['engines']['codex']['binary'])
    binary.write_text(binary.read_text().replace('def finish():', '''def finish():
    event('item/completed',turnId=turn,item=dict(id='owner-question',type='agentMessage',delivery='async',phase='commentary',
          text='Which address?',questions=[dict(title='Which address?',options=None)]))'''))
    with Service(config) as service:
        token = service.lease()
        request = service.start(token, goal='native-control')
        first = service.finished(request['request_id'])
        until(lambda: wakes(service) and wakes(service)[0]['hold_reason'] == 'supervision_question_pending')
        later = service.get(request['request_id'])
        question = next(r for r in later['dialogue'] if r.get('item_id') == 'owner-question')
        assert question['native_state'] == 'waiting' and 'native_request_id' not in question
        assert later['native_session'] == first['native_session']
        assert not rows(service, 'SELECT * FROM android_order_native_inputs')
        assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 1


# spec: ANYM-993, ANYM-997
def test_revoked_active_check_is_interrupted_with_original_accepted_receipt_retained(tmp_path, monkeypatch):
    config, record = host(tmp_path, monkeypatch, stall=True)
    with Service(config) as service:
        token = service.lease()
        request = service.start(token, goal='native-control')
        service.finished(request['request_id'])
        until(lambda: wakes(service) and wakes(service)[0]['state'] == 'accepted')
        body = json.loads(config.read_text())
        body['principals'][0]['permissions'] = ['android:read']
        config.write_text(json.dumps(body))
        until(lambda: not service.app.state.agents.supervision.active)
        result = json.loads(rows(service, 'SELECT result FROM agent_runs WHERE id=?', (request['request_id'],))[0]['result'])
        assert result['native_timing']['stop_source'] == 'supervision_revoked'
        assert wakes(service)[0]['state'] == 'accepted'
        assert native_receipt(service, wakes(service)[0]['wake_id'])['status'] == 'accepted'
        assert not service.app.state.agents.supervision.handles
        assert not service.app.state.devices.locks['home'].locked()
        assert any(r['message'].get('method') == 'turn/interrupt' for r in messages(record))
        assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 2


# spec: ANYM-993, ANYM-997
def test_successful_tracked_new_goal_advances_old_order_head_without_changing_its_context_or_budget(tmp_path, monkeypatch):
    config, record = host(tmp_path, monkeypatch, delay=.6)
    with Service(config) as service:
        token = service.lease()
        original = service.start(token, goal='native-control', owner_instruction='  Original A order only.\nRetain this wording.  ',
            params={'COUNT': 2, 'CONFIRMED': False, 'LABEL': '  exact  '}, max_minutes=1)
        first = service.finished(original['request_id'])
        supervision = service.app.state.agents.supervision
        handle = supervision.handles[('one', original['request_id'])]
        before = (handle.time_limit, handle.until, handle.commands, handle.request, handle.thread)
        active_spent = supervision.budgets[('one', original['request_id'])]['seconds_used']
        second_request = service.start(token, goal='  Home, new B task', owner_instruction='B has different words.', effort='high')
        second = service.finished(second_request['request_id'])
        assert second['state'] == 'succeeded'
        assert (handle.time_limit, handle.until, handle.commands, handle.request, handle.thread) == before
        assert supervision.budgets[('one', original['request_id'])]['seconds_used'] == active_spent
        assert handle.turn == second['native_session']['turn_id']
        audit, = rows(service, 'SELECT * FROM android_order_native_heads WHERE original_request_id=?', (original['request_id'],))
        assert audit['from_turn_id'] == first['native_session']['turn_id']
        assert audit['foreground_request_id'] == second_request['request_id']
        assert audit['foreground_turn_id'] == handle.turn
        until(lambda: wakes(service) and wakes(service)[0]['state'] == 'accepted')
        final = service.finished(original['request_id'])
        assert final['state'] == 'succeeded'
        starts = [r for r in messages(record) if r['message'].get('method') == 'turn/start']
        assert len(starts) == 3 and len({r['pid'] for r in messages(record)}) == 1
        resumed = json.loads(starts[-1]['message']['params']['input'][0]['text'].split('\n', 1)[1])
        assert resumed['original_request_id'] == original['request_id']
        assert resumed['original_request']['owner_instruction'] == '  Original A order only.\nRetain this wording.  '
        assert resumed['original_request']['goal'] == 'native-control'
        assert resumed['original_request']['params'] == {'COUNT': 2, 'CONFIRMED': False, 'LABEL': '  exact  '}
        assert starts[-1]['message']['params']['effort'] == 'medium'
        assert native_receipt(service, wakes(service)[0]['wake_id'])['native_turn_id'] == first['native_session']['turn_id']


# spec: ANYM-993, ANYM-997
def test_forty_five_minutes_delivery_idle_uses_only_original_cumulative_native_work_budget(tmp_path, monkeypatch):
    from deedkit_android import store as store_module
    config, record = host(tmp_path, monkeypatch, delay=600, repeat=True)
    binary = Path(json.loads(config.read_text())['engines']['codex']['binary'])
    source = binary.read_text()
    old = "arguments=dict(operation='next_check',payload=dict(obligation_ref=wake['obligation_ref'],at=time.time()+.12,checked_wake_id=wake['wake_id']))"
    assert old in source
    source = source.replace(old, "arguments=(dict(operation='next_check',payload=dict(obligation_ref=wake['obligation_ref'],at=wake['observation']['next_check_at']+(300 if count==5 else 600),checked_wake_id=wake['wake_id'])) if count<6 else dict(operation='update',payload=dict(obligation_ref=wake['obligation_ref'],changes=dict(lifecycle='received'))))")
    source = source.replace("'never' is not None", 'True')  # constant fixture policy; avoid compiling its literal identity warning
    compile(source, str(binary), 'exec')
    binary.write_text(source)
    with Service(config) as service:
        token = service.lease()
        request = service.start(token, goal='native-control', max_minutes=5)
        service.finished(request['request_id'])
        supervision = service.app.state.agents.supervision
        stop_ticker(service, supervision.task.get_loop())
        extra = [0]
        real_wall, real_mono = time.time, time.monotonic
        def wall():
            return real_wall() + extra[0]
        supervision.wall, supervision.clock = wall, lambda: real_mono() + extra[0]
        service.app.state.agents.orders.events.clock = wall
        monkeypatch.setattr(store_module, 'time', SimpleNamespace(time=wall))
        handle = supervision.handles[('one', request['request_id'])]
        budget = supervision.budgets[('one', request['request_id'])]
        for index, delivery_seconds in enumerate((600, 1200, 1800, 2400, 2700), start=1):
            spent, interval = budget['seconds_used'], handle.until
            extra[0] = delivery_seconds
            assert budget['seconds_used'] == spent and handle.until == interval  # delivery idle itself spends/refills nothing
            asyncio.run_coroutine_threadsafe(supervision.tick(), supervision.task.get_loop()).result(3)
            until(lambda: len(rows(service, 'SELECT * FROM android_order_native_inputs')) == index)
            assert service.finished(request['request_id'])['state'] == 'succeeded'
            until(lambda: not supervision.active)
            assert spent < budget['seconds_used'] < 300 and budget['seconds_limit'] == 300
            reservation, = rows(service, 'SELECT token FROM reservations WHERE device=?', ('home',))
            assert reservation['token'] == token
            if index < 5:
                assert handle.until > interval  # this task's genuine successful check alone establishes another fixed interval
        assert extra[0] == 45 * 60 and budget['used'] == 6
        assert not supervision.handles
        assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 6
        assert len([r for r in messages(record) if r['message'].get('method') == 'thread/start']) == 1
        assert len({r['pid'] for r in messages(record)}) == 1
        assert token not in record.read_text()


# spec: ANYM-993, ANYM-997
def test_exhausted_active_budget_refuses_due_input_before_any_native_preflight(tmp_path, monkeypatch):
    config, record = host(tmp_path, monkeypatch, delay=.3)
    with Service(config) as service:
        request = service.start(service.lease(), goal='native-control')
        service.finished(request['request_id'])
        supervision = service.app.state.agents.supervision
        budget = supervision.budgets[('one', request['request_id'])]
        budget['seconds_used'] = budget['seconds_limit']
        before = record.read_text()
        until(lambda: wakes(service) and wakes(service)[0]['hold_reason'] == 'lease_unavailable')
        assert record.read_text() == before
        assert not rows(service, 'SELECT * FROM android_order_native_inputs')
        assert not supervision.handles and not service.app.state.devices.locks['home'].locked()


# spec: ANYM-993, ANYM-997
def test_blocked_native_question_wait_is_charged_as_occupied_original_task_work(tmp_path):
    from test_native_dialogue import native
    native(tmp_path)
    config = tmp_path / 'config.json'
    body = json.loads(config.read_text())
    body['agent'].update(guarded=False, persistent=True)
    body['agent']['order_events'] = {'source_id': 'blocked-question-source', 'auth_epoch': 1}
    config.write_text(json.dumps(body))
    with Service(config) as service:
        token = service.lease()
        request = service.start(token, goal='question')
        def question():
            return next((e for e in service.get(request['request_id'])['events'] if e['kind'] == 'question'), None)
        event = until(question)
        time.sleep(.06)
        running = service.get(request['request_id'])
        assert running['native_supervision']['active_spent_ms'] >= 60
        service.post('agent', dict(action='reply', device_id='home', session_id=token, request_id=request['request_id'],
            native_thread_id=event['native_thread_id'], native_turn_id=event['native_turn_id'], native_request_id=event['native_request_id'],
            item_id=event['item_id'], answers={'address': ['Home'], 'choice': ['Shop']}))
        result = service.finished(request['request_id'])
        assert result['state'] == 'succeeded'
        assert result['native_supervision']['active_spent_ms'] >= running['native_supervision']['active_spent_ms']


# spec: ANYM-993, ANYM-997
@pytest.mark.parametrize('change', ['previous_head', 'token', 'session', 'process', 'thread', 'boundary', 'scope', 'rotated_original_key'])
def test_tracked_head_refuses_changed_binding_or_current_and_original_rights_after_await(tmp_path, monkeypatch, change):
    config, record = host(tmp_path, monkeypatch, delay=3)
    with Service(config) as service:
        token = service.lease()
        request = service.start(token, goal='native-control')
        service.finished(request['request_id'])
        supervision = service.app.state.agents.supervision
        stop_ticker(service, supervision.task.get_loop())
        handle = supervision.handles[('one', request['request_id'])]
        previous, interval = handle.turn, handle.until
        if change == 'rotated_original_key':
            body = json.loads(config.read_text())
            body['principals'][0]['key_sha256'] = hashlib.sha256(b'one-new').hexdigest()
            config.write_text(json.dumps(body))
        entered, release, runs = pause_final(monkeypatch)
        second = service.start(token, key='one-new' if change == 'rotated_original_key' else 'one', goal='  Home, gate-B')
        assert entered.wait(3)
        try:
            run = runs[0]
            assert run.native_completed_turn == run.native_admitted_turn
            if change == 'previous_head':
                run.native_previous_turn = 'untracked-head'
            elif change == 'token':
                run.request = run.request.model_copy(update={'session_id': 'unrelated-token'})
            elif change == 'session':
                handle.session = object()
            elif change == 'process':
                handle.process = object()
            elif change == 'thread':
                handle.thread = 'unrelated-thread'
            elif change == 'boundary':
                with sqlite3.connect(tmp_path / 'state/state.sqlite3') as db:
                    db.execute('UPDATE agent_native_bindings SET boundary=? WHERE owner=?', ('unverified-boundary', 'one'))
            elif change == 'scope':
                body = json.loads(config.read_text())
                body['principals'][0]['permissions'] = ['android:read']
                config.write_text(json.dumps(body))
            release.set()
            until(lambda: rows(service, 'SELECT state FROM agent_runs WHERE id=?', (second['request_id'],))[0]['state'] != 'running')
            assert handle.turn == previous and handle.until == interval
            assert not rows(service, 'SELECT * FROM android_order_native_heads')
            assert not rows(service, 'SELECT * FROM android_order_native_inputs')
            assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 2
        finally:
            release.set()


# spec: ANYM-993, ANYM-997
def test_pending_question_survives_tracked_new_task_and_still_blocks_old_timer(tmp_path, monkeypatch):
    config, record = host(tmp_path, monkeypatch, delay=.35)
    binary = Path(json.loads(config.read_text())['engines']['codex']['binary'])
    binary.write_text(binary.read_text().replace('def finish():', '''def finish():
    if mode == 'native-control':
        event('item/completed',turnId=turn,item=dict(id='keep-original-question',type='agentMessage',delivery='async',phase='commentary',
              text='Which address?',questions=[dict(title='Which address?',options=None)]))'''))
    with Service(config) as service:
        token = service.lease()
        request = service.start(token, goal='native-control')
        service.finished(request['request_id'])
        foreground = service.finished(service.start(token, goal='  Home, another tracked task')['request_id'])
        assert foreground['state'] == 'succeeded'
        until(lambda: wakes(service) and wakes(service)[0]['hold_reason'] == 'supervision_question_pending')
        view = service.get(request['request_id'])
        question = next(r for r in view['dialogue'] if r.get('item_id') == 'keep-original-question')
        assert question['native_state'] == 'waiting' and question['submission_intent'] is False
        assert len(rows(service, 'SELECT * FROM android_order_native_heads')) == 1
        assert not rows(service, 'SELECT * FROM android_order_native_inputs')
        assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 2


# spec: ANYM-993, ANYM-997
def test_live_native_command_at_terminal_cannot_authorize_an_idle_interval_or_new_native_input(tmp_path, monkeypatch):
    config, record = host(tmp_path, monkeypatch, delay=.2)
    binary = Path(json.loads(config.read_text())['engines']['codex']['binary'])
    binary.write_text(binary.read_text().replace('def finish():', '''def finish():
    if mode == 'native-control':
        event('item/started',turnId=turn,item=dict(id='owned-pty',type='commandExecution',command='synthetic background',status='inProgress',processId='owned-process'))
        event('item/completed',turnId=turn,item=dict(id='owned-pty',type='commandExecution',status='inProgress',processId='owned-process',exitCode=None))'''))
    with Service(config) as service:
        request = service.start(service.lease(), goal='native-control')
        service.finished(request['request_id'])
        until(lambda: wakes(service) and wakes(service)[0]['hold_reason'] == 'supervision_native_work_unconfirmed')
        session = service.app.state.agents.sessions[('one', 'home')]
        assert (session.last_turn_id, 'owned-pty') in session.pending_commands
        assert not service.app.state.agents.supervision.handles
        assert not rows(service, 'SELECT * FROM android_order_native_inputs')
        assert len([r for r in messages(record) if r['message'].get('method') == 'turn/start']) == 1
