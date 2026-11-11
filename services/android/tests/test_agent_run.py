"""The local-agent run (ANYM-971): admission, the engine process, stops and limits, progress, the result, the preamble,
storage and the two engines. A fake engine binary and a fake phone; no model, no phone, no live provider."""

import json
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

import pytest

from agent_harness import (PACKAGE, SECRET, TOOLS, Service, alive, fake_engine, pass_probe, recorded, set_secret,
                           write_config)
from deedkit_android import agent_run, agent_tools, engine_probe, engines
from deedkit_android.agent_screen import compact
from deedkit_android.config import Configuration
from deedkit_android.store import Store

CANARY = 'canary-' + uuid.uuid4().hex
ENGINES = ['claude', 'codex']
FINISH = {'call': 'finish', 'args': {'summary': 'Done', 'data': {'total': '12.50'}}}


def host(tmp_path, steps, name='claude', **options):
    binary, record = fake_engine(tmp_path, name, steps, **options.pop('plan', {}))
    config = write_config(tmp_path, **{name: binary}, **{'default': name, **options})
    pass_probe(tmp_path, name)
    return config, record


def leaks(root, value=CANARY):
    return [str(path) for path in Path(root).rglob('*')
            if path.is_file() and path.name != 'secrets.json' and value.encode() in path.read_bytes()]


# --- criterion 1: the operation --------------------------------------------------------------------------------------


# spec: ANYM-971
def test_start_get_and_stop_follow_the_principals_and_the_run_is_its_owners(tmp_path):
    config, _ = host(tmp_path, [{'sleep': 3}, FINISH])
    with Service(config) as service:
        session = service.lease()
        service.start(session, key='reader', code=403)  # android:control is needed
        view = service.start(session)
        assert view['state'] == 'running' and view['engine'] == 'claude'
        service.post('agent', {'action': 'get', 'request_id': view['request_id']}, key='two', code=404)
        service.post('agent', {'action': 'stop', 'request_id': view['request_id']}, key='two', code=404)
        service.get(str(uuid.uuid4()), code=404)
        service.start(session, key='two', code=409)  # not two's lease
        service.post('agent', {'action': 'get', 'request_id': view['request_id'], 'goal': 'x'}, code=422)
        service.post('agent', {'action': 'start', 'request_id': str(uuid.uuid4())}, code=422)
        assert service.post('agent', {'action': 'stop', 'request_id': view['request_id']})['state'] == 'stopped'


# spec: ANYM-971
def test_the_same_request_id_returns_the_run_and_other_fields_conflict(tmp_path):
    config, record = host(tmp_path, [{'sleep': 3}, FINISH])
    with Service(config) as service:
        session = service.lease()
        request_id = str(uuid.uuid4())
        first = service.start(session, request_id, apps=[PACKAGE], params={'SHOP': 'Tea House'})
        again = service.start(session, request_id, apps=[PACKAGE], params={'SHOP': 'Tea House'})
        assert again['request_id'] == first['request_id'] == request_id and again['state'] == 'running'
        other = service.start(session, request_id, code=409, params={'SHOP': 'Other'})
        assert other['error'] == 'request_id_conflict'
        assert recorded(record)['argv']  # one engine, started once
        service.post('agent', {'action': 'stop', 'request_id': request_id})


# spec: ANYM-971
def test_one_run_per_device_holds_the_device_lock_for_its_whole_life(tmp_path):
    config, _ = host(tmp_path, [{'sleep': 5}, FINISH])
    with Service(config) as service:
        session = service.lease()
        first = service.start(session)
        assert service.start(session, code=409)['error'] == 'device_busy'
        assert service.post('session', {'action': 'release', 'device_id': 'home', 'session_id': session},
                            code=409)['error'] == 'device_busy'
        assert service.app.state.devices.locks['home'].locked()
        service.post('agent', {'action': 'stop', 'request_id': first['request_id']})
        assert not service.app.state.devices.locks['home'].locked()
        second = service.start(session)  # the lease is still the caller's
        assert second['state'] == 'running'
        service.post('agent', {'action': 'stop', 'request_id': second['request_id']})


