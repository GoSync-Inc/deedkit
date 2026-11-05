"""The calendar backup's systemd units (ANYM-607): its own config, messaging's lock, a daily run that ends by 18:00.

The restore stack and procedure (ANYM-610): the Compose file's isolation, the written commands' shell syntax."""
import configparser
import copy
import json
from pathlib import Path
import re
import shlex
import subprocess
from types import SimpleNamespace

from messaging_service import backup_runner
import pytest
import yaml

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


RESTORE_SCRIPT = DEPLOY / 'calendar-restore.sh'


def restore_stack():
    return yaml.safe_load((DEPLOY / 'compose.calendar-restore.yaml').read_text())


def restore_section():
    """The text of BACKUP.md "Restore the family calendar", up to the next section."""
    match = re.search(r'^### Restore the family calendar.*?(?=^## )', (ROOT / 'BACKUP.md').read_text(), re.M | re.S)
    assert match
    return match[0]


# spec: ANYM-610
def test_the_restore_stack_is_the_database_and_the_api_only_with_no_worker():
    assert set(restore_stack()['services']) == {'calendar-db', 'calendar-api'}


# spec: ANYM-610
def test_the_restore_stack_has_one_internal_network_and_no_external_or_provider_network():
    stack = restore_stack()
    assert stack['networks'] == {'calendar-backend': {'internal': True}}
    for service in stack['services'].values():
        assert service['networks'] == ['calendar-backend'] and 'network_mode' not in service


# spec: ANYM-610
def test_the_restore_stack_mounts_no_vault_and_gives_the_api_only_its_own_profile():
    stack = restore_stack()
    assert stack['services']['calendar-api']['volumes'] == [
        '${CALENDAR_RESTORE_STATE_DIR}/service/gateway.json:/data/service/gateway.json:ro']
    assert stack['services']['calendar-db']['volumes'] == ['calendar-restore-database:/var/lib/postgresql']
    assert 'vault' not in json.dumps(stack['services'])


# spec: ANYM-610
def test_the_restore_stack_shares_no_name_with_production():
    stack, production = restore_stack(), yaml.safe_load((DEPLOY / 'compose.calendar.yaml').read_text())
    assert stack['name'] != production['name']
    assert not set(stack['volumes']) & set(production['volumes'])
    assert stack['volumes'] == {'calendar-restore-database': {}}  # project-scoped, never an external volume
    assert 'CALENDAR_RESTORE_STATE_DIR:?' in stack['services']['calendar-db']['env_file']
    text = (DEPLOY / 'compose.calendar-restore.yaml').read_text()
    assert '${CALENDAR_STATE_DIR' not in text and '${CALENDAR_DATABASE_VOLUME' not in text


# spec: ANYM-610
def test_the_restore_stack_publishes_no_port_and_starts_nothing_by_itself():
    # Docker 29 publishes nothing for a container whose only network is internal (probe on the backup server, 11-05).
    for service in restore_stack()['services'].values():
        assert 'ports' not in service and service['restart'] == 'no' and 'build' not in service


# spec: ANYM-610
def test_the_restore_api_runs_a_required_image_and_the_production_apis_sandbox_and_health_check():
    stack, production = restore_stack(), yaml.safe_load((DEPLOY / 'compose.calendar.yaml').read_text())
    api, live = stack['services']['calendar-api'], production['services']['calendar-api']
    assert api['image'].startswith('${CALENDAR_RESTORE_IMAGE:?')
    for key in ('command', 'healthcheck', 'read_only', 'user', 'cap_drop', 'security_opt', 'tmpfs', 'mem_limit', 'pids_limit'):
        assert api[key] == live[key], key
    assert stack['services']['calendar-db']['image'] == production['services']['calendar-db']['image']


# spec: ANYM-610
def test_the_restore_containers_keep_no_log_and_are_bounded():
    # PostgreSQL logs a failed statement, so a failed CREATE ROLE would leave its password in a retained log.
    for service in restore_stack()['services'].values():
        assert service['logging'] == {'driver': 'none'} and service['cpus'] == 1 and service['mem_limit']


# spec: ANYM-610
def test_the_restore_procedure_is_linked_and_its_commands_parse():
    target = 'BACKUP.md#restore-the-family-calendar'
    assert target in (ROOT / 'docs/operations/platform-cutover.md').read_text()
    assert target in (ROOT / 'services/calendar/docs/SERVICE.md').read_text()
    assert 'deploy/calendar-restore.sh' in restore_section() and '--days N' in restore_section()
    subprocess.run(['bash', '-n', str(RESTORE_SCRIPT)], check=True)
    script = RESTORE_SCRIPT.read_text()
    assert 'owner db-roles --check' in script and not re.search(r'db-roles(?! --check)', script)  # the check mints nothing
    assert '--exit-on-error' in script and 'set -euo pipefail' in script


