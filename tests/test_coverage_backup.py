"""Backup contract gaps, with disposable files and synthetic captures only.

Boundary tests model the worker/host control protocol. The age test runs the
actual encryption binary; neither class claims a live off-site delivery.
"""
import hashlib
import asyncio
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from messaging_service import backup_receiver as receiver
from messaging_service import backup_runner as runner_module
from messaging_service import backup_worker as worker
from messaging_service.backup import create_snapshot, quarantine_restore, verify_bundle, verify_snapshot
from messaging_service.backup_crypto import (
    decrypt_snapshot, encrypt_snapshot, encrypt_snapshot_stream, snapshot_stream_bound,
)
from messaging_service.store import Store
from test_backup_operations import packet, runner_fixture
from test_backup_streaming import reference_fixture, streaming_packet
from test_recovery import recovery_fixture

BACKUP_ID = '20261015T053000Z-0123456789abcdef'


def _runner_config(tmp_path):
    root = tmp_path.resolve()
    return {
        'directory': str(root / 'jobs'), 'project': 'synthetic-project',
        'network': 'synthetic-backend', 'image': 'synthetic/backup:fixture',
        'files_volume': 'synthetic-files', 'dsn_file': str(root / 'dsn'),
        'recipient_file': str(root / 'recipient'), 'mounts': [],
        'recovery': {'accounts': [], 'sources': []},
        'transfer': {'host': 'fixture.invalid', 'user': 'backup',
                     'identity_file': str(root / 'identity'),
                     'known_hosts_file': str(root / 'known-hosts')},
        'timeout_seconds': 60, 'keep_local': 2,
    }


@pytest.mark.parametrize('attack,code', [
    ('extra', 'invalid_backup_config'), ('mode', 'invalid_backup_config'),
    ('project', 'invalid_backup_config'), ('network', 'invalid_backup_config'),
    ('image', 'invalid_backup_config'), ('volume', 'invalid_backup_config'),
    ('relative', 'invalid_backup_path'), ('parent', 'invalid_backup_path'),
    ('symlink_parent', 'invalid_backup_path'), ('mount_duplicate', 'invalid_backup_mount'),
    ('mount_path', 'invalid_backup_mount'), ('ssh_fields', 'invalid_backup_transfer'),
    ('ssh_host', 'invalid_backup_transfer'), ('ssh_path', 'invalid_backup_transfer'),
    ('timeout_low', 'invalid_backup_timeout'), ('timeout_high', 'invalid_backup_timeout'),
    ('timeout_bool', 'invalid_backup_timeout'), ('retention_low', 'invalid_backup_retention'),
    ('retention_high', 'invalid_backup_retention'), ('retention_bool', 'invalid_backup_retention'),
])
def test_runner_rejects_untrusted_config_before_starting_any_worker(tmp_path, attack, code):
    cfg = _runner_config(tmp_path)
    if attack == 'extra': cfg['ssh_private_key'] = 'MUST_NOT_BE_A_WORKER_CONFIG'
    elif attack == 'mode': cfg['delivery_mode'] = 'unknown'
    elif attack in ('project', 'network', 'image'): cfg[attack] = 'unsafe value;exit'
    elif attack == 'volume': cfg['files_volume'] = 'unsafe/volume'
    elif attack == 'relative': cfg['dsn_file'] = 'relative'
    elif attack == 'parent': cfg['dsn_file'] = str(tmp_path.resolve() / '..' / 'outside')
    elif attack == 'symlink_parent':
        alias = tmp_path / 'alias'; alias.symlink_to(tmp_path.resolve(), target_is_directory=True)
        cfg['dsn_file'] = str(alias / 'dsn')
    elif attack.startswith('mount_'):
        mount = {'name': 'sessions', 'source': str(tmp_path.resolve())}
        cfg['mounts'] = [mount, dict(mount)] if attack == 'mount_duplicate' else [{**mount, 'source': 'relative'}]
    elif attack == 'ssh_fields': cfg['transfer'].pop('known_hosts_file')
    elif attack == 'ssh_host': cfg['transfer']['host'] = 'fixture.invalid;touch marker'
    elif attack == 'ssh_path': cfg['transfer']['identity_file'] = 'relative'
    elif attack.startswith('timeout_'): cfg['timeout_seconds'] = {'timeout_low': 59, 'timeout_high': 7201, 'timeout_bool': True}[attack]
    else: cfg['keep_local'] = {'retention_low': 0, 'retention_high': 25, 'retention_bool': True}[attack]
    path = tmp_path / 'config.json'; runner_module.write_json(path, cfg)
    with pytest.raises(ValueError, match=code): runner_module.config(path)
    assert not (tmp_path / 'jobs').exists()


