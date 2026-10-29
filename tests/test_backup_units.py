"""The calendar backup's systemd units (ANYM-607): its own config, messaging's lock, a daily run that ends by 18:00."""
import configparser
import json
from pathlib import Path
import re
import shlex
import subprocess

from messaging_service import backup_runner

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / 'deploy'
RUNNER = '/opt/personal-os-probe/ops/backup_runner.py'
MESSAGING_DIRECTORY = Path('/opt/personal-os-probe/backups')
MESSAGING_RUN = 18 * 3600  # personal-os-backup.timer, 18:00 UTC (docs/operations/production.md, Timer)


def unit(name):
    parser = configparser.ConfigParser(interpolation=None, strict=True, delimiters=('=',))
    parser.optionxform = lambda optionstr: optionstr  # keep systemd key case
    parser.read_string((DEPLOY / name).read_text())
    return parser


def seconds(value):
    match = re.fullmatch(r'(\d+)(s|min|h)?', value)
    return int(match[1]) * {None: 1, 's': 1, 'min': 60, 'h': 3600}[match[2]]


def example(tmp_path):
    data = json.loads((DEPLOY / 'backup-runner-calendar.example.json').read_text())
    path = tmp_path / 'runner.json'
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return backup_runner.config(path)


def service_config():
    service = unit('deedkit-calendar-backup.service')['Service']
    start, stop = shlex.split(service['ExecStart']), shlex.split(service['ExecStopPost'])
    assert start[:3] == ['/usr/bin/python3', '-I', RUNNER] and start[3] == '--config' and len(start) == 5
    assert stop == start + ['--recover-stop']
    return Path(start[4])


# spec: ANYM-607
def test_the_calendar_service_runs_the_host_runner_with_its_own_config_and_recovers_with_the_same_one(tmp_path):
    config, cfg = service_config(), example(tmp_path)
    assert cfg['profile'] == 'calendar'
    # Its own config and job directory, side by side; nothing of messaging's is its own.
    assert config.parent == Path(cfg['directory']).parent
    assert MESSAGING_DIRECTORY not in (config, *config.parents)
    directory = Path(cfg['directory'])
    assert directory != MESSAGING_DIRECTORY and MESSAGING_DIRECTORY not in directory.parents
    # The shared lock is messaging's runner lock, the one autodeploy holds.
    autodeploy = (DEPLOY / 'deedkit-autodeploy.service').read_text()
    assert re.search(r'^Environment=DEEDKIT_BACKUP_LOCK=(.+)$', autodeploy, re.M)[1] == cfg['host_lock']
    assert Path(cfg['host_lock']) == MESSAGING_DIRECTORY / '.runner.lock'


# spec: ANYM-607
def test_the_calendar_service_may_write_only_its_directory_and_the_shared_lock_directory(tmp_path):
    service, cfg = unit('deedkit-calendar-backup.service')['Service'], example(tmp_path)
    assert service['ProtectSystem'] == 'strict' and service['NoNewPrivileges'] == 'yes'
    assert set(service['ReadWritePaths'].split()) == {cfg['directory'], str(Path(cfg['host_lock']).parent)}
    assert service['User'] == 'root' and service['UMask'] == '0077' and service['Type'] == 'oneshot'


# spec: ANYM-607
def test_the_calendar_timer_fires_daily_at_16_utc_persistent_inside_autodeploys_no_start_hours():
    timer = unit('deedkit-calendar-backup.timer')
    assert timer['Timer']['OnCalendar'] == '*-*-* 16:00:00 UTC' and timer['Timer']['Persistent'] == 'true'
    assert timer['Timer']['Unit'] == 'deedkit-calendar-backup.service'
    assert timer['Install']['WantedBy'] == 'timers.target'
    autodeploy = (DEPLOY / 'deedkit-autodeploy.service').read_text()
    blocked = re.search(r'case "\$\$h" in ([0-9|]+)\) exit 1', autodeploy)[1].split('|')
    assert '16' in blocked and '17' in blocked


# spec: ANYM-607
def test_a_calendar_run_and_its_recovery_end_before_messagings_run_and_retry_none():
    timer, service = unit('deedkit-calendar-backup.timer')['Timer'], unit('deedkit-calendar-backup.service')
    start = 16 * 3600 + seconds(timer['RandomizedDelaySec'])
    # The runner's own lock wait ends first; systemd's start bound then ends the run before 18:00.
    assert start + backup_runner.HOST_LOCK_WAIT_SECONDS < MESSAGING_RUN
    budget = seconds(service['Service']['TimeoutStartSec'])
    assert budget > backup_runner.HOST_LOCK_WAIT_SECONDS
    assert start + budget + seconds(service['Service']['TimeoutStopSec']) < MESSAGING_RUN
    assert service['Service'].get('Restart', 'no') == 'no'


def admission():
    """The calendar service's ExecCondition as /bin/sh runs it, and the last UTC second it admits a start."""
    condition = unit('deedkit-calendar-backup.service')['Service']['ExecCondition']
    script = shlex.split(condition)[2].replace('$$', '$').replace('%%', '%')
    bound = re.search(r'\[ "\$m" -le (\d{4}) \]', script)[1]
    return script, int(bound[:2]) * 3600 + int(bound[2:]) * 60 + 59


# spec: ANYM-607
def test_a_late_start_is_not_admitted_so_no_calendar_run_reaches_messagings_run():
    service, timer = unit('deedkit-calendar-backup.service')['Service'], unit('deedkit-calendar-backup.timer')['Timer']
    _, last = admission()
    # The timer's own start (16:00 plus its random delay) is admitted; a Persistent= catch-up or a manual start
    # after the bound is skipped, and every admitted start, its whole budget and its recovery end before 18:00.
    assert 16 * 3600 + seconds(timer['RandomizedDelaySec']) <= last
    assert last + seconds(service['TimeoutStartSec']) + seconds(service['TimeoutStopSec']) < MESSAGING_RUN


# spec: ANYM-607
def test_the_admission_condition_admits_only_minutes_up_to_its_bound_and_fails_closed(tmp_path):
    script, _ = admission()
    fake = tmp_path / 'date'
    def admitted(clock):
        fake.write_text('#!/bin/sh\n' + ('echo ' + clock if clock else 'exit 1') + '\n')
        fake.chmod(0o700)
        return subprocess.run(['/bin/sh', '-c', script], env={'PATH': f'{tmp_path}:/usr/bin:/bin'}).returncode == 0
    assert all(admitted(clock) for clock in ('0000', '0930', '1600', '1605'))
    assert not any(admitted(clock) for clock in ('1606', '1700', '1759', '1800', '2359', 'x', ''))
