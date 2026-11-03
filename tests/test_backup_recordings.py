"""Recordings profile bundle (ANYM-714): format, config, worker refusals, mixed versions, plaintext hygiene.

Synthetic SQLite archives only; no provider, no Docker, no real age (a stub copies bytes).
"""
import copy
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile

import pytest

from messaging_service import backup_runner, backup_worker
from messaging_service.backup import verify_bundle
from messaging_service.backup_calendar import verify_calendar_bundle
from messaging_service.backup_crypto import decrypt_snapshot, encrypt_snapshot, snapshot_profile
from messaging_service.backup_recordings import (APPLICATION_ID, REFUSAL_CODES, RecordingsRefused, create_recordings_snapshot,
                                                 valid_recordings_member, verify_recordings_bundle)
from messaging_service.backup_runner import Runner, read_json, write_json

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'pre_recordings'
OWNED = 'deedkit-recordings-backup-'
ARCHIVE_ID = 'arch-0123456789abcdef'
JOB_ID = '20261103T120000Z-0123456789abcdef'
TRANSFER = {'host': 'backup.example.invalid', 'user': 'recbackup', 'identity_file': '/private/delivery_ed25519',
            'known_hosts_file': '/private/known_hosts'}


def make_archive(path, *, user_version=3, meta=True, application_id=APPLICATION_ID, records=2, events=3,
                 archive_id=ARCHIVE_ID, incarnation=1, bulk=0, meta_key='TEXT PRIMARY KEY'):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE records(ref TEXT PRIMARY KEY, body TEXT);'
                     'CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT);')
    db.executemany('INSERT INTO records VALUES(?,?)', [(f'r{i}', 'synthetic transcript ' * bulk) for i in range(records + bulk)])
    db.executemany('INSERT INTO events(ref) VALUES(?)', [(f'r{i % max(records, 1)}',) for i in range(events)])
    if meta:
        db.execute(f'CREATE TABLE meta(key {meta_key}, value)')
        if archive_id is not None:
            db.execute("INSERT INTO meta VALUES('archive_id',?)", (archive_id,))
        if incarnation is not None:
            db.execute("INSERT INTO meta VALUES('incarnation',?)", (incarnation,))
        db.execute("INSERT INTO meta VALUES('journal_id','journal-synthetic')")
    db.commit()
    db.execute(f'PRAGMA application_id={application_id}')
    db.execute(f'PRAGMA user_version={user_version}')
    db.commit()
    db.close()
    return path


def sealed(tmp_path, name='bundle', **kwargs):
    create_recordings_snapshot(make_archive(tmp_path / (name + '-staged') / 'archive.sqlite', **kwargs), tmp_path / name)
    return tmp_path / name


@pytest.fixture
def age_stub(tmp_path):
    path = tmp_path / 'age-stub'
    path.write_text('#!' + sys.executable + '\nimport sys,shutil\n'
        'source=open(sys.argv[-1],"rb") if "-d" in sys.argv else sys.stdin.buffer\n'
        'shutil.copyfileobj(source,sys.stdout.buffer)\n')
    path.chmod(0o700)
    return str(path)


def load_old(name, filename):
    spec = importlib.util.spec_from_file_location('messaging_service.' + name, FIXTURES / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_json_loose(path):
    return json.loads(Path(path).read_text())


def tar_plain(tmp_path, members, name='plain.tar'):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for member, data in members:
            info = tarfile.TarInfo(member)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    path = tmp_path / name
    path.write_bytes(buffer.getvalue())
    return path


# spec: ANYM-714
def test_a_good_snapshot_becomes_a_bundle_with_exactly_two_members_and_the_identity_facts(tmp_path):
    bundle = sealed(tmp_path, events=5, records=4)
    assert sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob('*') if p.is_file()) == [
        'manifest.json', 'state/archive.sqlite']
    manifest = verify_recordings_bundle(bundle)
    data = (bundle / 'state' / 'archive.sqlite').read_bytes()
    assert manifest['profile'] == 'recordings' and manifest['user_version'] == 3
    assert manifest['bytes'] == len(data) and manifest['sha256'] == hashlib.sha256(data).hexdigest()
    assert manifest['archive'] == {'archive_id': ARCHIVE_ID, 'incarnation': 1, 'sequence': 5, 'records': 4}
    assert snapshot_profile(bundle) == 'recordings'


# spec: ANYM-714
@pytest.mark.parametrize('version', [1, 2, 3, 4, 5])
def test_every_supported_schema_version_is_sealed_and_its_version_recorded(tmp_path, version):
    manifest = verify_recordings_bundle(sealed(tmp_path, user_version=version))
    assert manifest['user_version'] == version


# spec: ANYM-714
@pytest.mark.parametrize('version', [0, 6, 99])
def test_an_unknown_schema_version_is_refused_with_a_fixed_code_and_creates_no_bundle(tmp_path, version):
    staged = make_archive(tmp_path / 'staged' / 'archive.sqlite', user_version=version)
    with pytest.raises(RecordingsRefused) as refused:
        create_recordings_snapshot(staged, tmp_path / 'bundle')
    assert refused.value.code == 'recordings_schema_unsupported' and not (tmp_path / 'bundle').exists()


# spec: ANYM-714
def test_a_newer_manifest_version_is_refused_by_the_validator_so_an_older_helper_fails_closed(tmp_path):
    bundle = sealed(tmp_path)
    manifest = read_json_loose(bundle / 'manifest.json')
    manifest['user_version'] = 6
    (bundle / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='recordings_schema_unsupported'):
        verify_recordings_bundle(bundle)


def corrupt_pages(path):
    size = path.stat().st_size
    with path.open('r+b') as f:
        f.seek(4096)
        f.write(b'\xff' * (size - 4096))


