"""The calls profile on the backup server (ANYM-781): receiver, verifier, restore helper, follow and examples.

Synthetic SQLite files, a fake cloud and Docker, a pass-through age: no provider, no real call state. The helper in
these tests is the real worker over the real decryption, so a bad bundle is refused by the code that runs in production.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
from types import SimpleNamespace

import pytest

from messaging_service import backup_calls, backup_crypto
from messaging_service import backup_r2_receiver as r2
from messaging_service import backup_restore_worker as worker
from messaging_service.backup_calls import create_calls_snapshot
from messaging_service.backup_recordings import create_recordings_snapshot
from messaging_service.backup_recordings_continuity import synthetic_archive
from test_backup_r2 import BID, Cloud, THIRD, receive, wire
from test_backup_r2_verifier import HEAD, HostEngine, NEW_IMAGE, SelfTestEngine, created, follow_status, follower, instances, module, operator, pins, self_test_setup  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parents[1]
CAPTURED = '2026-11-04T03:00:00+00:00'
SECOND = '20261016T095703Z-0a23456789abcdef'
OK_CHECKS = {'integrity': 'ok', 'row_counts': 'ok', 'row_digests': 'ok'}


def make_state(path, rows=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript('''CREATE TABLE briefs(id TEXT PRIMARY KEY, body TEXT);
        CREATE TABLE calls(id TEXT PRIMARY KEY, brief_id TEXT);
        CREATE TABLE voice_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT);
        CREATE TABLE call_events(seq INTEGER PRIMARY KEY, kind TEXT);''')
    for i in range(rows):
        db.execute('INSERT INTO briefs VALUES(?,?)', (f'b{i}', f'synthetic brief {i}'))
        db.execute('INSERT INTO calls VALUES(?,?)', (f'c{i}', f'b{i}'))
        db.execute('INSERT INTO voice_events(kind) VALUES(?)', (f'synthetic event {i}',))
        db.execute('INSERT INTO call_events(kind) VALUES(?)', (f'synthetic step {i}',))
    db.commit()
    db.close()
    return path


def sealed(tmp_path, name='a', *, rows=3, captured=CAPTURED):
    """A bundle's members, as ANYM-778 seals them, manifest first."""
    staged = make_state(tmp_path / (name + '-staged') / 'voice.sqlite3', rows)
    create_calls_snapshot(staged, tmp_path / name, captured_at=captured)
    return {'manifest.json': (tmp_path / name / 'manifest.json').read_bytes(),
            'state/voice.sqlite3': (tmp_path / name / 'state' / 'voice.sqlite3').read_bytes()}


def stream(members):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w|') as archive:
        for name, data in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(data))
    return out.getvalue()


def sha_of(members):
    return hashlib.sha256(members['manifest.json']).hexdigest()


def rewritten(tmp_path, members, change):
    """The state after `change(db)`, its manifest's size and hash updated and its table facts left as they were."""
    path = tmp_path / 'rewritten.sqlite3'
    path.write_bytes(members['state/voice.sqlite3'])
    db = sqlite3.connect(path)
    change(db)
    db.commit()
    db.close()
    data = path.read_bytes()
    manifest = json.loads(members['manifest.json'])
    manifest.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    return {'manifest.json': (json.dumps(manifest, indent=2) + '\n').encode(), 'state/voice.sqlite3': data}


def worker_config(sha, size, profile='calls'):
    return {'v': 1, 'profile': profile, 'identity_file': '/run/restore/identity', 'work_directory': '/work',
            'database_host': 'restore-postgres', 'database_password': 'a' * 64, 'snapshot_sha256': sha,
            'max_plaintext_bytes': size}


@pytest.fixture
def helper(tmp_path, monkeypatch):
    """The real worker and decryption over a pass-through age; `/work` is the directory a test points it at."""
    age = tmp_path / 'age-pass-through'
    age.write_text('#!' + sys.executable + '\nimport sys,shutil\nshutil.copyfileobj(sys.stdin.buffer,sys.stdout.buffer)\n')
    age.chmod(0o700)
    real = backup_crypto.decrypt_snapshot_stream
    monkeypatch.setattr(worker, 'decrypt_snapshot_stream',
                        lambda source, identity, destination, max_bytes: real(source, identity, destination, age=str(age), max_bytes=max_bytes))
    state = SimpleNamespace(work=None, age=str(age))
    monkeypatch.setattr(worker, 'Path', lambda value: state.work if value == '/work' else Path(value))

    def run(members, sha=None, profile='calls', name='work'):
        state.work = tmp_path / name
        state.work.mkdir(mode=0o700)
        data = stream(members)
        return worker.run(worker_config(sha or sha_of(members), len(data), profile), io.BytesIO(data))
    state.run = run
    return state


class HelperEngine(HostEngine):
    """Synthetic Docker around the real calls helper: the stored ciphertext goes through worker.run, and the host then
    reads its facts the way Docker.restore does."""
    helper = None
    calls_error = None
    change_result = None

    def restore(self, config, receiver, client, receipt, run, payload, secret_dir):
        facts = super().restore(config, receiver, client, receipt, run, payload, secret_dir)
        if r2.profile(config) != 'calls':
            return facts
        if self.calls_error:
            raise self.calls_error
        out = io.BytesIO()
        r2.verify_object(receiver, client, receipt, out)
        self.helper.work = payload
        try:
            worker.run(json.loads((secret_dir / 'worker.json').read_text()), io.BytesIO(out.getvalue()[len(r2.AGE_HEADER):]))
        except Exception as error:
            worker.write_failure(payload, error)
            raise self.v.helper_failure(payload) from None
        result = r2.unique_json(self.v.private_file(payload / 'result.json', 16384))
        return self.change_result(result) if self.change_result else result


