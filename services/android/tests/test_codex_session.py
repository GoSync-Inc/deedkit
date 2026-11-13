"""Persistent native transport through the real HTTP lifecycle; no provider or device calls."""

import json
import sys
import time

from agent_harness import Service, alive, write_config


def live_host(tmp_path, delay=False, crash=False):
    record = tmp_path / 'rpc.jsonl'
    binary = tmp_path / 'codex'
    binary.write_text(f'''#!{sys.executable}
import json, os, sys, threading, time
if sys.argv[1:] == ['--version']:
    print('0.166.0'); raise SystemExit
turns = 0
thread_id = 'native-' + str(os.getpid())
current = None
selected = dict(model='gpt-6.1-sol',reasoningEffort='medium',serviceTier='ultrafast')
def emit(**message):
    print(json.dumps(message), flush=True)
def event(method, **params):
    emit(method=method, params=dict(threadId=thread_id, **params))
for line in sys.stdin:
    message = json.loads(line)
    with open({str(record)!r}, 'a') as f:
        f.write(json.dumps(dict(pid=os.getpid(), argv=sys.argv, message=message))+'\\n')
    method, params = message.get('method'), message.get('params', {{}})
    if method == 'initialize':
        emit(id=message['id'], result={{}})
    elif method in ('thread/start', 'thread/resume'):
        thread_id = params.get('threadId', thread_id)
        emit(id=message['id'], result=dict(thread=dict(id=thread_id),approvalPolicy='never',cwd=os.getcwd(),
             sandbox=dict(type='workspaceWrite',writableRoots=[os.getcwd()],networkAccess=True,
                          excludeTmpdirEnvVar=True,excludeSlashTmp=True),**selected))
    elif method == 'model/list':
        emit(id=message['id'],result=dict(data=[dict(id=n,model=n,supportedReasoningEfforts=[dict(reasoningEffort=e) for e in ('minimal','low','medium','high')],serviceTiers=[dict(id='ultrafast')]) for n in ('gpt-6.1-sol','gpt-6.1-terra')],nextCursor=None))
    elif method == 'thread/settings/update':
        selected=dict(model=params['model'],reasoningEffort=params['effort'],serviceTier=None if params['serviceTier']=='default' else params['serviceTier'])
        emit(id=message['id'],result={{}})
    elif method == 'turn/start':
        turns += 1; current = 'turn-' + str(turns)
        if {crash!r}:
            raise SystemExit(1)
        emit(id=message['id'], result=dict(turn=dict(id=current)))
        event('turn/started', turn=dict(id=current, status='inProgress'))
        event('item/started', turnId=current, item=dict(id='cmd', type='commandExecution', command='adb get-state'))
        if {delay!r} and turns == 1:
            continue
        event('item/completed', turnId=current, item=dict(id='cmd', type='commandExecution', command='adb get-state', exitCode=0))
        event('item/completed', turnId=current, item=dict(id='answer', type='agentMessage', phase='final_answer', text='Remembered turn '+str(turns)))
        event('thread/tokenUsage/updated', turnId=current, tokenUsage=dict(last=dict(inputTokens=20, cachedInputTokens=5, outputTokens=3)))
        event('turn/completed', turn=dict(id=current, status='completed', error=None))
    elif method == 'turn/interrupt':
        emit(id=message['id'], result={{}})
        event('turn/completed', turn=dict(id=current, status='interrupted', error=None))
''')
    binary.chmod(0o755)
    config = write_config(tmp_path, codex=binary, default='codex')
    body = json.loads(config.read_text())
    body['agent'].update(guarded=False, persistent=True)
    body['engines']['codex']['version']='0.166.0'
    config.write_text(json.dumps(body))
    return config, record


def calls(record):
    return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []


# spec: ANYM-993
def test_same_live_process_thread_and_context_across_mcp_goals(tmp_path):
    config, record = live_host(tmp_path)
    with Service(config) as service:
        session = service.lease()
        start = service.start(session)
        first = service.finished(start['request_id'])
        second = service.finished(service.start(session, model='gpt-6.1-sol', effort='high')['request_id'])
        assert first['summary'] == 'Remembered turn 1'
        assert second['summary'] == 'Remembered turn 2'
        assert first['native_session']['thread_id'] == second['native_session']['thread_id']
        assert first['native_session']['pid'] == second['native_session']['pid']
        assert first['native_session']['turn_id'] == 'turn-1'
        assert second['native_session']['turn_id'] == 'turn-2'
        pid = first['native_session']['pid']
        assert alive(pid) and second['timings']['commands'] == 1
        assert second['tokens'] == dict(input=20, output=3, cache_read=5, cache_write=0)
        service.start(session, request_id=start['request_id'])
        seen = calls(record)
        assert len([r for r in seen if r['message']['method'] == 'turn/start']) == 2
        assert len([r for r in seen if r['message']['method'] == 'thread/start']) == 1
        argv = seen[0]['argv']
        assert 'app-server' in argv
        assert 'skills.bundled.enabled=false' in argv
        assert 'features.skip_host_skill_discovery=true' in argv
        assert first['skills_updated'] == second['skills_updated'] == []
    assert not alive(pid)


