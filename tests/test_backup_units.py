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


def stop_post():
    """ExecStopPost's /bin/sh script as the shell runs it."""
    shell = shlex.split(unit('deedkit-calendar-backup.service')['Service']['ExecStopPost'])
    assert shell[:2] == ['/bin/sh', '-c'] and len(shell) == 3
    return shell[2].replace('$$', '$')


def service_config():
    service = unit('deedkit-calendar-backup.service')['Service']
    start, stop = shlex.split(service['ExecStart']), shlex.split(stop_post().split('|| exec ', 1)[1])
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
def test_the_calendar_timer_fires_daily_at_16_utc_persistent_and_shares_the_runner_lock_with_autodeploy():
    # Owner 11-04: autodeploy has no clock window; the calendar run and a deploy exclude each other by the lock.
    timer = unit('deedkit-calendar-backup.timer')
    assert timer['Timer']['OnCalendar'] == '*-*-* 16:00:00 UTC' and timer['Timer']['Persistent'] == 'true'
    assert timer['Timer']['Unit'] == 'deedkit-calendar-backup.service'
    assert timer['Install']['WantedBy'] == 'timers.target'
    host_lock = json.loads((DEPLOY / 'backup-runner-calendar.example.json').read_text())['host_lock']
    autodeploy = (DEPLOY / 'deedkit-autodeploy.service').read_text()
    assert f'Environment=DEEDKIT_BACKUP_LOCK={host_lock}' in autodeploy
    assert '/usr/bin/flock --nonblock --conflict-exit-code 75 ${DEEDKIT_BACKUP_LOCK} ' in autodeploy
    assert 'date' not in re.search(r'^ExecCondition=(.*)$', autodeploy, re.M)[1]


# spec: ANYM-607
def test_a_calendar_run_and_its_recovery_end_before_messagings_run_and_retry_none():
    timer, service = unit('deedkit-calendar-backup.timer')['Timer'], unit('deedkit-calendar-backup.service')
    start = latest_timer_start(timer)
    # The runner's own lock wait ends first; systemd's start bound then ends the run before 18:00.
    assert start + backup_runner.HOST_LOCK_WAIT_SECONDS < MESSAGING_RUN
    budget = seconds(service['Service']['TimeoutStartSec'])
    assert budget > backup_runner.HOST_LOCK_WAIT_SECONDS
    assert start + whole_budget(service['Service']) < MESSAGING_RUN
    assert service['Service'].get('Restart', 'no') == 'no'


def latest_timer_start(timer):
    """16:00 plus the timer's random delay and its accuracy (systemd's default accuracy is 1 min)."""
    return 16 * 3600 + seconds(timer['RandomizedDelaySec']) + seconds(timer.get('AccuracySec', '1min'))


def whole_budget(service):
    """TimeoutStartSec, then the main process's stop and ExecStopPost, each with its own TimeoutStopSec."""
    return seconds(service['TimeoutStartSec']) + 2 * seconds(service['TimeoutStopSec'])


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
    # The timer's own start (16:00 plus its random delay and accuracy) is admitted; a Persistent= catch-up or a
    # manual start after the bound is skipped, and every admitted start, its whole budget and its recovery end
    # before 18:00.
    assert latest_timer_start(timer) <= last
    assert last + whole_budget(service) < MESSAGING_RUN


# spec: ANYM-607
def test_the_admission_condition_admits_only_minutes_up_to_its_bound_and_fails_closed(tmp_path):
    script, _ = admission()
    fake = tmp_path / 'date'
    def admitted(clock):
        fake.write_text('#!/bin/sh\n' + ('echo ' + clock if clock else 'exit 1') + '\n')
        fake.chmod(0o700)
        return subprocess.run(['/bin/sh', '-c', script], env={'PATH': f'{tmp_path}:/usr/bin:/bin'}).returncode == 0
    assert all(admitted(clock) for clock in ('0000', '0930', '1600', '1602'))
    assert not any(admitted(clock) for clock in ('1603', '1605', '1700', '1759', '1800', '2359', 'x', ''))


FRESHNESS_DIRECTORY = '/opt/deedkit/calendar-backup/backups'
FRESHNESS_SCRIPT = '/opt/deedkit/calendar-backup/freshness.py'


