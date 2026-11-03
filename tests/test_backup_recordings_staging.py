"""Recordings archive staging, bounded work and isolation from the other backups (ANYM-727).

Synthetic SQLite archives, a fake Docker (the `docker exec` of the snapshot CLI writes a file), a fake clock and fake disk;
no real Docker, provider, timer or age.
"""
import configparser
import copy
import errno
import fcntl
import json
import os
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest
import yaml

from messaging_service import backup_runner, backup_worker
from messaging_service.backup_crypto import decrypt_snapshot
from messaging_service.backup_recordings import RecordingsRefused, create_recordings_snapshot, verify_recordings_bundle
from messaging_service.backup_runner import (GUARD_SECONDS, RECOVERY_BUDGET, STOP_BUDGET, Runner, Skip, next_start, read_json,
                                             timer_starts, worst_case)
from test_backup_recordings import ARCHIVE_ID, NOW, RecordingsDocker, make_archive, recordings_cfg, write_timers

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / 'deploy'
CONTAINER = 'recordings-api-1'
HOUR = 3600
GIB = 1024 ** 3
FREE = 10 * 1024 ** 4


@pytest.fixture
def age_stub(tmp_path):
    path = tmp_path / 'age-stub'
    path.write_text('#!' + sys.executable + '\nimport sys,shutil\n'
        'source=open(sys.argv[-1],"rb") if "-d" in sys.argv else sys.stdin.buffer\n'
        'shutil.copyfileobj(source,sys.stdout.buffer)\n')
    path.chmod(0o700)
    return str(path)


class SnapshotDocker(RecordingsDocker):
    """The running recordings container: `docker exec` writes the snapshot into the staging mount; the worker runs in process.

    ``mode``: ok, disabled, schema, failed, timeout, nofile, junk, empty. ``hold``: the worker container stays running.
    """
    def __init__(self, root, age, monkeypatch, staging):
        super().__init__(root, age, monkeypatch)
        self.staging, self.mode, self.hold, self.execs = staging, 'ok', False, []
        self.containers[CONTAINER] = {'Id': CONTAINER, 'Config': {'Labels': {}}, 'Mounts': [],
                                      'State': {'Running': True, 'Status': 'running', 'ExitCode': 0}}
    def exec(self, container, args, timeout):
        self.execs.append((container, list(args), timeout))
        target = self.staging / Path(args[-1]).parent.name / 'archive.sqlite'
        assert args[-1] == f'/backup-staging/{target.parent.name}/archive.sqlite'
        if self.mode == 'timeout':
            raise RuntimeError('recordings_exec_timeout')
        if self.mode == 'disabled':
            return 1, 'Archive backup failed: archive_disabled\n'
        if self.mode == 'schema':
            return 1, 'Archive backup failed: archive_schema_unsupported\n'
        if self.mode == 'failed':
            return 1, 'Archive backup failed: archive_unavailable\n'
        if self.mode == 'nofile':
            return 0, ''
        if self.mode == 'junk':
            target.write_bytes(b'not an sqlite file' * 500)
        elif self.mode == 'empty':
            target.write_bytes(b'')
        else:
            make_archive(target, bulk=50)
        os.chmod(target, 0o600)
        return 0, ''
    def call(self, *args):
        if args[0] == 'start' and self.hold:
            self.calls.append(args)
            self.containers[args[1]]['State'].update(Running=True, Status='running')
            return ''
        return super().call(*args)


def snapshot_runner(tmp_path, age_stub, monkeypatch, send=None, now=NOW, **options):
    cfg = recordings_cfg(tmp_path)
    cfg['recordings'].update(container=CONTAINER, **options)
    Path(cfg['host_lock']).parent.mkdir(mode=0o700)
    (tmp_path / 'recipient').write_text('fake')
    write_timers(tmp_path)
    (tmp_path / 'staging').mkdir(mode=0o700)
    docker = SnapshotDocker(Path(cfg['directory']), age_stub, monkeypatch, tmp_path / 'staging')
    delivered = []
    async def stored(path, result, transfer, timeout):
        received = tmp_path / f'received-{len(delivered)}'
        decrypt_snapshot(path, 'fake', received, age=age_stub)
        delivered.append(verify_recordings_bundle(received))
        return {**result, 'status': 'stored', 'duplicate': False}
    clock = [now]
    runner = Runner(cfg, docker=docker, sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds), send=send or stored,
                    clock=lambda: clock[0])
    runner.container_uid, runner.container_gid = os.getuid(), os.getgid()
    runner.disk = lambda path: SimpleNamespace(free=FREE)
    runner.now = clock
    return runner, docker, delivered


