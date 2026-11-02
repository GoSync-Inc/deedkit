"""The calendar backup's freshness check (ANYM-694): stdlib only, read-only, a fake clock for every age."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy' / 'calendar-backup-freshness.py'
NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)


def load():
    spec = importlib.util.spec_from_file_location('calendar_backup_freshness', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(at):
    return at.strftime('%Y-%m-%dT%H:%M:%SZ')


def check(tmp_path, content, *, limit=26, now=NOW, raw=False):
    """Run the check against a status file holding ``content`` (None: no file); returns (exit code, printed line)."""
    path = tmp_path / 'status.json'
    if content is not None:
        path.write_text(content if raw else json.dumps(content))
    lines = []
    code = load().run(['--status', str(path), '--max-age-hours', str(limit)], now=now, out=lines.append)
    assert len(lines) == 1
    return code, json.loads(lines[0])


# spec: ANYM-694
def test_a_snapshot_inside_the_limit_is_fresh_and_one_past_it_is_stale(tmp_path):
    fresh = {'state': 'ok', 'last_snapshot_at': stamp(NOW - timedelta(hours=25, minutes=59))}
    code, line = check(tmp_path, fresh)
    assert (code, line['status'], line['age_hours']) == (0, 'fresh', 26.0)
    code, line = check(tmp_path, {'last_snapshot_at': stamp(NOW - timedelta(hours=26, minutes=1))})
    assert (code, line['status'], line['age_hours']) == (4, 'stale', 26.0)
    # The comparison is in seconds, the printed age has one decimal: 25h59m rounds up to the limit and stays fresh.
    code, line = check(tmp_path, {'last_snapshot_at': stamp(NOW - timedelta(hours=12, minutes=18))})
    assert (code, line['status'], line['age_hours']) == (0, 'fresh', 12.3)


# spec: ANYM-694
def test_a_status_file_without_a_snapshot_time_is_stale(tmp_path):
    for content in ({'state': 'ok'}, {'state': 'failed', 'last_snapshot_at': None}):
        code, line = check(tmp_path, content)
        assert (code, line['status']) == (4, 'stale')


# spec: ANYM-694
def test_no_file_or_an_unreadable_one_is_unknown(tmp_path):
    code, line = check(tmp_path, None)
    assert (code, line['status']) == (5, 'unknown')
    for text in ('{not json', '', '[1, 2]', '"text"', '\x00\xff'):
        code, line = check(tmp_path, text, raw=True)
        assert (code, line['status']) == (5, 'unknown'), text
    directory = tmp_path / 'directory.json'
    directory.mkdir()
    lines = []
    assert load().run(['--status', str(directory), '--max-age-hours', '26'], now=NOW, out=lines.append) == 5


# spec: ANYM-694
def test_a_time_that_cannot_be_parsed_is_unknown(tmp_path):
    for value in ('yesterday', '2026-11-01', '2026-11-01T16:00:27', 12345, ['2026-11-01T16:00:27Z']):
        code, line = check(tmp_path, {'last_snapshot_at': value})
        assert (code, line['status']) == (5, 'unknown'), value


# spec: ANYM-694
def test_a_snapshot_a_few_minutes_ahead_counts_as_age_zero_and_further_ahead_is_unknown(tmp_path):
    code, line = check(tmp_path, {'last_snapshot_at': stamp(NOW + timedelta(minutes=4))})
    assert (code, line['status'], line['age_hours']) == (0, 'fresh', 0.0)
    code, line = check(tmp_path, {'last_snapshot_at': stamp(NOW + timedelta(minutes=6))})
    assert (code, line['status']) == (5, 'unknown')


# spec: ANYM-694
def test_the_age_limit_is_accepted_from_one_to_168_hours_only(tmp_path):
    module = load()
    path = tmp_path / 'status.json'
    path.write_text(json.dumps({'last_snapshot_at': stamp(NOW)}))
    for limit in ('1', '168', '29'):
        assert module.run(['--status', str(path), '--max-age-hours', limit], now=NOW, out=lambda line: None) == 0
    for limit in ('0', '169', '-1', '2.5', 'x', ''):
        with pytest.raises(SystemExit) as stopped:
            module.run(['--status', str(path), '--max-age-hours', limit], now=NOW, out=lambda line: None)
        assert stopped.value.code == 2, limit


# spec: ANYM-694
def test_the_script_is_a_standalone_command_that_prints_one_json_line_and_changes_nothing(tmp_path):
    path = tmp_path / 'status.json'
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    path.write_text(json.dumps({'last_snapshot_at': stamp(recent)}))
    before = sorted(tmp_path.iterdir())
    done = subprocess.run([sys.executable, '-I', str(SCRIPT), '--status', str(path), '--max-age-hours', '29'],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0 and done.stderr == ''
    assert json.loads(done.stdout)['status'] == 'fresh' and done.stdout.count('\n') == 1
    assert sorted(tmp_path.iterdir()) == before
    missing = subprocess.run([sys.executable, '-I', str(SCRIPT), '--status', str(tmp_path / 'none.json'),
                              '--max-age-hours', '29'], capture_output=True, text=True, timeout=30)
    assert missing.returncode == 5 and json.loads(missing.stdout)['status'] == 'unknown'


# spec: ANYM-694
def test_the_script_imports_nothing_outside_the_standard_library_and_takes_no_lock_docker_or_network():
    source = SCRIPT.read_text()
    imports = {line.split()[1].split('.')[0] for line in source.splitlines() if line.startswith(('import ', 'from '))}
    assert imports <= set(sys.stdlib_module_names)
    for forbidden in ('fcntl', 'subprocess', 'socket', 'urllib', 'http', 'docker', 'messaging_service'):
        assert forbidden not in imports


# spec: ANYM-694
def test_the_result_file_carries_the_fields_the_status_line_reads_and_is_world_readable(tmp_path):
    job = tmp_path / 'job'
    job.mkdir()
    status, result = job / 'status.json', job / 'freshness.json'
    status.write_text(json.dumps({'last_snapshot_at': stamp(NOW - timedelta(hours=30))}))
    lines = []
    code = load().run(['--status', str(status), '--max-age-hours', '29', '--write', str(result)], now=NOW,
                      out=lines.append)
    assert code == 4
    written = json.loads(result.read_text())
    assert written == {'checked_at': stamp(NOW), 'last_snapshot_at': stamp(NOW - timedelta(hours=30)),
                       'max_age_hours': 29, 'status': 'stale', 'exit_code': 4, 'age_hours': 30.0}
    assert result.stat().st_mode & 0o777 == 0o644
    assert sorted(path.name for path in job.iterdir()) == ['freshness.json', 'status.json']
    # An unknown result keeps the same keys, with no age.
    status.write_text('{broken')
    assert load().run(['--status', str(status), '--max-age-hours', '29', '--write', str(result)], now=NOW,
                      out=lambda line: None) == 5
    written = json.loads(result.read_text())
    assert written['status'] == 'unknown' and written['exit_code'] == 5 and written['last_snapshot_at'] is None
    assert written['age_hours'] is None


# spec: ANYM-694
def test_a_failing_check_names_the_remedy_on_stderr_when_asked(tmp_path, capsys):
    status = tmp_path / 'status.json'
    status.write_text(json.dumps({'last_snapshot_at': stamp(NOW - timedelta(hours=40))}))
    load().run(['--status', str(status), '--max-age-hours', '29', '--remedy'], now=NOW, out=lambda line: None)
    error = capsys.readouterr().err
    assert 'catch-up runs at 19:15 UTC when the newest snapshot is over 22 h old' in error
    assert 'BACKUP.md, Daily run and freshness' in error
    status.write_text(json.dumps({'last_snapshot_at': stamp(NOW)}))
    load().run(['--status', str(status), '--max-age-hours', '29', '--remedy'], now=NOW, out=lambda line: None)
    assert capsys.readouterr().err == ''