def unit_lines(name):
    """Every key of a unit file as a list of values per section (a key may repeat, as ExecCondition does)."""
    sections, current = {}, None
    for line in (DEPLOY / name).read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        if re.fullmatch(r'\[\w+\]', line):
            current = sections.setdefault(line[1:-1], {})
        else:
            key, value = line.split('=', 1)
            current.setdefault(key, []).append(value)
    return sections


def sh_script(command):
    """An Exec line of the form /bin/sh -c '<script>' as the shell runs it."""
    shell = shlex.split(command)
    assert shell[:2] == ['/bin/sh', '-c'] and len(shell) == 3
    return shell[2].replace('$$', '$').replace('%%', '%')


def run_condition(script, tmp_path, *, clock=None, freshness=None):
    """Run an ExecCondition script with a fake ``date`` and a fake freshness script (python3) on its path."""
    for name, body in (('date', 'echo ' + clock if clock else 'exit 1'), ('python3', f'exit {freshness}')):
        fake = tmp_path / name
        fake.write_text('#!/bin/sh\n' + body + '\n')
        fake.chmod(0o700)
    script = script.replace('/usr/bin/python3', str(tmp_path / 'python3'))
    return subprocess.run(['/bin/sh', '-c', script], env={'PATH': f'{tmp_path}:/usr/bin:/bin'}).returncode == 0


# spec: ANYM-694
def test_the_freshness_unit_is_read_only_takes_no_lock_and_writes_only_its_result_file():
    unit_file = unit_lines('deedkit-calendar-backup-fresh.service')
    service, text = unit_file['Service'], (DEPLOY / 'deedkit-calendar-backup-fresh.service').read_text()
    assert 'ExecCondition' not in service and 'docker' not in text and 'Requires' not in unit_file['Unit']
    assert service['Type'] == ['oneshot'] and seconds(service['TimeoutStartSec'][0]) <= 60
    assert service['ProtectSystem'] == ['strict'] and service['NoNewPrivileges'] == ['yes']
    assert service['ReadWritePaths'] == [FRESHNESS_DIRECTORY]
    start = shlex.split(service['ExecStart'][0])
    assert start[:3] == ['/usr/bin/python3', '-I', FRESHNESS_SCRIPT]
    assert start[start.index('--max-age-hours') + 1] == '29'
    assert start[start.index('--status') + 1] == FRESHNESS_DIRECTORY + '/status.json'
    assert start[start.index('--write') + 1] == FRESHNESS_DIRECTORY + '/freshness.json' and '--remedy' in start
    assert 'Restart' not in service and 'ExecStopPost' not in service


# spec: ANYM-694
def test_the_freshness_timer_fires_at_least_hourly_and_catches_up_after_a_reboot():
    timer = unit_lines('deedkit-calendar-backup-fresh.timer')['Timer']
    assert timer['Persistent'] == ['true'] and timer['Unit'] == ['deedkit-calendar-backup-fresh.service']
    assert re.fullmatch(r'\*-\*-\* \*:\d\d:00 UTC', timer['OnCalendar'][0])
    assert unit_lines('deedkit-calendar-backup-fresh.timer')['Install']['WantedBy'] == ['timers.target']


# spec: ANYM-694
def test_the_catchup_service_runs_the_same_runner_recovery_sandbox_and_budget_as_the_daily_service():
    daily, catchup = unit_lines('deedkit-calendar-backup.service'), unit_lines('deedkit-calendar-backup-catchup.service')
    for key in ('ExecStart', 'ExecStopPost', 'TimeoutStartSec', 'TimeoutStopSec', 'User', 'UMask', 'ProtectSystem',
                'ProtectHome', 'PrivateTmp', 'NoNewPrivileges', 'ReadWritePaths', 'MemoryMax', 'Type'):
        assert catchup['Service'][key] == daily['Service'][key], key
    assert catchup['Unit']['Requires'] == daily['Unit']['Requires']
    assert 'Restart' not in catchup['Service']
    assert len(catchup['Service']['ExecCondition']) == 2


