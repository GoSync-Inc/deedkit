"""Bare Codex lifecycle and actual learned skills; synthetic executables, no phone or provider."""

import json
from pathlib import Path
import re
import sys
import time

import pytest

from agent_harness import Service, recorded, write_config


def bare_host(tmp_path, *, delay=0, result=True, exit_code=0, commands=1, reject_schema=False,
              fenced=False, invalid_learning=False, startup_error=None, natural=True, native_skill=True):
    record = tmp_path / 'record.json'
    binary = tmp_path / 'codex'
    binary.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys, time
if sys.argv[1:] == ['--version']:
    print('9.9.9'); raise SystemExit
prompt = sys.stdin.read()
skills = pathlib.Path.cwd() / '.agents' / 'skills'
known = {{str(p.relative_to(skills)): p.read_text() for p in skills.glob('*/SKILL.md')}}
json.dump(dict(argv=sys.argv, env=dict(os.environ), stdin=prompt, pid=os.getpid(), known_skills=known), open({str(record)!r}, 'w'))
if ({reject_schema!r} and '--output-schema' in sys.argv) or {startup_error!r}:
    print(json.dumps(dict(type='turn.failed', error=dict(message='stream disconnected before completion'))), flush=True)
    print({startup_error!r} or 'Error: unsupported output schema', file=sys.stderr, flush=True)
    raise SystemExit(1)
print(json.dumps(dict(type='item.started', item=dict(id='a', type='command_execution', command='adb -s SERIAL shell input keyevent HOME'))), flush=True)
for i in range({commands}-1):
    print(json.dumps(dict(type='item.started', item=dict(id=str(i), type='command_execution', command='adb -s SERIAL get-state'))), flush=True)
time.sleep({delay})
print(json.dumps(dict(type='item.completed', item=dict(id='a', type='command_execution', command='adb -s SERIAL shell input keyevent HOME', exit_code=0))), flush=True)
if {result!r}:
    data = dict(summary='Cart total read', data=dict(total='141400'), lessons=[dict(package='example.food', steps=[dict(action='tap', resource_id='example.food:id/cart')], pitfalls=['refresh_hierarchy'])])
    if {invalid_learning!r}:
        data['lessons'][0]['notes'] = 'private task detail'
    body = json.dumps(data)
    if {fenced!r}:
        body = chr(96)*3 + 'json' + chr(10) + body + chr(10) + chr(96)*3
    if {natural!r}:
        body = 'Read-only task complete in ordinary prose.'
    if {native_skill!r}:
        skill = skills / 'example-food' / 'SKILL.md'
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text('---\\nname: example-food\\ndescription: Navigate the example food app.\\n---\\nRead the product title after content appears. Verified route: example://products.\\n')
    pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(body)