def folder(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return path


def crash(runner):
    """Run until the first poll of the worker, then die without any cleanup (what a kill or a reboot does)."""
    runner.clean_staging = lambda: None
    runner.sleep = lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        runner.run()
    del runner.clean_staging


def status_of(runner):
    return read_json(runner.status_path)


def clean(runner):
    return sorted(p.name for p in runner.staging_root.iterdir()) == [] and not runner.journal_path.exists()


def held(path):
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


# spec: ANYM-727
def test_the_complete_handoff_snapshot_in_the_running_container_hash_worker_bundle(tmp_path, age_stub, monkeypatch):
    runner, docker, delivered = snapshot_runner(tmp_path, age_stub, monkeypatch)
    runner.run()
    ((container, args, timeout),) = docker.execs
    job = runner.journal_path.parent
    (name,) = [p.name for p in job.iterdir() if backup_runner.ID.fullmatch(p.name)]
    assert container == CONTAINER and timeout == 300
    assert args == ['deedkit-recordings', '--config', '/run/deedkit/recordings/config.json', '--backup-to',
                    f'/backup-staging/{name}/archive.sqlite']
    # One writer: no container is created from the archive volume, only the offline worker (no network, read-only snapshot).
    create = next(call for call in docker.calls if call[0] == 'create')
    assert [call[0] for call in docker.calls].count('create') == 1 and 'run' not in {call[0] for call in docker.calls}
    mounts = [create[i + 1] for i, arg in enumerate(create) if arg == '--mount']
    assert f'type=bind,src={tmp_path}/staging/{name},dst=/sources/snapshot,readonly' in mounts
    assert not any('recordings-data' in mount or 'type=volume' in mount for mount in mounts)
    assert docker.touched() <= {CONTAINER, 'deedkit-recordings-backup-' + name.lower()}
    ((manifest),) = delivered
    assert manifest['archive']['archive_id'] == ARCHIVE_ID and manifest['bytes'] > 4096
    assert status_of(runner)['state'] == 'ok' and status_of(runner)['archive_bytes'] == manifest['bytes']
    assert clean(runner)


# spec: ANYM-727
def test_the_runner_hashes_the_snapshot_and_the_worker_refuses_a_file_that_differs(tmp_path, age_stub, monkeypatch):
    seen = []
    runner, docker, _ = snapshot_runner(tmp_path, age_stub, monkeypatch)
    real = docker.call
    def spying(*args):
        if args[0] == 'create':
            seen.append(read_json(runner.journal_path)['archive'])
        return real(*args)
    docker.call = spying
    runner.run()
    (facts,) = seen
    assert set(facts) == {'bytes', 'sha256'} and len(facts['sha256']) == 64
    # The same worker, a snapshot changed after the hash.
    staged = tmp_path / 'one' / 'archive.sqlite'
    make_archive(staged)
    work = tmp_path / 'work'
    work.mkdir(mode=0o700)
    (tmp_path / 'recipient').write_text('fake')
    config = {'profile': 'recordings', 'id': '20261104T030000Z-0123456789abcdef', 'snapshot_directory': str(staged.parent),
              'recipient_file': str(tmp_path / 'recipient'), 'work_directory': str(work),
              'expected_bytes': staged.stat().st_size + 1, 'expected_sha256': 'a' * 64}
    with pytest.raises(RecordingsRefused) as refused:
        backup_worker.run(config)
    assert refused.value.code == 'recordings_snapshot_changed'
    assert read_json(work / 'refused.json')['code'] == 'recordings_snapshot_changed' and not (work / 'bundle.tar.age').exists()
    assert not (work / 'snapshot').exists()


# spec: ANYM-727
@pytest.mark.parametrize('change', [{'expected_bytes': 5}, {'expected_sha256': 'b' * 64}, {'expected_bytes': 0, 'expected_sha256': 'a' * 64},
                                    {'expected_bytes': 5, 'expected_sha256': 'zz'}, {'encrypt_timeout_seconds': 0},
                                    {'integrity_timeout_seconds': '5'}])
def test_the_worker_config_takes_its_new_keys_only_in_valid_shapes(tmp_path, change):
    work = tmp_path / 'work'
    work.mkdir(mode=0o700)
    config = {'profile': 'recordings', 'id': '20261104T030000Z-0123456789abcdef', 'snapshot_directory': str(tmp_path),
              'recipient_file': str(tmp_path / 'r'), 'work_directory': str(work), **change}
    with pytest.raises(ValueError, match='invalid_backup_worker_config'):
        backup_worker.run(config)


# spec: ANYM-727
@pytest.mark.parametrize('mode,code', [('disabled', 'recordings_skipped_archive_disabled')])
def test_an_archive_that_is_off_is_a_recorded_skip_not_a_failure(tmp_path, age_stub, monkeypatch, mode, code):
    runner, docker, delivered = snapshot_runner(tmp_path, age_stub, monkeypatch)
    docker.mode = mode
    with pytest.raises(Skip) as skipped:
        runner.run()
    assert skipped.value.code == code
    runner.recover_stop()
    assert status_of(runner)['state'] == 'skipped' and status_of(runner)['error_code'] == code
    assert clean(runner) and delivered == [] and 'create' not in {call[0] for call in docker.calls}


# spec: ANYM-727
@pytest.mark.parametrize('running', [None, False])
def test_a_container_that_is_down_is_a_recorded_skip_and_runs_no_exec(tmp_path, age_stub, monkeypatch, running):
    runner, docker, _ = snapshot_runner(tmp_path, age_stub, monkeypatch)
    if running is None:
        del docker.containers[CONTAINER]
    else:
        docker.containers[CONTAINER]['State']['Running'] = False
    with pytest.raises(Skip, match='recordings_skipped_container_down'):
        runner.run()
    runner.recover_stop()
    assert docker.execs == [] and status_of(runner)['state'] == 'skipped' and clean(runner)


# spec: ANYM-727
@pytest.mark.parametrize('mode,code', [('timeout', 'recordings_exec_timeout'), ('failed', 'recordings_exec_failed'),
                                       ('schema', 'recordings_schema_unsupported'), ('nofile', 'recordings_snapshot_absent'),
                                       ('empty', 'recordings_snapshot_empty')])
def test_a_snapshot_call_that_fails_ends_the_job_with_its_code_and_leaves_no_plaintext(tmp_path, age_stub, monkeypatch, mode, code):
    runner, docker, delivered = snapshot_runner(tmp_path, age_stub, monkeypatch)
    docker.mode = mode
    with pytest.raises(RuntimeError, match=code):
        runner.run()
    runner.recover_stop()
    assert status_of(runner)['state'] == 'error' and status_of(runner)['error_code'] == code
    assert clean(runner) and delivered == [] and not list(Path(runner.cfg['directory']).rglob('bundle.tar.age'))


# spec: ANYM-727
def test_a_junk_snapshot_is_refused_by_the_worker_with_a_fixed_code_and_nothing_stays(tmp_path, age_stub, monkeypatch):
    runner, docker, delivered = snapshot_runner(tmp_path, age_stub, monkeypatch)
    docker.mode = 'junk'
    with pytest.raises(RuntimeError, match='recordings_snapshot_corrupt'):
        runner.run()
    runner.recover_stop()
    assert clean(runner) and delivered == [] and status_of(runner)['error_code'] == 'recordings_snapshot_corrupt'


# spec: ANYM-727
def test_the_snapshot_call_has_its_own_deadline_and_the_hash_has_its_own(tmp_path, age_stub, monkeypatch):
    runner, docker, _ = snapshot_runner(folder(tmp_path, 'a'), age_stub, monkeypatch, exec_timeout_seconds=111)
    runner.run()
    assert docker.execs[0][2] == 111
    # A hash that outlasts its deadline: after the snapshot call every clock reading jumps past it.
    runner, docker, _ = snapshot_runner(folder(tmp_path, 'b'), age_stub, monkeypatch, hash_timeout_seconds=10)
    readings = iter(range(0, 10 ** 9, 1000))
    real = docker.exec
    def exec_then_a_slow_clock(*args):
        runner.clock = lambda: next(readings)
        return real(*args)
    monkeypatch.setattr(docker, 'exec', exec_then_a_slow_clock)
    with pytest.raises(RuntimeError, match='recordings_hash_timeout'):
        runner.run()
    runner.recover_stop()
    assert clean(runner)


# spec: ANYM-727
def test_the_integrity_check_has_its_own_deadline(tmp_path):
    staged = make_archive(tmp_path / 'staging' / 'archive.sqlite', bulk=600)
    with pytest.raises(RecordingsRefused) as refused:
        create_recordings_snapshot(staged, tmp_path / 'bundle', copy_state=False, integrity_seconds=-1)
    assert refused.value.code == 'recordings_integrity_timeout' and not (tmp_path / 'bundle').exists()
    assert create_recordings_snapshot(staged, tmp_path / 'bundle', copy_state=False, integrity_seconds=60)['status'] == 'snapshot_created'


# spec: ANYM-727
def test_the_encryption_has_its_own_deadline_and_leaves_no_bundle_or_plaintext_copy(tmp_path, age_stub, monkeypatch):
    def slow(*args, **kwargs):
        import time
        time.sleep(5)
    monkeypatch.setattr(backup_worker, 'encrypt_snapshot', slow)
    staged = make_archive(tmp_path / 'staging' / 'archive.sqlite')
    work = tmp_path / 'work'
    work.mkdir(mode=0o700)
    (tmp_path / 'recipient').write_text('fake')
    config = {'profile': 'recordings', 'id': '20261104T030000Z-0123456789abcdef', 'snapshot_directory': str(staged.parent),
              'recipient_file': str(tmp_path / 'recipient'), 'work_directory': str(work), 'encrypt_timeout_seconds': 1}
    with pytest.raises(RecordingsRefused) as refused:
        backup_worker.run(config)
    assert refused.value.code == 'recordings_encrypt_timeout'
    assert read_json(work / 'refused.json')['code'] == 'recordings_encrypt_timeout'
    assert not (work / 'snapshot').exists() and not (work / 'bundle.tar.age').exists() and staged.exists()
    import signal
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0, 'the alarm is cleared'


# spec: ANYM-727
def test_a_full_disk_while_encrypting_ends_the_job_without_a_bundle_and_with_staging_emptied(tmp_path, age_stub, monkeypatch):
    runner, docker, delivered = snapshot_runner(tmp_path, age_stub, monkeypatch)
    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, 'No space left on device')
    monkeypatch.setattr(backup_worker, 'encrypt_snapshot', full)
    with pytest.raises(RuntimeError, match='recordings_no_space'):
        runner.run()
    runner.recover_stop()
    assert status_of(runner)['error_code'] == 'recordings_no_space' and clean(runner) and delivered == []
    assert not list(Path(runner.cfg['directory']).rglob('bundle.tar.age'))