def test_backup_worker_container_has_only_readonly_sources_public_recipient_and_private_tmpfs(tmp_path):
    run, docker = runner_fixture(tmp_path)
    run.prepare(); run.create_worker(tmp_path / run.journal['id'])
    create = next(call for call in docker.calls if call[0] == 'create')
    mounts = [create[index + 1] for index, value in enumerate(create) if value == '--mount']
    assert '--read-only' in create and create[create.index('--memory') + 1] == '384m'
    assert create[create.index('--cpus') + 1] == '0.75' and create[create.index('--pids-limit') + 1] == '64'
    assert create[create.index('--tmpfs') + 1] == '/tmp:size=32m,noexec,nosuid'
    assert create[create.index('--security-opt') + 1] == 'no-new-privileges'
    assert create[create.index('--cap-drop') + 1] == 'ALL'
    assert all('readonly' in mount for mount in mounts if ',dst=/work' not in mount)
    assert any('dst=/run/secrets/recipient,readonly' in mount for mount in mounts)
    assert not any('docker.sock' in mount or 'identity' in mount or 'known-host' in mount for mount in mounts)
    config = runner_module.read_json(tmp_path / run.journal['id'] / 'worker.json')
    assert set(config) == {'id', 'dsn_file', 'files_directory', 'recovery_config_file', 'recipient_file', 'work_directory'}
    assert config['recipient_file'] == '/run/secrets/recipient'


def test_docker_selection_excludes_oneoffs_and_unrelated_services(monkeypatch):
    docker = runner_module.Docker()
    def info(identifier, service, **labels):
        return {'Id': identifier, 'Config': {'Labels': {
            'com.docker.compose.service': service,
            **labels}}}
    records = {
        'tg': info('tg', 'telegram'),
        'wa': info('wa', 'whatsapp2'),
        'labelled': info('labelled', 'custom-collector', **{runner_module.COLLECTOR_LABEL: 'true'}),
        'oneoff': info('oneoff', 'telegram', **{'com.docker.compose.oneoff': 'True'}),
        'gateway': info('gateway', 'gateway'),
    }
    monkeypatch.setattr(docker, 'call', lambda *args: ' '.join(records))
    monkeypatch.setattr(docker, 'inspect', records.get)
    assert docker.running_collectors('synthetic') == [
        {'id': 'tg', 'service': 'telegram'}, {'id': 'wa', 'service': 'whatsapp2'},
        {'id': 'labelled', 'service': 'custom-collector'}]


def test_runner_verifies_staged_ciphertext_before_upload_and_preserves_completed_jobs(tmp_path):
    run, docker = runner_fixture(tmp_path)
    run.prepare(); work = tmp_path / run.journal['id']; run.collect(work)
    (work / 'bundle.tar.age').write_bytes(b'CORRUPT_SYNTHETIC_CIPHERTEXT')
    uploads = []
    async def send(*args): uploads.append(args)
    run.send = send
    with pytest.raises(RuntimeError, match='local_backup_corrupt'): run.run()
    assert uploads == [] and runner_module.read_json(tmp_path / 'status.json')['error_code'] == 'local_backup_corrupt'
    assert (tmp_path / 'journal.json').exists() and docker.containers['tg-id']['State']['Running']


def test_runner_retains_only_completed_local_jobs_and_records_integrity_metadata(tmp_path):
    run, _ = runner_fixture(tmp_path)
    completed = []
    for index in range(4):
        old = tmp_path / f'20200101T00000{index}Z-0123456789abcdef'; old.mkdir(mode=0o700)
        runner_module.write_json(old / 'delivery.json', {'status': 'stored'})
        completed.append(old)
    unfinished = tmp_path / '20200101T000005Z-0123456789abcdef'; unfinished.mkdir(mode=0o700)
    run.run()
    status = runner_module.read_json(tmp_path / 'status.json')
    assert all(status[key] for key in ('last_success_at', 'last_success_id', 'ciphertext_bytes',
                                       'ciphertext_sha256', 'last_snapshot_at'))
    assert not any(path.exists() for path in completed[:-1]) and completed[-1].exists()
    assert unfinished.exists()
    assert len([path for path in tmp_path.iterdir() if path.is_dir() and (path / 'delivery.json').exists()]) == 2


def test_upload_uses_strict_host_binding_and_never_forwards_agent_or_selects_remote_shell(tmp_path, monkeypatch):
    executable = tmp_path / 'ssh'; arguments = tmp_path / 'arguments.json'
    executable.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
        'assert "SSH_AUTH_SOCK" not in os.environ\n'
        'open(' + repr(str(arguments)) + ',"w").write(json.dumps(sys.argv[1:]))\n'
        'header=json.loads(sys.stdin.buffer.readline());data=sys.stdin.buffer.read()\n'
        'print(json.dumps({**header,"status":"stored"}))\n')
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('SSH_AUTH_SOCK', 'MUST_NOT_BE_FORWARDED')
    _, payload, receipt = packet(); ciphertext = tmp_path / 'ciphertext'; ciphertext.write_bytes(payload)
    transfer = _runner_config(tmp_path)['transfer']
    assert asyncio.run(runner_module.upload(ciphertext, receipt, transfer, 5))['status'] == 'stored'
    args = json.loads(arguments.read_text())
    assert args[:3] == ['-F', '/dev/null', '-T'] and args[-1] == 'backup-put-v1'
    for option in ('BatchMode=yes', 'IdentitiesOnly=yes', 'StrictHostKeyChecking=yes',
                   'PasswordAuthentication=no', 'KbdInteractiveAuthentication=no'):
        assert ['-o', option] == args[args.index(option) - 1:args.index(option) + 1]
    assert '-A' not in args and args[-2] == 'backup@fixture.invalid'