@pytest.fixture
def calls_instance(instances, helper, tmp_path):  # noqa: F811  (the imported fixture)
    """A calls verifier instance beside the messaging and calendar ones: its own receiver, state, key, marker and cloud."""
    s = instances
    for name in ('calls-state', 'calls-control', 'calls-verifier'):
        (tmp_path / name).mkdir(mode=0o700)
    control = tmp_path / 'calls-control'
    s.write_json(control / 'credentials.json', {'access_key_id': 'e' * 32, 'secret_access_key': 'f' * 64})
    own = {**r2.load_config(s.messaging['receiver_config_file']), 'state_directory': str(tmp_path / 'calls-state'),
           'credentials_file': str(control / 'credentials.json'), 'prefix': 'calls', 'profile': 'calls'}
    s.write_json(control / 'receiver.json', own)
    identity = control / 'identity'
    identity.write_text('SYNTHETIC_NOT_A_KEY')
    identity.chmod(0o600)
    s.calls = {**s.messaging, 'profile': 'calls', 'receiver_config_file': str(control / 'receiver.json'),
               'directory': str(tmp_path / 'calls-verifier'), 'identity_file': str(identity),
               'protected_file': str(control / 'protected.json')}
    s.calls_path = control / 'verifier.json'
    s.write_json(s.calls_path, s.calls)
    s.calls_receiver = own
    s.calls_cloud = Cloud()
    s.engine = HelperEngine(s.v, s.engine.root, s.host)
    s.engine.helper = helper
    s.deliver = lambda members, bid: receive(own, s.calls_cloud, data=r2.AGE_HEADER + stream(members), bid=bid, snapshot=sha_of(members))
    s.verify = lambda: s.v.run(s.calls, client=s.calls_cloud, engine=s.engine)
    s.directory = Path(s.calls['directory'])
    s.proof = lambda run: json.loads((s.directory / f'proof-{run}.json').read_text())
    return s


def freshness():
    spec = importlib.util.spec_from_file_location('recordings_backup_freshness', ROOT / 'deploy' / 'recordings-backup-freshness.py')
    module_ = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module_)
    return module_


def receipt_of(sha='b' * 64):
    return {'wire': {'id': BID, 'bytes': 100, 'sha256': 'a' * 64, 'snapshot_sha256': sha, 'received_at': '2026-10-16T09:17:03+00:00'}}


TABLES = {name: {'rows': 3, 'sha256': 'c' * 64} for name in ('briefs', 'calls', 'voice_events')}
MANIFEST = {'profile': 'calls', 'schema_version': 1, 'captured_at': CAPTURED, 'tables': TABLES}
FACTS = {'v': 1, 'status': 'verified', 'profile': 'calls', 'schema_version': 1, 'manifest_sha256': 'b' * 64,
         'captured_at': CAPTURED, 'checks': OK_CHECKS, 'tables': TABLES}


# --- criterion 1: the contract -----------------------------------------------------------------------------------------

# spec: ANYM-781
def test_the_contract_constants_are_the_bundle_format_and_the_three_checks():
    assert r2.CALLS_SCHEMA_VERSION == backup_calls.SCHEMA_VERSION == 1
    assert r2.CALLS_CHECKS == set(worker.CALLS_CHECKS) == set(OK_CHECKS)
    assert 'calls' in r2.PROFILES


# spec: ANYM-781
def test_the_helper_result_is_exactly_the_contract_and_holds_no_row_content(tmp_path, helper):
    members = sealed(tmp_path)
    result = helper.run(members)
    manifest = json.loads(members['manifest.json'])
    assert result == {'v': 1, 'status': 'verified', 'profile': 'calls', 'schema_version': 1, 'manifest_sha256': sha_of(members),
                      'captured_at': CAPTURED, 'checks': OK_CHECKS, 'tables': manifest['tables']}
    assert manifest['user_version'] == 0  # the calls database's, never the proof's schema version
    written = (helper.work / 'result.json').read_text()
    assert json.loads(written) == result and 'synthetic' not in written
    assert (helper.work / r2.CALLS_MANIFEST).read_bytes() == members['manifest.json']


# spec: ANYM-781
def test_a_good_calls_proof_is_promoted_with_the_manifests_capture_time(calls_instance):
    s = calls_instance
    members = sealed(s.host.parent)
    s.deliver(members, BID)
    result = s.verify()
    assert result['status'] == 'verified' and result['backup_id'] == BID
    verification = s.proof(result['verification_id'])['verification']
    assert verification['captured_at'] == CAPTURED and verification['profile'] == 'calls' and verification['schema_version'] == 1
    assert verification['checks'] == OK_CHECKS and verification['tables'] == json.loads(members['manifest.json'])['tables']
    assert verification['verified_at'] != CAPTURED
    assert json.loads(Path(s.calls['protected_file']).read_text())['backups'][0]['id'] == BID


# spec: ANYM-781
def test_an_old_snapshot_delivered_late_counts_its_age_from_captured_at_through_the_whole_chain(calls_instance):
    s = calls_instance
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=5)).isoformat()
    s.deliver(sealed(s.host.parent, captured=old), BID)
    result = s.verify()
    assert s.proof(result['verification_id'])['verification']['captured_at'] == old
    code, answer = freshness().evaluate(s.directory, 36, now)
    assert answer['status'] == 'stale' and code != 0 and answer['captured_at'] == old
    assert answer['age_hours'] >= 119 and answer['verified_age_hours'] < 1


# spec: ANYM-781
def test_a_fresh_snapshot_reads_fresh_through_the_whole_chain(calls_instance):
    s = calls_instance
    now = datetime.now(timezone.utc)
    s.deliver(sealed(s.host.parent, captured=(now - timedelta(hours=2)).isoformat()), BID)
    s.verify()
    assert freshness().evaluate(s.directory, 36, now)[1]['status'] == 'fresh'


# spec: ANYM-781
@pytest.mark.parametrize('change', ['captured_at', 'tables'])
def test_a_helper_that_lies_about_the_manifest_is_refused_and_protects_nothing(calls_instance, change):
    s = calls_instance
    s.deliver(sealed(s.host.parent), BID)
    now = datetime.now(timezone.utc).isoformat()

    def lie(result):
        if change == 'captured_at':
            return {**result, 'captured_at': now}
        return {**result, 'tables': {**result['tables'], 'briefs': {'rows': 99, 'sha256': 'd' * 64}}}
    s.engine.change_result = lie
    with pytest.raises(ValueError, match='invalid_helper_result'):
        s.verify()
    assert not Path(s.calls['protected_file']).exists() and not list(s.directory.glob('proof-*'))


# spec: ANYM-781
@pytest.mark.parametrize('case', ['absent', 'other_bytes', 'not_json'])
def test_the_manifest_copy_must_be_the_one_the_receipt_names(calls_instance, case):
    s = calls_instance
    v = s.v
    payload = s.directory / 'payload-test'
    payload.mkdir(mode=0o700)
    raw = {'other_bytes': b'{"profile":"calls"}\n', 'not_json': b'not json'}.get(case)
    named = 'b' * 64
    if raw is not None:
        (payload / r2.CALLS_MANIFEST).write_bytes(raw)
        (payload / r2.CALLS_MANIFEST).chmod(0o600)
        if case == 'not_json':
            named = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match='invalid_helper_result'):
        v.calls_manifest(payload, receipt_of(named))


