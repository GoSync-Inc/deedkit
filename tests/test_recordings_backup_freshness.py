"""The recordings archive backup's freshness check (ANYM-728): stdlib only, read-only, a fake clock for every age."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy' / 'recordings-backup-freshness.py'
NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
ARCHIVE = {'archive_id': 'arch-0123456789abcdef', 'incarnation': 1, 'sequence': 40, 'records': 12}


def load():
    spec = importlib.util.spec_from_file_location('recordings_backup_freshness', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(at):
    return at.strftime('%Y-%m-%dT%H:%M:%SZ')


RUN = 'a' * 32


def verifier(tmp_path, *, verified=NOW - timedelta(hours=3), captured=None, status=None, archive=ARCHIVE, proof=True, failure=None):
    directory = tmp_path / 'verifier'
    directory.mkdir(parents=True, exist_ok=True)
    if verified is not None:
        (directory / 'last-success.json').write_text(json.dumps({'v': 1, 'status': 'verified', 'verification_id': RUN,
                                                                 'verified_at': stamp(verified)}))
    if status is not None:
        (directory / 'status.json').write_text(json.dumps(status))
    if failure is not None:
        (directory / 'last-failure.json').write_text(json.dumps(failure))
    if proof:
        taken = captured or (verified or NOW) - timedelta(hours=2)
        (directory / f'proof-{RUN}.json').write_text(json.dumps({'backup': {'bytes': 4096}, 'verification': {
            'archive': archive, 'captured_at': stamp(taken)}}))
    return directory


def check(tmp_path, *args, now=NOW, **kwargs):
    lines = []
    code = load().run(['--verifier-directory', str(verifier(tmp_path, **kwargs)), '--max-age-hours', '30', *args],
                      now=now, out=lines.append)
    return code, json.loads(lines[0])


# spec: ANYM-728
def test_a_recent_verified_success_is_fresh_and_shows_bytes_identity_and_the_snapshot_age(tmp_path):
    code, line = check(tmp_path)
    assert code == 0 and line['status'] == 'fresh' and line['verified_age_hours'] == 3.0 and line['age_hours'] == 5.0
    assert line['bytes'] == 4096 and line['archive'] == ARCHIVE and 'last_error' not in line


# spec: ANYM-728
def test_a_delayed_old_snapshot_is_stale_even_right_after_its_verification(tmp_path):
    code, line = check(tmp_path, verified=NOW - timedelta(minutes=5), captured=NOW - timedelta(hours=40))
    assert code == 4 and line['status'] == 'stale' and line['age_hours'] == 40.0 and line['verified_age_hours'] == 0.1


# spec: ANYM-728
def test_the_evidence_is_the_proof_of_the_last_success_not_a_newer_proof_of_a_failed_promotion(tmp_path):
    directory = verifier(tmp_path)
    newer = {**ARCHIVE, 'sequence': 999}
    (directory / ('proof-' + 'b' * 32 + '.json')).write_text(json.dumps({'backup': {'bytes': 1}, 'verification': {
        'archive': newer, 'captured_at': stamp(NOW)}}))
    lines = []
    assert load().run(['--verifier-directory', str(directory), '--max-age-hours', '30'], now=NOW, out=lines.append) == 0
    assert json.loads(lines[0])['archive'] == ARCHIVE and json.loads(lines[0])['bytes'] == 4096


# spec: ANYM-728
def test_the_last_error_code_survives_an_idle_tick_and_a_missing_success_is_stale(tmp_path):
    failure = {'v': 1, 'status': 'failed', 'error': 'archive_content_lost', 'failed_at': stamp(NOW - timedelta(hours=1))}
    idle = {'v': 1, 'status': 'idle', 'checked_at': stamp(NOW)}
    code, line = check(tmp_path, status=idle, failure=failure)
    assert code == 0 and line['last_error'] == 'archive_content_lost' and line['last_error_at'] == failure['failed_at']
    code, line = check(tmp_path / 'none', verified=None, proof=False, failure=failure)
    assert code == 4 and line['reason'] == 'no verified success yet' and line['last_error'] == 'archive_content_lost'


# spec: ANYM-728
def test_unreadable_input_is_unknown_not_fresh(tmp_path):
    assert load().run(['--verifier-directory', str(tmp_path / 'missing'), '--max-age-hours', '30'], now=NOW, out=lambda _: None) == 5
    directory = verifier(tmp_path)
    (directory / 'last-success.json').write_text('{broken')
    assert load().run(['--verifier-directory', str(directory), '--max-age-hours', '30'], now=NOW, out=lambda _: None) == 5
    assert check(tmp_path / 'future', verified=NOW + timedelta(hours=2))[0] == 5
    assert check(tmp_path / 'noproof', proof=False)[0] == 5


# spec: ANYM-728
def test_an_archive_whose_counters_do_not_move_for_seven_days_gets_the_hint(tmp_path):
    state = tmp_path / 'state.json'
    first = check(tmp_path, '--state', str(state))[1]
    assert first['unchanged_days'] == 0 and 'hint' not in first
    later = NOW + timedelta(days=7, minutes=1)
    line = check(tmp_path, '--state', str(state), now=later, verified=later - timedelta(hours=1))[1]
    assert line['unchanged_days'] == 7 and line['hint'] == 'unchanged_for_7_days'
    moved = {**ARCHIVE, 'sequence': 41}
    line = check(tmp_path, '--state', str(state), now=later, verified=later - timedelta(hours=1), archive=moved)[1]
    assert line['unchanged_days'] == 0 and 'hint' not in line
    assert 'text' not in json.dumps(json.loads(state.read_text()))