@pytest.mark.parametrize('attack', ['unsupported', 'missing', 'multiple_hosts', 'port', 'newline'])
def test_dump_credentials_rejects_extra_connection_options_and_unsafe_pgpass_fields(tmp_path, attack):
    dsn = 'host=db port=5432 dbname=synthetic user=owner password=fixture'
    if attack == 'unsupported': dsn += ' options=-cstatement_timeout=0'
    elif attack == 'missing': dsn = 'host=db port=5432 user=owner password=fixture'
    elif attack == 'multiple_hosts': dsn = dsn.replace('host=db', 'host=db,other')
    elif attack == 'port': dsn = dsn.replace('port=5432', 'port=service')
    else: dsn = dsn.replace('password=fixture', "password='fixture\nsecret'")
    with pytest.raises(ValueError): worker.dump_credentials(dsn, tmp_path)
    assert not (tmp_path / 'pgpass').exists()


def _worker_setup(tmp_path, monkeypatch, mode='staged_v1'):
    work = tmp_path / 'work'; work.mkdir(mode=0o700)
    dsn = tmp_path / 'dsn'; dsn.write_text('host=db port=5432 user=owner password=synthetic dbname=synthetic')
    recipient = tmp_path / 'recipient'; recipient.write_text('PUBLIC_RECIPIENT_FIXTURE')
    recovery = tmp_path / 'recovery.json'; worker.write_json(recovery, {'accounts': [], 'sources': []})
    cfg = {'id': BACKUP_ID, 'dsn_file': str(dsn), 'files_directory': str(tmp_path / 'files'),
           'recovery_config_file': str(recovery), 'recipient_file': str(recipient),
           'work_directory': str(work), 'mode': mode}
    calls = []
    class SyntheticStore:
        def __init__(self, received_dsn, files, url):
            assert received_dsn == dsn.read_text(); self.files = files
        def close(self): calls.append('closed')
    monkeypatch.setattr(worker, 'Store', SyntheticStore)
    def capture(store, destination, command, config, *, captured_callback, reference_blobs, backup_fence):
        assert isinstance(store, SyntheticStore) and reference_blobs is (mode == 'stream_v2')
        assert backup_fence == BACKUP_ID
        assert 'password=' not in ' '.join(command)
        password_path = Path(os.environ['PGPASSFILE'])
        assert password_path.is_file() and password_path.stat().st_mode & 0o777 == 0o600
        assert 'synthetic' in password_path.read_text()
        calls.append('captured')
        destination.mkdir(); (destination / 'manifest.json').write_text(json.dumps({
            'captured_at': '2026-10-15T05:30:00+08:00', 'backup_fence': backup_fence}))
        captured_callback()
        return {'status': 'snapshot_created', 'tables': 0, 'blobs': 0, 'blob_bytes': 0}
    monkeypatch.setattr(worker, 'create_snapshot', capture)
    return cfg, work, calls


def test_staged_worker_signals_capture_before_encoding_and_restores_pgpass_environment(tmp_path, monkeypatch):
    cfg, work, calls = _worker_setup(tmp_path, monkeypatch)
    monkeypatch.setenv('PGPASSFILE', 'EXISTING_SYNTHETIC_SETTING')
    def encode(snapshot, output, recipient, *, consume_staged_blobs):
        assert worker.private_json(work / 'captured.json') == {'id': BACKUP_ID, 'status': 'capture_complete'}
        assert os.environ['PGPASSFILE'] == 'EXISTING_SYNTHETIC_SETTING'
        assert consume_staged_blobs is True and recipient == 'PUBLIC_RECIPIENT_FIXTURE'
        calls.append('encoded'); output.write_bytes(receiver.AGE_HEADER + b'SYNTHETIC_CIPHERTEXT' * 4)
    monkeypatch.setattr(worker, 'encrypt_snapshot', encode)
    result = worker.run(cfg)
    assert calls == ['captured', 'encoded', 'closed']
    assert result['backup_fence'] == BACKUP_ID
    assert result['sha256'] == hashlib.sha256((work / 'bundle.tar.age').read_bytes()).hexdigest()
    assert result['bytes'] == (work / 'bundle.tar.age').stat().st_size
    assert worker.private_json(work / 'result.json') == result
    assert (work / 'result.json').stat().st_mode & 0o777 == 0o600
    assert not list(work.glob('.status-*'))