# spec: ANYM-781
def test_a_calls_proof_and_another_profiles_proof_never_cross():
    v = module()
    receipt = receipt_of()
    proof = v.make_proof(receipt, FACTS, 'c' * 32, 'calls', MANIFEST)
    assert r2.validate_proof(proof, receipt, 'calls')
    for other in ('messaging', 'calendar', 'recordings'):
        with pytest.raises(ValueError, match='verifier_profile_mismatch'):
            r2.validate_proof(proof, receipt, other)
    with pytest.raises(ValueError, match='verifier_profile_mismatch'):
        v.make_proof(receipt, FACTS, 'c' * 32)
    archive = {'archive_id': 'arch-x', 'incarnation': 1, 'sequence': 3, 'records': 2}
    recordings = {'v': 1, 'status': 'verified', 'profile': 'recordings', 'schema_version': 4, 'manifest_sha256': 'b' * 64,
                  'checks': {k: True for k in r2.RECORDINGS_CHECKS}, 'archive': archive, 'captured_at': CAPTURED}
    with pytest.raises(ValueError, match='verifier_profile_mismatch'):
        r2.validate_proof(v.make_proof(receipt, recordings, 'c' * 32, 'recordings'), receipt, 'calls')


# spec: ANYM-781
@pytest.mark.parametrize('bad', [
    {'checks': {k: True for k in OK_CHECKS}},
    {'checks': {k: True for k in r2.RECORDINGS_CHECKS}},
    {'checks': {'integrity': 'ok', 'row_counts': 'ok'}},
    {'checks': {**OK_CHECKS, 'row_digests': 'mismatch'}},
    {'schema_version': 0}, {'schema_version': 2}, {'schema_version': True},
    {'captured_at': 'yesterday'}, {'captured_at': '2026-11-04T03:00:00'},
    {'tables': {}}, {'tables': {'bad name': {'rows': 1, 'sha256': 'c' * 64}}},
    {'tables': {'briefs': {'rows': -1, 'sha256': 'c' * 64}}}, {'tables': {'briefs': {'rows': 1, 'sha256': 'x'}}},
    {'tables': {'briefs': {'rows': 1, 'sha256': 'c' * 64, 'text': 'row'}}},
    {'manifest_sha256': 'd' * 64},
], ids=['true_checks', 'other_checks', 'missing_check', 'failed_check', 'schema_0', 'schema_2', 'schema_true', 'instant_word',
        'instant_naive', 'no_tables', 'table_name', 'negative_rows', 'bad_digest', 'row_content', 'other_manifest'])
def test_the_verifier_refuses_every_result_outside_the_contract(bad):
    v = module()
    with pytest.raises(ValueError, match='invalid_helper_result'):
        v.make_proof(receipt_of(), {**FACTS, **bad}, 'c' * 32, 'calls', {**MANIFEST, **{k: value for k, value in bad.items() if k in ('tables', 'captured_at')}})


# spec: ANYM-781
@pytest.mark.parametrize('manifest', [None, 'text', {**MANIFEST, 'captured_at': '2026-11-05T03:00:00+00:00'},
                                      {**MANIFEST, 'tables': {'briefs': {'rows': 1, 'sha256': 'c' * 64}}},
                                      {**MANIFEST, 'profile': 'recordings'}, {**MANIFEST, 'schema_version': 2}],
                         ids=['none', 'not_an_object', 'other_capture', 'other_tables', 'other_profile', 'other_format'])
def test_make_proof_refuses_a_capture_time_the_verified_manifest_does_not_hold(manifest):
    with pytest.raises(ValueError, match='invalid_helper_result'):
        module().make_proof(receipt_of(), FACTS, 'c' * 32, 'calls', manifest)


# spec: ANYM-781
def test_a_capture_time_in_the_future_never_looks_fresh():
    v = module()
    receipt = receipt_of()
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    with pytest.raises(ValueError, match='invalid_helper_result'):
        v.make_proof(receipt, {**FACTS, 'captured_at': future}, 'c' * 32, 'calls', {**MANIFEST, 'captured_at': future})
    proof = v.make_proof(receipt, FACTS, 'c' * 32, 'calls', MANIFEST)
    forged = {**proof, 'verification': {**proof['verification'], 'captured_at': future}}
    with pytest.raises(ValueError, match='invalid_restore_time'):
        r2.validate_proof(forged, receipt, 'calls')


# spec: ANYM-781
@pytest.mark.parametrize('field,value', [('checks', {k: True for k in OK_CHECKS}), ('schema_version', 2), ('captured_at', 'yesterday'),
                                         ('tables', {}), ('tables', {'bad name': {'rows': 1, 'sha256': 'c' * 64}})],
                         ids=['true_checks', 'schema_2', 'instant_word', 'no_tables', 'table_name'])
def test_the_receiver_refuses_a_proof_outside_the_contract(field, value):
    v = module()
    receipt = receipt_of()
    proof = v.make_proof(receipt, FACTS, 'c' * 32, 'calls', MANIFEST)
    changed = {**proof, 'verification': {**proof['verification'], field: value}}
    with pytest.raises(ValueError, match='independent_restore_proof_required'):
        r2.validate_proof(changed, receipt, 'calls')


# spec: ANYM-781
def test_the_other_profiles_keep_their_own_true_checks():
    v = module()
    receipt = receipt_of()
    facts = {'v': 1, 'status': 'verified', 'schema_version': v.SCHEMAS[-1], 'manifest_sha256': 'b' * 64,
             'checks': {k: 'ok' for k in r2.CHECKS}}
    with pytest.raises(ValueError, match='invalid_helper_result'):
        v.make_proof(receipt, facts, 'c' * 32)
    proof = v.make_proof(receipt, {**facts, 'checks': {k: True for k in r2.CHECKS}}, 'c' * 32)
    assert r2.validate_proof(proof, receipt)
    words = {**proof, 'verification': {**proof['verification'], 'checks': {k: 'ok' for k in r2.CHECKS}}}
    with pytest.raises(ValueError, match='independent_restore_proof_required'):
        r2.validate_proof(words, receipt)


# --- criterion 2: the profile list --------------------------------------------------------------------------------------

