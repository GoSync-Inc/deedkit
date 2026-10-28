"""The host backup runner's calendar profile (ANYM-608): no application container, locks against deploys."""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from messaging_service import backup_runner
from messaging_service.backup_receiver import AGE_HEADER
from messaging_service.backup_runner import ID, Runner, read_json, write_json

ROOT = Path(__file__).resolve().parents[1]
OWNED = 'deedkit-calendar-backup-'
TRANSFER = {'host': 'backup.example.invalid', 'user': 'calbackup', 'identity_file': '/private/delivery_ed25519',
            'known_hosts_file': '/private/known_hosts'}


class CalendarDocker:
    """Docker as a calendar run may see it: its own worker, and an application container it must never touch.

    ``mode``: ``encrypt`` (the worker captures, encrypts, drops the plaintext and exits), ``hold`` (still
    encrypting: plaintext and a partial age temporary), ``encrypted`` (result written, the worker not yet exited)
    or ``fail`` (the worker exits 1)."""
    def __init__(self, root):
        self.root, self.calls, self.mode = root, [], 'encrypt'
        self.containers = {'calendar-app': {'Id': 'calendar-app', 'Config': {'Labels': {
            'com.docker.compose.project': 'deedkit-calendar', 'com.docker.compose.service': 'calendar'}},
            'State': {'Running': True, 'Status': 'running', 'ExitCode': 0}}}
    def running_collectors(self, project):
        self.calls.append(('ps', project))
        return [{'id': 'calendar-app', 'service': 'calendar'}]
    def inspect(self, cid):
        self.calls.append(('inspect', cid))
        return copy.deepcopy(self.containers.get(cid))
    def call(self, *args):
        self.calls.append(args)
        if args[0] == 'create':
            name, labels = args[args.index('--name') + 1], {}
            for i, arg in enumerate(args):
                if arg == '--label':
                    key, value = args[i + 1].split('=', 1)
                    labels[key] = value
            self.containers[name] = {'Id': name, 'Config': {'Labels': labels},
                                     'State': {'Running': False, 'Status': 'created', 'ExitCode': 0}}
        elif args[0] == 'start':
            info = self.containers[args[1]]
            info['State'].update(Running=True, Status='running')
            work = self.root / info['Config']['Labels']['personal-os.backup.id']
            assert read_json(work / 'worker.json')['profile'] == 'calendar'
            (work / 'snapshot').mkdir()
            (work / 'snapshot' / 'key').write_bytes(b'SYNTHETIC VAULT KEY')
            if self.mode == 'fail':
                info['State'].update(Running=False, Status='exited', ExitCode=1)
                return ''
            write_json(work / 'captured.json', {'id': work.name, 'status': 'capture_complete'})
            if self.mode == 'hold':
                (work / '.encrypt-synthetic').write_bytes(b'partial ciphertext fixture')
                return ''
            data = AGE_HEADER + b'calendar ciphertext fixture' * 20
            shutil.rmtree(work / 'snapshot')
            (work / 'bundle.tar.age').write_bytes(data)
            write_json(work / 'result.json', {'v': 1, 'id': work.name, 'profile': 'calendar', 'status': 'encrypted',
                                              'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                                              'captured_at': '2026-10-28T00:00:00+00:00'})
            if self.mode == 'encrypt':
                info['State'].update(Running=False, Status='exited', ExitCode=0)
        elif args[0] == 'stop':
            self.containers[args[-1]]['State'].update(Running=False, Status='exited', ExitCode=143)
        elif args[0] == 'rm':
            self.containers.pop(args[1])
        return ''
    def touched(self):
        """Every container name a call named, except `create`'s image."""
        names = set()
        for args in self.calls:
            if args[0] == 'create':
                names.add(args[args.index('--name') + 1])
            elif args[0] in ('inspect', 'start', 'rm'):
                names.add(args[1])
            elif args[0] == 'stop':
                names.add(args[-1])
            else:
                names.add(args[0])
        return names


def calendar_cfg(tmp_path, **overrides):
    return {'profile': 'calendar', 'directory': str(tmp_path / 'calendar'), 'project': 'deedkit-calendar',
            'network': 'deedkit-calendar_backend', 'image': 'backup:test', 'dsn_file': '/private/calendar.dsn',
            'recipient_file': '/private/calendar.recipient',
            'state_mounts': {'vault': '/private/calendar/service/vault', 'profile': '/private/calendar/service'},
            'transfer': dict(TRANSFER), 'timeout_seconds': 60, 'keep_local': 1,
            'host_lock': str(tmp_path / 'messaging' / '.runner.lock'), **overrides}


def calendar_runner(tmp_path, send=None):
    cfg = calendar_cfg(tmp_path)
    docker = CalendarDocker(Path(cfg['directory']))
    sent = []
    async def stored(path, result, transfer, timeout):
        assert not (path.parent / 'snapshot').exists()
        sent.append((result['id'], result['sha256']))
        return {**result, 'status': 'stored', 'duplicate': False}
    runner = Runner(cfg, docker=docker, sleep=lambda _: None, send=send or stored)
    return runner, docker, sent


def private_file(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


def no_docker(monkeypatch):
    """Any Docker or other subprocess call fails the test."""
    def refused(*args, **kwargs):
        raise AssertionError(f'unexpected subprocess {args}')
    monkeypatch.setattr(backup_runner.subprocess, 'run', refused)


def hold(path):
    Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def held(path):
    """True while another open file description holds `path` (what `flock --nonblock` reads)."""
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def run_main(monkeypatch, *argv):
    monkeypatch.setattr(sys, 'argv', ['backup_runner', *argv])
    monkeypatch.setattr(backup_runner.signal, 'signal', lambda *_: None)
    with pytest.raises(SystemExit) as stopped:
        backup_runner.main()
    return stopped.value.code


def fake_clock(monkeypatch):
    now, sleeps = [0.0], []
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    monkeypatch.setattr(backup_runner, 'time', SimpleNamespace(monotonic=lambda: now[0], sleep=sleep, time=lambda: now[0]))
    return sleeps


# spec: ANYM-608
@pytest.mark.parametrize('profile', [None, 'messaging'])
def test_the_messaging_example_config_loads_unchanged_with_or_without_a_profile(tmp_path, profile):
    data = json.loads((ROOT / 'deploy' / 'backup-runner.example.json').read_text())
    if profile:
        data['profile'] = profile
    assert backup_runner.config(private_file(tmp_path / 'runner.json', data)) == data


# spec: ANYM-608
def test_the_calendar_example_config_loads(tmp_path):
    data = json.loads((ROOT / 'deploy' / 'backup-runner-calendar.example.json').read_text())
    assert data['profile'] == 'calendar' and data['host_lock'] == '/opt/personal-os-probe/backups/.runner.lock'
    assert backup_runner.config(private_file(tmp_path / 'runner.json', data)) == data


# spec: ANYM-608
@pytest.mark.parametrize('change,code', [
    ({'files_volume': 'files'}, 'invalid_backup_config'),
    ({'mounts': []}, 'invalid_backup_config'),
    ({'recovery': {'accounts': [], 'sources': []}}, 'invalid_backup_config'),
    ({'dump_timeout_seconds': 1800}, 'invalid_backup_config'),
    ({'host_lock': None}, 'invalid_backup_config'),
    ({'state_mounts': None}, 'invalid_backup_config'),
    ({'delivery_mode': 'stream_v2'}, 'invalid_backup_config'),
    ({'profile': 'files'}, 'invalid_backup_config'),
    ({'host_lock': 'relative/.runner.lock'}, 'invalid_backup_path'),
    ({'host_lock': 'own'}, 'invalid_backup_path'),
    ({'state_mounts': {'vault': '/private/vault'}}, 'invalid_backup_mount'),
    ({'state_mounts': {'vault': '/private/vault', 'profile': '/private/../etc'}}, 'invalid_backup_mount'),
    ({'busy_wait_seconds': 7201}, 'invalid_backup_timeout'),
    ({'keep_local': 0}, 'invalid_backup_retention'),
])
def test_a_calendar_config_with_a_messaging_key_or_without_its_own_keys_is_refused(tmp_path, change, code):
    cfg = calendar_cfg(tmp_path)
    for key, value in change.items():
        if value is None:
            cfg.pop(key)
        elif value == 'own':
            cfg[key] = cfg['directory'] + '/.runner.lock'
        else:
            cfg[key] = value
    with pytest.raises(ValueError, match=code):
        backup_runner.config(private_file(tmp_path / 'runner.json', cfg))


# spec: ANYM-608
def test_a_calendar_staged_config_and_its_busy_wait_are_accepted(tmp_path):
    cfg = calendar_cfg(tmp_path, delivery_mode='staged_v1', busy_wait_seconds=600)
    assert backup_runner.config(private_file(tmp_path / 'runner.json', cfg)) == cfg


# spec: ANYM-608
def test_a_calendar_run_lists_stops_and_starts_no_application_container(tmp_path):
    runner, docker, sent = calendar_runner(tmp_path)
    runner.run()
    root = Path(runner.cfg['directory'])
    assert docker.containers['calendar-app']['State']['Running']
    assert not any(args[0] == 'ps' for args in docker.calls)
    assert all(name.startswith(OWNED) for name in docker.touched()), docker.touched()
    create = next(args for args in docker.calls if args[0] == 'create')
    mounts = [create[i + 1] for i, arg in enumerate(create) if arg == '--mount']
    assert not any('type=volume' in mount for mount in mounts)
    assert 'type=bind,src=/private/calendar/service/vault,dst=/sources/vault,readonly' in mounts
    assert 'type=bind,src=/private/calendar/service,dst=/sources/profile,readonly' in mounts
    assert create[create.index('--label') + 1].startswith('personal-os.backup.id=')
    assert 'personal-os.backup.project=deedkit-calendar' in create and create[-1] == 'backup:test'
    (job,) = [p for p in root.iterdir() if p.is_dir() and ID.fullmatch(p.name)]
    assert read_json(job / 'worker.json') == {
        'profile': 'calendar', 'id': job.name, 'dsn_file': '/run/secrets/database_url',
        'vault_directory': '/sources/vault', 'profile_directory': '/sources/profile',
        'recipient_file': '/run/secrets/recipient', 'work_directory': '/work'}
    assert not (job / 'recovery.json').exists() and not (job / 'snapshot').exists()
    status = read_json(root / 'status.json')
    assert status['state'] == 'ok' and status['last_success_id'] == job.name and sent == [(job.name, status['ciphertext_sha256'])]
    assert not (root / 'journal.json').exists()


# spec: ANYM-608
def test_a_calendar_journal_records_no_stopped_container(tmp_path):
    runner, docker, _ = calendar_runner(tmp_path)
    runner.prepare()
    journal = read_json(Path(runner.cfg['directory']) / 'journal.json')
    assert journal['stopped'] == [] and journal['container'] == OWNED + journal['id'].lower()
    assert docker.calls == []


# spec: ANYM-608
@pytest.mark.parametrize('field', ['stopped', 'container'])
def test_a_calendar_journal_naming_another_container_is_refused_before_any_docker_call(tmp_path, field):
    runner, docker, _ = calendar_runner(tmp_path)
    runner.prepare()
    journal = read_json(runner.journal_path)
    journal[field] = [{'id': 'calendar-app', 'service': 'calendar'}] if field == 'stopped' else 'calendar-app'
    write_json(runner.journal_path, journal)
    for action in (Runner(runner.cfg, docker=docker).recover_stop, Runner(runner.cfg, docker=docker).run):
        with pytest.raises(ValueError, match='invalid_backup_journal'):
            action()
    assert docker.calls == [] and docker.containers['calendar-app']['State']['Running']
    assert read_json(runner.journal_path) == journal


# spec: ANYM-608
def test_recovery_of_an_interrupted_calendar_capture_stops_and_removes_only_its_own_worker(tmp_path):
    runner, docker, sent = calendar_runner(tmp_path)
    docker.mode = 'hold'
    ticks = iter(range(0, 10 ** 6, 30))
    runner.clock = lambda: next(ticks)
    with pytest.raises(RuntimeError, match='backup_capture_timeout'):
        runner.run()
    job = Path(runner.cfg['directory']) / runner.journal['id']
    assert (job / 'snapshot' / 'key').is_file() and (job / '.encrypt-synthetic').is_file()
    docker.calls.clear()
    runner.recover_stop()
    container = OWNED + job.name.lower()
    assert [args for args in docker.calls if args[0] != 'inspect'] == [('stop', '--time', '25', container),
                                                                        ('rm', container)]
    assert docker.touched() == {container}
    assert not job.exists() and not runner.journal_path.exists() and sent == []
    status = read_json(runner.status_path)
    assert status['error_code'] == 'backup_capture_timeout' and status['abandoned_id'] == job.name
    assert 'abandoned_fence' not in status and docker.containers['calendar-app']['State']['Running']
    # The next run takes a new snapshot under a new ID.
    docker.mode, runner.clock = 'encrypt', backup_runner.time.time
    runner.run()
    assert len(sent) == 1 and sent[0][0] != job.name


# spec: ANYM-608
def test_a_calendar_capture_that_failed_is_abandoned_without_a_fence_action(tmp_path):
    runner, docker, sent = calendar_runner(tmp_path)
    docker.mode = 'fail'
    with pytest.raises(RuntimeError, match='backup_capture_failed'):
        runner.run()
    runner.recover_stop()
    assert all(name.startswith(OWNED) and 'fence' not in name for name in docker.touched())
    assert not runner.journal_path.exists() and sent == []
    assert not any(p.name == 'snapshot' for p in Path(runner.cfg['directory']).rglob('*'))


# spec: ANYM-608
def test_a_calendar_bundle_encrypted_before_the_interruption_is_delivered_under_the_same_id(tmp_path):
    runner, docker, sent = calendar_runner(tmp_path)
    docker.mode = 'encrypted'
    ticks = iter(range(0, 10 ** 6, 30))
    runner.clock = lambda: next(ticks)
    with pytest.raises(RuntimeError, match='backup_capture_timeout'):
        runner.run()
    first = runner.journal['id']
    runner.recover_stop()
    assert read_json(runner.journal_path)['phase'] == 'upload'
    job = Path(runner.cfg['directory']) / first
    assert (job / 'bundle.tar.age').is_file() and not (job / 'snapshot').exists()
    runner.clock = backup_runner.time.time
    runner.run()
    assert [item[0] for item in sent] == [first] and sum(args[0] == 'create' for args in docker.calls) == 1
    assert read_json(runner.status_path)['last_success_id'] == first


# spec: ANYM-608
def test_a_receipt_lost_after_the_receiver_stored_the_calendar_bundle_is_read_back_under_the_same_id(tmp_path):
    stored = []
    async def send(path, result, transfer, timeout):
        duplicate = result['id'] in stored
        stored.append(result['id'])
        return {**result, 'status': 'stored', 'duplicate': duplicate}
    runner, docker, _ = calendar_runner(tmp_path, send)
    finish = runner.finish
    def crash(work, result, receipt):
        raise RuntimeError('host_crash')  # the receiver committed; the host stops before it saves the receipt
    runner.finish = crash
    with pytest.raises(RuntimeError, match='host_crash'):
        runner.run()
    runner.finish = finish
    assert read_json(runner.journal_path)['phase'] == 'upload'
    runner.run()
    assert len(stored) == 2 and stored[0] == stored[1]
    assert read_json(runner.status_path)['last_success_id'] == stored[0]
    assert sum(args[0] == 'create' for args in docker.calls) == 1


# spec: ANYM-608
def test_a_calendar_bundle_refused_as_busy_waits_like_messaging(tmp_path):
    attempts = []
    async def send(path, result, transfer, timeout):
        attempts.append(result['id'])
        if len(attempts) == 1:
            raise RuntimeError('receiver_busy')
        return {**result, 'status': 'stored', 'duplicate': False}
    runner, _, _ = calendar_runner(tmp_path, send)
    waits = []
    runner.sleep = lambda seconds: waits.append(seconds) if seconds >= 1 else None
    runner.run()
    assert waits == [backup_runner.BUSY_RETRY_SECONDS] and attempts[0] == attempts[1]
    assert read_json(runner.status_path)['state'] == 'ok'


# spec: ANYM-608
def test_a_held_host_lock_makes_a_calendar_run_wait_then_end_host_lock_busy_without_a_journal(tmp_path, monkeypatch,
                                                                                             capsys):
    cfg = calendar_cfg(tmp_path)
    path = private_file(tmp_path / 'calendar.json', cfg)
    fd = hold(cfg['host_lock'])
    sleeps = fake_clock(monkeypatch)
    no_docker(monkeypatch)
    try:
        assert run_main(monkeypatch, '--config', str(path)) == 1
    finally:
        os.close(fd)
    assert backup_runner.HOST_LOCK_WAIT_SECONDS <= 5400
    assert backup_runner.HOST_LOCK_WAIT_SECONDS - max(sleeps) <= sum(sleeps) <= backup_runner.HOST_LOCK_WAIT_SECONDS
    root = Path(cfg['directory'])
    assert not (root / 'journal.json').exists() and not any(ID.fullmatch(p.name) for p in root.iterdir())
    assert read_json(root / 'status.json')['error_code'] == 'host_lock_busy'
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {'status': 'error', 'code': 'host_lock_busy'}
    assert not held(cfg['host_lock']) and not held(root / '.runner.lock')


# spec: ANYM-608
def test_a_recover_stop_waits_for_the_host_lock_and_keeps_the_journal_while_it_is_held(tmp_path, monkeypatch):
    runner, _, _ = calendar_runner(tmp_path)
    runner.prepare()
    before = runner.journal_path.read_bytes()
    path = private_file(tmp_path / 'calendar.json', runner.cfg)
    fd = hold(runner.cfg['host_lock'])
    fake_clock(monkeypatch)
    no_docker(monkeypatch)
    try:
        assert run_main(monkeypatch, '--config', str(path), '--recover-stop') == 1
    finally:
        os.close(fd)
    assert runner.journal_path.read_bytes() == before


# spec: ANYM-608
def test_a_calendar_run_holding_the_host_lock_excludes_a_second_run_messaging_and_autodeploy(tmp_path, monkeypatch):
    cfg = calendar_cfg(tmp_path)
    messaging = json.loads((ROOT / 'deploy' / 'backup-runner.example.json').read_text())
    messaging['directory'] = str(Path(cfg['host_lock']).parent)
    messaging_path = private_file(tmp_path / 'messaging.json', messaging)
    for directory in (cfg['directory'], messaging['directory']):
        Path(directory).mkdir(mode=0o700)
    no_docker(monkeypatch)
    with backup_runner.hold_locks(cfg):
        with pytest.raises(RuntimeError, match='backup_job_already_running'):
            with backup_runner.hold_locks(cfg):
                pass
        with pytest.raises(RuntimeError, match='backup_job_already_running'):
            with backup_runner.hold_locks(messaging):
                pass
        assert held(cfg['host_lock']) and held(Path(cfg['directory']) / '.runner.lock')
        assert run_main(monkeypatch, '--config', str(messaging_path)) == 1  # the messaging runner refuses
        if shutil.which('flock'):  # the autodeploy unit's wrapper (Linux CI)
            command = ['flock', '--nonblock', '--conflict-exit-code', '75', cfg['host_lock'], 'true']
            assert subprocess.call(command) == 75
    assert not held(cfg['host_lock']) and not held(Path(cfg['directory']) / '.runner.lock')


# spec: ANYM-608
def test_a_calendar_run_takes_its_own_lock_before_the_host_lock(tmp_path, monkeypatch):
    cfg = calendar_cfg(tmp_path)
    Path(cfg['directory']).mkdir(mode=0o700)
    fd = hold(Path(cfg['directory']) / '.runner.lock')
    try:
        with pytest.raises(RuntimeError, match='backup_job_already_running'):
            with backup_runner.hold_locks(cfg):
                pass
        assert not os.path.exists(cfg['host_lock']) or not held(cfg['host_lock'])
    finally:
        os.close(fd)


# spec: ANYM-608
def test_a_messaging_run_takes_only_its_own_lock(tmp_path):
    messaging = json.loads((ROOT / 'deploy' / 'backup-runner.example.json').read_text())
    messaging['directory'] = str(tmp_path / 'messaging')
    Path(messaging['directory']).mkdir(mode=0o700)
    with backup_runner.hold_locks(messaging):
        assert held(tmp_path / 'messaging' / '.runner.lock')
        assert os.listdir(tmp_path / 'messaging') == ['.runner.lock'] and 'host_lock' not in messaging


# spec: ANYM-608
@pytest.mark.parametrize('profile', ['messaging', 'calendar'])
def test_check_reports_an_unfinished_journal_without_docker_or_locks(tmp_path, monkeypatch, capsys, profile):
    if profile == 'calendar':
        cfg = calendar_cfg(tmp_path)
    else:
        cfg = json.loads((ROOT / 'deploy' / 'backup-runner.example.json').read_text())
        cfg['directory'] = str(tmp_path / 'messaging')
    path = private_file(tmp_path / 'runner.json', cfg)
    no_docker(monkeypatch)
    assert run_main(monkeypatch, '--config', str(path), '--check') == 0
    assert json.loads(capsys.readouterr().out) == {'status': 'idle', 'profile': profile}
    assert not os.path.exists(cfg['directory'])  # read-only: not even the directory is created
    Path(cfg['directory']).mkdir(mode=0o700)
    journal = {'id': '20261028T180000Z-0123456789abcdef', 'phase': 'capturing', 'created': 1.0,
               'container': 'pos-backup-20261028t180000z-0123456789abcdef',
               'stopped': [{'id': 'tg-id', 'service': 'telegram'}]}
    write_json(Path(cfg['directory']) / 'journal.json', journal)
    fd = hold(Path(cfg['directory']) / '.runner.lock')  # a run in progress does not block the check
    try:
        assert run_main(monkeypatch, '--config', str(path), '--check') == 3
    finally:
        os.close(fd)
    assert json.loads(capsys.readouterr().out) == {'status': 'unfinished', 'profile': profile, 'id': journal['id'],
                                                   'phase': 'capturing', 'stopped': 1}
    assert read_json(Path(cfg['directory']) / 'journal.json') == journal
