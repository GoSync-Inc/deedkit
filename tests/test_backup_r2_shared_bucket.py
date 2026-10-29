"""Messaging and the calendar in one R2 bucket under their own prefixes (ANYM-607): receive, inventory, the
verifier's candidate scan, protect and rotate each list and delete only inside their own prefix."""
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path

import pytest

from messaging_service import backup_r2_receiver as r2, backup_r2_verifier as verifier
from tests.test_backup_r2 import Cloud, sha, wire, write_json

MESSAGING = 'deedkit/egor-20261022'
FIRST, SECOND = '20261029T160000Z-0123456789abcdef', '20261030T160000Z-1123456789abcdef'
M_FIRST, M_SECOND = '20261029T180000Z-2123456789abcdef', '20261030T180000Z-3123456789abcdef'


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Root maintenance on fixture paths, as test_backup_r2's `operator` fixture does."""
    original_fstat, original_stat = os.fstat, Path.stat
    ancestors = {(original_stat(p).st_dev, original_stat(p).st_ino) for p in (tmp_path, *tmp_path.parents)}
    def root_info(info):
        values = list(info); values[4] = 0
        if (info.st_dev, info.st_ino) in ancestors:
            values[0] &= ~0o022
        return os.stat_result(values)
    monkeypatch.setattr(os, 'fstat', lambda fd: root_info(original_fstat(fd)))
    monkeypatch.setattr(Path, 'stat', lambda p, *a, **kw: root_info(original_stat(p, *a, **kw)))
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    return tmp_path


def project(tmp_path, name, prefix, profile=None):
    base = tmp_path / name; base.mkdir(mode=0o700)
    state = base / 'state'; state.mkdir(mode=0o700)
    (state / '.receiver.lock').touch(mode=0o600)
    write_json(base / 'credentials.json', {'access_key_id': 'a' * 32, 'secret_access_key': 'b' * 64})
    value = {'v': 1, 'endpoint': 'https://' + '1' * 32 + '.r2.cloudflarestorage.com', 'bucket': 'deedkit-backups',
             'prefix': prefix, 'state_directory': str(state), 'credentials_file': str(base / 'credentials.json'),
             'max_bytes': 20 * 1024 ** 2, 'quota_bytes': 40 * 1024 ** 2, 'max_objects': 2, 'part_bytes': 5 * 1024 ** 2,
             'timeout_seconds': 900, **({'profile': profile} if profile else {})}
    write_json(base / 'config.json', value)
    control = base / 'control'; control.mkdir(mode=0o700)
    return r2.load_config(base / 'config.json'), control / 'protected.json', control / 'proof.json'


def proof(result, profile):
    verification = {'id': '1' * 32, 'verified_at': datetime.now(timezone.utc).isoformat(), 'schema_version': 6,
                    'manifest_sha256': result['snapshot_sha256'],
                    'checks': {name: True for name in (r2.CALENDAR_CHECKS if profile == 'calendar' else r2.CHECKS)}}
    if profile == 'calendar':
        verification.update(profile='calendar', vault_warnings=[])
    return {'v': 1, 'type': 'independent_restore_verification', 'status': 'verified',
            'backup': {k: result[k] for k in ('id', 'bytes', 'sha256', 'snapshot_sha256')}, 'verification': verification}


def keys(cloud, prefix):
    return {key for key in cloud.objects if key.startswith(prefix + '/')}


def deliver(config, cloud, bid, data):
    return r2.receive(config, io.BytesIO(wire(data, bid=bid, snapshot=sha(bid.encode()))), client=cloud)


def protect_first(config, cloud, marker, proof_file, bid, data, profile):
    result = deliver(config, cloud, bid, data)
    write_json(proof_file, proof(result, profile))
    r2.protect(config, marker, proof_file, client=cloud)
    return result


def rotate_to(config, cloud, marker, proof_file, bid, data, profile):
    result = deliver(config, cloud, bid, data)
    write_json(proof_file, proof(result, profile))
    return r2.rotate(config, marker, proof_file, expected_protected_sha256=sha(marker.read_bytes()), client=cloud)


