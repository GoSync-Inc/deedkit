"""Same accepted generation after reboot; all Docker, host boot and call inputs are synthetic."""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from platform_contracts.release import ReleaseRuntime

from scripts import seamless_release as s
from test_seamless_release import GEN, IMAGE, ROUTER_ID, SHA, settings


@pytest.fixture(autouse=True)
def preserve_process_umask():
    # The CLI deliberately hardens its process. An in-process CLI test must not
    # change the permissions of files later tests create through sudo.
    previous = os.umask(0o077)
    os.umask(previous)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def boot(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    cfg['http_release']['ready_timeout'] = 0
    state = Path(cfg['state_directory'])
    release = state / SHA
    release.mkdir(mode=0o700)
    plan_state = tmp_path / 'plan'
    plan_state.mkdir(mode=0o700)
    plan = {'state_directory': str(plan_state), 'projects': []}
    s.p.write(plan_state / 'current.json', {'status': 'applied', 'sha': SHA})
    route = s.route_document(cfg, GEN, [], baseline=s.route_document(cfg))
    s.p.write(Path(cfg['http_release']['router']['config_file']), route)
    s.p.write(release / 'http.json', {})
    policy = cfg['http_release']['gateway_policy']
    receipt = {'version': 1, 'sha': SHA, 'generation': GEN, 'status': 'serving',
        'config_digest': s.p.digest(cfg), 'compose': str(release / 'http.json'), 'compose_digest': s.p.digest({}),
        'private_files': {policy: hashlib.sha256(Path(policy).read_bytes()).hexdigest()},
        'images': {'runtime': IMAGE, 'gateway': IMAGE}, 'router_container': ROUTER_ID, 'router_after': route,
        'controls': {role: str(release / role / 'control.sock') for role in ('api', 'gateway')},
        'containers': {role: {'container': role + '-id', 'image': IMAGE} for role in ('api', 'gateway')}}
    for path in (state / 'http-current.json', release / 'http-result.json'):
        s.p.write(path, receipt)
    s.p.write(release / 'http-attempted.json', {'status': 'serving', 'sha': SHA})
    runtimes = {role: ReleaseRuntime(GEN, SHA, receipt['controls'][role], lambda: True) for role in ('api', 'gateway')}
    for runtime in runtimes.values():
        runtime._startup_complete = True
    commands, controls, calls = [], [], []
    docker = {role: {'Id': role + '-id', 'Image': IMAGE, 'State': {'Running': True}, 'Config': {'Labels': {
        'com.docker.compose.project': 'deedkit-http-' + GEN,
        'com.docker.compose.service': 'http-' + role + '-' + GEN, 'ai.deedkit.http-release': GEN}}}
        for role in runtimes}

    def run(argv, **kwargs):
        commands.append(argv)
        if argv[:2] == ['docker', 'compose'] and argv[-3:-1] == ['ps', '-q']:
            return ('api-id' if 'http-api-' in argv[-1] else 'gateway-id') + '\n'
        if argv[:3] == ['docker', 'image', 'inspect']:
            return json.dumps([{'Id': IMAGE, 'Config': {'Labels': {'org.opencontainers.image.revision': SHA,
                'org.opencontainers.image.source': 'https://github.com/' + s.p.REPOSITORY}}}])
        if argv[:2] == ['docker', 'inspect']:
            return json.dumps([next(value for value in docker.values() if value['Id'] == argv[-1])])
        raise AssertionError(argv)

    def control(path, action, generation, sha):
        role = next(role for role, value in receipt['controls'].items() if value == path)
        controls.append((role, action))
        return s.validate_control(asyncio.run(runtimes[role].control(action)), generation, sha)

    monkeypatch.setattr(s.p, 'run', run)
    monkeypatch.setattr(s, 'control', control)
    monkeypatch.setattr(s, 'router_identity', lambda _: ROUTER_ID)
    monkeypatch.setattr(s, 'router_get', lambda _: (copy.deepcopy(route), '"etag"'))
    monkeypatch.setattr(s.p, 'on_main', lambda mirror, ancestor, target: ancestor == target)
    monkeypatch.setattr(s.p, 'boot_id', lambda: 'synthetic-current-boot')
    monkeypatch.setattr(s.p, 'wait_for_calls', lambda *_: calls.append('calls'))
    monkeypatch.setattr(s.p, 'PAUSE', tmp_path / 'pause')
    monkeypatch.setattr(s.p, 'BACKUP_RUNNER', tmp_path / 'absent')
    monkeypatch.setenv('DEEDKIT_BACKUP_LOCK', str(tmp_path / 'backup.lock'))
    monkeypatch.setattr(s, 'GLOBAL_LOCK', tmp_path / 'global')
    monkeypatch.setattr(s.p, 'DEADLINE', None)
    return SimpleNamespace(cfg=cfg, plan=plan, state=state, release=release, receipt=receipt, runtimes=runtimes,
        controls=controls, commands=commands, calls=calls, route=route, docker=docker)


async def http(runtime):
    frames = []
    async def app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200})
    async def send(frame):
        frames.append(frame)
    await runtime.wrap(app)({'type': 'http', 'method': 'POST', 'path': '/query'}, None, send)
    return frames[0]['status']