@pytest.mark.parametrize('action', ['skip', 'start', 'unsafe'])
def test_stream_worker_reuses_verified_capture_and_requires_host_control(tmp_path, monkeypatch, action):
    cfg, work, calls = _worker_setup(tmp_path, monkeypatch, 'stream_v2')
    snapshot = work / 'snapshot'; snapshot.mkdir()
    manifest = {'schema_version': 34, 'tables': {}, 'blobs': [],
        'captured_at': '2026-10-15T05:30:00+08:00', 'backup_fence': BACKUP_ID}
    (snapshot / 'manifest.json').write_text(json.dumps(manifest))
    def validate_fence(store, received_manifest, token):
        assert store.files == Path(cfg['files_directory'])
        assert received_manifest == manifest and token == BACKUP_ID
        calls.append('fence_validated')
    monkeypatch.setattr(worker, 'validate_reference_fence', validate_fence)
    def verify(path, *, blob_root):
        assert path == snapshot; calls.append('verified'); return manifest
    monkeypatch.setattr(worker, 'verify_bundle', verify)
    monkeypatch.setattr(worker, 'snapshot_stream_bound', lambda *_: 4096)
    worker.write_json(work / 'stream-start.json', {'action': action})
    fifo_open = Path.open
    def opened(path, *args, **kwargs):
        if path == work / 'ciphertext.fifo':
            assert stat.S_ISFIFO(path.lstat().st_mode)
            assert path.stat().st_mode & 0o777 == 0o600
            return io.BytesIO()
        return fifo_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', opened)
    def encode(path, sink, recipient, *, blob_root, expected_manifest_sha256):
        assert worker.private_json(work / 'captured.json')['status'] == 'capture_complete'
        assert expected_manifest_sha256 == hashlib.sha256((snapshot / 'manifest.json').read_bytes()).hexdigest()
        calls.append('encoded'); return {'bytes': 128, 'sha256': 'a' * 64}
    monkeypatch.setattr(worker, 'encrypt_snapshot_stream', encode)
    if action == 'unsafe':
        with pytest.raises(ValueError, match='invalid_backup_stream_control'): worker.run(cfg)
    else:
        result = worker.run(cfg)
        assert result['status'] == ('already_delivered' if action == 'skip' else 'encrypted')
    assert calls == (['fence_validated', 'verified', 'encoded', 'closed'] if action == 'start'
        else ['fence_validated', 'verified', 'closed'])
    metadata = worker.private_json(work / 'stream.json')
    assert metadata['id'] == metadata['backup_fence'] == BACKUP_ID and metadata['max_bytes'] == 4096
    assert metadata['snapshot_sha256'] == hashlib.sha256((snapshot / 'manifest.json').read_bytes()).hexdigest()
    assert (work / 'result.json').exists() is (action == 'start')
    assert not (work / 'bundle.tar.age').exists()


@pytest.mark.parametrize('attack', ['missing', 'extra', 'mode', 'id', 'public_work', 'symlink_work'])
def test_worker_rejects_invalid_configuration_before_reading_credentials(tmp_path, monkeypatch, attack):
    cfg, work, calls = _worker_setup(tmp_path, monkeypatch)
    if attack == 'missing': cfg.pop('recipient_file')
    elif attack == 'extra': cfg['ssh_identity'] = 'unsafe'
    elif attack == 'mode': cfg['mode'] = 'invalid'
    elif attack == 'id': cfg['id'] = '../outside'
    elif attack == 'public_work': work.chmod(0o755)
    else:
        alias = tmp_path / 'alias'; alias.symlink_to(work, target_is_directory=True); cfg['work_directory'] = str(alias)
    # A failed path/shape check must not open this deliberately absent DSN.
    cfg['dsn_file'] = str(tmp_path / 'absent')
    with pytest.raises(ValueError): worker.run(cfg)
    assert calls == [] and not (work / 'result.json').exists()


def test_worker_failure_restores_pgpass_and_reports_no_secret_details(tmp_path, monkeypatch, capsys):
    cfg, work, calls = _worker_setup(tmp_path, monkeypatch)
    monkeypatch.delenv('PGPASSFILE', raising=False)
    def failed_capture(*args, **kwargs): raise RuntimeError('SECRET_DATABASE_ERROR')
    monkeypatch.setattr(worker, 'create_snapshot', failed_capture)
    path = tmp_path / 'worker.json'; worker.write_json(path, cfg)
    monkeypatch.setattr(sys, 'argv', ['backup-worker', '--config', str(path)])
    # Do not change the test process umask when exercising CLI error masking.
    monkeypatch.setattr(worker.os, 'umask', lambda _: None)
    with pytest.raises(SystemExit) as exc: worker.main()
    assert exc.value.code == 1 and 'PGPASSFILE' not in os.environ
    assert capsys.readouterr().out == '{"status":"error","code":"backup_worker_failed"}\n'
    assert calls == ['closed'] and not (work / 'result.json').exists()