# spec: ANYM-993
def test_stop_interrupts_turn_releases_phone_and_reuses_healthy_process(tmp_path):
    config, record = live_host(tmp_path, delay=True)
    with Service(config) as service:
        session = service.lease()
        run = service.start(session)
        deadline = time.monotonic() + 5
        while not any(r['message']['method'] == 'turn/start' for r in calls(record)):
            assert time.monotonic() < deadline
            time.sleep(.02)
        stopped = service.post('agent', dict(action='stop', request_id=run['request_id']))
        assert stopped['state'] == 'stopped'
        result = service.finished(service.start(session)['request_id'])
        assert result['summary'] == 'Remembered turn 2'
        assert len({r['pid'] for r in calls(record)}) == 1
        assert any(r['message']['method'] == 'turn/interrupt' for r in calls(record))


# spec: ANYM-993
def test_native_transport_loss_fails_original_request_without_replay(tmp_path):
    config, record = live_host(tmp_path, crash=True)
    with Service(config) as service:
        session = service.lease()
        start = service.start(session)
        assert service.finished(start['request_id'])['state'] == 'failed'
        service.start(session, request_id=start['request_id'])
        assert len([r for r in calls(record) if r['message']['method'] == 'turn/start']) == 1


# spec: ANYM-993
def test_timeout_interrupts_native_turn_before_phone_is_released(tmp_path, monkeypatch):
    from deedkit_android import codex_session
    monkeypatch.setattr(codex_session, 'MINUTE_S', .3)
    config, record = live_host(tmp_path, delay=True)
    with Service(config) as service:
        session = service.lease()
        result = service.finished(service.start(session, max_minutes=1)['request_id'])
        assert result['state'] == 'timed_out'
        assert any(r['message']['method'] == 'turn/interrupt' for r in calls(record))
        monkeypatch.setattr(codex_session, 'MINUTE_S', 60)
        result = service.finished(service.start(session)['request_id'])
        assert result['state'] == 'succeeded' and result['summary'] == 'Remembered turn 2'
        assert len({r['pid'] for r in calls(record)}) == 1


# spec: ANYM-993
def test_context_is_scoped_to_owner_and_phone(tmp_path):
    config, record = live_host(tmp_path)
    with Service(config) as service:
        session = service.lease()
        first = service.finished(service.start(session)['request_id'])
        service.post('session', dict(action='release', device_id='home', session_id=session))
        second_session = service.lease(key='two')
        second = service.finished(service.start(second_session, key='two')['request_id'], key='two')
        assert first['native_session']['thread_id'] != second['native_session']['thread_id']
        assert first['native_session']['pid'] != second['native_session']['pid']
        assert second['summary'] == 'Remembered turn 1'


# spec: ANYM-993
def test_service_restart_resumes_exact_saved_thread_without_replaying_task(tmp_path):
    config, record = live_host(tmp_path)
    with Service(config) as service:
        session = service.lease()
        first = service.finished(service.start(session)['request_id'])
    with Service(config) as service:
        second = service.finished(service.start(session)['request_id'])
        assert first['native_session']['thread_id'] == second['native_session']['thread_id']
        assert first['native_session']['pid'] != second['native_session']['pid']
        assert any(r['message']['method'] == 'thread/resume' for r in calls(record))
        assert len([r for r in calls(record) if r['message']['method'] == 'turn/start']) == 2


# spec: ANYM-993
def test_orphan_marker_identifies_the_native_process_before_recovery(tmp_path):
    from deedkit_android import engines
    config, record = live_host(tmp_path)
    with Service(config) as service:
        session = service.lease()
        first = service.finished(service.start(session)['request_id'])
        marker, = (tmp_path / 'state' / 'agent' / 'sessions').glob('run-live-*/marker.json')
        assert engines.kill_by_marker(marker)  # PID alone is never authority to kill a reused process
        assert not alive(first['native_session']['pid'])
        second = service.finished(service.start(session)['request_id'])
        assert second['native_session']['thread_id'] == first['native_session']['thread_id']