# spec: ANYM-727
def test_admission_counts_the_plaintext_and_the_ciphertext_together_above_the_floor(tmp_path, age_stub, monkeypatch):
    runner, docker, _ = snapshot_runner(tmp_path, age_stub, monkeypatch, expected_archive_bytes=2 * GIB)
    # One filesystem: 2 GiB of plaintext + 2 GiB of ciphertext + the 1 GiB floor.
    runner.disk = lambda path: SimpleNamespace(free=5 * GIB - 1)
    with pytest.raises(Skip, match='recordings_skipped_disk'):
        runner.run()
    runner.recover_stop()
    assert docker.execs == [] and clean(runner) and status_of(runner)['state'] == 'skipped'
    runner.disk = lambda path: SimpleNamespace(free=5 * GIB)
    runner.run()
    assert status_of(runner)['state'] == 'ok'


# spec: ANYM-727
def test_the_estimate_grows_with_the_last_archive_and_the_floor_is_configurable(tmp_path, age_stub, monkeypatch):
    runner, docker, _ = snapshot_runner(tmp_path, age_stub, monkeypatch, min_free_bytes=300 * 1024 ** 2)
    runner.status('ok')
    data = status_of(runner)
    data['archive_bytes'] = 4 * GIB
    backup_runner.write_json(runner.status_path, data)
    runner.disk = lambda path: SimpleNamespace(free=8 * GIB)
    with pytest.raises(Skip, match='recordings_skipped_disk'):
        runner.run()
    runner.recover_stop()
    runner.disk = lambda path: SimpleNamespace(free=8 * GIB + 300 * 1024 ** 2)
    runner.run()
    assert status_of(runner)['state'] == 'ok'