FAKE_DOCKER = r'''#!/usr/bin/env python3
import fcntl, json, os, sys
from pathlib import Path
args = sys.argv[1:]
# Under a lock: both sides of `restore run ... | sql` start at once, and each call must get its own number.
with open(os.environ['FAKE_LOG'], 'a+') as file:
    fcntl.flock(file, fcntl.LOCK_EX)
    file.seek(0)
    calls = len(file.read().splitlines())
    file.write(json.dumps(args) + '\n')
if args[0] in ('run', 'compose') and not sys.stdin.isatty():
    sys.stdin.read()
if calls + 1 == int(os.environ.get('FAKE_FAIL_AT', '0')) or calls + 1 >= int(os.environ.get('FAKE_FAIL_FROM', '1000000')):
    sys.exit(7)
if os.environ.get('FAKE_RESTORE_FAILS') and 'pg_restore' in args:
    sys.stderr.write('pg_restore: error: COPY failed: private row\n')
    sys.exit(1)
if args[:1] == ['run'] and any(value.endswith(':/work') for value in args):
    work = Path(next(value for value in args if value.endswith(':/work')).split(':')[0])
    profile = work / 'decrypted/state/profile'
    profile.mkdir(parents=True)
    for name in ('gateway', 'worker', 'runtime'):
        (profile / (name + '.json')).write_text(json.dumps({'dsn': 'postgresql://user:synthetic@calendar-db:5432/db', 'port': 1}))
    (profile / 'postgres.env').write_text('POSTGRES_USER=synthetic\n')
    (work / 'decrypted/manifest.json').write_text(json.dumps({'tables': {}, 'schema_version': 8}))
    (work / 'decrypted/database.dump').write_text('synthetic')
if '-At' in args and '-c' in args:
    print('family\tmember\tcalendar')
'''
FAKE_INSTALL = r'''#!/bin/sh
if [ "$1" = -d ]; then shift 7; mkdir -p "$@"; else cp "$7" "$8"; fi
'''
FAKE_RECEIVER = r'''#!/bin/sh
while [ $# -gt 0 ]; do [ "$1" = --output ] && echo synthetic > "$2"; shift; done
'''


