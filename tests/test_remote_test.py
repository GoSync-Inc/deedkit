"""scripts/remote-test.sh: DeedKit suites on the Mac mini as _deedkitci (owner D41/D42).

The mini is this machine: fakes of ssh, sudo, docker, uv, df, sysctl and the PostgreSQL binaries stand in
for it; rsync, git, tar and bash are real, so the copy, the secret filter and the traps are the script's own.
"""
import fcntl
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'remote-test.sh'
WORKER = 'infra_remote_test'

pytestmark = pytest.mark.skipif(not shutil.which('rsync') or not shutil.which('bash'),
                                reason='remote-test.sh needs bash and rsync')

FAKES = {
    # Every ssh runs its command here, in the owner's home, with the caller's stdin.
    'ssh': '''#!/usr/bin/env bash
echo "ssh $*" >> "$FAKE_LOG"
[ "${FAKE_SSH_DOWN:-0}" = 1 ] && exit 255
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -[ntq]) shift ;; *) break ;; esac; done
shift
cd "$FAKE_OWNER_HOME" && HOME="$FAKE_OWNER_HOME" exec bash -c "$*"
''',
    'sudo': '''#!/usr/bin/env bash
echo "sudo $*" >> "$FAKE_LOG"
[ "${FAKE_NO_SERVICE:-0}" = 1 ] && { echo "sudo: a password is required" >&2; exit 1; }
while [ $# -gt 0 ]; do case "$1" in -n) shift ;; -u) shift 2 ;; *) break ;; esac; done
HOME="$FAKE_SERVICE_HOME" exec "$@"
''',
    'docker': '''#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
[ "$1" = ps ] && printf '%s' "${FAKE_DOCKER_PS:-}"
exit 0
''',
    'uv': '''#!/usr/bin/env bash
dsn=""; [ -n "${PROBE_TEST_DSN_FILE:-}" ] && dsn="$(cat "$PROBE_TEST_DSN_FILE")"
echo "uv $* | cwd=${PWD##*/} dsn=$dsn shard=${DEEDKIT_TEST_SHARD:-} fcs=${FCS_TEST_DSN:-}" >> "$FAKE_LOG"
[ "$1" = run ] || exit 0
echo "FAKE SUITE OUTPUT ${DEEDKIT_TEST_SHARD:-}"
[ "${FAKE_UV_SLEEP:-0}" = 0 ] || sleep "$FAKE_UV_SLEEP"
exit "${FAKE_UV_EXIT:-0}"
''',
    'df': '''#!/usr/bin/env bash
echo "Filesystem 1024-blocks Used Available Capacity Mounted"
echo "/dev/fake 999999999 1 $((${FAKE_FREE_GB:-100} * 1048576)) 1% /"
''',
    'sysctl': '''#!/usr/bin/env bash
echo "{ ${FAKE_LOAD:-1.00} 1.00 1.00 }"
''',
}
PG_FAKES = {
    'initdb': '''#!/usr/bin/env bash
echo "initdb $*" >> "$FAKE_LOG"
while [ $# -gt 0 ]; do [ "$1" = -D ] && mkdir -p "$2"; shift; done
''',
    'postgres': '''#!/usr/bin/env bash
echo "postgres $*" >> "$FAKE_LOG"
echo $$ > "$2/postmaster.pid"
trap 'echo "postgres stopped by QUIT" >> "$FAKE_LOG"; exit 0' QUIT
while :; do sleep 0.1; done
''',
    'pg_isready': '#!/usr/bin/env bash\nexit 0\n',
    'createdb': '#!/usr/bin/env bash\necho "createdb $*" >> "$FAKE_LOG"\n',
}
SHIM = '#!/usr/bin/env bash\ncd "$HOME" && exec bash -s -- "$@"\n'


def write(path, text, mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


@pytest.fixture
def mini(tmp_path):
    for name, text in FAKES.items():
        write(tmp_path / 'bin' / name, text)
    for name, text in PG_FAKES.items():
        write(tmp_path / 'pgbin' / name, text)
    write(tmp_path / 'shim', SHIM)
    for name in ('owner', 'service', 'shared/stage', 'local'):
        (tmp_path / name).mkdir(parents=True)
    log = tmp_path / 'log'
    log.touch()
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", FAKE_LOG=str(log),
               FAKE_OWNER_HOME=str(tmp_path / 'owner'), FAKE_SERVICE_HOME=str(tmp_path / 'service'),
               DEEDKIT_REMOTE_HOST='mini', DEEDKIT_REMOTE_SHIM=str(tmp_path / 'shim'),
               DEEDKIT_REMOTE_SHARED=str(tmp_path / 'shared'), DEEDKIT_REMOTE_STAGE=str(tmp_path / 'stage'),
               DEEDKIT_CI_LOCAL_ROOT=str(tmp_path / 'local'), DEEDKIT_RUNNER_PATH_DIRS='',
               DEEDKIT_PG_BIN=str(tmp_path / 'pgbin'), DEEDKIT_REMOTE_WORKER=WORKER)
    for name in ('DEEDKIT_CI_ROOT', 'PROBE_TEST_DSN_FILE', 'FCS_TEST_DSN', 'DEEDKIT_TEST_SHARD', 'LOCAL_RUN'):
        env.pop(name, None)
    return tmp_path, env