# spec: ANYM-727
def test_a_snapshot_that_fills_the_disk_ends_with_no_space_and_the_plaintext_goes(tmp_path, age_stub, monkeypatch):
    runner, docker, _ = snapshot_runner(tmp_path, age_stub, monkeypatch)
    docker.mode = 'failed'
    runner.disk = lambda path: SimpleNamespace(free=GIB - 1) if docker.execs else SimpleNamespace(free=FREE)
    with pytest.raises(RuntimeError, match='recordings_no_space'):
        runner.run()
    runner.recover_stop()
    assert clean(runner)


# spec: ANYM-727
def test_after_the_snapshot_the_real_size_is_admitted_against_the_free_space(tmp_path, age_stub, monkeypatch):
    runner, docker, delivered = snapshot_runner(tmp_path, age_stub, monkeypatch)
    free = [FREE]
    runner.disk = lambda path: SimpleNamespace(free=free[0])
    real = docker.exec
    def exec_then_fill(*args):
        result = real(*args)
        free[0] = GIB + 100  # only the floor and a few bytes remain for the ciphertext
        return result
    monkeypatch.setattr(docker, 'exec', exec_then_fill)
    with pytest.raises(Skip, match='recordings_skipped_disk'):
        runner.run()
    runner.recover_stop()
    assert clean(runner) and delivered == [] and 'create' not in {call[0] for call in docker.calls}


# spec: ANYM-727
@pytest.mark.parametrize('prepare', ['wrong_mode', 'wrong_owner'])
def test_a_staging_directory_that_the_container_cannot_use_or_others_can_read_is_refused_before_any_exec(
        tmp_path, age_stub, monkeypatch, prepare):
    runner, docker, _ = snapshot_runner(tmp_path, age_stub, monkeypatch)
    if prepare == 'wrong_owner':
        runner.container_uid = os.getuid() + 1  # the mount is not the container user's: it could not write into it
    else:
        runner.staging_root.chmod(0o755)  # others could read the plaintext
    with pytest.raises((RuntimeError, ValueError), match='recordings_staging_unsafe|private_backup_directory_required'):
        runner.run()
    runner.staging_root.chmod(0o700)
    assert docker.execs == []


# --- admission against the other profiles' schedules ---------------------------------------------------------------------

def at(hour, minute=0, second=0):
    return NOW - 3 * HOUR + hour * HOUR + minute * 60 + second


# spec: ANYM-727
def test_the_timer_files_are_read_not_hard_coded_and_only_daily_utc_lines_are_understood(tmp_path):
    timer = tmp_path / 'x.timer'
    timer.write_text('[Timer]\nOnCalendar=*-*-* 18:00:00 UTC\nOnCalendar=*-*-* 06:30 UTC\nPersistent=true\n')
    assert timer_starts(timer) == [18 * HOUR, 6 * HOUR + 1800]
    for text in ('[Timer]\nOnBootSec=5min\n', '[Timer]\nOnCalendar=daily\n', '[Timer]\nOnCalendar=*-*-* 18:00:00\n',
                 '[Timer]\nOnCalendar=Mon *-*-* 18:00:00 UTC\n', '[Timer]\nOnCalendar=*-*-* 25:00:00 UTC\n', ''):
        timer.write_text(text)
        with pytest.raises(Skip, match='recordings_skipped_schedule_unknown'):
            timer_starts(timer)
    with pytest.raises(Skip, match='recordings_skipped_schedule_unknown'):
        timer_starts(tmp_path / 'absent.timer')


# spec: ANYM-727
def test_the_next_start_is_the_first_scheduled_time_after_now_across_midnight():
    assert next_start(at(3), [16 * HOUR]) == at(16)
    assert next_start(at(16), [16 * HOUR]) == at(16) + 24 * HOUR
    assert next_start(at(23, 59), [HOUR]) == at(25)
    assert next_start(at(3), [18 * HOUR, 16 * HOUR]) == at(16)


# spec: ANYM-727
def test_the_worst_case_is_the_sum_of_the_configured_deadlines_and_a_resumed_delivery_owns_only_its_tail(tmp_path):
    cfg = recordings_cfg(tmp_path)
    cfg['busy_wait_seconds'] = 1800
    assert worst_case(cfg) == STOP_BUDGET + 300 + 120 + 1200 + (60 + 1800) + RECOVERY_BUDGET
    assert worst_case(cfg, 'upload') == 60 + 1800 + RECOVERY_BUDGET
    cfg['recordings'].update(exec_timeout_seconds=31, hash_timeout_seconds=11)
    assert worst_case(cfg) == STOP_BUDGET + 31 + 11 + 1200 + (60 + 1800) + RECOVERY_BUDGET


# spec: ANYM-727
@pytest.mark.parametrize('now,admitted', [
    (at(3), True), (at(21, 30), True), (at(22, 0), True),
    (at(13, 0), True), (at(14, 40), False), (at(15, 30), False), (at(15, 59), False),   # the calendar at 16:00
    (at(17, 30), False), (at(17, 59), False),                                           # messaging at 18:00
    (at(19, 0), False), (at(19, 14), False)])                                           # the calendar catch-up at 19:15