def restore_run(tmp_path, *, reserve=1, **env):
    """calendar-restore.sh on fake docker, flock, install, sha256sum and receiver, in a throwaway root."""
    fakes, root, config = tmp_path / 'fakes', tmp_path / 'root', tmp_path / 'config'
    for directory in (fakes, root, config / 'control', config / 'restore'):
        directory.mkdir(parents=True, exist_ok=True)
    for name, text in {'docker': FAKE_DOCKER, 'install': FAKE_INSTALL, 'receiver': FAKE_RECEIVER,
                       'flock': '#!/bin/sh\nexit ${FAKE_FLOCK_STATUS:-0}\n',
                       'sha256sum': '#!/bin/sh\nexit ${FAKE_SHA_STATUS:-0}\n'}.items():
        (fakes / name).write_text(text)
        (fakes / name).chmod(0o700)
    (config / 'control/protected.json').write_text(json.dumps({'backups': [{'id': 'b1', 'sha256': 'a' * 64, 'bytes': 1}]}))
    (config / 'verifier.json').write_text(json.dumps({'helper_image': 'sha256:' + 'b' * 64, 'postgres_headroom_bytes': 1,
                                                      'reserve_bytes': reserve}))
    log = tmp_path / 'docker.log'
    log.unlink(missing_ok=True)
    result = subprocess.run(['bash', str(RESTORE_SCRIPT)], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, env={
        'PATH': f'{fakes}:/usr/bin:/bin', 'DIGEST': 'sha256:' + 'c' * 64, 'FAKE_LOG': str(log),
        'CALENDAR_RESTORE_ROOT': str(root), 'CALENDAR_RESTORE_CONFIG': str(config),
        'CALENDAR_RESTORE_LOCK': str(tmp_path / 'host.lock'), 'CALENDAR_RESTORE_PYTHON': str(fakes / 'receiver'), **env})
    calls = [' '.join(json.loads(line)) for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls, root


# spec: ANYM-610
def test_the_restore_stops_at_any_failing_docker_call_and_always_tears_down(tmp_path):
    result, calls, root = restore_run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'restore proved' in result.stdout and 'left: nothing' in result.stdout and not list(root.iterdir())
    # The first three calls are the check for an interrupted run's leftovers; the count ends where teardown starts.
    first_teardown = next(i for i, call in enumerate(calls) if i > 3 and call.startswith('ps -aq'))
    assert first_teardown > 10
    for number in range(1, first_teardown + 1):
        result, seen, root = restore_run(tmp_path, FAKE_FAIL_AT=str(number))
        assert result.returncode != 0 and 'restore proved' not in result.stdout, number
        assert '== teardown' in result.stdout and not list(root.iterdir()), number
        assert any(call.startswith('ps -aq') for call in seen[number:]), number


# spec: ANYM-610
def test_a_teardown_docker_cannot_verify_never_reports_nothing_left(tmp_path):
    _, calls, _ = restore_run(tmp_path)
    first_teardown = next(i for i, call in enumerate(calls) if i > 3 and call.startswith('ps -aq'))
    result, _, _ = restore_run(tmp_path, FAKE_FAIL_FROM=str(first_teardown + 1))
    assert 'restore proved' in result.stdout and result.returncode != 0
    assert 'COULD NOT VERIFY' in result.stdout and 'left: nothing' not in result.stdout


# spec: ANYM-610
def test_a_docker_that_cannot_answer_the_leftovers_check_stops_the_run_before_it_starts(tmp_path):
    result, calls, _ = restore_run(tmp_path, FAKE_FAIL_FROM='1')
    assert result.returncode != 0 and 'COULD NOT VERIFY' in result.stdout and 'left: nothing' not in result.stdout
    assert not any(call.startswith(('run ', 'pull ', 'compose ')) for call in calls)


# spec: ANYM-610
def test_an_interrupted_runs_leftovers_are_removed_and_the_run_ends_without_restoring(tmp_path):
    (tmp_path / 'root').mkdir()
    (tmp_path / 'root/calendar-restore.left').mkdir()
    result, calls, root = restore_run(tmp_path)
    assert result.returncode == 1 and 'run it again' in result.stdout and not list(root.iterdir())
    assert not any(call.startswith(('run ', 'pull ', 'compose ')) for call in calls)


# spec: ANYM-610
def test_a_failed_checksum_stops_before_anything_is_decrypted(tmp_path):
    result, calls, root = restore_run(tmp_path, FAKE_SHA_STATUS='1')
    assert result.returncode != 0 and '== teardown' in result.stdout and not list(root.iterdir())
    assert not any(call.startswith('run ') for call in calls)


# spec: ANYM-610
def test_a_failed_pg_restore_prints_no_row_and_tears_down(tmp_path):
    result, _, root = restore_run(tmp_path, FAKE_RESTORE_FAILS='1')
    assert result.returncode != 0 and 'pg_restore failed' in result.stdout and not list(root.iterdir())
    assert 'private row' not in result.stdout + result.stderr


# spec: ANYM-610
def test_the_restore_without_the_host_lock_creates_nothing_and_tears_nothing_down(tmp_path):
    result, calls, root = restore_run(tmp_path, FAKE_FLOCK_STATUS='1')
    assert result.returncode != 0 and calls == [] and not list(root.iterdir()) and '== teardown' not in result.stdout


# spec: ANYM-610
def test_the_restore_without_the_disk_bound_stops_before_it_downloads(tmp_path):
    result, calls, root = restore_run(tmp_path, reserve=2 ** 62)
    assert result.returncode != 0 and 'restore_disk_capacity' in result.stderr
    assert not any(call.startswith(('run ', 'pull ', 'compose ')) for call in calls) and not list(root.iterdir())


# spec: ANYM-610
def test_every_restore_helper_carries_the_label_teardown_removes_and_the_verifiers_bounds():
    script = RESTORE_SCRIPT.read_text()
    assert script.count('docker run ') == 1  # only in helper()
    body = re.search(r'^helper\(\) \{\n(.*?)^\}', script, re.M | re.S)[1]
    for flag in ('--rm', '--label "$LABEL"', '--log-driver none', '--memory 512m', '--memory-swap 512m', '--cpus 1',
                 '--pids-limit 256', '--read-only', '--cap-drop ALL', '--security-opt no-new-privileges'):
        assert flag in body, flag
    assert script.index('flock --wait') < script.index('trap teardown EXIT') < script.index('mktemp -d')


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


CALLS_UNITS = ('deedkit-calls-backup.service', 'deedkit-calls-backup-catchup.service')
HOUR = 3600


def real_config(tmp_path, name):
    path = tmp_path / name
    path.write_text((DEPLOY / name).read_text())
    path.chmod(0o600)
    return backup_runner.config(path)


def timer_start(name):
    timer = unit_lines(name)['Timer']
    (start,) = backup_runner.timer_starts(DEPLOY / name)
    return start + backup_runner.timer_delay(DEPLOY / name) + seconds(timer['AccuracySec'][0])


def unit_bound(service):
    lines = unit_lines(service)['Service']
    return seconds(lines['TimeoutStartSec'][0]) + 2 * seconds(lines['TimeoutStopSec'][0])


# spec: ANYM-782
def test_the_calls_units_run_the_host_runner_with_their_config_recover_with_it_and_write_only_their_directories(tmp_path):
    cfg = real_config(tmp_path, 'backup-runner-calls.example.json')
    config = '/opt/deedkit/calls-backup/runner.json'
    for name in CALLS_UNITS:
        service = unit_lines(name)['Service']
        assert shlex.split(service['ExecStart'][0]) == ['/usr/bin/python3', '-I', RUNNER, '--config', config]
        stop = service['ExecStopPost'][0]
        assert stop.endswith(f'{RUNNER} --config {config} --recover-stop') or stop.endswith(f'{RUNNER} --config {config} --recover-stop\'')
        assert service['Type'] == ['oneshot'] and service['User'] == ['root'] and 'Restart' not in service
        assert set(service['ReadWritePaths'][0].split()) == {str(Path(cfg['directory']).parent), str(Path(cfg['host_lock']).parent)}
        assert all(key in service for key in ('NoNewPrivileges', 'PrivateTmp', 'ProtectHome', 'ProtectSystem'))
    assert 'ExecCondition' not in unit_lines(CALLS_UNITS[0])['Service']


# spec: ANYM-782
def test_the_calls_units_time_limit_covers_the_runs_budgets_and_stays_inside_the_admission_bound(tmp_path):
    cfg = real_config(tmp_path, 'backup-runner-calls.example.json')
    assert backup_runner.worst_case(cfg) == 5515
    # A smaller accepted budget never admits a run the unit would still let hold the lock.
    reduced = copy.deepcopy(cfg)
    reduced['busy_wait_seconds'] = 0
    assert backup_runner.worst_case(reduced) < 4400
    assert backup_runner.admission_bound(reduced) == backup_runner.admission_bound(cfg) - 115
    for name in CALLS_UNITS:
        service = unit_lines(name)['Service']
        assert seconds(service['TimeoutStartSec'][0]) >= backup_runner.worst_case(cfg) - backup_runner.RECOVERY_BUDGET
        assert seconds(service['TimeoutStopSec'][0]) == backup_runner.UNIT_STOP_SECONDS
        assert seconds(service['TimeoutStartSec'][0]) == backup_runner.CALLS_UNIT_START_SECONDS
        assert unit_bound(name) <= backup_runner.admission_bound(reduced)


# spec: ANYM-782
def test_the_calls_timers_are_read_by_the_runner_persistent_and_installed():
    for name, service in (('deedkit-calls-backup.timer', CALLS_UNITS[0]), ('deedkit-calls-backup-catchup.timer', CALLS_UNITS[1])):
        lines = unit_lines(name)
        assert len(backup_runner.timer_starts(DEPLOY / name)) == 1 and lines['Timer']['Persistent'] == ['true']
        assert lines['Timer']['AccuracySec'] == ['1s'] and lines['Timer']['Unit'] == [service]
        assert lines['Install']['WantedBy'] == ['timers.target']


# spec: ANYM-782
def test_the_calls_catchup_clock_condition_admits_only_04_00_to_04_59_and_fails_closed(tmp_path):
    script = sh_script(unit_lines(CALLS_UNITS[1])['Service']['ExecCondition'][0])
    for clock in ('0400', '0415', '0459'):
        assert run_condition(script, tmp_path, clock=clock, freshness=0), clock
    for clock in ('0000', '0215', '0359', '0500', '0859', '1600', '2359', 'x', ''):
        assert not run_condition(script, tmp_path, clock=clock, freshness=0), clock


# spec: ANYM-782
def test_the_calls_catchup_starts_only_for_a_stale_or_unknown_status_file(tmp_path):
    script = sh_script(unit_lines(CALLS_UNITS[1])['Service']['ExecCondition'][1])
    start = shlex.split(script.split(';', 1)[0].replace('>/dev/null 2>&1', ''))
    assert start[:3] == ['/usr/bin/python3', '-I', '/opt/deedkit/calls-backup/freshness.py']
    assert start[start.index('--status') + 1] == '/opt/deedkit/calls-backup/backups/status.json'
    assert start[start.index('--max-age-hours') + 1] == '22'
    assert run_condition(script, tmp_path, freshness=4) and run_condition(script, tmp_path, freshness=5)
    for code in (0, 2, 127):
        assert not run_condition(script, tmp_path, freshness=code)


# spec: ANYM-782
def test_every_installed_example_is_accepted_and_only_calls_and_recordings_name_each_others_timers(tmp_path):
    calls = real_config(tmp_path, 'backup-runner-calls.example.json')
    recordings = real_config(tmp_path, 'backup-runner-recordings.example.json')
    calendar = real_config(tmp_path, 'backup-runner-calendar.example.json')
    assert 'timers' not in calendar and 'calls' not in calendar
    assert {Path(t).name for t in recordings['recordings']['timers']} >= {'deedkit-calls-backup.timer', 'deedkit-calls-backup-catchup.timer'}
    assert {Path(t).name for t in calls['calls']['timers']} == {
        'personal-os-backup.timer', 'deedkit-calendar-backup.timer', 'deedkit-calendar-backup-catchup.timer',
        'deedkit-recordings-backup.timer', 'deedkit-recordings-backup-catchup.timer'}


# spec: ANYM-782
def test_each_profiles_worst_case_ends_before_the_next_start_and_the_latest_evening_catchups_clear_the_calls_slot(tmp_path):
    calls = real_config(tmp_path, 'backup-runner-calls.example.json')
    recordings = real_config(tmp_path, 'backup-runner-recordings.example.json')
    day = 24 * HOUR
    slot, retry = timer_start('deedkit-calls-backup.timer'), timer_start('deedkit-calls-backup-catchup.timer')
    # (start, bound, next start). Messaging's end is its own unit's and the shared lock's; its unit is not in this repository.
    runs = [
        (timer_start('deedkit-calendar-backup.timer'), unit_bound('deedkit-calendar-backup.service'), MESSAGING_RUN),
        (timer_start('deedkit-calendar-backup-catchup.timer'), unit_bound('deedkit-calendar-backup-catchup.service'),
         timer_start('deedkit-recordings-backup.timer')),
        (timer_start('deedkit-recordings-backup.timer'), backup_runner.admission_bound(recordings),
         timer_start('deedkit-recordings-backup-catchup.timer')),
        (timer_start('deedkit-recordings-backup-catchup.timer'), backup_runner.admission_bound(recordings), day + 2 * HOUR + 15 * 60),
        (slot, backup_runner.admission_bound(calls), retry),
        (retry, backup_runner.admission_bound(calls), 16 * HOUR)]
    for start, bound, following in runs:
        assert start + bound <= following, (start, bound, following)
    last = day - 1  # a clock condition admits a catch-up until 23:59:59
    for bound in (unit_bound('deedkit-calendar-backup-catchup.service'), backup_runner.admission_bound(recordings)):
        assert last + bound + backup_runner.GUARD_SECONDS <= day + backup_runner.timer_starts(DEPLOY / 'deedkit-calls-backup.timer')[0]


# spec: ANYM-782
def test_the_runners_admission_reading_the_real_timer_files_admits_the_calls_and_recordings_slots(tmp_path):
    (tmp_path / 'personal-os-backup.timer').write_text('[Timer]\nOnCalendar=*-*-* 18:00:00 UTC\n')
    def admit(cfg, own, clock):
        paths = [tmp_path / Path(t).name if Path(t).name == 'personal-os-backup.timer' else DEPLOY / Path(t).name
                 for t in own['timers']]
        runner = SimpleNamespace(clock=lambda: 20000 * 86400 + clock, cfg=cfg, own={'timers': paths},
                                 code=lambda word: f"{cfg['profile']}_{word}")
        backup_runner.Runner.admit(runner)
    calls = real_config(tmp_path, 'backup-runner-calls.example.json')
    recordings = real_config(tmp_path, 'backup-runner-recordings.example.json')
    for name in ('deedkit-calls-backup.timer', 'deedkit-calls-backup-catchup.timer'):
        admit(calls, calls['calls'], timer_start(name))
    for name in ('deedkit-recordings-backup.timer', 'deedkit-recordings-backup-catchup.timer'):
        admit(recordings, recordings['recordings'], timer_start(name))
    admit(recordings, recordings['recordings'], 24 * HOUR - 1)  # the catch-up's last admitted second keeps its start
    # The bound reaches the calendar's 16:00 less the guard: one second later is refused, whatever the configured budget.
    latest = 16 * HOUR - backup_runner.GUARD_SECONDS - backup_runner.admission_bound(calls)
    admit(calls, calls['calls'], latest)
    with pytest.raises(backup_runner.Skip, match='calls_skipped_window'):
        admit(calls, calls['calls'], latest + 1)


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
