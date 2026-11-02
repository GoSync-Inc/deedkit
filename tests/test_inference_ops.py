"""ANYM-684: installing the inference service on the Mac mini (launchd, sandbox, busy probe, keys, deploy, probe).

Nothing here touches a mini, a tailnet, Docker or a real service: ssh, docker, the process table and GitHub are
fakes, the keygen and busy probe run on temporary directories, and the HTTPS probe talks to a local server with a
throwaway certificate. The modules under test are stdlib only and are loaded by path, so the suite does not depend
on the services/inference package (ANYM-682).
"""
import calendar
import concurrent.futures
import contextlib
import fcntl
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'services' / 'inference' / 'src' / 'deedkit_inference'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


keygen = load('inference_ops_keygen', PACKAGE / 'keygen.py')
busy_probe = load('inference_ops_busy_probe', PACKAGE / 'busy_probe.py')
render = load('inference_ops_render', ROOT / 'services' / 'inference' / 'launchd' / 'render.py')
sys.path.insert(0, str(ROOT / 'scripts'))
import inference_deploy as deploy  # noqa: E402
import inference_probe as probe  # noqa: E402
import production  # noqa: E402

SHA = 'a' * 40
SHA2 = 'b' * 40
ADMIN = {'admin': 'miniadmin', 'admin_home': '/Users/miniadmin', 'python': '/opt/homebrew/bin/python3.13'}


# ---------------------------------------------------------------- criterion 2: plist, sandbox, newsyslog

def service_plist():
    return plistlib.loads(render.render_service_plist(**ADMIN))


# spec: ANYM-684
def test_plist_is_loopback_nice_sandboxed_and_runs_as_the_service_user():
    plist = service_plist()
    profile = render.render_sandbox_profile()
    args = plist['ProgramArguments']
    assert plist['UserName'] == '_deedkitinf' and plist['GroupName'] == '_deedkitinf'
    # Standard at Nice 20: Background (efficiency cores, throttled I/O) made speech 17x slower on the mini; the busy probe stays Background
    assert plist['ProcessType'] == 'Standard' and plist['Nice'] == 20
    assert plistlib.loads(render.render_busy_plist(**{k: ADMIN[k] for k in ('admin', 'admin_home', 'python')}))['ProcessType'] == 'Background'
    assert plist['ExitTimeOut'] >= 900 and plist['RunAtLoad'] is True
    # sandbox-exec wraps the whole service (a shell that sets the release and execs it, so the tree is one), with the
    # shipped profile, and the service is started as `serve --config` with the release read from `current`
    assert args[:2] == ['/usr/bin/sandbox-exec', '-f'] and args[2].endswith('.sb')
    assert args[3:5] == ['/bin/sh', '-c'] and len(args) == 6
    script = args[5]
    assert script.startswith('DEEDKIT_INFERENCE_RELEASE="$(cat /Users/_deedkitinf/current/RELEASE)" exec ')
    assert script.endswith('/Users/_deedkitinf/current/.venv/bin/deedkit-inference serve --config /Users/_deedkitinf/config.json')
    assert not any(value in ('0.0.0.0', '::') for value in args)
    assert plist['StandardOutPath'].startswith('/Users/_deedkitinf/logs/')
    assert plist['StandardErrorPath'].startswith('/Users/_deedkitinf/logs/')
    # the profile: no outbound network at all; loopback port 8090 is the only thing that can be accepted
    lines = [line.strip() for line in profile.splitlines() if line.strip() and not line.strip().startswith(';')]
    # every network operation is denied first (outbound, other binds, other listeners), then the loopback port is let back
    assert '(deny network*)' in lines and lines.index('(deny network*)') < min(
        i for i, line in enumerate(lines) if line.startswith('(allow network'))
    allowed = [line for line in lines if line.startswith('(allow network')]
    assert sorted(allowed) == ['(allow network-bind (local ip "localhost:8090"))', '(allow network-inbound (local ip "localhost:8090"))']
    for line in allowed:
        assert re.search(r'localhost:8090\)', line) or '(local ip "localhost:8090")' in line, line
        assert 'remote' not in line and '*:' not in line, line
    assert not re.search(r'\(allow\s+(network\*|network-outbound\s*\))', profile)
    assert re.findall(r':(\d+)', profile) == ['8090'] * len(re.findall(r':(\d+)', profile))


# spec: ANYM-684
def test_sandboxed_code_cannot_change_what_runs_unsandboxed_or_read_the_keys():
    profile = render.render_sandbox_profile()
    home = '/Users/_deedkitinf'
    lines = [line.strip() for line in profile.splitlines() if line.strip() and not line.strip().startswith(';')]
    deny = f'(deny file-write* (subpath "{home}"))'
    allow = f'(allow file-write* (subpath "{home}/work") (subpath "{home}/logs") (subpath "{home}/state") (subpath "{home}/.cache"))'
    # the whole service home is read-only to the sandboxed tree (uv.toml, a proxy setting, the releases, the links, the
    # config, the policy, the key directory: all of it is later read or run outside the sandbox), except four places
    assert deny in lines and allow in lines and lines.index(deny) < lines.index(allow)  # the last matching rule wins
    assert [line for line in lines if line.startswith('(allow file-write')] == [allow]
    assert f'(deny file-read* (subpath "{home}/keys") (literal "{home}/probe.key"))' in lines
    for never in ('.config', 'releases', 'keys', 'policy.json', 'current'):
        assert never not in allow
    env = service_plist()['EnvironmentVariables']
    assert env['PYTHONDONTWRITEBYTECODE'] == '1'
    assert json.loads(render.render_config())['usage_file'].startswith(home + '/state/')  # the one file it keeps is writable


# spec: ANYM-684
def test_config_binds_loopback_and_matches_the_plist_and_the_probe():
    config = json.loads(render.render_config())
    plist = service_plist()
    assert config['version'] == 1 and config['mode'] == 'production'
    assert config['bind'] == '127.0.0.1' and config['port'] == 8090
    assert config['drain_seconds'] == 900 and plist['ExitTimeOut'] > config['drain_seconds']
    assert config['busy_file'] == '/Users/Shared/deedkit-inference/busy.json'
    assert plist['ProgramArguments'][-1].endswith('--config /Users/_deedkitinf/config.json')
    assert config['policy_file'] == '/Users/_deedkitinf/policy.json' and config['work_dir'] == '/Users/_deedkitinf/work'
    assert busy_probe.DEFAULT_FILE == config['busy_file']
    assert 'fake' not in json.dumps(config) and 'release' not in config  # the release comes from `current/RELEASE`


# spec: ANYM-684
def test_plist_keepalive_does_not_respawn_after_a_clean_exit():
    # KeepAlive is exactly {SuccessfulExit: false}: launchd restarts after a crash or a non-zero exit (the
    # service's own drain timeout) but not after the exit 0 of a finished drain, so a deploy's SIGTERM stops it
    # for good until `kickstart`. `Crashed`, a bare true or a PathState would each respawn the old release.
    plist = service_plist()
    assert plist['KeepAlive'] == {'SuccessfulExit': False}
    assert 'ThrottleInterval' not in plist or plist['ThrottleInterval'] >= 5
    # and the deploy stops it by SIGTERM and starts it with kickstart without -k (which would kill a running job)
    commands = SshRecorder().exercise_deploy()
    kills = [c for c in commands if 'launchctl kill' in c]
    kicks = [c for c in commands if 'launchctl kickstart' in c]
    assert kills and all('SIGTERM' in c and 'SIGKILL' not in c for c in kills)
    assert kicks and all(' -k' not in c for c in kicks)
    # the stop is graceful all the way: bootout (SIGTERM first) unloads the job so nothing restarts it, bootstrap loads it
    assert any('launchctl bootout --wait' in c for c in commands) and any('launchctl bootstrap system /Library/LaunchDaemons/' in c for c in commands)


# spec: ANYM-684
def test_rendered_files_hold_no_unfilled_placeholder_and_the_log_rotation_names_the_log():
    for blob in (render.render_service_plist(**ADMIN).decode(), render.render_busy_plist(**ADMIN).decode(), render.render_impact_plist(**ADMIN).decode(),
                 render.render_sandbox_profile(), render.render_newsyslog()):
        assert '@' not in blob.replace('@@', '')
    rotation = render.render_newsyslog()
    log = service_plist()['StandardErrorPath']
    assert log in rotation and '_deedkitinf:_deedkitinf' in rotation and '640' in rotation


# ---------------------------------------------------------------- criterion 3: busy probe

class FakeHost:
    """docker contexts and containers, and the process table, of a fake admin account."""

    def __init__(self, contexts=None, containers=None, processes='', broken=(), vm_processes=''):
        self.contexts = contexts if contexts is not None else {'default': '/var/run/docker.sock'}
        self.containers = containers or {}
        self.processes = processes
        self.vm_processes = vm_processes  # the `uid pcpu comm` table the probe reads for Between's VMs
        self.broken = set(broken)
        self.calls = []

    def sockets(self):
        return {path for path in self.contexts.values()}

    def __call__(self, argv):
        self.calls.append(list(argv))
        name = Path(argv[0]).name
        if name in self.broken:
            raise OSError(f'{name} failed')
        if name == 'docker' and argv[1:3] == ['context', 'ls']:
            if 'context-ls' in self.broken:
                return 'not json'
            return '\n'.join(json.dumps({'Name': n, 'DockerEndpoint': 'unix://' + s}) for n, s in self.contexts.items())
        if name == 'docker' and argv[1] == '--context':
            context = argv[2]
            if context in self.broken:
                raise subprocess.CalledProcessError(1, argv)
            return '\n'.join(self.containers.get(context, []))
        if name == 'ps':
            return self.vm_processes if 'uid=,pcpu=,comm=' in argv else self.processes
        raise AssertionError(f'unexpected command {argv}')


UIDS = {'_deedkitci': 453, '_betweenci': 451}


def observe(host, *, existing=None, uids=None, between_ci=None):
    existing = host.sockets() if existing is None else existing
    return busy_probe.observe(host, socket_exists=lambda path: path in existing, uid_of=lambda user: (uids or UIDS)[user],
                              between_ci=between_ci)


CI_LINE = '  453 python3 -c import fcntl /Users/_deedkitci/deedkit-ci/.locks/slot1 4242'


# spec: ANYM-684
def test_probe_reports_between_and_ci_activity():
    assert observe(FakeHost()) == {'between': False, 'deedkit_ci': False}
    # a Between container by name in the default context
    host = FakeHost(containers={'default': ['between-vm-1 foo=bar']})
    assert observe(host)['between'] is True
    # a Between label on another container, in a second context (Between's own colima context)
    host = FakeHost(contexts={'default': '/var/run/docker.sock', 'colima-b': '/Users/miniadmin/.colima/b/docker.sock'},
                    containers={'default': ['web nginx'], 'colima-b': ['db x=y,io.between.project=between']})
    assert observe(host) == {'between': True, 'deedkit_ci': False}
    # a context whose socket does not exist is skipped, not queried and not busy
    host = FakeHost(contexts={'default': '/var/run/docker.sock', 'gone': '/nonexistent.sock'},
                    containers={'gone': ['between-vm-1 x=y']})
    assert observe(host, existing={'/var/run/docker.sock'})['between'] is False
    assert not any(call[:3] == ['docker', '--context', 'gone'] for call in host.calls)
    # a remote-test run: a process of _deedkitci whose arguments name .locks/slot
    assert observe(FakeHost(processes=CI_LINE))['deedkit_ci'] is True
    # the same words in another user's process (the admin account's grep) are not a run
    assert observe(FakeHost(processes=CI_LINE.replace('453', '501', 1)))['deedkit_ci'] is False
    # a _deedkitci process that holds no slot (its shell, ssh session) is not a run either
    assert observe(FakeHost(processes='  453 /bin/bash /Users/_deedkitci/deedkit-ci/run.sh 99'))['deedkit_ci'] is False
    # the test user does not exist on this host: no process can run as it
    host = FakeHost(processes=CI_LINE)
    assert busy_probe.observe(host, socket_exists=lambda path: True, uid_of=lambda user: (_ for _ in ()).throw(KeyError(user)))['deedkit_ci'] is False


VM = '/System/Library/Frameworks/Virtualization.framework/Versions/A/XPCServices/com.apple.Virtualization.VirtualMachine.xpc/Contents/MacOS/com.apple.Virtualization.VirtualMachine'