def test_a_start_is_admitted_only_when_the_whole_worst_case_ends_15_minutes_before_another_profile_starts(
        tmp_path, age_stub, monkeypatch, now, admitted):
    runner, docker, _ = snapshot_runner(tmp_path, age_stub, monkeypatch, now=now)
    (tmp_path / 'timers' / 'catchup.timer').write_text('[Timer]\nOnCalendar=*-*-* 19:15:00 UTC\n')
    runner.cfg['recordings']['timers'].append(str(tmp_path / 'timers' / 'catchup.timer'))
    if admitted:
        runner.run()
        assert status_of(runner)['state'] == 'ok'
    else:
        with pytest.raises(Skip, match='recordings_skipped_window'):
            runner.run()
        assert not runner.journal_path.exists() and docker.execs == [] and list(runner.staging_root.iterdir()) == []


# spec: ANYM-727
def test_the_admission_boundary_is_exact(tmp_path, age_stub, monkeypatch):
    probe, _, _ = snapshot_runner(folder(tmp_path, 'a'), age_stub, monkeypatch)
    latest = at(16) - GUARD_SECONDS - worst_case(probe.cfg)
    admitted, _, _ = snapshot_runner(folder(tmp_path, 'b'), age_stub, monkeypatch, now=latest)
    admitted.admit('full')
    assert admitted.yield_at == at(16) - GUARD_SECONDS
    late, _, _ = snapshot_runner(folder(tmp_path, 'c'), age_stub, monkeypatch, now=latest + 1)
    with pytest.raises(Skip, match='recordings_skipped_window'):
        late.admit('full')


# spec: ANYM-727
@pytest.mark.parametrize('broken', ['absent', 'unknown_form'])
def test_an_unreadable_or_unknown_schedule_stops_the_job_before_anything_is_made(tmp_path, age_stub, monkeypatch, broken):
    runner, docker, _ = snapshot_runner(tmp_path, age_stub, monkeypatch)
    timer = tmp_path / 'timers' / 'messaging.timer'
    if broken == 'absent':
        timer.unlink()
    else:
        timer.write_text('[Timer]\nOnCalendar=hourly\n')
    with pytest.raises(Skip, match='recordings_skipped_schedule_unknown'):
        runner.run()
    assert not runner.journal_path.exists() and docker.execs == [] and list(runner.staging_root.iterdir()) == []


# spec: ANYM-727
def test_a_job_that_runs_past_its_window_yields_before_sealing_and_cleans_up(tmp_path, age_stub, monkeypatch):
    runner, docker, delivered = snapshot_runner(tmp_path, age_stub, monkeypatch)
    docker.hold = True
    runner.sleep = lambda seconds: runner.now.__setitem__(0, at(15, 50))  # a clock step: the calendar starts in 10 minutes
    with pytest.raises(Skip, match='recordings_skipped_yielded'):
        runner.run()
    container = 'deedkit-recordings-backup-' + runner.journal['id'].lower()
    runner.recover_stop()
    assert ('stop', '--time', '25', container) in docker.calls and container not in docker.containers
    assert status_of(runner)['state'] == 'skipped' and status_of(runner)['error_code'] == 'recordings_skipped_yielded'
    assert clean(runner) and delivered == [] and not list(Path(runner.cfg['directory']).rglob('bundle.tar.age'))


# spec: ANYM-727
def test_a_job_that_yields_after_the_snapshot_call_removes_the_staged_plaintext(tmp_path, age_stub, monkeypatch):
    runner, docker, delivered = snapshot_runner(tmp_path, age_stub, monkeypatch)
    real = docker.exec
    def exec_then_the_clock_jumps(*args):
        result = real(*args)
        runner.now[0] = at(15, 50)
        return result
    monkeypatch.setattr(docker, 'exec', exec_then_the_clock_jumps)
    with pytest.raises(Skip, match='recordings_skipped_yielded'):
        runner.run()
    runner.recover_stop()
    assert clean(runner) and delivered == [] and 'create' not in {call[0] for call in docker.calls}


# --- locks, retries, crashes -----------------------------------------------------------------------------------------------

def run_main(monkeypatch, tmp_path, runner_cfg, *argv):
    config = tmp_path / 'runner.json'
    config.write_text(json.dumps(runner_cfg))
    config.chmod(0o600)
    monkeypatch.setattr(sys, 'argv', ['backup_runner', '--config', str(config), *argv])
    monkeypatch.setattr(backup_runner.signal, 'signal', lambda *_: None)
    try:
        backup_runner.main()
    except SystemExit as stopped:
        return stopped.code
    return 0


# spec: ANYM-727
def test_a_busy_host_lock_is_a_skip_without_waiting_and_the_other_profile_is_not_disturbed(tmp_path, monkeypatch, capsys):
    cfg = recordings_cfg(tmp_path)
    Path(cfg['host_lock']).parent.mkdir(mode=0o700)
    Path(cfg['host_lock']).touch()
    other = os.open(cfg['host_lock'], os.O_RDWR)
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)  # messaging, the calendar or a deploy
    try:
        slept = []
        monkeypatch.setattr(backup_runner.time, 'sleep', lambda seconds: slept.append(seconds))
        assert run_main(monkeypatch, tmp_path, cfg) == 0
        assert slept == [] and held(cfg['host_lock'])
        assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {'status': 'skipped', 'code': 'recordings_skipped_lock_busy'}
        status = read_json(Path(cfg['directory']) / 'status.json')
        assert status['state'] == 'skipped' and status['error_code'] == 'recordings_skipped_lock_busy'
        assert not (Path(cfg['directory']) / 'journal.json').exists() and not Path(cfg['recordings']['staging_directory']).exists()
    finally:
        os.close(other)
    assert not held(cfg['host_lock'])


