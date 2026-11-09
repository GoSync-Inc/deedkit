"""Calls profile bundle (ANYM-778): config, capped helper, writer bound, bundle format, failure isolation, no leakage.

Synthetic SQLite files only; no provider, no Docker (a fake runs the real helper script as a local process), no real age.
The limits of the helper's container are Docker's; the pressure test stands in for them with process limits.
"""
import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import resource
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time

from hypothesis import given, strategies as st
import pytest

from messaging_service import backup_runner, backup_worker
from messaging_service.backup import verify_bundle
from messaging_service.backup_calls import (CallsRefused, create_calls_snapshot, inspect_snapshot, row_text,
                                            valid_calls_member, verify_calls_bundle)
from messaging_service.backup_crypto import decrypt_snapshot, encrypt_snapshot, snapshot_profile
from messaging_service.backup_recordings import verify_recordings_bundle
from messaging_service.backup_runner import (CALLS_SNAPSHOT_HELPER, CALLS_STEP_PAGES, CALLS_STEP_PAUSE,
                                             CALLS_WRITER_BOUND_SECONDS, Runner, read_json)
from test_backup_recordings import NOW, TRANSFER, private_file, write_timers

CANARY = 'canary-deleted-row-5f1e'
TOKEN = 'synthetic-handshake-token-77ab'
CAPTURED = '2026-11-04T03:00:00+00:00'
CALLS_CONTAINER = 'deedkit-calls-calls-1'
IMAGE_A, IMAGE_B = 'sha256:' + 'a' * 64, 'sha256:' + 'b' * 64