# spec: ANYM-781
def test_a_calls_receiver_accepts_a_delivery_and_stores_it_under_its_own_prefix_and_state(calls_instance):
    s = calls_instance
    loaded = r2.load_config(s.calls['receiver_config_file'])
    assert r2.profile(loaded) == 'calls'
    s.deliver(sealed(s.host.parent), BID)
    keys = list(s.calls_cloud.objects)
    assert keys and all(key.startswith('calls/') for key in keys) and not s.cloud.objects.keys() & set(keys)
    assert (Path(s.calls_receiver['state_directory']) / (BID + '.attempt.json')).exists()


# spec: ANYM-781
def test_an_unknown_profile_is_refused_in_the_config_and_on_the_wire(calls_instance):
    s = calls_instance
    path = Path(s.calls['receiver_config_file'])
    s.write_json(path, {**s.calls_receiver, 'profile': 'notes'})
    with pytest.raises(ValueError, match='invalid_config'):
        r2.load_config(path)
    s.write_json(path, s.calls_receiver)
    header = {'v': 2, 'id': BID, 'max_bytes': 64, 'snapshot_sha256': 'a' * 64}
    assert r2.validate_header(header, 1024) == header
    with pytest.raises(ValueError, match='invalid_header'):
        r2.validate_header({**header, 'profile': 'notes'}, 1024)
    with pytest.raises(ValueError, match='invalid_header'):
        r2.receive(s.calls_receiver, io.BytesIO(wire().replace(b'{"v"', b'{"profile": "notes", "v"', 1)), client=s.calls_cloud)


# spec: ANYM-781
def test_the_verifier_config_journal_and_host_record_accept_calls_and_the_two_configs_must_agree(calls_instance):
    s = calls_instance
    v = s.v
    assert v.load_config(s.calls_path)['profile'] == 'calls'
    assert v.names('a' * 32, 'calls')['helper'].startswith('deedkit-verify-calls-')
    s.write_json(Path(s.calls['receiver_config_file']), {**s.calls_receiver, 'profile': 'recordings'})
    with pytest.raises(ValueError, match='verifier_profile_mismatch'):
        v.load_config(s.calls_path)
    s.write_json(Path(s.calls['receiver_config_file']), s.calls_receiver)
    s.deliver(sealed(s.host.parent), BID)
    s.engine.calls_error = RuntimeError('restore_docker_failed')
    with pytest.raises(RuntimeError, match='restore_docker_failed'):
        s.verify()
    journal = json.loads((s.directory / 'active.json').read_text())
    assert journal['profile'] == 'calls' and s.engine.admissions[0]['profile'] == 'calls'
    s.engine.calls_error = None
    assert s.verify()['verification_id'] == journal['run']


# spec: ANYM-781
@pytest.mark.parametrize('configured', ['calendar', 'recordings'])
def test_a_calls_bundle_is_refused_under_another_profile(tmp_path, helper, configured):
    with pytest.raises(worker.RestoreStageError, match='verifier_profile_mismatch'):
        helper.run(sealed(tmp_path), profile=configured)
    assert not (helper.work / 'result.json').exists()


# spec: ANYM-781
def test_a_recordings_bundle_is_refused_under_the_calls_profile(tmp_path, helper):
    staged = synthetic_archive(tmp_path / 'r-staged' / 'archive.sqlite', [
        {'ref': 'synthetic01', 'provider_id': 'p1', 'transcript': 'synthetic words', 'summary': 'synthetic'}])
    create_recordings_snapshot(staged, tmp_path / 'r')
    members = {'manifest.json': (tmp_path / 'r' / 'manifest.json').read_bytes(),
               'state/archive.sqlite': (tmp_path / 'r' / 'state' / 'archive.sqlite').read_bytes()}
    with pytest.raises(worker.RestoreStageError, match='verifier_profile_mismatch'):
        helper.run(members)
    assert not (helper.work / 'result.json').exists()


# spec: ANYM-781
def test_the_worker_still_refuses_an_unknown_profile(tmp_path, helper):
    helper.work = tmp_path
    with pytest.raises(ValueError, match='invalid_restore_worker_config'):
        worker.run(worker_config('b' * 64, 10, 'notes'), io.BytesIO(b''))


# --- criterion 3: the isolated restore proof ----------------------------------------------------------------------------