def vm_table(*cpus, admin=0.0):
    """`ps -axo uid=,pcpu=,comm=` of the mini as recorded on 11-02: the _betweenci colima/lima helpers (all near 0 %),
    one Virtualization process per VM with the given %cpu, and the admin account's own colima VM."""
    rows = ['  451   0.0 /opt/between-ci/bin/colima', '  451   0.4 /opt/between-ci/bin/limactl', '  451   0.0 /bin/bash',
            '  451   0.0 sleep', '  451   0.0 ssh: /Users/_betweenci/.colima/_lima/colima-betweenci/ssh.sock [mux]']
    rows += [f'  451 {cpu:5.1f} {VM}' for cpu in cpus]
    rows += [f'  501 {admin:5.1f} {VM}', '  453 99.0 /usr/bin/python3', '  garbage line', '  501 not-a-number ' + VM]
    return '\n'.join(rows)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# spec: ANYM-684
def test_probe_sees_between_ci_that_runs_inside_its_colima_vms():
    # 11-02: Between's runners ran a full pytest job inside the _betweenci VM (a Virtualization process near 100 %)
    # while no Docker context of the admin account listed a container, and the file said between: false
    host = FakeHost(vm_processes=vm_table(0.3, 97.6))
    assert observe(host, between_ci=busy_probe.VmLoad(Clock())) == {'between': True, 'deedkit_ci': False}
    # the same VMs idle (recorded 0.2-6.8 %): not busy
    assert observe(FakeHost(vm_processes=vm_table(0.3, 2.4)), between_ci=busy_probe.VmLoad(Clock()))['between'] is False
    # the admin account's own VM and other users' Virtualization processes are not Between's
    assert observe(FakeHost(vm_processes=vm_table(0.3, 1.0, admin=99.0)), between_ci=busy_probe.VmLoad(Clock()))['between'] is False
    # no _betweenci user on this host: no VM of it can run
    gone = {'_deedkitci': 453}
    assert observe(FakeHost(vm_processes=vm_table(99.0)), uids=gone, between_ci=busy_probe.VmLoad(Clock()))['between'] is False
    # a container in a Docker context is still enough on its own
    host = FakeHost(containers={'default': ['between-vm-1 x=y']}, vm_processes=vm_table(0.1))
    assert observe(host, between_ci=busy_probe.VmLoad(Clock()))['between'] is True
    # no Virtualization process at all (no VM running): idle
    assert observe(FakeHost(vm_processes='  451 0.0 /opt/between-ci/bin/colima'), between_ci=busy_probe.VmLoad(Clock()))['between'] is False


# spec: ANYM-684
def test_probe_keeps_between_busy_through_the_gaps_of_a_bursty_job():
    # pytest on the VM spikes and idles within seconds; one quiet sample must not reopen admission
    clock = Clock()
    load = busy_probe.VmLoad(clock)
    host = FakeHost()
    verdicts = []
    for cpu, step in [(91.2, 5), (1.2, 5), (0.4, 5), (2.0, 60), (1.0, 50), (1.0, 20), (0.5, 5)]:
        host.vm_processes = vm_table(cpu, 0.2)
        verdicts.append((clock.now - 1000.0, observe(host, between_ci=load)['between']))
        clock.now += step
    # busy at the spike and for the hold afterwards, free again once a quiet window has also passed
    assert verdicts[:4] == [(0.0, True), (5.0, True), (10.0, True), (15.0, True)]
    assert verdicts[4] == (75.0, True) and verdicts[5] == (125.0, True)
    assert verdicts[6] == (145.0, False)
    # a sustained moderate load (15 % of a core for a minute) counts as busy; a steady 3 % does not
    clock = Clock()
    load = busy_probe.VmLoad(clock)
    for _ in range(14):
        assert observe(FakeHost(vm_processes=vm_table(15.0, 0.1)), between_ci=load)['between'] is True
        clock.now += 5
    clock = Clock()
    load = busy_probe.VmLoad(clock)
    for _ in range(30):
        assert observe(FakeHost(vm_processes=vm_table(3.0, 0.1)), between_ci=load)['between'] is False
        clock.now += 5


# spec: ANYM-684
@pytest.mark.parametrize('failing', ['uid=,pcpu=,comm=', 'uid=,args='])
def test_probe_sets_busy_when_either_process_table_cannot_be_read(failing):
    # one of the two tables fails while the docker side answers: fail closed, both flags true
    class OneBroken(FakeHost):
        def __call__(self, argv):
            if Path(argv[0]).name == 'ps' and failing in argv:
                raise subprocess.CalledProcessError(1, argv)
            return super().__call__(argv)

    assert observe(OneBroken(), between_ci=busy_probe.VmLoad(Clock())) == {'between': True, 'deedkit_ci': True}


# spec: ANYM-684
@pytest.mark.parametrize('reading', ['not-a-number', 'nan', 'inf', '-1.0'])
def test_probe_sets_busy_when_a_between_vm_reading_is_unreadable(reading):
    # a Between VM whose %cpu cannot be read is not "0 %": it must not hide the VM, or another busy one beside it
    table = vm_table(0.3) + f'\n  451 {reading} {VM}'
    assert observe(FakeHost(vm_processes=table), between_ci=busy_probe.VmLoad(Clock())) == {'between': True, 'deedkit_ci': True}


# spec: ANYM-684
def test_a_restarted_probe_starts_busy_and_opens_only_after_the_hold():
    # launchd restarts the probe in a quiet gap of a running job: it knows nothing of the spike before, so it holds
    clock = Clock()
    load = busy_probe.VmLoad(clock)
    load.hold()
    host = FakeHost(vm_processes=vm_table(0.4, 0.2))
    for waited in (0, 5, 60, 115):
        clock.now = 1000.0 + waited
        assert observe(host, between_ci=load)['between'] is True
    clock.now = 1000.0 + 125
    assert observe(host, between_ci=load)['between'] is False


# spec: ANYM-684
@pytest.mark.parametrize('broken', [('docker',), ('ps',), ('context-ls',), ('default',)])
def test_probe_sets_busy_when_it_cannot_observe(broken):
    # a command error, an unreadable process table, unparseable docker output or a context that exists but cannot
    # be queried: both flags true (fail closed: Between and the test runs first)
    host = FakeHost(processes='', broken=broken)
    assert observe(host) == {'between': True, 'deedkit_ci': True}


# spec: ANYM-684
def test_probe_once_reports_idle_only_when_nothing_is_active(monkeypatch, capsys):
    monkeypatch.setattr(busy_probe, 'observe', lambda **kwargs: {'between': False, 'deedkit_ci': False})
    assert busy_probe.main(['--once']) == 0 and json.loads(capsys.readouterr().out) == {'between': False, 'deedkit_ci': False}
    for state in ({'between': True, 'deedkit_ci': False}, {'between': False, 'deedkit_ci': True}):
        monkeypatch.setattr(busy_probe, 'observe', lambda state=state, **kwargs: state)
        assert busy_probe.main(['--once']) == 1 and json.loads(capsys.readouterr().out) == state

    def explode(**kwargs):
        raise RuntimeError('boom')
    monkeypatch.setattr(busy_probe, 'observe', explode)
    assert busy_probe.main(['--once']) == 1 and json.loads(capsys.readouterr().out) == {'between': True, 'deedkit_ci': True}


# spec: ANYM-684
def test_probe_sets_busy_when_the_observation_itself_blows_up(tmp_path):
    def explode(*args, **kwargs):
        raise RuntimeError('boom')
    assert busy_probe.observe_safely(explode) == {'between': True, 'deedkit_ci': True}