@pytest.fixture
def worktree(tmp_path):
    tree = tmp_path / 'worktrees' / 'infra' / 'remote-test'
    files = {'app.py': 'print(1)\n', 'tests/test_app.py': 'def test_app(): pass\n',
             'services/gateway/tests/test_credential_expiry.py': 'def test_expiry(): pass\n',
             'services/calendar/pyproject.toml': '', 'services/calls/pyproject.toml': '',
             'scripts/python_shards.py': '', 'gone.py': 'deleted below\n', '.gitignore': 'ignored.log\n',
             # Secret-shaped files that are tracked anyway.
             'deploy/.env.example': 'A=1\n', 'deploy/telegram-credentials.example.json': '{}\n',
             'keys/server.pem': 'synthetic\n', 'config/api_token.json': '{}\n', 'x/auth.json': '{}\n'}
    for name, text in files.items():
        write(tree / name, text, 0o644)
    git = ['git', '-C', str(tree), '-c', 'user.name=t', '-c', 'user.email=t@invalid', '-c', 'commit.gpgsign=false']
    subprocess.run(['git', 'init', '-q', str(tree)], check=True)
    subprocess.run([*git, 'add', '-A'], check=True)
    subprocess.run([*git, 'commit', '-qm', 'synthetic'], check=True)
    (tree / 'gone.py').unlink()
    # Untracked, not ignored: the working diff, and secrets a worker might leave lying around.
    untracked = {'new_module.py': 'x = 1\n', '.env': 'K=synthetic\n', 'local.env': 'K=1\n', 'id_ed25519': 'k\n',
                 '.tiden/session.json': '{}\n', '.venv/lib/site.py': '', 'node_modules/a/index.js': '',
                 'creds/credentials': 'k\n', 'tls.key': 'k\n', '.netrc': 'k\n', '.npmrc': 'k\n',
                 'test.dsn': 'postgresql://x\n', 'telegram.session': 'k\n', 'ignored.log': 'x\n',
                 'whatsapp/session.sqlite': 'k\n', 'secrets/token.txt': 'k\n'}
    for name, text in untracked.items():
        write(tree / name, text, 0o644)
    return tree


SENT = {'app.py', 'tests/test_app.py', 'services/gateway/tests/test_credential_expiry.py',
        'services/calendar/pyproject.toml', 'services/calls/pyproject.toml', 'scripts/python_shards.py',
        '.gitignore', 'new_module.py'}


def remote(env, *args, **extra):
    return subprocess.run(['bash', str(SCRIPT), *map(str, args)], env=dict(env, **extra), capture_output=True,
                          text=True, timeout=120)


def files_under(root):
    return {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and '.git' not in p.parts}


def log_lines(home, prefix):
    return [line for line in (home / 'log').read_text().splitlines() if line.startswith(prefix)]


def test_the_list_holds_tracked_and_untracked_files_but_no_secret_environment_or_deleted_file(mini, worktree):
    _, env = mini
    result = remote(env, '--list', worktree)
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.split()) == SENT


def test_a_run_sends_exactly_the_listed_files_and_runs_the_suite_as_the_service_user(mini, worktree):
    home, env = mini
    result = remote(env, worktree, 'gateway')
    assert result.returncode == 0, result.stderr
    assert 'FAKE SUITE OUTPUT' in result.stdout
    assert files_under(home / 'shared' / 'stage' / WORKER) == SENT
    copy = home / 'service' / 'deedkit-ci' / WORKER / 'src'
    assert files_under(copy) == SENT
    # The copy is a one-commit repository of itself, for the tests that read `git ls-files`.
    tracked = subprocess.run(['git', '-C', str(copy), 'ls-files'], capture_output=True, text=True, check=True)
    assert set(tracked.stdout.split()) == SENT
    assert any(f'-u _deedkitci {home / "shim"} --run {WORKER} gateway' in line for line in log_lines(home, 'sudo'))
    assert log_lines(home, 'uv run pytest -q | cwd=gateway')