ADMISSION = {
    'write denied app': ({'apps': ['com.whatsapp']}, 422, 'app_not_allowed'),
    'protected app': ({'apps': ['com.x8bit.bitwarden']}, 422, 'app_not_allowed'),
    'unknown secret': ({'secrets': ['NO_SUCH_SECRET']}, 422, 'secret_unknown'),
    'secret of another app': ({'apps': ['example.other'], 'secrets': [SECRET]}, 422, 'secret_not_in_apps'),
    'engine not configured': ({'engine': 'codex'}, 409, 'engine_unavailable'),
}
INVALID = {
    'six apps': {'apps': [f'example.app{n}' for n in range(6)]}, 'no apps': {'apps': []},
    'goal too long': {'goal': 'x' * 2001}, 'goal empty': {'goal': ''}, 'instruction short': {'owner_instruction': 'ab'},
    'instruction long': {'owner_instruction': 'x' * 1501}, 'minutes 16': {'max_minutes': 16}, 'minutes 0': {'max_minutes': 0},
    'actions 9': {'max_actions': 9}, 'actions 301': {'max_actions': 301},
    'twenty-one params': {'params': {f'P{n}': 'v' for n in range(21)}}, 'lower-case param': {'params': {'shop': 'v'}},
    'param value with a reference': {'params': {'SHOP': '$' + '{X}'}}, 'eleven outputs': {'outputs': [f'o{n}' for n in range(11)]},
    'bad output name': {'outputs': ['1total']}, 'six secrets': {'secrets': [f'S{n}' for n in range(6)]},
    'unknown engine': {'engine': 'gpt'}, 'unknown field': {'approve': True}, 'bad request id': {'request_id': 'not-a-uuid'},
}


# spec: ANYM-971
@pytest.mark.parametrize('name', list(ADMISSION))
def test_admission_refuses_what_the_run_may_not_do_and_starts_no_engine(tmp_path, name):
    fields, status, code = ADMISSION[name]
    config, record = host(tmp_path, [FINISH])
    set_secret(tmp_path, CANARY)
    with Service(config) as service:
        reply = service.start(service.lease(), code=status, **fields)
        assert reply['error'] == code
        if code == 'engine_unavailable':
            assert reply['hint'] == 'not_configured'
        assert not Path(record).exists() and not service.app.state.devices.locks['home'].locked()


# spec: ANYM-971
@pytest.mark.parametrize('name', list(INVALID))
def test_a_malformed_start_is_refused_before_anything_runs(tmp_path, name):
    config, record = host(tmp_path, [FINISH])
    with Service(config) as service:
        session = service.lease()
        body = {'action': 'start', 'device_id': 'home', 'session_id': session, 'request_id': str(uuid.uuid4()),
                'goal': 'Read the total', 'owner_instruction': 'For the owner', 'apps': [PACKAGE], **INVALID[name]}
        assert service.post('agent', body, code=422)['error'] == 'invalid_arguments' or name == 'bad request id'
        assert not Path(record).exists()


# --- criterion 10: the switch -----------------------------------------------------------------------------------------


# spec: ANYM-971
def test_a_disabled_agent_starts_nothing_and_reads_no_engine_login(tmp_path, monkeypatch):
    config, record = host(tmp_path, [FINISH], enabled=False)
    with Service(config) as service:
        monkeypatch.setattr(engines, 'private_file', lambda *a, **k: pytest.fail('a login file was read'))
        reply = service.start(service.lease(), code=409)
        assert reply['error'] == 'agent_disabled'
        capabilities = service.post('capabilities', {})
        assert capabilities['agent']['enabled'] is False and capabilities['agent']['reason'] == 'agent_disabled'
        assert 'start' in json.dumps(capabilities['agent']['input_schema']) and not Path(record).exists()