# spec: ANYM-694
def test_the_catchup_timer_fires_at_19_15_utc_persistent_with_one_second_accuracy():
    timer = unit_lines('deedkit-calendar-backup-catchup.timer')['Timer']
    assert timer['OnCalendar'] == ['*-*-* 19:15:00 UTC'] and timer['Persistent'] == ['true']
    assert timer['AccuracySec'] == ['1s'] and timer['Unit'] == ['deedkit-calendar-backup-catchup.service']
    assert 'RandomizedDelaySec' not in timer


# spec: ANYM-694
def test_the_catchup_clock_condition_admits_19_00_to_23_59_only_and_fails_closed(tmp_path):
    script = sh_script(unit_lines('deedkit-calendar-backup-catchup.service')['Service']['ExecCondition'][0])
    assert 'date -u' in script
    for clock in ('1900', '1915', '2200', '2359'):
        assert run_condition(script, tmp_path, clock=clock, freshness=0), clock
    # The morning is the daily unit's Persistent boot run (admitted to 16:02): a catch-up there is a second snapshot.
    for clock in ('0000', '0100', '0859', '1300', '1400', '1600', '1602', '1800', '1859', 'x', ''):
        assert not run_condition(script, tmp_path, clock=clock, freshness=0), clock


# spec: ANYM-694
def test_the_catchup_freshness_condition_runs_only_for_a_stale_or_unknown_snapshot(tmp_path):
    script = sh_script(unit_lines('deedkit-calendar-backup-catchup.service')['Service']['ExecCondition'][1])
    start = shlex.split(script.split(';', 1)[0].replace('>/dev/null 2>&1', ''))
    assert start[:3] == ['/usr/bin/python3', '-I', FRESHNESS_SCRIPT]
    assert start[start.index('--max-age-hours') + 1] == '22'
    assert start[start.index('--status') + 1] == FRESHNESS_DIRECTORY + '/status.json'
    assert '--write' not in start
    # Fresh: a no-op, so the two timers never make two snapshots a day. Stale and unknown: admitted.
    assert not run_condition(script, tmp_path, freshness=0)
    assert run_condition(script, tmp_path, freshness=4) and run_condition(script, tmp_path, freshness=5)
    # A script that cannot run (argument error, missing interpreter) does not start a snapshot.
    assert not run_condition(script, tmp_path, freshness=2) and not run_condition(script, tmp_path, freshness=127)


# spec: ANYM-694
def test_the_catchups_whole_budget_ends_before_the_next_daily_run_and_its_timer_start_is_admitted():
    service = {'Service': {key: values[0] for key, values in
                           unit_lines('deedkit-calendar-backup-catchup.service')['Service'].items()}}
    conditions = unit_lines('deedkit-calendar-backup-catchup.service')['Service']['ExecCondition']
    opens = re.search(r'-ge (\d{4})', sh_script(conditions[0]))[1]
    assert not re.search(r'-le \d{4}', sh_script(conditions[0]))  # no morning branch
    day = 24 * 3600
    last = day - 1  # 23:59:59, the latest minute a four-digit clock can admit
    assert last + whole_budget(service['Service']) < day + 16 * 3600  # before the next 16:00 run
    timer = unit_lines('deedkit-calendar-backup-catchup.timer')['Timer']
    start = 19 * 3600 + 15 * 60 + seconds(timer['AccuracySec'][0])
    assert int(opens[:2]) * 3600 + int(opens[2:]) * 60 <= start <= last
    # The daily service's own window and tests are untouched: the catch-up never moves its 16:02 admission.
    assert admission()[1] == 16 * 3600 + 2 * 60 + 59


# spec: ANYM-607
def test_a_start_the_condition_skipped_recovers_nothing_and_every_other_end_recovers(tmp_path):
    script = stop_post()
    assert script.count('/usr/bin/python3') == 1
    fake, calls = tmp_path / 'python3', tmp_path / 'calls'
    fake.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    fake.chmod(0o700)
    local = script.replace('/usr/bin/python3', str(fake))
    for result in ('exec-condition', 'success', 'timeout', 'exit-code', 'signal', ''):
        subprocess.run(['/bin/sh', '-c', local], env={'PATH': '/usr/bin:/bin', 'SERVICE_RESULT': result}, check=True)
    lines = calls.read_text().splitlines()
    assert len(lines) == 5 and all(line.endswith('runner.json --recover-stop') for line in lines)