print(json.dumps(dict(type='turn.completed', usage=dict(input_tokens=100, cached_input_tokens=20, output_tokens=10))), flush=True)
raise SystemExit({exit_code})
''')
    binary.chmod(0o755)
    config = write_config(tmp_path, codex=binary, default='codex')
    body = json.loads(config.read_text())
    body['agent']['guarded'] = False
    config.write_text(json.dumps(body))
    return config, record


# spec: ANYM-993
def test_ordinary_goal_natural_final_and_native_skill_authoring_reuse(tmp_path):
    config, record = bare_host(tmp_path, natural=True, native_skill=True)
    with Service(config) as service:
        session = service.lease()
        first = service.finished(service.start(session)['request_id'])
        seen = recorded(record)
        assert '$android-learning' not in seen['stdin']
        assert 'result-schema' not in seen['stdin'] and 'lessons' not in seen['stdin']
        assert '--output-schema' not in seen['argv']
        root = Path(seen['env']['CODEX_HOME']) / 'skills'
        assert seen['argv'][seen['argv'].index('--add-dir') + 1] == str(root)
        assert first['state'] == 'succeeded' and first['summary'] == 'Read-only task complete in ordinary prose.'
        assert first['data'] == {} and first['learning_error'] is None
        assert first['skills_updated'] == ['example-food/SKILL.md']
        skill = root / 'example-food' / 'SKILL.md'
        assert 'Verified route: example://products.' in skill.read_text()
        assert not (root / 'android-learning').exists()
        second = service.finished(service.start(session)['request_id'])
        assert second['state'] == 'succeeded'
        assert recorded(record)['known_skills']['example-food/SKILL.md'] == skill.read_text()
        assert 'Verified route' not in recorded(record)['stdin']  # native discovery, no injected skill grammar/body


# spec: ANYM-993
def test_bare_default_launch_result_and_mandatory_skill_reuse(tmp_path):
    config, record = bare_host(tmp_path)
    with Service(config) as service:
        session = service.lease()
        run = service.finished(service.start(session, outputs=['total'])['request_id'])
        assert run['state'] == 'succeeded', run
        result = run
        assert result['data'] == {} and result['summary'] == 'Read-only task complete in ordinary prose.'
        assert result['model'] == 'gpt-6.1-terra' and result['effort'] == 'medium'
        assert result['tokens']['input'] == 100 and result['timings']['commands'] == 1
        assert result['token_usage_observed'] is True
        assert result['skills_updated'] == ['example-food/SKILL.md']
        assert result['final_screenshot']['file_id']
        seen = recorded(record)
        argv, prompt = seen['argv'], seen['stdin']
        assert argv[argv.index('--sandbox') + 1] == 'workspace-write'
        assert 'sandbox_workspace_write.network_access=true' in argv
        assert 'approval_policy=never' in argv
        assert not any('mcp_servers' in item or 'dangerously' in item or 'ignore-rules' in item for item in argv)
        assert 'SERIAL' in prompt and 'Order check for me' in prompt
        assert service.phone.commands == []  # no guard, hierarchy, or phone MCP
        home = Path(seen['env']['CODEX_HOME'])
        skill = home / 'skills' / 'example-food' / 'SKILL.md'
        assert 'Verified route: example://products.' in skill.read_text()
        assert '141400' not in skill.read_text() and 'Cart total' not in skill.read_text()
        second = service.finished(service.start(session, model='gpt-6.1-sol', effort='high')['request_id'])
        assert second['model'] == 'gpt-6.1-sol'
        assert second['effort'] == 'high'
        assert recorded(record)['known_skills']['example-food/SKILL.md'] == skill.read_text()


# spec: ANYM-993
@pytest.mark.parametrize('fields', [{'engine': 'claude'}, {'secrets': ['PASSWORD']}, {'save_as': 'food'},
                                  {'model': '../invalid'}, {'effort': 'invalid'}])
def test_bare_rejects_unsupported_inputs_before_engine_start(tmp_path, fields):
    config, record = bare_host(tmp_path)
    with Service(config) as service:
        service.start(service.lease(), code=422, **fields)
        assert not record.exists()


# spec: ANYM-993
def test_stop_holds_device_and_releases_process_without_publishing_skill(tmp_path):
    config, record = bare_host(tmp_path, delay=60)
    with Service(config) as service:
        session = service.lease()
        request_id = service.start(session)['request_id']
        recorded(record)
        service.start(session, code=409)
        stopped = service.post('agent', {'action': 'stop', 'request_id': request_id})
        assert stopped['state'] == 'stopped'
        assert stopped['skills_updated'] == []
        assert not (tmp_path / 'codex-home' / 'skills' / 'example-food').exists()


# spec: ANYM-993
@pytest.mark.parametrize('result,exit_code', [(False, 0), (True, 2)])
def test_missing_result_or_unsuccessful_exit_never_succeeds(tmp_path, result, exit_code):
    config, _ = bare_host(tmp_path, result=result, exit_code=exit_code)
    with Service(config) as service:
        run = service.finished(service.start(service.lease())['request_id'])
        assert run['state'] == 'failed'
        assert run['skills_updated'] == []


# spec: ANYM-993
def test_knowledge_does_not_accept_private_text_or_dynamic_identifiers(tmp_path):
    # Keep the existing test identity: ordinary navigation prose/templates stay out of receipts;
    # no service-authored grammar strips them or turns task values into knowledge.
    config, record = bare_host(tmp_path)
    with Service(config) as service:
        run = service.finished(service.start(service.lease())['request_id'])
        home = Path(recorded(record)['env']['CODEX_HOME'])
        content = (home / 'skills' / 'example-food' / 'SKILL.md').read_text()
        assert 'Verified route: example://products.' in content
        assert '141400' not in content and 'Cart total' not in content
        assert content not in json.dumps(run)


# spec: ANYM-993
def test_timeout_and_command_cap_cancel_without_publishing(tmp_path, monkeypatch):
    from deedkit_android import agent_bare
    monkeypatch.setattr(agent_bare, 'MINUTE_S', 0.15)
    config, _ = bare_host(tmp_path, delay=60)
    with Service(config) as service:
        run = service.finished(service.start(service.lease(), max_minutes=1)['request_id'])
        assert run['state'] == 'timed_out' and run['skills_updated'] == []
    monkeypatch.setattr(agent_bare, 'MINUTE_S', 60)
    second = tmp_path / 'cap'
    second.mkdir()
    config, _ = bare_host(second, delay=60, commands=11)
    with Service(config) as service:
        run = service.finished(service.start(service.lease(), max_actions=10)['request_id'])
        assert run['state'] == 'action_cap' and run['skills_updated'] == []


# spec: ANYM-993
def test_optional_apps_start_identity_and_owner_scope_are_preserved(tmp_path):
    config, _ = bare_host(tmp_path)
    with Service(config) as service:
        session = service.lease()
        args = dict(action='start', device_id='home', session_id=session,
                    request_id='5f634f65-e8fa-4894-9098-1f0f9ab1fa53', goal='Read cart', owner_instruction='Read my cart')
        first = service.post('agent', args)
        service.post('agent', {**args, 'effort': 'high'}, code=409)
        service.post('agent', {'action': 'get', 'request_id': args['request_id']}, key='two', code=404)
        final = service.finished(first['request_id'])
        assert final['state'] == 'succeeded'
        assert service.post('agent', args)['request_id'] == first['request_id']


# spec: ANYM-993
def test_stop_really_terminates_the_owned_engine_pid(tmp_path):
    from agent_harness import alive
    config, record = bare_host(tmp_path, delay=60)
    with Service(config) as service:
        request_id = service.start(service.lease())['request_id']
        pid = recorded(record)['pid']
        assert alive(pid)
        service.post('agent', {'action': 'stop', 'request_id': request_id})
        deadline = time.monotonic() + 2
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not alive(pid)


# spec: ANYM-993
def test_final_screenshot_uses_serial_specific_adb_and_owner_scoped_download(tmp_path):
    import base64
    from deedkit_android.device import Devices
    config, _ = bare_host(tmp_path)
    adb = tmp_path / 'adb'
    log = tmp_path / 'adb.json'
    adb.write_text(f'''#!{sys.executable}