def flipped(tmp_path, members):
    data = bytearray(members['state/voice.sqlite3'])
    data[len(data) // 2] ^= 1
    return {**members, 'state/voice.sqlite3': bytes(data)}


def damaged_with_its_hash(tmp_path, members):
    data = bytearray(members['state/voice.sqlite3'])
    data[100:len(data)] = bytes(len(data) - 100)
    manifest = json.loads(members['manifest.json'])
    manifest.update(sha256=hashlib.sha256(bytes(data)).hexdigest())
    return {'manifest.json': (json.dumps(manifest, indent=2) + '\n').encode(), 'state/voice.sqlite3': bytes(data)}


def changed_row(tmp_path, members):
    return rewritten(tmp_path, members, lambda db: db.execute("UPDATE briefs SET body='synthetic other' WHERE id='b0'"))


def removed_table(tmp_path, members):
    return rewritten(tmp_path, members, lambda db: db.execute('DROP TABLE call_events'))


def removed_required_table(tmp_path, members):
    changed = rewritten(tmp_path, members, lambda db: db.execute('DROP TABLE voice_events'))
    manifest = json.loads(changed['manifest.json'])
    del manifest['tables']['voice_events']
    return {**changed, 'manifest.json': (json.dumps(manifest, indent=2) + '\n').encode()}


def other_database(tmp_path, members):
    return {'manifest.json': sealed(tmp_path, 'other', rows=5)['manifest.json'], 'state/voice.sqlite3': members['state/voice.sqlite3']}


def extra_member(tmp_path, members):
    return {**members, 'state/extra': b'synthetic'}


def no_state(tmp_path, members):
    return {'manifest.json': members['manifest.json']}


def manifest_second(tmp_path, members):
    return {'state/voice.sqlite3': members['state/voice.sqlite3'], 'manifest.json': members['manifest.json']}


TAMPERS = [flipped, damaged_with_its_hash, changed_row, removed_table, removed_required_table, other_database, extra_member,
           no_state, manifest_second]


# spec: ANYM-781
@pytest.mark.parametrize('tamper', TAMPERS, ids=lambda item: item.__name__)
def test_a_bad_bundle_is_refused_with_the_fixed_code_and_no_result(tmp_path, helper, tamper):
    with pytest.raises(worker.RestoreStageError, match='restore_mismatch') as refused:
        helper.run(tamper(tmp_path, sealed(tmp_path)))
    assert not (helper.work / 'result.json').exists()
    assert worker.write_failure(helper.work, refused.value) == 'restore_mismatch'
    written = (helper.work / 'failure.json').read_text()
    assert json.loads(written) == {'v': 1, 'status': 'failed', 'error': 'restore_mismatch'} and 'synthetic' not in written


# spec: ANYM-781
def test_a_whole_other_bundle_under_the_receipts_manifest_hash_is_a_mismatch(tmp_path, helper):
    with pytest.raises(worker.RestoreStageError, match='restore_mismatch'):
        helper.run(sealed(tmp_path), sha='d' * 64)
    assert not (helper.work / 'result.json').exists()


# spec: ANYM-781
@pytest.mark.parametrize('tamper', TAMPERS, ids=lambda item: item.__name__)
def test_a_bad_bundle_leaves_the_previous_point_and_the_status_says_restore_mismatch(calls_instance, tamper):
    s = calls_instance
    root = s.host.parent
    s.deliver(sealed(root), BID)
    first = s.verify()
    marker = Path(s.calls['protected_file']).read_bytes()
    s.deliver(tamper(root, sealed(root, 'second')), THIRD)
    with pytest.raises(RuntimeError, match='restore_mismatch'):
        s.verify()
    assert Path(s.calls['protected_file']).read_bytes() == marker
    for name in ('status.json', 'last-failure.json'):
        assert json.loads((s.directory / name).read_text())['error'] == 'restore_mismatch'
    assert json.loads((s.directory / 'last-success.json').read_text())['verification_id'] == first['verification_id']
    assert len(list(s.directory.glob('proof-*.json'))) == 1 and not list(s.directory.glob('payload-*'))


# spec: ANYM-781
def test_the_helper_never_writes_into_the_snapshot_and_leaves_it_read_only(tmp_path, helper):
    members = sealed(tmp_path)
    helper.run(members)
    state = helper.work / 'bundle' / 'state' / 'voice.sqlite3'
    assert state.read_bytes() == members['state/voice.sqlite3']
    assert sorted(p.name for p in state.parent.iterdir()) == ['voice.sqlite3']


# --- criterion 4: receive -> restore -> protect -> rotate ---------------------------------------------------------------

# spec: ANYM-781
def test_two_bundles_the_second_corrupt_keep_the_first_and_a_third_good_one_replaces_it(calls_instance):
    s = calls_instance
    root = s.host.parent
    now = datetime.now(timezone.utc)
    first_capture = (now - timedelta(hours=30)).isoformat()
    s.deliver(sealed(root, 'one', captured=first_capture), BID)
    first = s.verify()
    assert first['backup_id'] == BID
    marker = Path(s.calls['protected_file']).read_bytes()

    late_capture = (now - timedelta(hours=1)).isoformat()
    s.deliver(flipped(root, sealed(root, 'two', captured=late_capture)), SECOND)
    with pytest.raises(RuntimeError, match='restore_mismatch'):
        s.verify()
    assert Path(s.calls['protected_file']).read_bytes() == marker
    # The rejected delivery is newer, and the proof of the first stays current: freshness still reads the first capture.
    answer = freshness().evaluate(s.directory, 36, now)[1]
    assert answer['captured_at'] == first_capture and answer['status'] == 'fresh' and answer['last_error'] == 'restore_mismatch'

    # The refused candidate holds the second object slot until the operator's command.
    with pytest.raises(RuntimeError, match='receiver_quota'):
        s.deliver(sealed(root, 'blocked'), THIRD)
    assert s.v.discard_refused(s.calls, SECOND, client=s.calls_cloud) == {'v': 1, 'status': 'discarded', 'backup_id': SECOND}
    assert s.verify()['status'] == 'idle' and Path(s.calls['protected_file']).read_bytes() == marker

    third_capture = (now - timedelta(minutes=5)).isoformat()
    s.deliver(sealed(root, 'three', captured=third_capture), THIRD)
    third = s.verify()
    assert third['backup_id'] == THIRD and Path(s.calls['protected_file']).read_bytes() != marker
    assert s.proof(third['verification_id'])['verification']['captured_at'] == third_capture
    assert freshness().evaluate(s.directory, 36, now)[1]['captured_at'] == third_capture
    objects = [key for key in s.calls_cloud.objects if '/objects/' in key]
    assert len(objects) == 1 and objects[0].startswith('calls/objects/' + THIRD + '-')


# spec: ANYM-781
def test_calls_rotation_keeps_as_many_points_as_the_calendar_profile(calls_instance):
    s = calls_instance
    root = s.host.parent
    s.deliver(sealed(root, 'one'), BID)
    s.verify()
    s.deliver(sealed(root, 'two'), THIRD)
    s.verify()
    s.v.run(s.calendar, client=s.calendar_cloud, engine=s.engine)
    receive(r2.load_config(s.calendar['receiver_config_file']), s.calendar_cloud, bid=THIRD)
    s.v.run(s.calendar, client=s.calendar_cloud, engine=s.engine)
    def points(cloud):
        return [key for key in cloud.objects if '/objects/' in key]
    assert len(points(s.calls_cloud)) == len(points(s.calendar_cloud)) == 1
    assert s.calls_receiver['max_objects'] == r2.load_config(s.calendar['receiver_config_file'])['max_objects']
    example = json.loads((ROOT / 'deploy' / 'backup-runner-calls.example.json').read_text())
    assert example['keep_local'] == 1


# --- the operator's way past a refused candidate ----------------------------------------------------------------------------

@pytest.fixture
def refused(calls_instance):
    """A protected first bundle and a second one the helper refused: its journal and object remain."""
    s = calls_instance
    root = s.host.parent
    s.deliver(sealed(root, 'one'), BID)
    s.first = s.verify()
    s.marker = Path(s.calls['protected_file']).read_bytes()
    s.deliver(flipped(root, sealed(root, 'two')), SECOND)
    with pytest.raises(RuntimeError, match='restore_mismatch'):
        s.verify()
    s.discard = lambda bid=SECOND: s.v.discard_refused(s.calls, bid, client=s.calls_cloud)
    s.candidate_keys = lambda: [key for key in s.calls_cloud.objects if SECOND in key]
    s.attempt = Path(s.calls_receiver['state_directory']) / (SECOND + '.attempt.json')
    s.protected_keys = [key for key in s.calls_cloud.objects if BID in key]
    return s


def assert_replaceable(s):
    """The refused candidate is gone, the point is untouched, the next run is idle and a good bundle rotates."""
    assert not s.candidate_keys() and not s.attempt.exists() and not (s.directory / 'active.json').exists()
    assert Path(s.calls['protected_file']).read_bytes() == s.marker
    assert all(key in s.calls_cloud.objects for key in s.protected_keys)
    assert s.verify()['status'] == 'idle'
    s.deliver(sealed(s.host.parent, 'three'), THIRD)
    assert s.verify()['backup_id'] == THIRD


# spec: ANYM-781
def test_the_discard_removes_exactly_the_refused_candidate_and_keeps_its_record(refused):
    s = refused
    assert s.candidate_keys() and s.attempt.exists()
    assert s.discard() == {'v': 1, 'status': 'discarded', 'backup_id': SECOND}
    record = json.loads((s.directory / f'discarded-{SECOND}.json').read_text())
    assert record['backup_id'] == SECOND and record['failure']['error'] == 'restore_mismatch'
    assert record['journal']['receipt']['wire']['id'] == SECOND and 'synthetic brief' not in json.dumps(record)
    assert_replaceable(s)


# spec: ANYM-781
def test_the_discard_command_line_prints_its_outcome_and_a_refusal_is_a_fixed_code(refused, monkeypatch, capsys):
    s = refused
    monkeypatch.setattr(s.v.r2, 'make_client', lambda receiver: s.calls_cloud)
    assert s.v.main(['--discard-refused', THIRD, '--config', str(s.calls_path)]) == 1
    assert json.loads(capsys.readouterr().err)['error'] == 'discard_not_refused'
    assert s.v.main(['--discard-refused', SECOND, '--config', str(s.calls_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {'v': 1, 'status': 'discarded', 'backup_id': SECOND}
    assert_replaceable(s)


def nothing_changed(s, before):
    assert dict(s.calls_cloud.objects) == before and s.attempt.exists() and (s.directory / 'active.json').exists()
    assert Path(s.calls['protected_file']).read_bytes() == s.marker
    assert not list(s.directory.glob('discarded-*'))


# spec: ANYM-781
@pytest.mark.parametrize('case', ['other_id', 'transient_failure', 'cleanup_incomplete', 'proof_ready', 'marker_moved', 'uncommitted'])
def test_the_discard_refuses_anything_but_a_finished_refusal_of_that_candidate(refused, case):
    s = refused
    before = dict(s.calls_cloud.objects)
    status_path = s.directory / 'status.json'
    journal_path = s.directory / 'active.json'
    status, journal = json.loads(status_path.read_text()), json.loads(journal_path.read_text())
    bid = SECOND
    if case == 'other_id':
        bid = THIRD
    elif case == 'transient_failure':
        s.write_json(status_path, {**status, 'error': 'restore_docker_failed'})
    elif case == 'cleanup_incomplete':
        s.write_json(status_path, {**status, 'cleanup_complete': False})
    elif case == 'proof_ready':
        s.write_json(journal_path, {**journal, 'phase': 'proof_ready'})
    elif case == 'marker_moved':
        s.write_json(journal_path, {**journal, 'prior_sha256': 'd' * 64})
    else:
        attempt = json.loads(s.attempt.read_text())
        s.attempt.write_text(json.dumps({**attempt, 'phase': 'uploading'}))
    with pytest.raises(ValueError, match='discard_not_refused'):
        s.discard(bid)
    nothing_changed(s, before)


# spec: ANYM-781
def test_the_discard_never_removes_a_protected_point(refused):
    s = refused
    before = dict(s.calls_cloud.objects)
    with pytest.raises(ValueError, match='discard_protected'):
        s.discard(BID)
    nothing_changed(s, before)
    # A record of an earlier discard does not open the way either, whatever it names.
    s.write_json(s.directory / f'discarded-{BID}.json', {'v': 1, 'backup_id': BID, 'journal': json.loads((s.directory / 'active.json').read_text())})
    with pytest.raises(ValueError, match='discard_protected'):
        s.discard(BID)
    assert dict(s.calls_cloud.objects) == before


# spec: ANYM-781
def test_the_discard_is_calls_only_and_refuses_without_a_journal(calls_instance):
    s = calls_instance
    with pytest.raises(ValueError, match='discard_calls_only'):
        s.v.discard_refused(s.calendar, BID, client=s.calendar_cloud)
    with pytest.raises(ValueError, match='invalid_backup_id'):
        s.v.discard_refused(s.calls, '../x', client=s.calls_cloud)
    with pytest.raises(ValueError, match='discard_not_refused'):
        s.v.discard_refused(s.calls, BID, client=s.calls_cloud)
    for code in ('discard_calls_only', 'discard_not_refused', 'discard_protected', 'discard_unconfirmed'):
        assert s.v.failure_code(ValueError(code)) == code


# spec: ANYM-781
@pytest.mark.parametrize('held', ['verifier', 'receiver'])
def test_the_discard_changes_nothing_while_a_verifier_or_an_upload_holds_its_lock(refused, held):
    import fcntl
    s = refused
    before = dict(s.calls_cloud.objects)
    path, code = ((s.directory / '.verifier.lock', 'verifier_busy') if held == 'verifier' else
                  (Path(s.calls_receiver['state_directory']) / '.receiver.lock', 'receiver_busy'))
    lock = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        with pytest.raises(RuntimeError, match=code):
            s.discard()
    finally:
        os.close(lock)
    nothing_changed(s, before)
    assert s.discard()['status'] == 'discarded'


# spec: ANYM-781
@pytest.mark.parametrize('stop', ['after_the_record', 'after_the_object', 'after_the_receipt', 'after_the_attempt'])
def test_a_discard_stopped_at_any_step_harms_nothing_and_the_same_command_finishes_it(refused, stop, monkeypatch):
    s = refused
    v = s.v
    real_delete, real_write, real_unlink = s.calls_cloud.delete_object, v.local.write_maintenance_json, os.unlink

    class Stop(Exception):
        pass

    def write(name, value, **kw):
        result = real_write(name, value, **kw)
        if str(name).startswith('discarded-') and stop == 'after_the_record':
            raise Stop
        return result

    def delete(**kw):
        result = real_delete(**kw)
        if (stop == 'after_the_object' and '/objects/' in kw['Key']) or (stop == 'after_the_receipt' and '/receipts/' in kw['Key']):
            raise Stop
        return result

    def unlink(path, **kw):
        real_unlink(path, **kw)
        if stop == 'after_the_attempt' and str(path).endswith('.attempt.json'):
            raise Stop
    monkeypatch.setattr(v.local, 'write_maintenance_json', write)
    monkeypatch.setattr(s.calls_cloud, 'delete_object', delete)
    monkeypatch.setattr(v.os, 'unlink', unlink)
    with pytest.raises((Stop, RuntimeError)):  # cloud_call turns a stop inside the SDK call into storage_unavailable
        s.discard()
    monkeypatch.setattr(v.local, 'write_maintenance_json', real_write)
    monkeypatch.setattr(s.calls_cloud, 'delete_object', real_delete)
    monkeypatch.setattr(v.os, 'unlink', real_unlink)
    # The journal is still there: the timer's retry fails and moves no point.
    assert (s.directory / 'active.json').exists()
    with pytest.raises((RuntimeError, ValueError)):
        s.verify()
    assert Path(s.calls['protected_file']).read_bytes() == s.marker
    assert s.discard()['status'] == 'discarded'
    assert_replaceable(s)


# spec: ANYM-781
def test_a_discard_that_cannot_confirm_the_removal_keeps_the_journal(refused, monkeypatch):
    s = refused
    monkeypatch.setattr(s.calls_cloud, 'delete_object', lambda **kw: {})
    with pytest.raises(RuntimeError, match='discard_unconfirmed'):
        s.discard()
    assert (s.directory / 'active.json').exists() and Path(s.calls['protected_file']).read_bytes() == s.marker


# --- criterion 5: failure isolation -------------------------------------------------------------------------------------

# spec: ANYM-781
@pytest.mark.parametrize('error,code', [(TimeoutError('restore_deadline'), 'restore_deadline'), (RuntimeError('anything'), 'independent_restore_failed'),
                                        (OSError('synthetic'), 'independent_restore_failed')], ids=['timeout', 'exception', 'os_error'])
def test_a_raising_calls_restore_releases_the_lock_and_the_next_profiles_verify(calls_instance, error, code):
    s = calls_instance
    s.deliver(sealed(s.host.parent), BID)
    s.engine.calls_error = error
    with pytest.raises(type(error)):
        s.verify()
    assert json.loads((s.directory / 'status.json').read_text())['error'] == code
    assert not (s.host / 'admission.json').exists()
    assert not [item for item in s.engine.resources if 'calls' in item[1]]
    assert s.v.run(s.messaging, client=s.cloud, engine=s.engine)['status'] == 'verified'
    assert s.v.run(s.calendar, client=s.calendar_cloud, engine=s.engine)['status'] == 'verified'
    assert all(('remove', name) in s.engine.log for name in created(s.engine, 'calls'))
    s.engine.calls_error = None
    assert s.verify()['status'] == 'verified'


# spec: ANYM-781
def test_a_calls_refusal_never_fails_or_rotates_the_other_profiles(calls_instance):
    s = calls_instance
    root = s.host.parent
    runs = ((s.messaging, s.cloud), (s.calendar, s.calendar_cloud))
    for config, cloud in runs:
        assert s.v.run(config, client=cloud, engine=s.engine)['status'] == 'verified'
    markers = [Path(config['protected_file']).read_bytes() for config, _ in runs]
    s.deliver(flipped(root, sealed(root)), BID)
    with pytest.raises(RuntimeError, match='restore_mismatch'):
        s.verify()
    assert [Path(config['protected_file']).read_bytes() for config, _ in runs] == markers
    for config, cloud in runs:
        receive(r2.load_config(config['receiver_config_file']), cloud, bid=THIRD)
        assert s.v.run(config, client=cloud, engine=s.engine)['backup_id'] == THIRD


# spec: ANYM-781
def test_restore_mismatch_is_a_safe_code_for_the_host():
    v = module()
    assert v.failure_code(RuntimeError('restore_mismatch')) == 'restore_mismatch'
    assert 'restore_mismatch' in v.CALLS_FAILURES


# --- the follow self-test and the shared pins ---------------------------------------------------------------------------

# spec: ANYM-781
def test_the_follow_fixture_makes_a_bundle_the_calls_helper_accepts(self_test_setup, helper, monkeypatch):  # noqa: F811  (the imported fixture)
    v, f, directory, config = self_test_setup
    config = {**config, 'profile': 'calls'}
    monkeypatch.setattr(v, 'private_file', lambda path, limit=65536: Path(path).read_bytes())

    class Engine(SelfTestEngine):
        def command(self, *args):
            if args[:2] == ('container', 'create') and '/fixture' in ' '.join(args) and args[-1] == f.CALLS_FIXTURE_SCRIPT:
                self.resources[('helper', args[3])] = {}
                fixture = next(a for a in args if a.startswith('type=bind,src=')).split('src=')[1].split(',')[0]
                script = args[-1].replace("Path('/fixture')", f'Path({fixture!r})')
                script = script.replace("recipient=os.environ['DEEDKIT_SYNTHETIC_RECIPIENT']", "recipient='synthetic'")
                script = script.replace("root/'ciphertext',recipient)", f"root/'ciphertext',recipient,age={helper.age!r})")
                exec(compile(script, 'fixture', 'exec'), {})
                return b''
            return super().command(*args)

        def restore(self, tested, receiver, client, receipt, run, payload, secret_dir):
            assert tested['identity_file'] != self.config['identity_file']
            self.restored.append(run)
            for kind, name in self.v.names(run, 'calls').items():
                self.resources[(kind, name)] = {}
            helper.work = payload
            worker.run(json.loads((secret_dir / 'worker.json').read_text()), client.get_object()['Body'])
            return json.loads((payload / 'result.json').read_text())
    engine = Engine(v, f, config)
    assert f.self_test(engine, config) == ([2, 50], 1)
    assert len(engine.restored) == 1 and list(directory.iterdir()) == []


# spec: ANYM-781
def test_the_pin_step_moves_every_config_with_calls_and_a_calls_only_failure_holds_them_all(pins, calls_instance):  # noqa: F811
    s = pins
    s.paths = [*s.paths, s.calls_path]
    s.write_json(s.v.INSTANCES_FILE, {'v': 1, 'verifier_configs': [str(p) for p in s.paths]})
    before = [p.read_bytes() for p in s.paths]
    s.failures['calls'] = RuntimeError('anything the old helper said: /private/path')
    with pytest.raises(RuntimeError, match='calls_helper_failed') as held:
        s.update()
    assert s.f.failure_code(held.value) == 'calls_helper_failed'
    assert [p.read_bytes() for p in s.paths] == before and not (s.host / 'pins-switch.json').exists()
    s.failures.clear()
    assert s.update()['status'] == 'pinned'
    assert s.pairs() == [(NEW_IMAGE, HEAD)] * 3
    assert sorted(profile for profile, _ in s.tested if profile == 'calls') == ['calls', 'calls']


# spec: ANYM-781
def test_follow_holds_only_the_shared_pin_move_for_a_calls_failure(follower, calls_instance):  # noqa: F811
    s, t = follower, calls_instance
    messaging = Path(t.messaging['receiver_config_file']).parent / 'verifier.json'
    t.write_json(s.v.INSTANCES_FILE, {'v': 1, 'verifier_configs': [str(messaging), str(t.calls_path)]})
    before = [p.read_bytes() for p in (messaging, t.calls_path)]
    s.calendar_failure = RuntimeError('old helper cannot read a calls bundle')
    with pytest.raises(RuntimeError, match='calls_helper_failed'):
        s.run()
    assert [p.read_bytes() for p in (messaging, t.calls_path)] == before
    assert follow_status(s)['error'] == 'calls_helper_failed'
    s.calendar_failure = None
    assert s.run()['status'] == 'switched'
    assert 'calls' in s.profiles
    assert s.v.load_config(t.calls_path)['helper_revision'] == HEAD


# --- criterion 6: the repository's server-side pieces ----------------------------------------------------------------------

def load_example(name, mapping, destination):
    text = (ROOT / 'deploy' / name).read_text()
    for real, local_path in mapping.items():
        text = text.replace(real, str(local_path))
    destination.write_text(text)
    destination.chmod(0o600)


# spec: ANYM-781
def test_the_calls_examples_pass_the_real_validators(operator, tmp_path):  # noqa: F811
    v, _, _, _, _, write_json = operator
    etc, var, srv, shared = tmp_path / 'etc-calls', tmp_path / 'var-calls', tmp_path / 'srv-calls', tmp_path / 'postgres-data'
    for path in (etc, etc / 'control', etc / 'restore', var, var / 'verification', srv, srv / 'state', srv / 'state' / 'receive', shared):
        path.mkdir(mode=0o700)
    mapping = {'/etc/deedkit-calls-backup-r2': etc, '/var/lib/deedkit-calls-backup-r2': var, '/srv/backups/deedkit-calls': srv,
               '/var/lib/deedkit-backup-verify-postgres': shared}
    for name in ('receive-credentials.json', 'operator-credentials.json'):
        write_json(etc / name, {'access_key_id': 'a' * 32, 'secret_access_key': 'b' * 64})
    (etc / 'restore' / 'identity.agekey').write_text('SYNTHETIC_NOT_A_KEY')
    (etc / 'restore' / 'identity.agekey').chmod(0o600)
    load_example('calls-backup-r2-receive.example.json', mapping, etc / 'receive.json')
    load_example('calls-backup-r2-operator.example.json', mapping, etc / 'operator.json')
    load_example('calls-backup-r2-verifier.example.json', mapping, etc / 'verifier.json')
    receive_config = r2.load_config(etc / 'receive.json')
    operator_config = r2.load_config(etc / 'operator.json')
    assert r2.profile(receive_config) == r2.profile(operator_config) == 'calls'
    assert r2.destination(receive_config) == r2.destination(operator_config)
    assert receive_config['credentials_file'] != operator_config['credentials_file']
    verifier = v.load_config(etc / 'verifier.json')
    assert verifier['profile'] == 'calls' and verifier['receiver_config_file'] == str(etc / 'operator.json')
    messaging = json.loads((ROOT / 'deploy' / 'backup-r2.example.json').read_text())
    assert receive_config['prefix'] != messaging['prefix'] and receive_config['state_directory'] != messaging['state_directory']


# spec: ANYM-781
def test_the_instances_example_names_the_calls_verifier_and_loads(monkeypatch):
    from messaging_service import backup_r2_follow as f
    v = module()
    example = ROOT / 'deploy' / 'backup-r2-instances.example.json'
    monkeypatch.setattr(v, 'private_file', lambda path, limit: Path(path).read_bytes())
    listed = f.read_instances(example)
    assert Path('/etc/deedkit-calls-backup-r2/verifier.json') in listed and len(set(listed)) == len(listed) == 4


def unit(name):
    return (ROOT / 'deploy' / name).read_text().splitlines()


# spec: ANYM-781
def test_the_calls_verifier_units_are_the_recordings_pair_with_their_own_config():
    v = module()
    service = unit('deedkit-calls-backup-r2-verifier.service')
    other = unit('deedkit-recordings-backup-r2-verifier.service')
    assert 'ExecStart=/opt/deedkit-backup-r2/venv/bin/python -m messaging_service.backup_r2_verifier --config /etc/deedkit-calls-backup-r2/verifier.json' in service
    def rest(lines):
        return [line for line in lines if not line.startswith(('ExecStart=', 'Description='))]
    assert rest(service) == rest(other)
    limit = [line.split('=', 1)[1] for line in service if line.startswith('TimeoutStartSec=')]
    assert limit == ['26400s'] and 26400 >= 21600 + v.VERIFY_WAIT_SECONDS + v.BUSY_WAIT_MAX + 180
    timer = unit('deedkit-calls-backup-r2-verifier.timer')
    assert 'Unit=deedkit-calls-backup-r2-verifier.service' in timer
    def rest_timer(lines):
        return [line for line in lines if not line.startswith(('Unit=', 'Description='))]
    assert rest_timer(timer) == rest_timer(unit('deedkit-recordings-backup-r2-verifier.timer'))


# spec: ANYM-781
def test_the_documents_name_the_calls_profile_its_contract_and_its_restore_check():
    text = (ROOT / 'BACKUP_R2.md').read_text()
    section = text[text.index('## Calls verifier instance'):text.index('## Two verifiers on one receiver')]
    for word in ('`profile: calls`', '`restore_mismatch`', 'integrity_check', '`captured_at`', '`user_version`', '--discard-refused',
                 'calls-backup-r2-verifier.example.json', 'deedkit-calls-backup-r2-verifier.service', 'calls_helper_failed'):
        assert word in section, word
    backup = (ROOT / 'BACKUP.md').read_text()
    assert '`restore_mismatch`' in backup[backup.index('## Calls state (ANYM-778)'):]