# spec: ANYM-684
def test_probe_writes_atomically_with_a_fresh_timestamp(tmp_path, monkeypatch):
    tmp_path = tmp_path / 'shared'  # the suite's autouse fixtures put their own entries in tmp_path itself
    tmp_path.mkdir()
    path = tmp_path / 'busy.json'
    busy_probe.write_busy(path, {'between': False, 'deedkit_ci': True}, 1000.0)
    assert json.loads(path.read_text()) == {'ts': 1000.0, 'between': False, 'deedkit_ci': True}
    # a failed replace leaves the previous complete file and no temporary file
    real_replace = os.replace
    monkeypatch.setattr(os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(OSError):
        busy_probe.write_busy(path, {'between': True, 'deedkit_ci': True}, 2000.0)
    monkeypatch.setattr(os, 'replace', real_replace)
    assert json.loads(path.read_text())['ts'] == 1000.0
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ['busy.json']
    # the loop stamps every write with the time of its own observation and survives a failing observation
    clock = iter([10.0, 15.0, 20.0])
    seen = []
    states = iter([{'between': False, 'deedkit_ci': False}, RuntimeError('x'), {'between': False, 'deedkit_ci': False}])

    def fake_observe():
        value = next(states)
        if isinstance(value, Exception):
            raise value
        return value

    def sleep(seconds):
        assert seconds == 5
        seen.append(json.loads(path.read_text()))

    busy_probe.loop(path, observe=fake_observe, clock=lambda: next(clock), sleep=sleep, rounds=3)
    assert [entry['ts'] for entry in seen] == [10.0, 15.0] and seen[1]['between'] is True
    assert json.loads(path.read_text())['ts'] == 20.0


# spec: ANYM-684
def test_probe_file_modes(tmp_path, monkeypatch):
    # readable by the owner and the service user's group only, writable by the owner only, whatever the umask
    old = os.umask(0)
    try:
        path = tmp_path / 'busy.json'
        modes = []
        real_replace = os.replace
        monkeypatch.setattr(os, 'replace', lambda src, dst: (modes.append(stat.S_IMODE(os.stat(src).st_mode)), real_replace(src, dst)))
        busy_probe.write_busy(path, {'between': False, 'deedkit_ci': False}, 1.0)
        assert modes == [0o640]
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
        os.chmod(path, 0o666)
        busy_probe.write_busy(path, {'between': False, 'deedkit_ci': False}, 2.0)
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
    finally:
        os.umask(old)
    # the launchd job runs as the admin account, with KeepAlive, and is not the service user's
    plist = plistlib.loads(render.render_busy_plist(**ADMIN))
    assert plist['UserName'] == 'miniadmin' and plist['KeepAlive'] is True and plist['RunAtLoad'] is True
    assert plist['EnvironmentVariables']['HOME'] == '/Users/miniadmin'
    assert plist['ProgramArguments'][0] == ADMIN['python']
    assert plist['ProgramArguments'][1] == '/usr/local/libexec/deedkit-busy-probe'
    assert plist['ProgramArguments'][-1] == '/Users/Shared/deedkit-inference/busy.json'


DUTY_FAKE_PS = '''#!/bin/sh
# a fake ps: PS_MODE picks what the process table says about pid 4242
case "$PS_MODE" in
  gone) exit 1 ;;
  blind) exit 2 ;;
esac
case "$*" in
  *lstart*) case "$PS_MODE" in
      blind-lstart) exit 2 ;;
      empty-lstart) exit 0 ;;
      reused) echo "Tue Jan  2 00:00:00 2001" ;;
      *) echo "Mon Nov  2 03:25:37 2026    " ;; esac ;;
esac
exit 0
'''


# spec: ANYM-684
@pytest.mark.parametrize('shell', ['bash', 'zsh'])
@pytest.mark.parametrize('mode,kept', [('gone', False), ('reused', False), ('alive', True), ('blind', True),
                                       ('blind-lstart', True), ('empty-lstart', True)])
def test_undo_drops_a_duty_logger_entry_only_on_positive_evidence(tmp_path, shell, mode, kept):
    # the undo block of the runbook, run as written: an entry whose process cannot be inspected, or that is still the
    # logger, stays in the ledger (and the undo exits 1); only "gone" or "now another process" drops it
    if not shutil.which(shell):
        pytest.skip(f'{shell} is not installed')
    doc = (ROOT / 'docs' / 'operations' / 'inference-mini-setup.md').read_text()
    start = doc.index('sed -n "s/^duty //p" $L | while IFS="|" read -r P S; do')
    snippet = doc[start:doc.index('if grep -qx job-service $L;', start)]
    fakes = tmp_path / 'bin'
    fakes.mkdir()
    (fakes / 'ps').write_text(DUTY_FAKE_PS)
    (fakes / 'ps').chmod(0o755)
    (fakes / 'kill').write_text('#!/bin/sh\necho "kill must never be called" >&2\nexit 99\n')
    (fakes / 'kill').chmod(0o755)
    ledger, left = tmp_path / 'ledger', tmp_path / 'left'
    ledger.write_text('duty 4242|Mon Nov  2 03:25:37 2026    \n')
    left.write_text('')
    done = subprocess.run([shell, '-c', f'L={ledger}; R={left}\n{snippet}'], capture_output=True, text=True,
                          env={'PATH': f'{fakes}:/usr/bin:/bin', 'HOME': str(tmp_path), 'PS_MODE': mode})
    assert done.returncode == 0 and 'kill must never' not in done.stderr, done.stderr
    assert (left.read_text().strip() != '') is kept, (mode, left.read_text())


SUDO_FAKE = '''#!/bin/sh
while [ "$1" = -n ] || [ "$1" = -H ]; do shift; done
if [ "$1" = -u ]; then shift 2; fi
exec "$@"
'''


# spec: ANYM-691
@pytest.mark.parametrize('shell', ['bash', 'zsh'])
@pytest.mark.parametrize('recover_rc,ledger,loaded,stops', [
    (0, False, False, False), (2, False, False, False),  # done / a release before the feature, no ledger
    (4, False, False, True), (1, False, False, True),  # recover failed: nothing more is removed
    (2, True, False, True), (0, True, False, True),  # the ledger is still there
    (0, False, True, True)])  # the service job is still loaded
def test_undo_stops_before_removing_anything_when_recovery_fails(tmp_path, shell, recover_rc, ledger, loaded, stops):
    if not shutil.which(shell):
        pytest.skip(f'{shell} is not installed')
    doc = (ROOT / 'docs' / 'operations' / 'inference-mini-setup.md').read_text()
    start = doc.index('if grep -qx job-service $L;')
    snippet = doc[start:doc.index('if grep -qx job-busy $L;', start)].replace('/Users/', f'{tmp_path}/')
    fakes, release = tmp_path / 'bin', tmp_path / '_u' / 'current' / '.venv' / 'bin'
    for directory in (fakes, release, tmp_path / '_u' / 'state'):
        directory.mkdir(parents=True)
    (fakes / 'sudo').write_text(SUDO_FAKE)
    (fakes / 'launchctl').write_text(f'#!/bin/sh\n[ "$1" = print ] && exit {0 if loaded else 113}\nexit 0\n')
    (release / 'deedkit-inference').write_text(f'#!/bin/sh\n[ "$1" = recover ] || exit 9\nexit {recover_rc}\n')
    for fake in (fakes / 'sudo', fakes / 'launchctl', release / 'deedkit-inference'):
        fake.chmod(0o755)
    if ledger:
        (tmp_path / '_u' / 'state' / 'paused.json').write_text('{}')
    list_file, left = tmp_path / 'ledger', tmp_path / 'left'
    list_file.write_text('job-service\nhome\n')
    left.write_text('')
    done = subprocess.run([shell, '-c', f'U=_u; L={list_file}; R={left}\n{snippet}\necho reached-removal'], capture_output=True,
                          text=True, env={'PATH': f'{fakes}:/usr/bin:/bin', 'HOME': str(tmp_path)})
    assert (done.returncode == 1) is stops, (done.stdout, done.stderr)
    assert ('reached-removal' not in done.stdout) is stops and ('undo stopped' in done.stdout) is stops
    assert stops or done.returncode == 0


# spec: ANYM-684
def test_duty_log_verdicts_and_summary():
    duty = load('inference_ops_duty_log', ROOT / 'services' / 'inference' / 'launchd' / 'duty_log.py')
    now = 1_000_000.0
    fresh = json.dumps({'ts': now - 5, 'between': False, 'deedkit_ci': False})
    assert duty.verdict(fresh, now, 2.0, 9.0, 8.0, 7.9) == []
    assert duty.verdict(json.dumps({'ts': now, 'between': True, 'deedkit_ci': True}), now, 2.0, 9.0, 8.0, 7.9) == ['busy_between', 'busy_deedkit_ci']
    assert duty.verdict(None, now, 2.0, 9.0, 8.0, 7.9) == ['busy_file_missing']
    assert duty.verdict('not json', now, 2.0, 9.0, 8.0, 7.9) == ['busy_file_invalid']
    assert duty.verdict(json.dumps({'ts': now, 'between': 'no', 'deedkit_ci': False}), now, 2.0, 9.0, 8.0, 7.9) == ['busy_file_invalid']
    assert duty.verdict(json.dumps({'ts': now - 31, 'between': False, 'deedkit_ci': False}), now, 2.0, 9.0, 8.0, 7.9) == ['busy_file_stale']
    assert duty.verdict(json.dumps({'ts': now + 31, 'between': False, 'deedkit_ci': False}), now, 2.0, 9.0, 8.0, 7.9) == ['busy_file_stale']
    assert duty.verdict(fresh, now, 9.5, 5.0, 8.0, 7.9) == ['load_high', 'memory_low']
    rows = [duty.line(now + 60 * n, reasons, 1.0, 9.0) for n, reasons in enumerate(
        [[], [], ['busy_between'], ['busy_between', 'load_high'], ['busy_between'], [], ['memory_low'], []])]
    assert duty.summarize(rows) == {'samples': 8, 'open_fraction': 0.5, 'longest_closed_minutes': 3.0,
                                    'closed_by': {'busy_between': 3, 'load_high': 1, 'memory_low': 1}}
    assert duty.summarize([]) == {'samples': 0}


IMPACT = load('inference_ops_impact_log', ROOT / 'services' / 'inference' / 'launchd' / 'impact_log.py')
IMPACT_VM = '/System/Library/Frameworks/Virtualization.framework/Versions/A/XPCServices/com.apple.Virtualization.VirtualMachine.xpc/Contents/MacOS/com.apple.Virtualization.VirtualMachine'
T0 = 'Mon Nov  2 13:00:00 2026'
T1 = 'Mon Nov  2 13:05:00 2026'


def ps_text(*rows):
    """A fake `ps -axo uid=,pid=,stat=,lstart=,cputime=,rss=,comm=` table: (uid, pid, started, cputime, rss_kb, command[, state])."""
    return ''.join(f'{uid:>5} {pid:>5} {(state or ["S"])[0]} {started} {cpu} {rss} {command}\n'
                   for uid, pid, started, cpu, rss, command, *state in rows)


# spec: ANYM-684
def test_impact_log_reads_ps_cputime_and_counts_a_phase_by_cpu_not_by_a_resident_worker():
    assert IMPACT.parse_cputime('0:12.34') == pytest.approx(12.34)
    assert IMPACT.parse_cputime('1:02:03.50') == pytest.approx(3723.5)
    assert IMPACT.parse_cputime('2-01:00:00') == pytest.approx(2 * 86400 + 3600)
    base = (501, 10, T0, '0:01.00', 2048, '/sbin/launchd'), (600, 20, T0, '0:10.00', 4000000, 'python'), (451, 30, T0, '5:00.00', 900000, IMPACT_VM)
    before = IMPACT.read_processes(lambda: ps_text(*base))
    assert before[20] == (600, 10.0, 4000000, 'python', ' '.join(T0.split()), 'S')  # the start time is compared as words
    # a warm engine worker holds 4 GB and burns nothing: not a phase; the same process burning 6 s in 10 s is 0.6 cores
    idle = IMPACT.read_processes(lambda: ps_text(base[0], (600, 20, T0, '0:10.02', 4000000, 'python'), base[2]))
    busy = IMPACT.read_processes(lambda: ps_text(base[0], (600, 20, T0, '0:16.00', 4000000, 'python'), base[2], (600, 21, T1, '0:03.00', 100, 'child')))
    assert IMPACT.cores(before, idle, 600, 10.0)[0] < IMPACT.PHASE_CORES
    used, count, rss = IMPACT.cores(before, busy, 600, 10.0)
    assert used == pytest.approx(0.9) and count == 2 and rss == 4000100  # the child that started in the interval counts from zero
    # another user's processes and, with a mark, non-matching commands are not counted
    assert IMPACT.cores(before, busy, 451, 10.0, IMPACT.VM_MARK)[0] == 0.0
    vm_busy = IMPACT.read_processes(lambda: ps_text(base[0], base[1], (451, 30, T0, '5:20.00', 900000, IMPACT_VM)))
    assert IMPACT.cores(before, vm_busy, 451, 10.0, IMPACT.VM_MARK)[0] == pytest.approx(2.0)
    assert IMPACT.cores(before, vm_busy, 600, 10.0, IMPACT.VM_MARK)[0] == 0.0
    with pytest.raises(ValueError):
        IMPACT.read_processes(lambda: 'garbage\n')


# spec: ANYM-684
def test_impact_log_counts_a_reused_pid_from_zero_whatever_its_counter():
    old = IMPACT.read_processes(lambda: ps_text((600, 20, T0, '1:40.00', 1000, 'python')))  # 100 s of CPU
    # the pid was reused by a process that started later and has burned 6 s: 0.4 cores over 15 s, not 0 (counter went down)
    reborn = IMPACT.read_processes(lambda: ps_text((600, 20, T1, '0:06.00', 1000, 'python')))
    assert IMPACT.cores(old, reborn, 600, 15.0)[0] == pytest.approx(0.4)
    # and a replacement with a larger counter is not compared with its predecessor either
    bigger = IMPACT.read_processes(lambda: ps_text((600, 20, T1, '2:00.00', 1000, 'python')))
    assert IMPACT.cores(old, bigger, 600, 15.0)[0] == pytest.approx(8.0)
    same = IMPACT.read_processes(lambda: ps_text((600, 20, T0, '1:43.00', 1000, 'python')))
    assert IMPACT.cores(old, same, 600, 15.0)[0] == pytest.approx(0.2)


# spec: ANYM-684
def test_impact_log_busy_flags_fail_to_unknown_and_the_line_round_trips():
    now = 1_000_000.0
    fresh = json.dumps({'ts': now - 3, 'between': True, 'deedkit_ci': False})
    assert IMPACT.busy_flags(fresh, now) == ('t', 'f')
    for broken in (None, 'not json', json.dumps({'ts': now - 31, 'between': True, 'deedkit_ci': True}),
                   json.dumps({'ts': now, 'between': 'yes', 'deedkit_ci': False}),
                   '{"ts": NaN, "between": false, "deedkit_ci": false}', '{"ts": Infinity, "between": false, "deedkit_ci": false}',
                   '{"ts": 1' + '0' * 400 + ', "between": false, "deedkit_ci": false}'):
        assert IMPACT.busy_flags(broken, now) == ('?', '?')
    figures = {'inf_cpu': 0.62, 'inf_rss_kb': 4096 * 1024, 'stopped_kb': 0, 'vm_cores': 3.5, 'ci_cores': 0.0, 'load': 9.25, 'avail_gb': 6.04, 'swap_mb': 2300}
    row = IMPACT.parse_line(IMPACT.sample_line(now, ('t', 'f'), figures))
    assert row == {'ts': 1_000_000, 'between': 't', 'ci': 'f', 'phase': True, 'stopped_mb': 0.0, 'inf_cpu': 0.62, 'inf_rss_mb': 4096.0, 'vm_cores': 3.5,
                   'ci_cores': 0.0, 'load': 9.25, 'avail_gb': 6.0, 'swap_mb': 2300.0}
    assert IMPACT.parse_line('ts=1 between=t') is None and IMPACT.parse_line('') is None
    vm_stat = 'Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 65536\nPages inactive: 65536\nPages speculative: 65536\nPages active: 9\n'
    assert IMPACT.free_gb(vm_stat) == pytest.approx(3.0)
    # a header without all three counters is a rejected sample, not a 0.0 GB reading
    for broken_stat in ('page size of 16384 bytes\n', vm_stat.replace('Pages inactive: 65536\n', ''), vm_stat.replace('page size of 16384 bytes', '')):
        with pytest.raises(ValueError):
            IMPACT.free_gb(broken_stat)
    assert IMPACT.swap_mb('vm.swapusage: total = 3072.00M  used = 2300.50M  free = 771.50M  (encrypted)') == pytest.approx(2300.5)
    assert IMPACT.swap_mb('total = 4.00G  used = 1.50G') == pytest.approx(1536.0)


def impact_line(n, between, ci, phase, load, avail, vm=0.0, base=1_000_000, stopped_mb=0):
    figures = {'inf_cpu': 1.0 if phase else 0.0, 'inf_rss_kb': 0, 'stopped_kb': stopped_mb * 1024, 'vm_cores': vm, 'ci_cores': 0.0, 'load': load, 'avail_gb': avail, 'swap_mb': 100}
    return IMPACT.sample_line(base + 15 * n, (between, ci), figures)


# spec: ANYM-684
def test_impact_summary_separates_overlap_from_between_alone_and_idle():
    rows = [impact_line(0, 'f', 'f', 0, 1.0, 7.0), impact_line(1, 't', 'f', 0, 6.0, 5.0, 4.0), impact_line(2, 't', 'f', 0, 8.0, 5.0, 4.0),
            impact_line(3, 't', 'f', 1, 12.0, 3.0, 4.0), impact_line(4, 'f', 't', 1, 14.0, 2.0), impact_line(5, 'f', 'f', 1, 3.0, 6.0),
            impact_line(6, 't', 'f', 0, 6.0, 5.0), impact_line(7, '?', '?', 0, 1.0, 7.0), impact_line(8, '?', '?', 1, 1.0, 7.0),
            impact_line(9, '?', 'f', 0, 1.0, 7.0), impact_line(10, 't', 'f', 0, 7.0, 2.0, 1.0, stopped_mb=4096), 'garbage', '']
    summary = IMPACT.summarize(rows)
    assert summary['samples'] == 11 and summary['unknown_busy_samples'] == 3 and summary['gaps'] == 0
    # a stopped phase (no CPU, RAM still held) is its own situation, not "Between alone"
    assert summary['paused']['samples'] == 1 and summary['paused']['stopped_mb_max'] == 4096.0 and summary['paused']['avail_gb_min'] == 2.0
    assert summary['contended_alone']['stopped_samples'] == 0
    assert summary['overlap_minutes'] == 0.5 and summary['overlap_episodes'] == 1 and summary['longest_overlap_minutes'] == 0.5
    assert summary['overlap_while'] == {'between': 1, 'ci': 1}
    assert summary['overlap']['samples'] == 2 and summary['overlap']['load_mean'] == 13.0 and summary['overlap']['avail_gb_min'] == 2.0
    assert summary['contended_alone']['samples'] == 3 and summary['contended_alone']['load_mean'] == pytest.approx(6.667, abs=0.001)
    assert summary['contended_alone']['vm_cores_mean'] == pytest.approx(2.667, abs=0.001)
    # unknown busy flags are neither "alone" nor "idle": the two quiet samples are the only known ones
    assert summary['phase_alone']['samples'] == 1 and summary['idle']['samples'] == 1
    assert IMPACT.summarize([]) == {'samples': 0} and IMPACT.summarize(['x']) == {'samples': 0}


# spec: ANYM-684
def test_impact_summary_breaks_an_overlap_episode_at_a_hole_in_the_samples():
    rows = [impact_line(0, 't', 'f', 1, 9.0, 4.0), impact_line(1, 't', 'f', 1, 9.0, 4.0),
            impact_line(241, 't', 'f', 1, 9.0, 4.0), impact_line(242, 't', 'f', 1, 9.0, 4.0), impact_line(243, 't', 'f', 1, 9.0, 4.0)]
    summary = IMPACT.summarize(rows)  # one hour (240 samples) missing between the second and the third
    assert summary['overlap_episodes'] == 2 and summary['longest_overlap_minutes'] == 0.8
    assert summary['gaps'] == 1 and summary['gap_minutes'] == pytest.approx(59.8, abs=0.05)
    assert IMPACT.summarize(list(reversed(rows)))['overlap_episodes'] == 2  # the order of the lines does not matter
    # one missed sample (30 s between two lines at a 15 s interval) already ends the episode; ordinary jitter does not
    one_missing = IMPACT.summarize([impact_line(0, 't', 'f', 1, 9.0, 4.0), impact_line(2, 't', 'f', 1, 9.0, 4.0)])
    assert one_missing['overlap_episodes'] == 2 and one_missing['gaps'] == 1 and one_missing['gap_minutes'] == 0.2
    jitter = IMPACT.summarize([impact_line(0, 't', 'f', 1, 9.0, 4.0), IMPACT.sample_line(1_000_000 + 15 + 6, ('t', 'f'), {
        'inf_cpu': 1.0, 'inf_rss_kb': 0, 'stopped_kb': 0, 'vm_cores': 0.0, 'ci_cores': 0.0, 'load': 9.0, 'avail_gb': 4.0, 'swap_mb': 100})])
    assert jitter['overlap_episodes'] == 1 and jitter['gaps'] == 0


# spec: ANYM-684
def test_impact_log_collects_one_line_skips_the_first_and_never_signals(tmp_path, monkeypatch):
    first = ps_text((600, 20, T0, '0:10.00', 4000000, 'python'), (451, 30, T0, '5:00.00', 900000, IMPACT_VM))
    second = ps_text((600, 20, T0, '0:15.00', 4000000, 'python'), (451, 30, T0, '5:20.00', 900000, IMPACT_VM),
                     (600, 22, T0, '0:00.00', 2048 * 1024, 'worker', 'T+'))  # a stopped worker holding 2 GB
    tables = iter([IMPACT.read_processes(lambda: first), IMPACT.read_processes(lambda: second)])
    clock = iter([100.0, 110.0])
    monkeypatch.setattr(IMPACT, 'read_processes', lambda: next(tables))
    monkeypatch.setattr(IMPACT.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(IMPACT, 'uid_of', {'_deedkitinf': 600, '_deedkitci': None, '_betweenci': 451}.get)
    monkeypatch.setattr(IMPACT.os, 'getloadavg', lambda: (7.5, 0.0, 0.0))
    answers = {'/usr/bin/vm_stat': 'page size of 16384 bytes\nPages free: 65536\nPages inactive: 0\nPages speculative: 0\n',
               '/usr/sbin/sysctl': 'vm.swapusage: total = 3072.00M  used = 100.00M  free = 2972.00M'}
    monkeypatch.setattr(IMPACT.subprocess, 'run', lambda argv, **kw: type('R', (), {'stdout': answers[argv[0]]})())
    busy = tmp_path / 'busy.json'
    busy.write_text(json.dumps({'ts': 1_000_000, 'between': True, 'deedkit_ci': False}))
    state = {}
    with pytest.raises(ValueError):
        IMPACT.collect(state, 1_000_000.0, str(busy))  # the first sample has no interval: the logger skips it
    line = IMPACT.collect(state, 1_000_010.0, str(busy))
    row = IMPACT.parse_line(line)
    assert row['between'] == 't' and row['ci'] == 'f' and row['phase'] is True and row['inf_cpu'] == 0.5  # 5 s of CPU in 10 s is a phase (>= 0.3 cores)
    assert row['vm_cores'] == 2.0 and row['load'] == 7.5 and row['avail_gb'] == 1.0 and row['swap_mb'] == 100.0
    assert row['stopped_mb'] == 2048.0 and row['inf_rss_mb'] == pytest.approx(4000000 / 1024 + 2048, abs=1)  # the line keeps whole MB
    # the monitor is read-only: nothing in it signals, escalates or talks to launchd
    source = (ROOT / 'services' / 'inference' / 'launchd' / 'impact_log.py').read_text()
    for forbidden in ('kill', 'sudo', 'launchctl', 'os.system', 'signal'):
        assert forbidden not in source.split('"""', 2)[2]


# spec: ANYM-684
def test_impact_log_loop_survives_failed_appends_and_summaries_and_retries_the_summary(tmp_path, monkeypatch):
    day1, day2 = 1_700_000_000.0, 1_700_000_000.0 + 86400  # two local days
    d1, d2 = IMPACT.day_of(day1), IMPACT.day_of(day2)
    ticks = [day1 + n * 15 for n in range(4)] + [day2 + n * 15 for n in range(120)]
    now = {'t': ticks[0]}

    class Done(Exception):
        pass

    def sleep(_seconds):
        ticks.pop(0)
        if not ticks:
            raise Done  # the clock has run out: end the endless loop
        now['t'] = ticks[0]

    calls = {'append': 0, 'summary': 0}
    real_append, real_summary = IMPACT.append_line, IMPACT.write_summary

    def append(directory, day, line):
        calls['append'] += 1
        if calls['append'] in (2, 3):
            raise OSError('disk full')
        real_append(directory, day, line)

    def summary(directory, day, interval):
        calls['summary'] += 1
        if calls['summary'] <= 2:
            (directory / f'.summary-{day}.json.part').write_text('{"trunc')  # a half-written partial file
            raise OSError('disk full')
        real_summary(directory, day, interval)

    monkeypatch.setattr(IMPACT, 'append_line', append)
    monkeypatch.setattr(IMPACT, 'write_summary', summary)
    monkeypatch.setattr(IMPACT, 'collect', lambda state, started, busy: impact_line(0, 'f', 'f', 0, 1.0, 7.0, base=int(started)))
    with pytest.raises(Done):
        IMPACT.run_log(tmp_path, str(tmp_path / 'busy.json'), 15, 0, clock=lambda: now['t'], sleep=sleep)
    logs = sorted(path.name for path in tmp_path.glob('impact-*.log'))
    assert logs == [f'impact-{d1}.log', f'impact-{d2}.log']
    # two appends failed and were skipped; the loop went on to the next day
    assert (tmp_path / f'impact-{d1}.log').read_text().count('\n') == 2
    # the summary of day one failed twice (rollover, then the retry) and was written by a later retry, never truncated
    final = json.loads((tmp_path / f'summary-{d1}.json').read_text())
    assert final['samples'] == 2 and calls['summary'] >= 3


# spec: ANYM-684
def test_impact_summary_write_is_atomic_and_a_missing_day_writes_nothing(tmp_path):
    line = impact_line(0, 't', 'f', 1, 9.0, 4.0)
    (tmp_path / 'impact-2026-11-02.log').write_text(line + '\n')
    (tmp_path / 'impact-2026-11-03.log').write_text(line + '\n')
    assert IMPACT.pending_days(tmp_path, '2026-11-03') == ['2026-11-02']
    IMPACT.write_summary(tmp_path, '2026-11-02', 15)
    IMPACT.write_summary(tmp_path, '2026-11-04', 15)
    assert json.loads((tmp_path / 'summary-2026-11-02.json').read_text())['samples'] == 1
    assert not (tmp_path / 'summary-2026-11-04.json').exists() and not list(tmp_path.glob('.summary-*'))
    assert IMPACT.pending_days(tmp_path, '2026-11-03') == [] and IMPACT.pending_days(tmp_path, '2026-11-04') == ['2026-11-03']


# spec: ANYM-684
def test_impact_summary_catch_up_goes_on_past_a_day_that_keeps_failing(tmp_path, monkeypatch):
    line = impact_line(0, 't', 'f', 1, 9.0, 4.0)
    for day in ('2026-11-01', '2026-11-02', '2026-11-03'):
        (tmp_path / f'impact-{day}.log').write_text(line + '\n')
    real = IMPACT.write_summary

    def failing(directory, day, interval):
        if day == '2026-11-01':
            raise OSError('unreadable')
        real(directory, day, interval)

    monkeypatch.setattr(IMPACT, 'write_summary', failing)
    assert IMPACT.catch_up(tmp_path, '2026-11-04', 15) is False  # the failed day is reported for another try
    assert not (tmp_path / 'summary-2026-11-01.json').exists()
    assert (tmp_path / 'summary-2026-11-02.json').exists() and (tmp_path / 'summary-2026-11-03.json').exists()
    assert IMPACT.pending_days(tmp_path, '2026-11-04') == ['2026-11-01']


# spec: ANYM-684
def test_impact_plist_is_a_read_only_background_job_of_the_admin_account():
    plist = plistlib.loads(render.render_impact_plist(**ADMIN))
    assert plist['Label'] == 'com.deedkit.inference-impact' and plist['UserName'] == 'miniadmin'
    assert plist['KeepAlive'] is True and plist['RunAtLoad'] is True and plist['ProcessType'] == 'Background' and plist['Nice'] == 10
    assert plist['ProgramArguments'] == [ADMIN['python'], '/usr/local/libexec/deedkit-impact-log', 'log', '--dir',
                                         '/Users/miniadmin/deedkit-inference-impact']
    assert plist['StandardOutPath'] == plist['StandardErrorPath'] == '/Users/miniadmin/deedkit-inference-impact/launchd.log'
    assert 'sudo' not in ' '.join(plist['ProgramArguments'])


# ---------------------------------------------------------------- criterion 6: keygen

class Policy:
    """The test fixture's mirror of ANYM-682's authentication: a hash in any key of any principal, not expired."""

    def __init__(self, path):
        self.path = Path(path)

    def doc(self):
        return json.loads(self.path.read_text())

    def authenticates(self, key, now=None):
        digest = hashlib.sha256(key.encode()).hexdigest()
        now = now or time.time()
        for principal in self.doc()['principals']:
            for entry in principal['keys']:
                expires = calendar.timegm(time.strptime(entry['expires_at'], '%Y-%m-%dT%H:%M:%SZ'))
                if entry['sha256'] == digest and expires > now:
                    return principal['id']
        return None


@pytest.fixture()
def kg(tmp_path, capsys):
    policy = tmp_path / 'policy.json'

    class Kit:
        pass

    kit = Kit()
    kit.dir, kit.policy, kit.capsys = tmp_path, policy, capsys
    kit.state = Policy(policy)
    kit.clock = [1_800_000_000.0]
    keygen.utcnow = lambda: kit.clock[0]

    def run(*argv, key_file=None):
        args = ['--policy', str(policy), *argv]
        if key_file is not None:
            args += ['--key-file', str(key_file)]
        return keygen.main(args)

    kit.run = run
    yield kit
    keygen.utcnow = time.time


def first_key(kit, principal='messaging-enrichment', name='first.key'):
    path = kit.dir / name
    code = kit.run('--principal', principal, '--scopes', 'audio,image,document', '--max-jobs-per-day', '2000',
                   '--max-concurrent', '2', '--expires-days', '365', key_file=path)
    assert code == 0
    return path.read_text().strip()


def readback_line(expires_at, reachable='yes'):
    return f'inference mini=mini.example.ts.net reachable={reachable} status=ok queue=0/0/16 admission=open key_days_left=364 key_expires={expires_at}'


# spec: ANYM-684
def test_keygen_writes_only_the_hash(kg):
    key = first_key(kg)
    text = kg.policy.read_text()
    doc = json.loads(text)
    assert key not in text and len(key) >= 40
    principal, = doc['principals']
    entry, = principal['keys']
    assert entry['sha256'] == hashlib.sha256(key.encode()).hexdigest()
    assert entry['expires_at'] == '2028-01-15T08:00:00Z'  # 1_800_000_000 + 365 days, UTC
    assert principal == {'id': 'messaging-enrichment', 'keys': [entry], 'scopes': ['audio', 'image', 'document'],
                         'max_jobs_per_day': 2000, 'max_concurrent': 2}
    assert doc['revision'] == 1 and doc['version'] == 1 and stat.S_IMODE(kg.policy.stat().st_mode) == 0o600
    # a second principal bumps the revision and leaves the first as it was
    second = first_key(kg, 'deploy-probe', 'probe.key')
    doc2 = json.loads(kg.policy.read_text())
    assert doc2['revision'] == 2 and doc2['principals'][0] == principal and second not in kg.policy.read_text()
    # refuses a principal that exists, a scope that is not one of the three, a non-positive cap
    assert kg.run('--principal', 'deploy-probe', '--scopes', 'audio', '--max-jobs-per-day', '1', '--max-concurrent', '1',
                  '--expires-days', '1', key_file=kg.dir / 'again.key') != 0
    assert kg.run('--principal', 'x', '--scopes', 'audio,video', '--max-jobs-per-day', '1', '--max-concurrent', '1',
                  '--expires-days', '1', key_file=kg.dir / 'v.key') != 0
    assert kg.run('--principal', 'y', '--scopes', 'audio', '--max-jobs-per-day', '0', '--max-concurrent', '1',
                  '--expires-days', '1', key_file=kg.dir / 'z.key') != 0
    assert json.loads(kg.policy.read_text()) == doc2


# spec: ANYM-684
def test_key_file_mode_and_single_print(kg):
    old = os.umask(0)
    try:
        key = first_key(kg)
    finally:
        os.umask(old)
    path = kg.dir / 'first.key'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.read_text() == key + '\n'
    shown = kg.capsys.readouterr()
    assert key not in shown.out and key not in shown.err
    assert str(path) in shown.out  # it says where the key is, never what it is
    # an existing key file is never overwritten, and the failed run leaves the policy as it was
    before = kg.policy.read_text()
    assert kg.run('--principal', 'other', '--scopes', 'audio', '--max-jobs-per-day', '1', '--max-concurrent', '1',
                  '--expires-days', '1', key_file=path) != 0
    assert path.read_text() == key + '\n' and kg.policy.read_text() == before
    # no key reaches the policy lock, the rotation record or a temporary file either
    for entry in kg.dir.iterdir():
        if entry.is_file() and entry != path:
            assert key not in entry.read_text()


# spec: ANYM-684
def test_rotation_overlaps_and_retire_removes_only_the_old_hash(kg):
    old = first_key(kg)
    other = first_key(kg, 'deploy-probe', 'probe.key')
    other_doc = [p for p in json.loads(kg.policy.read_text())['principals'] if p['id'] == 'deploy-probe']
    kg.clock[0] += 10 * 86400
    assert kg.run('--rotate', '--principal', 'messaging-enrichment', '--expires-days', '365', key_file=kg.dir / 'new.key') == 0
    new = (kg.dir / 'new.key').read_text().strip()
    assert new != old and stat.S_IMODE((kg.dir / 'new.key').stat().st_mode) == 0o600
    assert kg.state.authenticates(old, kg.clock[0]) and kg.state.authenticates(new, kg.clock[0])  # both work
    entries = json.loads(kg.policy.read_text())['principals'][0]['keys']
    assert len(entries) == 2 and old not in kg.policy.read_text() and new not in kg.policy.read_text()
    revision = json.loads(kg.policy.read_text())['revision']
    new_expiry = entries[1]['expires_at']
    # a third key is refused: at most two overlap
    assert kg.run('--rotate', '--principal', 'messaging-enrichment', '--expires-days', '365', key_file=kg.dir / 'third.key') != 0
    assert kg.run('--confirm-readback', readback_line(new_expiry), '--principal', 'messaging-enrichment') == 0
    assert kg.run('--retire-old', '--principal', 'messaging-enrichment') == 0
    doc = json.loads(kg.policy.read_text())
    assert doc['revision'] > revision
    assert [e['sha256'] for e in doc['principals'][0]['keys']] == [hashlib.sha256(new.encode()).hexdigest()]
    assert not kg.state.authenticates(old, kg.clock[0]) and kg.state.authenticates(new, kg.clock[0])
    assert [p for p in doc['principals'] if p['id'] == 'deploy-probe'] == other_doc
    assert kg.state.authenticates(other, kg.clock[0]) == 'deploy-probe'


# spec: ANYM-684
def test_failed_delivery_leaves_the_old_key_working(kg):
    old = first_key(kg)
    kg.clock[0] += 10 * 86400
    assert kg.run('--rotate', '--principal', 'messaging-enrichment', '--expires-days', '365', key_file=kg.dir / 'new.key') == 0
    # the copy to production failed: nobody confirms; the old key still authenticates and nothing retires it
    before = kg.policy.read_text()
    assert kg.state.authenticates(old, kg.clock[0]) == 'messaging-enrichment'
    assert kg.run('--retire-old', '--principal', 'messaging-enrichment') != 0
    assert kg.policy.read_text() == before


# spec: ANYM-684
def test_retire_is_refused_until_the_workers_own_read_back_succeeds(kg):
    first_key(kg)
    # nothing to retire without a rotation
    assert kg.run('--retire-old', '--principal', 'messaging-enrichment') != 0
    old_expiry = json.loads(kg.policy.read_text())['principals'][0]['keys'][0]['expires_at']
    kg.clock[0] += 10 * 86400
    assert kg.run('--rotate', '--principal', 'messaging-enrichment', '--expires-days', '365', key_file=kg.dir / 'new.key') == 0
    new_expiry = json.loads(kg.policy.read_text())['principals'][0]['keys'][1]['expires_at']
    before = kg.policy.read_text()
    assert kg.run('--retire-old', '--principal', 'messaging-enrichment') != 0
    # read-backs that do not prove the NEW key reached the worker are not accepted: unreachable, no expiry (the
    # health-only probe), the OLD key's expiry (the worker still holds the old key), a made-up one
    for line in (readback_line(new_expiry, reachable='no'), 'inference mini=x reachable=yes status=ok queue=0/0/16 admission=open',
                 readback_line(old_expiry), readback_line('2031-01-01T00:00:00Z'), 'garbage', ''):
        assert kg.run('--confirm-readback', line, '--principal', 'messaging-enrichment') != 0, line
        assert kg.run('--retire-old', '--principal', 'messaging-enrichment') != 0
    assert kg.policy.read_text() == before
    # the confirmation names a principal that is not rotating: refused
    assert kg.run('--confirm-readback', readback_line(new_expiry), '--principal', 'deploy-probe') != 0
    assert kg.run('--confirm-readback', readback_line(new_expiry), '--principal', 'messaging-enrichment') == 0
    assert kg.run('--retire-old', '--principal', 'messaging-enrichment') == 0
    assert len(json.loads(kg.policy.read_text())['principals'][0]['keys']) == 1


# spec: ANYM-684
def test_concurrent_policy_writes_are_serialized(kg):
    names = [f'svc-{n}' for n in range(8)]

    def make(name):
        return keygen.main(['--policy', str(kg.policy), '--principal', name, '--scopes', 'audio', '--max-jobs-per-day', '5',
                            '--max-concurrent', '1', '--expires-days', '30', '--key-file', str(kg.dir / f'{name}.key')])

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        assert list(pool.map(make, names)) == [0] * 8
    doc = json.loads(kg.policy.read_text())
    assert sorted(p['id'] for p in doc['principals']) == names and doc['revision'] == 8
    # a writer waits for the lock of the one that holds it (the lock file is the one ANYM-682's writer takes)
    done = threading.Event()
    with keygen.policy_lock(kg.policy):
        worker = threading.Thread(target=lambda: (make('late'), done.set()))
        worker.start()
        assert not done.wait(0.5)
    worker.join(10)
    assert done.is_set() and json.loads(kg.policy.read_text())['revision'] == 9


# spec: ANYM-684
def test_keygen_refuses_a_symlinked_or_loose_policy(kg):
    first_key(kg)
    os.chmod(kg.policy, 0o644)
    assert kg.run('--principal', 'b', '--scopes', 'audio', '--max-jobs-per-day', '1', '--max-concurrent', '1',
                  '--expires-days', '1', key_file=kg.dir / 'b.key') != 0
    os.chmod(kg.policy, 0o600)
    real = kg.dir / 'real.json'
    shutil.move(kg.policy, real)
    kg.policy.symlink_to(real)
    assert kg.run('--principal', 'b', '--scopes', 'audio', '--max-jobs-per-day', '1', '--max-concurrent', '1',
                  '--expires-days', '1', key_file=kg.dir / 'b.key') != 0
    assert not (kg.dir / 'b.key').exists()


# ---------------------------------------------------------------- ANYM-691 criterion 5: pause ledger and recovery
PAUSE = load('inference_ops_pausestate', PACKAGE / 'pausestate.py')
GROUP_CODE = "import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\ntime.sleep(600)"


def members_of(pgid):
    return [row for row in PAUSE.process_table() if row['pgid'] == pgid and not row['stat'].startswith('Z')]  # a zombie is gone


def wait_for(condition, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture()
def groups():
    """Real process groups (own session: a leader and one helper). `make()` returns the stopped group's leader."""
    made = []

    def make(stopped=True):
        leader = subprocess.Popen([sys.executable, '-c', GROUP_CODE], start_new_session=True)
        made.append(leader)
        assert wait_for(lambda: len(members_of(leader.pid)) == 2), 'the helper did not start'
        if stopped:
            os.killpg(leader.pid, signal.SIGSTOP)
            assert wait_for(lambda: all(row['stat'].startswith('T') for row in members_of(leader.pid)))
        return leader

    yield make
    for leader in made:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(leader.pid, signal.SIGKILL)
        leader.wait()


def ledgered(tmp_path, leader, stopped_at=None):
    """A ledger naming `leader`'s group, written the way the service writes it: before the stop."""
    ledger = PAUSE.Ledger(tmp_path / 'state' / 'paused.json', instance='test')
    started = PAUSE.start_text(leader.pid)
    assert started
    ledger.record(leader.pid, leader.pid, started, stopped_at=time.time() if stopped_at is None else stopped_at)
    return ledger


# spec: ANYM-691
def test_recover_finds_a_stopped_helper_whose_leader_was_killed_and_leaves_foreign_groups_alone(tmp_path, groups):
    ours, foreign = groups(), groups()
    ledger = ledgered(tmp_path, ours)
    helper = next(row['pid'] for row in members_of(ours.pid) if row['pid'] != ours.pid)
    os.kill(ours.pid, signal.SIGKILL)  # the leader dies; its stopped helper keeps the group (and the memory) alive
    ours.wait()
    assert wait_for(lambda: [row['pid'] for row in members_of(ours.pid)] == [helper])
    report = PAUSE.recover(ledger.path)
    assert report['killed'] == [helper] and report['kept'] == []
    assert wait_for(lambda: members_of(ours.pid) == [])
    # a stopped group that is not in the ledger is not touched, whatever its uid and parent
    assert len(members_of(foreign.pid)) == 2 and all(row['stat'].startswith('T') for row in members_of(foreign.pid))
    assert not ledger.path.exists()  # nothing left to recover: the ledger goes away


# spec: ANYM-691
def test_recover_ignores_a_reused_pgid_that_started_after_the_stop(tmp_path, groups):
    stranger = groups()
    started = next(row['start'] for row in members_of(stranger.pid) if row['pid'] == stranger.pid)
    ledger = ledgered(tmp_path, stranger, stopped_at=started - 100)  # the entry's group stopped long before this one began
    report = PAUSE.recover(ledger.path)
    assert report['killed'] == [] and len(members_of(stranger.pid)) == 2
    assert not ledger.path.exists()  # no member matched: the entry is dropped, the process survives


# spec: ANYM-691
def test_recover_drops_an_entry_only_when_no_member_remains(tmp_path, groups):
    ours = groups()
    ledger = ledgered(tmp_path, ours)
    report = PAUSE.recover(ledger.path, send=lambda pid, number: None, sleep=lambda seconds: None)  # nothing dies
    assert report['kept'] == [ours.pid] and [g['pgid'] for g in ledger.groups()] == [ours.pid]
    PAUSE.recover(ledger.path)
    assert wait_for(lambda: members_of(ours.pid) == []) and not ledger.path.exists()
    # a process that is running again (state R/S) is not a member: only stopped ones are recovered
    running = groups(stopped=False)
    ledger = ledgered(tmp_path, running)
    assert PAUSE.recover(ledger.path)['killed'] == [] and len(members_of(running.pid)) == 2


# spec: ANYM-691
def test_ownership_cutoff_is_the_whole_second_of_the_stop_and_a_reused_leader_pid_matches_nothing():
    def table(*rows):
        return lambda: ''.join(f'{pid} {pgid} {stat} {when}\n' for pid, pgid, stat, when in rows)

    stop = time.mktime(time.strptime('Mon Nov 2 13:00:00 2026', PAUSE.START_FORMAT)) + 0.25
    group = {'pgid': 700, 'leader': 700, 'leader_start': 'Mon Nov 2 12:59:00 2026', 'stopped_at': stop}
    rows = PAUSE.process_table(table((701, 700, 'T', 'Mon Nov 2 13:00:00 2026'), (702, 700, 'T', 'Mon Nov 2 13:00:01 2026')))
    assert [row['pid'] for row in PAUSE.members(group, rows)] == [701]  # printed in the next second: started after the stop
    # the process that has the pgid as its pid started at another time than the leader did: the number was reused
    rows = PAUSE.process_table(table((700, 700, 'T', 'Mon Nov 2 12:00:00 2026'), (701, 700, 'T', 'Mon Nov 2 12:00:00 2026')))
    assert PAUSE.members(group, rows) == []
    rows = PAUSE.process_table(table((700, 700, 'T', 'Mon Nov 2 12:59:00 2026'), (701, 700, 'T', 'Mon Nov 2 12:59:30 2026')))
    assert [row['pid'] for row in PAUSE.members(group, rows)] == [700, 701]


# spec: ANYM-691
def test_recover_keeps_the_entry_of_a_continued_process_that_could_not_be_killed(tmp_path):
    stop = time.mktime(time.strptime('Mon Nov 2 13:00:00 2026', PAUSE.START_FORMAT))
    ledger = PAUSE.Ledger(tmp_path / 'state' / 'paused.json')
    ledger.record(700, 700, 'Mon Nov 2 12:59:00 2026', stopped_at=stop)
    state = {'stat': 'T'}

    def send(pid, number):
        if number == signal.SIGCONT:
            state['stat'] = 'S'  # it runs again, so it no longer looks stopped ...
        else:
            raise PermissionError  # ... but it could not be killed

    report = PAUSE.recover(ledger.path, run_ps=lambda: f"700 700 {state['stat']} Mon Nov 2 12:59:00 2026\n", send=send,
                           sleep=lambda seconds: None)
    assert report['kept'] == [700] and [g['pgid'] for g in ledger.groups()] == [700]


# spec: ANYM-691
def test_ledger_is_written_atomically_validated_and_private(tmp_path):
    path = tmp_path / 'state' / 'paused.json'
    ledger = PAUSE.Ledger(path, instance='abc', clock=lambda: 1000.0)
    assert ledger.groups() == []
    ledger.record(40, 40, 'Mon Nov  2 13:00:00 2026')
    ledger.record(41, 41, 'Mon Nov  2 13:00:01 2026')
    ledger.record(40, 40, 'Mon Nov  2 13:00:00 2026', stopped_at=2000.0)  # the same group again replaces its entry
    assert [(g['pgid'], g['stopped_at']) for g in ledger.groups()] == [(41, 1000.0), (40, 2000.0)]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and not list(path.parent.glob('.*.part'))
    ledger.remove(41)
    ledger.remove(999)
    assert [g['pgid'] for g in ledger.groups()] == [40]
    ledger.remove(40)
    ledger.clear_if_empty()
    assert not path.exists()
    for bad in ('not json', '{}', '{"version": 2, "groups": []}', '{"version": 1, "groups": [{"pgid": 1}]}',
                '{"version": 1, "groups": [{"pgid": 1, "leader": 1, "leader_start": "x", "stopped_at": 1}]}',
                '{"version": 1, "groups": [{"pgid": "9", "leader": 9, "leader_start": "x", "stopped_at": 1}]}'):
        path.write_text(bad)
        with pytest.raises(PAUSE.LedgerError):
            ledger.groups()
        with pytest.raises(PAUSE.LedgerError):
            PAUSE.recover(path)
    # a failed write leaves the previous ledger untouched and no partial file behind
    path.write_text(json.dumps({'version': 1, 'instance': 'x', 'groups': []}))
    blocked = PAUSE.Ledger(tmp_path / 'state' / 'missing' / 'x' / 'paused.json')
    (tmp_path / 'state' / 'missing').write_text('a file where a directory should be')
    with pytest.raises(PAUSE.LedgerError):
        blocked.record(40, 40, 'Mon Nov  2 13:00:00 2026')


# spec: ANYM-691
def test_ledger_never_writes_or_reads_through_a_symlink(tmp_path):
    # the engines can write the state directory and recovery runs unsandboxed: a planted link must not redirect a write
    state = tmp_path / 'state'
    state.mkdir()
    victim = tmp_path / 'config.json'
    victim.write_text('protected')
    (state / '.paused.json.part').symlink_to(victim)  # the old predictable temporary name
    (state / 'paused.json').symlink_to(victim)
    ledger = PAUSE.Ledger(state / 'paused.json')
    with pytest.raises(PAUSE.LedgerError):  # a ledger that is a link is refused, not followed
        ledger.groups()
    with pytest.raises(PAUSE.LedgerError):
        PAUSE.recover(state / 'paused.json')
    assert victim.read_text() == 'protected'
    (state / 'paused.json').unlink()
    ledger.record(40, 40, 'Mon Nov 2 13:00:00 2026', stopped_at=1000.0)
    assert victim.read_text() == 'protected' and not (state / 'paused.json').is_symlink()
    assert [g['pgid'] for g in ledger.groups()] == [40]


# spec: ANYM-691
def test_process_table_reads_ps_and_rejects_garbage():
    rows = PAUSE.process_table(lambda: '  501     1 Ss   Mon Nov  2 13:00:00 2026\n  600   600 T+   Mon Nov  2 13:05:09 2026\n\n')
    assert [(r['pid'], r['pgid'], r['stat']) for r in rows] == [(501, 1, 'Ss'), (600, 600, 'T+')]
    assert rows[1]['start'] - rows[0]['start'] == 309 and PAUSE.start_text(600, lambda: '600 600 T Mon Nov  2 13:05:09 2026\n') == 'Mon Nov 2 13:05:09 2026'
    assert PAUSE.start_text(7, lambda: '600 600 T Mon Nov  2 13:05:09 2026\n') is None
    for bad in ('garbage\n', '600 600 T Mon Nov 99 13:05:09 2026\n'):
        with pytest.raises(PAUSE.LedgerError):
            PAUSE.process_table(lambda bad=bad: bad)


# ---------------------------------------------------------------- criterion 7: deploy and rollback

class FakeMini:
    """A mini that drains: after SIGTERM the service runs `jobs` more ticks of its sleep and then exits 0."""

    def __init__(self, jobs=0, running=True, current=None, previous=None, lock_path=None, stale_release=False,
                 health_failures=0, never_exits=False, respawn=False, gap=0, work_on_respawn=0, capable=(), ledger=False,
                 recover_fails=False, unreadable=()):
        self.log, self.jobs, self.running, self.draining = [], jobs, running, False
        self.current, self.previous = current, previous
        self.releases = {r for r in (current, previous) if r}
        self.served = current if running else None
        self.stale_release, self.never_exits = stale_release, never_exits
        self.health_failures, self.lock_path = health_failures, lock_path
        self.killed, self.respawn, self.pid, self.terms = False, respawn, 100, 0
        self.loaded, self.gap, self.respawn_in, self.work_on_respawn, self.unloading = running, gap, 0, work_on_respawn, False
        self.now = 0.0
        self.capable, self.ledger, self.recover_fails, self.unreadable = set(capable), ledger, recover_fails, set(unreadable)

    # the injected sleep and clock
    def sleep(self, seconds):
        self.now += seconds
        self.log.append('sleep')
        if (self.draining or self.unloading) and self.running and not self.never_exits:
            self.jobs -= 1
            if self.jobs <= 0:
                self.exit_service(0 if not self.respawn else 3)

    def clock(self):
        return self.now

    @contextlib.contextmanager
    def hold_lock(self):
        handle = open(self.lock_path, 'a')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise deploy.Busy('another deploy holds the lock')
        try:
            yield
        finally:
            handle.close()

    def current_release(self):
        return self.current

    def previous_release(self):
        return self.previous

    def has_release(self, sha):
        return sha in self.releases

    def upload(self, sha, archive):
        self.log.append(('upload', sha))
        self.releases.add(sha)

    def sync(self, sha):
        self.log.append(('sync', sha))

    def exit_service(self, code):
        # launchd KeepAlive {SuccessfulExit: false}: exit 0 stays down; a non-zero exit restarts the OLD release, after
        # `gap` polls in which nothing runs (the throttle interval), possibly with work that arrived meanwhile
        self.running, self.draining = False, False
        if code != 0 and self.loaded:
            self.respawn_in = self.gap + 1

    def respawn_now(self):
        self.running, self.pid, self.jobs, self.draining = True, self.pid + 1, self.work_on_respawn, False
        self.respawn = False  # launchd restarts the old release once in these scenarios

    def service_pid(self):
        if self.respawn_in:
            self.respawn_in -= 1
            if self.respawn_in == 0 and self.loaded:
                self.respawn_now()
        return self.pid if self.running else None

    def service_loaded(self):
        return self.loaded

    def unload(self):
        # bootout --wait: the job is removed at once (no more restarts); the process it holds drains on its own time,
        # which the caller must wait out before it touches `current`
        self.log.append('unload')
        if self.respawn_in and self.loaded:  # the restart landed before bootout did
            self.respawn_now()
        self.loaded, self.respawn_in = False, 0
        if self.running and self.jobs <= 0:
            self.running = False
        self.unloading = self.running

    def term(self):
        self.log.append('term')
        self.terms += 1
        self.draining = True
        if self.jobs <= 0:
            self.exit_service(0)

    def kill(self):
        self.log.append('kill')
        self.killed = True
        self.exit_service(9)

    def recover_capable(self, sha):
        if sha in self.unreadable:  # a release with the command whose --check fails (exit 4)
            raise deploy.MiniError('recover --check failed: the ledger is malformed')
        return sha in self.capable  # a release that has the `recover` command (ANYM-691)

    def ledger_exists(self):
        return self.ledger

    def recover(self, sha):
        assert not self.running and not self.loaded, 'recovering while the job can still run'
        self.log.append(('recover', sha))
        if self.recover_fails:
            raise deploy.MiniError('recover failed: a group remains')

    def point(self, sha):
        assert not self.running and not self.loaded, 'switching current while the old release can still run'
        self.log.append(('point', sha))
        if self.current and self.current != sha:
            self.previous = self.current
        self.current = sha

    def start(self):
        self.log.append('start')
        self.loaded, self.running, self.draining = True, True, False
        self.served = self.previous if self.stale_release else self.current

    def health(self):
        if self.health_failures > 0:
            self.health_failures -= 1
            raise deploy.MiniError('connection refused')
        return {'status': 'ok', 'queue': {'depth': 0, 'active': 0, 'capacity': 16}, 'admission': 'closed'}

    def models(self):
        return {'data': [], 'release': self.served, 'key_expires_at': '2027-01-01T00:00:00Z'}


def run_deploy(mini, sha=SHA, *, verifier=lambda sha: {'sha': sha}, **options):
    out = []
    runner = deploy.Deploy(mini, verifier=verifier, archive=lambda sha: b'tar', sleep=mini.sleep, clock=mini.clock,
                           out=out.append, **options)
    return runner, out


def green_get(sha=SHA, *, compare='identical', ahead=0, gate='success', verify='success', base=None):
    run = {'id': 7, 'run_attempt': 1, 'head_sha': sha, 'head_branch': 'main', 'event': 'push', 'status': 'completed',
           'conclusion': 'success', 'html_url': 'https://example.invalid/run/7',
           'head_repository': {'full_name': production.REPOSITORY}}
    jobs = [{'id': 1, 'name': 'CI gate', 'status': 'completed', 'conclusion': gate},
            {'id': 2, 'name': 'checks / verify', 'status': 'completed', 'conclusion': verify}]

    def get(path):
        if path.startswith('compare/main...'):
            return {'status': compare, 'ahead_by': ahead, 'merge_base_commit': {'sha': base or sha}}
        if path.startswith('actions/workflows/ci.yml/runs'):
            return {'total_count': 1, 'workflow_runs': [run]}
        if '/jobs' in path:
            return {'total_count': len(jobs), 'jobs': jobs}
        raise AssertionError(path)

    return get


# spec: ANYM-684
@pytest.mark.parametrize('case', ['unmerged', 'red-gate', 'red-verify', 'not-a-sha', 'not-main-head'])
def test_deploy_refuses_a_red_or_unmerged_sha(case):
    sha, get = SHA, None
    if case == 'unmerged':
        get = green_get(compare='ahead', ahead=2)
    elif case == 'red-gate':
        get = green_get(gate='failure')
    elif case == 'red-verify':
        get = green_get(verify='failure')
    elif case == 'not-a-sha':
        sha, get = 'main', green_get()
    else:
        get = green_get(base='c' * 40)
    with pytest.raises(deploy.Refused):
        deploy.verify_green(sha, get=get)
    # the deploy stops before it touches the mini: no lock, no upload, no signal
    mini = FakeMini(current=SHA2, lock_path=None)
    runner, _ = run_deploy(mini, sha, verifier=lambda s: deploy.verify_green(s, get=get))
    with pytest.raises(deploy.Refused):
        runner.deploy(sha)
    assert mini.log == [] and mini.current == SHA2
    # and the green case passes
    assert deploy.verify_green(SHA, get=green_get())['sha'] == SHA


# spec: ANYM-684
def test_deploy_drains_by_sigterm_and_does_not_kill_running_work(tmp_path):
    mini = FakeMini(jobs=4, current=SHA2, lock_path=tmp_path / 'lock')
    runner, out = run_deploy(mini)
    runner.deploy(SHA)
    log = mini.log
    assert log.count('term') == 1 and 'kill' not in log and not mini.killed and mini.pid == 100
    # new code is on the mini and synced while the old release still serves; then drain, switch, start
    order = [entry for entry in log if entry != 'sleep']
    assert order == [('upload', SHA), ('sync', SHA), 'term', 'unload', ('point', SHA), 'start']
    assert log.index('term') < log.index(('point', SHA)) and log.count('sleep') >= 3  # it waited for the jobs
    # the service's own drain limit ended in exit 3 and launchd restarted the old release: that fresh process is
    # drained too (it has no jobs) before the switch
    mini = FakeMini(jobs=2, current=SHA2, lock_path=tmp_path / 'lock', respawn=True)
    runner, _ = run_deploy(mini)
    runner.deploy(SHA)
    assert mini.terms == 2 and not mini.killed and mini.current == SHA
    assert [e for e in mini.log if e != 'sleep'] == [('upload', SHA), ('sync', SHA), 'term', 'term', 'unload', ('point', SHA), 'start']
    # launchd restarts the old release only after a gap in which nothing runs, and it takes work meanwhile: the poll that
    # sees no process must not be taken for the end. The job is unloaded (bootout drains what the restart took) and only
    # then is `current` switched.
    mini = FakeMini(jobs=1, current=SHA2, lock_path=tmp_path / 'lock', respawn=True, gap=3, work_on_respawn=6)
    runner, _ = run_deploy(mini)
    runner.deploy(SHA)
    assert mini.current == SHA and not mini.killed
    unloaded, switched = mini.log.index('unload'), mini.log.index(('point', SHA))
    assert unloaded < switched < mini.log.index('start')
    assert mini.log[unloaded:switched].count('sleep') >= 5  # it waited for the restarted release to finish its work
    # a restarted release that took work gets the same drain limit as the first process, not a short grace
    mini = FakeMini(jobs=1, current=SHA2, lock_path=tmp_path / 'lock', respawn=True, gap=0, work_on_respawn=20)
    runner, _ = run_deploy(mini, drain_limit=120)
    runner.deploy(SHA)
    assert mini.terms == 2 and mini.current == SHA and not mini.killed
    # a service that does not finish within the limit is not killed, and nothing is switched or started
    mini = FakeMini(jobs=1, current=SHA2, lock_path=tmp_path / 'lock', never_exits=True)
    runner, _ = run_deploy(mini, drain_limit=120)
    with pytest.raises(deploy.DrainTimeout):
        runner.deploy(SHA)
    assert not mini.killed and 'start' not in mini.log and 'unload' not in mini.log and ('point', SHA) not in mini.log and mini.current == SHA2
    # --force kills after the limit and goes on
    mini = FakeMini(jobs=1, current=SHA2, lock_path=tmp_path / 'lock', never_exits=True)
    runner, _ = run_deploy(mini, drain_limit=120, force=True)
    runner.deploy(SHA)
    assert mini.killed and mini.log.index('kill') < mini.log.index(('point', SHA)) < mini.log.index('start')
    assert mini.log.count('term') == 2 and 'unload' in mini.log  # launchd restarted the killed old release; drained, unloaded


# spec: ANYM-684
def test_deploy_fails_when_the_running_release_is_not_the_deployed_sha(tmp_path):
    mini = FakeMini(current=SHA2, lock_path=tmp_path / 'lock', stale_release=True)
    runner, out = run_deploy(mini)
    with pytest.raises(deploy.DeployError, match='release'):
        runner.deploy(SHA)
    # health that never answers is a failure too, with the way back named
    mini = FakeMini(current=SHA2, lock_path=tmp_path / 'lock', health_failures=10 ** 6)
    runner, out = run_deploy(mini)
    with pytest.raises(deploy.DeployError, match='--rollback'):
        runner.deploy(SHA)
    # admission open or closed is reported, not required
    mini = FakeMini(current=SHA2, lock_path=tmp_path / 'lock', health_failures=2)
    runner, out = run_deploy(mini)
    runner.deploy(SHA)
    assert any('admission=closed' in line and SHA in line for line in out)


# spec: ANYM-684
def test_deploy_switches_current_and_keeps_the_previous_release(tmp_path):
    mini = FakeMini(current=SHA2, lock_path=tmp_path / 'lock')
    runner, _ = run_deploy(mini)
    runner.deploy(SHA)
    assert mini.current == SHA and mini.previous == SHA2 and SHA2 in mini.releases
    # deploying the release that already runs does not turn `previous` into the same release
    runner, _ = run_deploy(mini)
    runner.deploy(SHA)
    assert mini.current == SHA and mini.previous == SHA2
    # a release already on the mini is not unpacked and synced again
    assert [e for e in mini.log if isinstance(e, tuple) and e[0] == 'upload'] == [('upload', SHA)]


# spec: ANYM-684
def test_first_install_stages_a_release_without_touching_the_service(tmp_path):
    mini = FakeMini(running=False, current=None, lock_path=tmp_path / 'lock')
    runner, _ = run_deploy(mini)
    runner.stage(SHA)
    assert mini.log == [('upload', SHA), ('sync', SHA), ('point', SHA)] and mini.current == SHA
    # a mini that already has a release is not re-staged, and a red SHA is refused before anything happens
    runner, _ = run_deploy(mini)
    with pytest.raises(deploy.DeployError, match='current'):
        runner.stage(SHA2)
    mini = FakeMini(running=False, current=None, lock_path=tmp_path / 'lock')
    runner, _ = run_deploy(mini, verifier=lambda sha: (_ for _ in ()).throw(deploy.Refused('red')))
    with pytest.raises(deploy.Refused):
        runner.stage(SHA)
    assert mini.log == []


# spec: ANYM-684
def test_rollback_repoints_current(tmp_path):
    mini = FakeMini(jobs=2, current=SHA, previous=SHA2, lock_path=tmp_path / 'lock')
    runner, _ = run_deploy(mini)
    runner.rollback()
    assert mini.current == SHA2 and mini.previous == SHA
    order = [entry for entry in mini.log if entry != 'sleep']
    assert order == ['term', 'unload', ('point', SHA2), 'start'] and 'kill' not in mini.log
    # no previous release: refused, nothing signalled
    mini = FakeMini(current=SHA, previous=None, lock_path=tmp_path / 'lock')
    runner, _ = run_deploy(mini)
    with pytest.raises(deploy.DeployError, match='previous'):
        runner.rollback()
    assert mini.log == []


# spec: ANYM-691
def test_deploy_runs_recover_after_unload_and_before_switching_current_also_on_rollback(tmp_path):
    mini = FakeMini(jobs=2, current=SHA2, lock_path=tmp_path / 'lock', capable={SHA, SHA2})
    runner, out = run_deploy(mini)
    runner.deploy(SHA)
    order = [entry for entry in mini.log if entry != 'sleep']
    assert order[order.index('unload'):] == ['unload', ('recover', SHA2), ('point', SHA), 'start']  # the release being left first
    assert any('recovering stopped engine groups' in line for line in out)
    mini = FakeMini(jobs=2, current=SHA, previous=SHA2, lock_path=tmp_path / 'lock2', capable={SHA, SHA2})
    runner, _ = run_deploy(mini)
    runner.rollback()
    order = [entry for entry in mini.log if entry != 'sleep']
    assert order == ['term', 'unload', ('recover', SHA), ('point', SHA2), 'start']


# spec: ANYM-691
def test_deploy_from_a_release_without_recover_uses_the_target_release(tmp_path):
    mini = FakeMini(jobs=1, current=SHA2, lock_path=tmp_path / 'lock', capable={SHA})  # SHA2 is the release before the feature
    runner, _ = run_deploy(mini)
    runner.deploy(SHA)
    assert [entry for entry in mini.log if isinstance(entry, tuple) and entry[0] == 'recover'] == [('recover', SHA)]
    assert mini.current == SHA


# spec: ANYM-691
def test_deploy_with_no_capable_release_and_no_ledger_continues_and_with_a_ledger_fails_closed(tmp_path):
    mini = FakeMini(jobs=1, current=SHA2, lock_path=tmp_path / 'lock')
    runner, out = run_deploy(mini)
    runner.deploy(SHA)
    assert mini.current == SHA and not any(isinstance(e, tuple) and e[0] == 'recover' for e in mini.log)
    assert any('recovery skipped' in line for line in out)
    mini = FakeMini(jobs=1, current=SHA2, lock_path=tmp_path / 'lock2', ledger=True)
    runner, _ = run_deploy(mini)
    with pytest.raises(deploy.DeployError, match='pause ledger'):
        runner.deploy(SHA)
    assert mini.current == SHA2 and not {'term', 'unload', 'start'} & set(mini.log) and mini.running  # refused before the drain


# spec: ANYM-691
def test_rollback_to_a_release_without_the_feature_recovers_from_the_leaving_release(tmp_path):
    mini = FakeMini(jobs=1, current=SHA, previous=SHA2, lock_path=tmp_path / 'lock', capable={SHA})
    runner, _ = run_deploy(mini)
    runner.rollback()
    assert [entry for entry in mini.log if isinstance(entry, tuple) and entry[0] == 'recover'] == [('recover', SHA)]
    assert mini.current == SHA2 and mini.log[-1] == 'start'


# spec: ANYM-691
@pytest.mark.parametrize('action', ['deploy', 'rollback'])
def test_deploy_refuses_to_start_when_recover_fails(tmp_path, action):
    mini = FakeMini(jobs=1, current=SHA, previous=SHA2, lock_path=tmp_path / 'lock', capable={SHA, SHA2}, recover_fails=True)
    runner, _ = run_deploy(mini)
    with pytest.raises(deploy.DeployError, match='nothing was switched') as caught:
        runner.rollback() if action == 'rollback' else runner.deploy(SHA2)
    assert mini.current == SHA and not any(e == ('point', SHA2) for e in mini.log)  # the target is never promoted
    # the unchanged release is started again, so a failed recovery does not strand the service
    assert mini.log[-2:] == [('recover', SHA), 'start'] and mini.running and mini.served == SHA
    assert 'serving again' in str(caught.value)
    # a service that was not running before is not started by the failure path
    mini = FakeMini(running=False, current=SHA, previous=SHA2, lock_path=tmp_path / 'lock2', capable={SHA, SHA2}, recover_fails=True)
    runner, _ = run_deploy(mini)
    with pytest.raises(deploy.DeployError, match='was not running before'):
        runner.rollback() if action == 'rollback' else runner.deploy(SHA2)
    assert 'start' not in mini.log and not mini.running


# spec: ANYM-691
@pytest.mark.parametrize('case', ['ledger-no-reader', 'unreadable-ledger'])
def test_deploy_checks_the_ledger_before_it_drains(tmp_path, case):
    # a ledger that no release can read (none has the command, or --check fails) stops the deploy while the service still serves
    options = {'ledger': True} if case == 'ledger-no-reader' else {'capable': {SHA, SHA2}, 'unreadable': {SHA2}}
    for action in ('deploy', 'rollback'):
        mini = FakeMini(jobs=1, current=SHA2, previous=SHA, lock_path=tmp_path / f'lock-{action}', **options)
        runner, _ = run_deploy(mini)
        with pytest.raises(deploy.DeployError, match='nothing was switched or stopped'):
            runner.rollback() if action == 'rollback' else runner.deploy(SHA)
        assert not {'term', 'unload', 'start'} & set(mini.log) and mini.running and mini.current == SHA2  # not drained or unloaded


# spec: ANYM-684
def test_two_deploys_cannot_run_at_once(tmp_path):
    first = FakeMini(current=SHA2, lock_path=tmp_path / 'lock')
    second = FakeMini(current=SHA2, lock_path=tmp_path / 'lock')
    runner, _ = run_deploy(second)
    with first.hold_lock():
        with pytest.raises(deploy.Busy):
            runner.deploy(SHA)
        with pytest.raises(deploy.Busy):
            runner.rollback()
    assert second.log == [] and second.current == SHA2
    runner.deploy(SHA)  # the lock is free again
    assert second.current == SHA
    # the real holder: the script the ssh runner starts on the mini takes an exclusive flock and says so
    lock = tmp_path / 'holder.lock'
    holders = []
    try:
        for _ in range(2):
            proc = subprocess.Popen([sys.executable, '-I', '-c', deploy.LOCK_HOLDER, str(lock)], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, text=True)
            holders.append(proc)
        answers = [(proc.stdout.readline().strip(), proc) for proc in holders]
        assert sorted(line for line, _ in answers) == ['busy', 'held']
        # closing the holder's stdin (the ssh session ended) frees the lock
        winner = next(proc for line, proc in answers if line == 'held')
        winner.stdin.close()
        assert winner.wait(10) == 0
        again = subprocess.run([sys.executable, '-I', '-c', deploy.LOCK_HOLDER, str(lock)], input='', capture_output=True,
                               text=True, timeout=10)
        assert again.stdout.strip() == 'held' and again.returncode == 0
    finally:
        for proc in holders:
            proc.kill()
            proc.wait()


class SshRecorder:
    """A fake ssh transport: records every command the deploy sends and answers like a mini that is up and drained."""

    def __init__(self):
        self.commands, self.state = [], {'current': 'releases/' + SHA2, 'previous': None, 'running': True, 'loaded': True}

    @contextlib.contextmanager
    def hold(self, command):
        self.commands.append(command)
        yield

    def run(self, command, stdin=None, timeout=None):
        self.commands.append(command)
        if 'sh -c' not in command and command.rstrip().endswith('.ready') and ' test -f ' in command:
            return 1, ''
        if 'readlink' in command and 'sh -c' not in command:
            return (0, self.state['current'] + '\n') if '/current' in command else (1, '')
        if 'launchctl print' in command:
            if not self.state['loaded']:
                return 113, 'Could not find service "com.deedkit.inference" in domain for system'
            return 0, ('pid = 4242\nstate = running\n' if self.state['running'] else 'state = not running\n')
        if 'launchctl bootout' in command:
            self.state['running'], self.state['loaded'] = False, False
        if 'launchctl bootstrap' in command:
            self.state['running'], self.state['loaded'] = True, True
        if 'launchctl kill' in command:
            self.state['running'] = False
        if 'launchctl kickstart' in command:
            self.state['running'] = True
        if '/health' in command:
            return 0, json.dumps({'status': 'ok', 'queue': {'depth': 0, 'active': 0, 'capacity': 16}, 'admission': 'open'})
        if 'probe.key' in command and ' -I ' in command:
            return 0, json.dumps({'data': [], 'release': SHA, 'key_expires_at': '2027-01-01T00:00:00Z'})
        return 0, ''

    def exercise_deploy(self):
        mini = deploy.SshMini(self, home='/Users/_deedkitinf')
        runner = deploy.Deploy(mini, verifier=lambda sha: {'sha': sha}, archive=lambda sha: b'tar', sleep=lambda s: None,
                               clock=iter(range(0, 10_000, 5)).__next__, out=lambda line: None)
        runner.deploy(SHA)
        return self.commands


# spec: ANYM-684
def test_deploy_never_touches_models_policy_or_ledger():
    recorder = SshRecorder()
    commands = recorder.exercise_deploy()
    text = '\n'.join(commands)
    for forbidden in ('models', 'policy.json', 'busy.json', '.created', 'ledger', 'rm -r', 'prune', 'chmod', 'chown', 'tailscale'):
        assert forbidden not in text, forbidden
    allowed = re.compile(r'/Users/_deedkitinf/(releases(/[0-9a-f]{40}(/[\w./-]*)?|/\.deploy\.lock|/\.uv-cache(/[\w./-]*)?)?|current(\.new)?|previous|probe\.key|state(/paused\.json)?)$')  # state/: ANYM-691 recovery only
    paths = re.findall(r'(?<!HOME=)/Users/_deedkitinf[\w./-]*', text)  # HOME=<home> is uv's environment, not a path touched
    assert paths and all(allowed.match(path) for path in paths), [p for p in paths if not allowed.match(p)]
    # uv runs unsandboxed as the service user: empty environment, no configuration file of that user's home
    sync = next(c for c in commands if 'uv sync' in c)
    assert 'env -i' in sync and 'UV_NO_CONFIG=1' in sync and 'UV_CACHE_DIR=/Users/_deedkitinf/releases/.uv-cache' in sync
    assert any('launchctl bootout --wait' in c for c in commands)
    # the key for the read-back travels as a file read on the mini, never as a command-line argument
    assert not re.search(r'Bearer', text.replace("Authorization", "")) or 'probe.key' in text
    # and the rollback touches the same few paths
    recorder = SshRecorder()
    recorder.state['previous'] = 'releases/' + SHA
    mini = deploy.SshMini(recorder, home='/Users/_deedkitinf')
    mini.point(SHA)
    assert all(allowed.match(p) for p in re.findall(r'/Users/_deedkitinf[\w./-]*', '\n'.join(recorder.commands)))


# ---------------------------------------------------------------- criterion 8: probe from production

class Handler(http.server.BaseHTTPRequestHandler):
    routes = {}
    seen = []

    def do_GET(self):
        Handler.seen.append((self.path, self.headers.get('Authorization')))
        status, body = Handler.routes.get(self.path, (404, b'{}'))
        if body == 'hang':
            time.sleep(5)
            return
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def tls_server(tmp_path):
    openssl = shutil.which('openssl')
    if not openssl:
        pytest.skip('openssl is needed for the throwaway certificate')
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    made = subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert),
                           '-days', '2', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                          capture_output=True, text=True)
    if made.returncode:
        pytest.skip('openssl cannot make the certificate: ' + made.stderr[-200:])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Handler.routes, Handler.seen = {}, []
    trusted = ssl.create_default_context(cafile=str(cert))
    yield server, f'https://localhost:{server.server_address[1]}', trusted
    server.shutdown()
    server.server_close()


def health_body(**extra):
    return json.dumps({'status': 'ok', 'queue': {'depth': 3, 'active': 1, 'capacity': 16}, 'admission': 'open', **extra}).encode()


# spec: ANYM-684
@pytest.mark.parametrize('failure', ['refused', 'timeout', 'bad-certificate', 'garbage-body', 'server-error', 'not-json-object'])
def test_probe_prints_one_line_and_exits_zero_on_every_failure(failure, tls_server, capsys):
    server, url, trusted = tls_server
    context = trusted
    if failure == 'refused':
        url = 'https://127.0.0.1:1'
    elif failure == 'timeout':
        Handler.routes['/health'] = (200, 'hang')
    elif failure == 'bad-certificate':
        context = ssl.create_default_context()  # does not trust the throwaway certificate
        Handler.routes['/health'] = (200, health_body())
    elif failure == 'garbage-body':
        Handler.routes['/health'] = (200, b'\x00\xff<html>')
    elif failure == 'server-error':
        Handler.routes['/health'] = (503, b'{"error": {"code": "draining"}}')
    else:
        Handler.routes['/health'] = (200, b'[1, 2]')
    code = probe.main(['--url', url, '--timeout', '1'], context=context)
    out = capsys.readouterr().out
    assert code == 0 and out.count('\n') == 1 and out.startswith('inference ')
    assert 'reachable=no' in out or failure == 'server-error', out
    assert len(out) < 300
    if failure == 'bad-certificate':
        assert 'bad_certificate' in out
    if failure == 'timeout':
        assert 'timeout' in out
    # usage errors and a missing url exit 0 as well: the probe can never block a deploy
    assert probe.main([]) == 0 and probe.main(['--nonsense']) == 0 and capsys.readouterr().out.count('\n') == 2


# spec: ANYM-684
def test_probe_prints_days_left(tls_server, tmp_path, capsys):
    server, url, trusted = tls_server
    key = 'k' * 48
    key_file = tmp_path / 'inference.key'
    key_file.write_text(key + '\n')
    expires = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + 40 * 86400 + 600))
    Handler.routes['/health'] = (200, health_body())
    Handler.routes['/v1/models'] = (200, json.dumps({'data': [], 'key_expires_at': expires, 'release': SHA}).encode())
    assert probe.main(['--url', url, '--key-file', str(key_file)], context=trusted) == 0
    out = capsys.readouterr().out
    assert out.count('\n') == 1 and 'reachable=yes' in out and 'status=ok' in out and 'queue=3/1/16' in out
    assert 'admission=open' in out and 'key_days_left=40' in out and f'key_expires={expires}' in out
    assert key not in out and ('/v1/models', 'Bearer ' + key) in Handler.seen
    # health alone (no key file) prints no key fields and never calls /v1/models
    Handler.seen.clear()
    assert probe.main(['--url', url], context=trusted) == 0
    out = capsys.readouterr().out
    assert 'key_days_left' not in out and all(path != '/v1/models' for path, _ in Handler.seen)
    # an unreadable key file or a refused key is reported on the same line; health is still printed
    assert probe.main(['--url', url, '--key-file', str(tmp_path / 'missing')], context=trusted) == 0
    out = capsys.readouterr().out
    assert 'reachable=yes' in out and 'key=unreadable' in out and out.count('\n') == 1
    Handler.routes['/v1/models'] = (401, b'{"error": {"code": "unauthorized"}}')
    assert probe.main(['--url', url, '--key-file', str(key_file)], context=trusted) == 0
    assert 'key=rejected' in capsys.readouterr().out