import json, sys
json.dump(sys.argv[1:], open({str(log)!r}, 'w'))
sys.stdout.buffer.write(b'\\x89PNG\\r\\n\\x1a\\n' + b'0'*16)
''')
    adb.chmod(0o755)
    body = json.loads(config.read_text())
    body['adb'] = str(adb)
    config.write_text(json.dumps(body))
    with Service(config) as service:
        devices = service.app.state.devices
        devices.capture_image = Devices.capture_image.__get__(devices)
        run = service.finished(service.start(service.lease())['request_id'])
        assert json.loads(log.read_text()) == ['-s', 'SERIAL', 'exec-out', 'screencap', '-p']
        file_id = run['final_screenshot']['file_id']
        downloaded = service.post('download', {'file_id': file_id})
        assert base64.b64decode(downloaded['data_base64']).startswith(b'\x89PNG')
        service.post('download', {'file_id': file_id}, key='two', code=404)


# spec: ANYM-993
def test_bare_closes_the_target_warm_ui_worker_while_holding_its_phone_lock(tmp_path):
    config, _ = bare_host(tmp_path)
    with Service(config) as service:
        devices = service.app.state.devices
        closed = []

        async def close(device=None):
            if device is not None:
                assert devices.locks[device].locked()
                closed.append(device)

        devices.ui.close = close
        run = service.finished(service.start(service.lease())['request_id'])
        assert run['state'] == 'succeeded'
        assert closed == ['home']


# spec: ANYM-993
def test_app_skills_have_native_codex_names_and_keep_package_identity(tmp_path):
    import yaml
    config, record = bare_host(tmp_path)
    with Service(config) as service:
        service.finished(service.start(service.lease())['request_id'])
        root = Path(recorded(record)['env']['CODEX_HOME']) / 'skills'
        path, = root.glob('*/SKILL.md')
        metadata = yaml.safe_load(path.read_text().split('---')[1])
        assert re.fullmatch(r'[a-z0-9-]{1,63}', metadata['name'])
        assert path.parent.name == metadata['name']
        assert metadata['description'] == 'Navigate the example food app.'


# spec: ANYM-993
def test_native_startup_uses_plain_output_and_an_explicit_local_result_contract(tmp_path):
    config, record = bare_host(tmp_path, reject_schema=True)
    with Service(config) as service:
        run = service.finished(service.start(service.lease())['request_id'])
        assert run['state'] == 'succeeded', run
        seen = recorded(record)
        assert '--output-schema' not in seen['argv']
        assert 'result-schema.json' not in seen['stdin']
        assert '$android-learning' not in seen['stdin']


# spec: ANYM-993
def test_one_json_codefence_preserves_native_result_and_learned_skill(tmp_path):
    config, _ = bare_host(tmp_path, fenced=True, natural=False)
    with Service(config) as service:
        run = service.finished(service.start(service.lease())['request_id'])
        assert run['state'] == 'succeeded', run
        assert run['data'] == {} and run['summary'].startswith('```json\n')
        assert '"total": "141400"' in run['summary']
        assert run['skills_updated'] == ['example-food/SKILL.md']


# spec: ANYM-993
def test_invalid_learning_does_not_erase_a_completed_native_outcome(tmp_path):
    config, _ = bare_host(tmp_path, invalid_learning=True, natural=False)
    with Service(config) as service:
        run = service.finished(service.start(service.lease())['request_id'])
        assert run['state'] == 'succeeded', run
        assert '"summary": "Cart total read"' in run['summary'] and run['data'] == {}
        assert run['learning_error'] is None and run['skills_updated'] == ['example-food/SKILL.md']


# spec: ANYM-993
def test_startup_failure_returns_sanitized_native_error_without_success(tmp_path):
    canary = 'PRIVATE_OAUTH_CANARY_abcdefghijklmnopqrstuvwxyz'
    config, _ = bare_host(tmp_path, startup_error='Error: auth token=' + canary + '\n<hierarchy>private phone</hierarchy>')
    with Service(config) as service:
        run = service.finished(service.start(service.lease())['request_id'])
        assert run['state'] == 'failed' and run['exit_code'] == 1
        assert run['timings']['commands'] == 0 and run['tokens']['input'] == 0
        assert run['timings']['commands_started'] == 0 and run['token_usage_observed'] is False
        assert any('stream disconnected' in item['message'] for item in run['engine_errors'])
        assert any(item['source'] == 'stderr' for item in run['engine_errors'])
        assert canary not in json.dumps(run) and 'private phone' not in json.dumps(run)


# spec: ANYM-993
def test_native_diagnostics_redact_authorization_and_cookie_headers():
    from deedkit_android.agent_diagnostics import message
    canary = 'short_private_credential_123'
    for header in ('Authorization: Bearer ', 'Authorization: Basic ', '"Authorization": "Bearer ',
                   'Cookie: session=', 'Set-Cookie: session='):
        assert canary not in message('Error: ' + header + canary)