def build_bad(tmp_path, kind):
    path = tmp_path / 'staged' / 'archive.sqlite'
    path.parent.mkdir(parents=True)
    if kind == 'empty':
        path.write_bytes(b'')
    elif kind == 'not_a_database':
        path.write_bytes(b'this is not an sqlite file' * 400)
    elif kind == 'damaged':
        make_archive(path, bulk=400)
        assert path.stat().st_size > 8192
        corrupt_pages(path)
    elif kind == 'no_meta':
        make_archive(path, meta=False)
    elif kind == 'meta_without_archive_id':
        make_archive(path, archive_id=None)
    elif kind == 'meta_without_incarnation':
        make_archive(path, incarnation=None)
    elif kind == 'incarnation_zero':
        make_archive(path, incarnation=0)
    elif kind == 'old_schema_without_meta':
        make_archive(path, user_version=2, meta=False)
    elif kind == 'foreign_application':
        make_archive(path, application_id=0x12345678)
    elif kind == 'symlink':
        make_archive(tmp_path / 'real.sqlite')
        path.symlink_to(tmp_path / 'real.sqlite')
    elif kind == 'directory':
        path.mkdir()
    elif kind == 'duplicate_archive_id':
        make_archive(path, meta_key='TEXT')
        db = sqlite3.connect(path)
        db.execute("INSERT INTO meta VALUES('archive_id','arch-second-conflicting-id')")
        db.commit()
        db.close()
    elif kind == 'text_incarnation':
        make_archive(path, incarnation='1')
    elif kind == 'meta_wrong_shape':
        make_archive(path, meta=False)
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE meta(name TEXT, val TEXT)')
        db.commit()
        db.close()
    elif kind == 'text_sequence':
        make_archive(path)
        db = sqlite3.connect(path)
        db.executescript("DROP TABLE events; CREATE TABLE events(sequence TEXT); INSERT INTO events VALUES('not-a-sequence');")
        db.commit()
        db.close()
    return path


BAD = [('absent', 'recordings_snapshot_absent'), ('empty', 'recordings_snapshot_empty'),
       ('not_a_database', 'recordings_snapshot_corrupt'), ('damaged', 'recordings_snapshot_corrupt'),
       ('no_meta', 'recordings_identity_missing'), ('meta_without_archive_id', 'recordings_identity_missing'),
       ('meta_without_incarnation', 'recordings_identity_missing'), ('incarnation_zero', 'recordings_identity_missing'),
       ('old_schema_without_meta', 'recordings_identity_missing'),
       ('duplicate_archive_id', 'recordings_identity_missing'), ('text_incarnation', 'recordings_identity_missing'),
       ('meta_wrong_shape', 'recordings_identity_missing'), ('text_sequence', 'recordings_snapshot_corrupt'),
       ('foreign_application', 'recordings_snapshot_foreign'), ('symlink', 'recordings_snapshot_unsafe'),
       ('directory', 'recordings_snapshot_unsafe')]


# spec: ANYM-714
@pytest.mark.parametrize('kind,code', BAD)
def test_an_absent_empty_corrupt_identity_less_or_foreign_snapshot_creates_no_bundle(tmp_path, kind, code):
    path = build_bad(tmp_path, kind) if kind != 'absent' else tmp_path / 'staged' / 'archive.sqlite'
    with pytest.raises(RecordingsRefused) as refused:
        create_recordings_snapshot(path, tmp_path / 'out' / 'bundle')
    assert refused.value.code == code and code in REFUSAL_CODES
    assert not (tmp_path / 'out' / 'bundle').exists()
    assert not any(p.name.startswith('.recordings-backup-') for p in (tmp_path / 'out').iterdir())


# spec: ANYM-714
def test_a_refusal_code_outside_the_fixed_list_cannot_be_raised():
    with pytest.raises(ValueError, match='invalid_refusal_code'):
        RecordingsRefused('/private/path/with/content')


# spec: ANYM-714
@pytest.mark.parametrize('name,ok', [
    ('manifest.json', True), ('state/archive.sqlite', True),
    ('database.dump', False), ('state/vault/key', False), ('state/profile/worker.json', False),
    ('state/other.sqlite', False), ('state/archive.sqlite/x', False), ('state/../manifest.json', False),
    ('../manifest.json', False), ('/state/archive.sqlite', False), ('state/archive.sqlite-wal', False),
    ('files/' + 'a' * 64, False), ('recovery/' + 'a' * 64, False)])
def test_only_the_manifest_and_the_one_archive_are_recordings_members(name, ok):
    assert valid_recordings_member(name) is ok


# spec: ANYM-714
def test_a_bundle_with_a_second_state_file_an_extra_member_or_a_changed_archive_is_refused(tmp_path):
    for attack in ('second_state_file', 'extra_top_level', 'symlinked_archive', 'tampered', 'missing_manifest_key'):
        bundle = sealed(tmp_path, attack)
        if attack == 'second_state_file':
            (bundle / 'state' / 'other.sqlite').write_bytes(b'x')
        elif attack == 'extra_top_level':
            (bundle / 'database.dump').write_bytes(b'x')
        elif attack == 'symlinked_archive':
            (bundle / 'state' / 'archive.sqlite').unlink()
            (bundle / 'state' / 'archive.sqlite').symlink_to(make_archive(tmp_path / 'other.sqlite'))
        elif attack == 'tampered':
            with (bundle / 'state' / 'archive.sqlite').open('r+b') as f:
                f.seek(-1, os.SEEK_END)
                f.write(b'\x01')
        else:
            manifest = read_json_loose(bundle / 'manifest.json')
            manifest.pop('archive')
            (bundle / 'manifest.json').write_text(json.dumps(manifest))
        with pytest.raises((ValueError, RuntimeError), match='invalid_recordings_bundle|archive_corrupt'):
            verify_recordings_bundle(bundle)


# spec: ANYM-714
def test_a_manifest_that_disagrees_with_the_archive_identity_is_refused(tmp_path):
    bundle = sealed(tmp_path)
    manifest = read_json_loose(bundle / 'manifest.json')
    manifest['archive']['incarnation'] = 2
    (bundle / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match='archive_corrupt'):
        verify_recordings_bundle(bundle)