# spec: ANYM-727
def test_the_runner_takes_the_host_lock_without_a_wait_for_a_run_and_briefly_for_its_recovery(tmp_path, monkeypatch):
    waits = []
    from contextlib import contextmanager
    @contextmanager
    def spy(cfg, wait=backup_runner.HOST_LOCK_WAIT_SECONDS):
        waits.append(wait)
        yield
    monkeypatch.setattr(backup_runner, 'hold_locks', spy)
    monkeypatch.setattr(Runner, 'run', lambda self: None)
    monkeypatch.setattr(Runner, 'recover_stop', lambda self: None)
    cfg = recordings_cfg(tmp_path)
    assert run_main(monkeypatch, tmp_path, cfg) == 0
    assert run_main(monkeypatch, tmp_path, cfg, '--recover-stop') == 0
    assert waits == [0, backup_runner.RECOVERY_LOCK_WAIT]


# spec: ANYM-727
def test_the_calendar_still_waits_for_the_host_lock_up_to_its_bound(tmp_path, monkeypatch):
    lock = tmp_path / 'm' / '.runner.lock'
    lock.parent.mkdir(mode=0o700)
    lock.touch()
    other = os.open(lock, os.O_RDWR)
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    clock, sleeps = [0.0], []
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(backup_runner, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep, time=lambda: clock[0]))
    (tmp_path / 'cal').mkdir(mode=0o700)
    try:
        with pytest.raises(RuntimeError, match='host_lock_busy'):
            with backup_runner.hold_locks({'profile': 'calendar', 'directory': str(tmp_path / 'cal'), 'host_lock': str(lock)}):
                pass
    finally:
        os.close(other)
    assert sum(sleeps) == backup_runner.HOST_LOCK_WAIT_SECONDS


# spec: ANYM-727
def test_a_receiver_that_is_busy_is_offered_the_same_bundle_again_within_the_bound(tmp_path, age_stub, monkeypatch):
    attempts = []
    async def busy_then_stored(path, result, transfer, timeout):
        attempts.append(result['id'])
        if len(attempts) == 1:
            raise RuntimeError('receiver_busy')
        return {**result, 'status': 'stored', 'duplicate': False}
    runner, docker, _ = snapshot_runner(tmp_path, age_stub, monkeypatch, send=busy_then_stored)
    runner.cfg['busy_wait_seconds'] = 1800
    runner.run()
    assert len(attempts) == 2 and attempts[0] == attempts[1] and status_of(runner)['state'] == 'ok'
    assert worst_case(runner.cfg) >= 60 + 1800, 'the busy wait is part of the bound the admission checks'
    assert clean(runner)


# spec: ANYM-727
def test_a_catch_up_after_a_skipped_day_makes_one_snapshot_and_a_healthy_day_none(tmp_path, age_stub, monkeypatch):
    runner, docker, delivered = snapshot_runner(tmp_path, age_stub, monkeypatch)
    docker.mode = 'disabled'
    with pytest.raises(Skip):
        runner.run()
    runner.recover_stop()
    assert 'last_snapshot_at' not in status_of(runner)
    docker.mode = 'ok'
    runner.run()
    assert len(delivered) == 1 and 'last_snapshot_at' in status_of(runner)
    assert status_of(runner)['state'] == 'ok'


# spec: ANYM-727
def test_a_reboot_in_the_middle_leaves_a_worker_and_plaintext_that_the_recovery_removes(tmp_path, age_stub, monkeypatch):
    runner, docker, delivered = snapshot_runner(tmp_path, age_stub, monkeypatch)
    docker.hold = True
    crash(runner)  # killed while the worker runs
    job = runner.journal['id']
    assert (runner.staging_root / job / 'archive.sqlite').exists() and runner.journal_path.exists()
    # The next boot: ExecStopPost's recovery, in a new process.
    fresh = Runner(runner.cfg, docker=docker, clock=lambda: NOW)
    fresh.container_uid, fresh.container_gid = os.getuid(), os.getgid()
    fresh.recover_stop()
    assert clean(fresh) and delivered == [] and ('stop', '--time', '25', 'deedkit-recordings-backup-' + job.lower()) in docker.calls
    assert not Path(fresh.unsettled_path).exists()


# spec: ANYM-727
def test_a_cleanup_that_fails_is_reported_and_never_hides_the_stopped_worker_or_blocks_the_next_start(tmp_path, age_stub, monkeypatch):
    runner, docker, delivered = snapshot_runner(tmp_path, age_stub, monkeypatch)
    docker.hold = True
    crash(runner)
    job = runner.journal['id']
    runner.clean_staging = lambda: (_ for _ in ()).throw(OSError('read-only filesystem'))
    with pytest.raises(OSError, match='read-only'):
        runner.recover_stop()
    assert ('stop', '--time', '25', 'deedkit-recordings-backup-' + job.lower()) in docker.calls
    assert status_of(runner)['error_code'] == 'staging_cleanup_failed'
    assert (runner.staging_root / job / 'archive.sqlite').exists(), 'the plaintext is still there, and the status says so'
    # The next start empties what was left and delivers a new snapshot under a new ID.
    del runner.clean_staging
    docker.hold = False
    runner.sleep = lambda seconds: None
    runner.recover_stop()
    runner.run()
    assert len(delivered) == 1 and clean(runner)


