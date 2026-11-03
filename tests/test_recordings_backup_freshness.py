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


def verifier(tmp_path, *, verified=NOW - timedelta(hours=3), status=None, archive=ARCHIVE, proof=True):
    directory = tmp_path / 'verifier'
    directory.mkdir(parents=True, exist_ok=True)
    if verified is not None:
        (directory / 'last-success.json').write_text(json.dumps({'v': 1, 'status': 'verified', 'verified_at': stamp(verified)}))
    if status is not None:
        (directory / 'status.json').write_text(json.dumps(status))
    if proof:
        (directory / 'proof-a.json').write_text(json.dumps({'backup': {'bytes': 4096},
                                                            'verification': {'archive': archive}}))
    return directory


def check(tmp_path, *args, now=NOW, **kwargs):
    lines = []
    code = load().run(['--verifier-directory', str(verifier(tmp_path, **kwargs)), '--max-age-hours', '30', *args],
                      now=now, out=lines.append)
    return code, json.loads(lines[0])


# spec: ANYM-728
def test_a_recent_verified_success_is_fresh_and_shows_bytes_and_identity(tmp_path):
    code, line = check(tmp_path)
    assert code == 0 and line['status'] == 'fresh' and line['age_hours'] == 3.0
    assert line['bytes'] == 4096 and line['archive'] == ARCHIVE and 'last_error' not in line


# spec: ANYM-728
def test_an_old_or_missing_success_is_stale_and_the_last_error_code_is_shown(tmp_path):
    code, line = check(tmp_path, verified=NOW - timedelta(hours=31), status={'status': 'failed', 'error': 'archive_content_lost'})
    assert code == 4 and line['status'] == 'stale' and line['last_error'] == 'archive_content_lost'
    code, line = check(tmp_path / 'none', verified=None, proof=False)
    assert code == 4 and line['reason'] == 'no verified success yet'


# spec: ANYM-728
def test_unreadable_input_is_unknown_not_fresh(tmp_path):
    assert load().run(['--verifier-directory', str(tmp_path / 'missing'), '--max-age-hours', '30'], now=NOW, out=lambda _: None) == 5
    directory = verifier(tmp_path)
    (directory / 'last-success.json').write_text('{broken')
    assert load().run(['--verifier-directory', str(directory), '--max-age-hours', '30'], now=NOW, out=lambda _: None) == 5
    assert check(tmp_path / 'future', verified=NOW + timedelta(hours=2))[0] == 5


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