# spec: ANYM-714
@pytest.mark.parametrize('field,value', [
    ('bytes', True), ('bytes', 0), ('bytes', '10'), ('user_version', '3'), ('user_version', 3.0), ('sha256', 'A' * 64),
    ('created_at', 'yesterday'), ('captured_at', 5), ('format', 2), ('format', True), ('format', 1.0), ('archive', []),
    ('archive.sequence', '7'), ('archive.sequence', -1), ('archive.records', True), ('archive.incarnation', 0),
    ('archive.incarnation', '1'), ('archive.archive_id', ''), ('archive.archive_id', 'a b'), ('archive.extra', 1)])
def test_a_manifest_field_of_the_wrong_type_or_range_is_refused_independently_of_the_archive(tmp_path, field, value):
    bundle = sealed(tmp_path)
    manifest = read_json_loose(bundle / 'manifest.json')
    if field.startswith('archive.'):
        manifest['archive'][field.split('.', 1)[1]] = value
    else:
        manifest[field] = value
    (bundle / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='invalid_recordings_bundle'):
        verify_recordings_bundle(bundle)


# spec: ANYM-714
def test_the_worker_form_checks_and_encrypts_the_archive_in_place_with_a_manifest_only_directory(tmp_path, age_stub):
    staged = make_archive(tmp_path / 'staging' / 'archive.sqlite')
    create_recordings_snapshot(staged, tmp_path / 'snapshot', copy_state=False)
    assert [p.name for p in (tmp_path / 'snapshot').rglob('*')] == ['manifest.json']
    manifest = verify_recordings_bundle(tmp_path / 'snapshot', state=staged)
    with pytest.raises(ValueError, match='invalid_recordings_bundle'):
        verify_recordings_bundle(tmp_path / 'snapshot')
    encrypt_snapshot(tmp_path / 'snapshot', tmp_path / 'out.age', 'fake', age=age_stub, recordings_state=staged)
    with tarfile.open(tmp_path / 'out.age') as archive:
        assert archive.getnames() == ['manifest.json', 'state/archive.sqlite']
    decrypt_snapshot(tmp_path / 'out.age', 'fake', tmp_path / 'restored', age=age_stub)
    assert verify_recordings_bundle(tmp_path / 'restored') == manifest
    assert (tmp_path / 'restored/state/archive.sqlite').read_bytes() == staged.read_bytes()
    # A changed archive no longer matches the manifest it was checked against; another profile takes no external state.
    with staged.open('r+b') as f:
        f.seek(-1, os.SEEK_END)
        f.write(b'\x01')
    with pytest.raises(RuntimeError, match='archive_corrupt'):
        encrypt_snapshot(tmp_path / 'snapshot', tmp_path / 'again.age', 'fake', age=age_stub, recordings_state=staged)
    calendar_like = tmp_path / 'calendar'
    calendar_like.mkdir()
    (calendar_like / 'manifest.json').write_text('{"profile":"calendar"}')
    with pytest.raises(ValueError, match='invalid_recordings_state'):
        encrypt_snapshot(calendar_like, tmp_path / 'c.age', 'fake', age=age_stub, recordings_state=staged)


# spec: ANYM-714
def test_the_messaging_and_calendar_validators_refuse_a_recordings_bundle_and_it_refuses_theirs(tmp_path):
    bundle = sealed(tmp_path)
    with pytest.raises(ValueError, match='unsupported_backup'):
        verify_bundle(bundle)
    with pytest.raises(ValueError, match='invalid_calendar_bundle'):
        verify_calendar_bundle(bundle)
    for profile in ('calendar', None):
        foreign = tmp_path / f'foreign-{profile}'
        shutil.copytree(bundle, foreign)
        manifest = read_json_loose(foreign / 'manifest.json')
        if profile:
            manifest['profile'] = profile
        else:
            manifest.pop('profile')
        (foreign / 'manifest.json').write_text(json.dumps(manifest))
        with pytest.raises(ValueError, match='invalid_recordings_bundle'):
            verify_recordings_bundle(foreign)


# spec: ANYM-714
def test_encryption_roundtrip_restores_the_same_verified_bundle(tmp_path, age_stub):
    bundle = sealed(tmp_path)
    encrypt_snapshot(bundle, tmp_path / 'out.age', 'fake', age=age_stub)
    decrypt_snapshot(tmp_path / 'out.age', 'fake', tmp_path / 'restored', age=age_stub)
    assert verify_recordings_bundle(tmp_path / 'restored') == verify_recordings_bundle(bundle)
    assert (tmp_path / 'restored/state/archive.sqlite').read_bytes() == (bundle / 'state/archive.sqlite').read_bytes()
    with tarfile.open(tmp_path / 'out.age') as archive:
        assert archive.getnames() == ['manifest.json', 'state/archive.sqlite']


# spec: ANYM-714
def test_encryption_never_consumes_a_recordings_bundle_and_refuses_a_bundle_that_changed(tmp_path, age_stub):
    bundle = sealed(tmp_path)
    with pytest.raises(ValueError, match='invalid_staging_consumption'):
        encrypt_snapshot(bundle, tmp_path / 'a.age', 'fake', age=age_stub, consume_staged_blobs=True)
    (bundle / 'state' / 'other.sqlite').write_bytes(b'x')
    with pytest.raises(ValueError, match='invalid_recordings_bundle'):
        encrypt_snapshot(bundle, tmp_path / 'b.age', 'fake', age=age_stub)
    assert not (tmp_path / 'a.age').exists() and not (tmp_path / 'b.age').exists()