# --- criterion 2 and 9: the engine process and the two engines ---------------------------------------------------------


# spec: ANYM-971
@pytest.mark.parametrize('name', ENGINES)
def test_the_engine_gets_its_own_process_group_an_allowlisted_environment_and_a_safe_argv(tmp_path, name):
    config, record = host(tmp_path, [{'sleep': 0.2}, FINISH], name)
    set_secret(tmp_path, CANARY)
    with Service(config) as service:
        view = service.start(service.lease(), secrets=[SECRET], goal='Find the shop and read the total')
        seen = recorded(record)
        assert service.finished(view['request_id'])['state'] == 'succeeded'
    argv, env = seen['argv'], seen['env']
    state = tmp_path / 'state' / 'agent'
    login = {'claude': 'CLAUDE_CODE_OAUTH_TOKEN', 'codex': 'CODEX_HOME'}[name]
    # No other variable passes (the two extras are the fake's own interpreter's).
    assert set(env) - {'LC_CTYPE', '__CF_USER_TEXT_ENCODING'} == {'PATH', 'HOME', login}
    assert seen['pgid'] == seen['pid'] and seen['cwd'] == env['HOME'] and Path(env['HOME']).parent == state
    assert argv[0].endswith(name + '-' + argv[0].rsplit('-', 1)[1]) and not any(
        word in ' '.join(argv).lower() for word in ('--bare', 'dangerously', 'skip-permissions', 'bypass'))
    assert CANARY not in json.dumps(seen)  # the secret value never reaches the engine
    if name == 'claude':
        assert argv[1:4] == ['-p', '--output-format', 'stream-json'] and '--verbose' in argv
        assert argv[argv.index('--tools') + 1] == '' and '--strict-mcp-config' in argv and '--no-session-persistence' in argv
        assert argv[argv.index('--allowedTools') + 1] == 'mcp__phone__*' and argv[argv.index('--model') + 1] == 'sonnet'
        assert argv[argv.index('--setting-sources') + 1] == ''
        assert env['CLAUDE_CODE_OAUTH_TOKEN'] == (tmp_path / 'claude.token').read_text()
        assert 'Preamble version: 1' in argv[argv.index('--system-prompt') + 1]
        assert 'Find the shop and read the total' in seen['stdin'] and 'Goal' not in argv[-1]
    else:
        assert argv[1:5] == ['exec', '--json', '--ephemeral', '--ignore-user-config'] and argv[-1] == '-'
        assert {'-s', 'read-only', 'shell_tool', 'unified_exec', 'apps', 'plugins'} <= set(argv)
        assert 'mcp_servers.phone.default_tools_approval_mode="approve"' in argv
        assert env['CODEX_HOME'] == str(tmp_path / 'codex-home')
        assert 'Preamble version: 1' in seen['stdin'] and 'Find the shop and read the total' in seen['stdin']
    assert list(state.glob('run-*')) == []  # the run's private directory is removed


# spec: ANYM-971
def test_the_engine_is_chosen_per_host_and_per_run_and_an_unoffered_engine_says_why(tmp_path):
    claude, claude_record = fake_engine(tmp_path, 'claude', [FINISH])
    codex, codex_record = fake_engine(tmp_path, 'codex', [FINISH])
    config = write_config(tmp_path, claude=claude, codex=codex, default='codex')
    pass_probe(tmp_path, 'claude')  # codex has no probe record yet
    with Service(config) as service:
        found = service.post('capabilities', {})['agent']
        assert found['default_engine'] == 'codex' and found['enabled'] is True
        assert found['engines']['claude']['available'] is True
        assert found['engines']['codex'] == {'engine': 'codex', 'provider': 'OpenAI', 'available': False,
                                             'version': '9.9.9', 'model': None, 'reason': 'codex_probe_failed'}
        session = service.lease()
        refused = service.start(session, code=409)  # the default engine, codex, is not offered
        assert refused['error'] == 'engine_unavailable' and refused['hint'] == 'codex_probe_failed'
        view = service.start(session, engine='claude')
        assert service.finished(view['request_id'])['engine'] == 'claude' and recorded(claude_record)['argv']
        pass_probe(tmp_path, 'codex')
        assert service.post('capabilities', {})['agent']['engines']['codex']['available'] is True
        view = service.start(session)  # now the host default
        done = service.finished(view['request_id'])
        assert done['engine'] == 'codex' and recorded(codex_record)['argv'][1] == 'exec'


