"""The calls backup's proof reader (ANYM-782): stdlib only, read-only, the age from the proof's capture, a fake clock."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from hypothesis import given, strategies as st
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy' / 'calls-backup-freshness.py'
NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
RUN, NEWER = 'a' * 32, 'b' * 32


def load():
    spec = importlib.util.spec_from_file_location('calls_backup_freshness', SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(at):
    return at.strftime('%Y-%m-%dT%H:%M:%SZ')


def write_proof(directory, run, captured, profile='calls'):
    (directory / f'proof-{run}.json').write_text(json.dumps({'backup': {'bytes': 4096}, 'verification': {
        'id': run, 'profile': profile, 'captured_at': captured, 'tables': {'calls': {'rows': 1, 'sha256': 'c' * 64}}}}))


def verifier(root, *, captured: datetime | str = NOW - timedelta(hours=3), verified=None, profile='calls'):
    directory = Path(root) / 'verification'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'last-success.json').write_text(json.dumps({
        'v': 1, 'status': 'verified', 'verification_id': RUN, 'verified_at': stamp(verified or NOW - timedelta(hours=1))}))
    write_proof(directory, RUN, stamp(captured) if isinstance(captured, datetime) else captured, profile)
    return directory


def check(directory, *args, now=NOW):
    lines = []
    code = load().run(['--verifier-directory', str(directory), *args], now=now, out=lines.append)
    assert len(lines) == 1
    return code, json.loads(lines[0])


# spec: ANYM-782
def test_a_recent_proof_reads_fresh_with_the_age_of_its_capture(tmp_path):
    code, answer = check(verifier(tmp_path))
    assert code == 0 and answer['status'] == 'fresh' and answer['age_hours'] == 3.0
    assert answer['captured_at'] == stamp(NOW - timedelta(hours=3)) and answer['max_age_hours'] == 36


# spec: ANYM-782
def test_beyond_36_hours_is_stale_and_exits_non_zero_without_an_argument(tmp_path):
    assert check(verifier(tmp_path, captured=NOW - timedelta(hours=36)))[0] == 0
    code, answer = check(verifier(tmp_path, captured=NOW - timedelta(hours=36, seconds=1)))
    assert code == 4 and answer['status'] == 'stale'


# spec: ANYM-782
def test_no_success_yet_is_stale_and_keeps_the_last_error(tmp_path):
    directory = tmp_path / 'verification'
    directory.mkdir()
    (directory / 'last-failure.json').write_text(json.dumps({'error': 'restore_mismatch', 'failed_at': stamp(NOW)}))
    code, answer = check(directory)
    assert code == 4 and answer['status'] == 'stale' and answer['last_error'] == 'restore_mismatch'


# spec: ANYM-782
def test_a_delivered_but_rejected_candidate_leaves_the_old_proof_current_and_stale(tmp_path):
    directory = verifier(tmp_path, captured=NOW - timedelta(hours=40), verified=NOW - timedelta(hours=39))
    # A newer proof whose promotion failed, and a failure of the newer candidate: neither makes the point fresh.
    write_proof(directory, NEWER, stamp(NOW - timedelta(hours=1)))
    (directory / 'last-failure.json').write_text(json.dumps({'error': 'restore_mismatch', 'failed_at': stamp(NOW)}))
    code, answer = check(directory)
    assert code == 4 and answer['status'] == 'stale' and answer['age_hours'] == 40.0
    assert answer['last_error'] == 'restore_mismatch'


# spec: ANYM-782
def test_a_retry_of_an_old_snapshot_counts_from_its_capture_not_its_verification(tmp_path):
    code, answer = check(verifier(tmp_path, captured=NOW - timedelta(days=5), verified=NOW - timedelta(minutes=1)))
    assert code == 4 and answer['status'] == 'stale' and answer['age_hours'] == 120.0


def no_directory(root):
    return Path(root) / 'absent'


def broken_success(root):
    directory = verifier(root)
    (directory / 'last-success.json').write_text('{')
    return directory


def missing_proof(root):
    directory = verifier(root)
    (directory / f'proof-{RUN}.json').unlink()
    return directory


def bad_id(root):
    directory = verifier(root)
    (directory / 'last-success.json').write_text(json.dumps({'status': 'verified', 'verification_id': '../x'}))
    return directory


def proof_of_another_run(root):
    directory = verifier(root)
    write_proof(directory, NEWER, stamp(NOW))
    (directory / f'proof-{RUN}.json').write_text((directory / f'proof-{NEWER}.json').read_text())
    return directory


# spec: ANYM-782
@pytest.mark.parametrize('case', [
    no_directory, broken_success, missing_proof, bad_id, proof_of_another_run,
    lambda root: verifier(root, profile='recordings'),
    lambda root: verifier(root, captured='yesterday'),
    lambda root: verifier(root, captured='2026-11-10T09:00:00'),
    lambda root: verifier(root, captured=NOW + timedelta(minutes=6))])
def test_what_cannot_be_read_as_the_calls_proof_is_unknown(tmp_path, case):
    code, answer = check(case(tmp_path))
    assert code == 5 and answer['status'] == 'unknown'


# spec: ANYM-782
@given(st.integers(min_value=-300, max_value=30 * 24 * 3600), st.integers(min_value=1, max_value=168))
def test_the_answer_follows_only_the_capture_age_and_the_limit(age, limit):
    with tempfile.TemporaryDirectory() as root:
        code, answer = load().evaluate(verifier(root, captured=NOW - timedelta(seconds=age)), limit, NOW)
    assert (code, answer['status']) == ((4, 'stale') if age > limit * 3600 else (0, 'fresh'))


# spec: ANYM-782
def test_the_script_runs_with_no_site_packages_prints_no_row_data_and_changes_nothing(tmp_path):
    directory = verifier(tmp_path, captured=datetime.now(timezone.utc) - timedelta(hours=1))
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
    done = subprocess.run([sys.executable, '-I', str(SCRIPT), '--verifier-directory', str(directory), '--max-age-hours', '36'],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0 and done.stdout.count('\n') == 1
    assert set(json.loads(done.stdout)) <= {'status', 'captured_at', 'age_hours', 'max_age_hours', 'reason', 'last_error', 'last_error_at'}
    assert {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()} == before


# spec: ANYM-782
@pytest.mark.parametrize('limit', ['0', '169', 'x', '٣'])
def test_a_refused_limit_exits_2(tmp_path, limit):
    with pytest.raises(SystemExit) as refused:
        load().run(['--verifier-directory', str(tmp_path), '--max-age-hours', limit], now=NOW)
    assert refused.value.code == 2


# spec: ANYM-782
def test_the_docs_name_the_reader_the_delivery_state_and_the_sanitized_replacement():
    backup = (ROOT / 'BACKUP.md').read_text()
    section = backup.split('### Calls state: freshness', 1)[1]
    assert 'calls-backup-freshness.py --verifier-directory /var/lib/deedkit-calls-backup-r2/verification' in section
    assert 'ssh root@' in section and 'not a proof' in section
    policy = (ROOT / 'docs' / 'DATA-POLICY.md').read_text()
    (line,) = [part for part in policy.split('\n  - ') if part.startswith('The calls state backup')]
    for word in ('briefs', 'decision journals', 'media-handshake tokens', '`keep_local`', 'encrypted', 'sanitized replacement',
                 'not before', 'no call-deletion feature'):
        assert word in ' '.join(line.split()), word