# spec: ANYM-714
def test_decryption_admits_only_the_two_recordings_members_manifest_first(tmp_path, age_stub):
    bundle = sealed(tmp_path)
    manifest, archive = (bundle / 'manifest.json').read_bytes(), (bundle / 'state' / 'archive.sqlite').read_bytes()
    cases = {
        'extra_dump': ([('manifest.json', manifest), ('state/archive.sqlite', archive), ('database.dump', b'x')],
                       'invalid_backup_member'),
        'second_state': ([('manifest.json', manifest), ('state/archive.sqlite', archive), ('state/other.sqlite', b'x')],
                         'invalid_backup_member'),
        'escape': ([('manifest.json', manifest), ('state/../escape', b'x')], 'invalid_backup_member'),
        'archive_first': ([('state/archive.sqlite', archive), ('manifest.json', manifest)], 'invalid_backup_member'),
        'archive_missing': ([('manifest.json', manifest)], 'incomplete_backup'),
        'duplicate': ([('manifest.json', manifest), ('manifest.json', manifest)], 'invalid_backup_member'),
    }
    for name, (members, code) in cases.items():
        plain = tar_plain(tmp_path, members, name + '.tar')
        with pytest.raises(ValueError, match=code):
            decrypt_snapshot(plain, 'fake', tmp_path / ('restored-' + name), age=age_stub)
        assert not (tmp_path / ('restored-' + name)).exists()


# spec: ANYM-714
def test_the_pre_card_readers_refuse_a_recordings_bundle_by_its_profile_value(tmp_path, age_stub):
    old_crypto = load_old('old_backup_crypto', 'old_backup_crypto.py')
    bundle = sealed(tmp_path)
    with pytest.raises(ValueError, match='unsupported_backup'):
        old_crypto.snapshot_profile(bundle)
    with pytest.raises(ValueError):
        old_crypto.encrypt_snapshot(bundle, tmp_path / 'old.age', 'fake', age=age_stub)
    encrypt_snapshot(bundle, tmp_path / 'new.age', 'fake', age=age_stub)
    with pytest.raises(ValueError, match='invalid_backup_member'):
        old_crypto.decrypt_snapshot(tmp_path / 'new.age', 'fake', tmp_path / 'old-restored', age=age_stub)
    assert not (tmp_path / 'old-restored').exists()


# ---- runner -------------------------------------------------------------------------------------------------------

def recordings_cfg(tmp_path, **overrides):
    return {'profile': 'recordings', 'directory': str(tmp_path / 'recordings'), 'project': 'deedkit-recordings',
            'image': 'backup:test', 'recipient_file': str(tmp_path / 'recipient'), 'transfer': dict(TRANSFER),
            'timeout_seconds': 60, 'keep_local': 1, 'host_lock': str(tmp_path / 'messaging' / '.runner.lock'),
            'recordings': {'staging_directory': str(tmp_path / 'staging'), 'state_directory': str(tmp_path / 'state'),
                           'capture_timeout_seconds': 1200, 'container': 'recordings-api-1',
                           'container_config': '/run/deedkit/recordings/config.json', 'container_staging': '/run/deedkit/backup-staging',
                           'timers': [str(tmp_path / 'timers' / 'messaging.timer'),
                                      str(tmp_path / 'timers' / 'calendar.timer')]}, **overrides}


# 2026-11-04 03:00 UTC: 13 hours before the calendar's 16:00 run, so ANYM-727's admission always lets these runs start.
NOW = 1793761200.0


def write_timers(tmp_path):
    (tmp_path / 'timers').mkdir(exist_ok=True)
    for name, time in (('messaging', '18:00:00'), ('calendar', '16:00:00')):
        (tmp_path / 'timers' / f'{name}.timer').write_text(f'[Timer]\nOnCalendar=*-*-* {time} UTC\nPersistent=true\n')