REASONS = {
    'not configured': (lambda tmp, cfg: None, 'not_configured'),
    'login missing': (lambda tmp, cfg: (tmp / 'claude.token').unlink(), 'login_missing'),
    'login open to others': (lambda tmp, cfg: (tmp / 'claude.token').chmod(0o644), 'login_missing'),
    'version changed': (lambda tmp, cfg: cfg.write_text(cfg.read_text().replace('"9.9.9"', '"1.0.0"')), 'version_mismatch'),
    'probe failed': (lambda tmp, cfg: engines.write_probe(tmp / 'state', 'claude', {'version': '9.9.9', 'passed': False}),
                     'probe_failed'),
    'probe of another version': (lambda tmp, cfg: engines.write_probe(tmp / 'state', 'claude',
                                                                      {'version': '9.9.8', 'passed': True}), 'probe_failed'),
}


# spec: ANYM-971
@pytest.mark.parametrize('name', list(REASONS))
def test_an_engine_is_offered_only_with_login_pinned_version_and_a_passed_probe(tmp_path, name):
    binary, _ = fake_engine(tmp_path, 'claude', [FINISH])
    config = write_config(tmp_path, claude=None if name == 'not configured' else binary)
    pass_probe(tmp_path, 'claude')
    mutate, reason = REASONS[name]
    mutate(tmp_path, config)
    shown = engines.status(tmp_path / 'state', 'claude', Configuration(config).read().engines.get('claude')
                           if name != 'version changed' else Configuration(config).read().engines['claude'])
    assert shown['available'] is False and shown['reason'] == reason