@pytest.mark.parametrize('version', [1, 2])
def test_receiver_enforces_header_and_free_disk_bounds_without_publishing(tmp_path, monkeypatch, version):
    oversized = io.BytesIO(b'x' * 4096 + b'\n')
    with pytest.raises(ValueError, match='invalid_header'): receiver.receive(tmp_path, oversized, reserve_bytes=0)
    stream = packet()[0] if version == 1 else streaming_packet()[0]
    monkeypatch.setattr(receiver.shutil, 'disk_usage', lambda _: type('Usage', (), {'free': 32})())
    with pytest.raises(RuntimeError, match='receiver_disk_reserve'):
        receiver.receive(tmp_path, stream, reserve_bytes=1024)
    assert not list(tmp_path.glob('*.tar.age')) and not list(tmp_path.glob('.incoming-*'))


@pytest.mark.parametrize('attack', ['relative', 'public', 'symlink'])
def test_receiver_rejects_untrusted_destination(tmp_path, attack):
    destination = tmp_path
    if attack == 'relative': destination = Path('relative')
    elif attack == 'public': tmp_path.chmod(0o755)
    else:
        destination = tmp_path / 'alias'; destination.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError): receiver.receive(destination, packet()[0], reserve_bytes=0)
    assert not list(tmp_path.glob('*.tar.age'))


