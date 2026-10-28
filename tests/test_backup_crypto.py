"""Staging-space regression tests; transport stubs make no crypto assertion."""
import hashlib
import os
import sys

import pytest

from messaging_service.backup import create_snapshot, verify_bundle
from messaging_service.backup_crypto import decrypt_snapshot, encrypt_snapshot

DUMP = [sys.executable, '-c', 'print("UNIT_DUMP_NOT_A_DATABASE")']


@pytest.fixture
def age_stub(tmp_path):
    path = tmp_path / 'age-stub'
    path.write_text('#!' + sys.executable + '\nimport sys,shutil\n'
        'source=open(sys.argv[-1],"rb") if "-d" in sys.argv else sys.stdin.buffer\n'
        'shutil.copyfileobj(source,sys.stdout.buffer)\n')
    path.chmod(0o700)
    return str(path)


@pytest.mark.parametrize('consume', [False, True])
def test_encryption_roundtrip_can_consume_only_owned_staged_blobs(seeded, tmp_path, age_stub, consume):
    store, *_ = seeded
    snapshot = tmp_path / 'snapshot'
    create_snapshot(store, snapshot, DUMP)
    manifest = verify_bundle(snapshot)
    originals = {row['digest']: (store.files / row['digest']).read_bytes() for row in manifest['blobs']}
    assert originals
    kwargs = {'consume_staged_blobs': True} if consume else {}
    encrypt_snapshot(snapshot, tmp_path / 'bundle', 'fake', age=age_stub, **kwargs)
    assert {p.name for p in (snapshot / 'files').iterdir()} == (set() if consume else set(originals))
    assert (snapshot / 'manifest.json').exists() and (snapshot / 'database.dump').exists()
    assert all((store.files / name).read_bytes() == data for name, data in originals.items())
    decrypt_snapshot(tmp_path / 'bundle', 'fake', tmp_path / 'restored', age=age_stub)
    assert verify_bundle(tmp_path / 'restored') == manifest
    assert all((tmp_path / 'restored/files' / name).read_bytes() == data for name, data in originals.items())


@pytest.mark.parametrize('attack', ['corrupt', 'hardlink', 'invalid_opt_in'])
def test_consumption_rejects_invalid_snapshot_before_removing_any_file(seeded, tmp_path, age_stub, attack):
    store, *_ = seeded
    snapshot = tmp_path / 'snapshot'
    create_snapshot(store, snapshot, DUMP)
    blob = next((snapshot / 'files').iterdir())
    option = True
    if attack == 'corrupt':
        blob.write_bytes(b'corrupt')
    elif attack == 'hardlink':
        os.link(blob, tmp_path / 'shared-copy')
    else:
        option = 'true'
    before = {p.name: p.read_bytes() for p in (snapshot / 'files').iterdir()}
    with pytest.raises((ValueError, RuntimeError)):
        encrypt_snapshot(snapshot, tmp_path / 'bundle', 'fake', age=age_stub, consume_staged_blobs=option)
    assert {p.name: p.read_bytes() for p in (snapshot / 'files').iterdir()} == before
    assert not (tmp_path / 'bundle').exists() and not list(tmp_path.glob('.encrypt-*'))


def test_failed_encoder_never_publishes_or_changes_originals_or_old_backup(seeded, tmp_path):
    store, *_ = seeded
    snapshot = tmp_path / 'snapshot'
    create_snapshot(store, snapshot, DUMP)
    manifest = verify_bundle(snapshot)
    original_hashes = {row['digest']: hashlib.sha256((store.files / row['digest']).read_bytes()).hexdigest()
                       for row in manifest['blobs']}
    old = tmp_path / 'old-bundle'
    old.write_bytes(b'PREVIOUS_VERIFIED_BACKUP_FIXTURE')
    encoder = tmp_path / 'age-fails'
    encoder.write_text('#!' + sys.executable + '\nimport sys\nsys.stdin.buffer.read()\nsys.exit(1)\n')
    encoder.chmod(0o700)
    with pytest.raises(RuntimeError, match='backup_encryption_failed'):
        encrypt_snapshot(snapshot, tmp_path / 'bundle', 'fake', age=str(encoder), consume_staged_blobs=True)
    assert not (tmp_path / 'bundle').exists() and not list(tmp_path.glob('.encrypt-*'))
    assert old.read_bytes() == b'PREVIOUS_VERIFIED_BACKUP_FIXTURE'
    assert all(hashlib.sha256((store.files / name).read_bytes()).hexdigest() == digest
               for name, digest in original_hashes.items())
    # The runner discards this job's consumed staging on failure and captures anew.
    assert (snapshot / 'manifest.json').exists()


def calendar_bundle(directory):
    """A synthetic calendar bundle: stub dump, state from capture_state, manifest as the capture writes it."""
    from messaging_service.backup_calendar import capture_state, write_calendar_manifest
    from tests.test_backup_calendar import write_state
    vault, profile = write_state(directory.parent / (directory.name + '-src'), {'home': b'synthetic'})
    directory.mkdir(mode=0o700)
    (directory / 'database.dump').write_bytes(b'UNIT_DUMP_NOT_A_DATABASE')
    state = capture_state(vault, profile, directory / 'state', {'home': True})
    write_calendar_manifest(directory, captured_at='2026-10-28T12:00:00+00:00', schema_version=4,
        tables={'fcs.migrations': {'rows': 4, 'sha256': '0' * 64}}, type_warnings=[], state=state)
    return directory