# spec: ANYM-971
@pytest.mark.parametrize('name', ENGINES)
def test_one_full_run_per_engine_hides_every_secret_and_reports_the_result(tmp_path, name):
    steps = [{'say': 'Looking at the screen. ' + 'x' * 400}, {'call': 'screen'},
             {'call': 'tap', 'args': {'selector': {'text': 'Add to cart'}}},
             {'call': 'screen', 'args': {'image': True}}, {'call': 'screen', 'args': {'image': True}},
             {'call': 'screen', 'args': {'image': True}}, {'call': 'screen', 'args': {'image': True}},
             {'call': 'type', 'args': {'text': '${secret:%s}' % SECRET}}, {'call': 'read_text', 'args': {'selector': {'text': 'x'}}},
             {'call': 'screen', 'args': {'image': True}},
             {'call': 'finish', 'args': {'summary': 'Added; basket total 12.50', 'data': {'total': '12.50', 'extra': 'ignored'}}},
             {'call': 'finish', 'args': {'summary': 'Added; basket total 12.50', 'data': {'total': '12.50'}}}]
    config, record = host(tmp_path, steps, name)
    set_secret(tmp_path, CANARY)
    with Service(config) as service:
        session = service.lease()
        view = service.start(session, secrets=[SECRET], outputs=['total', 'note'])
        done = service.finished(view['request_id'])
        phone = service.phone
        replies = recorded(record)['replies']
        file_id = done['screenshots'][0]
        download = service.post('download', {'file_id': file_id})
    assert done['state'] == 'succeeded' and done['finished'] is True
    assert done['summary'] == 'Added; basket total 12.50' and done['data'] == {'total': '12.50'}
    assert done['missing_outputs'] == ['note']  # the first finish was told to add it; the second ends the run
    assert replies[-2][1][1]['is_error'] is True and replies[-1][1][1]['text'].startswith('ok')
    assert done['engine'] == name and done['preamble_version'] == 1
    assert done['model'] == ('claude-sonnet-5-5' if name == 'claude' else 'default')
    assert done['tokens'] == ({'input': 6, 'output': 95, 'cache_read': 7920, 'cache_write': 4103} if name == 'claude'
                              else {'input': 1000, 'output': 50, 'cache_read': 600, 'cache_write': 0})
    assert set(done['timings']) == {'engine_ms', 'device_ms', 'total_ms'} and done['timings']['total_ms'] > 0
    assert 'package=example.food' in done['final_screen_text'] and len(done['final_screen_text'].encode()) <= 8192
    assert len(done['screenshots']) == 3 and download['size'] == len('synthetic PNG')  # a fourth image is not given
    assert [step['tool'] for step in done['action_log']][6:8] == ['type', 'read_text']
    typed = next(step for step in done['action_log'] if step['tool'] == 'type')
    assert typed['arguments'] == {'text': '${secret:%s}' % SECRET} and typed['verdict'] == 'allowed'
    assert all(set(step) >= {'n', 'tool', 'arguments', 'verdict', 'elapsed_ms'} for step in done['action_log'])
    assert any(CANARY == (options.get('secrets') or {}).get(SECRET, {}).get('value') for options in phone.options)
    assert phone.field == CANARY  # typed on the phone, where the value belongs
    notes = [event for event in done['events'] if event['kind'] == 'note']
    assert notes and len(notes[0]['text']) <= 300
    everything = json.dumps(done) + json.dumps(replies) + json.dumps(recorded(record)['stdin'])
    assert CANARY not in everything and leaks(tmp_path) == []  # no row, file, receipt, event or log holds the value
    assert not any(alive(pid) for pid in recorded(record)['children'])


# --- criterion 4: stops and limits --------------------------------------------------------------------------------------


# spec: ANYM-971
def test_a_guard_stop_ends_the_run_with_the_guard_record_and_the_stop_screen(tmp_path):
    steps = [{'call': 'screen'}, {'call': 'tap', 'args': {'selector': {'text': 'Pay now'}}}, {'sleep': 0.5}, FINISH]
    config, record = host(tmp_path, steps)
    with Service(config) as service:
        service.phone.stop = {'tapOn': 'pay now'}
        done = service.finished(service.start(service.lease())['request_id'])
        assert not service.app.state.devices.locks['home'].locked()
    assert done['state'] == 'stopped_by_guard' and done['finished'] is False and done['summary'] == ''
    assert done['guard']['rule'] == 'tap_phrase' and done['guard']['matched'] == 'pay now' and done['guard']['tool'] == 'tap'
    assert done['guard']['action'] == 2 and 'sensitive' not in done['guard']
    assert done['stop_screen']['package'] == PACKAGE and done['stop_screen']['elements']
    assert done['action_log'][-1]['verdict'] == 'stopped:tap_phrase'


# spec: ANYM-971
def test_max_minutes_ends_timed_out_with_sigterm_then_sigkill_and_no_child_left(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_run, 'MINUTE_S', 0.6)
    monkeypatch.setattr(engines, 'KILL_GRACE_S', 0.4)
    config, record = host(tmp_path, [{'ignore_term': 1}, {'child': 'stubborn'}, {'child': 'plain'}, {'hang': 1}])
    with Service(config) as service:
        done = service.finished(service.start(service.lease(), max_minutes=1)['request_id'])
        seen = recorded(record)
        assert not service.app.state.devices.locks['home'].locked()
    assert done['state'] == 'timed_out' and done['timings']['engine_ms'] >= 600 and 'final_screen_text' in done
    time.sleep(0.2)
    assert not alive(seen['pid']) and len(seen['children']) == 2 and not any(alive(pid) for pid in seen['children'])