# spec: ANYM-727
@pytest.mark.parametrize('scenario', ['skip_disk', 'skip_down', 'failed_exec', 'success', 'yield'])
def test_in_every_outcome_the_host_lock_is_released_and_the_run_ends_before_the_next_other_profile_start(
        tmp_path, age_stub, monkeypatch, scenario):
    runner, docker, _ = snapshot_runner(tmp_path, age_stub, monkeypatch)
    lock = Path(runner.cfg['host_lock'])
    if scenario == 'skip_disk':
        runner.disk = lambda path: SimpleNamespace(free=GIB // 2)
    elif scenario == 'skip_down':
        docker.containers[CONTAINER]['State']['Running'] = False
    elif scenario == 'failed_exec':
        docker.mode = 'failed'
    elif scenario == 'yield':
        docker.hold = True
        runner.sleep = lambda seconds: runner.now.__setitem__(0, at(15, 50))
    with backup_runner.hold_locks(runner.cfg, 0):
        assert held(lock)
        try:
            runner.run()
        except (Skip, RuntimeError):
            runner.recover_stop()
    assert not held(lock), 'messaging and the calendar can take the lock at once'
    assert clean(runner)
    if scenario == 'yield':
        assert status_of(runner)['error_code'] == 'recordings_skipped_yielded'  # the clock was stepped past the window
    else:
        assert runner.now[0] <= at(16) - GUARD_SECONDS, 'it ended before the next start minus the guard'


# --- units, example config, compose, docs ---------------------------------------------------------------------------------

def unit(name):
    parser = configparser.ConfigParser(interpolation=None, strict=False, delimiters=('=',))
    parser.optionxform = lambda optionstr: optionstr
    parser.read_string((DEPLOY / name).read_text())
    return parser


def seconds(value):
    import re
    match = re.fullmatch(r'(\d+)(s|min|h)?', value)
    assert match is not None
    return int(match[1]) * {None: 1, 's': 1, 'min': 60, 'h': 3600}[match[2]]


def example(tmp_path):
    path = tmp_path / 'runner.json'
    path.write_text((DEPLOY / 'backup-runner-recordings.example.json').read_text())
    path.chmod(0o600)
    return backup_runner.config(path)


def hms(timer):
    value = unit(timer)['Timer']['OnCalendar'].removesuffix(' UTC')
    h, m, s = value.removeprefix('*-*-* ').split(':')
    return int(h) * HOUR + int(m) * 60 + int(s)


# spec: ANYM-727
def test_the_example_config_loads_and_names_the_timers_of_the_profiles_it_must_not_collide_with(tmp_path):
    cfg = example(tmp_path)
    assert cfg['profile'] == 'recordings' and cfg['host_lock'] == '/opt/personal-os-probe/backups/.runner.lock'
    assert {Path(t).name for t in cfg['recordings']['timers']} == {
        'personal-os-backup.timer', 'deedkit-calendar-backup.timer', 'deedkit-calendar-backup-catchup.timer'}
    assert cfg['recordings']['container_staging'] == '/backup-staging'
    assert Path(cfg['recordings']['staging_directory']).parent == Path(cfg['directory']).parent


# spec: ANYM-727
@pytest.mark.parametrize('change', [
    {'container': 'a b'}, {'container_config': 'relative.json'}, {'container_config': '/a/../b'},
    {'container_staging': '/x//y'}, {'timers': []}, {'timers': ['/etc/x.service']}, {'timers': 'one.timer'},
    {'timers': ['/etc/a.timer', '/etc/a.timer']}, {'exec_timeout_seconds': 5}, {'hash_timeout_seconds': 0},
    {'min_free_bytes': 1}, {'expected_archive_bytes': 0}, {'integrity_timeout_seconds': 1200, 'encrypt_timeout_seconds': 600},
    {'unknown_key': 1}])
def test_the_new_config_keys_are_validated(tmp_path, change):
    cfg = recordings_cfg(tmp_path)
    cfg['recordings'].update({'container': 'recordings-api-1', **change})
    with pytest.raises(ValueError):
        backup_runner.recordings_config(copy.deepcopy(cfg))


# spec: ANYM-727
def test_a_timer_file_inside_staging_state_or_the_job_directory_is_refused(tmp_path):
    for inside in ('staging', 'state', 'recordings'):
        cfg = recordings_cfg(tmp_path)
        cfg['recordings']['timers'] = [str(tmp_path / inside / 'x.timer')]
        with pytest.raises(ValueError, match='invalid_backup_path'):
            backup_runner.recordings_config(cfg)


# spec: ANYM-727
def test_the_service_runs_the_runner_with_the_examples_directory_and_recovers_with_the_same_config(tmp_path):
    cfg = example(tmp_path)
    for name in ('deedkit-recordings-backup.service', 'deedkit-recordings-backup-catchup.service'):
        service = unit(name)['Service']
        start = shlex.split(service['ExecStart'])
        assert start[:4] == ['/usr/bin/python3', '-I', '/opt/personal-os-probe/ops/backup_runner.py', '--config']
        config = Path(start[4])
        assert config.parent == Path(cfg['directory']).parent and config.name == 'runner.json' and len(start) == 5
        stop = service['ExecStopPost'].replace('$$', '$')
        command = shlex.split(stop)[2].split('|| exec ', 1)[1] if stop.startswith('/bin/sh -c') else stop
        assert shlex.split(command)[-3:] == ['--config', str(config), '--recover-stop']
        assert service['Type'] == 'oneshot' and service['User'] == 'root' and service['UMask'] == '0077'
        assert 'Restart' not in service
        paths = set(service['ReadWritePaths'].split())
        assert str(Path(cfg['directory']).parent) in paths and str(Path(cfg['host_lock']).parent) in paths
        for sandbox in ('NoNewPrivileges', 'PrivateTmp', 'ProtectHome', 'ProtectSystem'):
            assert sandbox in service
        assert seconds(service['TimeoutStartSec']) >= worst_case(cfg) - RECOVERY_BUDGET
        assert seconds(service['TimeoutStopSec']) >= RECOVERY_BUDGET


# spec: ANYM-727
def test_the_daily_slot_follows_the_calendar_catch_ups_worst_case_and_the_whole_run_ends_before_the_next_start(tmp_path):
    cfg = example(tmp_path)
    slot = hms('deedkit-recordings-backup.timer')
    calendar_catchup = unit('deedkit-calendar-backup-catchup.service')['Service']
    catchup_worst_end = (hms('deedkit-calendar-backup-catchup.timer') + seconds(calendar_catchup['TimeoutStartSec']) +
                         2 * seconds(calendar_catchup['TimeoutStopSec']))
    assert slot >= catchup_worst_end + GUARD_SECONDS
    messaging, calendar = 18 * HOUR, hms('deedkit-calendar-backup.timer')
    assert slot > messaging and slot > calendar
    nxt = min(calendar + 86400, messaging + 86400, hms('deedkit-calendar-backup-catchup.timer') + 86400)
    assert slot + worst_case(cfg) <= nxt - GUARD_SECONDS
    # The catch-up slot is after the daily one's worst case, and it also ends before the next start.
    retry = hms('deedkit-recordings-backup-catchup.timer')
    assert retry >= slot + worst_case(cfg) and retry + worst_case(cfg) <= nxt - GUARD_SECONDS


# spec: ANYM-727
def test_the_timers_are_persistent_with_one_second_accuracy_and_install_into_timers_target():
    daily, retry = unit('deedkit-recordings-backup.timer'), unit('deedkit-recordings-backup-catchup.timer')
    for timer, name in ((daily, 'deedkit-recordings-backup.service'), (retry, 'deedkit-recordings-backup-catchup.service')):
        assert timer['Timer']['OnCalendar'].endswith(' UTC') and timer['Timer']['Persistent'] == 'true'
        assert timer['Timer']['AccuracySec'] == '1s' and timer['Timer']['Unit'] == name
        assert timer['Install']['WantedBy'] == 'timers.target'


# spec: ANYM-727
def test_the_catch_up_runs_only_in_the_late_evening_and_only_for_a_snapshot_older_than_22_hours(tmp_path):
    text = (DEPLOY / 'deedkit-recordings-backup-catchup.service').read_text()
    conditions = [line for line in text.splitlines() if line.startswith('ExecCondition=')]
    assert len(conditions) == 2
    shell = [shlex.split(line.split('=', 1)[1])[2].replace('$$', '$').replace('%%', '%') for line in conditions]
    import subprocess
    for hhmm, ok in (('2259', False), ('2300', True), ('2359', True), ('0830', False), ('bad', False)):
        fake = tmp_path / f'date{hhmm}'
        fake.write_text(f'#!/bin/sh\necho {hhmm}\n')
        fake.chmod(0o700)
        script = shell[0].replace('date -u', str(fake))
        assert (subprocess.run(['/bin/sh', '-c', script]).returncode == 0) is ok
    assert '--max-age-hours 22' in shell[1] and '/backups/status.json' in shell[1]
    assert '[ "$r" -eq 4 ] || [ "$r" -eq 5 ]' in shell[1], 'stale or unknown runs; a fresh snapshot does not'


# spec: ANYM-727
def test_the_compose_overlay_mounts_a_writable_staging_directory_at_the_path_the_runner_execs_into(tmp_path):
    document = yaml.safe_load((DEPLOY / 'compose.recordings.yaml').read_text())
    volumes = document['services']['recordings-api']['volumes']
    (staging,) = [v for v in volumes if v.endswith(':/backup-staging')]
    cfg = example(tmp_path)
    assert staging == '${RECORDINGS_BACKUP_STAGING:-' + cfg['recordings']['staging_directory'] + '}:/backup-staging'
    assert not staging.endswith(':ro') and cfg['recordings']['container_staging'] == '/backup-staging'
    assert cfg['recordings']['container'] == 'deedkit-recordings-recordings-api-1'
    assert document['services']['recordings-api']['read_only'] is True
    assert document['services']['recordings-api']['user'] == f'{backup_runner.CONTAINER_UID}:{backup_runner.CONTAINER_UID}'
    assert 'recordings-data:/data' in volumes and cfg['recordings']['container_config'] == '/run/deedkit/recordings/config.json'


# spec: ANYM-727
def test_the_documentation_names_the_staging_path_ownership_slot_admission_and_independence():
    backup, production = (ROOT / 'BACKUP.md').read_text(), (ROOT / 'docs/operations/production.md').read_text()
    for text in (backup, production):
        for needle in ('/opt/deedkit/recordings-backup/staging', '10001', '21:30', '23:15', 'recordings_skipped_window'):
            assert needle in text, needle
    assert 'independent' in backup and 'archive is off' in backup.lower()
    assert 'expected archive size' in backup.lower() or 'expected_archive_bytes' in backup
    assert '## Recordings' in production and 'deedkit-recordings-backup.timer' in production