def make_state(path, *, rows=3, bulk=0, canary=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    # Some SQLite builds default to secure_delete; the live service's freed pages are what the snapshot must drop.
    db.executescript('PRAGMA journal_mode=DELETE;PRAGMA secure_delete=OFF;'
                     'CREATE TABLE briefs(id TEXT PRIMARY KEY, body TEXT NOT NULL);'
                     'CREATE TABLE calls(id TEXT PRIMARY KEY, brief_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL);'
                     'CREATE TABLE voice_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, call_id TEXT, body TEXT NOT NULL);'
                     'CREATE TABLE outbound_attempts(id TEXT PRIMARY KEY, token TEXT);')
    db.executemany('INSERT INTO briefs VALUES(?,?)', [(f'b{i}', 'synthetic brief') for i in range(rows)])
    db.executemany('INSERT INTO voice_events(id,call_id,body) VALUES(?,?,?)',
                   [(f'e{i}', 'c0', 'synthetic ' + 'x' * bulk) for i in range(rows)])
    db.execute('INSERT INTO outbound_attempts VALUES(?,?)', ('a0', TOKEN))
    if canary:
        db.execute('INSERT INTO briefs VALUES(?,?)', ('deleted', CANARY))
        db.commit()
        db.execute("DELETE FROM briefs WHERE id='deleted'")
    db.commit()
    db.close()
    return path


def helper(source, staging, *, pages=CALLS_STEP_PAGES, pause=CALLS_STEP_PAUSE, busy_pause=.05, retries=20, restart_pause=.1,
           limit=120, prefix='', reserve=0, **popen):
    """The real helper script as a local process."""
    return subprocess.Popen([sys.executable, '-I', '-c', prefix + CALLS_SNAPSHOT_HELPER, str(source), str(staging), str(pages), str(pause),
                             str(busy_pause), str(retries), str(restart_pause), str(limit), str(limit + 5), str(reserve)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **popen)


def calls_cfg(tmp_path, **overrides):
    own = overrides.pop('calls', {})
    return {'profile': 'calls', 'directory': str(tmp_path / 'calls'), 'project': 'deedkit-calls', 'image': 'backup:test',
            'recipient_file': str(tmp_path / 'recipient'), 'transfer': dict(TRANSFER), 'timeout_seconds': 60, 'keep_local': 1,
            'host_lock': str(tmp_path / 'messaging' / '.runner.lock'),
            'calls': {'staging_directory': str(tmp_path / 'staging'), 'state_directory': str(tmp_path / 'state'),
                      'capture_timeout_seconds': 1200, 'data_mount': str(tmp_path / 'data'), 'calls_container': CALLS_CONTAINER,
                      'timers': [str(tmp_path / 'timers' / 'messaging.timer'), str(tmp_path / 'timers' / 'calendar.timer')], **own},
            **overrides}


@pytest.fixture
def age_stub(tmp_path):
    path = tmp_path / 'age-stub'
    path.write_text('#!' + sys.executable + '\nimport sys,shutil\n'
                    'source=open(sys.argv[-1],"rb") if "-d" in sys.argv else sys.stdin.buffer\n'
                    'shutil.copyfileobj(source,sys.stdout.buffer)\n')
    path.chmod(0o700)
    return str(path)


class CallsDocker:
    """Docker as a calls run may see it. `attach` runs the real helper; `start` runs the real worker in process."""
    def __init__(self, tmp_path, age_stub, monkeypatch):
        self.tmp_path, self.calls, self.containers = tmp_path, [], {}
        self.image: str | None = IMAGE_A
        self.attach_result: object = None  # a code to answer instead of running the helper; 'timeout' raises; 'stuck' never goes
        self.stuck = False
        self.inspect_hook = None
        monkeypatch.setattr(backup_worker, 'encrypt_snapshot', lambda *a, **k: self.encrypt(age_stub, *a, **k))
        self.encrypt_error = None
    def encrypt(self, age_stub, *args, **kwargs):
        if self.encrypt_error:
            raise self.encrypt_error
        return encrypt_snapshot(*args, age=age_stub, **kwargs)
    def running_collectors(self, project):
        raise AssertionError('a calls run lists no collectors')
    def image_of(self, name):
        self.calls.append(('image_of', name))
        return self.image
    def inspect(self, name, timeout=30):
        self.calls.append(('inspect', name))
        if self.inspect_hook:
            self.inspect_hook(timeout)
        return copy.deepcopy(self.containers.get(name))
    def call(self, *args, timeout=90):
        self.calls.append(args)
        if args[0] == 'create':
            name, labels = args[args.index('--name') + 1], {}
            for i, arg in enumerate(args):
                if arg == '--label':
                    key, value = args[i + 1].split('=', 1)
                    labels[key] = value
            self.containers[name] = {'Id': name, 'Args': args, 'Config': {'Labels': labels},
                                     'State': {'Running': False, 'Status': 'created', 'ExitCode': 0}}
        elif args[0] == 'start':
            self.run_worker(args[1])
        elif args[:2] == ('rm', '--force'):
            if self.stuck:
                raise RuntimeError('docker_operation_failed')
            self.containers.pop(args[2])
        elif args[0] == 'rm':
            self.containers.pop(args[1])
        elif args[0] == 'stop':
            self.containers[args[-1]]['State'].update(Running=False, Status='exited', ExitCode=143)
        return ''
    def attach(self, name, timeout):
        self.calls.append(('attach', name, timeout))
        info = self.containers[name]
        if self.attach_result == 'timeout':
            raise RuntimeError('calls_helper_timeout')
        if self.attach_result is not None:
            return self.attach_result
        args = info['Args']
        sources = [a.split(',')[1][4:] for i, a in enumerate(args) if args[i - 1] == '--mount']
        tail = args[args.index('-c') + 2:]
        tail = [str(Path(sources[0]) / 'voice.sqlite3'), sources[1], *tail[2:]]
        done = subprocess.run([sys.executable, '-I', '-c', CALLS_SNAPSHOT_HELPER, *tail], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
        assert CANARY.encode() not in done.stderr
        self.containers.pop(name)  # --rm
        return done.returncode, done.stdout.decode()
    def run_worker(self, name):
        info = self.containers[name]
        work = Path(self.tmp_path / 'calls' / info['Config']['Labels']['personal-os.backup.id'])
        config = read_json(work / 'worker.json')
        args = info['Args']
        source = next(a for a in args if isinstance(a, str) and a.endswith('dst=/sources/snapshot,readonly')).split(',')[1][4:]
        config.update(snapshot_directory=source, work_directory=str(work), recipient_file=str(self.tmp_path / 'recipient'))
        try:
            backup_worker.run(config)
            code = 0
        except Exception:
            code = 1
        info['State'].update(Running=False, Status='exited', ExitCode=code)


def calls_runner(tmp_path, age_stub, monkeypatch, send=None, cls=Runner):
    cfg = calls_cfg(tmp_path)
    Path(cfg['host_lock']).parent.mkdir(mode=0o700)
    (tmp_path / 'recipient').write_text('fake')
    write_timers(tmp_path)
    make_state(tmp_path / 'data' / 'voice.sqlite3')
    docker = CallsDocker(tmp_path, age_stub, monkeypatch)
    sent = []
    async def stored(path, result, transfer, timeout):
        restored = tmp_path / f'received-{len(sent)}'
        decrypt_snapshot(path, 'fake', restored, age=age_stub)
        sent.append((result, verify_calls_bundle(restored), restored))
        return {**result, 'status': 'stored', 'duplicate': False}
    elapsed = [0.0]
    def sleep(seconds):
        elapsed[0] += seconds
    runner = cls(cfg, docker=docker, sleep=sleep, send=send or stored, clock=lambda: NOW)
    runner.monotonic = lambda: elapsed[0]
    runner.elapsed = elapsed
    runner.container_uid, runner.container_gid = os.getuid(), os.getgid()
    return runner, docker, sent


def fail_and_recover(runner):
    with pytest.raises(BaseException) as failure:
        runner.run()
    runner.recover_stop()
    return failure.value


def status(runner):
    return read_json(runner.status_path)


# spec: ANYM-778
def test_a_valid_calls_config_loads_and_the_example_parses_with_the_real_config(tmp_path):
    cfg = calls_cfg(tmp_path, delivery_mode='staged_v1', busy_wait_seconds=600)
    assert backup_runner.config(private_file(tmp_path / 'runner.json', cfg)) == cfg
    example = json.loads((Path(__file__).resolve().parents[1] / 'deploy' / 'backup-runner-calls.example.json').read_text())
    assert example['transfer']['host'].endswith('.invalid')
    assert backup_runner.calls_config(copy.deepcopy(example)) == example


# spec: ANYM-778
@pytest.mark.parametrize('change,code', [
    ({'dsn_file': '/private/db.dsn'}, 'invalid_backup_config'),
    ({'network': 'backend'}, 'invalid_backup_config'),
    ({'recordings': {}}, 'invalid_backup_config'),
    ({'delivery_mode': 'stream_v2'}, 'invalid_backup_config'),
    ({'project': 'bad project'}, 'invalid_backup_config'),
    ({'image': 'bad image'}, 'invalid_backup_config'),
    ({'calls': None}, 'invalid_backup_config'),
    ({'calls.extra': 1}, 'invalid_backup_config'),
    ({'calls.staging_directory': None}, 'invalid_backup_config'),
    ({'calls.data_mount': None}, 'invalid_backup_config'),
    ({'calls.calls_container': None}, 'invalid_backup_config'),
    ({'calls.timers': None}, 'invalid_backup_config'),
    ({'calls.capture_timeout_seconds': None}, 'invalid_backup_config'),
    ({'calls.calls_container': 'bad name'}, 'invalid_backup_config'),
    ({'calls.calls_container': 'a' * 129}, 'invalid_backup_config'),
    ({'directory': 'relative'}, 'invalid_backup_path'),
    ({'calls.data_mount': '/with,comma'}, 'invalid_backup_path'),
    ({'calls.staging_directory': '{root}/calls'}, 'invalid_backup_path'),
    ({'calls.staging_directory': '{root}/calls/staging'}, 'invalid_backup_path'),
    ({'calls.staging_directory': '{root}/messaging'}, 'invalid_backup_path'),
    ({'calls.staging_directory': '{root}/state/inner'}, 'invalid_backup_path'),
    ({'calls.staging_directory': '{root}/timers'}, 'invalid_backup_path'),
    ({'calls.data_mount': '{root}/staging/data'}, 'invalid_backup_path'),
    ({'calls.data_mount': '{root}/calls'}, 'invalid_backup_path'),
    ({'recipient_file': '{root}/staging/recipient'}, 'invalid_backup_path'),
    ({'host_lock': '{root}/calls/.runner.lock'}, 'invalid_backup_path'),
    ({'calls.timers': []}, 'invalid_backup_path'),
    ({'calls.timers': ['{root}/a.service']}, 'invalid_backup_path'),
    ({'calls.timers': ['{root}/a.timer', '{root}/a.timer']}, 'invalid_backup_path'),
    ({'calls.timers': ['{root}/state/a.timer']}, 'invalid_backup_path'),
    ({'transfer': {'host': 'x'}}, 'invalid_backup_transfer'),
    ({'timeout_seconds': 59}, 'invalid_backup_timeout'),
    ({'timeout_seconds': True}, 'invalid_backup_timeout'),
    ({'busy_wait_seconds': 7201}, 'invalid_backup_timeout'),
    ({'calls.capture_timeout_seconds': 959}, 'invalid_backup_timeout'),
    ({'calls.capture_timeout_seconds': 7201}, 'invalid_backup_timeout'),
    ({'keep_local': 2}, 'invalid_backup_retention'),
    ({'keep_local': True}, 'invalid_backup_retention'),
])
def test_a_calls_config_outside_the_rules_is_refused_with_its_fixed_code(tmp_path, change, code):
    cfg = calls_cfg(tmp_path)
    for key, value in change.items():
        if isinstance(value, str):
            value = value.replace('{root}', str(tmp_path))
        elif isinstance(value, list):
            value = [v.replace('{root}', str(tmp_path)) for v in value]
        if key.startswith('calls.'):
            name = key.split('.', 1)[1]
            if value is None:
                cfg['calls'].pop(name)
            else:
                cfg['calls'][name] = value
        else:
            cfg[key] = value
    with pytest.raises(ValueError, match=code):
        backup_runner.config(private_file(tmp_path / 'runner.json', cfg))


# spec: ANYM-778
def test_the_helper_argv_has_every_limit_no_network_and_a_read_only_state_mount(tmp_path, age_stub, monkeypatch):
    runner, docker, _ = calls_runner(tmp_path, age_stub, monkeypatch)
    runner.run()
    create = next(c for c in docker.calls if c[0] == 'create' and '--init' in c)
    text = ' '.join(create[:create.index('-c')])
    for flag in ('--rm --init', '--network none', '--read-only', '--cap-drop ALL', '--security-opt no-new-privileges',
                 '--memory 64m', '--memory-swap 64m', '--pids-limit 16', '--cpus 0.5', '--user 10001:10001'):
        assert flag in text
    assert f'type=bind,src={tmp_path}/data,dst=/data,readonly' in create
    staging_mount = next(a for a in create if a.endswith('dst=/staging'))
    assert 'readonly' not in staging_mount and '/staging/' in staging_mount
    assert sum(a == '--mount' for a in create) == 2 and IMAGE_A in create
    # The calls container is only read: its image, nothing else.
    assert all(CALLS_CONTAINER not in c or c == ('image_of', CALLS_CONTAINER) for c in docker.calls)


# spec: ANYM-778
def test_the_image_is_resolved_again_at_every_run(tmp_path, age_stub, monkeypatch):
    runner, docker, sent = calls_runner(tmp_path, age_stub, monkeypatch)
    runner.run()
    docker.image = IMAGE_B
    runner.run()
    helpers = [c for c in docker.calls if c[0] == 'create' and '--init' in c]
    assert [IMAGE_A in helpers[0], IMAGE_B in helpers[1]] == [True, True] and len(sent) == 2


# spec: ANYM-778
def test_an_absent_calls_container_ends_with_its_code_and_starts_nothing(tmp_path, age_stub, monkeypatch):
    runner, docker, sent = calls_runner(tmp_path, age_stub, monkeypatch)
    docker.image = None
    assert str(fail_and_recover(runner)) == 'no_calls_container'
    assert status(runner)['error_code'] == 'no_calls_container'
    assert not [c for c in docker.calls if c[0] in ('create', 'start', 'attach')] and not sent


# spec: ANYM-778
def test_a_timeout_kills_the_helper_and_reads_back_before_staging_is_cleaned(tmp_path, age_stub, monkeypatch):
    seen = []
    class Watching(Runner):
        def clean_staging(self):
            seen.append({n for n in self.docker.containers if n.startswith(backup_runner.CALLS_HELPER)})
            super().clean_staging()
    runner, docker, _ = calls_runner(tmp_path, age_stub, monkeypatch, cls=Watching)
    docker.attach_result = 'timeout'
    assert str(fail_and_recover(runner)) == 'calls_helper_timeout'
    assert seen[0] == set() and len(seen) >= 2  # the first clean is the new job's, before the helper exists
    assert all(s == set() for s in seen) and any(c[:2] == ('rm', '--force') for c in docker.calls)
    assert not list(runner.staging_root.iterdir())


# spec: ANYM-778
def test_a_helper_not_verified_gone_keeps_staging_and_the_recovery_looks_again(tmp_path, age_stub, monkeypatch):
    runner, docker, _ = calls_runner(tmp_path, age_stub, monkeypatch)
    docker.attach_result, docker.stuck = 'timeout', True
    with pytest.raises(RuntimeError, match='calls_helper_unverified'):
        runner.run()
    assert list(runner.staging_root.iterdir()) and read_json(runner.journal_path)['helper']
    with pytest.raises(RuntimeError, match='calls_helper_unverified'):
        runner.recover_stop()
    assert list(runner.staging_root.iterdir())
    docker.stuck = False
    runner.recover_stop()
    assert not list(runner.staging_root.iterdir()) and not runner.journal_path.exists()


# spec: ANYM-778
def test_a_helper_never_removes_a_container_it_does_not_own(tmp_path, age_stub, monkeypatch):
    runner, docker, _ = calls_runner(tmp_path, age_stub, monkeypatch)
    runner.journal = {'id': '20261103T120000Z-0123456789abcdef', 'helper': 'x'}
    name = backup_runner.CALLS_HELPER + runner.journal['id'].lower()
    docker.containers[name] = {'Config': {'Labels': {'personal-os.backup.id': 'another'}}, 'State': {'Running': True}}
    with pytest.raises(RuntimeError, match='calls_helper_unverified'):
        runner.reap_helper()
    assert name in docker.containers


# spec: ANYM-778
def test_the_reap_budget_is_elapsed_time_and_every_docker_call_gets_only_what_is_left(tmp_path, age_stub, monkeypatch):
    runner, docker, _ = calls_runner(tmp_path, age_stub, monkeypatch)
    runner.journal = {'id': '20261103T120000Z-0123456789abcdef', 'helper': 'x'}
    given = []
    def timing_out(timeout):
        given.append(timeout)
        runner.elapsed[0] += timeout
        raise subprocess.TimeoutExpired('docker', timeout)
    docker.inspect_hook = timing_out
    with pytest.raises(RuntimeError, match='calls_helper_unverified'):
        runner.reap_helper()
    assert given == [backup_runner.REAP_SECONDS] and runner.elapsed[0] <= backup_runner.REAP_SECONDS + 1


# spec: ANYM-778
def test_a_full_disk_on_the_journal_does_not_stop_the_cleanup_that_frees_it(tmp_path, age_stub, monkeypatch):
    runner, docker, _ = calls_runner(tmp_path, age_stub, monkeypatch)
    docker.attach_result = 'timeout'
    docker.stuck = True
    with pytest.raises(RuntimeError, match='calls_helper_unverified'):
        runner.run()
    assert list(runner.staging_root.iterdir())
    docker.stuck = False
    real = backup_runner.write_json
    def full(path, data):
        raise OSError(28, 'no space')
    monkeypatch.setattr(backup_runner, 'write_json', full)
    with pytest.raises(OSError):
        runner.recover_stop()
    assert not list(runner.staging_root.iterdir())
    monkeypatch.setattr(backup_runner, 'write_json', real)
    runner.recover_stop()
    assert not runner.journal_path.exists()


# spec: ANYM-778
def test_admission_budgets_from_the_live_file_with_its_free_pages(tmp_path, age_stub, monkeypatch):
    runner, docker, _ = calls_runner(tmp_path, age_stub, monkeypatch)
    live = tmp_path / 'data' / 'voice.sqlite3'
    os.truncate(live, 2 * 1024**3)
    floor = backup_runner.CALLS_DEFAULTS['min_free_bytes']
    class Usage:
        free = floor + 3 * 2 * 1024**3
    runner.disk = lambda path: Usage
    assert str(fail_and_recover(runner)) == 'calls_skipped_disk'
    assert not [c for c in docker.calls if c[0] == 'create']


# spec: ANYM-778
def test_the_helper_stops_when_staging_falls_below_its_reserve(tmp_path):
    source = make_state(tmp_path / 'voice.sqlite3')
    staging = tmp_path / 'staging'
    staging.mkdir()
    process = helper(source, staging, reserve=2**62)
    output, _ = process.communicate(timeout=60)
    assert process.returncode == 3 and json.loads(output) == {'error': 'no_space'}


# spec: ANYM-778
def test_the_snapshot_drops_deleted_content_passes_integrity_and_leaves_one_file(tmp_path):
    source = make_state(tmp_path / 'data' / 'voice.sqlite3')
    assert CANARY.encode() in source.read_bytes()
    staging = tmp_path / 'staging'
    staging.mkdir(mode=0o700)
    process = helper(source, staging)
    output, _ = process.communicate(timeout=60)
    assert process.returncode == 0 and json.loads(output) == {'status': 'ok'}
    assert [p.name for p in staging.iterdir()] == ['voice.sqlite3']
    assert CANARY.encode() not in (staging / 'voice.sqlite3').read_bytes()
    assert CANARY.encode() in source.read_bytes()
    db = sqlite3.connect(staging / 'voice.sqlite3')
    assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    assert db.execute('SELECT token FROM outbound_attempts').fetchall() == [(TOKEN,)]


# spec: ANYM-778
@pytest.mark.parametrize('source_kind,code', [('absent', 'no_database'), ('not_a_database', 'corrupt')])
def test_the_helper_answers_one_fixed_category(tmp_path, source_kind, code):
    source = tmp_path / 'voice.sqlite3'
    if source_kind == 'not_a_database':
        source.write_bytes(b'not sqlite ' * 1000)
    staging = tmp_path / 'staging'
    staging.mkdir()
    process = helper(source, staging)
    output, _ = process.communicate(timeout=60)
    assert process.returncode == 3 and json.loads(output) == {'error': code}


# spec: ANYM-778
def test_the_helper_gives_up_on_a_held_lock_after_its_bounded_retries(tmp_path):
    source = make_state(tmp_path / 'voice.sqlite3')
    staging = tmp_path / 'staging'
    staging.mkdir()
    holder = sqlite3.connect(source, isolation_level=None)
    holder.execute('BEGIN EXCLUSIVE')
    try:
        process = helper(source, staging, busy_pause=.01, retries=5)
        output, _ = process.communicate(timeout=60)
    finally:
        holder.execute('ROLLBACK')
        holder.close()
    assert process.returncode == 3 and json.loads(output) == {'error': 'busy'}


def writer_loop(path, stop, times, errors, gap=.05, limit=None):
    """A committed writer in one transaction per pair: a torn copy could not keep the two tables equal."""
    db = sqlite3.connect(path, timeout=5)
    db.execute('PRAGMA synchronous=OFF')  # the bound is what the copy adds, not this machine's fsync
    count = 0
    try:
        while not stop.is_set() and (limit is None or count < limit):
            started = time.perf_counter()
            db.execute('INSERT INTO voice_events(id,call_id,body) VALUES(?,?,?)', (f'w{count}', 'c0', 'synthetic'))
            db.execute('INSERT INTO briefs VALUES(?,?)', (f'w{count}', 'synthetic'))
            db.commit()
            times.append(time.perf_counter() - started)
            count += 1
            time.sleep(gap)
    except Exception as exc:
        errors.append(exc)
    finally:
        db.close()


# spec: ANYM-778
def test_a_writer_commits_within_the_bound_during_the_copy_and_the_copy_is_one_point_in_time(tmp_path):
    assert CALLS_STEP_PAGES <= 64
    source = make_state(tmp_path / 'data' / 'voice.sqlite3', rows=5200, bulk=4000, canary=False)
    assert source.stat().st_size >= 20 * 1024**2
    staging = tmp_path / 'staging'
    staging.mkdir(mode=0o700)
    stop, times, errors = threading.Event(), [], []
    process = helper(source, staging)
    thread = threading.Thread(target=writer_loop, args=(source, stop, times, errors), kwargs={'limit': 40})
    thread.start()
    try:
        while not times and thread.is_alive():
            time.sleep(.005)
        assert process.poll() is None, 'the first commit must land during the copy'
        thread.join(timeout=60)
        output, _ = process.communicate(timeout=120)
    finally:
        stop.set()
        thread.join(timeout=10)
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert not errors and len(times) == 40 and max(times) <= CALLS_WRITER_BOUND_SECONDS
    assert process.returncode == 0, output
    facts = inspect_snapshot(staging / 'voice.sqlite3')
    assert facts['tables']['voice_events']['rows'] - 5200 == facts['tables']['briefs']['rows'] - 5200
    assert facts['voice_events_max_seq'] == facts['tables']['voice_events']['rows']
    db = sqlite3.connect(staging / 'voice.sqlite3')
    assert db.execute('SELECT MAX(seq) FROM voice_events').fetchone()[0] == facts['voice_events_max_seq']
    assert db.execute('SELECT COUNT(*) FROM briefs').fetchone()[0] == facts['tables']['briefs']['rows']


# spec: ANYM-778
def test_a_writer_that_never_pauses_ends_the_copy_at_its_deadline_and_keeps_writing(tmp_path):
    source = make_state(tmp_path / 'voice.sqlite3', rows=1500, bulk=4000, canary=False)
    staging = tmp_path / 'staging'
    staging.mkdir()
    stop, times, errors = threading.Event(), [], []
    thread = threading.Thread(target=writer_loop, args=(source, stop, times, errors), kwargs={'gap': .01})
    thread.start()
    try:
        process = helper(source, staging, limit=3, restart_pause=.05)
        output, _ = process.communicate(timeout=60)
    finally:
        stop.set()
        thread.join(timeout=30)
    assert process.returncode == 3 and json.loads(output) == {'error': 'timeout'} and not errors


PRESSURE = {
    'cpu': (resource.RLIMIT_CPU, (1, 1), 'import threading\n'
            'threading.Thread(target=lambda: [None for _ in iter(int, 1)], daemon=True).start()\n'),
    'memory': (resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2), 'import os, threading, time\n'
               'def _hog():\n'
               '    held = []\n'
               '    time.sleep(.3)\n'
               '    try:\n'
               '        while True:\n'
               '            held.append(bytearray(8 * 1024**2))\n'
               '            time.sleep(.01)\n'
               '    except MemoryError:\n'
               '        os._exit(137)\n'
               'threading.Thread(target=_hog, daemon=True).start()\n'),
}


# spec: ANYM-778
@pytest.mark.parametrize('kind', sorted(PRESSURE))
def test_under_pressure_the_helper_is_killed_by_its_own_limit_and_the_writer_stays_within_the_bound(tmp_path, kind):
    if kind == 'memory' and not sys.platform.startswith('linux'):
        pytest.skip('RLIMIT_AS is enforced on Linux only; CI runs it')
    limit, values, prefix = PRESSURE[kind]
    source = make_state(tmp_path / 'voice.sqlite3', rows=5200, bulk=4000, canary=False)
    staging = tmp_path / 'staging'
    staging.mkdir()
    stop, times, errors = threading.Event(), [], []
    process = helper(source, staging, prefix=prefix, preexec_fn=lambda: resource.setrlimit(limit, values))
    thread = threading.Thread(target=writer_loop, args=(source, stop, times, errors))
    thread.start()
    try:
        process.communicate(timeout=120)
        died_after = len(times)
        time.sleep(.3)
    finally:
        stop.set()
        thread.join(timeout=30)
    assert process.returncode != 0 and not (staging / 'voice.sqlite3').exists()
    assert not errors and len(times) > died_after and max(times) <= CALLS_WRITER_BOUND_SECONDS
    assert sqlite3.connect(source).execute('PRAGMA integrity_check').fetchall() == [('ok',)]


def docker_ready():
    if not sys.platform.startswith('linux') or not shutil.which('docker'):
        return False
    return subprocess.run(['docker', 'info'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode == 0


@pytest.fixture
def real_helper(tmp_path, age_stub, monkeypatch):
    """The shipped `docker create` arguments on the calls image's own base: limits, user 10001, read-only root."""
    if not docker_ready():
        pytest.skip('needs Docker on Linux; CI runs it')
    dockerfile = (Path(__file__).resolve().parents[1] / 'services/calls/Dockerfile').read_text()
    image = re.findall(r'^FROM (python:\S+)', dockerfile, re.M)[-1]
    subprocess.run(['docker', 'pull', '-q', image], check=True, stdout=subprocess.DEVNULL, timeout=300)
    runner, _, _ = calls_runner(tmp_path, age_stub, monkeypatch)
    runner.journal = {'id': '20261103T120000Z-' + os.urandom(8).hex()}
    staging = tmp_path / 'job'
    staging.mkdir()
    # User 10001 is not this test's user: open permissions stand in for the host's ownership, which the runner checks.
    os.chmod(staging, 0o777)
    os.chmod(tmp_path / 'data', 0o777)
    name = backup_runner.CALLS_HELPER + runner.journal['id'].lower()
    def create(prefix='', keep=False):
        args = runner.helper_args(name, image, staging)
        if keep:  # the one change: the container stays, so its OOMKilled state can be read
            args.remove('--rm')
        args[args.index('-c') + 1] = prefix + args[args.index('-c') + 1]
        real = backup_runner.Docker()
        real.call(*args)
        return real, name, staging, image
    yield create
    subprocess.run(['docker', 'rm', '--force', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


def capture_running(real, name, staging, timeout):
    """Start the helper, and return once its copy exists on the staging mount: a writer started then overlaps it."""
    outcome = {}
    def attach():
        outcome['result'] = real.attach(name, timeout)
    thread = threading.Thread(target=attach)
    thread.start()
    limit = time.monotonic() + 60
    while not (staging / 'voice.copy.sqlite3').exists():
        assert thread.is_alive() and time.monotonic() < limit, 'the copy never started'
        time.sleep(.01)
    return thread, outcome


# spec: ANYM-778
def test_in_the_real_container_a_writer_stays_within_the_bound_and_the_snapshot_is_whole(tmp_path, real_helper):
    source = tmp_path / 'data' / 'voice.sqlite3'
    source.unlink()
    make_state(source, rows=5200, bulk=4000)
    db = sqlite3.connect(source)
    db.execute('CREATE INDEX synthetic_bodies ON voice_events(body)')  # a real temporary-file demand for VACUUM INTO
    db.commit()
    db.close()
    os.chmod(source, 0o666)
    real, name, staging, image = real_helper()
    stop, times, errors = threading.Event(), [], []
    capture, outcome = capture_running(real, name, staging, 300)
    full_size = source.stat().st_size
    thread = threading.Thread(target=writer_loop, args=(source, stop, times, errors), kwargs={'limit': 40})
    thread.start()
    try:
        while not times and thread.is_alive():
            time.sleep(.005)
        # A finished online backup is at least the source's size; integrity check and vacuum come only after it.
        assert capture.is_alive() and (staging / 'voice.copy.sqlite3').stat().st_size < full_size, 'the first commit must land during the copy'
        thread.join(timeout=60)
        capture.join(timeout=300)
    finally:
        stop.set()
        thread.join(timeout=60)
    code, output = outcome['result']
    assert (code, json.loads(output)) == (0, {'status': 'ok'}) and not errors and len(times) == 40
    assert max(times) <= CALLS_WRITER_BOUND_SECONDS
    os.chmod(source, 0o644)
    check = ('import sqlite3,sys;d=sqlite3.connect("file:/s/voice.sqlite3?mode=ro",uri=True);'
             'print(d.execute("PRAGMA integrity_check").fetchone()[0],'
             'd.execute("SELECT COUNT(*) FROM voice_events").fetchone()[0]-d.execute("SELECT COUNT(*) FROM briefs").fetchone()[0],'
             'd.execute("SELECT COUNT(*) FROM briefs WHERE id LIKE \'w%\'").fetchone()[0])')
    done = subprocess.run(['docker', 'run', '--rm', '--pull', 'never', '--network', 'none', '--user', '10001:10001', '--mount',
                           f'type=bind,src={staging},dst=/s,readonly', image, 'python', '-I', '-c', check],
                          stdout=subprocess.PIPE, check=True, timeout=120)
    integrity, difference, written = done.stdout.split()
    # A writer's row in the snapshot: that commit came before the online backup's final pass, which then restarted.
    assert (integrity, difference) == (b'ok', b'0') and int(written) >= 1
    assert sorted(p.name for p in staging.iterdir()) == ['voice.sqlite3']


def oom_kills_under_docker():
    """The kernel's count of memory-limit kills in Docker's parent cgroup. A cgroup's count includes its children's, so it
    outlives the container's own cgroup, and the kernel raises it before it sends the kill, so reading it needs no wait."""
    driver = subprocess.run(['docker', 'info', '--format', '{{.CgroupDriver}}'], stdout=subprocess.PIPE, text=True, check=True, timeout=30).stdout.strip()
    events = Path('/sys/fs/cgroup', {'systemd': 'system.slice', 'cgroupfs': 'docker'}[driver], 'memory.events').read_text()
    return int(dict(line.split() for line in events.splitlines())['oom_kill'])


# spec: ANYM-778
def test_in_the_real_container_its_memory_limit_kills_it_and_not_the_writer(tmp_path, real_helper):
    source = tmp_path / 'data' / 'voice.sqlite3'
    source.unlink()
    make_state(source, rows=5200, bulk=4000, canary=False)
    os.chmod(source, 0o666)
    hog = 'held = []\nimport threading, time\ndef _hog():\n    time.sleep(1)\n    while True:\n        held.append(b"x" * 8388608)\n        time.sleep(.01)\nthreading.Thread(target=_hog, daemon=True).start()\n'
    real, name, staging, _ = real_helper(hog, keep=True)
    kills_before = oom_kills_under_docker()
    stop, times, errors = threading.Event(), [], []
    capture, outcome = capture_running(real, name, staging, 120)
    thread = threading.Thread(target=writer_loop, args=(source, stop, times, errors))
    thread.start()
    try:
        capture.join(timeout=120)
        died_after = len(times)
        time.sleep(.3)
    finally:
        stop.set()
        thread.join(timeout=30)
    # SIGKILL ended the helper and the kernel counted a memory-limit kill; a `docker kill` gives the same exit and no count.
    # Docker's OOMKilled flag is not read: it stayed false through 15 s after 14 of 440 such kills (probe run 41982010224).
    assert outcome['result'][0] == 137 and oom_kills_under_docker() > kills_before, real.inspect(name)['State']
    assert not (staging / 'voice.sqlite3').exists() and died_after > 0
    assert not errors and len(times) > died_after and max(times) <= CALLS_WRITER_BOUND_SECONDS


def sealed(tmp_path, name='bundle', **kwargs):
    staged = make_state(tmp_path / (name + '-source') / 'voice.sqlite3', **kwargs)
    create_calls_snapshot(staged, tmp_path / name, captured_at=CAPTURED)
    return tmp_path / name


# spec: ANYM-778
def test_the_manifest_holds_the_format_the_tables_and_the_file_facts(tmp_path):
    bundle = sealed(tmp_path, rows=4)
    manifest = verify_calls_bundle(bundle)
    assert manifest['profile'] == 'calls' and manifest['schema_version'] == 1 and manifest['user_version'] == 0
    assert manifest['captured_at'] == CAPTURED and manifest['voice_events_max_seq'] == 4
    assert manifest['tables']['voice_events']['rows'] == 4 and manifest['tables']['briefs']['rows'] == 4
    assert set(manifest['tables']) == {'briefs', 'calls', 'voice_events', 'outbound_attempts'}
    assert manifest['bytes'] == (bundle / 'state/voice.sqlite3').stat().st_size and snapshot_profile(bundle) == 'calls'
    assert TOKEN not in (bundle / 'manifest.json').read_text()


# spec: ANYM-778
def test_seal_decrypt_and_verify_round_trip_with_the_manifest_first(tmp_path, age_stub):
    bundle = sealed(tmp_path)
    encrypt_snapshot(bundle, tmp_path / 'out.age', 'fake', age=age_stub)
    with tarfile.open(tmp_path / 'out.age') as archive:
        assert archive.getnames() == ['manifest.json', 'state/voice.sqlite3']
    decrypt_snapshot(tmp_path / 'out.age', 'fake', tmp_path / 'restored', age=age_stub)
    assert verify_calls_bundle(tmp_path / 'restored') == verify_calls_bundle(bundle)


def tar_plain(path, members):
    with tarfile.open(path, 'w') as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


# spec: ANYM-778
def test_decryption_refuses_an_extra_a_misplaced_or_a_tampered_member(tmp_path, age_stub):
    bundle = sealed(tmp_path)
    manifest, state = (bundle / 'manifest.json').read_bytes(), (bundle / 'state/voice.sqlite3').read_bytes()
    flipped = bytearray(state)
    flipped[len(flipped) // 2] ^= 1
    cases = {'extra': ([('manifest.json', manifest), ('state/voice.sqlite3', state), ('database.dump', b'x')], 'invalid_backup_member'),
             'second': ([('manifest.json', manifest), ('state/voice.sqlite3', state), ('state/other', b'x')], 'invalid_backup_member'),
             'state_first': ([('state/voice.sqlite3', state), ('manifest.json', manifest)], 'invalid_backup_member'),
             'missing': ([('manifest.json', manifest)], 'incomplete_backup'),
             'tampered': ([('manifest.json', manifest), ('state/voice.sqlite3', bytes(flipped))], 'calls_state_corrupt')}
    for name, (members, code) in cases.items():
        with pytest.raises((ValueError, RuntimeError), match=code):
            decrypt_snapshot(tar_plain(tmp_path / (name + '.tar'), members), 'fake', tmp_path / ('r-' + name), age=age_stub)
        assert not (tmp_path / ('r-' + name)).exists()


# spec: ANYM-778
@pytest.mark.parametrize('name,ok', [('manifest.json', True), ('state/voice.sqlite3', True), ('state/archive.sqlite', False),
                                     ('database.dump', False), ('state/voice.sqlite3-journal', False), ('../manifest.json', False)])
def test_only_the_manifest_and_the_one_database_are_calls_members(name, ok):
    assert valid_calls_member(name) is ok


# spec: ANYM-778
@pytest.mark.parametrize('field,value', [
    ('schema_version', 2), ('schema_version', True), ('profile', 'recordings'), ('bytes', 0), ('sha256', 'A' * 64),
    ('captured_at', 'yesterday'), ('captured_at', '2026-11-04T03:00:00'), ('tables', []), ('user_version', '0'),
    ('voice_events_max_seq', -1), ('extra', 1)])
def test_a_manifest_field_of_the_wrong_type_or_range_is_refused(tmp_path, field, value):
    bundle = sealed(tmp_path)
    manifest = json.loads((bundle / 'manifest.json').read_text())
    manifest[field] = value
    (bundle / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='invalid_calls_bundle'):
        verify_calls_bundle(bundle)


# spec: ANYM-778
def test_a_changed_row_digest_or_another_table_set_is_refused(tmp_path):
    for change in ('digest', 'rows', 'table'):
        bundle = sealed(tmp_path, change)
        manifest = json.loads((bundle / 'manifest.json').read_text())
        if change == 'digest':
            manifest['tables']['briefs']['sha256'] = '0' * 64
        elif change == 'rows':
            manifest['tables']['briefs']['rows'] += 1
        else:
            manifest['tables'].pop('briefs')
        (bundle / 'manifest.json').write_text(json.dumps(manifest))
        with pytest.raises(RuntimeError, match='calls_state_corrupt'):
            verify_calls_bundle(bundle)


# spec: ANYM-778
@pytest.mark.parametrize('kind,code', [('absent', 'calls_snapshot_absent'), ('empty', 'calls_snapshot_empty'),
                                       ('text', 'calls_snapshot_corrupt'), ('foreign', 'calls_snapshot_foreign'),
                                       ('symlink', 'calls_snapshot_unsafe')])
def test_a_file_that_is_not_a_calls_snapshot_creates_no_bundle(tmp_path, kind, code):
    path = tmp_path / 'staged' / 'voice.sqlite3'
    path.parent.mkdir()
    if kind == 'empty':
        path.write_bytes(b'')
    elif kind == 'text':
        path.write_bytes(b'not sqlite' * 100)
    elif kind == 'foreign':
        sqlite3.connect(path).executescript('CREATE TABLE other(a);')
    elif kind == 'symlink':
        path.symlink_to(make_state(tmp_path / 'real.sqlite3'))
    with pytest.raises(CallsRefused) as refused:
        create_calls_snapshot(path, tmp_path / 'out' / 'bundle', captured_at=CAPTURED)
    assert refused.value.code == code and not (tmp_path / 'out' / 'bundle').exists()


# spec: ANYM-778
def test_the_other_validators_refuse_a_calls_bundle_and_it_refuses_theirs(tmp_path):
    bundle = sealed(tmp_path)
    with pytest.raises(ValueError):
        verify_bundle(bundle)
    with pytest.raises(ValueError):
        verify_recordings_bundle(bundle)
    with pytest.raises(ValueError, match='invalid_calls_state'):
        foreign = tmp_path / 'foreign'
        foreign.mkdir()
        (foreign / 'manifest.json').write_text('{"profile":"recordings"}')
        encrypt_snapshot(foreign, tmp_path / 'x.age', 'fake', calls_state=bundle / 'state/voice.sqlite3')


CELLS = st.one_of(st.none(), st.integers(-2**63, 2**63 - 1), st.floats(allow_nan=False), st.text(), st.binary())


# spec: ANYM-778
@given(st.lists(CELLS, max_size=6))
def test_row_text_round_trips_every_storage_class_so_no_two_rows_share_a_form(row):
    def back(cell):
        tag = cell[0]
        return {'n': lambda: None, 'i': lambda: int(cell[1]), 'f': lambda: float.fromhex(cell[1]),
                'b': lambda: bytes.fromhex(cell[1]), 't': lambda: cell[1]}[tag]()
    text = row_text(row)
    assert text.isascii()
    decoded = [back(cell) for cell in json.loads(text)]
    assert [(type(a), a) for a in decoded] == [(type(a), a) for a in row]


# spec: ANYM-778
def test_a_whole_run_delivers_one_verified_bundle_and_empties_staging(tmp_path, age_stub, monkeypatch):
    runner, docker, sent = calls_runner(tmp_path, age_stub, monkeypatch)
    runner.run()
    result, manifest, _ = sent[0]
    assert result['profile'] == 'calls' and result['captured_at'] == manifest['captured_at']
    assert manifest['captured_at'].startswith('2026-11-04T03:00:00')
    assert not list(runner.staging_root.iterdir()) and not runner.journal_path.exists()
    assert status(runner)['state'] == 'ok' and status(runner)['archive_bytes'] == manifest['bytes']
    assert not runner.unsettled_path.exists() and not docker.containers


def inject_helper_timeout(docker, tmp_path):
    docker.attach_result = 'timeout'


def inject_locked(docker, tmp_path):
    docker.attach_result = (3, '{"error":"busy"}')


def inject_killed(docker, tmp_path):
    docker.attach_result = (137, '')


def inject_disk(docker, tmp_path):
    docker.attach_result = (3, '{"error":"no_space"}')


def inject_encryption(docker, tmp_path):
    docker.encrypt_error = RuntimeError('backup_encryption_failed')


def inject_encrypt_disk(docker, tmp_path):
    docker.encrypt_error = OSError(28, 'no space')


FAILURES = [(inject_helper_timeout, 'calls_helper_timeout'), (inject_locked, 'calls_snapshot_busy'),
            (inject_killed, 'calls_helper_failed'), (inject_disk, 'calls_no_space'),
            (inject_encryption, 'backup_capture_failed'), (inject_encrypt_disk, 'calls_no_space')]


# spec: ANYM-778
@pytest.mark.parametrize('inject,code', FAILURES, ids=[f[0].__name__ for f in FAILURES])
def test_a_failure_at_any_phase_ends_with_its_code_and_touches_nothing_else(tmp_path, age_stub, monkeypatch, inject, code):
    runner, docker, sent = calls_runner(tmp_path, age_stub, monkeypatch)
    other = tmp_path / 'messaging' / '.runner.lock.unsettled'
    other.write_text('{"profile":"calendar"}')
    journal = tmp_path / 'messaging' / 'journal.json'
    journal.write_text('{}')
    inject(docker, tmp_path)
    assert str(fail_and_recover(runner)) == code
    assert status(runner)['error_code'] == code and not sent
    assert not list(runner.staging_root.iterdir()) and not runner.journal_path.exists() and not runner.unsettled_path.exists()
    assert other.read_text() == '{"profile":"calendar"}' and journal.read_text() == '{}'
    assert all(CALLS_CONTAINER not in c or c == ('image_of', CALLS_CONTAINER) for c in docker.calls)
    # The next run takes a new snapshot; the host lock is free for every other profile.
    docker.attach_result, docker.encrypt_error = None, None
    with backup_runner.hold_locks(runner.cfg, 0):
        pass
    runner.run()
    assert len(sent) == 1 and other.exists()


# spec: ANYM-778
def test_a_busy_host_lock_skips_with_the_calls_code_after_cleaning_what_a_crash_left(tmp_path, age_stub, monkeypatch):
    import fcntl
    runner, _, _ = calls_runner(tmp_path, age_stub, monkeypatch)
    runner.staging_root.mkdir(mode=0o700)
    (runner.staging_root / 'left').write_text('plaintext')
    fd = os.open(runner.cfg['host_lock'], os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(backup_runner.Skip) as skip:
            with backup_runner.hold_locks(runner.cfg, 0, runner.cleanup_unowned):
                pass
    finally:
        os.close(fd)
    assert skip.value.code == 'calls_skipped_lock_busy' and skip.value.code in backup_runner.SAFE_ERRORS
    assert not list(runner.staging_root.iterdir())


# spec: ANYM-782
def test_a_busy_host_lock_schedules_one_retry_of_the_daily_unit_a_day_and_the_skip_stands(tmp_path, age_stub, monkeypatch,
                                                                                            capsys):
    # 11-09: automatic deploys held messaging's lock at 02:15 and 04:15; both calls runs skipped and nothing retried.
    import fcntl
    runner, _, _ = calls_runner(tmp_path, age_stub, monkeypatch)
    config = tmp_path / 'runner.json'
    config.write_text(json.dumps(runner.cfg))
    config.chmod(0o600)
    monkeypatch.setattr(backup_runner.sys, 'argv', ['backup_runner', '--config', str(config)])
    monkeypatch.setattr(backup_runner.signal, 'signal', lambda *_: None)
    unit: list[str | None] = [None]
    scheduled = []
    monkeypatch.setattr(backup_runner, 'own_unit', lambda profile: unit[0])
    def systemd_run(argv):
        scheduled.append(argv)
        if scheduled.count(argv) > 1:
            raise subprocess.CalledProcessError(1, argv)  # systemd refuses a second unit of that name
    monkeypatch.setattr(backup_runner, 'systemd_run', systemd_run)
    def main():
        with pytest.raises(SystemExit) as stopped:
            backup_runner.main()
        return stopped.value.code, [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    fd = os.open(runner.cfg['host_lock'], os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        # A hand run (or a test) is in no backup unit: it schedules nothing.
        assert main() == (0, [{'status': 'skipped', 'code': 'calls_skipped_lock_busy'}]) and scheduled == []
        unit[0] = 'deedkit-calls-backup-catchup.service'
        name = 'deedkit-calls-backup-retry-' + datetime.now(timezone.utc).strftime('%Y%m%d')
        assert main() == (0, [{'retry': name, 'in_seconds': 1200}, {'status': 'skipped', 'code': 'calls_skipped_lock_busy'}])
        assert scheduled == [['systemd-run', '--no-ask-password', '--quiet', '--unit', name, '--on-active=1200',
                              '--timer-property=AccuracySec=1s', '--timer-property=RemainAfterElapse=yes',
                              '--description=Retry of a calls backup run that found the host lock busy',
                              '/usr/bin/systemctl', 'start', '--no-block', 'deedkit-calls-backup.service']]
        # The same day the name exists: no second retry; the skip, its status and exit code are unchanged.
        assert main() == (0, [{'retry': None, 'in_seconds': 1200}, {'status': 'skipped', 'code': 'calls_skipped_lock_busy'}])
        assert len(scheduled) == 2 and read_json(Path(runner.cfg['directory']) / 'status.json')['error_code'] == \
            'calls_skipped_lock_busy'
    finally:
        os.close(fd)
    assert backup_runner.RETRY_DELAY_SECONDS == 1200 and backup_runner.RETRY_PROFILES == ('recordings', 'calls')
    monkeypatch.setattr(backup_runner, 'own_unit', lambda profile: None)
    assert backup_runner.schedule_retry('recordings') is None and len(scheduled) == 2


# spec: ANYM-778
def test_admission_reads_the_timers_at_every_start_and_skips_with_calls_codes(tmp_path, age_stub, monkeypatch):
    runner, docker, sent = calls_runner(tmp_path, age_stub, monkeypatch)
    runner.clock = lambda: NOW + 12 * 3600 + 30 * 60  # 15:30 UTC, 30 minutes before the calendar's 16:00
    with pytest.raises(backup_runner.Skip) as skip:
        runner.run()
    assert skip.value.code == 'calls_skipped_window' and not docker.calls and not sent
    runner.clock = lambda: NOW
    (tmp_path / 'timers' / 'calendar.timer').write_text('[Timer]\nOnCalendar=weekly\n')
    with pytest.raises(backup_runner.Skip) as skip:
        runner.run()
    assert skip.value.code == 'calls_skipped_schedule_unknown'


# spec: ANYM-778
def test_no_row_or_token_reaches_a_log_a_status_file_an_exception_or_staging(tmp_path, age_stub, monkeypatch, capfd):
    runner, docker, sent = calls_runner(tmp_path, age_stub, monkeypatch)
    runner.run()
    docker.attach_result = (3, json.dumps({'error': TOKEN}))
    failure = fail_and_recover(runner)
    captured = capfd.readouterr()
    texts = [captured.out, captured.err, str(failure), runner.status_path.read_text()]
    for path in Path(runner.cfg['directory']).rglob('*'):
        if path.is_file() and path.name != 'bundle.tar.age':  # the stub's "ciphertext" is plain; real age encrypts it
            texts.append(path.read_text(errors='replace'))
    assert not any(TOKEN in text or 'synthetic brief' in text or CANARY in text for text in texts)
    assert str(failure) == 'calls_helper_failed' and not list(runner.staging_root.iterdir())


# spec: ANYM-778
def test_staging_with_another_owner_or_mode_is_refused(tmp_path, age_stub, monkeypatch):
    runner, docker, _ = calls_runner(tmp_path, age_stub, monkeypatch)
    runner.container_uid = os.getuid() + 1
    assert str(fail_and_recover(runner)) == 'calls_staging_unsafe'
    assert not [c for c in docker.calls if c[0] == 'create']


# spec: ANYM-778
def test_backup_md_names_the_contents_keys_and_limits():
    text = (Path(__file__).resolve().parents[1] / 'BACKUP.md').read_text()
    section = text[text.index('## Calls state (ANYM-778)'):]
    for word in ('media-handshake tokens', 'learning tables', 'decision journals', 'data_mount', 'calls_container',
                 'container image', 'VACUUM INTO', 'host_lock', 'keep_local'):
        assert word in section