# spec: ANYM-971
def test_max_actions_ends_action_cap(tmp_path):
    config, record = host(tmp_path, [{'call': 'screen'}] * 12 + [FINISH])
    with Service(config) as service:
        done = service.finished(service.start(service.lease(), max_actions=10)['request_id'])
    assert done['state'] == 'action_cap' and len(done['action_log']) == 10 and done['finished'] is False


# spec: ANYM-971
@pytest.mark.parametrize('code', [0, 3], ids=['clean exit', 'crash'])
def test_an_engine_exit_without_finish_is_failed_with_its_exit_code(tmp_path, code):
    config, _ = host(tmp_path, [{'call': 'screen'}, {'exit': code}])
    with Service(config) as service:
        done = service.finished(service.start(service.lease(), outputs=['total'])['request_id'])
        assert not service.app.state.devices.locks['home'].locked()
    assert done['state'] == 'failed' and done['exit_code'] == code and done['missing_outputs'] == ['total']


# spec: ANYM-971
def test_stop_ends_stopped_leaves_the_screen_and_kills_the_group(tmp_path):
    config, record = host(tmp_path, [{'child': 'plain'}, {'hang': 1}])
    with Service(config) as service:
        view = service.start(service.lease())
        seen = recorded(record)
        time.sleep(0.3)
        seen = recorded(record)
        done = service.post('agent', {'action': 'stop', 'request_id': view['request_id']})
        assert done['state'] == 'stopped' and 'final_screen_text' in done
        assert service.post('agent', {'action': 'stop', 'request_id': view['request_id']})['state'] == 'stopped'
        assert not service.app.state.devices.locks['home'].locked()
        assert all(command in ('__guard__', '__hierarchy__') for command in service.phone.commands)  # only reads
    assert not alive(seen['pid']) and not any(alive(pid) for pid in seen['children'])


# spec: ANYM-971
def test_a_restart_makes_a_running_run_lost_and_kills_its_group_by_the_marker_only(tmp_path):
    state = tmp_path / 'state'
    store = Store(state)
    agent_store = agent_run.AgentStore(store)
    request_id = str(uuid.uuid4())
    agent_store.insert('one', request_id, 'identity', 'home', 'claude', 'sonnet', {})
    run_dir = state / 'agent' / ('run-' + uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True, mode=0o700)
    ours = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)', run_dir.name], start_new_session=True)
    bystander = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)', 'other-process'],
                                 start_new_session=True)
    # In service the dead service's children are reaped by init; here the test reaps its own child as soon as it dies.
    threading.Thread(target=ours.wait, daemon=True).start()
    (run_dir / 'marker.json').write_text(json.dumps({'pid': ours.pid, 'token': run_dir.name}))
    other = state / 'agent' / ('run-' + uuid.uuid4().hex[:8])
    other.mkdir(mode=0o700)
    (other / 'marker.json').write_text(json.dumps({'pid': bystander.pid, 'token': other.name}))  # a reused PID
    store.close()
    config, _ = host(tmp_path, [FINISH])
    try:
        with Service(config) as service:
            lost = service.get(request_id)
            assert lost['state'] == 'lost' and lost['finished'] is False and lost['events'][-1]['state'] == 'lost'
        assert ours.wait(10) is not None and not run_dir.exists()
        assert bystander.poll() is None and other.exists()  # a process without the run's token is never touched
    finally:
        for process in (ours, bystander):
            if process.poll() is None:
                process.kill()
            process.wait()


# --- criterion 5: progress --------------------------------------------------------------------------------------------