# The second calendar prefix starts with messaging's whole prefix: only the `/objects/` and `/receipts/`
# separators keep them apart.
# spec: ANYM-607
@pytest.mark.parametrize('calendar_prefix', ['deedkit-calendar/egor-20261029', MESSAGING + '-calendar'])
def test_rotation_in_a_shared_bucket_deletes_only_its_own_prefixs_objects_and_receipts(root, calendar_prefix):
    cloud = Cloud()
    messaging, m_marker, m_proof = project(root, 'messaging', MESSAGING)
    calendar, c_marker, c_proof = project(root, 'calendar', calendar_prefix, 'calendar')
    age = r2.AGE_HEADER
    protect_first(messaging, cloud, m_marker, m_proof, M_FIRST, age + b'messaging point' * 8, None)
    protect_first(calendar, cloud, c_marker, c_proof, FIRST, age + b'calendar point' * 8, 'calendar')
    messaging_keys = keys(cloud, MESSAGING)
    messaging_bytes = {key: cloud.objects[key] for key in messaging_keys}
    deleted = []
    original = cloud.delete_object
    def delete_object(**kw):
        deleted.append(kw['Key'])
        return original(**kw)
    cloud.delete_object = delete_object
    assert rotate_to(calendar, cloud, c_marker, c_proof, SECOND, age + b'calendar next' * 8, 'calendar')['status'] == 'rotated'
    assert deleted and all(key.startswith(calendar_prefix + '/') for key in deleted)
    assert {key: cloud.objects[key] for key in keys(cloud, MESSAGING)} == messaging_bytes
    assert all(FIRST not in key for key in cloud.objects) and any(SECOND in key for key in cloud.objects)
    calendar_keys, deleted[:] = keys(cloud, calendar_prefix), []
    calendar_bytes = {key: cloud.objects[key] for key in calendar_keys}
    assert rotate_to(messaging, cloud, m_marker, m_proof, M_SECOND, age + b'messaging next' * 8, None)['status'] == 'rotated'
    assert deleted and all(key.startswith(MESSAGING + '/') and not key.startswith(calendar_prefix + '/') for key in deleted)
    assert {key: cloud.objects[key] for key in keys(cloud, calendar_prefix)} == calendar_bytes


# spec: ANYM-607
@pytest.mark.parametrize('calendar_prefix', ['deedkit-calendar/egor-20261029', MESSAGING + '-calendar'])
def test_admission_inventory_and_the_verifiers_candidate_scan_see_only_their_own_prefix(root, calendar_prefix):
    cloud = Cloud()
    messaging, m_marker, m_proof = project(root, 'messaging', MESSAGING)
    calendar, _, _ = project(root, 'calendar', calendar_prefix, 'calendar')
    age = r2.AGE_HEADER
    protect_first(messaging, cloud, m_marker, m_proof, M_FIRST, age + b'messaging point' * 8, None)
    deliver(messaging, cloud, M_SECOND, age + b'messaging candidate' * 8)  # messaging is at its two objects
    # The calendar's admission counts only its own objects, so messaging's full prefix does not refuse it.
    received = deliver(calendar, cloud, FIRST, age + b'calendar point' * 8)
    assert received['status'] == 'stored'
    inventory = r2.inventory(calendar, cloud)
    assert list(inventory) == [key for key in keys(cloud, calendar_prefix) if '/objects/' in key]
    assert all(not key.startswith(MESSAGING + '/') for key in inventory)
    # The verifier's scan raises uncommitted_cloud_object on any object of its prefix without a receipt:
    # messaging's two objects are not in its view.
    state_fd = os.open(calendar['state_directory'], os.O_RDONLY | os.O_DIRECTORY)
    try:
        selected = verifier.select_candidate(calendar, cloud, state_fd, None)
    finally:
        os.close(state_fd)
    assert json.dumps(selected).count(FIRST) >= 1 and M_FIRST not in json.dumps(selected)


def own(cloud, prefix):
    """A project's own keys: its `<prefix>/objects/` and `<prefix>/receipts/`, whatever other prefix contains it."""
    return {key: data for key, data in cloud.objects.items()
            if key.startswith(prefix + '/objects/') or key.startswith(prefix + '/receipts/')}


# The calendar prefix is shorter here: a parent path of messaging's, or a string-prefix of it.
# spec: ANYM-607
@pytest.mark.parametrize('calendar_prefix', ['deedkit', MESSAGING[:-1]])
def test_a_calendar_prefix_that_contains_messagings_rotates_and_scans_only_its_own_keys(root, calendar_prefix):
    cloud = Cloud()
    messaging, m_marker, m_proof = project(root, 'messaging', MESSAGING)
    calendar, c_marker, c_proof = project(root, 'calendar', calendar_prefix, 'calendar')
    age = r2.AGE_HEADER
    protect_first(messaging, cloud, m_marker, m_proof, M_FIRST, age + b'messaging point' * 8, None)
    deliver(messaging, cloud, M_SECOND, age + b'messaging candidate' * 8)
    protect_first(calendar, cloud, c_marker, c_proof, FIRST, age + b'calendar point' * 8, 'calendar')
    assert set(r2.inventory(calendar, cloud)) == {k for k in own(cloud, calendar_prefix) if '/objects/' in k}
    messaging_bytes, deleted = own(cloud, MESSAGING), []
    original = cloud.delete_object
    def delete_object(**kw):
        deleted.append(kw['Key'])
        return original(**kw)
    cloud.delete_object = delete_object
    assert rotate_to(calendar, cloud, c_marker, c_proof, SECOND, age + b'calendar next' * 8, 'calendar')['status'] == 'rotated'
    assert deleted and all(key.startswith((calendar_prefix + '/objects/', calendar_prefix + '/receipts/')) for key in deleted)
    assert own(cloud, MESSAGING) == messaging_bytes
    state_fd = os.open(calendar['state_directory'], os.O_RDONLY | os.O_DIRECTORY)
    try:
        selected = json.dumps(verifier.select_candidate(calendar, cloud, state_fd, json.loads(c_marker.read_bytes())))
    finally:
        os.close(state_fd)
    assert M_FIRST not in selected and M_SECOND not in selected