def test_receiver_cli_ignores_original_command_and_uses_fixed_limits_and_deadline(tmp_path, monkeypatch, capsys):
    seen = []; alarms = []
    monkeypatch.setenv('SSH_ORIGINAL_COMMAND', '--prune; cat PRIVATE_SYNTHETIC_FILE')
    monkeypatch.setattr(sys, 'argv', ['receiver', '--directory', str(tmp_path)])
    monkeypatch.setattr(receiver.os, 'umask', lambda _: None)
    monkeypatch.setattr(receiver.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(receiver.signal, 'alarm', alarms.append)
    def accept(directory, source, **kwargs):
        seen.append(kwargs); return {'status': 'stored'}
    monkeypatch.setattr(receiver, 'receive', accept)
    monkeypatch.setattr(receiver, 'prune', lambda *_: pytest.fail('SSH_ORIGINAL_COMMAND selected maintenance'))
    receiver.main()
    assert len(seen) == 1 and seen[0]['max_bytes'] == 20 * 1024**3
    assert seen[0]['quota_bytes'] == 30 * 1024**3 and alarms == [900, 0]
    assert json.loads(capsys.readouterr().out)['status'] == 'stored'


@pytest.mark.parametrize('uid,marker', [(501, True), (0, False)])
def test_receiver_cli_refuses_prune_without_root_and_explicit_anchor(tmp_path, monkeypatch, capsys, uid, marker):
    argv = ['receiver', '--directory', str(tmp_path), '--prune']
    if marker: argv += ['--protected-file', str(tmp_path / 'anchor')]
    monkeypatch.setattr(sys, 'argv', argv)
    monkeypatch.setattr(receiver.os, 'umask', lambda _: None)
    monkeypatch.setattr(receiver.os, 'geteuid', lambda: uid)
    monkeypatch.setattr(receiver.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(receiver.signal, 'alarm', lambda _: None)
    monkeypatch.setattr(receiver, 'prune', lambda *_args, **_kwargs: pytest.fail('unauthorized maintenance'))
    with pytest.raises(SystemExit): receiver.main()
    assert json.loads(capsys.readouterr().out) == {'v': 1, 'status': 'error', 'code': 'receiver_failed'}


def _root_marker(monkeypatch, marker):
    original = Path.stat
    def root_owned(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == marker:
            values = list(info); values[4] = 0; return os.stat_result(values)
        return info
    monkeypatch.setattr(Path, 'stat', root_owned)


@pytest.mark.parametrize('attack', ['empty', 'nonroot', 'writable', 'oversized', 'corrupt', 'too_many_anchors', 'bad_cap'])
def test_retention_rejects_invalid_restore_anchors_without_any_deletion(tmp_path, monkeypatch, attack):
    originals = []
    for index in range(4):
        path = tmp_path / (f'20261015T05000{index}Z-0123456789abcdef.tar.age')
        path.write_bytes(receiver.AGE_HEADER + b'SYNTHETIC_CIPHERTEXT' * 4); originals.append(path)
    marker = tmp_path / 'anchors.json'
    anchors = [{'id': path.name[:-8], 'bytes': path.stat().st_size,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in originals[:2]]
    runner_module.write_json(marker, [] if attack == 'empty' else anchors)
    _root_marker(monkeypatch, marker)
    cap = 3
    if attack == 'nonroot':
        root_stat = Path.stat
        def unowned(path, *args, **kwargs):
            info = root_stat(path, *args, **kwargs)
            if path == marker:
                values = list(info); values[4] = 501; return os.stat_result(values)
            return info
        monkeypatch.setattr(Path, 'stat', unowned)
    elif attack == 'writable': marker.chmod(0o622)
    elif attack == 'oversized': marker.write_bytes(b'x' * 65537)
    elif attack == 'corrupt': originals[0].write_bytes(b'CORRUPT')
    elif attack == 'too_many_anchors': cap = 1
    elif attack == 'bad_cap': cap = 4
    before = {path.name: path.read_bytes() for path in originals}
    with pytest.raises(ValueError): receiver.prune(tmp_path, marker, max_copies=cap)
    assert {path.name: path.read_bytes() for path in tmp_path.glob('*.tar.age')} == before


def test_retention_removes_receipts_and_reports_released_bytes(tmp_path, monkeypatch):
    paths = []
    for index in range(4):
        path = tmp_path / (f'20261015T05000{index}Z-0123456789abcdef.tar.age')
        path.write_bytes(receiver.AGE_HEADER + b'SYNTHETIC' * 10); os.utime(path, (index, index)); paths.append(path)
        runner_module.write_json(tmp_path / (path.name[:-8] + '.receipt.json'), {'id': path.name[:-8]})
    marker = tmp_path / 'anchors.json'
    runner_module.write_json(marker, [{'id': paths[0].name[:-8], 'bytes': paths[0].stat().st_size,
                                      'sha256': hashlib.sha256(paths[0].read_bytes()).hexdigest()}])
    _root_marker(monkeypatch, marker)
    released = paths[1].stat().st_size + paths[2].stat().st_size
    result = receiver.prune(tmp_path, marker, max_copies=2)
    assert result['kept'] == 2 and result['removed'] == 2 and result['released_bytes'] == released
    assert {path.name for path in tmp_path.glob('*.tar.age')} == {paths[0].name, paths[-1].name}
    assert {path.name for path in tmp_path.glob('*.receipt.json')} == {paths[0].name[:-8] + '.receipt.json', paths[-1].name[:-8] + '.receipt.json'}


def test_stream_tar_members_are_ustar_private_and_fit_admission_bound(tmp_path):
    capture, originals, manifest, encoder = reference_fixture(tmp_path)
    sink = io.BytesIO()
    encoded = encrypt_snapshot_stream(capture, sink, 'fixture', blob_root=originals, age=encoder)
    assert encoded['bytes'] == len(sink.getvalue()) <= snapshot_stream_bound(capture, originals)
    assert encoded['sha256'] == hashlib.sha256(sink.getvalue()).hexdigest()
    with tarfile.open(fileobj=io.BytesIO(sink.getvalue())) as archive:
        members = archive.getmembers()
        assert {member.name for member in members} == {'manifest.json', 'database.dump', 'files/' + manifest['blobs'][0]['digest']}
        assert all(member.isfile() and member.mode == 0o600 for member in members)
        assert archive.extractfile('files/' + manifest['blobs'][0]['digest']).read() == (originals / manifest['blobs'][0]['digest']).read_bytes()
    assert sink.getvalue()[257:265] == b'ustar\x0000'
    assert not list((capture / 'files').iterdir())


@pytest.mark.parametrize('attack', ['existing_output', 'symlink', 'directory'])
def test_staged_encryption_rejects_existing_output_and_nonregular_members(tmp_path, attack):
    capture, originals, manifest, encoder = reference_fixture(tmp_path)
    blob = manifest['blobs'][0]['digest']
    shutil.copyfile(originals / blob, capture / 'files' / blob)
    output = tmp_path / 'bundle'
    if attack == 'existing_output':
        output.write_bytes(b'OLD_VERIFIED_CIPHERTEXT_FIXTURE')
    else:
        (capture / 'files' / blob).unlink()
        if attack == 'symlink': (capture / 'files' / blob).symlink_to(originals / blob)
        else: (capture / 'files' / blob).mkdir()
    with pytest.raises((ValueError, OSError, RuntimeError)):
        encrypt_snapshot(capture, output, 'fixture', age=encoder)
    if attack == 'existing_output': assert output.read_bytes() == b'OLD_VERIFIED_CIPHERTEXT_FIXTURE'
    else: assert not output.exists()
    assert not list(tmp_path.glob('.encrypt-*'))


@pytest.mark.parametrize('member', ['manifest.json', 'database.dump'])
def test_bundle_verification_rejects_fifo_without_opening_a_blocking_reader(tmp_path, monkeypatch, member):
    capture, _, _, _ = reference_fixture(tmp_path)
    path = capture / member; path.unlink(); os.mkfifo(path, 0o600)
    original_os_open = os.open
    original_path_open = Path.open
    def safe_os_open(target, flags, *args, **kwargs):
        if Path(target) == path and not flags & os.O_NONBLOCK:
            pytest.fail('backup verification attempted blocking FIFO open')
        return original_os_open(target, flags, *args, **kwargs)
    def safe_path_open(target, *args, **kwargs):
        if target == path: pytest.fail('backup verification attempted blocking FIFO open')
        return original_path_open(target, *args, **kwargs)
    monkeypatch.setattr(os, 'open', safe_os_open)
    monkeypatch.setattr(Path, 'open', safe_path_open)
    with pytest.raises(ValueError, match='invalid_backup'):
        verify_bundle(capture)


def test_cold_capture_rejects_fifo_owner_lock_without_blocking(tmp_path, monkeypatch):
    from messaging_service.recovery_state import cold_session_lock
    session = tmp_path / 'synthetic.session'
    owner_lock = Path(str(session) + '.owner.lock'); os.mkfifo(owner_lock, 0o600)
    original = os.open
    def safe_open(target, flags, *args, **kwargs):
        if Path(target) == owner_lock and not flags & os.O_NONBLOCK:
            pytest.fail('cold capture attempted blocking FIFO lock open')
        return original(target, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', safe_open)
    with pytest.raises(ValueError, match='invalid_session_lock'):
        with cold_session_lock(session): pytest.fail('FIFO accepted as a session lock')
    assert stat.S_ISFIFO(owner_lock.lstat().st_mode)


def test_actual_age_encrypts_and_authenticates_synthetic_bundle(tmp_path):
    age = shutil.which('age'); keygen = shutil.which('age-keygen')
    if not age or not keygen:
        if os.environ.get('CI'): pytest.fail('CI must install age and age-keygen for the backup encryption proof')
        pytest.skip('actual age binaries unavailable locally; crypto proof must run in CI')
    identity = tmp_path / 'identity'
    subprocess.run([keygen, '-o', str(identity)], check=True, capture_output=True)
    recipient = subprocess.run([keygen, '-y', str(identity)], check=True, capture_output=True, text=True).stdout.strip()
    capture, originals, manifest, _ = reference_fixture(tmp_path)
    # Ordinary staged bundles own their copies. This is not provider data.
    for blob in manifest['blobs']: shutil.copyfile(originals / blob['digest'], capture / 'files' / blob['digest'])
    ciphertext = tmp_path / 'bundle.tar.age'
    encrypt_snapshot(capture, ciphertext, recipient, age=age)
    data = ciphertext.read_bytes()
    assert data.startswith(receiver.AGE_HEADER) and b'SYNTHETIC ORIGINAL' not in data
    assert ciphertext.stat().st_mode & 0o777 == 0o600
    decrypt_snapshot(ciphertext, identity, tmp_path / 'restored', age=age)
    assert verify_bundle(tmp_path / 'restored') == manifest
    for blob in manifest['blobs']:
        assert (tmp_path / 'restored/files' / blob['digest']).read_bytes() == (originals / blob['digest']).read_bytes()
    # Both an unavailable decryption key and authenticated-payload corruption
    # must leave no published restore directory.
    wrong_identity = tmp_path / 'wrong-identity'
    subprocess.run([keygen, '-o', str(wrong_identity)], check=True, capture_output=True)
    with pytest.raises((RuntimeError, tarfile.TarError)): decrypt_snapshot(ciphertext, wrong_identity, tmp_path / 'wrong', age=age)
    changed = tmp_path / 'changed.age'; changed.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    with pytest.raises((RuntimeError, tarfile.TarError)): decrypt_snapshot(changed, identity, tmp_path / 'corrupt', age=age)
    assert not (tmp_path / 'wrong').exists() and not (tmp_path / 'corrupt').exists()
    assert not list(tmp_path.glob('.decrypt-*'))


def test_native_quarantine_sets_outgoing_uncertainty_and_finish_time(database, tmp_path):
    base = database.pool.conninfo
    name = 'anymsg_coverage_' + uuid4().hex + '_restore_test'
    restored = None; created = False
    try:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        created = True
        restored = Store(make_conninfo(base, dbname=name), tmp_path / 'synthetic-files', 'http://testserver')
        restored.migrate(); restored.seed()
        token = restored.issue_key('synthetic-quarantine', ['telegram-demo'])
        key = restored.capabilities(token)['credential_id']
        with restored.pool.connection() as connection:
            for index, state in enumerate(('queued', 'dispatching', 'sent', 'unknown')):
                connection.execute('''INSERT INTO outgoing_messages(principal,key_id,scope_version,
                    request_id,request_hash,account,chat,provider_chat,body,status) VALUES(%s,%s,1,%s,%s,
                    'telegram-demo','telegram-demo-chat','1001','Synthetic outgoing fixture',%s)''',
                    ('synthetic-quarantine', key, 'quarantine-' + str(index), 'a' * 64, state))
        quarantine_restore(restored)
        with restored.pool.connection() as connection:
            rows = connection.execute('SELECT status,error_code,finished_at FROM outgoing_messages ORDER BY request_id').fetchall()
        assert [row['status'] for row in rows] == ['cancelled', 'unknown', 'sent', 'unknown']
        assert [row['error_code'] for row in rows] == ['restored_disabled', 'restored_disabled', None, None]
        assert all(row['finished_at'] is not None for row in rows[:2])
        assert all(row['finished_at'] is None for row in rows[2:])
    finally:
        if restored is not None: restored.close()
        if created:
            with psycopg.connect(base, autocommit=True) as connection:
                connection.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


def test_actual_postgres_age_receiver_restore_drill_preserves_source_and_quarantines(seeded, tmp_path):
    """Real PG18 and age, local forced-command protocol, never a provider session."""
    container = os.environ.get('ANYMSG_TEST_POSTGRES_CONTAINER')
    age = shutil.which('age'); keygen = shutil.which('age-keygen')
    if not container or not age or not keygen:
        if os.environ.get('CI'): pytest.fail('CI must provide isolated PG service and age for native restore drill')
        pytest.skip('isolated PostgreSQL container or age unavailable locally')
    from psycopg.conninfo import conninfo_to_dict
    from messaging_service.recovery_state import materialize_recovery
    source, _, _ = seeded
    config = recovery_fixture(source, tmp_path)
    source_values = conninfo_to_dict(source.pool.conninfo)
    # Tests accept only the disposable source database controlled by conftest.
    assert source_values['dbname'] == 'messaging_probe_test'
    container_dsn = make_conninfo(host='127.0.0.1', port=5432,
        user=source_values['user'], dbname='messaging_probe_test')
    docker = ['docker', 'exec', '-i', '-e', 'PGPASSWORD=' + source_values['password'], container]
    command = [*docker, 'pg_dump', '--dbname=' + container_dsn]
    captured = []
    create_snapshot(source, tmp_path / 'capture', command, config,
        captured_callback=lambda: captured.append(True), reference_blobs=True)
    assert captured == [True]
    capture = tmp_path / 'capture'
    before = verify_bundle(capture, blob_root=source.files)
    source_hashes = {row['digest']: hashlib.sha256((source.files / row['digest']).read_bytes()).hexdigest()
                     for row in before['blobs']}
    identity = tmp_path / 'identity'
    subprocess.run([keygen, '-o', str(identity)], check=True, capture_output=True)
    recipient = subprocess.run([keygen, '-y', str(identity)], check=True, capture_output=True, text=True).stdout.strip()
    sink = io.BytesIO()
    snapshot_sha = hashlib.sha256((capture / 'manifest.json').read_bytes()).hexdigest()
    encrypted = encrypt_snapshot_stream(capture, sink, recipient, blob_root=source.files,
        age=age, expected_manifest_sha256=snapshot_sha)
    assert encrypted['sha256'] == hashlib.sha256(sink.getvalue()).hexdigest()
    destination = tmp_path / 'receiver'; destination.mkdir(mode=0o700)
    wire = streaming_packet(data=sink.getvalue(), id=BACKUP_ID,
        snapshot_sha256=snapshot_sha, max_bytes=snapshot_stream_bound(capture, source.files))[0]
    receipt = receiver.receive(destination, wire, reserve_bytes=0)
    assert receipt['status'] == 'stored' and receipt['sha256'] == encrypted['sha256']
    decrypt_snapshot(destination / (BACKUP_ID + '.tar.age'), identity, tmp_path / 'restored', age=age)
    assert verify_bundle(tmp_path / 'restored') == before
    assert materialize_recovery(tmp_path / 'restored', tmp_path / 'quarantine-files') == {
        'status': 'quarantined', 'sources': 3, 'activated': False}
    assert (tmp_path / 'quarantine-files/telegram-session/root/example.session').read_bytes() == b'FAKE_SESSION_BYTES'
    restored_name = 'anymsg_drill_' + uuid4().hex + '_restore_test'
    restored = None; created = False
    try:
        with psycopg.connect(source.pool.conninfo, autocommit=True) as connection:
            connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(restored_name)))
        created = True
        restore_dsn = make_conninfo(container_dsn, dbname=restored_name)
        subprocess.run([*docker, 'pg_restore', '--dbname=' + restore_dsn,
            '--exit-on-error', '--no-owner', '--no-acl'],
            input=(tmp_path / 'restored/database.dump').read_bytes(), check=True, capture_output=True)
        restored = Store(make_conninfo(source.pool.conninfo, dbname=restored_name),
            tmp_path / 'restored/files', 'http://testserver')
        assert verify_snapshot(restored, tmp_path / 'restored')['tables'] == 75
        quarantine_restore(restored)
        with restored.pool.connection() as connection:
            keys = connection.execute('SELECT revoked FROM api_keys').fetchall()
            policies = connection.execute('SELECT enabled FROM account_policies').fetchall()
        assert keys and all(row['revoked'] for row in keys)
        assert policies and all(not row['enabled'] for row in policies)
        # The original live database, CAS and fake cold session are unchanged.
        assert verify_snapshot(source, tmp_path / 'restored')['tables'] == 75
        assert all(hashlib.sha256((source.files / digest).read_bytes()).hexdigest() == value
                   for digest, value in source_hashes.items())
        assert (tmp_path / 'session/example.session').read_bytes() == b'FAKE_SESSION_BYTES'
    finally:
        if restored is not None: restored.close()
        if created:
            with psycopg.connect(source.pool.conninfo, autocommit=True) as connection:
                connection.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(restored_name)))