def test_a_second_run_removes_what_the_worktree_no_longer_has_and_keeps_the_environment(mini, worktree):
    home, env = mini
    assert remote(env, worktree, 'gateway').returncode == 0
    copy = home / 'service' / 'deedkit-ci' / WORKER / 'src'
    write(copy / '.venv' / 'kept.py', '', 0o644)
    (worktree / 'new_module.py').unlink()
    assert remote(env, worktree, 'gateway').returncode == 0
    assert files_under(copy) == SENT - {'new_module.py'} | {'.venv/kept.py'}


def test_the_root_suite_gets_a_postgres_on_localhost_and_a_private_dsn_file(mini, worktree):
    home, env = mini
    result = remote(env, worktree, 'root', 'tests/test_app.py')
    assert result.returncode == 0, result.stderr
    [postgres] = log_lines(home, 'postgres -D')
    assert '-c listen_addresses=127.0.0.1' in postgres
    port = postgres.split(' -p ')[1].split()[0]
    [pytest_line] = log_lines(home, 'uv run pytest tests/test_app.py')
    dsn_text = pytest_line.split('dsn=')[1].split()[0]
    assert dsn_text.startswith('postgresql://probe_owner:')
    assert dsn_text.endswith(f'@127.0.0.1:{port}/messaging_probe_test')
    syncs = [line.split('cwd=')[1].split()[0] for line in log_lines(home, 'uv sync --frozen --group dev')]
    assert syncs == ['src', 'gateway', 'calendar']
    dsn = home / 'service' / 'deedkit-ci' / WORKER / 'test-1.dsn'
    assert dsn.stat().st_mode & 0o777 == 0o600
    # Stopped at exit (immediate shutdown) and its cluster removed.
    assert log_lines(home, 'postgres stopped by QUIT')
    assert not list((home / 'service' / 'deedkit-ci' / WORKER).glob('pg*'))
    assert 'skipped: needs docker (CI covers)' in result.stderr


def test_the_full_root_suite_runs_parallel_shards_each_with_its_own_database(mini, worktree):
    home, env = mini
    result = remote(env, worktree, 'root')
    assert result.returncode == 0, result.stderr
    shards = sorted(line.split('shard=')[1].split()[0] for line in log_lines(home, 'uv run python scripts/python_shards.py'))
    assert shards == ['1/2', '2/2']
    dsns = {line.split('dsn=')[1].split()[0] for line in log_lines(home, 'uv run python scripts/python_shards.py')}
    assert len(dsns) == 2 and len(log_lines(home, 'postgres -D')) == 2
    assert '[1/2] FAKE SUITE OUTPUT 1/2' in result.stdout and '[2/2] FAKE SUITE OUTPUT 2/2' in result.stdout


def test_caps_on_the_mini_drop_the_root_suite_to_one_process_without_a_code_change(mini, worktree):
    home, env = mini
    write(home / 'shared' / 'caps.conf', 'PARALLEL=1\nMAX_RUNS=1\n', 0o644)
    assert remote(env, worktree, 'root').returncode == 0
    assert not log_lines(home, 'uv run python scripts/python_shards.py')
    assert len(log_lines(home, 'uv run pytest --tb=short')) == 1


def test_the_disk_guard_refuses_under_15_gigabytes_before_anything_runs(mini, worktree):
    home, env = mini
    result = remote(env, worktree, 'root', FAKE_FREE_GB='14', DEEDKIT_REMOTE_FALLBACK='0')
    assert result.returncode == 90
    assert 'disk guard: 14 G free, need 15 G' in result.stderr
    assert not log_lines(home, 'postgres') and not log_lines(home, 'uv')
    assert not (home / 'shared' / 'stage' / WORKER).exists()


def test_a_failing_suite_stops_postgres_and_returns_the_suite_status(mini, worktree):
    home, env = mini
    result = remote(env, worktree, 'calendar', FAKE_UV_EXIT='3')
    assert result.returncode == 3
    assert log_lines(home, 'postgres stopped by QUIT')
    assert 'fcs=postgresql://probe_owner:' in log_lines(home, 'uv run pytest -m not live')[0]


def test_a_terminated_run_stops_its_children_and_postgres(mini, worktree):
    home, env = mini
    process = subprocess.Popen(['bash', str(SCRIPT), str(worktree), 'root', 'tests/test_app.py'],
                               env=dict(env, FAKE_UV_SLEEP='60'), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True)
    pidfile = home / 'service' / 'deedkit-ci' / WORKER / 'run.pid'
    deadline = time.time() + 60
    while not log_lines(home, 'uv run pytest') and time.time() < deadline:
        time.sleep(0.2)
    os.kill(int(pidfile.read_text()), signal.SIGTERM)
    _, stderr = process.communicate(timeout=30)
    assert process.returncode == 143, stderr
    assert log_lines(home, 'postgres stopped by QUIT')
    assert not pidfile.exists()
    line = (home / 'service' / 'deedkit-ci' / 'load.log').read_text().splitlines()[-1].split('\t')
    assert line[1] == 'root' and line[-1] == '143'