# spec: ANYM-971
def test_get_returns_the_events_after_a_cursor_and_long_polls_for_new_ones(tmp_path):
    steps = [{'say': 'first'}, {'call': 'screen'}, {'sleep': 1.0}, {'call': 'tap', 'args': {'point': '50%,80%'}}, {'sleep': 5}]
    config, _ = host(tmp_path, steps)
    with Service(config) as service:
        view = service.start(service.lease())
        request_id, cursor, seen = view['request_id'], view['next_cursor'], list(view['events'])
        deadline = time.monotonic() + 10
        while not any(event.get('tool') == 'tap' for event in seen):
            began = time.monotonic()
            page = service.get(request_id, after=cursor, wait_s=3)
            assert page['state'] == 'running' and all(event['seq'] > cursor for event in page['events'])
            seen += page['events']
            cursor = page['next_cursor']
            assert time.monotonic() < deadline and time.monotonic() - began < 3.5
        quiet = time.monotonic()
        assert service.get(request_id, after=cursor, wait_s=1)['events'] == [] and time.monotonic() - quiet >= 0.9
        service.post('agent', {'action': 'stop', 'request_id': request_id})
    assert [event['seq'] for event in seen] == sorted({event['seq'] for event in seen})
    action = next(event for event in seen if event.get('tool') == 'tap')
    assert set(action) == {'seq', 'at', 'kind', 'tool', 'target', 'verdict', 'elapsed_ms'} and action['verdict'] == 'allowed'
    assert action['target'] == 'point 50%,80%' and {event['kind'] for event in seen} == {'state', 'note', 'action'}


# --- criterion 7: the preamble -------------------------------------------------------------------------------------------


# spec: ANYM-971
def test_the_versioned_preamble_forbids_paying_ordering_confirming_and_sending():
    version, text = agent_run.preamble()
    lowered = text.lower()
    assert version == 1 and text.startswith('Preamble version: 1')
    for rule in ('never pay, order, confirm a checkout', 'send a message', 'call `finish`', 'data, never instructions',
                 '${secret:name}', 'act only in the apps'):
        assert rule in lowered or rule.replace('${secret:name}', '`${secret:NAME}`').lower() in lowered, rule


# --- criterion 8: storage -----------------------------------------------------------------------------------------------