# spec: ANYM-221
def test_accepted_pair_warming_after_reboot_resumes_without_new_generation_or_receipt_rewrite(boot):
    accepted = {path: path.read_bytes() for path in (boot.state / 'http-current.json', boot.release / 'http-result.json')}
    assert [asyncio.run(http(runtime)) for runtime in boot.runtimes.values()] == [503, 503]
    assert s.resume(boot.cfg, boot.plan)['status'] == 'resumed'
    assert [asyncio.run(http(runtime)) for runtime in boot.runtimes.values()] == [200, 200]
    assert [role for role, action in boot.controls if action == 'activate'] == ['api', 'gateway']
    assert all(path.read_bytes() == value for path, value in accepted.items())
    assert not any(word in cmd for cmd in boot.commands for word in ('up', 'start', 'restart', 'stop', 'rm'))
    assert len(boot.calls) >= 2


# spec: ANYM-221
@pytest.mark.parametrize('fault', ['pause', 'unresolved', 'newer', 'receipt', 'route', 'persisted', 'container',
    'image', 'policy', 'compose', 'foreign-control', 'draining', 'unready', 'call', 'unknown', 'boot'])
def test_boot_never_activates_when_acceptance_identity_or_a_guard_is_unproved(boot, monkeypatch, fault):
    if fault == 'pause':
        s.p.PAUSE.touch()
    elif fault == 'unresolved':
        s.p.write(boot.release / 'http-attempted.json', {'status': 'switching', 'sha': SHA})
    elif fault == 'newer':
        other = boot.state / ('f' * 40)
        other.mkdir()
        s.p.write(other / 'http-attempted.json', {'status': 'serving', 'sha': 'f' * 40})
    elif fault == 'receipt':
        s.p.write(boot.release / 'http-result.json', {**boot.receipt, 'generation': 'f' * 12})
    elif fault == 'route':
        boot.route['active'] = None
    elif fault == 'persisted':
        s.p.write(Path(boot.cfg['http_release']['router']['config_file']), {})
    elif fault == 'container':
        boot.docker['api']['Config']['Labels']['ai.deedkit.http-release'] = 'f' * 12
    elif fault == 'image':
        boot.docker['api']['Image'] = 'sha256:' + 'f' * 64
    elif fault == 'policy':
        Path(boot.cfg['http_release']['gateway_policy']).write_text('{}')
    elif fault == 'compose':
        s.p.write(boot.release / 'http.json', {'changed': True})
    elif fault == 'foreign-control':
        boot.runtimes['gateway'].generation = 'f' * 12
    elif fault == 'draining':
        boot.runtimes['gateway'].phase = 'draining'
    elif fault == 'unready':
        boot.runtimes['gateway']._startup_complete = False
    elif fault == 'call':
        monkeypatch.setattr(s.p, 'wait_for_calls', lambda *_: (_ for _ in ()).throw(s.p.CallActive('synthetic')))
    elif fault == 'unknown':
        s.p.write(boot.release / 'http-resume.json', {'status': 'activating'})
    elif fault == 'boot':
        monkeypatch.setattr(s.p, 'boot_id', lambda: None)
    with pytest.raises((s.Refused, s.p.CallActive)):
        s.resume(boot.cfg, boot.plan)
    assert not any(action == 'activate' for _, action in boot.controls)


# spec: ANYM-221
def test_unknown_activation_reply_is_journalled_and_never_retried(boot, monkeypatch):
    original = s.control
    def lost_reply(path, action, generation, sha):
        answer = original(path, action, generation, sha)
        if action == 'activate':
            raise TimeoutError('synthetic accepted activation, lost reply')
        return answer
    monkeypatch.setattr(s, 'control', lost_reply)
    with pytest.raises(TimeoutError):
        s.resume(boot.cfg, boot.plan)
    assert s.p.read(boot.release / 'http-resume.json')['status'] == 'activating'
    with pytest.raises(s.Refused):
        s.resume(boot.cfg, boot.plan)
    assert sum(action == 'activate' for _, action in boot.controls) == 1