def test_each_mini_run_appends_one_load_line(mini, worktree):
    home, env = mini
    assert remote(env, worktree, 'root', 'tests/test_app.py').returncode == 0
    assert remote(env, worktree, 'gateway').returncode == 0
    lines = [line.split('\t') for line in (home / 'service' / 'deedkit-ci' / 'load.log').read_text().splitlines()]
    assert [(line[0], line[1], line[-1]) for line in lines] == [(WORKER, 'root', '0'), (WORKER, 'gateway', '0')]
    for _, _, start, wall, cpu, pg, _ in lines:
        assert start.endswith('Z') and int(wall) >= 0 and float(cpu) >= 0 and float(pg) >= 0
    assert remote(env, '--load').stdout.count(WORKER) == 2


def test_a_taken_slot_sends_the_run_to_the_laptop(mini, worktree):
    home, env = mini
    locks = home / 'service' / 'deedkit-ci' / '.locks'
    locks.mkdir(parents=True)
    with open(locks / 'slot1', 'a') as slot:
        fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
        refused = remote(env, worktree, 'gateway', DEEDKIT_REMOTE_FALLBACK='0')
        assert refused.returncode == 90 and 'all 1 DeedKit slot(s) taken' in refused.stderr
        fallback = remote(env, worktree, 'gateway')
    assert fallback.returncode == 0, fallback.stderr
    assert 'running gateway on this machine' in fallback.stderr
    assert log_lines(home, 'uv run pytest -q | cwd=gateway')
    assert not (home / 'shared' / 'stage' / WORKER).exists()


@pytest.mark.parametrize('extra', [{'FAKE_DOCKER_PS': 'between-ci-1234-test \n'},
                                   {'FAKE_DOCKER_PS': 'app-ready-1 io.between.local-ci.run=abc\n'},
                                   {'FAKE_LOAD': '9.50'}], ids=['between-container', 'between-label', 'load'])
def test_between_activity_refuses_the_mini(mini, worktree, extra):
    home, env = mini
    result = remote(env, worktree, 'gateway', DEEDKIT_REMOTE_FALLBACK='0', **extra)
    assert result.returncode == 90 and 'mini busy' in result.stderr
    assert not log_lines(home, 'sudo')


@pytest.mark.parametrize('extra,code', [({'FAKE_SSH_DOWN': '1'}, 91), ({'FAKE_NO_SERVICE': '1'}, 92)],
                         ids=['unreachable', 'no-service-user'])
def test_an_unreachable_or_unprepared_mini_falls_back_to_the_local_flow(mini, worktree, extra, code):
    home, env = mini
    assert remote(env, worktree, 'gateway', DEEDKIT_REMOTE_FALLBACK='0', **extra).returncode == code
    result = remote(env, worktree, 'gateway', **extra)
    assert result.returncode == 0, result.stderr
    assert 'running gateway on this machine' in result.stderr
    assert not (home / 'shared' / 'stage' / WORKER).exists()


def test_cleanup_removes_the_workers_copies_and_leftover_databases_only(mini, worktree):
    home, env = mini
    assert remote(env, worktree, 'gateway').returncode == 0
    other = home / 'service' / 'deedkit-ci' / 'other_worker' / 'src'
    write(other / 'kept.py', '', 0o644)
    leftover = home / 'service' / 'deedkit-ci' / WORKER / 'pg1' / 'cluster'
    sleeper = subprocess.Popen(['sleep', '60'])
    write(leftover / 'postmaster.pid', f'{sleeper.pid}\n', 0o600)
    result = remote(env, '--cleanup', WORKER)
    assert result.returncode == 0, result.stderr
    assert sleeper.wait(timeout=10) == -signal.SIGQUIT
    assert not (home / 'service' / 'deedkit-ci' / WORKER).exists()
    assert not (home / 'shared' / 'stage' / WORKER).exists()
    assert (other / 'kept.py').exists()


@pytest.mark.parametrize('name', ['../owner', 'A', '', 'a/b', '_x'])
def test_a_worker_name_that_could_leave_its_directory_is_refused(mini, name):
    home, env = mini
    result = remote(env, '--cleanup', name)
    assert result.returncode == 2
    assert not log_lines(home, 'ssh')