def real_age(tmp_path):
    import shutil
    import subprocess
    if not shutil.which('age') or not shutil.which('age-keygen'):
        if os.environ.get('CI'): pytest.fail('CI must provide age')
        pytest.skip('age unavailable locally')
    subprocess.run(['age-keygen', '-o', str(tmp_path / 'identity')], check=True, capture_output=True)
    return subprocess.run(['age-keygen', '-y', str(tmp_path / 'identity')], check=True, capture_output=True,
                          text=True).stdout.strip()


def age_tar(tmp_path, recipient, members):
    import io
    import subprocess
    import tarfile
    plain = io.BytesIO()
    with tarfile.open(fileobj=plain, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name, data in members:
            info = tarfile.TarInfo(name); info.size = len(data); archive.addfile(info, io.BytesIO(data))
    output = tmp_path / ('crafted-' + hashlib.sha256(plain.getvalue()).hexdigest()[:8] + '.age')
    subprocess.run(['age', '-r', recipient, '-o', str(output)], input=plain.getvalue(), check=True)
    return output


# spec: ANYM-605
def test_calendar_bundle_roundtrips_through_real_age(tmp_path):
    from messaging_service.backup_calendar import verify_calendar_bundle
    recipient = real_age(tmp_path)
    snapshot = calendar_bundle(tmp_path / 'snapshot')
    manifest = verify_calendar_bundle(snapshot)
    encrypt_snapshot(snapshot, tmp_path / 'bundle', recipient)
    decrypt_snapshot(tmp_path / 'bundle', tmp_path / 'identity', tmp_path / 'restored')
    assert verify_calendar_bundle(tmp_path / 'restored') == manifest
    for name in manifest['state_files']:
        assert (tmp_path / 'restored/state' / name).read_bytes() == (snapshot / 'state' / name).read_bytes()
    assert not (tmp_path / 'restored/files').exists()


# spec: ANYM-605
def test_state_members_only_in_a_calendar_bundle_and_messaging_members_never_in_one(seeded, tmp_path):
    store, *_ = seeded
    recipient = real_age(tmp_path)
    messaging = tmp_path / 'messaging'
    create_snapshot(store, messaging, DUMP)
    with pytest.raises(ValueError, match='invalid_backup_member'):
        decrypt_snapshot(age_tar(tmp_path, recipient, [('manifest.json', (messaging / 'manifest.json').read_bytes()),
            ('database.dump', (messaging / 'database.dump').read_bytes()), ('state/vault/key', b'k')]),
            tmp_path / 'identity', tmp_path / 'out-one')
    calendar = calendar_bundle(tmp_path / 'calendar')
    with pytest.raises(ValueError, match='invalid_backup_member'):
        decrypt_snapshot(age_tar(tmp_path, recipient, [('manifest.json', (calendar / 'manifest.json').read_bytes()),
            ('database.dump', (calendar / 'database.dump').read_bytes()), ('files/' + 'a' * 64, b'blob')]),
            tmp_path / 'identity', tmp_path / 'out-two')
    # A state member ahead of the calendar manifest is refused too: the profile is read from the first member.
    with pytest.raises(ValueError, match='invalid_backup_member'):
        decrypt_snapshot(age_tar(tmp_path, recipient, [('state/vault/key', (calendar / 'state/vault/key').read_bytes()),
            ('manifest.json', (calendar / 'manifest.json').read_bytes())]), tmp_path / 'identity', tmp_path / 'out-three')
    (calendar / 'files').mkdir()
    with pytest.raises(ValueError, match='invalid_calendar_bundle'):
        encrypt_snapshot(calendar, tmp_path / 'never', recipient)
    assert not any(tmp_path.glob('out-*')) and not (tmp_path / 'never').exists()


# spec: ANYM-605
@pytest.mark.parametrize('extra', ['files', 'recovery'])
def test_a_late_calendar_manifest_never_admits_messaging_members(seeded, tmp_path, extra):
    import json
    store, *_ = seeded
    recipient = real_age(tmp_path)
    messaging = tmp_path / 'messaging'
    create_snapshot(store, messaging, DUMP)
    manifest = json.loads((messaging / 'manifest.json').read_text())
    manifest['profile'] = 'calendar'
    if extra == 'recovery': manifest['format'] = 2
    member = extra + '/' + (manifest['blobs'][0]['digest'] if extra == 'files' else 'b' * 64)
    blob = (messaging / 'files' / manifest['blobs'][0]['digest']).read_bytes() if extra == 'files' else b'r'
    with pytest.raises(ValueError, match='invalid_backup_member'):
        decrypt_snapshot(age_tar(tmp_path, recipient, [('database.dump', (messaging / 'database.dump').read_bytes()),
            ('manifest.json', json.dumps(manifest).encode()), (member, blob)]), tmp_path / 'identity', tmp_path / 'out')
    assert not (tmp_path / 'out').exists()