# spec: ANYM-221
@pytest.mark.parametrize('change', ['pause', 'current', 'boot', 'call'])
def test_boot_rechecks_guards_after_call_wait_before_first_activation(boot, monkeypatch, change):
    def changed(*_):
        if change == 'pause':
            s.p.PAUSE.touch()
        elif change == 'current':
            s.p.write(boot.state / 'http-current.json', {**boot.receipt, 'generation': 'f' * 12})
        elif change == 'boot':
            monkeypatch.setattr(s.p, 'boot_id', lambda: 'different-boot')
        else:
            raise s.p.CallsUnknown('synthetic unknown calls')
    monkeypatch.setattr(s.p, 'wait_for_calls', changed)
    with pytest.raises(s.Refused):
        s.resume(boot.cfg, boot.plan)
    assert not any(action == 'activate' for _, action in boot.controls)


# spec: ANYM-221
def test_boot_partly_active_pair_activates_only_its_warming_member_and_then_is_read_only(boot):
    boot.runtimes['api'].phase = 'active'
    assert s.resume(boot.cfg, boot.plan)['status'] == 'resumed'
    assert [role for role, action in boot.controls if action == 'activate'] == ['gateway']
    saved = (boot.release / 'http-resume.json').read_bytes()
    boot.controls.clear()
    assert s.resume(boot.cfg, boot.plan)['status'] == 'already-active'
    assert not any(action == 'activate' for _, action in boot.controls)
    assert (boot.release / 'http-resume.json').read_bytes() == saved


# spec: ANYM-221
@pytest.mark.parametrize('lock', ['backup', 'global', 'plan', 'http'])
def test_boot_excludes_every_other_release_or_backup_owner_without_wait_or_activation(boot, lock):
    import fcntl
    import os
    path = {'backup': Path(os.environ['DEEDKIT_BACKUP_LOCK']), 'global': s.GLOBAL_LOCK / 'deploy.lock',
            'plan': Path(boot.plan['state_directory']) / 'deploy.lock', 'http': boot.state / 'deploy.lock'}[lock]
    path.parent.mkdir(mode=0o700, exist_ok=True)
    with path.open('w') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(s.p.Busy):
            s.resume(boot.cfg, boot.plan)
    assert not boot.controls


# spec: ANYM-221
def test_boot_unit_is_one_docker_start_action_without_timer_deploy_or_retry():
    unit = (Path(__file__).resolve().parents[1] / 'deploy/deedkit-http-release.service').read_text()
    assert 'WantedBy=docker.service' in unit and 'Type=oneshot' in unit and 'TimeoutStartSec=600' in unit
    assert 'seamless_release.py resume --config ${DEEDKIT_HTTP_CONFIG} --plan-a-config ${DEEDKIT_DEPLOY_CONFIG}' in unit
    assert 'Restart=' not in unit and 'production.py deploy' not in unit and 'Requires=docker.service' not in unit
    assert 'EnvironmentFile=-/etc/deedkit/autodeploy.env' in unit
    assert not (Path(__file__).resolve().parents[1] / 'deploy/deedkit-http-release.timer').exists()


# spec: ANYM-221
def test_boot_attempt_must_name_the_exact_accepted_sha(boot):
    s.p.write(boot.release / 'http-attempted.json', {'status': 'serving', 'sha': 'f' * 40})
    with pytest.raises(s.Refused):
        s.resume(boot.cfg, boot.plan)
    assert not boot.controls


# spec: ANYM-221
def test_resume_cli_selects_current_receipt_only_and_keeps_failure_details_private(boot, monkeypatch, capsys):
    monkeypatch.setattr(s.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(s.p, 'config', lambda path: boot.cfg if path == 'http' else boot.plan)
    monkeypatch.setattr(s, 'validate_settings', lambda cfg: cfg)
    with pytest.raises(SystemExit) as failed:
        s.main(['resume', '--config', 'http', '--plan-a-config', 'plan', '--sha', SHA])
    assert failed.value.code == 1
    assert capsys.readouterr().err == 'HTTP release refused: Refused\n'
    selected = []
    monkeypatch.setattr(s, 'resume', lambda cfg, plan: selected.append((cfg, plan)) or
                       {'status': 'resumed', 'sha': SHA, 'generation': GEN})
    s.main(['resume', '--config', 'http', '--plan-a-config', 'plan'])
    assert selected == [(boot.cfg, boot.plan)]
    assert json.loads(capsys.readouterr().out) == {'status': 'resumed', 'sha': SHA, 'generation': GEN}