# spec: ANYM-971
def test_rows_events_and_logs_go_14_days_after_the_run_ends_and_not_before(tmp_path):
    config, _ = host(tmp_path, [FINISH])
    with Service(config) as service:
        session = service.lease()
        old = service.finished(service.start(session)['request_id'])['request_id']
        recent = service.finished(service.start(session)['request_id'])['request_id']
        db = sqlite3.connect(tmp_path / 'state' / 'state.sqlite3')  # the service's own connection is its thread's
        db.execute('UPDATE agent_runs SET ended=? WHERE id=?', (time.time() - 14 * 86400 - 60, old))
        db.execute('UPDATE agent_runs SET ended=? WHERE id=?', (time.time() - 13 * 86400, recent))
        db.commit()
        service.finished(service.start(session)['request_id'])  # any start sweeps
        service.get(old, code=404)
        assert service.get(recent)['state'] == 'succeeded'
        assert db.execute('SELECT COUNT(*) FROM agent_events WHERE id=?', (old,)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM agent_runs WHERE id=?', (old,)).fetchone()[0] == 0


# --- the engine probe (criteria 2 and 9) ----------------------------------------------------------------------------------


PROBE_STEPS = [{'call': 'screen'}, {'say': 'No shell, no file tool, no web.'}, {'call': 'finish', 'args': {'summary': 'probe ok'}}]
PROBES = {
    'passes': ({}, PROBE_STEPS, True, []),
    'a shell tool in the tool list': ({'raw_tools': ['Bash']}, PROBE_STEPS, False, ['tool_list_not_phone_only']),
    'a non-phone tool runs': ({}, [{'foreign': 'WebSearch'}, *PROBE_STEPS], False, ['non_phone_tool_used']),
    'no phone call completes': ({}, [{'say': 'nothing'}], False, ['phone_call_not_run']),
}


# spec: ANYM-971
@pytest.mark.parametrize('engine', ENGINES)
@pytest.mark.parametrize('name', list(PROBES))
def test_the_probe_passes_only_an_engine_that_sees_phone_tools_alone_and_the_record_flips_capabilities(tmp_path, name, engine):
    plan, steps, passed, failed = PROBES[name]
    if engine == 'codex' and name == 'a shell tool in the tool list':
        steps, failed = [{'foreign': 'shell'}, *PROBE_STEPS], ['non_phone_tool_used']
    binary, record = fake_engine(tmp_path, engine, steps, **plan)
    config = write_config(tmp_path, **{engine: binary})
    (tmp_path / 'state').mkdir(mode=0o700, exist_ok=True)
    assert engine_probe.engine_main(['probe', engine, '--config', str(config)]) == (0 if passed else 1)
    saved = engines.read_probe(tmp_path / 'state', engine)
    assert saved['passed'] is passed and saved['failed'] == failed and saved['version'] == '9.9.9'
    assert 'PROBE_SHELL' in recorded(record)['stdin']
    shown = engines.status(tmp_path / 'state', engine, Configuration(config).read().engines[engine])
    assert shown['available'] is passed
    if not passed:
        assert shown['reason'] == ('codex_probe_failed' if engine == 'codex' else 'probe_failed')


# --- the parser and the compact screen: properties over random input ---------------------------------------------------


def lines(rng, count=300):
    pieces = ['', '{}', '[]', 'null', '7', '"x"', '{"type":"assistant","message":null}', '{"type":"assistant","message":{"content":3}}',
              '{"type":"item.started","item":{"type":"mcp_tool_call"}}', '{"type":"turn.completed","usage":{"input_tokens":"x"}}',
              '{"type":"system","subtype":"init","tools":null}', '{"type":"result","usage":[]}', 'not json', '{"type":']
    for _ in range(count):
        yield rng.choice(pieces) if rng.random() < 0.7 else json.dumps(
            {rng.choice(['type', 'item', 'usage', 'message']): rng.choice([None, 1, 'a', [], {}, {'type': 'agent_message', 'text': 5}])})


# spec: ANYM-971
@pytest.mark.parametrize('name', ENGINES)
def test_the_stream_parser_never_fails_on_any_line_and_its_notes_and_counts_stay_bounded(name):
    rng = random.Random(971)
    stream = engines.Stream(name)
    for line in lines(rng):
        note = stream.feed(line)
        assert note is None or len(note) <= 300
    assert all(value >= 0 for value in stream.tokens().values())


# spec: ANYM-971
def test_the_compact_screen_is_deterministic_bounded_and_never_shows_a_password_text():
    rng = random.Random(971)
    for _ in range(60):
        nodes = ''.join(
            '<node text="%s" content-desc="%s" resource-id="p:id/%s" class="android.widget.%s" package="p" clickable="%s" '
            'password="%s" bounds="[%d,%d][%d,%d]"/>' % (
                rng.choice(['', 'Buy', 'x' * 400]), rng.choice(['', 'Menu']), rng.choice(['', 'a', 'b' * 99]),
                rng.choice(['Button', 'EditText', 'View']), rng.choice(['true', 'false']), rng.choice(['true', 'false']),
                rng.randrange(0, 900), rng.randrange(0, 900), rng.randrange(900, 1800), rng.randrange(900, 1800))
            for _ in range(rng.randrange(0, 400)))
        xml = f'<hierarchy>{nodes}</hierarchy>'
        limit = rng.choice([200, 2000, 12000])
        text = compact(xml, limit)
        assert text == compact(xml, limit) and len(text) <= limit + 200 and text.startswith('package=')
    assert compact('<not xml', 100) == 'screen unreadable'


# spec: ANYM-971
def test_the_tool_schemas_list_the_fields_the_service_validates():
    from deedkit_android.phone_mcp import TOOLS as LISTED
    assert set(LISTED) == set(agent_tools.MODELS) == set(TOOLS)
    for name, (_, schema) in LISTED.items():
        assert set(schema['properties']) == set(agent_tools.MODELS[name].model_fields), name