def private_file(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


class RecordingsDocker:
    """Docker as a recordings run may see it: its own worker only. `start` runs the real worker in process."""
    def __init__(self, root, age_stub, monkeypatch):
        self.root, self.calls, self.containers = root, [], {}
        monkeypatch.setattr(backup_worker, 'encrypt_snapshot',
                            lambda *a, **k: encrypt_snapshot(*a, age=age_stub, **k))
    def running_collectors(self, project):
        raise AssertionError('a recordings run lists no collectors')
    def inspect(self, cid):
        self.calls.append(('inspect', cid))
        return copy.deepcopy(self.containers.get(cid))
    def call(self, *args):
        self.calls.append(args)
        if args[0] == 'create':
            name, labels = args[args.index('--name') + 1], {}
            mounts = [args[i + 1] for i, a in enumerate(args) if a == '--mount']
            for i, arg in enumerate(args):
                if arg == '--label':
                    key, value = args[i + 1].split('=', 1)
                    labels[key] = value
            self.containers[name] = {'Id': name, 'Config': {'Labels': labels}, 'Mounts': mounts,
                                     'State': {'Running': False, 'Status': 'created', 'ExitCode': 0}}
        elif args[0] == 'start':
            info = self.containers[args[1]]
            work = self.root / info['Config']['Labels']['personal-os.backup.id']
            config = read_json(work / 'worker.json')
            source = next(m for m in info['Mounts'] if m.endswith('dst=/sources/snapshot,readonly')).split(',')[1][4:]
            config.update(snapshot_directory=source, work_directory=str(work), recipient_file=str(work.parent.parent / 'recipient'))
            try:
                backup_worker.run(config)
                code = 0
            except Exception:
                code = 1
            info['State'].update(Running=False, Status='exited', ExitCode=code)
        elif args[0] == 'stop':
            self.containers[args[-1]]['State'].update(Running=False, Status='exited', ExitCode=143)
        elif args[0] == 'rm':
            self.containers.pop(args[1])
        return ''
    def touched(self):
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


class StagingRunner(Runner):
    """Stands in for ANYM-727: `staged` is copied into the job's private staging directory."""
    staged = None
    on_stage = None
    def stage_snapshot(self, destination):
        if self.on_stage:
            self.on_stage(destination)
        if self.staged is None:
            return
        target = destination / 'archive.sqlite'
        if self.staged.is_symlink():
            target.symlink_to(os.readlink(self.staged))
        elif self.staged.is_dir():
            target.mkdir()
        else:
            shutil.copyfile(self.staged, target)


def recordings_runner(tmp_path, age_stub, monkeypatch, send=None, staged='good'):
    cfg = recordings_cfg(tmp_path)
    Path(cfg['host_lock']).parent.mkdir(mode=0o700)
    (tmp_path / 'recipient').write_text('fake')
    write_timers(tmp_path)
    docker = RecordingsDocker(Path(cfg['directory']), age_stub, monkeypatch)
    sent = []
    async def stored(path, result, transfer, timeout):
        assert not (path.parent / 'snapshot').exists()
        decrypt_snapshot(path, 'fake', tmp_path / f'received-{len(sent)}', age=age_stub)
        sent.append((result['id'], result['sha256'], verify_recordings_bundle(tmp_path / f'received-{len(sent)}')))
        return {**result, 'status': 'stored', 'duplicate': False}
    runner = StagingRunner(cfg, docker=docker, sleep=lambda _: None, send=send or stored, clock=lambda: NOW)
    if staged == 'good':
        runner.staged = make_archive(tmp_path / 'source' / 'archive.sqlite')
    elif staged:
        runner.staged = build_bad(tmp_path / 'bad', staged)
    return runner, docker, sent


def staging_entries(runner):
    return sorted(p.name for p in runner.staging_root.iterdir())


# spec: ANYM-714
def test_a_valid_recordings_config_loads_and_a_config_without_it_is_unchanged(tmp_path):
    cfg = recordings_cfg(tmp_path, delivery_mode='staged_v1', busy_wait_seconds=600)
    assert backup_runner.config(private_file(tmp_path / 'runner.json', cfg)) == cfg
    messaging = json.loads((Path(__file__).resolve().parents[1] / 'deploy' / 'backup-runner.example.json').read_text())
    assert backup_runner.config(private_file(tmp_path / 'messaging.json', messaging)) == messaging


# spec: ANYM-714
@pytest.mark.parametrize('change,code', [
    ({'dsn_file': '/private/db.dsn'}, 'invalid_backup_config'),
    ({'network': 'backend'}, 'invalid_backup_config'),
    ({'files_volume': 'files'}, 'invalid_backup_config'),
    ({'state_mounts': {'vault': '/v', 'profile': '/p'}}, 'invalid_backup_config'),
    ({'delivery_mode': 'stream_v2'}, 'invalid_backup_config'),
    ({'recordings': None}, 'invalid_backup_config'),
    ({'recordings': {'staging_directory': '/s', 'state_directory': '/t'}}, 'invalid_backup_config'),
    ({'recordings': 'extra'}, 'invalid_backup_config'),
    ({'host_lock': None}, 'invalid_backup_config'),
    ({'host_lock': 'relative/.runner.lock'}, 'invalid_backup_path'),
    ({'host_lock': 'own'}, 'invalid_backup_path'),
    ({'recordings': 'staging_relative'}, 'invalid_backup_path'),
    ({'recordings': 'staging_is_directory'}, 'invalid_backup_path'),
    ({'recordings': 'staging_holds_directory'}, 'invalid_backup_path'),
    ({'recordings': 'directory_holds_staging'}, 'invalid_backup_path'),
    ({'recordings': 'staging_holds_lock'}, 'invalid_backup_path'),
    ({'recordings': 'state_in_staging'}, 'invalid_backup_path'),
    ({'recordings': 'recipient_in_staging'}, 'invalid_backup_path'),
    ({'recordings': 'identity_in_staging'}, 'invalid_backup_path'),
    ({'recordings': 'known_hosts_in_staging'}, 'invalid_backup_path'),
    ({'recordings': 'capture_short'}, 'invalid_backup_timeout'),
    ({'recordings': 'capture_long'}, 'invalid_backup_timeout'),
    ({'busy_wait_seconds': 7201}, 'invalid_backup_timeout'),
    ({'keep_local': 0}, 'invalid_backup_retention'),
    ({'transfer': {'host': 'x'}}, 'invalid_backup_transfer'),
])
def test_a_recordings_config_with_a_foreign_key_a_missing_own_key_or_overlapping_paths_is_refused(tmp_path, change, code):
    cfg = recordings_cfg(tmp_path)
    own = cfg['recordings']
    for key, value in change.items():
        if value is None:
            cfg.pop(key)
        elif value == 'own':
            cfg[key] = cfg['directory'] + '/.runner.lock'
        elif value == 'extra':
            own['unknown'] = 1
        elif value == 'staging_relative':
            own['staging_directory'] = 'staging'
        elif value == 'staging_is_directory':
            own['staging_directory'] = cfg['directory']
        elif value == 'staging_holds_directory':
            own['staging_directory'] = str(Path(cfg['directory']).parent)
        elif value == 'directory_holds_staging':
            own['staging_directory'] = cfg['directory'] + '/staging'
        elif value == 'staging_holds_lock':
            own['staging_directory'] = str(Path(cfg['host_lock']).parent)
        elif value == 'state_in_staging':
            own['state_directory'] = own['staging_directory'] + '/state'
        elif value == 'recipient_in_staging':
            cfg['recipient_file'] = own['staging_directory'] + '/recipient'
        elif value == 'identity_in_staging':
            cfg['transfer']['identity_file'] = own['staging_directory'] + '/keys/id_ed25519'
        elif value == 'known_hosts_in_staging':
            cfg['transfer']['known_hosts_file'] = own['staging_directory'] + '/known_hosts'
        elif value == 'capture_short':
            own['capture_timeout_seconds'] = 59
        elif value == 'capture_long':
            own['capture_timeout_seconds'] = 7201
        else:
            cfg[key] = value
    with pytest.raises(ValueError, match=code):
        backup_runner.config(private_file(tmp_path / 'runner.json', cfg))


# spec: ANYM-714
def test_a_runner_without_this_card_refuses_a_recordings_config_before_any_run(tmp_path):
    old_runner = load_old('old_backup_runner', 'old_backup_runner.py')
    path = private_file(tmp_path / 'runner.json', recordings_cfg(tmp_path))
    with pytest.raises(ValueError, match='invalid_backup_config'):
        old_runner.config(path)
    assert backup_runner.config(path)['profile'] == 'recordings'
    assert not (tmp_path / 'recordings').exists() and not (tmp_path / 'staging').exists()


# spec: ANYM-714
def test_a_recordings_run_stages_reads_only_and_delivers_a_verified_bundle(tmp_path, age_stub, monkeypatch):
    runner, docker, sent = recordings_runner(tmp_path, age_stub, monkeypatch)
    runner.run()
    root = Path(runner.cfg['directory'])
    assert all(name.startswith(OWNED) for name in docker.touched()), docker.touched()
    create = next(args for args in docker.calls if args[0] == 'create')
    mounts = [create[i + 1] for i, arg in enumerate(create) if arg == '--mount']
    assert create[create.index('--network') + 1] == 'none' and '--read-only' in create and create[-1] == 'backup:test'
    snapshot_mount = next(m for m in mounts if m.endswith('dst=/sources/snapshot,readonly'))
    assert snapshot_mount.startswith(f'type=bind,src={tmp_path}/staging/') and len(mounts) == 4
    assert not any('database_url' in m or 'type=volume' in m for m in mounts)
    (job,) = [p for p in root.iterdir() if p.is_dir() and backup_runner.ID.fullmatch(p.name)]
    assert read_json(job / 'worker.json') == {'profile': 'recordings', 'id': job.name, 'snapshot_directory': '/sources/snapshot',
                                              'recipient_file': '/run/secrets/recipient', 'work_directory': '/work',
                                              'integrity_timeout_seconds': 300, 'encrypt_timeout_seconds': 600}
    (delivered,) = sent
    assert delivered[0] == job.name and delivered[2]['archive']['archive_id'] == ARCHIVE_ID
    status = read_json(root / 'status.json')
    assert status['state'] == 'ok' and status['last_success_id'] == job.name
    assert not (root / 'journal.json').exists() and not (job / 'snapshot').exists()
    assert staging_entries(runner) == [] and (tmp_path / 'staging').is_dir()
    assert not Path(str(tmp_path / 'state' / '.unsettled')).exists()


# spec: ANYM-714
@pytest.mark.parametrize('kind,code', [(k, c) for k, c in BAD if k != 'absent'] + [(None, 'recordings_snapshot_absent')])
def test_a_bad_staged_snapshot_ends_the_job_with_its_code_no_bundle_and_no_plaintext(tmp_path, age_stub, monkeypatch, kind, code):
    runner, docker, sent = recordings_runner(tmp_path, age_stub, monkeypatch, staged=kind)
    with pytest.raises(RuntimeError, match=code):
        runner.run()
    root = Path(runner.cfg['directory'])
    status = read_json(root / 'status.json')
    assert status['state'] == 'error' and status['error_code'] == code
    assert sent == [] and staging_entries(runner) == []
    assert not list(root.rglob('bundle.tar.age')) and not list(root.rglob('snapshot'))
    runner.recover_stop()
    assert not runner.journal_path.exists() and staging_entries(runner) == []
    assert all(name.startswith(OWNED) for name in docker.touched())


# spec: ANYM-714
def test_a_bad_run_does_not_poison_the_next_good_run(tmp_path, age_stub, monkeypatch):
    runner, docker, sent = recordings_runner(tmp_path, age_stub, monkeypatch, staged='no_meta')
    with pytest.raises(RuntimeError, match='recordings_identity_missing'):
        runner.run()
    runner.recover_stop()
    runner.staged = make_archive(tmp_path / 'source' / 'archive.sqlite')
    runner.run()
    assert len(sent) == 1 and read_json(runner.status_path)['state'] == 'ok'


# spec: ANYM-714
def test_plaintext_left_in_staging_by_a_crashed_run_is_removed_at_the_next_start(tmp_path, age_stub, monkeypatch):
    runner, docker, sent = recordings_runner(tmp_path, age_stub, monkeypatch)
    staging = runner.staging_root
    staging.mkdir(mode=0o700)
    (staging / 'crashed-job').mkdir()
    shutil.copyfile(runner.staged, staging / 'crashed-job' / 'archive.sqlite')
    (staging / 'stray.sqlite').write_bytes(b'plaintext')
    (staging / 'link').symlink_to(tmp_path / 'source')
    seen = []
    runner.on_stage = lambda destination: seen.append((sorted(p.name for p in staging.iterdir()), destination.name))
    runner.run()
    (entries, own), = seen
    assert entries == [own] and backup_runner.ID.fullmatch(own), 'only this job\'s empty directory remains at staging time'
    assert (tmp_path / 'source' / 'archive.sqlite').exists(), 'a symlink in staging is removed, never followed'
    assert staging_entries(runner) == [] and len(sent) == 1


# spec: ANYM-714
def test_a_mount_inside_staging_is_refused_before_anything_is_deleted(tmp_path, age_stub, monkeypatch):
    runner, docker, sent = recordings_runner(tmp_path, age_stub, monkeypatch)
    staging = runner.staging_root
    staging.mkdir(mode=0o700)
    (staging / 'mounted').mkdir()
    (staging / 'mounted' / 'outside-data.txt').write_text('belongs to another filesystem')
    (staging / 'plain').mkdir()
    (staging / 'plain' / 'archive.sqlite').write_bytes(b'plaintext')
    table = tmp_path / 'mountinfo'
    table.write_text(f'36 35 0:30 / {staging.resolve()}/mounted rw,relatime - ext4 /dev/sda1 rw\n')
    monkeypatch.setattr(backup_runner, 'MOUNTINFO', str(table))
    with pytest.raises(ValueError, match='staging_mount_refused'):
        runner.clean_staging()
    assert (staging / 'mounted' / 'outside-data.txt').exists() and (staging / 'plain' / 'archive.sqlite').exists()
    with pytest.raises(ValueError, match='staging_mount_refused'):
        runner.run()
    assert docker.calls == [] and sent == [] and (staging / 'mounted' / 'outside-data.txt').exists()


# spec: ANYM-714
def test_the_mount_table_reading_finds_only_mounts_strictly_inside_the_directory(tmp_path):
    root = tmp_path / 'stag ing'
    root.mkdir()
    escaped = str(root.resolve()).replace(' ', '\\040')
    table = tmp_path / 'mountinfo'
    table.write_text(f'1 0 0:1 / {escaped}/sub\\040dir rw - ext4 a b\n2 0 0:2 / {escaped} rw - ext4 a b\n'
                     f'3 0 0:3 / {escaped}-other rw - ext4 a b\n4 0 0:4 /\n')
    assert backup_runner.mounts_under(root, table) == [str(root.resolve()) + '/sub dir']
    assert backup_runner.mounts_under(root, tmp_path / 'absent') == []


# spec: ANYM-714
def test_a_resumed_upload_journal_empties_surviving_staging_before_delivery(tmp_path, age_stub, monkeypatch):
    seen = []
    async def failing(path, result, transfer, timeout):
        raise RuntimeError('backup_delivery_failed')
    runner, docker, sent = recordings_runner(tmp_path, age_stub, monkeypatch, send=failing)
    with pytest.raises(RuntimeError, match='backup_delivery_failed'):
        runner.run()
    runner.recover_stop()
    job = runner.journal['id']
    assert read_json(runner.journal_path)['phase'] == 'upload'
    survivors = runner.staging_root / job
    survivors.mkdir(mode=0o700)
    (survivors / 'archive.sqlite').write_bytes(b'plaintext left by a crash')
    (runner.staging_root / 'stray.sqlite').write_bytes(b'plaintext')
    async def stored(path, result, transfer, timeout):
        seen.append(staging_entries(runner))
        return {**result, 'status': 'stored', 'duplicate': False}
    again = StagingRunner(runner.cfg, docker=docker, sleep=lambda _: None, send=stored, clock=lambda: NOW)
    again.run()
    assert seen == [[]] and staging_entries(again) == []
    # `--recover-stop` of an upload-phase journal empties it too.
    survivors.mkdir(mode=0o700)
    (survivors / 'archive.sqlite').write_bytes(b'plaintext')
    Runner(runner.cfg, docker=docker).recover_stop()
    assert staging_entries(again) == []


# spec: ANYM-714
def test_a_failing_cleanup_does_not_stop_recovery_from_stopping_the_running_worker(tmp_path, age_stub, monkeypatch):
    runner, docker, _ = recordings_runner(tmp_path, age_stub, monkeypatch)
    runner.prepare()
    job = runner.journal['id']
    container = OWNED + job.lower()
    docker.containers[container] = {'Id': container, 'Mounts': [], 'State': {'Running': True, 'Status': 'running', 'ExitCode': 0},
                                    'Config': {'Labels': {'personal-os.backup.id': job,
                                                          'personal-os.backup.project': 'deedkit-recordings'}}}
    def broken():
        raise OSError('staging is on a failing filesystem')
    monkeypatch.setattr(runner, 'clean_staging', broken)
    with pytest.raises(OSError, match='failing filesystem'):
        runner.recover_stop()
    assert ('stop', '--time', '25', container) in docker.calls and container not in docker.containers
    assert read_json(runner.status_path)['error_code'] == 'staging_cleanup_failed'


# spec: ANYM-714
def test_a_worker_refusal_file_that_is_not_an_object_changes_no_other_profiles_failure(tmp_path, age_stub, monkeypatch):
    cfg = {'profile': 'calendar', 'directory': str(tmp_path / 'cal'), 'project': 'p', 'network': 'n', 'image': 'i',
           'dsn_file': '/d', 'recipient_file': '/r', 'state_mounts': {'vault': '/v', 'profile': '/p'},
           'transfer': dict(TRANSFER), 'timeout_seconds': 60, 'keep_local': 1, 'host_lock': str(tmp_path / 'm' / '.runner.lock')}
    calendar = Runner(cfg)
    work = tmp_path / 'job'
    work.mkdir(mode=0o700)
    private_file(work / 'refused.json', ['not', 'an', 'object'])
    assert calendar.refusal(work) is None
    runner, _, _ = recordings_runner(tmp_path, age_stub, monkeypatch)
    for content in (['x'], {'code': 5}, {'code': 'backup_capture_timeout'}, {'code': 'recordings_made_up'}):
        private_file(work / 'refused.json', content)
        assert runner.refusal(work) is None
    private_file(work / 'refused.json', {'code': 'recordings_snapshot_corrupt'})
    assert runner.refusal(work) == 'recordings_snapshot_corrupt'


# spec: ANYM-714
def test_recovery_after_an_interrupted_capture_empties_staging_and_removes_only_its_own_worker(tmp_path, age_stub, monkeypatch):
    runner, docker, sent = recordings_runner(tmp_path, age_stub, monkeypatch)
    runner.prepare()
    job = runner.journal['id']
    assert staging_entries(runner) == [job]
    (runner.staging_root / job / 'archive.sqlite').write_bytes(b'plaintext')
    runner.recover_stop()
    assert staging_entries(runner) == [] and not runner.journal_path.exists() and sent == []
    assert docker.touched() <= {OWNED + job.lower()}


# spec: ANYM-714
def test_a_journal_naming_another_container_is_refused_before_any_docker_call(tmp_path, age_stub, monkeypatch):
    runner, docker, _ = recordings_runner(tmp_path, age_stub, monkeypatch)
    runner.prepare()
    journal = read_json(runner.journal_path)
    journal['container'] = 'deedkit-calendar-backup-' + journal['id'].lower()
    write_json(runner.journal_path, journal)
    for action in (Runner(runner.cfg, docker=docker).recover_stop,
                   Runner(runner.cfg, docker=docker, clock=lambda: NOW).run):
        with pytest.raises(ValueError, match='invalid_backup_journal'):
            action()
    assert docker.calls == []


# spec: ANYM-714
def test_a_bundle_written_before_a_rollback_stays_readable_by_the_new_runner(tmp_path, age_stub, monkeypatch):
    attempts = []
    async def failing(path, result, transfer, timeout):
        attempts.append(result['id'])
        raise RuntimeError('backup_delivery_failed')
    runner, docker, sent = recordings_runner(tmp_path, age_stub, monkeypatch, send=failing)
    with pytest.raises(RuntimeError, match='backup_delivery_failed'):
        runner.run()
    runner.recover_stop()
    job = Path(runner.cfg['directory']) / runner.journal['id']
    assert (job / 'bundle.tar.age').is_file() and read_json(runner.journal_path)['phase'] == 'upload'
    # The previous release cannot run this config, so it leaves the pending bundle alone ...
    old_runner = load_old('old_backup_runner', 'old_backup_runner.py')
    with pytest.raises(ValueError, match='invalid_backup_config'):
        old_runner.config(private_file(tmp_path / 'runner.json', runner.cfg))
    assert (job / 'bundle.tar.age').is_file()
    # ... and the new runner delivers it under the same ID without a new capture.
    async def stored(path, result, transfer, timeout):
        decrypt_snapshot(path, 'fake', tmp_path / 'received', age=age_stub)
        sent.append(result['id'])
        return {**result, 'status': 'stored', 'duplicate': False}
    again = StagingRunner(runner.cfg, docker=docker, sleep=lambda _: None, send=stored, clock=lambda: NOW)
    again.run()
    assert sent == [job.name] and attempts == [job.name]
    assert verify_recordings_bundle(tmp_path / 'received')['archive']['archive_id'] == ARCHIVE_ID


# spec: ANYM-714
def test_a_recordings_run_holds_its_own_lock_and_messagings_host_lock(tmp_path):
    cfg = recordings_cfg(tmp_path)
    Path(cfg['host_lock']).parent.mkdir(mode=0o700)
    Path(cfg['directory']).mkdir(mode=0o700)
    own = str(Path(cfg['directory']) / '.runner.lock')
    with backup_runner.hold_locks(cfg):
        assert held(own) and held(cfg['host_lock'])
    assert not held(own) and not held(cfg['host_lock'])


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


# spec: ANYM-714
def test_the_worker_config_is_exact_and_a_refusal_writes_only_a_fixed_code(tmp_path, age_stub, monkeypatch):
    monkeypatch.setattr(backup_worker, 'encrypt_snapshot', lambda *a, **k: encrypt_snapshot(*a, age=age_stub, **k))
    work = tmp_path / 'work'
    work.mkdir(mode=0o700)
    snapshots = tmp_path / 'snapshots'
    snapshots.mkdir()
    (tmp_path / 'recipient').write_text('fake')
    config = {'profile': 'recordings', 'id': JOB_ID, 'snapshot_directory': str(snapshots),
              'recipient_file': str(tmp_path / 'recipient'), 'work_directory': str(work)}
    for bad in ({**config, 'dsn_file': '/x'}, {k: v for k, v in config.items() if k != 'id'}, {**config, 'id': '../x'}):
        with pytest.raises(ValueError):
            backup_worker.run(bad)
    with pytest.raises(RecordingsRefused):
        backup_worker.run(config)
    refused = json.loads((work / 'refused.json').read_text())
    assert refused == {'id': JOB_ID, 'status': 'refused', 'code': 'recordings_snapshot_absent'}
    assert not (work / 'snapshot').exists() and not (work / 'bundle.tar.age').exists()
    make_archive(snapshots / 'archive.sqlite')
    (work / 'refused.json').unlink()
    result = backup_worker.run(config)
    assert result['profile'] == 'recordings' and (work / 'bundle.tar.age').is_file() and not (work / 'snapshot').exists()


# spec: ANYM-714
@pytest.mark.parametrize('failure', [None, 'captured', 'encrypt'])
def test_the_worker_keeps_no_plaintext_copy_of_the_archive_outside_staging_on_any_exit(tmp_path, age_stub, monkeypatch, failure):
    def encrypt(*a, **k):
        if failure == 'encrypt':
            raise RuntimeError('backup_encryption_failed')
        return encrypt_snapshot(*a, age=age_stub, **k)
    monkeypatch.setattr(backup_worker, 'encrypt_snapshot', encrypt)
    real_write = backup_worker.write_json
    def write(path, data):
        if failure == 'captured' and Path(path).name == 'captured.json':
            raise OSError('disk full')
        return real_write(path, data)
    monkeypatch.setattr(backup_worker, 'write_json', write)
    work = tmp_path / 'work'
    work.mkdir(mode=0o700)
    snapshots = tmp_path / 'snapshots'
    make_archive(snapshots / 'archive.sqlite')
    (tmp_path / 'recipient').write_text('fake')
    config = {'profile': 'recordings', 'id': JOB_ID, 'snapshot_directory': str(snapshots),
              'recipient_file': str(tmp_path / 'recipient'), 'work_directory': str(work)}
    seen = []
    real_encrypt = encrypt
    def watching(directory, *a, **k):
        seen.extend(sorted(p.relative_to(directory).as_posix() for p in Path(directory).rglob('*')))
        return real_encrypt(directory, *a, **k)
    monkeypatch.setattr(backup_worker, 'encrypt_snapshot', watching)
    if failure:
        with pytest.raises((OSError, RuntimeError)):
            backup_worker.run(config)
    else:
        backup_worker.run(config)
    assert not (work / 'snapshot').exists() and not list(work.rglob('archive.sqlite'))
    assert not list(work.glob('.recordings-backup-*'))
    assert seen in ([], ['manifest.json']), 'only the manifest is ever beside the encryption'
    assert (snapshots / 'archive.sqlite').is_file()
